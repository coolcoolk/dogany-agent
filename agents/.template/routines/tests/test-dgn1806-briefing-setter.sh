#!/bin/bash
# test-lane: fast
# DGN-1806: routines/set-briefing-times.sh moves the LOADED unit per slot.
# The pre-fix setter rewrote the deferred routines/*generic-brief-<slot>.plist
# only, reported success, and the live clock never moved.
#
#   [1] loaded unit rewritten + reloaded (bootout + bootstrap), all 3 slots
#   [2] deferred unit: installed-but-unloaded plist untouched and never
#       loaded; staged routines/ copy time-synced on disk, never loaded
#   [3] first run (no args, no BRIEF_TIME_*) adopts the live time
#   [4] multi-fire slot: backstop keeps its offset from the primary
#   [5] --dry-run writes nothing (config, installed, staged) and never
#       touches launchd
#   [6] slot with no loaded unit and no time: left unwritten, exit 4, named
#   [7] --no-reload rewrites the file, no launchctl, no lockstep claim
#   [8] BRIEF_UNITS_<SLOT> override picks a custom unit
#   [9] static: ASCII only, no hardcoded default time in the setter
#  [10] no arg + no config + no loaded unit (any slot): nothing written, exit 4
#  [11] kit-style explicit args on a no-launchd instance: all written, exit 4
#
# SAFETY: launchctl and plutil are PATH stubs (python3 plistlib stands in for
# plutil); the real launchd is never reached. ROUTINE_CTL_LA_DIR points at a
# temp dir.
#
# Run: bash agents/.template/routines/tests/test-dgn1806-briefing-setter.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBT="$(cd "$SCRIPT_DIR/.." && pwd)/set-briefing-times.sh"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
check(){ if eval "$2"; then ok "$1"; else fail "$1"; fi; }

T="$(mktemp -d "${TMPDIR:-/tmp}/dgn1806.XXXXXX")"
trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; mkdir -p "$BIN"

# --- stubs -------------------------------------------------------------------
cat > "$BIN/launchctl" <<'EOS'
#!/bin/bash
# state: $LC_STATE (one loaded label per line); every call -> $LC_LOG
echo "$*" >> "$LC_LOG"
touch "$LC_STATE"
case "$1" in
  print) grep -qxF -- "${2##*/}" "$LC_STATE" ;;
  list)  while IFS= read -r l; do printf -- '-\t0\t%s\n' "$l"; done < "$LC_STATE" ;;
  bootout) l="${2##*/}"; grep -vxF -- "$l" "$LC_STATE" > "$LC_STATE.t"; mv "$LC_STATE.t" "$LC_STATE" ;;
  bootstrap) basename "$3" .plist >> "$LC_STATE" ;;
  load) basename "$2" .plist >> "$LC_STATE" ;;
  *) exit 1 ;;
esac
EOS
cat > "$BIN/plutil" <<'EOS'
#!/usr/bin/env python3
import json, os, plistlib, sys
a = sys.argv[1:]
with open(os.environ["PL_LOG"], "a") as fh:
    fh.write(" ".join(a) + "\n")
try:
    if a[:2] == ["-convert", "json"]:
        with open(a[-1], "rb") as fh:
            sys.stdout.write(json.dumps(plistlib.load(fh)))
    elif a[0] == "-replace" and a[2] == "-xml":
        key, frag, path = a[1], a[3], a[4]
        doc = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0">'
               + frag + '</plist>').encode()
        with open(path, "rb") as fh:
            d = plistlib.load(fh)
        d[key] = plistlib.loads(doc)
        with open(path, "wb") as fh:
            plistlib.dump(d, fh)
    elif a[0] == "-lint":
        with open(a[1], "rb") as fh:
            plistlib.load(fh)
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
EOS
chmod +x "$BIN/launchctl" "$BIN/plutil"
export PATH="$BIN:$PATH"

