"""
Order executor using py_clob_client directly.
Bypasses custom client for reliable order execution.
"""

import os
import logging
from typing import Optional, Dict, Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from lib.trade_tracker import get_tracker

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Execute orders using py_clob_client."""
    
    def __init__(self, private_key: str, api_creds: Dict[str, str], dry_run: bool = False):
        self.dry_run = dry_run
        self.private_key = private_key
        
        # Create client
        self.client = ClobClient(
            host='https://clob.polymarket.com',
            chain_id=137,
            key=private_key,
            signature_type=0
        )
        
        # Set API credentials
        creds = ApiCreds(
            api_key=api_creds['api_key'],
            api_secret=api_creds['secret'],
            api_passphrase=api_creds['passphrase']
        )
        self.client.set_api_creds(creds)
        logger.info("OrderExecutor initialized with py_clob_client")
    
    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
        strategy: str = "unknown",
        market: str = "unknown",
        outcome: str = "unknown",
        signals: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Place an order.
        
        Args:
            token_id: Market token ID
            side: BUY or SELL
            price: Price (0-1)
            size: Size in USDC
            order_type: GTC, GTD, or FOK
            strategy: Strategy name for tracking
            market: Market name for tracking
            outcome: Outcome (UP/DOWN) for tracking
            signals: Signal data for tracking
        
        Returns:
            Order result
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would place {side} {size} @ {price} on {token_id[:16]}...")
            return {"success": True, "order_id": "DRY_RUN", "dry_run": True}
        
        try:
            # Calculate contracts from USD size
            contracts = size / price if price > 0 else 0
            
            # Build order args
            order_args = OrderArgs(
                price=price,
                size=contracts,
                side=BUY if side.upper() == "BUY" else SELL,
                token_id=token_id
            )
            
            # Build and sign order
            signed_order = self.client.create_and_post_order(order_args)
            
            order_id = signed_order.get("orderID", signed_order.get("id"))
            logger.info(f"Order placed: {side} {size:.2f} @ {price:.4f} = {contracts:.2f} contracts")
            
            # Log trade for tracking
            try:
                tracker = get_tracker()
                tracker.log_trade(
                    strategy=strategy,
                    market=market,
                    token_id=token_id,
                    side=side,
                    outcome=outcome,
                    entry_price=price,
                    size_usd=size,
                    contracts=contracts,
                    signals=signals or {},
                    order_id=order_id
                )
            except Exception as track_err:
                logger.warning(f"Failed to log trade: {track_err}")
            
            return {
                "success": True,
                "order_id": order_id,
                "data": signed_order
            }
            
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order."""
        if self.dry_run:
            return {"success": True, "dry_run": True}
        
        try:
            result = self.client.cancel(order_id)
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_open_orders(self) -> list:
        """Get open orders."""
        try:
            return self.client.get_orders()
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []
