#!/bin/bash
# compat-lint.sh -- pack contract validator (DGN-803 LS-3, v0).
#
# Verifies that a pack's manifest and payload satisfy the dogany-agent
# framework contract BEFORE publish and BEFORE install. Six checks:
#
#   C1  manifest required fields + semver range validity
#   C2  requires_framework range vs framework VERSION
#   C3  version 3-point consistency: EXPECTED_USER_VERSION == max(migrations)
#       == PRAGMA user_version in schema.sql; reversible: markers required
#       (cadence-gate equivalent relocated here per DGN-681 G6/LS-3)
#   C4  payload path allowlist: only declared surfaces allowed; framework
#       core paths (bridge/, memory-engine/, scripts/, routines/ core,
#       service/<non-kit>/) are forbidden
#   C5  secret-sweep + persona-token + personal-data gates (reuse
#       pack_publish.sh constants; no re-implementation)
#   D-D publish-signature gate (install-side only, DGN-783 B3): manifest
#       status must be "published" AND the catalog row for the pack id must
#       carry the same status. Runs BEFORE C6 so unsigned payload code is
#       never executed by the verb smoke.
#   C6  CLI contract verb smoke: payload/database/<kit>.sh check + dump
#       exit code 0
#
# Kit identity + capabilities (DGN-1002, additive -- contract_version stays 1):
#   - KIT_NAME derives from the manifest 'provides_kit' field. Key ABSENT =>
#     legacy default "lifekit" (pre-DGN-1002 behavior byte-identical). Key
#     PRESENT => value must match ^[a-z][a-z0-9_-]{0,31}$ and must not be a
#     framework-reserved name; anything else (empty, traversal, glob chars,
#     non-string) => C1 FAIL + abort (fail closed -- a poisoned name must
#     never drive the C3/C4/C6 path rules).
#   - Optional manifest block "capabilities": {"db_lane": true|false}.
#     Block absent => db_lane=true (C3/C6 run exactly as before).
#     db_lane=false => C3/C6 capability-SKIP, BUT shipping payload/database/
#     files while declaring db_lane=false is a contradiction => FAIL
#     (skip-smuggling refused). Malformed block (non-object / unknown keys /
#     non-boolean db_lane) => C1 FAIL.
#
# Payload not seeded (only .gitkeep present) => C3 and C6 print SKIP and
# exit 0. C1, C2, C4, C5 run fully now.
#
# Manifest parse gate (DGN-1004, additive -- runs before legacy-grace):
#   - manifest missing (file does not exist) => unchanged, falls through to
#     the ordinary C1 "manifest not found" FAIL.
#   - manifest present but does not parse as JSON, or parses to a non-object
#     top-level value (array/string/number/null) => C1 FAIL + abort, exit 1.
#     This is NOT a legacy pack -- legacy-grace requires actually observing
#     "no contract_version" in a readable manifest, and an unparseable file
#     gives no such observation. Previously a parse failure fell through
#     `|| echo ""` fallbacks to an empty contract/kind read, which matched
#     the legacy-grace condition and exited 0 with zero checks run -- a kit
#     could ship corrupt JSON and get a green gate. Fixed: parse failure now
#     fails closed before legacy-grace is even evaluated.
#   - manifest parses fine as an object but has no contract_version (old-shape
#     pack) => legacy-grace unchanged, exit 0.
#
# Pack class axis (DGN-1018, additive -- contract_version stays 1):
#   - C-KIND (named slot, C1..C7 untouched): a manifest that carries
#     contract_version must declare kind exactly "kit" or "pack". Anything
#     else (absent / non-string / "agentpack" / other) => FAIL + ABORT --
#     kind now selects the class-branched allowlists below, and a poisoned
#     class value must never drive them (same logic as the poisoned-KIT_NAME
#     abort). "agentpack" is a repo-naming segment (ESTATE-TAXONOMY §7),
#     never a manifest kind. Legacy packs (no contract_version, kind!=kit)
#     never reach this slot (legacy-grace exits first).
#   - KIT_NAME resolution is scoped to kind=kit (DGN-1018 §3): the legacy
#     "lifekit" default only ever existed to preserve pre-provides_kit KIT
#     manifests, so kind=pack gets NO kit identity (KIT_NAME empty; C3/C6
#     are kit-surface checks and are class-SKIPped for kind=pack).
#     kind=pack declaring provides_kit = C1 FAIL (a kit-providing pack is
#     kind=kit -- S4b symmetry).
#   - C4 allowlist is class-branched (same slot, table parameterized by
#     kind): kind=pack allows service/<service_namespace>/ + knowledge/ +
#     skills-bundle + routines/bundle + config/<id>.conf + i18n*;
#     database/ and mirror/ are kit-exclusive => FAIL for kind=pack
#     (ZERO-MIGRATION INVARIANT mechanized).
#   - service_namespace (optional manifest field, kind=pack only): grammar
#     reuses the provides_kit rules; kind=kit declaring it = FAIL (dual
#     truth-source refused); equal to requires_kit.kit = FAIL (dependency
#     surface impersonation); declared but payload/service/<ns>/ absent =
#     FAIL (dead declaration, C7 precedent).
#   - capabilities.db_lane for kind=pack: absent => false (a behavior pack
#     has no DB lane to preserve); declared true => FAIL (kit-exclusive
#     surface); declared false => PASS.
#
# C-KITDEP (named slot, S4b-1 -- DGN-681-S4b-RATIFIED §3-A; C1..C7
# untouched; placed after legacy-grace, after the C1 declaration block):
#   requires_kit form validation. Absent => SKIP ("no kit dependency").
#   Present => must be a JSON object with keys exactly {kit, range}; kit
#   reuses the provides_kit grammar (+ not a self-dependency vs own id /
#   own provides_kit); range must satisfy _is_semver_range; kind=kit
#   declaring requires_kit = FAIL (kit->kit install semantics undefined --
#   undefined = refused, DGN-1004). Satisfaction (instance state) is NOT
#   checked here -- that is pack_install's install-time inline check; this
#   script keeps its --pack-dir/--framework-version interface unexpanded.
#
# Usage:
#   compat-lint.sh --pack-dir <pack-repo-root> \
#                  --framework-version <semver>
#   # or for install-side (bypasses nothing -- same gate):
#   compat-lint.sh --pack-dir <pack-repo-root> \
#                  --framework-version <semver> \
#                  --install-side [--catalog <catalog.json>]
#
# --catalog: catalog file the D-D gate checks the pack row against
#            (default: <repo>/packs/catalog.json relative to this script).
#
# Exit codes: 0 = PASS (all checks passed or SKIPped), 1 = FAIL (at least one
# check failed). On FAIL the failing check(s) are printed to stderr.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Argument parse
# ---------------------------------------------------------------------------
PACK_DIR=""
FW_VERSION=""
INSTALL_SIDE=0
CATALOG_FILE="$SCRIPT_DIR/../../packs/catalog.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack-dir)          PACK_DIR="$2";      shift 2 ;;
    --framework-version) FW_VERSION="$2";    shift 2 ;;
    --install-side)      INSTALL_SIDE=1;     shift   ;;
    --catalog)           CATALOG_FILE="$2";  shift 2 ;;
    *) echo "[compat-lint] ERROR: unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$PACK_DIR" ]]    || { echo "[compat-lint] ERROR: --pack-dir required" >&2; exit 1; }
[[ -d "$PACK_DIR" ]]    || { echo "[compat-lint] ERROR: not a directory: $PACK_DIR" >&2; exit 1; }
[[ -n "$FW_VERSION" ]]  || { echo "[compat-lint] ERROR: --framework-version required" >&2; exit 1; }

MANIFEST="$PACK_DIR/pack-manifest.json"
PAYLOAD_ROOT_FIELD=""

