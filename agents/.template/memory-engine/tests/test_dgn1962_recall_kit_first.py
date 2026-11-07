# test-lane: fast    # measured <1s (2026-10-10)
# DGN-1962: recall injection -- kit facts first, newer memory wins, passed
# expectations stale, date anchor.  End-to-end through cmd_hook with the
# keyword recall lane (no Ollama) and the fake entity-facts provider in
# tests/fixtures/dgn1962.  Incident shape: a person's child was born
# (2026-10-02, kit record + a 10-03 memory line) but an older 08-23 memory
# line still said the birth was expected after the holiday, and that older
# line drove the answer.  Names are fictional.
#
# Run: python3 -m pytest memory-engine/tests/test_dgn1962_recall_kit_first.py -v

import argparse
import datetime
import io
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
TODAY = datetime.date(2026, 10, 9)

MOTHER = "\uc774\uc740\uc194"
# Memory lines exactly as the engine stores them: "<fact> (<date>, <source>)".
LINE_OLD = MOTHER + " \ucd9c\uc0b0 \uc608\uc815\uc77c\uc740 \ucd94\uc11d \uc774\ud6c4 (2026-08-23, nightly-consolidate)"
LINE_NEW = MOTHER + " \ub538 \ucd9c\uc0b0\ud588\uc74c, 10/2 \ud0dc\uc5b4\ub0a8 (2026-10-03, nightly-consolidate)"
LINE_OTHER = MOTHER + " \uaf43 \uc54c\ub808\ub974\uae30 \uc788\uc74c (2026-05-10, nightly-consolidate)"
LINE_DATED_EXPECT = MOTHER + " \ucd9c\uc0b0 \uc608\uc815\uc77c 2026-10-01 (2026-08-23, nightly-consolidate)"
PROMPT = MOTHER + " \uc120\ubb3c \ucd94\ucc9c\ud574\uc918"  # space-separated: keyword lane tokens


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setattr(memory, "HERE", str(tmp_path))
    monkeypatch.setattr(memory, "_today_local", lambda: TODAY)
    monkeypatch.setattr(memory, "_hook_maybe_reindex", lambda: None)
    monkeypatch.setattr(memory, "_hook_search_with_timeout", lambda p: None)
    monkeypatch.setattr(memory, "_hook_today_recall", lambda p, s: None)
    monkeypatch.setattr(memory, "_hook_body_state_line", lambda: None)
    monkeypatch.setattr(memory, "_ledger_log", lambda line: None)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("LIFEKIT_DIR", raising=False)
    monkeypatch.delenv("FAKE_MODE", raising=False)
    monkeypatch.setattr(memory, "_recall_subjects_cache", None)
    sys.modules.pop(memory.ENTITY_FACTS_MODULE, None)
    yield tmp_path
    sys.modules.pop(memory.ENTITY_FACTS_MODULE, None)
    while FIXTURE_DIR in sys.path:
        sys.path.remove(FIXTURE_DIR)


def _seed(lines):
    conn = memory.connect()
    memory.init_db(conn)
    for t in lines:
        cur = conn.execute(
            "INSERT INTO notes(source_file, section, text) VALUES(?,?,?)",
            ("people.md", "people", t))
        conn.execute("INSERT INTO notes_fts(rowid, text) VALUES(?,?)",
                     (cur.lastrowid, t))
    conn.commit()
    conn.close()


def _run_hook(monkeypatch, prompt=PROMPT):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": prompt, "session_id": "S1"}, ensure_ascii=False)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    try:
        memory.cmd_hook(argparse.Namespace())
    except SystemExit:
        pass
    finally:
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    out = buf.getvalue().strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None


def _kit_block(ctx):
    return any(l.startswith(memory.LEDGER_SENTINEL) for l in ctx.splitlines())


