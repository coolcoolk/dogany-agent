#!/usr/bin/env bash
# get.sh -- Dogany one-line install entry (DGN-1288).
#
# The single command a user pastes (hosting face pending -- see
# docs/proposals/dgn-1288/00-README.md section 2):
#
#   curl -fsSL https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/get.sh | bash
#
# What it does, in order:
#   1. make sure git exists (macOS: Command Line Tools GUI dialog; Linux: the
#      detected package manager -- apt-get/dnf/yum/pacman/zypper/apk; when
#      nothing matches, one dead-end block that PRINTS the two commands it is
#      asking for rather than describing them)
#   2. land / refresh the framework clone at ~/.dogany/framework (DGN-384)
#   3. hand off to install.sh with stdin re-attached to the terminal
#
# The user never sees, chooses, or navigates to a download location: the
# clone IS the install home, fixed at ~/.dogany/framework. Re-running the
# same one-liner is always safe: existing clone -> fetch (fail-open when
# offline) -> install.sh re-run; install.sh's own same-root pass-through
# (DGN-227 MAJOR-3 case b) absorbs an existing instance.
#
# Self-containment: this file is fetched and run BEFORE the repo exists, so
# it sources nothing and duplicates the minimal msg/lang helpers from
# install.sh on purpose. Everything lives in functions and the LAST line is
# the only top-level call: a truncated download parses as a prefix of
# function definitions and executes nothing (curl|bash partial-read
# hardening, rustup/homebrew pattern).
#
# stdin: under `curl | bash` stdin is the (exhausted) pipe. install.sh has 7
# `-t 0` gates and abort_on_eof for exactly that state, so the handoff
# re-attaches /dev/tty. When no tty exists at all the only pass-through is
# --dry-run (which never reads input); anything else aborts with one line.
#
# ASCII-only code + comments. bash 3.2 compatible (macOS stock; no arrays
# needed here, no mapfile, no assoc arrays). set -u only: every external
# command is explicitly guarded -- under curl|bash, -e would turn a guarded
# probe into a silent death with no message.
set -u

# --- constants (env-overridable for tests / hosting changes) ---------------
# DOGANY_GET_REPO_URL   where the framework is cloned from. The default is
#                       the public repo; the selftest points it at a local
#                       fixture. Changing hosting = changing this default.
# DOGANY_FRAMEWORK_HOME the install home. DGN-384 settled ~/.dogany/framework;
#                       the override exists for sandboxed tests only.
GET_REPO_URL="${DOGANY_GET_REPO_URL:-https://github.com/coolcoolk/dogany-agent}"
FRAMEWORK_HOME="${DOGANY_FRAMEWORK_HOME:-$HOME/.dogany/framework}"

# How long to wait for the macOS Command Line Tools GUI install before giving
# up (seconds). The download is user-network-bound; 30 min is the ceiling,
# not the expectation. Poll every 5s.
CLT_WAIT_MAX=1800

