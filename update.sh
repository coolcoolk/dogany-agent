#!/usr/bin/env bash
# update.sh -- refresh a Dogany instance's FRAMEWORK from this repo, safely.
#
# update != release. This script CONSUMES a published framework release INTO
# an instance ("update yourself"). Bumping VERSION + tagging PRODUCES a release
# (that is release.sh, a separate maintainer-only act). Told to "update
# yourself", an agent runs routines/self-update.sh (a zero-arg wrapper that
# resolves its own instance root and git-pulls the repo before invoking this
# script with --root <self> --yes) -- it does NOT cut a release.
#
# What it does:
#   1. Resolve the update SOURCE (DGN-593 A3). Channel "release" (default,
#      DGN-221): NEVER moves the shared checkout -- fetches tags, extracts the
#      latest v* tag into a private per-run temp dir via `git archive`, and
#      re-invokes THAT tree's update.sh ("release never touches the shared
#      checkout"). Channel "main" (DOGANY_UPDATE_CHANNEL=main): git pull
#      --ff-only, guarded to run only when the repo is ON the main branch.
#   2. Re-sync ONLY framework code into the instance (agents/main by default):
#      bridge code, routines, memory engine, service SDK, database schema,
#      config, .claude/settings.json, worklog template, and the official
#      framework skills (root skills/dogany-*). See the FRAMEWORK SERVICES
#      MANIFEST comment below for the exact allowlist.
#   3. Refresh RULES.md, telegram.md, and bridge.md -- framework-owned docs.
#      RULES.md is the framework constitution (DGN-130); telegram.md and
#      bridge.md are the bridge output-contract docs (DGN-875). All three are
#      refreshed with the SAME user-edit-detection + backup contract as the
#      dogany-* skills -- if a local edit is detected, the instance copy is
#      backed up to <file>.user-<timestamp> before being replaced.
#   4. Re-substitute the five mint placeholders on the refreshed files, using
#      the instance manifest (.instance.conf) written at mint time.
#
# What it NEVER touches (user data + instance identity are preserved verbatim):
#   - memories/            (long-term memory markdown)
#   - .telegram_bot/.env   (bot token, allowed users) and runtime/.env
#   - *.db                 (lifekit.db, memory-engine/state.db -- user data + cache)
#   - bridge/venv/         (built virtualenv)
#   - AGENT.md / USER.md   (instance identity: name, Role, accreted Workflows,
#                           user facts -- instance-owned, see IDENTITY GUARD below)
#   - CLAUDE.md            (thin entrypoint that @-includes RULES/AGENT/USER)
#   - NON-dogany skills under .claude/skills/     (user-authored skills)
#   - .claude/settings.local.json  (instance-local harness config -- hooks and
#                           settings the instance adds for itself. Claude Code
#                           merges it with settings.json natively, so instance
#                           hooks belong THERE, never in the framework-owned
#                           settings.json. DGN-359)
#   - preserve-list entries (.claude/.dogany-preserve -- instance-root-relative
#                           paths the operator declared as locally customized;
#                           see the INSTANCE-PRESERVE LIST section. DGN-359)
#
# It is idempotent: running it twice with no upstream changes is a no-op refresh.
#
# A real minted instance is REQUIRED: the target must carry a .instance.conf
# (written at mint time). The default ./agents/main is a repo SCAFFOLD, not a
# minted instance, so a bare ./update.sh with no --root now errors out instead
# of silently no-op'ing against the scaffold. Point --root at a real deployed
# instance dir (e.g. ~/.dogany/main), or pass --force to override the gate.
#
# Usage:
#   ./update.sh --root DIR      # update a specific minted instance dir (required)
#   ./update.sh                 # targets ./agents/main -- REFUSED unless --force
#                               #   (scaffold has no .instance.conf)
#   ./update.sh --no-pull       # skip source sync (refresh from current checkout)
#   ./update.sh --dry-run       # show what would change, write nothing
#   ./update.sh --code-only     # (alias --no-migrate) land framework code/files
#                               #   but SKIP the section 3f-migrate DB schema
#                               #   step. Divergent-lineage escape hatch
#                               #   (DGN-656): an instance whose lifekit.db
#                               #   lineage forked from canonical (e.g. domain
#                               #   pack migrations) takes framework code
#                               #   fixes WITHOUT being force-marched through
#                               #   canonical migrations. NOT the default:
#                               #   version-pinned DB-coupled code that would
#                               #   outrun the frozen schema is held back
#                               #   (see the 3f forward-pin guard).
#   ./update.sh --force         # bypass the .instance.conf validity gate
#   ./update.sh --yes | -y      # bypass the pre-flight confirmation prompt
#   ./update.sh --bridge-accept REL
#                               # accept the vendor version of ONE bridge file
#                               #   (REL instance-relative, e.g. bridge/bot.py;
#                               #   repeatable; instance copy is backed up to
#                               #   .claude/bridge-backups/ before landing)
#   ./update.sh --bridge-accept-all
#                               # accept vendor version of all NON-conflict
#                               #   bridge drift (backs up each file first);
#                               #   REFUSED when conflict-class files exist
#   ./update.sh --force-accept-all
#                               # accept-all INCLUDING conflict-class files
#                               #   (per-file backups still taken first)
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
# resolve_channel_tag <repo_root> <channel>  (DGN-621 v2 phase1)
#   Picks the update-target tag for the staged deployment model. Channels are
#   distinguished by TAG SUFFIX, not branches/repos:
#     release (default) : highest STABLE tag  vX.Y.Z  -- pre-release tags
#                         (any 'v*-*', e.g. -dev.N / -rc.N) are excluded.
#     dev               : highest tag over the whole 'v*' set (a pre-release is
#                         eligible). versionsort.suffix=- makes any hyphenated
#                         suffix rank BELOW its own stable, so once the stable
#                         exists dev subscribers move forward to it and never
#                         regress to a -dev. (Default git version-sort would
#                         WRONGLY rank vX.Y.Z-dev.N ABOVE vX.Y.Z -- DGN-621
#                         verified 2026-07-28 -- hence the explicit -c flag;
#                         it also makes ordering independent of global config.)
#   DOGANY_UPDATE_PIN (optional, env): if set non-empty it OVERRIDES channel --
#     select exactly that tag; if the pinned tag is absent, FAIL LOUD (a pin
#     that cannot be honored is an operator error, never a silent latest).
#   Behavior for a stable-only estate (no PIN, channel release/unset) is
#   byte-identical to the old one-liner: highest v* == highest stable v*.
#   Prints the resolved tag on stdout; empty output = no eligible tag (callers
#   already handle "no tag"). Channel "main" never reaches here (pull path).
# ---------------------------------------------------------------------------
resolve_channel_tag() {
  _rct_repo="$1"
  _rct_channel="${2:-release}"
  if [ -n "${DOGANY_UPDATE_PIN:-}" ]; then
    if git -C "$_rct_repo" rev-parse -q --verify "refs/tags/${DOGANY_UPDATE_PIN}" >/dev/null 2>&1; then
      printf '%s\n' "$DOGANY_UPDATE_PIN"
      return 0
    fi
    die "DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' not found as a tag in ${_rct_repo} -- refusing to fall back to latest (fix the pin or unset it)"
  fi
  if [ "$_rct_channel" = "dev" ]; then
    # versionsort.suffix=- ranks any pre-release below its stable (so a promoted
    # vX.Y.Z outranks vX.Y.Z-dev.N); without it default git sorts the -dev ABOVE.
    git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
      | head -n1
  else
    # release (default, and any unknown channel): exclude pre-release tags
    # (any tag whose name contains a hyphen, e.g. -dev.N / -rc.N).
    git -C "$_rct_repo" tag --list 'v*' --sort=-v:refname \
      | grep -v -- '-' | head -n1
  fi
}

# ---------------------------------------------------------------------------
# resolve_update_pin <instance_conf> <channel>  (DGN-673 B2, owner decision D2)
#   DOGANY_UPDATE_PIN persistence: env wins, .instance.conf fallback -- the
#   exact UPDATE_CHANNEL read pattern above. A pin persisted in the conf
#   survives scheduled self-updates, so a rolled-back instance is never
#   silently re-upgraded to the bad tag on the next run (DGN-673 failure S8).
#   Resolution:
#     1. env DOGANY_UPDATE_PIN non-empty -> env wins, conf not consulted.
#     2. else conf line 'DOGANY_UPDATE_PIN=<tag>' -> adopt the conf pin.
#     3. conf line present but EMPTY value -> pin released: print a notice
#        (repeats every run until the leftover line is deleted -- deliberate
#        cleanup nudge; pin removal is a rollback incident-close item).
#   A resolved pin is EXPORTED so child update.sh runs inherit it. Every
#   pinned run prints a loud PINNED banner; channel 'main' never resolves
#   tags (pull path), so a pin there gets a loud no-effect warning instead.
#   Call at most ONCE per operator-level run, where the update target is
#   about to be resolved -- never in --no-pull child runs (the parent
#   already announced the pin; a second banner would be noise).
# ---------------------------------------------------------------------------
resolve_update_pin() {
  _rup_conf="$1"
  _rup_channel="${2:-release}"
  if [ -z "${DOGANY_UPDATE_PIN:-}" ] && [ -f "$_rup_conf" ] \
     && grep -q '^DOGANY_UPDATE_PIN=' "$_rup_conf" 2>/dev/null; then
    _rup_pin="$(sed -n 's/^DOGANY_UPDATE_PIN=//p' "$_rup_conf" | head -n1)"
    if [ -n "$_rup_pin" ]; then
      DOGANY_UPDATE_PIN="$_rup_pin"
    else
      msg "[update] 핀 해제됨 -- 채널 추종 재개 (.instance.conf의 빈 DOGANY_UPDATE_PIN= 라인은 삭제해도 됩니다)" \
          "[update] pin released -- channel following resumed (the empty DOGANY_UPDATE_PIN= line in .instance.conf can be deleted)"
    fi
  fi
  [ -n "${DOGANY_UPDATE_PIN:-}" ] || return 0
  export DOGANY_UPDATE_PIN
  if [ "$_rup_channel" = "main" ]; then
    msg "[update][경고] DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' 은 channel=main(pull 경로)에서 아무 효과가 없습니다" \
        "[update][WARN] DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' has NO effect on channel=main (pull path)" >&2
    return 0
  fi
  printf '%s\n' "============================================================"
  msg "[update] PINNED to ${DOGANY_UPDATE_PIN} -- 채널 추종 중단 (플릿 구제 후 핀 제거 필수)" \
      "[update] PINNED to ${DOGANY_UPDATE_PIN} -- channel following suspended (remove pin after fleet remedy)"
  printf '%s\n' "============================================================"
}

# WSL drift check constants (mirror install.sh). The Windows-side setup writes
# a marker at this version; if it drifts below the required version, update.sh
# NAGS (prints, never fails the update) to re-run setup-windows.ps1.
REQUIRED_WINDOWS_SETUP_VERSION=1
WINDOWS_SETUP_MARKER="/etc/dogany/windows-setup.version"
is_wsl() { grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }

# On WSL, warn (do not fail) when the Windows-side setup marker is missing or
# older than required -- the .wslconfig/wsl.conf shape may have changed and the
# user must re-run setup-windows.ps1. Reads only a Linux-side file; never
# touches the Windows filesystem.
wsl_drift_nag() {
  is_wsl || return 0
  local marker_ver=0
  if [ -f "$WINDOWS_SETUP_MARKER" ]; then
    marker_ver="$(tr -dc '0-9' < "$WINDOWS_SETUP_MARKER" 2>/dev/null)"
    marker_ver="${marker_ver:-0}"
  fi
  [ "$marker_ver" -ge "$REQUIRED_WINDOWS_SETUP_VERSION" ] 2>/dev/null && return 0

  local ps1='powershell.exe -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\<your-linux-username>\.dogany\framework\windows\setup-windows.ps1'
  printf '%s\n' "------------------------------------------------------------" >&2
  msg "[update][주의] Windows(WSL2) 설정이 오래되었거나 없습니다 (마커 v${marker_ver}, 필요 v${REQUIRED_WINDOWS_SETUP_VERSION})." \
      "[update][NOTE] Windows (WSL2) setup is stale or missing (marker v${marker_ver}, need v${REQUIRED_WINDOWS_SETUP_VERSION})." >&2
  msg "Windows PowerShell(일반 사용자)에서 아래를 다시 실행하세요:" \
      "Re-run this in Windows PowerShell (normal user):" >&2
  printf '  %s\n' "$ps1" >&2
  msg "업데이트는 계속 진행됩니다." "The update continues regardless." >&2
  printf '%s\n' "------------------------------------------------------------" >&2
}

# Portable in-place sed: BSD (macOS) and GNU (Linux) disagree on `sed -i`'s
# flavor (BSD requires a mandatory backup-suffix arg, GNU forbids the space).
# Sidestep the incompatibility entirely: run sed to a temp file, then mv it back.
# Args: <file> <sed-arg>...  (the sed args are the -e expressions to apply).
# Preserves LC_ALL=C. GNU-safe by construction (no -i used at all).
# MODE PRESERVATION: mktemp creates 0600 files, so a bare mv would clobber the
# target's permissions -- every substituted script lost its exec bit (defect
# found dogfooding a live instance update). `cp -p` stamps the original file's
# mode onto the temp BEFORE the mv; the sed redirect truncates content only.
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

# ---------------------------------------------------------------------------
# Args.
# ---------------------------------------------------------------------------
INSTANCE="$REPO_ROOT/agents/main"
DO_PULL=1
DRY_RUN=0
CODE_ONLY=0
FORCE=0
ASSUME_YES=0
BRIDGE_ACCEPT_LIST=()
BRIDGE_ACCEPT_ALL=0
FORCE_ACCEPT_ALL=0
# DGN-593: preserve the original argv VERBATIM -- the release channel
# (section 1) re-invokes the EXTRACTED tree's update.sh with exactly these
# args (+ --no-pull), so every operator flag survives the handoff.
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --root)    INSTANCE="$2"; shift 2 ;;
    --no-pull) DO_PULL=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --code-only|--no-migrate) CODE_ONLY=1; shift ;;
    --force)   FORCE=1; shift ;;
    -y|--yes)  ASSUME_YES=1; shift ;;
    --bridge-accept)
      [ $# -ge 2 ] || die "--bridge-accept requires a relpath argument"
      BRIDGE_ACCEPT_LIST+=("$2"); shift 2 ;;
    --bridge-accept-all) BRIDGE_ACCEPT_ALL=1; shift ;;
    --force-accept-all)  BRIDGE_ACCEPT_ALL=1; FORCE_ACCEPT_ALL=1; shift ;;
    -h|--help)
      sed -n '2,88p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[ -d "$TEMPLATE" ] || die "framework template not found: $TEMPLATE"
[ -d "$INSTANCE" ] || die "instance dir not found: $INSTANCE (pass --root DIR)"
INSTANCE="$(cd "$INSTANCE" && pwd)"

# Guard: never treat the repo itself or the template as the instance.
#
# DGN-341: when --root resolves to the repo root itself, the caller is running
# the "dogfood layout" (instance root == framework repo root -- the clone IS
# the instance). This layout is UNSUPPORTED: update.sh cannot safely refresh
# framework files into the same tree it is reading them from. The caller must
# migrate to the standard layout (a separate instance directory that CONSUMES
# the framework repo). The message below names the layout explicitly so users
# can distinguish this refusal from a generic mistake.
if [ "$INSTANCE" = "$REPO_ROOT" ]; then
  printf '%s\n' "------------------------------------------------------------" >&2
  msg "[update][오류] dogfood 레이아웃 감지: 인스턴스 루트가 프레임워크 저장소 루트와 동일합니다." \
      "[update][ERROR] dogfood layout detected: instance root == framework repo root." >&2
  msg "  이 레이아웃은 지원되지 않습니다. update.sh는 동일한 트리에서 파일을 읽으면서 갱신할 수 없습니다." \
      "  This layout is unsupported: update.sh cannot refresh framework files into the same tree it reads from." >&2
  msg "  조치: 표준 레이아웃(저장소를 소비하는 별도 인스턴스 디렉터리)으로 마이그레이션하세요." \
      "  Remediation: migrate to the standard layout (a separate instance directory consuming the framework repo)." >&2
  msg "  참조: docs/ 의 install/update 문서를 확인하세요." \
      "  See: install and update docs in docs/." >&2
  printf '%s\n' "------------------------------------------------------------" >&2
  exit 1
fi
[ "$INSTANCE" = "$TEMPLATE" ]  && die "refusing to update the template itself"

# INSTANCE-VALIDITY GATE: a real minted instance carries a .instance.conf (written
# by mint.sh). The default ./agents/main is a repo SCAFFOLD (no .instance.conf,
# RULES/USER symlinked into rules/), NOT a deployable instance -- updating it is
# almost always an operator who forgot --root. Refuse unless --force. This turns
# what used to be a silent no-op against the scaffold into an immediate error.
if [ ! -f "$INSTANCE/.instance.conf" ] && [ "$FORCE" = "0" ]; then
  die "not a minted Dogany instance (no .instance.conf): $INSTANCE
        pass --root DIR pointing at a real instance (e.g. ~/.dogany/main),
        or --force to override the gate."
fi

# ===========================================================================
# FRAMEWORK SERVICES MANIFEST (DGN-130) -- the EXACT, explicit allowlist of
# framework-owned paths this script refreshes into an instance. This is the
# single documented source of truth for the shared-services refresh; the
# section-3 rsync blocks below implement exactly these entries and nothing
# more. It is an ALLOWLIST by construction, never a "sync everything then
# exclude" glob -- adding a path here is a deliberate act.
#
#   bridge/                 bridge code (framework); venv + .env preserved;
#                           per-file 3-way reconcile against the
#                           .claude/.dogany-bridge.sha manifest (DGN-593);
#                           substitution files + UPSTREAM.md land
#                           unconditionally (preserve-list still supreme);
#                           unmanifested code files are adopt-provisional
#                           (DGN-677): seed M tagged #adopted-provisional (no
#                           landing this run), escalate to CONFLICT next run if
#                           the vendor also changed (--bridge-accept to land)
#   routines/               framework schedulers/scripts (+ bundle)
#   memory-engine/*.py,*.md memory ENGINE code + taxonomy; NEVER state.db / markdown
#   config/ (i18n only)     locales refreshed; agent.conf/lifekit.conf are
#                           per-instance STATE (write-if-absent, never reset)
#   service/                service SDK facade (lifekit + mailer)
#   mirror/                 GCal/GTasks mirror engine code + schema; NEVER *.db
#                           (mirror_state.db is per-instance sync bookkeeping)
#   database/               schema.sql + lifekit.py/.sh/README; NEVER *.db
#   .claude/settings.json   harness config (instance model choice preserved;
#                           FRAMEWORK hooks only -- instance-local hooks live
#                           in .claude/settings.local.json, which this script
#                           NEVER writes; Claude Code merges both natively)
#   .claude/agents/         framework agent definitions (baseline-editor,
#                           propagation-editor, release-closer; DGN-663). Plain
#                           rsync (no --delete): activated by presence, never a
#                           symlink. A locally-modified agent def is protected
#                           by .claude/.dogany-preserve (per-file exclude);
#                           user-authored agent defs are never pruned
#   worklog/_TEMPLATE.md    ticket template only; never existing tickets
#   skills/dogany-*         official framework skills (edit-detect + backup)
#   .claude/skills-bundle/  dormant lifekit bundle skills
#   RULES.md                framework constitution (edit-detect + backup; DGN-130)
#   telegram.md             framework output-contract doc, Telegram channel layer
#                           (edit-detect + backup; DGN-875)
#   bridge.md               framework output-contract doc, channel-agnostic layer
#                           (edit-detect + backup; DGN-875)
#   git-hooks/              tracked hook scripts (pre-commit detached-HEAD guard;
#                           DGN-525); exec bit preserved; no --delete
#
# Everything NOT on this list is instance state / personal data and is never
# written: memories/, *.db, .env, sessions, runtime/, logs/, bridge/venv/,
# user-authored (non-dogany-) skills, and the identity entrypoints below.
#
# IDENTITY GUARD (DGN-130): AGENT.md and USER.md are instance-owned identity
# (agent name, Role, accreted Workflows; user facts). They are NEVER a refresh
# target in any channel. Exactly TWO framework-owned files live at the instance
# root and ARE refreshed, each by its own channel: RULES.md (section 3k,
# verbatim) and AGENT-OPS.md (section 3k2, substituted -- DGN-387). AGENT.md and
# USER.md remain the only guarded identity files: not in the manifest above, not
# in the RULES/AGENT-OPS channels, not in any section-3 rsync (all of which
# target named subdirs, not the instance root). This constant exists so a future
# edit that tries to fold an entrypoint into a refresh path trips an explicit,
# greppable guard rather than silently clobbering identity. Do not remove; do
# not add AGENT.md / USER.md to it.
FRAMEWORK_NEVER_REFRESH=( "AGENT.md" "USER.md" )
assert_identity_never_refreshed() {
  # RULES.md is deliberately ABSENT here: it is framework-owned and refreshed
  # (with backup) by the DGN-130 channel. Only true identity files are guarded.
  local f
  for f in "${FRAMEWORK_NEVER_REFRESH[@]}"; do
    case "$f" in
      AGENT.md|USER.md) : ;;  # expected members
      *) die "IDENTITY GUARD violated: unexpected entry '$f' in FRAMEWORK_NEVER_REFRESH" ;;
    esac
  done
}
assert_identity_never_refreshed

RSYNC_DRY=""
if [ "$DRY_RUN" = "1" ]; then
  RSYNC_DRY="--dry-run"
  msg "[dry-run] 파일을 쓰지 않고 변경 예정만 표시합니다." \
      "[dry-run] no files will be written; showing planned changes only."
fi

# DGN-656: --code-only escape-hatch notice. Loud by design -- this mode is a
# deliberate deviation from the default lockstep contract (code + schema move
# together). It exists for DIVERGENT-LINEAGE instances only: a lifekit.db whose
# migration lineage forked from canonical (domain-pack migrations, e.g. Warg
# framework 001-010 + health-pack W01-W03) must be able to take framework code
# fixes without canonical migrations being force-applied on top of its fork
# (semantically wrong + collision-prone). Misuse risk: code that REQUIRES a
# newer schema landing against an old DB. Containment: the 3f forward-pin guard
# holds back version-pinned DB-coupled code (lifekit.py) whose pin outruns the
# frozen DB. Self-update.sh forwards this flag through verbatim (ORIG_ARGS).
if [ "$CODE_ONLY" = "1" ]; then
  printf '%s\n' "------------------------------------------------------------"
  msg "[update][주의] --code-only 모드: 프레임워크 코드/파일만 랜딩, DB 스키마 마이그레이션은 건너뜁니다." \
      "[update][NOTE] --code-only mode: landing framework code/files ONLY; DB schema migrations are SKIPPED."
  msg "  분기 lineage 인스턴스 전용 escape hatch입니다 (DGN-656). 기본 업데이트 경로가 아닙니다." \
      "  Divergent-lineage escape hatch (DGN-656). This is NOT the default update path."
  msg "  DB user_version 핀이 앞서는 코드(lifekit.py)는 보류됩니다 (3f forward-pin 가드)." \
      "  Code whose DB user_version pin outruns the frozen schema (lifekit.py) is held back (3f forward-pin guard)."
  printf '%s\n' "------------------------------------------------------------"
fi

# Dry-run-safe directory creation (DGN-130). Several refresh sections `mkdir -p`
# a destination dir before an rsync/cp that rsync's own --dry-run then skips --
# which left empty scaffold dirs behind on a --dry-run (cosmetic, but violates
# the "dry-run writes NOTHING" contract). ensure_dir is a no-op under --dry-run
# so a preview never mutates the filesystem; real runs mkdir -p as before.
ensure_dir() {
  [ "$DRY_RUN" = "1" ] && return 0
  mkdir -p "$1"
}

