"""Tonight's games decision board (READ-ONLY research tool -- never places orders).

Usage:
    PYTHONPATH=. .venv/bin/python betting_board/board.py [--sports nba,nhl] [--date YYYY-MM-DD] [--days 1] [--refresh]

Data sources (all GETs, no order endpoints are ever touched):
  * Kalshi public market data (no auth)        -> yes bid / ask / volume per team market
  * The Odds API v4, Pinnacle h2h (1 credit/sport/run, cached 10 min) -> de-vigged fair prob
  * ESPN free site API (scoreboard, standings, injuries) -> records, streak, L10, back-to-back, injuries
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.recorder.devig import devig_power  # noqa: E402
from src.recorder.matching import DRAW_NAMES, match_event, norm, outcome_name_for, team_matches, ticker_date  # noqa: E402

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports"

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = OUT_DIR / "cache"
ODDS_CACHE_TTL_S = 600

# Live bot rule (src/recorder/live_maker.py): maker edge >= 2c, maker price within [0.35, 0.80].
MIN_MAKER_EDGE = 0.02
MIN_PRICE, MAX_PRICE = 0.35, 0.80

SPORTS = {
    "nba": {"series": "KXNBAGAME", "odds": "basketball_nba", "espn": "basketball/nba", "label": "NBA"},
    "nhl": {"series": "KXNHLGAME", "odds": "icehockey_nhl", "espn": "hockey/nhl", "label": "NHL"},
    "nfl": {"series": "KXNFLGAME", "odds": "americanfootball_nfl", "espn": "football/nfl", "label": "NFL"},
    "epl": {"series": "KXEPLGAME", "odds": "soccer_epl", "espn": "soccer/eng.1", "label": "EPL"},
}

OUT_STATUSES = {"out", "injured reserve", "suspension", "suspended", "ir", "long term ir", "o"}
DTD_STATUSES = {"day-to-day", "questionable", "doubtful", "game time decision", "probable"}


# --------------------------------------------------------------------------- HTTP

def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 4) -> tuple[dict | list, httpx.Response]:
    last = None
    for attempt in range(retries):
        try:
            r = client.get(url, params=params or {})
        except httpx.HTTPError as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(0.8 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json(), r
    raise RuntimeError(f"GET {url} failed: {last}")


def fnum(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- data models

@dataclass
class TeamRow:
    ticker: str
    name: str                 # Kalshi yes_sub_title
    abbr: str                 # Kalshi ticker suffix
    bid: float | None
    ask: float | None
    volume: float
    home_away: str = "?"
    fair: float | None = None
    espn: dict | None = None  # competitor dict from ESPN scoreboard
    standing: dict = field(default_factory=dict)
    b2b: bool | None = None
    injuries: dict = field(default_factory=dict)

    @property
    def spread(self) -> float | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    @property
    def maker_price(self) -> float | None:
        """Same as the live bot (src/recorder/ws_paper.py): bid+1c if spread>=2c, else ask-1c."""
        if self.ask is None or self.ask <= 0:
            return None
        b = self.bid if self.bid else max(0.01, self.ask - 0.02)
        return round(b + 0.01, 4) if self.ask - b >= 0.02 - 1e-9 else round(max(0.01, self.ask - 0.01), 4)

    @property
    def taker_edge(self) -> float | None:
        return None if self.fair is None or self.ask is None else self.fair - self.ask

    @property
    def maker_edge(self) -> float | None:
        mp = self.maker_price
        return None if self.fair is None or mp is None else self.fair - mp

    def is_play(self, pregame: bool) -> bool:
        mp, me = self.maker_price, self.maker_edge
        return (pregame and mp is not None and me is not None and me >= MIN_MAKER_EDGE - 1e-9
                and MIN_PRICE <= mp <= MAX_PRICE and mp < (self.ask or 0))


@dataclass
class Game:
    sport: str
    event_ticker: str
    gdate: date
    teams: list[TeamRow]
    start_utc: datetime | None = None
    start_src: str = ""
    status: str = "pre"        # pre / live / final (from ESPN when available)
    status_detail: str = ""
    odds_matched: bool = False
    espn_matched: bool = False
    pinnacle_update: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def pregame(self) -> bool:
        return self.status == "pre"

    def label(self) -> str:
        away = [t for t in self.teams if t.home_away == "away"]
        home = [t for t in self.teams if t.home_away == "home"]
        if away and home:
            return f"{away[0].abbr} @ {home[0].abbr}"
        return " v ".join(t.abbr for t in self.teams if norm(t.name) not in DRAW_NAMES)


# --------------------------------------------------------------------------- fetchers

def kalshi_markets(client: httpx.Client, series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data, _ = get_json(client, f"{KALSHI_BASE}/markets", params)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


def pinnacle_odds(client: httpx.Client, api_key: str, sport_key: str, refresh: bool) -> tuple[list[dict], str]:
    """1 credit per call; cached for 10 minutes to protect the budget."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"odds_{sport_key}.json"
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < ODDS_CACHE_TTL_S:
        age = int(time.time() - cache.stat().st_mtime)
        return json.loads(cache.read_text()), f"cache ({age}s old, 0 credits)"
    data, r = get_json(client, f"{ODDS_BASE}/sports/{sport_key}/odds",
                       {"apiKey": api_key, "regions": "eu", "markets": "h2h", "bookmakers": "pinnacle",
                        "oddsFormat": "decimal"})
    cache.write_text(json.dumps(data))
    rem, last = r.headers.get("x-requests-remaining", "?"), r.headers.get("x-requests-last", "?")
    return data, f"live call (cost {last}, remaining {rem})"


