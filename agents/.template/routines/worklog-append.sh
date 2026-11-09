#!/bin/bash
# worklog-append.sh -- append to a ticket by ID; the slug is never typed (DGN-1443).
#
# Usage:
#   worklog-append.sh DGN-NNNN < text             append stdin to that ticket
#   worklog-append.sh --new --slug <slug> [--why <id>] DGN-NNNN < text
#                                                 create the ticket (explicit only)
#   worklog-append.sh --set-status <status> [--why <id>] [--dry-run] DGN-NNNN < reason-text
#                                                 change the ticket status (see below)
#   worklog-append.sh --resolve DGN-NNNN          print the ticket path, write nothing
#   worklog-append.sh --orphans                   list worklog/*.md with no
#                                                 frontmatter id: (exit 1 if any)
#
# Options:
#   --why <id>        optional Why stored as why_id: on --new or entry into wip
#   --worklog <dir>   worklog directory (default: $WORKLOG_DIR, else the
#                     worklog/ beside this routines/ dir)
#
# --set-status (DGN-2106): the one framework command that changes an agent
# work item's status (devkit refuses direct status writes on DGN-/SKL-/DATE-
# items). Resolves the file by ID exactly like append, then, under a lock and
# with one atomic rename, rewrites frontmatter `status:` and `updated:`
# (today), also `closed_at:` (today) when the new status is done|dismissed and
# the field already exists, and appends one log line
# "- <date> status <old> -> <new>: <reason>" (reason = stdin). Every other
# byte is unchanged. Allowed statuses = VALID_STATUS in
# ticket-status-enum-lint.py (open wip blocked done parked backlog dismissed);
# anything else -> exit 2. No frontmatter fence or no `status:` field ->
# exit 6, nothing written. --dry-run prints the planned change, writes nothing.
# The framework writes records without developer intake, review or stage policy.
# An optional --why value is recorded as metadata, never used as admission.
#
# Quote style of updated:/closed_at: is preserved (quoted stays quoted, same
# quote char; bare stays bare).
#
# Concurrency: plain append takes the lock and `>>` appends in place (no
# temp/rename), so unlocked `>>` writers are never clobbered. --set-status
# must rename, so it builds from a snapshot and re-compares the file to that
# snapshot immediately before the rename, rebuilding if it changed. Residual
# window: an unlocked `>>` landing between that final compare and the rename
# (microseconds) is lost; only locked writers are fully excluded.
#
# Why: `cat >> worklog/DGN-NNNN-<slug>.md` with a slug typed from memory
# never fails -- a wrong slug silently creates a new, frontmatter-less file
# and the real ticket stays untouched. Measured twice in one session
# (2026-09-13). This script is the forcing point: the caller names the ID,
# the machine finds the file.
#
# Resolution: candidates are worklog/<ID>.md and worklog/<ID>-*.md (top
# level, not _-prefixed). The ticket is the candidate whose frontmatter
# `id:` equals <ID> exactly -- companion docs such as <ID>-SPEC-v2.md carry
# their own id and are not the ticket.
#   exactly 1  -> append
#   0          -> refuse (exit 3), list near-misses; --new is the only way
#                 to create
#   2 or more  -> refuse (exit 4), list the candidates
#
# Exit: 0 ok, 1 orphans found (--orphans), 2 usage / unknown status, 3 no
#       ticket, 4 ambiguous, 5 --new but the ticket already exists,
#       6 --set-status but no frontmatter / no status field.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WL="${WORKLOG_DIR:-$SCRIPT_DIR/../worklog}"

