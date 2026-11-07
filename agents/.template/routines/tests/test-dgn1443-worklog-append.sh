#!/bin/bash
# test-lane: fast
# test-dgn1443-worklog-append.sh -- regression test for DGN-1443.
#
# Defect: `cat >> worklog/<ID>-<slug>.md` with a misremembered slug never
# fails -- the shell silently creates a new frontmatter-less file and the
# real ticket is left untouched. routines/worklog-append.sh takes the ID,
# finds the file itself, and refuses rather than create.
#
#   T0  the defect itself: `cat >>` with a wrong slug creates an orphan
#   T1  one ticket for the ID -> append lands in the real file
#   T2  no ticket for the ID -> refuse (exit 3), nothing created
#   T3  only an id-less file with the right prefix (an earlier orphan) ->
#       refuse, reported as a near-miss, not appended to
#   T4  companion doc (<ID>-SPEC-v2.md, own id) beside the ticket -> ticket wins
#   T5  two files claim the same id -> refuse (exit 4), both listed
#   T6  --new creates only with an explicit slug, with frontmatter id;
#       --new on an existing ID refuses (exit 5)
#   T7  prefix collision: DGN-911 must not resolve to DGN-9110-*
#   T8  --orphans lists id-less files, skips _-reserved, exit 1 / 0
#   T9  file without a trailing newline -> append starts on its own line
#   T10 empty input -> refuse, file unchanged
#   T11 --new folds stdin frontmatter into the first block and records Why
#   T12 fixes inherit the restored ticket Why; unrelated tickets record none

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WA="$SCRIPT_DIR/../worklog-append.sh"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/dgn1443-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', expected '$3')"; fi; }
check_has() { if printf '%s\n' "$2" | grep -qE -- "$3"; then ok "$1"; else bad "$1 (no /$3/ in: $2)"; fi; }

ticket() {  # <dir> <file> <id>
  printf -- '---\nid: %s\nstatus: open\n---\n\nBody.\n' "$3" > "$1/$2"
}
fresh() { local d="$SANDBOX/$1"; mkdir -p "$d"; echo "$d"; }
count_md() { find "$1" -maxdepth 1 -name '*.md' | wc -l | tr -d ' '; }

# T0 -- reproduce the defect the script exists to stop
w="$(fresh t0)"
ticket "$w" DGN-9001-declared-vs-actual-audit.md DGN-9001
echo "log line" >> "$w/DGN-9001-declared-actual-audit.md"
check_eq "T0 cat >> with a wrong slug silently creates a second file" "$(count_md "$w")" "2"
check_eq "T0 ... and the real ticket is untouched" "$(grep -c 'log line' "$w/DGN-9001-declared-vs-actual-audit.md")" "0"

# T1
w="$(fresh t1)"
ticket "$w" DGN-9001-declared-vs-actual-audit.md DGN-9001
out="$(echo "log line" | "$WA" --worklog "$w" DGN-9001 2>&1)"; rc=$?
check_eq "T1 exit 0" "$rc" "0"
check_eq "T1 appended to the real ticket" "$(tail -1 "$w/DGN-9001-declared-vs-actual-audit.md")" "log line"
check_eq "T1 no new file" "$(count_md "$w")" "1"
check_has "T1 prints the resolved path" "$out" "DGN-9001-declared-vs-actual-audit.md"

# T2
w="$(fresh t2)"
ticket "$w" DGN-9001-a.md DGN-9001
out="$(echo x | "$WA" --worklog "$w" DGN-9002 2>&1)"; rc=$?
check_eq "T2 exit 3" "$rc" "3"
check_eq "T2 nothing created" "$(count_md "$w")" "1"
check_has "T2 says --new is needed" "$out" "--new"

# T3
w="$(fresh t3)"
echo "stray" > "$w/DGN-9003-wrong-slug.md"
out="$(echo x | "$WA" --worklog "$w" DGN-9003 2>&1)"; rc=$?
check_eq "T3 exit 3 (orphan is not the ticket)" "$rc" "3"
check_has "T3 orphan reported as near-miss" "$out" "near-miss: .*DGN-9003-wrong-slug.md"
check_eq "T3 orphan not appended to" "$(cat "$w/DGN-9003-wrong-slug.md")" "stray"

# T4
w="$(fresh t4)"
ticket "$w" DGN-9004-SPEC-v2.md DGN-9004-SPEC-v2
ticket "$w" DGN-9004-thing.md DGN-9004
out="$(echo x | "$WA" --worklog "$w" DGN-9004 2>&1)"; rc=$?
check_eq "T4 exit 0" "$rc" "0"
check_eq "T4 ticket got the append" "$(tail -1 "$w/DGN-9004-thing.md")" "x"
check_eq "T4 companion doc untouched" "$(tail -1 "$w/DGN-9004-SPEC-v2.md")" "Body."

# T5
w="$(fresh t5)"
ticket "$w" DGN-9005-one.md DGN-9005
ticket "$w" DGN-9005-two.md DGN-9005
out="$(echo x | "$WA" --worklog "$w" DGN-9005 2>&1)"; rc=$?
check_eq "T5 exit 4" "$rc" "4"
check_has "T5 lists candidate one" "$out" "candidate: .*DGN-9005-one.md"
check_has "T5 lists candidate two" "$out" "candidate: .*DGN-9005-two.md"
check_eq "T5 neither appended" "$(tail -1 "$w/DGN-9005-one.md")$(tail -1 "$w/DGN-9005-two.md")" "Body.Body."

