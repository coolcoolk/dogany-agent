#!/usr/bin/env bash
# test-lane: slow    # timed out >30s on a loaded host (2026-10-10; >=10s leaves the push lane, DGN-1734)
# test-dgn1655-instance-drive.sh -- the framework-owned instance drive
# (DGN-1655) and the design grill fixes F1/F2/F3/F5/F6/F7.
#
# Sections
#   1 POLICY    one TTL value -> period TTL/4, throttle period/2 (F1),
#               warn TTL/2; invalid TTL refused by the query AND the renderer
#   2 F2        the reserved (terminal-state-sweep, sweep) row: beat/open/
#               close refuse it, only a drive-marked sweep writes it, every
#               other sweep records under the diagnostic id
#   3 F1        beat stamped at sweep START; stale rendered argv refused;
#               append failure is nonzero with no synthetic beat; F5 mono
#   4 WINDOW    fake-clock window >= 2*TTL, no dispatch, no feature timer:
#               every gap <= TTL; 1-3 missed firings stay green, 4 go red;
#               the checker reads the cron row too (F7) and tells "unit did
#               not fire" from "unit fired, sweep did not beat"
#   5 OBSERVER  loaded label + beat older than TTL/2 -> one episode, quiet
#               while it lasts, cleared by recovery, new episode after
#   6 LAUNCHD   drive-setup.sh against a launchctl stub: install verifies the
#               LIVE definition (F3) -- stale interval, sticky bootout,
#               bootstrap failure, duplicate / foreign-root labels, no beat;
#               a TTL change re-renders and reloads; sandbox refusal (rc 5)
#   7 SYSTEMD   the same contract against a systemctl --user stub
#   8 WIRING    mint renders, start/install/update load + verify fail-closed,
#               the drive is never deferred or module-gated (kitless,
#               MIRROR_MODULE=off), dry-run start plans it
#
# Run:  bash routines/tests/test-dgn1655-instance-drive.sh
# Exit: 0 all pass, 1 any fail.
# Safe/offline: mktemp workspace; launchctl/systemctl are recording stubs
# behind the DRIVE_SETUP_* seams; the one sandbox case runs with a fake HOME
# and a PATH-shadow launchctl that records (and must see zero calls).

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RT="$(cd "$HARNESS_DIR/.." && pwd -P)"            # .../routines
TSL="$RT/terminal-state-ledger.py"
DS="$RT/lib/drive-setup.sh"
HO="$RT/health-observer.py"
REPO_ROOT=""
_w="$RT"
for _ in 1 2 3 4 5 6; do
  [[ -f "$_w/.dogany-canonical" ]] && REPO_ROOT="$_w" && break
  _w="$(dirname "$_w")"
done

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d "${TMPDIR:-/tmp}/dgn1655.XXXXXX")"
TDIR="$(cd "$TDIR" && pwd -P)"
cleanup() { rc=$?; chflags -R nouappend "$TDIR" 2>/dev/null || true; rm -rf "$TDIR" || true; return $rc; }
trap cleanup EXIT

export DOGANY_TSL_DEDUP_DIR="$TDIR/dedup"
export DOGANY_TSL_INBOX="$TDIR/inbox"
cat > "$TDIR/push.sh" <<'EOF'
#!/bin/bash
sleep "${PUSH_STUB_SLEEP:-0}"
echo "PUSH-STUB $*" >> "${PUSH_STUB_LOG:-/dev/null}"
exit 0
EOF
chmod +x "$TDIR/push.sh"
export DOGANY_TSL_PUSH="$TDIR/push.sh"

jget() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
count_rows() { # <ledger> <id> <surface>
  python3 - "$@" <<'PY'
import json, sys
n = 0
try:
    for line in open(sys.argv[1]):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "beat" and ev.get("id") == sys.argv[2] \
                and ev.get("surface") == sys.argv[3]:
            n += 1
except OSError:
    pass
print(n)
PY
}

# make_root <dir> [ttl] -- a minimal instance root carrying the code under test
make_root() {
  local r="$1" ttl="${2:-}"
  mkdir -p "$r/routines/lib" "$r/routines/bundle" "$r/.telegram_bot"
  cp "$TSL" "$r/routines/terminal-state-ledger.py"
  cp "$DS" "$r/routines/lib/drive-setup.sh"
  cp "$RT"/bundle/instance-drive.*.tpl "$r/routines/bundle/"
  printf 'DOGANY_AGENT_NAME=%s\n' "$(basename "$r")" > "$r/.instance.conf"
  if [[ -n "$ttl" ]]; then
    sed -i.bak "s/^TERMINAL_STATE_SWEEP_TTL_SECONDS = .*/TERMINAL_STATE_SWEEP_TTL_SECONDS = $ttl/" \
      "$r/routines/terminal-state-ledger.py" && rm -f "$r/routines/terminal-state-ledger.py.bak"
  fi
}

# ---------------------------------------------------------------------------
echo "== 1. POLICY: one TTL, everything derived =="
# ---------------------------------------------------------------------------
POL="$(python3 "$TSL" policy --json 2>&1)"
P_TTL="$(printf '%s' "$POL" | jget ttl)"; P_PER="$(printf '%s' "$POL" | jget period)"
P_THR="$(printf '%s' "$POL" | jget throttle)"; P_WARN="$(printf '%s' "$POL" | jget warn_age)"
if [[ "$P_TTL/$P_PER/$P_THR/$P_WARN" == "7200/1800/900/3600" ]]; then
  ok "policy 7200 -> period 1800, throttle 900 (period/2, F1), warn 3600 (TTL/2)"
else
  bad "policy derivation wrong: $POL"
fi
if [[ "$(grep -c '7200' "$TSL")" -le 3 ]] && ! grep -q 'SWEEP_BEAT_TTL_MIN' "$TSL"; then
  ok "no second editable 7200 floor (SWEEP_BEAT_TTL_MIN gone)"
