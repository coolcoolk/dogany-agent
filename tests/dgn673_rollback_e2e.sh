#!/bin/bash
# dgn673_rollback_e2e.sh -- DGN-673 release rollback path E2E (spec v3 section 7).
#
# Exercises the REAL update.sh rollback mode (B3): R1 reverse-drift ack-die,
# R2 exit-3-before-stamp checkpoint, the ROLLBACK MODE banner, and the plain
# scheduled fleet-remedy path (case 7, the doctrine load-bearing test).
#
# Fixture model (spec section 7, "scratch instance + clone of canonical with
# synthetic tags, never live"): each case builds a throwaway TARGET framework
# tree (a dir carrying the shipped update.sh + database/ + mirror/ + VERSION)
# and a throwaway INSTANCE under mktemp -d. Running
#   [envs] update.sh --root INST --no-pull
# from the TARGET tree is exactly what the release channel does after it
# git-archives the target tag and re-invokes the extracted tree's update.sh
# (update.sh L1015-1020) -- so a --no-pull run against a checked-out target
# tree faithfully models "instance consumes tag vX". No venv, no network, no
# live repo, no real agent instance is ever touched.
#
# Synthetic tags realized as distinct TARGET trees (spec fixtures):
#   vT0  = stable content, pin 5
#   vT1  = vT0 + code marker + additive migration 006 (reversible: yes) + pin 6
#   vT1n = vT0 + code marker only (no migration), pin 5
#   vT2  = revert release of vT1: behavior reverted (marker gone) but migration
#          006 + pin 6 RETAINED (spec section 1b class B; VERSION bumped)
#
# Usage: tests/dgn673_rollback_e2e.sh
# Exit: 0 = all pass, 1 = failures (summary at end).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQLITE="$(command -v sqlite3 || echo /usr/bin/sqlite3)"
UPDATE_SH="$REPO_ROOT/update.sh"

