"""External data ingestion feeds: The Odds API, ESPN Hidden Scoreboard, Open-Meteo."""

from src.feeds.espn_hidden_api import ESPNHiddenApiClient
from src.feeds.odds_api import OddsApiClient
from src.feeds.open_meteo import OpenMeteoClient

__all__ = ["OddsApiClient", "ESPNHiddenApiClient", "OpenMeteoClient"]
