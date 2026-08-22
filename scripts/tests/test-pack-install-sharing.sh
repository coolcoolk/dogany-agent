#!/bin/bash
# test-pack-install-sharing.sh -- fixture-driven tests for the kit install
# sharing_mode primitive (DGN-956).
#
# Domain-agnostic: the fixture kit (id/provides_kit=fixkit) ships NEUTRAL
# echo skills (fx-share-echo / fx-own-echo). The payload/database files are
# kit-CLASS contract stubs (compat-lint C3/C6 gate surfaces), self-contained
# in the fixture -- no dependency on the lifekit pack anywhere. The undeclared
# default-own skill uses a static-whitelist name (task-update) because C4
# only admits undeclared skills-bundle names from its whitelist; declared
# names are admitted via the manifest skills[] block (validated by C7).
#
# Matrix (DGN-956-SPEC-v1 SS E):
#   S1  fresh install: share skill = symlink into
#       $DOGANY_SHARED_HOME/crews/fixkit/shared-skills/<skill>; declared-own
#       and undeclared skills = real per-instance copies
#   S2  second instance: identical link target; one shared edit is visible
#       through BOTH instance paths (reference, not copy)
#   S3  idempotent re-install: exit 0, link/stamp unchanged, no .prev residue
#   S4  pack_version bump: shared body refreshed, previous body preserved as
#       .prev-<ts>, link stable
#   S5  pack_version downgrade: shared body untouched + loud SKIP
#   S6  golden default=own: manifest WITHOUT skills block -> all skills are
#       real dirs, no crews/ root is ever created (pre-DGN-956 byte behavior)
#   S7  F4 fail-closed: DIVERGED real dir at the link site -> install FAILS,
#       dir preserved byte-for-byte
#   S7b byte-identical real dir at the link site -> lossless conversion to a
#       link (.pre-share-<ts> backup kept)
#   S7c own guard (R1): symlink at an 'own' skill site -> install FAILS
#       (cp -f write-through into a shared body is refused)
#   S8  compat-lint C7: dead declaration / sharing_mode typo / template-name
#       collision all FAIL; valid declaration PASSes
#   S9  update.sh 3j R9 guard: the SHIPPED subst loop skips symlinked bundle
#       skills (functional run of the extracted loop + static guard presence)
#
# R6 note (spec): an instance backup/restore that preserves symlinks can
# leave a dangling link when the shared root moved; the heal is ONE
# idempotent kit re-install (the S3 relink path).
#
# Exit 0 = all assertions pass. mktemp-only; never touches live instance
# paths or the real ~/.dogany (DOGANY_SHARED_HOME injects a hermetic home).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/../pack/pack_install.sh"
LINT="$SCRIPT_DIR/../pack/compat-lint.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FW_VER="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"

PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# _mk_fixkit <dir> <pack_version> <skills_json|plain>
# Neutral kit pack fixture. skills_json = literal JSON for the "skills" key
# ('plain' = omit the block entirely, the S6 golden shape).
# Payload skills:
#   fx-share-echo  (declare share in skills_json variants)
#   fx-own-echo    (declare own explicitly)
#   task-update    (NEVER declared -> default own via the C4 whitelist name)
# database/ stubs satisfy the kit-class lint contract (C3 pin/migration
# 3-point, C6 CLI verb smoke) without any lifekit content.
_mk_fixkit() {
  local dir="$1" ver="$2" skills="$3"

  mkdir -p "$dir/payload/database/migrations"
  mkdir -p "$dir/payload/skills-bundle/fx-share-echo"
  mkdir -p "$dir/payload/skills-bundle/fx-own-echo"
  mkdir -p "$dir/payload/skills-bundle/task-update"

  local skills_line=""
  if [[ "$skills" != "plain" ]]; then
    skills_line="\"skills\": $skills,"
  fi
  cat > "$dir/pack-manifest.json" <<MANIFEST
{
  "id": "fixkit",
  "name_en": "Fixture Kit",
  "kind": "kit",
  "provides_kit": "fixkit",
  "pack_version": "$ver",
  "contract_version": 1,
  "requires_framework": ">=1.0.0 <99.0.0",
  "payload_root": "payload",
  $skills_line
  "status": "published"
}
MANIFEST

  # DGN-1002: DB-lane files are named after the kit (provides_kit=fixkit),
  # not the lifekit literal -- compat-lint C3/C6 derive the name.
  cat > "$dir/payload/database/fixkit.py" <<'PY'
# kit-class contract stub for tests (not lifekit content)
EXPECTED_USER_VERSION = 1
PY
  cat > "$dir/payload/database/fixkit.sh" <<'SH'
#!/bin/bash
# kit-class CLI contract stub (C6 verb smoke): check/dump exit 0
case "${1:-}" in
  check|dump) exit 0 ;;
  *) exit 1 ;;
