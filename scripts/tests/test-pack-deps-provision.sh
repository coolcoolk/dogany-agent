#!/bin/bash
# test-pack-deps-provision.sh -- fixture-driven tests for the kit<->pack
# dependency provisioning seam (DGN-850): scripts/pack/pack_deps_provision.sh
# plus its pack_install.sh wiring.
#
# Tests:
#   T1  zero-delta: requirements file absent -> no-op, exit 0, no pip call
#   T2  zero-delta: comments/blank-only requirements -> no-op, exit 0
#   T3  install: pip invoked with --user -r <req>; "installed" logged
#   T4  idempotency: already-satisfied -> SKIP logged; two runs both exit 0
#   T5  degrade: pip install failure -> DEGRADE logged, exit 0 (no crash)
#   T6  PEP 668: externally-managed refusal -> loud retry with
#       --break-system-packages (user site only), then success
#   T7  venv guard: a real venv interpreter candidate is SKIPPED
#   T8  dedup: symlinked duplicate candidate collapses to one target
#   T9  concurrency: held lock -> DEGRADE skip, exit 0 (no second writer)
#   T10 pack_install.sh kit-path wiring: requirements present -> deps step in
#       pack-install.log; absent -> explicit no-op line; dry-run -> plan line
#
# pip is SIMULATED via fake interpreter wrappers (never a real network
# install); non-pip invocations (-c probes, flock helper) exec the real
# python3 so venv/lock behavior is genuine.
#
# Exit 0 = all assertions pass; nonzero = at least one failure.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROVISIONER="$SCRIPT_DIR/../pack/pack_deps_provision.sh"
INSTALLER="$SCRIPT_DIR/../pack/pack_install.sh"

REAL_PY="$(command -v python3 || echo /usr/bin/python3)"

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# _mk_fake_py <path> <mode> <calls-log>
# Fake interpreter: intercepts `-m pip ...` per <mode>; everything else execs
# the real python3 (so -c venv probes and the fcntl lock helper are genuine).
# Modes: success | satisfied | fail | extmanaged | nopip
_mk_fake_py() {
  local path="$1" mode="$2" calls="$3"
  cat > "$path" <<FAKE
#!/bin/bash
MODE="$mode"
CALLS="$calls"
REAL="$REAL_PY"
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "pip" ]; then
  shift 2
  echo "\$*" >> "\$CALLS"
  case "\${1:-}" in
    --version)
      [ "\$MODE" = "nopip" ] && exit 1
      echo "pip 99.0 (fake)"; exit 0 ;;
    install)
      case "\$MODE" in
        nopip) exit 1 ;;
        success) echo "Successfully installed holidays-0.83"; exit 0 ;;
        satisfied) echo "Requirement already satisfied: holidays"; exit 0 ;;
        fail) echo "ERROR: fake pip resolution boom"; exit 1 ;;
        extmanaged)
          case " \$* " in
            *" --break-system-packages "*)
              echo "Successfully installed holidays-0.83"; exit 0 ;;
            *)
              echo "error: externally-managed-environment"; exit 1 ;;
          esac ;;
      esac ;;
  esac
  exit 1
fi
exec "\$REAL" "\$@"
FAKE
  chmod +x "$path"
}

# _mk_root <dir> -- minimal instance root
_mk_root() { mkdir -p "$1/.telegram_bot/logs"; }

# _mk_req <file> -- one-dep requirements
_mk_req() { printf '%s\n' "holidays>=0.83" > "$1"; }

# ---------------------------------------------------------------------------
echo "T1: zero-delta -- requirements file absent"
_t1="$(mktemp -d)"; _mk_root "$_t1"
_calls="$_t1/calls.log"; touch "$_calls"
_fp="$_t1/pybin"; mkdir -p "$_fp"; _mk_fake_py "$_fp/python3" success "$_calls"
_out="$(DOGANY_DEPS_PYTHONS="$_fp/python3" bash "$PROVISIONER" --root "$_t1" --requirements "$_t1/nope/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T1: exit 0" || bad "T1: exit $_rc"
grep -q 'no-op: requirements file absent' <<< "$_out" && ok "T1: no-op logged" || bad "T1: no-op line missing: $_out"
[[ ! -s "$_calls" ]] && ok "T1: no pip call made" || bad "T1: pip was called on zero-delta"
grep -q 'no-op' "$_t1/.telegram_bot/logs/pack-deps.log" && ok "T1: pack-deps.log written" || bad "T1: pack-deps.log missing no-op line"

# ---------------------------------------------------------------------------
echo "T2: zero-delta -- comments/blank-only requirements"
_t2="$(mktemp -d)"; _mk_root "$_t2"
printf '# only comments\n\n   \n# more\n' > "$_t2/requirements.txt"
_out="$(DOGANY_DEPS_PYTHONS="$_fp/python3" bash "$PROVISIONER" --root "$_t2" --requirements "$_t2/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T2: exit 0" || bad "T2: exit $_rc"
grep -q 'no-op: requirements file has no requirement lines' <<< "$_out" && ok "T2: empty no-op logged" || bad "T2: empty no-op missing: $_out"

