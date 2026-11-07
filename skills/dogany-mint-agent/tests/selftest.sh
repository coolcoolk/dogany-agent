#!/bin/bash
# test-lane: fast    # measured 0.5s (2026-09-16, DGN-1509 backup-tests dispatch)
# selftest.sh -- mint-agent module self-test (DGN-243). Sandbox-only:
# everything runs against synthetic data under a mktemp dir. Never touches
# live instance state, never creates an agent instance, never calls Telegram.
#
# DGN-1267 note: this dir also holds test_policy.py/test_reap.py, whose names
# match the PYTEST-class glob (tests/test_*.py) but which carry no pytest
# test_* functions -- see their own marker comments. Without a marker HERE
# too, run-tests.sh's SUITE/PYTEST split leaves this file UNLANED once the
# other two declare (a mixed tests/ dir is resolved file-by-file, not as a
# unit). This marker is what makes run-tests.sh actually execute this file.
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$TESTS_DIR/.." && pwd)"
SCRIPTS="$SKILL_DIR/scripts"
PY=python3
FAIL=0
step() { echo; echo "=== $1 ==="; }
assert() { # assert <name> <expr...>
  local name="$1"; shift
  if "$@"; then echo "PASS  $name"; else echo "FAIL  $name"; FAIL=1; fi
}

SANDBOX="$(mktemp -d /tmp/mint-agent-selftest.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
echo "[selftest] sandbox = $SANDBOX"

step "0. syntax"
assert "bash -n v2-ctl.sh" bash -n "$SCRIPTS/v2-ctl.sh"
assert "py_compile all" "$PY" -m py_compile \
  "$SCRIPTS/minting_policy.py" "$SCRIPTS/weekly_eval.py" "$SCRIPTS/minting_state.py"
assert "domains.json valid" "$PY" -c "import json;json.load(open('$SKILL_DIR/domains.json'))"

step "1. policy unit tests (synthetic)"
"$PY" "$TESTS_DIR/test_policy.py" || FAIL=1

step "2. weekly_eval end-to-end (synthetic transcripts + sqlite + state)"
TDIR="$SANDBOX/transcripts"; mkdir -p "$TDIR"
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# 10 user messages, 3 health-flavored (share 0.30 >= 0.20 -> fire).
{
  for i in 1 2 3; do
    printf '{"type":"user","timestamp":"%s","message":{"content":"오늘 운동 등 당기기 했어 단백질 얼마나 먹을까"}}\n' "$NOW_ISO"
  done
  for i in 1 2 3 4 5 6 7; do
    printf '{"type":"user","timestamp":"%s","message":{"content":"내일 회의 일정 잡아줘 %d"}}\n' "$NOW_ISO" "$i"
  done
  printf '{"type":"user","isSidechain":true,"timestamp":"%s","message":{"content":"운동 사이드체인은 제외되어야 함"}}\n' "$NOW_ISO"
  printf '{"type":"user","timestamp":"%s","message":{"content":"<system-reminder>skip me 운동</system-reminder>"}}\n' "$NOW_ISO"
  printf '{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"text","text":"운동 assistant msg excluded"}]}}\n' "$NOW_ISO"
} > "$TDIR/session1.jsonl"
DB="$SANDBOX/kit_life.db"
sqlite3 "$DB" "CREATE TABLE meals(id INTEGER); CREATE TABLE workouts(id INTEGER);
CREATE TABLE metric_log(id INTEGER);
INSERT INTO meals SELECT value FROM generate_series(1,20);
INSERT INTO workouts SELECT value FROM generate_series(1,8);
INSERT INTO metric_log SELECT value FROM generate_series(1,2);" 2>/dev/null || {
  # generate_series may be unavailable on old sqlite -- fall back to a loop.
  sqlite3 "$DB" "DROP TABLE IF EXISTS meals; DROP TABLE IF EXISTS workouts; DROP TABLE IF EXISTS metric_log;
  CREATE TABLE meals(id INTEGER); CREATE TABLE workouts(id INTEGER); CREATE TABLE metric_log(id INTEGER);"
  for i in $(seq 1 20); do sqlite3 "$DB" "INSERT INTO meals VALUES($i);"; done
  for i in $(seq 1 8);  do sqlite3 "$DB" "INSERT INTO workouts VALUES($i);"; done
  for i in $(seq 1 2);  do sqlite3 "$DB" "INSERT INTO metric_log VALUES($i);"; done
}
STATE="$SANDBOX/minting-state.json"
EVAL=("$PY" "$SCRIPTS/weekly_eval.py" --agent-dir "$SANDBOX" \
      --transcripts-dir "$TDIR" --db "$DB" --state "$STATE")

