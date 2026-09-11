"""DGN-163 turn-death safety net + DGN-082 inbound download retry.

Covers the invariant that a CONSUMED inbound update never yields zero
user-visible output:

  (a) a handler raising before any output -> the failure notice is sent exactly
      once;
  (b) an inbound download failing all 3 attempts -> the media-specific notice
      fires and the turn ends cleanly (no crash);
  (c) a download failing twice then succeeding -> normal flow, no notice;
  (d) an exception after partial output already streamed -> the softer
      "incomplete" variant is sent instead of "not processed";
  (e) the notice-send itself failing -> it is logged and swallowed (no crash
      loop), so a broken transport cannot turn one dead turn into a storm.

These drive bot.TelegramBot directly with a stubbed Application/bot, so no live
Telegram, token, or network is touched. All mocks live in-process.
"""

import asyncio

import pytest

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod


class _FakeBot:
    """Minimal stand-in for telegram.Bot: records every send_message call and,
    optionally, raises to simulate a dead transport."""

    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        if self.fail:
            raise RuntimeError("transport down")


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


def _make_bot(fake_bot):
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(fake_bot)
    return b


async def _drain_user_tasks(b, user_id):
    """Await every task the enqueue spawned for this user."""
    tasks = list(b._user_run_tasks.get(user_id, set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --- (a) handler raises before any output -> notice sent exactly once ---


@pytest.mark.asyncio
async def test_handler_crash_sends_notice_once(monkeypatch):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    fake = _FakeBot()
    b = _make_bot(fake)

    async def boom():
        raise ValueError("mid-turn death")

    async def on_overflow():
        raise AssertionError("overflow must not fire")

    ok = await b._enqueue_user_task(
        999, boom, on_overflow, chat_id=999, failure_message=messages.TURN_FAILED
    )
    assert ok is True
    await _drain_user_tasks(b, 999)

    assert fake.sent == [(999, messages.TURN_FAILED)]
    # exactly one notice, and it is not a raw traceback
    assert "Traceback" not in fake.sent[0][1]
    assert "ValueError" not in fake.sent[0][1]


# --- (b) download fails all 3 attempts -> media notice, clean turn ---


@pytest.mark.asyncio
async def test_download_fails_thrice_media_notice(monkeypatch):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    # collapse backoff sleeps so the test is instant
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _instant_sleep)

    calls = {"n": 0}

    class _AlwaysFailFile:
        async def download_to_drive(self, *a, **k):
            raise RuntimeError("nope")

    class _FailBot(_FakeBot):
        async def get_file(self, file_id, *a, **k):
            calls["n"] += 1
            raise RuntimeError("5xx blip")

    fake = _FailBot()
    b = _make_bot(fake)

    async def run_task():
        # mimic a media handler: download then re-raise the download failure so
        # the enqueue safety net emits the media-specific notice.
        await b._download_file("fid", _tmp_path())

    async def on_overflow():
        raise AssertionError

    await b._enqueue_user_task(
        7, run_task, on_overflow, chat_id=7, failure_message=messages.TURN_FAILED_PHOTO
    )
    await _drain_user_tasks(b, 7)

    assert calls["n"] == 3  # exactly 3 attempts
    assert fake.sent == [(7, messages.TURN_FAILED_PHOTO)]


# --- (c) download fails twice then succeeds -> normal flow, no notice ---


@pytest.mark.asyncio
async def test_download_recovers_no_notice(monkeypatch):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    monkeypatch.setattr(bot_mod.asyncio, "sleep", _instant_sleep)

    state = {"n": 0}

    class _RecoverFile:
        async def download_to_drive(self, *a, **k):
            return None

    class _RecoverBot(_FakeBot):
        async def get_file(self, file_id, *a, **k):
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("transient")
            return _RecoverFile()

    fake = _RecoverBot()
    b = _make_bot(fake)

    delivered = {"ok": False}

    async def run_task():
        await b._download_file("fid", _tmp_path())
        delivered["ok"] = True  # reached the post-download path

    async def on_overflow():
        raise AssertionError

    await b._enqueue_user_task(
        8, run_task, on_overflow, chat_id=8, failure_message=messages.TURN_FAILED_PHOTO
    )
    await _drain_user_tasks(b, 8)

    assert state["n"] == 3  # two failures then success on the third
    assert delivered["ok"] is True
    assert fake.sent == []  # no failure notice on a recovered download


# --- (d) exception after partial stream -> incomplete variant ---


@pytest.mark.asyncio
async def test_partial_stream_then_death_sends_incomplete(monkeypatch):
    # simulate that partial output already streamed for this user this turn.
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: True
    )
    fake = _FakeBot()
    b = _make_bot(fake)

    async def die_after_stream():
        raise RuntimeError("died mid-stream")

    async def on_overflow():
        raise AssertionError

    await b._enqueue_user_task(
        5, die_after_stream, on_overflow, chat_id=5, failure_message=messages.TURN_FAILED
    )
    await _drain_user_tasks(b, 5)

    assert fake.sent == [(5, messages.TURN_INCOMPLETE)]
    # must NOT claim the message was dropped
    assert fake.sent[0][1] != messages.TURN_FAILED


