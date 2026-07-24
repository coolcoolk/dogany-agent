#!/usr/bin/env bash
# test-status-footer.sh -- unit + integration tests for routines/status-footer.py
#
# DGN-453 direction A: footer is always suppressed.  The pinned dashboard.md
# is the sole surface for [결정대기]/live display.  The hook regenerates
# dashboard.md on every owner Stop and writes an empty sidecar (no
# message-level footer appended).
#
# DGN-541 S1 conditional display: dashboard.md is filled ONLY when
# (pending decisions >= 1) OR (working subagents >= 1).  A CONFIRMED empty
# board writes an EMPTY dashboard.md (bridge delete state machine takes the
# pin down); a collector FAILURE preserves the previous content (fail-open
# empty-ban -- a false-empty must never escalate into a pin delete).
# DGN-541 S2/S3 + Rev 9 (framework promotion): instance display tokens
# resolve env (DOGANY_CONSOLE_BASE / DOGANY_LIVE_LABEL / DOGANY_BOARD_EMOJI)
# -> config/agent.conf (DASHBOARD_CONSOLE_BASE / DASHBOARD_LIVE_LABEL /
# DASHBOARD_EMOJI) -> neutral default (label "서브에이전트 작업 중", title
# "작업대" with no emoji prefix).
#
# Scenarios:
#   1. Active subagent -> board filled, neutral live label + bare title.
#   2. DOGANY_LIVE_LABEL env override -> custom live label rendered.
#   3. Pending decision (no live agent) -> board filled with [결정대기] item.
#   4. Confirmed empty (completed agent, no decisions) -> EMPTY dashboard.md.
#   5. Collector failure (unreadable decisions source) -> previous board
#      content preserved (fail-open empty-ban).
#   6. Collector failure (missing transcript) -> previous content preserved.
#   7. Non-owner session -> no sidecar written, dashboard untouched.
#   8. config/agent.conf fallback -> conf label + conf emoji title; env still
#      beats conf when both are set.
#
# Run:  bash routines/tests/test-status-footer.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FOOTER_PY="$ROUTINES_DIR/status-footer.py"
PYTHON=/usr/bin/python3

PASS=0
FAIL=0
SKIP=0

ok()   { printf "  PASS: %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  FAIL: %s\n" "$1"; FAIL=$((FAIL + 1)); }
skip() { printf "  SKIP: %s\n" "$1"; SKIP=$((SKIP + 1)); }

assert_eq() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then ok "$desc"; else fail "$desc -- got='$got' want='$want'"; fi
}
assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- needle='$needle' not in output"; fi
}
assert_empty() {
  local desc="$1" val="$2"
  if [[ -z "$val" ]]; then ok "$desc"; else fail "$desc -- expected empty, got: $val"; fi
}

WORK="$(mktemp -d /tmp/status-footer-test.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Mock session layout.  transcript_path drives BOTH the liveness parse and
# the subagents-dir derivation (which uses expanduser("~")).  We override HOME
# to $WORK so meta.json resolution lands under the temp tree, never ~/.claude.
ENC="-Users-mock-proj"
SESS="mock-sess-0001"
PROJ_DIR="$WORK/.claude/projects/$ENC"
SUB_DIR="$PROJ_DIR/$SESS/subagents"
TRANSCRIPT="$PROJ_DIR/$SESS.jsonl"
MOCK_CWD="$WORK/cwd"
mkdir -p "$SUB_DIR" "$MOCK_CWD"

# Ownership mock (DGN-392): register mock-sess-0001 as the owner session so
# the ownership guard passes in tests.  DOGANY_SESSIONS_FILE overrides the
# default sessions.json path in _is_owner_session().
mkdir -p "$WORK/.telegram_bot"
cat > "$WORK/.telegram_bot/sessions.json" << 'EOF'
{"main": {"session_id": "mock-sess-0001"}}
EOF
export DOGANY_SESSIONS_FILE="$WORK/.telegram_bot/sessions.json"

# Isolate the dashboard write from the real workspace.
export DOGANY_BOT_DATA_DIR="$WORK/.telegram_bot"
DASHBOARD="$WORK/.telegram_bot/dashboard.md"

