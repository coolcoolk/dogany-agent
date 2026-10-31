#!/bin/sh
# self-update.sh -- update THIS instance to the latest framework, zero args.
#
# WHAT THIS IS: the "update yourself" entrypoint. An agent told to update
# itself runs THIS script -- it needs no --root and no operator input.
#
# update != release:
#   - self-update.sh / update.sh CONSUME a published framework release into
#     an instance (this is "update yourself").
#   - bumping VERSION + tagging PRODUCES a release (that is release.sh, a
#     separate, maintainer-only act). "Update yourself" is NEVER a release.
#
# Behaviour:
#   1. Resolve THIS instance's own root from the script's own location
#      (routines/ -> instance root), NOT from cwd -- the job survives a
#      workspace move and does not depend on where it is invoked from.
#   2. Refuse if that root has no .instance.conf (mirrors update.sh's gate:
#      a real minted instance always carries one).
#   3. Read DOGANY_REPO_ROOT from .instance.conf and resolve the update
#      SOURCE. Channel semantics (DGN-593 A3):
#        - release (default, DGN-221): the shared checkout is NEVER moved.
#          Read-only against the repo: `git fetch --tags`, then extract the
#          latest v* tag into a private per-run temp dir with `git archive`
#          and hand off to THAT tree's update.sh. Working tree / HEAD /
#          index of the shared repo are untouched -- release never touches
#          the shared checkout.
#        - main (DOGANY_UPDATE_CHANNEL=main, env or .instance.conf): old
#          `git pull --ff-only` dogfood behaviour, guarded: the repo must
#          actually be ON the main branch (detached / dev branch = a dev
#          session in progress -> die, never auto-switch).
#   4. Invoke update.sh --root <self> --no-pull --yes (self-targeted,
#      non-interactive) as a CHILD process -- never exec: exec would kill
#      the EXIT trap that cleans the per-run temp source. The child's exit
#      status is passed through as this script's exit status.
#
# No owner data, no machine paths, no identity placeholders: every path is
# resolved at runtime. Safe to ship generically (template / OSS).
#
# Usage:
#   ./self-update.sh            # update this instance to the latest framework
#   DOGANY_LANG=ko ./self-update.sh   # Korean messages (default: en)
#
# Pass-through: any extra args are forwarded to update.sh (e.g. --dry-run,
# --no-pull) for advanced/debug use; the common case is zero args.
#
# Local flags (consumed here, NEVER forwarded to update.sh):
#   --no-restart   skip the automatic bridge restart after a successful update
#   --direct-why=<reason>  estate hosts only: run outside the /update ledger
#                  anyway (operator emergency; the reason is recorded -- see
#                  the DGN-1417 single-door guard below)
#   --restart-notice=<version> --notice-file=<release note> [--product=<name>]
#                  render-only (DGN-1758): print the restart-complete notice
#                  for <version> (body from <release note>'s user-summary
#                  block, language from DOGANY_LANG) and exit 0. No update,
#                  no restart, no instance gate. The /update supervisor uses
#                  it to send the same notice in each restarted agent's chat.
#                  --product names the header product when the agent was
#                  touched ONLY by a pack/kit (DGN-1758 pack-name); omitted
#                  or empty -> the framework-style header (byte-identical).
#
# DGN-685: after a successful update this script carries the owner's single
# approval through to the bridge restart (bridge/self_restart.sh). Interactive
# runs (CLAUDECODE env present) restart immediately (--trigger user);
# autonomous runs (cron -- no CLAUDECODE) use --trigger auto so the idle guard
# can quietly defer. --dry-run implies --no-restart. Note: a no-op update
# (already up to date, rc 0) still restarts -- deliberate, it flushes stale
# long-running bridge processes (DGN-685 edge m2).
set -eu

# UTF-8 locale regardless of caller env. `cut -c` below (DGN detail-body
# truncation) is a POSIX character count only under a UTF-8 locale; under
# the C locale (e.g. an env with LANG/LC_ALL unset) it would count bytes
# instead and could slice a Korean release-notes character in half.
# Host coverage (DGN-1059): en_US.UTF-8 is not guaranteed (minimal Linux may
# not have it generated), and setting a missing locale falls back to C
# SILENTLY -- the fix would be inert. So probe candidates and take the first
# whose charmap really is UTF-8 (C.UTF-8 is built into glibc >= 2.35,
# Ubuntu 22.04+, and present on macOS). The probe must never kill the script
# under `set -e` -- hence 2>/dev/null + `|| true`.
_utf8_loc=""
for _cand in en_US.UTF-8 C.UTF-8; do
  if [ "$(LC_ALL="$_cand" locale charmap 2>/dev/null || true)" = "UTF-8" ]; then
    _utf8_loc="$_cand"
    break
  fi
done
if [ -n "$_utf8_loc" ]; then
  export LC_ALL="$_utf8_loc"
  export LANG="$_utf8_loc"
else
  # No UTF-8 locale at all. Do NOT abort the update over it (the update
  # itself still works), but never stay silent: silent byte-truncation is
  # the exact defect class this block exists to prevent.
  printf '%s\n' "[self-update.sh] WARN: no UTF-8 locale available (tried en_US.UTF-8, C.UTF-8); truncation may cut bytes, not characters, and can corrupt multi-byte (e.g. Korean) text" >&2
fi

_ENV_LANG="${DOGANY_LANG:-}"
DOGANY_LANG="${DOGANY_LANG:-en}"
msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { msg "[오류] $1" "[ERROR] $1" >&2; exit 1; }

# Never let a git op pop an interactive credential prompt. Under launchd/cron
# (no controlling TTY) an unset value can still make git fall through to a GUI
# askpass helper or block; force non-interactive here so an auth failure fails
# FAST and LOUD instead of hanging or half-updating. Belt for the same setting
# update.sh relies on; exported so every child git process inherits it.
export GIT_TERMINAL_PROMPT=0

# DGN-685: arg pre-pass. --no-restart is a LOCAL flag (consumed, not
# forwarded); --dry-run implies no restart but IS forwarded to update.sh.
# POSIX "$@" rebuild idiom -- value-carrying args (e.g. --bridge-accept GLOB)
# survive intact because everything is re-appended quoted (grill M2).
RESTART=1
DRY=0
PR_TMP=""
DIRECT_WHY=""
NOTICE_VER=""
NOTICE_FILE=""
NOTICE_PRODUCT=""
for a in "$@"; do
  shift
  case "$a" in
    --no-restart) RESTART=0 ;;
    --dry-run)    RESTART=0; DRY=1; set -- "$@" "$a" ;;
    --direct-why=*) DIRECT_WHY="${a#--direct-why=}" ;;
    --restart-notice=*) NOTICE_VER="${a#--restart-notice=}" ;;
    --notice-file=*)    NOTICE_FILE="${a#--notice-file=}" ;;
    --product=*)         NOTICE_PRODUCT="${a#--product=}" ;;
    *)            set -- "$@" "$a" ;;
  esac
done

# DGN-685 B1: structural restart-trigger gate. Interactive session = the
# harness Bash tool sets CLAUDECODE -> the owner's update approval doubles as
# restart approval (--trigger user, idle guard skipped, immediate). No
# CLAUDECODE (cron / autonomous) -> --trigger auto, idle guard applies and a
# refusal quietly defers -- an autonomous run can never hard-kill an active
# owner session. Structural, not caller-discipline (dec-094).
if [ -n "${CLAUDECODE:-}" ]; then TRIG="user"; else TRIG="auto"; fi

# ---------------------------------------------------------------------------
# resolve_channel_tag <repo_root> <channel>  (DGN-621 v2 phase1, DGN-626 W1)
#   Mirror of update.sh's selector. Channels are distinguished by TAG SUFFIX,
#   and every tag-consuming channel is an explicit WHITELIST -- exclusion is
#   never delegated to versionsort ordering (DGN-626 F1):
#     release (default) : highest STABLE tag  vX.Y.Z  -- pre-release tags
#                         (any 'v*-*', e.g. -dev.N / -rc.N) are excluded.
#     dev               : highest of  vX.Y.Z | vX.Y.Z-dev.N  ONLY (-rc.N
#                         excluded: a live rc would shadow later -dev tags and
#                         be auto-consumed by dev canaries).
#     staging           : highest of  vX.Y.Z | vX.Y.Z-rc.N  ONLY (-dev
#                         excluded) -- the soak ring (DGN-626).
#   versionsort.suffix=- ranks any hyphenated suffix BELOW its stable, so once
#   the stable exists dev/staging subscribers move forward to it and never
#   regress to a pre-release. (Default git version-sort would WRONGLY rank
#   vX.Y.Z-dev.N ABOVE vX.Y.Z -- DGN-621 verified 2026-07-28 -- hence the
#   explicit -c flag; it also makes ordering independent of global git config.)
#   DOGANY_UPDATE_PIN (optional, env): OVERRIDES channel -- select exactly that
#     tag; if the pinned tag is absent, FAIL LOUD (never silent latest).
#   Unknown channel: FAIL LOUD (die) -- never a silent release fallback.
#   Stable-only estate (no PIN, release/unset) == the old one-liner result.
#   Prints the tag on stdout; empty = no eligible tag (callers handle "no tag").
# ---------------------------------------------------------------------------
resolve_channel_tag() {
  _rct_repo="$1"
  # DGN-1569: no ':-release' collapse here -- an EMPTY channel must reach the
  # case below and be announced, never silently aliased to 'release'.
  _rct_channel="${2-}"
  # DGN-1748: a tag release.sh has minted but not yet proven on origin (gate 8)
  # carries a ref under refs/dogany/unpublished/ -- it is never a candidate,
  # on any channel or pin. '/' (never a tag name) keeps grep's pattern
  # non-empty when nothing is held.
  _rct_hold="$(git -C "$_rct_repo" for-each-ref --format='%(refname:lstrip=3)' refs/dogany/unpublished/ 2>/dev/null)"
  _rct_hold="${_rct_hold:-/}"
  if [ -n "${DOGANY_UPDATE_PIN:-}" ]; then
    if printf '%s\n' "$_rct_hold" | grep -qxF -e "$DOGANY_UPDATE_PIN"; then
      die "DOGANY_UPDATE_PIN='${DOGANY_UPDATE_PIN}' is minted but NOT yet published to origin (release.sh has not passed its post-push assert) -- refusing to install an unpublished tag"
    fi
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
        | grep -vxF -e "$_rct_hold" \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]+)?$' | head -n1
      ;;
    staging)
      # Whitelist: stable + -rc.N only (-dev excluded). Same ranking flag:
      # the moment a base's stable is cut, staging subscribers advance to it
      # and never regress to an -rc.
      git -C "$_rct_repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname \
        | grep -vxF -e "$_rct_hold" \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$' | head -n1
      ;;
    release)
      # release (default): exclude pre-release tags (any tag whose name
      # contains a hyphen, e.g. -dev.N / -rc.N).
      git -C "$_rct_repo" tag --list 'v*' --sort=-v:refname \
        | grep -vxF -e "$_rct_hold" \
        | grep -v -- '-' | head -n1
      ;;
    "")
      # DGN-1569: EMPTY is not a channel -- it means NOBODY declared one, and
      # an unset value must never look identical to a deliberate 'release'
      # (the estate's 0-done-vs-not-run rule). Resolve the same stable tag
      # release would (a channel-less instance keeps updating -- notice, not
      # refusal), but announce the absence on stderr every single run.
      msg "[update][경고] DGN-1569: 업데이트 채널이 선언되지 않았습니다 -- 기본값 release(안정 태그)로 해석합니다. DOGANY_UPDATE_CHANNEL 을 .instance.conf 에 명시 선언하세요." \
          "[update][WARN] DGN-1569: NO update channel declared -- resolving as the default 'release' (stable tags). Declare DOGANY_UPDATE_CHANNEL explicitly in .instance.conf." >&2
      git -C "$_rct_repo" tag --list 'v*' --sort=-v:refname \
        | grep -vxF -e "$_rct_hold" \
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

