#!/usr/bin/env bash
# test-lane: slow    # measured 88.9s (2026-09-11): spawns fake workers and
# test-host: macos observe mode refuses to run without sandbox-exec
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
#   (f) the notice renders the owner-designed START block (identity Response
#       workflow, "Completion signal"): title
#       `▶️ <background-term> 작업 시작` with " (N건)" ONLY when N>1, then
#       one `- <Model>: <작업명> <HH:MM>` line per run -- and the title TERM
#       is the instance-overridable background term (conf
#       DASHBOARD_LIVE_LABEL minus " 작업 중"), never a fixed string, for
#       EVERY burst: dispatch-only, job-only or mixed (DGN-1767);
#   (g) the glyph stays a third one (▶️): ✅ and ⚠️ are verdict glyphs the
#       owner already reads;
#   (h) DGN-1904 (owner dec-274): start and end are ONE pair under --notify.
#       A silent run sends the owner NOTHING (no start, no end), a push run
#       sends start + end, and a burst mixing the two counts only push runs.
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
  chflags -R nouappend,nouchg "$TDIR" 2>/dev/null   # terminal-state ledger is uappend
  chmod -R u+w "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/config" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot" "$WS/bridge" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/agentlib.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/model_display.py" "$WS/routines/lib/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
# The real detector: the name screen in the dispatcher imports it exactly the
# way the send rail does (stdlib only, no bridge venv).
cp "$REPO/bridge/machine_gate.py" "$WS/bridge/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
# push.sh stub: records every body it is handed, so the suite can count
# MESSAGES (not dispatches) and read what the owner would have seen. Bodies
# are MULTI-LINE now (the designed block: title + one line per run), so each
# recorded message ends with a sentinel line the counters key on.
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
TEXT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --text) TEXT="$2"; shift 2 ;;
    *) shift 1 ;;
  esac
done
printf '%s\n===MSG-END===\n' "$TEXT" >> "$PUSH_LOG"
exit "${PUSH_STUB_RC:-0}"
EOF
# DGN-1727: FAKE_CLAUDE_MODEL + FAKE_CLAUDE_INIT_DELAY make the stub report a
# model id (a stream init event) that many seconds after it starts, like the
# real CLI; without them it reports nothing (the pre-existing cases).
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
if [[ -n "${FAKE_CLAUDE_MODEL:-}" ]]; then
  sleep "${FAKE_CLAUDE_INIT_DELAY:-0}"
  printf '{"type":"system","subtype":"init","model":"%s"}\n' "$FAKE_CLAUDE_MODEL"
fi
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
# The title terms resolve env -> conf -> default; a caller's env must not
# leak into the conf cases below.
unset DOGANY_SIBLING_TERM DOGANY_LIVE_LABEL
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
# Spawns default to --notify push (DGN-1904: a silent run has no start notice),
# so the log also carries END pushes. msg_count / first_msg look at START
# messages only (title glyph ▶️); all_count counts every owner push.
all_count() { grep -c '^===MSG-END===$' "$PUSH_LOG" 2>/dev/null || true; }
msg_count() {
  awk 'BEGIN{n=0; first=1} /^===MSG-END===$/{first=1; next}
       first{ if (index($0, "▶️") == 1) n++; first=0 } END{print n}' \
    "$PUSH_LOG" 2>/dev/null || echo 0
}
end_count() { grep -c '^✅ ' "$PUSH_LOG" 2>/dev/null || true; }
# The first recorded START message, sentinel stripped (whole multi-line body).
first_msg() {
  awk 'BEGIN{first=1; take=0} /^===MSG-END===$/{ if (take) exit; first=1; next }
       first{ first=0; if (index($0, "▶️") == 1) take=1 }
       take{print}' "$PUSH_LOG" 2>/dev/null
}
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
  FAKE_CLAUDE_SLEEP=1 "$DD" --read-only --task "$TDIR/task.md" --model sonnet \
    --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "probe-job" --display-name "$name" \
    --timeout 2 "$@"
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
BODY="$(first_msg)"
grep -qF "$NAME_A" <<< "$BODY" && ok "T1E the body names the job (display name)" \
  || bad "T1E body names the job (got: $BODY)"
