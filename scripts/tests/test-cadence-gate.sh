#!/bin/bash
# test-cadence-gate.sh -- self-test for scripts/lib/cadence-gate.sh (DGN-731 +
# DGN-880 WARN-only downgrade).
#
# Tests:
#   FP2 -- gap measurement + WARN threshold (cadence_minor_gap, cadence_check)
#   FP3 (DGN-880) -- cadence_check NEVER dies in ANY mode (WARN/telemetry only;
#         brick prevention is structural in the mirror-forward publisher)
#   migration-in-gap detection stays as telemetry (cadence_migration_in_gap)
#
# publish.sh dry-run integration test: run publish.sh --dry-run and verify the
# cadence gate C1 appears in the output (publish.sh must stay dry-run-safe).
#
# Exit 0 = all assertions pass; nonzero = a failure (prints which).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CADENCE_LIB="$SCRIPT_DIR/../lib/cadence-gate.sh"
PUBLISH="$SCRIPT_DIR/../publish.sh"
CANON_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

check_eq() {  # $1=desc $2=got $3=expected
  if [ "$2" = "$3" ]; then
    ok "$1 (got: $2)"
  else
    bad "$1 (got: '$2', expected: '$3')"
  fi
}

check_match() {  # $1=desc $2=string $3=pattern (grep -E)
  if printf '%s\n' "$2" | grep -qE "$3"; then
    ok "$1"
  else
    bad "$1 (string did not match /$3/: $2)"
  fi
}

check_not_match() {  # $1=desc $2=string $3=pattern (grep -E)
  if ! printf '%s\n' "$2" | grep -qE "$3"; then
    ok "$1"
  else
    bad "$1 (string unexpectedly matched /$3/: $2)"
  fi
}

echo "== cadence-gate.sh self-test (DGN-731) =="

# Source the library into this shell for unit tests.
# shellcheck source=../lib/cadence-gate.sh
. "$CADENCE_LIB"

# ============================================================================
# 1. cadence_minor_gap -- version gap arithmetic
# ============================================================================
echo "[1] cadence_minor_gap: version gap arithmetic"

check_eq "gap 1.18.0 -> 1.26.0 = 8"  "$(cadence_minor_gap 1.18.0 1.26.0)" "8"
check_eq "gap 1.26.0 -> 1.26.0 = 0"  "$(cadence_minor_gap 1.26.0 1.26.0)" "0"
check_eq "gap 1.25.0 -> 1.26.0 = 1"  "$(cadence_minor_gap 1.25.0 1.26.0)" "1"
check_eq "gap 1.26.0 -> 1.25.0 = 1 (absolute)"  "$(cadence_minor_gap 1.26.0 1.25.0)" "1"
check_eq "gap 1.26.1 -> 1.26.3 = 0 (patch only, same minor)"  "$(cadence_minor_gap 1.26.1 1.26.3)" "0"
check_eq "empty pub_ver -> 0 (graceful)"  "$(cadence_minor_gap '' 1.26.0)" "0"
check_eq "empty canon_ver -> 0 (graceful)"  "$(cadence_minor_gap 1.26.0 '')" "0"
# Cross-major: sentinel >= CADENCE_BLOCK_GAP (should be >=2 so block fires).
CROSS_MAJOR="$(cadence_minor_gap 1.26.0 2.0.0)"
[ "$CROSS_MAJOR" -ge 2 ] && ok "cross-major gap >= 2 (sentinel: $CROSS_MAJOR)" \
                          || bad "cross-major gap should be >= 2 (got: $CROSS_MAJOR)"

# ============================================================================
# 2. cadence_check -- FP2 WARN decision in dry-run mode
# ============================================================================
echo "[2] cadence_check: FP2 drift alarm (dry-run)"

# gap=0: no warn
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "1.26.0" "public" "main" "dry-run" 2>&1)"
check_match "gap=0 -> no drift PASS line" "$OUT" "no drift"
check_not_match "gap=0 -> no WARN fired" "$OUT" "WARN.*gap"

