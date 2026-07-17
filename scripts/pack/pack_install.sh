#!/bin/bash
# pack_install.sh -- post-mint pack integration installer (manifest-driven).
#
# Runs the FULL deterministic chain for a named pack after the framework
# mint is complete. Every step is idempotent. Failure stops immediately
# with nonzero exit and the last log line as the error surface.
#
# DGN-366 L1/L2 generalization: the installer is pack-agnostic. Each pack
# ships a pack-manifest.json next to its payload declaring its categories,
# reference identity (slug/root) and idempotency markers. The installer
# preflights and installs ONLY the declared categories -- no hard
# requirements on lib/, knowledge-snapshot.sh or ledger.py remain.
#
# Usage:
#   pack_install.sh <slug> <root> --pack <pack-id>
#                   [--instance-root <path>] [--catalog <file>]
#                   [--model <sonnet|opus|haiku>]
#                   [--migrate-from <peer-root>] [--no-start] [--no-state]
#                   [--dry-run]
#
# Arguments:
#   <slug>          agent slug (kebab-case, must match the minted instance)
#   <root>          absolute path to the minted instance root
#   --pack <id>     pack id from the catalog (required)
#   --instance-root <path>
#                   root of the CALLING instance (the agent running the
#                   mint). Instance-dependent steps (minting_state record)
#                   resolve against it. When absent those steps SKIP with
#                   an explicit log line (never silently).
#   --catalog <file>
#                   catalog file override (default: <repo>/packs/catalog.json
#                   relative to this script). Relative package_dir entries
#                   resolve against the catalog file's directory.
#   --model <m>     requested model for the instance (default: sonnet;
#                   used by step 9 verify)
#   --migrate-from <peer-root>
#                   MIGRATION PATH (DGN-284 #2/#3/#6): this mint migrates an
#                   existing user's records from the main-agent instance at
#                   <peer-root>. Effects: peer integration keys (L1_DB /
#                   L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG) are appended
#                   to agent.conf pointing at <peer-root>, and the domain
#                   seed (when declared) is 'pending_data'. OMITTED
#                   (default) = fresh/standalone mint: NO peer keys, domain
#                   seed 'ready', no migration deferral.
#   --no-start      skip the launchd bot-start step (safe for testing)
#   --no-state      skip step 11 minting_state record (safe for tests)
#   --dry-run       resolve plan + preflight; no writes, exit 0 = plan OK
#
# pack-manifest.json contract (lives at <package_dir>/pack-manifest.json):
#   {
#     "name": "<pack name>",
#     "reference_slug": "<slug the payload was authored for; ALSO the
#                         payload subdirectory name under package_dir>",
#     "reference_root": "<absolute instance root the payload was authored
#                         for; rendered to the minted root at install>",
#     "reference_home": "<optional: home prefix of reference_root; enables
#                         tilde-form and home-prefix rendering>",
#     "agent_md_marker": "<idempotency marker inside AGENT.md.add>",
#     "agent_conf_marker": "<idempotency marker line for agent.conf.add>",
#     "domain_seed": true|false (optional; step 8 runs only when truthy),
#     "categories": [ {"category": "<name>", "required": true|false,
#                      "files": [...]  (optional, 'lib' only)} ... ]
#   }
#   Category names: lib, routines, plists, prompts, agent_conf_fragment,
#   triggers, db_migrations, skills, agent_md_fragment, scripts,
#   knowledge_snapshot.
#
# Steps (all idempotent, all logged to <root>/.telegram_bot/logs/pack-install.log):
#   1. preflight checks (declared categories only)
#   2. package copy (declared categories: lib/ routines/ prompts/ plists/
#      db_migrations/ scripts/ triggers); plists + plists.defer are
#      RENDERED, not copied: launchd labels, plist filenames and reference
#      paths are derived from <slug>/<root> (DGN-284 #1)
#   3. agent.conf fragment append (idempotent via manifest marker; peer
#      integration keys only on the migration path -- DGN-284 #3/#6)
#   4. W01 ledger apply CLI (only when the pack ships lib/ledger.py)
#   5. ledger-inject hook wiring (only when the pack ships
#      routines/ledger-inject.py)
#   6. knowledge snapshot (only when knowledge_snapshot declared)
#   7. refined skills install (SKILL.md overwrites + symlink ensure)
#   7b. AGENT.md fragment append (RENDERED via the same slug/root
#       substitution as plists; idempotent via manifest marker)
#   8. domain seed (only when manifest declares domain_seed; migration
#      path = pending_data, fresh = ready; DGN-284 #2, decision 11)
#   9. model config verify (settings.json model == requested model)
#  10. bot start via mint_run.sh start (honoring plists.defer) [--no-start]
#  11. minting_state record via <instance-root> [--no-state; skips with an
#      explicit log line when --instance-root is absent]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_CATALOG="$REPO_DIR/packs/catalog.json"