# ---------------------------------------------------------------------------
# run_with_timeout <secs> <cmd> [args...]  (DGN-604)
#   Run <cmd> in the background and hard-kill it if it outlives <secs>.
#   GIT_TERMINAL_PROMPT=0 above only silences git's OWN username/password
#   prompt; a locked macOS login keychain accessed by a GUI-session process
#   (e.g. a launchd agent, unlike a headless cron job) can still raise an
#   OS-level keychain-unlock dialog that no script input can dismiss -- with
#   no independent ceiling THAT would wedge the probe (and the whole
#   self-update run) forever, which is the exact "must not hang" failure this
#   ticket calls out. No timeout(1)/gtimeout(1) assumed (macOS ships neither):
#   plain background pid + sleep-watcher kill, portable /bin/sh.
#   Combined stdout+stderr lands in $RWT_OUTPUT; exit status is returned.
#   A watcher-forced kill exits 143 (128+SIGTERM) -- distinguishable from a
#   normal command failure by the caller if it cares to.
# ---------------------------------------------------------------------------
run_with_timeout() {
  _rwt_secs="$1"; shift
  _rwt_out="$(mktemp "${TMPDIR:-/tmp}/dogany-authprobe.XXXXXX")" || return 1
  "$@" >"$_rwt_out" 2>&1 &
  _rwt_pid=$!
  ( sleep "$_rwt_secs"; kill -TERM "$_rwt_pid" 2>/dev/null ) &
  _rwt_watcher=$!
  _rwt_rc=0
  wait "$_rwt_pid" 2>/dev/null || _rwt_rc=$?
  kill "$_rwt_watcher" 2>/dev/null || true
  wait "$_rwt_watcher" 2>/dev/null || true
  RWT_OUTPUT="$(cat "$_rwt_out" 2>/dev/null)"
  rm -f "$_rwt_out"
  return "$_rwt_rc"
}

# ---------------------------------------------------------------------------
# fw_clone_shape_heal <repo_root> <dry_run>  (DGN-1947)
#   A framework clone made with `git clone --depth 1 --branch vX.Y.Z` (a hand
#   recovery, 2026-10-06) is shallow and its ONLY fetch refspec is
#   +refs/tags/vX.Y.Z:refs/tags/vX.Y.Z: nothing it fetches by default ever
#   names a later version. Runs AFTER tag_divergence_check (DGN-1717 already
#   compared every local v* tag with origin) and BEFORE the tag fetch that
#   resolves the target. When every fetch refspec names one exact tag (no
#   branch, no wildcard) it restores the standard clone refspec
#   (+refs/heads/*:refs/remotes/origin/*) plus a NON-forced
#   refs/tags/*:refs/tags/* -- deliberately without '+': a later plain fetch
#   still refuses to clobber a diverged tag, so the DGN-1717 baseline cannot
#   move behind the guard's back. A shallow clone is unshallowed. Any other
#   clone (a refspec naming a branch or a wildcard, not shallow) is left
#   exactly as it is. Dry-run names what it would do and changes nothing. A
#   failed step is a warning: the tag fetch right after decides the run.
#   POSIX sh, byte-identical in update.sh and routines/self-update.sh
#   (tests/dgn1723_selfupdate_parity_selftest.sh pins the copies).
# ---------------------------------------------------------------------------
fw_clone_shape_heal() {
  _fh_repo="$1"; _fh_dry="$2"
  git -C "$_fh_repo" remote get-url origin >/dev/null 2>&1 || return 0
  _fh_specs="$(git -C "$_fh_repo" config --get-all remote.origin.fetch 2>/dev/null || true)"
  _fh_narrow=1
  for _fh_s in $_fh_specs; do
    _fh_src="${_fh_s#+}"; _fh_src="${_fh_src%%:*}"
    case "$_fh_src" in
      *'*'*) _fh_narrow=0 ;;
      refs/tags/*) : ;;
      *) _fh_narrow=0 ;;
    esac
  done
  _fh_shallow="$(git -C "$_fh_repo" rev-parse --is-shallow-repository 2>/dev/null || echo false)"
  [ "$_fh_narrow" = "1" ] || [ "$_fh_shallow" = "true" ] || return 0
  msg "[update] 프레임워크 clone 형태 복구 (DGN-1947): $_fh_repo (fetch 범위: $(printf '%s ' $_fh_specs)| shallow=$_fh_shallow) -- 이 형태로는 새 버전 태그를 받지 못합니다." \
      "[update] framework clone shape heal (DGN-1947): $_fh_repo (fetch refspec: $(printf '%s ' $_fh_specs)| shallow=$_fh_shallow) -- a clone of this shape never receives a newer version tag." >&2
  if [ "$_fh_dry" = "1" ]; then
    msg "  [dry-run] 실제 실행은 fetch 범위를 브랜치 + 전체 태그로 넓히고 shallow 를 해제합니다 (지금은 변경 없음)." \
        "  [dry-run] the real run widens the refspec to branches + all tags and unshallows (nothing changed now)." >&2
    return 0
  fi
  if [ "$_fh_narrow" = "1" ]; then
    if git -C "$_fh_repo" config --replace-all remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*' \
       && git -C "$_fh_repo" config --add remote.origin.fetch 'refs/tags/*:refs/tags/*'; then
      msg "  fetch 범위 복구: +refs/heads/*:refs/remotes/origin/* + refs/tags/*:refs/tags/* (태그 강제 갱신 없음)" \
          "  refspec restored: +refs/heads/*:refs/remotes/origin/* + refs/tags/*:refs/tags/* (tags never force-updated)" >&2
    else
      msg "  [경고] fetch 범위를 바꾸지 못했습니다 -- 아래 태그 fetch 로 계속합니다." \
          "  [WARN] could not rewrite the fetch refspec -- continuing with the tag fetch below." >&2
    fi
  fi
  if [ "$_fh_shallow" = "true" ]; then
    if GIT_TERMINAL_PROMPT=0 git -C "$_fh_repo" fetch -q --unshallow origin >&2; then
      msg "  shallow 해제 완료" "  unshallowed" >&2
    else
      msg "  [경고] shallow 해제 실패 -- 아래 태그 fetch 로 계속합니다." \
          "  [WARN] unshallow failed -- continuing with the tag fetch below." >&2
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# preflight_auth_check <repo_root>  (DGN-604 hardening)
#   The shared framework clone can track a PRIVATE remote, so every fetch/
#   pull needs a real credential. On macOS that credential is read out of the
#   login keychain (osxkeychain helper, or the `gh:github.com` entry `gh auth
#   git-credential` reads); on a host that rebooted but has not reached GUI
#   login yet, the login keychain is still LOCKED, so the credential is
#   unreadable and a private fetch fails with an opaque "could not read
#   Username" error deep inside update.sh's own git calls -- or, worse, hangs
#   on an unanswerable unlock prompt (see run_with_timeout above). A
#   launchd/cron self-update firing in that window needs a fast, clear,
#   non-hanging failure instead.
#
#   This probe runs a cheap, read-only, strictly non-interactive
#   `git ls-remote` against origin BEFORE any real fetch/pull touches the
#   tree. Measured cost on a healthy authenticated private remote: ~0.8s
#   (network+TLS+auth handshake) -- a second handshake beyond what the real
#   fetch/pull pays right after, added ONLY on the already-slow network path,
#   never skipped to save time on the happy path (a skipped probe is not a
#   probe). On success we proceed. On failure we STOP with ONE clear,
#   actionable message (keychain-locked/not-logged-in vs. timeout vs. a
#   genuine network/remote error) instead of letting the real fetch emit a
#   cryptic error deeper in the run. This is exit-code-and-log evidence
#   (DGN-1194) -- no notification is sent from here.
#
#   Returns 0 = OK (or nothing to probe -- no origin configured). Returns 1 =
#   auth/net/timeout failure; the caller decides whether to die.
# ---------------------------------------------------------------------------
preflight_auth_check() {
  _pa_repo="$1"
  # Resolve origin URL; if there is no origin we cannot probe -- let the real
  # fetch surface whatever error it will (do not fabricate an auth verdict).
  _pa_url="$(git -C "$_pa_repo" remote get-url origin 2>/dev/null || true)"
  [ -n "$_pa_url" ] || return 0

  # Probe --heads HEAD: forces the same auth handshake a real fetch/pull would
  # pay, without transferring any objects. run_with_timeout is the hard
  # ceiling (default 15s, override via DOGANY_AUTH_PROBE_TIMEOUT for a slow
  # link or a fast test); GIT_TERMINAL_PROMPT=0 (exported above) is the belt
  # for git's own prompt path.
  _pa_rc=0
  run_with_timeout "${DOGANY_AUTH_PROBE_TIMEOUT:-15}" \
    git -C "$_pa_repo" ls-remote --heads origin HEAD || _pa_rc=$?
  _pa_err="$RWT_OUTPUT"
  [ "$_pa_rc" -eq 0 ] && return 0

  # Classify the failure so the operator sees the RIGHT remedy. Keychain-lock
  # / missing-credential / timeout symptoms differ from network/DNS symptoms.
  printf '%s\n' "============================================================" >&2
  case "$_pa_rc" in
    143)
      msg "[self-update][오류] 비대화 인증 확인이 ${DOGANY_AUTH_PROBE_TIMEOUT:-15}초 내에 끝나지 않아 중단(타임아웃) -- private remote fetch 불가." \
          "[self-update][ERROR] non-interactive auth probe did not finish within ${DOGANY_AUTH_PROBE_TIMEOUT:-15}s -- timed out, cannot fetch the private remote." >&2
      msg "  가능성 높은 원인: 잠긴 로그인 키체인의 OS 레벨 잠금해제 대화상자가 응답 없는 프로세스에서 멎어있음." \
          "  Likely cause: a locked login keychain's OS-level unlock dialog stalled with nothing there to answer it." >&2
      msg "  조치: GUI 로그인(키체인 잠금 해제) 후 재시도, 또는 'gh auth login' 재로그인." \
          "  Action: log into the GUI (unlocks the keychain) and retry, or re-run 'gh auth login'." >&2
      ;;
    *)
      case "$_pa_err" in
        *"could not read Username"*|*"Authentication failed"*|*"terminal prompts disabled"*|*"could not read Password"*|*"invalid credentials"*|*"403"*|*"denied"*)
          msg "[self-update][오류] 비대화 인증 실패 -- private remote fetch 불가." \
              "[self-update][ERROR] non-interactive auth failed -- cannot fetch the private remote." >&2
          msg "  가능성 높은 원인: 재부팅 후 GUI 로그인 전이라 로그인 키체인이 잠겨 자격증명을 못 읽음," \
              "  Likely cause: the login keychain is LOCKED (rebooted, not yet logged into the GUI)," >&2
          msg "  또는 gh 로그인이 만료됨 ('gh auth status' 확인)." \
              "  or the gh login has expired (check 'gh auth status')." >&2
          msg "  조치: GUI 로그인(키체인 잠금 해제) 후 재시도, 또는 'gh auth login' 재로그인." \
              "  Action: log into the GUI (unlocks the keychain) and retry, or re-run 'gh auth login'." >&2
          ;;
        *)
          msg "[self-update][오류] 원격 접근 실패 (인증 아닌 네트워크/원격 오류로 보임)." \
              "[self-update][ERROR] cannot reach the remote (looks like a network/remote error, not auth)." >&2
          msg "  네트워크 상태를 확인하고 재시도하세요." \
              "  Check connectivity and retry." >&2
          ;;
      esac
      ;;
  esac
  # Echo the raw git error (first line) for diagnosis, without leaking secrets
  # (ls-remote never prints the token).
  printf '  git: rc=%s %s\n' "$_pa_rc" "$(printf '%s' "$_pa_err" | head -n1)" >&2
  printf '%s\n' "============================================================" >&2
  return 1
}

# 1) Resolve this instance's own root: routines/ -> instance root.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 2) Gate: a real minted instance carries a .instance.conf.
CONF="$SELF_ROOT/.instance.conf"
# DGN-1758: the render-only mode needs no instance (it runs from the repo copy).
[ -f "$CONF" ] || [ -n "$NOTICE_VER" ] || die "not a minted Dogany instance (no .instance.conf): $SELF_ROOT"

# DGN-685 M3: user-notice language -- env DOGANY_LANG wins, else the
# instance's config/agent.conf AGENT_LANG, else en (msg() reads the result).
if [ -z "$_ENV_LANG" ]; then
  _CONF_LANG="$(sed -n 's/^AGENT_LANG=//p' "$SELF_ROOT/config/agent.conf" 2>/dev/null | head -n1)"
  if [ -n "$_CONF_LANG" ]; then DOGANY_LANG="$_CONF_LANG"; fi
fi

# DGN-1814 r2b: owner-facing notice words come from the layered i18n lookup
# (config/i18n/<lang>.json + kit layers; routines/lib/i18n_lookup.py) in
# DOGANY_LANG -- the SAME restart.* keys bridge/self_restart.sh renders, so
# the two restart-notice paths share one copy. English defaults in code; a
# miss (old locale file, no python3) never blocks the notice.
# i18n_fmt <key> <en-default> [name=value ...] -> value, {name} slots filled.
i18n_fmt() {
  _if_val="$(python3 "$SELF_ROOT/routines/lib/i18n_lookup.py" "$SELF_ROOT" "$DOGANY_LANG" "$1" 2>/dev/null)" || _if_val=""
  [ -n "$_if_val" ] || _if_val="$2"
  shift 2
  for _if_kv in "$@"; do
    _if_k="{${_if_kv%%=*}}"
    _if_v="${_if_kv#*=}"
    case "$_if_val" in
      *"$_if_k"*) _if_val="${_if_val%%"$_if_k"*}${_if_v}${_if_val#*"$_if_k"}" ;;
    esac
  done
  printf '%s' "$_if_val"
}

# ---------------------------------------------------------------------------
# DGN-687 helpers: release-note extraction for the restart-complete notice.
# DGN-822: notice bodies are passed RAW (no pre-escaping of & < >) -- push.sh
# routes every text send through the bridge sanitizer, which escapes entities
# itself; pre-escaping here would double-escape (e.g. "->" rendering as
# "-&gt;"). Whitelisted Telegram tags (<blockquote expandable>, ...) stay raw
# and pass the sanitizer verbatim.
# ---------------------------------------------------------------------------

# changelog_section <changelog_file> <version-without-v>
#   Print the CHANGELOG body for that version (lines between its "## [x.y.z]"
#   heading and the next "## [" heading). Empty output = not found (fail-open).
#   DGN-725: strip (DGN-NNN) and bare DGN-NNN ticket refs before output so
#   internal ticket numbers never reach the user-facing notice.
changelog_section() {
  _cs_file="$1"
  _cs_ver="$2"
  [ -f "$_cs_file" ] || return 0
  awk -v ver="## [${_cs_ver}]" '
    index($0, ver) == 1 { grab = 1; next }
    grab && /^## \[/    { exit }
    grab                { print }
  ' "$_cs_file" \
    | sed -e 's/([[:space:]]*DGN-[0-9][0-9]*[[:space:]]*)//g' \
          -e 's/[[:space:]]*DGN-[0-9][0-9]*//g' \
    | sed -e '/./,$!d'
}

