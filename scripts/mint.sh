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
# skills, service surface -- so it runs with no parent-tree paths.
# mint.sh therefore copies from agents/.template while DEREFERENCING the shared
# symlinks (-L), and additionally bundles rules/ + service/ into the instance.
#
# LIFEKIT EXTRACTION (DGN-803 LS-5): lifekit content (database/lifekit.py,
# schema.sql, migrations, bundle skills/routines, service/lifekit facade) is
# NOT part of the framework anymore -- it ships as the independent lifekit
# pack (repo dogany-lifekit) and lands at KIT ACTIVATION via
# scripts/pack/pack_install.sh (kind=kit path), which also initializes
# lifekit.db from the payload schema. A fresh mint completes with NO lifekit
# files and NO lifekit.db -- that is the NORMAL state of a non-lifekit mint.
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
    --prefix <text>    notify prefix emoji/tag          (default: empty -- notify
                       readers resolve the prefix at READ time via
                       routines/lib/agent_prefix.py: .instance.conf field,
                       else persona '- Emoji:', else no prefix. DGN-828)
    --lang   <en|ko>   working language + .env LOCALE   (default: en)
    --channel <release|dev|staging|main>
                      update channel DECLARED in .instance.conf (default:
                      release -- the safe channel for a non-canary newborn).
                      DGN-1569: the declaration is always written; absence is
                      never a channel. The DOGANY_UPDATE_CHANNEL env var is
                      deliberately NOT inherited (a canary shell must not
                      silently mint canaries) -- only this flag chooses.
                      On a re-mint the existing declared channel is KEPT
                      unless --channel is passed again explicitly.
    --token <token>   DEPRECATED bot-token fallback; prefer DOGANY_BOT_TOKEN
    --owner-id <ids>  ALLOWED_USER_IDS for .env         (default: empty = claim mode)
    --tz <tz>         IANA timezone for .env; empty = TZ line omitted
    --tz-offset-hours <int>  per-agent AGENT_TZ_OFFSET_HOURS override (whole
                      hours, -12..14); default = crew value, else parent
                      (--tz-offset-parent-hours, set by mint_run), else host
    --email <addr>    outbound email address for .env   (default: empty = not connected)
    --email-cc <addr> CC address for outbound email     (default: empty)
    --whisper <model> faster-whisper model for .env     (default: empty = line omitted)
    --models <list>   bridge model whitelist, comma-sep (default: empty = line omitted)
    --print-env       render the .env body to stdout and exit (no --root, no writes)
    --env-overwrite   overwrite an existing .env atomically (install reconfigure only)
    --no-venv         skip building the bridge venv
    --core-only       build venv with runtime deps only (skip test tools)
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
# DGN-828: default EMPTY, never a "[agent]" placeholder. An empty bake means
# notify readers resolve the prefix at read time (routines/lib/agent_prefix.py:
# .instance.conf field -> persona '- Emoji:' filled at onboarding -> no prefix),
# so nothing has to be copied into the manifest for the emoji to take effect.
AGENT_PREFIX=""
AGENT_LANG="en"
# DGN-1569: empty = "no explicit --channel" (distinct from any real channel).
# The final seeded value resolves at manifest time: flag > preserved existing
# declaration > 'release' default. Never read from the environment.
UPDATE_CHANNEL_FLAG=""
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
# The kit readers accept signed whole hours only, so these are deliberately
# not IANA-zone or decimal-hour knobs.  --tz-offset-hours is an explicit
# per-agent override; --tz-offset-parent-hours is mint_run's internal carrier
# for the parent's declaration, which ranks BELOW the crew value (DGN-1704).
TZ_OFFSET_HOURS_OPT=""
TZ_OFFSET_PARENT_OPT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --root)         TARGET="$2"; shift 2 ;;
    --name)         AGENT_NAME="$2"; shift 2 ;;
    --label)        AGENT_LABEL="$2"; shift 2 ;;
    --user)         USER_LABEL="$2"; shift 2 ;;
    --prefix)       AGENT_PREFIX="$2"; shift 2 ;;
    --lang)         AGENT_LANG="$2"; shift 2 ;;
    --channel)      UPDATE_CHANNEL_FLAG="$2"; shift 2 ;;
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
    --tz-offset-hours) TZ_OFFSET_HOURS_OPT="$2"; shift 2 ;;
    --tz-offset-parent-hours) TZ_OFFSET_PARENT_OPT="$2"; shift 2 ;;
    --print-env)    PRINT_ENV=1; shift ;;
    --env-overwrite) ENV_OVERWRITE=1; shift ;;
    --no-venv)      BUILD_VENV=0; shift ;;
    --core-only)    CORE_ONLY=1; shift ;;
    --force)        FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# DGN-1569: validate an explicit --channel IMMEDIATELY, before any write --
# a typo'd channel must never be seeded (self-update would die loud on it
# later; failing here is strictly earlier and cheaper).
case "$UPDATE_CHANNEL_FLAG" in
  ""|release|dev|staging|main) : ;;
  *)
    echo "[mint][FATAL] invalid --channel '${UPDATE_CHANNEL_FLAG}' (valid: release|dev|staging|main)" >&2
    exit 1 ;;
esac

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
  # DGN-167: bridge model whitelist derived from config/agent.conf PLAN= (DGN-590).
  # DGN-813: fable,opus,sonnet,haiku on every plan (fable is listed, not defaulted).
  # Controls /model picker. --models overrides this caller-supplied value below.
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
  # DGN-1552: born WITHOUT these, a new agent cannot continue itself when a
  # turn hits the cap -- it would ship DGN-1523's owner-facing defect (a
  # message telling the owner to press a button that is not sent) as its
  # DEFAULT state. config.py:390 defaults to "0" but every live instance runs
  # 1 (measured 4/4, 2026-09-18), so the birth certificate carries the live
  # consensus, not the code fallback. MAX stays at the code default; leaders
  # raise it per instance.
  printf '# --- Turn timeout: continue on our own rather than asking the owner\n'
  printf '# to press anything. Code default is off; every live agent runs it on.\n'
  printf 'CLAUDE_AUTO_RESUME=1\n'
  printf 'CLAUDE_AUTO_RESUME_MAX=2\n'
  # DGN-1650 / dec-159: NO per-turn budget keys are written here. The 20-minute
  # turn (CLAUDE_PROCESS_TIMEOUT 1200, CLAUDE_TIMEOUT_STOP_GRACE 60) is the
  # canonical bridge/config.py default, so a newborn inherits it; the env keys
  # stay available as per-instance overrides only.
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

# DGN-1646: refuse before creating the target; sibling stores are inert here.
python3 "$REPO_ROOT/scripts/pack/lib/transcript_store.py" "$TARGET" || exit 1

# Resolve target to an absolute path (may not exist yet).
mkdir -p "$TARGET"
PROJECT_ROOT="$(cd "$TARGET" && pwd)"
[ -n "$AGENT_NAME" ]  || AGENT_NAME="$(basename "$PROJECT_ROOT")"
[ -n "$AGENT_LABEL" ] || AGENT_LABEL="$AGENT_NAME"
[ -n "$AGENT_LANG" ]  || AGENT_LANG="en"
HOME_DIR="$HOME"

# Guard: refuse to mint into a non-empty dir unless --force.
# TARGET_WAS_EMPTY is latched here, before anything is written, because it is
# the ONLY point at which "fresh birth" and "re-mint over a live instance" are
# still distinguishable -- every later step creates files of its own. The
# DGN-1566 fresh-mint assertion below reads it: a live instance legitimately
# carries runtime state (its own session-inbox, its own logs), a newborn never
# does, and an assertion that cannot tell the two apart would abort exactly the
# re-mint --force path install.sh's reconfigure depends on.
if [ -n "$(ls -A "$PROJECT_ROOT" 2>/dev/null)" ]; then
  TARGET_WAS_EMPTY=0
  if [ "$FORCE" != "1" ]; then
    echo "ERROR: target not empty: $PROJECT_ROOT (use --force to overwrite)" >&2; exit 1
  fi
else
  TARGET_WAS_EMPTY=1
fi
# Latch this before mint writes the template scaffold.  A key that arrives
# from agents/.template is a seed, not an owner declaration; a key already in
# the target belongs to the owner and is immutable across a re-mint.
if [ -f "$PROJECT_ROOT/config/agent.conf" ]; then
  AGENT_CONF_EXISTED=1
else
  AGENT_CONF_EXISTED=0
fi
# Guard: never mint onto the repo itself.
if [ "$PROJECT_ROOT" = "$REPO_ROOT" ]; then
  echo "ERROR: refusing to mint onto the repo root itself: $REPO_ROOT" >&2; exit 1
fi
# Guard: template must exist.
[ -d "$TEMPLATE" ] || { echo "ERROR: template not found: $TEMPLATE" >&2; exit 1; }

