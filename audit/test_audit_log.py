#!/usr/bin/env python3
"""Tests for the tamper-evident audit log.

The property under test is narrow and worth stating plainly: this log detects
*after-the-fact edits*. Each record's hash covers the previous record's hash, so
altering, reordering or deleting any line breaks every hash downstream of it.

Without AUDIT_HMAC_KEY set it is a plain SHA-256 chain — that stops corruption
and careless edits, but anyone who can write the file can recompute a consistent
chain. With the key set, forging the chain requires the key. Both modes are
covered below, because the weaker guarantee is the one that ships by default and
is the one most likely to be over-claimed.
"""
import json
import os
import tempfile
import unittest

import audit_log


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "audit.jsonl")
        self.addCleanup(self.tmp.cleanup)
        # Each test starts from a known-empty chain; the module caches last-hash
        # per path, so a stale entry from another test would silently link chains.
        audit_log.reset_chain(self.path)

    def _write(self, n):
        for i in range(n):
            audit_log.write_audit_record({"event": "test", "i": i}, path=self.path)

    def _lines(self):
        with open(self.path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_intact_chain_verifies(self):
        self._write(5)
        result = audit_log.verify_chain(self.path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["broken"], [])

    def test_each_record_links_to_its_predecessor(self):
        self._write(3)
        rows = self._lines()
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(
                cur["prev_hash"], prev["hash"],
                "a record's prev_hash must be the previous record's hash — that "
                "linkage is the entire tamper-evidence property")

    def test_edited_field_breaks_the_chain(self):
        self._write(5)
        rows = self._lines()
        rows[2]["i"] = 999                      # edit a payload field, leave hashes alone
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        result = audit_log.verify_chain(self.path)
        self.assertFalse(result["ok"], "an edited payload must not verify")
        self.assertIn(3, result["broken"], f"expected line 3 flagged, got {result['broken']}")

    def test_deleted_record_breaks_the_chain(self):
        # The failure mode that matters most in practice: quietly dropping an
        # inconvenient row rather than altering one.
        self._write(5)
        rows = self._lines()
        del rows[2]
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        self.assertFalse(audit_log.verify_chain(self.path)["ok"],
                         "deleting a record must not verify")

    def test_reordered_records_break_the_chain(self):
        self._write(5)
        rows = self._lines()
        rows[1], rows[3] = rows[3], rows[1]
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        self.assertFalse(audit_log.verify_chain(self.path)["ok"],
                         "reordering records must not verify")

    def test_appending_is_the_supported_operation(self):
        # Appending must keep verifying — otherwise the check would fire on normal
        # use and be ignored, which is how a broken alarm becomes no alarm.
        self._write(3)
        self.assertTrue(audit_log.verify_chain(self.path)["ok"])
        self._write(3)
        self.assertTrue(audit_log.verify_chain(self.path)["ok"])
        self.assertEqual(len(self._lines()), 6)

    def test_hmac_mode_rejects_a_chain_forged_without_the_key(self):
        # Documents the actual security boundary. An attacker who can write the file
        # can always recompute a valid *plain* chain; they cannot recompute a keyed
        # one. If this ever passes without the key, the HMAC path is not wired up.
        os.environ["AUDIT_HMAC_KEY"] = "test-key-not-a-real-secret"
        self.addCleanup(os.environ.pop, "AUDIT_HMAC_KEY", None)
        audit_log.reset_chain(self.path)
        self._write(3)
        self.assertTrue(audit_log.verify_chain(self.path)["ok"])

        # Re-hash the file as an attacker without the key would: plain SHA-256.
        rows = self._lines()
        rows[1]["i"] = 4242
        prev = rows[0]["hash"]
        for r in rows[1:]:
            r["prev_hash"] = prev
            body = {k: v for k, v in r.items() if k != "hash"}
            import hashlib
            r["hash"] = hashlib.sha256(
                json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            prev = r["hash"]
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        self.assertFalse(audit_log.verify_chain(self.path)["ok"],
                         "a plain-SHA256 forgery must fail verification under HMAC mode")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "audit.jsonl")
        self.addCleanup(self.tmp.cleanup)
        audit_log.reset_chain(self.path)

    def test_malformed_line_is_skipped_not_raised(self):
        # A partial final line is the expected result of a crash mid-append. One bad
        # row must not cost the caller the other hundred thousand.
        audit_log.write_audit_record({"event": "a"}, path=self.path)
        with open(self.path, "a") as fh:
            fh.write('{"event": "truncated"')          # no newline, no closing brace
        records = list(audit_log.iter_records(self.path, include_archives=False))
        self.assertEqual([r["event"] for r in records], ["a"])

    def test_missing_file_yields_nothing_rather_than_raising(self):
        records = list(audit_log.iter_records(
            os.path.join(self.tmp.name, "nope.jsonl"), include_archives=False))
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
