#!/usr/bin/env bash
# test-lane: slow    # measured 19.5s (2026-09-04 full sweep, DGN-1267)
# test-dgn1218-dispatch-gate.sh -- bottleneck-axis gate + calibration reader.
#
# DGN-1218 (parent DGN-1214): the dispatch gate must ask WHICH task property
# selects a tier, and DGN-1485 requires one line of task evidence for that
# answer. Both are machine-checkable (a written rule runs at 3.4%, DGN-1194).
# Two-layer design under test:
#   LAYER 1 (logic, provider-neutral): --bottleneck coverage|accuracy|judgment
#     is required; it names a property of the TASK, never of a model.
#   LAYER 2 (data): lib/dispatch-tier-bindings.tsv binds (runtime, axis) ->
#     measured models (DGN-1193). Mismatch -> REFUSED unless --why (recorded).
#     Missing row (unmeasured runtime) -> pass-through with a notice.
#     Missing FILE -> refused, fail-closed.
#
# The two locks this suite exists for:
#   (a) an unjustified dispatch IS REFUSED (not warned past -- warn-and-pass
#       is the 3.4% shape);
#   (b) the gate logic contains NO model names -- a provider swap (3.0) must
#       be rows in the data file, zero code. If a model name creeps into
#       tier_binding_lookup / bottleneck_gate / print_calibration, T9 fails.
#
# Run: bash routines/tests/test-dgn1218-dispatch-gate.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: fake claude stub via PATH, push/usage-gate stubs, scratch
# workspace under mktemp. No real spawn, no telegram, no live ledger.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"
BINDINGS="$REPO/routines/lib/dispatch-tier-bindings.tsv"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1218-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  chflags -R nouappend,nouchg "$TDIR" 2>/dev/null   # terminal-state ledger is uappend
  chmod -R u+w "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cp "$BINDINGS" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-model-display.tsv" "$WS/routines/lib/"
cp "$REPO/routines/lib/model_display.py" "$WS/routines/lib/"
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass"
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
echo "FAKE-CLAUDE argv: $*"
sleep "${FAKE_CLAUDE_SLEEP:-1}"
echo "FAKE-CLAUDE-DONE"
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF
git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
# DGN-1504: worktree runs need sandbox-exec (stand-ins off macOS).
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
DD="$WS/routines/dispatch-detached.sh"
printf 'gate probe task' > "$TDIR/task.md"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    # .finalized is created FIRST in finalize_run; status.json lands after
    # the close-time measurements (DGN-1504 checkout snapshot included).
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }

echo "== T1. unjustified dispatch is REFUSED (the DGN-1218 core lock) =="
"$DD" --read-only --task "$TDIR/task.md" --model fable --label t1 >/dev/null 2>"$TDIR/t1.err"
RC=$?
[[ $RC -eq 1 ]] && ok "T1A no --bottleneck -> refused (exit 1)" \
  || bad "T1A no --bottleneck -> refused (got exit $RC)"
grep -q -- "--bottleneck is required" "$TDIR/t1.err" \
  && ok "T1B refusal names the missing axis" || bad "T1B refusal names the missing axis"
grep -q "coverage" "$TDIR/t1.err" && grep -q "judgment" "$TDIR/t1.err" \
  && ok "T1C refusal teaches the discriminator (axis semantics in message)" \
  || bad "T1C refusal teaches the discriminator"
[[ ! -d "$WS/.telegram_bot/dispatch" ]] || [[ -z "$(ls "$WS/.telegram_bot/dispatch" 2>/dev/null)" ]] \
  && ok "T1D refused run left no rundir" || bad "T1D refused run left no rundir"

echo "== T2. invalid axis value refused =="
"$DD" --read-only --task "$TDIR/task.md" --model fable --bottleneck vibes --label t2 >/dev/null 2>"$TDIR/t2.err"
[[ $? -eq 1 ]] && grep -q "invalid --bottleneck" "$TDIR/t2.err" \
  && ok "T2 free-form axis ('vibes') refused -- enum only" \
  || bad "T2 free-form axis refused"

echo "== T2A. declared axis without its task rationale is refused =="
"$DD" --read-only --task "$TDIR/task.md" --model sonnet --bottleneck coverage --label t2a \
  >/dev/null 2>"$TDIR/t2a.err"