def espn_scoreboard(client: httpx.Client, path: str, d: date) -> list[dict]:
    try:
        data, _ = get_json(client, f"{ESPN_SITE}/{path}/scoreboard", {"dates": d.strftime("%Y%m%d"), "limit": 200})
        return data.get("events", [])
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] ESPN scoreboard {path} {d}: {e}", file=sys.stderr)
        return []


def espn_standings(client: httpx.Client, path: str) -> dict[str, dict]:
    """team id -> {overall, home, road, l10, streak} (only the fields ESPN provides)."""
    try:
        data, _ = get_json(client, f"{ESPN_STANDINGS}/{path}/standings")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] ESPN standings {path}: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}

    def walk(o):
        if isinstance(o, dict):
            for e in o.get("standings", {}).get("entries", []) if isinstance(o.get("standings"), dict) else []:
                tid = str(e.get("team", {}).get("id", ""))
                rec = {}
                for s in e.get("stats", []):
                    key = {"total": "overall", "home": "home", "road": "road",
                           "lasttengames": "l10", "streak": "streak"}.get(s.get("type", ""))
                    val = s.get("displayValue") or s.get("summary")
                    if key and val:
                        rec[key] = val.split(",")[0].strip()  # NHL: "8-1-1, 0 PTS" -> "8-1-1"
                if tid:
                    out[tid] = rec
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def espn_injuries(client: httpx.Client, path: str) -> dict[str, list[dict]]:
    """team displayName (normalized) -> list of {name, status, date}."""
    try:
        data, _ = get_json(client, f"{ESPN_SITE}/{path}/injuries")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] ESPN injuries {path}: {e}", file=sys.stderr)
        return {}
    out: dict[str, list[dict]] = {}
    for team in data.get("injuries", []) or []:
        lst = []
        for inj in team.get("injuries", []) or []:
            lst.append({"name": (inj.get("athlete") or {}).get("shortName") or (inj.get("athlete") or {}).get("displayName", "?"),
                        "status": (inj.get("status") or "").strip(), "date": inj.get("date", "")})
        out[norm(team.get("displayName", ""))] = lst
    return out


# --------------------------------------------------------------------------- matching helpers

