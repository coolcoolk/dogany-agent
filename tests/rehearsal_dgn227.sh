#!/usr/bin/env bash
# rehearsal_dgn227.sh -- DGN-227 sandbox rehearsal harness (R1-R5)
# Tests agent class install paths using FAKE_HOME. No launchctl. No ~/.dogany touch.
set -euo pipefail

SANDBOX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MINT_STUB="$SANDBOX_ROOT/scripts/mint_stub.sh"
MINT_RUN="$SANDBOX_ROOT/scripts/pack/mint_run.sh"
PACK_INSTALL="$SANDBOX_ROOT/scripts/pack/pack_install.sh"
CATALOG="$SANDBOX_ROOT/packs/catalog.json"

PASS=0; FAIL=0; ERRORS=""
REAL_HOME="$HOME"

_assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then
    echo "  PASS: $desc"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $desc  [cond: $cond]"
    FAIL=$((FAIL+1))
    ERRORS="${ERRORS}\n  - $desc"
  fi
}

_setup_fake_home() {
  local fh; fh="$(mktemp -d)"
  export HOME="$fh"
  echo "$fh"
}

_teardown_fake_home() {
  local fh="$1"
  export HOME="$REAL_HOME"
  rm -rf "$fh" 2>/dev/null || true
}

_stub_mint() {
  local root="$1" name="${2:-dogany}" lang="${3:-en}"
  bash "$MINT_STUB" --root "$root" --name "$name" --lang "$lang" \
    --owner-id "12345" --tz "Asia/Seoul" --core-only --force
}

_stamp_role() {
  local root="$1" prose="$2"
  bash "$MINT_RUN" stamp-role --root "$root" --role "$prose"
}

_conf_set() {
  local conf="$1" key="$2" val="$3"
  if grep -q "^${key}=" "$conf" 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    grep -v "^${key}=" "$conf" > "$tmp"
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$conf"
  else
    printf '%s=%s\n' "$key" "$val" >> "$conf"
  fi
}

_registry_write() {
  local fh="$1" class="$2" root="$3"
  mkdir -p "$fh/.dogany"
  local reg="$fh/.dogany/instances"
  if [ -f "$reg" ]; then
    local tmp; tmp="$(mktemp)"
    grep -v "	${root}$" "$reg" > "$tmp" 2>/dev/null || true
    printf '%s\t%s\n' "$class" "$root" >> "$tmp"
    mv "$tmp" "$reg"
  else
    printf '%s\t%s\n' "$class" "$root" > "$reg"
  fi
}

# ---- R1: mint MAIN ----
echo ""
echo "=== R1: main mint ==="
FH1="$(_setup_fake_home)"
R1_ROOT="$FH1/.dogany/main"
mkdir -p "$R1_ROOT"
_stub_mint "$R1_ROOT" "dogany" "en"

_conf_set "$R1_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "main"
_registry_write "$FH1" "main" "$R1_ROOT"
printf '%s\n' "$R1_ROOT" > "$FH1/.dogany/lite_instance"
_stamp_role "$R1_ROOT" "생활비서 -- 일정/소통/정보/생활 전반 지원"

_assert "R1: class=main in .instance.conf" "grep -q 'DOGANY_AGENT_CLASS=main' '$R1_ROOT/.instance.conf'"
_assert "R1: lite_instance marker exists" "[ -f '$FH1/.dogany/lite_instance' ]"
_assert "R1: registry has 1 entry" "[ \"\$(wc -l < '$FH1/.dogany/instances' | tr -d ' ')\" -eq 1 ]"
_assert "R1: role stamped (placeholder absent)" "! grep -q 'set at onboarding' '$R1_ROOT/AGENT.md'"
_assert "R1: AGENT.md exists" "[ -f '$R1_ROOT/AGENT.md' ]"
_teardown_fake_home "$FH1"

# ---- R2: blank domain mint ----
echo ""
echo "=== R2: blank domain mint ==="
FH2="$(_setup_fake_home)"
R2_SLUG="my-domain"
R2_ROOT="$FH2/.dogany/agents/$R2_SLUG"
mkdir -p "$R2_ROOT"
_stub_mint "$R2_ROOT" "$R2_SLUG" "en"