# ---------------------------------------------------------------------------
# Manifest parse gate (DGN-1004) -- MUST run before legacy-grace.
# ---------------------------------------------------------------------------
# Legacy-grace below decides "no contract_version" == "old-shape pack, let it
# pass". That inference is only valid when the manifest was actually readable
# as a JSON object -- i.e. we genuinely observed the absence of the field.
# A manifest that fails to parse (corrupt JSON, truncated file, or a
# top-level JSON value that isn't an object -- array/string/number/null) is
# a DIFFERENT state: we observed NOTHING, because .get() was never callable.
# Previously every manifest-reading python one-liner in this script used
# `2>/dev/null || echo ""`, so a parse failure silently produced an empty
# string == field absent == indistinguishable from "old-shape pack with no
# contract_version" == legacy-grace exit 0, ALL CHECKS PASS. A kit shipped
# with deliberately-malformed JSON got a green gate with zero checks run --
# and every other manifest read later in this script (C1 fields, kit name,
# capabilities, skills block, D-D status/id) shared the same `|| echo`
# fallback, so all of them inherit this gate's protection once placed here,
# first, before any of them run.
#
# Fail closed: unparseable / missing-as-object / non-object JSON -> FAIL,
# non-zero exit, loud message. Only a manifest that parses AND is a JSON
# object reaches the legacy-grace decision below (unchanged from before).
if [[ -f "$MANIFEST" ]]; then
  _mf_parse_verdict="$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception as e:
    print('invalid: %s: %s' % (type(e).__name__, e))
    sys.exit(0)
if not isinstance(d, dict):
    print('invalid: manifest top-level JSON is %s, not an object' % type(d).__name__)
    sys.exit(0)
print('ok')
" "$MANIFEST" 2>/dev/null || echo "invalid: python3 manifest parse crashed")"
  if [[ "$_mf_parse_verdict" != "ok" ]]; then
    echo "[compat-lint] FAIL: C1: pack-manifest.json does not parse as a JSON object -- ${_mf_parse_verdict#invalid: } ($MANIFEST)" >&2
    echo "[compat-lint] FAIL: C1: an unparseable manifest is NOT a legacy pack -- legacy-grace requires actually observing 'no contract_version', which requires a readable manifest. Fail closed." >&2
    echo "[compat-lint] ABORT: manifest unparseable, remaining checks skipped" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Legacy-grace early exit (FIX-1, DGN-803)
# ---------------------------------------------------------------------------
# A pack with no contract_version AND kind != "kit" is a legacy (pre-v2-contract)
# pack.  These packs installed successfully before compat-lint existed, so
# blocking them here would be a net-new regression.  Kits are exempt from grace:
# a kit without contract_version is always a FAIL (contract is mandatory for kits).
#
# C4/C5/C6 are also skipped for legacy packs (status-quo preservation: they
# installed without these gates before; adding new blocks now is a regression).
# The gate is a no-op for legacy packs -- exit 0 immediately after a loud log.
#
# Reachable only for a manifest that just passed the parse gate above (file
# exists AND parses AND is a JSON object) -- so the `|| echo ""` fallbacks
# below are now genuinely "field absent in a valid object", never "manifest
# unreadable" (DGN-1004).
if [[ -f "$MANIFEST" ]]; then
  _lg_contract="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('contract_version'); print(v if v is not None else '')" "$MANIFEST" 2>/dev/null || echo "")"
  _lg_kind="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('kind',''))" "$MANIFEST" 2>/dev/null || echo "")"
  if [[ -z "$_lg_contract" && "$_lg_kind" != "kit" ]]; then
    echo "[compat-lint] INFO: legacy pack: no contract_version, v2 contract checks skipped (kind='${_lg_kind:-unset}')"
    echo "[compat-lint] ALL CHECKS PASS (or SKIPped pending payload seed)"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
FAIL_COUNT=0
FAIL_MSGS=""

_fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  local msg="[compat-lint] FAIL: $*"
  echo "$msg" >&2
  FAIL_MSGS="${FAIL_MSGS}${msg}"$'\n'
}

_pass() { echo "[compat-lint] PASS: $*"; }
_skip() { echo "[compat-lint] SKIP: $*"; }
_info() { echo "[compat-lint] INFO: $*"; }
_warn() { echo "[compat-lint] WARN: $*"; }

# _semver_satisfies <version> <range>
# Supports: >=X.Y.Z <A.B.C (space-separated ANDs)
# Returns 0 if satisfied, 1 if not.
# Only implements the subset used by dogany pack manifests.
_semver_satisfies() {
  local ver="$1" range="$2"
  python3 - "$ver" "$range" <<'PYEOF'
import sys, re

def parse_semver(s):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', s.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())

def satisfies(ver_str, range_str):
    ver = parse_semver(ver_str)
    if ver is None:
        sys.stderr.write("invalid version: %s\n" % ver_str)
        sys.exit(2)
    # split range on whitespace; each token is a constraint
    for token in range_str.split():
        token = token.strip()
        if not token:
            continue
        # operator + version
        m = re.match(r'^(>=|>|<=|<|==|!=|\^|~)(.+)$', token)
        if not m:
            sys.stderr.write("unsupported range token: %s\n" % token)
            sys.exit(2)
        op, cv_str = m.group(1), m.group(2)
        cv = parse_semver(cv_str)
        if cv is None:
            sys.stderr.write("invalid constraint version: %s\n" % cv_str)
            sys.exit(2)
        if op == '>='  and not (ver >= cv): sys.exit(1)
        elif op == '>' and not (ver >  cv): sys.exit(1)
        elif op == '<=' and not (ver <= cv): sys.exit(1)
        elif op == '<'  and not (ver <  cv): sys.exit(1)
        elif op == '==' and not (ver == cv): sys.exit(1)
        elif op == '!=' and not (ver != cv): sys.exit(1)
        # ^ and ~ not needed for current manifests -- treat as unsupported
        elif op in ('^', '~'):
            sys.stderr.write("unsupported operator %s in range\n" % op)
            sys.exit(2)
    sys.exit(0)

satisfies(sys.argv[1], sys.argv[2])
PYEOF
}

# _is_semver_range <string>
# Returns 0 if the string is a valid semver range (basic token set), 1 if not.
_is_semver_range() {
  python3 - "$1" <<'PYEOF'
import sys, re
s = sys.argv[1]
tokens = s.split()
if not tokens:
    sys.exit(1)
valid_op = re.compile(r'^(>=|>|<=|<|==|!=)\d+\.\d+\.\d+')
for t in tokens:
    if not valid_op.match(t):
        sys.exit(1)
sys.exit(0)
PYEOF
}

# Kit-token grammar (single source -- DGN-1002 provides_kit rules, reused
# verbatim by service_namespace and requires_kit.kit; no new grammar).
# A token must match the name regex AND not be a framework-reserved name:
# these names double as C4 allowlist tokens (service/<x>/, config/<x>.conf)
# and must never impersonate a framework surface or a payload root.
_KIT_TOKEN_RE='^[a-z][a-z0-9_-]{0,31}$'
_RESERVED_TOKENS=(bridge memory-engine scripts routines service config database mirror skills-bundle payload)

# _kit_token_ok <token> -- 0 if grammar+reservation pass, 1 otherwise
_kit_token_ok() {
  local t="$1" r
  [[ "$t" =~ $_KIT_TOKEN_RE ]] || return 1
  for r in "${_RESERVED_TOKENS[@]}"; do
    [[ "$t" == "$r" ]] && return 1
  done
  return 0
}

# ---------------------------------------------------------------------------
# CHECK 1: manifest required fields + semver range validity
# ---------------------------------------------------------------------------
_info "C1 -- manifest required fields + semver range"

if [[ ! -f "$MANIFEST" ]]; then
  _fail "C1: pack-manifest.json not found: $MANIFEST"
  # Cannot continue without manifest
  echo "[compat-lint] ABORT: manifest missing, remaining checks skipped" >&2
  exit 1
fi

_mf_str() {
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print(v if isinstance(v,(str,int,float)) else '')" "$MANIFEST" "$1" 2>/dev/null || echo ""
}

PACK_VERSION="$(_mf_str pack_version)"
CONTRACT_VERSION="$(_mf_str contract_version)"
REQ_FRAMEWORK="$(_mf_str requires_framework)"
PAYLOAD_ROOT_FIELD="$(_mf_str payload_root)"

if [[ -z "$PACK_VERSION" ]]; then
  _fail "C1: manifest missing required field 'pack_version'"
fi
if [[ -z "$CONTRACT_VERSION" ]]; then
  _fail "C1: manifest missing required field 'contract_version'"
fi
if [[ -z "$REQ_FRAMEWORK" ]]; then
  _fail "C1: manifest missing required field 'requires_framework'"
fi

