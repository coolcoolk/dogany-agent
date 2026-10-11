# DGN-1949: same-day recall gap. Replays the live case: the agent gave a
# housewarming-gift list at 07:34 and again at 15:54; at 18:02 a NEW session
# asked "집들이선물이나다시 알려줘" and the agent claimed it was not recorded.
# Before the fix the glued prompt tokenized to one LIKE term that matched
# nothing, so neither the hook nor `memory.py search` surfaced the answer.
#
# All sandboxed: temp DB + synthetic jsonl fixtures, zero LLM / embedding.
# Run: /usr/bin/python3 -m pytest memory-engine/tests/test_dgn1949_same_day_answer.py -v

import argparse
import datetime
import io
import json
import os
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

# Frozen clock at the live-case instant (18:02:30 local) -- same pattern as
# test_dgn501_sameday_recall.py: memory.py's local-midnight anchor and the
# fixtures share one instant, so the suite is wall-clock independent.
FROZEN_NOW = datetime.datetime(2026, 10, 8, 18, 2, 30).astimezone()


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fromtimestamp(FROZEN_NOW.timestamp())
        return cls.fromtimestamp(FROZEN_NOW.timestamp(), tz)


class _FrozenDatetimeModule:
    timezone = datetime.timezone
    timedelta = datetime.timedelta
    date = datetime.date
    datetime = _FrozenDateTime


def _at(hh, mm, day_offset=0):
    t = FROZEN_NOW.replace(hour=hh, minute=mm, second=0, microsecond=0)
    t = t + datetime.timedelta(days=day_offset)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _msg(typ, text, ts):
    return json.dumps(
        {"type": typ, "timestamp": ts, "message": {"role": typ, "content": text}},
        ensure_ascii=False,
    )


def _write_session(proj_dir, session, lines):
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, f"{session}.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


LIST_0734 = ("집들이 선물로 이런 게 좋아요:\n1. 디퓨저\n2. 와인 한 병\n"
             "3. 핸드워시 세트\n4. 휴지·세제 묶음")
LIST_1554 = ("아침에 드린 집들이 선물 목록 다시 정리했어요:\n1. 디퓨저\n2. 와인 한 병\n"
             "3. 핸드워시 세트\n4. 휴지·세제 묶음\n5. 고급 수건 세트")
LIVE_PROMPT = "집들이선물이나다시 알려줘"


def _ag_sequence(proj_dir):
    """The live day: two answered sessions + unrelated noise + yesterday's list."""
    _write_session(proj_dir, "S0734aaa", [
        _msg("user", "친구 집들이 가는데 선물 뭐 사가면 좋을까?", _at(7, 33)),
        _msg("assistant", LIST_0734, _at(7, 34)),
    ])
    _write_session(proj_dir, "S1200bbb", [
        _msg("user", "점심 메뉴 추천해줘", _at(12, 0)),
        _msg("assistant", "김치찌개 어때요?", _at(12, 1)),
    ])
    _write_session(proj_dir, "S1554ccc", [
        _msg("user", "아까 그 선물 목록 다시 보여줘", _at(15, 53)),
        _msg("assistant", LIST_1554, _at(15, 54)),
    ])
    _write_session(proj_dir, "S1802ddd", [
        _msg("user", LIVE_PROMPT, _at(18, 2)),
    ])


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    proj_dir = tmp_path / "projects" / "agent-life"
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(proj_dir / "*.jsonl"))
    monkeypatch.setattr(memory, "datetime", _FrozenDatetimeModule)
    return str(proj_dir)


def _run_hook(stdin_obj, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_obj, ensure_ascii=False)))
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_hook_search_with_timeout", lambda p: None)
    monkeypatch.setattr(memory, "_hook_keyword_fallback", lambda p: None)
    monkeypatch.setattr(memory, "_hook_ledger_preload", lambda p, t0=None: None)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    try:
        memory.cmd_hook(argparse.Namespace())
    except SystemExit:
        pass
    finally:
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    out = buf.getvalue().strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _no_embed(monkeypatch):
    def boom(_text):
        raise OSError("ollama down (test)")
    monkeypatch.setattr(memory, "embed", boom)


# ---------------------------------------------------------------------------
# (a) recall hook: the 18:02 back-reference prompt now carries the 15:54 list
# ---------------------------------------------------------------------------

def test_ag_hook_injects_latest_answer_with_time(sandbox, monkeypatch):
    _ag_sequence(sandbox)
    ctx = _run_hook({"prompt": LIVE_PROMPT, "session_id": "S1802ddd"}, monkeypatch)
    assert memory.SAME_DAY_HEADER in ctx, f"same-day answer block missing: {ctx!r}"
    assert "15:54" in ctx, "best hit must carry its time"
    assert "고급 수건 세트" in ctx, "the 15:54 answer body (list) must be injected"
    assert "07:34" in ctx, "the earlier same-topic answer time is pointed at"
    assert "김치찌개" not in ctx, "unrelated exchange must not be injected"


