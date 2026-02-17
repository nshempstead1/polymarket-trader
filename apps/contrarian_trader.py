#!/usr/bin/env python3
"""
Contrarian Trading Daemon - True Mirror of auto_trader.py

Same signals, same timing, opposite side on every trade.

Every strategy detects the EXACT same condition as the original, but
buys the OTHER outcome in the market:

- MirrorValueScanner: finds cheap outcomes (10-35%) → buys the OTHER outcome
- MirrorSwingTrader: detects price drops → buys the OTHER outcome
- MirrorArbitrage: finds cheapest when sum < 1 → buys the OTHER outcome
- MirrorFlashCrash: detects crash on a side → buys the OTHER side

If the original auto_trader lost on every trade, this would have won
on every trade — same entry time, opposite position.

Usage:
    python apps/contrarian_trader.py --dry-run          # Watch without executing
    python apps/contrarian_trader.py                    # Live trading
    python apps/contrarian_trader.py --strategies value,swing
    python apps/contrarian_trader.py --default-trade 15 --max-exposure 300
"""

import os, sys, asyncio, argparse, logging, time, signal
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.config import Config
from src.market_search import MarketSearch
from src.gamma_client import GammaClient
from lib.risk_manager import RiskManager, RiskConfig
from lib.trade_journal import TradeJournal