# The designed START block (identity Response workflow, "Completion signal"):
# a lone dispatch gets the bare title -- NO (N건) suffix -- and one item line
# `- <Model>: <작업명> <HH:MM>` (model = capitalized TSV display column).
[[ "$(head -1 <<< "$BODY")" == "▶️ 백그라운드 작업 시작" ]] \
  && ok "T1F title line is the designed block title, bare (no batch suffix)" \
  || bad "T1F designed title line (got: $(head -1 <<< "$BODY"))"
grep -qE "^- Claude Sonnet 5\.5: $NAME_A [0-9]{2}:[0-9]{2}\$" <<< "$BODY" \
  && ok "T1G item line = '- <Model>: <작업명> <HH:MM>' with capitalized model" \
  || bad "T1G designed item line (got: $BODY)"
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
BODY3="$(first_msg)"
if grep -qF "$NAME_A" <<< "$BODY3" && grep -qF "$NAME_B" <<< "$BODY3" \
   && grep -qF "$NAME_C" <<< "$BODY3"; then
  ok "T3C the one message names all three jobs"
else
  bad "T3C the one message names all three jobs (got: $BODY3)"
fi
[[ "$(head -1 <<< "$BODY3")" == "▶️ 백그라운드 작업 시작 (3건)" ]] \
  && ok "T3D N>1 appends (N건) to the title line (designed batch form)" \
  || bad "T3D batch title with (3건) (got: $(head -1 <<< "$BODY3"))"
[[ "$(grep -c '^- ' <<< "$BODY3")" == "3" ]] \
  && ok "T3D2 one item line per run, three runs -> three item lines" \
  || bad "T3D2 three item lines (got: $BODY3)"
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
"$DD" --read-only --task "$TDIR/task.md" --model sonnet --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" \
  --label "probe-job" >/dev/null 2>&1
RC=$?
[[ $RC -eq 1 ]] && ok "T5A missing display name still refuses the spawn (exit 1)" \
  || bad "T5A missing display name refuses the spawn (rc=$RC)"
# (b) refused at the tier-binding gate.
"$DD" --read-only --task "$TDIR/task.md" --model haiku --bottleneck judgment --bottleneck-why "the design tradeoff requires an explicit verdict" \
  --label "probe-job" --display-name "$NAME_A" >/dev/null 2>&1
RC=$?
[[ $RC -eq 2 ]] && ok "T5B axis/model mismatch still refuses the spawn (exit 2)" \
  || bad "T5B axis/model mismatch refuses the spawn (rc=$RC)"
# (c) refused at the concurrency cap. The count is set deterministically --
# waiting for real workers to hold slots would make the refusal a race.
printf '2\n' > "$WS/.telegram_bot/dispatch/active-count"
DOGANY_DISPATCH_MAX_ACTIVE=2 "$DD" --read-only --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --label "probe-job" --display-name "$NAME_A" \
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
[[ "$(all_count)" == "0" ]] \
  && ok "T5E refused dispatches sent no notice at all" \
  || bad "T5E refused dispatches sent no notice (got $(all_count): $(cat "$PUSH_LOG"))"

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
BODY7="$(first_msg)"
[[ "$(msg_count)" == "1" ]] && grep -qF "$NAME_C" <<< "$BODY7" \
  && ok "T7D the clean sibling's notice still goes out" \
  || bad "T7D clean sibling still notified (got: $BODY7)"
grep -qF "dsp-20260911-110101" <<< "$BODY7" \
  && bad "T7E the dirty name never reached the body" \
  || ok "T7E the dirty name never reached the body"

echo "== T8. copy discipline: the block is approved, the term is not fixed =="
# The start notice now renders the owner-designed START block (identity
# Response workflow, "Completion signal"), so the pin INVERTS from the old
# draft-marker check: a dated approval record must sit above the title, the
# pending marker must NOT (approved copy re-read as draft gets re-worded by
# the next pass), and the title TERM must be resolved through background_term()
# -- hardcoding the neutral term into the string is the exact
# halfway-override the owner caught on 2026-09-18.
MARKER="WORDING PENDING OWNER CONFIRMATION"
python3 - "$WRAPPER" "$MARKER" <<'PY' \
  && ok "T8A approval record (not the pending marker) sits above the block title" \
  || bad "T8A approval record within 25 lines above the block title, no pending marker"
import sys
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
marker, window = sys.argv[2], 25
needle = '작업 시작"'
idx = [i for i, ln in enumerate(lines)
       if needle in ln and not ln.lstrip().startswith("#")]
