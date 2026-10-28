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

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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
    """Sends instant 24/7 push notification to phone via macOS iMessage, Cloud SMS, Telegram, Slack, and ntfy.sh."""
    logger.info(f"📱 PHONE ALERT: {title} — {message}")
    hc = await client.get_client()

    # 1. Direct macOS iMessage text to +19493107304 when Mac is awake
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

    # 2. Cloud-native push via ntfy.sh (works 24/7 on GitHub Actions & local with 0 setup + Lock-Screen Action Buttons!)
    cmd_topic = f"{NTFY_TOPIC}-cmd"
    try:
        await hc.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            content=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "chart_with_upwards_trend,moneybag",
                "Actions": (
                    f"http, 📊 Status, https://ntfy.sh/{cmd_topic}, body=status, clear=true; "
                    f"http, 💼 Positions, https://ntfy.sh/{cmd_topic}, body=positions, clear=true; "
                    f"http, ⏸️ Pause/Resume, https://ntfy.sh/{cmd_topic}, body=toggle_pause, clear=true"
                ),
            },
        )
    except Exception:
        pass

    # 3. Telegram Bot 24/7 Instant Push + Interactive 2-Way Command Keyboard (works on Mac & GitHub Cloud when Mac is OFF)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or getattr(settings, "TELEGRAM_CHAT_ID", None)
    if tg_token and tg_chat:
        try:
            await hc.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={
                    "chat_id": tg_chat,
                    "text": f"*{title}*\n{message}",
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "keyboard": [
                            [{"text": "📊 Live Status"}, {"text": "💼 Positions"}],
                            [{"text": "⏸️ Pause Buys"}, {"text": "▶️ Resume Buys"}],
                        ],
                        "resize_keyboard": True,
                    },
                },
            )
        except Exception:
            pass

    # 4. Slack Incoming Webhook 24/7 Instant Push (works on Mac & GitHub Cloud when Mac is OFF)
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if slack_webhook:
        try:
            await hc.post(
                slack_webhook,
                json={"text": f"*{title}*\n{message}"},
            )
        except Exception:
            pass

    # 5. Direct 24/7 Cloud SMS to +19493107304 (Textbelt or Twilio when Mac is OFF or SMS_ALWAYS=1)
    textbelt_key = os.environ.get("TEXTBELT_API_KEY", "").strip()
    if textbelt_key and IMESSAGE_TARGET and (sys.platform != "darwin" or os.environ.get("SMS_ALWAYS") == "1"):
        try:
            await hc.post(
                "https://textbelt.com/text",
                data={"phone": IMESSAGE_TARGET, "message": f"{title}\n{message}", "key": textbelt_key},
            )
        except Exception:
            pass

    tw_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    tw_tok = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    tw_from = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if tw_sid and tw_tok and tw_from and IMESSAGE_TARGET and (sys.platform != "darwin" or os.environ.get("SMS_ALWAYS") == "1"):
        try:
            await hc.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{tw_sid}/Messages.json",
                data={"To": IMESSAGE_TARGET, "From": tw_from, "Body": f"{title}\n{message}"},
                auth=(tw_sid, tw_tok),
            )
        except Exception:
            pass


