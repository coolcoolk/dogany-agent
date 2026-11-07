#!/bin/bash
# set-briefing-times.sh -- briefing-time setter (DGN-422, folds the DGN-420
# seam; slot resolution harvested from the live-instance fix, DGN-1806).
#
# Writes BRIEF_TIME_MORNING / BRIEF_TIME_RETRO / BRIEF_TIME_WEEKLY into
# config/agent.conf, then moves the schedule of the unit that ACTUALLY OCCUPIES
# each slot on THIS instance (launchd StartCalendarInterval on macOS, systemd
# --user timer OnCalendar on Linux, DGN-1810) -- so the wording clock
# (generic-brief.sh reads the same keys) and the fire clock never drift.
#
# INSTANCE MODEL: the routine engine is one scheduling machine; each briefing
# is an instance of it, sent by the agent that owns it, in its own voice. This
# script sets the CLOCK of this instance's units only; it never merges,
# relays, or re-hosts a briefing on another agent.
#
# WHY SLOT RESOLUTION (DGN-1806): the previous version rewrote, unconditionally,
# routines/*generic-brief-<slot>.plist. Those units are DEFERRED
# (routines/plists.defer): not installed in ~/Library/LaunchAgents, not loaded.
# On a live instance the slots are occupied by the instance's own units
# (section-*, or morning-brief / daily-retro / weekly-review). So the setter
# updated a dormant file, reported success, and the live clock never moved.
#
# Now, per slot: resolve the candidate units, keep the ones launchd has LOADED,
# rewrite THOSE installed plists, and reload them. A unit that is not already
# loaded is NEVER loaded here (deferred stays deferred). The staged,
# never-loaded routines/*generic-brief-<slot>.plist is still kept time-synced
# on disk, so a later brief-slot-ctl enable inherits the right clock.
#
# Slot -> candidate units: BRIEF_UNITS_<SLOT> in config/agent.conf overrides;
# otherwise default_units() below. Values are label SUFFIXES (the part after
# com.telegram-skill-bot.<agent>.), space-separated, priority order.
#
# FIRST-RUN SAFETY (adopt, do not impose): a slot with no explicit argument and
# no BRIEF_TIME_* key ADOPTS the time of its live unit.
#
# NO FRAMEWORK DEFAULT TIME (DGN-1806 r3): the framework owns no briefing time.
# Default times belong to the kit (bundle.conf), which passes them explicitly.
# Per slot: explicit arg -> existing BRIEF_TIME_* -> loaded live unit's time;
# if none resolves, the slot is left UNWRITTEN (no config key, no plist
# rewrite), named on stderr, and the script exits 4. A guessed time is never
# written: a guess on an instance with no loaded unit would put a wording
# clock in config that disagrees with the unit that actually fires.
#
# MULTI-FIRE SLOTS: a StartCalendarInterval ARRAY is a primary + backstop pair
# (e.g. 20:30 primary, 20:45 backstop). The requested time lands on entry[0];
# every later entry keeps its ORIGINAL offset in minutes from entry[0].
#
# HONESTY: a slot is reported as moved only when its loaded unit was rewritten
# AND reloaded. Slots with no loaded unit are named on stderr and the script
# exits 4 (config written, nothing fires there). Unresolved slots (no time
# from any source) also exit 4 (nothing written for them). --no-reload slots are named as
# pending (file rewritten, live clock unchanged until the unit is reloaded).
#
# SCHEDULER BACKEND (DGN-1810): launchd when launchctl is on PATH, else systemd
# --user when systemctl is; BRIEF_SCHED_BACKEND=launchd|systemd forces one.
# The pre-fix setter was launchd-only: on Linux every slot exited 4 and no
# timer moved. Both backends share the adopt / backstop / dry-run / honesty
# semantics above.
#
# launchd half: plutil does both read and write (it tolerates XML that strict
# parsers reject, and canonicalises on write, which drops XML comments).
# Without plutil, config is still written and every plist is reported as
# skipped.
#
# systemd half: per label suffix the candidates are dogany-<agent>-<suffix>.timer
# (routine-ctl.sh naming) then com.telegram-skill-bot.<agent>.<suffix>.timer
# (template-shipped units). LOADED = `systemctl --user is-active`. The timer
# file (FragmentPath, else the unit dir) has its [Timer] OnCalendar= lines
# rewritten in place: line 1 is the primary, later lines are backstops that
# keep their offset (the plist array rule); a day-of-week prefix is rewritten
# only on lines that carry one. Move = daemon-reload + restart of the timer.
#
# Usage:
#   set-briefing-times.sh [--root <dir>] [--no-reload] [--dry-run] \
#     [--morning HH:MM] [--retro HH:MM] [--weekly "<Day> HH:MM"]
#   --no-reload  rewrite plists but do not bootout/bootstrap them.
#   --dry-run    resolve + print what would change; write nothing.
# Day accepts Sun..Sat (3-letter, case-insensitive) -> launchd Weekday 0..6.
# A slot with no argument keeps its BRIEF_TIME_* key, else adopts its loaded
# unit's time, else is left unwritten. There is no built-in default time.
# Exit: 0 ok | 1 bad input / missing config | 2 bad arg | 4 slot(s) with no
# loaded unit (config written; nothing fires there) and/or slot(s) with no
# resolvable time (left unwritten).
#
# Seams (tests): ROUTINE_CTL_LA_DIR overrides ~/Library/LaunchAgents;
# ROUTINE_CTL_UNIT_DIR overrides ~/.config/systemd/user; BRIEF_SCHED_BACKEND
# forces the backend; launchctl / systemctl / plutil are resolved from PATH.
set -euo pipefail