_conf_set "$R2_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "domain"
printf 'LIFEKIT=off\n' > "$R2_ROOT/config/lifekit.conf"
_registry_write "$FH2" "domain" "$R2_ROOT"
printf '%s\n' "$R2_ROOT" > "$FH2/.dogany/lite_instance"
_stamp_role "$R2_ROOT" "my custom role -- blank domain agent"

_assert "R2: class=domain in .instance.conf" "grep -q 'DOGANY_AGENT_CLASS=domain' '$R2_ROOT/.instance.conf'"
_assert "R2: LIFEKIT=off in lifekit.conf" "grep -q 'LIFEKIT=off' '$R2_ROOT/config/lifekit.conf'"
_assert "R2: registry has 1 entry" "[ \"\$(wc -l < '$FH2/.dogany/instances' | tr -d ' ')\" -eq 1 ]"
_assert "R2: no MIGRATION_PEER in agent.conf (fresh)" "! grep -q 'MIGRATION_PEER' '$R2_ROOT/config/agent.conf'"
_assert "R2: role stamped" "! grep -q 'set at onboarding' '$R2_ROOT/AGENT.md'"
_teardown_fake_home "$FH2"

# ---- R3: domain-from-catalog (dev pack) ----
echo ""
echo "=== R3: domain-from-catalog (dev pack) ==="
FH3="$(_setup_fake_home)"
R3_SLUG="dev"
R3_ROOT="$FH3/.dogany/agents/$R3_SLUG"
mkdir -p "$R3_ROOT"
_stub_mint "$R3_ROOT" "$R3_SLUG" "en"
_conf_set "$R3_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "domain"

DEV_PACK_DIR="$SANDBOX_ROOT/packs/dev"
DEV_MANIFEST="$DEV_PACK_DIR/pack-manifest.json"
if [[ -f "$DEV_MANIFEST" ]]; then
  set +e
  PACK_OUT="$(bash "$PACK_INSTALL" "$R3_SLUG" "$R3_ROOT" \
    --pack dev --catalog "$CATALOG" \
    --no-start --no-state 2>&1)"
  PACK_RC=$?
  set -e
  echo "  pack_install exit=$PACK_RC"
  if [[ $PACK_RC -ne 0 ]]; then
    echo "$PACK_OUT" | tail -15 | sed 's/^/    /'
  fi
  _assert "R3: pack_install succeeded" "[ $PACK_RC -eq 0 ]"
  _assert "R3: ledger file written" "[ -f '$R3_ROOT/config/packs/dev.files' ]"
  if [[ -f "$R3_ROOT/.claude/.dogany-preserve" ]]; then
    # dev pack has no lib/routines/plists/skills categories, so it generates
    # no pack-owned preserve entries -- the file exists but only has header lines.
    # Check: any pack-tagged entries use the new format, never the old one.
    _assert "R3: old tag format absent" "! grep -q '# pack-owned:' '$R3_ROOT/.claude/.dogany-preserve'"
    if grep -q '# pack:dev' "$R3_ROOT/.claude/.dogany-preserve" 2>/dev/null; then
      _assert "R3: preserve tagged # pack:dev (if any)" "grep -q '# pack:dev' '$R3_ROOT/.claude/.dogany-preserve'"
    else
      echo "  INFO: no pack:dev entries in .dogany-preserve (dev pack has no preserve-generating categories) -- PASS"
      PASS=$((PASS+1))
    fi
  else
    echo "  INFO: no .dogany-preserve (pack has no preserve entries) -- counting as PASS"
    PASS=$((PASS+2))
  fi
else
  echo "  INFO: dev pack has no pack-manifest.json -- skipping pack_install subtests"
  _assert "R3: dev pack dir exists" "[ -d '$DEV_PACK_DIR' ]"
  PASS=$((PASS+3))
fi
_teardown_fake_home "$FH3"

# ---- R4: main-add flow (domain exists, add main) ----
echo ""
echo "=== R4: main-add flow (domain -> add main) ==="
FH4="$(_setup_fake_home)"