msg "[update] 레포   = $REPO_ROOT" "[update] repo     = $REPO_ROOT"
msg "[update] 인스턴스 = $INSTANCE" "[update] instance = $INSTANCE"

# ---------------------------------------------------------------------------
# REVERSE-DRIFT GUARD (DGN-249): prevent update.sh from overwriting an
# instance file that is AHEAD of the framework source -- e.g. an instance
# already running lifekit.py v6 (DGN-240 local patch) while canonical main
# still carries v5. Overwriting in that direction reverts the pin, leaving
# the live DB at v6 while the code expects v5 = all verbs fail-closed.
#
# Design:
#   GUARDED_FILES is an ordered list of "relpath:extractor_key" pairs for
#   every version-bearing file synced by update.sh. Adding a new guarded file
#   requires:
#     1. One entry in GUARDED_FILES below.
#     2. A matching extract_ver_<extractor_key>() function.
#
#   drift_guard_file RELPATH FW_SRC INST_DEST extractor_key
#     Extracts the integer version from both sides. Rules:
#       - instance > framework -> SKIP + loud warning block.
#       - instance <= framework -> return 0 (caller proceeds normally).
#       - parse failure on either side -> return 0 (guard is best-effort;
#         never blocks a normal update).
#     Returns 1 when the file should be skipped, 0 when proceed.
#
#   db_drift_nag DB_PATH FW_LIFEKIT_PY
#     Informational: if the instance DB's PRAGMA user_version > the framework
#     lifekit.py pin, print a class-of-warning up front. Non-blocking.
# ---------------------------------------------------------------------------

# Extractor: parse EXPECTED_USER_VERSION = <N> from a lifekit.py file.
# Prints the integer on stdout; exits non-zero on parse failure.
extract_ver_lifekit_py() {
  local f="$1"
  [ -f "$f" ] || return 1
  python3 -c "
import re, sys
txt = open(sys.argv[1]).read()
m = re.search(r'^EXPECTED_USER_VERSION\s*=\s*([0-9]+)', txt, re.MULTILINE)
if not m: sys.exit(1)
print(m.group(1))
" "$f" 2>/dev/null
}

# Extractor: parse the mirror version gate from an sdk_bridge.py.
# DGN-654: the gate moved from an exact whitelist (ALLOWED_USER_VERSIONS
# tuple/list) to a forward-tolerant floor (MIN_USER_VERSION = <N>). Parse
# the new scalar form FIRST; fall back to the legacy tuple/list form (max
# of the set) so the guard still engages against older instance copies
# during the transition. DGN-364 2.7b fix retained: tuple OR list via
# ast.literal_eval.
# Prints the integer on stdout; exits non-zero on parse failure.
extract_ver_sdk_bridge_py() {
  local f="$1"
  [ -f "$f" ] || return 1
  python3 -c "
import re, sys, ast
txt = open(sys.argv[1]).read()
m = re.search(r'^MIN_USER_VERSION\s*=\s*([0-9]+)', txt, re.MULTILINE)
if m:
    print(int(m.group(1)))
    sys.exit(0)
m = re.search(r'^ALLOWED_USER_VERSIONS\s*=\s*(\([^)]*\)|\[[^\]]*\])',
              txt, re.MULTILINE)
if not m: sys.exit(1)
vals = ast.literal_eval(m.group(1))
if not vals: sys.exit(1)
print(max(int(x) for x in vals))
" "$f" 2>/dev/null
}

# GUARDED_FILES: list of "relative/path/to/file:extractor_key" pairs.
# Path is relative to REPO_ROOT (framework source). The instance copy is
# resolved as $INSTANCE/<same-relative-path>.
# To guard a new file: add one line here + a matching extract_ver_<key>()
# function above.
GUARDED_FILES=(
  "database/lifekit.py:lifekit_py"
  # DGN-364 2.7b (F1): the sdk_bridge version pin is guarded at its REAL
  # path (mirror/sdk_bridge.py -- the old commented entry named the wrong
  # path database/sdk_bridge.py). Because section 3e-mirror is a wholesale
  # rsync, this entry engages as a PRE-RSYNC check there (anchored
  # --exclude '/sdk_bridge.py' on a SKIP verdict), not via the 3f
  # per-file loop.
  "mirror/sdk_bridge.py:sdk_bridge_py"
)

# drift_guard_file RELPATH FW_SRC INST_DEST EXTRACTOR_KEY
# Returns 1 (SKIP) when the instance file is ahead of the framework source.
# Returns 0 (PROCEED) in all other cases (including parse errors).
drift_guard_file() {
  local relpath="$1" fw_src="$2" inst_dest="$3" extkey="$4"

  # Both files must exist for the guard to engage.
  [ -f "$fw_src" ]   || return 0
  [ -f "$inst_dest" ] || return 0

  # Dispatch to the correct extractor.
  local fw_ver inst_ver
  fw_ver="$(  "extract_ver_${extkey}" "$fw_src"   2>/dev/null)" || return 0
  inst_ver="$("extract_ver_${extkey}" "$inst_dest" 2>/dev/null)" || return 0

  # Validate: must be plain integers.
  [[ "$fw_ver"   =~ ^[0-9]+$ ]] || return 0
  [[ "$inst_ver" =~ ^[0-9]+$ ]] || return 0

  if [ "$inst_ver" -gt "$fw_ver" ]; then
    # ROLLBACK MODE (DGN-673 B3, R1): a silent SKIP here would leave a TORN
    # tree (every other file downgraded, the guarded file still new). But a
    # blanket relax would clobber a LEGITIMATELY ahead instance -- an
    # instance can be ahead because of the bad release OR because of local
    # lineage (failure S11; Ag lifekit v15 > canonical 11 precedent), and
    # DOGANY_ROLLBACK=1 alone cannot tell the two apart. So the downgrade
    # proceeds ONLY under per-file transition consent naming the exact
    # versions: DOGANY_ROLLBACK_ACK="<extractor_key>:<inst>-><fw>[,...]".
    # No matching consent -> DIE (loud, prints the exact token required).
    if [ "${DOGANY_ROLLBACK:-0}" = "1" ]; then
      local ack_token="${extkey}:${inst_ver}->${fw_ver}"
      case ",${DOGANY_ROLLBACK_ACK:-}," in
        *",${ack_token},"*)
          msg "[update] 롤백 승인됨: $relpath v${inst_ver} -> v${fw_ver} (DOGANY_ROLLBACK_ACK)" \
              "[update] rollback acknowledged: $relpath v${inst_ver} -> v${fw_ver} (DOGANY_ROLLBACK_ACK)"
          return 0  # consented downgrade -- caller overwrites
          ;;
        *)
          printf '%s\n' "============================================================" >&2
          msg "[update][오류] 롤백 모드: 버전 하향에는 파일별 명시 동의가 필요합니다" \
              "[update][ERROR] ROLLBACK MODE: version downgrade needs per-file consent" >&2
          msg "  파일: $relpath (인스턴스 v${inst_ver} -> 프레임워크 v${fw_ver})" \
              "  file: $relpath (instance v${inst_ver} -> framework v${fw_ver})" >&2
          msg "  인스턴스가 '나쁜 릴리스 때문에' 앞선 것인지 '로컬 계보라서' 앞선 것인지" \
              "  Whether this instance is ahead BECAUSE OF the bad release or because" >&2
          msg "  이 스크립트는 구분할 수 없습니다 (S11: 정당하게 앞선 인스턴스 보호)." \
              "  of its own local lineage cannot be decided here (S11 protection)." >&2
          msg "  확인 후 진행하려면: DOGANY_ROLLBACK_ACK=\"${ack_token}\" 를 추가해 재실행" \
              "  To proceed after checking, re-run with: DOGANY_ROLLBACK_ACK=\"${ack_token}\"" >&2
          msg "  (여러 파일은 쉼표로 연결; 런북: docs/ROLLBACK.md 단계 0/4)" \
              "  (comma-join multiple tokens; runbook: docs/ROLLBACK.md steps 0/4)" >&2
          printf '%s\n' "============================================================" >&2
          die "rollback downgrade of $relpath not acknowledged (need DOGANY_ROLLBACK_ACK=\"${ack_token}\")"
          ;;
      esac
    fi
    printf '%s\n' "============================================================" >&2
    msg "[update][경고] 역주행 가드 발동 -- 파일 갱신 건너뜀" \
        "[update][WARN] REVERSE-DRIFT GUARD triggered -- file skipped" >&2
    msg "  파일: $relpath" \
        "  file: $relpath" >&2
    msg "  인스턴스 버전: $inst_ver  |  프레임워크 버전: $fw_ver (낮음)" \
        "  instance version: $inst_ver  |  framework version: $fw_ver (older)" >&2
    msg "  원인: 인스턴스 로컬 패치가 아직 canonical에 승격되지 않은 상태입니다." \
        "  cause: local instance patch not yet promoted to canonical framework." >&2
    msg "  조치: 해당 변경을 canonical에 승격(PR)한 뒤 다시 업데이트하세요." \
        "  action: promote the change to canonical (PR), then re-update." >&2
    printf '%s\n' "============================================================" >&2
    return 1  # caller must skip the copy
  fi

  return 0  # safe to proceed
}

# db_drift_nag DB_PATH FW_LIFEKIT_PY
# Informational: warn when instance DB is ahead of the framework pin.
# Never blocks; never exits non-zero.
db_drift_nag() {
  local db="$1" fw_lifekit="$2"
  [ -f "$db" ]           || return 0
  [ -f "$fw_lifekit" ]  || return 0
  command -v sqlite3 >/dev/null 2>&1 || return 0

  local db_ver fw_pin
  db_ver="$(sqlite3 "$db" 'PRAGMA user_version;' 2>/dev/null)" || return 0
  fw_pin="$(extract_ver_lifekit_py "$fw_lifekit" 2>/dev/null)"  || return 0
  [[ "$db_ver"  =~ ^[0-9]+$ ]] || return 0
  [[ "$fw_pin"  =~ ^[0-9]+$ ]] || return 0

  if [ "$db_ver" -gt "$fw_pin" ]; then
    printf '%s\n' "============================================================" >&2
    msg "[update][경고] DB 스키마가 프레임워크 핀보다 앞서 있습니다 (DB v${db_ver} > 핀 v${fw_pin})." \
        "[update][WARN] Instance DB schema is ahead of the framework pin (DB v${db_ver} > pin v${fw_pin})." >&2
    msg "  lifekit.py 파일 가드가 덮어쓰기를 차단합니다 (아래 로그 확인)." \
        "  The file-level drift guard will block the lifekit.py overwrite (see below)." >&2
    printf '%s\n' "============================================================" >&2
  fi
}

# ---------------------------------------------------------------------------
# ROLLBACK MODE (DGN-673 B3): DOGANY_ROLLBACK=1 switches this run into a
# deliberate downgrade. Three effects, wired at the exact decision points:
#   R1  drift_guard_file (above): silent-SKIP -> loud DIE unless the per-file
#       transition is acknowledged via DOGANY_ROLLBACK_ACK (S11 protection).
#   R2  rollback_db_checkpoint (below, called just before the DOGANY_FW_VERSION
#       stamp): DB ahead of the target pin -> delegate to the DGN-672 snapshot
#       restore path and exit 3 BEFORE the stamp, so the stamp becomes the
#       rollback-COMPLETE marker (S10 stamp-lie closed).
#   banner rollback_banner (below): one loud ROLLBACK MODE line per run (the
#       --no-pull child inherits DOGANY_ROLLBACK via env; the exported guard
#       keeps the child quiet).
# Forward runs (DOGANY_ROLLBACK unset) are UNTOUCHED by all three.
# ---------------------------------------------------------------------------
rollback_banner() {
  [ "${DOGANY_ROLLBACK:-0}" = "1" ] || return 0
  [ -n "${DOGANY_ROLLBACK_BANNERED:-}" ] && return 0
  export DOGANY_ROLLBACK_BANNERED=1
  printf '%s\n' "============================================================"
  msg "[update] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- 의도적 다운그레이드 실행" \
      "[update] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- deliberate downgrade run"
  printf '%s\n' "============================================================"
}

# snapshot_pragma FILE PRAGMA  -- read a PRAGMA from a snapshot WITHOUT
# touching it (read-only open; -readonly first, immutable URI fallback for
# older sqlite3 that lacks the flag). Empty output on any failure.
snapshot_pragma() {
  local f="$1" prag="$2" out=""
  out="$(sqlite3 -readonly "$f" "PRAGMA ${prag};" 2>/dev/null | head -n1)" || out=""
  if [ -z "$out" ]; then
    out="$(sqlite3 "file:${f}?immutable=1" "PRAGMA ${prag};" 2>/dev/null | head -n1)" || out=""
  fi
  printf '%s\n' "$out"
}

# rollback_db_checkpoint INSTANCE_DIR REPO_ROOT  (DGN-673 B3, R2)
#   In rollback mode ONLY, run AFTER 3f-migrate and BEFORE the DOGANY_FW_VERSION
#   stamp. Assert PRAGMA user_version == the target tree's EXPECTED_USER_VERSION.
#   - DB user_version <= target pin: PASS (return 0; the stamp -- the
#     rollback-COMPLETE marker -- is written by the caller).
#   - DB ahead + verified matching snapshot (lifekit.db.v<target>.bak-*,
#     verified by OPENING it: pragma == target AND integrity ok -- never the
#     filename alone, DGN-672 C3): print the delegation instruction
#     (restore-data.sh --to <bak> --no-catchup, DGN-672 C2 -- this script
#     NEVER hand-rolls a DB restore) and exit 3. Exit 3 is the EXPECTED
#     mid-procedure signal: tree synced, DB pending, stamp withheld; after
#     the restore the operator re-runs the same pinned update (idempotent),
#     which passes here and seals the stamp.
#   - DB ahead + NO verified snapshot + the delta (target, cur] crosses a
#     migration marked '-- reversible: no' (or unclassifiable -- missing
#     file/marker is treated as irreversible, fail-closed): hard REFUSE
#     (failure S1). Never hand-edit schema; escalate to DGN-672.
#   - DB ahead + NO verified snapshot + delta all reversible: exit 3 with a
#     pointer to restore-data.sh --list (git-dump restore points may exist).
# Forward runs are UNTOUCHED: without DOGANY_ROLLBACK=1 this returns 0
# immediately (db_drift_nag stays the non-blocking forward watcher; a
# blocking forward assert would brick legitimately-ahead lineages).
rollback_db_checkpoint() {
  [ "${DOGANY_ROLLBACK:-0}" = "1" ] || return 0
  local inst="$1" repo="$2"
  local db="$inst/database/lifekit.db"
  [ -f "$db" ] || return 0
  command -v sqlite3 >/dev/null 2>&1 || return 0

  local target cur
  target="$(extract_ver_lifekit_py "$repo/database/lifekit.py" 2>/dev/null)" || return 0
  cur="$(sqlite3 "$db" 'PRAGMA user_version;' 2>/dev/null)" || return 0
  [[ "$target" =~ ^[0-9]+$ ]] || return 0
  [[ "$cur"    =~ ^[0-9]+$ ]] || return 0

  if [ "$cur" -le "$target" ]; then
    msg "[update] 롤백 체크포인트 통과: DB v${cur} <= 타깃 핀 v${target} -- 버전 스탬프(롤백 완료 마커) 기록" \
        "[update] rollback checkpoint passed: DB v${cur} <= target pin v${target} -- writing the version stamp (rollback-complete marker)"
    return 0
  fi

  # DB is AHEAD of the rolled-back code. Locate a verified matching snapshot:
  # newest lifekit.db.v<target>.bak-* whose OPENED pragma equals the target
  # and whose integrity check passes (filename stamp is never trusted alone).
  # Glob + -nt comparison (no ls parsing: space-safe, portable).
  local snap="" f v ic
  for f in "$inst/database/lifekit.db.v${target}.bak-"*; do
    [ -f "$f" ] || continue
    case "$f" in *-wal|*-shm) continue ;; esac
    if [ -n "$snap" ] && [ ! "$f" -nt "$snap" ]; then
      continue
    fi
    v="$(snapshot_pragma "$f" user_version)"
    [ "$v" = "$target" ] || continue
    ic="$(snapshot_pragma "$f" integrity_check)"
    [ "$ic" = "ok" ] || continue
    snap="$f"
  done

  if [ -z "$snap" ]; then
    # No verified snapshot: classify the delta migrations (target, cur].
    # Source: the instance-local migrations dir when present (it carries the
    # bad release's migrations; the TARGET tree's dir ends at the target
    # version by definition), else the target tree's dir. A migration file
    # or its '-- reversible:' marker missing = unclassifiable = treated as
    # irreversible (fail-closed).
    local mig_src="$inst/database/migrations" irrev=0 n nnn mig marker found
    [ -d "$mig_src" ] || mig_src="$repo/database/migrations"
    for ((n = target + 1; n <= cur; n++)); do
      nnn="$(printf '%03d' "$n")"
      found=""
      for mig in "$mig_src/${nnn}_"*.sql; do
        [ -f "$mig" ] && { found="$mig"; break; }
      done
      if [ -z "$found" ]; then
        irrev=1
        break
      fi
      marker="$(sed -n 's/^-- reversible:[[:space:]]*//p' "$found" | head -n1)"
      if [ "$marker" != "yes" ]; then
        irrev=1
        break
      fi
    done
    if [ "$irrev" = "1" ]; then
      printf '%s\n' "============================================================" >&2
      msg "[update][거부] 롤백 불가: 비가역(또는 분류 불가) 마이그레이션 경계를 넘는데, 검증된 스냅샷이 없습니다" \
          "[update][REFUSE] rollback blocked: the delta crosses an irreversible (or unclassifiable) migration and NO verified snapshot exists" >&2
      msg "  DB v${cur} -> 타깃 v${target}; 필요한 스냅샷: lifekit.db.v${target}.bak-* (열어서 검증됨)" \
          "  DB v${cur} -> target v${target}; needed: a lifekit.db.v${target}.bak-* snapshot that verifies by open" >&2
      msg "  스키마 수기 편집 금지. DGN-672 복원 스토리로 에스컬레이션하세요 (database/restore-data.sh --list 로 다른 복원점 확인)." \
          "  Never hand-edit schema. Escalate to the DGN-672 restore story (check database/restore-data.sh --list for other restore points)." >&2
      msg "  치유: 핀을 제거하고 정방향 업데이트를 재실행하면 트리가 원래 버전으로 복귀합니다." \
          "  To heal: remove the pin and re-run a forward update to bring the tree back." >&2
      printf '%s\n' "============================================================" >&2
      die "rollback refused: irreversible migration boundary in (v${target}, v${cur}] without a verified snapshot (DGN-673 S1)"
    fi
    printf '%s\n' "============================================================" >&2
    msg "[update] 롤백 체크포인트: DB가 타깃보다 앞서 있고 (v${cur} > v${target}) 일치하는 검증된 스냅샷이 없습니다" \
        "[update] rollback checkpoint: DB ahead of target (v${cur} > v${target}) and no verified matching snapshot found" >&2
    msg "  복원점 탐색: database/restore-data.sh --list (git 덤프 복원점 사용 가능; 델타는 전부 가역)" \
        "  Locate a restore point: database/restore-data.sh --list (git-dump points usable; the delta is all-reversible)" >&2
    msg "  복원 후 동일한 핀 업데이트를 재실행하면 이 체크포인트를 통과하고 스탬프가 기록됩니다." \
        "  After restoring, re-run the same pinned update to pass this checkpoint and seal the stamp." >&2
    printf '%s\n' "============================================================" >&2
    exit 3
  fi

  printf '%s\n' "============================================================" >&2
  msg "[update] 롤백 체크포인트: 트리 동기화 완료, DB 복원 대기 (DB v${cur} > 타깃 v${target}) -- 스탬프 보류" \
      "[update] rollback checkpoint: tree synced, DB restore pending (DB v${cur} > target v${target}) -- stamp withheld" >&2
  msg "  다음 단계 (DGN-672 경로; 이 스크립트는 DB를 직접 되돌리지 않습니다):" \
      "  Next step (DGN-672 path; this script never restores the DB itself):" >&2
  printf '  database/restore-data.sh --to %s --no-catchup\n' "$snap" >&2
  msg "  복원 후 동일한 핀 롤백 업데이트를 재실행하세요 -- 그 실행이 체크포인트를 통과하고 스탬프(롤백 완료 마커)를 기록합니다." \
      "  Then re-run the SAME pinned rollback update -- that run passes this checkpoint and writes the stamp (rollback-complete marker)." >&2
  printf '%s\n' "============================================================" >&2
  exit 3
}

# ---------------------------------------------------------------------------
# INSTANCE-PRESERVE LIST (DGN-359): protect instance-local customizations from
# the framework refresh. Three live clobber incidents (DGN-290, DGN-359,
# DGN-363: Ag mirror down 5.5h) share one cause -- update.sh overwrote files
# an instance had deliberately customized. The structural fix has two halves:
#
#   1. HOOKS SPLIT: .claude/settings.json is framework-owned (this script may
#      rewrite it wholesale); instance-added hooks live in
#      .claude/settings.local.json, which Claude Code merges natively and this
#      script NEVER writes. No code is needed for that half -- nothing below
#      touches settings.local.json; this comment is the greppable guard.
#      Do not add settings.local.json to any refresh path.
#
#   2. PRESERVE LIST (this section): $INSTANCE/.claude/.dogany-preserve is an
#      OPTIONAL, instance-owned file listing instance-root-relative paths that
#      update.sh must not overwrite. Format: one path per line; '#' comments
#      and blank lines ignored; a trailing '/' preserves a whole directory.
#      Example:
#          routines/cron-guard.sh          # local patch not yet upstreamed
#          routines/bundle/                # whole dir
#      Mechanism: entries become anchored rsync --exclude patterns for the
#      section-3 rsync blocks, and skip checks for the single-file cp blocks.
#      The active list is printed on every run so preserved drift stays
#      visible, and entries missing on disk are flagged (typo nag).
#
#      Why an explicit list (not divergence detection / 3-way merge): the
#      placeholder re-substitution (section 4) makes EVERY instance file
#      differ from its template source, so naive checksum comparison
#      false-positives on all files; a post-install sha manifest across all
#      synced dirs or a 3-way merge is heavy machinery for the same outcome.
#      An explicit list is zero-false-positive, auditable, and matches this
#      script's allowlist philosophy. The known cost: it is opt-in -- a local
#      customization is protected only once it is registered. Protocol: any
#      live instance patch that diverges from the framework template MUST add
#      its path here in the same change.
#
#      Deliberately NOT covered: RULES.md (framework constitution, has its own
#      edit-detect + backup channel, section 3k) and skills/dogany-* (own
#      backup-on-modify channel, section 3i -- never silently clobbered).
#
#      DGN-907 pin expiry: an entry line may carry an inline annotation
#      "prune-after: vX.Y.Z" in its trailing comment, naming the release whose
#      consumption is expected to absorb the protected divergence:
#          bridge/formatting.py   # local HTML work -- prune-after: v1.23.0
#      Once THIS run consumes a version >= the annotation, the pin is EXPIRED:
#      it is auto-invalidated for the run (the vendor file re-lands) provided
#      the incoming vendor payload passes the shell-rail contract smoke; a
#      failing payload keeps the pin and warns hard (back-land first). See
#      preserve_expiry_reconcile below. Un-annotated entries keep the old
#      manual-prune-only behavior unchanged.
# ---------------------------------------------------------------------------
PRESERVE_FILE="$INSTANCE/.claude/.dogany-preserve"
PRESERVE_ENTRIES=()
# DGN-907: parallel arrays (bash-3.2: no associative arrays) recording each
# annotated entry path and its prune-after target version (leading 'v' dropped).
PRESERVE_PIN_PATHS=()
PRESERVE_PIN_VERS=()
# _dgn907_prune_after LINE -> print the prune-after target version from a raw
# preserve line's trailing comment ("# ... prune-after: v1.23.0" -> "1.23.0");
# empty when the line carries no (valid) annotation.
_dgn907_prune_after() {
  printf '%s' "$1" \
    | sed -n 's/.*prune-after:[[:space:]]*v\{0,1\}\([0-9][0-9.]*\).*/\1/p'
}
if [ -f "$PRESERVE_FILE" ]; then
  while IFS= read -r _pline || [ -n "$_pline" ]; do
    _praw="$_pline"
    _pline="${_pline%%#*}"
    # Trim surrounding whitespace (bash-3.2-safe).
    _pline="$(printf '%s' "$_pline" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$_pline" ] || continue
    _pline="${_pline#./}"
    _pline="${_pline#/}"
    case "$_pline" in
      *..*)
        msg "[update][경고] 보존 목록의 안전하지 않은 항목 무시: $_pline" \
            "[update][WARN] ignoring unsafe preserve entry: $_pline" >&2
        continue ;;
    esac
    PRESERVE_ENTRIES+=("$_pline")
    # DGN-907: capture the prune-after annotation from THIS entry's trailing
    # comment (entry lines only -- a pure comment line never reaches here).
    case "$_praw" in
      *prune-after:*)
        _pver="$(_dgn907_prune_after "$_praw")"
        if [ -n "$_pver" ]; then
          PRESERVE_PIN_PATHS+=("$_pline")
          PRESERVE_PIN_VERS+=("$_pver")
        fi
        ;;
    esac
  done < "$PRESERVE_FILE"
