#!/usr/bin/env bash
# test-lane: fast    # measured 1.7s (2026-09-04 full sweep, DGN-1267)
# test-host: macos sandbox-exec, /private/tmp, md5
# routines/tests/test-dispatch-observe.sh -- model-free tests for the DGN-1191
# observe-mode sandbox in dispatch-detached.sh. Renders the real profile for
# a throwaway WORKDIR under /private/tmp (the target-under-/tmp case that
# broke the first lab profile) and asserts, with the filesystem as judge:
# writes into targets / ~/.claude config / other sessions' state -> EPERM;
# TMPDIR scratch, sqlite3 -readonly queries, python3 reads, --dry-run
# scripts -> work; sandbox-exec absent -> fail-closed; worktree-mode tool
# policy unchanged. No model call, no live instance touched, nothing under
# .telegram_bot/. Exit 0 = all pass.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
DISPATCH_TEST=1 source "$REPO/routines/dispatch-detached.sh"
set +e   # the sourced script turns on -e; every probe below is EXPECTED to fail

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got: $2, want: $3)"; fi; }

# --- fixtures ---------------------------------------------------------------
LAB="$(mktemp -d /private/tmp/dgn1191-test.XXXXXX)"
WORKDIR="$LAB/repo"; mkdir -p "$WORKDIR" "$LAB/rundir"
git -C "$WORKDIR" init -q
echo readme > "$WORKDIR/README"
printf '#!/bin/bash\n[ "$1" = --dry-run ] || exit 2\nS=$(mktemp -d)\necho staged > "$S/f"\necho "dry-run ok: $S"\n' > "$WORKDIR/dry.sh"
chmod +x "$WORKDIR/dry.sh"
sqlite3 "$WORKDIR/t.db" 'create table t(x); insert into t values(1),(2);'
git -C "$WORKDIR" add -A >/dev/null
git -C "$WORKDIR" -c user.name=t -c user.email=t@t commit -qm init
# A second target under /tmp that does not exist yet (WORKTREE_BASE style).
WORKTREE_BASE="$LAB/wt-base"
SID="$(uuidgen | tr 'A-Z' 'a-z')"
PROF="$LAB/rundir/observe.sb"
OUTSIDE="$HOME/.dgn1191-canary-$$"       # outside every allow: default deny
SETTINGS_MD5="$(md5 -q "$HOME/.claude/settings.json" 2>/dev/null || echo none)"
SNAP="$(ls -t "$HOME/.claude/shell-snapshots" 2>/dev/null | head -1)"
SNAP_MD5=""; [[ -n "$SNAP" ]] && SNAP_MD5="$(md5 -q "$HOME/.claude/shell-snapshots/$SNAP")"
OTHER_ENV="$(ls "$HOME/.claude/session-env" 2>/dev/null | head -1)"

cleanup() { rm -rf "$LAB" "$OUTSIDE"; }
trap cleanup EXIT

sb() { sandbox-exec -f "$PROF" /bin/sh -c "cd '$WORKDIR' && $1" 2>&1; }

# --- render + self-check ----------------------------------------------------
render_observe_profile "$PROF" "$WORKDIR" "$SID"; check "T0a render" "$?" "0"
observe_profile_selfcheck "$PROF" "$WORKDIR" "$WORKSPACE" >/dev/null 2>&1; check "T0b self-check passes on a sound profile" "$?" "0"
tail -3 "$PROF" | grep -q 'subpath "'"$WORKDIR"'"' ; check "T0c WORKDIR deny is in the LAST rule group" "$?" "0"

