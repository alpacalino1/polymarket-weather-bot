"""
BTC price threshold barrier model strategy.

For binary markets like "Will BTC be ≥ $X by date Y?":
1. Fetch BTC spot price from free API (CoinGecko)
2. Estimate volatility from historical OHLCV data
3. Compute first-passage probability using GBM barrier formula
4. Compare to market price → trade if edge exists

Barrier probability formula (first passage of GBM):
  P(hit B within T) = Φ(d1) + (B/S0)^(2ν/σ²) · Φ(d2)
  where ν = μ - σ²/2, d1 = (ln(S0/B) + νT)/(σ√T), d2 = (ln(S0/B) - νT)/(σ√T)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

import numpy as np
from scipy.stats import norm

from feed.coingecko import CoinGeckoFeed


@dataclass
class BTCThresholdSignal:
    """Trading signal for a BTC threshold market."""
    condition_id: str
    token_id: str
    outcome: str
    side: str
    market_question: str
    btc_spot: float
    target_price: float
    days_to_expiry: int
    annual_vol: float
    model_probability: float
    market_price_yes: float
    edge: float
    size_usdc: float
    explanation: str


class BTCThresholdStrategy:
    """Model-based strategy for BTC price threshold markets.

    Handles questions like:
    - "Will bitcoin hit $1m before [date/event]?"
    - "Will bitcoin be above $X on [date]?"
    """

    MIN_EDGE = 0.05
    MAX_FORECAST_DAYS = 365 * 3  # 3 years max

    def __init__(self, feed: CoinGeckoFeed):
        self.feed = feed

    async def evaluate_barrier(
        self,
        condition_id: str,
        token_id_yes: str,
        token_id_no: str,
        market_question: str,
        market_price_yes: float,
        target_price: float,
        days_to_expiry: int,
        max_position: float = 100,
    ) -> Optional[BTCThresholdSignal]:
        """Evaluate a barrier-hit market (BTC hits $X before D).

        Args:
            target_price: Price target in USD (e.g., 1_000_000)
            days_to_expiry: Days until market resolves
            market_price_yes: Current YES price on Polymarket
        """
        if days_to_expiry <= 0 or days_to_expiry > self.MAX_FORECAST_DAYS:
            return None

        # Get BTC spot + volatility
        btc = await self.feed.get_btc_price()
        if not btc:
            return None

        spot = btc.price_usd
        if spot >= target_price:
            # Already above target → YES resolves to true
            # Buy YES if market hasn't caught up
            if market_price_yes < 0.85:
                return BTCThresholdSignal(
                    condition_id=condition_id,
                    token_id=token_id_yes,
                    outcome="Yes",
                    side="BUY",
                    market_question=market_question,
                    btc_spot=spot,
                    target_price=target_price,
                    days_to_expiry=days_to_expiry,
                    annual_vol=0,
                    model_probability=1.0,
                    market_price_yes=market_price_yes,
                    edge=1.0 - market_price_yes,
                    size_usdc=min(max_position, 50),
                    explanation=f"BTC already above target (${spot:,.0f} > ${target_price:,.0f})",
                )
            return None

        # Estimate volatility from historical data
        sigma = await self._estimate_volatility(days_to_expiry)

        # Compute barrier hit probability
        T = days_to_expiry / 365.25
        prob = self._barrier_probability(spot, target_price, T, sigma, drift_assumption=0.0)

        edge = prob - market_price_yes
        abs_edge = abs(edge)

        if abs_edge < self.MIN_EDGE:
            return None

        if edge > 0:
            side = "BUY"
            outcome = "Yes"
            token_id = token_id_yes
        else:
            side = "BUY"
            outcome = "No"
            token_id = token_id_no

        # Size: scale by edge magnitude
        size_mult = min(abs_edge / self.MIN_EDGE, 3.0)
        size = round(max_position * min(size_mult * 0.33, 1.0), 2)
        if size < 5:
            return None

        explanation = (
            f"BTC: ${spot:,.0f} → ${target_price:,.0f} ({target_price/spot:.1f}x) "
            f"in {days_to_expiry}d ({T:.1f}yr). "
            f"σ={sigma*100:.0f}% annual. "
            f"Model P={prob*100:.1f}% vs Market={market_price_yes*100:.1f}%. "
            f"Edge={edge*100:+.1f}%"
        )

        return BTCThresholdSignal(
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            market_question=market_question,
            btc_spot=spot,
            target_price=target_price,
            days_to_expiry=days_to_expiry,
            annual_vol=sigma,
            model_probability=prob,
            market_price_yes=market_price_yes,
            edge=abs_edge,
            size_usdc=size,
            explanation=explanation,
        )

    async def _estimate_volatility(self, days: int) -> float:
        """Estimate annualized volatility from historical data."""
        lookback = min(max(days, 30), 365)
        candles = await self.feed.get_market_chart('bitcoin', days=lookback)
        if not candles:
            return 0.60  # Default

        closes = np.array([c.close for c in candles])
        log_ret = np.diff(np.log(closes))
        daily_vol = np.std(log_ret)

        return daily_vol * np.sqrt(365.25)

    @staticmethod
    def _barrier_probability(
        spot: float,
        barrier: float,
        T: float,
        sigma: float,
        drift_assumption: float = 0.0,
    ) -> float:
        """Compute probability of GBM hitting barrier B > S₀ within time T.

        Uses the first-passage time formula for Brownian motion with drift ν.

        P(τ ≤ T) = Φ((ln(S₀/B) + νT)/(σ√T)) + (B/S₀)^(2ν/σ²) · Φ((ln(S₀/B) - νT)/(σ√T))

        where ν = drift - σ²/2 is the log-drift.
        """
        if spot >= barrier:
            return 1.0
        if sigma == 0 or T <= 0:
            return 0.0

        nu = drift_assumption - sigma**2 / 2
        sigma_sqrt_T = sigma * np.sqrt(T)

        d1 = (np.log(spot / barrier) + nu * T) / sigma_sqrt_T
        d2 = (np.log(spot / barrier) - nu * T) / sigma_sqrt_T

        term1 = norm.cdf(d1)
        barrier_factor = (barrier / spot) ** (2 * nu / sigma**2)
        term2 = barrier_factor * norm.cdf(d2)

        prob = term1 + term2
        return float(np.clip(prob, 0.0, 1.0))
