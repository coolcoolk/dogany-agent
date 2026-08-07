#!/bin/bash
# test-publish-dryrun.sh -- self-test for scripts/publish.sh (DGN-604 M3).
#
# Verifies that a --dry-run reads all 22 product.yaml units and produces a
# correct export / exclude classification:
#   - 22 units total, 21 public+official selected (only spending-log excluded)
#   - the private/poc unit (spending-log) is NOT in the selected set
#   - default-deny: shared no-owner files (rules/, database/, docs/*.md) do NOT
#     appear in the unit export list
#   - FIX 1: repo-meta (LICENSE/NOTICE/READMEs/CHANGELOG/.gitignore + exactly
#     the README-referenced docs/img images) is INJECTED, with no docs/ wildcard
#   - FIX 2: agent-browser (Vercel Labs IP) is THIRD-PARTY-BLOCKED, not exported,
#     while OUR framework browser-auth wrapper still exports
#   - most-specific-owner-wins: youtube-digest paths are owned by youtube-digest
#     (its own subdir), NOT by the broad agent-template glob
#   - F9 class blocks packs/health-trainer/warg and packs/dev/refdev even though
#     the framework unit owns packs/**
#   - the run exits 0 in dry-run and pushes NOTHING
#
# Exit 0 = all assertions pass; nonzero = a failure (prints which).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PUBLISH="$SCRIPT_DIR/../publish.sh"
# BSD mktemp (macOS) requires the X placeholder run to be at the END of the
# template; a trailing extension after the X's fails. Use a dir + fixed names.
TDIR="$(mktemp -d "${TMPDIR:-/tmp}/m3-selftest.XXXXXX")"
REPORT="$TDIR/report.md"
LOG="$TDIR/log.txt"

FAILS=0
check() {  # $1=description  $2=condition-already-evaluated (0 pass / nonzero fail)
  if [ "$2" -eq 0 ]; then
    echo "  ok   $1"
  else
    echo "  FAIL $1"
    FAILS=$(( FAILS + 1 ))
  fi
}

echo "== publish.sh dry-run self-test =="

# Run dry-run, capture stdout log + markdown report.
bash "$PUBLISH" --report "$REPORT" > "$LOG" 2>&1
RC=$?
check "dry-run exits 0" "$RC"

# --- unit counts -------------------------------------------------------------
grep -q "product.yaml units found: 22" "$LOG"; check "22 units read" $?
grep -q "public+official units selected: 21" "$LOG"; check "21 public+official selected" $?

# --- private/poc unit excluded ----------------------------------------------
grep -q '`spending-log` (private/poc)' "$REPORT"; check "spending-log listed as excluded (private/poc)" $?
# The private PoC module spending-log lives under agent-template's broad glob.
# It must be PRIVATE-BLOCKED, never exported (spec PoC/private 제외).
sed -n '/## PRIVATE-BLOCKED/,/## No-owner/p' "$REPORT" | grep -q 'skills-bundle/spending-log'
check "spending-log subtree is PRIVATE-BLOCKED" $?
# And confirm no spending-log path leaks into the export list.
sed -n '/## Export list/,/## F9-BLOCKED/p' "$REPORT" | grep -q 'skills-bundle/spending-log'
check "spending-log NOT in export list" $([ $? -ne 0 ] && echo 0 || echo 1)

