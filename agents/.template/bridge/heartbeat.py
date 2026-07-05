"""getUpdates liveness heartbeat (DGN-140, layer 1 of the two-layer watchdog).

Zombie polling = process alive, zero updates received (seen after laptop
sleep/wake). Layer 1: bot.py checks stalled() in-process and restarts polling.
Layer 2: the external bridge/watchdog.sh reads the heartbeat FILE's mtime and
kickstarts the whole service when even layer 1 is dead.

Touch placement is exception-selective ON PURPOSE (grill FATAL-1): the beat
fires only on (a) a successful getUpdates round trip and (b) TimedOut /
NetworkError raised by the transport -- both prove the polling task is still
scheduling requests. It must NEVER fire from a bare finally or on other
exceptions: a swallowed-RuntimeError hot-spin zombie would then keep
"beating" and hide the exact failure this heartbeat exists to expose.

In-process staleness uses time.monotonic(). On macOS that is
mach_absolute_time, which PAUSES during system sleep (verified 2026-07-05),
so waking from a long sleep does not instantly read as a stall; only real
dead time while running counts. The file carries wall-clock epoch for the
external watchdog, which has its own two-strike sleep/wake absorption.
"""

import os
import time
from typing import Optional

import telegram.error
from telegram.request import HTTPXRequest

from bridge.config import config

HEARTBEAT_FILE = config.bot_data_dir / "poll_heartbeat"
_WRITE_THROTTLE_S = 5.0  # min seconds between disk writes

_last_beat: Optional[float] = None  # monotonic time of the last beat
_last_write: float = 0.0  # monotonic time of the last disk write


def touch() -> None:
    """Record liveness. Best-effort: never raises, disk writes throttled."""
    global _last_beat, _last_write
    try:
        now = time.monotonic()
        _last_beat = now
        if _last_write > 0 and (now - _last_write) < _WRITE_THROTTLE_S:
            return
        _last_write = now
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HEARTBEAT_FILE.with_name(HEARTBEAT_FILE.name + ".tmp")
        tmp.write_text(str(int(time.time())), encoding="ascii")
        os.replace(tmp, HEARTBEAT_FILE)  # atomic within the same dir
    except Exception:
        pass


def stalled(threshold_s: float) -> bool:
    """True when a beat has been seen and the last one is older than threshold."""
    if _last_beat is None:
        return False
    return (time.monotonic() - _last_beat) > threshold_s


class HeartbeatHTTPXRequest(HTTPXRequest):
    """HTTPXRequest that beats the poll heartbeat around each request.

    Wire this ONLY as the get_updates_request so every request through this
    transport is a getUpdates long-poll round trip.
    """

    async def do_request(self, *args, **kwargs):
        try:
            result = await super().do_request(*args, **kwargs)
        except (telegram.error.TimedOut, telegram.error.NetworkError):
            # Transport timeouts/blips still prove the polling loop is alive
            # and scheduling requests: beat, then re-raise unchanged.
            touch()
            raise
        touch()
        return result