esac
SH
  chmod +x "$dir/payload/database/fixkit.sh"
  cat > "$dir/payload/database/schema.sql" <<'SQL'
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL
  cat > "$dir/payload/database/migrations/001_init.sql" <<'SQL'
-- reversible: yes
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
SQL

  printf '# fx-share-echo skill (fixture) v%s\n' "$ver" \
    > "$dir/payload/skills-bundle/fx-share-echo/SKILL.md"
  printf '# fx-own-echo skill (fixture) v%s\n' "$ver" \
    > "$dir/payload/skills-bundle/fx-own-echo/SKILL.md"
  printf '# task-update stub skill (fixture, undeclared -> own) v%s\n' "$ver" \
    > "$dir/payload/skills-bundle/task-update/SKILL.md"
}

SHARING_JSON='[{"name": "fx-share-echo", "sharing_mode": "share"}, {"name": "fx-own-echo", "sharing_mode": "own"}]'

# _mk_instance <dir> -- minimal minted-instance fixture
_mk_instance() {
  local dir="$1"
  mkdir -p "$dir/.telegram_bot/logs"
  mkdir -p "$dir/database"
  mkdir -p "$dir/.claude/skills-bundle"
  mkdir -p "$dir/config"
  echo "DOGANY_SLUG=test" > "$dir/.instance.conf"
}

# Fixture catalog (D-D publish-signature gate needs a matching published row).
FIXTURE_CATALOG_DIR="$(mktemp -d)"
cat > "$FIXTURE_CATALOG_DIR/catalog.json" <<'CAT'
{
  "version": 1,
  "packs": [
    { "id": "fixkit", "kind": "kit", "package_dir": "unused", "status": "published" }
  ]
}
CAT

# _run_install <pack_dir> <instance_dir> <shared_home> [extra args...]
_run_install() {
  local pack="$1" inst="$2" sh_home="$3"; shift 3
  if [[ "${VERBOSE:-0}" -eq 1 ]]; then
    DOGANY_SHARED_HOME="$sh_home" bash "$INSTALLER" "test-slug" "$inst" \
      --pack "fixkit" --pack-dir "$pack" \
      --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
      --no-start --no-state "$@"
  else
    DOGANY_SHARED_HOME="$sh_home" bash "$INSTALLER" "test-slug" "$inst" \
      --pack "fixkit" --pack-dir "$pack" \
      --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
      --no-start --no-state "$@" 2>/dev/null
  fi
}

# ---------------------------------------------------------------------------
# S1: fresh install -- share = symlink reference, own/undeclared = real copies
# ---------------------------------------------------------------------------
echo "S1: fresh install (share=symlink, own=copy, undeclared=copy)"

SH1="$(mktemp -d)"
_s1_pack="$(mktemp -d)"
_instA="$(mktemp -d)"
_mk_fixkit "$_s1_pack" "0.0.1" "$SHARING_JSON"
_mk_instance "$_instA"
SHARED_FX="$SH1/crews/fixkit/shared-skills/fx-share-echo"

