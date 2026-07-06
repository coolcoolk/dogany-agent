#!/usr/bin/env bash
# install.sh -- Dogany product first-run installer (macOS + Linux).
#
# The flagship, non-developer setup experience. Walks the user through:
#   1. language   2. prerequisites   3. dependencies (voice + embedding)
#   4. model recommendation   5. timezone   6. bot token + owner id (born-locked)
#   7. email connect (optional)   8. mint the agent + write .env
#   9. service autostart (launchd/systemd/manual)   10. final message
#
# DGN-164: heavy downloads (DGN-157 prereq auto-install, Ollama/bge-m3 embedding
# pull, voice deps) are front-loaded right after language so the long downloads
# start early; the light config steps (model pick, timezone, token, email) follow.
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
# faster-whisper model that lands in the instance .env as LOCAL_WHISPER_MODEL
# when voice is enabled. Chosen at the voice step (spec-aware reco preselected).
# Empty = voice disabled (ENABLE_VOICE=0); config.py default "small" still applies.
LOCAL_WHISPER_MODEL="${DOGANY_WHISPER_MODEL:-}"   # small | medium | large-v3
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
# Browser automation (agent-browser) opt-in. 0 = skip (default), 1 = install.
# Presettable via env for dry-run / scripted testing.
ENABLE_BROWSER="${DOGANY_BROWSER:-0}"  # 0 skip (default), 1 install
# DGN-167: bridge model whitelist seeded from subscription tier.
# Max tier -> "sonnet,opus,haiku"; else "sonnet,haiku". Written into .env as
# BRIDGE_MODELS so the /model picker shows the right set on first launch.
# Empty until step_model runs; render_env skips the line when empty.
BRIDGE_MODELS="${BRIDGE_MODELS:-}"

DRY_RUN=0
# In --dry-run, all filesystem writes are redirected under this temp dir and no
# live command (mint / launchctl / systemctl) is ever executed.
DRY_TMP=""

# The Python interpreter check_prereqs resolved to >= 3.11. Exported into the
# mint step (as DOGANY_PYTHON_BIN) so the bridge venv is built with THIS
# interpreter -- important when the system python3 is old (< 3.11) and a newer
# one (e.g. python3.12) was installed alongside it. Empty until check_prereqs
# runs; mint.sh falls back to python3 when it is empty.
PYTHON_BIN=""

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

# ---------------------------------------------------------------------------
# 3a2. Download progress (DGN-164)
# ---------------------------------------------------------------------------
# Big downloads must NEVER sit silent for minutes. Two strategies:
#   - run_with_progress: the tool emits its OWN live progress (ollama pull draws
#     per-layer bars, brew prints download lines). Run it attached to the
#     terminal so that native progress is visible; do not redirect its output.
#   - run_with_heartbeat: the tool is silent (or we deliberately capture its
#     output). Run it in the background and print a one-line elapsed heartbeat
#     every few seconds so the screen never looks frozen.
# Both are NO-OPs of their download in --dry-run (the callers already gate the
# real command behind DRY_RUN, so these are only ever reached on a real run).

# run_with_progress <label> -- CMD...
# Runs CMD with stdout/stderr attached to the terminal so the tool's native
# progress shows. Returns CMD's exit status. The label is informational only;
# callers print their own "downloading <name> (~<size>)" line first.
run_with_progress() {
  local _label="$1"; shift
  "$@"
}

# run_with_heartbeat <name> <approx-size> -- CMD...
# For downloads with no native progress. Announces the name + expected size,
# runs CMD in the background (output discarded), and prints a single rewriting
# heartbeat line "  downloading <name> (~<size>)... elapsed Ns" every ~3s until
# CMD exits. Returns CMD's exit status. On a non-TTY the heartbeat prints one
# line per tick (no cursor rewrite) so logs stay readable. Never used in
# --dry-run (callers gate the real command behind DRY_RUN first).
run_with_heartbeat() {
  local name="$1" size="$2"; shift 2
  msg "  다운로드: ${name} (~${size})..." "  downloading ${name} (~${size})..."
  "$@" >/dev/null 2>&1 &
  local pid=$! elapsed=0 tty=0
  [ -t 1 ] && tty=1
  while kill -0 "$pid" 2>/dev/null; do
    sleep 3; elapsed=$((elapsed + 3))
    if [ "$tty" = "1" ]; then
      # \r + clear-line so the elapsed counter updates in place.
      printf '\r\033[2K  ... %s (~%s), elapsed %ds' "$name" "$size" "$elapsed"
    else
      printf '  ... %s (~%s), elapsed %ds\n' "$name" "$size" "$elapsed"
    fi
  done
  [ "$tty" = "1" ] && printf '\r\033[2K'   # clear the heartbeat line
  wait "$pid" 2>/dev/null
}

# ---------------------------------------------------------------------------
# 3b. Arrow-key selector (DGN-136)
# ---------------------------------------------------------------------------
# select_menu selects among a list of options with up/down + Enter, in the
# OpenClaw/Hermes installer style. Pure bash 3.2 (macOS), zero external deps.
# Returns the 0-based index of the chosen option in the global SELECT_RESULT.
#
# Usage:
#   select_menu "Title line (already localized)" DEFAULT_INDEX opt0 opt1 ...
#   -> echoes the menu, lets the user move with arrows / k / j, confirm with
#      Enter, then sets SELECT_RESULT to the chosen 0-based index.
#
# Interaction model:
#   - Up / [A / k       : move selection up
#   - Down / [B / j     : move selection down
#   - Enter             : confirm current selection
#   - A digit 1..N      : shortcut -- jump to and immediately confirm that row
#   - The cursor is hidden during selection and restored on exit AND on Ctrl-C.
#
# This function assumes an interactive TTY; each caller gates it behind
# select_ui_available and provides the non-TTY typed fallback. It must NOT be
# called in dry-run or with a non-TTY stdin.
SELECT_RESULT=0
select_menu() {
  local title="$1" def="$2"; shift 2
  local opts=("$@")
  local n="${#opts[@]}"
  local cur="$def"
  [ "$cur" -lt 0 ] && cur=0
  [ "$cur" -ge "$n" ] && cur=$((n - 1))

  # tput helpers with graceful degradation when tput/terminfo is missing.
  local has_tput=0
  command -v tput >/dev/null 2>&1 && tput cnorm >/dev/null 2>&1 && has_tput=1
  local rev="" norm=""
  if [ "$has_tput" = "1" ]; then
    rev="$(tput smso 2>/dev/null || true)"   # standout / reverse video
    norm="$(tput rmso 2>/dev/null || true)"
  fi
  # ANSI fallback for reverse video if tput gave nothing.
  [ -z "$rev" ] && rev="$(printf '\033[7m')"
  [ -z "$norm" ] && norm="$(printf '\033[0m')"

  # Hide the cursor and guarantee it is restored on ANY exit path, including
  # Ctrl-C. Both the INT trap and the explicit 0x03-byte handler call cnorm
  # (show cursor) before exiting nonzero, so the terminal is never left with a
  # hidden cursor.
  _select_show_cursor() {
    if [ "$has_tput" = "1" ]; then tput cnorm 2>/dev/null || true
    else printf '\033[?25h'; fi
  }
  # On Ctrl-C: restore the cursor first, then exit nonzero (130 = the
  # conventional SIGINT code). Exiting directly is deterministic and avoids a
  # re-raise race where the interrupted `read` leaves the shell alive.
  _select_on_int() {
    _select_show_cursor
    printf '\n' >&2
    trap - INT
    exit 130
  }
  trap '_select_on_int' INT
  if [ "$has_tput" = "1" ]; then tput civis 2>/dev/null || true
  else printf '\033[?25l'; fi

  # Pre-drain any pending buffered input (paste protection, DGN-142 parity) so a
  # stray paste cannot pre-answer the menu via the raw byte reader below.
  local __junk
  while IFS= read -r -t 1 __junk 2>/dev/null; do :; done

  printf '%s\n' "$title"

  local first=1 key rest
  while :; do
    # Redraw: on the first pass just print; afterwards move the cursor up N lines
    # to overwrite the previous render in place.
    if [ "$first" = "1" ]; then
      first=0
    else
      printf '\033[%dA' "$n"
    fi
    local i=0
    while [ "$i" -lt "$n" ]; do
      printf '\033[2K'   # clear the whole line before redrawing
      if [ "$i" = "$cur" ]; then
        printf '  %s> %s%s\n' "$rev" "${opts[$i]}" "$norm"
      else
        printf '    %s\n' "${opts[$i]}"
      fi
      i=$((i + 1))
    done

    # Read one raw byte. EOF (read fails) -> restore + abort like other prompts.
    IFS= read -rsn1 key || { _select_show_cursor; trap - INT; abort_on_eof; }
    case "$key" in
      $'\003')
        # Ctrl-C. `read -sn1` disables ISIG for the duration of the read, so an
        # interactive Ctrl-C arrives here as a literal 0x03 byte rather than a
        # SIGINT signal. Handle it explicitly: restore the cursor and exit
        # nonzero (130) -- deterministic regardless of the trap firing.
        _select_show_cursor; trap - INT
        printf '\n' >&2
        exit 130
        ;;
      $'\033')
        # Escape sequence: read the two following bytes ([A / [B / [C / [D).
        # -t 1 so a lone ESC does not hang forever.
        IFS= read -rsn2 -t 1 rest 2>/dev/null || rest=""
        case "$rest" in
          '[A'|'OA') cur=$(( (cur - 1 + n) % n )) ;;   # up
          '[B'|'OB') cur=$(( (cur + 1) % n )) ;;       # down
          *) : ;;
        esac
        ;;
      k|K) cur=$(( (cur - 1 + n) % n )) ;;
      j|J) cur=$(( (cur + 1) % n )) ;;
      ''|$'\n'|$'\r')
        # Enter -> confirm current selection.
        _select_show_cursor; trap - INT
        SELECT_RESULT="$cur"
        return 0
        ;;
      [1-9])
        # Digit shortcut: jump to row N (1-based) and confirm immediately.
        if [ "$key" -ge 1 ] && [ "$key" -le "$n" ]; then
          _select_show_cursor; trap - INT
          SELECT_RESULT=$((key - 1))
          return 0
        fi
        ;;
      *) : ;;
    esac
  done
}