# --- default-deny: shared no-owner files not attributed to a unit ------------
# NB: the unit Export list must never attribute repo-meta / shared paths to a
# unit owner. (repo-meta ships via the separate REPO-META injection section --
# asserted below.) rules/ database/ docs/*.md stay dropped entirely.
EXPORT_SECTION="$(sed -n '/## Export list/,/## REPO-META/p' "$REPORT")"
printf '%s\n' "$EXPORT_SECTION" | grep -qE '^- (LICENSE|NOTICE|README\.md|README-ko\.md|CHANGELOG\.md|\.gitignore)$'
check "repo-meta NOT owned by any unit (only injected, not unit-attributed)" $([ $? -ne 0 ] && echo 0 || echo 1)
printf '%s\n' "$EXPORT_SECTION" | grep -qE '^- rules/'
check "rules/ NOT in export (no-owner)" $([ $? -ne 0 ] && echo 0 || echo 1)
printf '%s\n' "$EXPORT_SECTION" | grep -qE '^- database/'
check "database/ NOT in export (no-owner)" $([ $? -ne 0 ] && echo 0 || echo 1)
printf '%s\n' "$EXPORT_SECTION" | grep -qE '^- docs/[A-Za-z].*\.md'
check "docs/*.md NOT in export (no-owner)" $([ $? -ne 0 ] && echo 0 || echo 1)

# --- FIX 1: repo-meta injection ----------------------------------------------
META_SECTION="$(sed -n '/## REPO-META injected/,/## F9-BLOCKED/p' "$REPORT")"
for f in LICENSE NOTICE README.md README-ko.md CHANGELOG.md .gitignore; do
  printf '%s\n' "$META_SECTION" | grep -qxF -- "- $f"
  check "repo-meta injected: $f" $?
done
grep -q 'files:.*repo-meta=24' "$LOG" 2>/dev/null || grep -q 'repo-meta injected     : 24' "$LOG"
check "repo-meta count = 24 (6 root + 8 README images + 10 install-shared)" $?
# --- FIX 3: install/mint-essential shared files injected ---------------------
# The public repo is a real clone->install->mint distribution. mint.sh reads
# rules/RULES.md (template symlink target) + a fixed set of hoisted database/
# code by name; none is unit-owned, so the strict allowlist drops them and the
# minted instance breaks (no RULES.md, no schema.sql -> lifekit.db never inits).
# These must be injected (enumerated, NOT a rules/ or database/ glob).
for f in rules/RULES.md rules/USER.example.md database/schema.sql \
         database/lifekit.py database/lifekit.sh database/remind_select.py \
         database/routine_roller.py database/routine_projection.py \
         database/relmod.py database/relmod.sh; do
  printf '%s\n' "$META_SECTION" | grep -qxF -- "- $f"
  check "install-shared injected: $f" $?
done
# backdoor guard: injection must NOT pull the dev surface under rules/ database/
# (migrations/ + tests/ are read by no installer path and must stay dropped).
printf '%s\n' "$META_SECTION" | grep -qE '^- database/(migrations|tests)/'
check "install-shared did NOT smuggle database/migrations|tests (no backdoor)" $([ $? -ne 0 ] && echo 0 || echo 1)
# every injected docs/img image must be one the READMEs actually reference
BADIMG=0
while read -r line; do
  case "$line" in "- docs/img/"*)
    img="${line#- }"
    grep -qF "$img" "$SCRIPT_DIR/../../README.md" "$SCRIPT_DIR/../../README-ko.md" || BADIMG=1 ;;
  esac
done <<< "$META_SECTION"
check "every injected docs/img image is README-referenced (no docs/ wildcard)" "$BADIMG"
# backdoor guard: injection must NOT pull unreferenced docs (e.g. docs/*.md)
printf '%s\n' "$META_SECTION" | grep -qE '^- docs/[A-Za-z].*\.md'
check "repo-meta injection did NOT smuggle docs/*.md (no backdoor)" $([ $? -ne 0 ] && echo 0 || echo 1)
printf '%s\n' "$META_SECTION" | grep -qE '^- docs/img/[^ ]+$'
check "repo-meta injection DID include README images" $?