def _espn_name_variants(team: dict) -> list[str]:
    loc, nick, disp = team.get("location", ""), team.get("name", ""), team.get("displayName", "")
    v = [disp, f"{loc} {nick}".strip()]
    v += [x.replace("LA ", "Los Angeles ", 1) for x in v if x.startswith("LA ")]
    return [x for x in v if x]


def espn_team_matches(row: TeamRow, odds_full: str | None, comp: dict) -> bool:
    t = comp.get("team", {})
    nick = norm(t.get("name", ""))
    if odds_full and nick and (norm(odds_full) == norm(t.get("displayName", "")) or norm(odds_full).endswith(" " + nick)):
        return True
    if any(team_matches(row.name, v) for v in _espn_name_variants(t)):
        return True
    return bool(row.abbr) and row.abbr.upper() == (t.get("abbreviation") or "").upper()


def find_espn_event(teams: list[TeamRow], odds_names: dict[str, str], events: list[dict]) -> dict | None:
    real = [t for t in teams if norm(t.name) not in DRAW_NAMES]
    hits = []
    for ev in events:
        comps = (ev.get("competitions") or [{}])[0].get("competitors", [])
        if len(comps) != 2:
            continue
        mapping = {}
        for row in real:
            m = [c for c in comps if espn_team_matches(row, odds_names.get(row.ticker), c)]
            if len(m) == 1:
                mapping[row.ticker] = m[0]
        if len(mapping) == len(real) == 2 and len({id(c) for c in mapping.values()}) == 2:
            hits.append((ev, mapping))
    return hits[0] if len(hits) == 1 else None


def injury_summary(lst: list[dict], today: date) -> dict:
    outs = [i for i in lst if i["status"].lower() in OUT_STATUSES]
    dtd = [i for i in lst if i["status"].lower() in DTD_STATUSES]
    recent = []
    for i in outs + dtd:
        try:
            d = datetime.fromisoformat(i["date"].replace("Z", "+00:00")).astimezone(ET).date()
            if (today - d).days <= 3:
                recent.append(i)
        except ValueError:
            pass
    return {"out": outs, "dtd": dtd, "recent": recent}


# --------------------------------------------------------------------------- board builder