# user_summary <releasefile> <lang>  (DGN-699 Spec A)
#   Extract the user-facing, jargon-free summary for <lang> from the
#   machine-readable HTML-comment block a release note carries near the top:
#
#     <!-- user-summary
#     ko: |
#       <Korean summary>
#     en: |
#       <English summary>
#     -->
#
#   This is the NOTICE body source, deliberately kept OUT of CHANGELOG.md
#   (which stays English/model-facing per DGN-210). Robust to absence: a
#   missing file, a missing block, or a missing <lang> key all print nothing
#   (empty stdout) so the caller falls back to changelog_section. YAML block
#   scalars ('key: |') are honoured -- every line indented deeper than the key
#   belongs to that key, and leading indentation is stripped. Matching stops at
#   the next same-or-less-indented 'key:' line or the closing '-->'.
user_summary() {
  _us_file="$1"
  _us_lang="$2"
  [ -f "$_us_file" ] || return 0
  awk -v want="$_us_lang" '
    # Enter the block on the "<!-- user-summary" opener; leave on "-->".
    /<!--[ \t]*user-summary[ \t]*$/ { inblk = 1; next }
    !inblk { next }
    /-->/  { exit }
    # A "key: |" line opens a block scalar; remember the key indent so we can
    # tell continuation lines (deeper) from the next key (same/less indent).
    match($0, /^[ \t]*[A-Za-z0-9_]+:[ \t]*\|[ \t]*$/) {
      line = $0
      sub(/:[ \t]*\|[ \t]*$/, "", line)      # -> "  ko"
      key = line; sub(/^[ \t]+/, "", key)    # -> "ko"
      keyind = match(line, /[^ \t]/) - 1     # indent columns of the key
      grab = (key == want) ? 1 : 0
      contind = -1
      next
    }
    grab {
      # First continuation line fixes the strip width (its own indent).
      curind = match($0, /[^ \t]/) - 1
      if ($0 ~ /^[ \t]*$/) { print ""; next }   # blank line inside the scalar
      if (curind <= keyind) { grab = 0; next }  # dedented -> scalar ended
      if (contind < 0) contind = curind
      print substr($0, contind + 1)
    }
  ' "$_us_file" | sed -e '/./,$!d'
}

# ---------------------------------------------------------------------------
# advance_consume_checkout <rc> <target_ref>  (DGN-1513 SPEC v2 SS2)
#   Advance the crew-leader-only consume checkout ($DOGANY_CONSUME_ROOT, a
#   detached `git worktree` of $REPO_ROOT under <instance>/.dogany-consume/)
#   to <target_ref> right after the child update.sh succeeds. <target_ref> is
#   whatever this run already resolved (LATEST_TAG for the tag channels, or
#   the just-pulled HEAD commit for the main channel) -- no new version math.
#   DOGANY_CONSUME_ROOT undeclared (the normal case: any
#   non-crew-leader instance) is a silent no-op -- the only conditional-
#   receipt logic this step has (spec SS2.1).
#   Failure is fail-stale (spec SS2.2): never dies the run (the framework
#   update already succeeded -- reporting that as a failure would be worse),
#   never silent either (bilingual ERROR line + the skill surface stays on
#   its previous tag, self-heals on the next successful run).
#   `checkout --detach` is called with no --force: a dirty checkout (nobody
#   should ever write there) is refused outright, not clobbered.
# ---------------------------------------------------------------------------
advance_consume_checkout() {
  _acc_rc="$1"
  _acc_ref="$2"
  [ "$_acc_rc" -eq 0 ] || return 0
  _acc_root="$(sed -n 's/^DOGANY_CONSUME_ROOT=//p' "$CONF" | head -n1)"
  [ -n "$_acc_root" ] || return 0
  if [ -z "$_acc_ref" ]; then
    msg "[self-update][오류] 소비 체크아웃 전진 대상을 판별할 수 없음 (채널=${UPDATE_CHANNEL:-unknown}) -- 직전 태그 유지: $_acc_root" \
        "[self-update][ERROR] could not determine a target ref to advance the consume checkout to (channel=${UPDATE_CHANNEL:-unknown}) -- staying at previous tag: $_acc_root" >&2
    return 0
  fi
  if [ ! -e "$_acc_root/.git" ]; then
    msg "[self-update][오류] 소비 체크아웃 없음 -- v2-ctl.sh on 으로 먼저 조성하세요: $_acc_root" \
        "[self-update][ERROR] consume checkout not found -- create it first with v2-ctl.sh on: $_acc_root" >&2
    return 0
  fi
  if ! _acc_dirty="$(git -C "$_acc_root" status --porcelain 2>&1)"; then
    # DGN-1513 F-3: this mode does NOT self-heal on the next run (a moved/
    # re-cloned canonical repo leaves the worktree's .git file dangling
    # forever) -- the error must name the repair verb, not just the freeze.
    msg "[self-update][오류] 소비 체크아웃 상태 확인 실패 -- 전진 거부(직전 태그 유지). 수리: .claude/skills/dogany-mint-agent/scripts/v2-ctl.sh on (정본 레포 이동/재클론 복구 경로): $_acc_root" \
        "[self-update][ERROR] failed to read consume checkout status -- advance refused (staying at previous tag). Repair: .claude/skills/dogany-mint-agent/scripts/v2-ctl.sh on (covers a moved/re-cloned canonical repo): $_acc_root" >&2
    return 0
  fi
  if [ -n "$_acc_dirty" ]; then
    msg "[self-update][오류] 소비 체크아웃 dirty -- 전진 거부(직전 태그 유지, --force 안 씀): $_acc_root" \
        "[self-update][ERROR] consume checkout is dirty -- advance refused (staying at previous tag, no --force): $_acc_root" >&2
    return 0
  fi
  if git -C "$_acc_root" checkout --detach "$_acc_ref" >/dev/null 2>&1; then
    msg "[self-update] 소비 체크아웃 전진: $_acc_ref ($_acc_root)" \
        "[self-update] consume checkout advanced: $_acc_ref ($_acc_root)"
  else
    msg "[self-update][오류] 소비 체크아웃 전진 실패 ($_acc_ref) -- 직전 태그 유지, 다음 실행에서 자동 재시도: $_acc_root" \
        "[self-update][ERROR] consume checkout advance FAILED ($_acc_ref) -- staying at previous tag, next run auto-retries: $_acc_root" >&2
  fi
  return 0
}

