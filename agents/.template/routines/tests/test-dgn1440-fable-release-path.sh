#!/usr/bin/env bash
# test-lane: fast
# test-dgn1440-fable-release-path.sh -- auto-release for a stale/false-positive
# .fable-exhausted marker, reconciled against MEASURED Fable-scoped utilization
# on the very next dispatch (no wait for the up-to-4-day expiry, no manual step).
#
# DGN-1440: a marker written on a runtime death (fable-death-catch.py /
# dispatch-detached.sh) had no release path before its self-computed/cached
# expiry -- a false positive (death misread as quota exhaustion) sealed a
# perfectly healthy Fable window for up to ~4 days. usage-gate.py already owns
# the flag's read-time semantics (fable_exhausted()); this pins the added
# reconciliation there: a flag is HONORED only while corroborated by a fresh,
# LOW-utilization-contradicting measurement of the Fable-scoped weekly limit
# (parsed from claude-usage.sh --json's "limits" array, the same payload
# fetch_usage() already pulls for the account-wide 5h/7d numbers -- no new
# network call, no new cache file).
#
# Covers:
#   T1  flag present + measured Fable-scoped pct LOW  -> auto-released, dispatch
#       allowed, flag file removed (false-positive self-heals same-turn).
#   T2  flag present + measured Fable-scoped pct HIGH -> flag stands, dispatch
#       still redirected to opus (a real cap is never waived by this path).
#   T3  flag present + usage fetch fails / no Fable-scoped entry in the
#       response -> conservative: flag stands (never release on missing
#       evidence).
#   T4  no flag -> unaffected (silent allow either way; this is a reconcile
#       path, not a new gate).
#
# Safe/offline: stubbed claude-usage.sh, no real API call, no live state.

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
check() {
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got='$2' want='$3')"; fi
}

TDIR="$(mktemp -d /tmp/dgn1440-release.XXXXXX)"
trap 'rm -rf "$TDIR"' EXIT

mkdir -p "$TDIR/routines" "$TDIR/.claude" "$TDIR/cwd/config"
cp "$REPO/routines/usage-gate.py" "$TDIR/routines/usage-gate.py"

GATE="$TDIR/routines/usage-gate.py"
CWD="$TDIR/cwd"
# usage-gate.py anchors ALL state (cache/flag) to <script dir>/../.claude
# (DGN-835 path anchor), NOT the payload cwd -- match that here, or the
# fixture writes to a path the gate never reads (silently green/red-proof).
STATE="$TDIR/.claude"
CACHE="$STATE/.usage-cache.json"
FLAG="$STATE/.fable-exhausted"
CONF="$CWD/config/agent.conf"
printf 'AGENT_LANG=en\nPLAN=max_20x\n' > "$CONF"

# stub_usage FABLE_PCT|omit -- claude-usage.sh stub. FH/7d stay well under
# threshold so only the flag (not the generic threshold path) can trigger a
# deny. "omit" drops the Fable-scoped entry from "limits" entirely (T3).
stub_usage() {
  local pct="$1"
  local limits_json="[]"
  if [[ "$pct" != "omit" ]]; then
    limits_json="[{\"kind\":\"weekly_model\",\"scope\":{\"model\":{\"display_name\":\"Fable\"}},\"percent\":${pct},\"resets_at\":\"2099-01-01T00:00:00Z\"}]"
  fi
  cat > "$TDIR/routines/claude-usage.sh" <<EOS
#!/usr/bin/env bash
[[ "\${STUB_FAIL:-0}" == "1" ]] && exit 1
cat <<EOF
{"five_hour":{"utilization":0,"resets_at":"2026-01-01T00:00:00Z"},
 "seven_day":{"utilization":0,"resets_at":"2026-01-01T00:00:00Z"},
 "limits":${limits_json}}
EOF
EOS
  chmod +x "$TDIR/routines/claude-usage.sh"
}

write_flag() {
  python3 - "$FLAG" <<'PY'
import json, sys, datetime as d
json.dump({
    "expiry": (d.datetime.now(d.timezone.utc) + d.timedelta(days=3)).isoformat(),
    "recorded_at": (d.datetime.now(d.timezone.utc) - d.timedelta(hours=1)).isoformat(),
    "source": "fable-death-catch",
}, open(sys.argv[1], "w"))
PY
}

# run_gate MODEL -> G_OUT G_RC G_DEC
run_gate() {
  local model="$1"
  local payload
  payload="$(printf '{"tool_name":"Agent","tool_input":{"model":"%s"},"cwd":"%s"}' "$model" "$CWD")"
  G_OUT="$(printf '%s' "$payload" | python3 "$GATE" 2>/dev/null)"
  G_RC=$?
  if [[ -z "$G_OUT" ]]; then
    G_DEC="silent"
  else
    G_DEC="$(printf '%s' "$G_OUT" | python3 -c \
      'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecision"])' \
      2>/dev/null || echo "unparseable")"
  fi
}

echo "T1 flag present + measured Fable pct LOW -> auto-released, allow, flag removed"
rm -f "$FLAG" "$CACHE"
stub_usage 3
write_flag
run_gate fable
check "T1 low measured pct -> silent allow" "$G_DEC" "silent"
[[ ! -e "$FLAG" ]] && ok "T1 flag removed on reconcile" || bad "T1 flag removed on reconcile"

echo "T2 flag present + measured Fable pct HIGH -> flag stands, still redirected"
rm -f "$FLAG" "$CACHE"
stub_usage 97
write_flag
run_gate fable
check "T2 high measured pct -> deny (redirect)" "$G_DEC" "deny"
echo "$G_OUT" | grep -q "model: opus" && ok "T2 redirect names opus re-fire" || bad "T2 redirect names opus re-fire ($G_OUT)"
[[ -e "$FLAG" ]] && ok "T2 flag NOT removed (real cap stands)" || bad "T2 flag NOT removed (real cap stands)"

echo "T3 flag present + no corroborating measurement -> conservative, flag stands"
rm -f "$FLAG" "$CACHE"
stub_usage omit
write_flag
run_gate fable
check "T3 no Fable-scoped entry -> deny (redirect)" "$G_DEC" "deny"
[[ -e "$FLAG" ]] && ok "T3 flag NOT removed (no evidence to release on)" || bad "T3 flag NOT removed (no evidence to release on)"

rm -f "$FLAG" "$CACHE"
stub_usage 3
STUB_FAIL=1
write_flag
G_OUT="$(printf '{"tool_name":"Agent","tool_input":{"model":"fable"},"cwd":"%s"}' "$CWD" | STUB_FAIL=1 python3 "$GATE" 2>/dev/null)"
if [[ -z "$G_OUT" ]]; then G_DEC="silent"; else
  G_DEC="$(printf '%s' "$G_OUT" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecision"])' \
    2>/dev/null || echo "unparseable")"
fi
check "T3b fetch failure -> deny (redirect)" "$G_DEC" "deny"
[[ -e "$FLAG" ]] && ok "T3b flag NOT removed (fetch failed, no evidence)" || bad "T3b flag NOT removed (fetch failed, no evidence)"

echo "T4 no flag -> unaffected (reconcile path is a no-op)"
rm -f "$FLAG" "$CACHE"
stub_usage 3
run_gate fable
check "T4 no flag, cold -> silent allow" "$G_DEC" "silent"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