ROOT=""
IN_MORNING=""; IN_RETRO=""; IN_WEEKLY=""
RELOAD=1; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root)    ROOT="$2"; shift 2 ;;
    --morning) IN_MORNING="$2"; shift 2 ;;
    --retro)   IN_RETRO="$2"; shift 2 ;;
    --weekly)  IN_WEEKLY="$2"; shift 2 ;;
    --no-reload) RELOAD=0; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ROOT" ] || ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/config/agent.conf"
RTDIR="$ROOT/routines"
LA_DIR="${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}"
UNIT_DIR="${ROUTINE_CTL_UNIT_DIR:-$HOME/.config/systemd/user}"
UID_N="$(id -u)"
[ -f "$CONF" ] || { echo "ERROR: agent.conf not found at $CONF" >&2; exit 1; }

# Same agent-name resolution as lib/brief-slot-ctl.sh (labels are rendered
# from the minted name, not a placeholder).
AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$ROOT")}"
LABEL_PREFIX="com.telegram-skill-bot.${AGENT_NAME}."
HAVE_PLUTIL=0; command -v plutil >/dev/null 2>&1 && HAVE_PLUTIL=1
BACKEND="${BRIEF_SCHED_BACKEND:-}"
if [ -z "$BACKEND" ]; then
  if command -v launchctl >/dev/null 2>&1; then BACKEND=launchd
  elif command -v systemctl >/dev/null 2>&1; then BACKEND=systemd
  else BACKEND=none; fi   # no scheduler: config only, every slot reported dead
fi
case "$BACKEND" in launchd|systemd|none) ;; *)
  echo "ERROR: bad BRIEF_SCHED_BACKEND '$BACKEND' (want launchd|systemd)" >&2; exit 2 ;; esac

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

