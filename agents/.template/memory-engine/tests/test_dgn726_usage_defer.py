# DGN-726: consolidate usage-exhaustion = silent defer + reset-aligned retry.
# Fully sandboxed: temp state.db + temp memories dir; all LLM / transcript /
# embedding / push calls stubbed; usage state stubbed (never hits the network or
# usage-gate). Never touches the live engine state or memories/.
#
# Run: python3 -m pytest memory-engine/tests/test_dgn726_usage_defer.py -v
#
# Coverage:
#   - usage-exhausted all-fail  -> NO push, exit 0, watermark preserved, marker written
#     with the next reset boundary (POST-HOC classifier).
#   - PRE-FLIGHT exhausted       -> compression skipped, exit 0, marker written, silent.
#   - genuine (non-usage) all-fail -> report built + exit 1 (DGN-182 visibility intact).
#   - reset-time computation      -> earliest exhausted-window reset chosen.

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


PAD = "x" * (memory.CONSOLIDATE_CHUNK_CHARS - 100)
CHUNK_OK = "alpha-topic " + PAD
CHUNK_BAD = "FAILME bravo-topic " + PAD


def _fail_all_compressor(text, prompt_prefix=None, model=None):
    # Simulate a usage-exhaustion rc=1: every chunk raises (as memory.py's real
    # compressor does on `sonnet exited abnormally (rc=1)`).
    raise RuntimeError("sonnet exited abnormally (rc=1): ")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    mem_dir = tmp_path / "memories"
    raw_dir = mem_dir / "_raw"
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(mem_dir))
    monkeypatch.setattr(memory, "TARGET_MEMORY_MD", str(mem_dir / "inbox.md"))
    monkeypatch.setattr(memory, "RAW_ARCHIVE_DIR", str(raw_dir))
    monkeypatch.setattr(
        memory, "CONSOLIDATE_DEADLETTER_PATH", str(raw_dir / "consolidate-dead-letter.jsonl")
    )
    # defer marker in the sandbox (never touches the real HERE/.consolidate-usage-defer.json).
    monkeypatch.setattr(memory, "CONSOLIDATE_DEFER_MARKER", str(tmp_path / ".defer.json"))

    monkeypatch.setattr(memory, "archive_raw_transcript", lambda wm, now: 0)
    monkeypatch.setattr(memory, "_second_stage_filter", lambda cands: (list(cands), [], False))
    monkeypatch.setattr(memory, "_rule_filter_candidates", lambda cands: (list(cands), []))
    monkeypatch.setattr(memory, "embed", lambda text: (_ for _ in ()).throw(OSError("no ollama")))
    monkeypatch.setattr(memory, "cmd_index", lambda a: None)
    return tmp_path


def run_consolidate(monkeypatch, convo, max_ts, n_msgs, no_push=False):
    monkeypatch.setattr(memory, "collect_transcript", lambda wm, now: (convo, max_ts, n_msgs))
    args = argparse.Namespace(dry_run=False, no_push=no_push, since_days=None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.cmd_consolidate(args)
    out = buf.getvalue()
    sys.stdout.write(out)
    return rc, out


def watermark(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    row = conn.execute("SELECT value FROM consolidation_state WHERE key='last_ts'").fetchone()
    conn.close()
    return row[0] if row else None


def marker(tmp_path):
    p = tmp_path / ".defer.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Reset-time computation
# ---------------------------------------------------------------------------

def test_next_reset_prefers_exhausted_window():
    # Only 5h exhausted -> its reset chosen even though 7d resets sooner.
    usage = {
        "five_hour_util": 100.0,
        "seven_day_util": 40.0,
        "five_hour_resets_at": "2026-08-05T12:00:00Z",
        "seven_day_resets_at": "2026-08-05T08:00:00Z",
    }
    assert memory._next_usage_reset_iso(usage) == "2026-08-05T12:00:00Z"


def test_next_reset_earliest_when_both_exhausted():
    usage = {
        "five_hour_util": 100.0,
        "seven_day_util": 100.0,
        "five_hour_resets_at": "2026-08-05T12:00:00Z",
        "seven_day_resets_at": "2026-08-05T08:00:00Z",
    }
    # both exhausted -> soonest boundary (earliest retry opportunity).
    assert memory._next_usage_reset_iso(usage) == "2026-08-05T08:00:00Z"


def test_next_reset_empty_when_no_times():
    assert memory._next_usage_reset_iso({"five_hour_util": 100.0}) == ""
    assert memory._next_usage_reset_iso(None) == ""


def test_usage_exhausted_predicate():
    assert memory._usage_exhausted({"five_hour_util": 100.0, "seven_day_util": 10.0})
    assert memory._usage_exhausted({"five_hour_util": 10.0, "seven_day_util": 99.5})
    assert not memory._usage_exhausted({"five_hour_util": 98.0, "seven_day_util": 90.0})
    assert not memory._usage_exhausted(None)  # unknown -> NOT exhausted (fail-open to visible)


# ---------------------------------------------------------------------------
# POST-HOC: all-fail + exhausted -> silent defer
# ---------------------------------------------------------------------------

def test_usage_defer_is_fully_silent(sandbox, monkeypatch):
    tmp_path = sandbox
    monkeypatch.setattr(memory, "compress_with_haiku", _fail_all_compressor)
    # usage exhausted at the post-hoc check.
    monkeypatch.setattr(memory, "_query_usage_state", lambda: {
        "five_hour_util": 100.0, "seven_day_util": 60.0,
        "five_hour_resets_at": "2026-08-05T12:00:00Z", "seven_day_resets_at": "",
    })
    sent = []
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: sent.append(rep) or True)

    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)

    assert rc == 0, "usage-defer must exit 0 so cron-guard stays silent"
    assert sent == [], "usage-defer must NOT push any report"
    assert "usage-defer" in out
    assert watermark(tmp_path) is None, "watermark preserved (span re-collected)"
    m = marker(tmp_path)
    assert m is not None and m["reason"] == "usage-exhaustion"
    assert m["reset_at"] == "2026-08-05T12:00:00Z"