# ---------------------------------------------------------------------------
# restart_notice <vdisp> <release_file> [<product>]  (DGN-687 notice #3,
#   DGN-1758 seam, DGN-1758 pack-name)
#   Print the restart-complete notice for <vdisp> ("v1.2.3") in DOGANY_LANG.
#   The ONE renderer: maybe_restart below hands it to self_restart --notice
#   (never a <product> -- that path is always framework), and --restart-notice
#   (render-only mode) prints it for the /update supervisor
#   (scripts/pack/update_surface.py), which sends it in each restarted
#   agent's own chat and passes <product> when the agent was touched ONLY by
#   a pack/kit (owner-approved copy 2026-09-27 18:07). Missing file / block
#   -> header line only; missing <product> -> the framework-style header.
# ---------------------------------------------------------------------------
restart_notice() {
  _vdisp="$1"
  _rn_file="$2"
  _product="${3:-}"
  # DGN-687 notice #3 (restart complete = update complete): pushed by the
  # self_restart worker AFTER the bridge is back up. Korean copy is LOCKED
  # (owner-approved 2026-08-01; DGN-697 re-lock 2026-08-02: header line plain,
  # release-notes fold stays expandable); en mirrors the structure.
  #
  # DGN-699 Spec A: the fold body is the user-facing, jargon-free summary from
  # releases/v<ver>.md's user-summary block (rendered in DOGANY_LANG), NOT the
  # English CHANGELOG scrape (which is model-facing per DGN-210).
  # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/developer-facing
  # (DGN-210) and contains raw markdown headers, backticks, and internal file/
  # function names that must never reach user-facing output (RULES: no internal
  # mechanics in user-facing text). When the user-summary block is absent (old
  # release or timing gap), the notice shows no fold body -- safe silence is
  # preferable to exposing raw developer content.
  # DGN-785: strip_tickets <text> -- remove DGN-NNN markers so they can never
  #   reach user-facing output. Strips parenthesized form first, then bare form,
  #   then collapses any doubled spaces / dangling " -- " left behind.
  strip_tickets() {
    printf '%s\n' "$1" \
      | sed -e 's/([[:space:]]*DGN-[0-9][0-9]*[^)]*[[:space:]]*)//g' \
            -e 's/[[:space:]]*DGN-[0-9][0-9]*//g' \
            -e 's/  */ /g' \
            -e 's/[[:space:]]*--[[:space:]]*$//' \
            -e 's/^[[:space:]]*//'
  }
  _notes_raw="$(user_summary "$_rn_file" "$DOGANY_LANG")"
  # DGN-785: apply ticket-strip safety net to all section bodies before render.
  _notes="$(strip_tickets "$_notes_raw")"
  # DGN-742: extended detail fold (COMPLETION notice only).
  #   Reads <lang>_detail key from the same user-summary block. Hard-capped at
  #   ~2500 chars; truncated output gets a continuation hint.
  #   If detail is empty, notice is byte-identical to pre-742 output (parity).
  # DGN-785: detail and try sections use the same strip_tickets safety net.
  _detail_raw="$(user_summary "$_rn_file" "${DOGANY_LANG}_detail")"
  _detail="$(strip_tickets "$_detail_raw")"
  if [ -n "$_detail" ]; then
    _detail_body="$_detail"
    _detail_len="${#_detail_body}"
    if [ "$_detail_len" -gt 2500 ]; then
      # NOTE: `head -c` truncates by BYTES regardless of locale and can slice
      # a multi-byte Hangul character in half; `cut -c` is a POSIX character
      # count under the UTF-8 locale exported above -- character-safe.
      _detail_body="$(printf '%s\n' "$_detail_body" | cut -c1-2500)"
      _detail_body="${_detail_body}
$(i18n_fmt restart.fold_truncated "…(cut here — ask me if you want the rest)")"
    fi
  fi
  # DGN-785: third section -- "try this" (ko_try / en_try key).
  #   Absent -> section omitted entirely (byte-parity with pre-785 when absent).
  _try_raw="$(user_summary "$_rn_file" "${DOGANY_LANG}_try")"
  _try="$(strip_tickets "$_try_raw")"
  # DGN-829: parse DOGANY_SECTION_GLYPHS (space-separated; empty/unset -> default).
  # Positional: 1st=summary, 2nd=detail, 3rd=try. Fewer than 3 -> bracket fallback.
  # Uses a subshell to avoid clobbering the script's own positional parameters ($@).
  _sg_raw="${DOGANY_SECTION_GLYPHS:-}"
  _sg1="" _sg2="" _sg3=""
  if [ -n "$_sg_raw" ]; then
    # subshell: split raw string, print one token per line, grab first three
    _sg_parsed="$(printf '%s\n' "$_sg_raw" | tr -s '[:space:]' '\n' | grep -v '^$' | head -3)"
    _sg1="$(printf '%s\n' "$_sg_parsed" | sed -n '1p')"
    _sg2="$(printf '%s\n' "$_sg_parsed" | sed -n '2p')"
    _sg3="$(printf '%s\n' "$_sg_parsed" | sed -n '3p')"
  fi
  # default palette when env unset or token absent (owner 2026-08-20: notice
  # sections use the neutral left-triangle to unify with the update-available
  # notice's "▸ Update notes" label; L0 reply section glyphs are a SEPARATE
  # default in bridge/config.py and stay checkmark/pin/clipboard).
  # 형님 확정 2026-08-24: 라벨을 <b>로 감싸고 글리프는 굵은 삼각 ▶.
  # 사유 = 기존 ▸ 는 본문 불릿 • 과 무게가 비슷해 섹션 머리와 항목이 헷갈렸다.
  # 채널 계층은 글리프+굵기로 낸다 (vendors/telegram.md Markup).
  # 참고: bridge/formatting.py:769 `_HEADER_GLYPHS = ("▪","◆","▸")` 가 H1/H2/H3+ 를
  # 매핑한다. 통지 라벨이 쓰던 ▸ 는 그 중 가장 깊은 H3 자리였다(계층 역전).
  # ◆(H2)로 올리는 안을 제시했으나 형님이 ▶ 를 선택 -- 형님 픽이 정본이다.
  #
  # 형님 확정 2026-09-08: ▶ -> ▸ 로 되돌린다 (위 2026-08-24 결정을 대체).
  # 사유 = U+25B6 ▶ 는 유니코드 이모지 목록에 있어 모바일 클라이언트가 컬러
  # 이모지(play button)로 그린다. 고른 값과 화면에 닿는 값이 다르다. 외부
  # 사용자 설치본에서 실측 관측(형님 스크린샷 2026-09-08 19:11).
  # U+25B8 ▸ 는 이모지 목록에 없어 어느 기기에서도 활자로만 렌더된다.
  # 2026-08-24 의 기각 사유(불릿 • 과 무게가 비슷해 헷갈림)는 같은 결정에서
  # 함께 도입된 <b> 라벨 굵기 대비로 해소된 것으로 형님이 판단.
  # 규율: 통지 글리프 기본값은 이모지 프레젠테이션을 갖지 않는 코드포인트만 쓴다.
  [ -z "$_sg1" ] && _sg1="▸"
  [ -z "$_sg2" ] && _sg2="▸"
  [ -z "$_sg3" ] && _sg3="▸"
  # DGN-1758 pack-name (owner 2026-09-27 18:07): a pack/kit-only restart names
  # the product in the header; a framework restart never carries <product>,
  # so its header stays byte-identical to the pre-existing copy.
  # DGN-1814 r2b: header + section labels are the restart.* i18n keys (the
  # locked ko copy lives in config/i18n/ko.json verbatim).
  if [ -n "$_product" ]; then
    _notice="$(i18n_fmt restart.update_done_product "Restart complete · {product} {version} update complete" \
      "product=${_product}" "version=${_vdisp}")"
  else
    _notice="$(i18n_fmt restart.update_done "Restart complete · {version} update complete" "version=${_vdisp}")"
  fi
  _label_summary="<b>${_sg1} $(i18n_fmt restart.fold_summary "At a glance")</b>"
  _label_detail="<b>${_sg2} $(i18n_fmt restart.fold_detail "What changed")</b>"
  _label_try="<b>${_sg3} $(i18n_fmt restart.fold_try "Try this")</b>"
  # DGN-766: all sections render as ONE expandable blockquote (DGN-718 single-
  #   block principle). DGN-785: three sections -- summary / detail / try.
  #   Each section: [label]\n<body>. Sections separated by ONE blank line.
  #   Empty section -> label and body both omitted. All empty -> no blockquote.
  if [ -n "$_notes" ] || [ -n "$_detail" ] || [ -n "$_try" ]; then
    _body=""
    if [ -n "$_notes" ]; then
      _body="${_label_summary}
${_notes}"
    fi
    if [ -n "$_detail" ]; then
      if [ -n "$_body" ]; then
        _body="${_body}

${_label_detail}
${_detail_body}"
      else
        _body="${_label_detail}
${_detail_body}"
      fi
    fi
    if [ -n "$_try" ]; then
      _try_body="$_try"
      if [ -n "$_body" ]; then
        _body="${_body}

${_label_try}
${_try_body}"
      else
        _body="${_label_try}
${_try_body}"
      fi
    fi
    _notice="${_notice}
<blockquote expandable>${_body}</blockquote>"
  fi
  printf '%s\n' "$_notice"
}

