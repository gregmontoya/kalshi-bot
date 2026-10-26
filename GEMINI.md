================================================================================
KALSHI QUANTITATIVE MULTI-STRATEGY ENGINE — MASTER HIGHLIGHTS & BUILD SPEC
================================================================================

1. SYSTEM OVERVIEW & CORE PHILOSOPHY
--------------------------------------------------------------------------------
We are building an automated, 24/7 quantitative sports and prediction market
trading engine connected to the Kalshi v2 Exchange API, paired with a mobile
command center (Telegram Bot) and a live web dashboard (Astro + Tailwind +
Shadcn UI + Tremor PWA).

Why This Works on Kalshi (vs. Traditional Sportsbooks):
- Kalshi is a CFTC-regulated Central Limit Order Book (CLOB) exchange. Winning
  accounts and automated bots are never throttled or banned.
- We act as the "Price Maker" (posting resting limit orders) rather than the
  "Price Taker" (betting into house juice).
- We can freely buy, scale out, hedge ("Green Out"), or cut losing tickets at
  any second pre-game or mid-game without sportsbook cash-out penalties.


2. NON-NEGOTIABLE EXCHANGE & MATH LAWS (CRITICAL FOR CODING)
--------------------------------------------------------------------------------
* LAW 1: Reciprocal Order Book Math ("Bids Only" Quirk)
  Kalshi's `/markets/{ticker}/orderbook` endpoint returns ONLY `yes` bids and
  `no` bids (under `orderbook_fp` using `yes_dollars` and `no_dollars` fixed-
  point strings like "0.5200"). Never look for an `asks` array.
  Always calculate implied asks as:
  - best_yes_ask = 1.00 - best_no_bid  (or 100 - best_no_bid in cents)
  - best_no_ask  = 1.00 - best_yes_bid (or 100 - best_yes_bid in cents)

* LAW 2: Fixed-Point Dollar Strings (API Format)
  Kalshi v2 uses fixed-point dollar strings (e.g., "0.7200") and `*_fp` contract
  count strings rather than legacy integer cent fields. Parse all incoming
  prices as Decimal/float values and format outgoing orders accordingly.

* LAW 3: Maker-First Execution (3.5% vs. 0% Fee Edge)
  - Taker Fee Formula: round_up(0.07 * contracts * P * (1 - P)). At a $0.50
    contract, paying Taker fees costs $1.75 per 100 contracts (3.5% each way,
    nearly 7% round-trip).
  - Maker Fee: $0.00 on most markets (or 0.0175 multiplier on select series).
  - Rule: All entries and take-profit targets MUST default to resting Limit
    Orders (`MAKER_ONLY`) placed inside the spread or 1 to 3 cents below the
    current ask. Never cross the spread with Taker orders except on emergency
    stop-loss exits.
  - Batching: Avoid buying 1 single contract at a time due to whole-cent fee
    rounding; batch in sizes of 10, 25, or 50+ contracts.

* LAW 4: Vegas Vig Removal (De-Vigging)
  Standard sportsbook lines (-110 / -110) sum to 104.76% implied probability.
  Before comparing sharp sportsbook odds (Pinnacle, Circa) to Kalshi prices,
  strip the bookmaker vigorish using Multiplicative or Shin de-vigging so
  both sides sum to exactly 100.0%.

* LAW 5: Never Use Native Kalshi "Combos" (Parlays)
  Native Kalshi Combos use a locked Request-for-Quote (RFQ) system and CANNOT
  be sold or exited early once filled. All multi-leg plays must be executed as
  "Synthetic Baskets" on individual single-game order books so any failing leg
  can be sold mid-game.

* LAW 6: Mandatory API Order Safety Flags
  - `sell_position_floor: 0` on all sell orders so the bot can never
    accidentally over-sell and flip into a short position.
  - `cancel_on_pause: true` so resting limit orders automatically cancel if the
    exchange pauses a market during a touchdown, turnover, or injury review.
  - Game-State Switch: Rest Maker orders pre-game, during commercial breaks, and
    at halftime; widen spreads or use IOC/FOK during live play to avoid getting
    sniped by low-latency stadium feeds.


