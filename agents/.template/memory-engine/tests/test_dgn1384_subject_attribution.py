# test-lane: fast
# DGN-1384: a consolidated line keeps WHO said it, checked by code.
# Sandboxed: no model call, no transcript dir, no memories/ write.
#
# Run: python3 -m pytest memory-engine/tests/test_dgn1384_subject_attribution.py -v

import json
import os
import sys

import pytest

ENGINE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ENGINE_DIR)

import memory  # noqa: E402

U = "__USER_LABEL__: "
A = "__AGENT_LABEL__: "


@pytest.fixture(autouse=True)
def _fresh_subjects():
    memory._ITEM_SUBJECTS.clear()
    yield
    memory._ITEM_SUBJECTS.clear()


def _subject(fact):
    return memory._ITEM_SUBJECTS[fact][0]


def test_resolver_module_loads():
    # The owner check fails closed without it; the template ships it.
    assert memory._owner_turn_lib() is not None


def test_owner_quote_found_in_owner_line_is_owner_quoted():
    chunk = U + "protein target 160g a day, set it\n" + A + "ok, saved"
    facts = memory._attribute_items(
        ['[U] protein target 160g a day || "protein target 160g a day"'], chunk)
    assert facts == ["protein target 160g a day"]
    assert _subject(facts[0]) == memory.SUBJECT_OWNER


def test_owner_claim_quoting_the_agent_is_unverified():
    # The forgery direction: the agent's conclusion dressed as the owner's.
    chunk = U + "hmm ok\n" + A + "so scan-style linting is banned, hooks only"
    facts = memory._attribute_items(
        ['[U] scan-style linting is banned || "scan-style linting is banned"'], chunk)
    assert _subject(facts[0]) == memory.SUBJECT_UNVERIFIED


def test_owner_claim_without_quote_is_unverified():
    chunk = U + "wake at 5\n"
    facts = memory._attribute_items(["[U] wakes at 05:00"], chunk)
    assert _subject(facts[0]) == memory.SUBJECT_UNVERIFIED


def test_agent_mark_is_agent_judged():
    facts = memory._attribute_items(["[A] weekly retro looks sufficient"], A + "x")
    assert facts == ["weekly retro looks sufficient"]
    assert _subject(facts[0]) == memory.SUBJECT_AGENT


def test_single_meal_is_not_upgraded_to_fixed_breakfast():
    quote = "\uc544\uce68\uc5d0 \uc774\uc0ad\ud1a0\uc2a4\ud2b8 \uba39\uc5c8\uc5b4"
    chunk = U + quote
    facts = memory._attribute_items(
        ['[U] \uace0\uc815 \uc544\uce68 \uc2dd\ub2e8: \uc774\uc0ad\ud1a0\uc2a4 || "' + quote + '"'], chunk)
    assert facts == [quote + " [obs:1]"]
    assert _subject(facts[0]) == memory.SUBJECT_OWNER


def test_explicit_owner_schedule_keeps_habit_word():
    quote = "\uc55e\uc73c\ub85c \ub9e4\uc77c 9\uc2dc\uc5d0 \uc54c\ub824\uc918"
    facts = memory._attribute_items(
        ['[U] ' + quote + ' || "' + quote + '"'], U + quote)
    assert facts == [quote]


@pytest.mark.parametrize("fact", [
    "consolidate runs \ub9e4\uc77c 04:30",
    "the backup job always runs after push",
])
def test_agent_infrastructure_habits_are_unchanged(fact):
    facts = memory._attribute_items(["[A] " + fact], A + fact)
    assert facts == [fact]
    assert _subject(facts[0]) == memory.SUBJECT_AGENT


def test_unmarked_item_is_unverified():
    facts = memory._attribute_items(["some fact"], U + "some fact")
    assert _subject(facts[0]) == memory.SUBJECT_UNVERIFIED


def test_dispatch_return_row_is_not_an_owner_line():
    chunk = U + "[dispatch-return] run done -- tags never move\n"
    facts = memory._attribute_items(['[U] tags never move || "tags never move"'], chunk)
    assert _subject(facts[0]) == memory.SUBJECT_UNVERIFIED


