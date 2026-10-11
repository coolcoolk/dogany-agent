#!/usr/bin/env bash
# gate.sh -- dogany-mac-optimize gates (DGN-1712). READ-ONLY, writes nothing.
#
#   gate.sh [check] [ROOT]   entry gate, run FIRST.
#     1. platform: `uname -s` must be Darwin. Anything else -> not supported.
#     2. representative: this instance must be its crew's representative.
#        Field = DOGANY_AGENT_CLASS in <root>/.instance.conf (main | domain),
#        same field + rule as install.sh instance_class_of(): `domain` = not
#        representative; any other value or absent key/file = main (G1/P13).
#        crew.conf carries member= lines only -- no leader field exists, so
#        the class is the only representative mark in the estate.
#     ROOT: arg, else $CLAUDE_PROJECT_DIR, else $PWD. Not an instance (no
#     config/agent.conf) = cannot confirm = not allowed.
#     exit 0 ok | 3 not macOS | 4 not representative | 5 root not an instance
#
#   gate.sh owner PID        ownership walk for one process.
#     Walks ppid up from PID to the nearest live Claude Code process.
#     session=<claude pid>|none ; offlimits=yes|no. Any claude ancestor =
#     off limits. No claude ancestor but walk broke early (pid vanished,
#     ps failed) = unsure = off limits. exit 0 (verdict is in the output).
set -u

is_claude() {  # $1 = full comm path from ps
  case "$1" in
    claude|*/claude|*/claude/versions/*) return 0 ;;
  esac
  return 1
}

claude_ancestor() {  # $1 = pid -> echo claude pid | none | unsure
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

os="$(uname -s 2>/dev/null || echo unknown)"
echo "platform=$os"
if [ "$os" != "Darwin" ]; then
  echo "verdict=not-mac"
  exit 3
fi

if [ "${1:-}" = "owner" ]; then
  s="$(claude_ancestor "${2:-}")"
  echo "session=$s"
  case "$s" in none) echo "offlimits=no" ;; *) echo "offlimits=yes" ;; esac
  exit 0
fi
[ "${1:-}" = "check" ] && shift

root="${1:-${CLAUDE_PROJECT_DIR:-$PWD}}"
echo "root=$root"
if [ ! -f "$root/config/agent.conf" ]; then
  echo "verdict=unknown-root"
  exit 5
fi

gate_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
. "$gate_dir/../../scripts/pack/lib/instance_class.sh"
class="$(instance_class_of "$root")"
case "$class" in
  domain) echo "class=domain"; echo "verdict=not-representative"; exit 4 ;;
  *)      echo "class=main";   echo "verdict=ok"; exit 0 ;;
esac
