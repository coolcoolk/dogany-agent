#!/bin/bash
# test-compat-lint.sh -- fixture-driven tests for scripts/pack/compat-lint.sh
# (DGN-803 LS-3).
#
# Checks C1-C6 pass/fail/skip behavior using synthetic fixtures under
# scripts/tests/fixtures/compat-lint/.
#
# Exit 0 = all assertions pass; nonzero = at least one failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINT="$SCRIPT_DIR/../pack/compat-lint.sh"
FX_DIR="$SCRIPT_DIR/fixtures/compat-lint"

# Framework version shipped in this repo
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FW_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"

PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# run_lint <fixture-dir> <fw-version> -- returns exit code of compat-lint.sh
# Output is captured to stdout/stderr of this script when tests fail.
run_lint() {
  local fxdir="$1" fw="$2"
  bash "$LINT" --pack-dir "$fxdir" --framework-version "$fw" 2>&1
}

run_lint_rc() {
  local fxdir="$1" fw="$2"
  bash "$LINT" --pack-dir "$fxdir" --framework-version "$fw" >/dev/null 2>&1
  echo $?
}

check_pass() {  # check_pass <desc> <fixture-dir> [<fw-version>]
  local desc="$1" fxdir="$2" fw="${3:-$FW_VERSION}"
  local rc
  rc="$(run_lint_rc "$fxdir" "$fw")"
  if [[ "$rc" -eq 0 ]]; then
    ok "$desc => exit 0 (PASS/SKIP)"
  else
    bad "$desc => expected exit 0, got $rc"
    echo "--- compat-lint output for $desc ---"
    run_lint "$fxdir" "$fw" || true
    echo "---"
  fi
}

check_fail() {  # check_fail <desc> <fixture-dir> [<fw-version>]
  local desc="$1" fxdir="$2" fw="${3:-$FW_VERSION}"
  local rc
  rc="$(run_lint_rc "$fxdir" "$fw")"
  if [[ "$rc" -ne 0 ]]; then
    ok "$desc => exit non-zero (FAIL as expected)"
  else
    bad "$desc => expected non-zero exit, got 0 (lint did not catch the violation)"
    echo "--- compat-lint output for $desc ---"
    run_lint "$fxdir" "$fw" || true
    echo "---"
  fi
}

check_output_contains() {  # check_output_contains <desc> <fixture-dir> <pattern> [<fw>]
  local desc="$1" fxdir="$2" pattern="$3" fw="${4:-$FW_VERSION}"
  local out
  out="$(run_lint "$fxdir" "$fw" || true)"
  if printf '%s\n' "$out" | grep -qE "$pattern"; then
    ok "$desc => output contains /$pattern/"
  else
    bad "$desc => output does not contain /$pattern/"
    echo "--- compat-lint output for $desc ---"
    printf '%s\n' "$out"
    echo "---"
  fi
}

echo "=== test-compat-lint.sh: fw_version=$FW_VERSION ==="
echo ""

# ---------------------------------------------------------------------------
# T1: good fixture -- not seeded, C1/C2/C4/C5 PASS, C3/C6 SKIP
# ---------------------------------------------------------------------------
echo "--- T1: good unseeded fixture (C1/C2/C4/C5=PASS, C3/C6=SKIP) ---"
check_pass "T1 good fixture exit 0" "$FX_DIR/good" "$FW_VERSION"
check_output_contains "T1 C1 PASS" "$FX_DIR/good" "PASS.*C1"
check_output_contains "T1 C2 PASS" "$FX_DIR/good" "PASS.*C2"
check_output_contains "T1 C3 SKIP" "$FX_DIR/good" "SKIP.*C3"
check_output_contains "T1 C4 PASS" "$FX_DIR/good" "PASS.*C4"
check_output_contains "T1 C5 PASS" "$FX_DIR/good" "PASS.*C5"
check_output_contains "T1 C6 SKIP" "$FX_DIR/good" "SKIP.*C6"

echo ""
# ---------------------------------------------------------------------------
# T2: missing-fields fixture -- C1 FAIL (pack_version/contract_version/requires_framework absent)
# ---------------------------------------------------------------------------
echo "--- T2: missing required manifest fields (C1 FAIL) ---"
check_fail "T2 missing fields => non-zero exit" "$FX_DIR/missing-fields"
check_output_contains "T2 C1 FAIL pack_version" "$FX_DIR/missing-fields" "FAIL.*C1.*pack_version"
check_output_contains "T2 C1 FAIL contract_version" "$FX_DIR/missing-fields" "FAIL.*C1.*contract_version"
check_output_contains "T2 C1 FAIL requires_framework" "$FX_DIR/missing-fields" "FAIL.*C1.*requires_framework"

echo ""
# ---------------------------------------------------------------------------
# T3: range-mismatch fixture -- C2 FAIL (framework 1.29.0 does not satisfy >=2.0.0)
# ---------------------------------------------------------------------------
echo "--- T3: requires_framework range mismatch (C2 FAIL) ---"
check_fail "T3 range mismatch => non-zero exit" "$FX_DIR/range-mismatch"
check_output_contains "T3 C2 FAIL" "$FX_DIR/range-mismatch" "FAIL.*C2"

echo ""
# ---------------------------------------------------------------------------
# T4: allowlist-violation fixture -- C4 FAIL (bridge/ path in payload)
# ---------------------------------------------------------------------------
echo "--- T4: payload allowlist violation (C4 FAIL: bridge/ infiltration) ---"
check_fail "T4 allowlist violation => non-zero exit" "$FX_DIR/allowlist-violation"
check_output_contains "T4 C4 FAIL bridge" "$FX_DIR/allowlist-violation" "FAIL.*C4.*bridge"