# Validate pack_version is semver (x.y.z)
if [[ -n "$PACK_VERSION" ]]; then
  if ! printf '%s' "$PACK_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    _fail "C1: pack_version is not valid semver (x.y.z): $PACK_VERSION"
  fi
fi

# Validate requires_framework is a valid semver range
if [[ -n "$REQ_FRAMEWORK" ]]; then
  if ! _is_semver_range "$REQ_FRAMEWORK"; then
    _fail "C1: requires_framework is not a valid semver range: $REQ_FRAMEWORK"
  fi
fi

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  _pass "C1: manifest fields present (pack_version=$PACK_VERSION contract_version=$CONTRACT_VERSION requires_framework=$REQ_FRAMEWORK)"
fi

# Unit vocabulary declaration (DGN-783 B4, additive -- contract_version stays 1).
# A kit SHOULD declare units:{primary,set}; absence is a WARN, never a FAIL
# (existing published kits without the block keep passing; display falls back
# to the i18n key unit.generic).
_MF_KIND="$(_mf_str kind)"
if [[ "$_MF_KIND" == "kit" ]]; then
  _units_present="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('units'), dict) else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_units_present" -eq 1 ]]; then
    _info "C1: kit declares units block"
  else
    _warn "C1: kit manifest has no units block -- display falls back to unit.generic (declare units:{primary,set})"
  fi
fi

# ---------------------------------------------------------------------------
# CHECK C-KIND: pack class vocabulary (DGN-1018 §2; named slot, C1..C7
# untouched; legacy packs never reach here -- legacy-grace exits first).
# A contract manifest's kind must be exactly "kit" or "pack". kind now
# drives the class-branched allowlists (KIT_NAME scope, C4 table, C3/C6
# class skip) AND the pack_install dispatch, so a poisoned value must be
# caught fail-closed before it can mislead any of them -- same logic as the
# poisoned-KIT_NAME ABORT below.
# PACK_CLASS: "kit" | "pack". Contract-absent manifests reaching this point
# are necessarily kind=kit (legacy-grace) => class kit, vocabulary check
# scoped to contract packs only.
# ---------------------------------------------------------------------------
PACK_CLASS="kit"
if [[ -n "$CONTRACT_VERSION" ]]; then
  _kind_desc="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
if 'kind' not in d:
    print('absent')
elif not isinstance(d['kind'], str):
    print('non-string: %r' % (d['kind'],))
else:
    print('str')
" "$MANIFEST" 2>/dev/null || echo "unreadable")"
  if [[ "$_kind_desc" == "str" && ( "$_MF_KIND" == "kit" || "$_MF_KIND" == "pack" ) ]]; then
    PACK_CLASS="$_MF_KIND"
    _pass "C-KIND: kind='$_MF_KIND' is a valid contract pack class (kit|pack)"
  else
    if [[ "$_kind_desc" == "str" ]]; then
      _kind_shown="'$_MF_KIND'"
    else
      _kind_shown="$_kind_desc"
    fi
    _fail "C-KIND: manifest kind ${_kind_shown} invalid -- a contract pack must declare kind exactly 'kit' or 'pack' ('agentpack' is a repo-naming segment, not a manifest kind)"
    echo "[compat-lint] ABORT: kind invalid, remaining checks skipped (a poisoned class value must not drive class-branched allowlists or install dispatch)" >&2
    exit 1
  fi
else
  _info "C-KIND: no contract_version (legacy-shape kit) -- kind vocabulary check applies to contract packs only"
fi

# ---------------------------------------------------------------------------
# Kit identity + capability declaration (DGN-1002; additive -- contract_version
# stays 1, same seam as the DGN-783 B4 units block / DGN-956 skills block).
#
# KIT_NAME (drives C3 database/<kit>.py, C4 service/<kit>/ + config/<kit>.conf,
# C6 database/<kit>.sh):
#   - 'provides_kit' key ABSENT  -> legacy default "lifekit". Packs published
#     before this field keep passing byte-identically.
#   - 'provides_kit' key PRESENT -> fail-closed validation (see header).
#     Validation uses [[ =~ ]] against the WHOLE string (grep would split on
#     embedded newlines and pass a multi-line value if any single line
#     matched). Invalid -> C1 FAIL + ABORT: later checks must never run with
#     a poisoned name (the old literal was inherently safe; a derived value
#     is trusted only after this gate).
#
# DB_LANE (drives C3/C6 capability skip):
#   - capabilities block absent, or empty object -> db_lane=true (today's
#     behavior). Declared true -> same. Declared false -> C3/C6 SKIP, with
#     the C3-side contradiction check (database/ files shipped anyway = FAIL).
#   - Malformed declaration -> C1 FAIL and DB_LANE stays 1 (fail closed in
#     the "more checks, not fewer" direction).
# ---------------------------------------------------------------------------
# KIT_NAME resolution is scoped to kind=kit (DGN-1018 §3, input-layer fix of
# the C1 identity-resolution layer -- DGN-1002 precedent). The legacy
# "lifekit" default only ever existed to keep pre-provides_kit KIT manifests
# byte-identical; a kind=pack manifest has no kit identity at all, so the
# default must not leak into it (that leak is what made C4 misjudge a
# behavior pack's service/<ns>/ as a kit-surface impersonation).
KIT_NAME="lifekit"
_pk_present="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'provides_kit' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
if [[ "$PACK_CLASS" == "pack" ]]; then
  KIT_NAME=""
  if [[ "$_pk_present" -eq 1 ]]; then
    _fail "C1: kind=pack manifest declares provides_kit -- a kit-providing pack is kind=kit (S4b symmetry: provides_kit <=> kind=kit, requires_kit <=> kind=pack)"
  else
    _info "C1: class=pack -- no kit identity (provides_kit correctly absent; C3/C6 kit surfaces not applicable)"
  fi
else
  if [[ "$_pk_present" -eq 1 ]]; then
    KIT_NAME="$(_mf_str provides_kit)"
    # Framework-reserved names: kit name doubles as an allowlist token in C4
    # (service/<kit>/, config/<kit>.conf) -- a kit must not impersonate a
    # framework surface or a payload root. Grammar: _kit_token_ok (shared
    # single source with service_namespace / requires_kit.kit).
    if ! _kit_token_ok "$KIT_NAME"; then
      _fail "C1: provides_kit invalid: '$KIT_NAME' -- must match ^[a-z][a-z0-9_-]{0,31}\$ and not be a framework-reserved name"
      echo "[compat-lint] ABORT: provides_kit invalid, remaining checks skipped (a poisoned kit name must not drive path allowlists)" >&2
      exit 1
    fi
  fi
  _info "C1: kit name resolved: $KIT_NAME (provides_kit $( [[ "$_pk_present" -eq 1 ]] && echo declared || echo "absent -> legacy default" ))"
fi

DB_LANE=1
_caps_verdict="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
caps = d.get("capabilities")
if caps is None:
    print("absent"); sys.exit(0)
if not isinstance(caps, dict):
    print("invalid: capabilities is not an object"); sys.exit(0)
unknown = sorted(set(caps) - {"db_lane"})
if unknown:
    print("invalid: unknown capabilities key(s): %s" % ", ".join(repr(k) for k in unknown)); sys.exit(0)
v = caps.get("db_lane")
if v is None:
    print("absent"); sys.exit(0)   # empty object == nothing declared
if not isinstance(v, bool):
    print("invalid: capabilities.db_lane must be a JSON boolean, got %r" % (v,)); sys.exit(0)
