#!/bin/bash
# test-pack-install-requires-kit.sh -- fixture-driven tests for
#   (A) the DGN-681 S4b-1 §2-c install-time requires_kit satisfaction gate
#       (pre-payload-copy, fail-closed, NO warn lane), and
#   (B) the TK-13 U1 atomic DOGANY_PACKS upsert primitive
#       (flock + tmp/fsync/rename; both call sites co-repaired)
# in scripts/pack/pack_install.sh.
#
# Gate verdict-table rows (DGN-681-S4b-RATIFIED §3-B, one test per row +
# every form-damage sub-case):
#   G1  requires_kit absent                       -> SKIP, install proceeds
#   G2  form: non-object (array)                  -> BLOCK
#   G3  form: missing key (kit only)              -> BLOCK
#   G4  form: extra key                           -> BLOCK
#   G5  form: kit grammar violation               -> BLOCK
#   G6  form: kit framework-reserved name         -> BLOCK
#   G7  form: self-dep (kit == own id)            -> BLOCK
#   G8  form: range grammar violation (^)         -> BLOCK
#   G9  form: kind=kit declares requires_kit      -> BLOCK
#   G10 .instance.conf absent                     -> BLOCK
#   G11 DOGANY_PACKS line absent                  -> BLOCK
#   G12 no <kit>@ entry                           -> BLOCK
#   G13 entry version unparseable (unversioned)   -> BLOCK
#   G13b entry without '@' at all                 -> BLOCK (same row)
#   G14 installed version outside range           -> BLOCK
#   G15 satisfied                                 -> PASS + kit@version log
#   G16 Warg live-shape regression (conf values copied from the live file
#       into a fixture; live instance untouched): lifekit@1.2.0 vs
#       ">=1.1.0 <2.0.0"                          -> PASS
#   G17 legacy pack (no contract_version)         -> gate not applicable
#       (mirrors compat-lint legacy-grace; dev-pack class regression guard)
# BLOCK cases run WITHOUT --dry-run and assert the instance tree is untouched
# (pre-payload-copy proof).
#
# Atomicity (TK-13 RESPEC §5.1 / gate G3):
#   A1  single-writer byte-identity: full kit install upserts DOGANY_PACKS
#       with EXACTLY the pre-U1 content semantics (other entries preserved,
#       exact final bytes asserted) and preserves the conf file mode.
#   A2  crash injection: SIGKILL inside the write window (test-only delay
#       hook between fsync and rename) -> .instance.conf is byte-identical
#       to the original -- never truncated/partial.
#   A3  concurrent writers: writer 1 holds the lock inside the critical
#       section while writer 2 upserts a different pack id -> BOTH entries
#       present (lost-update prevention; rename alone cannot provide this).
#   A4  lock timeout: an external flock holder + DOGANY_CONF_LOCK_TIMEOUT=1
#       -> loud FAIL, non-zero exit, conf untouched.
#
# Exit 0 = all assertions pass. Uses mktemp -d everywhere; NEVER touches live
# instance paths (Ag, Warg, ...) -- the Warg conf is read-only copied.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/../pack/pack_install.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# _mk_inst <dir> [packs-line|NONE|NOCONF]
# Minimal instance fixture. Third form controls .instance.conf:
#   default          -> conf with DOGANY_PACKS=lifekit@1.2.0
#   "<literal line>" -> conf with that DOGANY_PACKS line verbatim
#   NONE             -> conf WITHOUT any DOGANY_PACKS line
#   NOCONF           -> no .instance.conf at all
_mk_inst() {
  local dir="$1" packs="${2:-DOGANY_PACKS=lifekit@1.2.0}"
  mkdir -p "$dir/.telegram_bot/logs" "$dir/bridge" "$dir/.claude" \
           "$dir/database" "$dir/service" "$dir/config"
  echo '{}' > "$dir/.claude/settings.json"
  if [[ "$packs" == "NOCONF" ]]; then
    return 0
  fi
  {
    echo "DOGANY_AGENT_NAME=testslug"
    echo "DOGANY_TIER=lite"
    [[ "$packs" != "NONE" ]] && echo "$packs"
  } > "$dir/.instance.conf"
}

