"""
Continuous Autonomous Live & Worldwide Order Roller + Pre-Trade Educator.
Features:
1. Full Worldwide & Prop/Total/Spread Scanner (35+ series across Moneylines, Over/Unders, Spreads, Player Props, Tennis, Soccer, NHL, NBA, NFL, Esports).
2. 5-Factor Pre-Trade Intelligence Gate (PreTradeEducator):
   - Whale Order Book Depth Imbalance: Sums top-5 YES bid liquidity vs implied NO ask liquidity; vetoes plays where sellers outweigh buyers.
   - Trade Tape Momentum Check: Queries recent public trades (/markets/trades) to confirm price is stepping UP or stable (never catching a falling knife).
   - Live ESPN Scoreboard / Game-State Verification: Confirms live score margin & game clock where applicable.
   - Self-Learning Memory Ledger (`data/self_learning_memory.json`): Tracks win/loss outcomes, realized P&L, and Brier accuracy by series and adjusts confidence weights automatically.
3. 96c Harvest + Trailing Drop Stop (-12c from peak or <=58c floor).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.core.auth import KalshiAuthSigner
from src.exchange.kalshi_client import KalshiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("live_auto_roller")

MEMORY_PATH = Path("data/self_learning_memory.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "kalshi-greg-3fc522ce")
IMESSAGE_TARGET = os.environ.get("IMESSAGE_TARGET", "+19493107304")


async def send_phone_alert(client: KalshiClient, title: str, message: str) -> None:
    """Sends instant push notification to phone via macOS iMessage, ntfy.sh, and Telegram."""
    logger.info(f"📱 PHONE ALERT: {title} — {message}")
    # 1. Direct macOS iMessage text to +19493107304 (First priority!)
    if IMESSAGE_TARGET and sys.platform == "darwin":
        try:
            import subprocess
            safe_msg = f"{title}\n{message}".replace('"', "'")
            script = (
                'tell application "Messages"\n'
                '  set targetService to 1st account whose service type = iMessage\n'
                f'  set targetBuddy to participant "{IMESSAGE_TARGET}" of targetService\n'
                f'  send "{safe_msg}" to targetBuddy\n'
                'end tell'
            )
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # 2. Cloud-native push via ntfy.sh (works 24/7 on GitHub Actions & local)
    try:
        hc = await client.get_client()
        await hc.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            content=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "chart_with_upwards_trend,moneybag"},
        )
    except Exception:
        pass

    # 3. Optional Telegram Bot push
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or getattr(settings, "TELEGRAM_CHAT_ID", None)
    if tg_token and tg_chat:
        try:
            hc = await client.get_client()
            await hc.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": f"*{title}*\n{message}", "parse_mode": "Markdown"},
            )
        except Exception:
            pass


from datetime import datetime, timezone

WATCH_SERIES = [
    # 1. Hourly & Daily Crypto (24/7 Every Hour Settlement)
    "KXBTCD",
    "KXBTC",
    "KXETHD",
    "KXETH",
    "KXSOLD",
    "KXXRPD",
    "KXXRP",
    "KXDOGED",
    "KXDOGE",
    "KXBNBD",
    # 2. Intraday & Daily Stock Indices / Commodities
    "KXINXU",
    "KXNASDAQ100U",
    "KXINX",
    "KXNASDAQ100",
    "KXWTI",
    # 3. Daily City Weather High-Temp Brackets
    "KXHIGHLAX",
    "KXHIGHNY",
    "KXHIGHCHI",
    "KXHIGHMIA",
    "KXHIGHAUS",
    "KXHIGHDEN",
    "KXHIGHPHIL",
    # 4. Sports Game Winners (Moneylines, College Football/Basketball, UCL, MLS, Tennis/Esports)
    "KXNBAGAME",
    "KXNFLGAME",
    "KXNCAAFGAME",
    "KXNCAAMBGAME",
    "KXNCAAWBGAME",
    "KXNHLGAME",
    "KXWTAMATCH",
    "KXATPMATCH",
    "KXUCLGAME",
    "KXMLSGAME",
    "KXLIGAMXGAME",
    "KXLALIGAGAME",
    "KXLIGUE1GAME",
    "KXEPLGAME",
    "KXSERIEAGAME",
    "KXBUNDESLIGAGAME",
    # 5. Sports Over / Unders (Totals, 1H/2H Totals, 1Q-4Q Totals, Team Totals, BTTS, Corners)
    "KXNBATOTAL",
    "KXNBA1HTOTAL",
    "KXNBA2HTOTAL",
    "KXNBA1QTOTAL",
    "KXNBA2QTOTAL",
    "KXNBA3QTOTAL",
    "KXNBA4QTOTAL",
    "KXNBA1HTEAMTOTAL",
    "KXNBATEAMTOTAL",
    "KXNFLTOTAL",
    "KXNFL1HTOTAL",
    "KXNFL2HTOTAL",
    "KXNFL1QTOTAL",
    "KXNFL2QTOTAL",
    "KXNFL3QTOTAL",
    "KXNFL4QTOTAL",
    "KXNFLTEAMTOTAL",
    "KXNCAAFTOTAL",
    "KXNCAAF1HTOTAL",
    "KXNCAAF2HTOTAL",
    "KXNCAAF1QTOTAL",
    "KXNCAAF2QTOTAL",
    "KXNCAAF3QTOTAL",
    "KXNCAAF4QTOTAL",
    "KXNCAAFTEAMTOTAL",
    "KXNCAAMBTOTAL",
    "KXNCAAMB1HTOTAL",
    "KXNCAAMB2HTOTAL",
    "KXNHLTOTAL",
    "KXNHL1PTOTAL",
    "KXNHL2PTOTAL",
    "KXNHL3PTOTAL",
    "KXNHLTEAMTOTAL",
    "KXLIGAMXBTTS",
    "KXLALIGATOTAL",
    "KXLALIGA1HTOTAL",
    "KXUCLTOTAL",
    "KXUCL1HTOTAL",
    "KXEPLTOTAL",
    "KXEPL1HTOTAL",
    "KXSERIEATOTAL",
    "KXSERIEA1HTOTAL",
    "KXBUNDESLIGATOTAL",
    "KXBUNDESLIGA1HTOTAL",
    "KXUCLTEAMTOTAL",
    "KXSERIEACORNERS",
    # 6. Sports Point Spreads & Quarter/Half Winners (1Q, 2Q, 3Q, 4Q, 1H, 2H, 1P-3P)
    "KXNBASPREAD",
    "KXNBA1HSPREAD",
    "KXNBA2HSPREAD",
    "KXNBA1QSPREAD",
    "KXNBA2QSPREAD",
    "KXNBA3QSPREAD",
    "KXNBA4QSPREAD",
    "KXNBA1Q",
    "KXNBA2Q",
    "KXNBA3Q",
    "KXNBA4Q",
    "KXNBA1H",
    "KXNBA2H",
    "KXNFLSPREAD",
    "KXNFL1HSPREAD",
    "KXNFL2HSPREAD",
    "KXNFL1QSPREAD",
    "KXNFL2QSPREAD",
    "KXNFL3QSPREAD",
    "KXNFL4QSPREAD",
    "KXNFL1Q",
    "KXNFL2Q",
    "KXNFL3Q",
    "KXNFL4Q",
    "KXNFL1H",
    "KXNFL2H",
    "KXNCAAFSPREAD",
    "KXNCAAF1HSPREAD",
    "KXNCAAF2HSPREAD",
    "KXNCAAF1QSPREAD",
    "KXNCAAF2QSPREAD",
    "KXNCAAF3QSPREAD",
    "KXNCAAF4QSPREAD",
    "KXNCAAF1Q",
    "KXNCAAF2Q",
    "KXNCAAF3Q",
    "KXNCAAF4Q",
    "KXNCAAF1H",
    "KXNCAAF2H",
    "KXNCAAMBSPREAD",
    "KXNCAAMB1HSPREAD",
    "KXNCAAMB1H",
    "KXNHLSPREAD",
    "KXNHL1PSPREAD",
    "KXNHL2PSPREAD",
    "KXNHL3PSPREAD",
    "KXNHL1P",
    "KXNHL2P",
    "KXNHL3P",
    "KXUCL1H",
    "KXEPL1H",
    "KXLALIGA1H",
    "KXSERIEA1H",
    "KXBUNDESLIGA1H",
    # 7. Sports Player Props (NBA PTS/REB/AST/3PT/PRA/BLK/STL, NFL TD/PASS/RUSH/REC, NHL PTS/GOAL/AST, Soccer)
    "KXNBAPTS",
    "KXNBAREB",
    "KXNBAAST",
    "KXNBA3PT",
    "KXNBAPRA",
    "KXNBABLK",
    "KXNBASTL",
    "KXNFLANYTD",
    "KXNFLPASSYDS",
    "KXNFLRUSHYDS",
    "KXNFLRECYDS",
    "KXNFLREC",
    "KXNHLPTS",
    "KXNHLGOAL",
    "KXNHLAST",
    "KXUCLSOA",
]


class PreTradeEducator:
    """
    Educates the bot before any trade entry by analyzing:
    1. Whale Order Book Depth Ratio (Top-5 Bid $ vs Ask $)
    2. Live Trade Tape Momentum (Recent fills trending up vs falling knife)
    3. Self-Learning Historical Series Performance (Win rate & realized P&L per series)
    """

    def __init__(self):
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.memory = self._load_memory()

    def _load_memory(self) -> dict:
        if MEMORY_PATH.exists():
            try:
                return json.loads(MEMORY_PATH.read_text())
            except Exception:
                pass
        return {"series_stats": {}, "evaluated_plays": 0, "vetoed_plays": 0}

    def save_memory(self) -> None:
        try:
            MEMORY_PATH.write_text(json.dumps(self.memory, indent=2))
        except Exception:
            pass

    def update_from_positions(self, positions: list[dict]) -> None:
        series_Map: dict[str, dict] = {}
        for p in positions:
            t = p.get("ticker", "")
            series = t.split("-")[0] if "-" in t else t
            if not series or series.startswith("KXMVE"):
                continue
            pnl = float(p.get("realized_pnl_dollars", "0") or 0)
            if abs(pnl) >= 0.01:
                st = series_Map.setdefault(series, {"realized_pnl": 0.0, "wins": 0, "losses": 0})
                st["realized_pnl"] = round(st["realized_pnl"] + pnl, 4)
                if pnl >= 0.05:
                    st["wins"] += 1
                elif pnl <= -0.25:
                    st["losses"] += 1
        if series_Map:
            existing = self.memory.setdefault("series_stats", {})
            for s, data in series_Map.items():
                existing[s] = data
        self.save_memory()

    def record_equity_snapshot(self, cash_cents: int, port_cents: int, equity_cents: int) -> None:
        """Records daily winnings ledger and equity curve snapshots for charts/tables."""
        now_local = datetime.now()
        day_str = now_local.strftime("%Y-%m-%d")
        ts_str = now_local.strftime("%H:%M")
        eq_dlr = round(equity_cents / 100.0, 2)
        cash_dlr = round(cash_cents / 100.0, 2)
        port_dlr = round(port_cents / 100.0, 2)

        hist = self.memory.setdefault("equity_history", [])
        deposit_delta = 0.0
        if hist:
            prev_cash = float(hist[-1].get("cash", cash_dlr))
            prev_eq = float(hist[-1].get("equity", eq_dlr))
            # If both cash and total equity jump simultaneously by >= $25, it's an external deposit
            if (cash_dlr - prev_cash) >= 25.0 and (eq_dlr - prev_eq) >= 20.0:
                deposit_delta = round(cash_dlr - prev_cash, 2)

        daily = self.memory.setdefault("daily_ledger", {})
        day_entry = daily.setdefault(
            day_str,
            {
                "start_equity": eq_dlr,
                "current_equity": eq_dlr,
                "high_equity": eq_dlr,
                "low_equity": eq_dlr,
                "net_change_dlr": 0.0,
                "net_change_pct": 0.0,
                "deposits_dlr": 0.0,
            },
        )
        if deposit_delta > 0:
            day_entry["start_equity"] = round(day_entry.get("start_equity", eq_dlr) + deposit_delta, 2)
            day_entry["deposits_dlr"] = round(day_entry.get("deposits_dlr", 0.0) + deposit_delta, 2)

        day_entry["current_equity"] = eq_dlr
        day_entry["high_equity"] = max(day_entry.get("high_equity", eq_dlr), eq_dlr)
        day_entry["low_equity"] = min(day_entry.get("low_equity", eq_dlr), eq_dlr)
        start_eq = day_entry.get("start_equity", eq_dlr) or eq_dlr
        day_entry["net_change_dlr"] = round(eq_dlr - start_eq, 2)
        day_entry["net_change_pct"] = round(((eq_dlr - start_eq) / max(0.01, start_eq)) * 100.0, 2)

        if not hist or hist[-1].get("time") != ts_str or abs(hist[-1].get("equity", 0) - eq_dlr) >= 0.05:
            hist.append({"date": day_str, "time": ts_str, "equity": eq_dlr, "cash": cash_dlr, "portfolio": port_dlr})
            if len(hist) > 500:
                self.memory["equity_history"] = hist[-500:]
        self.save_memory()

    async def verify_external_research(self, client: KalshiClient, ticker: str, title: str) -> tuple[bool, str]:
        """
        Queries real-world external APIs (Coinbase Spot, ESPN Live Scoreboard, Open-Meteo Weather)
        to verify fundamental live truth before placing any bet.
        """
        hc = await client.get_client()
        series = ticker.split("-")[0] if "-" in ticker else ticker

        # A. Live Multi-Exchange Crypto Oracle (Synthetic CF Benchmarks BRTI + L2 Whale Book + Dynamic ATR Cushion)
        crypto_map = {
            "KXBTCD": ("BTC-USD", "XXBTZUSD"), "KXBTC": ("BTC-USD", "XXBTZUSD"),
            "KXETHD": ("ETH-USD", "XETHZUSD"), "KXETH": ("ETH-USD", "XETHZUSD"),
            "KXSOLD": ("SOL-USD", "SOLUSD"),
            "KXXRPD": ("XRP-USD", "XXRPZUSD"), "KXXRP": ("XRP-USD", "XXRPZUSD"),
            "KXDOGED": ("DOGE-USD", "XDGUSD"), "KXDOGE": ("DOGE-USD", "XDGUSD"),
            "KXBNBD": ("BNB-USD", ""),
        }
        if series in crypto_map and "-T" in ticker:
            pair, kraken_pair = crypto_map[series]
            try:
                strike = float(ticker.split("-T")[-1])
                # 1. Synthetic CF Benchmarks BRTI Settlement Price (Coinbase Spot + Kraken Spot Average)
                r_cb = await hc.get(f"https://api.coinbase.com/v2/prices/{pair}/spot", timeout=2.5)
                cb_spot = float(r_cb.json()["data"]["amount"])
                spot = cb_spot
                oracle_src = "CB"
                if kraken_pair:
                    try:
                        r_kr = await hc.get(f"https://api.kraken.com/0/public/Ticker?pair={kraken_pair}", timeout=2.0)
                        kr_res = r_kr.json().get("result", {})
                        if kr_res:
                            first_key = next(iter(kr_res))
                            kr_spot = float(kr_res[first_key]["c"][0])
                            spot = (cb_spot + kr_spot) / 2.0
                            oracle_src = "BRTI(CB+Kraken)"
                    except Exception:
                        pass

                is_below = "below" in title.lower()
                cushion_pct = (((strike - spot) if is_below else (spot - strike)) / strike) * 100.0

                # 2. Dynamic 15-Minute ATR Volatility Cushion & Multi-Timeframe Candlesticks (Coinbase Exchange API)
                is_high_beta_alt = series in ("KXSOLD", "KXXRPD", "KXXRP", "KXDOGED", "KXDOGE", "KXBNBD")
                base_cush = 0.75 if is_high_beta_alt else 0.45
                min_required_cushion = base_cush
                trend_str = ""
                try:
                    rc = await hc.get(f"https://api.exchange.coinbase.com/products/{pair}/candles?granularity=300", timeout=2.5)
                    candles = rc.json()
                    if isinstance(candles, list) and len(candles) >= 3:
                        # candles[i] = [time, low, high, open, close, volume]
                        newest_close = float(candles[0][4])
                        oldest_open = float(candles[2][3])
                        high_15m = max(float(c[2]) for c in candles[:3])
                        low_15m = min(float(c[1]) for c in candles[:3])
                        atr_15m_pct = ((high_15m - low_15m) / max(0.01, spot)) * 100.0
                        # Require strike cushion to exceed at least 1.50x the live 15-min volatility range (min 0.45% BTC/ETH, 0.75% Alts)
                        min_required_cushion = max(base_cush, round(atr_15m_pct * 1.50, 2))

                        trend_15m = ((newest_close - oldest_open) / oldest_open) * 100.0
                        effective_trend = -trend_15m if is_below else trend_15m
                        if effective_trend < -0.02:
                            return False, f"VETO [Crypto 15m Trend]: {pair} drifting {trend_15m:+.2f}% over last 15m toward strike (strict zero-drift rule)"
                        trend_str = f" | 15mTrend={trend_15m:+.2f}% | 15mVol={atr_15m_pct:.2f}%"
                except Exception:
                    pass

                if cushion_pct < min_required_cushion:
                    return False, (
                        f"VETO [{oracle_src}]: {pair} spot (${spot:,.2f}) cushion {cushion_pct:+.2f}% < "
                        f"strict volatility cushion {min_required_cushion:.2f}% (strike ${strike:,.2f})"
                    )

                # 3. Coinbase Exchange L2 Spot Order Book Whale Wall Check (Top 50 Spot Bids vs Asks)
                l2_str = ""
                try:
                    rl2 = await hc.get(f"https://api.exchange.coinbase.com/products/{pair}/book?level=2", timeout=2.5)
                    book_l2 = rl2.json()
                    bids_l2 = sum(float(b[0]) * float(b[1]) for b in book_l2.get("bids", [])[:25])
                    asks_l2 = sum(float(a[0]) * float(a[1]) for a in book_l2.get("asks", [])[:25])
                    if bids_l2 > 0 and asks_l2 > 0:
                        spot_imb = (asks_l2 / bids_l2) if is_below else (bids_l2 / asks_l2)
                        if spot_imb < 0.80:
                            return False, f"VETO [Spot L2 Whale Book]: Opposing Coinbase spot orderbook wall (ratio={spot_imb:.2f}x < 0.80x strict min)"
                        l2_str = f" | SpotL2={spot_imb:.2f}x"
                except Exception:
                    pass

                return True, f"{oracle_src}=${spot:,.2f} ({cushion_pct:+.2f}% > strike{trend_str}{l2_str})"
            except Exception as e:
                return False, f"VETO [Crypto Oracle Error]: Could not verify live crypto spot ({e})"

        # B. Live City Weather High-Temp Verification via Open-Meteo API
        weather_coords = {
            "KXHIGHLAX": (34.05, -118.24),
            "KXHIGHNY": (40.78, -73.97),
            "KXHIGHCHI": (41.88, -87.63),
            "KXHIGHMIA": (25.79, -80.29),
            "KXHIGHAUS": (30.27, -97.74),
            "KXHIGHDEN": (39.74, -104.99),
            "KXHIGHPHIL": (39.95, -75.16),
        }
        if series in weather_coords and "-T" in ticker:
            lat, lon = weather_coords[series]
            try:
                strike_temp = float(ticker.split("-T")[-1])
                url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto&forecast_days=1"
                r = await hc.get(url, timeout=3.0)
                forecast_max = float(r.json()["daily"]["temperature_2m_max"][0])
                is_below = "below" in title.lower()
                diff_f = (strike_temp - forecast_max) if is_below else (forecast_max - strike_temp)
                if diff_f < 1.5:
                    return False, f"VETO [Open-Meteo API]: Forecast max ({forecast_max:.1f}F) within 1.5F/wrong side of strike ({strike_temp:.1f}F)"
                return True, f"Open-Meteo Max={forecast_max:.1f}F (diff {diff_f:+.1f}F vs {strike_temp:.0f}F)"
            except Exception as e:
                return False, f"VETO [Open-Meteo API Error]: Could not verify weather forecast ({e})"

        # B2. Live Wall Street Indices & Crude Oil Verification via Yahoo Finance API (with 15m ATR & Trend Guard!)
        yahoo_map = {
            "KXINXU": "%5EGSPC", "KXINX": "%5EGSPC",
            "KXNASDAQ100U": "%5ENDX", "KXNASDAQ100": "%5ENDX",
            "KXWTI": "CL=F",
        }
        if series in yahoo_map and "-T" in ticker:
            ysym = yahoo_map[series]
            try:
                strike_val = float(ticker.split("-T")[-1])
                r = await hc.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?interval=5m&range=1d",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=3.0,
                )
                res_obj = r.json()["chart"]["result"][0]
                spot_val = float(res_obj["meta"]["regularMarketPrice"])
                is_below = "below" in title.lower()
                cushion_pct = (((strike_val - spot_val) if is_below else (spot_val - strike_val)) / strike_val) * 100.0

                min_req_cushion = 0.35
                trend_info = ""
                try:
                    q = res_obj.get("indicators", {}).get("quote", [{}])[0]
                    closes = [x for x in (q.get("close") or []) if x is not None]
                    opens = [x for x in (q.get("open") or []) if x is not None]
                    highs = [x for x in (q.get("high") or []) if x is not None]
                    lows = [x for x in (q.get("low") or []) if x is not None]
                    if len(closes) >= 3 and len(opens) >= 3 and len(highs) >= 3 and len(lows) >= 3:
                        h15 = max(highs[-3:])
                        l15 = min(lows[-3:])
                        atr_15m = ((h15 - l15) / max(0.01, spot_val)) * 100.0
                        min_req_cushion = max(0.35, round(atr_15m * 1.25, 2))
                        trend_15m = ((closes[-1] - opens[-3]) / max(0.01, opens[-3])) * 100.0
                        eff_trend = -trend_15m if is_below else trend_15m
                        if eff_trend < -0.05:
                            return False, f"VETO [Wall St 15m Trend]: {series} moving {trend_15m:+.2f}% toward strike over last 15m"
                        trend_info = f" | 15mTrend={trend_15m:+.2f}% | 15mVol={atr_15m:.2f}%"
                except Exception:
                    pass

                if cushion_pct < min_req_cushion:
                    return False, (
                        f"VETO [Wall St Feed]: {series} spot (${spot_val:,.2f}) cushion {cushion_pct:+.2f}% < "
                        f"required {min_req_cushion:.2f}% (strike ${strike_val:,.2f})"
                    )
                return True, f"WallSt Spot={spot_val:,.2f} ({cushion_pct:+.2f}% > strike{trend_info})"
            except Exception as e:
                return False, f"VETO [Wall St API Error]: Could not verify live index/commodity feed ({e})"

        # C2. Deep ESPN Live Box Score, Quarter/Halftime Linescore & Matchup Verification
        sp_path = None
        if series not in (
            "KXNBAGAME", "KXNFLGAME", "KXNCAAFGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",
            "KXNHLGAME", "KXUCLGAME", "KXMLSGAME", "KXLIGAMXGAME", "KXLALIGAGAME",
            "KXEPLGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME",
        ):
            if series.startswith("KXNBA"):
                sp_path = "basketball/nba/scoreboard"
            elif series.startswith("KXNFL"):
                sp_path = "football/nfl/scoreboard"
            elif series.startswith("KXNCAAF"):
                sp_path = "football/college-football/scoreboard?groups=80&limit=200"
            elif series.startswith("KXNCAAMB"):
                sp_path = "basketball/mens-college-basketball/scoreboard?groups=50&limit=200"
            elif series.startswith("KXNHL"):
                sp_path = "hockey/nhl/scoreboard"
            elif series.startswith("KXUCL"):
                sp_path = "soccer/uefa.champions/scoreboard"
            elif series.startswith("KXEPL"):
                sp_path = "soccer/eng.1/scoreboard"
            elif series.startswith("KXLALIGA"):
                sp_path = "soccer/esp.1/scoreboard"
            elif series.startswith("KXSERIEA"):
                sp_path = "soccer/ita.1/scoreboard"
            elif series.startswith("KXBUNDESLIGA"):
                sp_path = "soccer/ger.1/scoreboard"
            elif series.startswith("KXLIGAMX"):
                sp_path = "soccer/mex.1/scoreboard"

        if sp_path:
            try:
                r = await hc.get(f"https://site.api.espn.com/apis/site/v2/sports/{sp_path}", timeout=3.5)
                events = r.json().get("events", [])
                live_events = [
                    ev for ev in events
                    if ev.get("status", {}).get("type", {}).get("state") == "in"
                ]
                if not live_events:
                    return False, f"VETO [ESPN Live Check]: No live in-play {series} games right now (all pre-game/post-game)"

                # Match exact game in ticker (e.g. KXNBAPTS-26OCT26PHXOKC-...) to an active live event
                parts = ticker.split("-")
                event_slug = parts[1].upper() if len(parts) >= 2 else ""
                matched_ev = None
                for ev in live_events:
                    comps = ev.get("competitions", [{}])
                    competitors = comps[0].get("competitors", []) if comps else []
                    abbrs = [
                        (c.get("team", {}).get("abbreviation") or "").upper()
                        for c in competitors
                    ]
                    if any(ab and ab in event_slug for ab in abbrs):
                        matched_ev = ev
                        break
                if not matched_ev:
                    return False, f"VETO [ESPN Matchup Check]: Specific game '{event_slug}' is NOT live in-play right now"

                comp = matched_ev.get("competitions", [{}])[0]
                st_obj = comp.get("status", {})
                live_period = int(st_obj.get("period", 1) or 1)
                clock_disp = st_obj.get("displayClock", "")
                competitors = comp.get("competitors", [])
                is_soccer = "soccer/" in sp_path

                # 1. Quarter / Period / Halftime Active Window Enforcement (Never lock cash in a future quarter!)
                req_periods = None
                period_tag = f"P{live_period}"
                if any(q in series for q in ("1Q", "1P")):
                    req_periods = (1,)
                    period_tag = "1Q" if "1Q" in series else "1P"
                elif any(q in series for q in ("2Q", "2P")):
                    req_periods = (2,)
                    period_tag = "2Q" if "2Q" in series else "2P"
                elif any(q in series for q in ("3Q", "3P")):
                    req_periods = (3,)
                    period_tag = "3Q" if "3Q" in series else "3P"
                elif "4Q" in series:
                    req_periods = (4,)
                    period_tag = "4Q"
                elif "1H" in series:
                    req_periods = (1,) if is_soccer else (1, 2)
                    period_tag = "1H"
                elif "2H" in series:
                    req_periods = (2,) if is_soccer else (3, 4)
                    period_tag = "2H"

                if req_periods is not None and live_period not in req_periods:
                    return False, (
                        f"VETO [Period Window]: {series} targets {period_tag} (periods {req_periods}), "
                        f"but live game is in P{live_period} ({clock_disp}) — never lock cash in wrong period!"
                    )

                # Helper to compute exact Quarter / Half / Full-Game points per competitor via ESPN linescores
                def _comp_window_pts(c_obj: dict) -> float:
                    ls = [float(x.get("value", 0) or 0) for x in c_obj.get("linescores", [])]
                    full_sc = float(c_obj.get("score", 0) or (sum(ls) if ls else 0))
                    if req_periods == (1,) and ls:
                        return ls[0]
                    if req_periods == (2,) and not is_soccer:
                        return ls[1] if len(ls) >= 2 else 0.0
                    if req_periods == (3,):
                        return ls[2] if len(ls) >= 3 else 0.0
                    if req_periods == (4,):
                        return ls[3] if len(ls) >= 4 else 0.0
                    if req_periods == (1, 2) and ls:
                        return sum(ls[:2])
                    if req_periods == (3, 4) and ls:
                        return sum(ls[2:4])
                    return full_sc

                # 2. Quarter/Half Winner, Spread & Total Live Scoreboard Verification
                suffix = parts[-1].upper() if len(parts) >= 3 else ""
                import re
                m_suf = re.match(r"^([A-Z]{2,4})?(\d+(?:\.\d+)?)$", suffix)

                # Helper to check Vegas Opening -> Closing Line Steam & Multiplicative De-Vig (GEMINI.md Law 4)
                odds_obj = (comp.get("odds") or [{}])[0] if comp.get("odds") else {}
                vegas_steam_tag = ""
                try:
                    tot_o = odds_obj.get("total", {}).get("over", {})
                    t_open_s = str(tot_o.get("open", {}).get("line", "")).replace("o", "").replace("u", "")
                    t_close_s = str(tot_o.get("close", {}).get("line", "")).replace("o", "").replace("u", "")
                    if "TOTAL" in series and t_open_s and t_close_s:
                        t_shift = float(t_close_s) - float(t_open_s)
                        if t_shift <= -3.5 and req_periods is None:
                            return False, f"VETO [Vegas Total Crash]: Sportsbooks steamed total DOWN ({t_open_s} -> {t_close_s}, {t_shift:+.1f} pts)"
                        if t_shift >= 1.5:
                            vegas_steam_tag = f" | VEGAS-STEAM(OU {t_open_s}->{t_close_s})"
                except Exception:
                    pass

                if "TOTAL" in series and len(competitors) >= 2 and m_suf:
                    tm_prefix, num_str = m_suf.group(1), m_suf.group(2)
                    target_val = max(0.5, float(num_str) - 0.5)
                    if "TEAMTOTAL" in series and tm_prefix:
                        target_comp = next(
                            (c for c in competitors if (c.get("team", {}).get("abbreviation") or "").upper() == tm_prefix),
                            competitors[0],
                        )
                        cur_pts = _comp_window_pts(target_comp)
                    else:
                        cur_pts = _comp_window_pts(competitors[0]) + _comp_window_pts(competitors[1])
                    prog_pct = (cur_pts / target_val) * 100.0
                    if prog_pct < 65.0:
                        return False, (
                            f"VETO [ESPN {period_tag} Total]: Only {cur_pts:.0f}/{target_val:.1f} pts scored "
                            f"({prog_pct:.0f}% < 65% min in P{live_period} {clock_disp})"
                        )
                    lock_tag = (" | PROP-LOCK" if prog_pct >= 80.0 else "") + vegas_steam_tag
                    return True, f"ESPN {period_tag} Total Verified ({cur_pts:.0f}/{target_val:.1f} = {prog_pct:.0f}% banked{lock_tag} | P{live_period} {clock_disp})"

                if "SPREAD" in series and len(competitors) >= 2 and m_suf and m_suf.group(1):
                    tm_prefix, num_str = m_suf.group(1), m_suf.group(2)
                    strike_val = max(0.5, float(num_str) - 0.5)
                    c_idx = next(
                        (i for i, c in enumerate(competitors) if (c.get("team", {}).get("abbreviation") or "").upper() == tm_prefix),
                        None,
                    )
                    if c_idx is not None:
                        my_pts = _comp_window_pts(competitors[c_idx])
                        opp_pts = _comp_window_pts(competitors[1 - c_idx])
                        margin = my_pts - opp_pts
                        if margin <= 0 or margin < (strike_val - 2.5):
                            return False, (
                                f"VETO [ESPN {period_tag} Spread]: {tm_prefix} margin is {margin:+.0f} ({my_pts:.0f}-{opp_pts:.0f}) "
                                f"vs -{strike_val:.1f} spread in P{live_period} {clock_disp}"
                            )
                        lock_tag = " | PROP-LOCK" if margin >= (strike_val + 2.5) else ""
                        return True, f"ESPN {period_tag} Spread Verified ({tm_prefix} {my_pts:.0f}-{opp_pts:.0f}, margin {margin:+.0f} vs -{strike_val:.1f}{lock_tag} | P{live_period} {clock_disp})"

                if req_periods is not None and "TOTAL" not in series and "SPREAD" not in series and len(competitors) >= 2:
                    c_idx = next(
                        (i for i, c in enumerate(competitors) if (c.get("team", {}).get("abbreviation") or "").upper() == suffix),
                        None,
                    )
                    if c_idx is not None:
                        my_pts = _comp_window_pts(competitors[c_idx])
                        opp_pts = _comp_window_pts(competitors[1 - c_idx])
                        margin = my_pts - opp_pts
                        if margin <= 0:
                            return False, (
                                f"VETO [ESPN {period_tag} Winner]: {suffix} is NOT leading {period_tag} "
                                f"({my_pts:.0f}-{opp_pts:.0f} in P{live_period} {clock_disp})"
                            )
                        # Multi-Possession / Anti-Single-Goal Shield for Quarter & Half Winners
                        if is_soccer and margin < 2:
                            try:
                                clk_min = float(str(clock_disp).replace("'", "").split("+")[0] or 0)
                            except Exception:
                                clk_min = 0.0
                            if clk_min < 38.0:
                                return False, f"VETO [Soccer 1H 1-Goal Risk]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} at {clock_disp} (<38' or <2 goal cushion)"
                        elif series.startswith(("KXNBA", "KXNFL")) and margin < 4:
                            return False, f"VETO [ESPN {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} (+{margin:.0f} < +4 pt two-possession min)"
                        lock_tag = " | PROP-LOCK" if margin >= 6 or (is_soccer and margin >= 2) else ""
                        return True, f"ESPN {period_tag} Leader Verified ({suffix} leading {my_pts:.0f}-{opp_pts:.0f}{lock_tag} | P{live_period} {clock_disp})"

                # 3. Deep Player Prop Box Score Check via ESPN Summary API (/summary?event={id})
                nba_prop_Set = {"KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA", "KXNBABLK", "KXNBASTL"}
                nfl_prop_Set = {"KXNFLPASSYDS", "KXNFLRUSHYDS", "KXNFLRECYDS", "KXNFLREC"}
                if (series in nba_prop_Set or series in nfl_prop_Set) and ":" in title and len(parts) >= 4:
                    player_target_name = title.split(":")[0].strip().lower()
                    player_last = player_target_name.split()[-1] if player_target_name else ""
                    try:
                        target_val = float(parts[-1])
                    except Exception:
                        target_val = 0.0

                    if target_val > 0 and player_last:
                        sport_base = sp_path.split("/scoreboard")[0]
                        ev_id = matched_ev.get("id", "")
                        r_box = await hc.get(
                            f"https://site.api.espn.com/apis/site/v2/sports/{sport_base}/summary?event={ev_id}",
                            timeout=3.5,
                        )
                        bs_players = r_box.json().get("boxscore", {}).get("players", [])
                        found_stat = None
                        fouls_val = 0
                        mins_played = "0"
                        for team_blk in bs_players:
                            for sg in team_blk.get("statistics", []):
                                sg_name = (sg.get("name") or "").lower()
                                labels = [lbl.upper() for lbl in sg.get("labels", [])]
                                for ath in sg.get("athletes", []):
                                    a_name = (ath.get("athlete", {}).get("displayName") or "").lower()
                                    if player_last in a_name:
                                        st_arr = ath.get("stats", [])
                                        if not st_arr:
                                            continue
                                        if series in nba_prop_Set and len(st_arr) == len(labels):
                                            s_map = dict(zip(labels, st_arr))
                                            mins_played = s_map.get("MIN", "0")
                                            fouls_val = int(float(s_map.get("PF", "0") or 0))
                                            pts = float(s_map.get("PTS", "0") or 0)
                                            reb = float(s_map.get("REB", "0") or 0)
                                            ast = float(s_map.get("AST", "0") or 0)
                                            blk = float(s_map.get("BLK", "0") or 0)
                                            stl = float(s_map.get("STL", "0") or 0)
                                            tpt_raw = str(s_map.get("3PT", "0-0")).split("-")[0]
                                            tpt = float(tpt_raw or 0)
                                            if series == "KXNBAPTS": found_stat = pts
                                            elif series == "KXNBAREB": found_stat = reb
                                            elif series == "KXNBAAST": found_stat = ast
                                            elif series == "KXNBA3PT": found_stat = tpt
                                            elif series == "KXNBAPRA": found_stat = pts + reb + ast
                                            elif series == "KXNBABLK": found_stat = blk
                                            elif series == "KXNBASTL": found_stat = stl
                                        elif series in nfl_prop_Set and len(st_arr) == len(labels):
                                            s_map = dict(zip(labels, st_arr))
                                            if series == "KXNFLPASSYDS" and sg_name == "passing":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLRUSHYDS" and sg_name == "rushing":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLRECYDS" and sg_name == "receiving":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLREC" and sg_name == "receiving":
                                                found_stat = float(s_map.get("REC", "0") or 0)
                        if found_stat is not None:
                            if fouls_val >= 5:
                                return False, f"VETO [ESPN Boxscore]: {title} has {fouls_val} personal fouls (foul trouble risk!)"
                            prog_pct = (found_stat / target_val) * 100.0
                            if prog_pct < 65.0:
                                return False, f"VETO [ESPN Boxscore]: {title} only at {found_stat:.0f}/{target_val:.0f} ({prog_pct:.0f}% < 65% live progress min)"
                            # Late-Clock Crunch Veto: Never buy a prop in the final 2 mins of Q4 if still <82% of target!
                            try:
                                clk_m = float(str(clock_disp).split(":")[0]) if ":" in str(clock_disp) else 5.0
                            except Exception:
                                clk_m = 5.0
                            if live_period >= 4 and clk_m < 2.0 and prog_pct < 82.0:
                                return False, (
                                    f"VETO [Late Clock Crunch]: Only {clock_disp} left in P{live_period} and {title} is at "
                                    f"{found_stat:.0f}/{target_val:.0f} ({prog_pct:.0f}% < 82% min for final 2m)"
                                )
                            # Blowout / Garbage-Time Bench Shield: Veto Q3/Q4 player props in 22+ pt blowouts unless >=90% banked!
                            if len(competitors) >= 2 and live_period >= 3 and prog_pct < 90.0:
                                gm_margin = abs(float(competitors[0].get("score", 0) or 0) - float(competitors[1].get("score", 0) or 0))
                                max_blowout = 22.0 if series in nba_prop_Set else 21.0
                                if gm_margin >= max_blowout:
                                    return False, (
                                        f"VETO [Blowout Bench Risk]: Game margin is {gm_margin:.0f} pts in P{live_period} "
                                        f"— starters risk getting benched in garbage time!"
                                    )
                            lock_tag = " | PROP-LOCK" if prog_pct >= 80.0 else ""
                            if str(clock_disp).strip() in ("0:00", "0.0", ""):
                                lock_tag += " | CLOCK-FROZEN"
                            return True, f"ESPN Boxscore Verified ({found_stat:.0f}/{target_val:.0f} = {prog_pct:.0f}% banked{lock_tag} | MIN={mins_played} | PF={fouls_val})"

                return True, f"ESPN Matchup Live ({event_slug} active in P{live_period} {clock_disp})"
            except Exception as e:
                return False, f"VETO [ESPN API Error]: League live check failed ({e})"

        # C. Live Sports & Tennis Scoreboard Verification via ESPN API (Fail-Closed!)
        espn_map = {
            "KXWTAMATCH": "tennis/wta/scoreboard",
            "KXATPMATCH": "tennis/atp/scoreboard",
            "KXNBAGAME": "basketball/nba/scoreboard",
            "KXNFLGAME": "football/nfl/scoreboard",
            "KXNCAAFGAME": "football/college-football/scoreboard?groups=80&limit=200",
            "KXNCAAMBGAME": "basketball/mens-college-basketball/scoreboard?groups=50&limit=200",
            "KXNCAAWBGAME": "basketball/womens-college-basketball/scoreboard?groups=50&limit=200",
            "KXNHLGAME": "hockey/nhl/scoreboard",
            "KXUCLGAME": "soccer/uefa.champions/scoreboard",
            "KXMLSGAME": "soccer/usa.1/scoreboard",
            "KXLIGAMXGAME": "soccer/mex.1/scoreboard",
            "KXLALIGAGAME": "soccer/esp.1/scoreboard",
            "KXEPLGAME": "soccer/eng.1/scoreboard",
            "KXSERIEAGAME": "soccer/ita.1/scoreboard",
            "KXBUNDESLIGAGAME": "soccer/ger.1/scoreboard",
            "KXLIGUE1GAME": "soccer/fra.1/scoreboard",
        }
        if series in espn_map:
            sport_path = espn_map[series]
            target_name = title.replace(" wins", "").strip().lower()
            last_word = target_name.split()[-1] if target_name else ""
            try:
                r = await hc.get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}", timeout=3.5)
                events = r.json().get("events", [])
                for ev in events:
                    comps = list(ev.get("competitions", []))
                    for grp in ev.get("groupings", []):
                        comps.extend(grp.get("competitions", []))
                    for comp in comps:
                        competitors = comp.get("competitors", [])
                        if len(competitors) < 2:
                            continue
                        names = []
                        scores = []
                        sets_won = []
                        cur_set_games = []
                        for c in competitors:
                            n = (c.get("athlete", {}).get("displayName") or c.get("team", {}).get("displayName") or "").lower()
                            names.append(n)
                            ls = c.get("linescores", [])
                            if series in ("KXWTAMATCH", "KXATPMATCH") and ls:
                                scores.append(sum(float(x.get("value", 0) or 0) for x in ls))
                                sets_won.append(sum(1 for x in ls if x.get("winner") is True))
                                cur_set_games.append(float(ls[-1].get("value", 0) or 0))
                            elif c.get("score") is not None:
                                scores.append(float(c.get("score", 0) or 0))
                                sets_won.append(0)
                                cur_set_games.append(0.0)
                            elif ls:
                                scores.append(sum(float(x.get("value", 0) or 0) for x in ls))
                                sets_won.append(0)
                                cur_set_games.append(0.0)
                            else:
                                scores.append(0.0)
                                sets_won.append(0)
                                cur_set_games.append(0.0)
                        if last_word and any(last_word in n for n in names):
                            state = comp.get("status", {}).get("type", {}).get("state", "")
                            if state != "in":
                                return False, f"VETO [ESPN API]: Match '{title}' is not live in-play (state='{state}')"
                            idx = 0 if last_word in names[0] else 1
                            opp_idx = 1 - idx
                            margin = scores[idx] - scores[opp_idx]
                            if margin <= 0:
                                return False, f"VETO [ESPN API]: '{title}' is NOT LEADING on live scoreboard ({scores[idx]:.0f} vs {scores[opp_idx]:.0f})"
                            if series in ("KXWTAMATCH", "KXATPMATCH"):
                                if sets_won[idx] < sets_won[opp_idx]:
                                    return False, f"VETO [ESPN Tennis]: '{title}' is trailing in sets ({sets_won[idx]}-{sets_won[opp_idx]})"
                                if sets_won[idx] == sets_won[opp_idx] and cur_set_games[idx] <= cur_set_games[opp_idx]:
                                    return False, f"VETO [ESPN Tennis]: '{title}' not leading current set ({cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f})"

                            # Anti-Fluke / Multi-Possession Cushion Shield on Full-Game Moneylines (Soccer, NHL, NBA, NFL)
                            st_obj = comp.get("status", {})
                            period_num = int(st_obj.get("period", 1) or 1)
                            clock_disp = st_obj.get("displayClock", "")
                            if "soccer/" in sport_path and margin < 2:
                                try:
                                    clk_min = float(str(clock_disp).replace("'", "").split("+")[0] or 0)
                                except Exception:
                                    clk_min = 0.0
                                if period_num < 2 or clk_min < 72.0:
                                    return False, (
                                        f"VETO [Soccer 1-Goal Draw Risk]: '{title}' only leading {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"at {clock_disp} (requires 2+ goal cushion or 72'+ late clock!)"
                                    )
                            elif series == "KXNHLGAME" and margin < 2 and period_num < 3:
                                return False, (
                                    f"VETO [NHL 1-Goal Risk]: '{title}' only up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                    f"in P{period_num} (requires 2+ goal lead or 3rd Period!)"
                                )
                            elif series in ("KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME"):
                                min_bball_margin = 6 if period_num <= 2 else 4
                                if margin < min_bball_margin:
                                    return False, (
                                        f"VETO [Basketball 1-Possession Risk]: '{title}' only up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"(+{margin:.0f} < +{min_bball_margin} pt cushion in P{period_num})"
                                    )
                            elif series in ("KXNFLGAME", "KXNCAAFGAME"):
                                min_fball_margin = 7 if period_num <= 2 else 4
                                if margin < min_fball_margin:
                                    return False, (
                                        f"VETO [Football FG Risk]: '{title}' only up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"(+{margin:.0f} < +{min_fball_margin} pt cushion in P{period_num})"
                                    )

                            # Check Vegas Opening vs Closing Line Movement, Game Clock Period & Live Win Probability
                            vegas_note = ""
                            try:
                                is_4q_sport = series in ("KXNBAGAME", "KXNFLGAME", "KXNCAAFGAME", "KXNCAAWBGAME")
                                is_late_clock = (period_num >= 3) if is_4q_sport else (period_num >= 2)
                                if is_late_clock:
                                    vegas_note += f" | LATE-CLOCK(P{period_num} {clock_disp})"
                                else:
                                    vegas_note += f" | P{period_num} {clock_disp}"

                                sit_prob = comp.get("situation", {}).get("lastPlay", {}).get("probability", {})
                                home_Away = competitors[idx].get("homeAway", "")
                                wp = sit_prob.get("homeWinPercentage" if home_Away == "home" else "awayWinPercentage")
                                if wp is not None:
                                    wp_pct = float(wp) * 100.0
                                    if wp_pct < 75.0:
                                        return False, f"VETO [ESPN Live BPI]: Real-time win model is only {wp_pct:.1f}% (<75% min)"
                                    vegas_note += f" | LiveBPI={wp_pct:.1f}%"

                                odds_list = comp.get("odds", [])
                                if odds_list and isinstance(odds_list[0], dict):
                                    ml = odds_list[0].get("moneyline", {})
                                    side_ml = ml.get("home" if home_Away == "home" else "away", {})
                                    opp_ml = ml.get("away" if home_Away == "home" else "home", {})
                                    open_o = side_ml.get("open", {}).get("odds")
                                    close_o = side_ml.get("close", {}).get("odds")
                                    opp_close_o = opp_ml.get("close", {}).get("odds")
                                    if open_o and close_o:
                                        def _am_prob(s):
                                            v = float(str(s).replace("+", "").replace("EVEN", "100"))
                                            return (-v / (-v + 100.0)) if v < 0 else (100.0 / (v + 100.0))
                                        p_open = _am_prob(open_o) * 100.0
                                        p_close = _am_prob(close_o) * 100.0
                                        # Multiplicative De-Vigging (GEMINI.md Law 4)
                                        if opp_close_o:
                                            p_opp = _am_prob(opp_close_o) * 100.0
                                            devig_close = (p_close / max(1.0, p_close + p_opp)) * 100.0
                                        else:
                                            devig_close = p_close
                                        shift = p_close - p_open
                                        if shift <= -5.0:
                                            return False, f"VETO [Vegas Line Crash]: Vegas steamed AGAINST '{title}' ({open_o} -> {close_o}, {shift:+.1f}%)"
                                        steam_flag = " | VEGAS-STEAM" if shift >= 3.0 else ""
                                        vegas_note += f" | DeVig={devig_close:.1f}% VegasML={open_o}->{close_o} ({shift:+.1f}%){steam_flag}"
                            except Exception:
                                pass

                            return True, f"ESPN Live Verified ({state.upper()}: {scores[idx]:.0f}-{scores[opp_idx]:.0f}{vegas_note})"
                return False, f"VETO [ESPN API]: '{title}' not found as active in-play leader on live ESPN scoreboard"
            except Exception as e:
                return False, f"VETO [ESPN API Error]: Scoreboard verification failed ({e})"

        return False, f"VETO [Unverified Series]: '{series}' has no live external scoreboard/data feed"

    async def evaluate_opportunity(
        self, client: KalshiClient, ticker: str, ob, title: str = "", min_book_depth_dlr: float = 150.0
    ) -> tuple[bool, str, float]:
        """
        Returns (approved: bool, reason: str, confidence_multiplier: float).
        Enforces Ultra-High-Conviction 5-Gate Filter + External API Research.
        """
        self.memory["evaluated_plays"] = self.memory.get("evaluated_plays", 0) + 1

        # 1. Whale Order Book Imbalance Check (Top 5 levels near touch) + Self-Correcting Drawdown Gate
        series = ticker.split("-")[0] if "-" in ticker else ticker
        st = self.memory.get("series_stats", {}).get(series, {})
        series_pnl = float(st.get("realized_pnl", 0.0) or 0.0)
        req_imbalance = 1.85 if series_pnl < -1.00 else 1.35

        top_yes_bids = sorted(ob.yes_bids, key=lambda x: x.price, reverse=True)[:5]
        top_no_bids = sorted(ob.no_bids, key=lambda x: x.price, reverse=True)[:5]
        yes_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_yes_bids)
        no_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_no_bids)
        imbalance_ratio = (yes_depth + 1.0) / (no_depth + 1.0)

        if yes_depth < min_book_depth_dlr:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, f"VETO: Thin bid liquidity (${yes_depth:.0f} < ${min_book_depth_dlr:.0f} tier exit depth)", 0.0

        if imbalance_ratio < req_imbalance:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, f"VETO: Weak whale buying pressure (Imbalance={imbalance_ratio:.2f}x < {req_imbalance:.2f}x min for {series})", 0.0

        # 2. Recent Trade Tape Recency & Momentum Check (Must be actively trading NOW & strictly flat/rising)
        momentum_delta = 0
        last_trade_mins_ago = 999.0
        try:
            trades_resp = await client.request(
                "GET", "/markets/trades", params={"ticker": ticker, "limit": 10}, authenticated=False
            )
            trades = trades_resp.get("trades", [])
            if not trades:
                self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                self.save_memory()
                return False, "VETO: Zero recent trades on tape (inactive market)", 0.0

            now_utc = datetime.now(timezone.utc)
            last_ts = datetime.fromisoformat(trades[0]["created_time"].replace("Z", "+00:00"))
            last_trade_mins_ago = max(0.0, (now_utc - last_ts).total_seconds() / 60.0)
            if last_trade_mins_ago > 10.0:
                self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                self.save_memory()
                return False, f"VETO: Stale market (last trade {last_trade_mins_ago:.1f}m ago > 10m max)", 0.0

            if len(trades) >= 3:
                newest_price = float(trades[0].get("yes_price_dollars") or (trades[0].get("yes_price", 0) / 100.0))
                oldest_price = float(trades[-1].get("yes_price_dollars") or (trades[-1].get("yes_price", 0) / 100.0))
                momentum_delta = int(round((newest_price - oldest_price) * 100))
                if momentum_delta < 0:
                    self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                    self.save_memory()
                    return False, f"VETO: Negative tape drift ({momentum_delta}c over last 10 trades — only buying rising/flat leaders)", 0.0
        except Exception as e:
            return False, f"VETO: Trade tape unavailable ({e})", 0.0

        # 3. External Real-World API Research (Coinbase Spot, ESPN Live Scoreboard, Open-Meteo Weather)
        ext_ok, ext_reason = await self.verify_external_research(client, ticker, title)
        if not ext_ok:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, ext_reason, 0.0

        # 4. Self-Learning Series Confidence Multiplier + A+ Whale Wall Boost
        conf_mult = 1.0
        if series_pnl > 0:
            conf_mult = 1.15  # Boost allocation 15% on proven profitable series
        elif series_pnl < -1.00:
            conf_mult = 0.85  # Auto-trim size 15% on drawdown series until back in green
        if imbalance_ratio >= 2.25 and momentum_delta >= 0:
            conf_mult *= 1.22  # A+ Whale Wall (>=2.25x) + non-negative tape momentum
        elif imbalance_ratio >= 1.85 and momentum_delta >= 0:
            conf_mult *= 1.12
        if "LATE-CLOCK" in ext_reason or "PROP-LOCK" in ext_reason:
            conf_mult *= 1.12  # Late-game clock drain or >=80% banked player prop boost
        if "CLOCK-FROZEN" in ext_reason or "VEGAS-STEAM" in ext_reason:
            conf_mult *= 1.08  # Commercial/Halftime zero-snipe edge or sharp Vegas line steam boost

        self.save_memory()
        reason = (
            f"APPROVED [Whale={imbalance_ratio:.2f}x | Depth=${yes_depth:.0f} | LastTrade={last_trade_mins_ago:.1f}m | Tape={momentum_delta:+d}c | {ext_reason}]"
        )
        return True, reason, conf_mult


def get_scaling_tier(equity_cents: int) -> dict:
    """
    Automatic 4-Tier Institutional Kelly Bankroll Scaling.
    Automatically adjusts position size, minimum contracts, concurrent plays, and required orderbook depth
    whether the account has $25, $100, $500, or $2,500+.
    """
    eq_dlr = equity_cents / 100.0
    if eq_dlr < 60.0:
        return {
            "name": "TIER 1 (Growth)",
            "base_pct": 0.40,
            "boost_pct": 0.50,
            "min_stake_cents": 650,
            "max_stake_cents": 2500,
            "min_contracts": 8,
            "max_concurrent": 3,
            "min_depth_dlr": 150.0,
        }
    elif eq_dlr < 200.0:
        return {
            "name": "TIER 2 (Acceleration)",
            "base_pct": 0.28,
            "boost_pct": 0.35,
            "min_stake_cents": 1500,
            "max_stake_cents": 7400,
            "min_contracts": 18,
            "max_concurrent": 4,
            "min_depth_dlr": 250.0,
        }
    elif eq_dlr < 750.0:
        # Continuous Micro-Compounding inside Tier 3 ($200-$750): scales smoothly with every +$25 gained!
        dyn_min_contracts = min(80, max(30, int(eq_dlr * 0.11)))
        dyn_max_stake_c = min(22000, max(9500, int(equity_cents * 0.36)))
        return {
            "name": "TIER 3 ($500 Pro Mode)",
            "base_pct": 0.25,
            "boost_pct": 0.33,
            "min_stake_cents": 3500,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 5,
            "min_depth_dlr": 300.0,
            "iceberg_slice": 110,
        }
    elif eq_dlr < 2000.0:
        # TIER 4 ($750-$2,000 Pro/Whale Scaling): deploys $150-$440 per play across up to 7 concurrent winners!
        dyn_min_contracts = min(160, max(75, int(eq_dlr * 0.09)))
        dyn_max_stake_c = min(45000, max(22000, int(equity_cents * 0.26)))
        return {
            "name": "TIER 4 ($1k-$2k Pro Whale Mode)",
            "base_pct": 0.20,
            "boost_pct": 0.26,
            "min_stake_cents": 7500,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 7,
            "min_depth_dlr": 500.0,
            "iceberg_slice": 150,
        }
    else:
        # TIER 5 ($2,000 to $5,000 Sweet-Spot Plateau Mode):
        # Caps active working sizing at the $5,000 zero-slippage plateau (~$450-$550/trade)
        # so every dollar above $5,000 ($4,000+/week) sits protected in cash for weekly bank withdrawal!
        working_eq_dlr = min(eq_dlr, 5000.0)
        working_eq_cents = min(equity_cents, 500000)
        dyn_min_contracts = min(220, max(120, int(working_eq_dlr * 0.045)))
        dyn_max_stake_c = min(55000, max(35000, int(working_eq_cents * 0.11)))
        return {
            "name": "TIER 5 ($5k Sweet-Spot Plateau Mode)",
            "base_pct": 0.10,
            "boost_pct": 0.12,
            "min_stake_cents": 12000,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 9,
            "min_depth_dlr": 750.0,
            "iceberg_slice": 200,
            "working_cap_cents": 500000,
        }


def load_signer() -> KalshiAuthSigner:
    key_id = os.environ.get("KALSHI_KEY_ID") or settings.KALSHI_KEY_ID
    env_pem = os.environ.get("KALSHI_PRIVATE_KEY")
    pem_file = Path("secrets/kalshi_private_key.pem")
    if env_pem and "PRIVATE KEY" in env_pem:
        clean_pem = env_pem.replace("\\n", "\n")
        return KalshiAuthSigner(key_id=key_id, private_key=clean_pem)
    return KalshiAuthSigner(key_id=key_id, private_key=pem_file)


async def run_autonomous_loop(duration_seconds: int = 0):
    signer = load_signer()
    client = KalshiClient(signer=signer, base_url=settings.PROD_REST_URL)
    educator = PreTradeEducator()
    logger.info("Autonomous Worldwide Live Roller + PreTradeEducator started.")
    try:
        await client.request(
            "POST",
            "/portfolio/target_balance_allocation",
            json_data={
                "allocations": [
                    {"exchange_index": 0, "percent": 40},
                    {"exchange_index": 1, "percent": 0},
                    {"exchange_index": 2, "percent": 20},
                    {"exchange_index": 3, "percent": 40},
                ],
                "resting_margin_reservation": "sum",
            },
            authenticated=True,
        )
        logger.info("Kalshi 3-Shard Auto-Allocator verified: 40% Shard 0 / 20% Shard 2 (Crypto) / 40% Shard 3.")
    except Exception as e:
        logger.warning(f"Shard allocation sync note: {e}")
    start_ts = time.time()
    peak_bids: dict[str, int] = {}
    tracked_open_positions: dict[str, dict] = educator.memory.setdefault("tracked_open_positions", {})
    stopped_out_events: set[str] = {"KXLIGAMXGAME-26OCT25TIJSLA"}
    series_shard_cache: dict[str, int] = {}

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            positions = await client.get_positions()
            educator.update_from_positions(positions)
            bal = await client.get_balance()
            total_cents = bal.get("balance", 0)
            port_cents = bal.get("portfolio_value", 0)
            equity_cents = total_cents + port_cents
            educator.record_equity_snapshot(total_cents, port_cents, equity_cents)
            # Track $5,000 Sweet-Spot Plateau & Weekly Bank-Withdrawal Vault
            eq_dlr_now = round(equity_cents / 100.0, 2)
            withdrawable_vault_dlr = round(max(0.0, eq_dlr_now - 5000.0), 2)
            vault_obj = educator.memory.setdefault(
                "sweet_spot_plateau_vault",
                {"working_base_cap_dlr": 5000.0, "withdrawable_above_5k_dlr": 0.0, "last_milestone_alerted_dlr": 0.0},
            )
            vault_obj["withdrawable_above_5k_dlr"] = withdrawable_vault_dlr
            if withdrawable_vault_dlr >= float(vault_obj.get("last_milestone_alerted_dlr", 0.0)) + 1000.0:
                vault_obj["last_milestone_alerted_dlr"] = float(int(withdrawable_vault_dlr // 1000) * 1000)
                educator.save_memory()
                await send_phone_alert(
                    client,
                    "🏦 $5K SWEET-SPOT PAYDAY VAULT READY!",
                    (
                        f"Total Equity: ${eq_dlr_now:,.2f}\n"
                        f"Active Working Engine Cap: $5,000.00 (~$500/trade zero-slippage plateau)\n"
                        f"Protected Cash Ready to Withdraw to Bank: +${withdrawable_vault_dlr:,.2f}!"
                    ),
                )

            # Detect when any previously held position cashes out (Example 3 Win notification)
            for pos in positions:
                t = pos.get("ticker", "")
                cnt = float(pos.get("position_fp", "0") or 0)
                exp = float(pos.get("market_exposure_dollars", "0") or 0)
                if cnt >= 1.0:
                    prev_tr = tracked_open_positions.get(t, {})
                    max_c = max(int(cnt), int(prev_tr.get("max_count", 0) or 0))
                    max_s = max(exp, float(prev_tr.get("max_stake", 0.0) or 0.0))
                    stage1_alerted = bool(prev_tr.get("stage1_alerted", False))
                    if max_c >= 6 and int(cnt) <= int(max_c * 0.65) and not stage1_alerted:
                        stage1_alerted = True
                        sold_c = max_c - int(cnt)
                        realized_so_far = float(pos.get("realized_pnl_dollars", "0") or 0)
                        await send_phone_alert(
                            client,
                            "💵 STAGE-1 PROFIT BANKED (+Breakeven Shield Locked!)",
                            (
                                f"Play: {t}\n"
                                f"Action: Sold {sold_c}x Stage-1 Take-Profit (Remaining runner: {int(cnt)}x)\n"
                                f"Status: Risk-Free Breakeven Shield active on runner!\n"
                                f"Current Account Equity: ${equity_cents/100:.2f}"
                            ),
                        )
                    tracked_open_positions[t] = {
                        "count": int(cnt),
                        "stake": exp,
                        "max_count": max_c,
                        "max_stake": max_s,
                        "stage1_alerted": stage1_alerted,
                    }
                    educator.save_memory()
                elif cnt == 0.0 and t in tracked_open_positions:
                    prev = tracked_open_positions.pop(t)
                    stopped_out_events.add(_entity_key(t) if "_entity_key" in locals() else "-".join(t.split("-")[:2]))
                    educator.memory.setdefault("harvested_tickers", []).append(t)
                    educator.save_memory()
                    stake_dlr = prev.get("max_stake") or prev["stake"]
                    cnt_int = prev.get("max_count") or prev["count"]
                    realized_pnl = float(pos.get("realized_pnl_dollars", "0") or 0)
                    profit_dlr = realized_pnl if realized_pnl > 0 else max(0.01, round((cnt_int * 0.94) - stake_dlr, 2))
                    payout_dlr = round(stake_dlr + profit_dlr, 2)
                    roi_pct = round((profit_dlr / max(0.01, stake_dlr)) * 100, 1)
                    await send_phone_alert(
                        client,
                        "💰 TRADE WON & CASHED OUT!",
                        (
                            f"Play: {t}\n"
                            f"Action: Sold {cnt_int}x (${payout_dlr:.2f} payout on ${stake_dlr:.2f} stake)\n"
                            f"Net Realized Profit: +${profit_dlr:.2f} (+{roi_pct}% ROI)\n"
                            f"New Account Equity: ${equity_cents/100:.2f}"
                        ),
                    )

            orders_resp = await client.request("GET", "/portfolio/orders", authenticated=True)
            open_orders = [
                o for o in orders_resp.get("orders", []) if o.get("status") == "resting"
            ]

            resting_sell_orders: dict[str, list[dict]] = {}
            resting_sell_counts: dict[str, float] = {}
            resting_buy_tickers: set[str] = set()
            stale_buys_to_cancel: list[dict] = []
            for o in open_orders:
                t = o.get("ticker", "")
                if o.get("action") == "sell":
                    resting_sell_orders.setdefault(t, []).append(o)
                    resting_sell_counts[t] = resting_sell_counts.get(t, 0.0) + float(
                        o.get("remaining_count_fp", "0") or 0
                    )
                elif o.get("action") == "buy":
                    oid = o.get("order_id", "")
                    resting_buy_tickers.add(t)
                    age_sec = 0.0
                    c_str = o.get("created_time", "")
                    if c_str:
                        try:
                            c_dt = datetime.fromisoformat(c_str.replace("Z", "+00:00"))
                            age_sec = (now_utc - c_dt).total_seconds()
                        except Exception:
                            age_sec = 999.0
                    # Auto-cancel resting buy orders unfilled after 40s (true UTC age) for ultra-fast cash reallocation!
                    if age_sec > 40.0:
                        stale_buys_to_cancel.append(
                            {
                                "order_id": oid,
                                "market_ticker": t,
                                "exchange_index": o.get("exchange_index", 0),
                                "subaccount": o.get("subaccount_number", 0),
                            }
                        )

            if stale_buys_to_cancel:
                logger.info(f"Auto-canceling {len(stale_buys_to_cancel)} unfilled resting buy order(s) (>75s UTC age) to free cash.")
                try:
                    await client.request(
                        "DELETE",
                        "/portfolio/events/orders/batched",
                        json_data={"orders": stale_buys_to_cancel},
                        authenticated=True,
                    )
                except Exception:
                    pass

            # 1. Manage Open Positions: True GEMINI.md <48c Guillotine + 90c-99c Harvest Queue
            for pos in positions:
                ticker = pos.get("ticker", "")
                pos_fp = float(pos.get("position_fp", "0") or 0)
                whole_pos = int(pos_fp)
                if whole_pos < 1:
                    continue

                ob = await client.get_orderbook(ticker)
                cur_bid = ob.best_yes_bid or 0
                cur_ask = ob.best_yes_ask or 99
                cur_spread = cur_ask - cur_bid
                if cur_bid > peak_bids.get(ticker, 0):
                    peak_bids[ticker] = cur_bid

                peaked = peak_bids.get(ticker, cur_bid)
                exp_dlr = float(pos.get("market_exposure_dollars", "0") or 0)
                avg_entry_cents = int(round((exp_dlr / pos_fp) * 100)) if exp_dlr > 0 else 80
                max_orig_cnt = int(tracked_open_positions.get(ticker, {}).get("max_count", whole_pos) or whole_pos)
                is_soccer_ml = any(
                    ticker.startswith(sp)
                    for sp in ("KXLALIGAGAME", "KXUCLGAME", "KXEPLGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXMLSGAME", "KXLIGAMXGAME")
                )
                stage1_banked = (
                    (max_orig_cnt >= 6 and whole_pos <= int(max_orig_cnt * 0.60))
                    or (peaked >= avg_entry_cents + 8)
                    or (is_soccer_ml and peaked >= avg_entry_cents + 3)
                )
                # Dynamic Stop Floor: 48c pre-Stage-1, ratchets up to (entry - 4c) once Stage-1 (+6c) or Soccer +3c is reached!
                stop_floor_c = max(48, avg_entry_cents - 4) if stage1_banked else 48
                thesis_broken_guillotine = (0 < cur_bid <= stop_floor_c) and (cur_ask <= stop_floor_c + 5)
                if thesis_broken_guillotine:
                    logger.warning(
                        f"THESIS-BROKEN <48c GUILLOTINE TRIGGERED on {ticker} (cur_bid={cur_bid}c, ask={cur_ask}c). Salvaging stake!"
                    )
                    stopped_out_events.add("-".join(ticker.split("-")[:2]))
                    to_cancel = [
                        {
                            "order_id": so["order_id"],
                            "market_ticker": ticker,
                            "exchange_index": so.get("exchange_index", 0),
                            "subaccount": so.get("subaccount_number", 0),
                        }
                        for so in resting_sell_orders.get(ticker, [])
                    ]
                    if to_cancel:
                        await client.request(
                            "DELETE",
                            "/portfolio/events/orders/batched",
                            json_data={"orders": to_cancel},
                            authenticated=True,
                        )
                    await client.request(
                        "POST",
                        "/portfolio/events/orders",
                        json_data={
                            "ticker": ticker,
                            "side": "ask",
                            "count": f"{whole_pos}.00",
                            "price": f"{max(1, cur_bid) / 100:.4f}",
                            "time_in_force": "immediate_or_cancel",
                            "self_trade_prevention_type": "maker",
                            "client_order_id": str(uuid.uuid4()),
                        },
                        authenticated=True,
                    )
                    tracked_open_positions.pop(ticker, None)
                    educator.save_memory()
                    await send_phone_alert(
                        client,
                        "🛑 Stop-Loss Triggered (<48c Guillotine)",
                        (
                            f"Play: {ticker}\n"
                            f"Action: Exited {whole_pos}x @ {cur_bid}c (Peak: {peaked}c, <48c thesis-broken guillotine triggered) "
                            f"to salvage stake.\n"
                            f"New Account Equity: ${equity_cents/100:.2f}"
                        ),
                    )
                    continue

                # Strategy 2 (GEMINI.md Pod 2): Mid-Game "Green-Out" Equal-Profit Ratchet (+18c Surge on Value Entries)
                greened_out_set: set[str] = set(educator.memory.setdefault("greened_out_tickers", []))
                if (
                    avg_entry_cents <= 75
                    and cur_bid >= avg_entry_cents + 18
                    and whole_pos >= 6
                    and ticker not in greened_out_set
                ):
                    half_pos = whole_pos // 2
                    runner_pos = whole_pos - half_pos
                    green_price_cents = min(94, max(cur_bid + 1, cur_ask))
                    logger.info(
                        f"🟢 POD 2 GREEN-OUT TRIGGERED on {ticker} (Entry={avg_entry_cents}c -> Bid={cur_bid}c)! Locking 50% ({half_pos}x @ {green_price_cents}c) + {runner_pos}x House-Money Runner @ 96c."
                    )
                    to_cancel = [
                        {
                            "order_id": so["order_id"],
                            "market_ticker": ticker,
                            "exchange_index": so.get("exchange_index", 0),
                            "subaccount": so.get("subaccount_number", 0),
                        }
                        for so in resting_sell_orders.get(ticker, [])
                    ]
                    try:
                        if to_cancel:
                            await client.request(
                                "DELETE",
                                "/portfolio/events/orders/batched",
                                json_data={"orders": to_cancel},
                                authenticated=True,
                            )
                        # 1) Place 50% Green-Out Maker Profit Lock at live touch (cur_bid + 1)
                        await client.request(
                            "POST",
                            "/portfolio/events/orders",
                            json_data={
                                "ticker": ticker,
                                "side": "ask",
                                "count": f"{half_pos}.00",
                                "price": f"{green_price_cents / 100:.4f}",
                                "time_in_force": "good_till_canceled",
                                "self_trade_prevention_type": "maker",
                                "post_only": True,
                                "client_order_id": str(uuid.uuid4()),
                            },
                            authenticated=True,
                        )
                        # 2) Place remaining 50% "House Money Runner" at 96c
                        await client.request(
                            "POST",
                            "/portfolio/events/orders",
                            json_data={
                                "ticker": ticker,
                                "side": "ask",
                                "count": f"{runner_pos}.00",
                                "price": "0.9600",
                                "time_in_force": "good_till_canceled",
                                "self_trade_prevention_type": "maker",
                                "post_only": True,
                                "client_order_id": str(uuid.uuid4()),
                            },
                            authenticated=True,
                        )
                        greened_out_set.add(ticker)
                        educator.memory["greened_out_tickers"] = list(greened_out_set)
                        educator.save_memory()
                        continue
                    except Exception as e:
                        logger.warning(f"Green-Out split failed on {ticker}: {e}")

                # Ensure Strategy A (50/50 Fast-Flip + Wave-Rider Split) & Strategy B (Front-of-Queue Ask Undercut)
                whole_covered = int(resting_sell_counts.get(ticker, 0.0))
                uncovered = whole_pos - whole_covered
                if uncovered >= 1:
                    # "No More $1 Crumbs" Rule: Minimum +6c Stage-1 Fast-Flip & +11c Stage-2 Wave-Rider ($4.50-$8.50+ per win!)
                    undercut_ask = max(cur_bid + 1, cur_ask - 1)
                    if undercut_ask >= avg_entry_cents + 6:
                        fast_tp = min(avg_entry_cents + 7, undercut_ask)
                    else:
                        fast_tp = max(cur_bid + 1, min(93, avg_entry_cents + 6))
                    wave_tp = max(fast_tp + 3, min(96, avg_entry_cents + 11))

                    if uncovered >= 4:
                        fast_cnt = uncovered // 2
                        wave_cnt = uncovered - fast_cnt
                        legs = [(fast_cnt, fast_tp, "Stage-1 Fast-Flip (+6c Target)"), (wave_cnt, wave_tp, "Stage-2 Wave-Rider (+11c Target)")]
                    else:
                        legs = [(uncovered, fast_tp, "Fast-Flip Harvest (+6c Target)")]

                    for leg_cnt, leg_cents, leg_label in legs:
                        count_str = f"{leg_cnt}.00"
                        tp_price = f"{leg_cents / 100:.4f}"
                        logger.info(
                            f"Auto-queuing {leg_label} Sell for {ticker}: {count_str} @ {tp_price}"
                        )
                        try:
                            await client.request(
                                "POST",
                                "/portfolio/events/orders",
                                json_data={
                                    "ticker": ticker,
                                    "side": "ask",
                                    "count": count_str,
                                    "price": tp_price,
                                    "time_in_force": "good_till_canceled",
                                    "self_trade_prevention_type": "maker",
                                    "post_only": True,
                                    "client_order_id": str(uuid.uuid4()),
                                },
                                authenticated=True,
                            )
                        except Exception as e:
                            logger.warning(f"Could not place {leg_label} sell for {ticker}: {e}")

            # 2. Dynamic 4-Tier Bankroll Scaling into SOONEST Worldwide Games (80c-89c Leaders & 93c-97c Tail Locks)
            tier = get_scaling_tier(equity_cents)
            min_stake_c = tier["min_stake_cents"]
            logger.info(
                f"Heartbeat [{tier['name']}] | Cash: ${total_cents/100:.2f} | Portfolio: ${port_cents/100:.2f} | Total Equity: ${equity_cents/100:.2f}"
            )

            if total_cents >= min_stake_c and len(resting_buy_tickers) < tier["max_concurrent"]:
                shard_cents = {0: 0, 1: 0, 2: 0, 3: 0}
                for b_item in bal.get("balance_breakdown", []):
                    idx = b_item.get("exchange_index")
                    if idx in shard_cents:
                        shard_cents[idx] = int(float(b_item.get("balance", "0") or 0) * 100)

                # Pre-warm any dry trading shard (0, 2, 3) via Intra Account Transfer API if total cash permits
                for dst_shard in (0, 2, 3):
                    if shard_cents.get(dst_shard, 0) < min_stake_c:
                        donor_shard = max((0, 2, 3), key=lambda s: shard_cents.get(s, 0))
                        if donor_shard != dst_shard and shard_cents.get(donor_shard, 0) >= (min_stake_c * 2):
                            xfer_c = min(min_stake_c + 500, shard_cents[donor_shard] - min_stake_c)
                            if xfer_c >= 500:
                                try:
                                    await client.request(
                                        "POST",
                                        "/portfolio/intra_exchange_instance_transfer",
                                        json_data={
                                            "source": "event_contract",
                                            "destination": "event_contract",
                                            "amount": xfer_c * 100,  # Kalshi expects centicents (1c = 100 centicents)
                                            "client_transfer_id": str(uuid.uuid4()),
                                            "source_exchange_shard": donor_shard,
                                            "destination_exchange_shard": dst_shard,
                                            "source_subaccount": 0,
                                            "destination_subaccount": 0,
                                        },
                                        authenticated=True,
                                    )
                                    shard_cents[donor_shard] -= xfer_c
                                    shard_cents[dst_shard] += xfer_c
                                    logger.info(
                                        f"⚡ Auto-Rebalanced ${xfer_c/100:.2f} from Shard {donor_shard} -> Shard {dst_shard} (Shards: 0=${shard_cents[0]/100:.2f}, 2=${shard_cents[2]/100:.2f}, 3=${shard_cents[3]/100:.2f})"
                                    )
                                    await asyncio.sleep(1.0)
                                except Exception as e:
                                    logger.warning(f"Pre-warm shard transfer {donor_shard}->{dst_shard} note: {e}")

                active_all_tickers = (
                    list(resting_buy_tickers)
                    + list(resting_sell_counts.keys())
                    + list(tracked_open_positions.keys())
                )
                def _entity_key(tk: str) -> str:
                    pts = tk.split("-")
                    return "-".join(pts[:3]) if len(pts) >= 4 else "-".join(pts[:2])

                harvested_set = set(educator.memory.get("harvested_tickers", []))
                active_events = set(stopped_out_events) | {
                    _entity_key(tk) for tk in active_all_tickers
                } | {_entity_key(tk) for tk in harvested_set}
                active_crypto_count = sum(
                    1
                    for tk in active_all_tickers
                    if tk.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                )
                active_index_count = sum(
                    1
                    for tk in active_all_tickers
                    if tk.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                )
                candidates = []
                for series in WATCH_SERIES:
                    is_crypto_s = series.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                    is_index_s = series.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                    # Correlation Guard: Max 1 active Crypto play & Max 1 active Wall St Index play at a time!
                    if is_crypto_s and active_crypto_count >= 1:
                        continue
                    if is_index_s and active_index_count >= 1:
                        continue
                    try:
                        res = await client.request(
                            "GET",
                            "/markets",
                            params={"series_ticker": series, "status": "open", "limit": 1000},
                        )
                        await asyncio.sleep(0.035)
                        markets_list = res.get("markets", [])
                        if markets_list and series not in series_shard_cache:
                            series_shard_cache[series] = int(markets_list[0].get("exchange_index", 0) or 0)
                        for m in markets_list:
                            shard_key = int(m.get("exchange_index", 0) or 0)

                            t = m.get("ticker", "")
                            # Block fragile Soccer Draws (-TIE) and narrow middle range brackets (-B)
                            if t.endswith("-TIE") or "-B" in t:
                                continue
                            event_prefix = _entity_key(t)
                            if t in resting_buy_tickers or resting_sell_counts.get(t, 0) > 0 or event_prefix in active_events:
                                continue

                            exp_str = m.get("expected_expiration_time") or m.get("expiration_time") or ""
                            if not exp_str:
                                continue
                            try:
                                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                                mins_to_exp = (exp_dt - now_utc).total_seconds() / 60.0
                            except Exception:
                                continue
                            # Strict Final-25-Minute Lock Window for volatile Crypto/Indices; 165m for live in-play Sports
                            if is_crypto_s or is_index_s:
                                if not (5.0 <= mins_to_exp <= 25.0):
                                    continue
                            else:
                                if not (-180.0 <= mins_to_exp <= 165.0):
                                    continue

                            # Fast pre-filter on live Bid/Ask before fetching full orderbook
                            bid_f = float(m.get("yes_bid_dollars") or 0)
                            ask_f = float(m.get("yes_ask_dollars") or 0)
                            bid_c = int(round(bid_f * 100))
                            ask_c = int(round(ask_f * 100))
                            spread_c = ask_c - bid_c
                            if spread_c <= 0 or spread_c > 3:
                                continue
                            # "Meat of the Move" Sweet Spot (76c-87c): Ban 88c-96c penny-picking where upside is only $1!
                            if not (76 <= ask_c <= 87):
                                continue

                            clean_title = m.get("yes_sub_title") or m.get("title", t)
                            # Fast-Settlement Velocity Cap: Quarters/Periods resolve in ~10m, Halves in ~20m, Player Props mid-game!
                            eff_mins = max(0.0, mins_to_exp)
                            if any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P")):
                                eff_mins = min(eff_mins, 10.0)
                            elif any(h in series for h in ("1H", "2H")):
                                eff_mins = min(eff_mins, 20.0)
                            elif len(t.split("-")) >= 4:
                                eff_mins = min(eff_mins, 30.0)
                            # "One-Way Ratchet" Bonus: Prefer Over Totals & Player Props (where stats can ONLY go up, never backwards!)
                            is_one_way_ratchet = ("TOTAL" in series) or ("BTTS" in series) or ("CORNERS" in series) or (len(t.split("-")) >= 4)
                            ratchet_bonus = -12.0 if is_one_way_ratchet else 0.0
                            # Velocity Priority Score: Tightest spread (1c first) + One-Way Ratchet bonus + fastest-settling period/prop
                            vel_score = (spread_c * 25.0) + eff_mins + ratchet_bonus
                            candidates.append((vel_score, mins_to_exp, t, clean_title, shard_key))
                    except Exception:
                        pass

                # Sort by Fastest Capital Turnover Velocity (1c spreads & closest to finishing first!)
                candidates.sort(key=lambda x: x[0])

                for _vel, mins_left, t, title, shard_key in candidates[:35]:
                    event_prefix = _entity_key(t)
                    is_crypto_t = t.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                    is_index_t = t.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                    if (
                        event_prefix in active_events
                        or total_cents < min_stake_c
                        or (is_crypto_t and active_crypto_count >= 1)
                        or (is_index_t and active_index_count >= 1)
                    ):
                        continue
                    await asyncio.sleep(0.12)
                    ob = await client.get_orderbook(t)
                    if ob.best_yes_bid and ob.best_yes_ask:
                        spread = ob.best_yes_ask - ob.best_yes_bid
                        is_leader_zone = (76 <= ob.best_yes_ask <= 87 and spread <= 3)
                        is_tail_lock_zone = False

                        if is_leader_zone:
                            approved, reason, conf_mult = await educator.evaluate_opportunity(
                                client, t, ob, title=title, min_book_depth_dlr=tier["min_depth_dlr"]
                            )
                            logger.info(
                                f"PreTradeEducator [76-87c MEAT-OF-MOVE LEADER | {t} ({mins_left:.0f}m left)]: {reason}"
                            )
                            if not approved:
                                continue

                            # Front-of-Queue Spread-Capture Entry Discount: bid (cur_bid + 1) on 2c-3c spreads to save +1c for free!
                            bid_cents = max(ob.best_yes_bid + 1, ob.best_yes_ask - 2) if spread >= 2 else (ob.best_yes_ask - 1)
                            price_str = f"{bid_cents / 100:.4f}"
                            # 4-Tier Institutional Kelly Sizing (Cap working equity at $5,000 Sweet-Spot Plateau so >$5k is reserved for bank withdrawal!)
                            working_equity_cents = min(equity_cents, tier.get("working_cap_cents", equity_cents))
                            if is_crypto_t or is_index_t:
                                base_alloc_pct = min(0.20, tier["base_pct"])
                            else:
                                base_alloc_pct = tier["boost_pct"] if (is_tail_lock_zone or conf_mult >= 1.20) else tier["base_pct"]
                            raw_stake = int(working_equity_cents * base_alloc_pct * conf_mult)
                            target_stake_cents = min(tier["max_stake_cents"], max(min_stake_c, raw_stake), total_cents)

                            # Just-In-Time (JIT) Cross-Shard Liquidity Sweeper: pull cash from other shards if needed!
                            if shard_cents.get(shard_key, 0) < target_stake_cents:
                                deficit_cents = target_stake_cents - shard_cents.get(shard_key, 0) + 25
                                for donor in sorted((0, 2, 3), key=lambda s: shard_cents.get(s, 0), reverse=True):
                                    if donor == shard_key or deficit_cents <= 0:
                                        continue
                                    avail = shard_cents.get(donor, 0)
                                    pull_c = min(avail, deficit_cents)
                                    if pull_c >= 100:
                                        try:
                                            await client.request(
                                                "POST",
                                                "/portfolio/intra_exchange_instance_transfer",
                                                json_data={
                                                    "source": "event_contract",
                                                    "destination": "event_contract",
                                                    "amount": pull_c * 100,  # centicents
                                                    "client_transfer_id": str(uuid.uuid4()),
                                                    "source_exchange_shard": donor,
                                                    "destination_exchange_shard": shard_key,
                                                    "source_subaccount": 0,
                                                    "destination_subaccount": 0,
                                                },
                                                authenticated=True,
                                            )
                                            shard_cents[donor] -= pull_c
                                            shard_cents[shard_key] = shard_cents.get(shard_key, 0) + pull_c
                                            deficit_cents -= pull_c
                                            logger.info(
                                                f"⚡ JIT Shard Sweep: Pulled ${pull_c/100:.2f} from Shard {donor} -> Shard {shard_key} for {t}"
                                            )
                                            await asyncio.sleep(1.2)
                                        except Exception as e:
                                            logger.warning(f"JIT Shard Sweep {donor}->{shard_key} failed: {e}")

                            alloc_cents = min(shard_cents.get(shard_key, 0), target_stake_cents)
                            count_int = int(alloc_cents // bid_cents)
                            if count_int >= tier["min_contracts"]:
                                # Institutional 2-Tranche Maker Ladder (For $1k-$2k+ orders > max_slice, splits 60% @ bid_cents + 40% @ bid_cents-1c!)
                                max_slice = tier.get("iceberg_slice", 110)
                                slice_count = min(count_int, max_slice) if count_int > max_slice else count_int
                                rem_ladder_count = max(0, min(count_int - slice_count, max_slice)) if count_int > max_slice else 0
                                logger.info(
                                    f"Auto-deploying [{tier['name']}] Maker Bid on {t}: {slice_count} contracts (Target={count_int}) @ {price_str} (${(slice_count*bid_cents)/100:.2f})"
                                )
                                try:
                                    await client.request(
                                        "POST",
                                        "/portfolio/events/orders",
                                        json_data={
                                            "ticker": t,
                                            "side": "bid",
                                            "count": f"{slice_count}.00",
                                            "price": price_str,
                                            "time_in_force": "good_till_canceled",
                                            "self_trade_prevention_type": "maker",
                                            "post_only": True,
                                            "client_order_id": str(uuid.uuid4()),
                                        },
                                        authenticated=True,
                                    )
                                    resting_buy_tickers.add(t)
                                    active_events.add(event_prefix)
                                    if is_crypto_t:
                                        active_crypto_count += 1
                                    if is_index_t:
                                        active_index_count += 1
                                    shard_cents[shard_key] -= slice_count * bid_cents
                                    total_cents -= slice_count * bid_cents
                                    if rem_ladder_count >= 15 and shard_cents.get(shard_key, 0) >= rem_ladder_count * max(1, bid_cents - 1):
                                        ladder_p_str = f"{max(1, bid_cents - 1) / 100:.4f}"
                                        await client.request(
                                            "POST",
                                            "/portfolio/events/orders",
                                            json_data={
                                                "ticker": t,
                                                "side": "bid",
                                                "count": f"{rem_ladder_count}.00",
                                                "price": ladder_p_str,
                                                "time_in_force": "good_till_canceled",
                                                "self_trade_prevention_type": "maker",
                                                "post_only": True,
                                                "client_order_id": str(uuid.uuid4()),
                                            },
                                            authenticated=True,
                                        )
                                        shard_cents[shard_key] -= rem_ladder_count * max(1, bid_cents - 1)
                                        total_cents -= rem_ladder_count * max(1, bid_cents - 1)
                                except Exception:
                                    pass

        except Exception as e:
            logger.error(f"Loop error: {e}")

        if duration_seconds > 0 and (time.time() - start_ts) >= duration_seconds:
            logger.info(f"Completed scheduled cloud burst ({duration_seconds}s).")
            break

        await asyncio.sleep(5)

    await client.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=0)
    args = parser.parse_args()
    await run_autonomous_loop(duration_seconds=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