print("true" if v else "false")
PYEOF
)" || _caps_verdict="invalid: manifest JSON unreadable"
# kind=pack: the DB lane is a kit-exclusive surface (DGN-1018 §4). Absent
# block => db_lane=false (a behavior pack has no "today's behavior" to
# preserve, unlike the kit true-default); declared true => FAIL (owning a DB
# lane without a kit facade is a single-writer-rule violation declared in
# writing). File-side smuggling is refused by the C4 database/ FAIL for
# kind=pack -- one declaration-side rule + one file-side rule,
# verdict-consistent, no layer stacking.
[[ "$PACK_CLASS" == "pack" ]] && DB_LANE=0
case "$_caps_verdict" in
  absent)
    ;;  # kit: DB_LANE stays 1 (today's behavior); pack: stays 0 (no DB lane)
  true)
    if [[ "$PACK_CLASS" == "pack" ]]; then
      _fail "C1: kind=pack declares capabilities.db_lane=true -- the DB lane is a kit-exclusive surface (a pack owning a DB lane without a kit facade violates the single-writer rule)"
    else
      _info "C1: capabilities.db_lane=true (declared)"
    fi
    ;;
  false)
    DB_LANE=0
    if [[ "$PACK_CLASS" == "pack" ]]; then
      _info "C1: capabilities.db_lane=false (kind=pack -- consistent: no DB lane)"
    else
      _info "C1: capabilities.db_lane=false -- C3/C6 will be capability-SKIPped (contradiction check applies)"
    fi
    ;;
  invalid:*)
    _fail "C1: capabilities block malformed -- ${_caps_verdict#invalid: } (declaration must be well-formed; fail closed)"
    ;;
esac

# ---------------------------------------------------------------------------
# service_namespace declaration (DGN-1018 §4; C1 declaration-validation
# group, same seam as the capabilities block above). Optional single string;
# the behavior-pack service surface path. Grammar reuses the provides_kit
# rules (_kit_token_ok -- no new grammar). Fail-closed rules:
#   - kind=kit declaring it = FAIL (a kit's service namespace canonical
#     source is provides_kit -- dual truth-source refused)
#   - equal to requires_kit.kit = FAIL (dependency-kit surface impersonation)
#   - declared but payload/service/<ns>/ absent = FAIL (dead declaration,
#     C7 precedent -- checked after payload-root resolution below)
# An invalid value never becomes SERVICE_NS (stays empty), so a poisoned
# namespace can never drive the C4 allowlist.
# ---------------------------------------------------------------------------
SERVICE_NS=""
_ns_probe="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
if 'service_namespace' not in d:
    print('absent')
elif not isinstance(d['service_namespace'], str):
    print('nonstring: %r' % (d['service_namespace'],))
else:
    print('str:' + d['service_namespace'])
" "$MANIFEST" 2>/dev/null || echo "absent")"

# requires_kit.kit raw value (string only; used by the impersonation check
# here and by the C-KITDEP self-dep check below).
_RK_KIT_RAW="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
rk = d.get('requires_kit')
v = rk.get('kit') if isinstance(rk, dict) else None
print(v if isinstance(v, str) else '')
" "$MANIFEST" 2>/dev/null || echo "")"

if [[ "$_ns_probe" != "absent" ]]; then
  if [[ "$PACK_CLASS" == "kit" ]]; then
    _fail "C1: kind=kit manifest declares service_namespace -- a kit's service namespace canonical source is provides_kit (dual truth-source refused)"
  elif [[ "$_ns_probe" == nonstring:* ]]; then
    _fail "C1: service_namespace must be a single string, got ${_ns_probe#nonstring: }"
  else
    _ns_val="${_ns_probe#str:}"
    if ! _kit_token_ok "$_ns_val"; then
      _fail "C1: service_namespace invalid: '$_ns_val' -- must match ^[a-z][a-z0-9_-]{0,31}\$ and not be a framework-reserved name"
    elif [[ -n "$_RK_KIT_RAW" && "$_ns_val" == "$_RK_KIT_RAW" ]]; then
      _fail "C1: service_namespace '$_ns_val' equals requires_kit.kit -- a pack must not impersonate its dependency kit's service surface"
    else
      SERVICE_NS="$_ns_val"
      _info "C1: service_namespace resolved: $SERVICE_NS"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# CHECK C-KITDEP: requires_kit form validation (DGN-681-S4b-RATIFIED §3-A;
# named slot, C1..C7 untouched; after legacy-grace, after the C1 block).
# Form only -- satisfaction against the target instance's DOGANY_PACKS is
# pack_install's install-time inline check (this script does not know
# instance state; interface stays --pack-dir + --framework-version).
# Fail-closed line (DGN-1004): absence observed = SKIP (no kit dependency);
# present but damaged = FAIL (a contract pack that wrote requires_kit opted
# into the contract -- no grace for a damaged declaration).
# ---------------------------------------------------------------------------
_info "C-KITDEP -- requires_kit form validation (S4b-1)"

_rk_probe="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
if 'requires_kit' not in d:
    print('absent'); sys.exit(0)
rk = d['requires_kit']
if not isinstance(rk, dict):
    print('nonobject: %s' % type(rk).__name__); sys.exit(0)
keys = set(rk)
want = {'kit', 'range'}
missing = sorted(want - keys)
extra = sorted(keys - want)
if missing or extra:
    parts = []
    if missing: parts.append('missing key(s): %s' % ', '.join(missing))
    if extra:   parts.append('extra key(s): %s' % ', '.join(extra))
    print('badkeys: %s' % '; '.join(parts)); sys.exit(0)
if not isinstance(rk['kit'], str):
    print('kitnonstring: %r' % (rk['kit'],)); sys.exit(0)
if not isinstance(rk['range'], str):
    print('rangenonstring: %r' % (rk['range'],)); sys.exit(0)
print('ok')
" "$MANIFEST" 2>/dev/null || echo "unreadable")"

if [[ "$_rk_probe" == "absent" ]]; then
  _skip "C-KITDEP: no requires_kit declared -- no kit dependency (form check not applicable)"
else
  CKITDEP_FAIL=0
  # kind restriction: kit->kit dependency install semantics are undefined
  # (undefined = refused, DGN-1004 direction).
  if [[ "$_MF_KIND" == "kit" ]]; then
    _fail "C-KITDEP: kind=kit manifest declares requires_kit -- kit->kit dependency install semantics are undefined (undefined = refused)"
    CKITDEP_FAIL=1
  fi
  case "$_rk_probe" in
    ok)
      _rk_kit="$_RK_KIT_RAW"
      _rk_range="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['requires_kit']['range'])" "$MANIFEST" 2>/dev/null || echo "")"
      if ! _kit_token_ok "$_rk_kit"; then
        _fail "C-KITDEP: requires_kit.kit invalid: '$_rk_kit' -- must match ^[a-z][a-z0-9_-]{0,31}\$ and not be a framework-reserved name"
        CKITDEP_FAIL=1
      else
        _rk_self_id="$(_mf_str id)"
        _rk_self_pk=""
        [[ "$_pk_present" -eq 1 ]] && _rk_self_pk="$(_mf_str provides_kit)"
        if [[ "$_rk_kit" == "$_rk_self_id" || ( -n "$_rk_self_pk" && "$_rk_kit" == "$_rk_self_pk" ) ]]; then
          _fail "C-KITDEP: requires_kit.kit '$_rk_kit' is a self-dependency (equals own id / own provides_kit)"
          CKITDEP_FAIL=1
        fi
      fi
      if ! _is_semver_range "$_rk_range"; then
        _fail "C-KITDEP: requires_kit.range is not a valid semver range: '$_rk_range' (token set: >= > <= < == != + x.y.z)"
        CKITDEP_FAIL=1
      fi
      if [[ "$CKITDEP_FAIL" -eq 0 ]]; then
        _pass "C-KITDEP: requires_kit form valid (kit=$_rk_kit range='$_rk_range')"
      fi
      ;;
    nonobject:*)
      _fail "C-KITDEP: requires_kit must be a JSON object with keys exactly {kit, range}, got ${_rk_probe#nonobject: } (array/string/other refused)"
      ;;
    badkeys:*)
      _fail "C-KITDEP: requires_kit keys must be exactly {kit, range} -- ${_rk_probe#badkeys: }"
      ;;
    kitnonstring:*)
      _fail "C-KITDEP: requires_kit.kit must be a string, got ${_rk_probe#kitnonstring: }"
      ;;
    rangenonstring:*)
      _fail "C-KITDEP: requires_kit.range must be a string, got ${_rk_probe#rangenonstring: }"
      ;;
    *)
      _fail "C-KITDEP: requires_kit unreadable (manifest read error)"
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# CHECK 2: requires_framework vs framework VERSION
# ---------------------------------------------------------------------------
_info "C2 -- requires_framework vs framework VERSION=$FW_VERSION"