def test_ag_hook_no_hit_injects_nothing(sandbox, monkeypatch):
    _ag_sequence(sandbox)
    ctx = _run_hook({"prompt": "등산화 추천 다시 알려줘", "session_id": "S1802ddd"}, monkeypatch)
    assert memory.SAME_DAY_HEADER not in ctx, f"no hit must inject nothing: {ctx!r}"


def test_hook_requires_back_reference(sandbox, monkeypatch):
    _ag_sequence(sandbox)
    ctx = _run_hook({"prompt": "집들이 선물 추천", "session_id": "S1802ddd"}, monkeypatch)
    assert memory.SAME_DAY_HEADER not in ctx, "lane is gated on back-reference prompts"


def test_hook_answer_is_size_bounded(sandbox, monkeypatch):
    _write_session(sandbox, "Slong", [
        _msg("user", "집들이 선물 목록 길게 줘", _at(9, 0)),
        _msg("assistant", "집들이 선물 목록\n" + ("디퓨저 와인 " * 800), _at(9, 1)),
    ])
    ctx = _run_hook({"prompt": "집들이 선물 다시", "session_id": "Snow"}, monkeypatch)
    assert memory.SAME_DAY_HEADER in ctx
    block = ctx[ctx.index(memory.SAME_DAY_HEADER):].split("\n\n[오늘 다른 세션")[0]
    budget = memory.SAME_DAY_ANSWER_MAX + memory.SAME_DAY_QUESTION_MAX + 400
    assert len(block) <= budget, f"answer block over budget: {len(block)}"


def test_yesterday_answer_is_not_today(sandbox, monkeypatch):
    _write_session(sandbox, "Syday", [
        _msg("user", "집들이 선물 뭐가 좋아?", _at(20, 0, day_offset=-1)),
        _msg("assistant", "어제 목록: 디퓨저", _at(20, 1, day_offset=-1)),
    ])
    ctx = _run_hook({"prompt": LIVE_PROMPT, "session_id": "Snow"}, monkeypatch)
    assert memory.SAME_DAY_HEADER not in ctx, "only today's transcripts are in scope"


# ---------------------------------------------------------------------------
# (b) absence-claim gate: `memory.py search` covers today's answers
# ---------------------------------------------------------------------------

def test_ag_search_finds_today_answer(sandbox, monkeypatch, capsys):
    _ag_sequence(sandbox)
    _no_embed(monkeypatch)
    results = memory.search_core(LIVE_PROMPT, k=5, log=False)
    answers = [r for r in results if r.get("kind") == "today-answer"]
    assert answers, f"search must surface today's answer: {results!r}"
    assert "15:54" in answers[0]["section"]
    assert "고급 수건 세트" in answers[0]["text"]

    capsys.readouterr()
    memory.cmd_search(argparse.Namespace(query="등산화 사이즈", k=5, json=False))
    out = capsys.readouterr().out
    assert "today's conversations" in out, "0-hit verdict must state today was searched"


# ---------------------------------------------------------------------------
# no cross-agent leak
# ---------------------------------------------------------------------------

def test_no_cross_agent_leak(tmp_path, monkeypatch):
    projects = str(tmp_path / "projects")
    me = str(tmp_path / "agents" / "life")
    other = str(tmp_path / "agents" / "life-old")      # sibling with a shared prefix
    third = str(tmp_path / "agents" / "devbot")
    for root in (other, third):
        d = os.path.dirname(memory._transcript_glob_for(root, projects))
        _ag_sequence(d)
    my_glob = memory._transcript_glob_for(me, projects)
    os.makedirs(os.path.dirname(my_glob))
    _write_session(os.path.dirname(my_glob), "Smine", [
        _msg("user", "오늘 날씨 어때?", _at(9, 0)),
        _msg("assistant", "맑아요", _at(9, 1)),
    ])
    assert "*" not in os.path.dirname(my_glob), "agent dir part must be literal"

    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "life-state.db"))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", my_glob)
    monkeypatch.setattr(memory, "datetime", _FrozenDatetimeModule)
    _no_embed(monkeypatch)

    ctx = _run_hook({"prompt": LIVE_PROMPT, "session_id": "Snow"}, monkeypatch)
    assert "디퓨저" not in ctx, f"another agent's transcript leaked into the hook: {ctx!r}"
    results = memory.search_core(LIVE_PROMPT, k=5, log=False)
    assert not any("디퓨저" in r["text"] for r in results), "leak into search"
    conn = memory.connect()
    sessions = {r[0] for r in conn.execute("SELECT DISTINCT session FROM transcript_notes")}
    conn.close()
    assert sessions == {"Smine"}, f"only this agent's sessions may be indexed: {sessions}"


def test_default_glob_is_this_agent_only():
    agent_root = os.path.normpath(os.path.join(ENGINE_DIR, ".."))
    expected = memory._transcript_glob_for(agent_root, os.path.expanduser("~/.claude/projects"))
    assert memory.TRANSCRIPT_GLOB == expected
    assert "*" not in os.path.dirname(memory.TRANSCRIPT_GLOB)