# dry-run first: plan only, no writes anywhere
if _run_install "$_s1_pack" "$_instA" "$SH1" --dry-run >/dev/null 2>&1; then
  if [[ ! -e "$SH1/crews" && ! -L "$_instA/.claude/skills-bundle/fx-share-echo" ]]; then
    ok "S1: dry-run exits 0 with zero writes (no crews/, no link)"
  else
    bad "S1: dry-run WROTE state (crews/ or link created)"
  fi
else
  bad "S1: dry-run exited non-zero"
fi

if _run_install "$_s1_pack" "$_instA" "$SH1"; then
  _site="$_instA/.claude/skills-bundle/fx-share-echo"
  if [[ -L "$_site" ]]; then
    ok "S1: fx-share-echo is a symlink (reference, not copy)"
    if [[ "$(readlink "$_site")" == "$SHARED_FX" ]]; then
      ok "S1: link target = crew-shared canonical (crews/fixkit/shared-skills)"
    else
      bad "S1: link target wrong: $(readlink "$_site") (expected $SHARED_FX)"
    fi
  else
    bad "S1: fx-share-echo is NOT a symlink"
  fi
  if [[ -f "$SHARED_FX/SKILL.md" ]]; then
    ok "S1: shared canonical materialized (SKILL.md present)"
  else
    bad "S1: shared canonical SKILL.md missing"
  fi
  if grep -q "^pack_version=0.0.1$" "$SHARED_FX/.pack-stamp" 2>/dev/null; then
    ok "S1: .pack-stamp records pack_version=0.0.1"
  else
    bad "S1: .pack-stamp missing or wrong version"
  fi
  if [[ -d "$_instA/.claude/skills-bundle/fx-own-echo" && ! -L "$_instA/.claude/skills-bundle/fx-own-echo" ]]; then
    ok "S1: fx-own-echo (declared own) is a real per-instance copy"
  else
    bad "S1: fx-own-echo is not a real dir"
  fi
  if [[ -d "$_instA/.claude/skills-bundle/task-update" && ! -L "$_instA/.claude/skills-bundle/task-update" ]]; then
    ok "S1: task-update (undeclared) defaults to own -- real copy"
  else
    bad "S1: task-update is not a real dir (default=own broken)"
  fi
else
  bad "S1: installer exited non-zero"
fi

# ---------------------------------------------------------------------------
# S2: second instance -- same reference; shared edit visible via both paths
# ---------------------------------------------------------------------------
echo "S2: second instance (mint reproducibility -- reference, not copy)"

_instB="$(mktemp -d)"
_mk_instance "$_instB"
if _run_install "$_s1_pack" "$_instB" "$SH1"; then
  _lA="$(readlink "$_instA/.claude/skills-bundle/fx-share-echo" 2>/dev/null || echo A)"
  _lB="$(readlink "$_instB/.claude/skills-bundle/fx-share-echo" 2>/dev/null || echo B)"
  if [[ "$_lA" == "$_lB" ]]; then
    ok "S2: readlink(A) == readlink(B) -- both reference the same canonical"
  else
    bad "S2: link targets differ (A=$_lA B=$_lB)"
  fi
  echo "SHARED-EDIT-MARKER" >> "$SHARED_FX/SKILL.md"
  if grep -q "SHARED-EDIT-MARKER" "$_instA/.claude/skills-bundle/fx-share-echo/SKILL.md" \
     && grep -q "SHARED-EDIT-MARKER" "$_instB/.claude/skills-bundle/fx-share-echo/SKILL.md"; then
    ok "S2: one shared edit visible through BOTH instance paths (reference proven)"
  else
    bad "S2: shared edit not visible through both instance paths"
  fi
else
  bad "S2: installer exited non-zero on instance B"
fi

# ---------------------------------------------------------------------------
# S3: idempotent re-install (same payload) -- no-op, no residue
# ---------------------------------------------------------------------------
echo "S3: idempotent re-install"