# 2a. dry-run: decision JSON, no state write.
DRY_OUT="$("${EVAL[@]}" --dry-run)"
echo "$DRY_OUT" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
w=d["winner"]
assert w and w["domain"]=="health" and w["rec_no"]==1, w
m=d["metrics"]["health"]
assert abs(m["share"]-0.30)<1e-9, m
assert m["records"]==30, m
print("PASS  dry-run: health fires (share 0.30, records 30)")' || FAIL=1
assert "dry-run wrote no state" test ! -f "$STATE"

# 2b. real run: Korean block + fixed cost wording verbatim + state write.
BLOCK="$("${EVAL[@]}")"
echo "$BLOCK" | head -2
assert "block has header" grep -q '^\[전담 에이전트 추천\]' <<<"$BLOCK"
assert "cost wording verbatim" grep -qF '단기적으로 토큰 소모량이 다소 늘 수 있으나, 컨텍스트가 분리되어 인지 비용이 줄고 장기적·구조적으로 토큰 소모량이 줄도록 설계되어 있습니다.' <<<"$BLOCK"
assert "state recorded 1 rec" "$PY" -c "
import json;s=json.load(open('$STATE'))
assert len(s['domains']['health']['recommendations'])==1, s"

# 2c. same-day re-run: block repeats, state NOT duplicated.
BLOCK2="$("${EVAL[@]}")"
assert "same-day refire prints block" grep -q '^\[전담 에이전트 추천\]' <<<"$BLOCK2"
assert "same-day refire no dup state" "$PY" -c "
import json;s=json.load(open('$STATE'))
assert len(s['domains']['health']['recommendations'])==1, s"

# 2d. decline -> cooldown silences next weekly fire.
"$PY" "$SCRIPTS/minting_state.py" decline health --state "$STATE" >/dev/null
OUT3="$("${EVAL[@]}" --today "$(date -v+7d +%F 2>/dev/null || date -d '+7 days' +%F)")"
assert "post-decline +1w = silent" test -z "$OUT3"

# 2e. after cooldown, base metrics (0.30 share) do NOT clear strict 1.5x bar.
OUT4="$("${EVAL[@]}" --today "$(date -v+40d +%F 2>/dev/null || date -d '+40 days' +%F)")"
assert "post-cooldown share 0.30 not > 0.30 = silent" test -z "$OUT4"

# 2f. accept -> dedicated agent -> permanent quiet even at high metrics.
# DGN-1505 (landed into this bundle today by DGN-1513): accept now requires
# --path <dir> (the actual landing directory, an existence-check anchor) --
# pass the sandbox itself as a synthetic stand-in, same idiom as the
# DGN-1505 payload's own verification selftest (files/landing/
# DGN-1505-accept-path/verification/selftest.sh.with-dgn1505-path-tests:115).
"$PY" "$SCRIPTS/minting_state.py" accept health --agent worg --path "$SANDBOX" --state "$STATE" >/dev/null
OUT5="$("${EVAL[@]}" --today "$(date -v+80d +%F 2>/dev/null || date -d '+80 days' +%F)" --dry-run)"
echo "$OUT5" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
h=[x for x in d["decisions"] if x["domain"]=="health"][0]
assert h["reason"]=="dedicated-agent-exists", h
print("PASS  minted domain never re-recommended")' || FAIL=1