# gap=1: WARN fires (>= CADENCE_WARN=1), but no block (gap < CADENCE_BLOCK_GAP=2 and need to check mig)
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "1.25.0" "public" "main" "dry-run" 2>&1)"
check_match "gap=1 -> WARN fires" "$OUT" "WARN gap=1"

# gap=0 first publish (empty pub_ver)
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "" "public" "main" "dry-run" 2>&1)"
check_match "empty pub_ver -> first publish PASS" "$OUT" "first publish"

# ============================================================================
# 3. cadence_check -- DGN-880: WARN-only, no BLOCK message in dry-run
# ============================================================================
echo "[3] cadence_check: WARN-only in dry-run (DGN-880, no BLOCK)"

# gap=2 (old FP3 threshold): only a WARN, never a BLOCK message
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "1.24.0" "public" "main" "dry-run" 2>&1)"
check_match "gap=2 -> WARN fires" "$OUT" "WARN gap=2"
check_not_match "gap=2 -> NO block message (WARN-only)" "$OUT" "BLOCK"

# gap=8: still only a WARN
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "1.18.0" "public" "main" "dry-run" 2>&1)"
check_match "gap=8 -> WARN fires" "$OUT" "WARN gap=8"
check_not_match "gap=8 -> NO block message (WARN-only)" "$OUT" "BLOCK"

# ============================================================================
# 4. cadence_check -- DGN-880: NEVER dies in execute mode (exit 0 always)
# ============================================================================
echo "[4] cadence_check: never dies in execute mode (DGN-880)"

# gap=2 in execute mode: MUST exit 0 now (the old gate deadlock is removed;
# brick prevention is structural -- the publisher always steps one tag).
( cadence_check "$CANON_REPO" "1.26.0" "1.24.0" "public" "main" "execute" >/dev/null 2>&1 )
RC=$?
[ "$RC" -eq 0 ] && ok "gap=2 in execute mode -> exit 0 (WARN-only)" \
                || bad "gap=2 in execute mode must exit 0 now (got $RC)"

# gap=8 in execute mode: exit 0
( cadence_check "$CANON_REPO" "1.26.0" "1.18.0" "public" "main" "execute" >/dev/null 2>&1 )
RC=$?
[ "$RC" -eq 0 ] && ok "gap=8 in execute mode -> exit 0 (WARN-only)" \
                || bad "gap=8 in execute mode must exit 0 now (got $RC)"

# gap=1 + migration-in-gap in execute mode: exit 0 (the DGN-880 A1 deadlock
# case -- gap=1 with a migration used to BLOCK with no sanctioned path).
( cadence_check "$CANON_REPO" "1.17.0" "1.15.0" "public" "main" "execute" >/dev/null 2>&1 )
RC=$?
[ "$RC" -eq 0 ] && ok "migration-in-gap in execute mode -> exit 0 (telemetry only)" \
                || bad "migration-in-gap in execute mode must exit 0 now (got $RC)"

# gap=0 in execute mode: exit 0 (unchanged)
( CADENCE_WARN=2 cadence_check "$CANON_REPO" "1.26.0" "1.26.0" "public" "main" "execute" 2>/dev/null )
RC=$?
[ "$RC" -eq 0 ] && ok "gap=0 in execute mode -> exit 0 (no block)" \
                || bad "gap=0 in execute mode should exit 0 (got $RC)"

# ============================================================================
# 5. cadence_migration_in_gap -- migration detection on real repo tags
# ============================================================================
echo "[5] cadence_migration_in_gap: real repo tags"

# v1.15.0 -> v1.17.0: 010_unified_event.sql + 011_income_kind.sql were ADDED.
# These are the most recent migration additions in this repo's history.
MIG="$(cadence_migration_in_gap "$CANON_REPO" "1.15.0" "1.17.0")"
check_eq "v1.15.0->v1.17.0 has migration additions -> yes" "$MIG" "yes"

# v1.22.0 -> v1.26.0: no new migration files were added (only marker stamps
# in v1.18->v1.22 were modifications, not additions; schema user_version
# unchanged at 11 across this range).
MIG="$(cadence_migration_in_gap "$CANON_REPO" "1.22.0" "1.26.0")"
check_eq "v1.22.0->v1.26.0 no migration additions -> no" "$MIG" "no"

