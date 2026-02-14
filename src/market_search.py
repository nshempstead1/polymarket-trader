"""
Market Search - Find Any Polymarket Market by Keyword

Extends the bot beyond 15-minute crypto markets to support
ANY market on Polymarket. Searches via the Gamma API and
returns token IDs ready for trading.

Example:
    from src.market_search import MarketSearch

    search = MarketSearch()

    # Find markets by keyword
    results = search.find_markets("Trump")
    for m in results:
        print(m["question"], m["token_ids"])

    # Get a specific market by condition ID or slug
    market = search.get_market("0x123...")
"""

import json
from typing import Optional, Dict, Any, List
from .http import ThreadLocalSessionMixin


class MarketSearch(ThreadLocalSessionMixin):
    """
    Search and discover any Polymarket market.

    Uses the Gamma API to search markets by keyword, category,
    or status. Returns token IDs and metadata ready for trading.
    """

    GAMMA_HOST = "https://gamma-api.polymarket.com"
    CLOB_HOST = "https://clob.polymarket.com"

    def __init__(self, gamma_host: str = GAMMA_HOST, clob_host: str = CLOB_HOST, timeout: int = 15):
        super().__init__()
        self.gamma_host = gamma_host.rstrip("/")
        self.clob_host = clob_host.rstrip("/")
        self.timeout = timeout

    def find_markets(
        self,
        query: str,
        active_only: bool = True,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Search for markets by keyword.

        Args:
            query: Search term (e.g., "Trump", "Bitcoin", "election")
            active_only: Only return markets accepting orders
            limit: Max results to return
            offset: Pagination offset

        Returns:
            List of market dictionaries with parsed token IDs
        """
        url = f"{self.gamma_host}/markets"
        params = {
            "limit": limit,
            "offset": offset,
        }

        # The Gamma API supports full-text search via the _q param
        if query:
            params["_q"] = query

        if active_only:
            params["active"] = "true"
            params["closed"] = "false"

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            markets = response.json()
        except Exception as e:
            print(f"Search failed: {e}")
            return []

        results = []
        for market in markets:
            parsed = self._parse_market(market)
            if parsed:
                results.append(parsed)

        return results

    def find_markets_by_tag(self, tag: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search for markets by tag/category.

        Args:
            tag: Tag name (e.g., "politics", "crypto", "sports")
            limit: Max results

        Returns:
            List of market dictionaries
        """
        url = f"{self.gamma_host}/markets"
        params = {
            "tag": tag,
            "active": "true",
            "closed": "false",
            "limit": limit,
        }

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            markets = response.json()
        except Exception:
            return []

        return [self._parse_market(m) for m in markets if self._parse_market(m)]

    def get_market_by_id(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific market by its condition ID.

        Args:
            condition_id: The market's condition ID

        Returns:
            Parsed market dict or None
        """
        url = f"{self.gamma_host}/markets/{condition_id}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return self._parse_market(response.json())
        except Exception:
            pass
        return None

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific market by its URL slug.

        Args:
            slug: Market slug (from polymarket.com/event/slug-name)

        Returns:
            Parsed market dict or None
        """
        url = f"{self.gamma_host}/markets/slug/{slug}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return self._parse_market(response.json())
        except Exception:
            pass
        return None

    def get_events(self, query: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search for events (which contain multiple markets).

        An "event" on Polymarket groups related yes/no markets.
        For example, "2024 Election" event contains markets like
        "Will Trump win?" and "Will Biden win?"

        Args:
            query: Search term
            limit: Max results

        Returns:
            List of event dictionaries with nested markets
        """
        url = f"{self.gamma_host}/events"
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
        }
        if query:
            params["_q"] = query

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            events = response.json()
        except Exception:
            return []

        results = []
        for event in events:
            parsed_event = {
                "id": event.get("id", ""),
                "title": event.get("title", ""),
                "slug": event.get("slug", ""),
                "description": event.get("description", ""),
                "start_date": event.get("startDate", ""),
                "end_date": event.get("endDate", ""),
                "liquidity": float(event.get("liquidity", 0) or 0),
                "volume": float(event.get("volume", 0) or 0),
                "markets": [],
            }

            # Parse nested markets
            for market in event.get("markets", []):
                parsed = self._parse_market(market)
                if parsed:
                    parsed_event["markets"].append(parsed)

            if parsed_event["markets"]:
                results.append(parsed_event)

        return results

    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Get the current orderbook for a token from the CLOB API.

        Args:
            token_id: The CLOB token ID

        Returns:
            Orderbook data with bids and asks
        """
        url = f"{self.clob_host}/book"
        params = {"token_id": token_id}

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Failed to get orderbook: {e}")
            return {}

    def get_market_price(self, token_id: str) -> Optional[float]:
        """
        Get the current mid price for a token.

        Args:
            token_id: The CLOB token ID

        Returns:
            Mid price as float, or None
        """
        book = self.get_orderbook(token_id)
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1

        if best_bid > 0 and best_ask < 1:
            return (best_bid + best_ask) / 2
        elif best_bid > 0:
            return best_bid
        elif best_ask < 1:
            return best_ask
        return None

    def get_trending(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get trending/popular markets.

        Returns:
            List of markets sorted by volume
        """
        url = f"{self.gamma_host}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume",
            "ascending": "false",
            "limit": limit,
        }

        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            markets = response.json()
        except Exception:
            return []

        return [self._parse_market(m) for m in markets if self._parse_market(m)]

    def _parse_market(self, market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse raw Gamma API market into clean format with token IDs."""
        if not market:
            return None

        # Parse token IDs
        clob_token_ids = market.get("clobTokenIds", "[]")
        if isinstance(clob_token_ids, str):
            try:
                token_ids = json.loads(clob_token_ids)
            except json.JSONDecodeError:
                token_ids = []
        else:
            token_ids = clob_token_ids or []

        # Parse outcomes
        outcomes = market.get("outcomes", '["Yes", "No"]')
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = ["Yes", "No"]

        # Parse outcome prices
        outcome_prices = market.get("outcomePrices", '["0.5", "0.5"]')
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except json.JSONDecodeError:
                outcome_prices = []

        # Build token map: {outcome_label: token_id}
        token_map = {}
        price_map = {}
        for i, outcome in enumerate(outcomes):
            label = str(outcome).lower()
            if i < len(token_ids):
                token_map[label] = token_ids[i]
            if i < len(outcome_prices):
                try:
                    price_map[label] = float(outcome_prices[i])
                except (ValueError, TypeError):
                    price_map[label] = 0.0

        return {
            "condition_id": market.get("conditionId", ""),
            "question": market.get("question", ""),
            "slug": market.get("slug", ""),
            "description": market.get("description", ""),
            "outcomes": outcomes,
            "token_ids": token_map,
            "token_id_list": token_ids,
            "prices": price_map,
            "end_date": market.get("endDate", ""),
            "accepting_orders": market.get("acceptingOrders", False),
            "liquidity": float(market.get("liquidity", 0) or 0),
            "volume": float(market.get("volume", 0) or 0),
            "volume_24h": float(market.get("volume24hr", 0) or 0),
            "best_bid": market.get("bestBid"),
            "best_ask": market.get("bestAsk"),
            "spread": market.get("spread"),
            "url": f"https://polymarket.com/event/{market.get('slug', '')}",
        }

    def print_market(self, market: Dict[str, Any], show_tokens: bool = True) -> None:
        """Pretty-print a market result."""
        print(f"\n{'='*70}")
        print(f"  {market['question']}")
        print(f"  URL: {market['url']}")
        print(f"  Condition ID: {market['condition_id']}")
        print(f"  Status: {'Active' if market['accepting_orders'] else 'Closed'}")
        print(f"  Liquidity: ${market['liquidity']:,.0f}  |  Volume: ${market['volume']:,.0f}")
        print(f"  Ends: {market['end_date']}")

        if market['prices']:
            print(f"\n  Prices:")
            for outcome, price in market['prices'].items():
                pct = price * 100
                print(f"    {outcome.upper():12s} {price:.4f} ({pct:.1f}%)")

        if show_tokens and market['token_ids']:
            print(f"\n  Token IDs (for bot.place_order):")
            for outcome, tid in market['token_ids'].items():
                print(f"    {outcome.upper():12s} {tid}")

        print(f"{'='*70}")
