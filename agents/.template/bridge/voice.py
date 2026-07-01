"""Voice input: ffmpeg audio prep + local faster-whisper transcription.

Input only (no TTS). A voice note is downloaded by the bot, converted to a
whisper-friendly format here when needed, transcribed locally, and the resulting
text is fed back into the normal text pipeline prefixed with a microphone glyph.
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Sequence

from bridge.config import config

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.model_name = (model or "small").strip() or "small"
        self.language = (language or "").strip() or None
        self.device = device
        self.compute_type = compute_type
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
        segments, _info = model.transcribe(
            str(audio_path), language=self.language, beam_size=5, vad_filter=True
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
    return LocalWhisperTranscriber(
        model=config.local_whisper_model, language=config.whisper_language
    )
