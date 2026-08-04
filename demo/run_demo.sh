#!/bin/bash
# run_demo.sh — the reconciler, end to end, with no credentials and no network.
#
# Builds a synthetic ledger (demo/build_fixture.py) and runs the real reconcile.py
# against it. Nothing here is mocked: the same code path that reports on a live
# wallet reports on the fixture.
#
# RECONCILE_OFFLINE=1 short-circuits the RPC balance lookups, so this is genuinely
# offline rather than "offline unless a public RPC happens to answer".
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

# Any address works — the fixture ledger is keyed on nothing but itself. The burn
# address makes it obvious in the output that this is not real trading.
export POLY_EOA="${POLY_EOA:-0x000000000000000000000000000000000000dEaD}"
export RECONCILE_OFFLINE=1

echo "── building fixture ledger ────────────────────────────────────"
python3 "$HERE/build_fixture.py"
echo

echo "── reconcile.py --no-sync ─────────────────────────────────────"
python3 "$ROOT/reconcile/reconcile.py" --no-sync
echo

echo "── reconcile.py --review ──────────────────────────────────────"
python3 "$ROOT/reconcile/reconcile.py" --no-sync --review
echo

echo "── reconcile.py --check on a clean ledger ─────────────────────"
set +e
python3 "$ROOT/reconcile/reconcile.py" --no-sync --check
clean_rc=$?
set -e
echo
echo "exit=$clean_rc  — the [1a]/[2a] buckets above printed but did not count."
echo "Suppressed is not the same as hidden: they are always shown, never tallied."
echo

# Now the case the tool exists for. Same ledger, plus one settlement that arrived
# on chain after the audit era began and that nothing locally accounts for.
echo "── same ledger, one unexplained settlement planted ────────────"
python3 "$HERE/build_fixture.py" --with-divergence
echo
set +e
python3 "$ROOT/reconcile/reconcile.py" --no-sync --check
dirty_rc=$?
set -e
echo
echo "exit=$dirty_rc  — group [2] counted, so --check fails and the watchdog pages."
echo

if [[ "$clean_rc" -eq 0 && "$dirty_rc" -eq 1 ]]; then
    echo "demo OK: clean ledger exits 0, planted divergence exits 1."
else
    echo "demo FAILED: expected 0 then 1, got $clean_rc then $dirty_rc." >&2
    exit 1
fi