def test_photo_wrapper_text_is_not_owner_words_but_caption_is():
    lib = memory._owner_turn_lib()
    head = lib._WRAPPER_STRINGS["en"]["photo_prompt_single"]
    cap = lib._WRAPPER_STRINGS["en"]["user_caption"].format(caption="this is my lunch")
    chunk = U + head + "\n/tmp/a.jpg\n" + cap
    wrap = memory._attribute_items(['[U] user sent a photo || "The user sent a photo"'], chunk)
    assert _subject(wrap[0]) == memory.SUBJECT_UNVERIFIED
    own = memory._attribute_items(['[U] lunch photo || "this is my lunch"'], chunk)
    assert _subject(own[0]) == memory.SUBJECT_OWNER


def test_multiline_owner_message_is_one_body():
    chunk = U + "first line\nsecond line has the rule\n" + A + "noted"
    facts = memory._attribute_items(['[U] the rule || "second line has the rule"'], chunk)
    assert _subject(facts[0]) == memory.SUBJECT_OWNER


def test_correction_marker_survives_after_subject_mark():
    chunk = U + "no, protein is 150g now"
    facts = memory._attribute_items(['[U] \uc815\uc815: protein 150g || "protein is 150g now"'], chunk)
    assert memory._is_correction(facts[0])
    assert _subject(facts[0]) == memory.SUBJECT_OWNER


def test_owner_wins_when_same_fact_seen_twice():
    memory._attribute_items(["[A] fact x"], A + "fact x")
    memory._attribute_items(['[U] fact x || "fact x"'], U + "fact x")
    assert _subject("fact x") == memory.SUBJECT_OWNER


def test_subject_tag_shapes():
    memory._attribute_items(['[U] f1 || "say (this)"'], U + "say (this)")
    memory._attribute_items(["[A] f2"], A + "f2")
    t1 = memory._subject_tag("f1", "2026-10-10")
    assert t1 == 'f1 -- owner: "say [this]" (2026-10-10, by=owner-quoted, via=nightly-consolidate)'
    assert memory._subject_tag("f2", "2026-10-10").endswith(
        "(2026-10-10, by=agent-judged, via=nightly-consolidate)")
    assert memory._subject_tag("never-seen", "2026-10-10").endswith(
        "(2026-10-10, by=unverified, via=nightly-consolidate)")


def test_resolver_missing_fails_closed(monkeypatch):
    monkeypatch.setattr(memory, "_OWNER_TURN", [None])
    facts = memory._attribute_items(['[U] a || "a b"'], U + "a b")
    assert _subject(facts[0]) == memory.SUBJECT_UNVERIFIED


def _write_transcript(tmp_path, entries):
    d = tmp_path / "proj"
    d.mkdir()
    fp = d / "s1.jsonl"
    fp.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return str(d / "*.jsonl")


def test_ingest_relabels_machine_user_rows_on_consolidate_lane_only(tmp_path, monkeypatch):
    import datetime
    entries = [
        {"type": "user", "timestamp": "2026-10-10T01:00:00.000Z",
         "message": {"content": "keep the retro weekly"}},
        {"type": "user", "timestamp": "2026-10-10T01:01:00.000Z",
         "message": {"content": "[dispatch-return] job finished -- result text"}},
        {"type": "assistant", "timestamp": "2026-10-10T01:02:00.000Z",
         "message": {"content": [{"type": "text", "text": "noted"}]}},
    ]
    monkeypatch.setattr(memory, "TRANSCRIPT_GLOB", _write_transcript(tmp_path, entries))
    now = datetime.datetime(2026, 10, 10, 2, tzinfo=datetime.timezone.utc)
    rows = memory._iter_transcript_rows("2026-10-09T00:00:00.000Z", now)
    spk = [r[1] for r in rows]
    assert spk == ["__USER_LABEL__", memory.MACHINE_SPEAKER, "__AGENT_LABEL__"]
    raw_rows = memory._iter_transcript_rows("2026-10-09T00:00:00.000Z", now, raw=True)
    assert [r[1] for r in raw_rows][1] == "__USER_LABEL__"   # same-day lane untouched


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
