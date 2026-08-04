"""
shared/devig.py — remove bookmaker vig from two-sided odds to recover fair
probabilities. Zero dependencies (pure math), so it runs identically in every
bot container.

A sportsbook's two-sided implied probabilities sum to >1 (the "overround" /
vig). Devigging redistributes that excess to estimate the true probabilities.
Three standard methods:

  - proportional : scale each side by 1/overround. Simplest; slightly
                   over-favors the favorite.
  - power        : find k such that p_i**k sum to 1. Corrects the
                   favorite-longshot bias better than proportional.
  - shin         : Shin (1993) model of insider trading; industry standard for
                   the sharpest correction. Solves for the insider fraction z.

Usage:
    from devig import devig_two_way, american_to_prob
    p_yes, p_no = devig_two_way(american_to_prob(-150), american_to_prob(+130))
"""
from __future__ import annotations

from typing import Tuple


def american_to_prob(odds: float) -> float:
    """American odds → raw implied probability (still contains vig)."""
    if odds < 0:
        return (-odds) / ((-odds) + 100.0)
    return 100.0 / (odds + 100.0)


def decimal_to_prob(odds: float) -> float:
    """Decimal odds → raw implied probability (still contains vig)."""
    if odds <= 1.0:
        raise ValueError(f"decimal odds must be > 1.0, got {odds}")
    return 1.0 / odds


def _proportional(imp_a: float, imp_b: float) -> Tuple[float, float]:
    total = imp_a + imp_b
    return imp_a / total, imp_b / total


def _power(imp_a: float, imp_b: float, tol: float = 1e-9) -> Tuple[float, float]:
    # Solve for k with p_a**k + p_b**k == 1 via bisection. k > 1 shifts mass
    # away from the favorite (corrects favorite-longshot bias).
    lo, hi = 0.5, 5.0
    for _ in range(100):
        k = (lo + hi) / 2.0
        s = imp_a ** k + imp_b ** k
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k          # need larger k to shrink the sum
        else:
            hi = k
    return imp_a ** k, imp_b ** k


def _shin(imp_a: float, imp_b: float, tol: float = 1e-12) -> Tuple[float, float]:
    # Shin: given booked probs pi, solve for insider fraction z in [0, 1) s.t.
    #   sum_i [ sqrt(z**2 + 4*(1-z)*pi_i**2 / booksum) - z ] / (2*(1-z)) == 1
    # then fair_i is that bracket term. For a two-way book this has a clean
    # bisection on z.
    booksum = imp_a + imp_b

    def fair(z: float, pi: float) -> float:
        return (((z * z + 4 * (1 - z) * pi * pi / booksum) ** 0.5) - z) / (2 * (1 - z))

    lo, hi = 0.0, 0.999999
    for _ in range(200):
        z = (lo + hi) / 2.0
        s = fair(z, imp_a) + fair(z, imp_b)
        if abs(s - 1.0) < tol:
            break
        # higher z removes more vig → smaller sum; we want sum == 1
        if s > 1.0:
            lo = z
        else:
            hi = z
    fa, fb = fair(z, imp_a), fair(z, imp_b)
    total = fa + fb                      # renormalize residual
    return fa / total, fb / total


def devig_two_way(imp_a: float, imp_b: float, method: str = "shin") -> Tuple[float, float]:
    """
    Remove vig from two raw implied probabilities. Returns (fair_a, fair_b)
    summing to 1.0. `method` in {"proportional", "power", "shin"}.
    """
    if imp_a <= 0 or imp_b <= 0:
        raise ValueError(f"implied probs must be > 0, got {imp_a}, {imp_b}")
    if imp_a + imp_b <= 1.0:
        # No vig (or an arb); proportional normalize is the honest answer.
        return _proportional(imp_a, imp_b)
    if method == "proportional":
        return _proportional(imp_a, imp_b)
    if method == "power":
        return _power(imp_a, imp_b)
    if method == "shin":
        return _shin(imp_a, imp_b)
    raise ValueError(f"unknown devig method: {method!r}")
