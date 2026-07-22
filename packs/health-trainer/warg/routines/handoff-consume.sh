#!/bin/bash
# handoff-consume.sh -- Warg public wake entrypoint (v3 5.1 wake contract).
#
# Single execution via flock(2) on files/handoff/.consume.lock (taken
# inside the python CLI -- macOS has no flock(1)). A second invocation
# while one runs exits 0 immediately; the running instance's rescan loop
# picks up anything dropped meanwhile. Fired by:
#   - launchd WatchPaths on files/handoff/inbox (dec-013 B' event ping)
#   - the scheduled sweeps 04:05 / 12:30 / 20:15 (loss backstop)
#   - peer best-effort nohup wake (flock busy -> 3x30s retry -> give up;
#     the message stays in the inbox = sweep belt)
#
# Per-run processing cap: HANDOFF_RUN_CAP (default in lib). Remainder
# waits for the next run (dec-013 pathological-inflow ceiling).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"

exec python3 "$LIB_DIR/handoff_cli.py" consume \
  --root "$WARG_ROOT" --side warg "$@"
