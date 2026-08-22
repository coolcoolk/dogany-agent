#!/bin/bash
# dgn965_install_plist_pick_selftest.sh -- DGN-965 self-test.
#
# DGN-965 is the DGN-964 defect class surviving at a second call site:
# install.sh's install_launchd() picked the FIRST *.plist glob match with no
# identity check, so a legacy plist from a renamed/reinstalled agent (which
# can sort before the current one) got installed instead of the real bridge
# plist. DGN-964 fixed the identical bug in watchdog_setup.sh with
# expected_bridge_label() + pick_plist() (Label match preferred, WARN on
# multiple candidates, sorted-first fallback, never a silent guess); this
# ticket ports the same two functions into install.sh and wires them into
# install_launchd(), excluding *.watchdog.plist the same way
# write_service_marker() does on the live-instance side.
#
# Covers:
#   [1] pick_plist + expected_bridge_label -- direct unit coverage (pure
#       functions: single candidate, Label match among multiple w/ a decoy
#       that sorts first, no-Label-match fallback, unknown-identity
#       fallback, zero candidates, warning routed to stderr only).
#   [2] install_launchd -- end-to-end wiring: legacy-decoy pick, no-match
#       sorted fallback, *.watchdog.plist exclusion, zero-candidate no-op.
#
# install.sh supports DOGANY_INSTALL_LIB=1 sourcing (its own tail guard:
# "the test harness sources this file for its functions ... without running
# the wizard") -- this test exercises the REAL shipped functions, not a
# re-typed copy (same technique as tests/rehearsal_dgn227.sh).
#
# SAFETY: every install_launchd call in [2] runs with HOME redirected to a
# throwaway mktemp dir AND launchctl shadowed by a no-op shell function
# defined before install.sh is sourced -- the real host launchd is NEVER
# touched, no plist is ever bootstrapped for real.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn965-plist.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }
assert_eq() { # assert_eq <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2] got [$3])"; fi
}
assert_contains() { # assert_contains <desc> <needle> <haystack>
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1 (missing [$2] in [$3])" ;; esac
}

write_plist() { # write_plist <path> <label>
  cat > "$1" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$2</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/echo</string>
    </array>
</dict>
</plist>
PLIST
}

# ===========================================================================
# [1] pick_plist + expected_bridge_label -- direct unit coverage
# ===========================================================================
CURRENT="pick_plist+expected_bridge_label"
say "[1] pick_plist + expected_bridge_label (direct)"

UDIR="$WORK/unit"
mkdir -p "$UDIR/no-instance" "$UDIR/named" "$UDIR/placeholder"
printf 'DOGANY_AGENT_NAME=skull\n' > "$UDIR/named/.instance.conf"
printf 'DOGANY_AGENT_NAME=__NAME__\n' > "$UDIR/placeholder/.instance.conf"

SOLO="$UDIR/a.plist"
write_plist "$SOLO" "com.telegram-skill-bot.solo"
# Decoy filename ("aaa-legacy") deliberately sorts BEFORE the current one
# ("zzz-current") -- this is the exact ordering that misled the pre-fix
# first-glob pick (DGN-964 Skull incident: dogany-smith.* sorted before
# skull.*).
DECOY="$UDIR/aaa-legacy.plist"
write_plist "$DECOY" "com.telegram-skill-bot.dogany-smith"
CURRENTP="$UDIR/zzz-current.plist"
write_plist "$CURRENTP" "com.telegram-skill-bot.skull"

