"""DGN-2018 selftest: a bare NO_PUSH leaves the existing card/buttons last."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import sdk_bridge as sdk
from bridge.bot import TelegramBot
from bridge.i18n.en import STRINGS as EN
from bridge.i18n.ko import STRINGS as KO
from bridge.sdk_bridge import SdkBridge, _PendingRequest, _UserStreamState


def _seat():
    bot = TelegramBot.__new__(TelegramBot)
    bot._send_text_body = AsyncMock(return_value=[])
    bot._send_content_artifacts = AsyncMock()
    bot._kick_update_offer = MagicMock()
    # ESTATE:BEGIN the reply path's idrill prose gate is an estate seam (bot.py region, DGN-924); the public _reply_smart never calls it
    bot._idrill_prose_gate = lambda text, *_: (text, False)
    # ESTATE:END
    bot._term_dict_gate = lambda text, *_: (text, False)
    bot._reply_link_id = lambda _: None
    transport = SimpleNamespace(delete_message=AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(id=11), get_bot=lambda: transport)
    return bot, message, transport


@pytest.mark.parametrize("content", ["NO_PUSH", " \n NO_PUSH \n"])
@pytest.mark.parametrize("drafts", [None, [91]])
def test_reply_seat_silent_exit(content, drafts):
    bot, message, transport = _seat()
    asyncio.run(bot._reply_smart(message, content, streamed=bool(drafts),
                                 draft_message_ids=drafts))
    bot._send_text_body.assert_not_awaited()
    bot._send_content_artifacts.assert_not_awaited()
    bot._kick_update_offer.assert_not_called()
    assert transport.delete_message.await_count == len(drafts or [])


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
def test_live_finalize_to_reply_preserves_last_screen(fallback, streamed):
    async def scenario():
        bridge = SdkBridge()
        state = _UserStreamState(client=MagicMock(), model=None)
        req = _PendingRequest(
            user_id=7, chat_id=11, model=None, requested_session_id=None,
            permission_callback=None, typing_callback=None,
            future=asyncio.get_running_loop().create_future(), user_message="next",
        )
        req.last_assistant_texts = [] if fallback else ["NO_PUSH"]
        # A silent final must not turn buffered narration into a fresh bubble.
        req.interim_texts = ["The table and card were sent."]
        if streamed:
            req.streaming_handler = SimpleNamespace(
                drafts=[SimpleNamespace(message_id=91)],
                cancel=AsyncMock(), finalize_all=AsyncMock(),
            )
        result = SimpleNamespace(is_error=False, result="NO_PUSH",
                                 session_id="silent-session")
        bridge._maybe_mark_options = AsyncMock(return_value=("NO_PUSH", False))
        with patch.object(
            sdk, "_effective_interim_mode", return_value="fold"
        ), patch.object(sdk.notice_spool, "reserve_for_synthesis") as reserve:
            reserve.return_value = {"record": {"id": "pending", "body_oneline": "Update ready."}}
            retry = await bridge._finalize_result(7, state, req, result)
        assert retry is False
        assert req.future.done()
        response = req.future.result()
        assert response.success and not response.has_options
        assert response.notice_id is None
        assert response.session_id == "silent-session"
        reserve.assert_not_called()
        bridge._maybe_mark_options.assert_not_awaited()
        if streamed:
            req.streaming_handler.cancel.assert_awaited_once()
            req.streaming_handler.finalize_all.assert_not_awaited()
        bot, message, transport = _seat()
        await bot._reply_smart(message, response.content,
                               force_options=response.has_options,
                               streamed=response.streamed,
                               draft_message_ids=response.draft_message_ids)
        # No final text, menu carry, update offer, or unrelated deletion can
        # displace the already-delivered table -> card -> buttons.
        bot._send_text_body.assert_not_awaited()
        bot._send_content_artifacts.assert_not_awaited()
        bot._kick_update_offer.assert_not_called()
        transport.delete_message.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("content", ["Answer.", "Answer.\nNO_PUSH", "NO_PUSH\nAnswer."])
def test_substantive_live_reply_still_delivers(content):
    bot, message, _ = _seat()
    asyncio.run(bot._reply_smart(message, content))
    assert bot._send_text_body.await_args.args[1] == "Answer."
    bot._send_content_artifacts.assert_awaited_once()


def test_error_result_is_not_silenced():
    async def scenario():
        bridge = SdkBridge()
        req = _PendingRequest(
            user_id=7, chat_id=11, model=None, requested_session_id=None,
            permission_callback=None, typing_callback=None,
            future=asyncio.get_running_loop().create_future(), user_message="next",
        )
        state = _UserStreamState(client=MagicMock(), model=None)
        result = SimpleNamespace(is_error=True, result="NO_PUSH", session_id="error")
        bridge._fold_finalize = AsyncMock()
        await bridge._finalize_result(7, state, req, result)
        response = req.future.result()
        assert not response.success
        assert response.content and response.content != "NO_PUSH"
    asyncio.run(scenario())


def test_injected_contract_documents_silent_final():
    assert EN["system_prompt"] == KO["system_prompt"]
    assert "end a live text turn silently" in EN["system_prompt"]
    assert "NO_PUSH" in sdk.messages.SYSTEM_PROMPT
