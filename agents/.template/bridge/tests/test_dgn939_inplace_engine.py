"""DGN-939: in-place callback drilldown engine -- N-step navigation, « Back,
N-token argv substitution, and the expandable-blockquote confirmation fold.

Builds on the DGN-918/924 idrill primitive (2-step). This suite proves the
N-step generalization and the item6 fold render, deterministically (model turn
0), with a mock Telegram query capturing editMessageText / editMessageReplyMarkup.

Covered mechanisms + edge cases (self-grill):

  N-step navigation
    (n1) 3-step arm: step1 tap swaps to step2 keyboard (markup edit).
    (n2) step2 tap swaps to step3 keyboard.
    (n3) step3 (final) tap fires cmd with {1}{2}{3} all substituted.
    (n4) nav stack accumulates (step, value) captures in order.
    (n5) step_final defaults to "2" for a 2-step arm (backward-compat firing).

  « Back navigation
    (b1) opt-in: nav_back:true appends a Back row on non-first steps.
    (b2) opt-out (default): no Back row (DGN-918 byte-identical keyboard).
    (b3) Back tap pops the last capture and re-renders the prior step.
    (b4) Back at root (empty nav) -> IDRILL_NAV_ROOT, no crash (underflow).
    (b5) Back is never rendered on the first step (nothing to go back to).

  N-token argv substitution
    (t1) {1}{2}{3} substituted from the ordered nav captures.
    (t2) a token referencing a non-captured index -> no fire (malformed).
    (t3) every capture is whitelisted against its own step declaration.

  item6 expandable-blockquote confirmation fold
    (f1) confirm_fold declared -> headline + fold:: block, HTML-rendered edit.
    (f2) no confirm_fold -> plain-text edit (DGN-918/924 path, no HTML).
    (f3) fold summary/body carry {1}/{2} tokens, substituted.
    (f4) confirm_fold with empty summary -> no fold appended.
    (f5) fold render produces an expandable blockquote in the HTML.

  robustness
    (r1) double-tap on the final step: second sees consumed arm -> EXPIRED.
    (r2) edit failure on any path is swallowed (fail-soft, no raise).
    (r3) a step marked non-final but with no successor -> IDRILL_ERROR.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import bridge.bot as bot_mod
from bridge.bot import TelegramBot
from bridge import messages


def run(coro):
    return asyncio.run(coro)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.message.reply_markup = "EXISTING-MARKUP"
        self.text_edits = []       # text arg of edit_message_text
        self.text_edit_kwargs = [] # kwargs (parse_mode etc.)
        self.markup_edits = []     # reply_markup of edit_message_reply_markup

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append(text)
        self.text_edit_kwargs.append(kwargs)

    async def edit_message_reply_markup(self, *, reply_markup=None, **kwargs):
        self.markup_edits.append(reply_markup)


class RaisingQuery(FakeQuery):
    """Every edit raises -- proves the handler swallows edit failures."""

    async def edit_message_text(self, text, **kwargs):
        raise RuntimeError("edit boom")

    async def edit_message_reply_markup(self, *, reply_markup=None, **kwargs):
        raise RuntimeError("markup boom")


@pytest.fixture
def bot():
    return TelegramBot.__new__(TelegramBot)


@pytest.fixture
def arm_dir(tmp_path):
    cb = tmp_path / "files" / "program" / ".idrill-arm"
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield cb


CMD_BIN = "/opt/lifekit/bin/record-entry"

# A 3-step arm: reps -> RIR -> tempo (arbitrary; the bridge is domain-agnostic).
THREE_STEP_ARM = {
    "session_id": "sess-3s",
    "step_final": "3",
    "nav_back": True,
    "cmd": [CMD_BIN, "sess-3s", "{1}", "--rir", "{2}", "--tempo", "{3}"],
    "cmd_skip2": [CMD_BIN, "sess-3s", "{1}"],
    "step_buttons": {
        "1": [["8", "8"], ["9", "9"], ["10", "10"]],
        "2": [["0", "0"], ["1", "1"], ["2", "2"]],
        "3": [["slow", "slow"], ["fast", "fast"], ["skip", "skip"]],
    },
    "step_validate": {
        "1": r"^[0-9]{1,4}$",
        "2": r"^([0-9]|skip)$",
        "3": r"^(slow|fast|skip)$",
    },
    "step_text": {"1": "몇 회?", "2": "RIR?", "3": "템포?"},
}


def write_arm(arm_dir, content, arm_id="abcd1234", extra=None, omit=None):
    c = dict(content)
    if extra:
        c.update(extra)
    for k in (omit or ()):
        c.pop(k, None)
    (arm_dir / arm_id).write_text(json.dumps(c), encoding="utf-8")
    return arm_dir / arm_id


def read_arm(arm_dir, arm_id="abcd1234"):
    return json.loads((arm_dir / arm_id).read_text())


# ---------------------------------------------------------------------------
# N-step navigation
# ---------------------------------------------------------------------------

def test_n1_step1_swaps_to_step2(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM)
    q = FakeQuery("idrill:abcd1234:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    # step_text[2] declared -> text+markup swap
    assert q.text_edits == ["RIR?"]
    rows = q.text_edit_kwargs[0]["reply_markup"].inline_keyboard
    assert [b.text for b in rows[0]] == ["0", "1", "2"]
    # nav_back true -> a Back row appended
    assert rows[-1][0].callback_data == "idrill:abcd1234:back"


def test_n2_step2_swaps_to_step3(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM, extra={"_nav": [["1", "8"]]})
    q = FakeQuery("idrill:abcd1234:2:1")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == ["템포?"]
    rows = q.text_edit_kwargs[0]["reply_markup"].inline_keyboard
    assert [b.text for b in rows[0]] == ["slow", "fast", "skip"]


def test_n3_step3_final_fires_all_three_tokens(bot, arm_dir, monkeypatch):
    write_arm(arm_dir, THREE_STEP_ARM, extra={"_nav": [["1", "8"], ["2", "1"]]})
    q = FakeQuery("idrill:abcd1234:3:slow")

    captured = []
    fake = MagicMock(); fake.returncode = 0; fake.stdout = "ok"; fake.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **k: captured.append(argv) or fake)
    run(bot._handle_idrill_callback(q, q.data))

    assert captured[0] == [
        CMD_BIN, "sess-3s", "8", "--rir", "1", "--tempo", "slow",
    ]


def test_n4_nav_stack_accumulates_in_order(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM)
    run(bot._handle_idrill_callback(FakeQuery("idrill:abcd1234:1:9"),
                                    "idrill:abcd1234:1:9"))
    assert read_arm(arm_dir)["_nav"] == [["1", "9"]]
    run(bot._handle_idrill_callback(FakeQuery("idrill:abcd1234:2:2"),
                                    "idrill:abcd1234:2:2"))
    assert read_arm(arm_dir)["_nav"] == [["1", "9"], ["2", "2"]]


def test_n5_two_step_arm_final_defaults_to_step2(bot, arm_dir, monkeypatch):
    """A 2-step arm (no step_final) fires at step 2 -- DGN-918 contract kept."""
    two_step = {
        "session_id": "s2",
        "cmd": [CMD_BIN, "{1}", "--rir", "{2}"],
        "cmd_skip2": [CMD_BIN, "{1}"],
        "step_buttons": {"1": [["8", "8"]], "2": [["0", "0"], ["skip", "skip"]]},
        "step_validate": {"1": r"^[0-9]+$", "2": r"^([0-9]|skip)$"},
    }
    write_arm(arm_dir, two_step, extra={"_pending_step1": "8", "_nav": [["1", "8"]]})
    q = FakeQuery("idrill:abcd1234:2:0")
    captured = []
    fake = MagicMock(); fake.returncode = 0; fake.stdout = "ok"; fake.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **k: captured.append(argv) or fake)
    run(bot._handle_idrill_callback(q, q.data))
    assert captured[0] == [CMD_BIN, "8", "--rir", "0"]


# ---------------------------------------------------------------------------
# « Back navigation
# ---------------------------------------------------------------------------

def test_b1_back_row_opt_in(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM)
    q = FakeQuery("idrill:abcd1234:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    rows = q.text_edit_kwargs[0]["reply_markup"].inline_keyboard
    assert any(
        b.callback_data == "idrill:abcd1234:back" for row in rows for b in row
    )


def test_b2_back_row_opt_out_default(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM, omit=("nav_back",))
    q = FakeQuery("idrill:abcd1234:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    rows = q.text_edit_kwargs[0]["reply_markup"].inline_keyboard
    assert all(
        b.callback_data != "idrill:abcd1234:back" for row in rows for b in row
    )


def test_b3_back_pops_and_rerenders_prior_step(bot, arm_dir):
    # user is on step3 (nav has step1,step2 captured)
    write_arm(arm_dir, THREE_STEP_ARM, extra={"_nav": [["1", "8"], ["2", "1"]]})
    q = FakeQuery("idrill:abcd1234:back")
    run(bot._handle_idrill_callback(q, q.data))
    # popped step2 -> re-render step2's prompt + keyboard
    assert q.text_edits == ["RIR?"]
    assert read_arm(arm_dir)["_nav"] == [["1", "8"]]


def test_b4_back_at_root_underflow(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM, extra={"_nav": []})
    q = FakeQuery("idrill:abcd1234:back")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_NAV_ROOT]
    assert len(q.markup_edits) == 0


def test_b4b_back_missing_arm_expired(bot, arm_dir):
    q = FakeQuery("idrill:abcd1234:back")  # no arm file
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


def test_b5_no_back_on_first_step(bot, arm_dir):
    # Even with nav_back true, the first step must not carry a Back row.
    write_arm(arm_dir, THREE_STEP_ARM)
    msg = MagicMock()
    reply = []
    async def _reply(text, reply_markup=None, **k):
        reply.append((text, reply_markup))
    msg.reply_text = _reply
    msg.chat.id = 1
    run(bot._idrill_send_initial_keyboard(msg, "abcd1234"))
    rows = reply[0][1].inline_keyboard
    assert all(
        b.callback_data != "idrill:abcd1234:back" for row in rows for b in row
    )


# ---------------------------------------------------------------------------
# N-token argv substitution
# ---------------------------------------------------------------------------

def test_t2_token_index_out_of_range_no_fire(bot, arm_dir, monkeypatch):
    """A {3} token on a flow that captured only 2 values -> no fire."""
    arm = dict(THREE_STEP_ARM)
    arm["step_final"] = "2"  # final at step2 but cmd declares {3}
    arm["cmd"] = [CMD_BIN, "{1}", "{2}", "{3}"]
    write_arm(arm_dir, arm, extra={"_nav": [["1", "8"]]})
    q = FakeQuery("idrill:abcd1234:2:1")
    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))
    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


def test_t3_undeclared_capture_value_rejected(bot, arm_dir, monkeypatch):
    """A captured value outside its step declaration aborts the final fire."""
    # Corrupt the nav to hold an undeclared step1 value.
    write_arm(arm_dir, THREE_STEP_ARM,
              extra={"_nav": [["1", "999"], ["2", "1"]]})
    # 999 fails step_validate[1] ^[0-9]{1,4}$? it passes (3 digits). Use letters.
    write_arm(arm_dir, THREE_STEP_ARM,
              extra={"_nav": [["1", "zzz"], ["2", "1"]]})
    q = FakeQuery("idrill:abcd1234:3:slow")
    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))
    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


# ---------------------------------------------------------------------------
# item6 expandable-blockquote confirmation fold
# ---------------------------------------------------------------------------

FOLD_ARM = {
    "session_id": "sf",
    "cmd": [CMD_BIN, "{1}", "--rir", "{2}"],
    "cmd_skip2": [CMD_BIN, "{1}"],
    "step_buttons": {"1": [["8", "8"]], "2": [["0", "0"], ["skip", "skip"]]},
    "step_validate": {"1": r"^[0-9]+$", "2": r"^([0-9]|skip)$"},
    "confirm_fmt": "✅ {1}회 · RIR {2}",
    "confirm_fold": {
        "summary": "왜 이 값?",
        "body": "직전 세트 {1}회, RIR {2} 기준 계산.",
    },
}


def _fire_final(bot, arm_dir, monkeypatch, arm, tap="idrill:abcd1234:2:0"):
    write_arm(arm_dir, arm, extra={"_nav": [["1", "8"]], "_pending_step1": "8"})
    q = FakeQuery(tap)
    fake = MagicMock(); fake.returncode = 0; fake.stdout = "ok"; fake.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake)
    run(bot._handle_idrill_callback(q, q.data))
    return q


def test_f1_confirm_fold_renders_html_expandable(bot, arm_dir, monkeypatch):
    q = _fire_final(bot, arm_dir, monkeypatch, FOLD_ARM)
    assert len(q.text_edits) == 1
    html = q.text_edits[0]
    # Headline present + expandable blockquote rendered
    assert "8회" in html
    assert "<blockquote expandable>" in html
    assert q.text_edit_kwargs[0].get("parse_mode") == "HTML"


def test_f2_no_fold_plain_text(bot, arm_dir, monkeypatch):
    arm = dict(FOLD_ARM); arm.pop("confirm_fold")
    q = _fire_final(bot, arm_dir, monkeypatch, arm)
    assert q.text_edits == ["✅ 8회 · RIR 0"]
    # plain text edit -> no HTML parse_mode
    assert q.text_edit_kwargs[0].get("parse_mode") is None


def test_f3_fold_tokens_substituted(bot, arm_dir, monkeypatch):
    q = _fire_final(bot, arm_dir, monkeypatch, FOLD_ARM)
    html = q.text_edits[0]
    assert "직전 세트 8회, RIR 0 기준" in html


def test_f4_empty_fold_summary_no_block(bot, arm_dir, monkeypatch):
    arm = dict(FOLD_ARM)
    arm["confirm_fold"] = {"summary": "   ", "body": "x"}
    q = _fire_final(bot, arm_dir, monkeypatch, arm)
    assert "<blockquote" not in q.text_edits[0]


def test_f5_confirm_fold_helper_produces_typed_block():
    arm = {"confirm_fold": {"summary": "S {1}", "body": "B {2}"}}
    out = TelegramBot._idrill_confirm_fold(arm, "8", "0")
    assert out.startswith("fold::")
    assert "S 8" in out and "B 0" in out
    assert out.rstrip().endswith("fold::end")


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------

def test_r1_double_tap_final_second_expired(bot, arm_dir, monkeypatch):
    write_arm(arm_dir, FOLD_ARM,
              extra={"_nav": [["1", "8"]], "_pending_step1": "8"})
    fake = MagicMock(); fake.returncode = 0; fake.stdout = "ok"; fake.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake)
    q1 = FakeQuery("idrill:abcd1234:2:0")
    q2 = FakeQuery("idrill:abcd1234:2:0")
    run(bot._handle_idrill_callback(q1, q1.data))  # fires + consumes
    run(bot._handle_idrill_callback(q2, q2.data))  # arm gone -> expired
    assert q1.text_edits and q1.text_edits[0] != messages.IDRILL_ARM_EXPIRED
    assert q2.text_edits == [messages.IDRILL_ARM_EXPIRED]


def test_r2_edit_failure_swallowed_on_advance(bot, arm_dir):
    write_arm(arm_dir, THREE_STEP_ARM)
    q = RaisingQuery("idrill:abcd1234:1:8")
    # Must not raise even though every edit throws.
    run(bot._handle_idrill_callback(q, q.data))
    # nav still advanced (the write happened before the edit)
    assert read_arm(arm_dir)["_nav"] == [["1", "8"]]


def test_r2b_edit_failure_swallowed_on_final(bot, arm_dir, monkeypatch):
    write_arm(arm_dir, FOLD_ARM,
              extra={"_nav": [["1", "8"]], "_pending_step1": "8"})
    fake = MagicMock(); fake.returncode = 0; fake.stdout = "ok"; fake.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake)
    q = RaisingQuery("idrill:abcd1234:2:0")
    run(bot._handle_idrill_callback(q, q.data))  # must not raise
    # arm consumed despite edit failure
    assert not (arm_dir / "abcd1234").exists()


def test_r3_nonfinal_step_no_successor_errors(bot, arm_dir):
    """A step declared non-final but with no next step -> IDRILL_ERROR."""
    arm = {
        "session_id": "x",
        "step_final": "5",  # final is 5 but only step 1 declared
        "cmd": [CMD_BIN, "{1}"],
        "step_buttons": {"1": [["8", "8"]]},
        "step_validate": {"1": r"^[0-9]+$"},
    }
    write_arm(arm_dir, arm)
    q = FakeQuery("idrill:abcd1234:1:8")  # step1 non-final, no step2
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ERROR]
