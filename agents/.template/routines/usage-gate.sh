#!/usr/bin/env bash
# usage-gate.sh -- Claude usage window gate for autonomous-loop spawn decisions.
# Wrapper around claude-usage.sh output only [F2]: NO credential/keychain access.
#
# Exit codes:
#   0  USAGE_GATE: pass  five_hour=NN seven_day=NN
#   1  USAGE_GATE: defer reason=tight ...
#   2  USAGE_GATE: defer reason=lookup-failed cause="<why>"  (DGN-1590)
#
# Thresholds (spec DGN-532 LOCKED v2, What--C):
#   five_hour >= 90  -> defer
#   seven_day >= 95  -> defer  (waived for the owner-driven live session when
#                              USAGE_GATE_7D=auto-only -- DGN-925 parity with
#                              usage-gate.py; the decision is delegated to it)
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

# DGN-1706: a failed LOOKUP (e.g. HTTP 429 on the usage endpoint) is not an
# exhausted WINDOW. Before deferring, fall back to the usage-gate.py TTL cache
# when it is recent: that is still a measured value, so the fail-closed rule
# ("an unknown window is never approvable") is kept -- only a truly unknown
# window (no cache, or cache older than CACHE_FALLBACK_MAX_SEC) defers.
CACHE_FALLBACK_MAX_SEC="${USAGE_GATE_CACHE_FALLBACK_MAX_SEC:-900}"
_cached_parse() {
  python3 - "$WORKSPACE/.claude/.usage-cache.json" "$CACHE_FALLBACK_MAX_SEC" <<'PY' 2>/dev/null
import json, os, sys, time
path, max_age = sys.argv[1], float(sys.argv[2])
try:
    age = time.time() - os.path.getmtime(path)
    if age > max_age:
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    fh = int(float(d["five_hour_util"]))
    sd = int(float(d["seven_day_util"]))
except Exception:
    sys.exit(1)
print("OK five_hour=%d seven_day=%d cache_age=%d" % (fh, sd, int(age)))
PY
}

# DGN-1590: the failure cause (HTTP status / curl error / token state) is on
# claude-usage.sh's stdout as its "[Live Rate-Limit] <cause>" line. Keep just
# that one line (single line, capped) so a recurrence stays diagnosable; never
# the whole output. Best-effort: cause extraction must not change the gate rc.
_lookup_cause() {
  local c
  c=$(printf '%s\n' "$raw_output" | grep -m1 -F '[Live Rate-Limit]' || true)
  if [[ -z "$c" ]]; then
    c=$(printf '%s\n' "$raw_output" | grep -m1 '[[:alnum:]]' || true)
  fi
  c="${c#*\[Live Rate-Limit\]}"
  c=$(printf '%s' "$c" | tr -s '[:space:]"' ' ' | cut -c1-200)
  c="${c# }"; c="${c% }"
  printf '%s' "${c:-no-output}"
}

raw_output=""
parse_result=""
if ! raw_output=$("$USAGE_SCRIPT" 2>/dev/null); then
  # claude-usage.sh exited nonzero -> lookup failed; recent cache or defer
  lookup_cause="$(_lookup_cause)"
  if parse_result="$(_cached_parse)"; then
    echo "USAGE_GATE: note lookup-failed, using cached window (${parse_result#OK }) cause=\"${lookup_cause}\"" >&2
  else
    _on_defer "lookup-failed" "cause=\"${lookup_cause}\""
    exit 2
  fi
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

if [[ -z "$parse_result" ]] && ! parse_result=$(python3 -c "$_PARSE_SCRIPT" "$_tmpfile"); then
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

# Owner one-shot approval sentinel (DGN-1148).
# usage-gate.py already implements this contract and its own deny message tells
# the owner to "touch <cwd>/.claude/.usage-gate-approved then retry" -- but that
# path was never wired here, so the DEFAULT dispatch route (dispatch-detached.sh
# calls THIS file, not the .py hook) could not honour an approval the owner had
# already granted. Existence != wiring; this closes that seam.
#
# Semantics are copied from usage-gate.py, not relaxed:
#   - the sentinel body is an integer COUNT of approved dispatches (DGN-835):
#     empty file / bare `touch` = 1, `echo N >` = N. Each consume decrements by
#     one and the file is removed at zero, with the original mtime preserved so
#     the TTL window never silently extends. A plain `rm -f` here would cancel
#     the remaining N-1 approvals of an owner-approved fan-out -- the .py hook
#     and this one MUST decide identically or the same approval means two
#     different things depending on which entry point ran.
#   - TTL 600s: a stale sentinel is ignored (and left in place for the .py hook
#     to reap; we never treat an expired file as approval)
#   - it waives the THRESHOLD branch only. lookup-failed stays fail-CLOSED --
#     an unknown window is never approvable, because the owner cannot have
#     approved a cost nobody measured.
# Path anchor: $WORKSPACE (SCRIPT_DIR/..), matching usage-gate.py's STATE_DIR --
# never a cwd-derived path, which would split the state across .claude dirs.
SENTINEL_FILE="$WORKSPACE/.claude/.usage-gate-approved"
SENTINEL_TTL_SEC=600

