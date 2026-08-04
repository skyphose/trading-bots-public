#!/usr/bin/env python3
"""Build a synthetic ledger.sqlite so reconcile.py can be run with no network.

The data here is invented. It is shaped to exercise the interesting paths rather
than to flatter the system: the demo portfolio loses money, one market has no
local attribution ('unknown'), one redemption has no matching settled record, and
one placed trade has no on-chain fill. That is what the reconciler is for, so a
demo where everything agrees would demonstrate nothing.

Reads no live data and contacts nothing. `reconcile.py --no-sync` then reports on
it exactly as it would on a real wallet's ledger.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RECONCILE_DIR = os.path.join(os.path.dirname(HERE), "reconcile")
sys.path.insert(0, RECONCILE_DIR)

import reconcile  # noqa: E402  (path set above)

# Fixed epoch so the demo output is byte-identical on every run — a demo that
# changes every time cannot be diffed or screenshotted with confidence.
BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _ts(days: int) -> int:
    return int((BASE + timedelta(days=days)).timestamp())


def _cid(n: int) -> str:
    return "0x" + f"{n:x}".rjust(64, "d")


# (cid_n, title, bot, staked, redeemed, day)
MARKETS = [
    (1, "Will BTC close above $70k on Jun 7?",       "polymarket-main", 12.00, 14.80, 2),
    (2, "Fed cuts rates at the June meeting?",       "polymarket-main", 10.00,  0.00, 3),
    (3, "Lakers vs Celtics — Games Total O/U 2.5",   "crackshot",        4.00,  6.10, 5),
    (4, "Will it rain in Chicago on Jun 9?",         "crackshot",        3.00,  0.00, 6),
    (5, "CPI print above 3.2% for May?",             "polymarket-main",  8.00,  9.40, 8),
    (6, "Election market — candidate A wins",        None,               6.00,  4.20, 9),
    (7, "Will ETH flip $4k before July?",            "sniper",           2.50,  0.00, 11),
]


def build(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)

    con = sqlite3.connect(path)
    con.executescript(reconcile.SCHEMA)

    for n, title, bot, staked, redeemed, day in MARKETS:
        cid = _cid(n)
        con.execute(
            "INSERT INTO fills (tx_hash, asset, condition_id, side, size, price, "
            "usdc_size, ts, title, outcome_index) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"0xfill{n:060x}", f"asset{n}", cid, "BUY", staked / 0.45, 0.45,
             staked, _ts(day), title, 0),
        )
        if redeemed:
            con.execute(
                "INSERT INTO redemptions (tx_hash, condition_id, usdc_size, size, ts, title) "
                "VALUES (?,?,?,?,?,?)",
                (f"0xredm{n:060x}", cid, redeemed, staked / 0.45, _ts(day + 4), title),
            )
        if bot:
            con.execute("INSERT INTO attribution (condition_id, bot) VALUES (?,?)", (cid, bot))
        # Market 6 gets no attribution row on purpose -> reports as 'unknown', the
        # gap worth surfacing rather than dropping.

    # An open position, so mark-to-market differs from realized cash flow.
    con.execute(
        "INSERT INTO positions_snapshot (taken_at, condition_id, title, size, "
        "initial_value, current_value, redeemable) VALUES (?,?,?,?,?,?,?)",
        (_ts(20), _cid(2), MARKETS[1][1], 22.2, 10.00, 5.40, 0),
    )
    con.execute(
        "INSERT INTO positions_snapshot (taken_at, condition_id, title, size, "
        "initial_value, current_value, redeemable) VALUES (?,?,?,?,?,?,?)",
        (_ts(20), _cid(4), MARKETS[3][1], 6.7, 3.00, 2.10, 0),
    )

    # Funding: one deposit from an external wallet, so --capital has something to
    # reconcile. class must be 'wallet' (not 'deposit') — print_capital sums
    # per-counterparty rows whose class is 'wallet' or 'bridge'.
    #
    # $11.00 is chosen so the two independent P&L derivations agree exactly:
    #   from trading : redeemed + open - staked  = 34.50 + 7.50 - 45.50 = -3.50
    #   from capital : assets - net capital in   =  7.50 - 11.00        = -3.50
    # That agreement is the point of the --capital section, so the demo should show
    # it holding rather than showing an unexplained gap. Capital recycles through
    # many trades, which is why $11 of deposits supports $45.50 of stake.
    con.execute(
        "INSERT INTO external_flows (tx_hash, log_index, ts, token, direction, "
        "counterparty, label, amount, class) VALUES (?,?,?,?,?,?,?,?,?)",
        ("0xdep0", "0", _ts(0), "USDC.e", "in",
         "0x" + "a" * 40, "external wallet", 11.00, "wallet"),
    )
    # Wallet cash is 0 in the demo: RECONCILE_OFFLINE=1 skips the live balanceOf
    # calls, so `assets` is open positions only. Recording a non-zero pUSD here
    # would contradict the report a reader actually sees.
    con.execute(
        "INSERT INTO balances (taken_at, pusd, usdc_e, pol) VALUES (?,?,?,?)",
        (_ts(20), 0.00, 0.00, 4.2),
    )

    con.commit()
    con.close()


def add_divergence(path: str) -> None:
    """Plant a redemption the local records cannot explain.

    Dated after AUDIT_ERA_CUTOFF (2026-07-15) and above UNBOOKED_MIN_USD ($1.00),
    so it lands in counted group [2] rather than the suppressed [2a]/[2b] buckets.
    This is the case the tool exists for: real settlement proceeds arrived on chain
    and nothing locally accounts for them.
    """
    cid = _cid(99)
    con = sqlite3.connect(path)
    ts = int(datetime(2026, 7, 28, tzinfo=timezone.utc).timestamp())
    con.execute(
        "INSERT OR REPLACE INTO redemptions (tx_hash, condition_id, usdc_size, size, ts, title) "
        "VALUES (?,?,?,?,?,?)",
        ("0x" + "9" * 62, cid, 18.75, 41.6, ts, "Unrecorded settlement — nothing local explains this"),
    )
    con.commit()
    con.close()


if __name__ == "__main__":
    out = os.path.join(RECONCILE_DIR, "shared-tmp", "ledger.sqlite")
    build(out)
    if "--with-divergence" in sys.argv:
        add_divergence(out)
        print(f"wrote {out} (+ planted divergence)")
    else:
        print(f"wrote {out}")