# _mk_bpack <dir> <requires_kit-json|ABSENT> [kind] [contract|LEGACY]
# Contract behavior-pack fixture (agent/module dispatch path). The manifest
# carries just enough for the pre-dispatch gate plus agent-path dry-run
# preflight (categories payload optional -> skip).
_mk_bpack() {
  local dir="$1" rk="$2" kind="${3:-pack}" contract="${4:-1}"
  mkdir -p "$dir/refslug"
  {
    echo '{'
    echo '  "id": "test-bpack",'
    echo '  "name": "Test behavior pack",'
    echo "  \"kind\": \"$kind\","
    [[ "$contract" != "LEGACY" ]] && echo "  \"contract_version\": $contract,"
    echo '  "pack_version": "0.1.0",'
    echo '  "requires_framework": ">=1.0.0 <99.0.0",'
    [[ "$rk" != "ABSENT" ]] && echo "  \"requires_kit\": $rk,"
    echo '  "reference_slug": "refslug",'
    echo '  "reference_root": "/tmp/nonexistent-ref-root",'
    echo '  "categories": [ {"category": "routines"} ]'
    echo '}'
  } > "$dir/pack-manifest.json"
  python3 -m json.tool "$dir/pack-manifest.json" >/dev/null || {
    echo "FIXTURE BUG: manifest not valid JSON: $dir" >&2; exit 99; }
}

# _mk_kit <dir> <kit> [extra-manifest-lines-file]
# Kit-class fixture that passes install-side compat-lint (published + D-D row
# in the fixture catalog). Mirrors test-pack-install-kit.sh _mk_pack minimally.
_mk_kit() {
  local dir="$1" kit="$2"
  mkdir -p "$dir/payload/database/migrations" "$dir/payload/service/$kit" \
           "$dir/payload/config"
  cat > "$dir/pack-manifest.json" <<MANIFEST
{
  "id": "$kit",
  "name_en": "Testkit",
  "kind": "kit",
  "provides_kit": "$kit",
  "pack_version": "1.0.0",
  "contract_version": 1,
  "requires_framework": ">=1.0.0 <99.0.0",
  "payload_root": "payload",
  "deploy_owner": "skull",
  "status": "published"
}
MANIFEST
  cat > "$dir/payload/database/$kit.py" <<PY
# $kit.py stub for tests
EXPECTED_USER_VERSION = 1
PY
  cat > "$dir/payload/database/$kit.sh" <<'SH'
#!/bin/bash
case "${1:-}" in check|dump) exit 0 ;; *) exit 1 ;; esac
SH
  chmod +x "$dir/payload/database/$kit.sh"
  cat > "$dir/payload/database/schema.sql" <<SQL
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL
  cat > "$dir/payload/database/migrations/001_init.sql" <<SQL
-- reversible: yes
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL
  echo "KEY_A=val_a" > "$dir/payload/config/$kit.conf"
}

FIXTURE_CATALOG_DIR="$(mktemp -d)"
cat > "$FIXTURE_CATALOG_DIR/catalog.json" <<'CAT'
{
  "version": 1,
  "packs": [
    { "id": "lifekit", "kind": "kit", "package_dir": "unused", "status": "published" },
    { "id": "fookit",  "kind": "kit", "package_dir": "unused", "status": "published" }
  ]
}
CAT

# _run <pack_dir> <inst> <pack_id> [extra flags...] -- combined output, rc preserved
_run() {
  local pack="$1" inst="$2" pid="$3"; shift 3
  bash "$INSTALLER" "testslug" "$inst" \
    --pack "$pid" --pack-dir "$pack" \
    --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
    --no-start --no-state "$@" 2>&1
}

# _tree_snapshot <dir> -- stable listing of the instance tree + conf bytes
_tree_snapshot() {
  ( cd "$1" && find . -not -path './.telegram_bot/logs*' | sort
    [[ -f .instance.conf ]] && cat .instance.conf )
}