def build_sport(client: httpx.Client, sport: str, dates: list[date], api_key: str | None, refresh: bool,
                log: list[str]) -> tuple[list[Game], list[str]]:
    cfg = SPORTS[sport]
    unmatched: list[str] = []
    markets = kalshi_markets(client, cfg["series"])
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        td = ticker_date(m.get("event_ticker", ""))
        if td in dates:
            by_event.setdefault(m["event_ticker"], []).append(m)
    log.append(f"{cfg['label']}: {len(markets)} open Kalshi markets, {len(by_event)} games on {', '.join(map(str, dates))}")
    if not by_event:
        return [], unmatched

    games: list[Game] = []
    for ev_t, ms in sorted(by_event.items()):
        rows = [TeamRow(ticker=m["ticker"], name=m.get("yes_sub_title") or m.get("title", "?"),
                        abbr=m["ticker"].rsplit("-", 1)[-1], bid=fnum(m.get("yes_bid_dollars")),
                        ask=fnum(m.get("yes_ask_dollars")), volume=fnum(m.get("volume_fp")) or 0.0) for m in ms]
        rows.sort(key=lambda r: (norm(r.name) in DRAW_NAMES, r.abbr))
        g = Game(sport=sport, event_ticker=ev_t, gdate=ticker_date(ev_t), teams=rows)
        occ = ms[0].get("occurrence_datetime")
        if occ:
            g.start_utc = datetime.fromisoformat(occ.replace("Z", "+00:00"))
            g.start_src = "kalshi"
        games.append(g)

    # ---- Pinnacle (1 credit)
    odds_names: dict[str, str] = {}
    if api_key:
        try:
            odds, how = pinnacle_odds(client, api_key, cfg["odds"], refresh)
            log.append(f"{cfg['label']}: Odds API {how}; {len(odds)} Pinnacle events")
        except Exception as e:  # noqa: BLE001
            odds = []
            log.append(f"{cfg['label']}: Odds API FAILED: {e}")
        for g in games:
            ev = match_event(g.event_ticker, [t.name for t in g.teams], odds)
            if not ev:
                unmatched.append(f"{cfg['label']} {g.event_ticker} ({' v '.join(t.name for t in g.teams)}): no unique Pinnacle event")
                continue
            book = next((b for b in ev.get("bookmakers", []) if b.get("key") == "pinnacle"), None)
            mk = next((m for m in (book or {}).get("markets", []) if m.get("key") == "h2h"), None)
            if not mk:
                unmatched.append(f"{cfg['label']} {g.event_ticker}: Pinnacle has no h2h line yet")
                continue
            outs = mk["outcomes"]
            try:
                probs = devig_power([float(o["price"]) for o in outs])
            except ValueError:
                unmatched.append(f"{cfg['label']} {g.event_ticker}: invalid Pinnacle odds")
                continue
            fair = {o["name"]: p for o, p in zip(outs, probs)}
            names = {t.ticker: outcome_name_for(t.name, ev) for t in g.teams}
            if any(n is None or n not in fair for n in names.values()) or len(set(names.values())) != len(g.teams):
                unmatched.append(f"{cfg['label']} {g.event_ticker}: could not map every Kalshi team to a Pinnacle outcome")
                continue
            g.odds_matched = True
            g.pinnacle_update = mk.get("last_update") or book.get("last_update")
            g.start_utc = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            g.start_src = "odds"
            for t in g.teams:
                t.fair = fair[names[t.ticker]]
                odds_names[t.ticker] = names[t.ticker]
                if names[t.ticker] == ev.get("home_team"):
                    t.home_away = "home"
                elif names[t.ticker] == ev.get("away_team"):
                    t.home_away = "away"
                else:
                    t.home_away = "-"
    else:
        log.append(f"{cfg['label']}: no THE_ODDS_API_KEY/ODDS_API_KEY -> no Pinnacle fair prices")

    # ---- ESPN context (free)
    standings = espn_standings(client, cfg["espn"])
    injuries = espn_injuries(client, cfg["espn"])
    sb_cache: dict[date, list[dict]] = {}

    def sb(d: date) -> list[dict]:
        if d not in sb_cache:
            sb_cache[d] = espn_scoreboard(client, cfg["espn"], d)
        return sb_cache[d]

    for g in games:
        hit = find_espn_event(g.teams, odds_names, sb(g.gdate))
        if not hit:
            unmatched.append(f"{cfg['label']} {g.event_ticker}: no unique ESPN scoreboard game (context skipped)")
            continue
        ev, mapping = hit
        g.espn_matched = True
        comp = ev["competitions"][0]
        st = comp.get("status", {}).get("type", {})
        g.status = {"pre": "pre", "in": "live", "post": "final"}.get(st.get("state", "pre"), "pre")
        g.status_detail = st.get("shortDetail", "")
        if ev.get("date"):
            g.start_utc = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            g.start_src = "espn"
        yesterday_ids = {str(c.get("team", {}).get("id")) for e in sb(g.gdate - timedelta(days=1))
                         for c in (e.get("competitions") or [{}])[0].get("competitors", [])}
        for t in g.teams:
            c = mapping.get(t.ticker)
            if not c:
                continue
            t.espn = c
            t.home_away = c.get("homeAway", t.home_away)
            tid = str(c.get("team", {}).get("id", ""))
            rec = dict(standings.get(tid, {}))
            for r in c.get("records", []) or []:  # fall back to scoreboard records
                key = {"total": "overall", "home": "home", "road": "road"}.get(r.get("type", ""))
                if key and key not in rec and r.get("summary"):
                    rec[key] = r["summary"]
            t.standing = rec
            if sport in ("nba", "nhl"):
                t.b2b = tid in yesterday_ids
            inj = injuries.get(norm(c.get("team", {}).get("displayName", "")))
            if inj is not None:
                t.injuries = injury_summary(inj, g.gdate)

    for g in games:
        g.notes = game_notes(g)
    games.sort(key=lambda g: (g.start_utc or datetime.max.replace(tzinfo=ET), g.event_ticker))
    return games, unmatched


