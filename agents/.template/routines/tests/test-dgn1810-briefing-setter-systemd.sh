#!/bin/bash
# test-lane: fast
# DGN-1810: routines/set-briefing-times.sh moves systemd --user timers.
# The pre-fix setter was launchd-only: on Linux every slot exited 4 (config
# written, "no loaded unit") and no timer moved.
#
#   [1] loaded timers rewritten (OnCalendar) + daemon-reload + restart, all 3
#       slots; weekly day rewritten; routine-ctl naming (dogany-<agent>-<x>)
#   [2] inactive timer is never started/rewritten; template-shipped naming
#       (com.telegram-skill-bot.<agent>.<x>.timer) resolved via FragmentPath
#   [3] first run (no args, no BRIEF_TIME_*) adopts the live time + weekday
#   [4] multi-fire slot: backstop OnCalendar line keeps its offset
#   [5] --dry-run writes nothing, never reloads/restarts
#   [6] --no-reload rewrites the file, no systemctl reload, pending named
#   [7] dayless weekly timer: no day guessed onto it, weekly left unwritten
#   [8] no active timer at all: exit 4, slots named dead
#
# SAFETY: systemctl is a PATH stub; the real systemd is never reached.
# ROUTINE_CTL_UNIT_DIR points at a temp dir; BRIEF_SCHED_BACKEND forces the
# systemd backend so the suite runs the same on macOS.
#
# Run: bash agents/.template/routines/tests/test-dgn1810-briefing-setter-systemd.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBT="$(cd "$SCRIPT_DIR/.." && pwd)/set-briefing-times.sh"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
check(){ if eval "$2"; then ok "$1"; else fail "$1"; fi; }

T="$(mktemp -d "${TMPDIR:-/tmp}/dgn1810.XXXXXX")"
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"

# --- stub --------------------------------------------------------------------
# state: $SC_STATE (one active unit per line); $SC_FRAG ("unit<TAB>path"
# lines answered by `show -p FragmentPath`); every call -> $SC_LOG
cat > "$BIN/systemctl" <<'EOS'
#!/bin/bash
echo "$*" >> "$SC_LOG"
touch "$SC_STATE" "$SC_FRAG"
[ "$1" = "--user" ] && shift
case "$1" in
  is-active) u="${!#}"; grep -qxF -- "$u" "$SC_STATE" ;;
  show) u="${!#}"; awk -F'\t' -v u="$u" '$1 == u { print $2 }' "$SC_FRAG" ;;
  daemon-reload) exit 0 ;;
  restart) grep -qxF -- "$2" "$SC_STATE" || echo "$2" >> "$SC_STATE" ;;
  *) exit 1 ;;
esac
EOS
chmod +x "$BIN/systemctl"
export PATH="$BIN:$PATH"

AG=tbot; P="com.telegram-skill-bot.$AG"
mkinst() {
  local r="$T/$1"
  mkdir -p "$r/config" "$r/routines" "$r/units"
  echo "AGENT_LANG=en" > "$r/config/agent.conf"
  echo "DOGANY_AGENT_NAME=$AG" > "$r/.instance.conf"
  : > "$r/sc.state"; : > "$r/sc.log"; : > "$r/sc.frag"
  echo "$r"
}
# mktimer <file> <OnCalendar values...>
mktimer() {
  local f="$1"; shift
  { printf '[Unit]\nDescription=test timer\n\n[Timer]\n'
    for v in "$@"; do printf 'OnCalendar=%s\n' "$v"; done
    printf 'Persistent=true\n\n[Install]\nWantedBy=timers.target\n'; } > "$f"
}
# load <root> <unit> <OnCalendar...> -> unit file in the unit dir + active
load() { local r="$1" u="$2"; shift 2; mktimer "$r/units/$u" "$@"; echo "$u" >> "$r/sc.state"; }
oncal() { grep '^OnCalendar=' "$1" | cut -d= -f2- | paste -sd'|' -; }
run() { local r="$1"; shift
  BRIEF_SCHED_BACKEND=systemd ROUTINE_CTL_UNIT_DIR="$r/units" \
    SC_STATE="$r/sc.state" SC_LOG="$r/sc.log" SC_FRAG="$r/sc.frag" \
    bash "$SBT" --root "$r" "$@" >"$r/out" 2>"$r/err"; echo $? > "$r/rc"; }