3. THE 3 CORE STRATEGY PODS (CONFIGURED VIA `config/strategies.yaml`)
--------------------------------------------------------------------------------
Each pod runs independently in either `PAPER` (Sandbox/Shadow) or `LIVE` mode
with its own isolated scoreboard, P&L, Brier Score, and Closing Line Value.

* POD 1: `maker_momentum_basket` (Simultaneous 3-Leg 70%–78% Synthetic Baskets)
  - Target: Contracts trending upward into the $0.68 to $0.76 zone.
  - Two-Key Filter:
    1) Price is stepping up with positive order book buying pressure.
    2) De-vigged sharp Vegas probability is >= Kalshi price + $0.03.
  - Grouping: Group 2 to 3 confirmed plays into a tracked `BasketID` in the
    local database.
  - Entry: Post resting Maker limit bids $0.02 below current ask to catch
    micro-dips without paying Taker fees.
  - Auto Take-Profit Queue: As soon as an entry fills, immediately place a
    resting Maker sell limit order at $0.88 to $0.92 in the order book queue.
  - Vegas-Confirmed <$0.50 Guillotine (Smart Stop-Loss):
    If any leg in the basket drops to <= $0.48, check live de-vigged sharp
    sportsbook odds in milliseconds:
    * If sharp Vegas probability is still >= 58%, HOLD (ignore retail panic).
    * If sharp Vegas probability also crashed < 50% (thesis is broken), fire an
      immediate sell order on that leg to salvage ~50% of stake while the other
      2 winning basket legs keep the overall basket profitable.

* POD 2: `sharp_value_green_out` (Pre-Game Value + Mid-Game Profit Lock)
  - Entry: Pre-game Maker limit buy ($0.38 to $0.58 range) when de-vigged
    Pinnacle/Circa probability exceeds Kalshi price by >= $0.05.
  - Mid-Game "Green Out" (Equal-Profit Hedge): When an open position rises by
    +$0.16 (e.g., our underdog scores first and moves from $0.45 to $0.65,
    meaning the opposing `NO` contract is now $0.35), automatically sell half
    or buy the exact number of opposing `NO` contracts so that a guaranteed
    profit is locked in regardless of who wins the game in the 4th quarter.

* POD 3: `tail_lock_harvester` (Late 4th Quarter 99%+ Mathematical Locks)
  - Warning: Never blindly buy $0.95 contracts without a model check (1 miracle
    comeback wipes out 19 wins).
  - Rule: Scan 4th quarter contracts trading at $0.95 to $0.97 ONLY when live
    ESPN game-state data (possession, clock < 2 mins, lead, opponent timeouts
    == 0) confirms true mathematical win probability is >= 99.2%. Rest Maker
    bids to scoop panic-sellers and hold 5 minutes to $1.00 settlement.


4. SELF-LEARNING & INSTITUTIONAL FEATURES
--------------------------------------------------------------------------------
1. Closing Line Value (CLV) Truth Meter: Record the price we paid vs. the final
   Kalshi closing price 1 minute before kickoff. Beating the closing line proves
   mathematical +EV before the game even starts.
2. Nightly 3:00 AM Self-Learning Grader: Compute the Brier Score
   BS = (1/N) * sum((forecast - outcome)^2) across settled trades. Apply
   Exponential Decay weighting to team/player trends (60% last 3 games, 30%
   games 4-6, 10% older games) and auto-adjust strategy confidence weights.
3. Order Book Imbalance ("Whale Tape Reader"): Trigger alerts when bid vs. ask
   dollar depth exceeds a 10:1 ratio on a game line or total.
4. Stadium Weather & Wind Integration: Pull Open-Meteo wind gusts for outdoor
   NFL, College Football, and PGA Golf venues. Auto-downgrade passing/scoring
   projections when sustained winds cross 15+ mph.
5. Strategy Paper Shadowing: Test new strategy pods in `PAPER` mode against real
   order books and promote to `LIVE` only after proving positive CLV and Brier
   accuracy over 100+ games.


