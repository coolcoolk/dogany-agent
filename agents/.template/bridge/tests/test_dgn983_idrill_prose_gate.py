"""DGN-983 / DGN-1527: prose count-question beside an armed idrill keyboard.

A turn that arms an idrill keyboard sometimes also asks in prose for the value
the keyboard collects, so the owner sees buttons AND a text question. The rule
is ONE output-gate rule (routines/lib/output_gate.py IDRILL_PROSE_Q); the
bridge send seat normalizes (removes the duplicate question) and counts.

Acceptance (ticket):
  A. keyboard + duplicate count-question -> question removed and counted
  B. keyboard + unrelated prose          -> untouched
  C. no keyboard                         -> untouched
  D. kill switch off                     -> untouched (would-fire still counted)
Plus: the keyboard is never dropped, non-question prose is never dropped,
consumer rails are never rewritten, the streamed draft is corrected, and the
lib marker regex stays in lockstep with the bridge's.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge import artifacts  # noqa: E402
from bridge.formatting import IDRILL_MARKER_RE  # noqa: E402

TEMPLATE = _root
LIB_PATH = TEMPLATE / "routines" / "lib" / "output_gate.py"
SWITCH_PATH = TEMPLATE / "routines" / "gate-switch.py"
# The rule lives beside the bridge in an instance (routines/lib). A bridge
# tree staged on its own (extract-bridge gate 3 --control) has no routines/,
# so there is nothing to test the seam against.
pytestmark = pytest.mark.skipif(
    not (LIB_PATH.is_file() and SWITCH_PATH.is_file()),
    reason="routines/lib/output_gate.py not beside this bridge tree",
)
ARM_ID = "0bcbbb7b"
MARKER = "[[IDRILL:%s]]" % ARM_ID

ARM = {
    "step_buttons": {
        "1": [["10", "10"], ["11", "11"], ["12", "12"]],
        "2": [["0", "0"], ["1", "1"], ["2", "2"]],
    },
    "step_text": {"1": "반복수를 눌러주세요.", "2": "RIR을 눌러주세요."},
    "cmd": ["/bin/true", "{1}", "{2}"],
}


def _load_lib():
    spec = importlib.util.spec_from_file_location("output_gate_dgn983", LIB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lib():
    return _load_lib()


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A minimal instance root: routines/lib/output_gate.py, gate-switch.py,
    config/agent.conf and one armed step-1 keyboard."""
    root = tmp_path / "inst"
    (root / "routines" / "lib").mkdir(parents=True)
    shutil.copy(LIB_PATH, root / "routines" / "lib" / "output_gate.py")
    shutil.copy(SWITCH_PATH, root / "routines" / "gate-switch.py")
    (root / "config").mkdir()
    (root / "config" / "agent.conf").write_text("", encoding="utf-8")
    arm_dir = root.joinpath(*artifacts.ARM_SUBDIR)
    arm_dir.mkdir(parents=True)
    (arm_dir / ARM_ID).write_text(json.dumps(ARM), encoding="utf-8")
    for key in ("GATE_SEND_OUTPUT_IDRILL_PROSE", "GATE_SEND_ALL",
                "GATE_STOP_ALL", "GATE_ALL", "GATE_SWITCH_CONF"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GATE_SWITCH_LOG", str(tmp_path / "suppressed.log"))
    return root


def _canary(root_logs: Path):
    p = root_logs / artifacts.OUTPUT_GATE_CANARY_LOG
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _gate(instance, text, ready=True):
    logs = instance / "logs"
    out = artifacts.normalize_idrill_prose(
        text, instance, ready, log_dir=logs, arm_id=ARM_ID)
    return out, _canary(logs)


# ---------------------------------------------------------------------------
# Rule (lib) -- the predicate and the normalizer
# ---------------------------------------------------------------------------

LEAKS = [
    "몇 개 하셨어요?",
    "몇 개 하셨는지 아래에서 탭해 주세요.",  # the measured 08-20 leak form
    "몇 회 하셨나요",
    "이번 세트 몇 개 했어요?",
    "반복수 알려주세요.",
    "반복수는 어떻게 되세요?",
    "How many reps did you get?",
]
CLEAN = [
    "아래에서 반복수 눌러주세요.",  # pointer at the buttons, not a question
    "반복수를 아래 버튼으로 입력해 주세요.",
    "컨디션 어떠세요?",
    "몇 번째 세트인지 표에 있어요.",
    "반복수가 늘어나셨어요.",
    "펜들레이 로우 60kg 준비됐어요.",
    "좋아요, 첫 워킹세트 들어갑니다.",
    # false-positive classes measured on the 2026-09-30 transcript replay
    "끝나면 몇 개를 어떻게 옮겼는지 정리해서 알려드릴게요.",
    "어디서 온 기록이 몇 번으로 들어왔는지 적어 두는 표예요.",
    "알림 몇 개는 필요 없어지지 않나 싶었으니까요.",
    "카드에는 \"세션마다 무게나 횟수 조금씩 올리기\"로 적어요.",
    "형님 말씀대로 목적은 묻는 횟수를 줄이는 쪽입니다.",
    "step_text 는 `반복수를 입력해주세요.` 입니다.",
]


@pytest.mark.parametrize("sentence", LEAKS)
def test_predicate_hits_count_questions(lib, sentence):
    assert lib.is_idrill_count_question(sentence)


@pytest.mark.parametrize("sentence", CLEAN)
def test_predicate_leaves_other_prose(lib, sentence):
    assert not lib.is_idrill_count_question(sentence)


def test_A_duplicate_question_removed_rest_kept(lib):
    text = "좋아요, 첫 워킹세트 들어갑니다. 몇 개 하셨어요?\n" + MARKER
    out, removed = lib.normalize_idrill_prose_question(text)
    assert removed == ["몇 개 하셨어요?"]
    assert out == "좋아요, 첫 워킹세트 들어갑니다.\n" + MARKER


def test_A_question_line_dropped_without_blank_pileup(lib):
    text = "세트 끝나셨네요.\n\n몇 개 하셨어요?\n\n" + MARKER
    out, removed = lib.normalize_idrill_prose_question(text)
    assert removed == ["몇 개 하셨어요?"]
    assert out == "세트 끝나셨네요.\n\n" + MARKER


def test_B_unrelated_prose_is_identity(lib):
    text = "펜들레이 로우 60kg 준비됐어요.\n아래에서 반복수 눌러주세요.\n" + MARKER
    out, removed = lib.normalize_idrill_prose_question(text)
    assert removed == [] and out is text


def test_C_no_marker_is_identity(lib):
    text = "몇 개 하셨어요?"
    out, removed = lib.normalize_idrill_prose_question(text)
    assert removed == [] and out is text
    # an inline (non-standalone) marker mention is not an armed keyboard
    text2 = "마커는 `[[IDRILL:0bcbbb7b]]` 형태예요. 몇 개 하셨어요?"
    assert lib.normalize_idrill_prose_question(text2) == (text2, [])


def test_json_shaped_line_never_touched(lib):
    text = '{"step_text": {"1": "반복수를 입력해주세요?"}}\n' + MARKER
    assert lib.normalize_idrill_prose_question(text) == (text, [])


def test_masked_lines_never_touched(lib):
    text = ("```\n몇 개 하셨어요?\n```\n> 몇 개 하셨어요?\n"
            "| 몇 개 하셨어요? |\n" + MARKER)
    assert lib.normalize_idrill_prose_question(text) == (text, [])


def test_check_message_detects(lib):
    v = lib.check_message("몇 개 하셨어요?\n" + MARKER)
    hits = [x for x in v if x.rule_id == "IDRILL_PROSE_Q"]
    assert len(hits) == 1 and hits[0].severity == lib.SEVERITY_DETECT
    assert not [x for x in lib.check_message("몇 개 하셨어요?")
                if x.rule_id == "IDRILL_PROSE_Q"]


def test_marker_regex_parity_with_bridge(lib):
    assert lib.IDRILL_MARKER_LINE_RE.pattern == IDRILL_MARKER_RE.pattern


# ---------------------------------------------------------------------------
# Bridge adapter (artifacts.normalize_idrill_prose) -- switch + counter
# ---------------------------------------------------------------------------

def test_A_adapter_removes_and_counts(instance):
    (out, changed), lines = _gate(instance, "좋아요.\n몇 개 하셨어요?")
    assert changed and out == "좋아요."
    assert len(lines) == 1
    assert "IDRILL_PROSE_Q" in lines[0] and "stage=normalized" in lines[0]
    assert "arm=%s" % ARM_ID in lines[0] and "[몇 개 하셨어요?]" in lines[0]


def test_B_adapter_unrelated_prose_untouched_uncounted(instance):
    (out, changed), lines = _gate(instance, "좋아요. 아래에서 눌러주세요.")
    assert not changed and out == "좋아요. 아래에서 눌러주세요."
    assert lines == []


def test_C_adapter_keyboard_not_ready_untouched(instance):
    (out, changed), lines = _gate(instance, "몇 개 하셨어요?", ready=False)
    assert not changed and out == "몇 개 하셨어요?" and lines == []


def test_D_kill_switch_env_off_untouched_but_counted(instance, monkeypatch, tmp_path):
    monkeypatch.setenv("GATE_SEND_OUTPUT_IDRILL_PROSE", "off")
    (out, changed), lines = _gate(instance, "좋아요.\n몇 개 하셨어요?")
    assert not changed and out == "좋아요.\n몇 개 하셨어요?"
    assert len(lines) == 1 and "stage=suppressed-off" in lines[0]
    sup = (tmp_path / "suppressed.log").read_text(encoding="utf-8")
    assert "output-gate-idrill-prose" in sup


def test_D_kill_switch_conf_off_and_master(instance, monkeypatch):
    conf = instance / "config" / "agent.conf"
    conf.write_text("GATE_SEND_OUTPUT_IDRILL_PROSE=off\n", encoding="utf-8")
    (out, changed), _ = _gate(instance, "몇 개 하셨어요?")
    assert not changed
    conf.write_text("GATE_SEND_ALL=off\n", encoding="utf-8")
    (out, changed), _ = _gate(instance, "몇 개 하셨어요?")
    assert not changed


def test_stop_master_does_not_disarm_send_group(instance, monkeypatch):
    monkeypatch.setenv("GATE_STOP_ALL", "off")
    monkeypatch.setenv("GATE_ALL", "off")
    (out, changed), _ = _gate(instance, "좋아요.\n몇 개 하셨어요?")
    assert changed and out == "좋아요."


def test_lib_absent_or_old_is_untouched(tmp_path):
    root = tmp_path / "bare"
    (root / "routines" / "lib").mkdir(parents=True)
    out = artifacts.normalize_idrill_prose("몇 개 하셨어요?", root, True)
    assert out == ("몇 개 하셨어요?", False)
    (root / "routines" / "lib" / "output_gate.py").write_text(
        "def check_message(text):\n    return []\n", encoding="utf-8")
    out = artifacts.normalize_idrill_prose("몇 개 하셨어요?", root, True)
    assert out == ("몇 개 하셨어요?", False)


def test_step1_renders_gates():
    assert artifacts.step1_renders(ARM, ARM_ID)
    assert not artifacts.step1_renders(None, ARM_ID)
    assert not artifacts.step1_renders(ARM, "BAD")
    assert not artifacts.step1_renders({"step_buttons": ARM["step_buttons"]}, ARM_ID)


# ---------------------------------------------------------------------------
# Send seats (bot.py) -- keyboard kept, draft corrected, consumer untouched
# ---------------------------------------------------------------------------

def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


@pytest.fixture
def seat(instance, monkeypatch, tmp_path):
    bot_mod = _load_bot()
    monkeypatch.setattr(bot_mod, "PROJECT_ROOT", instance)
    logs = tmp_path / "botlogs"
    monkeypatch.setattr(bot_mod.config, "logs_dir", logs)
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot = MagicMock()
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    bot._send_content_artifacts = AsyncMock()
    bot._kick_update_offer = MagicMock()
    return bot_mod, bot, logs


def _message(sent):
    msg = MagicMock()
    msg.chat.id = 42
    msg.message_id = 100
    from datetime import datetime, timezone
    msg.date = datetime.now(timezone.utc)

    async def _reply_text(text, **kw):
        sent.append(text)

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    tg = MagicMock()
    msg.get_bot.return_value = tg
    tg.delete_message = AsyncMock()
    tg.edit_message_text = AsyncMock()
    return msg


def test_seat_reply_removes_question_keeps_keyboard(seat):
    bot_mod, bot, logs = seat
    sent = []
    msg = _message(sent)
    content = "좋아요, 첫 워킹세트 들어갑니다.\n몇 개 하셨어요?\n" + MARKER
    asyncio.run(bot._reply_smart(msg, content))
    body = "\n".join(sent)
    assert "몇 개 하셨어요" not in body
    assert "첫 워킹세트 들어갑니다" in body
    # the keyboard tail still receives the ORIGINAL content (marker intact)
    args = bot._send_content_artifacts.await_args
    assert MARKER in args.args[1]
    assert any("stage=normalized" in ln for ln in _canary(logs))


def test_seat_reply_unrelated_prose_untouched(seat):
    bot_mod, bot, logs = seat
    sent = []
    content = "좋아요.\n아래에서 반복수 눌러주세요.\n" + MARKER
    asyncio.run(bot._reply_smart(_message(sent), content))
    assert "아래에서 반복수 눌러주세요" in "\n".join(sent)
    assert _canary(logs) == []


def test_seat_reply_missing_arm_untouched(seat, instance):
    bot_mod, bot, logs = seat
    (instance.joinpath(*artifacts.ARM_SUBDIR) / ARM_ID).unlink()
    sent = []
    asyncio.run(bot._reply_smart(_message(sent), "몇 개 하셨어요?\n" + MARKER))
    assert "몇 개 하셨어요?" in "\n".join(sent)


def test_seat_streamed_draft_is_force_edited(seat):
    bot_mod, bot, logs = seat
    sent = []
    msg = _message(sent)
    content = "좋아요.\n몇 개 하셨어요?\n" + MARKER
    asyncio.run(bot._reply_smart(msg, content, streamed=True,
                                 draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    assert tg.edit_message_text.await_count == 1
    edited = tg.edit_message_text.await_args
    text = edited.kwargs.get("text") or edited.args[0]
    assert "몇 개" not in text and "좋아요." in text


def test_seat_streamed_question_only_body_deletes_draft(seat):
    bot_mod, bot, logs = seat
    sent = []
    msg = _message(sent)
    asyncio.run(bot._reply_smart(msg, "몇 개 하셨어요?\n" + MARKER,
                                 streamed=True, draft_message_ids=[7]))
    tg = msg.get_bot.return_value
    tg.delete_message.assert_awaited_with(42, 7)
    assert sent == []
    bot._send_content_artifacts.assert_awaited()


def test_seat_send_smart_consumer_rail_untouched(seat):
    bot_mod, bot, logs = seat
    bot._send_text_body_chat = AsyncMock(return_value=[])
    asyncio.run(bot._send_smart(42, "몇 개 하셨어요?\n" + MARKER,
                                rail=bot_mod.RAIL_CONSUMER))
    body = bot._send_text_body_chat.await_args.args[1]
    assert "몇 개 하셨어요?" in body
    assert _canary(logs) == []


def test_seat_send_smart_model_rail_normalized(seat):
    bot_mod, bot, logs = seat
    bot._send_text_body_chat = AsyncMock(return_value=[])
    asyncio.run(bot._send_smart(42, "좋아요.\n몇 개 하셨어요?\n" + MARKER,
                                rail=bot_mod.RAIL_MODEL))
    body = bot._send_text_body_chat.await_args.args[1]
    assert "몇 개" not in body and "좋아요." in body
