"""Voice input: ffmpeg audio prep + local faster-whisper transcription.

Input only (no TTS). A voice note is downloaded by the bot, converted to a
whisper-friendly format here when needed, transcribed locally, and the resulting
text is fed back into the normal text pipeline prefixed with a microphone glyph.
"""

import asyncio
import inspect
import json
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Sequence

from bridge.config import BOT_DATA_DIR, config

logger = logging.getLogger(__name__)

# DGN-1694: derived vocabulary cache (schema 1: {"terms": [{"term": ...}]},
# ordered by priority). Written outside the bridge; absent = no derived terms.
VOCAB_CACHE_PATH = BOT_DATA_DIR / "voice-vocab.json"
# faster-whisper keeps max_length // 2 - 1 prompt tokens (448 -> 223): the
# FIRST ones for hotwords, the LAST ones for initial_prompt -- an overflow
# would silently drop the highest-priority terms, so the cut happens here.
DEFAULT_MAX_LENGTH = 448


def load_derived_vocabulary(path: Optional[Path] = None) -> List[str]:
    """Terms from the derived-vocabulary cache; any problem -> []."""
    try:
        data = json.loads(Path(path or VOCAB_CACHE_PATH).read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            return []
        return [t["term"] for t in data.get("terms", [])
                if isinstance(t, dict) and isinstance(t.get("term"), str)]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def fit_vocabulary(phrases: Sequence[str], count_tokens, budget: int) -> List[str]:
    """Priority-ordered, case-insensitively deduplicated phrases whose joined
    hint (" " + ", ".join(...), as the recognizer encodes it) fits budget.
    A phrase that does not fit is skipped; shorter later phrases may still."""
    kept: List[str] = []
    seen = set()
    for phrase in phrases:
        phrase = str(phrase).strip()
        if not phrase or phrase.casefold() in seen:
            continue
        seen.add(phrase.casefold())
        if count_tokens(" " + ", ".join(kept + [phrase])) <= budget:
            kept.append(phrase)
    return kept


def _token_counter(model):
    """The loaded model's own tokenizer; else UTF-8 bytes, a strict upper
    bound for a byte-level BPE (never more tokens than bytes)."""
    tokenizer = getattr(model, "hf_tokenizer", None)
    if tokenizer is not None and callable(getattr(tokenizer, "encode", None)):
        return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)
    return lambda text: len(text.encode("utf-8"))


class TranscriptionError(RuntimeError):
    """Raised when transcription fails."""


class EmptyTranscriptionError(TranscriptionError):
    """Raised when transcription yields empty text."""


