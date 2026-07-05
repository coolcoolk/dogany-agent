#!/usr/bin/env bash
# mint.sh -- instantiate a STANDALONE dogany-agent from this repo.
#
# The repo mirrors dogany-project's layout: the per-agent template lives at
# agents/.template, shared code is hoisted to rules/ + skills/ + database/ +
# service/, and agents/.template references the shared bits via symlinks
# (RULES.md -> ../../rules/RULES.md ; .claude/skills/<fw> -> ../../../../skills/<fw>).
#
# A minted instance is SELF-CONTAINED: the target dir is itself PROJECT_ROOT and
# carries real copies (never symlinks) of everything it needs -- rules, framework
# skills, database schema, service SDK -- so it runs with no parent-tree paths.
# mint.sh therefore copies from agents/.template while DEREFERENCING the shared
# symlinks (-L), and additionally bundles rules/, database/schema.sql, and
# service/ into the instance.
#
# Placeholders substituted (the ONLY six the framework uses):
#   __PROJECT_ROOT__   absolute instance root  = the target dir itself
#   __AGENT_NAME__     launchd Label slug + filenames
#   __AGENT_LABEL__    assistant speaker label
#   __USER_LABEL__     user honorific
#   __HOME__           OS user home (for PATH / HOME / ~/.claude)
#   __AGENT_LANG__     working language (en default; from install --lang)
#
# This script GENERATES files + builds the bridge venv only. It does NOT load
# launchd plists and does NOT start the bridge (live ops requiring approval).
set -euo pipefail

# Repo root = parent of this scripts/ dir. Path-independent: derived from the
# script location, never a hardcoded or parent-tree assumption.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/agents/.template"

usage() {
  cat <<USAGE
mint.sh -- instantiate a standalone dogany-agent

  Usage: mint.sh --root <target-dir> [options]

  Options:
    --root  <path>    instance dir (becomes PROJECT_ROOT). REQUIRED.
    --name  <text>    agent name / launchd slug        (default: basename of --root)
    --label <text>    assistant speaker label          (default: <name>)
    --user  <text>    user honorific label             (default: you)
    --lang  <en|ko>   working language                 (default: en)
    --token <token>   Telegram bot token for .env      (default: placeholder)
    --no-venv         skip building the bridge venv
    --core-only       build venv with core deps only (skip faster-whisper/voice)
    --force           allow minting into an existing non-empty dir
    -h, --help        this help

  Example:
    mint.sh --root /tmp/dga-inst --name testagent --core-only
USAGE
}

TARGET=""
AGENT_NAME=""
AGENT_LABEL=""
USER_LABEL="you"
AGENT_LANG="en"
BOT_TOKEN="your_bot_token_here"
BUILD_VENV=1
CORE_ONLY=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)  TARGET="$2"; shift 2 ;;
    --name)  AGENT_NAME="$2"; shift 2 ;;
    --label) AGENT_LABEL="$2"; shift 2 ;;
    --user)  USER_LABEL="$2"; shift 2 ;;
    --lang)  AGENT_LANG="$2"; shift 2 ;;
    --token) BOT_TOKEN="$2"; shift 2 ;;
    --no-venv) BUILD_VENV=0; shift ;;
    --core-only) CORE_ONLY=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

[ -n "$TARGET" ] || { echo "ERROR: --root <target-dir> is required" >&2; usage; exit 1; }

# Resolve target to an absolute path (may not exist yet).
mkdir -p "$TARGET"
PROJECT_ROOT="$(cd "$TARGET" && pwd)"
[ -n "$AGENT_NAME" ]  || AGENT_NAME="$(basename "$PROJECT_ROOT")"
[ -n "$AGENT_LABEL" ] || AGENT_LABEL="$AGENT_NAME"
[ -n "$AGENT_LANG" ]  || AGENT_LANG="en"
HOME_DIR="$HOME"

# Guard: refuse to mint into a non-empty dir unless --force.
if [ -n "$(ls -A "$PROJECT_ROOT" 2>/dev/null)" ] && [ "$FORCE" != "1" ]; then
  echo "ERROR: target not empty: $PROJECT_ROOT (use --force to overwrite)" >&2; exit 1
