"""DGN-918: idrill: in-place drilldown capture callback primitive.

Contract v2 (declared-argv executor) + de-workout rename: the bridge knows
no domain verb and no domain step meaning. The arm file declares `cmd` /
`cmd_skip2` argv; the bridge substitutes only the positional "{1}" / "{2}"
placeholder elements (numeric whitelist) and executes the list verbatim
(list-form subprocess, no shell, no PATH).

Covers the safety invariants the self-grill flagged:

  (a) step1 tap -> step2 keyboard swap (edit_message_reply_markup, NOT new msg).
  (b) :1:other -> free-text prompt, arm file NOT consumed.
  (c) step2 tap -> declared `cmd` argv fired with {1}/{2} substituted.
  (d) step2 skip -> declared `cmd_skip2` argv fired instead.
  (e) arm-id file consumed after step2 tap (unlinked).
  (f) missing arm file -> fail-soft, IDRILL_ARM_EXPIRED, no crash.
  (g) arm_id path traversal (e.g. "../etc/passwd") -> rejected, IDRILL_ERROR.
  (h) concurrent double-tap on step2: second tap sees no arm file -> EXPIRED.
  (i) argv injection via consumer-declared element with spaces/quotes (list form).
  (j) cmd key absent/malformed -> fail-soft, IDRILL_ERROR, arm file consumed.
  (k) declared cmd nonzero exit -> fail-soft, IDRILL_ERROR.
  (l) callback_data byte limit: all idrill: prefixes well under 64 bytes.
  (m) confirm_fmt / confirm_fmt_skip render (Addition B, positional tokens).
  (n) missing confirm_fmt -> fallback message constants.
  (o) malformed JSON in arm file -> IDRILL_ARM_EXPIRED (missing arm = same path).
  (p) `4+` tap -> {2} substituted with normalized "4" in argv.
  (q) token substitution whitelist: non-numeric step1 value rejected, no fire.
  (r) bridge sources carry no workout-domain literal (rename forced by test).
  (s) malicious cmd argv values never reach a shell (real exec, list form).
  (t) subprocess invoked list-form without shell=True.
  (u) partial/substring tokens are NOT substituted (exact-element only).
  (v) skip variant declaring a {2} token -> rejected, no fire.
  (w) idrill: prefix is registered in the callback router (old prefix gone).
  (x) legacy workout-named tokens pass through verbatim (no substitution).
  (y) step2 tap without a prior step1 tap -> fail-soft, no fire.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bridge.bot as bot_mod
from bridge.bot import TelegramBot
from bridge import messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


class FakeQuery:
    """Minimal query mock with recorded edit calls."""

    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.message.reply_markup = "EXISTING-MARKUP"
        self.text_edits = []           # (text,) from edit_message_text
        self.markup_edits = []         # (reply_markup,) from edit_message_reply_markup

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append(text)

    async def edit_message_reply_markup(self, *, reply_markup=None, **kwargs):
        self.markup_edits.append(reply_markup)


@pytest.fixture
def bot():
    """TelegramBot instance without __init__ state."""
    return TelegramBot.__new__(TelegramBot)


@pytest.fixture
def arm_dir(tmp_path):
    """Temporary .idrill-arm directory; patches PROJECT_ROOT to tmp_path."""
    cb = tmp_path / "files" / "program" / ".idrill-arm"
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield cb


# Neutral domain verb path: the bridge must treat argv[0] as opaque.
CMD_BIN = "/opt/lifekit/bin/record-entry"

ARM_CONTENT = {
    "session_id": "sess-abc123",
    "item_name": "스쿼트",
    "slot_num": "1",
    "seq": "3",
    # Contract v2: declared argv with positional placeholder tokens.
    "cmd": [CMD_BIN, "sess-abc123", "스쿼트", "1", "3", "{1}",
            "--weight", "80", "--intensity", "{2}"],
    "cmd_skip2": [CMD_BIN, "sess-abc123", "스쿼트", "1", "3", "{1}",
                  "--weight", "80"],
    # Contract v3: step keyboards + validate regexes are declared by the arm
    # file (the bridge holds no hardcoded button set). This fixture keeps the
    # v1.38.0 workout values, but now as arm DECLARATION -- the bridge is
    # domain-agnostic and reads them from here.
    "step_buttons": {
        "1": [["8", "8"], ["9", "9"], ["10", "10"], ["기타", "other"]],
        "2": [["0", "0"], ["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"],
              ["건너뜀", "skip"]],
    },
    "step_validate": {
        "1": r"^[0-9]{1,4}$",
        "2": r"^([0-3]|4|skip)$",
    },
}


def write_arm(arm_dir, arm_id="abcd1234", extra=None, omit=None):
    content = dict(ARM_CONTENT)
    if extra:
        content.update(extra)
    for key in (omit or ()):
        content.pop(key, None)
    path = arm_dir / arm_id
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) step1 tap -> step2 keyboard swap
# ---------------------------------------------------------------------------

def test_step1_tap_swaps_to_step2_keyboard(bot, arm_dir):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id)
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))

    # edit_message_reply_markup called, NOT edit_message_text
    assert len(q.markup_edits) == 1
    assert len(q.text_edits) == 0

    kb = q.markup_edits[0]
    # Keyboard is built from the arm's declared step_buttons["2"] (contract v3):
    # one row of [0 1 2 3 4 건너뜀], labels + callback values from the arm file.
    assert kb is not None
    rows = kb.inline_keyboard
    assert len(rows) == 1
    step2_labels = [btn.text for btn in rows[0]]
    assert step2_labels == ["0", "1", "2", "3", "4", "건너뜀"]

    # callback_data for each step2 button must start with idrill:<arm_id>:2:
    for btn in rows[0]:
        assert btn.callback_data.startswith(f"idrill:{arm_id}:2:")
    # The skip button carries the declared "skip" value.
    assert rows[0][-1].callback_data == f"idrill:{arm_id}:2:skip"


def test_step1_tap_stores_pending_value(bot, arm_dir):
    """After step1 tap, arm file gets _pending_step1 field."""
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id)
    q = FakeQuery(f"idrill:{arm_id}:1:10")
    run(bot._handle_idrill_callback(q, q.data))

    updated = json.loads(arm_path.read_text())
    assert updated.get("_pending_step1") == "10"


# ---------------------------------------------------------------------------
# (b) :1:other -> free-text prompt, arm NOT consumed
# ---------------------------------------------------------------------------

def test_step1_other_sends_prompt_and_leaves_arm(bot, arm_dir):
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id)
    q = FakeQuery(f"idrill:{arm_id}:1:other")
    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == [messages.IDRILL_OTHER_PROMPT]
    assert len(q.markup_edits) == 0
    # Arm file NOT deleted
    assert arm_path.exists()


# ---------------------------------------------------------------------------
# (c) step2 tap -> declared `cmd` argv fired with tokens substituted
# ---------------------------------------------------------------------------

def test_step2_tap_fires_declared_cmd_with_substitution(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"})
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""

    def fake_run(argv, **kwargs):
        argv_captured.append(argv)
        return fake_proc

    monkeypatch.setattr(bot_mod.subprocess, "run", fake_run)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(argv_captured) == 1
    # Exactly the declared argv, tokens substituted, nothing appended.
    assert argv_captured[0] == [
        CMD_BIN, "sess-abc123", "스쿼트", "1", "3", "8",
        "--weight", "80", "--intensity", "2",
    ]


# ---------------------------------------------------------------------------
# (d) step2 skip -> declared `cmd_skip2` argv fired
# ---------------------------------------------------------------------------

def test_step2_skip_uses_cmd_skip2(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "6"})
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert argv_captured[0] == [
        CMD_BIN, "sess-abc123", "스쿼트", "1", "3", "6", "--weight", "80",
    ]
    assert "--intensity" not in argv_captured[0]


# ---------------------------------------------------------------------------
# (e) arm-id file consumed after step2 tap
# ---------------------------------------------------------------------------

def test_arm_file_consumed_after_step2(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id, extra={"_pending_step1": "5"})
    q = FakeQuery(f"idrill:{arm_id}:2:1")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))
    assert not arm_path.exists(), "arm file must be deleted after step2 tap"


# ---------------------------------------------------------------------------
# (f) missing arm file -> fail-soft
# ---------------------------------------------------------------------------

def test_missing_arm_file_step1_tap(bot, arm_dir):
    """Step1 tap on expired/missing arm -> IDRILL_ARM_EXPIRED, no crash."""
    q = FakeQuery("idrill:abcd1234:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


def test_missing_arm_file_step2_tap(bot, arm_dir):
    """Step2 tap on missing arm -> IDRILL_ARM_EXPIRED, no crash."""
    q = FakeQuery("idrill:abcd1234:2:2")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


# ---------------------------------------------------------------------------
# (g) arm_id path traversal -> rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "abcd1234/x",
    "../../x",
    "ABCD1234",          # uppercase: not matching [0-9a-f]{8}
    "abcd123",           # 7 chars
    "abcd12345",         # 9 chars
    "abcd123g",          # 'g' is not hex
    "",
])
def test_traversal_arm_id_rejected(bot, arm_dir, bad_id):
    q = FakeQuery(f"idrill:{bad_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ERROR]
    assert len(q.markup_edits) == 0


# ---------------------------------------------------------------------------
# (h) concurrent double-tap on step2: second sees consumed file
# ---------------------------------------------------------------------------

def test_double_tap_step2_second_is_expired(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"})

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    q1 = FakeQuery(f"idrill:{arm_id}:2:1")
    q2 = FakeQuery(f"idrill:{arm_id}:2:1")

    run(bot._handle_idrill_callback(q1, q1.data))  # consumes file
    run(bot._handle_idrill_callback(q2, q2.data))  # file gone -> expired

    # First tap: should have a confirmation text
    assert len(q1.text_edits) == 1
    assert q1.text_edits[0] != messages.IDRILL_ARM_EXPIRED

    # Second tap: arm file gone -> expired
    assert q2.text_edits == [messages.IDRILL_ARM_EXPIRED]


# ---------------------------------------------------------------------------
# (i) argv injection: declared element with spaces/quotes uses list form
# ---------------------------------------------------------------------------

def test_declared_element_with_spaces_no_injection(bot, arm_dir, monkeypatch):
    """Declared argv element with spaces/quotes stays a single argv element."""
    arm_id = "abcd1234"
    hostile = "bench press 'heavy'; rm -rf /"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "5",
        "cmd": [CMD_BIN, "sess-abc123", hostile, "1", "3", "{1}",
                "--weight", "80", "--intensity", "{2}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:0")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    argv = argv_captured[0]
    # The hostile string is a single element in the list (not shell-split),
    # passed through verbatim -- no re-parsing.
    idx = argv.index(hostile)
    assert idx == 2  # exactly where the file declared it


# ---------------------------------------------------------------------------
# (j) cmd key absent/malformed -> fail-soft, IDRILL_ERROR, no fire
# ---------------------------------------------------------------------------

def test_cmd_key_absent_fails_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"},
                         omit=("cmd", "cmd_skip2"))
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == []  # nothing executed
    assert q.text_edits == [messages.IDRILL_ERROR]
    # Arm file still consumed even on fire failure
    assert not arm_path.exists()


def test_skip_variant_missing_cmd_skip2_fails_soft(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"},
              omit=("cmd_skip2",))
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


@pytest.mark.parametrize("bad_cmd", [
    "not-a-list at all",                       # string, not list
    [],                                        # empty list
    [["nested"], "x"],                         # non-string element
    [42, "x"],                                 # int element
])
def test_malformed_cmd_rejected(bot, arm_dir, monkeypatch, bad_cmd):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8", "cmd": bad_cmd})
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


# ---------------------------------------------------------------------------
# (k) declared cmd nonzero exit -> fail-soft
# ---------------------------------------------------------------------------

def test_declared_cmd_nonzero_exit(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"})
    q = FakeQuery(f"idrill:{arm_id}:2:1")

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = ""
    fake_proc.stderr = "some error"
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == [messages.IDRILL_ERROR]
    assert not arm_path.exists()  # consumed regardless


# ---------------------------------------------------------------------------
# (l) callback_data byte limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cb_data", [
    "idrill:abcd1234:1:8",
    "idrill:abcd1234:1:12",
    "idrill:abcd1234:1:other",
    "idrill:abcd1234:2:0",
    "idrill:abcd1234:2:4+",
    "idrill:abcd1234:2:skip",
])
def test_callback_data_under_64_bytes(cb_data):
    assert len(cb_data.encode("utf-8")) <= 64, (
        f"callback_data too long ({len(cb_data.encode())} bytes): {cb_data!r}"
    )


# ---------------------------------------------------------------------------
# (p) declared multi-char value (e.g. "4+") passes through argv VERBATIM
#     -- contract v3 removed the hardcoded 4+ -> 4 normalization; the bridge
#     substitutes the declared value as-is.
# ---------------------------------------------------------------------------

def test_declared_multichar_value_passes_through_verbatim(bot, arm_dir, monkeypatch):
    """A declared step2 value like '4+' is substituted into argv verbatim
    (no bridge-side normalization -- the bridge is domain-agnostic)."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "10",
        "step_buttons": {
            "1": [["10", "10"]],
            "2": [["0", "0"], ["4+", "4+"], ["건너뜀", "skip"]],
        },
        "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-4]|4\+|skip)$"},
    })
    q = FakeQuery(f"idrill:{arm_id}:2:4+")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    argv = argv_captured[0]
    step2_idx = argv.index("--intensity")
    assert argv[step2_idx + 1] == "4+", "declared '4+' must pass through verbatim"