usage() {
  sed -n "2,15p" "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# frontmatter_id <file> -- the value of `id:` inside a leading --- fence,
# empty if there is no fence or no id.
frontmatter_id() {
  awk '
    NR == 1 { if ($0 != "---") exit; next }
    $0 == "---" { exit }
    /^id:[ \t]*/ { sub(/^id:[ \t]*/, ""); sub(/[ \t]+$/, ""); gsub(/^["'\'']|["'\'']$/, ""); print; exit }
  ' "$1"
}

# frontmatter_value <key> <file> -- value from the first, leading block only.
frontmatter_value() {
  awk -v key="$1" '
    NR == 1 { if ($0 != "---") exit; next }
    $0 == "---" { exit }
    index($0, key ":") == 1 {
      v = substr($0, length(key) + 2)
      sub(/^[ \t]*/, "", v); sub(/[ \t]*$/, "", v)
      gsub(/^['\''\"]|['\''\"]$/, "", v); print v; exit
    }
  ' "$2"
}

MODE=append
SLUG=""
WHY_ID=""
ID=""
NEW_STATUS=""
DRY=0
VALID_STATUS="open wip blocked done parked backlog dismissed"
while [ $# -gt 0 ]; do
  case "$1" in
    --new) MODE=new ;;
    --why) [ $# -ge 2 ] || usage; WHY_ID="$2"; shift ;;
    --slug) [ $# -ge 2 ] || usage; SLUG="$2"; shift ;;
    --set-status) [ $# -ge 2 ] || usage; MODE=setstatus; NEW_STATUS="$2"; shift ;;
    --dry-run) DRY=1 ;;
    --resolve) MODE=resolve ;;
    --orphans) MODE=orphans ;;
    --worklog) [ $# -ge 2 ] || usage; WL="$2"; shift ;;
    -h|--help) usage ;;
    -*) echo "worklog-append: unknown option $1" >&2; usage ;;
    *) [ -z "$ID" ] || usage; ID="$1" ;;
  esac
  shift
done

if [ ! -d "$WL" ]; then
  echo "worklog-append: no worklog directory: $WL" >&2
  exit 2
fi

