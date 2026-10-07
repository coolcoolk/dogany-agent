"""DGN-649: oversized-dimension image send handling.

Telegram sendPhoto rejects width+height > 10000 or aspect ratio > 20 with an
opaque BadRequest that the bridge reported as a generic "network error"
(trigger: a 750x73490 full-page screenshot). Covers:

1. Pure header parser (PNG / GIF / JPEG / WEBP) -- the no-Pillow probe path.
2. photo_send_verdict boundaries (sum 10000, ratio 20, the ticket's case).
3. downscale_for_photo (Pillow path; skipped when Pillow is absent).
4. classify_send_error mapping (dimensions / too_large / network / api).
5. _send_file_paths wiring: pre-send document fallback, downscale send +
   temp cleanup, runtime dimension-error document retry, and reason-accurate
   failure messages.
"""

import asyncio
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from bridge import messages
from bridge.image_send import (
    classify_send_error,
    downscale_for_photo,
    parse_image_size_from_header,
    photo_send_verdict,
    probe_image_dimensions,
)

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# ---------------------------------------------------------------------------
# Fixture byte builders (header-only, enough for the pure parser)
# ---------------------------------------------------------------------------

def png_bytes(w: int, h: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + w.to_bytes(4, "big")
        + h.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def gif_bytes(w: int, h: int) -> bytes:
    return b"GIF89a" + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00" * 4


def jpeg_bytes(w: int, h: int) -> bytes:
    # SOI + APP0 (JFIF) + SOF0 carrying the frame size.
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof0 = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + h.to_bytes(2, "big")
        + w.to_bytes(2, "big")
        + b"\x03" + b"\x00" * 9
    )
    return b"\xff\xd8" + app0 + sof0


def webp_vp8l_bytes(w: int, h: int) -> bytes:
    bits = (w - 1) | ((h - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little")
    return b"RIFF" + (20).to_bytes(4, "little") + b"WEBPVP8L" + (5).to_bytes(4, "little") + payload


def webp_vp8x_bytes(w: int, h: int) -> bytes:
    payload = b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + (18).to_bytes(4, "little") + b"WEBPVP8X" + (10).to_bytes(4, "little") + payload


def webp_vp8_bytes(w: int, h: int) -> bytes:
    payload = b"\x00" * 3 + b"\x9d\x01\x2a" + w.to_bytes(2, "little") + h.to_bytes(2, "little")
    return b"RIFF" + (14).to_bytes(4, "little") + b"WEBPVP8 " + (10).to_bytes(4, "little") + payload


# ---------------------------------------------------------------------------
# 1. Pure header parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data,expected",
    [
        (png_bytes(750, 73490), (750, 73490)),
        (gif_bytes(320, 240), (320, 240)),
        (jpeg_bytes(4000, 3000), (4000, 3000)),
        (webp_vp8l_bytes(1234, 5678), (1234, 5678)),
        (webp_vp8x_bytes(9000, 2000), (9000, 2000)),
        (webp_vp8_bytes(640, 480), (640, 480)),
    ],
)
def test_header_parser_formats(data, expected):
    assert parse_image_size_from_header(data) == expected


def test_header_parser_garbage_returns_none():
    assert parse_image_size_from_header(b"") is None
    assert parse_image_size_from_header(b"not an image at all") is None
    assert parse_image_size_from_header(b"\x89PNG\r\n\x1a\n1234") is None  # truncated


def test_probe_falls_back_to_header_parser(tmp_path):
    """Header-only PNG is unopenable by Pillow -> pure parser must answer."""
    p = tmp_path / "tall.png"
    p.write_bytes(png_bytes(750, 73490))
    assert probe_image_dimensions(p) == (750, 73490)


def test_probe_unreadable_returns_none(tmp_path):
    assert probe_image_dimensions(tmp_path / "missing.png") is None


# ---------------------------------------------------------------------------
# 2. Verdict boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "w,h,expected",
    [
        (800, 600, "photo"),
        (5000, 5000, "photo"),        # sum == 10000: allowed
        (5001, 5000, "downscale"),    # sum overflow, ratio legal
        (8000, 6000, "downscale"),
        (400, 8000, "photo"),         # ratio == 20 exactly: allowed
        (400, 8001, "document"),      # ratio just over 20
        (750, 73490, "document"),     # the ticket's screenshot
        (0, 100, "photo"),            # degenerate: cannot judge
    ],
)
def test_photo_send_verdict(w, h, expected):
    assert photo_send_verdict(w, h) == expected


# ---------------------------------------------------------------------------
# 3. Downscale (Pillow path)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_PIL, reason="Pillow not installed")
def test_downscale_fits_limit_and_preserves_aspect(tmp_path, monkeypatch):
    import bridge.image_send as image_send
    monkeypatch.setattr(image_send, "TG_PHOTO_MAX_DIM_SUM", 100)
    src = tmp_path / "big.png"
    Image.new("RGB", (80, 60), (10, 20, 30)).save(src)
    out = downscale_for_photo(src)
    assert out is not None and out.exists()
    try:
        with Image.open(out) as im:
            w, h = im.size
        assert w + h <= 100
        assert abs(w / h - 80 / 60) < 0.1
    finally:
        out.unlink()


@pytest.mark.skipif(not HAVE_PIL, reason="Pillow not installed")
def test_downscale_noop_when_within_limit(tmp_path):
    src = tmp_path / "small.png"
    Image.new("RGB", (40, 30)).save(src)
    assert downscale_for_photo(src) is None


