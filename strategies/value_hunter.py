#!/usr/bin/env python3
"""
Value Hunter Strategy - Find Mispriced Markets

Looks for markets where:
1. Price is extreme (<15% or >85%) but has high volume/liquidity
2. Recent price movement suggests momentum
3. Spread is tight enough to trade profitably

Usage:
    python strategies/value_hunter.py scan
    python strategies/value_hunter.py scan --min-liquidity 10000
    python strategies/value_hunter.py watch "topic" --auto
"""

import argparse
import asyncio
import time
from datetime import datetime
from typing import Dict, List, Any, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.market_search import MarketSearch
from src.bot import TradingBot


class ValueHunter:
    """
    Scans markets for value opportunities.
    
    Criteria for a "value" trade:
    - Extreme price (very low or very high probability)
    - High liquidity (market is active)
    - Tight spread (can enter/exit without slippage)
    """
    
    def __init__(self):
        self.search = MarketSearch()
        self.bot = None
        
    def init_bot(self):
        """Initialize trading bot."""
        if not self.bot:
            self.bot = TradingBot(
                safe_address=os.environ.get("POLY_SAFE_ADDRESS"),
                private_key=os.environ.get("POLY_PRIVATE_KEY"),
            )
        return self.bot.is_initialized()
    
    def scan_opportunities(
        self,
        min_liquidity: float = 5000,
        max_spread: float = 0.05,
        extreme_threshold: float = 0.15,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Scan for value opportunities.
        
        Args:
            min_liquidity: Minimum market liquidity in USD
            max_spread: Maximum bid-ask spread (0.05 = 5%)
            extreme_threshold: Price below this or above (1-this) is "extreme"
            limit: Max markets to scan
            
        Returns:
            List of opportunity dicts
        """
        opportunities = []
        
        # Get trending markets (high volume = more info)
        markets = self.search.get_trending(limit=limit)
        
        for market in markets:
            if market['liquidity'] < min_liquidity:
                continue
                
            if not market['accepting_orders']:
                continue
            
            # Check each outcome
            for outcome, price in market['prices'].items():
                # Look for extreme prices
                if price < extreme_threshold or price > (1 - extreme_threshold):
                    # Get orderbook for spread check
                    token_id = market['token_ids'].get(outcome)
                    if not token_id:
                        continue
                    
                    book = self.search.get_orderbook(token_id)
                    if not book:
                        continue
                    
                    bids = book.get('bids', [])
                    asks = book.get('asks', [])
                    
                    if not bids or not asks:
                        continue
                    
                    best_bid = float(bids[0]['price'])
                    best_ask = float(asks[0]['price'])
                    spread = best_ask - best_bid
                    
                    if spread > max_spread:
                        continue
                    
                    # Calculate potential value
                    is_undervalued = price < extreme_threshold
                    
                    opportunities.append({
                        'question': market['question'],
                        'slug': market['slug'],
                        'outcome': outcome,
                        'price': price,
                        'best_bid': best_bid,
                        'best_ask': best_ask,
                        'spread': spread,
                        'liquidity': market['liquidity'],
                        'volume_24h': market['volume_24h'],
                        'token_id': token_id,
                        'direction': 'BUY' if is_undervalued else 'SELL',
                        'rationale': f"{'Low' if is_undervalued else 'High'} price ({price*100:.1f}%) with ${market['liquidity']:,.0f} liquidity",
                        'end_date': market['end_date'],
                    })
        
        # Sort by liquidity (more liquid = safer)
        opportunities.sort(key=lambda x: x['liquidity'], reverse=True)
        
        return opportunities
    
    def scan_momentum(
        self,
        query: str,
        check_interval: float = 60,
        momentum_threshold: float = 0.03
    ) -> List[Dict[str, Any]]:
        """
        Find markets with recent price momentum.
        
        Compares current price to recent price and looks for movement.
        """
        # First scan
        markets_t0 = {m['slug']: m for m in self.search.find_markets(query, limit=20)}
        
        print(f"Watching {len(markets_t0)} markets for {check_interval}s...")
        time.sleep(check_interval)
        
        # Second scan
        momentum_plays = []
        markets_t1 = {m['slug']: m for m in self.search.find_markets(query, limit=20)}
        
        for slug, m1 in markets_t1.items():
            if slug not in markets_t0:
                continue
            
            m0 = markets_t0[slug]
            
            for outcome in m1['prices']:
                p0 = m0['prices'].get(outcome, 0.5)
                p1 = m1['prices'].get(outcome, 0.5)
                change = p1 - p0
                
                if abs(change) >= momentum_threshold:
                    momentum_plays.append({
                        'question': m1['question'],
                        'slug': slug,
                        'outcome': outcome,
                        'price_before': p0,
                        'price_now': p1,
                        'change': change,
                        'direction': 'UP' if change > 0 else 'DOWN',
                        'token_id': m1['token_ids'].get(outcome),
                    })
        
        return momentum_plays
    
    async def execute_trade(
        self,
        opportunity: Dict[str, Any],
        size: float = 10,
        dry_run: bool = True
    ) -> bool:
        """Execute a trade on an opportunity."""
        if not self.init_bot():
            print("Failed to initialize bot")
            return False
        
        token_id = opportunity['token_id']
        side = opportunity['direction']
        price = opportunity['best_ask'] if side == 'BUY' else opportunity['best_bid']
        
        print(f"\n{'[DRY RUN] ' if dry_run else ''}Executing trade:")
        print(f"  {opportunity['question'][:50]}...")
        print(f"  {side} {opportunity['outcome'].upper()} @ {price:.4f}")
        print(f"  Size: ${size:.2f}")
        
        if dry_run:
            return True
        
        result = await self.bot.place_order(
            token_id=token_id,
            price=price,
            size=size / price,  # Convert USD to shares
            side=side
        )
        
        if result.success:
            print(f"  ✓ Order placed: {result.order_id}")
            return True
        else:
            print(f"  ✗ Order failed: {result.message}")
            return False


def print_opportunities(opps: List[Dict[str, Any]]):
    """Pretty print opportunities."""
    if not opps:
        print("\nNo opportunities found matching criteria.")
        return
    
    print(f"\n{'='*80}")
    print(f"  Found {len(opps)} opportunities")
    print(f"{'='*80}")
    
    for i, opp in enumerate(opps, 1):
        direction_symbol = "📈" if opp['direction'] == 'BUY' else "📉"
        print(f"\n{i}. {direction_symbol} {opp['outcome'].upper()} @ {opp['price']*100:.1f}%")
        print(f"   {opp['question'][:65]}...")
        print(f"   Bid: {opp['best_bid']:.4f} | Ask: {opp['best_ask']:.4f} | Spread: {opp['spread']*100:.1f}%")
        print(f"   Liquidity: ${opp['liquidity']:,.0f} | 24h Vol: ${opp['volume_24h']:,.0f}")
        print(f"   Rationale: {opp['rationale']}")


async def main():
    parser = argparse.ArgumentParser(description="Value Hunter Strategy")
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan for opportunities')
    scan_parser.add_argument('--min-liquidity', type=float, default=5000)
    scan_parser.add_argument('--max-spread', type=float, default=0.05)
    scan_parser.add_argument('--threshold', type=float, default=0.15)
    scan_parser.add_argument('--limit', type=int, default=50)
    
    # Watch command
    watch_parser = subparsers.add_parser('watch', help='Watch for momentum')
    watch_parser.add_argument('query', help='Search query')
    watch_parser.add_argument('--interval', type=float, default=60)
    watch_parser.add_argument('--auto', action='store_true', help='Auto-trade on signals')
    watch_parser.add_argument('--size', type=float, default=10)
    
    # Trade command
    trade_parser = subparsers.add_parser('trade', help='Trade top opportunity')
    trade_parser.add_argument('--size', type=float, default=10)
    trade_parser.add_argument('--dry-run', action='store_true')
    
    args = parser.parse_args()
    
    hunter = ValueHunter()
    
    if args.command == 'scan':
        print(f"Scanning for value opportunities...")
        print(f"  Min liquidity: ${args.min_liquidity:,.0f}")
        print(f"  Max spread: {args.max_spread*100:.0f}%")
        print(f"  Extreme threshold: <{args.threshold*100:.0f}% or >{(1-args.threshold)*100:.0f}%")
        
        opps = hunter.scan_opportunities(
            min_liquidity=args.min_liquidity,
            max_spread=args.max_spread,
            extreme_threshold=args.threshold,
            limit=args.limit
        )
        print_opportunities(opps)
        
    elif args.command == 'watch':
        print(f"Watching '{args.query}' for momentum (interval: {args.interval}s)")
        
        while True:
            plays = hunter.scan_momentum(args.query, check_interval=args.interval)
            
            if plays:
                print(f"\n⚡ {len(plays)} momentum signals detected!")
                for p in plays:
                    direction = "📈" if p['direction'] == 'UP' else "📉"
                    print(f"  {direction} {p['outcome'].upper()}: {p['price_before']*100:.1f}% → {p['price_now']*100:.1f}% ({p['change']*100:+.1f}%)")
                    print(f"     {p['question'][:50]}...")
                
                if args.auto:
                    for p in plays:
                        # Trade in direction of momentum
                        opp = {
                            'question': p['question'],
                            'outcome': p['outcome'],
                            'token_id': p['token_id'],
                            'direction': 'BUY' if p['direction'] == 'UP' else 'SELL',
                            'best_ask': p['price_now'],
                            'best_bid': p['price_now'],
                        }
                        await hunter.execute_trade(opp, size=args.size, dry_run=False)
            else:
                print(f"  No momentum detected. Continuing...")
    
    elif args.command == 'trade':
        opps = hunter.scan_opportunities()
        if opps:
            await hunter.execute_trade(opps[0], size=args.size, dry_run=args.dry_run)
        else:
            print("No opportunities to trade.")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
