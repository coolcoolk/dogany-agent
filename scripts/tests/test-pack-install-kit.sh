#!/bin/bash
# test-pack-install-kit.sh -- fixture-driven tests for kit-class install
# extension in scripts/pack/pack_install.sh (DGN-803 LS-4).
#
# Tests:
#   T1  Fresh kit install: payload categories land in correct instance paths;
#       lifekit.db initialized from payload schema (DGN-803 LS-5 db-init);
#       lifekit.sh check exit 0 (CLI contract surface).
#   T2  Reverse-drift guard: instance pin > payload pin -> kit_core SKIPPED.
#   T3  Reverse-drift equal pin: install proceeds (idempotent, not a skip).
#   T4  Config merge-key: existing keys preserved, new keys appended.
#   T5  Idempotency: install twice -> stable result, no duplication.
#   T6  Reverse-drift parse failure -> kit_core SKIPPED (fail-safe, LS-5
#       hardening; was fail-open PROCEED in LS-4).
#   T7  Catalog-row kit install (--pack lifekit, no --pack-dir): package_dir
#       resolves relative to the catalog file; pack_version falls back to the
#       pack manifest when the catalog row omits it (DGN-803 LS-5 e2).
#   T9  db-init atomicity (DGN-783 B2): a schema.sql that fails mid-load must
#       leave NO lifekit.db (no partial DB frozen by the exists->keep branch)
#       and the install must exit non-zero.
#   T10 kit-migrate (DGN-783 B1): instance DB behind the payload pin with the
#       migration file present -> forward apply + backup + user_version==pin.
#   T11 kit-migrate gap FATAL (DGN-783 B1): code pin ahead of the DB with NO
#       migration files available -> install exits non-zero (never exit 0
#       with a fail-closed runtime).
#
# Fixtures declare status=published and installs pass a fixture catalog with
# a matching published row: the D-D publish-signature gate (DGN-783 B3) runs
# install-side before C6 and refuses unsigned packs.
#
# Exit 0 = all assertions pass; nonzero = at least one failure.
# Uses mktemp -d for all instance and pack directories. Never touches live
# instance paths (Ag, Warg, etc).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/../pack/pack_install.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# _mk_pack <dir> <pack_version> <euv>
# Creates a minimal kit pack fixture at <dir> with:
#   pack-manifest.json (kind=kit, provides_kit=lifekit, payload_root=payload)
#   payload/database/lifekit.py  (EXPECTED_USER_VERSION=<euv>)
#   payload/database/lifekit.sh  (check/dump stubs, exit 0)
#   payload/database/schema.sql  (PRAGMA user_version=<euv>)
#   payload/database/migrations/001_init.sql (if euv>=1, -- reversible: yes)
#   payload/service/lifekit/__init__.py
#   payload/skills-bundle/diet-log/SKILL.md
#   payload/routines/bundle/morning-brief.sh
#   payload/config/lifekit.conf  (KEY_A=val_a, KEY_NEW=val_new)
_mk_pack() {
  local dir="$1" ver="$2" euv="$3"

  mkdir -p "$dir/payload/database/migrations"
  mkdir -p "$dir/payload/service/lifekit"
  mkdir -p "$dir/payload/skills-bundle/diet-log"
  mkdir -p "$dir/payload/routines/bundle"
  mkdir -p "$dir/payload/config"
  mkdir -p "$dir/payload/config/i18n"

  # pack-manifest.json
  cat > "$dir/pack-manifest.json" <<MANIFEST
{
  "id": "lifekit",
  "name_en": "Lifekit",
  "kind": "kit",
  "provides_kit": "lifekit",
  "pack_version": "$ver",
  "contract_version": 1,
  "requires_framework": ">=1.0.0 <99.0.0",
  "payload_root": "payload",
  "deploy_owner": "skull",
  "status": "published"
}
MANIFEST

  # payload/database/lifekit.py with EXPECTED_USER_VERSION
  cat > "$dir/payload/database/lifekit.py" <<PY
# lifekit.py stub for tests
EXPECTED_USER_VERSION = $euv
PY

  # payload/database/lifekit.sh -- CLI contract surface (C6: check + dump exit 0)
  cat > "$dir/payload/database/lifekit.sh" <<'SH'
#!/bin/bash
# lifekit.sh stub: CLI contract surface for tests (exit 0 on check/dump)
case "${1:-}" in
  check|dump) exit 0 ;;
  *) exit 1 ;;
