#!/usr/bin/env bash
# test-lane: slow    # measured 11.4s (2026-10-10, two detached fake workers)
unset DOGANY_BRIDGE_SESSION
# test-dgn1979-two-agents-e2e.sh -- DGN-1979 phase 1 end to end: two agents on
# one machine dispatch through ONE host dispatcher.
#
#   Q1  agent A takes the only slot of the lane (host cap 1)
#   Q2  agent B's request does not fit -> queued on the HOST (exit 0, ticket),
#       no run dir yet, the decision recorded `queued`
#   Q3  B bumps its ticket with a stated reason; the bump is logged
#   Q4  --list on either agent shows the host view (lane, queue)
#   Q5  --no-queue is compatible but still queues (DGN-2011)
#   Q6  A's worker finishes -> its release drains the queue -> B's OWN script
#       runs the replayed request in B's workspace and finalizes there
#   Q7  afterwards: no lease, no ticket left; the drain is a decision line
#   Q8  the retired off override still uses the host (DGN-2011)
#
# Offline: fake claude on PATH, push/usage-gate stubs, temp HOME and
# DOGANY_SHARED_HOME, pinned machine reading.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d "${TMPDIR:-/tmp}/dgn1979-e2e.XXXXXX")"
TDIR="$(cd "$TDIR" && pwd -P)"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  # DGN-2090: the ledger it wrote is chflags uappend; clear the flags on THIS
  # fixture dir only, so rm -rf really removes it.
  [[ -n "${KEEP:-}" ]] || { chflags -R nouappend,nouchg "$TDIR" 2>/dev/null; chmod -R u+w "$TDIR" 2>/dev/null; rm -rf "$TDIR"; }
  echo "TDIR=$TDIR"
}
trap cleanup EXIT

mkdir -p "$TDIR/bin" "$TDIR/home"
export HOME="$TDIR/home"
export DOGANY_SHARED_HOME="$TDIR/shared"
export DOGANY_DISPATCH_MACHINE="1 8 32000 0"
export PUSH_STUB_LOG="$TDIR/push.log"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
unset DOGANY_HOST_DISPATCH DOGANY_DISPATCH_WORKER

make_ws() {  # <dir>
  local ws="$1"
  mkdir -p "$ws/routines/lib" "$ws/product" "$ws/.claude" "$ws/.telegram_bot"
  cp "$WRAPPER" "$ws/routines/"
  bash "$HARNESS_DIR/fixtures/install-dispatch-policy.sh" "$ws"
  cp "$REPO/routines/lib/dispatch_storage.py" "$REPO/routines/lib/conf_reader.py" "$ws/routines/lib/"
  local f
  for f in ledger-update.sh dispatch-tier-bindings.tsv dispatch-effort-bindings.tsv model_display.py run_block.py i18n_lookup.py \
           dispatch-model-display.tsv machine_signal.py conf_reader.py why_owner_turn.py \
           host_dispatcher.py reset_passes.py api_credit_pool.py; do
    cp "$REPO/routines/lib/$f" "$ws/routines/lib/"
  done
  mkdir -p "$ws/config" && cp -R "$REPO/config/i18n" "$ws/config/"
  cp "$REPO/routines/terminal-state-ledger.py" "$ws/routines/" 2>/dev/null || true
  printf '#!/bin/bash\necho "USAGE_GATE: pass five_hour=10 seven_day=10"\nexit 0\n' > "$ws/routines/usage-gate.sh"
  printf '#!/bin/bash\necho "PUSH-STUB $*" >> "${PUSH_STUB_LOG:-/dev/null}"\nexit 0\n' > "$ws/routines/push.sh"
  chmod +x "$ws/routines/dispatch-detached.sh" "$ws/routines/usage-gate.sh" "$ws/routines/push.sh" \
    "$ws/routines/lib/ledger-update.sh"
  cat > "$ws/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
  git -C "$ws" init -q -b main >/dev/null
  git -C "$ws" -c user.email=t@t -c user.name=t add -A >/dev/null
  git -C "$ws" -c user.email=t@t -c user.name=t commit -qm seed >/dev/null
}

cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
echo "FAKE-CLAUDE pwd: $(pwd)"
# FAKE_CLAUDE_HOLD=<file>: run until the suite creates it (bounded 60s).
if [[ -n "${FAKE_CLAUDE_HOLD:-}" ]]; then
  for _i in $(seq 1 600); do [[ -e "$FAKE_CLAUDE_HOLD" ]] && break; sleep 0.1; done