def test_downscale_unopenable_returns_none(tmp_path):
    src = tmp_path / "fake.png"
    src.write_bytes(png_bytes(9000, 2000))  # header only, not a real image
    assert downscale_for_photo(src) is None


# ---------------------------------------------------------------------------
# 4. Failure classification
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    pass


class NetworkError(Exception):
    pass


class TimedOut(NetworkError):
    pass


@pytest.mark.parametrize(
    "exc,expected",
    [
        (BadRequest("Photo_invalid_dimensions"), "dimensions"),
        (BadRequest("Image_process_failed"), "dimensions"),
        (BadRequest("Request Entity Too Large"), "too_large"),
        (BadRequest("File is too big"), "too_large"),
        (TimedOut("Timed out"), "network"),
        (NetworkError("httpx.ConnectError"), "network"),
        (ConnectionError("reset"), "network"),
        (BadRequest("Chat not found"), "api"),
        (RuntimeError("boom"), "api"),
    ],
)
def test_classify_send_error(exc, expected):
    assert classify_send_error(exc) == expected


# ---------------------------------------------------------------------------
# 5. _send_file_paths wiring
# ---------------------------------------------------------------------------

import bridge.bot as bot_mod


class FakeBot:
    def __init__(self, photo_errors=None, document_errors=None):
        self.photo_calls = []
        self.document_calls = []
        self.messages = []
        self._photo_errors = list(photo_errors or [])
        self._document_errors = list(document_errors or [])

    async def send_photo(self, chat_id, photo=None):
        self.photo_calls.append(Path(photo.name))
        if self._photo_errors:
            raise self._photo_errors.pop(0)

    async def send_document(self, chat_id, document=None):
        self.document_calls.append(Path(document.name))
        if self._document_errors:
            raise self._document_errors.pop(0)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def run_send(fake_bot, paths, **patches):
    fake_self = SimpleNamespace(application=SimpleNamespace(bot=fake_bot))

    async def no_sleep(_):
        return None

    with ExitStack() as stack:
        stack.enter_context(patch.object(bot_mod.asyncio, "sleep", no_sleep))
        for name, value in patches.items():
            stack.enter_context(patch.object(bot_mod, name, value))
        asyncio.run(bot_mod.TelegramBot._send_file_paths(fake_self, 1, paths))


def test_ratio_overflow_sends_document(tmp_path):
    """Ticket case: 750x73490 -> pre-send verdict routes to send_document."""
    p = tmp_path / "tickets.png"
    p.write_bytes(png_bytes(750, 73490))
    fake = FakeBot()
    run_send(fake, [p])
    assert fake.document_calls == [p]
    assert fake.photo_calls == []
    assert fake.messages == []


def test_sum_overflow_downscales_and_cleans_temp(tmp_path):
    p = tmp_path / "wide.png"
    p.write_bytes(png_bytes(8000, 6000))
    fd, scaled_name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    scaled = Path(scaled_name)
    scaled.write_bytes(png_bytes(5714, 4285))
    fake = FakeBot()
    run_send(fake, [p], downscale_for_photo=lambda _: scaled)
    assert fake.photo_calls == [scaled]
    assert fake.document_calls == []
    assert not scaled.exists()  # temp deleted after send
    assert fake.messages == []


def test_sum_overflow_without_pillow_falls_back_to_document(tmp_path):
    p = tmp_path / "wide.png"
    p.write_bytes(png_bytes(8000, 6000))
    fake = FakeBot()
    run_send(fake, [p], downscale_for_photo=lambda _: None)
    assert fake.document_calls == [p]
    assert fake.photo_calls == []
    assert fake.messages == []


def test_runtime_dimension_error_retries_as_document(tmp_path):
    """Probe missed the overflow -> BadRequest at send -> document retry."""
    p = tmp_path / "odd.png"
    p.write_bytes(png_bytes(100, 100))
    fake = FakeBot(photo_errors=[BadRequest("Photo_invalid_dimensions")])
    run_send(fake, [p], probe_image_dimensions=lambda _: None)
    assert fake.photo_calls == [p]
    assert fake.document_calls == [p]
    assert fake.messages == []


def test_exhausted_dimension_failure_reports_dimensions(tmp_path):
    p = tmp_path / "odd.png"
    p.write_bytes(png_bytes(100, 100))
    fake = FakeBot(
        photo_errors=[BadRequest("Photo_invalid_dimensions")],
        document_errors=[BadRequest("Photo_invalid_dimensions")],
    )
    run_send(fake, [p], probe_image_dimensions=lambda _: None)
    assert fake.messages == [
        messages.SEND_FILE_FAILED_DIMENSIONS.format(filename=p.name)
    ]


def test_exhausted_network_failure_keeps_network_message(tmp_path):
    p = tmp_path / "ok.png"
    p.write_bytes(png_bytes(100, 100))
    fake = FakeBot(photo_errors=[TimedOut("Timed out"), TimedOut("Timed out")])
    run_send(fake, [p])
    assert fake.photo_calls == [p, p]  # retry stayed a photo send
    assert fake.messages == [messages.SEND_FILE_FAILED.format(filename=p.name)]


def test_exhausted_api_failure_reports_delivery_error(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4")
    fake = FakeBot(
        document_errors=[BadRequest("Chat not found"), BadRequest("Chat not found")]
    )
    run_send(fake, [p])
    assert fake.messages == [messages.SEND_FILE_FAILED_API.format(filename=p.name)]