esac
SH
  chmod +x "$dir/payload/database/lifekit.sh"

  # payload/database/schema.sql with matching PRAGMA user_version
  cat > "$dir/payload/database/schema.sql" <<SQL
PRAGMA user_version = $euv;
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL

  # payload/database/migrations/ -- migration file matching EUV (C3: max(migrations)==EUV)
  local mig_num
  printf -v mig_num '%03d' "$euv"
  cat > "$dir/payload/database/migrations/${mig_num}_init.sql" <<SQL
-- reversible: yes
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL

  # payload/service/lifekit/__init__.py
  echo "# service facade stub" > "$dir/payload/service/lifekit/__init__.py"

  # payload/skills-bundle/diet-log/SKILL.md
  echo "# diet-log skill" > "$dir/payload/skills-bundle/diet-log/SKILL.md"

  # payload/routines/bundle/morning-brief.sh
  cat > "$dir/payload/routines/bundle/morning-brief.sh" <<'SH'
#!/bin/bash
# morning-brief stub
SH
  chmod +x "$dir/payload/routines/bundle/morning-brief.sh"

  # payload/config/lifekit.conf (two keys: one existing-compat, one new)
  cat > "$dir/payload/config/lifekit.conf" <<CONF
KEY_A=val_a
KEY_NEW=val_new
CONF

  # payload/config/i18n/*.bundle.json -- i18n JSON bundles (FIX-2, DGN-803)
  printf '%s\n' '{"bundle.diet_log": "Log diet", "bundle.workout_log": "Log workout"}' \
    > "$dir/payload/config/i18n/en.bundle.json"
  printf '%s\n' '{"bundle.diet_log": "식단 기록", "bundle.workout_log": "운동 기록"}' \
    > "$dir/payload/config/i18n/ko.bundle.json"
}

# _mk_instance <dir>
# Creates a minimal minted-instance fixture at <dir>.
# Provides: .instance.conf, .telegram_bot/logs/, database/, service/,
#           .claude/skills-bundle/, routines/, config/
_mk_instance() {
  local dir="$1"
  mkdir -p "$dir/.telegram_bot/logs"
  mkdir -p "$dir/database"
  mkdir -p "$dir/service"
  mkdir -p "$dir/.claude/skills-bundle"
  mkdir -p "$dir/routines"
  mkdir -p "$dir/config"
  # minimal .instance.conf
  echo "DOGANY_SLUG=test" > "$dir/.instance.conf"
}

# Shared fixture catalog: the D-D publish-signature gate (DGN-783 B3)
# requires the catalog row status to match the manifest status (published).
FIXTURE_CATALOG_DIR="$(mktemp -d)"
cat > "$FIXTURE_CATALOG_DIR/catalog.json" <<'CAT'
{
  "version": 1,
  "packs": [
    { "id": "lifekit", "kind": "kit", "package_dir": "unused", "status": "published" }
  ]
}
CAT

# _run_install <pack_dir> <instance_dir> -- runs pack_install.sh kit path
# Returns installer exit code; output captured to /dev/null unless VERBOSE=1.
_run_install() {
  local pack="$1" inst="$2"
  if [[ "${VERBOSE:-0}" -eq 1 ]]; then
    bash "$INSTALLER" "test-slug" "$inst" \
      --pack "lifekit" \
      --pack-dir "$pack" \
      --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
      --no-start --no-state
  else
    bash "$INSTALLER" "test-slug" "$inst" \
      --pack "lifekit" \
      --pack-dir "$pack" \
      --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
      --no-start --no-state 2>/dev/null
  fi
}

