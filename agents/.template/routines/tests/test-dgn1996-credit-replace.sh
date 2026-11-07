#!/usr/bin/env bash
# test-lane: slow
unset DOGANY_BRIDGE_SESSION  # DGN-1688: hermetic -- never look like a self-cancel
# test-dgn1996-credit-replace.sh -- DGN-1996: a claude-api worker killed by
# the provider's credit-exhaustion verdict (billing_error / "credit balance
# is too low") is re-placed by the machine ONCE through normal selection (the
# exhausted marker vetoes claude-api), with the SAME brief and reason on the
# ledger row; a run with commits is retained and reported instead. The return says
# "re-placed on <runtime>" and asks nobody to choose.
#
# Evidence: 2026-10-11 dsp-20261011-122835-1507 (claude-api) hit billing_error,
# exhausted.json was written, status.json result=error, nothing re-placed it,
# and the agent put a top-up / wait / subscription menu to the owner.
#
# Run: bash routines/tests/test-dgn1996-credit-replace.sh
# Safe/offline: fake claude via PATH, push/usage-gate stubs, fake API key
# file (no secret store, no keychain), temp HOME + DOGANY_SHARED_HOME.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1996-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$WS/config" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
bash "$HARNESS_DIR/fixtures/install-dispatch-policy.sh" "$WS"
cp "$REPO/routines/lib/dispatch_storage.py" "$REPO/routines/lib/conf_reader.py" "$WS/routines/lib/"
# No secret_store.py / book_identity.py: the key comes from the file below
# only, and no credential of this host is ever read.
for f in ledger-update.sh dispatch-tier-bindings.tsv dispatch-effort-bindings.tsv \
         model_display.py run_block.py i18n_lookup.py dispatch-model-display.tsv api_credit_pool.py reset_passes.py \
         runtime_headroom.py host_dispatcher.py conf_reader.py; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
mkdir -p "$WS/config" && cp -R "$REPO/config/i18n" "$WS/config/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:?}"
exit 0
EOF
# Fake claude. With ANTHROPIC_API_KEY in env (= the claude-api lane):
#   FAKE_API=billing  die before producing a commit
#   FAKE_API=billing_committed  commit partial work, then die
#   FAKE_API=other    a non-credit API error
# Without the key (subscription lane): record what it was given, finish.
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
prompt=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[$i]}" == -p ]] && prompt="${args[$((i+1))]}"
done
lane=sub; [[ -n "${ANTHROPIC_API_KEY:-}" ]] && lane=api
echo "$lane" >> "${FAKE_CALLS:?}"
if [[ $lane == api ]]; then
  case "${FAKE_API:-}" in
    billing|billing_committed)
      if [[ "${FAKE_API:-}" == billing_committed ]]; then
      echo partial > partial.txt
      git add partial.txt >/dev/null 2>&1 && git commit -qm "partial work before the credit died" >/dev/null 2>&1
      fi
      echo '{"type":"assistant","message":{"model":"claude-x","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"git commit"}}]}}'
      echo '{"type":"assistant","message":{"model":"<synthetic>","content":[{"type":"text","text":"Credit balance is too low"}]},"error":"billing_error","isApiErrorMessage":true}'
      echo '{"type":"result","subtype":"success","is_error":true,"result":"Credit balance is too low","total_cost_usd":0.12}'
      exit 1 ;;
    other)
      echo '{"type":"result","subtype":"success","is_error":true,"result":"API Error: 500 internal","total_cost_usd":0.01}'
      exit 1 ;;
  esac