# --- FIX 2: agent-browser (third-party IP) excluded --------------------------
TP_SECTION="$(sed -n '/## THIRD-PARTY-BLOCKED/,/## No-owner/p' "$REPORT")"
printf '%s\n' "$TP_SECTION" | grep -q 'skills-bundle/agent-browser/SKILL.md'
check "agent-browser SKILL.md is THIRD-PARTY-BLOCKED" $?
# and NOT in the unit export list
printf '%s\n' "$EXPORT_SECTION" | grep -q 'skills-bundle/agent-browser'
check "agent-browser NOT in export list (Vercel IP, uncataloged)" $([ $? -ne 0 ] && echo 0 || echo 1)
# OUR framework browser-auth wrapper IS ours -> must still export
printf '%s\n' "$EXPORT_SECTION" | grep -q 'routines/browser-auth-open.sh'
check "OUR browser-auth-open.sh STILL exports (framework IP, not excluded)" $?

# --- most-specific-owner-wins ------------------------------------------------
# youtube-digest paths must be attributed to youtube-digest, not agent-template.
sed -n '/^### youtube-digest$/,/^### /p' "$REPORT" | grep -q 'skills-bundle/youtube-digest'
check "youtube-digest owns its own subdir (most-specific-owner-wins)" $?

# --- F9 class blocks owned domain/PoC-fixture paths --------------------------
grep -q 'packs/health-trainer/warg/' "$REPORT"
check "warg PoC instance config present in report" $?
sed -n '/## F9-BLOCKED/,/## No-owner/p' "$REPORT" | grep -q 'packs/health-trainer/warg/'
check "warg config is F9-BLOCKED (not exported despite owned)" $?
sed -n '/## F9-BLOCKED/,/## No-owner/p' "$REPORT" | grep -q 'packs/dev/refdev/'
check "packs/dev/refdev is F9-BLOCKED" $?
# And confirm warg/refdev are NOT in the export list.
printf '%s\n' "$EXPORT_SECTION" | grep -qE 'packs/health-trainer/warg/'
check "warg config NOT in export list" $([ $? -ne 0 ] && echo 0 || echo 1)

# --- symlink-safety (grill ATTACK 1c) ----------------------------------------
# FIX 3 changed this: rules/RULES.md is now INJECTED, so the template's
# RULES.md symlink target lands inside the export set and the link RESOLVES --
# it must NO LONGER be flagged as escaping, and it MUST ship (a minted instance
# needs RULES.md). The escape gate still stands for any OTHER symlink whose
# target is genuinely outside the export set. The 9 skill symlinks point at
# exported skills/ dirs and MUST stay in the export.
SYM_SECTION="$(sed -n '/## SYMLINK-ESCAPE/,/## No-owner/p' "$REPORT")"
printf '%s\n' "$SYM_SECTION" | grep -q 'agents/.template/RULES.md ->'
check "template RULES.md symlink NO LONGER escapes (target rules/RULES.md injected)" $([ $? -ne 0 ] && echo 0 || echo 1)
printf '%s\n' "$EXPORT_SECTION" | grep -q 'agents/.template/RULES.md'
check "template RULES.md symlink SHIPS (resolves into export set)" $?
N_SKILL_LINKS="$(printf '%s\n' "$EXPORT_SECTION" | grep -c '.claude/skills/dogany-')"
check "all 9 skill symlinks kept (targets are exported skills/)" $([ "$N_SKILL_LINKS" -eq 9 ] && echo 0 || echo 1)

# --- F9 hardening (grill ATTACK 2c/2d) ---------------------------------------
# location-independent db/env class must not have regressed the legit .env.example
# TEMPLATE files (they ship placeholder keys, not secrets).
printf '%s\n' "$EXPORT_SECTION" | grep -q 'bridge/.env.example'
check "legit .env.example TEMPLATE still exports (not F9-blocked)" $?

# --- pushed nothing ----------------------------------------------------------
grep -q 'nothing was created or pushed' "$LOG"; check "dry-run pushed nothing" $?

echo ""
if [ "$FAILS" -eq 0 ]; then
  echo "RESULT: PASS (all assertions)"
  rm -rf "$TDIR"
  exit 0
else
  echo "RESULT: FAIL ($FAILS assertion(s)) -- report=$REPORT log=$LOG"
  exit 1
fi