RC=$?
[[ $RC -eq 1 ]] && grep -q -- "--bottleneck-why is required" "$TDIR/t2a.err" \
  && ! grep -q "measured binding" "$TDIR/t2a.err" \
  && ok "T2A axis without rationale is refused before tier selection" \
  || bad "T2A axis without rationale is refused (rc=$RC)"

echo "== T3. axis/model mismatch refused WITH the measured binding shown =="
"$DD" --read-only --task "$TDIR/task.md" --model sonnet --bottleneck coverage \
  --bottleneck-why "all discovered files must be included" --label t3 >"$TDIR/t3.out" 2>"$TDIR/t3.err"
RC=$?
[[ $RC -eq 2 ]] && ok "T3A coverage+sonnet -> gate refusal (exit 2)" \
  || bad "T3A coverage+sonnet -> gate refusal (got exit $RC)"
grep -q "measured binding    : opus,fable" "$TDIR/t3.err" \
  && ok "T3B refusal shows the measured binding (from data, not code)" \
  || bad "T3B refusal shows the measured binding"
grep -q -- "--why" "$TDIR/t3.err" \
  && ok "T3C refusal names the override path (--why, recorded)" \
  || bad "T3C refusal names the override path"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 0)"
[[ "${COUNT:-0}" == "0" ]] && ok "T3D no active-count slot held by a refused spawn" \
  || bad "T3D no active-count slot held (got $COUNT)"

echo "== T4. over-tiering is also a mismatch (fable on an accuracy task) =="
"$DD" --read-only --task "$TDIR/task.md" --model fable --bottleneck accuracy \
  --bottleneck-why "each supplied result requires exact verification" --label t4 >/dev/null 2>"$TDIR/t4.err"
[[ $? -eq 2 ]] && grep -q "measured binding    : sonnet" "$TDIR/t4.err" \
  && ok "T4 accuracy+fable refused -- the gate pushes DOWN, not only up" \
  || bad "T4 accuracy+fable refused"

echo "== T5. --why override passes AND is recorded verbatim (run.env + status.json) =="
WHY_TEXT="disk truth needs cross-checking beyond given items"
AXIS_WHY_TEXT="disk evidence requires complete source enumeration"
OUT="$(FAKE_CLAUDE_SLEEP=5 "$DD" --task "$TDIR/task.md" --model sonnet --bottleneck coverage \
  --bottleneck-why "$AXIS_WHY_TEXT" \
  --why "$WHY_TEXT" --label t5 --worktree t5 --timeout 2 --notify silent)"
RC=$?
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
[[ $RC -eq 0 && -n "$RUNID" ]] && ok "T5A mismatch + --why -> spawned" \
  || bad "T5A mismatch + --why -> spawned (rc=$RC)"
grep -q "tier-binding OVERRIDE" <<< "$OUT" \
  && ok "T5B override is announced, never silent" || bad "T5B override announced"
grep -q "^BOTTLENECK=coverage$" "$RUNDIR/run.env" \
  && ok "T5C run.env records BOTTLENECK" || bad "T5C run.env records BOTTLENECK"
grep -q "^RUNTIME=claude-code$" "$RUNDIR/run.env" \
  && ok "T5D run.env records RUNTIME (cross-provider ledger axis)" \
  || bad "T5D run.env records RUNTIME"
( source "$RUNDIR/run.env" 2>/dev/null && [[ "$TIER_WHY" == "$WHY_TEXT" ]] ) \
  && ok "T5E run.env records --why verbatim" || bad "T5E run.env records --why verbatim"
( source "$RUNDIR/run.env" 2>/dev/null && [[ "$BOTTLENECK_WHY" == "$AXIS_WHY_TEXT" ]] ) \
  && ok "T5F run.env records --bottleneck-why verbatim" || bad "T5F run.env records --bottleneck-why verbatim"
# Running-row note carries axis= (checked BEFORE finalize replaces the note
# with the end-state text).
grep "dsp/t5" "$WS/product/auto-loop-ledger.md" | grep -q "axis=coverage" \
  && ok "T5H ledger running note carries axis=" || bad "T5H ledger running note carries axis="
if wait_finalized "$RUNDIR" 30; then
  python3 - "$RUNDIR/status.json" "$WHY_TEXT" "$AXIS_WHY_TEXT" <<'PY' && ok "T5G status.json carries bottleneck evidence" || bad "T5G status.json carries bottleneck evidence"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["bottleneck"] == "coverage", d