# ---------------------------------------------------------------------------
# (m) confirm_fmt / confirm_fmt_skip render (Addition B, positional tokens)
# ---------------------------------------------------------------------------

def test_confirm_fmt_renders_with_positional_tokens(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "8",
        "confirm_fmt": "✅ {1}회 · 강도 {2} 기록",
    })
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == ["✅ 8회 · 강도 2 기록"]


def test_confirm_fmt_skip_used_for_step2_skip(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "6",
        "confirm_fmt_skip": "✅ {1}회 기록",
    })
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == ["✅ 6회 기록"]


def test_confirm_fmt_4plus_step2_as_string(bot, arm_dir, monkeypatch):
    """4+ must be passed as a literal string in the confirm format."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "10",
        "confirm_fmt": "✅ {1}회 · 강도 {2} 기록",
        "step_buttons": {
            "1": [["10", "10"]],
            "2": [["0", "0"], ["4+", "4+"], ["건너뜀", "skip"]],
        },
        "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-4]|4\+|skip)$"},
    })
    q = FakeQuery(f"idrill:{arm_id}:2:4+")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == ["✅ 10회 · 강도 4+ 기록"]


# ---------------------------------------------------------------------------
# (n) missing confirm_fmt -> fallback messages
# ---------------------------------------------------------------------------

def test_missing_confirm_fmt_falls_back(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"})
    q = FakeQuery(f"idrill:{arm_id}:2:1")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    # Should use the fallback message constant (contains step1 value)
    assert len(q.text_edits) == 1
    assert "8" in q.text_edits[0]  # step1 value in fallback text


def test_missing_confirm_fmt_skip_falls_back(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "6"})
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert len(q.text_edits) == 1
    text = q.text_edits[0]
    assert "6" in text


# ---------------------------------------------------------------------------
# (o) malformed JSON in arm file
# ---------------------------------------------------------------------------

def test_malformed_arm_json_step1(bot, arm_dir):
    arm_id = "abcd1234"
    (arm_dir / arm_id).write_text("{ bad json }", encoding="utf-8")
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


def test_malformed_arm_json_step2(bot, arm_dir):
    arm_id = "abcd1234"
    (arm_dir / arm_id).write_text("{ bad json }", encoding="utf-8")
    q = FakeQuery(f"idrill:{arm_id}:2:1")
    run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits == [messages.IDRILL_ARM_EXPIRED]


# ---------------------------------------------------------------------------
# (q) token substitution whitelist: non-numeric step1 rejected, no fire
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_val1", [
    "abc",                 # non-numeric
    "8; rm -rf /",         # shell injection attempt
    "-1",                  # negative
    "99999",               # overflow (length bound: 4 digits max)
    "{weight}",            # format-string smuggling
    "8.5",                 # float
    "",                    # empty
    "٨",                   # unicode digit (int() accepts it; regex must not)
])
def test_nonnumeric_step1_rejected_no_fire(bot, arm_dir, monkeypatch, bad_val1):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": bad_val1})
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == [], f"step1 value {bad_val1!r} must never reach subprocess"
    assert q.text_edits == [messages.IDRILL_ERROR]


# ---------------------------------------------------------------------------
# (r) bridge sources carry no workout-domain literal (rename forced by test)
# ---------------------------------------------------------------------------

_FORBIDDEN_LITERALS = ("wset", "reps", "rir", "set-add")

_SOURCE_FILES = [
    Path(bot_mod.__file__),
    Path(bot_mod.__file__).parent / "messages.py",
    Path(bot_mod.__file__).parent / "i18n" / "en.py",
    Path(bot_mod.__file__).parent / "i18n" / "ko.py",
]


@pytest.mark.parametrize("src_path", _SOURCE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("literal", _FORBIDDEN_LITERALS)
def test_bridge_sources_have_no_workout_literal(src_path, literal):
    src = src_path.read_text(encoding="utf-8").lower()
    assert literal not in src, (
        f"bridge must be domain agnostic: {literal!r} literal found in "
        f"{src_path.name} (strings/functions/keys included)"
    )


# ---------------------------------------------------------------------------
# (s) malicious cmd argv values never reach a shell (real exec, list form)
# ---------------------------------------------------------------------------

def test_malicious_argv_not_shell_interpreted(bot, arm_dir, tmp_path):
    """Real subprocess: shell metacharacters in declared argv stay literal."""
    marker = tmp_path / "pwned"
    out = tmp_path / "out.txt"
    cmd = [
        sys.executable, "-c",
        "import sys; open(sys.argv[1], 'w').write('|'.join(sys.argv[2:]))",
        str(out),
        f"$(touch {marker})",
        f"; touch {marker}",
        "{1}",
    ]
    write_arm(arm_dir, "abcd1234", extra={"_pending_step1": "7", "cmd": cmd})
    q = FakeQuery("idrill:abcd1234:2:2")

    run(bot._handle_idrill_callback(q, q.data))

    assert not marker.exists(), "shell metacharacters must not execute"
    parts = out.read_text(encoding="utf-8").split("|")
    assert parts[0] == f"$(touch {marker})"   # literal, not expanded
    assert parts[1] == f"; touch {marker}"    # literal, not split
    assert parts[2] == "7"                    # {1} substituted


# ---------------------------------------------------------------------------
# (t) subprocess invoked list-form without shell=True
# ---------------------------------------------------------------------------

def test_subprocess_list_form_no_shell_kwarg(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"})
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    calls = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return fake_proc

    monkeypatch.setattr(bot_mod.subprocess, "run", fake_run)

    run(bot._handle_idrill_callback(q, q.data))

    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert not kwargs.get("shell"), "shell=True is forbidden"
    assert "cwd" not in kwargs, "cwd must be untouched"
    assert "env" not in kwargs, "env/PATH must be untouched"


# ---------------------------------------------------------------------------
# (u) partial/substring tokens are NOT substituted (exact-element only)
# ---------------------------------------------------------------------------

def test_partial_token_not_substituted(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "9",
        "cmd": [CMD_BIN, "x{1}y", "{1}", "--intensity", "{2}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:3")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert argv_captured[0] == [CMD_BIN, "x{1}y", "9", "--intensity", "3"]


# ---------------------------------------------------------------------------
# (v) skip variant declaring a {2} token -> rejected, no fire
# ---------------------------------------------------------------------------

def test_skip_variant_with_step2_token_rejected(bot, arm_dir, monkeypatch):
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "6",
        "cmd_skip2": [CMD_BIN, "{1}", "--intensity", "{2}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == [], "unsubstitutable {2} token must abort the fire"
    assert q.text_edits == [messages.IDRILL_ERROR]


# ---------------------------------------------------------------------------
# (w) idrill: prefix is registered in the callback router (old prefix gone)
# ---------------------------------------------------------------------------

def test_idrill_prefix_registered_in_router():
    src = Path(bot_mod.__file__).read_text(encoding="utf-8")
    assert 'data.startswith("idrill:")' in src, (
        "callback router must dispatch the idrill: prefix"
    )
    # Old prefix must be fully retired from the router (the literal scan
    # test enforces the rest of the file).
    assert '"wset:"' not in src


def test_handler_and_fire_fn_renamed():
    assert hasattr(TelegramBot, "_handle_idrill_callback")
    assert hasattr(TelegramBot, "_idrill_fire_cmd")
    assert not hasattr(TelegramBot, "_handle_wset_callback")
    assert not hasattr(TelegramBot, "_wset_fire_cmd")


# ---------------------------------------------------------------------------
# (x) legacy workout-named tokens pass through verbatim (no substitution)
# ---------------------------------------------------------------------------

def test_legacy_named_tokens_pass_through_verbatim(bot, arm_dir, monkeypatch):
    """Old-contract named tokens are opaque elements now: passed through
    verbatim, never substituted (positional {1}/{2} only)."""
    arm_id = "abcd1234"
    legacy_1 = "{" + "reps" + "}"
    legacy_2 = "{" + "rir" + "}"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "8",
        "cmd": [CMD_BIN, legacy_1, legacy_2, "{1}", "{2}"],
    })
    q = FakeQuery(f"idrill:{arm_id}:2:3")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)

    run(bot._handle_idrill_callback(q, q.data))

    assert argv_captured[0] == [CMD_BIN, legacy_1, legacy_2, "8", "3"]


# ---------------------------------------------------------------------------
# (y) step2 tap without a prior step1 tap -> fail-soft, no fire
# ---------------------------------------------------------------------------

def test_step2_without_step1_fails_soft(bot, arm_dir, monkeypatch):
    """No _pending_step1 in the arm file (step2 tapped without step1):
    the placeholder value fails the numeric whitelist -> no fire, error."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id)  # no _pending_step1
    q = FakeQuery(f"idrill:{arm_id}:2:2")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda a, **k: called.append(a))

    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


