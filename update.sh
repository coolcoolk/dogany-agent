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
#   1. git pull (fast-forward the repo to the latest published framework).
#   2. Re-sync ONLY framework code into the instance (agents/main by default):
#      bridge code, routines, memory engine, service SDK, database schema,
#      config, .claude/settings.json, worklog template, and the official
#      framework skills (root skills/dogany-*). See the FRAMEWORK SERVICES
#      MANIFEST comment below for the exact allowlist.
#   3. Refresh RULES.md, the framework constitution (DGN-130). RULES.md is
#      framework-owned: users must not edit it. It is refreshed with the SAME
#      user-edit-detection + backup contract as the dogany-* skills -- if a
#      local edit is detected, the instance copy is backed up to
#      RULES.md.user-<timestamp> before being replaced.
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
#   ./update.sh --no-pull       # skip git pull (refresh from current checkout)
#   ./update.sh --dry-run       # show what would change, write nothing
#   ./update.sh --force         # bypass the .instance.conf validity gate
#   ./update.sh --yes | -y      # bypass the pre-flight confirmation prompt
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

  local ps1='powershell.exe -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\<your-linux-username>\dogany-agent\windows\setup-windows.ps1'
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
FORCE=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root)    INSTANCE="$2"; shift 2 ;;
    --no-pull) DO_PULL=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force)   FORCE=1; shift ;;
    -y|--yes)  ASSUME_YES=1; shift ;;
    -h|--help)
      sed -n '2,48p' "$0"; exit 0 ;;
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
#   bridge/                 bridge code (framework); venv + .env preserved
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
#   worklog/_TEMPLATE.md    ticket template only; never existing tickets
#   skills/dogany-*         official framework skills (edit-detect + backup)
#   .claude/skills-bundle/  dormant lifekit bundle skills
#   RULES.md                framework constitution (edit-detect + backup; DGN-130)
#
# Everything NOT on this list is instance state / personal data and is never
# written: memories/, *.db, .env, sessions, runtime/, logs/, bridge/venv/,
# user-authored (non-dogany-) skills, and the identity entrypoints below.
#
# IDENTITY GUARD (DGN-130): AGENT.md and USER.md are instance-owned identity
# (agent name, Role, accreted Workflows; user facts). They are NEVER part of
# any refresh path -- not in the manifest above, not in the RULES channel, not
# in any section-3 rsync (all of which target named subdirs, not the instance
# root). This constant exists so a future edit that tries to fold an entrypoint
# into a refresh path trips an explicit, greppable guard rather than silently
# clobbering identity. Do not remove; do not add AGENT.md / USER.md to it.
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