if [[ -n "$REQ_FRAMEWORK" ]]; then
  _sat_exit=0
  _semver_satisfies "$FW_VERSION" "$REQ_FRAMEWORK" || _sat_exit=$?
  if [[ "$_sat_exit" -eq 0 ]]; then
    _pass "C2: framework VERSION=$FW_VERSION satisfies requires_framework='$REQ_FRAMEWORK'"
  elif [[ "$_sat_exit" -eq 1 ]]; then
    _fail "C2: framework VERSION=$FW_VERSION does NOT satisfy requires_framework='$REQ_FRAMEWORK'"
  else
    _fail "C2: semver range parse error -- requires_framework='$REQ_FRAMEWORK' version='$FW_VERSION'"
  fi
else
  _fail "C2: requires_framework field absent -- cannot verify framework compatibility (C1 already flagged)"
fi

# ---------------------------------------------------------------------------
# Payload root resolution
# ---------------------------------------------------------------------------
PAYLOAD_ROOT_FIELD="${PAYLOAD_ROOT_FIELD:-payload}"

# Grill fix G-PR1: payload_root must not escape PACK_DIR.
# A manifest-forged payload_root like "../other-dir" would point compat-lint
# at arbitrary filesystem paths, potentially spoofing the allowlist check.
# Resolve both paths and verify containment before trusting the payload_root.
PACK_DIR_REAL="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$PACK_DIR")"
PAYLOAD_DIR_CANDIDATE="$PACK_DIR/$PAYLOAD_ROOT_FIELD"
PAYLOAD_DIR_REAL="$(python3 -c "import os,sys; p=os.path.realpath(sys.argv[1]); print(p)" "$PAYLOAD_DIR_CANDIDATE")"

