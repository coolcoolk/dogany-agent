#!/usr/bin/env bash
# test-lane: slow    # measured 88.9s (2026-09-11): spawns fake workers and
#                    # waits out the join window on every case
# test-dispatch-start-notice.sh -- the owner notice at dispatch START.
#
# THE GAP. Background work pushed the owner a notice when it FINISHED and
# nothing when it STARTED, so the only start signal was the agent saying so in
# prose -- which it forgot repeatedly on 2026-09-11, leaving the owner to ask
# whether work had begun at all. A machine-emitted notice does not depend on
# that diligence.
#
# The locks this suite exists for:
#   (a) ONE notice per burst, not N. The owner chose batching over
#       per-dispatch notices (20+ dispatches that day, plus a standing
#       complaint about message fragmentation), so three dispatches fired
#       inside the join window must produce exactly one message carrying all
#       three names and the count;
#   (b) a refused or failed dispatch emits NOTHING -- the notice means work is
#       running;
#   (c) the notice never blocks or fails the spawn: the sender is detached and
#       a push failure leaves the spawn green;
#   (d) the body carries owner-facing names only -- it must pass
#       bridge/machine_gate.find_execution_identifiers, the guard on the send
#       rail, rather than depend on it;
#   (e) a display name that itself carries an identifier shape is kept OUT of
#       the queue (loudly) instead of taking the whole burst's notice down at
#       the send rail;
#   (f) the DRAFT wording keeps the repo's pending-copy marker attached, and
#       does not reuse the owner-confirmed completion glyphs for a new meaning.
#
# Run: bash routines/tests/test-dispatch-start-notice.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: fake claude stub via PATH, push/usage-gate stubs, scratch
# workspace under mktemp. No real spawn, no telegram, no live ledger.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/startnotice-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/config" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot" "$WS/bridge" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/agentlib.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
# The real detector: the name screen in the dispatcher imports it exactly the
# way the send rail does (stdlib only, no bridge venv).
cp "$REPO/bridge/machine_gate.py" "$WS/bridge/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
# push.sh stub: records every body it is handed, one per line, so the suite can
# count MESSAGES (not dispatches) and read what the owner would have seen.
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
TEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --text) TEXT="$2"; shift 2 ;;
    *) shift 1 ;;
  esac
done
printf '%s\n' "$TEXT" >> "$PUSH_LOG"
exit "${PUSH_STUB_RC:-0}"
EOF
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
sleep "${FAKE_CLAUDE_SLEEP:-1}"
echo "FAKE-CLAUDE-DONE"
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"

printf 'AGENT_LANG=ko\n' > "$WS/config/agent.conf"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
EOF

git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
# Cap raised so a 3-dispatch burst is not refused by the concurrency gate
# (default 3, and the workers are still running inside the window).
export DOGANY_DISPATCH_MAX_ACTIVE=9
# The join window under test. The VALUE is measured in the script's own
# header; what this suite pins is the MECHANISM, so it uses a window wide
# enough that three sequential launcher runs (gates + calibration reader +
# detach, ~1-2s each) reliably land inside one burst on a loaded host.
export DOGANY_START_NOTICE_WINDOW=12
export PUSH_LOG="$TDIR/push.log"
: > "$PUSH_LOG"
DD="$WS/routines/dispatch-detached.sh"
NOTICE_DIR="$WS/.telegram_bot/dispatch/start-notice"
printf 'start notice probe task' > "$TDIR/task.md"

NAME_A="관계도 모바일 렌더 정리"
NAME_B="발주 시작 알림 배선"
NAME_C="사용량 게이트 실측"

