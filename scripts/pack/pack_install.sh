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
#                      "files": [...]  (optional, 'lib' only)} ... ],
#     "knowledge": { ... }  (optional; REQUIRED together with the
#                      knowledge_snapshot category -- half declaration is a
#                      preflight FAIL. DGN-402 knowledge wiring standard;
#                      schema + authoring rules: docs/KNOWLEDGE-WIRING.md)
#   }
#   Category names: lib, routines, plists, prompts, agent_conf_fragment,
#   triggers, db_migrations, skills, agent_md_fragment, scripts,
#   knowledge_snapshot.
#
# Steps (all idempotent, all logged to <root>/.telegram_bot/logs/pack-install.log):
#   1. preflight checks (declared categories only)
#   2. package copy (declared categories: lib/ routines/ prompts/ plists/
#      db_migrations/ scripts/ triggers); plists are RENDERED, not copied:
#      launchd labels, plist filenames and reference paths are derived from
#      <slug>/<root> (DGN-284 #1). A bundled plists.defer is MERGE-APPENDED
#      into the instance framework defer (DGN-227 MINOR-5), not clobbered.
#   3. agent.conf fragment append (idempotent via manifest marker; peer
#      integration keys only on the migration path -- DGN-284 #3/#6)
#   4. W01 ledger apply CLI (only when the pack ships lib/ledger.py)
#   5. ledger-inject hook wiring (only when the pack ships
#      routines/ledger-inject.py)
#   6. knowledge snapshot (only when knowledge_snapshot declared; source
#      resolves to the bundled frozen snapshot at
#      <package_dir>/<reference_slug>/knowledge/<warehouse>/ when present
#      (DGN-227 B5 delivery channel), else falls back to manifest
#      knowledge.source publisher-local path -- DGN-402)
#   7. skills install, two modes: REFINE (instance bundle dir exists -> render
#      SKILL.md only, DGN-402) and NET-NEW (bundle dir absent -> install the
#      whole payload skill directory, text rendered / binaries copied,
#      DGN-227 B6). Both preserve-register pack-owned and reconcile against
#      the install ledger (D1)
#   7b. AGENT.md fragment append (RENDERED via the same slug/root
#       substitution as plists; idempotent via manifest marker)
#   7c. knowledge wiring selftest (knowledge_selftest.sh: gates G1-G4 for
#       warehouse packs, inverse check for warehouse-less packs; zero-model,
#       exit != 0 = install FAIL -- DGN-402)
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
INSTANCE_ROOT="" CATALOG="$DEFAULT_CATALOG" UPGRADE=0
shift 2 2>/dev/null || { echo "usage: pack_install.sh <slug> <root> --pack <id> [--instance-root <path>] [--catalog <file>] [--model <sonnet|opus|haiku>] [--migrate-from <peer-root>] [--upgrade] [--no-start] [--no-state] [--dry-run]" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)          PACK_ID="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --catalog)       CATALOG="$2"; shift 2 ;;
    --model)         MODEL_OPT="$2"; shift 2 ;;
    --migrate-from)  PEER_ROOT="$2"; MIGRATION=1; shift 2 ;;
    --upgrade)       UPGRADE=1; shift ;;
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
# DGN-227 B3/P6: catalog entry pack_version (semver). Legacy entries without
# the field install as 'unversioned' (loud in the ledger header).
PACK_VERSION="$(_pack_field pack_version)"
PACK_VERSION="${PACK_VERSION:-unversioned}"

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

# Validate every declared category name against the known whitelist (M3).
# An unknown name is a manifest error -- FATAL at preflight with a clear message.
KNOWN_CATEGORIES="lib routines plists prompts agent_conf_fragment triggers db_migrations skills agent_md_fragment scripts knowledge_snapshot"

_cat_validate_out="$(python3 - "$MANIFEST" "$KNOWN_CATEGORIES" <<'PYEOF'
import json, sys
known = set(sys.argv[2].split())
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    name = c["category"]
    if name not in known:
        # Print a sentinel line; exit 0 so set -e does not swallow the message.
        print("UNKNOWN_CATEGORY: manifest category %r is not in the known category whitelist: %s" % (
            name, sys.argv[1]))
        sys.exit(0)
PYEOF
)"
if [[ "$_cat_validate_out" == UNKNOWN_CATEGORY:* ]]; then
  _fail "${_cat_validate_out#UNKNOWN_CATEGORY: }"
fi

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

# ---------- knowledge object (DGN-402 knowledge wiring standard) --------------
# Single source of truth for warehouse wiring = the pack manifest 'knowledge'
# object. It must appear together with the knowledge_snapshot category (half
# declaration = preflight FAIL below). The install path NEVER reads the
# catalog.json knowledge prose (display-only).
KNOWLEDGE_DECL="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('knowledge'), dict) else 0)" "$MANIFEST")"

KNOW_WAREHOUSE="" KNOW_SOURCE="" KNOW_SMOKE_ITEM="" KNOW_SMOKE_ARGS=""
KNOW_CONSUMER_LINES="" KNOW_TURN_LINES="" KNOW_CONSUMER_IDS=""
if [[ "$KNOWLEDGE_DECL" -eq 1 ]]; then
  _know_str() { # _know_str <key> -- knowledge.<key> string field ('' when absent)
    python3 -c "import json,sys; k=json.load(open(sys.argv[1])).get('knowledge') or {}; v=k.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
  }
  KNOW_WAREHOUSE="$(_know_str warehouse)"
  KNOW_SOURCE="$(_know_str source)"
  KNOW_SMOKE_ITEM="$(_know_str smoke_item)"
  KNOW_SMOKE_ARGS="$(_know_str smoke_args)"
  # '~' expansion for the publisher source path (spec S1 layer 1)
  KNOW_SOURCE="${KNOW_SOURCE/#\~/$HOME}"
  # consumer_skills as "skill<TAB>domain domain ..." lines
  KNOW_CONSUMER_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for skill, domains in (k.get("consumer_skills") or {}).items():
    if not isinstance(domains, list):
        domains = []
    print("%s\t%s" % (skill, " ".join(str(d) for d in domains)))