# ---------------------------------------------------------------------------
# T1: Fresh kit install -- category files land in correct instance paths
# ---------------------------------------------------------------------------
echo "T1: fresh kit install"

_t1_pack="$(mktemp -d)"
_t1_inst="$(mktemp -d)"
_mk_pack "$_t1_pack" "1.0.0" "1"
_mk_instance "$_t1_inst"

if _run_install "$_t1_pack" "$_t1_inst"; then
  # kit_core: database/lifekit.py
  if [[ -f "$_t1_inst/database/lifekit.py" ]]; then
    ok "T1: database/lifekit.py installed"
  else
    bad "T1: database/lifekit.py MISSING"
  fi
  # kit_core: database/migrations/001_init.sql
  if [[ -f "$_t1_inst/database/migrations/001_init.sql" ]]; then
    ok "T1: database/migrations/001_init.sql installed"
  else
    bad "T1: database/migrations/001_init.sql MISSING"
  fi
  # service_facade
  if [[ -f "$_t1_inst/service/lifekit/__init__.py" ]]; then
    ok "T1: service/lifekit/__init__.py installed"
  else
    bad "T1: service/lifekit/__init__.py MISSING"
  fi
  # skills-bundle
  if [[ -f "$_t1_inst/.claude/skills-bundle/diet-log/SKILL.md" ]]; then
    ok "T1: .claude/skills-bundle/diet-log/SKILL.md installed"
  else
    bad "T1: .claude/skills-bundle/diet-log/SKILL.md MISSING"
  fi
  # routines/bundle
  if [[ -f "$_t1_inst/routines/bundle/morning-brief.sh" ]]; then
    ok "T1: routines/bundle/morning-brief.sh installed"
  else
    bad "T1: routines/bundle/morning-brief.sh MISSING"
  fi
  # config/lifekit.conf
  if [[ -f "$_t1_inst/config/lifekit.conf" ]]; then
    ok "T1: config/lifekit.conf installed"
  else
    bad "T1: config/lifekit.conf MISSING"
  fi
  # DOGANY_PACKS upsert
  if grep -q "DOGANY_PACKS=lifekit@1.0.0" "$_t1_inst/.instance.conf" 2>/dev/null; then
    ok "T1: DOGANY_PACKS=lifekit@1.0.0 recorded"
  else
    bad "T1: DOGANY_PACKS not recorded correctly (expected lifekit@1.0.0)"
    grep "DOGANY_PACKS" "$_t1_inst/.instance.conf" 2>/dev/null || true
  fi
  # db-init (DGN-803 LS-5): lifekit.db initialized from payload schema.sql
  # at kit activation (the step that moved out of mint.sh).
  if [[ -f "$_t1_inst/database/lifekit.db" ]]; then
    ok "T1: lifekit.db initialized at kit activation"
    _uv="$(sqlite3 "$_t1_inst/database/lifekit.db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
    if [[ "$_uv" == "1" ]]; then
      ok "T1: lifekit.db user_version == schema PRAGMA stamp (1)"
    else
      bad "T1: lifekit.db user_version=$_uv (expected 1 from schema.sql)"
    fi
  else
    bad "T1: lifekit.db MISSING after kit install (db-init did not run)"
  fi
  # CLI contract surface (smoke test 2): lifekit.sh check exit 0
  if bash "$_t1_inst/database/lifekit.sh" check >/dev/null 2>&1; then
    ok "T1: lifekit.sh check exit 0"
  else
    bad "T1: lifekit.sh check exited non-zero"
  fi
else
  bad "T1: installer exited non-zero"
fi

rm -rf "$_t1_pack" "$_t1_inst"

# ---------------------------------------------------------------------------
# T2: Reverse-drift guard -- instance pin > payload pin -> kit_core SKIPPED
# ---------------------------------------------------------------------------
echo "T2: reverse-drift guard (instance pin > payload pin)"

_t2_pack="$(mktemp -d)"
_t2_inst="$(mktemp -d)"
# Payload pin = 5, instance pin = 19 (simulates live Ag/Warg scenario)
_mk_pack "$_t2_pack" "1.0.0" "5"
_mk_instance "$_t2_inst"