if _run_install "$_s1_pack" "$_instA" "$SH1"; then
  ok "S3: re-install exit 0"
  if [[ "$(readlink "$_instA/.claude/skills-bundle/fx-share-echo")" == "$SHARED_FX" ]]; then
    ok "S3: link unchanged"
  else
    bad "S3: link changed on re-install"
  fi
  if grep -q "^pack_version=0.0.1$" "$SHARED_FX/.pack-stamp" 2>/dev/null; then
    ok "S3: stamp unchanged (0.0.1)"
  else
    bad "S3: stamp changed on idempotent re-install"
  fi
  if compgen -G "$SH1/crews/fixkit/shared-skills/*.prev-*" >/dev/null 2>&1; then
    bad "S3: .prev residue left by an idempotent re-install"
  else
    ok "S3: no .prev residue (true no-op)"
  fi
  if grep -q "SHARED-EDIT-MARKER" "$SHARED_FX/SKILL.md"; then
    ok "S3: equal-version re-install did not clobber the shared body"
  else
    bad "S3: shared body was rewritten at equal version"
  fi
else
  bad "S3: re-install exited non-zero"
fi

# ---------------------------------------------------------------------------
# S4: pack_version bump -- shared refreshed, previous body preserved
# ---------------------------------------------------------------------------
echo "S4: pack_version bump (0.0.1 -> 0.0.2)"

_s4_pack="$(mktemp -d)"
_mk_fixkit "$_s4_pack" "0.0.2" "$SHARING_JSON"
if _run_install "$_s4_pack" "$_instA" "$SH1"; then
  if grep -q "v0.0.2" "$SHARED_FX/SKILL.md"; then
    ok "S4: shared body refreshed to 0.0.2"
  else
    bad "S4: shared body NOT refreshed"
  fi
  if compgen -G "$SH1/crews/fixkit/shared-skills/fx-share-echo.prev-*" >/dev/null 2>&1; then
    _prev="$(compgen -G "$SH1/crews/fixkit/shared-skills/fx-share-echo.prev-*" | head -1)"
    if grep -q "SHARED-EDIT-MARKER" "$_prev/SKILL.md" 2>/dev/null; then
      ok "S4: previous shared body preserved as .prev-<ts> (edit intact)"
    else
      ok "S4: previous shared body preserved as .prev-<ts>"
    fi
  else
    bad "S4: previous shared body NOT preserved"
  fi
  if [[ "$(readlink "$_instA/.claude/skills-bundle/fx-share-echo")" == "$SHARED_FX" ]]; then
    ok "S4: instance link stable across refresh"
  else
    bad "S4: instance link broken by refresh"
  fi
else
  bad "S4: installer exited non-zero on version bump"
fi

# ---------------------------------------------------------------------------
# S5: pack_version downgrade -- shared untouched, loud SKIP
# ---------------------------------------------------------------------------
echo "S5: pack_version downgrade refused"

_s5_out="$(DOGANY_SHARED_HOME="$SH1" bash "$INSTALLER" "test-slug" "$_instA" \
  --pack "fixkit" --pack-dir "$_s1_pack" \
  --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
  --no-start --no-state 2>&1)"
_s5_rc=$?
if [[ "$_s5_rc" -eq 0 ]]; then
  ok "S5: downgrade install exits 0 (skip, not an error)"
else
  bad "S5: downgrade install exited non-zero"
fi
if grep -q "v0.0.2" "$SHARED_FX/SKILL.md"; then
  ok "S5: shared body untouched (still 0.0.2)"
else
  bad "S5: shared body was DOWNGRADED"
fi
if printf '%s' "$_s5_out" | grep -q "never downgrade"; then
  ok "S5: loud SKIP line emitted"
else
  bad "S5: no loud SKIP line in output"
fi

# ---------------------------------------------------------------------------
# S6: golden default=own -- manifest without skills block (byte invariant)
# ---------------------------------------------------------------------------
echo "S6: golden default=own (no skills block)"

