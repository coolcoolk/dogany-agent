"""DGN-1995: one owner-facing line from explicitly named usage windows.

The provider's utilization is USED percent. Only this formatter converts it
to LEFT percent; callers pass the resulting owner_usage_line through.
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def reset_passed(value, now=None):
    """Whether an ISO or epoch (seconds/milliseconds) reset is due."""
    if value in (None, "") or isinstance(value, bool):
        return False
    try:
        try:
            stamp = float(value)
            if stamp > 1e12:
                stamp /= 1000
        except (TypeError, ValueError):
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            stamp = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        return stamp <= (time.time() if now is None else now)
    except (ValueError, TypeError, OverflowError):
        return False


def snapshot_expired(snapshot, now=None):
    """Any passed window invalidates a TTL hit, including scoped limits."""
    now = time.time() if now is None else now
    if isinstance(snapshot, dict):
        return any((key.endswith("resets_at") and reset_passed(value, now))
                   or snapshot_expired(value, now)
                   for key, value in snapshot.items())
    if isinstance(snapshot, list):
        return any(snapshot_expired(value, now) for value in snapshot)
    return False


def owner_usage_line(snapshot):
    """Render each window independently; absent/invalid usage is unknown."""
    def window(key, label):
        row = snapshot.get(key) if isinstance(snapshot, dict) else None
        used = row.get("utilization") if isinstance(row, dict) else None
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or not 0 <= used <= 100
                or reset_passed(row.get("resets_at"))):
            return label + " unavailable"
        return "%s %g%% left" % (label, round(100 - used, 2))

    if isinstance(snapshot, dict) and "five_hour" in snapshot and snapshot["five_hour"] is None:
        return window("seven_day", "weekly")
    return "%s (%s)" % (window("seven_day", "weekly"),
                         window("five_hour", "5h"))


def _claude_snapshot_context():
    """Read the existing snapshot on every turn, without fetching or writing.

    Old dispatch output can predate a redeemed pass. A missing/stale snapshot
    must therefore replace old numbers with unknown, never resurrect them.
    """
    prefix = "[Claude subscription usage] "
    unavailable = (prefix + "Current usage unavailable. Before quoting quota, "
                   "read routines/claude-usage.sh --json and use owner_usage_line; "
                   "earlier conversation/dispatch figures are historical.")
    try:
        folder = Path(os.environ.get("DOGANY_USAGE_SNAPSHOT_DIR")
                      or Path.home() / ".dogany" / "usage")
        with (folder / "snapshot.json").open() as stream:
            age = time.time() - os.fstat(stream.fileno()).st_mtime
            ttl = float(os.environ.get("DOGANY_USAGE_SNAPSHOT_TTL_SEC", "300"))
            if not math.isfinite(ttl) or not 0 <= age <= ttl:
                return unavailable
            snapshot = json.load(stream)
        if not isinstance(snapshot, dict):
            return unavailable
        # A reset crossed after the snapshot makes that window unknown.
        for key in ("five_hour", "seven_day"):
            row = snapshot.get(key)
            if isinstance(row, dict) and row.get("resets_at"):
                reset = datetime.fromisoformat(row["resets_at"].replace("Z", "+00:00"))
                if reset.tzinfo is None or reset.timestamp() <= time.time():
                    snapshot[key] = dict(row, utilization=None)
        return (prefix + 'owner_usage_line="' + owner_usage_line(snapshot)
                + '" (snapshot age %ds). Use this labelled line for quota claims; '
                'it supersedes earlier conversation/dispatch figures. '
                'utilization means used, not left.' % int(age))
    except (OSError, ValueError, TypeError, AttributeError):
        return unavailable


def snapshot_context(workspace=None):
    """Keep owner_usage_line intact, then add current dispatch pool evidence."""
    line = _claude_snapshot_context()
    try:
        from dispatch_pools import read_pools, render
        return line + "\n" + render(read_pools(workspace))
    except ImportError:
        # Partial/older installs retain their existing Claude context.
        return line
