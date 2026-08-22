#!/bin/bash
# test_dgn656_code_only.sh -- DGN-656 self-test harness (--code-only landing)
#                             + DGN-663 (.claude/agents/ manifest sync).
#
# Covers the DGN-656 locked design (Option A: update.sh --code-only skips the
# section 3f-migrate DB schema step so divergent-lineage instances take
# framework code fixes without canonical migrations being force-applied):
#   T1 baseline lockstep      default run still applies pending migrations
#   T2 code-only skip         --code-only lands code, DB schema untouched
#   T3 forward-pin guard      version-pinned lifekit.py held back when its
#                             pin outruns the frozen DB (misuse containment)
#   T4 coherent pin lands     --code-only still lands lifekit.py when the
#                             pin does NOT outrun the DB
#   T5 dry-run purity         --dry-run --code-only writes nothing, reports
#                             the would-skip lines
#
# And the DGN-663 manifest addition (.claude/agents/ is a framework service
# update.sh syncs, while .dogany-preserve protects a locally-modified def):
#   T6 agents synced          framework .claude/agents/*.md land into an
#                             existing instance on a default update
#   T7 preserve supremacy     a .dogany-preserve-listed agent def is NOT
#                             clobbered; unlisted defs still refresh; the
#                             preserved entry does NOT trip the "matched no
#                             section" false-warn
#
# SAFETY: every scenario runs against throwaway sandbox repos + instances
# under a private mktemp WORK dir. No network, no git remotes, no launchd.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn656-test.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"   # physical path (macOS /var -> /private/var)
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }
assert_eq() { # assert_eq <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2] got [$3])"; fi
}
assert_grep() { # assert_grep <desc> <pattern> <file>
  if grep -q "$2" "$3" 2>/dev/null; then ok "$1"; else bad "$1 (pattern [$2] absent in $3)"; fi
}
assert_nogrep() { # assert_nogrep <desc> <pattern> <file>
  if grep -q "$2" "$3" 2>/dev/null; then bad "$1 (pattern [$2] PRESENT in $3)"; else ok "$1"; fi
}
file_sha() { shasum < "$1" 2>/dev/null | awk '{print $1}'; }
db_ver() { sqlite3 "$1" 'PRAGMA user_version;' 2>/dev/null; }
# DGN-672 C3 stamped backup filename: lifekit.db.v<ver>.bak-<ts>.
bak_count() { find "$1" -maxdepth 1 -name 'lifekit.db.v*.bak-*' 2>/dev/null | wc -l | tr -d ' '; }

# ---------------------------------------------------------------------------
# sandbox builders
# ---------------------------------------------------------------------------
# build_repo REPO_DIR FW_LIFEKIT_PIN
#   Minimal framework tree: real update.sh under test + fixture template,
#   database/ (lifekit.py at the given pin, schema.sql, migrations/011),
#   .claude/agents/*.md framework defs (DGN-663).
build_repo() {
  local r="$1" pin="$2"
  mkdir -p "$r/agents/.template/routines" "$r/agents/.template/bridge" \
           "$r/agents/.template/.claude/agents" \
           "$r/database/migrations"
  cp "$SANDBOX/update.sh" "$r/update.sh"
  chmod +x "$r/update.sh"
  printf '9.9.9-test\n' > "$r/VERSION"
  printf '# RULES (test fixture)\n' > "$r/agents/.template/RULES.md"
  printf '# AGENT-OPS (test fixture)\nroot: __PROJECT_ROOT__\n' \
    > "$r/agents/.template/AGENT-OPS.md"
  printf '#!/bin/sh\n# routine fixture vendor-v2\nexit 0\n' \
    > "$r/agents/.template/routines/marker.sh"
  chmod +x "$r/agents/.template/routines/marker.sh"
  printf '#!/bin/sh\n# vendor v2 ROOT=__PROJECT_ROOT__ AGENT=__AGENT_NAME__\nexit 0\n' \
    > "$r/agents/.template/bridge/self_restart.sh"
  chmod +x "$r/agents/.template/bridge/self_restart.sh"
  # Framework agent definitions (DGN-663). One carries __PROJECT_ROOT__ to
  # exercise the in-loop re-substitution.
  printf -- '---\nname: baseline-editor\n---\nFRAMEWORK agent def vendor-v2\n' \
    > "$r/agents/.template/.claude/agents/baseline-editor.md"
  printf -- '---\nname: release-closer\n---\nvendor-v2 ROOT=__PROJECT_ROOT__\n' \
    > "$r/agents/.template/.claude/agents/release-closer.md"
  # Framework database fixtures.
  printf 'EXPECTED_USER_VERSION = %s  # fixture\nLIFEKIT_FIXTURE = "vendor-v2"\n' "$pin" \
    > "$r/database/lifekit.py"
  printf -- '-- schema fixture vendor-v2\nCREATE TABLE event (id INTEGER);\n' \
    > "$r/database/schema.sql"
  printf 'ALTER TABLE event ADD COLUMN income_kind TEXT;\nPRAGMA user_version = 11;\n' \
    > "$r/database/migrations/011_income_kind.sql"
}