reset_push() { : > "$PUSH_LOG"; }
msg_count() { grep -c . "$PUSH_LOG" 2>/dev/null || true; }
# Wait until the pending sender has flushed (window + margin), then settle.
wait_flush() {
  local i
  for i in $(seq 1 40); do
    if [[ ! -d "$NOTICE_DIR/.flusher" ]] && [[ "$(msg_count)" != "0" ]]; then
      sleep 1
      return 0
    fi
    sleep 1
  done
  return 1
}
spawn() {  # spawn <display-name> [extra args...]
  local name="$1"; shift
  FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
    --bottleneck accuracy --label "probe-job" --display-name "$name" \
    --timeout 2 --notify silent "$@"
}

echo "== T1. one dispatch -> one notice, in the completion notice's family =="
reset_push
OUT="$(spawn "$NAME_A" 2>"$TDIR/t1.err")"
RC=$?
[[ $RC -eq 0 ]] && ok "T1A dispatch spawns" || bad "T1A dispatch spawns (rc=$RC, $(cat "$TDIR/t1.err"))"
# Non-blocking: the spawn returned before the sender fired (that is also what
# makes a push failure unable to fail a spawn -- T6).
[[ "$(msg_count)" == "0" ]] \
  && ok "T1B spawn returned with no push yet (sender is detached, spawn never waits)" \
  || bad "T1B spawn returned before the push (got $(msg_count) message(s))"
grep -q "start notice: owner push in ${DOGANY_START_NOTICE_WINDOW}s" <<< "$OUT" \
  && ok "T1C the spawn line says a notice is pending and when (never silent)" \
  || bad "T1C spawn line announces the pending notice"
wait_flush || bad "T1 sender never flushed"
[[ "$(msg_count)" == "1" ]] && ok "T1D exactly one message was sent" \
  || bad "T1D exactly one message (got $(msg_count))"
BODY="$(head -1 "$PUSH_LOG")"
grep -qF "$NAME_A" <<< "$BODY" && ok "T1E the body names the job (display name)" \
  || bad "T1E body names the job (got: $BODY)"
grep -q '^▶️ 작업시작: ' <<< "$BODY" \
  && ok "T1F shape = glyph + status label + colon + name (completion family)" \
  || bad "T1F notice shape (got: $BODY)"
grep -q ' - ' <<< "$BODY" \
  && ok "T1G the body states what happens next after ' - ' (family)" \
  || bad "T1G body has the ' - ' next-step segment (got: $BODY)"
grep -q "probe-job" <<< "$BODY" && bad "T1H machine label absent from the body" \
  || ok "T1H machine label absent from the body"
grep -qE "dsp-|dsp/" <<< "$BODY" && bad "T1I no runid or branch in the body" \
  || ok "T1I no runid or branch in the body"

echo "== T2. the body passes the send rail's identifier guard =="
python3 - "$REPO/bridge/machine_gate.py" "$BODY" <<'PY' \
  && ok "T2A find_execution_identifiers finds nothing in the notice body" \
  || bad "T2A the notice body trips the identifier guard"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("_mg", sys.argv[1])
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)
hits = mg.find_execution_identifiers(sys.argv[2])
assert not hits, hits
PY

echo "== T3. a burst of three -> ONE message, not three (the owner's ruling) =="
reset_push
spawn "$NAME_A" >/dev/null 2>&1
spawn "$NAME_B" >/dev/null 2>&1
OUT3="$(spawn "$NAME_C" 2>/dev/null)"
grep -q "start notice: joined the pending batch" <<< "$OUT3" \
  && ok "T3A a later spawn reports joining the pending batch" \
  || bad "T3A later spawn joins the pending batch"
wait_flush || bad "T3 sender never flushed"
[[ "$(msg_count)" == "1" ]] \
  && ok "T3B three dispatches produced exactly ONE message" \
  || bad "T3B one message per burst (got $(msg_count))"
BODY3="$(head -1 "$PUSH_LOG")"
if grep -qF "$NAME_A" <<< "$BODY3" && grep -qF "$NAME_B" <<< "$BODY3" \
   && grep -qF "$NAME_C" <<< "$BODY3"; then
  ok "T3C the one message names all three jobs"
else
  bad "T3C the one message names all three jobs (got: $BODY3)"