# --- writes into the target (which lives under /tmp = the allowed scratch) --
sb 'touch x' >/dev/null;                                  check "T1 touch WORKDIR/x -> EPERM" "$([[ -e $WORKDIR/x ]] && echo written || echo denied)" "denied"
sb 'git tag t1' >/dev/null;                               check "T6 git tag -> EPERM (no ref)" "$(git -C "$WORKDIR" tag --list | wc -l | tr -d ' ')" "0"
sb "git log -1 --output=$WORKDIR/gitout" >/dev/null;      check "T7a git log --output into WORKDIR -> EPERM" "$([[ -e $WORKDIR/gitout ]] && echo written || echo denied)" "denied"
sb "git log -1 --output=$OUTSIDE" >/dev/null;             check "T7b git log --output outside every allow -> EPERM" "$([[ -e $OUTSIDE ]] && echo written || echo denied)" "denied"
sb "python3 -c \"open('py','w').write('x')\"" >/dev/null;  check "T5 python3 open('w') -> EPERM" "$([[ -e $WORKDIR/py ]] && echo written || echo denied)" "denied"
sb "sqlite3 -readonly t.db \"select writefile('sq','x')\"" >/dev/null; check "T4 sqlite3 writefile() -> no file" "$([[ -e $WORKDIR/sq ]] && echo written || echo denied)" "denied"
sb "sqlite3 -readonly t.db '.shell touch sh'" >/dev/null;  check "T14 sqlite3 .shell touch -> no file" "$([[ -e $WORKDIR/sh ]] && echo written || echo denied)" "denied"
sb "ln README /private/tmp/dgn1191-link-$$" >/dev/null;   check "T8 hardlink target file into /tmp -> EPERM" "$([[ -e /private/tmp/dgn1191-link-$$ ]] && echo written || echo denied)" "denied"
sb "mv '$WORKDIR' '$LAB/moved'" >/dev/null;               check "T9 rename target dir -> EPERM" "$([[ -d $LAB/moved ]] && echo written || echo denied)" "denied"
sb "mkdir -p '$WORKTREE_BASE/canary'" >/dev/null;         check "T12 non-existent /tmp target (WORKTREE_BASE) -> EPERM" "$([[ -d $WORKTREE_BASE/canary ]] && echo written || echo denied)" "denied"
sb "touch '$WORKSPACE/.dgn1191-canary'" >/dev/null;        check "T12b WORKSPACE (non-/tmp target) -> EPERM" "$([[ -e $WORKSPACE/.dgn1191-canary ]] && echo written || echo denied)" "denied"
rm -f "$WORKSPACE/.dgn1191-canary"

# --- ~/.claude: config / hooks / other sessions' state ----------------------
sb "echo '# probe' >> '$HOME/.claude/settings.json'" >/dev/null; check "T10 ~/.claude/settings.json append -> EPERM" "$(md5 -q "$HOME/.claude/settings.json" 2>/dev/null || echo none)" "$SETTINGS_MD5"
sb "mkdir '$HOME/.claude/hooks'" >/dev/null;               check "T10b ~/.claude/hooks mkdir -> EPERM" "$([[ -d $HOME/.claude/hooks ]] && echo written || echo denied)" "denied"
sb "echo x >> '$HOME/.claude.json'" >/dev/null;            check "T10c ~/.claude.json append -> EPERM" "$(sb "echo x >> '$HOME/.claude.json'" | grep -c 'Operation not permitted')" "1"
sb "cp '$HOME/.claude.json' '$HOME/.claude.json.backup.probe'" >/dev/null; check "T10d ~/.claude.json.backup.* create -> EPERM" "$([[ -e $HOME/.claude.json.backup.probe ]] && echo written || echo denied)" "denied"
if [[ -n "$SNAP" ]]; then
  sb "echo '# probe' >> '$HOME/.claude/shell-snapshots/$SNAP'" >/dev/null; check "T16a another session's shell snapshot -> EPERM" "$(md5 -q "$HOME/.claude/shell-snapshots/$SNAP")" "$SNAP_MD5"
fi
sb "touch '$HOME/.claude/shell-snapshots/snapshot-zsh-9999999999999-probe.sh'" >/dev/null; check "T16b new shell snapshot -> EPERM" "$([[ -e $HOME/.claude/shell-snapshots/snapshot-zsh-9999999999999-probe.sh ]] && echo written || echo denied)" "denied"
if [[ -n "$OTHER_ENV" ]]; then
  sb "touch '$HOME/.claude/session-env/$OTHER_ENV/probe'" >/dev/null; check "T16c another session's env dir -> EPERM" "$([[ -e $HOME/.claude/session-env/$OTHER_ENV/probe ]] && echo written || echo denied)" "denied"
fi
sb "mkdir -p '$HOME/.claude/session-env/$SID' && touch '$HOME/.claude/session-env/$SID/ok'" >/dev/null; check "T16d own session env dir -> allowed" "$([[ -e $HOME/.claude/session-env/$SID/ok ]] && echo written || echo denied)" "written"
rm -rf "$HOME/.claude/session-env/$SID"
SLUG="$(sb_project_slug "$WORKDIR")"
sb "mkdir -p '$HOME/.claude/projects/$SLUG' && touch '$HOME/.claude/projects/$SLUG/$SID.jsonl'" >/dev/null; check "T16e own transcript -> allowed" "$([[ -e $HOME/.claude/projects/$SLUG/$SID.jsonl ]] && echo written || echo denied)" "written"
sb "touch '$HOME/.claude/projects/$SLUG/other.jsonl'" >/dev/null; check "T16f other transcript in same project -> EPERM" "$([[ -e $HOME/.claude/projects/$SLUG/other.jsonl ]] && echo written || echo denied)" "denied"
rm -rf "$HOME/.claude/projects/$SLUG"

