"""DGN-1736 slice 0: the owner-send transport belt, observe mode.

OwnerGuardRequest sits on the application's general HTTPXRequest (the same
PTB seam as heartbeat.HeartbeatHTTPXRequest). Contract pinned here:
- each owner-text endpoint carrying a directive line logs OWNER_BELT_HIT;
- clean text logs nothing;
- the request is forwarded byte-identical in every case (observe only);
- non-owner endpoints are not inspected;
- a scan error never blocks the send.
routines/push.sh is out of scope (separate process, own HTTP client).
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.request import HTTPXRequest, RequestData
from telegram.request._requestparameter import RequestParameter

import bridge.tests.conftest  # noqa: F401 -- hermetic bridge environment
from bridge import owner_belt
from bridge.owner_belt import OWNER_TEXT_FIELDS, OwnerGuardRequest, scan_text

URL = "https://api.telegram.org/bottest:token/"
LEAKS = {
    "NO_PUSH": "result line\n\nNO_PUSH",
    "PUSH": "result line\nPUSH",
    "OPTIONS": "pick one\n1. a\n2. b\n[[OPTIONS]]",
    # ESTATE:BEGIN idrill directive shape (owner_belt._ESTATE_LINE_SHAPES)
    "IDRILL": "body\n[[IDRILL:arm-1]]",
    # ESTATE:END
    "send_file": "body\nsend_file:: /tmp/out.md",
    "link_preview": "body\nlink_preview:: https://example.com",
    "toolcall": 'body &lt;invoke name="Bash"&gt;x&lt;/invoke&gt;',
}
CLEAN = [
    "plain answer",
    "<b>Result</b>: done.\nPUSH notifications are off.",
    "say NO_PUSH unless needed",
    "see [[OPTIONS]] docs inline",
    "send_file is a marker name",
    "",
]


def _data(field, value):
    return RequestData([
        RequestParameter.from_input("chat_id", 11),
        RequestParameter.from_input(field, value),
    ])


def _drive(endpoint, data):
    """Run OwnerGuardRequest.do_request with the network half stubbed."""
    inner = AsyncMock(return_value=(200, b'{"ok":true,"result":true}'))
    req = OwnerGuardRequest()
    with patch.object(HTTPXRequest, "do_request", inner):
        result = asyncio.run(req.do_request(URL + endpoint, "POST", request_data=data))
    return inner, result


def _hits(caplog):
    return [r.getMessage() for r in caplog.records if "OWNER_BELT_HIT" in r.getMessage()]


@pytest.mark.parametrize("endpoint,field", sorted(OWNER_TEXT_FIELDS.items()))
def test_each_owner_endpoint_logs_a_hit_and_forwards_unchanged(caplog, endpoint, field):
    data = _data(field, "result line\n\nNO_PUSH")
    before = data.json_payload
    with caplog.at_level(logging.WARNING, logger=owner_belt.__name__):
        inner, result = _drive(endpoint, data)
    assert _hits(caplog) == [
        f"OWNER_BELT_HIT path={endpoint} field={field} shape=NO_PUSH"
    ]
    # Observe mode: the very same object goes out, byte-identical.
    assert inner.await_count == 1
    assert inner.await_args.kwargs["request_data"] is data
    assert data.json_payload == before
    assert json.loads(before)[field] == "result line\n\nNO_PUSH"
    assert result == (200, b'{"ok":true,"result":true}')


# The IDRILL shape lives in an ESTATE region; the public build has no row.
SHAPES = sorted(s for s in LEAKS if s == "toolcall" or s in dict(owner_belt._LINE_SHAPES))


def test_core_shapes_present():
    assert {"NO_PUSH", "PUSH", "OPTIONS", "send_file", "link_preview", "toolcall"} <= set(SHAPES)


@pytest.mark.parametrize("shape", SHAPES)
def test_every_shape_is_reported(caplog, shape):
    data = _data("text", LEAKS[shape])
    before = data.json_payload
    with caplog.at_level(logging.WARNING, logger=owner_belt.__name__):
        inner, _ = _drive("sendMessage", data)
    assert f"OWNER_BELT_HIT path=sendMessage field=text shape={shape}" in _hits(caplog)
    assert data.json_payload == before
    assert inner.await_args.kwargs["request_data"] is data


@pytest.mark.parametrize("text", CLEAN)
def test_clean_text_logs_nothing(caplog, text):
    data = _data("text", text)
    before = data.json_payload
    with caplog.at_level(logging.DEBUG, logger=owner_belt.__name__):
        inner, _ = _drive("sendMessage", data)
    assert caplog.records == []
    assert data.json_payload == before
    assert inner.await_count == 1


def test_html_wrapped_directive_line_still_counts():
    assert scan_text("body\n<b>NO_PUSH</b>") == ["NO_PUSH"]
    assert scan_text("pick\n[[OPTIONS: yes|no]]") == ["OPTIONS"]


def test_non_owner_endpoint_is_not_inspected(caplog):
    data = _data("text", "NO_PUSH")
    with caplog.at_level(logging.DEBUG, logger=owner_belt.__name__):
        inner, _ = _drive("deleteMessage", data)
    assert caplog.records == []
    assert inner.await_count == 1


def test_scan_error_never_blocks_the_send(caplog):
    broken = MagicMock()
    type(broken).parameters = property(lambda self: (_ for _ in ()).throw(ValueError("boom")))
    with caplog.at_level(logging.WARNING, logger=owner_belt.__name__):
        inner, result = _drive("sendMessage", broken)
    assert inner.await_count == 1
    assert inner.await_args.kwargs["request_data"] is broken
    assert result[0] == 200
    assert any("OWNER_BELT_SCAN_ERROR path=sendMessage" in r.getMessage() for r in caplog.records)
    assert _hits(caplog) == []


def test_endpoint_log_never_carries_the_token():
    assert owner_belt.endpoint_of("https://api.telegram.org/bot123:SECRET/sendMessage") == "sendMessage"


def test_real_bot_calls_ride_the_belt(caplog):
    """End to end through PTB: Bot.send_message / answer_callback_query build
    the request, the belt sees it, the text reaches the transport as sent."""
    message = {"message_id": 1, "date": 0, "chat": {"id": 11, "type": "private"},
               "text": "x"}
    seen = []

    async def fake_transport(self, url, method, request_data=None, **kwargs):
        seen.append((owner_belt.endpoint_of(url), dict(request_data.parameters)))
        if url.endswith("answerCallbackQuery"):
            return 200, b'{"ok":true,"result":true}'
        return 200, json.dumps({"ok": True, "result": message}).encode()

    async def run():
        bot = Bot("test:token", request=OwnerGuardRequest())
        await bot.send_message(11, "result line\nNO_PUSH")
        await bot.answer_callback_query("cb-1", text="expired: [[OPTIONS]]\n[[OPTIONS]]",
                                        show_alert=True)
        await bot.send_message(11, "clean answer")

    with patch.object(HTTPXRequest, "do_request", fake_transport), \
            caplog.at_level(logging.WARNING, logger=owner_belt.__name__):
        asyncio.run(run())
    assert _hits(caplog) == [
        "OWNER_BELT_HIT path=sendMessage field=text shape=NO_PUSH",
        "OWNER_BELT_HIT path=answerCallbackQuery field=text shape=OPTIONS",
    ]
    assert [s[1]["text"] for s in seen] == [
        "result line\nNO_PUSH", "expired: [[OPTIONS]]\n[[OPTIONS]]", "clean answer",
    ]


def test_bot_build_installs_the_belt_on_owner_sends_only():
    import bridge.bot as bot_mod
    from bridge import heartbeat

    bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
    with patch.object(bot_mod.TelegramBot, "_setup_handlers", lambda self: None), \
            patch.object(bot_mod.TelegramBot, "_error_handler", lambda *a: None, create=True):
        bot.build()
    get_updates_request, request = bot.application.bot._request
    assert isinstance(request, OwnerGuardRequest)
    assert isinstance(get_updates_request, heartbeat.HeartbeatHTTPXRequest)
    assert not isinstance(get_updates_request, OwnerGuardRequest)
