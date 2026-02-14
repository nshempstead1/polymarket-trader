"""
Risk Manager - Shared risk controls across all strategies

Enforces:
- Max position size per market
- Max total exposure across all markets
- Max concurrent positions
- Daily loss limit (circuit breaker)
- Per-trade size limits
- Cooldown between trades on same market

All strategies must check with the RiskManager before placing trades.
"""

import time
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """Risk management configuration."""

    # Per-trade limits
    min_trade_size: float = 5.0       # Min USDC per trade
    max_trade_size: float = 25.0      # Max USDC per trade
    default_trade_size: float = 10.0  # Default if not specified

    # Position limits
    max_positions: int = 10           # Max concurrent open positions
    max_per_market: float = 50.0      # Max USDC exposure per market
    max_total_exposure: float = 200.0 # Max USDC across all positions

    # Daily limits
    daily_loss_limit: float = 50.0    # Stop trading after this much loss in a day
    daily_trade_limit: int = 50       # Max trades per day

    # Cooldowns
    trade_cooldown: float = 30.0      # Seconds between trades on same market
    global_cooldown: float = 5.0      # Seconds between any trades

    # Price filters
    min_price: float = 0.05           # Don't buy below this (too risky)
    max_price: float = 0.95           # Don't buy above this (too little upside)
    min_liquidity: float = 1000.0     # Skip markets with less liquidity


