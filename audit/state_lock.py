"""
shared/state_lock.py — file-level write lock for bot state files.

pid_lock.py stops a second full bot *process* from starting, but does not
protect a state file (bot_state.json / sniper_state.json / crackshot_state.json
/ kalshi_state.json) from concurrent *writers within a single process tree* —
the dashboard, master-bot's orphan_redeemer.py and rebalancer.py, and each
bot's own save loop all read-modify-write the same files independently. This
is the write-level gap the pid_lock pattern doc (duplicate-bot-detection)
flags as still open.

Use:
    from state_lock import locked_write

    with locked_write(STATE_PATH):
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)

The lock file is a sibling of the state file (`<path>.lock`), so it survives
on the same persistent volume the state file lives on — never put it in /tmp.
Uses fcntl.flock (advisory, POSIX) which is process- and thread-safe; blocks
until acquired rather than failing, since state saves are infrequent and
short-lived and a hard failure here would drop a trade/position update.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging

log = logging.getLogger(__name__)


@contextlib.contextmanager
def locked_write(path: str):
    """Serialize writers to `path` via an exclusive advisory lock on `path + '.lock'`."""
    lock_path = path + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
