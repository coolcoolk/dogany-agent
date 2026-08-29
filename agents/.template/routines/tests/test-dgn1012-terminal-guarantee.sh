#!/usr/bin/env bash
# test-dgn1012-terminal-guarantee.sh -- the terminal-state guarantee is ARMED,
# REACHED, and reports STATE and CONTENT as two independent facts.
#
# This suite exists because "존재 != 배선됨 != 동작함". dispatch-detached.sh has
# carried DGN-1012 registration call sites since 5514fc64, and every one of them
# is `python3 "$SCRIPT_DIR/terminal-state-ledger.py" ... || true`. When the
# machine is absent the call exits 2, the `|| true` eats it, and the whole
# dispatch suite stays green (measured: 61/61 PASS while stderr printed
# "can't open file .../terminal-state-ledger.py" twice). A guarantee that can
# be missing without a single test going red is not a guarantee.
#
# Sections
#   1 ARMED     -- the machine exists, compiles, and answers its own CLI.
#   2 REACHED   -- the wrapper's exact argv actually lands a ledger row, and a
#                  SIGKILLed worker (the only signal that skips the EXIT trap --
#                  measured: TERM/INT/HUP all run it) is recovered by sweep.
#   3 SPLIT     -- lifecycle and product are separate fields. A run killed at
#                  the deadline that nonetheless produced commits must report
#                  the product, not just the kill (2026-08-28: exit=124, work
#                  complete, report unwritten -- status and content were bound
#                  to one signal).
#   4 DRIVE     -- something actually WAKES the sweep (two independent legs),
#                  the drive's own gap is observable, and a failing sweep is
#                  loud under its OWN identity without reddening the unrelated
#                  job that happened to drive it.
#   5 COHERENCE -- the design doc's surface table may not claim more (or less)
#                  than the code has, checked in both directions.
#
# Run:  bash routines/tests/test-dgn1012-terminal-guarantee.sh
# Exit: 0 all pass, 1 any fail.
# Safe/offline: scratch mktemp workspace, stub push.sh, no live ledger, no
# telegram, no real claude.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"
TSL="$REPO/routines/terminal-state-ledger.py"
CRON_GUARD="$REPO/routines/cron-guard.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1012-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() { rc=$?; pkill -f "$TDIR" 2>/dev/null || true; chflags -R nouappend "$TDIR" 2>/dev/null || true; rm -rf "$TDIR" || true; return $rc; }
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot/session-inbox" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/" 2>/dev/null
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/" 2>/dev/null
[[ -f "$TSL" ]] && cp "$TSL" "$WS/routines/"
[[ -f "$CRON_GUARD" ]] && cp "$CRON_GUARD" "$WS/routines/"
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:-/dev/null}"
exit "${PUSH_STUB_RC:-0}"
EOF
chmod +x "$WS/routines/push.sh" "$WS/routines"/*.sh 2>/dev/null
cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
| auto/seed | seed | merged | 1 | - | 2026-01-01 00:00 KST | - | seed |
EOF

SCRATCH_LEDGER="$TDIR/tsl.jsonl"
export DOGANY_TSL_LEDGER="$SCRATCH_LEDGER"
export DOGANY_TSL_INBOX="$WS/.telegram_bot/session-inbox"
export DOGANY_TSL_PUSH="$WS/routines/push.sh"
export DOGANY_TSL_DEDUP_DIR="$TDIR/dedup"

# ---------------------------------------------------------------------------
echo "== 1. ARMED: the machine exists and answers =="
# ---------------------------------------------------------------------------
if [[ -f "$TSL" ]]; then
  ok "terminal-state-ledger.py present in routines/"
else
  bad "terminal-state-ledger.py MISSING -- every dispatch registration call is a no-op"
fi

if [[ -f "$TSL" ]] && python3 -c "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)" "$TSL" >/dev/null 2>&1; then
  ok "terminal-state-ledger.py compiles"
else
  bad "terminal-state-ledger.py does not compile (or is absent)"
fi

if [[ -f "$TSL" ]] && python3 "$TSL" status >/dev/null 2>&1; then
  ok "terminal-state-ledger.py status answers rc=0"
else
  bad "terminal-state-ledger.py status did not answer rc=0"
fi

# The wrapper names the script; the script must be at exactly that name.
# Guards the rename-drift failure class (a moved file re-silences every caller).
called_names="$(grep -o 'terminal-state-ledger\.py' "$WRAPPER" | sort -u)"
if [[ "$called_names" == "terminal-state-ledger.py" ]]; then
  ok "wrapper calls exactly routines/terminal-state-ledger.py"
else
  bad "wrapper call name drifted: '$called_names'"
fi

# ---------------------------------------------------------------------------
echo "== 2. REACHED: the wrapper's own argv lands a row =="
# ---------------------------------------------------------------------------
# Replay the EXACT registration argv from dispatch-detached.sh do_spawn.
python3 "$WS/routines/terminal-state-ledger.py" open --surface dispatch \
  --id "dsp-fixture-1" --ttl 5400 --notify session \
  --note "fixture label" --evidence "$TDIR/out.log" >/dev/null 2>"$TDIR/open.err"
open_rc=$?
if [[ $open_rc -eq 0 ]]; then
  ok "spawn-time registration rc=0"
else
  bad "spawn-time registration rc=$open_rc (stderr: $(head -1 "$TDIR/open.err"))"
fi

if grep -q '"event": "open"' "$SCRATCH_LEDGER" 2>/dev/null \
   && grep -q 'dsp-fixture-1' "$SCRATCH_LEDGER" 2>/dev/null; then
  ok "registration wrote an open row to the ledger"
else
  bad "no open row in ledger -- registration reached nothing"
fi

# SIGKILL is the ONE signal that skips the worker EXIT trap (measured on
# bash 3.2.57/darwin: TERM+INT+HUP all run it). So the sweep is the only
# recovery path, and it must actually fire.
python3 "$WS/routines/terminal-state-ledger.py" open --surface dispatch \
  --id "dsp-fixture-killed" --ttl 1 --notify session \
  --note "sigkilled worker" --evidence "$TDIR/out.log" >/dev/null 2>&1
sweep_out="$(python3 "$WS/routines/terminal-state-ledger.py" sweep --now "$(( $(date +%s) + 99999 ))" 2>&1)"
sweep_rc=$?
if [[ $sweep_rc -eq 0 ]]; then
  ok "sweep of an abandoned obligation rc=0"
else
  bad "sweep rc=$sweep_rc ($sweep_out)"
fi

if grep -q '"event": "expired"' "$SCRATCH_LEDGER" 2>/dev/null; then
  ok "sweep recorded an expired row (SIGKILL path is recoverable)"
else
  bad "sweep left the abandoned obligation open -- SIGKILL ends in silence"
fi

if ls "$WS/.telegram_bot/session-inbox"/*.md >/dev/null 2>&1; then
  ok "sweep dropped a session-inbox file (owner agent is told)"
else
  bad "sweep produced no session-inbox drop -- expiry is itself silent"
fi

# cron surface: "did not run" must be distinguishable from "ran clean".
if [[ -f "$CRON_GUARD" ]] && grep -q 'terminal-state-ledger.py' "$CRON_GUARD"; then
  ok "cron-guard.sh emits a liveness beat"
else
  bad "cron-guard.sh has no beat -- 'job never ran' == 'job ran clean'"
fi

# ---------------------------------------------------------------------------
echo "== 3. SPLIT: lifecycle and product are independent fields =="
# ---------------------------------------------------------------------------
# 2026-08-28 incident shape: the deadline killed the run (lifecycle) but the
# work had landed (product). Build exactly that state and read the drop.
export DISPATCH_TEST=1
RUNID="dsp-fixture-124"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
mkdir -p "$RUNDIR"
: > "$RUNDIR/out.log"

WT="$TDIR/wt"
git init -q -b main "$WT" >/dev/null 2>&1
git -C "$WT" config user.email t@t; git -C "$WT" config user.name t
echo seed > "$WT/seed.txt"; git -C "$WT" add -A >/dev/null; git -C "$WT" commit -qm seed >/dev/null
git -C "$WT" checkout -q -b dsp/fixture
echo product > "$WT/delivered.txt"
git -C "$WT" add -A >/dev/null; git -C "$WT" commit -qm "the work that survived the kill" >/dev/null

# shellcheck disable=SC1090
source "$WS/routines/dispatch-detached.sh"
SCRIPT_DIR="$WS/routines"; WORKSPACE="$WS"
LEDGER_UPDATE="$WS/routines/lib/ledger-update.sh"
LEDGER_FILE="$WS/product/auto-loop-ledger.md"; LEDGER_BRANCH="dsp/fixture"
SESSION_INBOX_DIR="$WS/.telegram_bot/session-inbox"
PUSH_SH="$WS/routines/push.sh"
RUNID="$RUNID"; LABEL="fixture 124"; TICKET="DGN-1012"; MODEL="opus"
BRANCH="dsp/fixture"; TIMEOUT_MIN=90; NOTIFY="silent"
WT_PATH="$WT"; WT_REPO="$WT"; STARTED_AT="2026-08-28T14:00:00+0900"

rm -f "$SESSION_INBOX_DIR"/dispatch-*.md
finalize_run timed-out 124 >/dev/null 2>&1

DROP="$SESSION_INBOX_DIR/dispatch-$RUNID.md"
if [[ -f "$DROP" ]]; then
  ok "timed-out run still drops a terminal signal"
else
  bad "timed-out run produced no drop"
fi

# The drop must carry the PRODUCT, measured from artifacts, not inferred from
# the exit code.
if [[ -f "$DROP" ]] && grep -q 'commits=1' "$DROP"; then
  ok "drop reports the surviving product (commits=1)"
else
  bad "drop hides the product -- commits=1 exists on disk but is not in the signal"
fi

# ...and it must TELL the owner agent to recover it. Today's drop routes every
# non-completed lifecycle to "판단하라" and never says the work is there.
if [[ -f "$DROP" ]] && grep -q '회수' "$DROP"; then
  ok "drop instructs recovery of the surviving product"
else
  bad "drop instructs no recovery -- product present, instruction says only 'diagnose the failure'"
fi

# Control: a genuinely empty timed-out run must NOT claim a product.
RUNID2="dsp-fixture-124-empty"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID2"; mkdir -p "$RUNDIR"; : > "$RUNDIR/out.log"
RUNID="$RUNID2"; WT_PATH=""; WT_REPO="$WS"; BRANCH=""
finalize_run timed-out 124 >/dev/null 2>&1
DROP2="$SESSION_INBOX_DIR/dispatch-$RUNID2.md"
if [[ -f "$DROP2" ]] && ! grep -q '회수' "$DROP2"; then
  ok "control: empty timed-out run claims no product"
else
  bad "control: empty timed-out run falsely claims a product"
fi

# ---------------------------------------------------------------------------
echo "== 4. DRIVE: something wakes the sweep, and the sweep's noise is its own =="
# ---------------------------------------------------------------------------
# NOTE: §3 sources dispatch-detached.sh, whose own `set -euo pipefail` (:86)
# leaks errexit into this shell. §4 runs commands that are SUPPOSED to fail
# (that is the measurement), so restore the suite's declared mode first --
# without this the suite exits 2 mid-section instead of reporting FAILs.
set +e
# §2 proved the sweep RECOVERS a SIGKILLed obligation when it is called.
# Nothing in canonical CALLED it (measured before this section landed:
# `grep -rn 'ledger.py" sweep'` over *.sh/*.py minus tests -> rc=1, while the
# same grep for 'sweep' inside the ledger itself hit 3 lines -- the detector
# was alive and saw zero). An uncalled sweep is a guarantee on paper.
#
# Drive = TWO INDEPENDENT LEGS, neither depending on the other:
#   leg 1  cron-guard.sh        -- every periodic job (plist 11/11, systemd 2/2)
#   leg 2  dispatch-detached.sh -- launchd-independent, rides dispatch activity
# and the sweep records its OWN liveness beat, so a gap in the drive is itself
# an obligation that the next sweep expires.

CG_ISO="$WS/routines/cron-guard-iso.sh"
ISO_DEDUP="$TDIR/cg-dedup"
ISO_PUSH_LOG="$TDIR/cg-push.log"
mkdir -p "$ISO_DEDUP"
if [[ -f "$CRON_GUARD" ]]; then
  sed -e "s|DEDUP_DIR=\"/tmp/dogany-cron-guard\"|DEDUP_DIR=\"$ISO_DEDUP\"|" \
      "$CRON_GUARD" > "$CG_ISO"
  chmod +x "$CG_ISO"
fi

# -- leg 1 wired?
if [[ -f "$CRON_GUARD" ]] && grep -q 'terminal-state-ledger.py" sweep' "$CRON_GUARD"; then
  ok "leg 1: cron-guard.sh drives the sweep (every periodic job is a driver)"
else
  bad "leg 1 MISSING: no periodic job wakes the sweep -- SIGKILL ends in silence"
fi

# -- leg 2 wired? (must NOT share a fate with launchd)
if grep -q 'terminal-state-ledger.py" sweep' "$WRAPPER"; then
  ok "leg 2: dispatch-detached.sh drives the sweep (launchd-independent)"
else
  bad "leg 2 MISSING: the drive has a single point of failure (the cron fleet)"
fi

# -- the throttle exists, so riding EVERY job is affordable
if python3 "$TSL" sweep --throttle 3600 --now "$(date +%s)" >/dev/null 2>&1; then
  ok "sweep accepts --throttle (riding all 13 jobs costs one sweep per window)"
else
  bad "sweep has no --throttle -- driving it from every job would sweep ~288x/day"
fi

# -- and the throttle actually suppresses the second run in the window
L_THR="$TDIR/tsl-throttle.jsonl"
: > "$L_THR"
DOGANY_TSL_LEDGER="$L_THR" python3 "$TSL" sweep --throttle 3600 >/dev/null 2>&1
DOGANY_TSL_LEDGER="$L_THR" python3 "$TSL" sweep --throttle 3600 >/dev/null 2>&1
thr_beats="$(grep -c '"surface": "sweep"' "$L_THR" 2>/dev/null; true)"
thr_beats="${thr_beats:-0}"
if [[ "$thr_beats" -eq 1 ]]; then
  ok "throttle: 2 drives in one window -> 1 sweep (self-beat count=1)"
else
  bad "throttle did not collapse 2 drives into 1 sweep (self-beat count=$thr_beats, want 1)"
fi

# -- the sweep leaves its OWN liveness beat (drive-gap becomes observable)
if grep -q '"surface": "sweep"' "$L_THR" 2>/dev/null; then
  ok "sweep records its own liveness beat (the closure machine is on the ledger)"
else
  bad "sweep leaves no self-beat -- 'the sweep never ran' is unobservable"
fi

# -- concurrency: two drivers firing at the same instant must not double-sweep
#    (health-observer and mirror-poll both fire on a 300s StartInterval).
L_RACE="$TDIR/tsl-race.jsonl"
: > "$L_RACE"
DOGANY_TSL_LEDGER="$L_RACE" python3 "$TSL" sweep --throttle 3600 >/dev/null 2>&1 &
DOGANY_TSL_LEDGER="$L_RACE" python3 "$TSL" sweep --throttle 3600 >/dev/null 2>&1 &
wait
race_beats="$(grep -c '"surface": "sweep"' "$L_RACE" 2>/dev/null; true)"
race_beats="${race_beats:-0}"
if [[ "$race_beats" -eq 1 ]]; then
  ok "concurrent drivers: exactly 1 sweep ran (atomic claim held)"
else
  bad "concurrent drivers produced $race_beats sweeps (want 1) -- duplicate drops/pushes"
fi

# -- a GAP in the drive is itself detected: a stale self-beat expires and is reported
L_GAP="$TDIR/tsl-gap.jsonl"
GAP_INBOX="$TDIR/gap-inbox"
mkdir -p "$GAP_INBOX"
GAP_PAST=$(( $(date +%s) - 20000 ))
printf '{"v": 1, "event": "beat", "id": "terminal-state-sweep", "surface": "sweep", "at": "gap-fixture", "at_epoch": %d, "rc": 0, "ttl_secs": 7200, "expires_epoch": %d, "note": "prev sweep"}\n' \
  "$GAP_PAST" "$(( GAP_PAST + 7200 ))" > "$L_GAP"
DOGANY_TSL_LEDGER="$L_GAP" DOGANY_TSL_INBOX="$GAP_INBOX" \
  python3 "$TSL" sweep --throttle 3600 >/dev/null 2>&1
if grep -q '"event": "expired"' "$L_GAP" 2>/dev/null \
   && grep -q 'terminal-state-sweep' "$L_GAP" 2>/dev/null \
   && ls "$GAP_INBOX"/*.md >/dev/null 2>&1; then
  ok "drive gap detected: a stale self-beat expires and is reported"
else
  bad "a drive gap left no trace -- the sweep can stop for hours unnoticed"
fi

# -- END TO END through leg 1: an abandoned (SIGKILLed) obligation is recovered
#    by wrapping an ordinary, unrelated job. This is the whole axis in one shot.
L_E2E="$TDIR/tsl-e2e.jsonl"
E2E_INBOX="$TDIR/e2e-inbox"
mkdir -p "$E2E_INBOX"
E2E_PAST=$(( $(date +%s) - 10 ))
printf '{"v": 1, "event": "open", "id": "dsp-e2e-killed", "surface": "dispatch", "at": "e2e-fixture", "at_epoch": %d, "ttl_secs": 1, "expires_epoch": %d, "notify": "session", "note": "worker SIGKILLed", "evidence": "%s/out.log"}\n' \
  "$(( E2E_PAST - 100 ))" "$E2E_PAST" "$TDIR" > "$L_E2E"
e2e_out="$(DOGANY_TSL_LEDGER="$L_E2E" DOGANY_TSL_INBOX="$E2E_INBOX" \
           DOGANY_TSL_DEDUP_DIR="$TDIR/dedup-e2e" PUSH_STUB_LOG="$ISO_PUSH_LOG" \
           bash "$CG_ISO" --label dgn1012.e2e -- /usr/bin/true 2>&1)"
e2e_rc=$?
if [[ $e2e_rc -eq 0 ]] && grep -q '"event": "expired"' "$L_E2E" 2>/dev/null \
   && ls "$E2E_INBOX"/*.md >/dev/null 2>&1; then
  ok "e2e: an unrelated cron job recovered a SIGKILLed delegation (host rc=0)"
else
  bad "e2e: wrapping a job did not recover the abandoned obligation (rc=$e2e_rc, out: $(echo "$e2e_out" | head -1))"
fi

# -- NOISE ISOLATION: a FAILING sweep must not paint the wrapped job red.
#    This is the objection that held the decision back. Force the sweep to
#    fail (due obligation + an inbox path that cannot be created) while the
#    wrapped command succeeds.
L_ISO="$TDIR/tsl-iso.jsonl"
: > "$TDIR/blocker"                        # a FILE, so makedirs() under it fails
ISO_PAST=$(( $(date +%s) - 10 ))
printf '{"v": 1, "event": "open", "id": "dsp-iso-killed", "surface": "dispatch", "at": "iso-fixture", "at_epoch": %d, "ttl_secs": 1, "expires_epoch": %d, "notify": "session", "note": "worker SIGKILLed", "evidence": "x"}\n' \
  "$(( ISO_PAST - 100 ))" "$ISO_PAST" > "$L_ISO"
: > "$ISO_PUSH_LOG"
iso_out="$(DOGANY_TSL_LEDGER="$L_ISO" DOGANY_TSL_INBOX="$TDIR/blocker/inbox" \
           DOGANY_TSL_DEDUP_DIR="$TDIR/dedup-iso" PUSH_STUB_LOG="$ISO_PUSH_LOG" \
           bash "$CG_ISO" --label dgn1012.iso -- /usr/bin/true 2>&1)"
iso_rc=$?
if [[ $iso_rc -eq 0 ]]; then
  ok "noise isolation: a failing sweep leaves the wrapped job's exit code at 0"
else
  bad "noise isolation BROKEN: sweep rc leaked into an unrelated job (rc=$iso_rc)"
fi

if ! grep -q 'ROUTINE FAILED' "$ISO_PUSH_LOG" 2>/dev/null; then
  ok "noise isolation: a failing sweep did not fire the job's ROUTINE FAILED alert"
else
  bad "noise isolation BROKEN: sweep failure impersonated a job failure"
fi

# ...but it must not be silent either. It fires under its OWN identity.
if grep -q '종결' "$ISO_PUSH_LOG" 2>/dev/null; then
  ok "sweep failure raises its OWN owner alert (loud, and not the job's name)"
else
  bad "sweep failure was SILENT -- the closure machine can die quietly"
fi

# POSITIVE CONTROL for the two negative claims above: the same push log, the
# same detector, on a job that really failed. Without this, "no ROUTINE FAILED
# in the log" and "the log could not be read" are the same observation.
: > "$ISO_PUSH_LOG"
DOGANY_TSL_LEDGER="$TDIR/tsl-ctl.jsonl" DOGANY_TSL_INBOX="$WS/.telegram_bot/session-inbox" \
  DOGANY_TSL_DEDUP_DIR="$TDIR/dedup-ctl" PUSH_STUB_LOG="$ISO_PUSH_LOG" \
  bash "$CG_ISO" --label dgn1012.control -- /usr/bin/false >/dev/null 2>&1
ctl_rc=$?
if [[ $ctl_rc -ne 0 ]] && grep -q 'ROUTINE FAILED' "$ISO_PUSH_LOG" 2>/dev/null; then
  ok "control: a genuinely failed job DOES exit nonzero and DOES push ROUTINE FAILED"
else
  bad "control: the failure detector is dead (rc=$ctl_rc) -- the two checks above prove nothing"
fi

# ---------------------------------------------------------------------------
echo "== 5. COHERENCE: the design doc may not claim more than the code has =="
# ---------------------------------------------------------------------------
# The axis's own anti-slogan clause: "문서에 '종결을 보장한다'고 쓰는 순간,
# 기계가 없으면 그게 거짓 강제점이다. 미배선 표면은 문서에 '미보장'이라고 적는다."
# That clause is unenforceable while it lives only in prose. The design doc
# carries a machine-readable surface table; this section runs each row's probe
# and requires the code to agree with the claim -- IN BOTH DIRECTIONS. A 보장
# row whose probe misses fails (the doc over-claims). A 미보장 row whose probe
# HITS also fails (someone wired a surface and left the doc stale). Drift is a
# red test either way, so the guarantee table can never quietly go fictional.
DESIGN_DOC="$REPO/../../files/reports/DGN-1012-DESIGN.md"
if [[ -f "$DESIGN_DOC" ]]; then
  ok "design doc present ($(basename "$DESIGN_DOC"))"
  rows=0
  while IFS='|' read -r _ surface status probe _rest; do
    surface="${surface#"${surface%%[![:space:]]*}"}"; surface="${surface%"${surface##*[![:space:]]}"}"
    status="${status#"${status%%[![:space:]]*}"}";    status="${status%"${status##*[![:space:]]}"}"
    probe="${probe#"${probe%%[![:space:]]*}"}";       probe="${probe%"${probe##*[![:space:]]}"}"
    [[ -z "$surface" || "$surface" == "surface" || "$surface" == --* ]] && continue
    [[ "$status" != "보장" && "$status" != "미보장" ]] && continue
    rows=$((rows + 1))
    hit=1
    case "$probe" in
      file:*)
        [[ -f "$REPO/${probe#file:}" ]] && hit=0 ;;
      grep:*)
        rest="${probe#grep:}"; pfile="${rest%%:*}"; pat="${rest#*:}"
        grep -q -- "$pat" "$REPO/$pfile" 2>/dev/null && hit=0 ;;
      *) hit=2 ;;
    esac
    if [[ $hit -eq 2 ]]; then
      bad "surface '$surface': unknown probe form '$probe'"
    elif [[ "$status" == "보장" && $hit -eq 0 ]]; then
      ok "surface '$surface' claims 보장 and the probe confirms it"
    elif [[ "$status" == "보장" && $hit -ne 0 ]]; then
      bad "surface '$surface' claims 보장 but probe FAILS ($probe) -- false enforcement point"
    elif [[ "$status" == "미보장" && $hit -ne 0 ]]; then
      ok "surface '$surface' declared 미보장 and is indeed unwired"
    else
      bad "surface '$surface' declared 미보장 but probe HITS ($probe) -- doc is stale, update it to 보장"
    fi
  done < <(sed -n '/DGN-1012-SURFACES v1/,/END-SURFACES/p' "$DESIGN_DOC")
  if [[ $rows -ge 5 ]]; then
    ok "surface table covers all $rows surfaces (공란 없음)"
  else
    bad "surface table has only $rows parsed rows -- the axis requires all 5, no blanks"
  fi
else
  bad "design doc missing at $DESIGN_DOC -- guarantee claims are unanchored"
fi

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] && exit 0
exit 1
