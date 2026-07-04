#!/usr/bin/env bash
# install.sh -- Dogany product first-run installer (macOS + Linux).
#
# The flagship, non-developer setup experience. Walks the user through:
#   1. language   2. timezone   3. prerequisites   4. dependencies
#   5. bot token + owner id (born-locked)   6. email connect (optional)
#   7. mint the agent   8. write .env
#   9. service autostart (launchd/systemd/manual)   10. final message
#
# BYO-compute: the user's OWN Claude Code auth is used as-is (personal/self-host
# is within Anthropic policy). We only verify the `claude` CLI runs; we never
# force an API key and never block subscription auth.
#
# Design notes:
#   - Reuses scripts/mint.sh for instance creation (no duplicated mint logic).
#   - macOS ships bash 3.2: NO associative arrays, NO mapfile. Portable only.
#   - .env is written atomically (temp -> mv); any overwrite is backed up first.
#   - --dry-run runs the whole flow from env/args with mock values, writes only
#     into a temp dir, and does NOT call mint.sh or install a service.
#
# ASCII-only code + comments. User-facing prompts are bilingual (ko/en).
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Constants / globals
# ---------------------------------------------------------------------------
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_PATH"
MINT_SH="$REPO_ROOT/scripts/mint.sh"

# Populated as the flow proceeds.
DOGANY_LANG="${DOGANY_LANG:-}"        # ko | en
# Set to 1 when --lang was passed explicitly: skips the language confirm step
# (the flag is the user's decision -- do not second-guess it interactively).
LANG_FORCED=0
DOGANY_TZ="${DOGANY_TZ:-}"            # IANA tz string
BOT_TOKEN=""
OWNER_ID=""
BOT_NAME=""
# Optional email-send (dogany-mailer) connect. Blank = not connected.
EMAIL_ADDRESS="${EMAIL_ADDRESS:-}"
EMAIL_APP_PASSWORD="${EMAIL_APP_PASSWORD:-}"
EMAIL_CC="${EMAIL_CC:-}"
ENABLE_VOICE="${DOGANY_VOICE:-0}"     # 0 core-only (default), 1 full
INSTALL_ROOT="${DOGANY_INSTALL_ROOT:-$SCRIPT_PATH/agents/main}"
# Default 1 installed agent. A non-1 value (env or config) bypasses the single-agent refusal.
DOGANY_MAX_AGENTS="${DOGANY_MAX_AGENTS:-1}"
AGENT_NAME="${DOGANY_AGENT_NAME:-dogany}"
SERVICE_CHOICE=""                     # auto | manual
OS_KIND=""                            # macos | linux
# Claude model for the minted instance settings.json. Chosen at the model step
# (recommended from the subscription tier). Empty until then; template default
# stays "sonnet" and is only overwritten post-mint when this is set.
DOGANY_MODEL="${DOGANY_MODEL:-}"      # sonnet | opus

DRY_RUN=0
# In --dry-run, all filesystem writes are redirected under this temp dir and no
# live command (mint / launchctl / systemctl) is ever executed.
DRY_TMP=""

# Lite tier: single-agent idempotency marker.
LITE_MARKER_DIR="$HOME/.dogany"
LITE_MARKER="$LITE_MARKER_DIR/lite_instance"

# ---------------------------------------------------------------------------
# 1. Bilingual string helper (inline; NOT a full i18n system by design)
# ---------------------------------------------------------------------------
# Usage: msg "korean text" "english text"  -> echoes the one matching $DOGANY_LANG.
msg() {
  if [ "${DOGANY_LANG:-en}" = "ko" ]; then
    printf '%s\n' "$1"
  else
    printf '%s\n' "$2"
  fi
}

# Same but no trailing newline (for prompts).
msgn() {
  if [ "${DOGANY_LANG:-en}" = "ko" ]; then
    printf '%s' "$1"
  else
    printf '%s' "$2"
  fi
}

hr() { printf '%s\n' "------------------------------------------------------------"; }

