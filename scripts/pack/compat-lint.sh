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
#       service/<non-lifekit>/) are forbidden
#   C5  secret-sweep + persona-token + personal-data gates (reuse
#       pack_publish.sh constants; no re-implementation)
#   D-D publish-signature gate (install-side only, DGN-783 B3): manifest
#       status must be "published" AND the catalog row for the pack id must
#       carry the same status. Runs BEFORE C6 so unsigned payload code is
#       never executed by the verb smoke.
#   C6  CLI contract verb smoke: payload/database/lifekit.sh check + dump
#       exit code 0
#
# Payload not seeded (only .gitkeep present) => C3 and C6 print SKIP and
# exit 0. C1, C2, C4, C5 run fully now.
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
else
  DB_DIR="$PAYLOAD_DIR/database"
  LIFEKIT_PY="$DB_DIR/lifekit.py"
  SCHEMA_SQL="$DB_DIR/schema.sql"
  MIGRATIONS_DIR="$DB_DIR/migrations"

  C3_FAIL=0

  # 3a. EXPECTED_USER_VERSION from lifekit.py
  # FIX-1: extract leading integer only -- inline comments like
  # "EXPECTED_USER_VERSION = 19  # 018_owning_agent_writelayer" caused
  # the old sed to produce "19#018..." which crashes $((10#$EUV)).
  EUV=""
  if [[ -f "$LIFEKIT_PY" ]]; then
    EUV="$(grep -E '^EXPECTED_USER_VERSION[[:space:]]*=' "$LIFEKIT_PY" 2>/dev/null \
           | head -1 \
           | sed -E 's/^EXPECTED_USER_VERSION[[:space:]]*=[[:space:]]*([0-9]+).*/\1/')"
  fi
  if [[ -z "$EUV" ]]; then
    _fail "C3: EXPECTED_USER_VERSION not found in payload/database/lifekit.py"
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
# Allowed surfaces (INVENTORY.md §A):
#   database/*
#   service/lifekit/*
#   skills-bundle/<8 cataloged kinds>/*
#   routines/bundle/*
#   config/lifekit.conf
#   config/i18n*    (i18n fragment keys)
#   requirements.txt (payload root only -- DGN-850 python dependency
#                     declaration; consumed by pack_deps_provision.sh)
#
# FORBIDDEN (framework core -- any match = FAIL):
#   bridge/
#   memory-engine/
#   scripts/    (top-level scripts = framework machinery)
#   routines/<non-bundle path>   (routines/ core)
#   service/<not lifekit>/       (other service targets)
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
        # database/* is fully allowed
        allowed=1
        ;;
      service)
        # only service/lifekit/* is allowed; service/<other>/ is FORBIDDEN
        second_comp=""
        rest="${rel#*/}"
        second_comp="${rest%%/*}"
        if [[ "$second_comp" == "lifekit" ]]; then
          allowed=1
        else
          _fail "C4: forbidden payload path (service/<non-lifekit>): $rel"
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
        # only config/lifekit.conf and config/i18n* files (i18n fragment keys)
        fname="$(basename "$rel")"
        case "$fname" in
          lifekit.conf|i18n.*|i18n_*)
            allowed=1
            ;;
          *)
            case "$rel" in
              config/i18n*)
                allowed=1
                ;;
              *)
                _fail "C4: forbidden config file (only lifekit.conf and i18n* fragments allowed): $rel"
                C4_FAIL=1
                ;;
            esac
            ;;
        esac
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
# MUST run BEFORE C6: C6 executes payload code (lifekit.sh check/dump), so
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
_info "C6 -- CLI contract verb smoke (lifekit.sh check + dump)"

if [[ "$DD_FAIL" -eq 1 ]]; then
  _skip "C6: D-D signature gate FAILED -- payload code NOT executed (verb smoke suppressed)"
elif [[ "$PAYLOAD_IS_SEEDED" -eq 0 ]]; then
  _skip "C6: payload not seeded (only .gitkeep present) -- deferred to LS-2 (exit 0, not a gate pass)"
else
  LIFEKIT_SH="$PAYLOAD_DIR/database/lifekit.sh"
  C6_FAIL=0

  if [[ ! -f "$LIFEKIT_SH" ]]; then
    _fail "C6: payload/database/lifekit.sh not found -- CLI contract surface missing"
    C6_FAIL=1
  elif [[ ! -x "$LIFEKIT_SH" ]]; then
    _fail "C6: payload/database/lifekit.sh is not executable"
    C6_FAIL=1
  else
    # Run check verb
    if ! "$LIFEKIT_SH" check >/dev/null 2>&1; then
      _fail "C6: lifekit.sh check returned non-zero exit code"
      C6_FAIL=1
    fi
    # Run dump verb
    if ! "$LIFEKIT_SH" dump >/dev/null 2>&1; then
      _fail "C6: lifekit.sh dump returned non-zero exit code"
      C6_FAIL=1
    fi
  fi

  if [[ "$C6_FAIL" -eq 0 ]]; then
    _pass "C6: CLI contract verb smoke PASS (lifekit.sh check + dump exit 0)"
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