else
  bad "a separate 7200 policy constant survives in the ledger"
fi
for badttl in 7201 0 -4; do
  R="$TDIR/pol-$badttl"; make_root "$R" "$badttl"
  python3 "$R/routines/terminal-state-ledger.py" policy >/dev/null 2>&1; prc=$?
  DRIVE_SETUP_OS=Darwin bash "$R/routines/lib/drive-setup.sh" render "$R" >/dev/null 2>&1; rrc=$?
  if [[ $prc -eq 2 && $rrc -eq 2 ]] && ! ls "$R"/routines/*.instance-drive.plist >/dev/null 2>&1; then
    ok "TTL=$badttl refused by policy query (rc 2) and renderer (rc 2), nothing rendered"
  else
    bad "TTL=$badttl not refused (policy rc=$prc render rc=$rrc)"
  fi
done

# Missing or partial query output must identify the absent input, not fall
# through concatenated-number validation or render guessed policy values.
for missing in engine ttl period throttle; do
  R="$TDIR/missing-$missing"; make_root "$R"
  case "$missing" in
    engine) rm "$R/routines/terminal-state-ledger.py"; expected="ledger engine missing: $R/routines/terminal-state-ledger.py" ;;
    ttl) printf 'pass\n' > "$R/routines/terminal-state-ledger.py"; expected="policy query missing input 'ttl'" ;;
    period) printf 'print("ttl=7200 throttle=900")\n' > "$R/routines/terminal-state-ledger.py"; expected="policy query missing input 'period'" ;;
    throttle) printf 'print("ttl=7200 period=1800")\n' > "$R/routines/terminal-state-ledger.py"; expected="policy query missing input 'throttle'" ;;
  esac
  out="$(DRIVE_SETUP_OS=Darwin bash "$R/routines/lib/drive-setup.sh" render "$R" 2>&1)"; rc=$?
  if [[ $rc -eq 2 && "$out" == *"$expected"* && "$out" == *"$R/routines/terminal-state-ledger.py"* ]] \
     && ! ls "$R"/routines/*.instance-drive.plist >/dev/null 2>&1; then
    ok "missing $missing names the input and engine path; no guessed drive rendered"
  else
    bad "missing $missing diagnostic/render wrong (rc=$rc): $out"
  fi
done

# ---------------------------------------------------------------------------
echo "== 2. F2: the reserved row belongs to the drive only =="
# ---------------------------------------------------------------------------
L2="$TDIR/f2.jsonl"; : > "$L2"
export DOGANY_TSL_LEDGER="$L2"
python3 "$TSL" beat --id terminal-state-sweep --surface sweep >/dev/null 2>&1; b1=$?
python3 "$TSL" beat --id terminal-state-sweep >/dev/null 2>&1; b2=$?
python3 "$TSL" beat --id other --surface sweep >/dev/null 2>&1; b3=$?
python3 "$TSL" open --id terminal-state-sweep --surface x --ttl 5 >/dev/null 2>&1; b4=$?
python3 "$TSL" close --id terminal-state-sweep --state done >/dev/null 2>&1; b5=$?
python3 "$TSL" sweep --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1; s_nomark=$?
n_res_before="$(count_rows "$L2" terminal-state-sweep sweep)"
n_diag="$(count_rows "$L2" terminal-state-sweep-diag sweep-diag)"
DOGANY_TSL_DRIVE=1 python3 "$TSL" sweep --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1; s_mark=$?
n_res_after="$(count_rows "$L2" terminal-state-sweep sweep)"
if [[ "$b1$b2$b3$b4$b5" == "33333" && $s_nomark -eq 0 && $s_mark -eq 0 \
      && "$n_res_before" -eq 0 && "$n_diag" -eq 1 && "$n_res_after" -eq 1 ]] \
   && ! grep -q '"event": "open", "id": "terminal-state-sweep"' "$L2"; then
  ok "F2: beat/open/close refuse the reserved row (rc 3); an unmarked sweep records the diag row; only the drive-marked sweep writes terminal-state-sweep"
else
  bad "F2 reservation broken (beat rc=$b1/$b2/$b3 open=$b4 close=$b5 sweep=$s_nomark/$s_mark reserved=$n_res_before->$n_res_after diag=$n_diag)"
fi
unset DOGANY_TSL_LEDGER

# ---------------------------------------------------------------------------
echo "== 3. F1: beat at sweep start; stale argv; append failure; F5 mono =="
# ---------------------------------------------------------------------------
L3="$TDIR/f1.jsonl"
past=$(( $(date +%s) - 50 ))
printf '{"v": 1, "event": "open", "id": "slow-owner", "surface": "dispatch", "at": "x", "at_epoch": %d, "ttl_secs": 1, "expires_epoch": %d, "notify": "owner", "note": "n", "evidence": ""}\n' \
  "$((past - 10))" "$past" > "$L3"
t0="$(date +%s)"
DOGANY_TSL_LEDGER="$L3" DOGANY_TSL_DRIVE=1 PUSH_STUB_SLEEP=3 PUSH_STUB_LOG="$TDIR/f1-push.log" \
  python3 "$TSL" sweep --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1
t1="$(date +%s)"
read -r beat_at beat_line exp_line mono_ok period_ok < <(python3 - "$L3" <<'PY'
import json, sys
beat_at, bl, el, mono, per = -1, -1, -1, 0, 0
for i, line in enumerate(open(sys.argv[1])):
    ev = json.loads(line)
    if ev["event"] == "beat" and ev["id"] == "terminal-state-sweep":
        beat_at, bl = ev["at_epoch"], i
        mono = 1 if isinstance(ev.get("mono"), float) else 0
        per = 1 if ev.get("period") == 1800 and ev.get("throttle") == 900 else 0
    if ev["event"] == "expired":
        el = i
print(beat_at, bl, el, mono, per)
PY
)
if [[ $((t1 - t0)) -ge 3 && "$beat_at" -le $((t0 + 1)) && "$beat_line" -ge 0 && "$beat_line" -lt "$exp_line" ]]; then
  ok "F1: beat stamped at sweep START (beat $((beat_at - t0))s after start; the 3s notify came after it)"
else
  bad "F1: beat not at start (t0=$t0 t1=$t1 beat_at=$beat_at beat_line=$beat_line expired_line=$exp_line)"
fi
if [[ "$mono_ok" == "1" && "$period_ok" == "1" ]]; then
  ok "F5: the beat carries time.monotonic() as data (and the rendered period/throttle)"
else
  bad "beat lacks mono ($mono_ok) or policy fields ($period_ok)"
fi

L3b="$TDIR/stale.jsonl"
printf '{"v": 1, "event": "open", "id": "stale-o", "surface": "dispatch", "at": "x", "at_epoch": %d, "ttl_secs": 1, "expires_epoch": %d, "notify": "session", "note": "n", "evidence": ""}\n' \
  "$((past - 10))" "$past" > "$L3b"
DOGANY_TSL_LEDGER="$L3b" DOGANY_TSL_DRIVE=1 python3 "$TSL" sweep --throttle 1800 --beat-ttl 7200 >/dev/null 2>&1; src=$?
if [[ $src -eq 4 && "$(count_rows "$L3b" terminal-state-sweep sweep)" -eq 0 ]] \
   && grep -q '"event": "expired"' "$L3b" && [[ "$(count_rows "$L3b" terminal-state-sweep-diag sweep-diag)" -eq 1 ]]; then
  ok "stale rendered argv (old throttle==period): rc 4, no reserved beat, obligations still expire"
else
  bad "stale drive argv not refused (rc=$src)"
fi

: > "$TDIR/notadir"
DOGANY_TSL_LEDGER="$TDIR/notadir/l.jsonl" DOGANY_TSL_DRIVE=1 \
  python3 "$TSL" sweep --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1; arc=$?
if [[ $arc -ne 0 ]]; then
  ok "ledger append failure -> nonzero (rc=$arc), no synthetic success"
else
  bad "append failure exited 0"
fi

L3c="$TDIR/corrupt.jsonl"
printf 'not json\n{"broken": \n' > "$L3c"
DOGANY_TSL_LEDGER="$L3c" DOGANY_TSL_DRIVE=1 python3 "$TSL" sweep --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1; crc=$?
if [[ $crc -eq 0 && "$(count_rows "$L3c" terminal-state-sweep sweep)" -eq 1 ]] \
   && DOGANY_TSL_LEDGER="$L3c" python3 "$TSL" check --label x --json 2>/dev/null | grep -q '"corrupt": 2'; then
  ok "corrupt ledger lines are skipped and counted; the drive still beats"
else
  bad "corrupt ledger broke the drive (rc=$crc)"
fi

# ---------------------------------------------------------------------------
echo "== 4. WINDOW: fake clock, no dispatch, no feature timer =="
# ---------------------------------------------------------------------------
LABEL4="com.telegram-skill-bot.t4.instance-drive"
# fire <ledger> <epoch> [rc] -- one drive-unit firing at <epoch>: the sweep
# (drive marker, rendered argv, fake clock) then cron-guard's own cron row.
fire() {
  DOGANY_TSL_LEDGER="$1" DOGANY_TSL_DRIVE=1 python3 "$TSL" sweep --now "$2" \
    --throttle "$P_THR" --beat-ttl "$P_TTL" >/dev/null 2>&1
  printf '{"v": 1, "event": "beat", "id": "%s", "surface": "cron", "at": "f", "at_epoch": %d, "rc": %d, "ttl_secs": null, "expires_epoch": null, "note": ""}\n' \
    "$LABEL4" "$(( $2 + 2 ))" "${3:-0}" >> "$1"
}
# window <ledger> <start> <n> <skip-csv> [mode] -- n nominal firings every
# period with -5..+7s scheduler jitter; listed slots are missed (mode=gone:
# the unit did not fire at all; mode=fail: it fired, sweep never beat).
window() {
  local l="$1" st="$2" n="$3" skip=",$4," mode="${5:-gone}" i at
  : > "$l"
  for ((i = 0; i < n; i++)); do
    at=$(( st + i * P_PER + (i % 3 == 0 ? -5 : 7) ))
    if [[ "$skip" == *",$i,"* ]]; then
      [[ "$mode" == "fail" ]] && printf '{"v": 1, "event": "beat", "id": "%s", "surface": "cron", "at": "f", "at_epoch": %d, "rc": 1, "ttl_secs": null, "expires_epoch": null, "note": ""}\n' "$LABEL4" "$at" >> "$l"
      continue
    fi
    fire "$l" "$at"
  done
}
ST=$(( $(date +%s) - 400000 ))
N=$(( 2 * P_TTL / P_PER + 2 ))          # > 2*TTL, >= 9 firing opportunities
UNTIL=$(( ST + (N - 1) * P_PER + 10 ))
L4="$TDIR/w0.jsonl"; window "$L4" "$ST" "$N" ""
out="$(DOGANY_TSL_LEDGER="$L4" python3 "$TSL" check --since "$((ST - 10))" --until "$UNTIL" --label "$LABEL4" --qualify --json)"; rc=$?
if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"green": true' \
   && [[ "$(printf '%s' "$out" | jget drive_beats)" -eq "$N" ]]; then
  ok "no dispatch, no feature timer, $N firings over $((UNTIL - ST))s (>= 2*TTL): every gap <= TTL (max $(printf '%s' "$out" | jget max_gap)s)"
else
  bad "clean window not green (rc=$rc): $(printf '%s' "$out" | tr -d '\n' | head -c 400)"
fi
for miss in "3" "3,4" "3,4,5"; do
  L4m="$TDIR/w-$miss.jsonl"; window "$L4m" "$ST" "$N" "$miss"
  DOGANY_TSL_LEDGER="$L4m" python3 "$TSL" check --since "$ST" --until "$UNTIL" --label "$LABEL4" >/dev/null 2>&1; mrc=$?
  if [[ $mrc -eq 0 ]]; then
    ok "missed firing(s) [$miss]: gap stays <= TTL (TTL/4 margin holds)"
  else
    bad "missed firing(s) [$miss] went red -- the TTL/4 margin arithmetic is wrong"
  fi
done
L4r="$TDIR/w-red.jsonl"; window "$L4r" "$ST" "$N" "3,4,5,6"
out="$(DOGANY_TSL_LEDGER="$L4r" python3 "$TSL" check --since "$ST" --until "$UNTIL" --label "$LABEL4")"; rrc=$?
if [[ $rrc -eq 1 ]] && printf '%s' "$out" | grep -q 'unit did not fire'; then
  ok "four missed firings: RED, and the cron row says the unit did not fire (F7)"
else
  bad "4 missed firings not red/diagnosed (rc=$rrc): $out"
fi
L4f="$TDIR/w-fail.jsonl"; window "$L4f" "$ST" "$N" "3,4,5,6" fail
out="$(DOGANY_TSL_LEDGER="$L4f" python3 "$TSL" check --since "$ST" --until "$UNTIL" --label "$LABEL4")"; frc=$?
if [[ $frc -eq 1 ]] && printf '%s' "$out" | grep -q 'unit fired 4x in the gap'; then
  ok "unit fired but sweep never beat: RED with the F7 split ('unit fired 4x ... sweep throttled, skipped or failed')"
else
  bad "F7 split missing (rc=$frc): $out"
fi
# a returning drive after > TTL leaves the failed gap on the ledger, and the
# next sweep expires the overdue drive row (history is not overwritten)
L4g="$TDIR/w-gap.jsonl"; : > "$L4g"
fire "$L4g" "$ST"; fire "$L4g" $(( ST + P_TTL + 600 ))
out="$(DOGANY_TSL_LEDGER="$L4g" python3 "$TSL" check --since "$ST" --until $(( ST + P_TTL + 700 )) --label "$LABEL4")"
if printf '%s' "$out" | grep -q "gap $(( P_TTL + 600 ))s > ttl" \
   && grep -q '"event": "expired", "id": "terminal-state-sweep"' "$L4g"; then
  ok "drive returning after > TTL: auditable red gap kept, overdue drive row expired"
else
  bad "returned drive hid its gap: $out"
fi
L4d="$TDIR/w-dsp.jsonl"; window "$L4d" "$ST" "$N" ""
printf '{"v": 1, "event": "open", "id": "dsp-x", "surface": "dispatch", "at": "d", "at_epoch": %d, "ttl_secs": 5400, "expires_epoch": %d, "notify": "session", "note": "", "evidence": ""}\n' \
  $(( ST + 100 )) $(( ST + 5500 )) >> "$L4d"
DOGANY_TSL_LEDGER="$L4d" python3 "$TSL" check --since "$ST" --until "$UNTIL" --label "$LABEL4" --qualify >/dev/null 2>&1; drc=$?
DOGANY_TSL_LEDGER="$L4" python3 "$TSL" check --since "$ST" --until $(( ST + P_TTL )) --label "$LABEL4" --qualify >/dev/null 2>&1; qrc=$?
if [[ $drc -eq 1 && $qrc -eq 1 ]]; then
  ok "--qualify refuses a window with own-agent dispatch or shorter than 2*TTL"
else
  bad "--qualify too lenient (dispatch rc=$drc short rc=$qrc)"
fi

# ---------------------------------------------------------------------------
echo "== 5. OBSERVER: TTL/2 warning episode =="
# ---------------------------------------------------------------------------
R5="$TDIR/obs5"; make_root "$R5"
L5="$TDIR/obs.jsonl"; : > "$L5"
obs_out="$(DOGANY_TSL_LEDGER="$L5" DOGANY_HEALTH_OBSERVER_STATE_DIR="$TDIR/hostate" \
  python3 - "$HO" "$R5" "$L5" <<'PY'
import importlib.util, json, os, sys
ho_path, root, ledger = sys.argv[1:4]
sys.path.insert(0, os.path.join(os.path.dirname(ho_path), "lib"))
spec = importlib.util.spec_from_file_location("ho", ho_path)
ho = importlib.util.module_from_spec(spec); spec.loader.exec_module(ho)
label = "com.telegram-skill-bot.obs5.instance-drive"
now = 2_000_000_000
def beat(at):
    with open(ledger, "a") as f:
        f.write(json.dumps({"v": 1, "event": "beat", "id": "terminal-state-sweep",
                            "surface": "sweep", "at": "x", "at_epoch": at,
                            "rc": 0, "ttl_secs": 7200, "expires_epoch": at + 7200}) + "\n")
inbox = os.path.join(root, ".telegram_bot", "session-inbox")
def drops():
    return len([n for n in os.listdir(inbox)]) if os.path.isdir(inbox) else 0
res = []
beat(now - 1000)
res.append(len(ho.observe_instance_drive({label: None}, root, now)))           # fresh
res.append(len(ho.observe_instance_drive({label: None}, root, now + 2700)))    # age 3700 > 3600
res.append(len(ho.observe_instance_drive({label: None}, root, now + 3000)))    # same episode
res.append(drops())
beat(now + 3100)
res.append(len(ho.observe_instance_drive({label: None}, root, now + 3200)))    # recovered
res.append(len(ho.observe_instance_drive({label: None}, root, now + 3100 + 3700)))  # new episode
res.append(len(ho.observe_instance_drive({}, root, now + 3100 + 9000)))        # not loaded
print(" ".join(map(str, res)))
PY
)"
if [[ "$obs_out" == "0 1 0 1 0 1 0" ]]; then
  ok "observer: fresh=quiet, > TTL/2 = one episode + one session drop, quiet while open, cleared by a fresh beat, new episode later, unloaded label ignored"
else
  bad "observer episode sequence wrong: [$obs_out] want [0 1 0 1 0 1 0]"
fi
if grep -q 'Never start mirror-poll or another feature timer' "$R5"/.telegram_bot/session-inbox/instance-drive-*.md 2>/dev/null; then
  ok "observer drop forbids restoring a feature timer to green the graph (F6 window rule)"
else
  bad "observer drop lacks the no-mask instruction"
fi

# ---------------------------------------------------------------------------
echo "== 6. LAUNCHD: install verifies the LIVE definition (F3/F6) =="
# ---------------------------------------------------------------------------
STUB="$TDIR/lc"; mkdir -p "$STUB"
cat > "$STUB/launchctl" <<'PY'
#!/usr/bin/env python3
# launchctl stub: a private domain in $LC_STUB_DIR/state.json
import json, os, plistlib, subprocess, sys
d = os.environ["LC_STUB_DIR"]
sp = os.path.join(d, "state.json")
st = json.load(open(sp)) if os.path.exists(sp) else {}
open(os.path.join(d, "calls.log"), "a").write(" ".join(sys.argv[1:]) + "\n")
def save(): json.dump(st, open(sp, "w"))
def counter(name):
    p = os.path.join(d, name)
    n = int(open(p).read()) if os.path.exists(p) else 0
    if n > 0:
        open(p, "w").write(str(n - 1))
    return n
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "bootout":
    label = sys.argv[2].rsplit("/", 1)[-1]
    if label not in st:
        sys.stderr.write("Boot-out failed: 3: No such process\n"); sys.exit(3)
    if counter("sticky") > 0:
        sys.exit(0)                       # F3: bootout not reflected
    del st[label]; save(); sys.exit(0)
if cmd == "bootstrap":
    if counter("bootstrap_fail") > 0:
        sys.stderr.write("Bootstrap failed: 5: Input/output error\n"); sys.exit(5)
    path = sys.argv[3]
    pl = plistlib.load(open(path, "rb"))
    label = pl["Label"]
    if label in st:
        sys.stderr.write("Bootstrap failed: 37: already loaded\n"); sys.exit(37)
    snap = os.path.join(d, label + ".loaded.plist")
    open(snap, "wb").write(open(path, "rb").read())      # the LIVE definition
    st[label] = {"plist": snap, "runs": 0}
    if pl.get("RunAtLoad") and not os.path.exists(os.path.join(d, "no_run")):
        args = pl["ProgramArguments"]
        env = dict(os.environ, **pl.get("EnvironmentVariables", {}))
        env.pop("HOME", None); env["HOME"] = os.environ.get("HOME", "/tmp")
        subprocess.run(args[args.index("--") + 1:], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st[label]["runs"] = 1
    save(); sys.exit(0)
if cmd == "print":
    label = sys.argv[2].rsplit("/", 1)[-1]
    if label not in st:
        sys.stderr.write('Could not find service "%s" in domain\n' % label); sys.exit(113)
    pl = plistlib.load(open(st[label]["plist"], "rb"))
    out = ["%s = {" % sys.argv[2], "\tactive count = 0",
           "\tpath = %s" % st[label]["plist"], "\ttype = LaunchAgent",
           "\tstate = not running", "", "\tprogram = %s" % pl["ProgramArguments"][0],
           "\targuments = {"]
    out += ["\t\t%s" % a for a in pl["ProgramArguments"]]
    out += ["\t}", "", "\tenvironment = {"]
    out += ["\t\t%s => %s" % kv for kv in pl.get("EnvironmentVariables", {}).items()]
    out += ["\t}", "\truns = %d" % st[label]["runs"], "\tlast exit code = 0"]
    if "StartInterval" in pl:
        out.append("\trun interval = %d seconds" % pl["StartInterval"])
    out.append("}")
    print("\n".join(out)); sys.exit(0)
if cmd == "list":
    print("PID\tStatus\tLabel")
    for label in st:
        print("-\t0\t%s" % label)
    sys.exit(0)
sys.stderr.write("stub: refusing %s\n" % " ".join(sys.argv[1:])); sys.exit(64)
PY
chmod +x "$STUB/launchctl"

R6="$TDIR/t6"; make_root "$R6"
LBL6="com.telegram-skill-bot.t6.instance-drive"
export LC_STUB_DIR="$STUB"
ds6() { # <verb> -- drive-setup against the stub
  DOGANY_TSL_LEDGER="$TDIR/t6.jsonl" DRIVE_SETUP_OS=Darwin DRIVE_SETUP_LAUNCHCTL="$STUB/launchctl" \
    DRIVE_SETUP_LA_DIR="$TDIR/la" DRIVE_SETUP_GRACE="${G:-2}" DRIVE_SETUP_POLL=1 \
    bash "$R6/routines/lib/drive-setup.sh" "$1" "$R6" > "$TDIR/ds6.out" 2>&1
}
reset6() { chflags nouappend "$TDIR/t6.jsonl" 2>/dev/null; rm -f "$TDIR/t6.jsonl" "$STUB"/state.json "$STUB"/sticky "$STUB"/bootstrap_fail "$STUB"/no_run "$STUB"/*.loaded.plist; : > "$STUB/calls.log"; : > "$TDIR/t6.jsonl"; }

reset6; ds6 install; rc=$?
if [[ $rc -eq 0 ]] && [[ -f "$TDIR/la/$LBL6.plist" ]] && [[ "$(count_rows "$TDIR/t6.jsonl" terminal-state-sweep sweep)" -eq 1 ]] \
   && [[ "$(plutil -extract StartInterval raw -o - "$R6/routines/$LBL6.plist" 2>/dev/null || python3 -c 'import plistlib,sys; print(plistlib.load(open(sys.argv[1],"rb"))["StartInterval"])' "$R6/routines/$LBL6.plist")" == "$P_PER" ]]; then
  ok "fresh install: rendered StartInterval=$P_PER, loaded, live-verified, first drive beat written through the unit's own argv"
else
  bad "fresh install failed (rc=$rc): $(tail -3 "$TDIR/ds6.out")"
fi
if grep -q "<string>$P_THR</string>" "$R6/routines/$LBL6.plist" && grep -q "<string>$P_TTL</string>" "$R6/routines/$LBL6.plist" \
   && grep -q 'DOGANY_DRIVE_PERIOD' "$R6/routines/$LBL6.plist" && ! grep -q '__[A-Z_]*__' "$R6/routines/$LBL6.plist"; then
  ok "rendered unit records throttle/ttl in argv and period/ttl in its environment (auditable)"
else
  bad "rendered unit lacks the audited policy values"
fi

# reinstall over a loaded unit: confirmed bootout -> bootstrap -> verify
: > "$STUB/calls.log"; ds6 install; rc=$?
if [[ $rc -eq 0 ]] && grep -q '^bootout' "$STUB/calls.log" && grep -q '^bootstrap' "$STUB/calls.log"; then
  ok "reinstall over a loaded drive: bootout confirmed, bootstrapped, verified"
else
  bad "reinstall failed (rc=$rc)"
fi

# F3 measured shape: the first bootout is not reflected -> retried, still ok
echo 1 > "$STUB/sticky"; ds6 install; rc=$?
if [[ $rc -eq 0 ]] && [[ "$(grep -c '^bootout' "$STUB/calls.log")" -ge 3 ]]; then
  ok "F3: an unreflected first bootout is re-issued until print says gone"
else
  bad "sticky-once bootout not handled (rc=$rc)"
fi

# F3: the OLD job stays loaded (bootout never reflected) -> fail closed
reset6
python3 - "$R6/routines/bundle/instance-drive.plist.tpl" "$STUB/old.plist" "$R6" <<'PY'
import sys
t = open(sys.argv[1]).read()
for k, v in {"__LABEL__": "com.telegram-skill-bot.t6.instance-drive", "__ROOT__": sys.argv[3],
             "__HOMEDIR__": "/tmp", "__PERIOD__": "3600", "__THROTTLE__": "900", "__TTL__": "7200"}.items():
    t = t.replace(k, v)
open(sys.argv[2], "w").write(t)
PY
printf '{"%s": {"plist": "%s", "runs": 9}}' "$LBL6" "$STUB/old.plist" > "$STUB/state.json"
ds6 verify; vrc=$?
if [[ $vrc -eq 3 ]] && grep -q "live run interval is '3600', rendered period is $P_PER" "$TDIR/ds6.out"; then
  ok "F3: verify reads the LIVE interval -- an old StartInterval=3600 job fails even though the file on disk is new"
else
  bad "stale live interval passed verify (rc=$vrc): $(cat "$TDIR/ds6.out")"
fi
echo 9 > "$STUB/sticky"; ds6 install; irc=$?
if [[ $irc -eq 3 ]] && grep -q 'not reflected' "$TDIR/ds6.out"; then
  ok "F6: a bootout that never takes effect fails the install (no false 'reloaded')"
else
  bad "sticky bootout did not fail closed (rc=$irc)"
fi

reset6; echo 2 > "$STUB/bootstrap_fail"; ds6 install; brc=$?
if [[ $brc -eq 3 ]] && grep -q 'bootstrap' "$TDIR/ds6.out"; then
  ok "F6: bootstrap failure fails the install (rc 3), not swallowed"
else
  bad "bootstrap failure swallowed (rc=$brc)"
fi

# duplicate drive for the same root, and a foreign-root label
reset6; ds6 install >/dev/null 2>&1
python3 - "$STUB" "$R6" <<'PY'
import json, os, plistlib, sys
d, root = sys.argv[1], sys.argv[2]
st = json.load(open(os.path.join(d, "state.json")))
src = st["com.telegram-skill-bot.t6.instance-drive"]["plist"]
pl = plistlib.load(open(src, "rb")); pl["Label"] = "com.telegram-skill-bot.t6old.instance-drive"
p = os.path.join(d, "dup.plist"); plistlib.dump(pl, open(p, "wb"))
st[pl["Label"]] = {"plist": p, "runs": 3}
json.dump(st, open(os.path.join(d, "state.json"), "w"))
PY
ds6 verify; drc=$?
if [[ $drc -eq 3 ]] && grep -q 'expected exactly one loaded drive' "$TDIR/ds6.out"; then
  ok "two loaded drive labels pointing at one root -> verify fails"
else
  bad "duplicate drive passed (rc=$drc)"
fi
reset6
python3 - "$R6/routines/bundle/instance-drive.plist.tpl" "$STUB/foreign.plist" <<'PY'
import sys
t = open(sys.argv[1]).read()
for k, v in {"__LABEL__": "com.telegram-skill-bot.t6.instance-drive", "__ROOT__": "/elsewhere/other",
             "__HOMEDIR__": "/tmp", "__PERIOD__": "1800", "__THROTTLE__": "900", "__TTL__": "7200"}.items():
    t = t.replace(k, v)
open(sys.argv[2], "w").write(t)
PY
printf '{"%s": {"plist": "%s", "runs": 5}}' "$LBL6" "$STUB/foreign.plist" > "$STUB/state.json"
ds6 verify; frc=$?
if [[ $frc -eq 3 ]] && grep -q 'does not point at this root' "$TDIR/ds6.out"; then
  ok "our label loaded from ANOTHER instance root -> verify fails"
else
  bad "foreign-root drive passed (rc=$frc)"
fi

reset6; : > "$STUB/no_run"; ds6 install; nrc=$?
if [[ $nrc -eq 3 ]] && grep -q 'runs = 0' "$TDIR/ds6.out"; then
  ok "loaded but never ran (runs=0, no beat) -> install fails after the grace window"
else
  bad "a drive that never ran passed (rc=$nrc)"
fi

# a changed TTL re-renders and reloads the live interval in the same run
reset6; ds6 install >/dev/null 2>&1
sed -i.bak 's/^TERMINAL_STATE_SWEEP_TTL_SECONDS = .*/TERMINAL_STATE_SWEEP_TTL_SECONDS = 3600/' \
  "$R6/routines/terminal-state-ledger.py" && rm -f "$R6/routines/terminal-state-ledger.py.bak"