# Whether the arrow selector can run: needs an interactive TTY and a sane TERM,
# and never in dry-run (dry-run must stay fully scripted/non-interactive).
select_ui_available() {
  [ "$DRY_RUN" = "1" ] && return 1
  [ -t 0 ] || return 1
  [ "${TERM:-}" = "dumb" ] && return 1
  return 0
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
# DGN-136: when an interactive TTY is available, render as a 2-option arrow-key
# selector ("Yes"/"No", localized ko/en) with the default preselected. In
# dry-run or any non-TTY context it falls back to the original typed y/n prompt
# below, so piped installs / CI / the scripted test battery keep working
# unchanged.
confirm() {
  local ko="$1" en="$2" def="${3:-y}" reply
  local hint="[Y/n]"; [ "$def" = "n" ] && hint="[y/N]"
  if [ "$DRY_RUN" = "1" ]; then
    msgn "$ko " "$en "; printf '%s [dry-run: %s]\n' "$hint" "$def"
    [ "$def" = "y" ]; return
  fi
  # Interactive TTY -> arrow-key selector. select_menu pre-drains buffered input
  # itself (paste protection parity with the typed path below).
  if select_ui_available; then
    local yes_lbl no_lbl title def_idx
    if [ "${DOGANY_LANG:-en}" = "ko" ]; then
      yes_lbl="예"; no_lbl="아니오"; title="$ko"
    else
      yes_lbl="Yes"; no_lbl="No"; title="$en"
    fi
    def_idx=0; [ "$def" = "n" ] && def_idx=1
    select_menu "$title" "$def_idx" "$yes_lbl" "$no_lbl"
    [ "$SELECT_RESULT" = "0" ] && return 0
    return 1
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

# The Windows-side setup (windows/setup-windows.ps1) writes a Linux-side marker
# at this version. install.sh (preflight) and update.sh (drift nag) compare the
# marker against REQUIRED_WINDOWS_SETUP_VERSION. Bump this in lockstep with the
# .ps1's $SetupVersion whenever the required .wslconfig/wsl.conf shape changes.
REQUIRED_WINDOWS_SETUP_VERSION=1
WINDOWS_SETUP_MARKER="/etc/dogany/windows-setup.version"

# True when running inside WSL (WSL1 or WSL2). Cheap, no external deps beyond
# grep; the WSL kernel embeds "microsoft" in osrelease.
is_wsl() {
  grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null
}

# WSL preflight HARD guard. On WSL, the agent needs Windows-side setup done
# FIRST (windows/setup-windows.ps1): systemd as PID 1 and the keep-alive
# task/timeouts. Without them install.sh would half-install silently (systemctl
# exists but PID 1 is init, enable errors are swallowed, the bot ends up dead).
# This runs immediately after detect_os(), before any prompt, and exits
# non-zero with the exact Step 3 command when the environment is not ready.
# No-op off WSL.
wsl_preflight() {
  is_wsl || return 0

  local ps1='powershell.exe -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\<your-linux-username>\dogany-agent\windows\setup-windows.ps1'
  local pid1 marker_ver
  pid1="$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')"

  local ok=1
  [ "$pid1" = "systemd" ] || ok=0
  [ -f "$WINDOWS_SETUP_MARKER" ] || ok=0
  if [ -f "$WINDOWS_SETUP_MARKER" ]; then
    marker_ver="$(tr -dc '0-9' < "$WINDOWS_SETUP_MARKER" 2>/dev/null)"
    marker_ver="${marker_ver:-0}"
    [ "$marker_ver" -ge "$REQUIRED_WINDOWS_SETUP_VERSION" ] 2>/dev/null || ok=0
  fi
  [ "$ok" = "1" ] && return 0

  hr >&2
  msg "[중단] Windows(WSL2) 준비가 완료되지 않았습니다." \
      "[STOP] Windows (WSL2) setup is not complete." >&2
  if [ "$pid1" != "systemd" ]; then
    msg "  - systemd 가 PID 1 이 아닙니다 (현재: ${pid1:-unknown})." \
        "  - systemd is not PID 1 (currently: ${pid1:-unknown})." >&2
  fi
  if [ ! -f "$WINDOWS_SETUP_MARKER" ]; then
    msg "  - Windows 설정 마커가 없습니다 ($WINDOWS_SETUP_MARKER)." \
        "  - Windows setup marker is missing ($WINDOWS_SETUP_MARKER)." >&2
  elif [ "${marker_ver:-0}" -lt "$REQUIRED_WINDOWS_SETUP_VERSION" ] 2>/dev/null; then
    msg "  - Windows 설정이 오래되었습니다 (마커 v${marker_ver:-0}, 필요 v${REQUIRED_WINDOWS_SETUP_VERSION}). setup-windows.ps1 을 다시 실행하세요." \
        "  - Windows setup is stale (marker v${marker_ver:-0}, need v${REQUIRED_WINDOWS_SETUP_VERSION}). Re-run setup-windows.ps1." >&2
  fi
  msg "먼저 Windows PowerShell(일반 사용자, 관리자 아님)에서 아래를 실행하세요:" \
      "First, run this in Windows PowerShell (normal user, NOT administrator):" >&2
  printf '  %s\n' "$ps1" >&2
  msg "그런 다음 Ubuntu 를 다시 열고 install.sh 를 다시 실행하세요." \
      "Then reopen Ubuntu and run install.sh again." >&2
  hr >&2
  exit 1
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

# ---------------------------------------------------------------------------
# 5b. Prerequisite probes + auto-install (DGN-157)
# ---------------------------------------------------------------------------
# The product targets non-terminal users. A missing dep must NOT hard-die with
# "install manually and re-run"; instead we probe everything WITHOUT dying,
# collect the missing set, show ONE list + ONE confirm, then install
# sequentially and re-probe. Anything still missing after the install pass ->
# the original manual instructions for exactly those deps, then exit 1 (never
# worse than today).

# probe_claude -> 0 if `claude` is on PATH, else 1. (Auth is a separate concern,
# handled after presence is assured.)
probe_claude() { command -v claude >/dev/null 2>&1; }

# probe_python -> 0 if a Python >= 3.11 is found. Sets PYTHON_BIN + globals
# PY_FOUND_VER for the OK line. Reuses the existing candidate list + py_version_ok.
PY_FOUND_VER=""
probe_python() {
  PYTHON_BIN=""; PY_FOUND_VER=""
  local cand ver
  for cand in python3 python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
      if py_version_ok "$ver"; then PYTHON_BIN="$cand"; PY_FOUND_VER="$ver"; return 0; fi
    fi
  done
  return 1
}

# probe_git -> 0 if `git` is on PATH.
probe_git() { command -v git >/dev/null 2>&1; }

# Print the manual (fallback) install instructions for one dep, OS-aware. These
# are the SAME instructions the pre-DGN-157 installer printed on hard-fail; they
# are shown only for deps still missing after the auto-install pass.
manual_hint() {
  case "$1" in
    claude)
      msg "  claude CLI 설치: 아래 공식 설치 명령을 실행한 뒤 새 터미널을 여세요:" \
          "  Install claude CLI: run the official installer below, then open a new terminal:"
      # Officially recommended native installer for macOS + Linux + WSL.
      # Verified 2026-07-05 at https://code.claude.com/docs/en/setup
      printf '    %s\n' "curl -fsSL https://claude.ai/install.sh | bash"
      msg "  그런 다음 본인 계정으로 로그인(구독/자체호스팅 모두 가능)한 뒤 다시 실행하세요." \
          "  Then sign in with your own account (subscription or self-host both fine) and re-run."
      ;;
    python)
      if [ "$OS_KIND" = "macos" ]; then
        msg "  Python 3.11+ 설치: brew install python@3.12  (Homebrew 없으면 https://brew.sh)" \
            "  Install Python 3.11+: brew install python@3.12  (no Homebrew? https://brew.sh)"
      else
        msg "  Python 3.11+ 설치: sudo apt-get install python3 python3-venv  (또는 배포판 패키지 매니저)" \
            "  Install Python 3.11+: sudo apt-get install python3 python3-venv  (or your distro's package manager)"
      fi
      ;;
    git)
      if [ "$OS_KIND" = "macos" ]; then
        msg "  git 설치: xcode-select --install  또는  brew install git" \
            "  Install git: xcode-select --install  or  brew install git"
      else
        msg "  git 설치: sudo apt-get install git" \
            "  Install git: sudo apt-get install git"
      fi
      ;;
  esac
}