# ===========================================================================
# DGN-924: shape-agnostic -- step buttons + validation read from the arm file
# declaration, NOT from bridge hardcoded constants.
# ===========================================================================

# --- (924-a) step2 keyboard is built from the arm's declared step_buttons[2] ---

def test_step2_keyboard_built_from_declaration(bot, arm_dir):
    """The step2 keyboard labels + callback values come from step_buttons[2]."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "step_buttons": {
            "1": [["8", "8"]],
            "2": [["easy", "e"], ["hard", "h"], ["done", "skip"]],
        },
    })
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))

    rows = q.markup_edits[0].inline_keyboard
    labels = [b.text for b in rows[0]]
    cbs = [b.callback_data for b in rows[0]]
    assert labels == ["easy", "hard", "done"]
    assert cbs == [
        f"idrill:{arm_id}:2:e",
        f"idrill:{arm_id}:2:h",
        f"idrill:{arm_id}:2:skip",
    ]


# --- (924-b) a value NOT declared as a button is rejected (fail-soft) ---

def test_step1_undeclared_value_rejected(bot, arm_dir):
    """A step1 tap value outside the declared buttons AND failing validate is
    rejected -- no capture, no keyboard swap."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "step_buttons": {"1": [["a", "a"], ["b", "b"]], "2": [["x", "x"]]},
    }, omit=("step_validate",))
    q = FakeQuery(f"idrill:{arm_id}:1:zzz")  # zzz not a declared step1 value
    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == [messages.IDRILL_ERROR]
    assert len(q.markup_edits) == 0
    updated = json.loads((arm_dir / arm_id).read_text())
    assert "_pending_step1" not in updated


