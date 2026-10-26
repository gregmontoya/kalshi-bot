"""Open-Meteo Weather Feed for Stadium Conditions.

Analyzes wind speed, gusts, and precipitation to identify outdoor market impacts.
"""

import logging
from typing import Optional
import httpx
from src.config import settings
from src.core.models import StadiumWeather

logger = logging.getLogger(__name__)

STADIUM_COORDINATES: dict[str, dict] = {
    "arrowhead": {"name": "GEHA Field at Arrowhead Stadium", "lat": 39.0489, "lon": -94.4839, "is_dome": False},
    "lincoln": {"name": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675, "is_dome": False},
    "highmark": {"name": "Highmark Stadium", "lat": 42.7738, "lon": -78.7870, "is_dome": False},
    "ford_field": {"name": "Ford Field", "lat": 42.3400, "lon": -83.0456, "is_dome": True},
    "allegiant": {"name": "Allegiant Stadium", "lat": 36.0908, "lon": -115.1833, "is_dome": True},
}


class OpenMeteoClient:
    """Fetches wind and weather data from Open-Meteo API."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or settings.OPEN_METEO_URL

    async def get_stadium_weather(self, stadium_key: str = "arrowhead") -> StadiumWeather:
        stadium = STADIUM_COORDINATES.get(stadium_key.lower(), STADIUM_COORDINATES["arrowhead"])

        if stadium["is_dome"]:
            return StadiumWeather(
                venue_name=stadium["name"],
                latitude=stadium["lat"],
                longitude=stadium["lon"],
                temperature_f=72.0,
                wind_speed_mph=0.0,
                wind_gusts_mph=0.0,
                precipitation_prob=0.0,
                is_dome=True,
                impact_note="Domed stadium (controlled environment).",
            )

        params = {
            "latitude": stadium["lat"],
            "longitude": stadium["lon"],
            "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
        }

        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                resp = await client.get(self.base_url, params=params)
                resp.raise_for_status()
                data = resp.json()
                current = data.get("current", {})

                temp = float(current.get("temperature_2m", 65.0))
                wind_spd = float(current.get("wind_speed_10m", 8.0))
                gusts = float(current.get("wind_gusts_10m", 12.0))
                precip = float(current.get("precipitation", 0.0))

                impact = "Normal conditions"
                if wind_spd >= 20.0 or gusts >= 28.0:
                    impact = f"High wind alert ({wind_spd:.1f} mph): expect depressed passing & field goal efficiency."
                elif temp < 32.0:
                    impact = f"Sub-freezing temperature ({temp:.1f}F): potential ball security and run-heavy game script."

                return StadiumWeather(
                    venue_name=stadium["name"],
                    latitude=stadium["lat"],
                    longitude=stadium["lon"],
                    temperature_f=temp,
                    wind_speed_mph=wind_spd,
                    wind_gusts_mph=gusts,
                    precipitation_prob=precip,
                    is_dome=False,
                    impact_note=impact,
                )
            except Exception as exc:
                logger.warning("Error fetching Open-Meteo weather: %s; using default stadium model", exc)
                return StadiumWeather(
                    venue_name=stadium["name"],
                    latitude=stadium["lat"],
                    longitude=stadium["lon"],
                    temperature_f=58.0,
                    wind_speed_mph=9.5,
                    wind_gusts_mph=14.0,
                    precipitation_prob=0.1,
                    is_dome=False,
                    impact_note="Simulated weather feed active.",
                )
