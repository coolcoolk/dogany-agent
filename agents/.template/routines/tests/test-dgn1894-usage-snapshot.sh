#!/usr/bin/env bash
# test-lane: fast
# test-dgn1894-usage-snapshot.sh -- one machine-wide usage snapshot.
#
# DGN-1894: usage-gate.py cached per WORKSPACE (<root>/.claude/.usage-cache.json)
# and claude-usage.sh had no cache at all, so N workspaces + direct pollers
# (a sibling agent's runner every 3 min, /usage, retry preflight) each hit
# GET /api/oauth/usage independently -> HTTP 429 -> every dispatch usage-gate
# deferred. The fix: ONE snapshot under ~/.dogany/usage (one account per macOS
# user), refreshed by a single lock holder at most once per TTL; every reader
# goes through claude-usage.sh, which serves it cache-first; a 429 starts a
# machine-wide back-off and the last snapshot keeps being served WITH its age.
#
# Covers:
#   T1  sequential calls from two workspaces inside the TTL -> one API call
#   T2  cold parallel fan-out (6 callers, slow API)           -> one API call
#   T3  usage-gate.py get_usage() in two workspaces           -> one API call
#   T4  TTL expired                                           -> refreshes
#   T5  429 on a stale-but-recent snapshot -> served, age disclosed, back-off
#       holds the next caller off the API entirely
#   T6  429 with no usable snapshot -> still a failure, cause kept (fail-closed
#       callers such as usage-gate.sh keep their contract)
#   T7  usage-gate.sh parses a snapshot-served report (age line is inert)
#   T8  usage-gate.py stamps the snapshot's real fetched_at, not "now"
#
# Safe/offline: curl is stubbed on PATH, HOME is a temp dir with a fake
# credential file. No real API call, no live state.

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HARNESS_DIR/.." && pwd)"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
check() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got='$2' want='$3')"; fi
}

TDIR="$(mktemp -d /tmp/dgn1894-snap.XXXXXX)"
trap 'rm -rf "$TDIR"' EXIT

export HOME="$TDIR/home"
mkdir -p "$HOME/.claude" "$TDIR/bin"
unset DOGANY_USAGE_SNAPSHOT_DIR DOGANY_USAGE_SNAPSHOT_TTL_SEC \
      DOGANY_USAGE_SNAPSHOT_STALE_MAX_SEC DOGANY_USAGE_BACKOFF_SEC
# valid (far-future) file credential -> the Keychain is never consulted
printf '{"claudeAiOauth":{"accessToken":"tok-fixture","expiresAt":4102444800000}}\n' \
  > "$HOME/.claude/.credentials.json"
SNAP="$HOME/.dogany/usage/snapshot.json"
CALLS="$TDIR/curl-calls.log"
: > "$CALLS"

# curl stub: one line per invocation; STUB_CODE / STUB_SLEEP / STUB_FH control it.
cat > "$TDIR/bin/curl" <<'EOS'
#!/usr/bin/env bash
cat >/dev/null  # drain the --config - header (never logged)
echo call >> "$STUB_CALLS"
[[ -n "${STUB_SLEEP:-}" ]] && sleep "$STUB_SLEEP"
code="${STUB_CODE:-200}"
if [[ "$code" == "200" ]]; then
  printf '{"five_hour":{"utilization":%s,"resets_at":"2099-01-01T00:00:00Z"},"seven_day":{"utilization":20,"resets_at":"2099-01-02T00:00:00Z"},"limits":[]}' "${STUB_FH:-10}"
else
  printf '{"error":{"type":"rate_limit_error"}}'
fi
printf '\n__HTTP_STATUS__:%s' "$code"
EOS
chmod +x "$TDIR/bin/curl"
export PATH="$TDIR/bin:$PATH"
export STUB_CALLS="$CALLS"

# two workspaces sharing one machine (same HOME)
for ws in A B; do
  mkdir -p "$TDIR/ws$ws/routines/lib" "$TDIR/ws$ws/.claude" "$TDIR/ws$ws/.telegram_bot/state"
  cp "$SRC/claude-usage.sh" "$SRC/usage-gate.py" "$SRC/usage-gate.sh" "$TDIR/ws$ws/routines/"
  cp "$SRC/lib/conf_reader.py" "$TDIR/ws$ws/routines/lib/" 2>/dev/null || true
done
UA="$TDIR/wsA/routines/claude-usage.sh"
UB="$TDIR/wsB/routines/claude-usage.sh"

calls() { wc -l < "$CALLS" | tr -d ' '; }
reset_all() { : > "$CALLS"; rm -rf "$HOME/.dogany/usage"; rm -f "$TDIR"/ws?/.claude/.usage-cache.json; }
age_snapshot() {  # age_snapshot SECONDS -- backdate the snapshot mtime
  python3 -c 'import os,sys,time; t=time.time()-int(sys.argv[2]); os.utime(sys.argv[1],(t,t))' "$SNAP" "$1"
}
jget() {  # jget KEYPATH <<< json
  python3 -c '
import json,sys
d=json.loads(sys.stdin.read())
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print(d)' "$1"
}