def _with_kit(monkeypatch, mode=""):
    monkeypatch.setenv("LIFEKIT_DIR", FIXTURE_DIR)
    if mode:
        monkeypatch.setenv("FAKE_MODE", mode)


def test_incident_kit_first_end_to_end(env, monkeypatch):
    _seed([LINE_OLD, LINE_NEW, LINE_OTHER])
    _with_kit(monkeypatch)
    ctx = _run_hook(monkeypatch)
    assert ctx is not None
    # Kit facts present, with the birth fact, rendered as structured state.
    kit_line = [l for l in ctx.splitlines() if "born 2026-10-02" in l]
    assert kit_line and kit_line[0].startswith(memory.LEDGER_SENTINEL), ctx
    # Kit facts stand BEFORE recall.
    assert ctx.index("born 2026-10-02") < ctx.index("[\uad00\ub828 \uae30\uc5b5"), ctx
    # The stale expectation never reaches the model; the birth subject is
    # the kit's, so the memory duplicate is withheld too.
    assert "\ucd94\uc11d \uc774\ud6c4" not in ctx, ctx
    assert "10/2 \ud0dc\uc5b4\ub0a8" not in ctx, ctx
    # Memory fills what the kit lacks, with the date anchor.
    other = [l for l in ctx.splitlines() if "\uc54c\ub808\ub974\uae30" in l]
    assert other and "recorded 2026-05-10, today 2026-10-09" in other[0], ctx
    assert "2 recalled line(s) withheld" in ctx, ctx


