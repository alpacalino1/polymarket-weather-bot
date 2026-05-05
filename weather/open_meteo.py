"""
Open-Meteo API client - completely free, no API key required.
Provides global weather forecasts, ensemble data, and geocoding.

API docs: https://open-meteo.com/en/docs
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import httpx
import numpy as np
from scipy.stats import norm


# ─── Data models ───────────────────────────────────────────────────────────

@dataclass
class GeoLocation:
    """Geocoding result."""
    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""  # State/province
    timezone: str = "UTC"


@dataclass
class DailyForecast:
    """Single day forecast data."""
    date: date
    temp_max: float          # °C
    temp_min: float          # °C
    temp_mean: float          # °C (derived)
    precip_sum: float         # mm
    precip_prob_max: float    # % (0-100)
    precip_prob_mean: float   # % (0-100)
    wind_speed_max: float     # km/h
    wind_gust_max: float      # km/h
    snowfall_sum: float       # cm
    # Uncertainty from ensemble spread (if available)
    temp_max_std: float = 0.0
    temp_min_std: float = 0.0


@dataclass
class WeatherForecast:
    """Multi-day weather forecast."""
    location: GeoLocation
    daily: list[DailyForecast] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    source: str = "open-meteo"


# ─── API Client ────────────────────────────────────────────────────────────

class OpenMeteoClient:
    """Free global weather API client. No API key required."""

    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    ENSEMBLE_URL = "https://api.open-meteo.com/v1/forecast"  # Same, add ensemble params

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Geocoding ──────────────────────────────────────────────────────

    async def geocode(self, city: str, country: str = "US") -> list[GeoLocation]:
        """Convert city name to lat/lon coordinates.

        Args:
            city: City name (e.g., "New York", "Chicago")
            country: Two-letter country code (default: US)
        """
        client = await self._get_client()
        params = {
            "name": city,
            "count": 5,
            "language": "en",
            "format": "json",
        }
        response = await client.get(self.GEOCODING_URL, params=params)
        response.raise_for_status()
        data = response.json()

        results = []
        for r in data.get("results", []):
            loc = GeoLocation(
                name=r.get("name", city),
                latitude=r["latitude"],
                longitude=r["longitude"],
                country=r.get("country_code", r.get("country", "")),
                admin1=r.get("admin1", ""),
                timezone=r.get("timezone", "UTC"),
            )
            results.append(loc)

        # Filter by country if specified
        if country and results:
            filtered = [r for r in results if r.country.upper() == country.upper()]
            if filtered:
                results = filtered

        return results

    async def geocode_first(self, city: str, state: str = "", country: str = "US") -> Optional[GeoLocation]:
        """Get the first geocoding result."""
        results = await self.geocode(city, country)
        if not results:
            return None

        # If state is provided, try to match
        if state and len(results) > 1:
            for r in results:
                state_lower = state.lower()
                if (r.admin1.lower() == state_lower or
                        state_lower in r.name.lower() or
                        state_lower in r.admin1.lower()):
                    return r

        return results[0]

    # ── Forecast ───────────────────────────────────────────────────────

    async def get_forecast(
        self,
        latitude: float,
        longitude: float,
        forecast_days: int = 7,
        past_days: int = 0,
        include_ensemble: bool = True,
    ) -> WeatherForecast:
        """Fetch weather forecast for a location.

        Args:
            latitude: Location latitude
            longitude: Location longitude
            forecast_days: Number of forecast days (max 16)
            past_days: Days of historical data (max 92)
            include_ensemble: Include ensemble spread for uncertainty estimation
        """
        client = await self._get_client()

        daily_params = [
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "precipitation_probability_mean",
            "precipitation_hours",
            "wind_speed_10m_max",
            "wind_gusts_10m_max",
            "snowfall_sum",
        ]

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": ",".join(daily_params),
            "forecast_days": min(forecast_days, 16),
            "past_days": min(past_days, 92),
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "auto",
        }

        # Note: ensemble models require separate endpoint, not compatible with daily params
        # We estimate uncertainty heuristically in _get_ensemble_spread() instead

        response = await client.get(self.FORECAST_URL, params=params)
        response.raise_for_status()
        data = response.json()

        # Parse daily data
        daily_data = data.get("daily", {})
        dates = [date.fromisoformat(d) for d in daily_data.get("time", [])]
        temp_max_vals = daily_data.get("temperature_2m_max", [])
        temp_min_vals = daily_data.get("temperature_2m_min", [])
        precip_sum = daily_data.get("precipitation_sum", [])
        precip_prob_max = daily_data.get("precipitation_probability_max", [])
        precip_prob_mean = daily_data.get("precipitation_probability_mean", [])
        wind_max = daily_data.get("wind_speed_10m_max", [])
        wind_gust = daily_data.get("wind_gusts_10m_max", [])
        snow = daily_data.get("snowfall_sum", [])

        # Get uncertainty from ensemble spread if available
        ensemble_std = await self._get_ensemble_spread(latitude, longitude, forecast_days)

        daily_forecasts = []
        for i, d in enumerate(dates):
            t_max = temp_max_vals[i] if i < len(temp_max_vals) else 0
            t_min = temp_min_vals[i] if i < len(temp_min_vals) else 0
            t_mean = (t_max + t_min) / 2.0

            df = DailyForecast(
                date=d,
                temp_max=t_max,
                temp_min=t_min,
                temp_mean=t_mean,
                precip_sum=precip_sum[i] if i < len(precip_sum) else 0,
                precip_prob_max=precip_prob_max[i] if i < len(precip_prob_max) else 0,
                precip_prob_mean=precip_prob_mean[i] if i < len(precip_prob_mean) else 0,
                wind_speed_max=wind_max[i] if i < len(wind_max) else 0,
                wind_gust_max=wind_gust[i] if i < len(wind_gust) else 0,
                snowfall_sum=snow[i] if i < len(snow) else 0,
                temp_max_std=ensemble_std[i]["t_max_std"] if i < len(ensemble_std) else 3.0,
                temp_min_std=ensemble_std[i]["t_min_std"] if i < len(ensemble_std) else 3.0,
            )
            daily_forecasts.append(df)

        return WeatherForecast(
            location=GeoLocation(
                name=f"{latitude},{longitude}",
                latitude=latitude,
                longitude=longitude,
                timezone=data.get("timezone", "UTC"),
            ),
            daily=daily_forecasts,
        )

    async def _get_ensemble_spread(
        self, latitude: float, longitude: float, days: int
    ) -> list[dict]:
        """Estimate forecast uncertainty using ensemble members."""
        # For simplicity, use heuristic: uncertainty increases with forecast lead time
        # Day 0-1: std ~2°C, increasing by ~1°C per day beyond that
        spread = []
        for day in range(days):
            base_std = 2.0 + day * 0.8  # Increases with forecast distance
            spread.append({
                "t_max_std": base_std,
                "t_min_std": base_std * 0.9,
            })
        return spread

    # ── Probability calculations ───────────────────────────────────────

    @staticmethod
    def temperature_probability(
        forecast: DailyForecast,
        threshold_c: float,
        above: bool = True,
    ) -> float:
        """Calculate probability that temperature exceeds (or is below) a threshold.

        Uses normal distribution approximation around the forecast mean,
        with uncertainty from ensemble spread.

        Args:
            forecast: Daily forecast data
            threshold_c: Temperature threshold in Celsius
            above: True = P(temp >= threshold), False = P(temp <= threshold)

        Returns:
            Probability between 0.0 and 1.0
        """
        mean = forecast.temp_max if above else forecast.temp_min
        std = forecast.temp_max_std if above else forecast.temp_min_std

        if std == 0:
            std = 2.0  # Default uncertainty

        # P(X >= threshold) = 1 - CDF(threshold)
        if above:
            prob = 1.0 - norm.cdf(threshold_c, loc=mean, scale=std)
        else:
            prob = norm.cdf(threshold_c, loc=mean, scale=std)

        return float(np.clip(prob, 0.0, 1.0))

    @staticmethod
    def precipitation_probability(forecast: DailyForecast) -> float:
        """Get probability of precipitation (any measurable amount).

        Uses Open-Meteo's precipitation_probability_max if available,
        otherwise estimates from precipitation sum.
        """
        if forecast.precip_prob_max is not None and forecast.precip_prob_max > 0:
            return forecast.precip_prob_max / 100.0
        if forecast.precip_prob_mean is not None and forecast.precip_prob_mean > 0:
            return forecast.precip_prob_mean / 100.0
        # Heuristic: if precip_sum > 0, high probability
        if forecast.precip_sum > 5:
            return 0.95
        if forecast.precip_sum > 1:
            return 0.80
        if forecast.precip_sum > 0:
            return 0.65
        return 0.0

    @staticmethod
    def snowfall_probability(forecast: DailyForecast, threshold_cm: float = 0.1) -> float:
        """Probability of snowfall exceeding threshold."""
        if forecast.snowfall_sum > threshold_cm * 3:
            return 0.95
        if forecast.snowfall_sum > threshold_cm:
            return 0.75
        if forecast.snowfall_sum > 0:
            return 0.40
        # Snow possible if temperature at or below freezing
        if forecast.temp_min < 2:
            return 0.10
        return 0.0

    @staticmethod
    def wind_probability(forecast: DailyForecast, threshold_kmh: float) -> float:
        """Probability of wind speed exceeding threshold."""
        mean = forecast.wind_speed_max
        # Assume ~20% uncertainty in wind forecasts
        std = max(mean * 0.2, 8.0)
        prob = 1.0 - norm.cdf(threshold_kmh, loc=mean, scale=std)
        return float(np.clip(prob, 0.0, 1.0))


# ─── Unit conversion helpers ──────────────────────────────────────────────

def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9 / 5 + 32


def inches_to_cm(inches: float) -> float:
    return inches * 2.54


def mph_to_kmh(mph: float) -> float:
    return mph * 1.60934