# ---------------------------------------------------------------------------
# maybe_restart <rc> <ver> <src_root>  (DGN-685 spec v2 A3)
#   Carry a successful update straight into a bridge restart. Every early
#   return is `return 0` -- under set -e a non-zero return here would clobber
#   the update exit status (grill M1). <src_root> = the tree the update was
#   applied FROM (extracted tag dir or repo checkout); its CHANGELOG.md feeds
#   the DGN-687 restart-complete release-note fold.
# ---------------------------------------------------------------------------
maybe_restart() {
  _rc="$1"
  _ver="$2"
  _src_root="$3"
  [ "$_rc" -eq 0 ] || return 0
  # DGN-1250: update.sh regenerates .claude/settings.json wholesale from the
  # framework hook manifest and has repeatedly dropped instance-local hook
  # wiring in the process (worklog/DGN-1250-framework-update-disarms-
  # instance-gates.md, 4 recurrences). Fires unconditionally on every
  # successful update (before the RESTART gate below, so --no-restart runs
  # still get checked) and never blocks: best-effort, output discarded,
  # loss delivery happens via the instance's cron-queue inject helper
  # inside the child script, not via this call's exit status.
  # SELF-LOSS RESIDUAL (honestly unresolved, DGN-1250): this call lives
  # inside self-update.sh, which is itself framework-synced -- git history
  # shows it rewritten wholesale by "framework update [auto]" commits (e.g.
  # 3a1394d +132 lines, b8178c0 +165/-35 lines, both within the last week).
  # A future upstream rewrite of this function CAN silently drop this call,
  # same disease class as the settings.json wiring it defends. No update-
  # safe attach point currently exists for THIS file's own hook: settings.
  # json hooks are regenerated by the same mechanism (would just move the
  # disease one hop), and settings.local.json is untracked/instance-only so
  # it cannot be shipped via this git commit. Left as an open residual, not
  # silently "solved" -- see worklog/DGN-1250-*.md.
  if [ -f "$SELF_ROOT/routines/wiring-diff-check.py" ]; then
    python3 "$SELF_ROOT/routines/wiring-diff-check.py" --root "$SELF_ROOT" \
      >/dev/null 2>&1 || true
  fi
  [ "$RESTART" -eq 1 ] || return 0
  _rs="$SELF_ROOT/bridge/self_restart.sh"
  if [ ! -x "$_rs" ]; then
    msg "[self-update] 재시작 스크립트 없음/실행불가 -- 재시작 건너뜀 (수동: bridge/self_restart.sh --trigger user)" \
        "[self-update] restart script missing/not executable -- skipping restart (manual: bridge/self_restart.sh --trigger user)"
    return 0
  fi
  _vplain="${_ver#v}"
  _vdisp="v${_vplain}"
  # DGN-1758: one renderer for this path and the /update supervisor.
  _notice="$(restart_notice "$_vdisp" "$_src_root/releases/${_vdisp}.md")"
  if "$_rs" --trigger "$TRIG" --delay 20 --reason "framework self-update to ${_vdisp}" --notice "$_notice"; then
    # DGN-687 notice #2 (install complete -- no release notes here; the
    # completion claim + notes belong to the restart-complete push).
    # DGN-697: short status lines are plain text (no blockquote); only the
    # release-notes fold stays expandable. Relayed verbatim as plain lines.
    printf '%s\n' "[self-update] relay the user notice below VERBATIM as the final line of this turn (DGN-687; do not translate/rewrite/code-block it):"
    # DGN-851: the install-complete line carries the PRODUCT name, which is
    # config data -- resolved from i18n key 'update.installed' ({version}
    # slot) through the shared layered lookup (routines/lib/i18n_lookup.py:
    # kit override > framework default, lang -> en; DGN-1789). DGN-1814 r2b:
    # the in-code fallback (locale file lacks the key, or no python3) is
    # English -- no Korean literal in code; the locked ko copy is
    # config/i18n/ko.json update.installed.
    printf '%s\n' "$(i18n_fmt update.installed "Dogany update installed · {version}
Restarting shortly." "version=${_vdisp}")"
  else
    # Launcher-stage failure/refusal only (exit 3 setup error, or idle-guard
    # deferral on --trigger auto). A detached-worker failure is covered by the
    # worker's own warning push, not here (grill m4).
    msg "[self-update] 재시작이 시작되지 않음(유예 또는 실패) -- 필요 시 수동: bridge/self_restart.sh --trigger user" \
        "[self-update] restart not started (deferred or failed) -- manual if needed: bridge/self_restart.sh --trigger user"
  fi
  return 0
}

# DGN-1758: render-only mode (see header) -- print and stop, touch nothing.
if [ -n "$NOTICE_VER" ]; then
  restart_notice "v${NOTICE_VER#v}" "$NOTICE_FILE" "$NOTICE_PRODUCT"
  exit 0
fi

# 3) Recover the framework repo root from the instance manifest.
#    Read DOGANY_REPO_ROOT without sourcing the whole conf (avoid importing
#    unrelated vars into this shell).
REPO_ROOT="$(sed -n 's/^DOGANY_REPO_ROOT=//p' "$CONF" | head -n1)"
[ -n "$REPO_ROOT" ] || die "DOGANY_REPO_ROOT missing from $CONF"
[ -d "$REPO_ROOT" ] || die "framework repo not found: $REPO_ROOT"
[ -f "$REPO_ROOT/update.sh" ] || die "update.sh not found in repo: $REPO_ROOT"

# DGN-1417: ONE apply door. Where the framework checkout carries
# scripts/pack/update_surface.py (the /update surface) and this instance's
# bridge registers /update, updates are applied only through the /update
# ledger: scripts/pack/update_apply.sh runs the plan in the fixed order
# kit -> pack -> framework, applies a crew's framework to every member in one
# run, and delegates each framework unit back to THIS script with
# DOGANY_UPDATE_DOOR=apply. A direct run here is a second door: one crew
# member moves alone (the DGN-1407 split, 2026-09-12) and the framework can
# land ahead of the kit it reads (kit data silently unread). Refused before
# any fetch or write. --dry-run writes nothing and stays open. An operator
# emergency (bridge down, /update unreachable) passes with a stated reason,
# which is recorded. Since dec-203 (DGN-1816) the public tree carries
# update_surface.py too, so public installs that have /update take the same
# door; an instance installed before /update existed has no other door and
# stays open -- judged from its installed stamp in .instance.conf, not from
# bridge files an unfinished earlier run may already have landed (DGN-1901).
# The check itself lives in routines/lib/update_door.sh, shared
# with the repo's update.sh so `dogany update` cannot bypass it.
# A tree without the /update surface has no door to keep (the lib is only
# required where the surface is).
if [ -f "$REPO_ROOT/scripts/pack/update_surface.py" ]; then
  DOOR_LIB="$SCRIPT_DIR/lib/update_door.sh"
  [ -f "$DOOR_LIB" ] || die "update door check missing: $DOOR_LIB -- refusing (fail-closed, DGN-1417)"
  . "$DOOR_LIB"
  update_door_check "$REPO_ROOT" "$SELF_ROOT" "self-update" "$DRY" "$DIRECT_WHY" || exit 1
fi

# 3b) Resolve the update source (DGN-593 A3).
#     Channel "release": per-run private tag extraction -- the shared repo is
#     only READ (ref read via fetch --tags, object read via git archive);
#     its working tree / HEAD / index are never moved.
#     Channel "main": pull --ff-only, guarded to the main branch.
UPDATE_CHANNEL="${DOGANY_UPDATE_CHANNEL:-$(sed -n 's/^DOGANY_UPDATE_CHANNEL=//p' "$CONF" | head -n1)}"
# DGN-1569: absence is not a declaration. Empty here means NOBODY chose a
# channel (env unset, no .instance.conf line) -- that must never look
# identical to a deliberate DOGANY_UPDATE_CHANNEL=release. Proceed on the
# release default (unchanged effective behavior; the safe channel for a
# non-canary) but say so LOUDLY, naming the instance now and the consumed
# tag once it resolves below. Deliberately a notice, not a refusal: existing
# channel-less instances must keep updating; mint now seeds the declaration
# for every newborn (scripts/mint.sh --channel), so this marks legacy
# instances only.
CHANNEL_DECLARED=1
if [ -z "$UPDATE_CHANNEL" ]; then
  CHANNEL_DECLARED=0
  UPDATE_CHANNEL="release"
  _cd_name="$(sed -n 's/^DOGANY_AGENT_NAME=//p' "$CONF" | head -n1)"
  [ -n "$_cd_name" ] || _cd_name="$(basename "$SELF_ROOT")"
  msg "[self-update][경고] DGN-1569: 인스턴스 '${_cd_name}' 는 업데이트 채널을 선언하지 않았습니다 (env 미설정, $CONF 에 DOGANY_UPDATE_CHANNEL= 라인 없음) -- 기본값 release(안정 태그)로 진행합니다. 채널을 명시 선언하세요." \
      "[self-update][WARN] DGN-1569: instance '${_cd_name}' declares NO update channel (env unset, no DOGANY_UPDATE_CHANNEL= line in $CONF) -- proceeding on the default 'release' (stable tags). Declare the channel explicitly." >&2