# ---------------------------------------------------------------------------
# 1b. Lite-tier single-agent idempotency helpers
# ---------------------------------------------------------------------------
# Portable path canonicalization for bash 3.2 (macOS ships bash 3.2; no
# realpath/readlink -f guaranteed). Strategy:
#   - If the directory already exists: cd into it and capture $PWD (resolves
#     symlinks the shell follows, and normalises . / .. / trailing slashes).
#   - Otherwise: normalize trailing slashes and collapse /./ sequences via sed.
#     This covers the "pre-creation" path (install to a not-yet-existing dir).
canon_path() {
  local p="$1"
  # Expand a literal quoted tilde ("~/x" arrives verbatim when quoted).
  case "$p" in "~/"*) p="$HOME/${p#\~/}" ;; "~") p="$HOME" ;; esac
  # Relative paths anchor at the caller's cwd (a relative --root under a
  # TCC-gated cwd must still resolve into the gated tree).
  case "$p" in /*) : ;; *) p="$PWD/$p" ;; esac
  if [ -d "$p" ]; then
    (cd "$p" && pwd -P)   # physical: resolves symlinks and case (APFS)
  else
    # Not yet created: physically resolve the deepest existing ancestor,
    # then re-append the tail.
    local dir="$p" tail=""
    while [ ! -d "$dir" ] && [ "$dir" != "/" ]; do
      tail="/$(basename "$dir")$tail"
      dir="$(dirname "$dir")"
    done
    printf '%s%s' "$(cd "$dir" && pwd -P)" "$tail" \
      | sed -E 's|/+$||; s|/+|/|g; s|/\./|/|g'
  fi
}

# Single-agent idempotency: at most ONE installed instance by default.
# Call before minting. Exits non-zero if a different VALID instance already
# holds the marker. Returns 0 if mint should proceed (no conflict, same root, or
# a stale marker that was self-healed).
check_lite_single_agent() {
  local current_root="$1"
  local current_canon
  current_canon="$(canon_path "$current_root")"

  # Config knob: a non-1 limit bypasses the single-agent refusal.
  if [ "${DOGANY_MAX_AGENTS:-1}" != "1" ]; then
    return 0
  fi

  # --- marker absent -> no prior install -> proceed ---
  if [ ! -f "$LITE_MARKER" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      msg "[dry-run] lite_instance 마커 없음 -> 신규 설치 진행 예정" \
          "[dry-run] lite_instance marker absent -> would proceed with new install"
      msg "[dry-run] 마커 쓸 위치: $LITE_MARKER (내용: $current_canon)" \
          "[dry-run] Would write marker: $LITE_MARKER (content: $current_canon)"
    fi
    return 0
  fi

  # --- marker present but empty / malformed -> warn, treat as absent ---
  local recorded
  recorded="$(cat "$LITE_MARKER" 2>/dev/null | tr -d '[:space:]')"
  if [ -z "$recorded" ]; then
    msg "[경고] lite_instance 마커가 비어 있거나 손상되었습니다. 없는 것으로 처리합니다." \
        "[WARN] lite_instance marker is empty or malformed. Treating as absent."
    if [ "$DRY_RUN" = "1" ]; then
      msg "[dry-run] 마커 쓸 위치: $LITE_MARKER (내용: $current_canon)" \
          "[dry-run] Would write marker: $LITE_MARKER (content: $current_canon)"
    fi
    return 0
  fi

  # --- DGN-145: recorded root missing OR not a minted instance -> stale marker,
  # self-heal (warn, drop the marker, CONTINUE the install). A minted instance
  # always carries an AGENT.md; its absence means the marked path is not a real
  # instance (moved/deleted/leftover), so it must not block a fresh install.
  if [ ! -d "$recorded" ] || [ ! -f "$recorded/AGENT.md" ]; then
    msg "[경고] 기록된 설치 경로가 유효한 인스턴스가 아닙니다: $recorded. 오래된 마커를 제거하고 신규 설치로 계속합니다." \
        "[WARN] Recorded install path is not a valid instance: $recorded. Removing the stale marker and continuing with a new install." >&2
    [ "$DRY_RUN" = "1" ] || rm -f "$LITE_MARKER" 2>/dev/null || true
    return 0
  fi

  local recorded_canon
  recorded_canon="$(canon_path "$recorded")"

  # --- same root -> reconfigure of existing single agent -> proceed ---
  if [ "$recorded_canon" = "$current_canon" ]; then
    msg "기존 에이전트를 재설정합니다: $recorded_canon" \
        "Reconfiguring existing agent: $recorded_canon"
    return 0
  fi

  # --- DGN-145: a valid instance already exists at a different path -> stop and
  # point the user at the reset command. No tier language, no upsell.
  hr >&2
  msg "에이전트 인스턴스가 이미 존재합니다: $recorded_canon" \
      "An agent instance already exists at: $recorded_canon" >&2
  msg "해당 인스턴스를 초기화하려면 그 경로를 --root 로 지정해 다시 실행하세요:" \
      "To reset it, rerun with --root pointing at that path:" >&2
  printf '  bash install.sh --root "%s"\n' "$recorded_canon" >&2
  hr >&2
  exit 2
}

# Write (or overwrite) the Lite single-agent marker after a successful mint.
# No-op in dry-run.
write_lite_marker() {
  local root_to_record="$1"
  [ "$DRY_RUN" = "1" ] && return 0
  mkdir -p "$LITE_MARKER_DIR"
  printf '%s\n' "$(canon_path "$root_to_record")" > "$LITE_MARKER"
}

# ---------------------------------------------------------------------------
# 2. Error trap
# ---------------------------------------------------------------------------
on_error() {
  local exit_code=$?
  local line=${1:-?}
  echo "" >&2
  hr >&2
  msg "[에러] 설치가 중단되었습니다 (line $line, code $exit_code)." \
      "[ERROR] Install aborted (line $line, code $exit_code)." >&2
  msg "위 마지막 메시지를 확인하세요. 다시 실행하면 이어서 진행할 수 있습니다." \
      "Check the last message above. Re-running is safe and resumes where possible." >&2
  hr >&2
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

# ---------------------------------------------------------------------------
# 3. Small IO helpers
# ---------------------------------------------------------------------------
# Abort cleanly when an interactive prompt hits stdin EOF (e.g. piped input ran
# out). Looping forever on a dead stdin reprints the prompt endlessly; instead
# print a clear message and exit. NOT called in dry-run (which never reads).
abort_on_eof() {
  hr >&2
  msg "[에러] 입력이 예기치 않게 끝났습니다 (stdin EOF). 설치를 중단합니다." \
      "[ERROR] Input ended unexpectedly (stdin EOF). Aborting install." >&2
  msg "설치 마법사는 대화형 입력이 필요합니다. 터미널에서 직접 실행하세요." \
      "The install wizard needs interactive input. Run it directly in a terminal." >&2
  hr >&2
  exit 1
}

# Drain any lines still buffered on the tty right now, non-blocking, and echo
# them (newline-joined) to stdout. Used after a paste so extra lines the user
# pasted (a full multi-line BotFather / userinfobot message) do not leak into
# the NEXT prompt one-per-read. bash 3.2 (macOS) note: `read -t` rejects
# fractional seconds ("invalid timeout specification"), so the smallest portable
# window is `-t 1`. Each buffered line arrives instantly; the 1s ceiling only
# applies once, when the buffer is empty (nothing more to drain). Prints nothing
# when the buffer is empty. Safe in non-interactive/EOF contexts (loop just ends).
drain_buffered_lines() {
  local extra
  while IFS= read -r -t 1 extra; do
    printf '%s\n' "$extra"
  done
}

# ask VAR "ko prompt" "en prompt" [default]  -- reads a line into VAR.
# In dry-run, if VAR is already set (from env/args), keep it and echo the mock.
# After the first read, drains any further tty-buffered lines into the value so a
# multi-line paste is captured whole (DGN-142) rather than dribbling into later
# prompts. Callers that only want the first line (single-value answers) still work
# because the drained tail is simply appended; extraction over the whole blob then
# picks the right token/id, and single-token answers have no tail to drain.
ask() {
  local __var="$1" ko="$2" en="$3" def="${4:-}"
  local __cur
  eval "__cur=\${$__var:-}"
  if [ "$DRY_RUN" = "1" ]; then
    # Dry-run: never block on a real prompt. Use existing value or default.
    if [ -z "$__cur" ]; then __cur="$def"; fi
    msgn "$ko" "$en"; printf '[dry-run mock: %s]\n' "${__cur:-<empty>}"
    eval "$__var=\$__cur"
    return 0
  fi
  msgn "$ko" "$en"
  local reply
  # read fails (nonzero) only on EOF, not on an empty line -> abort on EOF so a
  # dead/exhausted stdin never loops the caller forever.
  IFS= read -r reply || abort_on_eof
  # Drain leftover buffered paste lines and append them to the value BEFORE the
  # caller extracts (token/id) or the next prompt runs. Newline-joined so a
  # multi-line blob stays intact for grep-based extraction.
  local __tail
  __tail="$(drain_buffered_lines)"
  if [ -n "$__tail" ]; then reply="$reply"$'\n'"$__tail"; fi
  if [ -z "$reply" ] && [ -n "$def" ]; then reply="$def"; fi
  eval "$__var=\$reply"
}

# ask_secret VAR "ko prompt" "en prompt"  -- like ask() but the typed value is
# NOT echoed to the terminal (read -rs). Use for secrets (e.g. app passwords).
# Dry-run-safe: keeps any preset value / echoes a mock, exactly like ask().
ask_secret() {
  local __var="$1" ko="$2" en="$3"
  local __cur
  eval "__cur=\${$__var:-}"
  if [ "$DRY_RUN" = "1" ]; then
    msgn "$ko" "$en"; printf '[dry-run mock: %s]\n' "${__cur:+***}"
    eval "$__var=\$__cur"
    return 0
  fi
  msgn "$ko" "$en"
  local reply
  # -s: silent (no echo while typing). read has no trailing newline in -s mode,
  # so print one explicitly after the (invisible) input finishes.
  IFS= read -rs reply || { printf '\n'; abort_on_eof; }
  printf '\n'
  eval "$__var=\$reply"
}

# confirm "ko question" "en question" [default_yes]  -> returns 0 for yes.
confirm() {
  local ko="$1" en="$2" def="${3:-y}" reply
  local hint="[Y/n]"; [ "$def" = "n" ] && hint="[y/N]"
  if [ "$DRY_RUN" = "1" ]; then
    msgn "$ko " "$en "; printf '%s [dry-run: %s]\n' "$hint" "$def"
    [ "$def" = "y" ]; return
  fi
  # DGN-142: drain any leftover tty-buffered paste lines (e.g. the tail of a
  # BotFather message) BEFORE printing the prompt, so none is silently consumed
  # as the y/n answer. Discard them -- a confirm has no use for pasted prose.
  drain_buffered_lines >/dev/null
  msgn "$ko " "$en "; printf '%s ' "$hint"
  IFS= read -r reply || abort_on_eof
  reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
  [ -z "$reply" ] && reply="$def"
  case "$reply" in y|yes|예|ㅇ) return 0 ;; *) return 1 ;; esac
}

# ---------------------------------------------------------------------------
# 4. Token / id extraction (regex over a pasted BotFather / userinfobot blob)
# ---------------------------------------------------------------------------
# Telegram bot token shape: <8-10 digits>:<35 chars of [A-Za-z0-9_-]>.
# grep -oE prints only the matched token from anywhere in the pasted message.
extract_token() {
  printf '%s' "$1" | grep -oE '[0-9]{8,10}:[A-Za-z0-9_-]{35}' | head -n1 || true
}

# Numeric Telegram user id. userinfobot prints e.g. "Id: 123456789".
# We take the first standalone run of 5-15 digits.
extract_user_id() {
  printf '%s' "$1" | grep -oE '[0-9]{5,15}' | head -n1 || true
}

# Mask a token for display: keep the numeric id and last 4, hide the middle.
mask_token() {
  printf '%s' "$1" | sed -E 's/^([0-9]{8,10}:)[A-Za-z0-9_-]{31}([A-Za-z0-9_-]{4})$/\1********************************\2/'
}

# ---------------------------------------------------------------------------
# 5. OS + prerequisite detection
# ---------------------------------------------------------------------------
detect_os() {
  case "$(uname -s)" in
    Darwin) OS_KIND="macos" ;;
    Linux)  OS_KIND="linux" ;;
    *) msg "지원되지 않는 OS 입니다: $(uname -s)" "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
  esac
}

# macOS TCC guard: launchd background services (and their child processes) run
# under a context that CANNOT read TCC-gated folders -- ~/Documents, ~/Desktop,
# ~/Downloads -- without an interactive "Operation not permitted" that a headless
# service can never satisfy. An install rooted there yields a dead bot on first
# boot. HARD refuse (a warned-past user still hits the dead bot). Linux: no-op.
# Checks the resolved INSTALL_ROOT (which for the default in-repo mint is the
# repo path itself, so a repo cloned under ~/Documents is caught too).
tcc_guard() {
  [ "$OS_KIND" = "macos" ] || return 0
  local root_canon home_canon root_lc gated_lc
  root_canon="$(canon_path "$INSTALL_ROOT")"
  home_canon="$(cd "$HOME" && pwd -P)"
  # Case-insensitive compare (APFS default is case-insensitive).
  root_lc="$(printf '%s' "$root_canon" | tr '[:upper:]' '[:lower:]')"
  local gated
  for gated in "$home_canon/Documents" "$home_canon/Desktop" "$home_canon/Downloads"; do
    gated_lc="$(printf '%s' "$gated" | tr '[:upper:]' '[:lower:]')"
    case "$root_lc/" in
      "$gated_lc"/*)
        hr >&2
        msg "[에러] 설치 경로가 macOS 보호 폴더 안에 있습니다: $root_canon" \
            "[ERROR] Install path is inside a macOS protected folder: $root_canon" >&2
        msg "launchd 백그라운드 서비스는 문서/데스크탑/다운로드 폴더(TCC 보호)를 읽을 수 없어" \
            "launchd background services cannot read the Documents/Desktop/Downloads folders (TCC-gated)," >&2
        msg "봇이 부팅 직후 'Operation not permitted' 로 죽습니다." \
            "so the bot would die on boot with 'Operation not permitted'." >&2
        msg "홈 디렉토리($HOME) 바로 아래나 일반 폴더에 클론/설치한 뒤 다시 실행하세요." \
            "Clone/install directly under your home ($HOME) or another plain directory, then re-run." >&2
        hr >&2
        exit 1
        ;;
    esac
  done
}

detect_lang() {
  if [ -n "$DOGANY_LANG" ]; then return; fi
  case "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" in
    *ko*|*ko_KR*|*Korean*) DOGANY_LANG="ko" ;;
    *) DOGANY_LANG="en" ;;
  esac
}

detect_tz() {
  if [ -n "$DOGANY_TZ" ]; then return; fi
  local tz=""
  if [ "$OS_KIND" = "macos" ]; then
    if [ -L /etc/localtime ]; then
      # /etc/localtime -> /var/db/timezone/zoneinfo/Asia/Seoul
      tz="$(readlink /etc/localtime 2>/dev/null | sed -E 's#.*/zoneinfo/##')"
    fi
    if [ -z "$tz" ] && command -v systemsetup >/dev/null 2>&1; then
      tz="$(systemsetup -gettimezone 2>/dev/null | sed -E 's/^Time Zone: //')"
    fi
  else
    if command -v timedatectl >/dev/null 2>&1; then
      tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    fi
    if [ -z "$tz" ] && [ -r /etc/timezone ]; then
      tz="$(cat /etc/timezone 2>/dev/null || true)"
    fi
    if [ -z "$tz" ] && [ -L /etc/localtime ]; then
      tz="$(readlink /etc/localtime 2>/dev/null | sed -E 's#.*/zoneinfo/##')"
    fi
  fi
  [ -z "$tz" ] && tz="UTC"
  DOGANY_TZ="$tz"
}

# ver_ge "3.11.4" "3.11" -> 0 if first >= second (major.minor compare).
py_version_ok() {
  local have="$1"
  local maj min
  maj="$(printf '%s' "$have" | cut -d. -f1)"
  min="$(printf '%s' "$have" | cut -d. -f2)"
  [ -z "$maj" ] && return 1
  [ "$maj" -gt 3 ] && return 0
  [ "$maj" -eq 3 ] && [ "${min:-0}" -ge 11 ] && return 0
  return 1
}

# claude_auth_ok -- lightweight probe that `claude` is actually AUTHENTICATED,
# not merely installed. Strategy (cheapest signal first):
#   1) A short non-interactive `claude -p` call. If it returns 0 with output,
#      the model was reachable -> authenticated. A wrapped timeout keeps a hung
#      auth prompt from blocking install (macOS lacks `timeout` by default, so
#      we background + poll). This costs one tiny token call, only when needed.
#   2) Fallback: inspect ~/.claude.json for an oauthAccount block (present after
#      a successful subscription login) as a best-effort offline signal.
# Returns 0 if authenticated-looking, non-zero otherwise. Never prints secrets.
claude_auth_ok() {
  # --- probe 1: a tiny -p sanity call, time-bounded ---
  local out_file rc
  out_file="$(mktemp "${TMPDIR:-/tmp}/dogany-auth.XXXXXX")"
  ( claude -p "reply with the single word: ok" >"$out_file" 2>/dev/null ) &
  local pid=$!
  # poll up to ~30s (auth-failure returns fast; a real call is a few seconds).
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited+1))
    if [ "$waited" -ge 30 ]; then
      kill "$pid" 2>/dev/null || true
      break
    fi
  done
  wait "$pid" 2>/dev/null; rc=$?
  if [ "$rc" = "0" ] && [ -s "$out_file" ]; then
    rm -f "$out_file"
    return 0
  fi
  rm -f "$out_file"

  # --- probe 2: offline config inspection (best-effort) ---
  if [ -r "$HOME/.claude.json" ]; then
    if grep -q '"oauthAccount"' "$HOME/.claude.json" 2>/dev/null \
       || grep -q '"hasCompletedOnboarding"[[:space:]]*:[[:space:]]*true' "$HOME/.claude.json" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

