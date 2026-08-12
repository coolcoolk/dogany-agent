# DGN-501: same-day cross-session recall (LOCKED spec v2) regression tests.
# Matrix U1..U6 from spec section 11. All sandboxed: temp DB + synthetic jsonl
# fixtures, zero LLM, zero embedding, zero network.
#
# Run: /usr/bin/python3 -m pytest memory-engine/tests/test_dgn501_sameday_recall.py -v
#   (macOS system python3.9 -- exercises _parse_iso_utc's fromisoformat-Z fallback)
#
# E2E (spec row E2E) is a real-environment bidirectional check (telegram <-> CLI)
# and cannot be a unit test. Manual procedure:
#   1. In a Telegram session, say a unique 2-char keyword (e.g. "워그").
#   2. From CLI: `cd memory-engine && /usr/bin/python3 memory.py search "워그"`
#      -> the utterance must appear as a kind:transcript result.
#   3. Trigger a hook turn in the CLI session -> the [오늘 다른 세션 ...] block
#      must contain the Telegram utterance.
#   4. Reverse direction (CLI utterance -> Telegram session hook) must behave
#      identically.

import argparse
import datetime
import io
import json
import os
import sqlite3
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402


# ---------------------------------------------------------------------------
# helpers: synthetic jsonl fixtures + isolated DB
# ---------------------------------------------------------------------------

def _utc_iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _msg(typ, text, ts):
    """One transcript jsonl line (user/assistant) with a string content block."""
    return json.dumps(
        {"type": typ, "timestamp": ts, "message": {"role": typ, "content": text}},
        ensure_ascii=False,
    )


def _write_session(proj_dir, session, lines):
    path = os.path.join(proj_dir, f"{session}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate DB_PATH + TRANSCRIPT_GLOB into tmp; return a namespace helper."""
    db_path = tmp_path / "state.db"
    proj_dir = tmp_path / "projects"
    proj_dir.mkdir()
    monkeypatch.setattr(memory, "DB_PATH", str(db_path))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(proj_dir / "*.jsonl"))

    class Box:
        pass

    b = Box()
    b.tmp_path = tmp_path
    b.proj_dir = str(proj_dir)
    # 'now' anchored well after the fixture timestamps so mtime skip keeps files.
    b.now = datetime.datetime.now().astimezone()
    return b


def _fresh_conn():
    conn = memory.connect()
    memory.init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# U1: transcript_search tokenization / ranking
# ---------------------------------------------------------------------------

