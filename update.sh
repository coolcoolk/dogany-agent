#!/usr/bin/env bash
# update.sh -- refresh a Dogany instance's FRAMEWORK from this repo, safely.
#
# What it does:
#   1. git pull (fast-forward the repo to the latest published framework).
#   2. Re-sync ONLY framework code into the instance (agents/main by default):
#      bridge code, routines, memory engine, service SDK, database schema,
#      config, .claude/settings.json, worklog template, and the official
#      framework skills (root skills/dogany-*).
#   3. Re-substitute the five mint placeholders on the refreshed files, using
#      the instance manifest (.instance.conf) written at mint time.
#
# What it NEVER touches (user data is preserved verbatim):
#   - memories/            (long-term memory markdown)
#   - .telegram_bot/.env   (bot token, allowed users) and runtime/.env
#   - *.db                 (lifekit.db, memory/state.db -- user data + cache)
#   - bridge/venv/         (built virtualenv)
#   - AGENT.md / USER.md / CLAUDE.md / RULES.md   (identity + user-owned entrypoints)
#   - NON-dogany skills under .claude/skills/     (user-authored skills)
#
# It is idempotent: running it twice with no upstream changes is a no-op refresh.
#
# Usage:
#   ./update.sh                 # update ./agents/main (default instance)
#   ./update.sh --root DIR      # update a specific instance dir
#   ./update.sh --no-pull       # skip git pull (refresh from current checkout)
#   ./update.sh --dry-run       # show what would change, write nothing
#   DOGANY_LANG=ko ./update.sh  # Korean messages (default: en)
set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the repo (this script lives at repo root).
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$REPO_ROOT/agents/.template"
SKILLS_ROOT="$REPO_ROOT/skills"

# ---------------------------------------------------------------------------
# Bilingual message helper (mirrors install.sh).
# ---------------------------------------------------------------------------
DOGANY_LANG="${DOGANY_LANG:-en}"
msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { msg "[오류] $1" "[ERROR] $1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Args.
# ---------------------------------------------------------------------------
INSTANCE="$REPO_ROOT/agents/main"
DO_PULL=1
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root)    INSTANCE="$2"; shift 2 ;;
    --no-pull) DO_PULL=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,40p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -d "$TEMPLATE" ] || die "framework template not found: $TEMPLATE"
[ -d "$INSTANCE" ] || die "instance dir not found: $INSTANCE (pass --root DIR)"
INSTANCE="$(cd "$INSTANCE" && pwd)"

# Guard: never treat the repo itself or the template as the instance.
[ "$INSTANCE" = "$REPO_ROOT" ] && die "refusing to update the repo root itself"
[ "$INSTANCE" = "$TEMPLATE" ]  && die "refusing to update the template itself"

RSYNC_DRY=""
if [ "$DRY_RUN" = "1" ]; then
  RSYNC_DRY="--dry-run"
  msg "[dry-run] 파일을 쓰지 않고 변경 예정만 표시합니다." \
      "[dry-run] no files will be written; showing planned changes only."
fi

msg "[update] 레포   = $REPO_ROOT" "[update] repo     = $REPO_ROOT"
msg "[update] 인스턴스 = $INSTANCE" "[update] instance = $INSTANCE"

# ---------------------------------------------------------------------------
# 1) git pull -- fast-forward to the latest published framework.
# ---------------------------------------------------------------------------
if [ "$DO_PULL" = "1" ]; then
  if [ -d "$REPO_ROOT/.git" ]; then
    msg "[update] git pull ..." "[update] git pull ..."
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] git pull 생략" "  [dry-run] skipping git pull"
    else
      git -C "$REPO_ROOT" pull --ff-only \
        || die "git pull failed (resolve manually, or re-run with --no-pull)"
    fi
  else
    msg "[update] .git 없음 -> pull 건너뜀" "[update] no .git -> skipping pull"
  fi
fi

REPO_VERSION="unknown"
[ -f "$REPO_ROOT/VERSION" ] && REPO_VERSION="$(head -n1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
msg "[update] 프레임워크 버전 = $REPO_VERSION" "[update] framework version = $REPO_VERSION"

# ---------------------------------------------------------------------------
# 2) Recover instance identity (for placeholder re-substitution).
#    Prefer the manifest written by mint.sh; fall back to plist-derived name.
# ---------------------------------------------------------------------------
AGENT_NAME=""; AGENT_LABEL=""; USER_LABEL=""
if [ -f "$INSTANCE/.instance.conf" ]; then
  # shellcheck disable=SC1090
  . "$INSTANCE/.instance.conf"
  AGENT_NAME="${DOGANY_AGENT_NAME:-}"
  AGENT_LABEL="${DOGANY_AGENT_LABEL:-}"
  USER_LABEL="${DOGANY_USER_LABEL:-}"
