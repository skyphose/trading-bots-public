# Alerting that stays worth reading

Notes on the monitoring design, written down because most of it was learned by getting it
wrong first.

## The failure this is all guarding against

Two redemptions that credited **$0.00** had no matching local settlement record. Technically a
divergence, so `reconcile.py --check` exited 1. The watchdog ran every six hours and dutifully
sent the same Discord alert every six hours, for three days.

Nothing was wrong. There was no cash to book — a $0.00 redemption has nothing to account for.

The cost was not the noise itself. It was that a **real** booking leak surfaced during that
window, into a channel everyone had already learned to scroll past.

> An alert that always fires carries no information, and trains the reader to ignore the channel
> it arrives in. A monitor that cries wolf is worse than no monitor, because it consumes the
> attention a real one would need.

## Counted vs. suppressed

`--check` emits one machine-readable line per divergence group:

```
RECONCILE_STATUS group=2a count=4 usd=34.50 severity=info counted=0
RECONCILE_STATUS group=2  count=1 usd=18.75 severity=warn counted=1
```

`counted=1` groups drive the exit code and the page. `counted=0` groups are printed and ignored.

The watchdog filters on `counted=0` rather than pattern-matching the human-readable text. That
matters: the original implementation built its alert body by grepping for a leading `[`, which
swept every suppressed bucket into every page — so each alert opened with a wall of closed
history before reaching the actual problem.

### Suppression must be visible

Every suppressed bucket is still **printed in full**, with its count and dollar total. This is
deliberate and it is the rule that most often gets violated under pressure:

> A silently-dropped bucket is how the original problem hid.

The distinction is between *"we looked at this and decided it does not warrant waking you"* and
*"we stopped looking."* The first is engineering. The second is how a monitor rots into
decoration. If you cannot see what was suppressed, you cannot audit the suppression — and a
suppression rule nobody can audit will eventually swallow something real.

### Each rule was earned

| Group | Rule | The false page that produced it |
|---|---|---|
| `[1a]` | Ignore markets predating the audit system | Decommissioned bot's trades, no local evidence can exist |
| `[2a]` | Ignore redemptions before the audit era | Pinned `--check` at exit 1 permanently, hiding a live leak |
| `[2b]` | Ignore unbooked proceeds under $1.00 | The two $0.00 redemptions above |
| `[3a]` | Ignore placed trades under 24h old | Audit rows written at *placement*; the chain feed lags hours |

`[3a]` is the clearest case. A trade filled on time at 22:46 was not yet visible in the data-api
feed when the reconciler synced at 00:45. It paged, then cleared itself at 06:47.

That self-clearing is the diagnostic. **A genuine phantom — an order booked but never filled —
can never resolve itself**, because the fill will never appear. Anything that clears on its own
was a timing artifact. Confusing the two sends you hunting a bug that is not there while the
real class of bug (booking a fill that never happened) looks identical in the alert text.

## Page on change, not on persistence

The watchdog fingerprints the set of counted groups and alerts when that fingerprint *changes* —
including changing to empty, which sends a RESOLVED message.

A weekly nag re-sends a genuinely persistent fault, so "page on change" does not become "mention
it once and forget."

## A checker that stops checking must be loud

The subtlest bug in this design was found by writing it down:

If the reconciler cannot run at all — missing configuration, unreadable ledger — it emits **no
status lines**. Naive logic reads zero counted groups as "clean", so a misconfigured reconciler
would announce `DIVERGENCE RESOLVED` and then stay silent forever while verifying nothing.

So the exit code is trichotomous, not binary:

| Exit | Meaning | Response |
|---|---|---|
| 0 | Ran, found nothing counted | Quiet (or RESOLVED, on change) |
| 1 | Ran, found divergence | Page, severity by dollar value |
| ≥2 | **Could not run** | Page — louder than a clean result |

> Absence of evidence gets reported as absence of evidence, never as evidence of absence.

## Fail toward the alarm

Where a check cannot determine an answer, it resolves in the direction that produces a page.

The audit-log timestamp parser returns `0.0` on anything it cannot read, which marks the record
*mature* — so it gets counted and paged rather than aged out of the report forever. A parse bug
becomes a noisy false positive, which someone fixes. The opposite default makes it an invisible
blind spot that silently swallows real phantoms.

This trades a small amount of noise for the guarantee that a broken checker announces itself.
That is almost always the right trade, and it is the one instance where adding noise is correct.
