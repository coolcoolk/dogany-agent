#!/usr/bin/env bash
# test-lane: fast
# test-dgn1301-fable-death-catch.sh -- PostToolUse fable-death-catch.py writer.
#
# DGN-1301 (DGN-606 backport): the main-session PostToolUse hook that catches
# a Fable heavy-dispatch runtime death on the weekly usage cap and writes the
# .claude/.fable-exhausted marker for the usage-gate pre-flight redirect.
#
# Pins:
#   - Trigger: Agent/Task + model=fable + death signature -> flag written,
#     Fable->Opus directive emitted; anything else passes through silently.
#   - Path anchor: flag + cache resolve from the SCRIPT location (workspace
#     root /.claude), NEVER from the payload cwd -- a cwd-derived path would
#     split the marker across .claude dirs (DGN-835 path anchor).
#   - Expiry contract: .usage-cache.json seven_day_resets_at when present,
#     now+3d fallback -- same schema as dispatch-detached.sh's
#     write_fable_exhausted_marker (single marker, one schema).
#   - Reader agreement: usage-gate.py fable_exhausted() accepts the flag.
#   - T9-parity flag states (DGN-1158): absent / valid / expired / corrupted
#     pre-existing flag -> writer overwrites, flag ends valid.
#   - Fail-open: garbage stdin exits 0.
#
# Run: bash routines/tests/test-dgn1301-fable-death-catch.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: scripts copied into a mktemp scratch workspace; no live
# .claude state touched, no network, no spawn.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1301-test.XXXXXX)"
WS="$TDIR/ws"
trap 'rm -rf "$TDIR"' EXIT

# Scratch workspace: hook + usage-gate reader anchored to WS, decoy cwd dir.
mkdir -p "$WS/routines/lib" "$TDIR/decoy-cwd"
cp "$REPO/routines/fable-death-catch.py" "$WS/routines/"
cp "$REPO/routines/usage-gate.py" "$WS/routines/"
cp "$REPO/routines/lib/conf_reader.py" "$WS/routines/lib/" 2>/dev/null || true

HOOK="/usr/bin/python3 $WS/routines/fable-death-catch.py"
FLAG="$WS/.claude/.fable-exhausted"
CACHE="$WS/.claude/.usage-cache.json"

# payload <tool_name> <model> <response> [cwd]
payload() {
  python3 - "$1" "$2" "$3" "${4:-}" <<'PY'
import json, sys
tool, model, resp, cwd = sys.argv[1:5]
data = {"hook_event_name": "PostToolUse", "tool_name": tool,
        "tool_input": {"model": model, "prompt": "x"}, "tool_response": resp}
if cwd:
    data["cwd"] = cwd
print(json.dumps(data))
PY
}

# flag_valid: flag exists, parses, expiry in the future (writer's own contract).
flag_valid() {
  python3 - "$FLAG" <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    flag = json.load(open(sys.argv[1], encoding="utf-8"))
    exp = datetime.fromisoformat(str(flag["expiry"]).replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sys.exit(0 if exp > datetime.now(timezone.utc) else 1)
except Exception:
    sys.exit(1)
PY
}

# gate_reads_flag: the template usage-gate.py's own fable_exhausted() (imported
# from the scratch copy, so its STATE_DIR anchors to WS) accepts the flag.
# NOTE: fable_exhausted() DELETES an expired/stale flag as a side effect.
gate_reads_flag() {
  python3 - "$WS/routines" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ug", sys.argv[1] + "/usage-gate.py")
ug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ug)
sys.exit(0 if ug.fable_exhausted() else 1)
PY
}

echo "T1 trigger: fable death -> flag + directive"
rm -f "$FLAG" "$CACHE"
OUT="$(payload Task fable "Claude usage limit reached: You've hit your limit." | $HOOK)"
RC=$?
[[ $RC -eq 0 ]] && ok "T1 exit 0" || bad "T1 exit 0 (got $RC)"
echo "$OUT" | grep -q "additionalContext" && ok "T1 directive emitted" || bad "T1 directive emitted"
echo "$OUT" | grep -q "model: opus" && ok "T1 directive names opus re-fire" || bad "T1 directive names opus re-fire"
flag_valid && ok "T1 flag written, valid future expiry" || bad "T1 flag written, valid future expiry"
grep -q '"source": "fable-death-catch"' "$FLAG" && ok "T1 flag source stamped" || bad "T1 flag source stamped"