# AGENT_TZ_OFFSET_HOURS is consumed as an integer by kit readers.  Do not
# turn a fractional-hour host zone into a misleading truncated offset: the
# reader-compatible representation does not exist, so stop before minting.
# DGN-1704 crew layer: one resolver (routines/lib/tz_offset.sh) owns the
# order.  For a newborn it is: explicit --tz-offset-hours (a per-agent
# override) > crew value (<crews home>/crew.conf TZ_OFFSET_HOURS) > the
# parent's declaration (mint_run's --tz-offset-parent-hours carrier) > host.
# Mint STAMPS the winner into agent.conf rather than leaving the key absent:
# kit readers outside this repo read AGENT_TZ_OFFSET_HOURS directly, and the
# crew value stays the source because the crew layer is consulted first.
TZ_OFFSET_LIB="$TEMPLATE/routines/lib/tz_offset.sh"
[ -f "$TZ_OFFSET_LIB" ] || { echo "ERROR: tz offset resolver missing ($TZ_OFFSET_LIB)" >&2; exit 1; }
# shellcheck source=../agents/.template/routines/lib/tz_offset.sh
. "$TZ_OFFSET_LIB"

MINT_TZ_OFFSET_HOURS=""
MINT_TZ_OFFSET_SOURCE=""
for _tz_opt in "$TZ_OFFSET_HOURS_OPT" "$TZ_OFFSET_PARENT_OPT"; do
  [ -z "$_tz_opt" ] || tz_offset_valid "$_tz_opt" || {
    echo "ERROR: tz offset '$_tz_opt' must be a whole number from -12 to 14 (kit readers do not parse fractional offsets)" >&2
    exit 1
  }
done
if [ -n "$TZ_OFFSET_HOURS_OPT" ]; then
  MINT_TZ_OFFSET_HOURS="$TZ_OFFSET_HOURS_OPT"; MINT_TZ_OFFSET_SOURCE="explicit override"
else
  MINT_TZ_OFFSET_HOURS="$(tz_offset_crew)"
  if [ -n "$MINT_TZ_OFFSET_HOURS" ]; then
    MINT_TZ_OFFSET_SOURCE="crew value $(tz_offset_crew_conf)"
  elif [ -n "$TZ_OFFSET_PARENT_OPT" ]; then
    MINT_TZ_OFFSET_HOURS="$TZ_OFFSET_PARENT_OPT"; MINT_TZ_OFFSET_SOURCE="parent declaration"
  fi
fi
# The host lookup is deferred: a re-mint that keeps an existing declaration
# must not fail on a fractional host zone it never needed.
_mint_tz_offset_fill_host() {
  [ -n "$MINT_TZ_OFFSET_HOURS" ] && return 0
  MINT_TZ_OFFSET_HOURS="$(tz_offset_host)" || exit $?
  MINT_TZ_OFFSET_SOURCE="host localtime"
}
[ "$AGENT_CONF_EXISTED" = "1" ] || _mint_tz_offset_fill_host

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
#    config/agent.conf + config/secret-patterns.conf are EXCLUDED here and
#    copied write-if-absent below (same contract as .env): a re-mint with
#    --force must never reset the user's language/address settings or
#    owner-identity sweep patterns already customized after onboarding.
#    config/lifekit.conf left the template with the lifekit pack (DGN-803
#    LS-5); the kit install merges it in and install.sh seeds the LIFEKIT=
#    lifecycle key on its own (conf helpers create the file when absent).
#
#    worklog/_DECISIONS.md is EXCLUDED here and copied write-if-absent at
#    step 7c below (DGN-817): a re-mint with --force must never overwrite a
#    live instance's pending-decision ledger.
#
#    CONSTRAINT (bundle dormancy): bundle skills live as REAL dirs in
#    .claude/skills-bundle/ (framework members ship in the template; kit
#    members arrive via pack_install) and are activated by an instance-local
#    symlink .claude/skills/<id> -> ../skills-bundle/<id>, created ONLY
#    post-mint by the dogany-lifekit-setup skill. NEVER pre-place such
#    symlinks in the template: rsync -aL would dereference them into
#    permanent real dirs and break the off-toggle. Instance-created symlinks
#    survive a re-mint because this rsync has no --delete and the template
#    has no such paths.
# DGN-808 (defect2 hardening): refuse to mint if the template tree contains a
# .dogany-private-backup marker anywhere in its subtree. A marker in the
# template would propagate into every minted instance and auto-exempt them from
# owner-PII sweep -- a framework-wide hole. Fail-closed: abort the mint.
if find "$TEMPLATE" -name '.dogany-private-backup' -print -quit 2>/dev/null | grep -q .; then
  echo "[mint] FATAL: template tree contains .dogany-private-backup -- this marker" >&2
  echo "  must never be present in the template (DGN-808). Remove it first." >&2
  exit 1
fi

# DGN-1141 stage 6: CONSTITUTION.md / CONTRACT.md / RULES.md are excluded. The template
# still SHIPS them (as symlinks into repo rules/) because update.sh section 3k6
# needs a source for instances that have not been relaid out yet -- but a fresh
# mint lands directly in the 2.0 layout, where those two files were merged into
# rules/hot.framework.md. Copying them would put two disagreeing copies of the
# constitution on a brand-new instance and make update.sh's section 3k8 retire
# them on the very first self-update (migration residue on a tree that never
# migrated).
#
# RULES.md is the SAME argument one file further (DGN-1141 stage 8, M11
# MAJOR-2). It is the v1 constitution, superseded by rules/hot.framework.md,
# and update.sh's stated reason for keeping it -- "a PRE-2.0 instance keeps its
# real RULES.md v1 file ... rollback stays a one-liner" -- does not reach a
# tree that was never pre-2.0. On a fresh mint it is the largest of the three:
# 7.8K of routing rules naming USER.md / AGENT.md, files this layout does not
# have, sitting in the tree an agent greps. Outside the loader chain, so not
# hot pollution -- just a second, disagreeing constitution. Section 3k in
# update.sh is presence-gated for the same reason, so it stays absent.
#
# DGN-1566: RUNTIME STATE. The exclude list below used to cover VCS, build and
# user-state files only, and that was the whole defect. `rsync -aL` DOES NOT
# READ .gitignore -- so every path the instance .gitignore declares runtime
# (agents/.template/.gitignore: "Secrets and runtime state are NEVER
# committed") was invisible to git and fully visible to the copy. Measured on
# canonical v2.6.0-dev.17: one `DGN_TEST_LANE=fast bash git-hooks/run-tests.sh`
# run left 12 session-inbox outbound-records, an outbound-ledger.jsonl and a
# push-guard-override.log inside agents/.template/.telegram_bot/, and mint
# handed all of it to the next newborn -- which the bridge then injects as if
# it were that agent's own history (108 items had accumulated by the time
# DGN-1155's case [E] measured it).
#
# So the rule is not "name the three files that leaked". It is: mint must never
# copy runtime state REGARDLESS of who dirtied the template. Three shapes,
# below, and every pattern in them is a transcription of a line the instance
# .gitignore already declares runtime -- the exclude list is not a new policy,
# it is the policy rsync could not see. Each was checked against
# `git ls-files agents/.template`: NONE has a tracked match, so none of them
# can drop a file the template legitimately ships.
#
# TB_KEEP / MEM_KEEP are ALLOW-lists, not deny-lists, and that is deliberate.
# `.telegram_bot/` and `memories/*` are the two directories the instance
# .gitignore excludes WHOLESALE, with named exceptions force-added back
# (.telegram_bot/.env.example; memories/MEMORY.md + inbox.md -- verified: those
# are the only tracked files under either). A deny-list over a directory that
# is definitionally runtime is how DGN-1566 happened: the list existed, it just
# did not happen to name session-inbox. An allow-list is the only shape that
# survives the NEXT runtime filename nobody has invented yet.
#
# The known cost of an allow-list is that it fails SILENTLY when the template
# legitimately grows a file there and nobody updates TB_KEEP/MEM_KEEP. That
# cost is paid off by the preflight notice below, which prints every path it is
# dropping: a forgotten allow-list entry shows up in the mint log as a skipped
# file instead of as a mystery absence on the newborn.
TB_KEEP=(.env.example)
MEM_KEEP=(MEMORY.md inbox.md)
# By-NAME runtime state, excluded at any depth. rsync patterns with no slash
# match the basename at every level, which is what "an instance writes these
# wherever it runs" needs. Per-line reasoning:
#   .env                     the instance's live secrets. A dev's own bot token
#                            in the template would be copied into the newborn
#                            and then KEPT by step 4's write-if-absent .env --
#                            the single most severe member of this class.
#   .sweep-identity          owner-PII sweep override. A template copy would
#                            auto-exempt every instance from the owner sweep:
#                            the exact DGN-808 hole, one filename over.
#   sessions.json            bridge session-id bookkeeping (another agent's).
#   health*.json             bridge health snapshots.
#   *.log / *.pid            logs and pidfiles. push-guard-override.log was
#                            481K of one before it was archived out.
#   logs / runtime           the two runtime DIRS the .gitignore names
#                            (`**/logs/`, `**/runtime/`). Generic words, so
#                            worth stating: mint mkdirs .telegram_bot/logs
#                            itself at step 4, and `runtime/` is the retired
#                            pre-.telegram_bot env path push.sh still falls
#                            back to -- neither is ever template content.
#   .idrill-arm              idrill per-session arm state. Measured residue:
#                            the fast lane leaves files/program/.idrill-arm/.
#   .classify_inbox_last     inbox-classifier cursor; .classify_retry_pending /
#                            .classify_reindex_pending its retry state (DGN-1873).
#   .reminders               routines/.reminders/ runtime queue.
#   .last-skill.json         .claude runtime markers. .token-gate-approved is
#   .token-gate-approved     an APPROVAL: shipping one pre-approves a gate on
#   .skill-landing-declared  an agent that never passed it.
#   .pytest_cache / .venv    build cruft the existing list missed (its
#                            __pycache__ / venv siblings are already covered).
#   *.bak*                   DGN-868's four backup shapes in one pattern; the
#                            existing list covers only `*.bak.*`.
RUNTIME_NAMES=(
  .env .sweep-identity sessions.json 'health*.json'
  '*.log' '*.pid' logs runtime
  .idrill-arm .classify_inbox_last .classify_retry_pending .classify_reindex_pending
  .reminders
  .last-skill.json .token-gate-approved .skill-landing-declared
  .pytest_cache .venv '*.bak*'
)
# ANCHORED scratch paths (root-relative). Anchored rather than by-name because
# `tmp` and `_archive` are words a legitimate template file could carry
# somewhere else; at these exact three paths they are scratch by declaration.
RUNTIME_PATHS=(files/_archive files/tmp .claude/skill-backups)

