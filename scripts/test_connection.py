#!/usr/bin/env python3
"""
Connection Test - Verify your setup works before live trading

Checks:
1. Environment variables are set
2. Private key is valid
3. Safe address is valid
4. API connection works (no auth needed)
5. Authenticated API works (needs valid credentials)
6. Builder/gasless mode (if configured)
7. WebSocket connection
8. Market search works

Usage:
    python scripts/test_connection.py
"""

import os
import sys
import asyncio
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))


class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def ok(msg):
    print(f"  {Colors.GREEN}✓{Colors.RESET} {msg}")


def warn(msg):
    print(f"  {Colors.YELLOW}⚠{Colors.RESET} {msg}")


def fail(msg):
    print(f"  {Colors.RED}✗{Colors.RESET} {msg}")


def section(title):
    print(f"\n{Colors.BOLD}{Colors.BLUE}[{title}]{Colors.RESET}")


async def main():
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Polymarket Trading Bot - Connection Test{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")

    passed = 0
    failed = 0
    warnings = 0

    # 1. Environment variables
    section("1. Environment Variables")
    private_key = os.environ.get("POLY_PRIVATE_KEY", "")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS", "")
    builder_key = os.environ.get("POLY_BUILDER_API_KEY", "")
    builder_secret = os.environ.get("POLY_BUILDER_API_SECRET", "")
    builder_pass = os.environ.get("POLY_BUILDER_API_PASSPHRASE", "")

    if private_key:
        ok(f"POLY_PRIVATE_KEY is set ({len(private_key)} chars)")
        passed += 1
    else:
        fail("POLY_PRIVATE_KEY is NOT set")
        print(f"    {Colors.DIM}Set it: export POLY_PRIVATE_KEY=your_key{Colors.RESET}")
        failed += 1

    if safe_address:
        ok(f"POLY_SAFE_ADDRESS is set ({safe_address[:10]}...)")
        passed += 1
    else:
        fail("POLY_SAFE_ADDRESS is NOT set")
        print(f"    {Colors.DIM}Get it from polymarket.com/settings{Colors.RESET}")
        failed += 1

    if builder_key and builder_secret and builder_pass:
        ok("Builder credentials are set (gasless mode available)")
        passed += 1
    else:
        warn("Builder credentials not set (will pay gas fees)")
        print(f"    {Colors.DIM}Apply at polymarket.com/settings?tab=builder{Colors.RESET}")
        warnings += 1

    # 2. Private key validation
    section("2. Private Key Validation")
    if private_key:
        try:
            from src.crypto import verify_private_key
            is_valid, result = verify_private_key(private_key)
            if is_valid:
                ok(f"Private key format is valid")
                passed += 1

                from src.signer import OrderSigner
                signer = OrderSigner(private_key)
                ok(f"Signer address: {signer.address}")
                passed += 1
            else:
                fail(f"Private key invalid: {result}")
                failed += 1
        except Exception as e:
            fail(f"Key validation error: {e}")
            failed += 1
    else:
        fail("Skipped (no key)")
        failed += 1

    # 3. Safe address validation
    section("3. Safe Address Validation")
    if safe_address:
        from src.utils import validate_address
        if validate_address(safe_address):
            ok(f"Safe address format is valid")
            passed += 1
        else:
            fail(f"Safe address format is invalid")
            failed += 1
    else:
        fail("Skipped (no address)")
        failed += 1

    # 4. Public API (no auth)
    section("4. Public API Connection")
    try:
        import requests
        response = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": "0"},
            timeout=10
        )
        if response.status_code in (200, 400):
            ok(f"CLOB API is reachable (status: {response.status_code})")
            passed += 1
        else:
            warn(f"CLOB API returned status {response.status_code}")
            warnings += 1
    except Exception as e:
        fail(f"CLOB API unreachable: {e}")
        failed += 1

    try:
        response = requests.get(
            "https://gamma-api.polymarket.com/markets?limit=1",
            timeout=10
        )
        if response.status_code == 200:
            ok(f"Gamma API is reachable")
            passed += 1
        else:
            fail(f"Gamma API returned status {response.status_code}")
            failed += 1
    except Exception as e:
        fail(f"Gamma API unreachable: {e}")
        failed += 1

    # 5. Market search
    section("5. Market Search")
    try:
        from src.market_search import MarketSearch
        search = MarketSearch()
        markets = search.get_trending(limit=3)
        if markets:
            ok(f"Market search works ({len(markets)} trending markets found)")
            for m in markets[:3]:
                prices = " | ".join(f"{k}: {v:.2f}" for k, v in m["prices"].items())
                print(f"    {Colors.DIM}{m['question'][:60]}  [{prices}]{Colors.RESET}")
            passed += 1
        else:
            warn("Market search returned no results")
            warnings += 1
    except Exception as e:
        fail(f"Market search failed: {e}")
        failed += 1

    # 6. Bot initialization
    section("6. Bot Initialization")
    if private_key and safe_address:
        try:
            from src.bot import TradingBot
            from src.config import Config

            config = Config.from_env()
            bot = TradingBot(config=config, private_key=private_key)

            if bot.is_initialized():
                ok("Bot initialized successfully")
                ok(f"Gasless mode: {'ENABLED' if config.use_gasless else 'DISABLED'}")
                passed += 1
            else:
                fail("Bot initialization returned False")
                failed += 1
        except Exception as e:
            fail(f"Bot initialization failed: {e}")
            failed += 1
    else:
        fail("Skipped (missing credentials)")
        failed += 1

    # 7. Authenticated API
    section("7. Authenticated API")
    if private_key and safe_address:
        try:
            orders = await bot.get_open_orders()
            ok(f"Authenticated API works ({len(orders)} open orders)")
            passed += 1
        except Exception as e:
            warn(f"Authenticated API failed: {e}")
            print(f"    {Colors.DIM}This may be normal if you haven't traded yet{Colors.RESET}")
            warnings += 1
    else:
        fail("Skipped (missing credentials)")
        failed += 1

    # 8. WebSocket
    section("8. WebSocket Connection")
    try:
        from src.websocket_client import MarketWebSocket
        ws = MarketWebSocket()
        connected = await asyncio.wait_for(ws.connect(), timeout=10)
        if connected:
            ok("WebSocket connected successfully")
            await ws.disconnect()
            passed += 1
        else:
            fail("WebSocket connection failed")
            failed += 1
    except asyncio.TimeoutError:
        fail("WebSocket connection timed out")
        failed += 1
    except Exception as e:
        fail(f"WebSocket error: {e}")
        failed += 1

    # Summary
    total = passed + failed + warnings
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Results:{Colors.RESET}")
    print(f"    {Colors.GREEN}Passed:   {passed}/{total}{Colors.RESET}")
    if warnings:
        print(f"    {Colors.YELLOW}Warnings: {warnings}/{total}{Colors.RESET}")
    if failed:
        print(f"    {Colors.RED}Failed:   {failed}/{total}{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")

    if failed == 0:
        print(f"\n  {Colors.GREEN}{Colors.BOLD}All checks passed! You're ready to trade.{Colors.RESET}")
        print(f"\n  Next steps:")
        print(f"    python scripts/market_explorer.py trending")
        print(f"    python scripts/market_explorer.py search \"your topic\"")
        print(f"    python scripts/market_explorer.py trade \"your topic\"")
        print(f"    python scripts/general_trader.py watch \"your topic\"")
    elif failed <= 2:
        print(f"\n  {Colors.YELLOW}Almost ready — fix the failures above and re-run.{Colors.RESET}")
    else:
        print(f"\n  {Colors.RED}Multiple issues found. Start with setting your .env file.{Colors.RESET}")

    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
