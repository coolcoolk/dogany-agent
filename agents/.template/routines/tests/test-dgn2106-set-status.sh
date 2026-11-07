#!/bin/bash
# test-lane: fast
# test-dgn2106-set-status.sh -- worklog-append.sh --set-status (DGN-2106).
#   T1 open -> wip      T2 wip -> done sets closed_at     T3 unknown refused
#   T4 wrong id refused T5 --dry-run writes nothing       T6 no status field
#   T7 two concurrent writers on one file keep both changes
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WA="$SCRIPT_DIR/../worklog-append.sh"
SB="$(mktemp -d /tmp/dgn2106-test.XXXXXX)"
trap 'rm -rf "$SB"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
W="$SB/wl"; mkdir "$W"
printf -- '---\nid: DGN-1\nstatus: open\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\nBody one.\n' > "$W/DGN-1-a.md"
printf -- '---\nid: DGN-2\nstatus: wip\nupdated: 2026-01-01\nclosed_at:\ntitle: keep  me\n---\n\nBody two.\n' > "$W/DGN-2-b.md"
printf -- '---\nid: DGN-3\ntitle: nostatus\n---\n\nBody three.\n' > "$W/DGN-3-c.md"
cp -r "$W" "$SB/orig"
T="$(date +%Y-%m-%d)"

echo "reason one" | "$WA" --worklog "$W" --set-status wip DGN-1 >/dev/null; rc=$?
[ $rc = 0 ] && grep -q '^status: wip$' "$W/DGN-1-a.md" && grep -q "^updated: $T$" "$W/DGN-1-a.md" \
  && grep -q "status open -> wip: reason one" "$W/DGN-1-a.md" && ok T1 || bad "T1 rc=$rc"
diff -u "$SB/orig/DGN-1-a.md" "$W/DGN-1-a.md" | tail -n +3

echo "shipped" | "$WA" --worklog "$W" --set-status done DGN-2 >/dev/null; rc=$?
[ $rc = 0 ] && grep -q "^closed_at: $T$" "$W/DGN-2-b.md" && grep -q '^title: keep  me$' "$W/DGN-2-b.md" && ok T2 || bad "T2 rc=$rc"
diff -u "$SB/orig/DGN-2-b.md" "$W/DGN-2-b.md" | tail -n +3

cp -r "$W" "$SB/pre"
echo x | "$WA" --worklog "$W" --set-status bogus DGN-1 2>"$SB/err"; rc=$?
[ $rc = 2 ] && diff -r "$SB/pre" "$W" >/dev/null && ok T3 || bad "T3 rc=$rc"
echo x | "$WA" --worklog "$W" --set-status done DGN-99 2>/dev/null; rc=$?
[ $rc = 3 ] && diff -r "$SB/pre" "$W" >/dev/null && ok T4 || bad "T4 rc=$rc"
echo x | "$WA" --worklog "$W" --set-status done --dry-run DGN-1 | head -12; rc=${PIPESTATUS[1]}
diff -r "$SB/pre" "$W" >/dev/null && [ $rc = 0 ] && ok T5 || bad "T5 rc=$rc"
echo x | "$WA" --worklog "$W" --set-status done DGN-3 2>/dev/null; rc=$?
[ $rc = 6 ] && diff -r "$SB/pre" "$W" >/dev/null && ok T6 || bad "T6 rc=$rc"

for i in 1 2 3 4 5 6; do
  ( echo "w$i" | "$WA" --worklog "$W" DGN-1 >/dev/null ) &
done
( echo c | "$WA" --worklog "$W" --set-status blocked DGN-1 >/dev/null ) &
wait
n=$(grep -cE '^w[1-6]$' "$W/DGN-1-a.md")
[ "$n" = 6 ] && grep -q '^status: blocked$' "$W/DGN-1-a.md" && [ "$(grep -c '^---$' "$W/DGN-1-a.md")" = 2 ] && ok T7 || bad "T7 n=$n"
ls "$W" | grep -qE 'lock|tmp' && bad "T7 leftovers" 

echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ]