# _runtime_residue <root> -- print every runtime-state path under <root>,
# root-relative, one per line. ONE definition, read by BOTH the preflight
# notice (against the template) and the fresh-mint assertion (against the new
# instance), so the two can never drift from each other or from the excludes.
_runtime_residue() {
  local root="$1" d p n allowed keep
  for d in .telegram_bot memories; do
    if [ "$d" = ".telegram_bot" ]; then keep="${TB_KEEP[*]}"; else keep="${MEM_KEEP[*]}"; fi
    allowed=" $keep "
    [ -d "$root/$d" ] || continue
    for p in "$root/$d"/* "$root/$d"/.[!.]*; do
      if [ -e "$p" ] || [ -L "$p" ]; then
        n="$(basename "$p")"
        case "$allowed" in *" $n "*) continue ;; esac
        printf '%s/%s\n' "$d" "$n"
      fi
    done
  done
  # -prune twice, for two different reasons: .git/venv/__pycache__ are already
  # excluded wholesale above, so a hit inside one of them is noise (a built
  # venv carries thousands of files this pattern set would match); and a
  # matching directory is reported as itself rather than as every file under
  # it, so "skip .telegram_bot/session-inbox" stays one line instead of 108.
  for p in "${RUNTIME_NAMES[@]}"; do
    find "$root" \
         \( -name .git -o -name venv -o -name __pycache__ \) -prune -o \
         -name "$p" -prune -print 2>/dev/null | sed "s#^$root/##" || true
  done
  for p in "${RUNTIME_PATHS[@]}"; do
    if [ -e "$root/$p" ]; then printf '%s\n' "$p"; fi
  done
  return 0
}

# Build the rsync filter argv from the three declarations above. The two
# --include lines MUST precede their --exclude: rsync applies the FIRST
# matching rule, so an allow-list entry listed after its directory wildcard
# would be dead text.
RUNTIME_EXCLUDES=()
for _n in ${TB_KEEP[@]+"${TB_KEEP[@]}"};  do RUNTIME_EXCLUDES+=(--include "/.telegram_bot/$_n"); done
RUNTIME_EXCLUDES+=(--exclude '/.telegram_bot/*')
for _n in ${MEM_KEEP[@]+"${MEM_KEEP[@]}"}; do RUNTIME_EXCLUDES+=(--include "/memories/$_n"); done
RUNTIME_EXCLUDES+=(--exclude '/memories/*')
for _n in "${RUNTIME_NAMES[@]}"; do RUNTIME_EXCLUDES+=(--exclude "$_n"); done
for _n in "${RUNTIME_PATHS[@]}"; do RUNTIME_EXCLUDES+=(--exclude "/$_n"); done

# PREFLIGHT NOTICE, not a refusal -- and the difference is the point.
# DGN-808's .dogany-private-backup guard a few lines up aborts the mint,
# because that marker has NO legitimate reason to exist in the template and no
# copy-exclude can undo what its semantics grant. Runtime drops are the
# opposite: they appear in a canonical working tree as the ORDINARY side effect
# of the repo running its own test suite (git-hooks/pre-push runs the fast
# lane, so `git push` alone is enough), and the estate's real mint path IS that
# working tree. Failing closed here would put a manual-cleanup wall in front of
# every canonical mint while adding no safety the excludes do not already give.
# So this names what it is dropping and proceeds. It is a receipt: it keeps the
# test-hygiene half of DGN-1566 visible instead of letting the exclude list
# silently absorb it forever.
_RESIDUE="$(_runtime_residue "$TEMPLATE" | LC_ALL=C sort -u)"
if [ -n "$_RESIDUE" ]; then
  echo "[mint] NOTICE: template carries runtime state -- NOT copied (DGN-1566):"
  printf '%s\n' "$_RESIDUE" | sed 's/^/[mint]   skip /'
  echo "[mint]   (a suite wrote into agents/.template/ in place; harmless to this"
  echo "[mint]    mint, still a test-hygiene defect worth a ticket.)"
fi

rsync -aL \
  "${RUNTIME_EXCLUDES[@]}" \
  --exclude '.git' \
  --exclude '/CONSTITUTION.md' \
  --exclude '/CONTRACT.md' \
  --exclude '/RULES.md' \
  --exclude '/AGENT.md' \
  --exclude '/PROFILE.md' \
  --exclude '/DISCIPLINE.md' \
  --exclude '/USER.md' \
  --exclude '/rules/hot.custom.md' \
  --exclude '/identity/hot.custom.agent.md' \
  --exclude '/identity/hot.custom.owner.md' \
  --exclude '/.claude/settings.local.json' \
  --exclude 'bridge/venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.bak.*' \
  --exclude '.DS_Store' \
  --exclude 'memory-engine/state.db' \
  --exclude '*.db' \
  --exclude 'config/agent.conf' \
  --exclude 'config/secret-patterns.conf' \
  --exclude 'worklog/_DECISIONS.md' \
  "$TEMPLATE/" "$PROJECT_ROOT/"

# DGN-1566, fail-closed half: assert the PROPERTY the ticket is actually about
# -- "a newborn carries no runtime state" -- rather than trusting that the
# exclude list above is complete. A long filter list's failure mode is a
# missing line, and a missing line is silent: the file just arrives. This is
# the only place that can catch it, and it catches it at birth.
#
# Checked HERE, immediately after the copy, because this is the last moment the
# instance tree contains nothing but template content -- step 4 writes the .env
# and mkdirs .telegram_bot/logs, both of which are runtime by name.
# FRESH MINTS ONLY: a re-mint --force runs over a LIVE instance whose own
# session-inbox, logs and .env are legitimate state that rsync (no --delete)
# correctly leaves alone. Asserting there would abort install.sh's reconfigure
# path on a healthy agent, which is why TARGET_WAS_EMPTY is latched before the
# first write.
if [ "$TARGET_WAS_EMPTY" = "1" ]; then
  _BORN_RESIDUE="$(_runtime_residue "$PROJECT_ROOT" | LC_ALL=C sort -u)"
  if [ -n "$_BORN_RESIDUE" ]; then
    echo "[mint] FATAL: the fresh instance carries RUNTIME STATE after the template" >&2
    echo "  copy (DGN-1566). A newborn must never inherit another instance's runtime" >&2
    echo "  files -- the bridge injects session-inbox items as if they were its own" >&2
    echo "  history. This means the exclude list above has a hole, not that the" >&2
    echo "  template is merely dirty. Offending paths, instance-relative:" >&2
    printf '%s\n' "$_BORN_RESIDUE" | sed 's/^/    /' >&2
    exit 1
  fi
fi

# 1a) identity markdown: keep-if-present (re-mint must NEVER reset an agent's
#     identity -- Role, name, accreted Workflows). Three layouts are accepted,
#     newest first, and the loop below scaffolds ONLY the newest one:
#       identity/hot.custom.agent.md  2.0 LAYOUT (DGN-1141 stage 6) -- the one
#                                     the template ships and mint creates
#       PROFILE.md                    2.0 flat root (DGN-773 T3) -- kept when
#                                     present, never manufactured
#       AGENT.md                      pre-2.0 -- kept when present, ditto
#     identity/hot.custom.owner.md holds user facts (was USER.md pre-relayout).
#     rules/hot.custom.md is the instance's own discipline overlay (was
#     DISCIPLINE.md). It IS scaffolded, in section 1a2 below -- the hub chains
#     it unconditionally as of DGN-1141 stage 8, so an unseeded overlay makes
#     "MISSING" the normal loader verdict and destroys the signal. update.sh's
#     relayout still overrides the seed with a locally-edited DISCIPLINE.md
#     when one exists. Every one of these names is excluded from the rsync
#     above, so a re-mint --force cannot reset them.
#
#     COPY FLAG IS LATCHED: "cp -Pp", never "cp -p" (DGN-773 안 C). The
#     template used to ship AGENT.md/PROFILE.md/USER.md as compat SYMLINKS,
#     and "cp -p" DEREFERENCES a link, laying down a second REAL file -- which
#     is exactly onboarding-check.py resolve_persona_md()'s collision
#     condition and lock-spec R5's pack_install preflight die condition, i.e.
#     every new mint would be born broken. DGN-1141 stage 6 retired those
#     three template symlinks (identity/ files are real files now), so nothing
#     in the loop dereferences today -- the flag stays latched anyway because
#     "-P" is no-dereference on BSD *and* GNU and the next compat link would
#     silently reintroduce the defect. Do NOT "fix" this to "cp -Rp" (GNU
#     coreutils does not make -R imply -P). Regression cover: T3 selftest [3g].
#
#     DISCIPLINE.md stays in the exclude list (DGN-773 T3, gap found at
#     T2-consistency review): the file is retired from the template, but a
#     pre-relayout instance still carries a locally-edited one and a re-mint
#     --force must not touch it before update.sh migrates it.
for idmd in identity/hot.custom.agent.md identity/hot.custom.owner.md; do
  # DGN-773 remint guard: a pre-2.0 instance carries identity in a REAL
  # (non-symlink) AGENT.md and has no PROFILE.md yet -- that is "not migrated",
  # not "brand new". Scaffolding an empty identity file here would outrank the
  # real AGENT.md in resolve_persona_md() and re-trigger onboarding on a
  # fully-configured agent. Leave the ambiguity for update.sh step M/N to
  # resolve instead of manufacturing it here.
  if [ "$idmd" = "identity/hot.custom.agent.md" ] \
     && [ ! -f "$PROJECT_ROOT/PROFILE.md" ] \
     && [ -f "$PROJECT_ROOT/AGENT.md" ] && [ ! -L "$PROJECT_ROOT/AGENT.md" ]; then
    echo "[mint] $idmd absent, AGENT.md is a real file -> pre-2.0 instance, skip scaffold (update migrates)"
    continue
  fi
  # Same manufacturing-ambiguity guard one layer up (DGN-1141): a pre-relayout
  # 2.0 instance carries its live persona / user facts in a REAL root
  # PROFILE.md / USER.md. Laying down an identity/ scaffold next to it would
  # outrank the real file in resolve_persona_md() and re-trigger onboarding on
  # a configured agent. Leave it for the update.sh relayout (step N).
  if [ "$idmd" = "identity/hot.custom.agent.md" ] \
     && [ -f "$PROJECT_ROOT/PROFILE.md" ] && [ ! -L "$PROJECT_ROOT/PROFILE.md" ]; then
    echo "[mint] real root PROFILE.md present -> pre-relayout instance, skip identity/ scaffold (update migrates)"
    continue
  fi
  if [ "$idmd" = "identity/hot.custom.owner.md" ] \
     && [ -f "$PROJECT_ROOT/USER.md" ] && [ ! -L "$PROJECT_ROOT/USER.md" ]; then
    echo "[mint] real root USER.md present -> pre-relayout instance, skip identity/ scaffold (update migrates)"
    continue
  fi
  if [ ! -f "$PROJECT_ROOT/$idmd" ] && [ -f "$TEMPLATE/$idmd" ]; then
    mkdir -p "$PROJECT_ROOT/$(dirname "$idmd")"
    cp -Pp "$TEMPLATE/$idmd" "$PROJECT_ROOT/$idmd"
    echo "[mint] wrote $idmd (scaffold)"
  elif [ -f "$PROJECT_ROOT/$idmd" ]; then
    echo "[mint] $idmd exists -> keep (identity preserved)"
  fi
done

# 1a2) the instance-owned operating-gate overlay. Excluded from the rsync above
#     (a re-mint must never reset it) and scaffolded here instead, exactly like
#     the identity pair. agents/.template/AGENTS.md chains @rules/hot.custom.md
#     unconditionally, so seeding it is what keeps a MISSING verdict from the
#     loader hook meaningful: without the seed every no-divergence instance
#     would report a permanent MISSING and a real one would look identical
#     (DGN-1141 stage 8, M11 FATAL-1). This reverses section 3.4's "never
#     pre-create an empty overlay", which was decided while the overlay was
#     outside the loader chain.
#
#     NOT scaffolded on a pre-relayout tree: a real root DISCIPLINE.md is the
#     live overlay content and update.sh step N moves it into this exact path.
#     Manufacturing an empty file here would make that move hit its
#     "both exist -- move deferred" branch and strand the owner's local gates
#     at the root forever. Same manufacturing-ambiguity rule as the identity
#     scaffolds above.
if [ ! -f "$PROJECT_ROOT/rules/hot.custom.md" ] && [ -f "$TEMPLATE/rules/hot.custom.md" ]; then
  if [ -f "$PROJECT_ROOT/DISCIPLINE.md" ] && [ ! -L "$PROJECT_ROOT/DISCIPLINE.md" ]; then
    echo "[mint] real root DISCIPLINE.md present -> pre-relayout instance, skip overlay scaffold (update migrates)"
  else
    mkdir -p "$PROJECT_ROOT/rules"
    cp -Pp "$TEMPLATE/rules/hot.custom.md" "$PROJECT_ROOT/rules/hot.custom.md"
    echo "[mint] wrote rules/hot.custom.md (empty overlay scaffold)"
  fi
elif [ -f "$PROJECT_ROOT/rules/hot.custom.md" ]; then
  echo "[mint] rules/hot.custom.md exists -> keep (instance-owned)"
fi

# 1a3) the instance-owned HOOK surface: .claude/settings.local.json.
#     Excluded from the rsync above and scaffolded here for the same reason as
#     rules/hot.custom.md: a re-mint --force must never reset it. It is the
#     file pack hooks are wired into (pack_install.sh _hooks_wire, DGN-1250 /
#     DGN-1665) precisely BECAUSE update.sh regenerates its sibling
#     .claude/settings.json wholesale on every framework update -- a pack hook
#     written there is erased with no error anywhere (DGN-359 hooks split, six
#     measured recurrences in DGN-1250). rsync would carry the template copy
#     over a live instance's wired hooks on a re-mint, which is the same
#     clobber one door over, so the template file is the SEED and this block
#     is the only thing that lays it down.
#     The installer also creates this file when it is missing (create-if-
#     missing), so an instance minted before this block is not stranded; the
#     seed exists so a newborn is not depending on that recovery path.
if [ ! -f "$PROJECT_ROOT/.claude/settings.local.json" ]; then
  mkdir -p "$PROJECT_ROOT/.claude"
  if [ -f "$TEMPLATE/.claude/settings.local.json" ]; then
    cp -Pp "$TEMPLATE/.claude/settings.local.json" "$PROJECT_ROOT/.claude/settings.local.json"
  else
    printf '{}\n' > "$PROJECT_ROOT/.claude/settings.local.json"
  fi
  echo "[mint] wrote .claude/settings.local.json (instance-owned hook surface scaffold)"
else
  echo "[mint] .claude/settings.local.json exists -> keep (instance-owned)"
fi

# 1b) per-instance conf state: scaffold only if absent (idempotent re-mint).
#     lifekit.conf is no longer scaffolded here: it moved to the lifekit pack
#     payload (DGN-803 LS-5) and is merge-key installed at kit activation.
for conf in agent.conf secret-patterns.conf; do
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

# The template ships the key commented out (DGN-1704: a literal 0 there
# counted as declared UTC).  A fresh mint always stamps the resolved value
# (replacing any seed line); a re-mint stamps only when the instance has no
# declaration yet -- a present value may be an explicit owner choice,
# including an intentional 0, and is never rewritten.
_tz_declared=0
if [ -f "$PROJECT_ROOT/config/agent.conf" ] && grep -q '^AGENT_TZ_OFFSET_HOURS=' "$PROJECT_ROOT/config/agent.conf"; then
  _tz_declared=1
fi
if [ -f "$PROJECT_ROOT/config/agent.conf" ] && { [ "$AGENT_CONF_EXISTED" = "0" ] || [ "$_tz_declared" = "0" ]; }; then
  _mint_tz_offset_fill_host
  if [ "$_tz_declared" = "1" ]; then
    sed -i.tmp "s/^AGENT_TZ_OFFSET_HOURS=.*/AGENT_TZ_OFFSET_HOURS=${MINT_TZ_OFFSET_HOURS}/" "$PROJECT_ROOT/config/agent.conf"
    rm -f "$PROJECT_ROOT/config/agent.conf.tmp"
  else
    printf '\nAGENT_TZ_OFFSET_HOURS=%s\n' "$MINT_TZ_OFFSET_HOURS" >> "$PROJECT_ROOT/config/agent.conf"
  fi
  echo "[mint] stamped AGENT_TZ_OFFSET_HOURS=$MINT_TZ_OFFSET_HOURS ($MINT_TZ_OFFSET_SOURCE)"
elif [ "$AGENT_CONF_EXISTED" = "1" ]; then
  echo "[mint] AGENT_TZ_OFFSET_HOURS exists -> keep (owner declaration)"
fi

# 1b'') DGN-1379-SPEC-r2-remaining-three S2: seed config/handoff.capabilities
#       -- this instance's declaration of what it does with each incoming
#       handoff type, consumed by routines/lib/handoff/engine.py consume.
#       No instance-unique value lives in this file (no substitution token),
#       so it is written here rather than shipped in the template (the
#       template's rsync pass has no --ignore-existing and would clobber an
#       owner-edited declaration on every re-mint). Write-if-absent, same
#       idempotent shape as the agent.conf/secret-patterns.conf scaffold
#       above: a re-mint must never overwrite an owner's declaration.
#       Only the builtin:notice-append mode ships here -- exec/notify are not
#       implemented by the consume layer yet (DGN-1379-SPEC-slug-consume-
#       layer scope); seeding a mode nothing runs would lie to the newborn.
if [ ! -f "$PROJECT_ROOT/config/handoff.capabilities" ]; then
  mkdir -p "$PROJECT_ROOT/config"
  cat > "$PROJECT_ROOT/config/handoff.capabilities" <<'CAPEOF'
# handoff capabilities -- this instance's declaration of what it does with
# each incoming message type. Consumed by routines/lib/handoff/engine.py
# consume (DGN-1379). Absent file == empty table == everything is left in
# the inbox; a malformed file aborts consumption (fail-closed).
decision.notice builtin:notice-append idem=ulid
CAPEOF
  echo "[mint] wrote config/handoff.capabilities (scaffold)"
else
  echo "[mint] config/handoff.capabilities exists -> keep (idempotent)"
fi

# 1b') DGN-635 FIX-3 / DGN-1488: stamp the instance's immutable handoff slug
#      at mint time. The slug IS the mailbox address (~/.dogany/handoff/<slug>/,
#      no registry/resolve layer -- DGN-635 spec v1 LOCKED), so it must be
#      recorded once at birth, not re-derived per caller. Write-if-absent: a
#      re-mint keeps the born slug (the address must never drift on a rename).
#      Lowercased: slug grammar is lowercase (mint_run.sh ^[a-z][a-z0-9-]+$)
#      and routines/lib/handoff/engine.py own_slug() lowercases its basename
#      fallback identically. Consumers: engine.py own_slug(),
#      routines/handoff-provision.sh, routines/skill-feedback-gate.py.
#      (The TEMPLATE ships no SLUG= line on purpose -- the value is per
#      instance; this stamp is the mint-time counterpart, not a template seed.)
AGENT_SLUG="$(printf '%s' "$AGENT_NAME" | tr '[:upper:]' '[:lower:]')"
if [ -f "$PROJECT_ROOT/config/agent.conf" ] \
   && ! grep -q '^SLUG=' "$PROJECT_ROOT/config/agent.conf"; then
  {
    printf '\n# Immutable handoff slug -- this instance'\''s mailbox address under\n'
    printf '# ~/.dogany/handoff/<slug>/ (DGN-635: the address IS the rule; no registry).\n'
    printf '# Stamped once by mint.sh; never hand-edit after mint.\n'
    printf 'SLUG=%s\n' "$AGENT_SLUG"
  } >> "$PROJECT_ROOT/config/agent.conf"
  echo "[mint] stamped SLUG=$AGENT_SLUG into config/agent.conf"
else
  echo "[mint] SLUG stamp skipped (line present or conf missing) -> keep"
fi

# 1c) bundle the hoisted shared roots the instance needs to be self-contained:
#     - (USER.md scaffold ships inside the template; RULES.md already dereferenced),
#     - database/ dir is created EMPTY: lifekit content (schema.sql, lifekit.py,
#       CLI, selectors) moved to the lifekit pack (DGN-803 LS-5) and lands here
#       only at kit activation via pack_install.sh. Framework consumers
#       (backup-data.sh, remind.sh, memory-engine body-state) treat the empty
#       dir as the NORMAL non-lifekit state (silent no-op).
#     - service/ surface (service/lifekit/ facade also moved to the pack; what
#       remains -- e.g. service/mailer -- is framework-owned).
mkdir -p "$PROJECT_ROOT/database"
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
manifest_mint_owns_key() {
  case "$1" in
    DOGANY_AGENT_NAME|DOGANY_AGENT_LABEL|DOGANY_USER_LABEL|DOGANY_AGENT_PREFIX|\
    DOGANY_FW_VERSION|DOGANY_FW_REPO_SHA|DOGANY_FW_TAG|DOGANY_REPO_ROOT|\
    DOGANY_MINTED_AT|DOGANY_AGENT_CLASS|DOGANY_PACKS|DOGANY_UPDATE_CHANNEL) return 0 ;;
    *) return 1 ;;
  esac
}