echo "T2 reader agreement: usage-gate fable_exhausted() accepts the flag"
gate_reads_flag && ok "T2 usage-gate reads writer's flag" || bad "T2 usage-gate reads writer's flag"

echo "T3 path anchor: payload cwd is IGNORED (script-anchored .claude)"
rm -f "$FLAG"
payload Task fable "You've hit your limit." "$TDIR/decoy-cwd" | $HOOK > /dev/null
[[ -f "$FLAG" ]] && ok "T3 flag at workspace root" || bad "T3 flag at workspace root"
[[ ! -e "$TDIR/decoy-cwd/.claude/.fable-exhausted" ]] && ok "T3 no flag under payload cwd" || bad "T3 no flag under payload cwd"

echo "T4 expiry source: cache seven_day_resets_at wins, else now+3d"
rm -f "$FLAG"
RESETS="2099-01-02T03:04:05+00:00"
printf '{"seven_day_resets_at": "%s"}' "$RESETS" > "$CACHE"
payload Agent fable "usage limit reached" | $HOOK > /dev/null
grep -q "$RESETS" "$FLAG" && ok "T4 expiry = cache seven_day_resets_at" || bad "T4 expiry = cache seven_day_resets_at"
rm -f "$FLAG" "$CACHE"
payload Agent fable "usage limit reached" | $HOOK > /dev/null
python3 - "$FLAG" <<'PY' && ok "T4 fallback expiry ~ now+3d" || bad "T4 fallback expiry ~ now+3d"
import json, sys
from datetime import datetime, timezone
exp = datetime.fromisoformat(json.load(open(sys.argv[1]))["expiry"])
delta = (exp - datetime.now(timezone.utc)).total_seconds()
sys.exit(0 if 2 * 86400 < delta <= 3 * 86400 else 1)
PY

echo "T5 pass-through: non-fable / non-heavy / non-death never write"
rm -f "$FLAG"
OUT="$(payload Task opus "You've hit your limit." | $HOOK)"
[[ -z "$OUT" && ! -e "$FLAG" ]] && ok "T5 non-fable model ignored" || bad "T5 non-fable model ignored"
OUT="$(payload Bash fable "You've hit your limit." | $HOOK)"
[[ -z "$OUT" && ! -e "$FLAG" ]] && ok "T5 non-heavy tool ignored" || bad "T5 non-heavy tool ignored"
OUT="$(payload Task fable "Task complete: discussed rate limits in the abstract." | $HOOK)"
[[ -z "$OUT" && ! -e "$FLAG" ]] && ok "T5 non-death response ignored" || bad "T5 non-death response ignored"

echo "T6 flag states (T9 parity): absent / valid / expired / corrupted"
# Case 1: absent -> writer creates a valid flag.
rm -f "$FLAG"
payload Task fable "You've hit your limit." | $HOOK > /dev/null
flag_valid && ok "T6A absent -> valid flag written" || bad "T6A absent -> valid flag written"
# Case 2: valid future-expiry flag -> overwritten, stays valid.
python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)+d.timedelta(hours=1)).isoformat()}, open('$FLAG','w'))"
payload Task fable "You've hit your limit." | $HOOK > /dev/null
flag_valid && grep -q fable-death-catch "$FLAG" && ok "T6B valid -> overwritten, stays valid" || bad "T6B valid -> overwritten, stays valid"
# Case 3: expired flag -> overwritten, becomes valid again.
python3 -c "import json,datetime as d; json.dump({'expiry':(d.datetime.now(d.timezone.utc)-d.timedelta(days=1)).isoformat()}, open('$FLAG','w'))"
payload Task fable "You've hit your limit." | $HOOK > /dev/null
flag_valid && ok "T6C expired -> overwritten, becomes valid" || bad "T6C expired -> overwritten, becomes valid"
# Case 4: corrupted / unparseable JSON -> overwritten, becomes valid.
printf 'not json{{{' > "$FLAG"
payload Task fable "You've hit your limit." | $HOOK > /dev/null
flag_valid && ok "T6D corrupted -> overwritten, becomes valid" || bad "T6D corrupted -> overwritten, becomes valid"
rm -f "$FLAG"

echo "T7 fail-open: garbage stdin exits 0, writes nothing"
printf 'not json at all' | $HOOK > /dev/null 2>&1
RC=$?
[[ $RC -eq 0 && ! -e "$FLAG" ]] && ok "T7 garbage stdin fail-open" || bad "T7 garbage stdin fail-open (rc=$RC)"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
