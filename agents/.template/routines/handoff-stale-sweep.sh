#!/bin/bash
# handoff-stale-sweep.sh -- DGN-635 FIX-4 stale-mailbox backstop (DGN-695
# recurrence guard). Any file older than HANDOFF_STALE_HOURS (default 24h)
# in the own mailbox inbox/ or dead/ (plus the legacy inbox during the
# transition) -> ONE session-inbox notice listing them. Runs from the daily
# launchd unit; no state -- a still-stale file is re-listed on the next run.
#
# DGN-1462: notify the maintainer through session-inbox, never the owner.
# Atomic hidden-temp -> *.md rename matches the handoff notifier contract.
# Delivery failure logs the paths and leaves mail untouched for the next tick;
# there is no owner-push fallback.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="${AGENT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HOURS="${HANDOFF_STALE_HOURS:-24}"
SESSION_INBOX="${HANDOFF_SESSION_INBOX:-$AGENT_ROOT/.telegram_bot/session-inbox}"

SLUG="${HANDOFF_SELF_SLUG:-$(sed -n 's/^SLUG=//p' "$AGENT_ROOT/config/agent.conf" 2>/dev/null | head -1)}"
[[ -n "$SLUG" ]] || { echo "handoff-stale-sweep: SLUG= missing" >&2; exit 1; }
MAILBOX="${DOGANY_HANDOFF_ROOT:-$HOME/.dogany/handoff}/$SLUG"

# inject_session <text>: same atomic write handoff-consume.sh's
# _inject_session() does. Returns nonzero on any failure (dir missing,
# write/rename failed) without touching set -e -- callers gate on it with
# if/else so a failure never aborts the script.
inject_session() {
  local text="$1"
  [[ -d "$SESSION_INBOX" ]] || return 1
  local fname="handoff-stale-$(date -u +%Y%m%dT%H%M%SZ)-$$.md"
  local tmp="$SESSION_INBOX/.$fname"
  printf '%s\n' "$text" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$SESSION_INBOX/$fname" 2>/dev/null || { rm -f "$tmp"; return 1; }
  return 0
}

# A missing optional legacy inbox is normal. A missing own mailbox or a
# failed find is an incomplete scan, never a clean result.
SCAN_COMPLETE=1
SCAN_DIRS=()
for dir in "$MAILBOX/inbox" "$MAILBOX/dead" "$AGENT_ROOT/files/handoff/inbox"; do
  if [[ -d "$dir" ]]; then
    SCAN_DIRS+=("$dir")
  elif [[ "$dir" != "$AGENT_ROOT/files/handoff/inbox" || -e "$dir" ]]; then
    echo "handoff-stale-sweep: scan unavailable: $dir" >&2
    SCAN_COMPLETE=0
  fi
done
STALE=""
if [[ ${#SCAN_DIRS[@]} -gt 0 ]]; then
  STALE="$(find "${SCAN_DIRS[@]}" -type f -name '*.md' -mmin "+$((HOURS * 60))")" || SCAN_COMPLETE=0
fi
if [[ "$SCAN_COMPLETE" -eq 0 ]]; then
  echo "handoff-stale-sweep: scan incomplete -- will retry next tick" >&2
fi
if [[ -n "$STALE" ]]; then
  {
    echo "handoff-stale-sweep: stale (>${HOURS}h) un-triaged in mailbox '$SLUG':"
    echo "$STALE"
  } >&2
  MSG="[handoff-stale-sweep] un-triaged (>${HOURS}h) in mailbox '$SLUG' -- triage now:"$'\n'"$STALE"
  if inject_session "$MSG"; then
    echo "handoff-stale-sweep: delivered stale notice to session-inbox"
  else
    echo "handoff-stale-sweep: session-inbox delivery FAILED, stale items still un-triaged (see stderr for paths) -- will retry next tick" >&2
  fi
elif [[ "$SCAN_COMPLETE" -eq 1 ]]; then
  echo "handoff-stale-sweep: scanned ${#SCAN_DIRS[@]} directories, 0 stale files"
fi

# DGN-635 MINOR: prune consumed mail in done/ past retention (default 60d),
# mirroring the archive 60-day policy. done/ is recipient-owned, so a local
# prune here (not the install-root retention()) keeps the maildir bounded.
DONE_KEEP_DAYS="${HANDOFF_DONE_KEEP_DAYS:-60}"
find "$MAILBOX/done" -type f -mtime "+$DONE_KEEP_DAYS" -delete 2>/dev/null || true

# Fail-open: this routine never fails the cron-guard wrapper on a delivery
# problem -- only the SLUG= config-error exit above is a real routine
# failure. A scanned result (clean or stale) always reaches exit 0.
exit 0
