#!/usr/bin/env python3
"""
General Market Trader - Trade ANY Polymarket market with configurable strategies

Unlike the flash crash strategy (which only works on 15-minute crypto markets),
this script lets you monitor and trade any market on Polymarket.

Modes:
    1. Watch mode - monitor prices and show live orderbook
    2. Limit order mode - place orders at your target price
    3. Auto mode - buy when price drops below threshold, sell above

Usage:
    # Watch a market (monitor only, no trading)
    python scripts/general_trader.py watch "Trump" --outcome yes

    # Place a limit buy order on a market
    python scripts/general_trader.py buy "Trump" --outcome yes --price 0.55 --size 20

    # Auto-trade: buy below 0.40, sell above 0.60
    python scripts/general_trader.py auto "Trump" --outcome yes --buy-below 0.40 --sell-above 0.60 --size 10
"""

import os
import sys
import asyncio
import argparse
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.market_search import MarketSearch
from src.websocket_client import MarketWebSocket, OrderbookSnapshot


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


def select_market(query: str, outcome: str = None) -> tuple:
    """Search and let user select a market. Returns (market_dict, selected_outcome, token_id)."""
    search = MarketSearch()
    print(f"\n{Colors.CYAN}Searching for '{query}'...{Colors.RESET}")

    markets = search.find_markets(query, limit=10)
    if not markets:
        print(f"{Colors.RED}No markets found.{Colors.RESET}")
        sys.exit(1)

    # If only one result, auto-select
    if len(markets) == 1:
        market = markets[0]
        print(f"\n{Colors.GREEN}Found: {market['question']}{Colors.RESET}")
    else:
        for i, m in enumerate(markets, 1):
            prices = ""
            if m["prices"]:
                parts = [f"{k}: {v:.2f}" for k, v in m["prices"].items()]
                prices = " | ".join(parts)
            print(f"  {i}. {m['question']}  [{prices}]")

        try:
            choice = input(f"\n{Colors.BOLD}Select market: {Colors.RESET}").strip()
            idx = int(choice) - 1
            market = markets[idx]
        except (ValueError, IndexError, KeyboardInterrupt):
            print("Cancelled.")
            sys.exit(0)

    # Select outcome
    outcomes = list(market["token_ids"].keys())
    if outcome and outcome.lower() in market["token_ids"]:
        selected = outcome.lower()
    elif len(outcomes) == 1:
        selected = outcomes[0]
    else:
        print(f"\n  Outcomes:")
        for i, o in enumerate(outcomes, 1):
            price = market["prices"].get(o, 0)
            print(f"    {i}. {o.upper()} (current: {price:.2f})")

        if outcome:
            # Try partial match
            for o in outcomes:
                if outcome.lower() in o.lower():
                    selected = o
                    break
            else:
                try:
                    choice = input(f"  Select outcome: ").strip()
                    selected = outcomes[int(choice) - 1]
                except (ValueError, IndexError):
                    selected = outcomes[0]
        else:
            try:
                choice = input(f"  Select outcome: ").strip()
                selected = outcomes[int(choice) - 1]
            except (ValueError, IndexError):
                selected = outcomes[0]

    token_id = market["token_ids"][selected]
    print(f"\n  {Colors.GREEN}Selected: {market['question']} → {selected.upper()}{Colors.RESET}")
    print(f"  Token: {token_id[:30]}...")

    return market, selected, token_id


async def watch_market(query: str, outcome: str = None, refresh: float = 2.0):
    """Watch mode: live price monitoring via REST polling."""
    market, selected, token_id = select_market(query, outcome)
    search = MarketSearch()

    print(f"\n{Colors.BOLD}Watching {market['question']} → {selected.upper()}{Colors.RESET}")
    print(f"Press Ctrl+C to stop\n")

    prev_price = None
    while True:
        try:
            book = search.get_orderbook(token_id)
            if not book:
                print(f"{Colors.YELLOW}No data{Colors.RESET}")
                await asyncio.sleep(refresh)
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            mid = (best_bid + best_ask) / 2 if best_bid and best_ask < 1 else best_bid or best_ask
            spread = best_ask - best_bid

            # Direction indicator
            if prev_price is not None:
                if mid > prev_price:
                    arrow = f"{Colors.GREEN}▲{Colors.RESET}"
                elif mid < prev_price:
                    arrow = f"{Colors.RED}▼{Colors.RESET}"
                else:
                    arrow = f"{Colors.DIM}━{Colors.RESET}"
            else:
                arrow = " "

            bid_depth = sum(float(b["size"]) for b in bids[:5])
            ask_depth = sum(float(a["size"]) for a in asks[:5])

            ts = time.strftime("%H:%M:%S")
            print(
                f"  [{ts}] {arrow} Mid: {Colors.BOLD}{mid:.4f}{Colors.RESET} ({mid*100:.1f}%)  |  "
                f"Bid: {Colors.GREEN}{best_bid:.4f}{Colors.RESET}  Ask: {Colors.RED}{best_ask:.4f}{Colors.RESET}  |  "
                f"Spread: {spread:.4f}  |  Depth: {bid_depth:.0f}/{ask_depth:.0f}"
            )

            prev_price = mid
            await asyncio.sleep(refresh)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"{Colors.RED}Error: {e}{Colors.RESET}")
            await asyncio.sleep(refresh)


