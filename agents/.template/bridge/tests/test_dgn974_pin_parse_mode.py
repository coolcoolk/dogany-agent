"""DGN-974: pin (dashboard) surface HTML render + fail-open parse-mode tests.

Root cause: dashboard.py's two send sites (edit_message_text / send_message)
never set parse_mode, so the pin always rendered as plain text (no code
blocks, no bold). The fix routes pin text through the SAME converter the
chat path uses (bridge.formatting.sanitize_message_for_telegram) and sends
with parse_mode="HTML" at both sites, with a fail-open plain-text fallback
when conversion raises OR Telegram rejects the HTML with a parse-entity
BadRequest -- the pin must never stop updating over a formatting problem.

Covers (spec success criteria):
  a) a fenced code block renders as HTML <pre> and is sent with parse_mode
  b) raw '<' and '&' in prose (e.g. a ticket title) does not blow up and is
     still sent (escaped)
  c) conversion raising -> plain-text fallback send, message still
     delivered, bookkeeping correct
  d) Telegram raising a parse-error BadRequest -> plain-text retry succeeds
  e) a NON-parse BadRequest ("message is not modified") is NOT swallowed by
     the new fallback -- exactly one call, pre-existing behavior preserved
Both the edit site (_sync, existing pinned message) and the send site
(_recreate, fresh pin) are covered.
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from bridge.dashboard import DashboardSync, _is_parse_error

OWNER_ID = 42


def _mock_bot():
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    bot.unpin_chat_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# _is_parse_error classifier
# ---------------------------------------------------------------------------

class TestIsParseError(unittest.TestCase):
    def _e(self, msg):
        return Exception(msg)

    def test_parse_entities_true(self):
        self.assertTrue(
            _is_parse_error(
                self._e("Can't parse entities: unsupported start tag \"x\" at byte offset 3")
            )
        )

    def test_parse_entities_case_insensitive(self):
        self.assertTrue(_is_parse_error(self._e("CAN'T PARSE ENTITIES: bad tag")))

    def test_not_modified_is_not_a_parse_error(self):
        self.assertFalse(
            _is_parse_error(self._e("message is not modified: content and reply"))
        )

    def test_unrelated_bad_request_is_not_a_parse_error(self):
        self.assertFalse(_is_parse_error(self._e("chat not found")))


# ---------------------------------------------------------------------------
# Shared harness for _sync (edit) / _recreate (send) fixtures
# ---------------------------------------------------------------------------

class _DashboardCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="dash-dgn974-")
        self._dir = Path(self._td.name)
        self._bot = _mock_bot()
        self._ds = DashboardSync(
            bot=self._bot,
            turn_active=lambda uid: False,
            dashboard_path=self._dir / "dashboard.md",
            state_path=self._dir / "state.json",
        )

    def tearDown(self):
        self._td.cleanup()

    def _sync(self, text, chat_id=OWNER_ID, mtime=100.0):
        asyncio.run(self._ds._sync(chat_id, text, mtime))

    def _recreate(self, text, now=None):
        if now is None:
            now = time.monotonic()
        asyncio.run(self._ds._recreate(OWNER_ID, text, 100.0, now))
        return now


# ---------------------------------------------------------------------------
# a) fenced code block -> HTML <pre> + parse_mode="HTML", both send sites
# ---------------------------------------------------------------------------

class TestCodeBlockRendersHtml(_DashboardCase):
    CONTENT = "종목표\n```\nAAPL 10주\n```"

    def test_edit_site_sends_pre_with_parse_mode(self):
        self._ds._message_id = 1111
        self._sync(self.CONTENT)
        kwargs = self._bot.edit_message_text.await_args.kwargs
        self.assertIn("<pre>", kwargs["text"])
        self.assertIn("AAPL 10주", kwargs["text"])
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertFalse(self._ds._dirty)

    def test_send_site_sends_pre_with_parse_mode(self):
        fake_msg = MagicMock()
        fake_msg.message_id = 9999
        self._bot.send_message = AsyncMock(return_value=fake_msg)
        self._recreate(self.CONTENT)
        kwargs = self._bot.send_message.await_args.kwargs
        self.assertIn("<pre>", kwargs["text"])
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertFalse(self._ds._dirty)


# ---------------------------------------------------------------------------
# b) raw '<' / '&' in prose (e.g. ticket title) -> escaped, not a crash
# ---------------------------------------------------------------------------

class TestAngleAmpersandEscaped(_DashboardCase):
    CONTENT = "[진행중 티켓] DGN-1 <urgent> A & B fix"

    def test_edit_site_escapes_and_sends(self):
        self._ds._message_id = 1111
        self._sync(self.CONTENT)  # must not raise
        kwargs = self._bot.edit_message_text.await_args.kwargs
        self.assertIn("&lt;urgent&gt;", kwargs["text"])
        self.assertIn("A &amp; B", kwargs["text"])
        self.assertNotIn("<urgent>", kwargs["text"])
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertFalse(self._ds._dirty)


# ---------------------------------------------------------------------------
# c) conversion raises -> fail-open plain-text send, bookkeeping correct
# ---------------------------------------------------------------------------

class TestConversionFailsOpen(_DashboardCase):
    def test_edit_site_falls_back_to_plain_on_conversion_error(self):
        self._ds._message_id = 1111
        with patch(
            "bridge.dashboard.sanitize_message_for_telegram",
            side_effect=RuntimeError("boom"),
        ):
            self._sync("plain content")
        kwargs = self._bot.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["text"], "plain content")
        self.assertNotIn("parse_mode", kwargs)
        # Fallback success must mark synced exactly like a normal success.
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._ds._last_synced_mtime, 100.0)

    def test_send_site_falls_back_to_plain_on_conversion_error(self):
        fake_msg = MagicMock()
        fake_msg.message_id = 9999
        self._bot.send_message = AsyncMock(return_value=fake_msg)
        with patch(
            "bridge.dashboard.balance_telegram_html",
            side_effect=RuntimeError("boom"),
        ):
            self._recreate("plain content")
        kwargs = self._bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["text"], "plain content")
        self.assertNotIn("parse_mode", kwargs)
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._ds._message_id, 9999)


# ---------------------------------------------------------------------------
# d) Telegram parse-error BadRequest -> plain-text retry succeeds
# ---------------------------------------------------------------------------

class TestParseErrorRetrySucceeds(_DashboardCase):
    def test_edit_site_retries_plain_and_marks_synced(self):
        self._ds._message_id = 1111
        parse_err = telegram.error.BadRequest(
            "Can't parse entities: unsupported start tag \"z\" at byte offset 0"
        )
        self._bot.edit_message_text = AsyncMock(side_effect=[parse_err, MagicMock()])
        self._sync("some **content**")
        self.assertEqual(self._bot.edit_message_text.await_count, 2)
        first_kwargs = self._bot.edit_message_text.await_args_list[0].kwargs
        second_kwargs = self._bot.edit_message_text.await_args_list[1].kwargs
        self.assertEqual(first_kwargs["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second_kwargs)
        self.assertEqual(second_kwargs["text"], "some **content**")
        # Fallback success must mark synced exactly like a normal success.
        self.assertFalse(self._ds._dirty)
        self.assertEqual(self._ds._last_synced_mtime, 100.0)

    def test_send_site_retries_plain_and_succeeds(self):
        fake_msg = MagicMock()
        fake_msg.message_id = 8888
        parse_err = telegram.error.BadRequest(
            "Can't parse entities: unclosed start tag \"b\" at byte offset 2"
        )
        self._bot.send_message = AsyncMock(side_effect=[parse_err, fake_msg])
        self._recreate("some **content**")
        self.assertEqual(self._bot.send_message.await_count, 2)
        first_kwargs = self._bot.send_message.await_args_list[0].kwargs
        second_kwargs = self._bot.send_message.await_args_list[1].kwargs
        self.assertEqual(first_kwargs["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second_kwargs)
        self.assertEqual(self._ds._message_id, 8888)
        self.assertFalse(self._ds._dirty)


# ---------------------------------------------------------------------------
# e) a NON-parse BadRequest is NOT swallowed -- pre-existing behavior kept
# ---------------------------------------------------------------------------

class TestNonParseBadRequestUntouched(_DashboardCase):
    def test_not_modified_still_one_call_and_marks_synced(self):
        """'message is not modified' must NOT trigger a plain-text retry --
        the fallback is scoped to parse errors only. Exactly one edit call,
        same as before parse_mode existed."""
        self._ds._message_id = 1111
        err = telegram.error.BadRequest("message is not modified: content and reply")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        self._sync("content")
        self.assertEqual(self._bot.edit_message_text.await_count, 1)
        self.assertFalse(self._ds._dirty)  # existing not-modified == synced path

    def test_needs_recreate_still_one_call_and_falls_through(self):
        """A non-parse BadRequest that signals 'message no longer editable'
        must still fall through to _recreate exactly as before -- no plain
        retry attempted at the edit site for this error class."""
        self._ds._message_id = 1111
        err = telegram.error.BadRequest("message to edit not found")
        self._bot.edit_message_text = AsyncMock(side_effect=err)
        recreate = AsyncMock()
        with patch.object(self._ds, "_recreate", recreate):
            self._sync("content")
        self.assertEqual(self._bot.edit_message_text.await_count, 1)
        recreate.assert_awaited_once()

    def test_send_site_generic_bad_request_not_retried(self):
        """A non-parse BadRequest at the send site propagates unchanged
        (single call) -- matches the pre-existing recreate-send error path."""
        err = telegram.error.BadRequest("chat not found")
        self._bot.send_message = AsyncMock(side_effect=err)
        self._ds._chat_id = OWNER_ID
        self._ds._message_id = 1111
        self._recreate("content")
        self.assertEqual(self._bot.send_message.await_count, 1)
        self.assertIsNone(self._ds._chat_id)  # existing chat-gone handling


if __name__ == "__main__":
    unittest.main()