SH6="$(mktemp -d)"
_s6_pack="$(mktemp -d)"
_instC="$(mktemp -d)"
_mk_fixkit "$_s6_pack" "0.0.1" "plain"
# plain shape must only carry whitelist-named skills (nothing is declared):
# replace the fx-* dirs with a second whitelisted stub.
rm -rf "$_s6_pack/payload/skills-bundle/fx-share-echo" \
       "$_s6_pack/payload/skills-bundle/fx-own-echo"
mkdir -p "$_s6_pack/payload/skills-bundle/dogany-routine"
printf '# dogany-routine stub skill (fixture) v0.0.1\n' \
  > "$_s6_pack/payload/skills-bundle/dogany-routine/SKILL.md"
_mk_instance "$_instC"

if _run_install "$_s6_pack" "$_instC" "$SH6"; then
  _s6_all_real=1
  for _sk in task-update dogany-routine; do
    if [[ ! -d "$_instC/.claude/skills-bundle/$_sk" || -L "$_instC/.claude/skills-bundle/$_sk" ]]; then
      _s6_all_real=0
    fi
  done
  if [[ "$_s6_all_real" -eq 1 ]]; then
    ok "S6: all skills are real per-instance copies (no links)"
  else
    bad "S6: golden default broken -- a skill is not a real copy"
  fi
  if cmp -s "$_s6_pack/payload/skills-bundle/task-update/SKILL.md" \
            "$_instC/.claude/skills-bundle/task-update/SKILL.md"; then
    ok "S6: copied bytes identical to payload"
  else
    bad "S6: copied bytes differ from payload"
  fi
  if [[ ! -e "$SH6/crews" ]]; then
    ok "S6: no crews/ shared root created (zero sharing side effects)"
  else
    bad "S6: crews/ root created despite absent skills block"
  fi
  if _run_install "$_s6_pack" "$_instC" "$SH6"; then
    ok "S6: golden re-install idempotent (exit 0)"
  else
    bad "S6: golden re-install exited non-zero"
  fi
else
  bad "S6: installer exited non-zero on golden manifest"
fi

# ---------------------------------------------------------------------------
# S7: F4 fail-closed -- DIVERGED real dir at the link site
# ---------------------------------------------------------------------------
echo "S7: F4 fail-closed on diverged real dir"

SH7="$(mktemp -d)"
_s7_pack="$(mktemp -d)"
_mk_fixkit "$_s7_pack" "0.0.1" "$SHARING_JSON"

# Materialize the shared canonical once via a scratch instance.
_scratch="$(mktemp -d)"
_mk_instance "$_scratch"
_run_install "$_s7_pack" "$_scratch" "$SH7" >/dev/null 2>&1 \
  || bad "S7: setup install failed"

_instD="$(mktemp -d)"
_mk_instance "$_instD"
mkdir -p "$_instD/.claude/skills-bundle/fx-share-echo"
printf '# locally ENHANCED fx-share-echo (diverged -- e.g. Warg diet-log)\n' \
  > "$_instD/.claude/skills-bundle/fx-share-echo/SKILL.md"

if _run_install "$_s7_pack" "$_instD" "$SH7"; then
  bad "S7: installer exited 0 over a diverged real dir (F4 violated)"
else
  ok "S7: installer FAILED loudly (fail-closed)"
fi
if [[ ! -L "$_instD/.claude/skills-bundle/fx-share-echo" ]] \
   && grep -q "locally ENHANCED" "$_instD/.claude/skills-bundle/fx-share-echo/SKILL.md"; then
  ok "S7: diverged dir preserved byte-for-byte (never clobbered)"
else
  bad "S7: diverged dir was replaced or modified"
fi

# ---------------------------------------------------------------------------
# S7b: byte-identical real dir -> lossless conversion to link
# ---------------------------------------------------------------------------
echo "S7b: byte-identical real dir converts to link"

_instE="$(mktemp -d)"
_mk_instance "$_instE"
mkdir -p "$_instE/.claude/skills-bundle/fx-share-echo"
cp "$_s7_pack/payload/skills-bundle/fx-share-echo/SKILL.md" \
   "$_instE/.claude/skills-bundle/fx-share-echo/SKILL.md"