def game_notes(g: Game) -> list[str]:
    n: list[str] = []
    if g.status != "pre":
        n.append(f"Game {g.status.upper()} ({g.status_detail}) -- prices are in-play, not a pre-game comparison")
    asks = [t.ask for t in g.teams if t.ask]
    if len(asks) == len(g.teams) and asks:
        n.append(f"Kalshi ask sum {sum(asks):.2f}")
    for t in g.teams:
        if t.fair is None:
            continue
        te, me = t.taker_edge, t.maker_edge
        if te is not None and te >= 0.01:
            n.append(f"Kalshi ask {te * 100:.1f}c below Pinnacle on {t.abbr}")
        elif me is not None and me >= 0.01:
            n.append(f"{t.abbr}: resting bid at {t.maker_price:.2f} would be {me * 100:.1f}c below Pinnacle (maker only)")
        if t.is_play(g.pregame):
            n.append(f"MODEL PLAY {t.abbr} maker @ {t.maker_price:.2f} (fair {t.fair:.3f})")
    if g.odds_matched and not any((t.maker_edge or -1) >= 0.01 for t in g.teams):
        n.append("no side priced >=1c below Pinnacle (no edge)")
    for t in g.teams:
        if t.b2b:
            n.append(f"{t.abbr} on 2nd night of back-to-back")
        s = t.standing.get("streak", "")
        if len(s) >= 2 and s[0] in "WL" and s[1:].isdigit() and int(s[1:]) >= 3:
            n.append(f"{t.abbr} streak {s}")
        if t.injuries:
            o, d, r = len(t.injuries["out"]), len(t.injuries["dtd"]), len(t.injuries["recent"])
            if o or d:
                part = f"{t.abbr} {o} OUT" + (f", {d} DTD/Q" if d else "")
                if r:
                    part += f" ({r} ESPN-updated in last 3 days: " + ", ".join(f"{i['name']} {i['status']}" for i in t.injuries["recent"][:3]) + ")"
                n.append(part)
        if t.spread is not None and t.spread >= 0.04:
            n.append(f"{t.abbr} wide spread {t.spread * 100:.0f}c")
    vol = sum(t.volume for t in g.teams)
    if vol < 500:
        n.append(f"thin Kalshi volume ({vol:.0f} contracts)")
    return n


# --------------------------------------------------------------------------- rendering

def fmt_p(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.3f}" if signed else f"{v:.2f}" if abs(v * 100 - round(v * 100)) < 1e-6 else f"{v:.3f}"


def start_pt(g: Game) -> str:
    if not g.start_utc:
        return "?"
    s = g.start_utc.astimezone(PT).strftime("%a %-I:%M%p").replace("AM", "a").replace("PM", "p")
    return s + ("~" if g.start_src == "kalshi" else "")


def rec_str(t: TeamRow) -> str:
    s = t.standing
    parts = []
    if s.get("overall"):
        parts.append(s["overall"])
    side = s.get("home") if t.home_away == "home" else s.get("road") if t.home_away == "away" else None
    if side:
        parts.append(("H " if t.home_away == "home" else "A ") + side)
    if s.get("l10"):
        parts.append("L10 " + s["l10"])
    if s.get("streak"):
        parts.append(s["streak"])
    return " ".join(parts) or "-"


def inj_str(t: TeamRow) -> str:
    if not t.injuries:
        return "-"
    o, d = len(t.injuries["out"]), len(t.injuries["dtd"])
    return f"{o}O/{d}Q" if (o or d) else "0"


COLS = ["Time PT", "Game", "Team", "H/A", "Bid", "Ask", "Sprd", "Vol", "Fair", "T-edge", "M-px", "M-edge", "Flag",
        "Record", "B2B", "Inj"]


