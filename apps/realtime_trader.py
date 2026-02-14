#!/usr/bin/env python3
"""
Real-Time Volatility Trader v2 - Millisecond Execution

Streams live prices from Binance + Polymarket WebSockets.
Executes instantly when edge threshold hit.
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(name)s - %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("realtime")

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws"


@dataclass 
class Market:
    coin: str
    up_token: str = ""
    down_token: str = ""
    end_time: int = 0
    
    up_bid: float = 0.0
    up_ask: float = 1.0
    down_bid: float = 0.0
    down_ask: float = 1.0
    
    ref_price: float = 0.0
    start_price: float = 0.0
    
    @property
    def up_mid(self) -> float:
        return (self.up_bid + self.up_ask) / 2 if self.up_bid > 0 else 0.5
    
    @property
    def down_mid(self) -> float:
        return (self.down_bid + self.down_ask) / 2 if self.down_bid > 0 else 0.5
    
    @property 
    def secs_left(self) -> int:
        return max(0, self.end_time - int(time.time()))
    
    def fair_value(self) -> tuple:
        """Returns (up_fair, down_fair) based on price movement."""
        if self.ref_price <= 0 or self.start_price <= 0:
            return 0.5, 0.5
        
        pct = (self.ref_price - self.start_price) / self.start_price
        up_prob = 0.5 + (pct * 40)  # 1% move = 40% shift
        up_prob = max(0.1, min(0.9, up_prob))
        return up_prob, 1 - up_prob
    
    def get_edge(self) -> tuple:
        """Returns (side, edge, fair, price)."""
        up_fair, down_fair = self.fair_value()
        
        up_edge = up_fair - self.up_ask if self.up_ask < 0.95 else -1
        down_edge = down_fair - self.down_ask if self.down_ask < 0.95 else -1
        
        if up_edge > down_edge and up_edge > 0:
            return "up", up_edge, up_fair, self.up_ask
        elif down_edge > 0:
            return "down", down_edge, down_fair, self.down_ask
        return None, 0, 0, 0


class Trader:
    def __init__(self, coins=["BTC", "ETH"]):
        self.coins = coins
        self.markets: Dict[str, Market] = {c: Market(coin=c) for c in coins}
        
        self.edge_threshold = 0.04  # 4% edge
        self.trade_size = 5.0
        
        self.positions = {}
        self.cooldowns = {}
        
        self.ticks = 0
        self.trades = 0
        
        self.bot = None
        
    async def init_bot(self):
        from src.bot import TradingBot
        self.bot = TradingBot()
        log.info("Bot ready")
    
    async def discover_markets(self):
        """Find current 15-min markets."""
        from src.gamma_client import GammaClient
        gamma = GammaClient()
        
        for coin in self.coins:
            try:
                market = await asyncio.to_thread(gamma.get_current_15m_market, coin)
                
                if market:
                    tokens = gamma.parse_token_ids(market)
                    
                    end_str = market.get("endDate", "")
                    if end_str:
                        from datetime import datetime
                        try:
                            end_time = int(datetime.fromisoformat(end_str.replace("Z","")).timestamp())
                        except:
                            end_time = int(time.time()) + 900
                    else:
                        end_time = int(time.time()) + 900
                    
                    self.markets[coin].up_token = tokens.get("up", "")
                    self.markets[coin].down_token = tokens.get("down", "")
                    self.markets[coin].end_time = end_time
                    
                    title = market.get("title", "")[:40]
                    log.info(f"✓ {coin}: {title}... ({self.markets[coin].secs_left}s left)")
                else:
                    log.warning(f"No 15m market found for {coin}")
                        
            except Exception as e:
                log.error(f"Discovery error {coin}: {e}")
    
    async def binance_stream(self):
        """Stream Binance prices."""
        streams = "/".join(f"{c.lower()}usdt@trade" for c in self.coins)
        url = f"{BINANCE_WS}/{streams}"
        
        while True:
            try:
                async with ws_connect(url) as ws:
                    log.info("Binance WS connected")
                    async for msg in ws:
                        d = json.loads(msg)
                        sym = d.get("s", "").replace("USDT", "")
                        px = float(d.get("p", 0))
                        
                        if sym in self.markets and px > 0:
                            m = self.markets[sym]
                            m.ref_price = px
                            if m.start_price == 0:
                                m.start_price = px
                            await self.check(m)
                            
            except Exception as e:
                log.error(f"Binance err: {e}")
                await asyncio.sleep(1)
    
    async def poly_stream(self):
        """Stream Polymarket orderbooks."""
        await asyncio.sleep(1)  # Wait for market discovery
        
        while True:
            tokens = []
            for m in self.markets.values():
                if m.up_token:
                    tokens.extend([m.up_token, m.down_token])
            
            if not tokens:
                await asyncio.sleep(5)
                continue
            
            try:
                async with ws_connect(POLY_WS) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": "book", 
                        "assets_ids": tokens
                    }))
                    log.info(f"Polymarket WS: {len(tokens)} tokens")
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        
                        # Handle both single message and array of messages
                        messages = data if isinstance(data, list) else [data]
                        
                        for d in messages:
                            if not isinstance(d, dict):
                                continue
                            etype = d.get("event_type", "")
                        
                        if etype == "book":
                            aid = d.get("asset_id", "")
                            bids = d.get("bids", [])
                            asks = d.get("asks", [])
                            
                            # Parse bid/ask - could be {"price":x,"size":y} or [price, size]
                            def get_price(levels):
                                if not levels:
                                    return None
                                lvl = levels[0]
                                if isinstance(lvl, dict):
                                    return float(lvl.get("price", 0))
                                elif isinstance(lvl, list):
                                    return float(lvl[0])
                                return float(lvl)
                            
                            bid_px = get_price(bids)
                            ask_px = get_price(asks)
                            
                            for m in self.markets.values():
                                if aid == m.up_token:
                                    if bid_px is not None: m.up_bid = bid_px
                                    if ask_px is not None: m.up_ask = ask_px
                                    await self.check(m)
                                elif aid == m.down_token:
                                    if bid_px is not None: m.down_bid = bid_px
                                    if ask_px is not None: m.down_ask = ask_px
                                    await self.check(m)
                        
                        elif etype == "price_change":
                            aid = d.get("asset_id", "")
                            bid = float(d.get("best_bid", 0))
                            ask = float(d.get("best_ask", 1))
                            
                            for m in self.markets.values():
                                if aid == m.up_token:
                                    m.up_bid = bid
                                    m.up_ask = ask
                                    await self.check(m)
                                elif aid == m.down_token:
                                    m.down_bid = bid
                                    m.down_ask = ask
                                    await self.check(m)
                                
            except Exception as e:
                log.error(f"Poly err: {e}")
                await asyncio.sleep(1)
    
    async def check(self, m: Market):
        """Check edge on every tick."""
        self.ticks += 1
        
        if m.secs_left < 30:
            return
        if m.coin in self.positions:
            return
        if time.time() - self.cooldowns.get(m.coin, 0) < 30:
            return
        
        side, edge, fair, price = m.get_edge()
        
        if side and edge >= self.edge_threshold:
            token = m.up_token if side == "up" else m.down_token
            size = self.trade_size / price  # Convert $ to shares
            
            log.info(
                f"🎯 {m.coin} {side.upper()} | Edge: {edge:.1%} | "
                f"Fair: {fair:.2f} vs Ask: {price:.2f} | "
                f"Ref: ${m.ref_price:.2f}"
            )
            
            if self.bot:
                try:
                    result = await self.bot.place_order(
                        token_id=token,
                        price=price + 0.02,  # Aggressive
                        size=size,
                        side="BUY"
                    )
                    
                    if result and result.success:
                        self.trades += 1
                        self.cooldowns[m.coin] = time.time()
                        self.positions[m.coin] = {"side": side, "entry": price}
                        log.info(f"✅ FILLED {m.coin} {side.upper()} ${self.trade_size} @ {price:.2f}")
                    else:
                        log.warning(f"Order failed: {result}")
                        
                except Exception as e:
                    log.error(f"Trade error: {e}")
    
    async def status_loop(self):
        """Status updates."""
        while True:
            await asyncio.sleep(5)
            
            log.info(f"📊 Ticks: {self.ticks} | Trades: {self.trades}")
            for c, m in self.markets.items():
                if m.up_token:
                    side, edge, fair, _ = m.get_edge()
                    log.info(
                        f"  {c}: {m.secs_left}s | "
                        f"UP {m.up_bid:.2f}/{m.up_ask:.2f} | "
                        f"DN {m.down_bid:.2f}/{m.down_ask:.2f} | "
                        f"Ref ${m.ref_price:.2f} | Edge {edge:.1%}"
                    )
    
    async def market_refresh(self):
        """Refresh markets every 10 mins."""
        while True:
            await asyncio.sleep(600)
            log.info("Refreshing markets...")
            await self.discover_markets()
    
    async def run(self):
        log.info("Starting Real-Time Trader")
        await self.init_bot()
        await self.discover_markets()
        
        await asyncio.gather(
            self.binance_stream(),
            self.poly_stream(),
            self.status_loop(),
            self.market_refresh(),
        )


if __name__ == "__main__":
    asyncio.run(Trader(["BTC", "ETH"]).run())
