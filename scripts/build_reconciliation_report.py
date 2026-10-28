import json
from pathlib import Path
from collections import defaultdict

ev_dir = Path("audit_evidence_20261028")
fills = json.loads((ev_dir / "exchange_fills_all.json").read_text())["fills"]
orders = json.loads((ev_dir / "exchange_orders_all.json").read_text())["orders"]
settlements = json.loads((ev_dir / "exchange_settlements.json").read_text())["settlements"]
bal = json.loads((ev_dir / "exchange_balance.json").read_text())
mem = json.loads((ev_dir / "self_learning_memory.json").read_text())

CUTOFF = "2026-10-26T00:00:00Z"
recent_fills = [f for f in fills if f.get("created_time", "") >= CUTOFF]
recent_orders = [o for o in orders if o.get("created_time", "") >= CUTOFF]
recent_settlements = [s for s in settlements if s.get("settled_time", "") >= CUTOFF]

dedup_fills = {f["fill_id"]: f for f in recent_fills}
recent_fills = sorted(dedup_fills.values(), key=lambda x: x["created_time"])
settlement_by_ticker = {s["ticker"]: s for s in recent_settlements}
autopsy_by_ticker = defaultdict(list)
for a in mem.get("trade_autopsy_log", []):
    autopsy_by_ticker[a["ticker"]].append(a)

by_ticker = defaultdict(list)
for f in recent_fills:
    by_ticker[f["ticker"]].append(f)

# Also check if any settlement in recent_settlements had fills before CUTOFF or vice versa
all_settlement_map = {s["ticker"]: s for s in settlements}

rows = []
tot_maker_fees = 0.0
tot_taker_fees = 0.0
tot_net_pnl = 0.0
tot_hold_pnl = 0.0
wins = 0
losses = 0
win_dollars = 0.0
loss_dollars = 0.0
stopped_out_winners = []

for ticker, t_fills in sorted(by_ticker.items(), key=lambda kv: kv[1][0]["created_time"]):
    st = all_settlement_map.get(ticker, {})
    yes_qty = 0.0
    yes_cost = 0.0
    no_qty = 0.0
    no_cost = 0.0
    maker_f = 0.0
    taker_f = 0.0
    entry_side = None
    taker_exit_orders = set()

    for i, f in enumerate(t_fills):
        cnt = float(f.get("count_fp") or 0)
        yp = float(f.get("yes_price_dollars") or 0)
        np = float(f.get("no_price_dollars") or 0)
        fc = float(f.get("fee_cost") or 0)
        is_t = bool(f.get("is_taker"))
        if is_t:
            taker_f += fc
        else:
            maker_f += fc
        o_side = f.get("outcome_side") or f.get("side")
        if i == 0:
            entry_side = o_side
        if o_side == "yes":
            yes_qty += cnt
            yes_cost += cnt * yp
        elif o_side == "no":
            no_qty += cnt
            no_cost += cnt * np
        if i > 0 and o_side != entry_side and is_t:
            taker_exit_orders.add(f.get("order_id"))

    paired = min(yes_qty, no_qty)
    unp_yes = max(0.0, yes_qty - paired)
    unp_no = max(0.0, no_qty - paired)
    mkt_res = st.get("market_result", "unsettled")

    settle_payout = 0.0
    if mkt_res == "yes":
        settle_payout = unp_yes * 1.0
    elif mkt_res == "no":
        settle_payout = unp_no * 1.0

    gross_payout = paired * 1.0 + settle_payout
    tot_cost = yes_cost + no_cost
    fees = maker_f + taker_f
    net_pnl = round(gross_payout - tot_cost - fees, 2)

    eqty = yes_qty if entry_side == "yes" else no_qty
    ecost = yes_cost if entry_side == "yes" else no_cost
    xqty = no_qty if entry_side == "yes" else yes_qty
    xcost = no_cost if entry_side == "yes" else yes_cost
    eavg_c = round((ecost / eqty) * 100, 1) if eqty > 0 else 0.0
    xavg_c = round(100.0 - ((xcost / xqty) * 100.0), 1) if xqty > 0 else 0.0

    hold_payout = eqty * 1.0 if mkt_res == entry_side else 0.0
    hold_pnl = round(hold_payout - ecost - maker_f, 2)

    is_stopped_winner = (net_pnl < -0.50 and mkt_res == entry_side)
    if is_stopped_winner:
        stopped_out_winners.append((ticker, net_pnl, hold_pnl, round(hold_pnl - net_pnl, 2), eavg_c, xavg_c, eqty, round(fees, 2)))

    if net_pnl >= 0:
        wins += 1
        win_dollars += net_pnl
    else:
        losses += 1
        loss_dollars += net_pnl

    tot_maker_fees += maker_f
    tot_taker_fees += taker_f
    tot_net_pnl += net_pnl
    tot_hold_pnl += hold_pnl

    auts = autopsy_by_ticker.get(ticker, [])
    aut_pnl = round(sum(float(a.get("net_pnl_dlr", 0)) for a in auts), 2)

    rows.append({
        "idx": len(rows) + 1,
        "first_fill_utc": t_fills[0]["created_time"][:19] + "Z",
        "last_fill_utc": t_fills[-1]["created_time"][:19] + "Z",
        "ticker": ticker,
        "entry_side": entry_side.upper(),
        "fills": len(t_fills),
        "ioc_exit_orders": len(taker_exit_orders),
        "entry_qty": round(eqty, 2),
        "entry_avg_c": eavg_c,
        "exit_qty": round(xqty, 2),
        "exit_avg_c": xavg_c,
        "unpaired_settle_qty": round(eqty - paired, 2),
        "market_result": mkt_res.upper(),
        "maker_fee": round(maker_f, 2),
        "taker_fee": round(taker_f, 2),
        "total_fee": round(fees, 2),
        "true_net_pnl": net_pnl,
        "hold_to_settle_pnl": hold_pnl,
        "autopsy_records": len(auts),
        "autopsy_logged_pnl": aut_pnl,
        "autopsy_error": round(aut_pnl - net_pnl, 2),
        "stopped_out_winner": is_stopped_winner,
    })

