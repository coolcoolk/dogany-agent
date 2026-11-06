#!/usr/bin/env bash
# handoff-provision.sh -- recipient-side slug-mailbox provisioning (DGN-1488).
#
# Creates this instance's handoff RECEIVING address:
#   <base>/<slug>/{inbox,done,dead,tmp}
#   base = $DOGANY_HANDOFF_ROOT (tests/fixtures) else $HOME/.dogany/handoff
#
# DGN-635 spec v1 (grill v2, LOCKED -- routines/lib/handoff/handoff.py
# "maildir-by-slug mailboxes"): the RECIPIENT owns mailbox provisioning at
# mint time; a sender NEVER creates a mailbox (FATAL-1 -- handoff.py raises
# MailboxMissingError naming this script as the remedy). DGN-1488 measured
# that the named remedy did not exist in any tree and no mint path
# provisioned, so a fresh mint was born with no reception entrance at all.
# scripts/mint.sh now runs this script on every mint; existing instances
# receive it via the update path and run it once by hand (the sender-side
# MailboxMissingError message points here). Idempotent -- safe to re-run.
#
# Slug resolution mirrors routines/lib/handoff/engine.py own_slug():
# config/agent.conf SLUG= first (first match, quotes stripped; stamped at
# mint per DGN-635 FIX-3), else the instance root's basename lowercased.
# The two derivations MUST stay identical: the mailbox this script creates
# is the address engine.py stamps into outgoing meta["from"], and a reply
# to that from-slug must land in the tree provisioned here.
#
# Deliberately ABSENT here (do not "complete" this file):
#   - handoff_cli.py wiring: the cli is retired permanently (DGN-1379 D1,
#     LOCKED); routines/lib/handoff/engine.py is the only submit entry
#     point, and tests/install_smoke.sh fails the template if a cli file
#     reappears.
#   - consume/watch job registration: the dispatch loop that drains this
#     mailbox is DGN-1379 R2 (design v3.1 sec.4.3), not landed yet --
#     registering a watcher with no consumer would fire into a void.
#
# Usage: handoff-provision.sh [--check]
#   No recipient argument: it provisions THIS instance's own slug only; any
#   other argument exits 2 (DGN-1563).
#   --check   report state and exit 1 when any mailbox dir is missing;
#             writes nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

conf_read() { # <file> <key> -> first-match value; whitespace + quotes stripped
  local file="$1" key="$2" line val
  [ -f "$file" ] || { printf ''; return 0; }
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "${key}="*)
        val="${line#*=}"
        # trim surrounding whitespace, then one layer of matching quotes
        # (same normalization as routines/lib/conf_reader.py conf_get).
        val="$(printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        case "$val" in
          \"*\") val="${val#\"}"; val="${val%\"}" ;;
          \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        printf '%s' "$val"; return 0 ;;
    esac
  done < "$file"
  printf ''
}

SLUG="$(conf_read "$ROOT/config/agent.conf" SLUG)"
if [ -z "$SLUG" ]; then
  SLUG="$(basename "$ROOT" | tr '[:upper:]' '[:lower:]')"
fi
# Same trust boundary as engine.py validate_slug (R1 hardness case 2): the
# slug is joined straight into a path below, so path separators, dot-dirs
# and whitespace are refused loudly, never sanitized silently.
case "$SLUG" in
  ""|.|..|*/*|*[[:space:]]*)
    echo "handoff-provision: invalid slug '${SLUG}' (empty / dot / separator / whitespace)" >&2
    exit 2 ;;
esac

# DGN-1563: this script provisions ONLY this instance's own slug. It takes no
# recipient argument; a peer slug used to be swallowed silently (the caller
# believed the peer was provisioned, only the own mailbox was made). Any
# argument other than --check is refused loudly rather than ignored.
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--check" ]; }; then
  echo "handoff-provision: unexpected argument '$*' -- this script provisions its own slug ('${SLUG}') only and takes no recipient argument; to provision another slug, run it on that instance's side" >&2
  echo "usage: handoff-provision.sh [--check]" >&2
  exit 2
fi

BASE="${DOGANY_HANDOFF_ROOT:-$HOME/.dogany/handoff}"
BOX="$BASE/$SLUG"

MISSING=0
for d in inbox done dead tmp; do
  [ -d "$BOX/$d" ] || MISSING=1
done

if [ "${1:-}" = "--check" ]; then
  if [ "$MISSING" -eq 0 ]; then
    echo "handoff-provision: OK mailbox provisioned: $BOX"
    exit 0
  fi
  echo "handoff-provision: MISSING mailbox tree at $BOX -- run routines/handoff-provision.sh" >&2
  exit 1
fi

if [ "$MISSING" -eq 0 ]; then
  echo "handoff-provision: mailbox already provisioned: $BOX"
  exit 0
fi
for d in inbox done dead tmp; do
  mkdir -p "$BOX/$d"
done
echo "handoff-provision: provisioned mailbox: $BOX (slug=$SLUG)"