def test_step2_undeclared_value_rejected(bot, arm_dir, monkeypatch):
    """A step2 tap value outside declaration AND failing validate: no fire."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "8",
        "step_buttons": {"1": [["8", "8"]], "2": [["x", "x"], ["건너뜀", "skip"]]},
    }, omit=("step_validate",))
    q = FakeQuery(f"idrill:{arm_id}:2:9")  # 9 not declared

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


def test_arbitrary_injected_value_rejected(bot, arm_dir, monkeypatch):
    """An arbitrary (attacker-chosen) step2 value never reaches subprocess."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "8",
        "step_buttons": {"1": [["8", "8"]], "2": [["0", "0"], ["건너뜀", "skip"]]},
    }, omit=("step_validate",))
    q = FakeQuery(f"idrill:{arm_id}:2:$(rm -rf /)")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))

    assert called == [], "arbitrary value must be rejected before any fire"
    assert q.text_edits == [messages.IDRILL_ERROR]


# --- (924-c) step_validate regex is the gate when present ---

def test_step_validate_regex_accepts_matching(bot, arm_dir, monkeypatch):
    """A value matching step_validate[2] fires even if not an explicit button
    value in the same shape (validate is the source when present)."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "12",
        "step_buttons": {"1": [["12", "12"]], "2": [["0", "0"], ["건너뜀", "skip"]]},
        "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-9]{1,2}|skip)$"},
    })
    q = FakeQuery(f"idrill:{arm_id}:2:47")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)
    run(bot._handle_idrill_callback(q, q.data))

    argv = argv_captured[0]
    assert argv[argv.index("--intensity") + 1] == "47"


def test_step_validate_regex_rejects_nonmatching(bot, arm_dir, monkeypatch):
    """A value failing step_validate[2] is rejected even if callback well-formed."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "12",
        "step_buttons": {"1": [["12", "12"]], "2": [["0", "0"], ["건너뜀", "skip"]]},
        "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-3]|skip)$"},
    })
    q = FakeQuery(f"idrill:{arm_id}:2:99")  # fails ^([0-3]|skip)$

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