# ---------------------------------------------------------------------------
# PRE-FLIGHT: exhausted before compression -> skip doomed calls, silent
# ---------------------------------------------------------------------------

def test_preflight_skips_compression(sandbox, monkeypatch):
    tmp_path = sandbox
    calls = {"n": 0}

    def _counting_compressor(text, prompt_prefix=None, model=None):
        calls["n"] += 1
        raise RuntimeError("should not be called")

    monkeypatch.setattr(memory, "compress_with_haiku", _counting_compressor)
    monkeypatch.setattr(memory, "_query_usage_state", lambda: {
        "five_hour_util": 99.5, "seven_day_util": 20.0,
        "five_hour_resets_at": "2026-08-05T09:30:00Z", "seven_day_resets_at": "",
    })
    sent = []
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: sent.append(rep) or True)

    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)

    assert rc == 0
    assert calls["n"] == 0, "pre-flight must skip the doomed sonnet calls entirely"
    assert sent == []
    assert "PRE-FLIGHT" in out
    assert watermark(tmp_path) is None
    m = marker(tmp_path)
    assert m is not None and m["reset_at"] == "2026-08-05T09:30:00Z"


# ---------------------------------------------------------------------------
# GENUINE (non-usage) all-fail -> visible (DGN-182 regression guard)
# ---------------------------------------------------------------------------

def test_genuine_failure_stays_visible(sandbox, monkeypatch):
    tmp_path = sandbox
    monkeypatch.setattr(memory, "compress_with_haiku", _fail_all_compressor)
    # usage NOT exhausted -> genuine defect classification.
    monkeypatch.setattr(memory, "_query_usage_state", lambda: {
        "five_hour_util": 30.0, "seven_day_util": 55.0,
        "five_hour_resets_at": "", "seven_day_resets_at": "",
    })
    sent = []
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: sent.append(rep) or True)

    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)

    assert rc == 1, "genuine failure must exit 1 -> cron-guard alert"
    assert len(sent) == 1, "genuine failure must push the snag report"
    assert "watermark preserved" in out
    assert watermark(tmp_path) is None
    assert marker(tmp_path) is None, "no defer marker on a genuine failure"


def test_genuine_failure_visible_when_usage_unknown(sandbox, monkeypatch):
    # usage lookup fails (None) -> must NOT silence; stays visible.
    tmp_path = sandbox
    monkeypatch.setattr(memory, "compress_with_haiku", _fail_all_compressor)
    monkeypatch.setattr(memory, "_query_usage_state", lambda: None)
    sent = []
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: sent.append(rep) or True)

    convo = CHUNK_OK + "\n" + CHUNK_BAD
    rc, out = run_consolidate(monkeypatch, convo, "2026-07-14T01:00:00.000000Z", 2)

    assert rc == 1
    assert len(sent) == 1
    assert marker(tmp_path) is None