# ---------------------------------------------------------------------------
echo "T3: install -- pip invoked with --user -r"
_t3="$(mktemp -d)"; _mk_root "$_t3"
_calls3="$_t3/calls.log"; touch "$_calls3"
_fp3="$_t3/pybin"; mkdir -p "$_fp3"; _mk_fake_py "$_fp3/python3" success "$_calls3"
_mk_req "$_t3/requirements.txt"
_out="$(DOGANY_DEPS_PYTHONS="$_fp3/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
  bash "$PROVISIONER" --root "$_t3" --requirements "$_t3/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T3: exit 0" || bad "T3: exit $_rc"
grep -q 'installed: holidays-0.83' <<< "$_out" && ok "T3: installed logged" || bad "T3: installed line missing: $_out"
grep -q -- 'install --user .* -r ' "$_calls3" && ok "T3: pip called with --user -r" || bad "T3: pip call shape wrong: $(cat "$_calls3")"
grep -q 'DONE targets=1 installed=1 satisfied=0 degraded=0' <<< "$_out" && ok "T3: summary correct" || bad "T3: summary wrong"

# ---------------------------------------------------------------------------
echo "T4: idempotency -- already satisfied, two runs"
_t4="$(mktemp -d)"; _mk_root "$_t4"
_calls4="$_t4/calls.log"; touch "$_calls4"
_fp4="$_t4/pybin"; mkdir -p "$_fp4"; _mk_fake_py "$_fp4/python3" satisfied "$_calls4"
_mk_req "$_t4/requirements.txt"
for _i in 1 2; do
  _out="$(DOGANY_DEPS_PYTHONS="$_fp4/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
    bash "$PROVISIONER" --root "$_t4" --requirements "$_t4/requirements.txt" 2>&1)"; _rc=$?
  [[ $_rc -eq 0 ]] && ok "T4: run $_i exit 0" || bad "T4: run $_i exit $_rc"
  grep -q 'SKIP: all requirements already satisfied' <<< "$_out" && ok "T4: run $_i SKIP logged" || bad "T4: run $_i SKIP missing"
  grep -q 'DONE targets=1 installed=0 satisfied=1 degraded=0' <<< "$_out" && ok "T4: run $_i summary satisfied" || bad "T4: run $_i summary wrong"
done

# ---------------------------------------------------------------------------
echo "T5: degrade -- pip install failure, no crash"
_t5="$(mktemp -d)"; _mk_root "$_t5"
_calls5="$_t5/calls.log"; touch "$_calls5"
_fp5="$_t5/pybin"; mkdir -p "$_fp5"; _mk_fake_py "$_fp5/python3" fail "$_calls5"
_mk_req "$_t5/requirements.txt"
_out="$(DOGANY_DEPS_PYTHONS="$_fp5/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
  bash "$PROVISIONER" --root "$_t5" --requirements "$_t5/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T5: exit 0 despite pip failure" || bad "T5: exit $_rc (must degrade, not crash)"
grep -q 'DEGRADE: pip install failed' <<< "$_out" && ok "T5: DEGRADE logged" || bad "T5: DEGRADE line missing: $_out"
grep -q 'DONE targets=1 installed=0 satisfied=0 degraded=1' <<< "$_out" && ok "T5: summary degraded" || bad "T5: summary wrong"

# ---------------------------------------------------------------------------
echo "T6: PEP 668 externally-managed -- loud --break-system-packages retry"
_t6="$(mktemp -d)"; _mk_root "$_t6"
_calls6="$_t6/calls.log"; touch "$_calls6"
_fp6="$_t6/pybin"; mkdir -p "$_fp6"; _mk_fake_py "$_fp6/python3" extmanaged "$_calls6"
_mk_req "$_t6/requirements.txt"
_out="$(DOGANY_DEPS_PYTHONS="$_fp6/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
  bash "$PROVISIONER" --root "$_t6" --requirements "$_t6/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T6: exit 0" || bad "T6: exit $_rc"
grep -q 'retrying with --break-system-packages' <<< "$_out" && ok "T6: retry logged loudly" || bad "T6: retry line missing: $_out"
grep -q 'installed: holidays-0.83' <<< "$_out" && ok "T6: retry succeeded" || bad "T6: retry did not install"
grep -q -- '--break-system-packages' "$_calls6" && ok "T6: retry call carried the flag" || bad "T6: flag missing in pip calls"