async def poll_two_way_commands(
    client: KalshiClient,
    educator,
    total_cents: int,
    port_cents: int,
    equity_cents: int,
    positions: list[dict],
    open_orders: list[dict],
) -> None:
    """
    Polls incoming 2-way commands from Telegram Bot AND ntfy.sh every 6 seconds
    so the user can text/tap the bot 24/7 even when the Mac is completely turned OFF!
    Supported commands: status, how we doing, positions, bets, pause, resume, toggle_pause
    """
    now_t = time.time()
    if now_t - getattr(educator, "_last_cmd_poll_ts", 0.0) < 6.0:
        return
    educator._last_cmd_poll_ts = now_t
    hc = await client.get_client()
    incoming_cmds: list[str] = []

    # A. Poll ntfy.sh command channel (from lock-screen buttons or ntfy app chat)
    cmd_topic = f"{NTFY_TOPIC}-cmd"
    last_ntfy_id = educator.memory.get("last_ntfy_cmd_id", "")
    try:
        r_nt = await hc.get(f"https://ntfy.sh/{cmd_topic}/json?poll=1&since=20s", timeout=2.5)
        if r_nt.status_code == 200 and r_nt.text.strip():
            for line in r_nt.text.strip().splitlines():
                obj = json.loads(line)
                mid = obj.get("id", "")
                msg_txt = (obj.get("message") or "").strip().lower()
                if mid and mid != last_ntfy_id and msg_txt:
                    last_ntfy_id = mid
                    educator.memory["last_ntfy_cmd_id"] = mid
                    incoming_cmds.append(msg_txt)
    except Exception:
        pass

    # B. Poll Telegram Bot getUpdates (2-way chat from anywhere in the world with Mac OFF!)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if tg_token:
        offset = int(educator.memory.get("tg_update_offset", 0) or 0)
        try:
            r_tg = await hc.get(
                f"https://api.telegram.org/bot{tg_token}/getUpdates",
                params={"offset": offset + 1, "timeout": 0},
                timeout=2.5,
            )
            if r_tg.status_code == 200:
                for upd in r_tg.json().get("result", []):
                    uid = int(upd.get("update_id", 0))
                    if uid > offset:
                        offset = uid
                        educator.memory["tg_update_offset"] = offset
                    msg_obj = upd.get("message") or {}
                    chat_id = str((msg_obj.get("chat") or {}).get("id") or "")
                    if chat_id and not os.environ.get("TELEGRAM_CHAT_ID"):
                        os.environ["TELEGRAM_CHAT_ID"] = chat_id
                        educator.memory["telegram_chat_id"] = chat_id
                    txt = (msg_obj.get("text") or "").strip().lower()
                    if txt:
                        incoming_cmds.append(txt)
        except Exception:
            pass

    if not incoming_cmds:
        return

    educator.save_memory()
    cmd = incoming_cmds[-1]
    day_str = datetime.now().strftime("%Y-%m-%d")
    day_led = educator.memory.get("daily_ledger", {}).get(day_str, {})
    net_today = float(day_led.get("net_change_dlr", 0.0) or 0.0)
    pct_today = float(day_led.get("net_change_pct", 0.0) or 0.0)
    q_st = educator.get_daily_quota_status()
    engine_mode = "☁️ GITHUB CLOUD 24/7" if sys.platform != "darwin" else "💻 MAC PRIMARY ENGINE"

    if any(k in cmd for k in ("pause", "stop", "toggle_pause")) and "resume" not in cmd:
        cur_p = bool(educator.memory.get("manual_pause_buys", False))
        new_p = (not cur_p) if "toggle" in cmd else True
        educator.memory["manual_pause_buys"] = new_p
        educator.save_memory()
        state_lbl = "⏸️ NEW BUYS PAUSED (Open positions still managed!)" if new_p else "▶️ RESUMED FULL AUTONOMOUS HUNTING!"
        await send_phone_alert(
            client,
            "⚙️ 2-Way Command Executed",
            f"{state_lbl}\nEngine: {engine_mode}\nEquity: ${equity_cents/100:.2f} (Today: {net_today:+.2f})",
        )
    elif any(k in cmd for k in ("resume", "start", "go")):
        educator.memory["manual_pause_buys"] = False
        educator.save_memory()
        await send_phone_alert(
            client,
            "▶️ Autonomous Hunting Resumed!",
            f"Engine: {engine_mode}\nCash Ready: ${total_cents/100:.2f} | Equity: ${equity_cents/100:.2f}",
        )
    elif any(k in cmd for k in ("pos", "bet", "open")):
        active_p = [
            f"• {p.get('ticker')}: {float(p.get('position_fp',0)):.0f}x (${float(p.get('market_exposure_dollars',0)):.2f})"
            for p in positions
            if float(p.get("position_fp", "0") or 0) >= 1.0
        ]
        pos_body = "\n".join(active_p) if active_p else "None (100% Liquid Cash hunting next A+ setup!)"
        await send_phone_alert(
            client,
            f"💼 Active Positions ({len(active_p)})",
            f"{pos_body}\nResting Orders: {len(open_orders)}\nCash: ${total_cents/100:.2f} | Equity: ${equity_cents/100:.2f}",
        )
    else:
        dist_750 = max(0.0, 750.0 - (equity_cents / 100.0))
        await send_phone_alert(
            client,
            "📊 Live 24/7 Command Report",
            (
                f"Engine: {engine_mode}\n"
                f"Total Equity: ${equity_cents/100:.2f} (Cash: ${total_cents/100:.2f} | Port: ${port_cents/100:.2f})\n"
                f"Today's Net Profit: {net_today:+.2f} ({pct_today:+.2f}%)\n"
                f"Daily Quota: {q_st['completed_bets']}/{q_st['target_bets']} ({q_st['wins']}W-{q_st['losses']}L)\n"
                f"Distance to $750 Tier 4: ${dist_750:.2f}"
            ),
        )


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
    "KXMLBGAME",
    "KXWTAMATCH",
    "KXATPMATCH",
    "KXATPCHALLENGERMATCH",
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
        self.odds_api_cache: dict[str, tuple[float, list]] = {}
        self.sleeper_cache: dict[str, tuple[float, dict, set, set]] = {}
        self.espn_scoreboard_cache: dict[str, tuple[float, list]] = {}
        self.espn_summary_cache: dict[str, tuple[float, dict]] = {}
        self.odds_api_key = (
            os.environ.get("ODDS_API_KEY")
            or os.environ.get("THE_ODDS_API_KEY")
            or ""
        )

    async def _get_espn_events_cached(self, hc, sport_path: str, ttl: float = 4.0) -> list:
        now_t = time.time()
        c_ts, c_ev = self.espn_scoreboard_cache.get(sport_path, (0.0, []))
        if (now_t - c_ts) <= ttl and c_ev:
            return c_ev
        try:
            r = await hc.get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}", timeout=3.5)
            events = r.json().get("events", [])
            if events:
                self.espn_scoreboard_cache[sport_path] = (now_t, events)
                return events
        except Exception:
            if (now_t - c_ts) <= 30.0 and c_ev:
                return c_ev
            raise
        return c_ev

    async def _get_espn_summary_cached(self, hc, sport_base: str, ev_id: str, ttl: float = 4.0) -> dict:
        key = f"{sport_base}:{ev_id}"
        now_t = time.time()
        c_ts, c_sum = self.espn_summary_cache.get(key, (0.0, {}))
        if (now_t - c_ts) <= ttl and c_sum:
            return c_sum
        try:
            r_box = await hc.get(
                f"https://site.api.espn.com/apis/site/v2/sports/{sport_base}/summary?event={ev_id}",
                timeout=3.5,
            )
            data = r_box.json()
            if data:
                self.espn_summary_cache[key] = (now_t, data)
                return data
        except Exception:
            if (now_t - c_ts) <= 30.0 and c_sum:
                return c_sum
            raise
        return c_sum

    @staticmethod
    def classify_archetype(ticker: str) -> str:
        series = ticker.split("-")[0] if "-" in ticker else ticker
        if any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1H", "2H", "1P", "2P", "3P")):
            return "FAST_PERIOD_MARKET"
        if any(p in series for p in ("PASSYDS", "PASSTD", "RECYDS", "REC")):
            return "NFL_PASS_PROP"
        if "RUSHYDS" in series or "ANYTD" in series:
            return "NFL_RUSH_TD_PROP"
        if any(p in series for p in ("NBAPTS", "NBAREB", "NBAAST", "NBA3PT", "NBAPRA", "NBAPR", "NBAPA", "NBABLK", "NBASTL")):
            return "NBA_PLAYER_PROP"
        if any(p in series for p in ("NHLPTS", "NHLGOAL", "NHLAST", "NHLSOG", "UCLSOA")):
            return "HOCKEY_SOCCER_PROP"
        if "TOTAL" in series:
            return "FULL_GAME_TOTAL"
        if "SPREAD" in series:
            return "FULL_GAME_SPREAD"
        if series in ("KXATPMATCH", "KXWTAMATCH"):
            return "TENNIS_MATCH"
        if any(s in series for s in ("LALIGA", "UCL", "EPL", "SERIEA", "BUNDESLIGA", "MLS", "LIGAMX", "LIGUE1")):
            return "SOCCER_MONEYLINE"
        if any(c in series for c in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "INX", "NASDAQ", "WTI", "HIGH")):
            return "MACRO_CRYPTO_WEATHER"
        return "FULL_GAME_MONEYLINE"

    def record_trade_autopsy(
        self,
        ticker: str,
        entry_cents: int,
        exit_cents: int,
        peak_cents: int,
        net_pnl_dlr: float,
        exit_reason: str,
    ) -> None:
        """
        Records a structured Post-Trade Winner/Loser Autopsy and updates Archetype & Daily Quota learning.
        """
        now_local = datetime.now()
        day_str = now_local.strftime("%Y-%m-%d")
        arch = self.classify_archetype(ticker)
        is_win = net_pnl_dlr >= 0.0
        if is_win:
            diagnosis = "CLEAN_MAKER_HARVEST" if exit_cents >= entry_cents + 5 else "STAGE1_PROFIT_SHIELD_SAVE"
        else:
            if "TOTAL" in ticker or "PTS" in ticker or "YDS" in ticker:
                diagnosis = "LATE_PACE_OR_GAME_SCRIPT_STALL"
            elif peak_cents >= entry_cents + 4:
                diagnosis = "UNBANKED_REVERSAL_AFTER_LEAD"
            else:
                diagnosis = "SCOREBOARD_MOMENTUM_FLIP"

        autopsies = self.memory.setdefault("trade_autopsy_log", [])
        autopsies.append(
            {
                "timestamp": now_local.strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": ticker,
                "archetype": arch,
                "outcome": "WIN" if is_win else "LOSS",
                "entry_cents": entry_cents,
                "peak_cents": peak_cents,
                "exit_cents": exit_cents,
                "net_pnl_dlr": round(net_pnl_dlr, 2),
                "diagnosis": diagnosis,
                "exit_reason": exit_reason,
            }
        )
        if len(autopsies) > 250:
            self.memory["trade_autopsy_log"] = autopsies[-250:]

        arch_map = self.memory.setdefault("archetype_learning", {})
        a_stat = arch_map.setdefault(arch, {"wins": 0, "losses": 0, "realized_pnl": 0.0, "last_diagnosis": ""})
        if is_win:
            a_stat["wins"] = int(a_stat.get("wins", 0)) + 1
        else:
            a_stat["losses"] = int(a_stat.get("losses", 0)) + 1
        a_stat["realized_pnl"] = round(float(a_stat.get("realized_pnl", 0.0)) + net_pnl_dlr, 2)
        a_stat["last_diagnosis"] = diagnosis

        # Update Daily Smart Bet Quota Tracker (Target: 25 High-Conviction Maker Bets / Day)
        q_map = self.memory.setdefault("daily_quota_tracker", {})
        q_day = q_map.setdefault(
            day_str,
            {"target_bets": 25, "completed_bets": 0, "wins": 0, "losses": 0, "net_pnl_dlr": 0.0},
        )
        q_day["completed_bets"] = int(q_day.get("completed_bets", 0)) + 1
        if is_win:
            q_day["wins"] = int(q_day.get("wins", 0)) + 1
        else:
            q_day["losses"] = int(q_day.get("losses", 0)) + 1
        q_day["net_pnl_dlr"] = round(float(q_day.get("net_pnl_dlr", 0.0)) + net_pnl_dlr, 2)

        # Anti-Martingale Win/Loss Streak & House-Money Pool Tracker:
        # - Shrinks to Conservative Defense Mode (7% stake) when losing/rebuilding
        # - Expands freely in House-Money Mode (up to 18-20% stake) when on a 2+ Win Streak, while capping max -10c stop risk at <=35% of banked profit!
        st_state = self.memory.setdefault(
            "streak_state",
            {"win_streak": 0, "loss_streak": 0, "house_money_pool_dlr": 0.0, "mode": "CONSERVATIVE_DEFENSE"},
        )
        if is_win:
            st_state["win_streak"] = int(st_state.get("win_streak", 0)) + 1
            st_state["loss_streak"] = 0
            st_state["house_money_pool_dlr"] = round(max(0.0, float(st_state.get("house_money_pool_dlr", 0.0)) + net_pnl_dlr), 2)
        else:
            is_scout_loss = bool(self.memory.get("last_scout_flags", {}).get(ticker, False)) or abs(net_pnl_dlr) <= 3.50
            new_pool = round(max(0.0, float(st_state.get("house_money_pool_dlr", 0.0)) + net_pnl_dlr), 2)
            st_state["house_money_pool_dlr"] = new_pool
            if is_scout_loss and new_pool > 0.0 and int(st_state.get("win_streak", 0)) >= 2:
                # Wall Street Asymmetric Scout Shield: a tiny -$2.50 Scout scratch only steps streak back -1 (never nukes a Gear-3/4 heater to 0!)
                st_state["win_streak"] = max(1, int(st_state.get("win_streak", 0)) - 1)
            else:
                st_state["win_streak"] = 0
                st_state["loss_streak"] = int(st_state.get("loss_streak", 0)) + 1

        w_strk = int(st_state["win_streak"])
        hm_pool = float(st_state["house_money_pool_dlr"])
        if w_strk >= 2 and hm_pool >= 6.0:
            st_state["mode"] = f"HOUSE_MONEY_OFFENSE({w_strk}W_Streak,+${hm_pool:.2f}_Pool)"
        elif w_strk == 1 and hm_pool > 0.0:
            st_state["mode"] = f"STANDARD_BUILDER(1W,+${hm_pool:.2f}_Pool)"
        else:
            st_state["mode"] = f"CONSERVATIVE_DEFENSE({st_state['loss_streak']}L)"
        self.save_memory()

    def get_streak_sizing(self, equity_cents: int, bid_cents: int, ticker: str = "") -> tuple[int, str, float]:
        """
        Combines 4-Gear Daily Streak Scaling with 3-Tier Play Action Sizing:
        - TIER C (SCOUT SCALP, CONF 68-75 or early P1/P2 / $80-$290 book depth):
          *$24-$32 stake (~30-38 contracts -> +$2.00 to +$3.00 profit/win, only -$2.40 max risk at -8c stop!).
        - TIER B (BUILDER SCALP, CONF 76-83):
          *$54-$90 stake (~68-110 contracts -> +$4.50 to +$8.50 profit/win).
        - TIER A (A+ SNIPER / HEATER, CONF >= 84):
          *Full 4-Gear Streak Stake ($65 in Gear 1 -> $90 in Gear 2 -> $130 in Gear 3 -> $155-$195 in Gear 4 -> +$10 to +$19/win!).
        """
        st_state = self.memory.setdefault(
            "streak_state",
            {"win_streak": 0, "loss_streak": 0, "house_money_pool_dlr": 0.0, "mode": "CONSERVATIVE_DEFENSE"},
        )
        w_strk = int(st_state.get("win_streak", 0) or 0)
        l_strk = int(st_state.get("loss_streak", 0) or 0)
        hm_pool_dlr = float(st_state.get("house_money_pool_dlr", 0.0) or 0.0)
        hm_pool_cents = int(round(hm_pool_dlr * 100))

        conf_sc = int(self.memory.get("last_conf_scores", {}).get(ticker, 80) or 80) if ticker else 80
        is_scout = bool(self.memory.get("last_scout_flags", {}).get(ticker, False)) if ticker else False

        if is_scout or conf_sc <= 75:
            scout_c = min(3200, max(2200, int(equity_cents * 0.065)))
            return scout_c, f"🎯 TIER-C SCOUT SCALP ($28 Cap, Conf={conf_sc}/100 -> +$2-$3 Target)", 0.0

        if w_strk >= 4 and hm_pool_dlr >= 25.0:
            base_c = min(19500, int(equity_cents * 0.36), int(equity_cents * 0.24) + int(hm_pool_cents * 0.85))
            gear_lbl = f"🚀 GEAR-4 HEATER ({w_strk}W, Pool=+${hm_pool_dlr:.2f})"
        elif w_strk >= 2 and hm_pool_dlr >= 4.0:
            base_c = min(13500, int(equity_cents * 0.28), int(equity_cents * 0.20) + int(hm_pool_cents * 0.75))
            gear_lbl = f"🔥 GEAR-3 HOUSE-MONEY ({w_strk}W, Pool=+${hm_pool_dlr:.2f})"
        elif w_strk >= 1:
            base_c = min(9200, int(equity_cents * 0.20))
            gear_lbl = f"⚖️ GEAR-2 BUILDER ({w_strk}W, Pool=+${hm_pool_dlr:.2f})"
        else:
            base_c = min(6800, max(4800, int(equity_cents * 0.16)))
            gear_lbl = f"🛡️ GEAR-1 RECOVERY ({l_strk}L)"

        if conf_sc <= 82:
            builder_c = max(4800, int(base_c * 0.85))
            return builder_c, f"⚡ TIER-B BUILDER [{gear_lbl}, Conf={conf_sc}/100]", 0.10
        return base_c, f"💎 TIER-A SNIPER [{gear_lbl}, Conf={conf_sc}/100]", 0.0

    def get_daily_quota_status(self) -> dict:
        now_local = datetime.now()
        day_str = now_local.strftime("%Y-%m-%d")
        q_map = self.memory.setdefault("daily_quota_tracker", {})
        q_day = q_map.setdefault(
            day_str,
            {"target_bets": 40, "completed_bets": 0, "wins": 0, "losses": 0, "net_pnl_dlr": 0.0},
        )
        q_day["target_bets"] = 40
        # Expected pace across 24h day (~1.67 bets/hour)
        frac_day = max(0.05, (now_local.hour + now_local.minute / 60.0) / 24.0)
        expected_by_now = round(40.0 * frac_day, 1)
        done = int(q_day.get("completed_bets", 0))
        behind_pace = (expected_by_now - done) >= 1.5 and done < 40
        quota_met = done >= 40
        return {
            "target_bets": 40,
            "completed_bets": done,
            "expected_by_now": expected_by_now,
            "behind_pace": behind_pace,
            "quota_met": quota_met,
            "wins": int(q_day.get("wins", 0)),
            "losses": int(q_day.get("losses", 0)),
        }

    async def get_sleeper_fantasy_intel(
        self, client: KalshiClient, sport: str, player_target_name: str
    ) -> tuple[float, str, bool]:
        """
        Queries Sleeper Fantasy API (`https://api.sleeper.app/v1/players/{sport}` + 24h trending waiver adds/drops).
        Returns (req_pct_adjustment, tag_note, hard_veto).
        - Vetoes players flagged `Out` / `Doubtful` / `IR`.
        - Penalizes `Questionable` / `GTD` decoy risk (+8% required banked stat) and `depth_chart_order >= 3` backup mirages (+12% required banked stat).
        - Boosts Fantasy Alpha #1 starters (`depth_chart_order == 1` & `search_rank <= 50` or top trending waiver breakout: -3.5% required banked stat).
        """
        now_ts = time.time()
        cache_file = Path(f"data/sleeper_{sport}_cache.json")
        c_ts, name_idx, trend_adds, trend_drops = self.sleeper_cache.get(sport, (0.0, {}, set(), set()))
        if (now_ts - c_ts) > 10800.0 or not name_idx:
            try:
                hc = await client.get_client()
                raw_players = None
                if cache_file.exists() and (now_ts - cache_file.stat().st_mtime) < 43200.0:
                    raw_players = json.loads(cache_file.read_text())
                else:
                    r_pl = await hc.get(f"https://api.sleeper.app/v1/players/{sport}", timeout=8.0)
                    if r_pl.status_code == 200:
                        raw_players = r_pl.json()
                        try:
                            cache_file.write_text(json.dumps(raw_players))
                        except Exception:
                            pass
                r_add = await hc.get(f"https://api.sleeper.app/v1/players/{sport}/trending/add?lookback_hours=24&limit=40", timeout=3.0)
                r_drp = await hc.get(f"https://api.sleeper.app/v1/players/{sport}/trending/drop?lookback_hours=24&limit=40", timeout=3.0)
                trend_adds = {str(x.get("player_id")) for x in (r_add.json() if r_add.status_code == 200 else [])}
                trend_drops = {str(x.get("player_id")) for x in (r_drp.json() if r_drp.status_code == 200 else [])}
                if isinstance(raw_players, dict):
                    import unicodedata
                    def _norm(s: str) -> str:
                        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").replace(".", "").strip().lower()
                    name_idx = {}
                    for pid, pdata in raw_players.items():
                        if not isinstance(pdata, dict):
                            continue
                        fn = _norm(pdata.get("full_name") or f"{pdata.get('first_name','')} {pdata.get('last_name','')}")
                        if fn and pdata.get("active"):
                            name_idx[fn] = {
                                "pid": str(pid),
                                "inj": pdata.get("injury_status") or "",
                                "depth": pdata.get("depth_chart_order"),
                                "rank": pdata.get("search_rank") or 9999,
                            }
                    self.sleeper_cache[sport] = (now_ts, name_idx, trend_adds, trend_drops)
            except Exception:
                return 0.0, "", False

        # Explicit Perennial All-Star / MVP Superstar Floor vs. 4-Tier Sleeper Rank Classification:
        all_star_superstars = {
            "nikola jokic", "giannis antetokounmpo", "luka doncic", "shai gilgeous-alexander",
            "jayson tatum", "stephen curry", "anthony davis", "lebron james", "kevin durant",
            "anthony edwards", "donovan mitchell", "victor wembanyama", "jalen brunson",
            "devin booker", "tyrese haliburton", "damian lillard", "jaylen brown", "de'aaron fox",
            "domantas sabonis", "trae young", "paolo banchero", "cade cunningham", "karl-anthony towns",
            "josh allen", "lamar jackson", "patrick mahomes", "joe burrow", "jalen hurts",
            "saquon barkley", "derrick henry", "bijan robinson", "jahmyr gibbs", "breece hall",
            "ja'marr chase", "ceedee lamb", "justin jefferson", "amon-ra st brown", "nico collins",
            "aj brown", "puka nacua", "malik nabers", "brock bowers", "trey mcbride",
        }
        is_explicit_all_star = player_target_name in all_star_superstars
        p_info = name_idx.get(player_target_name)
        if not p_info and not is_explicit_all_star:
            return 0.0, "", False
        inj = str((p_info or {}).get("inj") or "").upper()
        depth = (p_info or {}).get("depth", 1 if is_explicit_all_star else None)
        rank = int((p_info or {}).get("rank") or (15 if is_explicit_all_star else 9999))
        pid = (p_info or {}).get("pid", "")

        if inj in ("OUT", "DOUBTFUL", "IR", "SUSPENDED"):
            return 99.0, f"VETO [Sleeper Fantasy Injury]: {player_target_name} flagged '{inj}'", True
        adj = 0.0
        tags = []
        if inj in ("QUESTIONABLE", "GTD", "PROBABLE"):
            adj += 8.0
            tags.append(f"FANTASY-INJURY-DECOY({inj}:+8%)")
        # 4-Tier Player Classification: All-Star (Tier 1) -> Starter (Tier 2) -> Role Player (Tier 3) -> Scrub/Backup (Tier 4)
        if is_explicit_all_star or (depth == 1 and rank <= 35 and not inj):
            adj -= 4.5
            tags.append(f"+TIER1-ALL-STAR(Rank#{rank})")
        elif depth == 1 and rank <= 85 and not inj:
            adj -= 2.5
            tags.append(f"+TIER2-QUALITY-STARTER(Rank#{rank})")
        elif (depth is not None and int(depth) >= 3) or rank >= 220:
            adj += 12.0
            tags.append(f"TIER4-BENCH-SCRUB-MIRAGE(Depth#{depth},Rank#{rank}:+12%)")
        else:
            adj += 3.0
            tags.append(f"TIER3-ROLE-PLAYER(Rank#{rank}:+3%)")
        if pid in trend_drops:
            adj += 6.0
            tags.append("FANTASY-WAIVER-DROP(+6%)")
        elif pid in trend_adds and (depth == 1 or depth == 2):
            adj -= 2.5
            tags.append("+FANTASY-WAIVER-BREAKOUT")
        return adj, (" | " + " ".join(tags) if tags else ""), False

    async def verify_pinnacle_sharp_odds(
        self, client: KalshiClient, series: str, ticker: str, title: str
    ) -> str:
        """
        Queries The Odds API v4 (Pinnacle, FanDuel, DraftKings, BetMGM) with a 12-minute quota-saving cache
        ONLY after a play has already passed all 4 primary gates. Applies Multiplicative De-Vigging (Law 4).
        """
        if not self.odds_api_key:
            return ""
        sport_key = None
        if series.startswith("KXNBA"):
            sport_key = "basketball_nba"
        elif series.startswith("KXNFL"):
            sport_key = "americanfootball_nfl"
        elif series.startswith("KXNCAAF"):
            sport_key = "americanfootball_ncaaf"
        elif series.startswith("KXNCAAMB"):
            sport_key = "basketball_ncaab"
        elif series.startswith("KXNHL"):
            sport_key = "icehockey_nhl"
        elif series.startswith("KXEPL"):
            sport_key = "soccer_epl"
        elif series.startswith("KXUCL"):
            sport_key = "soccer_uefa_champs_league"
        elif series.startswith("KXLALIGA"):
            sport_key = "soccer_spain_la_liga"
        if not sport_key:
            return ""

        now_ts = time.time()
        cached_ts, games = self.odds_api_cache.get(sport_key, (0.0, []))
        if (now_ts - cached_ts) > 120.0 or not games:
            try:
                hc = await client.get_client()
                r = await hc.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                    params={
                        "apiKey": self.odds_api_key,
                        "regions": "us,eu",
                        "markets": "h2h",
                        "bookmakers": "pinnacle,fanduel,draftkings,betmgm",
                    },
                    timeout=3.5,
                )
                if r.status_code == 200 and isinstance(r.json(), list):
                    games = r.json()
                    self.odds_api_cache[sport_key] = (now_ts, games)
            except Exception:
                return ""

        target_clean = title.replace(" wins", "").strip().lower()
        last_w = target_clean.split()[-1] if target_clean else ""
        if not last_w or len(last_w) < 3:
            return ""

        for g in games:
            h_tm = (g.get("home_team") or "").lower()
            a_tm = (g.get("away_team") or "").lower()
            if last_w in h_tm or last_w in a_tm:
                bks = g.get("bookmakers", [])
                # Prioritize Pinnacle first, then FanDuel / DraftKings / BetMGM
                bks_sorted = sorted(
                    bks,
                    key=lambda b: {"pinnacle": 0, "fanduel": 1, "draftkings": 2, "betmgm": 3}.get(b.get("key", ""), 9),
                )
                for bk in bks_sorted:
                    bk_key = bk.get("key", "").upper()
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") == "h2h":
                            outcomes = mkt.get("outcomes", [])
                            if len(outcomes) >= 2:
                                raw_probs = {}
                                for o in outcomes:
                                    nm = (o.get("name") or "").lower()
                                    pr = float(o.get("price") or 0.0)
                                    if pr > 1.0:
                                        raw_probs[nm] = 1.0 / pr
                                tot_raw = sum(raw_probs.values())
                                if tot_raw > 0:
                                    for nm, rp in raw_probs.items():
                                        if last_w in nm:
                                            devig_pct = (rp / tot_raw) * 100.0
                                            tag_boost = " | PINNACLE-SHARP" if devig_pct >= 72.0 else ""
                                            return f" | {bk_key}_DeVig={devig_pct:.1f}%{tag_boost}"
        return ""

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
            # True external deposit: both cash and total equity jump by nearly the same amount (>= $45) without portfolio liquidation
            if (eq_dlr - prev_eq) >= 45.0 and abs((cash_dlr - prev_cash) - (eq_dlr - prev_eq)) <= 5.0:
                deposit_delta = round(eq_dlr - prev_eq, 2)

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
                now_min_utc = datetime.now(timezone.utc).minute
                is_final_12m_window = (46 <= now_min_utc <= 57)
                base_cush = (0.45 if is_high_beta_alt else 0.25) if is_final_12m_window else (0.75 if is_high_beta_alt else 0.45)
                min_required_cushion = base_cush
                trend_str = " | SCOUT-TIER(CRYPTO-FINAL-12M)" if is_final_12m_window else ""
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
                        atr_mult = 1.10 if is_final_12m_window else 1.50
                        min_required_cushion = max(base_cush, round(atr_15m_pct * atr_mult, 2))

                        trend_15m = ((newest_close - oldest_open) / oldest_open) * 100.0
                        effective_trend = -trend_15m if is_below else trend_15m
                        if effective_trend < -0.02:
                            return False, f"VETO [Crypto 15m Trend]: {pair} drifting {trend_15m:+.2f}% over last 15m toward strike (strict zero-drift rule)"
                        trend_str += f" | 15mTrend={trend_15m:+.2f}% | 15mVol={atr_15m_pct:.2f}%"
                except Exception:
                    pass

                # Psychological Round-Number Stop-Hunt / Liquidity-Sweep Shield:
                # When a strike sits right at a major psychological round number ($1,000 BTC or $100 ETH),
                # leveraged perpetual stop-hunts frequently wick through the level — require +25% wider volatility buffer!
                if ("BTC" in series and strike > 0 and (strike % 1000.0 == 0 or abs(strike - round(strike, -3)) / strike <= 0.0015)) or (
                    "ETH" in series and strike > 0 and (strike % 100.0 == 0 or abs(strike - round(strike, -2)) / strike <= 0.0015)
                ):
                    min_required_cushion = round(min_required_cushion * 1.25, 2)

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

        # Helper: Team Quality & Standings Tier Engine (`GOOD` vs `MEDIUM` vs `BAD` + Home/Road Split + Top-25 Rank)
        def _team_standings_edge(my_comp: dict, opp_comp: dict) -> tuple[float, str]:
            def _parse_comp_strength(c_obj: dict) -> tuple[float, str, str]:
                recs = c_obj.get("records") or []
                ov_sum = "0-0"
                venue_sum = ""
                is_home = (c_obj.get("homeAway") or "").lower() == "home"
                for r_item in recs:
                    r_type = (r_item.get("type") or r_item.get("name") or "").lower()
                    s_val = str(r_item.get("summary") or "")
                    if r_type in ("total", "overall") and "-" in s_val:
                        ov_sum = s_val
                    elif is_home and r_type == "home" and "-" in s_val:
                        venue_sum = s_val
                    elif not is_home and r_type in ("road", "away") and "-" in s_val:
                        venue_sum = s_val

                def _rec_to_wp(rec_s: str, prior_g: float = 4.0) -> float:
                    try:
                        nums = [float(x) for x in rec_s.split("-") if x.strip().isdigit()]
                        if len(nums) == 2:
                            w, l = nums[0], nums[1]
                            return (w + 0.5 * prior_g) / max(1.0, w + l + prior_g)
                        if len(nums) >= 3:
                            w, l, ot_d = nums[0], nums[1], nums[2]
                            return (w + 0.5 * ot_d + 0.5 * prior_g) / max(1.0, w + l + ot_d + prior_g)
                    except Exception:
                        pass
                    return 0.50

                ov_wp = _rec_to_wp(ov_sum, prior_g=4.0)
                ven_wp = _rec_to_wp(venue_sum, prior_g=2.0) if venue_sum else ov_wp
                strength = 0.70 * ov_wp + 0.30 * ven_wp
                # Blend with Sport-Aware Franchise Power Tier Priors (so early-season records still recognize Elite Contenders vs. Tanking Teams!)
                tm_ab = (c_obj.get("team", {}).get("abbreviation") or "").upper()
                if series.startswith("KXNBA"):
                    elite_franchises = {"BOS", "OKC", "DEN", "CLE", "NYK", "NY", "MIN", "DAL", "PHX", "GSW", "GS", "HOU", "LAL", "ORL", "IND", "MIL"}
                    bad_tanking_franchises = {"WAS", "WSH", "UTA", "UTAH", "BKN", "CHA", "POR", "DET", "TOR"}
                elif series.startswith("KXNHL"):
                    elite_franchises = {"FLA", "DAL", "EDM", "CAR", "VGK", "WPG", "COL", "NYR", "TOR", "NJD", "NJ", "TBL", "TB", "VAN", "BOS"}
                    bad_tanking_franchises = {"SJS", "SJ", "CHI", "ANA", "CBJ", "MTL", "CGY", "PHI"}
                elif series.startswith("KXNFL"):
                    elite_franchises = {"KC", "DET", "BAL", "BUF", "SF", "PHI", "GB", "HOU", "MIN", "WAS", "WSH", "ATL", "PIT"}
                    bad_tanking_franchises = {"CAR", "NE", "NYG", "TEN", "LV", "CLE", "JAX", "NO", "MIA"}
                else:
                    elite_franchises = {"RMA", "MCI", "FCB", "ARS", "LIV", "BAY", "INT", "PSG", "B04", "ATM", "JUV", "NAP", "DOR"}
                    bad_tanking_franchises = {"SOU", "IPS", "WOL", "LEC", "VLL", "LAS", "MON", "VEN", "CAG", "BOC", "KIL"}
                if tm_ab in elite_franchises:
                    strength = min(0.92, strength + 0.09)
                elif tm_ab in bad_tanking_franchises:
                    strength = max(0.15, strength - 0.09)
                rank_val = int(c_obj.get("curatedRank", {}).get("current", 99) or 99)
                if 1 <= rank_val <= 10:
                    strength = min(0.93, strength + 0.14)
                elif 11 <= rank_val <= 25:
                    strength = min(0.87, strength + 0.08)
                tier_lbl = "ELITE" if strength >= 0.63 else ("GOOD" if strength >= 0.55 else ("BAD" if strength <= 0.43 else "MED"))
                rank_str = f"#{rank_val} " if 1 <= rank_val <= 25 else ""
                return strength, f"{rank_str}{tier_lbl}({ov_sum})", tier_lbl

            my_str, my_lbl, my_tier = _parse_comp_strength(my_comp)
            opp_str, opp_lbl, opp_tier = _parse_comp_strength(opp_comp)
            edge = my_str - opp_str
            tag = f" | STANDINGS[{my_lbl} vs {opp_lbl}, edge={edge:+.2f}]"
            return edge, tag

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
                events = await self._get_espn_events_cached(hc, sp_path, ttl=4.0)
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
                    if req_periods == (1,):
                        return ls[0] if ls else (full_sc if live_period == 1 else 0.0)
                    if req_periods == (2,) and not is_soccer:
                        return ls[1] if len(ls) >= 2 else 0.0
                    if req_periods == (3,):
                        return ls[2] if len(ls) >= 3 else 0.0
                    if req_periods == (4,):
                        return ls[3] if len(ls) >= 4 else 0.0
                    if req_periods == (1, 2):
                        return sum(ls[:2]) if ls else (full_sc if live_period <= 2 else 0.0)
                    if req_periods == (3, 4):
                        return sum(ls[2:4]) if len(ls) >= 3 else 0.0
                    return full_sc

                # 2. Quarter/Half Winner, Spread & Total Live Scoreboard Verification
                suffix = parts[-1].upper() if len(parts) >= 3 else ""
                import re
                m_suf = re.match(r"^([A-Z]{2,4})?(\d+(?:\.\d+)?)$", suffix)

                def _abbr_matches(espn_ab: str, kalshi_ab: str) -> bool:
                    e_u = (espn_ab or "").upper().strip()
                    k_u = (kalshi_ab or "").upper().strip()
                    if not e_u or not k_u:
                        return False
                    if e_u == k_u or e_u.startswith(k_u) or k_u.startswith(e_u):
                        return True
                    alias_map = {
                        "WSH": "WAS", "WAS": "WSH",
                        "UTAH": "UTA", "UTA": "UTAH",
                        "GS": "GSW", "GSW": "GS",
                        "SA": "SAS", "SAS": "SA",
                        "NY": "NYK", "NYK": "NY",
                        "NO": "NOP", "NOP": "NO",
                        "LA": "LAK", "LAK": "LA",
                        "NJ": "NJD", "NJD": "NJ",
                        "TB": "TBL", "TBL": "TB",
                        "SJ": "SJS", "SJS": "SJ",
                    }
                    return alias_map.get(e_u) == k_u

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
                            (c for c in competitors if _abbr_matches(c.get("team", {}).get("abbreviation", ""), tm_prefix)),
                            competitors[0],
                        )
                        cur_pts = _comp_window_pts(target_comp)
                    else:
                        cur_pts = _comp_window_pts(competitors[0]) + _comp_window_pts(competitors[1])
                    prog_pct = (cur_pts / target_val) * 100.0
                    pts_needed = max(0.0, target_val - cur_pts)
                    gm_margin_tot = abs(float(competitors[0].get("score", 0) or 0) - float(competitors[1].get("score", 0) or 0))
                    try:
                        clean_clk = str(clock_disp).replace("'", "").split(":")[0].split("+")[0].strip()
                        cur_clk_m = float(clean_clk) if clean_clk else 0.5
                    except Exception:
                        cur_clk_m = 6.0

                    # Positive-Theta Total Rule (LATE-CLOCK UNDERS ONLY / 100% BAN ON UNBANKED OVERS):
                    # 1) If evaluating a YES ("Over") Total: 100% VETO across ALL sports unless the points/goals are ALREADY 100% scored (pts_needed <= 0)!
                    #    Never hope for future goals/points against a ticking clock!
                    # 2) If evaluating a NO ("Under") Total (`[UNDER-NO]` tag in title):
                    #    Verify Late-Clock + Multi-Score Safety Cushion so time decay (Theta) works 100% FOR us!
                    is_under_no_play = title.startswith("[UNDER-NO]")
                    if not is_under_no_play:
                        if pts_needed > 0.0:
                            return False, (
                                f"VETO [Unbanked Over Total Ban]: {series} Over still needs {pts_needed:.1f} more pts/goals "
                                f"({cur_pts:.0f}/{target_val:.1f} at P{live_period} {clock_disp}) — ONLY Late-Clock UNDERS or 100%-banked totals allowed!"
                            )
                        return True, f"ESPN {period_tag} Total 100% BANKED ({cur_pts:.0f}/{target_val:.1f} already scored | P{live_period} {clock_disp}) | PROP-LOCK"
                    else:
                        # Late-Clock UNDER (`NO` on Over Total) Verification:
                        # Number of discrete goals/points required to exceed `target_val` (e.g. Over 3.5 -> requires 4 goals to break Under 3.5):
                        pts_to_break_under = (target_val + 0.5) - cur_pts
                        if pts_to_break_under <= 0.0:
                            return False, f"VETO [Under Already Broken]: Score {cur_pts:.0f} already exceeded Under {target_val:.1f}!"
                        if is_soccer:
                            if live_period < 2 or cur_clk_m < 65.0:
                                return False, (
                                    f"VETO [Soccer Under Too Early]: Clock at P{live_period} {clock_disp} (<65' min for Late-Clock Under)"
                                )
                            if pts_to_break_under < 2.0:
                                return False, (
                                    f"VETO [Soccer Under Cushion <2 Goals]: {cur_pts:.0f} goals scored vs Under {target_val:.1f} "
                                    f"(only {pts_to_break_under:.0f} goal buffer — requires >=2 goal cushion so 1 fluke goal cannot kill Under!)"
                                )
                            return True, (
                                f"ESPN Soccer Late-Clock UNDER Verified ({cur_pts:.0f} goals vs Under {target_val:.1f} = "
                                f"+{pts_to_break_under:.0f} Goal Cushion at {clock_disp} | LATE-CLOCK)"
                            )
                        elif "NHL" in series:
                            target_nhl_p = max(req_periods) if req_periods else 3
                            max_nhl_clk = 10.0 if req_periods else 12.0
                            if live_period != target_nhl_p or cur_clk_m > max_nhl_clk:
                                return False, (
                                    f"VETO [NHL {period_tag} Under Too Early]: P{live_period} {clock_disp} (requires P{target_nhl_p} <={max_nhl_clk:.0f}:00 left)"
                                )
                            if req_periods is None and gm_margin_tot == 0.0:
                                return False, (
                                    f"VETO [NHL Tied Overtime Under Trap]: Game is TIED at P3 {clock_disp} "
                                    "(Overtime/Shootout guarantees an extra goal and kills Under!)"
                                )
                            min_nhl_under_buf = 3.0 if (req_periods is None and gm_margin_tot <= 1.0) else 2.0
                            if pts_to_break_under < min_nhl_under_buf:
                                return False, (
                                    f"VETO [NHL {period_tag} Under Cushion <{min_nhl_under_buf:.0f} Goals]: {cur_pts:.0f} goals vs Under {target_val:.1f} "
                                    f"at P{live_period} {clock_disp} (margin={gm_margin_tot:.0f} — empty-netter/OT shield!)"
                                )
                            return True, (
                                f"ESPN NHL {period_tag} Late-Clock UNDER Verified ({cur_pts:.0f} goals vs Under {target_val:.1f} = "
                                f"+{pts_to_break_under:.0f} Goal Cushion at P{live_period} {clock_disp} | LATE-CLOCK)"
                            )
                        elif series.startswith(("KXNFL", "KXNCAAF")):
                            target_fb_p = max(req_periods) if req_periods else 4
                            if live_period != target_fb_p or cur_clk_m > 10.0:
                                return False, (
                                    f"VETO [Football {period_tag} Under Too Early]: P{live_period} {clock_disp} (requires P{target_fb_p} <=10:00 left)"
                                )
                            if req_periods is None and gm_margin_tot < 9.0:
                                return False, (
                                    f"VETO [Football 1-Score Overtime/Hurry-Up Under Trap]: Margin is only {gm_margin_tot:.0f} pts in P4 {clock_disp} "
                                    "(requires >=9 pt 2-score lead so game cannot tie/go to OT and leader runs the clock!)"
                                )
                            if pts_to_break_under < 10.0:
                                return False, (
                                    f"VETO [Football {period_tag} Under <2-Score Cushion]: Only {pts_to_break_under:.0f} pts cushion on Under {target_val:.1f} "
                                    f"(requires >=10 pts / 2-possession buffer in P{live_period} {clock_disp}!)"
                                )
                            return True, (
                                f"ESPN Football {period_tag} Late-Clock UNDER Verified ({cur_pts:.0f} pts vs Under {target_val:.1f} = "
                                f"+{pts_to_break_under:.0f} pt Cushion at P{live_period} {clock_disp} | LATE-CLOCK)"
                            )
                        else:
                            # Basketball (NBA / NCAAMB) Late-Clock Under (Supports 1Q/2Q/3Q/1H which have ZERO Overtime risk, plus Full-Game P4!):
                            final_reg_p = max(req_periods) if req_periods else (2 if "NCAAMB" in series else 4)
                            max_bb_clk = 6.0 if req_periods else 7.5
                            if live_period != final_reg_p or cur_clk_m > max_bb_clk or cur_clk_m < 0.8:
                                return False, (
                                    f"VETO [Basketball {period_tag} Under Clock Window]: P{live_period} {clock_disp} "
                                    f"(requires P{final_reg_p} <={max_bb_clk:.1f}m left for Late-Clock Under)"
                                )
                            if req_periods is None and gm_margin_tot < 9.0:
                                return False, (
                                    f"VETO [Basketball Overtime & Foul-Parade Under Trap]: Margin is only {gm_margin_tot:.0f} pts with {clock_disp} left "
                                    "(requires >=9 pt 3-possession lead so game cannot go to 25-pt Overtime or FT foul parade!)"
                                )
                            p_len_m = 20.0 if "NCAAMB" in series else 12.0
                            num_p_in_win = len(req_periods) if req_periods else (2 if "NCAAMB" in series else 4)
                            elapsed_gm_m = max(4.0, ((num_p_in_win - 1) * p_len_m) + max(0.0, p_len_m - cur_clk_m))
                            live_ppm = cur_pts / elapsed_gm_m
                            req_ppm_to_lose = pts_to_break_under / max(0.5, cur_clk_m)
                            min_bb_cush = 11.0 if req_periods else 14.0
                            if pts_to_break_under < min_bb_cush or req_ppm_to_lose < (live_ppm * 1.40):
                                return False, (
                                    f"VETO [Basketball {period_tag} Under Pace Cushion]: Cushion={pts_to_break_under:.0f} pts in {clock_disp} "
                                    f"(req_ppm={req_ppm_to_lose:.1f} vs live_ppm={live_ppm:.1f} — needs >={min_bb_cush:.0f}pt buffer & >=1.40x pace gap!)"
                                )
                            return True, (
                                f"ESPN Basketball {period_tag} Late-Clock UNDER Verified ({cur_pts:.0f} pts vs Under {target_val:.1f} = "
                                f"+{pts_to_break_under:.0f} pt Cushion in P{live_period} {clock_disp}, reqPPM={req_ppm_to_lose:.1f} vs live={live_ppm:.1f} | LATE-CLOCK)"
                            )

                if "BTTS" in series and len(competitors) >= 2:
                    sc0 = _comp_window_pts(competitors[0])
                    sc1 = _comp_window_pts(competitors[1])
                    if sc0 >= 1 and sc1 >= 1:
                        return True, f"ESPN BTTS LOCKED ({sc0:.0f}-{sc1:.0f}, both teams already scored!)"
                    return False, f"VETO [ESPN BTTS]: Score is {sc0:.0f}-{sc1:.0f} (waiting for both teams or stronger setup)"

                if "SPREAD" in series and len(competitors) >= 2 and m_suf and m_suf.group(1):
                    tm_prefix, num_str = m_suf.group(1), m_suf.group(2)
                    strike_val = max(0.5, float(num_str) - 0.5)
                    c_idx = next(
                        (i for i, c in enumerate(competitors) if _abbr_matches(c.get("team", {}).get("abbreviation", ""), tm_prefix)),
                        None,
                    )
                    if c_idx is not None:
                        my_pts = _comp_window_pts(competitors[c_idx])
                        opp_pts = _comp_window_pts(competitors[1 - c_idx])
                        margin = my_pts - opp_pts
                        # Overtime Spread Distortion Gate: In a 5-min OT (P5+), teams start tied and rarely cover >=5.5 spreads!
                        if req_periods is None and live_period >= 5 and strike_val >= 5.5 and margin < (strike_val + 4.5):
                            return False, f"VETO [Overtime Spread Trap]: -{strike_val:.1f} spread in OT (P{live_period}) is nearly impossible to cover!"
                        # Time-in-Play Velocity & Multi-Score Cushion Gate on Full-Game Spreads:
                        # - NEVER buy Full-Game Spreads in P1/P2 when 1 goal or a 6-pt run breaks the spread!
                        # - In NHL (3 periods), require Period 3 (or +4 goals above spread in P2) so 1 goal against you CANNOT put a -1.5 puck line in danger!
                        # - In NBA/NFL/College, require 2nd Half (`live_period >= 3`) unless up +18.0 points above the spread!
                        scout_spread_tag = ""
                        if req_periods is None and not is_soccer:
                            if "NHL" in series and live_period <= 2 and margin < (strike_val + 2.5):
                                return False, f"VETO [NHL Early Puck-Line Ban]: Full-game -{strike_val:.1f} puck line in P{live_period} with +{margin:.0f}G lead is vulnerable to 1 goal (requires P3 or +4G lead!)"
                            elif "NHL" not in series and live_period <= 2 and margin < (strike_val + 18.0):
                                return False, (
                                    f"VETO [1H Full-Game Spread Ban]: Full-game -{strike_val:.1f} spread in P{live_period} {clock_disp} is vulnerable to 2nd-half runs "
                                    f"(use 1Q/2Q/1H or wait for Q3/Q4!)"
                                )
                        # Require team to ALREADY be covering the spread by a Multi-Score Safety Cushion:
                        # - NHL/Soccer: +1.5 goals above spread (2-goal cushion so 1 goal against you CANNOT break the spread!)
                        # - Fast Quarters/Halves (req_periods is not None): +5.5 points above spread (2 full possessions!)
                        # - Full-Game NBA/Football Spreads in Q3: +8.5 points above spread
                        # - Full-Game NBA/Football Spreads in Q4: +6.5 points above spread
                        if "NHL" in series or is_soccer:
                            req_spread_buf = 1.5
                        elif req_periods is not None:
                            req_spread_buf = 5.5
                        else:
                            req_spread_buf = 8.5 if live_period <= 3 else 6.5
                        # Opponent Red-Zone Threat Shield on Football Spreads:
                        # If the trailing opponent currently has the ball inside the Red Zone (about to score 7 pts!), require +7.0 extra cushion!
                        sit_sp = comp.get("situation") or {}
                        if series.startswith(("KXNFL", "KXNCAAF")) and sit_sp.get("isRedZone") and str(sit_sp.get("possession") or "") == str(competitors[1 - c_idx].get("id") or ""):
                            req_spread_buf += 7.0
                        # Standings Quality Tier Adjustment (`GOOD` vs `MEDIUM` vs `BAD`):
                        # If a BAD/lower-tier team is leading a GOOD/higher-tier team (`st_edge <= -0.10`), require extra cushion ("Underdog Mirage Lead Shield")!
                        st_edge, st_tag = _team_standings_edge(competitors[c_idx], competitors[1 - c_idx])
                        if st_edge <= -0.10:
                            req_spread_buf += 1.0 if ("NHL" in series or is_soccer) else 3.5
                        if margin <= 0 or margin < (strike_val + req_spread_buf):
                            return False, (
                                f"VETO [ESPN {period_tag} Spread Cushion]: {tm_prefix} margin is {margin:+.0f} ({my_pts:.0f}-{opp_pts:.0f}) "
                                f"vs -{strike_val:.1f} spread (requires +{strike_val + req_spread_buf:.1f} min lead{st_tag}) in P{live_period} {clock_disp}"
                            )
                        lock_tag = (" | PROP-LOCK" if margin >= (strike_val + req_spread_buf + 1.0) or st_edge >= 0.12 else "") + st_tag + scout_spread_tag
                        return True, f"ESPN {period_tag} Spread Verified ({tm_prefix} {my_pts:.0f}-{opp_pts:.0f}, margin {margin:+.0f} vs -{strike_val:.1f}{lock_tag} | P{live_period} {clock_disp})"

                if req_periods is not None and "TOTAL" not in series and "SPREAD" not in series and len(competitors) >= 2:
                    c_idx = next(
                        (i for i, c in enumerate(competitors) if _abbr_matches(c.get("team", {}).get("abbreviation", ""), suffix)),
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
                        try:
                            p_clk_m = float(str(clock_disp).replace("'", "").split(":")[0].split("+")[0] or 0)
                        except Exception:
                            p_clk_m = 6.0
                        st_edge_w, st_tag_w = _team_standings_edge(competitors[c_idx], competitors[1 - c_idx])
                        # Clock-Aware Multi-Possession Shield for Quarter, Period & Half Winners across ALL leagues:
                        # STRICT 2+ GOAL CUSHION on Soccer Halves & NHL Periods (1 goal ties a 3-way half/period and wipes out the play!)
                        if is_soccer and margin < 2:
                            return False, f"VETO [Soccer 1H 1-Goal Lead Ban]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} at {clock_disp} (strict 2+ goal cushion required{st_tag_w}!)"
                        elif "NHL" in series and margin < 2:
                            return False, f"VETO [NHL {period_tag} 1-Goal Lead Ban]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} with {clock_disp} left (strict 2+ goal cushion required{st_tag_w}!)"
                        elif series.startswith(("KXNFL", "KXNCAAF")):
                            req_fb_p_cush = 10 if st_edge_w <= -0.10 else 7
                            if margin < req_fb_p_cush:
                                return False, f"VETO [Football {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} (+{margin:.0f} < +{req_fb_p_cush} pt min{st_tag_w})"
                        elif series.startswith(("KXNBA", "KXNCAAMB", "KXNCAAWB")):
                            # Never let a routine 4-5 point NBA run wipe out a Quarter/Half bet!
                            # - For 1H (in P1) or 2H (in P3): require >= +16 pt blowout!
                            # - With > 5:00 left in the Quarter/Half: require >= +13 pts (5 possessions!)
                            # - With 2:30 to 5:00 left: require >= +9 pts (3+ possessions!)
                            # - Inside final <= 2:30: require >= +6 pts
                            if ("1H" in series and live_period == 1 and not series.startswith("KXNCAAMB")) or ("2H" in series and live_period == 3):
                                base_bb_cush = 16
                            elif p_clk_m > 5.0:
                                base_bb_cush = 13
                            elif p_clk_m > 2.5:
                                base_bb_cush = 9
                            else:
                                base_bb_cush = 6
                            req_bb_p_cush = base_bb_cush + (3 if st_edge_w <= -0.10 else 0)
                            if margin < req_bb_p_cush:
                                return False, f"VETO [Basketball {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} (+{margin:.0f} < +{req_bb_p_cush} pt min in P{live_period} {clock_disp}{st_tag_w})"
                        lock_tag = (" | PROP-LOCK" if margin >= 8 or (is_soccer and margin >= 2) or st_edge_w >= 0.12 else "") + st_tag_w
                        return True, f"ESPN {period_tag} Leader Verified ({suffix} leading {my_pts:.0f}-{opp_pts:.0f}{lock_tag} | P{live_period} {clock_disp})"

                # 3. Deep Player Prop Box Score Check via ESPN Summary API (/summary?event={id}) — 100% FAIL-CLOSED!
                nba_prop_Set = {
                    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA",
                    "KXNBAPR", "KXNBAPA", "KXNBADD2", "KXNBATD3", "KXNBABLK", "KXNBASTL",
                }
                nfl_prop_Set = {"KXNFLPASSYDS", "KXNFLPASSTD", "KXNFLRUSHYDS", "KXNFLRECYDS", "KXNFLREC"}
                nhl_prop_Set = {"KXNHLPTS", "KXNHLGOAL", "KXNHLAST", "KXNHLSOG"}
                if (series in nba_prop_Set or series in nfl_prop_Set or series in nhl_prop_Set) and ":" in title and len(parts) >= 4:
                    import unicodedata
                    def _strip_accents(s: str) -> str:
                        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").replace(".", "").strip().lower()

                    player_target_name = _strip_accents(title.split(":")[0])
                    name_tokens = player_target_name.split()
                    player_first = name_tokens[0] if name_tokens else ""
                    player_last = name_tokens[-1] if name_tokens else ""
                    try:
                        target_val = 1.0 if series in ("KXNBADD2", "KXNBATD3", "KXNHLGOAL") else float(parts[-1])
                    except Exception:
                        target_val = 0.0

                    if target_val > 0 and player_last:
                        sport_base = sp_path.split("/scoreboard")[0]
                        ev_id = matched_ev.get("id", "")
                        box_json = await self._get_espn_summary_cached(hc, sport_base, ev_id, ttl=4.0)
                        bs_players = box_json.get("boxscore", {}).get("players", [])
                        found_stat = None
                        fouls_val = 0
                        mins_played = "0"
                        player_team_abbr = ""
                        ath_ejected = False
                        is_nfl_qb = False
                        fga_val = 0.0
                        fta_val = 0.0
                        tpt_made_val = 0.0
                        pts_scored_val = 0.0
                        for team_blk in bs_players:
                            tm_blk_abbr = (team_blk.get("team", {}).get("abbreviation") or "").upper()
                            for sg in team_blk.get("statistics", []):
                                sg_name = (sg.get("name") or "").lower()
                                labels = [lbl.upper() for lbl in sg.get("labels", [])]
                                for ath in sg.get("athletes", []):
                                    a_name = _strip_accents(ath.get("athlete", {}).get("displayName") or "")
                                    a_tokens = a_name.split()
                                    a_first = a_tokens[0] if a_tokens else ""
                                    a_last = a_tokens[-1] if a_tokens else ""
                                    # Exact First-Initial + Last-Name Match (Prevents Jalen Williams vs Jaylin Williams collisions + handles Jokić/Dončić!)
                                    name_matched = (
                                        (player_target_name == a_name)
                                        or (player_last == a_last and (not player_first or not a_first or player_first[0] == a_first[0]))
                                    )
                                    if name_matched:
                                        if ath.get("ejected") or ath.get("didNotPlay"):
                                            ath_ejected = True
                                        pos_abbr = (ath.get("athlete", {}).get("position", {}).get("abbreviation") or "").upper()
                                        if pos_abbr == "QB" or sg_name == "passing":
                                            is_nfl_qb = True
                                        st_arr = ath.get("stats", [])
                                        if not st_arr:
                                            continue
                                        player_team_abbr = tm_blk_abbr or player_team_abbr
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
                                            try:
                                                fg_parts = str(s_map.get("FG", "0-0")).split("-")
                                                fga_val = float(fg_parts[1] if len(fg_parts) > 1 else 0)
                                                ft_parts = str(s_map.get("FT", "0-0")).split("-")
                                                fta_val = float(ft_parts[1] if len(ft_parts) > 1 else 0)
                                            except Exception:
                                                pass
                                            tpt_made_val = tpt
                                            pts_scored_val = pts
                                            if series == "KXNBAPTS": found_stat = pts
                                            elif series == "KXNBAREB": found_stat = reb
                                            elif series == "KXNBAAST": found_stat = ast
                                            elif series == "KXNBA3PT": found_stat = tpt
                                            elif series == "KXNBAPRA": found_stat = pts + reb + ast
                                            elif series == "KXNBAPR": found_stat = pts + reb
                                            elif series == "KXNBAPA": found_stat = pts + ast
                                            elif series == "KXNBABLK": found_stat = blk
                                            elif series == "KXNBASTL": found_stat = stl
                                            elif series == "KXNBADD2":
                                                cats = sorted([pts, reb, ast, blk, stl], reverse=True)
                                                found_stat = (min(10.0, cats[0]) + min(10.0, cats[1])) / 20.0
                                        elif series in nfl_prop_Set and len(st_arr) == len(labels):
                                            s_map = dict(zip(labels, st_arr))
                                            if series == "KXNFLPASSYDS" and sg_name == "passing":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLPASSTD" and sg_name == "passing":
                                                found_stat = float(s_map.get("TD", "0") or 0)
                                            elif series == "KXNFLRUSHYDS" and sg_name == "rushing":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLRECYDS" and sg_name == "receiving":
                                                found_stat = float(s_map.get("YDS", "0") or 0)
                                            elif series == "KXNFLREC" and sg_name == "receiving":
                                                found_stat = float(s_map.get("REC", "0") or 0)
                                        elif series in nhl_prop_Set and len(st_arr) == len(labels):
                                            s_map = dict(zip(labels, st_arr))
                                            g_val = float(s_map.get("G", "0") or 0)
                                            a_val = float(s_map.get("A", "0") or 0)
                                            sog_val = float(s_map.get("S", s_map.get("SOG", "0")) or 0)
                                            if series == "KXNHLPTS": found_stat = max(found_stat or 0.0, g_val + a_val)
                                            elif series == "KXNHLGOAL": found_stat = max(found_stat or 0.0, g_val)
                                            elif series == "KXNHLAST": found_stat = max(found_stat or 0.0, a_val)
                                            elif series == "KXNHLSOG": found_stat = max(found_stat or 0.0, sog_val)
                        if found_stat is not None:
                            prog_pct = (found_stat / target_val) * 100.0
                            if ath_ejected and prog_pct < 100.0:
                                return False, f"VETO [ESPN Ejection/DNP Shield]: {title} is flagged EJECTED/DNP in boxscore!"
                            # Period-Aware NBA Starter Foul-Trouble Bench Shield:
                            # 3 fouls in Q1 or 4 fouls in Q2 forces coaches to bench starters for the rest of the half!
                            max_allowed_fouls = {1: 3, 2: 4}.get(live_period, 5)
                            if fouls_val >= max_allowed_fouls and prog_pct < 95.0:
                                return False, (
                                    f"VETO [ESPN Foul-Trouble Bench Shield]: {title} has {fouls_val} fouls in P{live_period} "
                                    f"(>={max_allowed_fouls} max — starter bench risk!)"
                                )
                            try:
                                clk_m = float(str(clock_disp).split(":")[0]) if ":" in str(clock_disp) else 5.0
                            except Exception:
                                clk_m = 5.0
                            try:
                                mins_float = float(str(mins_played).split(":")[0] or 0)
                            except Exception:
                                mins_float = 15.0

                            # Unnatural Scenario: In-Game Injury / Locker-Room "Ghost Minutes" Stall Shield:
                            # If a player racked up early stats in Q1, tweaked an ankle/hamstring, and went to the locker room,
                            # his minutes played (`mins_float`) will lag far behind the elapsed game clock!
                            if series in nba_prop_Set and prog_pct < 92.0:
                                if (live_period == 2 and clk_m < 5.0 and mins_float <= 8.0) or (
                                    live_period == 3 and clk_m < 5.0 and mins_float <= 15.0
                                ) or (live_period >= 4 and mins_float <= 20.0):
                                    return False, (
                                        f"VETO [Injury / Locker-Room Ghost-Minutes Stall]: {title} has only played {mins_played} MIN "
                                        f"by P{live_period} {clock_disp} (injury/locker-room/bench restriction risk!)"
                                    )

                            # Compute Signed Team Score Differential & Standings Tier Edge for this specific player's team:
                            player_team_margin = None
                            player_st_edge = 0.0
                            player_st_tag = ""
                            gm_margin = 0.0
                            total_gm_pts = 0.0
                            if len(competitors) >= 2:
                                sc0 = float(competitors[0].get("score", 0) or 0)
                                sc1 = float(competitors[1].get("score", 0) or 0)
                                gm_margin = abs(sc0 - sc1)
                                total_gm_pts = sc0 + sc1
                                tm0_abbr = (competitors[0].get("team", {}).get("abbreviation") or "").upper()
                                tm1_abbr = (competitors[1].get("team", {}).get("abbreviation") or "").upper()
                                if player_team_abbr == tm0_abbr:
                                    player_team_margin = sc0 - sc1
                                    player_st_edge, player_st_tag = _team_standings_edge(competitors[0], competitors[1])
                                elif player_team_abbr == tm1_abbr:
                                    player_team_margin = sc1 - sc0
                                    player_st_edge, player_st_tag = _team_standings_edge(competitors[1], competitors[0])

                            # Period-Aware + Minute-by-Minute Clock-Ahead Hot-Hand Player Prop Pace Curve:
                            p_len_prop = 15.0 if series in nfl_prop_Set else (20.0 if series in nhl_prop_Set else 12.0)
                            reg_tot_prop = 60.0 if (series in nfl_prop_Set or series in nhl_prop_Set) else 48.0
                            max_p_prop = 3 if series in nhl_prop_Set else 4
                            el_prop_m = ((min(max_p_prop, live_period) - 1) * p_len_prop) + max(0.0, p_len_prop - clk_m)
                            el_prop_pct = min(95.0, (el_prop_m / reg_tot_prop) * 100.0)
                            # Anti-Stall Prop Rule: Never buy 1st-Half props unless >=90% banked (avoids Halftime cash lock & injury risk);
                            # require >=86% banked in Q3 and >=92% banked in Q4 (1 basket away)!
                            req_prop_pct = max({1: 92.0, 2: 90.0, 3: 86.0}.get(live_period, 92.0), el_prop_pct + 12.0)
                            script_prop_tag = player_st_tag

                            # 0A. Live Sleeper Fantasy Football & Basketball Player Intelligence (`injury_status`, `depth_chart_order`, `search_rank`, 24h waiver trends):
                            if series in nfl_prop_Set or series in nba_prop_Set:
                                slp_sport = "nfl" if series in nfl_prop_Set else "nba"
                                slp_adj, slp_tag, slp_veto = await self.get_sleeper_fantasy_intel(client, slp_sport, player_target_name)
                                if slp_veto:
                                    return False, slp_tag
                                req_prop_pct = max(40.0, req_prop_pct + slp_adj)
                                script_prop_tag += slp_tag

                            # 0. Standings Quality Mismatch on Player Props:
                            # If a player on a BAD team faces a GOOD team (`player_st_edge <= -0.14`) in the 2nd Half, require +4.0% higher banked progress!
                            if live_period >= 3 and player_st_edge <= -0.14 and prog_pct < 90.0:
                                req_prop_pct += 4.0

                            # 1. NBA Early-Q2 / Early-Q4 Mandatory Starter Bench-Rotation Stall Shield:
                            # Coaches sub out primary starters from 12:00 down to ~7:30 of Q2 and Q4 (4.5 mins of dead bench clock!).
                            if series in nba_prop_Set and live_period in (2, 4) and 7.5 <= clk_m <= 11.8 and prog_pct < 92.0:
                                req_prop_pct += 8.0

                            # 1B. NBA "Low-Volume 3PT Fluke Heater" vs. "High-Usage Free-Throw Merchant Floor":
                            if series == "KXNBAPTS" and live_period <= 3:
                                if pts_scored_val >= 9.0 and (tpt_made_val * 3.0) >= (pts_scored_val * 0.80) and fga_val <= 6.0 and fta_val == 0.0:
                                    req_prop_pct += 8.0
                                elif fta_val >= 6.0 or fga_val >= 12.0:
                                    req_prop_pct = max(40.0, req_prop_pct - 3.0)
                                    script_prop_tag += f" | +FT-VOLUME-FLOOR(FGA={fga_val:.0f},FTA={fta_val:.0f})"

                            # 1C. NBA Score-Script Engine ("Trailing Star Chase-Mode Urgency" vs. "Leading 24s Shot-Clock Bleed"):
                            if series in nba_prop_Set and player_team_margin is not None and live_period >= 2:
                                # TRAILING URGENCY BOOST: When an NBA starter's team is down 4 to 14 pts, coaches skip bench rest and stars push pace!
                                if -14.0 <= player_team_margin <= -4.0 and "TIER4" not in script_prop_tag:
                                    req_prop_pct = max(40.0, req_prop_pct - 3.5)
                                    script_prop_tag += f" | +NBA-TRAILING-URGENCY-BOOST({player_team_abbr} {player_team_margin:+.0f})"
                                # LEADING STALL VETO: When leading by >=10 pts in final 6:00 of Q4, point guard walks ball up & bleeds 24s shot clock!
                                elif series in ("KXNBAAST", "KXNBAPTS", "KXNBAPRA", "KXNBAPR", "KXNBAPA") and live_period >= 4 and clk_m < 6.0 and player_team_margin >= 10.0 and prog_pct < 90.0:
                                    return False, (
                                        f"VETO [NBA 4Q 24s Shot-Clock Bleed]: {player_team_abbr} up +{player_team_margin:.0f} in P4 {clock_disp} "
                                        f"— leading team milks 24s shot clock (stalls stats at {found_stat:.0f}/{target_val:.0f})!"
                                    )
                                # LATE-Q3 "NO-4TH-QUARTER" STAR REST SHIELD: When leading by >=16 pts with <4:00 left in Q3, stars often sit the entire 4th quarter!
                                elif live_period == 3 and clk_m < 4.0 and player_team_margin >= 16.0 and prog_pct < 92.0:
                                    return False, (
                                        f"VETO [NBA No-4Q Blowout Rest Risk]: {player_team_abbr} up +{player_team_margin:.0f} late in Q3 ({clock_disp}) "
                                        f"— starters often sit the entire 4th quarter!"
                                    )

                            # 1D. NBA Rebound Scarcity ("Hyper-Shooting / No Missed Shots" Drought) on `KXNBAREB`:
                            # When both teams shoot 58%+ FG% (e.g. >135 pts by Halftime or >195 pts by Q3), available rebounds plummet by 30%!
                            if series == "KXNBAREB" and ((live_period == 2 and total_gm_pts >= 134.0) or (live_period == 3 and total_gm_pts >= 195.0)):
                                req_prop_pct += 6.0

                            # 2. NFL Signed Game-Script Engine + QB Kneel-Down Negative Rushing Yards Shield:
                            if player_team_margin is not None and live_period >= 2:
                                if series in ("KXNFLPASSYDS", "KXNFLPASSTD", "KXNFLRECYDS", "KXNFLREC"):
                                    # STALL VETO: Player's OWN team is leading by >=10 pts -> Coach runs the ball & milks the 40s play clock!
                                    if player_team_margin >= 10.0 and prog_pct < 90.0:
                                        return False, (
                                            f"VETO [NFL Leading Clock-Milk Pass Stall]: {player_team_abbr} is UP +{player_team_margin:.0f} pts in P{live_period} "
                                            f"— coach runs the ball & milks play clock (stalls QB/WR stats at {found_stat:.0f}/{target_val:.0f})!"
                                        )
                                    # ENHANCE BOOST: Player's OWN team is trailing by 6 to 16 pts -> Hurry-up pass-every-down script!
                                    if -16.0 <= player_team_margin <= -6.0:
                                        req_prop_pct = max(40.0, req_prop_pct - 4.0)
                                        script_prop_tag = f" | +HURRY-UP-PASS-BOOST({player_team_abbr} {player_team_margin:+.0f})"
                                elif series == "KXNFLRUSHYDS":
                                    # Unnatural Scenario: NFL Quarterback Victory-Formation Kneel-Down Negative Rushing Yards Trap!
                                    # Each kneel-down officially deducts -1 to -2 rushing yards from the QB at the end of Q4!
                                    if is_nfl_qb and player_team_margin > 0 and found_stat < (target_val + 4.5):
                                        return False, (
                                            f"VETO [NFL QB Kneel-Down Negative Rush Trap]: QB {title} is at {found_stat:.0f}/{target_val:.0f} yds "
                                            f"while leading (+{player_team_margin:.0f}) — 3 kneel-downs deduct -3 to -5 yds in Q4!"
                                        )
                                    # STALL VETO: RB's OWN team is trailing by >=10 pts -> Coach abandons the run!
                                    if player_team_margin <= -10.0 and prog_pct < 90.0:
                                        return False, (
                                            f"VETO [NFL Trailing Abandon-Run Trap]: {player_team_abbr} is DOWN {player_team_margin:+.0f} pts in P{live_period} "
                                            f"— trailing team abandons the run (stalls RB rushing yards at {found_stat:.0f}/{target_val:.0f})!"
                                        )
                                    # ENHANCE BOOST: RB's OWN team is leading by +7 to +20 pts in 2nd Half -> Coach feeds workhorse RB to milk clock!
                                    if live_period >= 3 and 7.0 <= player_team_margin <= 20.0 and not is_nfl_qb:
                                        req_prop_pct = max(50.0, req_prop_pct - 4.0)
                                        script_prop_tag = f" | +CLOCK-KILLER-RB-BOOST({player_team_abbr} +{player_team_margin:.0f})"

                            # 3. NHL Score-Effects Engine ("Turtle Veto" when leading +2+ vs. "Desperation SOG/Points Boost" when trailing 1-2):
                            if series in nhl_prop_Set and player_team_margin is not None and live_period >= 2:
                                if player_team_margin >= 2.0 and prog_pct < 90.0:
                                    req_prop_pct += 8.0
                                elif -2.0 <= player_team_margin <= -1.0:
                                    req_prop_pct = max(50.0, req_prop_pct - 4.0)
                                    script_prop_tag += f" | +NHL-SCORE-EFFECTS-BOOST({player_team_abbr} {player_team_margin:+.0f})"

                            if prog_pct < req_prop_pct:
                                return False, (
                                    f"VETO [ESPN Boxscore]: {title} at {found_stat:.0f}/{target_val:.0f} "
                                    f"({prog_pct:.0f}% < {req_prop_pct:.0f}% P{live_period} script-adjusted min)"
                                )
                            # Late-Clock Crunch Veto: Never buy a prop in the final 5 mins of Q4 if <85% (or final 3 mins if <90%)!
                            if live_period >= 4 and clk_m < 5.0:
                                req_late_prop = 90.0 if clk_m < 3.0 else 85.0
                                if prog_pct < req_late_prop:
                                    return False, (
                                        f"VETO [Late Clock Crunch]: Only {clock_disp} left in P{live_period} and {title} is at "
                                        f"{found_stat:.0f}/{target_val:.0f} ({prog_pct:.0f}% < {req_late_prop:.0f}% min)"
                                    )
                            # Fallback NFL 2-Score Run-Script & Garbage-Time Blowout Bench Shield:
                            if len(competitors) >= 2 and prog_pct < 90.0:
                                if series in ("KXNFLPASSYDS", "KXNFLPASSTD", "KXNFLRECYDS", "KXNFLREC") and live_period >= 2 and gm_margin >= 14.0 and not script_prop_tag:
                                    return False, (
                                        f"VETO [NFL 2-Score Run-Script Trap]: NFL margin is {gm_margin:.0f} pts in P{live_period} "
                                        f"— winning team runs the clock (stalls QB/WR passing stats)!"
                                    )
                                if live_period >= 3:
                                    # Clock-Scaled White-Flag Blowout Bench Shield:
                                    # - In Q3: 18 pts NBA / 17 pts NFL
                                    # - In Early Q4 (>=5:00 left): 15 pts NBA / 16 pts NFL
                                    # - In Late Q4 (<5:00 left): 12 pts NBA / 14 pts NFL (coaches empty the bench with a 5-for-5 hockey sub!)
                                    if series in nba_prop_Set:
                                        max_blowout = 18.0 if live_period == 3 else (12.0 if clk_m < 5.0 else 15.0)
                                    else:
                                        max_blowout = 17.0 if live_period == 3 else (14.0 if clk_m < 5.0 else 16.0)
                                    if gm_margin >= max_blowout:
                                        return False, (
                                            f"VETO [White-Flag Blowout Bench Risk]: Game margin is {gm_margin:.0f} pts (>={max_blowout:.0f} max) "
                                            f"in P{live_period} {clock_disp} — coaches rest starters in blowouts!"
                                        )
                            lock_tag = (" | PROP-LOCK" if prog_pct >= (req_prop_pct + 12.0) else "") + script_prop_tag
                            if str(clock_disp).strip() in ("0:00", "0.0", ""):
                                lock_tag += " | CLOCK-FROZEN"
                            return True, f"ESPN Boxscore Verified ({found_stat:.0f}/{target_val:.0f} = {prog_pct:.0f}% banked in P{live_period}{lock_tag} | MIN={mins_played} | PF={fouls_val})"
                        return False, f"VETO [ESPN Boxscore]: Player '{player_target_name}' has 0 stats or not active in boxscore yet (P{live_period} {clock_disp})"

                return False, f"VETO [Fail-Closed Boxscore Gate]: Could not explicitly verify boxscore stat/line for {ticker} ({series}) in P{live_period}"
            except Exception as e:
                return False, f"VETO [ESPN API Error]: League live check failed ({e})"

        # C. Live Sports & Tennis Scoreboard Verification via ESPN API (Fail-Closed!)
        espn_map = {
            "KXWTAMATCH": "tennis/wta/scoreboard",
            "KXATPMATCH": "tennis/atp/scoreboard",
            "KXATPCHALLENGERMATCH": "tennis/atp/scoreboard",
            "KXNBAGAME": "basketball/nba/scoreboard",
            "KXNFLGAME": "football/nfl/scoreboard",
            "KXNCAAFGAME": "football/college-football/scoreboard?groups=80&limit=200",
            "KXNCAAMBGAME": "basketball/mens-college-basketball/scoreboard?groups=50&limit=200",
            "KXNCAAWBGAME": "basketball/womens-college-basketball/scoreboard?groups=50&limit=200",
            "KXNHLGAME": "hockey/nhl/scoreboard",
            "KXMLBGAME": "baseball/mlb/scoreboard",
            "KXUCLGAME": "soccer/uefa.champions/scoreboard",
            "KXMLSGAME": "soccer/usa.1/scoreboard",
            "KXLIGAMXGAME": "soccer/mex.1/scoreboard",
            "KXLALIGAGAME": "soccer/esp.1/scoreboard",
            "KXEPLGAME": "soccer/eng.1/scoreboard",
            "KXSERIEAGAME": "soccer/ita.1/scoreboard",
            "KXBUNDESLIGAGAME": "soccer/ger.1/scoreboard",
            "KXLIGUE1GAME": "soccer/fra.1/scoreboard",
        }
        is_tennis_s = series in ("KXWTAMATCH", "KXATPMATCH", "KXATPCHALLENGERMATCH")
        if series in espn_map:
            sport_path = espn_map[series]
            target_name = title.replace(" wins", "").strip().lower()
            last_word = target_name.split()[-1] if target_name else ""
            try:
                events = await self._get_espn_events_cached(hc, sport_path, ttl=4.0)
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
                            if is_tennis_s and ls:
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
                            endgame_snipe_tag = ""
                            if is_tennis_s:
                                game_diff = cur_set_games[idx] - cur_set_games[opp_idx]
                                if sets_won[idx] < sets_won[opp_idx]:
                                    return False, f"VETO [ESPN Tennis]: '{title}' is trailing in sets ({sets_won[idx]}-{sets_won[opp_idx]})"
                                min_set1_break = 3 if series == "KXWTAMATCH" else 2
                                min_set2_diff = 0 if series == "KXWTAMATCH" else -1
                                if sets_won[idx] == sets_won[opp_idx] and (game_diff < min_set1_break or cur_set_games[idx] < 3):
                                    return False, f"VETO [ESPN Tennis Break Risk]: '{title}' tied {sets_won[idx]}-{sets_won[opp_idx]} in sets and {cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f} (needs +{min_set1_break} game lead!)"
                                if sets_won[idx] > sets_won[opp_idx] and game_diff < min_set2_diff:
                                    return False, f"VETO [ESPN Tennis Momentum]: '{title}' down in current set ({cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f}) despite set lead"
                                # True Endgame Sniper Detection for Tennis (e.g., Flavio Cobolli leading 5-3 in 3rd Set or up 1 Set + 4-2 in Set 2!):
                                if (sets_won[idx] > sets_won[opp_idx] and cur_set_games[idx] >= 4 and game_diff >= 2) or (
                                    sets_won[idx] == sets_won[opp_idx] and sets_won[idx] >= 1 and cur_set_games[idx] >= 5 and game_diff >= 2
                                ):
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(Sets {sets_won[idx]}-{sets_won[opp_idx]}, Game {cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f})"
                            else:
                                if margin <= 0:
                                    return False, f"VETO [ESPN API]: '{title}' is NOT LEADING on live scoreboard ({scores[idx]:.0f} vs {scores[opp_idx]:.0f})"
                                # Active Opponent Scoring-Run Veto (Never buy into a team whose lead is actively shrinking!):
                                m_hist_map = getattr(self, "_live_margin_hist", None)
                                if m_hist_map is None:
                                    m_hist_map = {}
                                    self._live_margin_hist = m_hist_map
                                now_m_ts = time.time()
                                m_hist = [pt for pt in m_hist_map.get(ticker, []) if (now_m_ts - pt[0]) <= 180.0]
                                m_hist.append((now_m_ts, margin))
                                m_hist_map[ticker] = m_hist
                                recent_peak_margin = max(pt[1] for pt in m_hist)
                                margin_drop = recent_peak_margin - margin
                                max_allow_drop = 5.0 if series in ("KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME", "KXNFLGAME", "KXNCAAFGAME") else 1.0
                                if margin_drop >= max_allow_drop:
                                    return False, (
                                        f"VETO [Opponent Active Scoring Run]: '{title}' lead shrank from +{recent_peak_margin:.0f} down to +{margin:.0f} "
                                        "in last 3m — waiting for opponent's run to stop!"
                                    )

                            # Anti-Fluke / Multi-Possession Cushion Shield + Standings Quality Tier (`GOOD` vs `MEDIUM` vs `BAD`) on Full-Game Moneylines:
                            st_obj = comp.get("status", {})
                            period_num = int(st_obj.get("period", 1) or 1)
                            clock_disp = st_obj.get("displayClock", "")
                            ml_st_edge, ml_st_tag = (0.0, "") if is_tennis_s else _team_standings_edge(competitors[idx], competitors[opp_idx])
                            if "soccer/" in sport_path and margin < 2:
                                return False, (
                                    f"VETO [Soccer 1-Goal Lead Ban]: '{title}' only leading {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                    f"at {clock_disp} — STRICT 2+ GOAL CUSHION REQUIRED on all Soccer Moneylines{ml_st_tag}!"
                                )
                            elif series == "KXNHLGAME":
                                try:
                                    nhl_clk_m = float(str(clock_disp).split(":")[0]) if ":" in str(clock_disp) else 10.0
                                except Exception:
                                    nhl_clk_m = 10.0
                                min_nhl_margin = 3 if (period_num == 1 or (period_num == 2 and nhl_clk_m > 8.0)) else 2
                                if ml_st_edge <= -0.10 and not (period_num >= 3 and nhl_clk_m <= 10.0):
                                    min_nhl_margin = max(min_nhl_margin, 3)
                                if margin < min_nhl_margin:
                                    return False, (
                                        f"VETO [NHL Period/Standings Cushion]: '{title}' up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"in P{period_num} {clock_disp} (requires >={min_nhl_margin}-goal cushion{ml_st_tag}!)"
                                    )
                            elif series == "KXMLBGAME" and (period_num < 6 or margin < 2):
                                return False, (
                                    f"VETO [MLB Inning/Run Cushion]: '{title}' up {scores[idx]:.0f}-{scores[opp_idx]:.0f} in Inning {period_num} "
                                    "(requires >=6th Inning & >=2-run cushion!)"
                                )
                            elif series in ("KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME"):
                                try:
                                    q4_clk_m = float(str(clock_disp).split(":")[0]) if ":" in str(clock_disp) else 6.0
                                except Exception:
                                    q4_clk_m = 6.0
                                if period_num <= 2:
                                    min_bball_margin = 15 if series == "KXNBAGAME" else 10
                                elif period_num == 3:
                                    min_bball_margin = 12
                                else:
                                    min_bball_margin = 5 if q4_clk_m < 3.0 else 8
                                if ml_st_edge <= -0.10:
                                    min_bball_margin += 4
                                if margin < min_bball_margin:
                                    return False, (
                                        f"VETO [Basketball Cushion/Time Gate]: '{title}' up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"(+{margin:.0f} < +{min_bball_margin} pt min in P{period_num} {clock_disp}{ml_st_tag} — avoiding fragile Q3/Q4 leads!)"
                                    )
                                if period_num >= 4 and q4_clk_m <= 4.0 and margin >= min_bball_margin:
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(P{period_num} {clock_disp}, +{margin:.0f}pts)"
                            elif series in ("KXNFLGAME", "KXNCAAFGAME"):
                                min_fball_margin = 14 if period_num <= 2 else (10 if period_num == 3 else 7)
                                if ml_st_edge <= -0.10:
                                    min_fball_margin += 4
                                sit_fb = comp.get("situation") or {}
                                if sit_fb.get("isRedZone") and str(sit_fb.get("possession") or "") == str(competitors[opp_idx].get("id") or ""):
                                    min_fball_margin += 7
                                if margin < min_fball_margin:
                                    return False, (
                                        f"VETO [Football Cushion / Red-Zone Gate]: '{title}' up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"(+{margin:.0f} < +{min_fball_margin} pt min in P{period_num}{ml_st_tag} — avoiding single-TD / Red-Zone flips!)"
                                    )
                                if period_num >= 4 and margin >= 9:
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(P{period_num} {clock_disp}, +{margin:.0f}pts)"
                            if not endgame_snipe_tag:
                                if series == "KXNHLGAME" and period_num >= 3 and margin >= 2:
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(NHL P3 {clock_disp}, +{margin:.0f}G)"
                                elif "soccer/" in sport_path and period_num >= 2 and margin >= 2:
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(Soccer 2H {clock_disp}, +{margin:.0f}G)"
                                elif series == "KXMLBGAME" and period_num >= 8 and margin >= 2:
                                    endgame_snipe_tag = f" | ENDGAME-SNIPE(MLB Inn {period_num}, +{margin:.0f}R)"

                            # Check Vegas Opening vs Closing Line Movement, Game Clock Period & Live Win Probability
                            vegas_note = ml_st_tag + endgame_snipe_tag
                            try:
                                is_4q_sport = series in ("KXNBAGAME", "KXNFLGAME", "KXNCAAFGAME", "KXNCAAWBGAME", "KXNHLGAME")
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

        # 1. Whale Order Book Imbalance Check (Top 5 levels near touch) + Self-Correcting Series & Archetype Drawdown Gate
        series = ticker.split("-")[0] if "-" in ticker else ticker
        arch = self.classify_archetype(ticker)
        st = self.memory.get("series_stats", {}).get(series, {})
        series_pnl = float(st.get("realized_pnl", 0.0) or 0.0)
        arch_st = self.memory.get("archetype_learning", {}).get(arch, {})
        arch_pnl = float(arch_st.get("realized_pnl", 0.0) or 0.0)
        st_state_eval = self.memory.get("streak_state", {})
        in_defense_mode = int(st_state_eval.get("win_streak", 0) or 0) == 0 or float(st_state_eval.get("house_money_pool_dlr", 0.0) or 0.0) <= 0.0
        q_status_eval = self.get_daily_quota_status()
        quota_pace_relief = -0.15 if q_status_eval.get("behind_pace", False) else 0.0
        req_imbalance = max(1.25, (1.60 if (series_pnl < -1.00 or arch_pnl < -5.00) else 1.30) + (0.15 if in_defense_mode else 0.0) + quota_pace_relief)

        is_under_no = title.startswith("[UNDER-NO]")
        top_yes_bids = sorted(ob.yes_bids, key=lambda x: x.price, reverse=True)[:5]
        top_no_bids = sorted(ob.no_bids, key=lambda x: x.price, reverse=True)[:5]
        raw_yes_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_yes_bids)
        raw_no_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_no_bids)
        yes_depth = raw_no_depth if is_under_no else raw_yes_depth
        no_depth = raw_yes_depth if is_under_no else raw_no_depth
        imbalance_ratio = (yes_depth + 1.0) / (no_depth + 1.0)

        is_quarter_mkt = any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P"))
        is_prop_mkt = len(ticker.split("-")) >= 4
        eff_min_depth = min(min_book_depth_dlr, 90.0) if is_quarter_mkt else (min(min_book_depth_dlr, 150.0) if is_prop_mkt else min_book_depth_dlr)
        is_depth_scout = False
        if yes_depth < eff_min_depth:
            if yes_depth >= 85.0:
                is_depth_scout = True
            else:
                self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                self.save_memory()
                return False, f"VETO: Thin bid liquidity (${yes_depth:.0f} < $85 min Scout exit depth)", 0.0

        if imbalance_ratio < req_imbalance:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, f"VETO: Weak whale buying pressure (Imbalance={imbalance_ratio:.2f}x < {req_imbalance:.2f}x min for {series})", 0.0

        # 2. Recent Trade Tape Recency, Velocity & Whip-Saw Volatility Check:
        #    - Gate 2A (Pop-and-Drop Reversal Veto): Never buy a percentage that jumped to a peak and dropped >=4c back down!
        #    - Gate 2B (Parabolic Vertical Spike Veto): If price jumped >= +22c inside < 2.5 mins from <62c, wait for consolidation!
        #    - Gate 2C (Whip-Saw Volatility Veto): Block ping-ponging contracts where total tick path >= 28c with low net efficiency!
        momentum_delta = 0
        last_trade_mins_ago = 999.0
        tape_velocity_tag = ""
        try:
            trades_resp = await client.request(
                "GET", "/markets/trades", params={"ticker": ticker, "limit": 20}, authenticated=False
            )
            trades = trades_resp.get("trades", [])
            if not trades:
                if not (is_quarter_mkt or is_prop_mkt):
                    self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                    self.save_memory()
                    return False, "VETO: Zero recent trades on tape (inactive market)", 0.0
                last_trade_mins_ago = 0.0
            else:
                now_utc = datetime.now(timezone.utc)
                last_ts = datetime.fromisoformat(trades[0]["created_time"].replace("Z", "+00:00"))
                last_trade_mins_ago = max(0.0, (now_utc - last_ts).total_seconds() / 60.0)
                max_stale_m = 60.0 if (is_quarter_mkt or is_prop_mkt) else (18.0 if q_status_eval.get("behind_pace", False) else 12.0)
                if last_trade_mins_ago > max_stale_m:
                    self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                    self.save_memory()
                    return False, f"VETO: Stale market (last trade {last_trade_mins_ago:.1f}m ago > {max_stale_m:.0f}m max)", 0.0

                if len(trades) >= 3:
                    raw_yes_prices = [
                        int(round(float(tr.get("yes_price_dollars") or (tr.get("yes_price", 0) / 100.0)) * 100))
                        for tr in trades
                    ]
                    t_prices = [(100 - p) for p in raw_yes_prices] if is_under_no else raw_yes_prices
                    newest_c = t_prices[0]
                    window_10 = t_prices[:10]
                    oldest_10_c = window_10[-1]
                    momentum_delta = newest_c - oldest_10_c
                    # Allow -1c 10-trade bid-ask print bounce if Whale Imbalance >= 1.80x and last print did not tick down:
                    min_allowed_drift = -1 if (imbalance_ratio >= 1.80 and newest_c >= t_prices[1]) else 0
                    if momentum_delta < min_allowed_drift:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, f"VETO: Negative tape drift ({momentum_delta}c over last {len(window_10)} trades — only buying rising/flat leaders)", 0.0

                    # Gate 2A-0: Multi-Timeframe Anti-Falling-Knife & Live Orderbook Divergence Shield:
                    # 1) Zero Down-Tick Law: Last print (`newest_c`) must NOT be lower than the print immediately before it (`t_prices[1]`) or the 3-print average!
                    prev_3_avg = sum(t_prices[1:4]) / max(1, len(t_prices[1:4]))
                    if newest_c < t_prices[1] or newest_c < (prev_3_avg - 0.5):
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Micro Falling Knife]: Last trade ticked DOWN ({t_prices[1]}c -> {newest_c}c, 3-trade avg={prev_3_avg:.1f}c) "
                            "— waiting for confirmed uptick!"
                        ), 0.0
                    # 2) Live Orderbook Falling Knife Shield: Live best bid must not have dropped >=2c below the last executed trade!
                    live_side_bid = (ob.best_no_bid or 0) if is_under_no else (ob.best_yes_bid or 0)
                    if live_side_bid < (newest_c - 1):
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Orderbook Falling Knife]: Live bid ({live_side_bid}c) dropped below last trade ({newest_c}c) "
                            "— market makers pulling bids!"
                        ), 0.0
                    # 3) Anti-Single-Print Spoof Jump Filter: A single isolated jump (>3.5c above the 3-trade average) must print at least 2-3 fills at the new level!
                    cur_3_avg = sum(t_prices[:3]) / 3.0
                    if (newest_c - cur_3_avg) > 3.5:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Unconfirmed Single-Print Jump]: Last print ({newest_c}c) jumped +{newest_c - cur_3_avg:.1f}c above 3-trade avg "
                            f"({cur_3_avg:.1f}c) — waiting for multi-trade support at this level!"
                        ), 0.0

                    # Gate 2A: Pop-and-Drop / Peak Pullback Veto (Jumped up and is now dropping back down!)
                    tape_high_c = max(t_prices[:12])
                    pullback_from_peak_c = tape_high_c - newest_c
                    if pullback_from_peak_c >= 4:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Pop-and-Drop Reversal]: Jumped to {tape_high_c}c peak and dropped -{pullback_from_peak_c}c "
                            f"back down to {newest_c}c (avoiding fading spike!)"
                        ), 0.0

                    # Gate 2B: Parabolic Vertical Spike / Overheated FOMO Exhaustion Veto
                    # Check trades within the last 2.5 minutes: if it jumped >= +22c from <62c in <2.5 mins, wait for consolidation!
                    recent_fast_prices = []
                    for tr, pc in zip(trades, t_prices):
                        try:
                            tr_dt = datetime.fromisoformat(tr["created_time"].replace("Z", "+00:00"))
                            if (now_utc - tr_dt).total_seconds() <= 150.0:
                                recent_fast_prices.append(pc)
                        except Exception:
                            pass
                    if len(recent_fast_prices) >= 2:
                        fast_low_c = min(recent_fast_prices)
                        fast_jump_c = newest_c - fast_low_c
                        if fast_jump_c >= 22 and fast_low_c <= 62:
                            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                            self.save_memory()
                            return False, (
                                f"VETO [Overheated Vertical Spike]: Percentage jumped +{fast_jump_c}c ({fast_low_c}c -> {newest_c}c) "
                                "in <2.5 mins — waiting for consolidation so it doesn't snap back!"
                            ), 0.0

                    # Gate 2C: Tick-to-Tick Whip-Saw Volatility Veto (Ping-Pong Percentage)
                    total_path_c = sum(abs(window_10[i] - window_10[i - 1]) for i in range(1, len(window_10)))
                    net_disp_c = abs(newest_c - oldest_10_c)
                    eff_ratio = (net_disp_c / total_path_c) if total_path_c > 0 else 1.0
                    if total_path_c >= 24 and eff_ratio < 0.25:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Whip-Saw Percentage Volatility]: Price ping-ponging ({total_path_c}c total swing for only "
                            f"{momentum_delta:+d}c net move, efficiency={eff_ratio:.0%})!"
                        ), 0.0

                    # Check External Real-World API Research FIRST so True Endgame Snipes (like 5-3 Tennis 3rd Set, <4m Quarter/Under Locks)
                    # can fast-track past slow plateau waits and snipe up to 90c!
                    last_4_p = t_prices[:min(4, len(t_prices))]
                    plateau_dwell_sec = 60.0
                    if len(trades) >= 4:
                        try:
                            t4_dt = datetime.fromisoformat(trades[3]["created_time"].replace("Z", "+00:00"))
                            plateau_dwell_sec = max(0.0, (now_utc - t4_dt).total_seconds())
                        except Exception:
                            plateau_dwell_sec = 60.0
                    if 0 <= momentum_delta <= 14 and pullback_from_peak_c <= 1 and min(last_4_p) >= (newest_c - 2):
                        tape_velocity_tag = f" | +PLATEAU-HOLD({plateau_dwell_sec:.0f}s, Eff={eff_ratio:.0%})"
                else:
                    last_4_p = [80]
                    plateau_dwell_sec = 60.0
                    newest_c = 80
        except Exception as e:
            return False, f"VETO: Trade tape unavailable ({e})", 0.0

        # 3. External Real-World API Research (Coinbase Spot, ESPN Live Scoreboard, Open-Meteo Weather)
        ext_ok, ext_reason = await self.verify_external_research(client, ticker, title)
        if not ext_ok:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, ext_reason, 0.0

        is_endgame_snipe = (
            "ENDGAME-SNIPE" in ext_reason
            or (is_under_no and "LATE-CLOCK" in ext_reason)
        )
        live_side_ask = (ob.best_no_ask or 99) if is_under_no else (ob.best_yes_ask or 99)
        if live_side_ask > 86 and not is_endgame_snipe:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, (
                f"VETO [87-90c Reserved for Endgame Snipes]: Ask={live_side_ask}c > 86c and not in final-minutes ENDGAME-SNIPE / LATE-CLOCK UNDER window"
            ), 0.0

        # Gate 2D: Sustained Plateau & Trend Hold Filter (Applied to Mid-Game Plays; Bypassed on Verified Endgame Snipes!)
        if not is_endgame_snipe and len(trades) >= 3:
            if min(last_4_p) < (newest_c - 3):
                self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                self.save_memory()
                return False, (
                    f"VETO [Unheld Flash Pop — No Plateau Support]: Last 4 prints ({last_4_p}) haven't stabilized within 3c of {newest_c}c yet "
                    "— waiting for price to hold steady so it doesn't dip 1-2% after entry!"
                ), 0.0
            if momentum_delta >= 6 and plateau_dwell_sec < 25.0:
                self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                self.save_memory()
                return False, (
                    f"VETO [Too-Fast Pop Without Plateau Hold]: Climbed +{momentum_delta}c in only {plateau_dwell_sec:.0f}s "
                    "— waiting >=25s at plateau to confirm support!"
                ), 0.0

        # 3B. Live Pinnacle / Sharp Book Multiplicative De-Vig Check (The Odds API v4 — GEMINI.md Law 4 Active Veto Gate!)
        pin_note = await self.verify_pinnacle_sharp_odds(client, series, ticker, title)
        if pin_note:
            ext_reason += pin_note
            if not is_under_no and "ENDGAME-SNIPE" not in ext_reason:
                import re as _re_pin
                _m_dv = _re_pin.search(r"_DeVig=(\d+(?:\.\d+)?)%", pin_note)
                if _m_dv:
                    dv_val = float(_m_dv.group(1))
                    if dv_val < 54.0:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Sharp Vegas Underdog Fluke Gate]: Sharp sportsbook De-Vig is only {dv_val:.1f}% (<54% min) "
                            f"while Kalshi is {live_side_ask}c — avoiding underdog fluke leads that mean-revert!"
                        ), 0.0
                    if (live_side_ask - dv_val) > 18.0 and "LATE-CLOCK" not in ext_reason:
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Sharp Vegas Overpriced Gate]: Kalshi ask ({live_side_ask}c) exceeds Sharp De-Vig ({dv_val:.1f}%) "
                            f"by +{live_side_ask - dv_val:.1f}% (>18% max gap before late clock)!"
                        ), 0.0
        if tape_velocity_tag:
            ext_reason += tape_velocity_tag

        # 4. Institutional 0-100 Team/Player Pedigree & Trend Confidence Score + Minimum 75/100 Gate:
        conf_score = 76
        if "ENDGAME-SNIPE" in ext_reason:
            conf_score += 8   # Verified Final-Minutes / Match-Point Endgame Snipe boost!
        if "STANDINGS[ELITE" in ext_reason or "STANDINGS[#" in ext_reason or "FANTASY-ALPHA" in ext_reason:
            conf_score += 10
        elif "STANDINGS[GOOD" in ext_reason or "STARTER#1" in ext_reason:
            conf_score += 5
        elif "STANDINGS[BAD" in ext_reason:
            conf_score -= 12  # Penalize bad/tanking teams heavily so weak leads get vetoed!
        if "vs BAD(" in ext_reason:
            conf_score += 4   # Bonus when playing AGAINST a bad/tanking opponent!
        elif "vs ELITE(" in ext_reason and not is_under_no:
            conf_score -= 5   # Caution when leading against an Elite powerhouse opponent!
        if "+PLATEAU-HOLD" in ext_reason:
            conf_score += 5
        if imbalance_ratio >= 2.25:
            conf_score += 5
        elif imbalance_ratio >= 1.75:
            conf_score += 3
        if "PINNACLE-SHARP" in ext_reason or "VEGAS-STEAM" in ext_reason:
            conf_score += 4
        if "CLOCK-FROZEN" in ext_reason or "LATE-CLOCK" in ext_reason or is_under_no:
            conf_score += 4
        conf_score = max(10, min(99, conf_score))
        min_conf_req = 74 if ("ENDGAME-SNIPE" in ext_reason or is_under_no) else 76
        if conf_score < min_conf_req:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, (
                f"VETO [Low Team Pedigree / Confidence Score: {conf_score}/100 < {min_conf_req} min]: Shaky team quality or matchup ({ext_reason})"
            ), 0.0
        is_scout_play = is_depth_scout or ("SCOUT-TIER" in ext_reason) or (conf_score <= 78)
        self.memory.setdefault("last_conf_scores", {})[ticker] = conf_score
        self.memory.setdefault("last_scout_flags", {})[ticker] = is_scout_play
        tier_tag = "TIER-C SCOUT($28)" if is_scout_play else ("TIER-B BUILDER" if conf_score <= 82 else "TIER-A SNIPER")

        conf_mult = 1.0
        if series_pnl > 0:
            conf_mult = 1.15  # Boost allocation 15% on proven profitable series
        elif series_pnl < -1.00:
            conf_mult = 0.85  # Auto-trim size 15% on drawdown series until back in green
        arch_wins = int(arch_st.get("wins", 0) or 0)
        arch_losses = int(arch_st.get("losses", 0) or 0)
        if (arch_wins + arch_losses) >= 3:
            arch_wr = arch_wins / max(1, arch_wins + arch_losses)
            if arch_wr >= 0.80 and arch_pnl > 0:
                conf_mult *= 1.10
            elif arch_wr < 0.65 or arch_pnl < -10.0:
                conf_mult *= 0.85
        if imbalance_ratio >= 2.25 and momentum_delta >= 0:
            conf_mult *= 1.22  # A+ Whale Wall (>=2.25x) + non-negative tape momentum
        elif imbalance_ratio >= 1.85 and momentum_delta >= 0:
            conf_mult *= 1.12
        if "LATE-CLOCK" in ext_reason or "PROP-LOCK" in ext_reason:
            conf_mult *= 1.12  # Late-game clock drain or >=80% banked player prop boost
        if "CLOCK-FROZEN" in ext_reason or "VEGAS-STEAM" in ext_reason or "PINNACLE-SHARP" in ext_reason:
            conf_mult *= 1.08  # Commercial/Halftime zero-snipe edge or Pinnacle/Vegas sharp boost

        self.save_memory()
        reason = (
            f"APPROVED [{tier_tag} | CONF={conf_score}/100 | Whale={imbalance_ratio:.2f}x | Depth=${yes_depth:.0f} | LastTrade={last_trade_mins_ago:.1f}m | Tape={momentum_delta:+d}c | {ext_reason}]"
        )
        return True, reason, conf_mult