echo ""
# ---------------------------------------------------------------------------
# T5: pin-mismatch fixture -- C3 FAIL (EXPECTED_USER_VERSION=5, max_mig=4, PRAGMA=3)
# ---------------------------------------------------------------------------
echo "--- T5: version pin mismatch (C3 FAIL: 3 values inconsistent) ---"
check_fail "T5 pin mismatch => non-zero exit" "$FX_DIR/pin-mismatch"
check_output_contains "T5 C3 FAIL mismatch" "$FX_DIR/pin-mismatch" "FAIL.*C3"

echo ""
# ---------------------------------------------------------------------------
# T6: range that exactly matches boundary -- C2 PASS (fw=1.29.0 satisfies >=1.28.0 <2.0.0)
# ---------------------------------------------------------------------------
echo "--- T6: semver range boundary check (C2 PASS for fw=1.29.0 in [1.28.0, 2.0.0)) ---"
check_pass "T6 boundary pass" "$FX_DIR/good" "1.29.0"

echo ""
# ---------------------------------------------------------------------------
# T7: framework version exactly at upper bound -- C2 FAIL (2.0.0 does not satisfy <2.0.0)
# ---------------------------------------------------------------------------
echo "--- T7: framework at upper exclusive bound (C2 FAIL: 2.0.0 not <2.0.0) ---"
check_fail "T7 upper bound exclusive" "$FX_DIR/good" "2.0.0"
check_output_contains "T7 C2 FAIL at upper bound" "$FX_DIR/good" "FAIL.*C2" "2.0.0"

echo ""
# ---------------------------------------------------------------------------
# T8: sibling payload_root escape (BUG-1 fix -- payload_root="../compat-lint-evil")
# Pack dir = fixtures/compat-lint/sibling-payloadroot
# payload_root="../compat-lint-evil" resolves to sibling dir outside pack ->
# C1 FAIL (old strip-prefix logic would have PASSED this incorrectly)
# ---------------------------------------------------------------------------
echo "--- T8: sibling payload_root escape (C1 FAIL: BUG-1 fix verification) ---"
check_fail "T8 sibling payloadroot => non-zero exit" "$FX_DIR/sibling-payloadroot"
check_output_contains "T8 C1 FAIL escapes boundary" "$FX_DIR/sibling-payloadroot" "FAIL.*C1.*escapes pack boundary"

echo ""
# ---------------------------------------------------------------------------
# T9: symlink in payload/ points to absolute sibling dir outside pack boundary
# (BUG-1 fix -- absolute symlink to fixtures/compat-lint-evil)
# C4 FAIL (old strip-prefix logic would have PASSED this if pack_real was a
# prefix of a sibling path like /a/b when resolved was /a/b-evil)
# ---------------------------------------------------------------------------
echo "--- T9: symlink to sibling dir outside pack boundary (C4 FAIL: BUG-1 fix verification) ---"
check_fail "T9 symlink sibling escape => non-zero exit" "$FX_DIR/sibling-symlink"
check_output_contains "T9 C4 FAIL symlink escapes" "$FX_DIR/sibling-symlink" "FAIL.*C4.*symlink escapes"

echo ""
# ---------------------------------------------------------------------------
# T10: EUV inline comment -- FIX-1: "EXPECTED_USER_VERSION = 4  # comment"
# must parse as integer 4, not crash on "4#comment" in $((10#$EUV)).
# All versions match (EUV=4, max_mig=4, pragma=4) -> C3 PASS.
# ---------------------------------------------------------------------------
echo "--- T10: EXPECTED_USER_VERSION with inline comment (C3 PASS: FIX-1) ---"
check_pass "T10 EUV inline comment exit 0" "$FX_DIR/euv-inline-comment"
check_output_contains "T10 C3 PASS consistent" "$FX_DIR/euv-inline-comment" "PASS.*C3"

echo ""
# ---------------------------------------------------------------------------
# T11: backup residue in payload -- FIX-2: SKILL.md.bak.20260715 must be
# caught by C4b hygiene gate (token-free file that C5 misses).
# ---------------------------------------------------------------------------
echo "--- T11: .bak residue in payload (C4b FAIL: FIX-2) ---"
check_fail "T11 bak residue => non-zero exit" "$FX_DIR/bak-residue"
check_output_contains "T11 C4b FAIL bak" "$FX_DIR/bak-residue" "FAIL.*C4b.*backup"

echo ""
# ---------------------------------------------------------------------------
# T12: legacy-grace -- pack with no contract_version and kind != "kit"
# must exit 0 immediately with a loud SKIP log (FIX-1, DGN-803).
# T13: legacy-grace boundary -- kit with no contract_version must still FAIL
# (legacy-grace applies ONLY to non-kit packs).
# ---------------------------------------------------------------------------
echo "--- T12: legacy non-kit pack (no contract_version) => exit 0 (grace) ---"
check_pass "T12 legacy non-kit => exit 0 (legacy grace)" "$FX_DIR/legacy-pack"
check_output_contains "T12 legacy-grace log" "$FX_DIR/legacy-pack" "legacy pack.*no contract_version.*v2 contract checks skipped"

echo ""
echo "--- T13: legacy-grace boundary -- kit without contract_version => FAIL (no grace) ---"
check_fail "T13 kit missing contract_version => non-zero exit" "$FX_DIR/missing-fields"
check_output_contains "T13 C1 FAIL contract_version" "$FX_DIR/missing-fields" "FAIL.*C1.*contract_version"

echo ""
# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