else
  sleep "${FAKE_CLAUDE_SLEEP:-1}"
fi
echo "FAKE-CLAUDE-DONE"
EOF
chmod +x "$TDIR/bin/claude"
export PATH="$TDIR/bin:$PATH"
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"

WSA="$TDIR/agent-a"; WSB="$TDIR/agent-b"
make_ws "$WSA"; make_ws "$WSB"
HD="$REPO/routines/lib/host_dispatcher.py"

ARGS=(--read-only --model haiku --pin-why "Exercise shared host capacity across two detached agents" --bottleneck accuracy
      --bottleneck-why "given checks need exact pass fail verification"
      --why "dgn1979 e2e stub run" --timeout 2 --notify silent)

wait_for() {  # <seconds> <command...>
  local n="$1"; shift
  local i
  for i in $(seq 1 $(( n * 5 ))); do "$@" && return 0; sleep 0.2; done
  return 1
}
finalized_runs() { ls -d "$1"/.telegram_bot/dispatch/dsp-*/.finalized 2>/dev/null | wc -l | tr -d ' '; }
has_finalized() { [[ "$(finalized_runs "$1")" -ge "$2" ]]; }  # <ws> <n>
no_leases() { [[ -z "$(ls "$DOGANY_SHARED_HOME/dispatcher/leases" 2>/dev/null)" ]]; }

echo "== Q1 agent A takes the lane's only slot =="
printf 'task A' > "$TDIR/task-a.md"
# A holds the lane's only slot until Q5 is done -- a fixed 6s sleep ran out
# before Q4/Q5 on a loaded host (DGN-2038 I: red inside the merge gate's sweep).
OUT_A="$(cd "$WSA" && DOGANY_DISPATCH_MAX_ACTIVE=1 FAKE_CLAUDE_HOLD="$TDIR/release-a" \
  "$WSA/routines/dispatch-detached.sh" "${ARGS[@]}" --task "$TDIR/task-a.md" --label "a run" 2>&1)"
grep -q 'spawned: runid=' <<< "$OUT_A" && ok "A spawned" || bad "A spawned: $OUT_A"
LEASES="$(ls "$DOGANY_SHARED_HOME/dispatcher/leases" 2>/dev/null | wc -l | tr -d ' ')"
[[ "$LEASES" == 1 ]] && ok "one host lease" || bad "host leases = $LEASES"
grep -q '^HOST_LEASE_ID=L' "$WSA"/.telegram_bot/dispatch/dsp-*/run.env && ok "run.env carries the lease" \
  || bad "run.env lacks HOST_LEASE_ID"

echo "== Q2 agent B is queued on the host =="
printf 'task B' > "$TDIR/task-b.md"
RC=0
OUT_B="$(cd "$WSB" && DOGANY_DISPATCH_MAX_ACTIVE=1 \
  "$WSB/routines/dispatch-detached.sh" "${ARGS[@]}" --task "$TDIR/task-b.md" --label "b run" 2>&1)" || RC=$?
TICKET="$(sed -n 's/.*(ticket \(T[0-9]*-[0-9a-f]*\)).*/\1/p' <<< "$OUT_B" | head -1)"
[[ $RC -eq 0 && -n "$TICKET" ]] && ok "B queued (exit 0, ticket $TICKET)" || bad "B queued: rc=$RC $OUT_B"
[[ "$(ls -d "$WSB"/.telegram_bot/dispatch/dsp-* 2>/dev/null | wc -l | tr -d ' ')" == 0 ]] \
  && ok "no run dir for a queued request" || bad "B has a run dir while queued"