# --- slot -> unit resolution -------------------------------------------------
# Candidate order per slot: section-* (section routines), then the
# morning-brief / daily-retro / weekly-review routine set, then generic-brief
# (the kit-neutral fallback, normally deferred). Only LOADED units count, so a
# slot's owner is whichever of these this instance actually runs.
default_units() { # default_units <slot>
  case "$1" in
    morning) echo "section-morning morning-brief generic-brief-morning" ;;
    retro)   echo "section-retro daily-retro generic-brief-retro" ;;
    weekly)  echo "section-weekly weekly-review generic-brief-weekly" ;;
  esac
}
slot_units() { # slot_units <slot> -> space-separated label suffixes
  local slot="$1" key ov
  key="BRIEF_UNITS_$(printf '%s' "$slot" | tr 'a-z' 'A-Z')"
  ov="$(conf_get "$key")"
  if [ -n "$ov" ]; then printf '%s\n' "$ov"; else default_units "$slot"; fi
}
is_loaded() { # is_loaded <label | unit.timer>
  case "$BACKEND" in
    launchd)
      command -v launchctl >/dev/null 2>&1 || return 1
      launchctl print "gui/$UID_N/$1" >/dev/null 2>&1 && return 0
      # `launchctl list` columns: PID Status Label -- exact label match only.
      launchctl list 2>/dev/null | awk -v l="$1" '$3 == l { f = 1 } END { exit !f }' ;;
    systemd)
      command -v systemctl >/dev/null 2>&1 || return 1
      systemctl --user is-active --quiet "$1" 2>/dev/null ;;
    *) return 1 ;;
  esac
}
unit_ids() { # unit_ids <suffix> -> candidate unit ids for this backend
  case "$BACKEND" in
    systemd) printf '%s\n' "dogany-${AGENT_NAME}-$1.timer" "${LABEL_PREFIX}$1.timer" ;;
    *)       printf '%s\n' "${LABEL_PREFIX}$1" ;;
  esac
}
unit_file() { # unit_file <unit id> -> installed schedule file (may not exist)
  local frag
  case "$BACKEND" in
    systemd)
      frag="$(systemctl --user show -p FragmentPath --value "$1" 2>/dev/null || true)"
      if [ -n "$frag" ] && [ -f "$frag" ]; then printf '%s\n' "$frag"
      else printf '%s\n' "$UNIT_DIR/$1"; fi ;;
    *) printf '%s\n' "$LA_DIR/$1.plist" ;;
  esac
}
loaded_labels() { # loaded_labels <slot> -> loaded unit ids, priority order
  local slot="$1" u id
  for u in $(slot_units "$slot"); do
    for id in $(unit_ids "$u"); do
      is_loaded "$id" && printf '%s\n' "$id"
    done
  done
  return 0
}

# --- plist read/write --------------------------------------------------------
# plutil is used for BOTH read and write: Apple's parser tolerates the illegal
# `--` inside routine.plist.tpl's XML comment, which strict XML parsers reject.
_sci_json() { plutil -convert json -o - "$1" 2>/dev/null; }

primary_hhmm() { # primary_hhmm <plist> -> HH:MM of entry[0], empty on failure
  _sci_json "$1" | python3 -c '
import json, sys
try: sci = json.load(sys.stdin).get("StartCalendarInterval")
except Exception: raise SystemExit
if sci is None: raise SystemExit
if isinstance(sci, dict): sci = [sci]
if not sci: raise SystemExit
print("%02d:%02d" % (sci[0].get("Hour", 0), sci[0].get("Minute", 0)))
' 2>/dev/null || true
}

primary_weekday() { # primary_weekday <plist> -> 0..6 of entry[0], empty if none
  _sci_json "$1" | python3 -c '
import json, sys
try: sci = json.load(sys.stdin).get("StartCalendarInterval")
except Exception: raise SystemExit
if sci is None: raise SystemExit
if isinstance(sci, dict): sci = [sci]
if sci and "Weekday" in sci[0]: print(int(sci[0]["Weekday"]))
' 2>/dev/null || true
}

describe_sci() { # describe_sci <plist> -> human string
  _sci_json "$1" | python3 -c '
import json, sys
try: sci = json.load(sys.stdin).get("StartCalendarInterval")
except Exception: print("(unreadable)"); raise SystemExit
if sci is None: print("(none)"); raise SystemExit
if isinstance(sci, dict): sci = [sci]
days = "Sun Mon Tue Wed Thu Fri Sat".split()
parts = []
for e in sci:
    t = "%02d:%02d" % (e.get("Hour", 0), e.get("Minute", 0))
    if "Weekday" in e: t = days[e["Weekday"] % 7] + " " + t
    parts.append(t)
print(", ".join(parts))
' 2>/dev/null || echo "(unreadable)"
}

