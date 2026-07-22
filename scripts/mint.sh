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

  Secrets (environment variables ONLY -- argv is visible in ps):
    DOGANY_BOT_TOKEN   Telegram bot token for .env (wins over --token)
    (email uses Google OAuth in agent onboarding -- no app password, DGN-268 S4)

  Options:
    --root  <path>    instance dir (becomes PROJECT_ROOT). REQUIRED (except --print-env).
    --name   <text>    agent name / launchd slug        (default: basename of --root)
    --label  <text>    assistant speaker label          (default: <name>)
    --user   <text>    user honorific label             (default: you)
    --prefix <text>    notify prefix emoji/tag          (default: [agent])
    --lang   <en|ko>   working language + .env LOCALE   (default: en)
    --token <token>   DEPRECATED bot-token fallback; prefer DOGANY_BOT_TOKEN
    --owner-id <ids>  ALLOWED_USER_IDS for .env         (default: empty = claim mode)
    --tz <tz>         IANA timezone for .env; empty = TZ line omitted
    --email <addr>    outbound email address for .env   (default: empty = not connected)
    --email-cc <addr> CC address for outbound email     (default: empty)
    --whisper <model> faster-whisper model for .env     (default: empty = line omitted)
    --models <list>   bridge model whitelist, comma-sep (default: empty = line omitted)
    --print-env       render the .env body to stdout and exit (no --root, no writes)
    --env-overwrite   overwrite an existing .env atomically (install reconfigure only)
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
AGENT_PREFIX="[agent]"
AGENT_LANG="en"
# The bot token (DOGANY_BOT_TOKEN) comes from the environment only; it is never
# stored as a script-level variable to avoid any accidental echo. Email uses
# Google OAuth (DGN-268 S4) -- no email secret passes through mint.
BUILD_VENV=1
CORE_ONLY=0
FORCE=0
OWNER_IDS=""           # --owner-id
AGENT_TZ=""            # --tz
EMAIL_ADDR=""          # --email
EMAIL_CC_ADDR=""       # --email-cc
WHISPER_MODEL=""       # --whisper
BRIDGE_MODELS_ENV=""   # --models
PRINT_ENV=0            # --print-env
ENV_OVERWRITE=0        # --env-overwrite

while [ $# -gt 0 ]; do
  case "$1" in
    --root)         TARGET="$2"; shift 2 ;;
    --name)         AGENT_NAME="$2"; shift 2 ;;
    --label)        AGENT_LABEL="$2"; shift 2 ;;
    --user)         USER_LABEL="$2"; shift 2 ;;
    --prefix)       AGENT_PREFIX="$2"; shift 2 ;;
    --lang)         AGENT_LANG="$2"; shift 2 ;;
    --token)
      # DEPRECATED fallback: use the DOGANY_BOT_TOKEN env var instead (argv is
      # visible in ps). The env var WINS when both are set (env-first contract).
      if [ -z "${DOGANY_BOT_TOKEN:-}" ]; then DOGANY_BOT_TOKEN="$2"; fi
      shift 2 ;;
    --owner-id)     OWNER_IDS="$2"; shift 2 ;;
    --tz)           AGENT_TZ="$2"; shift 2 ;;
    --email)        EMAIL_ADDR="$2"; shift 2 ;;
    --email-cc)     EMAIL_CC_ADDR="$2"; shift 2 ;;
    --whisper)      WHISPER_MODEL="$2"; shift 2 ;;
    --models)       BRIDGE_MODELS_ENV="$2"; shift 2 ;;
    --print-env)    PRINT_ENV=1; shift ;;
    --env-overwrite) ENV_OVERWRITE=1; shift ;;
    --no-venv)      BUILD_VENV=0; shift ;;
    --core-only)    CORE_ONLY=1; shift ;;
    --force)        FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# env_render -- the SINGLE .env generator (install.sh passes values, never
