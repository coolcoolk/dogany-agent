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
# resolve_channel_tag <repo_root> <channel>  (DGN-621 v2 phase1, DGN-626 W1)
#   Picks the update-target tag for the staged deployment model. Channels are
#   distinguished by TAG SUFFIX, not branches/repos, and every tag-consuming
#   channel is an explicit WHITELIST -- exclusion is never delegated to
#   versionsort ordering (DGN-626 F1):
#     release (default) : highest STABLE tag  vX.Y.Z  -- pre-release tags
#                         (any 'v*-*', e.g. -dev.N / -rc.N) are excluded.
#     dev               : highest of  vX.Y.Z | vX.Y.Z-dev.N  ONLY. An -rc.N
#                         tag is EXCLUDED: versionsort ranks -dev.N below
#                         -rc.N of the same base, so a live rc would both
#                         shadow every later -dev tag (ghost tags) and be
#                         auto-consumed by dev canaries (stamp pollution).
#     staging           : highest of  vX.Y.Z | vX.Y.Z-rc.N  ONLY (-dev
#                         excluded) -- the soak ring (DGN-626).
#   versionsort.suffix=- makes any hyphenated suffix rank BELOW its own
#   stable, so once the stable exists dev/staging subscribers move forward to
#   it and never regress to a pre-release. (Default git version-sort would
#   WRONGLY rank vX.Y.Z-dev.N ABOVE vX.Y.Z -- DGN-621 verified 2026-07-28 --
#   hence the explicit -c flag; it also makes ordering independent of global
#   config.)
#   DOGANY_UPDATE_PIN (optional, env): if set non-empty it OVERRIDES channel --
#     select exactly that tag; if the pinned tag is absent, FAIL LOUD (a pin
#     that cannot be honored is an operator error, never a silent latest).
#   Unknown channel: FAIL LOUD (die). The old silent release fallback let a
#     typo'd channel quietly consume stable -- fail-closed instead.
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
  case "$_rct_channel" in
    dev)
      # Whitelist: stable + -dev.N only (DGN-626 F1 -- -rc.N excluded).
      # versionsort.suffix=- ranks any pre-release below its stable (so a
      # promoted vX.Y.Z outranks vX.Y.Z-dev.N); default git sorts -dev ABOVE.
      git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]+)?$' | head -n1
      ;;
    staging)
      # Whitelist: stable + -rc.N only (-dev excluded). Same ranking flag:
      # the moment a base's stable is cut, staging subscribers advance to it
      # and never regress to an -rc.
      git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' | head -n1
      ;;
    release|"")
      # release (default): exclude pre-release tags (any tag whose name
      # contains a hyphen, e.g. -dev.N / -rc.N).
      git -C "$_rct_repo" tag --list 'v*' --sort=-v:refname \
        | grep -v -- '-' | head -n1
      ;;
    *)
      # Unknown channel = operator error: fail loud, never a silent release
      # fallback (a typo'd channel quietly consuming stable is DGN-626 F1's
      # root shape). Channel "main" never reaches here (pull path).
      die "unknown DOGANY_UPDATE_CHANNEL '${_rct_channel}' (valid here: release|dev|staging) -- refusing the silent release fallback (fix the channel declaration)"
      ;;
  esac
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
#   RULES.md                framework constitution v1 -- byte-frozen, kept on disk
#                           for pre-2.0 instances and dropped from the loader
#                           chain by migration step M (edit-detect + backup;
#                           DGN-130, freeze DGN-773 R7)
#   CONSTITUTION.md         2.0 framework constitution (edit-detect + backup;
#                           section 3k6, DGN-773 R-7)
#   CONTRACT.md             2.0 framework contract (edit-detect + backup;
#                           section 3k6, DGN-773 R-7)
#   AGENTS.md               2.0 loader hub, the file CLAUDE.md chains into
#                           (edit-detect + backup; section 3k6, DGN-773 R-7)
#   telegram.md             framework output-contract doc, Telegram channel layer
#                           (edit-detect + backup; DGN-875). Root copy retired
#                           on relayout -- the 2.0 home is vendors/telegram.md
#   bridge.md               framework output-contract doc, channel-agnostic layer
#                           (edit-detect + backup; DGN-875). Retired by the
#                           DGN-1141 relayout: the base layer was dissolved
#                           into rules/hot.framework.md + the injected vendor
#   rules/hot.framework.md  2.0 layout: the merged framework rules (edit-detect
#   rules/cold.framework.why.md   + backup; section 3k6, DGN-1141)
#   vendors/telegram.md     2.0 layout: the Telegram vendor contract; loaded by
#                           spawner injection, never by the @ chain (DGN-1141)
#   playbooks/layout.md     2.0 layout: the instance layout playbook
#   playbooks/AGENT-OPS.md  framework ops reference (substituted; section 3k2)
#   git-hooks/              tracked hook scripts (pre-commit detached-HEAD guard;
#                           DGN-525); exec bit preserved; no --delete
#   scripts/secret-sweep.sh pre-push gate scanner + its identity-file example
#   scripts/sweep-identity.example
#                           (DGN-1058): the git-hooks/pre-push gate invokes
#                           <instance>/scripts/secret-sweep.sh, but scripts/
#                           was never a sync channel -- instances silently ran
#                           stale sweeps forever. Enumerated per-file (NOT a
#                           scripts/ dir channel: scripts/ also holds dev/
#                           release machinery that must never land in an
#                           instance); source = agents/.template/scripts/
#
# Everything NOT on this list is instance state / personal data and is never
# written: memories/, *.db, .env, sessions, runtime/, logs/, bridge/venv/,
# user-authored (non-dogany-) skills, and the identity entrypoints below.
#
# IDENTITY GUARD (DGN-130 -> DGN-773 R4 gap-1 -> DGN-1141 stage 6): the files
# an instance OWNS are NEVER a refresh target in any channel.
#
# The membership rule used to be a hardcoded array of three root filenames
# (the retired FRAMEWORK_NEVER_REFRESH holding PROFILE.md / USER.md /
# DISCIPLINE.md) plus a whitelist that had to be edited in the same commit or
# every update run died.
# DGN-1141 section 3.6 replaces the enumeration with a NAMING CONVENTION:
#
#     the loader reads   hot.*   plus at most one injected vendor
#     the updater NEVER overwrites   *.custom.*
#
# "custom" is the ownership token in the 2.0 layout (agents/.template):
#
#   identity/hot.custom.agent.md   the persona/identity entrypoint (agent name,
#                                  Role, accreted Workflows). Was AGENT.md
#                                  pre-2.0, PROFILE.md pre-relayout.
#   identity/hot.custom.owner.md   user facts. Was USER.md pre-relayout.
#   rules/hot.custom.md            the instance's own working-discipline
#                                  overlay. Was DISCIPLINE.md pre-relayout.
#                                  Never SEEDED either (DGN-1141 section 3.4:
#                                  an empty overlay file is pure noise) -- the
#                                  relayout migration creates it only when the
#                                  instance actually has local content to keep.
#
# WHY A CONVENTION IS STRONGER THAN THE ARRAY IT REPLACES: the array was inert
# data -- nothing consulted it at write time; it only tripped when a future
# editor changed the array without changing its twin whitelist. The convention
# is consulted by every framework write path (install_framework_root_doc dies
# on an instance-owned target), so a channel that tries to write an owned file
# fails LOUDLY at the moment of the write, not only when someone edits a list.
#
# LEGACY MEMBERS: the three pre-relayout root names are kept in the predicate
# below. An instance whose layout migration has not run yet (pre-2.0 release on
# the channel, DOGANY_ROLLBACK=1, or a fail-closed skip) still carries identity
# under those names, and the convention alone would not cover them. The
# predicate is therefore a strict SUPERSET of the retired array -- see
# tests/dgn773_t5_migrate_step_m_selftest.sh section [5], which diffs the two
# sets mechanically and fails if the convention ever protects LESS.
#
# Framework-owned files that DO live at the instance root or in the 2.0 layout
# and ARE refreshed, each by its own channel: RULES.md (section 3k, verbatim --
# v1 frozen, kept for pre-2.0 instances), CONSTITUTION.md / CONTRACT.md /
# AGENTS.md (section 3k6, verbatim -- DGN-773 R-7; the first two only while the
# instance is still pre-relayout), rules/hot.framework.md /
# rules/cold.framework.why.md / vendors/telegram.md / playbooks/layout.md
# (section 3k7, verbatim -- DGN-1141) and playbooks/AGENT-OPS.md (section 3k2,
# substituted -- DGN-387). None of them carries a ".custom." segment, which is
# exactly the point: the name declares the ownership.
#
# TRANSITION NOTE (2.0 window, DGN-773 R7 -> DGN-1141 stage 6): the AGENT.md ->
# PROFILE.md compat symlink was the R7 one-version window. The relayout
# migration retires BOTH root names (the real files move under identity/), so
# there is no compat symlink to guard here either -- and a REAL (non-symlink)
# AGENT.md next to a PROFILE.md is still a collision, rejected by migration
# step M, not here.

# is_instance_owned <path-or-name> -- true when the path is instance-owned and
# must never be written by a framework channel. Basename-scoped on purpose: the
# token is part of the FILE NAME, so the rule reads the same whether a caller
# passes "rules/hot.custom.md" or an absolute path.
is_instance_owned() {
  case "${1##*/}" in
    *.custom.*)                          return 0 ;;  # infix: hot.custom.md
    custom.*)                            return 0 ;;  # prefix: custom.files.md
    PROFILE.md|USER.md|DISCIPLINE.md)    return 0 ;;  # pre-relayout legacy
    AGENT.md)                            return 0 ;;  # pre-2.0 legacy
  esac
  return 1
}

# Self-check, kept where the array's guard used to run so the failure mode
# stays greppable: the predicate must answer YES for every member of the
# retired array (superset property) and NO for every framework-owned name that
# has a refresh channel. A future edit that narrows the convention trips here
# instead of silently clobbering identity on the next live update.
assert_instance_ownership_convention() {
  local f
  for f in "identity/hot.custom.agent.md" "identity/hot.custom.owner.md" \
           "rules/hot.custom.md" "PROFILE.md" "USER.md" "DISCIPLINE.md" "AGENT.md" \
           "playbooks/custom.files.md" "vendors/custom.telegram.md"; do
    if ! is_instance_owned "$f"; then
      die "OWNERSHIP CONVENTION violated: '$f' is instance-owned but is_instance_owned() says no"
    fi
  done
  for f in "RULES.md" "CONSTITUTION.md" "CONTRACT.md" "AGENTS.md" "CLAUDE.md" \
           "rules/hot.framework.md" "rules/cold.framework.why.md" \
           "vendors/telegram.md" "playbooks/layout.md" "playbooks/AGENT-OPS.md" \
           "playbooks/customize.md"; do
    if is_instance_owned "$f"; then
      die "OWNERSHIP CONVENTION violated: '$f' has a refresh channel but is_instance_owned() says it is owned"
    fi
  done
  return 0
}
assert_instance_ownership_convention

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
          # The ROLLBACK runbook is deliberately named WITHOUT its repo path:
          # it is held back from the public mirror (DGN-1097), and gate E4
          # (publish-ref-audit) blocks any consumer-facing literal that names
          # an unshipped repo file (DGN-1056).
          msg "  (여러 파일은 쉼표로 연결; 런북: ROLLBACK 런북 단계 0/4 -- 업스트림 소스 저장소, 공개 미러 미포함)" \
              "  (comma-join multiple tokens; runbook: ROLLBACK steps 0/4 -- upstream source repo, not shipped in this mirror)" >&2
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
#      ALSO not covered, and this one is a real gap rather than a design choice
#      (DGN-1141 stage 8, M11 MINOR-3): every doc installed by
#      install_framework_root_doc() -- AGENTS.md, rules/hot.framework.md,
#      rules/cold.framework.why.md, vendors/telegram.md, playbooks/layout.md,
#      README.md -- ignores .dogany-preserve entirely (that helper never calls
#      is_preserved(); section 3k8's RETIREMENT of root docs does honour it).
#      Pinning one of those names does not hold it, and _preserve_check_invalid
#      then reports the entry as never-matched, which reads to an owner like a
#      typo rather than an unimplemented pin. Honouring it was NOT done here on
#      purpose: these six are the loader chain and its targets, and a pin that
#      freezes hot.framework.md would freeze the constitution the agent reads
#      while every other doc moves on -- the divergence would be invisible and
#      permanent. Documented rather than implemented; revisit only with an
#      explicit expiry mechanism.
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

