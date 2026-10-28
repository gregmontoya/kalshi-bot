"""Crypto Maker/Taker Fee, Rebate, Green-Out Hedge & Delta-Neutral Math Engine.

Non-Negotiable Crypto Exchange Math Laws:
1. Maker-First (`POST_ONLY`):
   - Hyperliquid Maker Fee: -0.01% (-1.0 bps) REBATE (pays you to rest orders)
   - Hyperliquid Taker Fee: +0.035% (3.5 bps)
   - Kraken Pro Maker Fee: +0.16% (down to 0.00% at volume) vs +0.26% Taker
   - Coinbase Retail Taker Penalty: ~0.60% (60 bps) -> Saved on every Maker trade
2. Mid-Swing "Green-Out" (Scale-Out + Breakeven-Plus Stop):
   - Sells `scale_out_fraction` (e.g. 60%) via Maker limit order on bounce
   - Moves stop-loss on remaining 40% moonbag above entry so worst-case outcome is 100% risk-free profit.
3. Delta-Neutral Funding & Basis Math:
   - Long 1x Spot (Kraken/Coinbase) + Short 1x Perp (Hyperliquid) = 0.00 Net Directional Delta
   - Harvests hourly funding rate + perp-to-spot basis convergence.
"""

from typing import Any
from src.crypto.models import CryptoVenue