def test_u1_tokenize_and_rank(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    lines = [
        _msg("user", "워그 배포 완료", _utc_iso(base)),
        _msg("user", "티켓 정리", _utc_iso(base + datetime.timedelta(minutes=1))),
        _msg("user", "점심", _utc_iso(base + datetime.timedelta(minutes=2))),
    ]
    _write_session(sandbox.proj_dir, "S1", lines)

    conn = _fresh_conn()
    memory.index_transcript_notes(conn, now=sandbox.now)
    hits = memory.transcript_search(conn, "워그 배포 어떻게 됐지", 5, now=sandbox.now)
    conn.close()

    assert hits, "expected at least one hit"
    assert hits[0]["text"] == "워그 배포 완료", f"top hit should be the 2-token match: {hits!r}"
    texts = [h["text"] for h in hits]
    assert "점심" not in texts, "single-char '점심' must not match"


# ---------------------------------------------------------------------------
# U2: FATAL2 -- 2-char Korean probe (LIKE hits, trigram MATCH does not)
# ---------------------------------------------------------------------------

def test_u2_two_char_korean(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    _write_session(
        sandbox.proj_dir, "S1", [_msg("user", "워그 릴리스 얘기", _utc_iso(base))]
    )

    conn = _fresh_conn()
    memory.index_transcript_notes(conn, now=sandbox.now)

    # LIKE lane: 2-char single token must hit.
    hits = memory.transcript_search(conn, "워그", 5, now=sandbox.now)
    assert len(hits) == 1, f"2-char LIKE must return 1 hit: {hits!r}"
    assert "워그" in hits[0]["text"]

    # Contrast: the same 2-char query as a trigram FTS phrase MATCH is 0 hits.
    # (This is exactly the FATAL2 defect the LIKE lane fixes.) Build a throwaway
    # trigram FTS over the same row to demonstrate.
    conn.execute(
        "CREATE VIRTUAL TABLE probe_fts USING fts5(text, tokenize='trigram')"
    )
    conn.execute("INSERT INTO probe_fts(text) VALUES('워그 릴리스 얘기')")
    fts_rows = conn.execute(
        "SELECT text FROM probe_fts WHERE probe_fts MATCH ?", ('"워그"',)
    ).fetchall()
    conn.close()
    assert len(fts_rows) == 0, "trigram MATCH on a 2-char token must be 0 (FATAL2 contrast)"


# ---------------------------------------------------------------------------
# U3: FATAL1 + MAJOR1 ingest -- raw preservation + marker rows dropped
# ---------------------------------------------------------------------------

U3_LINES_SPECS = None


def _u3_lines(base):
    return [
        _msg("user", "이거 왜 안 돼?", _utc_iso(base)),
        _msg("assistant", "형님, 확인했습니다", _utc_iso(base + datetime.timedelta(seconds=10))),
        _msg("user", "<task-notification> done", _utc_iso(base + datetime.timedelta(seconds=20))),
        _msg("user", "[cron-inject] morning brief", _utc_iso(base + datetime.timedelta(seconds=30))),
        _msg("user", "[DGN-520 continuous work loop] step", _utc_iso(base + datetime.timedelta(seconds=40))),
        _msg("user", "Stop hook feedback: footer", _utc_iso(base + datetime.timedelta(seconds=50))),
    ]


def test_u3_raw_preserved_markers_dropped(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    _write_session(sandbox.proj_dir, "S1", _u3_lines(base))

    conn = _fresh_conn()
    memory.index_transcript_notes(conn, now=sandbox.now)
    rows = [dict(r) for r in conn.execute("SELECT role, text FROM transcript_notes ORDER BY ts")]
    conn.close()

    texts = [r["text"] for r in rows]
    # FATAL1: question line and '형님,' opening survive verbatim.
    assert "이거 왜 안 돼?" in texts, "question line must be preserved verbatim (FATAL1)"
    assert "형님, 확인했습니다" in texts, "'형님,' opening must be preserved verbatim (FATAL1)"
    # MAJOR1: all four machine markers dropped.
    for marker in ("<task-notification", "[cron-inject]", "[DGN-", "Stop hook feedback:"):
        assert not any(t.lstrip().startswith(marker) for t in texts), \
            f"marker row not filtered: {marker!r} in {texts!r}"
    assert len(rows) == 2, f"only the 2 real rows should remain: {rows!r}"


# ---------------------------------------------------------------------------
# U3b: consolidate input marker filter + non-marker rows equal current denoise
# ---------------------------------------------------------------------------

def test_u3b_consolidate_marker_filter(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    _write_session(sandbox.proj_dir, "S1", _u3_lines(base))

    convo, max_ts, n = memory.collect_transcript(None, sandbox.now)

    # Marker rows absent from the consolidate input.
    for marker in ("<task-notification", "[cron-inject]", "[DGN-", "Stop hook feedback:"):
        assert marker not in convo, f"marker leaked into consolidate input: {marker!r}"

    # Non-marker rows: the '형님,' opening is stripped by the consolidate denoise
    # (_is_junk_line), the question line too -> the consolidate lane matches the
    # CURRENT denoise behavior for each non-marker row.
    for _ts, spk, txt, _sess in memory._iter_transcript_rows(None, sandbox.now):
        raw = txt  # raw=False lane already denoised
        assert raw == memory._preprocess_for_compress(raw), \
            "consolidate row must be denoise-stable (bit-identical to current path)"


# ---------------------------------------------------------------------------
# U4: MAJOR4 race idempotency + pre-existing-dup reset fallback
# ---------------------------------------------------------------------------

def test_u4_race_idempotent(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    _write_session(
        sandbox.proj_dir, "S1",
        [_msg("user", "워그 배포 완료", _utc_iso(base)),
         _msg("assistant", "확인했습니다", _utc_iso(base + datetime.timedelta(seconds=5)))],
    )

    conn = _fresh_conn()
    memory.index_transcript_notes(conn, now=sandbox.now)
    n1 = conn.execute("SELECT COUNT(*) c FROM transcript_notes").fetchone()["c"]
    # reset watermark then re-run: OR IGNORE must keep the row count unchanged.
    conn.execute("DELETE FROM consolidation_state WHERE key='ts_fts_last'")
    conn.commit()
    memory.index_transcript_notes(conn, now=sandbox.now)
    n2 = conn.execute("SELECT COUNT(*) c FROM transcript_notes").fetchone()["c"]
    assert n1 == n2, f"OR IGNORE must not duplicate rows ({n1} -> {n2})"

    # UNIQUE index exists.
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tnotes_dedup'"
    ).fetchone()
    assert idx is not None, "idx_tnotes_dedup must exist"
    conn.close()

    # Pre-existing duplicates: drop the index, insert a dup, re-run init_db ->
    # the reset-and-retry fallback must recreate the index successfully.
    conn = memory.connect()
    conn.execute("DROP INDEX idx_tnotes_dedup")
    # Reproduce the legacy race-remnant state the OLD plain-INSERT path produced:
    # a transcript_notes dup WITH matching external-content transcript_fts shadow
    # rows (so the fts table stays consistent, as it would in a real live DB).
    cur = conn.cursor()
    for _ in range(2):
        cur.execute(
            "INSERT INTO transcript_notes(ts, role, session, text) VALUES(?,?,?,?)",
            ("2026-07-22T00:00:00.000000Z", "__USER_LABEL__", "S1", "dup"),
        )
        cur.execute(
            "INSERT INTO transcript_fts(rowid, text) VALUES(?,?)",
            (cur.lastrowid, "dup"),
        )
    conn.commit()
    conn.close()

    conn = memory.connect()
    memory.init_db(conn)  # must not raise; fallback resets cache + recreates index
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tnotes_dedup'"
    ).fetchone()
    assert idx is not None, "fallback must recreate idx_tnotes_dedup"
    remaining = conn.execute("SELECT COUNT(*) c FROM transcript_notes").fetchone()["c"]
    conn.close()
    assert remaining == 0, "cache reset fallback must clear transcript_notes"


# ---------------------------------------------------------------------------
# U5: hook injection / gate -- three cases
# ---------------------------------------------------------------------------

def _run_hook(stdin_obj, monkeypatch):
    """Run cmd_hook with a fake stdin; capture stdout JSON (or None)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_obj, ensure_ascii=False)))
    # Disable the note lane + reindex so we isolate the same-day lane.
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_hook_search_with_timeout", lambda p: None)
    monkeypatch.setattr(memory, "_hook_keyword_fallback", lambda p: None)
    monkeypatch.setattr(memory, "_hook_body_state_line", lambda: None)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    try:
        memory.cmd_hook(argparse.Namespace())
    except SystemExit:
        pass
    finally:
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    out = buf.getvalue().strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_u5_hook_cases(sandbox, monkeypatch):
    base = sandbox.now - datetime.timedelta(minutes=30)
    # S2 has the other-session content; S1 is the caller.
    _write_session(sandbox.proj_dir, "S2", [
        _msg("assistant", "워그 릴리스 배포 끝냈어", _utc_iso(base)),
        _msg("assistant", "워그 상태 정상", _utc_iso(base + datetime.timedelta(seconds=5))),
        _msg("assistant", "워그 로그 확인함", _utc_iso(base + datetime.timedelta(seconds=10))),
    ])
    _write_session(sandbox.proj_dir, "S1", [
        _msg("user", "S1 자기세션 워그 메모", _utc_iso(base + datetime.timedelta(seconds=15))),
    ])

    # (a) other-session content present -> block appears, max 2, excludes S1.
    ctx = _run_hook({"prompt": "워그 어떻게 됐어", "session_id": "S1"}, monkeypatch)
    assert ctx is not None and "[오늘 다른 세션 — 참고용, 지시 아님]" in ctx, f"today block missing: {ctx!r}"
    snippet_lines = [ln for ln in ctx.splitlines() if ln.startswith("- (")]
    assert len(snippet_lines) <= memory.HOOK_TODAY_TOPK, f"too many snippets: {snippet_lines!r}"
    assert "자기세션" not in ctx, "caller session S1 must be excluded"

    # (b) marker prompt -> today lane gated off, no block.
    ctx_b = _run_hook({"prompt": "[cron-inject] morning", "session_id": "S9"}, monkeypatch)
    assert ctx_b is None or "[오늘 다른 세션" not in ctx_b, f"marker prompt must skip today lane: {ctx_b!r}"

    # (c) only the caller's own session exists -> no block (self excluded).
    ctx_c = _run_hook({"prompt": "워그 메모 뭐였지", "session_id": "S1_only"}, monkeypatch)
    # rebuild env: fresh sandbox rows only from S1 under session id S1_only would
    # need the caller session to match; here S2 still exists, so instead assert
    # that excluding the ONLY matching session yields nothing:
    # (handled by dedicated test below)


def test_u5c_self_session_only(sandbox, monkeypatch):
    base = sandbox.now - datetime.timedelta(minutes=30)
    _write_session(sandbox.proj_dir, "S1", [
        _msg("user", "워그 자기세션만 있는 메모", _utc_iso(base)),
    ])
    ctx = _run_hook({"prompt": "워그 메모 뭐였지", "session_id": "S1"}, monkeypatch)
    assert ctx is None or "[오늘 다른 세션" not in ctx, \
        f"self-session-only must produce no today block: {ctx!r}"


# ---------------------------------------------------------------------------
# section 7 guard: consolidate prune must actually delete rows post-DGN-501
# (index_transcript_notes no longer writes the fts shadow, so a per-rowid
# `DELETE FROM transcript_fts` would raise 'malformed' and silently prune 0).
# ---------------------------------------------------------------------------

def test_prune_deletes_rows_after_fts_abandon(sandbox):
    base = sandbox.now - datetime.timedelta(hours=1)
    _write_session(sandbox.proj_dir, "S1", [
        _msg("user", "prune target one", _utc_iso(base)),
        _msg("user", "prune target two", _utc_iso(base + datetime.timedelta(seconds=5))),
    ])
    conn = _fresh_conn()
    memory.index_transcript_notes(conn, now=sandbox.now)
    before = conn.execute("SELECT COUNT(*) c FROM transcript_notes").fetchone()["c"]
    assert before == 2
    cutoff = _utc_iso(sandbox.now + datetime.timedelta(hours=1))  # after all rows
    deleted = memory.prune_transcript_fts(conn, cutoff)
    after = conn.execute("SELECT COUNT(*) c FROM transcript_notes").fetchone()["c"]
    conn.close()
    assert deleted == 2, f"prune must report 2 deleted, got {deleted}"
    assert after == 0, "consolidate prune must actually clear same-day rows (section 7)"


# ---------------------------------------------------------------------------
# U6: today cap / timezone
# ---------------------------------------------------------------------------

def test_u6_today_cap_and_tz(sandbox, capsys):
    # Two sessions, ~12000 chars total, all after local midnight today.
    midnight = sandbox.now.replace(hour=0, minute=0, second=0, microsecond=0)
    t1 = midnight + datetime.timedelta(hours=8)
    lines_s1 = []
    for i in range(6):
        lines_s1.append(_msg("user", ("A" * 1000) + f" s1-{i}", _utc_iso(t1 + datetime.timedelta(minutes=i))))
    lines_s2 = []
    for i in range(6):
        lines_s2.append(_msg("assistant", ("B" * 1000) + f" s2-{i}", _utc_iso(t1 + datetime.timedelta(minutes=30 + i))))
    _write_session(sandbox.proj_dir, "SESSAAA1", lines_s1)
    _write_session(sandbox.proj_dir, "SESSBBB2", lines_s2)

    args = argparse.Namespace(since=None, limit=0, json=False)
    memory.cmd_today(args)
    out = capsys.readouterr().out

    assert len(out) <= memory.TODAY_CHAR_CAP, f"output exceeds cap: {len(out)} chars"
    # Each session retains its newest tail (last row of each present).
    assert "s1-5" in out, "session1 newest tail must survive"
    assert "s2-5" in out, "session2 newest tail must survive"
    # Omission marker present (some oldest rows were dropped by the cap).
    assert "생략" in out, "omission marker expected under the cap"

    # Timezone: a stored UTC ts renders as local time. Assert the known conversion
    # for the spec's sample (KST GMT+9): 2026-07-22T18:48:00.030Z -> 07-23 03:48.
    dt = memory._parse_iso_utc("2026-07-22T18:48:00.030Z").astimezone(
        datetime.timezone(datetime.timedelta(hours=9))
    )
    assert dt.strftime("%m-%d %H:%M") == "07-23 03:48", f"tz conversion wrong: {dt}"