assert idx, "block title string not found: %r" % needle
for i in idx:
    above = lines[max(0, i - window):i]
    assert not any(marker in ln for ln in above), \
        "pending marker sits on approved copy at line %d" % (i + 1)
    assert any("approved" in ln.lower() and "2026-09-18" in ln for ln in above), \
        "no dated approval record within %d lines above line %d" % (window, i + 1)
PY
grep -q 'dec-094' <<< "$(awk '/START NOTICE -- one owner push/,/^do_spawn\(\)/' "$WRAPPER")" \
  && ok "T8B the copy gate is named (dec-094) on the start-notice copy too" \
  || bad "T8B the copy gate is named on the start-notice copy"
# The owner's two confirmed glyphs mean finished (✅) and problem (⚠️).
# Reusing either for "started" would overload a symbol he reads as a verdict.
if grep -qE '작업 시작.*(✅|⚠️)|(✅|⚠️).*작업 시작' "$WRAPPER"; then
  bad "T8C the start notice reuses a confirmed completion glyph"
else
  ok "T8C the start notice uses its own glyph, not ✅ or ⚠️"
fi
# The title term must come from background_term(), never a literal, and
# never sibling_term(): every start burst -- dispatch runs included -- is
# titled with the background term (owner-locked 2026-09-28, DGN-1767).
grep -q '▶️ $(background_term) 작업 시작' "$WRAPPER" \
  && ok "T8D the start title interpolates background_term() (term never fixed)" \
  || bad "T8D start title interpolates background_term()"
grep -q '▶️ $(sibling_term) 작업 시작' "$WRAPPER" \
  && bad "T8D2 the start title no longer uses sibling_term()" \
  || ok "T8D2 the start title no longer uses sibling_term()"
grep -q '✅ $(background_term) 작업 완료' "$WRAPPER" \
  && ok "T8E the end title interpolates the SAME background_term() (DGN-1769)" \
  || bad "T8E end title interpolates background_term()"

echo "== T10. the background term is instance-overridable via conf =="
reset_push
# DGN-1767: the start title follows background_term() (conf
# DASHBOARD_LIVE_LABEL minus " 작업 중"); the sibling term set beside it
# must NOT reach the start title (it still feeds the dispatch END title).
printf 'AGENT_LANG=ko\nDASHBOARD_LIVE_LABEL="TERM-X 작업 중"\nDASHBOARD_SIBLING_TERM=TERM-S\n' > "$WS/config/agent.conf"
spawn "$NAME_A" >/dev/null 2>&1
wait_flush || bad "T10 sender never flushed"
BODY10="$(first_msg)"
[[ "$(head -1 <<< "$BODY10")" == "▶️ TERM-X 작업 시작" ]] \
  && ok "T10A conf DASHBOARD_LIVE_LABEL feeds the start title (dispatch burst)" \
  || bad "T10A conf term feeds the start title (got: $(head -1 <<< "$BODY10"))"
if grep -qE '백그라운드|TERM-S' <<< "$BODY10"; then
  bad "T10B the default or the sibling term leaks past the instance term"
else
  ok "T10B the neutral default is fully replaced; the sibling term stays out"
fi
printf 'AGENT_LANG=ko\n' > "$WS/config/agent.conf"

echo "== T9. the sender never touches run bookkeeping =="
FLUSH_FN="$(awk '/^do_start_notice_flush\(\)/,/^}/' "$WRAPPER")"
grep -qE 'LEDGER_UPDATE|active_count|write_status_json|terminal-state-ledger' \
  <<< "$FLUSH_FN" \
  && bad "T9A the sender writes no ledger row, count or status (it has no run)" \
  || ok "T9A the sender writes no ledger row, count or status (it has no run)"
grep -q 'sleep "\$START_NOTICE_WINDOW"' <<< "$FLUSH_FN" \
  && ok "T9B the join is a time window anchored on the burst's first spawn" \
  || bad "T9B the join is a time window"

