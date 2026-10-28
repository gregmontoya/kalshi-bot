# Betting Board (read-only)

A "tonight's games" decision board for a human bettor. It **never places orders**: it only makes
public GET requests (Kalshi market data, The Odds API, ESPN) and writes reports. It is separate
from the live trading bot (`src/`) and only imports the pure helpers `src/recorder/devig.py` and
`src/recorder/matching.py`.

```bash
PYTHONPATH=. .venv/bin/python betting_board/board.py                     # NBA + NHL, today (US Eastern)
PYTHONPATH=. .venv/bin/python betting_board/board.py --sports nba,nhl,nfl,epl --date 2026-11-01
PYTHONPATH=. .venv/bin/python betting_board/board.py --sports nfl,epl --days 5   # upcoming window
```

Output: `betting_board/out/board_<date>.md`, `betting_board/out/board_<date>.html` (self-contained,
mobile-friendly, model plays highlighted green) and a compact table on stdout.

## Credits / budget
One Odds API call per sport (`/v4/sports/{sport}/odds?regions=eu&markets=h2h&bookmakers=pinnacle`)
= **1 credit per sport**, and the call is skipped if Kalshi has no games for that sport/date.
Responses are cached in `out/cache/` for 10 minutes (re-runs within 10 min cost 0 credits; use
`--refresh` to force). Credits remaining are printed in the run log. Key: `THE_ODDS_API_KEY` or
`ODDS_API_KEY` in `.env`.

## Columns
| Column | Meaning |
|---|---|
| Time PT | Start time in Pacific (ESPN, else Odds API; `~` = Kalshi's scheduled time, approximate) |
| Game / Team / H/A | Away @ Home, Kalshi team code (ticker suffix), home/away from ESPN or Odds API |
| Bid / Ask | Kalshi YES best bid / ask for "Team wins" (`yes_bid_dollars`, `yes_ask_dollars`) |
| Sprd | Ask − bid in cents |
| Vol | Kalshi lifetime contract volume for that market (`volume_fp`) |
| Fair | Pinnacle win probability, power de-vig (`devig_power`), all outcomes sum to 1.00 (EPL incl. draw) |
| T-edge | Taker edge = Fair − Ask (what you get crossing the spread; taker fees not included) |
| M-px | Maker price the live bot would rest at: bid + 1c if spread ≥ 2c, else ask − 1c (never crosses) |
| M-edge | Maker edge = Fair − M-px |
| Flag | `MODEL PLAY` (see rule), or `LIVE` / `FINAL` if ESPN says the game has started |
| Record | ESPN overall, home-or-road record (for the side the team is on tonight), last-10, streak |
| B2B | NBA/NHL: team also played yesterday (ESPN scoreboard for the previous date) |
| Inj | ESPN injury report: `O` = Out/IR/suspended, `Q` = day-to-day/questionable/doubtful |

Fields ESPN doesn't provide are shown as `-`; nothing is invented. Injury "ESPN-updated in last 3
days" uses the report's update date, a proxy for news the market may still be absorbing.

## The edge rule (`MODEL PLAY`)
Same rule as the live bot (`src/recorder/live_maker.py`):

* game is pre-game, and
* maker edge `Fair − M-px ≥ 0.02`, and
* maker price between `0.35` and `0.80`.

## Why Pinnacle is the reference price
Pinnacle is the sharpest widely available book. In the 6,200-game backtest Kalshi and Pinnacle were
**equally accurate on average** — buying Kalshi at the ask simply because it differs from Pinnacle
showed no edge. The only slice that showed edge was **buying ≥ 2c below Pinnacle with a resting
maker order** (+8% ROI, t = 1.84) — suggestive, **not yet statistically proven**. Treat MODEL PLAY
rows as candidates, not certainties; size small. Team context (records, B2B, injuries) is
informational and is generally already priced in by Pinnacle.

## Matching
Kalshi ↔ Pinnacle uses `src/recorder/matching.match_event` (both teams must match exactly one odds
event within ±1 day). Kalshi ↔ ESPN matches on the Odds API full name / ESPN nickname, ESPN display
name, or team abbreviation, and requires both teams to hit exactly one ESPN game. Anything that
fails is listed under **Unmatched / gaps** rather than guessed.
