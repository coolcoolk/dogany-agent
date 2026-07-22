"""DGN-517: JSON buffer-overflow crash must not kill the reader loop.

Two-pronged fix:
  (a) CLAUDE_MAX_BUFFER_SIZE is wired into ClaudeAgentOptions so the 16MB
      ceiling replaces the SDK default of 1MB (prevents most overflows).
  (b) "maximum buffer size" added to _RETRYABLE_MSG so if an overflow still
      occurs at runtime the error routes through _reconnect_and_retry instead
      of killing the reader loop and losing the session.

Test coverage:
  1. _is_retryable_sdk_error classifies the observed error string as retryable.
  2. A plain ValueError / TypeError (control) is NOT retryable.
  3. The overflow error string does NOT match any _NON_RETRYABLE prefix.
  4. CLAUDE_MAX_BUFFER_SIZE > 1MB (SDK default) -- the ceiling was actually raised.
  5. CLAUDE_MAX_BUFFER_SIZE is passed into ClaudeAgentOptions as max_buffer_size.
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bridge.sdk_bridge import SdkBridge, _is_retryable_sdk_error, _RETRYABLE_MSG
from bridge.config import CLAUDE_MAX_BUFFER_SIZE


# ---------------------------------------------------------------------------
# Exact error string observed in Warg logs on 2026-07-20 and 2026-07-21
# ---------------------------------------------------------------------------
_OBSERVED_ERROR = (
    "Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"
)

# SDK_DEFAULT is what subprocess_cli.py uses when max_buffer_size is not set.
_SDK_DEFAULT_BYTES = 1024 * 1024  # 1MB


class TestBufferOverflowRetryable(unittest.TestCase):
    """_is_retryable_sdk_error must classify the buffer overflow as retryable."""

    def test_observed_error_string_is_retryable(self):
        """The exact error from Warg logs must be classified retryable."""
        err = Exception(_OBSERVED_ERROR)
        self.assertTrue(
            _is_retryable_sdk_error(err),
            f"Expected retryable for: {_OBSERVED_ERROR}",
        )

    def test_any_maximum_buffer_size_error_is_retryable(self):
        """Variant with a different byte count must also be retryable."""
        err = Exception(
            "Failed to decode JSON: JSON message exceeded maximum buffer size of 16777216 bytes"
        )
        self.assertTrue(_is_retryable_sdk_error(err))

    def test_retryable_msg_contains_buffer_key(self):
        """'maximum buffer size' must appear in _RETRYABLE_MSG."""
        self.assertIn(
            "maximum buffer size",
            _RETRYABLE_MSG,
            "_RETRYABLE_MSG must include 'maximum buffer size' (DGN-517)",
        )


class TestControlErrorsNotRetryable(unittest.TestCase):
    """Non-buffer exceptions that were not retryable before must stay non-retryable."""

    def test_value_error_not_retryable(self):
        err = Exception("ValueError: bad value")
        self.assertFalse(_is_retryable_sdk_error(err))

    def test_type_error_not_retryable(self):
        err = Exception("TypeError: wrong type")
        self.assertFalse(_is_retryable_sdk_error(err))

    def test_invalid_token_not_retryable(self):
        err = Exception("Invalid token provided")
        self.assertFalse(_is_retryable_sdk_error(err))

    def test_permission_denied_not_retryable(self):
        err = Exception("Permission denied: path")
        self.assertFalse(_is_retryable_sdk_error(err))


class TestBufferSizeCeiling(unittest.TestCase):
    """CLAUDE_MAX_BUFFER_SIZE must exceed the SDK 1MB default."""

    def test_configured_ceiling_exceeds_sdk_default(self):
        """16MB ceiling (or whatever is set) must be > SDK default 1MB."""
        self.assertGreater(
            CLAUDE_MAX_BUFFER_SIZE,
            _SDK_DEFAULT_BYTES,
            f"CLAUDE_MAX_BUFFER_SIZE={CLAUDE_MAX_BUFFER_SIZE} must exceed SDK "
            f"default {_SDK_DEFAULT_BYTES}",
        )

    def test_buffer_size_is_positive(self):
        self.assertGreater(CLAUDE_MAX_BUFFER_SIZE, 0)


class TestBufferSizeWiredIntoOptions(unittest.TestCase):
    """_create_user_stream must pass max_buffer_size to ClaudeAgentOptions."""

    def test_max_buffer_size_passed_to_sdk(self):
        """ClaudeAgentOptions must receive max_buffer_size=CLAUDE_MAX_BUFFER_SIZE."""
        captured_opts = {}

        def fake_options(**kwargs):
            captured_opts.update(kwargs)
            m = MagicMock()
            return m

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with (
            patch("bridge.sdk_bridge.ClaudeAgentOptions", side_effect=fake_options),
            patch("bridge.sdk_bridge.ClaudeSDKClient", return_value=mock_client),
            patch("bridge.sdk_bridge.asyncio.create_task"),
        ):
            bridge = SdkBridge()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(bridge._create_user_stream(user_id=42, model=None))
            except Exception:
                pass  # partial init is fine; opts capture is the goal
            finally:
                loop.close()

        self.assertIn(
            "max_buffer_size",
            captured_opts,
            "ClaudeAgentOptions must receive a max_buffer_size kwarg",
        )
        self.assertEqual(
            captured_opts["max_buffer_size"],
            CLAUDE_MAX_BUFFER_SIZE,
            "max_buffer_size passed to SDK must equal CLAUDE_MAX_BUFFER_SIZE",
        )


if __name__ == "__main__":
    unittest.main()
