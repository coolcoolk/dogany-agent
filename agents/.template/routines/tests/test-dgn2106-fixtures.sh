#!/bin/bash
# test-lane: fast
# test-dgn2106-fixtures.sh -- --set-status keeps quote style; plain append never
# loses a racing unlocked `>>` (DGN-2106 review).
set -u
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WA="$D/../worklog-append.sh"
SB="$(mktemp -d /tmp/dgn2106-fx.XXXXXX)"; trap 'rm -rf "$SB"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
T="$(date +%Y-%m-%d)"
mkdir "$SB/wl"; cp "$D"/fixtures/dgn2106/*.md "$SB/wl/"
show() { [ -z "${SHOW:-}" ] || diff -u --label "before $1" --label "after $1" "$2" "$3" | tail -n +3; }

step() { # id file new
  cp "$SB/wl/$2" "$SB/prev.md"
  echo "r-$3" | "$WA" --worklog "$SB/wl" --set-status "$3" "$1" >/dev/null || return 1
  show "$2 ->$3" "$SB/prev.md" "$SB/wl/$2"
}
step DGN-9001 DGN-9001-quoted.md wip  && grep -q "^updated: \"$T\"$" "$SB/wl/DGN-9001-quoted.md" && ok quoted-wip || bad quoted-wip
step DGN-9001 DGN-9001-quoted.md done && grep -q "^updated: \"$T\"$" "$SB/wl/DGN-9001-quoted.md" \
  && grep -q "^closed_at: $T$" "$SB/wl/DGN-9001-quoted.md" && ok quoted-done || bad quoted-done
step DGN-9002 DGN-9002-bare.md wip  && grep -q "^updated: $T$" "$SB/wl/DGN-9002-bare.md" && ok bare-wip || bad bare-wip
step DGN-9002 DGN-9002-bare.md done && grep -q "^closed_at: $T$" "$SB/wl/DGN-9002-bare.md" && ok bare-done || bad bare-done
step DGN-9003 DGN-9003-noupd.md wip && grep -q "^updated: $T$" "$SB/wl/DGN-9003-noupd.md" && ok noupd-wip || bad noupd-wip
step DGN-9003 DGN-9003-noupd.md done && ! grep -q '^closed_at' "$SB/wl/DGN-9003-noupd.md" && ok noupd-done || bad noupd-done
# single-quote style preserved
printf -- "---\nid: DGN-9004\nstatus: open\nupdated: '2026-01-01'\n---\n" > "$SB/wl/DGN-9004-sq.md"
echo r | "$WA" --worklog "$SB/wl" --set-status wip DGN-9004 >/dev/null
grep -q "^updated: '$T'$" "$SB/wl/DGN-9004-sq.md" && ok single-quote || bad single-quote

# race: 50 unlocked plain >> appends vs 20 --set-status
R="$SB/wl/DGN-9005-race.md"
printf -- '---\nid: DGN-9005\nstatus: open\nupdated: "2026-01-01"\n---\n\nbody\n' > "$R"
( for i in $(seq 1 50); do echo "UNLOCKED-$i" >> "$R"; done ) &
w=$!
for i in $(seq 1 20); do
  s=wip; [ $((i%2)) = 0 ] && s=blocked
  echo "race $i" | "$WA" --worklog "$SB/wl" --set-status $s DGN-9005 >/dev/null
done
wait $w
n=$(grep -c '^UNLOCKED-' "$R"); sl=$(grep -c ' status ' "$R")
echo "  race: unlocked lines present=$n/50, status log lines=$sl/20"
[ "$n" = 50 ] && [ "$sl" = 20 ] && ok race || bad "race $n/50 $sl/20"
# plain append in place keeps inode
i1=$(ls -i "$R" | cut -d' ' -f1); echo x | "$WA" --worklog "$SB/wl" DGN-9005 >/dev/null
[ "$i1" = "$(ls -i "$R" | cut -d' ' -f1)" ] && ok append-inplace || bad append-inplace
echo "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" = 0 ]
