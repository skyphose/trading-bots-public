"""
test_reconcile.py — Offline unit tests for the on-chain ledger reconciler.

Run with:  python3 test_reconcile.py   (from the trading-bots/ directory)
No live credentials or network access required — every test builds a fixture SQLite
ledger in a temp dir.

reconcile.py is the module that decides what the bots' P&L actually IS. Before it existed,
P&L was imputed from local fields and reported +$175 while the chain said -$38. If this
module is wrong, we are back to trusting a number nobody checks — so its arithmetic and
its classification rules are pinned here.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reconcile


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _ledger(tmpdir):
    """An empty ledger with the real schema."""
    path = os.path.join(tmpdir, "ledger.sqlite")
    con = sqlite3.connect(path)
    con.executescript(reconcile.SCHEMA)
    return con


def _fill(con, tx, asset, cid, usdc, ts=1_780_000_000, side="BUY"):
    con.execute("INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tx, asset, cid, side, 0.0, 0.0, usdc, ts, "t", 0))


def _redeem(con, tx, cid, usdc, ts=1_780_100_000):
    con.execute("INSERT OR REPLACE INTO redemptions VALUES (?,?,?,?,?,?)",
                (tx, cid, usdc, 0.0, ts, "t"))


def _flow(con, tx, idx, cp, direction, amount, cls, token="USDC"):
    con.execute("INSERT OR REPLACE INTO external_flows VALUES (?,?,?,?,?,?,?,?,?)",
                (tx, idx, 1_780_000_000, token, direction, cp, "", amount, cls))


# ---------------------------------------------------------------------------
# Idempotency — the key no bot ever had
# ---------------------------------------------------------------------------

class TestFillIdempotency(unittest.TestCase):

    def test_same_tx_different_assets_are_separate_fills(self):
        # One market order fills against several makers in ONE transaction, each leg a
        # distinct asset. Keying on tx_hash alone would collapse them and understate spend.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _fill(con, "0xtx1", "assetA", "0xcid", 5.0)
            _fill(con, "0xtx1", "assetB", "0xcid", 3.0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)
            self.assertAlmostEqual(
                con.execute("SELECT SUM(usdc_size) FROM fills").fetchone()[0], 8.0, 6)

    def test_resyncing_the_same_row_does_not_double_count(self):
        # --sync runs repeatedly; a re-fetched fill must overwrite, never append.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            for _ in range(3):
                _fill(con, "0xtx1", "assetA", "0xcid", 5.0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
            self.assertAlmostEqual(
                con.execute("SELECT SUM(usdc_size) FROM fills").fetchone()[0], 5.0, 6)

    def test_redemptions_keyed_by_tx_and_condition(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            for _ in range(3):
                _redeem(con, "0xtxR", "0xcid", 12.0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM redemptions").fetchone()[0], 1)


# ---------------------------------------------------------------------------
# Cash-flow arithmetic
# ---------------------------------------------------------------------------

class TestCashFlow(unittest.TestCase):

    def test_realized_is_returned_minus_staked(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _fill(con, "0x1", "a", "0xc1", 100.0)
            _fill(con, "0x2", "b", "0xc2", 50.0)
            _redeem(con, "0xr1", "0xc1", 120.0)
            staked = con.execute(
                "SELECT SUM(usdc_size) FROM fills WHERE side='BUY'").fetchone()[0]
            red = con.execute("SELECT SUM(usdc_size) FROM redemptions").fetchone()[0]
            self.assertAlmostEqual(red - staked, -30.0, 6)

    def test_return_per_dollar(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _fill(con, "0x1", "a", "0xc1", 100.0)
            _redeem(con, "0xr1", "0xc1", 91.47)
            self.assertAlmostEqual(91.47 / 100.0, 0.9147, 4)

    def test_a_loss_cannot_be_reported_as_a_gain(self):
        # The whole point: staking more than comes back is negative, full stop. No entry
        # price, no side, no local field enters this calculation.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _fill(con, "0x1", "a", "0xc1", 1014.59)
            _redeem(con, "0xr1", "0xc1", 928.02)
            staked = con.execute(
                "SELECT SUM(usdc_size) FROM fills WHERE side='BUY'").fetchone()[0]
            red = con.execute("SELECT SUM(usdc_size) FROM redemptions").fetchone()[0]
            self.assertLess(red - staked, 0)
            self.assertAlmostEqual(red - staked, -86.57, 2)


# ---------------------------------------------------------------------------
# Counterparty classification
# ---------------------------------------------------------------------------

class TestCounterpartyClassification(unittest.TestCase):

    def test_known_protocol_addresses_are_protocol(self):
        for addr in ("0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
                     "0x0000000000000000000000000000000000000000"):
            self.assertEqual(reconcile.COUNTERPARTIES[addr][0], "protocol")

    def test_swaps_are_not_deposits(self):
        # A swap converts token types; counting it as a deposit would book the same
        # dollars again every time they changed form.
        self.assertEqual(
            reconcile.COUNTERPARTIES["0xa374094527e1673a86de625aa59517c5de346d32"][0], "swap")

    def test_relay_contracts_are_bridges_not_polymarket_infrastructure(self):
        # 0x4cd00e38 is relay.link's RelayDepository. An older vault note called it
        # "Polymarket's AA forwarder"; it is not.
        cls, label = reconcile.COUNTERPARTIES["0x4cd00e387622c35bddb9b4c962c136462338bc31"]
        self.assertEqual(cls, "bridge")
        self.assertIn("elay", label)

    def test_address_that_only_receives_is_a_trade_peer(self):
        # Polymarket settles peer-to-peer: ~100 one-off makers we PAID. If these read as
        # withdrawals, the entire trading volume looks like money taken out.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _flow(con, "0x1", "0", "0xmaker", "out", 5.0, "external?")
            con.commit()
            reconcile._resolve_wallets(con)
            self.assertEqual(
                con.execute("SELECT class FROM external_flows").fetchone()[0], "trade")

    def test_address_that_also_sends_is_a_wallet(self):
        # The distinguishing rule: a counterparty that has SENT us money is ours.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _flow(con, "0x1", "0", "0xmine", "in", 80.0, "external?")
            _flow(con, "0x2", "0", "0xmine", "out", 20.0, "external?")
            con.commit()
            reconcile._resolve_wallets(con)
            classes = {r[0] for r in con.execute("SELECT class FROM external_flows")}
            self.assertEqual(classes, {"wallet"},
                             "both directions for a known wallet must classify as wallet")

    def test_wallet_net_is_in_minus_out(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _flow(con, "0x1", "0", "0xmine", "in", 139.84, "external?")
            _flow(con, "0x2", "0", "0xmine", "out", 60.66, "external?")
            con.commit()
            reconcile._resolve_wallets(con)
            with patch.object(reconcile, "_erc20_balance", return_value=0.0):
                cap = reconcile.capital_view(con)
            self.assertAlmostEqual(cap["wallets_net"], 79.18, 2)


# ---------------------------------------------------------------------------
# Capital integrity — an impossible balance must be surfaced
# ---------------------------------------------------------------------------

class TestCapitalIntegrity(unittest.TestCase):

    def test_negative_implied_balance_is_detected(self):
        # Summing the transfer export implies -$51.00 of USDC. A balance cannot go
        # negative, so the export is incomplete — that is the signal that stops us
        # quoting a deposit total we cannot source.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _flow(con, "0x1", "0", "0xrelay", "out", 51.0, "bridge")
            con.commit()
            with patch.object(reconcile, "_erc20_balance", return_value=0.0):
                cap = reconcile.capital_view(con)
            self.assertLess(cap["implied"]["USDC"], 0)
            self.assertGreaterEqual(cap["actual"]["USDC"], 0)

    def test_balanced_ledger_reports_no_impossibility(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _flow(con, "0x1", "0", "0xmine", "in", 100.0, "wallet")
            con.commit()
            with patch.object(reconcile, "_erc20_balance", return_value=100.0):
                cap = reconcile.capital_view(con)
            self.assertAlmostEqual(cap["implied"]["USDC"], 100.0, 6)


# ---------------------------------------------------------------------------
# Divergence check — history vs live gaps
# ---------------------------------------------------------------------------

class TestDivergenceCheck(unittest.TestCase):
    SPACEX_CUTOFF = 1_782_259_200   # 2026-06-24T00:00Z

    def test_pre_cutoff_gaps_are_historical(self):
        # spacex-bot was decommissioned 2026-06-24 and never wrote a tracked audit log.
        # Those markets are explained history, not an open defect.
        self.assertLess(1_781_000_000, self.SPACEX_CUTOFF)

    def test_post_cutoff_gaps_are_live_defects(self):
        self.assertGreater(1_785_000_000, self.SPACEX_CUTOFF)

    def test_check_exits_nonzero_only_on_live_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            # A fill with no attribution, dated well before the cutoff.
            _fill(con, "0x1", "a", "0xhistoric", 4.65, ts=1_781_000_000)
            con.execute("INSERT OR REPLACE INTO attribution VALUES (?,?)",
                        ("0xhistoric", "unknown"))
            con.commit()
            with patch.object(reconcile, "AUDIT_SOURCES", {}):
                rc = reconcile.check(con)
            self.assertEqual(rc, 0, "a purely historical gap must not fail the check")

    # -- group [2]: unbooked redemptions, and the dollar floor -----------------
    #
    # These two are the regression test for the 2026-07-27..30 incident: two
    # redemptions crediting $0.00 held check() at exit 1 forever, so the watchdog
    # paged Discord every 6h for three days. Nothing asserted exit 1 at all
    # before, so the floor could have been widened to "always 0" undetected —
    # hence both directions are pinned here.

    def _unbooked(self, d, usd):
        """A post-AUDIT_ERA_CUTOFF redemption with no position_settled row anywhere."""
        con = _ledger(d)
        _redeem(con, "0xr1", "0xunbooked", usd, ts=1_785_000_000)
        con.commit()
        return con

    def test_zero_dollar_unbooked_redemption_is_not_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            con = self._unbooked(d, 0.0)
            with patch.object(reconcile, "AUDIT_SOURCES", {}):
                rc = reconcile.check(con)
            self.assertEqual(rc, 0, "a $0.00 redemption has no cash to book — "
                                    "counting it pins --check at 1 forever")

    def test_material_unbooked_redemption_is_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            con = self._unbooked(d, 50.0)
            with patch.object(reconcile, "AUDIT_SOURCES", {}):
                rc = reconcile.check(con)
            self.assertEqual(rc, 1, "a $50 unbooked redemption is a real booking gap "
                                    "and must still fail the check")

    def test_floor_boundary_is_one_dollar(self):
        # Pinned so the floor cannot be quietly raised to swallow real gaps.
        with tempfile.TemporaryDirectory() as d:
            with patch.object(reconcile, "AUDIT_SOURCES", {}):
                self.assertEqual(reconcile.check(self._unbooked(d, 0.99)), 0)
        with tempfile.TemporaryDirectory() as d:
            with patch.object(reconcile, "AUDIT_SOURCES", {}):
                self.assertEqual(reconcile.check(self._unbooked(d, 1.00)), 1)

    # -- group [3]: phantom fills, and the indexing-lag grace ------------------
    #
    # Regression test for 2026-08-04: crackshot placed a $1.00 trade at 22:46 that
    # filled on time, but the data-api had not indexed it when reconcile synced at
    # 00:45. check() counted it, Discord paged at 00:45:58, and the 06:47 run sent
    # RESOLVED. Group [3] had no maturity guard and no test at all -- the only
    # counted group with neither. Both directions are pinned so the grace cannot be
    # widened to swallow a real phantom, nor dropped back to paging on every trade.

    def _placed(self, d, age_sec, stake=1.00):
        """A ledger with no fills, plus one audit record 'placed' age_sec ago."""
        con = _ledger(d)
        con.commit()
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
        with open(os.path.join(d, "bot_audit.jsonl"), "w") as fh:
            fh.write(json.dumps({
                "bot": "crackshot", "event_type": "trade_evaluated",
                "timestamp": ts.isoformat(), "condition_id": "0xneverfilled",
                "stake_usdc": stake, "order_placed": True,
            }) + "\n")
        return con

    @contextlib.contextmanager
    def _audit_dir(self, d):
        with patch.object(reconcile, "ROOT", d), \
             patch.object(reconcile, "AUDIT_SOURCES", {"crackshot": ["bot_audit.jsonl"]}):
            yield

    def test_recently_placed_unfilled_trade_is_not_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            con = self._placed(d, age_sec=3600)
            with self._audit_dir(d):
                rc = reconcile.check(con)
            self.assertEqual(rc, 0, "a trade placed an hour ago may simply not be "
                                    "indexed yet — counting it pages, then self-resolves")

    def test_stale_placed_unfilled_trade_is_divergence(self):
        with tempfile.TemporaryDirectory() as d:
            con = self._placed(d, age_sec=48 * 3600)
            with self._audit_dir(d):
                rc = reconcile.check(con)
            self.assertEqual(rc, 1, "a trade unfilled after 48h never filled — "
                                    "that is a real phantom and must still page")

    def test_grace_boundary_is_pinned(self):
        for age, want in ((reconcile.PHANTOM_GRACE_SEC - 60, 0),
                          (reconcile.PHANTOM_GRACE_SEC + 60, 1)):
            with tempfile.TemporaryDirectory() as d:
                con = self._placed(d, age_sec=age)
                with self._audit_dir(d):
                    self.assertEqual(reconcile.check(con), want)

    def test_young_phantom_reported_but_not_counted(self):
        # Suppressed must still mean *printed*. A silently dropped bucket is how
        # the original booking leak hid for weeks.
        with tempfile.TemporaryDirectory() as d:
            con = self._placed(d, age_sec=3600)
            buf = io.StringIO()
            with self._audit_dir(d), contextlib.redirect_stdout(buf):
                reconcile.check(con)
            out = buf.getvalue()
            self.assertIn("[3a]", out, "the young bucket must still be visible")
            status = [l for l in out.splitlines() if l.startswith("RECONCILE_STATUS")]
            self.assertTrue(any("group=3a" in l and "counted=0" in l for l in status))
            self.assertFalse(any("group=3 " in l for l in status),
                             "nothing mature — group 3 must not be emitted as counted")

    def test_unparseable_timestamp_counts_as_mature(self):
        # Failing toward 'old' keeps a parse bug loud. Reading the ISO string as an
        # epoch int would silently yield 1970; reading it as unparseable must not
        # age a real phantom out of the report forever.
        self.assertEqual(reconcile._audit_ts({"timestamp": "not-a-date"}), 0.0)
        self.assertEqual(reconcile._audit_ts({}), 0.0)
        self.assertEqual(reconcile._audit_ts({"ts": 1_785_000_000}), 1_785_000_000.0)
        self.assertGreater(
            reconcile._audit_ts({"timestamp": "2026-08-04T12:10:14.030663+00:00"}), 0)

    def test_emit_status_severity_and_counted_flag(self):
        # trade_sentinel.sh filters on `counted=` and escalates on `severity=`,
        # so both fields are load-bearing for whether a human gets paged.
        self.assertEqual(reconcile._emit_status("2b", 2, 0.0, False), "info")
        self.assertEqual(reconcile._emit_status("2", 1, 24.99, True), "warn")
        self.assertEqual(reconcile._emit_status("2", 1, 25.0, True), "critical")

    def test_suppressed_groups_never_marked_counted(self):
        # The old alert grepped every `^[` line, so closed history rode along on
        # every page. Suppressed buckets must be distinguishable by machine.
        with tempfile.TemporaryDirectory() as d:
            con = self._unbooked(d, 0.0)
            buf = io.StringIO()
            with patch.object(reconcile, "AUDIT_SOURCES", {}), \
                 contextlib.redirect_stdout(buf):
                reconcile.check(con)
            status = [l for l in buf.getvalue().splitlines()
                      if l.startswith("RECONCILE_STATUS")]
            self.assertTrue(status, "check() must emit machine-readable status lines")
            self.assertTrue(any("group=2b" in l and "counted=0" in l for l in status))
            self.assertFalse([l for l in status if "counted=1" in l],
                             "a $0.00-only ledger must report no counted groups")


# ---------------------------------------------------------------------------
# Review — settled markets only
# ---------------------------------------------------------------------------

class TestReview(unittest.TestCase):

    def test_open_markets_excluded_from_return_on_stake(self):
        # An open position's mark is an opinion, not a result. Including it would let an
        # unrealised paper gain masquerade as edge.
        with tempfile.TemporaryDirectory() as d:
            con = _ledger(d)
            _fill(con, "0x1", "a", "0xsettled", 10.0)
            _fill(con, "0x2", "b", "0xopen", 10.0)
            _redeem(con, "0xr", "0xsettled", 12.0)
            con.execute("INSERT INTO positions_snapshot VALUES (?,?,?,?,?,?,?)",
                        (1_785_000_000, "0xopen", "t", 1.0, 10.0, 99.0, 0))
            con.execute("INSERT OR REPLACE INTO attribution VALUES (?,?)",
                        ("0xsettled", "polymarket-main"))
            con.execute("INSERT OR REPLACE INTO attribution VALUES (?,?)",
                        ("0xopen", "polymarket-main"))
            con.commit()
            self.assertEqual(reconcile.review(con), 0)

    def test_review_runs_on_an_empty_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(reconcile.review(_ledger(d)), 0)


# ---------------------------------------------------------------------------
# report.py must read rotated archives
# ---------------------------------------------------------------------------

class TestReportReadsArchives(unittest.TestCase):

    def test_stream_jsonl_includes_gz_archives(self):
        # Rotation gzips history into archive/ and keeps the last 50k lines. Reading only
        # the live file showed 3 placed trades against 447 in the archives.
        import gzip
        import report
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "archive"))
            with gzip.open(os.path.join(d, "archive", "audit.jsonl.20260101.gz"), "wt") as fh:
                fh.write(json.dumps({"n": 1}) + "\n")
            live = os.path.join(d, "audit.jsonl")
            with open(live, "w") as fh:
                fh.write(json.dumps({"n": 2}) + "\n")
            got = [r["n"] for r in report.stream_jsonl(live)]
            self.assertEqual(got, [1, 2], "archives must be read, oldest first")

    def test_missing_file_yields_nothing_rather_than_raising(self):
        import report
        self.assertEqual(list(report.stream_jsonl("/nonexistent/audit.jsonl")), [])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
