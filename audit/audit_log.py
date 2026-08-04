"""
shared/audit_log.py — Shared audit log writer for all Polymarket bots.

Path resolution order:
  1. AUDIT_LOG_PATH env var (set per-bot in docker-compose.yml)
  2. /app/audit.jsonl  (main bot default — /app is the polymarket-bot mount)
  3. /tmp/bot_audit.jsonl  (fallback for sniper/crackshot/kalshi)

Records are tamper-evident: each one carries the hash of the previous record, so
altering or deleting a line invalidates every hash after it. This matters because
the audit log is the sole evidence base for CLV analysis, per-strategy win rates and
every backtest conclusion drawn from live trading — a silently corrupted log yields
confident, wrong answers about which strategies have edge. It was previously an
unlocked bare append with no integrity check of any kind.

Integrity strength depends on AUDIT_HMAC_KEY:
  - set   → HMAC-SHA256. Detects deliberate tampering: an attacker who can write the
            file still cannot forge hashes without the key. Keep the key OUT of the
            log's directory (it is an env var, not a file, for this reason).
  - unset → plain SHA-256 chain. Detects truncation, corruption and careless edits,
            but anyone who can write the file can recompute a consistent chain.
"""
import glob
import gzip
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

from state_lock import locked_write

AUDIT_LOG = os.environ.get(
    "AUDIT_LOG_PATH",
    "/app/audit.jsonl" if os.path.isdir("/app") else "/tmp/bot_audit.jsonl",
)

# Rotation bounds. Past ROTATE_MAX_BYTES the full file is gzipped into a sibling
# archive/ dir and the live file is rewritten with only the last ROTATE_KEEP_LINES,
# so analysis tooling keeps reading current data without the file growing forever.
# Values match the thresholds bot.py used before rotation moved here.
ROTATE_MAX_BYTES  = 50 * 1024 * 1024
ROTATE_KEEP_LINES = 50_000

# Archives older than this are deleted after a successful rotation. Rotation
# bounded the live file but nothing ever bounded archive/, which had reached
# 223 MB across the two bots by 2026-07-29 and only grows. 90 days comfortably
# covers reconcile's lookback and any CLV analysis window.
#
# Deletion happens here rather than in disk_watchdog.sh on purpose: the watchdog
# is alert-only and must stay that way. Pruning belongs next to the code that
# creates the files, under the same lock that created them.
ARCHIVE_RETENTION_DAYS = 90

# Fields excluded when hashing a record: `hash` is the output itself, and including
# it would be self-referential.
_HASH_FIELD = "hash"
_PREV_FIELD = "prev_hash"

# Cache of the last hash per path so a normal append does not re-read a 30MB file.
_last_hash_cache: dict = {}


def _hmac_key():
    key = os.environ.get("AUDIT_HMAC_KEY", "")
    return key.encode() if key else None