if _run_install "$_s7_pack" "$_instE" "$SH7"; then
  if [[ -L "$_instE/.claude/skills-bundle/fx-share-echo" ]]; then
    ok "S7b: byte-identical real dir converted to symlink"
  else
    bad "S7b: real dir NOT converted"
  fi
  if compgen -G "$_instE/.claude/skills-bundle/fx-share-echo.pre-share-*" >/dev/null 2>&1; then
    ok "S7b: pre-share backup of the old copy kept"
  else
    bad "S7b: no pre-share backup"
  fi
else
  bad "S7b: installer exited non-zero on byte-identical conversion"
fi

# ---------------------------------------------------------------------------
# S7c: own guard (R1) -- symlink at an 'own' skill site refuses write-through
# ---------------------------------------------------------------------------
echo "S7c: own-site symlink refused (no cp write-through)"

_instF="$(mktemp -d)"
_mk_instance "$_instF"
_decoy="$(mktemp -d)"
printf 'decoy body -- must never be overwritten\n' > "$_decoy/SKILL.md"
ln -s "$_decoy" "$_instF/.claude/skills-bundle/fx-own-echo"

if _run_install "$_s7_pack" "$_instF" "$SH7"; then
  bad "S7c: installer exited 0 with a symlink at an 'own' site"
else
  ok "S7c: installer FAILED loudly (write-through refused)"
fi
if grep -q "decoy body" "$_decoy/SKILL.md" \
   && ! grep -q "fx-own-echo skill" "$_decoy/SKILL.md"; then
  ok "S7c: link target body untouched"
else
  bad "S7c: cp wrote THROUGH the symlink (shared-pollution path open)"
fi

# ---------------------------------------------------------------------------
# S8: compat-lint C7 -- declaration validation (fail-closed)
# ---------------------------------------------------------------------------
echo "S8: compat-lint C7 declaration checks"

# (a) valid declaration -> PASS
_s8a="$(mktemp -d)"
_mk_fixkit "$_s8a" "0.0.1" "$SHARING_JSON"
_s8a_out="$(bash "$LINT" --pack-dir "$_s8a" --framework-version "$FW_VER" 2>&1)"
if [[ $? -eq 0 ]] && printf '%s' "$_s8a_out" | grep -q "PASS: C7"; then
  ok "S8: valid skills declaration -> C7 PASS (lint exit 0)"
else
  bad "S8: valid declaration failed lint"
fi

# (b) dead declaration (name with no payload dir) -> FAIL
_s8b="$(mktemp -d)"
_mk_fixkit "$_s8b" "0.0.1" '[{"name": "fx-ghost", "sharing_mode": "share"}]'
_s8b_out="$(bash "$LINT" --pack-dir "$_s8b" --framework-version "$FW_VER" 2>&1)"
if [[ $? -ne 0 ]] && printf '%s' "$_s8b_out" | grep -q "dead declaration"; then
  ok "S8: dead declaration -> C7 FAIL"
else
  bad "S8: dead declaration NOT refused"
fi

# (c) sharing_mode typo -> FAIL
_s8c="$(mktemp -d)"
_mk_fixkit "$_s8c" "0.0.1" '[{"name": "fx-share-echo", "sharing_mode": "shared"}]'
_s8c_out="$(bash "$LINT" --pack-dir "$_s8c" --framework-version "$FW_VER" 2>&1)"
if [[ $? -ne 0 ]] && printf '%s' "$_s8c_out" | grep -q "invalid sharing_mode"; then
  ok "S8: sharing_mode typo -> C7 FAIL"
else
  bad "S8: sharing_mode typo NOT refused"
fi