# Extractor: parse max(ALLOWED_USER_VERSIONS = (...) or [...]) from an
# sdk_bridge.py. DGN-364 2.7b fix: the previous regex parsed ONLY list
# syntax '[...]' while every live pin uses tuple syntax '(7, 8)' -- the
# guard as written could never engage. Accept tuple OR list via
# ast.literal_eval on either bracket form.
# Prints the integer on stdout; exits non-zero on parse failure.
extract_ver_sdk_bridge_py() {
  local f="$1"
  [ -f "$f" ] || return 1
  python3 -c "
import re, sys, ast
txt = open(sys.argv[1]).read()
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
# ---------------------------------------------------------------------------
PRESERVE_FILE="$INSTANCE/.claude/.dogany-preserve"
PRESERVE_ENTRIES=()
if [ -f "$PRESERVE_FILE" ]; then
  while IFS= read -r _pline || [ -n "$_pline" ]; do
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
is_preserved() {
  local rel="$1" e
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    [ "$e" = "$rel" ] && return 0
    case "$e" in
      */) case "$rel" in "$e"*) return 0 ;; esac ;;
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
PEX=()
build_preserve_excludes() {
  local prefix="$1" e rel
  PEX=()
  for e in ${PRESERVE_ENTRIES[@]+"${PRESERVE_ENTRIES[@]}"}; do
    case "$e" in
      "$prefix"/?*)
        rel="${e#"$prefix"/}"
        PEX+=(--exclude "/$rel")
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# 1) Sync the repo to the latest PUBLISHED RELEASE (DGN-221).
#    Instances consume release tags (v*), never main HEAD -- pushing dev
#    commits to main must not stealth-patch users whose VERSION still shows
#    the last release. Escape hatch for development checkouts:
#    DOGANY_UPDATE_CHANNEL=main restores the old `git pull --ff-only`.
# ---------------------------------------------------------------------------
if [ "$DO_PULL" = "1" ]; then
  if [ -d "$REPO_ROOT/.git" ]; then
    if [ "${DOGANY_UPDATE_CHANNEL:-release}" = "main" ]; then
      msg "[update] git pull (channel=main) ..." "[update] git pull (channel=main) ..."
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] git pull 생략" "  [dry-run] skipping git pull"
      else
        git -C "$REPO_ROOT" pull --ff-only \
          || die "git pull failed (resolve manually, or re-run with --no-pull)"
      fi
    else
      msg "[update] 최신 릴리스 태그 확인 ..." "[update] resolving latest release tag ..."
      if [ "$DRY_RUN" = "1" ]; then
        msg "  [dry-run] git fetch/checkout 생략" "  [dry-run] skipping git fetch/checkout"
      else
        git -C "$REPO_ROOT" fetch --tags origin \
          || die "git fetch failed (resolve manually, or re-run with --no-pull)"
        # Highest semver release tag. --sort=-v:refname handles v1.2.0 vs v1.10.0.
        LATEST_TAG="$(git -C "$REPO_ROOT" tag --list 'v*' --sort=-v:refname | head -n1)"
        if [ -z "$LATEST_TAG" ]; then
          die "no release tag (v*) found -- cannot resolve a published release"
        fi
        if [ "$(git -C "$REPO_ROOT" rev-parse HEAD)" = "$(git -C "$REPO_ROOT" rev-parse "${LATEST_TAG}^{commit}")" ]; then
          msg "[update] 이미 최신 릴리스 ($LATEST_TAG)" "[update] already at latest release ($LATEST_TAG)"
        else
          git -C "$REPO_ROOT" checkout --quiet "$LATEST_TAG" \
            || die "checkout $LATEST_TAG failed (local changes? resolve manually, or re-run with --no-pull)"
          msg "[update] 릴리스 체크아웃: $LATEST_TAG" "[update] checked out release: $LATEST_TAG"
        fi
      fi
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

# 3a) bridge code (framework), but keep the built venv and the live .env.
if [ -d "$TEMPLATE/bridge" ]; then
  build_preserve_excludes "bridge"
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    "$TEMPLATE/bridge/" "$INSTANCE/bridge/"
  UPDATED+=("bridge/")
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
  build_preserve_excludes "mirror"
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

# 3b) routines (framework schedulers/scripts). Preserve-list excludes guard
#     instance-customized routine scripts (DGN-359/DGN-363 clobber class).
if [ -d "$TEMPLATE/routines" ]; then
  build_preserve_excludes "routines"
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    "$TEMPLATE/routines/" "$INSTANCE/routines/"
  UPDATED+=("routines/")
fi

# 3c) memory engine code ONLY (*.py + taxonomy doc) -- never memory markdown/db.
#     Preserve excludes must precede the include chain (rsync filter rules are
#     order-sensitive: first match wins).
if [ -d "$TEMPLATE/memory-engine" ]; then
  build_preserve_excludes "memory-engine"
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    --include '*/' --include '*.py' --include '*.md' --exclude '*' \
    "$TEMPLATE/memory-engine/" "$INSTANCE/memory-engine/"
  UPDATED+=("memory-engine/*.py")
fi

# 3d) config: i18n locales are FRAMEWORK (refresh); agent.conf + lifekit.conf
#     are per-instance STATE scaffolds (user language/address, lifekit
#     activation choices). Same write-if-absent contract as .env / lifekit.db
#     in mint.sh: an update must NEVER reset user choices back to template
#     defaults (e.g. LIFEKIT=pending, AGENT_LANG=ko).
if [ -d "$TEMPLATE/config" ]; then
  build_preserve_excludes "config"
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    --exclude 'agent.conf' \
    --exclude 'lifekit.conf' \
    "$TEMPLATE/config/" "$INSTANCE/config/"
  for f in agent.conf lifekit.conf; do
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

