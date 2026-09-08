"""Pre-send image normalization for Telegram photo limits (DGN-649).

Telegram Bot API sendPhoto rejects images whose width+height sum exceeds
10000 px or whose aspect ratio exceeds 20:1. The bridge used to surface that
rejection as a generic "network error" (a full-page 750x73490 screenshot was
the reported trigger). These helpers fix both defects:

- Fix A (pre-send normalization): probe dimensions BEFORE sending and decide
  photo / downscale / document. Pillow is optional: with it, an oversized but
  ratio-legal image is downscaled (aspect preserved) and still arrives as a
  photo; without it -- or when the ratio itself is illegal, which no
  aspect-preserving downscale can fix -- degrade to a document send, which has
  no dimension limit and preserves the original pixels. Dimension probing
  works without Pillow via a pure header parser (PNG / JPEG / GIF / WEBP).
- Fix B (accurate failure reason): classify a send exception into
  dimensions / too_large / network / api so the user-facing failure message
  states the real cause instead of always blaming the network.

Pure helpers (no Telegram I/O) so they are easy to unit-test; bot.py wires
them into the actual send path.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Telegram Bot API sendPhoto limits: "The photo's width and height must not
# exceed 10000 in total. Width and height ratio must be at most 20."
TG_PHOTO_MAX_DIM_SUM = 10000
TG_PHOTO_MAX_RATIO = 20.0

# Dimension headers live near the front of every supported format; 1MB covers
# JPEGs that stuff large EXIF/ICC segments before the SOF marker.
_HEADER_READ_BYTES = 1 << 20


# ---------------------------------------------------------------------------
# Dimension probing
# ---------------------------------------------------------------------------

def parse_image_size_from_header(data: bytes) -> Optional[Tuple[int, int]]:
    """Return (width, height) parsed from raw file bytes, or None.

    Pure-Python fallback for when Pillow is absent. Detects the format by
    magic bytes (PNG / GIF / WEBP / JPEG) rather than trusting the suffix.
    """
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return (w, h) if w and h else None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return (w, h) if w and h else None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _parse_webp(data)
    if data[:2] == b"\xff\xd8":
        return _parse_jpeg(data)
    return None


def _parse_webp(data: bytes) -> Optional[Tuple[int, int]]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        # Extended format: 24-bit canvas width/height minus one at offset 24.
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return (w, h)
    if chunk == b"VP8 " and len(data) >= 30:
        # Lossy: sync code 9d 01 2a, then 14-bit little-endian dimensions.
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        w = int.from_bytes(data[26:28], "little") & 0x3FFF
        h = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (w, h) if w and h else None
    if chunk == b"VP8L" and len(data) >= 25:
        # Lossless: signature byte 0x2f, then 14+14 bits of (dim - 1).
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return (w, h)
    return None


def _parse_jpeg(data: bytes) -> Optional[Tuple[int, int]]:
    """Walk JPEG segments to the first SOF marker carrying the frame size."""
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1  # fill byte
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers have no length field
            continue
        if marker == 0xDA:
            return None  # start of scan reached without a SOF
        if i + 4 > n:
            return None
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def probe_image_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    """Return (width, height) of an image file, or None when unprobeable.

    Pillow first when installed; otherwise (or when Pillow chokes) the pure
    header parser. None means "cannot judge" -- callers keep prior behavior.
    """
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(path) as im:
                return im.size
        except Exception:
            pass  # fall through to the header parser
    try:
        with open(path, "rb") as f:
            data = f.read(_HEADER_READ_BYTES)
    except OSError:
        return None
    return parse_image_size_from_header(data)


# ---------------------------------------------------------------------------
# Verdict + downscale
# ---------------------------------------------------------------------------

def photo_send_verdict(width: int, height: int) -> str:
    """Judge dimensions against sendPhoto limits.

    Returns "photo" (fits), "downscale" (sum overflow, ratio legal -- an
    aspect-preserving downscale fixes it), or "document" (ratio overflow --
    no aspect-preserving downscale can fix it; send as document).
    """
    if width <= 0 or height <= 0:
        return "photo"
    ratio = max(width, height) / min(width, height)
    if ratio > TG_PHOTO_MAX_RATIO:
        return "document"
    if width + height > TG_PHOTO_MAX_DIM_SUM:
        return "downscale"
    return "photo"


def downscale_for_photo(path: Path) -> Optional[Path]:
    """Write an aspect-preserving downscale that fits sendPhoto limits.

    Returns the temp file path (caller deletes after send), or None when
    Pillow is missing or the resize/save fails -- callers then degrade to a
    document send of the original.
    """
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return None
    tmp_path: Optional[Path] = None
    try:
        with Image.open(path) as im:
            w, h = im.size
            if w + h <= TG_PHOTO_MAX_DIM_SUM:
                return None
            scale = TG_PHOTO_MAX_DIM_SUM / float(w + h)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            fmt = im.format or "PNG"
            fd, tmp = tempfile.mkstemp(prefix="tg-photo-", suffix=path.suffix)
            os.close(fd)
            tmp_path = Path(tmp)
            resized = im.resize((new_w, new_h))
            if fmt == "JPEG" and resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            resized.save(tmp_path, format=fmt)
        return tmp_path
    except Exception:
        logger.warning("Downscale failed for %s", path, exc_info=True)
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return None


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

def classify_send_error(exc: BaseException) -> str:
    """Classify a Telegram send failure: dimensions | too_large | network | api.

    String/class-name matching keeps this module free of telegram imports.
    Telegram reports oversized dimensions as BadRequest "Photo_invalid_
    dimensions" (sometimes "Image_process_failed"); oversized payloads as
    "Request Entity Too Large" / "file is too big"; PTB network trouble as
    NetworkError/TimedOut subclasses.
    """
    msg = str(exc).lower()
    names = {c.__name__.lower() for c in type(exc).__mro__}
    if "photo_invalid_dimensions" in msg or "image_process_failed" in msg or "photo dimensions" in msg:
        return "dimensions"
    if "too large" in msg or "too big" in msg:
        return "too_large"
    if names & {"networkerror", "timedout"} or isinstance(exc, ConnectionError) or "timed out" in msg:
        return "network"
    return "api"