rc() { cat "$1/rc"; }

# --- [1] + [4] ----------------------------------------------------------------
echo "[1][4] active timers rewritten + reloaded; backstop kept"
R="$(mkinst live)"
load "$R" "dogany-$AG-morning-brief.timer" "*-*-* 05:00:00"
load "$R" "dogany-$AG-daily-retro.timer" "*-*-* 20:30:00" "*-*-* 20:45:00"
load "$R" "dogany-$AG-weekly-review.timer" "Sun *-*-* 21:00:00"
run "$R" --morning 06:30 --retro 21:00 --weekly "Sat 09:15"
check "[1] exit 0 when every slot moved a live timer" '[ "$(rc "$R")" = 0 ]'
check "[1] morning OnCalendar -> 06:30" '[ "$(oncal "$R/units/dogany-$AG-morning-brief.timer")" = "*-*-* 06:30:00" ]'
check "[1] weekly OnCalendar -> Sat 09:15" '[ "$(oncal "$R/units/dogany-$AG-weekly-review.timer")" = "Sat *-*-* 09:15:00" ]'
check "[1] daemon-reload issued" 'grep -q "^--user daemon-reload$" "$R/sc.log"'
for u in morning-brief daily-retro weekly-review; do
  check "[1] $u timer restarted" 'grep -q "^--user restart dogany-$AG-$u.timer$" "$R/sc.log"'
done
check "[1] config holds the new times" \
  'grep -qx "BRIEF_TIME_MORNING=06:30" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_RETRO=21:00" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_WEEKLY=Sat 09:15" "$R/config/agent.conf"'
check "[1] lockstep claimed for 3 slots" 'grep -q "config + 3 live unit slot(s) in lockstep" "$R/out"'
check "[1] no dead slot reported" '! grep -q "NO EMITTER" "$R/err"'
check "[1] other timer keys survive the rewrite" \
  'grep -qx "Persistent=true" "$R/units/dogany-$AG-daily-retro.timer" && grep -qx "WantedBy=timers.target" "$R/units/dogany-$AG-daily-retro.timer"'
check "[4] retro backstop keeps +15: 20:30|20:45 -> 21:00|21:15" \
  '[ "$(oncal "$R/units/dogany-$AG-daily-retro.timer")" = "*-*-* 21:00:00|*-*-* 21:15:00" ]'

# --- [2] inactive timer untouched; template naming via FragmentPath ------------
echo "[2] inactive timer untouched; template-shipped naming resolved"
R="$(mkinst frag)"
mkdir -p "$R/elsewhere"
mktimer "$R/elsewhere/$P.section-morning.timer" "*-*-* 04:15:00"
echo "$P.section-morning.timer" >> "$R/sc.state"
printf '%s\t%s\n' "$P.section-morning.timer" "$R/elsewhere/$P.section-morning.timer" > "$R/sc.frag"
mktimer "$R/units/dogany-$AG-generic-brief-morning.timer" "*-*-* 05:00:00"   # installed, NOT active
DEF_SUM="$(cksum < "$R/units/dogany-$AG-generic-brief-morning.timer")"
load "$R" "dogany-$AG-daily-retro.timer" "*-*-* 21:00:00"
load "$R" "dogany-$AG-weekly-review.timer" "Sun *-*-* 21:00:00"
run "$R" --morning 04:45
check "[2] exit 0" '[ "$(rc "$R")" = 0 ]'
check "[2] FragmentPath timer rewritten to 04:45" '[ "$(oncal "$R/elsewhere/$P.section-morning.timer")" = "*-*-* 04:45:00" ]'
check "[2] inactive generic-brief timer byte-identical" \
  '[ "$(cksum < "$R/units/dogany-$AG-generic-brief-morning.timer")" = "$DEF_SUM" ]'
check "[2] inactive timer never restarted" '! grep -q "restart .*generic-brief" "$R/sc.log"'