def _subjects_with(monkeypatch, tmp_path, entities):
    """recall_subjects.json copy with an instance entity list."""
    with open(memory.RECALL_SUBJECTS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data["entities"] = entities
    path = tmp_path / "recall_subjects.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(memory, "RECALL_SUBJECTS_PATH", str(path))
    monkeypatch.setattr(memory, "_recall_subjects_cache", None)


def test_newer_memory_wins_without_kit(env, monkeypatch):
    _subjects_with(monkeypatch, env, {MOTHER: []})
    _seed([LINE_OLD, LINE_NEW, LINE_OTHER])
    ctx = _run_hook(monkeypatch)
    assert ctx is not None
    assert "\ucd94\uc11d \uc774\ud6c4" not in ctx, ctx       # 08-23 superseded
    newer = [l for l in ctx.splitlines() if "10/2 \ud0dc\uc5b4\ub0a8" in l]
    assert newer and "today 2026-10-09" in newer[0], ctx
    assert "\uc54c\ub808\ub974\uae30" in ctx                 # other subject kept
    assert not _kit_block(ctx)               # no kit, no block


def test_passed_expectation_marked_stale(env, monkeypatch):
    _seed([LINE_DATED_EXPECT])
    ctx = _run_hook(monkeypatch)
    line = [l for l in ctx.splitlines() if "2026-10-01" in l and l.startswith("- ")]
    assert line and "STALE: expected date 2026-10-01 has passed" in line[0], ctx


def test_undated_old_expectation_flagged(env, monkeypatch):
    _seed([LINE_OLD])
    ctx = _run_hook(monkeypatch)
    line = [l for l in ctx.splitlines() if "\ucd94\uc11d \uc774\ud6c4" in l]
    assert line and "expectation recorded 47 days ago" in line[0], ctx
    assert "recorded 2026-08-23, today 2026-10-09" in line[0], ctx


@pytest.mark.parametrize("mode", ["raise", "garbage"])
def test_provider_failure_drops_lane_not_turn(env, monkeypatch, mode):
    _subjects_with(monkeypatch, env, {MOTHER: []})
    _seed([LINE_OLD, LINE_NEW])
    _with_kit(monkeypatch, mode)
    ctx = _run_hook(monkeypatch)
    assert ctx is not None and not _kit_block(ctx)
    assert "10/2 \ud0dc\uc5b4\ub0a8" in ctx and "\ucd94\uc11d \uc774\ud6c4" not in ctx


def test_provider_timeout_bounded(env, monkeypatch):
    _seed([LINE_NEW])
    _with_kit(monkeypatch, "hang")
    monkeypatch.setattr(memory, "ENTITY_FACTS_BUDGET_SEC", 0.3)
    import time
    t = time.monotonic()
    ctx = _run_hook(monkeypatch)
    assert time.monotonic() - t < 3
    assert ctx is not None and not _kit_block(ctx)


PUSH_BLOCK = ("[bridge:outbound-context]\n--- sent 10:00 ---\n" + PROMPT
              + "?\n[/bridge:outbound-context]\n")


def test_push_then_reply_recalls_on_the_owner_words(env, monkeypatch):
    # DGN-1967 bug 2: the pushes the bridge prefixes to the owner's turn
    # are the agent's words; recall and the kit search read the reply only.
    _seed([LINE_OTHER])
    _with_kit(monkeypatch)
    ctx = _run_hook(monkeypatch, PUSH_BLOCK + "\uc88b\uc544")
    assert ctx is None or "\uc54c\ub808\ub974\uae30" not in ctx, ctx
    assert ctx is None or not _kit_block(ctx), ctx
    ctx = _run_hook(monkeypatch, PUSH_BLOCK + PROMPT)
    assert ctx is not None and "\uc54c\ub808\ub974\uae30" in ctx, ctx


def test_machine_traffic_skips_provider(env, monkeypatch):
    _with_kit(monkeypatch)
    assert memory._entity_facts_start("[cron-inject] " + MOTHER) is None


def test_normalize_skips_malformed_items():
    raw = [None, {"entity": ""}, {"entity": "A", "facts": [{"text": ""}]},
           {"entity": "B", "aliases": ["b2", 3],
            "facts": [{"text": " x  y ", "date": 5, "topics": ["T", None]}]}]
    out = memory._entity_facts_normalize(raw)
    assert out == [{"entity": "B", "terms": ["b", "b2"],
                    "facts": [{"text": "x y", "date": None, "as_of": None,
                               "source": None, "topics": ["t"]}]}]


def test_body_dates_forms():
    body = "a 2026.10.01 b 10/2 c 10월 3일 d 1/2/3"
    got = memory._memory_body_dates(body, datetime.date(2026, 1, 1))
    assert datetime.date(2026, 10, 1) in got
    assert datetime.date(2026, 10, 2) in got
    assert datetime.date(2026, 10, 3) in got
    assert memory._memory_recorded_date("x (2026-08-23, src)") == datetime.date(2026, 8, 23)
    assert memory._memory_recorded_date("x (no stamp)") is None


# ---- DGN-1967 step 11: date and entity fixes ------------------------------

def test_month_day_rolls_to_next_occurrence(env, monkeypatch):
    # Written 2025-12-20, "due 1/15" means 2026-01-15, not 2025-01-15.
    monkeypatch.setattr(memory, "_today_local", lambda: datetime.date(2026, 1, 10))
    _seed([MOTHER + " \ucd9c\uc0b0 \uc608\uc815\uc77c 1/15 (2025-12-20, nightly-consolidate)"])
    ctx = _run_hook(monkeypatch)
    line = [l for l in ctx.splitlines() if "1/15" in l and l.startswith("- ")]
    assert line and "STALE" not in line[0], ctx
    got = memory._memory_body_dates("due 1/15", datetime.date(2025, 12, 20))
    assert got == [datetime.date(2026, 1, 15)]
    # A recent past reference stays in the line's year.
    got = memory._memory_body_dates("10/2 born", datetime.date(2026, 10, 3))
    assert got == [datetime.date(2026, 10, 2)]


def test_fractions_are_not_dates():
    a = datetime.date(2026, 8, 1)
    assert memory._memory_body_dates("flour 1/2 cup", a) == []
    assert memory._memory_body_dates("add 1 1/2 cups", a) == []
    assert memory._memory_body_dates("3/4 of the budget", a) == []
    assert memory._memory_body_dates("\uc124\ud0d5 1/2\ucef5", a) == []
    assert memory._memory_body_dates("due 1/15 for review", a) == [datetime.date(2027, 1, 15)]


def test_fraction_line_not_stale(env, monkeypatch):
    _seed([MOTHER + " \uc774\uc0ac \uc608\uc815, \uc9d0 1/2 \uc815\ub3c4 \uc2f8\ub450\uc5c8\uc74c (2026-10-01, nightly-consolidate)"])
    ctx = _run_hook(monkeypatch)
    line = [l for l in ctx.splitlines() if "1/2" in l and l.startswith("- ")]
    assert line and "STALE" not in line[0], ctx
    assert "expectation recorded 8 days ago" in line[0], ctx


def test_stale_judged_by_latest_date(env, monkeypatch):
    # Moved from 10-01 to 10-20: the later date has not passed.
    _seed([MOTHER + " \ucd9c\uc0b0 \uc608\uc815\uc77c 2026-10-01 -> 2026-10-20 (2026-09-20, nightly-consolidate)"])
    ctx = _run_hook(monkeypatch)
    line = [l for l in ctx.splitlines() if "2026-10-20" in l and l.startswith("- ")]
    assert line and "STALE" not in line[0], ctx


def test_two_people_same_subject_not_superseded(env, monkeypatch):
    # Prompt words are not entities: "hospital" must not key Kim's and
    # Lee's surgery lines onto one subject.
    _seed(["Kim surgery at the hospital (2026-08-01, nightly-consolidate)",
           "Lee surgery at the hospital (2026-09-01, nightly-consolidate)"])
    ctx = _run_hook(monkeypatch, prompt="how is the hospital stuff going")
    assert ctx is not None
    assert "Kim surgery" in ctx and "Lee surgery" in ctx, ctx
    assert "withheld" not in ctx, ctx


def test_listed_entities_key_supersession(env, monkeypatch):
    _subjects_with(monkeypatch, env, {"Kim": [], "Lee": ["Mr Lee"]})
    _seed(["Kim surgery at the hospital (2026-08-01, nightly-consolidate)",
           "Lee surgery at the hospital (2026-09-01, nightly-consolidate)",
           "Kim hospital discharge done (2026-09-05, nightly-consolidate)"])
    ctx = _run_hook(monkeypatch, prompt="how is the hospital stuff going")
    assert "Lee surgery" in ctx and "Kim hospital discharge" in ctx, ctx
    assert "Kim surgery" not in ctx, ctx
    assert "1 recalled line(s) withheld" in ctx, ctx


def test_today_uses_agent_timezone(tmp_path, monkeypatch):
    conf = tmp_path / "agent.conf"
    monkeypatch.setenv("DOGANY_AGENT_CONF", str(conf))
    monkeypatch.setenv("DOGANY_CREW_DIR", str(tmp_path / "crews"))
    fixed = datetime.datetime(2026, 10, 9, 20, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(memory, "_utc_now", lambda: fixed)
    conf.write_text("AGENT_TZ_OFFSET_HOURS=9\n", encoding="utf-8")
    assert memory._today_local() == datetime.date(2026, 10, 10)
    conf.write_text("AGENT_TZ_OFFSET_HOURS=-5\n", encoding="utf-8")
    assert memory._today_local() == datetime.date(2026, 10, 9)
    # Invalid agent value falls through to the crew value.
    conf.write_text("AGENT_TZ_OFFSET_HOURS=99\n", encoding="utf-8")
    (tmp_path / "crews").mkdir()
    (tmp_path / "crews" / "crew.conf").write_text("TZ_OFFSET_HOURS=+14\n", encoding="utf-8")
    assert memory._today_local() == datetime.date(2026, 10, 10)
    assert memory._agent_tz_offset_hours() == 14


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
