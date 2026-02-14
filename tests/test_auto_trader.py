"""
Unit Tests for Auto Trader Daemon

Tests the SeenTracker, execute_buy/sell helpers, strategy initialization,
and CLI argument parsing.

Run with:
    pytest tests/test_auto_trader.py -v
"""

import pytest
import sys
import time
import asyncio
import logging
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))

from apps.auto_trader import (
    SeenTracker,
    ValueScanner,
    SwingTrader,
    EventArbitrage,
    FlashCrashMonitor,
    StatusReporter,
    execute_buy,
    execute_sell,
    DEFAULT_CATEGORIES,
    write_pid,
    remove_pid,
    PID_FILE,
)
from lib.risk_manager import RiskManager, RiskConfig, TrackedPosition
from lib.trade_journal import TradeJournal


# ========================================================================
# SeenTracker Tests
# ========================================================================

class TestSeenTracker:
    """Tests for the SeenTracker with TTL-based expiry."""

    def test_add_and_contains(self):
        tracker = SeenTracker(ttl=3600)
        tracker.add("abc")
        assert "abc" in tracker
        assert "xyz" not in tracker

    def test_len(self):
        tracker = SeenTracker(ttl=3600)
        tracker.add("a")
        tracker.add("b")
        tracker.add("c")
        assert len(tracker) == 3

    def test_expiry(self):
        tracker = SeenTracker(ttl=1.0)
        tracker.add("abc")
        assert "abc" in tracker

        # Simulate time passing beyond TTL
        tracker._entries["abc"] = time.time() - 2.0
        assert "abc" not in tracker

    def test_prune_removes_expired(self):
        tracker = SeenTracker(ttl=1.0)
        tracker.add("a")
        tracker.add("b")
        # Expire both
        now = time.time()
        tracker._entries["a"] = now - 2.0
        tracker._entries["b"] = now - 2.0
        tracker._prune()
        assert len(tracker._entries) == 0

    def test_mixed_expiry(self):
        tracker = SeenTracker(ttl=1.0)
        tracker.add("fresh")
        tracker.add("stale")
        tracker._entries["stale"] = time.time() - 2.0

        assert "fresh" in tracker
        assert "stale" not in tracker
        assert len(tracker) == 1

    def test_re_add_resets_ttl(self):
        tracker = SeenTracker(ttl=1.0)
        tracker.add("abc")
        tracker._entries["abc"] = time.time() - 0.9  # Almost expired
        tracker.add("abc")  # Re-add resets TTL
        assert "abc" in tracker


# ========================================================================
# Execute Buy Tests
# ========================================================================

class TestExecuteBuy:
    """Tests for the execute_buy helper function."""

    def _make_mocks(self, trade_allowed=True, dry_run=False):
        bot = AsyncMock()
        bot.place_order = AsyncMock(return_value=Mock(success=True, order_id="ord_123"))

        risk = Mock()
        risk.config = RiskConfig(default_trade_size=10.0)
        risk.check_trade = Mock(return_value=(trade_allowed, "OK" if trade_allowed else "Max positions reached"))
        risk.register_trade = Mock(return_value="pos_001")

        journal = Mock()
        journal.log_decision = Mock(return_value=1)
        journal.log_snapshot = Mock()
        journal.log_trade = Mock()
        journal.open_position = Mock()

        log = logging.getLogger("test")

        market = {
            "condition_id": "cid_123",
            "question": "Will X happen by end of month?",
            "liquidity": 10000,
            "volume_24h": 5000,
        }

        return bot, risk, journal, log, market

    @pytest.mark.asyncio
    async def test_buy_rejected_by_risk(self):
        bot, risk, journal, log, market = self._make_mocks(trade_allowed=False)
        result = await execute_buy(bot, risk, journal, "value_scanner", market, "yes", "tid_1", 0.25, {"spread": 0.02}, False, log)
        assert result is False
        journal.log_decision.assert_called_once()
        assert journal.log_decision.call_args.kwargs["result"] == "rejected"
        bot.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_buy_dry_run(self):
        bot, risk, journal, log, market = self._make_mocks(trade_allowed=True, dry_run=True)
        result = await execute_buy(bot, risk, journal, "value_scanner", market, "yes", "tid_1", 0.25, {"spread": 0.02}, True, log)
        assert result is True
        bot.place_order.assert_not_called()
        journal.log_decision.assert_called_once()
        assert journal.log_decision.call_args.kwargs["result"] == "dry_run"

    @pytest.mark.asyncio
    async def test_buy_executed_success(self):
        bot, risk, journal, log, market = self._make_mocks(trade_allowed=True)
        result = await execute_buy(bot, risk, journal, "value_scanner", market, "yes", "tid_1", 0.25, {"spread": 0.02}, False, log)
        assert result is True
        bot.place_order.assert_called_once()
        risk.register_trade.assert_called_once()
        journal.log_trade.assert_called_once()
        journal.open_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_buy_executed_failure(self):
        bot, risk, journal, log, market = self._make_mocks(trade_allowed=True)
        bot.place_order = AsyncMock(return_value=Mock(success=False, message="Insufficient balance"))
        result = await execute_buy(bot, risk, journal, "value_scanner", market, "yes", "tid_1", 0.25, {"spread": 0.02}, False, log)
        assert result is False
        risk.register_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_buy_price_capped_at_095(self):
        bot, risk, journal, log, market = self._make_mocks(trade_allowed=True)
        await execute_buy(bot, risk, journal, "value_scanner", market, "yes", "tid_1", 0.94, {"spread": 0.02}, False, log)
        call_args = bot.place_order.call_args
        assert call_args.kwargs["price"] == 0.95