fi

if [ "${#PRESERVE_ENTRIES[@]}" -gt 0 ]; then
  msg "[update] 인스턴스 보존 목록 활성 (.claude/.dogany-preserve): ${#PRESERVE_ENTRIES[@]}개 항목은 갱신하지 않습니다:" \
      "[update] instance preserve list active (.claude/.dogany-preserve): ${#PRESERVE_ENTRIES[@]} entries will NOT be refreshed:"
  for _pe in "${PRESERVE_ENTRIES[@]}"; do
    if [ -e "$INSTANCE/$_pe" ]; then
      printf '  - %s\n' "$_pe"
    else
      msg "  - $_pe  [경고: 디스크에 없음 -- 오타?]" \
          "  - $_pe  [WARN: not on disk -- typo?]"
    fi
  done
fi

# is_preserved RELPATH -> 0 when RELPATH (instance-root-relative) is on the
# preserve list: exact file match, or under a trailing-slash directory entry.
# DGN-385: records the matching entry in _ALL_MATCHED_ENTRIES so it is not
# flagged as invalid by _preserve_check_invalid.
is_preserved() {
  local rel="$1" e
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    if [ "$e" = "$rel" ]; then
      _ALL_MATCHED_ENTRIES+=("$e")
      return 0
    fi
    case "$e" in
      */) case "$rel" in "$e"*) _ALL_MATCHED_ENTRIES+=("$e"); return 0 ;; esac ;;
    esac
  done
  return 1
}

# build_preserve_excludes PREFIX -- fill the global array PEX with rsync
# --exclude args for preserve entries under the instance-relative dir PREFIX
# (no trailing slash). Patterns are anchored ("/rel/path") to the rsync
# transfer root, which the section-3 blocks always set to $INSTANCE/PREFIX/.
# Callers expand it with the bash-3.2-safe empty-array idiom:
#   ${PEX[@]+"${PEX[@]}"}
#
# DGN-385: sets SECTION_HELD=1 (and adds entry to _ALL_MATCHED_ENTRIES) when
# the preserve list contains the section root itself ("PREFIX/").  Callers
# must check SECTION_HELD immediately after calling this function and skip the
# rsync entirely when it is set.  Also records file-level entries that were
# matched so that invalid (unmatched) entries can be flagged later.
PEX=()
SECTION_HELD=0
_ALL_MATCHED_ENTRIES=()
build_preserve_excludes() {
  local prefix="$1" e rel
  PEX=()
  SECTION_HELD=0
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    # DGN-385: section-root hold -- entry names the section dir itself.
    if [ "$e" = "${prefix}/" ]; then
      SECTION_HELD=1
      _ALL_MATCHED_ENTRIES+=("$e")
      continue
    fi
    case "$e" in
      "$prefix"/?*)
        rel="${e#"$prefix"/}"
        PEX+=(--exclude "/$rel")
        _ALL_MATCHED_ENTRIES+=("$e")
        ;;
    esac
  done
}

# _section_held_warn PREFIX SRC_DIR DEST_DIR [EXTRA_RSYNC_OPTS...]
# Common helper called by every section that uses build_preserve_excludes when
# SECTION_HELD=1 (the preserve list names the section root itself, e.g.
# "routines/").  Runs an itemized dry-run to count pending changes (so the
# operator knows the blast radius), prints the HELD WARN, and returns 0.
# Guards: COMMON_EXCLUDES applied so noise (venv/.env/etc.) is excluded from
# the count.
_section_held_warn() {
  local prefix="$1" src="$2" dest="$3"
  shift 3
  local _n=0
  _n="$(rsync -rcn --itemize-changes "${COMMON_EXCLUDES[@]}" \
      "$@" "$src/" "$dest/" 2>/dev/null \
      | grep -c '^[>c]f' || true)"
  printf '%s\n' "============================================================" >&2
  msg "[update][경고] HELD by .dogany-preserve: ${prefix}/ (differs from vendor in ${_n} files)" \
      "[update][WARN] HELD by .dogany-preserve: ${prefix}/ (differs from vendor in ${_n} files)" >&2
  msg "  ${prefix}/ rsync 전체를 건너뜁니다. .dogany-preserve에서 항목을 제거하면 갱신이 재개됩니다." \
      "  Skipping ${prefix}/ rsync entirely. Remove the entry from .dogany-preserve to resume updates." >&2
  printf '%s\n' "============================================================" >&2
}

# _preserve_check_invalid -- warn about any preserve entry that was never
# matched by build_preserve_excludes.  An unmatched entry either names a
# non-existent section or contains a typo, and would silently provide no
# protection at all (DGN-385).
# Called once, after all section-3 rsync blocks complete.
_preserve_check_invalid() {
  local e matched found
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    found=0
    for matched in ${_ALL_MATCHED_ENTRIES[@]+"${_ALL_MATCHED_ENTRIES[@]}"}; do
      [ "$matched" = "$e" ] && { found=1; break; }
    done
    if [ "$found" = "0" ]; then
      printf '%s\n' "============================================================" >&2
      msg "[update][경고] .dogany-preserve 항목이 어떤 섹션과도 매칭되지 않음 -- 오타?" \
          "[update][WARN] .dogany-preserve entry matched no section -- typo?" >&2
      msg "  항목: $e" \
          "  entry: $e" >&2
      msg "  이 항목은 아무 파일도 보호하지 않습니다. 경로를 확인하세요." \
          "  This entry protects no files. Check the path." >&2
      printf '%s\n' "============================================================" >&2
    fi
  done
}

# ---------------------------------------------------------------------------
# DGN-907: preserve-pin expiry + shell-rail contract smoke.
#
# Root cause class (DGN-906): a .dogany-preserve pin on bridge/formatting.py
# outlived its stated purpose (the divergence it protected shipped in
# canonical releases ago) and kept freezing the file, so routines/push.sh's
# sanitize import (`from bridge.formatting import sanitize_message_for_
# telegram`) broke lockstep with the vendored bridge -- every shell-rail push
# silently degraded to plain text with raw HTML tags. Two structural gates:
#   1. pin expiry (preserve_expiry_reconcile): an annotated pin whose
#      prune-after release is consumed is force-invalidated (hybrid model,
#      owner decision 2026-08-16) -- gated on the INCOMING payload passing
#      contract_smoke, so a re-land can never install a rail-breaking file.
#   2. post-flight contract gate (end of run): the LIVE instance must satisfy
#      the push.sh sanitize contract after reconcile, or the update FAILS
#      (exit 4) before the version stamp, with a warning push.
# ---------------------------------------------------------------------------

# _dgn907_ver_le A B -> 0 when dotted version A <= B (numeric per field;
# missing fields count as 0). awk keeps it portable (no sort -V dependency).
_dgn907_ver_le() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".");
    k = (n > m) ? n : m;
    for (i = 1; i <= k; i++) {
      p = ((i <= n) ? x[i] : 0) + 0; q = ((i <= m) ? y[i] : 0) + 0;
      if (p < q) exit 0;
      if (p > q) exit 1;
    }
    exit 0;
  }'
}

# _contract_python -> print the interpreter push.sh's sanitize hop would use
# (empty = none available). MUST mirror routines/push.sh resolution exactly:
# instance bridge venv, else BRIDGE_PYTHON, else python3 on PATH.
_contract_python() {
  if [ -x "$INSTANCE/bridge/venv/bin/python" ]; then
    printf '%s' "$INSTANCE/bridge/venv/bin/python"
  elif [ -n "${BRIDGE_PYTHON:-}" ]; then
    printf '%s' "$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s' "python3"
  fi
}

# contract_smoke ROOT -> 0 when the shell-rail sanitize contract holds against
# ROOT (a dir containing bridge/): the EXACT push.sh import resolves and
# renders markdown ("**x**" -> "<b>x</b>"). When the interpreter can import
# pytest and ROOT carries the DGN-822 contract test, the full test file runs
# too (both must pass -- it additionally pins the telegram-free invariant).
# No interpreter at all -> FAIL (an unverifiable contract is not a pass;
# callers that want a softer degrade check _contract_python themselves).
contract_smoke() {
  local root="$1" py tf
  py="$(_contract_python)"
  [ -n "$py" ] || return 1
  "$py" -c '
import sys
sys.path.insert(0, sys.argv[1])
from bridge.formatting import sanitize_message_for_telegram
out = sanitize_message_for_telegram("**x**")
sys.exit(0 if "<b>x</b>" in out else 1)
' "$root" >/dev/null 2>&1 || return 1
  tf="$root/bridge/tests/test_dgn822_formatting_telegram_free.py"
  if [ -f "$tf" ] && "$py" -c "import pytest" >/dev/null 2>&1; then
    "$py" -m pytest -q -p no:cacheprovider "$tf" >/dev/null 2>&1 || return 1
  fi
  return 0
}

# preserve_expiry_reconcile -- DGN-907 hybrid pin-expiry pass (owner decision
# 2026-08-16). For every annotated pin whose prune-after target is <= the
# version THIS run consumes (REPO_VERSION), the pin is EXPIRED:
#   * incoming vendor payload ($TEMPLATE) passes contract_smoke -> the entry
#     is dropped from PRESERVE_ENTRIES for this run (auto-invalidate; the
#     canonical file re-lands through the normal channels) + a prune nudge.
#   * smoke FAILS (or no interpreter) -> the pin STAYS and a hard warning
#     names the un-absorbed divergence: back-land into canonical first, then
#     prune. Landing is gated on a PASSING smoke -- never on hope.
# The .dogany-preserve file itself is never edited (instance-owned): an
# invalidated entry nags every run until the operator deletes the line.
# The smoke verdict is computed once per run (memoized) -- it tests the
# template payload, which is identical for every entry.
# Interplay with the DGN-593 bridge reconcile: invalidation only re-opens the
# landing path. A bridge file whose on-disk bytes genuinely diverged from the
# last-installed manifest sha still classifies as CONFLICT there (never a
# silent clobber of local work); the post-flight contract gate then fails the
# run loudly until the operator back-lands or --bridge-accept's -- expiry can
# force RESOLUTION, but never a silent overwrite.
# Uses globals: PRESERVE_ENTRIES, PRESERVE_PIN_PATHS/VERS, REPO_VERSION,
# TEMPLATE. Mutates: PRESERVE_ENTRIES.
preserve_expiry_reconcile() {
  [ "${#PRESERVE_PIN_PATHS[@]}" -gt 0 ] || return 0
  [ "$REPO_VERSION" != "unknown" ] || return 0
  local i=0 pp pv verdict="" e hit
  _DGN907_DROP=()
  while [ "$i" -lt "${#PRESERVE_PIN_PATHS[@]}" ]; do
    pp="${PRESERVE_PIN_PATHS[$i]}"
    pv="${PRESERVE_PIN_VERS[$i]}"
    i=$((i + 1))
    _dgn907_ver_le "$pv" "$REPO_VERSION" || continue
    if [ -z "$verdict" ]; then
      if contract_smoke "$TEMPLATE"; then verdict="pass"; else verdict="fail"; fi
    fi
    if [ "$verdict" = "pass" ]; then
      _DGN907_DROP+=("$pp")
      printf '%s\n' "============================================================"
      msg "[update] 보존 핀 만료: $pp (prune-after v$pv <= v$REPO_VERSION 소비됨)" \
          "[update] preserve pin EXPIRED: $pp (prune-after v$pv <= v$REPO_VERSION consumed)"
      msg "  이번 실행에서 핀을 자동 해제합니다 -- 정본 착지 경로가 다시 열립니다 (수신 파일 계약 스모크 통과; 실분기 파일은 bridge reconcile CONFLICT로 표면화)." \
          "  auto-invalidated for this run -- the canonical landing path re-opens (incoming payload passed the contract smoke; a genuinely diverged file surfaces as a bridge reconcile CONFLICT instead of landing silently)."
      msg "  .claude/.dogany-preserve 에서 이 줄을 삭제하면 이 알림이 사라집니다." \
          "  delete this line from .claude/.dogany-preserve to silence this notice."
      printf '%s\n' "============================================================"
    else
      printf '%s\n' "============================================================" >&2
      msg "[update][경고] 보존 핀 만료됐지만 해제 보류: $pp" \
          "[update][WARN] preserve pin EXPIRED but NOT released: $pp" >&2
      msg "  prune-after v$pv 는 소비됐지만, 수신될 정본 파일이 shell-rail 계약 스모크에 실패했습니다." \
          "  prune-after v$pv is consumed, yet the incoming vendor payload FAILS the shell-rail contract smoke." >&2
      msg "  핀을 유지합니다(재착지 안 함). 로컬 분기분을 정본에 back-land 한 뒤 항목을 정리하세요." \
          "  keeping the pin (no re-land). Back-land the local divergence into canonical, then prune the entry." >&2
      printf '%s\n' "============================================================" >&2
    fi
  done
  [ "${#_DGN907_DROP[@]}" -gt 0 ] || return 0
  _DGN907_KEEP=()
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    hit=0
    for pp in "${_DGN907_DROP[@]}"; do
      [ "$e" = "$pp" ] && { hit=1; break; }
    done
    [ "$hit" = "1" ] || _DGN907_KEEP+=("$e")
  done
  PRESERVE_ENTRIES=(${_DGN907_KEEP[@]+"${_DGN907_KEEP[@]}"})
  return 0
}

# Content digest of a single file (DGN-130 RULES channel). Dereferences symlinks
# (the template's RULES.md is a symlink into rules/); missing file -> stable
# empty marker so a fresh instance and a deleted file both compare cleanly.
# HOISTED above section 3a (DGN-593): the bridge per-file reconcile needs it;
# the original definition sat below 3a (subst_one hoist precedent).
file_checksum() {
  local f="$1"
  [ -f "$f" ] || { printf '%s\n' "d41d8cd98f00b204e9800998ecf8427e-empty"; return; }
  shasum < "$f" 2>/dev/null | awk '{print $1}'
}

# Bridge per-file manifest (DGN-593 design B): records the sha of every
# bridge/ file as this script LAST INSTALLED it ("what we last laid down" = M),
# the DGN-130 skills-manifest pattern extended to bridge/ with the policy
# reversed (preserve wins -- bridge is live code). Format: "<relpath>  <sha>",
# relpath instance-root-relative (e.g. "bridge/dashboard.py"). Kept separate
# from the skills/framework manifests so the channels never race on one file.
BRIDGE_MANIFEST="$INSTANCE/.claude/.dogany-bridge.sha"
bridge_manifest_sha() {
  local rel="$1"
  [ -f "$BRIDGE_MANIFEST" ] || { printf '%s' ""; return; }
  awk -v n="$rel" '$1==n {print $2; exit}' "$BRIDGE_MANIFEST"
}

# DGN-677 adopt-on-preserve tag. A class-5 (bootstrap, no manifest) code file is
# adopted into M with its current on-disk sha but MARKED provisional via a third
# inline token so the next run has a 3-way baseline yet does NOT trust "I==M =>
# unedited". Tag string is a fixed contract: one space + this exact $3 token.
# Both manifest readers ignore $3 (bridge_manifest_sha reads $2, the universe awk
# reads $1), so the tag never perturbs sha lookup or file discovery.
BRIDGE_PROVISIONAL_TAG='#adopted-provisional'
# True when this instance-relative path is recorded provisional in M
# (relpath in $1, sha in $2, tag in $3).
bridge_is_provisional() {
  local rel="$1"
  [ -f "$BRIDGE_MANIFEST" ] || return 1
  awk -v n="$rel" -v t="$BRIDGE_PROVISIONAL_TAG" \
    '$1==n && $3==t {found=1; exit} END{exit !found}' "$BRIDGE_MANIFEST"
}

# ---------------------------------------------------------------------------
# DGN-757: vendor-ancestry proof for bridge files with NO trusted 3-way
# baseline. An instance whose bridge/ files were seeded by an init-era commit
# (DGN-625 private continuity git-home) instead of by update.sh carries no
# manifest entry: every code file classifies as row 5 (bootstrap), the
# DGN-677 adopt-provisional keeps the STALE on-disk seed, and run 2 escalates
# to CONFLICT -- the render layer freezes forever unless an operator
# hand-runs --bridge-accept (observed live: Skull bridge/formatting.py stuck
# at its init-era version while core advanced -> raw-HTML symptom). Fix:
# PROVE the on-disk file is a pristine stale vendor copy by matching its
# exact bytes (git blob id) against the framework repo's history of the SAME
# template path. A byte-exact match against a released vendor generation
# means the file carries ZERO local edits, so landing the current vendor
# version is lossless drift -- auto-land it (with a belt-and-braces backup).
# No match (genuine/unknown edit) -> existing adopt-provisional / CONFLICT
# semantics stay untouched; no history reachable (tarball install, no .git)
# -> same safe degrade. A user-modified file can therefore never be
# auto-clobbered by this path: its bytes exist in no vendor generation.
BRIDGE_ANC_LOADED=0
BRIDGE_ANC_GITROOT=""
BRIDGE_ANC_INDEX=""
bridge_ancestry_index() {
  # Build (once per run, lazily) the "blob-id template-path" index over the
  # whole history of agents/.template/bridge/. History source: the shared
  # framework checkout -- DOGANY_REPO_ROOT from .instance.conf (sourced in
  # the identity section above; release-channel child runs execute from an
  # extracted tag tree that has no .git), falling back to this script's own
  # repo root (channel main / direct dev runs).
  if [ "$BRIDGE_ANC_LOADED" = "1" ]; then return 0; fi
  BRIDGE_ANC_LOADED=1
  if [ -n "${DOGANY_REPO_ROOT:-}" ] && [ -e "${DOGANY_REPO_ROOT}/.git" ]; then
    BRIDGE_ANC_GITROOT="$DOGANY_REPO_ROOT"
  elif [ -e "$REPO_ROOT/.git" ]; then
    BRIDGE_ANC_GITROOT="$REPO_ROOT"
  else
    return 0
  fi
  # --raw new-blob column ($4); deletion rows (all-zero new blob) dropped.
  # --all so every vendored generation (any branch, not only released tags)
  # is eligible, not just the current branch tip; --no-renames keeps one
  # path per row ($6). (tag-only scope = DGN-757 follow-up.)
  BRIDGE_ANC_INDEX="$(git -C "$BRIDGE_ANC_GITROOT" log --all --format= --raw \
      --no-abbrev --no-renames -- agents/.template/bridge/ 2>/dev/null \
    | awk '$1 ~ /^:/ && $4 !~ /^0+$/ { print $4, $6 }' | LC_ALL=C sort -u)" || true
}
# True when instance bridge/<rel> ($1, bridge-relative) is byte-identical to
# SOME historical vendored version of the same template path -- i.e. provably
# a pristine (unedited) vendor seed, not a user edit.
bridge_vendor_ancestor() {
  local rel="$1" blob
  bridge_ancestry_index
  if [ -z "$BRIDGE_ANC_INDEX" ]; then return 1; fi
  blob="$(git -C "$BRIDGE_ANC_GITROOT" hash-object "$INSTANCE/bridge/$rel" 2>/dev/null)" || return 1
  if [ -z "$blob" ]; then return 1; fi
  printf '%s\n' "$BRIDGE_ANC_INDEX" \
    | grep -qxF "$blob agents/.template/bridge/$rel"
}

# ---------------------------------------------------------------------------
# ROLLBACK MODE banner (DGN-673 B3): announce before the source resolution /
# pre-flight so the operator sees the mode before anything is written. Runs
# in every process (parent and --no-pull child); the exported guard inside
# keeps it to one print per operator run.
rollback_banner