# --- observation must keep working ------------------------------------------
check "T2 mktemp (TMPDIR) -> works" "$(sb 'd=$(mktemp -d) && rmdir "$d" && echo ok')" "ok"
check "T3 sqlite3 -readonly query -> works" "$(sb "sqlite3 -readonly t.db 'select count(*) from t'")" "2"
check "T3b python3 read -> works" "$(sb "python3 -c \"print(open('README').read().strip())\"")" "readme"
check "T15 --dry-run script writing to mktemp -> works" "$(sb './dry.sh --dry-run' | cut -d: -f1)" "dry-run ok"
check "T15b git -C / cd / git log -> works" "$(sb "git log --oneline | wc -l | tr -d ' '")" "1"
check "T15c ssh material unreadable (push cut)" "$(sb "cat '$HOME/.ssh/known_hosts' >/dev/null 2>&1; echo \$?" | tail -1)" "1"

# --- inherited fd: the wrapper's out.log is the only output channel ---------
sandbox-exec -f "$PROF" /bin/sh -c 'echo via-inherited-fd' > "$LAB/rundir/out.log" 2>&1
check "T11 inherited stdout fd still records out.log" "$(cat "$LAB/rundir/out.log")" "via-inherited-fd"

# --- self-check catches a render defect --------------------------------------
sed '/TARGETS LAST/,$d' "$PROF" > "$LAB/rundir/broken.sb"
observe_profile_selfcheck "$LAB/rundir/broken.sb" "$WORKDIR" >/dev/null 2>&1; check "T17 self-check refuses a profile that leaves WORKDIR open" "$?" "1"
echo '(version 1' > "$LAB/rundir/syntax.sb"
observe_profile_selfcheck "$LAB/rundir/syntax.sb" "$WORKDIR" >/dev/null 2>&1; check "T17b self-check refuses a profile that does not compile" "$?" "1"
render_observe_profile "$LAB/rundir/q.sb" "$LAB/bad\"quote" "$SID" 2>/dev/null; check "T17c render refuses an unquotable path" "$?" "1"

# --- fail-closed when sandbox-exec is absent ---------------------------------
( PATH=/nonexistent; observe_sandbox_available 2>/dev/null ); check "T13 sandbox-exec absent -> gate refuses" "$?" "1"
( PATH=/nonexistent; WT_PATH=""; SANDBOX_PROFILE="$PROF"; SESSION_ID="$SID"; select_tool_policy 2>/dev/null ); check "T13b worker refuses to run observe mode without sandbox-exec" "$?" "1"
( WT_PATH=""; SANDBOX_PROFILE=""; SESSION_ID=""; select_tool_policy 2>/dev/null ); check "T13c worker refuses observe mode without a rendered profile" "$?" "1"

# --- DGN-1458 plan lane: one writable plan dir, live target denied ----------
LIVE_T="$LAB/live"; mkdir -p "$LIVE_T" "$LAB/rundir/plan"
PLAN_DIR="$LAB/rundir/plan"; LIVE_TARGETS="$LIVE_T"
render_observe_profile "$LAB/rundir/plan.sb" "$WORKDIR" "$SID"; check "T19a plan-lane render" "$?" "0"
PLAN_DIR=""; LIVE_TARGETS=""
sandbox-exec -f "$LAB/rundir/plan.sb" /bin/sh -c "echo '{}' > '$LAB/rundir/plan/plan.json'" 2>/dev/null
check "T19b planner writes RUNDIR/plan/plan.json" "$([[ -s $LAB/rundir/plan/plan.json ]] && echo written || echo denied)" "written"
sandbox-exec -f "$LAB/rundir/plan.sb" /bin/sh -c "touch '$LIVE_T/x'" 2>/dev/null
check "T19c planner cannot write the live target (under /tmp)" "$([[ -e $LIVE_T/x ]] && echo written || echo denied)" "denied"

# --- worktree mode: tool policy untouched, write profile wraps (DGN-1504) ---
WT_PATH="$LAB/wt"; SANDBOX_PROFILE=""; SESSION_ID=""; WRITE_PROFILE="$PROF"; select_tool_policy
check "T18a worktree mode: write-profile sandbox wrapper (DGN-1504)" "${WRAP[*]}" "sandbox-exec -f $PROF"
check "T18b worktree mode: tool args unchanged" "${TOOL_ARGS[*]}" "--permission-mode bypassPermissions --disallowedTools Task,Agent"
WT_PATH=""; SANDBOX_PROFILE="$PROF"; SESSION_ID="$SID"; select_tool_policy
check "T18c observe mode: sandbox wrapper" "${WRAP[*]}" "sandbox-exec -f $PROF"
check "T18d observe mode: tool args" "${TOOL_ARGS[*]}" "--permission-mode default --allowedTools Bash,WebSearch,WebFetch --disallowedTools Task,Agent,Write,Edit,NotebookEdit --session-id $SID"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
