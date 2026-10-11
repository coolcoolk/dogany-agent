"""DGN-1955: idrill step branching (step_goto), named slot placeholders
(step_slots / slot_default), and the selected-state marker.

Fixture = the set-logging keyboard (design 2): step 1 carries a weight row
(prescribed value preselected) and a count row; step 2 asks the effort grade.
  * tap count -> effort step -> tap effort fires          (2 taps)
  * tap alt weight -> step 1 re-renders with alt selected (3 taps total)
  * tap the selected weight -> no-op (no write, no edit)
Existing (pre-1955) arms must render byte-identically: golden test against
tests/golden/dgn1955_existing_arms.json, recorded on the pre-1955 engine.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import bridge.bot as bot_mod
from bridge import artifacts
from bridge.bot import TelegramBot
from bridge.tests import _idrill_golden

ARM_ID = "5e7a1955"
CMD_BIN = "/opt/kit/bin/record-entry"
DOT = "\u25cf "

SET_ARM = {
    "session_id": "sess-1955",
    "step_buttons": {
        "1": [
            [["60kg", "60"], ["57.5kg", "57.5"]],
            [["8", "r8"], ["10", "r10"], ["12", "r12"]],
        ],
        "2": [["0", "0"], ["1", "1"], ["2", "2"], ["3", "3"]],
    },
    "step_text": {
        "1": "Set 2 \u00b7 {w}kg",
        "2": "{w}kg \u00d7 {reps} \u00b7 RIR?",
    },
    "step_final": "2",
    "nav_back": True,
    "step_goto": {"1": {"60": "1", "57.5": "1"}},
    "step_slots": {
        "1": {"w": ["60", "57.5"],
              "reps": {"r8": "8", "r10": "10", "r12": "12"}},
        "2": {"rir": ["0", "1", "2", "3"]},
    },
    "slot_default": {"w": "60"},
    "cmd": [CMD_BIN, "sess-1955", "--weight", "{w}", "--count", "{reps}",
            "--grade", "{rir}"],
    "confirm_fmt": "{w}kg \u00d7 {reps} @ {rir}",
}


def run(coro):
    return asyncio.run(coro)


class Q:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.edits = []

    async def edit_message_text(self, text, **kw):
        self.edits.append(("text", text, _rows(kw.get("reply_markup"))))

    async def edit_message_reply_markup(self, *, reply_markup=None, **kw):
        self.edits.append(("markup", None, _rows(reply_markup)))


def _rows(markup):
    if markup is None:
        return None
    return [[(b.text, b.callback_data) for b in row]
            for row in markup.inline_keyboard]


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "files" / "program" / ".idrill-arm"
    d.mkdir(parents=True)
    fired = []

    def fake_run(argv, **kw):
        fired.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path), \
            patch.object(bot_mod.subprocess, "run", side_effect=fake_run):
        yield {"root": tmp_path, "dir": d, "fired": fired,
               "bot": TelegramBot.__new__(TelegramBot)}


def arm_with(env, arm=None):
    (env["dir"] / ARM_ID).write_text(json.dumps(arm or SET_ARM),
                                     encoding="utf-8")


def tap(env, suffix):
    q = Q("idrill:%s:%s" % (ARM_ID, suffix))
    run(env["bot"]._handle_idrill_callback(q, q.data))
    return q


def labels(rows):
    return [[lbl for lbl, _cb in row] for row in rows]


# --- golden: existing arms byte-identical ----------------------------------

def test_existing_arms_golden_byte_identical():
    golden = json.loads(_idrill_golden.GOLDEN_PATH.read_text(encoding="utf-8"))
    # The bridge binds UI strings to the process locale at import time, and
    # the golden is recorded under LOCALE=en. Replay in a child process with
    # the locale pinned so the test pins behaviour, not the host language.
    import os
    import subprocess
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, LOCALE="en")
    out = subprocess.run(
        [sys.executable, "-c",
         "import json,sys; sys.path[:0]=[sys.argv[1], sys.argv[2]];"
         "import _idrill_golden as G;"
         "print(json.dumps(G.transcript(), ensure_ascii=False))",
         str(root), str(Path(__file__).resolve().parent)],
        capture_output=True, text=True, env=env, timeout=120, check=True)
    now = json.loads(out.stdout.strip().splitlines()[-1])
    assert now == golden


# --- initial render: prescribed value preselected ---------------------------

def test_initial_keyboard_preselects_prescribed_weight(env):
    arm_with(env)
    init = artifacts.initial_keyboard_json(env["root"], ARM_ID)
    assert init["text"] == "Set 2 \u00b7 60kg"
    kb = [[b["text"] for b in row] for row in init["reply_markup"]["inline_keyboard"]]
    assert kb == [[DOT + "60kg", "57.5kg"], ["8", "10", "12"]]
    # callback data carries the raw value, never the marker
    cbs = [b["callback_data"] for row in init["reply_markup"]["inline_keyboard"]
           for b in row]
    assert cbs[:2] == ["idrill:%s:1:60" % ARM_ID, "idrill:%s:1:57.5" % ARM_ID]


# --- 2 taps: count -> effort step -> fire ------------------------------------

def test_two_tap_path_count_then_grade(env):
    arm_with(env)
    q1 = tap(env, "1:r10")
    kind, text, rows = q1.edits[0]
    assert kind == "text"
    assert text == "60kg \u00d7 10 \u00b7 RIR?"
    assert labels(rows)[0] == ["0", "1", "2", "3"]
    assert rows[-1] == [(rows[-1][0][0], "idrill:%s:back" % ARM_ID)]
    q2 = tap(env, "2:2")
    assert env["fired"] == [[CMD_BIN, "sess-1955", "--weight", "60",
                             "--count", "10", "--grade", "2"]]
    assert q2.edits[-1][1] == "60kg \u00d7 10 @ 2"
    assert not (env["dir"] / ARM_ID).exists()


# --- 3 taps: alt weight self-loop, then count, then grade --------------------

def test_three_tap_path_alt_weight_reselects(env):
    arm_with(env)
    q1 = tap(env, "1:57.5")
    kind, text, rows = q1.edits[0]
    assert (kind, text) == ("text", "Set 2 \u00b7 57.5kg")
    assert labels(rows) == [["60kg", DOT + "57.5kg"], ["8", "10", "12"]]
    arm = json.loads((env["dir"] / ARM_ID).read_text())
    assert arm["_nav"] == [["1", "57.5"]]
    q2 = tap(env, "1:r12")
    assert q2.edits[0][1] == "57.5kg \u00d7 12 \u00b7 RIR?"
    tap(env, "2:1")
    assert env["fired"] == [[CMD_BIN, "sess-1955", "--weight", "57.5",
                             "--count", "12", "--grade", "1"]]


def test_tapping_selected_value_is_noop(env):
    arm_with(env)
    before = (env["dir"] / ARM_ID).read_text()
    q = tap(env, "1:60")                     # prescribed, preselected
    assert q.edits == []
    assert (env["dir"] / ARM_ID).read_text() == before
    tap(env, "1:57.5")
    mid = (env["dir"] / ARM_ID).read_text()
    q = tap(env, "1:57.5")                   # now selected -> no-op
    assert q.edits == []
    assert (env["dir"] / ARM_ID).read_text() == mid
    q = tap(env, "1:60")                     # switch back -> re-render
    assert labels(q.edits[0][2])[0] == [DOT + "60kg", "57.5kg"]


def test_back_from_grade_step_restores_selection(env):
    arm_with(env)
    tap(env, "1:57.5")
    tap(env, "1:r8")
    q = tap(env, "back")
    kind, text, rows = q.edits[0]
    assert text == "Set 2 \u00b7 57.5kg"
    assert labels(rows) == [["60kg", DOT + "57.5kg"], ["8", "10", "12"]]


def test_count_selected_after_back_still_advances(env):
    """No-op applies only to self-loop targets: a selected value whose goto
    leaves the step still advances."""
    arm_with(env)
    tap(env, "1:r10")
    tap(env, "back")
    tap(env, "1:r10")
    tap(env, "back")
    q = tap(env, "1:r10")
    assert q.edits and q.edits[0][1] == "60kg \u00d7 10 \u00b7 RIR?"


# --- schema edges --------------------------------------------------------------

def test_goto_to_nonnumeric_falls_back_to_next_step():
    arm = dict(SET_ARM, step_goto={"1": {"60": "x"}})
    assert artifacts.goto_target(arm, "1", "60") is None
    assert artifacts.goto_target(SET_ARM, "1", "57.5") == "1"
    assert artifacts.goto_target(SET_ARM, "1", "r8") is None


def test_goto_forward_jump_without_slots(env):
    """step_goto alone (no slots) jumps past a step; no marker, text verbatim."""
    arm = {
        "step_buttons": {"1": [["a", "a"], ["b", "b"]],
                         "2": [["x", "x"]], "3": [["y", "y"]]},
        "step_text": {"1": "one", "2": "two {a}", "3": "three {a}"},
        "step_final": "3",
        "step_goto": {"1": {"b": "3"}},
        "cmd": [CMD_BIN, "{1}", "{2}"],
    }
    arm_with(env, arm)
    q = tap(env, "1:b")
    assert q.edits[0][:2] == ("text", "three {a}")
    assert labels(q.edits[0][2]) == [["y"]]
    tap(env, "3:y")
    assert env["fired"] == [[CMD_BIN, "b", "y"]]


def test_undeclared_default_ignored_and_unset_placeholder():
    arm = dict(SET_ARM, slot_default={"w": "99"})
    assert artifacts.slot_displays(arm) == {}
    rows, text = artifacts.build_step_keyboard_spec(arm, ARM_ID, "1")
    assert text == "Set 2 \u00b7 ?kg"
    assert [[l for l, _ in r] for r in rows] == [["60kg", "57.5kg"],
                                                 ["8", "10", "12"]]


def test_slot_cannot_widen_whitelist():
    arm = json.loads(json.dumps(SET_ARM))
    arm["step_slots"]["1"]["w"].append("999")
    assert "999" not in artifacts.step_slot_map(arm, "1")


def test_unset_slot_token_in_cmd_aborts_fire(env):
    arm = dict(SET_ARM, slot_default={})
    arm["cmd"] = [CMD_BIN, "{w}", "{rir}"]
    arm_with(env, arm)
    tap(env, "1:r8")
    tap(env, "2:0")
    assert env["fired"] == []


def test_undeclared_named_token_stays_verbatim(env):
    arm = dict(SET_ARM)
    arm["cmd"] = [CMD_BIN, "{w}", "{other_name}", "x{w}"]
    arm_with(env, arm)
    tap(env, "1:r8")
    tap(env, "2:3")
    assert env["fired"] == [[CMD_BIN, "60", "{other_name}", "x{w}"]]


def test_custom_selected_marker():
    arm = dict(SET_ARM, selected_marker="> ")
    rows, _ = artifacts.build_step_keyboard_spec(arm, ARM_ID, "1")
    assert rows[0][0][0] == "> 60kg"
