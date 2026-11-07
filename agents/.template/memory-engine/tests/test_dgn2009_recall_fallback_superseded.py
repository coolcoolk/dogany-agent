# test-lane: fast    # measured <1s (2026-10-12)
# DGN-2009: (1) a 0-hit memory search falls back to this agent's own recent
# session transcripts before any absence claim; (2) consolidation marks a
# fact [SUPERSEDED] when a later same-subject fact contradicts it, and recall
# never returns a marked line as current.  Incident shape (live, 2026-10-11):
# a gift plan talked through on 10-09 was not in memory, so search found
# nothing and the agent guessed; and the 08-23 "due after the holiday" line
# was repeated after the 10-03 "born 10-02" line.  Names are fictional.
#
# All sandboxed: temp DB / memories dir / jsonl fixtures, zero LLM/embedding.
# Run: python3 -m pytest memory-engine/tests/test_dgn2009_recall_fallback_superseded.py -v

import argparse
import datetime
import json
import os
import sys

import pytest

ENGINE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "dgn1962")

# Frozen at the live recall instant: 2026-10-11 21:36 local.
FROZEN_NOW = datetime.datetime(2026, 10, 11, 21, 36, 0).astimezone()


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


def _at(day_offset, hh, mm):
    t = FROZEN_NOW.replace(hour=hh, minute=mm) + datetime.timedelta(days=day_offset)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _msg(typ, text, ts):
    return json.dumps({"type": typ, "timestamp": ts,
                       "message": {"role": typ, "content": text}},
                      ensure_ascii=False)


def _session(proj, name, lines):
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, name + ".jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


PLAN = ("크래프트 박스 선물 계획: "
        "콜리에서 주문 완료, 포장은 "
        "리본 + 카드, 남은 단계: 카드 쓰기")
QUERY = "크래프트 박스 선물"   # "kraft box gift"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    proj = str(tmp_path / "projects" / "agent-life")
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", os.path.join(proj, "*.jsonl"))
    monkeypatch.setattr(memory, "datetime", _FrozenDatetimeModule)

    def no_embed(_text):
        raise OSError("ollama down (test)")
    monkeypatch.setattr(memory, "embed", no_embed)
    return proj


def _seed(lines, source="MEMORY.md"):
    conn = memory.connect()
    memory.init_db(conn)
    for t in lines:
        cur = conn.execute("INSERT INTO notes(source_file, section, text) VALUES(?,?,?)",
                           (source, "people", t))
        conn.execute("INSERT INTO notes_fts(rowid, text) VALUES(?,?)", (cur.lastrowid, t))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# (1) recall fallback to the agent's own recent transcripts
# ---------------------------------------------------------------------------

def test_zero_hit_search_finds_plan_in_recent_transcript(sandbox, capsys):
    _session(sandbox, "S1009plan", [
        _msg("user", "크래프트 박스로 선물 포장하자", _at(-2, 0, 45)),
        _msg("assistant", PLAN, _at(-2, 0, 50)),
    ])
    _seed(["점심은 김치찌개 (2026-09-01, nightly-consolidate)"])
    results = memory.search_core(QUERY, k=5, log=False)
    recent = [r for r in results if r["kind"] == "recent-transcript"]
    assert recent, results
    assert "콜리" in recent[0]["text"]                  # the Kurly order
    assert recent[0]["section"].startswith("answer 2026-10-09"), recent[0]

    capsys.readouterr()
    memory.cmd_search(argparse.Namespace(query=QUERY, k=5, json=False))
    assert "recent-convo:" in capsys.readouterr().out


def test_fallback_skipped_when_memory_hits(sandbox):
    _session(sandbox, "S1009plan", [
        _msg("user", QUERY, _at(-2, 0, 45)),
        _msg("assistant", PLAN, _at(-2, 0, 50)),
    ])
    _seed([QUERY + " 리본 포장 (2026-10-09, nightly-consolidate)"])
    results = memory.search_core(QUERY, k=5, log=False)
    assert results and results[0]["kind"] == "note"
    assert not [r for r in results if r["kind"] == "recent-transcript"]


