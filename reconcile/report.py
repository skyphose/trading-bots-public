#!/usr/bin/env python3
"""Consolidated trade performance report across all trading bots."""

import glob
import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))

AUDIT_FILES = {
    "polymarket": os.path.join(BASE, "polymarket-bot", "audit.jsonl"),
    "kalshi":     os.path.join(BASE, "kalshi-bot", "kalshi_audit.jsonl"),
    "crackshot":  os.path.join(BASE, "shared-tmp", "crackshot_audit.jsonl"),
    "sniper":     os.path.join(BASE, "shared-tmp", "sniper_audit.jsonl"),
}

BANKROLL_FILE = os.path.join(BASE, "shared-tmp", "bankroll_report.json")
HEALTH_FILE   = os.path.join(BASE, "shared-tmp", "master_health.json")


LEDGER_FILE = os.path.join(BASE, "shared-tmp", "ledger.sqlite")


def corrected_clv(record: dict):
    """CLV recomputed from the market price on read — side-correct, never trusting the
    stored `clv`.

    The stored `clv` was computed at settlement from `entry_price`, and the main bot
    recorded the YES mid as `entry_price` even on NO trades (fixed for new trades
    2026-07-28, but every earlier row keeps the wrong value). Over the full settled
    history that inflated mean CLV to +0.230 (t=5.85); recomputing the true entry cost
    from `market_price` gives +0.023 (t=0.67) — statistically zero, and consistent with
    the on-chain −7.1% return per $1 staked. So the stored number is ~90% artifact and
    must not be displayed as edge.

    market_price is the market's YES mid at entry; the true cost of the side we bought is
    that for YES and (1 - that) for NO. Falls back to the stored clv only for the ~32
    pre-2026-05 rows that predate market_price capture.
    """
    won = record.get("won")
    if won is None:
        return record.get("clv")
    mp = record.get("market_price")
    side = record.get("side")
    if mp is not None and side in ("YES", "NO"):
        true_entry = float(mp) if side == "YES" else 1.0 - float(mp)
        return (1.0 if won else 0.0) - true_entry
    return record.get("clv")


