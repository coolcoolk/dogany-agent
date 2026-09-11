#!/usr/bin/env bash
# test-lane: fast    # measured 0.4s (2026-09-04 full sweep, DGN-1267)
# test-dgn1192-stream-render.sh -- out.log must carry EVERY assistant message,
# not just the last one.
#
# Regression this locks: `claude -p` text mode prints only the FINAL assistant
# message. On 2026-09-01 a worker split its report across two messages and the
# first (conclusion + totals) was destroyed. A truncated report always reads as
# "this model was not good enough", so the loss also poisons tier measurement
# (DGN-1193) in the expensive direction.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/../../routines/dispatch-detached.sh"
[[ -f "$TARGET" ]] || { echo "FAIL: dispatch-detached.sh not found at $TARGET"; exit 1; }

PASS=0; FAIL=0
ok()  { echo "  ok   -- $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL -- $1"; FAIL=$((FAIL+1)); }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
RUNDIR="$WORK/run"; mkdir -p "$RUNDIR"

# Load only the renderer (the script is not sourceable end-to-end).
sed -n '/^render_out_log() {$/,/^}$/p' "$TARGET" > "$WORK/render.sh"
[[ -s "$WORK/render.sh" ]] || { echo "FAIL: render_out_log not found"; exit 1; }
# shellcheck disable=SC1090
source "$WORK/render.sh"

# --- T1: two assistant messages -> BOTH survive -------------------------------
cat > "$RUNDIR/stream.jsonl" <<'JSONL'
{"type":"system","subtype":"init"}
{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"NOT_REPORT_CONTENT"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"FIRST_MESSAGE_CONCLUSION"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"SECOND_MESSAGE_TABLE"}]}}
{"type":"result","subtype":"success"}
JSONL
: > "$RUNDIR/claude.err"
render_out_log
grep -q "FIRST_MESSAGE_CONCLUSION" "$RUNDIR/out.log" && ok "T1a first message survives (the 2026-09-01 loss)" || bad "T1a first message LOST"
grep -q "SECOND_MESSAGE_TABLE"     "$RUNDIR/out.log" && ok "T1b last message survives"  || bad "T1b last message lost"
grep -q "NOT_REPORT_CONTENT"       "$RUNDIR/out.log" && bad "T1c thinking leaked into report" || ok "T1c thinking excluded"
# order preserved
if [[ "$(grep -n 'FIRST_MESSAGE_CONCLUSION' "$RUNDIR/out.log" | cut -d: -f1)" -lt \
      "$(grep -n 'SECOND_MESSAGE_TABLE'     "$RUNDIR/out.log" | cut -d: -f1)" ]]; then
  ok "T1d order preserved"; else bad "T1d order scrambled"; fi

# --- T2: unparseable stream -> fall back, never destroy evidence ---------------
printf 'this is not json at all\nneither is this\n' > "$RUNDIR/stream.jsonl"
printf 'boom: worker died\n' > "$RUNDIR/claude.err"
render_out_log
grep -q "this is not json at all" "$RUNDIR/out.log" && ok "T2a raw stream preserved on parse failure" || bad "T2a evidence destroyed"
grep -q "boom: worker died"       "$RUNDIR/out.log" && ok "T2b stderr preserved"                     || bad "T2b stderr lost"

# --- T3: empty stream -> stderr still reaches out.log -------------------------
: > "$RUNDIR/stream.jsonl"
printf 'startup failure\n' > "$RUNDIR/claude.err"
render_out_log
grep -q "startup failure" "$RUNDIR/out.log" && ok "T3 stderr-only run still produces evidence" || bad "T3 silent empty out.log"

# --- T4: wiring -- the flags and the call must both be present ----------------
grep -q -- "--output-format stream-json --verbose" "$TARGET" && ok "T4a stream-json flags wired" || bad "T4a flags missing"
grep -q '> "\$RUNDIR/stream.jsonl"' "$TARGET" && ok "T4b stdout captured to stream.jsonl" || bad "T4b stdout still to out.log"
[[ "$(grep -c '^\s*render_out_log' "$TARGET")" -ge 2 ]] && ok "T4c render called on normal AND cancel path" || bad "T4c render not wired on both paths"

echo
echo "PASS $PASS  FAIL $FAIL"
[[ $FAIL -eq 0 ]]