# A recovery re-mint refreshes mint-owned facts, but .instance.conf also holds
# update landing and extension state. Preserve every valid non-mint-owned
# KEY=value record, so a future update record is not lost by this whole-file
# render merely because mint.sh has not learned its name yet.
#
# Render and validate a same-directory temporary file before mv. Restoring after
# replacement leaves a window where recovery state is absent.
manifest_preserved_state() {
  local manifest="$1" record key
  [ -f "$manifest" ] || return 0
  while IFS= read -r record || [ -n "$record" ]; do
    key="${record%%=*}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    manifest_mint_owns_key "$key" && continue
    printf '%s\n' "$record"
  done < "$manifest"
}

FW_VERSION="unknown"
[ -f "$REPO_ROOT/VERSION" ] && FW_VERSION="$(head -n1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
# DGN-1475: DOGANY_REPO_ROOT is a PATH, not a ref -- whatever is checked out
# there at mint time is what mint_run.sh/pack_install.sh actually execute for
# the life of the instance, and that checkout can move (branch switch, new
# commits) with nothing in the instance recording it. Stamp the exact commit
# so the question "what framework code produced/last touched this instance"
# is answerable later from .instance.conf alone, independent of DOGANY_FW_TAG
# (which only moves on an explicit self-update tag landing, DGN-626 W1) and
# of DOGANY_FW_VERSION (a VERSION-file semver that can lag many commits
# behind on an unreleased branch). "unknown" when REPO_ROOT is not a git
# checkout (matches the FW_VERSION "unknown" fallback above).
FW_REPO_SHA="unknown"
git -C "$REPO_ROOT" rev-parse HEAD >/dev/null 2>&1 \
  && FW_REPO_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
