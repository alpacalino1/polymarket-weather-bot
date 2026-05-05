"""
Configuration management for the Polymarket Weather Trading Bot.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env file if present
load_dotenv()


@dataclass
class PolymarketConfig:
    """Polymarket API and wallet configuration."""
    private_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_PRIVATE_KEY", ""))
    api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_SECRET", ""))
    api_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_PASSPHRASE", ""))
    clob_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))
    gamma_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"))
    chain_id: int = field(default_factory=lambda: int(os.getenv("POLYMARKET_CHAIN_ID", "137")))

    @property
    def is_configured(self) -> bool:
        """Check if we have enough credentials to trade."""
        return bool(self.private_key) or bool(self.api_key)

    @property
    def use_wallet_auth(self) -> bool:
        return bool(self.private_key)

    @property
    def use_api_key_auth(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret)


@dataclass
class TradingConfig:
    """Trading parameters and risk management."""
    max_position_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_USDC", "100")))
    min_edge: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE", "0.05")))
    max_concurrent_positions: int = field(default_factory=lambda: int(os.getenv("MAX_CONCURRENT_POSITIONS", "5")))
    market_check_cooldown_minutes: int = field(
        default_factory=lambda: int(os.getenv("MARKET_CHECK_COOLDOWN_MINUTES", "30"))
    )
    bot_mode: str = field(default_factory=lambda: os.getenv("BOT_MODE", "scan"))
    scan_interval_minutes: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL_MINUTES", "15")))
    max_total_exposure_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "500")))
    max_daily_loss_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_USDC", "200")))
    min_market_volume_usdc: float = field(default_factory=lambda: float(os.getenv("MIN_MARKET_VOLUME_USDC", "1000")))

    @property
    def is_live(self) -> bool:
        return self.bot_mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.bot_mode == "paper"

    @property
    def is_scan(self) -> bool:
        return self.bot_mode == "scan"


@dataclass
class WeatherConfig:
    """Weather API configuration."""
    openweathermap_key: str = field(default_factory=lambda: os.getenv("OPENWEATHERMAP_API_KEY", ""))
    weatherbit_key: str = field(default_factory=lambda: os.getenv("WEATHERBIT_API_KEY", ""))
    use_nws: bool = True  # Always available for US, free
    use_open_meteo: bool = True  # Always available global, free


@dataclass
class AppConfig:
    """Master configuration."""
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    focus_locations: list[str] = field(default_factory=lambda: [
        loc.strip() for loc in os.getenv("FOCUS_LOCATIONS", "").split("|") if loc.strip()
    ])
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "bot.log"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(Path.home() / ".polymarket-weather-bot"))))

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)


# Global config instance
config = AppConfig()
