#!/bin/bash
# dgn672_backup_restore_test.sh -- DGN-672 lifekit backup/restore E2E (T1-T7).
#
# Every test runs against a throwaway sandbox instance built under mktemp -d;
# the live repo / any real agent instance is NEVER touched (spec section 8).
# Also verifies the estate principle "wholesale binary overwrite is never a
# default path": every restore observed here goes through safety copy +
# candidate verify + version gate + post-verify (T3/T6 assert the stops).
#
# Usage: tests/dgn672_backup_restore_test.sh
# Exit: 0 = all pass, 1 = failures (summary at the end).
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQLITE="$(command -v sqlite3 || echo /usr/bin/sqlite3)"

# DGN-803 LS-5: the lifekit core (lifekit.py/.sh, schema.sql) left the
# framework tree -- it ships as the independent lifekit pack. This suite
# tests the FRAMEWORK backup/restore engine (residue: backup-data.sh,
# restore-data.sh) against an INSTALLED lifekit surface, so the fixture now
# sources the core from the pack payload (sibling checkout convention,
# override via DOGANY_LIFEKIT_PAYLOAD) -- exactly the files pack_install.sh
# places on an instance at kit activation. Payload absent -> loud SKIP
# (exit 0): the seam cannot be exercised without the pack checkout.
LIFEKIT_PAYLOAD="${DOGANY_LIFEKIT_PAYLOAD:-$REPO_ROOT/../dogany-lifekit/payload}"
if [[ ! -f "$LIFEKIT_PAYLOAD/database/lifekit.py" ]]; then
  if [[ "${DGN803_RELEASE_GATE:-0}" == "1" ]]; then
    echo "FAIL: lifekit pack payload not found at $LIFEKIT_PAYLOAD (DGN803_RELEASE_GATE=1 -- payload required)" >&2
    echo "      clone the dogany-lifekit repo next to this one (or set DOGANY_LIFEKIT_PAYLOAD)" >&2
    exit 1
  fi
  echo "SKIP: lifekit pack payload not found at $LIFEKIT_PAYLOAD (DGN-803 LS-5)"
  echo "      clone the dogany-lifekit repo next to this one (or set DOGANY_LIFEKIT_PAYLOAD) to run the backup/restore seam suite"
  echo "      set DGN803_RELEASE_GATE=1 to require payload presence (release gate mode)"
  exit 0
fi

# The pack owns the pin now, so T6's version-gate legs derive their fixture
# versions from the payload pin instead of the retired canon literal (11).
LK_PIN="$(sed -n 's/^EXPECTED_USER_VERSION[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
  "$LIFEKIT_PAYLOAD/database/lifekit.py" | head -1)"
if ! [[ "$LK_PIN" =~ ^[0-9]+$ ]]; then
  echo "SKIP: cannot parse EXPECTED_USER_VERSION from payload lifekit.py (DGN-803 LS-5)"
  exit 0
fi
LK_P1=$((LK_PIN+1)); LK_P2=$((LK_PIN+2))
printf -v LK_P1_PAD '%03d' "$LK_P1"
printf -v LK_P2_PAD '%03d' "$LK_P2"
printf -v LK_PIN_PAD '%03d' "$LK_PIN"

PASS=0
FAIL=0
FAILED_NAMES=()

