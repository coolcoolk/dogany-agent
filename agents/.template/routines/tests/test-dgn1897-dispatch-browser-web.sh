#!/usr/bin/env bash
# test-lane: fast
# routines/tests/test-dgn1897-dispatch-browser-web.sh -- model-free tests for
# DGN-1897: a read-only (observe) dispatch must be able to do web/visual work.
# Before the fix the worker (a) had only Bash allowed under
# --permission-mode default, so WebSearch/WebFetch were denied in -p mode, and
# (b) agent-browser bound its daemon socket in ~/.agent-browser (no
# AGENT_BROWSER_SOCKET_DIR / XDG_RUNTIME_DIR) -- a HOME write the observe
# profile denies -- and Chrome's own seatbelt cannot apply inside the outer
# dispatch seatbelt (nested sandbox_apply is EPERM; measured: "Auto-launch
# failed: CDP response channel closed" without --no-sandbox, page loads with
# it). Profiles are judged by the real kernel where it applies one, else by
# fixtures/sandbox_exec_model.py. No model call, no browser launched, no live
# instance touched. Exit 0 = all pass.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
DISPATCH_TEST=1 source "$REPO/routines/dispatch-detached.sh"
set +e

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got: $2, want: $3)"; fi; }

SBMODEL="$HARNESS_DIR/fixtures/sandbox_exec_model.py"
LAB="$(mktemp -d /private/tmp/dgn1897-test.XXXXXX)"
WORKDIR="$LAB/repo"; mkdir -p "$WORKDIR" "$LAB/rundir" "$LAB/bin"
WORKTREE_BASE="$LAB/wt-base"
SID="$(uuidgen | tr 'A-Z' 'a-z')"
PROF="$LAB/rundir/observe.sb"
RUNID="dsp-20261006-000000-$(printf '%04d' $(( $$ % 10000 )))"
cleanup() { rm -rf "$LAB" "/tmp/dogany-ab-$RUNID"; }
trap cleanup EXIT

render_observe_profile "$PROF" "$WORKDIR" "$SID"; check "T0 observe profile renders" "$?" "0"
verdict() { python3 "$SBMODEL" verdict "$PROF" file-write* "$1"; }

# --- (b) cause, pinned: the agent-browser default socket dir is closed --------
check "T1 cause: ~/.agent-browser (default socket dir) is NOT writable in observe" \
  "$(verdict "$HOME/.agent-browser/default.sock")" "deny"

# --- worker browser env: per-run socket dir the profile leaves open -----------
worker_browser_env_args
ABDIR="$(worker_browser_socket_dir)"
check "T2 socket dir is per-run under /tmp" "$ABDIR" "/tmp/dogany-ab-$RUNID"
check "T3 socket dir created by the worker shell" "$([[ -d $ABDIR ]] && echo yes || echo no)" "yes"
check "T4 socket dir writable under the observe profile (model)" "$(verdict "$ABDIR/default.sock")" "allow"
check "T4b socket dir writable under the observe profile, /private spelling" "$(verdict "/private$ABDIR/default.sock")" "allow"
# AF_UNIX sun_path is 104 bytes on macOS; a long session name must still fit.
check "T5 socket path for a 40-char session fits sun_path" \
  "$(( ${#ABDIR} + 1 + 40 + 5 < 104 ))" "1"
check "T6 env carries the socket dir" \
  "$(printf '%s\n' "${WORKER_BROWSER_ENV[@]}" | grep -c "^AGENT_BROWSER_SOCKET_DIR=$ABDIR\$")" "1"
check "T7 env disables Chrome's own (nested) sandbox" \
  "$(printf '%s\n' "${WORKER_BROWSER_ENV[@]}" | grep -c '^AGENT_BROWSER_ARGS=.*--no-sandbox')" "1"
check "T7b an owner-set AGENT_BROWSER_ARGS is kept, not replaced" \
  "$(AGENT_BROWSER_ARGS='--lang=ko' worker_browser_env_args; printf '%s\n' "${WORKER_BROWSER_ENV[@]}" | grep -c '^AGENT_BROWSER_ARGS=--lang=ko,--no-sandbox$')" "1"
worker_browser_env_args

# Real kernel, where it applies a profile (skipped inside a seatbelt: nested
# sandbox_apply is EPERM and every verdict would be vacuous).
if sandbox-exec -p '(version 1)(allow default)' /usr/bin/true 2>/dev/null; then
  sandbox-exec -f "$PROF" /bin/sh -c "touch '$ABDIR/k.sock'" 2>/dev/null
  check "T8 kernel: socket dir writable in observe" "$([[ -e $ABDIR/k.sock ]] && echo written || echo denied)" "written"
  sandbox-exec -f "$PROF" /bin/sh -c "touch '$HOME/.agent-browser/.dgn1897-probe-$$'" 2>/dev/null
  check "T9 kernel: ~/.agent-browser stays closed" "$([[ -e $HOME/.agent-browser/.dgn1897-probe-$$ ]] && echo written || echo denied)" "denied"
  rm -f "$HOME/.agent-browser/.dgn1897-probe-$$"
else
  echo "SKIP T8/T9 kernel blocks (sandbox_apply refused here: nested seatbelt)"
fi

# --- both spawn sites pass the env --------------------------------------------
check "T10 claude-code + codex spawn sites carry WORKER_BROWSER_ENV" \
  "$(grep -c 'WORKER_BROWSER_ENV\[@\]+"\${WORKER_BROWSER_ENV\[@\]}"' "$REPO/routines/dispatch-detached.sh")" "2"

# --- teardown: daemons closed, dir removed, nothing else touched --------------
printf '#!/bin/sh\necho "$AGENT_BROWSER_SOCKET_DIR $*" >> "%s"\n' "$LAB/ab.calls" > "$LAB/bin/agent-browser"
chmod +x "$LAB/bin/agent-browser"
[[ -d $ABDIR ]] && touch "$ABDIR/default.sock"
PATH="$LAB/bin:$PATH" worker_browser_teardown
check "T11 teardown closes every daemon of THIS run's socket dir" "$(cat "$LAB/ab.calls" 2>/dev/null)" "$ABDIR close --all"
check "T12 teardown removes the socket dir" "$([[ -e $ABDIR ]] && echo left || echo gone)" "gone"
: > "$LAB/ab.calls"
PATH="$LAB/bin:$PATH" worker_browser_teardown
check "T13 teardown without a socket dir is a no-op" "$(cat "$LAB/ab.calls")" ""
grep -q 'worker_browser_teardown' <(declare -f finalize_run); check "T14 finalize_run tears the browser down" "$?" "0"

# --- (a) tool policy: web tools allowed in observe, writers still denied ------
WT_PATH=""; SANDBOX_PROFILE="$PROF"; SESSION_ID="$SID"
PATH="$LAB/bin:$PATH"
printf '#!/bin/sh\nexit 0\n' > "$LAB/bin/sandbox-exec"; chmod +x "$LAB/bin/sandbox-exec"; hash -r
select_tool_policy
check "T15 observe: WebSearch + WebFetch allowed next to Bash" "${TOOL_ARGS[*]}" \
  "--permission-mode default --allowedTools Bash,WebSearch,WebFetch --disallowedTools Task,Agent,Write,Edit,NotebookEdit --session-id $SID"

# --- the worker is told what it can use ---------------------------------------
B="$(worker_backstop)"
grep -q 'agent-browser' <<<"$B"; check "T16 backstop names agent-browser as available" "$?" "0"
grep -q 'WebSearch' <<<"$B"; check "T17 backstop names WebSearch/WebFetch as available" "$?" "0"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
