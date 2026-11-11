# test-lane: fast
# test-component: memory hook session admission
# test-tier: 0
# test-prereq: tmp-files, pytest
# test-new-component: first persistent session admission boundary; existing recall suites test retrieval and curation, not emission claims
# test-covers: agents/.template/memory-engine/memory.py agents/.template/memory-engine/tests/fixtures/dgn2173/synthetic_recall.json
"""Hook admission across process restarts, sessions, and existing speech."""
import argparse
import concurrent.futures
import datetime
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memory


def recall(*texts):
    return "\n".join(memory._recall_header("test") + ["- [facts] " + t for t in texts])


def apply(text, session="one", prompt="orchid", **kw):
    return memory._hook_filter_injection(recall(text), None, prompt, session, **kw)[0]


def test_persistent_session_dedupe_and_isolation():
    assert apply("orchid blooms in spring")
    # Each call opens a new sqlite connection, as separate hook processes do.
    assert apply("orchid blooms in spring") is None
    assert apply("orchid blooms in spring", session="two")
    assert apply("orchid blooms in winter")
    assert apply("orchid blooms in spring", session=None)
    assert apply("orchid blooms in spring", session=None)
    db = sqlite3.connect(memory.INJECTION_DB_PATH)
    assert db.execute("SELECT count(*) FROM emitted").fetchone()[0] == 3
    assert all("orchid" not in str(r) for r in db.execute("SELECT * FROM emitted"))
    db.close()


def test_concurrent_hooks_claim_item_once():
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sum(bool(x) for x in pool.map(lambda _: apply("orchid blooms"), range(2))) == 1


@pytest.mark.parametrize("marker", ["[bridge:injected-turn]", "[cron-inject]",
    "<task-notification>", "[dispatch-return]", "cron-inject", "dispatch-return"])
def test_machine_turn_does_not_search(marker, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("machine turn searched memory")
    monkeypatch.setattr(memory, "_hook_search_with_timeout", forbidden)
    monkeypatch.setattr(memory, "_hook_keyword_fallback", forbidden)
    monkeypatch.setattr(memory, "_hook_today_recall", forbidden)
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_entity_facts_start", lambda p: None)
    monkeypatch.setattr(memory, "_entity_facts_collect", lambda *a: [])
    monkeypatch.setattr(memory, "_hook_ledger_preload", lambda *a: "structured state")
    monkeypatch.setattr(memory, "_hook_turn_frame", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_usage_context", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": marker + " orchid", "session_id": "m"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(SystemExit):
        memory.cmd_hook(argparse.Namespace())
    assert "structured state" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


def test_floor_and_korean_glued_recall():
    assert apply("unrelated June ticket", prompt="orchid") is None
    assert apply("orchid blooms", prompt="orchid again")
    assert apply("분수 정원 조명은 파란색", prompt="분수정원조명이나다시 알려줘")


def test_already_in_session_and_tool_payload_ignored(tmp_path):
    path = tmp_path / "one.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "orchid blooms in spring"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "orchid blooms in winter"}]}},
    ]))
    assert apply("orchid blooms in spring", transcript_path=str(path)) is None
    assert apply("orchid blooms in winter", transcript_path=str(path))


def test_today_lines_deduped_but_new_answer_retained():
    today = "[today]\n- (user, 10:00) orchid blooms in spring\n- (agent, 10:01) orchid blooms in winter"
    _, first = memory._hook_filter_injection(None, today, "orchid", "s", history="orchidbloomsinspring")
    assert "spring" not in first and "winter" in first
    assert memory._hook_filter_injection(None, today, "orchid", "s", history="orchidbloomsinspring") == (None, None)
    assert memory._hook_filter_injection(None, "[today]\n- (agent, 10:02) orchid blooms in autumn", "orchid", "s")[1]


def test_answer_framing_removed_when_answer_already_present():
    answer = memory.SAME_DAY_HEADER + " -- 10:00]\n(This answer is already recorded.)\n- user asked: orchid?\n- answer (10:00):\norchid blooms in spring"
    assert memory._hook_filter_injection(None, answer, "orchid again", "s", history="orchidbloomsinspring") == (None, None)


def test_unwritable_cache_is_best_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "INJECTION_DB_PATH", str(tmp_path / "absent" / "cache.db"))
    assert apply("orchid blooms")
    assert apply("unrelated ticket") is None


SYNTHETIC_CASES = json.loads((Path(__file__).resolve().parent / "fixtures" /
                             "dgn2173" / "synthetic_recall.json").read_text())["cases"]


SYNTHETIC_NOW = datetime.datetime(2026, 10, 18, 12, tzinfo=datetime.timezone.utc)


class _SyntheticDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(SYNTHETIC_NOW.timestamp(), tz)


class _SyntheticDatetimeModule:
    timedelta = datetime.timedelta
    timezone = datetime.timezone
    datetime = _SyntheticDateTime


