#!/usr/bin/env python3
"""
Contrarian Real-Time Trader - The Exact Opposite of realtime_trader.py

The original realtime_trader.py:
  - Watches Binance price moves
  - If BTC goes UP on Binance -> buys UP on Polymarket
  - Assumes Binance direction predicts Polymarket outcome

This contrarian version:
  - Watches the SAME Binance price moves
  - If BTC goes UP on Binance -> buys DOWN on Polymarket
  - Assumes the crowd overreacts to Binance moves (mean reversion)

If the original bot lost consistently by following Binance direction,
this one should win by fading it.

Usage:
    python apps/contrarian_realtime.py                  # Live trading BTC+ETH
    python apps/contrarian_realtime.py --dry-run        # Watch without executing
    python apps/contrarian_realtime.py --coins BTC      # BTC only
    python apps/contrarian_realtime.py --edge 0.03      # Lower edge threshold
    python apps/contrarian_realtime.py --size 15        # $15 per trade
"""

import asyncio
import json
import logging
import math
import os
import sys
import time
import signal
import argparse
from dataclasses import dataclass
from typing import Dict, Optional, List
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect

from dotenv import load_dotenv
load_dotenv()

from src.bot import TradingBot
from src.config import Config
from src.gamma_client import GammaClient
from lib.risk_manager import RiskManager, RiskConfig
from lib.trade_journal import TradeJournal

Path("logs").mkdir(exist_ok=True)
fh = logging.FileHandler("logs/contrarian_realtime.log")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
logging.basicConfig(level=logging.DEBUG, handlers=[fh, ch])
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("contrarian_realtime")

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws"


@dataclass
class LiveMarket:
    """Tracks real-time state of a single 15-min up/down market pair."""
    coin: str
    slug: str = ""
    condition_id: str = ""
    up_token: str = ""
    down_token: str = ""
    end_time: int = 0
    up_bid: float = 0.0
    up_ask: float = 1.0
    down_bid: float = 0.0
    down_ask: float = 1.0
    ref_price: float = 0.0
    start_price: float = 0.0
    binance_ticks: int = 0
    poly_ticks: int = 0

    @property
    def up_mid(self) -> float:
        return (self.up_bid + self.up_ask) / 2 if self.up_bid > 0 and self.up_ask < 1 else 0.5

    @property
    def down_mid(self) -> float:
        return (self.down_bid + self.down_ask) / 2 if self.down_bid > 0 and self.down_ask < 1 else 0.5

    @property
    def secs_left(self) -> int:
        return max(0, self.end_time - int(time.time()))

    @property
    def has_data(self) -> bool:
        return self.up_token != "" and self.ref_price > 0 and self.start_price > 0 and self.up_bid > 0 and self.binance_ticks >= 5

    def fair_value(self) -> tuple:
        """
        CONTRARIAN fair value: INVERTED from original.

        Original: Binance UP -> UP probability increases
        Contrarian: Binance UP -> DOWN probability increases (fade the move)

        The logic: if Binance price went up, the crowd has already pushed
        UP too high and DOWN too low. We bet on the reversion.
        """
        if self.ref_price <= 0 or self.start_price <= 0:
            return 0.5, 0.5
        pct_move = (self.ref_price - self.start_price) / self.start_price
        k = 300  # Same sensitivity as original
        # INVERTED: negative sign flips the direction
        # If Binance goes up (pct_move > 0), up_prob goes DOWN
        up_prob = 1 / (1 + math.exp(k * pct_move))  # Note: +k instead of -k
        up_prob = max(0.08, min(0.92, up_prob))
        return up_prob, 1 - up_prob

    def get_edge(self) -> tuple:
        """Returns (side, edge, fair_price, market_ask, token_id) or (None,0,0,0,"")."""
        if not self.has_data:
            return None, 0, 0, 0, ""
        up_fair, down_fair = self.fair_value()
        up_edge = up_fair - self.up_ask if self.up_ask < 0.95 else -1
        down_edge = down_fair - self.down_ask if self.down_ask < 0.95 else -1
        if up_edge > down_edge and up_edge > 0:
            return "up", up_edge, up_fair, self.up_ask, self.up_token
        elif down_edge > 0:
            return "down", down_edge, down_fair, self.down_ask, self.down_token
        return None, 0, 0, 0, ""


