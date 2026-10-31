"""Pack-declared flow callbacks; see FLOW-TAP-CONTRACT.md (DGN-2036)."""

import json
import logging
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)
PREFIX = "flow|"
TIMEOUT_SECONDS = 30


def dispatch(root: Path, data: str) -> bool:
    """Run the configured argv with the opaque tap appended, off the event loop.

    Configuration is instance-owned, never supplied by the callback. No shell,
    interpolation, or model is involved. The pack owns validation and rendering.
    """
    started = 0.0
    try:
        conf = dotenv_values(root / "config" / "agent.conf", interpolate=False)
        argv = json.loads(conf.get("FLOW_TAP_COMMAND") or "null")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv)
                or not argv[0]):
            raise ValueError("FLOW_TAP_COMMAND must be a nonempty JSON argv")
        # Resolve relative commands from the instance root, never from PATH.
        argv[0] = str(root / argv[0])
        started = time.monotonic()
        try:
            result = subprocess.run(
                [*argv, data], cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("flow tap command timed out; retrying once")
            result = subprocess.run(
                [*argv, data], cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TIMEOUT_SECONDS, check=False,
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("flow tap done rc=%s elapsed_ms=%s", result.returncode, elapsed_ms)
        if result.returncode == 0:
            return True
        logger.warning("flow tap command exited %s", result.returncode)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("flow tap command failed: %s elapsed_ms=%s", exc,
                       int((time.monotonic() - started) * 1000) if started else -1)
    return False