# Pre-install a lifekit.py with pin=19 in the instance
cat > "$_t2_inst/database/lifekit.py" <<PY
# lifekit.py with higher pin (simulates live instance ahead of payload)
EXPECTED_USER_VERSION = 19
# sentinel: original instance content
PY

if _run_install "$_t2_pack" "$_t2_inst"; then
  # The instance lifekit.py must NOT be overwritten (pin 19 preserved)
  _pin_after="$(grep -E '^EXPECTED_USER_VERSION' "$_t2_inst/database/lifekit.py" \
    | head -1 | grep -Eo '[0-9]+' || true)"
  if [[ "$_pin_after" == "19" ]]; then
    ok "T2: kit_core SKIPPED -- instance lifekit.py pin=19 preserved (not overwritten to 5)"
  else
    bad "T2: kit_core NOT skipped -- instance lifekit.py pin changed to $_pin_after (expected 19)"
  fi
  # sentinel comment must still be present
  if grep -q "sentinel" "$_t2_inst/database/lifekit.py" 2>/dev/null; then
    ok "T2: instance lifekit.py content unchanged (sentinel present)"
  else
    bad "T2: instance lifekit.py content was overwritten (sentinel missing)"
  fi
  # Other categories (service_facade, skills) should still be installed
  if [[ -f "$_t2_inst/service/lifekit/__init__.py" ]]; then
    ok "T2: service_facade still installed despite kit_core skip"
  else
    bad "T2: service_facade missing (should install even when kit_core skipped)"
  fi
else
  bad "T2: installer exited non-zero"
fi

rm -rf "$_t2_pack" "$_t2_inst"

# ---------------------------------------------------------------------------
# T3: Reverse-drift equal pin -- install proceeds (not a skip)
# ---------------------------------------------------------------------------
echo "T3: reverse-drift equal pin (should proceed, not skip)"

_t3_pack="$(mktemp -d)"
_t3_inst="$(mktemp -d)"
_mk_pack "$_t3_pack" "1.0.0" "11"
_mk_instance "$_t3_inst"

# Pre-install a lifekit.py with the same pin as payload (pin=11)
cat > "$_t3_inst/database/lifekit.py" <<PY
# old content at same pin
EXPECTED_USER_VERSION = 11
# old-sentinel
PY

if _run_install "$_t3_pack" "$_t3_inst"; then
  # The payload lifekit.py should replace the old one (equal pin -> proceed)
  if grep -q "old-sentinel" "$_t3_inst/database/lifekit.py" 2>/dev/null; then
    # Old sentinel still present -- may mean install was skipped incorrectly
    # BUT: since the stub lifekit.py in pack also has pin=11 and no old-sentinel
    # the absence of old-sentinel means the copy happened.
    bad "T3: old-sentinel still present -- kit_core was unexpectedly skipped at equal pin"
  else
    ok "T3: equal pin -- kit_core proceeded (old content replaced)"
  fi
else
  bad "T3: installer exited non-zero"
fi

rm -rf "$_t3_pack" "$_t3_inst"

# ---------------------------------------------------------------------------
# T4: Config merge-key -- existing keys preserved, new keys appended
# ---------------------------------------------------------------------------
echo "T4: config merge-key"

_t4_pack="$(mktemp -d)"
_t4_inst="$(mktemp -d)"
_mk_pack "$_t4_pack" "1.0.0" "1"
_mk_instance "$_t4_inst"

# Pre-existing config with KEY_A at a different value (must be preserved)
cat > "$_t4_inst/config/lifekit.conf" <<CONF
KEY_A=existing_value
KEY_EXISTING_ONLY=untouched
CONF