def test_fallback_window_and_absence_message(sandbox, capsys):
    # Older than the window -> out; today's rows are the same-day lanes', not
    # duplicated as recent-transcript.
    _session(sandbox, "Sold", [
        _msg("user", QUERY + " 어때?", _at(-(memory.RECENT_TRANSCRIPT_DAYS + 2), 10, 0)),
        _msg("assistant", PLAN, _at(-(memory.RECENT_TRANSCRIPT_DAYS + 2), 10, 1)),
    ])
    _session(sandbox, "Stoday", [
        _msg("user", QUERY + " 어때?", _at(0, 9, 0)),
        _msg("assistant", PLAN, _at(0, 9, 1)),
    ])
    results = memory.search_core(QUERY, k=5, log=False)
    assert not [r for r in results if r["kind"] == "recent-transcript"], results
    assert [r for r in results if r["kind"] == "today-answer"], results

    memory.cmd_search(argparse.Namespace(query="등산화 사이즈", k=5, json=False))
    out = capsys.readouterr().out
    assert "last %d days" % memory.RECENT_TRANSCRIPT_DAYS in out, out


# ---------------------------------------------------------------------------
# (2) superseded facts: the 08-23 "due after Chuseok" vs 10-03 "born 10-02"
# ---------------------------------------------------------------------------

MOTHER = "이은솔"
LINE_OLD = MOTHER + " 출산 예정일은 추석 이후 (2026-08-23, nightly-consolidate)"
LINE_NEW = MOTHER + " 딸 출산했음, 10/2 태어남 (2026-10-03, nightly-consolidate)"
LINE_OTHER = MOTHER + " 꽃 알레르기 있음 (2026-05-10, nightly-consolidate)"
LINE_FACT_A = "Kim surgery at the hospital (2026-08-01, nightly-consolidate)"
LINE_FACT_B = "Kim hospital discharge done (2026-09-05, nightly-consolidate)"
CHUSEOK = "추석 이후"


@pytest.fixture
def mem(tmp_path, monkeypatch):
    d = tmp_path / "memories"
    d.mkdir()
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(d))
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "_recall_subjects_cache", None)
    monkeypatch.setattr(memory, "_ledger_log", lambda line: None)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("LIFEKIT_DIR", raising=False)
    monkeypatch.delenv("FAKE_MODE", raising=False)
    sys.modules.pop(memory.ENTITY_FACTS_MODULE, None)
    yield d
    sys.modules.pop(memory.ENTITY_FACTS_MODULE, None)
    while FIXTURE_DIR in sys.path:
        sys.path.remove(FIXTURE_DIR)