fi
grep -q '^▶️ 작업시작 (3건): ' <<< "$BODY3" \
  && ok "T3D the batch form carries the count in the label" \
  || bad "T3D batch form carries the count (got: $BODY3)"
[[ ! -d "$NOTICE_DIR/.flusher" ]] \
  && ok "T3E the flusher claim is released after the flush (next burst can arm)" \
  || bad "T3E flusher claim released after flush"
[[ ! -s "$NOTICE_DIR/queue" ]] \
  && ok "T3F the queue is emptied by the flush (no name is sent twice)" \
  || bad "T3F queue emptied by the flush"

echo "== T4. a later burst gets its OWN message (the window is not a mute) =="
reset_push
spawn "$NAME_B" >/dev/null 2>&1
wait_flush || bad "T4 sender never flushed"
[[ "$(msg_count)" == "1" ]] && ok "T4A the next burst is notified again" \
  || bad "T4A next burst notified (got $(msg_count))"

echo "== T5. refused / failed dispatches emit NOTHING =="
reset_push
# (a) refused at the display-name gate (ko instance, no name).
"$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --label "probe-job" >/dev/null 2>&1
RC=$?
[[ $RC -eq 1 ]] && ok "T5A missing display name still refuses the spawn (exit 1)" \
  || bad "T5A missing display name refuses the spawn (rc=$RC)"
# (b) refused at the tier-binding gate.
"$DD" --task "$TDIR/task.md" --model haiku --bottleneck judgment \
  --label "probe-job" --display-name "$NAME_A" >/dev/null 2>&1
RC=$?
[[ $RC -eq 2 ]] && ok "T5B axis/model mismatch still refuses the spawn (exit 2)" \
  || bad "T5B axis/model mismatch refuses the spawn (rc=$RC)"
# (c) refused at the concurrency cap. The count is set deterministically --
# waiting for real workers to hold slots would make the refusal a race.
printf '2\n' > "$WS/.telegram_bot/dispatch/active-count"
DOGANY_DISPATCH_MAX_ACTIVE=2 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "probe-job" --display-name "$NAME_A" \
  >/dev/null 2>&1
RC=$?
[[ $RC -eq 2 ]] && ok "T5C active-cap refusal still refuses the spawn (exit 2)" \
  || bad "T5C active-cap refusal refuses the spawn (rc=$RC)"
printf '0\n' > "$WS/.telegram_bot/dispatch/active-count"
sleep "$(( DOGANY_START_NOTICE_WINDOW + 3 ))"
if [[ ! -d "$NOTICE_DIR/.flusher" ]]; then
  ok "T5D no sender was armed by any refused dispatch"
else
  bad "T5D a refused dispatch armed a sender"
fi
[[ "$(msg_count)" == "0" ]] \
  && ok "T5E refused dispatches sent no notice at all" \
  || bad "T5E refused dispatches sent no notice (got $(msg_count): $(cat "$PUSH_LOG"))"

echo "== T6. a push failure cannot fail the spawn =="
reset_push
RC=0
PUSH_STUB_RC=2 spawn "$NAME_A" >/dev/null 2>&1 || RC=$?
[[ $RC -eq 0 ]] && ok "T6A spawn exits 0 even though the sender's push fails" \
  || bad "T6A spawn exits 0 on push failure (rc=$RC)"
sleep "$(( DOGANY_START_NOTICE_WINDOW + 3 ))"
grep -q "push FAILED" "$NOTICE_DIR/flusher.log" \
  && ok "T6B the failure is recorded loudly in the sender's log (never silent)" \
  || bad "T6B push failure recorded in the sender log"

echo "== T7. an identifier-shaped display name is kept OUT of the queue =="
reset_push
# A name a caller really could pass. It is the caller's DATA, not wording from
# this change -- and the send rail would refuse the whole batch over it.
DIRTY="dsp-20260911-110101 정리"
OUT7="$(spawn "$DIRTY" 2>"$TDIR/t7.err")"
RC=$?
[[ $RC -eq 0 ]] \
  && ok "T7A the dispatch itself still runs (the notice is not a gate)" \
  || bad "T7A the dispatch still runs (rc=$RC)"
