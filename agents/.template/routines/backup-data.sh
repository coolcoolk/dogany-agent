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
#   * Pre-dump gate: use `<stem>.sh check` when that verb exists; legacy
#     engines without it fall back to read-only sqlite3 integrity_check. A
#     verified FAIL does NOT dump; it keeps <stem>.sql (the last good
#     snapshot), pushes an alert, and exits 1. If neither verifier exists,
#     log that the check is unavailable and continue: an unsupported command
#     is not evidence of corruption, and refusing every legacy backup would
#     be less safe than preserving normal backup cadence.
#   * TMP lifecycle (R3): the dump temp file stays INSIDE database/ because
#     atomic mv requires same-filesystem; a crash residue is therefore covered
#     twice -- trap EXIT cleanup here + `database/<stem>.sql.*` in .gitignore.
#   * Commit is pathspec-scoped to database/ (never sweeps unrelated files).
#   * No-remote guard: repo without origin = local commits only, silent skip.
#   * Push failures keep the local commit and exit 0, but are never infinitely
#     silent: a counter (outside git) alerts at 3 consecutive failures, then
#     at every multiple of 7.
#   * Push failures say WHY (DGN-1696): git push stderr is captured and the
#     pre-push gate's PUSH BLOCKED / STOP lines plus each sweep hit, REDACTED
#     to "catN <name> <file>:<line>" (never the matched text), go to the
#     backup log (stderr); the hit list also rides the owner alert. A bare
#     "push failed" x35 hid a one-line cause for six days.
#   * Backup pushes are gated by the secret sweep + D7 only (DGN-1724): the
#     push exports DOGANY_PUSH_PURPOSE=owner-backup, and git-hooks/pre-push
#     skips its code-test lane for that purpose when every pushed commit is a
#     database/*.sql dump (or the repo is a declared private backup). Never
#     --no-verify: the sweeps always run. Every other push keeps the lane.
#   * Push failure record (DGN-1724): each failure writes the hook STAGE
#     (sweep | d7 | wiring | test-lane | hook | git) and the first failing
#     line (redacted) to $FAIL_REASON_FILE and the log. The consecutive count
#     never drops below the number of unpushed backup commits (derived from
#     git, so a lost counter file cannot reset it to 1), and a no-change day
#     still retries the push of an unpushed backlog. The owner is alerted
#     through push.sh ONCE per failure streak when the count reaches
#     BACKUP_PUSH_ALERT_AFTER (default 3) or the oldest unpushed backup commit
#     is >= 24h old, then again at every multiple of 7.
#
# Usage: backup-data.sh [--dry]   (--dry = dump only, no commit/push)
# Exit: 0 = success / no-op / push-failed-but-committed; 1 = corruption or
#       dump/commit error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
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
FAIL_REASON_FILE="$AGENT_DIR/.telegram_bot/backup-push-fail.reason"
ALERT_AFTER="${BACKUP_PUSH_ALERT_AFTER:-3}"
[[ "$ALERT_AFTER" =~ ^[1-9][0-9]*$ ]] || ALERT_AFTER=3
ALERT_AGE_SECS=86400

DRY=0
[[ "${1:-}" == "--dry" ]] && DRY=1

# --- 0) kitless mint = normal no-op (spec 2a change 1) ----------------------
if [[ ! -f "$DB" || ! -x "$LIFE_SH" ]]; then
  exit 0
fi

# --- 1) pre-dump corruption canary (spec 2a change 2, G3) -------------------
# A legacy engine has no `check` verb. Probe its unknown-command exit status
# twice, rather than parsing its (possibly localized) stderr. If `check` has
# that same nonzero status, sqlite3 gets the authoritative read-only check.
CHECK_RC=0
"$LIFE_SH" check >/dev/null 2>&1 || CHECK_RC=$?
CHECK_SUPPORTED=1
if [[ "$CHECK_RC" -ne 0 ]]; then
  UNKNOWN_RC_1=0
  "$LIFE_SH" __backup_data_unknown_check_probe_1__ >/dev/null 2>&1 || UNKNOWN_RC_1=$?
  UNKNOWN_RC_2=0
  "$LIFE_SH" __backup_data_unknown_check_probe_2__ >/dev/null 2>&1 || UNKNOWN_RC_2=$?
  if [[ "$UNKNOWN_RC_1" -ne 0 && "$UNKNOWN_RC_1" -eq "$UNKNOWN_RC_2" \
        && "$CHECK_RC" -eq "$UNKNOWN_RC_1" ]]; then
    CHECK_SUPPORTED=0
  fi
fi

CORRUPT=0
if [[ "$CHECK_SUPPORTED" -eq 1 ]]; then
  [[ "$CHECK_RC" -eq 0 ]] || CORRUPT=1