PYEOF
)"
  # turns as "type<TAB>home" lines
  KNOW_TURN_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for t in k.get("turns") or []:
    if isinstance(t, dict):
        print("%s\t%s" % (t.get("type", ""), t.get("home", "")))
PYEOF
)"
  KNOW_CONSUMER_IDS="$(printf '%s\n' "$KNOW_CONSUMER_LINES" | awk -F'\t' 'NF{printf "%s ", $1}')"
fi

_is_consumer_skill() { # _is_consumer_skill <skill-id> -- 0 when in manifest set
  local id="$1" c
  for c in $KNOW_CONSUMER_IDS; do
    [[ "$c" == "$id" ]] && return 0
  done
  return 1
}

# DGN-227 B6: manifest top-level 'net_new_skills' (optional array of skill ids)
# lets a pack DECLARE a skill as net-new (brand-new domain skill it brings in,
# no pre-existing instance bundle dir). The preflight rule-3 "instance bundle
# dir exists" requirement is waived for a skill that is either declared here OR
# whose payload provides a full directory (files beyond SKILL.md).
NET_NEW_SKILLS="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
for s in d.get("net_new_skills") or []:
    print(s)
PYEOF
)"
_is_declared_net_new() { # _is_declared_net_new <skill-id> -- 0 when declared
  local id="$1" s
  while IFS= read -r s; do
    [[ -n "$s" && "$s" == "$id" ]] && return 0
  done <<< "$NET_NEW_SKILLS"
  return 1
}
# _skill_payload_is_full_dir <skill-id> -- 0 when the pack payload skills/<id>/
# carries files beyond SKILL.md (multi-file skill dir), i.e. eligible for the
# net-new install mode by shape.
_skill_payload_is_full_dir() {
  local id="$1" n
  n="$(find "$PKG_PAYLOAD/skills/$id" -type f 2>/dev/null | grep -cv '/SKILL\.md$' || true)"
  [[ "${n:-0}" -gt 0 ]]
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

# _is_text_file <path> -- 0 when the file is text (render-eligible), 1 when
# binary (copy verbatim). Used by the B6 net-new skill directory install mode
# to decide render-vs-copy per file (spec B6: "text files -> render pipeline,
# binaries copied as-is"). NUL-byte probe (grep -Iq): a file with a NUL byte in
# the first chunk is treated as binary, matching git/POSIX text heuristics.
_is_text_file() {
  LC_ALL=C grep -Iq . "$1" 2>/dev/null || {
    # grep -I returns nonzero on binary OR empty file; an empty file is text.
    [[ -s "$1" ]] && return 1
  }
  return 0
}

# _subst_mint_tokens <file> -- substitute the mint identity tokens in place.
# Substitutes exactly:
#   __(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__
# Values sourced from <root>/.instance.conf (DOGANY_* fields) plus
# config/agent.conf AGENT_LANG. Mirrors update.sh subst_one (~L1053-1066):
# minimal sed, no other tokens; identity tokens are substituted only when the
# instance identity is complete (never write empty labels -- residue is caught
# by the G4 unrendered-token gate instead).
# CROSS-REF: the token list appears in four places that must stay in sync:
#   (1) mint.sh sanity check (~L504 alternation)
#   (2) update.sh subst_one (~L1053-1066)
#   (3) pack_install.sh _subst_mint_tokens (this function)
#   (4) G4 unrendered-token check (scripts/pack/knowledge_selftest.sh)
# When adding a token, update all four sites and their cross-ref comments.
_subst_mint_tokens() {
  local f="$1" tmp
  local agent_name="" agent_label="" user_label="" agent_prefix="" agent_lang=""
  if [[ -f "$ROOT/.instance.conf" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$ROOT/.instance.conf"
    agent_name="${DOGANY_AGENT_NAME:-}"
    agent_label="${DOGANY_AGENT_LABEL:-}"
    user_label="${DOGANY_USER_LABEL:-}"
    # optional field (absent on pre-DGN-213 instances) -- same fallback as update.sh
    agent_prefix="${DOGANY_AGENT_PREFIX:-[agent]}"
  fi
  agent_lang="$(grep -E '^AGENT_LANG=' "$ROOT/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
  agent_lang="${agent_lang:-en}"

  local sed_args=(-e "s#__PROJECT_ROOT__#${ROOT}#g" -e "s#__HOME__#${HOME}#g")
  if [[ -n "$agent_name" && -n "$agent_label" && -n "$user_label" ]]; then
    sed_args+=(-e "s#__AGENT_NAME__#${agent_name}#g" \
               -e "s#__AGENT_LABEL__#${agent_label}#g" \
               -e "s#__USER_LABEL__#${user_label}#g" \
               -e "s#__AGENT_PREFIX__#${agent_prefix}#g" \
               -e "s#__AGENT_LANG__#${agent_lang}#g")
  else
    _log "  WARN: instance identity incomplete (.instance.conf) -- identity token substitution skipped (any residue FAILs the knowledge selftest)"
  fi
  tmp="$(mktemp)"
  sed "${sed_args[@]}" "$f" > "$tmp"
  mv "$tmp" "$f"
}

# _preserve_register <relpath> -- idempotently add an instance-root-relative
# path to <root>/.claude/.dogany-preserve, tagged pack-owned, so update.sh
# section 3j does not clobber the pack-installed SKILL.md on framework
# refresh (DGN-402 layer 3 ownership; update.sh build_preserve_excludes
# already honors file-level entries). A pre-existing entry for the same path
# (pack-owned or hand-written) is left untouched.
_preserve_register() {
  local rel="$1"
  local pf="$ROOT/.claude/.dogany-preserve"
  if [[ ! -f "$pf" ]]; then
    {
      echo "# .dogany-preserve -- instance-local files update.sh must NOT refresh."
      echo "# One instance-root-relative path per line (trailing '/' = directory)."
      echo "# Lines tagged '# pack:<pack-id>' are managed by pack_install.sh."
    } > "$pf"
    _log "  created .claude/.dogany-preserve"
  fi
  if awk -v p="$rel" '{ line=$0; sub(/#.*/, "", line);
                        gsub(/^[[:space:]]+|[[:space:]]+$/, "", line);
                        if (line == p) found=1 }
                      END { exit found ? 0 : 1 }' "$pf"; then
    _log "  preserve entry already present: $rel (idempotent)"
  else
    # DGN-227 D1/P18: pack-owned tail tag '# pack:<id>' (machine-readable;
    # untagged lines = hand-written = untouchable).
    printf '%s  # pack:%s\n' "$rel" "$PACK_ID" >> "$pf"
    _log "  preserve-registered: $rel (pack:$PACK_ID)"
  fi
}

# ---------------------------------------------------------------------------
# DGN-227 B3/P25: installed-files ledger (config/packs/<id>.files).
# Single record function called at EVERY copy point (H1-9: the ledger is not
# a side product -- every install write funnels through _ledger_record).
# The ledger is (a) the --upgrade removal-diff source (D2) and (b) the
# preserve-reconcile verdict source (D1).
# ---------------------------------------------------------------------------
LEDGER_DIR="$ROOT/config/packs"
LEDGER_FILE="$LEDGER_DIR/$PACK_ID.files"
LEDGER_STAGE=""

_ledger_record() { # _ledger_record <root-relative-path>
  [[ -n "$LEDGER_STAGE" ]] || LEDGER_STAGE="$(mktemp)"
  printf '%s\n' "$1" >> "$LEDGER_STAGE"
}

_ledger_finalize() {
  mkdir -p "$LEDGER_DIR"
  {
    echo "# pack install ledger -- DGN-227 B3/P25"
    echo "# pack: $PACK_ID"
    echo "# pack_version: $PACK_VERSION"
    echo "# installed: $(date '+%Y-%m-%dT%H:%M:%S')"
    if [[ -n "$LEDGER_STAGE" && -s "$LEDGER_STAGE" ]]; then
      sort -u "$LEDGER_STAGE"
    fi
  } > "$LEDGER_FILE"
  [[ -n "$LEDGER_STAGE" ]] && rm -f "$LEDGER_STAGE"
  _log "  ledger written: config/packs/$PACK_ID.files"
}

# _ledger_paths <file> -- entries only (comments stripped).
_ledger_paths() {
  [[ -f "$1" ]] || return 0
  grep -v '^#' "$1" | grep -v '^[[:space:]]*$' || true
}

# _preserve_reconcile -- DGN-227 D1/P18: verdict source REPLACED. Candidates =
# lines tail-tagged '# pack:<this-id>' (legacy '# pack-owned: <this-id>' lines
# are also candidates and get migrated/removed -- rehearsal note: legacy-tag
# handling is not specified by the spec, see OPEN QUESTIONS). Keep/remove =
# is the path in THIS install's ledger (config/packs/<id>.files). Untagged
# (hand-written) lines are never touched.
_preserve_reconcile() {
  local pf="$ROOT/.claude/.dogany-preserve"
  [[ -f "$pf" ]] || return 0
  local tmp removed=0 line path is_cand
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    is_cand=0
    case "$line" in
      *"# pack:${PACK_ID}") is_cand=1 ;;
      *"# pack-owned: ${PACK_ID} "*|*"# pack-owned: ${PACK_ID}") is_cand=1 ;;
    esac
    if [[ "$is_cand" -eq 1 ]]; then
      path="${line%%#*}"
      path="$(printf '%s' "$path" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      if ! _ledger_paths "$LEDGER_FILE" | grep -qxF "$path"; then
        _log "  preserve reconcile: removed stale pack entry (not in ledger): $path"
        removed=1
        continue
      fi
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$pf"
  if [[ "$removed" -eq 1 ]]; then
    mv "$tmp" "$pf"
  else
    rm -f "$tmp"
  fi
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

# ---- knowledge wiring preflight (DGN-402 spec v2.1 S1/S2) -------------------
# Rule 1: manifest 'knowledge' object and knowledge_snapshot category must
#         appear together (half declaration = manifest error).
if has_cat knowledge_snapshot && [[ "$KNOWLEDGE_DECL" -eq 0 ]]; then
  _log "PREFLIGHT FAIL: category knowledge_snapshot declared without a manifest 'knowledge' object (half declaration)"
  fail=1
fi
if [[ "$KNOWLEDGE_DECL" -eq 1 ]]; then
  if ! has_cat knowledge_snapshot; then
    _log "PREFLIGHT FAIL: manifest 'knowledge' object declared without the knowledge_snapshot category (half declaration)"
    fail=1
  fi
  # Rule 2: knowledge_snapshot is valid only together with the scripts
  #         category -- STEP 6 runs the snapshot script STEP 2f installs.
  if ! has_cat scripts; then
    _log "PREFLIGHT FAIL: knowledge declared but 'scripts' category missing (STEP 6 runs the script STEP 2f copies to <root>/scripts/)"
    fail=1
  fi
  [[ -n "$KNOW_WAREHOUSE" ]] || { _log "PREFLIGHT FAIL: knowledge.warehouse is empty"; fail=1; }
  # Rule 3: consumer_skills nonempty; each skill must exist in the pack
  #         payload skills/. The "instance skills-bundle dir exists" requirement
  #         (DGN-402) is WAIVED (DGN-227 B6) when the skill is net-new -- either
  #         declared in manifest net_new_skills OR the payload provides a full
  #         directory (files beyond SKILL.md). STEP 7 net-new mode installs the
  #         whole directory in that case (no pre-existing bundle dir needed).
  if [[ -z "$KNOW_CONSUMER_LINES" ]]; then
    _log "PREFLIGHT FAIL: knowledge.consumer_skills is empty"
    fail=1
  else
    while IFS=$'\t' read -r _ck _cd; do
      [[ -n "$_ck" ]] || continue
      [[ -f "$PKG_PAYLOAD/skills/$_ck/SKILL.md" ]] || { _log "PREFLIGHT FAIL: consumer skill '$_ck' not in pack payload skills/"; fail=1; }
      if [[ ! -d "$ROOT/.claude/skills-bundle/$_ck" ]]; then
        if _is_declared_net_new "$_ck" || _skill_payload_is_full_dir "$_ck"; then
          _log "preflight: consumer skill '$_ck' net-new (no instance bundle dir) -- STEP 7 will install the full directory (B6)"
        else
          _log "PREFLIGHT FAIL: consumer skill '$_ck' has no instance skills-bundle dir and is not net-new (declare in net_new_skills or ship a full payload dir): $ROOT/.claude/skills-bundle/$_ck"
          fail=1
        fi
      fi
    done <<< "$KNOW_CONSUMER_LINES"
  fi
  # Rule 4: turns nonempty; type T1/T2/T3 only; home is an instance-root-
  #         relative pack artifact path (existence is enforced post-install
  #         by the STEP 7c gate G4 on <root>/<home>).
  if [[ -z "$KNOW_TURN_LINES" ]]; then
    _log "PREFLIGHT FAIL: knowledge.turns is empty"
    fail=1
  else
    while IFS=$'\t' read -r _tt _th; do
      [[ -n "$_tt$_th" ]] || continue
      case "$_tt" in
        T1|T2|T3) : ;;
        *) _log "PREFLIGHT FAIL: knowledge.turns type must be T1/T2/T3 (got '$_tt')"; fail=1 ;;
      esac
      [[ -n "$_th" ]] || { _log "PREFLIGHT FAIL: knowledge.turns entry has empty home"; fail=1; }
      case "$_th" in
        /*|*..*) _log "PREFLIGHT FAIL: knowledge.turns home must be an instance-root-relative pack artifact path (got '$_th')"; fail=1 ;;
      esac
    done <<< "$KNOW_TURN_LINES"
  fi
fi

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

# ---------- NM3: payload checksum verification GATE -------------------------
# DGN-227 B4-5 / D2: before applying ANY payload, verify each shipped file
# against the pack's checksums.sha manifest (sha256). A mismatch or a listed
# file missing on disk = corrupt/tampered payload -> loud-FAIL the install
# (never warn-continue). checksums.sha lines are '<sha256hex>  <relpath>' with
# relpath relative to <package_dir> (the publish pipeline generates it, B4-5).
# Absent checksums.sha = legacy/dev pack: loud WARN (no silent skip), install
# continues -- a published pack MUST ship it (publish gate, B4-5); this arms
# the gate for packs that carry it without breaking pre-NM3 packs.
CHECKSUMS_FILE="$PACKAGE_DIR/checksums.sha"
if [[ -f "$CHECKSUMS_FILE" ]]; then
  _log "NM3: verifying payload against checksums.sha"
  _nm3_out="$(python3 - "$PACKAGE_DIR" "$CHECKSUMS_FILE" <<'PYEOF'
import hashlib, os, sys
pkg_dir, sums = sys.argv[1], sys.argv[2]
bad = []
n = 0
with open(sums) as f:
    for raw in f:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # '<hex>  <relpath>' -- split on the first run of spaces (relpath may
        # contain single spaces, so split(None, 1) then re-strip is unsafe;
        # the publish format uses exactly two spaces as the separator).
        parts = line.split("  ", 1)
        if len(parts) != 2:
            bad.append("MALFORMED: %r" % line)
            continue
        want, rel = parts[0].strip(), parts[1].strip()
        p = os.path.join(pkg_dir, rel)
        if not os.path.isfile(p):
            bad.append("MISSING: %s" % rel)
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != want:
            bad.append("MISMATCH: %s (want %s got %s)" % (rel, want[:12], got[:12]))
        n += 1
if bad:
    for b in bad:
        print("NM3FAIL " + b)
    sys.exit(1)
print("NM3OK verified %d files" % n)
PYEOF
)" || {
    while IFS= read -r _l; do _log "  ${_l}"; done <<< "$_nm3_out"
    _fail "NM3: payload checksum verification FAILED -- corrupt/tampered payload, install aborted (B4-5 gate)"
  }
  _log "  ${_nm3_out}"
  _log "NM3: checksum verification passed"
else
  _log "NM3: WARN -- no checksums.sha in package ($CHECKSUMS_FILE) -- verification SKIPPED (legacy/dev pack; a published pack MUST ship it per B4-5, loud not silent)"
fi

# DGN-227 D2 note: the --upgrade stale-removal phase (ledger diff + bootout +
# NM3 backup + removal + ledger re-record) lives AFTER the apply steps, next
# to _preserve_reconcile -- the old ledger stays untouched on disk until
# _ledger_finalize there, so the diff source survives the whole apply pass.
if [[ "${UPGRADE:-0}" -eq 1 ]]; then
  _log "=== pack-install UPGRADE pack=$PACK_ID slug=$SLUG root=$ROOT ==="
fi

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
        _ledger_record "routines/lib/$f"
        _preserve_register "routines/lib/$f"
        _log "  copied lib/$f -> routines/lib/"
      else
        _log "  WARN: manifest lib file not in package: lib/$f (skipping)"
      fi
    done <<< "$LIB_FILES"
  else
    for f in "$PKG_LIB/"*.py; do
      [[ -e "$f" ]] || continue
      cp -f "$f" "$ROOT/routines/lib/$(basename "$f")"
      _ledger_record "routines/lib/$(basename "$f")"
      _preserve_register "routines/lib/$(basename "$f")"
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
    _ledger_record "routines/$(basename "$f")"
    _preserve_register "routines/$(basename "$f")"
    _log "  copied routines/$(basename "$f")"
  done
  for f in "$PKG_PAYLOAD/routines/"*.py; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/routines/"
    _ledger_record "routines/$(basename "$f")"
    _preserve_register "routines/$(basename "$f")"
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
    _ledger_record "routines/$dst_base"
    _preserve_register "routines/$dst_base"
    _log "  rendered routines/$dst_base (slug-derived label + instance paths)"
  done
  # DGN-227 MINOR-5: defer-merge policy = merge-append (NOT clobber). A pack
  # that bundles its own plists.defer must NOT overwrite the instance framework
  # defer manifest (which stages the generic-brief units). Instead, the pack's
  # rendered defer ENTRIES are appended to the existing instance defer, both
  # preserved. Substitution is _render_to (same slug/root rewrite as the plist
  # filenames), keeping the output format-consistent with DGN-417's
  # telegram-agent->agent-name substitution (no literal telegram-agent
  # leftover). Duplicate basenames (already in the instance defer) are skipped.
  # deferral manifest basenames must match the rendered plist filenames.
  if [[ -f "$PKG_PAYLOAD/routines/plists.defer" ]]; then
    _pack_defer="$(mktemp)"
    _render_to "$PKG_PAYLOAD/routines/plists.defer" "$_pack_defer"
    _inst_defer="$ROOT/routines/plists.defer"
    if [[ ! -f "$_inst_defer" ]]; then
      # No framework defer present -- the pack defer becomes the manifest.
      cp "$_pack_defer" "$_inst_defer"
      _log "  installed routines/plists.defer (no prior framework defer -- pack defer adopted)"
    else
      _appended=0
      _pack_marker="# --- pack:$PACK_ID defer entries (DGN-227 MINOR-5 merge-append) ---"
      while IFS= read -r _de || [[ -n "$_de" ]]; do
        # skip blank + comment lines from the pack defer body
        case "$_de" in ''|'#'*) continue ;; esac
        # dedup: entry already present (any non-comment line) -> skip
        if grep -qxF "$_de" "$_inst_defer"; then
          _log "  defer merge: entry already present, skipping: $_de"
          continue
        fi
        if [[ "$_appended" -eq 0 ]]; then
          # append the section marker once, before the first new entry
          if ! grep -qxF "$_pack_marker" "$_inst_defer"; then
            printf '%s\n' "$_pack_marker" >> "$_inst_defer"
          fi
          _appended=1
        fi
        printf '%s\n' "$_de" >> "$_inst_defer"
        _log "  defer merge: appended pack entry: $_de"
      done < "$_pack_defer"
      [[ "$_appended" -eq 1 ]] || _log "  defer merge: all pack entries already present (idempotent, no change)"
    fi
    rm -f "$_pack_defer"
    _ledger_record "routines/plists.defer"
    _preserve_register "routines/plists.defer"
    _log "  merged routines/plists.defer (framework entries preserved + pack entries appended)"
  fi
else
  _log "  category plists not declared -- skipping"
fi

# 2d. prompts/
if has_cat prompts && [[ -d "$PKG_PAYLOAD/routines/prompts" ]]; then
  mkdir -p "$ROOT/routines/prompts"
  cp -rf "$PKG_PAYLOAD/routines/prompts/." "$ROOT/routines/prompts/"
  while IFS= read -r _pf; do
    _rel="routines/prompts/${_pf#"$PKG_PAYLOAD/routines/prompts/"}"
    _ledger_record "$_rel"
    _preserve_register "$_rel"
  done < <(find "$PKG_PAYLOAD/routines/prompts" -type f)
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
    _ledger_record "database/migrations/$(basename "$f")"
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
    _ledger_record "scripts/$(basename "$f")"
    _log "  copied scripts/$(basename "$f")"
  done
else
  has_cat scripts || _log "  category scripts not declared -- skipping"
fi

# 2g. config/triggers.yaml
if has_cat triggers && [[ -f "$PKG_PAYLOAD/config/triggers.yaml" ]]; then
  mkdir -p "$ROOT/config"
  cp -f "$PKG_PAYLOAD/config/triggers.yaml" "$ROOT/config/triggers.yaml"
  _ledger_record "config/triggers.yaml"
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

  # Peer-integration keys belong to the MIGRATION path only (DGN-284 #3/#6).
  # DGN-227 E2-1/P24: MIGRATION_PEER joins the migration key family (fresh
  # strips it). HANDOFF_PEER_MAIN is a BRIEFING-topology key, NOT a migration
  # key -- it is deliberately NOT in this strip list (fresh paths may write it).
  PEER_KEYS_RE='^(L1_DB|L1_EXPECTED_USER_VERSION|HANDOFF_PEER_AG|MIGRATION_PEER)='

  # DGN-227 D2/P8: fragments are managed as BEGIN/END marker-pair blocks so
  # --upgrade can replace them (remove-and-reappend). Legacy single-marker
  # blocks cannot be bounded mechanically -> --upgrade loud-FAILs on them.
  CONF_PAIR_BEGIN="# DOGANY-PACK:$PACK_ID:BEGIN"
  CONF_PAIR_END="# DOGANY-PACK:$PACK_ID:END"

  if [[ -f "$CONF_ADD" ]]; then
    if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$CONF_PAIR_BEGIN" "$AGENT_CONF" 2>/dev/null; then
      # marker-pair replacement: excise the old block, then fall through to append
      _tmp_conf="$(mktemp)"
      awk -v b="$CONF_PAIR_BEGIN" -v e="$CONF_PAIR_END" '
        $0 == b { inblk=1; next }
        $0 == e { inblk=0; next }
        !inblk { print }' "$AGENT_CONF" > "$_tmp_conf"
      mv "$_tmp_conf" "$AGENT_CONF"
      _log "  upgrade: excised prior agent.conf fragment block (marker pair)"
    elif [[ "$UPGRADE" -eq 1 ]] && grep -qF "$MARKER" "$AGENT_CONF" 2>/dev/null; then
      _fail "step 3: --upgrade found a LEGACY single-marker fragment block (no BEGIN/END pair) in agent.conf -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
    fi

    if grep -qF "$CONF_PAIR_BEGIN" "$AGENT_CONF" 2>/dev/null \
       || { [[ "$UPGRADE" -eq 0 ]] && grep -qF "$MARKER" "$AGENT_CONF" 2>/dev/null; }; then
      _log "  agent.conf.add already appended (idempotent, skipping)"
    elif [[ "$MIGRATION" -eq 1 ]]; then
      {
        echo ""
        echo "$CONF_PAIR_BEGIN"
        echo "$MARKER"
        # strip comment-only header lines; point peer keys at the actual peer
        grep -v '^#' "$CONF_ADD" | grep -v '^$' \
          | sed -e "s|^L1_DB=.*|L1_DB=$PEER_ROOT/database/lifekit.db|" \
                -e "s|^HANDOFF_PEER_AG=.*|HANDOFF_PEER_AG=$PEER_ROOT|" \
                -e "s|^MIGRATION_PEER=.*|MIGRATION_PEER=$PEER_ROOT|" || true
        # DGN-227 E2-1/P24: the migration discriminator key is ALWAYS written
        # on the migration path, even when the fragment does not carry it.
        if ! grep -Eq '^MIGRATION_PEER=' "$CONF_ADD"; then
          echo "MIGRATION_PEER=$PEER_ROOT"
        fi
        echo "$CONF_PAIR_END"
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (migration path; peer=$PEER_ROOT; MIGRATION_PEER set)"
    else
      {
        echo ""
        echo "$CONF_PAIR_BEGIN"
        echo "$MARKER"
        echo "# fresh/standalone mint (DGN-284/DGN-227): migration-family keys"
        echo "# (L1_DB / L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG / MIGRATION_PEER) intentionally omitted"
        # keep any future non-migration keys the fragment may carry
        grep -v '^#' "$CONF_ADD" | grep -v '^$' | grep -Ev "$PEER_KEYS_RE" || true
        echo "$CONF_PAIR_END"
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (fresh path; migration keys omitted)"
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
# DGN-227 B5: source resolution priority (frozen-snapshot delivery channel):
#   (1) a bundled frozen snapshot at <package_dir>/<reference_slug>/knowledge/
#       (customer-machine path) -- inject it as the snapshot source so a pack
#       ships its warehouse to other machines; the publisher-local path is not
#       required to exist. This is the B5 delivery channel (F1 resolution).
#   (2) absent -> fall back to the manifest knowledge.source publisher-local
#       path (same-machine pilot / dev scenario, DGN-402 behavior preserved).
# The snapshot script call convention (idempotent rsync + pin record +
# instance user-data exclusion) is unchanged -- only the source path differs.
if has_cat knowledge_snapshot; then
  _log "step 6: knowledge snapshot"

  SNAPSHOT_SH="$ROOT/scripts/knowledge-snapshot.sh"
  PKG_KNOWLEDGE="$PKG_PAYLOAD/knowledge"
  SNAP_SOURCE=""
  if [[ -n "$KNOW_WAREHOUSE" && -d "$PKG_KNOWLEDGE/$KNOW_WAREHOUSE" ]]; then
    SNAP_SOURCE="$PKG_KNOWLEDGE/$KNOW_WAREHOUSE"
    _log "  snapshot source (bundled frozen channel, B5): $SNAP_SOURCE"
  elif [[ -n "$KNOW_SOURCE" ]]; then
    SNAP_SOURCE="$KNOW_SOURCE"
    _log "  snapshot source (manifest knowledge.source, publisher-local fallback): $SNAP_SOURCE"
  fi
  if [[ -x "$SNAPSHOT_SH" ]]; then
    bash "$SNAPSHOT_SH" "$ROOT" ${SNAP_SOURCE:+"$SNAP_SOURCE"} 2>&1 | while IFS= read -r line; do _log "  snapshot: $line"; done
    # DGN-227 B3: the warehouse root DIRECTORY is the ledger unit for
    # knowledge (per-file churn is snapshot-internal).
    [[ -n "$KNOW_WAREHOUSE" ]] && _ledger_record "knowledge/$KNOW_WAREHOUSE/"
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

    if [[ -d "$bundle_dir" ]]; then
      # ---- refine mode (DGN-402): instance bundle dir exists -> render the
      #      SKILL.md only (single-file refine of an existing framework skill).
      # DGN-402: plain cp replaced by the render pipeline -- reference-identity
      # substitution (slug/root/home) + mint-token substitution. Unrendered
      # token residue is a hard FAIL at the STEP 7c gate (G4).
      _render_to "$src_skill_md" "$bundle_dir/SKILL.md"
      _subst_mint_tokens "$bundle_dir/SKILL.md"
      _log "  installed .claude/skills-bundle/$skill_id/SKILL.md (rendered, refine mode)"
      _ledger_record ".claude/skills-bundle/$skill_id/SKILL.md"

      # DGN-227 D1/P18 (widens DGN-402 layer 3): EVERY pack file landing in an
      # update.sh allowlist-managed zone (.claude/skills-bundle here) is
      # preserve-registered pack-owned -- not only consumer skills. Pack
      # reinstall still overwrites (preserve binds update.sh only).
      _preserve_register ".claude/skills-bundle/$skill_id/SKILL.md"
    else
      # ---- net-new mode (DGN-227 B6): a brand-new multi-file domain skill the
      #      pack brings in. Install the WHOLE payload skill directory into
      #      .claude/skills-bundle/<id>/. Text files go through the render
      #      pipeline (reference-identity + mint-token subst); binaries are
      #      copied verbatim. Each installed file is ledger-recorded and
      #      preserve-registered pack-owned (D1) so it survives framework
      #      refresh AND is reconciled on upgrade. G4 (STEP 7c) applies the
      #      unrendered-token gate to every net-new file.
      _log "  net-new skill directory: $skill_id (bundle dir absent -> full install)"
      mkdir -p "$bundle_dir"
      while IFS= read -r _sf; do
        _rel="${_sf#"$skill_dir"}"           # path relative to the skill dir root
        _rel="${_rel#/}"
        _dst="$bundle_dir/$_rel"
        mkdir -p "$(dirname "$_dst")"
        if _is_text_file "$_sf"; then
          _render_to "$_sf" "$_dst"
          _subst_mint_tokens "$_dst"
          # preserve the source executable bit through the render (render writes
          # a fresh file via sed, dropping mode)
          [[ -x "$_sf" ]] && chmod +x "$_dst"
          _log "    rendered .claude/skills-bundle/$skill_id/$_rel"
        else
          cp -f "$_sf" "$_dst"
          _log "    copied (binary) .claude/skills-bundle/$skill_id/$_rel"
        fi
        _ledger_record ".claude/skills-bundle/$skill_id/$_rel"
        _preserve_register ".claude/skills-bundle/$skill_id/$_rel"
      done < <(find "$skill_dir" -type f)
      _log "  installed net-new skill .claude/skills-bundle/$skill_id/ (full directory)"
    fi

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

# ---------- DGN-227 B3/D2: ledger finalize + --upgrade stale removal --------
# Rehearsal ordering note (OPEN QUESTION): spec D2 orders the phases
# remove -> apply -> re-record; here the removal diff runs AFTER apply using
# the freshly recorded ledger (old-ledger snapshot taken first). End state is
# identical (a stale path can never equal a new path), but it deviates from
# the spec's literal phase order -- escalated, see the rehearsal report.
OLD_LEDGER_SNAP=""
if [[ "$UPGRADE" -eq 1 ]]; then
  if [[ -f "$LEDGER_FILE" ]]; then
    OLD_LEDGER_SNAP="$(mktemp)"
    cp "$LEDGER_FILE" "$OLD_LEDGER_SNAP"
  else
    # legacy ledger-less install (pre-DGN-227): removal diff has no source.
    _log "  WARN: --upgrade with NO prior ledger (legacy install) -- stale-removal phase SKIPPED (loud, not silent); the ledger recorded now arms removal semantics for the NEXT upgrade"
  fi
fi

_ledger_finalize

if [[ "$UPGRADE" -eq 1 && -n "$OLD_LEDGER_SNAP" ]]; then
  _log "  upgrade: stale diff (old ledger vs new payload set)"
  UPG_BACKUP_DIR="$ROOT/files/_archive/pack-upgrade-$PACK_ID-$(date +%Y%m%d-%H%M%S)"
  while IFS= read -r stale; do
    [[ -n "$stale" ]] || continue
    if _ledger_paths "$LEDGER_FILE" | grep -qxF "$stale"; then
      continue   # still in the new target set -- not stale
    fi
    if [[ ! -e "$ROOT/$stale" ]]; then
      _log "  upgrade: stale ledger entry has no file on disk: $stale (ledger drift -- skipping)"
      continue
    fi
    # (a) plists get a launchd bootout BEFORE file removal (D2 phase 1a).
    if [[ "$stale" == *.plist ]]; then
      _stale_label="$(basename "$stale" .plist)"
      if [[ -n "${DOGANY_LAUNCHD_CAPTURE:-}" ]]; then
        printf 'launchctl bootout gui/UID/%s\n' "$_stale_label" >> "$DOGANY_LAUNCHD_CAPTURE"
        _log "  upgrade: (rehearsal) bootout captured for $_stale_label"
      else
        launchctl bootout "gui/$(id -u)/$_stale_label" >/dev/null 2>&1 || true
        rm -f "$HOME/Library/LaunchAgents/$(basename "$stale")" 2>/dev/null || true
        _log "  upgrade: booted out + unstaged launchd unit $_stale_label"
      fi
    fi
    # (b) NM3 backup, then remove.
    mkdir -p "$UPG_BACKUP_DIR/$(dirname "$stale")"
    cp -p "$ROOT/$stale" "$UPG_BACKUP_DIR/$stale"
    rm -f "$ROOT/$stale"
    _log "  upgrade: removed stale pack file: $stale (backup: files/_archive/$(basename "$UPG_BACKUP_DIR")/)"
  done < <(_ledger_paths "$OLD_LEDGER_SNAP")
  rm -f "$OLD_LEDGER_SNAP"
fi

# DGN-227 B3/P7: instance consumption record -- DOGANY_PACKS list-form upsert
# in .instance.conf (id@version entries; other packs' entries preserved).
python3 - "$ROOT/.instance.conf" "$PACK_ID" "$PACK_VERSION" <<'PYEOF'
import sys, pathlib
conf = pathlib.Path(sys.argv[1])
pid, ver = sys.argv[2], sys.argv[3]
lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
entry = f"{pid}@{ver}"
found = False
for i, ln in enumerate(lines):
    if ln.startswith("DOGANY_PACKS="):
        items = [x for x in ln.split("=", 1)[1].split(",") if x]
        items = [x for x in items if x.split("@", 1)[0] != pid]
        items.append(entry)
        lines[i] = "DOGANY_PACKS=" + ",".join(items)
        found = True
        break
if not found:
    lines.append("DOGANY_PACKS=" + entry)
conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"[packs-record] DOGANY_PACKS upserted: {entry}")
PYEOF
_log "  .instance.conf DOGANY_PACKS upserted ($PACK_ID@$PACK_VERSION)"

# DGN-227 D1/P18 (replaces DGN-402 grill r2 MAJOR-2 predicate): reconcile
# pack-tagged preserve entries against the INSTALL LEDGER on every
# install/reinstall/upgrade; hand-written (untagged) entries never touched.
_preserve_reconcile

# ---------- STEP 7b: AGENT.md fragment append (rendered, idempotent) --------
if has_cat agent_md_fragment; then
  _log "step 7b: AGENT.md fragment append"

  AGENT_MD="$ROOT/AGENT.md"
  AGENT_ADD="$PKG_PAYLOAD/AGENT.md.add"

  # DGN-227 D2/P8: BEGIN/END marker-pair block (HTML comment pair for md).
  MD_PAIR_BEGIN="<!-- DOGANY-PACK:$PACK_ID:BEGIN -->"
  MD_PAIR_END="<!-- DOGANY-PACK:$PACK_ID:END -->"

  if [[ -f "$AGENT_ADD" ]]; then
    if [[ ! -f "$AGENT_MD" ]]; then
      _fail "step 7b: AGENT.md not found at $AGENT_MD (mint incomplete?)"
    fi
    if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$MD_PAIR_BEGIN" "$AGENT_MD" 2>/dev/null; then
      _tmp_md="$(mktemp)"
      awk -v b="$MD_PAIR_BEGIN" -v e="$MD_PAIR_END" '
        $0 == b { inblk=1; next }
        $0 == e { inblk=0; next }
        !inblk { print }' "$AGENT_MD" > "$_tmp_md"
      mv "$_tmp_md" "$AGENT_MD"
      _log "  upgrade: excised prior AGENT.md fragment block (marker pair)"
    elif [[ "$UPGRADE" -eq 1 ]] && grep -qF "$AGENT_MARKER" "$AGENT_MD" 2>/dev/null; then
      _fail "step 7b: --upgrade found a LEGACY single-marker AGENT.md fragment (no BEGIN/END pair) -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
    fi
    if grep -qF "$MD_PAIR_BEGIN" "$AGENT_MD" 2>/dev/null \
       || { [[ "$UPGRADE" -eq 0 ]] && grep -qF "$AGENT_MARKER" "$AGENT_MD" 2>/dev/null; }; then
      _log "  AGENT.md fragment already appended (idempotent, skipping)"
    else
      # Render the fragment through the same slug/root substitution as the
      # plists so slug-derived prose lands correctly (DGN-366 L2 step 7b).
      RENDERED_ADD="$(mktemp)"
      _render_to "$AGENT_ADD" "$RENDERED_ADD"
      {
        echo ""
        echo "$MD_PAIR_BEGIN"
        cat "$RENDERED_ADD"
        echo "$MD_PAIR_END"
      } >> "$AGENT_MD"
      rm -f "$RENDERED_ADD"
      _log "  appended AGENT.md.add (rendered, marker pair) to AGENT.md (marker: $AGENT_MARKER)"
    fi
  else
    _log "  no AGENT.md.add in package (skipping)"
  fi

  _log "step 7b: AGENT.md fragment done"
else
  _log "step 7b: AGENT.md fragment SKIPPED (category agent_md_fragment not declared)"
fi

# ---------- STEP 7c: knowledge wiring selftest (DGN-402, zero-model) ---------
# Warehouse packs: gates G1-G4 (delivery / discovery / refraction predicates).
# Warehouse-less packs: inverse check (zero warehouse artifacts). Same script
# is re-run by the agent-crafting phase 2 checklist (single logic home).
# G5 (live probes) stays manual -- the script only prints a reminder.
SELFTEST_SH="$SCRIPT_DIR/knowledge_selftest.sh"
_log "step 7c: knowledge wiring selftest"
[[ -x "$SELFTEST_SH" ]] || _fail "step 7c: knowledge_selftest.sh not found/executable: $SELFTEST_SH"
SELFTEST_OUT="$(mktemp)"
set +e
"$SELFTEST_SH" "$ROOT" --manifest "$MANIFEST" > "$SELFTEST_OUT" 2>&1
SELFTEST_RC=$?
set -e
while IFS= read -r line; do _log "  selftest: $line"; done < "$SELFTEST_OUT"
rm -f "$SELFTEST_OUT"
if [[ "$SELFTEST_RC" -ne 0 ]]; then
  _fail "step 7c: knowledge wiring selftest FAILED (exit $SELFTEST_RC) -- the wiring gates must pass at install time"
fi
_log "step 7c: knowledge wiring selftest done"

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
