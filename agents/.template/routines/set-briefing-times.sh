#!/bin/bash
# set-briefing-times.sh -- DGN-422 briefing-time onboarding step (folds the
# DGN-420 seam). Writes BRIEF_TIME_MORNING / BRIEF_TIME_RETRO / BRIEF_TIME_WEEKLY
# into config/agent.conf, then regenerates the generic-brief launchd plist
# StartCalendarInterval (Hour/Minute; weekly also Weekday) from the same keys --
# so the wording clock (generic-brief.sh) and the fire clock (launchd) never
# drift. Deterministic, no model. The onboarding flow invokes this AFTER the
# user picks (or skips to) briefing times.
#
# Usage:
#   set-briefing-times.sh [--root <dir>] \
#     [--morning HH:MM] [--retro HH:MM] [--weekly "<Day> HH:MM"]
# Any slot omitted keeps its existing config value, else the default:
#   morning 07:00, retro 22:00, weekly "Sun 20:00" (form: <Day> HH:MM).
# Day accepts Sun..Sat (3-letter, case-insensitive) -> launchd Weekday 0..6.
set -euo pipefail

ROOT=""
IN_MORNING=""; IN_RETRO=""; IN_WEEKLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    --morning) IN_MORNING="$2"; shift 2 ;;
    --retro)   IN_RETRO="$2"; shift 2 ;;
    --weekly)  IN_WEEKLY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ROOT" ] || ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/config/agent.conf"
RTDIR="$ROOT/routines"
[ -f "$CONF" ] || { echo "ERROR: agent.conf not found at $CONF" >&2; exit 1; }

# --- config helpers (same read/upsert semantics as generic-brief.sh) ---------
conf_get() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- || true; }
conf_upsert() { # conf_upsert <key> <value>
  local key="$1" val="$2" tmp
  if grep -qE "^$key=" "$CONF" 2>/dev/null; then
    # grep -v exits 1 when it removes ALL lines (single-key file) -> `|| true`.
    tmp="$(mktemp)"; grep -vE "^$key=" "$CONF" > "$tmp" || true; mv "$tmp" "$CONF"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$CONF"
}

valid_hhmm() { [[ "$1" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; }

# Resolve each slot: explicit input -> existing config -> default.
MORNING="${IN_MORNING:-$(conf_get BRIEF_TIME_MORNING)}"; MORNING="${MORNING:-07:00}"
RETRO="${IN_RETRO:-$(conf_get BRIEF_TIME_RETRO)}";       RETRO="${RETRO:-22:00}"
WEEKLY="${IN_WEEKLY:-$(conf_get BRIEF_TIME_WEEKLY)}";    WEEKLY="${WEEKLY:-Sun 20:00}"

valid_hhmm "$MORNING" || { echo "ERROR: bad --morning '$MORNING' (want HH:MM)" >&2; exit 1; }
valid_hhmm "$RETRO"   || { echo "ERROR: bad --retro '$RETRO' (want HH:MM)" >&2; exit 1; }

# weekly = "<Day> HH:MM"
W_DAY="${WEEKLY%% *}"; W_TIME="${WEEKLY##* }"
valid_hhmm "$W_TIME" || { echo "ERROR: bad --weekly time in '$WEEKLY' (want '<Day> HH:MM')" >&2; exit 1; }
case "$(printf '%s' "$W_DAY" | tr 'A-Z' 'a-z')" in
  sun) W_WD=0 ;; mon) W_WD=1 ;; tue) W_WD=2 ;; wed) W_WD=3 ;;
  thu) W_WD=4 ;; fri) W_WD=5 ;; sat) W_WD=6 ;;
  *) echo "ERROR: bad --weekly day '$W_DAY' (want Sun..Sat)" >&2; exit 1 ;;
esac

# --- write config (source of truth) ------------------------------------------
conf_upsert BRIEF_TIME_MORNING "$MORNING"
conf_upsert BRIEF_TIME_RETRO   "$RETRO"
conf_upsert BRIEF_TIME_WEEKLY  "$W_DAY $W_TIME"

# --- regenerate the launchd plist StartCalendarInterval from the config -------
# Reuse the same plutil-first / perl-fallback approach install.sh uses so this
# works headless (no new mechanism -- climb the ladder). The plist is rewritten
# in place; a running launchd unit is reloaded only by the caller (onboarding
# does not restart units), consistent with the config-is-truth model.
set_hm() { # set_hm <plist> <hh> <mm>
  local f="$1" hh="$2" mm="$3"
  hh=$((10#$hh)); mm=$((10#$mm))
  if command -v plutil >/dev/null 2>&1 \
     && plutil -replace StartCalendarInterval.Hour -integer "$hh" "$f" 2>/dev/null \
     && plutil -replace StartCalendarInterval.Minute -integer "$mm" "$f" 2>/dev/null; then
    return 0
  fi
  perl -0pi -e "s#(<key>Hour</key>\s*<integer>)\d+#\${1}$hh#; s#(<key>Minute</key>\s*<integer>)\d+#\${1}$mm#" "$f" 2>/dev/null
}
set_weekday() { # set_weekday <plist> <wd>
  local f="$1" wd="$2"
  wd=$((10#$wd))
  if command -v plutil >/dev/null 2>&1 \
     && plutil -replace StartCalendarInterval.Weekday -integer "$wd" "$f" 2>/dev/null; then
    return 0
  fi
  perl -0pi -e "s#(<key>Weekday</key>\s*<integer>)\d+#\${1}$wd#" "$f" 2>/dev/null
}

regen_slot() { # regen_slot <label> <hh> <mm> [wd]
  local label="$1" hh="$2" mm="$3" wd="${4:-}" f found=0
  for f in "$RTDIR"/*"$label".plist; do
    [ -f "$f" ] || continue
    found=1
    set_hm "$f" "$hh" "$mm" || true
    [ -n "$wd" ] && { set_weekday "$f" "$wd" || true; }
  done
  [ "$found" = "1" ] || echo "  [briefing] $label: no plist found -- skipped" >&2
}

M_HH="${MORNING%%:*}"; M_MM="${MORNING##*:}"
R_HH="${RETRO%%:*}";   R_MM="${RETRO##*:}"
W_HH="${W_TIME%%:*}";  W_MM="${W_TIME##*:}"
regen_slot generic-brief-morning "$M_HH" "$M_MM"
regen_slot generic-brief-retro   "$R_HH" "$R_MM"
regen_slot generic-brief-weekly  "$W_HH" "$W_MM" "$W_WD"

echo "briefing times set: morning=$MORNING retro=$RETRO weekly=$W_DAY $W_TIME (config + plist regenerated)"
