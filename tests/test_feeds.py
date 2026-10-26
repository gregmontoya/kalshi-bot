"""Unit and integration tests for Phase 2 External Feeds."""

import math
import pytest
from src.feeds.espn_hidden_api import ESPNHiddenApiClient
from src.feeds.odds_api import OddsApiClient
from src.feeds.open_meteo import OpenMeteoClient


@pytest.mark.asyncio
async def test_odds_api_sharp_devigging():
    client = OddsApiClient()
    odds = await client.get_sharp_odds()

    assert len(odds) > 0
    for item in odds:
        assert item.bookmaker in ["pinnacle", "circa"]
        assert math.isclose(item.home_devigged_prob + item.away_devigged_prob, 1.0, rel_tol=1e-4)
        assert 0.0 < item.home_devigged_prob < 1.0
        assert 0.0 < item.away_devigged_prob < 1.0


@pytest.mark.asyncio
async def test_espn_hidden_api_game_state_parsing():
    client = ESPNHiddenApiClient()
    games = await client.get_scoreboard(sport="football", league="nfl")

    assert len(games) > 0
    for g in games:
        assert g.home_team
        assert g.away_team
        assert 1 <= g.period <= 5
        assert g.seconds_remaining >= 0
        assert 0 <= g.home_timeouts <= 3
        assert 0 <= g.away_timeouts <= 3

    # Check that at least one simulated game represents a 4th-quarter state
    fourth_q = [g for g in games if g.is_fourth_quarter]
    assert len(fourth_q) >= 1
    assert fourth_q[0].period == 4


@pytest.mark.asyncio
async def test_open_meteo_stadium_weather():
    client = OpenMeteoClient()

    # Outdoor stadium (Arrowhead)
    weather_outdoor = await client.get_stadium_weather("arrowhead")
    assert weather_outdoor.venue_name == "GEHA Field at Arrowhead Stadium"
    assert weather_outdoor.is_dome is False
    assert weather_outdoor.wind_speed_mph >= 0.0

    # Domed stadium (Ford Field)
    weather_dome = await client.get_stadium_weather("ford_field")
    assert weather_dome.venue_name == "Ford Field"
    assert weather_dome.is_dome is True
    assert weather_dome.wind_speed_mph == 0.0
    assert "Domed stadium" in (weather_dome.impact_note or "")