async def place_order(query, outcome, price, size, side):
    """Place a single order on a market."""
    market, selected, token_id = select_market(query, outcome)

    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")
    if not private_key or not safe_address:
        print(f"{Colors.RED}Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env{Colors.RESET}")
        sys.exit(1)

    from src.bot import TradingBot
    from src.config import Config

    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Bot failed to initialize{Colors.RESET}")
        sys.exit(1)

    cost = price * size
    print(f"\n{Colors.BOLD}Order:{Colors.RESET}")
    print(f"  {side.upper()} {selected.upper()} @ {price:.4f} x {size:.1f} shares")
    print(f"  Estimated cost: ~${cost:.2f} USDC")

    confirm = input(f"\n  Confirm? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Cancelled.")
        return

    result = await bot.place_order(
        token_id=token_id,
        price=price,
        size=size,
        side=side.upper(),
    )

    if result.success:
        print(f"\n{Colors.GREEN}✓ Order placed! ID: {result.order_id}{Colors.RESET}")
    else:
        print(f"\n{Colors.RED}✗ Failed: {result.message}{Colors.RESET}")


async def auto_trade(query, outcome, buy_below, sell_above, size, check_interval=5.0):
    """Auto-trade: buy when price drops below threshold, sell when it rises above."""
    market, selected, token_id = select_market(query, outcome)

    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")
    if not private_key or not safe_address:
        print(f"{Colors.RED}Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS in .env{Colors.RESET}")
        sys.exit(1)

    from src.bot import TradingBot
    from src.config import Config

    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)
    search = MarketSearch()

    if not bot.is_initialized():
        print(f"{Colors.RED}Bot failed to initialize{Colors.RESET}")
        sys.exit(1)

    print(f"\n{Colors.BOLD}Auto-Trading: {market['question']} → {selected.upper()}{Colors.RESET}")
    print(f"  Buy below:  {buy_below:.4f} ({buy_below*100:.1f}%)")
    print(f"  Sell above: {sell_above:.4f} ({sell_above*100:.1f}%)")
    print(f"  Size: {size:.1f} shares per trade")
    print(f"  Checking every {check_interval:.0f}s")
    print(f"\n{Colors.YELLOW}Press Ctrl+C to stop{Colors.RESET}\n")

    holding = False  # Track if we have a position
    entry_price = 0.0
    trades = 0
    total_pnl = 0.0

    while True:
        try:
            price = search.get_market_price(token_id)
            if price is None:
                await asyncio.sleep(check_interval)
                continue

            ts = time.strftime("%H:%M:%S")

            if not holding and price <= buy_below:
                # Buy signal
                buy_price = min(price + 0.02, 0.99)
                print(f"  [{ts}] {Colors.GREEN}BUY SIGNAL{Colors.RESET} - Price {price:.4f} <= {buy_below:.4f}")

                result = await bot.place_order(
                    token_id=token_id,
                    price=buy_price,
                    size=size,
                    side="BUY",
                )

                if result.success:
                    holding = True
                    entry_price = price
                    trades += 1
                    print(f"  [{ts}] {Colors.GREEN}✓ Bought @ {price:.4f}{Colors.RESET} (order: {result.order_id})")
                else:
                    print(f"  [{ts}] {Colors.RED}✗ Buy failed: {result.message}{Colors.RESET}")

            elif holding and price >= sell_above:
                # Sell signal
                sell_price = max(price - 0.02, 0.01)
                pnl = (price - entry_price) * size
                total_pnl += pnl

                print(f"  [{ts}] {Colors.RED}SELL SIGNAL{Colors.RESET} - Price {price:.4f} >= {sell_above:.4f}")

                result = await bot.place_order(
                    token_id=token_id,
                    price=sell_price,
                    size=size,
                    side="SELL",
                )

                if result.success:
                    holding = False
                    pnl_color = Colors.GREEN if pnl >= 0 else Colors.RED
                    print(f"  [{ts}] {Colors.GREEN}✓ Sold @ {price:.4f}{Colors.RESET} PnL: {pnl_color}${pnl:+.2f}{Colors.RESET}")
                else:
                    print(f"  [{ts}] {Colors.RED}✗ Sell failed: {result.message}{Colors.RESET}")

            else:
                # No signal - log status
                status = f"{Colors.CYAN}HOLDING @ {entry_price:.4f}{Colors.RESET}" if holding else f"{Colors.DIM}WATCHING{Colors.RESET}"
                unrealized = (price - entry_price) * size if holding else 0
                ur_color = Colors.GREEN if unrealized >= 0 else Colors.RED

                print(
                    f"  [{ts}] {status}  |  Price: {price:.4f}  |  "
                    f"Trades: {trades}  |  PnL: ${total_pnl:+.2f}  |  "
                    f"Unrealized: {ur_color}${unrealized:+.2f}{Colors.RESET}"
                )

            await asyncio.sleep(check_interval)

        except KeyboardInterrupt:
            print(f"\n\n{Colors.BOLD}Session Summary:{Colors.RESET}")
            print(f"  Trades: {trades}")
            print(f"  Realized PnL: ${total_pnl:+.2f}")
            if holding:
                print(f"  {Colors.YELLOW}Warning: Still holding a position!{Colors.RESET}")
            break
        except Exception as e:
            print(f"  {Colors.RED}Error: {e}{Colors.RESET}")
            await asyncio.sleep(check_interval)


