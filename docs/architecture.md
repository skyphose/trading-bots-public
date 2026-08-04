# Architecture

The system these modules come from. This document describes the whole; the repository
contains the extracted parts marked **(here)**.

## Topology

```
                         ┌──────────────┐
                         │  master-bot  │  health checks, capital allocation,
                         │              │  orphaned-position redemption
                         └──────┬───────┘
          ┌───────────┬─────────┴───────┬────────────┐
          │           │                 │            │
   ┌──────▼─────┐ ┌───▼────┐ ┌──────────▼─┐ ┌────────▼───┐
   │ polymarket │ │ sniper │ │  crackshot │ │   kalshi   │
   │    main    │ │        │ │            │ │            │
   │ Bayesian + │ │ complete│ │ brackets, │ │ CFTC mkts, │
   │ LMSR + EV  │ │ -set arb│ │ O/U sports│ │ RSA auth   │
   └──────┬─────┘ └───┬────┘ └─────┬──────┘ └──────┬─────┘
          │           │            │               │
          └───────────┴──────┬─────┴───────────────┘
                             │  append-only, hash-chained audit logs  (here)
                      ┌──────▼───────┐
                      │ reconcile.py │  on-chain truth, the only trusted P&L  (here)
                      └──────┬───────┘
                             │  RECONCILE_STATUS lines
                      ┌──────▼───────┐
                      │   sentinel   │  host watchdog → alerts
                      └──────────────┘
```

All containers egress through a WireGuard VPN. Every bot signs as the **same EOA**
(`sig_type=0`, no proxy Safe), which is the source of the one piece of genuinely hard
concurrency in the system.

## Per-cycle pipeline

Each bot runs the same shape, differing in how it forms a probability estimate:

1. **Scan** — fetch candidate markets, filter on volume, spread and time-to-close.
2. **Estimate** — form `p_hat`, a probability independent of the current market price.
3. **Compare** — `EV = p_hat - market_price`. **(here:** `signals/lmsr.py`**)**
4. **Gate** — a chain of independent veto conditions, any one of which blocks the trade.
   **(here:** `signals/gates.py`**)**
5. **Size** — fractional Kelly, clamped by per-market and portfolio exposure caps.
   **(here:** `signals/sizing.py`**)**
6. **Execute** — place the order, then record **only on a confirmed fill**.
7. **Record** — append to the hash-chained audit log. **(here:** `audit/`**)**

Step 6's emphasis is load-bearing. A non-`None` order object is not a fill; treating it as one
books phantom exposure that freezes the bankroll against positions that do not exist. This bug
appeared independently in two different bots.

## Signal sources

`p_hat` is aggregated from independent evidence rather than any single model — the Bayesian
updater accumulates log-odds contributions so no one source dominates.

| Family | Examples |
|---|---|
| Market structure | Order-book imbalance, spread dynamics, LMSR inefficiency |
| Statistical | GARCH volatility regime, cointegration, calibration tiers |
| External data | Weather, economic releases, sports odds consensus (devigged) |
| Cross-market | Complete-set arbitrage; the same event priced on two venues |

**(here:** `signals/devig.py` implements the vig removal — proportional, power, and Shin (1993).
Shin models insider trading and is the industry standard for the sharpest correction.**)**

## Concurrency

Four containers signing from one EOA means nonces are contended. Two mechanisms:

- **Nonce lease** — an exclusive, cross-container lock held for the duration of a signing
  sequence. Without it, two bots fetch the same nonce and one transaction is silently dropped.
- **State locks** — `flock`-based, because duplicate processes writing the same JSON state file
  corrupt it. **(here:** `audit/state_lock.py`**)**

Both are the unglamorous kind of code that only announces itself by its absence.

## Data flow, and the trust boundary

```
  bots ──write──> audit logs ──read──> reconcile.py <──read── Polygon chain
                  (evidence)            (accounting)           (truth)
```

The arrow that does not exist is the point: **`reconcile.py` never writes back.** Audit files
stay byte-for-byte intact, so they remain usable as evidence of what the system believed at the
time — which is exactly what made the original post-mortem possible.

The direction of trust is one-way. Local records can be wrong, incomplete, or optimistic. The
chain has no opinion.

## What is not in this repository

Live strategy parameters, wallet configuration, VPN credentials, container definitions and
operational tooling. The extract is produced by a script that refuses to publish if any address
outside a public-contract allowlist appears in it. The framework and the instruments are the
interesting part; the thresholds are just numbers that happened to be true for one wallet.