def row_cells(g: Game, t: TeamRow) -> list[str]:
    flag = "MODEL PLAY" if t.is_play(g.pregame) else ("LIVE" if g.status == "live" else "FINAL" if g.status == "final" else "")
    return [start_pt(g), g.label(), t.abbr, {"home": "H", "away": "A"}.get(t.home_away, "-"),
            fmt_p(t.bid), fmt_p(t.ask), "-" if t.spread is None else f"{t.spread * 100:.0f}c",
            f"{t.volume:,.0f}", fmt_p(t.fair), fmt_p(t.taker_edge, True), fmt_p(t.maker_price),
            fmt_p(t.maker_edge, True), flag, rec_str(t), "Y" if t.b2b else ("N" if t.b2b is False else "-"), inj_str(t)]


def render_stdout(all_games: dict[str, list[Game]]) -> str:
    lines = []
    for sport, games in all_games.items():
        if not games:
            continue
        lines.append(f"\n=== {SPORTS[sport]['label']} ===")
        table = [COLS] + [row_cells(g, t) for g in games for t in g.teams]
        w = [max(len(r[i]) for r in table) for i in range(len(COLS))]
        prev = None
        for i, r in enumerate(table):
            if i > 0 and prev is not None and r[1] != prev:
                lines.append("")
            prev = r[1] if i > 0 else None
            lines.append("  ".join(c.ljust(w[j]) for j, c in enumerate(r)).rstrip())
        lines.append("\nNotes:")
        for g in games:
            lines.append(f"  {g.label():<12} " + "; ".join(g.notes))
    return "\n".join(lines)


def render_md(all_games: dict[str, list[Game]], meta: list[str], unmatched: list[str], title: str) -> str:
    out = [f"# {title}", "", "_Read-only research board. Nothing here places orders._", ""]
    out += ["## Run log", ""] + [f"- {m}" for m in meta] + [""]
    for sport, games in all_games.items():
        if not games:
            continue
        out += [f"## {SPORTS[sport]['label']}", "", "| " + " | ".join(COLS) + " |", "|" + "---|" * len(COLS)]
        for g in games:
            for t in g.teams:
                cells = row_cells(g, t)
                if cells[12] == "MODEL PLAY":
                    cells = [f"**{c}**" for c in cells]
                out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
        out += ["", "### Notes", ""] + [f"- **{g.label()}** ({g.event_ticker}): " + "; ".join(g.notes) for g in games] + [""]
    out += ["## Unmatched / gaps", ""] + ([f"- {u}" for u in unmatched] or ["- none"]) + [""]
    return "\n".join(out)