# --- (e) notice-send itself fails -> logged, no crash loop ---


@pytest.mark.asyncio
async def test_notice_send_failure_is_swallowed(monkeypatch, caplog):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    fake = _FakeBot(fail=True)  # send_message raises
    b = _make_bot(fake)

    async def boom():
        raise ValueError("turn death")

    async def on_overflow():
        raise AssertionError

    # must not raise, must not loop: a single send attempt, error logged.
    await b._enqueue_user_task(
        3, boom, on_overflow, chat_id=3, failure_message=messages.TURN_FAILED
    )
    await _drain_user_tasks(b, 3)

    # one send attempt was made and it raised (recorded before raising)
    assert fake.sent == [(3, messages.TURN_FAILED)]
    assert any(
        "turn-death notice send failed" in r.getMessage() for r in caplog.records
    )


# --- integration: real media handlers route to the media-specific notice ---


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def send_action(self, *a, **k):
        return None


class _FakeMessage:
    def __init__(self, chat):
        self.chat = chat
        self.caption = None

    async def reply_text(self, *a, **k):
        return None


class _FakeUpdate:
    def __init__(self, chat_id):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat)


class _FakeDoc:
    file_id = "docfid"
    file_name = "report.pdf"


@pytest.mark.asyncio
async def test_photo_dispatch_all_fail_routes_photo_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    fake = _FakeBot()
    b = _make_bot(fake)
    b._image_dir = tmp_path / "images"

    async def always_fail(file_id, destination, *a, **k):
        raise RuntimeError("download blew up after retries")

    monkeypatch.setattr(b, "_download_file", always_fail)

    upd = _FakeUpdate(42)
    await b._dispatch_photo_task(upd, 42, ["a", "b"], caption="")
    await _drain_user_tasks(b, 42)

    assert fake.sent == [(42, messages.TURN_FAILED_PHOTO)]


@pytest.mark.asyncio
async def test_document_all_fail_routes_document_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sdk_bridge_mod.sdk_bridge, "user_has_streamed_output", lambda uid: False
    )
    fake = _FakeBot()
    b = _make_bot(fake)
    b._inbox_dir = tmp_path / "inbox"

    async def always_fail(file_id, destination, *a, **k):
        raise RuntimeError("doc download failed after retries")

    monkeypatch.setattr(b, "_download_file", always_fail)

    # build the update/context the handler expects
    class _Ctx:
        pass

    upd = _FakeUpdate(77)
    upd.message.document = _FakeDoc()

    class _User:
        id = 77

    upd.effective_user = _User()

    monkeypatch.setattr(b, "_check_access", _always_true)

    await b._handle_document_message(upd, _Ctx())
    await _drain_user_tasks(b, 77)

    assert fake.sent == [(77, messages.TURN_FAILED_DOCUMENT)]


# --- helpers ---


async def _always_true(*a, **k):
    return True


async def _instant_sleep(_seconds):
    return None


def _tmp_path():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="dgn163-dl-")) / "dest.bin"