# ---------- arg parse -------------------------------------------------------
SLUG="${1:-}"
ROOT="${2:-}"
PACK_ID="" DRY=0 NO_START=0 NO_STATE=0 MODEL_OPT="" MIGRATION=0 PEER_ROOT=""
INSTANCE_ROOT="" CATALOG="$DEFAULT_CATALOG"
shift 2 2>/dev/null || { echo "usage: pack_install.sh <slug> <root> --pack <id> [--instance-root <path>] [--catalog <file>] [--model <sonnet|opus|haiku>] [--migrate-from <peer-root>] [--no-start] [--no-state] [--dry-run]" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)          PACK_ID="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --catalog)       CATALOG="$2"; shift 2 ;;
    --model)         MODEL_OPT="$2"; shift 2 ;;
    --migrate-from)  PEER_ROOT="$2"; MIGRATION=1; shift 2 ;;
    --no-start)      NO_START=1; shift ;;
    --no-state)      NO_STATE=1; shift ;;
    --dry-run|--dry) DRY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
MODEL_OPT="${MODEL_OPT:-sonnet}"

[[ -n "$SLUG" ]] || { echo "ERROR: slug required" >&2; exit 1; }
[[ -n "$ROOT" ]] || { echo "ERROR: root required" >&2; exit 1; }
[[ -n "$PACK_ID" ]] || { echo "ERROR: --pack <id> required" >&2; exit 1; }
if [[ "$MIGRATION" -eq 1 ]]; then
  [[ -n "$PEER_ROOT" ]] || { echo "ERROR: --migrate-from requires a peer root path" >&2; exit 1; }
fi
[[ -f "$CATALOG" ]] || { echo "ERROR: catalog not found: $CATALOG" >&2; exit 1; }
CATALOG_DIR="$(cd "$(dirname "$CATALOG")" && pwd)"

# ---------- logging ---------------------------------------------------------
LOG_DIR="$ROOT/.telegram_bot/logs"
LOG_FILE="$LOG_DIR/pack-install.log"

_log() {
  local ts msg
  ts="$(date '+%Y-%m-%dT%H:%M:%S')"
  msg="$1"
  if [[ "$DRY" -eq 1 ]]; then
    echo "[dry-run] $msg" >&2
  elif [[ -d "$LOG_DIR" ]]; then
    echo "[$ts] $msg" | tee -a "$LOG_FILE"
  else
    # log dir not created yet (pre-preflight) -- stdout only
    echo "[$ts] $msg"
  fi
}

_fail() {
  _log "FATAL: $1"
  exit 1
}

# ---------- resolve pack from the catalog ------------------------------------
PACK_JSON="$(python3 - "$CATALOG" "$PACK_ID" <<'PYEOF'
import json, sys

catalog_path = sys.argv[1]
pack_id = sys.argv[2]

with open(catalog_path) as f:
    cat = json.load(f)

for p in cat.get("packs", []):
    if p["id"] == pack_id:
        print(json.dumps(p))
        sys.exit(0)

print("null")
sys.exit(0)
PYEOF
)"

[[ "$PACK_JSON" != "null" && -n "$PACK_JSON" ]] || _fail "pack not found in catalog: $PACK_ID"

# Read fields from pack JSON
_pack_field() {
  python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get(sys.argv[2]) or '')" "$PACK_JSON" "$1"
}

PACKAGE_DIR="$(_pack_field package_dir)"
DOMAIN_FIELD="$(_pack_field "id")"

[[ -n "$PACKAGE_DIR" ]] || _fail "pack '$PACK_ID' missing 'package_dir' field in catalog"

# Resolve package_dir relative to the CATALOG FILE location (absolute allowed)
if [[ "${PACKAGE_DIR:0:1}" != "/" ]]; then
  PACKAGE_DIR="$CATALOG_DIR/$PACKAGE_DIR"
fi

# ---------- pack manifest (declaration-driven install) ------------------------
MANIFEST="$PACKAGE_DIR/pack-manifest.json"
[[ -f "$MANIFEST" ]] || _fail "pack-manifest.json not found: $MANIFEST (every pack must declare its categories -- no legacy fallback)"

