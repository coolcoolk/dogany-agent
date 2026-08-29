#!/usr/bin/env bash
# usage-gate.sh -- Claude usage window gate for autonomous-loop spawn decisions.
# Wrapper around claude-usage.sh output only [F2]: NO credential/keychain access.
#
# Exit codes:
#   0  USAGE_GATE: pass  five_hour=NN seven_day=NN
#   1  USAGE_GATE: defer reason=tight ...
#   2  USAGE_GATE: defer reason=lookup-failed
#
# Thresholds (spec DGN-532 LOCKED v2, What--C):
#   five_hour >= 90  -> defer
#   seven_day >= 95  -> defer
#   lookup/parse fail -> defer (fail-CLOSED)
#
# 6-consecutive-defer counter:
#   State file: .telegram_bot/state/usage-gate-defer.count
#   On each defer: increment; if count reaches 6 print PUSH_SIGNAL once and reset to 0.
#   On pass: reset to 0.
#   Caller is responsible for acting on PUSH_SIGNAL; this script never fires push itself.
#
# NOTE: set -x is FORBIDDEN -- claude-usage.sh exposes tokens and this wrapper
#       inherits its output stream. Never enable execution tracing here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"

USAGE_SCRIPT="$SCRIPT_DIR/claude-usage.sh"
DEFER_COUNT_FILE="$WORKSPACE/.telegram_bot/state/usage-gate-defer.count"

# Env-overridable caps (CTO-loop trial, DGN-532). Defaults preserve prior behavior.
THRESHOLD_FIVE_HOUR="${LOOP_CAP_FIVE_HOUR:-90}"
THRESHOLD_SEVEN_DAY="${LOOP_CAP_SEVEN_DAY:-95}"
PUSH_SIGNAL_AT=6

# --- helpers ---

_read_defer_count() {
  if [[ -f "$DEFER_COUNT_FILE" ]]; then
    local v
    v=$(cat "$DEFER_COUNT_FILE" 2>/dev/null || echo "0")
    v="${v//[^0-9]/}"  # strip non-digits
    echo "${v:-0}"
  else
    echo "0"
  fi
}

_write_defer_count() {
  local n="$1"
  mkdir -p "$(dirname "$DEFER_COUNT_FILE")"
  printf '%s\n' "$n" > "$DEFER_COUNT_FILE"
}

_on_defer() {
  local reason="$1"
  shift
  local extra="${*:-}"

  local count
  count=$(_read_defer_count)
  count=$(( count + 1 ))

  if (( count >= PUSH_SIGNAL_AT )); then
    echo "PUSH_SIGNAL: consecutive_defer_count=${count} threshold=${PUSH_SIGNAL_AT}"
    _write_defer_count 0
  else
    _write_defer_count "$count"
  fi

  if [[ -n "$extra" ]]; then
    echo "USAGE_GATE: defer reason=${reason} ${extra}"
  else
    echo "USAGE_GATE: defer reason=${reason}"
  fi
}

_on_pass() {
  local fh="$1"
  local sd="$2"
  _write_defer_count 0
  echo "USAGE_GATE: pass five_hour=${fh} seven_day=${sd}"
}

# --- run claude-usage.sh and capture output ---
# stderr suppressed: token errors from claude-usage.sh must not leak.

raw_output=""
if ! raw_output=$("$USAGE_SCRIPT" 2>/dev/null); then
  # claude-usage.sh exited nonzero -> lookup failed
  _on_defer "lookup-failed"
  exit 2
fi

# --- parse five_hour and seven_day percentages from output ---
#
# Output format (LOCALE=ko, default):
#   Claude 사용 한도
#   ─────...
#   5시간            <- five_hour section label
#   [████░] 12%      <- bar + percent (pctstr = rounded integer, no decimals)
#   리셋 ...
#   주간             <- seven_day section label
#   [████░] 32%
#   리셋 ...
#   [Fable ...]      <- optional per-model rows; ignored here
#   ─────...
#
# LOCALE=en labels: "5h" and "weekly". Same bar+percent format.
#
# Strategy: write the captured output to a tmpfile and pass its path to
# python3. Using pipe+heredoc simultaneously on python3 is not portable
# (the heredoc wins the stdin competition; pipe data is lost).

_tmpfile="$(mktemp /tmp/usage-gate-raw.XXXXXX)"
# cleanup on any exit
trap 'rm -f "$_tmpfile"' EXIT

printf '%s\n' "$raw_output" > "$_tmpfile"

_PARSE_SCRIPT='
import sys, re

path = sys.argv[1]
try:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
except Exception as e:
    print(f"PARSE_ERROR file_read={e}")
    sys.exit(1)

label_sets = {
    "five_hour": ["5시간", "5h"],
    "seven_day": ["주간", "weekly"],
}

results = {}
for key, labels in label_sets.items():
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in labels:
            if i + 1 < len(lines):
                m = re.search(r"(\d+)%", lines[i + 1])
                if m:
                    results[key] = int(m.group(1))
                    found = True
                    break
    if not found:
        results[key] = None

fh = results.get("five_hour")
sd = results.get("seven_day")

if fh is None or sd is None:
    print(f"PARSE_ERROR five_hour={fh} seven_day={sd}")
    sys.exit(1)

print(f"OK five_hour={fh} seven_day={sd}")
sys.exit(0)
'

parse_result=""
if ! parse_result=$(python3 -c "$_PARSE_SCRIPT" "$_tmpfile"); then
  _on_defer "lookup-failed" "detail=${parse_result:-parse_error}"
  exit 2
fi

# parse_result is "OK five_hour=NN seven_day=NN"
fh_pct=""
sd_pct=""
if [[ "$parse_result" =~ five_hour=([0-9]+) ]]; then
  fh_pct="${BASH_REMATCH[1]}"
fi
if [[ "$parse_result" =~ seven_day=([0-9]+) ]]; then
  sd_pct="${BASH_REMATCH[1]}"
fi

# fail-CLOSED: missing values -> defer
if [[ -z "$fh_pct" || -z "$sd_pct" ]]; then
  _on_defer "lookup-failed" "regex_extract_failed parse=${parse_result}"
  exit 2
fi

# --- threshold check ---

if (( fh_pct >= THRESHOLD_FIVE_HOUR )); then
  _on_defer "tight" "five_hour=${fh_pct} seven_day=${sd_pct} trigger=five_hour"
  exit 1
fi

if (( sd_pct >= THRESHOLD_SEVEN_DAY )); then
  _on_defer "tight" "five_hour=${fh_pct} seven_day=${sd_pct} trigger=seven_day"
  exit 1
fi

# --- pass ---

_on_pass "$fh_pct" "$sd_pct"
exit 0