check_prereqs() {
  hr
  msg "[3/10] 사전 조건 확인" "[3/10] Checking prerequisites"
  hr

  # --- claude CLI (BYO-compute; verify it runs, do NOT dictate auth) ---
  if command -v claude >/dev/null 2>&1; then
    local cver
    cver="$(claude --version 2>/dev/null | head -n1 || true)"
    msg "  [OK] claude CLI: ${cver:-발견됨}" "  [OK] claude CLI: ${cver:-found}"
  else
    msg "  [실패] claude CLI 를 PATH 에서 찾을 수 없습니다." \
        "  [FAIL] claude CLI not found on PATH."
    msg "  설치: https://docs.claude.com/claude-code 를 참고해 Claude Code 를 설치하고," \
        "  Install: follow https://docs.claude.com/claude-code to install Claude Code,"
    msg "  본인 계정으로 로그인(구독/자체호스팅 모두 가능)한 뒤 다시 실행하세요." \
        "  sign in with your own account (subscription or self-host both fine), then re-run."
    exit 1
  fi

  # --- claude AUTH probe (presence != authenticated) ---
  # Presence alone lets a logged-OUT user pass, then the bot crashes on its first
  # chat. Probe that the CLI can actually reach the model; if not, launch the
  # OFFICIAL interactive login inline so the user signs in DURING install, then
  # re-probe. We NEVER reimplement auth -- we only invoke `claude`.
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] claude 인증 프로브/로그인은 건너뜁니다." \
        "  [dry-run] Skipping claude auth probe/login."
  else
    if claude_auth_ok; then
      msg "  [OK] claude 인증됨 (모델 응답 확인)." "  [OK] claude authenticated (model responded)."
    else
      msg "  [주의] claude 가 설치되어 있으나 로그인되지 않은 것으로 보입니다." \
          "  [NOTE] claude is installed but does not appear to be logged in."
      msg "  지금 공식 로그인 화면을 실행합니다. 안내에 따라 로그인/인증을 마치세요." \
          "  Launching the official login now. Complete sign-in/auth as prompted."
      # Official interactive flow. `claude` with no -p opens the TUI where /login
      # is available; this inherits the terminal so the user can complete OAuth.
      claude || true
      if claude_auth_ok; then
        msg "  [OK] claude 인증 완료." "  [OK] claude authentication complete."
      else
        msg "  [실패] claude 인증을 확인하지 못했습니다." \
            "  [FAIL] Could not verify claude authentication."
        msg "  터미널에서  claude  를 실행해 로그인(/login)한 뒤 설치를 다시 실행하세요." \
            "  Run  claude  in a terminal, sign in (/login), then re-run the installer."
        exit 1
      fi
    fi
  fi

  # --- Python 3.11+ ---
  local py_bin="" py_ver=""
  for cand in python3 python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then
      py_ver="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
      if py_version_ok "$py_ver"; then py_bin="$cand"; break; fi
    fi
  done
  if [ -n "$py_bin" ]; then
    msg "  [OK] Python: $py_ver ($py_bin)" "  [OK] Python: $py_ver ($py_bin)"
  else
    msg "  [실패] Python 3.11 이상이 필요합니다." "  [FAIL] Python 3.11+ is required."
    if [ "$OS_KIND" = "macos" ]; then
      msg "  설치: brew install python@3.12  (Homebrew 없으면 https://brew.sh)" \
          "  Install: brew install python@3.12  (no Homebrew? https://brew.sh)"
    else
      msg "  설치: sudo apt install python3.12  (또는 배포판 패키지 매니저)" \
          "  Install: sudo apt install python3.12  (or your distro's package manager)"
    fi
    exit 1
  fi

  # --- git ---
  if command -v git >/dev/null 2>&1; then
    msg "  [OK] git: $(git --version 2>/dev/null | head -n1)" \
        "  [OK] git: $(git --version 2>/dev/null | head -n1)"
  else
    msg "  [실패] git 이 필요합니다." "  [FAIL] git is required."
    if [ "$OS_KIND" = "macos" ]; then
      msg "  설치: xcode-select --install  또는  brew install git" \
          "  Install: xcode-select --install  or  brew install git"
    else
      msg "  설치: sudo apt install git" "  Install: sudo apt install git"
    fi
    exit 1
  fi

  # --- mint.sh must exist ---
  if [ ! -f "$MINT_SH" ]; then
    msg "  [실패] mint.sh 를 찾을 수 없습니다: $MINT_SH" \
        "  [FAIL] mint.sh not found: $MINT_SH" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Step 1: language
# ---------------------------------------------------------------------------
step_language() {
  hr
  msg "[1/10] 언어" "[1/10] Language"
  hr
  # --lang was passed explicitly -> the user already decided; skip the confirm.
  if [ "$LANG_FORCED" = "1" ]; then
    msg "언어: 한국어 (--lang)" "Language: English (--lang)"
    return 0
  fi
  # detect_lang already ran; show detected, let user switch.
  if [ "$DOGANY_LANG" = "ko" ]; then
    msg "감지된 언어: 한국어" "Detected language: Korean"
    if ! confirm "한국어로 진행할까요? (n = English)" "Proceed in Korean? (n = English)" "y"; then
      DOGANY_LANG="en"
    fi
  else
    msg "Detected language: English" "Detected language: English"
    if ! confirm "Proceed in English? (n = 한국어)" "Proceed in English? (n = Korean)" "y"; then
      DOGANY_LANG="ko"
    fi
  fi
  msg "언어: 한국어" "Language: English"
}

# ---------------------------------------------------------------------------
# Step 2: timezone
# ---------------------------------------------------------------------------
# Validate an IANA timezone name. Prefers python3 zoneinfo; falls back to
# the tzdata files on disk; unknown environment -> accept (fail-open).
tz_valid() {
  local tz="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$tz" <<'PYEOF' >/dev/null 2>&1
import sys, zoneinfo
zoneinfo.ZoneInfo(sys.argv[1])
PYEOF
    return $?
  fi
  [ -f "/usr/share/zoneinfo/$tz" ] && return 0
  return 0  # no validator available -> accept
}

# DGN-143: fuzzy timezone lookup. Given a rough query (e.g. "tokyo", "seoul"),
# print up to 8 IANA zone names whose full name contains the query
# (case-insensitive substring) over python3's zoneinfo.available_timezones(),
# one per line, sorted. Prints nothing when python3 is unavailable or no zone
# matches -- callers fall back to plain reprompt. Never fails the install.
tz_fuzzy_candidates() {
  local query="$1"
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$query" <<'PYEOF' 2>/dev/null || true
import sys
try:
    from zoneinfo import available_timezones
    q = sys.argv[1].strip().lower()
    if not q:
        sys.exit(0)
    hits = sorted(z for z in available_timezones() if q in z.lower())
    for z in hits[:8]:
        print(z)
except Exception:
    pass
PYEOF
}

# Resolve the SYSTEM local timezone -- the clock launchd/systemd actually fire
# on. This is independent of the user's chosen DOGANY_TZ (e.g. a UTC server with
# a user in Asia/Seoul). Best-effort; empty -> caller treats as "same as chosen"
# (no conversion, the safe common case).
system_tz() {
  local tz=""
  if [ -L /etc/localtime ]; then
    tz="$(readlink /etc/localtime 2>/dev/null | sed -E 's#.*/zoneinfo/##')"
  fi
  if [ -z "$tz" ] && [ "$OS_KIND" = "linux" ]; then
    command -v timedatectl >/dev/null 2>&1 && \
      tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    [ -z "$tz" ] && [ -r /etc/timezone ] && tz="$(cat /etc/timezone 2>/dev/null || true)"
  fi
  printf '%s' "$tz"
}

# Convert a user-local HH:MM (in $DOGANY_TZ) to the equivalent HH:MM on the
# SYSTEM clock (system_tz). launchd StartCalendarInterval / systemd OnCalendar
# fire on the system clock, so a routine the user wants at 04:30 Asia/Seoul must
# be stamped as 19:30 on a UTC host. Prints "HH MM" (space-separated, zero-padded
# hour/minute as integers without leading zeros for arithmetic safety -> we emit
# plain integers). Uses python3 zoneinfo with TODAY's offset.
# NOTE (DST caveat, acceptable for v1.0.1): the offset is computed for today; a
# zone that changes offset across DST will drift by an hour for part of the year.
# If system TZ == chosen TZ (or either is unresolved) -> echoes the input
# unchanged (zero behavior change in the common case).
convert_routine_time() {
  local hh="$1" mm="$2" user_tz="$3" sys_tz="$4"
  # No conversion when either side is unknown or they match.
  if [ -z "$user_tz" ] || [ -z "$sys_tz" ] || [ "$user_tz" = "$sys_tz" ]; then
    printf '%s %s' "$hh" "$mm"; return 0
  fi
  command -v python3 >/dev/null 2>&1 || { printf '%s %s' "$hh" "$mm"; return 0; }
  python3 - "$hh" "$mm" "$user_tz" "$sys_tz" <<'PYEOF' 2>/dev/null || printf '%s %s' "$hh" "$mm"
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
hh, mm, user_tz, sys_tz = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    today = datetime.now(ZoneInfo(user_tz)).date()
    local = datetime(today.year, today.month, today.day, hh, mm, tzinfo=ZoneInfo(user_tz))
    on_sys = local.astimezone(ZoneInfo(sys_tz))
    print("%d %d" % (on_sys.hour, on_sys.minute))
except Exception:
    print("%d %d" % (hh, mm))
PYEOF
}

step_timezone() {
  hr
  msg "[2/10] 타임존" "[2/10] Timezone"
  hr
  msg "감지된 타임존: $DOGANY_TZ" "Detected timezone: $DOGANY_TZ"
  if ! confirm "이 타임존이 맞나요?" "Is this timezone correct?" "y"; then
    local tz_in="" tries=0
    while [ "$tries" -lt 3 ]; do
      ask tz_in "타임존을 입력하세요 (IANA 이름 또는 도시명, 예: Asia/Seoul, America/New_York): " \
                "Enter a timezone (IANA name or a city, e.g. Asia/Seoul, America/New_York): "
      [ -z "$tz_in" ] && break  # keep detected
      # Exact IANA input is accepted directly (final gate below).
      if tz_valid "$tz_in"; then
        DOGANY_TZ="$tz_in"; break
      fi
      # DGN-143: not exact -> case-insensitive substring search over the IANA
      # zone list and offer numbered candidates to pick from.
      local cands=""
      cands="$(tz_fuzzy_candidates "$tz_in")"
      if [ -n "$cands" ]; then
        msg "정확히 일치하지 않습니다. '$tz_in' 후보:" \
            "No exact match. Candidates for '$tz_in':"
        local i=0 line pick=""
        # Build a numbered menu; capture choices into positional-safe vars.
        local c1="" c2="" c3="" c4="" c5="" c6="" c7="" c8=""
        while IFS= read -r line; do
          [ -n "$line" ] || continue
          i=$((i + 1))
          eval "c$i=\$line"
          printf '  %d) %s\n' "$i" "$line"
        done <<EOF
$cands
EOF
        ask pick "번호를 선택하세요 (건너뛰려면 빈 줄): " \
                 "Pick a number (blank to skip): "
        case "$pick" in
          ''|*[!0-9]*) : ;;  # non-numeric / blank -> reprompt
          *)
            if [ "$pick" -ge 1 ] && [ "$pick" -le "$i" ]; then
              eval "tz_in=\$c$pick"
              if tz_valid "$tz_in"; then DOGANY_TZ="$tz_in"; break; fi
            fi
            ;;
        esac
      fi
      tries=$((tries + 1))
      msg "유효한 타임존을 찾지 못했습니다 (예: Asia/Seoul, America/New_York)." \
          "Could not resolve a valid timezone (e.g. Asia/Seoul, America/New_York)."
    done
  fi
  msg "타임존: $DOGANY_TZ" "Timezone: $DOGANY_TZ"
}