chflags nouappend "$TDIR/t6.jsonl" 2>/dev/null; rm -f "$TDIR/t6.jsonl"; ds6 install; trc=$?
live_ivl="$(DRIVE_SETUP_LAUNCHCTL= "$STUB/launchctl" print "gui/$(id -u)/$LBL6" | sed -n 's/.*run interval = \([0-9]*\) seconds.*/\1/p')"
if [[ $trc -eq 0 && "$live_ivl" == "900" ]] && grep -q '<string>450</string>' "$R6/routines/$LBL6.plist"; then
  ok "TTL 7200 -> 3600: re-rendered (period 900, throttle 450) and the LIVE interval is 900"
else
  bad "TTL change did not reload the live interval (rc=$trc live=$live_ivl)"
fi
cp "$TSL" "$R6/routines/terminal-state-ledger.py"

# sandbox refusal: fake HOME, no seam -> rc 5, launchctl never called
SB="$TDIR/sbbin"; mkdir -p "$SB"
printf '#!/bin/sh\necho "$*" >> "%s/real-calls.log"\nexit 0\n' "$TDIR" > "$SB/launchctl"; chmod +x "$SB/launchctl"
: > "$TDIR/real-calls.log"
HOME="$TDIR/fakehome" PATH="$SB:$PATH" DRIVE_SETUP_OS=Darwin DOGANY_TSL_LEDGER="$TDIR/t6.jsonl" \
  bash "$R6/routines/lib/drive-setup.sh" install "$R6" > "$TDIR/sb.out" 2>&1; sbrc=$?