def test_no_declaration_for_step_rejects(bot, arm_dir, monkeypatch):
    """An arm with neither step_buttons nor step_validate for a step rejects
    every value (fail-soft: the bridge never accepts an ungated value)."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={"_pending_step1": "8"},
              omit=("step_buttons", "step_validate"))
    q = FakeQuery(f"idrill:{arm_id}:2:0")

    called = []
    monkeypatch.setattr(bot_mod.subprocess, "run", lambda a, **k: called.append(a))
    run(bot._handle_idrill_callback(q, q.data))

    assert called == []
    assert q.text_edits == [messages.IDRILL_ERROR]


# --- (924-d) the removed hardcoded constants are absent from the class/source ---

def test_hardcoded_step_constants_removed():
    """The RIR/reps hardcoded button + regex constants must no longer exist."""
    for name in (
        "_IDRILL_STEP2_BUTTONS", "_IDRILL_STEP2_VALUES",
        "_IDRILL_STEP2_SKIP_LABEL", "_IDRILL_STEP2_SKIP_SUFFIX",
        "_IDRILL_STEP1_RE", "_IDRILL_STEP2_RE",
    ):
        assert not hasattr(TelegramBot, name), (
            f"hardcoded step constant {name} must be removed (arm-declared now)"
        )


def test_bridge_source_has_no_rir_button_literal():
    """The bridge source must not carry the RIR button value set or reps regex
    literal as hardcoded tokens (proves the SOURCE moved to the arm file)."""
    src = Path(bot_mod.__file__).read_text(encoding="utf-8")
    assert '"4+"' not in src, "hardcoded '4+' RIR literal must be gone"
    assert "건너뜀" not in src, "hardcoded skip label must be gone"
    assert '{"0", "1", "2", "3", "4+", "skip"}' not in src
    assert r'r"^[0-9]{1,4}$"' not in src
    assert r'r"^[0-4]$"' not in src


# --- (924-e) a NON-RIR button set proves true genericity ---

def test_non_rir_buttonset_full_flow(bot, arm_dir, monkeypatch):
    """A completely different domain (mood am/pm + a/b/c) drives the same
    mechanism. Proves the bridge is shape-agnostic (no workout assumption)."""
    arm_id = "abcd1234"
    bin_path = "/opt/lifekit/bin/log-mood"
    write_arm(arm_dir, arm_id, extra={
        "cmd": [bin_path, "sess-1", "{1}", "--mood", "{2}"],
        "step_buttons": {
            "1": [["morning", "am"], ["evening", "pm"]],
            "2": [["a", "a"], ["b", "b"], ["c", "c"], ["skip", "skip"]],
        },
        "step_validate": {"1": r"^(am|pm)$", "2": r"^([abc]|skip)$"},
    })

    q1 = FakeQuery(f"idrill:{arm_id}:1:am")
    run(bot._handle_idrill_callback(q1, q1.data))
    rows = q1.markup_edits[0].inline_keyboard
    assert [b.text for b in rows[0]] == ["a", "b", "c", "skip"]

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)
    q2 = FakeQuery(f"idrill:{arm_id}:2:b")
    run(bot._handle_idrill_callback(q2, q2.data))

    assert argv_captured[0] == [bin_path, "sess-1", "am", "--mood", "b"]


# --- (924-f) skip / other are declaration-driven, not hardcoded ---

def test_skip_value_declaration_driven(bot, arm_dir, monkeypatch):
    """'skip' fires cmd_skip2 -- and is gated by the arm's own declaration."""
    arm_id = "abcd1234"
    write_arm(arm_dir, arm_id, extra={
        "_pending_step1": "am",
        "cmd_skip2": ["/opt/lifekit/bin/log-mood", "sess-1", "{1}"],
        "step_buttons": {"1": [["morning", "am"]], "2": [["a", "a"], ["끝", "skip"]]},
        "step_validate": {"1": r"^am$", "2": r"^(a|skip)$"},
    })
    q = FakeQuery(f"idrill:{arm_id}:2:skip")

    argv_captured = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""
    monkeypatch.setattr(bot_mod.subprocess, "run",
                        lambda argv, **kw: argv_captured.append(argv) or fake_proc)
    run(bot._handle_idrill_callback(q, q.data))

    assert argv_captured[0] == ["/opt/lifekit/bin/log-mood", "sess-1", "am"]