# ---------------------------------------------------------------------------
# Step 4: dependencies (voice opt-in)
# ---------------------------------------------------------------------------
step_dependencies() {
  hr
  msg "[4/10] 의존성" "[4/10] Dependencies"
  hr
  msg "기본은 핵심 의존성만 설치합니다 (빠르고 가벼움)." \
      "Default installs core dependencies only (fast and light)."
  # ENABLE_VOICE may already be preset via env for dry-run.
  if [ "$ENABLE_VOICE" != "1" ]; then
    # DGN-144: state the real download size and the device disk state so the
    # user can judge before opting in.
    msg "음성 입력은 faster-whisper small 모델(~0.5GB)을 내려받습니다." \
        "Voice input downloads faster-whisper small (~0.5GB)."
    disk_free_line
    if confirm "음성 입력을 활성화할까요?" \
               "Enable voice input?" "n"; then
      ENABLE_VOICE=1
    else
      ENABLE_VOICE=0
    fi
  fi
  if [ "$ENABLE_VOICE" = "1" ]; then
    msg "  -> 전체 설치 (음성 포함)" "  -> full install (voice included)"
  else
    msg "  -> 핵심 전용 설치 (--core-only)" "  -> core-only install (--core-only)"
  fi

  # Optional semantic-memory embedding backend (Ollama + bge-m3).
  step_embedding
}

# DGN-144: disk free/total for the $HOME volume, as a bilingual one-liner
# "disk: N GB free of M GB" (integers, floor). Portable via `df -k` + awk (GB =
# kB / 1024 / 1024). Fail-open: prints nothing if df/awk cannot produce numbers,
# so a detection miss never blocks or breaks a prompt. Always returns 0.
disk_free_line() {
  local out avail_kb total_kb
  # df -k on $HOME: 1K-blocks. Columns vary (BSD vs GNU); read the data row's
  # total(2) and available(4) fields via awk, tolerating a wrapped first line.
  out="$(df -k "$HOME" 2>/dev/null | awk 'NR>1 && $2 ~ /^[0-9]+$/ {print $2" "$4; exit}')"
  total_kb="${out%% *}"
  avail_kb="${out##* }"
  case "$total_kb" in ''|*[!0-9]*) return 0 ;; esac
  case "$avail_kb" in ''|*[!0-9]*) return 0 ;; esac
  local free_gb=$((avail_kb / 1024 / 1024))
  local total_gb=$((total_kb / 1024 / 1024))
  msg "  디스크: ${free_gb}GB 여유 / 총 ${total_gb}GB" \
      "  disk: ${free_gb} GB free of ${total_gb} GB"
  return 0
}

# Total system RAM in GB (integer, floor). macOS: sysctl hw.memsize (bytes).
# Linux: /proc/meminfo MemTotal (kB). Unknown -> empty string.
# Prints RAM in kB (empty if undetectable). Always returns 0 (fail-open:
# a detection failure must never kill the install under set -e).
system_ram_kb() {
  local bytes="" kb=""
  if [ "$OS_KIND" = "macos" ]; then
    bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    [ -n "$bytes" ] && printf '%s' "$((bytes / 1024))"
  else
    kb="$(grep -E '^MemTotal:' /proc/meminfo 2>/dev/null | awk '{print $2}' || true)"
    [ -n "$kb" ] && printf '%s' "$kb"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Step 4b: optional embedding backend (semantic memory)
# ---------------------------------------------------------------------------
# Semantic (cross-lingual) memory recall needs a local embedding model: Ollama
# running bge-m3 (~1.2GB). Fully optional -- without it the agent falls back to
# keyword recall. ALL failures fail-open (warn + continue install). Dry-run only
# prints decisions.
step_embedding() {
  hr
  msg "  [4b] 시맨틱 메모리 임베딩 (선택: Ollama + bge-m3, ~1.2GB)" \
      "  [4b] Semantic memory embedding (optional: Ollama + bge-m3, ~1.2GB)"
  hr

  local have_ollama=0
  command -v ollama >/dev/null 2>&1 && have_ollama=1

  # Already installed AND model present -> nothing to do.
  if [ "$have_ollama" = "1" ] && ollama list 2>/dev/null | grep -qi 'bge-m3'; then
    msg "  [OK] Ollama + bge-m3 이미 설치됨 -> 시맨틱 메모리 사용 가능." \
        "  [OK] Ollama + bge-m3 already present -> semantic memory available."
    return 0
  fi

  # Recommend by RAM: 16GB-class or more -> default YES, else default NO.
  # Threshold in kB (15,000,000 kB): real 16GB Linux reports ~16.2M kB minus
  # kernel-reserved memory, so a GB floor would misclassify it as 15GB.
  local ram_kb; ram_kb="$(system_ram_kb || true)"
  local ram_disp="?"
  [ -n "$ram_kb" ] && ram_disp="$((ram_kb / 1024 / 1024))"
  local def="n"
  if [ -n "$ram_kb" ] && [ "$ram_kb" -ge 15000000 ]; then
    def="y"
    msg "  감지된 RAM: ${ram_disp}GB급 (16GB 이상) -> 임베딩 설치 권장." \
        "  Detected RAM: ${ram_disp}GB class (16GB+) -> installing embeddings is recommended."
  else
    def="n"
    msg "  감지된 RAM: ${ram_disp}GB (16GB 미만/불명) -> 키워드 검색만으로도 동작합니다. 나중에 언제든 추가 가능." \
        "  Detected RAM: ${ram_disp}GB (<16GB or unknown) -> keyword-only recall works; add later anytime."
  fi
  # DGN-144: show the device disk state next to the RAM check (bge-m3 ~1.5GB).
  disk_free_line

  if ! confirm "지금 Ollama + bge-m3 를 설치할까요?" "Install Ollama + bge-m3 now?" "$def"; then
    msg "  임베딩을 건너뜁니다. 키워드 검색으로 동작하며, 나중에 언제든 추가할 수 있습니다." \
        "  Skipping embeddings. Keyword recall works; you can add this anytime later."
    return 0
  fi

  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] ollama 설치/서버기동/'ollama pull bge-m3' 를 수행할 예정 (실제 실행 안 함)." \
        "  [dry-run] Would install ollama / start server / run 'ollama pull bge-m3' (not executed)."
    return 0
  fi

  # Install ollama if missing. macOS: brew if available, else print manual URL and
  # skip. Linux: print the official curl command but NEVER auto-run curl|sh.
  if [ "$have_ollama" = "0" ]; then
    if [ "$OS_KIND" = "macos" ]; then
      if command -v brew >/dev/null 2>&1; then
        msg "  Ollama 설치 중 (brew)..." "  Installing Ollama (brew)..."
        if ! brew install ollama >/dev/null 2>&1; then
          msg "  [경고] brew 로 Ollama 설치 실패. 수동 설치: https://ollama.com/download . 키워드 검색으로 계속합니다." \
              "  [WARN] brew failed to install Ollama. Install manually: https://ollama.com/download . Continuing with keyword recall." >&2
          return 0
        fi
      else
        msg "  [주의] Homebrew 가 없습니다. https://ollama.com/download 에서 수동 설치 후 다시 시도하세요. 지금은 키워드 검색으로 계속합니다." \
            "  [NOTE] Homebrew not found. Install manually from https://ollama.com/download and re-run. Continuing with keyword recall for now." >&2
        return 0
      fi
    else
      # Linux: security posture -- do NOT pipe curl to sh automatically.
      msg "  [주의] Ollama 자동 설치는 하지 않습니다(보안). 아래 공식 명령을 직접 실행한 뒤 다시 시도하세요:" \
          "  [NOTE] Not auto-installing Ollama (security). Run the official command below yourself, then re-run:" >&2
      printf '    curl -fsSL https://ollama.com/install.sh | sh\n' >&2
      msg "  지금은 키워드 검색으로 계속합니다." "  Continuing with keyword recall for now." >&2
      return 0
    fi
    have_ollama=1
  fi

  # Ensure the server is running (least-invasive headless start).
  if ! ollama list >/dev/null 2>&1; then
    if [ "$OS_KIND" = "macos" ] && command -v brew >/dev/null 2>&1 \
       && brew services list 2>/dev/null | grep -qi '^ollama'; then
      brew services start ollama >/dev/null 2>&1 || true
    else
      # Headless background server; detach and give it a moment to bind.
      nohup ollama serve >/dev/null 2>&1 &
    fi
    local waited=0
    while ! ollama list >/dev/null 2>&1; do
      sleep 1; waited=$((waited + 1))
      [ "$waited" -ge 10 ] && break
    done
  fi

  if ! ollama list >/dev/null 2>&1; then
    msg "  [경고] Ollama 서버를 시작하지 못했습니다. 키워드 검색으로 계속합니다." \
        "  [WARN] Could not start the Ollama server. Continuing with keyword recall." >&2
    return 0
  fi

  msg "  bge-m3 모델을 내려받습니다 (~1.2GB, 시간이 걸릴 수 있습니다)..." \
      "  Pulling bge-m3 (~1.2GB, this can take a while)..."
  if ollama pull bge-m3 >/dev/null 2>&1; then
    msg "  [OK] bge-m3 준비 완료 -> 시맨틱 메모리 사용 가능." \
        "  [OK] bge-m3 ready -> semantic memory available."
  else
    msg "  [경고] bge-m3 다운로드 실패. 키워드 검색으로 계속합니다. 나중에  ollama pull bge-m3  로 재시도하세요." \
        "  [WARN] Failed to pull bge-m3. Continuing with keyword recall; retry later with  ollama pull bge-m3 ." >&2
  fi
}

# ---------------------------------------------------------------------------
# Step 3b: model recommendation (subscription-tier based)
# ---------------------------------------------------------------------------
# Read ONLY organizationRateLimitTier + organizationType from ~/.claude.json and
# map to a recommended model. PRIVACY: this helper reads NOTHING else from that
# file -- never email, names, uuids or any other oauth field -- and echoes only
# the single word "opus" or "sonnet" (or nothing on failure). Never log the tier
# string itself (it is low-sensitivity but out of caution we print only the
# derived model). "max" tier -> opus; pro / unknown / missing file -> sonnet.
recommend_model() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$HOME/.claude.json" <<'PYEOF'
import sys, json
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    oa = data.get("oauthAccount") or {}
    # Read ONLY these two fields. Do not touch any other key (PII).
    tier = str(oa.get("organizationRateLimitTier") or "").lower()
    otype = str(oa.get("organizationType") or "").lower()
    if "max" in tier or "max" in otype:
        print("opus")
    else:
        print("sonnet")
except Exception:
    sys.exit(1)
PYEOF
}