# Human label for a dep key, used in the summary list.
dep_label() {
  case "$1" in
    claude) msgn "Claude Code CLI (claude)" "Claude Code CLI (claude)" ;;
    python) msgn "Python 3.11+" "Python 3.11+" ;;
    git)    msgn "git" "git" ;;
  esac
}

# --- Homebrew bootstrap (macOS) -----------------------------------------------
# Install Homebrew via the official script when absent. The script manages its
# OWN sudo/password prompt, so we run it attached to the tty (no wrapping).
# Returns 0 if brew is available afterwards. Best-effort PATH pickup for the
# Apple-silicon default prefix so brew is usable in THIS shell.
ensure_brew() {
  command -v brew >/dev/null 2>&1 && return 0
  msg "  Homebrew 가 없습니다. 공식 설치 스크립트로 설치합니다 (비밀번호를 물을 수 있습니다)." \
      "  Homebrew is missing. Installing it via the official script (it may ask for your password)."
  # Official Homebrew installer. It handles its own sudo/password prompting;
  # attach it to the tty rather than capturing output.
  # Source: https://brew.sh
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || true
  # PATH pickup in THIS shell: Apple-silicon installs to /opt/homebrew, Intel to
  # /usr/local. `brew shellenv` exports the right PATH for whichever exists.
  local brew_bin
  for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$brew_bin" ]; then
      eval "$("$brew_bin" shellenv)" 2>/dev/null || true
      break
    fi
  done
  command -v brew >/dev/null 2>&1
}

# --- per-dep installers -------------------------------------------------------
# Each returns 0 on apparent success, non-zero otherwise. They print per-item
# progress. The caller re-probes afterwards, so a false "success" is still
# caught by the re-probe -> manual-hint fallback.

install_claude() {
  msg "  -> Claude Code CLI 설치 중..." "  -> Installing Claude Code CLI..."
  # Officially recommended native installer for macOS AND Linux/WSL.
  # Verified 2026-07-05 at https://code.claude.com/docs/en/setup
  # (native installer script; npm is only an alternative). Binary lands in
  # ~/.local/bin; auto-updates in the background.
  curl -fsSL https://claude.ai/install.sh | bash || return 1
  # PATH pickup in THIS shell: the native installer drops the binary in
  # ~/.local/bin, which is often not yet on PATH in the running shell.
  if ! command -v claude >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/claude" ]; then
      case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac
    fi
  fi
  command -v claude >/dev/null 2>&1
}

install_python() {
  msg "  -> Python 3.11+ 설치 중..." "  -> Installing Python 3.11+..."
  if [ "$OS_KIND" = "macos" ]; then
    ensure_brew || return 1
    brew install python@3.12 || return 1
    # brew keg for python@3.12 may not be first on PATH; brew links python3.12.
    hash -r 2>/dev/null || true
  else
    apt_install python3 python3-venv || return 1
  fi
}

install_git() {
  msg "  -> git 설치 중..." "  -> Installing git..."
  if [ "$OS_KIND" = "macos" ]; then
    ensure_brew || return 1
    brew install git || return 1
    hash -r 2>/dev/null || true
  else
    apt_install git || return 1
  fi
}

# --- Linux/Debian apt helper --------------------------------------------------
# `apt-get update` runs at most ONCE per install (guarded by APT_UPDATED). The
# first sudo is preceded by a plain-language notice that a password prompt may
# appear. If apt-get is absent (non-Debian distro), returns non-zero so the
# caller falls back to manual instructions.
APT_UPDATED=0
apt_install() {
  if ! command -v apt-get >/dev/null 2>&1; then
    msg "  [주의] apt-get 을 찾을 수 없습니다 (Debian/Ubuntu 계열이 아님). 수동 설치가 필요합니다." \
        "  [NOTE] apt-get not found (non-Debian distro). Manual install required."
    return 1
  fi
  if [ "$APT_UPDATED" = "0" ]; then
    msg "  잠시 후 sudo 로 패키지 목록을 갱신합니다. 비밀번호 입력창이 뜰 수 있습니다." \
        "  About to refresh the package list with sudo; a password prompt may appear."
    sudo apt-get update || return 1
    APT_UPDATED=1
  fi
  sudo apt-get install -y "$@" || return 1
}

check_prereqs() {
  hr
  msg "[2/10] 사전 조건 확인" "[2/10] Checking prerequisites"
  hr

  # --- probe everything WITHOUT dying; collect the missing set (space list) ---
  local missing=""
  if probe_claude; then
    local cver
    cver="$(claude --version 2>/dev/null | head -n1 || true)"
    msg "  [OK] claude CLI: ${cver:-발견됨}" "  [OK] claude CLI: ${cver:-found}"
  else
    msg "  [미설치] claude CLI 를 PATH 에서 찾을 수 없습니다." \
        "  [MISSING] claude CLI not found on PATH."
    missing="$missing claude"
  fi

  if probe_python; then
    msg "  [OK] Python: $PY_FOUND_VER ($PYTHON_BIN)" "  [OK] Python: $PY_FOUND_VER ($PYTHON_BIN)"
  else
    # python3 may exist but be < 3.11 -- that still counts as missing; we install
    # a newer one ALONGSIDE (never uninstall) and re-resolve PYTHON_BIN after.
    local old_ver=""
    if command -v python3 >/dev/null 2>&1; then
      old_ver="$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    fi
    if [ -n "$old_ver" ]; then
      msg "  [미설치] Python 3.11 이상이 필요합니다 (현재 python3: $old_ver)." \
          "  [MISSING] Python 3.11+ is required (current python3: $old_ver)."
    else
      msg "  [미설치] Python 3.11 이상이 필요합니다." \
          "  [MISSING] Python 3.11+ is required."
    fi
    missing="$missing python"
  fi

  if probe_git; then
    msg "  [OK] git: $(git --version 2>/dev/null | head -n1)" \
        "  [OK] git: $(git --version 2>/dev/null | head -n1)"
  else
    msg "  [미설치] git 이 필요합니다." "  [MISSING] git is required."
    missing="$missing git"
  fi

  # Trim leading space.
  missing="${missing# }"

  if [ -n "$missing" ]; then
    resolve_missing_deps "$missing"
  fi

  # --- claude AUTH probe (presence != authenticated). Only meaningful once the
  # CLI is actually present; runs after any install/re-probe above. ---
  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] claude 인증 프로브/로그인은 건너뜁니다." \
        "  [dry-run] Skipping claude auth probe/login."
  elif probe_claude; then
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

  # --- mint.sh must exist ---
  if [ ! -f "$MINT_SH" ]; then
    msg "  [실패] mint.sh 를 찾을 수 없습니다: $MINT_SH" \
        "  [FAIL] mint.sh not found: $MINT_SH" >&2
    exit 1
  fi
}