# --- fixture helpers ---------------------------------------------------------
AG=tbot; P="com.telegram-skill-bot.$AG"
# mkplist <file> <label> <entries...>; entry = "HH:MM" or "WD HH:MM"; >1 entry -> array
mkplist() {
  local f="$1" label="$2"; shift 2
  python3 - "$f" "$label" "$@" <<'EOP'
import plistlib, sys
f, label, ents = sys.argv[1], sys.argv[2], sys.argv[3:]
out = []
for e in ents:
    p = e.split()
    hh, mm = map(int, p[-1].split(":"))
    it = {"Hour": hh, "Minute": mm}
    if len(p) == 2: it["Weekday"] = int(p[0])
    out.append(it)
d = {"Label": label, "ProgramArguments": ["/bin/true"],
     "StartCalendarInterval": out[0] if len(out) == 1 else out}
with open(f, "wb") as fh: plistlib.dump(d, fh)
EOP
}
# sci <file> -> "Wd HH:MM, ..." (Wd only when present)
sci() {
  python3 - "$1" <<'EOP'
import plistlib, sys
s = plistlib.load(open(sys.argv[1], "rb"))["StartCalendarInterval"]
s = [s] if isinstance(s, dict) else s
print(", ".join((str(e["Weekday"]) + " " if "Weekday" in e else "")
                + "%02d:%02d" % (e["Hour"], e["Minute"]) for e in s))
EOP
}
# mkinst <name> -> root with config, .instance.conf, staged generic-brief copies
mkinst() {
  local r="$T/$1" s
  mkdir -p "$r/config" "$r/routines" "$r/la"
  echo "AGENT_LANG=en" > "$r/config/agent.conf"
  echo "DOGANY_AGENT_NAME=$AG" > "$r/.instance.conf"
  for s in morning retro; do mkplist "$r/routines/$P.generic-brief-$s.plist" "$P.generic-brief-$s" "07:00"; done
  mkplist "$r/routines/$P.generic-brief-weekly.plist" "$P.generic-brief-weekly" "0 20:00"
  : > "$r/lc.state"; : > "$r/lc.log"; : > "$r/pl.log"
  echo "$r"
}
# load <root> <suffix> <entries...> -> installed + loaded unit
load() { local r="$1" u="$2"; shift 2
  mkplist "$r/la/$P.$u.plist" "$P.$u" "$@"; echo "$P.$u" >> "$r/lc.state"; }
run() { local r="$1"; shift
  ROUTINE_CTL_LA_DIR="$r/la" LC_STATE="$r/lc.state" LC_LOG="$r/lc.log" PL_LOG="$r/pl.log" \
    bash "$SBT" --root "$r" "$@" >"$r/out" 2>"$r/err"; echo $? > "$r/rc"; }
rc() { cat "$1/rc"; }

# --- [1] + [2] + [4]: live instance, all three slots owned, explicit times ----
echo "[1][2][4] loaded units rewritten + reloaded; deferred untouched; backstop kept"
R="$(mkinst live)"
load "$R" morning-brief "05:00"
load "$R" daily-retro "20:30" "20:45"
load "$R" weekly-review "0 22:00"
mkplist "$R/la/$P.generic-brief-morning.plist" "$P.generic-brief-morning" "07:00"   # installed, NOT loaded
DEF_SUM="$(cksum < "$R/la/$P.generic-brief-morning.plist")"
run "$R" --morning 06:30 --retro 21:00 --weekly "Sat 09:15"
check "[1] exit 0 when every slot moved a live unit" '[ "$(rc "$R")" = 0 ]'
check "[1] morning-brief rewritten to 06:30" '[ "$(sci "$R/la/$P.morning-brief.plist")" = "06:30" ]'
check "[1] weekly-review rewritten to Sat(6) 09:15" '[ "$(sci "$R/la/$P.weekly-review.plist")" = "6 09:15" ]'
for u in morning-brief daily-retro weekly-review; do
  check "[1] $u booted out + bootstrapped" \
    'grep -q "^bootout gui/.*/$P.$u\$" "$R/lc.log" && grep -q "^bootstrap gui/[0-9]* $R/la/$P.$u.plist\$" "$R/lc.log"'
done
check "[1] config holds the new times" \
  'grep -qx "BRIEF_TIME_MORNING=06:30" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_RETRO=21:00" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_WEEKLY=Sat 09:15" "$R/config/agent.conf"'
check "[1] summary claims lockstep for 3 slots" 'grep -q "config + 3 live unit slot(s) in lockstep" "$R/out"'
check "[4] retro backstop keeps +15: 20:30,20:45 -> 21:00,21:15" '[ "$(sci "$R/la/$P.daily-retro.plist")" = "21:00, 21:15" ]'
check "[2] installed-but-unloaded generic-brief plist byte-identical" \
  '[ "$(cksum < "$R/la/$P.generic-brief-morning.plist")" = "$DEF_SUM" ]'
check "[2] no generic-brief label ever bootstrapped/loaded" '! grep -E "^(bootstrap|load) .*generic-brief" "$R/lc.log"'
check "[2] generic-brief still not loaded afterwards" '! grep -q generic-brief "$R/lc.state"'
check "[2] staged dormant copy time-synced on disk (morning 06:30)" '[ "$(sci "$R/routines/$P.generic-brief-morning.plist")" = "06:30" ]'
check "[2] staged dormant weekly copy synced (Sat 09:15)" '[ "$(sci "$R/routines/$P.generic-brief-weekly.plist")" = "6 09:15" ]'
check "[2] no staged file copied into LaunchAgents" '[ ! -e "$R/la/$P.generic-brief-retro.plist" ]'