step_model() {
  hr
  msg "[3b/10] 모델 추천" "[3b/10] Model recommendation"
  hr

  # If a model was preset via env (dry-run/testing), honor it and skip detection.
  if [ -n "$DOGANY_MODEL" ]; then
    msg "모델(사전 설정): $DOGANY_MODEL" "Model (preset): $DOGANY_MODEL"
    return 0
  fi

  local rec=""
  rec="$(recommend_model 2>/dev/null || true)"

  if [ -z "$rec" ]; then
    # Detection failed (no python3 / no file / parse error) -> ask plainly.
    msg "구독 등급을 확인하지 못했습니다. 사용할 모델을 선택하세요:" \
        "Could not detect your subscription tier. Choose a model:"
    msg "  1) sonnet (기본, 빠르고 경제적)" "  1) sonnet (default, fast and economical)"
    msg "  2) opus   (더 강력한 추론, 더 느림/비쌈)" "  2) opus   (stronger reasoning, slower/costlier)"
    local choice=""
    ask choice "번호 선택 [1]: " "Pick a number [1]: " "1"
    case "$choice" in
      2|opus) DOGANY_MODEL="opus" ;;
      *)      DOGANY_MODEL="sonnet" ;;
    esac
    msg "모델: $DOGANY_MODEL" "Model: $DOGANY_MODEL"
    return 0
  fi

  # Recommendation available. Show it; the default answer keeps the recommended
  # model, but the user can switch explicitly.
  if [ "$rec" = "opus" ]; then
    msg "구독 등급 기준 추천 모델: opus (강력한 추론)." \
        "Recommended model for your subscription: opus (stronger reasoning)."
    if confirm "opus 로 설정할까요? (n = sonnet)" "Use opus? (n = sonnet)" "y"; then
      DOGANY_MODEL="opus"
    else
      DOGANY_MODEL="sonnet"
    fi
  else
    msg "구독 등급 기준 추천 모델: sonnet (빠르고 경제적)." \
        "Recommended model for your subscription: sonnet (fast and economical)."
    if confirm "sonnet 으로 설정할까요? (n = opus)" "Use sonnet? (n = opus)" "y"; then
      DOGANY_MODEL="sonnet"
    else
      DOGANY_MODEL="opus"
    fi
  fi
  msg "모델: $DOGANY_MODEL" "Model: $DOGANY_MODEL"
}

# Write the chosen model into the minted instance's .claude/settings.json using
# a python3 JSON round-trip (never sed -- settings.json is real JSON). No-op if
# no model was chosen or the file is missing. Dry-run: print what would happen.
write_instance_model() {
  local root="$1" model="$2"
  [ -n "$model" ] || return 0
  local settings="$root/.claude/settings.json"
  if [ "$DRY_RUN" = "1" ]; then
    msg "[dry-run] settings.json model 필드에 '$model' 기록 예정: $settings" \
        "[dry-run] Would write model '$model' into settings.json: $settings"
    return 0
  fi
  [ -f "$settings" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  if python3 - "$settings" "$model" <<'PYEOF'
import sys, json
path, model = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["model"] = model
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
except Exception:
    sys.exit(1)
PYEOF
  then
    msg "모델 설정: $model ($settings)" "Model set: $model ($settings)"
  else
    msg "[경고] settings.json 에 모델을 기록하지 못했습니다(파일 손상?). 수동 확인: $settings" \
        "[warn] Could not write model into settings.json (malformed?). Check manually: $settings"
  fi
}

# ---------------------------------------------------------------------------
# Step 5: bot token + owner id
# ---------------------------------------------------------------------------
step_bot_token() {
  hr
  msg "[5/10] 텔레그램 봇 토큰 + 오너 ID" "[5/10] Telegram bot token + owner id"
  hr
  msg "이 토큰은 에이전트가 당신의 텔레그램 봇으로 대화하기 위한 열쇠입니다. 봇은 이 ID에 born-locked 되어 오너 전용으로 동작합니다." \
      "This token is the key that lets the agent talk as your Telegram bot. The bot is born-locked to your id and answers only you."
  msg "봇 만들기:" "Create your bot:"
  msg "  1) 텔레그램에서 @BotFather (공식 봇 생성기) 를 엽니다." \
      "  1) Open @BotFather (Telegram's official bot maker) in Telegram."
  msg "  2) /newbot 를 보내고 표시 이름과 사용자명(@...bot)을 정합니다." \
      "  2) Send /newbot and pick a display name and a username (must end in 'bot')."
  msg "  3) BotFather 답장에서 토큰 줄만 복사해 여기에 붙여넣으세요 (형식 예: 1234567890:***)." \
      "  3) Copy the token line from BotFather's reply and paste it here (shape: 1234567890:***)."

  # --- token (capped retries: give up after 5 failed attempts) ---
  local token_tries=0
  while :; do
    local blob=""
    ask blob "봇 토큰만 입력 (예: 1234567890:***): " "Enter the bot token only (e.g. 1234567890:***): " \
        "${DOGANY_MOCK_TOKEN_BLOB:-}"
    BOT_TOKEN="$(extract_token "$blob")"
    if [ -n "$BOT_TOKEN" ]; then
      msg "추출된 토큰: $(mask_token "$BOT_TOKEN")" \
          "Extracted token: $(mask_token "$BOT_TOKEN")"
      if confirm "이 토큰이 맞나요?" "Is this token correct?" "y"; then break; fi
    else
      msg "토큰을 찾지 못했습니다. 다시 붙여넣어 주세요." \
          "No token found. Please paste again."
      [ "$DRY_RUN" = "1" ] && break
    fi
    token_tries=$((token_tries + 1))
    if [ "$token_tries" -ge 5 ]; then
      msg "[에러] 5회 시도 후에도 유효한 토큰을 받지 못했습니다. 설치를 중단합니다." \
          "[ERROR] No valid token after 5 attempts. Aborting install." >&2
      exit 1
    fi
  done

  # Best-effort bot username lookup via getMe (optional, non-fatal).
  lookup_bot_name

  # --- owner id ---
  msg "" ""
  msg "이 숫자 ID 로 봇을 당신에게 잠급니다 -- 다른 사람은 봇과 대화할 수 없습니다." \
      "This numeric id locks the bot to you -- nobody else can talk to it."
  msg "당신의 숫자 텔레그램 ID 가져오기:" "Get your numeric Telegram id:"
  msg "  1) 텔레그램에서 @userinfobot (당신의 ID 를 알려주는 봇) 를 엽니다." \
      "  1) Open @userinfobot (a bot that tells you your id) in Telegram."
  msg "  2) 아무 메시지나 보내면 당신의 숫자 ID 를 답장으로 보냅니다." \
      "  2) Send it any message; it replies with your numeric id."
  msg "  3) 답장에서 숫자 ID 만 입력하세요 (예: 12345678). 건너뛰려면 빈 줄." \
      "  3) Enter just the numeric id from the reply (e.g. 12345678). Leave blank to skip."
  local id_blob=""
  ask id_blob "숫자 ID 만 입력 (예: 12345678, 선택): " \
              "Enter the numeric id only (e.g. 12345678, optional): " \
              "${DOGANY_MOCK_ID_BLOB:-}"
  OWNER_ID="$(extract_user_id "$id_blob")"
  if [ -n "$OWNER_ID" ]; then
    msg "추출된 ID: $OWNER_ID" "Extracted id: $OWNER_ID"
    if ! confirm "이 ID 가 맞나요?" "Is this id correct?" "y"; then OWNER_ID=""; fi
  fi
  if [ -n "$OWNER_ID" ]; then
    msg "봇이 이 ID 로 잠깁니다 (첫 부팅부터 오너 전용)." \
        "Bot will be locked to this id (owner-only from first boot)."
  else
    msg "ID 를 건너뛰었습니다. 봇은 처음 시작 시 일회용 /claim 코드를 로그에 출력합니다." \
        "Skipped id. On first start the bot prints a one-time /claim code to its log."
    msg "텔레그램에서 그 코드로  /claim <코드>  를 보내 오너십을 확보하세요." \
        "Send  /claim <code>  in Telegram to take ownership."
  fi
}

# getMe: resolve the bot @username so the final message can name it.
lookup_bot_name() {
  BOT_NAME=""
  [ -z "$BOT_TOKEN" ] && return 0
  if [ "$DRY_RUN" = "1" ]; then BOT_NAME="mock_bot"; return 0; fi
  command -v curl >/dev/null 2>&1 || return 0
  local resp uname
  resp="$(curl -fsS --max-time 8 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"
  uname="$(printf '%s' "$resp" | grep -oE '"username":"[^"]+"' | head -n1 | sed -E 's/.*:"([^"]+)"/\1/' || true)"
  [ -n "$uname" ] && BOT_NAME="$uname"
}

# ---------------------------------------------------------------------------
# Step 6: email connect (OPTIONAL -- dogany-mailer send capability)
# ---------------------------------------------------------------------------
# Wires the agent's outbound email at install time. Fully optional and
# skip-friendly (default = no). The app password is NEVER echoed anywhere
# (no logs, no dry-run preview). Whitespace in the app password is stripped
# because Google displays it as 4x4 groups with spaces.
step_email_connect() {
  hr
  msg "[6/10] 이메일 연결 (선택)" "[6/10] Email connect (optional)"
  hr
  if ! confirm \
      "에이전트가 메일을 보낼 수 있도록 이메일 계정을 연결할까요? 선택 사항 -- 건너뛰고 나중에 메인 에이전트를 통해 추가할 수 있습니다. 연결한다면 개인 계정이 아닌 전용(신규) 계정을 권장합니다." \
      "Connect an email account so the agent can send mail? Optional -- you can skip and add it later via the main agent. If you connect, a NEW dedicated account (not your personal) is recommended." \
      "n"; then
    # Skip: leave globals blank -> mailer stays 'not connected'.
    EMAIL_ADDRESS=""
    EMAIL_APP_PASSWORD=""
    EMAIL_CC=""
    msg "이메일 연결을 건너뜁니다. 나중에 메인 에이전트에게 요청해 추가할 수 있습니다." \
        "Skipping email connect. You can add it later by asking the main agent."
    return 0
  fi

  ask EMAIL_ADDRESS "에이전트 발신 이메일 (신규 전용 Gmail 권장): " \
                    "agent's sending email (a new dedicated Gmail is recommended): "

  msg "앱 비밀번호는 로그인 비밀번호가 아니라 이 앱 전용 16자리 코드입니다 (계정 비밀번호는 절대 입력하지 마세요)." \
      "The app password is a 16-char app-only code, NOT your login password (never enter your account password)."
  msg "먼저 2단계 인증을 켠 뒤 myaccount.google.com/apppasswords 에서 생성하세요. 4x4 그룹의 공백은 자동으로 제거됩니다." \
      "Turn on 2-step verification first, then create one at myaccount.google.com/apppasswords. The 4x4 spaces are stripped automatically."
  ask_secret EMAIL_APP_PASSWORD "앱 비밀번호 붙여넣기 (입력은 화면에 표시되지 않습니다): " \
                                "Paste the app password (input is hidden): "
  # Google shows the app password as 4x4 groups with spaces -> strip ALL
  # whitespace. Do NOT lowercase. Never echo the value.
  EMAIL_APP_PASSWORD="$(printf '%s' "$EMAIL_APP_PASSWORD" | tr -d '[:space:]')"

  ask EMAIL_CC "본인 이메일 (에이전트가 발신 시 참조로 넣습니다): " \
               "your own email (the agent CCs you on sends): "

  if [ -n "$EMAIL_ADDRESS" ]; then
    msg "이메일 연결됨: $EMAIL_ADDRESS (앱 비밀번호는 저장만 되고 화면에 표시하지 않습니다)." \
        "Email connected: $EMAIL_ADDRESS (app password stored, never displayed)."
  fi
}

