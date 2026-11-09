# test-lane: fast
# test-tier: 0
# test-component: memory
# test-prereq: tmp-files, pytest
# test-new-component: Restore the instance-only consolidate_error_logging boundary; no canonical suite owns these cases.
# test-covers: agents/.template/memory-engine/memory.py
# DGN-523: consolidate error-detail regression tests.
# Guards two fixes:
#   (A) compress_with_haiku raises RuntimeError with a clean message (no prompt embedded)
#       when TimeoutExpired is raised -- the old str(e) put the full 24k-char prompt into
#       last_error, consuming all 500 stored chars and making the actual exception invisible.
#   (B) _build_consolidate_report includes error_hint in the Telegram "snag" line so the
#       user notification names what failed instead of bare "please check".
#   (C) cmd_consolidate populates error_hint for both the partial-failure and full-failure
#       paths and passes it through to _build_consolidate_report.
#
# Fully sandboxed: no real LLM calls, no real file writes (dry-run / monkeypatch).
# Run: python3 -m pytest memory-engine/tests/test_dgn523_error_logging.py -v

import argparse
import contextlib
import io
import subprocess
import sys
import os

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

for key in list(os.environ):
    if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        os.environ.pop(key)

import memory  # noqa: E402


# ---------------------------------------------------------------------------
# (A) compress_with_haiku error message -- no prompt embedded
# ---------------------------------------------------------------------------

def test_timeout_error_does_not_embed_prompt(monkeypatch):
    """When the claude subprocess times out, the RuntimeError message must NOT
    include the prompt text (which could be 24 000 chars), just the exception
    class and the timeout value."""
    LONG_PROMPT = "A" * 24000

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(memory, "_scrub_claude_session_env", lambda: os.environ.copy())

    with pytest.raises(RuntimeError) as exc_info:
        memory.compress_with_haiku(LONG_PROMPT)

    msg = str(exc_info.value)
    # Must name the exception class and the timeout value
    assert "TimeoutExpired" in msg, f"exception class absent from: {msg!r}"
    assert "180" in msg, f"timeout value absent from: {msg!r}"
    # Must NOT contain the prompt content
    assert "AAAA" not in msg, f"prompt text leaked into error message (first 80 chars): {msg[:80]!r}"
    # Total length must be well under 500 (so last_error field gets the actual info)
    assert len(msg) < 200, f"error message too long ({len(msg)} chars); prompt may be embedded"


def test_oserror_message_safe(monkeypatch):
    """OSError messages (e.g. 'No such file or directory') are safe -- they do not
    include the prompt -- so they are passed through unchanged."""
    def fake_run(cmd, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(memory, "_scrub_claude_session_env", lambda: os.environ.copy())

    with pytest.raises(RuntimeError) as exc_info:
        memory.compress_with_haiku("small prompt")

    msg = str(exc_info.value)
    assert "OSError" in msg or "No such file" in msg


# ---------------------------------------------------------------------------
# (B) _build_consolidate_report includes error_hint
# ---------------------------------------------------------------------------

def test_report_snag_line_contains_hint():
    """When error_hint is supplied, the snag line must include it in parens."""
    hint = "3 chunk(s) failed compression"
    report = memory._build_consolidate_report(
        new_items=[], skipped=0, had_error=True, mem_lines=0, error_hint=hint
    )
    assert report is not None
    assert "Hit a snag during sorting" in report
    assert hint in report, f"hint not found in report: {report!r}"


def test_report_snag_line_no_hint():
    """When error_hint is None (or omitted), the snag line must still appear."""
    report = memory._build_consolidate_report(
        new_items=[], skipped=0, had_error=True, mem_lines=0
    )
    assert report is not None
    assert "Hit a snag during sorting" in report


def test_report_no_snag_when_no_error():
    """When had_error=False and no over-cap condition, no report is sent
    (0 new items, no error -> None)."""
    report = memory._build_consolidate_report(
        new_items=[], skipped=0, had_error=False, mem_lines=0
    )
    assert report is None


# ---------------------------------------------------------------------------
# (C) cmd_consolidate passes error_hint through (integration, sandboxed)
# ---------------------------------------------------------------------------

PAD = "x" * (memory.CONSOLIDATE_CHUNK_CHARS - 100)
CHUNK_OK = "alpha-topic " + PAD
CHUNK_BAD = "FAILME bravo-topic " + PAD


def make_failing_compressor():
    def fake(text, prompt_prefix=None, model=None):
        if "FAILME" in text:
            raise RuntimeError("simulated: sonnet call failed: TimeoutExpired: timeout after 180s")
        return [f"stable fact from chunk"]
    return fake


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
    monkeypatch.setattr(memory, "CONSOLIDATE_RETRY_MAX", 3)
    monkeypatch.setattr(
        memory, "archive_raw_transcript", lambda wm, now, until_iso=None: 0
    )
    monkeypatch.setattr(memory, "_second_stage_filter", lambda cands: (list(cands), [], False))
    monkeypatch.setattr(memory, "_rule_filter_candidates", lambda cands: (list(cands), []))
    monkeypatch.setattr(memory, "embed", lambda t: [0.0] * 384)
    monkeypatch.setattr(memory, "cmd_index", lambda a: None)
    # cmd_consolidate pre-flights live usage and defers (rc=75) when a window is
    # >= 99%; this test is about error_hint, not the usage gate, so pin "no
    # usage data" (fail-open) instead of reading the real account.
    monkeypatch.setattr(memory, "_query_usage_state", lambda: None)
    # keep the last-success stamp inside the sandbox (default is a tracked file)
    monkeypatch.setattr(memory, "CONSOLIDATE_LAST_SUCCESS", str(tmp_path / ".last-success"))
    # Capture what the report builder receives via a wrapper
    return tmp_path


def test_partial_failure_error_hint_passed(sandbox, monkeypatch):
    """When chunks partially fail, cmd_consolidate must call _build_consolidate_report
    with an error_hint naming the failure count."""
    captured = {}

    original_build = memory._build_consolidate_report

    def capturing_build(new_items, skipped, had_error, mem_lines, error_hint=None,
                        backlog_rows=0):
        captured["error_hint"] = error_hint
        captured["had_error"] = had_error
        return original_build(new_items, skipped, had_error, mem_lines, error_hint=error_hint)

    monkeypatch.setattr(memory, "_build_consolidate_report", capturing_build)
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: False)
    monkeypatch.setattr(memory, "compress_with_haiku", make_failing_compressor())
    monkeypatch.setattr(
        memory, "_iter_transcript_rows",
        lambda wm, now: [
            ("2026-07-23T03:59:00.000000Z", "\ud615\ub2d8", CHUNK_OK, "test"),
            ("2026-07-23T04:00:00.000000Z", "\ud615\ub2d8", CHUNK_BAD, "test"),
        ],
    )

    args = argparse.Namespace(dry_run=False, no_push=False, since_days=None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.cmd_consolidate(args)

    assert rc == 0
    assert captured.get("had_error") is True, "had_error not set"
    hint = captured.get("error_hint") or ""
    assert "chunk" in hint.lower(), f"error_hint missing chunk count: {hint!r}"
    assert "failed" in hint.lower(), f"error_hint missing 'failed': {hint!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