# ===========================================================================
# USER-FACING STRINGS -- DRAFT, PENDING OWNER APPROVAL (DGN-1288)
# ===========================================================================
# Single review surface: every string the installing user can see from this
# script lives in THIS block and nowhere else in this file. The same block is
# reproduced verbatim in docs/proposals/dgn-1288/00-README.md section 5 for
# the owner review. These are proposals, not decisions -- nothing user-visible
# ships before the owner approves that section.
# ===========================================================================
S_START_KO="Dogany 설치를 시작합니다."
S_START_EN="Starting the Dogany install."
S_CLT_DIALOG_KO="git 준비를 위해 macOS 명령어 도구 설치 창이 뜹니다. '설치'를 눌러주세요."
S_CLT_DIALOG_EN="A macOS dialog will appear to install the command line tools (needed for git). Click 'Install'."
S_CLT_WAIT_KO="도구 설치가 끝나기를 기다리는 중"
S_CLT_WAIT_EN="Waiting for the tools install to finish"
S_CLT_FAIL_KO="명령어 도구 설치를 확인하지 못했습니다. 설치 창을 완료한 뒤 아래 명령을 다시 실행해 주세요:"
S_CLT_FAIL_EN="Could not confirm the command line tools install. Finish the install dialog, then run this again:"
S_SUDO_NOTE_KO="git 설치를 위해 sudo 를 사용합니다. 비밀번호 입력창이 뜰 수 있습니다."
S_SUDO_NOTE_EN="About to install git with sudo; a password prompt may appear."
S_GIT_MANUAL_CMD_KO="git 이 필요합니다. 아래 명령으로 설치해 주세요:"
S_GIT_MANUAL_CMD_EN="git is required. Install it with:"
S_GIT_MANUAL_PKG_KO="git 이 필요합니다. 배포판 패키지 관리자로 'git' 패키지를 설치해 주세요."
S_GIT_MANUAL_PKG_EN="git is required. Install the 'git' package with your distro's package manager."
S_RERUN_KO="그런 다음 아래 명령을 다시 실행해 주세요:"
S_RERUN_EN="Then run this again:"
S_CHECKING_KO="받아둔 파일을 확인하는 중..."
S_CHECKING_EN="Checking the existing download..."
S_OFFLINE_KO="네트워크에 연결할 수 없어, 이미 받아둔 버전으로 계속합니다."
S_OFFLINE_EN="Could not reach the network -- continuing with the copy already downloaded."
S_ORIGIN_DIFF_KO="설치 폴더에 직접 받아두신 사본이 있습니다. 덮어쓰지 않고 그대로 사용합니다."
S_ORIGIN_DIFF_EN="The install folder holds a copy you fetched yourself. Using it as-is, not overwriting it."
S_BROKEN_MOVED_KO="설치 폴더에 완성되지 않은 파일이 있어 아래 이름으로 옮겼습니다 (지우지 않았습니다):"
S_BROKEN_MOVED_EN="The install folder held an unfinished download; renamed it to this (nothing deleted):"
S_DOWNLOADING_KO="Dogany 를 내려받는 중..."
S_DOWNLOADING_EN="Downloading Dogany..."
S_DOWNLOAD_FAIL_KO="내려받지 못했습니다. 네트워크 연결을 확인한 뒤 아래 명령을 다시 실행해 주세요:"
S_DOWNLOAD_FAIL_EN="Download failed. Check your network connection, then run this again:"
S_NO_TTY_KO="설치 마법사는 대화형 터미널이 필요합니다. 터미널에서 직접 실행해 주세요."
S_NO_TTY_EN="The install wizard needs an interactive terminal. Please run this in a terminal."
# ===========================================================================
# END USER-FACING STRINGS
# ===========================================================================

# --- minimal helpers (mirrors install.sh detect_lang / msg, on purpose) ----
GET_LANG="${DOGANY_LANG:-}"
get_detect_lang() {
  if [ -n "$GET_LANG" ]; then return 0; fi
  case "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" in
    *ko*|*ko_KR*|*Korean*) GET_LANG="ko" ;;
    *) GET_LANG="en" ;;
  esac
}

# msg "ko" "en" -- print the localized line.
msg() {
  if [ "$GET_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi
}

os_kind() {
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'macos' ;;
    *)      printf 'linux' ;;
  esac
}

# --- dead-end helpers -----------------------------------------------------
# rerun_cmd: the exact command that restarts this install, DERIVED from
# GET_REPO_URL so a hosting change moves one constant and every dead-end
# message follows. Phase 1 told the user to "paste the same command again",
# which assumes the one-liner is still on screen -- after a scroll or a
# cleared terminal it is not, and the instruction pointed at nothing
# (DGN-1288 phase 2, section B).
rerun_cmd() {
  local u
  u="$(norm_url "$GET_REPO_URL")"
  case "$u" in
    https://github.com/*)
      printf 'curl -fsSL https://raw.githubusercontent.com/%s/main/get.sh | bash' \
        "${u#https://github.com/}" ;;
    *github.com:*)   # scp-form ssh remote (git@github.com:owner/repo)
      printf 'curl -fsSL https://raw.githubusercontent.com/%s/main/get.sh | bash' \
        "${u##*github.com:}" ;;
    http://*|https://*)
      printf 'curl -fsSL %s/get.sh | bash' "$u" ;;
    *)               # local checkout / fixture path: run it in place
      printf 'bash %s/get.sh' "$u" ;;
  esac
}