# Empty decisions file: no pending decisions unless a scenario overrides it.
# Prevents the real worklog/_DECISIONS.md from leaking into the mock runs.
touch "$WORK/decisions.md"
export DOGANY_DECISIONS_FILE="$WORK/decisions.md"

# Isolate the junior ledger from the real workspace: a MISSING file is the
# CONFIRMED-empty case (no auto-loop fleet), keeping the empty-write
# scenario deterministic.
export DOGANY_LEDGER_FILE="$WORK/no-ledger.md"

# Display tokens: scenarios assert the neutral defaults unless overridden,
# so the caller's environment must not leak any in.  The conf layer is
# isolated the same way: point DOGANY_AGENT_CONF at an empty mock conf so
# the real instance config/agent.conf never bleeds into the assertions.
unset DOGANY_LIVE_LABEL DOGANY_BOARD_EMOJI DOGANY_CONSOLE_BASE
: > "$WORK/agent.conf"
export DOGANY_AGENT_CONF="$WORK/agent.conf"

cat > "$SUB_DIR/agent-aaa0000111.meta.json" << 'EOF'
{"agentType":"general-purpose","description":"GCal sync task","toolUseId":"toolu_x"}
EOF

# A subagent is only ACTIVE if its jsonl is fresh (mtime < LIVE_STALE_SECS).
# Create it now = fresh.  set_mtime() below re-freshens it per scenario.
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n' \
  > "$SUB_DIR/agent-aaa0000111.jsonl"
set_mtime() {  # set_mtime <file> <epoch_seconds>
  "$PYTHON" - "$1" "$2" <<'PY'
import os, sys
os.utime(sys.argv[1], (float(sys.argv[2]), float(sys.argv[2])))
PY
}
freshen() { set_mtime "$SUB_DIR/agent-aaa0000111.jsonl" "$("$PYTHON" -c 'import time;print(int(time.time()))')"; }

REAL_HOME="$HOME"
export HOME="$WORK"

run_footer() {
  local json_input="$1"
  local rc=0
  output="$( "$PYTHON" "$FOOTER_PY" <<< "$json_input" )" || rc=$?
  echo "$output"
  return $rc
}

launch_line() {
  local aid="$1"
  printf '{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"Async agent launched successfully.\\nagentId: %s (internal ID)"}]}]}}' "$aid"
}
compl_line() {
  local aid="$1"
  printf '{"type":"user","message":{"content":[{"type":"text","text":"<task-notification>\\n<task-id>%s</task-id>\\n<status>completed</status>\\n<summary>came to rest</summary>"}]}}' "$aid"
}
make_input() {
  local sha="${1:-false}"
  printf '{"session_id":"s1","transcript_path":"%s","cwd":"%s","hook_event_name":"Stop","stop_hook_active":%s}' \
    "$TRANSCRIPT" "$MOCK_CWD" "$sha"
}

echo ""
echo "=== status-footer.py self-tests (DGN-541: conditional display) ==="
echo ""

# ---- scenario 1: active subagent -> board filled, neutral label ------------
echo "--- scenario 1: active subagent -> dashboard filled, neutral live label ---"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
assert_empty "no stdout (footer always suppressed)" "$out"
if [[ -s "$DASHBOARD" ]]; then
  ok "dashboard.md filled (live agent = display trigger)"
  content="$(cat "$DASHBOARD")"
  assert_contains "neutral live label (env unset)" "$content" '[서브에이전트 작업 중]'
  assert_contains "agent description rendered" "$content" 'GCal sync task'
  assert_contains "freshness stamp present" "$content" '갱신 '
  assert_eq "bare board title (no emoji when env/conf unset)" \
    "$(head -1 "$DASHBOARD")" "작업대"
else
  fail "dashboard.md missing or empty despite live agent"
fi
echo ""

# ---- scenario 2: DOGANY_LIVE_LABEL override -> custom label (S3) ----------
echo "--- scenario 2: DOGANY_LIVE_LABEL env -> custom live label ---"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
out="$(DOGANY_LIVE_LABEL='CUSTOM-LIVE-LABEL' "$PYTHON" "$FOOTER_PY" <<< "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "custom live label rendered" "$content" '[CUSTOM-LIVE-LABEL]'
if [[ "$content" == *'[서브에이전트 작업 중]'* ]]; then
  fail "neutral default still rendered despite env override"