@dataclass
class LivePosition:
    coin: str
    side: str
    token_id: str
    entry_price: float
    entry_fair: float
    entry_edge: float
    entry_time: float
    size_shares: float
    size_usdc: float
    order_id: str = ""
    risk_pos_id: str = ""
    peak_price: float = 0.0

    @property
    def hold_secs(self) -> float:
        return time.time() - self.entry_time


class ContrarianRealTimeTrader:
    """
    Contrarian dual WebSocket trader for 15-minute crypto markets.

    Same architecture as RealTimeTrader but with INVERTED fair value:
    - Original: Binance UP -> buy UP on Polymarket
    - Contrarian: Binance UP -> buy DOWN on Polymarket (fade the crowd)
    """

    def __init__(self, coins=None, edge_threshold=0.04, trade_size=10.0,
                 dry_run=False, tp_cents=0.08, sl_cents=0.05):
        self.coins = coins or ["BTC", "ETH"]
        self.edge_threshold = edge_threshold
        self.trade_size = trade_size
        self.dry_run = dry_run
        self.tp = tp_cents
        self.sl = sl_cents

        self.markets: Dict[str, LiveMarket] = {c: LiveMarket(coin=c) for c in self.coins}
        self.positions: Dict[str, LivePosition] = {}
        self.cooldowns: Dict[str, float] = {}

        self.total_ticks = 0
        self.edge_checks = 0
        self.trades_executed = 0
        self.start_time = time.time()

        self.bot: Optional[TradingBot] = None
        self.risk: Optional[RiskManager] = None
        self.journal: Optional[TradeJournal] = None
        self.gamma = GammaClient()

    async def init(self):
        config = Config.from_env()
        pk = os.environ.get("POLY_PRIVATE_KEY")
        if not pk:
            log.error("POLY_PRIVATE_KEY not set")
            sys.exit(1)
        self.bot = TradingBot(config=config, private_key=pk)
        if not self.bot.is_initialized():
            log.error("Bot failed to initialize")
            sys.exit(1)
        self.risk = RiskManager(
            config=RiskConfig(min_trade_size=5.0, max_trade_size=50.0,
                default_trade_size=self.trade_size, max_positions=4,
                max_total_exposure=100.0, daily_loss_limit=30.0,
                trade_cooldown=20.0, global_cooldown=2.0),
            state_file="risk_state_contrarian_rt.json")
        self.journal = TradeJournal(db_path="data/trades_contrarian.db")
        log.info(f"Contrarian bot initialized | Dry run: {self.dry_run}")

    async def discover_markets(self):
        for coin in self.coins:
            try:
                info = await asyncio.to_thread(self.gamma.get_market_info, coin)
                if not info or not info.get("accepting_orders"):
                    log.warning(f"No active 15m market for {coin}")
                    continue
                m = self.markets[coin]
                tokens = info.get("token_ids", {})
                m.up_token = tokens.get("up", "")
                m.down_token = tokens.get("down", "")
                m.condition_id = info.get("raw", {}).get("conditionId", f"15m-{coin}")
                m.slug = info.get("slug", "")
                end_str = info.get("end_date", "")
                if end_str:
                    from datetime import datetime
                    try:
                        m.end_time = int(datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        m.end_time = int(time.time()) + 900
                else:
                    m.end_time = int(time.time()) + 900
                m.start_price = 0.0
                m.ref_price = 0.0
                m.binance_ticks = 0
                m.poly_ticks = 0
                log.info(f"[CONTRARIAN] {coin}: {m.secs_left}s left | UP:{m.up_token[:12]}.. DN:{m.down_token[:12]}..")
            except Exception as e:
                log.error(f"Discovery error {coin}: {e}")

    # ========================================================================
    # WebSocket Streams (identical to original — data collection is the same)
    # ========================================================================

    async def binance_stream(self):
        streams = "/".join(f"{c.lower()}usdt@trade" for c in self.coins)
        url = f"{BINANCE_WS}/{streams}"
        while True:
            try:
                async with ws_connect(url, ping_interval=20) as ws:
                    log.info(f"Binance WS connected ({', '.join(self.coins)})")
                    async for msg in ws:
                        data = json.loads(msg)
                        sym = data.get("s", "").replace("USDT", "")
                        px = float(data.get("p", 0))
                        if sym in self.markets and px > 0:
                            m = self.markets[sym]
                            m.ref_price = px
                            m.binance_ticks += 1
                            if m.start_price == 0:
                                m.start_price = px
                                log.info(f"{sym} start price: ${px:,.2f}")
                            self.total_ticks += 1
                            await self._check_edge(m)
                            await self._check_exit(m)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Binance WS error: {e}")
                await asyncio.sleep(2)

    async def poly_stream(self):
        await asyncio.sleep(2)
        while True:
            tokens = []
            for m in self.markets.values():
                if m.up_token:
                    tokens.extend([m.up_token, m.down_token])
            if not tokens:
                await asyncio.sleep(5)
                continue
            try:
                async with ws_connect(POLY_WS, ping_interval=20) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "channel": "book", "assets_ids": tokens}))
                    log.info(f"Polymarket WS connected ({len(tokens)} tokens)")
                    async for msg in ws:
                        data = json.loads(msg)
                        messages = data if isinstance(data, list) else [data]
                        for d in messages:
                            if not isinstance(d, dict):
                                continue
                            etype = d.get("event_type", "")
                            if etype == "book":
                                self._on_book(d)
                            elif etype == "price_change":
                                self._on_price_change(d)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Polymarket WS error: {e}")
                await asyncio.sleep(2)

    def _best_price(self, levels):
        if not levels:
            return None
        lvl = levels[0]
        if isinstance(lvl, dict):
            return float(lvl.get("price", 0))
        elif isinstance(lvl, (list, tuple)):
            return float(lvl[0])
        return float(lvl)

    def _on_book(self, d: dict):
        aid = d.get("asset_id", "")
        bid_px = self._best_price(d.get("bids", []))
        ask_px = self._best_price(d.get("asks", []))
        for m in self.markets.values():
            if aid == m.up_token:
                if bid_px is not None:
                    m.up_bid = bid_px
                if ask_px is not None:
                    m.up_ask = ask_px
                m.poly_ticks += 1
                self.total_ticks += 1
            elif aid == m.down_token:
                if bid_px is not None:
                    m.down_bid = bid_px
                if ask_px is not None:
                    m.down_ask = ask_px
                m.poly_ticks += 1
                self.total_ticks += 1

    def _on_price_change(self, d: dict):
        aid = d.get("asset_id", "")
        bid, ask = float(d.get("best_bid", 0)), float(d.get("best_ask", 1))
        for m in self.markets.values():
            if aid == m.up_token:
                if bid > 0:
                    m.up_bid = bid
                if ask < 1:
                    m.up_ask = ask
            elif aid == m.down_token:
                if bid > 0:
                    m.down_bid = bid
                if ask < 1:
                    m.down_ask = ask

    # ========================================================================
    # Edge Detection & Execution (uses INVERTED fair_value from LiveMarket)
    # ========================================================================

    async def _check_edge(self, m: LiveMarket):
        self.edge_checks += 1
        if m.secs_left < 30 or m.coin in self.positions:
            return
        if time.time() - self.cooldowns.get(m.coin, 0) < 20:
            return
        if self.risk and self.risk.is_halted:
            return

        side, edge, fair, price, token_id = m.get_edge()
        if side is None or edge < self.edge_threshold:
            return

        size_usdc = self.trade_size
        size_shares = size_usdc / price
        pct_move = (m.ref_price - m.start_price) / m.start_price * 100 if m.start_price else 0

        signals = {
            "coin": m.coin, "side": side, "edge": round(edge, 4),
            "fair": round(fair, 4), "market_ask": round(price, 4),
            "ref_price": round(m.ref_price, 2), "start_price": round(m.start_price, 2),
            "pct_move": round(pct_move, 4),
            "up_book": f"{m.up_bid:.3f}/{m.up_ask:.3f}",
            "dn_book": f"{m.down_bid:.3f}/{m.down_ask:.3f}",
            "secs_left": m.secs_left, "ticks": m.binance_ticks,
            "contrarian": True,
        }

        if self.risk:
            allowed, reason = self.risk.check_trade(
                strategy="contrarian_rt", condition_id=m.condition_id,
                token_id=token_id, price=price, size_usdc=size_usdc)
            if not allowed:
                if self.journal:
                    self.journal.log_decision(strategy="contrarian_rt", action="BUY", result="rejected",
                        market_question=f"{m.coin} 15m {side} (contrarian)", condition_id=m.condition_id,
                        token_id=token_id, outcome=side, signals=signals, rejection_reason=reason)
                return

        log.info(f"[CONTRARIAN] {m.coin} {side.upper()} | Edge: {edge:.1%} | Fair: {fair:.3f} vs Ask: {price:.3f} | ${m.ref_price:,.2f} ({pct_move:+.3f}%) | {m.secs_left}s")

        did = None
        if self.journal:
            did = self.journal.log_decision(strategy="contrarian_rt", action="BUY",
                result="dry_run" if self.dry_run else "executing",
                market_question=f"{m.coin} 15m {side} (contrarian)", condition_id=m.condition_id,
                token_id=token_id, outcome=side, signals=signals)
            self.journal.log_snapshot(token_id=token_id,
                mid_price=(m.up_mid if side == "up" else m.down_mid),
                best_bid=(m.up_bid if side == "up" else m.down_bid),
                best_ask=(m.up_ask if side == "up" else m.down_ask),
                spread=((m.up_ask - m.up_bid) if side == "up" else (m.down_ask - m.down_bid)),
                decision_id=did)

        if self.dry_run:
            log.info(f"[CONTRARIAN DRY RUN] Would buy {m.coin} {side.upper()} @ {price:.3f}")
            self.cooldowns[m.coin] = time.time()
            return

        buy_price = min(price + 0.02, 0.95)
        result = await self.bot.place_order(token_id=token_id, price=buy_price, size=size_shares, side="BUY")

        if result and result.success:
            self.trades_executed += 1
            self.cooldowns[m.coin] = time.time()
            pos = LivePosition(coin=m.coin, side=side, token_id=token_id,
                entry_price=price, entry_fair=fair, entry_edge=edge,
                entry_time=time.time(), size_shares=size_shares,
                size_usdc=size_usdc, order_id=result.order_id, peak_price=price)
            if self.risk:
                pos.risk_pos_id = self.risk.register_trade(
                    strategy="contrarian_rt", market_question=f"{m.coin} 15m {side} (contrarian)",
                    condition_id=m.condition_id, token_id=token_id, outcome=side,
                    side="BUY", price=price, size_shares=size_shares,
                    size_usdc=size_usdc, order_id=result.order_id)
            if self.journal:
                self.journal.log_trade(strategy="contrarian_rt", side="BUY", price=price,
                    size_shares=size_shares, size_usdc=size_usdc,
                    market_question=f"{m.coin} 15m {side} (contrarian)", condition_id=m.condition_id,
                    token_id=token_id, outcome=side, order_id=result.order_id, decision_id=did)
                self.journal.open_position(
                    position_id=pos.risk_pos_id or f"crt_{int(time.time())}",
                    strategy="contrarian_rt", entry_price=price,
                    size_shares=size_shares, size_usdc=size_usdc,
                    market_question=f"{m.coin} 15m {side} (contrarian)", condition_id=m.condition_id,
                    token_id=token_id, outcome=side, entry_order_id=result.order_id,
                    entry_signals=signals)
            self.positions[m.coin] = pos
            log.info(f"[CONTRARIAN] FILLED {m.coin} {side.upper()} ${size_usdc:.2f} @ {price:.3f}")
        else:
            msg = result.message if result else "No result"
            log.warning(f"[CONTRARIAN] Order failed: {msg}")
            if self.journal:
                self.journal.log_decision(strategy="contrarian_rt", action="BUY", result="failed",
                    market_question=f"{m.coin} 15m {side} (contrarian)", condition_id=m.condition_id,
                    token_id=token_id, outcome=side, signals=signals, notes=msg)

    # ========================================================================
    # Position Exit Management (same TP/SL logic)
    # ========================================================================

    async def _check_exit(self, m: LiveMarket):
        if m.coin not in self.positions:
            return
        pos = self.positions[m.coin]
        current_bid = m.up_bid if pos.side == "up" else m.down_bid
        if current_bid <= 0:
            return

        pos.peak_price = max(pos.peak_price, current_bid)
        pnl = (current_bid - pos.entry_price) * pos.size_shares
        change = current_bid - pos.entry_price

        exit_reason = None
        if change >= self.tp:
            exit_reason = "take_profit"
        elif change <= -self.sl:
            exit_reason = "stop_loss"
        elif m.secs_left <= 60 and m.secs_left > 0:
            exit_reason = "market_closing"
        elif (pos.peak_price - pos.entry_price) >= 0.05 and (pos.peak_price - current_bid) >= 0.03:
            exit_reason = "trailing_stop"

        if not exit_reason:
            return

        log.info(f"[CONTRARIAN EXIT] {m.coin} {pos.side.upper()} | {pos.entry_price:.3f}->{current_bid:.3f} | ${pnl:+.2f} | {exit_reason} | {pos.hold_secs:.0f}s")

        if not self.dry_run:
            sell_price = max(current_bid - 0.01, 0.01)
            result = await self.bot.place_order(token_id=pos.token_id, price=sell_price, size=pos.size_shares, side="SELL")
            if not (result and result.success):
                log.warning("Sell failed, retrying aggressive...")
                await self.bot.place_order(token_id=pos.token_id, price=max(current_bid - 0.03, 0.01), size=pos.size_shares, side="SELL")

        if self.risk and pos.risk_pos_id:
            self.risk.close_position(pos.risk_pos_id, current_bid, pnl)

        if self.journal and pos.risk_pos_id:
            self.journal.close_position(pos.risk_pos_id, current_bid, pnl, exit_reason=exit_reason)
            self.journal.log_trade(strategy="contrarian_rt", side="SELL", price=current_bid,
                size_shares=pos.size_shares, size_usdc=pos.size_shares * current_bid,
                market_question=f"{m.coin} 15m {pos.side} (contrarian)", condition_id=m.condition_id,
                token_id=pos.token_id, outcome=pos.side)

        del self.positions[m.coin]

    # ========================================================================
    # Support Loops
    # ========================================================================

    async def status_loop(self):
        while True:
            try:
                await asyncio.sleep(10)
                elapsed = time.time() - self.start_time
                tps = self.total_ticks / elapsed if elapsed > 0 else 0
                for c, m in self.markets.items():
                    if not m.up_token:
                        continue
                    side, edge, fair, price, _ = m.get_edge()
                    edge_str = f"{edge:.1%}" if side else "none"
                    pct = (m.ref_price - m.start_price) / m.start_price * 100 if m.start_price > 0 else 0
                    pos_str = ""
                    if c in self.positions:
                        pos = self.positions[c]
                        bid = m.up_bid if pos.side == "up" else m.down_bid
                        pos_pnl = (bid - pos.entry_price) * pos.size_shares
                        pos_str = f" | POS: {pos.side.upper()} ${pos_pnl:+.2f}"
                    log.info(f"  [CONTRARIAN] {c}: {m.secs_left:>3d}s | UP {m.up_bid:.3f}/{m.up_ask:.3f} DN {m.down_bid:.3f}/{m.down_ask:.3f} | ${m.ref_price:,.2f} ({pct:+.3f}%) | Edge: {edge_str}{pos_str}")
                log.info(f"  Ticks: {self.total_ticks} ({tps:.1f}/s) | Checks: {self.edge_checks} | Trades: {self.trades_executed}")
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def market_refresh_loop(self):
        while True:
            try:
                needs_refresh = False
                for m in self.markets.values():
                    if m.up_token and m.secs_left <= 5:
                        needs_refresh = True
                        if m.coin in self.positions:
                            log.warning(f"Market expiring, force-closing {m.coin}")
                            pos = self.positions[m.coin]
                            bid = m.up_bid if pos.side == "up" else m.down_bid
                            pnl = (bid - pos.entry_price) * pos.size_shares
                            if self.risk and pos.risk_pos_id:
                                self.risk.close_position(pos.risk_pos_id, bid, pnl)
                            if self.journal and pos.risk_pos_id:
                                self.journal.close_position(pos.risk_pos_id, bid, pnl, exit_reason="market_expired")
                            del self.positions[m.coin]
                if needs_refresh:
                    log.info("Market expired, discovering new...")
                    await asyncio.sleep(10)
                    await self.discover_markets()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Refresh error: {e}")
                await asyncio.sleep(10)

    async def run(self):
        await self.init()
        await self.discover_markets()
        active = sum(1 for m in self.markets.values() if m.up_token)
        if active == 0:
            log.error("No active markets found")
            return

        log.info("=" * 60)
        log.info("CONTRARIAN REAL-TIME TRADER")
        log.info("Fading Binance direction — the OPPOSITE of realtime_trader.py")
        log.info(f"  Coins: {', '.join(self.coins)} | Edge: {self.edge_threshold:.1%} | Size: ${self.trade_size}")
        log.info(f"  TP: +{self.tp:.2f} / SL: -{self.sl:.2f} | Dry: {self.dry_run}")
        log.info("=" * 60)

        shutdown = asyncio.Event()
        def handle_sig(sig, frame):
            log.info("Shutdown...")
            shutdown.set()
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

        tasks = [
            asyncio.create_task(self.binance_stream()),
            asyncio.create_task(self.poly_stream()),
            asyncio.create_task(self.status_loop()),
            asyncio.create_task(self.market_refresh_loop()),
        ]

        try:
            await shutdown.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = time.time() - self.start_time
            log.info(f"SHUTDOWN | {elapsed/60:.1f}min | Ticks: {self.total_ticks} | Trades: {self.trades_executed} | Open: {len(self.positions)}")


def main():
    p = argparse.ArgumentParser(description="Contrarian Real-Time Polymarket Trader (fades Binance direction)")
    p.add_argument("--coins", type=str, default="BTC,ETH", help="Comma-separated (default: BTC,ETH)")
    p.add_argument("--edge", type=float, default=0.04, help="Min edge to trade (default: 0.04 = 4%%)")
    p.add_argument("--size", type=float, default=10.0, help="Trade size USDC (default: 10)")
    p.add_argument("--tp", type=float, default=0.08, help="Take profit cents (default: 0.08)")
    p.add_argument("--sl", type=float, default=0.05, help="Stop loss cents (default: 0.05)")
    p.add_argument("--dry-run", action="store_true", help="Log but don't execute")
    args = p.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",")]
    asyncio.run(ContrarianRealTimeTrader(
        coins=coins,
        edge_threshold=args.edge,
        trade_size=args.size,
        dry_run=args.dry_run,
        tp_cents=args.tp,
        sl_cents=args.sl
    ).run())


if __name__ == "__main__":
    main()