def print_reconciled_section():
    """
    The measured P&L, from the chain.

    Everything below this section is derived from `estimated_pnl_usdc`, which is imputed
    from locally-stored stake and entry price — and the main bot stored the YES mid as
    `entry_price` even on NO trades, so the imputation is not merely noisy, it is wrong.
    This section reads shared-tmp/ledger.sqlite (see reconcile.py), where every figure is
    a real fill or a real redemption. When the two disagree, this one is right.
    """
    import sqlite3

    if not os.path.exists(LEDGER_FILE):
        print("\n\n## RECONCILED P&L (on-chain)")
        print("  no ledger yet — run: python3 reconcile.py")
        return
    try:
        con = sqlite3.connect(f"file:{LEDGER_FILE}?mode=ro", uri=True)
        # NOTE ON `side`: two different conventions collide across this codebase.
        #   ledger `fills.side`  = ORDER DIRECTION ("BUY" / "SELL") — from the data-api feed.
        #   audit-log `side`     = OUTCOME token   ("YES" / "NO")  — what the bot logs locally.
        # Here we are on the ledger, so side='SELL' means a real pre-resolution exit, and
        # redemptions (claiming resolved winners) are a separate table — never a "sell".
        spent = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='BUY'").fetchone()[0]
        sold = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM fills WHERE side='SELL'").fetchone()[0]
        n_sells = con.execute("SELECT COUNT(*) FROM fills WHERE side='SELL'").fetchone()[0]
        red = con.execute("SELECT COALESCE(SUM(usdc_size),0) FROM redemptions").fetchone()[0]
        taken = con.execute("SELECT MAX(taken_at) FROM positions_snapshot").fetchone()[0]
        open_val = con.execute(
            "SELECT COALESCE(SUM(current_value),0) FROM positions_snapshot WHERE taken_at=?",
            (taken,),
        ).fetchone()[0] if taken else 0.0
        # Current liquid wallet (latest snapshot). Kept separate from `spent` so the
        # cumulative lifetime flow can never be misread as a balance again.
        bal_row = con.execute(
            "SELECT pusd, usdc_e FROM balances ORDER BY taken_at DESC LIMIT 1"
        ).fetchone()
        liquid = (bal_row[0] or 0.0) + (bal_row[1] or 0.0) if bal_row else 0.0
    except Exception as exc:
        print(f"\n\n## RECONCILED P&L (on-chain)\n  ledger unreadable: {exc}")
        return

    realized = sold + red - spent
    print("\n\n## RECONCILED P&L (on-chain — this is the real number)")
    # CURRENT holdings first, so the headline is what you actually own, not a flow.
    print(f"  Liquid wallet    ${liquid:>9,.2f}   (pUSD + USDC.e on hand now)")
    print(f"  Open positions   ${open_val:>9,.2f}   (mark-to-market)")
    print(f"  {'-'*40}")
    print(f"  ON-CHAIN VALUE   ${liquid + open_val:>9,.2f}   ← what you hold right now")
    print()
    # LIFETIME flows below. These accumulate across every trade and recycle the same
    # capital many times over — they are NOT a balance. `Staked` reached $1k on an ~$80
    # bankroll purely through churn; do not read it as money in the wallet.
    print(f"  Staked (lifetime, gross) ${spent:>9,.2f}   ({int(spent // max(liquid + open_val, 1))}x capital recycled)")
    print(f"  Redeemed (settlement)    ${red:>9,.2f}   (claimed resolved winners)")
    # Sold = real pre-resolution exits (side='SELL' fills), kept distinct from redemptions
    # so actual sell activity is visible. The bots are buy-and-hold, so this is normally $0.
    sold_note = f"({n_sells} sell fills)" if sold else "— buy-and-hold, no exits executed"
    print(f"  Sold (pre-resolution)    ${sold:>9,.2f}   {sold_note}")
    print(f"  {'-'*40}")
    print(f"  Realized P&L     ${realized:>+9,.2f}")
    print(f"  Mark-to-market   ${realized + open_val:>+9,.2f}")
    if spent:
        print(f"  Return per $1 staked  ${(red + sold) / spent:.4f}")
    if taken:
        age = (now_utc().timestamp() - taken) / 3600
        print(f"  (ledger synced {age:.1f}h ago; refresh with `python3 reconcile.py --sync`)")


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def stream_jsonl(path):
    """
    Stream an audit log INCLUDING its rotated archives.

    This used to read only the live .jsonl. Rotation gzips the full history into a
    sibling archive/ dir and keeps the last 50,000 lines, so every report silently
    became "the recent window" with no indication history had been dropped —
    polymarket-bot/audit.jsonl sat at 46.8 MB against a 50 MB trigger while showing
    3 placed trades against 447 in the archives. Archives are read oldest-first so
    de-duplication by key keeps the same precedence as before.
    """
    archive_dir = os.path.join(os.path.dirname(path), "archive")
    stem = os.path.basename(path).split(".")[0]
    sources = sorted(
        p for p in glob.glob(os.path.join(archive_dir, f"{stem}*.gz"))
    ) + [path]

    for src in sources:
        opener = gzip.open if src.endswith(".gz") else open
        try:
            with opener(src, "rt", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except (FileNotFoundError, OSError):
            continue


def is_placed(record, bot):
    """Return True if this record represents an actual placed trade."""
    if bot == "polymarket":
        # No 'event' key — just a top-level evaluation record
        return record.get("order_placed") is True and "event" not in record and "event_type" not in record
    else:
        # crackshot / sniper / kalshi
        return record.get("order_placed") is True


def is_settled(record):
    return record.get("event") == "position_settled"


def get_condition_key(record, bot):
    """Unique key per market position."""
    if bot == "kalshi":
        return record.get("ticker") or record.get("condition_id")
    return record.get("condition_id")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def now_utc():
    return datetime.now(timezone.utc)


def is_executed_exit(record):
    """A real pre-resolution SELL the bot executed (news-driven trim/exit, or an arb
    leg sold back to flat). These carry an `event` key, so `is_placed` deliberately
    excludes them — without this bucket they were dropped from every summary entirely."""
    return (
        record.get("event") in ("news_exit", "arb_unwound")
        and record.get("mode") == "executed"
    )


def collect_bot_data(bot, path):
    placed = {}   # condition_key -> record
    settled = {}  # condition_key -> record
    exits = {}    # condition_key -> record (executed pre-resolution SELLs)

    for rec in stream_jsonl(path):
        key = get_condition_key(rec, bot)
        if not key:
            continue
        if is_placed(rec, bot):
            # Keep earliest placement per market
            if key not in placed:
                placed[key] = rec
        elif is_settled(rec):
            settled[key] = rec
        elif is_executed_exit(rec):
            exits[key] = rec

    return placed, settled, exits


def get_stake(record, bot):
    if bot == "kalshi":
        return record.get("stake_usd") or record.get("stake_usdc") or 0.0
    return record.get("stake_usdc") or record.get("stake_usd") or 0.0


def get_placed_at(record, bot):
    return record.get("placed_at") or record.get("timestamp") or record.get("entered_at")


def fmt_pnl(v):
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}"