# resolve_missing_deps "<space-separated dep keys>" -- present ONE summary list,
# ONE confirm (default Y), install sequentially with progress, RE-PROBE, and on
# any still-missing dep print the manual instructions and exit 1. Handles
# --dry-run (report only) and non-tty stdin (list + manual, exit 1) exactly like
# the pre-DGN-157 behavior for those paths.
resolve_missing_deps() {
  local missing="$1" dep
  hr
  msg "다음 항목이 없어 설치가 필요합니다:" "The following prerequisites are missing and need to be installed:"
  for dep in $missing; do
    printf '  - %s\n' "$(dep_label "$dep")"
  done
  hr

  # --dry-run: report what WOULD be installed, install nothing, continue.
  if [ "$DRY_RUN" = "1" ]; then
    msg "[dry-run] 위 항목을 자동 설치할 예정입니다 (실제로는 아무것도 설치하지 않음)." \
        "[dry-run] Would auto-install the above (nothing is actually installed)."
    return 0
  fi

  # Non-interactive stdin (not a tty): do not hang on the confirm. Print the
  # manual instructions and exit 1 -- same as the pre-DGN-157 behavior.
  if [ ! -t 0 ]; then
    msg "비대화형 입력(파이프)이라 자동 설치를 건너뜁니다. 아래를 수동으로 설치한 뒤 다시 실행하세요:" \
        "Non-interactive input (piped) -- skipping auto-install. Install the following manually and re-run:"
    for dep in $missing; do manual_hint "$dep"; done
    exit 1
  fi

  # ONE confirm (default Y).
  if ! confirm "지금 자동으로 설치할까요?" "Install them now automatically?" "y"; then
    msg "자동 설치를 건너뜁니다. 아래를 수동으로 설치한 뒤 다시 실행하세요:" \
        "Skipping auto-install. Install the following manually and re-run:"
    for dep in $missing; do manual_hint "$dep"; done
    exit 1
  fi

  # Install sequentially with per-item progress. A failed installer is not fatal
  # here; the re-probe below is the single source of truth.
  for dep in $missing; do
    case "$dep" in
      claude) install_claude || true ;;
      python) install_python || true ;;
      git)    install_git || true ;;
    esac
  done

  # --- RE-PROBE everything; collect what is STILL missing ---
  local still=""
  probe_claude || still="$still claude"
  probe_python || still="$still python"
  probe_git    || still="$still git"
  still="${still# }"

  if [ -n "$still" ]; then
    hr
    msg "[실패] 다음 항목을 여전히 설치하지 못했습니다. 수동으로 설치한 뒤 다시 실행하세요:" \
        "[FAIL] The following are still missing. Install them manually and re-run:"
    for dep in $still; do manual_hint "$dep"; done
    hr
    exit 1
  fi

  hr
  msg "  모든 사전 조건이 준비되었습니다." "  All prerequisites are ready."
  # Re-print the resolved versions so the record is clear post-install.
  if probe_python; then
    msg "  [OK] Python: $PY_FOUND_VER ($PYTHON_BIN)" "  [OK] Python: $PY_FOUND_VER ($PYTHON_BIN)"
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
  msg "[5/10] 타임존" "[5/10] Timezone"
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
        local i=0 line pick=""
        # Collect candidates into an array (also mirrored into positional-safe
        # c1..c8 for the typed-fallback path).
        local c1="" c2="" c3="" c4="" c5="" c6="" c7="" c8=""
        local cand_arr=()
        while IFS= read -r line; do
          [ -n "$line" ] || continue
          i=$((i + 1))
          eval "c$i=\$line"
          cand_arr+=("$line")
        done <<EOF
$cands
EOF
        # DGN-136: arrow-key selector with a trailing "type again" option on a
        # TTY; the original numbered typed menu otherwise.
        if select_ui_available; then
          local tz_title again_lbl
          if [ "${DOGANY_LANG:-en}" = "ko" ]; then
            tz_title="정확히 일치하지 않습니다. '$tz_in' 후보 (선택하거나 다시 입력):"
            again_lbl="(다시 입력)"
          else
            tz_title="No exact match. Candidates for '$tz_in' (pick one or type again):"
            again_lbl="(type again)"
          fi
          select_menu "$tz_title" 0 "${cand_arr[@]}" "$again_lbl"
          # Last option == "type again" -> leave tz_in as-is and reprompt.
          if [ "$SELECT_RESULT" -lt "$i" ]; then
            tz_in="${cand_arr[$SELECT_RESULT]}"
            if tz_valid "$tz_in"; then DOGANY_TZ="$tz_in"; break; fi
          fi
        else
          msg "정확히 일치하지 않습니다. '$tz_in' 후보:" \
              "No exact match. Candidates for '$tz_in':"
          local j=1
          while [ "$j" -le "$i" ]; do
            eval "line=\$c$j"
            printf '  %d) %s\n' "$j" "$line"
            j=$((j + 1))
          done
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
  msg "[3/10] 의존성" "[3/10] Dependencies"
  hr
  msg "기본은 핵심 의존성만 설치합니다 (빠르고 가벼움)." \
      "Default installs core dependencies only (fast and light)."
  # DGN-146: voice is now a MODEL choice, not a yes/no. Pick the faster-whisper
  # model (or skip); the choice lands in the instance .env as LOCAL_WHISPER_MODEL.
  step_voice_model

  if [ "$ENABLE_VOICE" = "1" ]; then
    msg "  -> 전체 설치 (음성 포함, 모델: ${LOCAL_WHISPER_MODEL})" \
        "  -> full install (voice included, model: ${LOCAL_WHISPER_MODEL})"
  else
    msg "  -> 핵심 전용 설치 (--core-only)" "  -> core-only install (--core-only)"
  fi

  # Optional semantic-memory embedding backend (Ollama + bge-m3).
  step_embedding

  # Optional browser automation (agent-browser CLI + Chrome for Testing, ~684MB).
  step_browser
}

# DGN-146: voice model picker. Shows the device spec header (disk free/total +
# RAM) and a menu where every option carries the model's on-disk size, so BOTH
# numbers -- each model's size AND the device's capacity -- are visible side by
# side (형님's acceptance rule). The spec-computed recommendation (whisper_reco)
# is preselected. Rides the DGN-136 arrow selector when a TTY is available and
# falls back to a typed numbered prompt otherwise (non-TTY/dry-run stay scripted).
#
# faster-whisper (CTranslate2) approx on-disk sizes, stated conservatively (~):
#   small ~0.5GB, medium ~1.5GB, large-v3 ~3GB (tiny/base omitted -- too weak).
# Sets ENABLE_VOICE (0/1) and, when enabled, LOCAL_WHISPER_MODEL to the choice.
step_voice_model() {
  # Preset via env (dry-run / scripted): honor an explicit choice, don't prompt.
  # DOGANY_WHISPER_MODEL set -> that model; ENABLE_VOICE=1 without a model ->
  # fall through to the picker default (reco); ENABLE_VOICE unset/0 -> menu.
  if [ -n "$LOCAL_WHISPER_MODEL" ]; then
    ENABLE_VOICE=1
    return 0
  fi

  # Spec header: model sizes are shown per-option below; here we show the device
  # capacity (disk + RAM) so the user weighs size against free space.
  msg "음성 입력(선택): 로컬 faster-whisper 모델을 내려받아 음성 메시지를 받아씁니다." \
      "Voice input (optional): downloads a local faster-whisper model to transcribe voice messages."
  disk_free_line
  ram_line

  local reco; reco="$(whisper_reco || true)"
  [ -n "$reco" ] || reco="small"

  # Menu rows (index-aligned with the model ids below). Row 0 = skip.
  local opt_skip opt_small opt_medium opt_large
  if [ "${DOGANY_LANG:-en}" = "ko" ]; then
    opt_skip="음성 입력 건너뛰기"
    opt_small="small (~0.5GB)"
    opt_medium="medium (~1.5GB, 한국어 이름 인식이 더 정확)"
    opt_large="large-v3 (~3GB, 최고 품질)"
  else
    opt_skip="skip voice input"
    opt_small="small (~0.5GB)"
    opt_medium="medium (~1.5GB, better for Korean names)"
    opt_large="large-v3 (~3GB, best quality)"
  fi
  # models[i] is the model id for menu row i (row 0 = skip -> empty).
  local models=("" "small" "medium" "large-v3")

  # Default (preselected) row = the recommended model's row.
  local def_idx=1
  case "$reco" in
    medium)   def_idx=2 ;;
    large-v3) def_idx=3 ;;
    *)        def_idx=1 ;;
  esac
  msg "  권장(선택된 항목): ${reco}" "  Recommended (preselected): ${reco}"

  local title choice_idx
  if [ "${DOGANY_LANG:-en}" = "ko" ]; then
    title="음성 입력 모델을 선택하세요:"
  else
    title="Choose the voice input model:"
  fi

  if select_ui_available; then
    select_menu "$title" "$def_idx" "$opt_skip" "$opt_small" "$opt_medium" "$opt_large"
    choice_idx="$SELECT_RESULT"
  else
    # Typed numbered fallback (non-TTY / no selector). 1-based prompt; blank
    # accepts the recommended default. Drain buffered paste lines first.
    drain_buffered_lines >/dev/null 2>&1 || true
    msg "$title" "$title"
    printf '  1) %s\n' "$opt_skip"
    printf '  2) %s\n' "$opt_small"
    printf '  3) %s\n' "$opt_medium"
    printf '  4) %s\n' "$opt_large"
    local pick=""
    ask pick "번호 선택 (빈 줄 = 권장 ${reco}): " "Pick a number (blank = recommended ${reco}): "
    case "$pick" in
      1) choice_idx=0 ;;
      2) choice_idx=1 ;;
      3) choice_idx=2 ;;
      4) choice_idx=3 ;;
      ''|*[!0-9]*) choice_idx="$def_idx" ;;   # blank / junk -> recommended
      *) choice_idx="$def_idx" ;;
    esac
  fi

  if [ "$choice_idx" = "0" ]; then
    ENABLE_VOICE=0
    LOCAL_WHISPER_MODEL=""
    msg "음성 입력을 건너뜁니다 (나중에 추가 가능)." \
        "Skipping voice input (you can add it later)."
    return 0
  fi
  ENABLE_VOICE=1
  LOCAL_WHISPER_MODEL="${models[$choice_idx]}"
  [ -n "$LOCAL_WHISPER_MODEL" ] || LOCAL_WHISPER_MODEL="$reco"
  msg "선택된 음성 모델: ${LOCAL_WHISPER_MODEL}" "Selected voice model: ${LOCAL_WHISPER_MODEL}"
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

