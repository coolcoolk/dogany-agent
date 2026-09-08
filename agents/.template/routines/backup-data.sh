#!/bin/bash
# backup-data.sh -- daily life-engine DB text backup (DGN-672 canonical).
#
# The engine stem (kit_life | lifekit) is RESOLVED per instance via
# routines/lib/life-engine.sh, never hardcoded (DGN-1238): live fleets exist
# on both coordinates. <stem> below means the resolved name.
#
# Chain: <stem>.sh check (corruption canary) -> <stem>.sh dump -> atomic mv
# -> database/<stem>.sql -> git commit (database/ pathspec only) -> git push.
# The dump self-carries PRAGMA user_version (DGN-672 C4), so a restore knows
# the snapshot's schema version. Restore counterpart: database/restore-data.sh.
#
# Design points (DGN-672 spec, section 2a):
#   * No <stem>.db / <stem>.sh = the NORMAL state of a kitless mint ->
#     silent exit 0 (a daily-failing cron would be noise, not signal) --
#     EXCEPT when the instance config says the kit IS configured and the
#     engine still cannot be found: that logs a daily warning (DGN-1238
#     quiet-failure split) before the same exit 0 (fail-open).
#   * Pre-dump gate: `<stem>.sh check` FAIL -> do NOT dump; keep <stem>.sql
#     (the last good snapshot), push an alert, exit 1. sqlite3 .dump exits 0
#     even on a corrupt DB, so without this gate a corrupt snapshot would
#     silently overwrite the last good one and get committed (G3 canary).
#   * TMP lifecycle (R3): the dump temp file stays INSIDE database/ because
#     atomic mv requires same-filesystem; a crash residue is therefore covered
#     twice -- trap EXIT cleanup here + `database/<stem>.sql.*` in .gitignore.
#   * Commit is pathspec-scoped to database/ (never sweeps unrelated files).
#   * No-remote guard: repo without origin = local commits only, silent skip.
#   * Push failures keep the local commit and exit 0, but are never infinitely
#     silent: a counter (outside git) alerts at 3 consecutive failures, then
#     at every multiple of 7.
#
# Usage: backup-data.sh [--dry]   (--dry = dump only, no commit/push)
# Exit: 0 = success / no-op / push-failed-but-committed; 1 = corruption or
#       dump/commit error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$AGENT_DIR/database"

# DGN-1238: resolve the engine stem (needs .db + .sh); unresolved -> legacy
# stem so the kitless gate below no-ops byte-identically to the old script.
STEM=""
if [[ -f "$SCRIPT_DIR/lib/life-engine.sh" ]]; then
  . "$SCRIPT_DIR/lib/life-engine.sh"
  STEM="$(life_engine_stem "$AGENT_DIR" db sh)"
  if [[ -z "$STEM" ]] && life_engine_configured "$AGENT_DIR"; then
    life_engine_warn_missing "$AGENT_DIR" "backup-data.sh"
  fi
fi
STEM="${STEM:-lifekit}"
DB="$DATA_DIR/$STEM.db"
DUMP="$DATA_DIR/$STEM.sql"
LIFE_SH="$DATA_DIR/$STEM.sh"
PUSH_SH="$SCRIPT_DIR/push.sh"
FAIL_COUNT_FILE="$AGENT_DIR/.telegram_bot/backup-push-fail.count"

DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

# --- 0) kitless mint = normal no-op (spec 2a change 1) ----------------------
if [[ ! -f "$DB" || ! -x "$LIFE_SH" ]]; then
  exit 0
fi

# --- 1) pre-dump corruption canary (spec 2a change 2, G3) -------------------
# check exits 1 on integrity FAIL (DB absence was already handled above).
if ! "$LIFE_SH" check >/dev/null 2>&1; then
  echo "[backup] $STEM DB corruption detected -- dump refused; last good backup preserved ($DUMP)" >&2
  if [[ -x "$PUSH_SH" ]]; then
    "$PUSH_SH" --text "$STEM DB corruption detected -- daily dump refused, last good backup preserved (database/$STEM.sql). Inspect with: $STEM.sh check / restore-data.sh --list" || true
  fi
  exit 1
fi

# --- 2) dump (atomic replace; TMP stays in database/ = same fs, R3) ---------
TMP="$(mktemp "${DUMP}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
# Route signal deaths through the EXIT trap (a TERM'd dump run must not leave
# <stem>.sql.* residue behind; kill -9 is covered by the .gitignore line).
trap 'exit 143' INT TERM HUP
if ! "$LIFE_SH" dump > "$TMP"; then
  echo "[backup] dump failed" >&2
  exit 1
fi
mv "$TMP" "$DUMP"
echo "[backup] $STEM.sql dumped ($(wc -l < "$DUMP" | tr -d ' ') lines)"

if [[ "$DRY" -eq 1 ]]; then
  echo "[backup] --dry: skipping commit/push"
  exit 0
fi

# --- 3) commit (pathspec-scoped, spec 2a change 4) --------------------------
cd "$AGENT_DIR"
git add -- database/
if git diff --cached --quiet -- database/; then
  echo "[backup] no changes -- skip"
  exit 0
fi
STAMP="$(date '+%Y-%m-%d')"
if ! git commit -q -m "backup: $STEM dump $STAMP" -- database/; then
  echo "[backup] commit failed" >&2
  exit 1
fi
echo "[backup] committed ($(git rev-parse --short HEAD))"

# --- 4) push (no-remote guard + bounded-silence fail counter) ---------------
if ! git remote get-url origin >/dev/null 2>&1; then
  # Local-git-only instance (mint.sh guarantees git init): local commit is
  # the backup; push silently skipped (spec 2a, D2 policy).
  exit 0
fi

if git push -q origin HEAD >/dev/null 2>&1; then
  rm -f "$FAIL_COUNT_FILE"
  echo "[backup] pushed"
  exit 0
fi

# Push failed: local commit is kept (exit 0 policy) but never infinitely
# silent (spec 2a change 5). Counter lives outside git.
mkdir -p "$(dirname "$FAIL_COUNT_FILE")"
prev="$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0)"
[[ "$prev" =~ ^[0-9]+$ ]] || prev=0
count=$((prev + 1))
echo "$count" > "$FAIL_COUNT_FILE"
echo "[backup] push failed (local commit kept; consecutive failures: $count)" >&2
if [[ "$count" -eq 3 || ( "$count" -gt 3 && $((count % 7)) -eq 0 ) ]]; then
  if [[ -x "$PUSH_SH" ]]; then
    "$PUSH_SH" --text "$STEM backup push has failed $count times in a row (local commits are safe). Check the backup remote." || true
  fi
fi
exit 0
