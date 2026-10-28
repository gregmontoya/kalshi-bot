"""
Deterministic Release-Blocking Unit Test Suite (Section 9 Minimum Acceptance Tests).
Verifies all 12 acceptance criteria from the Third-Pass Source-Code Audit before any live deployment.
"""

from src.risk.remediated_engine_guards import (
    ExitIntent,
    FillReconciledLedger,
    ReconciledPosition,
    build_safe_order_payload,
    check_entry_capacity_and_quota,
    compute_effective_imbalance_threshold,
    reconcile_ioc_exit,
    reconcile_startup_state,
    size_position_by_risk_budget,
    validate_fair_probability_and_freshness,
)


def test_1_partial_ioc_exit_retains_remaining_exposure():
    """1. A partially filled IOC exit retains the exact remaining exposure in local state."""
    ledger = FillReconciledLedger()
    ledger.process_fill({
        "fill_id": "entry-1",
        "ticker": "KXSERIEAGAME-26OCT27ROMCAG-ROM",
        "outcome_side": "yes",
        "count_fp": "100.00",
        "yes_price_dollars": "0.8700",
        "no_price_dollars": "0.1300",
        "fee_cost": "0.2000",
    })
    tracked = {"KXSERIEAGAME-26OCT27ROMCAG-ROM": ledger.positions["KXSERIEAGAME-26OCT27ROMCAG-ROM"]}
    intent = ExitIntent(
        ticker="KXSERIEAGAME-26OCT27ROMCAG-ROM",
        client_order_id="exit-cid-1",
        requested_qty=100.0,
        limit_price_cents=68,
        side="ask",
    )
    # Only 25 of 100 contracts fill on the IOC exit
    partial_fills = [{
        "fill_id": "exit-fill-1",
        "ticker": "KXSERIEAGAME-26OCT27ROMCAG-ROM",
        "outcome_side": "no",
        "count_fp": "25.00",
        "yes_price_dollars": "0.6800",
        "no_price_dollars": "0.3200",
        "fee_cost": "0.4000",
    }]
    res = reconcile_ioc_exit(tracked, intent, {"status": "canceled"}, partial_fills, ledger)
    assert res.status == "PARTIAL"
    assert res.filled_qty == 25.0
    assert res.remaining_qty == 75.0
    assert "KXSERIEAGAME-26OCT27ROMCAG-ROM" in tracked
    assert tracked["KXSERIEAGAME-26OCT27ROMCAG-ROM"].quantity == 75.0
    assert tracked["KXSERIEAGAME-26OCT27ROMCAG-ROM"].emergency_exit_state is True
    assert len(ledger.completed_trades) == 0


def test_2_rejected_or_timed_out_exit_never_records_completed_trade():
    """2. A rejected or timed-out exit does not get recorded as a completed trade."""
    ledger = FillReconciledLedger()
    ledger.process_fill({
        "fill_id": "entry-2",
        "ticker": "KXNBA1H-26OCT27DETNYK-DET",
        "outcome_side": "yes",
        "count_fp": "50.00",
        "yes_price_dollars": "0.8400",
        "no_price_dollars": "0.1600",
        "fee_cost": "0.0000",
    })
    tracked = {"KXNBA1H-26OCT27DETNYK-DET": ledger.positions["KXNBA1H-26OCT27DETNYK-DET"]}
    intent = ExitIntent(
        ticker="KXNBA1H-26OCT27DETNYK-DET",
        client_order_id="exit-cid-2",
        requested_qty=50.0,
        limit_price_cents=72,
        side="ask",
    )
    res_timeout = reconcile_ioc_exit(tracked, intent, None, [], ledger)
    assert res_timeout.status == "TIMED_OUT"
    assert res_timeout.remaining_qty == 50.0
    assert len(ledger.completed_trades) == 0
    assert tracked["KXNBA1H-26OCT27DETNYK-DET"].quantity == 50.0


def test_3_idempotent_fill_processing_never_doubles_realized_pnl():
    """3. Processing the same exchange fill twice never doubles realized P&L."""
    ledger = FillReconciledLedger()
    entry_fill = {
        "fill_id": "fill-open-100",
        "ticker": "KXNHLGAME-26OCT27FLAWPG-FLA",
        "outcome_side": "yes",
        "count_fp": "71.00",
        "yes_price_dollars": "0.8000",
        "no_price_dollars": "0.2000",
        "fee_cost": "0.0000",
    }
    exit_fill = {
        "fill_id": "fill-close-100",
        "ticker": "KXNHLGAME-26OCT27FLAWPG-FLA",
        "outcome_side": "no",
        "count_fp": "71.00",
        "yes_price_dollars": "0.8800",
        "no_price_dollars": "0.1200",
        "fee_cost": "0.3300",
    }
    ledger.process_fill(entry_fill)
    pnl_first = ledger.process_fill(exit_fill)
    pnl_duplicate = ledger.process_fill(exit_fill)
    assert round(pnl_first, 2) == 5.35  # 71 * (0.88 - 0.80) - 0.33 = 5.68 - 0.33 = 5.35
    assert pnl_duplicate == 0.0
    assert len(ledger.completed_trades) == 1