# _expect_block <label> <out> <rc> <needle>
_expect_block() {
  local label="$1" out="$2" rc="$3" needle="$4"
  if [[ "$rc" -ne 0 ]] && grep -qF "requires_kit gate" <<<"$out" \
     && grep -qF "$needle" <<<"$out"; then
    ok "$label: BLOCK (rc=$rc, reason surfaced)"
  else
    bad "$label: expected BLOCK with '$needle' (rc=$rc); output: $(tail -3 <<<"$out")"
  fi
}

RK_OK='{ "kit": "lifekit", "range": ">=1.1.0 <2.0.0" }'

# ---------------------------------------------------------------------------
echo "G1: requires_kit absent -> SKIP (contract pack, dry-run proceeds)"
_g1p="$(mktemp -d)"; _g1i="$(mktemp -d)"
_mk_bpack "$_g1p" ABSENT
_mk_inst "$_g1i"
_out="$(_run "$_g1p" "$_g1i" test-bpack --dry-run)"; _rc=$?
if grep -qF "requires_kit gate: SKIP -- requires_kit absent" <<<"$_out" && [[ "$_rc" -eq 0 ]] \
   && grep -qF "DRY-RUN OK" <<<"$_out"; then
  ok "G1: SKIP + dry-run OK (rc=0)"
else
  bad "G1: expected SKIP + DRY-RUN OK (rc=$_rc); output: $(tail -3 <<<"$_out")"
fi

echo "G2..G8: form damage -> BLOCK (pre-payload-copy, instance untouched)"
_block_form_case() { # <label> <rk-json> <needle>
  local label="$1" rk="$2" needle="$3"
  local p i before after
  p="$(mktemp -d)"; i="$(mktemp -d)"
  _mk_bpack "$p" "$rk"
  _mk_inst "$i"
  before="$(_tree_snapshot "$i")"
  _out="$(_run "$p" "$i" test-bpack)"; _rc=$?
  _expect_block "$label" "$_out" "$_rc" "$needle"
  after="$(_tree_snapshot "$i")"
  if [[ "$before" == "$after" ]]; then
    ok "$label: instance tree untouched (blocked before payload copy)"
  else
    bad "$label: instance tree CHANGED on BLOCK"
  fi
}
_block_form_case "G2 non-object"    '["lifekit"]'                                        "must be a JSON object"
_block_form_case "G3 missing key"   '{ "kit": "lifekit" }'                               "keys must be exactly {kit, range}"
_block_form_case "G4 extra key"     '{ "kit": "lifekit", "range": ">=1.1.0 <2.0.0", "optional": true }' "keys must be exactly {kit, range}"
_block_form_case "G5 kit grammar"   '{ "kit": "Lifekit!", "range": ">=1.1.0 <2.0.0" }'   "requires_kit.kit invalid"
_block_form_case "G6 reserved name" '{ "kit": "bridge", "range": ">=1.1.0 <2.0.0" }'     "framework-reserved name"
_block_form_case "G7 self-dep"      '{ "kit": "test-bpack", "range": ">=1.1.0 <2.0.0" }' "self-dependency"
_block_form_case "G8 range grammar" '{ "kit": "lifekit", "range": "^1.1.0" }'            "not a valid semver range"

echo "G9: kind=kit declares requires_kit -> BLOCK"
_g9p="$(mktemp -d)"; _g9i="$(mktemp -d)"
_mk_kit "$_g9p" lifekit
python3 - "$_g9p/pack-manifest.json" <<'PYEOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["requires_kit"] = {"kit": "otherkit", "range": ">=1.0.0 <2.0.0"}
json.dump(d, open(p, "w"), indent=2)
PYEOF
_mk_inst "$_g9i"
_out="$(_run "$_g9p" "$_g9i" lifekit)"; _rc=$?
_expect_block "G9 kind=kit" "$_out" "$_rc" "kind=kit must not declare requires_kit"