# ---------------------------------------------------------------------------
echo "T7: venv guard -- venv interpreter candidate skipped"
_t7="$(mktemp -d)"; _mk_root "$_t7"
_calls7="$_t7/calls.log"; touch "$_calls7"
_fp7="$_t7/pybin"; mkdir -p "$_fp7"; _mk_fake_py "$_fp7/python3" success "$_calls7"
_mk_req "$_t7/requirements.txt"
if "$REAL_PY" -m venv --without-pip "$_t7/venv" >/dev/null 2>&1 && [[ -x "$_t7/venv/bin/python3" ]]; then
  _out="$(DOGANY_DEPS_PYTHONS="$_t7/venv/bin/python3:$_fp7/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
    bash "$PROVISIONER" --root "$_t7" --requirements "$_t7/requirements.txt" 2>&1)"; _rc=$?
  [[ $_rc -eq 0 ]] && ok "T7: exit 0" || bad "T7: exit $_rc"
  grep -q 'venv interpreter, not a runtime target' <<< "$_out" && ok "T7: venv candidate skipped" || bad "T7: venv skip line missing: $_out"
  grep -q 'DONE targets=1 ' <<< "$_out" && ok "T7: only the runtime target provisioned" || bad "T7: target count wrong"
else
  bad "T7: could not build venv fixture with $REAL_PY"
fi

# ---------------------------------------------------------------------------
echo "T8: dedup -- symlinked duplicate collapses to one target"
_t8="$(mktemp -d)"; _mk_root "$_t8"
_calls8="$_t8/calls.log"; touch "$_calls8"
_fp8="$_t8/pybin"; mkdir -p "$_fp8"; _mk_fake_py "$_fp8/python3" success "$_calls8"
ln -s "$_fp8/python3" "$_fp8/python3-alias"
_mk_req "$_t8/requirements.txt"
_out="$(DOGANY_DEPS_PYTHONS="$_fp8/python3:$_fp8/python3-alias" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
  bash "$PROVISIONER" --root "$_t8" --requirements "$_t8/requirements.txt" 2>&1)"; _rc=$?
[[ $_rc -eq 0 ]] && ok "T8: exit 0" || bad "T8: exit $_rc"
grep -q 'candidate deduped' <<< "$_out" && ok "T8: dedup logged" || bad "T8: dedup line missing: $_out"
grep -q 'DONE targets=1 ' <<< "$_out" && ok "T8: single target after dedup" || bad "T8: target count wrong"

# ---------------------------------------------------------------------------
echo "T9: concurrency -- held lock degrades to skip"
_t9="$(mktemp -d)"; _mk_root "$_t9"
_calls9="$_t9/calls.log"; touch "$_calls9"
_fp9="$_t9/pybin"; mkdir -p "$_fp9"; _mk_fake_py "$_fp9/python3" success "$_calls9"
_mk_req "$_t9/requirements.txt"
"$REAL_PY" - "$_t9/.telegram_bot/pack-deps.lock" <<'PY' &
import fcntl, sys, time
f = open(sys.argv[1], "w")
fcntl.flock(f, fcntl.LOCK_EX)
time.sleep(20)
PY
_holder=$!
sleep 1
_out="$(DOGANY_DEPS_PYTHONS="$_fp9/python3" DOGANY_DEPS_LOCK_RETRIES=2 DOGANY_DEPS_LOCK_INTERVAL=0 \
  bash "$PROVISIONER" --root "$_t9" --requirements "$_t9/requirements.txt" 2>&1)"; _rc=$?
kill "$_holder" 2>/dev/null; wait "$_holder" 2>/dev/null
[[ $_rc -eq 0 ]] && ok "T9: exit 0" || bad "T9: exit $_rc"
grep -q 'DEGRADE: another provisioner holds the lock' <<< "$_out" && ok "T9: lock degrade logged" || bad "T9: lock degrade missing: $_out"
grep -q 'install ' "$_calls9" && bad "T9: pip ran despite held lock" || ok "T9: no pip call under held lock"

# ---------------------------------------------------------------------------
echo "T10: pack_install.sh kit-path wiring"
# Minimal kit pack fixture (mirrors test-pack-install-kit.sh shape so
# compat-lint passes: file-number == pin == user_version == 1).
_mk_kit_pack() { # _mk_kit_pack <dir> <with_req 0|1>
  local dir="$1" with_req="$2"
  mkdir -p "$dir/payload/database/migrations"
  cat > "$dir/pack-manifest.json" <<'MANIFEST'
{
  "id": "lifekit",
  "name_en": "Lifekit",
  "kind": "kit",
  "provides_kit": "lifekit",
  "pack_version": "1.0.0",
  "contract_version": 1,
  "requires_framework": ">=1.0.0 <99.0.0",
  "payload_root": "payload",
  "deploy_owner": "skull",
  "status": "test"
}
MANIFEST
  printf '%s\n' "# stub" "EXPECTED_USER_VERSION = 1" > "$dir/payload/database/lifekit.py"
  cat > "$dir/payload/database/lifekit.sh" <<'SH'
#!/bin/bash
# lifekit.sh stub: CLI contract surface for tests (exit 0 on check/dump)
case "${1:-}" in
  check|dump) exit 0 ;;
  *) exit 1 ;;
