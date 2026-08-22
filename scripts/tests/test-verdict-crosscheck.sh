#!/bin/bash
# test-verdict-crosscheck.sh -- DGN-681-S4b-RATIFIED §3-A/§3-B lockstep
# cross-check between compat-lint.sh C-KITDEP (form validation) and
# pack_install.sh's install-time requires_kit inline gate (form validation
# half of it).
#
# WHY THIS TEST EXISTS (background, not a re-derivation of the design):
# DGN-681 S4b RATIFIED deliberately DUPLICATES requires_kit form validation
# in two places -- compat-lint C-KITDEP (fail-open when compat-lint.sh is
# absent) and pack_install's inline gate (fail-closed, always runs,
# INDEPENDENT of compat-lint per DGN-1002 lockstep precedent). The ratified
# doc's own self-grill names the accepted cost explicitly ("살아남은 리스크
# 3 -- 검증 로직 이중화 드리프트") and both landing agents recommended one
# lockstep cross-test. This file is that test.
#
# WHAT THIS TEST DOES: for every FORM axis row of the §3-A / §3-B verdict
# tables (absent / valid / non-object / missing-key / extra-key / kit-grammar
# / reserved-name / self-dep / range-grammar / kind=kit-declares), it ACTUALLY
# RUNS both compat-lint.sh and pack_install.sh against manifests carrying the
# SAME requires_kit value (the pack_install-side manifest is DERIVED from the
# compat-lint fixture file by copying id/kind/provides_kit/requires_kit
# verbatim -- never hand-retyped, so both invocations are driven by literally
# the same JSON value) and asserts the verdicts map 1:1 per the ratified
# mapping: compat-lint FAIL <-> pack_install BLOCK, PASS <-> PASS, SKIP <-> SKIP.
#
# WHAT THIS TEST DOES NOT DO (explicit non-goal, ratified §3-B "충족 검증"):
# the SATISFACTION axis (.instance.conf presence / DOGANY_PACKS line /
# <kit>@ entry presence / installed-version-vs-range) is a pack_install-ONLY
# concept -- compat-lint has no instance state (interface stays
# --pack-dir/--framework-version, ratified §3-A). There is nothing on the
# compat-lint side to cross-check satisfaction against, so those rows are
# NOT represented here. This is a scope boundary, not a coverage gap.
# Satisfaction rows are already covered by test-pack-install-requires-kit.sh
# (G10-G16). Form rows are already covered by test-compat-lint.sh (T30) and
# test-pack-install-requires-kit.sh (G2-G9) INDEPENDENTLY -- this file adds
# ONLY the cross-comparison layer; it does not re-derive either suite's
# per-implementation assertions.
#
# This file does NOT modify compat-lint.sh or pack_install.sh. Read-only
# against both. A verdict drift found here is reported, never silently
# patched -- fixing drift is a Metal design decision, not a test-authoring one.
#
# Exit 0 = every case's mapped verdict matches; nonzero = at least one drift.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LINT="$SCRIPT_DIR/../pack/compat-lint.sh"
INSTALLER="$SCRIPT_DIR/../pack/pack_install.sh"
FX_DIR="$SCRIPT_DIR/fixtures/compat-lint"
FW_VERSION="$(tr -d '[:space:]' < "$REPO_DIR/VERSION")"

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

FIXTURE_CATALOG_DIR="$(mktemp -d)"
cat > "$FIXTURE_CATALOG_DIR/catalog.json" <<'CAT'
{ "version": 1, "packs": [] }
CAT

# ---------------------------------------------------------------------------
# compat-lint side: run it for real, extract the C-KITDEP verdict token.
# ---------------------------------------------------------------------------
_compat_ckitdep_verdict() {
  local fxdir="$1" out line
  out="$(bash "$LINT" --pack-dir "$fxdir" --framework-version "$FW_VERSION" 2>&1)"
  line="$(grep -E '\[compat-lint\] (PASS|FAIL|SKIP): C-KITDEP' <<<"$out" | head -1)"
  case "$line" in
    *"FAIL: C-KITDEP"*) echo "FAIL" ;;
    *"PASS: C-KITDEP"*) echo "PASS" ;;
    *"SKIP: C-KITDEP"*) echo "SKIP" ;;
    *) echo "MISSING" ;;
  esac
}

# ---------------------------------------------------------------------------
# pack_install side: derive an install-shaped manifest from the SAME
# compat-lint fixture manifest -- id/kind/provides_kit/requires_kit copied
# verbatim (never retyped) so the two sides run on the identical requires_kit
# input. Extra install-only fields (categories/reference_slug/reference_root)
# are added because pack_install needs them to get past manifest load; they
# have no bearing on the requires_kit gate itself (the gate runs before any
# category dispatch -- verified against pack_install.sh source order).
# ---------------------------------------------------------------------------
_derive_install_manifest() {
  local src="$1" dst="$2"
  python3 - "$src" "$dst" <<'PYEOF'
import json, sys
src = json.load(open(sys.argv[1]))
out = {
    "id": src.get("id", "test-bpack"),
    "name": "crosscheck fixture (derived, not hand-authored)",
    "kind": src.get("kind", "pack"),
    "contract_version": src.get("contract_version", 1),
    "pack_version": src.get("pack_version", "0.1.0"),
    "requires_framework": src.get("requires_framework", ">=1.0.0 <99.0.0"),
    "reference_slug": "refslug",
    "reference_root": "/tmp/nonexistent-ref-root-crosscheck",
    "categories": [{"category": "routines"}],
}
if "provides_kit" in src:
    out["provides_kit"] = src["provides_kit"]
if "requires_kit" in src:
    out["requires_kit"] = src["requires_kit"]
json.dump(out, open(sys.argv[2], "w"), indent=2)
PYEOF
}