def setup_logging(log_file="logs/contrarian_trader.log", debug=False):
    Path("logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file); fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))
    ch = logging.StreamHandler(); ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
    logging.basicConfig(level=logging.DEBUG, handlers=[fh, ch])
    for n in ["src.websocket_client","src.bot","urllib3"]: logging.getLogger(n).setLevel(logging.WARNING)
    return logging.getLogger("contrarian_trader")


def get_other_outcome(market: dict, triggered_outcome: str) -> tuple:
    """
    Get the OTHER outcome in a binary market.

    Args:
        market: Market dict with token_ids and prices
        triggered_outcome: The outcome the original bot would have bought

    Returns:
        (other_outcome, other_token_id, other_price) or (None, None, None)
    """
    token_ids = market.get("token_ids", {})
    prices = market.get("prices", {})
    for outcome, tid in token_ids.items():
        if outcome != triggered_outcome:
            return outcome, tid, prices.get(outcome, 0)
    return None, None, None


async def execute_buy(bot, risk, journal, strategy, market, outcome, token_id, price, signals, dry_run, log):
    """Shared buy execution."""
    cid, q = market.get("condition_id",""), market.get("question","")
    size_usdc = risk.config.default_trade_size
    size_shares = size_usdc / price
    allowed, reason = risk.check_trade(strategy=strategy, condition_id=cid, token_id=token_id, price=price, size_usdc=size_usdc, side="BUY")
    if not allowed:
        journal.log_decision(strategy=strategy, action="BUY", result="rejected", market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, signals=signals, rejection_reason=reason)
        log.debug(f"Rejected: {reason} - {q[:50]}")
        return False
    did = journal.log_decision(strategy=strategy, action="BUY", result="dry_run" if dry_run else "executed", market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, signals=signals)
    journal.log_snapshot(token_id=token_id, mid_price=price, best_bid=signals.get("best_bid",0), best_ask=signals.get("best_ask",0), spread=signals.get("spread",0), bid_depth_5=signals.get("bid_depth",0), volume_24h=market.get("volume_24h",0), liquidity=market.get("liquidity",0), decision_id=did)
    log.info(f"[{strategy}] BUY {outcome.upper()} @ {price:.4f} ${size_usdc:.2f} - {q[:55]}")
    if dry_run:
        log.info(f"[{strategy}] [DRY RUN] Would have placed order"); return True
    result = await bot.place_order(token_id=token_id, price=min(price+0.02,0.95), size=size_shares, side="BUY")
    if result.success:
        pid = risk.register_trade(strategy=strategy, market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, side="BUY", price=price, size_shares=size_shares, size_usdc=size_usdc, order_id=result.order_id)
        journal.log_trade(strategy=strategy, side="BUY", price=price, size_shares=size_shares, size_usdc=size_usdc, market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, order_id=result.order_id, order_status="placed", decision_id=did)
        journal.open_position(position_id=pid, strategy=strategy, entry_price=price, size_shares=size_shares, size_usdc=size_usdc, market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, entry_order_id=result.order_id, entry_signals=signals)
        log.info(f"[{strategy}] Order placed: {result.order_id}"); return True
    else:
        journal.log_decision(strategy=strategy, action="BUY", result="failed", market_question=q, condition_id=cid, token_id=token_id, outcome=outcome, signals=signals, notes=result.message)
        log.warning(f"[{strategy}] Order failed: {result.message}"); return False


async def execute_sell(bot, risk, journal, pos, current_price, exit_reason, dry_run, log):
    """Shared sell execution."""
    pnl = pos.unrealized_pnl(current_price)
    log.info(f"[{pos.strategy}] SELL {pos.outcome.upper()} {pos.entry_price:.4f}->{current_price:.4f} PnL: ${pnl:+.2f} ({exit_reason})")
    order_id = ""
    if not dry_run:
        result = await bot.place_order(token_id=pos.token_id, price=max(current_price-0.02,0.01), size=pos.size_shares, side="SELL")
        if result.success: order_id = result.order_id
        else: log.warning(f"Sell failed: {result.message}"); return
    risk.close_position(pos.id, current_price, pnl)
    journal.close_position(position_id=pos.id, exit_price=current_price, realized_pnl=pnl, exit_reason=exit_reason, exit_order_id=order_id)
    journal.log_trade(strategy=pos.strategy, side="SELL", price=current_price, size_shares=pos.size_shares, size_usdc=pos.size_shares*current_price, market_question=pos.market_question, condition_id=pos.condition_id, token_id=pos.token_id, outcome=pos.outcome, order_id=order_id, order_status="placed")


# =============================================================================
# MIRROR STRATEGY 1: MirrorValueScanner
#
# Original: finds cheap outcomes (10-35%), buys them
# Mirror:   finds cheap outcomes (10-35%), buys the OTHER outcome instead
# Same trigger, same time, opposite side.
# =============================================================================

class MirrorValueScanner:
    def __init__(s, bot, risk, search, journal, dry_run=False):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.log=logging.getLogger("mirror_value"); s.scan_interval=300; s.tp=0.15; s.sl=0.10; s._seen=set()

    async def run(s):
        s.log.info("Mirror Value Scanner started (same signal, opposite side)")
        while True:
            try:
                if not s.risk.is_halted: await s._scan(); await s._manage()
                await asyncio.sleep(s.scan_interval)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)

    async def _scan(s):
        try: markets = await asyncio.to_thread(s.search.find_markets, "", active_only=True, limit=50)
        except: return
        for m in markets:
            cid=m.get("condition_id","")
            if not m.get("accepting_orders") or cid in s._seen or m.get("liquidity",0)<s.risk.config.min_liquidity: continue
            for outcome,price in m.get("prices",{}).items():
                # SAME signal as original: find cheap outcomes (10-35%)
                if not(0.10<=price<=0.35 and m.get("liquidity",0)>5000): continue
                tid=m.get("token_ids",{}).get(outcome)
                if not tid: continue
                # MIRROR: get the OTHER outcome
                other_outcome, other_tid, other_price = get_other_outcome(m, outcome)
                if not other_tid or other_price <= 0: continue
                try:
                    book=await asyncio.to_thread(s.search.get_orderbook,other_tid)
                    if not book or not book.get("bids") or not book.get("asks"): continue
                    bb,ba=float(book["bids"][0]["price"]),float(book["asks"][0]["price"])
                    spread=ba-bb; depth=sum(float(b["size"]) for b in book["bids"][:5])
                    if spread>0.10 or depth<50: continue
                    mid=(bb+ba)/2
                    sig={"triggered_by":outcome,"triggered_price":price,"mirror_outcome":other_outcome,"price":mid,"spread":spread,"bid_depth":depth,"best_bid":bb,"best_ask":ba,"liquidity":m.get("liquidity",0),"volume_24h":m.get("volume_24h",0),"score":depth/spread if spread>0 else 0}
                    if await execute_buy(s.bot,s.risk,s.journal,"mirror_value",m,other_outcome,other_tid,mid,sig,s.dry_run,s.log): s._seen.add(cid)
                except Exception as e: s.log.debug(f"Skipped: {e}"); continue

    async def _manage(s):
        for p in [p for p in s.risk.get_all_positions() if p.strategy=="mirror_value"]:
            try:
                pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                if pr is None: continue
                s.journal.update_position_extremes(p.id,pr); ch=pr-p.entry_price
                if ch>=s.tp: await execute_sell(s.bot,s.risk,s.journal,p,pr,"take_profit",s.dry_run,s.log)
                elif ch<=-s.sl: await execute_sell(s.bot,s.risk,s.journal,p,pr,"stop_loss",s.dry_run,s.log)
                elif (time.time()-p.entry_time)>86400: await execute_sell(s.bot,s.risk,s.journal,p,pr,"time_exit_24h",s.dry_run,s.log)
            except Exception as e: s.log.error(f"Pos error: {e}")


