"""DGN-085 part 2: _reply_smart / _send_smart auto-split code+options.

When a turn output contains BOTH a fenced code block (```) AND [[OPTIONS]]
buttons, the bridge must:
  1. Delete any streaming drafts (which were finalized without parse_mode).
  2. Re-send the body via _send_text_body / _send_text_body_chat (HTML for code
     segments), so code blocks and tables render correctly.
  3. Send the [[OPTIONS]] keyboard as a separate trailing message.

Also covers regression: options-only and code-only cases must not double-send.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, call, patch

# Guard: mock telegram only when the real package is absent.
if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-standalone")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(sent: list, chat_id: int = 42):
    """Return a minimal message mock whose reply_text appends to `sent`."""
    msg = MagicMock()
    msg.chat.id = chat_id

    async def _reply_text(
        text,
        parse_mode=None,
        reply_markup=None,
        link_preview_options=None,
        **kwargs,
    ):
        # DGN-947 P4 rider: absorb send-path kwargs the production code now
        # passes (DGN-932 disable_notification / notify_silent). The old fixed
        # signature raised TypeError on disable_notification, which surfaced as
        # a false "button regression" red -- the InlineKeyboardMarkup was in
        # fact built and passed correctly (harness drift, not a code defect).
        sent.append({
            "text": text,
            "parse_mode": parse_mode,
            "markup": reply_markup,
            "link_preview_options": link_preview_options,
            "disable_notification": kwargs.get("disable_notification"),
        })

    msg.reply_text = AsyncMock(side_effect=_reply_text)
    msg.get_bot.return_value = MagicMock()
    msg.get_bot.return_value.delete_message = AsyncMock()
    return msg


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Import TelegramBot under heavy mocking (no live Telegram / claude-agent-sdk)
# ---------------------------------------------------------------------------

def _load_bot():
    """Import bridge.bot with all external I/O mocked out."""
    # Already have telegram mocked if absent above.
    # Mock claude_agent_sdk and bridge.sdk_bridge.
    mock_sdk = MagicMock()
    mock_sdk.PermissionResultAllow = MagicMock
    mock_sdk.PermissionResultDeny = MagicMock
    sys.modules.setdefault("claude_agent_sdk", mock_sdk)

    import bridge.bot as bot_mod
    return bot_mod


# ---------------------------------------------------------------------------
# Test: _reply_smart splits code+options correctly
# ---------------------------------------------------------------------------

class TestReplySmart:
    def _make_bot(self):
        import bridge.bot as bot_mod
        bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        bot.application = MagicMock()
        bot.application.bot = MagicMock()
        bot.application.bot.delete_message = AsyncMock()
        return bot

    def test_streamed_code_plus_options_deletes_draft_and_resends(self):
        """Streamed response with code block + [[OPTIONS]]: draft deleted, body re-sent,
        options keyboard sent as a separate message (not inside the code bubble)."""
        bot_mod = _load_bot()
        bot = self._make_bot()
        sent = []
        msg = _make_message(sent)
        draft_ids = [101]

        content = (
            "Here is a table:\n\n"
            "```\n"
            "| col1 | col2 |\n"
            "| ---- | ---- |\n"
            "| a    | b    |\n"
            "```\n\n"
            "Pick one:\n\n"
            "1. Option A\n"
            "2. Option B\n"
            "[[OPTIONS]]"
        )

        _run(bot._reply_smart(
            msg, content,
            force_options=True, streamed=True, draft_message_ids=draft_ids
        ))

        # Draft 101 must be deleted.
        msg.get_bot.return_value.delete_message.assert_called_once_with(msg.chat.id, 101)

        # The code block must have been sent as HTML <pre>.
        html_sends = [s for s in sent if s.get("parse_mode") == "HTML"]
        assert html_sends, "Expected at least one HTML message for the code block"
        assert any("<pre>" in s["text"] for s in html_sends), (
            "Code block should be wrapped in <pre> HTML"
        )

        # The SELECT_PROMPT keyboard message must be sent last (separate message).
        kb_sends = [s for s in sent if s.get("markup") is not None]
        assert kb_sends, "Expected keyboard message for [[OPTIONS]]"

        # Verify ordering: all non-keyboard messages come before the keyboard.
        last_kb_idx = max(i for i, s in enumerate(sent) if s.get("markup") is not None)
        for i, s in enumerate(sent):
            if s.get("markup") is None:
                assert i < last_kb_idx, (
                    f"Non-keyboard message at index {i} came after keyboard at {last_kb_idx}"
                )

    def test_non_streamed_code_plus_options(self):
        """Non-streamed code+options: body sent first (HTML), keyboard second."""
        bot_mod = _load_bot()
        bot = self._make_bot()
        sent = []
        msg = _make_message(sent)

        content = (
            "```python\nprint('hello')\n```\n\n"
            "1. Run\n"
            "2. Skip\n"
            "[[OPTIONS]]"
        )

        _run(bot._reply_smart(
            msg, content,
            force_options=True, streamed=False
        ))

        html_sends = [s for s in sent if s.get("parse_mode") == "HTML"]
        assert html_sends, "Code block should be sent as HTML"
        kb_sends = [s for s in sent if s.get("markup") is not None]
        assert kb_sends, "Keyboard should be sent"

        # Keyboard comes after all content messages.
        last_kb_idx = max(i for i, s in enumerate(sent) if s.get("markup") is not None)
        assert all(
            i < last_kb_idx
            for i, s in enumerate(sent)
            if s.get("markup") is None
        ), "Content must precede keyboard"

    def test_code_only_no_duplicate_send(self):
        """Streamed code block without [[OPTIONS]] -- no keyboard, no double-send."""
        bot_mod = _load_bot()
        bot = self._make_bot()
        sent = []
        msg = _make_message(sent)
        draft_ids = [202]

        content = "```python\nfor x in range(5):\n    print(x)\n```\n"

        _run(bot._reply_smart(
            msg, content,
            force_options=False, streamed=True, draft_message_ids=draft_ids
        ))

        # Draft deleted and body re-sent (code-only streamed path).
        msg.get_bot.return_value.delete_message.assert_called_once_with(msg.chat.id, 202)
        html_sends = [s for s in sent if s.get("parse_mode") == "HTML"]
        assert html_sends, "Code block should be HTML"

        # No keyboard.
        assert not any(s.get("markup") is not None for s in sent), (
            "No keyboard should be sent for options-free content"
        )

    def test_options_only_no_code_no_resend(self):
        """Streamed authored-marker options draft: deleted + list button-only.

        Expectation updated at DGN-665 landing (DGN-696 merge): the streamed
        decision-ask draft bakes in the unstripped list, so _reply_smart now
        deletes the draft and shows the choices as buttons only. The prose
        lead-in is re-sent stripped; the list itself never re-sends as body.
        """
        bot_mod = _load_bot()
        bot = self._make_bot()
        sent = []
        msg = _make_message(sent)
        draft_ids = [303]

        content = "Pick one:\n\n1. Go\n2. Stop\n[[OPTIONS]]"

        _run(bot._reply_smart(
            msg, content,
            force_options=True, streamed=True, draft_message_ids=draft_ids
        ))

        # DGN-665: the draft (with the baked-in list) is deleted.
        msg.get_bot.return_value.delete_message.assert_called_once()

        # Keyboard should be sent.
        kb_sends = [s for s in sent if s.get("markup") is not None]
        assert kb_sends, "Keyboard should still be sent for options-only content"

        # The numbered list must not re-send as body text (button-only).
        body_sends = [s for s in sent if s.get("markup") is None]
        assert all("1. Go" not in (s.get("text") or "") for s in body_sends), (
            "Consumed options list must not reappear in the body"
        )


# ---------------------------------------------------------------------------
# Test: extraction fix (DGN-085 part 1, re-confirmed after framework regression)
# ---------------------------------------------------------------------------

class TestExtractionAfterRegression:
    """Confirm the extraction fix is in place (was lost in framework v1.2.1 update)."""

    def test_extract_options_ignores_fenced_code_numbers(self):
        from bridge.options import extract_options
        content = (
            "```python\n1. step_one()\n2. step_two()\n```\n\n"
            "1. Run it\n2. Skip it\n"
        )
        assert extract_options(content) == ["Run it", "Skip it"]

    def test_has_numbered_list_ignores_fenced_code(self):
        from bridge.options import has_numbered_list
        assert not has_numbered_list("```\n1. a\n2. b\n```\n")

    def test_combined_regression(self):
        """Code block with 3 items then 2 prose options -- worst-case regression."""
        from bridge.options import extract_options
        content = (
            "```\n1. alpha\n2. beta\n3. gamma\n```\n\n"
            "1. Do this\n2. Do that\n"
        )
        assert extract_options(content) == ["Do this", "Do that"]
