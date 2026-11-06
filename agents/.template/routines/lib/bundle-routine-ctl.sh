#!/bin/bash
# bundle-routine-ctl.sh -- switch ONE life-kit bundle routine on/off, with its
# brief-slot swap, in one call (DGN-1826). The machine half of the
# dogany-lifekit-setup skill's "routine on / routine off" (also used by its
# deactivate and reconcile procedures), so the skill does not hand-split the
# schedule column or remember the slot map.
#
# Usage (run from anywhere; agent root = two dirs up, or --root):
#   bundle-routine-ctl.sh on  <id> ["<[dow ]HH:MM>"] [--root <dir>]
#   bundle-routine-ctl.sh off <id>                   [--root <dir>]
#   bundle-routine-ctl.sh slot-sync <id> [--root <dir>]
#   bundle-routine-ctl.sh slot <id>       print the brief slot <id> owns ('' = none)
#
# on:  schedule = the optional argument, else the bundle.conf schedule column
#      of <id> (bundle.conf resolved by life_engine.py, never a hardcoded stem).
#      `[dow ]HH:MM` is split: 'HH:MM' -> daily, '<dow> HH:MM' -> weekly on dow
#      (dow case-insensitive, sun..sat), passed to routine-ctl.sh as its
#      [weekday] argument. Then BRIEF-SLOT SWAP (C2/P22): a slot-owning routine
#      unloads its same-slot generic brief (brief-slot-ctl.sh disable <slot>).
# off: routine-ctl.sh disable <id>, then the slot's generic brief is restored
#      (brief-slot-ctl.sh enable <slot>) so the briefing never goes dark.
# SLOT OWNERSHIP IS KIT KNOWLEDGE: the framework never names a kit routine.
#      The kit's bundle.conf declares it in an optional 6th column `slot`
#      (id, kind, schedule, conf-key, module, slot):
#        morning | retro | weekly   this routine owns that generic-brief slot
#        -                          no slot (no swap)
#      Rules (checked over the WHOLE file before anything changes; a violation
#      -> exit 1, nothing scheduled, nothing swapped):
#        - the column is all-or-nothing: some data rows with it and some
#          without = mixed -> rejected;
#        - value exactly one of the four above (CR tolerated);
#        - a slot value other than '-' only on a kind=routine row;
#        - a slot owned by at most one row.
#      Column absent from the whole file -> legacy fallback (compat block
#      below). An <id> with no bundle.conf row owns no slot.
# The swap is launchd-load-layer only (brief-slot-ctl never deletes a plist).
# slot-sync: the routine stays as it is (its clock is never re-rendered, so a
#      time the owner changed survives); only its slot is re-aligned to the
#      routine's ACTUAL state (routine-ctl.sh status): scheduled -> generic
#      brief disabled, not scheduled -> restored. Reconcile's repair for a
#      double utterance (both loaded) or a briefing gap (neither loaded).
#      Slotless routine -> no-op.
# on/off are idempotent: reconcile applies a changed item with on/off and runs
# slot-sync for a slot-owning routine whose state already matches.
#
# Output rows (TAB-separated, stdout):
#   ON    <id> <HH:MM> <weekday|daily>
#   OFF   <id>
#   STATE <id> <on|off>               (slot-sync: the routine's actual state)
#   SLOT  <disable|enable> <slot>
#   WARN  brief-slot-ctl <detail>     (swap failed; routine state already set)
# Exit: 0 ok (WARN rows are non-fatal) | 1 usage / unknown id / bad schedule /
#       no bundle.conf / bad slot column |
#       4 routine-ctl.sh failed (its stderr passed through; nothing swapped)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
CMD="${ARGS[0]:-}"; ID="${ARGS[1]:-}"; SCHED="${ARGS[2]:-}"
[ -n "$ROOT" ] || ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RCTL="${BUNDLE_ROUTINE_RCTL:-$ROOT/routines/lib/routine-ctl.sh}"
SLOTCTL="${BUNDLE_ROUTINE_SLOTCTL:-$ROOT/routines/lib/brief-slot-ctl.sh}"

usage() {
  echo "usage: bundle-routine-ctl.sh on <id> [\"[dow ]HH:MM\"] | off <id> | slot-sync <id> | slot <id> [--root <dir>]" >&2
  exit 1
}
[ -n "$CMD" ] && [ -n "$ID" ] || usage
case "$ID" in (*[!a-z0-9-]*) echo "[bundle-routine-ctl] bad id (kebab-case only): $ID" >&2; exit 1 ;; esac

swap() { # swap <disable|enable> <slot>
  local out
  if out="$(bash "$SLOTCTL" "$1" "$2" 2>&1)"; then
    printf 'SLOT\t%s\t%s\n' "$1" "$2"
  else
    printf 'WARN\tbrief-slot-ctl\t%s %s: %s\n' "$1" "$2" "$(printf '%s' "$out" | tail -1)"
  fi
}

rctl_failed() { # routine-ctl failed: its stderr already shown; nothing swapped
  echo "[bundle-routine-ctl] routine-ctl.sh $CMD $ID failed (rc=$1); no brief-slot swap" >&2
  exit 4
}

bundle_path() { # the kit's bundle.conf (same resolver as the lifekit-setup skill)
  local bundle
  bundle="$(python3 "$ROOT/routines/lib/life_engine.py" "$ROOT" 2>/dev/null | sed -n 's/^bundle=//p' | head -1)"
  [ -n "$bundle" ] && [ -f "$bundle" ] || { echo "[bundle-routine-ctl] no bundle.conf (kit not installed)" >&2; return 1; }
  printf '%s\n' "$bundle"
}