# Framework single-file manifest (DGN-130): records the sha of framework-owned
# FILES (RULES.md, telegram.md, bridge.md, playbooks/AGENT-OPS.md and -- since DGN-773
# R-7 -- CONSTITUTION.md / CONTRACT.md / AGENTS.md) as this script last
# installed them, exactly like .dogany-skills.sha does for skill dirs. Used to
# detect a user edit before a refresh overwrites it. Format: "<relpath>  <sha>".
# Kept separate from the skills manifest so the two channels never race on one
# file.
# HOISTED here (DGN-773 T5): migration step M sits at the 3f/3g boundary, above
# the old definition site, and records the docs it installs in this manifest.
FRAMEWORK_MANIFEST="$INSTANCE/.claude/.dogany-framework.sha"
framework_manifest_sha() {
  local rel="$1"
  [ -f "$FRAMEWORK_MANIFEST" ] || { printf '%s' ""; return; }
  awk -v n="$rel" '$1==n {print $2; exit}' "$FRAMEWORK_MANIFEST"
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
      # Channel-aware target tag (DGN-621 v2 phase1, DGN-626 W1). release =
      # highest STABLE tag; dev = stable|-dev.N whitelist; staging =
      # stable|-rc.N whitelist; unknown channel dies loud;
      # DOGANY_UPDATE_PIN overrides (fails loud if the pinned tag is missing).
      # Stable-only estate w/o PIN on release/unset == the old one-liner result.
      LATEST_TAG="$(resolve_channel_tag "$REPO_ROOT" "${DOGANY_UPDATE_CHANNEL:-release}")"
      [ -n "$LATEST_TAG" ] || die "no release tag (v*) found -- cannot resolve a published release"
      # DGN-626 W1: hand the consumed tag NAME to the --no-pull child (the
      # extracted tree carries no .git, so the child cannot re-derive which
      # tag it came from) -- consumed by the DOGANY_FW_TAG landing stamp.
      DOGANY_UPDATE_SRC_TAG="$LATEST_TAG"
      export DOGANY_UPDATE_SRC_TAG
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

# ---------------------------------------------------------------------------
# fw_reqframework_guard <instance_dir> <target_version> <dry_run 0|1>
#   (DGN-1031, revised after fleet verification found the first cut was a
#   no-op for the live estate -- see the UNKNOWN section below)
#
#   Framework-update-time compat gate: BEFORE any framework file is landed
#   (section 3) or the preflight prompt is shown, evaluate every pack MOUNTED
#   on this instance (.instance.conf DOGANY_PACKS ledger) against the TARGET
#   framework version this run is about to install ($REPO_VERSION -- NOT the
#   instance's current framework version).
#
#   The gap this closes: compat-lint.sh C2 already checks requires_framework
#   vs framework VERSION, but ONLY at pack-INSTALL time
#   (scripts/pack/compat-lint.sh:663-679, called from
#   scripts/pack/pack_install.sh:869-893 install-side). There was no
#   equivalent check at framework-UPDATE time -- a pack could declare
#   requires_framework=">=1.30.0 <2.0.0" and framework 2.0 would land anyway,
#   leaving the pack mounted somewhere its own manifest says it cannot run
#   (measured; DGN-1031 blocker 2). tests/dgn1031_reqfw_guard_selftest.sh
#   covers this function.
#
#   Cached record: config/packs/<id>.requires_framework, one file per mounted
#   pack, written by pack_install.sh's _reqfw_record() at install time.
#
#   BACKFILL (new): a pack installed BEFORE this fix shipped has no cached
#   record (measured live: ~/.dogany/agents/Ag and .../Warg both mount
#   lifekit@1.2.0 with zero config/packs/ records). The first cut treated
#   "no record" as "no constraint" unconditionally -- which made the guard a
#   silent no-op for exactly the fleet it exists to protect: 0 records
#   evaluated, PASS, update proceeds past a pack whose manifest explicitly
#   excludes the target. Fixed by resolving the manifest on the spot and
#   caching the result, so the record is populated on this and every future
#   run without a separate migration step:
#     - DOGANY_REPO_ROOT is read straight out of the INSTANCE's own
#       .instance.conf (not this script's own $REPO_ROOT). This matters: the
#       DEFAULT update channel ("release", self-update.sh) invokes update.sh
#       from a THROWAWAY `git archive` extraction with no sibling checkouts,
#       so package_dir entries like "../../dogany-lifekit" resolve to
#       nothing relative to that temp tree. DOGANY_REPO_ROOT, by contrast, is
#       the STABLE canonical checkout path recorded at mint/last-update time
#       (verified live: the estate's domain instances record
#       DOGANY_REPO_ROOT=$HOME/dogany/dogany-agent, and
#       <that>/packs/catalog.json's lifekit package_dir
#       "../../dogany-lifekit" resolves to the real, present
#       $HOME/dogany/dogany-lifekit checkout with a readable
#       pack-manifest.json). Resolving against the durable path, not the
#       currently-executing script's own path, is what makes auto-backfill
#       reliable across every channel.
#     - Auto-backfill (not a standalone registrar script): a separate
#       "run this once per instance before 2.0" script is an availability
#       risk in exactly the failure mode this ticket is about -- an
#       instance nobody remembers to run it against stays unprotected.
#       Backfilling INSIDE the guard is self-healing: every future update
#       run (cron-driven self-update included) populates the cache the
#       moment it is missing, with no separate operator action required.
#       DRY_RUN respects "write nothing": resolution + evaluation still run
#       (so a dry-run preview is never blind to a real violation), but the
#       cache file is only PERSISTED when dry_run=0.
#     - Manifest reachable, but the manifest itself declares no
#       requires_framework -> SKIP (confirmed no constraint; no cache file
#       written, mirrors _reqfw_record's own absent-field behavior so a
#       later manifest edit that ADDS the field is picked up next run
#       instead of going stale in a cache).
#
#   UNKNOWN (new): a mounted pack with NO cached record AND an unresolvable
#   manifest (catalog entry missing, sibling repo not checked out on this
#   machine, DOGANY_REPO_ROOT stale/absent, manifest unparseable) is a THIRD
#   state, distinct from both "confirmed no constraint" (SKIP) and "confirmed
#   violation" (FAIL) -- we genuinely do not know. Treating UNKNOWN as SKIP
#   was the exact bug just fixed (a check that passes because it could not
#   even inspect its candidate); treating it as an unconditional FAIL would
#   lock out every instance whose pack source repo simply is not checked out
#   on the machine running the update, for framework patch releases that
#   almost certainly do not touch that pack's compatibility surface at all.
#   Resolution (DGN-1031 dispatch decision): severity scales with how big the
#   version jump is, mirroring the semver contract the requires_framework
#   field itself is written against (breaking changes live at MAJOR bumps;
#   that is the whole reason packs write ranges like "<2.0.0" instead of
#   pinning exact versions):
#     - target MAJOR != current installed MAJOR (DOGANY_FW_VERSION in
#       .instance.conf) -> UNKNOWN BLOCKS. A major bump is precisely the
#       scenario a requires_framework ceiling exists to catch, so "cannot
#       prove compatibility" must fail closed here -- this is the 2.0 case.
#     - current version itself unparseable/absent -> BLOCKS (cannot prove
#       the bump is NOT major, so assume it is -- same fail-closed direction
#       as every other uncertain input in this script).
#     - otherwise (minor/patch bump, both versions parse, majors equal) ->
#       WARN: printed loudly, counted separately in the summary, but does
#       NOT fail the guard. A minor/patch bump breaking an unresolvable
#       pack's declared floor/ceiling would be a semver violation on the
#       FRAMEWORK's side, not something this instance can reasonably be
#       locked out for.
#
#   DOGANY_PACKS enumeration mirrors the exact "DOGANY_PACKS=<id>@<ver>,..."
#   comma-list / split-on-first-@ parsing scripts/pack/pack_install.sh's
#   C-KITDEP requires_kit satisfaction gate already uses (pack_install.sh
#   ~line 450-462) -- not sourced as a shared function (pack_install.sh is a
#   separate script with no library seam for this), mirrored deliberately,
#   the same lockstep-duplication pattern this codebase already uses between
#   compat-lint.sh and pack_install.sh for KIT_NAME/C-KITDEP. If either
#   side's DOGANY_PACKS format changes, change both.
#
#   Semver range grammar + satisfaction (>= > <= < == !=, space-separated
#   AND, x.y.z only) is SINGLE-SOURCED in scripts/pack/lib/semver_range.py
#   (DGN-1031 residual debt 1 -- this used to be a lockstep duplicate of
#   compat-lint.sh's copy; both consumers now import the same module).
#   Resolution order: $DOGANY_SEMVER_LIB (set by sed-extraction consumers
#   like scripts/pack/update_plan.sh and the selftests, which source this
#   function OUTSIDE update.sh's own tree), else $REPO_ROOT/scripts/pack/
#   lib/semver_range.py. Release channel holds: the re-invoked update.sh
#   runs from a full `git archive` extraction of the target tag, which
#   carries scripts/pack/lib/ (no export-ignore in this repo -- the
#   extraction gate above re-confirms that premise each run), so REPO_ROOT
#   resolves the module there too. A missing/unloadable module FAILS the
#   whole guard (exit 1) -- fail closed, never "0 constraints checked,
#   pass".
#
#   Verdict per pack (four states, never collapsed into each other):
#     PASS  -- constraint known (cached or freshly resolved), target
#              satisfies it.
#     SKIP  -- constraint CONFIRMED absent: no cached record, but the
#              manifest WAS reachable and genuinely declares no
#              requires_framework field.
#     WARN  -- UNKNOWN (constraint unknowable: no record, unresolvable
#              manifest) on a minor/patch bump. Non-blocking, but never
#              silent -- always in the printed verdict list and the summary.
#     FAIL  -- malformed cached/resolved record, OR a confirmed violation,
#              OR UNKNOWN on a major bump. Any FAIL -> the whole guard fails
#              closed (exit 1, no bypass flag); the update refuses, naming
#              every FAIL pack (id, declared range or reason, target
#              version).
#
#   Silent-failure guard (measured defect class -- a check that "passes"
#   because it inspected zero candidates, THEN measured again live on Ag/Warg
#   and found the same class hiding one layer down: a check that passes
#   because every candidate it inspected was UNKNOWN): this function ALWAYS
#   prints "packs evaluated: N" (including N=0) AND a per-verdict summary
#   count (PASS/SKIP/WARN-UNKNOWN/FAIL) -- a run that is all SKIP/WARN looks
#   nothing like a run that is all PASS, on purpose.
# ---------------------------------------------------------------------------
fw_reqframework_guard() {
  local instdir="$1" target="$2" dry_run="${3:-0}"
  local conf="$instdir/.instance.conf"
  local packsdir="$instdir/config/packs"
  # Single-source semver comparator (see the header block above): env
  # override first (sed-extraction consumers), REPO_ROOT fallback second
  # (update.sh's own tree, release-archive tree included). ${REPO_ROOT:-}
  # keeps `set -u` extraction contexts alive; an empty/wrong path is
  # caught fail-closed by the loader below.
  local semver_lib="${DOGANY_SEMVER_LIB:-${REPO_ROOT:-}/scripts/pack/lib/semver_range.py}"
  python3 - "$conf" "$packsdir" "$target" "$dry_run" "$semver_lib" <<'PYEOF'
import json, re, sys, pathlib

conf_path = pathlib.Path(sys.argv[1])
packs_dir = pathlib.Path(sys.argv[2])
target = sys.argv[3]
dry_run = sys.argv[4] == "1"

# ---- .instance.conf: DOGANY_PACKS + DOGANY_FW_VERSION + DOGANY_REPO_ROOT --
entries = []
cur_fw = ""
repo_root_hint = ""
if conf_path.exists():
    for ln in conf_path.read_text(encoding='utf-8').splitlines():
        if ln.startswith('DOGANY_PACKS='):
            items = [x for x in ln.split('=', 1)[1].split(',') if x]
            for it in items:
                pid, _, ver = it.partition('@')
                if pid:
                    entries.append((pid, ver))
        elif ln.startswith('DOGANY_FW_VERSION='):
            cur_fw = ln.split('=', 1)[1].strip()
        elif ln.startswith('DOGANY_REPO_ROOT='):
            repo_root_hint = ln.split('=', 1)[1].strip()

print("[fw-reqfw-guard] INFO: packs evaluated: %d" % len(entries))
if not entries:
    print("[fw-reqfw-guard] INFO: 0 mounted packs found (.instance.conf absent, "
          "no DOGANY_PACKS line, or an empty list) -- nothing to gate, PASS")
    sys.exit(0)

# ---- semver comparator: single source (scripts/pack/lib/semver_range.py) --
# Loaded AFTER the 0-mounted-packs early exit (a pack-less instance never
# consults the comparator -- that path stays byte-identical), BEFORE any
# range/version evaluation. Unloadable module = the guard cannot evaluate
# ANY range = FAIL the guard (exit 1), never skip -- a missing comparator
# reading as "compatible" is exactly the silent-failure class this guard
# exists to kill.
try:
    import importlib.util
    _sv_spec = importlib.util.spec_from_file_location("semver_range", sys.argv[5])
    if _sv_spec is None or _sv_spec.loader is None:
        raise ImportError("not loadable as a python module")
    _sv = importlib.util.module_from_spec(_sv_spec)
    _sv_spec.loader.exec_module(_sv)
    parse_semver = _sv.parse_semver
    is_semver_range = _sv.is_semver_range
    satisfies = _sv.satisfies
except Exception as _sv_err:
    print("[fw-reqfw-guard] FAIL: semver comparator module unavailable "
          "(%s: %s) -- cannot evaluate any requires_framework range, "
          "fail closed" % (sys.argv[5] or "<empty path>", _sv_err))
    sys.exit(1)

# ---- version-bump severity (drives the UNKNOWN policy) --------------------
target_major = parse_semver(target)
cur_major = parse_semver(cur_fw)
if target_major is None or cur_major is None:
    is_major_bump = True
    bump_desc = ("unparseable version(s) (target=%r current=%r) -- "
                 "assume MAJOR (fail closed on uncertainty)" % (target, cur_fw))
else:
    is_major_bump = target_major[0] != cur_major[0]
    bump_desc = ("%s vs current %s -- %s bump"
                 % (target, cur_fw or 'unknown',
                    "MAJOR" if is_major_bump else "minor/patch"))
print("[fw-reqfw-guard] INFO: version bump severity: %s" % bump_desc)

# ---- best-effort manifest resolution for backfill (catalog + sibling repo) -
def resolve_manifest_value(pid):
    """Returns (requires_framework_str_or_None, manifest_path_or_None,
    resolved_ok). resolved_ok=False means UNKNOWN (manifest unreachable);
    resolved_ok=True + value None means CONFIRMED no constraint (SKIP)."""
    if not repo_root_hint:
        return None, None, False
    catalog_path = pathlib.Path(repo_root_hint) / "packs" / "catalog.json"
    if not catalog_path.is_file():
        return None, None, False
    try:
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    except Exception:
        return None, None, False
    entry = next((p for p in catalog.get("packs", []) if p.get("id") == pid), None)
    if entry is None:
        return None, None, False
    package_dir = entry.get("package_dir")
    if not package_dir:
        return None, None, False
    manifest_path = (catalog_path.parent / package_dir / "pack-manifest.json").resolve()
    if not manifest_path.is_file():
        return None, str(manifest_path), False
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception:
        return None, str(manifest_path), False
    if not isinstance(manifest, dict):
        return None, str(manifest_path), False
    val = manifest.get("requires_framework")
    return (val if isinstance(val, str) and val else None), str(manifest_path), True

pass_n = skip_n = warn_n = 0
violations = []
for pid, ver in entries:
    rf_file = packs_dir / ("%s.requires_framework" % pid)
    raw = None
    if rf_file.exists():
        raw = rf_file.read_text(encoding='utf-8').strip()
    else:
        resolved_val, manifest_path, resolved_ok = resolve_manifest_value(pid)
        if resolved_ok and resolved_val:
            raw = resolved_val
            if dry_run:
                print("[fw-reqfw-guard] BACKFILL(dry-run, not persisted): %s@%s -> '%s' "
                      "(resolved via %s)" % (pid, ver, raw, manifest_path))
            else:
                packs_dir.mkdir(parents=True, exist_ok=True)
                rf_file.write_text(raw + "\n", encoding='utf-8')
                print("[fw-reqfw-guard] BACKFILL: %s@%s -> '%s' (resolved via %s, cached to %s)"
                      % (pid, ver, raw, manifest_path, rf_file))
        elif resolved_ok:
            skip_n += 1
            print("[fw-reqfw-guard] SKIP: %s@%s -- manifest resolved (%s) and confirms "
                  "no constraint declared" % (pid, ver, manifest_path))
            continue
        else:
            # UNKNOWN: no cached record, manifest unreachable.
            if is_major_bump:
                violations.append((pid, ver, "UNKNOWN (unresolvable manifest)"))
                print("[fw-reqfw-guard] FAIL: %s@%s -- UNKNOWN: no cached requires_framework "
                      "record and the manifest could not be resolved (checked "
                      "DOGANY_REPO_ROOT=%r); target is a MAJOR bump -- cannot prove "
                      "compatibility, fail closed" % (pid, ver, repo_root_hint))
            else:
                warn_n += 1
                print("[fw-reqfw-guard] WARN: %s@%s -- UNKNOWN: no cached requires_framework "
                      "record and the manifest could not be resolved (checked "
                      "DOGANY_REPO_ROOT=%r); target is a minor/patch bump -- proceeding, "
                      "but this pack's compatibility is NOT verified" % (pid, ver, repo_root_hint))
            continue

    if not raw or not is_semver_range(raw):
        violations.append((pid, ver, "malformed: %r" % raw))
        print("[fw-reqfw-guard] FAIL: %s@%s -- malformed requires_framework record: %r "
              "(malformed is NOT treated as absent -- fail closed)" % (pid, ver, raw))
        continue
    sat = satisfies(target, raw)
    if sat is None:
        violations.append((pid, ver, "unparseable: '%s' vs target '%s'" % (raw, target)))
        print("[fw-reqfw-guard] FAIL: %s@%s -- could not evaluate target version '%s' "
              "against range '%s'" % (pid, ver, target, raw))
    elif sat:
        pass_n += 1
        print("[fw-reqfw-guard] PASS: %s@%s -- target framework %s satisfies "
              "requires_framework '%s'" % (pid, ver, target, raw))
    else:
        violations.append((pid, ver, "requires_framework '%s'" % raw))
        print("[fw-reqfw-guard] FAIL: %s@%s -- target framework %s does NOT satisfy "
              "requires_framework '%s'" % (pid, ver, target, raw))

print("[fw-reqfw-guard] INFO: verdict summary -- PASS=%d SKIP=%d WARN(unknown)=%d FAIL=%d "
      "(of %d evaluated)" % (pass_n, skip_n, warn_n, len(violations), len(entries)))

if violations:
    print("[fw-reqfw-guard] ============================================================")
    print("[fw-reqfw-guard] BLOCKED: %d pack(s) incompatible (or unverifiable on a major "
          "bump) with target framework %s:" % (len(violations), target))
    for pid, ver, reason in violations:
        print("[fw-reqfw-guard]   - %s@%s -- %s" % (pid, ver, reason))
    print("[fw-reqfw-guard] Refusing update -- resolve each pack (upgrade, unmount it, or "
          "make its manifest resolvable) before updating the framework to %s." % target)
    sys.exit(1)

if warn_n:
    print("[fw-reqfw-guard] %d pack(s) evaluated, no confirmed violations, but %d UNKNOWN "
          "(unverified -- proceeding on a minor/patch bump, see WARN lines above) against "
          "target framework %s" % (len(entries), warn_n, target))
else:
    print("[fw-reqfw-guard] ALL %d pack(s) compatible with target framework %s" % (len(entries), target))
sys.exit(0)
PYEOF
}

fw_reqframework_guard "$INSTANCE" "$REPO_VERSION" "$DRY_RUN" \
  || die "framework-compat gate BLOCKED the update (DGN-1031) -- see [fw-reqfw-guard] lines above (resolve/unmount the named pack(s), then re-run)"

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
# 2.6) CONTENT-DRIFT GUARD (DGN-1118; design + signal established by DGN-1109,
#      do not reinvent). The reverse-drift guards in this script compare PINS
#      ONLY (drift_guard_file: EXPECTED_USER_VERSION / MIN_USER_VERSION
#      integers), so a live file that drifted in CONTENT at the same pin --
#      live hotfixes, live-grown features -- passes them and is then silently
#      truncated by the section-3 rsync/cp blocks. DGN-1109 closed exactly
#      this hole for the pack-REINSTALL lane (pack_install.sh); this section
#      closes the framework-UPDATE lane with the SAME judgement.
#
#      SINGLE SOURCE (DGN-1034 shape guard): the scanner lives in
#      scripts/pack/lib/content_drift.py and is shared verbatim with
#      pack_install.sh -- never embed a second copy of the judgement here.
#
#      Signal, per file (DGN-1109): lost = live-only lines (set difference,
#      reorder/blank-insensitive, counted NET OF BASELINE -- see below).
#      lost==0 (identical / pure addition) -> PASS. lost>0 + shrink signature
#      (top-level symbol loss / net line shrink >= 5 / byte halving) ->
#      DESTRUCTIVE -> BLOCK before the first byte is written. lost>0 without
#      a shrink signature -> DRIFT: enumerate by name, then proceed (a gate
#      that blocks every normal update gets bypassed by habit and is
#      equivalent to silence, DGN-1005/1006). Hash mismatch alone never
#      judges.
#
#      UPDATE-LANE DELTA vs pack lane (updates are FREQUENT normal
#      operations, so the false-positive cost is higher here -- a blocked
#      update means the owner stops receiving the framework):
#        * BASELINE: the vendor tree of the tag this instance currently runs
#          (DOGANY_FW_TAG / v$DOGANY_FW_VERSION, extracted read-only via
#          `git archive` from the instance's recorded DOGANY_REPO_ROOT --
#          the release-channel child runs from a throwaway extraction with
#          no .git, fw_reqframework_guard precedent). live == baseline means
#          the live copy is UNTOUCHED vendor content, so even a payload that
#          legitimately SHRINKS the file (vendor refactor) is vendor
#          progression, not user loss -> PASS. Without this, every
#          line-removing release would false-BLOCK every instance. When no
#          baseline can be resolved the guard falls back to the strict
#          DGN-1109 signal (loud note; bypass below still applies).
#          DOGANY_UPDATE_CONTENT_BASELINE=<dir> overrides (tests/operator).
#        * SUBSTITUTION: section 4 re-substitutes mint placeholders into the
#          instance copies, so EVERY live file differs from its raw template
#          source (the documented reason .dogany-preserve avoided checksum
#          comparison). The scanner applies the same substitutions to the
#          payload/baseline side before comparing (subst_one parity:
#          __PROJECT_ROOT__/__HOME__ always, identity tokens under
#          IDENTITY_OK=1), so a normal substituted instance compares clean.
#
#      SCANNED SURFACES mirror the section-3 copy steps 1:1, BLIND channels
#      only -- surfaces whose copy step is a plain rsync/cp with no content
#      awareness: mirror/ (minus the sdk_bridge pin-guard lockstep and the
#      .pack-owned sentinel), routines/, memory-engine/ (*.py/*.md),
#      config/ (i18n; conf scaffolds are write-if-absent -> never
#      overwritten), service/, database/ (3f per-file list, minus the
#      lifekit.py pin/forward-pin lockstep), worklog/_TEMPLATE.md,
#      .claude/skills-bundle/, .claude/agents/, git-hooks/,
#      scripts/secret-sweep.sh + scripts/sweep-identity.example.
#      DELIBERATELY NOT SCANNED (each already content-aware and loud):
#      bridge/ (DGN-593 3-way reconcile; preserve-wins, conflicts never
#      auto-land), skills/dogany-* + RULES.md + CONSTITUTION.md/CONTRACT.md/
#      AGENTS.md + telegram.md/bridge.md + playbooks/AGENT-OPS.md (edit-detect +
#      dated backup channels), .claude/settings.json (substituted rebuild +
#      model restore). .dogany-preserve entries are excluded in lockstep
#      (they will not be copied, so they must not be judged).
#
#      PLACEMENT vs the self-update path (measured, not assumed): the
#      default release channel NEVER copies with the old code -- section 1's
#      parent extracts the target tag and re-invokes THAT tree's update.sh
#      (--no-pull child), which is the process that reaches section 3. So
#      this guard always executes from the PAYLOAD's own tree ("after
#      self-replacement" by construction), activates fleet-wide with the
#      first tag that ships it, and runs BEFORE the first section-3 write
#      and BEFORE the 2.5 preflight prompt (the operator sees the verdict
#      before consenting; the only earlier instance write is the
#      fw_reqframework_guard requires_framework cache backfill -- repo-
#      derived metadata, never live-authored content).
#
#      FAIL: BLOCK (die, nothing written -- dry-run included: an audit run
#      surfaces the same verdict). BYPASS: DOGANY_UPDATE_ACCEPT_CONTENT_LOSS
#      must equal the EXACT target version being landed ($REPO_VERSION); the
#      full enumeration is always printed first as the receipt. Scanner
#      failure -> fail-closed (DGN-1004): cannot prove the copy is safe.
# ---------------------------------------------------------------------------
content_drift_guard() {
  local _cd_lib="${DOGANY_CONTENT_DRIFT_LIB:-$REPO_ROOT/scripts/pack/lib/content_drift.py}"
  [ -f "$_cd_lib" ] \
    || die "CONTENT-DRIFT GUARD (DGN-1118): scanner lib missing ($_cd_lib) -- torn tree, cannot prove the update is safe (fail-closed, DGN-1004)"
  command -v python3 >/dev/null 2>&1 \
    || die "CONTENT-DRIFT GUARD (DGN-1118): python3 unavailable -- cannot prove the update is safe (fail-closed, DGN-1004)"

  # -- baseline: vendor tree of the instance's currently-landed tag ---------
  local _cd_base="" _cd_base_tmp="" _bl_repo="${DOGANY_REPO_ROOT:-}" _bl_tag="${DOGANY_FW_TAG:-}"
  [ -z "$_bl_tag" ] && [ -n "${DOGANY_FW_VERSION:-}" ] && _bl_tag="v${DOGANY_FW_VERSION}"
  if [ -n "${DOGANY_UPDATE_CONTENT_BASELINE:-}" ] && [ -d "${DOGANY_UPDATE_CONTENT_BASELINE}" ]; then
    _cd_base="$DOGANY_UPDATE_CONTENT_BASELINE"
    msg "[update] 콘텐츠-드리프트 가드: 베이스라인 오버라이드 사용 ($_cd_base)" \
        "[update] content-drift guard: using baseline override ($_cd_base)"
  elif [ -n "$_bl_repo" ] && [ -e "$_bl_repo/.git" ] && [ -n "$_bl_tag" ] \
       && git -C "$_bl_repo" rev-parse -q --verify "refs/tags/${_bl_tag}^{commit}" >/dev/null 2>&1; then
    _cd_base_tmp="$(mktemp -d "${TMPDIR:-/tmp}/dogany-cd-base.XXXXXX")" || _cd_base_tmp=""
    if [ -n "$_cd_base_tmp" ]; then
      if git -C "$_bl_repo" archive --format=tar "$_bl_tag" 2>/dev/null \
           | tar -xf - -C "$_cd_base_tmp" 2>/dev/null; then
        _cd_base="$_cd_base_tmp"
        msg "[update] 콘텐츠-드리프트 가드: 베이스라인 = ${_bl_tag} (현재 랜딩된 태그, 읽기 전용 추출)" \
            "[update] content-drift guard: baseline = ${_bl_tag} (currently-landed tag, read-only extraction)"
      else
        rm -rf "$_cd_base_tmp"; _cd_base_tmp=""
      fi
    fi
  fi
  if [ -z "$_cd_base" ]; then
    msg "[update][주의] 콘텐츠-드리프트 가드: 베이스라인 해석 불가 (repo=${_bl_repo:-없음}, tag=${_bl_tag:-없음}) -- 엄격 DGN-1109 신호로 판정합니다 (벤더 축소 리팩터가 차단될 수 있음)" \
        "[update][NOTE] content-drift guard: no baseline resolvable (repo=${_bl_repo:-none}, tag=${_bl_tag:-none}) -- judging with the strict DGN-1109 signal (a legitimate vendor shrink may block)"
  fi

  # -- spec + flag assembly (copy-step lockstep) ----------------------------
  local _tab; _tab="$(printf '\t')"
  local CD_SPECS=() CD_FLAGS=()
  # COMMON_EXCLUDES parity (section 3 defines the rsync array later; keep the
  # two lists in lockstep when editing either).
  local _cx
  for _cx in '.git' '__pycache__' '*.pyc' '*.bak.*' '.DS_Store' 'venv' '*.db' '.env' 'runtime' 'logs'; do
    CD_FLAGS+=(--exclude "*:${_cx}")
  done
  # plists.defer is localized by a literal sed (telegram-agent -> agent name,
  # section 4), not by __TOKEN__ substitution -- unscannable without inverse
  # false positives. Renamed *.plist/*.service/*.timer self-skip (the live
  # file no longer exists at the payload's relpath).
  CD_FLAGS+=(--exclude '*:plists.defer')
  # Substitution parity with subst_one (identity arm under the same gate).
  CD_FLAGS+=(--subst "__PROJECT_ROOT__=${INSTANCE}" --subst "__HOME__=${HOME}")
  if [ "$IDENTITY_OK" = "1" ]; then
    CD_FLAGS+=(--subst "__AGENT_NAME__=${AGENT_NAME}" \
               --subst "__AGENT_LABEL__=${AGENT_LABEL}" \
               --subst "__USER_LABEL__=${USER_LABEL}" \
               --subst "__AGENT_PREFIX__=${AGENT_PREFIX}" \
               --subst "__AGENT_LANG__=${AGENT_LANG}")
  fi
  # .dogany-preserve lockstep: preserved paths are never copied -> never judged.
  local _pe
  for _pe in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    CD_FLAGS+=(--preserve "$_pe")
  done

  # _cd_spec SURFACE PAYLOAD LIVE -- append a spec, attaching the baseline
  # twin when the baseline tree carries the same repo-relative path.
  _cd_spec() {
    local _surf="$1" _pay="$2" _live="$3" _basep=""
    if [ -n "$_cd_base" ]; then
      _basep="$_cd_base/${_pay#"$REPO_ROOT"/}"
      [ -e "$_basep" ] || _basep=""
    fi
    if [ -n "$_basep" ]; then
      CD_SPECS+=("${_surf}${_tab}${_pay}${_tab}${_live}${_tab}${_basep}")
    else
      CD_SPECS+=("${_surf}${_tab}${_pay}${_tab}${_live}")
    fi
  }

  # 3e-mirror lockstep: skip when pack-owned; sdk_bridge pin-SKIP -> exclude.
  if [ -d "$REPO_ROOT/mirror" ] && [ ! -f "$INSTANCE/mirror/.pack-owned" ]; then
    local _sb_fw="$REPO_ROOT/mirror/sdk_bridge.py" _sb_in="$INSTANCE/mirror/sdk_bridge.py"
    if [ -f "$_sb_fw" ] && [ -f "$_sb_in" ]; then
      local _sb_fv="" _sb_iv=""
      _sb_fv="$(extract_ver_sdk_bridge_py "$_sb_fw" 2>/dev/null)" || _sb_fv=""
      _sb_iv="$(extract_ver_sdk_bridge_py "$_sb_in" 2>/dev/null)" || _sb_iv=""
      if [[ "$_sb_fv" =~ ^[0-9]+$ ]] && [[ "$_sb_iv" =~ ^[0-9]+$ ]] && [ "$_sb_iv" -gt "$_sb_fv" ]; then
        CD_FLAGS+=(--exclude 'mirror:/sdk_bridge.py')
      fi
    fi
    _cd_spec "mirror" "$REPO_ROOT/mirror" "$INSTANCE/mirror"
  fi
  # 3b/3c/3d/3e lockstep.
  [ -d "$TEMPLATE/routines" ] && _cd_spec "routines" "$TEMPLATE/routines" "$INSTANCE/routines"
  if [ -d "$TEMPLATE/memory-engine" ]; then
    CD_FLAGS+=(--only 'memory-engine:*.py' --only 'memory-engine:*.md')
    _cd_spec "memory-engine" "$TEMPLATE/memory-engine" "$INSTANCE/memory-engine"
  fi
  if [ -d "$TEMPLATE/config" ]; then
    CD_FLAGS+=(--exclude 'config:/agent.conf' --exclude 'config:/lifekit.conf' \
               --exclude 'config:/secret-patterns.conf')
    _cd_spec "config" "$TEMPLATE/config" "$INSTANCE/config"
  fi
  [ -d "$REPO_ROOT/service" ] && _cd_spec "service" "$REPO_ROOT/service" "$INSTANCE/service"
  # 3f lockstep: the exact per-file list, minus the pin-guard skips (a file
  # the pin guard will SKIP is not copied -> not judged).
  local _dbf
  for _dbf in schema.sql lifekit.py lifekit.sh README.md remind_select.py routine_roller.py routine_projection.py relmod.py relmod.sh; do
    [ -f "$REPO_ROOT/database/$_dbf" ] || continue
    if [ "$_dbf" = "lifekit.py" ]; then
      # forward-pin lockstep (--code-only, DGN-656).
      if [ "$CODE_ONLY" = "1" ] && [ -f "$INSTANCE/database/lifekit.db" ] \
         && command -v sqlite3 >/dev/null 2>&1; then
        local _co_db="" _co_fw=""
        _co_db="$(sqlite3 "$INSTANCE/database/lifekit.db" 'PRAGMA user_version;' 2>/dev/null || true)"
        _co_fw="$(extract_ver_lifekit_py "$REPO_ROOT/database/lifekit.py" 2>/dev/null || true)"
        if [[ "$_co_db" =~ ^[0-9]+$ ]] && [[ "$_co_fw" =~ ^[0-9]+$ ]] && [ "$_co_fw" -gt "$_co_db" ]; then
          continue
        fi
      fi
      # reverse-drift pin lockstep (DGN-249).
      if [ -f "$INSTANCE/database/lifekit.py" ]; then
        local _lk_fv="" _lk_iv=""
        _lk_fv="$(extract_ver_lifekit_py "$REPO_ROOT/database/lifekit.py" 2>/dev/null)" || _lk_fv=""
        _lk_iv="$(extract_ver_lifekit_py "$INSTANCE/database/lifekit.py" 2>/dev/null)" || _lk_iv=""
        if [[ "$_lk_fv" =~ ^[0-9]+$ ]] && [[ "$_lk_iv" =~ ^[0-9]+$ ]] && [ "$_lk_iv" -gt "$_lk_fv" ]; then
          continue
        fi
      fi
    fi
    _cd_spec "database/$_dbf" "$REPO_ROOT/database/$_dbf" "$INSTANCE/database/$_dbf"
  done
  # 3h / 3j / 3j2 / 3k1 / 3k5 lockstep.
  [ -f "$TEMPLATE/worklog/_TEMPLATE.md" ] \
    && _cd_spec "worklog/_TEMPLATE.md" "$TEMPLATE/worklog/_TEMPLATE.md" "$INSTANCE/worklog/_TEMPLATE.md"
  [ -d "$TEMPLATE/.claude/skills-bundle" ] \
    && _cd_spec ".claude/skills-bundle" "$TEMPLATE/.claude/skills-bundle" "$INSTANCE/.claude/skills-bundle"
  [ -d "$TEMPLATE/.claude/agents" ] \
    && _cd_spec ".claude/agents" "$TEMPLATE/.claude/agents" "$INSTANCE/.claude/agents"
  [ -d "$REPO_ROOT/git-hooks" ] \
    && _cd_spec "git-hooks" "$REPO_ROOT/git-hooks" "$INSTANCE/git-hooks"
  local _swf
  for _swf in "scripts/secret-sweep.sh" "scripts/sweep-identity.example"; do
    [ -f "$TEMPLATE/$_swf" ] && _cd_spec "$_swf" "$TEMPLATE/$_swf" "$INSTANCE/$_swf"
  done

  # -- scan + verdict -------------------------------------------------------
  local _cd_rc=0 _cd_out="" _cd_l
  _cd_out="$(python3 "$_cd_lib" ${CD_FLAGS[@]+"${CD_FLAGS[@]}"} ${CD_SPECS[@]+"${CD_SPECS[@]}"})" || _cd_rc=$?
  [ -n "$_cd_base_tmp" ] && rm -rf "$_cd_base_tmp"

  if [ "$_cd_rc" -eq 4 ]; then
    printf '%s\n' "============================================================" >&2
    msg "[update][오류] 콘텐츠-드리프트 가드 (DGN-1118): 핀은 통과하지만, 이 업데이트는 라이브 콘텐츠를 지웁니다:" \
        "[update][ERROR] CONTENT-DRIFT GUARD (DGN-1118): pins allow the copy, but this update would ERASE live content:" >&2
    while IFS= read -r _cd_l; do printf '  %s\n' "$_cd_l" >&2; done <<EOF_CD
$_cd_out
EOF_CD
    if [ "${DOGANY_UPDATE_ACCEPT_CONTENT_LOSS:-}" = "$REPO_VERSION" ]; then
      msg "[update] 콘텐츠-드리프트 가드: 소실 승인됨 (DOGANY_UPDATE_ACCEPT_CONTENT_LOSS=${REPO_VERSION}) -- 진행합니다; 위 열거가 영수증입니다" \
          "[update] content-drift guard: loss ACCEPTED by operator (DOGANY_UPDATE_ACCEPT_CONTENT_LOSS=${REPO_VERSION}) -- proceeding; the enumeration above is the receipt" >&2
      printf '%s\n' "============================================================" >&2
    else
      msg "  조치: 라이브 전용 콘텐츠를 canonical에 승격(백포트)한 뒤 다시 업데이트하거나," \
          "  action: back-port the live-only content into canonical and re-update, or" >&2
      msg "  열거된 소실을 승인하려면 DOGANY_UPDATE_ACCEPT_CONTENT_LOSS=${REPO_VERSION} 로 재실행하세요 (정확한 대상 버전 일치 필요)." \
          "  re-run with DOGANY_UPDATE_ACCEPT_CONTENT_LOSS=${REPO_VERSION} to accept the enumerated loss (exact target version required)." >&2
      printf '%s\n' "============================================================" >&2
      die "CONTENT-DRIFT GUARD BLOCK (DGN-1118): destructive overwrite detected -- nothing was written"
    fi
  elif [ "$_cd_rc" -eq 3 ]; then
    msg "[update] 콘텐츠-드리프트 가드 (DGN-1118): 비파괴 드리프트 -- 아래 라이브 전용 줄들이 덮어쓰기됩니다:" \
        "[update] content-drift guard (DGN-1118): non-destructive drift -- these live-only lines WILL be overwritten:"
    while IFS= read -r _cd_l; do printf '  %s\n' "$_cd_l"; done <<EOF_CD
$_cd_out
EOF_CD
    msg "[update] 콘텐츠-드리프트 가드: 진행 (축소 시그니처 없음: 심볼 소실/순 축소>=5줄/바이트 반토막 아님)" \
        "[update] content-drift guard: PROCEED (no shrink signature: no symbol loss, net shrink < 5 lines, no byte halving)"
  elif [ "$_cd_rc" -eq 0 ]; then
    msg "[update] 콘텐츠-드리프트 가드 (DGN-1118): PASS -- 덮어쓰기로 소실되는 라이브 전용 콘텐츠 없음" \
        "[update] content-drift guard (DGN-1118): PASS -- no live-only content would be overwritten"
  else
    die "CONTENT-DRIFT GUARD (DGN-1118): scanner failed (rc=$_cd_rc) -- cannot prove the update is safe, fail-closed (DGN-1004)"
  fi
  return 0
}
content_drift_guard

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
      # -bail (DGN-1032): same partial-commit-laundering defect as
      # pack_install.sh kit-migrate -- without it a mid-file failure still
      # reaches the trailing PRAGMA user_version=<target>/COMMIT and stamps
      # the target version over a half-applied schema, which a re-run then
      # reads as already in sync. -bail aborts before that stamp is reached.
      sqlite3 -bail "$DB" < "$mig" || die "migration $nnn failed to apply ($base); DB backup at $bak"
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

# ---------------------------------------------------------------------------
# STEP M) 2.0 identity + loader migration (DGN-773 R6), and the framework
#     root-doc refresh channel it shares with section 3k6 (DGN-773 R-7).
#
#     Placement is a CONTRACT, not a convenience: after 3f-migrate, before 3g.
#     3g rewrites .claude/settings.json, whose SessionStart hook points at the
#     identity file. The rename (step 2 below) must land FIRST so that whichever
#     side of a crash we end up on, the hook resolves:
#       * old settings.json (literal <root>/AGENT.md) + compat symlink -> resolves
#       * new settings.json (<root> as a dual-accept dir) + PROFILE.md -> resolves
#     Reversed, a run that dies between the two leaves new settings + no
#     PROFILE.md = onboarding-check reports "file missing" = a LIVE agent gets
#     re-onboarded (DGN-773 R4 gap-3).
#
#     STATE GATE (idempotence): the migration is driven purely by what is on
#     disk, never by a version stamp, so every step is re-entrant and a crash at
#     any point is repaired by simply re-running update.
#       PROFILE.md + AGENT.md symlink   -> already migrated (steps 1-2 skipped)
#       PROFILE.md + AGENT.md absent    -> already migrated, compat symlink
#                                          recreated (this is the mv/ln crash
#                                          window; recreating it is what closes
#                                          the window, see step 2)
#       real AGENT.md, no PROFILE.md    -> MIGRATE
#       real AGENT.md + PROFILE.md      -> die (true collision; refuse to guess
#                                          which one is the live identity)
#       neither                         -> die (not a minted instance)
#
#     Steps 3-5 (rules docs, AGENTS.md hub, CLAUDE.md switch) run for BOTH the
#     migrating and the already-migrated instance, each individually guarded:
#     otherwise a run that died after step 2 could never finish the loader
#     rewiring, because the gate would report "already migrated" forever.
#
# >>> DGN-773-T5 STEP-M BEGIN
#     (everything between these markers is extracted and exercised verbatim by
#      tests/dgn773_t5_migrate_step_m_selftest.sh -- keep it self-contained:
#      the only externals are INSTANCE / TEMPLATE / DRY_RUN / UPDATED /
#      FRAMEWORK_MANIFEST and msg() / die() / file_checksum() /
#      framework_manifest_sha().)

# Framework-owned root docs installed so far THIS RUN. Step M installs the
# 2.0 docs as part of the migration; section 3k6 is the standing channel for
# the same three files. Both call the helper below; this list keeps the second
# call a no-op instead of a redundant copy + duplicate summary line.
_FW_ROOT_DOCS_DONE=""

# install_framework_root_doc <relpath> <ko-label> <en-label>
#
# The DGN-130 section-3k contract, generalized: framework-owned, carries no
# mint placeholders, copied verbatim (cp -pL, so a template symlink into
# rules/ is dereferenced), user edits detected against the framework manifest
# and preserved as <relpath>.user-<ts> next to the file before the refresh.
#
# <relpath> may name a SUBDIRECTORY file (DGN-1141 stage 6: rules/,
# vendors/, playbooks/). The manifest key is the relpath, so "AGENTS.md" and
# "rules/hot.framework.md" never collide.
#
# NOT for an instance-owned file. rules/hot.custom.md and identity/hot.custom.*
# belong to the instance (DGN-1141 section 3.6, the *.custom.* convention);
# the guard below turns a future "just add a channel for it" edit into a hard
# die at the moment of the write instead of silent identity loss.
install_framework_root_doc() {
  local name="$1" ko="$2" en="$3"
  if is_instance_owned "$name"; then
    die "IDENTITY GUARD violated: framework refresh channel targets instance-owned '$name' (DGN-1141 *.custom.* convention)"
  fi
  case " $_FW_ROOT_DOCS_DONE " in
    *" $name "*) return 0 ;;
  esac
  local src="$TEMPLATE/$name"
  [ -e "$src" ] || return 0
  local dest="$INSTANCE/$name"
  case "$name" in
    */*) mkdir -p "$INSTANCE/${name%/*}" || die "failed to create $INSTANCE/${name%/*}" ;;
  esac

  # A symlink at the destination is not ours to rewrite: cp would write THROUGH
  # it and mutate whatever it points at (the pre-2.0 metal/skull RULES.md link
  # chain is the live example -- DGN-773 R7). Warn and skip; never clobber.
  if [ -L "$dest" ]; then
    msg "  [update][경고] $name 이(가) 심링크입니다 -- 리프레시 건너뜀 (수동 해소 필요): $dest" \
        "  [update][WARN] $name is a symlink -- refresh skipped (resolve by hand): $dest"
    return 0
  fi
  if [ -e "$dest" ] && [ ! -f "$dest" ]; then
    die "framework doc $name at $dest is not a regular file -- resolve by hand"
  fi

  local recorded cur incoming user_modified=0
  recorded="$(framework_manifest_sha "$name")"
  cur="$(file_checksum "$dest")"
  incoming="$(file_checksum "$src")"
  if [ -f "$dest" ]; then
    if [ -n "$recorded" ]; then
      [ "$cur" != "$recorded" ] && user_modified=1
    else
      # No manifest entry (first install, or an instance that predates this
      # channel): modified only if the on-disk copy actually differs.
      [ "$cur" != "$incoming" ] && user_modified=1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    if [ "$user_modified" = "1" ]; then
      msg "  [dry-run] 사용자 수정 $name 백업 예정" \
          "  [dry-run] would back up user-modified $name"
    fi
    if [ "$cur" != "$incoming" ]; then
      msg "  [dry-run] $name 갱신 예정" "  [dry-run] would refresh $name"
    else
      msg "  [dry-run] $name 최신 (변경 없음)" "  [dry-run] $name already current"
    fi
    # Do NOT copy, back up, or touch the framework manifest in dry-run.
    return 0
  fi

  if [ "$user_modified" = "1" ]; then
    local ts bak
    ts="$(date +%Y%m%d-%H%M%S)"
    bak="$INSTANCE/$name.user-$ts"
    cp -p "$dest" "$bak" || die "failed to back up user-modified $name"
    msg "  [update][경고] 사용자 수정 $name 발견 -- 백업: $bak" \
        "  [update][WARN] user-modified $name detected -- backed up to: $bak"
  fi
  cp -pL "$src" "$dest" || die "failed to install $name"

  # Upsert this file's line in the framework manifest, leaving every other
  # line intact (mirror of section 3k).
  local installed fw_tmp esc
  installed="$(file_checksum "$dest")"
  mkdir -p "$INSTANCE/.claude"
  esc="$(printf '%s' "$name" | sed 's/\./\\./g')"
  fw_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
  {
    printf '# .dogany-framework.sha -- checksums of framework-owned FILES as installed\n'
    printf '# by dogany-agent (mint.sh / update.sh). Used to detect user edits before a\n'
    printf '# framework refresh overwrites them. Format: "<relpath>  <sha>".\n'
    if [ -f "$FRAMEWORK_MANIFEST" ]; then
      grep -vE "^#|^${esc}[[:space:]]" "$FRAMEWORK_MANIFEST" 2>/dev/null || true
    fi
    printf '%s  %s\n' "$name" "$installed"
  } > "$fw_tmp"
  mv -f "$fw_tmp" "$FRAMEWORK_MANIFEST"

  _FW_ROOT_DOCS_DONE="$_FW_ROOT_DOCS_DONE $name"
  if [ "$DOGANY_LANG" = "ko" ]; then UPDATED+=("$name ($ko)"); else UPDATED+=("$name ($en)"); fi
}

# ---------------------------------------------------------------------------
# STEP N) 2.0 flat root -> 2.0 LAYOUT relayout (DGN-1141 stage 6).
#
#     What moves (and why the names are the machine now):
#       PROFILE.md     -> identity/hot.custom.agent.md
#       USER.md        -> identity/hot.custom.owner.md
#       DISCIPLINE.md  -> rules/hot.custom.md   (only when locally edited)
#       AGENT.md       -> removed (the DGN-773 R7 compat symlink, window closed)
#     DGN-1141 section 3.6 replaced the FRAMEWORK_NEVER_REFRESH array with a
#     naming convention, and the convention only pays off once the files
#     actually carry the token: after this step every instance-owned file
#     matches *.custom.* and every framework-owned file does not.
#
#     PLACEMENT IS A CONTRACT (M9 section 6-1): step N runs BEFORE step 3/4
#     below. Those steps pick which framework docs to install from the on-disk
#     layout and install the AGENTS.md hub; a hub that chains rules/ +
#     identity/ into a not-yet-relaid-out instance is a DEAD loader chain, and
#     a dead @-include is exit 0 with an empty stderr (DGN-1141 M2 measurement)
#     -- the agent boots with no rules at all and nothing says so.
#
#     THE REVERSE WINDOW IS REAL AND IS NOT CLOSED (M11 MAJOR-5). Between the
#     end of this step and the hub swap in step 4, the identity files have
#     MOVED while AGENTS.md still names them at the root: a session that starts
#     in that gap boots with no persona, no owner facts and no discipline. The
#     ordering cannot remove it, only choose its direction -- installing the
#     hub first would produce the dead chain above, which is strictly worse
#     (all rules gone, and it persists until the next run rather than until the
#     next statement). What makes this direction survivable is asserted, not
#     assumed, by t5 case [13b]: the loader hook names the missing files, and a
#     re-run converges. Closing it outright needs an atomic staging area for
#     the whole doc set, which is a bigger change than this stage carries.
#
#     STATE GATE (idempotence): driven purely by what is on disk, never by a
#     version stamp, so every sub-step is re-entrant and a crash at any point
#     is repaired by re-running update.
#       identity/hot.custom.agent.md present  -> relaid out; remaining sub-steps
#                                                are individually guarded no-ops
#                                                (this is also the crash-window
#                                                repair path: a dangling
#                                                AGENT.md link, a USER.md that
#                                                never moved, etc.)
#       real root PROFILE.md, no identity file -> RELAY OUT
#       both real                              -> die (step M's gate, above)
#       neither                                -> no-op (nothing to move)
#
#     FAIL-CLOSED PRECONDITIONS (relayout_preconditions_ok). The move is
#     skipped, loudly, unless the instance can resolve its identity in EVERY
#     crash state without the old names. Two things must already be true:
#       (a) routines/onboarding-check.py publishes the "DGN-1141-relayout-aware"
#           token, i.e. it does 3-way dual-accept. Section 3b refreshes
#           routines/ EARLIER in the same run, so this is normally satisfied by
#           the time we get here -- unless .dogany-preserve pins the old hook.
#       (b) .claude/settings.json wires ONBOARDING_FILE to the instance ROOT,
#           not a literal <root>/AGENT.md. The literal form is honored verbatim
#           (no dual-accept), so moving the file out from under it would make a
#           LIVE agent read as "identity missing" = re-onboarding, in the
#           window between the mv and anything that could repair it.
#     Together these mean NO NEW COMPAT SYMLINK IS EVER CREATED: the old names
#     are retired outright, and an instance that is not ready yet simply keeps
#     what it has for one more run. A pre-2.0 instance therefore takes TWO
#     update runs (run 1: step M + section 3g rewires the hook; run 2: step N)
#     -- deliberately, because the alternative is a re-onboarding window on a
#     live agent. Both skips announce themselves (0-done and not-run must not
#     look alike).
#
#     ROLLBACK: every file is copied into .dogany-migrate-backup/<name>
#     .pre-relayout BEFORE it moves, write-once, and recorded in MANIFEST.txt.
#     Reversing is "move the anchors back, delete identity/ + rules/hot.custom.md".

# The one and only sha the framework ever shipped as agents/.template/
# DISCIPLINE.md (a317ed8c "DGN-773 T2: author 2.0 baseline files", retired by
# 0034381e). An instance copy that still hashes to this never received a local
# edit, so it carries nothing that rules/hot.framework.md has not absorbed
# (DGN-1141 M2: 27/27 sentences are a subset) and is retired instead of being
# promoted into the hot chain a second time. Any other hash is treated as
# owner content and moved to rules/hot.custom.md, never dropped.
DGN1141_DISCIPLINE_SEED_SHA="8a470fb8fcc5585e60a189976a027a67358caa94"

# _relayout_record <label> <detail> -- append one line to the migration
# manifest, creating its header when this is the first migration event.
_relayout_record() {
  local bakdir="$INSTANCE/.dogany-migrate-backup"
  local manifest="$bakdir/MANIFEST.txt"
  mkdir -p "$bakdir" || die "failed to create $bakdir"
  if [ ! -f "$manifest" ]; then
    {
      printf '# .dogany-migrate-backup -- DGN-773 2.0 migration rollback anchor.\n'
      printf '# Restore procedure: the ROLLBACK runbook (upstream source repo), "2.0 IDENTITY/LOADER MIGRATION".\n'
    } > "$manifest" || die "failed to write the migration manifest"
  fi
  printf '%s  %s  %s\n' "$1" "$2" "$(date +%Y%m%d-%H%M%S)" >> "$manifest"
}

# _relayout_anchor <relpath> -- copy an instance file into the rollback anchor
# dir under <relpath>.pre-relayout. WRITE-ONCE (same rule as step M's
# AGENT.md.pre-2.0): an anchor from an earlier, crashed attempt is parked, never
# overwritten -- overwriting it would destroy the only copy of the pre-migration
# bytes at exactly the moment they matter.
_relayout_anchor() {
  local rel="$1"
  local src="$INSTANCE/$rel"
  local bakdir="$INSTANCE/.dogany-migrate-backup"
  local dst="$bakdir/$rel.pre-relayout"
  [ -f "$src" ] || return 0
  [ -L "$src" ] && return 0
  mkdir -p "$bakdir" || die "failed to create $bakdir"
  if [ -f "$dst" ]; then
    if [ "$(file_checksum "$dst")" = "$(file_checksum "$src")" ]; then
      return 0
    fi
    local ots; ots="$(date +%Y%m%d-%H%M%S)"
    mv "$dst" "$dst.$ots" || die "failed to park the previous relayout anchor for $rel"
    msg "  [update][경고] 기존 relayout 앵커 보관: $dst.$ots" \
        "  [update][WARN] parked the previous relayout anchor: $dst.$ots"
  fi
  cp -p "$src" "$dst" || die "failed to anchor $rel before the relayout"
  _relayout_record "$rel.pre-relayout" "$(file_checksum "$dst")"
}

# relayout_preconditions_ok -- fail-CLOSED. 0 = safe to move identity now.
#
# ONE precondition, and it is about the SessionStart hook, not about taste: the
# hook decides whether a live agent gets re-onboarded, and it resolves the
# persona by NAME. The check is a token the hook publishes about itself
# ("DGN-1141-relayout-aware"), never a grep for a path that could also appear
# in a stale docstring. Move the file out from under a hook that only knows
# PROFILE.md / AGENT.md and a fully-configured agent reads as "identity file
# missing" = onboarding restarts on a live instance (the DGN-773 R4 gap-3
# hazard, one layout later). Section 3b refreshes routines/ EARLIER in the same
# run, so this is normally already true by the time we get here -- unless
# .dogany-preserve pins the old hook, which is exactly the case worth deferring.
relayout_preconditions_ok() {
  local hook="$INSTANCE/routines/onboarding-check.py"
  if [ -f "$hook" ] && ! grep -q 'DGN-1141-relayout-aware' "$hook" 2>/dev/null; then
    msg "  [update] 레이아웃 이행 보류: 온보딩 훅이 아직 identity/ 를 모릅니다 (routines 갱신 후 다음 실행에서 이행)" \
        "  [update] relayout deferred: this instance's onboarding hook does not know identity/ yet -- it migrates on the next run (routines refresh lands first)"
    return 1
  fi
  # (2) the hook must be wired to the instance ROOT, not to a literal identity
  #     FILE path. The literal form (the pre-2.0 wiring) is honored verbatim --
  #     no dual-accept at all -- so while it is on disk the persona MUST stay
  #     reachable under its old name, and the mv that moves it opens a window
  #     in which the old name resolves to nothing. Section 3g rewrites this
  #     wiring to the root dir LATER IN THIS SAME RUN, so a pre-2.0 instance
  #     simply relays out on its NEXT update: one extra run buys a migration
  #     with no re-onboarding window at all.
  local settings="$INSTANCE/.claude/settings.json"
  if [ -f "$settings" ] \
     && grep -qE 'ONBOARDING_FILE=[^"[:space:]]*/(AGENT|PROFILE)\.md' "$settings" 2>/dev/null; then
    msg "  [update] 레이아웃 이행 보류: settings.json 이 아직 정체성 파일 경로를 직접 가리킵니다 (3g 가 이번 실행에서 재배선하며, 다음 실행에서 이행)" \
        "  [update] relayout deferred: settings.json still wires ONBOARDING_FILE to a literal identity file -- section 3g rewires it later in this run, so the relayout lands on the next one"
    return 1
  fi
  return 0
}