PASS=0
FAIL=0
FAILED_NAMES=()
ok()   { PASS=$((PASS+1)); echo "  ok    - $1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); echo "  FAIL  - $1${2:+ ($2)}"; }
check(){ if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1" "${3:-}"; fi; }

SANDBOXES=()
cleanup_all(){ local s; for s in ${SANDBOXES[@]+"${SANDBOXES[@]}"}; do [ -n "$s" ] && rm -rf "$s"; done; }
trap cleanup_all EXIT

db_ver(){ "$SQLITE" "file:$1?immutable=1" 'PRAGMA user_version;' 2>/dev/null; }
file_md5(){ md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
stamp_of(){ sed -n 's/^DOGANY_FW_VERSION=//p' "$1/.instance.conf" | head -n1; }
inst_pin(){ sed -n 's/^EXPECTED_USER_VERSION[[:space:]]*=[[:space:]]*//p' "$1/database/lifekit.py" | head -n1; }

# ---------------------------------------------------------------------------
# make_fw <dir> <version> <pin> <marker:0|1> <with_mig006:0|1>
#   Build a minimal-but-REAL target framework tree that update.sh accepts.
#   marker=1 writes a behavioral marker line into lifekit.sh (the "behavior");
#   with_mig006=1 ships an additive reversible migration 006 bumping to <pin>.
# ---------------------------------------------------------------------------
make_fw(){
  local dir="$1" ver="$2" pin="$3" marker="$4" mig="$5"
  mkdir -p "$dir/database/migrations" "$dir/mirror" "$dir/agents/.template"
  cp "$UPDATE_SH" "$dir/update.sh"
  printf '%s\n' "$ver" > "$dir/VERSION"
  printf 'EXPECTED_USER_VERSION = %s\n' "$pin" > "$dir/database/lifekit.py"
  printf 'MIN_USER_VERSION = 5\n' > "$dir/mirror/sdk_bridge.py"
  printf 'PRAGMA user_version=5;\n' > "$dir/database/schema.sql"
  if [ "$marker" = "1" ]; then
    printf '#!/bin/bash\n# BEHAVIOR_MARKER_VT1\necho lifekit\n' > "$dir/database/lifekit.sh"
  else
    printf '#!/bin/bash\necho lifekit\n' > "$dir/database/lifekit.sh"
  fi
  if [ "$mig" = "1" ]; then
    printf -- '-- reversible: yes\n-- down: none\nPRAGMA user_version=%s;\n' "$pin" \
      > "$dir/database/migrations/006_synthetic_additive.sql"
  fi
}

# ---------------------------------------------------------------------------
# make_inst <dir> <fw_dir> <pin> <db_ver> <stamp>
#   Build a throwaway instance seeded FROM a framework tree at a given code pin
#   and DB user_version, with a prior DOGANY_FW_VERSION stamp.
# ---------------------------------------------------------------------------
make_inst(){
  local dir="$1" fw="$2" pin="$3" dbv="$4" stamp="$5"
  mkdir -p "$dir/database/migrations" "$dir/mirror" "$dir/config"
  cp "$fw/database/lifekit.sh" "$dir/database/lifekit.sh"
  printf 'EXPECTED_USER_VERSION = %s\n' "$pin" > "$dir/database/lifekit.py"
  printf 'MIN_USER_VERSION = 5\n' > "$dir/mirror/sdk_bridge.py"
  printf 'PRAGMA user_version=5;\n' > "$dir/database/schema.sql"
  printf 'DOGANY_FW_VERSION=%s\n' "$stamp" > "$dir/.instance.conf"
  "$SQLITE" "$dir/database/lifekit.db" "PRAGMA user_version=$dbv;"
}

new_sb(){ local sb; sb="$(mktemp -d "${TMPDIR:-/tmp}/dgn673-e2e.XXXXXX")"; SANDBOXES+=("$sb"); echo "$sb"; }

# ===========================================================================
echo "== case 1: forward leg -- update to vT1, migration applies, stamp updates =="
SB="$(new_sb)"; VT1="$SB/vT1"; INST="$SB/inst"
make_fw "$VT1" 1.21.0 6 1 1
make_inst "$INST" "$VT1" 5 5 1.20.0     # instance at pin 5 / DB 5, older stamp
bash "$VT1/update.sh" --root "$INST" --no-pull --yes >"$SB/c1.log" 2>&1
check "case1 forward update exit 0" $?
[ "$(db_ver "$INST/database/lifekit.db")" = "6" ]; check "case1 migration 006 applied (DB v6)" $?
grep -q 'BEHAVIOR_MARKER_VT1' "$INST/database/lifekit.sh"; check "case1 behavior marker landed" $?
ls "$INST"/database/lifekit.db.v5.bak-* >/dev/null 2>&1; check "case1 version-stamped .backup snapshot exists (672-M4)" $?
[ "$(stamp_of "$INST")" = "1.21.0" ]; check "case1 DOGANY_FW_VERSION stamped forward" $?
! grep -q 'ROLLBACK MODE' "$SB/c1.log"; check "case1 no ROLLBACK banner on forward run" $?

# ===========================================================================
echo "== case 2: code-only rollback -- vT1n -> vT0, DB untouched =="
SB="$(new_sb)"; VT0="$SB/vT0"; INST="$SB/inst"
make_fw "$VT0" 1.20.0 5 0 0
make_inst "$INST" "$VT0" 5 5 1.21.0     # code ahead (marker), DB at 5 (== target pin)
printf '#!/bin/bash\n# BEHAVIOR_MARKER_VT1\necho lifekit\n' > "$INST/database/lifekit.sh"  # vT1n behavior
DB_MD5_BEFORE="$(file_md5 "$INST/database/lifekit.db")"
DOGANY_ROLLBACK=1 bash "$VT0/update.sh" --root "$INST" --no-pull --yes >"$SB/c2.log" 2>&1
check "case2 code-only rollback exit 0" $?
[ "$(file_md5 "$INST/database/lifekit.db")" = "$DB_MD5_BEFORE" ]; check "case2 DB byte-identical (untouched)" $?
! grep -q 'BEHAVIOR_MARKER_VT1' "$INST/database/lifekit.sh"; check "case2 behavior reverted (marker gone)" $?
grep -q 'ROLLBACK MODE' "$SB/c2.log"; check "case2 ROLLBACK banner printed" $?
[ "$(stamp_of "$INST")" = "1.20.0" ]; check "case2 stamp = vPREV (rollback-complete)" $?

# ===========================================================================
echo "== case 3: schema rollback money path -- exit3, restore, re-run seals stamp =="
SB="$(new_sb)"; VT0="$SB/vT0"; INST="$SB/inst"
make_fw "$VT0" 1.20.0 5 0 0             # target pin 5
make_inst "$INST" "$VT0" 6 6 1.21.0     # instance pin 6 / DB 6 (post-vT1)
printf '#!/bin/bash\n# BEHAVIOR_MARKER_VT1\necho lifekit\n' > "$INST/database/lifekit.sh"
# instance carries vT1's additive reversible migration 006 (in the delta)
printf -- '-- reversible: yes\n-- down: none\nPRAGMA user_version=6;\n' > "$INST/database/migrations/006_synthetic_additive.sql"
# post-upgrade canary row proxy: a verified v5 snapshot with a real loss window.
# The snapshot user_version == target 5 (rollback restore point).
"$SQLITE" "$INST/database/lifekit.db.v5.bak-20260801-000000" 'PRAGMA user_version=5;'
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0/update.sh" --root "$INST" --no-pull --yes >"$SB/c3a.log" 2>&1
[ "$?" -eq 3 ]; check "case3 pinned run exits 3 (checkpoint, DB ahead)" $?
grep -q 'restore-data.sh --to' "$SB/c3a.log"; check "case3 exit-3 message points at restore-data.sh" $?
[ "$(stamp_of "$INST")" = "1.21.0" ]; check "case3 stamp WITHHELD at exit 3 (still vBAD)" $?
# operator restore: DB back to target pin 5 (restore-data.sh delegated; emulate the swap)
"$SQLITE" "$INST/database/lifekit.db" 'PRAGMA user_version=5;'
# then re-run the SAME pinned update -> passes checkpoint, seals stamp
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0/update.sh" --root "$INST" --no-pull --yes >"$SB/c3b.log" 2>&1
check "case3 re-run after restore exits 0" $?
[ "$(db_ver "$INST/database/lifekit.db")" = "5" ]; check "case3 DB user_version == target 5" $?
[ "$(inst_pin "$INST")" = "5" ]; check "case3 instance lifekit.py pin == target 5 (2b verify)" $?
[ "$(stamp_of "$INST")" = "1.20.0" ]; check "case3 stamp sealed = vPREV (rollback-complete)" $?

# ===========================================================================
echo "== case 4: R1 regression three-way (skip / die / ack-lands) =="
SB="$(new_sb)"; VT0="$SB/vT0"
make_fw "$VT0" 1.20.0 5 0 0
# 4a: pinned downgrade WITHOUT rollback mode -> guard still SKIPS (intact)
I4="$SB/i4a"; make_inst "$I4" "$VT0" 6 5 1.21.0
bash "$VT0/update.sh" --root "$I4" --no-pull --yes >"$SB/c4a.log" 2>&1
c4a=$?
grep -q 'REVERSE-DRIFT GUARD triggered' "$SB/c4a.log"; check "case4a guard skips without rollback mode (original purpose intact)" $?
[ "$(inst_pin "$I4")" = "6" ]; check "case4a lifekit.py NOT downgraded (skip honored)" $?
[ "$c4a" -eq 0 ]; check "case4a run completes 0" $?
# 4b: DOGANY_ROLLBACK=1 but NO ack -> loud DIE
I4="$SB/i4b"; make_inst "$I4" "$VT0" 6 5 1.21.0
DOGANY_ROLLBACK=1 bash "$VT0/update.sh" --root "$I4" --no-pull --yes >"$SB/c4b.log" 2>&1
[ "$?" -ne 0 ]; check "case4b rollback mode + no ack -> non-zero die" $?
grep -q 'needs per-file consent' "$SB/c4b.log"; check "case4b die message demands DOGANY_ROLLBACK_ACK" $?
[ "$(inst_pin "$I4")" = "6" ]; check "case4b lifekit.py untouched (die before overwrite)" $?
# 4c: matching DOGANY_ROLLBACK_ACK -> file LANDS (downgraded to 5)
I4="$SB/i4c"; make_inst "$I4" "$VT0" 6 5 1.21.0
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0/update.sh" --root "$I4" --no-pull --yes >"$SB/c4c.log" 2>&1
check "case4c rollback mode + matching ack exit 0" $?
[ "$(inst_pin "$I4")" = "5" ]; check "case4c lifekit.py downgraded to target 5 (ack landed)" $?

# ===========================================================================
echo "== case 5: refusals (irreversible / missing-tag / DB-ahead-no-restore) =="
SB="$(new_sb)"; VT0="$SB/vT0"
make_fw "$VT0" 1.20.0 5 0 0
# 5a: crossing an irreversible (unclassifiable) migration, NO snapshot -> hard REFUSE
I5="$SB/i5a"; make_inst "$I5" "$VT0" 6 6 1.21.0   # DB 6, no migration 006 present -> unclassifiable
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0/update.sh" --root "$I5" --no-pull --yes >"$SB/c5a.log" 2>&1
[ "$?" -ne 0 ]; check "case5a irreversible+no-snapshot -> hard refuse non-zero" $?
grep -q 'REFUSE' "$SB/c5a.log" && grep -q 'DGN-672' "$SB/c5a.log"
check "case5a refuse names DGN-672 restore story pointer" $?
# 5b: DOGANY_UPDATE_PIN names a MISSING tag -> loud die (phase1 re-assert), env variant.
# resolve_channel_tag extracted from the shipped update.sh and exercised directly.
SNIP="$SB/sel.sh"
{ printf '%s\n' 'msg(){ printf "%s\n" "$2" >&2; }'
  printf '%s\n' 'die(){ printf "[ERROR] %s\n" "$1" >&2; exit 1; }'
  sed -n '/^resolve_channel_tag() {/,/^}/p' "$UPDATE_SH"; } > "$SNIP"
GITREPO="$SB/gitrepo"; mkdir -p "$GITREPO"
git -C "$GITREPO" init -q -b main
git -C "$GITREPO" -c user.email=t@t -c user.name=t commit -q --allow-empty -m x
git -C "$GITREPO" tag v1.0.0
( . "$SNIP"; DOGANY_UPDATE_PIN="v9.9.9-missing" resolve_channel_tag "$GITREPO" release ) >/dev/null 2>"$SB/c5b.log"
[ "$?" -ne 0 ]; check "case5b missing pin tag -> loud die (env), never silent latest" $?
grep -q 'refusing to fall back to latest' "$SB/c5b.log"; check "case5b die message states no-fallback" $?
# 5c: rollback mode, DB ahead, NO restore performed -> exit 3 + NO stamp (isolated)
I5="$SB/i5c"; make_inst "$I5" "$VT0" 6 6 1.21.0
printf -- '-- reversible: yes\n-- down: none\nPRAGMA user_version=6;\n' > "$I5/database/migrations/006_synthetic_additive.sql"
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0/update.sh" --root "$I5" --no-pull --yes >"$SB/c5c.log" 2>&1
[ "$?" -eq 3 ]; check "case5c rollback mode + DB ahead + no restore -> exit 3" $?
[ "$(stamp_of "$I5")" = "1.21.0" ]; check "case5c NO stamp written at exit 3" $?

# ===========================================================================
echo "== case 6: S9 rehearsal -- target tree WITHOUT rollback mode (old tag) =="
SB="$(new_sb)"; VT0OLD="$SB/vT0old"; INST="$SB/inst"
make_fw "$VT0OLD" 1.15.0 5 0 0
# simulate a pre-rollback-aware update.sh: strip every DOGANY_ROLLBACK mention
sed '/DOGANY_ROLLBACK/d' "$UPDATE_SH" > "$VT0OLD/update.sh"
! grep -q 'DOGANY_ROLLBACK' "$VT0OLD/update.sh"; check "case6 old target tree has no rollback mode (S9 precondition)" $?
# capability check (runbook step 1) detects the gap
grep -q 'DOGANY_ROLLBACK' "$VT0OLD/update.sh"; cap=$?   # non-zero == absent
[ "$cap" -ne 0 ]; check "case6 capability check (grep DOGANY_ROLLBACK) reports ABSENT" $?
make_inst "$INST" "$VT0OLD" 6 5 1.21.0   # code ahead, DB at target (code-only shape)
# old update.sh ignores DOGANY_ROLLBACK -> guard SKIPS lifekit.py -> torn tree (documented)
DOGANY_ROLLBACK=1 DOGANY_ROLLBACK_ACK="lifekit_py:6->5" \
  bash "$VT0OLD/update.sh" --root "$INST" --no-pull --yes >"$SB/c6.log" 2>&1 || true
[ "$(inst_pin "$INST")" = "6" ]; check "case6 torn result confirmed (lifekit.py still ahead, guard skipped)" $?
# manual guarded-file copy step (runbook step 5) heals the torn file
cp "$VT0OLD/database/lifekit.py" "$INST/database/lifekit.py"
cp "$VT0OLD/mirror/sdk_bridge.py" "$INST/mirror/sdk_bridge.py"
[ "$(inst_pin "$INST")" = "5" ]; check "case6 manual guarded-file copy heals torn tree (2b verify green)" $?

# ===========================================================================
echo "== case 7: fleet remedy vT2 via PLAIN scheduled path -- NO torn state =="
# The doctrine load-bearing test. Instance on vT1 (migration applied, DB 6,
# pin 6) consumes vT2 -- revert release: behavior reverted, migration 006 +
# pin 6 RETAINED. Consumed via the PLAIN update (no env vars, no rollback
# mode) -- exactly the scheduled path a fleet takes.
SB="$(new_sb)"; VT2="$SB/vT2"; INST="$SB/inst"
make_fw "$VT2" 1.22.0 6 0 1             # behavior reverted (marker=0), mig006+pin6 RETAINED
make_inst "$INST" "$VT2" 6 6 1.21.0     # instance at vT1 shape: pin 6 / DB 6
printf '#!/bin/bash\n# BEHAVIOR_MARKER_VT1\necho lifekit\n' > "$INST/database/lifekit.sh"  # vT1 behavior present
printf -- '-- reversible: yes\n-- down: none\nPRAGMA user_version=6;\n' > "$INST/database/migrations/006_synthetic_additive.sql"
DB_MD5_BEFORE="$(file_md5 "$INST/database/lifekit.db")"
# PLAIN run: no DOGANY_ROLLBACK, no ack, no pin -- the scheduled shape.
bash "$VT2/update.sh" --root "$INST" --no-pull --yes >"$SB/c7.log" 2>&1
check "case7 plain scheduled update exit 0 (no rollback mode)" $?
! grep -q 'ROLLBACK MODE' "$SB/c7.log"; check "case7 no rollback banner (plain path)" $?
! grep -q 'REVERSE-DRIFT GUARD triggered' "$SB/c7.log"; check "case7 NO torn state: guard silent (pins equal both sides)" $?
[ "$(inst_pin "$INST")" = "6" ]; check "case7 landed lifekit.py pin == 6 (schema retained)" $?
[ "$(db_ver "$INST/database/lifekit.db")" = "6" ]; check "case7 DB user_version unchanged == 6 (schema retained)" $?
[ "$(file_md5 "$INST/database/lifekit.db")" = "$DB_MD5_BEFORE" ]; check "case7 DB untouched (no migration re-applied)" $?
[ -f "$INST/database/migrations/006_synthetic_additive.sql" ]; check "case7 migration 006 still present (retained)" $?
! grep -q 'BEHAVIOR_MARKER_VT1' "$INST/database/lifekit.sh"; check "case7 behavior reverted (marker gone)" $?
[ "$(inst_pin "$INST")" = "$(db_ver "$INST/database/lifekit.db")" ]; check "case7 R2 invariant: landed pin == DB user_version (no torn state)" $?
[ "$(stamp_of "$INST")" = "1.22.0" ]; check "case7 DOGANY_FW_VERSION == vT2" $?

# ===========================================================================
echo "== case 8: re-upgrade leg -- clear pin, update to vT1, 006 re-applies =="
SB="$(new_sb)"; VT1="$SB/vT1"; INST="$SB/inst"
make_fw "$VT1" 1.21.0 6 1 1
make_inst "$INST" "$VT1" 5 5 1.20.0     # rolled-back shape: code+DB at vT0 (5)
bash "$VT1/update.sh" --root "$INST" --no-pull --yes >"$SB/c8.log" 2>&1
check "case8 re-upgrade exit 0" $?
[ "$(db_ver "$INST/database/lifekit.db")" = "6" ]; check "case8 migration 006 re-applies cleanly (round-trip)" $?
grep -q 'BEHAVIOR_MARKER_VT1' "$INST/database/lifekit.sh"; check "case8 behavior back (marker present)" $?
[ "$(stamp_of "$INST")" = "1.21.0" ]; check "case8 stamp forward to vT1 again" $?

# ===========================================================================
echo ""
echo "==================================================================="
echo "DGN-673 rollback E2E: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  printf ' failed: %s\n' "${FAILED_NAMES[@]}"
  exit 1
fi
echo "all green."
exit 0
