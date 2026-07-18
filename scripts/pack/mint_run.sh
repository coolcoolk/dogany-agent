#!/bin/bash
# mint_run.sh -- pack machinery wrapper around the framework installer
# (scripts/mint.sh). DGN-243, generalized per DGN-366 L1 (repo-resident
# machinery; instance context via explicit --instance-root).
#
# It does NOT reimplement minting: it resolves instance defaults (owner id,
# tz, lang, user label) from the CALLING instance passed via
# --instance-root, validates inputs, and calls the framework mint.sh.
# Token comes from the DOGANY_BOT_TOKEN env var ONLY (never argv, never
# echoed).
#
# Instance context contract (DGN-366 L1): the calling instance's root is
# passed EXPLICITLY as --instance-root <path>. There is no script-location
# derivation of the instance root. When --instance-root is absent, every
# instance-dependent step (owner-id/tz/lang/user-label/models reads, mint
# journal, minting_state record) SKIPS WITH AN EXPLICIT LOG LINE and the
# flags/defaults take over -- never a silent skip.
#
# Usage:
#   DOGANY_BOT_TOKEN=<token> mint_run.sh mint --slug <slug> [--root <dir>]
#            [--instance-root <dir>] [--owner-id <ids>] [--tz <tz>]
#            [--lang <en|ko>] [--core-only] [--model <sonnet|opus|haiku>]
#            [--dry-run]
#   mint_run.sh start --root <dir> [--deferred] [--dry-run]
#            # load launchd plists (LIVE op -- only after explicit user
#            # approval)
#   DOGANY_BOT_TOKEN=<token> mint_run.sh pipeline --slug <slug> --pack <id>
#            [--root <dir>] [--instance-root <dir>] [--owner-id <ids>]
#            [--tz <tz>] [--lang <en|ko>] [--role <prose>] [--core-only]
#            [--model <sonnet|opus|haiku>] [--catalog <file>]
#            [--migrate-from <peer-root>] [--no-start] [--no-state]
#            [--dry-run]
#            # --migrate-from: MIGRATION PATH (DGN-284): the new instance
#            #          migrates an existing user's records from the main
#            #          agent at <peer-root>. Sets peer keys
#            #          (HANDOFF_PEER_AG etc.) + domain seed pending_data
#            #          in pack_install. OMITTED (default) = fresh/
#            #          standalone mint: no peer keys, seed ready.
#            # --no-start: skip launchd bootstrap (pack_install step 10)
#            # --no-state: skip minting_state record (pack_install step 11)
#            # --model: model for the new instance (default: sonnet)
#            # --catalog: pack catalog override (passed to pack_install)
#   mint_run.sh recover <slug> --instance-root <dir> [--root <dir>]
#            # crash-recovery: if a prior mint session crashed after the
#            # framework mint.sh call but before post-mint stamps completed,
#            # replay stamps (role, model) from the saved mint journal.
#            # The journal lives in the CALLING instance
#            # (<instance-root>/.mint-journal), so --instance-root is
#            # required here. Safe to re-run: idempotent guards prevent
#            # double-stamping.
#
# --dry-run: preflight checks + resolved plan (token masked) + a real
# mint.sh --print-env smoke (no writes anywhere). Exit 0 = plan OK.
# In start mode --dry-run prints the load/defer plan without copying or
# bootstrapping anything.
#
# Deferral manifest (DGN-238 grill-final MAJOR-1): an instance package may
# ship <root>/routines/plists.defer -- one plist basename per line ('#'
# comments allowed). Plain `start` SKIPS those units (staged, not loaded)
# so a package can stage e.g. daily routines behind a later gate. Load the
# deferred set later with `mint_run.sh start --root <dir> --deferred`
# (loads ONLY the deferred units). No manifest file = load everything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo shipping this machinery (default framework root for mint.sh).
MACHINERY_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

