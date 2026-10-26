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

    # Storage & Persistence
    DUCKDB_PATH: str = Field(default="data/kalshi_quant.duckdb", description="Local DuckDB database file path")
    FIREBASE_CREDENTIALS_PATH: Optional[str] = Field(default=None, description="Path to Firebase credentials")
    FIREBASE_FIRESTORE_COLLECTION: str = "kalshi_quant_state"
    BIGQUERY_PROJECT_ID: Optional[str] = Field(default=None, description="BigQuery Project ID")
    BIGQUERY_DATASET: str = "kalshi_quant_grades"

    # Strategy Pod 1: maker_momentum_basket
    POD1_ENABLED: bool = True
    POD1_ENTRY_PRICE_MIN: int = 68
    POD1_ENTRY_PRICE_MAX: int = 76
    POD1_MIN_SHARP_EDGE_CENTS: int = 3
    POD1_DIP_FILL_OFFSET_CENTS: int = 2
    POD1_TAKE_PROFIT_MIN: int = 88
    POD1_TAKE_PROFIT_MAX: int = 92
    POD1_GUILLOTINE_TRIGGER_PRICE: int = 48
    POD1_GUILLOTINE_HOLD_SHARP_PROB: float = 0.58
    POD1_GUILLOTINE_EXIT_SHARP_PROB: float = 0.50
    POD1_DEFAULT_CONTRACTS_PER_LEG: int = 10
    POD1_BASKET_SIZE: int = 3

    # Strategy Pod 2: sharp_value_green_out
    POD2_ENABLED: bool = True
    POD2_ENTRY_PRICE_MIN: int = 38
    POD2_ENTRY_PRICE_MAX: int = 58
    POD2_MIN_SHARP_EDGE_CENTS: int = 5
    POD2_GREEN_OUT_GAIN_CENTS: int = 16
    POD2_DEFAULT_CONTRACTS: int = 25

    # Strategy Pod 3: tail_lock_harvester
    POD3_ENABLED: bool = True
    POD3_ENTRY_PRICE_MIN: int = 95
    POD3_ENTRY_PRICE_MAX: int = 97
    POD3_MIN_WIN_PROBABILITY: float = 0.992
    POD3_DEFAULT_CONTRACTS: int = 50


settings = Settings()
