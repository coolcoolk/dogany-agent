#!/bin/bash
# Unit-style dry test for chunked classify-inbox.
# Tests: chunk math, partial-progress on injected chunk failure, inbox integrity.
# Uses CLASSIFY_CMD and MEMORIES_DIR env seams -- never calls the real Opus API.
# Usage: bash test-classify-inbox-chunked.sh [--verbose]
set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MEM_PY="$BASE/memory-engine/memory.py"
PY="/usr/bin/python3"
TMPDIR_ROOT="$(mktemp -d /tmp/classify-test-XXXXXX)"
trap 'rm -rf "$TMPDIR_ROOT"' EXIT

log()    { echo "[test] $*"; }
ok()     { echo "[PASS] $*"; }
fail()   { echo "[FAIL] $*" >&2; FAILURES=$((FAILURES + 1)); }
verbose(){ [[ "$VERBOSE" -eq 1 ]] && echo "  $*"; }
FAILURES=0

# ---- build a minimal test memories directory ----
MEM_DIR="$TMPDIR_ROOT/memories"
mkdir -p "$MEM_DIR"
# Export so all memory.py subprocess calls use the scratch dir, not production.
export MEMORIES_DIR="$MEM_DIR"

reset_inbox() {
    # (Re)write inbox.md with 25 real items.
    {
        echo '<!-- dogany-format: section -->'
        echo '### unclassified (inbox)'
        echo '§'
        echo '<!-- seed comment -->'
        echo '§'
        for i in $(seq 1 25); do
            echo "item-$i content for testing chunk $i"
            echo '§'
        done
    } > "$MEM_DIR/inbox.md"
}

reset_topic() {
    # Reset the fake topic file.
    cat > "$MEM_DIR/about-user.md" <<'TOPICEOF'
<!-- dogany-format: section -->
### About user
§
stub item
§
TOPICEOF
}

reset_inbox
reset_topic

log "inbox.md written (25 items)"

# Verify inbox-count sees 25 items using the real logic.
count=$("$PY" "$MEM_PY" inbox-count 2>/dev/null)
if [[ "$count" -eq 25 ]]; then
    ok "inbox-count = 25 (MEMORIES_DIR seam works)"
else
    fail "inbox-count expected 25, got $count"
fi

# ---- stub verdict file: items 1-3 -> about-user.md, 4 -> DROP, 5 -> no verdict (kept) ----
STUB_FILE="$TMPDIR_ROOT/stub_verdicts.txt"
cat > "$STUB_FILE" <<'EOF'
1 about-user.md
2 about-user.md
3 about-user.md
4 DROP
EOF

# ---- TEST 1: chunk math (CLASSIFY_CHUNK_SIZE=5, 25 items = 5 chunks, all succeed) ----
log ""
log "TEST 1: chunk math (5 items/chunk, 25 items -> 5 chunks, all succeed)"

export CLASSIFY_CHUNK_SIZE=5
export CLASSIFY_CMD="cat $STUB_FILE"

"$PY" "$MEM_PY" classify-inbox --no-push 2>&1 | \
    { [[ "$VERBOSE" -eq 1 ]] && cat || cat > /dev/null; }
rc=$?
unset CLASSIFY_CMD CLASSIFY_CHUNK_SIZE

if [[ "$rc" -eq 0 ]]; then
    ok "classify-inbox exited 0 (success)"
else
    fail "classify-inbox exited $rc (expected 0)"
fi

# 5 chunks of 5; each chunk: items 1/2/3 -> about-user.md, 4 -> DROP, 5 -> no verdict (kept).
# So 1 item stays in inbox per chunk -> 5 retained total.
remaining=$("$PY" "$MEM_PY" inbox-count 2>/dev/null || echo 0)
if [[ "$remaining" -eq 5 ]]; then
    ok "inbox items after full run = 5 (1 retained per chunk)"
else
    fail "inbox items after full run: expected 5, got $remaining"
fi

# about-user.md should have 3 items per chunk * 5 chunks = 15 new entries.
new_entries=$(grep -c "inbox-classified" "$MEM_DIR/about-user.md" 2>/dev/null || echo 0)
if [[ "$new_entries" -eq 15 ]]; then
    ok "about-user.md received 15 new classified items (3 per chunk x 5)"
else
    fail "about-user.md new entries: expected 15, got $new_entries"
fi

# ---- TEST 2: partial progress on injected chunk failure ----
reset_inbox
reset_topic

# Counter-based stub: call #2 exits 1 (simulates Opus timeout/error for chunk 2).
COUNTER_FILE="$TMPDIR_ROOT/call_counter"
echo 0 > "$COUNTER_FILE"
STUB_SCRIPT="$TMPDIR_ROOT/stub_with_failure.sh"
cat > "$STUB_SCRIPT" <<STUB
#!/bin/bash
n=\$(cat "$COUNTER_FILE")
n=\$((n+1))
echo \$n > "$COUNTER_FILE"
if [[ "\$n" -eq 2 ]]; then
    echo "[stub] simulating chunk 2 failure" >&2
    exit 1
fi
cat "$STUB_FILE"
STUB
chmod +x "$STUB_SCRIPT"