elif command -v sqlite3 >/dev/null 2>&1; then
  INTEGRITY="$(sqlite3 -readonly "$DB" 'PRAGMA integrity_check' 2>/dev/null || true)"
  [[ "$INTEGRITY" == "ok" ]] || CORRUPT=1
else
  echo "[backup] integrity check unavailable" >&2
fi

if [[ "$CORRUPT" -eq 1 ]]; then
  echo "[backup] $STEM DB corruption detected -- dump refused; last good backup preserved ($DUMP)" >&2
  if [[ -x "$PUSH_SH" ]]; then
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final; a test pins this marker to the
    # string. Previous copy was English, named the dump file path, and handed
    # the owner two shell commands to run -- the agent runs commands, the
    # owner does not. The path and the inspect commands stay on the stderr
    # line above, which the maintainer reads.
    # One word for one thing (owner ruling 2026-09-11): this notice and the
    # remote-push notice below used two different Korean words for the same
    # object. The owner picked 백업; the archival-sounding synonym it used
    # here is retired and must not come back (a test pins its absence).
    "$PUSH_SH" --text "⚠️ 백업에 문제가 생겼어요. 오늘 백업은 건너뛰고 마지막 정상 백업은 그대로 뒀어요. 제가 원인 점검하고 알려드릴게요." --audience owner || true
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
# A nested instance can inherit a parent's repository. Its database must
# never be added to or committed through that parent repository.
REPO_TOP="$(git -C "$AGENT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "$REPO_TOP" != "$AGENT_DIR" ]]; then
  echo "[backup] instance has no own repo -- git leg skipped"
  exit 0
fi

# DGN-1724: unpushed backup commits = "backup: " commits on HEAD that the
# remote-tracking branch does not have. Derived from git, so it survives a
# lost counter file. Prints nothing when there is no tracking ref to compare.
_upstream_ref() {
  local br
  br="$(git -C "$AGENT_DIR" symbolic-ref -q --short HEAD 2>/dev/null)" || return 1
  git -C "$AGENT_DIR" rev-parse -q --verify "refs/remotes/origin/$br" >/dev/null 2>&1 || return 1
  echo "refs/remotes/origin/$br"
}
_unpushed_backup_count() {
  local up
  up="$(_upstream_ref)" || { echo 0; return 0; }
  git -C "$AGENT_DIR" rev-list --count --grep='^backup: ' "$up..HEAD" 2>/dev/null || echo 0
}
_oldest_unpushed_backup_epoch() {
  local up
  up="$(_upstream_ref)" || return 0
  git -C "$AGENT_DIR" log --format=%ct --grep='^backup: ' "$up..HEAD" 2>/dev/null | tail -1
}

HAS_REMOTE=0
git -C "$AGENT_DIR" remote get-url origin >/dev/null 2>&1 && HAS_REMOTE=1

# Pack staging (pack_install --prepare) lives under database/.gen* -- generated,
# never backed up (it carries machine paths that the commit-time sweep rejects).
# Exclude pathspecs make git add fail when these paths are ignored. Add the
# directory normally, then remove staging from the index for older instances
# without ignore rules. --cached preserves files; --ignore-unmatch handles
# absent entries without hiding real errors. Commit exclusions below are safe
# with ignored paths and keep generated content outside the commit pathspec.
git -C "$AGENT_DIR" add -- database/
git -C "$AGENT_DIR" rm -r -q --cached --ignore-unmatch -- database/.gen database/.gen-active
if git -C "$AGENT_DIR" diff --cached --quiet -- database/; then
  BACKLOG=0
  [[ "$HAS_REMOTE" -eq 1 ]] && BACKLOG="$(_unpushed_backup_count)"
  if [[ "$BACKLOG" -eq 0 ]]; then
    echo "[backup] no changes -- skip"
    exit 0
  fi
  # DGN-1724: an unchanged dump used to exit here, so an unpushed backlog was
  # neither retried nor counted on quiet days.
  echo "[backup] no changes -- retrying push of $BACKLOG unpushed backup commit(s)"
else
  STAMP="$(date '+%Y-%m-%d')"
  if ! git -C "$AGENT_DIR" commit -q -m "backup: $STEM dump $STAMP" -- database/ ":(exclude)database/.gen" ":(exclude)database/.gen/**" ":(exclude)database/.gen-active"; then
    echo "[backup] commit failed" >&2
    exit 1
  fi
  echo "[backup] committed ($(git -C "$AGENT_DIR" rev-parse --short HEAD))"
fi

# --- 4) push (no-remote guard + bounded-silence fail counter) ---------------
if [[ "$HAS_REMOTE" -eq 0 ]]; then
  # Local-git-only instance (mint.sh guarantees git init): local commit is
  # the backup; push silently skipped (spec 2a, D2 policy).
  exit 0
