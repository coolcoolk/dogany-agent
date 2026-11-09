#!/bin/bash
# test-lane: fast
# DGN-1730: routines/ledger-reconcile.sh -- flips already-merged pending-review
# rows to merged (via lib/ledger-update.sh), reports counts, --dry-run writes
# nothing; and cleanup-files.sh runs it (the wiring point).
#
# Run: bash agents/.template/routines/tests/test-dgn1730-ledger-reconcile.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTINES_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
has()  { if grep -qF -- "$2" <<<"$3"; then ok "$1"; else fail "$1 -- missing: $2"; fi; }
hasnt(){ if grep -qF -- "$2" <<<"$3"; then fail "$1 -- unexpected: $2"; else ok "$1"; fi; }

T="$(mktemp -d "${TMPDIR:-/tmp}/dgn1730.XXXXXX")"
trap 'rm -rf "$T"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# fake instance: routines/ (copies of the real scripts) + product/ledger + git repo (main)
INST="$T/inst"; mkdir -p "$INST/routines/lib" "$INST/product" "$INST/files/tmp"
cp "$ROUTINES_DIR/ledger-reconcile.sh" "$ROUTINES_DIR/cleanup-files.sh" "$INST/routines/"
cp "$ROUTINES_DIR/lib/ledger-update.sh" "$INST/routines/lib/"
CANON="$T/canon"; mkdir -p "$CANON"
for r in "$INST" "$CANON"; do
  base=main; [[ "$r" == "$CANON" ]] && base=dev
  git -C "$r" init -q -b "$base"; echo x > "$r/root"; git -C "$r" add -A; git -C "$r" commit -q -m root
done
mk() { # repo base branch -> commit on branch, back to base
  git -C "$1" checkout -q -b "$3" "$2"; echo "$3" > "$1/$(echo "$3" | tr / _)"; git -C "$1" add -A
  git -C "$1" commit -q -m "$3"; git -C "$1" checkout -q "$2"; }
mrg() { git -C "$1" merge -q --no-ff -m "m $3" "$3"; }

mk "$INST" main dsp/a-merged;      mrg "$INST" main dsp/a-merged
mk "$INST" main dsp/b-sha;         mrg "$INST" main dsp/b-sha
SHA_B="$(git -C "$INST" rev-parse dsp/b-sha | cut -c1-10)"; git -C "$INST" branch -q -D dsp/b-sha
mk "$INST" main dsp/c-unmerged
mk "$CANON" dev dsp/e-canon;       mrg "$CANON" dev dsp/e-canon

LEDGER="$INST/product/auto-loop-ledger.md"
{
  echo "# auto-loop-ledger"
  echo "| dsp/a-merged | ia | pending-review | 0 | - | ts | - | dispatch (HEAD=deadbeef) |"
  echo "| dsp/b-sha | ib | pending-review | 0 | - | ts | - | dispatch (HEAD=$SHA_B) |"
  echo "| dsp/c-unmerged | ic | pending-review | 0 | - | ts | - | dispatch (HEAD=deadbeef) |"
  echo "| dsp/e-canon | ie | pending-review | 0 | - | ts | - | dispatch (HEAD=deadbeef) |"
  echo "| dsp/f-lost | if | pending-review | 0 | - | ts | - | dispatch (HEAD=0123456789) |"
  echo "| dsp/g-running | ig | running | 0 | - | ts | - | x |"
} > "$LEDGER"
BEFORE="$(cat "$LEDGER")"
export DOGANY_REPO_ROOT="$CANON"
unset DOGANY_LEDGER_FILE DOGANY_INSTANCE_ROOT

echo "== --dry-run: reports, writes nothing"
OUT="$(bash "$INST/routines/ledger-reconcile.sh" --dry-run 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok "dry-run exit 0" || fail "dry-run exit $RC"
has "dry-run counts" "flipped=3 unmerged=1 unresolvable=1 dry-run" "$OUT"
has "dry-run names the merged branch" "would flip dsp/a-merged" "$OUT"
[[ "$(cat "$LEDGER")" == "$BEFORE" ]] && ok "dry-run leaves ledger byte-identical" || fail "dry-run modified the ledger"

echo "== real run"
OUT="$(bash "$INST/routines/ledger-reconcile.sh" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok "exit 0" || fail "exit $RC"
has "real counts" "flipped=3 unmerged=1 unresolvable=1" "$OUT"
hasnt "real run is not tagged dry-run" "dry-run" "$OUT"
L="$(cat "$LEDGER")"
has "merged-branch row flipped" "| dsp/a-merged | ia | merged |" "$L"
has "sha-only row flipped" "| dsp/b-sha | ib | merged |" "$L"
has "canonical-repo (dev) row flipped" "| dsp/e-canon | ie | merged |" "$L"
has "flip note" "reconciled: already in base" "$L"
has "unmerged row untouched" "| dsp/c-unmerged | ic | pending-review |" "$L"
has "unresolvable row untouched" "| dsp/f-lost | if | pending-review |" "$L"
has "running row untouched" "| dsp/g-running | ig | running |" "$L"

echo "== idempotent"
OUT="$(bash "$INST/routines/ledger-reconcile.sh" 2>&1)"
has "second run flips nothing" "flipped=0 unmerged=1 unresolvable=1" "$OUT"

echo "== degrade: no readable repo / no ledger -> exit 0 no-op"
OUT="$(DOGANY_INSTANCE_ROOT="$T/nowhere" DOGANY_REPO_ROOT="$T/nowhere" bash "$INST/routines/ledger-reconcile.sh" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok "no repo: exit 0" || fail "no repo: exit $RC"
has "no repo: reported" "nothing to do" "$OUT"
OUT="$(bash "$INST/routines/ledger-reconcile.sh" --ledger "$T/missing.md" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok "missing ledger: exit 0" || fail "missing ledger: exit $RC"

echo "== wiring: cleanup-files.sh runs the reconcile (dry-run passes through)"
{
  echo "# auto-loop-ledger"
  echo "| dsp/a-merged | ia | pending-review | 0 | - | ts | - | n |"
} > "$LEDGER"
OUT="$(bash "$INST/routines/cleanup-files.sh" --dry-run 2>&1)"
has "cleanup dry-run invokes reconcile dry-run" "ledger-reconcile: would flip dsp/a-merged" "$OUT"
has "cleanup dry-run wrote nothing" "| dsp/a-merged | ia | pending-review |" "$(cat "$LEDGER")"
OUT="$(bash "$INST/routines/cleanup-files.sh" 2>&1)"; RC=$?
[[ $RC -eq 0 ]] && ok "cleanup exit 0" || fail "cleanup exit $RC"
has "cleanup real run flips the row" "| dsp/a-merged | ia | merged |" "$(cat "$LEDGER")"

echo
echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
