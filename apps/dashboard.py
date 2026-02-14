#!/usr/bin/env python3
"""
Trading Dashboard - Simple web UI for monitoring the daemon

Run: python apps/dashboard.py
View: http://localhost:8080
"""

import os
import json
import requests
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

DATA_DIR = Path("data")
WALLET = "0x769Bb0B16c551aA103F8aC7642677DDCc9dd8447"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Polymarket Trading Dashboard</title>
    <meta http-equiv="refresh" content="30">
    <style>
        * { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
        body { background: #0d1117; color: #c9d1d9; padding: 20px; margin: 0; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }
        h2 { color: #8b949e; margin-top: 30px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin: 10px 0; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .stat { text-align: center; }
        .stat-value { font-size: 28px; font-weight: bold; color: #58a6ff; }
        .stat-label { font-size: 12px; color: #8b949e; text-transform: uppercase; }
        .positive { color: #3fb950 !important; }
        .negative { color: #f85149 !important; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #30363d; }
        th { color: #8b949e; font-weight: 500; }
        .badge { padding: 2px 8px; border-radius: 12px; font-size: 12px; }
        .badge-buy { background: #238636; }
        .badge-sell { background: #da3633; }
        .badge-flash { background: #a371f7; }
        .badge-arb { background: #58a6ff; }
        .badge-value { background: #3fb950; }
        .badge-swing { background: #d29922; }
        .timestamp { color: #8b949e; font-size: 12px; }
        .refresh { color: #8b949e; font-size: 12px; float: right; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Polymarket Trading Dashboard <span class="refresh">Auto-refresh: 30s</span></h1>
        
        <div class="stats">
            <div class="card stat">
                <div class="stat-value">{{ stats.positions }}</div>
                <div class="stat-label">Open Positions</div>
            </div>
            <div class="card stat">
                <div class="stat-value">${{ "%.2f"|format(stats.total_value) }}</div>
                <div class="stat-label">Total Value</div>
            </div>
            <div class="card stat">
                <div class="stat-value {{ 'positive' if stats.total_pnl >= 0 else 'negative' }}">
                    ${{ "%+.2f"|format(stats.total_pnl) }}
                </div>
                <div class="stat-label">Total PnL</div>
            </div>
            <div class="card stat">
                <div class="stat-value">{{ stats.trades_today }}</div>
                <div class="stat-label">Trades Today</div>
            </div>
        </div>

        <h2>📈 Open Positions</h2>
        <div class="card">
            <table>
                <tr>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Shares</th>
                    <th>Value</th>
                    <th>PnL</th>
                </tr>
                {% for pos in positions %}
                <tr>
                    <td>{{ pos.title[:50] }}...</td>
                    <td><span class="badge badge-{{ pos.outcome|lower }}">{{ pos.outcome }}</span></td>
                    <td>{{ "%.2f"|format(pos.size) }}</td>
                    <td>${{ "%.2f"|format(pos.currentValue) }}</td>
                    <td class="{{ 'positive' if pos.cashPnl >= 0 else 'negative' }}">
                        ${{ "%+.2f"|format(pos.cashPnl) }}
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <h2>📊 Recent Trades</h2>
        <div class="card">
            <table>
                <tr>
                    <th>Time</th>
                    <th>Strategy</th>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Price</th>
                    <th>Size</th>
                </tr>
                {% for trade in trades %}
                <tr>
                    <td class="timestamp">{{ trade.time }}</td>
                    <td><span class="badge badge-{{ trade.strategy }}">{{ trade.strategy }}</span></td>
                    <td>{{ trade.market[:40] }}...</td>
                    <td><span class="badge badge-{{ trade.side|lower }}">{{ trade.side }}</span></td>
                    <td>{{ "%.2f"|format(trade.price * 100) }}¢</td>
                    <td>${{ "%.2f"|format(trade.size_usd) }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <h2>🎯 Strategy Performance</h2>
        <div class="stats">
            {% for strat, data in strategy_stats.items() %}
            <div class="card stat">
                <div class="stat-value">{{ data.trades }}</div>
                <div class="stat-label">{{ strat }} trades</div>
            </div>
            {% endfor %}
        </div>

        <p class="timestamp" style="margin-top: 30px;">
            Last updated: {{ now }} | Wallet: {{ wallet[:10] }}...{{ wallet[-6:] }}
        </p>
    </div>
</body>
</html>
"""


def get_positions():
    """Fetch current positions from Polymarket."""
    try:
        resp = requests.get(
            f"https://data-api.polymarket.com/positions?user={WALLET}",
            timeout=10
        )
        positions = resp.json()
        return [p for p in positions if p.get("currentValue", 0) > 0]
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []


def get_recent_trades(limit=20):
    """Get recent trades from tracking file."""
    trades = []
    trades_file = DATA_DIR / "trades.jsonl"
    
    if trades_file.exists():
        with open(trades_file) as f:
            lines = f.readlines()[-limit:]
            for line in reversed(lines):
                try:
                    trade = json.loads(line)
                    if trade.get("type") == "ENTRY":
                        trades.append({
                            "time": trade.get("timestamp", "")[:19].replace("T", " "),
                            "strategy": trade.get("strategy", "unknown"),
                            "market": trade.get("market", "unknown"),
                            "side": trade.get("side", "BUY"),
                            "price": trade.get("entry_price", 0),
                            "size_usd": trade.get("size_usd", 0),
                            "signals": trade.get("signals", {})
                        })
                except json.JSONDecodeError:
                    continue
    
    return trades


def get_strategy_stats():
    """Calculate stats per strategy."""
    stats = {}
    trades_file = DATA_DIR / "trades.jsonl"
    
    if trades_file.exists():
        with open(trades_file) as f:
            for line in f:
                try:
                    trade = json.loads(line)
                    if trade.get("type") == "ENTRY":
                        strat = trade.get("strategy", "unknown")
                        if strat not in stats:
                            stats[strat] = {"trades": 0, "volume": 0}
                        stats[strat]["trades"] += 1
                        stats[strat]["volume"] += trade.get("size_usd", 0)
                except json.JSONDecodeError:
                    continue
    
    return stats


@app.route("/")
def dashboard():
    """Main dashboard page."""
    positions = get_positions()
    trades = get_recent_trades()
    strategy_stats = get_strategy_stats()
    
    total_value = sum(p.get("currentValue", 0) for p in positions)
    total_pnl = sum(p.get("cashPnl", 0) for p in positions)
    
    stats = {
        "positions": len(positions),
        "total_value": total_value,
        "total_pnl": total_pnl,
        "trades_today": len(trades)
    }
    
    return render_template_string(
        HTML_TEMPLATE,
        stats=stats,
        positions=positions,
        trades=trades,
        strategy_stats=strategy_stats,
        wallet=WALLET,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )


@app.route("/api/stats")
def api_stats():
    """JSON API for stats."""
    positions = get_positions()
    return jsonify({
        "positions": len(positions),
        "total_value": sum(p.get("currentValue", 0) for p in positions),
        "total_pnl": sum(p.get("cashPnl", 0) for p in positions),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


if __name__ == "__main__":
    print("🚀 Dashboard starting at http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