# ---------------------------------------------------------------------------
# Step 7 + 8: mint the instance, then write/augment .env atomically
# ---------------------------------------------------------------------------
# The env path inside the instance (matches config.py BOT_DATA_DIR).
env_path_for_root() { printf '%s/.telegram_bot/.env' "$1"; }

# Write a fresh .env body to stdout. Called for both dry-run preview and the
# real augmentation. Uses the exact key names config.py reads.
render_env() {
  local token="$1" ids="$2" locale="$3" tz="$4"
  local email_addr="${5:-}" email_pw="${6:-}" email_cc="${7:-}"
  printf '# Dogany bridge configuration -- generated by install.sh\n'
  printf '# Do NOT commit this file (contains your bot token).\n\n'
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
  printf '# Born-locked: when set, this list is authoritative and claim mode is off.\n'
  printf 'ALLOWED_USER_IDS=%s\n' "$ids"
  printf 'LOCALE=%s\n' "$locale"
  printf 'TZ=%s\n' "$tz"
  printf '# Extra path-guard roots (os.pathsep-separated). Empty for the product.\n'
  printf 'EXTRA_ALLOWED_ROOTS=\n'
  printf '# --- Email (dogany-mailer; optional, connect-time). Blank = not connected.\n'
  printf '# Gmail: use an App Password (not your login password). Never commit real values.\n'
  printf 'EMAIL_ADDRESS=%s\n' "$email_addr"
  printf 'EMAIL_APP_PASSWORD=%s\n' "$email_pw"
  printf 'EMAIL_CC=%s\n' "$email_cc"
  printf '# SMTP_HOST=smtp.gmail.com   # optional, default smtp.gmail.com\n'
  printf '# SMTP_PORT=587              # optional, default 587 (STARTTLS)\n'
}

# Set AGENT_LANG in a minted instance's config/agent.conf to the collected
# install language. Rewrites an existing AGENT_LANG= line (mint always scaffolds
# one, default en) or appends it if somehow absent. Atomic: temp -> mv.
write_agent_lang() {
  local root="$1" lang="${2:-en}"
  local conf="$root/config/agent.conf"
  [ -f "$conf" ] || return 0
  local tmp
  tmp="$(mktemp "${conf}.tmp.XXXXXX")" || return 0
  if grep -q '^AGENT_LANG=' "$conf"; then
    sed "s#^AGENT_LANG=.*#AGENT_LANG=${lang}#" "$conf" > "$tmp" && mv -f "$tmp" "$conf"
  else
    { cat "$conf"; printf 'AGENT_LANG=%s\n' "$lang"; } > "$tmp" && mv -f "$tmp" "$conf"
  fi
  rm -f "$tmp" 2>/dev/null || true
}

step_mint_and_env() {
  hr
  msg "[7-8/10] 에이전트 생성 및 설정 파일 작성" "[7-8/10] Mint the agent and write config"
  hr

  local target env_file backup
  if [ "$DRY_RUN" = "1" ]; then
    target="$DRY_TMP/instance"
    # Lite idempotency check (dry-run branch).
    check_lite_single_agent "$target"
    env_file="$(env_path_for_root "$target")"
    mkdir -p "$(dirname "$env_file")"
    msg "설치 위치(모의): $target" "Install location (mock): $target"
    msg "mint.sh 는 호출하지 않습니다 (dry-run)." "mint.sh is NOT called (dry-run)."
    msg "다음 인자로 호출될 예정: mint.sh --root '$target' --name '$AGENT_NAME' --lang '$DOGANY_LANG' --token <token> $(mint_dep_flag)" \
        "Would call: mint.sh --root '$target' --name '$AGENT_NAME' --lang '$DOGANY_LANG' --token <token> $(mint_dep_flag)"
    msg "작성될 .env: $env_file" "Would write .env: $env_file"
    msg "--- .env 미리보기 ---" "--- .env preview ---"
    # Mask the app password in the preview: the REAL value must never reach
    # stdout. Show *** when set, blank when unset (mirrors token masking).
    local pw_mask=""; [ -n "$EMAIL_APP_PASSWORD" ] && pw_mask="***"
    render_env "$(mask_token "$BOT_TOKEN")" "$OWNER_ID" "$DOGANY_LANG" "$DOGANY_TZ" \
               "$EMAIL_ADDRESS" "$pw_mask" "$EMAIL_CC"
    msg "--- 끝 ---" "--- end ---"
    # Actually write the mock .env into the temp dir so the flow is testable.
    render_env "$BOT_TOKEN" "$OWNER_ID" "$DOGANY_LANG" "$DOGANY_TZ" \
               "$EMAIL_ADDRESS" "$EMAIL_APP_PASSWORD" "$EMAIL_CC" > "$env_file"
    chmod 600 "$env_file" 2>/dev/null || true
    # Show the model write that a real run would perform into settings.json.
    write_instance_model "$target" "$DOGANY_MODEL"
    return 0
  fi

  target="$INSTALL_ROOT"

  # Single-agent idempotency check: stop if a distinct valid instance exists.
  check_lite_single_agent "$target"

  # Re-run handling: if an .env already exists, back it up before touching it.
  env_file="$(env_path_for_root "$target")"
  if [ -f "$env_file" ]; then
    if ! confirm "기존 설정이 발견됨: $env_file . 덮어쓸까요? (백업됨)" \
                 "Existing config found: $env_file . Overwrite? (backed up)" "n"; then
      msg "기존 설정을 유지합니다. mint 는 건너뜁니다." \
          "Keeping existing config. Skipping mint."
      # Still honor the model the wizard just confirmed (reconfigure flow).
      write_instance_model "$target" "$DOGANY_MODEL"
      return 0
    fi
    backup="${env_file}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$env_file" "$backup"
    msg "백업: $backup" "Backed up: $backup"
  fi

  # 6/7a) mint the instance (reuses scripts/mint.sh; passes the real token).
  msg "에이전트를 생성합니다... (수 분 소요 가능)" "Minting the agent... (may take a few minutes)"
  bash "$MINT_SH" --root "$target" --name "$AGENT_NAME" --force \
    --lang "$DOGANY_LANG" --token "$BOT_TOKEN" $(mint_dep_flag)

  # 7a2) record the collected install language in config/agent.conf so the
  #      locale-aware hooks (onboarding first-contact ctx, lifekit i18n) speak
  #      the user's language. mint scaffolds agent.conf write-if-absent (default
  #      AGENT_LANG=en); overwrite that line here with the chosen language.
  write_agent_lang "$target" "$DOGANY_LANG"

  # 7b) augment the .env mint wrote with the keys mint does not manage
  #     (ALLOWED_USER_IDS / LOCALE / TZ / EXTRA_ALLOWED_ROOTS). Atomic: temp->mv.
  env_file="$(env_path_for_root "$target")"
  mkdir -p "$(dirname "$env_file")"
  local tmp_env
  tmp_env="$(mktemp "${env_file}.tmp.XXXXXX")"
  render_env "$BOT_TOKEN" "$OWNER_ID" "$DOGANY_LANG" "$DOGANY_TZ" \
             "$EMAIL_ADDRESS" "$EMAIL_APP_PASSWORD" "$EMAIL_CC" > "$tmp_env"
  chmod 600 "$tmp_env"
  mv -f "$tmp_env" "$env_file"
  msg "설정 파일 작성 완료: $env_file" "Wrote config: $env_file"

  # 7c) write the chosen model into the minted instance's .claude/settings.json
  #     (template default is "sonnet"; overwrite only when a model was chosen).
  write_instance_model "$target" "$DOGANY_MODEL"

  # Record this install root as the single Lite instance.
  write_lite_marker "$target"
}

# --core-only unless voice is opted in.
mint_dep_flag() {
  if [ "$ENABLE_VOICE" = "1" ]; then printf '%s' ""; else printf '%s' "--core-only"; fi
}

# ---------------------------------------------------------------------------
# Step 9: service autostart
# ---------------------------------------------------------------------------
step_service() {
  hr
  msg "[9/10] 자동 시작 서비스 (선택)" "[9/10] Autostart service (optional)"
  hr
  local manual_cmd="bash \"$INSTALL_ROOT/bridge/start.sh\" --path \"$INSTALL_ROOT\""

  msg "자동 시작을 켜면 로그인(또는 재부팅) 때마다 봇이 알아서 실행됩니다 -- 매번 직접 켤 필요가 없습니다 (권장)." \
      "Autostart runs the bot for you on every login (or reboot) -- no need to launch it by hand each time (recommended)."
  if ! confirm "로그인 시 봇을 자동으로 실행하도록 설정할까요? (n = 수동 실행)" \
               "Auto-start the bot at login? (n = run manually)" "y"; then
    SERVICE_CHOICE="manual"
    msg "수동 실행 명령:" "Run manually with:"
    printf '  %s\n' "$manual_cmd"
    return 0
  fi
  SERVICE_CHOICE="auto"

  if [ "$OS_KIND" = "macos" ]; then
    install_launchd "$manual_cmd"
  else
    install_systemd "$manual_cmd"
  fi
}

# Read the launchd Label from a plist. The registered service name is the plist's
# <key>Label</key> value, which does NOT always equal the FILENAME (basename
# minus .plist). Verifying against the filename is a false-negative bug -- e.g.
# a file named com.telegram-skill-bot.<name>.newbridge.plist whose Label key is
# com.telegram-skill-bot.<name>. Prefer plutil (ships on macOS), then PlistBuddy,
# then a grep fallback. Empty output -> caller falls back to the filename.
plist_label() {
  local plist="$1" label=""
  [ -f "$plist" ] || return 0
  if command -v plutil >/dev/null 2>&1; then
    label="$(plutil -extract Label raw -o - "$plist" 2>/dev/null || true)"
  fi
  if [ -z "$label" ] && [ -x /usr/libexec/PlistBuddy ]; then
    label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist" 2>/dev/null || true)"
  fi
  if [ -z "$label" ]; then
    # Fallback: the <string> line immediately after the <key>Label</key> line.
    label="$(grep -A1 '<key>Label</key>' "$plist" 2>/dev/null \
             | grep '<string>' | head -n1 \
             | sed -E 's#.*<string>(.*)</string>.*#\1#')"
  fi
  printf '%s' "$label"
}

