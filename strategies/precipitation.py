"""
Precipitation strategy for Polymarket weather markets.

Handles markets asking "Will it rain in [city] on [date]?" or
"Will [city] receive ≥ [X] inches of rain/snow on [date]?".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from weather.open_meteo import (
    OpenMeteoClient,
    DailyForecast,
    inches_to_cm,
)
from polymarket.markets import ParsedWeatherMarket
from strategies.temperature import TradeSignal


class PrecipitationStrategy:
    """Strategy for precipitation-related markets.

    Uses Open-Meteo's precipitation probability and accumulated
    precipitation forecasts to evaluate market prices.
    """

    MIN_EDGE = 0.05
    MIN_CONFIDENCE = 0.6
    MAX_FORECAST_DAYS = 16

    def __init__(self, open_meteo: OpenMeteoClient):
        self.weather = open_meteo

    async def evaluate(
        self,
        parsed: ParsedWeatherMarket,
        market_price_yes: float,
        token_id_yes: str,
        token_id_no: str = "",
        max_position: float = 100,
    ) -> Optional[TradeSignal]:
        """Evaluate a precipitation market."""
        if parsed.metric not in ("precipitation", "snowfall"):
            return None
        if parsed.confidence < self.MIN_CONFIDENCE:
            return None
        if not parsed.city:
            return None

        target_date = parsed.target_date
        if not target_date:
            return None

        today = datetime.now().date()
        forecast_days = (target_date - today).days
        if forecast_days < 0 or forecast_days > self.MAX_FORECAST_DAYS:
            return None

        # Geocode
        location = await self.weather.geocode_first(parsed.city, parsed.state)
        if not location:
            return None

        # Fetch forecast
        actual_days = min(forecast_days + 2, self.MAX_FORECAST_DAYS)
        forecast = await self.weather.get_forecast(
            latitude=location.latitude,
            longitude=location.longitude,
            forecast_days=actual_days,
        )

        # Find target day forecast
        target_forecast = None
        for df in forecast.daily:
            if df.date == target_date:
                target_forecast = df
                break

        if not target_forecast:
            if forecast_days < len(forecast.daily):
                target_forecast = forecast.daily[forecast_days]
            elif forecast.daily:
                target_forecast = forecast.daily[-1]
            else:
                return None

        # Calculate model probability based on whether there's a threshold
        if parsed.precip_threshold_inches is not None and parsed.precip_threshold_inches > 0:
            # Market asks about a specific amount (≥ X inches)
            model_prob = self._evaluate_amount_threshold(
                target_forecast, parsed, forecast_days
            )
        else:
            # Market asks simply "will it rain?" or "will it snow?"
            if parsed.metric == "snowfall":
                model_prob = self.weather.snowfall_probability(target_forecast)
            else:
                model_prob = self.weather.precipitation_probability(target_forecast)

        # Calculate edge
        edge = model_prob - market_price_yes
        abs_edge = abs(edge)

        if abs_edge < self.MIN_EDGE:
            return None

        # Size
        edge_multiplier = min(abs_edge / self.MIN_EDGE, 3.0)
        size_usdc = round(max_position * min(edge_multiplier * 0.33, 1.0) * parsed.confidence, 2)

        if size_usdc < 1:
            return None

        if edge > 0:
            side = "BUY"
            outcome = "Yes"
            token_id = token_id_yes
        else:
            side = "BUY"
            outcome = "No"
            token_id = token_id_no

        if not token_id:
            return None

        explanation = (
            f"Model prob {model_prob:.1%} vs market {market_price_yes:.1%} "
            f"(edge: {edge:+.1%}). "
            f"Precip sum: {target_forecast.precip_sum:.1f}mm, "
            f"Precip prob max: {target_forecast.precip_prob_max:.0f}%, "
            f"Snow: {target_forecast.snowfall_sum:.1f}cm. "
            f"City: {parsed.city}, {parsed.state}. "
            f"Days out: {forecast_days}."
        )

        return TradeSignal(
            condition_id=parsed.condition_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            market_price=market_price_yes if outcome == "Yes" else 1 - market_price_yes,
            model_probability=model_prob,
            edge=abs_edge,
            size_usdc=size_usdc,
            market_question=parsed.raw_question,
            strategy="Precipitation",
            confidence=parsed.confidence,
            explanation=explanation,
        )

    def _evaluate_amount_threshold(
        self,
        forecast: DailyForecast,
        parsed: ParsedWeatherMarket,
        forecast_days: int,
    ) -> float:
        """Evaluate probability of precipitation exceeding a specific amount."""
        threshold_inches = parsed.precip_threshold_inches or 0

        if parsed.metric == "snowfall":
            # Convert inches to cm
            threshold_cm = inches_to_cm(threshold_inches)
            expected = forecast.snowfall_sum
            # Snowfall forecasts are less precise; add uncertainty
            std = max(expected * 0.5, 2.0) + forecast_days * 0.5
        else:
            # Rain: precip_sum is in mm
            threshold_mm = threshold_inches * 25.4
            expected = forecast.precip_sum
            std = max(expected * 0.4, 3.0) + forecast_days * 1.0

        from scipy.stats import norm
        import numpy as np

        if std <= 0:
            std = 2.0

        # P(amount >= threshold)
        prob = 1.0 - norm.cdf(threshold_mm if parsed.metric != "snowfall" else threshold_cm,
                               loc=expected, scale=std)
        return float(np.clip(prob, 0.0, 1.0))