_mf_id() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('id',''))" "$1"
}

# Uniform SATISFYING instance (DOGANY_PACKS=lifekit@1.2.0) for every case.
# Form-damage cases BLOCK before pack_install.sh ever reads .instance.conf
# (form validation precedes the satisfaction read in the source -- verified),
# so this fixture only actually matters for the "valid" row (where the form
# is clean and the gate must fall through to satisfaction and PASS). Giving
# every case the same satisfying instance isolates the FORM axis cleanly:
# any BLOCK we observe is attributable to form damage, never to an
# unrelated/accidental satisfaction failure muddying the cross-check.
_mk_inst() {
  local dir="$1"
  mkdir -p "$dir/.telegram_bot/logs" "$dir/bridge" "$dir/.claude"
  echo '{}' > "$dir/.claude/settings.json"
  {
    echo "DOGANY_AGENT_NAME=testslug"
    echo "DOGANY_TIER=lite"
    echo "DOGANY_PACKS=lifekit@1.2.0"
  } > "$dir/.instance.conf"
}

# Verdict detection: SKIP/PASS have a literal "requires_kit gate: SKIP --" /
# "requires_kit gate: PASS --" log line (pack_install.sh _log calls). BLOCK
# goes through _fail(), which logs "requires_kit gate: <reason>" WITHOUT the
# literal word BLOCK and exits non-zero -- so BLOCK is detected as "the
# requires_kit gate line appeared, but neither literal SKIP/PASS pattern
# matched, and the process exited non-zero" (checked in that order).
_install_gate_verdict() {
  local pack="$1" inst="$2" pid="$3" out rc
  out="$(bash "$INSTALLER" testslug "$inst" --pack "$pid" --pack-dir "$pack" \
         --catalog "$FIXTURE_CATALOG_DIR/catalog.json" \
         --no-start --no-state --dry-run 2>&1)"
  rc=$?
  if grep -qE 'requires_kit gate: SKIP --' <<<"$out"; then
    echo "SKIP"
  elif grep -qE 'requires_kit gate: PASS --' <<<"$out"; then
    echo "PASS"
  elif grep -qF 'requires_kit gate:' <<<"$out" && [[ "$rc" -ne 0 ]]; then
    echo "BLOCK"
  else
    echo "MISSING(rc=$rc)"
  fi
}

# Ratified mapping (DGN-681-S4b-RATIFIED §3-B): compat-lint FAIL <-> BLOCK,
# PASS <-> PASS, SKIP <-> SKIP.
_map_install_to_compat() {
  case "$1" in
    BLOCK) echo "FAIL" ;;
    PASS)  echo "PASS" ;;
    SKIP)  echo "SKIP" ;;
    *)     echo "UNMAPPABLE:$1" ;;
  esac
}

# ---------------------------------------------------------------------------
# Case matrix -- FORM axis only. Every row reuses an EXISTING compat-lint
# fixture (scripts/tests/fixtures/compat-lint/), already exercised by
# test-compat-lint.sh T30 (and, for form-damage semantics, by
# test-pack-install-requires-kit.sh G2-G9) -- no new fixture authored here,
# only the pack_install-side manifest is DERIVED at run time (see above).
# ---------------------------------------------------------------------------
CASE_NAMES=(absent            valid           nonobject       missing_key         extra_key         kit_grammar     reserved_name   self_dep       range_grammar     kind_kit_declares)
CASE_FIXTURES=(good           behavior-pack   rk-nonobject    rk-missing-range    rk-extra-key      rk-badkit       rk-reserved     rk-selfdep     rk-badrange       rk-onkit)

echo "=== C-KITDEP <-> pack_install requires_kit-gate verdict-identical cross-check (DGN-681-S4b-RATIFIED §3) ==="
for i in "${!CASE_NAMES[@]}"; do
  name="${CASE_NAMES[$i]}"
  fx="${CASE_FIXTURES[$i]}"
  fxdir="$FX_DIR/$fx"
  if [[ ! -d "$fxdir" ]]; then
    bad "$name: fixture missing: $fxdir"
    continue
  fi

  compat_v="$(_compat_ckitdep_verdict "$fxdir")"

  pdir="$(mktemp -d)"; idir="$(mktemp -d)"
  _derive_install_manifest "$fxdir/pack-manifest.json" "$pdir/pack-manifest.json"
  pid="$(_mf_id "$fxdir/pack-manifest.json")"
  _mk_inst "$idir"
  install_v="$(_install_gate_verdict "$pdir" "$idir" "$pid")"
  mapped="$(_map_install_to_compat "$install_v")"

  if [[ "$compat_v" == "MISSING" ]] || [[ "$install_v" == MISSING* ]]; then
    bad "$name ($fx): verdict line not found (compat-lint=$compat_v pack_install=$install_v) -- cannot cross-check"
  elif [[ "$mapped" == "$compat_v" ]]; then
    ok "$name ($fx): compat-lint=$compat_v <-> pack_install=$install_v (mapped=$mapped) MATCH"
  else
    bad "$name ($fx): VERDICT DRIFT -- compat-lint=$compat_v but pack_install=$install_v (mapped=$mapped) -- DGN-681-S4b-RATIFIED §3-B lockstep violated"
  fi
done

echo "----------------------------------------"
echo "verdict-crosscheck: PASS=$PASS FAIL=$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
exit 0