def get_scaling_tier(equity_cents: int) -> dict:
    """
    Automatic Institutional Kelly Bankroll Scaling with STRICT 10%-12% Per-Trade Risk Cap.
    Never risks more than 10%-12% of account equity ($44-$55 at $440 equity) on a single play,
    so even back-to-back-to-back stop-losses (-10c cap = ~$6/loss) can NEVER threaten the account!
    """
    eq_dlr = equity_cents / 100.0
    if eq_dlr < 120.0:
        return {
            "name": "TIER 1 (Protected Growth)",
            "base_pct": 0.12,
            "boost_pct": 0.14,
            "min_stake_cents": 800,
            "max_stake_cents": 2000,
            "min_contracts": 10,
            "max_concurrent": 3,
            "min_depth_dlr": 150.0,
            "iceberg_slice": 30,
        }
    elif eq_dlr < 300.0:
        return {
            "name": "TIER 2 (Protected Acceleration)",
            "base_pct": 0.10,
            "boost_pct": 0.12,
            "min_stake_cents": 1500,
            "max_stake_cents": min(3600, max(2000, int(equity_cents * 0.12))),
            "min_contracts": 18,
            "max_concurrent": 3,
            "min_depth_dlr": 250.0,
            "iceberg_slice": 45,
        }
    elif eq_dlr < 750.0:
        # Strict 10%-12% Max Per-Trade Cap ($44-$55 per trade at $440-$460 equity = ~55-65 contracts)
        dyn_max_stake_c = min(5500, max(3500, int(equity_cents * 0.12)))
        dyn_min_stake_c = min(4000, max(2500, int(equity_cents * 0.09)))
        dyn_min_contracts = min(45, max(25, int(eq_dlr * 0.07)))
        return {
            "name": "TIER 3 ($300-$750 Protected Pro Mode)",
            "base_pct": 0.10,
            "boost_pct": 0.12,
            "min_stake_cents": dyn_min_stake_c,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 3,
            "min_depth_dlr": 300.0,
            "iceberg_slice": 240,
        }
    elif eq_dlr < 2000.0:
        dyn_max_stake_c = min(15000, max(6000, int(equity_cents * 0.11)))
        dyn_min_contracts = min(90, max(45, int(eq_dlr * 0.06)))
        return {
            "name": "TIER 4 ($750-$2k Protected Whale Mode)",
            "base_pct": 0.10,
            "boost_pct": 0.11,
            "min_stake_cents": 5000,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 4,
            "min_depth_dlr": 450.0,
            "iceberg_slice": 280,
        }
    else:
        working_eq_dlr = min(eq_dlr, 5000.0)
        working_eq_cents = min(equity_cents, 500000)
        dyn_min_contracts = min(180, max(90, int(working_eq_dlr * 0.045)))
        dyn_max_stake_c = min(45000, max(18000, int(working_eq_cents * 0.10)))
        return {
            "name": "TIER 5 ($5k Sweet-Spot Plateau Mode)",
            "base_pct": 0.09,
            "boost_pct": 0.10,
            "min_stake_cents": 9000,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 5,
            "min_depth_dlr": 750.0,
            "iceberg_slice": 180,
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
    stopped_out_events: set[str] = {"KXLIGAMXGAME-26OCT25TIJSLA", "KXSERIEATOTAL-26OCT27ROMCAG", "KXSERIEAGAME-26OCT27ROMCAG"}
    stopped_out_matchups: set[str] = set(educator.memory.setdefault("stopped_out_matchups", ["26OCT27ROMCAG", "26OCT25TIJSLA"]))
    series_shard_cache: dict[str, int] = {}
    cold_series_until: dict[str, float] = {}
    series_markets_cache: dict[str, tuple[float, list]] = {}
    veto_cooldown_until: dict[str, float] = {}
    stop_breach_counts: dict[str, int] = {}

    def _entity_key(tk: str) -> str:
        pts = tk.split("-")
        return "-".join(pts[:3]) if len(pts) >= 4 else "-".join(pts[:2])

    def _matchup_slug(tk: str) -> str:
        pts = tk.split("-")
        return pts[1] if len(pts) >= 2 else tk

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

            # HARD DAILY CIRCUIT BREAKER (Max -$40.00 Daily Drawdown Stop):
            cb_state = educator.memory.setdefault("daily_circuit_breaker", {})
            today_str = now_utc.strftime("%Y-%m-%d")
            if cb_state.get("day") != today_str or cb_state.get("protected_floor_cents") is None:
                cb_state["day"] = today_str
                cb_state["start_equity_cents"] = equity_cents
                cb_state["protected_floor_cents"] = max(10000, equity_cents - 4000)
                cb_state["tripped"] = False
                educator.save_memory()
            daily_floor_c = int(cb_state.get("protected_floor_cents", 40000))
            if equity_cents > int(cb_state.get("start_equity_cents", equity_cents)):
                # Ratchet daily start equity & floor UP as we win during the day!
                cb_state["start_equity_cents"] = equity_cents
                cb_state["protected_floor_cents"] = max(daily_floor_c, equity_cents - 4000)
                daily_floor_c = int(cb_state["protected_floor_cents"])
                educator.save_memory()
            if equity_cents <= daily_floor_c and not cb_state.get("tripped", False):
                cb_state["tripped"] = True
                educator.memory["manual_pause_buys"] = True
                educator.save_memory()
                logger.error(
                    f"🚨 DAILY CIRCUIT BREAKER TRIPPED: Equity ${equity_cents/100:.2f} hit protected floor ${daily_floor_c/100:.2f} (-$40 max daily drawdown cap). Pausing all buys!"
                )
                await send_phone_alert(
                    client,
                    "🚨 DAILY CIRCUIT BREAKER TRIPPED (-$40 Cap)",
                    (
                        f"Live Equity (${equity_cents/100:.2f}) reached today's protected floor (${daily_floor_c/100:.2f}).\n"
                        "All new buying is automatically PAUSED for the rest of the day to protect your bankroll."
                    ),
                )

            # Track $5,000 Sweet-Spot Plateau & Weekly Bank-Withdrawal Vault
            eq_dlr_now = round(equity_cents / 100.0, 2)
            withdrawable_vault_dlr = round(max(0.0, eq_dlr_now - 5000.0), 2)
            vault_obj = educator.memory.setdefault(
                "sweet_spot_plateau_vault",
                {"working_base_cap_dlr": 5000.0, "withdrawable_above_5k_dlr": 0.0, "last_milestone_alerted_dlr": 0.0},
            )
            vault_obj["withdrawable_above_5k_dlr"] = withdrawable_vault_dlr
            # Milestone Upgrade Reminders (#2 Kalshi Free Advanced API Tier @ $750+ & #3 OpticOdds/Unabated @ $5,000+)
            upg_rem = educator.memory.setdefault(
                "upgrade_reminders",
                {
                    "kalshi_advanced_api_tier_alerted": False,
                    "opticodds_5k_plateau_alerted": False,
                },
            )
            if eq_dlr_now >= 750.0 and not upg_rem.get("kalshi_advanced_api_tier_alerted"):
                upg_rem["kalshi_advanced_api_tier_alerted"] = True
                educator.save_memory()
                await send_phone_alert(
                    client,
                    "🥈 MILESTONE REMINDER #2: Unlock Free Kalshi Advanced API Tier!",
                    (
                        f"Equity hit ${eq_dlr_now:,.2f} with consistent Maker volume!\n"
                        "Action: Email/message Kalshi API Support to request the FREE 'Advanced / Market Maker API Tier' "
                        "(30-100+ req/sec) so we can drop scan speed from 5s down to 1.5s!"
                    ),
                )
            if eq_dlr_now >= 5000.0 and not upg_rem.get("opticodds_5k_plateau_alerted"):
                upg_rem["opticodds_5k_plateau_alerted"] = True
                educator.save_memory()
                await send_phone_alert(
                    client,
                    "🥉 MILESTONE REMINDER #3 ($5,000 HIT!): Unlock OpticOdds / Unabated Feed!",
                    (
                        f"WE HIT THE $5,000 SWEET-SPOT PLATEAU (${eq_dlr_now:,.2f})!\n"
                        "1. Unlock Antigravity's $250 Trophy Pod!\n"
                        "2. Optional Institutional Upgrade: Use pure profits to unlock OpticOdds / Unabated sub-second WebSocket feed!"
                    ),
                )
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

            # Detect when any previously held position cashes out (Supports both YES > 0 and NO < 0 positions!)
            open_pos_fp_map: dict[str, float] = {}
            for pos in positions:
                t = pos.get("ticker", "")
                raw_cnt = float(pos.get("position_fp", "0") or 0)
                open_pos_fp_map[t] = raw_cnt
                cnt = abs(raw_cnt)
                exp = float(pos.get("market_exposure_dollars", "0") or 0)
                if cnt >= 1.0:
                    prev_tr = tracked_open_positions.get(t, {})
                    max_c = max(int(cnt), int(prev_tr.get("max_count", 0) or 0))
                    max_s = max(exp, float(prev_tr.get("max_stake", 0.0) or 0.0))
                    stage1_alerted = bool(prev_tr.get("stage1_alerted", False))
                    if max_c >= 6 and int(cnt) <= int(max_c * 0.65) and not stage1_alerted:
                        stage1_alerted = True
                        sold_c = max_c - int(cnt)
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
                        "is_no": (raw_cnt < 0),
                        "entry_ts": float(prev_tr.get("entry_ts") or time.time()),
                        "scratch_requeued": bool(prev_tr.get("scratch_requeued", False)),
                        "stage2_ratcheted": bool(prev_tr.get("stage2_ratcheted", False)),
                    }
                    educator.save_memory()
                elif cnt < 1.0 and t in tracked_open_positions:
                    prev = tracked_open_positions.pop(t)
                    realized_pnl = float(pos.get("realized_pnl_dollars", "0") or 0)
                    stake_dlr = prev.get("max_stake") or prev["stake"]
                    cnt_int = max(1, int(prev.get("max_count") or prev["count"]))
                    avg_ent_c = int(round((stake_dlr / cnt_int) * 100)) if stake_dlr > 0 else 82
                    pk_c = peak_bids.get(t, avg_ent_c + 6)
                    if realized_pnl >= 0:
                        educator.memory.setdefault("harvested_tickers", []).append(t)
                        educator.memory.setdefault("last_win_entry_cents", {})[t] = avg_ent_c
                        profit_dlr = realized_pnl if realized_pnl > 0 else max(0.01, round((cnt_int * 0.92) - stake_dlr, 2))
                        educator.record_trade_autopsy(
                            ticker=t,
                            entry_cents=avg_ent_c,
                            exit_cents=min(96, avg_ent_c + int(round((profit_dlr / cnt_int) * 100))),
                            peak_cents=pk_c,
                            net_pnl_dlr=profit_dlr,
                            exit_reason="STAGE1_AND_STAGE2_MAKER_HARVEST",
                        )
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
                    else:
                        stopped_out_events.add(_entity_key(t))
                        if not t.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB", "KXINX", "KXNASDAQ", "KXWTI")):
                            stopped_out_matchups.add(_matchup_slug(t))
                            educator.memory["stopped_out_matchups"] = list(stopped_out_matchups)
                        educator.record_trade_autopsy(
                            ticker=t,
                            entry_cents=avg_ent_c,
                            exit_cents=max(1, avg_ent_c + int(round((realized_pnl / cnt_int) * 100))),
                            peak_cents=pk_c,
                            net_pnl_dlr=realized_pnl,
                            exit_reason="POSITION_CLOSED_OR_SETTLED_LOSS",
                        )

            orders_resp = await client.request("GET", "/portfolio/orders", authenticated=True)
            open_orders = [
                o for o in orders_resp.get("orders", []) if o.get("status") == "resting"
            ]

            resting_sell_orders: dict[str, list[dict]] = {}
            resting_sell_counts: dict[str, float] = {}
            resting_buy_tickers: set[str] = set()
            locked_buy_shards: set[int] = set()
            stale_buys_to_cancel: list[dict] = []
            for o in open_orders:
                t = o.get("ticker", "")
                p_fp = open_pos_fp_map.get(t, 0.0)
                o_act = o.get("action", "")
                # A resting order is a closing Take-Profit order if we hold YES (p_fp >= 1) and action=="sell",
                # OR if we hold NO (p_fp <= -1, Late-Clock Under) and action=="buy"!
                is_closing_order = (p_fp >= 1.0 and o_act == "sell") or (p_fp <= -1.0 and o_act == "buy")
                if is_closing_order:
                    resting_sell_orders.setdefault(t, []).append(o)
                    resting_sell_counts[t] = resting_sell_counts.get(t, 0.0) + float(
                        o.get("remaining_count_fp", "0") or 0
                    )
                else:
                    oid = o.get("order_id", "")
                    age_sec = 0.0
                    c_str = o.get("created_time", "")
                    if c_str:
                        try:
                            c_dt = datetime.fromisoformat(c_str.replace("Z", "+00:00"))
                            age_sec = (now_utc - c_dt).total_seconds()
                        except Exception:
                            age_sec = 999.0
                    # FIFO Senior Queue-Priority Preservation + Instant Outbid Re-Pegging:
                    # 1) If our resting Maker buy was OUTBID (`my_bid_c < cur_best_b`) and is >=8s old, cancel immediately (`max_wait_sec = 8.0`)
                    #    so we can re-peg to the new front of the line on the next cycle instead of sitting buried for 25s!
                    # 2) If our resting Maker buy is STILL at the #1 best bid (`my_bid_c >= cur_best_b`) on a tight 1c-2c spread,
                    #    hold our senior FIFO timestamp up to 75s so we never give up our #1 spot in line!
                    filled_so_far = float(o.get("fill_count_fp", "0") or 0)
                    max_wait_sec = 75.0 if filled_so_far >= 1.0 else 25.0
                    if age_sec >= 8.0 and filled_so_far < 1.0:
                        try:
                            ob_q = await client.get_orderbook(t)
                            o_side_str = o.get("book_side") or o.get("side") or "bid"
                            is_no_buy_q = (o_side_str == "ask")
                            cur_best_b = (ob_q.best_no_bid or 0) if is_no_buy_q else (ob_q.best_yes_bid or 0)
                            cur_best_a = (ob_q.best_no_ask or 99) if is_no_buy_q else (ob_q.best_yes_ask or 99)
                            yes_p_c = int(round(float(o.get("yes_price_dollars") or 0) * 100))
                            my_bid_c = (100 - yes_p_c) if is_no_buy_q else yes_p_c
                            if my_bid_c > 0 and cur_best_b > my_bid_c:
                                max_wait_sec = 8.0
                            elif my_bid_c > 0 and my_bid_c >= cur_best_b and 1 <= (cur_best_a - cur_best_b) <= 2 and 76 <= cur_best_a <= 90:
                                max_wait_sec = 75.0
                        except Exception:
                            pass
                    if age_sec > max_wait_sec:
                        stale_buys_to_cancel.append(
                            {
                                "order_id": oid,
                                "market_ticker": t,
                                "exchange_index": o.get("exchange_index", 0),
                                "subaccount": o.get("subaccount_number", 0),
                            }
                        )
                    else:
                        resting_buy_tickers.add(t)
                        locked_buy_shards.add(int(o.get("exchange_index", 0) or 0))

            if stale_buys_to_cancel:
                logger.info(f"Auto-canceling {len(stale_buys_to_cancel)} unfilled resting buy order(s) (>25s UTC age) to prevent stale fills and free cash.")
                try:
                    await client.request(
                        "DELETE",
                        "/portfolio/events/orders/batched",
                        json_data={"orders": stale_buys_to_cancel},
                        authenticated=True,
                    )
                    await asyncio.sleep(0.4)
                    bal = await client.request("GET", "/portfolio/balance", authenticated=True)
                    total_cents = int(bal.get("balance", 0) or 0)
                    port_cents = int(bal.get("portfolio_value", 0) or 0)
                    equity_cents = total_cents + port_cents
                except Exception:
                    pass

            # 1. Manage Open Positions: Tight -10c Stop-Loss Cap + Stage-1/Stage-2 Harvest Queue (Supports YES & NO Under positions!)
            for pos in positions:
                ticker = pos.get("ticker", "")
                pos_fp = float(pos.get("position_fp", "0") or 0)
                is_no_pos = (pos_fp <= -1.0)
                whole_pos = int(abs(pos_fp))
                if whole_pos < 1:
                    continue

                ob = await client.get_orderbook(ticker)
                cur_bid = (ob.best_no_bid or 0) if is_no_pos else (ob.best_yes_bid or 0)
                cur_ask = (ob.best_no_ask or 99) if is_no_pos else (ob.best_yes_ask or 99)
                cur_spread = cur_ask - cur_bid
                if cur_bid > peak_bids.get(ticker, 0):
                    peak_bids[ticker] = cur_bid

                peaked = peak_bids.get(ticker, cur_bid)
                exp_dlr = float(pos.get("market_exposure_dollars", "0") or 0)
                avg_entry_cents = int(round((exp_dlr / abs(pos_fp)) * 100)) if exp_dlr > 0 else 80
                max_orig_cnt = int(tracked_open_positions.get(ticker, {}).get("max_count", whole_pos) or whole_pos)
                pos_conf_score = int(educator.memory.get("last_conf_scores", {}).get(ticker, 83) or 83)
                # Stage-1 is ONLY banked when >=40% of contracts have ACTUALLY filled & cashed out (never on an unfilled touch!)
                stage1_banked = (max_orig_cnt >= 6 and whole_pos <= int(max_orig_cnt * 0.58))
                # CONFIDENCE-TUNED STOP-LOSS & STAGE-1 SHIELD:
                # - Elite Powerhouse Teams (`pos_conf_score >= 85/100`): full `-10c` stop-loss buffer so a 1-play blip never shakes us out!
                # - Medium-Confidence Teams (`pos_conf_score < 82/100`): tighter `-8c` leash + 70% Stage-1 early profit lock!
                max_stop_dist = 10 if pos_conf_score >= 82 else 8
                if stage1_banked:
                    # STRICT ZERO-GIVEBACK BREAKEVEN RATCHET:
                    # Once Stage-1 (60%) has filled at +5c/+6c, NEVER allow the 40% runner to fall below avg_entry_cents!
                    stop_floor_c = max(68, avg_entry_cents)
                elif peaked >= avg_entry_cents + 5:
                    # Wall Street Anti-Round-Trip Ratchet: if trade touched +5c profit, tighten stop floor to -2c!
                    stop_floor_c = max(66, avg_entry_cents - 2)
                elif peaked >= avg_entry_cents + 3:
                    stop_floor_c = max(64, avg_entry_cents - min(5, max_stop_dist - 3))
                else:
                    stop_floor_c = max(62, avg_entry_cents - max_stop_dist)
                raw_breach = (0 < cur_bid <= stop_floor_c) and (cur_ask <= stop_floor_c + 4)
                thesis_broken_guillotine = False
                if raw_breach:
                    # Vegas / Live Scoreboard-Confirmed Guillotine + 2-Tick Anti-Flash-Wick Shield (GEMINI.md Law / Pod 1):
                    # Never panic-sell the bottom of a 5-second market-maker bid pull (like NJ 81c -> 68c -> 84c!)
                    # if our team STILL holds its scoreboard cushion on ESPN!
                    stop_breach_counts[ticker] = stop_breach_counts.get(ticker, 0) + 1
                    sb_still_valid = False
                    if cur_bid >= 60:
                        try:
                            sb_still_valid, _ = await educator.verify_external_research(
                                client, ticker, f"[UNDER-NO] {ticker}" if is_no_pos else ticker
                            )
                        except Exception:
                            sb_still_valid = False
                    eff_floor_c = (stop_floor_c - 4) if sb_still_valid else stop_floor_c
                    if cur_bid <= 56 or (cur_bid <= eff_floor_c and stop_breach_counts[ticker] >= 2):
                        thesis_broken_guillotine = True
                    else:
                        logger.info(
                            f"🛡️ Anti-Wick / Scoreboard Hold on {ticker}: bid={cur_bid}c dipped to floor={stop_floor_c}c "
                            f"(tick {stop_breach_counts[ticker]}/2, ScoreboardValid={sb_still_valid}, eff_floor={eff_floor_c}c) — holding through temporary flash wick!"
                        )
                else:
                    stop_breach_counts[ticker] = 0
                if thesis_broken_guillotine:
                    logger.warning(
                        f"TIGHT -10c STOP-LOSS / STAGE-1 SHIELD TRIGGERED on {ticker} ({'NO-UNDER' if is_no_pos else 'YES'}, cur_bid={cur_bid}c, ask={cur_ask}c, floor={stop_floor_c}c). Exiting cleanly!"
                    )
                    stopped_out_events.add(_entity_key(ticker))
                    if not ticker.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB", "KXINX", "KXNASDAQ", "KXWTI")):
                        stopped_out_matchups.add(_matchup_slug(ticker))
                        educator.memory["stopped_out_matchups"] = list(stopped_out_matchups)
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
                    exit_order_side = "bid" if is_no_pos else "ask"
                    exit_yes_price_c = min(99, max(1, 100 - cur_bid)) if is_no_pos else max(1, cur_bid)
                    await client.request(
                        "POST",
                        "/portfolio/events/orders",
                        json_data={
                            "ticker": ticker,
                            "side": exit_order_side,
                            "count": f"{whole_pos}.00",
                            "price": f"{exit_yes_price_c / 100:.4f}",
                            "time_in_force": "immediate_or_cancel",
                            "self_trade_prevention_type": "maker",
                            "client_order_id": str(uuid.uuid4()),
                        },
                        authenticated=True,
                    )
                    est_pnl = round(
                        ((max_orig_cnt - whole_pos) * 0.06) + (whole_pos * ((cur_bid - avg_entry_cents) / 100.0)),
                        2,
                    )
                    educator.record_trade_autopsy(
                        ticker=ticker,
                        entry_cents=avg_entry_cents,
                        exit_cents=cur_bid,
                        peak_cents=peaked,
                        net_pnl_dlr=est_pnl,
                        exit_reason="STAGE1_PROFIT_SHIELD_EXIT" if stage1_banked else "TIGHT_10C_STOP_LOSS",
                    )
                    tracked_open_positions.pop(ticker, None)
                    educator.save_memory()
                    await send_phone_alert(
                        client,
                        "🛡️ Stage-1 Profit Shield Locked!" if stage1_banked else "🛑 Tight -10c Stop-Loss Triggered",
                        (
                            f"Play: {ticker} ({'UNDER/NO' if is_no_pos else 'YES'})\n"
                            f"Action: Exited {whole_pos}x @ {cur_bid}c (Entry: {avg_entry_cents}c, Floor: {stop_floor_c}c)\n"
                            f"Estimated Net Trade P&L: {est_pnl:+.2f}\n"
                            f"New Account Equity: ${equity_cents/100:.2f}"
                        ),
                    )
                    continue

                # 12-MINUTE STAGNATION SCRATCH RULE + STAGE-2 RUNNER DYNAMIC TAKE-PROFIT RATCHET:
                pos_meta = tracked_open_positions.get(ticker, {})
                pos_age_mins = (time.time() - float(pos_meta.get("entry_ts") or time.time())) / 60.0
                existing_sells = resting_sell_orders.get(ticker, [])
                # A) If position has sat >= 11.5 mins without banking Stage-1 (`not stage1_banked`), and price is near entry,
                #    cancel the +5c/+6c target and re-queue at `avg_entry_cents + 1` (Scratch +1c) so we exit BEFORE a game run!
                if (
                    not stage1_banked
                    and pos_age_mins >= 11.5
                    and not pos_meta.get("scratch_requeued", False)
                    and (avg_entry_cents - 2) <= cur_bid <= (avg_entry_cents + 3)
                ):
                    logger.info(
                        f"⏱️ 12-Min Stagnation Scratch Rule on {ticker} (open {pos_age_mins:.1f}m without Stage-1): "
                        f"Re-pegging resting sell to Scratch+{1}c ({max(cur_bid + 1, avg_entry_cents + 1)}c) to avoid game-run exposure!"
                    )
                    pos_meta["scratch_requeued"] = True
                    if existing_sells:
                        try:
                            await client.request(
                                "DELETE",
                                "/portfolio/events/orders/batched",
                                json_data={
                                    "orders": [
                                        {
                                            "order_id": so["order_id"],
                                            "market_ticker": ticker,
                                            "exchange_index": so.get("exchange_index", 0),
                                            "subaccount": so.get("subaccount_number", 0),
                                        }
                                        for so in existing_sells
                                    ]
                                },
                                authenticated=True,
                            )
                            resting_sell_orders[ticker] = []
                            resting_sell_counts[ticker] = 0.0
                        except Exception:
                            pass
                # B) If Stage-1 IS banked (`stage1_banked`) and position has been open >= 14.0 mins (like SAS16 at 93c/94c),
                #    ratchet Stage-2 from +9c/+10c down to `max(cur_bid + 1, avg_entry_cents + 6)` so we cash out the runner now!
                elif (
                    stage1_banked
                    and pos_age_mins >= 14.0
                    and not pos_meta.get("stage2_ratcheted", False)
                    and cur_bid >= avg_entry_cents + 4
                ):
                    logger.info(
                        f"🎯 Stage-2 Runner Profit Lock on {ticker} (open {pos_age_mins:.1f}m, bid={cur_bid}c): "
                        f"Ratcheting Stage-2 runner from 97c/98c down to {max(cur_bid + 1, avg_entry_cents + 6)}c for fast cash-out!"
                    )
                    pos_meta["stage2_ratcheted"] = True
                    if existing_sells:
                        try:
                            await client.request(
                                "DELETE",
                                "/portfolio/events/orders/batched",
                                json_data={
                                    "orders": [
                                        {
                                            "order_id": so["order_id"],
                                            "market_ticker": ticker,
                                            "exchange_index": so.get("exchange_index", 0),
                                            "subaccount": so.get("subaccount_number", 0),
                                        }
                                        for so in existing_sells
                                    ]
                                },
                                authenticated=True,
                            )
                            resting_sell_orders[ticker] = []
                            resting_sell_counts[ticker] = 0.0
                        except Exception:
                            pass

                # Ensure Strategy A (60/40 Fast-Flip + Wave-Rider Split) & Strategy B (Front-of-Queue Ask Undercut)
                whole_covered = int(resting_sell_counts.get(ticker, 0.0))
                uncovered = whole_pos - whole_covered
                if uncovered >= 1:
                    undercut_ask = max(cur_bid + 1, cur_ask - 1)
                    if pos_meta.get("scratch_requeued", False) and not stage1_banked:
                        fast_tp = max(cur_bid + 1, avg_entry_cents + 1)
                        wave_tp = max(fast_tp + 1, avg_entry_cents + 3)
                    elif pos_meta.get("stage2_ratcheted", False) and stage1_banked:
                        fast_tp = max(cur_bid + 1, min(95, avg_entry_cents + 6))
                        wave_tp = fast_tp
                    elif undercut_ask >= avg_entry_cents + 5:
                        fast_tp = min(95, avg_entry_cents + 6, undercut_ask)
                        wave_tp = max(fast_tp + 3, min(97, avg_entry_cents + 9))
                    else:
                        fast_tp = max(cur_bid + 1, min(94, avg_entry_cents + 5))
                        wave_tp = max(fast_tp + 3, min(97, avg_entry_cents + 9))

                    # Whale Ask-Wall Front-Runner (Reciprocal Orderbook Inspection):
                    # An Ask Wall on our side at price `P` is a Bid Wall on the opposing side at `100 - P`.
                    # If >= $400 of opposing Whale Wall sits right at `fast_tp` or `wave_tp`, undercut it by -1c
                    # so our take-profit sits #1 at the front of the sell queue instead of behind the wall!
                    opp_bids_map = {
                        int(lvl.price): float(lvl.quantity) * (lvl.price / 100.0)
                        for lvl in (ob.yes_bids if is_no_pos else ob.no_bids)
                    }
                    if opp_bids_map.get(100 - fast_tp, 0.0) >= 400.0 and (fast_tp - 1) >= max(cur_bid + 1, avg_entry_cents + 5):
                        fast_tp -= 1
                    if opp_bids_map.get(100 - wave_tp, 0.0) >= 400.0 and (wave_tp - 1) >= (fast_tp + 2):
                        wave_tp -= 1

                    if uncovered >= 4:
                        s1_ratio = 0.60 if pos_conf_score >= 82 else 0.70
                        fast_cnt = max(2, int(round(uncovered * s1_ratio)))
                        wave_cnt = uncovered - fast_cnt
                        legs = [(fast_cnt, fast_tp, f"Stage-1 Fast-Flip (+6c, {int(s1_ratio*100)}%)"), (wave_cnt, wave_tp, "Stage-2 Wave-Rider (+11c Target)")]
                    else:
                        legs = [(uncovered, fast_tp, "Fast-Flip Harvest (+6c Target)")]

                    tp_order_side = "bid" if is_no_pos else "ask"
                    for leg_cnt, leg_cents, leg_label in legs:
                        count_str = f"{leg_cnt}.00"
                        eff_yes_tp_c = max(1, min(99, 100 - leg_cents)) if is_no_pos else leg_cents
                        tp_price = f"{eff_yes_tp_c / 100:.4f}"
                        logger.info(
                            f"Auto-queuing {leg_label} Sell for {ticker} ({'NO-UNDER' if is_no_pos else 'YES'}): {count_str} @ {leg_cents}c (kalshi_yes_p={tp_price})"
                        )
                        try:
                            await client.request(
                                "POST",
                                "/portfolio/events/orders",
                                json_data={
                                    "ticker": ticker,
                                    "side": tp_order_side,
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

            # 2. Dynamic 4-Tier Bankroll Scaling + Daily Smart Bet Quota Pacing
            tier = get_scaling_tier(equity_cents)
            min_stake_c = tier["min_stake_cents"]
            quota_st = educator.get_daily_quota_status()
            min_leader_ask = 76
            max_leader_ask = 90
            logger.info(
                f"Heartbeat [{tier['name']}] | Cash: ${total_cents/100:.2f} | Portfolio: ${port_cents/100:.2f} | Total Equity: ${equity_cents/100:.2f} (Floor=${daily_floor_c/100:.2f}) | Daily Quota: {quota_st['completed_bets']}/{quota_st['target_bets']} ({quota_st['wins']}W-{quota_st['losses']}L, Pace={quota_st['expected_by_now']})"
            )
            # Emit silent Mac Primary Engine pulse every 90s so GitHub Cloud 24/7 Failover knows when Mac is awake vs. shut down
            if sys.platform == "darwin" and duration_seconds == 0:
                now_hb = time.time()
                if now_hb - getattr(educator, "_last_cloud_hb_ts", 0.0) >= 90.0:
                    educator._last_cloud_hb_ts = now_hb
                    try:
                        hc = await client.get_client()
                        await hc.post(
                            "https://ntfy.sh/kalshi-greg-hb-3fc522ce",
                            content=f"mac-online:{equity_cents/100:.2f}".encode("utf-8"),
                            timeout=3.0,
                        )
                    except Exception:
                        pass
            elif duration_seconds > 0:
                now_hb = time.time()
                if now_hb - getattr(educator, "_last_cloud_hb_ts", 0.0) >= 45.0:
                    educator._last_cloud_hb_ts = now_hb
                    try:
                        hc = await client.get_client()
                        r_hb = await hc.get("https://ntfy.sh/kalshi-greg-hb-3fc522ce/json?poll=1&since=120s", timeout=3.0)
                        if r_hb.status_code == 200 and "mac-online" in r_hb.text:
                            logger.info("🟢 Mac Primary Engine came back online! Yielding Cloud Failover loop immediately.")
                            break
                    except Exception:
                        pass

            # Poll 24/7 Two-Way Commands (Telegram Bot & ntfy.sh lock-screen buttons — works even when Mac is OFF!)
            await poll_two_way_commands(client, educator, total_cents, port_cents, equity_cents, positions, open_orders)

            unshielded_open_count = sum(
                1
                for tk_o, tr_o in tracked_open_positions.items()
                if not tr_o.get("stage1_alerted", False) and float(tr_o.get("stake", 0) or 0) >= 15.0
            )

            max_conc = 5 if equity_cents >= 38000 else min(4, tier["max_concurrent"] + 1)
            if (
                not educator.memory.get("manual_pause_buys", False)
                and not cb_state.get("tripped", False)
                and equity_cents > daily_floor_c
                and total_cents >= min_stake_c
                and len(resting_buy_tickers) < max_conc
                and unshielded_open_count < max_conc
            ):
                shard_cents = {0: 0, 1: 0, 2: 0, 3: 0}
                for b_item in bal.get("balance_breakdown", []):
                    idx = b_item.get("exchange_index")
                    if idx in shard_cents:
                        shard_cents[idx] = int(float(b_item.get("balance", "0") or 0) * 100)

                for dst_shard in ((0, 2, 3) if (total_cents >= 26000 and not resting_buy_tickers) else ()):
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
                                            "amount": xfer_c * 100,
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
                harvested_set = set(educator.memory.get("harvested_tickers", []))
                last_win_entry_map = educator.memory.get("last_win_entry_cents", {})
                # STRICT ANTI-DOUBLE-BOOKING, STOPPED-OUT GAME BAN & STAIR-STEP MULTI-SCALP ON THE WAY UP:
                # 1) NEVER hold 2 concurrent positions/orders in the same game at the same time (`active_all_tickers`).
                # 2) If a game EVER stops out/loses (`stopped_out_matchups`), permanently ban that entire game across ALL series forever!
                # 3) If a trade WINS and cashes out, allow rolling into the next Quarter/Period/Under OR re-scalping the same winner
                #    ONLY if it has stepped UP >= +4c higher on the staircase (`side_ask >= last_win_entry + 4`, never on a pullback)!
                active_matchups = (
                    set(stopped_out_matchups)
                    | {_matchup_slug(tk) for tk in active_all_tickers if not tk.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB", "KXINX", "KXNASDAQ", "KXWTI"))}
                )
                active_events = (
                    set(stopped_out_events)
                    | {_entity_key(tk) for tk in active_all_tickers}
                )
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
                now_scan_ts = time.time()
                series_to_poll = []
                for series in WATCH_SERIES:
                    is_crypto_s = series.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                    is_index_s = series.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                    if is_crypto_s and active_crypto_count >= 1:
                        continue
                    if is_index_s and active_index_count >= 1:
                        continue
                    if now_scan_ts < cold_series_until.get(series, 0.0):
                        continue
                    series_to_poll.append(series)

                async def _fetch_series_markets(s_name: str):
                    c_ts, c_mkts = series_markets_cache.get(s_name, (0.0, []))
                    if (now_scan_ts - c_ts) <= 22.0 and c_mkts:
                        return s_name, c_mkts, True, True
                    try:
                        res = await client.request(
                            "GET",
                            "/markets",
                            params={"series_ticker": s_name, "status": "open", "limit": 1000},
                            authenticated=False,
                        )
                        mkts = res.get("markets", [])
                        series_markets_cache[s_name] = (now_scan_ts, mkts)
                        return s_name, mkts, True, False
                    except Exception:
                        if c_mkts:
                            return s_name, c_mkts, True, True
                        return s_name, [], False, True

                batched_series_results = []
                uncached_to_poll = []
                for s_item in series_to_poll:
                    c_ts, c_mkts = series_markets_cache.get(s_item, (0.0, []))
                    if (now_scan_ts - c_ts) <= 22.0 and c_mkts:
                        batched_series_results.append((s_item, c_mkts, True))
                    else:
                        uncached_to_poll.append(s_item)

                for b_idx in range(0, len(uncached_to_poll), 2):
                    batch_slice = uncached_to_poll[b_idx : b_idx + 2]
                    b_res = await asyncio.gather(*(_fetch_series_markets(s) for s in batch_slice))
                    for s_n, m_l, f_ok, _ in b_res:
                        batched_series_results.append((s_n, m_l, f_ok))
                    if b_idx + 2 < len(uncached_to_poll):
                        await asyncio.sleep(0.11)

                for series, markets_list, fetch_ok in batched_series_results:
                    if not fetch_ok:
                        continue
                    is_crypto_s = series.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                    is_index_s = series.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                    try:
                        if markets_list and series not in series_shard_cache:
                            series_shard_cache[series] = int(markets_list[0].get("exchange_index", 0) or 0)
                        has_near_active = False
                        for m in markets_list:
                            shard_key = int(m.get("exchange_index", 0) or 0)

                            t = m.get("ticker", "")
                            if t.endswith("-TIE") or "-B" in t:
                                continue
                            event_prefix = _entity_key(t)
                            mslug = _matchup_slug(t)
                            exp_str = m.get("expected_expiration_time") or m.get("expiration_time") or ""
                            if not exp_str:
                                continue
                            try:
                                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                                mins_to_exp = (exp_dt - now_utc).total_seconds() / 60.0
                            except Exception:
                                continue
                            if -10080.0 <= mins_to_exp <= 185.0:
                                has_near_active = True

                            if (
                                t in resting_buy_tickers
                                or resting_sell_counts.get(t, 0) > 0
                                or event_prefix in active_events
                                or (not is_crypto_s and not is_index_s and mslug in active_matchups)
                            ):
                                continue

                            # Allow -7d to +185m on Live Sports so delayed/multi-day Tennis & active live games are caught while filtering out games hours in the future!
                            if is_crypto_s or is_index_s:
                                if not (5.0 <= mins_to_exp <= 25.0):
                                    continue
                            else:
                                if not (-10080.0 <= mins_to_exp <= 185.0):
                                    continue

                            bid_f = float(m.get("yes_bid_dollars") or 0)
                            ask_f = float(m.get("yes_ask_dollars") or 0)
                            bid_c = int(round(bid_f * 100))
                            ask_c = int(round(ask_f * 100))
                            spread_c = ask_c - bid_c
                            if spread_c <= 0 or spread_c > 3:
                                continue

                            raw_title = m.get("yes_sub_title") or m.get("title", t)
                            # Rule 5: For TOTAL markets, scan Late-Clock UNDERS (`NO` side in 76c-87c zone, i.e., `100 - bid_c` in 76c-87c)
                            # or 100%-already-banked Overs (`YES` side in 76c-87c zone)
                            no_ask_c = 100 - bid_c
                            is_under_candidate = False
                            if "TOTAL" in series and "TEAMTOTAL" not in series and (min_leader_ask <= no_ask_c <= max_leader_ask):
                                is_under_candidate = True
                                clean_title = f"[UNDER-NO] {raw_title}"
                            elif min_leader_ask <= ask_c <= max_leader_ask:
                                clean_title = raw_title
                            else:
                                continue

                            # One-Win-Per-Ticker ("Bank the Win & Walk Away" Rule):
                            # Once we win and cash out `t`, NEVER re-buy that exact same ticker `t` again so we never give back a banked win!
                            if t in harvested_set:
                                continue

                            if now_scan_ts < veto_cooldown_until.get(t, 0.0):
                                continue

                            eff_mins = max(0.0, mins_to_exp)
                            is_q_series = any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P"))
                            is_prop_mkt = len(t.split("-")) >= 4
                            if is_q_series:
                                eff_mins = min(eff_mins, 8.0)
                            elif is_prop_mkt:
                                eff_mins = min(eff_mins, 12.0)
                            elif any(h in series for h in ("1H", "2H")):
                                eff_mins = min(eff_mins, 20.0)
                            quarter_king_bonus = -30.0 if is_q_series else (-25.0 if is_prop_mkt else (-15.0 if is_under_candidate else 0.0))
                            is_one_way_ratchet = is_prop_mkt
                            ratchet_bonus = -12.0 if is_one_way_ratchet else 0.0
                            import re as _re
                            _m_strike = _re.search(r"(\d+(?:\.\d+)?)$", t.split("-")[-1])
                            strike_num = float(_m_strike.group(1)) if _m_strike else 0.0
                            # BUYING POINTS / ALT-LINE SAFETY TIEBREAKER:
                            # - For Late-Clock Unders (`is_under_candidate`): prefer HIGHER strike numbers (e.g. Under 4.5 before Under 3.5 -> buying UP points!).
                            # - For Spreads / Team Totals / Player Props: prefer LOWER strike numbers (e.g. -2.5 before -6.5, or 20+ pts before 25+ pts -> buying DOWN points!).
                            alt_line_safety_tiebreaker = (-strike_num * 0.45) if is_under_candidate else (strike_num * 0.45)
                            # Sweet-Spot Price Bonus: 79c-84c is the optimal risk/reward + fill-velocity zone (-8.0 priority boost!)
                            eff_ask_c = no_ask_c if is_under_candidate else ask_c
                            sweet_spot_bonus = -8.0 if (79 <= eff_ask_c <= 84) else 0.0
                            # Treat 1c and 2c spreads as equal Tier-1 priority (since a 2c spread lets us step +1c to #1 in the queue!):
                            spread_pen = 20.0 if spread_c <= 2 else 55.0
                            vel_score = spread_pen + eff_mins + quarter_king_bonus + ratchet_bonus + alt_line_safety_tiebreaker + sweet_spot_bonus
                            candidates.append((vel_score, mins_to_exp, t, clean_title, shard_key))
                        if not has_near_active and not is_crypto_s and not is_index_s:
                            cold_series_until[series] = time.time() + 240.0
                    except Exception:
                        pass

                candidates.sort(key=lambda x: x[0])
                dedup_candidates = []
                seen_entity_counts: dict[str, int] = {}
                for cand in candidates:
                    ek = _entity_key(cand[2])
                    if seen_entity_counts.get(ek, 0) < 2:
                        seen_entity_counts[ek] = seen_entity_counts.get(ek, 0) + 1
                        dedup_candidates.append(cand)

                for _vel, mins_left, t, title, shard_key in dedup_candidates[:40]:
                    event_prefix = _entity_key(t)
                    matchup_slug = _matchup_slug(t)
                    is_crypto_t = t.startswith(("KXBTC", "KXETH", "KXSOL", "KXXRP", "KXDOGE", "KXBNB"))
                    is_index_t = t.startswith(("KXINX", "KXNASDAQ", "KXWTI"))
                    if (
                        event_prefix in active_events
                        or (not is_crypto_t and not is_index_t and matchup_slug in active_matchups)
                        or total_cents < min_stake_c
                        or unshielded_open_count >= max_conc
                        or len(resting_buy_tickers) >= max(1, max_conc - unshielded_open_count)
                        or (is_crypto_t and active_crypto_count >= 1)
                        or (is_index_t and active_index_count >= 1)
                    ):
                        continue
                    await asyncio.sleep(0.05)
                    ob = await client.get_orderbook(t)
                    is_under_cand = title.startswith("[UNDER-NO]")
                    side_bid = ob.best_no_bid if is_under_cand else ob.best_yes_bid
                    side_ask = ob.best_no_ask if is_under_cand else ob.best_yes_ask
                    if side_bid and side_ask:
                        spread = side_ask - side_bid
                        is_leader_zone = (min_leader_ask <= side_ask <= max_leader_ask and 1 <= spread <= 3)
                        is_tail_lock_zone = False

                        if is_leader_zone:
                            approved, reason, conf_mult = await educator.evaluate_opportunity(
                                client, t, ob, title=title, min_book_depth_dlr=tier["min_depth_dlr"]
                            )
                            logger.info(
                                f"PreTradeEducator [{'76-87c LATE-CLOCK UNDER' if is_under_cand else '76-87c MEAT-OF-MOVE LEADER'} | {t} ({mins_left:.0f}m left)]: {reason}"
                            )
                            if not approved:
                                if any(k in reason for k in ("Period Window", "Under Too Early", "Under Clock Window", "not live in-play", "No live in-play", "Matchup Check", "Unbanked Over")):
                                    veto_cooldown_until[t] = time.time() + 120.0
                                elif any(k in reason for k in ("Stale market", "Zero recent trades", "Thin bid liquidity")):
                                    veto_cooldown_until[t] = time.time() + 45.0
                                elif any(k in reason for k in ("Negative tape drift", "Pop-and-Drop", "Whip-Saw")):
                                    veto_cooldown_until[t] = time.time() + 18.0
                                else:
                                    veto_cooldown_until[t] = time.time() + 7.0
                                continue

                            # Front-of-Queue Whale Bid Placement v2:
                            # On a 1c spread, join `side_bid`. On a 2c spread (`80c/82c`), step +1c (`81c` -> #1 in queue!) whenever:
                            # a) `best_lvl_dlr >= $100` (a real queue is already ahead of us at `side_bid`), OR
                            # b) `opp_best_ask_dlr <= $160` (the opposing ask level is thin and about to be swept upward!), OR
                            # c) `imbalance >= 2.5x` (strong Whale buying pressure pushing the book up).
                            side_top_bids = sorted(ob.no_bids if is_under_cand else ob.yes_bids, key=lambda x: x.price, reverse=True)
                            opp_top_bids = sorted(ob.yes_bids if is_under_cand else ob.no_bids, key=lambda x: x.price, reverse=True)
                            best_lvl_dlr = (float(side_top_bids[0].quantity) * (side_top_bids[0].price / 100.0)) if side_top_bids else 0.0
                            opp_best_ask_dlr = (float(opp_top_bids[0].quantity) * ((100 - opp_top_bids[0].price) / 100.0)) if opp_top_bids else 999.0
                            top5_side_dlr = sum(float(l.quantity) * (l.price / 100.0) for l in side_top_bids[:5])
                            top5_opp_dlr = sum(float(l.quantity) * (l.price / 100.0) for l in opp_top_bids[:5])
                            ob_imb = (top5_side_dlr + 1.0) / (top5_opp_dlr + 1.0)
                            step_on_2c = (best_lvl_dlr >= 100.0 or opp_best_ask_dlr <= 160.0 or ob_imb >= 2.5)
                            bid_cents = (side_bid + 1) if (spread >= 3 or (spread == 2 and step_on_2c)) else side_bid
                            entry_order_side = "ask" if is_under_cand else "bid"
                            eff_yes_entry_c = max(1, min(99, 100 - bid_cents)) if is_under_cand else bid_cents
                            price_str = f"{eff_yes_entry_c / 100:.4f}"
                            # 3-TIER PLAY SIZING + 4-GEAR DAILY STREAK COMPOUNDING:
                            working_equity_cents = min(equity_cents, tier.get("working_cap_cents", equity_cents))
                            streak_stake_c, streak_mode_label, _ = educator.get_streak_sizing(working_equity_cents, bid_cents, ticker=t)
                            target_stake_cents = min(streak_stake_c, total_cents)
                            eff_min_contracts = max(10, int(round((target_stake_cents * 0.72) // max(1, bid_cents))))

                            # Just-In-Time (JIT) Cross-Shard Liquidity Sweeper: pull cash from other shards if needed!
                            if shard_cents.get(shard_key, 0) < target_stake_cents:
                                deficit_cents = target_stake_cents - shard_cents.get(shard_key, 0) + 25
                                for donor in sorted((0, 2, 3), key=lambda s: shard_cents.get(s, 0), reverse=True):
                                    if donor == shard_key or donor in locked_buy_shards or deficit_cents <= 0:
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
                                                    "amount": pull_c * 100,
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
                            if count_int >= eff_min_contracts:
                                max_slice = tier.get("iceberg_slice", 70)
                                slice_count = min(count_int, max_slice)
                                logger.info(
                                    f"Auto-deploying [{streak_mode_label}] Maker {'UNDER(NO)' if is_under_cand else 'YES'} Bid on {t}: {slice_count} contracts @ {bid_cents}c (${(slice_count*bid_cents)/100:.2f})"
                                )
                                try:
                                    await client.request(
                                        "POST",
                                        "/portfolio/events/orders",
                                        json_data={
                                            "ticker": t,
                                            "side": entry_order_side,
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
                                    active_matchups.add(matchup_slug)
                                    locked_buy_shards.add(shard_key)
                                    if is_crypto_t:
                                        active_crypto_count += 1
                                    if is_index_t:
                                        active_index_count += 1
                                    shard_cents[shard_key] -= slice_count * bid_cents
                                    total_cents -= slice_count * bid_cents
                                except Exception:
                                    pass

        except Exception as e:
            logger.error(f"Loop error: {e}")

        if duration_seconds > 0 and (time.time() - start_ts) >= duration_seconds:
            logger.info(f"Completed scheduled cloud burst ({duration_seconds}s).")
            break

        await asyncio.sleep(1.5)

    await client.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=0)
    args = parser.parse_args()
    pid_file = Path("/tmp/kalshi_auto_roller.pid")
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != os.getpid():
                import signal
                os.kill(old_pid, signal.SIGTERM)
                await asyncio.sleep(0.5)
        except Exception:
            pass
    try:
        pid_file.write_text(str(os.getpid()))
    except Exception:
        pass
    await run_autonomous_loop(duration_seconds=args.duration)


if __name__ == "__main__":
    asyncio.run(main())