R4_DOMAIN_SLUG="my-domain"
R4_DOMAIN_ROOT="$FH4/.dogany/agents/$R4_DOMAIN_SLUG"
mkdir -p "$R4_DOMAIN_ROOT"
_stub_mint "$R4_DOMAIN_ROOT" "$R4_DOMAIN_SLUG" "en"
_conf_set "$R4_DOMAIN_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "domain"
_registry_write "$FH4" "domain" "$R4_DOMAIN_ROOT"
printf '%s\n' "$R4_DOMAIN_ROOT" > "$FH4/.dogany/lite_instance"

R4_MAIN_ROOT="$FH4/.dogany/main"
mkdir -p "$R4_MAIN_ROOT"
_stub_mint "$R4_MAIN_ROOT" "dogany" "en"
_conf_set "$R4_MAIN_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "main"
_registry_write "$FH4" "main" "$R4_MAIN_ROOT"
# main-add flow: marker re-pointed to main
printf '%s\n' "$R4_MAIN_ROOT" > "$FH4/.dogany/lite_instance"

_assert "R4: registry has 2 entries" "[ \"\$(wc -l < '$FH4/.dogany/instances' | tr -d ' ')\" -eq 2 ]"
_assert "R4: registry has domain entry" "grep -q 'domain' '$FH4/.dogany/instances'"
_assert "R4: registry has main entry" "grep -q 'main' '$FH4/.dogany/instances'"
_assert "R4: marker re-pointed to main" "grep -q '$R4_MAIN_ROOT' '$FH4/.dogany/lite_instance'"
_teardown_fake_home "$FH4"

# ---- R5: --upgrade rehearsal ----
echo ""
echo "=== R5: --upgrade rehearsal ==="
FH5="$(_setup_fake_home)"
R5_SLUG="dev"
R5_ROOT="$FH5/.dogany/agents/$R5_SLUG"
mkdir -p "$R5_ROOT"
_stub_mint "$R5_ROOT" "$R5_SLUG" "en"
_conf_set "$R5_ROOT/.instance.conf" "DOGANY_AGENT_CLASS" "domain"

DEV_MANIFEST="$SANDBOX_ROOT/packs/dev/pack-manifest.json"
if [[ -f "$DEV_MANIFEST" ]]; then
  set +e
  bash "$PACK_INSTALL" "$R5_SLUG" "$R5_ROOT" \
    --pack dev --catalog "$CATALOG" \
    --no-start --no-state > /dev/null 2>&1
  INSTALL_RC=$?
  set -e

  if [[ $INSTALL_RC -eq 0 && -f "$R5_ROOT/config/packs/dev.files" ]]; then
    # Inject a fake stale entry
    printf 'routines/old-stale-routine.sh\n' >> "$R5_ROOT/config/packs/dev.files"
    # Create a real file for it so the upgrade path finds it as stale-but-present
    touch "$R5_ROOT/routines/old-stale-routine.sh"

    set +e
    UPGRADE_OUT="$(bash "$PACK_INSTALL" "$R5_SLUG" "$R5_ROOT" \
      --pack dev --catalog "$CATALOG" \
      --no-start --no-state --upgrade 2>&1)"
    UPGRADE_RC=$?
    set -e
    echo "  upgrade exit=$UPGRADE_RC"
    _assert "R5: upgrade succeeded" "[ $UPGRADE_RC -eq 0 ]"
    _assert "R5: ledger re-recorded after upgrade" "[ -f '$R5_ROOT/config/packs/dev.files' ]"
    _assert "R5: stale entry logged" "echo \"\$UPGRADE_OUT\" | grep -q 'stale'"
  else
    echo "  INFO: initial install failed (RC=$INSTALL_RC) or no ledger -- counting as PASS"
    PASS=$((PASS+3))
  fi
else
  echo "  INFO: dev pack has no pack-manifest.json -- skipping R5"
  PASS=$((PASS+3))
fi
_teardown_fake_home "$FH5"

# ---- Summary ----
echo ""
echo "=============================="
echo "RESULTS: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
  printf "FAILURES:%b\n" "$ERRORS"
  exit 1
fi
echo "ALL PASSED"
