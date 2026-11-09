"""Read-only DGN-2339 use observations after an instance update.

No PM reads or code, no execution of kit adapters, no service mutations.
Only framework receipts, registrations, retirement records and mirror
queue counters are observed. Evidence is collected here, never accepted
from a caller-supplied use count.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import update_defer

UNITS = {"mirror-poll", "mirror-reconcile", "routine-roller"}


def collect(root):
    root = Path(root).resolve(strict=True)
    path = update_defer.receipt_path(root)
    # Missing receipts must not become read_receipt's legacy empty default.
    raw = path.read_bytes()
    receipt = update_defer.read_receipt(root)
    if receipt["pending"]:
        raise ValueError("update incomplete")
    definitions = update_defer.definitions(root)
    units = {label for label in definitions if label.rsplit(".", 1)[-1] in UNITS}
    if len(units) != 3 or not units <= set(receipt["active"]):
        raise ValueError("three activated routine registrations required")
    if units & update_defer.retired_labels(root):
        raise ValueError("routine retirement recorded")
    for label in sorted(units):
        proc = subprocess.run([os.environ.get("ROUTINE_CTL_LAUNCHCTL", "launchctl"),
                               "print", "gui/" + str(os.getuid()) + "/" + label],
                              capture_output=True, timeout=5, check=False)
        if proc.returncode:
            raise ValueError("routine registration unavailable: " + label)
    db = root / "mirror/mirror_state.db"
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
        # Failed/claimed work remains outstanding; a failed drain is not use.
        pending = conn.execute("SELECT count(*) FROM mirror_outbox WHERE status != 'pushed'").fetchone()[0]
        pushed = conn.execute("SELECT count(*) FROM mirror_outbox WHERE status = 'pushed'").fetchone()[0]
    return {"schema": "routine-use/1", "root": str(root), "at": time.time(),
            "update": hashlib.sha256(raw).hexdigest(), "update_mtime_ns": path.stat().st_mtime_ns,
            "units": sorted(units), "queue": pending, "pushed": pushed}


def positive(before, after):
    return (before["root"] == after["root"] and before["update"] == after["update"]
            and before["update_mtime_ns"] == after["update_mtime_ns"]
            and before["units"] == after["units"] and before["at"] < after["at"]
            and before["queue"] > after["queue"] and before["pushed"] < after["pushed"])
