#!/bin/bash
# dgn673_migration_marker_lint.sh -- DGN-673 B4 reversibility-marker lint.
#
# Every lifekit migration (database/migrations/NNN_*.sql) MUST declare its
# reversibility in its FIRST TWO LINES:
#     -- reversible: yes|no
#     -- down: <NNN_description.down.sql | none>
# Rationale (DGN-673 spec 3a/3b): the rollback DB gate is mechanical -- any
# 'reversible: no' migration inside a rollback delta with no verified snapshot
# is a HARD REFUSE (failure S1). The marker keeps that gate greppable; this
# lint keeps the marker mandatory for every FUTURE migration too.
#
# Rules enforced:
#   L1  line 1 matches exactly '-- reversible: yes' or '-- reversible: no'
#   L2  line 2 matches '-- down: none' or '-- down: NNN_<name>.down.sql'
#   L3  'reversible: no' requires 'down: none' (irreversible = no down file
#       by definition; a down file on an irreversible migration is a lie)
#   L4  a named down file must exist next to the migration and carry the
#       SAME NNN prefix as the migration it reverses
#
# The lint runs against the REAL migrations dir (must be clean) and against
# crafted bad fixtures in a temp dir (must be flagged) -- both directions
# asserted, dgn593/dgn621 selftest style.
#
# SAFETY: read-only against the repo; fixtures live in a private mktemp dir.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIG_DIR="$SANDBOX/database/migrations"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn673-lint.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL: $*"; }

# ---------------------------------------------------------------------------
# lint_migrations_dir <dir>
#   Prints one VIOLATION line per broken rule; returns 0 clean / 1 dirty.
#   *.down.sql files are companions, not migrations -> skipped as lint
#   subjects (they are only checked for existence via rule L4).
# ---------------------------------------------------------------------------
lint_migrations_dir() {
  local dir="$1" rc=0 f base l1 l2 down nnn dnnn
  for f in "$dir"/[0-9][0-9][0-9]_*.sql; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    case "$base" in *.down.sql) continue ;; esac
    l1="$(sed -n '1p' "$f")"
    l2="$(sed -n '2p' "$f")"
    if ! printf '%s\n' "$l1" | grep -Eq '^-- reversible: (yes|no)$'; then
      printf 'VIOLATION %s: line 1 must be "-- reversible: yes|no" (got: %s)\n' "$base" "${l1:-<empty>}"
      rc=1
      continue
    fi
    if ! printf '%s\n' "$l2" | grep -Eq '^-- down: (none|[0-9]{3}_[A-Za-z0-9_-]+\.down\.sql)$'; then
      printf 'VIOLATION %s: line 2 must be "-- down: none|NNN_<name>.down.sql" (got: %s)\n' "$base" "${l2:-<empty>}"
      rc=1
      continue
    fi
    down="${l2#-- down: }"
    if [ "$l1" = "-- reversible: no" ] && [ "$down" != "none" ]; then
      printf 'VIOLATION %s: "reversible: no" requires "down: none"\n' "$base"
      rc=1
      continue
    fi
    if [ "$down" != "none" ]; then
      nnn="${base%%_*}"
      dnnn="${down%%_*}"
      if [ "$nnn" != "$dnnn" ]; then
        printf 'VIOLATION %s: down file NNN mismatch (%s vs %s)\n' "$base" "$dnnn" "$nnn"
        rc=1
        continue
      fi
      if [ ! -f "$dir/$down" ]; then
        printf 'VIOLATION %s: declared down file missing: %s\n' "$base" "$down"
        rc=1
      fi
    fi
  done
  return $rc
}

# ---------------------------------------------------------------------------
# 1+2) Real migrations dir legs.
# DGN-803 LS-5: lifekit migrations left the canonical tree (independent
# lifekit pack owns the lineage; the reversible-marker forcing point lives in
# compat-lint C3, enforced publish-side + install-side). When the dir is
# absent this is the NORMAL post-split state -> the real-dir legs SKIP and
# only the fixture self-test below gates. The legs are kept (not deleted) so
# the lint still bites if migrations ever reappear in-tree.
# ---------------------------------------------------------------------------
if [ -d "$MIG_DIR" ]; then
  say "[1] real migrations dir: $MIG_DIR"
  OUT="$(lint_migrations_dir "$MIG_DIR")"; RC=$?
  if [ "$RC" = "0" ] && [ -z "$OUT" ]; then
    ok "real migrations dir is marker-clean"
  else
    bad "real migrations dir has violations:"; printf '%s\n' "$OUT"
  fi

  # Spot-check the locked classification: 010 is the only irreversible one.
  say "[2] classification spot checks (spec 3a)"
  if [ "$(sed -n '1p' "$MIG_DIR/010_unified_event.sql")" = "-- reversible: no" ]; then
    ok "010_unified_event stamped reversible: no"
  else
    bad "010_unified_event must be stamped reversible: no"
  fi
  NO_COUNT="$(grep -l '^-- reversible: no$' "$MIG_DIR"/[0-9][0-9][0-9]_*.sql | wc -l | tr -d ' ')"
  if [ "$NO_COUNT" = "1" ]; then
    ok "exactly one irreversible migration (010) in 002-011"
  else
    bad "expected exactly 1 'reversible: no' migration, found $NO_COUNT"
  fi