fi
if [ -d "$REPO_ROOT/.git" ]; then
  # AUTH PRE-FLIGHT (DGN-604): both channels below hit the network against
  # origin (fetch --tags / pull --ff-only). Probe credentials FIRST so a
  # keychain-locked boot window, an expired gh login, or a stalled OS unlock
  # dialog fails fast with a clear remedy instead of a cryptic error (or a
  # hang) partway through the update. A no-origin repo returns 0 (nothing to
  # probe); a classified failure dies here, before anything is touched.
  preflight_auth_check "$REPO_ROOT" \
    || die "self-update aborted before touching the instance (auth pre-flight failed -- see above). No files changed."
  # DGN-673 B2: pin persistence -- env wins, .instance.conf fallback; loud
  # PINNED banner on every pinned run (exported for the child update.sh).
  resolve_update_pin "$CONF" "$UPDATE_CHANNEL"
  if [ "$UPDATE_CHANNEL" = "main" ]; then
    # Main-branch guard (DGN-593 D3): pull only when the shared repo is
    # actually on main. Detached HEAD / dev branch means a dev session is in
    # progress -- die with a hint, never auto-switch.
    CUR_BRANCH="$(git -C "$REPO_ROOT" symbolic-ref -q --short HEAD || true)"
    if [ "$CUR_BRANCH" != "main" ]; then
      die "shared repo not on main (on '${CUR_BRANCH:-detached}') -- finish the dev session / restore main, then retry: $REPO_ROOT"
    fi
    msg "[self-update] 프레임워크 최신화: git pull --ff-only ($REPO_ROOT, channel=main)" \
        "[self-update] fetching latest framework: git pull --ff-only ($REPO_ROOT, channel=main)"
    git -C "$REPO_ROOT" pull --ff-only \
      || die "git pull --ff-only failed in $REPO_ROOT (resolve manually, then re-run)"
  else
    msg "[self-update] 프레임워크 최신화: 최신 릴리스 태그 ($REPO_ROOT)" \
        "[self-update] fetching latest framework: latest release tag ($REPO_ROOT)"
    # PUBLIC HISTORY REWRITE RE-ANCHOR (DGN-1887), BEFORE the divergence
    # check below: a clone made before the public mirror's history rewrite
    # (DGN-880) holds old v* tags that legitimately differ from origin, and
    # tag_divergence_check would stop the /update button there for good.
    # update.sh's public_rewrite_reanchor (+ _pr_norm_url) acts only under
    # its narrow gate (origin = public mirror, every diverged tag strictly
    # older than the installed version, recorded DOGANY_FW_TAG not among
    # them, instance not inside the old checkout); it then clones fresh into
    # the standard home, repoints DOGANY_REPO_ROOT and prints that path, and
    # this run continues on it (dry-run: a temp clone, no conf change).
    # Same verbatim-port contract as TD_SRC below (dgn1723 A0 pins both
    # functions byte-identical to update.sh). No bash -> refuse.
    PR_SRC="$(cat <<'PR_EOF'
_pr_norm_url() {
  local u="${1%/}"
  u="${u%.git}"; u="${u#https://}"; u="${u#http://}"; u="${u#ssh://}"; u="${u#git@}"
  if [[ "$u" == *github.com:* ]]; then u="${u%%github.com:*}github.com/${u#*github.com:}"; fi
  printf '%s' "$u"
}
public_rewrite_reanchor() {
  local repo="$1" conf="$2" instance="$3" dry="$4" origin_url public_url remote local_refs diverged
  local inst rec_tag lib t cmp home new real_repo real_inst real_home stamp n tmpc
  [ "${DOGANY_REANCHORED:-0}" = "1" ] && return 0
  [ -f "$conf" ] || return 0
  origin_url="$(git -C "$repo" remote get-url origin 2>/dev/null)" || return 0
  public_url="${DOGANY_PUBLIC_REPO_URL:-https://github.com/coolcoolk/dogany-agent}"
  [ "$(_pr_norm_url "$origin_url")" = "$(_pr_norm_url "$public_url")" ] || return 0
  remote="$(git -C "$repo" ls-remote --tags origin 'refs/tags/v*' 2>/dev/null)" || return 0
  local_refs="$(git -C "$repo" for-each-ref --format='%(objectname) %(refname)' 'refs/tags/v*' 2>/dev/null || true)"
  diverged="$(
    { printf '%s\n' "$remote" | awk -F'\t' 'NF==2 && $2 !~ /\^\{\}$/ { print "R", $1, $2 }'
      printf '%s\n' "$local_refs" | awk 'NF==2 { print "L", $1, $2 }'
    } | awk '
      $1 == "R" { r[$3] = $2; next }
      $1 == "L" { l[$3] = $2 }
      END { for (t in l) if ((t in r) && r[t] != l[t]) { sub("^refs/tags/", "", t); print t } }
    ' | LC_ALL=C sort
  )"
  [ -n "$diverged" ] || return 0
  inst="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$conf" | head -n1)"
  rec_tag="$(sed -n 's/^DOGANY_FW_TAG=//p' "$conf" | head -n1)"
  [ -n "$inst" ] || return 0
  if [ -n "$rec_tag" ] && grep -qxF -- "$rec_tag" <<<"$diverged"; then return 0; fi
  lib="$repo/scripts/pack/lib/semver_range.py"
  [ -f "$lib" ] || return 0
  while IFS= read -r t; do
    cmp="$(python3 "$lib" compare "${t#v}" "$inst" 2>/dev/null)" || return 0
    [ "$cmp" = "-1" ] || return 0
  done <<<"$diverged"
  # An instance living INSIDE the old checkout (legacy <repo>/agents/<name>)
  # would move with it: no automatic re-anchor there.
  real_repo="$(cd "$repo" && pwd -P)"
  real_inst="$(cd "$instance" && pwd -P)/"
  [ "${real_inst#"$real_repo"/}" = "$real_inst" ] || return 0

  home="${DOGANY_FRAMEWORK_HOME:-$HOME/.dogany/framework}"
  n="$(printf '%s\n' "$diverged" | wc -l | tr -d ' ')"
  printf '%s\n' "============================================================" >&2
  msg "[update] 공개 저장소 이력 재작성 감지 (DGN-1887): $repo 의 옛 태그 ${n}개가 origin 과 다릅니다 (모두 설치본 v${inst} 보다 낮음, 공개 미러는 버전별 스냅샷만 게시)." \
      "[update] public history rewrite detected (DGN-1887): ${n} old tag(s) in $repo differ from origin (all older than installed v${inst}; the public mirror publishes one snapshot per version)." >&2
  msg "  이 체크아웃의 태그는 건드리지 않고, 새 clone 으로 다시 기준을 잡습니다: $home" \
      "  Leaving this checkout's tags untouched and re-anchoring on a fresh clone: $home" >&2
  printf '%s\n' "============================================================" >&2

  if [ "$dry" = "1" ]; then
    new="$(mktemp -d "${TMPDIR:-/tmp}/dogany-reanchor.XXXXXX")" || die "mktemp failed for the re-anchor preview clone"
    git clone --quiet "$origin_url" "$new" >&2 \
      || { rm -rf "$new"; die "git clone $origin_url failed (re-anchor preview, DGN-1887) -- check the network, then re-run. Nothing was changed."; }
    msg "  [dry-run] 임시 clone 에서 프리뷰합니다 (.instance.conf 변경 없음, 실제 실행은 $home 에 clone 하고 DOGANY_REPO_ROOT 를 바꿉니다)" \
        "  [dry-run] previewing from a temp clone (no .instance.conf change; the real run clones into $home and repoints DOGANY_REPO_ROOT)" >&2
    printf '%s\n' "$new"
    return 0
  fi

  real_home=""
  [ -e "$home" ] && real_home="$(cd "$home" 2>/dev/null && pwd -P || true)"
  stamp="$(date +%Y%m%d%H%M%S)"
  mkdir -p "$(dirname "$home")" || die "cannot create $(dirname "$home") for the re-anchor clone"
  if [ -e "$home" ] && [ "$real_home" != "$real_repo" ]; then
    die "re-anchor (DGN-1887): $home already exists and is not this checkout -- not overwriting it. Point DOGANY_REPO_ROOT in $conf at a fresh clone by hand, then re-run."
  fi
  new="$(mktemp -d "${home}.reanchor.XXXXXX")" || die "mktemp failed next to $home"
  rmdir "$new"
  git clone --quiet "$origin_url" "$new" >&2 \
    || { rm -rf "$new"; die "git clone $origin_url failed (re-anchor, DGN-1887) -- check the network, then re-run. Nothing was changed."; }
  if [ "$real_home" = "$real_repo" ]; then
    # This checkout IS the standard home: keep it beside, never delete it.
    mv "$home" "${home}.pre-rewrite-${stamp}" || { rm -rf "$new"; die "cannot move $home aside for the re-anchor"; }
    msg "  옛 체크아웃 보존: ${home}.pre-rewrite-${stamp}" "  old checkout kept at: ${home}.pre-rewrite-${stamp}" >&2
  fi
  mv "$new" "$home" || die "cannot move the fresh clone into $home"
  home="$(cd "$home" && pwd -P)"
  # Rewrite the conf in place (same inode and mode; no helper from either
  # caller), one DOGANY_REPO_ROOT line.
  tmpc="$(mktemp "${conf}.reanchor.XXXXXX")" || die "cannot repoint DOGANY_REPO_ROOT in $conf (fresh clone is at $home)"
  { sed '/^DOGANY_REPO_ROOT=/d' "$conf" && printf 'DOGANY_REPO_ROOT=%s\n' "$home"; } > "$tmpc" \
    && cat "$tmpc" > "$conf" \
    || { rm -f "$tmpc"; die "cannot repoint DOGANY_REPO_ROOT in $conf (fresh clone is at $home)"; }
  rm -f "$tmpc"
  msg "  DOGANY_REPO_ROOT -> $home" "  DOGANY_REPO_ROOT -> $home" >&2
  printf '%s\n' "$home"
}
PR_EOF
)"
    PR_ROOT="$(DOGANY_LANG="$DOGANY_LANG" bash -c '
      set -euo pipefail
      msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf "%s\n" "$1"; else printf "%s\n" "$2"; fi; }
      die() { msg "[ERROR] $1" "[ERROR] $1" >&2; exit 1; }
      eval "$1"
      public_rewrite_reanchor "$2" "$3" "$4" "$5"
    ' public_rewrite_reanchor "$PR_SRC" "$REPO_ROOT" "$CONF" "$SELF_ROOT" "$DRY")" \
      || die "self-update aborted before fetching tags (public-rewrite re-anchor failed -- see above). No files changed."
    if [ -n "$PR_ROOT" ]; then
      [ -f "$PR_ROOT/update.sh" ] || die "re-anchored clone has no update.sh: $PR_ROOT"
      # Dry-run: PR_ROOT is a private temp clone; the EXIT trap removes it.
      if [ "$DRY" = "1" ]; then
        PR_TMP="$PR_ROOT"
        trap '[ -z "$PR_TMP" ] || rm -rf "$PR_TMP"' EXIT INT TERM
      fi
      REPO_ROOT="$PR_ROOT"
      msg "[self-update] 새 clone 에서 계속합니다: $REPO_ROOT" \
          "[self-update] continuing on the fresh clone: $REPO_ROOT"
    fi
    # TAG DIVERGENCE PRE-CHECK (DGN-1723): a plain `git fetch --tags` refuses
    # to clobber a local v* tag whose object differs from origin's, and
    # rewriting one would silently move the DOGANY_FW_TAG drift baseline.
    # Run update.sh's tag_divergence_check (DGN-1717) BEFORE fetching.
    # It is a VERBATIM port (tests/dgn1723_selfupdate_parity_selftest.sh
    # fails on any byte difference from update.sh's copy): a shared sourced
    # lib would make update.sh depend on a new file that every standalone
    # copy of update.sh (fixtures, old checkouts) lacks. The function is
    # bash and this script is POSIX sh, so it runs in a bash child with the
    # msg/die contract update.sh provides. No bash -> refuse (fail-closed).
    TD_SRC="$(cat <<'TD_EOF'
