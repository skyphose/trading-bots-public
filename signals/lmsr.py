"""
LMSR (Logarithmic Market Scoring Rule) pricing engine.

Implements:
  - Cost function:    C(q) = b * ln(sum(e^(qi/b)))
  - Price function:   pi(q) = e^(qi/b) / sum(e^(qj/b))   [softmax]
  - Trade cost:       cost_of_trade = C(q_after) - C(q_before)
  - Inefficiency signal: EV > min_ev threshold
"""

import math
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core LMSR math
# ---------------------------------------------------------------------------

def lmsr_cost(q: List[float], b: float) -> float:
    """
    LMSR cost function: C(q) = b * ln(sum_i exp(qi / b))

    Parameters
    ----------
    q : list of share quantities for each outcome (e.g. [q_yes, q_no])
    b : liquidity parameter (higher b = less price impact)

    Returns
    -------
    float : the market maker's cost (total liability)
    """
    if b <= 0:
        raise ValueError(f"Liquidity parameter b must be positive, got {b}")
    # Use log-sum-exp trick for numerical stability
    scaled = [qi / b for qi in q]
    max_scaled = max(scaled)
    log_sum = max_scaled + math.log(sum(math.exp(s - max_scaled) for s in scaled))
    return b * log_sum


def lmsr_prices(q: List[float], b: float) -> List[float]:
    """
    LMSR price function (softmax):
        pi(q) = exp(qi/b) / sum_j(exp(qj/b))

    Returns a probability distribution over outcomes.

    Parameters
    ----------
    q : list of share quantities per outcome
    b : liquidity parameter

    Returns
    -------
    list of floats : prices (probabilities) for each outcome, sums to 1.0
    """
    if b <= 0:
        raise ValueError(f"Liquidity parameter b must be positive, got {b}")
    scaled = [qi / b for qi in q]
    max_scaled = max(scaled)
    exp_shifted = [math.exp(s - max_scaled) for s in scaled]
    total = sum(exp_shifted)
    return [e / total for e in exp_shifted]


def lmsr_trade_cost(
    q: List[float],
    outcome_index: int,
    delta: float,
    b: float,
) -> float:
    """
    Cost to purchase `delta` shares of outcome `outcome_index`.

    trade_cost = C(q1, ..., qi + delta, ..., qn) - C(q1, ..., qi, ..., qn)

    A positive delta means buying; negative means selling.
    Returns the cost in numeraire (USDC). Positive = you pay, negative = you receive.

    Parameters
    ----------
    q             : current share quantities for all outcomes
    outcome_index : which outcome to trade (0 = YES, 1 = NO for binary markets)
    delta         : shares to buy (positive) or sell (negative)
    b             : liquidity parameter

    Returns
    -------
    float : cost of the trade in USDC
    """
    if outcome_index < 0 or outcome_index >= len(q):
        raise IndexError(f"outcome_index {outcome_index} out of range for q of len {len(q)}")

    q_before = list(q)
    q_after = list(q)
    q_after[outcome_index] += delta

    cost_before = lmsr_cost(q_before, b)
    cost_after = lmsr_cost(q_after, b)
    return cost_after - cost_before


def effective_price(
    q: List[float],
    outcome_index: int,
    delta: float,
    b: float,
) -> float:
    """
    Effective (average) price per share for buying `delta` shares.

    effective_price = trade_cost / delta

    This is always >= the current instantaneous price for buys (price impact).

    Parameters
    ----------
    q             : current share quantities
    outcome_index : outcome to trade
    delta         : number of shares (must be != 0)
    b             : liquidity parameter

    Returns
    -------
    float : effective price per share
    """
    if delta == 0:
        raise ValueError("delta must be non-zero to compute effective price")
    return lmsr_trade_cost(q, outcome_index, delta, b) / delta


# ---------------------------------------------------------------------------
# Inefficiency / EV signal detection
# ---------------------------------------------------------------------------

def compute_ev(p_hat: float, market_price: float) -> float:
    """
    Expected value of buying YES at `market_price` given our estimate `p_hat`.

    EV = p_hat - market_price

    Positive EV means YES is underpriced; negative means YES is overpriced
    (and we should consider buying NO instead).

    Parameters
    ----------
    p_hat        : our Bayesian-estimated true probability of YES
    market_price : current market price of YES (0 to 1)

    Returns
    -------
    float : expected value per dollar risked (approximate)
    """
    return p_hat - market_price


def detect_inefficiency(
    p_hat: float,
    market_price: float,
    min_ev: float,
    q: List[float],
    b: float,
    delta_probe: float = 10.0,
) -> Tuple[bool, str, float]:
    """
    Check whether the market offers a tradeable edge.

    Returns a (signal, side, ev) triple:
      - signal : True if an actionable edge was found
      - side   : "YES" or "NO" (which token to buy)
      - ev     : magnitude of the expected value

    The check also verifies that price impact for a probe trade of `delta_probe`
    shares does not destroy the edge.

    Parameters
    ----------
    p_hat        : estimated true probability of YES
    market_price : current LMSR YES price
    min_ev       : minimum EV threshold to consider a trade
    q            : current share quantities [q_yes, q_no]
    b            : liquidity parameter
    delta_probe  : shares to probe for price-impact sanity check
    """
    ev_yes = compute_ev(p_hat, market_price)
    ev_no = compute_ev(1.0 - p_hat, 1.0 - market_price)  # for NO token

    # Determine which side has the edge
    if ev_yes >= min_ev:
        side = "YES"
        ev = ev_yes
        outcome_idx = 0
    elif ev_no >= min_ev:
        side = "NO"
        ev = ev_no
        outcome_idx = 1
    else:
        return False, "NONE", max(ev_yes, ev_no)

    # Sanity-check: effective price after buying `delta_probe` shares
    eff_price = effective_price(q, outcome_idx, delta_probe, b)
    our_prob = p_hat if side == "YES" else (1.0 - p_hat)

    if eff_price >= our_prob:
        logger.debug(
            "Edge found (EV=%.4f on %s) but price impact erases it "
            "(eff_price=%.4f >= p_hat=%.4f). Skipping.",
            ev, side, eff_price, our_prob,
        )
        return False, side, ev

    logger.info(
        "Inefficiency detected: side=%s, EV=%.4f, market_price=%.4f, "
        "p_hat=%.4f, eff_price_probe=%.4f",
        side, ev, market_price, p_hat, eff_price,
    )
    return True, side, ev


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def implied_quantities(yes_price: float, b: float) -> List[float]:
    """
    Back-solve the share quantities [q_yes, q_no] that produce the given
    YES price under LMSR.

    By convention we pin q_no = 0 and solve for q_yes:
        p_yes = sigmoid((q_yes - q_no) / b) = sigmoid(q_yes / b)
    So: q_yes = b * ln(p_yes / (1 - p_yes))   (log-odds)

    Parameters
    ----------
    yes_price : current YES price in (0, 1)
    b         : liquidity parameter

    Returns
    -------
    [q_yes, q_no] with q_no fixed at 0
    """
    if not (0 < yes_price < 1):
        raise ValueError(f"yes_price must be strictly in (0,1), got {yes_price}")
    q_yes = b * math.log(yes_price / (1.0 - yes_price))
    return [q_yes, 0.0]