# renders). Emits the exact key names bridge/config.py reads. Reads the bot
# token from environment (DOGANY_BOT_TOKEN -- set by caller, never argv).
# Conditional lines: TZ only when a tz was given; BRIDGE_MODELS /
# LOCAL_WHISPER_MODEL only when set so config.py defaults apply. LOCALE is
# ALWAYS emitted. No [mint] banner lines on stdout. Email sends via Google
# OAuth (DGN-268 S4) -- no app password / SMTP keys are written.
env_render() {
  printf '# Dogany bridge configuration -- generated by dogany mint\n'
  printf '# Do NOT commit this file (contains your bot token).\n\n'
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "${DOGANY_BOT_TOKEN:-}"
  printf '# Born-locked: when set, this list is authoritative and claim mode is off.\n'
  printf 'ALLOWED_USER_IDS=%s\n' "${OWNER_IDS}"
  printf 'LOCALE=%s\n' "${AGENT_LANG}"
  # TZ line is omitted entirely when no tz was given (absent != empty).
  if [ -n "${AGENT_TZ}" ]; then
    printf 'TZ=%s\n' "${AGENT_TZ}"
  fi
  printf '# Extra path-guard roots (os.pathsep-separated). Empty for the product.\n'
  printf 'EXTRA_ALLOWED_ROOTS=\n'
  # DGN-167: bridge model whitelist seeded from subscription tier at install time.
  # max tier -> fable,opus,sonnet,haiku (DGN-346); non-max -> sonnet,haiku. Controls /model picker.
  if [ -n "${BRIDGE_MODELS_ENV}" ]; then
    printf '# --- Bridge model whitelist (controls /model picker). Set by installer.\n'
    printf 'BRIDGE_MODELS=%s\n' "${BRIDGE_MODELS_ENV}"
  fi
  # DGN-146: voice model chosen at the deps step. Only emitted when voice is
  # enabled; skip leaves it out so config.py's "small" default applies.
  if [ -n "${WHISPER_MODEL}" ]; then
    printf '# --- Voice input (faster-whisper). Model chosen during install.\n'
    printf 'LOCAL_WHISPER_MODEL=%s\n' "${WHISPER_MODEL}"
  fi
  printf '# --- Email (dogany-mailer). Sent via your connected Google account\n'
  printf '# (gws gmail, one OAuth login covering calendar + tasks + gmail.send;\n'
  printf '# connected in agent onboarding). No app password / SMTP keys here.\n'
  printf '# EMAIL_ADDRESS: optional From hint. EMAIL_CC: owner auto-CC on sends.\n'
  printf 'EMAIL_ADDRESS=%s\n' "${EMAIL_ADDR}"
  printf 'EMAIL_CC=%s\n' "${EMAIL_CC_ADDR}"
}

# --print-env: render the .env body to stdout and exit -- handled FIRST,
# before the --root guard, before any mkdir, and before any [mint] banner
# echo. stdout must stay byte-clean env content (callers redirect it to a
# file); everything else this script prints goes to stdout only AFTER this
# early exit, so a --print-env run can never pollute the render.
if [ "$PRINT_ENV" = "1" ]; then
  env_render
  exit 0
fi

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
#    config/lifekit.conf + config/agent.conf + config/secret-patterns.conf are
#    EXCLUDED here and copied write-if-absent below (same contract as .env /
#    lifekit.db): a re-mint with --force must never reset the instance's lifekit
#    activation state, the user's language/address settings, or owner-identity
#    sweep patterns already customized after onboarding.
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
  --exclude 'config/secret-patterns.conf' \
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
for conf in lifekit.conf agent.conf secret-patterns.conf; do
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
for f in schema.sql lifekit.py lifekit.sh README.md remind_select.py routine_roller.py routine_projection.py relmod.py; do
  [ -f "$REPO_ROOT/database/$f" ] && cp -p "$REPO_ROOT/database/$f" "$PROJECT_ROOT/database/$f"
done
if [ -d "$REPO_ROOT/service" ]; then
  rsync -aL --exclude '__pycache__' --exclude '*.pyc' "$REPO_ROOT/service/" "$PROJECT_ROOT/service/"
fi