render_sci() { # render_sci <plist> <hh> <mm> [wd] -> XML fragment on stdout
  _sci_json "$1" | python3 -c '
import json, sys
hh, mm = int(sys.argv[1]), int(sys.argv[2])
wd = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "" else None
sci = json.load(sys.stdin).get("StartCalendarInterval")
if sci is None:
    sys.exit(3)
was_dict = isinstance(sci, dict)
entries = [sci] if was_dict else list(sci)
if not entries:
    sys.exit(3)
base = entries[0].get("Hour", 0) * 60 + entries[0].get("Minute", 0)
new_base = hh * 60 + mm
out = []
for e in entries:
    cur = e.get("Hour", 0) * 60 + e.get("Minute", 0)
    # entry[0] lands exactly on the requested time; later entries keep their
    # original offset (primary + backstop gap survives a time change).
    t = (new_base + (cur - base)) % (24 * 60)
    item = {"Hour": t // 60, "Minute": t % 60}
    # Weekday is only rewritten on entries that already carried one.
    if "Weekday" in e:
        item["Weekday"] = int(wd) if wd is not None else e["Weekday"]
    out.append(item)
def frag(it):
    s = "<dict>"
    if "Weekday" in it:
        s += "<key>Weekday</key><integer>%d</integer>" % it["Weekday"]
    s += "<key>Hour</key><integer>%d</integer>" % it["Hour"]
    s += "<key>Minute</key><integer>%d</integer>" % it["Minute"]
    return s + "</dict>"
sys.stdout.write(frag(out[0]) if was_dict
                 else "<array>" + "".join(frag(i) for i in out) + "</array>")
' "$2" "$3" "${4:-}"
}

# --- systemd timer read/write (DGN-1810) --------------------------------------
# One python helper over the [Timer] OnCalendar= lines. Supported shape per
# line: "[<Day>] <date> HH:MM[:SS]" (what install.sh / routine-ctl.sh render).
# An empty "OnCalendar=" resets the list (systemd semantics). A line whose time
# is not HH:MM (e.g. "daily") makes the file unreadable here, so it is skipped
# rather than guessed.
_timer_py() { # _timer_py <mode> <file> [hh mm [wd]]; mode: hhmm|wd|describe|render
  python3 - "$@" <<'EOPY'
import re, sys
mode, path = sys.argv[1], sys.argv[2]
NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
FULL = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday"]
TIME = re.compile(r"^(\d{1,2}):(\d{2})(:\d{2}(?:\.\d+)?)?$")
def day_idx(tok):
    t = tok.lower()
    for i in range(7):
        if t == NAMES[i].lower() or t == FULL[i]:
            return i
    return None
def parse(val):
    toks = val.split()
    if not toks: return None
    m = TIME.match(toks[-1])
    if not m: return None
    day = toks[0] if len(toks) > 1 and toks[0][:1].isalpha() else None
    date = toks[1:-1] if day else toks[:-1]
    return {"day": day, "date": date, "h": int(m.group(1)),
            "m": int(m.group(2)), "sec": m.group(3) or ""}
lines = open(path).read().split("\n")
sect, ents = None, []
for i, ln in enumerate(lines):
    s = ln.strip()
    if s.startswith("[") and s.endswith("]"):
        sect = s; continue
    if sect != "[Timer]": continue
    k, eq, v = s.partition("=")
    if not eq or k.strip() != "OnCalendar": continue
    v = v.strip()
    if not v:
        ents = []; continue
    p = parse(v)
    if p is None: sys.exit(3)
    p["line"] = i; ents.append(p)
if not ents: sys.exit(3)
if mode == "hhmm":
    print("%02d:%02d" % (ents[0]["h"], ents[0]["m"]))
elif mode == "wd":
    i = day_idx(ents[0]["day"] or "")
    if i is not None: print(i)
elif mode == "describe":
    print(", ".join(((e["day"] + " ") if e["day"] else "")
                    + "%02d:%02d" % (e["h"], e["m"]) for e in ents))
elif mode == "render":
    hh, mm = int(sys.argv[3]), int(sys.argv[4])
    wd = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "" else None
    base = ents[0]["h"] * 60 + ents[0]["m"]
    new_base = hh * 60 + mm
    for e in ents:
        # entry[0] lands on the requested time; later lines keep their offset.
        t = (new_base + (e["h"] * 60 + e["m"] - base)) % (24 * 60)
        day = e["day"]
        # A day is only rewritten on lines that already carried one.
        if day and wd is not None: day = NAMES[int(wd) % 7]
        val = " ".join(([day] if day else []) + e["date"]
                       + ["%02d:%02d%s" % (t // 60, t % 60, e["sec"])])
        lines[e["line"]] = "OnCalendar=" + val
    sys.stdout.write("\n".join(lines))
EOPY
}

primary_hhmm_any() { # primary_hhmm_any <file> -> HH:MM (plist or timer)
  case "$1" in
    *.timer) _timer_py hhmm "$1" 2>/dev/null || true ;;
    *) [ "$HAVE_PLUTIL" = "1" ] && primary_hhmm "$1" ;;
  esac
  return 0
}
primary_weekday_any() { # primary_weekday_any <file> -> 0..6 or empty
  case "$1" in
    *.timer) _timer_py wd "$1" 2>/dev/null || true ;;
    *) [ "$HAVE_PLUTIL" = "1" ] && primary_weekday "$1" ;;
  esac
  return 0
}

