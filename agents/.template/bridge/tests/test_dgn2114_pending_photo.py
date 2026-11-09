"""Real intake handlers -> fake Telegram file API -> real turn queue -> SDK.

Only Telegram transport, the model response and the retry clock are fake.
The legacy three-attempt window must not consume the only copy of a photo.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut

from bridge import bot as bot_mod, messages, pending_media


class FileAPI:
    def __init__(self, failures=(), stage="download_to_drive"):
        self.failures = list(failures)
        self.stage = stage
        self.calls = []
        self.sent = []

    async def get_file(self, file_id, **kwargs):
        self.calls.append(("get_file", file_id))
        if self.stage == "get_file" and self.failures:
            raise self.failures.pop(0)
        return self

    async def download_to_drive(self, custom_path, **kwargs):
        self.calls.append(("download_to_drive", custom_path))
        if self.stage == "download_to_drive" and self.failures:
            Path(custom_path).write_bytes(b"partial")
            raise self.failures.pop(0)
        Path(custom_path).write_bytes(b"recovered photo")

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_chat_action(self, **kwargs):
        pass


def update(api, mid=101, chat=42, caption="workout caption", group=None, text=None):
    body = {
        "message_id": mid,
        "date": int(datetime.now(timezone.utc).timestamp()),
        "chat": {"id": chat, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Test"},
    }
    if text is None:
        body.update(photo=[{"file_id": f"photo-{mid}", "file_unique_id": str(mid),
                            "width": 10, "height": 10}], caption=caption)
        if group:
            body["media_group_id"] = group
    else:
        body["text"] = text
    return Update.de_json({"update_id": mid, "message": body}, api)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setattr(bot_mod.config, "bot_data_dir", tmp_path / "state-root")
    monkeypatch.setattr(bot_mod.config, "allowed_user_ids", [42])
    agent = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message", agent)
    # The gate ends at the real SDK request; no generated response to render.
    monkeypatch.setattr(bot_mod.TelegramBot, "_settle_turn", AsyncMock(return_value=None))
    original_sleep = asyncio.sleep
    clock = SimpleNamespace(value=1000.0, sleeps=[])

    async def advance(seconds):
        clock.sleeps.append(seconds)
        clock.value += seconds
        await original_sleep(0)

    fake_time = SimpleNamespace(time=lambda: clock.value, monotonic=time.monotonic)
    monkeypatch.setattr(bot_mod, "time", fake_time)
    monkeypatch.setattr(pending_media, "time", fake_time)
    monkeypatch.setattr(bot_mod.asyncio, "sleep", advance)

    def make(api):
        bridge = bot_mod.TelegramBot()
        bridge.application = SimpleNamespace(bot=api, shutdown=AsyncMock())
        return bridge

    return make, agent, clock


async def drain(bridge):
    tasks = set(bridge._media_retry_tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    # Includes follow-up tasks created by the real queue's done callbacks.
    for _ in range(12):
        tasks = {task for tasks in bridge._user_run_tasks.values() for task in tasks}
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError("turn queue did not drain")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["get_file", "download_to_drive"])
async def test_three_timeouts_then_recovery_reaches_sdk(harness, stage, caplog):
    make, agent, clock = harness
    api = FileAPI([TimedOut("read timeout") for _ in range(3)], stage)
    bridge = make(api)
    incoming = update(api, caption="/queue workout caption")
    with caplog.at_level(logging.WARNING):
        await bridge._handle_photo_message(incoming, None)
        # Saved before any getFile/download operation, including caption.
        stored = json.loads(bridge._pending_media.path.read_text())["42:101"]
        assert stored["caption"] == "workout caption"
        assert stored["file_id"] == "photo-101"
        assert stored["received_at"] == 1000
        await drain(bridge)
    agent.assert_awaited_once()
    text = agent.call_args.kwargs["user_message"]
    assert "workout caption" in text and "/queue" not in text
    assert str(bridge._image_dir / "42_101.jpg") in text
    assert (bridge._image_dir / "42_101.jpg").read_bytes() == b"recovered photo"
    assert len([c for c in api.calls if c[0] == stage]) == 4
    assert clock.sleeps[:3] == [1, 3, 5]
    assert max(clock.sleeps) <= 60
    assert api.sent == []
    failures = [r.getMessage() for r in caplog.records if "stage=" in r.getMessage()]
    assert len(failures) == 4  # three attempts + exhausted short window
    assert all(f"stage={stage}" in line and "exception=TimedOut" in line
               and "message_id=101" in line and "read timeout" in line for line in failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimedOut("still down"), BadRequest("invalid file")])
async def test_permanent_failure_waits_for_bound_and_reports_once(harness, error):
    make, agent, clock = harness
    api = FileAPI([error] * 1000)
    bridge = make(api)
    incoming = update(api)
    await bridge._handle_photo_message(incoming, None)
    await drain(bridge)
    assert clock.value == 1600
    assert max(clock.sleeps) <= 60
    agent.assert_not_awaited()
    assert api.sent == [(42, messages.PHOTO_DOWNLOAD_FAILED)]
    assert not (bridge._image_dir / "42_101.jpg").exists()
    await bridge._handle_photo_message(incoming, None)
    restarted = make(api)
    restarted._resume_pending_media()
    await restarted._handle_photo_message(incoming, None)
    await drain(restarted)
    assert api.sent == [(42, messages.PHOTO_DOWNLOAD_FAILED)]


@pytest.mark.asyncio
async def test_duplicate_updates_dedupe_pending_delivered_and_restarted(harness):
    make, agent, _ = harness
    api = FileAPI([TimedOut()] * 3)
    bridge = make(api)
    incoming = update(api)
    await asyncio.gather(bridge._handle_photo_message(incoming, None),
                         bridge._handle_photo_message(incoming, None))
    await drain(bridge)
    await bridge._handle_photo_message(incoming, None)
    restarted = make(api)
    await restarted._handle_photo_message(incoming, None)
    await drain(restarted)
    agent.assert_awaited_once()
    assert len([c for c in api.calls if c[0] == "get_file"]) == 4


@pytest.mark.asyncio
async def test_restart_resumes_saved_photo_without_telegram_redelivery(harness):
    make, agent, _ = harness
    api = FileAPI()
    bridge = make(api)
    await bridge._handle_photo_message(update(api), None)
    await bridge._graceful_shutdown(force=True)
    agent.assert_not_awaited()
    restarted = make(api)
    restarted._resume_pending_media()
    await drain(restarted)
    agent.assert_awaited_once()
    assert "workout caption" in agent.call_args.kwargs["user_message"]


@pytest.mark.asyncio
async def test_restart_after_three_timeouts_resumes_original_intake(harness):
    make, agent, clock = harness
    api = FileAPI([TimedOut()] * 3)
    stalled = asyncio.Event()
    original = api.download_to_drive

    async def fourth_attempt(**kwargs):
        if not api.failures:
            stalled.set()
            await asyncio.Event().wait()
        await original(**kwargs)

    api.download_to_drive = fourth_attempt
    bridge = make(api)
    await bridge._handle_photo_message(update(api), None)
    await asyncio.wait_for(stalled.wait(), timeout=2)
    assert clock.sleeps[:3] == [1, 3, 5]
    await bridge._graceful_shutdown(force=True)
    agent.assert_not_awaited()
    recovered = make(FileAPI())
    recovered._resume_pending_media()
    await drain(recovered)
    agent.assert_awaited_once()
    assert recovered._pending_media.records["42:101"]["received_at"] == 1000


@pytest.mark.asyncio
async def test_pending_note_reaches_text_sdk_only_in_same_chat(harness):
    make, agent, _ = harness
    api = FileAPI()
    bridge = make(api)
    # An album is durably pending during its debounce; no fake store inserts.
    await bridge._handle_photo_message(update(api, group="album"), None)
    for group in bridge._media_groups.values():
        group["timer"].cancel()
    await bridge._handle_text_message(update(api, mid=102, text="did you see it?"), None)
    await drain(bridge)
    text = agent.call_args.kwargs["user_message"]
    assert pending_media.PENDING_NOTE in text and "did you see it?" in text
    await bridge._handle_text_message(update(api, mid=103, chat=99, text="other chat"), None)
    await drain(bridge)
    assert pending_media.PENDING_NOTE not in agent.call_args.kwargs["user_message"]
    bridge._resume_pending_media()
    await drain(bridge)
    await bridge._handle_text_message(update(api, mid=104, text="now?"), None)
    await drain(bridge)
    assert pending_media.PENDING_NOTE not in agent.call_args.kwargs["user_message"]


@pytest.mark.asyncio
async def test_album_retries_missing_item_keeps_caption_and_one_turn(harness, monkeypatch):
    make, agent, _ = harness
    api = FileAPI([TimedOut()] * 3)
    bridge = make(api)
    monkeypatch.setattr(bot_mod, "MEDIA_GROUP_DEBOUNCE", 0)
    await bridge._handle_photo_message(update(api, caption="/queue both", group="album"), None)
    await bridge._handle_photo_message(update(api, mid=102, caption=None, group="album"), None)
    await bridge._media_groups["album"]["timer"]
    await drain(bridge)
    agent.assert_awaited_once()
    text = agent.call_args.kwargs["user_message"]
    assert "both" in text and "/queue" not in text
    assert "42_101.jpg" in text and "42_102.jpg" in text
    assert api.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError("connection refused"),
                                      NetworkError("Bad Gateway")])
async def test_connection_and_5xx_recover(harness, error):
    make, agent, _ = harness
    api = FileAPI([error] * 3, "get_file")
    bridge = make(api)
    await bridge._handle_photo_message(update(api), None)
    await drain(bridge)
    agent.assert_awaited_once()
    assert api.sent == []


@pytest.mark.asyncio
async def test_expired_notice_survives_failed_sends_and_restart(harness):
    make, agent, _ = harness
    api = FileAPI([BadRequest("invalid id")])
    bridge = make(api)
    failed_sends = asyncio.Event()

    async def offline_send(*args, **kwargs):
        failed_sends.set()
        await asyncio.Event().wait()

    api.send_message = offline_send
    await bridge._handle_photo_message(update(api), None)
    await asyncio.wait_for(failed_sends.wait(), timeout=2)
    assert bridge._pending_media.records["42:101"]["notice_pending"] is True
    tasks = set(bridge._media_retry_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    recovered_api = FileAPI()
    restarted = make(recovered_api)
    restarted._resume_pending_media()
    await drain(restarted)
    agent.assert_not_awaited()
    assert recovered_api.calls == []  # Only the notice, no new photo attempt.
    assert recovered_api.sent == [(42, messages.PHOTO_DOWNLOAD_FAILED)]
    restarted._resume_pending_media()
    await drain(restarted)
    assert len(recovered_api.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["get_file", "download_to_drive"])
async def test_hung_file_api_is_bounded_and_logs_active_stage(harness, monkeypatch, stage, caplog):
    make, agent, _ = harness
    api = FileAPI()
    bridge = make(api)
    # Use real elapsed time for this boundary assertion. No fake clock can
    # prove that an indefinitely awaiting transport is actually cancelled.
    monkeypatch.setattr(bot_mod, "time", time)
    monkeypatch.setattr(pending_media, "time", time)
    monkeypatch.setattr(pending_media, "RETRY_WINDOW", 0.02)

    async def hung(*args, **kwargs):
        await asyncio.Event().wait()

    setattr(api, stage, hung)
    with caplog.at_level(logging.WARNING):
        await bridge._handle_photo_message(update(api), None)
        await drain(bridge)
    agent.assert_not_awaited()
    assert api.sent == [(42, messages.PHOTO_DOWNLOAD_FAILED)]
    assert any(f"stage={stage}" in r.getMessage() and "message_id=101" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_album_partial_failure_keeps_successful_photo_and_caption(harness, monkeypatch):
    make, agent, _ = harness
    api = FileAPI()
    original = api.get_file

    async def one_missing(fid, **kwargs):
        if fid == "photo-102":
            raise TimedOut("second photo is unavailable")
        return await original(fid, **kwargs)

    api.get_file = one_missing
    bridge = make(api)
    monkeypatch.setattr(bot_mod, "MEDIA_GROUP_DEBOUNCE", 0)
    await bridge._handle_photo_message(update(api, group="album"), None)
    await bridge._handle_photo_message(update(api, mid=102, caption=None, group="album"), None)
    await bridge._media_groups["album"]["timer"]
    await drain(bridge)
    agent.assert_awaited_once()
    text = agent.call_args.kwargs["user_message"]
    assert "42_101.jpg" in text and "workout caption" in text
    assert "42_102.jpg" not in text
    assert api.sent == [(42, messages.PHOTO_DOWNLOAD_FAILED)]
    assert len([c for c in api.calls if c[0] == "get_file"]) == 1


@pytest.mark.asyncio
async def test_queue_text_carries_pending_photo_note(harness):
    make, agent, _ = harness
    api = FileAPI()
    bridge = make(api)
    await bridge._handle_photo_message(update(api, group="album"), None)
    group = bridge._media_groups["album"]
    group["timer"].cancel()
    await bridge._cmd_queue(update(api, mid=102, text="/queue waiting"),
                            SimpleNamespace(args=["waiting"]))
    await drain(bridge)
    assert pending_media.PENDING_NOTE in agent.call_args.kwargs["user_message"]
    bridge._resume_pending_media()
    await drain(bridge)
