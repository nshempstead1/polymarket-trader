"""
Detailed Trade Logger - Captures full reasoning for each trade

Logs every decision with:
- All signals and their values
- Why the trade was/wasn't taken
- Market conditions at time of trade
- Expected vs actual outcomes (for learning)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

TRADE_LOG = DATA_DIR / "trade_reasoning.jsonl"
REVIEW_LOG = DATA_DIR / "review_flags.jsonl"


def log_trade_decision(
    strategy: str,
    market: str,
    token_id: str,
    action: str,  # BUY, SELL, PASS
    signals: Dict[str, Any],
    reasoning: str,
    market_state: Dict[str, Any] = None,
    confidence: float = 0.0,
    expected_edge: float = 0.0
):
    """
    Log a detailed trade decision with full reasoning.
    
    Args:
        strategy: Strategy name (flash_crash, arb, value, swing)
        market: Market name/question
        token_id: Token being traded
        action: BUY, SELL, or PASS
        signals: Dict of all signal values that informed decision
        reasoning: Human-readable explanation of why
        market_state: Current market conditions (prices, volume, etc)
        confidence: Model confidence 0-1
        expected_edge: Expected edge/profit percentage
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "market": market,
        "token_id": token_id[:20] + "..." if len(token_id) > 20 else token_id,
        "action": action,
        "signals": signals,
        "reasoning": reasoning,
        "market_state": market_state or {},
        "confidence": confidence,
        "expected_edge": expected_edge
    }
    
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    
    # Also log to standard logger for visibility
    if action != "PASS":
        logger.info(f"[{strategy}] {action} {market[:40]}... | {reasoning}")


def log_flash_crash_decision(
    market: str,
    token_id: str,
    action: str,
    coin: str,
    direction: str,
    prev_price: float,
    curr_price: float,
    drop_size: float,
    threshold: float,
    size_usd: float = 0,
    reason_pass: str = None
):
    """Log flash crash strategy decision with specific signals."""
    signals = {
        "coin": coin,
        "direction": direction,
        "prev_price": prev_price,
        "curr_price": curr_price,
        "drop_size": drop_size,
        "drop_pct": drop_size / prev_price if prev_price > 0 else 0,
        "threshold": threshold,
        "meets_threshold": drop_size >= threshold
    }
    
    if action == "BUY":
        reasoning = f"Flash crash detected: {coin} {direction} dropped {drop_size:.2f} ({signals['drop_pct']*100:.1f}%) from {prev_price:.2f} to {curr_price:.2f}. Threshold {threshold:.2f} met. Buying ${size_usd:.2f}."
    else:
        reasoning = reason_pass or f"Drop {drop_size:.2f} below threshold {threshold:.2f}"
    
    market_state = {
        "current_price": curr_price,
        "size_usd": size_usd
    }
    
    log_trade_decision(
        strategy="flash_crash",
        market=market,
        token_id=token_id,
        action=action,
        signals=signals,
        reasoning=reasoning,
        market_state=market_state,
        confidence=min(drop_size / threshold, 1.0) if threshold > 0 else 0,
        expected_edge=drop_size * 0.5  # Expect to capture ~50% of drop
    )


def log_arb_decision(
    market: str,
    token_id: str,
    action: str,
    event_sum: float,
    outcome_price: float,
    fair_value: float,
    edge: float,
    size_usd: float = 0,
    reason_pass: str = None
):
    """Log arbitrage strategy decision with specific signals."""
    signals = {
        "event_sum": event_sum,
        "outcome_price": outcome_price,
        "fair_value": fair_value,
        "edge": edge,
        "edge_pct": edge / outcome_price if outcome_price > 0 else 0,
        "mispricing": 1.0 - event_sum
    }
    
    if action == "BUY":
        reasoning = f"Arb opportunity: Event sum {event_sum:.2f} != 1.00. Buying at {outcome_price:.2f}, fair value {fair_value:.2f}. Edge: {edge*100:.1f}%"
    else:
        reasoning = reason_pass or f"Edge {edge*100:.1f}% below threshold"
    
    log_trade_decision(
        strategy="arb",
        market=market,
        token_id=token_id,
        action=action,
        signals=signals,
        reasoning=reasoning,
        confidence=min(abs(edge) * 10, 1.0),
        expected_edge=edge
    )


def log_value_decision(
    market: str,
    token_id: str,
    action: str,
    current_price: float,
    estimated_fair: float,
    volume_24h: float,
    liquidity: float,
    size_usd: float = 0,
    reason_pass: str = None
):
    """Log value strategy decision with specific signals."""
    edge = (estimated_fair - current_price) / current_price if current_price > 0 else 0
    
    signals = {
        "current_price": current_price,
        "estimated_fair": estimated_fair,
        "edge": edge,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "underpriced": current_price < estimated_fair
    }
    
    if action == "BUY":
        reasoning = f"Value play: Price {current_price:.2f} below fair value {estimated_fair:.2f}. Edge: {edge*100:.1f}%. Volume: ${volume_24h:.0f}"
    else:
        reasoning = reason_pass or f"No value edge found"
    
    log_trade_decision(
        strategy="value",
        market=market,
        token_id=token_id,
        action=action,
        signals=signals,
        reasoning=reasoning,
        confidence=min(abs(edge) * 5, 1.0),
        expected_edge=edge
    )


def log_swing_decision(
    market: str,
    token_id: str,
    action: str,
    current_price: float,
    price_30m_ago: float,
    price_change: float,
    threshold: float,
    size_usd: float = 0,
    reason_pass: str = None
):
    """Log swing strategy decision with specific signals."""
    signals = {
        "current_price": current_price,
        "price_30m_ago": price_30m_ago,
        "price_change": price_change,
        "change_pct": price_change / price_30m_ago if price_30m_ago > 0 else 0,
        "threshold": threshold,
        "meets_threshold": abs(price_change) >= threshold
    }
    
    if action == "BUY":
        reasoning = f"Swing trade: Price dropped {abs(price_change)*100:.1f}% in 30min ({price_30m_ago:.2f} → {current_price:.2f}). Buying dip."
    else:
        reasoning = reason_pass or f"Price change {price_change*100:.1f}% below threshold"
    
    log_trade_decision(
        strategy="swing",
        market=market,
        token_id=token_id,
        action=action,
        signals=signals,
        reasoning=reasoning,
        confidence=min(abs(price_change) / threshold, 1.0) if threshold > 0 else 0,
        expected_edge=abs(price_change) * 0.3
    )


def flag_for_review(
    issue_type: str,
    description: str,
    trade_data: Dict[str, Any] = None,
    severity: str = "warning"  # info, warning, error
):
    """Flag an issue for sub-agent review."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issue_type": issue_type,
        "description": description,
        "trade_data": trade_data or {},
        "severity": severity,
        "reviewed": False
    }
    
    with open(REVIEW_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    
    logger.warning(f"[REVIEW] {severity.upper()}: {issue_type} - {description}")


def get_recent_decisions(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent trade decisions for review."""
    decisions = []
    if TRADE_LOG.exists():
        with open(TRADE_LOG) as f:
            lines = f.readlines()[-limit:]
            for line in lines:
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return decisions


def get_review_flags(unreviewed_only: bool = True) -> List[Dict[str, Any]]:
    """Get flags that need review."""
    flags = []
    if REVIEW_LOG.exists():
        with open(REVIEW_LOG) as f:
            for line in f:
                try:
                    flag = json.loads(line)
                    if not unreviewed_only or not flag.get("reviewed"):
                        flags.append(flag)
                except json.JSONDecodeError:
                    continue
    return flags