_mf_field() { # _mf_field <key> -- string field ('' when absent)
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
}

PACK_NAME="$(_mf_field name)"
PKG_REF_SLUG="$(_mf_field reference_slug)"
PKG_REF_ROOT="$(_mf_field reference_root)"
PKG_REF_HOME="$(_mf_field reference_home)"
AGENT_MARKER="$(_mf_field agent_md_marker)"
CONF_MARKER="$(_mf_field agent_conf_marker)"
DOMAIN_SEED_DECL="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get('domain_seed') else 0)" "$MANIFEST")"

# categories as "name<TAB>required(0/1)" lines
CATEGORY_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    print("%s\t%d" % (c["category"], 1 if c.get("required") else 0))
PYEOF
)"

# optional explicit file list for the lib category (backward-compat: the
# health pack lib/ carries peer-side files that must NOT deploy)
LIB_FILES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    if c["category"] == "lib":
        for name in c.get("files", []):
            print(name)
PYEOF
)"

has_cat() { # has_cat <name> -- 0 when the category is declared
  printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' -v c="$1" '$1==c{f=1} END{exit f?0:1}'
}
cat_required() { # cat_required <name> -- 0 when declared required:true
  printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' -v c="$1" '$1==c && $2==1{f=1} END{exit f?0:1}'
}

[[ -n "$PACK_NAME" ]]     || _fail "manifest missing 'name': $MANIFEST"
[[ -n "$PKG_REF_SLUG" ]]  || _fail "manifest missing 'reference_slug': $MANIFEST"
[[ -n "$PKG_REF_ROOT" ]]  || _fail "manifest missing 'reference_root': $MANIFEST"
[[ -n "$CATEGORY_LINES" ]] || _fail "manifest declares no categories: $MANIFEST"
if has_cat agent_md_fragment; then
  [[ -n "$AGENT_MARKER" ]] || _fail "manifest declares agent_md_fragment but 'agent_md_marker' is empty: $MANIFEST"
fi
if has_cat agent_conf_fragment; then
  [[ -n "$CONF_MARKER" ]] || _fail "manifest declares agent_conf_fragment but 'agent_conf_marker' is empty: $MANIFEST"
fi

# The payload subdirectory is named after the manifest reference slug
PKG_PAYLOAD="$PACKAGE_DIR/$PKG_REF_SLUG"
PKG_LIB="$PACKAGE_DIR/lib"
PKG_SCRIPTS="$PKG_PAYLOAD/scripts"
KNOWLEDGE_SNAP="$PKG_SCRIPTS/knowledge-snapshot.sh"