# Same version: v1.26.0 -> v1.26.0 = no gap = no migration
MIG="$(cadence_migration_in_gap "$CANON_REPO" "1.26.0" "1.26.0")"
check_eq "v1.26.0->v1.26.0 same version -> no migration" "$MIG" "no"

# Nonexistent tag -> unknown
MIG="$(cadence_migration_in_gap "$CANON_REPO" "0.0.0" "1.26.0")"
check_eq "nonexistent FROM tag -> unknown" "$MIG" "unknown"

MIG="$(cadence_migration_in_gap "$CANON_REPO" "1.26.0" "99.99.0")"
check_eq "nonexistent TO tag -> unknown" "$MIG" "unknown"

# ============================================================================
# 6. cadence_check -- migration-in-gap reported as TELEMETRY (DGN-880)
# ============================================================================
echo "[6] cadence_check: migration-in-gap telemetry, never a block (DGN-880)"

# v1.15.0 -> v1.17.0: gap=2 AND migration-in-gap=yes -> INFO telemetry only.
OUT="$(cadence_check "$CANON_REPO" "1.17.0" "1.15.0" "public" "main" "dry-run" 2>&1)"
check_match "mig-in-gap reported as telemetry" "$OUT" "migration-in-gap=yes"
check_not_match "mig-in-gap does NOT block" "$OUT" "BLOCK"

# gap=1, no migration (v1.25.0->v1.26.0): WARN only.
MIG_CHECK="$(cadence_migration_in_gap "$CANON_REPO" "1.25.0" "1.26.0")"
OUT="$(cadence_check "$CANON_REPO" "1.26.0" "1.25.0" "public" "main" "dry-run" 2>&1)"
check_match "gap=1 -> WARN fires" "$OUT" "WARN gap=1"
check_not_match "gap=1 -> no BLOCK line ever" "$OUT" "BLOCK"
ok "gap=1 mig-in-gap=$MIG_CHECK -- telemetry-only verified"

# ============================================================================
# 7. publish.sh --dry-run integration: gate C1 appears and exits 0
# ============================================================================
echo "[7] publish.sh --dry-run integration: cadence gate C1 visible"

TDIR="$(mktemp -d "${TMPDIR:-/tmp}/cadence-selftest.XXXXXX")"
LOG="$TDIR/log.txt"
bash "$PUBLISH" > "$LOG" 2>&1
PUBLISH_RC=$?
check_eq "publish.sh --dry-run exits 0" "$PUBLISH_RC" "0"

grep -q "gate C1: public-sync cadence gap" "$LOG"
check_eq "gate C1 header appears in publish.sh dry-run output" "$?" "0"

grep -q "nothing was created or pushed" "$LOG"
check_eq "publish.sh dry-run pushed nothing" "$?" "0"

# publish.sh sourced cadence-gate.sh without error if we got this far.
ok "cadence-gate.sh sourced by publish.sh without error"

rm -rf "$TDIR"

# ============================================================================
# 8. Threshold overrides via env vars
# ============================================================================
echo "[8] threshold overrides (CADENCE_WARN / CADENCE_BLOCK_GAP via env)"

# Lower CADENCE_WARN to 0: even gap=0 fires WARN (edge case, not practical but
# confirms the constant is honored).
OUT="$(CADENCE_WARN=0 cadence_check "$CANON_REPO" "1.26.0" "1.26.0" "public" "main" "dry-run" 2>&1)"
check_match "CADENCE_WARN=0, gap=0 -> WARN fires" "$OUT" "WARN gap=0"

# gap=8: never blocks regardless of CADENCE_BLOCK_GAP (retained for interface
# compat only -- DGN-880).
OUT="$(CADENCE_BLOCK_GAP=1 cadence_check "$CANON_REPO" "1.26.0" "1.18.0" "public" "main" "dry-run" 2>&1)"
check_not_match "CADENCE_BLOCK_GAP=1, gap=8 -> STILL no block (DGN-880)" "$OUT" "BLOCK"
check_match "CADENCE_BLOCK_GAP=1, gap=8 -> still warns" "$OUT" "WARN gap=8"

# ============================================================================
echo ""
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
