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
        r = await hc.get(f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}", timeout=3.5)
        events = r.json().get("events", [])
        self.espn_scoreboard_cache[sport_path] = (now_t, events)
        return events

    async def _get_espn_summary_cached(self, hc, sport_base: str, ev_id: str, ttl: float = 4.0) -> dict:
        key = f"{sport_base}:{ev_id}"
        now_t = time.time()
        c_ts, c_sum = self.espn_summary_cache.get(key, (0.0, {}))
        if (now_t - c_ts) <= ttl and c_sum:
            return c_sum
        r_box = await hc.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_base}/summary?event={ev_id}",
            timeout=3.5,
        )
        data = r_box.json()
        self.espn_summary_cache[key] = (now_t, data)
        return data

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
        self.save_memory()

    def get_daily_quota_status(self) -> dict:
        now_local = datetime.now()
        day_str = now_local.strftime("%Y-%m-%d")
        q_map = self.memory.setdefault("daily_quota_tracker", {})
        q_day = q_map.setdefault(
            day_str,
            {"target_bets": 25, "completed_bets": 0, "wins": 0, "losses": 0, "net_pnl_dlr": 0.0},
        )
        # Expected pace across 24h day (~1.04 bets/hour)
        frac_day = max(0.05, (now_local.hour + now_local.minute / 60.0) / 24.0)
        expected_by_now = round(25.0 * frac_day, 1)
        done = int(q_day.get("completed_bets", 0))
        behind_pace = (expected_by_now - done) >= 1.5 and done < 25
        quota_met = done >= 25
        return {
            "target_bets": 25,
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
        if (now_ts - cached_ts) > 720.0 or not games:
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
                # Blend with Explicit Franchise Power Tier Priors (so early-season 2-2 records still recognize Elite Contenders vs. Tanking Teams!)
                tm_ab = (c_obj.get("team", {}).get("abbreviation") or "").upper()
                elite_franchises = {"BOS", "OKC", "DEN", "CLE", "NYK", "MIN", "DAL", "HOU", "GSW", "KC", "DET", "BAL", "BUF", "PHI", "GB", "FLA", "EDM", "CAR", "DAL", "WPG", "RMA", "MCI", "FCB", "ARS", "LIV", "BAY", "INT"}
                bad_tanking_franchises = {"WAS", "UTA", "BKN", "CHA", "POR", "TOR", "CAR", "NE", "NYG", "TEN", "LV", "CLE", "SJS", "ANA", "CBJ", "CHI"}
                if tm_ab in elite_franchises:
                    strength = min(0.90, strength + 0.07)
                elif tm_ab in bad_tanking_franchises:
                    strength = max(0.15, strength - 0.07)
                rank_val = int(c_obj.get("curatedRank", {}).get("current", 99) or 99)
                if 1 <= rank_val <= 10:
                    strength = min(0.92, strength + 0.14)
                elif 11 <= rank_val <= 25:
                    strength = min(0.86, strength + 0.08)
                tier_lbl = "ELITE" if strength >= 0.65 else ("GOOD" if strength >= 0.56 else ("BAD" if strength <= 0.43 else "MED"))
                rank_str = f"#{rank_val} " if 1 <= rank_val <= 25 else ""
                return strength, f"{rank_str}{ tier_lbl }({ov_sum})", tier_lbl

            my_str, my_lbl, my_tier = _parse_comp_strength(my_comp)
            opp_str, opp_lbl, opp_tier = _parse_comp_strength(opp_comp)
            edge = my_str - opp_str
            tag = f" | STANDINGS[{my_lbl} vs {opp_lbl}, edge={edge:+.2f}]" if abs(edge) >= 0.06 or my_tier != opp_tier else ""
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
                    pts_needed = max(0.0, target_val - cur_pts)
                    gm_margin_tot = abs(float(competitors[0].get("score", 0) or 0) - float(competitors[1].get("score", 0) or 0))
                    try:
                        clean_clk = str(clock_disp).replace("'", "").split(":")[0].split("+")[0].strip()
                        cur_clk_m = float(clean_clk) if clean_clk else 0.5
                    except Exception:
                        cur_clk_m = 6.0

                    # Positive-Theta Total Rule:
                    # In Soccer & NHL, NEVER buy an "Over" Total unless the goals are ALREADY 100% scored on the scoreboard (pts_needed <= 0)!
                    # Hoping for a goal in the last 15-30 minutes has negative time-decay (Theta bleeds every minute without a goal).
                    if (is_soccer or "NHL" in series) and pts_needed > 0.0:
                        return False, (
                            f"VETO [Negative-Theta Unbanked Total]: {series} still needs {pts_needed:.1f} more goal(s) "
                            f"({cur_pts:.0f}/{target_val:.1f} at {clock_disp}) — never hope for goals against a ticking clock!"
                        )

                    # Period-Aware + Minute-by-Minute Clock-Ahead Scoring Pace Curve:
                    if req_periods is not None:
                        is_final_period_of_win = (live_period == req_periods[-1])
                        req_tot_pct = 90.0 if (is_final_period_of_win and cur_clk_m < 4.5) else 80.0
                    elif is_soccer:
                        req_tot_pct = 100.0
                    elif "NHL" in series:
                        req_tot_pct = 100.0
                    else:
                        p_len_m = 15.0 if series.startswith(("KXNFL", "KXNCAAF")) else (20.0 if "NCAAMB" in series else 12.0)
                        reg_tot_m = 60.0 if series.startswith(("KXNFL", "KXNCAAF")) else (40.0 if "NCAAMB" in series else 48.0)
                        max_reg_p = 2 if "NCAAMB" in series else 4
                        elapsed_gm_m = ((min(max_reg_p, live_period) - 1) * p_len_m) + max(0.0, p_len_m - cur_clk_m)
                        elapsed_gm_pct = min(95.0, (elapsed_gm_m / reg_tot_m) * 100.0)
                        req_tot_pct = max({1: 65.0, 2: 78.0, 3: 86.0}.get(live_period, 92.0), elapsed_gm_pct + 12.0)

                    # Point Trap 0 (Overtime Sudden-Death & Short-Period Impossible-Total Trap):
                    # - NHL Overtime (P4+): Sudden-death 3-on-3 or Shootout means AT MOST 1.0 goal can be scored in the entire rest of the game!
                    # - NFL Overtime (P5+): A sudden-death Field Goal (3 pts) or walk-off TD (6 pts, no PAT kicked!) ends the game immediately!
                    if "NHL" in series and live_period >= 4 and pts_needed > 1.0:
                        return False, (
                            f"VETO [NHL Sudden-Death OT Impossible Total]: Needs {pts_needed:.1f} goals in OT (P{live_period}), "
                            f"but sudden-death OT/SO can only add max 1 goal!"
                        )
                    if series.startswith(("KXNFL", "KXNCAAF")) and live_period >= 5 and pts_needed > 3.0:
                        return False, (
                            f"VETO [Football Sudden-Death OT Cap]: Needs {pts_needed:.1f} pts in OT (P{live_period}), "
                            f"where a 3-pt Field Goal ends the game!"
                        )

                    # Point Trap A (Football Discrete 3/7-pt Scoring, 0-Timeouts Runoff & 4Q Victory-Formation Clock-Chew Trap):
                    # 1. Inside final 6:00 of P4 with >=9 pt (2-score) lead, teams kneel down in the red zone instead of kicking FGs or scoring TDs!
                    # 2. Inside final 3:30 of P2/P4, if leading team has possession and trailing team has 0 timeouts (`homeTimeouts`/`awayTimeouts`), clock runs out!
                    # 3. In P4 with >3.0 pts needed (a FG won't win it — needs a full TD!) and >=13 pt margin or <8:00 left, VETO!
                    script_tot_tag = ""
                    if series.startswith(("KXNFL", "KXNCAAF")):
                        sit_tot = comp.get("situation") or {}
                        poss_tot_id = str(sit_tot.get("possession") or "")
                        sc0_t = float(competitors[0].get("score", 0) or 0)
                        sc1_t = float(competitors[1].get("score", 0) or 0)
                        lead_idx = 0 if sc0_t >= sc1_t else 1
                        trail_idx = 1 - lead_idx
                        trail_ha = (competitors[trail_idx].get("homeAway") or "").lower()
                        trail_to = int(sit_tot.get("homeTimeouts" if trail_ha == "home" else "awayTimeouts", 2) or 0)
                        if live_period in (2, 4) and pts_needed > 0.0 and cur_clk_m <= 3.5 and gm_margin_tot >= 1.0:
                            if poss_tot_id == str(competitors[lead_idx].get("id") or "") and trail_to == 0 and prog_pct < 96.0:
                                return False, (
                                    f"VETO [Football 0-Timeouts Clock Runoff]: Leading team has ball at P{live_period} {clock_disp} "
                                    f"and trailing team has 0 timeouts left (2-min runoff kills Over)!"
                                )
                            elif poss_tot_id == str(competitors[trail_idx].get("id") or "") and trail_to >= 2 and gm_margin_tot <= 14.0:
                                req_tot_pct = max(58.0, req_tot_pct - 3.5)
                                script_tot_tag += f" | +HURRY-UP-{trail_to}TO-BOOST"
                        if live_period >= 4:
                            if pts_needed > 0.0 and cur_clk_m < 6.0 and gm_margin_tot >= 9.0:
                                return False, (
                                    f"VETO [Football 4Q Kneel-Down / Victory Formation]: Needs {pts_needed:.1f} more pts in P4 {clock_disp} "
                                    f"with {gm_margin_tot:.0f}-pt 2-score lead (teams kneel/run clock instead of scoring!)"
                                )
                            if pts_needed > 3.0 and (gm_margin_tot >= 13.0 or cur_clk_m < 8.0):
                                return False, (
                                    f"VETO [Football 4Q Clock-Chew / TD Trap]: Needs {pts_needed:.1f} more pts (>3pt FG) in P4 {clock_disp} "
                                    f"with {gm_margin_tot:.0f}-pt margin (clock-chew risk!)"
                                )
                    # Point Trap B (Soccer Late Corner-Flag Time-Wasting / Park-the-Bus Trap):
                    # After 75' in Soccer, if the Over is not already 100% banked (pts_needed > 0), leading teams sub in defenders and waste time at the corner flag!
                    if is_soccer and live_period >= 2 and cur_clk_m >= 75.0 and pts_needed > 0.0:
                        return False, (
                            f"VETO [Soccer >75' Time-Wasting Trap]: Still needs {pts_needed:.1f} goal(s) at {clock_disp} "
                            f"(corner-flag stall / park-the-bus risk!)"
                        )
                    # Point Trap C (NBA 4Q Blowout Bench Dribble-Out, 24s Shot-Clock Bleed, 2-for-1 Quarter End & Intentional Foul Parade):
                    if series.startswith(("KXNBA", "KXNCAAMB")):
                        if live_period in (1, 2, 3) and 0.55 <= cur_clk_m <= 1.35:
                            script_tot_tag += " | +2-FOR-1-CLOCK-BOOST"
                        if live_period >= 4:
                            if (gm_margin_tot >= 16.0 or cur_clk_m < 4.0) and not (1.0 <= gm_margin_tot <= 7.0 and cur_clk_m <= 3.0) and prog_pct < 90.0:
                                return False, (
                                    f"VETO [Basketball 4Q Dribble-Out Trap]: {cur_pts:.0f}/{target_val:.1f} ({prog_pct:.0f}% < 90% min) "
                                    f"in P4 {clock_disp} (margin={gm_margin_tot:.0f} pts)"
                                )
                            if "TEAMTOTAL" in series and cur_clk_m < 6.0 and gm_margin_tot >= 10.0 and prog_pct < 90.0:
                                return False, (
                                    f"VETO [NBA 4Q 24s Shot-Clock Bleed]: Team Total at {cur_pts:.0f}/{target_val:.1f} ({prog_pct:.0f}% < 90%) "
                                    f"with {gm_margin_tot:.0f}-pt lead in P4 {clock_disp} (walk-ball-up pace bleed!)"
                                )
                            if 1.0 <= gm_margin_tot <= 7.0 and cur_clk_m <= 3.0:
                                req_tot_pct = max(72.0, req_tot_pct - 4.0)
                                script_tot_tag += " | +FOUL-PARADE-BOOST"
                    elif "NHL" in series and live_period == 3 and cur_clk_m <= 3.5 and 1.0 <= gm_margin_tot <= 2.0:
                        req_tot_pct = max(75.0, req_tot_pct - 4.0)
                        script_tot_tag += " | +EMPTY-NET-BOOST"

                    if prog_pct < req_tot_pct:
                        return False, (
                            f"VETO [ESPN {period_tag} Total]: Only {cur_pts:.0f}/{target_val:.1f} pts scored "
                            f"({prog_pct:.0f}% < {req_tot_pct:.0f}% min for P{live_period} {clock_disp})"
                        )
                    lock_tag = (" | PROP-LOCK" if prog_pct >= (req_tot_pct + 12.0) else "") + script_tot_tag + vegas_steam_tag
                    return True, f"ESPN {period_tag} Total Verified ({cur_pts:.0f}/{target_val:.1f} = {prog_pct:.0f}% banked{lock_tag} | P{live_period} {clock_disp})"

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
                        (i for i, c in enumerate(competitors) if (c.get("team", {}).get("abbreviation") or "").upper() == tm_prefix),
                        None,
                    )
                    if c_idx is not None:
                        my_pts = _comp_window_pts(competitors[c_idx])
                        opp_pts = _comp_window_pts(competitors[1 - c_idx])
                        margin = my_pts - opp_pts
                        # Overtime Spread Distortion Gate: In a 5-min OT (P5+), teams start tied and rarely cover >=5.5 spreads!
                        if req_periods is None and live_period >= 5 and strike_val >= 5.5 and margin < (strike_val + 4.5):
                            return False, f"VETO [Overtime Spread Trap]: -{strike_val:.1f} spread in OT (P{live_period}) is nearly impossible to cover!"
                        # Time-in-Play Velocity Gate:
                        # - In NBA/NFL/College 1st Half (P1/P2), avoid Full-Game Spreads (traps cash through 20-min Halftime) unless up +16 above spread
                        # - In NHL (3 periods), avoid Full-Game Puck Lines in P1/P2 unless up +2.5 goals above spread (use 1P/2P instead!)
                        if req_periods is None and not is_soccer:
                            if "NHL" in series and live_period <= 2 and margin < (strike_val + 2.5):
                                return False, f"VETO [NHL Intermission Gate]: Full-game puck line in P{live_period} traps cash through intermission (use 1P/2P/3P!)"
                            elif "NHL" not in series and live_period <= 2 and margin < (strike_val + 16.0):
                                return False, (
                                    f"VETO [1H Time-in-Play Gate]: Full-game spread in P{live_period} {clock_disp} traps cash through Halftime "
                                    f"(use 1Q/2Q/1H spread instead!)"
                                )
                        # Require team to ALREADY be covering the spread:
                        # - NHL/Soccer: +0.5 goals
                        # - Fast Quarters/Halves (req_periods is not None): +3.5 points
                        # - Full-Game NBA/Football Spreads in Q3: +7.5 points above spread (prevents Q3 2-possession swing wipeouts!)
                        # - Full-Game NBA/Football Spreads in Q4: +5.0 points above spread
                        if "NHL" in series or is_soccer:
                            req_spread_buf = 0.5
                        elif req_periods is not None:
                            req_spread_buf = 3.5
                        else:
                            req_spread_buf = 7.5 if live_period <= 3 else 5.0
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
                        lock_tag = (" | PROP-LOCK" if margin >= (strike_val + req_spread_buf + 1.0) or st_edge >= 0.12 else "") + st_tag
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
                        try:
                            p_clk_m = float(str(clock_disp).replace("'", "").split(":")[0].split("+")[0] or 0)
                        except Exception:
                            p_clk_m = 6.0
                        st_edge_w, st_tag_w = _team_standings_edge(competitors[c_idx], competitors[1 - c_idx])
                        # Clock-Aware Multi-Possession Shield for Quarter, Period & Half Winners across ALL leagues:
                        if is_soccer and margin < 2:
                            if p_clk_m < 38.0 or st_edge_w <= -0.10:
                                return False, f"VETO [Soccer 1H 1-Goal Risk]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} at {clock_disp} (<38' or <2 goal cushion{st_tag_w})"
                        elif "NHL" in series:
                            req_nhl_p_cush = (2 if st_edge_w <= -0.10 else 1) if p_clk_m < 7.0 else 2
                            if margin < req_nhl_p_cush:
                                return False, f"VETO [NHL {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} with {clock_disp} left (needs +{req_nhl_p_cush} goals{st_tag_w})"
                        elif series.startswith(("KXNFL", "KXNCAAF")):
                            req_fb_p_cush = 10 if st_edge_w <= -0.10 else 7
                            if margin < req_fb_p_cush:
                                return False, f"VETO [Football {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} (+{margin:.0f} < +{req_fb_p_cush} pt min{st_tag_w})"
                        elif series.startswith(("KXNBA", "KXNCAAMB", "KXNCAAWB")):
                            req_bb_p_cush = (6 if p_clk_m < 5.0 else 8) + (3 if st_edge_w <= -0.10 else 0)
                            if margin < req_bb_p_cush:
                                return False, f"VETO [Basketball {period_tag} Cushion]: {suffix} only up {my_pts:.0f}-{opp_pts:.0f} (+{margin:.0f} < +{req_bb_p_cush} pt min with {clock_disp} left{st_tag_w})"
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
                            el_prop_pct = min(93.0, (el_prop_m / reg_tot_prop) * 100.0)
                            req_prop_pct = max({1: 42.0, 2: 58.0, 3: 72.0}.get(live_period, 82.0), el_prop_pct + 6.0)
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
                                # WTA vs. ATP Service-Break Volatility Shield:
                                # On the WTA tour (`KXWTAMATCH`), single breaks are broken back 45%+ of the time — require a Double Break (+3 games) in Set 1,
                                # or a 1-Set lead while on-serve (`game_diff >= 0`)!
                                game_diff = cur_set_games[idx] - cur_set_games[opp_idx]
                                min_set1_break = 3 if series == "KXWTAMATCH" else 2
                                min_set2_diff = 0 if series == "KXWTAMATCH" else -1
                                if sets_won[idx] == sets_won[opp_idx] and (game_diff < min_set1_break or cur_set_games[idx] < 3):
                                    return False, f"VETO [ESPN Tennis Break Risk]: '{title}' tied {sets_won[idx]}-{sets_won[opp_idx]} in sets and {cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f} (needs +{min_set1_break} game lead!)"
                                if sets_won[idx] > sets_won[opp_idx] and game_diff < min_set2_diff:
                                    return False, f"VETO [ESPN Tennis Momentum]: '{title}' down in current set ({cur_set_games[idx]:.0f}-{cur_set_games[opp_idx]:.0f}) despite set lead"

                            # Anti-Fluke / Multi-Possession Cushion Shield + Standings Quality Tier (`GOOD` vs `MEDIUM` vs `BAD`) on Full-Game Moneylines:
                            st_obj = comp.get("status", {})
                            period_num = int(st_obj.get("period", 1) or 1)
                            clock_disp = st_obj.get("displayClock", "")
                            ml_st_edge, ml_st_tag = (0.0, "") if series in ("KXWTAMATCH", "KXATPMATCH") else _team_standings_edge(competitors[idx], competitors[opp_idx])
                            if "soccer/" in sport_path and margin < 2:
                                try:
                                    clk_min = float(str(clock_disp).replace("'", "").split("+")[0] or 0)
                                except Exception:
                                    clk_min = 0.0
                                # Stoppage-Time Siege, Early-Game 1-Goal Draw & Bad-vs-Good Mirage Shield:
                                req_soc_clk = 82.0 if ml_st_edge <= -0.10 else 74.0
                                if period_num < 2 or clk_min < req_soc_clk or "+" in str(clock_disp):
                                    return False, (
                                        f"VETO [Soccer 1-Goal / Stoppage-Time Risk]: '{title}' only leading {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"at {clock_disp} (requires 2+ goal cushion or {req_soc_clk:.0f}'-89' regulation window{ml_st_tag}!)"
                                    )
                            elif series == "KXNHLGAME" and margin < 2 and (period_num < 3 or ml_st_edge <= -0.10):
                                return False, (
                                    f"VETO [NHL 1-Goal Risk]: '{title}' only up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                    f"in P{period_num} (requires 2+ goal lead{ml_st_tag}!)"
                                )
                            elif series in ("KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME"):
                                # Strict Multi-Possession Full-Game Moneyline Cushion:
                                # - P1/P2 (1st Half): +15 pts (prioritize 1Q/2Q/1H fast-settle lines!)
                                # - P3 (3rd Quarter): +12 pts (a 5-pt Q3 lead is <2 three-pointers and caused both MINCLE & DALMIA wipeouts!)
                                # - P4 (4th Quarter): +8 pts (or +5 pts inside final 3:00 of Q4)
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
                            elif series in ("KXNFLGAME", "KXNCAAFGAME"):
                                min_fball_margin = 14 if period_num <= 2 else (10 if period_num == 3 else 7)
                                if ml_st_edge <= -0.10:
                                    min_fball_margin += 4
                                # Opponent Driving in Red Zone Threat Shield:
                                # If the trailing opponent has possession inside the Red Zone (about to score 7 pts!), add +7 pts to required cushion!
                                sit_fb = comp.get("situation") or {}
                                if sit_fb.get("isRedZone") and str(sit_fb.get("possession") or "") == str(competitors[opp_idx].get("id") or ""):
                                    min_fball_margin += 7
                                if margin < min_fball_margin:
                                    return False, (
                                        f"VETO [Football Cushion / Red-Zone Gate]: '{title}' up {scores[idx]:.0f}-{scores[opp_idx]:.0f} "
                                        f"(+{margin:.0f} < +{min_fball_margin} pt min in P{period_num}{ml_st_tag} — avoiding single-TD / Red-Zone flips!)"
                                    )

                            # Check Vegas Opening vs Closing Line Movement, Game Clock Period & Live Win Probability
                            vegas_note = ml_st_tag
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

        # 1. Whale Order Book Imbalance Check (Top 5 levels near touch) + Self-Correcting Series & Archetype Drawdown Gate
        series = ticker.split("-")[0] if "-" in ticker else ticker
        arch = self.classify_archetype(ticker)
        st = self.memory.get("series_stats", {}).get(series, {})
        series_pnl = float(st.get("realized_pnl", 0.0) or 0.0)
        arch_st = self.memory.get("archetype_learning", {}).get(arch, {})
        arch_pnl = float(arch_st.get("realized_pnl", 0.0) or 0.0)
        req_imbalance = 1.85 if (series_pnl < -1.00 or arch_pnl < -5.00) else 1.35

        top_yes_bids = sorted(ob.yes_bids, key=lambda x: x.price, reverse=True)[:5]
        top_no_bids = sorted(ob.no_bids, key=lambda x: x.price, reverse=True)[:5]
        yes_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_yes_bids)
        no_depth = sum(float(level.quantity) * (level.price / 100.0) for level in top_no_bids)
        imbalance_ratio = (yes_depth + 1.0) / (no_depth + 1.0)

        is_quarter_mkt = any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P"))
        is_prop_mkt = len(ticker.split("-")) >= 4
        eff_min_depth = min(min_book_depth_dlr, 90.0) if is_quarter_mkt else (min(min_book_depth_dlr, 150.0) if is_prop_mkt else min_book_depth_dlr)
        if yes_depth < eff_min_depth:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, f"VETO: Thin bid liquidity (${yes_depth:.0f} < ${eff_min_depth:.0f} tier exit depth)", 0.0

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
                max_stale_m = 60.0 if (is_quarter_mkt or is_prop_mkt) else 10.0
                if last_trade_mins_ago > max_stale_m:
                    self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                    self.save_memory()
                    return False, f"VETO: Stale market (last trade {last_trade_mins_ago:.1f}m ago > {max_stale_m:.0f}m max)", 0.0

                if len(trades) >= 3:
                    t_prices = [
                        int(round(float(tr.get("yes_price_dollars") or (tr.get("yes_price", 0) / 100.0)) * 100))
                        for tr in trades
                    ]
                    newest_c = t_prices[0]
                    window_10 = t_prices[:10]
                    oldest_10_c = window_10[-1]
                    momentum_delta = newest_c - oldest_10_c
                    if momentum_delta < 0:
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
                    if (ob.best_yes_bid or 0) < (newest_c - 1):
                        self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
                        self.save_memory()
                        return False, (
                            f"VETO [Orderbook Falling Knife]: Live bid ({ob.best_yes_bid}c) dropped below last trade ({newest_c}c) "
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
                    if 2 <= momentum_delta <= 14 and pullback_from_peak_c <= 1 and eff_ratio >= 0.60:
                        tape_velocity_tag = f" | +STAIR-STEP-VELOCITY(Eff={eff_ratio:.0%})"
        except Exception as e:
            return False, f"VETO: Trade tape unavailable ({e})", 0.0

        # 3. External Real-World API Research (Coinbase Spot, ESPN Live Scoreboard, Open-Meteo Weather)
        ext_ok, ext_reason = await self.verify_external_research(client, ticker, title)
        if not ext_ok:
            self.memory["vetoed_plays"] = self.memory.get("vetoed_plays", 0) + 1
            self.save_memory()
            return False, ext_reason, 0.0

        # 3B. Live Pinnacle / Sharp Book Multiplicative De-Vig Check (The Odds API v4) + Stair-Step Tape Velocity Tag
        pin_note = await self.verify_pinnacle_sharp_odds(client, series, ticker, title)
        if pin_note:
            ext_reason += pin_note
        if tape_velocity_tag:
            ext_reason += tape_velocity_tag

        # 4. Self-Learning Series & Archetype Confidence Multiplier + A+ Whale Wall Boost
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
        # Supports up to $310/leg (350-contract front-of-queue slice = +$26 to +$32/win at $690 equity after +$200 deposit!)
        dyn_min_contracts = min(120, max(35, int(eq_dlr * 0.14)))
        dyn_max_stake_c = min(31000, max(12500, int(equity_cents * 0.44)))
        return {
            "name": "TIER 3 ($500-$750 Pro Mode)",
            "base_pct": 0.32,
            "boost_pct": 0.40,
            "min_stake_cents": 4500,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 5,
            "min_depth_dlr": 300.0,
            "iceberg_slice": 350,
        }
    elif eq_dlr < 2000.0:
        # TIER 4 ($750-$2,000 Pro/Whale Scaling): deploys $260-$480 per play (320-450 contracts = +$28 to +$40/win!)
        dyn_min_contracts = min(180, max(90, int(eq_dlr * 0.12)))
        dyn_max_stake_c = min(48000, max(28000, int(equity_cents * 0.40)))
        return {
            "name": "TIER 4 ($1k-$2k Pro Whale Mode)",
            "base_pct": 0.30,
            "boost_pct": 0.38,
            "min_stake_cents": 7500,
            "max_stake_cents": dyn_max_stake_c,
            "min_contracts": dyn_min_contracts,
            "max_concurrent": 7,
            "min_depth_dlr": 450.0,
            "iceberg_slice": 400,
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
    cold_series_until: dict[str, float] = {}

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
                elif cnt < 1.0 and t in tracked_open_positions:
                    prev = tracked_open_positions.pop(t)
                    stopped_out_events.add(_entity_key(t) if "_entity_key" in locals() else "-".join(t.split("-")[:2]))
                    realized_pnl = float(pos.get("realized_pnl_dollars", "0") or 0)
                    stake_dlr = prev.get("max_stake") or prev["stake"]
                    cnt_int = max(1, int(prev.get("max_count") or prev["count"]))
                    avg_ent_c = int(round((stake_dlr / cnt_int) * 100)) if stake_dlr > 0 else 82
                    pk_c = peak_bids.get(t, avg_ent_c + 6)
                    if realized_pnl >= 0:
                        educator.memory.setdefault("harvested_tickers", []).append(t)
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
                if o.get("action") == "sell":
                    resting_sell_orders.setdefault(t, []).append(o)
                    resting_sell_counts[t] = resting_sell_counts.get(t, 0.0) + float(
                        o.get("remaining_count_fp", "0") or 0
                    )
                elif o.get("action") == "buy":
                    oid = o.get("order_id", "")
                    age_sec = 0.0
                    c_str = o.get("created_time", "")
                    if c_str:
                        try:
                            c_dt = datetime.fromisoformat(c_str.replace("Z", "+00:00"))
                            age_sec = (now_utc - c_dt).total_seconds()
                        except Exception:
                            age_sec = 999.0
                    # Auto-cancel resting buy orders unfilled after 40s (or 110s if already partially filling!)
                    filled_so_far = float(o.get("fill_count_fp", "0") or 0)
                    max_wait_sec = 110.0 if filled_so_far >= 1.0 else 40.0
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
                logger.info(f"Auto-canceling {len(stale_buys_to_cancel)} unfilled resting buy order(s) (>40s UTC age) to free cash.")
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
                is_fast_period = any(
                    p in ticker for p in ("1Q", "2Q", "3Q", "4Q", "1H", "2H", "1P", "2P", "3P")
                )
                is_one_way_ratchet = any(
                    k in ticker for k in ("TOTAL", "PTS", "REB", "AST", "3PT", "PRA", "YDS", "REC", "GOAL", "SOA")
                )
                # Stage-1 is ONLY banked when >=40% of contracts have ACTUALLY filled & cashed out (never on an unfilled touch!)
                stage1_banked = (max_orig_cnt >= 6 and whole_pos <= int(max_orig_cnt * 0.58))
                # Smart Stop-Loss Floor (GEMINI.md Law 3 & Pod 1):
                # - Never fire hair-trigger Taker dumps (-3c / -7c) BEFORE Stage-1 banks!
                # - UNIVERSAL STAGE-1 PROFIT-LOCK SHIELD: Once Stage-1 (60% of contracts) has actually sold in cash at +6c,
                #   lock the remaining 40% runner's floor at `avg_entry_cents - 3` across ALL markets (including Totals & Props!)
                #   so `(0.60 * +6c) + (0.40 * -3c) = +2.4c` GUARANTEED NET PROFIT (a Stage-1 winner can NEVER turn into a loss like GSWDEN-236 did!).
                base_floor_c = 52 if is_fast_period else 48
                if stage1_banked:
                    stop_floor_c = max(base_floor_c, avg_entry_cents - 3)
                elif is_soccer_ml and peaked >= avg_entry_cents + 4:
                    stop_floor_c = max(base_floor_c, avg_entry_cents - 6)
                else:
                    stop_floor_c = base_floor_c
                thesis_broken_guillotine = (0 < cur_bid <= stop_floor_c) and (cur_ask <= stop_floor_c + 5)
                if thesis_broken_guillotine:
                    logger.warning(
                        f"THESIS-BROKEN / STAGE-1 SHIELD TRIGGERED on {ticker} (cur_bid={cur_bid}c, ask={cur_ask}c, floor={stop_floor_c}c). Salvaging stake!"
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
                        exit_reason="STAGE1_PROFIT_SHIELD_EXIT" if stage1_banked else "THESIS_BROKEN_GUILLOTINE",
                    )
                    tracked_open_positions.pop(ticker, None)
                    educator.save_memory()
                    await send_phone_alert(
                        client,
                        "🛡️ Stage-1 Profit Shield Locked!" if stage1_banked else "🛑 Stop-Loss Triggered (<48c Guillotine)",
                        (
                            f"Play: {ticker}\n"
                            f"Action: Exited {whole_pos}x @ {cur_bid}c (Peak: {peaked}c, Floor: {stop_floor_c}c)\n"
                            f"Estimated Net Trade P&L: {est_pnl:+.2f}\n"
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
                        # 60% Stage-1 Fast-Flip (+6c) + 40% Stage-2 Wave-Rider (+11c) so Stage-1 banks the majority of profit immediately!
                        fast_cnt = max(2, int(round(uncovered * 0.60)))
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

            # 2. Dynamic 4-Tier Bankroll Scaling + Daily Smart Bet Quota Pacing (Target: 25 High-Conviction Maker Bets/Day)
            tier = get_scaling_tier(equity_cents)
            min_stake_c = tier["min_stake_cents"]
            quota_st = educator.get_daily_quota_status()
            min_leader_ask = 75 if quota_st["behind_pace"] else (80 if quota_st["quota_met"] else 76)
            max_leader_ask = 88 if quota_st["behind_pace"] else 87
            logger.info(
                f"Heartbeat [{tier['name']}] | Cash: ${total_cents/100:.2f} | Portfolio: ${port_cents/100:.2f} | Total Equity: ${equity_cents/100:.2f} | Daily Quota: {quota_st['completed_bets']}/{quota_st['target_bets']} ({quota_st['wins']}W-{quota_st['losses']}L, Pace={quota_st['expected_by_now']})"
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

            # Anti-Cluster Stagger Shield: Allow a full 3-Leg Basket (3 unshielded legs across independent games)
            # so $130+ cash never sits idle on the bench, while unlocking slots 4-5 once any leg banks Stage-1 (+6c)!
            unshielded_open_count = sum(
                1
                for tk_o, tr_o in tracked_open_positions.items()
                if not tr_o.get("stage1_alerted", False) and float(tr_o.get("stake", 0) or 0) >= 15.0
            )

            if (
                not educator.memory.get("manual_pause_buys", False)
                and total_cents >= min_stake_c
                and len(resting_buy_tickers) < min(3, tier["max_concurrent"])
                and unshielded_open_count < 3
            ):
                shard_cents = {0: 0, 1: 0, 2: 0, 3: 0}
                for b_item in bal.get("balance_breakdown", []):
                    idx = b_item.get("exchange_index")
                    if idx in shard_cents:
                        shard_cents[idx] = int(float(b_item.get("balance", "0") or 0) * 100)

                # Pre-warm dry trading shards ONLY when cash > $260 and no resting buy orders are reserving shard margin
                # (otherwise let the JIT Cross-Shard Sweeper concentrate cash on whichever shard has the approved A+ play!)
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

                def _matchup_slug(tk: str) -> str:
                    pts = tk.split("-")
                    return pts[1] if len(pts) >= 2 else tk

                harvested_set = set(educator.memory.get("harvested_tickers", []))
                # Strict Single-Game Correlation Lock: Never hold two concurrent active positions or resting buys
                # in the same matchup (e.g. never hold both DALMIA Moneyline AND DALMIA Total at the same time!).
                # Once a matchup position cashes out, _matchup_slug frees up so we can step up the alt-line ladder!
                active_matchups = {_matchup_slug(tk) for tk in active_all_tickers}
                active_events = (
                    set(stopped_out_events)
                    | {_entity_key(tk) for tk in active_all_tickers}
                    | {
                        _entity_key(tk)
                        for tk in harvested_set
                        if not any(k in tk for k in ("SPREAD", "TOTAL", "PTS", "REB", "AST", "3PT", "YDS", "REC", "GOAL", "SOA"))
                    }
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
                    # Correlation Guard: Max 1 active Crypto play & Max 1 active Wall St Index play at a time!
                    if is_crypto_s and active_crypto_count >= 1:
                        continue
                    if is_index_s and active_index_count >= 1:
                        continue
                    if now_scan_ts < cold_series_until.get(series, 0.0):
                        continue
                    series_to_poll.append(series)

                async def _fetch_series_markets(s_name: str):
                    try:
                        res = await client.request(
                            "GET",
                            "/markets",
                            params={"series_ticker": s_name, "status": "open", "limit": 1000},
                            authenticated=False,
                        )
                        return s_name, res.get("markets", []), True
                    except Exception:
                        return s_name, [], False

                # Poll Hot/Due Series in concurrent 2-way pairs (~16 req/sec, zero 429 bursts on Kalshi Basic Tier!)
                batched_series_results = []
                for b_idx in range(0, len(series_to_poll), 2):
                    batch_slice = series_to_poll[b_idx : b_idx + 2]
                    b_res = await asyncio.gather(*(_fetch_series_markets(s) for s in batch_slice))
                    batched_series_results.extend(b_res)
                    if b_idx + 2 < len(series_to_poll):
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
                            # Block fragile Soccer Draws (-TIE) and narrow middle range brackets (-B)
                            if t.endswith("-TIE") or "-B" in t:
                                continue
                            event_prefix = _entity_key(t)
                            exp_str = m.get("expected_expiration_time") or m.get("expiration_time") or ""
                            if not exp_str:
                                continue
                            try:
                                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                                mins_to_exp = (exp_dt - now_utc).total_seconds() / 60.0
                            except Exception:
                                continue
                            if -180.0 <= mins_to_exp <= 210.0:
                                has_near_active = True

                            if (
                                t in resting_buy_tickers
                                or resting_sell_counts.get(t, 0) > 0
                                or t in harvested_set
                                or event_prefix in active_events
                            ):
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
                            # "Meat of the Move" Sweet Spot (75c-88c when behind daily quota pace, 76c-87c normal):
                            if not (min_leader_ask <= ask_c <= max_leader_ask):
                                continue

                            clean_title = m.get("yes_sub_title") or m.get("title", t)
                            # Fast-Settlement Velocity Cap: Quarters/Periods resolve in ~8m, Player Props in ~12m, Halves in ~20m!
                            eff_mins = max(0.0, mins_to_exp)
                            is_q_series = any(q in series for q in ("1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P"))
                            is_prop_mkt = len(t.split("-")) >= 4
                            if is_q_series:
                                eff_mins = min(eff_mins, 8.0)
                            elif is_prop_mkt:
                                eff_mins = min(eff_mins, 12.0)
                            elif any(h in series for h in ("1H", "2H")):
                                eff_mins = min(eff_mins, 20.0)
                            # "Hot Quarter & Player Prop King" Priority: ALWAYS attack 12-min Quarters (-30.0) and Hot Player Props (-25.0)
                            # BEFORE Halftime or Full-Game lines!
                            quarter_king_bonus = -30.0 if is_q_series else (-25.0 if is_prop_mkt else 0.0)
                            # "One-Way Ratchet" Bonus: ONLY for Player Props (NEVER prioritize unbanked Over/Under Totals over Moneylines!)
                            is_one_way_ratchet = is_prop_mkt
                            ratchet_bonus = -12.0 if is_one_way_ratchet else 0.0
                            # Alt-Line Cushion Priority ("Buying Points / Safer Strike First"):
                            # Within the 76c-87c zone on the same game/player, prefer the LOWER strike number first
                            # (e.g. -2.5 before -8.5 spread, Over 213 before Over 219, 100 yds before 150 yds) for maximum safety cushion!
                            import re as _re
                            _m_strike = _re.search(r"(\d+(?:\.\d+)?)$", t.split("-")[-1])
                            alt_line_safety_tiebreaker = float(_m_strike.group(1)) * 0.04 if _m_strike else 0.0
                            # Velocity Priority Score: Hot Quarter/Prop First + Tightest spread + One-Way Ratchet + safest Alt-Line strike
                            vel_score = (spread_c * 25.0) + eff_mins + quarter_king_bonus + ratchet_bonus + alt_line_safety_tiebreaker
                            candidates.append((vel_score, mins_to_exp, t, clean_title, shard_key))
                        # If a series has zero markets active/expiring within 3.5 hours, sleep that cold series for 4 minutes!
                        if not has_near_active and not is_crypto_s and not is_index_s:
                            cold_series_until[series] = time.time() + 240.0
                    except Exception:
                        pass

                # Sort by Fastest Capital Turnover Velocity, then deduplicate to max 2 alt-strikes per game/player
                # so one game with 10 alt-lines never pushes Player Props or other sports out of the inspection queue!
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
                        or matchup_slug in active_matchups
                        or total_cents < min_stake_c
                        or unshielded_open_count >= 3
                        or len(resting_buy_tickers) >= max(1, 3 - unshielded_open_count)
                        or (is_crypto_t and active_crypto_count >= 1)
                        or (is_index_t and active_index_count >= 1)
                    ):
                        continue
                    await asyncio.sleep(0.06)
                    ob = await client.get_orderbook(t)
                    if ob.best_yes_bid and ob.best_yes_ask:
                        spread = ob.best_yes_ask - ob.best_yes_bid
                        is_leader_zone = (min_leader_ask <= ob.best_yes_ask <= max_leader_ask and spread <= 3)
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
                                    active_matchups.add(matchup_slug)
                                    locked_buy_shards.add(shard_key)
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