def bar(ratio, width=20):
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def print_report():
    bankroll = load_json(BANKROLL_FILE)
    health   = load_json(HEALTH_FILE)
    now      = now_utc()

    print("=" * 68)
    print("  TRADING BOTS — PERFORMANCE REPORT")
    print(f"  Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 68)

    # ── System Snapshot ────────────────────────────────────────────────
    print("\n## SYSTEM SNAPSHOT")
    total_pusd = bankroll.get("total_pusd", 0)
    deployable = bankroll.get("deployable", 0)
    bots_br    = bankroll.get("bots", {})
    print(f"  Liquid pUSD (on-chain): ${total_pusd:.2f}")
    print(f"  Total deployable:       ${deployable:.2f}")

    health_bots = health.get("bots", {})
    alerts = health.get("alerts", [])

    print(f"\n  {'Bot':<18} {'Bankroll':>10}  {'Health':<8}  {'Issues'}")
    print(f"  {'-'*18} {'-'*10}  {'-'*8}  {'-'*28}")
    for bot_name, br in bots_br.items():
        h = health_bots.get(bot_name, {})
        status = h.get("health", "?")
        issues = ", ".join(h.get("issues", [])) or "—"
        flag = " ⚠" if status != "ok" else ""
        print(f"  {bot_name:<18} ${br['bankroll']:>8.2f}  {status:<8}{flag}  {issues}")

    if alerts:
        print("\n  ALERTS:")
        for a in alerts:
            for issue in a.get("issues", []):
                print(f"    ⚠  {a['bot']}: {issue}")

    print_reconciled_section()

    # ── Collect all trade data ──────────────────────────────────────────
    all_placed  = {}  # bot -> {key: record}
    all_settled = {}  # bot -> {key: record}
    all_exits   = {}  # bot -> {key: record} (executed pre-resolution SELLs)

    for bot, path in AUDIT_FILES.items():
        p, s, e = collect_bot_data(bot, path)
        all_placed[bot]  = p
        all_settled[bot] = s
        all_exits[bot]   = e

    # ── Per-Bot Summary ─────────────────────────────────────────────────
    print("\n\n## PER-BOT SUMMARY  (ESTIMATED — imputed from local records, not measured)")
    print(f"\n  {'Bot':<12} {'Placed':>7} {'Settled':>8} {'Open':>6} {'Exits':>6} {'Win%':>6} {'P&L':>9} {'Avg Stk':>8} {'Avg CLV':>8}")
    print(f"  {'-'*12} {'-'*7} {'-'*8} {'-'*6} {'-'*6} {'-'*6} {'-'*9} {'-'*8} {'-'*8}")

    grand_placed = grand_settled = grand_wins = grand_exits = 0
    grand_pnl = 0.0
    all_settled_flat = []

    for bot in ["polymarket", "kalshi", "crackshot", "sniper"]:
        placed  = all_placed[bot]
        settled = all_settled[bot]
        exits   = all_exits[bot]

        n_placed  = len(placed)
        n_settled = len(settled)
        n_exits   = len(exits)
        # A market the bot sold out of pre-resolution is no longer "open" even if it
        # never produced a position_settled row — count exits as closed.
        n_open    = len([k for k in placed if k not in settled and k not in exits])

        wins     = [r for r in settled.values() if r.get("won")]
        losses   = [r for r in settled.values() if r.get("won") is False]
        win_rate = len(wins) / n_settled * 100 if n_settled else 0

        pnl_list = [r.get("estimated_pnl_usdc") or r.get("estimated_pnl_usd") or 0 for r in settled.values()]
        total_pnl = sum(pnl_list)

        stake_list = [get_stake(r, bot) for r in placed.values()]
        avg_stake  = sum(stake_list) / len(stake_list) if stake_list else 0

        # Side-correct CLV recomputed from market_price, not the biased stored clv.
        clv_list = [c for c in (corrected_clv(r) for r in settled.values()) if c is not None]
        avg_clv  = sum(clv_list) / len(clv_list) if clv_list else None
        clv_str  = f"{avg_clv:+.3f}" if avg_clv is not None else "  n/a"

        win_str = f"{win_rate:.0f}%" if n_settled else "  n/a"
        exit_str = str(n_exits) if n_exits else "—"
        print(f"  {bot:<12} {n_placed:>7} {n_settled:>8} {n_open:>6} {exit_str:>6} {win_str:>6} {fmt_pnl(total_pnl):>9} ${avg_stake:>6.2f} {clv_str:>8}")

        grand_placed  += n_placed
        grand_settled += n_settled
        grand_wins    += len(wins)
        grand_exits   += n_exits
        grand_pnl     += total_pnl

        for k, r in settled.items():
            r["_bot"] = bot
            all_settled_flat.append(r)

    grand_wr = grand_wins / grand_settled * 100 if grand_settled else 0
    grand_exit_str = str(grand_exits) if grand_exits else "—"
    print(f"  {'TOTAL':<12} {grand_placed:>7} {grand_settled:>8} {'':>6} {grand_exit_str:>6} {grand_wr:.0f}%{'':<3} {fmt_pnl(grand_pnl):>9}")

    # ── Category Breakdown ──────────────────────────────────────────────
    print("\n\n## CATEGORY BREAKDOWN (settled trades)")
    cat_data = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    for r in all_settled_flat:
        cat = r.get("category") or r.get("trade_type") or "unknown"
        pnl = r.get("estimated_pnl_usdc") or r.get("estimated_pnl_usd") or 0
        if r.get("won"):
            cat_data[cat]["wins"] += 1
        else:
            cat_data[cat]["losses"] += 1
        cat_data[cat]["pnl"] += pnl

    print(f"\n  {'Category':<18} {'W':>4} {'L':>4} {'Win%':>6} {'P&L':>9}  Win bar")
    print(f"  {'-'*18} {'-'*4} {'-'*4} {'-'*6} {'-'*9}  {'-'*20}")
    for cat, d in sorted(cat_data.items(), key=lambda x: -(x[1]["wins"]+x[1]["losses"])):
        total = d["wins"] + d["losses"]
        wr    = d["wins"] / total if total else 0
        print(f"  {cat:<18} {d['wins']:>4} {d['losses']:>4} {wr*100:>5.0f}% {fmt_pnl(d['pnl']):>9}  {bar(wr)}")

    # ── Open Positions ──────────────────────────────────────────────────
    print("\n\n## OPEN POSITIONS (placed, not yet settled)")
    print(f"  {'Bot':<12} {'Side':<5} {'Stake':>7} {'Entry':>7} {'Age(d)':>7}  Question")
    print(f"  {'-'*12} {'-'*5} {'-'*7} {'-'*7} {'-'*7}  {'-'*38}")

    open_rows = []
    for bot in ["polymarket", "kalshi", "crackshot", "sniper"]:
        placed  = all_placed[bot]
        settled = all_settled[bot]
        for key, r in placed.items():
            if key in settled:
                continue
            ts_str = get_placed_at(r, bot)
            ts     = parse_ts(ts_str)
            age    = (now - ts).days if ts else "?"
            stake  = get_stake(r, bot)
            entry  = r.get("entry_price") or r.get("market_price") or 0
            side   = r.get("side", "?")
            q      = r.get("question") or r.get("title") or key
            open_rows.append((bot, side, stake, entry, age, q))

    open_rows.sort(key=lambda x: x[4] if isinstance(x[4], int) else -1, reverse=True)
    for bot, side, stake, entry, age, q in open_rows[:30]:
        q_short = (q[:42] + "…") if len(q) > 43 else q
        print(f"  {bot:<12} {side:<5} ${stake:>5.2f} {entry:>7.3f} {str(age):>7}  {q_short}")

    if len(open_rows) > 30:
        print(f"  ... and {len(open_rows) - 30} more open positions")

    # ── Recent Settled Trades ───────────────────────────────────────────
    print("\n\n## RECENT SETTLED TRADES (last 20)")
    sorted_settled = sorted(
        all_settled_flat,
        key=lambda r: r.get("settled_at") or r.get("timestamp") or "",
        reverse=True
    )[:20]

    print(f"\n  {'Date':<12} {'Bot':<12} {'Side':<5} {'Res':>4} {'P&L':>8}  {'CLV':>6}  Question")
    print(f"  {'-'*12} {'-'*12} {'-'*5} {'-'*4} {'-'*8}  {'-'*6}  {'-'*38}")
    for r in sorted_settled:
        settled_at = parse_ts(r.get("settled_at") or r.get("timestamp"))
        date_str   = settled_at.strftime("%Y-%m-%d") if settled_at else "?"
        bot_name   = r.get("_bot", r.get("bot", "?"))
        side       = r.get("side", "?")
        won        = "WIN" if r.get("won") else "LOSS"
        pnl        = r.get("estimated_pnl_usdc") or r.get("estimated_pnl_usd") or 0
        clv        = corrected_clv(r)
        clv_str    = f"{clv:+.3f}" if clv is not None else "   n/a"
        q          = r.get("question") or r.get("title") or "?"
        q_short    = (q[:40] + "…") if len(q) > 41 else q
        print(f"  {date_str:<12} {bot_name:<12} {side:<5} {won:>4} {fmt_pnl(pnl):>8}  {clv_str:>6}  {q_short}")

    # ── CLV Distribution (polymarket only) ─────────────────────────────
    pm_settled = list(all_settled["polymarket"].values())
    # Recomputed from market_price — the stored clv made this histogram bimodal (spikes
    # at +0.7 and −0.4, which is won − yes_mid, not CLV). The corrected version centers
    # near zero, which is the real distribution.
    clv_vals = [c for c in (corrected_clv(r) for r in pm_settled) if c is not None]
    if clv_vals:
        print("\n\n## CLV DISTRIBUTION (polymarket settled trades, side-corrected)")
        buckets = defaultdict(int)
        for v in clv_vals:
            b = int(v * 10) / 10  # round to nearest 0.1
            buckets[b] += 1
        max_count = max(buckets.values())
        print()
        for b in sorted(buckets):
            count = buckets[b]
            width = int(count / max_count * 40)
            print(f"  {b:+.1f}  {'█'*width:<40}  {count}")
        pos_clv = sum(1 for v in clv_vals if v > 0)
        print(f"\n  Positive CLV trades: {pos_clv}/{len(clv_vals)} ({pos_clv/len(clv_vals)*100:.0f}%)")
        print(f"  Mean CLV: {sum(clv_vals)/len(clv_vals):+.4f}")

    print("\n" + "=" * 68)


if __name__ == "__main__":
    print_report()