tag_divergence_check() {
  local repo="$1" conf="$2" remote local_refs diverged n rec_tag
  remote="$(git -C "$repo" ls-remote --tags origin 'refs/tags/v*' 2>/dev/null)" \
    || die "cannot read tags from origin (git ls-remote failed in $repo) -- check the network / credentials, then re-run. Nothing was changed."
  local_refs="$(git -C "$repo" for-each-ref --format='%(objectname) %(refname)' 'refs/tags/v*' 2>/dev/null || true)"
  # ls-remote lists "<oid>\t<ref>" plus a peeled "<ref>^{}" row for annotated
  # tags; the unpeeled row is the object the fetch compares, the same object
  # for-each-ref prints for a local tag.
  diverged="$(
    { printf '%s\n' "$remote" | awk -F'\t' 'NF==2 && $2 !~ /\^\{\}$/ { print "R", $1, $2 }'
      printf '%s\n' "$local_refs" | awk 'NF==2 { print "L", $1, $2 }'
    } | awk '
      $1 == "R" { r[$3] = $2; next }
      $1 == "L" { l[$3] = $2 }
      END { for (t in l) if ((t in r) && r[t] != l[t]) { sub("^refs/tags/", "", t); print t } }
    ' | LC_ALL=C sort
  )"
  [ -n "$diverged" ] || return 0
  n="$(printf '%s\n' "$diverged" | wc -l | tr -d ' ')"
  rec_tag="$(sed -n 's/^DOGANY_FW_TAG=//p' "$conf" 2>/dev/null | head -n1 || true)"
  printf '%s\n' "============================================================" >&2
  msg "[update][오류] 태그 불일치 (DGN-1717): $repo 의 로컬 태그 ${n}개가 origin 의 같은 이름 태그와 다른 객체를 가리킵니다:" \
      "[update][ERROR] tag divergence (DGN-1717): ${n} local tag(s) in $repo point at different objects than origin's same-named tags:" >&2
  printf '  %s\n' $(printf '%s\n' "$diverged" | head -n 20) >&2
  if [ "$n" -gt 20 ]; then printf '  ... (%s more)\n' "$((n - 20))" >&2; fi
  if [ -n "$rec_tag" ] && grep -qxF -- "$rec_tag" <<<"$diverged"; then
    msg "  이 인스턴스가 기록한 DOGANY_FW_TAG=$rec_tag 도 포함됩니다 (콘텐츠 드리프트 기준선)." \
        "  This instance's recorded DOGANY_FW_TAG=$rec_tag is among them (the content-drift baseline)." >&2
  fi
  msg "  일반 'git fetch --tags' 는 이 태그들을 덮어쓰지 않으므로 업데이트 소스를 안전하게 정할 수 없습니다. 아무것도 변경하지 않았습니다." \
      "  A plain 'git fetch --tags' will not overwrite them, so the update source cannot be resolved safely. Nothing was changed." >&2
  msg "  태그를 강제로 가져오거나 삭제해서 넘어가지 마세요: 같은 이름의 태그 내용이 바뀌면 드리프트 기준선이 조용히 바뀝니다." \
      "  Do not force-fetch or delete these tags to get past this: changing what a tag name means locally silently moves the drift baseline." >&2
  msg "  복구: 이 체크아웃은 그대로 두고, 새 위치에 프레임워크 전체를 clone 한 뒤 (git clone <origin URL> <새 위치> -- --depth / --branch 없이: 얕은 단일 태그 clone 은 이후 새 버전을 받지 못합니다, DGN-1947) .instance.conf 의 DOGANY_REPO_ROOT 를 그 경로로 바꾸고," \
      "  Recovery: leave this checkout as it is, make a FULL clone into a NEW directory (git clone <origin URL> <new dir> -- no --depth / --branch: a shallow single-tag clone never receives a later version, DGN-1947), point DOGANY_REPO_ROOT in .instance.conf at it," >&2
  msg "  <새 clone>/update.sh --root <인스턴스> --dry-run 이 통과하면 --dry-run 없이 실행하세요. 위 태그 목록을 관리자에게 알려 주세요." \
      "  then run <new clone>/update.sh --root <instance> --dry-run and, if it passes, the same without --dry-run. Report the tag list above to the maintainers." >&2
  printf '%s\n' "============================================================" >&2
  die "tag divergence with origin (${n} tag(s)) -- update stopped before fetching, nothing changed"
}
TD_EOF
)"
    DOGANY_LANG="$DOGANY_LANG" bash -c '
      set -euo pipefail
      msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf "%s\n" "$1"; else printf "%s\n" "$2"; fi; }
      die() { msg "[ERROR] $1" "[ERROR] $1" >&2; exit 1; }
      eval "$1"
      tag_divergence_check "$2" "$3"
    ' tag_divergence_check "$TD_SRC" "$REPO_ROOT" "$CONF" \
      || die "self-update aborted before fetching tags (tag divergence check failed -- see above). No files changed."
    # DGN-1947: a shallow / single-tag clone is widened before the fetch
    # (update.sh fw_clone_shape_heal, verbatim; dry-run changes nothing).
    fw_clone_shape_heal "$REPO_ROOT" "$DRY"
    git -C "$REPO_ROOT" fetch --tags origin \
      || die "git fetch failed in $REPO_ROOT (resolve manually, then re-run)"
    # Channel-aware target tag (DGN-621 v2 phase1, DGN-626 W1): release =
    # stable only; dev = stable|-dev.N whitelist; staging = stable|-rc.N
    # whitelist; unknown channel dies loud; DOGANY_UPDATE_PIN overrides (fails
    # loud if the pinned tag is missing). Stable-only estate on release == old
    # one-liner.
    LATEST_TAG="$(resolve_channel_tag "$REPO_ROOT" "$UPDATE_CHANNEL")"
    [ -n "$LATEST_TAG" ] || die "no release tag (v*) found in $REPO_ROOT"
    # DGN-1569 second half of the undeclared notice: name the exact tag a
    # channel nobody chose is about to consume.
    if [ "$CHANNEL_DECLARED" = "0" ]; then
      msg "[self-update][경고] DGN-1569: 선언되지 않은 채널이 지금 소비하려는 태그: ${LATEST_TAG} (기본값 release)" \
          "[self-update][WARN] DGN-1569: the undeclared channel is about to follow tag ${LATEST_TAG} (default release)" >&2
    fi
    # -----------------------------------------------------------------------
    # RUN-LEVEL DOWNGRADE GUARD (DGN-1136) -- wired HERE, right after the
    # target tag is fixed and BEFORE any extraction work. The channel/pin
    # resolver above hands back whatever the channel math says; without this
    # check nothing compares that candidate against what is ALREADY installed,
    # so a channel whose top sits BEHIND this instance (subscriber drift)
    # silently downgrades the whole tree -- and update.sh's REVERSE-DRIFT
    # GUARD (DGN-249) is per-FILE (SKIP+WARN, run continues), which turns a
    # downgrade run into a TORN tree with a lying version stamp. That guard
    # stays; this is the RUN-level layer above it: candidate < installed
    # refuses the WHOLE run. Same intent as the fleet planner's
    # BLOCKED-DRIFT predicate (scripts/pack/update_plan.sh: _ver_ok
    # "$cand" ">=$fw"), via the SAME single-source comparator
    # (scripts/pack/lib/semver_range.py -- no new semver copy). DGN-1723:
    # on its PRECEDENCE lane (`compare`, pre-release aware) with the same
    # -1/0/1 contract and DOGANY_FW_TAG exact-build rule as update.sh's
    # fw_downgrade_guard. Fires regardless of channel AND pin -- a pin is a target
    # selector, not downgrade consent. Escape hatch: DOGANY_ROLLBACK=1 (the
    # update.sh rollback lane, DGN-673 B3 -- same variable, no second
    # ledger) demotes the refusal to a loud warning and proceeds; update.sh's
    # own per-file consent (DOGANY_ROLLBACK_ACK) still applies downstream.
    # Every unknown is fail-CLOSED (missing DOGANY_FW_VERSION line,
    # unparseable version on either side, missing comparator): "don't know,
    # burn anyway" is the exact defect shape this guard exists to kill.
    # -----------------------------------------------------------------------
    DG_CAND_VER="${LATEST_TAG#v}"
    DG_INST_VER="$(sed -n 's/^DOGANY_FW_VERSION=//p' "$CONF" | head -n1)"
    DG_INST_TAG="$(sed -n 's/^DOGANY_FW_TAG=//p' "$CONF" | head -n1)"
    DG_PIN_DISP="${DOGANY_UPDATE_PIN:-none}"
    if [ -z "$DG_INST_VER" ]; then
      msg "[self-update][오류] 하향 가드 (DGN-1136): $CONF 에 DOGANY_FW_VERSION 라인이 없습니다 -- 후보 ${LATEST_TAG} (채널=${UPDATE_CHANNEL}, 핀=${DG_PIN_DISP}) 가 하향이 아님을 증명할 수 없어 중단합니다 (fail-closed)" \
          "[self-update][ERROR] downgrade guard (DGN-1136): DOGANY_FW_VERSION not found in $CONF -- cannot prove candidate ${LATEST_TAG} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}) is not a downgrade, refusing (fail-closed)" >&2
      msg "  조치: .instance.conf 의 DOGANY_FW_VERSION=<X.Y.Z> 라인을 복원한 뒤 재실행하세요." \
          "  action: restore the DOGANY_FW_VERSION=<X.Y.Z> line in .instance.conf, then re-run." >&2
      die "downgrade guard (DGN-1136): DOGANY_FW_VERSION missing -- fail-closed"
    fi
    DG_SEMVER_LIB="$REPO_ROOT/scripts/pack/lib/semver_range.py"
    if [ ! -f "$DG_SEMVER_LIB" ]; then
      die "downgrade guard (DGN-1136): semver comparator not found ($DG_SEMVER_LIB) -- cannot prove candidate ${LATEST_TAG} is not a downgrade of installed v${DG_INST_VER}, refusing (fail-closed; bring the shared checkout forward so scripts/pack/lib/semver_range.py exists, then re-run)"
    fi
    # Exact installed build (DGN-1723, same rule as update.sh
    # fw_downgrade_guard): a -dev.N / -rc.N tag's VERSION file carries only
    # X.Y.Z, so DOGANY_FW_VERSION never holds a pre-release. The landing
    # stamp DOGANY_FW_TAG names the exact build -- used only while its X.Y.Z
    # still equals DOGANY_FW_VERSION (otherwise it is stale and ignored).
    DG_INST_LABEL="v${DG_INST_VER}"
    case "$DG_INST_TAG" in
      v[0-9]*)
        _dg_t="${DG_INST_TAG#v}"
        if [ "${_dg_t%%[-+]*}" = "${DG_INST_VER%%[-+]*}" ]; then
          DG_INST_VER="$_dg_t"; DG_INST_LABEL="$DG_INST_TAG (DOGANY_FW_TAG)"
        fi ;;
    esac
    # Precedence lane (SemVer 2.0 section 11, pre-release aware):
    # `compare <cand> <installed>` prints -1 / 0 / 1, exit 2 on an
    # unparseable side. The range lane (`satisfies`) prefix-parses X.Y.Z and
    # read 2.6.0-dev.9 == 2.6.0, so a dev build over its own release passed.
    dg_rc=0
    dg_cmp="$(python3 "$DG_SEMVER_LIB" compare "$DG_CAND_VER" "$DG_INST_VER" 2>/dev/null)" || dg_rc=$?
    [ "$dg_rc" = "0" ] || dg_cmp="error"
    # Installed build known only as X.Y.Z (no usable DOGANY_FW_TAG) and the
    # candidate is a pre-release of that SAME X.Y.Z: the order is
    # unknowable (the install may itself be any -dev.N of it) -> proceed
    # with a notice, exactly as update.sh does.
    if [ "$dg_cmp" = "-1" ] && [ "$DG_INST_VER" = "${DG_INST_VER%%[-+]*}" ] \
       && [ "$DG_INST_LABEL" = "v$DG_INST_VER" ] && [ "${DG_CAND_VER%%[-+]*}" = "$DG_INST_VER" ]; then
      msg "[self-update] installed v${DG_INST_VER} has no exact build tag (DOGANY_FW_TAG) -- same-X.Y.Z pre-release ${LATEST_TAG} is not judged a downgrade." \
          "[self-update] installed v${DG_INST_VER} has no exact build tag (DOGANY_FW_TAG) -- same-X.Y.Z pre-release ${LATEST_TAG} is not judged a downgrade."
      dg_cmp=0
    fi
    case "$dg_cmp" in
      0|1)
        # candidate >= installed: forward update or same-version re-apply --
        # both are normal use; proceed untouched.
        ;;
      -1)
        if [ "${DOGANY_ROLLBACK:-0}" = "1" ]; then
          printf '%s\n' "============================================================" >&2
          msg "[self-update][경고] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- 하향 진행: 설치본 ${DG_INST_LABEL} -> 후보 ${LATEST_TAG} (채널=${UPDATE_CHANNEL}, 핀=${DG_PIN_DISP})" \
              "[self-update][WARN] ROLLBACK MODE (DOGANY_ROLLBACK=1) -- proceeding with DOWNGRADE: installed ${DG_INST_LABEL} -> candidate ${LATEST_TAG} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP})" >&2
          msg "  update.sh 의 파일 단위 롤백 동의(DOGANY_ROLLBACK_ACK)는 하위 단계에서 계속 적용됩니다." \
              "  update.sh's per-file rollback consent (DOGANY_ROLLBACK_ACK) still applies downstream." >&2
          printf '%s\n' "============================================================" >&2
        else
          printf '%s\n' "============================================================" >&2
          msg "[self-update][오류] 하향 거부 (DGN-1136) -- 업데이트 후보가 설치본보다 낮습니다" \
              "[self-update][ERROR] DOWNGRADE REFUSED (DGN-1136) -- update candidate is OLDER than the installed framework" >&2
          msg "  설치본: ${DG_INST_LABEL}  |  후보: ${LATEST_TAG}  |  채널: ${UPDATE_CHANNEL}  |  핀: ${DG_PIN_DISP}" \
              "  installed: ${DG_INST_LABEL}  |  candidate: ${LATEST_TAG}  |  channel: ${UPDATE_CHANNEL}  |  pin: ${DG_PIN_DISP}" >&2
          msg "  원인: 해석된 업데이트 목적지(채널 top 또는 핀)가 설치본 뒤에 있습니다 (구독원 표류)." \
              "  cause: the resolved update target (channel top or pin) sits BEHIND the installed version (subscriber drift)." >&2
          msg "  조치: 채널/핀 선언을 점검하세요. 의도적 롤백이라면 DOGANY_ROLLBACK=1 로 재실행하세요 (update.sh 롤백 레인; 파일별 동의는 별도로 요구됩니다)." \
              "  action: check the channel/pin declaration. For a DELIBERATE rollback re-run with DOGANY_ROLLBACK=1 (update.sh rollback lane; per-file consent is still required downstream)." >&2
          printf '%s\n' "============================================================" >&2
          die "downgrade refused (DGN-1136): candidate ${LATEST_TAG} < installed ${DG_INST_LABEL} (channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}) -- set DOGANY_ROLLBACK=1 only for a deliberate rollback"
        fi
        ;;
      *)
        # exit 2 = comparator parse error (either side not X.Y.Z[-pre]);
        # anything else (e.g. 127 = no python3, or a checkout whose
        # semver_range.py predates the compare lane) is equally "cannot judge".
        # Deliberately NOT bypassable by DOGANY_ROLLBACK: rollback consents
        # to a KNOWN downgrade, not to an unjudgeable one.
        die "downgrade guard (DGN-1136): cannot compare versions (candidate '${DG_CAND_VER}' vs installed '${DG_INST_VER}', comparator rc=${dg_rc}) -- not provably a forward update, refusing (fail-closed; channel=${UPDATE_CHANNEL}, pin=${DG_PIN_DISP}; both sides must be X.Y.Z[-pre] semver)"
        ;;
    esac
    # DGN-626 W1: hand the consumed tag NAME to the --no-pull child (the
    # extracted tree carries no .git, so the child cannot re-derive which
    # tag it came from) -- consumed by the DOGANY_FW_TAG landing stamp.
    DOGANY_UPDATE_SRC_TAG="$LATEST_TAG"
    export DOGANY_UPDATE_SRC_TAG
    # Per-run private source (DGN-593 A3): extract the tag into a temp dir.
    # The trap is installed IMMEDIATELY after mktemp, before any die below,
    # so the temp source can never leak (EXIT covers normal exit and die;
    # INT/TERM cover signals).
    SRC="$(mktemp -d "${TMPDIR:-/tmp}/dogany-src.XXXXXX")" \
      || die "mktemp failed for release source extraction"
    trap 'rm -rf "$SRC"; [ -z "$PR_TMP" ] || rm -rf "$PR_TMP"' EXIT INT TERM
    # Two-step extraction (sh has no pipefail -> no pipe; check each step).
    git -C "$REPO_ROOT" archive --format=tar "$LATEST_TAG" > "$SRC/src.tar" \
      || die "git archive $LATEST_TAG failed in $REPO_ROOT"
    tar -xf "$SRC/src.tar" -C "$SRC" \
      || die "tar extraction of $LATEST_TAG failed"
    rm -f "$SRC/src.tar"
    # Extraction verification gate: never consume a half-extracted tree.
    if [ ! -f "$SRC/update.sh" ] || [ ! -d "$SRC/agents/.template" ]; then
      die "extracted source incomplete ($LATEST_TAG): $SRC"
    fi
    msg "[self-update] 릴리스 소스 추출: $LATEST_TAG" \
        "[self-update] release source extracted: $LATEST_TAG"
    # 4-release) Self-targeted, non-interactive update from the EXTRACTED
    #    tree. Child call (never exec -- exec would kill the EXIT trap and
    #    leak the temp dir); the child's exit status passes through.
    msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
        "[self-update] updating this instance: $SELF_ROOT"
    rc=0
    # DGN-603 SF1: invoke via `bash` (not the extracted file's exec bit) so a
    # noexec TMPDIR mount cannot block the extracted update.sh from running.
    bash "$SRC/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@" || rc=$?
    # DGN-1513 SPEC v2 SS2.1: advance the consume checkout to the SAME tag,
    # right after the child update.sh succeeds -- reuses $LATEST_TAG, no new
    # version logic. No-op (silent) when DOGANY_CONSUME_ROOT is undeclared.
    advance_consume_checkout "$rc" "$LATEST_TAG"
    # DGN-685: successful update -> restart wiring (before the EXIT trap
    # cleans $SRC, so the release notes are still readable).
    maybe_restart "$rc" "$LATEST_TAG" "$SRC"
    exit "$rc"
  fi