# tilde_path: collapse the user's home prefix to ~ for DISPLAY only. Paths we
# print are read by a human, and the absolute form spells out the account name
# with no added information -- ~/.dogany/... is shorter and says the same
# thing. Never used for a path we then operate on.
tilde_path() {
  case "$1" in
    "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;;
    *)         printf '%s' "$1" ;;
  esac
}

# print_rerun: that command on its own indented line, ready to copy. Always to
# stderr -- every caller is a failure path.
print_rerun() {
  printf '    %s\n' "$(rerun_cmd)" >&2
}

# --- linux package manager ladder -----------------------------------------
# Deliberate twin of install.sh's pkg_install ladder: get.sh runs BEFORE the
# repo exists, so it cannot source it. Same managers, same detection order
# (dnf before yum -- `yum` is a dnf shim on Fedora/RHEL8+), same non-
# interactive flags. Only git is ever installed from here, and every one of
# these managers spells it "git", so get.sh needs no package-name table (the
# name that diverges is python's, which only install.sh installs).
GET_PKG_MGR=""
GET_PKG_SUDO=""

get_detect_pkg_mgr() {
  local m
  for m in apt-get dnf yum pacman zypper apk; do
    if command -v "$m" >/dev/null 2>&1; then GET_PKG_MGR="$m"; return 0; fi
  done
  return 1
}

# Sets GET_PKG_SUDO to "sudo", or to "" when we are ALREADY root (minimal
# container images run as root and often carry no sudo binary at all).
# Callers test the RETURN, not the emptiness: "" is a success value.
get_pkg_can_elevate() {
  GET_PKG_SUDO=""
  local uid
  uid="$(id -u 2>/dev/null || true)"
  [ "$uid" = "0" ] && return 0
  if command -v sudo >/dev/null 2>&1; then GET_PKG_SUDO="sudo"; return 0; fi
  return 1
}

# Metadata refresh; empty for managers that refresh per transaction (dnf/yum).
# pacman gets -Sy only: a full -Syu system upgrade is not ours to run.
get_pkg_update_cmd() {
  case "$GET_PKG_MGR" in
    apt-get) printf 'apt-get update' ;;
    apk)     printf 'apk update' ;;
    zypper)  printf 'zypper --non-interactive refresh' ;;
    pacman)  printf 'pacman -Sy' ;;
  esac
}

get_pkg_install_cmd() {
  case "$GET_PKG_MGR" in
    apt-get) printf 'apt-get install -y' ;;
    dnf)     printf 'dnf install -y' ;;
    yum)     printf 'yum install -y' ;;
    pacman)  printf 'pacman -S --needed --noconfirm' ;;
    zypper)  printf 'zypper --non-interactive install' ;;
    apk)     printf 'apk add' ;;
  esac
}

# The copyable "install git yourself" line for THIS machine, empty when no
# known manager is present. The sudo prefix here is about the shell the user
# will type into, so it depends only on whether they are root.
get_pkg_manual_cmd() {
  [ -n "$GET_PKG_MGR" ] || return 0
  local pfx="sudo " uid
  uid="$(id -u 2>/dev/null || true)"
  [ "$uid" = "0" ] && pfx=""
  printf '%s%s git' "$pfx" "$(get_pkg_install_cmd)"
}

# --- 1. git ---------------------------------------------------------------
# git_ok: git exists AND is runnable. On macOS the stock /usr/bin/git is a
# shim that pops the CLT GUI dialog when the tools are missing -- probing it
# with `git --version` would trigger the dialog outside our messaging, so
# when the resolved git IS the shim we require `xcode-select -p` (CLT or full
# Xcode present) before trusting it. A non-shim git (brew, etc.) is trusted
# as-is.
git_ok() {
  command -v git >/dev/null 2>&1 || return 1
  if [ "$(os_kind)" = "macos" ] && [ "$(command -v git)" = "/usr/bin/git" ]; then
    xcode-select -p >/dev/null 2>&1 || return 1
  fi
  return 0
}

