#!/usr/bin/env bash
# test-lane: slow    # spawns fake claude/codex workers and waits for finalize
unset DOGANY_BRIDGE_SESSION  # DGN-1688: hermetic
# test-dgn1770-effort-binding.sh -- effort resolves from DATA like the model.
#
# DGN-1770: --effort omitted -> lib/dispatch-effort-bindings.tsv decides for
# the FINAL (runtime, axis, model) and the source is recorded (run.env,
# status.json, spawn banner), mirroring MODEL_SOURCE (DGN-1666).
#   F1-F9  function level (DISPATCH_TEST sourcing): table / explicit /
#          unmeasured / no-effect / missing table / pass-through rule /
#          sonnet+high accuracy warning.
#   D1-D4  data guards: every cell is a legal level, every row's model is a
#          bound model of its tier row, no judgment and no cloud rows; the
#          lookup functions carry no model name (DGN-1218 T9 rule).
#   E1-E4  spawn E2E with fake CLIs: claude gets the table effort, explicit
#          wins, unmeasured passes no flag, codex gets -c model_reasoning_effort.
#
# Safe/offline: fake claude + codex via PATH, usage/quota/hook stubs, scratch
# git repo under mktemp. No real spawn, no network, no live ledger.
#
# Run: bash routines/tests/test-dgn1770-effort-binding.sh

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1770-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
for f in ledger-update.sh dispatch-tier-bindings.tsv dispatch-effort-bindings.tsv \
         model_display.py dispatch-model-display.tsv; do
  cp "$REPO/routines/lib/$f" "$WS/routines/lib/"
done
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
echo "USAGE_GATE: pass five_hour=10 seven_day=10"
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$WS/routines/codex-ratelimit.py" <<'EOF'
#!/usr/bin/env python3
import json, sys
if "--record-failure" in sys.argv:
    sys.exit(0)