if _run_install "$_t4_pack" "$_t4_inst"; then
  # KEY_A existing value must NOT be overwritten
  if grep -q "^KEY_A=existing_value" "$_t4_inst/config/lifekit.conf"; then
    ok "T4: existing key KEY_A preserved (not clobbered)"
  else
    bad "T4: KEY_A was overwritten (expected existing_value)"
    grep "KEY_A" "$_t4_inst/config/lifekit.conf" || true
  fi
  # KEY_EXISTING_ONLY must stay
  if grep -q "^KEY_EXISTING_ONLY=untouched" "$_t4_inst/config/lifekit.conf"; then
    ok "T4: KEY_EXISTING_ONLY preserved"
  else
    bad "T4: KEY_EXISTING_ONLY was removed"
  fi
  # KEY_NEW from payload must be added
  if grep -q "^KEY_NEW=val_new" "$_t4_inst/config/lifekit.conf"; then
    ok "T4: new key KEY_NEW appended from payload"
  else
    bad "T4: KEY_NEW not appended (expected val_new)"
    cat "$_t4_inst/config/lifekit.conf" || true
  fi
else
  bad "T4: installer exited non-zero"
fi

rm -rf "$_t4_pack" "$_t4_inst"

# ---------------------------------------------------------------------------
# T5: Idempotency -- install twice produces stable result, no duplication
# ---------------------------------------------------------------------------
echo "T5: idempotency (install twice)"

_t5_pack="$(mktemp -d)"
_t5_inst="$(mktemp -d)"
_mk_pack "$_t5_pack" "1.0.0" "1"
_mk_instance "$_t5_inst"

# First install
if ! _run_install "$_t5_pack" "$_t5_inst"; then
  bad "T5: first install exited non-zero"
fi
# Second install (idempotent)
if ! _run_install "$_t5_pack" "$_t5_inst"; then
  bad "T5: second install exited non-zero"
else
  ok "T5: second install succeeded (no fatal error)"
fi

# DOGANY_PACKS must have exactly one lifekit entry (no duplication)
_packs_line="$(grep "^DOGANY_PACKS=" "$_t5_inst/.instance.conf" || true)"
_lifekit_count="$(printf '%s' "$_packs_line" | grep -o "lifekit@" | wc -l | tr -d ' ')"
if [[ "$_lifekit_count" -eq 1 ]]; then
  ok "T5: DOGANY_PACKS has exactly one lifekit entry (no duplication)"
else
  bad "T5: DOGANY_PACKS has $_lifekit_count lifekit entries (expected 1): $_packs_line"
fi

# config/lifekit.conf KEY_A must appear exactly once
_key_count="$(grep -c "^KEY_A=" "$_t5_inst/config/lifekit.conf" 2>/dev/null || echo 0)"
if [[ "$_key_count" -eq 1 ]]; then
  ok "T5: config/lifekit.conf KEY_A appears exactly once (no duplication)"
else
  bad "T5: config/lifekit.conf KEY_A appears $_key_count times (expected 1)"
fi

rm -rf "$_t5_pack" "$_t5_inst"

# ---------------------------------------------------------------------------
# T6: Reverse-drift parse failure -> kit_core SKIPPED (fail-safe, LS-5)
# ---------------------------------------------------------------------------
echo "T6: reverse-drift parse failure (fail-safe SKIP)"

_t6_pack="$(mktemp -d)"
_t6_inst="$(mktemp -d)"
_mk_pack "$_t6_pack" "1.0.0" "5"
_mk_instance "$_t6_inst"

# Instance lifekit.py exists but its pin line is UNPARSEABLE: the guard can
# no longer prove payload >= instance, so kit_core must be SKIPPED (loud),
# never PROCEED (the LS-4 fail-open behavior this hardening replaces).
cat > "$_t6_inst/database/lifekit.py" <<PY
# lifekit.py with a mangled pin line (simulates local edit / corruption)
EXPECTED_USER_VERSION = "not-a-number"
# t6-sentinel: original instance content
PY