# migrate_relayout -- STEP N, driven by the on-disk state gate.
migrate_relayout() {
  local profile="$INSTANCE/PROFILE.md"
  local userfile="$INSTANCE/USER.md"
  local agentmd="$INSTANCE/AGENT.md"
  local discipline="$INSTANCE/DISCIPLINE.md"
  local id_agent="$INSTANCE/identity/hot.custom.agent.md"
  local id_owner="$INSTANCE/identity/hot.custom.owner.md"

  # The consumed release must actually SHIP the 2.0 layout, or there is nothing
  # to move to. Fail-safe: never restructure toward a shape this release does
  # not carry (mirror of the step-M version gate's intent, expressed as a
  # template-content check so it also holds for channel=main checkouts).
  [ -f "$TEMPLATE/identity/hot.custom.agent.md" ] || return 0
  [ -f "$TEMPLATE/rules/hot.framework.md" ] || return 0

  local relaid=0
  [ -f "$id_agent" ] && relaid=1

  if [ "$relaid" = "0" ]; then
    # Nothing to move (step M already refused a tree with no persona at all).
    [ -f "$profile" ] || return 0
    relayout_preconditions_ok || return 0
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] 2.0 레이아웃 이행 예정: PROFILE.md/USER.md -> identity/, DISCIPLINE.md -> rules/hot.custom.md, AGENT.md 호환 심링크 폐지" \
          "  [dry-run] would relayout to the 2.0 layout: PROFILE.md/USER.md -> identity/, DISCIPLINE.md -> rules/hot.custom.md, retire the AGENT.md compat symlink"
      return 0
    fi
  fi
  [ "$DRY_RUN" = "1" ] && return 0

  # ---- N1: rollback anchors FIRST, then the directory skeleton ---------
  _relayout_anchor "PROFILE.md"
  _relayout_anchor "USER.md"
  _relayout_anchor "DISCIPLINE.md"
  # AGENTS.md is anchored on the SAME write-once rule as the identity files.
  # It is not moved by this step -- step 4 REPLACES it, and
  # install_framework_root_doc() only backs up a copy that differs from the
  # framework manifest, so a hub that matched the framework record disappeared
  # with no copy anywhere. Reversing the relayout then had nothing to restore
  # and left the loader chain pointing at three files the reversal deleted.
  # Anchored only while the hub is still the PRE-relayout one: an already
  # relaid-out instance re-entering this step must not overwrite (or, on a
  # tree that never had an anchor, manufacture) a "pre-relayout" record that
  # actually holds the post-relayout hub.
  # README.md rides the same rule for the same reason: stage 8 put it on the
  # framework refresh channel (it is the one owner-facing doc and it was the
  # only 2.0 doc with no channel at all), which means the relayout OVERWRITES
  # the owner's pre-migration copy -- and install_framework_root_doc() keeps a
  # backup only of a copy that differs from the framework record.
  if [ "$relaid" = "0" ]; then
    _relayout_anchor "AGENTS.md"
    _relayout_anchor "README.md"
  fi
  # ONLY the directories THIS step writes into. vendors/ used to be created
  # here and its contract file lands ~200 lines later, in the framework-doc
  # channel -- so a crash in between left the exact shape bridge/sdk_bridge.py
  # reads as fail-CLOSED (vendors/ present + declared contract absent =
  # VendorContractMissing at import = the bridge does not boot). A migration
  # crash must never take the channel down: with no vendors/ dir the bridge
  # fails OPEN (no vendor injected) and the owner can still say "run it again".
  # install_framework_root_doc() mkdir -p's its own parent, so pre-creating
  # vendors/ and playbooks/ bought nothing.
  mkdir -p "$INSTANCE/identity" "$INSTANCE/rules" \
    || die "failed to create the 2.0 layout directories"

  # ---- N2: persona (same-FS mv, atomic) --------------------------------
  if [ -f "$profile" ] && [ ! -L "$profile" ] && [ ! -f "$id_agent" ]; then
    mv "$profile" "$id_agent" || die "failed to move PROFILE.md -> identity/hot.custom.agent.md"
    _relayout_record "identity/hot.custom.agent.md" "moved-from:PROFILE.md"
    msg "  [update] 2.0 레이아웃: PROFILE.md -> identity/hot.custom.agent.md" \
        "  [update] 2.0 relayout: PROFILE.md -> identity/hot.custom.agent.md"
    UPDATED+=("identity/hot.custom.agent.md (moved from PROFILE.md)")
  fi

  # ---- N3: retire the compat symlinks (stage 6, section C) --------------
  #   AGENT.md -> PROFILE.md was DGN-773 R7's one-version window; after N2 it
  #   dangles. Removing it here is safe ONLY because the preconditions above
  #   already established that nothing resolves identity by that name any more
  #   (the hook does 3-way dual-accept and is wired to the root dir).
  #   This branch is also the crash-window repair: a re-run after a death
  #   between N2 and here finds exactly this state and finishes the job.
  #   An UNEXPECTED link target is left alone and reported -- never silently
  #   delete something an owner wired by hand.
  if [ -L "$agentmd" ]; then
    local atgt; atgt="$(readlink "$agentmd" 2>/dev/null || true)"
    case "$atgt" in
      PROFILE.md|"$INSTANCE/PROFILE.md"|identity/hot.custom.agent.md|"$INSTANCE/identity/hot.custom.agent.md")
        rm -f "$agentmd" || die "failed to retire the AGENT.md compat symlink"
        _relayout_record "AGENT.md.compat-symlink" "retired->$atgt"
        msg "  [update] 2.0 레이아웃: AGENT.md 호환 심링크 폐지 (정체성은 identity/hot.custom.agent.md)" \
            "  [update] 2.0 relayout: retired the AGENT.md compat symlink (identity lives at identity/hot.custom.agent.md)"
        UPDATED+=("AGENT.md (compat symlink retired)")
        ;;
      *)
        msg "  [update][경고] AGENT.md 심링크가 예상 밖 '$atgt' 를 가리킵니다 -- 그대로 두고 수동 확인 요망" \
            "  [update][WARN] the AGENT.md symlink points at an unexpected '$atgt' -- left in place, resolve by hand"
        ;;
    esac
  fi
  if [ -L "$profile" ]; then
    rm -f "$profile" || die "failed to retire the PROFILE.md compat symlink"
    _relayout_record "PROFILE.md.compat-symlink" "retired"
    msg "  [update] 2.0 레이아웃: PROFILE.md 호환 심링크 폐지" \
        "  [update] 2.0 relayout: retired the PROFILE.md compat symlink"
    UPDATED+=("PROFILE.md (compat symlink retired)")
  fi

  # ---- N4: owner facts --------------------------------------------------
  if [ -f "$userfile" ] && [ ! -L "$userfile" ]; then
    if [ ! -f "$id_owner" ]; then
      mv "$userfile" "$id_owner" || die "failed to move USER.md -> identity/hot.custom.owner.md"
      _relayout_record "identity/hot.custom.owner.md" "moved-from:USER.md"
      msg "  [update] 2.0 레이아웃: USER.md -> identity/hot.custom.owner.md" \
          "  [update] 2.0 relayout: USER.md -> identity/hot.custom.owner.md"
      UPDATED+=("identity/hot.custom.owner.md (moved from USER.md)")
    else
      msg "  [update][경고] USER.md 와 identity/hot.custom.owner.md 가 둘 다 실파일입니다 -- 이동 보류, 수동 확인 요망" \
          "  [update][WARN] both USER.md and identity/hot.custom.owner.md are real files -- move deferred, resolve by hand"
    fi
  elif [ -L "$userfile" ]; then
    rm -f "$userfile" || die "failed to retire the USER.md compat symlink"
    _relayout_record "USER.md.compat-symlink" "retired"
    msg "  [update] 2.0 레이아웃: USER.md 호환 심링크 폐지" \
        "  [update] 2.0 relayout: retired the USER.md compat symlink"
    UPDATED+=("USER.md (compat symlink retired)")
  fi

  # ---- N5: local discipline overlay ------------------------------------
  if [ -f "$discipline" ] && [ ! -L "$discipline" ]; then
    local dsha; dsha="$(file_checksum "$discipline")"
    if [ "$dsha" = "$DGN1141_DISCIPLINE_SEED_SHA" ]; then
      rm -f "$discipline" || die "failed to retire the untouched DISCIPLINE.md seed"
      _relayout_record "DISCIPLINE.md" "untouched-seed-retired:$dsha"
      msg "  [update] 2.0 레이아웃: DISCIPLINE.md 는 미편집 시드 -> 폐지 (내용은 rules/hot.framework.md 에 흡수, 앵커 보관)" \
          "  [update] 2.0 relayout: DISCIPLINE.md was the untouched framework seed -> retired (absorbed into rules/hot.framework.md; anchor kept)"
      UPDATED+=("DISCIPLINE.md (untouched seed retired -- absorbed into rules/hot.framework.md)")
    elif [ ! -f "$INSTANCE/rules/hot.custom.md" ]; then
      mv "$discipline" "$INSTANCE/rules/hot.custom.md" \
        || die "failed to move DISCIPLINE.md -> rules/hot.custom.md"
      _relayout_record "rules/hot.custom.md" "moved-from:DISCIPLINE.md:$dsha"
      msg "  [update] 2.0 레이아웃: 로컬 편집된 DISCIPLINE.md -> rules/hot.custom.md (인스턴스 소유 덮개 -- 절대 갱신 안 함). AGENTS.md 허브가 정본 다음에 이 덮개를 로드합니다 -- 같은 제목의 절은 덮개가 이깁니다. 내용을 rules/hot.framework.md 와 대조해 실제 분기만 남기세요" \
          "  [update] 2.0 relayout: locally-edited DISCIPLINE.md -> rules/hot.custom.md (instance-owned overlay -- never refreshed). The AGENTS.md hub loads it AFTER the canon, so a same-titled section here wins -- review it against rules/hot.framework.md and keep only real divergence"
      UPDATED+=("rules/hot.custom.md (moved from a locally-edited DISCIPLINE.md)")
    else
      msg "  [update][경고] DISCIPLINE.md 와 rules/hot.custom.md 가 둘 다 존재합니다 -- 이동 보류, 수동 확인 요망" \
          "  [update][WARN] both DISCIPLINE.md and rules/hot.custom.md exist -- move deferred, resolve by hand"
    fi
  fi

  # ---- N6: seed the overlay so the hub's last @-target always resolves --
  #   agents/.template/AGENTS.md chains @rules/hot.custom.md unconditionally.
  #   MEASURED (claude 2.1.241, headless fixture, both directions): an
  #   @-reference to an absent file is a SILENT no-op -- exit 0, empty stderr,
  #   sibling references still load -- and the same reference loads the file
  #   the moment it exists. So the unconditional chain line is safe either way,
  #   and the choice of seeding is about OBSERVATION, not about loading:
  #   without a seed, loader-manifest.py would report "MISSING: hot.custom.md"
  #   on every instance that simply has no divergence, and a permanent MISSING
  #   is indistinguishable from a real one. Seeding keeps MISSING meaningful.
  #
  #   This reverses DGN-1141 section 3.4's "never pre-create an empty overlay".
  #   That decision was taken while the overlay was OUTSIDE the loader chain,
  #   where an empty file really was pure noise; inside the chain it costs a
  #   few dozen tokens and buys a working absence signal.
  #
  #   NOT install_framework_root_doc(): that channel dies on any *.custom.*
  #   target by design (identity guard). This is a create-if-absent seed --
  #   once the file exists the framework never writes it again, which is what
  #   makes seeding safe for an owner who has already diverged.
  if [ ! -e "$INSTANCE/rules/hot.custom.md" ] && [ -f "$TEMPLATE/rules/hot.custom.md" ]; then
    mkdir -p "$INSTANCE/rules" || die "failed to create $INSTANCE/rules"
    cp -p "$TEMPLATE/rules/hot.custom.md" "$INSTANCE/rules/hot.custom.md" \
      || die "failed to seed rules/hot.custom.md"
    # Record it only when this tree really is mid-migration. On an instance
    # MINTED into the 2.0 layout (mint seeds the overlay itself) this branch is
    # a no-op; on one minted before the overlay was chained, seeding is a
    # repair, not a migration -- creating a rollback-anchor dir for it would
    # manufacture migration residue on a tree that never migrated.
    # The sha is recorded so a ROLLBACK can tell "this is the seed the framework
    # laid down" (discard it) from "the owner diverged after the seed" (keep
    # it) -- section 8a2 renames the overlay back to DISCIPLINE.md, and
    # resurrecting an empty 2.0 stub into a v1 loader chain is not a restore.
    [ -d "$INSTANCE/.dogany-migrate-backup" ] \
      && _relayout_record "rules/hot.custom.md" \
           "seeded-empty-overlay:$(file_checksum "$INSTANCE/rules/hot.custom.md")"
    msg "  [update] 2.0 레이아웃: 빈 덮개 rules/hot.custom.md 시드 (인스턴스 소유 -- 갱신 대상 아님)" \
        "  [update] 2.0 relayout: seeded the empty overlay rules/hot.custom.md (instance-owned -- never refreshed)"
    UPDATED+=("rules/hot.custom.md (empty overlay seeded)")
  fi
}

