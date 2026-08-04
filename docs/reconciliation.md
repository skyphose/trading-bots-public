# The reconciliation

A post-mortem of a system that lied to itself for three months, and what was built so it
could not do it again.

## What was believed

By 2026-07-27 the system reported **+$175.27** of realized profit across roughly 400 trades. That
number appeared in the dashboard, in the daily digest, in the project's own documentation, and in
an investor-facing prospectus that described it as *"real, live-traded results — not backtests."*

## What was true

The wallet's net trading cash flow over the same period was **−$86.58**. Every $1 staked had come
back as **$0.9147**.

## The mechanism

Three independent faults compounded.

**1. P&L was imputed, never measured.** Settlement code computed profit arithmetically:

```python
pnl = stake / entry_price - stake
```

This says "if I paid `entry_price` per share for `stake` dollars, I hold `stake/entry_price`
shares, each worth $1 on a win." It is correct — *for a winning position whose entry price was
recorded correctly*. It never consults what actually arrived in the wallet.

**2. `entry_price` held the wrong side's price.** The bot stored the YES midpoint as
`entry_price` even when it had bought NO. On a NO trade at a YES mid of 0.70, the true entry was
0.30 but 0.70 was recorded. On **154 of 201** settled positions the divisor was the wrong side's
price — inflating or deflating every derived figure, unpredictably.

**3. Losses were structurally under-counted.** "Already redeemed" paths booked full wins for
tokens never held. A logging call passed in the wrong argument slot raised a `TypeError` that
escaped inside a `try` block, filing successful redemptions as failures.

Individually each is a normal bug. Together they produced a number that was not merely wrong but
*wrong in a consistent direction*, which is far more dangerous — a noisy error gets noticed.

## The part that made it durable

A nightly job scraped the reported P&L total and wrote it into the project's documentation and
knowledge base. The fabricated figure was therefore *republished as ground truth every night*, in
places a reader would reasonably treat as authoritative. Checking the docs confirmed the number.
Checking the dashboard confirmed the number. Nothing in the loop consulted the chain.

**A system that reports on itself will eventually report what it wishes were true.** The fix is
not more careful arithmetic; it is a source of truth that has no stake in the answer.

## What was built

`reconcile.py` treats the blockchain as the only trustworthy record:

- **TRADE rows** from Polymarket's data-api are real fills with the USDC actually paid.
- **REDEEM rows** are real settlement proceeds.
- Idempotency key is `tx_hash + asset`, so re-syncing is safe and cannot double-count.
- Markets no local record claims are attributed to `unknown` rather than dropped — they are
  exactly the gap worth surfacing.

It **never writes to the audit logs and never moves funds.** Existing audit files are left
byte-for-byte intact so they remain usable as forensic evidence of what was believed.

The republish loop was closed at the same time: the nightly job now reads the on-chain ledger and
**refuses to publish** if that ledger is missing or more than 48h stale, with no fallback to the
imputed figure. A guard with a fallback is not a guard.

## What it cost to find out

Reconciliation is retroactive. It told us the truth about trades already made; it could not undo
them. The strategy research resting on the fabricated numbers — win rates, closing-line value,
per-strategy edge — all had to be discarded and re-derived. Some of it did not survive:
per-strategy results now clear no significance test at the available sample size.

That is the honest outcome, and it is the reason this repository leads with a negative number.

## The rule that came out of it

> **Local records are evidence. The chain is accounting.**
>
> Any P&L figure must come from the reconciled ledger. Never quote the imputed field — not in a
> dashboard, not in a report, not in a commit message.

A corollary that has since caught bugs in three separate components:

> **Record a trade only on a confirmed fill.** A non-`None` order object is not a fill. Booking
> one produces phantom exposure that freezes the bankroll against positions that do not exist.