fi
{
  printf 'branch=%s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  printf 'partial=%s\n' "$([[ -f partial.txt ]] && echo yes || echo no)"
  printf 'prompt=%s\n' "$prompt"
} > "${FAKE_SEEN:?}"
echo '{"type":"assistant","message":{"model":"claude-opus-fake","content":[{"type":"text","text":"WORK-DONE on the subscription"}]}}'
exit 0
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
echo "PLAN=max_5x" > "$WS/config/agent.conf"
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export PUSH_STUB_LOG="$TDIR/push-calls.log"
export HOME="$TDIR/home"
export DOGANY_SHARED_HOME="$TDIR/shared"
export DOGANY_DISPATCH_MACHINE="1 8 32000 0"
( umask 077; printf 'sk-ant-dgn1996-NOTREAL\n' > "$TDIR/key" )
export ANTHROPIC_API_KEY_FILE="$TDIR/key"
mkdir -p "$HOME"
# The balance-probe behavior has its own fake-CLI unit coverage. This
# worker/re-place fixture begins with a fresh successful probe cache.
KEY_FP="$(python3 -c 'import hashlib; print(hashlib.sha256(b"sk-ant-dgn1996-NOTREAL").hexdigest()[:12])')"
mkdir -p "$HOME/.dogany/api-credit/$KEY_FP"
printf '{"ts":%s,"result":"payable","detail":"fixture"}\n' "$(date +%s)" \
  > "$HOME/.dogany/api-credit/$KEY_FP/probe.json"
DD="$WS/routines/dispatch-detached.sh"
# DGN-2304: --worktree refuses an inherited cwd repo that differs from the workspace repo.
cd "$WS"
DROOT="$WS/.telegram_bot/dispatch"
printf 'Why: dgn1996 fixture\nWhat: write the thing\nbuild the thing\n' > "$TDIR/task.md"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1" | head -1; }
sfield() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])).get(sys.argv[2],""); print(v if isinstance(v,str) else json.dumps(v))' "$1" "$2"; }
ledger_row() { grep "| $1 |" "$WS/product/auto-loop-ledger.md"; }

# spawn <case> [extra args...] -> RUNDIR, CALLS, SEEN
spawn() {
  local c="$1"; shift
  CALLS="$TDIR/calls-$c.txt"; : > "$CALLS"
  SEEN="$TDIR/seen-$c.txt"; rm -f "$SEEN"
  local out
  out="$(FAKE_CALLS="$CALLS" FAKE_SEEN="$SEEN" "$DD" --task "$TDIR/task.md" --model opus --pin-why "Exercise credit exhaustion replacement through normal runtime selection" --bottleneck accuracy \
    --bottleneck-why "given checks need exact pass fail verification" --why "dgn1996 test stub run" \
    --label "cr-$c" --timeout 2 "$@" 2>&1)"
  RUNDIR="$DROOT/$(runid_of "$out")"
  if ! wait_finalized "$RUNDIR" 90; then
    bad "$c: run did not finalize ($out)"
    return 1
  fi
}

