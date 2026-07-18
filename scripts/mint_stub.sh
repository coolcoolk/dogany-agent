#!/usr/bin/env bash
# mint_stub.sh -- stub replacing mint.sh for DGN-227 sandbox rehearsal.
# Creates minimal instance directory structure without real bot tokens.
set -euo pipefail

ROOT="" NAME="dogany" LANG="en" OWNER_ID="12345" TZ_OPT="Asia/Seoul"
CORE_ONLY=0 FORCE=0 PRINT_ENV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)      ROOT="$2"; shift 2 ;;
    --name)      NAME="$2"; shift 2 ;;
    --label)     shift 2 ;;
    --user)      shift 2 ;;
    --lang)      LANG="$2"; shift 2 ;;
    --owner-id)  OWNER_ID="$2"; shift 2 ;;
    --tz)        TZ_OPT="$2"; shift 2 ;;
    --models)    shift 2 ;;
    --whisper)   shift 2 ;;
    --email)     shift 2 ;;
    --email-cc)  shift 2 ;;
    --core-only) CORE_ONLY=1; shift ;;
    --force)     FORCE=1; shift ;;
    --env-overwrite) shift ;;
    --print-env)
      echo "TELEGRAM_BOT_TOKEN=stub_token"
      echo "ALLOWED_USER_IDS=${OWNER_ID:-12345}"
      echo "TZ=${TZ_OPT:-Asia/Seoul}"
      exit 0 ;;
    *) shift ;;
  esac
done

[[ -n "$ROOT" ]] || { echo "mint_stub: --root required" >&2; exit 1; }

mkdir -p "$ROOT/bridge"
mkdir -p "$ROOT/config/packs"
mkdir -p "$ROOT/routines"
mkdir -p "$ROOT/.claude/skills-bundle"
mkdir -p "$ROOT/.claude/skills"
mkdir -p "$ROOT/.telegram_bot/logs"
mkdir -p "$ROOT/memory-engine"
mkdir -p "$ROOT/database"

# AGENT.md with Primary-focus placeholder (matches _stamp_role's expected pattern)
cat > "$ROOT/AGENT.md" <<'EOF'
# AGENT

## Role
- Primary focus: (set at onboarding -- one prose line naming the main hat;
  the general front-door bullet above still applies)

## Onboarding

  1. name  -- pick a name
  2. emoji -- pick an emoji
  3. title -- how to call the user
  4. tone  -- communication tone
  5. humor -- humor level
  6. role  -- LAST: two options
     - 1. life assistant (lifekit)
     - 2. custom role
Keep each question short.

when all are filled (question 6 = the Primary-focus slot filled),
the onboarding is complete.
EOF

# .instance.conf
cat > "$ROOT/.instance.conf" <<EOF
DOGANY_AGENT_NAME=${NAME}
DOGANY_AGENT_LABEL=${NAME}
DOGANY_USER_LABEL=you
DOGANY_AGENT_PREFIX=[agent]
DOGANY_REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
MINTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
TIER=lite
EOF

# config/agent.conf
printf 'AGENT_LANG=%s\n' "$LANG" > "$ROOT/config/agent.conf"

# config/lifekit.conf
printf 'LIFEKIT=pending\n' > "$ROOT/config/lifekit.conf"

# .claude/settings.json
printf '{"model": "sonnet", "hooks": {}}\n' > "$ROOT/.claude/settings.json"

# .telegram_bot/.env
mkdir -p "$ROOT/.telegram_bot"
cat > "$ROOT/.telegram_bot/.env" <<EOF
TELEGRAM_BOT_TOKEN=stub_token
ALLOWED_USER_IDS=${OWNER_ID}
TZ=${TZ_OPT}
EOF
chmod 600 "$ROOT/.telegram_bot/.env"

echo "[mint_stub] minted -> $ROOT"