5. REQUIRED APIS, INFRASTRUCTURE & TECH STACK
--------------------------------------------------------------------------------
- Exchange API: Kalshi API v2 (Free Basic & Advanced Tiers: 20-30 reads/writes
  per second).
  * Demo REST: `https://demo-api.kalshi.co/trade-api/v2`
  * Prod REST: `https://external-api.kalshi.com/trade-api/v2`
  * Prod WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
  * Auth: RSA-PSS (SHA-256) or Ed25519 signature over `timestamp_ms + METHOD +
    path_without_query`. Headers: `KALSHI-ACCESS-KEY`,
    `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.
- Sharp Odds Feed: The Odds API v4 (`https://api.the-odds-api.com/v4/sports/...`)
  polling Pinnacle, Circa, DraftKings, and FanDuel.
- Live Scoreboard & Trends: ESPN Hidden Scoreboard API (free live clock, down,
  distance, timeouts) + `nfl_data_py` and `nba_api`.
- Weather Feed: Open-Meteo API (`https://api.open-meteo.com/v1/forecast` - free).
- Mobile Command Center: Telegram Bot API (`@BotFather`) with push alerts and
  inline interactive buttons (`[Green Out All]`, `[Pause Bot]`, `[Positions]`).
- Backend & Databases: Python 3.12+ (`httpx`, `websockets`, `cryptography`,
  `pydantic`), DuckDB/SQLite (sub-millisecond local basket state), Firebase
  Firestore (real-time UI sync), and Google BigQuery (historical candlesticks &
  nightly 3:00 AM Brier/CLV audits).
- Frontend Web App: Astro.js + Tailwind CSS + Shadcn UI + Tremor Charts saved
  to iPhone Home Screen as a Progressive Web App (PWA).


6. DIRECTORY STRUCTURE FOR GOOGLE ANTIGRAVITY
--------------------------------------------------------------------------------
kalshi-quant-engine/
├── GEMINI.md                    # Workspace rules & spec
├── .env.example                 # API keys, demo/live toggle, max drawdown caps
├── config/
│   └── strategies.yaml          # Hot-reloadable rules for all Strategy Pods
├── secrets/
│   └── kalshi_private_key.pem   # Git-ignored RSA/Ed25519 private key
├── src/
│   ├── core/
│   │   ├── auth.py              # RSA-PSS / Ed25519 request signer
│   │   ├── client.py            # Async REST & WebSocket client for Kalshi v2
│   │   ├── orderbook.py         # Reciprocal `1.00 - bid` parser & imbalance
│   │   └── fee_math.py          # Maker/Taker fee & Green-Out hedge calculator
│   ├── feeds/
│   │   ├── odds_api.py          # The Odds API poller + multiplicative de-vig
│   │   ├── espn_live.py         # Real-time clock, timeouts, possession
│   │   ├── weather.py           # Open-Meteo stadium wind & rain feed
│   │   └── entity_mapper.py     # Maps Kalshi tickers to external game IDs
│   ├── strategies/
│   │   ├── base.py              # Abstract StrategyPod class (Paper vs Live)
│   │   ├── basket_pod.py        # Pod 1: 70-78c 3-Leg Maker Baskets & <50c stop
│   │   ├── green_out_pod.py     # Pod 2: Sharp Pre-Game + Equal-Profit Hedge
│   │   └── tail_lock_pod.py     # Pod 3: 4Q 99.2%+ mathematical lock scanner
│   ├── risk/
│   │   ├── circuit_breaker.py   # Daily drawdown cap, sell_position_floor: 0
│   │   └── clv_grader.py        # Nightly 3:00 AM Brier Score & CLV grader
│   ├── notifications/
│   │   └── telegram_bot.py      # Push alerts & interactive inline buttons
│   ├── db/
│   │   ├── local_store.py       # DuckDB/SQLite state for active baskets/orders
│   │   └── cloud_sync.py        # Firestore & BigQuery sync for PWA & audits
│   └── main.py                  # Async event loop orchestrator
└── dashboard/                   # Astro.js + Tailwind + Shadcn UI + Tremor PWA