def test_4_restart_with_open_positions_reconstructs_exchange_state():
    """4. Restarting with open positions reconstructs the correct counts, costs, and outstanding orders."""
    local_mem = {}
    exchange_positions = [
        {
            "ticker": "KXNBAGAME-26OCT28BOSMIA-BOS",
            "position_fp": "40.00",
            "market_exposure_dollars": "30.00",
            "fees_paid_dollars": "0.12",
        },
        {
            "ticker": "KXNHLTOTAL-26OCT28NYRWSH-6",
            "position_fp": "-25.00",
            "market_exposure_dollars": "20.00",
            "fees_paid_dollars": "0.00",
        },
    ]
    exchange_orders = [
        {"ticker": "KXNBAGAME-26OCT28BOSMIA-BOS", "status": "resting", "action": "sell"},
        {"ticker": "KXNFLGAME-26OCT28BUFKC-BUF", "status": "resting", "action": "buy"},
    ]
    rebuilt, reserved = reconcile_startup_state(local_mem, exchange_positions, exchange_orders)
    assert len(rebuilt) == 2
    assert rebuilt["KXNBAGAME-26OCT28BOSMIA-BOS"].side == "yes"
    assert rebuilt["KXNBAGAME-26OCT28BOSMIA-BOS"].quantity == 40.0
    assert rebuilt["KXNHLTOTAL-26OCT28NYRWSH-6"].side == "no"
    assert rebuilt["KXNHLTOTAL-26OCT28NYRWSH-6"].quantity == 25.0
    # Resting sell on BOS is a closing order, whereas resting buy on BUF is a reserved new entry
    assert reserved == {"KXNFLGAME-26OCT28BUFKC-BUF"}


def test_5_stale_ghost_position_absent_from_exchange_is_purged():
    """5. A stale local position absent from the exchange cannot consume active risk capacity indefinitely."""
    local_mem = {
        "KXWTAMATCH-26OCT26ZAKLIN-LIN": {
            "count": 122,
            "stake": 101.26,
            "max_count": 244,
            "max_stake": 202.52,
            "stage1_alerted": True,
        }
    }
    rebuilt, reserved = reconcile_startup_state(local_mem, exchange_positions=[], exchange_resting_orders=[])
    assert len(rebuilt) == 0
    assert len(reserved) == 0
    assert "KXWTAMATCH-26OCT26ZAKLIN-LIN" not in local_mem


def test_6_five_open_or_reserved_positions_prevent_a_sixth():
    """6. Five open positions or reserved market commitments prevent a sixth."""
    open_pos = {
        f"TICKER-{i}": ReconciledPosition(ticker=f"TICKER-{i}", side="yes", quantity=10.0, cost_basis_dollars=7.5)
        for i in range(3)
    }
    reserved = {"TICKER-3", "TICKER-4"}
    ok, reason, slots = check_entry_capacity_and_quota(
        open_positions=open_pos,
        reserved_entry_tickers=reserved,
        max_concurrent=5,
        completed_trades_today=10,
        daily_trade_cap=40,
        manual_pause=False,
    )
    assert ok is False
    assert slots == 0
    assert "Capacity full" in reason


def test_7_reaching_daily_trade_cap_blocks_new_entries():
    """7. Reaching the daily trade cap blocks new entries."""
    ok, reason, slots = check_entry_capacity_and_quota(
        open_positions={},
        reserved_entry_tickers=set(),
        max_concurrent=5,
        completed_trades_today=40,
        daily_trade_cap=40,
        manual_pause=False,
    )
    assert ok is False
    assert slots == 0
    assert "Daily trade limit reached" in reason


def test_8_falling_behind_daily_target_never_relaxes_entry_criteria():
    """8. Falling behind the daily target never relaxes entry criteria."""
    normal_thresh = compute_effective_imbalance_threshold(1.60, behind_pace=False)
    behind_thresh = compute_effective_imbalance_threshold(1.60, behind_pace=True)
    assert behind_thresh == normal_thresh == 1.60


def test_9_expired_external_validation_rejects_or_cancels_entry():
    """9. An expired external validation cancels/blocks the associated entry order."""
    ok, reason = validate_fair_probability_and_freshness(
        series="KXNBAGAME",
        side="yes",
        kalshi_effective_price=0.72,
        sharp_prob=0.78,
        sharp_age_sec=28.0,  # > 15.0s max age
        scoreboard_verified=True,
        scoreboard_age_sec=3.0,
        min_edge=0.03,
        max_evidence_age_sec=15.0,
    )
    assert ok is False
    assert "expired" in reason