# --- [3] first run adopts the live time --------------------------------------
echo "[3] first run adopts the live time"
R="$(mkinst adopt)"
load "$R" morning-brief "05:00"
load "$R" section-retro "20:30" "20:45"
load "$R" weekly-review "0 22:00"
run "$R"
check "[3] exit 0" '[ "$(rc "$R")" = 0 ]'
check "[3] morning adopted 05:00 from the live unit" 'grep -qx "BRIEF_TIME_MORNING=05:00" "$R/config/agent.conf"'
check "[3] retro adopted from section-retro primary 20:30" 'grep -qx "BRIEF_TIME_RETRO=20:30" "$R/config/agent.conf"'
check "[3] weekly adopted Sun 22:00" 'grep -qx "BRIEF_TIME_WEEKLY=Sun 22:00" "$R/config/agent.conf"'
check "[3] live clocks unchanged" \
  '[ "$(sci "$R/la/$P.morning-brief.plist")" = "05:00" ] && [ "$(sci "$R/la/$P.section-retro.plist")" = "20:30, 20:45" ] && [ "$(sci "$R/la/$P.weekly-review.plist")" = "0 22:00" ]'
check "[3] config key beats live unit on the next run" \
  'echo "BRIEF_TIME_MORNING=06:00" >> "$R/config/agent.conf"; sed -i.bak "/^BRIEF_TIME_MORNING=05:00$/d" "$R/config/agent.conf"; run "$R"; [ "$(sci "$R/la/$P.morning-brief.plist")" = "06:00" ]'