assert d["runtime"] == "claude-code", d
assert d["tier_why"] == sys.argv[2], d
assert d["bottleneck_why"] == sys.argv[3], d
PY
else
  bad "T5G status.json carries fields (run never finalized)"
fi

echo "== T6. bound axis+model passes without --why =="
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --bottleneck-why "given checks need exact pass fail verification" \
  --label t6 --worktree t6 --timeout 2 --notify silent)"
RC=$?
RUNID="$(runid_of "$OUT")"
[[ $RC -eq 0 && -n "$RUNID" ]] && ok "T6 accuracy+sonnet (bound) spawns cleanly" \
  || bad "T6 accuracy+sonnet spawns (rc=$RC)"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID" 30 || bad "T6 finalize"

echo "== T6b. accuracy binding accepts opus without --why (effort-bench 2026-09-26) =="
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model opus --bottleneck accuracy \
  --bottleneck-why "given checks need exact pass fail verification" \
  --label t6b --worktree t6b --timeout 2 --notify silent 2>&1)"
RC=$?
RUNID="$(runid_of "$OUT")"
[[ $RC -eq 0 && -n "$RUNID" ]] && ! grep -q "OVERRIDE" <<< "$OUT" \
  && ok "T6B accuracy+opus (bound) spawns without --why" \
  || bad "T6B accuracy+opus spawns without --why (rc=$RC)"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID" 30 || bad "T6B finalize"

echo "== T7. haiku exclusion is DATA-driven (no row anywhere -> --why required) =="
"$DD" --read-only --task "$TDIR/task.md" --model haiku --bottleneck accuracy \
  --bottleneck-why "given checks need exact pass fail verification" --label t7 >/dev/null 2>"$TDIR/t7.err"
[[ $? -eq 2 ]] && ok "T7 haiku refused via data absence (ladder exclusion, DGN-1193)" \
  || bad "T7 haiku refused via data absence"

echo "== T8. unmeasured runtime: no row -> pass-through with notice (3.0: never block) =="
# DGN-1444 (2026-09-13): "codex" is no longer a placeholder for "some future
# unmeasured runtime" -- it is now a real, implemented runtime with its own
# tier-binding rows AND its own observe-mode refusal (no --worktree here), so
# it can no longer stand in for this scenario. "antigravity" remains
# genuinely row-free in dispatch-tier-bindings.tsv (2026-09-02 owner decision:
# multi-provider build-out is 3.0) and dispatch-detached.sh has no
# antigravity-specific branch, so it still exercises the SAME generic
# pass-through-with-notice path this test is actually about.
OUT="$(FAKE_CLAUDE_SLEEP=1 DOGANY_DISPATCH_RUNTIME=antigravity "$DD" --task "$TDIR/task.md" \
  --model sonnet --bottleneck coverage --bottleneck-why "all discovered files must be included" \
  --label t8 --worktree t8 --timeout 2 --notify silent)"
RC=$?
RUNID="$(runid_of "$OUT")"
[[ $RC -eq 0 ]] && grep -q "no row for (runtime=antigravity" <<< "$OUT" \
  && ok "T8A unmeasured runtime passes with an explicit notice" \
  || bad "T8A unmeasured runtime passes with notice (rc=$RC)"
grep -q "^RUNTIME=antigravity$" "$WS/.telegram_bot/dispatch/$RUNID/run.env" \
  && ok "T8B run.env records the foreign runtime (future measurement possible)" \
  || bad "T8B run.env records the foreign runtime"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID" 30 || bad "T8 finalize"

echo "== T9. NO MODEL NAMES in gate logic -- the 3.0 rip-out lock =="
for fn in tier_binding_lookup bottleneck_gate print_calibration; do
  awk "/^${fn}\(\)/,/^}/" "$WRAPPER" > "$TDIR/fn-$fn.txt"
  [[ -s "$TDIR/fn-$fn.txt" ]] || bad "T9 could not extract $fn from wrapper"
done
if grep -hEi 'fable|opus|sonnet|haiku' "$TDIR"/fn-*.txt >"$TDIR/t9.hits" 2>/dev/null; then
  bad "T9A model name hardcoded in gate/reader logic: $(head -3 "$TDIR/t9.hits")"