if [ "$MODE" = orphans ]; then
  n=0
  for f in "$WL"/*.md; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in _*) continue ;; esac
    if [ -z "$(frontmatter_id "$f")" ]; then
      echo "orphan: $f (no frontmatter id:)"
      n=$((n+1))
    fi
  done
  [ "$n" -eq 0 ] && exit 0
  echo "worklog-append: $n worklog file(s) without a frontmatter id:" >&2
  exit 1
fi

[ -n "$ID" ] || usage
if [ "$MODE" = setstatus ]; then
  case " $VALID_STATUS " in
    *" $NEW_STATUS "*) ;;
    *) echo "worklog-append: unknown status '$NEW_STATUS' (allowed: $VALID_STATUS)" >&2; exit 2 ;;
  esac
fi
if ! printf '%s' "$ID" | grep -qE '^[A-Za-z][A-Za-z0-9]*-[0-9]+$'; then
  echo "worklog-append: '$ID' is not a ticket ID (give the ID, not a file name)" >&2
  exit 2
fi

matches=()
others=()
for f in "$WL/$ID.md" "$WL/$ID"-*.md; do
  [ -f "$f" ] || continue
  if [ "$(frontmatter_id "$f")" = "$ID" ]; then
    matches+=("$f")
  else
    others+=("$f")
  fi
done

if [ "$MODE" = new ]; then
  if [ ${#matches[@]} -gt 0 ]; then
    echo "worklog-append: $ID already exists: ${matches[0]} (drop --new to append)" >&2
    exit 5
  fi
  if ! printf '%s' "$SLUG" | grep -qE '^[a-z0-9][a-z0-9-]*$'; then
    echo "worklog-append: --new needs --slug <lowercase-hyphen-slug>" >&2
    exit 2
  fi
  target="$WL/$ID-$SLUG.md"
  if [ -e "$target" ]; then
    echo "worklog-append: $target exists but is not ticket $ID (frontmatter id differs)" >&2
    exit 5
  fi
  if [ -n "$WHY_ID" ] && ! printf '%s' "$WHY_ID" | grep -qE '^why-[0-9]+$'; then
    echo "worklog-append: --why needs a Why id (why-NNNN)" >&2
    exit 2
  fi
  input="$(cat)"
  # Split a leading stdin frontmatter block from its body.  The fields are
  # folded into the generated block below so readers never encounter a
  # second, ignored block.
  input_fm="$(printf '%s\n' "$input" | awk '
    NR == 1 { if ($0 != "---") exit; inside=1; next }
    inside && $0 == "---" { exit }
    inside { print }
  ')"
  if [ "$(printf '%s\n' "$input" | sed -n '1p')" = "---" ]; then
    body="$(printf '%s\n' "$input" | awk '
      NR == 1 { inside=1; next }
      inside && $0 == "---" { inside=0; next }
      !inside { print }
    ')"
  else
    body="$input"
  fi

  stdin_why="$(printf '%s\n' "$input_fm" | awk '
    /^why_id:[ \t]*/ { sub(/^why_id:[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/^['\''\"]|['\''\"]$/, ""); print; exit }
  ')"
  kind="$(printf '%s\n' "$input_fm" | awk '
    /^kind:[ \t]*/ { sub(/^kind:[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/^['\''\"]|['\''\"]$/, ""); print; exit }
  ')"
  restores="$(printf '%s\n' "$input_fm" | awk '
    /^restores:[ \t]*/ { sub(/^restores:[ \t]*/, ""); sub(/[ \t]*$/, ""); gsub(/^['\''\"]|['\''\"]$/, ""); print; exit }
  ')"

  effective_why="$WHY_ID"
  inherited_from=""
  [ -n "$effective_why" ] || effective_why="$stdin_why"
  if [ -z "$effective_why" ] && [ "$kind" = fix ] && printf '%s' "$restores" | grep -qE '^[A-Za-z][A-Za-z0-9]*-[0-9]+$'; then
    for restored in "$WL/$restores.md" "$WL/$restores"-*.md; do
      [ -f "$restored" ] || continue
      [ "$(frontmatter_id "$restored")" = "$restores" ] || continue
      inherited="$(frontmatter_value why_id "$restored")"
      if printf '%s' "$inherited" | grep -qE '^why-[0-9]+$'; then
        effective_why="$inherited"
        inherited_from="$restores"
        break
      fi
    done
  fi
  [ -n "$effective_why" ] || effective_why=none
  if [ "$effective_why" = none ] && [ "$kind" != fix ]; then
    echo "worklog-append: no Why recorded; use --why why-NNNN when its parent Why is known" >&2
  fi
  today="$(date +%Y-%m-%d)"
  {
    printf -- '---\nid: %s\nstatus: open\ncreated: %s\nupdated: %s\n' "$ID" "$today" "$today"
    printf 'why_id: %s\n' "$effective_why"
    [ -z "$inherited_from" ] || printf 'why_inherited_from: %s\n' "$inherited_from"
    # Generated identity/lifecycle fields and the resolved Why fields win;
    # every other caller-supplied field stays in its original order.
    printf '%s\n' "$input_fm" | awk '
      /^(id|status|created|updated|why_id|why_inherited_from):[ \t]*/ { next }
      NF { print }
    '
    printf -- '---\n'
    [ -n "$body" ] && printf '\n%s\n' "$body"
  } > "$target"
  echo "$target"
  exit 0
fi

if [ ${#matches[@]} -eq 0 ]; then
  echo "worklog-append: no ticket with id $ID in $WL -- refusing to create one" >&2
  for f in ${others[@]+"${others[@]}"}; do
    echo "  near-miss: $f (frontmatter id: '$(frontmatter_id "$f")')" >&2
  done
  echo "  (a new ticket needs --new --slug <slug>)" >&2
  exit 3
fi
if [ ${#matches[@]} -gt 1 ]; then
  echo "worklog-append: ${#matches[@]} files claim id $ID -- refusing to pick one:" >&2
  for f in "${matches[@]}"; do echo "  candidate: $f" >&2; done
  exit 4
fi

target="${matches[0]}"
if [ "$MODE" = resolve ]; then
  echo "$target"
  exit 0
fi

body="$(cat)"
if [ -z "$body" ]; then
  echo "worklog-append: empty input, nothing appended to $target" >&2
  exit 2
fi

# Lock per ticket (mkdir is atomic and portable); released on exit.
LOCK="$target.lock"
tries=0
until mkdir "$LOCK" 2>/dev/null; do
  tries=$((tries+1))
  if [ "$tries" -gt 600 ]; then
    echo "worklog-append: could not lock $target (stale $LOCK?)" >&2
    exit 7
  fi
  sleep 0.05
done
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

today="$(date +%Y-%m-%d)"

# Keep the append on its own line even if the file lacks a trailing newline.
ensure_newline() {
  if [ -s "$1" ] && [ "$(tail -c1 "$1" | od -An -c | tr -d ' ')" != '\n' ]; then
    printf '\n' >> "$1"
  fi
}

if [ "$MODE" != setstatus ]; then
  # Plain append: in place under the lock, no temp/rename, so an unlocked
  # `>>` from another writer is never overwritten (DGN-2106 review).
  if [ "$DRY" = 1 ]; then
    echo "worklog-append: dry-run, nothing written: $target"
    exit 0
  fi
  ensure_newline "$target"
  printf '%s\n' "$body" >> "$target"
  echo "$target"
  exit 0
fi

# --set-status: build from a snapshot, then re-verify the file is unchanged
# right before the rename; if an unlocked writer touched it, rebuild.
attempt=0
while :; do
  attempt=$((attempt+1))
  SNAP="$(mktemp "$target.snap.XXXXXX")"
  TMP2="$(mktemp "$target.tmp.XXXXXX")"
  TMP="$TMP2"
  trap 'rmdir "$LOCK" 2>/dev/null; rm -f "$SNAP" "$TMP2"' EXIT
  cp -p "$target" "$SNAP"
  cp -p "$target" "$TMP2"   # keep mode; content overwritten below
  old="$(awk '
    NR == 1 { if ($0 != "---") exit; next }
    $0 == "---" { exit }
    /^status:/ { sub(/^status:[ \t]*/, ""); sub(/[ \t]+$/, ""); print; exit }
  ' "$SNAP")"
  # Optional Why metadata is carried on entry into wip. No stage policy is
  # evaluated by this general record writer.
  starting=0
  normalized_old="$(printf '%s' "$old" | tr -d "\"'")"
  [ "$NEW_STATUS" != wip ] || [ "$normalized_old" = wip ] || starting=1
  start_why=""
  if [ "$starting" = 1 ]; then
    start_why="$WHY_ID"
    if [ -n "$start_why" ] && ! printf '%s' "$start_why" | grep -qE '^why-[0-9]+$'; then
      echo "worklog-append: --why needs a Why id (why-NNNN)" >&2
      exit 2
    fi
  fi
  closing=0
  case "$NEW_STATUS" in done|dismissed) closing=1 ;; esac
  # setval: replace the value of `key:` with today, keeping the existing
  # quote style (quoted stays quoted with the same quote char; bare stays bare).
  awk -v st="$NEW_STATUS" -v today="$today" -v closing="$closing" -v why="$start_why" '
    function setval(key,   rest, q) {
      rest = $0; sub(/^[A-Za-z_]+:[ \t]*/, "", rest)
      q = substr(rest, 1, 1)
      if (q == "\"" || q == "\047") return key ": " q today q
      return key ": " today
    }
    NR == 1 { if ($0 != "---") { exit 6 } infm = 1; print; next }
    infm && $0 == "---" {
      infm = 0
      if (!seen_status) exit 6
      if (!seen_upd) print "updated: " today
      if (why != "" && !seen_why) print "why_id: " why
      print; next
    }
    infm && /^why_id:/ && why != "" { seen_why = 1; print "why_id: " why; next }
    infm && /^status:/ && !seen_status { seen_status = 1; print "status: " st; next }
    infm && /^updated:/ && !seen_upd { seen_upd = 1; print setval("updated"); next }
    infm && closing == 1 && /^closed_at:/ && !seen_cl { seen_cl = 1; print setval("closed_at"); next }
    { print }
    END { if (infm || !seen_status) exit 6 }
  ' "$SNAP" > "$TMP2"
  if [ $? -ne 0 ]; then
    echo "worklog-append: $target has no frontmatter status: field -- nothing written" >&2
    exit 6
  fi
  ensure_newline "$TMP2"
  printf '%s\n' "- $today status ${old:-?} -> $NEW_STATUS: $body" >> "$TMP2"

  if [ "$DRY" = 1 ]; then
    echo "worklog-append: dry-run, nothing written: $target"
    diff -u "$target" "$TMP2" | sed 1,2d
    exit 0
  fi
  if cmp -s "$target" "$SNAP"; then
    mv "$TMP2" "$target" && TMP2=""
    break
  fi
  rm -f "$SNAP" "$TMP2"
  [ "$attempt" -lt 20 ] || { echo "worklog-append: $target kept changing under us" >&2; exit 7; }
done
echo "$target"
