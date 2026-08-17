#!/usr/bin/env bash
# test-session-recap-size-log.sh -- tests for session-recap size logging
#
# Tests:
#   S1: session-recap.py writes a size log entry when it injects context
#   S2: ticket-hygiene weekly scan flags when 7d median > 4000 chars
#   S3: ticket-hygiene weekly scan is silent when 7d median <= 4000 chars
#   S4: no size log file -> no flag in scan output
#
# S2-S4 require ticket-hygiene.sh in the routines directory. If absent,
# these tests are skipped (the file is not part of all agent deployments).
#
# Run:  bash routines/tests/test-session-recap-size-log.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$ROUTINES_DIR/.." && pwd)"
RECAP_PY="$ROUTINES_DIR/session-recap.py"
HYGIENE_SH="$ROUTINES_DIR/ticket-hygiene.sh"

PASS=0
FAIL=0
SKIP=0

ok()   { printf "  PASS: %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  FAIL: %s\n" "$1"; FAIL=$((FAIL + 1)); }
skip() { printf "  SKIP: %s\n" "$1"; SKIP=$((SKIP + 1)); }

assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" == *"$needle"* ]]; then ok "$desc"; else fail "$desc -- needle='$needle' not found"; fi
}
assert_not_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if [[ "$haystack" != *"$needle"* ]]; then ok "$desc"; else fail "$desc -- needle='$needle' unexpectedly found"; fi
}

TODAY="$(date '+%Y-%m-%d')"

# ---------------------------------------------------------------------------
# S1: session-recap.py writes size log entry
# ---------------------------------------------------------------------------
echo ""
echo "=== S1: session-recap.py writes size log ==="

S1_WORK="$(mktemp -d /tmp/recap-size-s1.XXXXXX)"
trap 'rm -rf "$S1_WORK"' EXIT

mkdir -p "$S1_WORK/.telegram_bot/logs"
mkdir -p "$S1_WORK/proj"

# current session (empty)
touch "$S1_WORK/proj/current.jsonl"

# previous session with real content
cat > "$S1_WORK/proj/previous.jsonl" <<'JSONL'
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello, can you help me with a task?"}]}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Sure, I can help with that."}]}}
JSONL

PAYLOAD="{\"source\":\"startup\",\"transcript_path\":\"$S1_WORK/proj/current.jsonl\",\"cwd\":\"$S1_WORK\"}"

python3 "$RECAP_PY" <<< "$PAYLOAD" > /dev/null

SIZE_LOG="$S1_WORK/.telegram_bot/logs/session-recap-size.log"
if [[ -f "$SIZE_LOG" ]]; then
  ok "S1: session-recap-size.log created"
else
  fail "S1: session-recap-size.log not created"
fi