def main():
    parser = argparse.ArgumentParser(description="General Market Trader for Polymarket")
    subparsers = parser.add_subparsers(dest="command")

    # Watch
    watch_parser = subparsers.add_parser("watch", help="Monitor a market (no trading)")
    watch_parser.add_argument("query", type=str, help="Market search term")
    watch_parser.add_argument("--outcome", type=str, default=None, help="Outcome (yes/no/up/down)")
    watch_parser.add_argument("--refresh", type=float, default=3.0, help="Refresh interval (seconds)")

    # Buy
    buy_parser = subparsers.add_parser("buy", help="Place a buy order")
    buy_parser.add_argument("query", type=str, help="Market search term")
    buy_parser.add_argument("--outcome", type=str, default=None, help="Outcome")
    buy_parser.add_argument("--price", type=float, required=True, help="Buy price (0-1)")
    buy_parser.add_argument("--size", type=float, required=True, help="Number of shares")

    # Sell
    sell_parser = subparsers.add_parser("sell", help="Place a sell order")
    sell_parser.add_argument("query", type=str, help="Market search term")
    sell_parser.add_argument("--outcome", type=str, default=None, help="Outcome")
    sell_parser.add_argument("--price", type=float, required=True, help="Sell price (0-1)")
    sell_parser.add_argument("--size", type=float, required=True, help="Number of shares")

    # Auto
    auto_parser = subparsers.add_parser("auto", help="Auto-trade with buy/sell thresholds")
    auto_parser.add_argument("query", type=str, help="Market search term")
    auto_parser.add_argument("--outcome", type=str, default=None, help="Outcome")
    auto_parser.add_argument("--buy-below", type=float, required=True, help="Buy when price drops below this")
    auto_parser.add_argument("--sell-above", type=float, required=True, help="Sell when price rises above this")
    auto_parser.add_argument("--size", type=float, default=10.0, help="Shares per trade")
    auto_parser.add_argument("--interval", type=float, default=5.0, help="Check interval (seconds)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "watch":
        asyncio.run(watch_market(args.query, args.outcome, args.refresh))

    elif args.command == "buy":
        asyncio.run(place_order(args.query, args.outcome, args.price, args.size, "BUY"))

    elif args.command == "sell":
        asyncio.run(place_order(args.query, args.outcome, args.price, args.size, "SELL"))

    elif args.command == "auto":
        asyncio.run(auto_trade(
            args.query, args.outcome,
            args.buy_below, args.sell_above,
            args.size, args.interval,
        ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone.")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        sys.exit(1)