def test_other_value_triggers_text_fallback(bot, arm_dir):
    """A declared 'other' step1 value triggers the free-text prompt (no fire),
    leaving the arm file intact for TTL cleanup."""
    arm_id = "abcd1234"
    arm_path = write_arm(arm_dir, arm_id, extra={
        "step_buttons": {
            "1": [["1", "1"], ["기타", "other"]],
            "2": [["0", "0"], ["건너뜀", "skip"]],
        },
    })
    q = FakeQuery(f"idrill:{arm_id}:1:other")
    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == [messages.IDRILL_OTHER_PROMPT]
    assert len(q.markup_edits) == 0
    assert arm_path.exists()


# ===========================================================================
# DGN-924 additions: [[IDRILL:<arm_id>]] initial-keyboard output marker +
# step_text step messages.
# ===========================================================================

class FakeMessage:
    """Minimal message mock recording reply_text calls."""

    def __init__(self):
        self.chat = MagicMock()
        self.chat.id = 12345
        self.reply_calls = []  # (text, reply_markup)

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.reply_calls.append((text, reply_markup))


STEP_TEXT_ARM = {
    "session_id": "sess-abc123",
    "cmd": [CMD_BIN, "sess-abc123", "{1}", "--intensity", "{2}"],
    "cmd_skip2": [CMD_BIN, "sess-abc123", "{1}"],
    "step_buttons": {
        "1": [["8", "8"], ["9", "9"], ["기타", "other"]],
        "2": [["0", "0"], ["1", "1"], ["건너뜀", "skip"]],
    },
    "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-1]|skip)$"},
    "step_text": {"1": "몇 회 하셨나요?", "2": "강도(RIR)는?"},
}