echo "== T11. DGN-1727: the start notice waits for the observed model =="
# opus: the run reports claude-opus-5-5 6s after it starts, AFTER the 3s join
# window. The notice must wait for it (bounded) and render "Claude Opus 5.5".
spawn_m() {  # spawn_m <model> <display-name> [extra args...]
  local model="$1" name="$2"; shift 2
  "$DD" --read-only --task "$TDIR/task.md" --model "$model" \
    --bottleneck judgment --bottleneck-why "the design tradeoff requires an explicit verdict" \
    --label "probe-job" --display-name "$name" --timeout 2 "$@"
}
reset_push
T11_T0=$(date +%s)
DOGANY_START_NOTICE_WINDOW=3 DOGANY_START_NOTICE_MODEL_WAIT=25 \
  FAKE_CLAUDE_MODEL=claude-opus-5-5 FAKE_CLAUDE_INIT_DELAY=6 FAKE_CLAUDE_SLEEP=20 \
  spawn_m opus "$NAME_A" >/dev/null 2>&1
wait_flush || bad "T11 sender never flushed"
T11_ELAPSED=$(( $(date +%s) - T11_T0 ))
BODY11="$(first_msg)"
grep -qE "^- Claude Opus 5\.5: $NAME_A [0-9]{2}:[0-9]{2}\$" <<< "$BODY11" \
  && ok "T11A observed id arrives inside the bound -> '- Claude Opus 5.5: ...'" \
  || bad "T11A versioned label (got: $BODY11)"
[[ "$T11_ELAPSED" -le 20 ]] \
  && ok "T11B the notice went out as soon as the id arrived (${T11_ELAPSED}s, well under the 25s bound)" \
  || bad "T11B notice not held past the observed id (${T11_ELAPSED}s)"

echo "== T12. DGN-1727: id never arrives -> bare name at the bound, no guess =="
reset_push
T12_T0=$(date +%s)
DOGANY_START_NOTICE_WINDOW=3 DOGANY_START_NOTICE_MODEL_WAIT=5 FAKE_CLAUDE_SLEEP=40 \
  spawn_m opus "$NAME_B" >/dev/null 2>&1
wait_flush || bad "T12 sender never flushed"
T12_ELAPSED=$(( $(date +%s) - T12_T0 ))
BODY12="$(first_msg)"
grep -qE "^- Claude Opus 5.5: $NAME_B [0-9]{2}:[0-9]{2}\$" <<< "$BODY12" \
  && ok "T12A no id at the bound -> bare '- Claude Opus 5.5: ...' (a version is never guessed)" \
  || bad "T12A bare label at the bound (got: $BODY12)"
[[ "$T12_ELAPSED" -le 20 ]] \
  && ok "T12B the wait is bounded (${T12_ELAPSED}s with a 5s bound)" \
  || bad "T12B wait not bounded (${T12_ELAPSED}s)"

echo "== T13. DGN-1727: mixed batch -- one message, per-item labels =="
# window 6: three launcher runs land in one burst. opus reports its id in
# time, sonnet never does (bare), and a session background job rides along
# with its description only. ONE shared bound covers the batch.
reset_push
T13_T0=$(date +%s)
export DOGANY_START_NOTICE_WINDOW=6 DOGANY_START_NOTICE_MODEL_WAIT=8
FAKE_CLAUDE_MODEL=claude-opus-5-5 FAKE_CLAUDE_INIT_DELAY=1 FAKE_CLAUDE_SLEEP=30 \
  spawn_m opus "$NAME_A" >/dev/null 2>&1
FAKE_CLAUDE_SLEEP=30 "$DD" --read-only --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" \
  --label "probe-job" --display-name "$NAME_B" --timeout 2 >/dev/null 2>&1
"$DD" --start-notice-enqueue --display-name "$NAME_C" --model-slot "cloud-wait.sh" >/dev/null 2>&1
export DOGANY_START_NOTICE_WINDOW=12
unset DOGANY_START_NOTICE_MODEL_WAIT
wait_flush || bad "T13 sender never flushed"
T13_ELAPSED=$(( $(date +%s) - T13_T0 ))
BODY13="$(first_msg)"
[[ "$(msg_count)" == "1" ]] && [[ "$(head -1 <<< "$BODY13")" == "▶️ 백그라운드 작업 시작 (3건)" ]] \
  && ok "T13A three mixed items -> ONE message with (3건)" \
  || bad "T13A one batched message (got $(msg_count): $BODY13)"
grep -qE "^- Claude Opus 5\.5: $NAME_A [0-9]{2}:[0-9]{2}\$" <<< "$BODY13" \
  && ok "T13B the observed run shows its version" || bad "T13B observed item (got: $BODY13)"