# 1d) mirror/ engine (DGN-268 S3): the GCal/GTasks mirror lives at repo-root
#     mirror/ (single canonical home; NOT in the template, to avoid a third
#     copy). Ship CODE + schema ONLY into the instance so the cron flag-gates
#     have something to import. NEVER copy state: mirror_state.db holds the
#     instance's live sync bookkeeping (surface ids / etags / cursors) -- a
#     re-mint must preserve it, so *.db / *.db.bak* are excluded exactly like
#     lifekit.db above. Idempotent on re-mint: rsync has no --delete and the
#     db excludes protect live state, so re-minting only refreshes code.
if [ -d "$REPO_ROOT/mirror" ]; then
  rsync -aL \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '*.db' \
    --exclude '*.db-wal' \
    --exclude '*.db-shm' \
    --exclude '*.db.bak*' \
    --exclude '*.bak*' \
    "$REPO_ROOT/mirror/" "$PROJECT_ROOT/mirror/"
  echo "[mint] copied mirror/ engine (code + schema; *.db excluded)"
fi

# Portable in-place sed: BSD (macOS) and GNU (Linux) disagree on `sed -i`'s
# flavor (BSD requires a mandatory backup-suffix arg, GNU forbids the space).
# Sidestep the incompatibility entirely: run sed to a temp file, then mv it back.
# Args: <file> <sed-arg>...  (the sed args are the -e expressions to apply).
# Preserves LC_ALL=C. GNU-safe by construction (no -i used at all).
# MODE PRESERVATION: mktemp creates 0600 files, so a bare mv would clobber the
# target's permissions (exec bits). `cp -p` stamps the original file's mode
# onto the temp BEFORE the mv; the sed redirect truncates content only.
sed_inplace() {
  local f="$1"; shift
  local tmp
  tmp="$(mktemp "${f}.sed.XXXXXX")"
  cp -p "$f" "$tmp"
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
    -e "s#__AGENT_PREFIX__#${AGENT_PREFIX}#g" \
    -e "s#__HOME__#${HOME_DIR}#g" \
    -e "s#__AGENT_LANG__#${AGENT_LANG}#g"
}

while IFS= read -r -d '' f; do
  substitute "$f"