if _run_install "$_t6_pack" "$_t6_inst"; then
  if grep -q "t6-sentinel" "$_t6_inst/database/lifekit.py" 2>/dev/null; then
    ok "T6: kit_core SKIPPED on parse failure (instance file preserved)"
  else
    bad "T6: kit_core PROCEEDED on parse failure (fail-open regression -- instance file overwritten)"
  fi
  # Non-core categories still install (same contract as T2).
  if [[ -f "$_t6_inst/service/lifekit/__init__.py" ]]; then
    ok "T6: service_facade still installed despite kit_core skip"
  else
    bad "T6: service_facade missing (should install even when kit_core skipped)"
  fi
else
  bad "T6: installer exited non-zero"
fi

rm -rf "$_t6_pack" "$_t6_inst"

# ---------------------------------------------------------------------------
# T7: Catalog-row kit install (--pack lifekit via catalog, no --pack-dir)
# ---------------------------------------------------------------------------
echo "T7: catalog-row kit install (package_dir + manifest pack_version fallback)"

_t7_root="$(mktemp -d)"
_t7_pack="$_t7_root/pack-repo"
_t7_inst="$_t7_root/inst"
mkdir -p "$_t7_pack" "$_t7_inst"
_mk_pack "$_t7_pack" "1.2.3" "1"
_mk_instance "$_t7_inst"

# Fixture catalog: kit row WITHOUT pack_version (the LS-5 e2 shape -- the
# external pack repo's manifest is authoritative), package_dir relative to
# the catalog file location (sibling-checkout convention).
mkdir -p "$_t7_root/packs"
cat > "$_t7_root/packs/catalog.json" <<CAT
{
  "version": 1,
  "packs": [
    { "id": "lifekit", "kind": "kit", "package_dir": "../pack-repo", "status": "published" }
  ]
}
CAT

if bash "$INSTALLER" "test-slug" "$_t7_inst" \
     --pack "lifekit" \
     --catalog "$_t7_root/packs/catalog.json" \
     --no-start --no-state 2>/dev/null; then
  if [[ -f "$_t7_inst/database/lifekit.py" ]]; then
    ok "T7: kit payload installed via catalog package_dir resolution"
  else
    bad "T7: database/lifekit.py MISSING (catalog resolution failed)"
  fi
  # pack_version must come from the MANIFEST (1.2.3), not 'unversioned'.
  if grep -q "DOGANY_PACKS=lifekit@1.2.3" "$_t7_inst/.instance.conf" 2>/dev/null; then
    ok "T7: DOGANY_PACKS=lifekit@1.2.3 (manifest pack_version fallback)"
  else
    bad "T7: DOGANY_PACKS wrong (expected lifekit@1.2.3): $(grep '^DOGANY_PACKS=' "$_t7_inst/.instance.conf" 2>/dev/null || echo absent)"
  fi
else
  bad "T7: installer exited non-zero on catalog-row kit install"
fi

rm -rf "$_t7_root"

# ---------------------------------------------------------------------------
# T8: i18n JSON merge (FIX-2, DGN-803)
# payload/config/i18n/<lang>.bundle.json -> instance config/i18n/<lang>.json
# add-only: existing keys preserved, new bundle.* keys added, idempotent.
# ---------------------------------------------------------------------------
echo "T8: i18n JSON merge (payload/config/i18n/*.bundle.json -> config/i18n/<lang>.json)"

_t8_pack="$(mktemp -d)"
_t8_inst="$(mktemp -d)"
_mk_pack "$_t8_pack" "1.0.0" "1"
_mk_instance "$_t8_inst"

# Pre-seed instance en.json with an existing key that must NOT be clobbered.
mkdir -p "$_t8_inst/config/i18n"
printf '%s\n' '{"existing.key": "do not overwrite", "bundle.diet_log": "old value -- must NOT change"}' \
  > "$_t8_inst/config/i18n/en.json"