else
  ok "neutral default replaced by env override"
fi
echo ""

# ---- scenario 3: pending decision only -> board filled ---------------------
echo "--- scenario 3: pending decision (no live agent) -> dashboard filled ---"
cat > "$WORK/decisions.md" << 'EOF'
- [2026-07-23] [dec-999] Sample decision
EOF
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "dashboard contains [결정대기] section" "$content" '[결정대기]'
assert_contains "dashboard contains decision item" "$content" 'dec-999'
: > "$WORK/decisions.md"
echo ""

# ---- scenario 4: confirmed empty -> EMPTY dashboard write (S1) -------------
echo "--- scenario 4: confirmed empty board -> empty dashboard.md written ---"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
if [[ -f "$DASHBOARD" && ! -s "$DASHBOARD" ]]; then
  ok "dashboard.md written EMPTY (confirmed-empty board)"
else
  fail "dashboard.md not empty -- got: $(cat "$DASHBOARD" 2>/dev/null | head -3)"
fi
echo ""

# ---- scenario 5: collector failure -> preserve previous content ------------
echo "--- scenario 5: decisions source unreadable -> previous content preserved ---"
printf 'PRESERVED-CONTENT\n' > "$DASHBOARD"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
# Point the decisions source at a DIRECTORY: open() fails with a non-
# FileNotFoundError -> collector returns failure, not confirmed-empty.
mkdir -p "$WORK/decisions-as-dir"
out="$(DOGANY_DECISIONS_FILE="$WORK/decisions-as-dir" "$PYTHON" "$FOOTER_PY" <<< "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
assert_eq "previous dashboard content preserved (fail-open empty-ban)" \
  "$(cat "$DASHBOARD" 2>/dev/null)" "PRESERVED-CONTENT"
echo ""

# ---- scenario 6: missing transcript -> preserve previous content -----------
echo "--- scenario 6: missing transcript -> previous content preserved ---"
printf 'PRESERVED-CONTENT-2\n' > "$DASHBOARD"
rm -f "$TRANSCRIPT"
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
assert_eq "previous dashboard content preserved (unreadable transcript)" \
  "$(cat "$DASHBOARD" 2>/dev/null)" "PRESERVED-CONTENT-2"
echo ""

# ---- scenario 7: non-owner session -> no sidecar, dashboard untouched ------
echo "--- scenario 7: non-owner session -> no output, no sidecar ---"
printf 'PRESERVED-CONTENT-3\n' > "$DASHBOARD"
nonowner_input="$(printf '{"session_id":"s1","transcript_path":"","cwd":"%s","hook_event_name":"Stop","stop_hook_active":false}' "$MOCK_CWD")"
rm -f "$WORK/.telegram_bot/footer-sidecar.json" 2>/dev/null
out="$(run_footer "$nonowner_input")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
assert_empty "no output (non-owner session)" "$out"
if [[ ! -f "$WORK/.telegram_bot/footer-sidecar.json" ]]; then
  ok "sidecar not written for non-owner session"
else
  fail "sidecar was written for non-owner session"
fi
assert_eq "dashboard untouched for non-owner session" \
  "$(cat "$DASHBOARD" 2>/dev/null)" "PRESERVED-CONTENT-3"
echo ""

# ---- scenario 8: config/agent.conf fallback (Rev 9) ------------------------
echo "--- scenario 8: agent.conf fallback -> conf label + emoji title; env wins ---"
cat > "$WORK/agent.conf" << 'EOF'
DASHBOARD_EMOJI=⭐
DASHBOARD_LIVE_LABEL=CONF-LIVE-LABEL
EOF
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "exit code 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "conf live label rendered (env unset)" "$content" '[CONF-LIVE-LABEL]'
assert_eq "conf emoji prefixes the board title" \
  "$(head -1 "$DASHBOARD")" "⭐ 작업대"
out="$(DOGANY_LIVE_LABEL='ENV-LIVE-LABEL' "$PYTHON" "$FOOTER_PY" <<< "$(make_input false)")"; rc=$?
assert_eq "exit code 0 (env + conf both set)" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "env beats conf for the live label" "$content" '[ENV-LIVE-LABEL]'
: > "$WORK/agent.conf"
echo ""

echo "==========================="
printf "Results: %d passed, %d failed, %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "==========================="
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
