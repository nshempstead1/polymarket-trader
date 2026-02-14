# Polymarket Trading Bot - Deployment Guide

## What Clawdbot Needs To Do

Everything is built. The ONLY tasks remaining are:

1. **Set up credentials** (.env file)
2. **Install dependencies**
3. **Run the connection test**
4. **Optional: Set up VPN if needed for region restrictions**

---

## Step 1: Clone and Install

```bash
# Clone the repo (or copy these files to the server)
git clone https://github.com/discountry/polymarket-trading-bot.git
cd polymarket-trading-bot

# Install Python dependencies
pip install -r requirements.txt
```

## Step 2: Create .env File

Copy the example and fill in your actual credentials:

```bash
cp .env.example .env
```

Edit `.env` with these values:

```env
# REQUIRED - Get from MetaMask (Account Details → Export Private Key)
POLY_PRIVATE_KEY=your_64_character_hex_private_key

# REQUIRED - Get from polymarket.com/settings → Wallet Address
POLY_SAFE_ADDRESS=0xYourPolymarketSafeAddress

# OPTIONAL but recommended - Apply at polymarket.com/settings?tab=builder
# These enable gasless (no gas fee) trading
POLY_BUILDER_API_KEY=your_builder_key
POLY_BUILDER_API_SECRET=your_builder_secret
POLY_BUILDER_API_PASSPHRASE=your_builder_passphrase
```

**Where to find each value:**

| Credential | Where to get it |
|---|---|
| POLY_PRIVATE_KEY | MetaMask → three dots → Account Details → Show Private Key |
| POLY_SAFE_ADDRESS | polymarket.com/settings → "Deposit Address" or wallet shown |
| Builder credentials | polymarket.com/settings?tab=builder (must apply first) |

## Step 3: Test the Connection

```bash
python scripts/test_connection.py
```

This will verify:
- Your credentials are valid
- API connections work
- WebSocket connects
- Market search works
- Bot initializes

**All checks should pass before you trade.**

## Step 4 (Optional): VPN Setup

If Polymarket is restricted in your region, set up a VPN before running.
The bot makes HTTP/WebSocket connections to:
- `clob.polymarket.com` (order API)
- `gamma-api.polymarket.com` (market discovery)
- `ws-subscriptions-clob.polymarket.com` (real-time data)
- `relayer-v2.polymarket.com` (gasless transactions)

Any VPN that routes traffic through a non-restricted country will work.

---

## How to Use the Bot

### Search for Markets

```bash
# Search by keyword
python scripts/market_explorer.py search "Trump"
python scripts/market_explorer.py search "Bitcoin" --tokens

# See trending markets
python scripts/market_explorer.py trending

# Search events (groups of markets)
python scripts/market_explorer.py events "election"

# Get details on a specific market
python scripts/market_explorer.py info --slug "market-slug-here"
```

### Interactive Trading

```bash
# Search, select a market, and place an order interactively
python scripts/market_explorer.py trade "topic"
```

### Watch a Market (No Trading)

```bash
python scripts/general_trader.py watch "Trump" --outcome yes
```

### Place Orders

```bash
# Buy
python scripts/general_trader.py buy "Trump" --outcome yes --price 0.55 --size 20

# Sell
python scripts/general_trader.py sell "Trump" --outcome yes --price 0.70 --size 20
```

### Auto-Trade (Buy Low / Sell High)

```bash
python scripts/general_trader.py auto "topic" \
    --outcome yes \
    --buy-below 0.40 \
    --sell-above 0.60 \
    --size 10 \
    --interval 5
```

This will automatically:
- Buy when price drops below 0.40
- Sell when price rises above 0.60
- Check every 5 seconds

### Flash Crash Strategy (15-min Crypto Markets)

```bash
python apps/run_flash_crash.py --coin BTC --size 5 --drop 0.25
```

### Interactive Bot Shell

```bash
python scripts/run_bot.py --interactive
```

Commands: `status`, `place`, `cancel`, `cancel-all`, `trades`, `price`, `exit`

---

## File Structure (What Was Added)

```
polymarket-trading-bot/
├── src/
│   ├── market_search.py       ← NEW: Search ANY market by keyword
│   └── (existing files unchanged)
│
├── scripts/
│   ├── market_explorer.py     ← NEW: CLI to search/browse/trade markets
│   ├── general_trader.py      ← NEW: Watch/buy/sell/auto-trade any market
│   ├── test_connection.py     ← NEW: Verify setup before trading
│   └── (existing files unchanged)
│
└── DEPLOY.md                  ← This file
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'web3'` | Run `pip install -r requirements.txt` |
| `POLY_PRIVATE_KEY not set` | Create `.env` file (Step 2) |
| `Invalid private key` | Must be 64 hex chars, with or without 0x prefix |
| WebSocket timeout | Check VPN connection; Polymarket may be blocked |
| `Order failed` | Check USDC balance on Polymarket; verify Safe address |
| `401 Unauthorized` | API credentials may need to be re-derived; try running quickstart |
| Can't find market | Try broader search terms; market may have closed |

## Important Notes

- **Start small.** Test with $1-5 orders first.
- **The bot trades real money.** Every order hits your Polymarket account.
- **Private key = full wallet access.** Never share your .env file.
- **No guarantee of profit.** Prediction markets are speculative.
- **Monitor positions.** The auto-trader is simple; it doesn't handle edge cases like market resolution.
