# Kalshi Quant Multi-Strategy Engine

A high-performance algorithmic trading and quant execution engine built for Kalshi event contracts. Strictly implements exchange-level quantitative laws, Vegas de-vigging, multi-pod strategy execution, and real-time risk controls.

---

## 1. Non-Negotiable Exchange & Math Laws

### Current Kalshi v2 Endpoints
- **Demo REST:** `https://demo-api.kalshi.co/trade-api/v2`
- **Prod REST:** `https://external-api.kalshi.com/trade-api/v2`
- **Prod WebSocket:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- **Demo WebSocket:** `wss://demo-api.kalshi.co/trade-api/ws/v2`

### RSA-PSS Request Signing
Every authenticated request requires:
- `KALSHI-ACCESS-KEY`: API Key ID
- `KALSHI-ACCESS-TIMESTAMP`: Milliseconds string (`time.time() * 1000`)
- `KALSHI-ACCESS-SIGNATURE`: Base64 RSA-PSS with SHA256 over:
  $$\text{Message} = \text{timestamp} + \text{METHOD} + \text{path\_without\_query}$$
Implemented in [`engine/core/auth.py`](file:///Users/gregmontoya/Documents/My_AI_Workspaces/Sports%20Predictor/engine/core/auth.py).

### Reciprocal Order Book Law
Kalshi `/markets/{ticker}/orderbook` **ONLY** returns `yes` bids and `no` bids. **Never** looks for an `asks` key. Always computes:
- $\text{best\_yes\_ask} = 100 - \text{best\_no\_bid}$
- $\text{best\_no\_ask} = 100 - \text{best\_yes\_bid}$

Full reciprocal depth ladders are synthesized in [`engine/core/orderbook.py`](file:///Users/gregmontoya/Documents/My_AI_Workspaces/Sports%20Predictor/engine/core/orderbook.py).

### Maker-First Execution Law
Taker fees are:
$$\text{Taker Fee (cents)} = \left\lceil 0.07 \times \text{contracts} \times \frac{P}{100} \times \left(1 - \frac{P}{100}\right) \times 100 \right\rceil$$
All entries and take-profits default to resting Limit Orders (`post_only: true`, `MAKER_ONLY`) so exchange fees are **$0.00**. Never crosses the spread with market orders unless executing an emergency stop-loss.

### Mandatory Order Safety Flags
- `sell_position_floor: 0`: Passed on all sell orders to mathematically prevent accidental naked shorting.
- `cancel_on_pause: true`: Automatically cancels resting orders if the market pauses on a scoring play or injury timeout.

### No Native Combos
**Never** uses Kalshi's native Combo/RFQ parlay endpoints. All multi-leg groupings are executed as **Synthetic Baskets** on individual single-game order books so each leg can be managed or guillotined mid-game independently.

### Vegas De-Vigging
Strips sportsbook vig from The Odds API (`pinnacle`, `circa`) using **Shin's model** (accounting for informed insider trading) and **Multiplicative normalization** so implied probabilities sum to exactly $100.0\%$ before comparing to Kalshi prices. Implemented in [`engine/core/math_engine.py`](file:///Users/gregmontoya/Documents/My_AI_Workspaces/Sports%20Predictor/engine/core/math_engine.py).

---

## 2. Core Strategy Pods

### Pod 1: `maker_momentum_basket`
1. Scans for contracts trending up into the **68¢ to 76¢** zone where de-vigged sharp Vegas probability $\ge \text{kalshi\_ask} + 3¢$.
2. Groups 2 to 3 confirmed plays into a tracked `BasketID` in DuckDB.
3. Enters via resting Maker limit bids **2¢ below current price** to catch dip fills ($0.00 maker fee).
4. Immediately upon fill, rests a Maker Take-Profit sell order at **88¢ to 92¢** (default 90¢).
5. **Vegas-Confirmed <50¢ Guillotine**:
   - If any basket leg drops $\le 48¢$, queries live de-vigged sharp probability.
   - If sharp prob is still $\ge 58\% \implies$ **HOLD** (ignores retail panic noise).
   - If sharp prob also dropped $< 50\% \implies$ fires immediate exit on that leg.

### Pod 2: `sharp_value_green_out`
1. Pre-game Maker limit buy in the **38¢ to 58¢** zone when de-vigged sharp probability exceeds Kalshi price by $\ge 5¢$.
2. **Mid-Game Green Out**: When position rises **+16¢** (e.g. opening drive touchdown):
   - Calculates exact contracts to sell (or opposing NO to buy) to lock in equal guaranteed profit regardless of final outcome:
   $$\Pi_{\text{guaranteed}} = \text{contracts} \times (P_{\text{current}} - P_{\text{entry}})$$

### Pod 3: `tail_lock_harvester`
1. Scans 4th quarter contracts priced at **95¢ to 97¢**.
2. Queries the ESPN Hidden Scoreboard API for live quarter, game clock, possession, and timeouts.
3. Mathematical kneel-down clock physics confirms win probability $\ge 99.2\%$:
   - If leading team possesses ball and $T_{\text{remaining}} \le (\text{kneels\_available} \times 40\text{s}) - (TO_{\text{opp}} \times 40\text{s})$, win probability is mathematically **100.0%**.
4. Enters resting Maker buy at 95¢–96¢ to harvest the remaining 4¢–5¢ premium with zero taker fee.

---

## 3. Directory Layout & Architecture

```
.
├── engine/
│   ├── config.py               # Pydantic Settings (Demo/Prod URLs, RSA keys, Pod thresholds)
│   ├── main.py                 # CLI & Orchestrator entry point
│   ├── core/
│   │   ├── auth.py             # RSA-PSS request signing & key utilities
│   │   ├── orderbook.py        # Reciprocal Order Book Law & ask synthesis
│   │   ├── math_engine.py      # Shin/Multiplicative de-vig, fees, green-out, tail-lock math
│   │   └── models.py           # Pydantic schemas (Order, Market, Basket, Position, etc.)
│   ├── exchange/
│   │   ├── kalshi_client.py    # Authenticated REST client + safety flags + maker-first
│   │   ├── kalshi_ws.py        # WebSocket streaming client for live orderbook & trade ticks
│   │   └── simulator.py        # High-fidelity Paper Trading engine with fee accounting
│   ├── feeds/
│   │   ├── odds_api.py         # The Odds API v4 (Pinnacle/Circa) + de-vigging
│   │   ├── espn_hidden_api.py  # ESPN hidden scoreboard (clock, possession, timeouts, win prob)
│   │   └── open_meteo.py       # Stadium weather & wind speed feed
│   ├── pods/
│   │   ├── base.py             # Base Strategy Pod lifecycle
│   │   ├── maker_momentum_basket.py # Pod 1
│   │   ├── sharp_value_green_out.py # Pod 2
│   │   └── tail_lock_harvester.py   # Pod 3
│   ├── storage/
│   │   ├── duckdb_manager.py   # Columnar embedded storage for baskets, legs, fills, odds
│   │   ├── firebase_sync.py    # Live UI sync with Firestore (or local in-memory fallback)
│   │   └── bigquery_grader.py  # Nightly 3:00 AM Brier Score & CLV self-learning grader
│   ├── notifications/
│   │   └── telegram_bot.py     # Telegram Bot with inline action buttons
│   └── api/
│       └── server.py           # FastAPI dashboard server
├── frontend/                   # Astro.js + Tailwind CSS + PWA Quant Dashboard
│   ├── package.json
│   ├── astro.config.mjs
│   ├── tailwind.config.cjs
│   └── src/
│       ├── layouts/Layout.astro
│       ├── components/         # Reciprocal book visualizer, Pod 1/2/3 cards, Grader, Weather
│       └── pages/index.astro
└── tests/
    ├── test_auth.py            # RSA-PSS signing & verification tests
    ├── test_orderbook.py       # Reciprocal orderbook & ask synthesis tests
    ├── test_math_engine.py     # Shin de-vig, fee calc, green-out, tail-lock tests
    ├── test_pods.py            # Pod 1, Pod 2, Pod 3 strategy rule tests
    └── test_simulator.py       # Paper simulator matching & safety flag tests
```

---

## 4. Quick Start

### 1. Setup Virtual Environment
```bash
# Python 3.12+
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your Kalshi RSA Key ID, Telegram Bot token, or Odds API key (optional for paper mode)
```

### 3. Generate RSA-PSS Key Pair (if needed)
```bash
.venv/bin/python engine/main.py generate-keys
```

### 4. Run Unit Test Suite
```bash
.venv/bin/python -m pytest tests/ -v
# 21 unit tests covering RSA-PSS signing, reciprocal order books, Shin de-vigging, Pod 1/2/3 rules
```

### 5. Start the Engine & PWA Dashboard
```bash
.venv/bin/python engine/main.py start --mode PAPER --port 8000
```
Open **`http://127.0.0.1:8000`** in your browser to view the interactive quant dashboard.

### 6. CLI Commands
```bash
# Strip vig from sports lines using Shin and Multiplicative methods
.venv/bin/python engine/main.py devig 1.50 2.70
.venv/bin/python engine/main.py devig -150 +130

# Run a single scan cycle across all 3 strategy pods
.venv/bin/python engine/main.py scan
```

---

## 5. Telegram Bot Controls
When Telegram credentials are configured, the bot provides interactive buttons:
- `[📊 Status]`: Returns engine mode, portfolio cash balance, and open pod items.
- `[💰 Green Out All]`: Executes equal guaranteed profit green out across all eligible positions.
- `[⏸️ Pause Bot]` / `[▶️ Resume Bot]`: Pauses or resumes background scanners and order placements.
- `[⚡ Force Guillotine Leg]`: Manually cuts any leg dipping into distress.