if _run_install "$_t8_pack" "$_t8_inst"; then
  # en.json: existing.key must be preserved unchanged
  _existing="$(python3 -c "import json; d=json.load(open('$_t8_inst/config/i18n/en.json')); print(d.get('existing.key','MISSING'))" 2>/dev/null)"
  if [[ "$_existing" == "do not overwrite" ]]; then
    ok "T8: existing.key preserved in en.json (not clobbered)"
  else
    bad "T8: existing.key was overwritten or missing (got: $_existing)"
  fi
  # en.json: bundle.diet_log pre-seeded old value must be preserved (already present)
  _diet_en="$(python3 -c "import json; d=json.load(open('$_t8_inst/config/i18n/en.json')); print(d.get('bundle.diet_log','MISSING'))" 2>/dev/null)"
  if [[ "$_diet_en" == "old value -- must NOT change" ]]; then
    ok "T8: bundle.diet_log preserved (pre-existing key not clobbered)"
  else
    bad "T8: bundle.diet_log was overwritten (expected old value, got: $_diet_en)"
  fi
  # en.json: bundle.workout_log not pre-seeded -- must be added from bundle
  _workout_en="$(python3 -c "import json; d=json.load(open('$_t8_inst/config/i18n/en.json')); print(d.get('bundle.workout_log','MISSING'))" 2>/dev/null)"
  if [[ "$_workout_en" == "Log workout" ]]; then
    ok "T8: bundle.workout_log added to en.json from bundle"
  else
    bad "T8: bundle.workout_log not added or wrong (expected 'Log workout', got: $_workout_en)"
  fi
  # ko.json: must be created from scratch (instance had no ko.json)
  _diet_ko="$(python3 -c "import json; d=json.load(open('$_t8_inst/config/i18n/ko.json')); print(d.get('bundle.diet_log','MISSING'))" 2>/dev/null)"
  if [[ "$_diet_ko" == "식단 기록" ]]; then
    ok "T8: ko.json created with bundle.diet_log from bundle"
  else
    bad "T8: ko.json bundle.diet_log missing or wrong (expected '식단 기록', got: $_diet_ko)"
  fi
  # Idempotency: install again, en.json must be stable (no duplication)
  if _run_install "$_t8_pack" "$_t8_inst"; then
    _workout_en2="$(python3 -c "import json; d=json.load(open('$_t8_inst/config/i18n/en.json')); print(d.get('bundle.workout_log','MISSING'))" 2>/dev/null)"
    if [[ "$_workout_en2" == "Log workout" ]]; then
      ok "T8: idempotent -- second install leaves en.json stable"
    else
      bad "T8: idempotent second install changed bundle.workout_log (got: $_workout_en2)"
    fi
  else
    bad "T8: second (idempotent) install exited non-zero"
  fi
else
  bad "T8: installer exited non-zero"
fi

rm -rf "$_t8_pack" "$_t8_inst"

# ---------------------------------------------------------------------------
# T9: db-init atomicity (DGN-783 B2)
# schema.sql fails mid-load -> NO lifekit.db left (the old direct
# `sqlite3 db < schema` left a partial DB the exists->keep branch then froze).
# ---------------------------------------------------------------------------
echo "T9: db-init atomicity (partial schema load leaves no DB)"

_t9_pack="$(mktemp -d)"
_t9_inst="$(mktemp -d)"
_mk_pack "$_t9_pack" "1.0.0" "1"
_mk_instance "$_t9_inst"

# Corrupt the schema AFTER the fixture build: pragma + one valid statement,
# then invalid SQL (fails mid-load, after the DB file is already created).
cat > "$_t9_pack/payload/database/schema.sql" <<'SQL'
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
THIS IS NOT VALID SQL;
SQL

if _run_install "$_t9_pack" "$_t9_inst"; then
  bad "T9: installer exited 0 on a failing schema load (expected non-zero)"
else
  ok "T9: installer exited non-zero on failing schema load"
fi
if [[ -f "$_t9_inst/database/lifekit.db" ]]; then
  bad "T9: partial lifekit.db left behind (atomicity broken)"
else
  ok "T9: no lifekit.db left behind (partial DB discarded)"
fi
if compgen -G "$_t9_inst/database/lifekit.db.init-tmp.*" >/dev/null 2>&1; then
  bad "T9: init tmp file left behind"
else
  ok "T9: no init tmp residue"
fi

rm -rf "$_t9_pack" "$_t9_inst"

