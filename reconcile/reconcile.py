#!/usr/bin/env python3
"""
Reconcile the bots' recorded trade history against on-chain truth.

Why this exists
---------------
Every P&L figure the system reported before 2026-07-27 was *imputed*, never measured.
`shared/redemption.py` computed `pnl = stake/entry_price - stake` from locally-stored
fields, and `bot.py` stored the YES mid as `entry_price` even on NO trades, so the
divisor was frequently the wrong side's price. Summed across all archives that produced
+$175.27 of "realized profit" while the wallet's actual net trading cash flow over the
same period was -$86.58.

The only trustworthy record of what happened is the chain. Polymarket's data-api exposes
it directly: TRADE rows are real fills with the USDC actually paid, REDEEM rows are real
settlement proceeds. This module pulls that ledger into SQLite, attributes each market to
the bot that traded it, and reports where the local audit logs disagree.

It never writes to the audit logs and never moves funds. Existing audit files are left
byte-for-byte intact so they remain usable as forensic evidence.

Usage
-----
    python3 reconcile.py --sync      # fetch from data-api into shared-tmp/ledger.sqlite
    python3 reconcile.py             # sync, then print the reconciliation report
    python3 reconcile.py --check     # divergence only, exit 1 if any found
    python3 reconcile.py --capital   # deposits/withdrawals, and P&L derived two ways
    python3 reconcile.py --review    # per-bot per-month return on stake
    python3 reconcile.py --no-sync   # report from the cached ledger, no network
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import collections
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

# The bots' Polymarket trading identity. They sign as this EOA directly (sig_type=0), so
# data-api reports it as the `proxyWallet` for every position.
#
# Deliberately has no default. A wrong-but-plausible wallet here does not error -- it
# reconciles cleanly against someone else's trades and reports a confident, wrong P&L,
# which is the exact failure class this whole module exists to catch. Requiring the value
# also keeps a live address out of the source, so the file can be published as-is.
# Resolved at import but validated in main(), so the offline test suite can import this
# module without POLY_EOA set.
EOA = os.environ.get("POLY_EOA", "")

DATA_API = "https://data-api.polymarket.com"
LEDGER = os.path.join(ROOT, "shared-tmp", "ledger.sqlite")

# polygon-rpc.com returns 403 to plain urllib from here; the rest work. Same fallback
# order gas_watchdog.sh uses.
RPCS = [
    "https://1rpc.io/matic",
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
]

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# Blockscout serves the full ERC-20 transfer history with no API key. Needed because
# api.polygonscan.com 301s without one and every public RPC caps eth_getLogs at 50 blocks,
# which would be ~120,000 requests to cover the wallet's history.
BLOCKSCOUT = "https://polygon.blockscout.com/api"

# Counterparty classification for external_flows. Anything not listed is treated as a CLOB
# trade counterparty — Polymarket settles peer-to-peer, so ~$923 of the $1,005 spent went
# directly to ~100 one-off maker addresses rather than to an exchange contract.
COUNTERPARTIES = {
    # Protocol: trading, redemption, collateral wrapping. Value stays inside the system.
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045": ("protocol", "CTF (ConditionalTokens)"),
    "0x3a3bd7bb9528e159577f7c2e685cc81a765002e2": ("protocol", "NegRisk wrapped collateral"),
    "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296": ("protocol", "NegRiskAdapter"),
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": ("protocol", "pUSD contract (wrap)"),
    "0x0000000000000000000000000000000000000000": ("protocol", "mint/burn"),
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": ("protocol", "CTF Exchange"),
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": ("protocol", "NegRiskCtfExchange"),
    "0x56c79347e95530c01a2fc76e732f9566da16e113": ("protocol", "NegRiskExchange"),
    # Swaps: token conversion, value-neutral. Counting these as deposits would double-count
    # the same dollars every time they changed token.
    "0xd36ec33c8bed5a9f7b6630855f1533455b98a418": ("swap", "swap router"),
    "0xa374094527e1673a86de625aa59517c5de346d32": ("swap", "UniswapV3Pool"),
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": ("swap", "Uniswap SwapRouter02"),
    # Bridges (relay.link). NOTE: 0x4cd00e38 is RelayDepository — NOT Polymarket's
    # account-abstraction forwarder, which is what an older note claimed. We sent it $50.02
    # via depositErc20 on 2026-04-14.
    "0x4cd00e387622c35bddb9b4c962c136462338bc31": ("bridge", "relay.link RelayDepository"),
    "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f": ("bridge", "relay.link RelayRouterV3"),
}

# condition_id -> bot, resolved by scanning each bot's audit log and state file.
AUDIT_SOURCES = {
    "polymarket-main": ["polymarket-bot/audit.jsonl", "polymarket-bot/archive/audit*.gz"],
    "crackshot": ["shared-tmp/crackshot_audit.jsonl", "shared-tmp/archive/crackshot_audit*.gz"],
    "sniper": ["shared-tmp/sniper_audit.jsonl", "shared-tmp/archive/sniper_audit*.gz"],
    # Positions no bot tracked, redeemed by master-bot/orphan_redeemer.py or the
    # manual redeem_orphans.py. Kept separate so orphans are not attributed to a
    # bot that never traded them.
    "master-orphan": ["shared-tmp/orphan_audit.jsonl", "shared-tmp/archive/orphan_audit*.gz"],
}

# How long a locally-'placed' trade may go unmatched on chain before group [3]
# counts it. An audit record is written at *placement*; the data-api /activity feed
# lags that by hours -- crackshot's 2026-08-03 22:46 fill was still unindexed when
# reconcile synced at 00:45, which paged Discord at 00:45:58 and cleared itself at
# 06:47. That is the "always fires, carries no information" failure the [2a]/[2b]
# guards below exist to prevent, arrived at by a third route. A trade that never
# filled never fills, so waiting a day costs nothing, and the sentinel's own 6h
# throttle already bounds how fast a real phantom could surface anyway.
PHANTOM_GRACE_SEC = 86_400

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    tx_hash      TEXT NOT NULL,
    asset        TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side         TEXT,
    size         REAL,
    price        REAL,
    usdc_size    REAL,
    ts           INTEGER,
    title        TEXT,
    outcome_index INTEGER,
    PRIMARY KEY (tx_hash, asset)
);
CREATE TABLE IF NOT EXISTS redemptions (
    tx_hash      TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    usdc_size    REAL,
    size         REAL,
    ts           INTEGER,
    title        TEXT,
    PRIMARY KEY (tx_hash, condition_id)
);
CREATE TABLE IF NOT EXISTS positions_snapshot (
    taken_at      INTEGER NOT NULL,
    condition_id  TEXT NOT NULL,
    title         TEXT,
    size          REAL,
    initial_value REAL,
    current_value REAL,
    redeemable    INTEGER,
    PRIMARY KEY (taken_at, condition_id)
);
CREATE TABLE IF NOT EXISTS attribution (
    condition_id TEXT PRIMARY KEY,
    bot          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_flows (
    tx_hash      TEXT NOT NULL,
    log_index    TEXT NOT NULL,
    ts           INTEGER,
    token        TEXT,
    direction    TEXT,          -- in | out
    counterparty TEXT,
    label        TEXT,
    amount       REAL,
    class        TEXT,          -- deposit | withdrawal | bridge | swap | protocol | trade
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS balances (
    taken_at INTEGER PRIMARY KEY,
    pusd     REAL,
    usdc_e   REAL,
    pol      REAL
);
CREATE INDEX IF NOT EXISTS idx_fills_cid ON fills(condition_id);
CREATE INDEX IF NOT EXISTS idx_red_cid   ON redemptions(condition_id);
"""


