"""Vig removal for n-way decimal odds (GEMINI.md Law 4)."""

from __future__ import annotations


def implied_probs(decimal_odds: list[float]) -> list[float]:
    if not decimal_odds or any(o <= 1.0 for o in decimal_odds):
        raise ValueError(f"Invalid decimal odds: {decimal_odds}")
    return [1.0 / o for o in decimal_odds]


def devig_multiplicative(decimal_odds: list[float]) -> list[float]:
    imp = implied_probs(decimal_odds)
    total = sum(imp)
    return [p / total for p in imp]


def devig_power(decimal_odds: list[float], tol: float = 1e-10) -> list[float]:
    """Find k such that sum(p_i ** k) == 1. Corrects favourite-longshot bias better than multiplicative."""
    imp = implied_probs(decimal_odds)
    if abs(sum(imp) - 1.0) < tol:
        return imp
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2.0
        s = sum(p**k for p in imp)
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k  # need larger exponent to shrink probabilities
        else:
            hi = k
    out = [p**k for p in imp]
    total = sum(out)
    return [p / total for p in out]