class CryptoFeeMathEngine:
    """Quantitative fee, rebate, Green-Out, and Delta-Neutral yield calculator."""

    # Standard fee schedule in basis points (1 bp = 0.01% = 0.0001)
    FEE_SCHEDULE_BPS = {
        CryptoVenue.HYPERLIQUID: {"maker_bps": -1.0, "taker_bps": 3.5},
        CryptoVenue.KRAKEN_PRO: {"maker_bps": 16.0, "taker_bps": 26.0},
        CryptoVenue.COINBASE: {"maker_bps": 40.0, "taker_bps": 60.0},
    }

    @classmethod
    def calculate_order_fee_usd(
        cls,
        notional_usd: float,
        venue: CryptoVenue = CryptoVenue.HYPERLIQUID,
        is_maker: bool = True,
    ) -> dict[str, float]:
        """
        Calculates exact USD fee (negative = Maker rebate paid to us)
        and how much money was saved vs a retail Coinbase/Exchange Taker order.
        """
        schedule = cls.FEE_SCHEDULE_BPS.get(venue, {"maker_bps": 0.0, "taker_bps": 10.0})
        applied_bps = schedule["maker_bps"] if is_maker else schedule["taker_bps"]
        fee_usd = notional_usd * (applied_bps / 10000.0)

        # Compare against standard retail Taker execution (Coinbase 60 bps)
        retail_taker_fee_usd = notional_usd * (60.0 / 10000.0)
        saved_vs_retail_usd = retail_taker_fee_usd - fee_usd

        return {
            "notional_usd": round(notional_usd, 2),
            "venue": venue.value,
            "is_maker": is_maker,
            "applied_bps": applied_bps,
            "fee_usd": round(fee_usd, 4),
            "rebate_earned_usd": round(abs(fee_usd), 4) if fee_usd < 0 else 0.0,
            "saved_vs_retail_taker_usd": round(saved_vs_retail_usd, 4),
        }

    @classmethod
    def calculate_green_out_plan(
        cls,
        entry_price: float,
        current_price: float,
        quantity: float,
        scale_out_fraction: float = 0.60,
        protective_stop_buffer_pct: float = 0.45,
        venue: CryptoVenue = CryptoVenue.HYPERLIQUID,
    ) -> dict[str, Any]:
        """
        Calculates the exact Crypto "Green-Out" (Equal/Guaranteed Profit Lock):
        1. Sells `scale_out_fraction` (e.g. 60%) at `current_price` via POST_ONLY Maker.
        2. Moves stop-loss on remaining `1 - scale_out_fraction` (40%) to `entry_price * (1 + buffer)`.
        3. Guarantees net USD profit even if the market reverses right after Green-Out,
           while keeping 40% uncapped upside ("Moonbag").
        """
        if entry_price <= 0 or quantity <= 0:
            return {}

        sold_qty = round(quantity * scale_out_fraction, 6)
        remaining_qty = round(quantity - sold_qty, 6)

        locked_gross_profit = sold_qty * (current_price - entry_price)
        fee_info = cls.calculate_order_fee_usd(sold_qty * current_price, venue=venue, is_maker=True)
        locked_net_profit = locked_gross_profit - fee_info["fee_usd"]

        new_stop_price = round(entry_price * (1.0 + protective_stop_buffer_pct / 100.0), 4)
        stop_out_profit_on_runner = remaining_qty * (new_stop_price - entry_price)
        guaranteed_worst_case_profit = locked_net_profit + stop_out_profit_on_runner

        runner_target_price = round(entry_price * 1.055, 4)  # +5.5% runner target
        best_case_profit = locked_net_profit + remaining_qty * (runner_target_price - entry_price)

        return {
            "entry_price": round(entry_price, 4),
            "green_out_price": round(current_price, 4),
            "gain_pct": round(((current_price - entry_price) / entry_price) * 100.0, 2),
            "sold_quantity": sold_qty,
            "remaining_moonbag_quantity": remaining_qty,
            "new_protective_stop_price": new_stop_price,
            "runner_target_price": runner_target_price,
            "locked_realized_profit_usd": round(locked_net_profit, 2),
            "guaranteed_min_total_profit_usd": round(guaranteed_worst_case_profit, 2),
            "target_total_profit_usd": round(best_case_profit, 2),
            "saved_vs_retail_fees_usd": fee_info["saved_vs_retail_taker_usd"],
        }

    @staticmethod
    def calculate_delta_neutral_lock(
        spot_price: float,
        perp_price: float,
        funding_rate_hourly_bps: float,
        pair_notional_usd: float,
        staking_yield_apy: float = 7.20,
        persist_hours: int = 12,
    ) -> dict[str, Any]:
        """
        Computes the Ethena-Grade Triple-Yield Stack for Pod 3:
        1. Liquid Staking / Kraken Staked Spot Yield (~7.2% APY on Staked SOL/ETH)
        2. Hourly Perpetual Funding Rate Yield (paid by leveraged longs to our 1x short)
        3. Perp-to-Spot Basis Convergence Yield (entering when Perp > Spot)
        Includes Funding-Flip Immunity check (persist_hours >= 6).
        """
        leg_notional = pair_notional_usd / 2.0
        quantity = leg_notional / spot_price if spot_price > 0 else 0.0

        basis_bps = ((perp_price - spot_price) / spot_price) * 10000.0 if spot_price > 0 else 0.0
        # Hourly funding rate in bps -> Daily % -> Annualized APY %
        daily_funding_pct = (funding_rate_hourly_bps * 24.0) / 100.0
        annualized_funding_apy = daily_funding_pct * 365.0
        annualized_basis_bonus_pct = (basis_bps / 100.0) * 12.0  # Assuming convergence within ~30 days
        total_combined_apy = staking_yield_apy + annualized_funding_apy + max(0.0, annualized_basis_bonus_pct)

        daily_total_pct = total_combined_apy / 365.0
        daily_cash_payout_usd = leg_notional * (daily_total_pct / 100.0)
        monthly_cash_payout_usd = daily_cash_payout_usd * 30.0
        funding_flip_immune = funding_rate_hourly_bps >= 0.8 and persist_hours >= 6

        return {
            "pair_notional_usd": round(pair_notional_usd, 2),
            "leg_notional_usd": round(leg_notional, 2),
            "hedged_quantity": round(quantity, 6),
            "spot_entry_price": round(spot_price, 4),
            "perp_entry_price": round(perp_price, 4),
            "entry_basis_bps": round(basis_bps, 2),
            "staking_yield_apy": round(staking_yield_apy, 2),
            "funding_rate_hourly_bps": round(funding_rate_hourly_bps, 3),
            "annualized_funding_apy": round(annualized_funding_apy, 2),
            "total_expected_apy": round(total_combined_apy, 2),
            "daily_cash_yield_usd": round(daily_cash_payout_usd, 2),
            "monthly_cash_yield_usd": round(monthly_cash_payout_usd, 2),
            "funding_flip_immune": funding_flip_immune,
            "directional_price_risk_pct": 0.0,
        }
