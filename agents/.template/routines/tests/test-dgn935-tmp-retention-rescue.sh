#!/bin/bash
# test-lane: fast    # measured 2.2s green (2026-09-15; was UNLANED since 3a24ac1e and FAILED every fast-lane run)
# test-dgn935-tmp-retention-rescue.sh -- cleanup-files.sh tmp handling (DGN-935 restore)
#
# Guards the three tmp behaviors an [auto] framework update silently regressed
# (DGN-1054 pattern: update overwrote instance-first quarantine logic):
#   T1  spec-signature file in tmp -> quarantined to _archive/tmp-rescue/<date>/
#       with a [WARN] log line (never trashed, never deleted)
#   T2  scratch older than RETENTION_DAYS (7) -> trash (if available) or
#       _archive -- reversible, no rm -rf
#   T2b same, with trash unavailable -> falls back to _archive
#   T3  scratch younger than RETENTION_DAYS -> left in place
#   T4  --dry-run -> plans printed, nothing moves (all three paths)
#   T5  .gitkeep never touched by any path
# Plus the DGN-196 set -e regression: every run must exit 0 and reach the
# completion marker.
#
# The real shipped script runs against a throwaway fake agent root (only the
# BASE= line is patched in a copy). A stub `trash` on PATH captures the trash
# branch deterministically without polluting the user's real Trash; the
# no-trash fallback is exercised via a copy whose trash probe is renamed.
#
# Run: bash agents/.template/routines/tests/test-dgn935-tmp-retention-rescue.sh
# Exit: 0 all pass, nonzero any fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLEANUP="$ROUTINES_DIR/cleanup-files.sh"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

assert_exists() {
  local desc="$1" path="$2"
  if [[ -e "$path" ]]; then ok "$desc"; else fail "$desc -- not found: $path"; fi
}

assert_not_exists() {
  local desc="$1" path="$2"
  if [[ ! -e "$path" ]]; then ok "$desc"; else fail "$desc -- still exists: $path"; fi
}

assert_grep() {
  local desc="$1" pattern="$2" file="$3"
  if grep -q "$pattern" "$file"; then ok "$desc"; else fail "$desc -- pattern not in log: $pattern"; fi
}

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dgn935-test.XXXXXX")"
trap 'rm -rf "$STUB_DIR"' EXIT

RESCUE_DATE="$(date +%Y%m%d)"

# timestamp helpers (BSD date first, GNU fallback)
old_stamp()    { date -v-8d +%Y%m%d%H%M 2>/dev/null || date -d '8 days ago' +%Y%m%d%H%M; }
recent_stamp() { date -v-1d +%Y%m%d%H%M 2>/dev/null || date -d '1 day ago' +%Y%m%d%H%M; }

# stub trash: records what it received instead of using the real Trash
STUB_BIN="$STUB_DIR/bin"
STUB_TRASH="$STUB_DIR/trashed"
mkdir -p "$STUB_BIN" "$STUB_TRASH"
cat > "$STUB_BIN/trash" <<EOF
#!/bin/bash
mv "\$@" "$STUB_TRASH/"
EOF
chmod +x "$STUB_BIN/trash"

# fresh fake agent root per scenario
make_root() {
  local root="$1"
  rm -rf "$root"
  mkdir -p "$root/files/tmp" "$root/files/outbox" "$root/files/_archive" \
           "$root/.telegram_bot/images"
  touch "$root/files/tmp/.gitkeep" "$root/files/outbox/.gitkeep" \
        "$root/.telegram_bot/images/.gitkeep"
}

# patched copy of the real script: only BASE= is replaced
patch_script() {
  local root="$1" out="$2"
  sed -e "s|^BASE=.*|BASE=\"$root\"|" "$CLEANUP" > "$out"
  chmod +x "$out"
}

run_and_check_completion() {
  # $1=label $2=script $3=logfile, then args...
  local label="$1" script="$2" logfile="$3"
  shift 3
  local rc=0
  PATH="$STUB_BIN:$PATH" "$script" "$@" > "$logfile" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then ok "$label: exit 0 (DGN-196 no set -e abort)"; else fail "$label: exit $rc"; cat "$logfile"; fi
  assert_grep "$label: reached completion marker" "완료" "$logfile"
}

echo ""
echo "=== DGN-935 tmp retention + rescue self-tests ==="

# ---- T1: spec-signature file -> quarantined to tmp-rescue, WARN logged ----
echo ""
echo "--- T1: spec-signature file quarantined ---"
ROOT="$STUB_DIR/root_t1"; make_root "$ROOT"
SCRIPT="$STUB_DIR/cleanup_t1.sh"; patch_script "$ROOT" "$SCRIPT"
touch "$ROOT/files/tmp/dgn999-spec-locked.md"
run_and_check_completion "T1" "$SCRIPT" "$STUB_DIR/t1.log"
assert_grep "T1: WARN line for spec signature" "\[WARN\] spec signature" "$STUB_DIR/t1.log"
assert_exists "T1: file quarantined to tmp-rescue/<date>/" \
  "$ROOT/files/_archive/tmp-rescue/$RESCUE_DATE/dgn999-spec-locked.md"