done < <(find "$PROJECT_ROOT" -type f \
            \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.plist' \
               -o -name '*.service' -o -name '*.timer' \
               -o -name '*.md' -o -name '*.example' -o -name '*.txt' -o -name '*.conf' \) \
            -not -path '*/venv/*' -not -path '*/.git/*' -print0)

# 3) rename agent-specific units (Label already substituted; make filenames
#    carry the agent name for launchd/systemd clarity). Covers macOS plists and
#    the Linux mirror systemd units (DGN-268 S3 .service/.timer parity).
for p in "$PROJECT_ROOT"/bridge/*.plist "$PROJECT_ROOT"/routines/*.plist \
         "$PROJECT_ROOT"/routines/*.service "$PROJECT_ROOT"/routines/*.timer; do
  [ -e "$p" ] || continue
  np="${p//telegram-agent/$AGENT_NAME}"
  [ "$np" != "$p" ] && mv "$p" "$np"
done

# 3a) plists.defer basename manifest (DGN-227 E1-1): its entries must keep
#     matching the plist filenames after the rename above, and the file has no
#     extension so the step-2 render pass does not cover it -- substitute here.
if [ -f "$PROJECT_ROOT/routines/plists.defer" ]; then
  sed -i.bak "s/telegram-agent/$AGENT_NAME/g" "$PROJECT_ROOT/routines/plists.defer" \
    && rm -f "$PROJECT_ROOT/routines/plists.defer.bak"
fi

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
# Agent prefix: preserved across re-mints (the instance's baked notify prefix).
# Fresh mint -> the value of --prefix (default [agent]). update.sh reads this
# field to re-substitute __AGENT_PREFIX__ after a framework refresh.
SAVED_PREFIX="$(grep -E '^DOGANY_AGENT_PREFIX=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
# --prefix flag wins on a new mint; on re-mint keep the existing baked value
# unless the operator explicitly passes --prefix again (AGENT_PREFIX != default).
if [ -n "$SAVED_PREFIX" ] && [ "$AGENT_PREFIX" = "[agent]" ]; then
  AGENT_PREFIX="$SAVED_PREFIX"
fi
# DGN-227 MAJOR-4 (A4/P4): agent class + pack-consumption record are preserved
# across re-mints (keep-if-present, same contract as MINTED_AT/TIER/PREFIX).
# mint.sh itself does not author these -- install.sh (dgn227_postmint) stamps
# DOGANY_AGENT_CLASS and pack_install upserts DOGANY_PACKS -- but a re-mint /
# recover rewrites .instance.conf wholesale, so without this a domain instance
# would silently reclassify to main (P13 default) and its pack record (which
# --upgrade version comparison depends on) would be wiped. Re-emit only when a
# prior value exists so a fresh direct mint keeps its current lean manifest.
SAVED_CLASS="$(grep -E '^DOGANY_AGENT_CLASS=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
SAVED_PACKS="$(grep -E '^DOGANY_PACKS=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
cat > "$PROJECT_ROOT/.instance.conf" <<MANIFEST
# .instance.conf -- non-secret instance manifest written by mint.sh.
# Consumed by update.sh to re-substitute placeholders on framework refresh.
# Secrets (bot token, chat id) live in .telegram_bot/.env, NEVER here.
DOGANY_AGENT_NAME=${AGENT_NAME}
DOGANY_AGENT_LABEL=${AGENT_LABEL}
DOGANY_USER_LABEL=${USER_LABEL}
DOGANY_AGENT_PREFIX=${AGENT_PREFIX}
DOGANY_FW_VERSION=${FW_VERSION}
DOGANY_REPO_ROOT=${REPO_ROOT}
DOGANY_MINTED_AT=${MINTED_AT}
DOGANY_TIER=${TIER}
MANIFEST
# DGN-227 MAJOR-4: append preserved class/pack records only when they existed
# (keep-if-present). Emitting them conditionally keeps a fresh direct mint's
# manifest unchanged while a re-mint of a domain / pack-consuming instance
# retains DOGANY_AGENT_CLASS and DOGANY_PACKS.
[ -n "$SAVED_CLASS" ] && printf 'DOGANY_AGENT_CLASS=%s\n' "$SAVED_CLASS" >> "$PROJECT_ROOT/.instance.conf"
[ -n "$SAVED_PACKS" ] && printf 'DOGANY_PACKS=%s\n' "$SAVED_PACKS" >> "$PROJECT_ROOT/.instance.conf"
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

# 3d) write the framework FILE checksum manifest (.dogany-framework.sha) so the
#     very first self-update does NOT fire a spurious "user-modified" WARN +
#     backup on a pristine instance (DGN-387). Records the sha of the
#     framework-owned instance-root files AS THEY LAND ON DISK here at mint:
#       - AGENT-OPS.md : sha of the SUBSTITUTED on-disk file (step 2 already ran
#                        the whole-tree *.md substitution over it).
#       - RULES.md     : sha of the verbatim-copied RULES.md.
#     Same digest as update.sh's file_checksum (shasum < file | awk '{print $1}')
#     and the same 3-line manifest header, so update.sh's 3k/3k2 upserts append
#     cleanly. MANDATORY: without it every mint starts on update.sh's no-manifest
#     branch, so any template edit between mint and first self-update would fire
#     a false backup on a pristine instance.
fw_file_checksum() {
  local f="$1"
  [ -f "$f" ] || { printf '%s\n' "d41d8cd98f00b204e9800998ecf8427e-empty"; return; }
  shasum < "$f" 2>/dev/null | awk '{print $1}'
}
FRAMEWORK_MANIFEST="$PROJECT_ROOT/.claude/.dogany-framework.sha"
mkdir -p "$PROJECT_ROOT/.claude"
{
  printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
  printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
  printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
  [ -f "$PROJECT_ROOT/AGENT-OPS.md" ] && \
    printf 'AGENT-OPS.md  %s\n' "$(fw_file_checksum "$PROJECT_ROOT/AGENT-OPS.md")"
  [ -f "$PROJECT_ROOT/RULES.md" ] && \
    printf 'RULES.md  %s\n' "$(fw_file_checksum "$PROJECT_ROOT/RULES.md")"
} > "$FRAMEWORK_MANIFEST"
echo "[mint] wrote $FRAMEWORK_MANIFEST (framework file checksums)"

# 4) write the project .env via env_render (single generator; the shipped
#    .env.example is documentation only, no longer consumed here).
#    Write-if-absent: an existing .env is user state and is kept untouched
#    (all env flags are ignored). --env-overwrite forces an atomic replace
#    (install.sh's confirmed reconfigure path only -- it backs up first).
#    Atomicity + permissions: mktemp in the SAME dir, chmod 600 BEFORE any
#    content lands, then mv -f. The .env is 0600 from the moment it exists
#    and a mid-write failure never leaves a partial .env behind.
ENV_DST="$PROJECT_ROOT/.telegram_bot/.env"
mkdir -p "$PROJECT_ROOT/.telegram_bot"
if [ -f "$ENV_DST" ] && [ "$ENV_OVERWRITE" != "1" ]; then
  echo "[mint] env exists -> keep (env flags ignored)"
else
  TMP_ENV="$(mktemp "${ENV_DST}.tmp.XXXXXX")"
  chmod 600 "$TMP_ENV"
  env_render > "$TMP_ENV"
  mv -f "$TMP_ENV" "$ENV_DST"
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
#    CROSS-REF: token list also at update.sh subst_one (~L1053-1066),
#    pack_install.sh _subst_mint_tokens, and knowledge_selftest.sh G4 --
#    keep all four sites in sync when adding a token.
LEFT="$(grep -rlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__' \
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

# 8) initialize a local git repo for continuity.
#    Local-only: no remote is added (remote/push wiring is owner-gated).
#    Idempotent on re-mint: if .git already exists, skip entirely.
#    The .gitignore was copied with the template (step 1); it excludes secrets
#    (.env / tokens / venv / logs) and runtime state while keeping memories/,
#    config, worklog, and identity markdown.
if [ -d "$PROJECT_ROOT/.git" ]; then
  echo "[mint] .git exists -> skip git init (idempotent)"
  # Idempotent: ensure core.hooksPath is set even on re-mint.
  git -C "$PROJECT_ROOT" config core.hooksPath git-hooks
  echo "[mint] git config core.hooksPath = git-hooks (idempotent)"
elif ! command -v git >/dev/null 2>&1; then
  echo "[mint][WARN] git not found -- skipping repo init" >&2
else
  (
    cd "$PROJECT_ROOT"
    git init -q
    # Wire the tracked git-hooks/ dir as the active hooks path so the
    # pre-commit guard (git-hooks/pre-commit) is active from the first commit.
    # Idempotent: safe to re-run; only sets if not already correct.
    git config core.hooksPath git-hooks
    git add --all
    GIT_AUTHOR_NAME="dogany-mint" \
    GIT_AUTHOR_EMAIL="mint@dogany.local" \
    GIT_COMMITTER_NAME="dogany-mint" \
    GIT_COMMITTER_EMAIL="mint@dogany.local" \
    git commit -q -m "init: mint ${AGENT_NAME} (dogany-agent ${FW_VERSION})"
  )
  echo "[mint] git repo initialized at $PROJECT_ROOT (.git)"
fi

cat <<DONE

[mint] DONE -> $PROJECT_ROOT

Next steps (manual / require approval):
  1. Review the config:  $ENV_DST (written by mint; edit if needed)
  2. Fill identity:      AGENT.md onboarding skeleton; first SessionStart runs
                         onboarding-check.py to set name/emoji/tone.
     NOTE: for specialist agents, seed persona/Role in AGENT.md NOW, before
     proceeding to token/launchd steps. Role seeding is a manual step with no
     persistent record; a crashed or interrupted builder session cannot recover
     it -- the bot wakes generic and runs generic onboarding. Record the
     specialist Role text in AGENT.md first, then continue below.
     (v2 mint-agent path owns first-class role seeding; this note covers v1.)
     CRAFT NOTE: CRAFT activation extends the Role with domain-agent
     orchestration -- specialist agents minted from this same base, coordinated
     by the main agent; a specialist mint rewrites the Role section at creation
     (e.g. "fitness-domain expert: coach the user from lifekit records and
     training principles"). Role is editable on explicit user request, per RULES
     edit rights. Route Role writes through the baseline-editor per the
     agent-crafting phase steps.
  3. (optional) voice:   bridge/venv/bin/pip install faster-whisper
  4. Load launchd:       cp bridge/*.plist routines/*.plist ~/Library/LaunchAgents/
                         then launchctl bootstrap (LIVE op -- get approval first).
DONE