# System RAM in whole GB (floor), or empty when undetectable. Thin wrapper over
# system_ram_kb so the tier logic and the display line share one source of truth.
# Overridable for tests via DOGANY_TEST_RAM_GB (an integer GB), which lets the
# self-test battery stub a machine spec without real hardware.
system_ram_gb() {
  if [ -n "${DOGANY_TEST_RAM_GB:-}" ]; then printf '%s' "$DOGANY_TEST_RAM_GB"; return 0; fi
  local kb; kb="$(system_ram_kb || true)"
  [ -n "$kb" ] && printf '%s' "$((kb / 1024 / 1024))"
  return 0
}

# Free space in GB on the $HOME volume (floor), or empty when undetectable.
# Same df -k parse as disk_free_line, exposed as a number for the tier logic.
# Overridable for tests via DOGANY_TEST_DISK_GB.
disk_free_gb() {
  if [ -n "${DOGANY_TEST_DISK_GB:-}" ]; then printf '%s' "$DOGANY_TEST_DISK_GB"; return 0; fi
  local avail_kb
  avail_kb="$(df -k "$HOME" 2>/dev/null | awk 'NR>1 && $2 ~ /^[0-9]+$/ {print $4; exit}')"
  case "$avail_kb" in ''|*[!0-9]*) return 0 ;; esac
  printf '%s' "$((avail_kb / 1024 / 1024))"
  return 0
}

# DGN-146: bilingual RAM line, mirrors disk_free_line so every local-model step
# can show BOTH numbers (model size + device capacity) side by side. Fail-open:
# prints nothing if RAM is undetectable. Always returns 0.
ram_line() {
  local gb; gb="$(system_ram_gb || true)"
  case "$gb" in ''|*[!0-9]*) return 0 ;; esac
  msg "  메모리(RAM): ${gb}GB" "  RAM: ${gb} GB"
  return 0
}

# DGN-146: spec-aware faster-whisper recommendation. Prints the recommended model
# name (small | medium | large-v3) for the detected RAM + free disk. Tiers (per
# 형님's rule + faster-whisper/CTranslate2 size knowledge, sizes conservative ~):
#   RAM <8GB           -> small     (~0.5GB; runs anywhere)
#   RAM 8-16GB         -> small     (medium listed but not preselected)
#   RAM >=16GB, disk>8 -> medium    (~1.5GB; markedly better for Korean names)
#   RAM >=32GB, disk>12-> large-v3  (~3GB; best, only on roomy machines)
# Unknown RAM (empty) is treated conservatively as the smallest tier -> small.
# Pure integer comparisons; stub RAM/disk via the DOGANY_TEST_* overrides above.
whisper_reco() {
  local ram; ram="$(system_ram_gb || true)"
  local disk; disk="$(disk_free_gb || true)"
  case "$ram" in ''|*[!0-9]*) printf 'small'; return 0 ;; esac
  case "$disk" in ''|*[!0-9]*) disk=0 ;; esac
  if [ "$ram" -ge 32 ] && [ "$disk" -gt 12 ]; then printf 'large-v3'; return 0; fi
  if [ "$ram" -ge 16 ] && [ "$disk" -gt 8 ];  then printf 'medium';   return 0; fi
  printf 'small'
  return 0
}

# ---------------------------------------------------------------------------
# Step 4b: optional embedding backend (semantic memory)
# ---------------------------------------------------------------------------
# Semantic (cross-lingual) memory recall needs a local embedding model: Ollama
# running bge-m3 (~1.2GB pull, verified via ollama registry). Fully optional --
# without it the agent falls back to keyword recall. ALL failures fail-open (warn
# + continue install). Dry-run only prints decisions.
step_embedding() {
  hr
  msg "  [3b] 시맨틱 메모리 임베딩 (선택: Ollama + bge-m3, ~1.2GB)" \
      "  [3b] Semantic memory embedding (optional: Ollama + bge-m3, ~1.2GB)"
  hr

  local have_ollama=0
  command -v ollama >/dev/null 2>&1 && have_ollama=1

  # Already installed AND model present -> nothing to do.
  if [ "$have_ollama" = "1" ] && ollama list 2>/dev/null | grep -qi 'bge-m3'; then
    msg "  [OK] Ollama + bge-m3 이미 설치됨 -> 시맨틱 메모리 사용 가능." \
        "  [OK] Ollama + bge-m3 already present -> semantic memory available."
    return 0
  fi

  # DGN-146: state BOTH numbers side by side -- the model's download size and the
  # device's capacity (RAM + free disk) -- so the choice is informed. bge-m3 pull
  # is ~1.2GB (verified figure; earlier ~1.5GB copy was a leftover error).
  msg "  bge-m3 다운로드 크기: ~1.2GB (임베딩 추론 시 RAM 약 1.5-2GB)." \
      "  bge-m3 download size: ~1.2GB (inference RAM roughly 1.5-2GB)."
  # DGN-144: show the device disk state next to the RAM check.
  disk_free_line
  ram_line

  # DGN-146 recommendation boundary: RAM >=8GB -> recommend bge-m3 (default YES);
  # <8GB (or unknown) -> recommend SKIP (keyword search works without it).
  # bge-m3 needs ~1.5-2GB RAM at inference; on an 8GB box that leaves room, below
  # it competes with everything else -> keyword-only is the safer default.
  local ram_gb; ram_gb="$(system_ram_gb || true)"
  local ram_disp="?"
  [ -n "$ram_gb" ] && ram_disp="$ram_gb"
  local def="n"
  if [ -n "$ram_gb" ] && [ "$ram_gb" -ge 8 ]; then
    def="y"
    msg "  감지된 RAM: ${ram_disp}GB (8GB 이상) -> bge-m3 임베딩 설치 권장." \
        "  Detected RAM: ${ram_disp}GB (8GB+) -> installing bge-m3 embeddings is recommended."
  else
    def="n"
    msg "  감지된 RAM: ${ram_disp}GB (8GB 미만/불명) -> 건너뛰기 권장 (임베딩 없이도 키워드 검색이 동작합니다)." \
        "  Detected RAM: ${ram_disp}GB (<8GB or unknown) -> skip recommended (keyword search works without it)."
  fi

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
        # DGN-164: let brew print its own download/progress output (do not
        # silence it) so the ~hundreds-of-MB Ollama install is not a silent gap.
        msg "  Ollama 설치 중 (brew, 진행 상황이 아래에 표시됩니다)..." \
            "  Installing Ollama (brew; progress shown below)..."
        if ! run_with_progress "ollama (brew)" brew install ollama; then
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

  # DGN-164: announce size, then show progress instead of a multi-minute silence.
  # On a TTY, `ollama pull` draws its own live per-layer progress bars -> run it
  # attached (run_with_progress). Off a TTY (piped/CI), those bars would spam raw
  # control codes, so fall back to a periodic elapsed heartbeat instead.
  msg "  bge-m3 모델을 내려받습니다 (~1.2GB, 시간이 걸릴 수 있습니다)..." \
      "  Pulling bge-m3 (~1.2GB, this can take a while)..."
  local pull_rc=0
  if [ -t 1 ]; then
    run_with_progress "bge-m3" ollama pull bge-m3 || pull_rc=$?
  else
    run_with_heartbeat "bge-m3" "1.2GB" ollama pull bge-m3 || pull_rc=$?
  fi
  if [ "$pull_rc" = "0" ]; then
    msg "  [OK] bge-m3 준비 완료 -> 시맨틱 메모리 사용 가능." \
        "  [OK] bge-m3 ready -> semantic memory available."
  else
    msg "  [경고] bge-m3 다운로드 실패. 키워드 검색으로 계속합니다. 나중에  ollama pull bge-m3  로 재시도하세요." \
        "  [WARN] Failed to pull bge-m3. Continuing with keyword recall; retry later with  ollama pull bge-m3 ." >&2
  fi
}