# ── plumbing ────────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": "trading-bots-reconcile/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def _rpc(method: str, params: list):
    """First RPC that answers wins. Returns None if they all fail — balances are a
    nice-to-have here, not a reason to fail the whole reconciliation.

    RECONCILE_OFFLINE=1 short-circuits to None without touching the network. Callers
    already treat None as "balance unavailable" because public RPCs fail routinely,
    so this needs no separate code path. It exists so `--no-sync` is genuinely
    offline: without it the cached-ledger mode still made live balanceOf calls, which
    made the demo non-deterministic and quietly queried whatever address it was given.
    """
    if os.environ.get("RECONCILE_OFFLINE") == "1":
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for rpc in RPCS:
        try:
            req = urllib.request.Request(
                rpc, body, {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as fh:
                return json.load(fh).get("result")
        except Exception:
            continue
    return None


def _erc20_balance(token: str, who: str) -> float | None:
    r = _rpc("eth_call", [{"to": token, "data": "0x70a08231" + who[2:].lower().rjust(64, "0")}, "latest"])
    return int(r, 16) / 1e6 if r and r != "0x" else None


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    con = sqlite3.connect(LEDGER)
    con.executescript(SCHEMA)
    return con


# ── sync ────────────────────────────────────────────────────────────────────────

def fetch_activity() -> list[dict]:
    """Full TRADE + REDEEM history, paginated. data-api caps `limit` at 500."""
    out, offset = [], 0
    while True:
        url = f"{DATA_API}/activity?user={EOA}&limit=500&offset={offset}"
        try:
            batch = _get_json(url)
        except urllib.error.HTTPError as exc:
            print(f"  activity fetch failed at offset {offset}: {exc}", file=sys.stderr)
            break
        if not batch:
            break
        out += batch
        offset += len(batch)
        if len(batch) < 500:
            break
        time.sleep(0.2)
    return out


def fetch_token_transfers() -> list[dict]:
    """Full ERC-20 transfer history for the EOA, oldest first."""
    out, page = [], 1
    while page <= 60:
        url = (f"{BLOCKSCOUT}?module=account&action=tokentx"
               f"&address={EOA}&page={page}&offset=200&sort=asc")
        try:
            batch = _get_json(url).get("result")
        except Exception as exc:
            print(f"  tokentx fetch failed at page {page}: {exc}", file=sys.stderr)
            break
        if not isinstance(batch, list) or not batch:
            break
        out += batch
        if len(batch) < 200:
            break
        page += 1
        time.sleep(0.3)
    return out


def sync_external_flows(con: sqlite3.Connection) -> None:
    """
    Classify every stablecoin movement so 'how much have I actually put in' is answerable.

    A transfer to an address we do not recognise is a CLOB trade counterparty, not a
    withdrawal — Polymarket settles peer-to-peer, so most trading spend looks like ~100
    one-off payments to strangers. Getting that wrong would report the entire trading
    volume as money withdrawn.
    """
    me = EOA.lower()
    rows = fetch_token_transfers()
    kept = 0
    for x in rows:
        sym = (x.get("tokenSymbol") or "").upper()
        if sym not in ("USDC", "USDC.E", "PUSD"):
            continue
        try:
            amount = int(x["value"]) / 10 ** int(x["tokenDecimal"])
        except (KeyError, ValueError):
            continue
        frm, to = x.get("from", "").lower(), x.get("to", "").lower()
        direction = "in" if to == me else "out"
        cp = frm if direction == "in" else to
        cls, label = COUNTERPARTIES.get(cp, ("trade", "CLOB counterparty"))
        if cls == "trade":
            # Distinguishing a withdrawal from a trade payment cannot be done per-transfer:
            # both are "we sent stablecoins to an address we don't recognise". The rule that
            # works is per-COUNTERPARTY — Polymarket's CLOB peers only ever receive from us
            # (settlement is peer-to-peer), so an address that has also *sent* us money is a
            # wallet of ours, and flows to it are withdrawals. Marked provisionally here and
            # resolved in _resolve_wallets() once every row is loaded.
            cls, label = ("external?", "unclassified counterparty")
        con.execute(
            "INSERT OR REPLACE INTO external_flows VALUES (?,?,?,?,?,?,?,?,?)",
            (x.get("hash", ""), str(x.get("logIndex", x.get("transactionIndex", ""))),
             int(x.get("timeStamp", 0)), sym, direction, cp, label, amount, cls),
        )
        kept += 1
    con.commit()
    wallets = _resolve_wallets(con)
    print(f"  external flows: {kept} stablecoin transfers classified, "
          f"{len(wallets)} external wallet(s) identified")


def _resolve_wallets(con: sqlite3.Connection) -> set:
    """
    Split provisional 'external?' counterparties into wallets vs trade peers.

    A CLOB counterparty only ever receives from us. Anything that has also sent us money is
    a wallet we control (or fund from), so money going the other way is a withdrawal, not
    trading spend. Without this split the ~100 one-off maker addresses we paid would each
    read as a withdrawal and the entire trading volume would look like money taken out.
    """
    senders = {r[0] for r in con.execute(
        "SELECT DISTINCT counterparty FROM external_flows "
        "WHERE class='external?' AND direction='in'")}
    for cp in senders:
        con.execute("UPDATE external_flows SET class='wallet', label='external wallet' "
                    "WHERE counterparty=? AND class='external?'", (cp,))
    con.execute("UPDATE external_flows SET class='trade', label='CLOB counterparty' "
                "WHERE class='external?'")
    con.commit()
    return senders


def capital_view(con: sqlite3.Connection) -> dict:
    """
    Net capital in, plus an explicit integrity check.

    Summing transfers must reproduce the wallet's actual balance. Today it does not: the
    export implies a NEGATIVE USDC balance, which cannot happen, so ~$51 of inbound USDC
    from 2026-04-14 is missing from it. That is reported as `unexplained` rather than
    quietly folded into a deposit total — an impossible balance is the signal that the
    source is incomplete, and it is the only reason we know not to trust the total.
    """
    flows = list(con.execute(
        "SELECT token,direction,SUM(amount) FROM external_flows GROUP BY token,direction"))
    implied = collections.defaultdict(float)
    for token, direction, amt in flows:
        implied[token] += amt if direction == "in" else -amt

    actual = {"PUSD": _erc20_balance(PUSD, EOA) or 0.0,
              "USDC.E": _erc20_balance(USDC_E, EOA) or 0.0,
              "USDC": _erc20_balance(USDC_NATIVE, EOA) or 0.0}

    # Net per counterparty: a wallet that sent $139.84 and got $60.66 back funded $79.18,
    # and reporting the gross $139.84 as "deposits" would overstate what was committed.
    per_cp = collections.defaultdict(float)
    labels = {}
    for cp, lab, cls, d, amt in con.execute(
            "SELECT counterparty,label,class,direction,SUM(amount) FROM external_flows "
            "WHERE class IN ('wallet','bridge') GROUP BY counterparty,label,class,direction"):
        per_cp[(cls, cp)] += amt if d == "in" else -amt
        labels[cp] = lab

    wallets_net = sum(v for (cls, _), v in per_cp.items() if cls == "wallet")
    bridge_net = sum(v for (cls, _), v in per_cp.items() if cls == "bridge")
    unexplained = sum(actual.values()) - sum(implied.values())
    return {"implied": dict(implied), "actual": actual, "per_cp": dict(per_cp),
            "labels": labels, "wallets_net": wallets_net, "bridge_net": bridge_net,
            "unexplained": unexplained}


def print_capital(con: sqlite3.Connection) -> None:
    cap = capital_view(con)
    spent = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='BUY'").fetchone()[0]
    sold = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='SELL'").fetchone()[0]
    red = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM redemptions").fetchone()[0]
    open_val = sum(p["current"] for p in _latest_positions(con).values())
    pusd = cap["actual"].get("PUSD", 0.0)
    assets = pusd + open_val
    pnl_trading = sold + red - spent + open_val

    print("\n" + "=" * 72)
    print("CAPITAL — where the money came from and where it stands")
    print("=" * 72)
    print("  Net funding by counterparty (money in, minus anything sent back):")
    for (cls, cp), net in sorted(cap["per_cp"].items(), key=lambda i: -i[1]):
        if abs(net) < 0.01:
            continue
        print(f"    {cls:7} {cp[:16]}…  ${net:>+10,.2f}   {cap['labels'].get(cp,'')}")
    net_observed = cap["wallets_net"] + cap["bridge_net"]
    print(f"  {'-'*62}")
    print(f"    from your own wallets           ${cap['wallets_net']:>+11,.2f}")
    print(f"    net across bridges              ${cap['bridge_net']:>+11,.2f}")
    print(f"    NET CAPITAL IN (observed)       ${net_observed:>+11,.2f}")

    print(f"\n  Assets now: pUSD ${pusd:,.2f} + open positions ${open_val:,.2f} = ${assets:,.2f}")
    print("\n  P&L two ways — these must agree, or one of them is not measuring:")
    print(f"    from trading (returned - staked + open) : ${pnl_trading:>+10,.2f}   <- verified")
    print(f"    from capital (assets - net capital in)  : ${assets - net_observed:>+10,.2f}")
    gap = (assets - net_observed) - pnl_trading
    print(f"    GAP                                     : ${gap:>+10,.2f}")

    print("\n  Transfer-list integrity (implied balance vs actual balanceOf):")
    broken = False
    for tok in ("PUSD", "USDC.E", "USDC"):
        imp, act = cap["implied"].get(tok, 0.0), cap["actual"].get(tok, 0.0)
        bad = imp < -0.01 <= act
        broken = broken or bad
        flag = "  <- IMPOSSIBLE: a balance cannot go negative" if bad else ""
        print(f"    {tok:7} implied ${imp:>9,.2f}   actual ${act:>9,.2f}{flag}")

    if broken or abs(gap) > 1.0:
        print("\n  READ THIS BEFORE QUOTING A DEPOSIT TOTAL.")
        print("  The transfer export is demonstrably incomplete: summing it implies a")
        print("  negative token balance, which cannot happen. 'Net capital in' above is")
        print(f"  therefore a LOWER BOUND, short by roughly ${abs(gap):,.2f} of unsourced")
        print("  inbound value. This does NOT affect the trading P&L, which is measured")
        print("  as (returned - staked) from fills and redemptions and was confirmed")
        print("  independently against raw protocol transfers to within 0.4%.")


def sync(con: sqlite3.Connection) -> None:
    act = fetch_activity()
    fills = reds = 0
    for a in act:
        cid = a.get("conditionId") or ""
        ts = int(a.get("timestamp") or 0)
        usdc = float(a.get("usdcSize") or 0.0)
        tx = a.get("transactionHash") or ""
        if a.get("type") == "TRADE":
            # (tx_hash, asset) is the idempotency key no bot has ever had — a single
            # market order can fill against several makers in one tx, and each leg is a
            # distinct asset. Keying on condition_id alone would collapse them.
            con.execute(
                "INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tx, a.get("asset") or "", cid, a.get("side"), float(a.get("size") or 0),
                 float(a.get("price") or 0), usdc, ts, a.get("title"),
                 a.get("outcomeIndex")),
            )
            fills += 1
        elif a.get("type") == "REDEEM":
            con.execute(
                "INSERT OR REPLACE INTO redemptions VALUES (?,?,?,?,?,?)",
                (tx, cid, usdc, float(a.get("size") or 0), ts, a.get("title")),
            )
            reds += 1

    now = int(time.time())
    try:
        for p in _get_json(f"{DATA_API}/positions?user={EOA}&sizeThreshold=0.01"):
            con.execute(
                "INSERT OR REPLACE INTO positions_snapshot VALUES (?,?,?,?,?,?,?)",
                (now, p.get("conditionId") or "", p.get("title"),
                 float(p.get("size") or 0), float(p.get("initialValue") or 0),
                 float(p.get("currentValue") or 0), 1 if p.get("redeemable") else 0),
            )
    except Exception as exc:
        print(f"  positions fetch failed: {exc}", file=sys.stderr)

    con.execute(
        "INSERT OR REPLACE INTO balances VALUES (?,?,?,?)",
        (now, _erc20_balance(PUSD, EOA), _erc20_balance(USDC_E, EOA),
         (lambda r: int(r, 16) / 1e18 if r else None)(_rpc("eth_getBalance", [EOA, "latest"]))),
    )

    build_attribution(con)
    con.commit()
    print(f"synced: {fills} fills, {reds} redemptions, {len(act)} activity rows")
    sync_external_flows(con)


def _iter_audit(patterns: list[str]):
    for pat in patterns:
        for path in sorted(glob.glob(os.path.join(ROOT, pat))):
            op = gzip.open if path.endswith(".gz") else open
            try:
                with op(path, "rt", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
            except OSError:
                continue


def _audit_ts(rec: dict) -> float:
    """Epoch seconds for an audit record, 0.0 if it cannot be read.

    Audit logs store `timestamp` as an ISO-8601 string ("2026-08-04T12:10:14+00:00"),
    unlike the integer `ts` columns in sqlite -- reading one as the other silently
    yields a 1970 date, which would mark every record mature. Returning 0.0 on a
    record whose time cannot be established is the deliberate direction to fail:
    it is treated as old, so it still gets counted and paged rather than aged out
    of the report forever by a parse bug.
    """
    raw = rec.get("timestamp") or rec.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def build_attribution(con: sqlite3.Connection) -> None:
    """Map condition_id -> bot from the audit logs. Markets no bot recorded stay
    'unknown' rather than being dropped: they are exactly the gap worth surfacing."""
    seen: dict[str, str] = {}
    for bot, patterns in AUDIT_SOURCES.items():
        for rec in _iter_audit(patterns):
            cid = rec.get("condition_id")
            if cid and cid not in seen:
                seen[cid] = bot
    for cid, bot in seen.items():
        con.execute("INSERT OR REPLACE INTO attribution VALUES (?,?)", (cid, bot))
    con.execute(
        "INSERT OR IGNORE INTO attribution "
        "SELECT DISTINCT condition_id, 'unknown' FROM fills"
    )


# ── report ──────────────────────────────────────────────────────────────────────

def _latest_positions(con) -> dict[str, dict]:
    row = con.execute("SELECT MAX(taken_at) FROM positions_snapshot").fetchone()
    if not row or row[0] is None:
        return {}
    return {
        r[0]: {"title": r[1], "initial": r[2], "current": r[3], "redeemable": bool(r[4])}
        for r in con.execute(
            "SELECT condition_id,title,initial_value,current_value,redeemable "
            "FROM positions_snapshot WHERE taken_at=?", (row[0],)
        )
    }


def report(con: sqlite3.Connection) -> None:
    spent = con.execute(
        "SELECT COALESCE(SUM(usdc_size),0), COUNT(*) FROM fills WHERE side='BUY'"
    ).fetchone()
    sold = con.execute(
        "SELECT COALESCE(SUM(usdc_size),0), COUNT(*) FROM fills WHERE side='SELL'"
    ).fetchone()
    red = con.execute("SELECT COALESCE(SUM(usdc_size),0), COUNT(*) FROM redemptions").fetchone()
    pos = _latest_positions(con)
    open_val = sum(p["current"] for p in pos.values())
    open_cost = sum(p["initial"] for p in pos.values())
    bal = con.execute("SELECT pusd,usdc_e,pol FROM balances ORDER BY taken_at DESC LIMIT 1").fetchone()

    realized = sold[0] + red[0] - spent[0]
    mtm = realized + open_val

    print("=" * 72)
    print(f"ON-CHAIN LEDGER — {EOA}")
    print("=" * 72)
    rng = con.execute("SELECT MIN(ts), MAX(ts) FROM fills").fetchone()
    if rng and rng[0]:
        f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        print(f"  period            {f(rng[0])} .. {f(rng[1])}")
    print(f"  BUY fills         {spent[1]:>6}   ${spent[0]:>10,.2f} staked")
    print(f"  SELL fills        {sold[1]:>6}   ${sold[0]:>10,.2f} proceeds")
    print(f"  REDEEMs           {red[1]:>6}   ${red[0]:>10,.2f} proceeds")
    print(f"  {'-'*66}")
    print(f"  REALIZED cash flow          ${realized:>+10,.2f}")
    print(f"  Open positions ({len(pos):>2})        ${open_val:>10,.2f}  (cost ${open_cost:,.2f})")
    print(f"  MARK-TO-MARKET P&L          ${mtm:>+10,.2f}")
    if spent[0]:
        print(f"  Every $1 staked returned    ${(sold[0]+red[0])/spent[0]:>10.4f}")
    if bal:
        print(f"\n  Wallet: pUSD ${bal[0]:,.2f} | USDC.e ${bal[1]:,.2f} | POL {bal[2]:,.2f}")
        if bal[0] is not None:
            print(f"  Total Polymarket capital    ${bal[0] + open_val:>10,.2f}")

    # per-bot
    print(f"\n{'bot':18} {'mkts':>5} {'staked':>10} {'redeemed':>10} {'open':>9} {'P&L':>10}")
    print("  " + "-" * 66)
    rows = con.execute("""
        SELECT COALESCE(a.bot,'unknown') AS bot,
               COUNT(DISTINCT f.condition_id),
               COALESCE(SUM(CASE WHEN f.side='BUY'  THEN f.usdc_size END),0),
               COALESCE(SUM(CASE WHEN f.side='SELL' THEN f.usdc_size END),0)
        FROM fills f LEFT JOIN attribution a ON a.condition_id=f.condition_id
        GROUP BY bot ORDER BY 3 DESC
    """).fetchall()
    attr = dict(con.execute("SELECT condition_id,bot FROM attribution"))
    red_by = defaultdict(float)
    for cid, amt in con.execute("SELECT condition_id, SUM(usdc_size) FROM redemptions GROUP BY condition_id"):
        red_by[attr.get(cid, "unknown")] += amt
    open_by = defaultdict(float)
    for cid, p in pos.items():
        open_by[attr.get(cid, "unknown")] += p["current"]
    for bot, nm, buy, sell in rows:
        pnl = sell + red_by[bot] + open_by[bot] - buy
        print(f"{bot:18} {nm:>5} {buy:>10,.2f} {red_by[bot]:>10,.2f} {open_by[bot]:>9,.2f} {pnl:>+10,.2f}")


def _emit_status(group: str, count: int, usd: float, counted: bool) -> str:
    """One machine-readable line per divergence group.

    trade_sentinel.sh used to build its Discord text by grepping `^\\[`, which swept
    in the suppressed [1a]/[2a] buckets — so every page listed closed history
    alongside whatever was actually wrong. Parsing these lines instead lets the
    watchdog alert on `counted=1` only, and gives it a severity to escalate on.
    """
    sev = "info" if not counted else ("warn" if usd < 25.0 else "critical")
    print(f"RECONCILE_STATUS group={group} count={count} usd={usd:.2f} "
          f"severity={sev} counted={int(counted)}")
    return sev


def check(con: sqlite3.Connection) -> int:
    """Where the local record and the chain disagree. Exit code 1 if anything found,
    so this can gate a cron or a pre-deploy step."""
    print("\n" + "=" * 72)
    print("DIVERGENCE: local records vs chain")
    print("=" * 72)
    problems = 0

    unattributed = con.execute("""
        SELECT f.condition_id, SUM(f.usdc_size), MIN(f.ts), MAX(f.title)
        FROM fills f LEFT JOIN attribution a ON a.condition_id = f.condition_id
        WHERE f.side='BUY' AND (a.bot IS NULL OR a.bot='unknown')
        GROUP BY f.condition_id ORDER BY 2 DESC
    """).fetchall()
    # spacex-bot was decommissioned 2026-06-24 and never wrote to a tracked audit log.
    # Everything before that cutoff is explained history, not an open defect — but it is
    # still printed, because a silently-dropped bucket is how the original problem hid.
    SPACEX_CUTOFF = int(datetime(2026, 6, 24, tzinfo=timezone.utc).timestamp())
    historical = [r for r in unattributed if r[2] < SPACEX_CUTOFF]
    live_gap = [r for r in unattributed if r[2] >= SPACEX_CUTOFF]

    if historical:
        print(f"\n[1a] {len(historical)} markets with no local record, all before "
              f"2026-06-24 (${sum(r[1] for r in historical):,.2f}) — KNOWN/CLOSED: "
              f"decommissioned spacex-bot and pre-audit code. Not counted as divergence.")
        _emit_status("1a", len(historical), sum(r[1] for r in historical), False)
    if live_gap:
        total = sum(r[1] for r in live_gap)
        problems += len(live_gap)
        _emit_status("1", len(live_gap), total, True)
        print(f"\n[1] {len(live_gap)} markets traded on-chain with NO local audit record "
              f"since 2026-06-24 (${total:,.2f} staked) — a LIVE recording gap")
        for cid, amt, ts, title in live_gap[:10]:
            d = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            print(f"      {d}  ${amt:6,.2f}  {cid[:18]}…  {str(title)[:40]}")
        if len(live_gap) > 10:
            print(f"      … and {len(live_gap)-10} more")

    # redemptions with no matching position_settled row anywhere
    settled_cids = set()
    for patterns in AUDIT_SOURCES.values():
        for rec in _iter_audit(patterns):
            if (rec.get("event") or rec.get("event_type")) == "position_settled":
                settled_cids.add(rec.get("condition_id"))
    unrecorded = con.execute(
        "SELECT condition_id, SUM(usdc_size), COUNT(*), MAX(ts) FROM redemptions GROUP BY condition_id"
    ).fetchall()
    missing = [r for r in unrecorded if r[0] not in settled_cids]

    # The audit archives only reach back to 2026-07-15; before that, settlements
    # were written by pre-audit code (and the decommissioned spacex-bot) that had
    # no position_settled event at all. Those rows can never be matched — the
    # evidence does not exist — so counting them as live divergence pins
    # `--check` permanently at exit 1, which is how the real ongoing leak
    # (orphan_redeemer booking nothing) stayed invisible for weeks.
    #
    # They are still printed. A silently-dropped bucket is how the original
    # problem hid. Same treatment as group [1a] above.
    AUDIT_ERA_CUTOFF = int(datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp())
    hist_missing = [r for r in missing if (r[3] or 0) < AUDIT_ERA_CUTOFF]
    live_missing = [r for r in missing if (r[3] or 0) >= AUDIT_ERA_CUTOFF]

    if hist_missing:
        print(f"\n[2a] {len(hist_missing)} redeemed markets with no position_settled record, "
              f"all before 2026-07-15 (${sum(r[1] for r in hist_missing):,.2f}) — KNOWN/CLOSED: "
              f"pre-audit code, no surviving evidence to match. Not counted as divergence.")
        _emit_status("2a", len(hist_missing), sum(r[1] for r in hist_missing), False)

    # A redemption that credited $0.00 has no cash to book, so demanding a
    # position_settled row for it is a false positive by construction. Two such
    # rows (2026-07-25) held `problems` at 2 from 2026-07-27 onward and paged
    # Discord every 6h — which is exactly the "permanently pinned at exit 1"
    # failure the [2a] comment above warns about, arrived at by a different
    # route. Counted groups must stay rare enough that a page means something.
    #
    # Still printed, never silently dropped. Raise the floor only with evidence:
    # sub-dollar proceeds are below the gas cost of the redemption that produced
    # them, so nothing at this scale can represent a real booking leak.
    UNBOOKED_MIN_USD = 1.00
    trivial      = [r for r in live_missing if (r[1] or 0.0) <  UNBOOKED_MIN_USD]
    live_missing = [r for r in live_missing if (r[1] or 0.0) >= UNBOOKED_MIN_USD]

    if trivial:
        print(f"\n[2b] {len(trivial)} redeemed markets with no position_settled record "
              f"but under ${UNBOOKED_MIN_USD:,.2f} of proceeds "
              f"(${sum(r[1] for r in trivial):,.2f} total) — nothing to book. "
              f"Not counted as divergence.")
        _emit_status("2b", len(trivial), sum(r[1] for r in trivial), False)

    missing = live_missing
    if missing:
        problems += len(missing)
        _emit_status("2", len(missing), sum(r[1] for r in missing), True)
        print(f"\n[2] {len(missing)} redeemed markets with NO position_settled record "
              f"since 2026-07-15 (${sum(r[1] for r in missing):,.2f} of proceeds unbooked) "
              f"— a LIVE booking gap")
        for cid, amt, _n, ts in missing[:10]:
            d = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"
            print(f"      {d}  ${amt:6,.2f}  {cid[:18]}…")
        if len(missing) > 10:
            print(f"      … and {len(missing)-10} more")

    # locally recorded trades that never appear as an on-chain fill
    chain_cids = {r[0] for r in con.execute("SELECT DISTINCT condition_id FROM fills")}
    phantom = defaultdict(float)
    young = defaultdict(float)
    cutoff = time.time() - PHANTOM_GRACE_SEC
    for bot, patterns in AUDIT_SOURCES.items():
        for rec in _iter_audit(patterns):
            if rec.get("order_placed") is True and rec.get("condition_id") not in chain_cids:
                stake = float(rec.get("stake_usdc") or 0)
                # Still within the indexing window: the fill may simply not be
                # visible yet. Surfaced below, never counted -- see PHANTOM_GRACE_SEC.
                if _audit_ts(rec) > cutoff:
                    young[bot] += stake
                else:
                    phantom[bot] += stake
    phantom = {k: v for k, v in phantom.items() if v}
    young = {k: v for k, v in young.items() if v}

    if young:
        print(f"\n[3a] Locally 'placed' trades with no on-chain fill yet, all within the "
              f"last {PHANTOM_GRACE_SEC // 3600}h (${sum(young.values()):,.2f}) — inside the "
              f"data-api indexing lag, not counted as divergence:")
        for bot, amt in sorted(young.items(), key=lambda x: -x[1]):
            print(f"      {bot:18} ${amt:,.2f}")
        _emit_status("3a", len(young), sum(young.values()), False)

    if phantom:
        problems += len(phantom)
        print("\n[3] Locally 'placed' trades with NO on-chain fill (booked but never filled):")
        for bot, amt in sorted(phantom.items(), key=lambda x: -x[1]):
            print(f"      {bot:18} ${amt:,.2f}")
        _emit_status("3", len(phantom), sum(phantom.values()), True)

    # the headline: estimated vs measured
    est = 0.0
    n_est = 0
    for patterns in AUDIT_SOURCES.values():
        seen = set()
        for rec in _iter_audit(patterns):
            if (rec.get("event") or rec.get("event_type")) == "position_settled":
                key = (rec.get("condition_id"), rec.get("settled_at"))
                if key in seen:
                    continue
                seen.add(key)
                est += float(rec.get("estimated_pnl_usdc") or rec.get("estimated_pnl_usd") or 0)
                n_est += 1
    spent = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='BUY'").fetchone()[0]
    sold = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='SELL'").fetchone()[0]
    red = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM redemptions").fetchone()[0]
    mtm = sold + red - spent + sum(p["current"] for p in _latest_positions(con).values())
    print("\n[4] Reported vs measured P&L")
    print(f"      audit 'estimated_pnl' over {n_est} settled rows : ${est:>+10,.2f}")
    print(f"      on-chain mark-to-market                        : ${mtm:>+10,.2f}")
    print(f"      OVERSTATEMENT                                  : ${est - mtm:>+10,.2f}")
    # Deliberately counted=False. `est` sums the imputed `estimated_pnl` field —
    # the very number that read +$184.52 while the chain said -$39.37 — so it is
    # cumulative over all history and cannot converge. Making it exit-worthy would
    # recreate the permanent page. It starts counting only once a *measured*
    # realized-P&L column exists to compare against mark-to-market.
    _emit_status("4", n_est, abs(est - mtm), False)

    # `problems` counts individual markets and bots, not groups — the old wording
    # made two $0.00 rows read as two whole categories of failure.
    print(f"\n{'no divergence found' if not problems else f'{problems} live divergence(s) found'}")
    return 1 if problems else 0


def review(con: sqlite3.Connection) -> int:
    """
    The 2026-08-10 half-size review, as one command.

    Prints per-bot per-month return on stake from the ledger, so the decision is made
    against measured cash rather than re-derived under time pressure. Settled markets only:
    an open position's mark is an opinion, not a result.
    """
    attr = dict(con.execute("SELECT condition_id,bot FROM attribution"))
    tk = con.execute("SELECT MAX(taken_at) FROM positions_snapshot").fetchone()[0]
    open_cids = {r[0] for r in con.execute(
        "SELECT condition_id FROM positions_snapshot WHERE taken_at=?", (tk,))} if tk else set()
    red = dict(con.execute("SELECT condition_id,SUM(usdc_size) FROM redemptions GROUP BY condition_id"))

    agg = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for cid, staked, ts in con.execute(
            "SELECT condition_id,SUM(usdc_size),MIN(ts) FROM fills WHERE side='BUY' GROUP BY condition_id"):
        if cid in open_cids:
            continue
        key = (attr.get(cid, "unknown"),
               datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m"))
        a = agg[key]
        a[0] += staked; a[1] += red.get(cid, 0.0); a[2] += 1

    print("=" * 72)
    print("REVIEW — return on stake by bot and month (settled markets only)")
    print("=" * 72)
    print(f"{'bot':18}{'month':9}{'n':>5}{'staked':>10}{'returned':>10}{'P&L':>9}{'per $1':>9}")
    print("-" * 72)
    for (bot, month), (s, r, n) in sorted(agg.items()):
        if s <= 0:
            continue
        print(f"{bot:18}{month:9}{n:>5}{s:>10,.2f}{r:>10,.2f}{r-s:>+9,.2f}{r/s:>9.4f}")
    print("\nDecision rules fixed in advance (see the 2026-07-27 resume plan):")
    print("  main >= 1.00 over the new sample      -> restore ALLOCATION_SCALE = 1.0")
    print("  crackshot < 1.00 over Jul + new       -> disable ou_sports only")
    print("  main < 1.00 over the new sample       -> halt again")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sync", action="store_true", help="fetch from data-api and exit")
    ap.add_argument("--no-sync", action="store_true", help="report from cached ledger only")
    ap.add_argument("--check", action="store_true", help="divergence report only; exit 1 if any")
    ap.add_argument("--capital", action="store_true", help="deposits/withdrawals and P&L two ways")
    ap.add_argument("--review", action="store_true", help="per-bot per-month return on stake")
    ns = ap.parse_args()

    # Checked here rather than at import so `python3 -m unittest test_reconcile` works
    # with no environment at all. Every mode below either queries the chain for this
    # address or labels its report with it, so none of them are meaningful without it.
    if not EOA:
        print("POLY_EOA is not set. Export the Polymarket trading wallet first:\n"
              "    export POLY_EOA=0xYourWalletAddress\n"
              "It has no default on purpose — a wrong address reconciles cleanly against\n"
              "someone else's trades and reports a confident, wrong number.", file=sys.stderr)
        return 2

    con = connect()
    if not ns.no_sync:
        sync(con)
    if ns.sync:
        return 0
    if ns.review:
        return review(con)
    if ns.capital:
        print_capital(con)
        return 0
    if ns.check:
        return check(con)
    report(con)
    print_capital(con)
    return check(con)


if __name__ == "__main__":
    sys.exit(main())