if [[ -f "$SIZE_LOG" ]]; then
  LINE="$(cat "$SIZE_LOG")"
  # format: YYYY-MM-DD HH:MM:SS <chars>
  if [[ "$LINE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\ [0-9]+$ ]]; then
    ok "S1: log line format YYYY-MM-DD HH:MM:SS <N>"
  else
    fail "S1: log line format wrong -- got: '$LINE'"
  fi

  CHARS="$(awk '{print $3}' "$SIZE_LOG")"
  if [[ "$CHARS" -gt 0 ]]; then
    ok "S1: injected char count > 0"
  else
    fail "S1: char count should be > 0 (got '$CHARS')"
  fi
fi

# ---------------------------------------------------------------------------
# S2-S4: ticket-hygiene 7d median scan (requires ticket-hygiene.sh)
# ---------------------------------------------------------------------------
if [[ ! -f "$HYGIENE_SH" ]]; then
  echo ""
  echo "=== S2-S4: SKIPPED (ticket-hygiene.sh not present in this deployment) ==="
  skip "S2: [recap capacity] flag (hygiene not present)"
  skip "S2: 4000 char exceeded text (hygiene not present)"
  skip "S2: median value in output (hygiene not present)"
  skip "S3: no [recap capacity] flag when median <= 4000 (hygiene not present)"
  skip "S3: no 4000 char exceeded text (hygiene not present)"
  skip "S4: no [recap capacity] when log absent (hygiene not present)"
else

# ---------------------------------------------------------------------------
# Hygiene helper: create patched ticket-hygiene.sh pointing at test workspace
# ---------------------------------------------------------------------------
setup_hygiene_work() {
  local work="$1"
  mkdir -p "$work/worklog/decisions"
  mkdir -p "$work/routines"
  mkdir -p "$work/.telegram_bot/logs"

  # minimal push stub
  cat > "$work/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH_CALLED $*"
exit 0
EOF
  chmod +x "$work/routines/push.sh"

  # minimal open ticket so build_body has something to push
  cat > "$work/worklog/DGN-100-open.md" <<EOF
---
id: DGN-100
title: open ticket
status: open
priority: P2
created: 2020-01-01
updated: 2020-01-01
---
body
EOF

  # patch ROOT in hygiene script
  sed "s|ROOT=\"\$(cd.*\$|ROOT=\"$work\"|" "$HYGIENE_SH" > "$work/routines/hygiene-test.sh"
  chmod +x "$work/routines/hygiene-test.sh"
}

run_hygiene() {
  local work="$1"
  shift
  bash "$work/routines/hygiene-test.sh" "$@" 2>&1 || true
}

# ---------------------------------------------------------------------------
# S2: 7d median > 4000 -> flag line appears
# ---------------------------------------------------------------------------
echo ""
echo "=== S2: median > 4000 -> flag in scan output ==="

S2_WORK="$(mktemp -d /tmp/recap-size-s2.XXXXXX)"
trap 'rm -rf "$S2_WORK"' EXIT

setup_hygiene_work "$S2_WORK"

# 5 entries today, all > 4000 chars -> median = 5000
cat > "$S2_WORK/.telegram_bot/logs/session-recap-size.log" <<EOF
$TODAY 08:00:00 5000
$TODAY 09:00:00 5000
$TODAY 10:00:00 5000
$TODAY 11:00:00 5000
$TODAY 12:00:00 5000
EOF

OUT="$(run_hygiene "$S2_WORK" --dry-run)"

assert_contains "S2: [recap 용량] flag line present" "$OUT" "[recap 용량]"
assert_contains "S2: 4000자 초과 text present" "$OUT" "4000자 초과"
assert_contains "S2: median value in output" "$OUT" "5000"

# ---------------------------------------------------------------------------
# S3: 7d median <= 4000 -> no flag
# ---------------------------------------------------------------------------
echo ""
echo "=== S3: median <= 4000 -> no flag ==="

S3_WORK="$(mktemp -d /tmp/recap-size-s3.XXXXXX)"
trap 'rm -rf "$S3_WORK"' EXIT

setup_hygiene_work "$S3_WORK"

# 3 entries today, median = 2000 (<= 4000)
cat > "$S3_WORK/.telegram_bot/logs/session-recap-size.log" <<EOF
$TODAY 08:00:00 1000
$TODAY 09:00:00 2000
$TODAY 10:00:00 3000
EOF

OUT="$(run_hygiene "$S3_WORK" --dry-run)"

assert_not_contains "S3: no [recap 용량] flag when median <= 4000" "$OUT" "[recap 용량]"
assert_not_contains "S3: no 4000자 초과 text" "$OUT" "4000자 초과"

# ---------------------------------------------------------------------------
# S4: no size log file -> no flag
# ---------------------------------------------------------------------------
echo ""
echo "=== S4: no size log -> no flag ==="

S4_WORK="$(mktemp -d /tmp/recap-size-s4.XXXXXX)"
trap 'rm -rf "$S4_WORK"' EXIT

setup_hygiene_work "$S4_WORK"
# deliberately do NOT create session-recap-size.log

OUT="$(run_hygiene "$S4_WORK" --dry-run)"

assert_not_contains "S4: no [recap 용량] when log absent" "$OUT" "[recap 용량]"

fi  # end HYGIENE_SH present block

# ---------------------------------------------------------------------------
echo ""
echo "=== Summary ==="
printf "Results: %d passed, %d failed, %d skipped\n" "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  echo "SOME TESTS FAILED"
  exit 1
else
  echo "ALL TESTS PASSED"
  exit 0
fi