# ---------------------------------------------------------------------------
# Step 4c: optional browser automation (agent-browser CLI + Chrome for Testing)
# ---------------------------------------------------------------------------
# agent-browser (Vercel Labs) ships Chrome for Testing (~684 MB) and a compact
# accessibility-tree snapshot format. Token cost: ~7K per 10-step task (vs ~114K
# for Playwright MCP). Default = NO -- the download is large and most users do not
# need browser automation.
#
# Activation: creates a symlink from .claude/skills/agent-browser pointing at the
# dormant bundle dir (.claude/skills-bundle/agent-browser). The CLI itself is
# installed globally via npm. Fail-open: any install failure warns and continues.
step_browser() {
  hr
  msg "  [4c] 브라우저 자동화 (선택: agent-browser CLI + Chrome for Testing, ~684MB)" \
      "  [4c] Browser automation (optional: agent-browser CLI + Chrome for Testing, ~684 MB)"
  hr

  # Preset via env (dry-run / scripted): honor an explicit choice.
  if [ "${DOGANY_BROWSER:-}" = "1" ]; then
    ENABLE_BROWSER=1
  fi

  # Already opted in via env -> skip the prompt, go straight to install.
  if [ "$ENABLE_BROWSER" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    _browser_install
    return 0
  fi

  msg "  Chrome for Testing 다운로드 크기: ~684MB. agent-browser CLI 는 npm 으로 전역 설치됩니다." \
      "  Chrome for Testing download size: ~684 MB. The agent-browser CLI is installed globally via npm."
  msg "  Node.js 가 PATH 에 있어야 합니다." \
      "  Node.js must be on PATH."
  msg "  에이전트가 웹사이트 접속, 폼 입력, 스크린샷 등이 불필요하다면 건너뛰세요." \
      "  Skip if you do not need the agent to open websites, fill forms, or take screenshots."
  msg "  나중에 언제든  npm install -g agent-browser && agent-browser install  로 추가할 수 있습니다." \
      "  You can add it anytime later with:  npm install -g agent-browser && agent-browser install"
  disk_free_line

  # Default NO: large download, not needed by every user.
  if ! confirm \
      "지금 브라우저 자동화를 설치할까요? (~684MB 다운로드, 기본값 N)?" \
      "Install browser automation now? (~684 MB download, default N)?" \
      "n"; then
    ENABLE_BROWSER=0
    msg "  브라우저 자동화를 건너뜁니다. 나중에 언제든  npm install -g agent-browser && agent-browser install  로 추가할 수 있습니다." \
        "  Skipping browser automation. Add it anytime later with:  npm install -g agent-browser && agent-browser install"
    return 0
  fi

  ENABLE_BROWSER=1

  if [ "$DRY_RUN" = "1" ]; then
    msg "  [dry-run] npm install -g agent-browser 와 agent-browser install 을 수행할 예정 (실제 실행 안 함)." \
        "  [dry-run] Would run npm install -g agent-browser and agent-browser install (not executed)."
    msg "  [dry-run] 스킬 심볼릭 링크 생성 예정: .claude/skills/agent-browser -> ../skills-bundle/agent-browser" \
        "  [dry-run] Would create skill symlink: .claude/skills/agent-browser -> ../skills-bundle/agent-browser"
    return 0
  fi

  _browser_install
}

# Internal: run the actual install steps for browser automation. Fail-open.
_browser_install() {
  # 1. npm global install of the CLI.
  if ! command -v npm >/dev/null 2>&1; then
    msg "  [경고] npm 이 없습니다. 브라우저 CLI 를 설치하지 못했습니다. 나중에 Node.js 설치 후 수동으로 실행하세요:" \
        "  [WARN] npm not found; cannot install the browser CLI. After installing Node.js, run manually:" >&2
    printf '    npm install -g agent-browser && agent-browser install\n' >&2
    ENABLE_BROWSER=0
    return 0
  fi

  msg "  agent-browser CLI 설치 중 (npm)..." "  Installing agent-browser CLI (npm)..."
  if ! npm install -g agent-browser >/dev/null 2>&1; then
    msg "  [경고] npm install -g agent-browser 실패. 수동 설치 후 계속할 수 있습니다." \
        "  [WARN] npm install -g agent-browser failed. You can install manually and continue." >&2
    ENABLE_BROWSER=0
    return 0
  fi

  # 2. Download Chrome for Testing.
  msg "  Chrome for Testing 다운로드 중 (~684MB, 시간이 걸릴 수 있습니다)..." \
      "  Downloading Chrome for Testing (~684 MB, this can take a while)..."
  if ! agent-browser install >/dev/null 2>&1; then
    msg "  [경고] agent-browser install 실패 (네트워크 또는 디스크 공간 부족일 수 있습니다). 수동으로 재시도:  agent-browser install" \
        "  [WARN] agent-browser install failed (network or disk space issue). Retry manually:  agent-browser install" >&2
    ENABLE_BROWSER=0
    return 0
  fi

  # 3. Skill symlink activation is deferred. step_browser runs before mint, so
  # the instance directory does not exist yet. browser_activate_skill() is called
  # after step_mint_and_env completes (see 7d in that function).
  msg "  [OK] agent-browser CLI 설치 완료. 스킬은 에이전트 생성 후 활성화됩니다." \
      "  [OK] agent-browser CLI installed. Skill will be activated after the agent is minted."
}

# Activate the agent-browser skill symlink in a minted instance.
# Called from step_mint_and_env (after mint) when ENABLE_BROWSER=1.
# No-op in dry-run (printed there already).
browser_activate_skill() {
  local root="$1"
  [ "$ENABLE_BROWSER" = "1" ] || return 0
  [ "$DRY_RUN" = "1" ] && return 0

  local bundle_dir="$root/.claude/skills-bundle/agent-browser"
  local link_dir="$root/.claude/skills"
  local link="$link_dir/agent-browser"

  # The bundle dir must exist in the minted instance (copied from .template).
  if [ ! -d "$bundle_dir" ]; then
    msg "  [경고] 스킬 번들 디렉토리를 찾지 못했습니다: $bundle_dir" \
        "  [WARN] Skill bundle directory not found: $bundle_dir" >&2
    return 0
  fi

  mkdir -p "$link_dir"
  # Idempotent: remove stale link before creating.
  [ -L "$link" ] && rm -f "$link"
  ln -s "../skills-bundle/agent-browser" "$link"
  msg "  [OK] 브라우저 자동화 스킬 활성화됨: $link" \
      "  [OK] Browser automation skill activated: $link"
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
        # DGN-167: max tier gets all three models in the /model picker.
        print("sonnet,opus,haiku")
    else:
        print("sonnet")
        # DGN-167: non-max tier: sonnet + haiku (no opus in the picker).
        print("sonnet,haiku")
except Exception:
    sys.exit(1)
PYEOF
}

step_model() {
  hr
  msg "[4/10] 모델 추천" "[4/10] Model recommendation"
  hr

  # If a model was preset via env (dry-run/testing), honor it and skip detection.
  if [ -n "$DOGANY_MODEL" ]; then
    msg "모델(사전 설정): $DOGANY_MODEL" "Model (preset): $DOGANY_MODEL"
    return 0
  fi

  local rec_out="" rec="" rec_bridge=""
  rec_out="$(recommend_model 2>/dev/null || true)"
  # recommend_model prints two lines: line1=model, line2=bridge_models.
  rec="$(printf '%s\n' "$rec_out" | head -1)"
  rec_bridge="$(printf '%s\n' "$rec_out" | sed -n '2p')"

  if [ -z "$rec" ]; then
    # Detection failed (no python3 / no file / parse error) -> ask plainly.
    msg "구독 등급을 확인하지 못했습니다. 사용할 모델을 선택하세요:" \
        "Could not detect your subscription tier. Choose a model:"
    # DGN-136: arrow-key selector on a TTY; typed numbered prompt otherwise.
    if select_ui_available; then
      local m_opt1 m_opt2 m_title
      if [ "${DOGANY_LANG:-en}" = "ko" ]; then
        m_title="사용할 모델을 선택하세요:"
        m_opt1="sonnet (기본, 빠르고 경제적)"
        m_opt2="opus   (더 강력한 추론, 더 느림/비쌈)"
      else
        m_title="Choose a model:"
        m_opt1="sonnet (default, fast and economical)"
        m_opt2="opus   (stronger reasoning, slower/costlier)"
      fi
      select_menu "$m_title" 0 "$m_opt1" "$m_opt2"
      if [ "$SELECT_RESULT" = "1" ]; then
        DOGANY_MODEL="opus"
        BRIDGE_MODELS="sonnet,opus,haiku"
      else
        DOGANY_MODEL="sonnet"
        BRIDGE_MODELS="sonnet,haiku"
      fi
    else
      msg "  1) sonnet (기본, 빠르고 경제적)" "  1) sonnet (default, fast and economical)"
      msg "  2) opus   (더 강력한 추론, 더 느림/비쌈)" "  2) opus   (stronger reasoning, slower/costlier)"
      local choice=""
      ask choice "번호 선택 [1]: " "Pick a number [1]: " "1"
      case "$choice" in
        2|opus)
          DOGANY_MODEL="opus"
          BRIDGE_MODELS="sonnet,opus,haiku"
          ;;
        *)
          DOGANY_MODEL="sonnet"
          BRIDGE_MODELS="sonnet,haiku"
          ;;
      esac
    fi
    msg "모델: $DOGANY_MODEL" "Model: $DOGANY_MODEL"
    return 0
  fi

  # Recommendation available. Show it; the default answer keeps the recommended
  # model, but the user can switch explicitly.
  # DGN-167: also capture the bridge model whitelist from the tier probe.
  if [ "$rec" = "opus" ]; then
    msg "구독 등급 기준 추천 모델: opus (강력한 추론)." \
        "Recommended model for your subscription: opus (stronger reasoning)."
    if confirm "opus 로 설정할까요? (n = sonnet)" "Use opus? (n = sonnet)" "y"; then
      DOGANY_MODEL="opus"
      # Max tier detected -> all three models available.
      BRIDGE_MODELS="${rec_bridge:-sonnet,opus,haiku}"
    else
      DOGANY_MODEL="sonnet"
      # User chose sonnet even on max tier -> still expose full model list.
      BRIDGE_MODELS="${rec_bridge:-sonnet,opus,haiku}"
    fi
  else
    msg "구독 등급 기준 추천 모델: sonnet (빠르고 경제적)." \
        "Recommended model for your subscription: sonnet (fast and economical)."
    if confirm "sonnet 으로 설정할까요? (n = opus)" "Use sonnet? (n = opus)" "y"; then
      DOGANY_MODEL="sonnet"
      BRIDGE_MODELS="${rec_bridge:-sonnet,haiku}"
    else
      DOGANY_MODEL="opus"
      # User overrode to opus on a non-max tier -> keep sonnet,haiku whitelist
      # (opus will still work via full model id; /model picker stays economical).
      BRIDGE_MODELS="${rec_bridge:-sonnet,haiku}"
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
  msg "[6/10] 텔레그램 봇 토큰 + 오너 ID" "[6/10] Telegram bot token + owner id"
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
  # DGN-147: after the shape confirm, verify LIVENESS via getMe (token_liveness).
  # A revoked/invalid token (401/403) counts against the SAME 5-try cap and
  # reprompts, so a dead bot no longer sails through the shape-only check.
  local token_tries=0
  while :; do
    local blob=""
    ask blob "봇 토큰만 입력 (예: 1234567890:***): " "Enter the bot token only (e.g. 1234567890:***): " \
        "${DOGANY_MOCK_TOKEN_BLOB:-}"
    BOT_TOKEN="$(extract_token "$blob")"
    if [ -n "$BOT_TOKEN" ]; then
      msg "추출된 토큰: $(mask_token "$BOT_TOKEN")" \
          "Extracted token: $(mask_token "$BOT_TOKEN")"
      if confirm "이 토큰이 맞나요?" "Is this token correct?" "y"; then
        # Shape confirmed -> liveness gate. 0 = live (proceed), 2 = network
        # unknown (warn + proceed, offline path), 1 = revoked (fall through to
        # the retry counter and reprompt). `|| __live=$?` keeps a nonzero return
        # from tripping `set -e` (the return code is a classification, not an error).
        local __live=0
        token_liveness || __live=$?
        if [ "$__live" != "1" ]; then break; fi
      else
        # User rejected the extracted token -> reprompt without a liveness call.
        token_tries=$((token_tries + 1))
        [ "$token_tries" -ge 5 ] && { msg "[에러] 5회 시도 후에도 유효한 토큰을 받지 못했습니다. 설치를 중단합니다." \
              "[ERROR] No valid token after 5 attempts. Aborting install." >&2; exit 1; }
        continue
      fi
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