@dataclass
class TrackedPosition:
    """A position tracked by the risk manager."""
    id: str
    strategy: str
    market_question: str
    condition_id: str
    token_id: str
    outcome: str
    side: str  # BUY or SELL
    entry_price: float
    size_shares: float
    size_usdc: float
    entry_time: float
    order_id: Optional[str] = None

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL."""
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.size_shares
        else:
            return (self.entry_price - current_price) * self.size_shares


class RiskManager:
    """
    Centralized risk management for all trading strategies.

    Every strategy must call check_trade() before placing orders
    and register_trade() after successful execution.
    """

    def __init__(self, config: Optional[RiskConfig] = None, state_file: str = "risk_state.json"):
        self.config = config or RiskConfig()
        self.state_file = state_file

        # Active positions
        self.positions: Dict[str, TrackedPosition] = {}

        # Daily tracking
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._day_start: float = self._get_day_start()

        # Cooldown tracking
        self._last_trade_time: float = 0.0
        self._last_trade_by_market: Dict[str, float] = {}

        # Circuit breaker
        self._halted: bool = False
        self._halt_reason: str = ""

        # Load persisted state
        self._load_state()

    def _get_day_start(self) -> float:
        """Get timestamp of start of current UTC day."""
        import datetime
        now = datetime.datetime.utcnow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    def _check_new_day(self):
        """Reset daily counters if new day."""
        current_day_start = self._get_day_start()
        if current_day_start > self._day_start:
            logger.info(f"New day - resetting daily counters (prev PnL: ${self._daily_pnl:+.2f}, trades: {self._daily_trades})")
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._day_start = current_day_start
            self._halted = False
            self._halt_reason = ""

    @property
    def total_exposure(self) -> float:
        """Total USDC exposure across all positions."""
        return sum(p.size_usdc for p in self.positions.values())

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def is_halted(self) -> bool:
        self._check_new_day()
        return self._halted

    def get_market_exposure(self, condition_id: str) -> float:
        """Get total USDC exposure for a specific market."""
        return sum(
            p.size_usdc for p in self.positions.values()
            if p.condition_id == condition_id
        )

    def check_trade(
        self,
        strategy: str,
        condition_id: str,
        token_id: str,
        price: float,
        size_usdc: float,
        side: str = "BUY",
    ) -> tuple[bool, str]:
        """
        Check if a trade is allowed under risk rules.

        Returns:
            (allowed, reason) - True if trade can proceed, else reason for rejection
        """
        self._check_new_day()

        # Circuit breaker
        if self._halted:
            return False, f"Trading halted: {self._halt_reason}"

        # Daily loss limit
        if self._daily_pnl <= -self.config.daily_loss_limit:
            self._halted = True
            self._halt_reason = f"Daily loss limit hit (${self._daily_pnl:.2f})"
            logger.warning(self._halt_reason)
            return False, self._halt_reason

        # Daily trade limit
        if self._daily_trades >= self.config.daily_trade_limit:
            return False, f"Daily trade limit reached ({self._daily_trades})"

        # Trade size limits
        if size_usdc < self.config.min_trade_size:
            return False, f"Trade too small (${size_usdc:.2f} < ${self.config.min_trade_size:.2f})"
        if size_usdc > self.config.max_trade_size:
            return False, f"Trade too large (${size_usdc:.2f} > ${self.config.max_trade_size:.2f})"

        # Price filters
        if price < self.config.min_price:
            return False, f"Price too low ({price:.4f} < {self.config.min_price})"
        if price > self.config.max_price:
            return False, f"Price too high ({price:.4f} > {self.config.max_price})"

        # Position limits
        if side == "BUY" and self.position_count >= self.config.max_positions:
            return False, f"Max positions reached ({self.position_count})"

        # Per-market exposure
        market_exposure = self.get_market_exposure(condition_id)
        if side == "BUY" and (market_exposure + size_usdc) > self.config.max_per_market:
            return False, f"Market exposure limit (${market_exposure:.2f} + ${size_usdc:.2f} > ${self.config.max_per_market:.2f})"

        # Total exposure
        if side == "BUY" and (self.total_exposure + size_usdc) > self.config.max_total_exposure:
            return False, f"Total exposure limit (${self.total_exposure:.2f} + ${size_usdc:.2f} > ${self.config.max_total_exposure:.2f})"

        # Global cooldown
        now = time.time()
        if (now - self._last_trade_time) < self.config.global_cooldown:
            remaining = self.config.global_cooldown - (now - self._last_trade_time)
            return False, f"Global cooldown ({remaining:.1f}s remaining)"

        # Per-market cooldown
        last_market_trade = self._last_trade_by_market.get(condition_id, 0)
        if (now - last_market_trade) < self.config.trade_cooldown:
            remaining = self.config.trade_cooldown - (now - last_market_trade)
            return False, f"Market cooldown ({remaining:.1f}s remaining)"

        return True, "OK"

    def register_trade(
        self,
        strategy: str,
        market_question: str,
        condition_id: str,
        token_id: str,
        outcome: str,
        side: str,
        price: float,
        size_shares: float,
        size_usdc: float,
        order_id: Optional[str] = None,
    ) -> str:
        """
        Register a successfully executed trade.

        Returns:
            Position ID
        """
        now = time.time()
        pos_id = f"{strategy[:3]}_{int(now)}_{len(self.positions)}"

        if side == "BUY":
            position = TrackedPosition(
                id=pos_id,
                strategy=strategy,
                market_question=market_question,
                condition_id=condition_id,
                token_id=token_id,
                outcome=outcome,
                side=side,
                entry_price=price,
                size_shares=size_shares,
                size_usdc=size_usdc,
                entry_time=now,
                order_id=order_id,
            )
            self.positions[pos_id] = position

        # Update tracking
        self._last_trade_time = now
        self._last_trade_by_market[condition_id] = now
        self._daily_trades += 1

        logger.info(
            f"[{strategy}] {side} {outcome} @ {price:.4f} "
            f"(${size_usdc:.2f}) - {market_question[:50]}"
        )

        self._save_state()
        return pos_id

    def close_position(self, position_id: str, exit_price: float, realized_pnl: float) -> Optional[TrackedPosition]:
        """Close a position and record PnL."""
        pos = self.positions.pop(position_id, None)
        if not pos:
            return None

        self._daily_pnl += realized_pnl
        self._daily_trades += 1

        pnl_str = f"${realized_pnl:+.2f}"
        logger.info(
            f"[{pos.strategy}] CLOSED {pos.outcome} @ {exit_price:.4f} "
            f"PnL: {pnl_str} - {pos.market_question[:50]}"
        )

        # Check if daily loss limit hit
        if self._daily_pnl <= -self.config.daily_loss_limit:
            self._halted = True
            self._halt_reason = f"Daily loss limit hit (${self._daily_pnl:.2f})"
            logger.warning(f"CIRCUIT BREAKER: {self._halt_reason}")

        self._save_state()
        return pos

    def get_status(self) -> Dict:
        """Get current risk status."""
        self._check_new_day()
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "positions": self.position_count,
            "max_positions": self.config.max_positions,
            "total_exposure": self.total_exposure,
            "max_exposure": self.config.max_total_exposure,
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": self.config.daily_loss_limit,
            "daily_trades": self._daily_trades,
            "daily_trade_limit": self.config.daily_trade_limit,
        }

    def get_all_positions(self) -> List[TrackedPosition]:
        return list(self.positions.values())

    def _save_state(self):
        """Persist state to disk."""
        try:
            state = {
                "positions": {
                    pid: {
                        "id": p.id,
                        "strategy": p.strategy,
                        "market_question": p.market_question,
                        "condition_id": p.condition_id,
                        "token_id": p.token_id,
                        "outcome": p.outcome,
                        "side": p.side,
                        "entry_price": p.entry_price,
                        "size_shares": p.size_shares,
                        "size_usdc": p.size_usdc,
                        "entry_time": p.entry_time,
                        "order_id": p.order_id,
                    }
                    for pid, p in self.positions.items()
                },
                "daily_pnl": self._daily_pnl,
                "daily_trades": self._daily_trades,
                "day_start": self._day_start,
                "halted": self._halted,
                "halt_reason": self._halt_reason,
            }
            Path(self.state_file).write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.error(f"Failed to save risk state: {e}")

    def _load_state(self):
        """Load persisted state."""
        try:
            path = Path(self.state_file)
            if not path.exists():
                return

            state = json.loads(path.read_text())

            # Restore positions
            for pid, pdata in state.get("positions", {}).items():
                self.positions[pid] = TrackedPosition(**pdata)

            # Restore daily counters (only if same day)
            saved_day = state.get("day_start", 0)
            if saved_day >= self._day_start:
                self._daily_pnl = state.get("daily_pnl", 0)
                self._daily_trades = state.get("daily_trades", 0)
                self._halted = state.get("halted", False)
                self._halt_reason = state.get("halt_reason", "")

            logger.info(f"Loaded risk state: {self.position_count} positions, daily PnL: ${self._daily_pnl:+.2f}")
        except Exception as e:
            logger.warning(f"Failed to load risk state: {e}")
