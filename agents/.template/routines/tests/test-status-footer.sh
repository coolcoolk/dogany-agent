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
# DGN-534 T3 ([언파크 후보] derived view, worklog/_UNPARK.md read-only):
#   t3-a. fresh ledger with candidates (no other trigger) -> board filled,
#         section + stamp + items, no staleness flag.
#   t3-b. zero candidates + fresh scan + live agent -> section suppressed
#         (Rev 8 empty-section suppression).
#   t3-c. zero candidates + fresh scan, no other trigger -> confirmed-empty
#         write (a suppressed section is not a display trigger).
#   t3-d. ledger ABSENT + live agent -> zero change (canonical no-op).
#   t3-e. zero candidates + STALE scan (>26h), no other trigger -> section
#         FORCED with 오래됨 flag (M5 silent-stop detector).
#   t3-f. ledger exists but unreadable -> forced "(스캔 기록 없음)".
#   t3-g. length-cut drop priority: done -> unpark items -> live; unpark
#         header (freshness) never dropped.
#
# DGN-967 (shared pin surface -- section ownership, compose model):
#   s967-a. board confirmed-empty + domain section -> pin content survives.
#   s967-b. board + domain -> domain first, board last, stamp = last line.
#   s967-c. owners update only their own section (no cross-clobber).
#   s967-d. foreign DIRECT write adopted as legacy section, survives empty
#           board (backward compat for non-API domain writers).
#   s967-e. foreign EMPTY write clears legacy; all-empty -> HIDE unchanged.
#   s967-f. --pin-clear removes one section; last one out -> HIDE.
#   s967-g. --ttl expiry sweeps a dead owner's section at compose.
#   s967-h. reserved (board/legacy) and malformed owner slugs rejected.
#   s967-i. crash residue (sha mismatch on own artifact) not re-adopted.
#   s967-j. over-budget domain section dropped from OUTPUT, kept in STORE.
#   s967-k. parallel pin-set race -> flock-serialized, manifest valid.
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

# Isolate the unpark ledger (DGN-534 T3): a MISSING ledger is the canonical
# no-op (instances without gate-scan machinery render no section), keeping
# every pre-T3 scenario byte-identical.
export DOGANY_UNPARK_FILE="$WORK/unpark-missing.md"

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