grep -lq '"status": "queued"' "$WSB"/.telegram_bot/dispatch-decisions/*.json 2>/dev/null \
  || grep -rlq '"queued"' "$WSB/.telegram_bot" 2>/dev/null
[[ $? -eq 0 ]] && ok "decision recorded queued" || bad "no queued decision record"

echo "== Q3 B bumps with a stated reason =="
BUMP="$(cd "$WSB" && "$WSB/routines/dispatch-detached.sh" --bump "$TICKET" --reason owner_waiting \
  --note "owner is waiting on this result" 2>&1)"
grep -q 'bumped' <<< "$BUMP" && ok "bump accepted ($BUMP)" || bad "bump: $BUMP"
grep -q '"reason": "owner_waiting"' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" \
  && grep -q 'owner is waiting on this result' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" \
  && ok "bump logged with its reason" || bad "bump not logged"
OTHER="$(cd "$WSA" && "$WSA/routines/dispatch-detached.sh" --bump "$TICKET" --reason owner_waiting \
  --note "not mine" 2>&1)" && bad "A bumped B's ticket" || ok "another agent cannot bump it"

echo "== Q4 host view =="
LIST="$(cd "$WSA" && "$WSA/routines/dispatch-detached.sh" --list 2>&1)"
grep -q "queued $TICKET owner-live" <<< "$LIST" && ok "A's --list shows B's ticket (owner-live)" \
  || bad "--list: $LIST"
grep -q 'active 1 / cap 1' <<< "$LIST" && ok "lane active/cap shown" || bad "lane line missing"

echo "== Q5 --no-queue still queues =="
RC=0
(cd "$WSB" && DOGANY_DISPATCH_MAX_ACTIVE=1 "$WSB/routines/dispatch-detached.sh" "${ARGS[@]}" \
  --task "$TDIR/task-b.md" --label "b now" --no-queue >"$TDIR/nq.out" 2>&1) || RC=$?
[[ $RC -eq 0 ]] && grep -q 'queued #2 (ticket T' "$TDIR/nq.out" && ok "--no-queue -> accepted in position 2" \
  || bad "--no-queue rc=$RC $(tail -3 "$TDIR/nq.out")"

echo "== Q6 A finishes -> the host drains B's ticket into B's workspace =="
touch "$TDIR/release-a"
wait_for 40 has_finalized "$WSA" 1 && ok "A finalized" || bad "A never finalized"
wait_for 40 has_finalized "$WSB" 2 && ok "B ran and finalized in its own workspace" \
  || bad "B never ran: $(cat "$WSB"/.telegram_bot/dispatch/queue-*.log 2>/dev/null | tail -5)"
grep -q 'FAKE-CLAUDE pwd' "$WSB"/.telegram_bot/dispatch/dsp-*/out.log 2>/dev/null \
  && ok "B's worker ran" || bad "B's worker output missing"
[[ -f "$WSB/.telegram_bot/dispatch/queue-$TICKET.log" ]] && ok "drain log in B's dispatch dir" \
  || bad "no drain log"
grep -q 'drained lease adopted' "$WSB/.telegram_bot/dispatch/queue-$TICKET.log" 2>/dev/null \
  || grep -q '"kind": "adopt"' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl"
[[ $? -eq 0 ]] && ok "B adopted its drained lease" || bad "no adopt"

echo "== Q7 books are clean =="
wait_for 20 no_leases \
  && ok "no lease left" || bad "leases left: $(ls "$DOGANY_SHARED_HOME/dispatcher/leases")"
[[ -z "$(ls "$DOGANY_SHARED_HOME/dispatcher/queue" 2>/dev/null)" ]] && ok "no ticket left" || bad "tickets left"
grep -q "\"ticket\": \"$TICKET\"" "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" \
  && grep -q '"via": "drain"' "$DOGANY_SHARED_HOME/dispatcher/decisions.jsonl" \
  && ok "drain grant on the decision log" || bad "no drain grant line"

echo "== Q8 retired off override still uses the host =="
OUT_OFF="$(cd "$WSA" && DOGANY_HOST_DISPATCH=0 FAKE_CLAUDE_SLEEP=1 \
  "$WSA/routines/dispatch-detached.sh" "${ARGS[@]}" --task "$TDIR/task-a.md" --label "a off" 2>&1)"
grep -q 'spawned: runid=' <<< "$OUT_OFF" && ok "spawned through the host" || bad "off path: $OUT_OFF"
grep -q '^HOST_LEASE_ID=L' "$WSA"/.telegram_bot/dispatch/dsp-*/run.env \
  && ok "host lease despite retired off override" || bad "missing host lease"
wait_for 30 has_finalized "$WSA" 2 || true

echo ""
echo "== DGN-1979 two agents e2e: $PASS passed, $FAIL failed =="
[[ $FAIL -eq 0 ]]