class AudioProcessor:
    """Audio format detection, conversion, and cleanup via ffmpeg."""

    _MP3 = {".mp3"}
    _OGG = {".ogg", ".oga", ".opus"}
    _AMR = {".amr"}

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        ffmpeg_args: Optional[Sequence[str]] = None,
    ) -> None:
        self.ffmpeg_path = (ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
        self.ffmpeg_args = list(ffmpeg_args or ("-ac", "1", "-ar", "16000"))

    async def check_ffmpeg_available(self) -> bool:
        exists = shutil.which(self.ffmpeg_path) is not None
        if not exists:
            logger.warning("ffmpeg binary not found: %s", self.ffmpeg_path)
        return exists

    async def detect_audio_format(self, file_path: Path) -> str:
        suffix = file_path.suffix.lower()
        if suffix in self._MP3:
            return "mp3"
        if suffix in self._OGG:
            return "ogg"
        if suffix in self._AMR:
            return "amr"
        try:
            with file_path.open("rb") as f:
                header = f.read(16)
        except OSError as exc:
            logger.error("Failed to read audio header from %s: %s", file_path, exc)
            return "unknown"
        if header.startswith(b"OggS"):
            return "ogg"
        if header.startswith(b"#!AMR"):
            return "amr"
        if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF):
            return "mp3"
        return "unknown"

    async def convert_audio(self, input_path: Path, output_path: Path) -> Path:
        command = [
            self.ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            *self.ffmpeg_args,
            str(output_path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (
                stderr.decode("utf-8", errors="ignore").strip()
                or stdout.decode("utf-8", errors="ignore").strip()
                or "unknown ffmpeg error"
            )
            logger.error("ffmpeg conversion failed: %s", detail)
            raise RuntimeError(f"ffmpeg conversion failed: {detail}")
        return output_path

    async def cleanup_audio_files(self, file_paths) -> None:
        for path in file_paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove temp audio %s: %s", path, exc)

    async def cleanup_stale_audio_files(self, audio_dir: Path, max_age_seconds: int) -> int:
        if not audio_dir.exists():
            return 0
        now = time.time()
        removed = 0
        for path in audio_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning("Failed to process stale audio %s: %s", path, exc)
        return removed

    async def prepare_for_whisper(
        self, source_path: Path, cleanup_paths: List[Path]
    ) -> Path:
        """Convert ogg/amr to mp3 for whisper; pass mp3 through untouched."""
        fmt = await self.detect_audio_format(source_path)
        if fmt == "mp3":
            return source_path
        if fmt not in {"amr", "ogg"}:
            return source_path
        if not await self.check_ffmpeg_available():
            raise RuntimeError("ffmpeg is not installed. Install ffmpeg to process voice.")
        converted_path = source_path.with_suffix(".mp3")
        cleanup_paths.append(converted_path)
        return await self.convert_audio(source_path, converted_path)


class LocalWhisperTranscriber:
    """Offline transcription via faster-whisper, same structured errors."""

    def __init__(
        self,
        model: str = "small",
        language: Optional[str] = None,
        device: str = "cpu",
        compute_type: str = "int8",
        vocabulary: Optional[Sequence[str]] = None,
    ) -> None:
        self.model_name = (model or "small").strip() or "small"
        self.language = (language or "").strip() or None
        self.device = device
        self.compute_type = compute_type
        self.vocabulary = list(vocabulary or [])
        self._hint: Optional[str] = None
        self._model = None

    def ensure_available(self) -> None:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Install it to enable local "
                "voice transcription."
            ) from exc

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel  # type: ignore

        self._model = WhisperModel(
            self.model_name, device=self.device, compute_type=self.compute_type
        )
        return self._model

    def _run(self, audio_path: Path) -> str:
        model = self._ensure_model()
        hints = {}
        if self._hint is None:
            max_length = getattr(model, "max_length", None)
            if not isinstance(max_length, int):
                max_length = DEFAULT_MAX_LENGTH
            kept = fit_vocabulary(self.vocabulary, _token_counter(model),
                                  max_length // 2 - 1)
            self._hint = ", ".join(kept)
            if len(kept) < len(self.vocabulary):
                logger.info("voice vocabulary cut to budget: %d of %d phrases",
                            len(kept), len(self.vocabulary))
        if self._hint:
            parameters = inspect.signature(model.transcribe).parameters
            key = "hotwords" if "hotwords" in parameters else "initial_prompt"
            hints[key] = self._hint
        segments, _info = model.transcribe(
            str(audio_path), language=self.language, beam_size=5, vad_filter=True,
            **hints,
        )
        return "".join(segment.text for segment in segments)

    async def transcribe_audio(
        self, audio_path: Path, duration_seconds: Optional[int] = None
    ) -> str:
        del duration_seconds
        try:
            text = (await asyncio.to_thread(self._run, audio_path)).strip()
        except Exception as exc:
            logger.error("Local whisper failed: %s", exc, exc_info=True)
            raise TranscriptionError("Unable to transcribe audio right now.") from exc
        if not text:
            raise EmptyTranscriptionError("No speech detected in the voice message.")
        return text


def build_transcriber() -> LocalWhisperTranscriber:
    # Explicit machine/instance vocabulary first, then the derived cache.
    return LocalWhisperTranscriber(
        model=config.local_whisper_model, language=config.whisper_language,
        vocabulary=list(config.whisper_vocabulary) + load_derived_vocabulary()
    )