# DGN-147: token liveness gate. Calls Telegram getMe with the token (5s timeout)
# and classifies the result. Also resolves the bot @username on success (subsumes
# the old lookup_bot_name -- best-effort name for the final message).
#   return 0  -> LIVE: HTTP 200, sets BOT_NAME to the @username, prints it as
#               positive feedback. Caller proceeds.
#   return 1  -> REVOKED/INVALID: HTTP 401/403. Prints a bilingual error. Caller
#               reprompts (counts against the retry cap).
#   return 2  -> UNKNOWN: timeout / network failure / curl missing / other code.
#               Prints a bilingual warning; caller proceeds (offline path).
# The token is NEVER printed: the getMe URL (which embeds it) is not echoed and
# curl is silent; only the response BODY (username / error text) is parsed.
# DRY_RUN mock-accepts without any network call.
token_liveness() {
  BOT_NAME=""
  [ -z "$BOT_TOKEN" ] && return 2
  if [ "$DRY_RUN" = "1" ]; then
    BOT_NAME="mock_bot"
    msg "  [dry-run] getMe 라이브니스 검사는 건너뜁니다 (모의 수락)." \
        "  [dry-run] Skipping getMe liveness check (mock accept)."
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    msg "  [주의] curl 이 없어 토큰 라이브니스를 확인할 수 없습니다. 계속 진행합니다." \
        "  [NOTE] curl not found; cannot verify token liveness. Proceeding." >&2
    return 2
  fi

  # Capture body to a temp file and the HTTP status separately. -s silent, no -f
  # so a 401 still yields a body + code (not an early curl error). The token-bearing
  # URL is never printed; on any failure we surface only the classification.
  local body_file code
  body_file="$(mktemp "${TMPDIR:-/tmp}/dogany-getme.XXXXXX")"
  code="$(curl -s -o "$body_file" -w '%{http_code}' --max-time 5 \
          "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"

  case "$code" in
    200)
      local uname
      uname="$(grep -oE '"username":"[^"]+"' "$body_file" 2>/dev/null | head -n1 | sed -E 's/.*:"([^"]+)"/\1/' || true)"
      rm -f "$body_file"
      if [ -n "$uname" ]; then
        BOT_NAME="$uname"
        msg "  [OK] 토큰 유효 확인: @${uname} 봇이 응답했습니다." \
            "  [OK] Token verified live: @${uname} responded."
      else
        # 200 but no username parsed -> still live; proceed without a name.
        msg "  [OK] 토큰이 유효합니다 (getMe 200)." "  [OK] Token is live (getMe 200)."
      fi
      return 0
      ;;
    401|403)
      rm -f "$body_file"
      msg "  [에러] 토큰이 폐기되었거나 유효하지 않습니다 (getMe ${code}). 새 토큰을 발급받아 붙여넣으세요." \
          "  [ERROR] Token is revoked/invalid (getMe ${code}). Paste a fresh one." >&2
      return 1
      ;;
    *)
      rm -f "$body_file"
      msg "  [주의] 토큰 라이브니스 확인 실패 (네트워크/타임아웃, 코드: ${code:-none}). 오프라인일 수 있어 계속 진행합니다." \
          "  [NOTE] Could not verify token liveness (network/timeout, code: ${code:-none}). Proceeding (may be offline)." >&2
      return 2
      ;;
  esac
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
  msg "[7/10] 이메일 연결 (선택)" "[7/10] Email connect (optional)"
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
  local whisper="${8:-}"
  local bridge_models="${9:-}"
  printf '# Dogany bridge configuration -- generated by install.sh\n'
  printf '# Do NOT commit this file (contains your bot token).\n\n'
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
  printf '# Born-locked: when set, this list is authoritative and claim mode is off.\n'
  printf 'ALLOWED_USER_IDS=%s\n' "$ids"
  printf 'LOCALE=%s\n' "$locale"
  printf 'TZ=%s\n' "$tz"
  printf '# Extra path-guard roots (os.pathsep-separated). Empty for the product.\n'
  printf 'EXTRA_ALLOWED_ROOTS=\n'
  # DGN-167: bridge model whitelist seeded from subscription tier at install time.
  # max tier -> sonnet,opus,haiku; non-max -> sonnet,haiku. Controls /model picker.
  if [ -n "$bridge_models" ]; then
    printf '# --- Bridge model whitelist (controls /model picker). Set by installer.\n'
    printf 'BRIDGE_MODELS=%s\n' "$bridge_models"
  fi
  # DGN-146: voice model chosen at the deps step. Only emitted when voice is
  # enabled; skip leaves it out so config.py's "small" default applies.
  if [ -n "$whisper" ]; then
    printf '# --- Voice input (faster-whisper). Model chosen during install.\n'
    printf 'LOCAL_WHISPER_MODEL=%s\n' "$whisper"
  fi
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
  msg "[8/10] 에이전트 생성 및 설정 파일 작성" "[8/10] Mint the agent and write config"
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
               "$EMAIL_ADDRESS" "$pw_mask" "$EMAIL_CC" "$LOCAL_WHISPER_MODEL" "$BRIDGE_MODELS"
    msg "--- 끝 ---" "--- end ---"
    # Actually write the mock .env into the temp dir so the flow is testable.
    render_env "$BOT_TOKEN" "$OWNER_ID" "$DOGANY_LANG" "$DOGANY_TZ" \
               "$EMAIL_ADDRESS" "$EMAIL_APP_PASSWORD" "$EMAIL_CC" "$LOCAL_WHISPER_MODEL" \
               "$BRIDGE_MODELS" > "$env_file"
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
  # DOGANY_PYTHON_BIN pins the venv interpreter to the one check_prereqs
  # resolved as >= 3.11 (matters when system python3 is old and a newer one was
  # installed alongside). mint.sh falls back to python3 when this is empty.
  msg "에이전트를 생성합니다... (수 분 소요 가능)" "Minting the agent... (may take a few minutes)"
  DOGANY_PYTHON_BIN="$PYTHON_BIN" bash "$MINT_SH" --root "$target" --name "$AGENT_NAME" --force \
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
             "$EMAIL_ADDRESS" "$EMAIL_APP_PASSWORD" "$EMAIL_CC" "$LOCAL_WHISPER_MODEL" \
             "$BRIDGE_MODELS" > "$tmp_env"
  chmod 600 "$tmp_env"
  mv -f "$tmp_env" "$env_file"
  msg "설정 파일 작성 완료: $env_file" "Wrote config: $env_file"

  # 7c) write the chosen model into the minted instance's .claude/settings.json
  #     (template default is "sonnet"; overwrite only when a model was chosen).
  write_instance_model "$target" "$DOGANY_MODEL"

  # 7d) activate the agent-browser skill symlink when the user opted in.
  #     The bundle dir ships from .template; the symlink makes it visible to Claude.
  browser_activate_skill "$target"

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
    msg "참고: 폴링 워치독은 자동 시작(auto) 모드를 선택했을 때만 함께 등록됩니다." \
        "Note: the polling watchdog is registered automatically only when the service mode is auto."
    return 0
  fi
  SERVICE_CHOICE="auto"

  if [ "$OS_KIND" = "macos" ]; then
    install_launchd "$manual_cmd"
  else
    install_systemd "$manual_cmd"
  fi

  # DGN-140: register the external polling watchdog (auto mode only).
  # Non-fatal by contract: watchdog_setup.sh always exits 0; a failure there
  # must never break the install.
  if [ "$DRY_RUN" = "1" ]; then
    msg "워치독 등록(모의): bash \"$INSTALL_ROOT/bridge/watchdog_setup.sh\"" \
        "Would register watchdog: bash \"$INSTALL_ROOT/bridge/watchdog_setup.sh\""
  elif [ -f "$INSTALL_ROOT/bridge/watchdog_setup.sh" ]; then
    bash "$INSTALL_ROOT/bridge/watchdog_setup.sh" \
      || msg "[경고] 워치독 등록에 실패했습니다 (봇 자체 동작에는 영향 없음)." \
             "[WARN] Watchdog registration failed (the bot itself is unaffected)."
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
# Step 9c: dogany launcher (symlink scripts/dogany into ~/.local/bin)
# ---------------------------------------------------------------------------
# A thin `dogany` command (DGN-137): chat / status / logs / update / start /
# stop. No sudo -- a user-local symlink into ~/.local/bin. Honors DRY_RUN.
step_launcher() {
  local src="$REPO_ROOT/scripts/dogany"
  local bin_dir="$HOME/.local/bin"
  local dest="$bin_dir/dogany"

  [ -f "$src" ] || return 0   # nothing to install if the script is missing.

  if [ "$DRY_RUN" = "1" ]; then
    msg "dogany 런처 설치(모의): ln -sf '$src' '$dest'" \
        "Would install dogany launcher: ln -sf '$src' '$dest'"
    return 0
  fi

  mkdir -p "$bin_dir"
  ln -sf "$src" "$dest"
  chmod +x "$src" 2>/dev/null || true
  msg "dogany 런처 설치됨: $dest" "Installed dogany launcher: $dest"

  # PATH hint: only if ~/.local/bin is not already on PATH.
  case ":$PATH:" in
    *":$bin_dir:"*) : ;;
    *)
      msg "참고: $bin_dir 가 PATH 에 없습니다. 셸 설정에 다음을 추가하세요:" \
          "Note: $bin_dir is not on your PATH. Add this to your shell profile:"
      printf '  export PATH="%s:$PATH"\n' "$bin_dir"
      ;;
  esac
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
  msg "로컬 명령: 'dogany' 로 에이전트를 열고, 'dogany status' 로 상태를, 'dogany logs -f' 로 로그를 봅니다." \
      "Local command: run 'dogany' to open your agent, 'dogany status' for health, 'dogany logs -f' for logs."
  if is_wsl; then
    hr
    msg "Windows(WSL2) 참고:" "Windows (WSL2) note:"
    msg "  터미널을 모두 닫아도, 화면을 잠가도, 절전/복귀 후에도 에이전트는 살아 있습니다." \
        "  The agent survives closing all terminals, screen lock, and sleep/wake."
    msg "  로그아웃하면 멈추고, 재부팅 후에는 다음 로그인 때 돌아옵니다(그 전에는 아님)." \
        "  It stops on sign-out; after a reboot it returns at the next sign-in (not before)."
    msg "  무인 재부팅 생존은 자동 로그인 옵트인이 필요합니다(README 참고)." \
        "  Unattended-reboot survival needs the opt-in auto-logon (see README)."
    msg "  검증: 모든 Ubuntu 창을 닫고 2분 뒤 봇에게 메시지 -> 답장해야 정상." \
        "  Verify: close all Ubuntu windows, wait 2 minutes, message the bot -> it must reply."
    msg "  재부팅 검증: 로그인 전에는 답장 없음(정상), 로그인 후 2분 내 답장." \
        "  Reboot check: no reply before sign-in (expected), reply within 2 minutes after sign-in."
    msg "  권장: AC 전원 유지 + 절전 끄기 -> powercfg /change standby-timeout-ac 0" \
        "  Recommended: keep on AC power and disable sleep -> powercfg /change standby-timeout-ac 0"
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
    DOGANY_WHISPER_MODEL=M   preselect the faster-whisper model (small|medium|large-v3)
    DOGANY_BROWSER=1         opt into browser automation (agent-browser CLI + Chrome for Testing)
    DOGANY_TEST_RAM_GB=N     stub detected RAM in GB (spec-reco testing)
    DOGANY_TEST_DISK_GB=N    stub free disk in GB (spec-reco testing)
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
  # WSL preflight runs first, before ANY prompt: on WSL a missing Windows-side
  # setup would otherwise half-install silently. Hard stop in under 5s, zero
  # typed answers. No-op off WSL.
  wsl_preflight
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

  # DGN-164: heavy downloads first. Language stays first (it localizes every
  # later prompt). Then the DGN-157 prerequisite auto-install (python/git/claude
  # CLI + auth), then the optional model downloads (voice deps + Ollama/bge-m3
  # embedding pull) -- so the long downloads start as early as dependency order
  # allows and the user is not left waiting at the very end. The light,
  # interactive config steps (Claude model pick, timezone, bot token, email)
  # follow. Dependency order preserved:
  #   - language localizes all subsequent prompts -> stays first.
  #   - check_prereqs installs the claude CLI (+auth), which step_model's
  #     subscription-tier read (~/.claude.json) needs -> model pick stays after it.
  #   - step_dependencies runs its own machine-spec detection (disk/RAM) inline
  #     before each recommendation, and gates every download behind its opt-in.
  step_language
  check_prereqs
  step_dependencies
  step_model
  step_timezone
  step_bot_token
  step_email_connect
  step_mint_and_env
  step_service
  step_routines
  step_launcher
  step_final
}

main "$@"