# migrate_step_m -- the whole of R6, driven by the on-disk state gate.
migrate_step_m() {
  local profile="$INSTANCE/PROFILE.md"
  local agentmd="$INSTANCE/AGENT.md"
  local bakdir="$INSTANCE/.dogany-migrate-backup"
  local manifest="$bakdir/MANIFEST.txt"

  local profile_present=0 agent_link=0 agent_real=0
  [ -f "$profile" ] && profile_present=1
  if [ -L "$agentmd" ]; then
    agent_link=1
  elif [ -f "$agentmd" ]; then
    agent_real=1
  fi

  # DGN-1141 stage 6: a RELAID-OUT instance carries its persona at
  # identity/hot.custom.agent.md and has no root PROFILE.md/AGENT.md at all.
  # That state must not fall through to the R6 gate below, whose "neither"
  # branch would die with "not a minted instance" on a perfectly healthy 2.0
  # instance. It is checked FIRST and short-circuits steps 1-2.
  local relaid=0
  [ -f "$INSTANCE/identity/hot.custom.agent.md" ] && relaid=1

  # ---- state gate (4 branches, R6; + the relayout branch) --------------
  local do_rename=0
  if [ "$relaid" = "1" ]; then
    # Already relaid out. A REAL root PROFILE.md or AGENT.md next to it is an
    # identity collision for the same reason the R6 gate refuses to guess:
    # two live personas, no way to tell which one the owner is talking to.
    # A DANGLING AGENT.md symlink is not a collision -- it is the step-N
    # crash window, repaired by migrate_relayout() below.
    if [ "$profile_present" = "1" ] && [ ! -L "$profile" ]; then
      die "identity collision: identity/hot.custom.agent.md and a real root PROFILE.md both exist at $INSTANCE.
  This is NOT auto-resolvable -- picking one would silently discard a live identity.
  Resolve by hand: keep the live persona as identity/hot.custom.agent.md, move the
  other aside (e.g. PROFILE.md.conflict-<date>), then re-run update."
    fi
    if [ "$agent_real" = "1" ]; then
      die "identity collision: identity/hot.custom.agent.md and a real (non-symlink) AGENT.md both exist at $INSTANCE.
  Resolve by hand (see .dogany-migrate-backup/), then re-run update."
    fi
  elif [ "$profile_present" = "1" ] && [ "$agent_real" = "1" ]; then
    die "identity collision: both PROFILE.md and a real (non-symlink) AGENT.md exist at $INSTANCE.
  This is NOT auto-resolvable -- picking one would silently discard a live identity.
  Resolve by hand: keep the live persona as PROFILE.md, move the other aside
  (e.g. AGENT.md.conflict-<date>), then re-run update."
  elif [ "$profile_present" = "1" ]; then
    # Already migrated. agent_link=1 is the steady 2.0 state; agent absent is
    # the mv/ln crash window (step 2) -- recreate the compat symlink, because a
    # pre-2.0 settings.json still points at the literal <root>/AGENT.md path and
    # a missing file there reads as "needs onboarding" on a LIVE agent.
    if [ "$agent_link" = "1" ]; then
      local tgt; tgt="$(readlink "$agentmd" 2>/dev/null || true)"
      if [ "$tgt" != "PROFILE.md" ]; then
        msg "  [update][경고] AGENT.md 호환 심링크가 PROFILE.md 가 아닌 '$tgt' 를 가리킵니다 -- 수동 확인 필요" \
            "  [update][WARN] AGENT.md compat symlink points at '$tgt', not PROFILE.md -- check by hand"
      fi
    elif [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] AGENT.md 호환 심링크 재생성 예정 (마이그레이션 크래시 윈도 복구)" \
          "  [dry-run] would recreate the AGENT.md compat symlink (migration crash-window repair)"
    else
      ln -s "PROFILE.md" "$agentmd" || die "failed to recreate the AGENT.md compat symlink"
      msg "  [update] AGENT.md 호환 심링크 재생성 (크래시 윈도 복구)" \
          "  [update] recreated the AGENT.md compat symlink (crash-window repair)"
      UPDATED+=("AGENT.md (compat symlink, repaired)")
    fi
  elif [ "$agent_real" = "1" ]; then
    do_rename=1
  elif [ "$agent_link" = "1" ]; then
    die "broken identity state at $INSTANCE: AGENT.md is a symlink but PROFILE.md does not exist.
  Restore PROFILE.md (see .dogany-migrate-backup/AGENT.md.pre-2.0 if present) and re-run update."
  else
    die "no identity file at $INSTANCE: neither PROFILE.md nor AGENT.md exists -- not a minted instance."
  fi

  # ---- steps 1-2: backup, rename, compat symlink ----------------------
  if [ "$do_rename" = "1" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] 2.0 마이그레이션 예정: AGENT.md 백업 -> PROFILE.md 개명 -> AGENT.md 호환 심링크" \
          "  [dry-run] would migrate to 2.0: back up AGENT.md -> rename to PROFILE.md -> AGENT.md compat symlink"
    else
      # 1. Backup FIRST -- this is the rollback anchor (ROLLBACK runbook,
      #    "2.0 IDENTITY/LOADER MIGRATION"; upstream source repo only, DGN-1097).
      mkdir -p "$bakdir" || die "failed to create $bakdir"
      local anchor="$bakdir/AGENT.md.pre-2.0"
      if [ -f "$anchor" ] && [ "$(file_checksum "$anchor")" != "$(file_checksum "$agentmd")" ]; then
        # An older anchor from a previous (crashed) attempt differs. Never
        # overwrite a rollback anchor: park it and keep going.
        local ots; ots="$(date +%Y%m%d-%H%M%S)"
        mv "$anchor" "$anchor.$ots" || die "failed to park the previous migration anchor"
        msg "  [update][경고] 기존 마이그레이션 앵커 보관: $anchor.$ots" \
            "  [update][WARN] parked the previous migration anchor: $anchor.$ots"
      fi
      cp -p "$agentmd" "$anchor" || die "failed to back up AGENT.md before the 2.0 rename"
      {
        printf '# .dogany-migrate-backup -- DGN-773 2.0 migration rollback anchor.\n'
        printf '# Restore procedure: the ROLLBACK runbook (upstream source repo), "2.0 IDENTITY/LOADER MIGRATION".\n'
      } > "$manifest.tmp" 2>/dev/null || true
      [ -f "$manifest" ] && grep -v '^#' "$manifest" >> "$manifest.tmp" 2>/dev/null
      printf 'AGENT.md.pre-2.0  %s  %s\n' "$(file_checksum "$anchor")" "$(date +%Y%m%d-%H%M%S)" >> "$manifest.tmp"
      mv -f "$manifest.tmp" "$manifest" || die "failed to write the migration manifest"

      # 2. Rename (same-FS, atomic) then lay the compat symlink. A crash
      #    between the two is repaired by the state gate above on re-run.
      mv "$agentmd" "$profile" || die "failed to rename AGENT.md -> PROFILE.md"
      ln -s "PROFILE.md" "$agentmd" || die "failed to create the AGENT.md compat symlink"
      msg "  [update] 2.0 마이그레이션: AGENT.md -> PROFILE.md 개명 + 호환 심링크 (백업: $anchor)" \
          "  [update] 2.0 migration: renamed AGENT.md -> PROFILE.md + compat symlink (backup: $anchor)"
      UPDATED+=("PROFILE.md (renamed from AGENT.md; AGENT.md kept as compat symlink)")
    fi
  fi

  # From here on the instance is (or, in dry-run, would be) in 2.0 shape.
  # Everything below is individually guarded and safe to re-run.
  [ "$DRY_RUN" = "1" ] && [ "$do_rename" = "1" ] && return 0

  # ---- step N: 2.0 -> layout relayout (DGN-1141 stage 6) ---------------
  #   Runs BEFORE the doc/hub steps below on purpose: those steps decide WHICH
  #   set of framework docs to install from the on-disk layout, and step 4
  #   installs the AGENTS.md hub. A hub that chains rules/ + identity/ into an
  #   instance that has not been relaid out yet is a dead loader chain
  #   (@missing is exit 0 + zero stderr -- DGN-1141 M2), so the move must land
  #   first. This ordering IS the acceptance criterion M9 section 6-1 left.
  migrate_relayout

  # ---- step 3: framework rule docs ------------------------------------
  #   Which set depends on the layout the instance is actually in:
  #     relaid out  -> rules/ + vendors/ + playbooks/ (DGN-1141 2.0 layout)
  #     not yet     -> CONSTITUTION.md / CONTRACT.md at the root (DGN-773 R-7)
  #   Never both: the second set is what the first one absorbed (M9 section 1c),
  #   and shipping both would double-inject the same rules.
  #   DISCIPLINE.md is NOT seeded any more -- the file is retired (its 27
  #   sentences were measured to be a strict subset of the merged framework
  #   rules, DGN-1141 M2), and its instance-owned successor rules/hot.custom.md
  #   is created ONLY by the relayout when the instance really has local
  #   content to keep (DGN-1141 section 3.4: never pre-create an empty overlay).
  if [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
    install_framework_root_doc "rules/hot.framework.md"     "프레임워크 규칙(hot)"  "framework rules (hot)"
    install_framework_root_doc "rules/cold.framework.why.md" "프레임워크 근거(cold)" "framework rationale (cold)"
    install_framework_root_doc "vendors/telegram.md"        "벤더 계약: telegram"   "vendor contract: telegram"
    install_framework_root_doc "playbooks/layout.md"        "레이아웃 플레이북"     "layout playbook"
    install_framework_root_doc "README.md"                  "오너용 레이아웃 안내"  "owner-facing layout guide"
  else
    install_framework_root_doc "CONSTITUTION.md" "프레임워크 헌법" "framework constitution"
    install_framework_root_doc "CONTRACT.md"     "프레임워크 계약" "framework contract"
  fi
  # NOTE (R6 step 7): a pre-2.0 instance keeps its real RULES.md v1 file. It is
  # byte-frozen (R7) so section 3k keeps refreshing it to identical content, and
  # it is simply dropped from the loader chain below -- leaving it on disk is
  # what makes the rollback in the ROLLBACK runbook a one-liner. That argument
  # is about instances that HAVE one: since DGN-1141 stage 8 neither mint nor
  # section 3k creates the file, so an instance minted into the 2.0 layout
  # never carries a second, disagreeing constitution (M11 MAJOR-2).

  # ---- step 4: AGENTS.md loader hub -----------------------------------
  #   The hub deliberately does NOT chain RULES.md: the v1 file and the new
  #   three-file split say the same things, so chaining both double-injects.
  #
  #   GATED ON THE LAYOUT (DGN-1141 stage 6): the shipped hub chains rules/ +
  #   identity/. If step N deferred (relayout_preconditions_ok said not yet),
  #   installing it here would hand the instance a hub whose every target is
  #   missing -- and a missing @-include is silent. Skip; the instance keeps a
  #   working chain and gets the hub on the run that completes the relayout.
  if [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
    install_framework_root_doc "AGENTS.md" "로더 허브" "loader hub"
  fi

  # ---- step 5: CLAUDE.md -> @AGENTS.md --------------------------------
  #   Guard is "does the chain already go through AGENTS.md", not "is this file
  #   byte-identical to the template": an instance that added its own lines to
  #   CLAUDE.md is left alone once the hub line is in place.
  #   The `-f AGENTS.md` term is the companion to step 4's layout gate: never
  #   point CLAUDE.md at a hub the instance does not have (DGN-1141 stage 6).
  local claude="$INSTANCE/CLAUDE.md"
  if [ -e "$TEMPLATE/CLAUDE.md" ] && [ -f "$INSTANCE/AGENTS.md" ] \
     && ! grep -qx '@AGENTS\.md' "$claude" 2>/dev/null; then
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] CLAUDE.md 로더 체인을 @AGENTS.md 로 교체 예정" \
          "  [dry-run] would switch the CLAUDE.md loader chain to @AGENTS.md"
    else
      mkdir -p "$bakdir" || die "failed to create $bakdir"
      if [ -f "$claude" ] && [ ! -f "$bakdir/CLAUDE.md.pre-2.0" ]; then
        cp -p "$claude" "$bakdir/CLAUDE.md.pre-2.0" || die "failed to back up CLAUDE.md"
        printf 'CLAUDE.md.pre-2.0  %s  %s\n' \
          "$(file_checksum "$bakdir/CLAUDE.md.pre-2.0")" "$(date +%Y%m%d-%H%M%S)" >> "$manifest"
      fi
      # Atomic: dest-adjacent mktemp + mv (settings.json precedent, section 3g).
      local claude_tmp
      claude_tmp="$(mktemp "${claude}.new.XXXXXX")" || die "failed to stage CLAUDE.md"
      cp -pL "$TEMPLATE/CLAUDE.md" "$claude_tmp" || die "failed to stage CLAUDE.md"
      mv -f "$claude_tmp" "$claude" || die "failed to install CLAUDE.md"
      msg "  [update] CLAUDE.md 로더 체인 -> @AGENTS.md (백업: $bakdir/CLAUDE.md.pre-2.0)" \
          "  [update] CLAUDE.md loader chain -> @AGENTS.md (backup: $bakdir/CLAUDE.md.pre-2.0)"
      UPDATED+=("CLAUDE.md (loader chain -> @AGENTS.md)")
    fi
  fi

  # ---- step 6: settings.json ------------------------------------------
  #   The rewrite itself is section 3g's job, unchanged (update.sh 3g). All this
  #   step owes it is a pre-2.0 copy to roll back to -- taken here because 3g
  #   runs immediately after and keeps no backup of its own.
  if [ "$DRY_RUN" != "1" ] && [ -d "$bakdir" ] \
     && [ -f "$INSTANCE/.claude/settings.json" ] && [ ! -f "$bakdir/settings.json.pre-2.0" ]; then
    cp -p "$INSTANCE/.claude/settings.json" "$bakdir/settings.json.pre-2.0" \
      || die "failed to back up .claude/settings.json"
    printf 'settings.json.pre-2.0  %s  %s\n' \
      "$(file_checksum "$bakdir/settings.json.pre-2.0")" "$(date +%Y%m%d-%H%M%S)" >> "$manifest"
  fi
}
# <<< DGN-773-T5 STEP-M END

# >>> DGN-1082 STEP-M-GATE BEGIN
#     (extracted verbatim by tests/dgn1082_step_m_version_gate_selftest.sh --
#      keep it self-contained: the only externals are REPO_VERSION /
#      DOGANY_ROLLBACK and _dgn907_ver_le / msg / migrate_step_m. NOTE:
#      sourcing this region EXECUTES the dispatch at the bottom.)
#
# Version gate (DGN-1082): step M is the 2.0 identity/loader migration. It may
# only run when the release being CONSUMED actually ships 2.0 -- otherwise a
# 1.x cut from a trunk that already carries the 2.0 restructuring would migrate
# every pre-2.0 instance on a minor update. REPO_VERSION is the right anchor:
# on the release channel the extracted tag tree's own update.sh runs (so
# REPO_VERSION == the consumed tag's VERSION), and channel=main reads the
# checkout's VERSION -- both are "what this run installs", matching
# resolve_channel_tag()'s result by construction.
#
# Pre-releases PASS: _dgn907_ver_le coerces each dotted field numerically, so
# "2.0.0-dev.1" reads as 2.0.0.1 >= 2.0.0. Deliberate -- dev/rc cuts of 2.0
# are exactly how the migration gets exercised before the stable cut.
#
# FAIL-CLOSED: an unreadable/garbage version skips the migration (a broken
# source tree must not restructure a live instance). Never silent -- both skip
# paths announce themselves (estate rule: 0-done and not-run must not look
# alike).
STEP_M_MIN_FW_VERSION="2.0.0"

# step_m_version_gate <repo_version> -> 0 when step M may run, 1 to skip.
step_m_version_gate() {
  local v="${1:-}"
  case "$v" in
    ""|unknown) return 1 ;;
  esac
  _dgn907_ver_le "$STEP_M_MIN_FW_VERSION" "$v"
}