fi
if [ -z "$AGENT_NAME" ]; then
  # Fallback: recover the agent name slug from a bridge plist filename.
  for p in "$INSTANCE"/bridge/com.*.newbridge.plist; do
    [ -e "$p" ] || continue
    base="$(basename "$p")"; base="${base#com.}"; AGENT_NAME="${base%%.*}"
    break
  done
fi
IDENTITY_OK=1
if [ -z "$AGENT_NAME" ] || [ -z "$AGENT_LABEL" ] || [ -z "$USER_LABEL" ]; then
  IDENTITY_OK=0
  msg "[update][경고] 인스턴스 정체성(.instance.conf)을 못 찾음 -- 정체성 플레이스홀더 치환은 건너뜁니다." \
      "[update][WARN] instance identity (.instance.conf) not found -- skipping identity placeholder substitution."
fi

# ---------------------------------------------------------------------------
# 3) Refresh framework paths (template -> instance), dereferencing symlinks so
#    the instance stays self-contained. Excludes protect all user data.
#    Rsync WITHOUT --delete on shared dirs so user files living beside framework
#    files (e.g. user skills, memories) are never removed.
# ---------------------------------------------------------------------------
COMMON_EXCLUDES=(
  --exclude '.git'
  --exclude '__pycache__'
  --exclude '*.pyc'
  --exclude '*.bak.*'
  --exclude '.DS_Store'
  --exclude 'venv'
  --exclude '*.db'
  --exclude '.env'
  --exclude 'runtime'
  --exclude 'logs'
)

UPDATED=()

# 3a) bridge code (framework), but keep the built venv and the live .env.
if [ -d "$TEMPLATE/bridge" ]; then
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    "$TEMPLATE/bridge/" "$INSTANCE/bridge/"
  UPDATED+=("bridge/")
fi

# 3b) routines (framework schedulers/scripts).
if [ -d "$TEMPLATE/routines" ]; then
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    "$TEMPLATE/routines/" "$INSTANCE/routines/"
  UPDATED+=("routines/")
fi

# 3c) memory engine code ONLY (*.py + taxonomy doc) -- never memory markdown/db.
if [ -d "$TEMPLATE/memory" ]; then
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    --include '*/' --include '*.py' --include '*.md' --exclude '*' \
    "$TEMPLATE/memory/" "$INSTANCE/memory/"
  UPDATED+=("memory/*.py")
fi

# 3d) config (agent.conf + i18n locales).
if [ -d "$TEMPLATE/config" ]; then
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    "$TEMPLATE/config/" "$INSTANCE/config/"
  UPDATED+=("config/")
fi

# 3e) service SDK facade (hoisted at repo root, bundled into the instance).
if [ -d "$REPO_ROOT/service" ]; then
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    "$REPO_ROOT/service/" "$INSTANCE/service/"
  UPDATED+=("service/")
fi

# 3f) database schema + CLI (framework), NEVER the *.db (excluded above).
mkdir -p "$INSTANCE/database"
for f in schema.sql lifekit.py lifekit.sh README.md; do
  [ -f "$REPO_ROOT/database/$f" ] || continue
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] database/$f 갱신 예정" "  [dry-run] would refresh database/$f"
  else
    cp -p "$REPO_ROOT/database/$f" "$INSTANCE/database/$f"
  fi
done
UPDATED+=("database/schema.sql+CLI")

# 3g) harness config: .claude/settings.json (framework), keep the skills dir intact.
if [ -f "$TEMPLATE/.claude/settings.json" ]; then
  mkdir -p "$INSTANCE/.claude"
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] .claude/settings.json 갱신 예정" "  [dry-run] would refresh .claude/settings.json"
  else
    cp -p "$TEMPLATE/.claude/settings.json" "$INSTANCE/.claude/settings.json"
  fi
  UPDATED+=(".claude/settings.json")
fi

# 3h) worklog template (framework), never existing worklog tickets.
if [ -f "$TEMPLATE/worklog/_TEMPLATE.md" ]; then
  mkdir -p "$INSTANCE/worklog"
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] worklog/_TEMPLATE.md 갱신 예정" "  [dry-run] would refresh worklog/_TEMPLATE.md"
  else
    cp -p "$TEMPLATE/worklog/_TEMPLATE.md" "$INSTANCE/worklog/_TEMPLATE.md"
  fi
  UPDATED+=("worklog/_TEMPLATE.md")
fi