esac
SH
  chmod +x "$dir/payload/database/lifekit.sh"
  printf '%s\n' 'PRAGMA user_version = 1;' 'CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);' > "$dir/payload/database/schema.sql"
  printf '%s\n' '-- reversible: yes' 'CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);' > "$dir/payload/database/migrations/001_init.sql"
  if [[ "$with_req" -eq 1 ]]; then
    printf '%s\n' "holidays>=0.83" > "$dir/payload/requirements.txt"
  fi
}
_mk_kit_inst() { # _mk_kit_inst <dir>
  mkdir -p "$1/.telegram_bot/logs" "$1/database" "$1/config"
  echo "DOGANY_SLUG=test" > "$1/.instance.conf"
}

_t10="$(mktemp -d)"
_calls10="$_t10/calls.log"; touch "$_calls10"
_fp10="$_t10/pybin"; mkdir -p "$_fp10"; _mk_fake_py "$_fp10/python3" success "$_calls10"

# (a) requirements present -> deps step + installed line in pack-install.log
_p10a="$_t10/pack-a"; _i10a="$_t10/inst-a"
_mk_kit_pack "$_p10a" 1; _mk_kit_inst "$_i10a"
if DOGANY_DEPS_PYTHONS="$_fp10/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
   bash "$INSTALLER" test-slug "$_i10a" --pack lifekit --pack-dir "$_p10a" --no-start --no-state >/dev/null 2>&1; then
  ok "T10a: kit install with requirements exits 0"
else
  bad "T10a: kit install failed"
fi
_il="$_i10a/.telegram_bot/logs/pack-install.log"
grep -q 'step deps-provision (DGN-850)' "$_il" 2>/dev/null && ok "T10a: deps step in pack-install.log" || bad "T10a: deps step missing in log"
grep -q 'installed: holidays-0.83' "$_il" 2>/dev/null && ok "T10a: install result teed into pack-install.log" || bad "T10a: install result missing in log"
grep -q 'install --user' "$_calls10" && ok "T10a: pip call went through the seam" || bad "T10a: no pip call recorded"

# (b) requirements absent -> explicit no-op (zero-delta), install still OK
_p10b="$_t10/pack-b"; _i10b="$_t10/inst-b"
_mk_kit_pack "$_p10b" 0; _mk_kit_inst "$_i10b"
if DOGANY_DEPS_PYTHONS="$_fp10/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
   bash "$INSTALLER" test-slug "$_i10b" --pack lifekit --pack-dir "$_p10b" --no-start --no-state >/dev/null 2>&1; then
  ok "T10b: kit install without requirements exits 0"
else
  bad "T10b: kit install failed"
fi
grep -q 'no-op: requirements file absent' "$_i10b/.telegram_bot/logs/pack-install.log" 2>/dev/null \
  && ok "T10b: zero-delta no-op logged" || bad "T10b: zero-delta no-op missing"

# (c) degraded pip must NOT fail the kit install (graceful contract)
_p10c="$_t10/pack-c"; _i10c="$_t10/inst-c"
_fp10c="$_t10/pybin-fail"; mkdir -p "$_fp10c"; _mk_fake_py "$_fp10c/python3" fail "$_t10/calls-c.log"
_mk_kit_pack "$_p10c" 1; _mk_kit_inst "$_i10c"
if DOGANY_DEPS_PYTHONS="$_fp10c/python3" DOGANY_DEPS_LOCK_RETRIES=1 DOGANY_DEPS_LOCK_INTERVAL=0 \
   bash "$INSTALLER" test-slug "$_i10c" --pack lifekit --pack-dir "$_p10c" --no-start --no-state >/dev/null 2>&1; then
  ok "T10c: kit install survives pip degrade (exit 0)"
else
  bad "T10c: pip degrade crashed the kit install"
fi
grep -q 'DEGRADE: pip install failed' "$_i10c/.telegram_bot/logs/pack-install.log" 2>/dev/null \
  && ok "T10c: degrade logged in pack-install.log" || bad "T10c: degrade line missing"

# (d) dry-run carries the deps plan line, no writes
_out="$(bash "$INSTALLER" test-slug "$_i10a" --pack lifekit --pack-dir "$_p10a" --no-start --no-state --dry-run 2>&1)"
grep -q 'deps-provision: payload/requirements.txt present' <<< "$_out" \
  && ok "T10d: dry-run plan line present" || bad "T10d: dry-run plan line missing: $_out"

# ---------------------------------------------------------------------------
echo ""
echo "RESULT: pass=$PASS fail=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
