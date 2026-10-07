"""The model that actually answered, per bridge process (DGN-1814 r2).

Why: settings said "sonnet" on two agents while the CLIs behind them resolved
different models (claude-sonnet-5 vs claude-sonnet-5-5). The only truth is the
id the CLI itself reports, so the bridge records the `model` of every
system/init message it reads (sdk_bridge, normal + proactive paths) into
<bot_data_dir>/state/live_model.json. Settings are never consulted.

Record (one JSON object, atomic replace):
  model        id that answered most recently (next process compares to this)
  first_model  first id this process observed
  prev         `model` recorded before this process started ("" = none, i.e.
               first-ever run -- never announced)
  boot         this process's start epoch (import time)

Reader: bridge/self_restart.sh. After the restart it drops the verify spool
(whose turn produces the first init message), then asks `changed` below for a
bounded time. A changed model folds one line into the ONE restart notice
("<emoji> Restart complete · Now answering with Claude Sonnet 5.5."); an
unchanged or first-ever model adds nothing.

CLI (stdlib only, no bridge imports -- the worker runs it with any python3):
  live_model.py changed <state.json> <since_epoch> <timeout_s> [<model_display_dir>]
    Waits until a process that booted at/after <since_epoch> recorded its
    first model. rc 0 + prints the new model's display name when it differs
    from `prev` (empty line when unchanged / first-ever). rc 3 when no prior
    record exists at all (nothing to compare -> caller must not wait).
    rc 2 on timeout. Display names come from routines/lib/model_display.py
    (the one owner-facing table); unknown ids fall back to the raw id.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

BOOT = time.time()
STATE_NAME = "live_model.json"

_first_seen: Optional[str] = None


def state_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / "state" / STATE_NAME


def read_record(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(doc, ensure_ascii=True), encoding="ascii")
    os.replace(tmp, path)


def observe(model_id: Any, path: Path) -> None:
    """Record a model id the CLI reported. Never raises."""
    global _first_seen
    if not isinstance(model_id, str) or not model_id.strip():
        return
    mid = model_id.strip()
    try:
        rec = read_record(path)
        if _first_seen is None:
            _first_seen = mid
            prev = rec.get("model") if isinstance(rec.get("model"), str) else ""
            _write(path, {"model": mid, "first_model": mid, "prev": prev,
                          "boot": BOOT})
        elif rec.get("model") != mid:
            rec["model"] = mid
            _write(path, rec)
    except Exception:  # noqa: BLE001 - presentation state, never break a turn
        pass


def this_process_model(path: Path) -> str:
    """Id that answered most recently, only once THIS process saw one.

    "" before this process's first init (a record left by an earlier process
    may predate a CLI or settings change -- the /model picker then shows the
    configured name without a version rather than a stale one).
    """
    if _first_seen is None:
        return ""
    mid = read_record(path).get("model")
    return mid.strip() if isinstance(mid, str) else ""


def _norm(mid: str) -> str:
    return re.sub(r"\[[^\]]*\]$", "", (mid or "").strip().lower())


def display(mid: str, lib_dir: Optional[str]) -> str:
    if lib_dir:
        try:
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            import model_display  # type: ignore
            return model_display.parse_model_id(mid) or mid
        except Exception:  # noqa: BLE001 - raw id beats no line
            pass
    return mid


def changed(path: Path, since: float, timeout: float,
            lib_dir: Optional[str] = None, interval: float = 0.5):
    """(rc, display). See module doc for rc meaning."""
    rec = read_record(path)
    if not rec.get("model"):
        return 3, ""
    deadline = time.time() + max(0.0, timeout)
    while True:
        rec = read_record(path)
        try:
            fresh = float(rec.get("boot", 0)) >= since
        except (TypeError, ValueError):
            fresh = False
        if fresh and rec.get("first_model"):
            new, prev = rec["first_model"], rec.get("prev") or ""
            if not prev:
                return 0, ""
            new_disp, prev_disp = display(new, lib_dir), display(prev, lib_dir)
            if new_disp == prev_disp or _norm(new) == _norm(prev):
                return 0, ""
            return 0, new_disp
        if time.time() >= deadline:
            return 2, ""
        time.sleep(min(interval, max(0.0, deadline - time.time())))


def main(argv) -> int:
    args = argv[1:]
    if len(args) >= 4 and args[0] == "changed":
        try:
            since, timeout = float(args[2]), float(args[3])
        except ValueError:
            return 2
        rc, disp = changed(Path(args[1]), since, timeout,
                           args[4] if len(args) > 4 else None)
        if rc == 0:
            print(disp)
        return rc
    print("usage: live_model.py changed <state> <since> <timeout> [<lib>]",
          file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
