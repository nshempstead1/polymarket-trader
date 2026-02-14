#!/usr/bin/env python3
"""
Market Explorer - Interactive CLI for finding and trading ANY Polymarket market

Usage:
    # Search for markets
    python scripts/market_explorer.py search "Trump"
    python scripts/market_explorer.py search "Bitcoin" --limit 10

    # Get trending markets
    python scripts/market_explorer.py trending

    # Search events (groups of related markets)
    python scripts/market_explorer.py events "election"

    # Get details on a specific market
    python scripts/market_explorer.py info --slug "will-trump-win"
    python scripts/market_explorer.py info --id "0xabc123..."

    # Get live orderbook for a token
    python scripts/market_explorer.py book <token_id>

    # Interactive trading mode
    python scripts/market_explorer.py trade "Trump"
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.market_search import MarketSearch


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def print_markets(markets, show_tokens=False):
    """Print a list of markets in a clean table format."""
    if not markets:
        print(f"{Colors.YELLOW}No markets found.{Colors.RESET}")
        return

    print(f"\n{Colors.BOLD}Found {len(markets)} market(s):{Colors.RESET}\n")

    for i, m in enumerate(markets, 1):
        status = f"{Colors.GREEN}ACTIVE{Colors.RESET}" if m["accepting_orders"] else f"{Colors.RED}CLOSED{Colors.RESET}"

        print(f"{Colors.BOLD}{i:>3}. {m['question']}{Colors.RESET}")
        print(f"     {status}  |  Liquidity: ${m['liquidity']:,.0f}  |  Vol 24h: ${m.get('volume_24h', 0):,.0f}")

        if m["prices"]:
            price_parts = []
            for outcome, price in m["prices"].items():
                color = Colors.GREEN if price > 0.5 else Colors.RED if price < 0.5 else Colors.YELLOW
                price_parts.append(f"{outcome.upper()}: {color}{price:.2f} ({price*100:.0f}%){Colors.RESET}")
            print(f"     {' | '.join(price_parts)}")

        if show_tokens and m["token_ids"]:
            for outcome, tid in m["token_ids"].items():
                print(f"     {Colors.DIM}{outcome.upper()} token: {tid}{Colors.RESET}")

        print()


def print_events(events):
    """Print events with their nested markets."""
    if not events:
        print(f"{Colors.YELLOW}No events found.{Colors.RESET}")
        return

    for event in events:
        print(f"\n{Colors.BOLD}{'='*70}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}  {event['title']}{Colors.RESET}")
        print(f"  Liquidity: ${event['liquidity']:,.0f}  |  Volume: ${event['volume']:,.0f}")
        print(f"{Colors.BOLD}{'='*70}{Colors.RESET}")

        for m in event["markets"]:
            status = f"{Colors.GREEN}●{Colors.RESET}" if m["accepting_orders"] else f"{Colors.RED}●{Colors.RESET}"
            prices = ""
            if m["prices"]:
                parts = []
                for outcome, price in m["prices"].items():
                    parts.append(f"{outcome}: {price:.2f}")
                prices = " | ".join(parts)

            print(f"  {status} {m['question']}")
            if prices:
                print(f"    {Colors.DIM}{prices}{Colors.RESET}")


def print_orderbook(book, levels=10):
    """Print orderbook in a readable format."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    print(f"\n{Colors.BOLD}{'BIDS':>25}  |  {'ASKS':<25}{Colors.RESET}")
    print(f"{'Price':>12} {'Size':>12}  |  {'Price':<12} {'Size':<12}")
    print("-" * 55)

    for i in range(min(levels, max(len(bids), len(asks)))):
        bid_str = ""
        ask_str = ""

        if i < len(bids):
            bid_str = f"{Colors.GREEN}{float(bids[i]['price']):>12.4f} {float(bids[i]['size']):>12.1f}{Colors.RESET}"
        else:
            bid_str = f"{'':>12} {'':>12}"

        if i < len(asks):
            ask_str = f"{Colors.RED}{float(asks[i]['price']):<12.4f} {float(asks[i]['size']):<12.1f}{Colors.RESET}"
        else:
            ask_str = f"{'':>12} {'':>12}"

        print(f"{bid_str}  |  {ask_str}")

    if bids and asks:
        spread = float(asks[0]["price"]) - float(bids[0]["price"])
        mid = (float(asks[0]["price"]) + float(bids[0]["price"])) / 2
        print(f"\n  Mid: {mid:.4f}  |  Spread: {spread:.4f}")


