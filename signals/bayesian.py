"""
Bayesian Belief Updater for market probability estimation.

Core formula:
    P(H|D) = P(D|H) * P(H) / P(D)

Sequential log-space updates (numerically stable):
    log P(H|D1..Dk) = log P(H) + sum_k(log P(Dk|H)) - log Z

where Z is the normalisation constant.

For binary YES/NO markets we maintain:
    log_prob_yes  = log P(YES is the true outcome)
    log_prob_no   = log P(NO  is the true outcome)
"""

import math
import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """
    A single piece of evidence to feed to the Bayesian updater.

    Attributes
    ----------
    likelihood_yes : P(data | YES is true)  — probability of seeing this
                     signal given that YES is the correct outcome.
    likelihood_no  : P(data | NO  is true)  — probability of seeing this
                     signal given that NO  is the correct outcome.
    source         : human-readable label (e.g. "news", "volume_spike")
    """
    likelihood_yes: float
    likelihood_no: float
    source: str = "unknown"

    def __post_init__(self) -> None:
        if not (0 < self.likelihood_yes < 1):
            raise ValueError(
                f"likelihood_yes must be in (0,1), got {self.likelihood_yes} "
                f"(source={self.source})"
            )
        if not (0 < self.likelihood_no < 1):
            raise ValueError(
                f"likelihood_no must be in (0,1), got {self.likelihood_no} "
                f"(source={self.source})"
            )


# ---------------------------------------------------------------------------
# Bayesian updater
# ---------------------------------------------------------------------------

