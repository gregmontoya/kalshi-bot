"""Institutional CLV Truth Meter and Nightly Exponential Decay Brier Score Grader.

Features:
1. Closing Line Value (CLV) Truth Meter: Evaluates price paid vs Kalshi closing price 1 min before kickoff.
2. Nightly 3:00 AM Self-Learning Grader:
   - Computes weighted Brier Score with Exponential Decay:
     60% weight for last 3 games, 30% for games 4-6, 10% for older games.
   - Adjusts strategy confidence weights dynamically.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Optional
import numpy as np

logger = logging.getLogger(__name__)


class InstitutionalCLVGrader:
    """Computes Closing Line Value and Exponential Decay Brier calibration."""

    @staticmethod
    def calculate_clv(entry_price_cents: int, closing_price_cents: int) -> dict[str, Any]:
        """
        CLV Truth Meter:
        CLV = (Closing Price / Entry Price) - 1.0
        Beating the closing line proves mathematical +EV before kickoff.
        """
        if entry_price_cents <= 0:
            return {"clv_pct": 0.0, "has_positive_clv": False}

        clv = (closing_price_cents / entry_price_cents) - 1.0
        return {
            "entry_price_cents": entry_price_cents,
            "closing_price_cents": closing_price_cents,
            "clv_pct": round(clv * 100.0, 2),
            "has_positive_clv": clv > 0.0,
        }

    @staticmethod
    def calculate_exponential_decay_brier_score(
        trades: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Nightly 3:00 AM Brier Score with Exponential Decay Weighting:
        - 60% weight to last 3 games
        - 30% weight to games 4-6
        - 10% weight to older games
        BS = sum(w_i * (forecast_i - outcome_i)^2) / sum(w_i)
        """
        if not trades:
            return {"weighted_brier_score": 0.25, "sample_size": 0, "confidence_weight": 0.50}

        sorted_trades = sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)
        squared_errors = []
        weights = []

        for idx, t in enumerate(sorted_trades):
            f = float(t.get("predicted_prob", 0.50))
            o = 1.0 if t.get("outcome") in ("win", "yes", 1) else 0.0
            sq_err = (f - o) ** 2

            # Exponential decay bucket
            if idx < 3:
                w = 0.60 / min(3, len(sorted_trades))
            elif idx < 6:
                w = 0.30 / min(3, max(1, len(sorted_trades) - 3))
            else:
                w = 0.10 / max(1, len(sorted_trades) - 6)

            squared_errors.append(sq_err)
            weights.append(w)

        weighted_bs = sum(e * w for e, w in zip(squared_errors, weights)) / sum(weights)

        # Dynamic strategy confidence weight (lower Brier Score = higher confidence)
        confidence_weight = max(0.20, min(1.0, 1.0 - (weighted_bs * 2.0)))

        return {
            "weighted_brier_score": round(weighted_bs, 4),
            "sample_size": len(trades),
            "confidence_weight": round(confidence_weight, 2),
            "unweighted_brier_score": round(sum(squared_errors) / len(squared_errors), 4),
        }
