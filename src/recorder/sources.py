"""HTTP sources. All failures raise -- no fallback or simulated data, ever."""

from __future__ import annotations

import asyncio

import httpx

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"


class SourceError(RuntimeError):
    pass


async def _get(client: httpx.AsyncClient, url: str, params: dict, retries: int = 3) -> httpx.Response:
    for attempt in range(retries):
        r = await client.get(url, params=params)
        if r.status_code == 429:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise SourceError(f"GET {url} -> {r.status_code}: {r.text[:200]}")
        return r
    raise SourceError(f"GET {url} -> rate limited after {retries} attempts")


async def kalshi_open_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = (await _get(client, f"{KALSHI_BASE}/markets", params)).json()
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


async def kalshi_market(client: httpx.AsyncClient, ticker: str) -> dict:
    return (await _get(client, f"{KALSHI_BASE}/markets/{ticker}", {})).json().get("market", {})


async def odds_events(client: httpx.AsyncClient, api_key: str, sport: str) -> list[dict]:
    """Free endpoint (0 credits): upcoming/in-play events with teams and commence times."""
    return (await _get(client, f"{ODDS_BASE}/sports/{sport}/events", {"apiKey": api_key})).json()


async def pinnacle_odds(client: httpx.AsyncClient, api_key: str, sport: str) -> tuple[list[dict], dict]:
    """Costs 1 credit. Returns (events, usage headers)."""
    r = await _get(client, f"{ODDS_BASE}/sports/{sport}/odds",
                   {"apiKey": api_key, "bookmakers": "pinnacle", "markets": "h2h", "oddsFormat": "decimal"})
    usage = {k: int(r.headers.get(f"x-requests-{k}", -1) or -1) for k in ("remaining", "used", "last")}
    return r.json(), usage
