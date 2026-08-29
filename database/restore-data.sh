#!/bin/bash
# restore-data.sh -- version-safe lifekit.db restore (DGN-672 M3; contract C2).
#
# The ONLY sanctioned path that replaces lifekit.db wholesale. Wholesale binary
# overwrite is never a default recovery route in this estate; it is legitimate
# ONLY for data loss/corruption/pollution of the DB itself, and ONLY through
# the 5-fold guard implemented here:
#   safety copy -> candidate verify -> version resolution -> post-verify gate
#   -> automatic swap-back on failure.
# DGN-673 (rollback) consumes this tool (Path B2 = --to <bak> --no-catchup
# after a CODE rollback); it never hand-rolls a restore.
#
# Usage:
#   restore-data.sh --list
#   restore-data.sh --to <commit|YYYY-MM-DD|latest|bakfile> [--dry]
#                   [--target-version <N>] [--no-catchup]
#
# Flags (fixed set -- 673 contract):
#   --list             show restore points: git history of database/lifekit.sql
#                      + local binary snapshots (*.bak-*). No side effects.
#   --to <point>       restore target. commit hash | date (latest snapshot at
#                      or before that date) | "latest" | path to a .bak file.
#   --dry              run steps 2-5 only (materialize/verify/catch-up) and
#                      report; the live DB is never touched.
#   --target-version N operator assertion for a version-unknown dump (legacy
#                      snapshots without the PRAGMA user_version line). Never
#                      assumed to be 0: 0-assumption would re-apply
#                      non-idempotent migrations over a current schema.
#   --no-catchup       skip migration catch-up (and its achieved-version
#                      gate). Required by the DGN-673 schema-rollback path B2
#                      where the target schema is DELIBERATELY older.
#
# --to staged algorithm (spec section 4):
#   0 QUIESCE PREFLIGHT  disk free >= live DB x 2 (materialized candidate and
#                        safety copy coexist), else abort.
#   1 SAFETY COPY        WAL-safe `.backup` -> lifekit.db.v<ver>.bak-<ts>
#                        (naming contract C3). A corrupt live DB can fail
#                        `.backup`; then the raw bytes (+wal/+shm) are
#                        preserved instead (never swap without a copy). Live
#                        DB missing (disaster) = notify and continue.
#   2 MATERIALIZE        mktemp -d OUTSIDE database/ + trap cleanup.
#                        .sql -> sqlite3 -bail (default CLI behavior is
#                        continue-after-error; -bail stops a partial
#                        materialize from passing silently). .bak -> cp.
#   3 VERSION RESOLUTION .sql: candidate pragma (self-carrying dump, C4);
#                        pragma 0 + no pragma line in source = version-unknown
#                        -> REFUSE without --target-version. .bak: pragma is
#                        the truth; a v<N> filename stamp is cross-checked
#                        (mismatch = abort); unstamped legacy = pragma alone +
#                        WAL-unsafe warning (C6).
#   4 VERIFY (pre-swap)  lifekit.sh check --db candidate: integrity ok is the
#                        hard gate; version and table set are reported.
#   5 CATCH-UP (R5)      instance-local database/migrations/, ascending; per
#                        file: FRESH pragma read -> NNN > cur; after apply the
#                        version MUST have advanced; terminal gate: achieved
#                        version == EXPECTED_USER_VERSION parsed from the
#                        instance's lifekit.py. Any anomaly = STOP, no swap
#                        (safe-by-stop; lineage skew classes are caught
#                        mechanically by the achieved gate).
#   6 ATOMIC SWAP        under BEGIN EXCLUSIVE after wal_checkpoint(TRUNCATE);
#                        stale -wal/-shm removed while holding the lock; the
#                        candidate is staged inside database/ first (atomic
#                        rename requires same filesystem). Busy = ABORT,
#                        never forced. Writer topology precondition: all
#                        framework lifekit writers are per-invocation (4a).
#   7 POST-VERIFY        live check + substantive owner read (agg-day). On
#                        failure: automatic swap-back from the step-1 safety
#                        copy, exit 2. Re-entry after a crash between swap and
#                        post-verify is safe: rerun starts at check again.
#
# Exit codes: 0 = restored / dry ok
#             1 = precondition or tool error (incl. version-unknown REFUSE)
#             2 = post-verify failed (live DB rolled back to safety copy)
#
# Test seams (used by tests/dgn672_backup_restore_test.sh only):
#   DGN672_TEST_PAUSE_BEFORE_SWAP=<sec>  sleep before the swap (crash-window test)
#   DGN672_TEST_FAIL_POSTVERIFY=1        force the post-verify branch (rollback test)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$SCRIPT_DIR"
DB="$DATA_DIR/lifekit.db"
DUMP_RELPATH="database/lifekit.sql"
MIG_DIR="$DATA_DIR/migrations"
LIFE_SH="$DATA_DIR/lifekit.sh"
LIFEKIT_PY="$DATA_DIR/lifekit.py"
SQLITE="$(command -v sqlite3 || echo /usr/bin/sqlite3)"