echo "== 1. claude-api run dies on billing_error -> re-placed on claude-code =="
if FAKE_API=billing spawn a --runtime claude-api --pin-why "Exercise credit exhaustion replacement through normal runtime selection" --worktree crslug --notify push; then
  K="$RUNDIR"
  KID="$(basename "$K")"
  # status.json says "replacing" while the re-place launcher runs.
  for _ in $(seq 1 120); do
    [[ "$(sfield "$K/status.json" result)" != replacing && -f "$WS/.telegram_bot/session-inbox/dispatch-$KID.md" ]] && break
    sleep 1
  done
  [[ "$(sfield "$K/status.json" runtime)" == claude-api ]] && ok "A0 killed run ran on claude-api" || bad "A0 runtime=$(sfield "$K/status.json" runtime)"
  [[ "$(sfield "$K/status.json" result)" == replaced ]] && ok "A1 killed run ends result=replaced (not error)" \
    || bad "A1 result=$(sfield "$K/status.json" result) $(tail -5 "$K/worker.log")"
  [[ "$(sfield "$K/status.json" replaced_on)" == claude-code ]] && ok "A2 replaced_on=claude-code" || bad "A2 replaced_on=$(sfield "$K/status.json" replaced_on) log: $(tail -3 "$K/replace.log" 2>/dev/null)"
  NID="$(sfield "$K/status.json" replaced_to)"
  N="$DROOT/$NID"
  m="$(find "$HOME/.dogany/api-credit" "$DROOT" -name 'exhausted.json' -o -name 'api-credit-exhausted.json' 2>/dev/null | head -1)"
  [[ -n "$m" ]] && grep -q "$KID" "$m" && ok "A3 pool marked exhausted by this run" || bad "A3 marker: $m"
  if [[ "$NID" == dsp-* ]] && wait_finalized "$N" 90; then
    [[ "$(sfield "$N/status.json" runtime)" == claude-code && "$(sfield "$N/status.json" result)" == completed ]] \
      && ok "A4 re-placed run ran on claude-code and completed" || bad "A4 $(sfield "$N/status.json" runtime)/$(sfield "$N/status.json" result)"
    [[ "$(sfield "$N/status.json" replaced_from)" == "$KID" ]] && ok "A5 re-placed run records replaced_from=$KID" || bad "A5"
    [[ "$(sfield "$N/status.json" requested_runtime)" == auto && "$(sfield "$N/status.json" selection_source)" != flag ]] \
      && ok "A6 runtime came from normal selection, not the old --runtime flag" \
      || bad "A6 requested=$(sfield "$N/status.json" requested_runtime) source=$(sfield "$N/status.json" selection_source)"
    cmp -s "$K/prompt.md" "$N/prompt.md" && ok "A7 same brief (prompt.md identical)" || bad "A7 prompt differs"
    grep -q "^prompt=Why: dgn1996 fixture" "$SEEN" && ok "A8 the subscription worker got the brief" || bad "A8 seen: $(cat "$SEEN" 2>/dev/null)"
    ( source "$N/run.env"; [[ "$WT_BASE" == dsp/crslug && "$BRANCH" == dsp/crslug-rp ]] ) \
      && ok "A9 base = the killed run's zero-commit branch dsp/crslug (new branch dsp/crslug-rp)" || bad "A9 $(grep -E '^(WT_BASE|BRANCH)=' "$N/run.env")"
    grep -q "^partial=no" "$SEEN" && ok "A10 killed attempt produced no committed work" || bad "A10 $(cat "$SEEN" 2>/dev/null)"
    grep '"kind": "replace"' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" 2>/dev/null \
      | grep -q "\"to\": \"$NID\".*API credit exhausted on claude-api" \
      && ok "A11 host decision log: replace -> $NID with the reason" || bad "A11 $(cat "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" 2>/dev/null | grep replace)"
  else
    bad "A4-A11 no re-placed run ($NID): $(cat "$K/replace.log" 2>/dev/null | tail -5)"
  fi
  row="$(ledger_row dsp/crslug)"
  [[ "$row" == *"resolved-requeued"* && "$row" == *"re-placed on claude-code as $NID"* && "$row" == *"API credit exhausted"* ]] \
    && ok "A12 killed row: resolved-requeued, re-placed on claude-code, reason" || bad "A12 $row"
  ib="$WS/.telegram_bot/session-inbox/dispatch-$KID.md"
  grep -q "re-placed on claude-code as $NID" "$ib" && ok "A13 return notice says re-placed on claude-code" || bad "A13 $(cat "$ib" 2>/dev/null)"
  grep -q "Nothing to decide" "$ib" && ! grep -qi "decide re-dispatch\|decide whether" "$ib" \
    && ok "A14 return asks the agent to choose nothing" || bad "A14 $(grep 'Session directive' "$ib")"
  if grep -q "Nothing for you to do" "$PUSH_STUB_LOG" && ! grep -qiE "top.?up|\?" <<< "$(grep 'Nothing for you' "$PUSH_STUB_LOG")"; then
    ok "A15 owner end notice is a statement, no resource question"
  else
    bad "A15 push log: $(cat "$PUSH_STUB_LOG" 2>/dev/null)"
  fi
  [[ "$(cat "$CALLS")" == $'api\nsub' || "$(sort "$CALLS" | tr '\n' ' ')" == "api sub " ]] \
    && ok "A16 exactly one api attempt then one subscription attempt" || bad "A16 calls: $(tr '\n' ';' < "$CALLS")"
  out="$(python3 -B "$WS/routines/lib/host_dispatcher.py" replace --rundir "$K" --runid "$KID" --script "$DD" --workspace "$WS")"
  [[ "$out" == refused*"already re-placed"* ]] && ok "A17 a second re-place of the same run is refused" || bad "A17 $out"