# 3i) official framework skills: refresh ONLY skills/dogany-* into the instance.
#     User-authored (non-dogany-) skills under .claude/skills/ are left alone.
mkdir -p "$INSTANCE/.claude/skills"
DOGANY_SKILLS=()
for d in "$SKILLS_ROOT"/dogany-*/; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  DOGANY_SKILLS+=("$name")
  # --delete here is scoped to the single dogany-* skill dir, so it prunes files
  # removed upstream WITHOUT affecting sibling user skills.
  rsync -aL --delete $RSYNC_DRY "${COMMON_EXCLUDES[@]}" \
    "$d" "$INSTANCE/.claude/skills/$name/"
done
[ ${#DOGANY_SKILLS[@]} -gt 0 ] && UPDATED+=("skills: ${DOGANY_SKILLS[*]}")

# ---------------------------------------------------------------------------
# 4) Re-substitute the five mint placeholders on the refreshed files.
#    Path placeholders (PROJECT_ROOT, HOME) are always safe to re-apply.
#    Identity placeholders are applied only when recovered from the manifest.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "0" ]; then
  subst_one() {
    local f="$1"
    LC_ALL=C sed -i '' \
      -e "s#__PROJECT_ROOT__#${INSTANCE}#g" \
      -e "s#__HOME__#${HOME}#g" \
      "$f"
    if [ "$IDENTITY_OK" = "1" ]; then
      LC_ALL=C sed -i '' \
        -e "s#__AGENT_NAME__#${AGENT_NAME}#g" \
        -e "s#__AGENT_LABEL__#${AGENT_LABEL}#g" \
        -e "s#__USER_LABEL__#${USER_LABEL}#g" \
        "$f"
    fi
  }
  # Substitute across refreshed framework file types, but NEVER identity/user
  # entrypoints (they carry the user's filled-in identity, not placeholders).
  while IFS= read -r -d '' f; do
    subst_one "$f"
  done < <(find \
      "$INSTANCE/bridge" "$INSTANCE/routines" "$INSTANCE/memory" \
      "$INSTANCE/config" "$INSTANCE/service" "$INSTANCE/database" \
      "$INSTANCE/.claude/settings.json" \
      "$INSTANCE/.claude/skills" "$INSTANCE/worklog/_TEMPLATE.md" \
      \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.plist' \
         -o -name '*.md' -o -name '*.conf' -o -name '*.txt' -o -name '*.example' \) \
      -type f \
      -not -path '*/venv/*' -not -path '*/__pycache__/*' -not -name '*.bak.*' \
      -print0 2>/dev/null)

  # Rename any freshly-copied generic plists to carry the agent name (mint step 3).
  if [ "$IDENTITY_OK" = "1" ]; then
    for p in "$INSTANCE"/bridge/*.plist "$INSTANCE"/routines/*.plist; do
      [ -e "$p" ] || continue
      np="${p//telegram-agent/$AGENT_NAME}"
      [ "$np" != "$p" ] && [ ! -e "$np" ] && mv "$p" "$np"
    done
  fi

  # Record the framework version this instance now matches.
  if [ -f "$INSTANCE/.instance.conf" ]; then
    if grep -q '^DOGANY_FW_VERSION=' "$INSTANCE/.instance.conf"; then
      LC_ALL=C sed -i '' \
        -e "s#^DOGANY_FW_VERSION=.*#DOGANY_FW_VERSION=${REPO_VERSION}#" \
        "$INSTANCE/.instance.conf"
    else
      printf 'DOGANY_FW_VERSION=%s\n' "$REPO_VERSION" >> "$INSTANCE/.instance.conf"
    fi
  fi

  # Sanity: warn on any surviving placeholders in active code.
  LEFT="$(grep -rlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|HOME)__' \
            --include='*.py' --include='*.sh' --include='*.json' --include='*.plist' \
            "$INSTANCE/bridge" "$INSTANCE/routines" "$INSTANCE/memory" \
            "$INSTANCE/config" "$INSTANCE/.claude" 2>/dev/null || true)"
  if [ -n "$LEFT" ]; then
    msg "[update][경고] 치환되지 않은 플레이스홀더:" "[update][WARN] unsubstituted placeholders in:"
    printf '%s\n' "$LEFT" >&2
  fi
fi

# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------
msg "[update] 갱신한 프레임워크 구성요소:" "[update] refreshed framework components:"
for u in "${UPDATED[@]}"; do printf '  - %s\n' "$u"; done
msg "[update] 보존됨: memories/, .telegram_bot/.env, *.db, bridge/venv, AGENT.md, USER.md, 사용자 스킬" \
    "[update] preserved: memories/, .telegram_bot/.env, *.db, bridge/venv, AGENT.md, USER.md, user skills"
if [ "$DRY_RUN" = "1" ]; then
  msg "[update] dry-run 완료 (변경 없음)." "[update] dry-run complete (no changes written)."
else
  msg "[update] 완료. 브릿지 재시작이 필요하면 승인 후 진행하세요." \
      "[update] done. If the bridge needs a restart, do so with approval."
fi