apply_timer() { # apply_timer <timer> <hh> <mm> [wd]
  local f="$1" before after tmp
  before="$(_timer_py describe "$f" 2>/dev/null)" || {
    echo "      ! $f: no readable OnCalendar -- skipped" >&2; return 1; }
  tmp="$(mktemp)"
  _timer_py render "$f" "$2" "$3" "${4:-}" > "$tmp" 2>/dev/null || {
    rm -f "$tmp"; echo "      ! $f: OnCalendar rewrite failed -- skipped" >&2; return 1; }
  if [ "$DRY_RUN" = "1" ]; then
    rm -f "$tmp"; echo "      (dry-run) $(basename "$f"): $before -> [would rewrite]"; return 0
  fi
  cat "$tmp" > "$f"; rm -f "$tmp"   # in place: a symlinked unit keeps its target
  after="$(_timer_py describe "$f" 2>/dev/null || echo "(unreadable)")"
  echo "      $(basename "$f"): $before -> $after"
}

apply_unit() { # apply_unit <file> <hh> <mm> [wd] -- dispatch on file type
  case "$1" in
    *.timer) apply_timer "$@" ;;
    *)       apply_plist "$@" ;;
  esac
}

apply_plist() { # apply_plist <plist> <hh> <mm> [wd]
  local f="$1" hh="$2" mm="$3" wd="${4:-}" xml before after
  [ "$HAVE_PLUTIL" = "1" ] || {
    echo "      ! $(basename "$f"): plutil not found -- skipped" >&2; return 1; }
  before="$(describe_sci "$f")"
  xml="$(render_sci "$f" "$hh" "$mm" "$wd")" || {
    echo "      ! $f: no StartCalendarInterval -- skipped" >&2; return 1; }
  if [ "$DRY_RUN" = "1" ]; then
    echo "      (dry-run) $(basename "$f"): $before -> [would rewrite]"; return 0
  fi
  plutil -replace StartCalendarInterval -xml "$xml" "$f" || {
    echo "      ! $f: plutil replace failed" >&2; return 1; }
  plutil -lint "$f" >/dev/null || { echo "      ! $f: lint failed after write" >&2; return 1; }
  after="$(describe_sci "$f")"
  echo "      $(basename "$f"): $before -> $after"
}

reload_unit() { # reload_unit <unit id> <file>
  [ "$DRY_RUN" = "1" ] && { echo "      (dry-run) would reload $1"; return 0; }
  if [ "$BACKEND" = "systemd" ]; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    systemctl --user restart "$1" >/dev/null 2>&1 || true
  else
    launchctl bootout "gui/$UID_N/$1" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$UID_N" "$2" 2>/dev/null \
      || launchctl load "$2" 2>/dev/null || true
  fi
  if is_loaded "$1"; then echo "      reloaded: $1"
  else echo "      ! FAILED TO RELOAD: $1" >&2; return 1; fi
}