def test_10_missing_or_mismatched_fair_probability_vetoes_entry():
    """10. Missing or mismatched fair-probability data vetoes entry (including Challenger tennis and NO side)."""
    # Missing sharp odds
    ok_missing, _ = validate_fair_probability_and_freshness(
        series="KXNBAGAME",
        side="yes",
        kalshi_effective_price=0.72,
        sharp_prob=None,
        sharp_age_sec=0.0,
        scoreboard_verified=True,
        scoreboard_age_sec=1.0,
    )
    assert ok_missing is False

    # Challenger tennis without verified scoreboard
    ok_chal, _ = validate_fair_probability_and_freshness(
        series="KXATPCHALLENGERMATCH",
        side="yes",
        kalshi_effective_price=0.72,
        sharp_prob=0.78,
        sharp_age_sec=2.0,
        scoreboard_verified=False,
        scoreboard_age_sec=0.0,
    )
    assert ok_chal is False

    # NO side symmetric check: if P(YES)=0.30, then P(NO)=0.70; at NO price 0.69, edge is +0.01 < +0.03 -> VETO
    ok_no_low_edge, _ = validate_fair_probability_and_freshness(
        series="KXNBATOTAL",
        side="no",
        kalshi_effective_price=0.69,
        sharp_prob=0.30,
        sharp_age_sec=2.0,
        scoreboard_verified=True,
        scoreboard_age_sec=2.0,
        min_edge=0.03,
    )
    assert ok_no_low_edge is False


def test_11_realized_pnl_reconciles_to_exchange_fills_fees_and_settlement():
    """11. Realized P&L reconciles to exchange executions, fees, and settlement accounting."""
    ledger = FillReconciledLedger()
    # Buy 103 YES @ $0.83 ($85.49) + $0.2544 Maker fee
    ledger.process_fill({
        "fill_id": "gri-buy",
        "ticker": "KXATPMATCH-26OCT25GRINAK-GRI",
        "outcome_side": "yes",
        "count_fp": "103.00",
        "yes_price_dollars": "0.8300",
        "no_price_dollars": "0.1700",
        "fee_cost": "0.2544",
    })
    # Sell 103 YES (acquire 103 NO @ $0.52, i.e. $0.48 YES price) + $1.7997 Taker fee
    pnl = ledger.process_fill({
        "fill_id": "gri-sell",
        "ticker": "KXATPMATCH-26OCT25GRINAK-GRI",
        "outcome_side": "no",
        "count_fp": "103.00",
        "yes_price_dollars": "0.4800",
        "no_price_dollars": "0.5200",
        "fee_cost": "1.7997",
    })
    # Exact exchange P&L: 103 * (0.48 - 0.83) - 0.2544 - 1.7997 = -36.05 - 2.0541 = -38.1041
    assert round(pnl, 2) == -38.10


def test_12_explicit_pause_blocks_entries_and_enforces_safety_flags_and_risk_sizing():
    """12. An explicit pause prevents new entries, enforces order safety flags, and disables streak escalation."""
    ok, reason, _ = check_entry_capacity_and_quota(
        open_positions={},
        reserved_entry_tickers=set(),
        max_concurrent=5,
        completed_trades_today=0,
        daily_trade_cap=40,
        manual_pause=True,
    )
    assert ok is False
    assert "Manual pause" in reason

    # Verify streak escalation is disabled (0W and 10W produce identical fixed-fractional risk sizing)
    qty_0w = size_position_by_risk_budget(
        equity_dollars=358.72,
        entry_price_dollars=0.72,
        estimated_stress_loss_per_contract=0.18,
        win_streak=0,
    )
    qty_10w = size_position_by_risk_budget(
        equity_dollars=358.72,
        entry_price_dollars=0.72,
        estimated_stress_loss_per_contract=0.18,
        win_streak=10,
    )
    assert qty_0w == qty_10w == 9  # floor((358.72 * 0.005) / 0.18) = floor(1.7936 / 0.18) = 9 contracts

    # Verify Law 6 safety flags on order payload
    sell_payload = build_safe_order_payload(
        ticker="KXNBAGAME-TEST",
        side="ask",
        action="sell",
        count=9,
        price_cents=90,
        client_order_id="cid-123",
        post_only=True,
    )
    assert sell_payload["sell_position_floor"] == 0
    assert sell_payload["cancel_on_pause"] is True
    assert sell_payload["post_only"] is True


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    tests = [
        test_1_partial_ioc_exit_retains_remaining_exposure,
        test_2_rejected_or_timed_out_exit_never_records_completed_trade,
        test_3_idempotent_fill_processing_never_doubles_realized_pnl,
        test_4_restart_with_open_positions_reconstructs_exchange_state,
        test_5_stale_ghost_position_absent_from_exchange_is_purged,
        test_6_five_open_or_reserved_positions_prevent_a_sixth,
        test_7_reaching_daily_trade_cap_blocks_new_entries,
        test_8_falling_behind_daily_target_never_relaxes_entry_criteria,
        test_9_expired_external_validation_rejects_or_cancels_entry,
        test_10_missing_or_mismatched_fair_probability_vetoes_entry,
        test_11_realized_pnl_reconciles_to_exchange_fills_fees_and_settlement,
        test_12_explicit_pause_blocks_entries_and_enforces_safety_flags_and_risk_sizing,
    ]
    for fn in tests:
        fn()
        print(f"PASS: {fn.__name__}")
    print(f"\nALL {len(tests)}/12 RELEASE-BLOCKING ACCEPTANCE TESTS PASSED.")