def render_html(all_games: dict[str, list[Game]], meta: list[str], unmatched: list[str], title: str) -> str:
    e = html.escape
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:12px;color:#111;background:#fafafa}}
h1{{font-size:1.25rem}} h2{{font-size:1.1rem;margin-top:1.5rem}}
.wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
table{{border-collapse:collapse;font-size:.82rem;min-width:900px;background:#fff}}
th,td{{padding:4px 7px;border-bottom:1px solid #e3e3e3;text-align:right;white-space:nowrap}}
th{{background:#f0f0f0;position:sticky;top:0}} td.l,th.l{{text-align:left}}
tr.gs td{{border-top:2px solid #999}} tr.play td{{background:#e6f7e6;font-weight:600}}
tr.live td{{color:#888}} .pos{{color:#08791c}} .neg{{color:#b00020}}
.notes li{{margin:3px 0;font-size:.85rem}} .muted{{color:#666;font-size:.8rem}}
</style></head><body><h1>{e(title)}</h1>
<p class="muted">Read-only research board &mdash; never places orders. Fair = Pinnacle power de-vig.
MODEL PLAY = pre-game, maker edge &ge; 2c and maker price 0.35&ndash;0.80 (live bot rule).</p>"""]
    for sport, games in all_games.items():
        if not games:
            continue
        parts.append(f"<h2>{e(SPORTS[sport]['label'])}</h2><div class='wrap'><table><tr>")
        parts.append("".join(f"<th class='{'l' if c in ('Time PT', 'Game', 'Team', 'Flag', 'Record') else ''}'>{e(c)}</th>" for c in COLS) + "</tr>")
        for g in games:
            for i, t in enumerate(g.teams):
                cells = row_cells(g, t)
                cls = " ".join(x for x in ["gs" if i == 0 else "", "play" if cells[12] == "MODEL PLAY" else "",
                                           "live" if g.status != "pre" else ""] if x)
                tds = []
                for j, c in enumerate(cells):
                    k = "l" if COLS[j] in ("Time PT", "Game", "Team", "Flag", "Record") else ""
                    if COLS[j] in ("T-edge", "M-edge") and c not in ("-", ""):
                        k += " pos" if c.startswith("+") and c != "+0.000" else " neg" if c.startswith("-") else ""
                    tds.append(f"<td class='{k.strip()}'>{e(c)}</td>")
                parts.append(f"<tr class='{cls}'>" + "".join(tds) + "</tr>")
        parts.append("</table></div><ul class='notes'>")
        parts += [f"<li><b>{e(g.label())}</b>: {e('; '.join(g.notes))}</li>" for g in games]
        parts.append("</ul>")
    parts.append("<h2>Unmatched / gaps</h2><ul class='notes'>" + ("".join(f"<li>{e(u)}</li>" for u in unmatched) or "<li>none</li>") + "</ul>")
    parts.append("<h2>Run log</h2><ul class='notes muted'>" + "".join(f"<li>{e(m)}</li>" for m in meta) + "</ul></body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Kalshi vs Pinnacle betting board")
    ap.add_argument("--sports", default="nba,nhl", help="comma list of nba,nhl,nfl,epl")
    ap.add_argument("--date", default=None, help="US Eastern game date YYYY-MM-DD (default: today ET)")
    ap.add_argument("--days", type=int, default=1, help="include this many days starting at --date (for NFL/EPL upcoming)")
    ap.add_argument("--refresh", action="store_true", help="ignore the 10-minute Odds API cache")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("THE_ODDS_API_KEY") or os.getenv("ODDS_API_KEY")
    start = date.fromisoformat(args.date) if args.date else datetime.now(ET).date()
    dates = [start + timedelta(days=i) for i in range(max(1, args.days))]
    sports = [s.strip().lower() for s in args.sports.split(",") if s.strip()]
    bad = [s for s in sports if s not in SPORTS]
    if bad:
        ap.error(f"unknown sport(s): {bad}; choose from {list(SPORTS)}")

    meta = [f"Generated {datetime.now(PT):%Y-%m-%d %I:%M %p} PT for ET date(s) {', '.join(map(str, dates))}"]
    all_games: dict[str, list[Game]] = {}
    unmatched: list[str] = []
    with httpx.Client(timeout=20, headers={"User-Agent": "betting-board/1.0 (read-only)"}) as client:
        for s in sports:
            try:
                games, um = build_sport(client, s, dates, api_key, args.refresh, meta)
            except Exception as ex:  # noqa: BLE001
                meta.append(f"{SPORTS[s]['label']}: FAILED ({ex})")
                games, um = [], []
            all_games[s] = games
            unmatched += um

    tag = start.isoformat() + (f"_{args.days}d" if args.days > 1 else "")
    title = f"Betting Board {tag}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_p, html_p = OUT_DIR / f"board_{tag}.md", OUT_DIR / f"board_{tag}.html"
    md_p.write_text(render_md(all_games, meta, unmatched, title))
    html_p.write_text(render_html(all_games, meta, unmatched, title))

    print(title)
    for m in meta:
        print("  " + m)
    print(render_stdout(all_games))
    print("\nUnmatched / gaps:")
    for u in unmatched or ["none"]:
        print("  " + u)
    print(f"\nWrote {md_p}\nWrote {html_p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
