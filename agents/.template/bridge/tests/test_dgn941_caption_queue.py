"""DGN-941: photo/document CAPTION "/queue" routes through coalescing.

Telegram only parses commands from message.text, so a photo/document caption
"/queue ..." never reaches the CommandHandler -- it lands as inert prompt text
and the immediate (interrupting) dispatch path fires. This suite covers:

  (helper) _split_caption_queue: leading /queue token detection + strip, with
    case/whitespace/@mention variants and the no-token passthrough.
  (a) caption /queue + in-flight -> buffered (coalesce), NOT interrupted.
  (b) no /queue caption -> immediate dispatch preserved (legacy behavior).
  (c) idle caption /queue -> processed immediately (coalesce idle == dispatch).
  (album) the /queue token on the first album item latches coalesce for the
    whole album; the token is stripped from the prompt caption.
  (document) same caption-/queue routing for documents.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bridge import bot as bot_mod


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def send_action(self, *a, **k):
        pass


class _FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id


class _FakeMessage:
    def __init__(self, chat, caption=None, photo=None, document=None,
                 media_group_id=None, date=None):
        self.chat = chat
        self.date = date or datetime.now(timezone.utc)
        self.caption = caption
        self.photo = photo or []
        self.document = document
        self.media_group_id = media_group_id

    async def reply_text(self, *a, **k):
        pass


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeUpdate:
    def __init__(self, uid=1, chat_id=1, caption=None, photo=None,
                 document=None, media_group_id=None, date=None):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.effective_user = _FakeUser(uid)
        self.message = _FakeMessage(
            chat, caption=caption, photo=photo, document=document,
            media_group_id=media_group_id, date=date,
        )


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


# ---------------------------------------------------------------------------
# Helper: _split_caption_queue
# ---------------------------------------------------------------------------


class TestSplitCaptionQueue:
    def test_leading_queue_stripped_and_coalesce_true(self):
        assert bot_mod.TelegramBot._split_caption_queue(
            "/queue 워그 도가니"
        ) == ("워그 도가니", True)

    def test_bare_queue_only(self):
        assert bot_mod.TelegramBot._split_caption_queue("/queue") == ("", True)

    def test_bare_queue_trailing_space(self):
        assert bot_mod.TelegramBot._split_caption_queue("/queue   ") == ("", True)

    def test_no_queue_passthrough(self):
        assert bot_mod.TelegramBot._split_caption_queue(
            "just a caption"
        ) == ("just a caption", False)

    def test_queue_not_at_start_not_matched(self):
        # /queue must be the LEADING token -- mid-caption is inert.
        assert bot_mod.TelegramBot._split_caption_queue(
            "look /queue here"
        ) == ("look /queue here", False)

    def test_case_insensitive(self):
        assert bot_mod.TelegramBot._split_caption_queue(
            "/QUEUE do it"
        ) == ("do it", True)
        assert bot_mod.TelegramBot._split_caption_queue(
            "/Queue do it"
        ) == ("do it", True)

    def test_bot_mention_suffix_stripped(self):
        assert bot_mod.TelegramBot._split_caption_queue(
            "/queue@metal_bot do it"
        ) == ("do it", True)

    def test_leading_whitespace_before_queue(self):
        # The caption is trimmed first, so leading whitespace still matches.
        assert bot_mod.TelegramBot._split_caption_queue(
            "  /queue do it"
        ) == ("do it", True)

    def test_empty_and_none(self):
        assert bot_mod.TelegramBot._split_caption_queue("") == ("", False)
        assert bot_mod.TelegramBot._split_caption_queue(None) == ("", False)

    def test_queueword_prefix_not_matched(self):
        # "/queued" must NOT match the /queue token (word-boundary via the
        # whitespace-or-end anchor).
        cap, coalesce = bot_mod.TelegramBot._split_caption_queue("/queued x")
        assert coalesce is False
        assert cap == "/queued x"


# ---------------------------------------------------------------------------
# Integration: photo handler routing
# ---------------------------------------------------------------------------


def _stub_photo_download(b):
    """Make _download_file a no-op and _check_access always pass."""
    b._download_file = AsyncMock(return_value=None)
    b._check_access = AsyncMock(return_value=True)


@pytest.mark.asyncio
async def test_caption_queue_photo_coalesces_when_inflight(monkeypatch):
    b = _make_bot()
    _stub_photo_download(b)
    user_id = 941
    barrier = asyncio.Event()
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        if not calls:
            calls.append(text)
            await barrier.wait()
        else:
            calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(
        bot_mod.sdk_bridge, "user_has_streamed_output",
        lambda uid: False, raising=False,
    )

    ts0 = datetime(2026, 8, 20, 5, 0, 0, tzinfo=timezone.utc)
    # A regular text turn is in flight (blocks on the barrier).
    await b._enqueue_text_task(user_id, "regular", ts0, _FakeUpdate(uid=user_id))

    # A photo with a "/queue" caption arrives mid-turn.
    upd = _FakeUpdate(
        uid=user_id, caption="/queue read this chart", photo=[_FakePhoto("f1")]
    )
    await b._handle_photo_message(upd, None)

    # It MUST land in the coalescing buffer -- never interrupt.
    assert len(b._user_pending_texts.get(user_id, [])) == 1
    buffered_text = b._user_pending_texts[user_id][0][0]
    # The /queue token is stripped from the prompt; the caption body survives.
    assert "/queue" not in buffered_text
    assert "read this chart" in buffered_text

    barrier.set()
    await asyncio.sleep(0)
    for _ in range(12):
        tasks = list(b._user_run_tasks.get(user_id, set()))
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    # No interrupt-notice send happened.
    assert b.application.bot.sent == []


@pytest.mark.asyncio
async def test_no_queue_caption_photo_dispatches_immediately(monkeypatch):
    b = _make_bot()
    _stub_photo_download(b)
    user_id = 942
    dispatched = []

    async def mock_dispatch(user_id_, text, ts, update, **kwargs):
        dispatched.append((text, kwargs.get("failure_message")))

    monkeypatch.setattr(b, "_dispatch_text_turn", mock_dispatch)

    upd = _FakeUpdate(
        uid=user_id, caption="just a normal chart", photo=[_FakePhoto("f1")]
    )
    await b._handle_photo_message(upd, None)

    # Idle + no /queue -> immediate dispatch (legacy), never buffered.
    assert b._user_pending_texts.get(user_id, []) == []
    assert len(dispatched) == 1
    text = dispatched[0][0]
    assert "just a normal chart" in text


@pytest.mark.asyncio
async def test_idle_caption_queue_photo_processed_immediately(monkeypatch):
    b = _make_bot()
    _stub_photo_download(b)
    user_id = 943
    dispatched = []

    async def mock_dispatch(user_id_, text, ts, update, **kwargs):
        dispatched.append(text)

    monkeypatch.setattr(b, "_dispatch_text_turn", mock_dispatch)

    upd = _FakeUpdate(
        uid=user_id, caption="/queue read this", photo=[_FakePhoto("f1")]
    )
    await b._handle_photo_message(upd, None)

    # Idle: coalesce path with an empty buffer dispatches immediately.
    assert b._user_pending_texts.get(user_id, []) == []
    assert len(dispatched) == 1
    assert "/queue" not in dispatched[0]
    assert "read this" in dispatched[0]


@pytest.mark.asyncio
async def test_caption_queue_document_coalesces_when_inflight(monkeypatch):
    b = _make_bot()
    b._download_file = AsyncMock(return_value=None)
    b._check_access = AsyncMock(return_value=True)
    user_id = 944
    barrier = asyncio.Event()
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        if not calls:
            calls.append(text)
            await barrier.wait()
        else:
            calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(
        bot_mod.sdk_bridge, "user_has_streamed_output",
        lambda uid: False, raising=False,
    )

    ts0 = datetime(2026, 8, 20, 5, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "regular", ts0, _FakeUpdate(uid=user_id))

    doc = SimpleNamespace(file_id="d1", file_name="report.pdf")
    upd = _FakeUpdate(uid=user_id, caption="/queue summarize", document=doc)
    await b._handle_document_message(upd, None)

    assert len(b._user_pending_texts.get(user_id, [])) == 1
    buffered_text = b._user_pending_texts[user_id][0][0]
    assert "/queue" not in buffered_text
    assert "summarize" in buffered_text

    barrier.set()
    for _ in range(12):
        tasks = list(b._user_run_tasks.get(user_id, set()))
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_album_caption_queue_latches_coalesce(monkeypatch):
    """DGN-941 album edge: /queue rides the first album item caption; the flag
    latches for the whole media group and the token is stripped."""
    b = _make_bot()
    _stub_photo_download(b)
    # Zero-out the album debounce so the flush fires promptly.
    monkeypatch.setattr(bot_mod, "MEDIA_GROUP_DEBOUNCE", 0.0, raising=False)
    user_id = 945
    barrier = asyncio.Event()
    calls = []

    async def mock_process(update, uid, text, **kwargs):
        if not calls:
            calls.append(text)
            await barrier.wait()
        else:
            calls.append(text)

    monkeypatch.setattr(b, "_process_user_message_text", mock_process)
    monkeypatch.setattr(
        bot_mod.sdk_bridge, "user_has_streamed_output",
        lambda uid: False, raising=False,
    )

    ts0 = datetime(2026, 8, 20, 5, 0, 0, tzinfo=timezone.utc)
    await b._enqueue_text_task(user_id, "regular", ts0, _FakeUpdate(uid=user_id))

    # First album item carries the /queue caption; second item is caption-less.
    upd1 = _FakeUpdate(
        uid=user_id, caption="/queue read these", photo=[_FakePhoto("a1")],
        media_group_id="grp1",
    )
    upd2 = _FakeUpdate(
        uid=user_id, caption=None, photo=[_FakePhoto("a2")],
        media_group_id="grp1",
    )
    await b._handle_photo_message(upd1, None)
    await b._handle_photo_message(upd2, None)

    # Let the album debounce flush.
    for _ in range(20):
        await asyncio.sleep(0)
        if b._user_pending_texts.get(user_id):
            break

    buffered = b._user_pending_texts.get(user_id, [])
    assert len(buffered) == 1, "album must coalesce, not interrupt"
    text = buffered[0][0]
    assert "/queue" not in text
    assert "read these" in text

    barrier.set()
    for _ in range(12):
        tasks = list(b._user_run_tasks.get(user_id, set()))
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