fi
# Guard: never mint onto the repo itself.
if [ "$PROJECT_ROOT" = "$REPO_ROOT" ]; then
  echo "ERROR: refusing to mint onto the repo root itself: $REPO_ROOT" >&2; exit 1
fi
# Guard: template must exist.
[ -d "$TEMPLATE" ] || { echo "ERROR: template not found: $TEMPLATE" >&2; exit 1; }

echo "[mint] repo       = $REPO_ROOT"
echo "[mint] agent      = $AGENT_NAME"
echo "[mint] root       = $PROJECT_ROOT"
echo "[mint] label      = $AGENT_LABEL   user = $USER_LABEL   lang = $AGENT_LANG"
echo "[mint] home       = $HOME_DIR"
echo "[mint] build venv = $BUILD_VENV   core-only = $CORE_ONLY"

# 1) copy agents/.template -> target, DEREFERENCING symlinks (-L) so the shared
#    RULES.md + framework skills land as real files in the self-contained instance.
#    Excludes VCS / runtime / build cruft.
#
#    config/lifekit.conf + config/agent.conf are EXCLUDED here and copied
#    write-if-absent below (same contract as .env / lifekit.db): a re-mint
#    with --force must never reset the instance's lifekit activation state
#    or the user's language/address settings.
#
#    CONSTRAINT (lifekit bundle dormancy): bundle skills live as REAL dirs in
#    .claude/skills-bundle/ and are activated by an instance-local symlink
#    .claude/skills/<id> -> ../skills-bundle/<id>, created ONLY post-mint by
#    the dogany-lifekit-setup skill. NEVER pre-place such symlinks in the
#    template: rsync -aL would dereference them into permanent real dirs and
#    break the off-toggle. Instance-created symlinks survive a re-mint because
#    this rsync has no --delete and the template has no such paths.
rsync -aL \
  --exclude '.git' \
  --exclude '/AGENT.md' \
  --exclude '/USER.md' \
  --exclude 'bridge/venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.bak.*' \
  --exclude '.DS_Store' \
  --exclude 'memory-engine/state.db' \
  --exclude '*.db' \
  --exclude 'config/lifekit.conf' \
  --exclude 'config/agent.conf' \
  "$TEMPLATE/" "$PROJECT_ROOT/"

# 1a) identity markdown: keep-if-present (re-mint must NEVER reset an agent's
#     identity -- Role, name, accreted Workflows live in AGENT.md; user facts
#     in USER.md). Excluded from the rsync above; copied only when absent.
for idmd in AGENT.md USER.md; do
  if [ ! -f "$PROJECT_ROOT/$idmd" ] && [ -f "$TEMPLATE/$idmd" ]; then
    cp -p "$TEMPLATE/$idmd" "$PROJECT_ROOT/$idmd"
    echo "[mint] wrote $idmd (scaffold)"
  elif [ -f "$PROJECT_ROOT/$idmd" ]; then
    echo "[mint] $idmd exists -> keep (identity preserved)"
  fi
done

# 1b) per-instance conf state: scaffold only if absent (idempotent re-mint).
for conf in lifekit.conf agent.conf; do
  if [ ! -f "$PROJECT_ROOT/config/$conf" ] && [ -f "$TEMPLATE/config/$conf" ]; then
    mkdir -p "$PROJECT_ROOT/config"
    cp -p "$TEMPLATE/config/$conf" "$PROJECT_ROOT/config/$conf"
    if [ "$conf" = "agent.conf" ]; then
      # keep conf in lockstep with --lang (template default is literal 'en')
      sed -i.tmp "s/^AGENT_LANG=.*/AGENT_LANG=${AGENT_LANG}/" "$PROJECT_ROOT/config/$conf" \
        && rm -f "$PROJECT_ROOT/config/$conf.tmp"
    fi
    echo "[mint] wrote config/$conf (scaffold)"
  elif [ -f "$PROJECT_ROOT/config/$conf" ]; then
    echo "[mint] config/$conf exists -> keep (idempotent)"
  fi
done

