# Kalshi Quant Multi-Strategy Engine — Workspace Rules & Spec

## 1. Non-Negotiable Exchange & Math Laws
- **Current Kalshi v2 Endpoints:**
  - Demo REST: `https://demo-api.kalshi.co/trade-api/v2`
  - Prod REST: `https://external-api.kalshi.com/trade-api/v2`
  - Prod WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- **RSA-PSS Signing:** Every authenticated request requires `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), and `KALSHI-ACCESS-SIGNATURE` (base64 RSA-PSS with SHA256 over `timestamp + METHOD + path_without_query`).
- **Reciprocal Order Book Law:** Kalshi `/markets/{ticker}/orderbook` ONLY returns `yes` bids and `no` bids. NEVER look for an `asks` key. Always compute:
  - `best_yes_ask = 100 - best_no_bid`
  - `best_no_ask = 100 - best_yes_bid`
- **Maker-First Execution:** Taker fees are `ceil(0.07 * contracts * P * (1 - P))`. Default all entries and take-profits to resting Limit Orders (`MAKER_ONLY`) so fees are $0.00 (or minimal Maker tier). Never cross the spread with market/taker orders unless executing an emergency stop-loss.
- **Mandatory Order Safety Flags:** All automated sell orders MUST pass `sell_position_floor: 0` (prevents accidental shorting) and `cancel_on_pause: true` (cancels resting orders if the market pauses on a scoring play).
- **No Native Combos:** NEVER use Kalshi's native Combo/RFQ parlay endpoints. All multi-leg groupings must be executed as **Synthetic Baskets** on individual single-game order books so individual legs can be sold mid-game.
- **Vegas De-Vigging:** Always strip sportsbook vig (multiplicative or Shin method) from The Odds API (`pinnacle`, `circa`) so implied probabilities sum to 100.0% before comparing to Kalshi prices.

## 2. Core Strategy Pods (Run in `PAPER` or `LIVE` mode)
1. **Pod 1 (`maker_momentum_basket`):**
   - Scan for contracts trending up into the `68c to 76c` zone where de-vigged sharp Vegas probability >= `kalshi_ask + 3c`.
   - Group 2 to 3 confirmed plays into a tracked `BasketID` in DuckDB/SQLite.
   - Enter via resting Maker limit bids 2c below current price to catch dip fills.
   - Immediately upon fill, rest a Maker Take-Profit sell order at `88c to 92c`.
   - **Vegas-Confirmed <50c Guillotine:** If any basket leg drops <= 48c, check live de-vigged sharp probability. If sharp prob is still >= 58%, HOLD (ignore retail noise). If sharp prob also dropped < 50%, fire immediate exit on that leg.
2. **Pod 2 (`sharp_value_green_out`):**
   - Pre-game Maker limit buy (38c to 58c) when de-vigged sharp probability exceeds Kalshi price by >= 5c.
   - **Mid-Game Green Out:** When position rises +16c (e.g., opening drive TD), calculate exact contracts to sell (or opposing `NO` to buy) to lock in equal guaranteed profit regardless of final outcome.
3. **Pod 3 (`tail_lock_harvester`):**
   - Scan 4th quarter contracts at `95c to 97c` ONLY when live ESPN clock/possession/timeout feed confirms mathematical win probability >= 99.2%.

## 3. Four-Phase Verifiable Implementation Roadmap
- **Phase 1 (Active):**
  - Scaffold `src/` directory layout, `.env.example`, `requirements.txt`.
  - `src/core/auth.py` (RSA-PSS request signing & key management).
  - `src/core/orderbook.py` (Reciprocal $100 - \text{bid}$ math & ask ladder synthesis).
  - `src/core/fee_math.py` (Maker/Taker fees, Shin de-vigging, Mid-Game Green-Out equal profit math).
  - Unit tests verifying 100% of mathematical and cryptographic laws.
- **Phase 2:** Exchange clients (REST & WebSocket), paper simulator, feeds (The Odds API, ESPN scoreboard, Open-Meteo).
- **Phase 3:** Strategy pods (Pod 1 Momentum Basket, Pod 2 Green Out, Pod 3 Tail-Lock Harvester), DuckDB persistence, BigQuery grader.
- **Phase 4:** Telegram Bot inline controller, FastAPI server, Astro.js PWA Dashboard.
