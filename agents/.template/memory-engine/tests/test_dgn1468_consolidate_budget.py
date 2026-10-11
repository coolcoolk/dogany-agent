# DGN-1468: the nightly consolidate is killed at a hard wall-clock limit by the
# queue runner, and a killed run used to commit NOTHING -- the watermark stayed
# put, so the next night re-collected the same span plus one more day. Each miss
# made the next run strictly more expensive: a ratchet with no self-healing.
# Measured on a dev instance 2026-09-15 (4 consecutive nights killed at 1500s): span had
# grown to 2135 messages / 1.34 MB / 56 chunks, and first-pass Sonnet compression
# alone projects to 56 x 33.2s = 1857s -- over the limit before any other stage runs.
#
# This suite pins the fix and, just as importantly, pins that a run which does
# NOT catch up can never read as a clean night (the DGN-1154 silent-green
# disease this ticket is a case of).
#
# Fully sandboxed: temp state.db + temp memories dir; transcript, compressor,
# filter, embedding, index and push are all stubbed; the clock is fake. Nothing
# under ~/.dogany or the live engine state is read or written (DGN-1474).
#
# Run: python3 -m pytest memory-engine/tests/test_dgn1468_consolidate_budget.py -v

import argparse
import contextlib
import datetime
import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402


# Measured on a dev instance 2026-09-15: 33.2s mean per 24k-char chunk (samples 35.6 /
# 35.5 / 28.4). The fake clock uses the same number so the budget arithmetic in
# the test matches the arithmetic the real run does.
SECS_PER_CHUNK = 33.2
ROW_CHARS = 8000  # 3 rows per 24000-char chunk