# DGN-1605 (a): also stamp DOGANY_FW_TAG for consistency with FW_VERSION/
# FW_REPO_SHA above -- content_drift_guard (DGN-1118, update.sh ~2107) reads
# ONLY DOGANY_FW_TAG to resolve its baseline, and an absent field forces it
# into strict mode, which false-blocks a freshly minted instance's very
# first framework update (its FW_REPO_SHA IS the exact answer, but the
# reader never looks there).
#
# keep-if-present, same contract as SAVED_PREFIX/SAVED_USER_LABEL/
# SAVED_CHANNEL below: FW_TAG only moves on an explicit self-update tag
# landing (DGN-626 W1), so once update.sh has stamped a real landed tag a
# re-mint must never regress it back to whatever this REPO_ROOT happens to
# have checked out today (that would silently rewrite update.sh's landing
# record with an unrelated fact -- worse than the absent field this ticket
# fixes). A fresh mint (no prior value) computes the tag pointing AT
# FW_REPO_SHA itself (commit -> tag, never tag -> commit): `git tag
# --points-at` peels annotated tags to their target commit internally, so
# it does not fall into the `git rev-parse <tag>` trap (that returns the
# tag OBJECT, not the commit, and produced a false "the release tag moved"
# diagnosis earlier today). If more than one tag points at the same commit,
# sort -V and take the highest for a deterministic pick. "unknown" fallback
# matches FW_VERSION/FW_REPO_SHA above (REPO_ROOT not a git checkout, or the
# checked-out commit is untagged -- e.g. mid-branch on an unreleased line).
SAVED_FW_TAG="$(grep -E '^DOGANY_FW_TAG=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [ -n "$SAVED_FW_TAG" ]; then
  FW_TAG="$SAVED_FW_TAG"
else
  FW_TAG="unknown"
  if [ "$FW_REPO_SHA" != "unknown" ]; then
    # Multiple tags can point at one commit -- typically at a release cut, where
    # the stable tag (v2.6.0) and the dev tag it was cut from (v2.6.0-dev.24)
    # share a commit. `sort -V | tail -1` picks the WRONG one there: measured on
    # this host (BSD sort), `printf 'v2.6.0-dev.24\nv2.6.0\n' | sort -V | tail -1`
    # yields v2.6.0-dev.24, but semver ranks a pre-release BELOW its stable
    # release, and an instance stamped with the dev tag would resolve its drift
    # baseline against a pre-release it does not actually follow. So: prefer a
    # tag with no pre-release hyphen, and only fall back to version order when
    # every candidate is a pre-release.
    _fw_tags_at="$(git -C "$REPO_ROOT" tag --points-at "$FW_REPO_SHA" 2>/dev/null)"
    if [ -n "$_fw_tags_at" ]; then
      # grep -v exits 1 when every candidate is a pre-release (nothing left
      # after the filter). Under `set -o pipefail` that rc propagates out of
      # the assignment and kills the mint -- measured 2026-09-20: this exact
      # line aborted every mint while the only tag at HEAD was v2.6.0-dev.25.
      # Absorb it: "no stable tag here" is a normal outcome, not an error.
      _fw_tag_candidate="$(printf '%s\n' "$_fw_tags_at" | { grep -v -- '-' || true; } | sort -V | tail -n1)"
      [ -z "$_fw_tag_candidate" ] \
        && _fw_tag_candidate="$(printf '%s\n' "$_fw_tags_at" | sort -V | tail -n1)"
      [ -n "$_fw_tag_candidate" ] && FW_TAG="$_fw_tag_candidate"
    fi
    unset _fw_tags_at _fw_tag_candidate
  fi