# --- [5] --dry-run writes nothing --------------------------------------------
echo "[5] --dry-run writes nothing"
R="$(mkinst dry)"
load "$R" morning-brief "05:00"
load "$R" daily-retro "20:30" "20:45"
snap() { (cat "$R/config/agent.conf"; for f in "$R"/la/* "$R"/routines/*; do cksum < "$f"; done) | cksum; }
BEFORE="$(snap)"
run "$R" --dry-run --morning 06:00 --retro 23:00 --weekly "Mon 08:00"
check "[5] config, installed and staged plists byte-identical" '[ "$(snap)" = "$BEFORE" ]'
check "[5] no launchctl bootout/bootstrap/load" '! grep -Eq "^(bootout|bootstrap|load)" "$R/lc.log"'
check "[5] no plutil -replace" '! grep -q "^-replace" "$R/pl.log"'
check "[5] output says dry-run" 'grep -q "dry-run, not written" "$R/out"'

# --- [6] no loaded unit, no time -> unwritten, exit 4, slot named ------------
echo "[6] no-loaded-unit slot with no time is left unwritten"
R="$(mkinst dead)"
load "$R" morning-brief "05:00"
STAGED_RETRO="$(cksum < "$R/routines/$P.generic-brief-retro.plist")"
run "$R"
check "[6] exit 4 (retro/weekly have no time)" '[ "$(rc "$R")" = 4 ]'
check "[6] no BRIEF_TIME_RETRO written" '! grep -q "^BRIEF_TIME_RETRO=" "$R/config/agent.conf"'
check "[6] no BRIEF_TIME_WEEKLY written" '! grep -q "^BRIEF_TIME_WEEKLY=" "$R/config/agent.conf"'
check "[6] morning still adopted 05:00" 'grep -qx "BRIEF_TIME_MORNING=05:00" "$R/config/agent.conf"'
check "[6] stderr names the unset slots" 'grep -q "NO TIME for: retro weekly" "$R/err"'
check "[6] no lockstep overclaim" '! grep -q "in lockstep" "$R/out"'
check "[6] staged retro copy untouched (no guessed time)" \
  '[ "$(cksum < "$R/routines/$P.generic-brief-retro.plist")" = "$STAGED_RETRO" ]'
# fresh mint: nothing loaded, .instance.conf absent, staged name != root basename
R="$(mkinst fresh)"; rm -f "$R/.instance.conf"
run "$R" --morning 06:15
check "[6] fresh mint: exit 4, morning named dead, retro/weekly named unset" \
  '[ "$(rc "$R")" = 4 ] && grep -q "NO EMITTER on this instance for: morning --" "$R/err" && grep -q "NO TIME for: retro weekly" "$R/err"'
check "[6] fresh mint: only the passed slot written" \
  'grep -qx "BRIEF_TIME_MORNING=06:15" "$R/config/agent.conf" && ! grep -Eq "^BRIEF_TIME_(RETRO|WEEKLY)=" "$R/config/agent.conf"'
check "[6] fresh mint: staged copy synced even when root basename != minted name" \
  '[ "$(sci "$R/routines/$P.generic-brief-morning.plist")" = "06:15" ]'
check "[6] fresh mint: launchd never touched" '! grep -Eq "^(bootout|bootstrap|load)" "$R/lc.log"'

# --- [7] --no-reload ---------------------------------------------------------
echo "[7] --no-reload"
R="$(mkinst noreload)"
load "$R" morning-brief "05:00"; load "$R" daily-retro "21:00"; load "$R" weekly-review "0 22:00"
run "$R" --no-reload --morning 06:45
check "[7] exit 0" '[ "$(rc "$R")" = 0 ]'
check "[7] file rewritten to 06:45" '[ "$(sci "$R/la/$P.morning-brief.plist")" = "06:45" ]'
check "[7] no bootout/bootstrap" '! grep -Eq "^(bootout|bootstrap|load)" "$R/lc.log"'
check "[7] pending slots named, no lockstep claim" \
  'grep -q "live clock NOT moved yet for: morning retro weekly" "$R/err" && ! grep -q "in lockstep" "$R/out"'

# --- [8] BRIEF_UNITS override ------------------------------------------------
echo "[8] BRIEF_UNITS_<SLOT> override"
R="$(mkinst override)"
echo "BRIEF_UNITS_MORNING=custom-am" >> "$R/config/agent.conf"
load "$R" custom-am "04:00"; load "$R" morning-brief "05:00"
load "$R" daily-retro "21:00"; load "$R" weekly-review "0 22:00"
run "$R" --morning 06:10
check "[8] custom unit moved" '[ "$(sci "$R/la/$P.custom-am.plist")" = "06:10" ]'
check "[8] default-table unit ignored under override" '[ "$(sci "$R/la/$P.morning-brief.plist")" = "05:00" ]'

# --- [9] static --------------------------------------------------------------
echo "[9] static"
check "[9] setter is ASCII only" '! LC_ALL=C grep -q "[^ -~	]" "$SBT"'
check "[9] bash -n" 'bash -n "$SBT"'
check "[9] no hardcoded default time in the setter" \
  '! grep -Eq "07:00|22:00|20:00" "$SBT"'

# --- [10] nothing resolvable -> nothing written, exit 4 ----------------------
echo "[10] no arg + no config + no loaded unit"
R="$(mkinst nothing)"
CONF_BEFORE="$(cksum < "$R/config/agent.conf")"
snap10() { for f in "$R"/routines/*; do cksum < "$f"; done | cksum; }
STAGED_BEFORE="$(snap10)"
run "$R"
check "[10] exit 4" '[ "$(rc "$R")" = 4 ]'
check "[10] config byte-identical (no BRIEF_TIME_* written)" '[ "$(cksum < "$R/config/agent.conf")" = "$CONF_BEFORE" ]'
check "[10] staged plists byte-identical" '[ "$(snap10)" = "$STAGED_BEFORE" ]'
check "[10] all three slots named as unset" 'grep -q "NO TIME for: morning retro weekly" "$R/err"'
check "[10] launchd never touched" '! grep -Eq "^(bootout|bootstrap|load)" "$R/lc.log"'
check "[10] summary claims no time" 'grep -q "morning=(unset) retro=(unset) weekly=(unset)" "$R/out"'

# --- [11] kit-style explicit args, no launchd --------------------------------
echo "[11] kit-style explicit args (no loaded unit, e.g. Linux)"
R="$(mkinst kit)"
run "$R" --morning 06:00 --retro 22:00 --weekly "Sun 20:00"
check "[11] exit 4 (no emitter), not a failure" '[ "$(rc "$R")" = 4 ]'
check "[11] kit times written verbatim" \
  'grep -qx "BRIEF_TIME_MORNING=06:00" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_RETRO=22:00" "$R/config/agent.conf" && grep -qx "BRIEF_TIME_WEEKLY=Sun 20:00" "$R/config/agent.conf"'
check "[11] no slot reported unset" '! grep -q "NO TIME" "$R/err"'
check "[11] dead slots named" 'grep -q "NO EMITTER on this instance for: morning retro weekly" "$R/err"'
check "[11] staged morning copy synced to the kit time 06:00" '[ "$(sci "$R/routines/$P.generic-brief-morning.plist")" = "06:00" ]'

echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ]
