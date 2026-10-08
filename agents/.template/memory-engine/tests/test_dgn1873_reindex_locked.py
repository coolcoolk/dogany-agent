# DGN-1873: classify-inbox's post-classify reindex vs a held sqlite write lock.
# Incident 2026-10-06 04:08: classification 10/10 chunks ok, then cmd_index ->
# "database is locked" -> rc=1 -> owner failure alarm, although the
# classification was already on disk. Fully sandboxed: temp state.db + temp
# memories dir, classifier + embedding + push stubbed.
#
# Run: python3 -m pytest memory-engine/tests/test_dgn1873_reindex_locked.py -v

import argparse
import os
import sqlite3
import sys
import threading

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

TOPIC = """<!-- dogany-format: section -->
### About user
§
stub item
§
"""


def _inbox(n):
    lines = ["<!-- dogany-format: section -->", "### unclassified (inbox)", "§"]
    for i in range(1, n + 1):
        lines += [f"item-{i} content for the reindex lock test", "§"]
    return "\n".join(lines) + "\n"


def _raise_oserror(_text):
    raise OSError("embedding stubbed out")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "about-user.md").write_text(TOPIC, encoding="utf-8")
    (mem / "inbox.md").write_text(_inbox(3), encoding="utf-8")
    verdicts = tmp_path / "verdicts.txt"
    verdicts.write_text("1 about-user.md\n2 about-user.md\n3 DROP\n", encoding="utf-8")
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(mem))
    monkeypatch.setattr(memory, "CLASSIFY_CMD", f"cat {verdicts}")
    monkeypatch.setattr(memory, "embed", _raise_oserror)
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: False)
    # Short waits so a held lock resolves in well under a second.
    monkeypatch.setattr(memory, "INDEX_BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(memory, "REINDEX_RETRY_DELAYS", (0.3, 0.3))
    monkeypatch.setattr(memory, "REINDEX_LOCK_WAIT_SEC", 0.5)
    monkeypatch.setattr(memory, "REINDEX_LOCK_PATH", str(tmp_path / ".reindex.lock"))
    # Build the index once so the classify run's reindex has real rows to rewrite
    # (the incident failed on DELETE FROM notes_fts of a changed topic file).
    assert memory.cmd_index(argparse.Namespace(lock=None)) == 0
    return tmp_path


def _hold_write_lock(db_path):
    """A second connection holding the database write lock (BEGIN IMMEDIATE)."""
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _classify():
    return memory.cmd_classify_inbox(argparse.Namespace(dry_run=False, no_push=True))


def test_held_write_lock_reproduces_incident(sandbox):
    """Pre-fix shape: a bare cmd_index under a held write lock raises."""
    (sandbox / "memories" / "about-user.md").write_text(TOPIC + "changed\n§\n", encoding="utf-8")
    holder = _hold_write_lock(memory.DB_PATH)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            memory.cmd_index(argparse.Namespace(lock=None))
    finally:
        holder.rollback()
        holder.close()


def test_lock_held_throughout_classify_kept_rc3(sandbox):
    holder = _hold_write_lock(memory.DB_PATH)
    try:
        rc = _classify()
    finally:
        holder.rollback()
        holder.close()
    assert rc == memory.CLASSIFY_RC_REINDEX_DEFERRED == 3
    # The classification result is on disk regardless of the reindex.
    topic = (sandbox / "memories" / "about-user.md").read_text(encoding="utf-8")
    assert "item-1 content" in topic and "item-2 content" in topic
    assert memory.cmd_inbox_count(argparse.Namespace()) == memory.CLASSIFY_RC_EMPTY
    # Our lock file is released.
    assert not os.path.exists(memory.REINDEX_LOCK_PATH)


def test_transient_lock_is_retried_rc0(sandbox):
    holder = _hold_write_lock(memory.DB_PATH)
    # Released during the first retry pause: attempt 1 fails, attempt 2 succeeds.
    threading.Timer(0.2, lambda: (holder.rollback(), holder.close())).start()
    rc = _classify()
    assert rc == memory.CLASSIFY_RC_OK
    conn = sqlite3.connect(memory.DB_PATH)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE source_file='about-user.md' AND text LIKE 'item-1%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "the classified item is searchable after the retried reindex"


def test_reindex_waits_for_the_shared_lock(sandbox):
    """A hook background index holding .reindex.lock is waited out, not raced."""
    lock = memory.REINDEX_LOCK_PATH
    with open(lock, "w") as f:
        f.write("99999")
    threading.Timer(0.2, lambda: os.remove(lock)).start()
    assert memory._reindex_locked_with_retry() is True
    assert not os.path.exists(lock)


def test_foreign_lock_never_removed(sandbox, monkeypatch):
    """Lock still held past the wait: proceed without it, leave it in place."""
    lock = memory.REINDEX_LOCK_PATH
    with open(lock, "w") as f:
        f.write("99999")
    monkeypatch.setattr(memory, "REINDEX_LOCK_WAIT_SEC", 0.1)
    assert memory._reindex_locked_with_retry() is True
    assert os.path.exists(lock)


def test_index_wait_lock_cli(sandbox, monkeypatch):
    """`memory.py index --wait-lock` (the check wrapper's pending-reindex retry)."""
    (sandbox / "memories" / "about-user.md").write_text(TOPIC + "changed\n\u00a7\n", encoding="utf-8")
    holder = _hold_write_lock(memory.DB_PATH)
    try:
        monkeypatch.setattr(sys, "argv", ["memory.py", "index", "--wait-lock"])
        assert memory.main() == 1
    finally:
        holder.rollback()
        holder.close()
    monkeypatch.setattr(sys, "argv", ["memory.py", "index", "--wait-lock"])
    assert memory.main() == 0


def test_consolidate_index_calls_are_locked_and_retried():
    """Q5: consolidate's own reindexes (pre-dedup refresh, post-store re-index)
    and the nightly wrapper's pre-index take the same lock + busy retry."""
    import inspect

    src = inspect.getsource(memory.cmd_consolidate)
    assert "cmd_index(" not in src, "a bare cmd_index in consolidate dies on a held lock"
    assert src.count("_reindex_locked_with_retry()") == 2
    wrapper = os.path.join(ENGINE_DIR, "..", "routines", "consolidate-0430.sh")
    with open(wrapper, encoding="utf-8") as fh:
        body = fh.read()
    assert "memory.py index --wait-lock" in body
    assert "memory.py index\n" not in body