assert_not_exists "T1: file gone from tmp" "$ROOT/files/tmp/dgn999-spec-locked.md"
assert_not_exists "T1: rescued file NOT sent to trash" "$STUB_TRASH/dgn999-spec-locked.md"
assert_exists "T5(T1 run): .gitkeep untouched" "$ROOT/files/tmp/.gitkeep"

# ---- T2: 8-day-old scratch -> trash (stub) ----
echo ""
echo "--- T2: old scratch goes to trash ---"
ROOT="$STUB_DIR/root_t2"; make_root "$ROOT"
SCRIPT="$STUB_DIR/cleanup_t2.sh"; patch_script "$ROOT" "$SCRIPT"
touch -t "$(old_stamp)" "$ROOT/files/tmp/scratch-old.txt"
run_and_check_completion "T2" "$SCRIPT" "$STUB_DIR/t2.log"
assert_not_exists "T2: old scratch removed from tmp" "$ROOT/files/tmp/scratch-old.txt"
assert_exists "T2: old scratch landed in trash" "$STUB_TRASH/scratch-old.txt"
assert_exists "T5(T2 run): .gitkeep untouched" "$ROOT/files/tmp/.gitkeep"

# ---- T2b: 8-day-old scratch, trash unavailable -> _archive fallback ----
echo ""
echo "--- T2b: old scratch falls back to _archive when trash is absent ---"
ROOT="$STUB_DIR/root_t2b"; make_root "$ROOT"
SCRIPT="$STUB_DIR/cleanup_t2b.sh"; patch_script "$ROOT" "$SCRIPT"
# rename the probe target so `command -v` fails -> real fallback branch runs
sed -i.bak 's|command -v trash |command -v trash-absent-for-test |g' "$SCRIPT" && rm -f "$SCRIPT.bak"
touch -t "$(old_stamp)" "$ROOT/files/tmp/scratch-old2.txt"
run_and_check_completion "T2b" "$SCRIPT" "$STUB_DIR/t2b.log"
assert_not_exists "T2b: old scratch removed from tmp" "$ROOT/files/tmp/scratch-old2.txt"
assert_exists "T2b: old scratch moved to _archive" "$ROOT/files/_archive/scratch-old2.txt"

# ---- T3: 1-day-old scratch -> left in place ----
echo ""
echo "--- T3: recent scratch survives (under retention) ---"
ROOT="$STUB_DIR/root_t3"; make_root "$ROOT"
SCRIPT="$STUB_DIR/cleanup_t3.sh"; patch_script "$ROOT" "$SCRIPT"
touch -t "$(recent_stamp)" "$ROOT/files/tmp/scratch-recent.txt"
run_and_check_completion "T3" "$SCRIPT" "$STUB_DIR/t3.log"
assert_exists "T3: recent scratch still in tmp" "$ROOT/files/tmp/scratch-recent.txt"
assert_exists "T5(T3 run): .gitkeep untouched" "$ROOT/files/tmp/.gitkeep"

# ---- T4: --dry-run with all three cases present -> nothing moves ----
echo ""
echo "--- T4: dry-run plans everything, moves nothing ---"
ROOT="$STUB_DIR/root_t4"; make_root "$ROOT"
SCRIPT="$STUB_DIR/cleanup_t4.sh"; patch_script "$ROOT" "$SCRIPT"
touch "$ROOT/files/tmp/dgn999-spec-locked.md"
touch -t "$(old_stamp)" "$ROOT/files/tmp/scratch-old.txt"
touch -t "$(recent_stamp)" "$ROOT/files/tmp/scratch-recent.txt"
run_and_check_completion "T4" "$SCRIPT" "$STUB_DIR/t4.log" --dry-run
assert_grep "T4: rescue planned in log" "\[DRY\] rescue move planned" "$STUB_DIR/t4.log"
assert_grep "T4: trash planned in log" "\[DRY\] trash" "$STUB_DIR/t4.log"
assert_exists "T4: spec file still in tmp" "$ROOT/files/tmp/dgn999-spec-locked.md"
assert_exists "T4: old scratch still in tmp" "$ROOT/files/tmp/scratch-old.txt"
assert_exists "T4: recent scratch still in tmp" "$ROOT/files/tmp/scratch-recent.txt"
assert_not_exists "T4: nothing quarantined" "$ROOT/files/_archive/tmp-rescue/$RESCUE_DATE/dgn999-spec-locked.md"
assert_not_exists "T4: nothing trashed" "$STUB_TRASH/scratch-recent.txt"
assert_exists "T5(T4 run): .gitkeep untouched" "$ROOT/files/tmp/.gitkeep"

echo ""
echo "=== result: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -eq 0 ]]; then exit 0; else exit 1; fi