regen_slot() { # regen_slot <slot> <hh> <mm> [wd]
  local slot="$1" hh="$2" mm="$3" wd="${4:-}" label f moved=0 pending=0 staged
  printf '  [%s] target %02d:%02d%s\n' "$slot" "$((10#$hh))" "$((10#$mm))" "${wd:+ (weekday $wd)}"
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    f="$(unit_file "$label")"
    if [ ! -f "$f" ]; then
      echo "      ! $label is loaded but $f is missing -- skipped" >&2; continue
    fi
    apply_unit "$f" "$hh" "$mm" "$wd" || continue
    if [ "$RELOAD" = "1" ]; then
      reload_unit "$label" "$f" && moved=1
    else
      echo "      (--no-reload) $label file updated, live clock unchanged until reload"
      pending=1
    fi
  done <<< "$(loaded_labels "$slot")"
  # Keep the DORMANT staged generic-brief copy time-synced on disk so a later
  # brief-slot-ctl enable inherits the right clock. Never loaded from here.
  # Glob, not the exact label: the staged file carries the minted name, which
  # need not equal the root basename when .instance.conf is absent.
  for staged in "$RTDIR"/*generic-brief-"${slot}".plist; do
    [ -f "$staged" ] || continue
    # The staged copy is a plist; with no plutil (Linux) it is left as is.
    [ "$HAVE_PLUTIL" = "1" ] || continue
    [ "$BACKEND" = "launchd" ] && is_loaded "$(basename "$staged" .plist)" && continue
    echo "      (staged, dormant)"
    apply_plist "$staged" "$hh" "$mm" "$wd" || true
  done
  if [ "$moved" = "1" ]; then
    MOVED=$((MOVED + 1))
  elif [ "$pending" = "1" ]; then
    PENDING_SLOTS="${PENDING_SLOTS}${PENDING_SLOTS:+ }$slot"
  else
    DEAD_SLOTS="${DEAD_SLOTS}${DEAD_SLOTS:+ }$slot"
    echo "      ! $slot: NO LOADED UNIT -- config updated, nothing fires" >&2
  fi
}

# --- resolve the effective time per slot -------------------------------------
# explicit arg -> config -> LIVE unit (adopt) -> unresolved (left unwritten).
slot_live_file() { # slot_live_file <slot> -> installed file of the top loaded unit
  local label f
  label="$(loaded_labels "$1" | head -1)"
  [ -n "$label" ] || return 0
  f="$(unit_file "$label")"
  [ -f "$f" ] && printf '%s\n' "$f"
  return 0
}
slot_live_hhmm() { # slot_live_hhmm <slot>
  local f; f="$(slot_live_file "$1")"
  [ -n "$f" ] && primary_hhmm_any "$f"
  return 0
}
slot_live_wd() { # slot_live_wd <slot>
  local f; f="$(slot_live_file "$1")"
  [ -n "$f" ] && primary_weekday_any "$f"
  return 0
}

MORNING="${IN_MORNING:-$(conf_get BRIEF_TIME_MORNING)}"
MORNING="${MORNING:-$(slot_live_hhmm morning)}"
RETRO="${IN_RETRO:-$(conf_get BRIEF_TIME_RETRO)}"
RETRO="${RETRO:-$(slot_live_hhmm retro)}"

WEEKLY="${IN_WEEKLY:-$(conf_get BRIEF_TIME_WEEKLY)}"
if [ -z "$WEEKLY" ]; then
  _wt="$(slot_live_hhmm weekly)"; _wd="$(slot_live_wd weekly)"
  # Adopt only a complete "<Day> HH:MM": a live weekly unit without a
  # Weekday is not guessed onto a day.
  if [ -n "$_wt" ] && [ -n "$_wd" ]; then
    _days=(Sun Mon Tue Wed Thu Fri Sat)
    WEEKLY="${_days[$_wd]} $_wt"
  fi
fi

UNSET_SLOTS=""
[ -n "$MORNING" ] || UNSET_SLOTS="${UNSET_SLOTS}${UNSET_SLOTS:+ }morning"
[ -n "$RETRO" ]   || UNSET_SLOTS="${UNSET_SLOTS}${UNSET_SLOTS:+ }retro"
[ -n "$WEEKLY" ]  || UNSET_SLOTS="${UNSET_SLOTS}${UNSET_SLOTS:+ }weekly"

[ -z "$MORNING" ] || valid_hhmm "$MORNING" || { echo "ERROR: bad --morning '$MORNING' (want HH:MM)" >&2; exit 1; }
[ -z "$RETRO" ]   || valid_hhmm "$RETRO"   || { echo "ERROR: bad --retro '$RETRO' (want HH:MM)" >&2; exit 1; }

# weekly = "<Day> HH:MM"
W_DAY=""; W_TIME=""; W_WD=""
if [ -n "$WEEKLY" ]; then
  W_DAY="${WEEKLY%% *}"; W_TIME="${WEEKLY##* }"
  valid_hhmm "$W_TIME" || { echo "ERROR: bad --weekly time in '$WEEKLY' (want '<Day> HH:MM')" >&2; exit 1; }
  case "$(printf '%s' "$W_DAY" | tr 'A-Z' 'a-z')" in
    sun) W_WD=0 ;; mon) W_WD=1 ;; tue) W_WD=2 ;; wed) W_WD=3 ;;
    thu) W_WD=4 ;; fri) W_WD=5 ;; sat) W_WD=6 ;;
    *) echo "ERROR: bad --weekly day '$W_DAY' (want Sun..Sat)" >&2; exit 1 ;;
  esac
fi

# --- write config (source of truth) ------------------------------------------
# Only resolved slots are written; an unresolved slot keeps no key at all.
if [ "$DRY_RUN" != "1" ]; then
  [ -z "$MORNING" ] || conf_upsert BRIEF_TIME_MORNING "$MORNING"
  [ -z "$RETRO" ]   || conf_upsert BRIEF_TIME_RETRO   "$RETRO"
  [ -z "$WEEKLY" ]  || conf_upsert BRIEF_TIME_WEEKLY  "$W_DAY $W_TIME"
fi

MOVED=0; DEAD_SLOTS=""; PENDING_SLOTS=""
echo "[briefing] instance=$AGENT_NAME root=$ROOT backend=$BACKEND$([ "$DRY_RUN" = "1" ] && echo " (dry-run: nothing written)")"
[ "$BACKEND" != "launchd" ] || [ "$HAVE_PLUTIL" = "1" ] \
  || echo "  ! plutil not found: config only, no plist is rewritten" >&2
unset_slot() { # unset_slot <slot>
  printf '  [%s] no time (no argument, no BRIEF_TIME_* key, no loaded unit)\n' "$1"
  echo "      ! $1: TIME UNSET -- nothing written for this slot" >&2
}
if [ -n "$MORNING" ]; then regen_slot morning "${MORNING%%:*}" "${MORNING##*:}"; else unset_slot morning; fi
if [ -n "$RETRO" ];   then regen_slot retro   "${RETRO%%:*}"   "${RETRO##*:}";   else unset_slot retro; fi
if [ -n "$WEEKLY" ];  then regen_slot weekly  "${W_TIME%%:*}"  "${W_TIME##*:}" "$W_WD"; else unset_slot weekly; fi

# Never report a live move that did not happen -- that overclaim is the defect
# class this script exists to close. Dead / pending / unset slots are named so
# the caller can pass the caveat on to the user.
SUMMARY="morning=${MORNING:-(unset)} retro=${RETRO:-(unset)} weekly=${WEEKLY:+$W_DAY $W_TIME}"
[ -n "$WEEKLY" ] || SUMMARY="${SUMMARY}(unset)"
if [ "$DRY_RUN" = "1" ]; then
  echo "briefing times (dry-run, not written): $SUMMARY"
else
  echo "briefing times set: $SUMMARY"
fi
if [ -n "$PENDING_SLOTS" ]; then
  echo "  --no-reload: plist rewritten, live clock NOT moved yet for: $PENDING_SLOTS" >&2
fi
if [ -n "$UNSET_SLOTS" ]; then
  echo "  NO TIME for: $UNSET_SLOTS -- left unwritten (pass --morning/--retro/--weekly)." >&2
fi
if [ -n "$DEAD_SLOTS" ]; then
  echo "  config written for resolved slots; $MOVED slot(s) moved a live unit." >&2
  echo "  NO EMITTER on this instance for: $DEAD_SLOTS -- nothing fires at those slots." >&2
fi
[ -z "$DEAD_SLOTS" ] && [ -z "$UNSET_SLOTS" ] || exit 4
[ -n "$PENDING_SLOTS" ] || echo "  config + $MOVED live unit slot(s) in lockstep."
exit 0
