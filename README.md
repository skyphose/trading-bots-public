# trading-bots

Instruments from a live prediction-market trading system: an **on-chain reconciler**, a
**tamper-evident audit log**, and the **pricing and sizing primitives** the bots trade on.

[![tests](https://github.com/skyphose/trading-bots-public/actions/workflows/ci.yml/badge.svg)](https://github.com/skyphose/trading-bots-public/actions/workflows/ci.yml)

**Pure standard library.** No dependencies, no build step, no API keys. Clone it and run it:

```bash
git clone https://github.com/skyphose/trading-bots-public
cd trading-bots-public
./demo/run_demo.sh
```

That builds a synthetic ledger and runs the real reconciler against it - fully offline -
showing a clean ledger exit 0 and a planted divergence exit 1.

---

## Why this exists

The system these came from reported +$175.27 of realized profit. Its wallet's actual net
trading cash flow over the same period was −$86.58.

Nothing was stolen and no trade misrouted. The P&L was simply never *measured*. It was
**imputed**: profit was computed as `stake/entry_price - stake` from locally stored fields, and
the code wrote the YES mid into `entry_price` even on NO trades - so on 154 of 201 settled
positions the divisor was the wrong side's price. Summed across archives, that arithmetic
produced a confident, growing, entirely fictional profit curve.

It was also self-reinforcing. A nightly job scraped the reported total and wrote it back into
the project's own documentation, so the fabricated number kept being republished as fact.

The fix was not better arithmetic. It was deciding that **local records are evidence, not
accounting** - and building something that checks them against a source that cannot be talked
into agreeing.

> The live system remains **net negative** on a measured basis. That is stated here because a
> repository about measuring honestly should not bury its own number. See
> [docs/reconciliation.md](docs/reconciliation.md).

---

## What is here

| Directory | What it is |
|---|---|
| [`reconcile/`](reconcile/) | Pulls every fill and redemption from Polymarket's data-api into SQLite, attributes each market to the bot that traded it, and reports where local records and the chain disagree. 843 lines, 31 tests, 5 CLI modes. |
| [`audit/`](audit/) | Append-only hash-chained log. Each record's hash covers the previous one, so any edit, deletion or reorder breaks every hash downstream. Optional HMAC. Rotation, archive reads, retention. |
| [`signals/`](signals/) | LMSR cost/price functions and inefficiency detection, Bayesian belief updates, fractional-Kelly sizing with risk caps, and three vig-removal methods (proportional, power, Shin). |
| [`demo/`](demo/) | An offline end-to-end run. Doubles as a CI test. |

### The reconciler

```bash
export POLY_EOA=0xYourWallet
python3 reconcile/reconcile.py            # ledger, per-bot P&L, capital, divergence
python3 reconcile/reconcile.py --check    # divergence only; exit 1 if any
python3 reconcile/reconcile.py --review   # per-bot per-month return on stake
python3 reconcile/reconcile.py --no-sync  # cached ledger, no network
```

`POLY_EOA` has no default on purpose. A wrong-but-plausible address does not error - it
reconciles cleanly against someone else's trades and reports a confident, wrong number, which is
the exact failure this tool exists to catch. `RECONCILE_OFFLINE=1` additionally skips the RPC
balance lookups, making `--no-sync` genuinely offline.

It **never writes to the audit logs and never moves funds.** Audit files stay byte-for-byte
intact so they remain usable as forensic evidence.

---

## The part worth stealing: counted vs. suppressed alerts

`--check` prints one machine-readable `RECONCILE_STATUS` line per divergence group, and only
some groups are *counted* toward the exit code.

```
RECONCILE_STATUS group=2a count=4 usd=34.50 severity=info counted=0
RECONCILE_STATUS group=2  count=1 usd=18.75 severity=warn counted=1
```

Every suppression was earned by a false page:

| Group | Suppressed because |
|---|---|
| `[1a]` | Markets predating the audit system. The evidence to match them against cannot exist. |
| `[2a]` | Redemptions predating the audit era. Unmatchable by construction. |
| `[2b]` | Unbooked redemptions under $1.00 - below the gas cost that produced them. Nothing to book. |
| `[3a]` | Placed trades under 24h old. Audit rows are written at *placement*; the chain feed lags by hours. |

Two $0.00-proceeds redemptions once held `--check` at exit 1 for three days, sending an identical
alert every six hours. That is how the *actual* ongoing leak stayed invisible - it arrived into a
channel everyone had learned to ignore.

**An alert that always fires carries no information.** So:

- Suppressed groups are still **printed**, never silently dropped. A dropped bucket is how the
  original problem hid.
- The watchdog pages on a **change** in the counted-group fingerprint, not on its persistence,
  with a weekly nag so a genuinely persistent fault is not forgotten.
- "Cannot run" is louder than "clean". A reconciler that fails to start emits no status lines at
  all, which naive logic reads as zero problems - so it would announce all-clear and then go
  quiet forever while checking nothing.

More in [docs/observability.md](docs/observability.md).

---

## Tests

```bash
cd reconcile && python3 -m unittest test_reconcile   # 31
cd audit     && python3 -m unittest test_audit_log   #  9
cd signals   && python3 -m unittest test_signals     # 16
./demo/run_demo.sh                                   # end-to-end, asserts exit codes
```

56 tests, all offline and credential-free, run against Python 3.9 / 3.11 / 3.13 in CI. The
audit-log tests cover the security boundary explicitly, including that a chain forged without
the HMAC key fails verification - the weaker default guarantee is the one most likely to be
over-claimed, so it is the one pinned hardest.

## Scope

This is an extract, not the whole system. The bots' live strategy parameters, wallet
configuration and operational tooling are not here - the framework and the instruments are the
interesting part, and the thresholds are not. The extract is generated by a script that refuses
to publish if any address outside a public-contract allowlist appears in it.

## Disclaimer

Personal research code, shared because the engineering may be useful. Not investment advice, not
a product, and no claim of profitability - the measured result is negative. Prediction-market
trading can lose money and this code has done so. MIT licensed; see [LICENSE](LICENSE).