MODE="${1:-}"
[[ "$MODE" == "mint" || "$MODE" == "start" || "$MODE" == "pipeline" || "$MODE" == "recover" || "$MODE" == "stamp-role" ]] || {
  echo "usage: mint_run.sh {mint|start|pipeline|recover|stamp-role} ..." >&2; exit 1; }
shift

SLUG="" ROOT="" OWNER_IDS="" AGENT_TZ="" LANG_OPT="" DRY=0 CORE_ONLY=0
ONLY_DEFERRED=0 ROLE_PROSE="" PACK_ID="" NO_START=0 NO_STATE=0 MODEL_OPT=""
PEER_ROOT="" INSTANCE_ROOT="" CATALOG_OPT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)      SLUG="$2"; shift 2 ;;
    --root)      ROOT="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --owner-id)  OWNER_IDS="$2"; shift 2 ;;
    --tz)        AGENT_TZ="$2"; shift 2 ;;
    --lang)      LANG_OPT="$2"; shift 2 ;;
    --core-only) CORE_ONLY=1; shift ;;
    --dry-run|--dry) DRY=1; shift ;;
    --deferred)  ONLY_DEFERRED=1; shift ;;
    --role)      ROLE_PROSE="$2"; shift 2 ;;
    --pack)      PACK_ID="$2"; shift 2 ;;
    --catalog)   CATALOG_OPT="$2"; shift 2 ;;
    --migrate-from) PEER_ROOT="$2"; shift 2 ;;
    --no-start)  NO_START=1; shift ;;
    --no-state)  NO_STATE=1; shift ;;
    --model)     MODEL_OPT="$2"; shift 2 ;;
    *)
      # recover mode takes the slug as a bare positional
      if [[ "$MODE" == "recover" && -z "$SLUG" && "${1:0:2}" != "--" ]]; then
        SLUG="$1"; shift
      else
        echo "unknown option: $1" >&2; exit 1
      fi
      ;;
  esac
done

# Validate --model if supplied (allowed: sonnet, opus, haiku; default: sonnet).
ALLOWED_MODELS="sonnet opus haiku"
if [[ -n "$MODEL_OPT" ]]; then
  valid=0
  for m in $ALLOWED_MODELS; do [[ "$MODEL_OPT" == "$m" ]] && valid=1 && break; done
  [[ "$valid" -eq 1 ]] || {
    echo "ERROR: --model '$MODEL_OPT' not recognized; allowed values: $ALLOWED_MODELS" >&2
    exit 1
  }
fi
# Apply default after validation so error messages are accurate.
MODEL_OPT="${MODEL_OPT:-sonnet}"

# Validate --instance-root if supplied.
if [[ -n "$INSTANCE_ROOT" ]]; then
  [[ -d "$INSTANCE_ROOT" ]] || {
    echo "ERROR: --instance-root not a directory: $INSTANCE_ROOT" >&2
    exit 1
  }
fi

# Journal directory: instance-resident state (DGN-366 L1). Empty when no
# --instance-root -- journal write/cleanup then SKIP with an explicit log.
JOURNAL_DIR=""
[[ -n "$INSTANCE_ROOT" ]] && JOURNAL_DIR="$INSTANCE_ROOT/.mint-journal"

conf_get() { # conf_get <file> <key> -- empty (not fatal) when key/file absent
  { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; } || true
}

