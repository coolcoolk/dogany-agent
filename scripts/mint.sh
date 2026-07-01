#!/usr/bin/env bash
# mint.sh -- instantiate a STANDALONE dogany-agent from this repo.
#
# Unlike dogany-project/scripts/mint-agent.sh (which mints from agents/.template
# into a shared parent tree with ../../rules + ../../../database), dogany-agent is
# SELF-CONTAINED: the minted instance dir is itself PROJECT_ROOT, and lifekit lives
# inside the instance at database/. No parent-tree paths are ever assumed.
#
# Placeholders substituted (the ONLY five the framework uses):
#   __PROJECT_ROOT__   absolute instance root  = the target dir itself
#   __AGENT_NAME__     launchd Label slug + filenames
#   __AGENT_LABEL__    assistant speaker label
#   __USER_LABEL__     user honorific
#   __HOME__           OS user home (for PATH / HOME / ~/.claude)
#
# This script GENERATES files + builds the bridge venv only. It does NOT load
# launchd plists and does NOT start the bridge (live ops requiring approval).
set -euo pipefail

# Repo root = parent of this scripts/ dir. Path-independent: derived from the
# script location, never a hardcoded or parent-tree assumption.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<USAGE
mint.sh -- instantiate a standalone dogany-agent

  Usage: mint.sh --root <target-dir> [options]

  Options:
    --root  <path>    instance dir (becomes PROJECT_ROOT). REQUIRED.
    --name  <text>    agent name / launchd slug        (default: basename of --root)
    --label <text>    assistant speaker label          (default: <name>)
    --user  <text>    user honorific label             (default: 사용자)
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
USER_LABEL="사용자"
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
HOME_DIR="$HOME"

# Guard: refuse to mint into a non-empty dir unless --force.
if [ -n "$(ls -A "$PROJECT_ROOT" 2>/dev/null)" ] && [ "$FORCE" != "1" ]; then
  echo "ERROR: target not empty: $PROJECT_ROOT (use --force to overwrite)" >&2; exit 1
fi
# Guard: never mint onto the repo itself.
if [ "$PROJECT_ROOT" = "$REPO_ROOT" ]; then
  echo "ERROR: refusing to mint onto the repo root itself: $REPO_ROOT" >&2; exit 1
fi

echo "[mint] repo       = $REPO_ROOT"
echo "[mint] agent      = $AGENT_NAME"
echo "[mint] root       = $PROJECT_ROOT"
echo "[mint] label      = $AGENT_LABEL   user = $USER_LABEL"
echo "[mint] home       = $HOME_DIR"
echo "[mint] build venv = $BUILD_VENV   core-only = $CORE_ONLY"

# 1) copy repo -> target, excluding VCS / runtime / build cruft.
rsync -a \
  --exclude '.git' \
  --exclude 'bridge/venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.bak.*' \
  --exclude '.DS_Store' \
  --exclude 'memory/state.db' \
  --exclude '*.db' \
  "$REPO_ROOT/" "$PROJECT_ROOT/"

# 2) substitute the five placeholders across text files.
#    '#' delimiter since values (paths) contain '/'. Tokens are distinct and the
#    substituted values never reintroduce another token, so order is irrelevant.
substitute() {
  local f="$1"
  LC_ALL=C sed -i '' \
    -e "s#__PROJECT_ROOT__#${PROJECT_ROOT}#g" \
    -e "s#__AGENT_NAME__#${AGENT_NAME}#g" \
    -e "s#__AGENT_LABEL__#${AGENT_LABEL}#g" \
    -e "s#__USER_LABEL__#${USER_LABEL}#g" \
    -e "s#__HOME__#${HOME_DIR}#g" \
    "$f"
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

# 5) build the bridge venv (self-contained, next to bridge/).
if [ "$BUILD_VENV" = "1" ]; then
  echo "[mint] building bridge venv ..."
  python3 -m venv "$PROJECT_ROOT/bridge/venv"
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

# 6) sanity: no placeholder survivors in active code files.
LEFT="$(grep -rlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|HOME)__' \
          --include='*.py' --include='*.sh' --include='*.json' --include='*.plist' \
          "$PROJECT_ROOT" 2>/dev/null || true)"
if [ -n "$LEFT" ]; then
  echo "[mint][WARN] placeholder survivors in:" >&2; echo "$LEFT" >&2
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
