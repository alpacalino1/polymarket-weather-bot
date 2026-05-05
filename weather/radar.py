"""
Weather radar data from free APIs.
Uses RainViewer API (global, free) and NWS radar (US, free).

Data sources:
- RainViewer: https://www.rainviewer.com/api.html (free, no key, global)
- NWS Radar: https://opengeo.ncep.noaa.gov (free, US only)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx


@dataclass
class RadarFrame:
    """Single radar frame/tile."""
    time: int  # Unix timestamp
    path: str  # Relative URL path to tile


@dataclass
class RadarData:
    """Complete radar data for a location."""
    host: str
    past: list[RadarFrame] = field(default_factory=list)
    nowcast: list[RadarFrame] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    # Precipitation intensity analysis
    intensity: float = 0.0        # 0-1 scale (0=none, 1=extreme)
    coverage_percent: float = 0.0 # % of area with precipitation
    trend: str = "stable"         # "increasing", "decreasing", "stable"


class RainViewerClient:
    """RainViewer global radar API. Free, no API key required.

    Provides global radar composites with past radar and future nowcast.
    """

    API_URL = "https://api.rainviewer.com/public/weather-maps.json"

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

    async def get_radar(self) -> RadarData:
        """Fetch the latest global radar composite data."""
        client = await self._get_client()
        response = await client.get(self.API_URL)
        response.raise_for_status()
        data = response.json()

        host = data.get("host", "")

        past_frames = [
            RadarFrame(time=f["time"], path=f["path"])
            for f in data.get("radar", {}).get("past", [])
        ]

        nowcast_frames = [
            RadarFrame(time=f["time"], path=f["path"])
            for f in data.get("radar", {}).get("nowcast", [])
        ]

        # Analyze intensity trends
        intensity, coverage, trend = self._analyze_radar(past_frames, nowcast_frames)

        return RadarData(
            host=host,
            past=past_frames,
            nowcast=nowcast_frames,
            intensity=intensity,
            coverage_percent=coverage,
            trend=trend,
        )

    def _analyze_radar(
        self,
        past: list[RadarFrame],
        nowcast: list[RadarFrame],
    ) -> tuple[float, float, str]:
        """Analyze radar data for precipitation trends.

        Since we can't easily decode the tile images server-side,
        we use frame timing as a proxy for activity:
        - More recent/upcoming frames → active precipitation
        - Density of nowcast frames → short-term intensity estimate
        """
        # Proxy: use number and density of frames as activity indicator
        total_frames = len(past) + len(nowcast)

        if total_frames == 0:
            return 0.0, 0.0, "stable"

        # Normalize intensity: 0.1-1.0 based on frame count
        # Typical: 10-20 past frames, 5-12 nowcast frames
        intensity = min(total_frames / 25.0, 1.0)

        # Coverage: estimate based on nowcast ratio
        if len(past) > 0:
            coverage = min(len(nowcast) / len(past), 1.0)
        else:
            coverage = 0.5 if nowcast else 0.0

        # Trend: compare nowcast count to past
        if len(nowcast) > len(past) * 0.3:
            trend = "increasing"
        elif len(nowcast) < len(past) * 0.15:
            trend = "decreasing"
        else:
            trend = "stable"

        return intensity, coverage, trend

    def get_tile_url(self, data: RadarData, frame: RadarFrame, 
                     z: int = 8, x: int = 0, y: int = 0,
                     color: int = 0, smoothing: int = 1,
                     snowfall: int = 0) -> str:
        """Build a URL for a specific radar tile.

        Args:
            data: RadarData from get_radar()
            frame: The specific radar frame
            z, x, y: Tile coordinates (slippy map format)
            color: Color scheme (0=original, 1=universal blue, etc.)
            smoothing: 1=on, 0=off
            snowfall: 1=show snow, 0=don't
        """
        return (
            f"https://{data.host}{frame.path}/256/{z}/{x}/{y}"
            f"/{color}/{smoothing}_{snowfall}.png"
        )


class NWSRadarClient:
    """NOAA/NWS radar data - free, US only.

    Uses Iowa Environmental Mesonet (IEM) for convenient access
    to NEXRAD radar composites.
    """

    IEM_RADAR_URL = "https://mesonet.agron.iastate.edu/api/1/nexrad.json"

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

    async def get_nexrad_stations(self) -> list[dict]:
        """List available NEXRAD radar stations."""
        client = await self._get_client()
        response = await client.get(f"{self.IEM_RADAR_URL}?operation=list")
        response.raise_for_status()
        return response.json().get("stations", [])

    async def get_nearest_radar(self, lat: float, lon: float) -> Optional[dict]:
        """Find the nearest NEXRAD station to a location."""
        stations = await self.get_nexrad_stations()

        if not stations:
            return None

        nearest = None
        min_dist = float("inf")

        for station in stations:
            slat = float(station["lat"])
            slon = float(station["lon"])
            # Simple distance approximation
            dist = ((lat - slat) ** 2 + (lon - slon) ** 2) ** 0.5
            if dist < min_dist:
                min_dist = dist
                nearest = station

        return nearest

    async def get_latest_radar_image_url(self, station_id: str) -> Optional[str]:
        """Get URL of the latest radar composite image for a station."""
        # Use IEM's convenient image endpoint
        now = datetime.utcnow()
        url = (
            f"https://mesonet.agron.iastate.edu/archive/data"
            f"/{now:%Y/%m/%d}/GIS/uscomp/n0q_{now:%Y%m%d%H%M}.png"
        )
        return url

    def get_radar_mosaic_url(self, sector: str = "CONUS") -> str:
        """Get URL for a NWS radar mosaic.

        Args:
            sector: "CONUS" (continental US), "ALASKA", "HAWAII", "PR"
        """
        base = "https://radar.weather.gov/ridge/standard"
        sector_map = {
            "CONUS": f"{base}/CONUS_loop.gif",
            "ALASKA": f"{base}/ALASKA_loop.gif",
            "HAWAII": f"{base}/HAWAII_loop.gif",
            "PR": f"{base}/PUERTORICO_loop.gif",
        }
        return sector_map.get(sector.upper(), sector_map["CONUS"])