if [[ $sbrc -eq 5 && ! -s "$TDIR/real-calls.log" ]] && grep -q 'NOT loaded and NOT verified' "$TDIR/sb.out"; then
  ok "sandbox (fake HOME): rc 5, zero launchctl calls, loudly 'not verified'"
else
  bad "sandbox guard leaked (rc=$sbrc calls=$(wc -l < "$TDIR/real-calls.log"))"
fi

# ---------------------------------------------------------------------------
echo "== 7. SYSTEMD: the same contract on Linux =="
# ---------------------------------------------------------------------------
SYS="$TDIR/sc"; mkdir -p "$SYS"
cat > "$SYS/systemctl" <<'PY'
#!/usr/bin/env python3
# systemctl --user stub: daemon-reload snapshots unit files (the LIVE set)
import json, os, re, shlex, subprocess, sys
d = os.environ["SC_STUB_DIR"]; ud = os.environ["SC_UNIT_DIR"]
sp = os.path.join(d, "state.json")
st = json.load(open(sp)) if os.path.exists(sp) else {"units": {}, "active": {}, "fired": {}}
def save(): json.dump(st, open(sp, "w"))
a = [x for x in sys.argv[1:] if x != "--user"]
def kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); out.setdefault(k, []).append(v)
    return out
def fmt(s):
    s = int(s)
    return "%dh" % (s // 3600) if s % 3600 == 0 else ("%dmin" % (s // 60) if s % 60 == 0 else "%ds" % s)
if a[0] == "daemon-reload":
    st["units"] = {n: open(os.path.join(ud, n)).read() for n in os.listdir(ud)}
    save(); sys.exit(0)
if a[0] == "enable":
    sys.exit(0 if a[1] in st["units"] else 1)
if a[0] == "restart":
    t = a[1]
    if t not in st["units"]:
        sys.exit(5)
    st["active"][t] = True
    svc = kv(st["units"][t])["Unit"][0]
    s = kv(st["units"][svc])
    env = dict(os.environ)
    for e in s.get("Environment", []):
        k, v = e.split("=", 1); env[k] = v
    argv = shlex.split(s["ExecStart"][0])
    if not os.path.exists(os.path.join(d, "no_run")):
        subprocess.run(argv[argv.index("--") + 1:], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        st["fired"][t] = True
    save(); sys.exit(0)
if a[0] == "show":
    unit, prop = a[1], a[3]
    text = st["units"].get(unit, "")
    k = kv(text)
    if prop == "ActiveState":
        print("active" if st["active"].get(unit) else "inactive")
    elif prop == "TimersMonotonic":
        v = k.get("OnUnitActiveSec", [""])[0]
        print("{ OnUnitActiveUSec=%s ; next_elapse=0 }" % (fmt(v) if v else ""))
    elif prop == "LastTriggerUSecMonotonic":
        print("123456789" if st["fired"].get(unit) else "0")
    elif prop == "ExecStart":
        e = k.get("ExecStart", [""])[0]
        print("{ path=%s ; argv[]=%s ; ignore_errors=no }" % (e.split(" ")[0], e) if e else "")
    elif prop == "Environment":
        print(" ".join(k.get("Environment", [])))
    sys.exit(0)
if a[0] == "list-units":
    for n in sorted(st["units"]):
        if n.endswith(".timer"):
            print("%s loaded %s waiting x" % (n, "active" if st["active"].get(n) else "inactive"))
    sys.exit(0)
sys.exit(64)
PY
chmod +x "$SYS/systemctl"
R7="$TDIR/t7"; make_root "$R7"
LBL7="com.telegram-skill-bot.t7.instance-drive"
export SC_STUB_DIR="$SYS" SC_UNIT_DIR="$TDIR/units"
ds7() {
  DOGANY_TSL_LEDGER="$TDIR/t7.jsonl" DRIVE_SETUP_OS=Linux DRIVE_SETUP_SYSTEMCTL="$SYS/systemctl" \
    DRIVE_SETUP_UNIT_DIR="$TDIR/units" DRIVE_SETUP_GRACE=2 DRIVE_SETUP_POLL=1 \
    bash "$R7/routines/lib/drive-setup.sh" "$1" "$R7" > "$TDIR/ds7.out" 2>&1
}
: > "$TDIR/t7.jsonl"; ds7 install; rc=$?
if [[ $rc -eq 0 ]] && grep -q "OnUnitActiveSec=$P_PER" "$TDIR/units/$LBL7.timer" \
   && [[ "$(count_rows "$TDIR/t7.jsonl" terminal-state-sweep sweep)" -eq 1 ]]; then
  ok "systemd: rendered OnUnitActiveSec=$P_PER, enabled, restarted, live-verified, first drive beat"
else
  bad "systemd install failed (rc=$rc): $(tail -3 "$TDIR/ds7.out")"
fi
# the unit file changes on disk but nobody reloads -> the LIVE timer is stale
sed -i.bak "s/OnUnitActiveSec=$P_PER/OnUnitActiveSec=3600/" "$TDIR/units/$LBL7.timer" && rm -f "$TDIR/units/$LBL7.timer.bak"
"$SYS/systemctl" --user daemon-reload
cp "$R7/routines/$LBL7.timer" "$TDIR/units/$LBL7.timer"      # file now correct, live still 3600
ds7 verify; vrc=$?
if [[ $vrc -eq 3 ]] && grep -q "live OnUnitActive interval is '3600'" "$TDIR/ds7.out"; then
  ok "systemd F3: verify reads systemctl show, not the file -- a stale live interval fails"
else
  bad "systemd stale live interval passed (rc=$vrc): $(cat "$TDIR/ds7.out")"
fi
ds7 install; irc=$?
if [[ $irc -eq 0 ]]; then
  ok "systemd: install (daemon-reload + restart) heals the stale live timer"
else
  bad "systemd reinstall failed (rc=$irc)"
fi

# ---------------------------------------------------------------------------
echo "== 8. WIRING: mint / start / install / update, never gated =="
# ---------------------------------------------------------------------------
if grep -q 'instance-drive' "$RT/plists.defer" 2>/dev/null; then
  bad "instance-drive is listed in plists.defer (would be deferred)"
else
  ok "instance-drive is not deferred (plists.defer)"
fi
if [[ -z "$REPO_ROOT" ]]; then
  echo "  SKIP: loader wiring checks -- not the canonical repo (.dogany-canonical not found) -- NOT a pass"
else
  MR="$REPO_ROOT/scripts/pack/mint_run.sh"
  grep -q 'bash "$DRIVE_SETUP" render "$PROJECT_ROOT"' "$REPO_ROOT/scripts/mint.sh" \
    && ok "mint.sh renders the drive (fail-closed on policy)" || bad "mint.sh does not render the drive"
  grep -q 'bash "$DRIVE_SETUP" install "$ROOT"' "$MR" && grep -q '\[\[ "$DRIVE_RC" -eq 0 \]\] || exit 1' "$MR" \
    && ok "mint_run.sh start reloads + live-verifies the drive and fails closed" || bad "mint_run.sh start drive wiring missing"
  grep -q 'bash "$_drive_setup" install "$INSTANCE"' "$REPO_ROOT/update.sh" \
    && grep -q 'instance drive could not be verified live' "$REPO_ROOT/update.sh" \
    && ok "update.sh installs the drive and dies when it cannot be verified (F6)" || bad "update.sh drive wiring missing"
  grep -q 'DOGANY_DRIVE_DEFER_TO_START=1' "$REPO_ROOT/scripts/pack/update_apply.sh" \
    && ok "update_apply.sh stop envelope defers drive load+verify to its restart" || bad "update_apply.sh envelope flag missing"
  grep -q '^  install_instance_drive$' "$REPO_ROOT/install.sh" && grep -q 'INSTALL_DRIVE_FAILED" = "1"' "$REPO_ROOT/install.sh" \
    && ok "install.sh loads + verifies the drive and fails the install when it cannot" || bad "install.sh drive wiring missing"

  # kitless + MIRROR_MODULE=off root: start --dry-run still plans the drive
  R8="$TDIR/t8"; make_root "$R8"; mkdir -p "$R8/bridge" "$R8/config"
  printf 'MIRROR_MODULE=off\n' > "$R8/config/agent.conf"
  DRIVE_SETUP_OS=Darwin bash "$R8/routines/lib/drive-setup.sh" render "$R8" >/dev/null 2>&1
  cat > "$R8/bridge/com.telegram-skill-bot.t8.newbridge.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.telegram-skill-bot.t8.newbridge</string></dict></plist>
EOF
  out="$(DOGANY_LAUNCH_AGENTS_DIR="$TDIR/la8" bash "$MR" start --root "$R8" --dry-run 2>&1)"; src=$?
  if [[ $src -eq 0 ]] && printf '%s' "$out" | grep -q 'would render, reload and live-verify the instance drive' \
     && ! printf '%s' "$out" | grep -q 'skip.*instance-drive'; then
    ok "kitless, MIRROR_MODULE=off instance: start still plans the drive (no module/kit gate)"
  else
    bad "drive gated or missing on a kitless/mirror-off start dry-run (rc=$src): $out"
  fi
fi

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] && exit 0
exit 1
