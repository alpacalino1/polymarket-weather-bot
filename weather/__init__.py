"""
Weather data aggregation layer.
Combines Open-Meteo (global, free), NWS (US, free), and radar data.
"""

from .open_meteo import (
    OpenMeteoClient,
    GeoLocation,
    DailyForecast,
    WeatherForecast,
    fahrenheit_to_celsius,
    celsius_to_fahrenheit,
    inches_to_cm,
    mph_to_kmh,
)
from .nws import (
    NWSClient,
    NWSPeriod,
    NWSForecast,
    NWSAlert,
)
from .radar import (
    RainViewerClient,
    NWSRadarClient,
    RadarData,
    RadarFrame,
)

__all__ = [
    "OpenMeteoClient",
    "GeoLocation",
    "DailyForecast",
    "WeatherForecast",
    "NWSClient",
    "NWSPeriod",
    "NWSForecast",
    "NWSAlert",
    "RainViewerClient",
    "NWSRadarClient",
    "RadarData",
    "RadarFrame",
    "fahrenheit_to_celsius",
    "celsius_to_fahrenheit",
    "inches_to_cm",
    "mph_to_kmh",
]