fi

echo "== 2. a re-placed run is never re-placed again =="
out="$(python3 -B "$WS/routines/lib/host_dispatcher.py" replace --rundir "$TDIR" --runid dsp-x --script "$DD" --replaced-from dsp-y)"
[[ "$out" == refused*"one re-place per run"* ]] && ok "B1 replaced-from set -> refused" || bad "B1 $out"

echo "== 3. a non-credit claude-api failure stays an error =="
rm -f "$HOME"/.dogany/api-credit/*/exhausted.json "$DROOT/api-credit-exhausted.json"
if FAKE_API=other spawn c --runtime claude-api --pin-why "Exercise credit exhaustion replacement through normal runtime selection" --worktree crother --notify silent; then
  [[ "$(sfield "$RUNDIR/status.json" result)" == error && -z "$(sfield "$RUNDIR/status.json" replaced_to)" ]] \
    && ok "C1 result=error, no re-place" || bad "C1 $(sfield "$RUNDIR/status.json" result) $(sfield "$RUNDIR/status.json" replaced_to)"
  [[ "$(cat "$CALLS")" == api ]] && ok "C2 one attempt only" || bad "C2 $(tr '\n' ';' < "$CALLS")"
fi

echo "== 4. a credit-exhausted run with commits is reported, not re-placed =="
rm -f "$HOME"/.dogany/api-credit/*/exhausted.json "$DROOT/api-credit-exhausted.json"
if FAKE_API=billing_committed spawn d --runtime claude-api --pin-why "Exercise credit exhaustion replacement through normal runtime selection" --worktree crcommitted --notify push; then
  [[ "$(sfield "$RUNDIR/status.json" result)" == error && -z "$(sfield "$RUNDIR/status.json" replaced_to)" ]] \
    && ok "D1 committed run ends error with no re-place" || bad "D1 $(sfield "$RUNDIR/status.json" result) $(sfield "$RUNDIR/status.json" replaced_to)"
  [[ "$(sfield "$RUNDIR/status.json" commits)" == 1 ]] && ok "D2 committed product is recorded" || bad "D2 commits=$(sfield "$RUNDIR/status.json" commits)"
  [[ "$(cat "$CALLS")" == api ]] && ok "D3 no second worker was launched" || bad "D3 $(tr '\n' ';' < "$CALLS")"
fi

echo "== 5. argv rewrite (unit) =="
python3 - "$WS/routines/lib" <<'PY' && ok "D1 runtime/attach/base dropped, task->snapshot, -rp slug, local-why for --worktree-repo" || bad "D1"
import sys
sys.path.insert(0, sys.argv[1])
import host_dispatcher as hd
a = hd.replace_argv(["--task", "/tmp/x", "--runtime", "claude-api", "--attach", "f", "--worktree", "s",
                     "--worktree-repo", "/r", "--repo-contained", "--base", "dev", "--label", "L"],
                    "/run/prompt.md", "dsp-1", "dsp/s")
assert a == ["--task", "/run/prompt.md", "--worktree", "s-rp", "--worktree-repo", "/r", "--repo-contained",
             "--label", "L", "--base", "dsp/s", "--local-why",
             "re-place of dsp-1: its partial work is on the unpushed local branch dsp/s"], a
b = hd.replace_argv(["--task", "t", "--read-only", "--repo", "/r", "--local-why", "needs the device"], "/p", "dsp-1", "")
assert b == ["--task", "/p", "--read-only", "--repo", "/r", "--local-why", "needs the device"], b
e = hd.replace_env("dsp-1", "why", {"DOGANY_DISPATCH_RUNTIME": "claude-api", "ANTHROPIC_API_KEY": "k", "PATH": "/b"})
assert e == {"PATH": "/b", "DOGANY_DISPATCH_REPLACED_FROM": "dsp-1", "DOGANY_DISPATCH_REPLACE_REASON": "why"}, e
PY

echo
echo "dgn1996 credit re-place: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