fi
# First-mint date: preserved across re-mints (the instance's birthday, UTC).
MINTED_AT="$(grep -E '^DOGANY_MINTED_AT=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
MINTED_AT="${MINTED_AT:-$(date -u +%Y-%m-%d)}"
# Agent prefix: preserved across re-mints (the instance's baked notify prefix).
# Fresh mint -> the value of --prefix (default EMPTY: readers resolve at read
# time via routines/lib/agent_prefix.py, DGN-828). update.sh reads this field
# to re-substitute __AGENT_PREFIX__ after a framework refresh.
SAVED_PREFIX="$(grep -E '^DOGANY_AGENT_PREFIX=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
# A legacy placeholder sentinel in the old manifest ("[agent]" mint default /
# unsubstituted __TOKEN__) is NOT preserved -- it IS the DGN-828 bug; dropping
# it lets read-time resolution take over. Same scrub for an explicitly passed
# placeholder --prefix.
case "$SAVED_PREFIX" in "[agent]"|__*__) SAVED_PREFIX="" ;; esac
case "$AGENT_PREFIX" in "[agent]"|__*__) AGENT_PREFIX="" ;; esac
# --prefix flag wins on a new mint; on re-mint keep the existing baked value
# unless the operator explicitly passes --prefix again (AGENT_PREFIX != default).
if [ -n "$SAVED_PREFIX" ] && [ -z "$AGENT_PREFIX" ]; then
  AGENT_PREFIX="$SAVED_PREFIX"
fi
# DGN-1464: user label (owner honorific) is preserved across re-mints, same
# keep-if-present contract as PREFIX above. It is a PER-AGENT axis learned in
# onboarding (Q3), never re-derived by mint -- 'you' is mint.sh's own neutral
# seed value (line ~91) AND the ONLY value mint_run.sh's caller path ever
# passes (it deliberately never inherits a parent's honorific, same ticket),
# so 'you' is the sentinel that means "no explicit non-default --user given",
# exactly like PREFIX's "[agent]"/__*__ placeholder sentinel above. Without
# this, a recovery re-mint of a live instance that already learned its
# honorific would silently reset it back to 'you' on every re-mint.
SAVED_USER_LABEL="$(grep -E '^DOGANY_USER_LABEL=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [ "$USER_LABEL" = "you" ] && [ -n "$SAVED_USER_LABEL" ]; then
  USER_LABEL="$SAVED_USER_LABEL"
fi
# DGN-227 MAJOR-4 (A4/P4): agent class + pack-consumption record are preserved
# across re-mints (keep-if-present, same contract as MINTED_AT/PREFIX).
# mint.sh itself does not author these -- install.sh (dgn227_postmint) stamps
# DOGANY_AGENT_CLASS and pack_install upserts DOGANY_PACKS -- but a re-mint /
# recover rewrites .instance.conf wholesale, so without this a domain instance
# would silently reclassify to main (P13 default) and its pack record (which
# --upgrade version comparison depends on) would be wiped. Re-emit only when a
# prior value exists so a fresh direct mint keeps its current lean manifest.
SAVED_CLASS="$(grep -E '^DOGANY_AGENT_CLASS=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
SAVED_PACKS="$(grep -E '^DOGANY_PACKS=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
# DGN-1569: the update channel is DECLARED at birth, never implied by absence
# (an unset value and a deliberate 'release' were indistinguishable on disk,
# so a newborn silently followed stable -- the 0-done-vs-not-run shape).
# Resolution: explicit --channel wins (fresh mint AND re-mint) > the existing
# declaration is KEPT (a recovery re-mint must never reset a channel the
# operator changed after birth -- same keep-if-present contract as
# MINTED_AT/PREFIX/CLASS above; first line wins, matching every reader's
# `head -n1`) > 'release' default (preserves the previous EFFECTIVE behavior;
# the safe channel for a newborn that nobody declared a canary).
# The DOGANY_UPDATE_CHANNEL env var is deliberately IGNORED for seeding: a
# mint run from a canary's shell must not silently mint canaries -- but a set
# env var that disagrees with the seeded value gets a loud notice below so
# the divergence is visible at birth, not at the first self-update.
SAVED_CHANNEL="$(grep -E '^DOGANY_UPDATE_CHANNEL=' "$PROJECT_ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [ -n "$UPDATE_CHANNEL_FLAG" ]; then
  UPDATE_CHANNEL="$UPDATE_CHANNEL_FLAG"
  UPDATE_CHANNEL_SRC="--channel"
elif [ -n "$SAVED_CHANNEL" ]; then
  UPDATE_CHANNEL="$SAVED_CHANNEL"
  UPDATE_CHANNEL_SRC="kept from existing .instance.conf"
else
  UPDATE_CHANNEL="release"
  UPDATE_CHANNEL_SRC="default -- no --channel given, no prior declaration"
fi
# A preserved value can predate this gate (or be a hand-edit typo): validate
# the FINAL value too, fail loud with the remedy. Never seed a channel that
# self-update.sh would die on.
case "$UPDATE_CHANNEL" in
  release|dev|staging|main) : ;;
  *)
    echo "[mint][FATAL] existing .instance.conf declares invalid channel '${UPDATE_CHANNEL}' (valid: release|dev|staging|main) -- re-run with an explicit --channel to correct it" >&2
    exit 1 ;;
esac
if [ -n "${DOGANY_UPDATE_CHANNEL:-}" ] && [ "$DOGANY_UPDATE_CHANNEL" != "$UPDATE_CHANNEL" ]; then
  echo "[mint][WARN] DGN-1569: env DOGANY_UPDATE_CHANNEL='${DOGANY_UPDATE_CHANNEL}' is NOT inherited by the newborn -- the manifest declares '${UPDATE_CHANNEL}'. Pass --channel ${DOGANY_UPDATE_CHANNEL} if the inheritance was intended." >&2
fi
echo "[mint] update channel: ${UPDATE_CHANNEL} (${UPDATE_CHANNEL_SRC})"
PRESERVED_MANIFEST_STATE="$(mktemp "$PROJECT_ROOT/.instance.conf.state.XXXXXX")"
manifest_preserved_state "$PROJECT_ROOT/.instance.conf" > "$PRESERVED_MANIFEST_STATE"
TMP_MANIFEST="$(mktemp "$PROJECT_ROOT/.instance.conf.tmp.XXXXXX")"
if [ -f "$PROJECT_ROOT/.instance.conf" ]; then
  cp -p "$PROJECT_ROOT/.instance.conf" "$TMP_MANIFEST"
fi
cat > "$TMP_MANIFEST" <<MANIFEST
# .instance.conf -- non-secret instance manifest written by mint.sh.
# Consumed by update.sh to re-substitute placeholders on framework refresh.
# Secrets (bot token, chat id) live in .telegram_bot/.env, NEVER here.
DOGANY_AGENT_NAME=${AGENT_NAME}
DOGANY_AGENT_LABEL=${AGENT_LABEL}
DOGANY_USER_LABEL=${USER_LABEL}
DOGANY_AGENT_PREFIX=${AGENT_PREFIX}
DOGANY_FW_VERSION=${FW_VERSION}
DOGANY_FW_REPO_SHA=${FW_REPO_SHA}
DOGANY_FW_TAG=${FW_TAG}
DOGANY_REPO_ROOT=${REPO_ROOT}
DOGANY_MINTED_AT=${MINTED_AT}
DOGANY_UPDATE_CHANNEL=${UPDATE_CHANNEL}
MANIFEST
# DGN-227 MAJOR-4: append preserved class/pack records only when they existed
# (keep-if-present). Emitting them conditionally keeps a fresh direct mint's
# manifest unchanged while a re-mint of a domain / pack-consuming instance
# retains DOGANY_AGENT_CLASS and DOGANY_PACKS.
[ -n "$SAVED_CLASS" ] && printf 'DOGANY_AGENT_CLASS=%s\n' "$SAVED_CLASS" >> "$TMP_MANIFEST"
[ -n "$SAVED_PACKS" ] && printf 'DOGANY_PACKS=%s\n' "$SAVED_PACKS" >> "$TMP_MANIFEST"
cat "$PRESERVED_MANIFEST_STATE" >> "$TMP_MANIFEST"

# A retained state record missing from the candidate is a recovery-safety
# failure. Abort before the atomic mv so it is visible and the old manifest
# remains intact rather than turning a recovery re-run into silent data loss.
while IFS= read -r preserved_record || [ -n "$preserved_record" ]; do
  if ! grep -Fqx -- "$preserved_record" "$TMP_MANIFEST"; then
    echo "[mint][FATAL] recovery would drop .instance.conf state: $preserved_record" >&2
    rm -f "$PRESERVED_MANIFEST_STATE" "$TMP_MANIFEST"
    exit 1
  fi