# 1c) bundle the hoisted shared roots the instance needs to be self-contained:
#     - (USER.md scaffold ships inside the template; RULES.md already dereferenced),
#     - database/ (schema only; *.db excluded -- lifekit.db is initialized below),
#     - service/ SDK facade (resolves ../../database/lifekit.py at the instance root).
mkdir -p "$PROJECT_ROOT/database"
[ -f "$REPO_ROOT/database/schema.sql" ] && cp -p "$REPO_ROOT/database/schema.sql" "$PROJECT_ROOT/database/schema.sql"
[ -f "$REPO_ROOT/database/lifekit.py" ] && cp -p "$REPO_ROOT/database/lifekit.py" "$PROJECT_ROOT/database/lifekit.py"
[ -f "$REPO_ROOT/database/lifekit.sh" ] && cp -p "$REPO_ROOT/database/lifekit.sh" "$PROJECT_ROOT/database/lifekit.sh"
[ -f "$REPO_ROOT/database/schema.sql" ] && [ -f "$REPO_ROOT/database/README.md" ] && cp -p "$REPO_ROOT/database/README.md" "$PROJECT_ROOT/database/README.md"
if [ -d "$REPO_ROOT/service" ]; then
  rsync -aL --exclude '__pycache__' --exclude '*.pyc' "$REPO_ROOT/service/" "$PROJECT_ROOT/service/"
fi

# Portable in-place sed: BSD (macOS) and GNU (Linux) disagree on `sed -i`'s
# flavor (BSD requires a mandatory backup-suffix arg, GNU forbids the space).
# Sidestep the incompatibility entirely: run sed to a temp file, then mv it back.
# Args: <file> <sed-arg>...  (the sed args are the -e expressions to apply).
# Preserves LC_ALL=C. GNU-safe by construction (no -i used at all).
sed_inplace() {
  local f="$1"; shift
  local tmp
  tmp="$(mktemp "${f}.sed.XXXXXX")"
  if LC_ALL=C sed "$@" "$f" > "$tmp"; then
    mv -f "$tmp" "$f"
  else
    rm -f "$tmp"
    return 1
  fi
}

# 2) substitute the six placeholders across text files.
#    '#' delimiter since values (paths) contain '/'. Tokens are distinct and the
#    substituted values never reintroduce another token, so order is irrelevant.
substitute() {
  local f="$1"
  sed_inplace "$f" \
    -e "s#__PROJECT_ROOT__#${PROJECT_ROOT}#g" \
    -e "s#__AGENT_NAME__#${AGENT_NAME}#g" \
    -e "s#__AGENT_LABEL__#${AGENT_LABEL}#g" \
    -e "s#__USER_LABEL__#${USER_LABEL}#g" \
    -e "s#__HOME__#${HOME_DIR}#g" \
    -e "s#__AGENT_LANG__#${AGENT_LANG}#g"
}

while IFS= read -r -d '' f; do
  substitute "$f"
