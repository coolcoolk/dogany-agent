"""DGN-932: class-wise notification policy (NOTIFY_POLICY) for in-process sends.

Two mechanisms (owner lock 2026-08-19):
  A. edited-surface bubbles -- notification decided ONCE at the first send;
     later in-place edits are notification-free at the Telegram level.
       draft = loud / fold = silent / countdown = loud (first tick only,
       supersedes DGN-594 hardcoded silence) / dashboard = silent
  B. one-shot messages -- Telegram-default loud; a send site opts into
     silence by class tag. options_prompt (SELECT_PROMPT button appendix)
     = silent; system/error/timeout/file/command replies stay untagged loud.

Covers: NOTIFY_POLICY parsing, notify_silent() default fallback + override
precedence, and the actual send-site wiring (draft / fold / countdown first
tick vs later ticks / options prompt on both _reply_ and _send_ paths).
push.sh (out-of-process push rail) is deliberately NOT covered: it already
carries its own --silent boolean and never reaches bridge config.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")
# Hermetic: a NOTIFY_POLICY leaked from the launching shell must not skew
# the unset-default assertions below.
os.environ.pop("NOTIFY_POLICY", None)

from bridge.config import (  # noqa: E402
    Config,
    NOTIFY_CLASS_DEFAULT_SILENT,
    config,
    notify_silent,
)

OWNER_ID = 42


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Config parsing (NOTIFY_POLICY env string -> dict)
# ---------------------------------------------------------------------------


class TestNotifyPolicyParsing:
    def test_unset_is_empty_dict(self):
        assert Config().notify_policy == {}

    def test_space_separated_entries(self):
        cfg = Config(notify_policy="fold=loud countdown=silent")
        assert cfg.notify_policy == {"fold": False, "countdown": True}

    def test_comma_separated_and_mixed_case(self):
        cfg = Config(notify_policy="Fold=LOUD,options_prompt=Silent")
        assert cfg.notify_policy == {"fold": False, "options_prompt": True}

    def test_malformed_entries_skipped(self):
        cfg = Config(notify_policy="fold=loud bogus x=maybe =silent")
        assert cfg.notify_policy == {"fold": False}

    def test_unknown_class_names_kept(self):
        # Forward-compatible: unknown classes parse fine and stay inert
        # until a send site tags them.
        cfg = Config(notify_policy="future_class=silent")
        assert cfg.notify_policy == {"future_class": True}

    def test_dict_passthrough(self):
        cfg = Config(notify_policy={"fold": False})
        assert cfg.notify_policy == {"fold": False}


# ---------------------------------------------------------------------------
# 2. Resolver: locked defaults + override precedence
# ---------------------------------------------------------------------------


class TestNotifySilentResolver:
    def test_locked_defaults(self):
        with patch.object(config, "notify_policy", {}):
            assert notify_silent("draft") is False       # answer arrival = loud
            assert notify_silent("fold") is True         # progress noise
            assert notify_silent("countdown") is False   # set-start signal
            assert notify_silent("dashboard") is True    # routine recreation
            assert notify_silent("options_prompt") is True  # button appendix

    def test_defaults_table_matches_lock(self):
        assert NOTIFY_CLASS_DEFAULT_SILENT == {
            "draft": False,
            "fold": True,
            "countdown": False,
            "dashboard": True,
            "options_prompt": True,
        }

    def test_unknown_class_is_loud(self):
        # One-shot sends keep the Telegram default unless tagged silent.
        with patch.object(config, "notify_policy", {}):
            assert notify_silent("system_notice") is False
            assert notify_silent("") is False

    def test_override_wins_over_default(self):
        with patch.object(
            config, "notify_policy", {"fold": False, "countdown": True}
        ):
            assert notify_silent("fold") is False
            assert notify_silent("countdown") is True
            # untouched classes keep their defaults
            assert notify_silent("dashboard") is True
            assert notify_silent("draft") is False

    def test_override_can_silence_unknown_class(self):
        with patch.object(config, "notify_policy", {"future_class": True}):
            assert notify_silent("future_class") is True


# ---------------------------------------------------------------------------
# 3. Mechanism A wiring: streaming draft (loud) / fold (silent)
# ---------------------------------------------------------------------------


def _mock_bot(msg_id=5555):
    bot = MagicMock()
    fake_msg = MagicMock()
    fake_msg.message_id = msg_id
    bot.send_message = AsyncMock(return_value=fake_msg)
    bot.edit_message_text = AsyncMock()
    return bot


class TestStreamingWiring:
    def test_draft_first_send_is_loud_by_default(self):
        from bridge.streaming import StreamingMessageHandler

        bot = _mock_bot()
        handler = StreamingMessageHandler(bot, OWNER_ID, OWNER_ID)
        with patch.object(config, "notify_policy", {}):
            _run(handler.create_draft("hello"))
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is False

    def test_draft_first_send_respects_silent_override(self):
        from bridge.streaming import StreamingMessageHandler

        bot = _mock_bot()
        handler = StreamingMessageHandler(bot, OWNER_ID, OWNER_ID)
        with patch.object(config, "notify_policy", {"draft": True}):
            _run(handler.create_draft("hello"))
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is True

    def test_fold_first_send_is_silent_by_default(self):
        from bridge.streaming import send_fold_html

        bot = _mock_bot()
        with patch.object(config, "notify_policy", {}):
            _run(send_fold_html(bot, OWNER_ID, "<blockquote>p</blockquote>"))
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is True

    def test_fold_first_send_respects_loud_override(self):
        from bridge.streaming import send_fold_html

        bot = _mock_bot()
        with patch.object(config, "notify_policy", {"fold": False}):
            _run(send_fold_html(bot, OWNER_ID, "<blockquote>p</blockquote>"))
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is False

    def test_overflow_chunks_ride_the_draft_class(self):
        from bridge.streaming import StreamingMessageHandler

        bot = _mock_bot()
        handler = StreamingMessageHandler(bot, OWNER_ID, OWNER_ID)
        with patch.object(config, "notify_policy", {}):
            _run(handler._send_extra_chunks(["overflow body"]))
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is False


# ---------------------------------------------------------------------------
# 4. Mechanism A wiring: countdown -- first tick loud, later ticks silent edits
# ---------------------------------------------------------------------------


class TestCountdownWiring:
    def _run_countdown(self, bot, seconds=0.35, cadence=0.1):
        from bridge.countdown import start_countdown
        from bridge.edit_guard import EditRateGuard

        async def scenario():
            countdown = start_countdown(
                bot, OWNER_ID, seconds, "rest", cadence=cadence,
                guard=EditRateGuard(min_interval=0.0),
            )
            await asyncio.wait_for(countdown.task, timeout=2)

        asyncio.run(scenario())

    def test_first_tick_send_is_loud_by_default(self):
        bot = _mock_bot()
        with patch.object(config, "notify_policy", {}):
            self._run_countdown(bot)
        assert bot.send_message.await_count == 1  # ONE send = one decision point
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is False

    def test_later_ticks_are_inherently_silent_edits(self):
        # Mechanism A invariant: every tick after the first is an in-place
        # edit -- notification-free at the Telegram level, and the edit calls
        # carry no notification flag at all.
        bot = _mock_bot()
        with patch.object(config, "notify_policy", {}):
            self._run_countdown(bot)
        assert bot.edit_message_text.await_count >= 2
        for call in bot.edit_message_text.await_args_list:
            assert "disable_notification" not in call.kwargs

    def test_silent_override_restores_dgn594_behavior(self):
        bot = _mock_bot()
        with patch.object(config, "notify_policy", {"countdown": True}):
            self._run_countdown(bot, seconds=0.05, cadence=0.05)
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["disable_notification"] is True


# ---------------------------------------------------------------------------
# 5. Mechanism B wiring: options button appendix (SELECT_PROMPT) is silent
# ---------------------------------------------------------------------------


def _load_bot():
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)
    import bridge.bot as bot_mod
    return bot_mod


MARKER_REPLY = (
    "Pick a direction:\n"
    "\n"
    "1. proceed\n"
    "2. hold\n"
    "[[OPTIONS]]\n"
)


def _make_chat_bot(bot_mod, sent):
    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    bot.application = MagicMock()

    async def _send_message(chat_id, text, parse_mode=None, reply_markup=None,
                            link_preview_options=None, disable_notification=None):
        sent.append({
            "text": text,
            "reply_markup": reply_markup,
            "disable_notification": disable_notification,
        })

    bot.application.bot = MagicMock()
    bot.application.bot.send_message = AsyncMock(side_effect=_send_message)
    bot.application.bot.delete_message = AsyncMock()
    bot._last_incoming_mid = {}
    return bot


def _make_message(sent, chat_id=OWNER_ID):
    msg = MagicMock()
    msg.chat.id = chat_id

    async def _reply_text(text, parse_mode=None, reply_markup=None,
                          link_preview_options=None, reply_parameters=None,
                          disable_notification=None):
        sent.append({
            "text": text,
            "reply_markup": reply_markup,
            "disable_notification": disable_notification,
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = MagicMock()
    msg.get_bot.return_value.delete_message = AsyncMock()
    return msg


class TestOptionsPromptWiring:
    def test_send_smart_buttons_message_is_silent_body_is_loud(self):
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        with patch.object(config, "notify_policy", {}):
            _run(bot._send_smart(OWNER_ID, MARKER_REPLY, force_options=True))
        buttons = [e for e in sent if e["reply_markup"] is not None]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert len(buttons) == 1, f"got {sent}"
        assert buttons[0]["disable_notification"] is True
        # The body keeps the Telegram default (loud): no silence flag set.
        assert all(not b["disable_notification"] for b in bodies)

    def test_reply_path_buttons_message_is_silent(self):
        bot_mod = _load_bot()
        sent = []
        message = _make_message(sent)
        bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        # _send_file_paths touches application.bot even with zero paths.
        bot.application = MagicMock()
        with patch.object(config, "notify_policy", {}):
            _run(
                bot._send_content_artifacts(
                    message, MARKER_REPLY, force_options=True
                )
            )
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert buttons[0]["disable_notification"] is True

    def test_buttons_respect_loud_override(self):
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        with patch.object(config, "notify_policy", {"options_prompt": False}):
            _run(bot._send_smart(OWNER_ID, MARKER_REPLY, force_options=True))
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"got {sent}"
        assert buttons[0]["disable_notification"] is False


# ---------------------------------------------------------------------------
# 6. M1: turn-level 1-loud guarantee (list-only + non-streamed regression)
# ---------------------------------------------------------------------------

LIST_ONLY_REPLY = (
    "1. proceed\n"
    "2. hold\n"
    "[[OPTIONS]]\n"
)


class TestTurnOneLoudGuarantee:
    """DGN-932 M1 fix: a decision turn with list-only body (no prose lead-in)
    and streamed=False must never be completely silent.

    Path: _reply_smart / _send_smart -> body strips to whitespace -> skip
    body send -> options_prompt default=silent -> ZERO notifications.
    Fix: when body_was_loud is False at the button send, promote to loud.
    """

    def test_send_smart_list_only_nonstreamed_buttons_are_loud(self):
        # No body -> no loud send -> buttons MUST be loud (turn's only signal).
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        with patch.object(config, "notify_policy", {}):
            _run(
                bot._send_smart(
                    OWNER_ID, LIST_ONLY_REPLY, force_options=True, streamed=False
                )
            )
        buttons = [e for e in sent if e["reply_markup"] is not None]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert len(buttons) == 1, f"all sends: {sent}"
        assert len(bodies) == 0, f"unexpected body sends: {bodies}"
        # The single buttons message must be LOUD (False = not silent).
        assert buttons[0]["disable_notification"] is False, (
            f"buttons were silent (disable_notification=True); turn had zero notifications"
        )

    def test_reply_path_list_only_nonstreamed_buttons_are_loud(self):
        # Same for _reply_smart via _send_content_artifacts.
        bot_mod = _load_bot()
        sent = []
        message = _make_message(sent)
        bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        bot.application = MagicMock()
        with patch.object(config, "notify_policy", {}):
            _run(
                bot._send_content_artifacts(
                    message, LIST_ONLY_REPLY, force_options=True,
                    body_was_loud=False,
                )
            )
        buttons = [e for e in sent if e["reply_markup"] is not None]
        assert len(buttons) == 1, f"all sends: {sent}"
        assert buttons[0]["disable_notification"] is False

    def test_send_smart_with_body_buttons_remain_silent(self):
        # When a loud body did precede, buttons stay silent (regression guard).
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        with patch.object(config, "notify_policy", {}):
            _run(
                bot._send_smart(
                    OWNER_ID, MARKER_REPLY, force_options=True, streamed=False
                )
            )
        buttons = [e for e in sent if e["reply_markup"] is not None]
        bodies = [e for e in sent if e["reply_markup"] is None]
        assert len(bodies) >= 1, f"expected a body send: {sent}"
        assert buttons[0]["disable_notification"] is True

    def test_send_smart_streamed_with_draft_buttons_remain_silent(self):
        # A streamed turn (draft = loud carrier) -> buttons still silent.
        bot_mod = _load_bot()
        sent = []
        bot = _make_chat_bot(bot_mod, sent)
        with patch.object(config, "notify_policy", {}):
            _run(
                bot._send_smart(
                    OWNER_ID, LIST_ONLY_REPLY, force_options=True,
                    streamed=True, draft_message_ids=[1001],
                )
            )
        buttons = [e for e in sent if e["reply_markup"] is not None]
        # The draft was the loud carrier; buttons must be silent.
        assert len(buttons) == 1, f"all sends: {sent}"
        assert buttons[0]["disable_notification"] is True