# ---------------------------------------------------------------------------
# T10: kit-migrate forward apply (DGN-783 B1)
# Instance DB at user_version=1, payload pin=2 with migration 002 shipped ->
# install applies 002, backs up first, and lands user_version==2.
# ---------------------------------------------------------------------------
echo "T10: kit-migrate forward apply (DB v1 -> pin v2)"

_t10_pack="$(mktemp -d)"
_t10_inst="$(mktemp -d)"
_mk_pack "$_t10_pack" "1.1.0" "2"
_mk_instance "$_t10_inst"

# Fixture 002 migration must bump the pragma (canonical migration convention).
cat > "$_t10_pack/payload/database/migrations/002_init.sql" <<'SQL'
-- reversible: yes
CREATE TABLE IF NOT EXISTS t10_new (id INTEGER PRIMARY KEY);
PRAGMA user_version = 2;
SQL

# Pre-existing instance DB at v1 (simulates an installed kit one pin behind).
sqlite3 "$_t10_inst/database/lifekit.db" \
  'PRAGMA user_version = 1; CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);'

if _run_install "$_t10_pack" "$_t10_inst"; then
  _t10_uv="$(sqlite3 "$_t10_inst/database/lifekit.db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
  if [[ "$_t10_uv" == "2" ]]; then
    ok "T10: migration applied -- user_version 1 -> 2 (== pin)"
  else
    bad "T10: user_version=$_t10_uv after install (expected 2)"
  fi
  if compgen -G "$_t10_inst/database/lifekit.db.v1.bak-*" >/dev/null 2>&1; then
    ok "T10: pre-apply backup written (lifekit.db.v1.bak-*)"
  else
    bad "T10: no pre-apply backup found"
  fi
  if sqlite3 "$_t10_inst/database/lifekit.db" \
       "SELECT name FROM sqlite_master WHERE name='t10_new';" 2>/dev/null | grep -q t10_new; then
    ok "T10: migration DDL landed (t10_new table exists)"
  else
    bad "T10: migration DDL missing (t10_new table absent)"
  fi
else
  bad "T10: installer exited non-zero (expected forward apply to succeed)"
fi

rm -rf "$_t10_pack" "$_t10_inst"

# ---------------------------------------------------------------------------
# T11: kit-migrate gap FATAL (DGN-783 B1 core defect shape)
# Instance code pin ahead (kit_core skipped by reverse-drift guard), DB
# behind, NO migration files available -> the install must FAIL (exit
# non-zero). Previously this exited 0 while the runtime fail-closed on the
# user_version assert.
# ---------------------------------------------------------------------------
echo "T11: kit-migrate gap FATAL (pin ahead, no migration files)"

_t11_pack="$(mktemp -d)"
_t11_inst="$(mktemp -d)"
_mk_pack "$_t11_pack" "1.0.0" "5"
_mk_instance "$_t11_inst"

# Instance code pin 19 (ahead of payload 5 -> kit_core SKIPPED), DB at v1,
# and no instance migrations exist to close the 1 -> 19 gap.
cat > "$_t11_inst/database/lifekit.py" <<PY
# lifekit.py ahead of payload
EXPECTED_USER_VERSION = 19
PY
sqlite3 "$_t11_inst/database/lifekit.db" \
  'PRAGMA user_version = 1; CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);'

if _run_install "$_t11_pack" "$_t11_inst"; then
  bad "T11: installer exited 0 with DB v1 vs code pin 19 and no migrations (B1 regression)"
else
  ok "T11: installer FAILED loudly on unclosable pin gap (no silent exit 0)"
fi
_t11_uv="$(sqlite3 "$_t11_inst/database/lifekit.db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
if [[ "$_t11_uv" == "1" ]]; then
  ok "T11: DB untouched (user_version still 1; forward-only, no partial moves)"
else
  bad "T11: DB user_version changed to $_t11_uv (expected untouched 1)"
fi

rm -rf "$_t11_pack" "$_t11_inst"

rm -rf "$FIXTURE_CATALOG_DIR"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -eq 0 ]]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
