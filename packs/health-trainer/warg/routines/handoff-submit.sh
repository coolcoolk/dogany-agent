#!/bin/bash
# handoff-submit.sh <type> -- Warg public entrypoint: generate + submit a
# briefing section to the Ag inbox (v3 5.1 wake contract, 5.2 flow).
#
#   type: morning | retro | weekly   (-> report.section.<type>)
#
# Generation concurrency guard (v3 5.2, grill-2 MINOR-5):
#   (a) per-type lock  .gen-<type>.lock (flock(2) via python; retry run
#       overlapping a live first run exits immediately)
#   (b) same-day submitted state file: once submitted, later runs exit 0
#       (idempotent; the 04:45/20:45 retry slots become no-ops on success)
#
# Section content: headless claude with routines/prompts/section-<type>.md
# unless HANDOFF_SECTION_GENERATOR overrides (tests / dry runs).
# Section expires = the matching Ag aggregation deadline (05:00 / 21:00 /
# Sun 22:00 local), metadata only: the section stays valid for its whole
# target day (aggregation fires AT the deadline; grill-final FATAL-1) and
# is archived with a reason note once the day is over -- it can never
# leak into tomorrow's briefing (created-today filter).
#
# dec-013 backstop: the retro run doubles as the 20:15 sweep -- kick the
# consume entrypoint first (non-blocking; flock keeps it single).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"

# Peer resolution (DGN-284 #6): env override -> own agent.conf -> NONE.
# A fresh/standalone mint has no HANDOFF_PEER_AG key; submitting sections
# nowhere is correct -- never fall back to a hardcoded live Ag path.
AG_ROOT="${HANDOFF_PEER_AG:-}"
if [ -z "$AG_ROOT" ] && [ -f "$WARG_ROOT/config/agent.conf" ]; then
  # no-match grep must not trip set -e/pipefail (fresh mint has no key)
  AG_ROOT="$({ grep -E '^HANDOFF_PEER_AG=' "$WARG_ROOT/config/agent.conf" || true; } | head -1 | cut -d= -f2-)"
fi
if [ -z "$AG_ROOT" ]; then
  echo "[handoff-submit] no handoff peer configured (HANDOFF_PEER_AG unset) -- standalone instance, nothing to submit"
  exit 0
fi

TYPE="${1:-}"
case "$TYPE" in
  morning|retro|weekly) ;;
  *) echo "usage: handoff-submit.sh {morning|retro|weekly}" >&2; exit 1 ;;
esac

# sweep ride-along (retro run = 20:15 backstop sweep)
if [ "$TYPE" = "retro" ]; then
  nohup "$SCRIPT_DIR/handoff-consume.sh" >/dev/null 2>&1 &
fi

exec python3 "$LIB_DIR/section_submit.py" \
  --root "$WARG_ROOT" --ag-root "$AG_ROOT" --type "$TYPE"