# ---- role stamp: SINGLE canonical implementation (DGN-227 A3/P15) ----------
# _stamp_role <root> <prose> [log-prefix]
# Stamps the Role Primary-focus prose into <root>/AGENT.md, excises the
# onboarding Q6 block and inserts the do-not-ask line. Idempotent (guard
# inside the python block). Three entry points call this ONE function:
#   (1) mint mode post-mint step, (2) recover mode journal replay,
#   (3) the stamp-role subcommand.
# Returns non-zero on stamp failure; missing AGENT.md is a WARN + return 0
# (preserves the legacy per-caller skip semantics).
_stamp_role() {
  local root="$1" prose="$2" prefix="${3:-role-stamp}"
  local agent_md="$root/AGENT.md"
  if [[ ! -f "$agent_md" ]]; then
    echo "[$prefix] WARN AGENT.md not found at $agent_md; skipping role stamp" >&2
    return 0
  fi
  python3 - "$agent_md" "$prose" "$prefix" <<'PYEOF'
import sys, re, pathlib

agent_md = pathlib.Path(sys.argv[1])
role_prose = sys.argv[2]
prefix = sys.argv[3]
text = agent_md.read_text(encoding="utf-8")

# Idempotence guard: if Primary focus is already stamped, do not re-stamp.
placeholder = "(set at onboarding -- one prose line naming the main hat;"
if placeholder not in text:
    print(f"[{prefix}] Primary focus already stamped or placeholder absent; skipping (idempotent).", file=sys.stderr)
    sys.exit(0)

# 1. Fill Primary-focus placeholder line.
pf_pattern = re.compile(
    r'\(set at onboarding -- one prose line naming the main hat;\s*'
    r'the general front-door bullet above still applies\)',
    re.DOTALL,
)
new_text, count = pf_pattern.subn(role_prose, text)
if count != 1:
    print(f"[{prefix}] ERROR: expected 1 Primary-focus placeholder match, got {count}; aborting", file=sys.stderr)
    sys.exit(1)

# 2. Excise Q6 bullet (the "6. role -- LAST: ..." block).
q6_pattern = re.compile(
    r'\n  6\. role\s+--\s+LAST:.*?(?=\nKeep each question)',
    re.DOTALL,
)
new_text, q6_count = q6_pattern.subn("", new_text)
if q6_count != 1:
    print(f"[{prefix}] ERROR: expected 1 Q6 block match, got {q6_count}; aborting", file=sys.stderr)
    sys.exit(1)

# 3. Update completion criterion.
new_text = new_text.replace(
    "when all are filled (question 6 = the Primary-focus slot filled),",
    "when all 5 are filled,",
)

# 4. Insert do-not-ask instruction before "Keep each question".
do_not_ask = (
    "Do NOT ask about your role -- it is already set"
    " (filled at mint; see Role section)."
)
keep_marker = "\nKeep each question"
if do_not_ask not in new_text:
    new_text = new_text.replace(
        keep_marker,
        "\n" + do_not_ask + keep_marker,
    )

agent_md.write_text(new_text, encoding="utf-8")
print(f"[{prefix}] OK: Primary focus stamped, Q6 excised, do-not-ask added.")
PYEOF
}

