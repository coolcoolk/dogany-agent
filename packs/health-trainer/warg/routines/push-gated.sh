#!/bin/bash
# push-gated.sh -- Warg's ONLY unsolicited-push path (v3 section 7(a)).
#
# NEW code, not a budget.py port (that tool is the question budget).
# Contract:
#   push-gated.sh --trigger <id> [push.sh args...]
# --trigger is REQUIRED; the id must be whitelisted in config/triggers.yaml
# and inside its per-trigger daily counter. Refusal = exit 1 + log, and
# NOTHING is sent. On allow, forwards the remaining args verbatim to the
# framework push.sh next to this script.
#
# There is deliberately NO solicited/unsolicited self-declare flag: every
# caller is a script with the trigger id baked in (DGN-071 distrust).
# Registry additions = user approval (V3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"
TRIGGERS="${WARG_TRIGGERS:-$WARG_ROOT/config/triggers.yaml}"
STATE_DIR="$WARG_ROOT/.telegram_bot/state"

TRIGGER=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --trigger) TRIGGER="${2:-}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

VERDICT="$(python3 - "$TRIGGERS" "$STATE_DIR" "$TRIGGER" "$LIB_DIR" <<'PY'
import sys
triggers, state_dir, trigger, libdir = sys.argv[1:5]
sys.path.insert(0, libdir)
import push_gate
reg = push_gate.load_registry(triggers)
ok, reason = push_gate.check_and_count(state_dir, reg, trigger or None)
print(("ALLOW" if ok else "DENY") + " " + reason)
PY
)"

echo "[push-gated] $VERDICT" >&2
case "$VERDICT" in
  ALLOW*) exec "$SCRIPT_DIR/push.sh" "${ARGS[@]}" ;;
  *)      exit 1 ;;
esac