# ROLLBACK MODE (DGN-673 B3): a deliberate downgrade must not re-migrate the
# tree it is downgrading. The 2.0 reversal is an operator procedure, not an
# automatic one -- the ROLLBACK runbook, "2.0 IDENTITY/LOADER MIGRATION"
# (upstream source repo only, DGN-1097 -- never name its repo path here).
if [ "${DOGANY_ROLLBACK:-0}" = "1" ]; then
  msg "  [update] 롤백 모드: 2.0 마이그레이션 스텝 M 건너뜀 (역방향은 ROLLBACK 런북의 수동 절차 -- 업스트림 소스 저장소)" \
      "  [update] rollback mode: skipping the 2.0 migration (step M); the reverse leg is the manual procedure in the ROLLBACK runbook (upstream source repo)"
elif ! step_m_version_gate "$REPO_VERSION"; then
  if [ "$REPO_VERSION" = "unknown" ]; then
    msg "  [update][경고] 소비 릴리스 버전을 읽을 수 없어 2.0 마이그레이션(스텝 M)을 건너뜁니다 (fail-closed)" \
        "  [update][WARN] consumed release version unreadable -- skipping the 2.0 migration (step M) fail-closed"
  else
    msg "  [update] 스텝 M 건너뜀: v${REPO_VERSION} 는 pre-2.0 릴리스 -- 2.0 정체성/로더 이행은 v${STEP_M_MIN_FW_VERSION} 이상 릴리스에서 실행됩니다" \
        "  [update] step M skipped: v${REPO_VERSION} is a pre-2.0 release -- the 2.0 identity/loader migration runs on v${STEP_M_MIN_FW_VERSION}+ releases only"
  fi