# ---- stamp-role mode: standalone entry point (DGN-227 A3/P15 entry 3) ------
# install.sh calls this on every install path right after mint:
#   mint_run.sh stamp-role --root <root> --role "<prose>"
if [[ "$MODE" == "stamp-role" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: stamp-role requires --root <dir>" >&2; exit 1; }
  [[ -n "$ROLE_PROSE" ]] || { echo "ERROR: stamp-role requires --role <prose>" >&2; exit 1; }
  [[ -d "$ROOT" ]] || { echo "ERROR: stamp-role root not a directory: $ROOT" >&2; exit 1; }
  # MINOR-8: the stamp-role SUBCOMMAND is a hard precondition of install (A3:
  # Q6 excision + role stamp). A missing AGENT.md here means a partial mint --
  # a WARN+skip (as _stamp_role does for the mint/recover callers) would let
  # install proceed with Q6 un-excised and the role un-stamped, silently
  # breaking A3. Fail loud on this entry; mint/recover keep the WARN semantics.
  [[ -f "$ROOT/AGENT.md" ]] || {
    echo "ERROR: stamp-role AGENT.md not found at $ROOT/AGENT.md (partial mint) -- refusing to proceed (A3)" >&2
    exit 1
  }
  _stamp_role "$ROOT" "$ROLE_PROSE" "stamp-role"
  exit $?
fi

# ---- recover mode: replay post-mint stamps from journal (crash recovery) ----
if [[ "$MODE" == "recover" ]]; then
  [[ -n "$SLUG" ]] || {
    echo "usage: mint_run.sh recover --slug <slug> --instance-root <dir> [--root <dir>]" >&2
    echo "  or:  mint_run.sh recover <slug> --instance-root <dir> [--root <dir>]" >&2
    exit 1
  }

  # The journal is instance-resident state: recover cannot run without the
  # calling instance's root (explicit contract, DGN-366 L1).
  [[ -n "$JOURNAL_DIR" ]] || {
    echo "[recover] SKIP: --instance-root not supplied -- the mint journal is instance-resident (<instance-root>/.mint-journal); nothing to recover without it" >&2
    exit 1
  }

  JOURNAL_FILE="$JOURNAL_DIR/$SLUG.json"
  [[ -f "$JOURNAL_FILE" ]] || {
    echo "[recover] no journal found for slug '$SLUG' at $JOURNAL_FILE" >&2
    echo "[recover] nothing to recover" >&2
    exit 0
  }

  # Read journal values.
  J_ROOT="$(python3 -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('root',''))")"
  J_ROLE="$(python3 -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('role_prose',''))")"
  J_MODEL="$(python3 -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('model','sonnet'))")"

  # --root flag overrides journal root.
  EFFECTIVE_ROOT="${ROOT:-$J_ROOT}"
  [[ -n "$EFFECTIVE_ROOT" ]] || {
    echo "[recover] cannot determine root: not in journal and --root not supplied" >&2
    exit 1
  }

  [[ -d "$EFFECTIVE_ROOT" ]] || {
    echo "[recover] target dir does not exist: $EFFECTIVE_ROOT" >&2
    echo "[recover] framework mint may not have completed; nothing to recover" >&2
    exit 1
  }

  echo "[recover] replaying post-mint stamps for slug '$SLUG' from $JOURNAL_FILE"

  # Model stamp (idempotent: always overwrite with journaled value).
  INSTANCE_SETTINGS="$EFFECTIVE_ROOT/.claude/settings.json"
  if [[ -f "$INSTANCE_SETTINGS" ]]; then
    python3 - "$INSTANCE_SETTINGS" "$J_MODEL" <<'PYEOF'
import json, sys
settings_path, model = sys.argv[1], sys.argv[2]
with open(settings_path, encoding="utf-8") as f:
    data = json.load(f)
data["model"] = model
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[recover] model set to {model!r} in settings.json")
PYEOF
  else
    echo "[recover] WARN settings.json not found at $INSTANCE_SETTINGS; skipping model stamp" >&2
  fi

  # Role stamp -- single canonical implementation (DGN-227 A3/P15).
  if [[ -n "$J_ROLE" ]]; then
    _stamp_role "$EFFECTIVE_ROOT" "$J_ROLE" "recover/role-stamp" || {
      STATUS=$?
      echo "[recover] ERROR: role-stamp step failed (exit $STATUS)" >&2; exit $STATUS
    }
  else
    echo "[recover] no role prose in journal; skipping role stamp"
  fi

  # Cleanup journal on success.
  rm -f "$JOURNAL_FILE"
  echo "[recover] journal removed: $JOURNAL_FILE"
  echo "[recover] DONE -- stamps replayed for $EFFECTIVE_ROOT"
  exit 0
fi