ensure_git() {
  git_ok && return 0
  if [ "$(os_kind)" = "macos" ]; then
    msg "$S_CLT_DIALOG_KO" "$S_CLT_DIALOG_EN"
    # Spawns the GUI installer and returns immediately; the user's only input
    # is clicking 'Install' in the dialog (a GUI yes/no -- within the
    # enter/y-n/arrows standard). Poll until git works or the ceiling hits.
    xcode-select --install >/dev/null 2>&1 || true
    local elapsed=0 tty=0
    [ -t 1 ] && tty=1
    while ! git_ok; do
      if [ "$elapsed" -ge "$CLT_WAIT_MAX" ]; then
        [ "$tty" = "1" ] && printf '\n'
        msg "$S_CLT_FAIL_KO" "$S_CLT_FAIL_EN" >&2
        print_rerun
        return 1
      fi
      sleep 5; elapsed=$((elapsed + 5))
      if [ "$tty" = "1" ]; then
        if [ "$GET_LANG" = "ko" ]; then
          printf '\r\033[2K  %s... %ds' "$S_CLT_WAIT_KO" "$elapsed"
        else
          printf '\r\033[2K  %s... %ds' "$S_CLT_WAIT_EN" "$elapsed"
        fi
      fi
    done
    [ "$tty" = "1" ] && printf '\r\033[2K'
    return 0
  fi
  # Linux: detected-manager ladder (apt-get/dnf/yum/pacman/zypper/apk). Phase 1
  # covered apt only, so every other distro hit the manual line -- the deferred
  # defect this phase closes (DGN-1288 phase 2, section A).
  local upd mcmd
  if get_detect_pkg_mgr && get_pkg_can_elevate; then
    msg "$S_SUDO_NOTE_KO" "$S_SUDO_NOTE_EN"
    upd="$(get_pkg_update_cmd)"
    # Word splitting on these is deliberate: they are our OWN literals from the
    # case tables above, never user input.
    if [ -z "$upd" ] || $GET_PKG_SUDO $upd; then
      if $GET_PKG_SUDO $(get_pkg_install_cmd) git; then
        git_ok && return 0
      fi
    fi
  fi
  # Still a dead end (unknown manager, no root, or the install itself failed) --
  # but the shortest one available: what to install, the command that installs
  # it here, and the command that resumes the install.
  mcmd="$(get_pkg_manual_cmd)"
  if [ -n "$mcmd" ]; then
    msg "$S_GIT_MANUAL_CMD_KO" "$S_GIT_MANUAL_CMD_EN" >&2
    printf '    %s\n' "$mcmd" >&2
  else
    msg "$S_GIT_MANUAL_PKG_KO" "$S_GIT_MANUAL_PKG_EN" >&2
  fi
  msg "$S_RERUN_KO" "$S_RERUN_EN" >&2
  print_rerun
  return 1
}

# --- 2. land / refresh the clone ------------------------------------------
# Normalize a git URL/path for origin comparison: strip a trailing slash and
# a trailing .git so mirror-vs-canonical spellings of the same repo match.
norm_url() {
  printf '%s' "$1" | sed -e 's|/$||' -e 's|\.git$||'
}

fresh_clone() {
  local tmp="$FRAMEWORK_HOME.tmp.$$" d
  # Stale tmp dirs from a previous interrupted run (different pid) -- sweep.
  for d in "$FRAMEWORK_HOME".tmp.*; do
    [ -e "$d" ] && rm -rf "$d"
  done
  msg "$S_DOWNLOADING_KO" "$S_DOWNLOADING_EN"
  mkdir -p "$(dirname "$FRAMEWORK_HOME")"
  # Clone into a tmp sibling, then mv into place: a killed/failed clone never
  # leaves a half-written tree AT the install home, so the next run's state
  # machine only ever sees complete-clone / absent / (legacy) non-git dir.
  # Native git progress stays attached to the terminal (DGN-164 discipline:
  # long downloads never sit silent).
  if git clone "$GET_REPO_URL" "$tmp"; then
    mv "$tmp" "$FRAMEWORK_HOME"
    return 0
  fi
  rm -rf "$tmp"
  msg "$S_DOWNLOAD_FAIL_KO" "$S_DOWNLOAD_FAIL_EN" >&2
  print_rerun
  return 1
}