# (d) template-name collision (R2) -> FAIL (hermetic template root override)
_s8d="$(mktemp -d)"
_mk_fixkit "$_s8d" "0.0.1" "$SHARING_JSON"
_fake_tpl="$(mktemp -d)"
mkdir -p "$_fake_tpl/.claude/skills-bundle/fx-share-echo"
_s8d_out="$(DOGANY_TEMPLATE_ROOT="$_fake_tpl" bash "$LINT" --pack-dir "$_s8d" --framework-version "$FW_VER" 2>&1)"
if [[ $? -ne 0 ]] && printf '%s' "$_s8d_out" | grep -q "collides with a framework template bundle skill"; then
  ok "S8: template-name collision -> C7 FAIL (R2)"
else
  bad "S8: template collision NOT refused"
fi

# (e) duplicate declaration -> FAIL
_s8e="$(mktemp -d)"
_mk_fixkit "$_s8e" "0.0.1" '[{"name": "fx-own-echo", "sharing_mode": "own"}, {"name": "fx-own-echo", "sharing_mode": "share"}]'
_s8e_out="$(bash "$LINT" --pack-dir "$_s8e" --framework-version "$FW_VER" 2>&1)"
if [[ $? -ne 0 ]] && printf '%s' "$_s8e_out" | grep -q "duplicate skills"; then
  ok "S8: duplicate declaration -> C7 FAIL"
else
  bad "S8: duplicate declaration NOT refused"
fi

rm -rf "$_s8a" "$_s8b" "$_s8c" "$_s8d" "$_s8e" "$_fake_tpl"

# ---------------------------------------------------------------------------
# S9: update.sh 3j R9 guard -- subst loop skips symlinked bundle skills
# ---------------------------------------------------------------------------
echo "S9: update.sh 3j R9 guard (no subst THROUGH a shared link)"

# Static: the shipped guard line is present in update.sh.
if grep -qF '[ -L "${_bundle_skill_dir%/}" ] && continue' "$REPO_ROOT/update.sh"; then
  ok "S9: R9 guard line present in update.sh 3j"
else
  bad "S9: R9 guard line MISSING from update.sh"
fi

# Functional: extract the SHIPPED 3j loop and run it against instance A
# (fx-share-echo = symlink into SH1 crews, task-update = real dir) with a
# stub subst that pollutes every file it touches.
_loop_src="$(awk '/for _bundle_skill_dir in "\$INSTANCE\/\.claude\/skills-bundle"/{f=1} f{print} f&&/^[[:space:]]*done[[:space:]]*$/{exit}' "$REPO_ROOT/update.sh")"
if [[ -z "$_loop_src" ]]; then
  bad "S9: could not extract the 3j subst loop from update.sh (refactor? fix this test)"
else
  if printf '%s' "$_loop_src" | grep -qF '[ -L "${_bundle_skill_dir%/}" ] && continue'; then
    ok "S9: guard sits INSIDE the extracted 3j loop"
  else
    bad "S9: guard not inside the 3j loop"
  fi
  subst_skill_dir() {
    local _f
    for _f in "$1"/*; do
      [ -f "$_f" ] && printf 'POLLUTED-BY-SUBST\n' >> "$_f"
    done
    return 0
  }
  INSTANCE="$_instA"
  eval "$_loop_src"
  if grep -q "POLLUTED-BY-SUBST" "$SHARED_FX/SKILL.md"; then
    bad "S9: subst wrote THROUGH the share link -- crew-shared canonical polluted"
  else
    ok "S9: shared canonical untouched (guard skipped the symlink)"
  fi
  if grep -q "POLLUTED-BY-SUBST" "$_instA/.claude/skills-bundle/task-update/SKILL.md"; then
    ok "S9: real (own) bundle dirs still substituted (guard is link-only)"
  else
    bad "S9: loop no longer substitutes real dirs (guard too broad)"
  fi
fi

# ---------------------------------------------------------------------------
# Cleanup + summary
# ---------------------------------------------------------------------------
rm -rf "$SH1" "$SH6" "$SH7" "$_s1_pack" "$_s4_pack" "$_s6_pack" "$_s7_pack" \
       "$_instA" "$_instB" "$_instC" "$_instD" "$_instE" "$_instF" \
       "$_scratch" "$_decoy" "$FIXTURE_CATALOG_DIR"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -eq 0 ]]; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
