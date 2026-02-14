#!/usr/bin/env python3
"""
Trade Review Script - For sub-agent monitoring

Run hourly to:
1. Review recent trades and their reasoning
2. Check for anomalies or issues
3. Calculate performance metrics
4. Generate alerts if needed

Output: Markdown report suitable for Telegram/logging
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List

DATA_DIR = Path("data")
TRADE_LOG = DATA_DIR / "trade_reasoning.jsonl"
TRADES_FILE = DATA_DIR / "trades.jsonl"
REVIEW_LOG = DATA_DIR / "review_flags.jsonl"


def load_recent_decisions(hours: int = 1) -> List[Dict[str, Any]]:
    """Load decisions from the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    decisions = []
    
    if TRADE_LOG.exists():
        with open(TRADE_LOG) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
                    if ts > cutoff:
                        decisions.append(d)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    
    return decisions


def load_recent_trades(hours: int = 24) -> List[Dict[str, Any]]:
    """Load actual trades from the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    trades = []
    
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            for line in f:
                try:
                    t = json.loads(line)
                    ts = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
                    if ts > cutoff:
                        trades.append(t)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    
    return trades


def analyze_decisions(decisions: List[Dict]) -> Dict[str, Any]:
    """Analyze decision patterns."""
    if not decisions:
        return {"total": 0, "by_strategy": {}, "by_action": {}}
    
    by_strategy = {}
    by_action = {"BUY": 0, "SELL": 0, "PASS": 0}
    confidence_sum = 0
    edge_sum = 0
    
    for d in decisions:
        strat = d.get("strategy", "unknown")
        action = d.get("action", "PASS")
        
        by_strategy[strat] = by_strategy.get(strat, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        confidence_sum += d.get("confidence", 0)
        edge_sum += d.get("expected_edge", 0)
    
    return {
        "total": len(decisions),
        "by_strategy": by_strategy,
        "by_action": by_action,
        "avg_confidence": confidence_sum / len(decisions),
        "avg_expected_edge": edge_sum / len(decisions)
    }


def check_for_issues(decisions: List[Dict], trades: List[Dict]) -> List[Dict]:
    """Check for potential issues that need review."""
    issues = []
    
    # Issue 1: High pass rate (not finding opportunities)
    if decisions:
        pass_rate = sum(1 for d in decisions if d.get("action") == "PASS") / len(decisions)
        if pass_rate > 0.99:
            issues.append({
                "type": "high_pass_rate",
                "severity": "warning",
                "description": f"Pass rate is {pass_rate*100:.1f}% - may be too conservative or no opportunities"
            })
    
    # Issue 2: Repeated failures on same market
    failures = {}
    for d in decisions:
        if "error" in d.get("reasoning", "").lower() or "fail" in d.get("reasoning", "").lower():
            market = d.get("market", "unknown")[:30]
            failures[market] = failures.get(market, 0) + 1
    
    for market, count in failures.items():
        if count >= 3:
            issues.append({
                "type": "repeated_failures",
                "severity": "error",
                "description": f"Market '{market}' failed {count} times"
            })
    
    # Issue 3: No trades in extended period
    entries = [t for t in trades if t.get("type") == "ENTRY"]
    if len(entries) == 0 and len(decisions) > 10:
        issues.append({
            "type": "no_trades",
            "severity": "info",
            "description": "No trades executed despite signals being evaluated"
        })
    
    # Issue 4: Low confidence trades
    low_conf = [d for d in decisions if d.get("action") != "PASS" and d.get("confidence", 1) < 0.3]
    if len(low_conf) > 3:
        issues.append({
            "type": "low_confidence_trades",
            "severity": "warning", 
            "description": f"{len(low_conf)} trades with confidence < 30%"
        })
    
    return issues


def calculate_performance(trades: List[Dict]) -> Dict[str, Any]:
    """Calculate performance metrics."""
    entries = [t for t in trades if t.get("type") == "ENTRY"]
    exits = [t for t in trades if t.get("type") == "EXIT"]
    
    total_volume = sum(t.get("size_usd", 0) for t in entries)
    total_pnl = sum(t.get("pnl", 0) for t in exits)
    
    wins = sum(1 for t in exits if t.get("pnl", 0) > 0)
    losses = sum(1 for t in exits if t.get("pnl", 0) <= 0)
    
    by_strategy = {}
    for t in entries:
        strat = t.get("strategy", "unknown")
        if strat not in by_strategy:
            by_strategy[strat] = {"trades": 0, "volume": 0}
        by_strategy[strat]["trades"] += 1
        by_strategy[strat]["volume"] += t.get("size_usd", 0)
    
    return {
        "total_entries": len(entries),
        "total_exits": len(exits),
        "total_volume": total_volume,
        "total_pnl": total_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / (wins + losses) if (wins + losses) > 0 else 0,
        "by_strategy": by_strategy
    }


def generate_report(hours: int = 1) -> str:
    """Generate a markdown report for review."""
    decisions = load_recent_decisions(hours)
    trades = load_recent_trades(24)  # 24h for performance context
    
    analysis = analyze_decisions(decisions)
    issues = check_for_issues(decisions, trades)
    performance = calculate_performance(trades)
    
    report = []
    report.append(f"# 🤖 Trade Review Report")
    report.append(f"**Period:** Last {hours} hour(s)")
    report.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    report.append("")
    
    # Summary
    report.append("## 📊 Summary")
    report.append(f"- **Decisions evaluated:** {analysis['total']}")
    report.append(f"- **Trades executed:** {analysis['by_action'].get('BUY', 0) + analysis['by_action'].get('SELL', 0)}")
    report.append(f"- **Avg confidence:** {analysis.get('avg_confidence', 0)*100:.1f}%")
    report.append(f"- **Avg expected edge:** {analysis.get('avg_expected_edge', 0)*100:.2f}%")
    report.append("")
    
    # By Strategy
    report.append("## 🎯 By Strategy")
    for strat, count in analysis["by_strategy"].items():
        report.append(f"- **{strat}:** {count} decisions")
    report.append("")
    
    # Performance (24h)
    report.append("## 📈 Performance (24h)")
    report.append(f"- **Total trades:** {performance['total_entries']}")
    report.append(f"- **Volume:** ${performance['total_volume']:.2f}")
    report.append(f"- **PnL:** ${performance['total_pnl']:+.2f}")
    report.append(f"- **Win rate:** {performance['win_rate']*100:.1f}%")
    report.append("")
    
    # Issues
    if issues:
        report.append("## ⚠️ Issues Detected")
        for issue in issues:
            icon = "🔴" if issue["severity"] == "error" else "🟡" if issue["severity"] == "warning" else "🔵"
            report.append(f"- {icon} **{issue['type']}:** {issue['description']}")
        report.append("")
    else:
        report.append("## ✅ No Issues Detected")
        report.append("")
    
    # Recent Trade Reasoning (last 5)
    report.append("## 📝 Recent Trade Reasoning")
    recent_buys = [d for d in decisions if d.get("action") == "BUY"][-5:]
    if recent_buys:
        for d in recent_buys:
            report.append(f"**{d.get('strategy', '?')}** @ {d.get('timestamp', '?')[:19]}")
            report.append(f"> {d.get('reasoning', 'No reasoning logged')}")
            report.append("")
    else:
        report.append("*No recent BUY decisions*")
        report.append("")
    
    return "\n".join(report)


if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(generate_report(hours))