# BUG-1 fix: use exact equality OR "/*" suffix -- the strip-prefix pattern
# "${X#PREFIX}" == "$X" misidentifies sibling dirs (e.g. /a/b-evil strips
# /a/b to give "-evil", which != original, so it wrongly passes).
if [[ "$PAYLOAD_DIR_REAL" != "$PACK_DIR_REAL" && "$PAYLOAD_DIR_REAL" != "$PACK_DIR_REAL"/* ]]; then
  _fail "C1: payload_root escapes pack boundary: payload_root='$PAYLOAD_ROOT_FIELD' resolves outside $PACK_DIR"
  echo "[compat-lint] ABORT: payload_root boundary violation, remaining checks skipped" >&2
  exit 1
fi
PAYLOAD_DIR="$PAYLOAD_DIR_CANDIDATE"

# service_namespace dead declaration (DGN-1018 §4; C7 precedent -- a declared
# name must have its payload surface). Needs the resolved PAYLOAD_DIR, hence
# checked here rather than in the C1 declaration group above.
if [[ -n "$SERVICE_NS" && ! -d "$PAYLOAD_DIR/service/$SERVICE_NS" ]]; then
  _fail "C1: service_namespace '$SERVICE_NS' declared but payload/service/$SERVICE_NS/ does not exist (dead declaration)"
fi

# Detect whether payload is seeded or only .gitkeep scaffolds exist.
# "Not seeded" = every file under PAYLOAD_DIR is a .gitkeep.
_payload_seeded() {
  if [[ ! -d "$PAYLOAD_DIR" ]]; then
    return 1  # not seeded
  fi
  # Any non-.gitkeep file counts as seeded
  local n
  n="$(find "$PAYLOAD_DIR" -type f ! -name '.gitkeep' | grep -c . || true)"
  [[ "${n:-0}" -gt 0 ]]
}

PAYLOAD_IS_SEEDED=0
if _payload_seeded; then
  PAYLOAD_IS_SEEDED=1
fi

# ---------------------------------------------------------------------------
# CHECK 3: 3-point version consistency (cadence-gate equivalent)
# SKIP if payload not seeded.
# ---------------------------------------------------------------------------
_info "C3 -- version 3-point consistency (EXPECTED_USER_VERSION / migrations / schema PRAGMA)"

if [[ "$PAYLOAD_IS_SEEDED" -eq 0 ]]; then
  _skip "C3: payload not seeded (only .gitkeep present) -- deferred to LS-2 (exit 0, not a gate pass)"
elif [[ "$PACK_CLASS" == "pack" ]]; then
  # Class skip (DGN-1018 §3): C3 is a kit DB-surface check; a kind=pack
  # manifest has no kit identity and no DB lane, so the check does not
  # apply at all. File-side smuggling (shipping database/ anyway) is
  # refused by the C4 database/ FAIL for kind=pack -- one rule per side,
  # verdict-consistent, no layer stacking.
  _skip "C3: kind=pack -- no kit DB surface to version-check (database/ shipping is refused by C4)"
elif [[ "$DB_LANE" -eq 0 ]]; then
  # Capability skip (DGN-1002) -- but a declaration that LIES the other way
  # (db_lane=false while database/ files ship anyway) is refused: skipping
  # validation on shipped DB code is exactly the skip-smuggling this gate
  # exists to catch.
  _db_files=0
  if [[ -d "$PAYLOAD_DIR/database" ]]; then
    _db_files="$(find "$PAYLOAD_DIR/database" -type f ! -name '.gitkeep' | grep -c . || true)"
  fi
  if [[ "${_db_files:-0}" -gt 0 ]]; then
    _fail "C3: manifest declares capabilities.db_lane=false but payload/database/ ships ${_db_files} file(s) -- undeclared DB lane, declaration contradicts payload (skip-smuggling refused)"
  else
    _skip "C3: kit declares capabilities.db_lane=false -- no DB lane to version-check"
  fi
else
  DB_DIR="$PAYLOAD_DIR/database"
  KIT_PY="$DB_DIR/$KIT_NAME.py"
  SCHEMA_SQL="$DB_DIR/schema.sql"
  MIGRATIONS_DIR="$DB_DIR/migrations"

  C3_FAIL=0

  # 3a. EXPECTED_USER_VERSION from <kit>.py
  # FIX-1: extract leading integer only -- inline comments like
  # "EXPECTED_USER_VERSION = 19  # 018_owning_agent_writelayer" caused
  # the old sed to produce "19#018..." which crashes $((10#$EUV)).
  EUV=""
  if [[ -f "$KIT_PY" ]]; then
    EUV="$(grep -E '^EXPECTED_USER_VERSION[[:space:]]*=' "$KIT_PY" 2>/dev/null \
           | head -1 \
           | sed -E 's/^EXPECTED_USER_VERSION[[:space:]]*=[[:space:]]*([0-9]+).*/\1/')"
  fi
  if [[ -z "$EUV" ]]; then
    _fail "C3: EXPECTED_USER_VERSION not found in payload/database/$KIT_NAME.py"
    C3_FAIL=1
  fi

  # 3b. max migration file number from migrations/*.sql
  MAX_MIG=0
  if [[ -d "$MIGRATIONS_DIR" ]]; then
    while IFS= read -r mfile; do
      [[ -n "$mfile" ]] || continue
      base="$(basename "$mfile")"
      # migration files named NNN_*.sql (3-digit zero-padded prefix)
      num="$(printf '%s' "$base" | grep -Eo '^[0-9]+' || true)"
      if [[ -n "$num" ]]; then
        n=$((10#$num))
        if [[ "$n" -gt "$MAX_MIG" ]]; then
          MAX_MIG="$n"
        fi
      fi
    done < <(find "$MIGRATIONS_DIR" -maxdepth 1 -name '*.sql' ! -name '.gitkeep')
  fi

  # 3c. PRAGMA user_version from schema.sql
  PRAGMA_UV=""
  if [[ -f "$SCHEMA_SQL" ]]; then
    PRAGMA_UV="$(grep -iE '^PRAGMA\s+user_version\s*=' "$SCHEMA_SQL" 2>/dev/null \
                 | head -1 | grep -Eo '[0-9]+' | head -1 || true)"
  fi
  if [[ -z "$PRAGMA_UV" ]]; then
    _fail "C3: PRAGMA user_version not found in payload/database/schema.sql"
    C3_FAIL=1
  fi

  # 3d. 3-point consistency check
  if [[ "$C3_FAIL" -eq 0 ]]; then
    EUV_N=$((10#$EUV))
    PRAGMA_N=$((10#$PRAGMA_UV))

    if [[ "$EUV_N" -ne "$MAX_MIG" || "$EUV_N" -ne "$PRAGMA_N" ]]; then
      _fail "C3: version mismatch: EXPECTED_USER_VERSION=$EUV_N, max(migrations)=$MAX_MIG, PRAGMA user_version=$PRAGMA_N -- all three must match"
      C3_FAIL=1
    fi

    # Pin bump without migration file = FAIL (cadence-gate G6 equivalent)
    # If EUV > 0 but no migration file exists with that number -> FAIL
    if [[ "$EUV_N" -gt 0 && "$MAX_MIG" -eq 0 ]]; then
      _fail "C3: EXPECTED_USER_VERSION=$EUV_N but no migration files found -- pin bump requires migration file"
      C3_FAIL=1
    fi
  fi

  # 3e. reversible: marker required on each migration file
  REV_FAIL=0
  if [[ -d "$MIGRATIONS_DIR" ]]; then
    while IFS= read -r mfile; do
      [[ -n "$mfile" ]] || continue
      if ! grep -q '^-- reversible:' "$mfile" 2>/dev/null; then
        _fail "C3: migration file missing '-- reversible:' marker: $(basename "$mfile")"
        REV_FAIL=1
      fi
    done < <(find "$MIGRATIONS_DIR" -maxdepth 1 -name '*.sql' ! -name '.gitkeep')
  fi

  if [[ "$C3_FAIL" -eq 0 && "$REV_FAIL" -eq 0 ]]; then
    _pass "C3: 3-point version consistent (EXPECTED_USER_VERSION=$EUV MAX_MIG=$MAX_MIG PRAGMA=$PRAGMA_UV); reversible markers present"
  fi
fi

# ---------------------------------------------------------------------------
# CHECK 4: payload path allowlist
# ---------------------------------------------------------------------------
# Allowed surfaces (INVENTORY.md §A; <kit> = KIT_NAME from provides_kit,
# DGN-1002):
#   database/*
#   service/<kit>/*
#   skills-bundle/<8 cataloged kinds>/*
#   routines/bundle/*
#   config/<kit>.conf
#   config/i18n*    (i18n fragment keys)
#   requirements.txt (payload root only -- DGN-850 python dependency
#                     declaration; consumed by pack_deps_provision.sh)
#
# FORBIDDEN (framework core -- any match = FAIL):
#   bridge/
#   memory-engine/
#   scripts/    (top-level scripts = framework machinery)
#   routines/<non-bundle path>   (routines/ core)
#   service/<not the kit>/       (other service targets)
#   Any path starting with ../ or /  (path traversal)
# ---------------------------------------------------------------------------
_info "C4 -- payload path allowlist"

# Cataloged skills-bundle kinds (from INVENTORY.md §A, 8 types)
ALLOWED_SKILLS=(
  "task-update"
  "diet-log"
  "workout-log"
  "appointment-log"
  "relationship"
  "relationship-care"
  "spending-log"
  "dogany-routine"
  "ledger-setup"
  "ledger-session"
  "ledger-log"
)

# DGN-956: skill names declared in the manifest skills[] block are ALSO
# allowed skills-bundle payload dirs -- their declarations are validated
# holistically by C7 below (mode vocabulary, dead declarations, template
# collision). The static whitelist keeps covering undeclared skills.
DECLARED_SKILLS="$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
for e in d.get('skills') or []:
    if isinstance(e, dict) and e.get('name'):
        print(e['name'])
" "$MANIFEST" 2>/dev/null || echo "")"

# config file owner name (DGN-1018 §4): kit => <KIT_NAME>.conf,
# pack => <id>.conf (the pack's own id; existing field, no new field).
CONF_OWNER="$KIT_NAME"
if [[ "$PACK_CLASS" == "pack" ]]; then
  CONF_OWNER="$(_mf_str id)"
fi

C4_FAIL=0

if [[ -d "$PAYLOAD_DIR" ]]; then
  while IFS= read -r fpath; do
    [[ -n "$fpath" ]] || continue
    # Relative to PAYLOAD_DIR
    rel="${fpath#"$PAYLOAD_DIR/"}"

    # Path traversal guard: symlinks must not escape the pack boundary.
    # Resolve any symlink and verify it stays within PACK_DIR.
    if [[ -L "$fpath" ]]; then
      resolved="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$fpath" 2>/dev/null || echo "")"
      # realpath of PACK_DIR for prefix comparison
      pack_real="$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$PACK_DIR" 2>/dev/null || echo "$PACK_DIR")"
      # BUG-1 fix: same sibling-dir issue -- use equality + "/*" suffix check.
      if [[ -n "$resolved" && "$resolved" != "$pack_real" && "$resolved" != "$pack_real"/* ]]; then
        _fail "C4: symlink escapes pack boundary: $rel -> $resolved"
        C4_FAIL=1
        continue
      fi
    fi

    # Path traversal in the rel path itself
    case "$rel" in
      ../*|..*|/*)
        _fail "C4: path traversal detected: $rel"
        C4_FAIL=1
        continue
        ;;
    esac

    # .gitkeep files are scaffolding, not payload -- skip
    [[ "$(basename "$fpath")" == ".gitkeep" ]] && continue

    # DGN-850: payload-root requirements.txt is the pack's python dependency
    # declaration (pip-provisioned into the instance runtime interpreters by
    # pack_deps_provision.sh). Payload ROOT only -- nested copies stay subject
    # to their directory's rules.
    [[ "$rel" == "requirements.txt" ]] && continue

    # Allowlist check: rel must match one of the allowed patterns.
    # We check the FIRST path component to catch forbidden roots quickly.
    first_comp="${rel%%/*}"
    allowed=0

    case "$first_comp" in
      database)
        if [[ "$PACK_CLASS" == "pack" ]]; then
          # DGN-1018 §4: database/ is a kit-exclusive surface. A behavior
          # pack ships no DB lane -- ZERO-MIGRATION INVARIANT mechanized.
          _fail "C4: forbidden payload path (database/ is a kit-exclusive surface; kind=pack ships no DB lane): $rel"
          C4_FAIL=1
        else
          # database/* is fully allowed (kit)
          allowed=1
        fi
        ;;
      service)
        # kit:  only service/<kit>/* is allowed; service/<other>/ FORBIDDEN.
        # pack: only service/<service_namespace>/* is allowed; an undeclared
        #       namespace with service/ files present is FORBIDDEN
        #       (DGN-1018 §4 -- declaration precedes surface).
        # Comparison is a quoted [[ == ]] (literal, no glob expansion) and
        # both KIT_NAME and SERVICE_NS passed the C1 fail-closed validation.
        second_comp=""
        rest="${rel#*/}"
        second_comp="${rest%%/*}"
        if [[ "$PACK_CLASS" == "pack" ]]; then
          if [[ -n "$SERVICE_NS" && "$second_comp" == "$SERVICE_NS" ]]; then
            allowed=1
          elif [[ -z "$SERVICE_NS" ]]; then
            _fail "C4: forbidden payload path (service/ files require a valid service_namespace declaration for kind=pack): $rel"
            C4_FAIL=1
          else
            _fail "C4: forbidden payload path (service/<non-namespace>; service_namespace=$SERVICE_NS): $rel"
            C4_FAIL=1
          fi
        elif [[ "$second_comp" == "$KIT_NAME" ]]; then
          allowed=1
        else
          _fail "C4: forbidden payload path (service/<non-kit>; kit=$KIT_NAME): $rel"
          C4_FAIL=1
        fi
        ;;
      skills-bundle)
        # only skills-bundle/<8 allowed kinds>/* is allowed
        rest="${rel#*/}"
        skill_name="${rest%%/*}"
        skill_ok=0
        for sk in "${ALLOWED_SKILLS[@]}"; do
          if [[ "$skill_name" == "$sk" ]]; then
            skill_ok=1
            break
          fi
        done
        # DGN-956: manifest-declared skills pass C4 (validated by C7).
        if [[ "$skill_ok" -eq 0 && -n "$DECLARED_SKILLS" ]]; then
          if printf '%s\n' "$DECLARED_SKILLS" | grep -qxF "$skill_name"; then
            skill_ok=1
          fi
        fi
        if [[ "$skill_ok" -eq 1 ]]; then
          allowed=1
        else
          _fail "C4: forbidden payload path (skills-bundle skill not in catalog): $rel"
          C4_FAIL=1
        fi
        ;;
      routines)
        # only routines/bundle/* is allowed; routines/<anything-else>/ is FORBIDDEN
        rest="${rel#*/}"
        sub="${rest%%/*}"
        if [[ "$sub" == "bundle" ]]; then
          allowed=1
        else
          _fail "C4: forbidden payload path (routines/<non-bundle>): $rel"
          C4_FAIL=1
        fi
        ;;
      config)
        # kit:  only config/<kit>.conf and config/i18n* files.
        # pack: only config/<id>.conf and config/i18n* files (DGN-1018 §4 --
        #       the pack's own id names its config; existing id field reused,
        #       no new field).
        # .conf match is a quoted [[ == ]] (literal -- the owner name never
        # enters a case pattern where glob chars would expand).
        fname="$(basename "$rel")"
        if [[ "$fname" == "$CONF_OWNER.conf" ]]; then
          allowed=1
        else
          case "$fname" in
            i18n.*|i18n_*)
              allowed=1
              ;;
            *)
              case "$rel" in
                config/i18n*)
                  allowed=1
                  ;;
                *)
                  _fail "C4: forbidden config file (only $CONF_OWNER.conf and i18n* fragments allowed): $rel"
                  C4_FAIL=1
                  ;;
              esac
              ;;
          esac
        fi
        ;;
      mirror)
        # DGN-872: mirror/ is lifekit-pack-owned SOURCE (canonical -> pack
        # transfer, DGN-855). Allow source files; forbid runtime residue so
        # this lint and the kit_mirror delivery step (pack_install.sh, the
        # *.db/*.pyc/download.html exclude set) agree on exactly what ships.
        # Without this case mirror/* fell to the default -> C4 abort BEFORE the
        # delivery step ever ran.
        # DGN-1018 §4: mirror/ is a kit-pack-exclusive source surface --
        # FORBIDDEN for kind=pack.
        if [[ "$PACK_CLASS" == "pack" ]]; then
          _fail "C4: forbidden payload path (mirror/ is a kit-pack-exclusive source surface): $rel"
          C4_FAIL=1
          continue
        fi
        fname="$(basename "$rel")"
        case "$fname" in
          *.db|*.db-wal|*.db-shm|*.db-journal|*.db.bak*|*.pyc|download.html)
            _fail "C4: forbidden mirror runtime residue (mirror source only -- no db/pyc/html): $rel"
            C4_FAIL=1
            ;;
          *)
            allowed=1
            ;;
        esac
        ;;
      knowledge)
        # DGN-1018 §4: knowledge/ is a behavior pack's domain asset --
        # allowed for kind=pack ONLY. For kits it stays exactly what it was
        # before this case arm existed: an unknown root (allowing it on kits
        # would open a zero-consumer surface -- refused).
        if [[ "$PACK_CLASS" == "pack" ]]; then
          allowed=1
        else
          _fail "C4: forbidden payload path (not in allowlist): $rel"
          C4_FAIL=1
        fi
        ;;
      bridge|memory-engine|scripts)
        # Absolutely forbidden -- framework core
        _fail "C4: forbidden payload path (framework core surface): $rel"
        C4_FAIL=1
        ;;
      *)
        # Unknown root -- not in allowlist
        _fail "C4: forbidden payload path (not in allowlist): $rel"
        C4_FAIL=1
        ;;
    esac
  done < <(find "$PAYLOAD_DIR" \( -type f -o -type l \))
