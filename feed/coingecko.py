"""
Free crypto price feeds - no API key required.

Sources:
- CoinGecko public API (free, rate-limited ~30 calls/min)
- Binance public API (free, no key)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx


@dataclass
class CryptoPrice:
    """Current price data for a crypto asset."""
    symbol: str
    name: str
    price_usd: float
    price_btc: float = 0.0
    change_24h_pct: float = 0.0
    volume_24h_usd: float = 0.0
    market_cap_usd: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    fetched_at: datetime = None

    def __post_init__(self):
        if self.fetched_at is None:
            self.fetched_at = datetime.now()


@dataclass
class HistoricalCandle:
    """OHLCV candle data."""
    timestamp: int  # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


class CoinGeckoFeed:
    """Free CoinGecko public API. No key required.

    Rate limit: ~30 calls/minute for free tier.
    Docs: https://www.coingecko.com/en/api
    """

    BASE = "https://api.coingecko.com/api/v3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_price(self, coin_id: str = "bitcoin", vs_currency: str = "usd") -> Optional[CryptoPrice]:
        """Get current price for a coin.

        Args:
            coin_id: CoinGecko ID (e.g., 'bitcoin', 'ethereum', 'solana')
            vs_currency: Quote currency (usd, eur, etc.)
        """
        client = await self._get_client()
        params = {
            "ids": coin_id,
            "vs_currencies": vs_currency,
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
        }
        try:
            r = await client.get(f"{self.BASE}/simple/price", params=params)
            r.raise_for_status()
            data = r.json().get(coin_id, {})
            return CryptoPrice(
                symbol=coin_id.upper(),
                name=coin_id.title(),
                price_usd=data.get(vs_currency, 0),
                change_24h_pct=data.get(f"{vs_currency}_24h_change", 0) or 0,
                volume_24h_usd=data.get(f"{vs_currency}_24h_vol", 0) or 0,
                market_cap_usd=data.get(f"{vs_currency}_market_cap", 0) or 0,
            )
        except Exception as e:
            print(f"CoinGecko error: {e}")
            return None

    async def get_btc_price(self) -> Optional[CryptoPrice]:
        """Shorthand for Bitcoin price."""
        return await self.get_price("bitcoin")

    async def get_market_chart(self, coin_id: str = "bitcoin", days: int = 7
                               ) -> list[HistoricalCandle]:
        """Get historical OHLCV data.

        Args:
            coin_id: CoinGecko coin ID
            days: Number of days of data (1, 7, 14, 30, 90, 180, 365, max)
        """
        client = await self._get_client()
        params = {"vs_currency": "usd", "days": days}
        try:
            r = await client.get(f"{self.BASE}/coins/{coin_id}/ohlc", params=params)
            r.raise_for_status()
            candles = []
            for row in r.json():
                candles.append(HistoricalCandle(
                    timestamp=row[0],
                    open=row[1], high=row[2], low=row[3],
                    close=row[4], volume=0,
                ))
            return candles
        except Exception:
            return []


class BinanceFeed:
    """Free Binance public API. No key required."""

    BASE = "https://api.binance.com/api/v3"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_btc_price(self) -> Optional[CryptoPrice]:
        """Get BTC/USDT price from Binance ticker."""
        client = await self._get_client()
        try:
            r = await client.get(f"{self.BASE}/ticker/24hr", params={"symbol": "BTCUSDT"})
            r.raise_for_status()
            data = r.json()
            return CryptoPrice(
                symbol="BTC",
                name="Bitcoin",
                price_usd=float(data["lastPrice"]),
                change_24h_pct=float(data["priceChangePercent"]),
                volume_24h_usd=float(data["quoteVolume"]),
                high_24h=float(data["highPrice"]),
                low_24h=float(data["lowPrice"]),
            )
        except Exception:
            return None

    async def get_klines(self, symbol: str = "BTCUSDT", interval: str = "1h", 
                         limit: int = 168) -> list[HistoricalCandle]:
        """Get kline/candlestick data.

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            interval: '1m', '5m', '15m', '1h', '4h', '1d', '1w'
            limit: Number of candles (max 1000)
        """
        client = await self._get_client()
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            r = await client.get(f"{self.BASE}/klines", params=params)
            r.raise_for_status()
            candles = []
            for row in r.json():
                candles.append(HistoricalCandle(
                    timestamp=row[0],
                    open=float(row[1]), high=float(row[2]),
                    low=float(row[3]), close=float(row[4]),
                    volume=float(row[5]),
                ))
            return candles
        except Exception:
            return []