echo "T1 sequential, two workspaces, inside TTL"
reset_all
for i in 1 2 3; do bash "$UA" --json >/dev/null 2>&1; bash "$UB" --json >/dev/null 2>&1; done
check "T1 six reads -> one API call" "$(calls)" "1"
check "T1 snapshot lives under ~/.dogany/usage" "$([[ -s "$SNAP" ]] && echo yes)" "yes"

echo "T2 cold parallel fan-out"
reset_all
pids=()
for i in 1 2 3 4 5 6; do
  ( out=$(STUB_SLEEP=1 bash "$UA" --json 2>/dev/null) && [[ "$(jget five_hour.utilization <<< "$out")" == "10" ]] \
      && echo ok > "$TDIR/t2.$i" ) &
  pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done
check "T2 six parallel callers -> one API call" "$(calls)" "1"
check "T2 every caller got the numbers" "$(ls "$TDIR"/t2.* 2>/dev/null | wc -l | tr -d ' ')" "6"

echo "T3 usage-gate.py get_usage() in two workspaces"
reset_all
for ws in A B; do
  python3 - "$TDIR/ws$ws/routines/usage-gate.py" <<'PY' >/dev/null 2>&1
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ug", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m.get_usage() is not None
PY
done
check "T3 two workspace gates -> one API call" "$(calls)" "1"

echo "T4 TTL expiry refreshes"
reset_all
bash "$UA" --json >/dev/null 2>&1
age_snapshot 400
out=$(STUB_FH=33 bash "$UB" --json 2>/dev/null)
check "T4 expired snapshot -> second API call" "$(calls)" "2"
check "T4 fresh numbers served" "$(jget five_hour.utilization <<< "$out")" "33"
check "T4 source=live" "$(jget _snapshot.source <<< "$out")" "live"

echo "T5 429 serves the last snapshot with its age, then backs off"
reset_all
bash "$UA" --json >/dev/null 2>&1
age_snapshot 400
out=$(STUB_CODE=429 bash "$UA" --json 2>/dev/null); rc=$?
check "T5 429 + recent snapshot -> rc 0" "$rc" "0"
check "T5 last numbers served" "$(jget five_hour.utilization <<< "$out")" "10"
check "T5 marked stale" "$(jget _snapshot.source <<< "$out")" "stale"
a=$(jget _snapshot.age_sec <<< "$out")
check "T5 age disclosed (>=400s)" "$([[ "$a" =~ ^[0-9]+$ ]] && (( a >= 400 )) && echo yes)" "yes"
before=$(calls)
disp=$(STUB_CODE=200 LOCALE=en bash "$UB" 2>/dev/null); rc=$?
check "T5 back-off: next caller makes no API call" "$(calls)" "$before"
check "T5 back-off display rc 0" "$rc" "0"
check "T5 display discloses age" "$(grep -c 'ago' <<< "$disp")" "1"

echo "T6 429 with no usable snapshot stays a failure"
reset_all
out=$(STUB_CODE=429 bash "$UA" 2>/dev/null); rc=$?
check "T6 no snapshot + 429 -> rc 1" "$rc" "1"
check "T6 cause kept on the [Live Rate-Limit] line" "$(grep -c 'Live Rate-Limit.*429' <<< "$out")" "1"
reset_all
bash "$UA" --json >/dev/null 2>&1
age_snapshot 5000
STUB_CODE=429 bash "$UA" --json >/dev/null 2>&1; rc=$?
check "T6 snapshot older than stale cap + 429 -> rc 1" "$rc" "1"

echo "T7 usage-gate.sh on a snapshot-served report"
reset_all
bash "$UA" --json >/dev/null 2>&1
age_snapshot 400
gate=$(STUB_CODE=429 LOCALE=ko bash "$TDIR/wsA/routines/usage-gate.sh" 2>/dev/null)
check "T7 gate passes on the served snapshot" "$(grep -c '^USAGE_GATE: pass five_hour=10 seven_day=20' <<< "$gate")" "1"

echo "T8 usage-gate.py keeps the snapshot's real fetched_at"
reset_all
bash "$UA" --json >/dev/null 2>&1
age_snapshot 400
lag=$(STUB_CODE=429 python3 - "$TDIR/wsB/routines/usage-gate.py" <<'PY' 2>/dev/null
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("ug", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
u = m.get_usage()
print(int(time.time() - u["fetched_at"]))
PY
)
check "T8 fetched_at is ~400s old, not now" "$([[ "$lag" =~ ^[0-9]+$ ]] && (( lag >= 400 )) && echo yes)" "yes"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