@pytest.mark.parametrize("case", SYNTHETIC_CASES, ids=lambda c: str(c["id"]))
def test_synthetic_recall_hook_keeps_established_candidate(case, monkeypatch):
    """Real cmd_hook/render/curation/admission; replay a previously found hit.

    Search candidates are pinned to avoid making this a live embedding test.
    Baseline is the same hook with admission bypassed. The followup must omit
    the repeated fact, and a different session must still receive it.
    """
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_hook_search_with_timeout", lambda p: [
        {"section": "facts", "text": case["fact"], "cosine": 0.8}])
    monkeypatch.setattr(memory, "_entity_facts_start", lambda p: None)
    monkeypatch.setattr(memory, "_entity_facts_collect", lambda *a: [])
    monkeypatch.setattr(memory, "_hook_today_recall", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_same_day_answer", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_ledger_preload", lambda *a: "structured state")
    monkeypatch.setattr(memory, "_hook_turn_frame", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_usage_context", lambda: None)
    monkeypatch.setattr(memory, "_recall_routes_enabled", lambda: False)

    def run(session):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": case["prompt"], "session_id": session})))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        with pytest.raises(SystemExit):
            memory.cmd_hook(argparse.Namespace())
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    admission = memory._hook_filter_injection
    monkeypatch.setattr(memory, "_hook_filter_injection", lambda r, t, *a: (r, t))
    baseline = run("baseline")
    monkeypatch.setattr(memory, "_hook_filter_injection", admission)
    first = run("synthetic")
    # Curation can strip date stamps / add a date anchor, so compare the
    # complete resulting note block rather than assuming raw text rendering.
    assert "[facts]" in baseline
    assert first == baseline
    assert "[facts]" not in run("synthetic")
    assert run("new-synthetic-session") == baseline


def test_new_answer_paragraph_keeps_source_header():
    today = memory.SAME_DAY_HEADER + " -- 10:00]\n(This answer is already recorded.)\n- answer (10:00):\norchid blooms in spring\n\norchid blooms in winter"
    _, ctx = memory._hook_filter_injection(None, today, "orchid", "s", history="orchidbloomsinspring")
    assert ctx.startswith(memory.SAME_DAY_HEADER)
    assert "spring" not in ctx and "winter" in ctx


def test_multiline_note_is_admitted_as_one_item():
    first = "orchid blooms\n  care: water sparingly"
    assert apply(first)
    ctx = recall(first, "orchid grows\n  care: bright light")
    after, _ = memory._hook_filter_injection(ctx, None, "orchid", "one")
    assert "water sparingly" not in after and "bright light" in after


def test_existing_session_hook_context_survives_cache_rebuild(tmp_path):
    path = tmp_path / "one.jsonl"
    path.write_text(json.dumps({"type": "attachment", "attachment": {
        "type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
        "content": [recall("orchid blooms in spring")]}}))
    assert apply("orchid blooms in spring", transcript_path=str(path)) is None
    assert apply("orchid blooms in winter", transcript_path=str(path))


@pytest.mark.parametrize("case", SYNTHETIC_CASES, ids=lambda c: str(c["id"]))
def test_synthetic_same_day_lookup_and_admission(case, tmp_path, monkeypatch):
    """Real SQLite retrieval -> answer rendering -> admission, no pinned hits."""
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(tmp_path / "absent" / "*.jsonl"))
    monkeypatch.setattr(memory, "datetime", _SyntheticDatetimeModule)
    conn = memory.connect()
    memory.init_db(conn)
    conn.executemany("INSERT INTO transcript_notes(ts, role, session, text) VALUES (?, ?, ?, ?)", [
        ("2026-10-18T03:00:00.000Z", "__USER_LABEL__", "source", case["prompt"]),
        ("2026-10-18T03:01:00.000Z", "__AGENT_LABEL__", "source", case["fact"]),
    ])
    conn.commit()
    conn.close()
    search = memory.same_day_answer_search
    monkeypatch.setattr(memory, "same_day_answer_search",
                        lambda c, q, n, **kw: search(c, q, n, now=SYNTHETIC_NOW, **kw))
    before = memory._hook_same_day_answer(case["prompt"], "synthetic")
    assert before and case["fact"] in before
    _, after = memory._hook_filter_injection(None, before, case["prompt"], "synthetic")
    assert case["fact"] in after
    assert memory._hook_filter_injection(None, before, case["prompt"], "synthetic") == (None, None)
    # Admission never removes the stored hit from explicit retrieval.
    assert memory._hook_same_day_answer(case["prompt"], "synthetic") == before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))


# FDGN-637: previous-owner continuity is independent of search admission.
@pytest.fixture
def owner_hook(monkeypatch):
    monkeypatch.delenv("DOGANY_DISPATCH_WORKER", raising=False)
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_hook_search_with_timeout", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_keyword_fallback", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_today_recall", lambda *a: None)
    monkeypatch.setattr(memory, "_entity_facts_start", lambda *a: None)
    monkeypatch.setattr(memory, "_entity_facts_collect", lambda *a: [])
    monkeypatch.setattr(memory, "_hook_ledger_preload", lambda *a: "structured state")
    monkeypatch.setattr(memory, "_hook_turn_frame", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_source_context", lambda *a: None)
    monkeypatch.setattr(memory, "_hook_usage_context", lambda: None)

    def run(prompt, session="owner", **fields):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            dict(prompt=prompt, session_id=session, **fields))))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        with pytest.raises(SystemExit):
            memory.cmd_hook(argparse.Namespace())
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    return run