else
  msg "[self-update] .git 없음 -> pull 건너뜀 (로컬 체크아웃 사용)" \
      "[self-update] no .git -> skipping pull (using local checkout)"
fi

# 4) main channel / no-.git: self-targeted, non-interactive update from the
#    repo checkout. --no-pull because we already synced above. Child call
#    (no exec, see step 4 note in the header); exit status passes through.
msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
    "[self-update] updating this instance: $SELF_ROOT"
rc=0
"$REPO_ROOT/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@" || rc=$?
# DGN-1513 SPEC v2 SS2.1: main channel -- advance the consume checkout to the
# shared checkout's just-pulled HEAD commit (no tag exists on this channel).
# No .git at all (the else-branch above) leaves this empty, which the
# function itself treats as "no target" and reports rather than guessing.
MAIN_HEAD=""
[ -d "$REPO_ROOT/.git" ] && MAIN_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
advance_consume_checkout "$rc" "$MAIN_HEAD"
# DGN-685: main / no-.git channel -- the just-pulled checkout IS the new
# version, so its VERSION file names what we updated to (grill m1).
MAIN_VER="$(head -n1 "$REPO_ROOT/VERSION" 2>/dev/null || true)"
maybe_restart "$rc" "${MAIN_VER:-unknown}" "$REPO_ROOT"
exit "$rc"