# 1) Resolve the update source (DGN-593 A3 + DGN-221).
#    Instances consume release tags (v*), never main HEAD -- pushing dev
#    commits to main must not stealth-patch users whose VERSION still shows
#    the last release. Channel "release" NEVER moves the shared checkout:
#    it fetches tags, extracts the latest v* tag into a private per-run temp
#    dir (`git archive`), and re-invokes the extracted tree's update.sh.
#    Escape hatch for development checkouts: DOGANY_UPDATE_CHANNEL=main
#    restores the old `git pull --ff-only`, guarded to the main branch.
# ---------------------------------------------------------------------------
if [ "$DO_PULL" = "1" ]; then
  if [ -d "$REPO_ROOT/.git" ]; then
    # DGN-673 B2: pin persistence for DIRECT update.sh invocation -- env wins,
    # $INSTANCE/.instance.conf fallback; loud PINNED banner on every pinned
    # run. --no-pull child runs never reach here (parent announced the pin).
    resolve_update_pin "$INSTANCE/.instance.conf" "${DOGANY_UPDATE_CHANNEL:-release}"
    if [ "${DOGANY_UPDATE_CHANNEL:-release}" = "main" ]; then
      # Main-branch guard (DGN-593 D3): pull only when the shared repo is
      # actually ON main. Detached HEAD / dev branch means a dev session is
      # in progress -- die with a hint, never auto-switch.
      _cur_branch="$(git -C "$REPO_ROOT" symbolic-ref -q --short HEAD || true)"
      if [ "$_cur_branch" != "main" ]; then
        die "shared repo not on main (on '${_cur_branch:-detached}') -- finish the dev session / restore main, then retry"
      fi
      msg "[update] git pull (channel=main) ..." "[update] git pull (channel=main) ..."
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] git pull 생략" "  [dry-run] skipping git pull"
      else
        git -C "$REPO_ROOT" pull --ff-only \
          || die "git pull failed (resolve manually, or re-run with --no-pull)"
      fi
    else
      # Release channel (DGN-593 A3): NEVER move the shared checkout. Read
      # only: fetch tags, extract the latest v* tag into a private temp dir
      # via `git archive`, then re-invoke THAT tree's update.sh with the
      # original argv (+ --no-pull). The extracted tree carries no .git, so
      # the re-invoked run's pull section is a natural no-op (the [ -d .git ]
      # loop guard); --no-pull is the second belt. NO exec: the EXIT trap
      # below must survive to clean the temp dir.
      if [ "$DRY_RUN" = "1" ]; then
        # Dry-run: no network -- preview from the LOCAL latest tag. The
        # extraction is a private temp dir (trap-cleaned), so the "dry-run
        # writes nothing to the instance/manifests" contract holds while the
        # preview becomes tag-accurate (M3).
        msg "  [dry-run] git fetch 생략 -- 로컬 최신 태그로 프리뷰" \
            "  [dry-run] skipping git fetch -- previewing from local latest tag"
      else
        msg "[update] 최신 릴리스 태그 확인 ..." "[update] resolving latest release tag ..."
        git -C "$REPO_ROOT" fetch --tags origin \
          || die "git fetch failed (resolve manually, or re-run with --no-pull)"
      fi
      # Channel-aware target tag (DGN-621 v2 phase1). release channel = highest
      # STABLE tag (pre-release excluded); dev channel includes pre-release;
      # DOGANY_UPDATE_PIN overrides (fails loud if the pinned tag is missing).
      # Stable-only estate w/o PIN on release/unset == the old one-liner result.
      LATEST_TAG="$(resolve_channel_tag "$REPO_ROOT" "${DOGANY_UPDATE_CHANNEL:-release}")"
      [ -n "$LATEST_TAG" ] || die "no release tag (v*) found -- cannot resolve a published release"
      SRC="$(mktemp -d "${TMPDIR:-/tmp}/dogany-src.XXXXXX")" \
        || die "mktemp failed for release source extraction"
      # Trap installed IMMEDIATELY after mktemp, before any die below, so the
      # temp source can never leak.
      trap 'rm -rf "$SRC"' EXIT INT TERM
      git -C "$REPO_ROOT" archive --format=tar "$LATEST_TAG" > "$SRC/src.tar" \
        || die "git archive $LATEST_TAG failed in $REPO_ROOT"
      tar -xf "$SRC/src.tar" -C "$SRC" \
        || die "tar extraction of $LATEST_TAG failed"
      rm -f "$SRC/src.tar"
      # Extraction verification gate: never consume a half-extracted tree
      # (also re-confirms the no-.gitattributes/export-ignore premise each run).
      if [ ! -f "$SRC/update.sh" ] || [ ! -d "$SRC/agents/.template" ]; then
        die "extracted source incomplete ($LATEST_TAG): $SRC"
      fi
      msg "[update] 릴리스 소스 추출: $LATEST_TAG (임시 소스)" \
          "[update] release source extracted: $LATEST_TAG (private temp source)"
      _rc=0
      # DGN-603 SF1: invoke via `bash` (not the extracted file's exec bit) so a
      # noexec TMPDIR mount on a hardened host cannot block the run.
      bash "$SRC/update.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"} --no-pull || _rc=$?
      exit "$_rc"
    fi
  else
    msg "[update] .git 없음 -> pull 건너뜀" "[update] no .git -> skipping pull"
  fi
fi

REPO_VERSION="unknown"
[ -f "$REPO_ROOT/VERSION" ] && REPO_VERSION="$(head -n1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
msg "[update] 프레임워크 버전 = $REPO_VERSION" "[update] framework version = $REPO_VERSION"

# DGN-907: pin-expiry reconcile MUST run here -- after the consumed version is
# known, before ANY section-3 consumer of PRESERVE_ENTRIES (first consumer is
# the 3a bridge reconcile). An expired pin invalidated here simply stops
# excluding its file, so the re-land flows through the normal channels.
preserve_expiry_reconcile

# ---------------------------------------------------------------------------
# 2) Recover instance identity (for placeholder re-substitution).
#    Prefer the manifest written by mint.sh; fall back to plist-derived name.
# ---------------------------------------------------------------------------
AGENT_NAME=""; AGENT_LABEL=""; USER_LABEL=""; AGENT_PREFIX=""
AGENT_LANG="$(grep -E "^AGENT_LANG=" "$INSTANCE/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_LANG="${AGENT_LANG:-en}"
if [ -f "$INSTANCE/.instance.conf" ]; then
  # shellcheck disable=SC1090
  . "$INSTANCE/.instance.conf"
  AGENT_NAME="${DOGANY_AGENT_NAME:-}"
  AGENT_LABEL="${DOGANY_AGENT_LABEL:-}"
  USER_LABEL="${DOGANY_USER_LABEL:-}"
  # DOGANY_AGENT_PREFIX: optional field (absent on pre-DGN-213 instances).
  # Fall back to generic "[agent]" so old installs without the field get a safe
  # substitution rather than an empty string or a crash.
  AGENT_PREFIX="${DOGANY_AGENT_PREFIX:-[agent]}"
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
  # Reachable when .instance.conf is missing/incomplete. A wholly missing
  # .instance.conf now only gets here under --force (the validity gate above
  # dies otherwise); a present-but-incomplete manifest still lands here. Either
  # way we skip identity substitution rather than write empty labels.
  IDENTITY_OK=0
  msg "[update][경고] 인스턴스 정체성(.instance.conf)을 못 찾음 -- 정체성 플레이스홀더 치환은 건너뜁니다." \
      "[update][WARN] instance identity (.instance.conf) not found -- skipping identity placeholder substitution."
fi

# ---------------------------------------------------------------------------
# 2.5) Pre-flight confirmation. Print a one-line summary of the target instance
#      and the framework version transition, then require an explicit y before
#      the first destructive rsync in section 3. Default is NO. --yes/-y bypasses
#      it; --dry-run skips it (nothing is written). In a non-interactive context
#      (stdin not a TTY) without --yes we refuse rather than proceed blindly.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "0" ] && [ "$ASSUME_YES" = "0" ]; then
  # Instance name from the manifest (DOGANY_AGENT_NAME, sourced above); fall back
  # to the recovered AGENT_NAME slug, else the basename of the instance dir.
  PREFLIGHT_NAME="${DOGANY_AGENT_NAME:-${AGENT_NAME:-$(basename "$INSTANCE")}}"
  CUR_FW="${DOGANY_FW_VERSION:-unknown}"
  msg "[update] 대상: ${PREFLIGHT_NAME}  ($INSTANCE)" \
      "[update] target: ${PREFLIGHT_NAME}  ($INSTANCE)"
  msg "[update] 프레임워크: ${CUR_FW} -> ${REPO_VERSION}" \
      "[update] framework: ${CUR_FW} -> ${REPO_VERSION}"
  if [ -t 0 ]; then
    msg "[update] 이 인스턴스를 업데이트할까요? [y/N] " \
        "[update] Update this instance? [y/N] "
    read -r _reply || _reply=""
    case "$_reply" in
      y|Y|yes|YES) : ;;
      *) die "aborted by user (no changes written)" ;;
    esac
  else
    die "non-interactive stdin and no --yes/-y: refusing to proceed. Re-run with --yes to confirm."
  fi
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

# 3a) bridge code (framework) -- DGN-593 design B: per-file 3-way reconcile.
#
# Two channels inside bridge/:
#
#   (1) SUBSTITUTION CHANNEL (unconditional rsync, as before): the section-4
#       substitution targets that live under bridge/ (self_restart.sh,
#       watchdog_setup.sh, *.plist) plus UPSTREAM.md (pin-doc freshness + the
#       D7 info-log input). Landing must never evaporate for these; a local
#       edit is protected ONLY via .claude/.dogany-preserve registration.
#       Their manifest shas are recorded AFTER section 4 substitution + plist
#       rename (report/diagnostic use, never a landing gate).
#
#   (2) CLASSIFICATION CHANNEL (everything else): 3-way per-file compare of
#       T (template source) / I (instance copy) / M (manifest = what this
#       script last installed, $BRIDGE_MANIFEST). Policy: preserve wins --
#       bridge is live code. Only lossless drift (vendor changed + local
#       unedited) and vendor-new files land automatically; conflicts NEVER
#       auto-land. Escape hatches: --bridge-accept <rel>,
#       --bridge-accept-all (refused while conflicts exist),
#       --force-accept-all -- all with per-file backups before landing.
#
# Guard order: (i) .dogany-preserve section-root hold ("bridge/") skips the
# whole section; per-file preserve entries outrank every classification row.
# The old pin/vrev gate (DGN-385/413) is DEMOTED to an info log (D7 -- keep
# for one release, then remove): it was direction-blind and first-line-only
# on multiline Vendor-rev; landing decisions now come from file-content 3-way.

# True when the bridge-relative path belongs to the substitution channel.
bridge_is_subst_channel() {
  case "$1" in
    self_restart.sh|watchdog_setup.sh|UPSTREAM.md|*.plist) return 0 ;;
  esac
  return 1
}

# Back up an instance file before an accept-landing overwrites it.
# Arg: instance-root-relative path (e.g. bridge/dashboard.py). Backup home is
# .claude/bridge-backups/ (skills-channel precedent: OUTSIDE .claude/skills/).
# Missing source file (deletion rows) -> nothing to back up, no-op.
bridge_backup_file() {
  local relkey="$1" src bak ts
  src="$INSTANCE/$relkey"
  [ -f "$src" ] || return 0
  ts="$(date +%Y%m%d-%H%M%S)"
  bak="$INSTANCE/.claude/bridge-backups/${relkey}.user-$ts"
  mkdir -p "$(dirname "$bak")"
  cp -p "$src" "$bak" || die "failed to back up $relkey before accept"
  msg "  [update] 수락 전 백업: $relkey -> $bak" \
      "  [update] backed up before accept: $relkey -> $bak"
}

# True when --bridge-accept was passed for this instance-relative path.
bridge_accept_requested() {
  local a
  for a in ${BRIDGE_ACCEPT_LIST[@]+"${BRIDGE_ACCEPT_LIST[@]}"}; do
    if [ "$a" = "$1" ]; then return 0; fi
  done
  return 1
}

# Land one classification-channel file template -> instance (real runs only;
# dry-run callers print their own preview line and never reach the copy).
# --checksum: the caller decided to land from a CONTENT compare, so the copy
# must be content-based too -- rsync's size+mtime quick-check false-negatives
# on same-size same-second edits and would silently skip the landing.
bridge_land() {
  local rel="$1"
  if [ "$DRY_RUN" = "1" ]; then return 0; fi
  mkdir -p "$(dirname "$INSTANCE/bridge/$rel")"
  rsync -aL --checksum "$TEMPLATE/bridge/$rel" "$INSTANCE/bridge/$rel"
}

BRIDGE_RECONCILE_RAN=0
BRIDGE_M_LINES=()        # manifest lines carried over verbatim (kept old M)
BRIDGE_M_RECHECK=()      # relkeys re-checksummed from disk AFTER section 4
BRIDGE_M_PROVISIONAL=()  # relkeys adopted this run as #adopted-provisional
                         # (DGN-677): the write block re-attaches the inline tag
                         # onto their BRIDGE_M_RECHECK line so it survives the
                         # per-run manifest rewrite (BRIDGE_M_LINES rebuilds a
                         # line from $1+$2 only and would drop a $3 tag).
BRIDGE_REPORT_LINES=()
BRIDGE_LANDED_N=0
BRIDGE_ADOPTED_N=0       # DGN-677: class-5 adopt-provisional count (NOT landed --
                         # on-disk bytes unchanged, only the manifest seeded).
BRIDGE_PRESERVED_N=0
BRIDGE_CONFLICT_N=0
BRIDGE_CONFLICT_FILES=()
BRIDGE_REPORT_FILE="$INSTANCE/.claude/bridge-reconcile.report"

if [ -d "$TEMPLATE/bridge" ]; then
  build_preserve_excludes "bridge"

  # --- Guard (i): section-root hold via .dogany-preserve ---
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "bridge" "$TEMPLATE/bridge" "$INSTANCE/bridge"
    UPDATED+=("bridge/ (HELD -- skipped by .dogany-preserve)")
  else
    # DGN-603 SF3: the DGN-593 D7 pin/vrev info-log (marked "keep for one
    # release, then remove") is now removed -- landing is decided per-file
    # 3-way and the pin/Vendor-rev comparison was informational only.

    # --- Channel (1): substitution files + UPSTREAM.md, unconditional rsync.
    #     Per-file preserve entries (PEX, anchored) still outrank the channel.
    #     Top-level only, matching the section-4 rename loop (bridge/*.plist).
    #     --checksum: content-based transfer (see bridge_land note); the
    #     substituted instance copies always differ, so they re-land each run
    #     and section 4 re-substitutes -- the pre-DGN-593 contract.
    ensure_dir "$INSTANCE/bridge"
    rsync -aL --checksum $RSYNC_DRY ${PEX[@]+"${PEX[@]}"} \
      --include '/self_restart.sh' --include '/watchdog_setup.sh' \
      --include '/UPSTREAM.md' --include '/*.plist' --exclude '*' \
      "$TEMPLATE/bridge/" "$INSTANCE/bridge/"

    # --- Channel (2): per-file 3-way classification (DGN-593 spec 3-2).
    #     Universe = template files UNION manifest entries; instance-only
    #     files we never installed stay untouched (no --delete policy kept).
    _bridge_universe="$(
      {
        ( cd "$TEMPLATE/bridge" && find . -type f \
            ! -name '.DS_Store' ! -name '*.pyc' ! -name '*.bak.*' \
            ! -name '*.db' ! -name '.env' \
            ! -path './.git/*' ! -path './venv/*' ! -path '*/__pycache__/*' \
            ! -path './runtime/*' ! -path './logs/*' \
            -print | sed 's|^\./||' )
        if [ -f "$BRIDGE_MANIFEST" ]; then
          awk '!/^#/ && $1 ~ /^bridge\// { sub(/^bridge\//, "", $1); print $1 }' \
            "$BRIDGE_MANIFEST"
        fi
      } | LC_ALL=C sort -u
    )"

    # Pass 1: classify every file. NO mutation here -- --bridge-accept-all
    # refusal semantics need the full conflict picture first.
    BRIDGE_CLASSIFIED=()   # entries "class:rel"
    _conflict_rows=()      # class 4/8 relkeys (accept-all refusal check)
    while IFS= read -r _rel; do
      [ -n "$_rel" ] || continue
      if bridge_is_subst_channel "$_rel"; then continue; fi
      _relkey="bridge/$_rel"
      if is_preserved "$_relkey"; then
        msg "  [update] 보존: $_relkey (.dogany-preserve)" \
            "  [update] preserved: $_relkey (.dogany-preserve)"
        _m="$(bridge_manifest_sha "$_relkey")"
        if [ -n "$_m" ]; then BRIDGE_M_LINES+=("$_relkey  $_m"); fi
        continue
      fi
      _t="$TEMPLATE/bridge/$_rel"
      _i="$INSTANCE/bridge/$_rel"
      _m="$(bridge_manifest_sha "$_relkey")"
      _cls=0
      if [ -f "$_t" ]; then
        _t_sha="$(file_checksum "$_t")"
        if [ -f "$_i" ]; then
          _i_sha="$(file_checksum "$_i")"
          if [ "$_i_sha" = "$_t_sha" ]; then
            _cls=1   # in sync
          elif [ -n "$_m" ] && [ "$_i_sha" = "$_m" ]; then
            _cls=2   # vendor changed + local unedited (lossless drift)
          elif [ -n "$_m" ] && [ "$_t_sha" = "$_m" ]; then
            _cls=3   # local edit + vendor unchanged
          elif [ -n "$_m" ]; then
            _cls=4   # both sides changed (conflict)
          else
            _cls=5   # no manifest (bootstrap): unverified edit
          fi
        else
          if [ -z "$_m" ]; then
            _cls=6   # vendor-new file
          elif [ "$_t_sha" = "$_m" ]; then
            _cls=7   # local deletion + vendor unchanged
          else
            _cls=8   # local deletion + vendor changed (conflict)
          fi
        fi
      else
        if [ -f "$_i" ]; then _cls=9; else _cls=10; fi
      fi
      BRIDGE_CLASSIFIED+=("${_cls}:${_rel}")
      case "$_cls" in
        4|8) _conflict_rows+=("$_relkey") ;;
      esac
    done <<< "$_bridge_universe"

    # --bridge-accept-all refusal (spec 3-5): any conflict-class file present
    # -> list them and refuse; only --force-accept-all passes.
    _accept_all_eff=0
    if [ "$BRIDGE_ACCEPT_ALL" = "1" ]; then
      if [ "${#_conflict_rows[@]}" -eq 0 ] || [ "$FORCE_ACCEPT_ALL" = "1" ]; then
        _accept_all_eff=1
      elif [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] --bridge-accept-all 거부 예정 -- 충돌 파일:" \
            "  [dry-run] would REFUSE --bridge-accept-all -- conflict-class files:"
        for _c in "${_conflict_rows[@]}"; do printf '    %s\n' "$_c"; done
      else
        msg "[update][오류] --bridge-accept-all 거부 -- 충돌 클래스 파일 존재 (--force-accept-all로만 통과):" \
            "[update][ERROR] --bridge-accept-all refused -- conflict-class file(s) present (--force-accept-all to override):" >&2
        for _c in "${_conflict_rows[@]}"; do printf '  %s\n' "$_c" >&2; done
        exit 1
      fi
    fi

    # Warn on --bridge-accept targets that match no classified file (typo /
    # substitution-channel path): the flag would silently protect nothing.
    for _a in ${BRIDGE_ACCEPT_LIST[@]+"${BRIDGE_ACCEPT_LIST[@]}"}; do
      _a_found=0
      for _entry in ${BRIDGE_CLASSIFIED[@]+"${BRIDGE_CLASSIFIED[@]}"}; do
        if [ "bridge/${_entry#*:}" = "$_a" ]; then _a_found=1; break; fi
      done
      if [ "$_a_found" = "0" ]; then
        msg "[update][경고] --bridge-accept 대상이 분류 목록에 없음 (오타/치환 채널?): $_a" \
            "[update][WARN] --bridge-accept target matched no classified bridge file (typo / substitution channel?): $_a"
      fi
    done

    # Pass 2: act per class (+ accept overrides).
    for _entry in ${BRIDGE_CLASSIFIED[@]+"${BRIDGE_CLASSIFIED[@]}"}; do
      _cls="${_entry%%:*}"
      _rel="${_entry#*:}"
      _relkey="bridge/$_rel"
      _accepted=0
      case "$_cls" in
        3|4|5|8)
          if bridge_accept_requested "$_relkey" || [ "$_accept_all_eff" = "1" ]; then
            _accepted=1
          fi ;;
        2)
          # DGN-677: a class-2 file that is still provisional is an escalated
          # CONFLICT (see class 2 below); allow explicit --bridge-accept (or
          # accept-all) to land it. The accept path backs up first and re-seeds M
          # WITHOUT the tag, so acceptance also converges the file out of
          # provisional. A non-provisional class 2 stays plain lossless drift.
          if bridge_is_provisional "$_relkey" \
             && { bridge_accept_requested "$_relkey" || [ "$_accept_all_eff" = "1" ]; }; then
            _accepted=1
          fi ;;
      esac
      if [ "$_accepted" = "1" ]; then
        # Accept = take the vendor/template version, with a backup of any
        # existing instance copy first, then seed M from the landed file.
        if [ "$DRY_RUN" = "1" ]; then
          msg "  [dry-run] 수락+착지 예정 (백업 선행): $_relkey" \
              "  [dry-run] would accept+land (backup first): $_relkey"
        else
          bridge_backup_file "$_relkey"
          bridge_land "$_rel"
          msg "  [update] 수락+착지: $_relkey" "  [update] accepted+landed: $_relkey"
        fi
        BRIDGE_M_RECHECK+=("$_relkey")
        BRIDGE_REPORT_LINES+=("LANDED $_relkey")
        BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
        continue
      fi
      case "$_cls" in
        1)
          # Row 1: in sync -- refresh M only.
          BRIDGE_M_RECHECK+=("$_relkey")
          ;;
        2)
          # Row 2: lossless drift -> land (the dashboard.py class).
          # DGN-677 HYBRID: if this file was adopt-provisional last run (M carries
          # #adopted-provisional), we do NOT trust "I==M => unedited". A genuine
          # local edit could have been frozen into M by the class-5 adopt. So
          # instead of auto-landing, ESCALATE to CONFLICT (require --bridge-accept
          # -- handled by the accept-eligibility branch above). A truly pristine
          # file lands with one explicit accept (1-run deferred landing); a
          # genuine edit is stopped here (loss actively blocked, not just backed
          # up). An accepted provisional never reaches here (accept path continues).
          if bridge_is_provisional "$_relkey"; then
            # DGN-757: before escalating, try the vendor-ancestry proof. The
            # run-1 adopt froze the on-disk bytes into M; if those bytes are
            # byte-identical to a historical vendored version of this path,
            # the "genuine local edit" hypothesis is DISPROVEN -- this is a
            # pristine stale seed, i.e. true lossless drift. Land it now
            # (backup first, belt-and-braces) and drop the provisional tag by
            # re-seeding M from the landed file. This is what un-freezes an
            # already-adopted init-seeded instance (DGN-625 class) without
            # operator action.
            if bridge_vendor_ancestor "$_rel"; then
              if [ "$DRY_RUN" = "1" ]; then
                msg "  [dry-run] 착지 예정 (잠정채택 + 벤더 계보 검증됨): $_relkey" \
                    "  [dry-run] would land (provisional + vendor-ancestry verified): $_relkey"
              else
                bridge_backup_file "$_relkey"
                bridge_land "$_rel"
                msg "  [update] 착지 (잠정채택 + 벤더 계보 검증됨 -- 순정 구본 시드): $_relkey" \
                    "  [update] landed (provisional + vendor-ancestry verified -- pristine stale seed): $_relkey"
              fi
              BRIDGE_M_RECHECK+=("$_relkey")
              BRIDGE_REPORT_LINES+=("LANDED $_relkey")
              BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
              continue
            fi
            printf '%s\n' "============================================================" >&2
            msg "[update][경고] bridge/ 충돌 (잠정채택 파일 + 벤더 변경) -- 착지 보류, 확인 필요: $_relkey" \
                "[update][WARN] bridge/ CONFLICT (provisional-adopt + vendor change) -- landing held, review required: $_relkey" >&2
            msg "  해소: 착지 원하면 --bridge-accept $_relkey / 로컬편집 지키려면 .dogany-preserve 등록" \
                "  Resolve: to land use --bridge-accept $_relkey / to keep local edit register in .dogany-preserve" >&2
            printf '%s\n' "============================================================" >&2
            # keep the provisional M line verbatim (sha + tag) so next run
            # re-evaluates. The file is held (not landed), so its on-disk sha ==
            # the adopted sha; carry the line via BRIDGE_M_LINES with the tag
            # re-appended ($2 from bridge_manifest_sha is tag-stripped).
            _m="$(bridge_manifest_sha "$_relkey")"
            if [ -n "$_m" ]; then
              BRIDGE_M_LINES+=("$_relkey  $_m $BRIDGE_PROVISIONAL_TAG")
            fi
            BRIDGE_REPORT_LINES+=("CONFLICT $_relkey")
            BRIDGE_CONFLICT_N=$((BRIDGE_CONFLICT_N+1))
            BRIDGE_CONFLICT_FILES+=("$_relkey")
            continue
          fi
          if [ "$DRY_RUN" = "1" ]; then
            msg "  [dry-run] 착지 예정 (lossless drift): $_relkey" \
                "  [dry-run] would land (lossless drift): $_relkey"
          else
            bridge_land "$_rel"
            msg "  [update] 착지 (lossless drift): $_relkey" \
                "  [update] landed (lossless drift): $_relkey"
          fi
          BRIDGE_M_RECHECK+=("$_relkey")
          BRIDGE_REPORT_LINES+=("LANDED $_relkey")
          BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
          ;;
        3)
          # Row 3: local edit + vendor unchanged -> preserve + WARN.
          # DGN-762: FIRST try the vendor-ancestry proof (same helper as the
          # class-2-provisional / class-5 paths). "Local edit" here is a
          # manifest-based hypothesis; if the on-disk bytes are byte-identical
          # to SOME historical vendored version of this path, that hypothesis
          # is DISPROVEN -- the file is a pristine stale vendor seed (DGN-762
          # skull class: stale messages.py frozen while bot.py advanced ->
          # runtime AttributeError). Land it (backup first, belt-and-braces).
          # Ancestry unavailable (no git history) or no match (genuine edit)
          # -> return 1 -> the existing preserve behavior below, unchanged.
          if bridge_vendor_ancestor "$_rel"; then
            if [ "$DRY_RUN" = "1" ]; then
              msg "  [dry-run] 착지 예정 (계보 검증된 순정 구본 시드): $_relkey" \
                  "  [dry-run] would land (ancestry-proven stale vendor seed): $_relkey"
            else
              bridge_backup_file "$_relkey"
              bridge_land "$_rel"
              msg "  [update] 착지 (계보 검증된 순정 구본 시드): $_relkey" \
                  "  [update] ancestry-proven stale vendor seed -> land: $_relkey"
            fi
            BRIDGE_M_RECHECK+=("$_relkey")
            BRIDGE_REPORT_LINES+=("LANDED $_relkey")
            BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
            continue
          fi
          msg "  [update][경고] 보존 (로컬 편집, 벤더 무변경): $_relkey" \
              "  [update][WARN] preserved (local edit, vendor unchanged): $_relkey"
          _m="$(bridge_manifest_sha "$_relkey")"
          if [ -n "$_m" ]; then BRIDGE_M_LINES+=("$_relkey  $_m"); fi
          BRIDGE_REPORT_LINES+=("PRESERVED $_relkey")
          BRIDGE_PRESERVED_N=$((BRIDGE_PRESERVED_N+1))
          ;;
        4)
          # Row 4: both sides changed -> preserve + STRONG WARN. NEVER auto-land.
          # DGN-762: same vendor-ancestry proof as class 3 above. Class 4's
          # "local side changed" is also manifest-hypothesized; byte-identity
          # with a historical vendored version disproves it (pristine stale
          # seed + vendor advance = lossless drift in disguise), so land the
          # current vendor file (backup first). No match / no history ->
          # return 1 -> the existing CONFLICT hold below, unchanged.
          if bridge_vendor_ancestor "$_rel"; then
            if [ "$DRY_RUN" = "1" ]; then
              msg "  [dry-run] 착지 예정 (계보 검증된 순정 구본 시드): $_relkey" \
                  "  [dry-run] would land (ancestry-proven stale vendor seed): $_relkey"
            else
              bridge_backup_file "$_relkey"
              bridge_land "$_rel"
              msg "  [update] 착지 (계보 검증된 순정 구본 시드): $_relkey" \
                  "  [update] ancestry-proven stale vendor seed -> land: $_relkey"
            fi
            BRIDGE_M_RECHECK+=("$_relkey")
            BRIDGE_REPORT_LINES+=("LANDED $_relkey")
            BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
            continue
          fi
          printf '%s\n' "============================================================" >&2
          msg "[update][경고] bridge/ 충돌 (양측 변경) -- 보존, 자동 착지 금지: $_relkey" \
              "[update][WARN] bridge/ CONFLICT (both sides changed) -- preserved, auto-landing forbidden: $_relkey" >&2
          msg "  해소: .dogany-preserve 등록 / --bridge-accept $_relkey / canonical로 upstream" \
              "  Resolve: register in .dogany-preserve / --bridge-accept $_relkey / upstream to canonical" >&2
          printf '%s\n' "============================================================" >&2
          _m="$(bridge_manifest_sha "$_relkey")"
          if [ -n "$_m" ]; then BRIDGE_M_LINES+=("$_relkey  $_m"); fi
          BRIDGE_REPORT_LINES+=("CONFLICT $_relkey")
          BRIDGE_CONFLICT_N=$((BRIDGE_CONFLICT_N+1))
          BRIDGE_CONFLICT_FILES+=("$_relkey")
          # STALE-PRESERVE surfacing (DGN-724 / DGN-696): a preserved local
          # divergence hides whether the CANONICAL side ALSO advanced -- if it
          # did, the instance is now MISSING canonical fixes on a frozen file.
          # Detect by comparing the incoming template sha against M (what we
          # last landed). _t_sha from Pass 1 is stale here (separate loop), so
          # recompute it minimally the same way the classifier did. This is an
          # ADDITIONAL report line (never replaces CONFLICT): readers that grep
          # ^CONFLICT keep matching, and the gap is now always visible.
          _t_sha_now="$(file_checksum "$TEMPLATE/bridge/$_rel")"
          if [ -n "$_m" ] && [ "$_t_sha_now" != "$_m" ]; then
            msg "[update][경고] bridge/ STALE-PRESERVE -- 로컬 보존 중이나 canonical이 전진(수정 누락), back-land 필요: $_relkey" \
                "[update][WARN] bridge/ STALE-PRESERVE -- local preserved but canonical advanced (missing fixes), back-land needed: $_relkey" >&2
            BRIDGE_REPORT_LINES+=("STALE-PRESERVE $_relkey")
          fi
          ;;
        5)
          # Row 5: bootstrap (no manifest) + differs.
          # DGN-757: FIRST try the vendor-ancestry proof (helper above). A
          # byte-exact match against framework history proves the on-disk
          # file is a pristine stale VENDOR SEED (init-era commit, DGN-625
          # class) -- not a user edit -- so the current vendor file lands
          # immediately instead of entering the adopt-provisional freeze.
          if bridge_vendor_ancestor "$_rel"; then
            if [ "$DRY_RUN" = "1" ]; then
              msg "  [dry-run] 착지 예정 (부트스트랩 + 벤더 계보 검증됨): $_relkey" \
                  "  [dry-run] would land (bootstrap + vendor-ancestry verified): $_relkey"
            else
              bridge_backup_file "$_relkey"
              bridge_land "$_rel"
              msg "  [update] 착지 (부트스트랩 + 벤더 계보 검증됨 -- 순정 구본 시드): $_relkey" \
                  "  [update] landed (bootstrap + vendor-ancestry verified -- pristine stale seed): $_relkey"
            fi
            BRIDGE_M_RECHECK+=("$_relkey")
            BRIDGE_REPORT_LINES+=("LANDED $_relkey")
            BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
            continue
          fi
          # adopt-provisional (DGN-677 HYBRID): keep the on-disk file (NO landing
          # this run), but adopt its current on-disk sha into M *tagged*
          # #adopted-provisional so the next run has a 3-way baseline yet is NOT
          # trusted as "unedited". The sha is the FINAL on-disk (post-section-4)
          # bytes -> matches the manifest hashing convention (BRIDGE_M_RECHECK
          # path). Breaks the self-perpetuating freeze (chicken-and-egg) while
          # deferring the (i)pristine / (ii)genuine-edit decision to run2, where a
          # provisional class-2 file is ESCALATED to CONFLICT (see class 2 below).
          if [ "$DRY_RUN" = "1" ]; then
            msg "  [dry-run] 보존+매니페스트 잠정채택 예정 (부트스트랩 시드): $_relkey" \
                "  [dry-run] would preserve + provisionally adopt manifest (bootstrap seed): $_relkey"
          else
            msg "  [update][정보] 보존+매니페스트 잠정채택 (부트스트랩 시드): $_relkey" \
                "  [update][INFO] preserved + manifest provisionally adopted (bootstrap seed): $_relkey"
          fi
          BRIDGE_M_RECHECK+=("$_relkey")          # write current on-disk sha to M
          BRIDGE_M_PROVISIONAL+=("$_relkey")      # <-- write block re-attaches tag
          BRIDGE_REPORT_LINES+=("ADOPTED-PROVISIONAL $_relkey")
          BRIDGE_ADOPTED_N=$((BRIDGE_ADOPTED_N+1))
          ;;
        6)
          # Row 6: vendor-new file -> land + record M (H1; rsync -aL parity).
          if [ "$DRY_RUN" = "1" ]; then
            msg "  [dry-run] 신규 착지 예정: $_relkey" \
                "  [dry-run] would land new file: $_relkey"
          else
            bridge_land "$_rel"
            msg "  [update] 신규 착지: $_relkey" "  [update] landed new file: $_relkey"
          fi
          BRIDGE_M_RECHECK+=("$_relkey")
          BRIDGE_REPORT_LINES+=("NEW $_relkey")
          BRIDGE_LANDED_N=$((BRIDGE_LANDED_N+1))
          ;;
        7)
          # Row 7: local deletion + vendor unchanged -> respect the deletion.
          # KEEP the M entry: it is the deletion memory -- dropping it would
          # reclassify the file as row 6 next run (resurrection bug).
          _m="$(bridge_manifest_sha "$_relkey")"
          if [ -n "$_m" ]; then BRIDGE_M_LINES+=("$_relkey  $_m"); fi
          BRIDGE_REPORT_LINES+=("DELETED-KEPT $_relkey")
          ;;
        8)
          # Row 8: local deletion + vendor changed -> conflict WARN;
          # lands only via --bridge-accept / --force-accept-all.
          msg "[update][경고] bridge/ 충돌 (로컬 삭제 + 벤더 변경) -- 미착지: $_relkey (착지: --bridge-accept)" \
              "[update][WARN] bridge/ CONFLICT (local deletion + vendor change) -- not landed: $_relkey (land via --bridge-accept)" >&2
          _m="$(bridge_manifest_sha "$_relkey")"
          if [ -n "$_m" ]; then BRIDGE_M_LINES+=("$_relkey  $_m"); fi
          BRIDGE_REPORT_LINES+=("CONFLICT $_relkey")
          BRIDGE_CONFLICT_N=$((BRIDGE_CONFLICT_N+1))
          BRIDGE_CONFLICT_FILES+=("$_relkey")
          ;;
        9)
          # Row 9: vendor removed the file -> leave the instance copy (no
          # --delete policy), drop the M entry, info log.
          msg "  [update][정보] 벤더측 제거 -- 인스턴스 파일 유지, 매니페스트 정리: $_relkey" \
              "  [update][INFO] removed on vendor side -- instance file kept, manifest entry dropped: $_relkey"
          ;;
        10)
          # Row 10: stale manifest entry -> drop silently.
          ;;
      esac
    done

    # Reconcile report (spec 3-6): STATE file, overwritten each real run.
    # Dry-run writes nothing (classification was printed above instead).
    if [ "$DRY_RUN" = "0" ]; then
      mkdir -p "$INSTANCE/.claude"
      {
        printf '# bridge-reconcile.report -- STATE file, overwritten on every update run (DGN-593)\n'
        printf '# ts: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
        printf '# source: %s\n' "$REPO_VERSION"
        for _l in ${BRIDGE_REPORT_LINES[@]+"${BRIDGE_REPORT_LINES[@]}"}; do
          printf '%s\n' "$_l"
        done
      } > "$BRIDGE_REPORT_FILE"
    fi
    BRIDGE_RECONCILE_RAN=1
    UPDATED+=("bridge/ (reconciled: ${BRIDGE_LANDED_N} landed / ${BRIDGE_ADOPTED_N} adopted / ${BRIDGE_PRESERVED_N} preserved / ${BRIDGE_CONFLICT_N} conflict)")
    if [ "$BRIDGE_ADOPTED_N" -gt 0 ]; then
      # DGN-677 [P3 G-a]: an adopt run seeds M but does NOT land -- make the
      # un-landed state explicit so the deferral cannot silently harden.
      UPDATED+=("bridge/ ${BRIDGE_ADOPTED_N} adopted (provisional -- NOT landed; a follow-up update is required to land or resolve)")
    fi
    if [ "$BRIDGE_CONFLICT_N" -gt 0 ]; then
      UPDATED+=("bridge/ CONFLICT: ${BRIDGE_CONFLICT_FILES[*]}")
    fi
  fi