class BayesianUpdater:
    """
    Maintains a binary belief state (YES / NO) in log-space and supports
    sequential Bayesian updates from arbitrary evidence signals.

    Usage
    -----
    >>> updater = BayesianUpdater(prior_yes=0.5)
    >>> updater.update(Signal(likelihood_yes=0.8, likelihood_no=0.3, source="news"))
    >>> p_hat = updater.p_yes()
    """

    def __init__(self, prior_yes: float = 0.5) -> None:
        """
        Parameters
        ----------
        prior_yes : prior probability that the YES outcome is correct.
                    Must be strictly between 0 and 1.
        """
        if not (0 < prior_yes < 1):
            raise ValueError(f"prior_yes must be in (0,1), got {prior_yes}")

        self._prior_yes: float = prior_yes  # stored for decay_toward_prior
        self._log_prob_yes: float = math.log(prior_yes)
        self._log_prob_no: float = math.log(1.0 - prior_yes)
        self._num_updates: int = 0
        self._signal_history: List[Signal] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, signal: Signal) -> "BayesianUpdater":
        """
        Incorporate one evidence signal via Bayes' rule (in log-space).

        log P(H|D) ∝ log P(H) + log P(D|H)

        After updating both hypotheses, we renormalise by subtracting log Z
        (the log partition function) to keep them in valid log-probability space.

        Parameters
        ----------
        signal : Signal with likelihoods P(D|YES) and P(D|NO)

        Returns
        -------
        self (for chaining)
        """
        log_l_yes = math.log(signal.likelihood_yes)
        log_l_no = math.log(signal.likelihood_no)

        # Unnormalised log posteriors
        log_unnorm_yes = self._log_prob_yes + log_l_yes
        log_unnorm_no = self._log_prob_no + log_l_no

        # Log-sum-exp for normalisation: log Z = log(exp(a) + exp(b))
        log_z = _log_sum_exp(log_unnorm_yes, log_unnorm_no)

        self._log_prob_yes = log_unnorm_yes - log_z
        self._log_prob_no = log_unnorm_no - log_z

        self._num_updates += 1
        self._signal_history.append(signal)

        logger.debug(
            "Bayesian update #%d from '%s': "
            "L(YES)=%.4f, L(NO)=%.4f -> P(YES)=%.4f",
            self._num_updates,
            signal.source,
            signal.likelihood_yes,
            signal.likelihood_no,
            self.p_yes(),
        )
        return self

    def update_batch(self, signals: List[Signal]) -> "BayesianUpdater":
        """
        Apply multiple signals sequentially (order matters if non-independent).

        Parameters
        ----------
        signals : list of Signal evidence items

        Returns
        -------
        self (for chaining)
        """
        for s in signals:
            self.update(s)
        return self

    def p_yes(self) -> float:
        """Return current posterior probability of the YES outcome."""
        return math.exp(self._log_prob_yes)

    def p_no(self) -> float:
        """Return current posterior probability of the NO outcome."""
        return math.exp(self._log_prob_no)

    def log_odds(self) -> float:
        """
        Log-odds of YES over NO.

        log_odds = log(P(YES) / P(NO)) = log_prob_yes - log_prob_no

        Positive values favour YES; negative values favour NO.
        """
        return self._log_prob_yes - self._log_prob_no

    def decay_toward_prior(self, decay_rate: float = 0.05) -> "BayesianUpdater":
        """
        Pull the current belief toward the configured prior at the given rate.

        Prevents stale beliefs from accumulating across many cycles in which no
        meaningful new signal arrives.  Should be called once per evaluation
        cycle regardless of whether a signal was applied.

        A decay_rate of 0.05 moves the belief 5% of the way from its current
        log-probability back toward the prior on each call.  At decay_rate=0
        this is a no-op; at decay_rate=1 the belief is immediately reset to the
        prior.

        Parameters
        ----------
        decay_rate : float in [0, 1]. Values near 0 preserve beliefs longer;
                     values near 1 forget signal history almost immediately.

        Returns
        -------
        self (for chaining)
        """
        if decay_rate <= 0.0:
            return self
        decay_rate = min(1.0, decay_rate)

        prior_log_yes = math.log(self._prior_yes)
        prior_log_no = math.log(1.0 - self._prior_yes)

        # Linearly interpolate log-probs toward the prior
        new_log_yes = (1.0 - decay_rate) * self._log_prob_yes + decay_rate * prior_log_yes
        new_log_no = (1.0 - decay_rate) * self._log_prob_no + decay_rate * prior_log_no

        # Renormalise so exp(log_prob_yes) + exp(log_prob_no) == 1
        log_z = _log_sum_exp(new_log_yes, new_log_no)
        self._log_prob_yes = new_log_yes - log_z
        self._log_prob_no = new_log_no - log_z

        return self

    def reset(self, prior_yes: float = 0.5) -> "BayesianUpdater":
        """Reset to a fresh prior, discarding all signal history."""
        self.__init__(prior_yes)
        return self

    @property
    def num_updates(self) -> int:
        """Number of update steps applied so far."""
        return self._num_updates

    @property
    def signal_history(self) -> List[Signal]:
        """Read-only view of all signals applied in order."""
        return list(self._signal_history)

    def expected_value(self, market_price: float) -> float:
        """
        Compute EV = p_hat - market_price.

        Positive EV → buy YES (we think YES is underpriced).
        Negative EV → buy NO  (we think NO  is underpriced).

        Parameters
        ----------
        market_price : current market price for the YES token (0 to 1)

        Returns
        -------
        float : expected value per unit of stake
        """
        return self.p_yes() - market_price

    def to_dict(self) -> dict:
        """Serialize belief state for persistence (excludes signal history)."""
        return {
            "prior_yes":    self._prior_yes,
            "log_prob_yes": self._log_prob_yes,
            "log_prob_no":  self._log_prob_no,
            "num_updates":  self._num_updates,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BayesianUpdater":
        """Restore a BayesianUpdater from a serialized dict."""
        obj = cls.__new__(cls)
        obj._prior_yes     = float(d["prior_yes"])
        obj._log_prob_yes  = float(d["log_prob_yes"])
        obj._log_prob_no   = float(d["log_prob_no"])
        obj._num_updates   = int(d.get("num_updates", 0))
        obj._signal_history = []
        return obj

    def __repr__(self) -> str:
        return (
            f"BayesianUpdater(p_yes={self.p_yes():.4f}, "
            f"p_no={self.p_no():.4f}, "
            f"updates={self._num_updates})"
        )


# ---------------------------------------------------------------------------
# Signal factory helpers
# ---------------------------------------------------------------------------

def signal_from_price_movement(
    price_change: float,
    confidence: float = 0.65,
    magnitude_scale: float = 0.05,
) -> Signal:
    """
    Convert a raw price movement into a Bayesian signal.

    A positive price_change (market moved up for YES) suggests bullish
    information; we treat this as moderate evidence in favour of YES.

    Confidence is now *scaled by the magnitude* of the price change:
    a move of size `magnitude_scale` (default 5 pp) receives the full
    `confidence` weight; smaller moves receive proportionally weaker
    weight.  This prevents micro-fluctuations from accumulating into
    strong, spurious beliefs.

    Parameters
    ----------
    price_change     : observed change in YES price (e.g. +0.05 = moved up 5pp)
    confidence       : maximum signal strength (0.5=neutral, 1.0=certain)
    magnitude_scale  : price change that maps to full `confidence`
                       (default 0.05 = 5 percentage points)

    Returns
    -------
    Signal with appropriate likelihoods
    """
    if price_change == 0.0:
        # Neutral: no information
        return Signal(likelihood_yes=0.5, likelihood_no=0.5, source="price_movement")

    confidence = max(0.501, min(0.999, confidence))  # clamp away from extremes

    # Scale confidence linearly with magnitude, capped at the full confidence level
    magnitude_scale = max(1e-6, magnitude_scale)
    scale_factor = min(1.0, abs(price_change) / magnitude_scale)
    scaled_confidence = 0.5 + (confidence - 0.5) * scale_factor
    # Re-clamp after scaling (scale_factor < 1 could produce values below 0.501)
    scaled_confidence = max(0.501, min(0.999, scaled_confidence))

    if price_change > 0:
        # Bullish signal: more likely to see this if YES is true
        l_yes = scaled_confidence
        l_no = 1.0 - scaled_confidence
    else:
        # Bearish signal: more likely to see this if NO is true
        l_yes = 1.0 - scaled_confidence
        l_no = scaled_confidence

    return Signal(
        likelihood_yes=l_yes,
        likelihood_no=l_no,
        source="price_movement",
    )


def signal_from_volume_spike(
    volume_ratio: float,
    confidence: float = 0.6,
) -> Signal:
    """
    Convert a volume spike into a Bayesian signal.

    Volume spikes alone carry no directional information — unusual activity
    suggests something is happening but doesn't indicate YES or NO.  Returns
    a neutral signal (likelihood_yes == likelihood_no).  The confidence and
    volume_ratio parameters are accepted for API consistency but unused.

    Parameters
    ----------
    volume_ratio : current volume / average volume (e.g. 3.0 = 3x normal)
    confidence   : unused — volume spikes are always direction-neutral

    Returns
    -------
    Signal with likelihood_yes == likelihood_no == 0.5
    """
    return Signal(likelihood_yes=0.5, likelihood_no=0.5, source="volume_spike")


def signal_from_sentiment(
    sentiment_score: float,
    confidence: float = 0.65,
) -> Signal:
    """
    Convert a sentiment score into a Bayesian signal.

    Parameters
    ----------
    sentiment_score : float in [-1, 1], positive = bullish for YES
    confidence      : how much we trust this signal

    Returns
    -------
    Signal
    """
    confidence = max(0.501, min(0.999, confidence))
    normalized = (sentiment_score + 1.0) / 2.0  # map [-1,1] -> [0,1]
    l_yes = 0.5 + (normalized - 0.5) * (confidence - 0.5) * 2
    # Clamp to (0,1) exclusive — do NOT clamp to 0.501+ here, as that would
    # prevent bearish signals from yielding l_yes < l_no.
    l_yes = max(0.001, min(0.999, l_yes))
    l_no = 1.0 - l_yes
    return Signal(
        likelihood_yes=l_yes,
        likelihood_no=l_no,
        source="sentiment",
    )


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _log_sum_exp(a: float, b: float) -> float:
    """
    Numerically stable log(exp(a) + exp(b)).

    Uses the identity: log(exp(a) + exp(b)) = max(a,b) + log(1 + exp(min-max))
    """
    if a >= b:
        return a + math.log1p(math.exp(b - a))
    else:
        return b + math.log1p(math.exp(a - b))