def write_steptext_arm(arm_dir, arm_id="abcd1234", extra=None, omit=None):
    content = dict(STEP_TEXT_ARM)
    if extra:
        content.update(extra)
    for k in (omit or ()):
        content.pop(k, None)
    (arm_dir / arm_id).write_text(json.dumps(content), encoding="utf-8")
    return arm_dir / arm_id


def test_idrill_marker_renders_initial_keyboard(bot, arm_dir):
    """[[IDRILL:<arm_id>]] -> step1 keyboard from step_buttons[1] + step_text[1].

    Drives the render helper the marker branch of _send_content_artifacts calls.
    """
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id)
    msg = FakeMessage()

    run(bot._idrill_send_initial_keyboard(msg, arm_id))

    assert len(msg.reply_calls) == 1
    text, markup = msg.reply_calls[0]
    assert text == "몇 회 하셨나요?"
    rows = markup.inline_keyboard
    assert [b.text for b in rows[0]] == ["8", "9", "기타"]
    assert [b.callback_data for b in rows[0]] == [
        f"idrill:{arm_id}:1:8",
        f"idrill:{arm_id}:1:9",
        f"idrill:{arm_id}:1:other",
    ]


def test_idrill_marker_dispatched_by_artifacts(bot, arm_dir, monkeypatch):
    """_send_content_artifacts consumes the [[IDRILL:..]] marker and calls the
    initial-keyboard render (integration: marker -> render dispatch)."""
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id)
    msg = FakeMessage()
    content = f"기록을 시작할게요.\n[[IDRILL:{arm_id}]]"

    # Stub the file-path send (needs a live application) and capture the
    # initial-keyboard dispatch.
    async def _noop_files(chat_id, paths):
        return None
    dispatched = []
    async def _capture(message, aid):
        dispatched.append(aid)
    monkeypatch.setattr(bot, "_send_file_paths", _noop_files)
    monkeypatch.setattr(bot, "_idrill_send_initial_keyboard", _capture)

    run(bot._send_content_artifacts(msg, content, force_options=False))

    assert dispatched == [arm_id]


