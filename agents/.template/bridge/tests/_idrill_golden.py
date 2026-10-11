"""DGN-1955 golden harness: drive EXISTING (pre-DGN-1955) arm shapes through
the idrill engine and record every observable output -- initial keyboard
spec, each in-place edit (text + markup), fired argv, confirmation text.

tests/golden/dgn1955_existing_arms.json was recorded from this harness on the
engine BEFORE step_goto / step_slots existed; the golden test asserts the
current engine reproduces it byte for byte. Regenerate only on a deliberate
contract change: python -m bridge.tests._idrill_golden > <that file>.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import bridge.bot as bot_mod
from bridge import artifacts
from bridge.bot import TelegramBot

GOLDEN_PATH = Path(__file__).parent / "golden" / "dgn1955_existing_arms.json"

CMD_BIN = "/opt/kit/bin/record-entry"
ARM_ID = "abcd1234"

# Existing arm shapes, one per contract generation (DGN-918/924/939/966).
EXISTING_ARMS = {
    "two_step_flat": {
        "cmd": [CMD_BIN, "s1", "{1}", "--x", "{2}"],
        "cmd_skip2": [CMD_BIN, "s1", "{1}"],
        "step_buttons": {
            "1": [["8", "8"], ["9", "9"], ["10", "10"], ["etc", "other"]],
            "2": [["0", "0"], ["1", "1"], ["2", "2"], ["pass", "skip"]],
        },
        "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-3]|skip)$"},
        "step_text": {"1": "Pick {1} first {x}", "2": "Then {w} second"},
        "confirm_fmt": "{1} / {2} done",
        "confirm_fmt_skip": "{1} done (skipped)",
    },
    "three_step_nav_fold": {
        "cmd": [CMD_BIN, "{1}", "{2}", "{3}"],
        "followup_cmd": [],
        "step_final": "3",
        "nav_back": True,
        "step_buttons": {
            "1": [["A", "a"], ["B", "b"]],
            "2": [[["X", "x"], ["Y", "y"]], [["Z", "z"]]],
            "3": [["1", "1"], ["2", "2"]],
        },
        "step_text": {"1": "first {a}", "3": "third"},
        "confirm_fmt": "{1}-{2}-{3}",
        "confirm_fold": {"summary": "sum {1}", "body": "body {2} {3}"},
    },
    "grid_mixed": {
        "cmd": [CMD_BIN, "{1}", "{2}"],
        "step_back_label": "<<",
        "nav_back": True,
        "step_buttons": {
            "1": [[["p", "p"], ["q", "q"]], ["r", "r"]],
            "2": [["k", "k"], ["l", "l"]],
        },
        "step_text": {"1": "grid", "2": "two"},
    },
    "hold": {
        "cmd": [CMD_BIN, "{1}", "{2}"],
        "is_hold": True,
        "hold_sec": 45,
        "step_buttons": {"1": [["30", "30"]], "2": [["ok", "ok"]]},
        "step_text": {"1": "hold"},
        "confirm_fmt": "held {sec}s",
    },
}

# Tap scripts per arm: each a list of callback suffixes after idrill:<id>:.
FLOWS = {
    "two_step_flat": [["1:9", "2:2"], ["1:10", "2:skip"], ["1:8", "back"],
                      ["1:other"]],
    "three_step_nav_fold": [["1:a", "2:z", "3:2"], ["1:b", "2:x", "back",
                                                     "2:y", "3:1"],
                            ["back"]],
    "grid_mixed": [["1:r", "back", "1:q", "2:l"]],
    "hold": [["1:30", "2:ok"]],
}


class _Q:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.events = []

    async def edit_message_text(self, text, **kw):
        self.events.append(["text", text, _markup(kw.get("reply_markup")),
                            kw.get("parse_mode")])

    async def edit_message_reply_markup(self, *, reply_markup=None, **kw):
        self.events.append(["markup", _markup(reply_markup)])


def _markup(m):
    if m is None:
        return None
    return [[[b.text, b.callback_data] for b in row]
            for row in m.inline_keyboard]


def transcript(arms=EXISTING_ARMS, flows=FLOWS):
    out = {}
    for name, arm in arms.items():
        rec = {"specs": {}, "flows": []}
        for step in sorted(arm["step_buttons"], key=int):
            rows, text = artifacts.build_step_keyboard_spec(
                arm, ARM_ID, step, back_label="BACK")
            rec["specs"][step] = [
                [list(b) for b in r] for r in rows] if rows else None, text
        for flow in flows.get(name, []):
            rec["flows"].append(_run_flow(arm, flow))
        out[name] = rec
    return out


def _run_flow(arm, flow):
    events = []
    fired = []

    def fake_run(argv, **kw):
        fired.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "files" / "program" / ".idrill-arm"
        d.mkdir(parents=True)
        (d / ARM_ID).write_text(json.dumps(arm), encoding="utf-8")
        bot = TelegramBot.__new__(TelegramBot)
        with patch.object(bot_mod, "PROJECT_ROOT", root), \
                patch.object(bot_mod.subprocess, "run", side_effect=fake_run):
            initial = artifacts.initial_keyboard_json(root, ARM_ID)
            for tap in flow:
                q = _Q("idrill:%s:%s" % (ARM_ID, tap))
                asyncio.run(bot._handle_idrill_callback(q, q.data))
                events.append([tap, q.events])
            left = (d / ARM_ID).exists()
    return {"initial": initial, "events": events, "fired": fired,
            "arm_left": left}


if __name__ == "__main__":
    json.dump(transcript(), sys.stdout, ensure_ascii=False, indent=1,
              sort_keys=True)
    sys.stdout.write("\n")