# T6
w="$(fresh t6)"
echo x | "$WA" --worklog "$w" --new DGN-9006 >/dev/null 2>&1; rc=$?
check_eq "T6 --new without --slug refused" "$rc" "2"
echo "first" | "$WA" --worklog "$w" --new --slug fresh-thing DGN-9006 >/dev/null 2>&1; rc=$?
check_eq "T6 --new --slug exit 0" "$rc" "0"
check_eq "T6 created file carries frontmatter id" "$(sed -n 2p "$w/DGN-9006-fresh-thing.md")" "id: DGN-9006"
check_eq "T6 body written" "$(tail -1 "$w/DGN-9006-fresh-thing.md")" "first"
echo x | "$WA" --worklog "$w" --new --slug other DGN-9006 >/dev/null 2>&1; rc=$?
check_eq "T6 --new on an existing ID refused" "$rc" "5"
check_eq "T6 no second file" "$(count_md "$w")" "1"
echo "second" | "$WA" --worklog "$w" DGN-9006 >/dev/null 2>&1
check_eq "T6 plain append finds the new ticket" "$(tail -1 "$w/DGN-9006-fresh-thing.md")" "second"

# T7
w="$(fresh t7)"
ticket "$w" DGN-9110-longer.md DGN-9110
echo x | "$WA" --worklog "$w" DGN-911 >/dev/null 2>&1; rc=$?
check_eq "T7 DGN-911 does not resolve to DGN-9110" "$rc" "3"
echo x | "$WA" --worklog "$w" DGN-911-longer.md >/dev/null 2>&1; rc=$?
check_eq "T7 a file name instead of an ID is refused" "$rc" "2"

# T8
w="$(fresh t8)"
ticket "$w" DGN-9008-ok.md DGN-9008
echo "stray" > "$w/DGN-9008-typo.md"
printf -- '---\ntitle: no id\n---\n' > "$w/DGN-9009-noid.md"
printf -- '# decisions\n' > "$w/_DECISIONS.md"
out="$("$WA" --worklog "$w" --orphans 2>&1)"; rc=$?
check_eq "T8 --orphans exit 1 when orphans exist" "$rc" "1"
check_has "T8 lists the no-frontmatter file" "$out" "orphan: .*DGN-9008-typo.md"
check_has "T8 lists the frontmatter-without-id file" "$out" "orphan: .*DGN-9009-noid.md"
if printf '%s\n' "$out" | grep -qE "DGN-9008-ok.md|_DECISIONS"; then bad "T8 real ticket / reserved file not listed"; else ok "T8 real ticket / reserved file not listed"; fi
rm "$w/DGN-9008-typo.md" "$w/DGN-9009-noid.md"
"$WA" --worklog "$w" --orphans >/dev/null 2>&1; rc=$?
check_eq "T8 --orphans exit 0 when clean" "$rc" "0"

# T9
w="$(fresh t9)"
printf -- '---\nid: DGN-9010\n---\nlast' > "$w/DGN-9010-x.md"
echo "next" | "$WA" --worklog "$w" DGN-9010 >/dev/null 2>&1
check_eq "T9 prior last line intact" "$(tail -2 "$w/DGN-9010-x.md" | head -1)" "last"
check_eq "T9 append on its own line" "$(tail -1 "$w/DGN-9010-x.md")" "next"

# T10
w="$(fresh t10)"
ticket "$w" DGN-9011-x.md DGN-9011
before="$(cat "$w/DGN-9011-x.md")"
"$WA" --worklog "$w" DGN-9011 </dev/null >/dev/null 2>&1; rc=$?
check_eq "T10 empty input refused" "$rc" "2"
check_eq "T10 file unchanged" "$(cat "$w/DGN-9011-x.md")" "$before"

# T11
w="$(fresh t11)"
printf '%s\n' '---' 'kind: feature' 'parent: DGN-1' 'why: owner context' '---' 'Body.' |
  "$WA" --worklog "$w" --new --slug merged --why why-0042 DGN-9012 >/dev/null 2>&1
created="$w/DGN-9012-merged.md"
check_eq "T11 only one frontmatter block" "$(grep -c '^---$' "$created")" "2"
check_has "T11 explicit Why recorded" "$(cat "$created")" '^why_id: why-0042$'
check_has "T11 stdin kind merged" "$(sed -n '1,/^---$/p' "$created" | tail -n +2)" '^kind: feature$'
check_has "T11 stdin parent merged" "$(cat "$created")" '^parent: DGN-1$'
check_has "T11 stdin why merged" "$(cat "$created")" '^why: owner context$'
check_eq "T11 body remains body" "$(tail -1 "$created")" "Body."

# T12
w="$(fresh t12)"
printf -- '%s\n' '---' 'id: DGN-9013' 'status: done' 'why_id: why-0007' '---' > "$w/DGN-9013-original.md"
printf '%s\n' '---' 'kind: fix' 'restores: DGN-9013' '---' 'Repair.' |
  "$WA" --worklog "$w" --new --slug repair DGN-9014 >/dev/null 2>&1
check_has "T12 fix inherits Why" "$(cat "$w/DGN-9014-repair.md")" '^why_id: why-0007$'
check_has "T12 inheritance source recorded" "$(cat "$w/DGN-9014-repair.md")" '^why_inherited_from: DGN-9013$'
out="$(printf 'Plain.\n' | "$WA" --worklog "$w" --new --slug plain DGN-9015 2>&1)"
check_has "T12 no-Why ticket records none" "$(cat "$w/DGN-9015-plain.md")" '^why_id: none$'
check_has "T12 no-Why ticket emits one hint" "$out" '^worklog-append: no Why recorded;'

echo ""
echo "== test-dgn1443-worklog-append.sh: ${PASS} passed, ${FAIL} failed =="
[ "$FAIL" -eq 0 ]