# =============================================================================
# MIRROR STRATEGY 2: MirrorSwingTrader
#
# Original: detects price drop >= 0.08 over 30 min, buys that token
# Mirror:   detects the same drop, buys the OTHER token in that market
# Same trigger, same time, opposite side.
# =============================================================================

class MirrorSwingTrader:
    def __init__(s, bot, risk, search, journal, dry_run=False):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.log=logging.getLogger("mirror_swing"); s.interval=60; s.thresh=0.08; s.tp=0.10; s.sl=0.08
        s.history:Dict[str,List[tuple]]={}; s._wl:Dict[str,dict]={}; s._wl_t=0.0

    async def run(s):
        s.log.info("Mirror Swing Trader started (same signal, opposite side)")
        while True:
            try:
                if s.risk.is_halted: await asyncio.sleep(60); continue
                if not s._wl or (time.time()-s._wl_t)>1800:
                    try:
                        ms=await asyncio.to_thread(s.search.get_trending,20); s._wl={}
                        for m in ms:
                            if not m.get("accepting_orders") or m.get("liquidity",0)<5000: continue
                            for o,tid in m.get("token_ids",{}).items(): s._wl[tid]={"market":m,"outcome":o,"condition_id":m["condition_id"]}
                        s.log.info(f"Watchlist: {len(s._wl)} tokens"); s._wl_t=time.time()
                    except Exception as e: s.log.debug(f"Error: {e}")
                for tid,info in s._wl.items():
                    try:
                        pr=await asyncio.to_thread(s.search.get_market_price,tid)
                        if pr is None: continue
                        now=time.time()
                        if tid not in s.history: s.history[tid]=[]
                        s.history[tid].append((now,pr)); s.history[tid]=[(t,p) for t,p in s.history[tid] if t>now-3600]
                        if len(s.history[tid])<10: continue
                        old=None
                        for t,p in s.history[tid]:
                            if t>=now-1800: old=p; break
                        # SAME signal as original: detect price drop
                        if old and (pr-old)<=-s.thresh and pr>=0.10:
                            # MIRROR: buy the OTHER outcome
                            other_outcome, other_tid, other_price = get_other_outcome(info["market"], info["outcome"])
                            if not other_tid: continue
                            # Get live price for the other side
                            other_pr = await asyncio.to_thread(s.search.get_market_price, other_tid)
                            if other_pr is None or other_pr <= 0: continue
                            sig={"triggered_by":info["outcome"],"swing":pr-old,"old_price":old,"triggered_price":pr,"mirror_outcome":other_outcome,"mirror_price":other_pr,"lookback_min":30,"liquidity":info["market"].get("liquidity",0)}
                            await execute_buy(s.bot,s.risk,s.journal,"mirror_swing",info["market"],other_outcome,other_tid,other_pr,sig,s.dry_run,s.log)
                    except Exception as e: s.log.debug(f"Skipped: {e}"); continue
                for p in [p for p in s.risk.get_all_positions() if p.strategy=="mirror_swing"]:
                    try:
                        pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                        if pr is None: continue
                        s.journal.update_position_extremes(p.id,pr); ch=pr-p.entry_price
                        if ch>=s.tp: await execute_sell(s.bot,s.risk,s.journal,p,pr,"take_profit",s.dry_run,s.log)
                        elif ch<=-s.sl: await execute_sell(s.bot,s.risk,s.journal,p,pr,"stop_loss",s.dry_run,s.log)
                    except Exception as e: s.log.debug(f"Error: {e}")
                await asyncio.sleep(s.interval)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)


# =============================================================================
# MIRROR STRATEGY 3: MirrorArbitrage
#
# Original: finds cheapest outcome when sum < 1.0, buys cheapest
# Mirror:   same detection, buys the OTHER outcome instead
# Same trigger, same time, opposite side.
# =============================================================================