# ---- start mode: load the NEW agent's launchd units (live op, user-gated) --
if [[ "$MODE" == "start" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: start requires --root <dir>" >&2; exit 1; }
  [[ -d "$ROOT/bridge" ]] || { echo "ERROR: not a minted instance: $ROOT" >&2; exit 1; }

  # Deferral manifest: plist basenames staged behind a later gate.
  DEFER_FILE="$ROOT/routines/plists.defer"
  DEFER_LIST=()
  if [[ -f "$DEFER_FILE" ]]; then
    while IFS= read -r line; do
      line="${line%%#*}"
      line="${line//[[:space:]]/}"
      [[ -n "$line" ]] && DEFER_LIST+=("$line")
    done < "$DEFER_FILE"
  fi
  is_deferred() {
    local b="$1" d
    for d in ${DEFER_LIST[@]+"${DEFER_LIST[@]}"}; do
      [[ "$b" == "$d" ]] && return 0
    done
    return 1
  }
  if [[ "$ONLY_DEFERRED" -eq 1 && "${#DEFER_LIST[@]}" -eq 0 ]]; then
    echo "ERROR: --deferred but no defer manifest at $DEFER_FILE" >&2
    exit 1
  fi

  LA_DIR="$HOME/Library/LaunchAgents"
  [[ "$DRY" -eq 1 ]] || mkdir -p "$LA_DIR"
  loaded=0 deferred=0
  for p in "$ROOT"/bridge/*.plist "$ROOT"/routines/*.plist; do
    [[ -e "$p" ]] || continue
    base="$(basename "$p")"
    label="$(basename "$p" .plist)"
    if [[ "$ONLY_DEFERRED" -eq 1 ]]; then
      is_deferred "$base" || continue
    elif is_deferred "$base"; then
      echo "[start] deferred $label (routines/plists.defer; load later with --deferred)"
      deferred=$((deferred + 1))
      continue
    fi
    if [[ "$DRY" -eq 1 ]]; then
      echo "[start] (dry-run) would load $label"
      loaded=$((loaded + 1))
      continue
    fi
    cp -f "$p" "$LA_DIR/"
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$LA_DIR/$base"
    echo "[start] loaded $label"
    loaded=$((loaded + 1))
  done
  [[ "$loaded" -gt 0 ]] || { echo "ERROR: no plists to load under $ROOT" >&2; exit 1; }
  if [[ "$DRY" -eq 1 ]]; then
    echo "[start] DRY-RUN DONE ($loaded units would load, $deferred deferred)"
  else
    echo "[start] DONE ($loaded units loaded, $deferred deferred)"
  fi
  exit 0
fi

# ---- pipeline mode: mint -> pack_install -> done (one entry, no user asks) --
if [[ "$MODE" == "pipeline" ]]; then
  [[ -n "$SLUG" ]] || { echo "ERROR: pipeline requires --slug <slug>" >&2; exit 1; }
  [[ -n "$PACK_ID" ]] || { echo "ERROR: pipeline requires --pack <pack-id>" >&2; exit 1; }

  # Derive ROOT if not supplied (same rule as mint mode)
  if [[ -z "$ROOT" ]]; then
    CAP="$(printf '%s' "$SLUG" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
    ROOT="$HOME/dogany/$CAP"
  fi

  PACK_INSTALL_SH="$SCRIPT_DIR/pack_install.sh"
  [[ -x "$PACK_INSTALL_SH" ]] || { echo "ERROR: pack_install.sh not found: $PACK_INSTALL_SH" >&2; exit 1; }

  # 1. dry-run the mint (does NOT require the instance root to exist)
  echo "[pipeline] step 1/3: mint dry-run..."
  MINT_ARGS=(--slug "$SLUG" --root "$ROOT")
  [[ -n "$INSTANCE_ROOT" ]] && MINT_ARGS+=(--instance-root "$INSTANCE_ROOT")
  [[ -n "$OWNER_IDS" ]] && MINT_ARGS+=(--owner-id "$OWNER_IDS")
  [[ -n "$AGENT_TZ" ]]  && MINT_ARGS+=(--tz "$AGENT_TZ")
  [[ -n "$LANG_OPT" ]]  && MINT_ARGS+=(--lang "$LANG_OPT")
  [[ -n "$ROLE_PROSE" ]] && MINT_ARGS+=(--role "$ROLE_PROSE")
  [[ "$CORE_ONLY" -eq 1 ]] && MINT_ARGS+=(--core-only)
  MINT_ARGS+=(--model "$MODEL_OPT")

  "$0" mint "${MINT_ARGS[@]}" --dry-run
  echo "[pipeline] mint dry-run OK"

  # NOTE: standalone pack_install dry-run removed (it required the minted root
  # to exist and aborted on fresh mint -- chicken-and-egg). The real pack_install
  # run (step 3) already performs its own preflight after the mint.

  if [[ "$DRY" -eq 1 ]]; then
    echo "[pipeline] DRY-RUN DONE -- mint preflight passed, nothing written"
    exit 0
  fi

  # 2. real mint
  echo "[pipeline] step 2/3: minting instance..."
  "$0" mint "${MINT_ARGS[@]}"
  echo "[pipeline] mint done -> $ROOT"

  # 3. real pack_install (step 1 inside it is preflight; includes start unless --no-start)
  if [[ -n "$PEER_ROOT" ]]; then
    echo "[pipeline] step 3/3: installing pack (migration path, peer=$PEER_ROOT)..."
  else
    echo "[pipeline] step 3/3: installing pack (fresh/standalone path)..."
  fi
  PACK_RUN_ARGS=("$SLUG" "$ROOT" --pack "$PACK_ID" --model "$MODEL_OPT")
  [[ -n "$INSTANCE_ROOT" ]] && PACK_RUN_ARGS+=(--instance-root "$INSTANCE_ROOT")
  [[ -n "$CATALOG_OPT" ]] && PACK_RUN_ARGS+=(--catalog "$CATALOG_OPT")
  [[ -n "$PEER_ROOT" ]] && PACK_RUN_ARGS+=(--migrate-from "$PEER_ROOT")
  [[ "$NO_START" -eq 1 ]] && PACK_RUN_ARGS+=(--no-start)
  [[ "$NO_STATE" -eq 1 ]] && PACK_RUN_ARGS+=(--no-state)
  "$PACK_INSTALL_SH" "${PACK_RUN_ARGS[@]}"
  echo "[pipeline] DONE -- instance ready at $ROOT"
  exit 0
fi

# ---- mint mode -------------------------------------------------------------
[[ -n "$SLUG" ]] || { echo "ERROR: --slug <slug> required" >&2; exit 1; }
[[ "$SLUG" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || {
  echo "ERROR: slug must be ascii kebab-case (^[a-z][a-z0-9-]{1,30}$): $SLUG" >&2
  exit 1; }

# Defaults resolved from the CALLING instance (--instance-root). When absent,
# each instance-dependent read SKIPS with an explicit log line (DGN-366 L1)
# and flags/defaults take over.
REPO_ROOT="" USER_LABEL=""
if [[ -n "$INSTANCE_ROOT" ]]; then
  INSTANCE_CONF="$INSTANCE_ROOT/.instance.conf"
  REPO_ROOT="$(conf_get "$INSTANCE_CONF" DOGANY_REPO_ROOT)"
  USER_LABEL="$(conf_get "$INSTANCE_CONF" DOGANY_USER_LABEL)"
else
  echo "[mint_run] SKIP instance conf read (--instance-root not supplied): repo root = this machinery's repo, user label = default" >&2
fi
REPO_ROOT="${REPO_ROOT:-$MACHINERY_REPO}"
USER_LABEL="${USER_LABEL:-you}"
MINT_SH="$REPO_ROOT/scripts/mint.sh"

if [[ -z "$LANG_OPT" ]]; then
  if [[ -n "$INSTANCE_ROOT" ]]; then
    LANG_OPT="$(conf_get "$INSTANCE_ROOT/config/agent.conf" AGENT_LANG)"
  else
    echo "[mint_run] SKIP lang read from instance agent.conf (--instance-root not supplied): default en unless --lang given" >&2
  fi
fi
LANG_OPT="${LANG_OPT:-en}"

ENV_FILE=""
[[ -n "$INSTANCE_ROOT" ]] && ENV_FILE="$INSTANCE_ROOT/.telegram_bot/.env"
if [[ -z "$OWNER_IDS" ]]; then
  if [[ -n "$ENV_FILE" ]]; then
    OWNER_IDS="$(conf_get "$ENV_FILE" ALLOWED_USER_IDS | cut -d, -f1)"
  else
    echo "[mint_run] SKIP owner-id read from instance .env (--instance-root not supplied): pass --owner-id" >&2
  fi
fi
if [[ -z "$AGENT_TZ" ]]; then
  if [[ -n "$ENV_FILE" ]]; then
    AGENT_TZ="$(conf_get "$ENV_FILE" TZ)"
  else
    echo "[mint_run] SKIP tz read from instance .env (--instance-root not supplied): default Asia/Seoul unless --tz given" >&2
  fi
  AGENT_TZ="${AGENT_TZ:-Asia/Seoul}"
fi

# Read BRIDGE_MODELS from the calling instance's .env so freshly minted
# instances inherit the same model picker list (DGN-281). Fall back to a safe
# default when unreadable; log so the operator knows the default fired.
HOST_BRIDGE_MODELS=""
if [[ -n "$ENV_FILE" ]]; then
  HOST_BRIDGE_MODELS="$(conf_get "$ENV_FILE" BRIDGE_MODELS)"
  if [[ -z "$HOST_BRIDGE_MODELS" ]]; then
    echo "[mint_run] WARN BRIDGE_MODELS not found in $ENV_FILE; defaulting to sonnet,opus,haiku" >&2
  fi
else
  echo "[mint_run] SKIP models read from instance .env (--instance-root not supplied): defaulting to sonnet,opus,haiku" >&2
fi
HOST_BRIDGE_MODELS="${HOST_BRIDGE_MODELS:-sonnet,opus,haiku}"

if [[ -z "$ROOT" ]]; then
  CAP="$(printf '%s' "$SLUG" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
  ROOT="$HOME/dogany/$CAP"
fi

# Preflight.
fail=0
[[ -x "$MINT_SH" ]] || { echo "[preflight] FAIL mint.sh not found/executable: $MINT_SH" >&2; fail=1; }
[[ -d "$REPO_ROOT/agents/.template" ]] || { echo "[preflight] FAIL template missing: $REPO_ROOT/agents/.template" >&2; fail=1; }
if [[ -e "$ROOT" && -n "$(ls -A "$ROOT" 2>/dev/null)" ]]; then
  echo "[preflight] FAIL target dir not empty: $ROOT" >&2; fail=1
fi
command -v sqlite3 >/dev/null 2>&1 || echo "[preflight] WARN sqlite3 missing (lifekit.db init will be skipped)" >&2
[[ -n "$OWNER_IDS" ]] || { echo "[preflight] FAIL owner id unresolved (pass --owner-id)" >&2; fail=1; }
if [[ -z "${DOGANY_BOT_TOKEN:-}" ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    echo "[preflight] WARN DOGANY_BOT_TOKEN not set (ok for dry-run)" >&2
  else
    echo "[preflight] FAIL DOGANY_BOT_TOKEN env var required" >&2; fail=1
  fi
fi
[[ "$fail" -eq 0 ]] || exit 1

ARGS=(--root "$ROOT" --name "$SLUG" --label "$SLUG" --user "$USER_LABEL"
      --lang "$LANG_OPT" --owner-id "$OWNER_IDS" --tz "$AGENT_TZ"
      --models "$HOST_BRIDGE_MODELS")
[[ "$CORE_ONLY" -eq 1 ]] && ARGS+=(--core-only)

if [[ "$DRY" -eq 1 ]]; then
  echo "[dry-run] plan: DOGANY_BOT_TOKEN=*** $MINT_SH ${ARGS[*]}"
  [[ -n "$ROLE_PROSE" ]] && echo "[dry-run] --role will stamp AGENT.md: $ROLE_PROSE"
  echo "[dry-run] .env render smoke (token masked):"
  "$MINT_SH" --print-env --lang "$LANG_OPT" --owner-id "$OWNER_IDS" --tz "$AGENT_TZ" \
    --models "$HOST_BRIDGE_MODELS" \
    | sed -E 's/^(TELEGRAM_BOT_TOKEN|EMAIL_APP_PASSWORD)=..*/\1=***masked***/'
  echo "[dry-run] OK -- nothing written"
  exit 0
fi

# Pre-record mint journal BEFORE any side effect (crash-safety). Persists all
# post-mint stamp inputs so a crashed session can recover them. The journal
# is instance-resident state: without --instance-root it SKIPS (explicit).
JOURNAL_FILE=""
if [[ -n "$JOURNAL_DIR" ]]; then
  mkdir -p "$JOURNAL_DIR"
  JOURNAL_FILE="$JOURNAL_DIR/$SLUG.json"
  JOURNAL_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 - "$JOURNAL_FILE" "$SLUG" "$ROOT" "$ROLE_PROSE" "$MODEL_OPT" "$JOURNAL_TS" <<'PYEOF'
import json, sys
journal_path, slug, root, role_prose, model, ts = sys.argv[1:7]
data = {
    "slug": slug,
    "root": root,
    "role_prose": role_prose,
    "model": model,
    "timestamp": ts,
}
with open(journal_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[mint_run] journal written: {journal_path}")
PYEOF
else
  echo "[mint_run] SKIP mint journal (--instance-root not supplied; journal is instance-resident) -- crash recovery unavailable for this run" >&2
fi

# Real mint. Token stays in env; mint.sh never prints it.
"$MINT_SH" "${ARGS[@]}"

# Domain-role stamping (DGN-277 finding 3): if --role was supplied, fill the
# Primary-focus placeholder and excise Q6 from the onboarding block.
# Runs FIRST among post-mint stamps (before model stamp) so that the most
# critical identity data is written immediately after the framework call.
# NOTE: the fuller Role-section rewrite (generic front-door bullet -> domain line)
# is intentionally NOT done here -- keep it a post-mint CRAFT specialization step.
if [[ -n "$ROLE_PROSE" ]]; then
  _stamp_role "$ROOT" "$ROLE_PROSE" "role-stamp" || {
    STATUS=$?
    echo "[mint_run] ERROR: role-stamp step failed (exit $STATUS)" >&2; exit $STATUS
  }
fi

# Model stamp: write the requested model into the new instance's settings.json.
# Uses python3 JSON round-trip (atomic: load -> mutate -> write).
INSTANCE_SETTINGS="$ROOT/.claude/settings.json"
if [[ -f "$INSTANCE_SETTINGS" ]]; then
  python3 - "$INSTANCE_SETTINGS" "$MODEL_OPT" <<'PYEOF'
import json, sys
settings_path, model = sys.argv[1], sys.argv[2]
with open(settings_path, encoding="utf-8") as f:
    data = json.load(f)
data["model"] = model
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[mint_run] model set to {model!r} in settings.json")
PYEOF
else
  echo "[mint_run] WARN settings.json not found at $INSTANCE_SETTINGS; skipping model stamp" >&2
fi

# Cleanup journal on fully successful stamp completion.
if [[ -n "$JOURNAL_FILE" ]]; then
  rm -f "$JOURNAL_FILE"
  echo "[mint_run] journal removed: $JOURNAL_FILE"
fi

echo
echo "[mint_run] minted -> $ROOT"
[[ -n "$ROLE_PROSE" ]] && echo "[mint_run] role stamped: $ROLE_PROSE"
echo "[mint_run] next: user-approved 'mint_run.sh start --root $ROOT' to load launchd,"
echo "[mint_run]       then hand off to the new bot (greeting + onboarding naming)."