def compute_hash(prev_hash, record: dict) -> str:
    """
    Hash of `record` chained to `prev_hash`.

    Serialization is canonical (sorted keys, no incidental whitespace) so the same
    logical record always hashes identically regardless of dict ordering.
    """
    body = {k: v for k, v in record.items() if k != _HASH_FIELD}
    payload = (prev_hash or "") + json.dumps(body, sort_keys=True, separators=(",", ":"),
                                             default=str)
    key = _hmac_key()
    if key:
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_last_hash(path: str):
    """
    Last record's hash, or None when the chain should restart here.

    Returns None for a missing/empty file (genesis) and for a pre-integrity legacy
    record (the ~2.5M records written before hashing existed). Reads only the tail
    so cost does not grow with the log.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return None
    try:
        with open(path, "rb") as fh:
            # 64KB comfortably covers one record; audit lines are ~1-2KB.
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace").strip().split("\n")
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line).get(_HASH_FIELD)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return None


def reset_chain(path: str) -> None:
    """
    Forget the cached chain tip for `path`.

    Call after anything replaces the file out from under this process, so the next
    append re-reads the tip from disk instead of chaining onto a record that may no
    longer be there. write_audit_record already does this after it rotates.

    Note it does NOT force a new segment after a normal rotation: rotation keeps the
    tail, so the re-read finds the last retained record and the chain simply continues.
    A fresh segment only results when the file is genuinely empty afterwards.
    """
    _last_hash_cache.pop(path, None)


def archive_paths(path: str) -> list:
    """Rotated generations of `path`, oldest first.

    Naming has drifted across versions — `<base>.<ts>.gz`, `<base>.<date>.gz` and
    `<stem>-<date>.jsonl.gz` all exist on disk — so match both shapes and order by
    mtime rather than by filename, which does not sort consistently across them.
    Patterns stay prefixed with this log's own basename/stem because shared-tmp/
    archive/ holds crackshot, sniper and orphan generations side by side.
    """
    adir = os.path.join(os.path.dirname(path) or ".", "archive")
    base = os.path.basename(path)
    stem = base.split(".")[0]
    found = set()
    for pat in (f"{base}*.gz", f"{stem}-*.gz"):
        found.update(glob.glob(os.path.join(adir, pat)))
    return sorted(found, key=lambda p: os.path.getmtime(p))


def iter_records(path: str = None, include_archives: bool = True, dedupe: bool = False):
    """Yield every audit record for `path` in chronological order.

    Rotation means the live file holds only the most recent ROTATE_KEEP_LINES. Any
    reader that opens `path` directly therefore sees a window, not a history — and
    for a log whose useful rows are rare that window can contain none of them. On
    2026-07-30 kalshi_audit.jsonl held 101,575 rows and zero fills: all 32 fills
    and all 3 settlements had aged into archive/, invisible to both calibration and
    the startup rehydration that reconstructs open positions from this log. Reading
    archives is what makes those readers see the trade record instead of a slice of
    gate noise.

    Malformed lines are skipped rather than raising: a partial final line is the
    expected result of a crash mid-append, and one bad row must not cost a caller
    the other hundred thousand.

    dedupe: when True, a record whose exact content was already yielded is skipped.
    Archives can genuinely overlap — a rotation that copied a file without truncating
    it leaves the copy AND the next rotation both containing the same rows. On disk
    right now `audit-2026-07-15.jsonl.gz` and `audit.jsonl.20260724.gz` share 734
    records (including 172 position_settled rows), so a naive P&L sum over
    include_archives doubles those settlements (measured: $417 vs $206 deduped).
    Existing consumers (report.py, reconcile.py) dedupe downstream on their own keys
    and so are unaffected — this flag is for any future aggregator, and it is opt-in
    because the seen-set costs ~16 bytes per unique record (hundreds of MB over the
    full multi-million-row history) which most callers, filtering to rare event types
    immediately, should not pay.
    """
    path = path or AUDIT_LOG
    sources = (archive_paths(path) if include_archives else []) + [path]
    seen = set() if dedupe else None
    for src in sources:
        try:
            opener = gzip.open if src.endswith(".gz") else open
            with opener(src, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    if seen is not None:
                        # 128-bit content digest — exact-identity dedup without holding
                        # the full ~500-byte line in memory for every one of millions.
                        h = hashlib.blake2b(line.encode("utf-8", "replace"),
                                            digest_size=16).digest()
                        if h in seen:
                            continue
                        seen.add(h)
                    try:
                        yield json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except FileNotFoundError:
            continue
        except OSError:
            continue


def _rotate_unlocked(path: str, log: logging.Logger = None,
                     max_bytes: int = None, keep_lines: int = None) -> bool:
    """
    Gzip `path` into a sibling archive/ dir and keep only its last `keep_lines`.
    Returns True if rotation happened.

    **Acquires no lock, by design.** Its only caller is write_audit_record, which
    already holds locked_write(path). fcntl.flock is per-open-file-description, so
    opening `<path>.lock` a second time inside that block would block waiting on a
    lock this same process holds — a self-deadlock that would freeze the trading
    loop. Callers outside that block must take locked_write(path) themselves.

    Holding the caller's lock across gzip + rewrite also closes the race the old
    bot.py-local version had: it read the whole file and os.replace'd it with no
    lock at all, so any append landing in that window was silently dropped.
    """
    max_bytes  = ROTATE_MAX_BYTES  if max_bytes  is None else max_bytes
    keep_lines = ROTATE_KEEP_LINES if keep_lines is None else keep_lines
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return False
    except OSError:
        return False

    adir = os.path.join(os.path.dirname(path) or ".", "archive")
    os.makedirs(adir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = os.path.join(adir, os.path.basename(path) + f".{ts}.gz")

    # Archive first, streamed — the live file is untouched until it is complete, so a
    # crash mid-gzip leaves a partial .gz but loses no records.
    with open(path, "rb") as fi, gzip.open(archive, "wb") as fo:
        for chunk in iter(lambda: fi.read(1 << 20), b""):
            fo.write(chunk)

    # Never loads more than max_bytes, since we only get here above the cap.
    # Guard keep_lines=0 explicitly: `lines[-0:]` is `lines[0:]`, i.e. the WHOLE file,
    # so the naive slice would silently retain everything for the one value that most
    # obviously means "retain nothing".
    with open(path) as f:
        lines = f.readlines()
    tail = lines[-keep_lines:] if keep_lines > 0 else []
    tmp = path + ".rot.tmp"
    with open(tmp, "w") as f:
        f.writelines(tail)
    os.replace(tmp, path)   # atomic swap of the live file

    if log:
        log.info("Rotated %s -> %s (kept last %d lines)", path, archive, len(tail))

    _prune_archives(adir, os.path.basename(path), log)
    return True


def _prune_archives(adir: str, basename: str, log: logging.Logger = None,
                    retention_days: int = None) -> int:
    """Delete this log's archives older than the retention window.

    Scoped to `basename` so one bot's rotation never touches another's archives
    in a shared directory (shared-tmp/archive/ holds crackshot, sniper and
    orphan logs side by side).

    Best-effort: a failure here must never propagate: the rotation itself has
    already succeeded and the caller is mid-append on a live trading loop.
    Returns the number of files removed.
    """
    days = ARCHIVE_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return 0

    cutoff = time.time() - days * 86400
    removed = 0
    # Two naming shapes exist on disk: "audit.jsonl.<ts>.gz" (current) and
    # "audit-2026-07-15.jsonl.gz" (older, and the single largest archive). Match
    # on the stem plus an explicit "." or "-" separator so both are covered
    # without "audit" also swallowing a differently-named sibling log.
    stem = basename.split(".")[0]
    prefixes = (stem + ".", stem + "-")
    try:
        for name in os.listdir(adir):
            if not name.startswith(prefixes) or not name.endswith(".gz"):
                continue
            fp = os.path.join(adir, name)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed

    if removed and log:
        log.info("Pruned %d archive(s) older than %d days from %s", removed, days, adir)
    return removed


def write_audit_record(record: dict, path: str = None, log: logging.Logger = None) -> None:
    """
    Append one JSON record to the audit log, chained to the previous record.

    The append is serialized with the same advisory lock used for state files: several
    processes (each bot, the dashboard, master-bot's redeemer) can hold the same path,
    and an unlocked append can interleave partial lines — which would now also break
    the chain, turning a silent corruption into a visible one.

    Rotation is checked here too, so every bot writing through this function gets a
    bounded audit log. It runs under the same lock acquisition as the append, making
    rotate-then-append a single critical section.
    """
    # Guard against the caller shape `write_audit_record(rec, log)` — the logger lands
    # in `path`, is truthy, and `locked_write(path)` then does `path + ".lock"`, raising
    # TypeError. That escaped the old `except OSError` below and aborted the caller
    # mid-redemption (crackshot/sniper wrote no audit rows for days, and successful
    # redeem txs were filed as failures). Accept it and carry on rather than blow up.
    if path is not None and not isinstance(path, (str, os.PathLike)):
        if log is None and isinstance(path, logging.Logger):
            log = path
        if log:
            log.warning(
                "write_audit_record got a %s for `path` — using the default log. "
                "Call signature is (record, path, log).", type(path).__name__,
            )
        path = None

    path = path or AUDIT_LOG
    try:
        with locked_write(path):
            # Must run before the tip is read: rotation rewrites the very file
            # _read_last_hash reads from, so reading first could chain onto a record
            # that rotation then moved into the archive.
            if _rotate_unlocked(path, log):
                _last_hash_cache.pop(path, None)

            prev = _last_hash_cache.get(path)
            if prev is None:
                prev = _read_last_hash(path)
            record[_PREV_FIELD] = prev
            record[_HASH_FIELD] = compute_hash(prev, record)
            with open(path, "a") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
            _last_hash_cache[path] = record[_HASH_FIELD]
    except Exception as exc:
        # A failed audit write must never take the bot down, but it does invalidate
        # the cached tip — the next write re-reads it rather than chaining onto a
        # record that may not have landed. Deliberately broader than OSError: this
        # function is called from inside redeem_resolved's try block, so anything that
        # escapes here is misread as a redemption failure for a tx that already landed.
        _last_hash_cache.pop(path, None)
        if log:
            log.warning("Could not write audit record: %s", exc)


def verify_chain(path: str = None, max_lines: int = None) -> dict:
    """
    Verify the hash chain of an audit log.

    Returns a summary dict:
        total     — records read
        hashed    — records carrying a hash (i.e. written after this feature)
        legacy    — records predating hashing; counted, not an error
        segments  — number of chain starts, i.e. records with prev_hash=None. Caused by
                    a genuine restart of the chain: the first record after the legacy
                    (pre-hashing) prefix, or a writer whose cached tip was cleared
                    against an empty file. **Rotation does not increment this.**
        broken    — 1-indexed line numbers whose hash does not match its contents, or
                    whose prev_hash does not match the preceding record
        ok        — True when broken is empty

    Why rotation is not a segment, and not corruption either: rotation keeps the TAIL of
    the file, so the first retained record still carries the prev_hash of a record that
    now lives only in archive/*.gz. That anchor is dangling but non-null, so it is
    neither a segment start nor a mismatch — the `saw_hashed` guard below skips the
    prev_hash comparison for the first hashed record precisely so this one unresolvable
    anchor is tolerated. A rotated log therefore reports segments=0, broken=[], ok=True.

    The tolerance is deliberately exactly one record deep. A dangling prev_hash appearing
    anywhere later means a record really was removed from the middle, and is reported.
    """
    path = path or AUDIT_LOG
    out = {"path": path, "total": 0, "hashed": 0, "legacy": 0,
           "segments": 0, "broken": [], "ok": True}
    try:
        fh = open(path, "r")
    except OSError as exc:
        out["ok"] = False
        out["error"] = str(exc)
        return out

    prev_hash = None
    saw_hashed = False
    with fh:
        for i, line in enumerate(fh, start=1):
            if max_lines and out["total"] >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            out["total"] += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                out["broken"].append(i)
                continue

            if _HASH_FIELD not in rec:
                out["legacy"] += 1
                prev_hash = None       # legacy prefix; chain resumes after it
                continue

            out["hashed"] += 1
            claimed_prev = rec.get(_PREV_FIELD)
            if claimed_prev is None:
                out["segments"] += 1
            elif saw_hashed and claimed_prev != prev_hash:
                out["broken"].append(i)

            if compute_hash(claimed_prev, rec) != rec[_HASH_FIELD]:
                out["broken"].append(i)

            prev_hash = rec[_HASH_FIELD]
            saw_hashed = True

    out["broken"] = sorted(set(out["broken"]))
    out["ok"] = not out["broken"]
    return out


if __name__ == "__main__":  # pragma: no cover
    import sys
    result = verify_chain(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)
