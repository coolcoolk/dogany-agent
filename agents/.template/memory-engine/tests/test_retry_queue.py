# DGN-153: consolidate partial-failure retry queue tests.
# Fully sandboxed: temp state.db + temp memories dir, all LLM / transcript /
# embedding calls stubbed. Never touches the live engine state or memories/.
#
# Run: python3 -m pytest memory-engine/tests/test_retry_queue.py -v

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402


# A line longer than CONSOLIDATE_CHUNK_CHARS becomes its own chunk, so two long
# lines produce exactly two chunks.
PAD = "x" * (memory.CONSOLIDATE_CHUNK_CHARS - 100)
CHUNK_OK = "alpha-topic " + PAD
CHUNK_BAD = "FAILME bravo-topic " + PAD


def make_fake_compressor(fail_markers):
    """Compressor stub: raises RuntimeError when the text contains any marker
    currently in fail_markers (a mutable set), else returns one fake item
    derived from the chunk's leading token."""

    def fake(text, prompt_prefix=None, model=None):
        for marker in fail_markers:
            if marker in text:
                raise RuntimeError("simulated model failure (quota)")
        token = text.split()[0] if text.split() else "empty"
        return [f"stable fact extracted from {token} chunk"]

    return fake


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect all engine state into tmp_path and stub externals."""
    mem_dir = tmp_path / "memories"
    raw_dir = mem_dir / "_raw"
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(mem_dir))
    monkeypatch.setattr(memory, "TARGET_MEMORY_MD", str(mem_dir / "inbox.md"))
    monkeypatch.setattr(memory, "RAW_ARCHIVE_DIR", str(raw_dir))
    monkeypatch.setattr(
        memory, "CONSOLIDATE_DEADLETTER_PATH", str(raw_dir / "consolidate-dead-letter.jsonl")
    )
    monkeypatch.setattr(memory, "CONSOLIDATE_RETRY_MAX", 3)  # small cap for the test

    # Stub externals: raw archive (reads live session jsonl), second-stage
    # filter + rule filter (claude CLI), embedding (Ollama), re-index, push.
    monkeypatch.setattr(memory, "archive_raw_transcript", lambda wm, now: 0)
    monkeypatch.setattr(memory, "_second_stage_filter", lambda cands: (list(cands), [], False))
    monkeypatch.setattr(memory, "_rule_filter_candidates", lambda cands: (list(cands), []))
    monkeypatch.setattr(memory, "embed", _raise_oserror)
    monkeypatch.setattr(memory, "cmd_index", lambda a: None)
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: False)
    return tmp_path


def _raise_oserror(text):
    raise OSError("no ollama in test sandbox")


def run_consolidate(monkeypatch, convo, max_ts, n_msgs):
    """Run cmd_consolidate with a stubbed transcript; return (rc, stdout)."""
    monkeypatch.setattr(memory, "collect_transcript", lambda wm, now: (convo, max_ts, n_msgs))
    args = argparse.Namespace(dry_run=False, no_push=True, since_days=None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.cmd_consolidate(args)
    out = buf.getvalue()
    sys.stdout.write(out)  # keep evidence visible in -s runs
    return rc, out


def q_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, chunk_text, retry_count FROM consolidate_retry_queue ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def watermark(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    row = conn.execute("SELECT value FROM consolidation_state WHERE key='last_ts'").fetchone()
    conn.close()
    return row[0] if row else None


def inbox_text(tmp_path):
    p = tmp_path / "memories" / "inbox.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_partial_failure_queues_and_next_run_drains(sandbox, monkeypatch):
    tmp_path = sandbox
    fail_markers = {"FAILME"}
    monkeypatch.setattr(memory, "compress_with_haiku", make_fake_compressor(fail_markers))

    # Run 1: chunk 1 ok, chunk 2 fails -> queued; watermark still advances.
    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)
    assert rc == 0
    assert "1 chunk(s) queued for retry" in out
    rows = q_rows(tmp_path)
    assert len(rows) == 1
    assert "FAILME" in rows[0]["chunk_text"]
    assert watermark(tmp_path) == "2026-07-14T01:00:00.000000Z"
    assert "alpha-topic" in inbox_text(tmp_path)          # successful chunk stored
    assert "FAILME" not in inbox_text(tmp_path)           # failed chunk not stored yet

    # Run 2: model recovered, no new conversation -> retry-only run drains the queue.
    fail_markers.clear()
    rc, out = run_consolidate(monkeypatch, "", None, 0)
    assert rc == 0
    assert "retry queue only" in out
    assert "1 chunk(s) drained" in out
    assert q_rows(tmp_path) == []                          # queue empty after success
    assert "FAILME" in inbox_text(tmp_path)                # retried chunk's fact stored
    assert watermark(tmp_path) == "2026-07-14T01:00:00.000000Z"  # untouched on retry-only run


def test_chunk_survives_repeated_failure_then_dead_letters(sandbox, monkeypatch):
    tmp_path = sandbox
    fail_markers = {"FAILME"}
    monkeypatch.setattr(memory, "compress_with_haiku", make_fake_compressor(fail_markers))

    # Queue the failing chunk (run 1: partial failure).
    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)
    assert rc == 0
    assert len(q_rows(tmp_path)) == 1

    # Retries 1..(MAX-1): chunk keeps failing but SURVIVES in the queue.
    for attempt in range(1, memory.CONSOLIDATE_RETRY_MAX):
        rc, out = run_consolidate(monkeypatch, "", None, 0)
        assert rc == 0
        rows = q_rows(tmp_path)
        assert len(rows) == 1, f"chunk dropped after attempt {attempt}"
        assert rows[0]["retry_count"] == attempt

    # Retry MAX: capped out -> moved to dead-letter file, never deleted.
    rc, out = run_consolidate(monkeypatch, "", None, 0)
    assert rc == 0
    assert "1 moved to dead-letter" in out
    assert q_rows(tmp_path) == []
    dl_path = memory.CONSOLIDATE_DEADLETTER_PATH
    assert os.path.isfile(dl_path)
    recs = [json.loads(ln) for ln in open(dl_path, encoding="utf-8")]
    assert len(recs) == 1
    assert "FAILME" in recs[0]["chunk_text"]
    assert recs[0]["retry_count"] == memory.CONSOLIDATE_RETRY_MAX

    # Dead-letter file survives the raw-archive pruner (only *.jsonl.gz pruned).
    memory.prune_raw_archive()
    assert os.path.isfile(dl_path)


def test_full_failure_preserves_watermark_and_queues_nothing(sandbox, monkeypatch):
    tmp_path = sandbox
    fail_markers = {"alpha-topic", "FAILME"}  # every chunk fails
    monkeypatch.setattr(memory, "compress_with_haiku", make_fake_compressor(fail_markers))

    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)
    assert rc == 1
    assert "watermark preserved" in out
    assert q_rows(tmp_path) == []          # nothing queued: whole span re-collected next run
    assert watermark(tmp_path) is None