# build_instance ROOT SLUG REPO_ROOT INST_LIFEKIT_PIN DB_USER_VERSION
#   Minted instance: .instance.conf + prior-install database state (instance
#   lifekit.py at its own pin, lifekit.db stamped at the given user_version).
build_instance() {
  local root="$1" slug="$2" repo="$3" ipin="$4" dbv="$5"
  mkdir -p "$root/bridge" "$root/config" "$root/routines" "$root/.claude" \
           "$root/database"
  printf 'AGENT_LANG=en\n' > "$root/config/agent.conf"
  {
    echo "DOGANY_AGENT_NAME=$slug"
    echo "DOGANY_AGENT_LABEL=$slug"
    echo "DOGANY_USER_LABEL=you"
    echo "DOGANY_AGENT_PREFIX=[agent]"
    echo "DOGANY_REPO_ROOT=$repo"
  } > "$root/.instance.conf"
  printf 'EXPECTED_USER_VERSION = %s  # fixture\nLIFEKIT_FIXTURE = "instance-v1"\n' "$ipin" \
    > "$root/database/lifekit.py"
  printf -- '-- schema fixture instance-v1\n' > "$root/database/schema.sql"
  sqlite3 "$root/database/lifekit.db" \
    "CREATE TABLE event (id INTEGER); PRAGMA user_version = ${dbv};"
}

run_update() { # run_update REPO INST LOG [extra args...]
  local repo="$1" inst="$2" log="$3"
  shift 3
  DOGANY_LANG=en bash "$repo/update.sh" --root "$inst" --no-pull --yes "$@" \
    > "$log" 2>&1
}

# ===========================================================================
CURRENT="T1"
say "T1: baseline lockstep (default run still applies pending migrations)"
w="$WORK/t1"; mkdir -p "$w"
build_repo "$w/repo" 11
build_instance "$w/inst" t1agent "$w/repo" 10 10
run_update "$w/repo" "$w/inst" "$w/t1.log"
assert_eq "update exits 0" "0" "$?"
assert_eq "migration applied: DB user_version 10 -> 11" "11" "$(db_ver "$w/inst/database/lifekit.db")"
assert_grep "apply line logged" "applied migration 011" "$w/t1.log"
assert_eq "pre-apply DB backup taken" "1" "$(bak_count "$w/inst/database")"
assert_grep "framework lifekit.py landed (default path)" \
  'LIFEKIT_FIXTURE = "vendor-v2"' "$w/inst/database/lifekit.py"
assert_nogrep "no code-only notice on a default run" "code-only mode" "$w/t1.log"