fi

# 3e-mirror) mirror/ engine (DGN-268 S3), hoisted at repo root (single home;
#     not in the template). Refresh CODE + schema ONLY. The instance's
#     mirror_state.db holds live sync bookkeeping (surface ids / etags /
#     cursors) and MUST survive a refresh -- COMMON_EXCLUDES already drops
#     *.db, and we add the WAL sidecars (*.db-wal / *.db-shm) belt-and-braces
#     so a mid-poll refresh never truncates in-flight state. Always-ship: the
#     cron flag-gate (MIRROR_MODULE) already silences opted-out users, so an
#     unconditional code refresh is correct and matches how service/ ships.
#
#     SECTION-ORDER SWAP (DGN-364 m7): this block runs BEFORE the routines/
#     rsync (3b) so the new adapter is always on disk before the new scripts
#     -- the scripts call get_mirror_targets; in the old order a 5-minute
#     poll firing between routines landing and mirror landing would
#     AttributeError once. Mirror-first is safe in both directions because
#     the promoted adapter keeps every old entry point (get_state etc.) the
#     old scripts use.
#
#     Reverse-drift guard (DGN-364 2.7b, F1): because this section is a
#     wholesale rsync (not per-file copies like 3f), the mirror/sdk_bridge.py
#     guard engages as a PRE-RSYNC check: on a SKIP verdict the exclude is
#     ANCHORED to the transfer root ('/sdk_bridge.py', leading slash -- an
#     unanchored pattern would also match a same-named file in any future
#     subdirectory of mirror/). Missing instance file = first-install
#     PROCEED (no exclude, the canonical file lands). Dry-run replicates the
#     3f reporting branch: the guard still evaluates and prints the would-be
#     verdict without mutating anything.
if [ -d "$REPO_ROOT/mirror" ]; then
  # Pack-ownership gate (DGN-855): if kit_mirror has ever successfully delivered
  # mirror/ to this instance it drops a sentinel file (.pack-owned).  That
  # sentinel -- not DOGANY_PACKS -- is the gate: instances that carried
  # DOGANY_PACKS=lifekit@<ver> from a pre-seam pack_install (before kit_mirror
  # existed) never got the sentinel, so 3e-mirror still runs for them (correct
  # canonical path) until they receive a new pack_install that writes it.
  # This dissolves the orphan window (B1) and avoids the substring false-positive
  # on 'mylifekit@1' etc. (B3).
  if [ -f "$INSTANCE/mirror/.pack-owned" ]; then
    msg "[update][INFO] 3e-mirror: SKIPPED -- lifekit pack owns mirror/ ($INSTANCE/mirror/.pack-owned present)" \
        "[update][INFO] 3e-mirror: SKIPPED -- lifekit pack owns mirror/ ($INSTANCE/mirror/.pack-owned present)"
    UPDATED+=("mirror/ (SKIPPED -- pack-owned)")
  else
    build_preserve_excludes "mirror"
    # DGN-385 FIX-1: section-root hold check (common to all sections).
    if [ "$SECTION_HELD" = "1" ]; then
      _section_held_warn "mirror" "$REPO_ROOT/mirror" "$INSTANCE/mirror" \
        --exclude '*.db-wal' --exclude '*.db-shm' --exclude '*.db.bak*'
      UPDATED+=("mirror/ (HELD -- skipped by .dogany-preserve)")
    else
      MIRROR_GUARD_EX=()
      _sb_fw="$REPO_ROOT/mirror/sdk_bridge.py"
      _sb_inst="$INSTANCE/mirror/sdk_bridge.py"
      if [ -f "$_sb_fw" ] && [ -f "$_sb_inst" ]; then
        if [ "$DRY_RUN" = "1" ]; then
          # Dry-run reporting branch (3f-style): evaluate + print, mutate nothing.
          _sb_fw_v="$( extract_ver_sdk_bridge_py "$_sb_fw"   2>/dev/null)" || true
          _sb_in_v="$( extract_ver_sdk_bridge_py "$_sb_inst" 2>/dev/null)" || true
          if [[ "$_sb_fw_v" =~ ^[0-9]+$ ]] && [[ "$_sb_in_v" =~ ^[0-9]+$ ]] && [ "$_sb_in_v" -gt "$_sb_fw_v" ]; then
            msg "  [dry-run][경고] 역주행 가드: mirror/sdk_bridge.py 갱신 건너뜀 예정 (인스턴스 v${_sb_in_v} > 프레임워크 v${_sb_fw_v})" \
                "  [dry-run][WARN] reverse-drift guard: would SKIP mirror/sdk_bridge.py (instance v${_sb_in_v} > framework v${_sb_fw_v})"
            MIRROR_GUARD_EX+=(--exclude '/sdk_bridge.py')
          else
            msg "  [dry-run] mirror/sdk_bridge.py 갱신 예정" \
                "  [dry-run] would refresh mirror/sdk_bridge.py"
          fi
        else
          drift_guard_file "mirror/sdk_bridge.py" "$_sb_fw" "$_sb_inst" "sdk_bridge_py" \
            || MIRROR_GUARD_EX+=(--exclude '/sdk_bridge.py')
        fi
      fi
      # Missing instance file: first-install PROCEED -- no exclude added.
      rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
        ${MIRROR_GUARD_EX[@]+"${MIRROR_GUARD_EX[@]}"} \
        --exclude '*.db-wal' \
        --exclude '*.db-shm' \
        --exclude '*.db.bak*' \
        "$REPO_ROOT/mirror/" "$INSTANCE/mirror/"
      UPDATED+=("mirror/ (code+schema; *.db preserved)")
    fi
  fi
fi