echo "G10..G14: satisfaction rows -> BLOCK"
_block_sat_case() { # <label> <inst-conf-mode> <needle>
  local label="$1" mode="$2" needle="$3"
  local p i
  p="$(mktemp -d)"; i="$(mktemp -d)"
  _mk_bpack "$p" "$RK_OK"
  _mk_inst "$i" "$mode"
  _out="$(_run "$p" "$i" test-bpack)"; _rc=$?
  _expect_block "$label" "$_out" "$_rc" "$needle"
}
_block_sat_case "G10 conf absent"        "NOCONF"                              ".instance.conf not found"
_block_sat_case "G11 no DOGANY_PACKS"    "NONE"                                "no DOGANY_PACKS line"
_block_sat_case "G12 no kit entry"       "DOGANY_PACKS=otherkit@1.0.0"         "no <kit>@ entry"
_block_sat_case "G13 unversioned"        "DOGANY_PACKS=lifekit@unversioned"    "unparseable version"
_block_sat_case "G13b bare entry (no @)" "DOGANY_PACKS=lifekit"                "unparseable version"
_block_sat_case "G14 outside range"      "DOGANY_PACKS=lifekit@2.5.0"          "does not satisfy required range"

echo "G15: satisfied -> PASS + kit@version log"
_g15p="$(mktemp -d)"; _g15i="$(mktemp -d)"
_mk_bpack "$_g15p" "$RK_OK"
_mk_inst "$_g15i" "DOGANY_PACKS=lifekit@1.2.0,health-trainer@0.1.0"
_out="$(_run "$_g15p" "$_g15i" test-bpack --dry-run)"; _rc=$?
if grep -qF "requires_kit gate: PASS -- lifekit@1.2.0 (range '>=1.1.0 <2.0.0' satisfied)" <<<"$_out" \
   && [[ "$_rc" -eq 0 ]] && grep -qF "DRY-RUN OK" <<<"$_out"; then
  ok "G15: PASS with kit@version logged, dry-run OK"
else
  bad "G15: expected PASS lifekit@1.2.0 (rc=$_rc); output: $(tail -3 <<<"$_out")"
fi

echo "G16: Warg live-shape regression (fixture copy; live untouched)"
_WARG_CONF="$HOME/.dogany/agents/Warg/.instance.conf"
if [[ -f "$_WARG_CONF" ]]; then
  _g16p="$(mktemp -d)"; _g16i="$(mktemp -d)"
  _mk_bpack "$_g16p" "$RK_OK"
  mkdir -p "$_g16i/.telegram_bot/logs" "$_g16i/bridge" "$_g16i/.claude"
  echo '{}' > "$_g16i/.claude/settings.json"
  cp "$_WARG_CONF" "$_g16i/.instance.conf"   # read-only source, fixture target
  _out="$(_run "$_g16p" "$_g16i" test-bpack --dry-run)"; _rc=$?
  if grep -qE "requires_kit gate: PASS -- lifekit@[0-9.]+ \(range '>=1.1.0 <2.0.0' satisfied\)" <<<"$_out" \
     && [[ "$_rc" -eq 0 ]]; then
    ok "G16: Warg real DOGANY_PACKS data -> PASS ($(grep -oE 'lifekit@[0-9.]+' <<<"$_out" | head -1))"
  else
    bad "G16: expected PASS on Warg-shape conf (rc=$_rc); output: $(tail -3 <<<"$_out")"
  fi
else
  echo "  (skip) Warg .instance.conf not present on this host"
fi

echo "G17: legacy pack (no contract_version, kind!=kit) -> gate not applicable"
_g17p="$(mktemp -d)"; _g17i="$(mktemp -d)"
_mk_bpack "$_g17p" ABSENT pack LEGACY
_mk_inst "$_g17i"
_out="$(_run "$_g17p" "$_g17i" test-bpack --dry-run)"; _rc=$?
if grep -qF "requires_kit gate: SKIP -- legacy pack (no contract_version)" <<<"$_out" \
   && [[ "$_rc" -eq 0 ]] && grep -qF "DRY-RUN OK" <<<"$_out"; then
  ok "G17: legacy-grace mirror SKIP + dry-run OK"