# ===========================================================================
CURRENT="T2"
say "T2: --code-only lands code, skips the 3f-migrate step (DB frozen)"
w="$WORK/t2"; mkdir -p "$w"
build_repo "$w/repo" 11
build_instance "$w/inst" t2agent "$w/repo" 10 10
run_update "$w/repo" "$w/inst" "$w/t2.log" --code-only
assert_eq "update exits 0" "0" "$?"
assert_eq "DB user_version FROZEN at 10" "10" "$(db_ver "$w/inst/database/lifekit.db")"
assert_eq "no DB backup written (nothing mutated)" "0" "$(bak_count "$w/inst/database")"
assert_grep "escape-hatch notice printed" "code-only mode" "$w/t2.log"
assert_grep "skip line names the migration" "skipped migration 011" "$w/t2.log"
assert_grep "summary reports the skip + frozen version" \
  "SKIPPED by --code-only (011; DB stays v10)" "$w/t2.log"
assert_grep "framework CODE landed (routines marker)" \
  "vendor-v2" "$w/inst/routines/marker.sh"
assert_grep "schema.sql landed (fresh-DB feed, no existing-DB mutation)" \
  "schema fixture vendor-v2" "$w/inst/database/schema.sql"

# ===========================================================================
CURRENT="T3"
say "T3: forward-pin guard holds back lifekit.py whose pin outruns the frozen DB"
w="$WORK/t3"; mkdir -p "$w"
build_repo "$w/repo" 11          # framework pin 11
build_instance "$w/inst" t3agent "$w/repo" 10 10   # instance pin 10, DB v10
before_sha="$(file_sha "$w/inst/database/lifekit.py")"
run_update "$w/repo" "$w/inst" "$w/t3.log" --code-only
assert_eq "update exits 0" "0" "$?"
assert_grep "guard warn printed" "FORWARD-PIN GUARD triggered" "$w/t3.log"
assert_eq "instance lifekit.py untouched (pin stays lineage-matched)" \
  "$before_sha" "$(file_sha "$w/inst/database/lifekit.py")"
assert_grep "instance copy still instance-v1" \
  'LIFEKIT_FIXTURE = "instance-v1"' "$w/inst/database/lifekit.py"
assert_eq "DB still frozen at 10" "10" "$(db_ver "$w/inst/database/lifekit.db")"

# ===========================================================================
CURRENT="T4"
say "T4: --code-only still lands lifekit.py when the pin is coherent (pin == DB)"
w="$WORK/t4"; mkdir -p "$w"
build_repo "$w/repo" 10          # framework pin 10 == DB v10: no outrun
rm -f "$w/repo/database/migrations/011_income_kind.sql"  # nothing pending
build_instance "$w/inst" t4agent "$w/repo" 10 10
run_update "$w/repo" "$w/inst" "$w/t4.log" --code-only
assert_eq "update exits 0" "0" "$?"
assert_grep "framework lifekit.py landed under --code-only" \
  'LIFEKIT_FIXTURE = "vendor-v2"' "$w/inst/database/lifekit.py"
assert_nogrep "no forward-pin warn" "FORWARD-PIN GUARD" "$w/t4.log"
assert_eq "DB untouched" "10" "$(db_ver "$w/inst/database/lifekit.db")"

# ===========================================================================
CURRENT="T5"
say "T5: --dry-run --code-only writes nothing, reports would-skip"
w="$WORK/t5"; mkdir -p "$w"
build_repo "$w/repo" 11
build_instance "$w/inst" t5agent "$w/repo" 10 10
before_lk="$(file_sha "$w/inst/database/lifekit.py")"
before_mk_missing=1; [ -f "$w/inst/routines/marker.sh" ] && before_mk_missing=0
run_update "$w/repo" "$w/inst" "$w/t5.log" --code-only --dry-run
assert_eq "update exits 0" "0" "$?"
assert_eq "DB untouched" "10" "$(db_ver "$w/inst/database/lifekit.db")"
assert_eq "no DB backup" "0" "$(bak_count "$w/inst/database")"
assert_eq "instance lifekit.py untouched" "$before_lk" "$(file_sha "$w/inst/database/lifekit.py")"
if [ "$before_mk_missing" = "1" ] && [ ! -f "$w/inst/routines/marker.sh" ]; then
  ok "no code landed under dry-run"
else
  bad "dry-run landed code (routines/marker.sh appeared)"
