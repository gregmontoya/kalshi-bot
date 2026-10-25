"""The Odds API v4 Integration with Sharp Bookmaker De-Vigging.

Pulls lines from sharp books:
- Pinnacle
- Circa
Strips bookmaker vig using Shin or Multiplicative method so probabilities sum to 100.0%.
"""

import logging
from typing import Any, Optional
import httpx
from engine.config import settings
from engine.core.math_engine import MathEngine
from engine.core.models import SharpOdds

logger = logging.getLogger(__name__)


class OddsApiClient:
    """Client for The Odds API v4 with automated vig removal."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.THE_ODDS_API_KEY
        self.base_url = settings.THE_ODDS_API_URL
        self._cache: dict[str, SharpOdds] = {}

    async def get_sharp_odds(
        self,
        sport_key: str = "americanfootball_nfl",
        regions: str = "us,eu",
        markets: str = "h2h",
    ) -> list[SharpOdds]:
        """
        Fetches odds from The Odds API v4 and filters for sharp books (Pinnacle, Circa).
        De-vigs probabilities using Shin method.
        """
        if not self.api_key or self.api_key == "your_the_odds_api_key_here":
            logger.info("The Odds API key not configured; using high-fidelity simulated sharp feed.")
            return self._get_simulated_sharp_odds(sport_key)

        url = f"{self.base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.error("Error fetching odds from The Odds API: %s; falling back to simulated feed", exc)
                return self._get_simulated_sharp_odds(sport_key)

        results = []
        for event in data:
            event_id = event.get("id")
            home_team = event.get("home_team")
            away_team = event.get("away_team")

            bookmakers = event.get("bookmakers", [])
            for bm in bookmakers:
                bm_key = bm.get("key", "").lower()
                if bm_key in settings.SHARP_BOOKMAKERS:
                    for market in bm.get("markets", []):
                        if market.get("key") == "h2h":
                            outcomes = market.get("outcomes", [])
                            home_outcome = next((o for o in outcomes if o.get("name") == home_team), None)
                            away_outcome = next((o for o in outcomes if o.get("name") == away_team), None)

                            if home_outcome and away_outcome:
                                home_price = float(home_outcome["price"])
                                away_price = float(away_outcome["price"])

                                # Strip vig via Shin method
                                p_home, p_away = MathEngine.devig_two_way(
                                    odds_home=home_price,
                                    odds_away=away_price,
                                    method="shin",
                                )

                                sharp_obj = SharpOdds(
                                    event_id=event_id,
                                    sport=sport_key,
                                    team_home=home_team,
                                    team_away=away_team,
                                    bookmaker=bm_key,
                                    home_raw_odds=home_price,
                                    away_raw_odds=away_price,
                                    home_devigged_prob=p_home,
                                    away_devigged_prob=p_away,
                                    devig_method="shin",
                                )
                                results.append(sharp_obj)
                                self._cache[f"{home_team}_{away_team}"] = sharp_obj

        return results

    def _get_simulated_sharp_odds(self, sport: str) -> list[SharpOdds]:
        """Provides simulated sharp lines with vig stripped for testing and offline execution."""
        sample_events = [
            ("Kansas City Chiefs", "Denver Broncos", 1.28, 4.05, "pinnacle"),
            ("Philadelphia Eagles", "Dallas Cowboys", 1.45, 2.95, "circa"),
            ("Buffalo Bills", "Miami Dolphins", 1.38, 3.30, "pinnacle"),
            ("Detroit Lions", "Green Bay Packers", 1.72, 2.22, "circa"),
        ]

        results = []
        for home, away, o_home, o_away, bookmaker in sample_events:
            p_home, p_away = MathEngine.devig_two_way(o_home, o_away, method="shin")
            sharp_obj = SharpOdds(
                event_id=f"sim-{home[:3]}-{away[:3]}",
                sport=sport,
                team_home=home,
                team_away=away,
                bookmaker=bookmaker,
                home_raw_odds=o_home,
                away_raw_odds=o_away,
                home_devigged_prob=p_home,
                away_devigged_prob=p_away,
                devig_method="shin",
            )
            results.append(sharp_obj)
            self._cache[f"{home}_{away}"] = sharp_obj

        return results