report_lines = []
report_lines.append("# KALSHI EXCHANGE FILL-BY-FILL & SETTLEMENT RECONCILIATION LEDGER (OCT 26 – OCT 28)")
report_lines.append("")
report_lines.append("## 1. Executive Reconciliation Summary")
report_lines.append(f"- **Current Exchange Cash Balance (`exchange_balance.json`):** `${bal.get('balance', 0)/100:.2f}` (`0` open positions, `0` resting orders)")
report_lines.append(f"- **Total Unique Markets Traded (`>= 2026-10-26T00:00:00Z`):** `{len(rows)}` (`{wins}W – {losses}L`, `{wins/len(rows)*100:.2f}%` Win Rate)")
report_lines.append(f"- **Total Deduplicated Exchange Fills:** `{len(recent_fills)}` across `{len(recent_orders)}` submitted orders")
report_lines.append(f"- **True Exchange Gross Wins (`{wins}` winners):** `+${win_dollars:.2f}` (Average Win: `+${win_dollars/max(1,wins):.2f}`)")
report_lines.append(f"- **True Exchange Gross Losses (`{losses}` losers):** `-${abs(loss_dollars):.2f}` (Average Loss: `-${abs(loss_dollars)/max(1,losses):.2f}`)")
report_lines.append(f"- **True Payoff Skew (Loss-to-Win Ratio):** `{(abs(loss_dollars)/max(1,losses))/(win_dollars/max(1,wins)):.2f} : 1` (True Break-Even Win Rate: `{(abs(loss_dollars)/max(1,losses))/((abs(loss_dollars)/max(1,losses))+(win_dollars/max(1,wins)))*100:.2f}%`)")
report_lines.append(f"- **Total Exchange Fees Paid:** `${tot_maker_fees + tot_taker_fees:.2f}` (`${tot_maker_fees:.2f}` Maker fees + `${tot_taker_fees:.2f}` Taker IOC stop-loss fees)")
report_lines.append(f"- **True Exchange Net Realized P&L:** **`${tot_net_pnl:+.2f}`**")
report_lines.append(f"- **Synthetic `trade_autopsy_log` Sum across these 66 tickers:** `${sum(r['autopsy_logged_pnl'] for r in rows):+.2f}` (**`${sum(r['autopsy_error'] for r in rows):+.2f}` accounting distortion** caused by partial IOC fills logging duplicate full-position losses and missing pre-Oct-27 autopsies)")
report_lines.append(f"- **Counterfactual Hold-to-Settlement P&L (if zero IOC stop-losses had been fired):** **`${tot_hold_pnl:+.2f}`** (`{len(stopped_out_winners)}` of the `{losses}` stopped-out losing trades actually **WON at `$1.00` settlement**, costing `${sum(x[3] for x in stopped_out_winners):+.2f}` in premature stop-loss liquidations!)")
report_lines.append("")
report_lines.append("## 2. The 10 Stopped-Out Trades That Actually Won at $1.00 Settlement")
report_lines.append("| Ticker | Qty | Entry Avg | Panic IOC Exit Avg | Fees Paid | True Realized Loss | Hold-to-Settlement P&L | Net Dollar Swing Lost to Stop-Loss |")
report_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
for tk, rpnl, hpnl, swg, ea, xa, eq, fe in sorted(stopped_out_winners, key=lambda x: x[3], reverse=True):
    report_lines.append(f"| `{tk}` | `{eq:.0f}x` | `{ea:.1f}c` | `{xa:.1f}c` | `${fe:.2f}` | **`${rpnl:+.2f}`** | **`${hpnl:+.2f}`** | **`-${swg:.2f}`** |")

report_lines.append("")
report_lines.append("## 3. Complete 66-Ticker Exchange-Reconciled Ledger (`exchange_fills.json` + `exchange_settlements.json`)")
report_lines.append("| # | First Fill (UTC) | Ticker | Side | Fills (IOCs) | Entry Qty @ Avg | Exit Qty @ Avg | Settle Qty | Result | Maker / Taker Fee | True Net P&L | Autopsy Logged (Count) | Hold-to-Settle P&L |")
report_lines.append("| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
for r in rows:
    sw_flag = " *(WON AT $1)*" if r["stopped_out_winner"] else ""
    report_lines.append(
        f"| {r['idx']} | `{r['first_fill_utc']}` | `{r['ticker']}` | `{r['entry_side']}` | `{r['fills']} ({r['ioc_exit_orders']})` | "
        f"`{r['entry_qty']}x @ {r['entry_avg_c']}c` | `{r['exit_qty']}x @ {r['exit_avg_c']}c` | `{r['unpaired_settle_qty']}x` | "
        f"`{r['market_result']}`{sw_flag} | `${r['maker_fee']:.2f} / ${r['taker_fee']:.2f}` | **`${r['true_net_pnl']:+.2f}`** | "
        f"`${r['autopsy_logged_pnl']:+.2f} ({r['autopsy_records']}x)` | `${r['hold_to_settle_pnl']:+.2f}` |"
    )

out_path = ev_dir / "CHATGPT_SECTION10_RECONCILIATION_PACKET.md"
out_path.write_text("\n".join(report_lines))
print("\n".join(report_lines[:40]))
print(f"\nWrote {len(report_lines)} lines to {out_path}")