ok()   { PASS=$((PASS+1)); echo "  ok    - $1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); echo "  FAIL  - $1${2:+ ($2)}"; }
check() { # check <name> <exit-code-of-condition>
  if [[ "$2" -eq 0 ]]; then ok "$1"; else bad "$1"; fi
}

SANDBOXES=()
cleanup_all() { local s; for s in "${SANDBOXES[@]:-}"; do [[ -n "$s" ]] && rm -rf "$s"; done; }
trap cleanup_all EXIT

# ---------------------------------------------------------------------------
# sandbox factory: fake agent instance (database/ + routines/ + git repo)
#   make_sandbox <with_db:1|0>  -> echoes sandbox path
# ---------------------------------------------------------------------------
make_sandbox() {
  local with_db="$1"
  local sb
  sb="$(mktemp -d "${TMPDIR:-/tmp}/dgn672-sb.XXXXXX")"
  mkdir -p "$sb/database" "$sb/routines" "$sb/.telegram_bot/logs"
  # Core = pack payload (installed-surface shape); restore-data.sh = framework
  # engine residue, still sourced from this repo (DGN-803 LS-5 seam split).
  cp "$LIFEKIT_PAYLOAD/database/lifekit.py" "$LIFEKIT_PAYLOAD/database/lifekit.sh" \
     "$LIFEKIT_PAYLOAD/database/schema.sql" \
     "$sb/database/"
  cp "$REPO_ROOT/database/restore-data.sh" "$sb/database/"
  mkdir -p "$sb/database/migrations"
  cp "$REPO_ROOT/agents/.template/routines/backup-data.sh" "$sb/routines/"
  cp "$REPO_ROOT/agents/.template/.gitignore" "$sb/.gitignore"
  chmod +x "$sb/database/lifekit.sh" "$sb/database/restore-data.sh" \
           "$sb/routines/backup-data.sh"
  # push.sh stub: records calls, never talks to Telegram.
  cat > "$sb/routines/push.sh" <<'EOF'
#!/bin/bash
echo "$@" >> "$(cd "$(dirname "$0")/.." && pwd)/.telegram_bot/push-calls.log"
exit 0
EOF
  chmod +x "$sb/routines/push.sh"
  git -C "$sb" init -q
  git -C "$sb" config user.email "test@dgn672.local"
  git -C "$sb" config user.name "dgn672-test"
  git -C "$sb" add -A
  git -C "$sb" commit -qm "initial"
  if [[ "$with_db" -eq 1 ]]; then
    "$SQLITE" "$sb/database/lifekit.db" < "$sb/database/schema.sql"
  fi
  SANDBOXES+=("$sb")
  echo "$sb"
}

db_ver() { "$SQLITE" "file:$1?immutable=1" 'PRAGMA user_version;' 2>/dev/null; }
# task-add writes to the unified event table (DGN-579), not legacy tasks.
task_rows() { "$SQLITE" "file:$1?immutable=1" "SELECT COUNT(*) FROM event;" 2>/dev/null; }
file_md5() { md5 -q "$1" 2>/dev/null || md5sum "$1" | cut -d' ' -f1; }
quiesce() { "$SQLITE" "$1" 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1; }

# ===========================================================================
echo "== T1: corrupt canary (check gate + never-pollute chain + restore) =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t1 task one" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
check "T1 initial backup runs" $?
[[ -f "$SB/database/lifekit.sql" ]]; check "T1 lifekit.sql exists" $?
grep -q "^PRAGMA user_version=$LK_PIN;" "$SB/database/lifekit.sql"
check "T1 dump self-carries user_version (C4)" $?
git -C "$SB" log --oneline | grep -q "backup: lifekit dump"
check "T1 backup committed" $?
SNAP_MD5="$(file_md5 "$SB/database/lifekit.sql")"
SNAP_TASKS="$(task_rows "$SB/database/lifekit.db")"

# corrupt the DB header (deterministic integrity FAIL); drop sidecars
printf 'CORRUPTCORRUPT' | dd of="$SB/database/lifekit.db" bs=1 seek=16 conv=notrunc 2>/dev/null
rm -f "$SB/database/lifekit.db-wal" "$SB/database/lifekit.db-shm"
"$SB/database/lifekit.sh" check >/dev/null 2>&1
[[ $? -eq 1 ]]; check "T1 check FAILs on corrupt DB" $?
"$SB/routines/backup-data.sh" >/dev/null 2>&1
[[ $? -eq 1 ]]; check "T1 backup refuses corrupt DB (exit 1)" $?
[[ "$(file_md5 "$SB/database/lifekit.sql")" == "$SNAP_MD5" ]]
check "T1 last good lifekit.sql byte-identical (chain unpolluted)" $?
grep -q "corruption detected" "$SB/.telegram_bot/push-calls.log" 2>/dev/null
check "T1 corruption alert pushed (24h canary)" $?

"$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
check "T1 restore --to latest from corrupt live succeeds" $?
"$SB/database/lifekit.sh" check >/dev/null 2>&1
check "T1 restored DB integrity ok" $?
[[ "$(task_rows "$SB/database/lifekit.db")" == "$SNAP_TASKS" ]]
check "T1 row counts == snapshot" $?
ls "$SB/database"/lifekit.db.corrupt.bak-* >/dev/null 2>&1
check "T1 corrupt bytes preserved as safety copy (forensics)" $?

# ===========================================================================
echo "== T2: disaster restore (DB files gone -> git .sql + catch-up) =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t2 task" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
rm -f "$SB/database/lifekit.db" "$SB/database/lifekit.db-wal" "$SB/database/lifekit.db-shm"
OUT="$("$SB/database/restore-data.sh" --to latest 2>&1)"
check "T2 restore succeeds with live DB missing" $?
echo "$OUT" | grep -q "disaster path"
check "T2 disaster path noticed (safety copy skipped, continued)" $?
[[ "$(db_ver "$SB/database/lifekit.db")" == "$LK_PIN" ]]
check "T2 user_version == instance EXPECTED after catch-up" $?
"$SB/database/lifekit.sh" agg-day 2026-08-01 >/dev/null 2>&1
check "T2 substantive owner read (agg-day) works" $?
[[ "$(task_rows "$SB/database/lifekit.db")" == "1" ]]
check "T2 data recovered" $?

# ===========================================================================
echo "== T3: clobber-guard + rollback (R3 + safety copy + swap-back) =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t3 task" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
quiesce "$SB/database/lifekit.db"
LIVE_MD5="$(file_md5 "$SB/database/lifekit.db")"

# (a) kill (TERM) inside the materialize->swap window: live untouched, no residue
DGN672_TEST_PAUSE_BEFORE_SWAP=15 "$SB/database/restore-data.sh" --to latest >/dev/null 2>&1 &
RPID=$!
sleep 3
kill "$RPID" 2>/dev/null
wait "$RPID" 2>/dev/null
sleep 1
[[ "$(file_md5 "$SB/database/lifekit.db")" == "$LIVE_MD5" ]]
check "T3 live DB untouched after kill before swap" $?
ls "$SB/database"/tmp-restore-*.db >/dev/null 2>&1
[[ $? -ne 0 ]]; check "T3 staged candidate cleaned by trap" $?
[[ -z "$(git -C "$SB" status --porcelain -- database/)" ]]
check "T3 git status clean after kill" $?

# (b) successful restore leaves a valid pre-swap safety copy
"$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
check "T3 normal restore succeeds" $?
BAK="$(ls -t "$SB/database"/lifekit.db.v${LK_PIN}.bak-* 2>/dev/null | head -1)"
[[ -n "$BAK" ]]; check "T3 versioned safety copy exists (C3 naming)" $?
[[ "$("$SQLITE" "file:$BAK?immutable=1" 'PRAGMA integrity_check;' 2>/dev/null)" == "ok" ]]
check "T3 safety copy is a valid DB" $?

# (c) post-verify failure injection -> automatic swap-back, byte-identical
sleep 1  # new safety-copy timestamp
DGN672_TEST_FAIL_POSTVERIFY=1 "$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
[[ $? -eq 2 ]]; check "T3 post-verify failure exits 2" $?
BAK2="$(ls -t "$SB/database"/lifekit.db.v${LK_PIN}.bak-* 2>/dev/null | head -1)"
cmp -s "$SB/database/lifekit.db" "$BAK2"
check "T3 swap-back byte-identical to safety copy" $?

# (d) dump killed mid-run (R3): no lifekit.sql.* residue, git clean
cat > "$SB/database/lifekit-slow.sh" <<EOF
#!/bin/bash
# test-only wrapper: slow dump to open a kill window
if [ "\$1" = "dump" ]; then sleep 15; fi
exec "$SB/database/lifekit-real.sh" "\$@"
EOF
mv "$SB/database/lifekit.sh" "$SB/database/lifekit-real.sh"
mv "$SB/database/lifekit-slow.sh" "$SB/database/lifekit.sh"
chmod +x "$SB/database/lifekit.sh" "$SB/database/lifekit-real.sh"
"$SB/routines/backup-data.sh" >/dev/null 2>&1 &
BPID=$!
sleep 3
kill "$BPID" 2>/dev/null
wait "$BPID" 2>/dev/null
sleep 1
ls "$SB/database"/lifekit.sql.* >/dev/null 2>&1
[[ $? -ne 0 ]]; check "T3 no lifekit.sql.* residue after killed dump (R3 trap)" $?
# scope to the dump surface: the test itself renamed lifekit.sh (mock wrapper)
[[ -z "$(git -C "$SB" status --porcelain -- 'database/lifekit.sql*')" ]]
check "T3 git status clean on dump surface after killed dump (R3)" $?
touch "$SB/database/lifekit.sql.residue"  # simulate kill -9 residue
[[ -z "$(git -C "$SB" status --porcelain -- 'database/lifekit.sql*')" ]]
check "T3 kill -9 residue is gitignore-covered (R3 second defense)" $?
rm -f "$SB/database/lifekit.sql.residue"
mv "$SB/database/lifekit-real.sh" "$SB/database/lifekit.sh"

# ===========================================================================
echo "== T4: swap contract (stale WAL removal + concurrent writers) =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t4 task" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
# (a) leave a genuinely uncheckpointed WAL (os._exit skips close/checkpoint)
python3 - "$SB/database/lifekit.db" <<'PY'
import os
import sqlite3
import sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA wal_autocheckpoint=0;")
conn.execute("INSERT OR REPLACE INTO config(key, value) VALUES('t4', 'wal');")
conn.commit()
os._exit(0)  # skip close -> WAL stays on disk, uncheckpointed
PY
[[ -f "$SB/database/lifekit.db-wal" ]]
check "T4 fixture: uncheckpointed -wal present" $?
"$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
check "T4 restore over uncheckpointed WAL succeeds" $?
# The STALE sidecars are removed inside the swap; post-verify reads may
# legitimately recreate FRESH ones on the new file. The stale-WAL proof is
# that the old WAL's uncommitted-to-main row never bled into the candidate.
[[ -z "$("$SQLITE" "$SB/database/lifekit.db" "SELECT value FROM config WHERE key='t4';")" ]]
check "T4 stale WAL content did not bleed into restored DB" $?
"$SB/database/lifekit.sh" check >/dev/null 2>&1
check "T4 clean read after swap" $?

# (b) per-invocation writer hammering during restore: consistent swap or ABORT
(
  for _ in $(seq 1 60); do
    "$SQLITE" -cmd '.timeout 2000' "$SB/database/lifekit.db" \
      "INSERT OR REPLACE INTO config(key, value) VALUES('hammer', hex(randomblob(4)));" \
      >/dev/null 2>&1
  done
) &
HPID=$!
"$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
RRC=$?
wait "$HPID" 2>/dev/null
[[ "$RRC" -eq 0 || "$RRC" -eq 1 ]]
check "T4 hammered restore = clean swap (0) or busy ABORT (1), never forced (rc=$RRC)" $?
"$SB/database/lifekit.sh" check >/dev/null 2>&1
check "T4 live DB healthy after hammering (no lost-write torn state)" $?

# ===========================================================================
echo "== T5: mint silent paths (no remote / no DB) + push-fail counter =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t5 task" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
check "T5 no-remote repo: local commit, exit 0" $?
git -C "$SB" log --oneline | grep -q "backup: lifekit dump"
check "T5 local backup commit exists" $?

SB2="$(make_sandbox 0)"   # non-lifekit mint: no DB at all
OUT="$("$SB2/routines/backup-data.sh" 2>&1)"
RC=$?
[[ "$RC" -eq 0 && -z "$OUT" ]]
check "T5 no-DB mint: silent exit 0" $?

# push-fail counter (spec 2a change 5): unreachable remote, 3 runs -> 1 alert
git -C "$SB" remote add origin /nonexistent/dgn672-remote.git
for i in 1 2 3; do
  "$SB/database/lifekit.sh" task-add "t5 filler $i" 2026-08-01 >/dev/null
  "$SB/routines/backup-data.sh" >/dev/null 2>&1
  [[ $? -eq 0 ]] || bad "T5 push-fail run $i still exits 0"
done
[[ "$(cat "$SB/.telegram_bot/backup-push-fail.count" 2>/dev/null)" == "3" ]]
check "T5 consecutive push-fail counter reached 3" $?
grep -q "failed 3 times" "$SB/.telegram_bot/push-calls.log" 2>/dev/null
check "T5 push-fail alert fired at 3 (bounded silence)" $?

# ===========================================================================
echo "== T6: version guards (REFUSE / --target-version / skew / --no-catchup) =="
SB="$(make_sandbox 1)"
"$SB/database/lifekit.sh" task-add "t6 task" 2026-08-01 >/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1
# strip the version line -> legacy pragma-less snapshot
grep -v '^PRAGMA user_version' "$SB/database/lifekit.sql" > "$SB/database/.strip" \
  && mv "$SB/database/.strip" "$SB/database/lifekit.sql"
git -C "$SB" add -- database/ && git -C "$SB" commit -qm "backup: legacy pragma-less" -- database/
quiesce "$SB/database/lifekit.db"
LIVE_MD5="$(file_md5 "$SB/database/lifekit.db")"
OUT="$("$SB/database/restore-data.sh" --to latest 2>&1)"
RC=$?
[[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "REFUSE"
check "T6 pragma-less dump REFUSEd (no 0-assumption)" $?
[[ "$(file_md5 "$SB/database/lifekit.db")" == "$LIVE_MD5" ]]
check "T6 live DB untouched after REFUSE" $?
"$SB/database/restore-data.sh" --to latest --target-version "$LK_PIN" >/dev/null 2>&1
check "T6 --target-version $LK_PIN proceeds" $?
[[ "$(db_ver "$SB/database/lifekit.db")" == "$LK_PIN" ]]
check "T6 asserted version landed" $?

# (b) real catch-up apply: snapshot at payload pin, instance moved to pin+1
SB="$(make_sandbox 1)"
sed -i '' -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P1/" \
  "$SB/database/lifekit.py" 2>/dev/null \
  || sed -i -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P1/" \
       "$SB/database/lifekit.py"
cat > "$SB/database/migrations/${LK_P1_PAD}_catchup_fixture.sql" <<EOF
BEGIN;
CREATE TABLE catchup_t (id INTEGER PRIMARY KEY);
PRAGMA user_version = $LK_P1;
COMMIT;
EOF
"$SB/routines/backup-data.sh" >/dev/null 2>&1        # snapshot carries the payload pin
"$SQLITE" "$SB/database/lifekit.db" < "$SB/database/migrations/${LK_P1_PAD}_catchup_fixture.sql"
"$SB/database/lifekit.sh" task-add "t6b post-snapshot task" 2026-08-01 >/dev/null
"$SB/database/restore-data.sh" --to latest >/dev/null 2>&1
check "T6 catch-up restore succeeds ($LK_PIN -> $LK_P1)" $?
[[ "$(db_ver "$SB/database/lifekit.db")" == "$LK_P1" ]]
check "T6 achieved version == EXPECTED after catch-up" $?
"$SQLITE" -readonly "$SB/database/lifekit.db" "SELECT 1 FROM sqlite_master WHERE name='catchup_t';" | grep -q 1
check "T6 catch-up migration actually applied" $?
[[ "$(task_rows "$SB/database/lifekit.db")" == "0" ]]
check "T6 restored to snapshot state (post-snapshot row gone)" $?

# (c) Ag-class skew lineage (file NNN != resulting version) -> STOP, no swap
SB="$(make_sandbox 1)"
sed -i '' -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P2/" \
  "$SB/database/lifekit.py" 2>/dev/null \
  || sed -i -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P2/" \
       "$SB/database/lifekit.py"
cat > "$SB/database/migrations/${LK_PIN_PAD}_skew_a.sql" <<EOF
BEGIN;
CREATE TABLE skew_a (id INTEGER PRIMARY KEY);
PRAGMA user_version = $LK_P1;
COMMIT;
EOF
cat > "$SB/database/migrations/${LK_P1_PAD}_skew_b.sql" <<EOF
BEGIN;
CREATE TABLE skew_b (id INTEGER PRIMARY KEY);
PRAGMA user_version = $LK_P2;
COMMIT;
EOF
# live lineage: skew_a applied (pin+1), snapshot taken at pin+1
"$SQLITE" "$SB/database/lifekit.db" < "$SB/database/migrations/${LK_PIN_PAD}_skew_a.sql"
"$SQLITE" "$SB/database/lifekit.db" < "$SB/database/migrations/${LK_P1_PAD}_skew_b.sql"
git -C "$SB" rm -q --cached . -r >/dev/null 2>&1; git -C "$SB" add -A; git -C "$SB" commit -qm wip >/dev/null 2>&1
# craft the mid-version candidate: dump of a pin+1 DB (skew: skew_b sets pin+2,
# NNN=pin+1 is not > cur=pin+1 -> silently skipped by an NNN-compare ->
# achieved gate STOP)
"$SQLITE" "$SB/database/lifekit.db" "PRAGMA user_version = $LK_P1; DROP TABLE skew_b;" 2>/dev/null
"$SB/routines/backup-data.sh" >/dev/null 2>&1        # snapshot carries pin+1
"$SQLITE" "$SB/database/lifekit.db" < "$SB/database/migrations/${LK_P1_PAD}_skew_b.sql"  # live back to pin+2
quiesce "$SB/database/lifekit.db"
LIVE_MD5="$(file_md5 "$SB/database/lifekit.db")"
OUT="$("$SB/database/restore-data.sh" --to latest 2>&1)"
RC=$?
[[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "STOP"
check "T6 skew lineage: achieved-version gate STOPs (safe-by-stop)" $?
[[ "$(file_md5 "$SB/database/lifekit.db")" == "$LIVE_MD5" ]]
check "T6 live DB untouched after skew STOP" $?

# (d) --no-catchup: candidate stays at its own (older) version, no gate
BAKF="$(ls -t "$SB/database"/lifekit.db.v${LK_P2}.bak-* 2>/dev/null | head -1)"
if [[ -z "$BAKF" ]]; then
  "$SQLITE" "$SB/database/lifekit.db" ".backup '$SB/database/lifekit.db.v${LK_P2}.bak-manual'"
  BAKF="$SB/database/lifekit.db.v${LK_P2}.bak-manual"
fi
# make an OLDER binary snapshot (pin+1) to prove no-catchup does not migrate it
"$SQLITE" "$SB/database/lifekit.db" "PRAGMA user_version = $LK_P1;"
"$SQLITE" "$SB/database/lifekit.db" ".backup '$SB/database/lifekit.db.v${LK_P1}.bak-fixture'"
"$SQLITE" "$SB/database/lifekit.db" "PRAGMA user_version = $LK_P2;"
"$SB/database/restore-data.sh" --to "$SB/database/lifekit.db.v${LK_P1}.bak-fixture" --no-catchup >/dev/null 2>&1
check "T6 --no-catchup restore succeeds (673 B2 path)" $?
[[ "$(db_ver "$SB/database/lifekit.db")" == "$LK_P1" ]]
check "T6 --no-catchup left version as-is (no migration ran)" $?

# (e) anomaly gap: migrations cannot reach EXPECTED -> STOP
SB="$(make_sandbox 1)"
sed -i '' -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P2/" \
  "$SB/database/lifekit.py" 2>/dev/null \
  || sed -i -e "s/^EXPECTED_USER_VERSION = $LK_PIN/EXPECTED_USER_VERSION = $LK_P2/" \
       "$SB/database/lifekit.py"
cat > "$SB/database/migrations/${LK_P1_PAD}_gap_only.sql" <<EOF
BEGIN;
CREATE TABLE gap_t (id INTEGER PRIMARY KEY);
PRAGMA user_version = $LK_P1;
COMMIT;
EOF
"$SB/routines/backup-data.sh" >/dev/null 2>&1        # snapshot carries the payload pin
quiesce "$SB/database/lifekit.db"
LIVE_MD5="$(file_md5 "$SB/database/lifekit.db")"
OUT="$("$SB/database/restore-data.sh" --to latest 2>&1)"
RC=$?
[[ "$RC" -eq 1 ]] && echo "$OUT" | grep -q "STOP"
check "T6 anomaly gap (pin+1 != EXPECTED pin+2): STOP" $?
[[ "$(file_md5 "$SB/database/lifekit.db")" == "$LIVE_MD5" ]]
check "T6 live DB untouched after gap STOP" $?

# ===========================================================================
echo "== T7: wiring guards (R1 Linux table + DEFAULT-LOADED plist + R3 line) =="
grep -E '^\s*printf .*"backup-data"\s+"routines/backup-data\.sh"' "$REPO_ROOT/install.sh" >/dev/null
check "T7 default_routine_set has backup-data row (Linux wiring, R1)" $?
PLIST="$REPO_ROOT/agents/.template/routines/com.telegram-skill-bot.telegram-agent.backup-data.plist"
[[ -f "$PLIST" ]]; check "T7 backup-data plist exists (macOS wiring)" $?
grep -q "backup-data" "$REPO_ROOT/agents/.template/routines/plists.defer" \
  && bad "T7 plist must NOT be in plists.defer (DEFAULT-LOADED)" \
  || ok "T7 plist not deferred (DEFAULT-LOADED class)"
grep -q '^database/lifekit\.sql\.\*' "$REPO_ROOT/agents/.template/.gitignore"
check "T7 .gitignore carries database/lifekit.sql.* (R3)" $?
grep -q "<integer>0</integer>" "$PLIST" && grep -q "<integer>30</integer>" "$PLIST"
check "T7 plist fires daily 00:30" $?

# ===========================================================================
echo
echo "== summary: $PASS passed, $FAIL failed =="
if [[ "$FAIL" -gt 0 ]]; then
  printf '  failed: %s\n' "${FAILED_NAMES[@]}"
  exit 1
fi
exit 0
