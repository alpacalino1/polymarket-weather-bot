"""
Polymarket CLOB API client for trading.
Uses py-clob-client for wallet auth, EIP-712 signing, and order management.

Also provides a Gamma API wrapper for market discovery.
"""

import os
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx


@dataclass
class PolymarketMarket:
    """A Polymarket market (from Gamma API)."""
    id: str
    condition_id: str
    question: str
    slug: str
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: dict[str, float] = field(default_factory=dict)
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    closed: bool = False
    end_date: Optional[datetime] = None
    tags: list[str] = field(default_factory=list)
    # CLOB token IDs for each outcome
    token_ids: dict[str, str] = field(default_factory=dict)
    # Event info
    event_title: str = ""
    event_slug: str = ""
    # Market metadata
    description: str = ""
    resolution_source: str = ""


@dataclass
class OrderResult:
    """Result of placing an order."""
    success: bool
    order_id: str = ""
    status: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    matched: float = 0.0
    error: str = ""
    transaction_hash: str = ""


@dataclass
class Position:
    """A position in a market."""
    condition_id: str
    token_id: str
    outcome: str  # "Yes" or "No"
    size: float
    avg_price: float
    market_question: str = ""


# ─── Gamma API (Market Discovery) ─────────────────────────────────────────

class GammaClient:
    """Polymarket Gamma API for market discovery and data.

    This API is public and doesn't require authentication for read operations.
    """

    BASE_URL = "https://gamma-api.polymarket.com"

    # Weather-related search terms
    WEATHER_SEARCH_TERMS = [
        "temperature", "heat", "cold", "degrees", "weather",
        "rain", "snow", "precipitation", "storm", "hurricane",
        "tornado", "wind", "drought", "flood", "wildfire",
        "record high", "record low", "heatwave", "cold snap",
        "freeze", "frost", "thunderstorm", "hail",
    ]

    def __init__(self, base_url: str = ""):
        self.base_url = base_url or self.BASE_URL
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def search_markets(
        self,
        query: str = "",
        tags: list[str] | None = None,
        limit: int = 100,
        closed: bool = False,
        offset: int = 0,
    ) -> list[PolymarketMarket]:
        """Search for markets on Polymarket.

        Args:
            query: Search query string
            tags: Filter by tags
            limit: Max results (max 500)
            closed: Include closed markets
            offset: Pagination offset
        """
        client = await self._get_client()
        params = {
            "limit": min(limit, 500),
            "closed": str(closed).lower(),
            "offset": offset,
        }
        if query:
            params["search"] = query
        if tags:
            params["tag"] = ",".join(tags)

        response = await client.get(f"{self.base_url}/markets", params=params)
        response.raise_for_status()
        data = response.json()

        return [self._parse_market(m) for m in data]

    async def search_weather_markets(self, limit: int = 100) -> list[PolymarketMarket]:
        """Search specifically for weather-related markets using multiple queries."""
        all_markets: dict[str, PolymarketMarket] = {}

        for term in self.WEATHER_SEARCH_TERMS:
            markets = await self.search_markets(query=term, limit=limit // 5)
            for market in markets:
                if market.condition_id not in all_markets:
                    all_markets[market.condition_id] = market

            if len(all_markets) >= limit:
                break

        # Also try with weather tag
        try:
            markets = await self.search_markets(tags=["weather"], limit=limit)
            for market in markets:
                if market.condition_id not in all_markets:
                    all_markets[market.condition_id] = market
        except Exception:
            pass

        return list(all_markets.values())[:limit]

    async def get_market(self, condition_id: str) -> Optional[PolymarketMarket]:
        """Get a specific market by condition ID."""
        client = await self._get_client()
        url = f"{self.base_url}/markets/{condition_id}"
        response = await client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._parse_market(response.json())

    async def get_market_prices(self, market: PolymarketMarket) -> dict[str, float]:
        """Get current prices for a market's outcomes. Returns {outcome: price}."""
        prices = {}
        for outcome in market.outcomes:
            token_id = market.token_ids.get(outcome)
            if token_id:
                try:
                    client = await self._get_client()
                    url = f"{self.base_url}/markets/{market.condition_id}/prices"
                    response = await client.get(url)
                    if response.status_code == 200:
                        data = response.json()
                        # Gamma price endpoint returns current prices
                        for item in data.get("history", []):
                            outcome_key = item.get("outcome", "")
                            price = float(item.get("price", 0))
                            prices[outcome_key] = price
                except Exception:
                    pass
        return prices

    def _parse_market(self, raw: dict) -> PolymarketMarket:
        """Parse a raw market dict into our model."""
        clob_ids = raw.get("clobTokenIds", [])
        tokens_raw = raw.get("tokens", [])
        outcomes_raw = raw.get("outcomes", [])
        outcome_prices_raw = raw.get("outcomePrices", [])

        # Map outcomes to their token IDs
        outcomes = []
        token_ids = {}
        for tok_str in outcomes_raw:
            outcomes.append(tok_str)
        for tok in tokens_raw:
            outcome = tok.get("outcome", "")
            token_id = tok.get("token_id", "")
            if outcome and token_id:
                token_ids[outcome] = token_id

        # Map outcome prices
        outcome_prices = {}
        for op in outcome_prices_raw:
            if isinstance(op, dict):
                outcome_prices[op.get("outcome", "")] = float(op.get("price", 0))

        # Parse end date
        end_date = None
        end_date_str = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date")
        if end_date_str:
            try:
                # Try common ISO formats
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return PolymarketMarket(
            id=raw.get("id", ""),
            condition_id=raw.get("conditionId", raw.get("condition_id", "")),
            question=raw.get("question", raw.get("title", "")),
            slug=raw.get("slug", ""),
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            volume=float(raw.get("volume", 0)),
            volume_24h=float(raw.get("volume24hr", raw.get("volume_24h", 0))),
            liquidity=float(raw.get("liquidity", 0)),
            closed=raw.get("closed", False),
            end_date=end_date,
            tags=raw.get("tags", []),
            token_ids=token_ids,
            event_title=raw.get("event", {}).get("title", "") if isinstance(raw.get("event"), dict) else "",
            event_slug=raw.get("eventSlug", raw.get("event_slug", "")),
            description=raw.get("description", ""),
            resolution_source=raw.get("resolutionSource", ""),
        )


# ─── CLOB Client (Trading) ─────────────────────────────────────────────────

class PolymarketClobClient:
    """Polymarket CLOB API client for order placement.

    Wraps py-clob-client for wallet auth and order signing.
    Falls back to readonly mode if not configured.
    """

    def __init__(
        self,
        private_key: str = "",
        chain_id: int = 137,
        clob_url: str = "https://clob.polymarket.com",
    ):
        self.private_key = private_key
        self.chain_id = chain_id
        self.clob_url = clob_url
        self._clob_client = None
        self._is_ready = False

        if private_key:
            self._init_clob_client()

    def _init_clob_client(self):
        """Initialize the py-clob-client instance."""
        try:
            from py_clob_client.client import ClobClient

            self._clob_client = ClobClient(
                host=self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=1,  # 1 = EOA wallet (MetaMask, etc.)
            )

            # Get API credentials from the CLOB server
            creds = self._clob_client.create_or_derive_api_creds()
            self._clob_client.set_api_creds(creds)

            self._is_ready = True
        except ImportError:
            print("⚠️  py-clob-client not installed. Trading disabled. Install with:")
            print("   pip install py-clob-client")
            self._is_ready = False
        except Exception as e:
            print(f"⚠️  Failed to initialize Polymarket client: {e}")
            self._is_ready = False

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    def place_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
    ) -> OrderResult:
        """Place a limit order on Polymarket CLOB.

        Args:
            token_id: The CLOB token ID for the outcome
            side: "BUY" or "SELL"
            price: Limit price (0.01 to 0.99 in USDC)
            size: Number of shares
        """
        if not self._is_ready or self._clob_client is None:
            return OrderResult(
                success=False,
                error="CLOB client not initialized. Set POLYMARKET_PRIVATE_KEY.",
            )

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            # Round price and size correctly
            price = round(price, 4)
            size = round(size, 1)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
                order_type=OrderType.GTC,  # Good-Til-Cancelled
            )

            result = self._clob_client.create_and_post_order(order_args)

            return OrderResult(
                success=True,
                order_id=result.get("orderID", result.get("id", "")),
                status=result.get("status", "live"),
                side=side.upper(),
                price=price,
                size=size,
                matched=float(result.get("size_matched", 0)),
                transaction_hash=result.get("transactionsHash", ""),
            )
        except Exception as e:
            return OrderResult(
                success=False,
                side=side.upper(),
                price=price,
                size=size,
                error=str(e),
            )

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook for a token."""
        if not self._is_ready or self._clob_client is None:
            return {"bids": [], "asks": []}

        try:
            return self._clob_client.get_order_book(token_id)
        except Exception:
            return {"bids": [], "asks": []}

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get current midpoint price for a token."""
        if not self._is_ready or self._clob_client is None:
            return None

        try:
            result = self._clob_client.get_midpoint(token_id)
            return float(result.get("mid", 0))
        except Exception:
            return None

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get best bid/ask price for a token."""
        ob = self.get_orderbook(token_id)
        orders = ob.get("bids" if side == "SELL" else "asks", [])
        if orders:
            return float(orders[0]["price"])
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order."""
        if not self._is_ready or self._clob_client is None:
            return False
        try:
            self._clob_client.cancel(order_id)
            return True
        except Exception:
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if not self._is_ready or self._clob_client is None:
            return False
        try:
            self._clob_client.cancel_all()
            return True
        except Exception:
            return False

    def get_balance(self) -> dict:
        """Get USDC balance and other info."""
        if not self._is_ready or self._clob_client is None:
            return {"balance": 0, "allowance": 0}
        try:
            return self._clob_client.get_balance_allowance()
        except Exception:
            return {"balance": 0, "allowance": 0}