done < <(find "$PROJECT_ROOT" -type f \
            \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.plist' \
               -o -name '*.md' -o -name '*.example' -o -name '*.txt' -o -name '*.conf' \) \
            -not -path '*/venv/*' -not -path '*/.git/*' -print0)

# 3) rename agent-specific plists (Label already substituted; make filenames
#    carry the agent name for launchd clarity).
for p in "$PROJECT_ROOT"/bridge/*.plist "$PROJECT_ROOT"/routines/*.plist; do
  [ -e "$p" ] || continue
  np="${p//telegram-agent/$AGENT_NAME}"
  [ "$np" != "$p" ] && mv "$p" "$np"
done

# 3b) write an instance manifest so update.sh can re-substitute the same five
#     placeholders when it refreshes framework files, and record the framework
#     version this instance was built from. Non-secret; no token/chat id here.
FW_VERSION="unknown"
[ -f "$REPO_ROOT/VERSION" ] && FW_VERSION="$(head -n1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
# First-mint date: preserved across re-mints (the instance's birthday, UTC).
MINTED_AT="$(grep -E '^DOGANY_MINTED_AT=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
MINTED_AT="${MINTED_AT:-$(date -u +%Y-%m-%d)}"
# Tier: preserved across re-mints (keep-if-present, same contract as MINTED_AT).
# Fresh mint -> lite (the free tier). Readers treat a missing field as lite too
# (fail-closed). Enum stays lite/basic/pro; marketing names = HAND/CRAFT/MASTER.
TIER="$(grep -E '^DOGANY_TIER=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
TIER="${TIER:-lite}"
cat > "$PROJECT_ROOT/.instance.conf" <<MANIFEST
# .instance.conf -- non-secret instance manifest written by mint.sh.
# Consumed by update.sh to re-substitute placeholders on framework refresh.
# Secrets (bot token, chat id) live in .telegram_bot/.env, NEVER here.
DOGANY_AGENT_NAME=${AGENT_NAME}
DOGANY_AGENT_LABEL=${AGENT_LABEL}
DOGANY_USER_LABEL=${USER_LABEL}
DOGANY_FW_VERSION=${FW_VERSION}
DOGANY_REPO_ROOT=${REPO_ROOT}
DOGANY_MINTED_AT=${MINTED_AT}
DOGANY_TIER=${TIER}
MANIFEST
echo "[mint] wrote $PROJECT_ROOT/.instance.conf (framework version ${FW_VERSION})"

# 3c) write the dogany-* skills checksum manifest. This records the sha of every
#     framework skill AS INSTALLED so update.sh can later tell whether the user
#     hand-edited one and must back it up before a framework refresh overwrites
#     it. Format: "<skill-name>  <sha>" (must match update.sh's skill_checksum).
skill_checksum() {
  local dir="$1"
  [ -d "$dir" ] || { printf '%s\n' "d41d8cd98f00b204e9800998ecf8427e-empty"; return; }
  ( cd "$dir" && \
    find . -type f ! -name '.DS_Store' -print0 2>/dev/null \
      | LC_ALL=C sort -z \
      | xargs -0 shasum 2>/dev/null \
      | shasum \
      | awk '{print $1}' )
}
SKILLS_MANIFEST="$PROJECT_ROOT/.claude/.dogany-skills.sha"
if [ -d "$PROJECT_ROOT/.claude/skills" ]; then
  mkdir -p "$PROJECT_ROOT/.claude"
  {
    printf '# .dogany-skills.sha -- checksums of framework dogany-* skills as installed\n'
    printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
    printf '# framework refresh overwrites them. Format: "<skill-name>  <sha>".\n'
    for sd in "$PROJECT_ROOT"/.claude/skills/dogany-*/; do
      [ -d "$sd" ] || continue
      sname="$(basename "$sd")"
      printf '%s  %s\n' "$sname" "$(skill_checksum "$sd")"
    done
  } > "$SKILLS_MANIFEST"
  echo "[mint] wrote $SKILLS_MANIFEST (dogany-* skill checksums)"
fi

# 4) create the project .env from example (token in; placeholder otherwise).
ENV_SRC="$PROJECT_ROOT/.telegram_bot/.env.example"
ENV_DST="$PROJECT_ROOT/.telegram_bot/.env"
if [ -f "$ENV_SRC" ] && [ ! -f "$ENV_DST" ]; then
  if grep -q '^TELEGRAM_BOT_TOKEN=' "$ENV_SRC"; then
    sed "s#^TELEGRAM_BOT_TOKEN=.*#TELEGRAM_BOT_TOKEN=${BOT_TOKEN}#" "$ENV_SRC" > "$ENV_DST"
  else
    { echo "TELEGRAM_BOT_TOKEN=${BOT_TOKEN}"; cat "$ENV_SRC"; } > "$ENV_DST"
  fi
  chmod 600 "$ENV_DST"
  echo "[mint] wrote $ENV_DST"
fi
mkdir -p "$PROJECT_ROOT/.telegram_bot/logs"

# 5) initialize the structured lane (lifekit.db) from schema.sql.
#    Empty structured lane, ready to receive data. NO user data seeded -- the
#    config table stays empty so the body-state hook is a no-op for a new user.
#    Idempotent: never clobber an existing db (a re-mint with --force keeps data).
SCHEMA_SRC="$PROJECT_ROOT/database/schema.sql"
LIFEKIT_DB="$PROJECT_ROOT/database/lifekit.db"
if [ -f "$LIFEKIT_DB" ]; then
  echo "[mint] lifekit.db exists -> keep (idempotent)"