print(json.dumps({"ok": True, "used_percent": 10}))
EOF
cat > "$WS/routines/codex-hook-verify.py" <<'EOF'
#!/usr/bin/env python3
print("stub: hooks trusted")
EOF
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
echo "FAKE-CLAUDE argv: $*"
sleep 1
echo "FAKE-CLAUDE-DONE"
EOF
cat > "$TDIR/bin/codex" <<'EOF'
#!/bin/bash
echo "FAKE-CODEX argv: $*" >> "${FAKE_CODEX_ARGV_LOG:?}"
echo '{"type":"item.completed","item":{"type":"agent_message","text":"FAKE-CODEX-DONE"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
EOF
chmod +x "$WS"/routines/*.sh "$WS"/routines/*.py "$WS/routines/lib/ledger-update.sh" \
  "$TDIR/bin/claude" "$TDIR/bin/codex"
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
source "$HARNESS_DIR/fixtures/sandbox-standins.sh"
install_sandbox_standins "$TDIR/bin"
# A worker that already runs inside a seatbelt (a detached dispatch) cannot
# apply a nested one: sandbox_apply fails and every claude-code spawn refuses
# before argv exists. These checks are about effort argv/records, not about
# confinement (test-dispatch-observe.sh owns that), so fall back to the same
# seatbelt-model stand-in the fixture installs on non-macOS hosts.
printf '(version 1)(allow default)\n' > "$TDIR/probe.sb"
if command -v sandbox-exec >/dev/null 2>&1 && ! sandbox-exec -f "$TDIR/probe.sb" /usr/bin/true 2>/dev/null; then
  printf '#!/bin/sh\nexec python3 "%s" exec "$@"\n' "$HARNESS_DIR/fixtures/sandbox_exec_model.py" > "$TDIR/bin/sandbox-exec"
  chmod +x "$TDIR/bin/sandbox-exec"
  echo "  note: nested seatbelt refused by this host -> seatbelt-model stand-in for the E section"
fi
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
export FAKE_CODEX_ARGV_LOG="$TDIR/codex-argv.log"
unset DOGANY_DISPATCH_MAX_ACTIVE DOGANY_DISPATCH_RUNTIME 2>/dev/null || true
DD="$WS/routines/dispatch-detached.sh"
printf 'effort task\n' > "$TDIR/task.md"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" && -f "$1/status.json" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }
status_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], "<absent>"))' "$1" "$2" 2>/dev/null
}

# resolve <runtime> <axis> <model> <explicit-effort> -> "EFFORT|SOURCE|WARNING"
resolve() {
  local rt="$1" ax="$2" m="$3" e="$4"
  (
    cd "$TDIR"
    export DISPATCH_TEST=1
    set --
    # shellcheck disable=SC1090
    source "$DD"
    RUNTIME="$rt"; BOTTLENECK="$ax"; MODEL="$m"; EFFORT="$e"
    [[ -n "${EFFORT_TABLE_OVERRIDE:-}" ]] && EFFORT_BINDINGS="$EFFORT_TABLE_OVERRIDE"
    resolve_effort >/dev/null
    printf '%s|%s|%s\n' "$EFFORT" "$EFFORT_SOURCE" "$EFFORT_WARNING"
  )
}

echo "== F. resolve_effort (function level) =="
out="$(resolve claude-code accuracy sonnet "")"
[[ "$out" == "low|table|" ]] && ok "F1 omitted, claude-code/accuracy/sonnet -> low, source=table" || bad "F1 got '$out'"
out="$(resolve claude-code accuracy opus "")"
[[ "$out" == "medium|table|" ]] && ok "F2 omitted, claude-code/accuracy/opus -> medium, source=table" || bad "F2 got '$out'"
out="$(resolve claude-code accuracy opus high)"
[[ "$out" == "high|explicit|" ]] && ok "F3 explicit --effort high wins over the table, source=explicit" || bad "F3 got '$out'"
out="$(resolve claude-code judgment opus "")"
[[ "$out" == "|unmeasured|" ]] && ok "F4 judgment (never measured) -> no flag, source=unmeasured" || bad "F4 got '$out'"
out="$(resolve claude-code coverage opus "")"
[[ "$out" == "low|table|" ]] && ok "F5 coverage/opus measured flat -> lowest no-loss effort low, source=table" || bad "F5 got '$out'"
printf 'claude-code\tcoverage\topus\t-\tfixture flat cell\n' > "$TDIR/flat.tsv"
out="$(EFFORT_TABLE_OVERRIDE="$TDIR/flat.tsv" resolve claude-code coverage opus "")"
[[ "$out" == "|no-effect|" ]] && ok "F5b a '-' cell -> no flag, source=no-effect" || bad "F5b got '$out'"
out="$(resolve codex accuracy fable "")"
[[ "$out" == "high|table|" ]] && ok "F6 codex/accuracy/fable(astra) -> high (owner dec-200), source=table" || bad "F6 got '$out'"
out="$(resolve cloud accuracy sonnet "")"
[[ "$out" == "|unmeasured|" ]] && ok "F7 cloud has no rows (no verified control) -> unmeasured" || bad "F7 got '$out'"
out="$(EFFORT_TABLE_OVERRIDE="$TDIR/nope.tsv" resolve claude-code accuracy sonnet "")"
[[ "$out" == "|unmeasured|" ]] && ok "F8 missing effort table degrades to CLI default, never refuses" || bad "F8 got '$out'"
out="$(resolve claude-code accuracy sonnet high)"
[[ "$out" == "high|explicit|sonnet + high on accuracy:"* ]] && ok "F9a claude sonnet+high accuracy -> warning" || bad "F9a got '$out'"
out="$(resolve cloud accuracy sonnet high)"
[[ "$out" == "high|explicit|sonnet + high on accuracy:"* ]] && ok "F9b cloud sonnet+high accuracy -> warning (same model)" || bad "F9b got '$out'"
out="$(resolve claude-code coverage sonnet high)"
[[ "$out" == "high|explicit|" ]] && ok "F9c sonnet+high off the accuracy axis -> no warning" || bad "F9c got '$out'"
out="$(resolve codex accuracy sonnet high)"
[[ "$out" == "high|explicit|" ]] && ok "F9d codex 'sonnet' alias is terra, not sonnet -> no warning" || bad "F9d got '$out'"
out="$(resolve claude-code accuracy sonnet medium)"
[[ "$out" == "medium|explicit|" ]] && ok "F9e sonnet+medium -> no warning" || bad "F9e got '$out'"

rules="$(
  cd "$TDIR"; export DISPATCH_TEST=1; set --
  # shellcheck disable=SC1090
  source "$DD"
  for c in claude-code:max codex:low codex:medium codex:high codex:max cloud:medium; do
    effort_passed_to_runtime "${c%%:*}" "${c#*:}" && printf '%s=pass ' "$c" || printf '%s=record ' "$c"
  done
)"
[[ "$rules" == "claude-code:max=pass codex:low=pass codex:medium=pass codex:high=pass codex:max=record cloud:medium=record " ]] \
  && ok "F10 pass-through rule: codex low/medium/high passed, codex max + cloud recorded only" || bad "F10 got '$rules'"

echo "== D. effort table data guards =="
EB="$REPO/routines/lib/dispatch-effort-bindings.tsv"
TB="$REPO/routines/lib/dispatch-tier-bindings.tsv"
bad_cells="$(awk -F'\t' '/^[[:space:]]*(#|$)/ {next} NF != 5 || $4 !~ /^(low|medium|high|-)$/ {print NR": "$0}' "$EB")"
[[ -z "$bad_cells" ]] && ok "D1 every row has 5 columns and a legal effort cell" || bad "D1 malformed rows: $bad_cells"
unbound="$(awk -F'\t' '
  FNR == NR { if ($0 !~ /^[[:space:]]*(#|$)/) bound[$1 FS $2] = "," $3 ","; next }
  /^[[:space:]]*(#|$)/ { next }
  index(bound[$1 FS $2], "," $3 ",") == 0 { print $1 "/" $2 "/" $3 }
' "$TB" "$EB")"
[[ -z "$unbound" ]] && ok "D2 every effort row names a model its tier row binds" || bad "D2 unbound effort rows: $unbound"
extra="$(awk -F'\t' '/^[[:space:]]*(#|$)/ {next} $2 == "judgment" || $1 == "cloud" {print $1 "/" $2 "/" $3}' "$EB")"
[[ -z "$extra" ]] && ok "D3 no judgment rows (unmeasured) and no cloud rows (no control)" || bad "D3 rows that must not exist: $extra"

for fn in effort_binding_lookup resolve_effort effort_passed_to_runtime; do
  awk "/^${fn}\\(\\)/,/^}/" "$WRAPPER" > "$TDIR/fn-$fn.txt"
  [[ -s "$TDIR/fn-$fn.txt" ]] || bad "D4 could not extract $fn from wrapper"
done
if grep -hEi 'fable|opus|sonnet|haiku' "$TDIR"/fn-*.txt >"$TDIR/d4.hits" 2>/dev/null; then
  bad "D4 model name hardcoded in effort lookup logic: $(head -3 "$TDIR/d4.hits")"
else
  ok "D4 effort lookup logic is model-name-free (the table owns the values)"
fi

echo "== E. spawn E2E (fake CLIs) =="
ARGS=(--read-only --task "$TDIR/task.md" --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" --timeout 2 --notify silent --runtime claude-code)

OUT="$("$DD" "${ARGS[@]}" --label "ef table" 2>&1)"
RUNID="$(runid_of "$OUT")"; RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" ]] && wait_finalized "$RUNDIR" 30; then
  grep -q -- "--model sonnet --effort low " "$RUNDIR/out.log" \
    && ok "E1a omitted effort: claude worker gets --effort low from the table" || bad "E1a argv: $(grep FAKE-CLAUDE "$RUNDIR/out.log" | head -1)"
  grep -q '^EFFORT_SOURCE=table$' "$RUNDIR/run.env" && ok "E1b run.env EFFORT_SOURCE=table" || bad "E1b run.env"
  [[ "$(status_field "$RUNDIR/status.json" effort_source)" == "table" && "$(status_field "$RUNDIR/status.json" effort)" == "low" ]] \
    && ok "E1c status.json effort=low effort_source=table" || bad "E1c status.json"
  grep -q "effort: low (source=table, passed as claude --effort)" <<< "$OUT" \
    && ok "E1d spawn banner names value + source" || bad "E1d banner"
else
  bad "E1 spawn/finalize: $OUT"
fi

OUT="$("$DD" "${ARGS[@]}" --model sonnet --effort high --label "ef explicit" 2>&1)"
RUNID="$(runid_of "$OUT")"; RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" ]] && wait_finalized "$RUNDIR" 30; then
  grep -q -- "--model sonnet --effort high " "$RUNDIR/out.log" && ok "E2a explicit --effort high passed" || bad "E2a argv"
  [[ "$(status_field "$RUNDIR/status.json" effort_source)" == "explicit" ]] && ok "E2b source=explicit" || bad "E2b status.json"
  grep -q "WARNING effort: sonnet + high on accuracy" <<< "$OUT" && ok "E2c sonnet+high accuracy -> WARNING on the spawn banner (spawned, not refused)" || bad "E2c banner: $OUT"
else
  bad "E2 spawn/finalize: $OUT"
fi

OUT="$("$DD" "${ARGS[@]/accuracy/judgment}" --label "ef unmeasured" 2>&1)"
RUNID="$(runid_of "$OUT")"; RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" ]] && wait_finalized "$RUNDIR" 30; then
  grep -q -- "--effort" "$RUNDIR/out.log" && bad "E3a unmeasured cell must pass no --effort" || ok "E3a unmeasured (judgment/opus) -> no --effort flag"
  [[ "$(status_field "$RUNDIR/status.json" effort_source)" == "unmeasured" && "$(status_field "$RUNDIR/status.json" effort)" == "" ]] \
    && ok "E3b status.json effort='' effort_source=unmeasured" || bad "E3b status.json"
  grep -q "effort: CLI default, no flag (source=unmeasured)" <<< "$OUT" && ok "E3c banner says unmeasured" || bad "E3c banner"
else
  bad "E3 spawn/finalize: $OUT"
fi

: > "$FAKE_CODEX_ARGV_LOG"
OUT="$("$DD" --task "$TDIR/task.md" --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" \
        --timeout 2 --notify silent --runtime codex --worktree efcodex --label "ef codex" 2>&1)"
RUNID="$(runid_of "$OUT")"; RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" ]] && wait_finalized "$RUNDIR" 30; then
  grep -q -- "-m gpt-6-astra -c model_reasoning_effort=high --json" "$FAKE_CODEX_ARGV_LOG" \
    && ok "E4a codex worker gets -c model_reasoning_effort=high (table)" || bad "E4a codex argv: $(cat "$FAKE_CODEX_ARGV_LOG")"
  [[ "$(status_field "$RUNDIR/status.json" effort_source)" == "table" ]] && ok "E4b codex run source=table" || bad "E4b status.json"
  grep -q "effort: high (source=table, passed as codex -c model_reasoning_effort)" <<< "$OUT" && ok "E4c banner names the codex control" || bad "E4c banner"
else
  bad "E4 spawn/finalize: $OUT"
fi

: > "$FAKE_CODEX_ARGV_LOG"
OUT="$("$DD" --task "$TDIR/task.md" --bottleneck accuracy --bottleneck-why "given checks need exact pass fail verification" \
        --timeout 2 --notify silent --runtime codex --worktree efcodexmax --effort max --label "ef codex max" 2>&1)"
RUNID="$(runid_of "$OUT")"; RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
if [[ -n "$RUNID" ]] && wait_finalized "$RUNDIR" 30; then
  grep -q -- "model_reasoning_effort" "$FAKE_CODEX_ARGV_LOG" && bad "E5a codex max must not be passed" || ok "E5a codex explicit max -> recorded, not passed"
  grep -q "effort: max (source=explicit) recorded but NOT passed" <<< "$OUT" && ok "E5b banner says recorded-only" || bad "E5b banner"
else
  bad "E5 spawn/finalize: $OUT"
fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