fi
assert_grep "would-skip migration reported" "would skip migration 011" "$w/t5.log"
assert_grep "would-skip lifekit.py reported (forward-pin, dry branch)" \
  "would SKIP database/lifekit.py" "$w/t5.log"

# ===========================================================================
CURRENT="T6"
say "T6: DGN-663 -- framework .claude/agents/ defs land into an existing instance"
w="$WORK/t6"; mkdir -p "$w"
build_repo "$w/repo" 11
build_instance "$w/inst" t6agent "$w/repo" 10 10
# instance starts with NO .claude/agents/ dir (pre-DGN-663 mint that never
# received defs, or a def improvement made after mint).
[ -e "$w/inst/.claude/agents" ] && bad "precondition: agents dir already present"
run_update "$w/repo" "$w/inst" "$w/t6.log"
assert_eq "update exits 0" "0" "$?"
if [ -f "$w/inst/.claude/agents/baseline-editor.md" ]; then
  ok "framework baseline-editor.md delivered to instance"
else
  bad "framework baseline-editor.md NOT delivered"
fi
assert_grep "delivered def carries vendor-v2 content" \
  "FRAMEWORK agent def vendor-v2" "$w/inst/.claude/agents/baseline-editor.md"
assert_grep ".claude/agents in the update summary" ".claude/agents/" "$w/t6.log"
# __PROJECT_ROOT__ placeholder was re-substituted in-loop (section 3j2).
assert_grep "release-closer.md placeholder substituted to instance path" \
  "ROOT=$w/inst" "$w/inst/.claude/agents/release-closer.md"
assert_nogrep "no raw __PROJECT_ROOT__ left in agent def" \
  "__PROJECT_ROOT__" "$w/inst/.claude/agents/release-closer.md"

# ===========================================================================
CURRENT="T7"
say "T7: DGN-663 preserve supremacy -- a .dogany-preserve'd agent def is NOT clobbered"
w="$WORK/t7"; mkdir -p "$w"
build_repo "$w/repo" 11
build_instance "$w/inst" t7agent "$w/repo" 10 10
mkdir -p "$w/inst/.claude/agents"
# Instance has a LOCALLY-MODIFIED baseline-editor.md (e.g. Metal's compressed
# variant) and declares it in .dogany-preserve. release-closer.md is NOT
# preserved -> it should refresh from the framework.
printf -- '---\nname: baseline-editor\n---\nLOCAL compressed variant KEEP-ME\n' \
  > "$w/inst/.claude/agents/baseline-editor.md"
printf -- '---\nname: release-closer\n---\nSTALE local copy\n' \
  > "$w/inst/.claude/agents/release-closer.md"
printf '.claude/agents/baseline-editor.md\n' > "$w/inst/.claude/.dogany-preserve"
before_be="$(file_sha "$w/inst/.claude/agents/baseline-editor.md")"
run_update "$w/repo" "$w/inst" "$w/t7.log"
assert_eq "update exits 0" "0" "$?"
assert_eq "preserved baseline-editor.md byte-identical (NOT clobbered)" \
  "$before_be" "$(file_sha "$w/inst/.claude/agents/baseline-editor.md")"
assert_grep "preserved def still carries the local marker" \
  "LOCAL compressed variant KEEP-ME" "$w/inst/.claude/agents/baseline-editor.md"
assert_nogrep "framework content did NOT overwrite the preserved def" \
  "FRAMEWORK agent def vendor-v2" "$w/inst/.claude/agents/baseline-editor.md"
assert_grep "unlisted release-closer.md DID refresh from framework" \
  "vendor-v2" "$w/inst/.claude/agents/release-closer.md"
# The preserved entry must be RECOGNIZED as a matched section, not false-warned.
assert_nogrep "no 'matched no section' false-warn for the agents preserve entry" \
  "matched no section" "$w/t7.log"

# ===========================================================================
say ""
say "== dgn656/663 self-test: $PASS ok, $FAIL failed =="
[ "$FAIL" = "0" ] || exit 1
exit 0