elif [ ! -f "$SCHEMA_SRC" ]; then
  echo "[mint][WARN] no schema.sql at $SCHEMA_SRC -- skipping lifekit.db init" >&2
elif ! command -v sqlite3 >/dev/null 2>&1; then
  echo "[mint][WARN] sqlite3 not found -- skipping lifekit.db init" >&2
else
  sqlite3 "$LIFEKIT_DB" < "$SCHEMA_SRC"
  echo "[mint] initialized $LIFEKIT_DB from schema.sql (empty structured lane)"
fi

# 6) build the bridge venv (self-contained, next to bridge/).
# The interpreter defaults to python3 but can be pinned via DOGANY_PYTHON_BIN
# (install.sh sets it to the interpreter it resolved as >= 3.11, so the venv is
# built with the right Python even when the system python3 is older).
if [ "$BUILD_VENV" = "1" ]; then
  VENV_PYTHON="${DOGANY_PYTHON_BIN:-python3}"
  echo "[mint] building bridge venv (interpreter: $VENV_PYTHON) ..."
  "$VENV_PYTHON" -m venv "$PROJECT_ROOT/bridge/venv"
  "$PROJECT_ROOT/bridge/venv/bin/pip" install -q --upgrade pip
  if [ "$CORE_ONLY" = "1" ]; then
    # Core deps needed to import the bridge. faster-whisper (voice) is optional
    # and heavy; install it separately when voice input is wanted.
    "$PROJECT_ROOT/bridge/venv/bin/pip" install -q \
      "python-telegram-bot>=20.7" "claude-agent-sdk>=0.1.72" \
      "pydantic>=2" "pydantic-settings>=2" python-dotenv
  else
    "$PROJECT_ROOT/bridge/venv/bin/pip" install -q -r "$PROJECT_ROOT/bridge/requirements.txt"
  fi
  echo "[mint] venv ready"
fi

# 7) sanity: no placeholder survivors.
#    (a) __X__ framework tokens across code + markdown (any survivor = a real
#        substitution miss).
LEFT="$(grep -rlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|HOME|AGENT_LANG)__' \
          --include='*.py' --include='*.sh' --include='*.json' --include='*.plist' --include='*.md' \
          "$PROJECT_ROOT" 2>/dev/null || true)"
if [ -n "$LEFT" ]; then
  echo "[mint][WARN] placeholder survivors (__X__ tokens) in:" >&2; echo "$LEFT" >&2
fi
#    (b) angle-bracket aspirational placeholders <UPPER_SNAKE> in the baseline
#        identity/rules markdown (AGENT.md / RULES.md / USER.md / CLAUDE.md).
#        These were never wired to a token; a mint would leave them raw. Scoped
#        to these files on purpose: skill docs legitimately use <TAB>/<name>
#        notation, which is not a placeholder. HTML comments (<!-- ... -->) start
#        with '!' so they never match <[A-Z_]+>.
ANGLE="$(grep -rlE '<[A-Z][A-Z_]+>' \
          --include='AGENT.md' --include='RULES.md' --include='USER.md' --include='CLAUDE.md' \
          "$PROJECT_ROOT" 2>/dev/null || true)"
if [ -n "$ANGLE" ]; then
  echo "[mint][WARN] angle-bracket placeholder survivors in:" >&2; echo "$ANGLE" >&2
fi

cat <<DONE

[mint] DONE -> $PROJECT_ROOT

Next steps (manual / require approval):
  1. Set the bot token:  edit $ENV_DST (TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS)
  2. Fill identity:      AGENT.md onboarding skeleton; first SessionStart runs
                         onboarding-check.py to set name/emoji/tone.
  3. (optional) voice:   bridge/venv/bin/pip install faster-whisper
  4. Load launchd:       cp bridge/*.plist routines/*.plist ~/Library/LaunchAgents/
                         then launchctl bootstrap (LIVE op -- get approval first).
DONE
