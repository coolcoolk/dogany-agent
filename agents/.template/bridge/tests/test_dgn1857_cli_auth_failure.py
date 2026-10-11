"""DGN-1857: the CLI's auth-failure text reached the owner verbatim.

Measured 2026-10-04 (Dogany 2.5.0): Telegram showed "Failed to authenticate:
OAuth session expired and could not be refreshed" twice, raw. Claude Code
reports an auth failure as a SYNTHETIC assistant message (model "<synthetic>",
error="authentication_failed"), not as an is_error result, so the text took
the ordinary answer path: streamed live (terminal / inline) and assembled
into the final body. At 73 chars it slips under the register guard's floor.

Fix under test: the message is recognized at ingestion (SDK error flag or a
narrow start-anchored text signature), kept off every owner surface, and the
turn finishes exactly like an is_error auth result -- the re-login notice, no
retry offer, the raw text in the log only. The no-pending path pushes the
same notice. A normal answer that mentions OAuth is untouched.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bridge import messages
from bridge import sdk_bridge as sdk
from bridge.sdk_bridge import SdkBridge, _UserStreamState
from bridge.tests.test_dgn1651_stop_block_duplicate import (
    _asst,
    _result,
    _tool,
    run_turn,
)

OBSERVED = "Failed to authenticate: OAuth session expired and could not be refreshed"
NARRATION = "파일을 읽어 볼게요"
OAUTH_ANSWER = (
    "OAuth 토큰은 보통 몇 시간 뒤 만료되고, CLI가 알아서 갱신해요. "
    "Failed to authenticate 같은 오류가 보이면 다시 로그인하면 됩니다."
)


def _synthetic(text=OBSERVED, stop_reason=None, error=None):
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="<synthetic>",
        stop_reason=stop_reason,
        parent_tool_use_id=None,
        error=error,
    )


def _surface_text(surface):
    return "\n".join(surface)


class TestSignature(unittest.TestCase):
    def test_cli_strings_match(self):
        for text in (
            OBSERVED,
            "Failed to authenticate. API Error: 401 "
            '{"type":"error","error":{"type":"authentication_error"}}',
            "Failed to authenticate: OAuth token revoked. Please log in again "
            "or contact your administrator.",
            "Login expired · Please run /login",
            "OAuth token revoked · Please run /login",
            "Not logged in · Please run /login",
            "Please run /login · API Error: 401 bad token",
            "Invalid API key · Fix external API key",
            "API Error: 401 {\"error\":{\"message\":\"OAuth token has expired.\"}}",
        ):
            self.assertTrue(sdk._is_cli_auth_failure_text(text), text)
            self.assertEqual(sdk._classify_error_result(text), "auth", text)

    def test_answers_that_mention_auth_do_not_match(self):
        for text in (
            OAUTH_ANSWER,
            "The OAuth token has a refresh step; Failed to authenticate means it broke.",
            "Please run /login in Claude Code to sign in.",
            "Invalid API key errors usually mean the key was rotated.",
            "Failed to authenticate" + " x" * 300,  # long body, not a CLI line
            "",
        ):
            self.assertFalse(sdk._is_cli_auth_failure_text(text), text)

    def test_sdk_error_flag_matches_without_signature(self):
        msg = _synthetic(text="Some new wording", error="authentication_failed")
        self.assertEqual(sdk._cli_auth_failure(msg), "Some new wording")
        self.assertIsNone(sdk._cli_auth_failure(_synthetic(text=OAUTH_ANSWER)))


class TestPendingTurn(unittest.TestCase):
    def _assert_notice_only(self, response, live, final):
        self.assertEqual(response.content, messages.ERROR_AUTH_RELOGIN)
        self.assertFalse(response.success)
        self.assertFalse(response.retry_offer)
        self.assertEqual(response.error_kind, "auth")
        self.assertIn(OBSERVED, response.error)  # raw text for the log only
        for surface in (live, final):
            self.assertNotIn("Failed to authenticate", _surface_text(surface))
        self.assertIn("claude auth login", _surface_text(final))

    def test_observed_string_on_the_non_error_final_path(self):
        # Suppress mode: nothing streams for a non-terminal message; the text
        # reached the owner through the final assembly.
        with self.assertLogs(sdk.logger, "WARNING") as logs:
            response, live, final, _ = run_turn(
                [_synthetic(), _result(result=OBSERVED)], interim_mode="suppress"
            )
        self._assert_notice_only(response, live, final)
        self.assertEqual(live, [])
        self.assertTrue(any(OBSERVED in line for line in logs.output))

    def test_observed_string_on_the_streamed_path(self):
        # Inline mode, narration already live, then the synthetic message as
        # a terminal block: pre-fix it streamed into the live bubble.
        response, live, final, _ = run_turn(
            [
                _asst("tool_use", [TextBlock(text=NARRATION), _tool()]),
                _synthetic(stop_reason="end_turn"),
                _result(result=OBSERVED),
            ],
            interim_mode="inline",
        )
        self._assert_notice_only(response, live, final)
        self.assertTrue(response.streamed)
        self.assertEqual(final, [messages.ERROR_AUTH_RELOGIN.replace(
            "`claude auth login`", "<code>claude auth login</code>").replace(
            "`security unlock-keychain ~/Library/Keychains/login.keychain-db`",
            "<code>security unlock-keychain ~/Library/Keychains/login.keychain-db</code>",
        )])

    def test_sdk_error_flag_with_is_error_result(self):
        response, live, final, _ = run_turn(
            [
                _synthetic(stop_reason="end_turn", error="authentication_failed"),
                _result(result=OBSERVED, is_error=True),
            ],
            interim_mode="fold",
        )
        self._assert_notice_only(response, live, final)

    def test_normal_answer_mentioning_oauth_is_unchanged(self):
        response, _live, final, _ = run_turn(
            [_asst("end_turn", [TextBlock(text=OAUTH_ANSWER)]), _result()],
            interim_mode="inline",
        )
        self.assertTrue(response.success)
        self.assertEqual(response.content, OAUTH_ANSWER)
        self.assertIsNone(response.error_kind)
        self.assertEqual(final, [OAUTH_ANSWER])

    def test_is_error_auth_result_unchanged(self):
        response, _live, _final, _ = run_turn(
            [_result(result="HTTP 401 invalid_api_key", is_error=True)],
            interim_mode="inline",
        )
        self.assertEqual(response.content, messages.ERROR_AUTH_RELOGIN)
        self.assertFalse(response.retry_offer)
        self.assertEqual(response.error_kind, "auth")
        self.assertEqual(response.error, "HTTP 401 invalid_api_key")


def _proactive(seq):
    push = AsyncMock()
    state = _UserStreamState(client=MagicMock(), model=None)
    state.last_chat_id = 11
    state.proactive_push = push
    bridge = SdkBridge()

    async def _go():
        for m in seq:
            await bridge._handle_proactive_message(7, state, m)

    asyncio.run(_go())
    return state, [c.args[1] for c in push.await_args_list]


def _plain_result():
    rm = MagicMock(spec=ResultMessage)
    rm.session_id = "sess-1"
    rm.is_error = False
    rm.result = OBSERVED
    rm.num_turns = 1
    return rm


class TestNoPendingTurn(unittest.TestCase):
    def test_proactive_auth_failure_pushes_the_notice(self):
        state, pushed = _proactive([_synthetic(), _plain_result()])
        self.assertEqual(pushed, [messages.ERROR_AUTH_RELOGIN])
        self.assertFalse(state.proactive_auth_failure)
        self.assertEqual(state.proactive_texts, [])

    def test_proactive_oauth_answer_is_unchanged(self):
        _, pushed = _proactive([_synthetic(text=OAUTH_ANSWER), _plain_result()])
        self.assertEqual(len(pushed), 1)
        self.assertNotEqual(pushed[0], messages.ERROR_AUTH_RELOGIN)
        self.assertIn("OAuth", pushed[0])


class TestApprovedCopy(unittest.TestCase):
    """dec-256: the re-login copy is owner-confirmed verbatim in BOTH
    catalogs. Any rewording is a new owner decision, not a code edit."""

    def test_ko_copy_is_the_approved_text(self):
        from bridge.i18n import ko

        self.assertEqual(
            ko.STRINGS["error_auth_relogin"],
            "Claude 로그인이 만료됐어요. 이 맥의 터미널에서 `claude auth login` 을 "
            "실행한 뒤 /restart 를 보내 주세요. 원격(SSH)으로 접속했다면 그 전에 "
            "`security unlock-keychain ~/Library/Keychains/login.keychain-db` 를 "
            "먼저 실행해 주세요.",
        )

    def test_en_copy_is_the_approved_text(self):
        from bridge.i18n import en

        self.assertEqual(
            en.STRINGS["error_auth_relogin"],
            "Your Claude login has expired. On this Mac, run `claude auth login` "
            "in Terminal, then send /restart. If you are connected remotely (SSH), "
            "first run `security unlock-keychain ~/Library/Keychains/login.keychain-db`.",
        )


if __name__ == "__main__":
    unittest.main()
