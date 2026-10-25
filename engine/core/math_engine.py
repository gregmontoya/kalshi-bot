"""Mathematical and Quantitative Engine for Kalshi Strategies.

Includes:
- Vegas De-Vigging (Multiplicative and Shin methods)
- Maker and Taker fee accounting
- Mid-Game Green Out calculators (equalized profit & synthetic NO hedging)
- 4th Quarter Tail-Lock mathematical win probability model
"""

import math
from typing import Any, Optional
import numpy as np


class MathEngine:
    """Quantitative algorithms adhering strictly to workspace exchange laws and strategies."""

    # -------------------------------------------------------------------------
    # 1. Exchange Fee Laws
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

    # -------------------------------------------------------------------------
    # 2. Vegas De-Vigging (Shin & Multiplicative)
    # -------------------------------------------------------------------------
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

    @staticmethod
    def odds_to_implied_prob(decimal_or_american_odds: float) -> float:
        """Converts decimal odds (e.g. 1.85) or American odds (e.g. -115, +140) to implied probability."""
        if decimal_or_american_odds > 1.0 and decimal_or_american_odds < 100.0:
            # Decimal odds
            return 1.0 / decimal_or_american_odds
        elif decimal_or_american_odds >= 100.0:
            # American positive odds (+150 -> 100 / (150 + 100))
            return 100.0 / (decimal_or_american_odds + 100.0)
        elif decimal_or_american_odds <= -100.0:
            # American negative odds (-150 -> 150 / (150 + 100))
            pos = abs(decimal_or_american_odds)
            return pos / (pos + 100.0)
        elif 0.0 < decimal_or_american_odds <= 1.0:
            return decimal_or_american_odds
        else:
            raise ValueError(f"Invalid odds value: {decimal_or_american_odds}")

    @classmethod
    def devig_shin(cls, raw_probs: list[float], max_iter: int = 100, tol: float = 1e-6) -> list[float]:
        """
        Shin (1993) De-Vigging Method for two or more outcomes.
        Accounts for insider trading parameter 'z'.
        Solves: sum_i [ sqrt(z^2 + 4*(1-z)*(pi_i^2 / sum_pi)) ] = 2 - z (for 2-way)
        or iteratively finds z such that sum of implied true probabilities equals 1.0.
        """
        pi = np.array(raw_probs, dtype=float)
        sum_pi = np.sum(pi)

        if len(raw_probs) == 2:
            # Binary Shin closed-form / fast bisection
            low_z = 0.0
            high_z = 0.4  # insider fraction z typically 0.01 - 0.15

            def diff_func(z: float) -> float:
                sqrt1 = math.sqrt(z * z + 4.0 * (1.0 - z) * (pi[0] ** 2) / sum_pi)
                sqrt2 = math.sqrt(z * z + 4.0 * (1.0 - z) * (pi[1] ** 2) / sum_pi)
                return (sqrt1 + sqrt2) - (2.0 - z)

            # Bisection for z
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

            # True probabilities under Shin model
            denom = 2.0 * (1.0 - z_star)
            if denom <= 0:
                return cls.devig_multiplicative(raw_probs)

            p0 = (math.sqrt(z_star * z_star + 4.0 * (1.0 - z_star) * (pi[0] ** 2) / sum_pi) - z_star) / denom
            p1 = (math.sqrt(z_star * z_star + 4.0 * (1.0 - z_star) * (pi[1] ** 2) / sum_pi) - z_star) / denom

            total = p0 + p1
            return [float(p0 / total), float(p1 / total)]
        else:
            # Generalized multi-runner fallback to multiplicative
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
        Guarantees: prob_home + prob_away == 1.0000.
        """
        p_home_raw = cls.odds_to_implied_prob(odds_home)
        p_away_raw = cls.odds_to_implied_prob(odds_away)

        if method.lower() == "shin":
            devigged = cls.devig_shin([p_home_raw, p_away_raw])
        else:
            devigged = cls.devig_multiplicative([p_home_raw, p_away_raw])

        return round(devigged[0], 5), round(devigged[1], 5)

    # -------------------------------------------------------------------------
    # 3. Pod 2 Mid-Game Green Out Mathematics
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_green_out(
        entry_price_cents: int,
        current_yes_bid_cents: int,
        initial_contracts: int,
    ) -> dict[str, Any]:
        """
        Calculates exact Green-Out execution parameters when position rises +16c or more.

        Goal: Equal guaranteed profit regardless of final outcome (YES wins or NO wins).

        Method 1: Selling YES Contracts
        - Selling initial_contracts YES at current_yes_bid_cents yields guaranteed profit:
          Profit = initial_contracts * (current_yes_bid_cents - entry_price_cents) cents.
        - Taker fee if crossed, or Maker fee ($0.00) if limit order rests.

        Method 2: Hedging with opposing NO contracts
        - Holding initial_contracts of YES @ entry_price_cents.
        - Buying N_no contracts of NO @ (100 - current_yes_bid_cents).
        - To equalize payout: N_no = initial_contracts.
        - Guaranteed Net Profit = initial_contracts * (current_yes_bid_cents - entry_price_cents).

        Method 3: Free-Roll (Risk-Free)
        - Sell just enough YES contracts to recoup 100% of initial principal.
        - Number to sell = ceil((initial_contracts * entry_price_cents) / current_yes_bid_cents).
        - Remaining contracts are free-roll profit on YES with $0 risk on NO.
        """
        gain_cents = current_yes_bid_cents - entry_price_cents
        is_green_out_eligible = gain_cents >= 16

        # Guaranteed profit from full exit
        guaranteed_profit_cents = initial_contracts * gain_cents
        guaranteed_profit_dollars = guaranteed_profit_cents / 100.0

        # Method 2: NO hedge
        no_purchase_price_cents = 100 - current_yes_bid_cents
        no_contracts_to_buy = initial_contracts

        # Method 3: Free-roll contracts
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
            # Execution Plan A: Sell All YES via Maker Limit
            "sell_all_action": {
                "action": "sell",
                "side": "yes",
                "contracts": initial_contracts,
                "price_cents": current_yes_bid_cents,
                "post_only": True,
            },
            # Execution Plan B: Buy Equal Opposing NO
            "buy_opposing_no_action": {
                "action": "buy",
                "side": "no",
                "contracts": no_contracts_to_buy,
                "price_cents": no_purchase_price_cents,
                "post_only": True,
            },
            # Execution Plan C: Free-Roll
            "freeroll_action": {
                "contracts_to_sell": freeroll_contracts_to_sell,
                "remaining_free_yes": remaining_freeroll_yes,
                "max_payout_cents": freeroll_upside_if_yes_cents,
            },
        }

    # -------------------------------------------------------------------------
    # 4. Pod 3 Tail-Lock Harvester Probability Engine
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_tail_lock_probability(
        is_fourth_quarter: bool,
        seconds_remaining: int,
        score_margin: int,  # positive means leading
        has_possession: bool,
        down: Optional[int],
        opp_timeouts_remaining: int,
        espn_win_prob: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Mathematical verification for Pod 3 Tail-Lock Harvester.
        Contracts must be 95c to 97c AND mathematical win probability >= 99.2%.

        Physics / Clock Math for Football:
        - 40 second play clock run-off per down.
        - Kneel-down clock burn:
          If team has ball with 1st down and 3 kneel downs available,
          effective clock burn = (4 - down) * 40 seconds - (opp_timeouts * 40 seconds).
        - Multi-possession safety:
          - 9+ points (2 possessions) with < 120s remaining.
          - 17+ points (3 possessions) with < 300s remaining.
        """
        if not is_fourth_quarter or score_margin <= 0:
            return {
                "qualifies_tail_lock": False,
                "calculated_win_prob": 0.0,
                "reason": "Not leading in 4th quarter",
            }

        calculated_prob = 0.50

        # Scenario A: Kneel-Down Mathematical Lock (100% win prob)
        if has_possession and down is not None and 1 <= down <= 4:
            downs_available = 4 - down
            max_seconds_burnable = (downs_available * 40) - (opp_timeouts_remaining * 40)
            if seconds_remaining <= max_seconds_burnable:
                return {
                    "qualifies_tail_lock": True,
                    "calculated_win_prob": 1.000,
                    "reason": "Mathematical Kneel-Down Lock (opponent cannot stop clock)",
                }

        # Scenario B: Multi-score leads late in 4th quarter
        if score_margin >= 17:  # 3-possession game
            if seconds_remaining <= 300:  # <= 5 mins
                calculated_prob = 0.999
            elif seconds_remaining <= 600:
                calculated_prob = 0.995
        elif score_margin >= 9:  # 2-possession game
            if seconds_remaining <= 90:  # <= 1.5 mins
                calculated_prob = 0.998
            elif seconds_remaining <= 180:  # <= 3 mins
                calculated_prob = 0.994
            elif seconds_remaining <= 240 and opp_timeouts_remaining == 0:
                calculated_prob = 0.995
        elif score_margin >= 4:  # Touchdown needed
            if seconds_remaining <= 45 and not has_possession and opp_timeouts_remaining == 0:
                calculated_prob = 0.993
            elif seconds_remaining <= 60 and has_possession:
                calculated_prob = 0.994

        # Incorporate ESPN hidden win prob if provided
        final_prob = calculated_prob
        if espn_win_prob is not None:
            # Conservative consensus: take the minimum or weighted blend
            final_prob = min(calculated_prob, espn_win_prob) if calculated_prob > 0.99 else espn_win_prob

        qualifies = final_prob >= 0.992

        return {
            "qualifies_tail_lock": qualifies,
            "calculated_win_prob": round(final_prob, 4),
            "score_margin": score_margin,
            "seconds_remaining": seconds_remaining,
            "reason": "Win probability >= 99.2% confirmed" if qualifies else f"Prob {final_prob:.3f} below 99.2% threshold",
        }
