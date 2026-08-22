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
# T14/T15: mirror) allowlist case (DGN-872). mirror/ is lifekit-pack-owned
# SOURCE -- source files (*.py/*.sql) must PASS C4; runtime residue (*.db etc)
# must FAIL C4 via the mirror branch (C4b does NOT catch *.db). Guards against
# lint<->delivery drift: v1.31.8 shipped kit_mirror delivery without this case.
# ---------------------------------------------------------------------------
echo "--- T14: mirror source files (*.py/*.sql) => C4 PASS ---"
# Fixture is seeded (real mirror files) so C3/C6 don't SKIP -> overall exit is
# non-zero on this minimal fixture (no lifekit.sh); assert the C4 verdict only,
# same pattern as T4 allowlist-violation. The regression guard is "mirror
# source no longer FAILs C4".
check_output_contains "T14 C4 PASS mirror source" "$FX_DIR/mirror-source" "PASS.*C4"

echo ""
echo "--- T15: mirror runtime residue (*.db) => C4 FAIL (mirror branch, not C4b) ---"
# The residue .db is synthesized at runtime -- it is deliberately NOT tracked
# (*.db must never be committed; secret-sweep cat8 forbids it, and this repo
# publishes to the public mirror). The mirror/ dir + adapter.py are tracked;
# only the runtime-residue .db is created here so the checkout stays db-free.
: > "$FX_DIR/mirror-residue/payload/mirror/mirror_state.db"
check_fail "T15 mirror .db residue => non-zero exit" "$FX_DIR/mirror-residue"
check_output_contains "T15 C4 FAIL mirror residue" "$FX_DIR/mirror-residue" "FAIL.*C4.*mirror"

echo ""
# ---------------------------------------------------------------------------
# T16: fookit -- renamed kit (provides_kit=fookit) with a full DB lane must
# PASS end-to-end (DGN-1002 axis 1: kit name derived from manifest; C3 reads
# database/fookit.py, C4 allows service/fookit/ + config/fookit.conf, C6
# smokes database/fookit.sh).
# ---------------------------------------------------------------------------
echo "--- T16: fookit renamed kit with DB lane => PASS (DGN-1002 axis 1) ---"
check_pass "T16 fookit exit 0" "$FX_DIR/fookit"
check_output_contains "T16 kit name resolved" "$FX_DIR/fookit" "kit name resolved: fookit"
check_output_contains "T16 C3 PASS" "$FX_DIR/fookit" "PASS.*C3"
check_output_contains "T16 C4 PASS" "$FX_DIR/fookit" "PASS.*C4: payload path allowlist clean"
check_output_contains "T16 C6 PASS" "$FX_DIR/fookit" "PASS.*C6.*fookit.sh check \+ dump"

echo ""
# ---------------------------------------------------------------------------
# T17: nodb-kit -- capabilities.db_lane=false, no DB payload => C3/C6
# capability-SKIP and overall PASS (DGN-1002 axis 2).
# ---------------------------------------------------------------------------
echo "--- T17: no-DB-lane kit (db_lane=false) => C3/C6 SKIP, PASS (DGN-1002 axis 2) ---"
check_pass "T17 nodb-kit exit 0" "$FX_DIR/nodb-kit"
check_output_contains "T17 C3 capability SKIP" "$FX_DIR/nodb-kit" "SKIP.*C3.*db_lane=false"
check_output_contains "T17 C6 capability SKIP" "$FX_DIR/nodb-kit" "SKIP.*C6.*db_lane=false"

echo ""
# ---------------------------------------------------------------------------
# T18: lying-dblane -- capabilities.db_lane=true but no database files
# shipped => fail closed (C3 missing pin source, C6 missing CLI surface).
# ---------------------------------------------------------------------------
echo "--- T18: db_lane=true but DB files absent => FAIL (fail closed) ---"
check_fail "T18 lying declaration => non-zero exit" "$FX_DIR/lying-dblane"
check_output_contains "T18 C3 FAIL missing kit py" "$FX_DIR/lying-dblane" "FAIL.*C3.*payload/database/bazkit.py"
check_output_contains "T18 C6 FAIL missing kit sh" "$FX_DIR/lying-dblane" "FAIL.*C6.*payload/database/bazkit.sh"

echo ""
# ---------------------------------------------------------------------------
# T19: skip-smuggle -- capabilities.db_lane=false but database/ files ARE
# shipped => FAIL (a false declaration must not turn shipped-DB validation
# into a SKIP).
# ---------------------------------------------------------------------------
echo "--- T19: db_lane=false but database/ files shipped => FAIL (skip-smuggling refused) ---"
check_fail "T19 skip smuggle => non-zero exit" "$FX_DIR/skip-smuggle"
check_output_contains "T19 C3 FAIL contradiction" "$FX_DIR/skip-smuggle" "FAIL.*C3.*db_lane=false but payload/database"