# 3b) routines (framework schedulers/scripts). Preserve-list excludes guard
#     instance-customized routine scripts (DGN-359/DGN-363 clobber class).
if [ -d "$TEMPLATE/routines" ]; then
  build_preserve_excludes "routines"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "routines" "$TEMPLATE/routines" "$INSTANCE/routines"
    UPDATED+=("routines/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      "$TEMPLATE/routines/" "$INSTANCE/routines/"
    UPDATED+=("routines/")
  fi
fi

# 3c) memory engine code ONLY (*.py + taxonomy doc) -- never memory markdown/db.
#     Preserve excludes must precede the include chain (rsync filter rules are
#     order-sensitive: first match wins).
if [ -d "$TEMPLATE/memory-engine" ]; then
  build_preserve_excludes "memory-engine"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "memory-engine" "$TEMPLATE/memory-engine" "$INSTANCE/memory-engine" \
      --include '*/' --include '*.py' --include '*.md' --exclude '*'
    UPDATED+=("memory-engine/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      --include '*/' --include '*.py' --include '*.md' --exclude '*' \
      "$TEMPLATE/memory-engine/" "$INSTANCE/memory-engine/"
    UPDATED+=("memory-engine/*.py")
  fi
fi

# 3d) config: i18n locales are FRAMEWORK (refresh); agent.conf + lifekit.conf
#     are per-instance STATE scaffolds (user language/address, lifekit
#     activation choices). Same write-if-absent contract as .env / lifekit.db
#     in mint.sh: an update must NEVER reset user choices back to template
#     defaults (e.g. LIFEKIT=pending, AGENT_LANG=ko).
if [ -d "$TEMPLATE/config" ]; then
  build_preserve_excludes "config"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "config" "$TEMPLATE/config" "$INSTANCE/config" \
      --exclude 'agent.conf' --exclude 'lifekit.conf'
    UPDATED+=("config/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      --exclude 'agent.conf' \
      --exclude 'lifekit.conf' \
      --exclude 'secret-patterns.conf' \
      "$TEMPLATE/config/" "$INSTANCE/config/"
    for f in agent.conf lifekit.conf secret-patterns.conf; do
      if [ ! -f "$INSTANCE/config/$f" ] && [ -f "$TEMPLATE/config/$f" ]; then
        if [ "$DRY_RUN" = "1" ]; then
          msg "  [dry-run] config/$f 스캐폴드 생성 예정 (없음)" \
              "  [dry-run] would scaffold config/$f (absent)"
        else
          cp -p "$TEMPLATE/config/$f" "$INSTANCE/config/$f"
        fi
      fi
    done
    UPDATED+=("config/ (i18n; conf scaffolds only if absent)")
  fi
fi

# 3e) service SDK facade (hoisted at repo root, bundled into the instance).
if [ -d "$REPO_ROOT/service" ]; then
  build_preserve_excludes "service"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "service" "$REPO_ROOT/service" "$INSTANCE/service"
    UPDATED+=("service/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      "$REPO_ROOT/service/" "$INSTANCE/service/"
    UPDATED+=("service/")
  fi
fi

# 3f) database schema + CLI (framework), NEVER the *.db (excluded above).
#
#     Before any copy, the DB drift nag checks PRAGMA user_version against the
#     framework lifekit.py pin (informational). Then drift_guard_file() guards
#     each file in GUARDED_FILES: if the instance copy carries a higher version
#     pin than the framework source, the copy is skipped with a loud warning
#     instead of silently reverting the instance to an older code version.
ensure_dir "$INSTANCE/database"

# DB version nag: informational, runs even under --dry-run (read-only check).
db_drift_nag "$INSTANCE/database/lifekit.db" "$REPO_ROOT/database/lifekit.py"

for f in schema.sql lifekit.py lifekit.sh README.md remind_select.py routine_roller.py routine_projection.py relmod.py relmod.sh; do
  [ -f "$REPO_ROOT/database/$f" ] || continue

  # Instance-preserve list (DGN-359): skip files the operator declared local.
  if is_preserved "database/$f"; then
    msg "  [update] 보존: database/$f (.dogany-preserve)" \
        "  [update] preserved: database/$f (.dogany-preserve)"
    continue
  fi

  # FORWARD-PIN GUARD (DGN-656): under --code-only, landing a lifekit.py whose
  # EXPECTED_USER_VERSION outruns the instance DB's (deliberately frozen)
  # user_version would fail-closed every lifekit verb -- the code demands a
  # migration this run will never apply. Hold the instance copy back and warn
  # loudly. Scope: lifekit.py only -- schema.sql feeds fresh-DB creation (no
  # existing-DB mutation) and the other CLI files carry no DB pin, so they land
  # normally. Mirror of the reverse-drift guard, opposite direction: there the
  # INSTANCE is ahead; here the FRAMEWORK would run ahead of a schema --code-only
  # froze. No DB / no sqlite3 / parse failure -> guard disengages (best-effort,
  # like drift_guard_file).
  if [ "$CODE_ONLY" = "1" ] && [ "$f" = "lifekit.py" ] \
     && [ -f "$INSTANCE/database/lifekit.db" ] \
     && command -v sqlite3 >/dev/null 2>&1; then
    _co_db_ver="$(sqlite3 "$INSTANCE/database/lifekit.db" 'PRAGMA user_version;' 2>/dev/null || true)"
    _co_fw_pin="$(extract_ver_lifekit_py "$REPO_ROOT/database/lifekit.py" 2>/dev/null || true)"
    if [[ "$_co_db_ver" =~ ^[0-9]+$ ]] && [[ "$_co_fw_pin" =~ ^[0-9]+$ ]] \
       && [ "$_co_fw_pin" -gt "$_co_db_ver" ]; then
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run][경고] forward-pin 가드(--code-only): database/$f 갱신 건너뜀 예정 (코드 핀 v${_co_fw_pin} > DB v${_co_db_ver}, 마이그레이션 스킵 상태)" \
            "  [dry-run][WARN] forward-pin guard (--code-only): would SKIP database/$f (code pin v${_co_fw_pin} > DB v${_co_db_ver} with migrations skipped)"
      else
        printf '%s\n' "============================================================" >&2
        msg "[update][경고] forward-pin 가드 발동 (--code-only) -- 파일 갱신 건너뜀" \
            "[update][WARN] FORWARD-PIN GUARD triggered (--code-only) -- file skipped" >&2
        msg "  파일: database/$f" \
            "  file: database/$f" >&2
        msg "  코드 핀: v${_co_fw_pin}  |  인스턴스 DB: v${_co_db_ver} (마이그레이션은 --code-only로 스킵됨)" \
            "  code pin: v${_co_fw_pin}  |  instance DB: v${_co_db_ver} (migrations skipped by --code-only)" >&2
        msg "  이 파일을 랜딩하면 코드가 요구하는 스키마가 없어 lifekit 전체가 fail-closed 됩니다." \
            "  Landing it would fail-close every lifekit verb (code demands a schema this run will not apply)." >&2
        msg "  조치: lineage reconcile 후 기본 업데이트를 돌리거나, 인스턴스 lineage에 맞는 핀을 유지하세요." \
            "  action: reconcile the migration lineage then run a default update, or keep the lineage-matched pin." >&2
        printf '%s\n' "============================================================" >&2
      fi
      continue
    fi
  fi

  # Reverse-drift guard: check GUARDED_FILES for this filename.
  _guarded_skip=0
  for _gentry in "${GUARDED_FILES[@]}"; do
    _grel="${_gentry%%:*}"
    _gkey="${_gentry##*:}"
    # Match by basename of the guarded relpath.
    if [ "$(basename "$_grel")" = "$f" ]; then
      _fw_src="$REPO_ROOT/$_grel"
      _inst_dest="$INSTANCE/$_grel"
      if [ "$DRY_RUN" = "1" ]; then
        # In dry-run: run the check but report would-skip instead of actually skipping.
        _fw_v="$(  "extract_ver_${_gkey}" "$_fw_src"   2>/dev/null)" || true
        _in_v="$( "extract_ver_${_gkey}" "$_inst_dest" 2>/dev/null)" || true
        if [[ "$_fw_v" =~ ^[0-9]+$ ]] && [[ "$_in_v" =~ ^[0-9]+$ ]] && [ "$_in_v" -gt "$_fw_v" ]; then
          msg "  [dry-run][경고] 역주행 가드: database/$f 갱신 건너뜀 예정 (인스턴스 v${_in_v} > 프레임워크 v${_fw_v})" \
              "  [dry-run][WARN] reverse-drift guard: would SKIP database/$f (instance v${_in_v} > framework v${_fw_v})"
          _guarded_skip=1
        else
          msg "  [dry-run] database/$f 갱신 예정" "  [dry-run] would refresh database/$f"
          _guarded_skip=2  # "proceed" marker -- suppress the default dry-run msg below
        fi
      else
        drift_guard_file "$_grel" "$_fw_src" "$_inst_dest" "$_gkey" || { _guarded_skip=1; }
      fi
      break
    fi
  done

  # _guarded_skip=1 -> blocked by guard; skip this file entirely.
  [ "$_guarded_skip" = "1" ] && continue

  if [ "$DRY_RUN" = "1" ]; then
    # _guarded_skip=2 means the guard already printed its dry-run line; skip default.
    [ "$_guarded_skip" = "2" ] || msg "  [dry-run] database/$f 갱신 예정" "  [dry-run] would refresh database/$f"
  else
    cp -p "$REPO_ROOT/database/$f" "$INSTANCE/database/$f"
  fi
done
# DGN-803 LS-5: database/schema.sql and the CLI surface (lifekit.py/.sh)
# were extracted to the lifekit pack; they no longer live in the framework
# tree (database/ now contains only restore-data.sh). The loop above is a
# no-op for all extracted files. Remove the misleading UPDATED entry that
# was appended here unconditionally even when no files were copied.

# 3f-migrate) apply pending lifekit.db schema migrations, forward-only.
#   The DB carries its schema version in SQLite's PRAGMA user_version. A DB freshly
#   created from schema.sql is version 1; real migrations start at 002. We apply
#   every migrations/NNN_*.sql whose NNN > the DB's current user_version, in
#   ascending numeric order, backing up the *.db before each apply. This is the
#   ONLY controlled path that mutates an existing lifekit.db (never delete/clobber).
#
#   --code-only (DGN-656): this step is the ONE thing --code-only removes.
#   Pending migrations are ENUMERATED and reported (so the operator sees the
#   exact lineage-divergence extent: which canonical migrations this instance
#   is deliberately not taking) but never applied; the DB stays at its current
#   user_version and no backup is written (nothing is mutated).
MIG_DIR="$REPO_ROOT/database/migrations"
DB="$INSTANCE/database/lifekit.db"
if [ -d "$MIG_DIR" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  cur_ver="$(sqlite3 "$DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
  cur_ver="${cur_ver:-0}"
  # ROLLBACK MODE direction note (DGN-673 B3, R2): this section is FORWARD
  # only (applies NNN > user_version; a DB ahead of the target tree is a
  # silent no-op here by design). On a downgrade that silence would mask a
  # DB-ahead-of-code state, so announce the delegation: the reverse leg is
  # OWNED by the DGN-672 snapshot path (restore-data.sh), enforced by
  # rollback_db_checkpoint before the version stamp -- never here.
  if [ "${DOGANY_ROLLBACK:-0}" = "1" ]; then
    _rb_target="$(extract_ver_lifekit_py "$REPO_ROOT/database/lifekit.py" 2>/dev/null)" || _rb_target=""
    if [[ "$_rb_target" =~ ^[0-9]+$ ]] && [ "$cur_ver" -gt "$_rb_target" ]; then
      msg "  [update] 롤백: DB(v${cur_ver})가 타깃 핀(v${_rb_target})보다 앞섬 -- 마이그레이션 섹션은 정방향 전용, 역방향은 restore-data.sh 스냅샷 경로로 위임 (스탬프 전 체크포인트에서 강제)" \
          "  [update] rollback: DB (v${cur_ver}) ahead of target pin (v${_rb_target}) -- this section is forward-only; the reverse leg is delegated to the restore-data.sh snapshot path (enforced by the pre-stamp checkpoint)"
    fi
  fi
  applied_migs=()
  skipped_migs=()
  # Iterate migrations in ascending numeric order (NNN prefix). Glob is sorted,
  # and zero-padded 3-digit prefixes sort correctly lexically == numerically.
  for mig in "$MIG_DIR"/[0-9][0-9][0-9]_*.sql; do
    [ -e "$mig" ] || continue
    base="$(basename "$mig")"
    nnn="${base%%_*}"
    # Strip leading zeros for a clean numeric compare (avoid octal via 10#).
    n=$((10#$nnn))
    [ "$n" -gt "$cur_ver" ] || continue
    if [ "$CODE_ONLY" = "1" ]; then
      # DGN-656: enumerate-but-skip. Same visibility as an apply line, so a
      # dry-run and a real run agree on WHAT is being skipped.
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] 마이그레이션 $nnn 건너뜀 예정 ($base) (--code-only)" \
            "  [dry-run] would skip migration $nnn ($base) (--code-only)"
      else
        msg "  [update] 마이그레이션 $nnn 건너뜀 ($base) (--code-only)" \
            "  [update] skipped migration $nnn ($base) (--code-only)"
      fi
      skipped_migs+=("$nnn")
      continue
    fi
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] 마이그레이션 $nnn 적용 예정 ($base)" \
          "  [dry-run] would apply migration $nnn ($base)"
    else
      # Back up the DB BEFORE applying this migration (DGN-672 M4).
      # WAL-safe: `.backup` folds the WAL into the copy (cp -p of a live WAL
      # DB loses everything not yet checkpointed). Version-stamped filename
      # per the DGN-672 C3 snapshot contract (lifekit.db.v<ver>.bak-<ts>);
      # restore-data.sh --list surfaces these as restore points.
      # FRESH pragma read every iteration: the previous migration in this
      # loop advanced user_version, so reusing a loop-entry version would
      # stamp later backups with a stale version.
      ts="$(date +%Y%m%d-%H%M%S)"
      v="$(sqlite3 "$DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
      bak="$INSTANCE/database/lifekit.db.v${v}.bak-$ts"
      sqlite3 "$DB" ".backup '$bak'" || die "failed to back up lifekit.db before migration $nnn"
      msg "  [update] DB 백업 -> $bak" "  [update] backed up DB -> $bak"
      sqlite3 "$DB" < "$mig" || die "migration $nnn failed to apply ($base); DB backup at $bak"
      msg "  [update] 마이그레이션 $nnn 적용 완료 ($base)" \
          "  [update] applied migration $nnn ($base)"
    fi
    applied_migs+=("$nnn")
  done
  if [ ${#applied_migs[@]} -gt 0 ]; then
    UPDATED+=("database/migrations: ${applied_migs[*]}")
  fi
  if [ ${#skipped_migs[@]} -gt 0 ]; then
    msg "[update][주의] --code-only: 보류 중 마이그레이션 ${#skipped_migs[@]}건 미적용 (DB는 v${cur_ver} 유지): ${skipped_migs[*]}" \
        "[update][NOTE] --code-only: ${#skipped_migs[@]} pending migration(s) NOT applied (DB stays v${cur_ver}): ${skipped_migs[*]}"
    UPDATED+=("database/migrations: SKIPPED by --code-only (${skipped_migs[*]}; DB stays v${cur_ver})")
  fi
fi

# Substitute the mint placeholders on a single file, in place. Hoisted here (out
# of section 4) so BOTH the settings.json install (section 3g) and the skills
# refresh loop can substitute a freshly installed file at install time. For the
# skills loop this matters because we checksum right after: hashing before
# substitution would make the substituted on-disk copy look "user-modified" on
# the next update and back it up spuriously. For settings.json it matters because
# a harness hook firing between copy and a later substitution would read a raw
# __PROJECT_ROOT__ placeholder -- so we substitute atomically at install (3g).
# CROSS-REF: token list also at mint.sh sanity check (~L504 alternation),
# pack_install.sh _subst_mint_tokens, and knowledge_selftest.sh G4 -- keep
# all four sites in sync when adding a token.
subst_one() {
  local f="$1"
  sed_inplace "$f" \
    -e "s#__PROJECT_ROOT__#${INSTANCE}#g" \
    -e "s#__HOME__#${HOME}#g"
  if [ "$IDENTITY_OK" = "1" ]; then
    sed_inplace "$f" \
      -e "s#__AGENT_NAME__#${AGENT_NAME}#g" \
      -e "s#__AGENT_LABEL__#${AGENT_LABEL}#g" \
      -e "s#__USER_LABEL__#${USER_LABEL}#g" \
      -e "s#__AGENT_PREFIX__#${AGENT_PREFIX}#g" \
      -e "s#__AGENT_LANG__#${AGENT_LANG}#g"
  fi
}

# 3g) harness config: .claude/settings.json (framework), keep the skills dir intact.
#     FRAMEWORK HOOKS ONLY (DGN-359): this file is framework-owned and rewritten
#     wholesale, so instance-local hooks placed here are clobbered on every
#     update (live incidents: DGN-290, DGN-359). Instance hooks belong in
#     .claude/settings.local.json, which Claude Code merges natively and this
#     script NEVER writes.
#     Two defects handled here:
#       * model reset: the instance may run a model different from the template
#         default. We read the instance's current "model" value first and re-apply
#         it after installing the template copy, so the choice survives the refresh.
#       * copy->substitute race: a hook firing between an install and a LATER
#         substitution pass would read a raw __PROJECT_ROOT__ placeholder. We build
#         the fully substituted (and model-restored) content in a temp file, then
#         atomically mv it into place, so the live file is never in a raw state.
if [ -f "$TEMPLATE/.claude/settings.json" ] && is_preserved ".claude/settings.json"; then
  msg "  [update] 보존: .claude/settings.json (.dogany-preserve)" \
      "  [update] preserved: .claude/settings.json (.dogany-preserve)"
elif [ -f "$TEMPLATE/.claude/settings.json" ]; then
  ensure_dir "$INSTANCE/.claude"
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] .claude/settings.json 갱신 예정" "  [dry-run] would refresh .claude/settings.json"
  else
    SETTINGS_DEST="$INSTANCE/.claude/settings.json"
    # Read the instance-chosen model BEFORE overwriting (empty if no file/key).
    OLD_MODEL=""
    if [ -f "$SETTINGS_DEST" ]; then
      OLD_MODEL="$(python3 -c 'import json,sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get("model","") or "")
except Exception:
    pass' "$SETTINGS_DEST" 2>/dev/null || true)"
    fi
    # Build substituted + model-restored content in a temp file, then atomic mv.
    settings_tmp="$(mktemp "${SETTINGS_DEST}.new.XXXXXX")"
    cp -p "$TEMPLATE/.claude/settings.json" "$settings_tmp"
    subst_one "$settings_tmp"
    if [ -n "$OLD_MODEL" ]; then
      python3 -c 'import json,sys
p, model = sys.argv[1], sys.argv[2]
with open(p) as fh:
    data = json.load(fh)
data["model"] = model
text = json.dumps(data, indent=2, ensure_ascii=False)
with open(p, "w") as fh:
    fh.write(text + "\n")' "$settings_tmp" "$OLD_MODEL" \
        || die "failed to restore instance model in settings.json"
    fi
    mv -f "$settings_tmp" "$SETTINGS_DEST"
  fi
  UPDATED+=(".claude/settings.json")
fi

# 3h) worklog template (framework), never existing worklog tickets.
if [ -f "$TEMPLATE/worklog/_TEMPLATE.md" ] && is_preserved "worklog/_TEMPLATE.md"; then
  msg "  [update] 보존: worklog/_TEMPLATE.md (.dogany-preserve)" \
      "  [update] preserved: worklog/_TEMPLATE.md (.dogany-preserve)"
elif [ -f "$TEMPLATE/worklog/_TEMPLATE.md" ]; then
  ensure_dir "$INSTANCE/worklog"
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] worklog/_TEMPLATE.md 갱신 예정" "  [dry-run] would refresh worklog/_TEMPLATE.md"
  else
    cp -p "$TEMPLATE/worklog/_TEMPLATE.md" "$INSTANCE/worklog/_TEMPLATE.md"
  fi
  UPDATED+=("worklog/_TEMPLATE.md")
fi

# 3i) official framework skills: refresh ONLY skills/dogany-* into the instance.
#     User-authored (non-dogany-) skills under .claude/skills/ are left alone.
#
#     BACKUP-ON-MODIFY guard: a dogany-* skill is FRAMEWORK, refreshed with
#     `rsync -aL --delete` (prunes upstream-removed files). If the user has
#     hand-edited an installed dogany-* skill, that overwrite would silently
#     destroy their edits. To prevent data loss we keep a checksum manifest of
#     what WE last installed (.claude/.dogany-skills.sha, "<name>  <sha>" lines):
#       * unmodified (instance sha == manifest sha) -> just refresh, as before.
#       * user-modified (differs from manifest, OR manifest entry missing but the
#         instance copy differs from the incoming template copy) -> back the
#         instance dir up to .claude/skill-backups/<name>.user-<timestamp>/ and
#         WARN, THEN refresh. The backup lives OUTSIDE .claude/skills/ on purpose:
#         a backup dir under .claude/skills/ gets registered by the harness as a
#         live duplicate skill.
#     After each refresh the manifest is updated to the newly installed sha.

# Deterministic, path-independent digest of a skill dir: hash each file's content
# together with its path RELATIVE to the dir, sorted, then hash the roll-up. Same
# content under repo-side and instance-side yields the same sha (absolute path is
# never part of the digest). Empty/missing dir -> stable empty marker.
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

# Read a skill's recorded sha from the manifest ("<name>  <sha>"); empty if none.
SKILLS_MANIFEST="$INSTANCE/.claude/.dogany-skills.sha"
manifest_sha() {
  local name="$1"
  [ -f "$SKILLS_MANIFEST" ] || { printf '%s' ""; return; }
  awk -v n="$name" '$1==n {print $2; exit}' "$SKILLS_MANIFEST"
}

# file_checksum() formerly lived here; hoisted above section 3a (DGN-593) --
# the bridge per-file reconcile uses it before this point. Definition only
# moved; 3k/3k2 call sites below are unchanged.

# Framework single-file manifest (DGN-130): records the sha of framework-owned
# FILES (currently RULES.md) as this script last installed them, exactly like
# .dogany-skills.sha does for skill dirs. Used to detect a user edit before a
# refresh overwrites it. Format: "<relpath>  <sha>". Kept separate from the
# skills manifest so the two channels never race on one file.
FRAMEWORK_MANIFEST="$INSTANCE/.claude/.dogany-framework.sha"
framework_manifest_sha() {
  local rel="$1"
  [ -f "$FRAMEWORK_MANIFEST" ] || { printf '%s' ""; return; }
  awk -v n="$rel" '$1==n {print $2; exit}' "$FRAMEWORK_MANIFEST"
}

# Substitute placeholders across every text file in one skill dir (in place).
subst_skill_dir() {
  local dir="$1"
  while IFS= read -r -d '' f; do
    subst_one "$f"
  done < <(find "$dir" -type f \
      \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.plist' \
         -o -name '*.md' -o -name '*.conf' -o -name '*.txt' -o -name '*.example' \) \
      -not -path '*/venv/*' -not -path '*/__pycache__/*' -not -name '*.bak.*' \
      -print0 2>/dev/null)
}

ensure_dir "$INSTANCE/.claude/skills"
DOGANY_SKILLS=()
# Collect new manifest lines as we install; rewrite the manifest at the end so a
# --dry-run leaves it untouched.
NEW_MANIFEST_LINES=()
for d in "$SKILLS_ROOT"/dogany-*/; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  DOGANY_SKILLS+=("$name")
  dest="$INSTANCE/.claude/skills/$name"

  # Decide whether the instance copy was user-modified BEFORE overwriting it.
  recorded="$(manifest_sha "$name")"
  cur_sha="$(skill_checksum "$dest")"
  incoming_sha="$(skill_checksum "$d")"
  user_modified=0
  if [ -d "$dest" ]; then
    if [ -n "$recorded" ]; then
      [ "$cur_sha" != "$recorded" ] && user_modified=1
    else
      # No manifest entry (e.g. pre-guard instance): treat as modified only if the
      # instance copy actually differs from what we're about to install.
      [ "$cur_sha" != "$incoming_sha" ] && user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    if [ "$user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 스킬 백업 예정: $name" \
          "  [dry-run] would back up user-modified $name"
    fi
    msg "  [dry-run] 스킬 갱신 예정: $name" "  [dry-run] would refresh $name"
    # Do NOT rsync, back up, or touch the manifest in dry-run.
    continue
  fi

  # Back up the user's version before it is overwritten/pruned.
  if [ "$user_modified" = "1" ]; then
    # Reuse the section 3f-migrate timestamp pattern for the backup suffix.
    ts="$(date +%Y%m%d-%H%M%S)"
    # Back up OUTSIDE .claude/skills/ -- a backup dir inside skills/ is registered
    # by the harness as a live duplicate skill.
    mkdir -p "$INSTANCE/.claude/skill-backups"
    bak="$INSTANCE/.claude/skill-backups/$name.user-$ts"
    cp -a "$dest" "$bak" || die "failed to back up user-modified skill $name"
    msg "  [update][경고] 사용자 수정 스킬 발견 -- 백업: $bak" \
        "  [update][WARN] user-modified skill detected -- backed up to: $bak"
  fi

  # --delete here is scoped to the single dogany-* skill dir, so it prunes files
  # removed upstream WITHOUT affecting sibling user skills.
  rsync -aL --delete "${COMMON_EXCLUDES[@]}" \
    "$d" "$dest/"

  # Substitute the mint placeholders on the freshly installed skill NOW, before we
  # checksum it -- so the manifest sha reflects the exact on-disk (post-substitution)
  # bytes. If we hashed before substitution, the next update would see the
  # substituted copy as "user-modified" and spuriously back it up every run.
  subst_skill_dir "$dest"

  # Record the sha of what we JUST installed (re-checksum the destination so the
  # manifest reflects the on-disk result, not the source).
  installed_sha="$(skill_checksum "$dest")"
  NEW_MANIFEST_LINES+=("$name  $installed_sha")
done
[ ${#DOGANY_SKILLS[@]} -gt 0 ] && UPDATED+=("skills: ${DOGANY_SKILLS[*]}")

# Rewrite the skills manifest with the freshly installed checksums (skip in
# dry-run, where NEW_MANIFEST_LINES is empty and nothing was installed).
if [ "$DRY_RUN" = "0" ] && [ ${#NEW_MANIFEST_LINES[@]} -gt 0 ]; then
  {
    printf '# .dogany-skills.sha -- checksums of framework dogany-* skills as installed\n'
    printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
    printf '# framework refresh overwrites them. Format: "<skill-name>  <sha>".\n'
    for line in "${NEW_MANIFEST_LINES[@]}"; do printf '%s\n' "$line"; done
  } > "$SKILLS_MANIFEST"
fi

# 3j) dormant lifekit bundle skills (framework). These live as real dirs under
#     .claude/skills-bundle/ and are activated by an instance-local symlink in
#     .claude/skills/ (created post-mint by dogany-lifekit-setup). Without this
#     refresh the bundle skills (diet-log, workout-log, appointment-log,
#     relationship, task-update) would stay frozen at mint time forever.
#     Framework-owned area: plain rsync (no --delete) so the activation symlinks
#     in .claude/skills/ are untouched and any user files are never pruned.
if [ -d "$TEMPLATE/.claude/skills-bundle" ]; then
  ensure_dir "$INSTANCE/.claude/skills-bundle"
  build_preserve_excludes ".claude/skills-bundle"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn ".claude/skills-bundle" "$TEMPLATE/.claude/skills-bundle" \
      "$INSTANCE/.claude/skills-bundle"
    UPDATED+=(".claude/skills-bundle/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      "$TEMPLATE/.claude/skills-bundle/" "$INSTANCE/.claude/skills-bundle/"
    UPDATED+=(".claude/skills-bundle/")
    # DGN-406 FIX: re-substitute mint placeholders on the freshly rsynced bundle
    # skills (mirror of section 3i's subst_skill_dir call). The plain rsync above
    # copies template SKILL.md files carrying __USER_LABEL__ etc.; without this
    # loop the live instance keeps unsubstituted placeholders. Skipped in dry-run
    # (nothing was actually copied). subst_skill_dir is idempotent.
    if [ "$DRY_RUN" = "0" ]; then
      for _bundle_skill_dir in "$INSTANCE/.claude/skills-bundle"/*/; do
        [ -d "$_bundle_skill_dir" ] || continue
        subst_skill_dir "$_bundle_skill_dir"
      done
    fi
  fi
fi

# 3j2) framework agent definitions (DGN-663). Files under .claude/agents/*.md
#     (baseline-editor, propagation-editor, release-closer) are framework-owned
#     subagent definitions. Before DGN-663 they were copied ONLY at mint time
#     and never delivered to existing instances by update -- so agent-definition
#     improvements never reached live agents, and any .dogany-preserve entry for
#     one false-warned "matched no section" (outside sync scope).
#
#     Framework-owned area: plain rsync (NO --delete) so user-authored agent
#     defs are never pruned. Preserve-list supremacy holds: build_preserve_excludes
#     ".claude/agents" turns a per-file .dogany-preserve entry (e.g. Metal's
#     compressed baseline-editor.md) into an anchored rsync --exclude, so a
#     locally-modified agent def is HELD, not clobbered -- and the match is
#     recorded in _ALL_MATCHED_ENTRIES, curing the DGN-663 false-warn. The
#     section-root hold ("[.]claude/agents/") skips the whole rsync as usual.
#     Agent defs carry the __PROJECT_ROOT__ placeholder (release-closer.md), so
#     re-substitute in-loop after the rsync (mirror of section 3j's subst pass;
#     section 4's find set targets named subdirs, not .claude/).
if [ -d "$TEMPLATE/.claude/agents" ]; then
  ensure_dir "$INSTANCE/.claude/agents"
  build_preserve_excludes ".claude/agents"
  # DGN-385 FIX-1: section-root hold check (common to all sections).
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn ".claude/agents" "$TEMPLATE/.claude/agents" \
      "$INSTANCE/.claude/agents"
    UPDATED+=(".claude/agents/ (HELD -- skipped by .dogany-preserve)")
  else
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      "$TEMPLATE/.claude/agents/" "$INSTANCE/.claude/agents/"
    UPDATED+=(".claude/agents/")
    # Re-substitute mint placeholders on the freshly rsynced agent defs. Skipped
    # in dry-run (nothing was actually copied). subst_one is idempotent. A file
    # excluded by a per-file .dogany-preserve entry was NOT rsynced, so its
    # local copy is left exactly as the operator wrote it (subst_one on it is a
    # no-op re-run over an already-substituted local file; still harmless).
    if [ "$DRY_RUN" = "0" ]; then
      for _agent_def in "$INSTANCE/.claude/agents"/*.md; do
        [ -f "$_agent_def" ] || continue
        subst_one "$_agent_def"
      done
    fi
  fi
fi

# 3k) RULES.md -- framework constitution (DGN-130). RULES.md is framework-owned:
#     users are told never to edit it, so the framework may push updates to it.
#     We refresh it with the SAME user-edit-detection + backup contract as the
#     dogany-* skills (section 3i), so a hand-edited RULES.md is preserved as a
#     dated backup before being replaced -- never silently clobbered.
#
#     Source: $TEMPLATE/RULES.md (a symlink into rules/RULES.md; shasum/cp
#     dereference it). RULES.md carries NO mint placeholders, so it is
#     deliberately NOT run through subst_one and is NOT in section 4's find set
#     (which targets named subdirs, never the instance root) -- it is copied
#     verbatim, exactly as it ships.
#
#     Contract mirror of section 3i:
#       recorded (manifest) sha == instance sha  -> unmodified, just refresh.
#       differs, OR no manifest entry but instance != incoming -> user-modified:
#         back up to RULES.md.user-<timestamp> at the instance root, WARN, then
#         refresh. The backup sits at the instance root (a peer of RULES.md),
#         NOT under .claude/ -- it is the user's own copy of the constitution.
#       After refresh, record the freshly installed sha in the framework manifest.
if [ -f "$TEMPLATE/RULES.md" ]; then
  RULES_SRC="$TEMPLATE/RULES.md"
  RULES_DEST="$INSTANCE/RULES.md"
  rules_recorded="$(framework_manifest_sha 'RULES.md')"
  rules_cur="$(file_checksum "$RULES_DEST")"
  rules_incoming="$(file_checksum "$RULES_SRC")"
  rules_user_modified=0
  if [ -f "$RULES_DEST" ]; then
    if [ -n "$rules_recorded" ]; then
      [ "$rules_cur" != "$rules_recorded" ] && rules_user_modified=1
    else
      # No manifest entry (pre-DGN-130 instance): treat as modified only if the
      # instance copy actually differs from what we're about to install.
      [ "$rules_cur" != "$rules_incoming" ] && rules_user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    if [ "$rules_user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 RULES.md 백업 예정" \
          "  [dry-run] would back up user-modified RULES.md"
    fi
    if [ "$rules_cur" != "$rules_incoming" ]; then
      msg "  [dry-run] RULES.md 갱신 예정" "  [dry-run] would refresh RULES.md"
    else
      msg "  [dry-run] RULES.md 최신 (변경 없음)" "  [dry-run] RULES.md already current"
    fi
    # Do NOT copy, back up, or touch the framework manifest in dry-run.
  else
    if [ "$rules_user_modified" = "1" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      bak="$INSTANCE/RULES.md.user-$ts"
      cp -p "$RULES_DEST" "$bak" || die "failed to back up user-modified RULES.md"
      msg "  [update][경고] 사용자 수정 RULES.md 발견 -- 백업: $bak" \
          "  [update][WARN] user-modified RULES.md detected -- backed up to: $bak"
    fi
    # Refresh verbatim (dereference the source symlink; preserve source mode).
    cp -pL "$RULES_SRC" "$RULES_DEST"
    # Record the freshly installed sha in the framework manifest (upsert the
    # RULES.md line; leave any future framework-file lines intact).
    rules_installed="$(file_checksum "$RULES_DEST")"
    mkdir -p "$INSTANCE/.claude"
    fw_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
    {
      printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
      printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
      printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
      if [ -f "$FRAMEWORK_MANIFEST" ]; then
        grep -vE '^#|^RULES\.md[[:space:]]' "$FRAMEWORK_MANIFEST" 2>/dev/null || true
      fi
      printf 'RULES.md  %s\n' "$rules_installed"
    } > "$fw_tmp"
    mv -f "$fw_tmp" "$FRAMEWORK_MANIFEST"
  fi
  UPDATED+=("RULES.md (framework constitution)")
fi

# 3k3) telegram.md -- framework output-contract doc (DGN-875). telegram.md is
#     framework-owned: it defines the Telegram channel layer of the bridge output
#     contract and must stay in sync with the canonical template so all live
#     instances receive improvements to message-structure rules (tables, markup,
#     [[OPTIONS]], send_file, etc.). It carries NO mint placeholders, so it is
#     copied verbatim (cp -pL) -- same contract as RULES.md in section 3k.
#     User-edit detection + backup: if the instance copy was hand-edited after
#     the last framework install, it is backed up to telegram.md.user-<timestamp>
#     at the instance root before being replaced.
if [ -f "$TEMPLATE/telegram.md" ]; then
  TELEGRAM_SRC="$TEMPLATE/telegram.md"
  TELEGRAM_DEST="$INSTANCE/telegram.md"
  telegram_recorded="$(framework_manifest_sha 'telegram.md')"
  telegram_cur="$(file_checksum "$TELEGRAM_DEST")"
  telegram_incoming="$(file_checksum "$TELEGRAM_SRC")"
  telegram_user_modified=0
  if [ -f "$TELEGRAM_DEST" ]; then
    if [ -n "$telegram_recorded" ]; then
      [ "$telegram_cur" != "$telegram_recorded" ] && telegram_user_modified=1
    else
      # No manifest entry (pre-DGN-875 instance): treat as modified only if the
      # instance copy actually differs from what we're about to install.
      [ "$telegram_cur" != "$telegram_incoming" ] && telegram_user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    if [ "$telegram_user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 telegram.md 백업 예정" \
          "  [dry-run] would back up user-modified telegram.md"
    fi
    if [ "$telegram_cur" != "$telegram_incoming" ]; then
      msg "  [dry-run] telegram.md 갱신 예정" "  [dry-run] would refresh telegram.md"
    else
      msg "  [dry-run] telegram.md 최신 (변경 없음)" "  [dry-run] telegram.md already current"
    fi
    # Do NOT copy, back up, or touch the framework manifest in dry-run.
  else
    if [ "$telegram_user_modified" = "1" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      bak="$INSTANCE/telegram.md.user-$ts"
      cp -p "$TELEGRAM_DEST" "$bak" || die "failed to back up user-modified telegram.md"
      msg "  [update][경고] 사용자 수정 telegram.md 발견 -- 백업: $bak" \
          "  [update][WARN] user-modified telegram.md detected -- backed up to: $bak"
    fi
    # Refresh verbatim (dereference the source symlink; preserve source mode).
    cp -pL "$TELEGRAM_SRC" "$TELEGRAM_DEST"
    # Record the freshly installed sha in the framework manifest (upsert the
    # telegram.md line; leave RULES.md, bridge.md, and any other lines intact).
    telegram_installed="$(file_checksum "$TELEGRAM_DEST")"
    mkdir -p "$INSTANCE/.claude"
    fw_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
    {
      printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
      printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
      printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
      if [ -f "$FRAMEWORK_MANIFEST" ]; then
        grep -vE '^#|^telegram\.md[[:space:]]' "$FRAMEWORK_MANIFEST" 2>/dev/null || true
      fi
      printf 'telegram.md  %s\n' "$telegram_installed"
    } > "$fw_tmp"
    mv -f "$fw_tmp" "$FRAMEWORK_MANIFEST"
  fi
  UPDATED+=("telegram.md (framework output contract)")
fi

# 3k4) bridge.md -- framework output-contract doc (DGN-875). bridge.md is
#     framework-owned: it defines the channel-agnostic layer of the bridge output
#     contract ([[OPTIONS]], send_file, message structure, table routing) and must
#     stay in sync so all instances receive updated contract rules. It carries NO
#     mint placeholders, so it is copied verbatim (cp -pL) -- same contract as
#     RULES.md in section 3k.
#     User-edit detection + backup: if the instance copy was hand-edited after
#     the last framework install, it is backed up to bridge.md.user-<timestamp>
#     at the instance root before being replaced.
if [ -f "$TEMPLATE/bridge.md" ]; then
  BRIDGE_SRC="$TEMPLATE/bridge.md"
  BRIDGE_DEST="$INSTANCE/bridge.md"
  bridge_recorded="$(framework_manifest_sha 'bridge.md')"
  bridge_cur="$(file_checksum "$BRIDGE_DEST")"
  bridge_incoming="$(file_checksum "$BRIDGE_SRC")"
  bridge_user_modified=0
  if [ -f "$BRIDGE_DEST" ]; then
    if [ -n "$bridge_recorded" ]; then
      [ "$bridge_cur" != "$bridge_recorded" ] && bridge_user_modified=1
    else
      # No manifest entry (pre-DGN-875 instance): treat as modified only if the
      # instance copy actually differs from what we're about to install.
      [ "$bridge_cur" != "$bridge_incoming" ] && bridge_user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    if [ "$bridge_user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 bridge.md 백업 예정" \
          "  [dry-run] would back up user-modified bridge.md"
    fi
    if [ "$bridge_cur" != "$bridge_incoming" ]; then
      msg "  [dry-run] bridge.md 갱신 예정" "  [dry-run] would refresh bridge.md"
    else
      msg "  [dry-run] bridge.md 최신 (변경 없음)" "  [dry-run] bridge.md already current"
    fi
    # Do NOT copy, back up, or touch the framework manifest in dry-run.
  else
    if [ "$bridge_user_modified" = "1" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      bak="$INSTANCE/bridge.md.user-$ts"
      cp -p "$BRIDGE_DEST" "$bak" || die "failed to back up user-modified bridge.md"
      msg "  [update][경고] 사용자 수정 bridge.md 발견 -- 백업: $bak" \
          "  [update][WARN] user-modified bridge.md detected -- backed up to: $bak"
    fi
    # Refresh verbatim (dereference the source symlink; preserve source mode).
    cp -pL "$BRIDGE_SRC" "$BRIDGE_DEST"
    # Record the freshly installed sha in the framework manifest (upsert the
    # bridge.md line; leave RULES.md, telegram.md, and any other lines intact).
    bridge_installed="$(file_checksum "$BRIDGE_DEST")"
    mkdir -p "$INSTANCE/.claude"
    fw_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
    {
      printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
      printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
      printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
      if [ -f "$FRAMEWORK_MANIFEST" ]; then
        grep -vE '^#|^bridge\.md[[:space:]]' "$FRAMEWORK_MANIFEST" 2>/dev/null || true
      fi
      printf 'bridge.md  %s\n' "$bridge_installed"
    } > "$fw_tmp"
    mv -f "$fw_tmp" "$FRAMEWORK_MANIFEST"
  fi
  UPDATED+=("bridge.md (framework output contract)")
fi

# 3k1) git-hooks/ -- tracked hook scripts (DGN-525). Framework-owned: the
#      pre-commit detached-HEAD guard lives here. Synced verbatim; exec bit
#      preserved via rsync -aL. Never deletes user-added hooks (no --delete).
#      write-if-absent semantics for any hook that does not yet exist; a hook
#      already present is refreshed (consistent with the rest of the manifest).
if [ -d "$REPO_ROOT/git-hooks" ]; then
  build_preserve_excludes "git-hooks"
  if [ "$SECTION_HELD" = "1" ]; then
    _section_held_warn "git-hooks" "$REPO_ROOT/git-hooks" "$INSTANCE/git-hooks"
    UPDATED+=("git-hooks/ (HELD -- skipped by .dogany-preserve)")
  else
    ensure_dir "$INSTANCE/git-hooks"
    rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
      "$REPO_ROOT/git-hooks/" "$INSTANCE/git-hooks/"
    UPDATED+=("git-hooks/")
  fi
fi

# 3k2) AGENT-OPS.md -- framework ops reference doc (DGN-387). A framework-owned
#     file at the instance root, like RULES.md, but with ONE deliberate
#     difference from 3k: AGENT-OPS.md carries a placeholder (__PROJECT_ROOT__),
#     so shas are recorded AND compared POST-SUBSTITUTION (the skills-channel
#     pattern), never verbatim. A substituted instance copy always differs from
#     the raw template incoming, so a verbatim compare would fire a spurious
#     "user-modified" WARN + backup on every fresh mint's first self-update.
#
#     Atomicity: DEST-ADJACENT mktemp (settings.json precedent, section 3g) so
#     the final mv is a same-filesystem rename, genuinely atomic -- $TMPDIR
#     mktemp forfeits mv atomicity across filesystems (tmpfs /tmp on Linux).
#
#     Contract: only __PROJECT_ROOT__/__HOME__ may appear (both outside the
#     IDENTITY_OK gate -> deterministic under IDENTITY_OK=0). Step 0 below
#     mechanically asserts the template carries no illegal identity token.
if [ -f "$TEMPLATE/AGENT-OPS.md" ]; then
  # Step 0: template-side placeholder-contract assert. Any dunder token other
  # than __PROJECT_ROOT__/__HOME__ is a template contract violation -- it would
  # substitute cleanly under IDENTITY_OK=1 and hide, so surface it loudly.
  ops_bad="$(grep -oE '__[A-Z][A-Z_]*__' "$TEMPLATE/AGENT-OPS.md" 2>/dev/null \
              | grep -vE '^__(PROJECT_ROOT|HOME)__$' | sort -u || true)"
  if [ -n "$ops_bad" ]; then
    msg "  [update][경고] AGENT-OPS.md 템플릿 규약 위반 -- 허용되지 않은 플레이스홀더:" \
        "  [update][WARN] AGENT-OPS.md template contract violation -- illegal placeholder(s):"
    printf '%s\n' "$ops_bad" >&2
  fi

  OPS_SRC="$TEMPLATE/AGENT-OPS.md"
  OPS_DEST="$INSTANCE/AGENT-OPS.md"
  # Step 1: build substituted incoming in a dest-adjacent temp. cp -pL carries
  # the source mode onto the 0600 mktemp file and dereferences a symlink source.
  ops_tmp="$(mktemp "$INSTANCE/AGENT-OPS.md.new.XXXXXX")"
  cp -pL "$OPS_SRC" "$ops_tmp"
  subst_one "$ops_tmp"  # only __PROJECT_ROOT__/__HOME__ fire -> deterministic

  # Step 2: gather the three shas (all post-substitution for incoming).
  ops_incoming_sub="$(file_checksum "$ops_tmp")"
  ops_cur="$(file_checksum "$OPS_DEST")"
  ops_recorded="$(framework_manifest_sha 'AGENT-OPS.md')"

  # Step 3: user_modified iff dest exists AND (recorded present AND cur !=
  #   recorded) OR (no manifest entry AND cur != incoming_sub). Because mint
  #   records the sha (mint.sh), the no-entry branch is a legacy/repair corner
  #   (manifest lost, pre-3k2 hand-drop), NOT the normal fresh-mint path.
  ops_user_modified=0
  if [ -f "$OPS_DEST" ]; then
    if [ -n "$ops_recorded" ]; then
      [ "$ops_cur" != "$ops_recorded" ] && ops_user_modified=1
    else
      [ "$ops_cur" != "$ops_incoming_sub" ] && ops_user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    # Step 4: report only; write nothing; drop the temp.
    if [ "$ops_user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 AGENT-OPS.md 백업 예정" \
          "  [dry-run] would back up user-modified AGENT-OPS.md"
    fi
    if [ "$ops_cur" != "$ops_incoming_sub" ]; then
      msg "  [dry-run] AGENT-OPS.md 갱신 예정" "  [dry-run] would refresh AGENT-OPS.md"
    else
      msg "  [dry-run] AGENT-OPS.md 최신 (변경 없음)" "  [dry-run] AGENT-OPS.md already current"
    fi
    rm -f "$ops_tmp"
  else
    # Step 5: real run. Back up a user-modified copy (instance-root peer, same
    #   as RULES), then atomic same-dir rename into place.
    if [ "$ops_user_modified" = "1" ]; then
      ts="$(date +%Y%m%d-%H%M%S)"
      ops_bak="$INSTANCE/AGENT-OPS.md.user-$ts"
      cp -p "$OPS_DEST" "$ops_bak" || die "failed to back up user-modified AGENT-OPS.md"
      msg "  [update][경고] 사용자 수정 AGENT-OPS.md 발견 -- 백업: $ops_bak" \
          "  [update][WARN] user-modified AGENT-OPS.md detected -- backed up to: $ops_bak"
    fi
    mv -f "$ops_tmp" "$OPS_DEST"
    # Re-checksum the INSTALLED file and upsert AGENT-OPS.md into the framework
    # manifest (filter its own line only; 3k's rewrite preserves foreign lines,
    # so the two upserts coexist).
    ops_installed="$(file_checksum "$OPS_DEST")"
    mkdir -p "$INSTANCE/.claude"
    ops_fw_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
    {
      printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
      printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
      printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
      if [ -f "$FRAMEWORK_MANIFEST" ]; then
        grep -vE '^#|^AGENT-OPS\.md[[:space:]]' "$FRAMEWORK_MANIFEST" 2>/dev/null || true
      fi
      printf 'AGENT-OPS.md  %s\n' "$ops_installed"
    } > "$ops_fw_tmp"
    mv -f "$ops_fw_tmp" "$FRAMEWORK_MANIFEST"
  fi
  # Step 6:
  UPDATED+=("AGENT-OPS.md (framework ops doc)")
fi

# ---------------------------------------------------------------------------
# 3l) PLAN backfill + BRIDGE_MODELS re-derive (DGN-590; idempotent).
#
#     DGN-590 supersedes the old DGN-167 BRIDGE_MODELS-only backfill. Every
#     self-update now (a) backfills config/agent.conf PLAN= when absent (probe
#     the subscription plan; keep-if-present -- update never overwrites the
#     user's conf, the deliberate asymmetry vs. install's probe-wins), then
#     (b) RE-DERIVES the .env BRIDGE_MODELS from that PLAN so a plan change or a
#     cached drift (the Warg 4-model cache != pro source case) auto-converges on
#     the next update.
#
#     BRIDGE_MODELS reconcile (only when PLAN resolved):
#       - key absent      -> append plan_bridge_models(PLAN)   (legacy backfill)
#       - key == expected -> no-op (order normalized to canonical if only order
#                            differs)
#       - key is a canonical 3- or 4-model set != expected -> REPLACE the line
#                            with expected (+ dated provenance comment)
#       - key is a custom/reduced list -> LEAVE untouched + a one-line notice
#     PLAN unresolved (probe failed): fail-open legacy path -- append the full
#     4-model list when the key is absent, leave it when present.
#
#     Placement: after all framework-file refreshes (sections 3a-3k), before
#     placeholder substitution (section 4). The .env is excluded from rsync
#     (COMMON_EXCLUDES) so it arrives here untouched by the refresh pass.
#     Provenance: any appended/replaced line gets a dated comment (DGN-246).
# ---------------------------------------------------------------------------

# KEEP-IN-SYNC (DGN-590): identical copies in install.sh / update.sh /
# scripts/pack/mint_run.sh -- P14 sweep asserts marker count == 3.
plan_bridge_models() {
  case "$1" in
    max_*) printf 'fable,opus,sonnet,haiku' ;;  # DGN-346 fable-first
    *)     printf 'opus,sonnet,haiku' ;;        # DGN-565 pro incl. opus
  esac
}

# Probe $HOME/.claude.json for the subscription plan slug (same rules as
# install.sh recommend_model). Prints pro|max_5x|max_20x on stdout. Exits
# non-zero on any failure (no python3 / missing file / parse error) -- the
# caller then writes NO PLAN and keeps the model list fail-open (P2-0). This is
# a LOCAL read of the current machine's Claude CLI credential file; no network.
resolve_PLAN() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "${HOME}/.claude.json" <<'PYEOF'
import sys, json
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    oa = data.get("oauthAccount") or {}
    tier  = str(oa.get("organizationRateLimitTier") or "").lower()
    otype = str(oa.get("organizationType") or "").lower()
    if "max_20x" in tier:
        print("max_20x")
    elif "max_5x" in tier:
        print("max_5x")
    elif "max" in tier or "max" in otype:
        print("max_5x")   # conservative: some max flavour, exact tier unknown
    else:
        print("pro")
except Exception:
    sys.exit(1)
PYEOF
}

# _models_set <csv> -- normalize a comma list to a sorted, de-duped space list
# (set identity, order-independent). Empty input -> empty.
_models_set() {
  printf '%s' "$1" | tr ',' '\n' | sed '/^$/d' | LC_ALL=C sort -u | tr '\n' ' '
}

# resolve_BRIDGE_MODELS_failopen -- always exits non-zero so backfill_env_key
# uses its conservative full-list fallback. Used ONLY on the pre-PLAN fail-open
# path (no PLAN in agent.conf AND plan probe failed).
resolve_BRIDGE_MODELS_failopen() { return 1; }

# backfill_env KEY resolve_func -- append KEY=<value> to the .env when absent.
# Never modifies, removes, or reorders existing lines. Idempotent: a second
# run is a no-op because grep finds the key on the first pass.
# Args: $1 = ENV_FILE path, $2 = key name, $3 = resolver function name.
backfill_env_key() {
  local env_file="$1" key="$2" resolver="$3"
  [ -f "$env_file" ] || return 0

  # Present means a non-commented line whose key matches exactly.
  if grep -qE "^${key}=" "$env_file" 2>/dev/null; then
    return 0  # key exists -- nothing to do
  fi

  # Resolve the default value; fall back conservatively on probe failure.
  local value
  if ! value="$("$resolver" 2>/dev/null)"; then
    # Probe failed: use full model list so the user is never left with less
    # than they would get from a fresh max-plan install. DGN-346: fable-first.
    value="fable,opus,sonnet,haiku"
  fi
  # Guard: resolver returned empty.
  [ -n "$value" ] || value="fable,opus,sonnet,haiku"

  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] .env 백필 예정: ${key}=${value}" \
        "  [dry-run] would backfill .env: ${key}=${value}"
    return 0
  fi

  # Append with a newline guard (some .env files lack a trailing newline) and
  # a dated provenance comment so operators can trace the addition back to this
  # update run.
  local stamp
  stamp="$(date +%Y-%m-%d)"
  # Ensure the file ends with a newline before appending.
  # wc -l returns leading-whitespace integers on macOS -- use -eq (arithmetic)
  # rather than = (string) to avoid comparing "0" against "       0".
  local last_char last_nl
  last_char="$(tail -c1 "$env_file" 2>/dev/null | wc -c)"
  if [ "$last_char" -gt 0 ]; then
    # File is non-empty; check whether it already ends in a newline.
    last_nl="$(tail -c1 "$env_file" | wc -l)"
    if [ "$last_nl" -eq 0 ]; then
      printf '\n' >> "$env_file"
    fi
  fi
  printf '# added by update.sh v%s (env backfill, %s) -- DGN-246\n' "$REPO_VERSION" "$stamp" >> "$env_file"
  printf '%s=%s\n' "$key" "$value" >> "$env_file"
  msg "  [update] .env 백필: ${key}=${value}" \
      "  [update] backfilled .env: ${key}=${value}"
}

# backfill_conf_key FILE KEY VALUE -- append KEY=VALUE to a conf file when the
# key is absent (keep-if-present). Mirrors backfill_env_key but targets
# config/agent.conf and takes a literal value (already resolved). Never modifies
# an existing line -- update.sh must not overwrite the user's PLAN choice.
backfill_conf_key() {
  local conf_file="$1" key="$2" value="$3"
  [ -n "$value" ] || return 0
  if [ -f "$conf_file" ] && grep -qE "^${key}=" "$conf_file" 2>/dev/null; then
    return 0  # present -- keep-if-present, never overwrite
  fi
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] agent.conf 백필 예정: ${key}=${value}" \
        "  [dry-run] would backfill agent.conf: ${key}=${value}"
    return 0
  fi
  mkdir -p "$(dirname "$conf_file")"
  touch "$conf_file"
  local last_char last_nl
  last_char="$(tail -c1 "$conf_file" 2>/dev/null | wc -c)"
  if [ "$last_char" -gt 0 ]; then
    last_nl="$(tail -c1 "$conf_file" | wc -l)"
    [ "$last_nl" -eq 0 ] && printf '\n' >> "$conf_file"
  fi
  printf '# added by update.sh v%s (PLAN backfill, %s) -- DGN-590\n' "$REPO_VERSION" "$(date +%Y-%m-%d)" >> "$conf_file"
  printf '%s=%s\n' "$key" "$value" >> "$conf_file"
  msg "  [update] agent.conf 백필: ${key}=${value}" \
      "  [update] backfilled agent.conf: ${key}=${value}"
}

# reconcile_bridge_models ENV_FILE EXPECTED -- re-derive the .env BRIDGE_MODELS
# line from EXPECTED (the plan-derived canonical list). See the 3l header for
# the full case table. Guards a custom/reduced list (leaves it + notice).
reconcile_bridge_models() {
  local env_file="$1" expected="$2" cur exp_set cur_set canon4 canon3
  [ -f "$env_file" ] || return 0
  canon4="$(_models_set 'fable,opus,sonnet,haiku')"
  canon3="$(_models_set 'opus,sonnet,haiku')"
  exp_set="$(_models_set "$expected")"

  if ! grep -qE '^BRIDGE_MODELS=' "$env_file" 2>/dev/null; then
    # Absent -> append expected (canonical) via the standard backfill path.
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] .env 백필 예정: BRIDGE_MODELS=${expected}" \
          "  [dry-run] would backfill .env: BRIDGE_MODELS=${expected}"
      return 0
    fi
    local last_char last_nl
    last_char="$(tail -c1 "$env_file" 2>/dev/null | wc -c)"
    if [ "$last_char" -gt 0 ]; then
      last_nl="$(tail -c1 "$env_file" | wc -l)"
      [ "$last_nl" -eq 0 ] && printf '\n' >> "$env_file"
    fi
    printf '# added by update.sh v%s (env backfill, %s) -- DGN-246/DGN-590\n' "$REPO_VERSION" "$(date +%Y-%m-%d)" >> "$env_file"
    printf 'BRIDGE_MODELS=%s\n' "$expected" >> "$env_file"
    msg "  [update] .env 백필: BRIDGE_MODELS=${expected}" \
        "  [update] backfilled .env: BRIDGE_MODELS=${expected}"
    return 0
  fi

  cur="$(grep -E '^BRIDGE_MODELS=' "$env_file" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
  cur_set="$(_models_set "$cur")"

  if [ "$cur_set" = "$exp_set" ]; then
    # Same set: normalize only if the on-disk order differs from canonical.
    if [ "$cur" != "$expected" ]; then
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] .env BRIDGE_MODELS 순서 정규화 예정: ${cur} -> ${expected}" \
            "  [dry-run] would normalize .env BRIDGE_MODELS order: ${cur} -> ${expected}"
        return 0
      fi
      local tmp; tmp="$(mktemp)"
      sed "s|^BRIDGE_MODELS=.*|BRIDGE_MODELS=${expected}|" "$env_file" > "$tmp" && mv "$tmp" "$env_file"
      msg "  [update] .env BRIDGE_MODELS 순서 정규화: ${expected}" \
          "  [update] normalized .env BRIDGE_MODELS order: ${expected}"
    fi
    return 0
  fi

  if [ "$cur_set" = "$canon4" ] || [ "$cur_set" = "$canon3" ]; then
    # A canonical set that no longer matches the plan -> re-point to expected.
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] .env BRIDGE_MODELS 재파생 예정: ${cur} -> ${expected}" \
          "  [dry-run] would re-derive .env BRIDGE_MODELS: ${cur} -> ${expected}"
      return 0
    fi
    local tmp; tmp="$(mktemp)"
    {
      awk -v repl="BRIDGE_MODELS=${expected}" \
          -v note="# re-derived by update.sh v${REPO_VERSION} from PLAN ($(date +%Y-%m-%d)) -- DGN-590" \
          '/^BRIDGE_MODELS=/{print note; print repl; next} {print}' "$env_file"
    } > "$tmp" && mv "$tmp" "$env_file"
    msg "  [update] .env BRIDGE_MODELS 재파생: ${cur} -> ${expected}" \
        "  [update] re-derived .env BRIDGE_MODELS: ${cur} -> ${expected}"
    return 0
  fi

  # Custom / reduced list -> leave untouched, notice only.
  msg "  [update] .env BRIDGE_MODELS 사용자 커스텀값 유지 (재파생 안 함): ${cur}" \
      "  [update] .env BRIDGE_MODELS is a custom value -- left untouched (no re-derive): ${cur}"
}

ENV_FILE="$INSTANCE/.telegram_bot/.env"
AGENT_CONF="$INSTANCE/config/agent.conf"

# (a) PLAN backfill (keep-if-present). Probe failure -> skip (P2-0 exception).
_resolved_plan=""
if _resolved_plan="$(resolve_PLAN 2>/dev/null)" && [ -n "$_resolved_plan" ]; then
  backfill_conf_key "$AGENT_CONF" PLAN "$_resolved_plan"
else
  _resolved_plan=""
  msg "  [update] 구독 플랜 프로브 실패 -- PLAN 미기입 (fail-open 유지)" \
      "  [update] subscription plan probe failed -- PLAN left unwritten (fail-open)"
fi

# (b) BRIDGE_MODELS re-derive from the (now possibly backfilled) PLAN.
# NOTE the trailing `|| true`: a conf without a PLAN= line makes grep exit 1,
# and under `set -eo pipefail` that aborted the whole update on exactly the
# documented fail-open path (probe failed AND no PLAN -- fresh bare mint).
# Found by the DGN-674 install smoke S11 leg.
_effective_plan=""
if [ -f "$AGENT_CONF" ]; then
  _effective_plan="$(grep -E '^PLAN=' "$AGENT_CONF" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
if [ -f "$ENV_FILE" ]; then
  if [ -n "$_effective_plan" ]; then
    reconcile_bridge_models "$ENV_FILE" "$(plan_bridge_models "$_effective_plan")"
  else
    # PLAN absent (pre-PLAN generation and probe failed): fail-open legacy path
    # -- backfill the full 4-model list only when the key is absent; leave any
    # existing value untouched.
    backfill_env_key "$ENV_FILE" BRIDGE_MODELS resolve_BRIDGE_MODELS_failopen
  fi
elif [ "$DRY_RUN" = "1" ]; then
  msg "  [dry-run] .env 없음 -- 백필 건너뜀 ($ENV_FILE)" \
      "  [dry-run] .env absent -- skipping backfill ($ENV_FILE)"
fi

# DGN-385: after all section-3 rsync/copy blocks, warn about any preserve
# entry that was never matched by any build_preserve_excludes call or
# is_preserved check -- such an entry silently protects nothing (typo / wrong
# path / removed section).
_preserve_check_invalid

# ---------------------------------------------------------------------------
# 3m) git hooks path -- wire the tracked git-hooks/ dir as the active hooks
#     path so the pre-commit guard is always active in this instance.
#     Idempotent: only sets the config key when it is absent or points
#     elsewhere; safe to run on every update.
# ---------------------------------------------------------------------------
if command -v git >/dev/null 2>&1 && [ -d "$INSTANCE/.git" ]; then
  _cur_hookspath="$(git -C "$INSTANCE" config --local core.hooksPath 2>/dev/null || true)"
  if [ "$_cur_hookspath" != "git-hooks" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] git config core.hooksPath = git-hooks 설정 예정" \
          "  [dry-run] would set git config core.hooksPath = git-hooks"
    else
      git -C "$INSTANCE" config core.hooksPath git-hooks
      msg "  [update] git config core.hooksPath = git-hooks" \
          "  [update] git config core.hooksPath = git-hooks"
    fi
    UPDATED+=("git config core.hooksPath=git-hooks")
  fi
fi

# ---------------------------------------------------------------------------
# 4) Re-substitute the five mint placeholders on the refreshed files.
#    Path placeholders (PROJECT_ROOT, HOME) are always safe to re-apply.
#    Identity placeholders are applied only when recovered from the manifest.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "0" ]; then
  # subst_one is hoisted above (defined before section 3g). Skills are already
  # substituted in-loop (section 3i) and settings.json is substituted atomically
  # at install (section 3g), so neither is listed here; this pass covers the
  # remaining refreshed framework files.
  # Substitute across refreshed framework file types, but NEVER identity/user
  # entrypoints (they carry the user's filled-in identity, not placeholders).
  while IFS= read -r -d '' f; do
    subst_one "$f"
  done < <(find \
      "$INSTANCE/bridge" "$INSTANCE/routines" "$INSTANCE/memory-engine" \
      "$INSTANCE/config" "$INSTANCE/service" "$INSTANCE/database" \
      "$INSTANCE/worklog/_TEMPLATE.md" \
      \( -name '*.py' -o -name '*.sh' -o -name '*.json' -o -name '*.plist' \
         -o -name '*.service' -o -name '*.timer' \
         -o -name '*.md' -o -name '*.conf' -o -name '*.txt' -o -name '*.example' \) \
      -type f \
      -not -path '*/venv/*' -not -path '*/__pycache__/*' -not -name '*.bak.*' \
      -print0 2>/dev/null)

  # Rename any freshly-copied generic units to carry the agent name (mint step 3).
  # Covers macOS plists and the Linux mirror systemd units (DGN-268 S3
  # .service/.timer) -- without .service/.timer here an updated Linux instance
  # would keep generic telegram-agent units with literal __PROJECT_ROOT__ etc.
  if [ "$IDENTITY_OK" = "1" ]; then
    for p in "$INSTANCE"/bridge/*.plist "$INSTANCE"/routines/*.plist \
             "$INSTANCE"/routines/*.service "$INSTANCE"/routines/*.timer; do
      [ -e "$p" ] || continue
      np="${p//telegram-agent/$AGENT_NAME}"
      [ "$np" = "$p" ] && continue
      if [ ! -e "$np" ]; then
        mv "$p" "$np"
      else
        # Agent-named plist already exists (already-minted instance): the freshly
        # rsynced generic telegram-agent copy is pure cruft -- remove it rather
        # than leave it lying in the instance forever.
        rm -f "$p"
      fi
    done

    # DGN-227 MAJOR-5: plists.defer carries literal telegram-agent plist
    # basenames and has no extension, so neither the section-4 subst_one pass
    # (name-filtered) nor the plist rename loop above touches it. Substitute it
    # here so its entries keep matching the renamed plist filenames -- otherwise
    # a later defer-honoring loader (pack_install STEP 10 mint_run start) treats
    # the entries as non-matching and bootstraps the generic-brief units onto the
    # live user channel by default. Same substitution as mint.sh step 3a.
    if [ -f "$INSTANCE/routines/plists.defer" ]; then
      sed_inplace "$INSTANCE/routines/plists.defer" \
        -e "s/telegram-agent/$AGENT_NAME/g"
    fi

    # DGN-140: (re)register the polling watchdog now that the watchdog files
    # are refreshed, substituted, and renamed. Non-fatal by contract.
    if [ -f "$INSTANCE/bridge/watchdog_setup.sh" ]; then
      bash "$INSTANCE/bridge/watchdog_setup.sh" \
        || msg "[update][경고] 워치독 등록에 실패했습니다 (무시하고 진행)." \
               "[update][WARN] Watchdog registration failed (continuing)."
    fi
  fi

  # DGN-593 (3-3): write the bridge per-file manifest NOW -- after section 4
  # substitution + plist rename -- so every recorded sha reflects the FINAL
  # on-disk instance bytes (hashing pre-substitution would misread the next
  # run's I==M compare as a user edit; skills-channel precedent).
  #   * classification-channel entries: kept lines verbatim + re-checksummed
  #     landed/in-sync files (deferred NEW_MANIFEST_LINES-style: dry-run never
  #     reaches this block, so it records nothing).
  #   * substitution-channel files (self_restart.sh / watchdog_setup.sh /
  #     UPSTREAM.md / *.plist at their renamed instance paths): recorded for
  #     report/diagnostics only -- their channel is unconditional, so these
  #     shas are never a landing gate.
  if [ "$BRIDGE_RECONCILE_RAN" = "1" ]; then
    mkdir -p "$INSTANCE/.claude"
    _bm_tmp="$(mktemp "${BRIDGE_MANIFEST}.XXXXXX")"
    {
      printf '# .dogany-bridge.sha -- per-file checksums of bridge/ as last installed\n'
      printf '# by dogany-agent update.sh (DGN-593). Used for the 3-way per-file\n'
      printf '# reconcile. Format: "<relpath>  <sha>".\n'
      for _l in ${BRIDGE_M_LINES[@]+"${BRIDGE_M_LINES[@]}"}; do
        printf '%s\n' "$_l"
      done
      for _rk in ${BRIDGE_M_RECHECK[@]+"${BRIDGE_M_RECHECK[@]}"}; do
        # DGN-677: re-attach the #adopted-provisional inline tag onto the sha
        # line of any class-5 adopted file so it persists across this atomic
        # rewrite (BRIDGE_M_LINES rebuilds from $1+$2 only and would drop $3).
        _rk_tag=""
        for _pv in ${BRIDGE_M_PROVISIONAL[@]+"${BRIDGE_M_PROVISIONAL[@]}"}; do
          if [ "$_pv" = "$_rk" ]; then _rk_tag=" $BRIDGE_PROVISIONAL_TAG"; break; fi
        done
        printf '%s  %s%s\n' "$_rk" "$(file_checksum "$INSTANCE/$_rk")" "$_rk_tag"
      done
      for _sf in "$INSTANCE"/bridge/self_restart.sh "$INSTANCE"/bridge/watchdog_setup.sh \
                 "$INSTANCE"/bridge/UPSTREAM.md "$INSTANCE"/bridge/*.plist; do
        [ -f "$_sf" ] || continue
        printf 'bridge/%s  %s\n' "$(basename "$_sf")" "$(file_checksum "$_sf")"
      done
    } > "$_bm_tmp"
    mv -f "$_bm_tmp" "$BRIDGE_MANIFEST"
  fi

  # ---------------------------------------------------------------------------
  # DGN-907 post-flight contract gate: after reconcile, the LIVE instance must
  # satisfy the shell-rail push contract -- routines/push.sh's sanitize hop
  # (`from bridge.formatting import sanitize_message_for_telegram`) must
  # resolve and render against the formatting.py NOW on disk. A preserved/
  # frozen bridge file that lost lockstep with routines core (DGN-906:
  # ImportError -> silent plain-text degrade + raw HTML tag leak) fails HERE,
  # loudly: warning push + exit 4 BEFORE the DOGANY_FW_VERSION stamp, so the
  # release is never marked consumed by a run that left the rail broken.
  # (self-update.sh propagates the rc, so a failed gate also skips the
  # automatic bridge restart.) No interpreter available -> WARN-skip: that
  # host's push rail degrades for a reason no update can fix, and blocking
  # every future update there would only make it worse.
  if [ -f "$INSTANCE/bridge/formatting.py" ] && [ -f "$INSTANCE/routines/push.sh" ]; then
    if [ -z "$(_contract_python)" ]; then
      msg "[update][경고] shell-rail 계약 게이트 건너뜀 -- 사용할 python 인터프리터가 없습니다." \
          "[update][WARN] shell-rail contract gate skipped -- no python interpreter available." >&2
    elif contract_smoke "$INSTANCE"; then
      msg "[update] shell-rail 계약 게이트: OK (push.sh sanitize 임포트가 live formatting.py에서 resolve됨)" \
          "[update] shell-rail contract gate: OK (push.sh sanitize import resolves against live formatting.py)"
    else
      printf '%s\n' "============================================================" >&2
      msg "[update][오류] shell-rail 계약 게이트 실패: push.sh sanitize 계약이 live bridge/formatting.py에서 성립하지 않습니다." \
          "[update][ERROR] shell-rail contract gate FAILED: the push.sh sanitize contract does not hold against live bridge/formatting.py." >&2
      msg "  원인 후보: .claude/.dogany-preserve 핀이 bridge 파일을 동결(락스텝 파손), 또는 벤더 payload 결함." \
          "  Likely causes: a .claude/.dogany-preserve pin freezing a bridge file (lockstep break), or a defective vendor payload." >&2
      msg "  업데이트를 실패 처리합니다(exit 4) -- 버전 스탬프 없음. 원인 해소 후 업데이트를 재실행하세요." \
          "  Failing the update (exit 4) -- no version stamp. Fix the cause, then re-run the update." >&2
      printf '%s\n' "============================================================" >&2
      # Warning push: push.sh itself survives a broken sanitizer (it degrades
      # to plain text), so this warning still reaches the owner. Never fatal.
      # User-facing copy below: confirmed by owner 2026-08-16 (dec-094 UX gate).
      if [ "$DOGANY_LANG" = "ko" ]; then
        _dgn907_pushmsg="업데이트 점검 실패: 알림 전송 기능이 새 버전과 맞물리지 않아 업데이트를 완료하지 못했습니다. 메탈이 확인 후 재실행하겠습니다."
      else
        _dgn907_pushmsg="Update check failed: the notification feature does not match the new version, so the update was not completed. Metal will check and re-run."
      fi
      bash "$INSTANCE/routines/push.sh" --text "$_dgn907_pushmsg" >/dev/null 2>&1 || true
      exit 4
    fi
  fi

  # ROLLBACK MODE checkpoint (DGN-673 B3, R2): in rollback mode ONLY, gate the
  # version stamp on DB/pin agreement. DB ahead of the target pin -> print the
  # DGN-672 restore delegation and exit 3 BEFORE the stamp below, so the stamp
  # is the rollback-COMPLETE marker (never a stamp-lie on a torn/incomplete
  # rollback, S10). Forward runs return immediately (no-op).
  rollback_db_checkpoint "$INSTANCE" "$REPO_ROOT"

  # Record the framework version this instance now matches.
  if [ -f "$INSTANCE/.instance.conf" ]; then
    if grep -q '^DOGANY_FW_VERSION=' "$INSTANCE/.instance.conf"; then
      sed_inplace "$INSTANCE/.instance.conf" \
        -e "s#^DOGANY_FW_VERSION=.*#DOGANY_FW_VERSION=${REPO_VERSION}#"
    else
      printf 'DOGANY_FW_VERSION=%s\n' "$REPO_VERSION" >> "$INSTANCE/.instance.conf"
    fi
  fi

  # Sanity: warn on ANY surviving dunder placeholder anywhere in the instance
  # (DGN-674 F1). Replaces the old named-alternation + DGN-387 root-md scans,
  # which drifted from the substitution list (they missed __AGENT_LANG__ and
  # dirs like database/service/mirror). A GENERIC __[A-Z][A-Z_]*__ scan cannot
  # drift as new framework tokens are added; the allowlist below is the 12
  # runtime-template tokens that are SUPPOSED to survive (cron/routine slot
  # tokens, claude-usage log format). MUST stay in sync with
  # tests/install_smoke.sh ALLOW_TOKENS (single source candidate -- DGN-674).
  # WARN-only: update runs on LIVE instances with user content; the hard gate
  # lives in mint.sh step 7 + smoke S2. `|| true` keeps a no-match (grep exit
  # 1) from aborting under set -e.
  _allow='__(ROOT|MINUTE|HOUR|HOMEDIR|NAME|LOGNAME|WEEKDAY_ENTRY|SCRIPT|LABEL|PROMPT|PATH|HTTP_STATUS)__'
  LEFT="$(grep -rIonE '__[A-Z][A-Z_]*__' "$INSTANCE" \
            --exclude-dir=venv --exclude-dir=.git 2>/dev/null \
          | grep -vE ":${_allow}\$" || true)"
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
# DGN-593 (3-6): one-line bridge reconcile summary (conflict surface).
if [ "$BRIDGE_RECONCILE_RAN" = "1" ]; then
  msg "[update] bridge reconcile: ${BRIDGE_LANDED_N} landed / ${BRIDGE_ADOPTED_N} adopted / ${BRIDGE_PRESERVED_N} preserved / ${BRIDGE_CONFLICT_N} conflict (report: $BRIDGE_REPORT_FILE)" \
      "[update] bridge reconcile: ${BRIDGE_LANDED_N} landed / ${BRIDGE_ADOPTED_N} adopted / ${BRIDGE_PRESERVED_N} preserved / ${BRIDGE_CONFLICT_N} conflict (report: $BRIDGE_REPORT_FILE)"
  if [ "$BRIDGE_ADOPTED_N" -gt 0 ]; then
    # DGN-677 [P3 G-a]: adopt != land -- surface the required follow-up.
    msg "[update] bridge: ${BRIDGE_ADOPTED_N} adopted (provisional -- NOT landed; a follow-up update is required to land or resolve)" \
        "[update] bridge: ${BRIDGE_ADOPTED_N} adopted (provisional -- NOT landed; a follow-up update is required to land or resolve)"
  fi
fi
msg "[update] 보존됨: memories/, .telegram_bot/.env, *.db, bridge/venv, AGENT.md, USER.md, 사용자 스킬" \
    "[update] preserved: memories/, .telegram_bot/.env, *.db, bridge/venv, AGENT.md, USER.md, user skills"
if [ "$DRY_RUN" = "1" ]; then
  msg "[update] dry-run 완료 (변경 없음)." "[update] dry-run complete (no changes written)."
else
  msg "[update] 완료. 브릿지 재시작이 필요하면 승인 후 진행하세요." \
      "[update] done. If the bridge needs a restart, do so with approval."

  # ---------------------------------------------------------------------------
  # Auto-commit: if the instance is a git repo and the update left uncommitted
  # changes, commit them now so framework updates are captured in history.
  # Rationale: without this step, update.sh installs files but never commits,
  # causing silent drift accumulation (DGN-557).
  # Guard: a failed commit never fails the update (|| true); warning is printed
  # so the operator knows to investigate. .gitignore is respected by git add -A,
  # so runtime artifacts (*.user-*, skill-backups/, etc.) stay out.
  # ---------------------------------------------------------------------------
  if [ -d "$INSTANCE/.git" ]; then
    _porcelain="$(git -C "$INSTANCE" status --porcelain 2>/dev/null)"
    if [ -n "$_porcelain" ]; then
      _fw_ver="${REPO_VERSION:-unknown}"
      msg "[update] 변경 사항 자동 커밋 중 (프레임워크 ${_fw_ver}) ..." \
          "[update] auto-committing framework update changes (${_fw_ver}) ..."
      (
        git -C "$INSTANCE" add -A \
          && git -C "$INSTANCE" commit \
               -m "framework update ${_fw_ver} [auto]" \
               --no-verify
      ) || {
        msg "[update][경고] 자동 커밋 실패 -- 수동으로 커밋하세요 (git status -s 로 확인)." \
            "[update][WARN] auto-commit failed -- please commit manually (check: git status -s)."
      }
    else
      msg "[update] 인스턴스 git 변경 없음 -- 자동 커밋 건너뜀." \
          "[update] instance git: no changes after update -- auto-commit skipped."
    fi
  fi
fi

# WSL: nag (never fail) if the Windows-side setup drifted below the required version.
wsl_drift_nag