def test_idrill_marker_stripped_from_body():
    """The [[IDRILL:..]] marker line is removed from the display body."""
    from bridge.formatting import strip_idrill_marker
    body = "line one\n[[IDRILL:abcd1234]]\nline two"
    clean, arm_id = strip_idrill_marker(body)
    assert arm_id == "abcd1234"
    assert "[[IDRILL" not in clean
    assert "line one" in clean and "line two" in clean


def test_idrill_marker_no_arm_id_when_absent():
    """No marker in the body -> strip returns None (no keyboard dispatched)."""
    from bridge.formatting import strip_idrill_marker
    clean, arm_id = strip_idrill_marker("plain body, no marker")
    assert arm_id is None
    assert clean == "plain body, no marker"


def test_idrill_marker_invalid_arm_id_fail_soft(bot, arm_dir):
    """An invalid arm_id shape renders nothing (fail-soft)."""
    msg = FakeMessage()
    run(bot._idrill_send_initial_keyboard(msg, "ABCD1234"))  # uppercase invalid
    assert msg.reply_calls == []


def test_idrill_marker_missing_arm_fail_soft(bot, arm_dir):
    """A well-formed arm_id whose arm file is absent renders nothing."""
    msg = FakeMessage()
    run(bot._idrill_send_initial_keyboard(msg, "abcd1234"))  # no arm file
    assert msg.reply_calls == []


def test_idrill_marker_no_step_buttons1_fail_soft(bot, arm_dir):
    """An arm declaring no step_buttons[1] renders nothing (fail-soft)."""
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id, extra={
        "step_buttons": {"2": [["0", "0"], ["건너뜀", "skip"]]},
    })
    msg = FakeMessage()
    run(bot._idrill_send_initial_keyboard(msg, arm_id))
    assert msg.reply_calls == []


def test_idrill_marker_no_step_text1_fail_soft(bot, arm_dir):
    """A marker whose arm declares no step_text[1] renders nothing (fail-soft)."""
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id, extra={"step_text": {"2": "x"}})
    msg = FakeMessage()
    run(bot._idrill_send_initial_keyboard(msg, arm_id))
    assert msg.reply_calls == []


def test_step2_swap_uses_step_text2(bot, arm_dir):
    """When step_text[2] is declared, the step1 tap swaps BOTH text and keyboard."""
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id)
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))

    assert q.text_edits == ["강도(RIR)는?"]
    assert len(q.markup_edits) == 0


def test_step2_swap_no_step_text_keeps_markup_only(bot, arm_dir):
    """Without step_text[2], the step1 tap keeps the markup-only swap (no text)."""
    arm_id = "abcd1234"
    write_steptext_arm(arm_dir, arm_id, omit=("step_text",))
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    run(bot._handle_idrill_callback(q, q.data))

    assert len(q.markup_edits) == 1
    assert q.text_edits == []
