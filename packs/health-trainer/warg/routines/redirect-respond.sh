#!/bin/bash
# redirect-respond.sh <inbox-md-file> -- Warg-side responder for relayed
# misdelivery messages (dec-013 B', OQ-T deliverable).
#
# Invoked by the consume path (handoff_cli warg_handlers.on_redirect) with
# the inbox message path. Contract:
#   - headless claude runs IN THE WARG WORKSPACE (transcript + memory
#     accrue here -- charter decision 2 rationale), handling the relayed
#     utterance as a normal domain turn (log / answer / coach).
#   - the answer goes out through push-gated.sh --trigger
#     redirect-response (user-initiated classification baked into the
#     trigger id; V4 unsolicited counter does not apply -- OQ-R).
#   - non-zero exit leaves the message in the inbox for the next sweep
#     (consume handler catches the failure; fail-safe, never silent).
#   - per-ulid responded marker makes the handler idempotent on the ulid
#     key (consume contract): a crash AFTER the push but BEFORE archive
#     re-runs this script on re-sweep -- the marker skips regeneration,
#     the exit 0 lets consume archive. No duplicate user response.
#
# Test seams (sandbox; unset in deployment):
#   HANDOFF_REDIRECT_GENERATOR  argv: <prompt-file>; stdout = answer text
#   WARG_PUSH_CMD               replaces push-gated.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"
PROMPT_FILE="$SCRIPT_DIR/prompts/redirect-respond.md"
STATE_DIR="$WARG_ROOT/.telegram_bot/state"
IDX="$STATE_DIR/redirect-responded.idx"
PUSH_CMD="${WARG_PUSH_CMD:-$SCRIPT_DIR/push-gated.sh}"

MSG_PATH="${1:?usage: redirect-respond.sh <inbox-md-file>}"
[ -f "$MSG_PATH" ] || { echo "no such message: $MSG_PATH" >&2; exit 1; }
mkdir -p "$STATE_DIR"

# parse the channel message with the channel's own parser (one owner)
PARSED="$(python3 - "$MSG_PATH" "$LIB_DIR" <<'PY'
import json, os, sys
path, libdir = sys.argv[1:3]
sys.path.insert(0, libdir)
import handoff
with open(path) as f:
    meta, body = handoff.parse_message(f.read())
rels = (meta.get("payload") or {}).get("attachments") or ""
inbox = os.path.dirname(path)
att = [os.path.join(inbox, r) for r in rels.split(",") if r]
print(json.dumps({"ulid": str(meta.get("id") or ""), "body": body,
                  "attachments": att}))
PY
)"
ULID="$(printf '%s' "$PARSED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ulid"])')"
[ -n "$ULID" ] || { echo "message has no ulid" >&2; exit 1; }

# idempotency belt: already responded -> let consume archive quietly
if [ -f "$IDX" ] && grep -qxF "$ULID" "$IDX"; then
  echo "already responded to $ULID (marker); skipping regeneration"
  exit 0
fi

# assemble the turn prompt: responder prompt + relayed utterance (+ photos)
TURN_FILE="$(mktemp)"
trap 'rm -f "$TURN_FILE"' EXIT
cat "$PROMPT_FILE" > "$TURN_FILE"
printf '%s' "$PARSED" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("\n## Relayed utterance (verbatim)\n")
print(d["body"] or "(empty)")
if d["attachments"]:
    print("\n## Attached photos (Read these files first)\n")
    for a in d["attachments"]:
        print("- " + a)
' >> "$TURN_FILE"

if [ -n "${HANDOFF_REDIRECT_GENERATOR:-}" ]; then
  ANSWER="$("$HANDOFF_REDIRECT_GENERATOR" "$TURN_FILE")"
else
  # env -u strips CLAUDECODE + CLAUDE_CODE_ENTRYPOINT so headless claude
  # does not see an enclosing session and refuses to start.
  ANSWER="$(cd "$WARG_ROOT" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "$(cat "$TURN_FILE")" --allowedTools "Bash,Read,Write,Edit,Glob,Grep")"
fi
[ -n "$ANSWER" ] || { echo "empty answer from generator" >&2; exit 1; }

"$PUSH_CMD" --trigger redirect-response --text "$ANSWER"

printf '%s\n' "$ULID" >> "$IDX"
echo "responded to $ULID"