fi

PUSH_ERR=""
# DGN-1696 / DGN-1724: mark ONLY this automated owner-data backup push. The
# pre-push hook always runs the secret sweep + D7, and skips its code-test
# lane only when every pushed commit is a database/*.sql dump (DGN-1724) or
# the pushed tree declares a private owner-backup repository (DGN-1696).
if PUSH_ERR="$(export DOGANY_PUSH_PURPOSE=owner-backup; git -C "$AGENT_DIR" push -q origin HEAD 2>&1 >/dev/null)"; then
  rm -f "$FAIL_COUNT_FILE" "$FAIL_REASON_FILE"
  echo "[backup] pushed"
  exit 0
fi

# DGN-1696: redacted cause. Sweep hit lines are rebuilt from their
# structure ("[catN name] file :: line N" -> "catN name file:N"); any other
# detail after " :: " (cat9 carries the commit identity itself) and D7 hit
# bodies (the added source line) are DROPPED, never echoed. Only lines of a
# known gate shape pass; unknown git stderr is summarized separately below.
_push_block_reasons() {
  awk '
    /^PUSH BLOCKED/ { sub(/:[[:space:]]*\/.*$/, ":"); print; next }
    /^STOP:/        { print; next }
    /^[[:space:]]*\[cat[0-9]+ [A-Za-z0-9-]+\]/ {
      line = $0; sub(/^[[:space:]]*\[/, "", line)
      tag = line; sub(/\].*$/, "", tag)
      rest = line; sub(/^[^]]*\][[:space:]]*/, "", rest)
      file = rest; sub(/[[:space:]]+(::|\().*$/, "", file)
      ln = ""
      if (match(rest, /:: line [0-9]+$/)) { ln = substr(rest, RSTART + 8) }
      print "  " tag " " file (ln != "" ? ":" ln : "")
      next
    }
    /^[[:space:]]*D7-OUTBOUND-HIT: / {
      f = $0; sub(/^[[:space:]]*D7-OUTBOUND-HIT: /, "", f); sub(/::.*$/, "", f)
      print "  D7 outbound-primitive " f
      next
    }
  '
}
PUSH_REASONS="$(printf '%s\n' "$PUSH_ERR" | _push_block_reasons)"
PUSH_HITS="$(printf '%s\n' "$PUSH_REASONS" | sed -n 's/^  //p')"

# DGN-1724: which hook stage (or git itself) refused, plus the first failing
# line. Hit lines are the redacted forms above; test-lane lines are suite
# names; git lines have URL credentials masked. Never the matched text.
_mask() { sed -E 's#://[^/@[:space:]]+@#://***@#g'; }
PUSH_STAGE="git"
PUSH_FIRST=""
case "$PUSH_ERR" in
  *"PUSH BLOCKED (D7)"*)             PUSH_STAGE="d7" ;;
  *"PUSH BLOCKED (DGN-1209)"*)       PUSH_STAGE="wiring" ;;
  *"PUSH BLOCKED -- test lane"*)     PUSH_STAGE="test-lane" ;;
  *"PUSH BLOCKED -- secret-sweep"*)  PUSH_STAGE="sweep" ;;
  *"PUSH BLOCKED"*|*"STOP:"*)        PUSH_STAGE="hook" ;;
esac
case "$PUSH_STAGE" in
  sweep|d7) PUSH_FIRST="$(printf '%s\n' "$PUSH_HITS" | sed -n '1p')" ;;
  test-lane) PUSH_FIRST="$(printf '%s\n' "$PUSH_ERR" | grep -m1 -E '^\[run-tests\] (FAIL|DEFERRED-RED|NEWLY RED)' || true)" ;;
  wiring|hook) PUSH_FIRST="$(printf '%s\n' "$PUSH_REASONS" | sed -n '1p')" ;;
  git) PUSH_FIRST="$(printf '%s\n' "$PUSH_ERR" | grep -m1 -E '^(error|fatal|remote|hint|!)|rejected' | _mask || true)" ;;
esac
[[ -n "$PUSH_FIRST" ]] || PUSH_FIRST="$(printf '%s\n' "$PUSH_REASONS" | sed -n '1p')"
[[ -n "$PUSH_FIRST" ]] || PUSH_FIRST="$(printf '%s\n' "$PUSH_ERR" | grep -m1 . | _mask || true)"
PUSH_FIRST="$(printf '%s' "${PUSH_FIRST:-(no output)}" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | cut -c1-200)"