ensure_clone() {
  if [ -e "$FRAMEWORK_HOME/.git" ]; then
    # Existing clone. Refresh tags/objects when it is OUR repo; a foreign
    # origin (developer checkout parked at the canonical path) is used as-is
    # -- never clobbered, never fetched into.
    local origin
    origin="$(git -C "$FRAMEWORK_HOME" config --get remote.origin.url 2>/dev/null || true)"
    if [ -n "$origin" ] && [ "$(norm_url "$origin")" = "$(norm_url "$GET_REPO_URL")" ]; then
      msg "$S_CHECKING_KO" "$S_CHECKING_EN"
      if ! git -C "$FRAMEWORK_HOME" fetch --tags --quiet origin 2>/dev/null; then
        # Fail-open: offline re-run proceeds on the local copy; install.sh's
        # tag pin (DGN-221) works from local tags.
        msg "$S_OFFLINE_KO" "$S_OFFLINE_EN"
      fi
    else
      msg "$S_ORIGIN_DIFF_KO" "$S_ORIGIN_DIFF_EN"
    fi
    return 0
  fi
  if [ -e "$FRAMEWORK_HOME" ]; then
    # Present but not a git repo: an interrupted legacy download or a stray
    # user dir. Empty -> just remove; non-empty -> move aside (never delete
    # user bytes), then clone fresh.
    if rmdir "$FRAMEWORK_HOME" 2>/dev/null; then
      :
    else
      local aside
      aside="$FRAMEWORK_HOME.broken.$(date +%Y%m%d-%H%M%S)"
      mv "$FRAMEWORK_HOME" "$aside"
      msg "$S_BROKEN_MOVED_KO" "$S_BROKEN_MOVED_EN"
      # Own indented line, same shape as print_rerun: the sentence above says
      # "below", so the path must actually BE below it, not trailing on the
      # same line. Home-collapsed -- the absolute form leaks the account name
      # for no benefit (owner, 2026-09-09: "여기서 내 이름 왜 나와").
      printf '    %s\n' "$(tilde_path "$aside")"
    fi
  fi
  fresh_clone
}

# --- 3. hand off to install.sh --------------------------------------------
handoff() {
  local inst="$FRAMEWORK_HOME/install.sh"
  if [ ! -f "$inst" ]; then
    # Complete clone without install.sh = corrupt/foreign content. Move it
    # aside so the NEXT paste of the same one-liner re-clones cleanly.
    mv "$FRAMEWORK_HOME" "$FRAMEWORK_HOME.broken.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    msg "$S_DOWNLOAD_FAIL_KO" "$S_DOWNLOAD_FAIL_EN" >&2
    print_rerun
    return 1
  fi
  if [ -t 0 ]; then
    exec bash "$inst" "$@"
  fi
  # Piped stdin (curl | bash): re-attach the terminal so install.sh's
  # interactive prompts read from the user, not the exhausted pipe.
  if ( : </dev/tty ) 2>/dev/null; then
    exec bash "$inst" "$@" </dev/tty
  fi
  # No terminal at all: only --dry-run may proceed (it never reads input).
  case " $* " in
    *" --dry-run "*) exec bash "$inst" "$@" ;;
  esac
  msg "$S_NO_TTY_KO" "$S_NO_TTY_EN" >&2
  return 1
}

get_main() {
  get_detect_lang
  msg "$S_START_KO" "$S_START_EN"
  ensure_git || exit 1
  ensure_clone || exit 1
  handoff "$@" || exit 1
}

# The ONLY top-level call, on the last line (partial-download hardening --
# see the header comment).
get_main "$@"