grep -q "start notice SKIPPED" "$TDIR/t7.err" \
  && ok "T7B the skip is reported at the spawn, where the name can be fixed" \
  || bad "T7B the skip is reported at the spawn"
grep -q "run-id" "$TDIR/t7.err" \
  && ok "T7C the report names the identifier shape it found" \
  || bad "T7C the report names the identifier shape"
# A clean sibling in the same burst must still be notified.
spawn "$NAME_C" >/dev/null 2>&1
wait_flush || bad "T7 sender never flushed"
BODY7="$(head -1 "$PUSH_LOG")"
[[ "$(msg_count)" == "1" ]] && grep -qF "$NAME_C" <<< "$BODY7" \
  && ok "T7D the clean sibling's notice still goes out" \
  || bad "T7D clean sibling still notified (got: $BODY7)"
grep -qF "dsp-20260911-110101" <<< "$BODY7" \
  && bad "T7E the dirty name never reached the body" \
  || ok "T7E the dirty name never reached the body"

echo "== T8. copy discipline: the draft is marked, the glyph is new =="
MARKER="WORDING PENDING OWNER CONFIRMATION"
# Same convention and same window as test-owner-notice-identifier-guard.py:
# the marker must sit in the comment block ABOVE the string, so draft copy
# cannot graduate to approved by nobody noticing.
python3 - "$WRAPPER" "$MARKER" <<'PY' \
  && ok "T8A both draft start strings carry the pending-copy marker above them" \
  || bad "T8A draft start strings carry the pending-copy marker within 25 lines above"
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
marker, window = sys.argv[2], 25
for needle in ("작업시작: %s", "작업시작 (%d건)"):
    idx = [i for i, ln in enumerate(lines) if needle in ln]
    assert idx, "draft string not found: %r" % needle
    for i in idx:
        assert any(marker in ln for ln in lines[max(0, i - window):i]), \
            "unmarked draft at line %d: %r" % (i + 1, needle)
PY
grep -q 'dec-094' <<< "$(awk '/START NOTICE -- one owner push/,/^do_spawn\(\)/' "$WRAPPER")" \
  && ok "T8B the copy gate is named (dec-094) on the start-notice copy too" \
  || bad "T8B the copy gate is named on the start-notice copy"
# The owner's two confirmed glyphs mean finished (✅) and problem (⚠️).
# Reusing either for "started" would overload a symbol he reads as a verdict.
if grep -qE '작업시작.*(✅|⚠️)|(✅|⚠️).*작업시작' "$WRAPPER"; then
  bad "T8C the start notice reuses a confirmed completion glyph"
else
  ok "T8C the start notice uses its own glyph, not ✅ or ⚠️"
fi
# The completion push is a different change's surface (and a different
# branch's): whatever wording it carries, this change must not have moved it.
if grep -qE '✅ 작업완료|맡기신 작업 하나가 끝났어요' "$WRAPPER"; then
  ok "T8D the completion copy is still present and untouched by this change"
else
  bad "T8D the completion copy is still present"
fi

echo "== T9. the sender never touches run bookkeeping =="
FLUSH_FN="$(awk '/^do_start_notice_flush\(\)/,/^}/' "$WRAPPER")"
grep -qE 'LEDGER_UPDATE|active_count|write_status_json|terminal-state-ledger' \
  <<< "$FLUSH_FN" \
  && bad "T9A the sender writes no ledger row, count or status (it has no run)" \
  || ok "T9A the sender writes no ledger row, count or status (it has no run)"
grep -q 'sleep "\$START_NOTICE_WINDOW"' <<< "$FLUSH_FN" \
  && ok "T9B the join is a time window anchored on the burst's first spawn" \
  || bad "T9B the join is a time window"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