async def interactive_trade(query):
    """Interactive mode: search, select a market, and trade."""
    search = MarketSearch()

    # Search
    print(f"\n{Colors.CYAN}Searching for '{query}'...{Colors.RESET}")
    markets = search.find_markets(query, limit=10)

    if not markets:
        print(f"{Colors.YELLOW}No markets found for '{query}'{Colors.RESET}")
        return

    print_markets(markets, show_tokens=True)

    # Select market
    try:
        choice = input(f"\n{Colors.BOLD}Select market number (or 'q' to quit): {Colors.RESET}").strip()
        if choice.lower() == 'q':
            return
        idx = int(choice) - 1
        if idx < 0 or idx >= len(markets):
            print(f"{Colors.RED}Invalid selection{Colors.RESET}")
            return
    except (ValueError, KeyboardInterrupt):
        return

    market = markets[idx]
    search.print_market(market, show_tokens=True)

    # Show orderbooks for each outcome
    print(f"\n{Colors.BOLD}Live Orderbooks:{Colors.RESET}")
    for outcome, tid in market["token_ids"].items():
        print(f"\n{Colors.BOLD}{outcome.upper()}{Colors.RESET} (token: {tid[:20]}...)")
        book = search.get_orderbook(tid)
        if book:
            print_orderbook(book, levels=5)

    # Check if bot is configured
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")

    if not private_key or not safe_address:
        print(f"\n{Colors.YELLOW}To trade, set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env{Colors.RESET}")
        print(f"\nToken IDs for manual use:")
        for outcome, tid in market["token_ids"].items():
            print(f"  {outcome.upper()}: {tid}")
        return

    # Offer to trade
    print(f"\n{Colors.BOLD}Place an order?{Colors.RESET}")
    for i, (outcome, tid) in enumerate(market["token_ids"].items(), 1):
        price = market["prices"].get(outcome, 0)
        print(f"  {i}. BUY {outcome.upper()} (current: {price:.2f})")

    try:
        side_choice = input(f"\n{Colors.BOLD}Select outcome (or 'q' to quit): {Colors.RESET}").strip()
        if side_choice.lower() == 'q':
            return

        side_idx = int(side_choice) - 1
        outcomes = list(market["token_ids"].keys())
        if side_idx < 0 or side_idx >= len(outcomes):
            print(f"{Colors.RED}Invalid selection{Colors.RESET}")
            return

        selected_outcome = outcomes[side_idx]
        selected_token = market["token_ids"][selected_outcome]
        current_price = market["prices"].get(selected_outcome, 0.5)

        price_input = input(f"  Price (default {current_price:.2f}): ").strip()
        price = float(price_input) if price_input else current_price

        size_input = input(f"  Size in shares (default 10): ").strip()
        size = float(size_input) if size_input else 10.0

        cost = price * size
        print(f"\n{Colors.BOLD}Order Summary:{Colors.RESET}")
        print(f"  BUY {selected_outcome.upper()}")
        print(f"  Price: {price:.4f} ({price*100:.1f}%)")
        print(f"  Size: {size:.1f} shares")
        print(f"  Cost: ~${cost:.2f} USDC")
        print(f"  Token: {selected_token[:30]}...")

        confirm = input(f"\n{Colors.BOLD}Confirm? (y/n): {Colors.RESET}").strip().lower()
        if confirm != 'y':
            print("Order cancelled.")
            return

        # Execute trade
        from src.bot import TradingBot
        from src.config import Config

        config = Config.from_env()
        bot = TradingBot(config=config, private_key=private_key)

        if not bot.is_initialized():
            print(f"{Colors.RED}Bot failed to initialize{Colors.RESET}")
            return

        result = await bot.place_order(
            token_id=selected_token,
            price=price,
            size=size,
            side="BUY",
        )

        if result.success:
            print(f"\n{Colors.GREEN}✓ Order placed! ID: {result.order_id}{Colors.RESET}")
        else:
            print(f"\n{Colors.RED}✗ Order failed: {result.message}{Colors.RESET}")

    except (ValueError, KeyboardInterrupt):
        print("\nCancelled.")


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Market Explorer - Find and trade any market"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # search
    search_parser = subparsers.add_parser("search", help="Search markets by keyword")
    search_parser.add_argument("query", type=str, help="Search term")
    search_parser.add_argument("--limit", type=int, default=10, help="Max results")
    search_parser.add_argument("--tokens", action="store_true", help="Show token IDs")

    # trending
    subparsers.add_parser("trending", help="Show trending markets")

    # events
    events_parser = subparsers.add_parser("events", help="Search events")
    events_parser.add_argument("query", type=str, nargs="?", default="", help="Search term")

    # info
    info_parser = subparsers.add_parser("info", help="Get market details")
    info_parser.add_argument("--slug", type=str, help="Market slug")
    info_parser.add_argument("--id", type=str, help="Condition ID")

    # book
    book_parser = subparsers.add_parser("book", help="Show orderbook for a token")
    book_parser.add_argument("token_id", type=str, help="Token ID")
    book_parser.add_argument("--levels", type=int, default=10, help="Orderbook depth")

    # trade
    trade_parser = subparsers.add_parser("trade", help="Interactive search and trade")
    trade_parser.add_argument("query", type=str, help="Search term")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    search = MarketSearch()

    if args.command == "search":
        markets = search.find_markets(args.query, limit=args.limit)
        print_markets(markets, show_tokens=args.tokens)

    elif args.command == "trending":
        print(f"\n{Colors.BOLD}Trending Markets:{Colors.RESET}")
        markets = search.get_trending(limit=10)
        print_markets(markets)

    elif args.command == "events":
        events = search.get_events(args.query, limit=10)
        print_events(events)

    elif args.command == "info":
        market = None
        if args.slug:
            market = search.get_market_by_slug(args.slug)
        elif args.id:
            market = search.get_market_by_id(args.id)
        else:
            print(f"{Colors.RED}Provide --slug or --id{Colors.RESET}")
            return

        if market:
            search.print_market(market, show_tokens=True)
        else:
            print(f"{Colors.YELLOW}Market not found.{Colors.RESET}")

    elif args.command == "book":
        book = search.get_orderbook(args.token_id)
        if book:
            print_orderbook(book, levels=args.levels)
        else:
            print(f"{Colors.RED}Failed to get orderbook{Colors.RESET}")

    elif args.command == "trade":
        asyncio.run(interactive_trade(args.query))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone.")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
