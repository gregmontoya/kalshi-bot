"""HTTP sources. All failures raise -- no fallback or simulated data, ever."""

from __future__ import annotations

import asyncio

import httpx

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"


class SourceError(RuntimeError):
    pass


async def _get(client: httpx.AsyncClient, url: str, params: dict, retries: int = 8) -> httpx.Response:
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            if attempt + 1 == retries:
                raise SourceError(f"GET {url} -> network error: {e}") from e
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
        if r.status_code == 429:
            await asyncio.sleep(0.6 * (attempt + 1))
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
    return r.json(), _usage(r)


def _usage(r: httpx.Response) -> dict:
    return {k: int(r.headers.get(f"x-requests-{k}", -1) or -1) for k in ("remaining", "used", "last")}


async def kalshi_settled_markets(client: httpx.AsyncClient, series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = (await _get(client, f"{KALSHI_BASE}/markets", params)).json()
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


async def kalshi_candles(client: httpx.AsyncClient, series: str, ticker: str, start_ts: int, end_ts: int,
                         period_min: int = 1) -> list[dict]:
    r = await _get(client, f"{KALSHI_BASE}/series/{series}/markets/{ticker}/candlesticks",
                   {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_min})
    return r.json().get("candlesticks", [])


async def historical_events(client: httpx.AsyncClient, api_key: str, sport: str, date_iso: str) -> tuple[list[dict], dict]:
    """Costs 1 credit."""
    r = await _get(client, f"{ODDS_BASE}/historical/sports/{sport}/events", {"apiKey": api_key, "date": date_iso})
    return r.json().get("data", []), _usage(r)


async def historical_pinnacle(client: httpx.AsyncClient, api_key: str, sport: str, date_iso: str) -> tuple[dict, dict]:
    """Costs 10 credits. Returns ({timestamp, data:[events]}, usage)."""
    r = await _get(client, f"{ODDS_BASE}/historical/sports/{sport}/odds",
                   {"apiKey": api_key, "date": date_iso, "bookmakers": "pinnacle", "markets": "h2h",
                    "oddsFormat": "decimal"})
    return r.json(), _usage(r)