# 3e) service SDK facade (hoisted at repo root, bundled into the instance).
if [ -d "$REPO_ROOT/service" ]; then
  build_preserve_excludes "service"
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    "$REPO_ROOT/service/" "$INSTANCE/service/"
  UPDATED+=("service/")
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

for f in schema.sql lifekit.py lifekit.sh README.md remind_select.py routine_roller.py routine_projection.py; do
  [ -f "$REPO_ROOT/database/$f" ] || continue

  # Instance-preserve list (DGN-359): skip files the operator declared local.
  if is_preserved "database/$f"; then
    msg "  [update] 보존: database/$f (.dogany-preserve)" \
        "  [update] preserved: database/$f (.dogany-preserve)"
    continue
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
UPDATED+=("database/schema.sql+CLI")

# 3f-migrate) apply pending lifekit.db schema migrations, forward-only.
#   The DB carries its schema version in SQLite's PRAGMA user_version. A DB freshly
#   created from schema.sql is version 1; real migrations start at 002. We apply
#   every migrations/NNN_*.sql whose NNN > the DB's current user_version, in
#   ascending numeric order, backing up the *.db before each apply. This is the
#   ONLY controlled path that mutates an existing lifekit.db (never delete/clobber).
MIG_DIR="$REPO_ROOT/database/migrations"
DB="$INSTANCE/database/lifekit.db"
if [ -d "$MIG_DIR" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  cur_ver="$(sqlite3 "$DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
  cur_ver="${cur_ver:-0}"
  applied_migs=()
  # Iterate migrations in ascending numeric order (NNN prefix). Glob is sorted,
  # and zero-padded 3-digit prefixes sort correctly lexically == numerically.
  for mig in "$MIG_DIR"/[0-9][0-9][0-9]_*.sql; do
    [ -e "$mig" ] || continue
    base="$(basename "$mig")"
    nnn="${base%%_*}"
    # Strip leading zeros for a clean numeric compare (avoid octal via 10#).
    n=$((10#$nnn))
    [ "$n" -gt "$cur_ver" ] || continue
    if [ "$DRY_RUN" = "1" ]; then
      msg "  [dry-run] 마이그레이션 $nnn 적용 예정 ($base)" \
          "  [dry-run] would apply migration $nnn ($base)"
    else
      # Back up the DB BEFORE applying this migration.
      ts="$(date +%Y%m%d-%H%M%S)"
      bak="$INSTANCE/database/lifekit.db.bak-$ts"
      cp -p "$DB" "$bak" || die "failed to back up lifekit.db before migration $nnn"
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
fi

# Substitute the mint placeholders on a single file, in place. Hoisted here (out
# of section 4) so BOTH the settings.json install (section 3g) and the skills
# refresh loop can substitute a freshly installed file at install time. For the
# skills loop this matters because we checksum right after: hashing before
# substitution would make the substituted on-disk copy look "user-modified" on
# the next update and back it up spuriously. For settings.json it matters because
# a harness hook firing between copy and a later substitution would read a raw
# __PROJECT_ROOT__ placeholder -- so we substitute atomically at install (3g).
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

# Content digest of a single file (DGN-130 RULES channel). Dereferences symlinks
# (the template's RULES.md is a symlink into rules/); missing file -> stable
# empty marker so a fresh instance and a deleted file both compare cleanly.
file_checksum() {
  local f="$1"
  [ -f "$f" ] || { printf '%s\n' "d41d8cd98f00b204e9800998ecf8427e-empty"; return; }
  shasum < "$f" 2>/dev/null | awk '{print $1}'
}

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
  rsync -aL $RSYNC_DRY "${COMMON_EXCLUDES[@]}" ${PEX[@]+"${PEX[@]}"} \
    "$TEMPLATE/.claude/skills-bundle/" "$INSTANCE/.claude/skills-bundle/"
  UPDATED+=(".claude/skills-bundle/")
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

# ---------------------------------------------------------------------------
# 3l) .env backfill -- add missing keys to the instance .env (idempotent).
#
#     Pre-DGN-167 installs lack BRIDGE_MODELS; bot.py falls back to "sonnet"
#     only, so /model shows a single entry. This step backfills any absent
#     key WITHOUT touching existing lines (user customizations are preserved
#     byte-for-byte; ordering is preserved; re-running is a no-op).
#
#     Design:
#       BACKFILL_KEYS is a declarative list of "KEY:resolver_func" pairs.
#       resolve_<KEY>() returns the default value string. Adding a new backfill
#       key in future = add one entry to BACKFILL_KEYS and one resolver.
#       Currently only BRIDGE_MODELS is wired (no speculative keys).
#
#     Placement: after all framework-file refreshes (sections 3a-3k), before
#     placeholder substitution (section 4). The .env is excluded from rsync
#     (COMMON_EXCLUDES) so it arrives here untouched by the refresh pass.
#
#     Provenance: each appended key gets a dated comment so operators can see
#     which update run added it and why -- mirrors the .instance.conf
#     DOGANY_FW_VERSION stamp convention.
# ---------------------------------------------------------------------------

# Probe $HOME/.claude.json for the subscription tier (same logic as
# install.sh recommend_model / step_model). Returns the bridge model list
# on stdout: "fable,opus,sonnet,haiku" for max tier, "sonnet,haiku" otherwise. (DGN-346)
# Exits non-zero on any failure (no python3 / missing file / parse error).
# This is a LOCAL read of the current machine's Claude CLI credential file;
# it makes no network call. Conservative fallback when the probe fails is
# handled by the caller.
resolve_BRIDGE_MODELS() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "${HOME}/.claude.json" <<'PYEOF'
import sys, json
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    oa = data.get("oauthAccount") or {}
    tier  = str(oa.get("organizationRateLimitTier") or "").lower()
    otype = str(oa.get("organizationType") or "").lower()
    if "max" in tier or "max" in otype:
        print("fable,opus,sonnet,haiku")  # DGN-346: fable-first
    else:
        print("sonnet,haiku")
except Exception:
    sys.exit(1)
PYEOF
}

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
    # than they would get from a fresh max-tier install. DGN-346: fable-first.
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

# Declarative backfill key list: "KEY:resolver_func" entries.
# Add future keys here as one additional line; add a resolve_KEY function.
BACKFILL_KEYS=(
  "BRIDGE_MODELS:resolve_BRIDGE_MODELS"
)

ENV_FILE="$INSTANCE/.telegram_bot/.env"
if [ -f "$ENV_FILE" ]; then
  for _bfentry in "${BACKFILL_KEYS[@]}"; do
    _bfkey="${_bfentry%%:*}"
    _bffn="${_bfentry##*:}"
    backfill_env_key "$ENV_FILE" "$_bfkey" "$_bffn"
  done
elif [ "$DRY_RUN" = "1" ]; then
  msg "  [dry-run] .env 없음 -- 백필 건너뜀 ($ENV_FILE)" \
      "  [dry-run] .env absent -- skipping backfill ($ENV_FILE)"
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

    # DGN-140: (re)register the polling watchdog now that the watchdog files
    # are refreshed, substituted, and renamed. Non-fatal by contract.
    if [ -f "$INSTANCE/bridge/watchdog_setup.sh" ]; then
      bash "$INSTANCE/bridge/watchdog_setup.sh" \
        || msg "[update][경고] 워치독 등록에 실패했습니다 (무시하고 진행)." \
               "[update][WARN] Watchdog registration failed (continuing)."
    fi
  fi

  # Record the framework version this instance now matches.
  if [ -f "$INSTANCE/.instance.conf" ]; then
    if grep -q '^DOGANY_FW_VERSION=' "$INSTANCE/.instance.conf"; then
      sed_inplace "$INSTANCE/.instance.conf" \
        -e "s#^DOGANY_FW_VERSION=.*#DOGANY_FW_VERSION=${REPO_VERSION}#"
    else
      printf 'DOGANY_FW_VERSION=%s\n' "$REPO_VERSION" >> "$INSTANCE/.instance.conf"
    fi
  fi

  # Sanity: warn on any surviving placeholders in active code.
  LEFT="$(grep -rlE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME)__' \
            --include='*.py' --include='*.sh' --include='*.json' --include='*.plist' \
            --include='*.service' --include='*.timer' \
            "$INSTANCE/bridge" "$INSTANCE/routines" "$INSTANCE/memory-engine" \
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

# WSL: nag (never fail) if the Windows-side setup drifted below the required version.
wsl_drift_nag
