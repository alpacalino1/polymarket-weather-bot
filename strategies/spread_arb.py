"""
Spread arbitrage strategy for Polymarket CLOB.

Scans orderbooks for binary markets where:
- YES_ask + NO_ask < $1.00 → buy both for risk-free profit
- YES_bid + NO_bid > $1.00 → sell both (if you own them)

No modeling required - pure orderbook arbitrage.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx


@dataclass
class ArbOpportunity:
    """A spread arbitrage opportunity."""
    condition_id: str
    market_question: str
    token_id_yes: str
    token_id_no: str

    # Orderbook snapshot
    best_ask_yes: float
    best_ask_no: float
    best_bid_yes: float
    best_bid_no: float
    ask_size_yes: float
    ask_size_no: float

    # Arbitrage metrics
    buy_cost: float         # ask_yes + ask_no (cost to buy both)
    buy_profit_pct: float   # (1 - buy_cost) as percentage
    sell_revenue: float     # bid_yes + bid_no (revenue from selling both)
    max_size_usdc: float    # Max size for the arb (min of sizes)

    # Metadata
    volume_24h: float = 0
    scanned_at: datetime = None

    def __post_init__(self):
        if self.scanned_at is None:
            self.scanned_at = datetime.now()

    @property
    def is_buy_arb(self) -> bool:
        """Can we profit by buying both YES and NO?"""
        return self.buy_cost < 0.995  # < $1, with small buffer

    @property
    def is_sell_arb(self) -> bool:
        """Can we profit by selling both (if we hold them)?"""
        return self.sell_revenue > 1.005  # > $1

    @property
    def profit_per_unit(self) -> float:
        """Profit per $1 pair traded."""
        if self.is_buy_arb:
            return round(1.0 - self.buy_cost, 4)
        return 0.0


class SpreadArbScanner:
    """Scans Polymarket CLOB for spread arbitrage opportunities.

    For each binary market, fetches the orderbook and checks if
    YES + NO can be bought for < $1 (risk-free arbitrage).
    """

    CLOB_URL = "https://clob.polymarket.com"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_orderbook(self, token_id: str) -> dict:
        """Fetch orderbook for a token from CLOB API."""
        client = await self._get_client()
        try:
            r = await client.get(f"{self.CLOB_URL}/book", params={"token_id": token_id})
            r.raise_for_status()
            return r.json()
        except Exception:
            return {"bids": [], "asks": []}

    def _best_price_and_size(self, orders: list[dict], side: str) -> tuple[float, float]:
        """Get best price and cumulative size at that price level."""
        if not orders:
            return (0.0, 0.0)

        # orders are [{"price": "0.55", "size": "100"}, ...]
        best = orders[0]
        price = float(best.get("price", 0))
        size = float(best.get("size", 0))
        return (price, size)

    async def check_market(self, token_id_yes: str, token_id_no: str,
                           condition_id: str = "", question: str = "",
                           volume_24h: float = 0) -> Optional[ArbOpportunity]:
        """Check a single market for arbitrage.

        Args:
            token_id_yes: CLOB token ID for YES outcome
            token_id_no: CLOB token ID for NO outcome
            condition_id: Polymarket condition ID
            question: Market question
            volume_24h: 24h volume for filtering
        """
        # Fetch both orderbooks in parallel
        ob_yes = await self.get_orderbook(token_id_yes)
        ob_no = await self.get_orderbook(token_id_no)

        bids_yes = ob_yes.get("bids", [])
        asks_yes = ob_yes.get("asks", [])
        bids_no = ob_no.get("bids", [])
        asks_no = ob_no.get("asks", [])

        if not asks_yes or not asks_no:
            return None  # No liquidity

        best_ask_yes, ask_sz_yes = self._best_price_and_size(asks_yes, "ask")
        best_ask_no, ask_sz_no = self._best_price_and_size(asks_no, "ask")
        best_bid_yes, _ = self._best_price_and_size(bids_yes, "bid")
        best_bid_no, _ = self._best_price_and_size(bids_no, "bid")

        buy_cost = best_ask_yes + best_ask_no
        sell_revenue = best_bid_yes + best_bid_no
        max_size = min(ask_sz_yes, ask_sz_no)

        if buy_cost >= 0.999:  # No meaningful arb
            return None

        return ArbOpportunity(
            condition_id=condition_id,
            market_question=question,
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
            best_bid_yes=best_bid_yes,
            best_bid_no=best_bid_no,
            ask_size_yes=ask_sz_yes,
            ask_size_no=ask_sz_no,
            buy_cost=round(buy_cost, 4),
            buy_profit_pct=round((1.0 - buy_cost) * 100, 2),
            sell_revenue=round(sell_revenue, 4),
            max_size_usdc=round(max_size, 2),
            volume_24h=volume_24h,
        )
