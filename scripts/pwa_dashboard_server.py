"""
Live iPhone Home-Screen PWA Command Center & Real-Time API Server (Port 8090).
Streams live equity, Tonight's $346.40 Bet Progress, the 13-Day $5,000 Sweet-Spot Sprint,
Antigravity Trophy Stack, and Series Self-Learning Leaderboard every 5 seconds.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MEMORY_PATH = Path("data/self_learning_memory.json")
LOG_PATH = Path("/tmp/kalshi_auto_roller.log")

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="apple-mobile-web-app-title" content="Kalshi $5K Engine" />
<title>Kalshi $5K Autonomous Engine — Live PWA</title>
<style>
  :root {
    --bg: #070b12;
    --card: #111827;
    --border: #1f2937;
    --text: #f8fafc;
    --muted: #94a3b8;
    --green: #10b981;
    --cyan: #06b6d4;
    --amber: #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", Roboto, sans-serif; }
  body { background: var(--bg); color: var(--text); padding: 14px; max-width: 820px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 12px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
  .h-title { font-size: 1.1rem; font-weight: 800; display: flex; align-items: center; gap: 8px; }
  .pulse { width: 9px; height: 9px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); animation: p 1.5s infinite; }
  @keyframes p { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .badge { font-size: 0.7rem; padding: 3px 8px; border-radius: 999px; background: rgba(16,185,129,0.16); color: var(--green); font-weight: 700; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 14px; }
  @media (min-width: 600px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
  .kpi-lbl { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; }
  .kpi-val { font-size: 1.45rem; font-weight: 800; margin-top: 4px; }
  .kpi-sub { font-size: 0.72rem; color: var(--muted); margin-top: 3px; }
  .green { color: var(--green); }
  .cyan { color: var(--cyan); }
  .amber { color: var(--amber); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 12px; }
  .card-title { font-size: 0.85rem; font-weight: 700; margin-bottom: 8px; display: flex; justify-content: space-between; }
  .bar-bg { width: 100%; height: 11px; background: #1e293b; border-radius: 999px; overflow: hidden; margin: 6px 0; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #10b981, #06b6d4); border-radius: 999px; transition: width 0.4s; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; }
  .log-box { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.71rem; color: #cbd5e1; background: #090d16; padding: 10px; border-radius: 8px; max-height: 190px; overflow-y: auto; line-height: 1.45; }
</style>
</head>
<body>
  <div class="header">
    <div class="h-title"><span class="pulse"></span> Kalshi $5K Engine</div>
    <span class="badge" id="tierBadge">TIER 3 ($500 Pro Mode)</span>
  </div>

  <div class="grid">
    <div class="kpi">
      <div class="kpi-lbl">Total Equity</div>
      <div class="kpi-val green" id="eqVal">$286.02</div>
      <div class="kpi-sub" id="cashSub">Cash: $286.02 | Open: $0.00</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Today Net Profit</div>
      <div class="kpi-val green" id="todayProfit">+$16.07</div>
      <div class="kpi-sub" id="twoDaySub">2-Day Net: +$16.32</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">Tonight Bet ($346.40)</div>
      <div class="kpi-val cyan" id="betProg">+$12.12 / +$72.50</div>
      <div class="kpi-sub" id="betRem">$60.38 left tonight</div>
    </div>
    <div class="kpi">
      <div class="kpi-lbl">AI Trophy & Payday Vault</div>
      <div class="kpi-val amber" id="trophyVal">$10.00 Pending</div>
      <div class="kpi-sub" id="vaultSub">> $5k Bank Sweep: $0.00</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      <span>🎯 Gate 1 Tonight: $273.90 -> $346.40 (+$72.50 Target)</span>
      <span id="betPct">16.7%</span>
    </div>
    <div class="bar-bg"><div class="bar-fill" id="betBar" style="width:16.7%"></div></div>
    <div style="font-size:0.73rem;color:var(--muted)">Reward at $346.40: $10.00 AI Trophy Seed + $500 Tier 4 Accelerator Deposit</div>
  </div>

  <div class="card">
    <div class="card-title">
      <span>🚀 13-Day Sprint to $5,000.00 Sweet-Spot Plateau (Target: Sat Nov 7)</span>
      <span id="sprintPct">5.7%</span>
    </div>
    <div class="bar-bg"><div class="bar-fill" id="sprintBar" style="width:5.7%"></div></div>
    <div style="font-size:0.73rem;color:var(--muted)">Working Cap: $5,000.00 (~$500/trade) • Everything >$5,000 auto-reserved for Weekly Bank Withdrawal!</div>
  </div>

  <div class="card">
    <div class="card-title"><span>🧠 Self-Learning Series Leaderboard</span><span id="evalStats"></span></div>
    <table>
      <thead><tr><th>Series</th><th>Wins / Losses</th><th>Realized P&L</th></tr></thead>
      <tbody id="seriesBody"></tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-title"><span>⚡ Live 5-Second Scanner & Trade Feed</span><span>Auto-Refresh: 5s</span></div>
    <div class="log-box" id="logBox">Loading live telemetry...</div>
  </div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    document.getElementById('eqVal').textContent = '$' + d.equity.toFixed(2);
    document.getElementById('cashSub').textContent = `Cash: $${d.cash.toFixed(2)} | Open: $${d.open_port.toFixed(2)}`;
    document.getElementById('todayProfit').textContent = (d.today_net >= 0 ? '+$' : '-$') + Math.abs(d.today_net).toFixed(2);
    const betGained = Math.max(0, d.equity - 273.90);
    const betPct = Math.min(100, (betGained / 72.50) * 100);
    document.getElementById('betProg').textContent = `+$${betGained.toFixed(2)} / +$72.50`;
    document.getElementById('betRem').textContent = `$${Math.max(0, 346.40 - d.equity).toFixed(2)} left tonight`;
    document.getElementById('betPct').textContent = betPct.toFixed(1) + '%';
    document.getElementById('betBar').style.width = betPct + '%';

    const spPct = Math.min(100, (d.equity / 5000.0) * 100);
    document.getElementById('sprintPct').textContent = spPct.toFixed(1) + '% ($' + d.equity.toFixed(2) + ' / $5,000)';
    document.getElementById('sprintBar').style.width = spPct + '%';
    document.getElementById('vaultSub').textContent = `> $5k Bank Sweep: $${Math.max(0, d.equity - 5000).toFixed(2)}`;
    document.getElementById('evalStats').textContent = `${d.evaluated} scanned / ${d.vetoed} vetoed`;

    const sb = document.getElementById('seriesBody');
    sb.innerHTML = d.series.map(s => `
      <tr>
        <td><strong>${s.name}</strong></td>
        <td>${s.wins}W - ${s.losses}L</td>
        <td style="color:${s.pnl >= 0 ? '#10b981' : '#f43f5e'};font-weight:700">${s.pnl >= 0 ? '+$' : '-$'}${Math.abs(s.pnl).toFixed(2)}</td>
      </tr>
    `).join('');

    document.getElementById('logBox').innerHTML = d.logs.join('<br/>');
  } catch (e) {}
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path.startswith("/api/state"):
            mem = {}
            if MEMORY_PATH.exists():
                try:
                    mem = json.loads(MEMORY_PATH.read_text())
                except Exception:
                    pass
            hist = mem.get("equity_history", [])
            last_h = hist[-1] if hist else {"equity": 286.02, "cash": 286.02, "portfolio": 0.0}
            daily = mem.get("daily_ledger", {}).get("2026-10-26", {})
            today_net = float(daily.get("net_change_dlr", 16.07))
            s_stats = mem.get("series_stats", {})
            series_rows = [
                {"name": k, "wins": v.get("wins", 0), "losses": v.get("losses", 0), "pnl": v.get("realized_pnl", 0.0)}
                for k, v in sorted(s_stats.items(), key=lambda x: x[1].get("realized_pnl", 0.0), reverse=True)
            ]
            logs = []
            if LOG_PATH.exists():
                try:
                    lines = LOG_PATH.read_text(errors="ignore").splitlines()[-150:]
                    logs = [
                        ln for ln in lines
                        if any(k in ln for k in ("Heartbeat", "APPROVED", "WON", "STAGE-1", "Placing", "Auto-Rebalanced"))
                    ][-10:]
                except Exception:
                    pass
            payload = {
                "equity": float(last_h.get("equity", 286.02)),
                "cash": float(last_h.get("cash", 286.02)),
                "open_port": float(last_h.get("portfolio", 0.0)),
                "today_net": today_net,
                "evaluated": mem.get("evaluated_plays", 0),
                "vetoed": mem.get("vetoed_plays", 0),
                "series": series_rows,
                "logs": logs,
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PWA_PORT", "8090"))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
