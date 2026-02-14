"""
Trade Journal - Complete Decision and Trade Tracking

Logs everything to SQLite for analysis:
- Every trade decision (why it traded, what signals triggered it)
- Every order placed (price, size, side, result)
- Every position opened and closed (entry, exit, PnL, hold time)
- Market snapshots at time of decision (prices, spreads, depth)
- Strategy performance metrics over time
- Rejected trades (why risk manager said no)

Usage:
    from lib.trade_journal import TradeJournal

    journal = TradeJournal("data/trades.db")

    # Log a decision
    journal.log_decision(
        strategy="value_scanner",
        action="BUY",
        market_question="Will X happen?",
        outcome="yes",
        signals={"price": 0.30, "liquidity": 15000, "spread": 0.02},
        result="executed",
    )

    # Log a trade
    journal.log_trade(...)

    # Get analytics
    stats = journal.get_strategy_stats("value_scanner")
"""

import sqlite3
import json
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class TradeJournal:
    """
    SQLite-backed trade journal for complete audit trail and analytics.

    Tables:
    - decisions: Every signal evaluation (including rejections)
    - trades: Every order placed
    - positions: Position lifecycle (open → close with PnL)
    - snapshots: Market state at time of decision
    - daily_stats: Aggregated daily performance
    """

    def __init__(self, db_path: str = "data/trades.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        """Thread-safe connection context manager."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    datetime TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    action TEXT NOT NULL,
                    market_question TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    signals TEXT,
                    result TEXT NOT NULL,
                    rejection_reason TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    datetime TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    decision_id INTEGER,
                    market_question TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size_shares REAL NOT NULL,
                    size_usdc REAL NOT NULL,
                    order_id TEXT,
                    order_status TEXT,
                    fill_price REAL,
                    fees REAL DEFAULT 0,
                    FOREIGN KEY (decision_id) REFERENCES decisions(id)
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    market_question TEXT,
                    condition_id TEXT,
                    token_id TEXT,
                    outcome TEXT,
                    entry_price REAL NOT NULL,
                    entry_time REAL NOT NULL,
                    entry_datetime TEXT NOT NULL,
                    size_shares REAL NOT NULL,
                    size_usdc REAL NOT NULL,
                    exit_price REAL,
                    exit_time REAL,
                    exit_datetime TEXT,
                    exit_reason TEXT,
                    realized_pnl REAL,
                    hold_time_seconds REAL,
                    status TEXT DEFAULT 'open',
                    entry_order_id TEXT,
                    exit_order_id TEXT,
                    entry_signals TEXT,
                    peak_price REAL,
                    trough_price REAL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    decision_id INTEGER,
                    token_id TEXT,
                    mid_price REAL,
                    best_bid REAL,
                    best_ask REAL,
                    spread REAL,
                    bid_depth_5 REAL,
                    ask_depth_5 REAL,
                    volume_24h REAL,
                    liquidity REAL,
                    FOREIGN KEY (decision_id) REFERENCES decisions(id)
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    gross_profit REAL DEFAULT 0,
                    gross_loss REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0,
                    avg_hold_time REAL DEFAULT 0,
                    best_trade REAL DEFAULT 0,
                    worst_trade REAL DEFAULT 0,
                    strategies_used TEXT,
                    total_volume REAL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON decisions(strategy);
                CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
                CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy);
            """)

    def _now(self) -> tuple:
        """Get current timestamp and ISO datetime."""
        ts = time.time()
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return ts, dt

    # ========================================================================
    # Logging Methods
    # ========================================================================

    def log_decision(
        self,
        strategy: str,
        action: str,
        result: str,
        market_question: str = "",
        condition_id: str = "",
        token_id: str = "",
        outcome: str = "",
        signals: Optional[Dict[str, Any]] = None,
        rejection_reason: str = "",
        notes: str = "",
    ) -> int:
        """
        Log a trading decision.

        Args:
            strategy: Strategy name
            action: What it wanted to do (BUY, SELL, HOLD, SKIP)
            result: What happened (executed, rejected, failed, skipped)
            signals: The data/signals that led to this decision
            rejection_reason: Why risk manager rejected (if applicable)

        Returns:
            Decision ID
        """
        ts, dt = self._now()
        signals_json = json.dumps(signals) if signals else None

        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO decisions
                (timestamp, datetime, strategy, action, market_question,
                 condition_id, token_id, outcome, signals, result,
                 rejection_reason, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, dt, strategy, action, market_question,
                  condition_id, token_id, outcome, signals_json,
                  result, rejection_reason, notes))
            return cursor.lastrowid

    def log_trade(
        self,
        strategy: str,
        side: str,
        price: float,
        size_shares: float,
        size_usdc: float,
        market_question: str = "",
        condition_id: str = "",
        token_id: str = "",
        outcome: str = "",
        order_id: str = "",
        order_status: str = "placed",
        decision_id: Optional[int] = None,
    ) -> int:
        """Log an executed trade."""
        ts, dt = self._now()

        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO trades
                (timestamp, datetime, strategy, decision_id, market_question,
                 condition_id, token_id, outcome, side, price,
                 size_shares, size_usdc, order_id, order_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, dt, strategy, decision_id, market_question,
                  condition_id, token_id, outcome, side, price,
                  size_shares, size_usdc, order_id, order_status))
            return cursor.lastrowid

    def log_snapshot(
        self,
        token_id: str,
        mid_price: float,
        best_bid: float = 0,
        best_ask: float = 0,
        spread: float = 0,
        bid_depth_5: float = 0,
        ask_depth_5: float = 0,
        volume_24h: float = 0,
        liquidity: float = 0,
        decision_id: Optional[int] = None,
    ):
        """Log a market snapshot."""
        ts = time.time()

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO snapshots
                (timestamp, decision_id, token_id, mid_price, best_bid,
                 best_ask, spread, bid_depth_5, ask_depth_5, volume_24h, liquidity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, decision_id, token_id, mid_price, best_bid,
                  best_ask, spread, bid_depth_5, ask_depth_5, volume_24h, liquidity))

    def open_position(
        self,
        position_id: str,
        strategy: str,
        entry_price: float,
        size_shares: float,
        size_usdc: float,
        market_question: str = "",
        condition_id: str = "",
        token_id: str = "",
        outcome: str = "",
        entry_order_id: str = "",
        entry_signals: Optional[Dict] = None,
    ):
        """Record a new position opened."""
        ts, dt = self._now()
        signals_json = json.dumps(entry_signals) if entry_signals else None

        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO positions
                (id, strategy, market_question, condition_id, token_id,
                 outcome, entry_price, entry_time, entry_datetime,
                 size_shares, size_usdc, status, entry_order_id,
                 entry_signals, peak_price, trough_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """, (position_id, strategy, market_question, condition_id,
                  token_id, outcome, entry_price, ts, dt,
                  size_shares, size_usdc, entry_order_id,
                  signals_json, entry_price, entry_price))

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        realized_pnl: float,
        exit_reason: str = "",
        exit_order_id: str = "",
    ):
        """Record a position closed."""
        ts, dt = self._now()

        with self._conn() as conn:
            # Get entry time for hold time calculation
            row = conn.execute(
                "SELECT entry_time FROM positions WHERE id = ?",
                (position_id,)
            ).fetchone()

            hold_time = (ts - row["entry_time"]) if row else 0

            conn.execute("""
                UPDATE positions SET
                    exit_price = ?,
                    exit_time = ?,
                    exit_datetime = ?,
                    exit_reason = ?,
                    realized_pnl = ?,
                    hold_time_seconds = ?,
                    status = 'closed',
                    exit_order_id = ?
                WHERE id = ?
            """, (exit_price, ts, dt, exit_reason, realized_pnl,
                  hold_time, exit_order_id, position_id))

            # Update daily stats
            self._update_daily_stats(conn, realized_pnl)

    def update_position_extremes(self, position_id: str, current_price: float):
        """Track peak and trough prices for a position (for drawdown analysis)."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE positions SET
                    peak_price = MAX(COALESCE(peak_price, 0), ?),
                    trough_price = MIN(COALESCE(trough_price, 999), ?)
                WHERE id = ? AND status = 'open'
            """, (current_price, current_price, position_id))

    def _update_daily_stats(self, conn, pnl: float):
        """Update aggregated daily stats."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        existing = conn.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        ).fetchone()

        if existing:
            total_trades = existing["total_trades"] + 1
            winning = existing["winning_trades"] + (1 if pnl > 0 else 0)
            losing = existing["losing_trades"] + (1 if pnl < 0 else 0)
            total_pnl = existing["total_pnl"] + pnl
            gross_profit = existing["gross_profit"] + (pnl if pnl > 0 else 0)
            gross_loss = existing["gross_loss"] + (pnl if pnl < 0 else 0)
            best = max(existing["best_trade"], pnl)
            worst = min(existing["worst_trade"], pnl)

            conn.execute("""
                UPDATE daily_stats SET
                    total_trades = ?, winning_trades = ?, losing_trades = ?,
                    total_pnl = ?, gross_profit = ?, gross_loss = ?,
                    best_trade = ?, worst_trade = ?
                WHERE date = ?
            """, (total_trades, winning, losing, total_pnl,
                  gross_profit, gross_loss, best, worst, today))
        else:
            conn.execute("""
                INSERT INTO daily_stats
                (date, total_trades, winning_trades, losing_trades,
                 total_pnl, gross_profit, gross_loss, best_trade, worst_trade)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
            """, (today,
                  1 if pnl > 0 else 0,
                  1 if pnl < 0 else 0,
                  pnl,
                  pnl if pnl > 0 else 0,
                  pnl if pnl < 0 else 0,
                  pnl, pnl))

    # ========================================================================
    # Query Methods
    # ========================================================================

    def get_strategy_stats(self, strategy: str = None, days: int = 30) -> Dict[str, Any]:
        """Get performance stats for a strategy (or all strategies)."""
        cutoff = time.time() - (days * 86400)

        with self._conn() as conn:
            if strategy:
                rows = conn.execute("""
                    SELECT * FROM positions
                    WHERE strategy = ? AND status = 'closed' AND entry_time > ?
                """, (strategy, cutoff)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM positions
                    WHERE status = 'closed' AND entry_time > ?
                """, (cutoff,)).fetchall()

            if not rows:
                return {"trades": 0, "pnl": 0, "win_rate": 0}

            pnls = [r["realized_pnl"] for r in rows if r["realized_pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            hold_times = [r["hold_time_seconds"] for r in rows if r["hold_time_seconds"]]

            return {
                "trades": len(pnls),
                "winning": len(wins),
                "losing": len(losses),
                "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
                "total_pnl": sum(pnls),
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
                "best_trade": max(pnls) if pnls else 0,
                "worst_trade": min(pnls) if pnls else 0,
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": sum(losses) / len(losses) if losses else 0,
                "profit_factor": abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf'),
                "avg_hold_minutes": (sum(hold_times) / len(hold_times) / 60) if hold_times else 0,
                "max_hold_minutes": (max(hold_times) / 60) if hold_times else 0,
            }

    def get_recent_trades(self, limit: int = 20, strategy: str = None) -> List[Dict]:
        """Get recent closed positions."""
        with self._conn() as conn:
            if strategy:
                rows = conn.execute("""
                    SELECT * FROM positions
                    WHERE status = 'closed' AND strategy = ?
                    ORDER BY exit_time DESC LIMIT ?
                """, (strategy, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM positions WHERE status = 'closed'
                    ORDER BY exit_time DESC LIMIT ?
                """, (limit,)).fetchall()

            return [dict(r) for r in rows]

    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_daily_stats(self, days: int = 30) -> List[Dict]:
        """Get daily performance stats."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_stats
                ORDER BY date DESC LIMIT ?
            """, (days,)).fetchall()
            return [dict(r) for r in rows]

    def get_decision_log(self, strategy: str = None, limit: int = 50) -> List[Dict]:
        """Get recent decisions with signals."""
        with self._conn() as conn:
            if strategy:
                rows = conn.execute("""
                    SELECT * FROM decisions
                    WHERE strategy = ?
                    ORDER BY timestamp DESC LIMIT ?
                """, (strategy, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM decisions
                    ORDER BY timestamp DESC LIMIT ?
                """, (limit,)).fetchall()

            results = []
            for r in rows:
                d = dict(r)
                if d.get("signals"):
                    d["signals"] = json.loads(d["signals"])
                results.append(d)
            return results

    def get_rejection_stats(self, days: int = 7) -> Dict[str, int]:
        """Get breakdown of why trades were rejected."""
        cutoff = time.time() - (days * 86400)

        with self._conn() as conn:
            rows = conn.execute("""
                SELECT rejection_reason, COUNT(*) as count
                FROM decisions
                WHERE result = 'rejected' AND timestamp > ?
                GROUP BY rejection_reason
                ORDER BY count DESC
            """, (cutoff,)).fetchall()

            return {r["rejection_reason"]: r["count"] for r in rows}

    def get_equity_curve(self, days: int = 30) -> List[Dict]:
        """Get cumulative PnL over time for equity curve plotting."""
        cutoff = time.time() - (days * 86400)

        with self._conn() as conn:
            rows = conn.execute("""
                SELECT exit_time, exit_datetime, realized_pnl, strategy
                FROM positions
                WHERE status = 'closed' AND exit_time > ?
                ORDER BY exit_time ASC
            """, (cutoff,)).fetchall()

            cumulative = 0
            curve = []
            for r in rows:
                cumulative += r["realized_pnl"]
                curve.append({
                    "timestamp": r["exit_time"],
                    "datetime": r["exit_datetime"],
                    "pnl": r["realized_pnl"],
                    "cumulative_pnl": cumulative,
                    "strategy": r["strategy"],
                })
            return curve

    def get_strategy_comparison(self, days: int = 30) -> Dict[str, Dict]:
        """Compare performance across strategies."""
        with self._conn() as conn:
            strategies = conn.execute("""
                SELECT DISTINCT strategy FROM positions WHERE status = 'closed'
            """).fetchall()

            comparison = {}
            for row in strategies:
                s = row["strategy"]
                comparison[s] = self.get_strategy_stats(s, days)

            return comparison

    def export_csv(self, filepath: str = "data/trades_export.csv"):
        """Export all closed positions to CSV for external analysis."""
        import csv

        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE status = 'closed'
                ORDER BY exit_time ASC
            """).fetchall()

            if not rows:
                logger.info("No closed positions to export")
                return

            Path(filepath).parent.mkdir(parents=True, exist_ok=True)

            with open(filepath, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                for r in rows:
                    writer.writerow(dict(r))

            logger.info(f"Exported {len(rows)} trades to {filepath}")