log ""
log "TEST 2: partial progress (chunk 2 of 5 fails, others succeed)"
echo 0 > "$COUNTER_FILE"

export CLASSIFY_CHUNK_SIZE=5
export CLASSIFY_CMD="bash $STUB_SCRIPT"

"$PY" "$MEM_PY" classify-inbox --no-push 2>&1 | \
    { [[ "$VERBOSE" -eq 1 ]] && cat || cat > /dev/null; }
rc=$?
unset CLASSIFY_CMD CLASSIFY_CHUNK_SIZE

if [[ "$rc" -eq 0 ]]; then
    ok "partial-progress run exited 0 (some chunks ok -> partial success)"
else
    fail "partial-progress run exited $rc (expected 0 for partial success)"
fi

# 4 ok chunks (1 retained each = 4) + failed chunk 2 (all 5 kept intact) = 9 remaining.
remaining2=$("$PY" "$MEM_PY" inbox-count 2>/dev/null || echo 0)
if [[ "$remaining2" -eq 9 ]]; then
    ok "inbox items after partial-failure run = 9 (4 ok retained + 5 failed chunk intact)"
else
    fail "inbox items after partial-failure run: expected 9, got $remaining2"
fi

# 4 ok chunks * 3 items filed each = 12 new entries in about-user.md.
new_entries2=$(grep -c "inbox-classified" "$MEM_DIR/about-user.md" 2>/dev/null || echo 0)
if [[ "$new_entries2" -eq 12 ]]; then
    ok "about-user.md received 12 classified items (3 per chunk x 4 ok chunks)"
else
    fail "about-user.md new entries: expected 12, got $new_entries2"
fi

# ---- TEST 3: total failure (all chunks fail -> rc=1, inbox untouched) ----
reset_inbox
reset_topic

log ""
log "TEST 3: total failure (all chunks fail -> rc=1)"

export CLASSIFY_CHUNK_SIZE=5
export CLASSIFY_CMD="bash -c 'echo fail >&2; exit 1'"

"$PY" "$MEM_PY" classify-inbox --no-push > /dev/null 2>&1
rc=$?
unset CLASSIFY_CMD CLASSIFY_CHUNK_SIZE

if [[ "$rc" -eq 1 ]]; then
    ok "total-failure run exited 1 (CLASSIFY_RC_FAIL)"
else
    fail "total-failure run exited $rc (expected 1)"
fi

# Inbox must still have all 25 items (no successful chunk removed anything).
remaining3=$("$PY" "$MEM_PY" inbox-count 2>/dev/null || echo 0)
if [[ "$remaining3" -eq 25 ]]; then
    ok "inbox intact after total failure (25 items unchanged)"
else
    fail "inbox after total failure: expected 25, got $remaining3"
fi

# ---- TEST 4: inbox.md is parseable after partial-progress run ----
reset_inbox
reset_topic
echo 0 > "$COUNTER_FILE"

log ""
log "TEST 4: inbox integrity (parseable after partial-progress run)"

export CLASSIFY_CHUNK_SIZE=5
export CLASSIFY_CMD="bash $STUB_SCRIPT"
"$PY" "$MEM_PY" classify-inbox --no-push > /dev/null 2>&1
unset CLASSIFY_CMD CLASSIFY_CHUNK_SIZE

if "$PY" "$MEM_PY" inbox-count > /dev/null 2>&1; then
    ok "inbox.md is parseable after partial-progress run"
else
    fail "inbox.md parse failed after partial-progress run"
fi

# ---- TEST 5: CLASSIFY_CHUNK_SIZE=1 (25 single-item chunks, each routed to about-user.md) ----
reset_inbox
reset_topic

# Single-item stub: item 1 -> about-user.md (only 1 item per chunk).
SINGLE_STUB="$TMPDIR_ROOT/single_stub.txt"
echo "1 about-user.md" > "$SINGLE_STUB"

log ""
log "TEST 5: CLASSIFY_CHUNK_SIZE=1 (25 chunks of 1, all assigned)"

export CLASSIFY_CHUNK_SIZE=1
export CLASSIFY_CMD="cat $SINGLE_STUB"

"$PY" "$MEM_PY" classify-inbox --no-push > /dev/null 2>&1
rc=$?
unset CLASSIFY_CMD CLASSIFY_CHUNK_SIZE

if [[ "$rc" -eq 0 ]]; then
    ok "chunk-size=1 run exited 0"
else
    fail "chunk-size=1 run exited $rc (expected 0)"
fi

# All 25 items have verdict '1 about-user.md' -> all assigned, inbox empty.
# inbox-count returns "0" on stdout and rc=2 when empty -- capture stdout only.
remaining5=$("$PY" "$MEM_PY" inbox-count 2>/dev/null; true)
remaining5="${remaining5:-0}"
if [[ "$remaining5" -eq 0 ]]; then
    ok "inbox empty after chunk-size=1 run (all 25 items assigned)"
else
    fail "inbox items after chunk-size=1: expected 0, got $remaining5"
fi

# ---- summary ----
echo ""
if [[ "$FAILURES" -eq 0 ]]; then
    echo "[ALL PASS] classify-inbox chunked test suite passed."
else
    echo "[FAIL] $FAILURES test(s) failed."
    exit 1
fi
