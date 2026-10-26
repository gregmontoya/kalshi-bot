"""Entity Mapper: Links Kalshi Market Tickers to External Sportsbook & ESPN IDs.

Translates between Kalshi contract nomenclature, The Odds API teams, and ESPN game events.
"""

import re
from typing import Any, Optional

# Canonical NFL team aliases mapping to standard team display names
NFL_TEAM_ALIASES: dict[str, str] = {
    "kc": "Kansas City Chiefs",
    "chiefs": "Kansas City Chiefs",
    "den": "Denver Broncos",
    "broncos": "Denver Broncos",
    "phi": "Philadelphia Eagles",
    "eagles": "Philadelphia Eagles",
    "dal": "Dallas Cowboys",
    "cowboys": "Dallas Cowboys",
    "buf": "Buffalo Bills",
    "bills": "Buffalo Bills",
    "mia": "Miami Dolphins",
    "dolphins": "Miami Dolphins",
    "det": "Detroit Lions",
    "lions": "Detroit Lions",
    "gb": "Green Bay Packers",
    "packers": "Green Bay Packers",
    "sf": "San Francisco 49ers",
    "49ers": "San Francisco 49ers",
    "bal": "Baltimore Ravens",
    "ravens": "Baltimore Ravens",
}


class EntityMapper:
    """Matches Kalshi market tickers with external data providers."""

    @staticmethod
    def match_team_from_ticker_or_title(text: str) -> Optional[str]:
        """Resolves team name from ticker (e.g. KXNFL-KC-DEN) or title."""
        text_lower = text.lower()
        for alias, full_name in NFL_TEAM_ALIASES.items():
            # Check for exact token match
            if re.search(rf"\b{re.escape(alias)}\b", text_lower) or full_name.lower() in text_lower:
                return full_name
        return None

    @staticmethod
    def link_market_to_external_events(
        kalshi_ticker: str,
        market_title: str,
        sharp_odds_list: list[Any],
        espn_games: list[Any],
    ) -> dict[str, Any]:
        """
        Links a Kalshi market to matched The Odds API sharp line and ESPN live game state.
        """
        matched_sharp = None
        matched_espn = None

        search_text = f"{kalshi_ticker} {market_title}".lower()

        # Match sharp odds
        for odds in sharp_odds_list:
            home = getattr(odds, "team_home", "").lower()
            away = getattr(odds, "team_away", "").lower()
            if (home and home in search_text) or (away and away in search_text):
                matched_sharp = odds
                break

        # Match ESPN game
        for game in espn_games:
            home = getattr(game, "home_team", "").lower()
            away = getattr(game, "away_team", "").lower()
            if (home and home in search_text) or (away and away in search_text):
                matched_espn = game
                break

        return {
            "kalshi_ticker": kalshi_ticker,
            "matched_sharp": matched_sharp,
            "matched_espn": matched_espn,
            "is_fully_linked": (matched_sharp is not None and matched_espn is not None),
        }