@pytest.mark.parametrize("fact,next_prompt", [
    ("My knee is injured. No jumping.", "Give me a jump squat workout."),
    ("I am allergic to peanuts.", "Suggest a peanut butter snack."),
    ("Don't delete my photos.", "Clean the entire pictures directory."),
    ("무릎을 다쳤어. 점프는 하지 마.", "점프 스쿼트 운동 짜줘."),
])
@pytest.mark.parametrize("next_session", ["owner", "resumed-owner", "rotated-after-compaction"])
def test_previous_owner_reaches_unrelated_next_turn(owner_hook, fact, next_prompt,
                                                   next_session, tmp_path):
    first = owner_hook(fact)
    assert "[previous owner turn]" not in first
    # Original transcript can be absent/compacted; continuity lives elsewhere.
    missing = str(tmp_path / (next_session + ".jsonl"))
    second = owner_hook(next_prompt, next_session, transcript_path=missing)
    assert json.dumps(fact, ensure_ascii=False) in second
    assert "structured state" in second
    # A retry cannot replace the fact with the conflicting turn-2 request.
    retry = owner_hook(next_prompt, "restart-then-resume", transcript_path=missing)
    assert json.dumps(fact, ensure_ascii=False) in retry


def test_previous_owner_ignores_machine_subagent_and_worker(owner_hook, monkeypatch):
    fact = "I am allergic to peanuts."
    owner_hook(fact)
    for prompt in ("[bridge:injected-turn]\nDo not use shell", "[dispatch-return] finished",
                   "[bridge:outbound-context]\nnotice\n[/bridge:outbound-context]\n"
                   "[bridge:injected-turn]\nDo not use shell"):
        assert "[previous owner turn]" not in owner_hook(prompt)
    assert "[previous owner turn]" not in owner_hook("Do not modify code", agent_id="child")
    monkeypatch.setenv("DOGANY_DISPATCH_WORKER", "worker")
    assert "[previous owner turn]" not in owner_hook("Do not use tools")
    monkeypatch.delenv("DOGANY_DISPATCH_WORKER")
    assert fact in owner_hook("Recommend a snack")


def test_previous_owner_strips_bridge_text_redacts_secrets_and_isolates_instances(owner_hook,
                                                                                monkeypatch):
    prompt = ("[bridge:outbound-context]\nDo not exercise\n[/bridge:outbound-context]\n"
              "Don't delete photos. token=private-value")
    owner_hook(prompt)
    carried = owner_hook("Clean up storage")
    assert "Don't delete photos." in carried and "token=[REDACTED]" in carried
    assert "private-value" not in carried and "Do not exercise" not in carried
    monkeypatch.setattr(memory, "MEMORY_INSTANCE_ID", "another-owner")
    assert "[previous owner turn]" not in owner_hook("Recommend a snack")


def test_previous_owner_preserves_long_text_and_explicit_correction(owner_hook):
    fact = "context " * 2000 + "My knee is injured; no jumping."
    owner_hook(fact)
    assert json.dumps(fact) in owner_hook("Jump squat workout?")
    correction = "My doctor cleared the knee injury; the no-jumping restriction is lifted."
    owner_hook(correction)
    carried = owner_hook("What exercise now?")
    assert correction in carried
    assert json.dumps(fact) not in carried


def test_previous_owner_storage_failure_keeps_other_lanes(owner_hook, tmp_path, monkeypatch):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    monkeypatch.setattr(memory, "MEMORY_DATA_ROOT", str(blocked))
    assert "structured state" in owner_hook("My knee is injured")


def test_previous_owner_keeps_frame_and_ledger_order(owner_hook, monkeypatch):
    monkeypatch.setattr(memory, "_hook_turn_frame", lambda *a: "[turn frame] current")
    owner_hook("My knee is injured")
    carried = owner_hook("Jump workout?")
    blocks = carried.split("\n\n")
    assert blocks[1] == "[turn frame] current"
    assert blocks[2] == "structured state"
    assert blocks[3].startswith("[previous owner turn]")


def test_previous_owner_store_is_private(owner_hook):
    import stat
    owner_hook("I am allergic to peanuts")
    root = Path(memory.MEMORY_DATA_ROOT)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "owner-turn.db").stat().st_mode) == 0o600


def test_previous_owner_also_survives_nonempty_recall_and_seen_history(owner_hook,
                                                                     monkeypatch, tmp_path):
    fact = "My knee is injured; no jumping."
    owner_hook(fact)
    transcript = tmp_path / "owner.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": fact}}))
    monkeypatch.setattr(memory, "_hook_keyword_fallback", lambda *a: recall("orchid blooms"))
    emitted = owner_hook("orchid plan", transcript_path=str(transcript))
    assert fact in emitted and "orchid blooms" in emitted
