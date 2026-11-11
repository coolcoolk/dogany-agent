"""Workbench pin buttons (DGN-1966): the bridge half of a one-tap watch toggle.

The pinned workbench board can carry an inline keyboard (bridge/dashboard.py
reads it from dashboard-actions.json).  A tap arrives as callback data
``wb:watch:<id>:on|off``; bot.py checks the owner (the same _check_access
gate as every button) and hands it here.  This module only forwards: the
host's routines/lib/watches.py owns the switch and watches.json (it disarms
or arms the job and records the health observer's retire / unretire), and
routines/status-footer.py --board-refresh re-renders the board with the new
state.  Absent host scripts -> "unavailable", nothing else happens.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PREFIX = "wb:"
WATCH_RE = re.compile(r"^wb:watch:([a-z0-9][a-z0-9-]{0,31}):(on|off)$")
TOGGLE_TIMEOUT_SECS = 90


def parse_watch(data: Optional[str]) -> Optional[Tuple[str, bool]]:
    """(watch_id, want_on) for a well-formed watch tap, else None."""
    m = WATCH_RE.match(data or "")
    if not m:
        return None
    return m.group(1), m.group(2) == "on"


def toggle_watch(root: Path, watch_id: str, want_on: bool) -> Tuple[int, str]:
    """Run watches.py toggle (blocking; call off the event loop)."""
    lib = Path(root) / "routines" / "lib" / "watches.py"
    if not lib.is_file():
        return 127, "watches.py unavailable"
    try:
        proc = subprocess.run(
            [sys.executable, str(lib), "toggle", watch_id,
             "on" if want_on else "off", "--by", "owner-tap"],
            capture_output=True, text=True, timeout=TOGGLE_TIMEOUT_SECS,
        )
    except Exception as e:  # noqa: BLE001 - a tap must never raise
        return 99, str(e)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def refresh_board(root: Path) -> None:
    """Fire status-footer.py --board-refresh detached (it debounces)."""
    script = Path(root) / "routines" / "status-footer.py"
    if not script.is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(script), "--board-refresh"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("workbench board refresh not started: %s", e)