grep -qE "^- Claude Sonnet 5\.5: $NAME_B [0-9]{2}:[0-9]{2}\$" <<< "$BODY13" \
  && ok "T13C the run that never reported keeps the bare name" || bad "T13C bare item (got: $BODY13)"
grep -qE "^- $NAME_C [0-9]{2}:[0-9]{2}\$" <<< "$BODY13" \
  && ok "T13D the background job line is the description only (no command name)" \
  || bad "T13D description-only job line (got: $BODY13)"
grep -q "cloud-wait.sh" <<< "$BODY13" \
  && bad "T13E the command name leaked into the notice" \
  || ok "T13E the command name never reaches the notice"
[[ "$T13_ELAPSED" -le 30 ]] \
  && ok "T13F the batch waited one bounded interval, not one per item (${T13_ELAPSED}s)" \
  || bad "T13F batch delay bounded (${T13_ELAPSED}s)"

echo "== T15. DGN-1767: job-only burst and legacy tab-less line, same title =="
reset_push
"$DD" --start-notice-enqueue --display-name "$NAME_A" --model-slot "Shell" >/dev/null 2>&1
"$DD" --start-notice-enqueue --display-name "$NAME_B" --model-slot "python3" >/dev/null 2>&1
wait_flush || bad "T15 sender never flushed"
BODY15="$(first_msg)"
[[ "$(msg_count)" == "1" ]] && [[ "$(head -1 <<< "$BODY15")" == "▶️ 백그라운드 작업 시작 (2건)" ]] \
  && ok "T15A two shell jobs -> ONE message titled with the background term" \
  || bad "T15A job-only burst title (got $(msg_count): $BODY15)"
grep -qE "^- $NAME_A [0-9]{2}:[0-9]{2}\$" <<< "$BODY15" \
  && grep -qE "^- $NAME_B [0-9]{2}:[0-9]{2}\$" <<< "$BODY15" \
  && ok "T15B job lines keep the description-only shape" \
  || bad "T15B job lines (got: $BODY15)"
# A pre-block queue line (name only, no tab) left by a mid-update burst.
reset_push
mkdir -p "$NOTICE_DIR"
printf '%s\n' "$NAME_C" > "$NOTICE_DIR/queue"
DOGANY_START_NOTICE_WINDOW=0 "$DD" --start-notice-flush >/dev/null 2>&1
BODY15L="$(first_msg)"
[[ "$BODY15L" == "▶️ 백그라운드 작업 시작"$'\n'"- $NAME_C" ]] \
  && ok "T15C a legacy tab-less line renders bare under the background title" \
  || bad "T15C legacy line (got: $BODY15L)"

echo "== T14. DGN-1727: codex rows are unchanged (no wait path) =="
ARM_FN="$(awk '/^start_notice_arm\(\)/,/^}/' "$WRAPPER")"
grep -q '"\$RUNTIME" == "claude-code"' <<< "$ARM_FN" \
  && ok "T14A only claude-code runs queue a pending-model line" \
  || bad "T14A pending-model queueing is claude-code only"

echo "== T16-T18. DGN-1904: start and end are ONE pair under --notify =="
# rundir_of <spawn stdout> -- the run dir from the spawn's "logs:" line.
rundir_of() { sed -n 's/^\[dispatch\]   logs:   \(.*\)\/out\.log$/\1/p' <<< "$1" | head -1; }
# wait_done <rundir> -- until finalize has written a terminal status.json.
wait_done() {
  local i
  for i in $(seq 1 40); do
    [[ -d "$1/.finalized" ]] && { sleep 1; return 0; }
    sleep 1
  done
  return 1
}
# wait_quiet -- until every earlier run has finalized (T11-T13 runs sleep
# 20-40s; their END pushes must not land inside the window measured below).
wait_quiet() {
  local i d busy
  for i in $(seq 1 60); do
    busy=0
    for d in "$WS/.telegram_bot/dispatch"/dsp-*; do
      [[ -d "$d" && ! -d "$d/.finalized" ]] && busy=1
    done
    [[ $busy -eq 0 ]] && { sleep 1; return 0; }
    sleep 1
  done
  return 1
}
INBOX="$WS/.telegram_bot/session-inbox"
NAME_S="조용한 점검 실행"