else
  bad "G17: expected legacy SKIP (rc=$_rc); output: $(tail -3 <<<"$_out")"
fi

# ---------------------------------------------------------------------------
echo "A1: atomic upsert -- single-writer byte-identity + mode preserved"
_a1p="$(mktemp -d)"; _a1i="$(mktemp -d)"
_mk_kit "$_a1p" lifekit
_mk_inst "$_a1i" "DOGANY_PACKS=other@1.0.0"
chmod 644 "$_a1i/.instance.conf"
_out="$(_run "$_a1p" "$_a1i" lifekit)"; _rc=$?
_expected=$'DOGANY_AGENT_NAME=testslug\nDOGANY_TIER=lite\nDOGANY_PACKS=other@1.0.0,lifekit@1.0.0\n'
_actual="$(cat "$_a1i/.instance.conf"; printf x)"; _actual="${_actual%x}"
if [[ "$_rc" -eq 0 && "$_actual" == "$_expected" ]]; then
  ok "A1: full kit install green + conf bytes EXACTLY match pre-U1 semantics"
else
  bad "A1: rc=$_rc; conf bytes: $(printf '%q' "$_actual")"
fi
_mode="$(stat -f '%Lp' "$_a1i/.instance.conf" 2>/dev/null || stat -c '%a' "$_a1i/.instance.conf")"
if [[ "$_mode" == "644" ]]; then
  ok "A1: conf file mode preserved (644)"
else
  bad "A1: conf file mode changed: $_mode"
fi
# Idempotency: run again, bytes stable.
_out="$(_run "$_a1p" "$_a1i" lifekit)"; _rc=$?
_actual2="$(cat "$_a1i/.instance.conf"; printf x)"; _actual2="${_actual2%x}"
if [[ "$_rc" -eq 0 && "$_actual2" == "$_expected" ]]; then
  ok "A1: reinstall idempotent (bytes stable)"
else
  bad "A1: reinstall changed bytes (rc=$_rc)"
fi

echo "A2: crash injection -- SIGKILL in the write window leaves conf intact"
_a2p="$(mktemp -d)"; _a2i="$(mktemp -d)"
_mk_kit "$_a2p" lifekit
_mk_inst "$_a2i" "DOGANY_PACKS=other@1.0.0"
_orig="$(cat "$_a2i/.instance.conf")"
DOGANY_TEST_ATOMIC_UPSERT_DELAY=6 _run "$_a2p" "$_a2i" lifekit >/dev/null 2>&1 &
_a2pid=$!
_hit=0
for _ in $(seq 1 600); do  # up to 30s for the install to reach the upsert
  if compgen -G "$_a2i/.instance.conf.tmp.*" >/dev/null 2>&1; then _hit=1; break; fi
  sleep 0.05
done
if [[ "$_hit" -eq 1 ]]; then
  # tmp exists = data fully written + fsynced, rename NOT yet done (delay
  # window). Kill -9 the upsert python mid-window: the classic partial-write
  # crash point for the pre-U1 write_text implementation.
  pkill -9 -f "$_a2i/.instance.conf" 2>/dev/null
  kill -9 "$_a2pid" 2>/dev/null
  wait "$_a2pid" 2>/dev/null
  _now="$(cat "$_a2i/.instance.conf")"
  if [[ "$_now" == "$_orig" ]]; then
    ok "A2: killed in write window -- conf byte-identical to original (no truncation)"
  else
    bad "A2: conf CHANGED/CORRUPTED after kill: $(printf '%q' "$_now")"
  fi
  if python3 -c "
import sys
for ln in open(sys.argv[1]):
    if not ln.rstrip('\n'):
        continue
    if '=' not in ln and not ln.startswith('#'):
        sys.exit(1)