# Each captured value/warning goes to its OWN file (not a shared "KEY=[...]"
# line format) -- pick_plist's warnings are multi-line, and a line-oriented
# mini-format would silently truncate them. Read the files back verbatim.
(
  export DOGANY_INSTALL_LIB=1
  export DOGANY_INSTALL_PINNED=1
  cd "$SANDBOX" || exit 1
  set --
  # shellcheck disable=SC1091
  source "$SANDBOX/install.sh"
  trap - ERR
  DOGANY_LANG=en

  INSTALL_ROOT="$UDIR/no-instance"
  expected_bridge_label > "$WORK/ebl_absent.val"

  INSTALL_ROOT="$UDIR/named"
  expected_bridge_label > "$WORK/ebl_named.val"

  INSTALL_ROOT="$UDIR/placeholder"
  expected_bridge_label > "$WORK/ebl_placeholder.val"

  pick_plist "" "$SOLO" > "$WORK/pick_single.val" 2>"$WORK/pick_single.err"

  pick_plist "com.telegram-skill-bot.skull" "$DECOY" "$CURRENTP" \
    > "$WORK/pick_labelmatch.val" 2>"$WORK/pick_labelmatch.err"

  pick_plist "com.telegram-skill-bot.no-such-agent" "$DECOY" "$CURRENTP" \
    > "$WORK/pick_nomatch.val" 2>"$WORK/pick_nomatch.err"

  pick_plist "" "$DECOY" "$CURRENTP" \
    > "$WORK/pick_unknown.val" 2>"$WORK/pick_unknown.err"

  pick_plist "com.telegram-skill-bot.skull" \
    > "$WORK/pick_zero.val" 2>"$WORK/pick_zero.err"
) || bad "unit subshell crashed (see $WORK/*.val / *.err)"

val() { cat "$WORK/$1.val" 2>/dev/null; }
werr() { cat "$WORK/$1.err" 2>/dev/null; }
werr_bytes() { wc -c < "$WORK/$1.err" 2>/dev/null | tr -d ' '; }

assert_eq "expected_bridge_label: no .instance.conf -> empty" \
  "" "$(val ebl_absent)"
assert_eq "expected_bridge_label: real identity resolves" \
  "com.telegram-skill-bot.skull" "$(val ebl_named)"
assert_eq "expected_bridge_label: mint-placeholder name -> empty" \
  "" "$(val ebl_placeholder)"

assert_eq "pick_plist: single candidate returned as-is" \
  "$SOLO" "$(val pick_single)"
assert_eq "pick_plist: single candidate -> no warning emitted" \
  "0" "$(werr_bytes pick_single)"

assert_eq "pick_plist: multiple + Label match -> picks the MATCHING one, not alphabetical-first decoy" \
  "$CURRENTP" "$(val pick_labelmatch)"
assert_contains "pick_plist: Label-match path warns (never silent)" \
  "multiple candidate" "$(werr pick_labelmatch)"
assert_contains "pick_plist: Label-match path names the winner" \
  "zzz-current.plist" "$(werr pick_labelmatch)"

assert_eq "pick_plist: multiple + no Label matches -> sorted-first fallback (decoy, matches glob order)" \
  "$DECOY" "$(val pick_nomatch)"
assert_contains "pick_plist: no-match fallback warns naming the fallback winner" \
  "aaa-legacy.plist" "$(werr pick_nomatch)"

assert_eq "pick_plist: multiple + unknown identity ('') -> sorted-first fallback" \
  "$DECOY" "$(val pick_unknown)"
assert_contains "pick_plist: unknown-identity fallback warns identity-unknown" \
  "identity unknown" "$(werr pick_unknown)"

assert_eq "pick_plist: zero candidates -> empty (no-op, never a guess)" \
  "" "$(val pick_zero)"
assert_eq "pick_plist: zero candidates -> no warning emitted" \
  "0" "$(werr_bytes pick_zero)"

# ===========================================================================
# [2] install_launchd -- end-to-end wiring (HOME-redirected, launchctl-stubbed)
# ===========================================================================
CURRENT="install_launchd"
say "[2] install_launchd (end-to-end, real code, sandboxed HOME)"

run_install_launchd() { # run_install_launchd <inst_root> <fakehome> <logfile>
  local inst="$1" fakehome="$2" log="$3"
  mkdir -p "$fakehome"
  (
    # Shadow launchctl with a no-op BEFORE install.sh is sourced -- function
    # definitions take priority over PATH lookup, so every launchctl call
    # inside the real install_launchd resolves here, never to the real
    # binary. Nothing this test does can register/unregister a real launchd
    # job.
    launchctl() { return 1; }
    export -f launchctl 2>/dev/null || true

    export HOME="$fakehome"
    export DOGANY_INSTALL_LIB=1
    export DOGANY_INSTALL_PINNED=1
    cd "$SANDBOX" || exit 1
    set --
    # shellcheck disable=SC1091
    source "$SANDBOX/install.sh"
    trap - ERR
    DOGANY_LANG=en
    DRY_RUN=0
    INSTALL_ROOT="$inst"
    install_launchd "manual fallback command"
  ) >"$log" 2>&1
}

