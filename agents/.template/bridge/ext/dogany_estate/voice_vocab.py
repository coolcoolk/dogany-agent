"""DGN-1694: refresh the derived voice vocabulary cache at bridge start.

The reader itself is framework code in the instance
(routines/lib/voice_vocab.py): it reads the estate's existing name owners
(mint manifest, persona, estate instances, skill display names, pack catalog,
kit vocabulary verb) and writes BOT_DATA_DIR/voice-vocab.json. The bridge core
(bridge/voice.py) only reads that cache when it builds its transcriber.

Runs in a daemon thread: a kit verb may take seconds, and a boot hook must
never block the boot. Every failure is absorbed with ONE log line; the
previous cache (or none) stays in effect.
"""

import logging
import subprocess
import sys
import threading

from bridge import config as config_mod

logger = logging.getLogger(__name__)

READER_REL = ("routines", "lib", "voice_vocab.py")
TIMEOUT_S = 60


def _refresh() -> None:
    root = config_mod.PROJECT_ROOT
    reader = root.joinpath(*READER_REL)
    if not reader.is_file():
        logger.info("voice vocabulary reader absent (%s); cache not refreshed", reader)
        return
    try:
        result = subprocess.run(
            [sys.executable, str(reader), str(root), "--write"],
            cwd=str(root), capture_output=True, text=True, timeout=TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("voice vocabulary refresh failed: %s", exc)
        return
    if result.returncode != 0:
        logger.warning("voice vocabulary refresh failed (rc=%d): %s",
                       result.returncode, (result.stderr or "").strip()[-300:])
        return
    count = len([ln for ln in result.stdout.splitlines() if ln.strip()])
    logger.info("voice vocabulary cache refreshed: %d terms", count)


def refresh_voice_vocab() -> threading.Thread:
    thread = threading.Thread(target=_refresh, name="voice-vocab-refresh", daemon=True)
    thread.start()
    return thread