# ---------- package reference identity rendering ------------------------------
# The package body was authored for the reference instance declared in the
# manifest. Launchd labels, plist filenames and reference paths inside the
# shipped payload are rewritten to the minted instance's slug and root at
# copy time so a second mint never collides with the reference instance.
_render_to() { # _render_to <src> <dst> -- copy with slug/root/home substitution
  # Order matters: instance root (absolute + tilde form) first, then the
  # remaining home prefix (when the manifest declares reference_home).
  local sed_args=()
  sed_args+=(-e "s|com\.telegram-skill-bot\.${PKG_REF_SLUG}\.|com.telegram-skill-bot.${SLUG}.|g")
  sed_args+=(-e "s|${PKG_REF_ROOT}|${ROOT}|g")
  if [[ -n "$PKG_REF_HOME" && "$PKG_REF_ROOT" == "$PKG_REF_HOME"/* ]]; then
    sed_args+=(-e "s|~${PKG_REF_ROOT#"$PKG_REF_HOME"}|${ROOT}|g")
    sed_args+=(-e "s|${PKG_REF_HOME}|${HOME}|g")
  fi
  sed "${sed_args[@]}" "$1" > "$2"
}

_render_basename() { # _render_basename <basename> -- slug-derived unit filename
  local b="$1"
  printf '%s\n' "${b//.${PKG_REF_SLUG}./.${SLUG}.}"
}

# ---------- preflight (declared categories only) ------------------------------
if [[ "$MIGRATION" -eq 1 ]]; then
  _log "preflight: pack=$PACK_ID root=$ROOT slug=$SLUG path=migration peer=$PEER_ROOT"
else
  _log "preflight: pack=$PACK_ID root=$ROOT slug=$SLUG path=fresh (no peer)"
fi
_log "preflight: manifest=$MANIFEST ref_slug=$PKG_REF_SLUG"

fail=0
if [[ "$MIGRATION" -eq 1 ]]; then
  [[ -d "$PEER_ROOT" ]] || { _log "PREFLIGHT FAIL: --migrate-from peer root not found: $PEER_ROOT"; fail=1; }
fi
[[ -d "$ROOT" ]]        || { _log "PREFLIGHT FAIL: root not found: $ROOT"; fail=1; }
[[ -d "$ROOT/bridge" ]] || { _log "PREFLIGHT FAIL: not a minted instance (no bridge/): $ROOT"; fail=1; }
[[ -d "$PACKAGE_DIR" ]] || { _log "PREFLIGHT FAIL: package_dir not found: $PACKAGE_DIR"; fail=1; }
[[ -d "$PKG_PAYLOAD" ]] || { _log "PREFLIGHT FAIL: package payload subdir ($PKG_REF_SLUG/) not found: $PKG_PAYLOAD"; fail=1; }
[[ -f "$ROOT/.claude/settings.json" ]] || { _log "PREFLIGHT FAIL: settings.json not found: $ROOT/.claude/settings.json"; fail=1; }
command -v python3 >/dev/null 2>&1 || { _log "PREFLIGHT FAIL: python3 not found"; fail=1; }

_preflight_cat() { # _preflight_cat <name> <check-type> <path>
  local name="$1" ctype="$2" path="$3" ok=1
  has_cat "$name" || return 0
  case "$ctype" in
    dir)  [[ -d "$path" ]] && ok=0 ;;
    file) [[ -f "$path" ]] && ok=0 ;;
    exec) [[ -x "$path" ]] && ok=0 ;;
    glob) compgen -G "$path" >/dev/null 2>&1 && ok=0 ;;
  esac
  if [[ "$ok" -ne 0 ]]; then
    if cat_required "$name"; then
      _log "PREFLIGHT FAIL: required category '$name' payload missing: $path"
      fail=1
    else
      _log "preflight: optional category '$name' payload missing ($path) -- will skip"
    fi
  fi
}

_preflight_cat lib                 dir  "$PKG_LIB"
_preflight_cat routines            dir  "$PKG_PAYLOAD/routines"
_preflight_cat plists              glob "$PKG_PAYLOAD/routines/*.plist"
_preflight_cat prompts             dir  "$PKG_PAYLOAD/routines/prompts"
_preflight_cat agent_conf_fragment file "$PKG_PAYLOAD/config/agent.conf.add"
_preflight_cat triggers            file "$PKG_PAYLOAD/config/triggers.yaml"
_preflight_cat db_migrations       dir  "$PKG_PAYLOAD/database/migrations"
_preflight_cat skills              dir  "$PKG_PAYLOAD/skills"
_preflight_cat agent_md_fragment   file "$PKG_PAYLOAD/AGENT.md.add"
_preflight_cat scripts             dir  "$PKG_SCRIPTS"
_preflight_cat knowledge_snapshot  exec "$KNOWLEDGE_SNAP"

[[ "$fail" -eq 0 ]] || exit 1

if [[ "$DRY" -eq 1 ]]; then
  _log "dry-run: preflight OK"
  _log "dry-run: would run full pack-install chain for pack=$PACK_ID -> $ROOT"
  _log "dry-run: package_dir=$PACKAGE_DIR"
  _log "dry-run: declared categories: $(printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' '{printf "%s ", $1}')"
  _log "dry-run: domain_seed declared: $DOMAIN_SEED_DECL"
  if [[ "$MIGRATION" -eq 1 ]]; then
    _log "dry-run: path=migration peer=$PEER_ROOT (peer keys appended, domain seed=pending_data)"
  else
    _log "dry-run: path=fresh (no peer keys, domain seed=ready)"
  fi
  if [[ -n "$INSTANCE_ROOT" ]]; then
    _log "dry-run: instance-root=$INSTANCE_ROOT (minting_state record enabled)"
  else
    _log "dry-run: instance-root NOT supplied -- step 11 minting_state record would SKIP (explicit)"
  fi
  _log "dry-run: no-start=$NO_START"
  echo "[pack_install] DRY-RUN OK -- no writes"
  exit 0
fi

# ---------- ensure log dir exists -------------------------------------------
mkdir -p "$LOG_DIR"
_log "=== pack-install START pack=$PACK_ID slug=$SLUG root=$ROOT ==="

# ---------- STEP 2: package copy (declared categories only) ------------------
_log "step 2: package copy"

# 2a. lib/ -> routines/lib/
if has_cat lib && [[ -d "$PKG_LIB" ]]; then
  mkdir -p "$ROOT/routines/lib"
  if [[ -n "$LIB_FILES" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      if [[ -f "$PKG_LIB/$f" ]]; then
        cp -f "$PKG_LIB/$f" "$ROOT/routines/lib/$f"
        _log "  copied lib/$f -> routines/lib/"
      else
        _log "  WARN: manifest lib file not in package: lib/$f (skipping)"
      fi
    done <<< "$LIB_FILES"
  else
    for f in "$PKG_LIB/"*.py; do
      [[ -e "$f" ]] || continue
      cp -f "$f" "$ROOT/routines/lib/$(basename "$f")"
      _log "  copied lib/$(basename "$f") -> routines/lib/"
    done
  fi
else
  has_cat lib || _log "  category lib not declared -- skipping"
fi

# 2b. routines/ -> routines/ (sh + py files)
if has_cat routines && [[ -d "$PKG_PAYLOAD/routines" ]]; then
  for f in "$PKG_PAYLOAD/routines/"*.sh; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/routines/"
    chmod +x "$ROOT/routines/$(basename "$f")"
    _log "  copied routines/$(basename "$f")"
  done
  for f in "$PKG_PAYLOAD/routines/"*.py; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/routines/"
    _log "  copied routines/$(basename "$f")"
  done
else
  has_cat routines || _log "  category routines not declared -- skipping"
fi

# 2c. plists + plists.defer -- RENDERED, not copied (DGN-284 #1): launchd
#     label + filename derive from the minted slug; package-reference paths
#     rewritten to $ROOT.
if has_cat plists; then
  for f in "$PKG_PAYLOAD/routines/"*.plist; do
    [[ -e "$f" ]] || continue
    dst_base="$(_render_basename "$(basename "$f")")"
    _render_to "$f" "$ROOT/routines/$dst_base"
    _log "  rendered routines/$dst_base (slug-derived label + instance paths)"
  done
  # deferral manifest basenames must match the rendered plist filenames,
  # so it is rendered with the same substitution
  if [[ -f "$PKG_PAYLOAD/routines/plists.defer" ]]; then
    _render_to "$PKG_PAYLOAD/routines/plists.defer" "$ROOT/routines/plists.defer"
    _log "  rendered routines/plists.defer"
  fi
else
  _log "  category plists not declared -- skipping"
fi

# 2d. prompts/
if has_cat prompts && [[ -d "$PKG_PAYLOAD/routines/prompts" ]]; then
  mkdir -p "$ROOT/routines/prompts"
  cp -rf "$PKG_PAYLOAD/routines/prompts/." "$ROOT/routines/prompts/"
  _log "  copied routines/prompts/"
else
  has_cat prompts || _log "  category prompts not declared -- skipping"
fi

# 2e. database/migrations/ -> database/migrations/
if has_cat db_migrations && [[ -d "$PKG_PAYLOAD/database/migrations" ]]; then
  mkdir -p "$ROOT/database/migrations"
  for f in "$PKG_PAYLOAD/database/migrations/"*.sql; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/database/migrations/"
    _log "  copied database/migrations/$(basename "$f")"
  done
else
  has_cat db_migrations || _log "  category db_migrations not declared -- skipping"
fi

# 2f. scripts/ -> scripts/
if has_cat scripts && [[ -d "$PKG_SCRIPTS" ]]; then
  mkdir -p "$ROOT/scripts"
  for f in "$PKG_SCRIPTS/"*.sh; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/scripts/"
    chmod +x "$ROOT/scripts/$(basename "$f")"
    _log "  copied scripts/$(basename "$f")"
  done
else
  has_cat scripts || _log "  category scripts not declared -- skipping"
fi

# 2g. config/triggers.yaml
if has_cat triggers && [[ -f "$PKG_PAYLOAD/config/triggers.yaml" ]]; then
  mkdir -p "$ROOT/config"
  cp -f "$PKG_PAYLOAD/config/triggers.yaml" "$ROOT/config/triggers.yaml"
  _log "  copied config/triggers.yaml"
else
  has_cat triggers || _log "  category triggers not declared -- skipping"
fi

_log "step 2: package copy done"

# ---------- STEP 3: agent.conf fragment append (idempotent) -----------------
if has_cat agent_conf_fragment; then
  _log "step 3: agent.conf append"

  CONF_ADD="$PKG_PAYLOAD/config/agent.conf.add"
  AGENT_CONF="$ROOT/config/agent.conf"
  MARKER="$CONF_MARKER"

  # Peer-integration keys (L1_DB / L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG)
  # belong to the MIGRATION path only (DGN-284 #3/#6): on a fresh/standalone
  # mint a present HANDOFF_PEER_AG makes the consult self-heal refuse to flip
  # pending_data -> ready and strands the instance in consult deferral.
  PEER_KEYS_RE='^(L1_DB|L1_EXPECTED_USER_VERSION|HANDOFF_PEER_AG)='

  if [[ -f "$CONF_ADD" ]]; then
    if grep -qF "$MARKER" "$AGENT_CONF" 2>/dev/null; then
      _log "  agent.conf.add already appended (idempotent, skipping)"
    elif [[ "$MIGRATION" -eq 1 ]]; then
      {
        echo ""
        echo "$MARKER"
        # strip comment-only header lines; point peer keys at the actual peer
        grep -v '^#' "$CONF_ADD" | grep -v '^$' \
          | sed -e "s|^L1_DB=.*|L1_DB=$PEER_ROOT/database/lifekit.db|" \
                -e "s|^HANDOFF_PEER_AG=.*|HANDOFF_PEER_AG=$PEER_ROOT|" || true
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (migration path; peer=$PEER_ROOT)"
    else
      {
        echo ""
        echo "$MARKER"
        echo "# fresh/standalone mint (DGN-284): peer-integration keys"
        echo "# (L1_DB / L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG) intentionally omitted"
        # keep any future non-peer keys the fragment may carry
        grep -v '^#' "$CONF_ADD" | grep -v '^$' | grep -Ev "$PEER_KEYS_RE" || true
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (fresh path; peer keys omitted)"
    fi
  else
    _log "  no agent.conf.add in package (skipping)"
  fi

  _log "step 3: agent.conf done"
else
  _log "step 3: agent.conf append SKIPPED (category agent_conf_fragment not declared)"
fi

# ---------- STEP 4: W01 ledger apply CLI ------------------------------------
# Runs only when the pack actually ships lib/ledger.py (declaration-driven:
# no generic hard requirement on ledger machinery).
if has_cat lib && [[ -f "$PKG_LIB/ledger.py" ]]; then
  _log "step 4: W01 ledger apply (ledger.py apply)"

  LEDGER_PY="$ROOT/routines/lib/ledger.py"
  DB="$ROOT/database/lifekit.db"

  if [[ -f "$LEDGER_PY" && -f "$DB" ]]; then
    cd "$ROOT"
    python3 "$LEDGER_PY" apply --db "$DB" 2>&1 | while IFS= read -r line; do _log "  ledger: $line"; done
    _log "step 4: W01 apply done"
  else
    [[ -f "$LEDGER_PY" ]] || _fail "step 4: ledger.py not found at $LEDGER_PY (package copy step 2 failed?)"
    [[ -f "$DB" ]] || _fail "step 4: lifekit.db not found at $DB (fresh mint incomplete?)"
  fi
else
  _log "step 4: ledger apply SKIPPED (pack ships no lib/ledger.py)"
fi

# ---------- STEP 5: hook wiring (settings.json -- idempotent python edit) ---
# Runs only when the pack ships routines/ledger-inject.py.
SETTINGS="$ROOT/.claude/settings.json"
if has_cat routines && [[ -f "$PKG_PAYLOAD/routines/ledger-inject.py" ]]; then
  _log "step 5: ledger-inject hook wiring"

  LEDGER_HOOK_CMD="/usr/bin/python3 $ROOT/routines/ledger-inject.py"

  python3 - "$SETTINGS" "$LEDGER_HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    s = json.load(f)

hooks = s.setdefault("hooks", {})
ups_list = hooks.setdefault("UserPromptSubmit", [])

# Check if the ledger-inject command is already wired anywhere in UserPromptSubmit
for entry in ups_list:
    # entry may be {"hooks": [...]} or direct {"type": "command", "command": ...}
    if isinstance(entry, dict):
        if entry.get("command") == hook_cmd:
            print("[hook-wire] ledger-inject already wired (idempotent)")
            sys.exit(0)
        for h in entry.get("hooks", []):
            if isinstance(h, dict) and h.get("command") == hook_cmd:
                print("[hook-wire] ledger-inject already wired (idempotent)")
                sys.exit(0)

# Add the hook as a new entry in UserPromptSubmit (same pattern as existing entries)
new_entry = {
    "hooks": [
        {
            "type": "command",
            "command": hook_cmd,
            "timeout": 10
        }
    ]
}
ups_list.append(new_entry)
print("[hook-wire] added ledger-inject to UserPromptSubmit")

with open(settings_path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
PYEOF
  _log "step 5: hook wiring done"
else
  _log "step 5: hook wiring SKIPPED (pack ships no routines/ledger-inject.py)"
fi

# ---------- STEP 6: knowledge snapshot --------------------------------------
if has_cat knowledge_snapshot; then
  _log "step 6: knowledge snapshot"

  SNAPSHOT_SH="$ROOT/scripts/knowledge-snapshot.sh"
  if [[ -x "$SNAPSHOT_SH" ]]; then
    bash "$SNAPSHOT_SH" "$ROOT" 2>&1 | while IFS= read -r line; do _log "  snapshot: $line"; done
    _log "step 6: knowledge snapshot done"
  else
    _log "  WARN: knowledge-snapshot.sh not found at $ROOT/scripts/ -- skipping (pack may not require it)"
  fi
else
  _log "step 6: knowledge snapshot SKIPPED (category knowledge_snapshot not declared)"
fi

# ---------- STEP 7: refined skills install (SKILL.md + symlinks) ------------
if has_cat skills && [[ -d "$PKG_PAYLOAD/skills" ]]; then
  _log "step 7: refined skills install"

  for skill_dir in "$PKG_PAYLOAD/skills/"*/; do
    skill_id="$(basename "$skill_dir")"
    src_skill_md="$skill_dir/SKILL.md"
    bundle_dir="$ROOT/.claude/skills-bundle/$skill_id"
    link_path="$ROOT/.claude/skills/$skill_id"

    if [[ ! -f "$src_skill_md" ]]; then
      _log "  WARN: no SKILL.md in package skills/$skill_id -- skipping"
      continue
    fi

    if [[ ! -d "$bundle_dir" ]]; then
      _log "  WARN: skills-bundle/$skill_id not found in instance (not installed by framework?) -- skipping"
      continue
    fi

    cp -f "$src_skill_md" "$bundle_dir/SKILL.md"
    _log "  overwrote .claude/skills-bundle/$skill_id/SKILL.md"

    # ensure symlink (template wiring: skills/ -> skills-bundle/)
    mkdir -p "$ROOT/.claude/skills"
    if [[ -L "$link_path" ]]; then
      current_target="$(readlink "$link_path")"
      expected_target="../skills-bundle/$skill_id"
      if [[ "$current_target" != "$expected_target" ]]; then
        ln -sfn "$expected_target" "$link_path"
        _log "  re-linked .claude/skills/$skill_id -> ../skills-bundle/$skill_id"
      else
        _log "  symlink .claude/skills/$skill_id already correct (idempotent)"
      fi
    else
      [[ -e "$link_path" ]] && { _log "  WARN: $link_path is not a symlink but exists -- removing"; rm -rf "$link_path"; }
      ln -sfn "../skills-bundle/$skill_id" "$link_path"
      _log "  linked .claude/skills/$skill_id -> ../skills-bundle/$skill_id"
    fi
  done
  _log "step 7: skills install done"
else
  _log "step 7: skills install SKIPPED (category skills not declared)"
fi

# ---------- STEP 7b: AGENT.md fragment append (rendered, idempotent) --------
if has_cat agent_md_fragment; then
  _log "step 7b: AGENT.md fragment append"

  AGENT_MD="$ROOT/AGENT.md"
  AGENT_ADD="$PKG_PAYLOAD/AGENT.md.add"

  if [[ -f "$AGENT_ADD" ]]; then
    if [[ ! -f "$AGENT_MD" ]]; then
      _fail "step 7b: AGENT.md not found at $AGENT_MD (mint incomplete?)"
    fi
    if grep -qF "$AGENT_MARKER" "$AGENT_MD" 2>/dev/null; then
      _log "  AGENT.md fragment already appended (idempotent, skipping)"
    else
      # Render the fragment through the same slug/root substitution as the
      # plists so slug-derived prose lands correctly (DGN-366 L2 step 7b).
      RENDERED_ADD="$(mktemp)"
      _render_to "$AGENT_ADD" "$RENDERED_ADD"
      {
        echo ""
        cat "$RENDERED_ADD"
      } >> "$AGENT_MD"
      rm -f "$RENDERED_ADD"
      _log "  appended AGENT.md.add (rendered) to AGENT.md (marker: $AGENT_MARKER)"
    fi
  else
    _log "  no AGENT.md.add in package (skipping)"
  fi

  _log "step 7b: AGENT.md fragment done"
else
  _log "step 7b: AGENT.md fragment SKIPPED (category agent_md_fragment not declared)"
fi

# ---------- STEP 8: domain seed (declaration-driven, DGN-284 #2) -------------
# Only when the manifest declares domain_seed. Decision 11: migration path =
# pending_data (digest job flips it to ready); fresh/standalone mint = ready.
if [[ "$DOMAIN_SEED_DECL" -eq 1 ]]; then
  DB="$ROOT/database/lifekit.db"
  if [[ "$MIGRATION" -eq 1 ]]; then
    CONSULT_SEED="pending_data"
  else
    CONSULT_SEED="ready"
  fi
  _log "step 8: consult_state seed ($CONSULT_SEED)"

  python3 - "$DB" "$CONSULT_SEED" <<'PYEOF'
import sqlite3, sys

db = sys.argv[1]
seed = sys.argv[2]
conn = sqlite3.connect(db)

# Only seed if the config table exists and consult_state is absent
has_config = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'"
).fetchone()
if not has_config:
    print("[consult_state] config table absent -- ledger apply step may have missed; skipping seed")
    conn.close()
    sys.exit(0)

existing = conn.execute(
    "SELECT value FROM config WHERE key='consult_state'"
).fetchone()
if existing:
    print("[consult_state] already set to %r (idempotent)" % existing[0])
else:
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('consult_state', ?)", (seed,)
    )
    conn.commit()
    print("[consult_state] seeded %s" % seed)
conn.close()
PYEOF
  _log "step 8: consult_state seed done"
else
  _log "step 8: domain seed SKIPPED (manifest declares no domain_seed)"
fi

# ---------- STEP 9: model config verify (requested model) -------------------
_log "step 9: settings.json model verify (requested: $MODEL_OPT)"

python3 - "$SETTINGS" "$MODEL_OPT" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
requested = sys.argv[2]

with open(settings_path) as f:
    s = json.load(f)

model = s.get("model", "")
if model == requested:
    print("[model] confirmed %s (OK)" % requested)
elif not model:
    s["model"] = requested
    with open(settings_path, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    print("[model] set to %s (was absent)" % requested)
else:
    # Pre-existing value differs from requested -- leave it alone (intentional).
    print("[model] already set to %r -- leaving as-is (pre-existing, intentional)" % model)
PYEOF
_log "step 9: model verify done"

# ---------- STEP 10: bot start (mint_run.sh start) --------------------------
if [[ "$NO_START" -eq 1 ]]; then
  _log "step 10: bot start SKIPPED (--no-start)"
else
  _log "step 10: bot start (launchd bootstrap, plists.defer honored)"
  MINT_RUN="$SCRIPT_DIR/mint_run.sh"
  [[ -x "$MINT_RUN" ]] || _fail "step 10: mint_run.sh not found/executable: $MINT_RUN"
  "$MINT_RUN" start --root "$ROOT" 2>&1 | while IFS= read -r line; do _log "  start: $line"; done
  _log "step 10: bot start done"
fi

# ---------- STEP 11: minting_state record (instance-dependent) ---------------
if [[ "$NO_STATE" -eq 1 ]]; then
  _log "step 11: minting_state record SKIPPED (--no-state)"
elif [[ -z "$INSTANCE_ROOT" ]]; then
  _log "step 11: minting_state record SKIPPED (--instance-root not supplied; instance-dependent step -- explicit skip, DGN-366 L1)"
else
  _log "step 11: record mint state (instance-root=$INSTANCE_ROOT)"
  STATE_PY="$INSTANCE_ROOT/.claude/skills-bundle/mint-agent/scripts/minting_state.py"
  if [[ -f "$STATE_PY" ]]; then
    python3 "$STATE_PY" accept "$DOMAIN_FIELD" --agent "$SLUG" 2>&1 | while IFS= read -r line; do _log "  state: $line"; done || true
    _log "step 11: minting_state accept done"
  else
    _log "  minting_state.py not found at $STATE_PY -- skipping accept record (explicit skip)"
  fi
fi

_log "=== pack-install DONE pack=$PACK_ID slug=$SLUG ==="
echo "[pack_install] DONE -- see $LOG_FILE"
