"""Match Kalshi game markets to The Odds API events.

Kalshi uses short names ("Los Angeles C", "St. Louis", "New York I");
The Odds API uses full names ("Los Angeles Clippers", "St Louis Blues", "New York Islanders").
A match is only accepted when BOTH teams of the Kalshi event map to exactly one odds event
within +/- 1 day of the date encoded in the Kalshi event ticker. Ambiguous cases return None.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_ET = ZoneInfo("America/New_York")
DRAW_NAMES = {"tie", "draw"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_ALIASES = {
    "as": "athletics",  # Kalshi "A's" (norm strips the apostrophe)
    "man utd": "manchester united",
}


def team_matches(kalshi_name: str, full_name: str) -> bool:
    k, f = norm(kalshi_name), norm(full_name)
    if not k or not f:
        return False
    if k in _ALIASES:
        target = _ALIASES[k]
        return f == target or target in f.split()
    if f == k or f.startswith(k + " "):
        return True
    parts = k.split()
    # Archived NHL style: "OTT Senators", "CBJ Blue Jackets", "UTA Mammoth" -> code prefix + nickname suffix
    if len(parts) >= 2 and len(parts[0]) in (2, 3):
        rest = " ".join(parts[1:])
        if f.endswith(" " + rest):
            return True
        if parts[0] == "uta" and f.startswith("utah "):
            return True
    # "Los Angeles C" -> base "los angeles", nickname initial "c"; "Chicago WS" -> initials "ws"
    if len(parts) >= 2 and len(parts[-1]) <= 3:
        base, code = " ".join(parts[:-1]), parts[-1]
        if f.startswith(base + " "):
            nick = f[len(base) + 1:].split()
            if len(code) == 1:
                return nick[0].startswith(code)
            return "".join(w[0] for w in nick) == code
    return False


def ticker_date(event_ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def _et_date(iso_ts: str) -> date:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(_ET).date()


def match_event(event_ticker: str, kalshi_teams: list[str], odds_events: list[dict]) -> dict | None:
    """Return the single odds event matching both Kalshi teams near the ticker date, else None."""
    teams = [t for t in kalshi_teams if norm(t) not in DRAW_NAMES]
    tdate = ticker_date(event_ticker)
    if len(teams) != 2 or tdate is None:
        return None
    candidates = []
    for ev in odds_events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        a, b = teams
        if not ((team_matches(a, home) and team_matches(b, away)) or (team_matches(a, away) and team_matches(b, home))):
            continue
        delta = abs((_et_date(ev["commence_time"]) - tdate).days)
        if delta <= 1:
            candidates.append((delta, ev))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None  # ambiguous
    return candidates[0][1]


def outcome_name_for(kalshi_team: str, odds_event: dict) -> str | None:
    """Map a Kalshi yes_sub_title to the odds outcome name ('Draw' for ties)."""
    if norm(kalshi_team) in DRAW_NAMES:
        return "Draw"
    hits = [n for n in (odds_event.get("home_team", ""), odds_event.get("away_team", "")) if team_matches(kalshi_team, n)]
    return hits[0] if len(hits) == 1 else None
