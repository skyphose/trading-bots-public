"""
Fractional Kelly position sizing for binary prediction markets.

Kelly criterion:
    f* = (p * b_odds - q) / b_odds
       = (p * (1/price - 1) - (1-p)) / (1/price - 1)

Simplified for binary markets where payout = 1/price per dollar staked:
    For YES bets:  f = (p_hat - p) / (1 - p)
    For NO  bets:  f = ((1-p_hat) - (1-p)) / p   =  (p - p_hat) / p

We then scale by kelly_fraction (default 0.25) and apply risk caps.

Reference: Kelly (1956), "A New Interpretation of Information Rate"
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def minimum_tradeable_edge(side: str, market_price: float, bankroll: float, min_order_size: float, kelly_fraction: float) -> Optional[float]:
    """Return the minimum EV needed for Kelly sizing to reach min_order_size."""
    if bankroll <= 0 or min_order_size <= 0 or kelly_fraction <= 0:
        return None

    required_scaled_kelly = min_order_size / bankroll
    required_raw_kelly = required_scaled_kelly / kelly_fraction
    if required_raw_kelly <= 0:
        return 0.0

    side = side.upper()
    if side == "YES":
        return required_raw_kelly * max(1e-9, 1.0 - market_price)
    if side == "NO":
        return required_raw_kelly * max(1e-9, market_price)
    raise ValueError(f"side must be 'YES' or 'NO', got '{side}'")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SizingResult:
    """Output of a position-sizing calculation."""
    side: str                   # "YES" or "NO"
    kelly_fraction_raw: float   # Full Kelly fraction (before scaling)
    kelly_fraction_used: float  # Fractional Kelly used (e.g. 0.25 * raw)
    stake_usdc: float           # Dollar amount to wager (post all caps)
    shares: float               # Approximate shares to buy at market_price
    market_price: float         # Price of the token being bought
    p_hat: float                # Our estimated probability
    ev: float                   # Expected value (p_hat - market_price or equiv)
    capped: bool                # True if position was capped by a risk limit
    cap_reason: Optional[str]   # Which limit was hit, if any


# ---------------------------------------------------------------------------
# Core Kelly math
# ---------------------------------------------------------------------------

def kelly_fraction_yes(p_hat: float, price: float) -> float:
    """
    Fractional Kelly for a YES bet on a binary market.

    f = (p_hat - price) / (1 - price)

    Derivation: on Polymarket you pay `price` per share and receive 1.0 if
    correct, so the net gain per dollar is (1 - price) / price (the "odds").
    Standard Kelly: f = (p * b - q) / b where b = (1-price)/price and q=1-p.
    Simplifying gives the formula above.

    Parameters
    ----------
    p_hat : our estimated probability of YES
    price : current market price of YES (0 to 1)

    Returns
    -------
    float : Kelly fraction in [0, 1] (negative values clamped to 0 — no bet)
    """
    if price <= 0 or price >= 1:
        return 0.0
    denominator = 1.0 - price
    if denominator < 1e-9:
        return 0.0
    f = (p_hat - price) / denominator
    return max(0.0, f)


def kelly_fraction_no(p_hat: float, price_yes: float) -> float:
    """
    Fractional Kelly for a NO bet on a binary market.

    Buying NO at price (1 - price_yes) when we believe YES has probability p_hat:
        f = ((1 - p_hat) - (1 - price_yes)) / price_yes
          = (price_yes - p_hat) / price_yes

    Parameters
    ----------
    p_hat     : our estimated probability of YES (so P(NO) = 1 - p_hat)
    price_yes : current market price of YES token

    Returns
    -------
    float : Kelly fraction in [0, 1]
    """
    price_no = 1.0 - price_yes
    if price_no <= 0 or price_no >= 1:
        return 0.0
    p_no = 1.0 - p_hat
    denominator = 1.0 - price_no  # = price_yes
    if denominator < 1e-9:
        return 0.0
    f = (p_no - price_no) / denominator
    return max(0.0, f)


# ---------------------------------------------------------------------------
# Main sizing function
# ---------------------------------------------------------------------------

def compute_position_size(
    side: str,
    p_hat: float,
    market_price: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_position_size: float = 100.0,
    per_market_exposure_cap: float = 50.0,
    current_market_exposure: float = 0.0,
    min_order_size: float = 1.0,
) -> Optional[SizingResult]:
    """
    Compute the optimal fractional-Kelly position size for a trade.

    Steps:
        1. Compute full Kelly fraction for the given side.
        2. Scale by kelly_fraction (e.g. 0.25 for quarter Kelly).
        3. Convert fraction to USDC amount: stake = f_scaled * bankroll.
        4. Apply risk caps: max_position_size, per_market_exposure_cap.
        5. Return None if stake < min_order_size (trade not worth placing).

    Parameters
    ----------
    side                    : "YES" or "NO"
    p_hat                   : our estimated probability of YES
    market_price            : current market price of YES (0 to 1)
    bankroll                : available trading capital in USDC
    kelly_fraction          : fraction of Kelly to use (default 0.25)
    max_position_size       : hard cap on a single trade in USDC
    per_market_exposure_cap : max total exposure per market in USDC
    current_market_exposure : current exposure in this market (USDC)
    min_order_size          : minimum trade size (below this we skip)

    Returns
    -------
    SizingResult if a trade is warranted, else None
    """
    side = side.upper()
    if side not in ("YES", "NO"):
        raise ValueError(f"side must be 'YES' or 'NO', got '{side}'")

    # 1. Compute raw (full) Kelly fraction
    if side == "YES":
        raw_kelly = kelly_fraction_yes(p_hat, market_price)
        ev = p_hat - market_price
        token_price = market_price
    else:
        raw_kelly = kelly_fraction_no(p_hat, market_price)
        ev = (1.0 - p_hat) - (1.0 - market_price)
        token_price = 1.0 - market_price

    if raw_kelly <= 0:
        logger.debug("Kelly fraction <= 0 for %s side (raw=%.4f). No trade.", side, raw_kelly)
        return None

    # 2. Apply fractional Kelly scaling
    # Bounded-price discount: prediction market prices are capped at 0–1,
    # so standard Kelly over-sizes bets near the extremes where payouts are
    # asymmetric and liquidity is thin. Discount linearly from 1.0× at p=0.85
    # to 0.0× at p=1.0 (and symmetrically for low prices).
    p_token = p_hat if side == "YES" else (1.0 - p_hat)
    boundary_discount = 1.0 - max(0.0, (p_token - 0.85) / 0.15)
    scaled_kelly = raw_kelly * kelly_fraction * boundary_discount

    # 3. Convert to USDC amount
    stake = scaled_kelly * bankroll

    # 4. Apply risk caps
    capped = False
    cap_reason = None

    if stake > max_position_size:
        logger.debug(
            "Stake %.2f USDC exceeds max_position_size=%.2f; capping.",
            stake, max_position_size,
        )
        stake = max_position_size
        capped = True
        cap_reason = "max_position_size"

    # Check per-market exposure cap
    remaining_market_capacity = per_market_exposure_cap - current_market_exposure
    if remaining_market_capacity <= 0:
        logger.info(
            "Market exposure cap reached (%.2f / %.2f USDC). Skipping trade.",
            current_market_exposure, per_market_exposure_cap,
        )
        return None

    if stake > remaining_market_capacity:
        logger.debug(
            "Stake %.2f USDC would exceed per_market_exposure_cap; "
            "reducing to %.2f USDC.",
            stake, remaining_market_capacity,
        )
        stake = remaining_market_capacity
        capped = True
        cap_reason = cap_reason or "per_market_exposure_cap"

    # 5. Check minimum order size
    if stake < min_order_size:
        logger.debug(
            "Stake %.4f USDC is below min_order_size=%.2f. Skipping.",
            stake, min_order_size,
        )
        return None

    # Approximate shares: stake / token_price
    shares = stake / token_price if token_price > 0 else 0.0

    result = SizingResult(
        side=side,
        kelly_fraction_raw=raw_kelly,
        kelly_fraction_used=scaled_kelly,
        stake_usdc=stake,
        shares=shares,
        market_price=market_price,
        p_hat=p_hat,
        ev=ev,
        capped=capped,
        cap_reason=cap_reason,
    )

    logger.info(
        "Sizing: side=%s, stake=%.2f USDC (~%.2f shares @ %.4f), "
        "full_kelly=%.4f, scaled_kelly=%.4f, EV=%.4f%s",
        result.side,
        result.stake_usdc,
        result.shares,
        result.market_price,
        result.kelly_fraction_raw,
        result.kelly_fraction_used,
        result.ev,
        f" [CAPPED: {result.cap_reason}]" if result.capped else "",
    )
    return result


# ---------------------------------------------------------------------------
# Time-to-close Kelly adjustment
# ---------------------------------------------------------------------------

def ttl_kelly_multiplier(p_hat: float, seconds_to_close: int) -> float:
    """
    Adjust Kelly fraction based on time-to-close and p_hat.

    Academic research shows favorites are systematically underpriced near expiry
    while longshots are overpriced. We boost Kelly for high-conviction bets as
    TTL shrinks, and discount for longshots near expiry.

    sqrt(T) shape matches empirical binary price volatility — more gradual in
    mid-life, steeper near expiry. Window extended to 720h (30 days) to cover
    the full recommended holding period.

      p_hat >= 0.72:  up to 1.25× at 6h (favorite underpricing bias)
      p_hat <= 0.28:  down to 0.60× at 6h (longshot overpricing bias)
      otherwise:      1.0× (no adjustment)
    """
    HOURS_FULL = 6.0     # max adjustment at ≤6h to close
    HOURS_NONE = 720.0   # no adjustment beyond 30 days
    hours = seconds_to_close / 3600.0
    if hours >= HOURS_NONE:
        return 1.0
    # t: 0.0 at 720h, 1.0 at 6h — sqrt shape matches binary price vol decay
    t = max(0.0, min(1.0, math.sqrt((HOURS_NONE - hours) / (HOURS_NONE - HOURS_FULL))))
    if p_hat >= 0.72:
        return 1.0 + t * 0.25       # up to 1.25×
    if p_hat <= 0.28:
        return 1.0 - t * 0.40       # down to 0.60×
    return 1.0


# ---------------------------------------------------------------------------
# Utility: bankroll management helpers
# ---------------------------------------------------------------------------

def max_loss_check(
    daily_pnl: float,
    daily_loss_limit: float,
) -> bool:
    """
    Return True if the bot should continue trading (daily loss not exceeded).

    Parameters
    ----------
    daily_pnl        : current day's realised PnL (negative = loss)
    daily_loss_limit : maximum allowed daily loss (positive number)

    Returns
    -------
    bool : True = OK to trade, False = halt
    """
    if daily_pnl <= -abs(daily_loss_limit):
        logger.warning(
            "Daily loss limit reached: PnL=%.2f USDC (limit=%.2f USDC). "
            "Bot will halt for today.",
            daily_pnl, daily_loss_limit,
        )
        return False
    return True


def exposure_check(
    current_total_exposure: float,
    max_total_exposure: float,
) -> bool:
    """
    Return True if we have remaining capacity for more exposure.

    Parameters
    ----------
    current_total_exposure : sum of all open positions (USDC)
    max_total_exposure     : configured portfolio exposure cap (USDC)

    Returns
    -------
    bool : True = capacity available, False = at or above cap
    """
    if current_total_exposure >= max_total_exposure:
        logger.warning(
            "Total exposure cap reached: %.2f / %.2f USDC. Skipping new trades.",
            current_total_exposure, max_total_exposure,
        )
        return False
    return True
