#!/usr/bin/env bash
# loadcheck.sh -- dogany-mac-load-check value carrier (DGN-1712).
# READ-ONLY: reads ps / sysctl / df only, writes nothing, anywhere.
#
#   loadcheck.sh [preflight]   machine numbers + heavy job families running.
#     load1/load5/load15, ncpu, swap_used_mb/swap_total_mb,
#     disk_used_pct (data volume), claude_sessions, and one
#     running_<family>=N line per heavy family. Values only -- the agent
#     reads them and decides; this script judges nothing.
#
#   loadcheck.sh owner PID     ownership walk for one process.
#     session=<nearest claude ancestor pid>|none|unsure ;
#     mine=yes (same session as the caller) | no | unsure.
#     Any doubt (walk broke, pid gone) = unsure = treat as not yours.
#
# Platform: macOS only. Non-Darwin -> prints platform=<os>, exit 3.
set -u

os="$(uname -s 2>/dev/null || echo unknown)"
echo "platform=$os"
[ "$os" = "Darwin" ] || { echo "verdict=not-mac"; exit 3; }

is_claude() {
  case "$1" in claude|*/claude|*/claude/versions/*) return 0 ;; esac
  return 1
}

claude_ancestor() {  # $1 = pid -> claude pid | none | unsure
  local p="$1" comm pp n=0
  while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null && [ "$n" -lt 64 ]; do
    comm="$(ps -o comm= -p "$p" 2>/dev/null)" || { echo unsure; return; }
    [ -n "$comm" ] || { echo unsure; return; }
    if is_claude "$comm"; then echo "$p"; return; fi
    pp="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -n "$pp" ] || { echo unsure; return; }
    p="$pp"; n=$((n + 1))
  done
  [ "$n" -lt 64 ] && echo none || echo unsure
}

if [ "${1:-}" = "owner" ]; then
  s="$(claude_ancestor "${2:-}")"
  me="$(claude_ancestor "$$")"
  echo "session=$s"
  case "$s:$me" in
    unsure:*|*:unsure|*:none) echo "mine=unsure" ;;
    none:*)                    echo "mine=no" ;;
    *) [ "$s" = "$me" ] && echo "mine=yes" || echo "mine=no" ;;
  esac
  exit 0
fi

read -r _ l1 l5 l15 _ <<<"$(sysctl -n vm.loadavg 2>/dev/null)"
echo "load1=${l1:-?}"; echo "load5=${l5:-?}"; echo "load15=${l15:-?}"
echo "ncpu=$(sysctl -n hw.ncpu 2>/dev/null || echo '?')"

sw="$(sysctl -n vm.swapusage 2>/dev/null)"
echo "swap_total_mb=$(printf '%s' "$sw" | sed -n 's/.*total = \([0-9.]*\)M.*/\1/p' | cut -d. -f1)"
echo "swap_used_mb=$(printf '%s' "$sw" | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p' | cut -d. -f1)"

vol=/System/Volumes/Data; [ -d "$vol" ] || vol=/
echo "disk_used_pct=$(df -Pk "$vol" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"

psout="$(ps -Ao comm=,args= 2>/dev/null)"
n_claude=0
while read -r comm _; do is_claude "$comm" && n_claude=$((n_claude + 1)); done <<<"$(ps -Ao comm= 2>/dev/null)"
echo "claude_sessions=$n_claude"

count() { printf '%s\n' "$psout" | grep -E -c -- "$1" || true; }
echo "running_xcodebuild=$(count '(^|/)xcodebuild( |$)')"
echo "running_next_build=$(count 'next build')"
echo "running_vitest=$(count '(^|/| )vitest( |$)')"
echo "running_gradle=$(count 'GradleDaemon|(^|/)gradlew?( |$)')"
echo "running_flutter_build=$(count 'flutter.* build ')"
echo "running_simulator=$(count '(^|/)launchd_sim( |$)')"