# DGN-967: dashboard.md is composed from the section store.  Scenarios that
# deliberately seed FOREIGN direct-write content (which the compose model now
# correctly preserves as the adopted "legacy" section) must reset the whole
# surface before later scenarios can assert a clean board again.
SECTIONS="$WORK/.telegram_bot/dashboard-sections.json"
reset_surface() {
  rm -f "$DASHBOARD" "$SECTIONS" "$WORK/.telegram_bot/.dashboard-sections.lock"
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
# Scenarios 5-7 seeded foreign direct-write content; DGN-967 adoption now
# keeps it alive across board writes (by design).  Reset for a clean board.
reset_surface
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

# ===========================================================================
# DGN-534 T3: dashboard [언파크 후보] derived view (ledger read-only)
# ===========================================================================
echo "=== DGN-534 T3: dashboard unpark-candidate section ==="
echo ""

UNPARK="$WORK/unpark.md"
NOW_STAMP="$(date "+%Y-%m-%d %H:%M:%S")"

# t3-a: fresh ledger with candidates, NO agents/decisions -> the section
#        alone fills the board (M4 resident signal) and renders the items.
echo "--- t3-a: fresh ledger, 2 candidates, no other trigger -> board + section ---"
cat > "$UNPARK" << EOF
# _UNPARK -- test ledger
last-scan: $NOW_STAMP KST
candidates: 2

DGN-294-big-rock  P1  UNPARK  gate: DGN-100 done
DGN-201-old-idea  P2  COND    gate: deps done, cond remains
EOF
export DOGANY_UNPARK_FILE="$UNPARK"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "section header present (unpark = display trigger)" "$dash" '[언파크 후보]'
assert_contains "scan stamp in header" "$dash" "(스캔 ${NOW_STAMP%:*}"
assert_contains "candidate 1 rendered (whitespace squeezed)" "$dash" 'DGN-294-big-rock P1 UNPARK gate: DGN-100 done'
assert_contains "candidate 2 rendered" "$dash" 'DGN-201-old-idea P2 COND'
if [[ "$dash" == *"오래됨"* ]]; then fail "fresh stamp wrongly flagged stale"; else ok "fresh stamp not flagged stale"; fi
echo ""

# t3-b: zero candidates + fresh scan + live agent -> board up (live), but
#        the unpark section is SUPPRESSED (Rev 8 empty-section suppression).
echo "--- t3-b: empty + fresh + live agent -> section suppressed ---"
cat > "$UNPARK" << EOF
last-scan: $NOW_STAMP KST
candidates: 0
EOF
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "board filled by live agent" "$dash" '[서브에이전트 작업 중]'
if [[ "$dash" == *"언파크"* ]]; then
  fail "empty+fresh section not suppressed"
else
  ok "empty+fresh section suppressed (Rev 8)"
fi
echo ""

# t3-c: zero candidates + fresh scan, no other trigger -> confirmed-empty
#        write (a suppressed section must not hold the board up).
echo "--- t3-c: empty + fresh, no other trigger -> empty dashboard write ---"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
if [[ -f "$DASHBOARD" && ! -s "$DASHBOARD" ]]; then
  ok "suppressed section is not a display trigger (empty write)"
else
  fail "board held up by suppressed section -- got: $(head -3 "$DASHBOARD" 2>/dev/null)"
fi
echo ""

# t3-d: ledger ABSENT + live agent -> canonical no-op (zero change for
#        instances without gate-scan machinery).
echo "--- t3-d: ledger absent -> no section (canonical no-op) ---"
export DOGANY_UNPARK_FILE="$WORK/unpark-missing.md"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "board filled by live agent" "$dash" '[서브에이전트 작업 중]'
if [[ "$dash" == *"언파크"* ]]; then
  fail "absent ledger still rendered a section"
else
  ok "absent ledger renders no section"
fi
echo ""

# t3-e: zero candidates + STALE scan (>26h), no other trigger -> section
#        FORCED with staleness flag (M5 silent-stop detector survives Rev 8).
echo "--- t3-e: empty + stale scan -> forced section with 오래됨 flag ---"
OLD_STAMP="$(date -v-30H "+%Y-%m-%d %H:%M:%S" 2>/dev/null || date -d '30 hours ago' "+%Y-%m-%d %H:%M:%S")"
cat > "$UNPARK" << EOF
last-scan: $OLD_STAMP KST
candidates: 0
EOF
export DOGANY_UNPARK_FILE="$UNPARK"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "stale scan forces the section + flag" "$dash" "[언파크 후보] (스캔 ${OLD_STAMP%:*}"
assert_contains "staleness flag present" "$dash" '오래됨'
echo ""

# t3-f: ledger exists but unreadable (directory) -> forced visibility with
#        "(스캔 기록 없음)" -- a broken ledger is never silent (M5).
echo "--- t3-f: unreadable ledger -> forced 스캔 기록 없음 ---"
mkdir -p "$WORK/unpark-as-dir"
export DOGANY_UNPARK_FILE="$WORK/unpark-as-dir"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "unreadable ledger surfaces" "$dash" '[언파크 후보] (스캔 기록 없음)'
echo ""

# t3-g: length-cut drop priority -- done first, then unpark items, live kept;
#        unpark header (freshness) survives even when all its items are cut.
echo "--- t3-g: drop priority done -> unpark -> live; header never dropped ---"
out="$("$PYTHON" - "$FOOTER_PY" <<'PY'
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("sf", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
big = "x" * 250  # under the per-item cap so items shrink only by dropping
done = [big] * 3  # capped to MAX_DONE_DISPLAY internally
live = [big] * 3
unpark = (time.time(), "2026-07-24 06:00", [big] * 15)  # fresh stamp
text = m._build_dashboard(["small decision"], live, done, unpark)
assert m._u16len(text) <= m.DASHBOARD_MAX_UNITS, "over budget"
n_done = text.split("[최근 완료]")[1].count(big) if "[최근 완료]" in text else 0
n_unpark = text.split("[언파크 후보")[1].split("[최근 완료]")[0].count(big)
n_live = text.split("[%s]" % m.LIVE_LABEL)[1].split("[언파크 후보")[0].count(big)
assert n_done == 0, "done not dropped first: %d" % n_done
assert n_live == 3, "live dropped before unpark: %d" % n_live
assert 0 < n_unpark < 15, "unpark cut wrong: %d" % n_unpark
assert "[언파크 후보] (스캔 2026-07-24 06:00)" in text, "header lost"
# Tiny budget: every unpark ITEM is cut but the header must survive (a
# live-item tail may legitimately remain -- live pops AFTER unpark).
m.DASHBOARD_MAX_UNITS = 400
text2 = m._build_dashboard(["small decision"], live, done, unpark)
assert "[언파크 후보] (스캔 2026-07-24 06:00)" in text2, "header dropped"
useg2 = text2.split("[언파크 후보")[1].split("[최근 완료]")[0]
assert useg2.count(big) == 0, "unpark items survived a full cut"
print("DROPTEST-OK done=%d unpark=%d live=%d header-survives-full-cut" % (
    n_done, n_unpark, n_live))
PY
)" && rc=0 || rc=$?
assert_eq "drop-priority unit exit 0" "$rc" "0"
assert_contains "drop order verified" "$out" 'DROPTEST-OK'
echo "  $out"
echo ""

# restore default isolation for any later additions
export DOGANY_UNPARK_FILE="$WORK/unpark-missing.md"

# ===========================================================================
# DGN-536 T3b: dashboard [콘솔액션] derived view (journal read-only, Rev 11)
# ===========================================================================
echo "=== DGN-536 T3b: dashboard console-actions section (Rev 11) ==="
echo ""

CA_FILE="$WORK/decision-actions.md"
# Point all scenarios at this file unless overridden.
export DOGANY_DECISION_ACTIONS_FILE="$WORK/ca-missing.md"

# t11-a: journal ABSENT -> no section (canonical no-op).  A live agent
#         fills the board but must not include any [콘솔액션] header.
echo "--- t11-a: journal absent -> no section (canonical no-op) ---"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "board filled by live agent" "$dash" '[서브에이전트 작업 중]'
if [[ "$dash" == *"콘솔액션"* ]]; then
  fail "absent journal still rendered a [콘솔액션] section"
else
  ok "absent journal renders no section (canonical no-op)"
fi
echo ""

# t11-b: journal with ONE pending act -> section rendered with count + item.
echo "--- t11-b: pending act -> section rendered ---"
cat > "$CA_FILE" << 'EOF'
## act-1753401600 [pending]
target: dec-123
action: approve
decided_at: 2026-07-25 10:00 KST
note: approve the design
EOF
export DOGANY_DECISION_ACTIONS_FILE="$CA_FILE"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "콘솔액션 header present" "$dash" '[콘솔액션]'
assert_contains "act id in item" "$dash" 'act-1753401600'
assert_contains "action verb in item" "$dash" 'approve'
assert_contains "target in item" "$dash" 'dec-123'
assert_contains "board trigger: pending act alone fills board" "$dash" '갱신 '
echo ""

# t11-c: journal with ONLY terminal acts -> section suppressed (no pending).
#         confirmed-empty board (no other trigger) -> empty dashboard write.
echo "--- t11-c: only terminal acts -> section suppressed, empty board ---"
cat > "$CA_FILE" << 'EOF'
## act-1753401600 [applied]
target: dec-123
action: approve
decided_at: 2026-07-25 09:00 KST
EOF
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
if [[ -f "$DASHBOARD" && ! -s "$DASHBOARD" ]]; then
  ok "terminal-only journal -> suppressed section -> empty dashboard write"
else
  if [[ "$(cat "$DASHBOARD" 2>/dev/null)" == *"콘솔액션"* ]]; then
    fail "terminal-only journal rendered [콘솔액션] section"
  else
    fail "expected empty dashboard, got non-empty without콘솔액션"
  fi
fi
echo ""

# t11-d: journal with failed act (non-pending, non-terminal word) ->
#         has_failed=True -> section header forced even with zero pending.
echo "--- t11-d: failed act -> header forced (zero pending) ---"
cat > "$CA_FILE" << 'EOF'
## act-1753401601 [failed]
target: dec-456
action: reject
decided_at: 2026-07-25 08:00 KST
EOF
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "failed act forces header visible" "$dash" '[콘솔액션]'
assert_contains "forced header shows 실패 flag" "$dash" '실패 act'
echo ""

# t11-e: journal with mixed acts (1 pending + 1 applied) ->
#         only pending appears as item; has_failed from applied -> True.
echo "--- t11-e: mixed acts -> only pending in items, has_failed from terminal ---"
cat > "$CA_FILE" << 'EOF'
## act-1753401700 [applied]
target: dec-789
action: approve
decided_at: 2026-07-24 15:00 KST

## act-1753401800 [pending]
target: dec-111
action: hold
decided_at: 2026-07-25 09:30 KST
EOF
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
dash="$(cat "$DASHBOARD" 2>/dev/null || true)"
assert_contains "콘솔액션 section present" "$dash" '[콘솔액션]'
assert_contains "pending act item rendered" "$dash" 'act-1753401800'
assert_contains "pending act verb rendered" "$dash" 'hold'
if [[ "$dash" == *"act-1753401700"* ]]; then
  fail "terminal act appeared in section items"
else
  ok "terminal act not in section items"
fi
echo ""

# t11-f: drop-priority unit test -- console-action items drop before live,
#         after unpark items; the 콘솔액션 HEADER is never dropped.
echo "--- t11-f: drop priority: ca items before live, header never dropped ---"
out="$("$PYTHON" - "$FOOTER_PY" <<'PY'
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("sf", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# Use items sized so that live(3) + ca(8) + unpark(4) + done(2) exceeds
# budget, forcing the drop loop.  Each big item = 200 units; at 3800 unit
# budget: 3(live) + 8(ca) + 4(unpark) + 2(done) = 17 items * ~220 each
# plus headers ~= 3740 before caps.  Nudge budget down to force drops.
big = "x" * 200
live = [big] * 3
ca_items_big = [big + " ca%d" % i for i in range(8)]  # sized items, distinct
unpark = (time.time(), "2026-07-24 06:00", [big] * 4)
done = [big] * 2
ca = (ca_items_big, False)
# Reduce budget so that not all items fit, forcing the drop loop.
orig_max = m.DASHBOARD_MAX_UNITS
m.DASHBOARD_MAX_UNITS = 1800
text = m._build_dashboard(["small decision"], live, done, unpark, ca)
m.DASHBOARD_MAX_UNITS = orig_max
assert m._u16len(text) <= 1800, "over reduced budget: %d" % m._u16len(text)
# live items must be intact (ca drops first, after unpark)
n_live = text.split("[%s]" % m.LIVE_LABEL)[1].split("[언파크 후보")[0].count(big) if m.LIVE_LABEL in text and "[언파크 후보" in text else -1
n_ca = sum(1 for i in range(8) if (big + " ca%d" % i) in text)
assert n_live == 3, "live items dropped too early: %d" % n_live
assert 0 <= n_ca < 8, "ca items cut wrong (expected < 8): %d" % n_ca
assert "[콘솔액션]" in text, "ca header dropped"
# extreme budget: all ca items cut, header still present
m.DASHBOARD_MAX_UNITS = 500
text2 = m._build_dashboard(["small decision"], live, done, unpark, ca)
m.DASHBOARD_MAX_UNITS = orig_max
assert "[콘솔액션]" in text2, "ca header dropped under extreme budget"
n_ca2 = sum(1 for i in range(8) if (big + " ca%d" % i) in text2)
assert n_ca2 == 0, "ca items survived extreme cut: %d" % n_ca2
print("CA-DROPTEST-OK live=%d ca=%d ca-header-survives-full-cut" % (n_live, n_ca))
PY
)" && rc=0 || rc=$?
assert_eq "ca drop-priority unit exit 0" "$rc" "0"
assert_contains "ca drop order verified" "$out" 'CA-DROPTEST-OK'
echo "  $out"
echo ""

# restore default isolation
export DOGANY_DECISION_ACTIONS_FILE="$WORK/ca-missing.md"

# ===========================================================================
# DGN-967: shared pin surface -- section ownership (compose model)
# ===========================================================================
echo "=== DGN-967: dashboard section ownership ==="
echo ""

pin_set() {  # pin_set <owner> <content> [extra args...]
  local owner="$1" content="$2"; shift 2
  "$PYTHON" "$FOOTER_PY" --pin-set "$owner" "$@" <<< "$content"
}

# s967-a: THE defect scenario -- board confirmed-empty + domain section
# present -> pin content survives intact (pre-967: every owner Stop wrote ""
# and the bridge deleted the domain pin).
echo "--- s967-a: board empty + domain section -> pin survives, content intact ---"
reset_surface
pin_set workout 'WORKOUT-CARD-LINE-1
WORKOUT-CARD-LINE-2'; rc=$?
assert_eq "pin-set exit 0" "$rc" "0"
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "footer exit 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "domain content survives the empty board write" \
  "$content" 'WORKOUT-CARD-LINE-1'
assert_contains "domain content intact (2nd line)" "$content" 'WORKOUT-CARD-LINE-2'
if [[ -s "$DASHBOARD" ]]; then
  ok "dashboard.md NON-empty (no HIDE signal -> no pin delete)"
else
  fail "dashboard.md empty -- the pin-delete defect is back"
fi
echo ""

# s967-b: board + domain both present -> domain first, board LAST, board's
# freshness stamp is the file's last line (stamp-last convention kept).
echo "--- s967-b: board + domain -> compose order, stamp stays last line ---"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
out="$(run_footer "$(make_input false)")"; rc=$?
assert_eq "footer exit 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_eq "domain section renders first" "$(head -1 "$DASHBOARD")" 'WORKOUT-CARD-LINE-1'
assert_contains "board section present (live agent)" "$content" '작업대'
assert_contains "board live label present" "$content" '[서브에이전트 작업 중]'
last_line="$(tail -1 "$DASHBOARD")"
if [[ "$last_line" == 갱신\ * ]]; then
  ok "freshness stamp is the last line"
else
  fail "stamp not last line -- got '$last_line'"
fi
echo ""

# s967-c: owner isolation -- A set, board write, B set: nobody disturbs
# anybody else's section.
echo "--- s967-c: each owner updates only its own section ---"
pin_set aaa 'SECTION-A-V1'
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
pin_set bbb 'SECTION-B-V1'
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "A intact after board + B writes" "$content" 'SECTION-A-V1'
assert_contains "B present" "$content" 'SECTION-B-V1'
assert_contains "board intact after B write" "$content" '작업대'
assert_contains "earlier workout section intact" "$content" 'WORKOUT-CARD-LINE-1'
pin_set aaa 'SECTION-A-V2'
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "A updated to V2" "$content" 'SECTION-A-V2'
if [[ "$content" == *'SECTION-A-V1'* ]]; then
  fail "stale A content still present after update"
else
  ok "A V1 replaced, not duplicated"
fi
assert_contains "B untouched by A update" "$content" 'SECTION-B-V1'
echo ""

# s967-d: backward compat -- a domain agent that NEVER adopts the API keeps
# writing dashboard.md directly; the next compose adopts it (legacy section)
# instead of clobbering it.
echo "--- s967-d: legacy adoption of a foreign direct write ---"
reset_surface
printf 'FOREIGN-DIRECT-CARD\n' > "$DASHBOARD"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "foreign direct write adopted, not clobbered" \
  "$content" 'FOREIGN-DIRECT-CARD'
assert_contains "board composed alongside adopted content" "$content" '작업대'
if grep -q '"legacy"' "$SECTIONS" 2>/dev/null; then
  ok "manifest carries the adopted legacy section"
else
  fail "legacy section missing from manifest"
fi
# ...and the empty-board Stop must STILL not delete it:
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
assert_contains "adopted content survives confirmed-empty board" \
  "$(cat "$DASHBOARD" 2>/dev/null)" 'FOREIGN-DIRECT-CARD'
echo ""

# s967-e: foreign EMPTY write = that writer's hide signal -> legacy cleared;
# with the board also empty the composed file is EMPTY (HIDE preserved).
echo "--- s967-e: foreign empty write clears legacy -> HIDE preserved ---"
: > "$DASHBOARD"
run_footer "$(make_input false)" > /dev/null
if [[ -f "$DASHBOARD" && ! -s "$DASHBOARD" ]]; then
  ok "all sections empty -> dashboard.md EMPTY (bridge hide unchanged)"
else
  fail "expected empty dashboard, got: $(head -2 "$DASHBOARD" 2>/dev/null)"
fi
echo ""

# s967-f: --pin-clear removes exactly one section; last section cleared ->
# empty file (HIDE).
echo "--- s967-f: pin-clear -> section removed; last one out -> HIDE ---"
reset_surface
pin_set one 'KEEP-ME'
pin_set two 'DROP-ME'
"$PYTHON" "$FOOTER_PY" --pin-clear two; rc=$?
assert_eq "pin-clear exit 0" "$rc" "0"
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "surviving section intact" "$content" 'KEEP-ME'
if [[ "$content" == *'DROP-ME'* ]]; then
  fail "cleared section still rendered"
else
  ok "cleared section gone from composed output"
fi
"$PYTHON" "$FOOTER_PY" --pin-clear one
if [[ -f "$DASHBOARD" && ! -s "$DASHBOARD" ]]; then
  ok "last section cleared -> empty file (HIDE)"
else
  fail "expected empty file after clearing last section"
fi
echo ""

# s967-g: opt-in TTL expiry -- a section whose owner died does not pin stale
# content past its declared TTL (sweep runs at the next compose).
echo "--- s967-g: --ttl expiry sweeps a dead owner's section ---"
reset_surface
pin_set mortal 'EXPIRES-FAST' --ttl 1
# epoch seconds are ints: age must EXCEED ttl, so sleep past the 2s boundary
sleep 2.2
pin_set other 'STILL-HERE'
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "live section kept" "$content" 'STILL-HERE'
if [[ "$content" == *'EXPIRES-FAST'* ]]; then
  fail "expired section still pinned"
else
  ok "expired section swept at compose"
fi
echo ""

# s967-h: owner slug validation -- reserved and malformed slugs rejected.
echo "--- s967-h: reserved/invalid owner slugs rejected ---"
pin_set board 'HIJACK' 2>/dev/null; rc=$?
assert_eq "reserved slug 'board' rejected (exit 1)" "$rc" "1"
pin_set legacy 'HIJACK' 2>/dev/null; rc=$?
assert_eq "reserved slug 'legacy' rejected (exit 1)" "$rc" "1"
pin_set 'Bad Slug!' 'X' 2>/dev/null; rc=$?
assert_eq "malformed slug rejected (exit 1)" "$rc" "1"
echo ""

# s967-i: crash residue self-heal -- a manifest whose composed_sha does not
# match disk, while disk IS our own composed output, must NOT re-adopt our
# own artifact as a duplicated legacy section.
echo "--- s967-i: crash residue (sha mismatch on own artifact) not re-adopted ---"
reset_surface
pin_set alpha 'ALPHA-CONTENT'
"$PYTHON" - "$SECTIONS" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["composed_sha"] = "deadbeef" * 8  # simulate crash between the two writes
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
n="$(grep -c 'ALPHA-CONTENT' "$DASHBOARD" 2>/dev/null || echo 0)"
assert_eq "own artifact appears exactly once (no legacy duplication)" "$n" "1"
if grep -q '"legacy"' "$SECTIONS" 2>/dev/null; then
  fail "own artifact was adopted as legacy"
else
  ok "no legacy section minted from own artifact"
fi
echo ""

# s967-j: length budget -- an oversized domain section is dropped from the
# OUTPUT (board survives), but stays in the STORE (returns when the board
# empties and the budget allows).
echo "--- s967-j: over-budget domain section dropped from output, kept in store ---"
reset_surface
pin_set jumbo "$("$PYTHON" -c 'print("JUMBO-" + "x" * 4200)')"
{ launch_line aaa0000111; echo; } > "$TRANSCRIPT"
freshen
run_footer "$(make_input false)" > /dev/null
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "board rendered despite jumbo section" "$content" '작업대'
if [[ "$content" == *'JUMBO-'* ]]; then
  fail "over-budget section not dropped from output"
else
  ok "over-budget section dropped from composed output"
fi
{ launch_line aaa0000111; echo; compl_line aaa0000111; echo; } > "$TRANSCRIPT"
run_footer "$(make_input false)" > /dev/null
assert_contains "jumbo section returns from store once board empties" \
  "$(cat "$DASHBOARD" 2>/dev/null)" 'JUMBO-'
echo ""

# s967-k: concurrency -- parallel CLI writers racing on the store; flock
# serializes; final state valid, both owners' last writes win.
echo "--- s967-k: parallel pin-set race -> serialized, both sections correct ---"
reset_surface
( for i in 1 2 3 4 5 6 7 8 9 10; do pin_set ra "RACE-A-$i" || echo "A$i-FAIL"; done ) &
( for i in 1 2 3 4 5 6 7 8 9 10; do pin_set rb "RACE-B-$i" || echo "B$i-FAIL"; done ) &
wait
content="$(cat "$DASHBOARD" 2>/dev/null)"
assert_contains "owner A final write present" "$content" 'RACE-A-10'
assert_contains "owner B final write present" "$content" 'RACE-B-10'
if "$PYTHON" -c 'import json,sys; json.load(open(sys.argv[1]))' "$SECTIONS" 2>/dev/null; then
  ok "manifest still valid JSON after the race"
else
  fail "manifest corrupted by concurrent writers"
fi
echo ""
reset_surface

echo "==========================="
printf "Results: %d passed, %d failed, %d skipped\n" "$PASS" "$FAIL" "$SKIP"
echo "==========================="
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
