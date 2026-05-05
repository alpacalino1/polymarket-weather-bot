"""
Parse Polymarket weather market questions to extract:
- Location (city, state)
- Date / date range
- Temperature threshold
- Weather metric type
- Precipitation threshold
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class ParsedWeatherMarket:
    """Extracted information from a weather market question."""
    raw_question: str
    condition_id: str

    # Extracted fields
    city: str = ""
    state: str = ""
    country: str = "US"
    target_date: Optional[date] = None
    end_date: Optional[date] = None
    date_confirmed: bool = False

    # What kind of weather event?
    metric: str = ""  # "temperature", "precipitation", "snowfall", "wind", "storm"

    # Temperature-specific
    temp_threshold_f: Optional[float] = None
    temp_threshold_c: Optional[float] = None
    temp_metric: str = ""  # "high", "low", "average", "record_high", "record_low"
    temp_direction: str = ""  # "above" or "below"

    # Precipitation-specific
    precip_type: str = ""  # "rain", "snow", "any"
    precip_threshold_inches: Optional[float] = None
    precip_probability_threshold: Optional[float] = None

    # Wind-specific
    wind_threshold_mph: Optional[float] = None

    # General
    is_weather: bool = False
    confidence: float = 1.0  # How confident we are in the parse (0-1)


class WeatherMarketParser:
    """Parse natural language weather market questions into structured data."""

    # ── City/State patterns ──────────────────────────────────────────────

    # Common US cities with their states (extendable)
    CITY_STATE_MAP: dict[str, tuple[str, str]] = {
        "new york": ("New York", "NY"),
        "new york city": ("New York", "NY"),
        "nyc": ("New York", "NY"),
        "los angeles": ("Los Angeles", "CA"),
        "la": ("Los Angeles", "CA"),
        "chicago": ("Chicago", "IL"),
        "houston": ("Houston", "TX"),
        "phoenix": ("Phoenix", "AZ"),
        "philadelphia": ("Philadelphia", "PA"),
        "san antonio": ("San Antonio", "TX"),
        "san diego": ("San Diego", "CA"),
        "dallas": ("Dallas", "TX"),
        "austin": ("Austin", "TX"),
        "jacksonville": ("Jacksonville", "FL"),
        "fort worth": ("Fort Worth", "TX"),
        "san jose": ("San Jose", "CA"),
        "columbus": ("Columbus", "OH"),
        "charlotte": ("Charlotte", "NC"),
        "indianapolis": ("Indianapolis", "IN"),
        "san francisco": ("San Francisco", "CA"),
        "seattle": ("Seattle", "WA"),
        "denver": ("Denver", "CO"),
        "washington": ("Washington", "DC"),
        "nashville": ("Nashville", "TN"),
        "boston": ("Boston", "MA"),
        "el paso": ("El Paso", "TX"),
        "detroit": ("Detroit", "MI"),
        "portland": ("Portland", "OR"),
        "memphis": ("Memphis", "TN"),
        "oklahoma city": ("Oklahoma City", "OK"),
        "las vegas": ("Las Vegas", "NV"),
        "louisville": ("Louisville", "KY"),
        "baltimore": ("Baltimore", "MD"),
        "milwaukee": ("Milwaukee", "WI"),
        "albuquerque": ("Albuquerque", "NM"),
        "tucson": ("Tucson", "AZ"),
        "fresno": ("Fresno", "CA"),
        "sacramento": ("Sacramento", "CA"),
        "mesa": ("Mesa", "AZ"),
        "atlanta": ("Atlanta", "GA"),
        "kansas city": ("Kansas City", "MO"),
        "omaha": ("Omaha", "NE"),
        "colorado springs": ("Colorado Springs", "CO"),
        "raleigh": ("Raleigh", "NC"),
        "long beach": ("Long Beach", "CA"),
        "virginia beach": ("Virginia Beach", "VA"),
        "miami": ("Miami", "FL"),
        "oakland": ("Oakland", "CA"),
        "minneapolis": ("Minneapolis", "MN"),
        "tampa": ("Tampa", "FL"),
        "tulsa": ("Tulsa", "OK"),
        "arlington": ("Arlington", "TX"),
        "new orleans": ("New Orleans", "LA"),
        "wichita": ("Wichita", "KS"),
        "cleveland": ("Cleveland", "OH"),
        "bakersfield": ("Bakersfield", "CA"),
        "aurora": ("Aurora", "CO"),
        "anaheim": ("Anaheim", "CA"),
        "honolulu": ("Honolulu", "HI"),
        "santa ana": ("Santa Ana", "CA"),
        "riverside": ("Riverside", "CA"),
        "corpus christi": ("Corpus Christi", "TX"),
        "lexington": ("Lexington", "KY"),
        "stockton": ("Stockton", "CA"),
        "st louis": ("St. Louis", "MO"),
        "saint louis": ("St. Louis", "MO"),
        "st. louis": ("St. Louis", "MO"),
        "pittsburgh": ("Pittsburgh", "PA"),
        "cincinnati": ("Cincinnati", "OH"),
        "anchorage": ("Anchorage", "AK"),
        "henderson": ("Henderson", "NV"),
        "greensboro": ("Greensboro", "NC"),
        "plano": ("Plano", "TX"),
        "newark": ("Newark", "NJ"),
        "toledo": ("Toledo", "OH"),
        "lincoln": ("Lincoln", "NE"),
        "orlando": ("Orlando", "FL"),
        "chula vista": ("Chula Vista", "CA"),
        "jersey city": ("Jersey City", "NJ"),
        "chandler": ("Chandler", "AZ"),
        "fort wayne": ("Fort Wayne", "IN"),
        "buffalo": ("Buffalo", "NY"),
        "durham": ("Durham", "NC"),
        "st. petersburg": ("St. Petersburg", "FL"),
        "irvine": ("Irvine", "CA"),
        "laredo": ("Laredo", "TX"),
        "lubbock": ("Lubbock", "TX"),
        "madison": ("Madison", "WI"),
        "gilbert": ("Gilbert", "AZ"),
        "norfolk": ("Norfolk", "VA"),
        "reno": ("Reno", "NV"),
        "winston salem": ("Winston-Salem", "NC"),
        "glendale": ("Glendale", "AZ"),
        "scottsdale": ("Scottsdale", "AZ"),
        "chesapeake": ("Chesapeake", "VA"),
        "garland": ("Garland", "TX"),
        "irving": ("Irving", "TX"),
        "hialeah": ("Hialeah", "FL"),
        "fremont": ("Fremont", "CA"),
        "boise": ("Boise", "ID"),
        "spokane": ("Spokane", "WA"),
        "baton rouge": ("Baton Rouge", "LA"),
        "tacoma": ("Tacoma", "WA"),
        "des moines": ("Des Moines", "IA"),
        "rochester": ("Rochester", "NY"),
        "york": ("York", "PA"),
        "richmond": ("Richmond", "VA"),
    }

    # ── Date patterns ────────────────────────────────────────────────────

    MONTH_MAP = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "july": 7,
        "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "oct": 10, "nov": 11, "dec": 12,
    }

    def parse(self, question: str, condition_id: str = "") -> ParsedWeatherMarket:
        """Parse a market question into structured weather data."""
        q = question.lower().strip()
        result = ParsedWeatherMarket(
            raw_question=question,
            condition_id=condition_id,
        )

        # First check if it's weather-related
        result.is_weather = self._is_weather_question(q)
        if not result.is_weather:
            return result

        # Extract location
        city, state = self._extract_location(q)
        result.city = city
        result.state = state

        # Extract date
        target_date, end_date, date_confirmed = self._extract_date(q)
        result.target_date = target_date
        result.end_date = end_date
        result.date_confirmed = date_confirmed

        # Determine metric type
        result.metric = self._determine_metric(q)

        # Extract specific thresholds based on metric
        if result.metric == "temperature":
            self._extract_temperature(q, result)
        elif result.metric == "precipitation":
            self._extract_precipitation(q, result)
        elif result.metric == "snowfall":
            self._extract_snowfall(q, result)
        elif result.metric == "wind":
            self._extract_wind(q, result)

        # Adjust confidence based on parse quality
        if not result.city:
            result.confidence *= 0.3
        if not result.target_date:
            result.confidence *= 0.4

        return result

    def _is_weather_question(self, q: str) -> bool:
        """Check if the question is weather-related."""
        weather_keywords = [
            "temperature", "heat", "cold", "degrees", "weather",
            "rain", "snow", "precipitation", "storm", "hurricane",
            "tornado", "wind", "windy", "drought", "flood",
            "record high", "record low", "heatwave", "cold snap",
            "freeze", "frost", "thunderstorm", "hail", "sleet",
            "fahrenheit", "celsius", "humidity", "dew point",
            "wind chill", "heat index", "barometer", "barometric",
            "climate", "meteorological", "forecast",
        ]
        q_lower = q.lower()
        return any(kw in q_lower for kw in weather_keywords)

    def _extract_location(self, q: str) -> tuple[str, str]:
        """Extract city and state from the question."""
        q_lower = q.lower()

        # Try exact matches from our map
        for city_key, (city, state) in self.CITY_STATE_MAP.items():
            if city_key in q_lower:
                return city, state

        # Try patterns like "in City, State" or "at City, State"
        location_patterns = [
            r"(?:in|at|for|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z]{2})",
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*([A-Z]{2})",
            r"(?:in|at|for|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        ]

        for pattern in location_patterns:
            match = re.search(pattern, q)
            if match:
                city = match.group(1).strip()
                state = match.group(2).strip() if match.lastindex and match.lastindex >= 2 else ""
                return city, state

        return "", ""

    def _extract_date(self, q: str) -> tuple[Optional[date], Optional[date], bool]:
        """Extract target date from the question.

        Returns: (target_date, end_date, confirmed)
        """
        today = date.today()
        year = today.year

        # Pattern: "Month Day" (e.g., "June 15", "July 4th")
        month_day_pattern = (
            r"(january|february|march|april|may|june|july|july"
            r"|august|september|october|november|december"
            r"|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
            r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
        )

        match = re.search(month_day_pattern, q, re.IGNORECASE)
        if match:
            month_name = match.group(1).lower()
            day = int(match.group(2))
            month = self.MONTH_MAP.get(month_name, 1)

            # Check if year is also mentioned
            year_match = re.search(r"(\d{4})", q)
            if year_match:
                year = int(year_match.group(1))

            try:
                target = date(year, month, day)
                # If the date has passed, assume next year
                if target < today:
                    target = date(year + 1, month, day)
                return target, None, True
            except ValueError:
                pass

        # Pattern: ISO date YYYY-MM-DD
        iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
        if iso_match:
            try:
                target = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
                return target, None, True
            except ValueError:
                pass

        # Pattern: "MM/DD/YYYY" or "MM/DD"
        slash_match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", q)
        if slash_match:
            month = int(slash_match.group(1))
            day = int(slash_match.group(2))
            yr = int(slash_match.group(3)) if slash_match.group(3) else year
            if yr < 100:
                yr += 2000
            try:
                target = date(yr, month, day)
                return target, None, True
            except ValueError:
                pass

        # Pattern: "by Date" or "before Date"
        by_match = re.search(r"(?:by|before)\s+([A-Z][a-z]+)\s+(\d{1,2})", q)
        if by_match:
            month_name = by_match.group(1).lower()
            day = int(by_match.group(2))
            month = self.MONTH_MAP.get(month_name, 1)
            try:
                target = date(year, month, day)
                return target, None, True
            except ValueError:
                pass

        # "this week", "next week", "today", "tomorrow"
        relative_patterns = {
            r"\btoday\b": 0,
            r"\btomorrow\b": 1,
            r"\bnext day\b": 1,
        }
        from datetime import timedelta

        for pattern, offset in relative_patterns.items():
            if re.search(pattern, q, re.IGNORECASE):
                return today + timedelta(days=offset), None, False

        # "this week" = end of current week (Sunday)
        if re.search(r"\bthis week\b", q, re.IGNORECASE):
            days_to_sunday = 6 - today.weekday()
            return today + timedelta(days=days_to_sunday), None, False

        return None, None, False

    def _determine_metric(self, q: str) -> str:
        """Determine what kind of weather metric the market is about."""
        q_lower = q.lower()

        if any(kw in q_lower for kw in ["temperature", "degrees", "°", "fahrenheit", "heat", "cold", "freeze", "frost"]):
            return "temperature"
        if any(kw in q_lower for kw in ["snow", "snowfall", "blizzard"]):
            return "snowfall"
        if any(kw in q_lower for kw in ["rain", "precipitation", "drizzle", "downpour"]):
            return "precipitation"
        if any(kw in q_lower for kw in ["wind", "windy", "gust"]):
            return "wind"
        if any(kw in q_lower for kw in ["hurricane", "tornado", "storm", "thunderstorm", "cyclone", "typhoon"]):
            return "storm"
        return "weather"

    def _extract_temperature(self, q: str, result: ParsedWeatherMarket):
        """Extract temperature thresholds from the question."""
        q_lower = q.lower()

        # Determine high/low/average
        if "high" in q_lower or "max" in q_lower or "highest" in q_lower or "peak" in q_lower:
            result.temp_metric = "high"
        elif "low" in q_lower or "min" in q_lower or "lowest" in q_lower:
            result.temp_metric = "low"
        elif "average" in q_lower or "mean" in q_lower:
            result.temp_metric = "average"
        elif "record high" in q_lower:
            result.temp_metric = "record_high"
        elif "record low" in q_lower:
            result.temp_metric = "record_low"
        else:
            result.temp_metric = "high"  # Default

        # Determine above/below
        above_patterns = [r"above", r"over", r"exceed", r"greater than", r"more than", r"≥", r">=", r"higher than"]
        below_patterns = [r"below", r"under", r"less than", r"lower than", r"≤", r"<=", r"beneath"]

        for pattern in above_patterns:
            if re.search(pattern, q_lower):
                result.temp_direction = "above"
                break
        for pattern in below_patterns:
            if re.search(pattern, q_lower):
                result.temp_direction = "below"
                break

        if not result.temp_direction:
            result.temp_direction = "above"  # Default for "Will temp reach X?"

        # Extract threshold value
        # Patterns: "90°F", "90 degrees", "90 F", "≥ 90°", "90F"
        temp_patterns = [
            r"(\d{2,3})\s*°?\s*[Ff](?:ahrenheit)?",
            r"(\d{2,3})\s*degrees?\s*(?:[Ff](?:ahrenheit)?)?",
            r"[≥≤>=<=]\s*(\d{2,3})\s*°?",
            r"temperature\s+(?:of\s+)?(\d{2,3})",
        ]

        for pattern in temp_patterns:
            match = re.search(pattern, q)
            if match:
                temp_f = float(match.group(1))
                result.temp_threshold_f = temp_f
                result.temp_threshold_c = (temp_f - 32) * 5 / 9
                result.confidence = min(result.confidence + 0.1, 1.0)
                break

    def _extract_precipitation(self, q: str, result: ParsedWeatherMarket):
        """Extract precipitation thresholds."""
        q_lower = q.lower()

        # Type
        if re.search(r"\brain\b", q_lower):
            result.precip_type = "rain"
        elif re.search(r"\bsnow\b", q_lower):
            result.precip_type = "snow"
        else:
            result.precip_type = "any"

        # Threshold in inches
        inch_patterns = [
            r"(\d+(?:\.\d+)?)\s*(?:inch|in|inches|″|\")",
            r"(\d+(?:\.\d+)?)\s*(?:mm|millimeter)",
        ]
        for pattern in inch_patterns:
            match = re.search(pattern, q)
            if match:
                val = float(match.group(1))
                if "mm" in match.group(0).lower():
                    val = val / 25.4  # mm to inches
                result.precip_threshold_inches = val
                result.confidence = min(result.confidence + 0.1, 1.0)
                break

    def _extract_snowfall(self, q: str, result: ParsedWeatherMarket):
        """Extract snowfall thresholds."""
        self._extract_precipitation(q, result)
        result.precip_type = "snow"

    def _extract_wind(self, q: str, result: ParsedWeatherMarket):
        """Extract wind speed thresholds."""
        wind_patterns = [
            r"(\d+)\s*(?:mph|miles per hour)",
            r"(\d+)\s*(?:km/h|kmh|kph|kilometers per hour)",
            r"(\d+)\s*(?:knot|kn|kts)",
            r"wind\s+(?:speed\s+)?(?:of\s+)?(\d+)",
        ]
        for pattern in wind_patterns:
            match = re.search(pattern, q)
            if match:
                val = float(match.group(1))
                if "km" in match.group(0).lower():
                    val = val * 0.621371  # km/h to mph
                elif "knot" in match.group(0).lower():
                    val = val * 1.15078  # knots to mph
                result.wind_threshold_mph = val
                result.confidence = min(result.confidence + 0.1, 1.0)
                break


# ─── Convenience function ──────────────────────────────────────────────────

_parser = WeatherMarketParser()

def parse_weather_market(question: str, condition_id: str = "") -> ParsedWeatherMarket:
    """Quick parse of a weather market question."""
    return _parser.parse(question, condition_id)