# Push failed: local commit is kept (exit 0 policy) but never infinitely
# silent (spec 2a change 5). Counter lives outside git; DGN-1724 floors it at
# the git-derived unpushed backup count so it cannot silently restart at 1.
mkdir -p "$(dirname "$FAIL_COUNT_FILE")"
prev="$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0)"
[[ "$prev" =~ ^[0-9]+$ ]] || prev=0
count=$((prev + 1))
backlog="$(_unpushed_backup_count || true)"
[[ "$backlog" =~ ^[0-9]+$ ]] || backlog=0
[[ "$backlog" -gt "$count" ]] && count="$backlog"
printf '%s\n' "$count" > "$FAIL_COUNT_FILE.tmp.$$" && mv -f "$FAIL_COUNT_FILE.tmp.$$" "$FAIL_COUNT_FILE"

now="$(date +%s)"
oldest="$(_oldest_unpushed_backup_epoch || true)"
age=0
[[ "$oldest" =~ ^[0-9]+$ ]] && age=$((now - oldest))
alerted="$(sed -n 's/^alerted=//p' "$FAIL_REASON_FILE" 2>/dev/null | head -1 || true)"
[[ "$alerted" =~ ^[0-9]+$ ]] || alerted=0
ALERT=0
ALERT_WHY=""
if [[ "$alerted" -eq 0 && "$count" -ge "$ALERT_AFTER" ]]; then
  ALERT=1; ALERT_WHY="$count consecutive failures"
elif [[ "$alerted" -eq 0 && "$age" -ge "$ALERT_AGE_SECS" ]]; then
  ALERT=1; ALERT_WHY="oldest unpushed backup is $((age / 3600))h old"
elif [[ "$alerted" -gt 0 && "$count" -gt "$alerted" && "$count" -gt "$ALERT_AFTER" && $((count % 7)) -eq 0 ]]; then
  ALERT=1; ALERT_WHY="still failing after $count attempts"
fi
[[ "$ALERT" -eq 1 ]] && alerted="$count"

{
  echo "count=$count"
  echo "stage=$PUSH_STAGE"
  echo "first=$PUSH_FIRST"
  echo "last_fail_at=$(date -u -d "@$now" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -r "$now" '+%Y-%m-%dT%H:%M:%SZ')"
  echo "unpushed_backups=$backlog"
  echo "oldest_unpushed_age_h=$((age / 3600))"
  echo "alerted=$alerted"
} > "$FAIL_REASON_FILE.tmp.$$" && mv -f "$FAIL_REASON_FILE.tmp.$$" "$FAIL_REASON_FILE"

echo "[backup] push failed (local commit kept; consecutive failures: $count)" >&2
echo "[backup]   stage=$PUSH_STAGE first=$PUSH_FIRST" >&2
if [[ -n "$PUSH_REASONS" ]]; then
  printf '%s\n' "$PUSH_REASONS" | sed 's/^/[backup]   /' >&2
elif [[ -n "$PUSH_ERR" ]]; then
  # Not a gate block (network / auth / rejected ref): last git lines, with
  # URL credentials masked.
  printf '%s\n' "$PUSH_ERR" | tail -5 | sed -E 's#://[^/@[:space:]]+@#://***@#g; s/^/[backup]   git: /' >&2
fi
if [[ "$ALERT" -eq 1 ]]; then
  echo "[backup] owner alerted: $ALERT_WHY" >&2
  if [[ -x "$PUSH_SH" ]]; then
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final; a test pins this marker to the
    # string. Previous copy was English and told the owner to "check the
    # backup remote" -- an action the agent owns. The consecutive-failure
    # count is a plain quantity, not an identifier, so it stays.
    # One word for one thing (owner ruling 2026-09-11): 백업, never 기록/보관.
    # DGN-1696: when the pre-push gate blocked, the redacted hit list (category
    # + repo-relative file:line, max 3) rides the alert so the cause is visible
    # without a log dive. Never the matched value itself.
    # DGN-1696 review: the redacted hit list stays in the backup log (agent-facing);
    # the owner alert keeps its approved copy -- category/file:line is internal
    # detail, and new owner copy needs owner confirmation before it ships.
    CAUSE=""
    if [[ -n "$PUSH_HITS" ]]; then
      hit_n="$(printf '%s\n' "$PUSH_HITS" | grep -c .)"
      CAUSE=" 올리기 전 보안 검사가 막은 항목 ${hit_n}건: $(printf '%s\n' "$PUSH_HITS" | head -3 | paste -sd ',' - | sed 's/,/, /g')."
    fi
    "$PUSH_SH" --text "⚠️ 백업을 원격에 올리는 데 ${count}번 연속 실패했어요. 백업 자체는 기기에 안전하게 남아 있어요. 제가 원인 점검하고 알려드릴게요." --audience owner || true
  fi
fi
exit 0