else
  migrate_step_m
fi
# <<< DGN-1082 STEP-M-GATE END

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

# FRAMEWORK_MANIFEST / framework_manifest_sha() formerly lived here; hoisted
# above section 3a (DGN-773 T5) -- migration step M (the 3f/3g boundary) writes
# the manifest for the rules docs it installs, and that runs before this point.
# Definition only moved; the 3k/3k3/3k4/3k6 call sites below are unchanged.

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
    # DGN-956 R2 note: this plain rsync would replace an instance-side symlink
    # (a crew-shared 'share' skill) with a real dir if the TEMPLATE ever ships
    # a bundle skill of the SAME NAME -- silent re-divergence. compat-lint C7
    # refuses such a collision at pack lint time; keep template bundle names
    # disjoint from pack 'share' skill names.
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
        # DGN-956 R9 guard: a 'share' bundle skill is a SYMLINK into the
        # crew-shared canonical (~/.dogany/crews/<kit>/shared-skills/).
        # Substituting through it would engrave THIS instance's placeholder
        # values into the shared body -- estate-wide pollution. Skip links.
        # (Strip the glob's trailing slash: -L on "link/" resolves through
        # the link and would miss it.)
        [ -L "${_bundle_skill_dir%/}" ] && continue
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
#
#     PRESENCE-GATED (DGN-1141 stage 8, M11 MAJOR-2). This channel used to
#     CREATE RULES.md wherever it was absent. The reason the file is kept at all
#     is stated at migration step 3 -- "a PRE-2.0 instance keeps its real
#     RULES.md v1 file ... rollback stays a one-liner" -- and that reason does
#     not reach an instance minted straight into the 2.0 layout, which was
#     never pre-2.0 and has no v1 chain to roll back to. On such a tree the
#     file is 7.8K of routing rules pointing at USER.md / AGENT.md, names this
#     layout does not have, disagreeing with the canon the agent actually
#     loads (rules/hot.framework.md routes the same facts to identity/). mint
#     stopped shipping it in the same change; the `-f "$RULES_DEST"` term keeps
#     it refreshed for every instance that legitimately still has one.
if [ -f "$TEMPLATE/RULES.md" ] && [ -f "$INSTANCE/RULES.md" ]; then
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