else
  ok "T9A gate + reader logic are model-name-free (Layer 1/2 split holds)"
fi
grep -q "$(printf 'claude-code\tcoverage\topus,fable')" "$BINDINGS" \
  && grep -q "$(printf 'claude-code\taccuracy\tsonnet,opus')" "$BINDINGS" \
  && grep -q "$(printf 'claude-code\tjudgment\topus,fable')" "$BINDINGS" \
  && ok "T9B DGN-1193 rule text lives in the DATA file (single source)" \
  || bad "T9B DGN-1193 rule text lives in the DATA file"

echo "== T10. missing data FILE -> fail-closed refusal =="
mv "$WS/routines/lib/dispatch-tier-bindings.tsv" "$TDIR/bindings.bak"
"$DD" --read-only --task "$TDIR/task.md" --model fable --bottleneck judgment \
  --bottleneck-why "the design tradeoff requires an explicit verdict" --label t10 >/dev/null 2>"$TDIR/t10.err"
[[ $? -eq 2 ]] && grep -q "data file missing" "$TDIR/t10.err" \
  && ok "T10 absent bindings file refuses (a lost file must not disarm the gate)" \
  || bad "T10 absent bindings file refuses"
mv "$TDIR/bindings.bak" "$WS/routines/lib/dispatch-tier-bindings.tsv"

echo "== T11. --why below minimum length refused =="
"$DD" --read-only --task "$TDIR/task.md" --model sonnet --bottleneck coverage --why "x" --label t11 \
  --bottleneck-why "all discovered files must be included" \
  >/dev/null 2>"$TDIR/t11.err"
[[ $? -eq 1 ]] && grep -q -- "--why too short" "$TDIR/t11.err" \
  && ok "T11 one-char --why refused (speed-bump against the reflex override)" \
  || bad "T11 one-char --why refused"

echo "== T12. calibration reader: history is actually readable and shown =="
# Fabricate an OLD-STYLE finalized run (pre-DGN-1218 status.json, no axis
# fields) plus the T5/T6/T8 real runs above.
OLDDIR="$WS/.telegram_bot/dispatch/dsp-20260101-000000-0001"
mkdir -p "$OLDDIR"
cat > "$OLDDIR/status.json" <<'EOF'
{"runid": "dsp-20260101-000000-0001", "label": "legacy-run", "ticket": "DGN-1", "model": "fable",
 "result": "completed", "exit_code": 0, "branch": "", "commits": 0, "head": "",
 "started_at": "2026-01-01T00:00:00+0900", "ended_at": "2026-01-01T00:17:00+0900",
 "timeout_min": 60, "notify": "skipped-silent", "out_log": "/nonexistent", "session_inbox": "dropped"}
EOF
printf 'RUNID=dsp-20260101-000000-0001\nLABEL=legacy-run\nMODEL=fable\n' > "$OLDDIR/run.env"
CAL="$("$DD" --calibration)"
grep -q "recent dispatches" <<< "$CAL" \
  && ok "T12A --calibration prints history" || bad "T12A --calibration prints history"
grep -q "legacy-run" <<< "$CAL" \
  && ok "T12B pre-gate runs render (axis column degrades to '-')" \
  || bad "T12B pre-gate runs render"
grep -q "coverage" <<< "$CAL" \
  && ok "T12C axis column populated for post-gate runs" \
  || bad "T12C axis column populated"
grep -qE "by model: .*(sonnet|fable)" <<< "$CAL" \
  && ok "T12D model distribution line present (the bias mechanism)" \
  || bad "T12D model distribution line present"
# The reader must fire ON the spawn path -- refusal included -- so the
# re-decision happens with the data on screen.
"$DD" --read-only --task "$TDIR/task.md" --model sonnet --bottleneck coverage \
  --bottleneck-why "all discovered files must be included" --label t12 \
  >"$TDIR/t12.out" 2>/dev/null
grep -q "recent dispatches" "$TDIR/t12.out" \
  && ok "T12E history is printed on the (refused) spawn path" \
  || bad "T12E history is printed on the refused spawn path"

echo "== T13. old run.env backward compat: --list survives axis-free run.env =="
"$DD" --list > "$TDIR/t13.out" 2>&1 \
  && ok "T13 --list runs over a pre-gate run.env (set -u safe)" \
  || bad "T13 --list runs over a pre-gate run.env"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