# --- 2a: legacy decoy (sorts first) + current bridge plist (Label match) +
#         watchdog plist -- must pick the CURRENT one, not the decoy, and
#         must never pick the watchdog plist.
INST_A="$WORK/inst-a"
mkdir -p "$INST_A/bridge" "$INST_A"
printf 'DOGANY_AGENT_NAME=skull\n' > "$INST_A/.instance.conf"
write_plist "$INST_A/bridge/aaa-dogany-smith.newbridge.plist" "com.telegram-skill-bot.dogany-smith"
write_plist "$INST_A/bridge/zzz-skull.newbridge.plist"        "com.telegram-skill-bot.skull"
write_plist "$INST_A/bridge/zzz-skull.watchdog.plist"         "com.telegram-skill-bot.skull.watchdog"
HOME_A="$WORK/home-a"
run_install_launchd "$INST_A" "$HOME_A" "$WORK/log-a.txt"
INSTALLED_A="$(ls "$HOME_A/Library/LaunchAgents" 2>/dev/null)"
assert_eq "2a: legacy-decoy scenario installs the CURRENT plist filename" \
  "zzz-skull.newbridge.plist" "$INSTALLED_A"
if [ -f "$HOME_A/Library/LaunchAgents/zzz-skull.newbridge.plist" ]; then
  LABEL_A="$(sed -n 's#.*<string>\(.*\)</string>.*#\1#p' "$HOME_A/Library/LaunchAgents/zzz-skull.newbridge.plist" | head -1)"
  assert_eq "2a: installed plist carries the CURRENT Label, not the legacy one" \
    "com.telegram-skill-bot.skull" "$LABEL_A"
else
  bad "2a: nothing installed under fake HOME/Library/LaunchAgents (see $WORK/log-a.txt)"
fi
assert_contains "2a: multi-candidate WARN present in output (never silent)" \
  "multiple candidate" "$(cat "$WORK/log-a.txt")"

# --- 2b: two candidates, NEITHER Label matches instance identity -> sorted-
#         first fallback, warned.
INST_B="$WORK/inst-b"
mkdir -p "$INST_B/bridge"
printf 'DOGANY_AGENT_NAME=nomatch-agent\n' > "$INST_B/.instance.conf"
write_plist "$INST_B/bridge/aaa-first.newbridge.plist"  "com.telegram-skill-bot.alpha"
write_plist "$INST_B/bridge/zzz-second.newbridge.plist" "com.telegram-skill-bot.beta"
HOME_B="$WORK/home-b"
run_install_launchd "$INST_B" "$HOME_B" "$WORK/log-b.txt"
INSTALLED_B="$(ls "$HOME_B/Library/LaunchAgents" 2>/dev/null)"
assert_eq "2b: no-Label-match -> sorted-first candidate installed" \
  "aaa-first.newbridge.plist" "$INSTALLED_B"
assert_contains "2b: no-Label-match path warns naming the fallback" \
  "aaa-first.newbridge.plist" "$(cat "$WORK/log-b.txt")"

# --- 2c: zero non-watchdog candidates (only a watchdog plist survives) ->
#         the existing "plist not found, run manually" no-op path fires;
#         nothing is copied, no crash.
INST_C="$WORK/inst-c"
mkdir -p "$INST_C/bridge"
printf 'DOGANY_AGENT_NAME=orphan\n' > "$INST_C/.instance.conf"
write_plist "$INST_C/bridge/orphan.watchdog.plist" "com.telegram-skill-bot.orphan.watchdog"
HOME_C="$WORK/home-c"
run_install_launchd "$INST_C" "$HOME_C" "$WORK/log-c.txt"
if [ -d "$HOME_C/Library/LaunchAgents" ] && [ -n "$(ls -A "$HOME_C/Library/LaunchAgents" 2>/dev/null)" ]; then
  bad "2c: zero-candidate scenario still installed something (silent guess): $(ls "$HOME_C/Library/LaunchAgents")"
else
  ok "2c: zero non-watchdog candidates -> nothing installed (no silent guess)"
fi
assert_contains "2c: zero-candidate path reports 'not found' (loud no-op, not a silent success claim)" \
  "not found" "$(cat "$WORK/log-c.txt")"
assert_contains "2c: zero-candidate path still prints the manual fallback command" \
  "manual fallback command" "$(cat "$WORK/log-c.txt")"

# ---------------------------------------------------------------------------
say ""
say "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
