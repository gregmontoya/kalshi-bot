"""Configuration and settings for src package."""

from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEMO = "DEMO"
    PROD = "PROD"


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BASE_DIR: Path = Path(__file__).resolve().parent.parent

    # Non-Negotiable Kalshi v2 Endpoints
    KALSHI_ENV: Environment = Environment.DEMO
    DEMO_REST_URL: str = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_REST_URL: str = "https://external-api.kalshi.com/trade-api/v2"
    PROD_WS_URL: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    DEMO_WS_URL: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"

    @property
    def kalshi_rest_url(self) -> str:
        return self.PROD_REST_URL if self.KALSHI_ENV == Environment.PROD else self.DEMO_REST_URL

    @property
    def kalshi_ws_url(self) -> str:
        return self.PROD_WS_URL if self.KALSHI_ENV == Environment.PROD else self.DEMO_WS_URL

    # Credentials
    KALSHI_KEY_ID: str = Field(default="demo-key-id", description="Kalshi RSA API Key ID")
    KALSHI_PRIVATE_KEY_PATH: Optional[str] = Field(default=None, description="Path to PEM RSA private key file")
    KALSHI_PRIVATE_KEY: Optional[str] = Field(default=None, description="RSA private key PEM string")

    # Trading Execution Mode
    TRADING_MODE: TradingMode = TradingMode.PAPER

    # Mandatory Safety Flags
    DEFAULT_SELL_POSITION_FLOOR: int = 0
    DEFAULT_CANCEL_ON_PAUSE: bool = True
    DEFAULT_POST_ONLY: bool = True

    # Feeds
    THE_ODDS_API_KEY: Optional[str] = Field(default=None, description="The Odds API v4 API Key")
    THE_ODDS_API_URL: str = "https://api.the-odds-api.com/v4"
    SHARP_BOOKMAKERS: list[str] = ["pinnacle", "circa"]
    ESPN_HIDDEN_SCOREBOARD_URL: str = "https://site.api.espn.com/apis/site/v2/sports"
    OPEN_METEO_URL: str = "https://api.open-meteo.com/v1/forecast"


settings = Settings()