# ========================================================================
# Execute Sell Tests
# ========================================================================

class TestExecuteSell:
    """Tests for the execute_sell helper function."""

    @pytest.mark.asyncio
    async def test_sell_dry_run(self):
        bot = AsyncMock()
        risk = Mock()
        risk.close_position = Mock()
        journal = Mock()
        journal.close_position = Mock()
        journal.log_trade = Mock()
        log = logging.getLogger("test")

        pos = TrackedPosition(
            id="pos_1", strategy="value_scanner", market_question="Test?",
            condition_id="cid_1", token_id="tid_1", outcome="yes",
            side="BUY", entry_price=0.25, size_shares=40.0,
            size_usdc=10.0, entry_time=time.time() - 3600,
        )

        await execute_sell(bot, risk, journal, pos, 0.40, "take_profit", True, log)
        bot.place_order.assert_not_called()
        risk.close_position.assert_called_once()
        journal.close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_live_success(self):
        bot = AsyncMock()
        bot.place_order = AsyncMock(return_value=Mock(success=True, order_id="sell_123"))
        risk = Mock()
        risk.close_position = Mock()
        journal = Mock()
        journal.close_position = Mock()
        journal.log_trade = Mock()
        log = logging.getLogger("test")

        pos = TrackedPosition(
            id="pos_1", strategy="value_scanner", market_question="Test?",
            condition_id="cid_1", token_id="tid_1", outcome="yes",
            side="BUY", entry_price=0.25, size_shares=40.0,
            size_usdc=10.0, entry_time=time.time() - 3600,
        )

        await execute_sell(bot, risk, journal, pos, 0.40, "take_profit", False, log)
        bot.place_order.assert_called_once()
        risk.close_position.assert_called_once()
        journal.close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_live_failure_aborts(self):
        bot = AsyncMock()
        bot.place_order = AsyncMock(return_value=Mock(success=False, message="No liquidity"))
        risk = Mock()
        risk.close_position = Mock()
        journal = Mock()
        journal.close_position = Mock()
        log = logging.getLogger("test")

        pos = TrackedPosition(
            id="pos_1", strategy="value_scanner", market_question="Test?",
            condition_id="cid_1", token_id="tid_1", outcome="yes",
            side="BUY", entry_price=0.25, size_shares=40.0,
            size_usdc=10.0, entry_time=time.time() - 3600,
        )

        await execute_sell(bot, risk, journal, pos, 0.40, "take_profit", False, log)
        # On sell failure, position is NOT closed
        risk.close_position.assert_not_called()
        journal.close_position.assert_not_called()


# ========================================================================
# Strategy Init Tests
# ========================================================================

class TestValueScannerInit:
    """Tests for ValueScanner initialization and configuration."""

    def test_default_categories(self):
        scanner = ValueScanner(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock()
        )
        assert scanner.categories == DEFAULT_CATEGORIES
        assert scanner.scan_interval == 300
        assert scanner.tp == 0.15
        assert scanner.sl == 0.10

    def test_custom_categories(self):
        cats = ["politics", "sports"]
        scanner = ValueScanner(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock(),
            categories=cats,
        )
        assert scanner.categories == cats

    def test_dry_run_flag(self):
        scanner = ValueScanner(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock(),
            dry_run=True,
        )
        assert scanner.dry_run is True

    def test_seen_tracker_is_expiring(self):
        scanner = ValueScanner(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock()
        )
        assert isinstance(scanner._seen, SeenTracker)
        assert scanner._seen.ttl == 3600


class TestSwingTraderInit:
    """Tests for SwingTrader initialization."""

    def test_default_categories(self):
        trader = SwingTrader(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock()
        )
        assert trader.categories == DEFAULT_CATEGORIES
        assert trader.interval == 60
        assert trader.thresh == 0.08

    def test_custom_categories(self):
        cats = ["entertainment"]
        trader = SwingTrader(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock(),
            categories=cats,
        )
        assert trader.categories == cats


class TestEventArbitrageInit:
    """Tests for EventArbitrage initialization."""

    def test_defaults(self):
        arb = EventArbitrage(
            bot=Mock(), risk=Mock(), search=Mock(), journal=Mock()
        )
        assert arb.interval == 120
        assert arb.min_mis == 0.05


# ========================================================================
# PID File Tests
# ========================================================================

class TestPidFile:
    """Tests for PID file management."""

    def test_write_and_remove_pid(self, tmp_path):
        pid_path = tmp_path / "test.pid"
        with patch("apps.auto_trader.PID_FILE", str(pid_path)):
            from apps.auto_trader import write_pid as wp, remove_pid as rp
            # Patch the module-level PID_FILE used by the functions
            import apps.auto_trader as at
            original = at.PID_FILE
            at.PID_FILE = str(pid_path)
            try:
                wp()
                assert pid_path.exists()
                content = pid_path.read_text()
                assert content == str(__import__("os").getpid())
                rp()
                assert not pid_path.exists()
            finally:
                at.PID_FILE = original


# ========================================================================
# DEFAULT_CATEGORIES Tests
# ========================================================================

class TestDefaultCategories:
    """Tests for the default category list."""

    def test_categories_non_empty(self):
        assert len(DEFAULT_CATEGORIES) > 0

    def test_includes_key_categories(self):
        assert "politics" in DEFAULT_CATEGORIES
        assert "sports" in DEFAULT_CATEGORIES

    def test_all_strings(self):
        for cat in DEFAULT_CATEGORIES:
            assert isinstance(cat, str)
            assert len(cat) > 0
