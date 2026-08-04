"""
Shared gate framework: GateResult, EvalContext, Gate ABC, GateChain.

Replaces the 18-stage if/else/audit spaghetti in each bot's market evaluation
with a composable chain of named Gate subclasses.  Each gate makes a single
decision; the chain stops at the first rejection.

Usage
-----
    chain = GateChain([SpreadGate(), EVGate(), YesDisabledGate(), NOFloorGate()])
    result = chain.run(ctx)
    if not result.passed:
        log.debug("Rejected by %s: %s", result.gate, result.reason)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateResult:
    """Outcome of a single gate check."""
    passed: bool
    gate: str
    reason: str | None = None

    @classmethod
    def pass_(cls, gate: str) -> "GateResult":
        return cls(passed=True, gate=gate)

    @classmethod
    def reject(cls, gate: str, reason: str) -> "GateResult":
        return cls(passed=False, gate=gate, reason=reason)


@dataclass
class EvalContext:
    """
    All information a gate might need to evaluate one market opportunity.

    Fields are populated progressively as the evaluation pipeline advances:
    - condition_id / question / config are always set
    - prices fields (yes_mid etc.) are set after orderbook fetch
    - p_hat / signal_count after Bayesian update
    - has_edge / side / ev after LMSR detection
    - stake after Kelly sizing
    """
    # --- Identity ---
    condition_id: str = ""
    question: str = ""

    # --- Market prices ---
    yes_mid: float = 0.5
    no_mid: float = 0.5
    fair_yes_mid: float = 0.5
    spread: float = 0.0

    # --- Bayesian posterior (post signal application) ---
    p_hat: float = 0.5
    signal_count: int = 0
    signal_names: list[str] = field(default_factory=list)

    # --- Edge detection ---
    has_edge: bool = False
    side: str = ""        # "YES" or "NO"
    ev: float = 0.0

    # --- Calibration ---
    tier: str = ""         # "HIGH", "MID", "LOW"

    # --- Sizing ---
    stake: float | None = None

    # --- Risk state (mutable during evaluation) ---
    total_exposure: float = 0.0
    market_exposure: float = 0.0

    # --- Config (pass in bot's config object) ---
    config: Any = None


class Gate(ABC):
    """
    Single-responsibility decision unit.

    Subclass and implement check().  The gate name is used in audit records
    and rejection logs — keep it short and descriptive.
    """
    name: str = "unnamed_gate"

    @abstractmethod
    def check(self, ctx: EvalContext) -> GateResult:
        """
        Evaluate one aspect of a trade opportunity.

        Return GateResult.pass_(self.name) to allow the trade to continue.
        Return GateResult.reject(self.name, reason) to stop evaluation.
        """
        ...


class GateChain:
    """
    Ordered sequence of gates evaluated left-to-right.

    run() stops at the first rejection and returns that GateResult.
    If all gates pass it returns a pass GateResult with gate='chain'.
    """

    def __init__(self, gates: list[Gate] | None = None) -> None:
        self._gates: list[Gate] = list(gates or [])

    def append(self, gate: Gate) -> None:
        self._gates.append(gate)

    def run(self, ctx: EvalContext) -> tuple[GateResult, list[str]]:
        """
        Run all gates in order.

        Returns (result, passed_gate_names) — the list is useful for the
        audit record's 'gates_passed' field.
        """
        passed_names: list[str] = []
        for gate in self._gates:
            result = gate.check(ctx)
            if not result.passed:
                return result, passed_names
            passed_names.append(gate.name)
        return GateResult.pass_("chain"), passed_names


# ---------------------------------------------------------------------------
# Common gate implementations shared across bots
# ---------------------------------------------------------------------------

class KeywordBlocklistGate(Gate):
    """Block markets whose question matches a blocked keyword (case-insensitive)."""
    name = "keyword_blocklist"

    def check(self, ctx: EvalContext) -> GateResult:
        cfg = ctx.config
        if not getattr(cfg, "blocked_keywords", None):
            return GateResult.pass_(self.name)
        q_lower = ctx.question.lower()
        for kw in cfg.blocked_keywords:
            if kw.lower() in q_lower:
                return GateResult.reject(self.name, f"keyword={kw!r}")
        return GateResult.pass_(self.name)


class SpreadGate(Gate):
    """Reject markets with a bid-ask spread above the scan threshold."""
    name = "spread"

    def check(self, ctx: EvalContext) -> GateResult:
        max_spread = getattr(ctx.config, "max_scan_spread", 0.08)
        if ctx.spread > max_spread:
            return GateResult.reject(
                self.name,
                f"spread={ctx.spread:.4f} > max={max_spread:.4f}",
            )
        return GateResult.pass_(self.name)


class PriceBoundsGate(Gate):
    """Reject markets priced near 0 or 1 (near-resolved)."""
    name = "price_bounds"

    def __init__(self, lo: float = 0.01, hi: float = 0.99) -> None:
        self._lo = lo
        self._hi = hi

    def check(self, ctx: EvalContext) -> GateResult:
        if not (self._lo <= ctx.yes_mid <= self._hi):
            return GateResult.reject(
                self.name,
                f"yes_mid={ctx.yes_mid:.4f} outside [{self._lo}, {self._hi}]",
            )
        return GateResult.pass_(self.name)


class MinSignalGate(Gate):
    """Require a minimum number of signal updates before trading."""
    name = "min_signal_count"

    def check(self, ctx: EvalContext) -> GateResult:
        required = getattr(ctx.config, "min_signals_before_trade", 0)
        if ctx.signal_count < required:
            return GateResult.reject(
                self.name,
                f"signals={ctx.signal_count} < required={required}",
            )
        return GateResult.pass_(self.name)


class EdgeGate(Gate):
    """Require positive edge (EV above threshold)."""
    name = "edge"

    def check(self, ctx: EvalContext) -> GateResult:
        if not ctx.has_edge:
            return GateResult.reject(
                self.name,
                f"ev={ctx.ev:.4f} < min_ev={getattr(ctx.config,'min_ev',0):.4f}",
            )
        return GateResult.pass_(self.name)


class YesDisabledGate(Gate):
    """Block YES trades when the yes_disabled flag is set."""
    name = "yes_disabled"

    def check(self, ctx: EvalContext) -> GateResult:
        if ctx.side == "YES" and getattr(ctx.config, "yes_disabled", False):
            return GateResult.reject(self.name, "yes_disabled=True")
        return GateResult.pass_(self.name)


class NOFloorGate(Gate):
    """
    Block NO trades when YES > 45%.

    Backtest (exp_20260611_003): NO on YES > 50% is negative-EV (CLV=-0.034, t=-4.83).
    Audit data (199 settled trades) shows losses extend to YES > 45% — tightened from 0.50.
    The edge only exists at NO 55c+ (YES ≤ 45%).
    """
    name = "no_floor"

    def check(self, ctx: EvalContext) -> GateResult:
        if ctx.side == "NO" and ctx.yes_mid > 0.45:
            return GateResult.reject(
                self.name,
                f"yes_mid={ctx.yes_mid:.3f} > 0.45 (negative-EV NO bucket)",
            )
        return GateResult.pass_(self.name)


class LongshotGate(Gate):
    """
    Require high conviction for YES trades on longshot markets (YES < 12%).

    Longshots are systematically overpriced — only take them with strong signal
    (p_hat >= market + 0.30).
    """
    name = "longshot"

    def check(self, ctx: EvalContext) -> GateResult:
        if ctx.side == "YES" and ctx.yes_mid < 0.12:
            required_gap = 0.30
            if ctx.p_hat < ctx.yes_mid + required_gap:
                return GateResult.reject(
                    self.name,
                    f"longshot yes_mid={ctx.yes_mid:.3f} p_hat={ctx.p_hat:.3f} "
                    f"gap={ctx.p_hat - ctx.yes_mid:+.3f} < +{required_gap}",
                )
        return GateResult.pass_(self.name)


class ExposureCapGate(Gate):
    """Reject trades that would push total portfolio exposure over the cap."""
    name = "exposure_cap"

    def check(self, ctx: EvalContext) -> GateResult:
        max_exp = getattr(ctx.config, "max_total_exposure", 50.0)
        stake = ctx.stake or 0.0
        projected = ctx.total_exposure + stake
        if projected > max_exp:
            return GateResult.reject(
                self.name,
                f"projected={projected:.2f} > cap={max_exp:.2f}",
            )
        return GateResult.pass_(self.name)


class PerMarketCapGate(Gate):
    """Reject trades that would exceed the per-market exposure cap."""
    name = "per_market_cap"

    def check(self, ctx: EvalContext) -> GateResult:
        cap = getattr(ctx.config, "per_market_exposure_cap", 10.0)
        stake = ctx.stake or 0.0
        projected = ctx.market_exposure + stake
        if projected > cap:
            return GateResult.reject(
                self.name,
                f"projected_market={projected:.2f} > cap={cap:.2f}",
            )
        return GateResult.pass_(self.name)
