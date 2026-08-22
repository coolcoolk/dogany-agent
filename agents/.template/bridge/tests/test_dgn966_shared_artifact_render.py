"""DGN-966: shared artifact-render contract (model-turn / fast-path / push).

Success criterion (ticket DGN-966):
  (1) PATH PARITY -- the same content carrying [[IDRILL:<id>]] rendered via
      the model-turn path (_send_content_artifacts / _reply_smart tail) and
      via the fast-path/proactive path (_send_smart) produces an IDENTICAL
      keyboard (rows, labels, callback_data).
  (2) MULTI-BUTTON -- a declared 6-button row renders as one row of 6 on the
      chat-id (fast-path) rail.
  (3) GRID + DRILLDOWN -- a multi-row grid step renders as declared rows, and
      a non-final tap on a fast-path-sent keyboard swaps in place to the next
      step (drilldown works off the model path).
  (4) PUSH RAIL -- the push.sh python hop (bridge.artifacts.initial_keyboard_json)
      yields the same labels + callback_data as the in-bot builder.
  (5) RIDER -- confirm_fmt {i} renders from the FULL capture list on N-step
      arms; 2-step arms byte-identical.

Grill probes: 64-byte callback_data overflow (Korean values), split marker
fragments, stale/expired arm, telegram-free import of bridge.artifacts,
legacy flat shape byte-compat, --button signature untouched (push.sh source
scan).
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


def _run(coro):
    return asyncio.run(coro)


OWNER_ID = 777001


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _make_chat_bot(bot_mod, sent):
    """Bot whose application.bot.send_message records sends (chat-id rail)."""
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()

    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None, disable_notification=None):
        sent.append({"chat_id": chat_id, "text": text,
                     "reply_markup": reply_markup})

    bot.application.bot = MagicMock()
    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    return bot


def _make_message(sent, chat_id=OWNER_ID):
    """Message mock recording reply_text sends (model-turn rail)."""
    msg = MagicMock()
    msg.chat.id = chat_id

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None, reply_parameters=None,
                          disable_notification=None):
        sent.append({"chat_id": chat_id, "text": text,
                     "reply_markup": reply_markup})

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = MagicMock()
    msg.get_bot.return_value.delete_message = AsyncMock()
    return msg


@pytest.fixture
def bot_mod():
    return _load_bot()


@pytest.fixture
def arm_dir(bot_mod, tmp_path):
    cb = tmp_path / "files" / "program" / ".idrill-arm"
    cb.mkdir(parents=True)
    with patch.object(bot_mod, "PROJECT_ROOT", tmp_path):
        yield cb


CMD_BIN = "/opt/lifekit/bin/record-entry"

# 6-button reps row (the owner requirement DGN-835's single button could not
# serve) + a second grid step -- declared GRID shape (rows).
GRID_ARM = {
    "session_id": "sess-966",
    "cmd": [CMD_BIN, "{1}", "{2}"],
    "step_buttons": {
        "1": [["6", "6"], ["8", "8"], ["10", "10"],
              ["12", "12"], ["15", "15"], ["20", "20"]],
        "2": [
            [["0", "0"], ["1", "1"], ["2", "2"]],
            [["3", "3"], ["건너뜀", "skip"]],
        ],
    },
    "step_validate": {"1": r"^[0-9]{1,4}$", "2": r"^([0-3]|skip)$"},
    "step_text": {"1": "몇 회?", "2": "강도(RIR)?"},
}


def write_arm(arm_dir, arm_id="deadbee1", content=GRID_ARM, extra=None):
    data = dict(content)
    if extra:
        data.update(extra)
    (arm_dir / arm_id).write_text(json.dumps(data), encoding="utf-8")
    return arm_dir / arm_id


def _kb_shape(markup):
    """(labels, callback_data) grid from an InlineKeyboardMarkup."""
    return [[(b.text, b.callback_data) for b in row]
            for row in markup.inline_keyboard]


# ---------------------------------------------------------------------------
# (1) PATH PARITY: model-turn vs fast-path render the identical keyboard
# ---------------------------------------------------------------------------

def test_same_content_renders_identical_keyboard_on_both_paths(bot_mod, arm_dir):
    arm_id = "deadbee1"
    write_arm(arm_dir, arm_id)
    content = f"세트 기록했습니다.\n[[IDRILL:{arm_id}]]"

    # model-turn rail (message target, _reply_smart artifact tail)
    sent_model = []
    bot_a = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot_a.application = MagicMock()
    msg = _make_message(sent_model)
    _run(bot_a._send_content_artifacts(msg, content, force_options=False))

    # fast-path / proactive rail (bare chat_id through _send_smart)
    sent_fast = []
    bot_b = _make_chat_bot(bot_mod, sent_fast)
    _run(bot_b._send_smart(OWNER_ID, content))

    kb_model = [e for e in sent_model if e["reply_markup"] is not None]
    kb_fast = [e for e in sent_fast if e["reply_markup"] is not None]
    assert len(kb_model) == 1, f"model path sent {sent_model}"
    assert len(kb_fast) == 1, f"fast path sent {sent_fast}"
    # identical keyboard: same text, same rows, same labels, same callback_data
    assert kb_model[0]["text"] == kb_fast[0]["text"] == "몇 회?"
    assert _kb_shape(kb_model[0]["reply_markup"]) == _kb_shape(kb_fast[0]["reply_markup"])
    # and the body did NOT leak the marker on either path
    for e in sent_model + sent_fast:
        assert "[[IDRILL" not in e["text"]


def test_fastpath_push_guaranteed_renders_keyboard(bot_mod, arm_dir):
    """Regression for the DGN-966 defect itself: the fast-path delivery entry
    (_fastpath_push_guaranteed -> _send_smart) renders the declared keyboard
    instead of swallowing the marker."""
    arm_id = "deadbee1"
    write_arm(arm_dir, arm_id)
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    content = f"| set | reps |\n[[IDRILL:{arm_id}]]"
    _run(bot._fastpath_push_guaranteed(OWNER_ID, content))
    kbs = [e for e in sent if e["reply_markup"] is not None]
    assert len(kbs) == 1, f"sent: {sent}"
    rows = _kb_shape(kbs[0]["reply_markup"])
    assert [lbl for lbl, _cb in rows[0]] == ["6", "8", "10", "12", "15", "20"]


# ---------------------------------------------------------------------------
# (2) MULTI-BUTTON: a 6-key reps row is one row of six on the fast path
# ---------------------------------------------------------------------------

def test_six_button_row_renders_flat_on_chat_rail(bot_mod, arm_dir):
    arm_id = "deadbee1"
    write_arm(arm_dir, arm_id)
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, f"본문\n[[IDRILL:{arm_id}]]"))
    kb = [e for e in sent if e["reply_markup"] is not None][0]
    rows = _kb_shape(kb["reply_markup"])
    assert len(rows) == 1 and len(rows[0]) == 6
    assert rows[0][0] == ("6", f"idrill:{arm_id}:1:6")
    assert rows[0][5] == ("20", f"idrill:{arm_id}:1:20")


# ---------------------------------------------------------------------------
# (3) GRID + DRILLDOWN off the model path
# ---------------------------------------------------------------------------

class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.text_edits = []
        self.markup_edits = []

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append((text, kwargs.get("reply_markup")))

    async def edit_message_reply_markup(self, *, reply_markup=None, **kwargs):
        self.markup_edits.append(reply_markup)


def test_drilldown_tap_on_fastpath_keyboard_swaps_to_grid_step(bot_mod, arm_dir):
    """A step-1 tap on a keyboard that was SENT by the fast-path rail swaps
    in place to the declared step-2 GRID (2 rows) -- drilldown is path-free."""
    arm_id = "deadbee1"
    write_arm(arm_dir, arm_id)
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, f"본문\n[[IDRILL:{arm_id}]]"))
    kb = [e for e in sent if e["reply_markup"] is not None][0]
    tap_cb = kb["reply_markup"].inline_keyboard[0][1].callback_data  # "8"

    q = FakeQuery(tap_cb)
    _run(bot._handle_idrill_callback(q, q.data))
    # step_text["2"] declared -> text+markup swap
    assert len(q.text_edits) == 1
    text, markup = q.text_edits[0]
    assert text == "강도(RIR)?"
    rows = _kb_shape(markup)
    assert len(rows) == 2                      # declared grid: two rows
    assert [l for l, _ in rows[0]] == ["0", "1", "2"]
    assert [l for l, _ in rows[1]] == ["3", "건너뜀"]


def test_legacy_flat_shape_still_one_row(bot_mod, arm_dir):
    """DGN-918/924 byte-compat: flat [[label, value], ...] stays ONE row."""
    from bridge import artifacts
    arm = {"step_buttons": {"1": [["8", "8"], ["9", "9"], ["기타", "other"]]}}
    rows = artifacts.step_button_rows(arm, "1")
    assert rows == [[["8", "8"], ["9", "9"], ["기타", "other"]]]


# ---------------------------------------------------------------------------
# (4) PUSH RAIL: the push.sh hop produces the same keyboard
# ---------------------------------------------------------------------------

def test_push_hop_json_matches_inbot_keyboard(bot_mod, arm_dir, tmp_path):
    from bridge import artifacts
    arm_id = "deadbee1"
    write_arm(arm_dir, arm_id)

    # in-bot render
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, f"본문\n[[IDRILL:{arm_id}]]"))
    inbot = [e for e in sent if e["reply_markup"] is not None][0]
    inbot_rows = _kb_shape(inbot["reply_markup"])

    # push.sh hop render (raw JSON, no telegram objects)
    hop = artifacts.initial_keyboard_json(tmp_path, arm_id)
    assert hop is not None
    assert hop["text"] == inbot["text"]
    hop_rows = [[(b["text"], b["callback_data"]) for b in row]
                for row in hop["reply_markup"]["inline_keyboard"]]
    assert hop_rows == inbot_rows


def test_push_hop_fail_soft_on_missing_arm(tmp_path):
    from bridge import artifacts
    assert artifacts.initial_keyboard_json(tmp_path, "deadbee1") is None
    assert artifacts.initial_keyboard_json(tmp_path, "../../etc") is None


def test_artifacts_module_is_telegram_free():
    """bridge.artifacts must import without python-telegram-bot (push.sh hop
    runs outside the venv -- DGN-822 invariant)."""
    import importlib
    import bridge.artifacts as a
    src = Path(a.__file__).read_text(encoding="utf-8")
    assert "import telegram" not in src
    assert "from telegram" not in src
    assert "from bridge.config" not in src and "import bridge.config" not in src
    importlib.reload(a)  # re-import cleanly


# ---------------------------------------------------------------------------
# (5) RIDER: confirm_fmt {i} over the full capture list
# ---------------------------------------------------------------------------

THREE_STEP_ARM = {
    "session_id": "sess-3step",
    "cmd": [CMD_BIN, "{1}", "{2}", "{3}"],
    "step_final": "3",
    "step_buttons": {
        "1": [["기록", "go"]],                     # gate-style step
        "2": [["8", "8"], ["10", "10"]],
        "3": [["0", "0"], ["1", "1"]],
    },
    "step_validate": {"1": r"^go$", "2": r"^[0-9]{1,4}$", "3": r"^[0-1]$"},
    "step_text": {"1": "기록할까요?", "2": "몇 회?", "3": "강도?"},
    "confirm_fmt": "기록: {2}회 (RIR {3})",
}


def test_confirm_fmt_i_tokens_render_full_capture_list(bot_mod, arm_dir):
    """N-step arm: {2} is the REAL reps (second capture), not the gate tap."""
    arm_id = "deadbee2"
    write_arm(arm_dir, arm_id, content=THREE_STEP_ARM)
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)

    fired = {}

    async def _fake_fire(arm, captures, skipped):
        fired["captures"] = captures
        return True

    bot._idrill_fire_cmd_n = _fake_fire
    bot._idrill_run_followup = AsyncMock(return_value=None)

    q1 = FakeQuery(f"idrill:{arm_id}:1:go")
    _run(bot._handle_idrill_callback(q1, q1.data))
    q2 = FakeQuery(f"idrill:{arm_id}:2:10")
    _run(bot._handle_idrill_callback(q2, q2.data))
    q3 = FakeQuery(f"idrill:{arm_id}:3:1")
    _run(bot._handle_idrill_callback(q3, q3.data))

    assert fired["captures"] == [("1", "go"), ("2", "10"), ("3", "1")]
    assert q3.text_edits, "no confirmation edit"
    confirmation = q3.text_edits[-1][0]
    # OLD defect: {1}-anchored render used the gate tap ("go"); now {2}/{3}
    # come from the full ordered capture list.
    assert confirmation == "기록: 10회 (RIR 1)"


def test_confirm_fmt_two_step_byte_identical(bot_mod):
    """Legacy scalar signature (DGN-918/924 pair) renders unchanged."""
    bot = bot_mod.TelegramBot
    arm = {"confirm_fmt": "{1}회, 강도 {2}"}
    assert bot._idrill_render_confirmation(arm, "12", "3") == "12회, 강도 3"
    arm_skip = {"confirm_fmt_skip": "{1}회 (강도 생략)"}
    assert bot._idrill_render_confirmation(arm_skip, "12", "skip") == "12회 (강도 생략)"


def test_confirm_fmt_list_skip_appends_literal_skip(bot_mod):
    """List input on the skip path: the final positional renders 'skip' just
    as the 2-step pair form did."""
    bot = bot_mod.TelegramBot
    arm = {"confirm_fmt_skip": "{1}회 기록 ({2})"}
    assert bot._idrill_render_confirmation(arm, ["12"], "skip") == "12회 기록 (skip)"


# ---------------------------------------------------------------------------
# Grill probes
# ---------------------------------------------------------------------------

def test_64byte_callback_data_button_dropped_not_message(bot_mod, arm_dir):
    """A Korean declared value overflowing Telegram's 64-byte callback_data
    limit drops ONLY that button; siblings and the message survive."""
    arm_id = "deadbee3"
    long_val = "아주긴한글값" * 5  # 30 chars * 3 bytes = 90 bytes > 64
    arm = dict(GRID_ARM)
    arm["step_buttons"] = {
        "1": [["짧음", "1"], ["김", long_val]],
        "2": [["0", "0"]],
    }
    write_arm(arm_dir, arm_id, content=arm)
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, f"본문\n[[IDRILL:{arm_id}]]"))
    kbs = [e for e in sent if e["reply_markup"] is not None]
    assert len(kbs) == 1
    rows = _kb_shape(kbs[0]["reply_markup"])
    assert rows == [[("짧음", f"idrill:{arm_id}:1:1")]]


def test_partial_marker_fragment_not_rendered(bot_mod, arm_dir):
    """A marker split across a chunk boundary (no closing ]]) must not render
    a keyboard nor crash; the fragment stays literal text (same as the
    model-turn path today)."""
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, "본문\n[[IDRILL:deadbee1"))
    assert all(e["reply_markup"] is None for e in sent)


def test_expired_arm_marker_fails_soft(bot_mod, arm_dir):
    """Marker referencing a consumed/expired arm: body sends, no keyboard,
    no crash (stale-render probe)."""
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    _run(bot._send_smart(OWNER_ID, "본문\n[[IDRILL:deadbeef]]"))
    bodies = [e for e in sent if e["reply_markup"] is None]
    assert len(bodies) == 1
    assert all(e["reply_markup"] is None for e in sent)


def test_stale_keyboard_tap_after_arm_consumed(bot_mod, arm_dir):
    """Tapping a rendered keyboard after the arm was consumed -> EXPIRED
    edit, never a crash (push-sent keyboards share this path)."""
    from bridge import messages
    arm_id = "deadbee1"
    path = write_arm(arm_dir, arm_id)
    path.unlink()  # consumed/TTL-cleaned before the tap
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    q = FakeQuery(f"idrill:{arm_id}:1:8")
    _run(bot._handle_idrill_callback(q, q.data))
    assert q.text_edits and q.text_edits[0][0] == messages.IDRILL_ARM_EXPIRED


def test_push_sh_button_contract_untouched():
    """Backward compat: the DGN-835 --button single-button contract survives
    verbatim in push.sh (old callers keep working)."""
    push = Path(__file__).resolve().parents[2] / "routines" / "push.sh"
    src = push.read_text(encoding="utf-8")
    assert '--button)  BUTTON="$2"; shift 2 ;;' in src
    # the single-button JSON assembly is still present
    assert '"inline_keyboard": [[{"text": label, "callback_data": cb}]]' in src
    # and the new marker hop rides the SHARED contract module
    assert "from bridge.artifacts import initial_keyboard_json" in src


def test_options_keyboard_paths_share_renderer(bot_mod):
    """[[OPTIONS]] buttons also come out of the ONE shared tail on the chat
    rail (previously a divergent inline copy in _send_smart)."""
    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    content = "골라주세요:\n\n1. 진행\n2. 보류\n[[OPTIONS]]\n"
    _run(bot._send_smart(OWNER_ID, content, force_options=True))
    kbs = [e for e in sent if e["reply_markup"] is not None]
    assert len(kbs) == 1
    buttons = [b for row in kbs[0]["reply_markup"].inline_keyboard for b in row]
    assert len(buttons) == 2


# ---------------------------------------------------------------------------
# Verification round (Item 2): outside-PROJECT_ROOT send_file:: confirm must
# not raise a dead-end Allow/Deny prompt on the non-interactive (bare
# chat_id) rail -- fast-path / proactive have no live turn to answer it
# (measured: a cron push has nobody to tap the button, and the generic
# callback path is subject to the 20-min STALE gate anyway). The interactive
# (model-turn message) rail keeps the confirm keyboard verbatim.
# ---------------------------------------------------------------------------

def test_outside_root_file_noninteractive_rail_no_button_logged_and_notified(
    bot_mod, tmp_path, caplog
):
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "outside" / "report.txt"
    outside_file.parent.mkdir()
    outside_file.write_text("data")

    sent = []
    bot = _make_chat_bot(bot_mod, sent)

    session_calls = {"get": [], "update": []}

    async def fake_get_session(uid):
        session_calls["get"].append(uid)
        return {}

    async def fake_update_session(uid, session):
        session_calls["update"].append((uid, dict(session)))

    with patch.object(bot_mod, "PROJECT_ROOT", root), \
         patch.object(bot_mod.session_manager, "get_session",
                       side_effect=fake_get_session), \
         patch.object(bot_mod.session_manager, "update_session",
                       side_effect=fake_update_session), \
         caplog.at_level("WARNING"):
        content = f"본문\nsend_file:: {outside_file}"
        _run(bot._send_smart(OWNER_ID, content))

    # no interactive Allow/Deny keyboard on this rail (would be a dead end:
    # no live turn to tap it, and the generic callback ages out at 20 min)
    assert all(e["reply_markup"] is None for e in sent)
    # never silent: a plain notice names the omission
    assert any(str(outside_file) in e["text"] for e in sent)
    # no session-state write (nothing pending left to expire or leak into an
    # unrelated later turn)
    assert session_calls == {"get": [], "update": []}
    # loud log line for the omission
    assert any("non-interactive rail" in r.message for r in caplog.records)


def test_outside_root_file_interactive_rail_unchanged(bot_mod, tmp_path):
    """Regression guard: the model-turn path's Allow/Deny confirm keyboard
    (and its session-state pending-file write) is untouched by the
    non-interactive-rail fix above."""
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "outside" / "report.txt"
    outside_file.parent.mkdir()
    outside_file.write_text("data")

    sent = []
    bot = _make_chat_bot(bot_mod, sent)
    msg = _make_message(sent, chat_id=OWNER_ID)

    session_calls = {"get": [], "update": []}

    async def fake_get_session(uid):
        session_calls["get"].append(uid)
        return {}

    async def fake_update_session(uid, session):
        session_calls["update"].append((uid, dict(session)))

    with patch.object(bot_mod, "PROJECT_ROOT", root), \
         patch.object(bot_mod.session_manager, "get_session",
                       side_effect=fake_get_session), \
         patch.object(bot_mod.session_manager, "update_session",
                       side_effect=fake_update_session):
        content = f"본문\nsend_file:: {outside_file}"
        _run(bot._send_content_artifacts(msg, content, force_options=False))

    kbs = [e for e in sent if e["reply_markup"] is not None]
    assert len(kbs) == 1
    assert kbs[0]["text"] == bot_mod.messages.EXTERNAL_FILE_PROMPT
    assert session_calls["update"] == [
        (OWNER_ID, {"pending_external_files": [str(outside_file.resolve())]})
    ]