bundle_schedule() { # schedule column of routine row <id>; rc 1 when no such row
  local bundle
  bundle="$(bundle_path)" || return 1
  awk -F'\t' -v id="$1" '!/^[[:space:]]*[#@]/ && $1 == id && $2 == "routine" { print $3; found=1; exit }
    END { exit found ? 0 : 1 }' "$bundle" \
    || { echo "[bundle-routine-ctl] no routine row '$1' in $bundle" >&2; return 1; }
}

slot_of() { # brief slot <id> owns ('' = none); rc 1 = no bundle.conf / bad slot column
  local bundle out
  bundle="$(bundle_path)" || return 1
  # prints "column <slot>" or "legacy -"; violations on stderr, rc 1
  out="$(awk -F'\t' -v id="$1" '
    { sub(/\r$/, "") }
    /^[[:space:]]*[#@]/ || /^[[:space:]]*$/ { next }
    {
      if (NF >= 6) {
        with++; v = $6
        if (v !~ /^(morning|retro|weekly|-)$/) {
          err = err sprintf("  %s: bad slot \047%s\047 (morning|retro|weekly|-)\n", $1, v)
        } else if (v != "-") {
          if ($2 != "routine") err = err sprintf("  %s: slot \047%s\047 on a kind=%s row (routines only)\n", $1, v, $2)
          else if (v in owner) err = err sprintf("  %s: slot \047%s\047 already owned by %s\n", $1, v, owner[v])
          else owner[v] = $1
        }
        if ($1 == id) s = v
      } else { without++; if (!first5) first5 = $1 }
    }
    END {
      if (with && without) err = err sprintf("  mixed: %d row(s) with the slot column, %d without (first: %s)\n", with, without, first5)
      if (err != "") { printf "%s", err > "/dev/stderr"; exit 1 }
      if (!with) { print "legacy -"; exit 0 }
      print "column " (s == "" ? "-" : s)
    }' "$bundle")" || { echo "[bundle-routine-ctl] bad slot column in $bundle; nothing changed" >&2; return 1; }
  case "$out" in
    "column -") echo "" ;;
    column\ *) echo "${out#column }" ;;
    *)
      # --- COMPAT (DGN-1826): legacy kit without the slot column ----------
      # Kits up to kit_life 2.37.2 ship a 5-column bundle.conf. Keep exactly
      # the slot map the lifekit-setup skill shipped before this helper (two
      # slots, NO weekly: a weekly swap exists only when the kit declares it).
      # REMOVE this block when every supported kit's pack-manifest
      # requires_framework floor is >= 2.6.0 (the framework version that reads
      # the slot column); then a 5-column file simply owns no slot.
      case "$1" in
        morning-brief) echo morning ;;
        daily-retro)   echo retro ;;
        *) echo "" ;;
      esac
      # --- end COMPAT -----------------------------------------------------
      ;;
  esac
}

case "$CMD" in
  slot)
    slot_of "$ID" || exit 1; exit 0 ;;
  on)
    S="$(slot_of "$ID")" || exit 1
    if [ -z "$SCHED" ]; then SCHED="$(bundle_schedule "$ID")" || exit 1; fi
    # split "[dow ]HH:MM" (extra whitespace tolerated)
    read -r f1 f2 rest <<<"$SCHED"
    if [ -n "${rest:-}" ]; then echo "[bundle-routine-ctl] bad schedule: '$SCHED'" >&2; exit 1; fi
    if [ -n "${f2:-}" ]; then
      WDAY="$(printf '%s' "$f1" | tr 'A-Z' 'a-z')"; HHMM="$f2"
      case "$WDAY" in mon|tue|wed|thu|fri|sat|sun) ;; *)
        echo "[bundle-routine-ctl] bad weekday in schedule: '$SCHED' (sun..sat)" >&2; exit 1 ;;
      esac
    else
      WDAY=""; HHMM="${f1:-}"
    fi
    [[ "$HHMM" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] \
      || { echo "[bundle-routine-ctl] bad time in schedule: '$SCHED' (HH:MM)" >&2; exit 1; }
    if [ -n "$WDAY" ]; then
      bash "$RCTL" enable "$ID" "routines/bundle/$ID.sh" "$HHMM" "$WDAY" >&2 || rctl_failed $?
    else
      bash "$RCTL" enable "$ID" "routines/bundle/$ID.sh" "$HHMM" >&2 || rctl_failed $?
    fi
    printf 'ON\t%s\t%s\t%s\n' "$ID" "$HHMM" "${WDAY:-daily}"
    [ -z "$S" ] || swap disable "$S"
    exit 0 ;;
  off)
    S="$(slot_of "$ID")" || exit 1
    bash "$RCTL" disable "$ID" >&2 || rctl_failed $?
    printf 'OFF\t%s\n' "$ID"
    [ -z "$S" ] || swap enable "$S"
    exit 0 ;;
  slot-sync)
    s="$(slot_of "$ID")" || exit 1; [ -n "$s" ] || exit 0
    bash "$RCTL" status "$ID" >/dev/null 2>&1; rc=$?
    case "$rc" in
      0) printf 'STATE\t%s\ton\n' "$ID"; swap disable "$s" ;;
      3) printf 'STATE\t%s\toff\n' "$ID"; swap enable "$s" ;;
      *) rctl_failed "$rc" ;;
    esac
    exit 0 ;;
  *) usage ;;
esac