_consume_fresh_sentinel() {
  [[ -f "$SENTINEL_FILE" ]] || return 1
  local mtime now age
  mtime=$(python3 -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$SENTINEL_FILE" 2>/dev/null) || return 1
  now=$(python3 -c 'import time; print(int(time.time()))')
  age=$(( now - mtime ))
  (( age <= SENTINEL_TTL_SEC )) || return 1
  # consume ONE unit (count semantics above); any IO failure = not approved.
  python3 - "$SENTINEL_FILE" <<'PY' || return 1
import os, sys

path = sys.argv[1]
try:
    st = os.stat(path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
        count = int(raw) if raw else 1
    except (OSError, ValueError):
        count = 1
    if count <= 1:
        os.remove(path)
    else:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(count - 1))
        os.replace(tmp, path)
        os.utime(path, (st.st_atime, st.st_mtime))
except OSError:
    sys.exit(1)
PY
  return 0
}


# --- DGN-925 parity: the 7d block is waived for the owner-driven live session
#     when USAGE_GATE_7D=auto-only. The .py hook has honoured this since
#     DGN-925; this entry point did not, so the SAME dispatch was allowed or
#     refused depending on which gate ran first -- and the detached-dispatch
#     path runs THIS one. One axis must not have two answers.
#
#     The decision is DELEGATED to usage-gate.py rather than reimplemented:
#     re-deriving "is this session interactive" in shell is exactly how the two
#     gates drifted apart in the first place. Any failure here (missing module,
#     import error, unexpected output) leaves apply_7d=1 -- fail-CLOSED, the
#     stricter answer.
apply_7d=1
_7d_probe="$(python3 - "$SCRIPT_DIR/usage-gate.py" "$WORKSPACE" <<'PY' 2>/dev/null
import importlib.util, sys
try:
    spec = importlib.util.spec_from_file_location("_ug", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    waived = mod.gate_7d_auto_only(sys.argv[2]) and mod.session_is_interactive()
    print("WAIVE" if waived else "APPLY")
except Exception:
    print("APPLY")
PY
)"
[[ "$_7d_probe" == "WAIVE" ]] && apply_7d=0
# Never waive silently: if the waiver is what let this dispatch through, say so.
# stderr, so the stdout contract ("USAGE_GATE: pass ...") that callers parse is
# unchanged.
if (( apply_7d == 0 )) && (( sd_pct >= THRESHOLD_SEVEN_DAY )); then
  echo "USAGE_GATE: 7d block waived (USAGE_GATE_7D=auto-only, owner-driven live session) seven_day=${sd_pct}" >&2
fi

# --- threshold check ---

if (( fh_pct >= THRESHOLD_FIVE_HOUR )); then
  if _consume_fresh_sentinel; then
    _write_defer_count 0
    echo "USAGE_GATE: pass five_hour=${fh_pct} seven_day=${sd_pct} owner_approved=one-shot trigger=five_hour"
    exit 0
  fi
  _on_defer "tight" "five_hour=${fh_pct} seven_day=${sd_pct} trigger=five_hour"
  exit 1
fi

if (( apply_7d == 1 && sd_pct >= THRESHOLD_SEVEN_DAY )); then
  if _consume_fresh_sentinel; then
    _write_defer_count 0
    echo "USAGE_GATE: pass five_hour=${fh_pct} seven_day=${sd_pct} owner_approved=one-shot trigger=seven_day"
    exit 0
  fi
  _on_defer "tight" "five_hour=${fh_pct} seven_day=${sd_pct} trigger=seven_day"
  exit 1
fi

# --- pass ---

_on_pass "$fh_pct" "$sd_pct"
exit 0