# T16: silent -> ZERO owner pushes, start or end; the inbox drop still lands.
wait_quiet || bad "T16 earlier runs never finalized"
reset_push
OUT16="$(spawn "$NAME_S" --notify silent 2>"$TDIR/t16.err")"
RC=$?
RD16="$(rundir_of "$OUT16")"
[[ $RC -eq 0 && -n "$RD16" ]] && ok "T16A a silent dispatch spawns" \
  || bad "T16A silent dispatch spawns (rc=$RC, $(cat "$TDIR/t16.err"))"
grep -q "start notice: none (--notify silent" <<< "$OUT16" \
  && ok "T16B the spawn line says no start notice is armed for a silent run" \
  || bad "T16B spawn line names the silent skip (got: $OUT16)"
[[ ! -d "$NOTICE_DIR/.flusher" ]] \
  && ok "T16C a silent run arms no sender" || bad "T16C a silent run armed a sender"
wait_done "$RD16" || bad "T16 run never finalized"
sleep "$(( DOGANY_START_NOTICE_WINDOW + 3 ))"
[[ "$(all_count)" == "0" ]] \
  && ok "T16D silent run -> zero owner pushes (no start, no end)" \
  || bad "T16D silent run pushed the owner (got $(all_count): $(cat "$PUSH_LOG"))"
grep -qF "$NAME_S" "$PUSH_LOG" \
  && bad "T16D2 no owner push ever names the silent run" \
  || ok "T16D2 no owner push ever names the silent run"
RID16="$(basename "$RD16")"
[[ -f "$INBOX/dispatch-$RID16.md" ]] \
  && ok "T16E the session-inbox completion drop is unchanged" \
  || bad "T16E session-inbox drop missing for $RID16"
grep -q '"notify": "skipped-silent"' "$RD16/status.json" \
  && ok "T16F status.json records the skipped end push" \
  || bad "T16F status.json notify state (got: $(grep notify "$RD16/status.json"))"

# T17: default push -> exactly one start and one end, same run name.
reset_push
OUT17="$(spawn "$NAME_B" 2>/dev/null)"
RD17="$(rundir_of "$OUT17")"
wait_flush || bad "T17 sender never flushed"
wait_done "$RD17" || bad "T17 run never finalized"
BODY17="$(first_msg)"
[[ "$(msg_count)" == "1" && "$(end_count)" == "1" && "$(all_count)" == "2" ]] \
  && ok "T17A push run -> one start + one end, nothing else" \
  || bad "T17A push pair (start=$(msg_count) end=$(end_count) all=$(all_count))"
grep -qF "$NAME_B" <<< "$BODY17" && grep -A1 '^✅ ' "$PUSH_LOG" | grep -qF "$NAME_B" \
  && ok "T17B start and end name the same run" \
  || bad "T17B pair names the run (got: $(cat "$PUSH_LOG"))"

# T18: mixed burst push / silent / push -> (2건), the silent run absent.
reset_push
OUT18A="$(spawn "$NAME_A" 2>/dev/null)"
OUT18B="$(spawn "$NAME_S" --notify silent 2>/dev/null)"
OUT18C="$(spawn "$NAME_C" 2>/dev/null)"
wait_flush || bad "T18 sender never flushed"
for o in "$OUT18A" "$OUT18B" "$OUT18C"; do wait_done "$(rundir_of "$o")" || bad "T18 run never finalized"; done
BODY18="$(first_msg)"
[[ "$(msg_count)" == "1" && "$(head -1 <<< "$BODY18")" == "▶️ 백그라운드 작업 시작 (2건)" ]] \
  && ok "T18A mixed burst -> ONE start message counting only the 2 push runs" \
  || bad "T18A mixed burst title (got $(msg_count): $BODY18)"
grep -qF "$NAME_S" <<< "$BODY18" \
  && bad "T18B the silent run is absent from the start message" \
  || ok "T18B the silent run is absent from the start message"
grep -qF "$NAME_A" <<< "$BODY18" && grep -qF "$NAME_C" <<< "$BODY18" \
  && ok "T18C both push runs are listed" || bad "T18C push runs listed (got: $BODY18)"
[[ "$(end_count)" == "2" ]] && ! grep -qF "$NAME_S" "$PUSH_LOG" \
  && ok "T18D two end pushes, none for the silent run (pairs close 2/2)" \
  || bad "T18D end pushes (got $(end_count): $(cat "$PUSH_LOG"))"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