# 3k6) CONSTITUTION.md / CONTRACT.md / AGENTS.md -- the 2.0 framework root docs
#     (DGN-773 R-7). Standing refresh channel, same contract as RULES.md in
#     section 3k (framework-owned, no mint placeholders, verbatim cp -pL,
#     user-edit detection + <name>.user-<ts> backup, sha recorded in the
#     framework manifest) -- implemented once as install_framework_root_doc()
#     next to migration step M above, because step M needs the same machine.
#
#     WHY THIS EXISTS: without it update.sh would mint instances it cannot
#     maintain. The frozen RULES.md v1 would keep being refreshed forever while
#     the three files the agent actually reads went stale from the day they
#     were installed.
#
#     NO INSTANCE-OWNED FILE IS EVER HERE. That used to be spelled out as a
#     list ("DISCIPLINE.md is not here and must never be added"); since
#     DGN-1141 stage 6 it is spelled out as a NAME: install_framework_root_doc()
#     dies on any *.custom.* target, so rules/hot.custom.md and
#     identity/hot.custom.* cannot be added here even by accident.
#
#     Instances that already ran step M in this same run are a no-op here --
#     install_framework_root_doc() tracks what it installed (_FW_ROOT_DOCS_DONE).
#
#     README.md (DGN-1141 stage 8, M11 MAJOR-3): the ONE owner-facing document
#     in the tree, and the only 2.0 doc that had no channel at all. After the
#     relayout the stale root copy told the owner "your profile is in USER.md"
#     about a file the same run had just deleted, and mint's `rsync -aL`
#     dereferenced the template's README.md -> playbooks/layout.md symlink into
#     a SECOND real file that only the other copy was refreshed from -- drift
#     by construction. Both are now installed from the same template source
#     (cp -pL dereferences the symlink), so they cannot disagree. Retiring the
#     root copy instead was rejected: root README.md is where an owner looks,
#     and playbooks/ is the drawer layout.md itself labels "do not touch
#     (framework internals)".
#
#     DGN-1141 stage 6: WHICH set depends on the layout on disk. A relaid-out
#     instance reads rules/hot.framework.md (which absorbed CONSTITUTION +
#     CONTRACT + DISCIPLINE, M9 section 1c); shipping the root pair as well
#     would double-inject the same rules and re-open the two-copy window that
#     M9 section 6-3 asked this stage to close. A not-yet-relaid-out instance
#     still reads the root pair, so it keeps getting it.
if [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
  install_framework_root_doc "rules/hot.framework.md"      "프레임워크 규칙(hot)"  "framework rules (hot)"
  install_framework_root_doc "rules/cold.framework.why.md" "프레임워크 근거(cold)" "framework rationale (cold)"
  install_framework_root_doc "vendors/telegram.md"         "벤더 계약: telegram"   "vendor contract: telegram"
  install_framework_root_doc "playbooks/layout.md"         "레이아웃 플레이북"     "layout playbook"
  install_framework_root_doc "README.md"                   "오너용 레이아웃 안내"  "owner-facing layout guide"
else
  install_framework_root_doc "CONSTITUTION.md" "프레임워크 헌법" "framework constitution"
  install_framework_root_doc "CONTRACT.md"     "프레임워크 계약" "framework contract"
fi

# THE HUB IS GATED ON THE LAYOUT, NOT ON THE RELEASE. agents/.template/AGENTS.md
# chains rules/ + identity/. Installing it into an instance that has not been
# relaid out yet replaces a WORKING hub with one whose every @-target is
# missing -- and a missing @-include is exit 0 with zero stderr (DGN-1141 M2),
# so the agent would boot with no rules at all and nothing would say so.
#
# This is not hypothetical: the step-M/N version gate (DGN-1082) skips the
# migration on a pre-2.0 release, and relayout_preconditions_ok() can defer it
# for one run -- in both cases this channel still runs. Measured on a real
# pre-relayout instance before the guard: "[loader] 1/4 loaded; MISSING:
# hot.framework.md,hot.custom.agent.md,hot.custom.owner.md".
#
# A deferred hub is safe: the instance keeps the hub it already has, which
# chains the docs it already has.
if [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
  install_framework_root_doc "AGENTS.md" "로더 허브" "loader hub"
elif [ -f "$INSTANCE/AGENTS.md" ]; then
  msg "  [update] AGENTS.md 허브 갱신 보류: 인스턴스가 아직 2.0 레이아웃이 아닙니다 (신형 허브는 rules/·identity/ 를 부르므로 지금 넣으면 로더 체인이 통째로 죽습니다)" \
      "  [update] AGENTS.md hub refresh deferred: this instance is not on the 2.0 layout yet (the new hub chains rules/ + identity/, which it does not have -- installing it now would kill the loader chain outright)"
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

# 3k5) pre-push gate scanner (DGN-1058). git-hooks/pre-push (delivered by 3k1
#      above) invokes <instance>/scripts/secret-sweep.sh -- but scripts/ was
#      never a sync channel, so instances ran whatever sweep they were minted
#      with, forever (found live: a 3-week-stale sweep predating DGN-808).
#      Enumerated PER-FILE on purpose: scripts/ also holds dev/release
#      machinery (publish.sh, release tooling -- DGN-960 STRIP scope) that
#      must never land in an instance. A whole-dir channel here would be the
#      wrong answer; add further files only as deliberate manifest entries.
#      Source is the template copy (byte-identical to the canonical root copy
#      by DGN-1058 invariant), consistent with the rest of the instance image.
for _sweep_file in "scripts/secret-sweep.sh" "scripts/sweep-identity.example"; do
  if [ -f "$TEMPLATE/$_sweep_file" ] && is_preserved "$_sweep_file"; then
    msg "  [update] 보존: $_sweep_file (.dogany-preserve)" \
        "  [update] preserved: $_sweep_file (.dogany-preserve)"
  elif [ -f "$TEMPLATE/$_sweep_file" ]; then
    ensure_dir "$INSTANCE/scripts"
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] $_sweep_file 갱신 예정" "  [dry-run] would refresh $_sweep_file"
    else
      cp -p "$TEMPLATE/$_sweep_file" "$INSTANCE/$_sweep_file"
    fi
    UPDATED+=("$_sweep_file")
  fi
done

# 3k2) playbooks/AGENT-OPS.md -- framework ops reference doc (DGN-387; moved
#     out of the instance root into playbooks/ by DGN-1141 stage 6, the same
#     commit that moves the template file and retargets tests/agentops). A
#     framework-owned file, like RULES.md, but with ONE deliberate
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
#     LAYOUT-AWARE DESTINATION (DGN-1141 stage 6): a relaid-out instance reads
#     playbooks/AGENT-OPS.md (its persona file points there); a not-yet-relaid
#     one still reads the root copy and its persona still points at the root
#     name. Installing to the wrong one leaves the LIVE pointer aimed at a copy
#     this channel no longer refreshes -- a doc that silently goes stale. The
#     relayout (section 3k8) is what moves the root copy into playbooks/.
if [ -f "$TEMPLATE/playbooks/AGENT-OPS.md" ]; then
  if [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
    OPS_REL="playbooks/AGENT-OPS.md"
    mkdir -p "$INSTANCE/playbooks" || die "failed to create $INSTANCE/playbooks"
  else
    OPS_REL="AGENT-OPS.md"
  fi
  # Step 0: template-side placeholder-contract assert. Any dunder token other
  # than __PROJECT_ROOT__/__HOME__ is a template contract violation -- it would
  # substitute cleanly under IDENTITY_OK=1 and hide, so surface it loudly.
  ops_bad="$(grep -oE '__[A-Z][A-Z_]*__' "$TEMPLATE/playbooks/AGENT-OPS.md" 2>/dev/null \
              | grep -vE '^__(PROJECT_ROOT|HOME)__$' | sort -u || true)"
  if [ -n "$ops_bad" ]; then
    msg "  [update][경고] AGENT-OPS.md 템플릿 규약 위반 -- 허용되지 않은 플레이스홀더:" \
        "  [update][WARN] AGENT-OPS.md template contract violation -- illegal placeholder(s):"
    printf '%s\n' "$ops_bad" >&2
  fi

  OPS_SRC="$TEMPLATE/playbooks/AGENT-OPS.md"
  OPS_DEST="$INSTANCE/$OPS_REL"
  # Step 1: build substituted incoming in a dest-adjacent temp. cp -pL carries
  # the source mode onto the 0600 mktemp file and dereferences a symlink source.
  ops_tmp="$(mktemp "${OPS_DEST}.new.XXXXXX")"
  cp -pL "$OPS_SRC" "$ops_tmp"
  subst_one "$ops_tmp"  # only __PROJECT_ROOT__/__HOME__ fire -> deterministic

  # Step 2: gather the three shas (all post-substitution for incoming).
  ops_incoming_sub="$(file_checksum "$ops_tmp")"
  ops_cur="$(file_checksum "$OPS_DEST")"
  ops_recorded="$(framework_manifest_sha "$OPS_REL")"

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
      ops_bak="${OPS_DEST}.user-$ts"
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
        ops_esc="$(printf '%s' "$OPS_REL" | sed 's/\./\\./g')"
        grep -vE "^#|^${ops_esc}[[:space:]]" "$FRAMEWORK_MANIFEST" 2>/dev/null || true
      fi
      printf '%s  %s\n' "$OPS_REL" "$ops_installed"
    } > "$ops_fw_tmp"
    mv -f "$ops_fw_tmp" "$FRAMEWORK_MANIFEST"
  fi
  # Step 6:
  UPDATED+=("$OPS_REL (framework ops doc)")
fi

# 3k8) retired root docs (DGN-1141 stage 6). A relaid-out instance reads
#     rules/hot.framework.md + identity/ + (injected) vendors/. The framework
#     docs that used to sit at its ROOT are then ORPHANS -- nothing chains
#     them, but they are still on disk saying the opposite of the live rules,
#     which is exactly the two-copy window M9 section 6-3 asked this stage to
#     close.
#
#     Every file here is FRAMEWORK-owned (each had its own refresh channel:
#     CONSTITUTION/CONTRACT = 3k6, bridge.md = 3k4, telegram.md = 3k3,
#     AGENT-OPS.md = 3k2), so removing the instance copy destroys nothing the
#     framework cannot reproduce. Even so it is a PARK, not a delete: the file
#     is copied to .dogany-migrate-backup/<name>.pre-relayout first (write-once,
#     same anchor rule as step N) and its framework-manifest line is dropped so
#     a later re-install is not mistaken for a user edit.
#
#     NOT here: RULES.md. R6 step 7 deliberately leaves the frozen v1 file on
#     disk so the ROLLBACK runbook stays a one-liner, and section 3k keeps
#     refreshing it to identical bytes.
#     NOT here either: anything instance-owned -- the loop asserts that.
#
#     .dogany-preserve wins. An instance that pinned one of these names asked
#     to keep it; we warn and move on rather than overriding the owner.
# >>> DGN-1141-3K8 BEGIN
#     (extracted verbatim by tests/dgn773_t5_migrate_step_m_selftest.sh --
#      keep it self-contained: the only externals are INSTANCE / DRY_RUN /
#      UPDATED / FRAMEWORK_MANIFEST and msg() / die() / is_preserved() /
#      is_instance_owned().)
if [ "$DRY_RUN" = "0" ] && [ -f "$INSTANCE/identity/hot.custom.agent.md" ]; then
  for _retired in CONSTITUTION.md CONTRACT.md bridge.md telegram.md AGENT-OPS.md; do
    [ -e "$INSTANCE/$_retired" ] || continue
    if is_instance_owned "$_retired"; then
      die "3k8 contract violation: '$_retired' is instance-owned and must never be retired by a framework channel"
    fi
    if is_preserved "$_retired"; then
      msg "  [update] 보존: $_retired (.dogany-preserve) -- 신형 레이아웃에서는 미사용 문서" \
          "  [update] preserved: $_retired (.dogany-preserve) -- unused in the 2.0 layout"
      continue
    fi
    if [ -L "$INSTANCE/$_retired" ]; then
      msg "  [update][경고] $_retired 이(가) 심링크입니다 -- 폐지 건너뜀 (수동 해소 필요)" \
          "  [update][WARN] $_retired is a symlink -- retirement skipped (resolve by hand)"
      continue
    fi
    _retired_anchor="$INSTANCE/.dogany-migrate-backup/$_retired.pre-relayout"
    mkdir -p "$INSTANCE/.dogany-migrate-backup" || die "failed to create the migration backup dir"
    if [ ! -f "$_retired_anchor" ]; then
      cp -p "$INSTANCE/$_retired" "$_retired_anchor" \
        || die "failed to anchor $_retired before retiring it"
    fi
    rm -f "$INSTANCE/$_retired" || die "failed to retire $_retired"
    if [ -f "$FRAMEWORK_MANIFEST" ]; then
      _retired_esc="$(printf '%s' "$_retired" | sed 's/\./\\./g')"
      _retired_tmp="$(mktemp "${FRAMEWORK_MANIFEST}.XXXXXX")"
      grep -vE "^${_retired_esc}[[:space:]]" "$FRAMEWORK_MANIFEST" > "$_retired_tmp" 2>/dev/null || true
      mv -f "$_retired_tmp" "$FRAMEWORK_MANIFEST"
    fi
    msg "  [update] 2.0 레이아웃: 미사용 루트 문서 폐지 -- $_retired (백업: $_retired_anchor)" \
        "  [update] 2.0 relayout: retired the unused root doc $_retired (backup: $_retired_anchor)"
    UPDATED+=("$_retired (retired -- unused in the 2.0 layout)")
  done
fi
# <<< DGN-1141-3K8 END

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
    # DGN-626 W1 landing stamp (spec v2 s1.5/s5.1): record WHICH tag this
    # instance consumed (DOGANY_FW_TAG) and WHEN that tag FIRST landed here
    # (DOGANY_FW_TAG_AT, epoch seconds). FW_TAG_AT is the soak-age anchor:
    # it moves ONLY when the consumed tag CHANGES, so repeated update runs on
    # the same tag (daily cron) and later conf edits never reset the soak
    # clock (M4: the anchor is the observed first landing, never the tag's
    # creatordate). Tag source: DOGANY_UPDATE_SRC_TAG exported by the
    # resolving parent; fallback DOGANY_UPDATE_PIN (a pinned tag-path run's
    # target IS the consumed tag -- covers a pre-DGN-626 parent driving this
    # tree as the --no-pull child, e.g. the staging bootstrap's first pinned
    # landing), guarded off the channel=main pull path where a pin has no
    # effect and HEAD is what actually landed. Neither known (plain
    # channel=main / local checkout run) -> leave any existing stamp alone.
    _fw_tag="${DOGANY_UPDATE_SRC_TAG:-}"
    if [ -z "$_fw_tag" ] && [ -n "${DOGANY_UPDATE_PIN:-}" ]; then
      _fw_chan="${DOGANY_UPDATE_CHANNEL:-$(sed -n 's/^DOGANY_UPDATE_CHANNEL=//p' "$INSTANCE/.instance.conf" | head -n1)}"
      [ "${_fw_chan:-release}" = "main" ] || _fw_tag="$DOGANY_UPDATE_PIN"
    fi
    if [ -n "$_fw_tag" ]; then
      _fw_prev_tag="$(sed -n 's/^DOGANY_FW_TAG=//p' "$INSTANCE/.instance.conf" | head -n1)"
      if [ "$_fw_prev_tag" != "$_fw_tag" ]; then
        if grep -q '^DOGANY_FW_TAG=' "$INSTANCE/.instance.conf"; then
          sed_inplace "$INSTANCE/.instance.conf" \
            -e "s#^DOGANY_FW_TAG=.*#DOGANY_FW_TAG=${_fw_tag}#"
        else
          printf 'DOGANY_FW_TAG=%s\n' "$_fw_tag" >> "$INSTANCE/.instance.conf"
        fi
        if grep -q '^DOGANY_FW_TAG_AT=' "$INSTANCE/.instance.conf"; then
          sed_inplace "$INSTANCE/.instance.conf" \
            -e "s#^DOGANY_FW_TAG_AT=.*#DOGANY_FW_TAG_AT=$(date +%s)#"
        else
          printf 'DOGANY_FW_TAG_AT=%s\n' "$(date +%s)" >> "$INSTANCE/.instance.conf"
        fi
      elif ! grep -q '^DOGANY_FW_TAG_AT=' "$INSTANCE/.instance.conf"; then
        # Same tag but no AT line (torn earlier write): backfill so the soak
        # anchor exists -- the conf-mtime fallback is the worse (age-
        # shrinking, false-negative) one.
        printf 'DOGANY_FW_TAG_AT=%s\n' "$(date +%s)" >> "$INSTANCE/.instance.conf"
      fi
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