class MirrorArbitrage:
    def __init__(s, bot, risk, search, journal, dry_run=False):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.log=logging.getLogger("mirror_arb"); s.interval=120; s.min_mis=0.05

    async def run(s):
        s.log.info("Mirror Arbitrage started (same signal, opposite side)")
        while True:
            try:
                if not s.risk.is_halted: await s._scan(); await s._manage()
                await asyncio.sleep(s.interval)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)

    async def _scan(s):
        try: events=await asyncio.to_thread(s.search.get_events,"",30)
        except: return
        for ev in events:
            ms=ev.get("markets",[])
            for m in ms:
                pr=m.get("prices",{})
                if len(pr)!=2: continue
                ps=sum(pr.values())
                # SAME signal as original: sum < 1.0 (underpriced)
                if ps<(1.0-s.min_mis):
                    # Original would buy cheapest
                    ch=min(pr,key=pr.get); cp=pr[ch]
                    fp=cp/ps; edge=fp-cp
                    if edge<0.03: continue
                    # MIRROR: buy the OTHER outcome instead
                    other_outcome, other_tid, other_price = get_other_outcome(m, ch)
                    if not other_tid or other_price<0.05 or other_price>0.90: continue
                    sig={"price_sum":ps,"triggered_by":ch,"triggered_price":cp,"fair_price":fp,"edge":edge,"mirror_outcome":other_outcome,"mirror_price":other_price,"liquidity":m.get("liquidity",0)}
                    await execute_buy(s.bot,s.risk,s.journal,"mirror_arb",m,other_outcome,other_tid,other_price,sig,s.dry_run,s.log)

    async def _manage(s):
        for p in [p for p in s.risk.get_all_positions() if p.strategy=="mirror_arb"]:
            try:
                pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                if pr is None: continue
                s.journal.update_position_extremes(p.id,pr); ch=pr-p.entry_price
                if ch>=0.05: await execute_sell(s.bot,s.risk,s.journal,p,pr,"take_profit",s.dry_run,s.log)
                elif ch<=-0.08: await execute_sell(s.bot,s.risk,s.journal,p,pr,"stop_loss",s.dry_run,s.log)
                elif (time.time()-p.entry_time)>43200: await execute_sell(s.bot,s.risk,s.journal,p,pr,"time_exit_12h",s.dry_run,s.log)
            except Exception as e: s.log.debug(f"Error: {e}")


# =============================================================================
# MIRROR STRATEGY 4: MirrorFlashCrash
#
# Original: detects crash (live price << gamma price) on a side, buys that side
# Mirror:   detects the same crash, buys the OTHER side
# Same trigger, same time, opposite side.
# =============================================================================

class MirrorFlashCrash:
    def __init__(s, bot, risk, journal, dry_run=False):
        s.bot,s.risk,s.journal,s.dry_run = bot,risk,journal,dry_run
        s.log=logging.getLogger("mirror_flash"); s.coins=["BTC","ETH"]; s.gamma=GammaClient(); s.search=MarketSearch()

    async def run(s):
        s.log.info(f"Mirror Flash Crash started for {s.coins} (same crash signal, opposite side)")
        while True:
            try:
                if not s.risk.is_halted:
                    for coin in s.coins:
                        try:
                            info=await asyncio.to_thread(s.gamma.get_market_info,coin)
                            if not info or not info.get("accepting_orders"): continue
                            tids,prices=info.get("token_ids",{}),info.get("prices",{})
                            for side in ["up","down"]:
                                tid=tids.get(side)
                                if not tid: continue
                                gp=prices.get(side,0.5)
                                try: lp=await asyncio.to_thread(s.search.get_market_price,tid)
                                except Exception as e: s.log.debug(f"Skipped: {e}"); continue
                                if lp is None: continue
                                # SAME signal as original: detect crash
                                drop=gp-lp
                                if drop>=0.20 and lp>=0.05:
                                    # MIRROR: buy the OTHER side
                                    other_side = "down" if side == "up" else "up"
                                    other_tid = tids.get(other_side)
                                    if not other_tid: continue
                                    try: other_lp = await asyncio.to_thread(s.search.get_market_price, other_tid)
                                    except: continue
                                    if other_lp is None or other_lp <= 0: continue
                                    sig={"triggered_side":side,"gamma_price":gp,"crash_price":lp,"drop":drop,"mirror_side":other_side,"mirror_price":other_lp,"coin":coin}
                                    m={"condition_id":f"15m-{coin}-{int(time.time())}","question":f"{coin} 15-min {other_side}","liquidity":10000,"volume_24h":0}
                                    await execute_buy(s.bot,s.risk,s.journal,"mirror_flash",m,other_side,other_tid,other_lp,sig,s.dry_run,s.log)
                        except Exception as e: s.log.debug(f"Error: {e}")
                await asyncio.sleep(30)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)