class FakeClock:
    """Monotonic clock the test advances explicitly (no sleeping)."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def make_rows(n_rows, start_min=0):
    """n_rows synthetic messages, ROW_CHARS each, ascending timestamps."""
    rows = []
    for i in range(n_rows):
        idx = start_min + i
        ts = "2026-09-%02dT%02d:%02d:00.000000Z" % (11 + idx // 1440, (idx // 60) % 24, idx % 60)
        rows.append((ts, "user", "msg%04d " % idx + "x" * (ROW_CHARS - 10), "sess"))
    return rows


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
    monkeypatch.setattr(memory, "CONSOLIDATE_LAST_SUCCESS", str(tmp_path / ".last-success"))
    monkeypatch.setattr(memory, "CONSOLIDATE_DEFER_MARKER", str(tmp_path / ".defer.json"))
    monkeypatch.setattr(memory, "CONSOLIDATE_BUDGET_BOOST_FILE",
                        str(tmp_path / ".budget-boost.json"), raising=False)
    monkeypatch.delenv("DOGANY_USAGE_DEFER_MARKER", raising=False)
    monkeypatch.delenv("DOGANY_BACKLOG_MARKER", raising=False)
    monkeypatch.setattr(memory, "_query_usage_state", lambda: {
        "five_hour_util": 10.0, "seven_day_util": 10.0,
        "five_hour_resets_at": "", "seven_day_resets_at": "",
    })
    monkeypatch.setattr(memory, "_second_stage_filter", lambda c: (list(c), [], False))
    monkeypatch.setattr(memory, "_rule_filter_candidates", lambda c: (list(c), []))
    monkeypatch.setattr(memory, "embed", _no_ollama)
    monkeypatch.setattr(memory, "cmd_index", lambda a: None)
    monkeypatch.setattr(memory, "_send_silent_report", lambda rep: False)
    # DGN-1873 isolation: the reindex lock defaults to a file beside memory.py
    # (shared by every suite that runs this module concurrently in the release
    # sweep); a test must never contend on it.
    monkeypatch.setattr(memory, "REINDEX_LOCK_PATH", str(tmp_path / ".reindex.lock"))

    clock = FakeClock()
    monkeypatch.setattr(memory, "time", clock)

    def fake_compress(text, prompt_prefix=None, model=None):
        clock.advance(SECS_PER_CHUNK)
        return ["fact from " + text.split(": ", 1)[-1].split()[0]]

    monkeypatch.setattr(memory, "compress_with_haiku", fake_compress)
    return tmp_path


def _no_ollama(text):
    raise OSError("no ollama in test sandbox")


def run(monkeypatch, rows, budget=None, archive_calls=None):
    """Run cmd_consolidate over `rows`; return (rc, stdout)."""
    if archive_calls is None:
        monkeypatch.setattr(memory, "archive_raw_transcript",
                            lambda wm, now, until_iso=None: 0)
    else:
        def _arch(wm, now, until_iso=None):
            kept = [r for r in rows if not until_iso or r[0] <= until_iso]
            archive_calls.append((wm, until_iso, len(kept)))
            return len(kept)
        monkeypatch.setattr(memory, "archive_raw_transcript", _arch)
    monkeypatch.setattr(memory, "_iter_transcript_rows",
                        lambda wm, now, raw=False: [r for r in rows if not wm or r[0] > wm])
    args = argparse.Namespace(dry_run=False, no_push=True, since_days=None, budget_secs=budget)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.cmd_consolidate(args)
    out = buf.getvalue()
    sys.stdout.write(out)
    return rc, out


def state(tmp_path, key):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    row = conn.execute("SELECT value FROM consolidation_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# row-aligned chunking -- the precondition for committing a partial watermark
# ---------------------------------------------------------------------------

def test_chunks_never_split_a_message():
    # A message containing newlines would be split mid-message by the text-level
    # _chunk_convo; committing a watermark at such a boundary would drop the
    # message's tail forever.
    rows = [
        ("2026-09-11T00:00:00.000000Z", "user", "line one\nline two\nline three", "s"),
        ("2026-09-11T00:01:00.000000Z", "user", "y" * 30, "s"),
    ]
    chunks = memory._chunk_rows(rows, max_chars=60)
    joined = "\n".join(c[0] for c in chunks)
    assert "line one\nline two\nline three" in joined, "message body must stay intact"
    for _text, ts, _n in chunks:
        assert ts in (r[0] for r in rows), "every chunk boundary is a real message ts"


def test_chunk_rows_covers_every_row_exactly_once():
    rows = make_rows(20)
    chunks = memory._chunk_rows(rows)
    assert sum(c[2] for c in chunks) == len(rows)
    joined = "\n".join(c[0] for c in chunks)
    for _ts, _spk, txt, _s in rows:
        assert txt.split()[0] in joined
    assert [c[1] for c in chunks] == sorted(c[1] for c in chunks), "ts boundaries ascend"


# ---------------------------------------------------------------------------
# the fix: a budget-bounded run still moves the watermark forward
# ---------------------------------------------------------------------------

def test_budget_stop_commits_the_work_it_did(sandbox, monkeypatch):
    tmp_path = sandbox
    rows = make_rows(30)                     # 10 chunks @ 33.2s = 332s of work
    rc, out = run(monkeypatch, rows, budget=150)   # room for ~4 chunks

    assert rc == 0, "a deliberate budget stop is forward progress, not a failure"
    assert "BUDGET STOP" in out
    assert "PARTIAL" in out
    wm = state(tmp_path, "last_ts")
    assert wm is not None, "the whole point: a bounded run must NOT commit nothing"
    assert wm in [r[0] for r in rows], "watermark lands on a real message boundary"
    assert wm < rows[-1][0], "and short of the span end (the run really did stop early)"
    assert int(state(tmp_path, "backlog_rows")) > 0
    assert int(state(tmp_path, "backlog_runs")) == 1


def test_backlog_shrinks_every_run_until_it_drains(sandbox, monkeypatch):
    # The ratchet, reversed: each run consumes a prefix, so the next run's span
    # is strictly smaller. Four nights of the old behavior grew the span; here it
    # must converge.
    tmp_path = sandbox
    rows = make_rows(30)
    seen = []
    for _ in range(6):
        rc, out = run(monkeypatch, rows, budget=150)
        assert rc in (0, 1)
        left = int(state(tmp_path, "backlog_rows") or 0)
        seen.append(left)
        if left == 0:
            break
    assert seen[-1] == 0, "backlog must reach zero: %r" % (seen,)
    assert all(b < a for a, b in zip(seen, seen[1:])), \
        "backlog must strictly shrink every run: %r" % (seen,)
    assert state(tmp_path, "last_ts") == rows[-1][0], "fully caught up at the end"
    assert int(state(tmp_path, "backlog_runs")) == 0, "counters reset once drained"


def test_unbounded_budget_processes_the_whole_span(sandbox, monkeypatch):
    # No-regression belt: budget <= 0 keeps the pre-DGN-1468 behavior (one run,
    # whole span, no backlog bookkeeping).
    tmp_path = sandbox
    rows = make_rows(30)
    rc, out = run(monkeypatch, rows, budget=0)
    assert rc == 0
    assert "BUDGET STOP" not in out
    assert "PARTIAL" not in out
    assert state(tmp_path, "last_ts") == rows[-1][0]
    assert (state(tmp_path, "backlog_rows") or "0") == "0"
    assert os.path.isfile(str(tmp_path / ".last-success")), "clean run stamps last-success"


def test_at_least_one_chunk_runs_even_on_an_absurd_budget(sandbox, monkeypatch):
    # A budget smaller than a single chunk must still make progress -- otherwise
    # the ratchet comes straight back as "0 chunks per night, forever".
    tmp_path = sandbox
    rows = make_rows(30)
    rc, _ = run(monkeypatch, rows, budget=1)
    assert rc == 0
    assert state(tmp_path, "last_ts") is not None


def test_partial_run_archives_only_the_consumed_prefix(sandbox, monkeypatch):
    # The raw archive is append-only; archiving the whole span on a partial run
    # would write the un-consumed tail twice (again when the next run consumes it).
    tmp_path = sandbox
    rows = make_rows(30)
    calls = []
    run(monkeypatch, rows, budget=150, archive_calls=calls)
    assert len(calls) == 1
    _wm, until, n = calls[0]
    assert until == state(tmp_path, "last_ts")
    assert n < len(rows), "archive bounded by the committed watermark"


# ---------------------------------------------------------------------------
# alert reachability: a run that did not catch up must not read as a clean night
# ---------------------------------------------------------------------------

def test_a_backlog_night_is_never_a_silent_green(sandbox, monkeypatch):
    tmp_path = sandbox
    rows = make_rows(30)
    _rc, out = run(monkeypatch, rows, budget=150)
    assert "PARTIAL" in out and "left for the next run" in out

    # and the one-line consumer (the pushed report) names it too -- the 0-new-items
    # "stay silent" shortcut must not swallow a run that is behind.
    rep = memory._build_consolidate_report([], 0, False, 10, backlog_rows=0)
    assert rep is None, "a genuinely quiet night still stays silent"
    rep = memory._build_consolidate_report([], 0, False, 10, backlog_rows=77)
    assert rep is not None, "a night with a backlog must not be silent"
    assert "77" in rep and "Not caught up" in rep


def test_stuck_backlog_is_counted_as_a_failure(sandbox, monkeypatch):
    # Draining is fine. Draining that never finishes is the silent green this
    # ticket is about: after CONSOLIDATE_BACKLOG_STUCK_RUNS consecutive backlog
    # runs, consolidate must exit non-zero so the queue runner counts it.
    tmp_path = sandbox
    monkeypatch.setattr(memory, "CONSOLIDATE_BACKLOG_STUCK_RUNS", 3)
    rows = make_rows(400)          # far more than any single budget can drain
    stamp = str(tmp_path / ".last-success")
    codes = []
    for _i in range(3):
        rc, _ = run(monkeypatch, rows, budget=150)
        codes.append(rc)
        # DGN-1468 grill [5]: the stamp means "fully caught up" -- NO backlog
        # night may refresh it, progress or stuck alike.
        assert not os.path.isfile(stamp), \
            "a backlog night must not stamp last-success (the staleness reader must stay red)"
    assert codes[:2] == [0, 0], "the first draining nights are progress, not failure"
    assert codes[2] == 1, "a stuck backlog must be COUNTED, not read as a clean night"


def test_stuck_backlog_under_cron_guard_exits_behind_not_failed(sandbox, monkeypatch):
    # DGN-1582 (1): under cron-guard (DOGANY_BACKLOG_MARKER exported) the stuck
    # night is a designed outcome -- the run committed its work inside its
    # budget. It exits 77 with a marker so cron-guard sends a "behind" notice
    # instead of the generic failure alarm; the queue counts it as behind=.
    tmp_path = sandbox
    monkeypatch.setattr(memory, "CONSOLIDATE_BACKLOG_STUCK_RUNS", 3)
    marker = tmp_path / "backlog" / "com.test.dmq-consolidate.json"
    monkeypatch.setenv("DOGANY_BACKLOG_MARKER", str(marker))
    rows = make_rows(400)
    codes = []
    for _i in range(3):
        rc, _ = run(monkeypatch, rows, budget=150)
        codes.append(rc)
        if _i < 2:
            assert not marker.exists(), "draining nights below the threshold write no marker"
    assert codes == [0, 0, memory.CONSOLIDATE_RC_BACKLOG_BEHIND] == [0, 0, 77]
    d = json.loads(marker.read_text())
    assert d["runs"] == 3 and d["rows_left"] > 0
    assert 0 < d["chunks_done"] < d["chunks_total"]
    assert d["at"].endswith("Z")
    assert not os.path.isfile(str(tmp_path / ".last-success")), \
        "behind is not success -- the stamp must stay stale"


def test_stuck_backlog_marker_write_failure_stays_a_failure(sandbox, monkeypatch):
    # 2-factor: no marker landed -> rc 1, the visible failure path (never 77
    # without the evidence cron-guard needs to word the notice).
    tmp_path = sandbox
    monkeypatch.setattr(memory, "CONSOLIDATE_BACKLOG_STUCK_RUNS", 1)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("DOGANY_BACKLOG_MARKER", str(blocker / "m.json"))
    rc, _ = run(monkeypatch, make_rows(400), budget=150)
    assert rc == 1


def test_behind_night_2_raises_next_budget_bounded(sandbox, monkeypatch):
    # DGN-1582 r3 (dec-236): behind night 1 changes nothing; from behind
    # night 2 the next night's budget goes up one step, never past the cap,
    # and the raise is recorded (boost file + marker) for the queue runner
    # and the operator notice.
    tmp_path = sandbox
    monkeypatch.setattr(memory, "CONSOLIDATE_BACKLOG_STUCK_RUNS", 1)
    monkeypatch.setattr(memory, "CONSOLIDATE_BUDGET_STEP_SECS", 100, raising=False)
    monkeypatch.setattr(memory, "CONSOLIDATE_BUDGET_CAP_SECS", 300, raising=False)
    marker = tmp_path / "backlog" / "m.json"
    boost = tmp_path / ".budget-boost.json"
    monkeypatch.setenv("DOGANY_BACKLOG_MARKER", str(marker))
    rows = make_rows(400)

    rc, _ = run(monkeypatch, rows, budget=150)          # behind night 1
    assert rc == 77
    d = json.loads(marker.read_text())
    assert d["behind_night"] == 1 and d["budget_raised"] is False
    assert not boost.exists(), "behind night 1 must not touch the budget"

    rc, out = run(monkeypatch, rows, budget=150)        # behind night 2
    assert rc == 77
    d = json.loads(marker.read_text())
    assert d["behind_night"] == 2 and d["budget_raised"] is True
    assert d["budget_next_secs"] == 250
    b = json.loads(boost.read_text())
    assert b["budget_secs"] == 250 and b["from"] == 150 and b["rows_left"] > 0
    assert "budget raised 150s -> 250s" in out

    rc, _ = run(monkeypatch, rows, budget=250)          # night 3: up to the cap
    assert json.loads(boost.read_text())["budget_secs"] == 300
    rc, out = run(monkeypatch, rows, budget=300)        # night 4: at the cap
    d = json.loads(marker.read_text())
    assert d["budget_raised"] is False and d["budget_next_secs"] == 300
    assert json.loads(boost.read_text())["budget_secs"] == 300, "never past the cap"
    assert "already at the cap" in out


def test_drained_night_clears_budget_boost(sandbox, monkeypatch):
    tmp_path = sandbox
    boost = tmp_path / ".budget-boost.json"
    boost.write_text(json.dumps({"budget_secs": 1500}))
    rc, out = run(monkeypatch, make_rows(9), budget=0)  # catches up fully
    assert rc == 0
    assert not boost.exists(), "a caught-up night returns to the default budget"


def test_stuck_streak_resets_once_caught_up(sandbox, monkeypatch):
    tmp_path = sandbox
    rows = make_rows(9)
    rc, _ = run(monkeypatch, rows, budget=100)     # ~2 chunks of 3 -> partial
    assert rc == 0 and int(state(tmp_path, "backlog_runs")) == 1
    rc, out = run(monkeypatch, rows, budget=0)     # catch up fully
    assert rc == 0
    assert "backlog drained" in out
    assert int(state(tmp_path, "backlog_runs")) == 0


# ---------------------------------------------------------------------------
# grill [1]: a timestamp tie must never straddle a committed chunk boundary.
# Collection is strict (`ts > watermark`), so a boundary inside a tie makes the
# tie's tail rows uncollectable on every following night -- silent, permanent
# loss that all three anti-silence layers miss (both nights exit 0, backlog 0).
# Verifier geometry reproduced exactly: 2 rows per chunk, millisecond-identical
# timestamps on the boundary pair.
# ---------------------------------------------------------------------------

TIE_ROW_CHARS = 11000  # 2 rows per 24000-char chunk


def _tie_rows():
    t0 = "2026-09-11T00:00:00.000000Z"
    t1 = "2026-09-11T00:01:00.123000Z"   # the tie ts (millisecond-identical pair)
    t2 = "2026-09-11T00:02:00.000000Z"
    pad = "x" * (TIE_ROW_CHARS - 10)
    return [
        (t0, "user", "msg0000 " + pad, "s"),
        (t1, "user", "msg0001 " + pad, "s"),
        (t1, "user", "msg0002 " + pad, "s"),  # tie tail: the row the bug lost
        (t2, "user", "msg0003 " + pad, "s"),
    ]


def test_chunk_boundary_never_splits_a_timestamp_tie():
    rows = _tie_rows()
    chunks = memory._chunk_rows(rows)
    assert sum(c[2] for c in chunks) == len(rows), "every row still chunked exactly once"
    # The tie pair must land in ONE chunk, even though that overflows max_chars.
    tie_chunks = [c[0] for c in chunks if "msg0001" in c[0] or "msg0002" in c[0]]
    assert len(tie_chunks) == 1 and "msg0001" in tie_chunks[0] and "msg0002" in tie_chunks[0], \
        "equal-ts rows must never be split across a chunk boundary"
    # Which makes every boundary ts STRICTLY increasing -- the invariant that
    # lets a partial commit at any boundary stay loss-free under `ts > watermark`.
    boundary_ts = [c[1] for c in chunks]
    assert all(b > a for a, b in zip(boundary_ts, boundary_ts[1:])), \
        "chunk boundary timestamps must strictly increase: %r" % (boundary_ts,)


def test_tie_ts_at_budget_boundary_is_never_lost(sandbox, monkeypatch):
    tmp_path = sandbox
    rows = _tie_rows()
    clock = memory.time  # the FakeClock the sandbox installed
    attempted = []

    def recording_compress(text, prompt_prefix=None, model=None):
        clock.advance(SECS_PER_CHUNK)
        attempted.append(text)
        return ["fact from " + text.split(": ", 1)[-1].split()[0]]

    monkeypatch.setattr(memory, "compress_with_haiku", recording_compress)

    # Night 1: budget only fits the first chunk -> partial stop AT the tie.
    rc, out = run(monkeypatch, rows, budget=40)
    assert rc == 0
    assert "BUDGET STOP" in out
    wm = state(tmp_path, "last_ts")
    assert wm is not None
    # THE invariant: the watermark never passes a row that was not attempted.
    blob = "\n".join(attempted)
    for ts, _spk, txt, _s in rows:
        if ts <= wm:
            token = txt.split()[0]
            assert token in blob, \
                "%s committed under the watermark but never attempted" % token

    # Night 2: unlimited catch-up. Every message must have been attempted by
    # SOME night -- the old code silently never collected the tie tail again.
    rc, _ = run(monkeypatch, rows, budget=0)
    assert rc == 0
    blob = "\n".join(attempted)
    for _ts, _spk, txt, _s in rows:
        token = txt.split()[0]
        assert token in blob, \
            "%s was never compressed on any night (silent permanent loss)" % token
    assert state(tmp_path, "last_ts") == rows[-1][0], "fully caught up"
    assert int(state(tmp_path, "backlog_rows") or 0) == 0


# ---------------------------------------------------------------------------
# grill [2]: the retry queue must drain inside the SAME run budget. An
# unbounded 3c drain broke "the run always ends inside its budget" (repro: 20
# queued chunks blew a 150s budget to 797s -> killed again at the hard limit).
# ---------------------------------------------------------------------------

def _seed_retry_queue(n_chunks, rows_per_chunk):
    conn = memory.connect()
    memory.init_db(conn)
    now = datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc)
    for i in range(n_chunks):
        text = "\n".join(
            "user: retryq%03d-%d payload" % (i, j) for j in range(rows_per_chunk)
        )
        memory._retryq_add(conn, text, "seeded failure", now)
    conn.close()


def test_retry_queue_drains_inside_the_run_budget(sandbox, monkeypatch):
    tmp_path = sandbox
    _seed_retry_queue(20, rows_per_chunk=3)
    rows = make_rows(30)
    clock = memory.time
    t_start = clock.monotonic()

    rc, out = run(monkeypatch, rows, budget=150)
    wall = clock.monotonic() - t_start

    assert rc == 0
    assert wall <= 150, "run must end inside its budget, took %.0fs" % wall
    assert "BUDGET STOP in retry queue" in out
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    n_q = conn.execute("SELECT COUNT(*) FROM consolidate_retry_queue").fetchone()[0]
    conn.close()
    assert n_q == 20, "budget-deferred retry chunks stay queued untouched"
    backlog = int(state(tmp_path, "backlog_rows"))
    assert backlog >= 20 * 3, \
        "deferred retry rows must be counted into the backlog: %d" % backlog
    assert not os.path.isfile(str(tmp_path / ".last-success")), \
        "a night that left work behind must not stamp last-success"


def test_retry_only_run_makes_progress_then_defers(sandbox, monkeypatch):
    # A retry-only run has no duration sample yet: the FIRST retry must still
    # run (forward progress, same rule as the first-pass loop), then the gate
    # defers the rest and counts them as backlog.
    tmp_path = sandbox
    _seed_retry_queue(5, rows_per_chunk=2)
    clock = memory.time
    t_start = clock.monotonic()

    rc, out = run(monkeypatch, [], budget=40)
    wall = clock.monotonic() - t_start

    assert rc == 0
    assert "1 chunk(s) drained" in out, "the first retry must run even with no sample"
    assert "BUDGET STOP in retry queue" in out
    assert wall <= 40, "took %.0fs" % wall
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    n_q = conn.execute("SELECT COUNT(*) FROM consolidate_retry_queue").fetchone()[0]
    conn.close()
    assert n_q == 4
    assert int(state(tmp_path, "backlog_rows")) == 4 * 2
    assert state(tmp_path, "last_ts") is None, "retry-only run never touches the watermark"


# ---------------------------------------------------------------------------
# grill [5]: a backlog night whose push candidates all die in the filters must
# STILL push the one-line report and must NOT stamp last-success. Repro was:
# backlog_rows=22, push 0, rc=0, last-success stamped -- a fully silent
# behind-night.
# ---------------------------------------------------------------------------

def run_with_push(monkeypatch, rows, budget, sent):
    monkeypatch.setattr(memory, "archive_raw_transcript",
                        lambda wm, now, until_iso=None: 0)
    monkeypatch.setattr(memory, "_iter_transcript_rows",
                        lambda wm, now, raw=False: [r for r in rows if not wm or r[0] > wm])
    monkeypatch.setattr(memory, "_send_silent_report",
                        lambda rep: bool(sent.append(rep)) or True)
    args = argparse.Namespace(dry_run=False, no_push=False, since_days=None,
                              budget_secs=budget)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memory.cmd_consolidate(args)
    return rc, buf.getvalue()


def test_backlog_night_with_zero_push_candidates_still_cries(sandbox, monkeypatch):
    tmp_path = sandbox
    rows = make_rows(30)
    # every candidate dies in the second-stage filter -> 0 push candidates
    monkeypatch.setattr(memory, "_second_stage_filter",
                        lambda c: ([], [(it, "test-drop") for it in c], False))
    sent = []
    rc, out = run_with_push(monkeypatch, rows, 150, sent)

    assert rc == 0
    assert int(state(tmp_path, "backlog_rows")) > 0
    assert sent, "a backlog night must push its report even with 0 candidates"
    assert "Not caught up" in sent[0]
    assert not os.path.isfile(str(tmp_path / ".last-success")), \
        "a backlog night must not stamp last-success"


def test_caught_up_quiet_night_stays_silent_and_stamps(sandbox, monkeypatch):
    # Counter-belt for [5]: a genuinely caught-up night with nothing worth
    # pushing keeps the old contract -- no report noise, last-success stamped.
    tmp_path = sandbox
    rows = make_rows(3)                     # one chunk, fully processed
    monkeypatch.setattr(memory, "_second_stage_filter",
                        lambda c: ([], [(it, "test-drop") for it in c], False))
    sent = []
    rc, _ = run_with_push(monkeypatch, rows, 0, sent)

    assert rc == 0
    assert sent == [], "a quiet caught-up night must stay silent"
    assert os.path.isfile(str(tmp_path / ".last-success"))
    assert int(state(tmp_path, "backlog_rows") or 0) == 0


# ---------------------------------------------------------------------------
# grill [6]: a misconfigured env var must degrade the one knob (default +
# warning), never kill the import -- memory.py is imported by EVERY entry
# point, search hook included.
# ---------------------------------------------------------------------------

def test_env_misconfig_never_kills_import():
    env = dict(os.environ)
    env["DOGANY_CONSOLIDATE_BUDGET_SECS"] = "abc"
    env["DOGANY_CONSOLIDATE_DOWNSTREAM_SECS"] = "half a second"
    env["CLASSIFY_CHUNK_SIZE"] = "many"
    code = (
        "import memory; "
        "print(memory.CONSOLIDATE_BUDGET_SECS, "
        "memory.CONSOLIDATE_DOWNSTREAM_SECS_PER_CAND, memory.CLASSIFY_CHUNK_SIZE)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, \
        "misconfigured env must not kill import:\n%s" % proc.stderr
    assert proc.stdout.split() == ["1200.0", "0.75", "100"], proc.stdout
    assert "[warn]" in proc.stderr, "the fallback must warn, not stay silent"