done < "$PRESERVED_MANIFEST_STATE"
PRESERVED_STATE_COUNT="$(wc -l < "$PRESERVED_MANIFEST_STATE" | tr -d '[:space:]')"
rm -f "$PRESERVED_MANIFEST_STATE"
mv -f "$TMP_MANIFEST" "$PROJECT_ROOT/.instance.conf"
echo "[mint] preserved $PRESERVED_STATE_COUNT non-mint-owned .instance.conf state record(s)"
echo "[mint] wrote $PROJECT_ROOT/.instance.conf (framework version ${FW_VERSION}, repo sha ${FW_REPO_SHA})"

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

# 3c2) write the lifekit BUNDLE skills checksum manifest (DGN-530), the exact
#      3c contract applied to .claude/skills-bundle/: records each bundle skill
#      AS INSTALLED (post-substitution, on-disk bytes -- step 2 already ran the
#      whole-tree substitution) so update.sh's section 3j edit-detect can tell
#      a user edit from mint-time state. Without this seed the very first
#      self-update runs on the no-manifest branch and fires a spurious
#      "user-modified" backup for every bundle skill on a pristine instance
#      (the DGN-387 lesson, same fix shape). Symlinked dirs are skipped
#      defensively (DGN-956 'share' skills are crew-shared bodies, not this
#      instance's install product -- update.sh 3j skips them the same way).
BUNDLE_MANIFEST="$PROJECT_ROOT/.claude/.dogany-bundle-skills.sha"
if [ -d "$PROJECT_ROOT/.claude/skills-bundle" ]; then
  mkdir -p "$PROJECT_ROOT/.claude"
  {
    printf '# .dogany-bundle-skills.sha -- checksums of lifekit bundle skills as installed\n'
    printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
    printf '# framework refresh overwrites them (DGN-530). Format: "<skill-name>  <sha>".\n'
    for sd in "$PROJECT_ROOT"/.claude/skills-bundle/*/; do
      [ -d "$sd" ] || continue
      [ -L "${sd%/}" ] && continue
      sname="$(basename "$sd")"
      printf '%s  %s\n' "$sname" "$(skill_checksum "$sd")"
    done
  } > "$BUNDLE_MANIFEST"
  echo "[mint] wrote $BUNDLE_MANIFEST (bundle skill checksums)"
fi

# 3d) write the framework FILE checksum manifest (.dogany-framework.sha) so the
#     very first self-update does NOT fire a spurious "user-modified" WARN +
#     backup on a pristine instance (DGN-387). Records the sha of the
#     framework-owned instance-root files AS THEY LAND ON DISK here at mint:
#       - playbooks/AGENT-OPS.md : sha of the SUBSTITUTED on-disk file (step 2
#                        already ran the whole-tree *.md substitution over it).
#                        Moved out of the instance root by DGN-1141 stage 6 --
#                        the manifest KEY is the relpath, matching update.sh 3k2.
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
  [ -f "$PROJECT_ROOT/playbooks/AGENT-OPS.md" ] && \
    printf 'playbooks/AGENT-OPS.md  %s\n' "$(fw_file_checksum "$PROJECT_ROOT/playbooks/AGENT-OPS.md")"
  [ -f "$PROJECT_ROOT/RULES.md" ] && \
    printf 'RULES.md  %s\n' "$(fw_file_checksum "$PROJECT_ROOT/RULES.md")"
} > "$FRAMEWORK_MANIFEST"
echo "[mint] wrote $FRAMEWORK_MANIFEST (framework file checksums)"

# 3e) write the bridge per-file manifest so the very first self-update does NOT
#     mis-classify pristine bridge code as class-5 bootstrap (DGN-677). Records
#     the sha of every bridge code file AS IT LANDS ON DISK here (post step-2
#     substitution + step-3 plist rename). MANDATORY: without it every mint
#     starts on update.sh's no-manifest branch -> class-5 freeze (the Warg
#     DGN-677 regression). The find universe filter below is a BYTE-IDENTICAL
#     copy of update.sh's _bridge_universe filter (update.sh: the "Channel (2)"
#     find); there is no shared lib between mint.sh and update.sh (verified:
#     file_checksum / fw_file_checksum / skill_checksum are each re-defined
#     locally), so the duplication is accepted with THIS warning comment. IF YOU
#     EDIT ONE FILTER YOU MUST EDIT BOTH -- a divergence re-opens the
#     missing-entry freeze. digest = fw_file_checksum, byte-identical to
#     update.sh's file_checksum (shasum < file | awk '{print $1}').
BRIDGE_MANIFEST="$PROJECT_ROOT/.claude/.dogany-bridge.sha"
if [ -d "$PROJECT_ROOT/bridge" ]; then
  mkdir -p "$PROJECT_ROOT/.claude"
  {
    printf '# .dogany-bridge.sha -- per-file checksums of bridge/ as last installed\n'
    printf '# by dogany-agent update.sh (DGN-593). Used for the 3-way per-file\n'
    printf '# reconcile. Format: "<relpath>  <sha>".\n'
    ( cd "$PROJECT_ROOT/bridge" && find . -type f \
        ! -name '.DS_Store' ! -name '*.pyc' ! -name '*.bak.*' \
        ! -name '*.db' ! -name '.env' \
        ! -path './.git/*' ! -path './venv/*' ! -path '*/__pycache__/*' \
        ! -path './runtime/*' ! -path './logs/*' \
        -print | sed 's|^\./||' | LC_ALL=C sort ) | while IFS= read -r _bf; do
      [ -n "$_bf" ] || continue
      printf 'bridge/%s  %s\n' "$_bf" "$(fw_file_checksum "$PROJECT_ROOT/bridge/$_bf")"
    done
  } > "$BRIDGE_MANIFEST"
  echo "[mint] wrote $BRIDGE_MANIFEST (bridge per-file checksums)"
fi

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

# 5) structured lane (lifekit.db): NOT initialized at mint anymore.
#    DGN-803 LS-5 (grill G2): the unconditional schema.sql -> lifekit.db init
#    moved to KIT ACTIVATION time -- scripts/pack/pack_install.sh (kind=kit
#    path) initializes lifekit.db from the pack payload schema right after
#    kit_core lands. A non-lifekit mint has no schema.sql and no lifekit.db,
#    and every framework consumer treats that as the NORMAL state (silent
#    no-op: backup-data.sh, remind.sh, memory-engine body-state). The F7
#    sqlite3 hard-fail breadcrumb (DGN-674) lives in pack_install.sh now.
echo "[mint] structured lane (lifekit.db) deferred to kit activation (pack_install)"

# 5b) recipient-side handoff mailbox provisioning (DGN-1488 / DGN-635).
#     ~/.dogany/handoff/<slug>/{inbox,done,dead,tmp} is this instance's
#     RECEIVING address. The locked DGN-635 contract puts its creation on the
#     recipient at mint time -- a sender never mkdirs (FATAL-1: handoff.py
#     submit_to_slug raises MailboxMissingError instead). Before DGN-1488 no
#     mint path provisioned it, so every fresh mint was born unreachable.
#     Delegated to the instance's own routines/handoff-provision.sh (the
#     script handoff.py's error message names as the remedy) so mint and
#     manual repair share one implementation. Idempotent; honors
#     DOGANY_HANDOFF_ROOT (test sandboxes use a fake HOME anyway). Fail-closed
#     under set -e: a mint whose mailbox cannot be created must not report
#     success -- an unreachable agent is the exact defect this step closes.
bash "$PROJECT_ROOT/routines/handoff-provision.sh"

# 6) build the bridge venv (self-contained, next to bridge/).
# Resolve once through the shared framework resolver. In particular, do not
# let an older /usr/bin/python3 win merely because it is first in PATH.
if [ "$BUILD_VENV" = "1" ]; then
  PY_RESOLVE_LIB="$REPO_ROOT/scripts/pack/lib/py_resolve.sh"
  [[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
    || { echo "ERROR: Python interpreter resolver is unavailable -- cannot build bridge venv" >&2; exit 1; }
  # shellcheck source=pack/lib/py_resolve.sh
  . "$PY_RESOLVE_LIB"
  pack_resolve_py3 3.10 || exit 1
  VENV_PYTHON="$PACK_PY3"
  echo "[mint] building bridge venv (interpreter: $VENV_PYTHON) ..."
  "$VENV_PYTHON" -m venv "$PROJECT_ROOT/bridge/venv"
  "$PROJECT_ROOT/bridge/venv/bin/pip" install -q --upgrade pip
  if [ "$CORE_ONLY" = "1" ]; then
    # Core deps needed to import the bridge = every requirements.txt line
    # ABOVE the "# --- optional" marker (single source, DGN-674 F2). The old
    # inline pip list here drifted independently of requirements.txt (A4);
    # now a new core dep added to requirements.txt flows to --core-only
    # automatically. Marker missing (edited file?) -> fall back to the FULL
    # requirements (heavier, never a broken import).
    CORE_REQ="$(mktemp "${TMPDIR:-/tmp}/dogany-core-req.XXXXXX")"
    if grep -q '^# --- optional' "$PROJECT_ROOT/bridge/requirements.txt"; then
      sed '/^# --- optional/,$d' "$PROJECT_ROOT/bridge/requirements.txt" > "$CORE_REQ"
    else
      cp "$PROJECT_ROOT/bridge/requirements.txt" "$CORE_REQ"
    fi
    "$PROJECT_ROOT/bridge/venv/bin/pip" install -q -r "$CORE_REQ"
    rm -f "$CORE_REQ"
  else
    "$PROJECT_ROOT/bridge/venv/bin/pip" install -q -r "$PROJECT_ROOT/bridge/requirements.txt"
  fi
  "$PROJECT_ROOT/bridge/venv/bin/python" -c "from faster_whisper import WhisperModel"
  echo "[mint] venv ready"
fi

# 7) sanity: no placeholder survivors -- HARD GATE (DGN-674 F1).
#    (a) __X__ framework tokens across the WHOLE instance, every file type
#        (grep -rI, no --include filter): the substitution list at step 2
#        covers .conf/.service/.timer/.txt and the extensionless plists.defer,
#        so the scan must not be narrower than the substitution (A9). Any
#        survivor = a real substitution miss -> ABORT (was WARN-only, which
#        let broken instances ship silently).
#    CROSS-REF: token list also at update.sh subst_one (~L1053-1066),
#    pack_install.sh _subst_mint_tokens, and knowledge_selftest.sh G4 --
#    keep all four sites in sync when adding a token. The release smoke
#    (tests/install_smoke.sh S2) additionally runs a GENERIC __[A-Z_]*__
#    scan that catches list drift here at release time.
LEFT="$(grep -rIlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__' \
          --exclude-dir=venv --exclude-dir=.git \
          "$PROJECT_ROOT" 2>/dev/null || true)"
if [ -n "$LEFT" ]; then
  echo "[mint][ERROR] placeholder survivors (__X__ tokens) in:" >&2; echo "$LEFT" >&2
  echo "[mint][ERROR] substitution missed the files above -- aborting (DGN-674 F1)." >&2
  exit 1
fi
#    (b) angle-bracket aspirational placeholders <UPPER_SNAKE> in the baseline
#        identity/rules markdown. EVERY layout generation is listed, newest
#        first: identity/hot.custom.*.md + rules/hot.*.md (2.0 layout,
#        DGN-1141) / PROFILE.md + USER.md (2.0 flat root) / AGENT.md +
#        RULES.md (pre-2.0) / CLAUDE.md. The older names must stay listed:
#        this same mint runs against pre-relayout instances, and dropping a
#        name silently scans nothing there. (Historically PROFILE.md had to be
#        listed because "grep -r" does NOT follow symlinks found during
#        recursion, so an AGENT.md-only list scanned the compat link and found
#        nothing -- DGN-1141 stage 6 retired those template links, but the
#        same trap applies to any future compat name.)
#        These were never wired to a token; a mint would leave them raw. Scoped
#        to these files on purpose: skill docs legitimately use <TAB>/<name>
#        notation, which is not a placeholder. HTML comments (<!-- ... -->) start
#        with '!' so they never match <[A-Z_]+>.
#        Stays WARN-only (not part of the F1 hard gate): the identity files are
#        keep-if-present USER content on a re-mint, so a user-authored
#        <UPPER_SNAKE> note must never abort a re-mint. The release smoke
#        (install_smoke.sh S2) hard-asserts this scan on a PRISTINE mint.
ANGLE="$(grep -rlE '<[A-Z][A-Z_]+>' \
          --include='hot.custom.agent.md' --include='hot.custom.owner.md' \
          --include='hot.custom.md' --include='hot.framework.md' \
          --include='PROFILE.md' --include='AGENT.md' --include='RULES.md' \
          --include='USER.md' --include='CLAUDE.md' \
          "$PROJECT_ROOT" 2>/dev/null || true)"
if [ -n "$ANGLE" ]; then
  echo "[mint][WARN] angle-bracket placeholder survivors in:" >&2; echo "$ANGLE" >&2
fi

# 7b) seed the secret-sweep owner-identity file for the pre-push PII gate.
#     scripts/secret-sweep.sh sources ~/.dogany/sweep-identity at runtime to
#     detect owner PII (Telegram ID / emails / real name / machine path) before
#     anything reaches a public history. The file holds PII so it is machine-
#     local and UNTRACKED; mint drops a template copy the new owner must fill.
#     Idempotent: if the file already exists, leave it untouched.
SWEEP_ID_DST="$HOME/.dogany/sweep-identity"
SWEEP_ID_SRC="$PROJECT_ROOT/scripts/sweep-identity.example"
if [ -f "$SWEEP_ID_DST" ]; then
  echo "[mint] sweep-identity exists -> keep (idempotent): $SWEEP_ID_DST"
elif [ ! -f "$SWEEP_ID_SRC" ]; then
  echo "[mint][WARN] no sweep-identity.example at $SWEEP_ID_SRC -- skipping" >&2
else
  mkdir -p "$HOME/.dogany"
  cp "$SWEEP_ID_SRC" "$SWEEP_ID_DST"
  chmod 600 "$SWEEP_ID_DST"
  echo "[mint] seeded $SWEEP_ID_DST (mode 600) from sweep-identity.example"
  echo "[mint][NOTE] FILL $SWEEP_ID_DST with your real identity -- until you do,"
  echo "[mint][NOTE] the pre-push secret-sweep owner-PII gate cannot protect you."
fi

# 7c) seed the pending-decision ledger (DGN-817, feeds DGN-214).
#     status-footer.py (Stop hook) reads worklog/_DECISIONS.md and attaches a
#     pending-decision summary to every reply footer. A MISSING file reads as
#     "confirmed no pending decisions" (status-footer.py _collect_decisions,
#     FileNotFoundError -> []) -- so an unseeded instance never breaks, it
#     just has no header-comment convention on hand the first time a decision
#     needs one. Same scaffold-only-if-absent contract as config/agent.conf
#     (1b): a re-mint with --force must NEVER touch an existing ledger -- it
#     can hold live pending decisions, and overwriting it would silently
#     destroy them. Excluded from the step-1 rsync for the same reason.
if [ ! -f "$PROJECT_ROOT/worklog/_DECISIONS.md" ] && [ -f "$TEMPLATE/worklog/_DECISIONS.md" ]; then
  mkdir -p "$PROJECT_ROOT/worklog"
  cp -p "$TEMPLATE/worklog/_DECISIONS.md" "$PROJECT_ROOT/worklog/_DECISIONS.md"
  echo "[mint] wrote worklog/_DECISIONS.md (scaffold)"
elif [ -f "$PROJECT_ROOT/worklog/_DECISIONS.md" ]; then
  echo "[mint] worklog/_DECISIONS.md exists -> keep (idempotent)"
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
  # DGN-668: tracked hooks must be executable or git silently skips them.
  if [ -f "$PROJECT_ROOT/git-hooks/pre-push" ]; then
    chmod +x "$PROJECT_ROOT/git-hooks/pre-push"
    echo "[mint] chmod +x git-hooks/pre-push (idempotent)"
  fi
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
    # DGN-668: install the pre-push secret-sweep gate as executable BEFORE the
    # init commit so the mode bit lands in history; a non-executable hook is
    # silently skipped by git.
    if [ -f git-hooks/pre-push ]; then
      chmod +x git-hooks/pre-push
    fi
    git add --all
    GIT_AUTHOR_NAME="dogany-mint" \
    GIT_AUTHOR_EMAIL="mint@dogany.local" \
    GIT_COMMITTER_NAME="dogany-mint" \
    GIT_COMMITTER_EMAIL="mint@dogany.local" \
    git commit -q -m "init: mint ${AGENT_NAME} (dogany-agent ${FW_VERSION})"
  )
  echo "[mint] git repo initialized at $PROJECT_ROOT (.git)"
fi

# 9) DGN-1694: seed the derived voice vocabulary cache (.telegram_bot/, git-
#    ignored). Best effort: the bridge refreshes it again on every start.
if python3 "$PROJECT_ROOT/routines/lib/voice_vocab.py" "$PROJECT_ROOT" --write >/dev/null 2>&1; then
  echo "[mint] voice vocabulary cache written"
else
  echo "[mint][WARN] voice vocabulary cache not written (bridge start retries)" >&2
fi

# Operator next-steps: only for a hand-run mint. install.sh (DOGANY_MINT_CALLER=install)
# runs those steps itself, so a new user never sees this developer checklist (rehearsal 2026-10-02).
if [ "${DOGANY_MINT_CALLER:-}" = "install" ]; then
  echo "[mint] DONE -> $PROJECT_ROOT"
else
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
     STAFF-AGENT NOTE: a main agent can grow staff (sub-)agents -- specialist
     agents minted from this same base, coordinated by the main agent; a
     specialist mint rewrites the Role section at creation
     (e.g. "fitness-domain expert: coach the user from lifekit records and
     training principles"). Role is editable on explicit user request, per RULES
     edit rights. Route Role writes through the baseline-editor per the
     agent-crafting phase steps.
  3. voice:             install ffmpeg for audio conversion
  4. Load launchd:       cp bridge/*.plist routines/*.plist ~/Library/LaunchAgents/
                         then launchctl bootstrap (LIVE op -- get approval first).
DONE
fi