class StatusReporter:
    def __init__(s, risk, search, journal):
        s.risk,s.search,s.journal = risk,search,journal; s.log=logging.getLogger("status")

    async def run(s):
        while True:
            try:
                await asyncio.sleep(300); st=s.risk.get_status()
                s.log.info(f"STATUS [MIRROR] | Pos:{st['positions']}/{st['max_positions']} Exp:${st['total_exposure']:.0f}/${st['max_exposure']:.0f} PnL:${st['daily_pnl']:+.2f} Trades:{st['daily_trades']} Halted:{st['halted']}")
                for p in s.risk.get_all_positions():
                    try:
                        pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                        if pr: s.journal.update_position_extremes(p.id,pr); s.log.info(f"  [{p.strategy[:8]}] {p.outcome.upper()} {p.entry_price:.3f}->{pr:.3f} ${p.unrealized_pnl(pr):+.2f} ({(time.time()-p.entry_time)/60:.0f}m) {p.market_question[:35]}")
                    except Exception as e: s.log.debug(f"Error: {e}")
            except asyncio.CancelledError: break
            except Exception as e: s.log.debug(f"Error: {e}")


async def run_daemon(args):
    log=setup_logging(debug=args.debug)
    pk,sa = os.environ.get("POLY_PRIVATE_KEY"), os.environ.get("POLY_SAFE_ADDRESS")
    if not pk or not sa: log.error("Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env"); sys.exit(1)
    config=Config.from_env(); bot=TradingBot(config=config,private_key=pk)
    if not bot.is_initialized(): log.error("Bot init failed"); sys.exit(1)
    rc=RiskConfig(min_trade_size=5.0,max_trade_size=args.max_trade,default_trade_size=args.default_trade,max_positions=args.max_positions,max_total_exposure=args.max_exposure,daily_loss_limit=args.daily_loss,daily_trade_limit=args.daily_trades)
    risk=RiskManager(config=rc,state_file="risk_state_contrarian.json"); journal=TradeJournal(db_path="data/trades_contrarian.db"); search=MarketSearch()
    en=set(args.strategies.split(",")) if args.strategies else {"value","swing","arb","flash"}
    log.info("="*60); log.info("MIRROR TRADING DAEMON STARTING")
    log.info("Same signals as auto_trader.py, OPPOSITE side on every trade")
    log.info(f"  Strategies: {', '.join(en)} | Trade: ${rc.default_trade_size} | Max exp: ${rc.max_total_exposure} | Dry: {args.dry_run}")
    log.info("="*60)
    tasks=[]
    if "value" in en: tasks.append(asyncio.create_task(MirrorValueScanner(bot,risk,search,journal,args.dry_run).run()))
    if "swing" in en: tasks.append(asyncio.create_task(MirrorSwingTrader(bot,risk,search,journal,args.dry_run).run()))
    if "arb" in en: tasks.append(asyncio.create_task(MirrorArbitrage(bot,risk,search,journal,args.dry_run).run()))
    if "flash" in en: tasks.append(asyncio.create_task(MirrorFlashCrash(bot,risk,journal,args.dry_run).run()))
    tasks.append(asyncio.create_task(StatusReporter(risk,search,journal).run()))
    shutdown=asyncio.Event()
    def sh(sig,frame): log.info("Shutdown..."); shutdown.set()
    signal.signal(signal.SIGINT,sh); signal.signal(signal.SIGTERM,sh)
    log.info(f"Running {len(tasks)} mirror tasks. Ctrl+C to stop.")
    try: await shutdown.wait()
    finally:
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        st=risk.get_status(); log.info(f"SHUTDOWN | Pos:{st['positions']} PnL:${st['daily_pnl']:+.2f} Trades:{st['daily_trades']}")


def main():
    p=argparse.ArgumentParser(description="Mirror Trading Daemon (same signals as auto_trader, opposite side)")
    p.add_argument("--strategies",type=str,default=None,help="value,swing,arb,flash")
    p.add_argument("--max-trade",type=float,default=25.0)
    p.add_argument("--default-trade",type=float,default=10.0)
    p.add_argument("--max-positions",type=int,default=10)
    p.add_argument("--max-exposure",type=float,default=200.0)
    p.add_argument("--daily-loss",type=float,default=50.0)
    p.add_argument("--daily-trades",type=int,default=50)
    p.add_argument("--dry-run",action="store_true",help="Log but don't execute")
    p.add_argument("--debug",action="store_true")
    asyncio.run(run_daemon(p.parse_args()))


if __name__=="__main__": main()