else
  say "[1/2] real migrations dir absent (lifekit extracted, DGN-803 LS-5) -- SKIP; fixture self-test still gates"
fi

# ---------------------------------------------------------------------------
# 3) Bad fixtures must each be flagged.
#    Each fixture gets its own dir so one violation cannot mask another.
# ---------------------------------------------------------------------------
say "[3] bad fixtures must fail"

mkfix() { # $1 fixture-name, stdin = file content written as 099_fix.sql
  local d="$WORK/$1"
  mkdir -p "$d"
  cat > "$d/099_fix.sql"
  printf '%s\n' "$d"
}

expect_dirty() { # $1 desc, $2 dir
  local out rc
  out="$(lint_migrations_dir "$2")"; rc=$?
  if [ "$rc" = "1" ] && [ -n "$out" ]; then
    ok "$1"
  else
    bad "$1 (lint passed a broken fixture)"
  fi
}

expect_clean() { # $1 desc, $2 dir
  local out rc
  out="$(lint_migrations_dir "$2")"; rc=$?
  if [ "$rc" = "0" ] && [ -z "$out" ]; then
    ok "$1"
  else
    bad "$1 (lint flagged a good fixture: $out)"
  fi
}

D="$(mkfix no_marker <<'EOF'
-- just a description comment, no marker
ALTER TABLE t ADD COLUMN c TEXT;
PRAGMA user_version = 99;
EOF
)"
expect_dirty "missing marker entirely" "$D"

D="$(mkfix marker_not_first <<'EOF'
-- description first
-- reversible: yes
-- down: none
PRAGMA user_version = 99;
EOF
)"
expect_dirty "marker present but not on lines 1-2" "$D"

D="$(mkfix bad_value <<'EOF'
-- reversible: maybe
-- down: none
PRAGMA user_version = 99;
EOF
)"
expect_dirty "invalid reversible value" "$D"

D="$(mkfix bad_down_format <<'EOF'
-- reversible: yes
-- down: undo.sql
PRAGMA user_version = 99;
EOF
)"
expect_dirty "down file not NNN_<name>.down.sql form" "$D"

D="$(mkfix no_with_down <<'EOF'
-- reversible: no
-- down: 099_fix.down.sql
PRAGMA user_version = 99;
EOF
)"
touch "$D/099_fix.down.sql"
expect_dirty "reversible: no with a named down file" "$D"

D="$(mkfix down_missing <<'EOF'
-- reversible: yes
-- down: 099_fix.down.sql
PRAGMA user_version = 99;
EOF
)"
expect_dirty "declared down file does not exist" "$D"

D="$(mkfix down_nnn_mismatch <<'EOF'
-- reversible: yes
-- down: 098_fix.down.sql
PRAGMA user_version = 99;
EOF
)"
touch "$D/098_fix.down.sql"
expect_dirty "down file NNN differs from migration NNN" "$D"

# ---------------------------------------------------------------------------
# 4) Good fixtures must pass (future-migration shapes).
# ---------------------------------------------------------------------------
say "[4] good fixtures must pass"

D="$(mkfix good_none <<'EOF'
-- reversible: yes
-- down: none
ALTER TABLE t ADD COLUMN c TEXT;
PRAGMA user_version = 99;
EOF
)"
expect_clean "reversible: yes / down: none" "$D"

D="$(mkfix good_down <<'EOF'
-- reversible: yes
-- down: 099_fix.down.sql
PRAGMA user_version = 99;
EOF
)"
touch "$D/099_fix.down.sql"
expect_clean "reversible: yes with matching down file present" "$D"

D="$(mkfix good_no <<'EOF'
-- reversible: no
-- down: none
DROP TABLE t;
PRAGMA user_version = 99;
EOF
)"
expect_clean "reversible: no / down: none (010 class)" "$D"

# ---------------------------------------------------------------------------
say ""
say "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
