"""Maker/Taker EV, Vegas De-Vigging, and Green-Out Hedge Calculator.

Non-Negotiable Mathematical Laws:
- Maker Fee: $0.00
- Taker Fee: ceil(0.07 * contracts * P * (1 - P))
- Shin / Multiplicative De-Vigging: sum(P_i) == 1.00000
- Mid-Game Green-Out: Equal guaranteed profit in all outcome states
"""

import math
from typing import Any, Optional
import numpy as np


class FeeMathEngine:
    """Quantitative algorithms for exchange fees, Vegas de-vigging, and green-out hedging."""

    # -------------------------------------------------------------------------
    # 1. Exchange Fee Laws & EV Calculation
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_taker_fee(contracts: int, price_cents: int) -> int:
        """
        Kalshi Taker Fee Law:
        taker_fee = ceil(0.07 * contracts * P * (1 - P))
        Where P = price in decimal probability (price_cents / 100.0).
        Returned in integer cents.
        """
        if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
            return 0
        p = price_cents / 100.0
        fee_cents_raw = 0.07 * contracts * p * (1.0 - p) * 100.0
        # Round to 6 decimal places before ceil to prevent IEEE 754 float precision artifact
        return int(math.ceil(round(fee_cents_raw, 6)))

    @staticmethod
    def calculate_maker_fee(contracts: int, price_cents: int) -> int:
        """
        Kalshi Maker-First Law:
        Maker limit orders rest on book and incur $0.00 fee.
        """
        return 0

    @classmethod
    def calculate_trade_ev(
        cls,
        entry_price_cents: int,
        true_probability: float,
        contracts: int = 1,
        is_maker: bool = True,
    ) -> dict[str, Any]:
        """
        Computes Expected Value (EV) comparing Maker ($0.00 fee) vs Taker execution.
        EV = P_true * (100 - P_entry) - (1 - P_true) * P_entry - Fee
        """
        p_entry = entry_price_cents
        fee_cents = (
            cls.calculate_maker_fee(contracts, p_entry)
            if is_maker
            else cls.calculate_taker_fee(contracts, p_entry)
        )
        hypothetical_taker_fee = cls.calculate_taker_fee(contracts, p_entry)

        # Expected payout per contract in cents
        win_pnl = 100 - p_entry
        loss_pnl = -p_entry

        total_ev_cents = (
            contracts * (true_probability * win_pnl + (1.0 - true_probability) * loss_pnl)
            - fee_cents
        )

        return {
            "entry_price_cents": entry_price_cents,
            "true_probability": true_probability,
            "contracts": contracts,
            "is_maker": is_maker,
            "fee_cents": fee_cents,
            "taker_fee_penalty_cents": hypothetical_taker_fee if is_maker else 0,
            "ev_cents": round(total_ev_cents, 2),
            "ev_dollars": round(total_ev_cents / 100.0, 4),
            "roi_pct": round((total_ev_cents / (contracts * p_entry)) * 100.0, 2)
            if p_entry > 0
            else 0.0,
        }

    # -------------------------------------------------------------------------
    # 2. Vegas De-Vigging (Shin & Multiplicative)
    # -------------------------------------------------------------------------
    @staticmethod
    def odds_to_implied_prob(decimal_or_american_odds: float) -> float:
        """Converts decimal odds (e.g. 1.85) or American odds (e.g. -115, +140) to implied probability."""
        if 1.0 < decimal_or_american_odds < 100.0:
            return 1.0 / decimal_or_american_odds
        elif decimal_or_american_odds >= 100.0:
            return 100.0 / (decimal_or_american_odds + 100.0)
        elif decimal_or_american_odds <= -100.0:
            pos = abs(decimal_or_american_odds)
            return pos / (pos + 100.0)
        elif 0.0 < decimal_or_american_odds <= 1.0:
            return decimal_or_american_odds
        else:
            raise ValueError(f"Invalid odds value: {decimal_or_american_odds}")

    @staticmethod
    def devig_multiplicative(raw_probs: list[float]) -> list[float]:
        """
        Multiplicative (proportional) de-vigging.
        Ensures implied probabilities sum to exactly 1.0 (100.0%).
        """
        total = sum(raw_probs)
        if total <= 0:
            raise ValueError("Raw probabilities must sum to a positive number")
        return [p / total for p in raw_probs]

    @classmethod
    def devig_shin(
        cls,
        raw_probs: list[float],
        max_iter: int = 100,
        tol: float = 1e-6,
    ) -> list[float]:
        """
        Shin (1993) De-Vigging Method for two or more outcomes.
        Accounts for insider trading parameter 'z'.
        Solves: sum_i [ sqrt(z^2 + 4*(1-z)*(pi_i^2 / sum_pi)) ] = 2 - z (for 2-way)
        """
        pi = np.array(raw_probs, dtype=float)
        sum_pi = np.sum(pi)

        if len(raw_probs) == 2:
            low_z = 0.0
            high_z = 0.4

            def diff_func(z: float) -> float:
                sqrt1 = math.sqrt(z * z + 4.0 * (1.0 - z) * (pi[0] ** 2) / sum_pi)
                sqrt2 = math.sqrt(z * z + 4.0 * (1.0 - z) * (pi[1] ** 2) / sum_pi)
                return (sqrt1 + sqrt2) - (2.0 - z)

            z_star = 0.0
            for _ in range(max_iter):
                mid_z = (low_z + high_z) / 2.0
                val = diff_func(mid_z)
                if abs(val) < tol:
                    z_star = mid_z
                    break
                if val > 0:
                    low_z = mid_z
                else:
                    high_z = mid_z
                z_star = mid_z

            denom = 2.0 * (1.0 - z_star)
            if denom <= 0:
                return cls.devig_multiplicative(raw_probs)

            p0 = (
                math.sqrt(z_star * z_star + 4.0 * (1.0 - z_star) * (pi[0] ** 2) / sum_pi)
                - z_star
            ) / denom
            p1 = (
                math.sqrt(z_star * z_star + 4.0 * (1.0 - z_star) * (pi[1] ** 2) / sum_pi)
                - z_star
            ) / denom

            total = p0 + p1
            return [float(p0 / total), float(p1 / total)]
        else:
            return cls.devig_multiplicative(raw_probs)

    @classmethod
    def devig_two_way(
        cls,
        odds_home: float,
        odds_away: float,
        method: str = "shin",
    ) -> tuple[float, float]:
        """
        De-vigs a 2-way sports line (e.g. Pinnacle / Circa moneyline).
        Guarantees: prob_home + prob_away == 1.00000.
        """
        p_home_raw = cls.odds_to_implied_prob(odds_home)
        p_away_raw = cls.odds_to_implied_prob(odds_away)

        if method.lower() == "shin":
            devigged = cls.devig_shin([p_home_raw, p_away_raw])
        else:
            devigged = cls.devig_multiplicative([p_home_raw, p_away_raw])

        return round(devigged[0], 5), round(devigged[1], 5)

    # -------------------------------------------------------------------------
    # 3. Mid-Game Green-Out Equal Profit Hedge Calculator
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_green_out(
        entry_price_cents: int,
        current_yes_bid_cents: int,
        initial_contracts: int,
    ) -> dict[str, Any]:
        """
        Calculates exact Green-Out execution parameters when position rises +16c or more.
        Guarantees EQUAL profit regardless of final game outcome.

        Option A: Full Sale of YES via resting Maker limit order at current_yes_bid_cents.
        Profit = initial_contracts * (current_yes_bid_cents - entry_price_cents).

        Option B: Equal Opposing NO Purchase.
        Buy initial_contracts of NO @ (100 - current_yes_bid_cents).
        Profit if YES wins: 100*Q - [Q*P_0 + Q*(100 - P_curr)] = Q*(P_curr - P_0).
        Profit if NO wins:  100*Q - [Q*P_0 + Q*(100 - P_curr)] = Q*(P_curr - P_0).
        Identical equal profit in both states!

        Option C: Free-Roll (Recoup initial investment, keep upside for $0 risk).
        """
        gain_cents = current_yes_bid_cents - entry_price_cents
        is_green_out_eligible = gain_cents >= 16

        guaranteed_profit_cents = initial_contracts * gain_cents
        guaranteed_profit_dollars = guaranteed_profit_cents / 100.0

        no_purchase_price_cents = 100 - current_yes_bid_cents
        no_contracts_to_buy = initial_contracts

        initial_investment_cents = initial_contracts * entry_price_cents
        freeroll_contracts_to_sell = min(
            initial_contracts,
            int(math.ceil(initial_investment_cents / current_yes_bid_cents)),
        )
        remaining_freeroll_yes = initial_contracts - freeroll_contracts_to_sell
        freeroll_upside_if_yes_cents = remaining_freeroll_yes * 100

        return {
            "entry_price_cents": entry_price_cents,
            "current_yes_bid_cents": current_yes_bid_cents,
            "gain_cents": gain_cents,
            "is_green_out_eligible": is_green_out_eligible,
            "initial_contracts": initial_contracts,
            "guaranteed_profit_cents": guaranteed_profit_cents,
            "guaranteed_profit_dollars": guaranteed_profit_dollars,
            "sell_all_action": {
                "action": "sell",
                "side": "yes",
                "contracts": initial_contracts,
                "price_cents": current_yes_bid_cents,
                "post_only": True,
            },
            "buy_opposing_no_action": {
                "action": "buy",
                "side": "no",
                "contracts": no_contracts_to_buy,
                "price_cents": no_purchase_price_cents,
                "post_only": True,
            },
            "freeroll_action": {
                "contracts_to_sell": freeroll_contracts_to_sell,
                "remaining_free_yes": remaining_freeroll_yes,
                "max_payout_cents": freeroll_upside_if_yes_cents,
            },
        }