step "3. minting_state.py CRUD (synthetic)"
S2="$SANDBOX/state2.json"
"$PY" "$SCRIPTS/minting_state.py" decline finance --state "$S2" | grep -q "cooldown until" \
  && echo "PASS  1st decline -> cooldown stance" || { echo "FAIL  decline stance"; FAIL=1; }
"$PY" "$SCRIPTS/minting_state.py" decline finance --state "$S2" | grep -q "silenced permanently" \
  && echo "PASS  2nd decline -> permanent silence" || { echo "FAIL  silence stance"; FAIL=1; }
"$PY" "$SCRIPTS/minting_state.py" silence travel --state "$S2" >/dev/null
"$PY" "$SCRIPTS/minting_state.py" show --state "$S2" --json | "$PY" -c '
import json,sys
s=json.load(sys.stdin)
assert s["domains"]["travel"]["silenced"] is True
assert len(s["domains"]["finance"]["declines"])==2
print("PASS  show --json roundtrip")' || FAIL=1

# DGN-1513 G6 (deferred from v1): steps 4/4b/6 used to `cp "$SCRIPTS/mint_run.sh"`
# and --dry-run it -- $SCRIPTS was this bundle's OWN mint_run.sh
# (skills/dogany-mint-agent/scripts/mint_run.sh), the fork DGN-1471 retired (see
# files/landing/DGN-1471-fork-retire/, scripts/tests/test-mint-fork-retire.sh).
# That file no longer exists in this tree, so the three steps were dead --
# `bash -n`/`bash "$SCRIPTS/mint_run.sh"` failed on "No such file", not on the
# behavior they meant to check. Deleted rather than re-aimed at the canonical
# scripts/pack/mint_run.sh: that script requires an explicit --instance-root
# for every instance-relative step (DGN-366 L1 -- "no script-location
# derivation of the instance root"), while these fixtures relied on exactly
# that derivation (mint_run.sh copied INTO a fake skill tree, journal path
# computed 4 dirs up from its own location, no --instance-root passed). A
# faithful re-aim is a new fixture, not a retarget, so it is left undone here
# rather than shipped as a re-aim that quietly changes what it covers.
# mint mode (former step 4) and teardown/transaction flows already have
# canonical coverage: scripts/tests/test-mint-run-source-tag.sh,
# scripts/tests/test-mint-teardown.sh. NOT yet covered anywhere against the
# canonical script: the `start` deferral-manifest plan (former step 4b) and
# the `recover` crash-journal replay (former step 6) -- a real gap, flagged
# here rather than papered over; a future ticket should add
# scripts/tests/test-mint-run-start-defer.sh / -recover.sh against
# scripts/pack/mint_run.sh with --instance-root wired in.

step "4. v2-ctl.sh status (read-only -- conf and symlink must AGREE)"
# pre-activation: conf off + symlink absent; post-activation (owner GO
# 2026-07-10): conf on + symlink ACTIVE. Either coherent pair passes.
# DGN-1513 (landed today): v2-ctl.sh is new in this bundle and its status
# lines pad the label to a fixed column ("conf           : ", "symlink"
# + 8 spaces), not the narrower spacing these greps were originally written
# against -- [[:space:]]+ tolerates the column width instead of hard-coding
# it, so a future re-alignment doesn't silently re-break this check again.
STATUS="$(bash "$SCRIPTS/v2-ctl.sh" status)"
echo "$STATUS"
if grep -qE 'conf[[:space:]]+: on\b' <<<"$STATUS"; then
    assert "module state coherent (on)" grep -qE 'symlink[[:space:]]+: ACTIVE' <<<"$STATUS"
else
    assert "module state coherent (off)" grep -qE 'symlink[[:space:]]+: absent' <<<"$STATUS"
fi

# ---------------------------------------------------------------------------
echo
if [[ "$FAIL" -eq 0 ]]; then echo "SELFTEST: ALL PASS"; else echo "SELFTEST: FAILURES PRESENT"; exit 1; fi