install_launchd() {
  local manual_cmd="$1"
  local src plist_name dest label
  # The minted instance carries the plist (renamed to the agent name by mint).
  # Find whatever *.plist mint produced under bridge/.
  src=""
  if [ "$DRY_RUN" = "1" ]; then
    src="$REPO_ROOT/agents/.template/bridge/com.telegram-skill-bot.telegram-agent.newbridge.plist"
  else
    for p in "$INSTALL_ROOT"/bridge/*.plist; do
      [ -e "$p" ] || continue; src="$p"; break
    done
  fi
  if [ -z "$src" ] || [ ! -f "$src" ]; then
    msg "launchd plist 를 찾지 못했습니다. 수동 실행하세요:" \
        "launchd plist not found. Run manually:"
    printf '  %s\n' "$manual_cmd"
    return 0
  fi
  plist_name="$(basename "$src")"
  # Verify against the plist's real Label key, not the filename (they differ:
  # file *.newbridge.plist vs Label com.telegram-skill-bot.<name>). Filename is
  # only a fallback when the Label cannot be read.
  label="$(plist_label "$src")"
  [ -n "$label" ] || label="$(basename "$plist_name" .plist)"
  dest="$HOME/Library/LaunchAgents/$plist_name"

  if [ "$DRY_RUN" = "1" ]; then
    msg "launchd 설치(모의): cp '$src' '$dest'" "Would install launchd: cp '$src' '$dest'"
    msg "그리고: launchctl bootstrap gui/\$UID '$dest'" \
        "Then: launchctl bootstrap gui/\$UID '$dest'"
    return 0
  fi

  mkdir -p "$HOME/Library/LaunchAgents"
  if [ -f "$dest" ]; then
    cp -p "$dest" "${dest}.bak.$(date +%Y%m%d-%H%M%S)"
    msg "기존 plist 백업함." "Backed up existing plist."
  fi
  cp -p "$src" "$dest"
  launchctl bootstrap "gui/$(id -u)" "$dest" 2>/dev/null \
    || launchctl load "$dest" 2>/dev/null || true

  # VERIFY the agent is actually registered before claiming success. Errors above
  # are swallowed (bootstrap vs load vary by macOS version), so the truth comes
  # from launchctl print / list, not from the install command's exit status.
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 \
     || launchctl list 2>/dev/null | grep -q -- "$label"; then
    msg "launchd 서비스 설치됨: $label" "launchd service installed: $label"
  else
    msg "[경고] launchd 서비스 등록을 확인하지 못했습니다: $label" \
        "[WARN] Could not verify the launchd service is registered: $label" >&2
    msg "다음 명령으로 수동 등록하거나 봇을 직접 실행하세요:" \
        "Register it manually with the command below, or run the bot directly:" >&2
    printf '  launchctl bootstrap gui/%s "%s"\n' "$(id -u)" "$dest" >&2
    printf '  %s\n' "$manual_cmd" >&2
  fi
}

install_systemd() {
  local manual_cmd="$1"
  local unit_dir="$HOME/.config/systemd/user"
  local unit_file="$unit_dir/dogany-agent.service"

  if [ "$DRY_RUN" = "1" ]; then
    msg "systemd user unit 작성(모의): $unit_file" "Would write systemd user unit: $unit_file"
    msg "그리고: systemctl --user enable --now dogany-agent.service" \
        "Then: systemctl --user enable --now dogany-agent.service"
    return 0
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    msg "systemctl 이 없습니다. 수동 실행하세요:" "No systemctl found. Run manually:"
    printf '  %s\n' "$manual_cmd"
    return 0
  fi

  mkdir -p "$unit_dir"
  if [ -f "$unit_file" ]; then
    cp -p "$unit_file" "${unit_file}.bak.$(date +%Y%m%d-%H%M%S)"
    msg "기존 unit 백업함." "Backed up existing unit."
  fi
  # Minimal user service (no systemd template shipped in-repo; generate one).
  cat > "$unit_file" <<UNIT
[Unit]
Description=Dogany agent Telegram bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash $INSTALL_ROOT/bridge/start.sh --path $INSTALL_ROOT
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable --now dogany-agent.service 2>/dev/null || true

  # VERIFY the unit is actually active before claiming success. The enable above
  # swallows errors, so the truth comes from is-active, not the exit status.
  if systemctl --user is-active --quiet dogany-agent.service; then
    msg "systemd 서비스 설치됨: dogany-agent.service" "systemd service installed: dogany-agent.service"
  else
    msg "[경고] systemd 서비스가 활성 상태인지 확인하지 못했습니다: dogany-agent.service" \
        "[WARN] Could not verify the systemd service is active: dogany-agent.service" >&2
    msg "다음 명령으로 수동 활성화하거나 봇을 직접 실행하세요:" \
        "Enable it manually with the command below, or run the bot directly:" >&2
    printf '  systemctl --user enable --now dogany-agent.service\n' >&2
    printf '  %s\n' "$manual_cmd" >&2
  fi

  # F3: a systemd --user service is killed on logout unless lingering is enabled.
  # Enabling linger is normally allowed for one's OWN user without sudo. Actually
  # run it (not just advise), then verify Linger=yes; WARN loudly if it fails.
  if loginctl enable-linger "$USER" 2>/dev/null \
     && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ]; then
    msg "로그아웃/재부팅 후에도 유지되도록 linger 를 활성화했습니다." \
        "Enabled linger so the service survives logout/reboot."
  else
    msg "[경고] linger 를 활성화하지 못했습니다. linger 없이는 로그아웃 시 봇이 종료됩니다." \
        "[WARN] Could not enable linger. Without it the bot stops on logout." >&2
    msg "다음 명령을 수동으로 실행하세요 (필요 시 sudo):" \
        "Run this manually (with sudo if needed):" >&2
    printf '  loginctl enable-linger %s\n' "$USER" >&2
  fi
}

# ---------------------------------------------------------------------------
# Step 9b: default routines (nightly consolidate, cleanup)
# ---------------------------------------------------------------------------
# The memory engine's nightly consolidate is a separate scheduled job from the
# bot itself. mint.sh already substitutes placeholders in the
# routine plists (routines/*.plist) and renames them to the agent name. Here we
# actually SCHEDULE them: launchd on macOS, systemd --user timers on Linux.
#
# The default routine set is a small, stable table. Fields (tab-separated):
#   <short-name>  <script-relative-to-INSTALL_ROOT>  <OnCalendar for systemd>
# The macOS path uses the already-minted plists (schedule lives in the plist);
# the Linux path uses the OnCalendar column to generate a systemd timer.
# OnCalendar syntax: systemd.time(7). consolidate/cleanup = daily 04:30.
# classify-inbox is layer-2 of the memory model and IS a standalone routine:
# consolidate (04:30) only dumps the night's notes into inbox.md; classify-inbox
# (05:00, AFTER consolidate) is what routes inbox.md into the topic files. Its
# wrapper (routines/classify-inbox-check.sh) is cheap (7-day marker + inbox-count
# guard) and only calls Opus when there is something to classify.
# weekly-review was removed from the product (its script hardcoded per-user
# Notion UUIDs = PII / dead code for generic users), so it is not scheduled here.
default_routine_set() {
  # short-name <TAB> script <TAB> OnCalendar
  printf '%s\t%s\t%s\n' "consolidate-0430"     "routines/consolidate-0430.sh"      "*-*-* 04:30:00"
  printf '%s\t%s\t%s\n' "classify-inbox-0500"  "routines/classify-inbox-check.sh"  "*-*-* 05:00:00"
  printf '%s\t%s\t%s\n' "cleanup-files"        "routines/cleanup-files.sh"         "*-*-* 04:30:00"
}

step_routines() {
  hr
  msg "[9b/10] 기본 루틴 예약 (야간 공고화 / 정리)" \
      "[9b/10] Scheduling default routines (nightly consolidate / cleanup)"
  hr
  if [ "$SERVICE_CHOICE" = "manual" ]; then
    msg "수동 실행 모드입니다. 루틴은 예약하되, 봇 자동시작은 건너뛴 상태입니다." \
        "Manual-run mode. Routines are still scheduled below; only the bot autostart was skipped."
  fi
  if [ "$OS_KIND" = "macos" ]; then
    schedule_routines_launchd
  else
    schedule_routines_systemd
  fi
}

# Convert a copied routine plist's StartCalendarInterval (Hour/Minute) from the
# user's chosen TZ to the system clock, in place. No-op when TZs match / unknown
# or the plist has no StartCalendarInterval. Prints a one-line notice on change.
# Args: <plist-path> <label> <sys_tz>. Uses python3 to read/rewrite the plist.
convert_plist_time() {
  local plist="$1" label="$2" sys_tz="$3"
  [ -f "$plist" ] || return 0
  [ -n "$DOGANY_TZ" ] && [ -n "$sys_tz" ] && [ "$DOGANY_TZ" != "$sys_tz" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  # Read the current Hour/Minute out of the plist (integers under
  # StartCalendarInterval). plutil raw extract is the portable reader on macOS.
  local hh mm
  hh="$(plutil -extract StartCalendarInterval.Hour raw -o - "$plist" 2>/dev/null || true)"
  mm="$(plutil -extract StartCalendarInterval.Minute raw -o - "$plist" 2>/dev/null || true)"
  [ -n "$hh" ] && [ -n "$mm" ] || return 0
  local conv new_hh new_mm
  conv="$(convert_routine_time "$hh" "$mm" "$DOGANY_TZ" "$sys_tz")"
  new_hh="${conv% *}"; new_mm="${conv#* }"
  if [ "$new_hh" = "$hh" ] && [ "$new_mm" = "$mm" ]; then
    return 0
  fi
  plutil -replace StartCalendarInterval.Hour -integer "$new_hh" "$plist" 2>/dev/null || return 0
  plutil -replace StartCalendarInterval.Minute -integer "$new_mm" "$plist" 2>/dev/null || return 0
  msg "    시간 변환: $(printf '%02d:%02d' "$hh" "$mm") ($DOGANY_TZ) -> $(printf '%02d:%02d' "$new_hh" "$new_mm") 시스템 시각 ($sys_tz)" \
      "    Time convert: $(printf '%02d:%02d' "$hh" "$mm") ($DOGANY_TZ) -> $(printf '%02d:%02d' "$new_hh" "$new_mm") system time ($sys_tz)"
}

# Convert a systemd OnCalendar spec "*-*-* HH:MM:SS" from the user's chosen TZ to
# the system clock. Echoes the (possibly rewritten) spec. No-op when TZs match /
# unknown or the spec has no parseable HH:MM. systemd timers fire on the system
# clock, so the same conversion as the launchd path applies.
convert_oncalendar() {
  local spec="$1" sys_tz="$2"
  if [ -z "$DOGANY_TZ" ] || [ -z "$sys_tz" ] || [ "$DOGANY_TZ" = "$sys_tz" ]; then
    printf '%s' "$spec"; return 0
  fi
  # Extract HH:MM from the time field (last space-separated token "HH:MM:SS").
  local timefield hh mm rest
  timefield="${spec##* }"          # e.g. 04:30:00
  hh="${timefield%%:*}"            # 04
  rest="${timefield#*:}"           # 30:00
  mm="${rest%%:*}"                 # 30
  case "$hh" in ''|*[!0-9]*) printf '%s' "$spec"; return 0 ;; esac
  case "$mm" in ''|*[!0-9]*) printf '%s' "$spec"; return 0 ;; esac
  local conv new_hh new_mm
  conv="$(convert_routine_time "$((10#$hh))" "$((10#$mm))" "$DOGANY_TZ" "$sys_tz")"
  new_hh="${conv% *}"; new_mm="${conv#* }"
  # Rebuild "*-*-* HH:MM:00" (seconds forced to 00, matching the source table).
  printf '*-*-* %02d:%02d:00' "$new_hh" "$new_mm"
}

# --- macOS: load each minted routine plist, verify via launchctl print/list ---
schedule_routines_launchd() {
  local uid; uid="$(id -u)"
  local plist_dir; plist_dir="$INSTALL_ROOT/routines"
  local la_dir="$HOME/Library/LaunchAgents"
  local sys_tz; sys_tz="$(system_tz)"

  if [ "$DRY_RUN" = "1" ]; then
    # In dry-run there is no minted instance; show the template routine plists.
    plist_dir="$REPO_ROOT/agents/.template/routines"
    msg "루틴 plist 로드(모의): $plist_dir/*.plist -> $la_dir" \
        "Would load routine plists (mock): $plist_dir/*.plist -> $la_dir"
    local p
    for p in "$plist_dir"/*.plist; do
      [ -e "$p" ] || continue
      msg "  모의: cp '$(basename "$p")' && launchctl bootstrap gui/$uid" \
          "  mock: cp '$(basename "$p")' && launchctl bootstrap gui/$uid"
    done
    return 0
  fi

  mkdir -p "$la_dir"
  local any=0 ok=0
  local p
  for p in "$plist_dir"/*.plist; do
    [ -e "$p" ] || continue
    any=1
    local name dest label
    name="$(basename "$p")"
    # Verify against the plist's real Label key (see plist_label); the routine
    # filenames happen to match today, but reading the Label is correct and
    # future-proofs against a filename/Label divergence.
    label="$(plist_label "$p")"
    [ -n "$label" ] || label="$(basename "$name" .plist)"
    dest="$la_dir/$name"
    if [ -f "$dest" ]; then
      cp -p "$dest" "${dest}.bak.$(date +%Y%m%d-%H%M%S)"
    fi
    cp -p "$p" "$dest"
    # Convert the routine's user-local time to the system clock IN the copied
    # plist before loading it (launchd fires on the system clock).
    convert_plist_time "$dest" "$label" "$sys_tz"
    # bootstrap (modern) with load fallback; errors swallowed -- truth via print.
    launchctl bootstrap "gui/$uid" "$dest" 2>/dev/null \
      || launchctl load "$dest" 2>/dev/null || true
    if launchctl print "gui/$uid/$label" >/dev/null 2>&1 \
       || launchctl list 2>/dev/null | grep -q -- "$label"; then
      msg "  [OK] 루틴 예약됨: $label" "  [OK] Routine scheduled: $label"
      ok=$((ok+1))
    else
      msg "  [경고] 루틴 등록을 확인하지 못했습니다: $label" \
          "  [WARN] Could not verify routine is registered: $label" >&2
      printf '    launchctl bootstrap gui/%s "%s"\n' "$uid" "$dest" >&2
    fi
  done
  if [ "$any" = "0" ]; then
    msg "[경고] 예약할 루틴 plist 를 찾지 못했습니다: $plist_dir" \
        "[WARN] No routine plists found to schedule: $plist_dir" >&2
  fi
}

# --- Linux: generate a systemd --user .service + .timer per routine, enable,
#     and verify via systemctl --user is-active/is-enabled. ---
schedule_routines_systemd() {
  local unit_dir="$HOME/.config/systemd/user"

  if [ "$DRY_RUN" = "1" ]; then
    msg "systemd 루틴 타이머 생성(모의): $unit_dir/dogany-<name>.{service,timer}" \
        "Would generate systemd routine timers (mock): $unit_dir/dogany-<name>.{service,timer}"
    default_routine_set | while IFS="$(printf '\t')" read -r rn rs rc; do
      [ -n "$rn" ] || continue
      msg "  모의: dogany-$rn.timer OnCalendar=$rc -> $rs" \
          "  mock: dogany-$rn.timer OnCalendar=$rc -> $rs"
    done
    msg "그리고: systemctl --user enable --now dogany-<name>.timer" \
        "Then: systemctl --user enable --now dogany-<name>.timer"
    return 0
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    msg "[경고] systemctl 이 없어 루틴을 예약할 수 없습니다. cron 등으로 수동 예약하세요:" \
        "[WARN] No systemctl; cannot schedule routines. Schedule them manually (e.g. cron):" >&2
    default_routine_set | while IFS="$(printf '\t')" read -r rn rs rc; do
      [ -n "$rn" ] || continue
      printf '    %s  (%s)\n' "$INSTALL_ROOT/$rs" "$rc" >&2
    done
    return 0
  fi

  mkdir -p "$unit_dir"
  local sys_tz; sys_tz="$(system_tz)"
  # Read the table into a plain string first: the while-read loop that generates
  # units must run in THIS shell (not a subshell) so counters survive; feed it
  # via a here-string, not a pipe.
  local table; table="$(default_routine_set)"
  local any=0
  while IFS="$(printf '\t')" read -r rn rs rc; do
    [ -n "$rn" ] || continue
    any=1
    # Convert the OnCalendar time from the user's TZ to the system clock (systemd
    # timers fire on the system clock). No-op when TZs match.
    local rc_conv; rc_conv="$(convert_oncalendar "$rc" "$sys_tz")"
    if [ "$rc_conv" != "$rc" ]; then
      msg "  시간 변환: $rc ($DOGANY_TZ) -> $rc_conv 시스템 시각 ($sys_tz)" \
          "  Time convert: $rc ($DOGANY_TZ) -> $rc_conv system time ($sys_tz)"
      rc="$rc_conv"
    fi
    local svc="$unit_dir/dogany-$rn.service"
    local tmr="$unit_dir/dogany-$rn.timer"
    # service: oneshot that runs the routine script once when the timer fires.
    cat > "$svc" <<SVC
[Unit]
Description=Dogany routine: $rn

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_ROOT/$rs
Environment=HOME=$HOME
WorkingDirectory=$INSTALL_ROOT
SVC
    # timer: persistent so a missed run (machine off) fires at next boot.
    cat > "$tmr" <<TMR
[Unit]
Description=Dogany routine timer: $rn

[Timer]
OnCalendar=$rc
Persistent=true

[Install]
WantedBy=timers.target
TMR
  done <<< "$table"

  if [ "$any" = "0" ]; then
    msg "[경고] 예약할 루틴 정의가 없습니다." "[WARN] No routine definitions to schedule." >&2
    return 0
  fi

  systemctl --user daemon-reload 2>/dev/null || true

  # enable + verify each timer. enable swallows errors; truth via is-enabled.
  while IFS="$(printf '\t')" read -r rn rs rc; do
    [ -n "$rn" ] || continue
    # Report the system-clock time actually written into the timer.
    rc="$(convert_oncalendar "$rc" "$sys_tz")"
    systemctl --user enable --now "dogany-$rn.timer" 2>/dev/null || true
    if systemctl --user is-enabled --quiet "dogany-$rn.timer" 2>/dev/null \
       || systemctl --user is-active --quiet "dogany-$rn.timer" 2>/dev/null; then
      msg "  [OK] 루틴 타이머 예약됨: dogany-$rn.timer ($rc)" \
          "  [OK] Routine timer scheduled: dogany-$rn.timer ($rc)"
    else
      msg "  [경고] 루틴 타이머를 확인하지 못했습니다: dogany-$rn.timer" \
          "  [WARN] Could not verify routine timer: dogany-$rn.timer" >&2
      printf '    systemctl --user enable --now dogany-%s.timer\n' "$rn" >&2
    fi
  done <<< "$table"

  # Routines need linger too (they run while the user is logged out). install_systemd
  # already tries to enable it for the bot; make it robust if this ran standalone.
  if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    loginctl enable-linger "$USER" 2>/dev/null || true
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
      msg "[경고] linger 미활성 -- 로그아웃 시 루틴 타이머가 멈춥니다. 수동:" \
          "[WARN] linger not enabled -- routine timers stop on logout. Manually:" >&2
      printf '    loginctl enable-linger %s\n' "$USER" >&2
    fi
  fi
}

# ---------------------------------------------------------------------------
# Step 10: final message
# ---------------------------------------------------------------------------
step_final() {
  hr
  msg "[10/10] 완료" "[10/10] Done"
  hr
  local at="@your_bot"
  [ -n "$BOT_NAME" ] && at="@$BOT_NAME"
  msg "설치가 완료되었습니다." "Setup complete."
  msg "텔레그램을 열고 봇($at)에게 메시지를 보내세요." \
      "Open Telegram and message your bot ($at)."
  msg "봇이 먼저 인사하고, 짧은 설정(이름, 이모지, 말투)을 대화로 안내합니다." \
      "It will greet you and walk you through a short setup (name, emoji, tone)."
  msg "정체성 온보딩은 앱 설치가 아니라 채팅 안에서 이뤄집니다." \
      "Identity onboarding happens in-chat, not during install."
  msg "생활관리(라이프킷) 묶음은 CRAFT 티어와 함께 제공될 예정입니다. 이번 릴리즈(HAND)는 범용 에이전트로 시작합니다." \
      "The lifekit (life-management) bundle arrives with the CRAFT tier. This release (HAND) starts as a general-purpose agent."
  msg "에이전트와 모든 데이터(기억, 파일, 데이터)는 이 폴더 안에 있습니다: $INSTALL_ROOT" \
      "Your agent and all its data (memory, files, database) live inside this folder: $INSTALL_ROOT"
  msg "백업하려면 이 폴더를 통째로 복사하세요." \
      "To back it up, copy that whole folder."
  msg "시맨틱 메모리 검색은 Ollama + bge-m3 모델을 설치하면 활성화됩니다(선택 사항). 없으면 키워드 검색으로 동작합니다." \
      "Semantic memory search activates if Ollama + bge-m3 are installed (optional); without them the agent falls back to keyword recall."
  if [ "$SERVICE_CHOICE" = "manual" ]; then
    msg "봇을 시작하려면 위의 수동 실행 명령을 사용하세요." \
        "Start the bot with the manual run command shown above."
  fi
  if [ "$DRY_RUN" = "1" ]; then
    hr
    msg "[dry-run] 실제 변경 없음. 임시 디렉토리: $DRY_TMP" \
        "[dry-run] No real changes. Temp dir: $DRY_TMP"
  fi
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
usage() {
  cat <<USAGE
install.sh -- Dogany product first-run installer

  Usage: bash install.sh [--dry-run] [--lang ko|en] [--root DIR] [--name NAME]

  Options:
    --dry-run       run the whole flow with mock inputs into a temp dir;
                    never calls mint.sh and never installs a service.
    --lang ko|en    force the install language (skips auto-detect prompt).
    --root DIR      instance install dir (default: ./agents/main in-repo, gitignored).
    --name NAME     agent name / launchd slug (default: dogany).
    -h, --help      this help.

  Dry-run env knobs (mock inputs):
    DOGANY_MOCK_TOKEN_BLOB   pasted BotFather blob
    DOGANY_MOCK_ID_BLOB      pasted userinfobot blob
    DOGANY_VOICE=1           opt into voice (full deps)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --lang)
      case "$2" in ko|en) : ;; *) echo "invalid --lang: $2 (ko|en)" >&2; exit 1 ;; esac
      DOGANY_LANG="$2"; LANG_FORCED=1; shift 2 ;;
    --root) INSTALL_ROOT="$2"; shift 2 ;;
    --name) AGENT_NAME="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
main() {
  detect_os
  # TCC guard runs right after OS detection, before any interactive work, so a
  # doomed install location is refused immediately (not after the whole wizard).
  tcc_guard
  detect_lang
  detect_tz

  if [ "$DRY_RUN" = "1" ]; then
    DRY_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-install-dryrun.XXXXXX")"
  fi

  hr
  msg "Dogany 설치 마법사" "Dogany install wizard"
  msg "OS: $OS_KIND" "OS: $OS_KIND"
  hr

  step_language
  step_timezone
  check_prereqs
  step_model
  step_dependencies
  step_bot_token
  step_email_connect
  step_mint_and_env
  step_service
  step_routines
  step_final
}

main "$@"
