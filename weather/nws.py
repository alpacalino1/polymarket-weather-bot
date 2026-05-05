"""
US National Weather Service API client - free, no API key required.
Provides US-only weather forecasts, alerts, and observations.

API docs: https://www.weather.gov/documentation/services-web-api
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

import httpx


@dataclass
class NWSPeriod:
    """A single forecast period from NWS."""
    name: str
    start_time: datetime
    end_time: datetime
    is_daytime: bool
    temperature: int  # °F
    temperature_unit: str = "F"
    temperature_trend: Optional[str] = None   # "rising", "falling", None
    wind_speed: str = ""
    wind_direction: str = ""
    short_forecast: str = ""
    detailed_forecast: str = ""
    precipitation_probability: Optional[int] = None  # 0-100 or None
    relative_humidity: Optional[int] = None


@dataclass
class NWSForecast:
    """NWS forecast for a location."""
    location_name: str
    periods: list[NWSPeriod] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    office: str = ""
    grid_x: int = 0
    grid_y: int = 0


@dataclass
class NWSAlert:
    """A weather alert/warning."""
    id: str
    event: str          # e.g., "Severe Thunderstorm Warning"
    headline: str
    severity: str       # "Extreme", "Severe", "Moderate", "Minor"
    certainty: str      # "Observed", "Likely", "Possible"
    area_desc: str
    effective: datetime
    expires: datetime
    description: str
    instruction: str = ""


class NWSClient:
    """Free US weather API client. No API key required."""

    BASE_URL = "https://api.weather.gov"
    USER_AGENT = "polymarket-weather-bot/1.0 (contact@example.com)"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": self.USER_AGENT, "Accept": "application/geo+json"},
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str) -> dict:
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()
        return response.json()

    async def get_point(self, lat: float, lon: float) -> dict:
        """Get NWS metadata for a geographic point (grid coordinates, forecast URLs)."""
        url = f"{self.BASE_URL}/points/{lat:.4f},{lon:.4f}"
        return await self._get(url)

    async def get_forecast(self, lat: float, lon: float) -> NWSForecast:
        """Fetch forecast for a US location."""
        point_data = await self.get_point(lat, lon)

        props = point_data.get("properties", {})
        forecast_url = props.get("forecast")
        office = props.get("cwa", "")
        grid_x = props.get("gridX", 0)
        grid_y = props.get("gridY", 0)
        location_name = props.get("relativeLocation", {}).get("properties", {}).get("city", "Unknown")

        if not forecast_url:
            raise ValueError(f"No NWS forecast available for {lat}, {lon}")

        forecast_data = await self._get(forecast_url)

        periods = []
        for p in forecast_data.get("properties", {}).get("periods", []):
            # Parse precipitation probability from short forecast
            precip_prob = self._extract_precip_prob(p.get("shortForecast", ""))

            period = NWSPeriod(
                name=p.get("name", ""),
                start_time=datetime.fromisoformat(p["startTime"].replace("Z", "+00:00")),
                end_time=datetime.fromisoformat(p["endTime"].replace("Z", "+00:00")),
                is_daytime=p.get("isDaytime", True),
                temperature=p.get("temperature", 0),
                temperature_unit=p.get("temperatureUnit", "F"),
                temperature_trend=p.get("temperatureTrend"),
                wind_speed=p.get("windSpeed", ""),
                wind_direction=p.get("windDirection", ""),
                short_forecast=p.get("shortForecast", ""),
                detailed_forecast=p.get("detailedForecast", ""),
                precipitation_probability=precip_prob,
                relative_humidity=p.get("relativeHumidity", {}).get("value"),
            )
            periods.append(period)

        return NWSForecast(
            location_name=location_name,
            periods=periods,
            office=office,
            grid_x=grid_x,
            grid_y=grid_y,
        )

    def _extract_precip_prob(self, short_forecast: str) -> Optional[int]:
        """Extract precipitation probability percentage from a forecast string."""
        import re
        # Match patterns like "Chance of precipitation is 40%",
        # "50 percent chance of rain"
        patterns = [
            r"(\d+)\s*%",
            r"(\d+)\s*percent",
            r"[Cc]hance.*?(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, short_forecast)
            if match:
                return int(match.group(1))
        return None

    async def get_alerts(self, lat: float, lon: float) -> list[NWSAlert]:
        """Get active weather alerts for a location."""
        point_data = await self.get_point(lat, lon)
        zone = point_data.get("properties", {}).get("forecastZone", "")
        if not zone:
            return []

        # Extract zone ID from URL
        zone_id = zone.rstrip("/").split("/")[-1]
        url = f"{self.BASE_URL}/alerts/active/zone/{zone_id}"
        alert_data = await self._get(url)

        alerts = []
        for feature in alert_data.get("features", []):
            props = feature.get("properties", {})
            alert = NWSAlert(
                id=props.get("id", ""),
                event=props.get("event", "Unknown"),
                headline=props.get("headline", ""),
                severity=props.get("severity", "Unknown"),
                certainty=props.get("certainty", "Unknown"),
                area_desc=props.get("areaDesc", ""),
                effective=datetime.fromisoformat(props["effective"].replace("Z", "+00:00")),
                expires=datetime.fromisoformat(props["expires"].replace("Z", "+00:00")),
                description=props.get("description", ""),
                instruction=props.get("instruction", ""),
            )
            alerts.append(alert)

        return alerts

    async def get_hourly_temperature(self, lat: float, lon: float) -> list[dict]:
        """Get hourly temperature forecast (more granular)."""
        point_data = await self.get_point(lat, lon)
        hourly_url = point_data.get("properties", {}).get("forecastHourly")
        if not hourly_url:
            return []

        data = await self._get(hourly_url)
        periods = data.get("properties", {}).get("periods", [])

        return [
            {
                "time": datetime.fromisoformat(p["startTime"].replace("Z", "+00:00")),
                "temperature_f": p["temperature"],
                "is_daytime": p["isDaytime"],
            }
            for p in periods[:48]  # Next 48 hours
        ]