fi

if [[ "$C4_FAIL" -eq 0 ]]; then
  _pass "C4: payload path allowlist clean"
fi

# ---------------------------------------------------------------------------
# CHECK 4b: payload hygiene -- backup/archive residue + python cache
# FIX-2: C5 token-based gates miss token-free packaging artifacts.
# Block regardless of token content: *.bak, *.bak.*, *.bak-*, *~, *.orig,
# *.swp, *.pyc; any path component named _archive, __pycache__ or
# .pytest_cache (DGN-783 B3: compiled caches must never ride a payload --
# they are machine-local artifacts and can shadow the shipped source).
# ---------------------------------------------------------------------------
_info "C4b -- payload hygiene (backup/archive residue + python cache)"

C4B_FAIL=0
if [[ -d "$PAYLOAD_DIR" ]]; then
  while IFS= read -r fpath; do
    [[ -n "$fpath" ]] || continue
    rel="${fpath#"$PAYLOAD_DIR/"}"
    fname="$(basename "$fpath")"
    violation=""

    # File name patterns
    case "$fname" in
      *.bak|*~|*.orig|*.swp)
        violation="backup/temp file: $rel"
        ;;
      *.bak.*|*.bak-*)
        violation="backup file variant: $rel"
        ;;
      *.pyc)
        violation="compiled python cache file: $rel"
        ;;
    esac

    # Path component: _archive / __pycache__ / .pytest_cache anywhere in the
    # relative path
    if [[ -z "$violation" ]]; then
      case "/$rel/" in
        */_archive/*)
          violation="_archive path component: $rel"
          ;;
        */__pycache__/*)
          violation="__pycache__ path component: $rel"
          ;;
        */.pytest_cache/*)
          violation=".pytest_cache path component: $rel"
          ;;
      esac
    fi

    if [[ -n "$violation" ]]; then
      _fail "C4b: packaging residue in payload -- $violation"
      C4B_FAIL=1
    fi
  done < <(find "$PAYLOAD_DIR" -type f ! -name '.gitkeep')
fi

if [[ "$C4B_FAIL" -eq 0 ]]; then
  _pass "C4b: payload hygiene clean (no backup/archive/python-cache residue)"
fi

# ---------------------------------------------------------------------------
# CHECK 7: kit skills sharing declaration (DGN-956)
# Optional top-level manifest block (additive -- contract_version stays 1,
# same seam as the DGN-783 B4 units block):
#   "skills": [ {"name": "<payload skills-bundle dir>",
#                "sharing_mode": "share"|"own"} ]
# Fail-closed rules:
#   - sharing_mode must be exactly "share" or "own"
#   - a declared name must have a payload skills-bundle dir (dead
#     declarations forbidden)
#   - duplicate declarations for one name are refused
#   - a 'share' skill name must be ABSENT from the framework template bundle
#     (agents/.template/.claude/skills-bundle): update.sh 3j rsync would
#     replace the instance symlink with a real dir -> silent re-divergence
#     (DGN-956 self-grill R2)
# Absent block = SKIP (all payload skills default to sharing_mode=own).
# DOGANY_TEMPLATE_ROOT override exists for test hermeticity only.
# ---------------------------------------------------------------------------
_info "C7 -- kit skills sharing declaration (DGN-956)"

_skills_present="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('skills'), list) else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
if [[ "$_skills_present" -eq 0 ]]; then
  _skip "C7: no skills block in manifest -- all payload skills default to sharing_mode=own"
else
  _TPL_BUNDLE="${DOGANY_TEMPLATE_ROOT:-$SCRIPT_DIR/../../agents/.template}/.claude/skills-bundle"
  _c7_violations="$(python3 - "$MANIFEST" "$PAYLOAD_DIR" "$_TPL_BUNDLE" <<'PYEOF'
import json, os, sys
d = json.load(open(sys.argv[1]))
payload, tpl = sys.argv[2], sys.argv[3]
seen = set()
for e in d.get("skills") or []:
    if not isinstance(e, dict):
        print("skills[] entry is not an object: %r" % (e,))
        continue
    n = e.get("name") or ""
    m = e.get("sharing_mode") or ""
    if not n:
        print("skills[] entry missing 'name'")
        continue
    if n in seen:
        print("duplicate skills[] declaration: '%s'" % n)
        continue
    seen.add(n)
    if m not in ("share", "own"):
        print("skill '%s' has invalid sharing_mode=%r (allowed: share|own)" % (n, m))
    if not os.path.isdir(os.path.join(payload, "skills-bundle", n)):
        print("skill '%s' declared but payload/skills-bundle/%s/ does not exist (dead declaration)" % (n, n))
    if m == "share" and os.path.isdir(os.path.join(tpl, n)):
        print("share skill '%s' collides with a framework template bundle skill -- update.sh 3j rsync would replace the instance symlink with a real dir (R2)" % n)
PYEOF
)" || _c7_violations="skills block unreadable (manifest JSON parse error)"
  if [[ -n "$_c7_violations" ]]; then
    while IFS= read -r _c7v; do
      [[ -n "$_c7v" ]] && _fail "C7: $_c7v"
    done <<< "$_c7_violations"
  else
    _c7_n="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d.get('skills') or []))" "$MANIFEST" 2>/dev/null || echo '?')"
    _pass "C7: skills sharing declaration valid ($_c7_n entrie(s); share|own vocabulary, no dead declarations, no template collision)"
  fi
fi

# ---------------------------------------------------------------------------
# CHECK 5: secret-sweep + persona-token + personal-data gates
# Reuse pack_publish.sh constants; call the same logic on PACK_DIR.
# ---------------------------------------------------------------------------
_info "C5 -- secret-sweep + persona-token + personal-data gates"

# TODO(DGN-803 follow-up): source these from a shared constants file to
# eliminate drift vs pack_publish.sh.
PERSONA_TOKENS_RE='__AGENT_LABEL__|__USER_LABEL__|__AGENT_NAME__|__USER_NAME__'
EXCLUDE_NAMES=( "memories" "USER.md" ".env" ".telegram_bot" )
EXCLUDE_SUFFIXES=( ".db" ".sqlite" ".sqlite3" )
EXCLUDE_TRANSCRIPT_DIRS=( "transcripts" "conversations" "sessions" "chatlog" )

C5_FAIL=0

# Gate (a): personal-data / conversation-memory removal
gate_a_fail=""
while IFS= read -r f; do
  base="$(basename "$f")"
  for ex in "${EXCLUDE_NAMES[@]}"; do
    [[ "$base" == "$ex" ]] && gate_a_fail+="  personal-data file: ${f#"$PACK_DIR/"}"$'\n'
  done
  for sfx in "${EXCLUDE_SUFFIXES[@]}"; do
    [[ "$base" == *"$sfx" ]] && gate_a_fail+="  real-data file: ${f#"$PACK_DIR/"}"$'\n'
  done
done < <(find "$PACK_DIR" -type f ! -path '*/.git/*')
while IFS= read -r d; do
  base="$(basename "$d")"
  for ex in "${EXCLUDE_TRANSCRIPT_DIRS[@]}" "${EXCLUDE_NAMES[@]}"; do
    [[ "$base" == "$ex" ]] && gate_a_fail+="  personal/transcript dir: ${d#"$PACK_DIR/"}"$'\n'
  done
done < <(find "$PACK_DIR" -type d ! -path '*/.git/*')

if [[ -n "$gate_a_fail" ]]; then
  _fail "C5: gate(a) personal-data/conversation-memory violations:"$'\n'"$gate_a_fail"
  C5_FAIL=1
fi

# Gate (b): persona-token residue
if grep -rlE "$PERSONA_TOKENS_RE" "$PACK_DIR" 2>/dev/null \
     | grep -v '/.git/' | grep -q . 2>/dev/null; then
  echo "[compat-lint] FAIL: C5: gate(b) persona-token residue in:" >&2
  grep -rlE "$PERSONA_TOKENS_RE" "$PACK_DIR" 2>/dev/null \
    | grep -v '/.git/' \
    | sed "s|^$PACK_DIR/|  |" >&2
  _fail "C5: gate(b) persona-token(s) found in pack"
  C5_FAIL=1
fi

if [[ "$C5_FAIL" -eq 0 ]]; then
  _pass "C5: secret-sweep + persona-token + personal-data gates PASS"
fi

# ---------------------------------------------------------------------------
# CHECK D-D: publish-signature gate (install-side only, DGN-783 B3)
# MUST run BEFORE C6: C6 executes payload code (<kit>.sh check/dump), so
# an unsigned payload must be refused before any of its code runs.
# Gate: manifest status == "published" AND the catalog row for the manifest
# id carries the SAME status (self-declaration alone is not an anchor -- the
# catalog row is the counter-signature).
# Publish-side lint (no --install-side) skips: pre-publish manifests are
# legitimately draft/scaffold, and C6 there runs the publisher's own code.
# ---------------------------------------------------------------------------
_info "D-D -- publish-signature gate (manifest status x catalog row)"

DD_FAIL=0
if [[ "$INSTALL_SIDE" -eq 1 ]]; then
  _mf_status="$(_mf_str status)"
  _mf_id="$(_mf_str id)"
  if [[ "$_mf_status" != "published" ]]; then
    _fail "D-D: manifest status='${_mf_status:-absent}' != 'published' -- unsigned pack, install refused"
    DD_FAIL=1
  fi
  _cat_status=""
  if [[ -f "$CATALOG_FILE" && -n "$_mf_id" ]]; then
    _cat_status="$(python3 -c "
import json, sys
cat = json.load(open(sys.argv[1]))
for p in cat.get('packs', []):
    if p.get('id') == sys.argv[2]:
        print(p.get('status', ''))
        break
" "$CATALOG_FILE" "$_mf_id" 2>/dev/null || echo "")"
  fi
  if [[ -z "$_cat_status" || "$_cat_status" != "$_mf_status" ]]; then
    _fail "D-D: catalog row status='${_cat_status:-absent}' does not match manifest status='${_mf_status:-absent}' (id='${_mf_id:-absent}' catalog=$CATALOG_FILE) -- signature anchor mismatch, install refused"
    DD_FAIL=1
  fi
  if [[ "$DD_FAIL" -eq 0 ]]; then
    _pass "D-D: manifest status=published and catalog row concurs (id=$_mf_id)"
  fi
else
  _skip "D-D: publish-side lint -- signature gate applies at install only"
fi

# ---------------------------------------------------------------------------
# CHECK 6: CLI contract verb smoke test
# SKIP if payload not seeded, or if the D-D gate failed (payload code must
# never execute past a failed signature gate).
# ---------------------------------------------------------------------------
_info "C6 -- CLI contract verb smoke (${KIT_NAME:-<no kit identity>}.sh check + dump)"

if [[ "$DD_FAIL" -eq 1 ]]; then
  _skip "C6: D-D signature gate FAILED -- payload code NOT executed (verb smoke suppressed)"
elif [[ "$PAYLOAD_IS_SEEDED" -eq 0 ]]; then
  _skip "C6: payload not seeded (only .gitkeep present) -- deferred to LS-2 (exit 0, not a gate pass)"
elif [[ "$PACK_CLASS" == "pack" ]]; then
  # Class skip (DGN-1018 §3): C6 smokes the kit CLI contract surface
  # (database/<kit>.sh); a kind=pack manifest has no kit identity, so
  # there is no contract surface to smoke.
  _skip "C6: kind=pack -- no kit CLI contract surface to smoke"
elif [[ "$DB_LANE" -eq 0 ]]; then
  # Capability skip (DGN-1002). The db_lane=false-vs-shipped-files
  # contradiction is already enforced in C3 -- no payload DB code exists to
  # smoke here by that same guarantee.
  _skip "C6: kit declares capabilities.db_lane=false -- no CLI contract surface to smoke"
else
  KIT_SH="$PAYLOAD_DIR/database/$KIT_NAME.sh"
  C6_FAIL=0

  if [[ ! -f "$KIT_SH" ]]; then
    _fail "C6: payload/database/$KIT_NAME.sh not found -- CLI contract surface missing"
    C6_FAIL=1
  elif [[ ! -x "$KIT_SH" ]]; then
    _fail "C6: payload/database/$KIT_NAME.sh is not executable"
    C6_FAIL=1
  else
    # Run check verb
    if ! "$KIT_SH" check >/dev/null 2>&1; then
      _fail "C6: $KIT_NAME.sh check returned non-zero exit code"
      C6_FAIL=1
    fi
    # Run dump verb
    if ! "$KIT_SH" dump >/dev/null 2>&1; then
      _fail "C6: $KIT_NAME.sh dump returned non-zero exit code"
      C6_FAIL=1
    fi
  fi

  if [[ "$C6_FAIL" -eq 0 ]]; then
    _pass "C6: CLI contract verb smoke PASS ($KIT_NAME.sh check + dump exit 0)"
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ "$FAIL_COUNT" -eq 0 ]]; then
  echo "[compat-lint] ALL CHECKS PASS (or SKIPped pending payload seed)"
  exit 0
else
  echo "[compat-lint] $FAIL_COUNT CHECK(S) FAILED" >&2
  exit 1
fi
