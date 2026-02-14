#!/usr/bin/env python3
"""Autonomous Trading Daemon - see DEPLOY.md for usage.

Scans hundreds of markets across categories (politics, sports, entertainment,
science, etc.) for value, swing, arbitrage, and flash crash opportunities.

Run with --dry-run to log signals without executing:
    python apps/auto_trader.py --dry-run
    nohup python apps/auto_trader.py --dry-run >> logs/auto_trader.log 2>&1 &
"""

import os, sys, asyncio, argparse, logging, time, signal, atexit
from pathlib import Path
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.config import Config
from src.market_search import MarketSearch
from src.gamma_client import GammaClient
from lib.risk_manager import RiskManager, RiskConfig
from lib.trade_journal import TradeJournal

# Default categories to scan for non-crypto market opportunities
DEFAULT_CATEGORIES = [
    "politics", "sports", "entertainment", "science",
    "business", "crypto", "pop-culture", "world",
]

PID_FILE = "data/auto_trader.pid"


def setup_logging(log_file="logs/auto_trader.log", debug=False):
    Path("logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(log_file); fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))
    ch = logging.StreamHandler(); ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
    logging.basicConfig(level=logging.DEBUG, handlers=[fh, ch])
    for n in ["src.websocket_client","src.bot","urllib3"]: logging.getLogger(n).setLevel(logging.WARNING)
    return logging.getLogger("auto_trader")


def write_pid():
    """Write PID file for daemon management."""
    Path(PID_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(PID_FILE).write_text(str(os.getpid()))

def remove_pid():
    """Remove PID file on exit."""
    try:
        Path(PID_FILE).unlink(missing_ok=True)
    except Exception:
        pass


class SeenTracker:
    """Tracks seen market condition IDs with automatic expiry.

    Markets are reconsidered after `ttl` seconds so the daemon
    can pick up changed conditions on long-running sessions.
    """

    def __init__(self, ttl: float = 3600.0):
        self.ttl = ttl
        self._entries: Dict[str, float] = {}

    def add(self, key: str):
        self._entries[key] = time.time()

    def __contains__(self, key: str) -> bool:
        ts = self._entries.get(key)
        if ts is None:
            return False
        if (time.time() - ts) > self.ttl:
            del self._entries[key]
            return False
        return True

    def __len__(self) -> int:
        self._prune()
        return len(self._entries)

    def _prune(self):
        now = time.time()
        expired = [k for k, t in self._entries.items() if (now - t) > self.ttl]
        for k in expired:
            del self._entries[k]


async def execute_buy(bot, risk, journal, strategy, market, outcome, token_id, price, signals, dry_run, log):
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


class ValueScanner:
    """Scans markets across categories for underpriced outcomes with good liquidity.

    Rotates through categories (politics, sports, entertainment, etc.) on each
    scan cycle. Paginates results to cover hundreds of markets per cycle.
    """

    def __init__(s, bot, risk, search, journal, dry_run=False, categories=None):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.categories = categories or DEFAULT_CATEGORIES
        s.log=logging.getLogger("value_scanner"); s.scan_interval=300; s.tp=0.15; s.sl=0.10
        s._seen=SeenTracker(ttl=3600)
        s._cat_idx = 0
        s._total_scanned = 0

    async def run(s):
        s.log.info(f"Value Scanner started - categories: {s.categories}")
        while True:
            try:
                if not s.risk.is_halted: await s._scan(); await s._manage()
                await asyncio.sleep(s.scan_interval)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)

    async def _scan(s):
        """Scan markets across categories with pagination."""
        all_markets = []

        # Fetch generic active markets (paginated)
        for offset in range(0, 150, 50):
            try:
                batch = await asyncio.to_thread(s.search.find_markets, "", active_only=True, limit=50, offset=offset)
                if not batch:
                    break
                all_markets.extend(batch)
            except Exception:
                break

        # Rotate through 2 categories per cycle to spread API load
        for _ in range(min(2, len(s.categories))):
            cat = s.categories[s._cat_idx % len(s.categories)]
            s._cat_idx += 1
            try:
                cat_markets = await asyncio.to_thread(s.search.find_markets_by_tag, cat, limit=50)
                all_markets.extend(cat_markets)
            except Exception:
                continue

        # Deduplicate by condition_id
        seen_cids: Set[str] = set()
        unique_markets = []
        for m in all_markets:
            cid = m.get("condition_id", "")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                unique_markets.append(m)

        s._total_scanned += len(unique_markets)
        s.log.info(f"Scanning {len(unique_markets)} markets (total lifetime: {s._total_scanned}, seen cache: {len(s._seen)})")

        for m in unique_markets:
            cid=m.get("condition_id","")
            if not m.get("accepting_orders") or cid in s._seen or m.get("liquidity",0)<s.risk.config.min_liquidity: continue
            for outcome,price in m.get("prices",{}).items():
                if not(0.10<=price<=0.35 and m.get("liquidity",0)>5000): continue
                tid=m.get("token_ids",{}).get(outcome)
                if not tid: continue
                try:
                    book=await asyncio.to_thread(s.search.get_orderbook,tid)
                    if not book or not book.get("bids") or not book.get("asks"): continue
                    bb,ba=float(book["bids"][0]["price"]),float(book["asks"][0]["price"])
                    spread=ba-bb; depth=sum(float(b["size"]) for b in book["bids"][:5])
                    if spread>0.10 or depth<50: continue
                    mid=(bb+ba)/2
                    sig={"price":mid,"spread":spread,"bid_depth":depth,"best_bid":bb,"best_ask":ba,"liquidity":m.get("liquidity",0),"volume_24h":m.get("volume_24h",0),"score":depth/spread if spread>0 else 0,"category":m.get("slug","")[:30]}
                    if await execute_buy(s.bot,s.risk,s.journal,"value_scanner",m,outcome,tid,mid,sig,s.dry_run,s.log): s._seen.add(cid)
                except Exception as e: s.log.debug(f"Skipped: {e}"); continue

    async def _manage(s):
        for p in [p for p in s.risk.get_all_positions() if p.strategy=="value_scanner"]:
            try:
                pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                if pr is None: continue
                s.journal.update_position_extremes(p.id,pr); ch=pr-p.entry_price
                if ch>=s.tp: await execute_sell(s.bot,s.risk,s.journal,p,pr,"take_profit",s.dry_run,s.log)
                elif ch<=-s.sl: await execute_sell(s.bot,s.risk,s.journal,p,pr,"stop_loss",s.dry_run,s.log)
                elif (time.time()-p.entry_time)>86400: await execute_sell(s.bot,s.risk,s.journal,p,pr,"time_exit_24h",s.dry_run,s.log)
            except Exception as e: s.log.error(f"Pos error: {e}")


class SwingTrader:
    """Monitors trending and category-specific markets for swing entries.

    Refreshes watchlist every 30 minutes from trending + category markets.
    """

    def __init__(s, bot, risk, search, journal, dry_run=False, categories=None):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.categories = categories or DEFAULT_CATEGORIES
        s.log=logging.getLogger("swing_trader"); s.interval=60; s.thresh=0.08; s.tp=0.10; s.sl=0.08
        s.history:Dict[str,List[tuple]]={}; s._wl:Dict[str,dict]={}; s._wl_t=0.0
        s._cat_idx = 0

    async def run(s):
        s.log.info(f"Swing Trader started - categories: {s.categories}")
        while True:
            try:
                if s.risk.is_halted: await asyncio.sleep(60); continue
                if not s._wl or (time.time()-s._wl_t)>1800:
                    await s._refresh_watchlist()
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
                        if old and (pr-old)<=-s.thresh and pr>=0.10:
                            sig={"swing":pr-old,"old_price":old,"current_price":pr,"lookback_min":30,"liquidity":info["market"].get("liquidity",0)}
                            await execute_buy(s.bot,s.risk,s.journal,"swing_trader",info["market"],info["outcome"],tid,pr,sig,s.dry_run,s.log)
                    except Exception as e: s.log.debug(f"Skipped: {e}"); continue
                for p in [p for p in s.risk.get_all_positions() if p.strategy=="swing_trader"]:
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

    async def _refresh_watchlist(s):
        """Build watchlist from trending + category markets."""
        s._wl = {}
        # Trending markets
        try:
            ms=await asyncio.to_thread(s.search.get_trending,20)
            for m in ms:
                if not m.get("accepting_orders") or m.get("liquidity",0)<5000: continue
                for o,tid in m.get("token_ids",{}).items(): s._wl[tid]={"market":m,"outcome":o,"condition_id":m["condition_id"]}
        except Exception as e: s.log.debug(f"Trending error: {e}")

        # Add from one category per refresh cycle
        cat = s.categories[s._cat_idx % len(s.categories)]
        s._cat_idx += 1
        try:
            cat_ms = await asyncio.to_thread(s.search.find_markets_by_tag, cat, limit=20)
            for m in cat_ms:
                if not m.get("accepting_orders") or m.get("liquidity",0)<5000: continue
                for o,tid in m.get("token_ids",{}).items():
                    if tid not in s._wl:
                        s._wl[tid]={"market":m,"outcome":o,"condition_id":m["condition_id"]}
        except Exception as e: s.log.debug(f"Category {cat} error: {e}")

        s.log.info(f"Watchlist: {len(s._wl)} tokens (trending + {cat})")
        s._wl_t=time.time()


class EventArbitrage:
    """Finds binary market mispricing across events with pagination."""

    def __init__(s, bot, risk, search, journal, dry_run=False):
        s.bot,s.risk,s.search,s.journal,s.dry_run = bot,risk,search,journal,dry_run
        s.log=logging.getLogger("arb_scanner"); s.interval=120; s.min_mis=0.05

    async def run(s):
        s.log.info("Event Arbitrage started")
        while True:
            try:
                if not s.risk.is_halted: await s._scan(); await s._manage()
                await asyncio.sleep(s.interval)
            except asyncio.CancelledError: break
            except Exception as e: s.log.error(f"Error: {e}"); await asyncio.sleep(60)

    async def _scan(s):
        # Fetch more events to cover broader market surface
        all_events = []
        for page_limit in [30, 30]:
            try:
                batch = await asyncio.to_thread(s.search.get_events, "", page_limit)
                all_events.extend(batch)
            except Exception:
                break

        scanned = 0
        for ev in all_events:
            ms=ev.get("markets",[])
            for m in ms:
                scanned += 1
                pr=m.get("prices",{})
                if len(pr)!=2: continue
                ps=sum(pr.values())
                if ps<(1.0-s.min_mis):
                    ch=min(pr,key=pr.get); cp=pr[ch]; tid=m.get("token_ids",{}).get(ch)
                    if not tid or cp<0.05 or cp>0.90: continue
                    fp=cp/ps; edge=fp-cp
                    if edge<0.03: continue
                    sig={"price_sum":ps,"fair_price":fp,"edge":edge,"cheapest_price":cp,"liquidity":m.get("liquidity",0)}
                    await execute_buy(s.bot,s.risk,s.journal,"arb_scanner",m,ch,tid,cp,sig,s.dry_run,s.log)
        s.log.debug(f"Arb scan: {scanned} markets across {len(all_events)} events")

    async def _manage(s):
        for p in [p for p in s.risk.get_all_positions() if p.strategy=="arb_scanner"]:
            try:
                pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                if pr is None: continue
                s.journal.update_position_extremes(p.id,pr); ch=pr-p.entry_price
                if ch>=0.05: await execute_sell(s.bot,s.risk,s.journal,p,pr,"take_profit",s.dry_run,s.log)
                elif ch<=-0.08: await execute_sell(s.bot,s.risk,s.journal,p,pr,"stop_loss",s.dry_run,s.log)
                elif (time.time()-p.entry_time)>43200: await execute_sell(s.bot,s.risk,s.journal,p,pr,"time_exit_12h",s.dry_run,s.log)
            except Exception as e: s.log.debug(f"Error: {e}")


class FlashCrashMonitor:
    def __init__(s, bot, risk, journal, dry_run=False):
        s.bot,s.risk,s.journal,s.dry_run = bot,risk,journal,dry_run
        s.log=logging.getLogger("flash_crash"); s.coins=["BTC","ETH"]; s.gamma=GammaClient(); s.search=MarketSearch()
    async def run(s):
        s.log.info(f"Flash Crash Monitor started for {s.coins}")
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
                                drop=gp-lp
                                if drop>=0.20 and lp>=0.05:
                                    sig={"gamma_price":gp,"live_price":lp,"drop":drop,"coin":coin,"side":side}
                                    m={"condition_id":f"15m-{coin}-{int(time.time())}","question":f"{coin} 15-min {side}","liquidity":10000,"volume_24h":0}
                                    await execute_buy(s.bot,s.risk,s.journal,"flash_crash",m,side,tid,lp,sig,s.dry_run,s.log)
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
                s.log.info(f"STATUS | Pos:{st['positions']}/{st['max_positions']} Exp:${st['total_exposure']:.0f}/${st['max_exposure']:.0f} PnL:${st['daily_pnl']:+.2f} Trades:{st['daily_trades']} Halted:{st['halted']}")
                for p in s.risk.get_all_positions():
                    try:
                        pr=await asyncio.to_thread(s.search.get_market_price,p.token_id)
                        if pr: s.journal.update_position_extremes(p.id,pr); s.log.info(f"  [{p.strategy[:8]}] {p.outcome.upper()} {p.entry_price:.3f}->{pr:.3f} ${p.unrealized_pnl(pr):+.2f} ({(time.time()-p.entry_time)/60:.0f}m) {p.market_question[:35]}")
                    except Exception as e: s.log.debug(f"Error: {e}")
            except asyncio.CancelledError: break
            except Exception as e: s.log.debug(f"Error: {e}")


async def run_daemon(args):
    log=setup_logging(debug=args.debug)

    # Ensure data directories exist
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    # Write PID file for daemon management
    write_pid()
    atexit.register(remove_pid)
    log.info(f"PID {os.getpid()} written to {PID_FILE}")

    pk,sa = os.environ.get("POLY_PRIVATE_KEY"), os.environ.get("POLY_SAFE_ADDRESS")
    if not pk or not sa: log.error("Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env"); sys.exit(1)
    config=Config.from_env(); bot=TradingBot(config=config,private_key=pk)
    if not bot.is_initialized(): log.error("Bot init failed"); sys.exit(1)
    rc=RiskConfig(min_trade_size=5.0,max_trade_size=args.max_trade,default_trade_size=args.default_trade,max_positions=args.max_positions,max_total_exposure=args.max_exposure,daily_loss_limit=args.daily_loss,daily_trade_limit=args.daily_trades)
    risk=RiskManager(config=rc,state_file="data/risk_state.json"); journal=TradeJournal(db_path="data/trades.db"); search=MarketSearch()

    # Parse categories
    categories = args.categories.split(",") if args.categories else DEFAULT_CATEGORIES

    en=set(args.strategies.split(",")) if args.strategies else {"value","swing","arb","flash"}
    log.info("="*60)
    log.info("AUTONOMOUS TRADING DAEMON STARTING")
    log.info(f"  Strategies: {', '.join(sorted(en))}")
    log.info(f"  Categories: {', '.join(categories)}")
    log.info(f"  Trade size: ${rc.default_trade_size} (max ${rc.max_trade_size})")
    log.info(f"  Max positions: {rc.max_positions} | Max exposure: ${rc.max_total_exposure}")
    log.info(f"  Daily loss limit: ${rc.daily_loss_limit} | Dry run: {args.dry_run}")
    log.info("="*60)

    tasks=[]
    if "value" in en: tasks.append(asyncio.create_task(ValueScanner(bot,risk,search,journal,args.dry_run,categories).run()))
    if "swing" in en: tasks.append(asyncio.create_task(SwingTrader(bot,risk,search,journal,args.dry_run,categories).run()))
    if "arb" in en: tasks.append(asyncio.create_task(EventArbitrage(bot,risk,search,journal,args.dry_run).run()))
    if "flash" in en: tasks.append(asyncio.create_task(FlashCrashMonitor(bot,risk,journal,args.dry_run).run()))
    tasks.append(asyncio.create_task(StatusReporter(risk,search,journal).run()))

    shutdown=asyncio.Event()
    def sh(sig,frame): log.info(f"Received signal {sig}, shutting down..."); shutdown.set()
    signal.signal(signal.SIGINT,sh); signal.signal(signal.SIGTERM,sh)
    log.info(f"Running {len(tasks)} tasks. Ctrl+C or SIGTERM to stop.")

    try: await shutdown.wait()
    finally:
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        st=risk.get_status(); log.info(f"SHUTDOWN | Pos:{st['positions']} PnL:${st['daily_pnl']:+.2f} Trades:{st['daily_trades']}")
        remove_pid()

def main():
    p=argparse.ArgumentParser(description="Autonomous Polymarket Trading Daemon")
    p.add_argument("--strategies",type=str,default=None,help="Comma-separated: value,swing,arb,flash (default: all)")
    p.add_argument("--categories",type=str,default=None,help=f"Comma-separated market categories (default: {','.join(DEFAULT_CATEGORIES)})")
    p.add_argument("--max-trade",type=float,default=25.0,help="Max USDC per trade (default: 25)")
    p.add_argument("--default-trade",type=float,default=10.0,help="Default USDC per trade (default: 10)")
    p.add_argument("--max-positions",type=int,default=10,help="Max concurrent positions (default: 10)")
    p.add_argument("--max-exposure",type=float,default=200.0,help="Max total USDC exposure (default: 200)")
    p.add_argument("--daily-loss",type=float,default=50.0,help="Daily loss limit circuit breaker (default: 50)")
    p.add_argument("--daily-trades",type=int,default=50,help="Max trades per day (default: 50)")
    p.add_argument("--dry-run",action="store_true",help="Log signals but don't execute trades")
    p.add_argument("--debug",action="store_true",help="Enable debug logging")
    asyncio.run(run_daemon(p.parse_args()))

if __name__=="__main__": main()