die()  { echo "[restore] ERROR: $*" >&2; exit 1; }
note() { echo "[restore] $*"; }

# Read user_version from a DB file without mutating it. A WAL-mode file
# without its -shm/-wal sidecars (every `.backup` product) refuses a plain
# read-only open (SQLITE_CANTOPEN); immutable=1 is the sanctioned read path
# for such standalone snapshot files.
db_version() {
  local v
  if v="$("$SQLITE" -readonly "$1" 'PRAGMA user_version;' 2>/dev/null)"; then
    echo "$v"; return 0
  fi
  "$SQLITE" "file:$1?immutable=1" 'PRAGMA user_version;' 2>/dev/null
}

# Portable mtime/size (macOS stat -f vs GNU stat -c).
file_meta() {
  local f="$1"
  if stat -f '%Sm %z' -t '%Y-%m-%d %H:%M' "$f" 2>/dev/null; then return 0; fi
  stat -c '%y %s' "$f" 2>/dev/null | cut -c1-16,20- || echo "? ?"
}

# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------
LIST=0; TO=""; DRY=0; TARGET_VERSION=""; NO_CATCHUP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)            LIST=1; shift ;;
    --to)              TO="${2:?--to needs a value}"; shift 2 ;;
    --dry)             DRY=1; shift ;;
    --target-version)  TARGET_VERSION="${2:?--target-version needs a value}"; shift 2 ;;
    --no-catchup)      NO_CATCHUP=1; shift ;;
    -h|--help)         sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$TARGET_VERSION" && ! "$TARGET_VERSION" =~ ^[0-9]+$ ]] \
  && die "--target-version must be a non-negative integer"

command -v "$SQLITE" >/dev/null 2>&1 || die "sqlite3 not found"
[[ -x "$LIFE_SH" ]] || die "lifekit.sh not found ($LIFE_SH)"

