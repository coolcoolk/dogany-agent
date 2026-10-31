#!/bin/bash
# DGN-1624: report adapter; never dispatch notifications or service operations.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/py_resolve.sh"
pack_resolve_py3 || exit 1
_gate_stop() {
  "$PACK_PY3" - "$SCRIPT_DIR/gates.json" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))['gates'][sys.argv[2]]['user_hint_ko'])
PY
}
rc=0
report="$("$PACK_PY3" "$SCRIPT_DIR/lib/mint_retire.py" "$@" 2>&1)" || rc=$?
if [[ "$rc" != 0 ]]; then
  case "${report%%$'\n'*}" in
    retire.request) _gate_stop retire.request ;;
    retire.recovery) _gate_stop retire.recovery ;;
    retire.evidence) _gate_stop retire.evidence ;;
    retire.units-up) _gate_stop retire.units-up ;;
    retire.busy) _gate_stop retire.busy ;;
    retire.restore-conflict) _gate_stop retire.restore-conflict ;;
    retire.restore-incompatible) _gate_stop retire.restore-incompatible ;;
    retire.partial) _gate_stop retire.partial ;;
    *) _gate_stop retire.prepare ;;
  esac
fi
printf '%s\n' "$report"
exit "$rc"