# --- [3] adopt ----------------------------------------------------------------
echo "[3] first run adopts the live time + weekday"
R="$(mkinst adopt)"
load "$R" "dogany-$AG-morning-brief.timer" "*-*-* 05:10:00"
load "$R" "dogany-$AG-daily-retro.timer" "*-*-* 20:30:00" "*-*-* 20:45:00"
load "$R" "dogany-$AG-weekly-review.timer" "Fri *-*-* 18:00:00"
run "$R"
check "[3] exit 0" '[ "$(rc "$R")" = 0 ]'
check "[3] morning adopted 05:10" 'grep -qx "BRIEF_TIME_MORNING=05:10" "$R/config/agent.conf"'
check "[3] retro adopted primary 20:30" 'grep -qx "BRIEF_TIME_RETRO=20:30" "$R/config/agent.conf"'
check "[3] weekly adopted Fri 18:00 from the timer's day" 'grep -qx "BRIEF_TIME_WEEKLY=Fri 18:00" "$R/config/agent.conf"'
check "[3] live clocks unchanged" \
  '[ "$(oncal "$R/units/dogany-$AG-daily-retro.timer")" = "*-*-* 20:30:00|*-*-* 20:45:00" ] && [ "$(oncal "$R/units/dogany-$AG-weekly-review.timer")" = "Fri *-*-* 18:00:00" ]'

# --- [5] dry-run --------------------------------------------------------------
echo "[5] --dry-run writes nothing"
R="$(mkinst dry)"
load "$R" "dogany-$AG-morning-brief.timer" "*-*-* 05:00:00"
load "$R" "dogany-$AG-weekly-review.timer" "Sun *-*-* 21:00:00"
snap() { (cat "$R/config/agent.conf"; for f in "$R"/units/*; do cksum < "$f"; done) | cksum; }
BEFORE="$(snap)"
run "$R" --dry-run --morning 06:00 --retro 23:00 --weekly "Mon 08:00"
check "[5] config + timers byte-identical" '[ "$(snap)" = "$BEFORE" ]'
check "[5] no daemon-reload / restart" '! grep -Eq "daemon-reload|restart" "$R/sc.log"'
check "[5] output says dry-run" 'grep -q "dry-run, not written" "$R/out"'

# --- [6] --no-reload ----------------------------------------------------------
echo "[6] --no-reload"
R="$(mkinst noreload)"
load "$R" "dogany-$AG-morning-brief.timer" "*-*-* 05:00:00"
load "$R" "dogany-$AG-daily-retro.timer" "*-*-* 21:00:00"
load "$R" "dogany-$AG-weekly-review.timer" "Sun *-*-* 21:00:00"
run "$R" --no-reload --morning 06:45
check "[6] exit 0" '[ "$(rc "$R")" = 0 ]'
check "[6] file rewritten to 06:45" '[ "$(oncal "$R/units/dogany-$AG-morning-brief.timer")" = "*-*-* 06:45:00" ]'
check "[6] no daemon-reload / restart" '! grep -Eq "daemon-reload|restart" "$R/sc.log"'
check "[6] pending named, no lockstep claim" \
  'grep -q "live clock NOT moved yet for: morning retro weekly" "$R/err" && ! grep -q "in lockstep" "$R/out"'

# --- [7] dayless weekly timer: no guessed day ---------------------------------
echo "[7] dayless weekly timer is not guessed onto a day"
R="$(mkinst dayless)"
load "$R" "dogany-$AG-morning-brief.timer" "*-*-* 05:00:00"
load "$R" "dogany-$AG-daily-retro.timer" "*-*-* 21:00:00"
load "$R" "dogany-$AG-weekly-review.timer" "*-*-* 21:00:00"
run "$R"
check "[7] exit 4 (weekly unresolved)" '[ "$(rc "$R")" = 4 ]'
check "[7] no BRIEF_TIME_WEEKLY written" '! grep -q "^BRIEF_TIME_WEEKLY=" "$R/config/agent.conf"'
check "[7] weekly named unset" 'grep -q "NO TIME for: weekly" "$R/err"'

# --- [8] nothing active -------------------------------------------------------
echo "[8] no active timer"
R="$(mkinst dead)"
mktimer "$R/units/dogany-$AG-morning-brief.timer" "*-*-* 05:00:00"   # present, inactive
run "$R" --morning 06:00 --retro 22:00 --weekly "Sun 20:00"
check "[8] exit 4" '[ "$(rc "$R")" = 4 ]'
check "[8] all slots named dead" 'grep -q "NO EMITTER on this instance for: morning retro weekly" "$R/err"'
check "[8] inactive timer untouched" '[ "$(oncal "$R/units/dogany-$AG-morning-brief.timer")" = "*-*-* 05:00:00" ]'
check "[8] backend reported" 'grep -q "backend=systemd" "$R/out"'

echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ]
