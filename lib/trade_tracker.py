"""
Trade Tracker - Structured logging for analysis and improvement

Logs:
- trades.jsonl: Every trade with entry/exit, PnL, signals
- decisions.jsonl: All signals/decisions (even non-trades)
- daily_summary.json: Daily aggregated stats
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class TradeTracker:
    """Track all trades and decisions for analysis."""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        self.trades_file = self.data_dir / "trades.jsonl"
        self.decisions_file = self.data_dir / "decisions.jsonl"
        self.summary_file = self.data_dir / "daily_summary.json"
        
        # In-memory stats for current session
        self.session_stats = {
            "started": datetime.now(timezone.utc).isoformat(),
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "signals_seen": 0,
            "signals_acted": 0
        }
        
        logger.info(f"TradeTracker initialized: {self.data_dir}")
    
    def log_decision(
        self,
        strategy: str,
        market: str,
        signal_type: str,
        signal_strength: float,
        action: str,  # "BUY", "SELL", "PASS"
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        """Log a trading decision (trade or pass)."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "market": market,
            "signal_type": signal_type,
            "signal_strength": signal_strength,
            "action": action,
            "reason": reason,
            "details": details or {}
        }
        
        self._append_jsonl(self.decisions_file, record)
        self.session_stats["signals_seen"] += 1
        if action != "PASS":
            self.session_stats["signals_acted"] += 1
    
    def log_trade(
        self,
        strategy: str,
        market: str,
        token_id: str,
        side: str,
        outcome: str,
        entry_price: float,
        size_usd: float,
        contracts: float,
        signals: Dict[str, Any],
        order_id: Optional[str] = None
    ):
        """Log a trade entry."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "ENTRY",
            "strategy": strategy,
            "market": market,
            "token_id": token_id,
            "side": side,
            "outcome": outcome,
            "entry_price": entry_price,
            "size_usd": size_usd,
            "contracts": contracts,
            "signals": signals,
            "order_id": order_id
        }
        
        self._append_jsonl(self.trades_file, record)
        self.session_stats["trades"] += 1
        logger.info(f"Trade logged: {side} {outcome} ${size_usd:.2f} @ {entry_price:.4f}")
    
    def log_exit(
        self,
        token_id: str,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        hold_time_minutes: float
    ):
        """Log a trade exit."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "EXIT",
            "token_id": token_id,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl": pnl,
            "hold_time_minutes": hold_time_minutes
        }
        
        self._append_jsonl(self.trades_file, record)
        self.session_stats["total_pnl"] += pnl
        if pnl > 0:
            self.session_stats["wins"] += 1
        else:
            self.session_stats["losses"] += 1
        
        logger.info(f"Exit logged: {exit_reason} PnL=${pnl:+.2f}")
    
    def get_session_stats(self) -> Dict[str, Any]:
        """Get current session statistics."""
        stats = self.session_stats.copy()
        if stats["trades"] > 0:
            stats["win_rate"] = stats["wins"] / stats["trades"]
        else:
            stats["win_rate"] = 0
        if stats["signals_seen"] > 0:
            stats["action_rate"] = stats["signals_acted"] / stats["signals_seen"]
        else:
            stats["action_rate"] = 0
        return stats
    
    def get_daily_summary(self) -> Dict[str, Any]:
        """Calculate daily summary from trades file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        trades = []
        if self.trades_file.exists():
            with open(self.trades_file) as f:
                for line in f:
                    try:
                        record = json.loads(line)
                        if record.get("timestamp", "").startswith(today):
                            trades.append(record)
                    except json.JSONDecodeError:
                        continue
        
        entries = [t for t in trades if t.get("type") == "ENTRY"]
        exits = [t for t in trades if t.get("type") == "EXIT"]
        
        return {
            "date": today,
            "total_entries": len(entries),
            "total_exits": len(exits),
            "total_pnl": sum(t.get("pnl", 0) for t in exits),
            "total_volume": sum(t.get("size_usd", 0) for t in entries),
            "by_strategy": self._group_by_strategy(entries, exits),
            "by_signal_type": self._group_by_signal(entries)
        }
    
    def _group_by_strategy(self, entries, exits) -> Dict[str, Any]:
        """Group stats by strategy."""
        strategies = {}
        for entry in entries:
            strat = entry.get("strategy", "unknown")
            if strat not in strategies:
                strategies[strat] = {"entries": 0, "volume": 0}
            strategies[strat]["entries"] += 1
            strategies[strat]["volume"] += entry.get("size_usd", 0)
        return strategies
    
    def _group_by_signal(self, entries) -> Dict[str, int]:
        """Count entries by signal type."""
        signals = {}
        for entry in entries:
            for sig_name in entry.get("signals", {}).keys():
                signals[sig_name] = signals.get(sig_name, 0) + 1
        return signals
    
    def _append_jsonl(self, filepath: Path, record: Dict[str, Any]):
        """Append a record to a JSONL file."""
        with open(filepath, "a") as f:
            f.write(json.dumps(record) + "\n")


# Global tracker instance
_tracker: Optional[TradeTracker] = None

def get_tracker() -> TradeTracker:
    """Get or create the global tracker."""
    global _tracker
    if _tracker is None:
        _tracker = TradeTracker()
    return _tracker