# ---------------------------------------------------------------------------
# --list (no side effects)
# ---------------------------------------------------------------------------
if [[ "$LIST" -eq 1 ]]; then
  echo "== git snapshots ($DUMP_RELPATH) =="
  if git -C "$AGENT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    local_shown=0
    while IFS= read -r line; do
      h="${line%% *}"; rest="${line#* }"
      n="$(git -C "$AGENT_DIR" show "$h:$DUMP_RELPATH" 2>/dev/null | wc -l | tr -d ' ')"
      printf '  %s  %s  (%s lines)\n' "$h" "$rest" "$n"
      local_shown=$((local_shown + 1))
    done < <(git -C "$AGENT_DIR" log -30 --format='%h %ad %s' --date=short -- "$DUMP_RELPATH" 2>/dev/null)
    [[ "$local_shown" -eq 0 ]] && echo "  (none)"
  else
    echo "  (not a git repo)"
  fi
  echo "== local binary snapshots (database/*.bak-*) =="
  found=0
  for f in "$DATA_DIR"/*.bak-*; do
    [[ -f "$f" ]] || continue
    case "$f" in *.bak-*-wal|*.bak-*-shm) continue ;; esac
    found=1
    v="$(db_version "$f" || true)"; [[ -n "$v" ]] || v="unreadable"
    meta="$(file_meta "$f")"
    tag=""
    [[ "$(basename "$f")" =~ \.v[0-9]+\.bak- ]] || tag="  [legacy cp-era: WAL-unsafe]"
    printf '  %s  version=%s  %s%s\n' "$(basename "$f")" "$v" "$meta" "$tag"
  done
  [[ "$found" -eq 0 ]] && echo "  (none)"
  exit 0
fi

[[ -n "$TO" ]] || die "nothing to do: use --list or --to <point>"

# ---------------------------------------------------------------------------
# resolve --to into MODE=sql(COMMIT) | MODE=bak(BAK_SRC)
# ---------------------------------------------------------------------------
MODE=""; COMMIT=""; BAK_SRC=""
if [[ -f "$TO" ]]; then
  MODE="bak"; BAK_SRC="$TO"
elif [[ "$TO" == "latest" ]]; then
  COMMIT="$(git -C "$AGENT_DIR" log -1 --format=%H -- "$DUMP_RELPATH" 2>/dev/null || true)"
  [[ -n "$COMMIT" ]] || die "no snapshot history for $DUMP_RELPATH"
  MODE="sql"
elif [[ "$TO" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  COMMIT="$(git -C "$AGENT_DIR" log -1 --format=%H --until="$TO 23:59:59" -- "$DUMP_RELPATH" 2>/dev/null || true)"
  [[ -n "$COMMIT" ]] || die "no snapshot at or before $TO"
  MODE="sql"
else
  COMMIT="$(git -C "$AGENT_DIR" rev-parse --verify --quiet "$TO^{commit}" || true)"
  [[ -n "$COMMIT" ]] || die "cannot resolve restore point: $TO (not a file, date, or commit)"
  git -C "$AGENT_DIR" cat-file -e "$COMMIT:$DUMP_RELPATH" 2>/dev/null \
    || die "commit $TO does not contain $DUMP_RELPATH"
  MODE="sql"
fi
if [[ "$MODE" == "sql" ]]; then
  POINT_DESC="commit ${COMMIT:0:10} ($(git -C "$AGENT_DIR" log -1 --format=%ad --date=short "$COMMIT"))"
else
  POINT_DESC="binary snapshot $(basename "$BAK_SRC")"
fi
note "restore point: $POINT_DESC"

TS="$(date +%Y%m%d-%H%M%S)"

# ---------------------------------------------------------------------------
# step 0: quiesce preflight (skipped in --dry: live DB is not part of a dry run)
# ---------------------------------------------------------------------------
if [[ "$DRY" -eq 0 && -f "$DB" ]]; then
  db_bytes="$(wc -c < "$DB" | tr -d ' ')"
  avail_bytes="$(df -Pk "$DATA_DIR" | awk 'NR==2 {print $4 * 1024}')"
  [[ "$avail_bytes" -ge $((db_bytes * 2)) ]] \
    || die "insufficient disk space: need $((db_bytes * 2)) bytes free (candidate + safety copy), have $avail_bytes"
fi

# ---------------------------------------------------------------------------
# step 1: safety copy (never swap without one; skipped in --dry)
# ---------------------------------------------------------------------------
SAFETY=""
if [[ "$DRY" -eq 0 ]]; then
  if [[ -f "$DB" ]]; then
    cur_ver="$(db_version "$DB" || true)"
    if [[ -n "$cur_ver" ]] \
       && "$SQLITE" "$DB" ".backup '$DATA_DIR/lifekit.db.v${cur_ver}.bak-$TS'" 2>/dev/null; then
      SAFETY="$DATA_DIR/lifekit.db.v${cur_ver}.bak-$TS"
      note "safety copy: $(basename "$SAFETY") (WAL-safe .backup)"
    else
      # A corrupt live DB can refuse `.backup`. The never-clobber rule still
      # holds: preserve the raw bytes (incl. -wal/-shm) for forensics/retry.
      SAFETY="$DATA_DIR/lifekit.db.corrupt.bak-$TS"
      cp -p "$DB" "$SAFETY" \
        || die "safety copy failed -- refusing to touch the live DB without a copy"
      [[ -f "$DB-wal" ]] && cp -p "$DB-wal" "$SAFETY-wal" 2>/dev/null || true
      [[ -f "$DB-shm" ]] && cp -p "$DB-shm" "$SAFETY-shm" 2>/dev/null || true
      note "WARNING: live DB unreadable by .backup -- raw bytes preserved as $(basename "$SAFETY")"
    fi
  else
    note "live DB missing (disaster path) -- no safety copy possible, continuing"
  fi
fi

# ---------------------------------------------------------------------------
# step 2: materialize candidate OUTSIDE database/ (trap-cleaned)
# ---------------------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lifekit-restore.XXXXXX")"
STAGED=""
cleanup() { rm -rf "$WORK"; [[ -n "$STAGED" ]] && rm -f "$STAGED"; return 0; }
trap cleanup EXIT
# Route signal deaths through the EXIT trap (tmp workdir + staged candidate
# must not survive a TERM'd run; kill -9 residue is .gitignore-covered).
trap 'exit 143' INT TERM HUP
TMPDB="$WORK/candidate.db"

if [[ "$MODE" == "sql" ]]; then
  git -C "$AGENT_DIR" show "$COMMIT:$DUMP_RELPATH" > "$WORK/candidate.sql" \
    || die "git show failed for $COMMIT:$DUMP_RELPATH"
  # -bail is mandatory: sqlite3 CLI default is continue-after-error, which
  # would let a partially-materialized candidate pass silently.
  "$SQLITE" -bail "$TMPDB" < "$WORK/candidate.sql" \
    || die "materialize failed (candidate discarded; live DB untouched)"
else
  cp "$BAK_SRC" "$TMPDB" || die "cannot copy $BAK_SRC"
fi

# ---------------------------------------------------------------------------
# step 3: version resolution (B1; contracts C3/C4/C6)
# ---------------------------------------------------------------------------
cand_ver="$(db_version "$TMPDB")" || die "cannot read candidate user_version"
if [[ "$MODE" == "sql" ]]; then
  if [[ "$cand_ver" -eq 0 ]] && ! grep -q '^PRAGMA user_version' "$WORK/candidate.sql"; then
    if [[ -n "$TARGET_VERSION" ]]; then
      "$SQLITE" "$TMPDB" "PRAGMA user_version = $TARGET_VERSION;"
      cand_ver="$TARGET_VERSION"
      note "version-unknown dump: operator asserted --target-version $TARGET_VERSION"
    else
      echo "[restore] REFUSE: version-unknown dump (no PRAGMA user_version line)." >&2
      echo "[restore] Never assumed 0: migration catch-up from 0 would re-apply" >&2
      echo "[restore] non-idempotent DDL over a current schema (destructive)." >&2
      echo "[restore] If the operator KNOWS the schema version of this snapshot," >&2
      echo "[restore] rerun with --target-version <N>." >&2
      exit 1
    fi
  elif [[ -n "$TARGET_VERSION" && "$TARGET_VERSION" -ne "$cand_ver" ]]; then
    die "--target-version $TARGET_VERSION conflicts with dump-carried version $cand_ver"
  fi
else
  base="$(basename "$BAK_SRC")"
  if [[ "$base" =~ \.v([0-9]+)\.bak- ]]; then
    stamp="${BASH_REMATCH[1]}"
    # The version truth is the pragma INSIDE the file; the filename stamp is
    # a cross-check only (contract C3: "read the version by opening it").
    [[ "$stamp" -eq "$cand_ver" ]] \
      || die "filename stamp v$stamp != pragma version $cand_ver -- snapshot mislabeled, aborting"
  else
    note "WARNING: legacy unstamped .bak (cp-era, WAL-unsafe): its loss window may exceed its mtime (C6)"
  fi
fi
note "candidate schema version: $cand_ver"

# ---------------------------------------------------------------------------
# step 4: verify candidate BEFORE any catch-up (integrity = hard gate)
# ---------------------------------------------------------------------------
CHECK_OUT="$("$LIFE_SH" check --db "$TMPDB")" \
  || die "candidate failed integrity check -- refusing (live DB untouched)"
note "candidate integrity: ok"
# Missing tables are informational here: a pre-catch-up candidate at an older
# schema version legitimately lacks newer tables.
missing_pre="$(printf '%s\n' "$CHECK_OUT" | grep -c '^missing=' || true)"
[[ "$missing_pre" -gt 0 ]] && note "candidate is missing $missing_pre table(s) vs schema.sql (pre catch-up)"

# ---------------------------------------------------------------------------
# step 5: migration catch-up on the candidate only (R5-hardened loop)
# ---------------------------------------------------------------------------
if [[ "$NO_CATCHUP" -eq 0 ]]; then
  EXPECTED="$(grep -E '^EXPECTED_USER_VERSION' "$LIFEKIT_PY" | head -1 \
              | sed -E 's/[^0-9]*([0-9]+).*/\1/')"
  [[ "$EXPECTED" =~ ^[0-9]+$ ]] \
    || die "cannot parse EXPECTED_USER_VERSION from $LIFEKIT_PY"
  applied=0
  if [[ -d "$MIG_DIR" ]]; then
    # INSTANCE-local migrations dir is the reference (G7 anomaly guard: never
    # assume the canonical dir).
    for mig in "$MIG_DIR"/[0-9][0-9][0-9]_*.sql; do
      [[ -e "$mig" ]] || continue
      mbase="$(basename "$mig")"
      nnn="${mbase%%_*}"
      n=$((10#$nnn))
      # (a) FRESH pragma read per file -- the previous migration may have
      # advanced the version; reusing a loop-start read is forbidden (R5).
      cur="$(db_version "$TMPDB")" || die "candidate version unreadable mid catch-up"
      [[ "$n" -gt "$cur" ]] || continue
      "$SQLITE" -bail "$TMPDB" < "$mig" \
        || die "catch-up migration $mbase failed on the candidate (live DB untouched)"
      # (b) the migration file itself must advance user_version (lineage
      # contract); a non-advancing file means the lineage is broken -> STOP.
      newv="$(db_version "$TMPDB")" || die "candidate version unreadable after $mbase"
      [[ "$newv" -gt "$cur" ]] \
        || die "migration $mbase did not advance user_version ($cur -> $newv) -- lineage contract broken, no swap"
      note "catch-up: applied $mbase ($cur -> $newv)"
      applied=$((applied + 1))
    done
  fi
  # (c) terminal gate: achieved version must equal the INSTANCE expectation.
  # This mechanically catches every NNN-vs-resulting-version skew class
  # (e.g. Ag lineage 011->v12 .. 014->v15) -- safe-by-stop, zero destruction.
  final_ver="$(db_version "$TMPDB")"
  if [[ "$final_ver" -ne "$EXPECTED" ]]; then
    echo "[restore] STOP: catch-up achieved user_version=$final_ver but this" >&2
    echo "[restore] instance expects EXPECTED_USER_VERSION=$EXPECTED (lifekit.py)." >&2
    echo "[restore] Migration lineage anomaly (version skew / gap). No swap was" >&2
    echo "[restore] performed; the live DB is untouched. Try another snapshot" >&2
    echo "[restore] or resolve the lineage (DGN-672 section 9 / G7)." >&2
    exit 1
  fi
  note "catch-up complete: $applied migration(s), user_version=$final_ver == EXPECTED ($EXPECTED)"
  # Post-catch-up the candidate must carry the full expected table set.
  missing_post="$("$LIFE_SH" check --db "$TMPDB" | grep '^missing=' || true)"
  [[ -z "$missing_post" ]] \
    || die "candidate is missing tables after catch-up ($(echo "$missing_post" | tr '\n' ' ')) -- lineage anomaly, no swap"
else
  note "catch-up SKIPPED (--no-catchup; DGN-673 B2 schema-rollback path)"
fi

if [[ "$DRY" -eq 1 ]]; then
  note "--dry: candidate OK (version $(db_version "$TMPDB")); live DB untouched"
  exit 0
fi

# Pre-swap row snapshot for the summary (from the live DB if readable).
PRE_TABLES="$("$LIFE_SH" check 2>/dev/null | grep '^table=' || true)"

# ---------------------------------------------------------------------------
# step 6: atomic swap under exclusive lock (4a contract)
# ---------------------------------------------------------------------------
# Stage the candidate INSIDE database/ first: atomic rename needs same fs.
# The staged name ends in .db so the instance .gitignore covers it.
STAGED="$DATA_DIR/tmp-restore-$$.db"
cp "$TMPDB" "$STAGED" || die "cannot stage candidate into $DATA_DIR"

# Test seam: widen the materialize->swap crash window (T3).
[[ -n "${DGN672_TEST_PAUSE_BEFORE_SWAP:-}" ]] && sleep "$DGN672_TEST_PAUSE_BEFORE_SWAP"

swap_in() {
  # $1 = staged file to become the live DB. Runs the 4a swap contract.
  python3 - "$DB" "$1" <<'PY'
import os
import sqlite3
import sys

db, staged = sys.argv[1], sys.argv[2]
conn = None
if os.path.exists(db):
    try:
        conn = sqlite3.connect(db, timeout=5)
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = 5000;")
        # Checkpoint TRUNCATE first: the WAL is folded into the main file and
        # emptied, so a crash at ANY later point leaves the old DB complete
        # as a main file alone, and deleting -wal/-shm below is safe.
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        if row and int(row[0]) == 1:
            print("swap: wal checkpoint blocked (busy) -- abort", file=sys.stderr)
            sys.exit(3)
        # Exclusive lock: an active writer means ABORT, never force
        # (busy_timeout gives per-invocation writers 5s to drain).
        conn.execute("BEGIN EXCLUSIVE;")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            print("swap: exclusive lock unavailable (%s) -- abort" % e,
                  file=sys.stderr)
            sys.exit(3)
        # Non-lock operational error on a broken file: fall through --
        # a corrupt live DB accepts no lock and has no writers to fence.
    except sqlite3.DatabaseError:
        # Corrupt live DB: no meaningful lock possible; writers fail on it
        # too. The safety copy already preserved its bytes.
        pass
# Stale -wal/-shm MUST go (an old WAL next to the new file would corrupt
# it), and MUST go only after the checkpoint above.
for suffix in ("-wal", "-shm"):
    sidecar = db + suffix
    if os.path.exists(sidecar):
        os.remove(sidecar)
os.replace(staged, db)  # atomic on same fs
if conn is not None:
    try:
        conn.close()  # old inode released; new connections see the new file
    except Exception:
        pass
sys.exit(0)
PY
}

set +e
swap_in "$STAGED"
swap_rc=$?
set -e
if [[ "$swap_rc" -eq 3 ]]; then
  die "active writer during swap window -- aborted (rerun when quiet; long-lived writers require the downtime procedure)"
elif [[ "$swap_rc" -ne 0 ]]; then
  die "swap failed (rc=$swap_rc); live DB untouched or safety copy available"
fi
STAGED=""  # consumed by os.replace
note "swap complete"

# ---------------------------------------------------------------------------
# step 7: post-verify; automatic swap-back on failure
# ---------------------------------------------------------------------------
post_ok=1
"$LIFE_SH" check >/dev/null 2>&1 || post_ok=0
"$LIFE_SH" agg-day "$(date +%F)" >/dev/null 2>&1 || post_ok=0
[[ -n "${DGN672_TEST_FAIL_POSTVERIFY:-}" ]] && post_ok=0

if [[ "$post_ok" -eq 0 ]]; then
  echo "[restore] post-verify FAILED on the restored DB" >&2
  if [[ -n "$SAFETY" && -f "$SAFETY" ]]; then
    BACK="$DATA_DIR/tmp-restore-back-$$.db"
    cp "$SAFETY" "$BACK" || { echo "[restore] CRITICAL: cannot stage swap-back; safety copy at $SAFETY" >&2; exit 2; }
    set +e
    swap_in "$BACK"
    back_rc=$?
    set -e
    rm -f "$BACK"
    if [[ "$back_rc" -eq 0 ]]; then
      echo "[restore] rolled back: live DB restored from $(basename "$SAFETY")" >&2
    else
      echo "[restore] CRITICAL: swap-back failed (rc=$back_rc); safety copy preserved at $SAFETY" >&2
    fi
  else
    echo "[restore] no safety copy exists (disaster path) -- nothing to roll back to" >&2
  fi
  exit 2
fi

# Success summary: restored point + row-count deltas for core tables.
POST_TABLES="$("$LIFE_SH" check | grep '^table=' || true)"
note "restored to $POINT_DESC"
if [[ -n "$PRE_TABLES" ]]; then
  while IFS= read -r line; do
    tbl="${line#table=}"; tbl="${tbl%% *}"
    new="${line##*rows=}"
    old="$(printf '%s\n' "$PRE_TABLES" | grep "^table=$tbl " | sed 's/.*rows=//' || true)"
    [[ -n "$old" && "$old" != "$new" ]] && note "  rows $tbl: $old -> $new"
  done <<< "$POST_TABLES"
fi
[[ -n "$SAFETY" ]] && note "pre-restore safety copy: $(basename "$SAFETY")"
exit 0