" "$_a2i/.instance.conf"; then
    ok "A2: conf still structurally parseable (KEY=VALUE lines only)"
  else
    bad "A2: conf structurally broken after kill"
  fi
else
  kill -9 "$_a2pid" 2>/dev/null; wait "$_a2pid" 2>/dev/null
  bad "A2: install never reached the upsert write window (tmp file not observed)"
fi

echo "A3: concurrent writers -- second upsert preserves the first (flock)"
_a3i="$(mktemp -d)"
_a3k1="$(mktemp -d)"; _a3k2="$(mktemp -d)"
_mk_kit "$_a3k1" lifekit
_mk_kit "$_a3k2" fookit
_mk_inst "$_a3i" "DOGANY_PACKS=other@1.0.0"
# Writer 1 sleeps 6s INSIDE the flock critical section (post-read, pre-rename).
DOGANY_TEST_ATOMIC_UPSERT_DELAY=6 _run "$_a3k1" "$_a3i" lifekit >/dev/null 2>&1 &
_w1=$!
_hit=0
for _ in $(seq 1 600); do
  if compgen -G "$_a3i/.instance.conf.tmp.*" >/dev/null 2>&1; then _hit=1; break; fi
  sleep 0.05
done
if [[ "$_hit" -eq 1 ]]; then
  # Writer 2 starts while writer 1 holds the lock. Without flock it would
  # read the pre-writer-1 conf and one of the two entries would be lost.
  _run "$_a3k2" "$_a3i" fookit >/dev/null 2>&1 &
  _w2=$!
  wait "$_w1"; _rc1=$?
  wait "$_w2"; _rc2=$?
  _line="$(grep '^DOGANY_PACKS=' "$_a3i/.instance.conf")"
  if [[ "$_rc1" -eq 0 && "$_rc2" -eq 0 ]] \
     && grep -q 'lifekit@1.0.0' <<<"$_line" \
     && grep -q 'fookit@1.0.0' <<<"$_line" \
     && grep -q 'other@1.0.0' <<<"$_line" \
     && [[ "$(grep -c '^DOGANY_PACKS=' "$_a3i/.instance.conf")" -eq 1 ]]; then
    ok "A3: both writers landed, zero lost updates: $_line"
  else
    bad "A3: lost update or failure (rc1=$_rc1 rc2=$_rc2): $_line"
  fi
else
  kill -9 "$_w1" 2>/dev/null; wait "$_w1" 2>/dev/null
  bad "A3: writer 1 never reached the upsert window"
fi

echo "A4: lock timeout -> loud FAIL, conf untouched"
_a4p="$(mktemp -d)"; _a4i="$(mktemp -d)"
_mk_kit "$_a4p" lifekit
_mk_inst "$_a4i" "DOGANY_PACKS=other@1.0.0"
_orig="$(cat "$_a4i/.instance.conf")"
python3 - "$_a4i/.instance.conf.lock" <<'PYEOF' &
import fcntl, sys, time
fh = open(sys.argv[1], "a")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
time.sleep(20)
PYEOF
_holder=$!
sleep 0.5   # let the holder grab the lock
_out="$(DOGANY_CONF_LOCK_TIMEOUT=1 _run "$_a4p" "$_a4i" lifekit)"; _rc=$?
kill "$_holder" 2>/dev/null; wait "$_holder" 2>/dev/null
if [[ "$_rc" -ne 0 ]] && grep -qF "could not acquire" <<<"$_out"; then
  ok "A4: timeout FAILED LOUDLY (rc=$_rc)"
else
  bad "A4: expected loud timeout failure (rc=$_rc); output: $(tail -3 <<<"$_out")"
fi
if [[ "$(cat "$_a4i/.instance.conf")" == "$_orig" ]]; then
  ok "A4: conf untouched on lock-timeout failure"
else
  bad "A4: conf CHANGED on lock-timeout failure"
fi

# ---------------------------------------------------------------------------
echo "----------------------------------------"
echo "requires-kit + atomic-upsert: PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