def _entities(monkeypatch, tmp_path, entities):
    with open(memory.RECALL_SUBJECTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data["entities"] = entities
    p = tmp_path / "recall_subjects.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(memory, "RECALL_SUBJECTS_PATH", str(p))
    monkeypatch.setattr(memory, "_recall_subjects_cache", None)


def _write_memory(d, lines):
    (d / "MEMORY.md").write_text(
        "## people\n" + "".join("- %s\n" % l for l in lines), encoding="utf-8")


def _file_lines(d):
    return (d / "MEMORY.md").read_text(encoding="utf-8").splitlines()


def test_consolidation_marks_contradicted_expectation(mem, monkeypatch, tmp_path):
    _entities(monkeypatch, tmp_path, {MOTHER: [], "Kim": []})
    _write_memory(mem, [LINE_OLD, LINE_OTHER, LINE_FACT_A, LINE_NEW, LINE_FACT_B])
    marked = memory.mark_superseded()
    assert [(m[1], m[2]) for m in marked] == [("- " + LINE_OLD, "- " + LINE_NEW)]
    lines = _file_lines(mem)
    assert lines[1] == "- [SUPERSEDED] " + LINE_OLD
    # Other subject, and a non-contradicting later fact, stay current.
    assert "- " + LINE_OTHER in lines and "- " + LINE_FACT_A in lines
    assert memory.mark_superseded() == []                     # idempotent


def test_correction_supersedes_plain_fact(mem, monkeypatch, tmp_path):
    _entities(monkeypatch, tmp_path, {"Kim": []})
    fix = "correction: Kim surgery moved to another hospital (2026-08-10, nightly-consolidate)"
    _write_memory(mem, [LINE_FACT_A, fix])
    assert len(memory.mark_superseded()) == 1
    assert _file_lines(mem)[1] == "- [SUPERSEDED] " + LINE_FACT_A


@pytest.mark.parametrize("old,new", [
    ("Momo is a dog", "Momo is a cat"),
    ("Rowan lives in Oslo", "Rowan lives in Bergen"),
    ("Rowan's favorite color is blue", "Rowan's favorite color is green"),
    ("Cache capacity: 32", "Cache capacity: 64"),
    ("\ubaa8\ubaa8\ub294 \uac15\uc544\uc9c0\uc57c", "\ubaa8\ubaa8\ub294 \uace0\uc591\uc774\uc57c"),
])
def test_explicit_correction_matches_without_topic_lexicon(old, new):
    rows = [("old", old + " (2026-10-14, nightly-consolidate)"),
            ("new", "Correction: " + new + " (2026-10-16, nightly-consolidate)")]
    assert memory.find_superseded(rows, entity_terms=[]) == {"old": "new"}


@pytest.mark.parametrize("new", [
    "Correction: Nori is a cat",
    "Correction: Momo likes naps",
    "Correction: Momo is a dog",
    "Momo is a cat",
    "Correction: Momo is a cat (2026-10-16, by=agent-judged)",
    "Correction: Momo is a cat (2026-10-16, by=unverified)",
])
def test_unrelated_or_unconfirmed_claim_cannot_supersede(new):
    if not new.endswith(")"):
        new += " (2026-10-16, nightly-consolidate)"
    old = "Momo is a dog (2026-10-14, nightly-consolidate)"
    assert memory.find_superseded([("old", old), ("new", new)], entity_terms=[]) == {}


def test_kit_entities_key_marking_without_instance_list(mem, monkeypatch):
    monkeypatch.setenv("LIFEKIT_DIR", FIXTURE_DIR)
    _write_memory(mem, [LINE_OLD, LINE_NEW])
    assert len(memory.mark_superseded()) == 1


def test_no_entity_no_mark(mem):
    # No instance list, no kit: subject cannot be established -> untouched.
    _write_memory(mem, [LINE_OLD, LINE_NEW])
    assert memory.mark_superseded() == []
    assert memory.mark_superseded(dry_run=True) == []


def test_superseded_never_recalled_as_current(mem, monkeypatch, tmp_path):
    _entities(monkeypatch, tmp_path, {MOTHER: []})
    _write_memory(mem, [LINE_OLD, LINE_NEW])
    memory.mark_superseded()
    stored = [l[2:] for l in _file_lines(mem) if l.startswith("- ")]
    _seed(stored)
    monkeypatch.setattr(memory, "embed", lambda t: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", str(tmp_path / "none" / "*.jsonl"))
    # Skill search: a phrase hit on the marked line returns nothing current.
    texts = [r["text"] for r in memory.search_core(CHUSEOK, k=5, log=False)]
    assert not any(CHUSEOK in t for t in texts), texts
    # Hook curation (keyword lane renders through _recall_curate).
    lines, withheld = memory._recall_curate(
        [{"section": "people", "text": t} for t in stored],
        today=datetime.date(2026, 10, 11))
    assert not any(CHUSEOK in l for l in lines) and withheld == 1, lines
    assert any("10/2" in l for l in lines), lines