echo ""
# ---------------------------------------------------------------------------
# T20: invalid provides_kit values => C1 FAIL + abort (fail closed). The old
# literal was inherently safe; a derived name is trusted only after the C1
# gate: empty string, glob chars, path traversal, framework-reserved names.
# ---------------------------------------------------------------------------
echo "--- T20: invalid provides_kit values => C1 FAIL + abort ---"
check_fail "T20a empty provides_kit => non-zero exit" "$FX_DIR/badname-empty"
check_output_contains "T20a C1 FAIL invalid name" "$FX_DIR/badname-empty" "FAIL.*C1.*provides_kit invalid"
check_fail "T20b glob provides_kit (*) => non-zero exit" "$FX_DIR/badname-glob"
check_output_contains "T20b C1 FAIL invalid name" "$FX_DIR/badname-glob" "FAIL.*C1.*provides_kit invalid"
check_fail "T20c traversal provides_kit (../bridge) => non-zero exit" "$FX_DIR/badname-traversal"
check_output_contains "T20c C1 FAIL invalid name" "$FX_DIR/badname-traversal" "FAIL.*C1.*provides_kit invalid"
check_fail "T20d reserved provides_kit (scripts) => non-zero exit" "$FX_DIR/badname-reserved"
check_output_contains "T20d C1 FAIL reserved name" "$FX_DIR/badname-reserved" "FAIL.*C1.*provides_kit invalid.*scripts"
check_output_contains "T20d abort before later checks" "$FX_DIR/badname-reserved" "ABORT: provides_kit invalid"

echo ""
# ---------------------------------------------------------------------------
# T21: malformed capabilities block => C1 FAIL (fail closed; DB_LANE keeps
# defaulting to true so no check is lost).
# ---------------------------------------------------------------------------
echo "--- T21: malformed capabilities block => C1 FAIL ---"
check_fail "T21a db_lane non-boolean => non-zero exit" "$FX_DIR/badcaps"
check_output_contains "T21a C1 FAIL boolean" "$FX_DIR/badcaps" "FAIL.*C1.*capabilities.*boolean"
check_fail "T21b unknown capabilities key => non-zero exit" "$FX_DIR/badcaps-unknown"
check_output_contains "T21b C1 FAIL unknown key" "$FX_DIR/badcaps-unknown" "FAIL.*C1.*unknown capabilities key"

echo ""
# ---------------------------------------------------------------------------
# T22: crossname -- kit=fookit shipping service/lifekit/ => C4 FAIL. The
# derived name REPLACES the lifekit allowance (no silent union with the old
# literal).
# ---------------------------------------------------------------------------
echo "--- T22: fookit shipping service/lifekit/ => C4 FAIL (no lifekit union) ---"
check_fail "T22 crossname service => non-zero exit" "$FX_DIR/crossname-service"
check_output_contains "T22 C4 FAIL non-kit service" "$FX_DIR/crossname-service" "FAIL.*C4.*service/<non-kit>.*kit=fookit.*lifekit"

echo ""
# ---------------------------------------------------------------------------
# T23: manifest parse gate (DGN-1004) -- an unparseable/non-object manifest
# must NOT fall through legacy-grace to a green exit. Distinguishes:
#   (a) manifest parses fine, no contract_version, kind != "kit" -> legacy
#       grace, exit 0 (T12 above, unchanged regression guard)
#   (b) manifest does not parse as a JSON object at all (malformed / missing
#       / non-object top-level) -> FAIL CLOSED, non-zero exit
# Before the fix, malformed JSON produced an empty contract/kind read via
# `|| echo ""`, which matched the legacy-grace condition and exited 0 with
# ALL CHECKS PASS -- a kit could ship corrupt JSON and get a green gate.
# ---------------------------------------------------------------------------
echo "--- T23a: malformed JSON manifest => FAIL CLOSED (not legacy-grace) ---"
check_fail "T23a malformed JSON => non-zero exit" "$FX_DIR/malformed-json"
check_output_contains "T23a C1 FAIL parse error" "$FX_DIR/malformed-json" "FAIL.*C1.*does not parse as a JSON object"
check_output_contains "T23a not-legacy message" "$FX_DIR/malformed-json" "FAIL.*C1.*an unparseable manifest is NOT a legacy pack"

echo ""
echo "--- T23b: missing manifest file => FAIL (unchanged: ordinary C1 not-found) ---"
check_fail "T23b missing manifest => non-zero exit" "$FX_DIR/no-manifest"
check_output_contains "T23b C1 FAIL not found" "$FX_DIR/no-manifest" "FAIL.*C1.*pack-manifest\.json not found"

echo ""
echo "--- T23c: top-level JSON array (not an object) => FAIL CLOSED ---"
check_fail "T23c array-top JSON => non-zero exit" "$FX_DIR/array-json"
check_output_contains "T23c C1 FAIL not an object" "$FX_DIR/array-json" "FAIL.*C1.*not an object"

echo ""
echo "--- T23d: top-level JSON string (not an object) => FAIL CLOSED ---"
check_fail "T23d string-top JSON => non-zero exit" "$FX_DIR/string-json"
check_output_contains "T23d C1 FAIL not an object" "$FX_DIR/string-json" "FAIL.*C1.*not an object"

echo ""
echo "--- T23e: genuine legacy pack (valid JSON object, no contract_version) still PASSES ---"
check_pass "T23e legacy-pack still exit 0 after DGN-1004 fix" "$FX_DIR/legacy-pack"
check_output_contains "T23e legacy-grace log intact" "$FX_DIR/legacy-pack" "legacy pack.*no contract_version.*v2 contract checks skipped"

echo ""
# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
