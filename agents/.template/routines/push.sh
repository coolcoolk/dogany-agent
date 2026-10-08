#!/bin/bash
# push.sh — 능동 루틴 핵심 부품 (인스턴스 인식)
# 헤드리스 claude로 내용 생성 → 텔레그램으로 능동 푸시
#
# 봇 토큰/대상은 "자기 인스턴스"의 .env에서 읽는다:
#   <스크립트>/../.telegram_bot/.env  (config.py BOT_DATA_DIR / install.sh 가 만드는 실제 경로)
#   구경로(../runtime/.env)와 전역 ~/telegram_bot/.env 는 폴백으로만 유지.
#   토큰이 비었거나 플레이스홀더면 다음 후보로 폴백.
# → each instance sends via its own bot token / chat id (no IDs hardcoded here).
#
# 사용법:
#   push.sh --model haiku --prompt "오늘 회고 질문 하나 던져줘"
#   push.sh --text "그대로 보낼 문구"
#   push.sh --photo /tmp/card.png [--caption "사진 설명"]   (사진 발송, 단독 동작 가능)
#   push.sh --env /path/to/.env   (명시적 인스턴스 지정, 선택)
#   push.sh --text "..." --audience owner|operator   (수신자 축, 기본 owner;
#     DGN-1585. operator 는 config/agent.conf 의 STEWARD_ENV_FILE 로 해소한
#     스튜어드 인스턴스 채널로 발신 -- 미설정/미존재면 owner 로 폴백 +
#     stderr 로그 1행. 레포 안의 모든 호출처는 명시 선언이 강제된다
#     (routines/push_audience_lint.py).)
#   push.sh --text "..." --audience operator --no-owner-fallback   (DGN-1808,
#     dec-220; opt-in, default unchanged) -- operator-ONLY message: when the
#     operator is unresolved nothing is sent (no owner fallback), one stderr
#     line, exit 9. For findings the owner cannot act on. Requires
#     --audience operator.
#   push.sh --text "..." --record-ledger-only   (deprecated no-op since
#     DGN-1642: ledger-only is now the only behavior -- every send appends
#     the outbound-ledger row and never drops a session-inbox turn. Flag
#     kept accepted so existing callers, e.g. scripts/pack/mint_run.sh, do
#     not break.)
#   push.sh --text "..." --button "라벨::callback_data"   (인라인 버튼 1개, DGN-835)
#     - reply_markup 1필드(단일 버튼)만 지원. callback_data는 텔레그램 64바이트 제한
#       -- 초과분은 여기서 가드해 버튼만 드롭하고 텍스트는 반드시 발송한다.
#     - 사진(sendPhoto) 경로에는 적용되지 않음(텍스트 발송 전용).
#   push.sh --text "..." --url-buttons-json '[[{"text":"지도","url":"https://..."}]]'
#     (kit_life remind: map links on prep/travel alerts) -- inline URL buttons
#     on the same message, any number of rows. Non-strict lane semantics: an
#     invalid set (not rows of {text,url}, a url that is not plain ASCII
#     https://, > 2048 chars) is dropped with a warning and the text still
#     goes out. Not combinable with --button (one reply_markup per message).
#     Capability token: url-buttons.
#   push.sh --text "...본문...
#   [[IDRILL:<arm_id>]]"    (DGN-966: arm 선언 키보드 -- 다중버튼/그리드/드릴다운)
#     - 본문 속 [[IDRILL:<arm_id>]] 마커는 공유 렌더 계약(bridge/artifacts.py)으로
#       처리: files/program/.idrill-arm/<arm_id> 의 step_buttons[1]/step_text[1] 을
#       읽어 본문 다음 메시지로 인라인 키보드를 발송한다 (모델턴/fast-path 와 동일
#       키보드). 이후 스텝 탭은 라이브 브릿지 콜백 엔진이 처리(드릴다운 동작).
#     - --button 은 기존 그대로 (하위호환; 단일 버튼 계약 유지).
#     - 키보드 발송 자체가 실패해도(본문은 이미 전송됨) exit 0 -- 본문 재전송을
#       유발하지 않는다(검증 라운드: redirect-respond.sh 등 non-zero=재시도로
#       보는 caller 존재 확인, exit 2였다면 본문 중복 발송). stderr 경고로만 노출.
#
#   push.sh --strict --text "..." [--chat <id>] [--thread <id>]
#       [--buttons-json '[[{"text":"1. a","callback_data":"opt:1. a"}]]']
#       [--expect-file <path>]                                  (DGN-1683 WP3)
#     - Strict screen mode: delivery is CONFIRMED or it is not delivery. One
#       Telegram message carries body + every button (inline_keyboard rows);
#       the body is byte-compared against the intended text (--expect-file, else
#       --text) AFTER the machine-line gate, so any dropped/rewritten line, an
#       over-long body (no truncation), a bad button or an unavailable gate is a
#       refusal, never a partial send. Telegram's ok + message_id are required;
#       a private per-call response file is used. Success prints ONE JSON line
#       on stdout {"ok":true,"message_id":N,"chat_id":C} (diagnostics: stderr)
#       and appends the normal outbound-ledger row. --chat/--thread (numeric)
#       address a specific chat / topic; both are strict-only.
#     - strict rc: 0 delivered / 2 not delivered (Telegram rejected, or curl
#       5 proxy DNS / 6 host DNS / 7 connect / 35 TLS handshake failed before
#       sending the request) / 3 test
#       guard / 4 identifier guard / 5 strict refusal (nothing sent) / 6
#       UNCERTAIN (other transport errors, including curl 28 timeout, or an
#       unparseable reply: Telegram may have accepted it) / 1 config.
#       Anything but 0 = delivery not confirmed.
#
#   push.sh --capabilities                                   (DGN-1803)
#     - prints ONE line of capability tokens ("strict edit url-buttons") and exits 0,
#       before any env/token/guard work. A push.sh without this flag exits 1
#       ("unknown arg") = no capabilities; nothing to version.
#   push.sh --strict --edit <message_id> --text "..." [--chat <id>] [--thread <id>]
#       [--buttons-json '<rows>'] [--expect-file <path>]        (DGN-1803)
#     - Edit a message THIS bot already sent (Telegram editMessageText) through
#       the SAME strict pipeline as a strict send (machine-line gate, byte
#       compare, formatting hop, 4000-char no-truncation, identifier guard,
#       test-context guard) -- no second copy of any gate.
#     - KEYBOARD: editMessageText without reply_markup REMOVES an existing
#       inline keyboard. Omitting --buttons-json therefore removes the keyboard;
#       pass --buttons-json again to keep/replace it.
#     - --thread is accepted for symmetry with strict send but not transmitted
#       (editMessageText addresses the message by chat_id + message_id only);
#       --silent has no effect on an edit.
#     - edit rc: 0 edited, stdout {"ok":true,"message_id":N,"chat_id":C,"edited":true}
#       / 8 Telegram "message is not modified" (success, NO ledger row, stdout
#       {"ok":true,...,"edited":false,"not_modified":true}) / 7 not editable
#       (not found, can't be edited, edit window passed, not this bot's; edit
#       not performed) / 6 UNCERTAIN (transport error or unparseable reply: the
#       edit may have landed) / 1-5 as strict send. Success appends an
#       outbound-ledger row tagged "tag":"edit" with the edited "message_id".
#
# 종료코드: 0 성공(키보드만 실패해도 0) / 1 설정오류 / 2 텔레그램 본문 전송 실패
#          / 3 테스트 맥락 가드 거부 (DGN-1122; PUSH_GUARD_OVERRIDE=send-anyway 로 명시 통과)
#          / 4 실행식별자 가드 거부 (runid/커밋해시/절대경로/exit-pid 가 본문에 있음;
#              우회 수단 없음 -- 호출 루틴의 문구를 고쳐야 한다)
#          / 9 operator 미해소 + --no-owner-fallback -> 발송 안 함 (의도된 로그 전용)

set -euo pipefail

# UTF-8 locale regardless of caller env (cron/launchd may leave LANG/LC_ALL
# unset -> C locale -> bash `${var:0:N}` below counts bytes, not chars, and
# can slice a Korean message body in half -- see ticket-hygiene.sh for the
# same class of bug against this exact truncation pattern).
# Host coverage (DGN-1059): en_US.UTF-8 is not guaranteed (minimal Linux may
# not have it generated), and setting a missing locale makes bash fall back
# to C SILENTLY -- the fix would be inert. So probe candidates and take the
# first whose charmap really is UTF-8 (C.UTF-8 is built into glibc >= 2.35,
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
  # No UTF-8 locale at all. Do NOT abort the push over it (delivery still
  # works for ASCII payloads), but never stay silent: silent byte-truncation
  # is the exact defect class this block exists to prevent.
  printf '%s\n' "[push.sh] WARN: no UTF-8 locale available (tried en_US.UTF-8, C.UTF-8); truncation may cut bytes, not characters, and can corrupt multi-byte (e.g. Korean) text" >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 실제 인스턴스 env 경로(config.py BOT_DATA_DIR / install.sh). runtime/.env 는 구경로 폴백.
INSTANCE_ENV="$SCRIPT_DIR/../.telegram_bot/.env"
LEGACY_ENV="$SCRIPT_DIR/../runtime/.env"
GLOBAL_ENV="$HOME/telegram_bot/.env"
PLACEHOLDER="your_bot_token_here"

MODEL="haiku"
PROMPT=""
RAW_TEXT=""
ENV_OVERRIDE=""
PHOTO_PATH=""
CAPTION=""
SILENT=""   # --silent 면 disable_notification=true (무음 발송: 알림 소리/배지 없이)
BUTTON=""   # --button "라벨::callback_data" -> 단일 인라인 버튼 (DGN-835)
URL_BUTTONS_JSON=""   # --url-buttons-json '<rows>' -> URL 버튼 (non-strict)
# DGN-822: --html is a deprecated no-op. Every text send now routes through the
# bridge sanitizer (bridge/formatting.py sanitize_message_for_telegram) and
# goes out as parse_mode=HTML; whitelisted Telegram tags in the body
# (<b> <i> <u> <s> <code> <pre> <blockquote> <a href>) still pass through.
# DGN-1585: --audience owner|operator (default owner). Owner is the fail-safe
# default for callers outside this repo; callers INSIDE this repo must
# declare it explicitly (routines/push_audience_lint.py enforces that). A
# value other than owner/operator fails immediately, same as an unknown arg
# -- a silent fallback to the default would let a typo route an operator-only
# alert to the owner's screen unnoticed.
AUDIENCE="owner"
RECORD_LEDGER_ONLY=""   # --record-ledger-only -> deprecated no-op since DGN-1642 (accepted, unused)
STRICT=""               # --strict -> confirmed-delivery screen mode (DGN-1683 WP3)
CHAT_OVERRIDE=""        # --chat <id>   (strict only)
THREAD_ID=""            # --thread <id> (strict only)
BUTTONS_JSON=""         # --buttons-json '<rows>' (strict only)
EXPECT_FILE=""          # --expect-file <path> (strict only)
EDIT_ID=""              # --edit <message_id> (strict only; DGN-1803)
NO_OWNER_FALLBACK=""    # --no-owner-fallback (operator only; DGN-1808 dec-220)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)   MODEL="$2"; shift 2 ;;
    --prompt)  PROMPT="$2"; shift 2 ;;
    --text)    RAW_TEXT="$2"; shift 2 ;;
    --photo)   PHOTO_PATH="$2"; shift 2 ;;
    --caption) CAPTION="$2"; shift 2 ;;
    --env)     ENV_OVERRIDE="$2"; shift 2 ;;
    --silent)  SILENT="true"; shift 1 ;;
    --button)  BUTTON="$2"; shift 2 ;;
    --url-buttons-json) URL_BUTTONS_JSON="$2"; shift 2 ;;
    --html)    shift 1 ;;  # DGN-822: deprecated no-op (sanitize pipe always sends HTML)
    --record-ledger-only) RECORD_LEDGER_ONLY="true"; shift 1 ;;  # DGN-1642: deprecated no-op
    --strict)  STRICT="true"; shift 1 ;;
    --capabilities) echo "strict edit url-buttons"; exit 0 ;;   # DGN-1803: capability line
    --edit)    EDIT_ID="$2"; shift 2 ;;
    --chat)    CHAT_OVERRIDE="$2"; shift 2 ;;
    --thread)  THREAD_ID="$2"; shift 2 ;;
    --buttons-json) BUTTONS_JSON="$2"; shift 2 ;;
    --expect-file)  EXPECT_FILE="$2"; shift 2 ;;
    --no-owner-fallback) NO_OWNER_FALLBACK="true"; shift 1 ;;
    --audience)
      case "$2" in
        owner|operator) AUDIENCE="$2" ;;
        *) echo "invalid --audience: $2 (must be owner or operator)" >&2; exit 1 ;;
      esac
      shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# DGN-1808 (dec-220): the flag only means something on the operator route; on
# an owner send it would be a contradiction, so refuse it like a bad arg.
if [[ -n "$NO_OWNER_FALLBACK" && "$AUDIENCE" != "operator" ]]; then
  echo "--no-owner-fallback requires --audience operator" >&2; exit 1
fi

# ---- DGN-1683 WP3: strict-mode argument contract -----------------------------
# Strict is a narrow, fully-checked lane: text screens only. Everything the
# lane cannot prove is refused BEFORE any send (rc 5 = nothing left the host).
_strict_refuse() {
  echo "[push] STRICT refusal: $1" >&2
  [[ -n "${_STRICT_INTENDED_FILE:-}" ]] && rm -f "$_STRICT_INTENDED_FILE"
  [[ -n "${IDRILL_KB_FILE:-}" ]] && rm -f "$IDRILL_KB_FILE"
  [[ -n "${_ALERT_HITS_FILE:-}" ]] && rm -f "$_ALERT_HITS_FILE"
  [[ -n "${_GATED_RAW_FILE:-}" ]] && rm -f "$_GATED_RAW_FILE"
  [[ "${_RESP_FILE:-/tmp/push_resp.json}" != "/tmp/push_resp.json" ]] && rm -f "$_RESP_FILE"
  exit 5
}
_STRICT_INTENDED_FILE=""
if [[ -z "$STRICT" ]]; then
  if [[ -n "$CHAT_OVERRIDE$THREAD_ID$BUTTONS_JSON$EXPECT_FILE$EDIT_ID" ]]; then
    echo "--chat/--thread/--buttons-json/--expect-file/--edit require --strict" >&2; exit 1
  fi
else
  [[ -n "$RAW_TEXT" ]] || { echo "[push] STRICT: --text is required" >&2; exit 1; }
  if [[ -n "$PROMPT$PHOTO_PATH$BUTTON$URL_BUTTONS_JSON" ]]; then
    echo "[push] STRICT: --prompt/--photo/--button/--url-buttons-json are not allowed (use --text/--buttons-json)" >&2; exit 1
  fi
  case "$CHAT_OVERRIDE" in ""|-[0-9]*|[0-9]*) ;; *) echo "[push] STRICT: --chat must be numeric" >&2; exit 1 ;; esac
  case "$CHAT_OVERRIDE" in *[!0-9-]*) echo "[push] STRICT: --chat must be numeric" >&2; exit 1 ;; esac
  case "$THREAD_ID" in *[!0-9]*) echo "[push] STRICT: --thread must be numeric" >&2; exit 1 ;; esac
  case "$EDIT_ID" in
    "") ;;
    *[!0-9]*|0*) echo "[push] STRICT: --edit must be a positive numeric message_id" >&2; exit 1 ;;
  esac
  # Bridge/render markers are grammar for model output; a machine screen carries
  # real buttons, so a marker in strict text is a caller defect, not copy.
  case "$RAW_TEXT" in
    *"[[IDRILL:"*|*"[[OPTIONS"*) _strict_refuse "bridge marker in screen text ([[IDRILL / [[OPTIONS); use --buttons-json" ;;
  esac
  # The intended bytes, captured BEFORE any hop can rewrite the body.
  _STRICT_INTENDED_FILE="$(mktemp /tmp/push_strict_intended.XXXXXX)"
  if [[ -n "$EXPECT_FILE" ]]; then
    [[ -f "$EXPECT_FILE" ]] || { rm -f "$_STRICT_INTENDED_FILE"; echo "[push] STRICT: --expect-file not found" >&2; exit 1; }
    cp "$EXPECT_FILE" "$_STRICT_INTENDED_FILE"
  else
    printf '%s' "$RAW_TEXT" > "$_STRICT_INTENDED_FILE"
  fi
fi

# ---- DGN-1122: sender-side test-context guard (single choke point) ----
# push.sh is the LAST hop before the Telegram API: every send path (--text /
# --photo / --prompt / --button / IDRILL keyboard) passes through the code
# below this block, so the guard lives HERE and only here -- caller-side
# discipline (test-harness env binding) already failed 6 times and put real
# test pushes on the owner's phone.
#
# PRIORITY ORDER (fixed): false-positive ZERO first, detection second. A
# blocked routine push silently loses an owner notification -- costlier than
# a stray test push. When in doubt this guard PASSES.
#
# Predicate layers (any hit = test context):
#   1. runner-native env markers: pytest itself exports PYTEST_CURRENT_TEST /
#      PYTEST_VERSION into every descendant process; bats exports
#      BATS_TEST_FILENAME. Set by the RUNNER, not by test authors -- so this
#      does NOT recreate the "caller must remember to bind env" trap.
#   2. process-ancestry scan (catches env-scrubbed spawns): walk ppids and
#      match each ancestor's LEADING argv tokens against test-runner shapes.
#      Only leading tokens are matched -- deeper argv can be free text
#      (claude -p "<prompt mentioning pytest>", wrapper --text bodies) and
#      matching it would false-positive legit pushes. push.sh's OWN command
#      line is skipped for the same reason (--text body rides its argv).
#
# Detector self-check (pgrep -af precedent): before any "clean" verdict the
# matcher must fire on a known-positive line, must NOT fire on a routine
# shape, and ps must be able to show a command at all. If the self-check
# fails the ancestry layer is BLIND -- warn loud and fail OPEN (never block
# on a blind detector); the env-marker layer is independent and still ran.
#
# Rejection is LOUD (named evidence + payload + exit 3) -- a silent no-op
# would let test suites pass green while covering nothing. Escape hatch:
# PUSH_GUARD_OVERRIDE=send-anyway proceeds WITH a logged receipt.
_guard_cmd_is_test_runner() {
  # $1 = one ancestor's full ps command line. Token-window discipline: only
  # t1/t2 (program/launcher positions) and the (t2,t3) flag pairs below are
  # inspected; anything deeper can be free text and is never matched.
  local _t1 _t2 _t3 _rest _b1 _b2
  read -r _t1 _t2 _t3 _rest <<< "$1" || true
  _b1="${_t1##*/}"; _b2="${_t2##*/}"
  case "$_b1" in pytest|py.test|bats|bats-exec*) return 0 ;; esac
  case "$_b2" in pytest|py.test|bats|bats-exec*) return 0 ;; esac
  case "$_t1" in */tests/*|tests/*) return 0 ;; esac
  case "$_t2" in */tests/*|tests/*) return 0 ;; esac
  # "python -m pytest|unittest", "sh -c 'pytest ...'", "uv|poetry run pytest"
  if [ "$_t2" = "-m" ] || [ "$_t2" = "-c" ] || [ "$_t2" = "run" ]; then
    case "$_t3" in pytest|py.test|unittest) return 0 ;; esac
  fi
  return 1
}

_guard_detector_selfcheck() {
  # POSITIVE first (pgrep -af class): a detector that cannot see a known
  # firing must never be allowed to report "clean".
  _guard_cmd_is_test_runner "/usr/bin/python3 -m pytest tests/test_x.py" || return 1
  _guard_cmd_is_test_runner "bash /w/routines/tests/test-push-button.sh" || return 1
  # negative control: a routine shape must NOT match (a matcher that fires
  # on everything is a false-positive machine, equally blind).
  _guard_cmd_is_test_runner "/bin/bash /w/routines/generic-brief.sh --slot morning" && return 1
  # ps machinery: must be able to show our own command line at all.
  [ -n "$(ps -p $$ -o command= 2>/dev/null || true)" ] || return 1
  return 0
}

_GUARD_EVIDENCE=""
if [[ -n "${PYTEST_CURRENT_TEST:-}" ]]; then
  _GUARD_EVIDENCE="pytest env marker PYTEST_CURRENT_TEST=${PYTEST_CURRENT_TEST}"
elif [[ -n "${PYTEST_VERSION:-}" ]]; then
  _GUARD_EVIDENCE="pytest env marker PYTEST_VERSION=${PYTEST_VERSION}"
elif [[ -n "${BATS_TEST_FILENAME:-}" ]]; then
  _GUARD_EVIDENCE="bats env marker BATS_TEST_FILENAME=${BATS_TEST_FILENAME}"
elif _guard_detector_selfcheck; then
  # ancestry walk: parent chain only (self skipped -- own argv carries the
  # --text body). Hop cap bounds the walk; stop at pid 1/0 or non-numeric.
  _g_pid="$(ps -p $$ -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
  _g_hops=0
  while [ -n "$_g_pid" ] && [ "$_g_pid" != "0" ] && [ "$_g_pid" != "1" ] \
      && [ "$_g_hops" -lt 25 ]; do
    case "$_g_pid" in *[!0-9]*) break ;; esac
    _g_cmd="$(ps -p "$_g_pid" -o command= 2>/dev/null || true)"
    if [ -n "$_g_cmd" ] && _guard_cmd_is_test_runner "$_g_cmd"; then
      _GUARD_EVIDENCE="test-runner ancestor pid $_g_pid: $_g_cmd"
      break
    fi
    _g_pid="$(ps -p "$_g_pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    _g_hops=$(( _g_hops + 1 ))
  done
else
  echo "[push] WARN: test-context detector self-check FAILED (ps/matcher blind on this host) -- ancestry scan skipped, passing anyway (fail-open: a blocked routine push is worse than a missed test push; env markers were still checked)" >&2
fi

if [[ -n "$_GUARD_EVIDENCE" ]]; then
  _g_payload=""
  [[ -n "$RAW_TEXT" ]]   && _g_payload="$_g_payload --text \"${RAW_TEXT:0:80}\""
  [[ -n "$PROMPT" ]]     && _g_payload="$_g_payload --prompt \"${PROMPT:0:80}\""
  [[ -n "$PHOTO_PATH" ]] && _g_payload="$_g_payload --photo $PHOTO_PATH"
  if [[ "${PUSH_GUARD_OVERRIDE:-}" == "send-anyway" ]]; then
    _g_receipt="[push] GUARD OVERRIDE receipt (DGN-1122): test context detected ($_GUARD_EVIDENCE) but PUSH_GUARD_OVERRIDE=send-anyway -- proceeding; payload:$_g_payload"
    echo "$_g_receipt" >&2
    # durable receipt, fail-soft (instance data dir may not exist in sandboxes)
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || true)" "$_g_receipt" \
      >> "$SCRIPT_DIR/../.telegram_bot/push-guard-override.log" 2>/dev/null || true
  else
    echo "[push] BLOCKED (DGN-1122 test-context guard): refusing to send from a test context -- this send would have hit the owner's REAL phone." >&2
    echo "[push]   evidence: $_GUARD_EVIDENCE" >&2
    echo "[push]   payload :$_g_payload" >&2
    echo "[push]   fix: bind your harness push env to a stub (do not fall back to the live sender), or set PUSH_GUARD_OVERRIDE=send-anyway for an INTENTIONAL live send (a receipt is logged)." >&2
    exit 3
  fi
fi

# B: CLAUDECODE guard -- nested claude -p is rejected by the CLI (M1: CLAUDECODE=1 inherited).
# Fail fast before any .env work so the error is visible; --text bypasses generation.
if [[ -n "${CLAUDECODE:-}" && -n "$PROMPT" ]]; then
  echo "[push] error: CLAUDECODE session detected -- claude -p cannot run inside an active session." >&2
  echo "[push] hint: use --text \"<message>\" to send a pre-composed message instead of --prompt." >&2
  exit 1
fi

# A: resolve per-call timeout command; empty = no-timeout fallback (M4: orphan stdout hold).
_PUSH_TIMEOUT=""
if command -v timeout > /dev/null 2>&1; then
  _PUSH_TIMEOUT="timeout 60"
elif command -v gtimeout > /dev/null 2>&1; then
  _PUSH_TIMEOUT="gtimeout 60"
fi

# ---- .env 골라 토큰/대상 읽기 ----
# `|| true`: under `set -o pipefail`, a missing key (grep exit 1) would
# otherwise poison the whole pipeline's exit status and, via `set -e`, abort
# the script at the call site's assignment -- read_kv's documented contract
# is "return empty string on a missing key", not "kill the caller". DGN-1585
# surfaced this: STEWARD_ENV_FILE is unset on most instances by design (S2's
# unresolved-fallback case), which is exactly the input this must survive.
read_kv() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' || true; }

# ---- DGN-1585 S2: audience routing ----------------------------------------
# owner (default) -> falls through unchanged: zero behavior change, no
# ENV_OVERRIDE touched here.
# operator -> resolve the steward instance's channel from config/agent.conf
# (STEWARD_ENV_FILE -- same shape as the WARG_LIFEKIT_SH peer-instance-path
# key already in that file; never hardcoded here). A resolved steward WINS
# over a caller's --env: --env picks both token and chat, and the callers
# that declare operator pass --env naming their OWN instance (cron-guard's
# ENV_PATH defaults to <agent>/.telegram_bot/.env), so "--env wins" sent every
# operator alarm to the owner's chat while the ledger said operator (measured
# 2026-09-27: all three cron-guard operator sites). The override is logged,
# never silent. Unresolved keeps the caller's --env: that is its owner, which
# is exactly the fallback below.
# Resolution failure (key empty, or the path it names does not exist) must
# NEVER become a silent drop: fall back to owner and log ONE line. That
# fallback line is the one DGN-1585 calls "the most important line in this
# spec" -- an operator-grade alert that goes owner-shaped is loud proof of
# a config gap, not a swallowed notification.
AUDIENCE_RESOLVED="$AUDIENCE"
if [[ "$AUDIENCE" == "operator" ]]; then
  _AGENT_CONF="$SCRIPT_DIR/../config/agent.conf"
  _STEWARD_ENV="$(read_kv STEWARD_ENV_FILE "$_AGENT_CONF")"
  if [[ -n "$_STEWARD_ENV" && -f "$_STEWARD_ENV" ]]; then
    if [[ -n "$ENV_OVERRIDE" && "$ENV_OVERRIDE" != "$_STEWARD_ENV" ]]; then
      echo "[push] audience=operator: --env $ENV_OVERRIDE overridden by STEWARD_ENV_FILE" >&2
    fi
    ENV_OVERRIDE="$_STEWARD_ENV"
  elif [[ -n "$NO_OWNER_FALLBACK" ]]; then
    # DGN-1808 (dec-220): an operator-ONLY finding (the owner cannot act on
    # it) must never go owner-shaped. Opt-in per call; the fallback below
    # stays the default for every other caller. Still one loud line, and a
    # distinct rc so the caller can tell "logged by design" from "failed".
    echo "[push] audience=operator unresolved -> not sent (--no-owner-fallback: operator-only, log only)" >&2
    [[ -n "$_STRICT_INTENDED_FILE" ]] && rm -f "$_STRICT_INTENDED_FILE"
    exit 9
  else
    echo "[push] audience=operator unresolved -> owner" >&2
    AUDIENCE_RESOLVED="owner"
  fi
fi

# env 후보 순서: --env 지정 > 인스턴스(.telegram_bot) > 구경로(runtime) > 전역(~/telegram_bot)
if [[ -n "$ENV_OVERRIDE" ]]; then
  ENV_CANDIDATES=("$ENV_OVERRIDE")
else
  ENV_CANDIDATES=("$INSTANCE_ENV" "$LEGACY_ENV" "$GLOBAL_ENV")
fi

# 첫 후보를 진단 메시지용 기본 ENV_FILE 로 둔다.
ENV_FILE="${ENV_CANDIDATES[0]}"
TOKEN=""; CHAT_ID=""
# 유효 토큰이 나올 때까지 후보를 순회. chat id 는 처음 발견되는 값을 유지.
for cand in "${ENV_CANDIDATES[@]}"; do
  [[ -f "$cand" ]] || continue
  cand_token="$(read_kv TELEGRAM_BOT_TOKEN "$cand")"
  cand_chat="$(read_kv ALLOWED_USER_IDS "$cand" | cut -d, -f1)"
  [[ -z "$CHAT_ID" && -n "$cand_chat" ]] && CHAT_ID="$cand_chat"
  if [[ -n "$cand_token" && "$cand_token" != "$PLACEHOLDER" ]]; then
    TOKEN="$cand_token"
    ENV_FILE="$cand"
    break
  fi
done
# A claimed install keeps ALLOWED_USER_IDS empty. Use the instance's own
# ownership.py (the writer), never a second owner.lock parser. INSTANCE_ENV
# establishes the <root>/.telegram_bot/.env layout; use the selected ENV_FILE
# so --env and operator routing resolve the owner alongside their bot token.
if [[ -z "$CHAT_ID" && -z "$CHAT_OVERRIDE" ]]; then
  _OWNER_ROOT="$(dirname "$(dirname "$ENV_FILE")")"
  if [[ -f "$_OWNER_ROOT/bridge/ownership.py" ]]; then
    CHAT_ID="$("${BRIDGE_PYTHON:-python3}" -B - "$_OWNER_ROOT/bridge" "$_OWNER_ROOT/.telegram_bot" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
from ownership import read_owner_lock
oid = read_owner_lock(sys.argv[2])
print(oid if oid is not None else "")
PY
)"
  fi
fi
# DGN-1683 WP3: strict --chat addresses a specific chat (group / DM); the bot
# token is still the instance's own, so it can only reach chats the bot is in.
[[ -n "$CHAT_OVERRIDE" ]] && CHAT_ID="$CHAT_OVERRIDE"
# No hardcoded recipient: use the configured owner or an explicit chat.
if [[ -z "$CHAT_ID" ]]; then
  echo "no chat id found (use /claim or set ALLOWED_USER_IDS in $ENV_FILE or $GLOBAL_ENV)" >&2; exit 1
fi

if [[ -z "$TOKEN" || "$TOKEN" == "$PLACEHOLDER" ]]; then
  echo "no valid bot token found (instance=$ENV_FILE, global=$GLOBAL_ENV)" >&2; exit 1
fi

# ---- 사진 발송 (sendPhoto, --photo 단독으로도 동작) ----
if [[ -n "$PHOTO_PATH" ]]; then
  if [[ ! -s "$PHOTO_PATH" ]]; then
    echo "[push] photo not found or empty: $PHOTO_PATH" >&2; exit 2
  fi
  CAPTION="${CAPTION:0:1024}"   # 텔레그램 caption 1024자 제한
  PHOTO_ARGS=(-F "chat_id=${CHAT_ID}" -F "photo=@${PHOTO_PATH}")
  [[ -n "$CAPTION" ]] && PHOTO_ARGS+=(-F "caption=${CAPTION}")
  PHOTO_CODE="$(curl -s -o /tmp/push_photo_resp.json -w '%{http_code}' \
    "https://api.telegram.org/bot${TOKEN}/sendPhoto" "${PHOTO_ARGS[@]}")"
  if [[ "$PHOTO_CODE" == "200" ]]; then
    echo "[push] photo sent OK (bot ${TOKEN%%:*} → chat ${CHAT_ID})"
  else
    echo "[push] telegram sendPhoto failed (HTTP $PHOTO_CODE):" >&2; cat /tmp/push_photo_resp.json >&2; exit 2
  fi
  # --photo 단독 호출(텍스트 없음)이면 여기서 종료
  if [[ -z "$RAW_TEXT" && -z "$PROMPT" ]]; then
    exit 0
  fi
fi

# ---- 메시지 본문 ----
if [[ -n "$RAW_TEXT" ]]; then
  BODY="$RAW_TEXT"
elif [[ -n "$PROMPT" ]]; then
  echo "[push] generating via claude --model $MODEL ..." >&2
  BODY=""
  _attempt=0
  while [[ $_attempt -lt 3 ]]; do
    _attempt=$(( _attempt + 1 ))
    # A: < /dev/null prevents stdin hang (M2); $_PUSH_TIMEOUT caps per-call wall time (M4);
    # || true lets set -e survive a non-zero claude exit so the retry loop continues.
    BODY="$($_PUSH_TIMEOUT claude -p "$PROMPT" --model "$MODEL" 2>/dev/null < /dev/null)" || true
    if [[ -n "$BODY" ]]; then
      break
    fi
    if [[ $_attempt -lt 3 ]]; then
      sleep 5
    fi
  done
  if [[ -z "$BODY" ]]; then
    echo "claude returned empty after $_attempt attempts" >&2
    exit 1
  fi
else
  echo "need --prompt or --text" >&2; exit 1
fi
# DGN-692: strip send_file:: marker lines before transmission (headless push is
# text-only; marker lines must not leak as raw text in the Telegram message).
# Applied unconditionally -- independent of --html flag.
BODY="$(printf '%s' "$BODY" | grep -v '^send_file::')"

# DGN-822: route the body through the bridge sanitizer so the shell rail gets
# the SAME cleanup as the conversation rail (headers, tables, bold, thematic
# breaks -> Telegram-safe HTML). Sanitize logic is owned by bridge/formatting.py
# alone -- this is a thin pipe, no shell-side formatting. Interpreter
# resolution mirrors bridge/start.sh (venv next to bridge/, else
# BRIDGE_PYTHON, else python3). The body travels via stdin so quotes, $,
# backticks and newlines survive verbatim. On ANY hop failure (missing
# python, missing module) SANITIZED stays empty and the raw body is sent as
# plain text below -- the push itself must never die on formatting.
BRIDGE_DIR="$SCRIPT_DIR/../bridge"
if [[ -x "$BRIDGE_DIR/venv/bin/python" ]]; then
  _PUSH_PYTHON="$BRIDGE_DIR/venv/bin/python"
elif [[ -n "${BRIDGE_PYTHON:-}" ]]; then
  _PUSH_PYTHON="$BRIDGE_PYTHON"
else
  _PUSH_PYTHON="python3"
fi
# DGN-966: [[IDRILL:<arm_id>]] marker -> arm-declared inline keyboard through
# the SHARED artifact-render contract (bridge/artifacts.py + formatting.py --
# the same spec builder the model-turn and fast-path rails use, so the
# keyboard is identical on every path; multi-button rows and drilldown steps
# included). The hop strips the marker from the body and stages the step-1
# keyboard (text + inline_keyboard JSON) in a temp file; the keyboard message
# is sent AFTER the body below. Fail-soft: on ANY hop failure the marker
# lines are grep-stripped (raw [[IDRILL:..]] must never leak to the chat) and
# the body still sends -- the push never dies on rendering.
IDRILL_KB_FILE=""
# Leading whitespace tolerated: strip_idrill_marker matches the stripped line,
# so the detection / fallback greps must not be stricter than the renderer.
if printf '%s' "$BODY" | grep -q '^[[:space:]]*\[\[IDRILL:'; then
  IDRILL_KB_FILE="$(mktemp /tmp/push_idrill_kb.XXXXXX)"
  _IDRILL_BODY="$(printf '%s' "$BODY" | "$_PUSH_PYTHON" -c "
import json, sys
from pathlib import Path
sys.stdin.reconfigure(encoding=\"utf-8\", errors=\"replace\")
sys.stdout.reconfigure(encoding=\"utf-8\")
sys.path.insert(0, sys.argv[1])
from bridge.formatting import strip_idrill_marker
from bridge.artifacts import initial_keyboard_json
clean, arm_id = strip_idrill_marker(sys.stdin.read())
if arm_id:
    kb = initial_keyboard_json(Path(sys.argv[2]), arm_id)
    if kb:
        Path(sys.argv[3]).write_text(
            json.dumps(kb, ensure_ascii=False), encoding=\"utf-8\")
sys.stdout.write(clean)
" "$(cd "$BRIDGE_DIR/.." && pwd)" "$(cd "$SCRIPT_DIR/.." && pwd)" \
    "$IDRILL_KB_FILE" 2>/dev/null)" && _idrill_rc=0 || _idrill_rc=$?
  if [[ "$_idrill_rc" -eq 0 ]]; then
    BODY="$_IDRILL_BODY"
  else
    echo "[push] warn: idrill render hop failed; stripping marker, sending body only" >&2
    # || true: a marker-only body greps to empty (grep exit 1) -- must not
    # kill the push under set -e.
    BODY="$(printf '%s' "$BODY" | grep -v '^[[:space:]]*\[\[IDRILL:' || true)"
    : > "$IDRILL_KB_FILE"
  fi
fi

RAW_BODY="$BODY"
# DGN-1209 R3: this hop is the push.sh rail's machine-line gate. The body is
# CONSUMER text (no model turn) -> rail=consumer: registered machine lines
# (`signal::...`, seed tokens) are dropped/stripped inside the sanitizer;
# unregistered machine-shaped lines pass and raise an alert.
# DGN-1756: hygiene alerts are ledger/log-only even with a steward configured.
# STEWARD_ENV_FILE resolves an owner chat, not an operator session. /health
# technical details already provide a query-time reader for durable markers.
# Fresh hits are spooled for logging after the body send; never relayed.
# Keep DOGANY_MACHINE_ALERT_HOP compatibility for callers suppressing diagnostics.
# Dedup per (rail, token, day) through the same ledger the bridge uses. The
# ledger marker is still written at render time (the curl send follows
# below) -- the one rail where "after send success" cannot be honored
# inside the hop; a body-send failure loses that day's alert, same accepted
# compromise as before.
# Exit 3 = the gate emptied the body (machine-only): nothing owner-facing to
# send, and the raw body must NOT be sent as the plain-text fallback.
# The hop also writes the GATED raw text (pre-render) to a temp file so the
# HTTP-400 plain-text fallback below re-sends gated text, never the raw body.
_SAN_RC=0
_GATED_RAW_FILE="$(mktemp /tmp/push_gated_raw.XXXXXX)"
_ALERT_HITS_FILE="$(mktemp /tmp/push_alert_hits.XXXXXX)"
SANITIZED="$(printf '%s' "$BODY" | "$_PUSH_PYTHON" -c "
import os, sys
from pathlib import Path
sys.stdin.reconfigure(encoding=\"utf-8\", errors=\"replace\")
sys.stdout.reconfigure(encoding=\"utf-8\")
sys.path.insert(0, sys.argv[1])
from bridge.formatting import sanitize_message_for_telegram
from bridge import machine_gate
hits = []
machine_gate.set_machine_line_alert_sink(lambda rail, tokens, sample: hits.append((rail, tuple(tokens))))
raw = sys.stdin.read()
out = sanitize_message_for_telegram(raw, rail=machine_gate.RAIL_CONSUMER)
if len(sys.argv) > 3:
    Path(sys.argv[3]).write_text(machine_gate.gate_machine_lines(raw, machine_gate.RAIL_CONSUMER).text, encoding=\"utf-8\")
if hits and not os.environ.get(\"DOGANY_MACHINE_ALERT_HOP\"):
    ledger = machine_gate.MachineLineAlertLedger(Path(sys.argv[2]) / \".telegram_bot\" / \"machine-line-alerts\")
    day = machine_gate.today_key()
    spool = []
    for rail, tokens in hits:
        fresh = ledger.unseen(rail, tokens, day)
        if not fresh:
            continue
        spool.append(\"%s\t%s\" % (rail, \",\".join(fresh)))
        ledger.mark(rail, fresh, day)
    if spool and len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(\"\\n\".join(spool) + \"\\n\", encoding=\"utf-8\")
if raw.strip() and not out.strip():
    sys.exit(3)
sys.stdout.write(out)
" "$(cd "$BRIDGE_DIR/.." && pwd)" "$(cd "$SCRIPT_DIR/.." && pwd)" "$_GATED_RAW_FILE" "$_ALERT_HITS_FILE" 2>/dev/null)" || _SAN_RC=$?
if [[ "$_SAN_RC" -ne 0 ]]; then SANITIZED=""; fi
# Hop ran (rc 0 or 3): the plain-text fallback body is the GATED raw text.
if [[ ( "$_SAN_RC" -eq 0 || "$_SAN_RC" -eq 3 ) && -f "$_GATED_RAW_FILE" ]]; then
  RAW_BODY="$(cat "$_GATED_RAW_FILE")"
fi
# DGN-1683 WP3 (grill fix 7): strict = the gated text must equal the intended
# text byte-for-byte. The gate can drop lines and still exit 0; only a fully
# emptied body is rc 3. Any difference (dropped line, stripped marker, altered
# text, hop unavailable) means the owner would see something other than the
# approved screen, so nothing is sent.
if [[ -n "$STRICT" ]]; then
  [[ "$_SAN_RC" -eq 0 ]] || _strict_refuse "outbound gate did not run clean (rc=$_SAN_RC); cannot prove the screen is intact"
  cmp -s "$_GATED_RAW_FILE" "$_STRICT_INTENDED_FILE" \
    || _strict_refuse "outbound gate changed the screen text (dropped or rewritten line); not sent"
  [[ -n "$SANITIZED" ]] || _strict_refuse "rendered body is empty; not sent"
  [[ "${#RAW_BODY}" -le 4000 && "${#SANITIZED}" -le 4000 ]] \
    || _strict_refuse "screen exceeds 4000 chars; strict never truncates"
fi
rm -f "$_GATED_RAW_FILE"

# ---- execution-identifier guard (BLOCKING) --------------------------------
# Same rail as the sanitize hop above, and deliberately DOWNSTREAM of it: the
# machine-line gate has already dropped registered signal lines, so this guard
# only ever judges text that really was about to reach the owner.
#
# WHY IT EXISTS. A detached routine sent the owner
# "dispatch completed: <job-label> (runid dsp-...)" -- internal mechanics on
# his phone. The machine-shape detector could not see it (it matches a line
# STARTING with an ALL-CAPS token) and the Stop-hook output gate was never in
# scope (a detached shell push produces no assistant message). The detector is
# bridge/machine_gate.find_execution_identifiers: precise identifier SHAPES
# (run id / commit hash / absolute path / exit-pid), no register heuristics.
#
# REFUSAL follows the test-context guard above: loud, named evidence, a fix
# line, and a distinct exit code. NO override env -- the guard exists because
# there is no legitimate send carrying these shapes, so an escape hatch would
# only reopen the leak.
#
# HOP UNAVAILABLE (no interpreter / no bridge module) = LOUD WARN + PASS, the
# same fail-open the test-context guard takes on a blind detector: a blocked
# routine push silently loses an owner notification, which is the costlier
# failure. The caller's own copy is then the only defense, which is why the
# calling routines were fixed at the source as well.
_IDENT_RC=0
_IDENT_HITS="$(printf '%s' "$RAW_BODY" | "$_PUSH_PYTHON" -c "
import sys
sys.stdin.reconfigure(encoding=\"utf-8\", errors=\"replace\")
sys.stdout.reconfigure(encoding=\"utf-8\")
sys.path.insert(0, sys.argv[1])
from bridge.machine_gate import find_execution_identifiers
for kind, sample in find_execution_identifiers(sys.stdin.read()):
    sys.stdout.write('%s\t%s\n' % (kind, sample[:120]))
" "$(cd "$BRIDGE_DIR/.." && pwd)" 2>/dev/null)" || _IDENT_RC=$?
if [[ "$_IDENT_RC" -ne 0 ]]; then
  echo "[push] WARN: execution-identifier guard could not run (interpreter or bridge module unavailable) -- sending UNCHECKED; the calling routine's own copy is the only remaining defense" >&2
elif [[ -n "$_IDENT_HITS" ]]; then
  echo "[push] BLOCKED (execution-identifier guard): this message carries internal execution identifiers, which must never reach the owner's screen." >&2
  while IFS="$(printf '\t')" read -r _ig_kind _ig_sample; do
    [[ -n "$_ig_kind" ]] || continue
    echo "[push]   found ${_ig_kind}: ${_ig_sample}" >&2
  done <<< "$_IDENT_HITS"
  echo "[push]   payload: ${RAW_BODY:0:120}" >&2
  echo "[push]   fix: delete the identifier from the message text in the CALLING routine. Keep it where the maintainer reads it (stderr / log / ledger) and tell the owner in plain language what happened and what comes next. There is no override for this guard." >&2
  rm -f "$_ALERT_HITS_FILE"
  [[ -n "$_STRICT_INTENDED_FILE" ]] && rm -f "$_STRICT_INTENDED_FILE"
  exit 4
fi

# Trim to 4000 chars (Telegram 4096-char limit with headroom).
# NOTE (DGN-688): trimming HTML at a fixed offset may split a tag mid-token;
# the resulting Telegram 400 triggers the raw plain-text fallback below.
RAW_BODY="${RAW_BODY:0:4000}"

# ---- 전송 ----
# --silent 시 disable_notification=true 로 무음 발송(알림 소리/진동 없이 도착).
SILENT_ARGS=()
[[ -n "$SILENT" ]] && SILENT_ARGS=(--data-urlencode "disable_notification=true")

# DGN-835: --button "라벨::callback_data" -> reply_markup 1필드(단일 인라인 버튼).
# JSON 은 python3 로 조립(라벨 한글/특수문자 안전). 형식 불량이면 경고 후 버튼 없이 발송
# (푸시 자체는 죽지 않는다).
# 64바이트 가드 (DGN-835 final-grill MAJOR-발견3): callback_data 가 텔레그램
# 64바이트 제한을 넘으면 sendMessage 가 400 으로 죽고 -- HTML/plain 두 시도 모두
# 버튼을 물고 재실패 -> 알림 텍스트가 통째로 소실된다. 그래서 초과분은 여기서
# 선제 검사해 버튼만 드롭하고 텍스트는 반드시 발송한다 (어떤 경우에도 알림
# 본문이 사라지지 않는 것이 계약).
BUTTON_ARGS=()
if [[ -n "$BUTTON" ]]; then
  _btn_json="$(python3 -c '
import json, sys
raw = sys.argv[1]
label, sep, cb = raw.partition("::")
if not sep or not label or not cb:
    sys.exit(1)
if len(cb.encode("utf-8")) > 64:
    sys.exit(2)  # Telegram callback_data hard limit is 64 BYTES
print(json.dumps({"inline_keyboard": [[{"text": label, "callback_data": cb}]]},
                 ensure_ascii=False))
' "$BUTTON" 2>/dev/null)" && _btn_rc=0 || _btn_rc=$?  # set -e safe capture
  if [[ "$_btn_rc" -eq 0 && -n "$_btn_json" ]]; then
    BUTTON_ARGS=(--data-urlencode "reply_markup=${_btn_json}")
  elif [[ "$_btn_rc" -eq 2 ]]; then
    echo "[push] warn: --button callback_data exceeds 64 bytes (Telegram limit); sending without button" >&2
  else
    echo "[push] warn: invalid --button (expected '라벨::callback_data'); sending without button" >&2
  fi
fi
# kit_life remind: URL buttons (map links). Validated up front for the same
# reason as the 64-byte guard above: a URL Telegram rejects makes BOTH the
# HTML and the plain retry fail with the markup attached, and the alert text
# would be lost. Bad set -> warn, send the text without buttons.
if [[ -n "$URL_BUTTONS_JSON" ]]; then
  if [[ -n "$BUTTON" ]]; then
    echo "[push] warn: --url-buttons-json ignored (--button already sets the keyboard)" >&2
  elif _url_json="$(printf '%s' "$URL_BUTTONS_JSON" | python3 -c '
import json, sys
sys.stdin.reconfigure(encoding="utf-8")
rows = json.loads(sys.stdin.read())
assert isinstance(rows, list) and rows
for r in rows:
    assert isinstance(r, list) and r
    for b in r:
        assert isinstance(b, dict) and set(b) == {"text", "url"}
        assert isinstance(b["text"], str) and b["text"].strip()
        u = b["url"]
        assert isinstance(u, str) and u.startswith("https://")
        assert u.isascii() and len(u) <= 2048 and not any(c.isspace() for c in u)
print(json.dumps({"inline_keyboard": rows}, ensure_ascii=False))
' 2>/dev/null)" && [[ -n "$_url_json" ]]; then
    BUTTON_ARGS=(--data-urlencode "reply_markup=${_url_json}")
  else
    echo "[push] warn: invalid --url-buttons-json (rows of {text,url}, https only); sending without buttons" >&2
  fi
fi

# DGN-1683 WP3: strict lane -- ALL buttons ride the same message (reply_markup),
# validated here; a bad button set is a refusal, never "send without buttons".
# The response goes to a private per-call file (the shared /tmp/push_resp.json
# is racy across concurrent sends); non-strict keeps its historic path.
_RESP_FILE="/tmp/push_resp.json"
_THREAD_ARGS=()
_CURL_LIMIT=()
_S_CURL_RC=0
if [[ -n "$STRICT" ]]; then
  _RESP_FILE="$(mktemp /tmp/push_strict_resp.XXXXXX)"
  _CURL_LIMIT=(--max-time 30)
  [[ -n "$THREAD_ID" ]] && _THREAD_ARGS=(--data-urlencode "message_thread_id=${THREAD_ID}")
  if [[ -n "$BUTTONS_JSON" ]]; then
    _btn_json="$(printf '%s' "$BUTTONS_JSON" | "$_PUSH_PYTHON" -c '
import json, sys
sys.stdin.reconfigure(encoding="utf-8")
rows = json.loads(sys.stdin.read())
assert isinstance(rows, list) and rows
for r in rows:
    assert isinstance(r, list) and r
    for b in r:
        assert isinstance(b, dict) and set(b) == {"text", "callback_data"}
        assert isinstance(b["text"], str) and b["text"].strip()
        assert isinstance(b["callback_data"], str) and b["callback_data"]
        assert len(b["callback_data"].encode("utf-8")) <= 64
print(json.dumps({"inline_keyboard": rows}, ensure_ascii=False))
' 2>/dev/null)" || _strict_refuse "--buttons-json is invalid (rows of {text,callback_data}, callback_data <= 64 bytes)"
    BUTTON_ARGS=(--data-urlencode "reply_markup=${_btn_json}")
  fi
fi

# DGN-1803: --edit swaps the API method and pins the message; everything else
# (body, parse_mode, reply_markup) rides the same call as a strict send.
_TG_METHOD="sendMessage"
_EDIT_ARGS=()
if [[ -n "$EDIT_ID" ]]; then
  _TG_METHOD="editMessageText"
  _EDIT_ARGS=(--data-urlencode "message_id=${EDIT_ID}")
  _THREAD_ARGS=()      # editMessageText has no message_thread_id
  SILENT_ARGS=()       # ... and no disable_notification
fi

_send_message() {
  # Usage: _send_message <parse_mode_arg...>
  # parse_mode_arg is empty for plain, or "--data-urlencode parse_mode=HTML" for HTML.
  curl -s ${_CURL_LIMIT[@]+"${_CURL_LIMIT[@]}"} -o "$_RESP_FILE" -w '%{http_code}' \
    "https://api.telegram.org/bot${TOKEN}/${_TG_METHOD}" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    ${_EDIT_ARGS[@]+"${_EDIT_ARGS[@]}"} \
    ${_THREAD_ARGS[@]+"${_THREAD_ARGS[@]}"} \
    ${SILENT_ARGS[@]+"${SILENT_ARGS[@]}"} \
    ${BUTTON_ARGS[@]+"${BUTTON_ARGS[@]}"} \
    "$@" \
    --data-urlencode "text=${BODY}"
}

# ---- DGN-1528: outbound-record CONTRACT -----------------------------------
# CONTRACT: every outbound send leaves a record. Record it AT SEND TIME --
# not batched, not deferred -- because the window a record has to survive is
# exactly a restart or another session reading history BETWEEN the send and
# any later write; a record written after that window has already been
# missed by it.
# DGN-1642: the contract has ONE leg now. It used to have two -- a durable
# ledger row (below) plus a drop into the session-inbox injection lane that
# handed the sending session a follow-up turn to speak its own record into.
# That second leg is deleted here: it manufactured a turn with nothing to
# say, which the quiet-turn machinery then had to suppress (and sometimes
# failed to, destroying real reports in the process, DGN-1591/DGN-1627). The
# ledger row alone IS the record -- durable, independent of any one live
# session, plain-text greppable by any later reader (human or agent), any
# instance restart. A session that wants to know what it sent reads this
# ledger; it is not handed a turn to re-announce it to itself.
# Implemented at ONE choke point (the main text sendMessage below) -- --photo
# and the idrill keyboard message do not call this yet. The contract is
# written here so the NEXT outbound tool/path inherits the ONE-LEG obligation
# (append the ledger row; never spend a turn on it) rather than rediscovering
# it or re-adding a session-inbox drop: call _record_outbound (or grow an
# equivalent) at ITS send point, or its sends are invisible to this record.
# --record-ledger-only (DGN-1623) is a deprecated no-op: this is now the
# only behavior, unconditionally, for every send. The flag stays accepted
# only so existing callers (scripts/pack/mint_run.sh) keep working.
_record_outbound() {
  # $1 = the exact text that was actually transmitted (post-fallback BODY).
  local _sent_text="$1"
  local _tag="${2:-}" _tag_mid="${3:-}"   # DGN-1803: edit rows carry tag + message_id
  local _data_dir="$SCRIPT_DIR/../.telegram_bot"
  local _ts
  _ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo unknown)"

  # durable ledger -- append-only JSONL (python3 for safe JSON escaping of
  # arbitrary body text: quotes, newlines, non-ASCII). Fail-soft: never turns
  # a delivered push into a failure; a lost record is worse silently retried
  # than loudly warned.
  mkdir -p "$_data_dir" 2>/dev/null || true
  # DGN-1585 S2: audience field, appended AFTER the existing three (ts/
  # chat_id/body order untouched -- append-only compat; DGN-1586's delivery
  # accounting reads this field). Value is the RESOLVED audience (post
  # operator-unresolved fallback), i.e. who actually got this send.
  IB_TEXT="$_sent_text" IB_TS="$_ts" IB_CHAT="$CHAT_ID" IB_AUDIENCE="$AUDIENCE_RESOLVED" \
  IB_TAG="$_tag" IB_MID="$_tag_mid" \
  IB_LEDGER="$_data_dir/outbound-ledger.jsonl" "$_PUSH_PYTHON" -c '
import json, os
row = {
    "ts": os.environ.get("IB_TS", ""),
    "chat_id": os.environ.get("IB_CHAT", ""),
    "body": os.environ.get("IB_TEXT", ""),
    "audience": os.environ.get("IB_AUDIENCE", ""),
}
if os.environ.get("IB_TAG"):
    row["tag"] = os.environ["IB_TAG"]
    row["message_id"] = os.environ.get("IB_MID", "")
line = json.dumps(row, ensure_ascii=False)
with open(os.environ["IB_LEDGER"], "a", encoding="utf-8") as f:
    f.write(line + "\n")
' 2>/dev/null \
    || echo "[push] WARN: outbound ledger append failed (send already delivered; record lost this send)" >&2
}

# DGN-966: a marker-only push (body strips to whitespace after the idrill hop)
# skips the body bubble -- the keyboard message below carries the push.
_BODY_BLANK=""
if [[ -z "$(printf '%s' "$BODY" | tr -d '[:space:]')" ]]; then
  _BODY_BLANK="true"
fi

_OUTBOUND_SENT=""   # DGN-1528: set true only where an actual sendMessage fires
if [[ -n "$_BODY_BLANK" && -n "$IDRILL_KB_FILE" && -s "$IDRILL_KB_FILE" ]]; then
  HTTP_CODE="200"  # nothing to send; keyboard message is the push
elif [[ "$_SAN_RC" -eq 3 ]]; then
  # DGN-1209: the machine-line gate dropped every line -- the body was
  # machine-only. Nothing owner-facing exists; do NOT fall back to the raw
  # body (that would re-leak exactly what the gate removed).
  echo "[push] body was machine-only after the outbound gate (DGN-1209); nothing to send" >&2
  HTTP_CODE="200"
elif [[ -n "$SANITIZED" ]]; then
  # DGN-822: sanitized send is ALWAYS parse_mode=HTML (absorbs the old --html
  # branch). On HTTP 400 (e.g. a tag split by the 4000-char trim) fall back
  # to the raw pre-sanitize body as plain text (DGN-688 behavior preserved).
  BODY="${SANITIZED:0:4000}"
  if [[ -n "$STRICT" ]]; then
    # Keep the curl rc under set -e so strict classification can distinguish
    # connect-phase failure from a request whose delivery is uncertain.
    HTTP_CODE="$(_send_message --data-urlencode "parse_mode=HTML")" || _S_CURL_RC=$?
    # An edit's 400 is usually a definite verdict (not modified / not editable),
    # not a render failure: retry as plain only when Telegram blames the markup.
    if [[ "$_S_CURL_RC" -eq 0 && "$HTTP_CODE" == "400" ]] \
       && { [[ -z "$EDIT_ID" ]] || grep -qi "parse entities" "$_RESP_FILE" 2>/dev/null; }; then
      echo "[push] HTML send failed (HTTP 400); retrying gated text as plain" >&2
      BODY="$RAW_BODY"
      HTTP_CODE="$(_send_message)" || _S_CURL_RC=$?
    fi
  else
    HTTP_CODE="$(_send_message --data-urlencode "parse_mode=HTML")"
    _OUTBOUND_SENT="true"
    if [[ "$HTTP_CODE" == "400" ]]; then
      echo "[push] HTML send failed (HTTP 400); retrying raw body as plain text" >&2
      BODY="$RAW_BODY"
      HTTP_CODE="$(_send_message)"
    fi
  fi
else
  # Sanitizer unavailable (or body sanitized to empty): send the raw body as
  # plain text so the push always goes out.
  echo "[push] sanitize hop unavailable; sending raw body as plain text" >&2
  BODY="$RAW_BODY"
  HTTP_CODE="$(_send_message)"
  _OUTBOUND_SENT="true"
fi

# DGN-1683 WP3: strict outcome. Only a parsed ok:true + integer message_id is
# delivery. A definite Telegram refusal (4xx + ok:false) or connect-phase
# curl failure (5/6/7/35; curl(1) EXIT CODES) is rc 2. Other failures
# (including timeout, send/receive errors, 5xx, unparseable 200) are rc 6 =
# UNCERTAIN -- the caller must never treat that as either sent or unsent.
_STRICT_MSG_ID=""
if [[ -n "$STRICT" ]]; then
  _s_class="$("$_PUSH_PYTHON" -c '
import json, sys
path, code, crc, edit = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
try:
    d = json.load(open(path, encoding="utf-8"))
except Exception:
    d = None
res = d.get("result") if isinstance(d, dict) else None
if crc in (5, 6, 7, 35):
    print("notconnected")
elif crc == 0 and code == "200" and isinstance(d, dict) and d.get("ok") is True \
        and isinstance(res, dict) and isinstance(res.get("message_id"), int) \
        and not isinstance(res.get("message_id"), bool):
    print("ok %d" % res["message_id"])
elif crc == 0 and code[:1] == "4" and isinstance(d, dict) and d.get("ok") is False:
    desc = str(d.get("description") or "").lower()
    if edit and "not modified" in desc:
        print("notmodified")
    elif edit and ("message to edit not found" in desc or "can\x27t be edited" in desc
                   or "message_id_invalid" in desc or "message can\x27t be edited" in desc):
        print("noteditable")
    else:
        print("rejected")
else:
    print("uncertain")
' "$_RESP_FILE" "${HTTP_CODE:-000}" "$_S_CURL_RC" "$EDIT_ID" 2>/dev/null)" || _s_class="uncertain"
  rm -f "$_STRICT_INTENDED_FILE"
  case "$_s_class" in
    ok\ *)
      _STRICT_MSG_ID="${_s_class#ok }"
      _OUTBOUND_SENT="true"
      HTTP_CODE="200"
      rm -f "$_RESP_FILE"
      ;;
    notmodified)
      # DGN-1803: Telegram says the text+keyboard already match -- the screen
      # IS what the caller wants, so success; nothing changed, so no ledger row.
      echo "[push] STRICT edit: message is not modified (already current)" >&2
      rm -f "$_RESP_FILE" "$_ALERT_HITS_FILE"
      printf '{"ok":true,"message_id":%s,"chat_id":%s,"edited":false,"not_modified":true}\n' "$EDIT_ID" "$CHAT_ID"
      exit 8
      ;;
    noteditable)
      echo "[push] STRICT edit: message $EDIT_ID is not editable (not found / can't be edited / too old / not this bot's) -- not edited" >&2
      rm -f "$_RESP_FILE" "$_ALERT_HITS_FILE"
      exit 7
      ;;
    notconnected)
      echo "[push] STRICT: connection failed (curl rc=$_S_CURL_RC, HTTP ${HTTP_CODE:-000}) -- not delivered" >&2
      rm -f "$_RESP_FILE" "$_ALERT_HITS_FILE"
      exit 2
      ;;
    rejected)
      echo "[push] STRICT: telegram rejected the screen (HTTP $HTTP_CODE) -- not delivered" >&2
      rm -f "$_RESP_FILE" "$_ALERT_HITS_FILE"
      exit 2
      ;;
    *)
      echo "[push] STRICT: delivery UNCERTAIN (curl rc=$_S_CURL_RC, HTTP ${HTTP_CODE:-000}) -- treat as not confirmed" >&2
      rm -f "$_RESP_FILE" "$_ALERT_HITS_FILE"
      exit 6
      ;;
  esac
fi

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "[push] telegram failed (HTTP $HTTP_CODE):" >&2; cat "$_RESP_FILE" >&2
  if [[ -n "$IDRILL_KB_FILE" ]]; then rm -f "$IDRILL_KB_FILE"; fi
  rm -f "$_ALERT_HITS_FILE"
  exit 2
fi

# DGN-1528: record the send -- BODY at this point holds exactly what
# transmitted successfully (sanitized HTML, or the raw-text fallback after an
# HTTP-400 HTML retry). Skipped on the two "nothing to send" branches above
# (idrill-only push, machine-only body) -- there is no outbound to record.
if [[ -n "$_OUTBOUND_SENT" ]]; then
  if [[ -n "$EDIT_ID" ]]; then
    _record_outbound "$BODY" edit "$_STRICT_MSG_ID"
  else
    _record_outbound "$BODY"
  fi
fi

# ---- DGN-1756: machine-line discoveries -> ledger + operator logs only ----
# The sanitize hop persisted the markers; /health exposes technical details.
if [[ -s "$_ALERT_HITS_FILE" && -z "${DOGANY_MACHINE_ALERT_HOP:-}" ]]; then
  while IFS=$'\t' read -r _al_rail _al_toks; do
    [[ -n "$_al_rail" && -n "$_al_toks" ]] || continue
    echo "[push] machine-line gate: ledger only rail=$_al_rail tokens=$_al_toks" >&2
  done < "$_ALERT_HITS_FILE"
fi
rm -f "$_ALERT_HITS_FILE"

# DGN-966: send the staged idrill step-1 keyboard as its own message (mirrors
# the in-bot render: body bubble first, then step_text[1] + inline keyboard).
# Multi-button rows / grids ride the arm-declared spec verbatim; drilldown
# taps are handled by the LIVE bridge callback engine (same bot token), so
# nested steps work on push-sent keyboards with no extra wiring here.
if [[ -n "$IDRILL_KB_FILE" && -s "$IDRILL_KB_FILE" ]]; then
  KB_TEXT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["text"])' "$IDRILL_KB_FILE" 2>/dev/null)" || KB_TEXT=""
  KB_MARKUP="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["reply_markup"], ensure_ascii=False))' "$IDRILL_KB_FILE" 2>/dev/null)" || KB_MARKUP=""
  rm -f "$IDRILL_KB_FILE"
  if [[ -n "$KB_TEXT" && -n "$KB_MARKUP" ]]; then
    KB_CODE="$(curl -s -o /tmp/push_kb_resp.json -w '%{http_code}' \
      "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${CHAT_ID}" \
      ${SILENT_ARGS[@]+"${SILENT_ARGS[@]}"} \
      --data-urlencode "reply_markup=${KB_MARKUP}" \
      --data-urlencode "text=${KB_TEXT}")"
    if [[ "$KB_CODE" != "200" ]]; then
      # Body (if any) already delivered -- the push itself succeeded from
      # every caller's point of view. DGN-966 verification round (evidence:
      # packs/health-trainer's handoff.consume "handler crash leaves the
      # message for the next sweep" chain -- redirect-respond.sh has no
      # `|| true` around its push-gated.sh call, so a non-zero push.sh exit
      # here would leave the maildir message unconsumed and RE-SEND THE SAME
      # BODY on the next poll; the same STAMP-AFTER-PUSH retry pattern exists
      # in mirror-reconcile.sh/mirror-poll.sh). No caller in this codebase
      # distinguishes exit codes -- they all treat "non-zero" as "retry the
      # whole push". So exit 0 here (never silent: logged loud on stderr) --
      # never exit 2 for a keyboard-only failure; exit 2 stays reserved for
      # the body sendMessage failing above (never delivered, safe to retry).
      echo "[push] WARN: idrill keyboard send failed (HTTP $KB_CODE) -- body already delivered, affordance lost this send:" >&2
      cat /tmp/push_kb_resp.json >&2
    else
      echo "[push] idrill keyboard sent OK"
    fi
  fi
elif [[ -n "$IDRILL_KB_FILE" ]]; then
  rm -f "$IDRILL_KB_FILE"
fi

if [[ -n "$STRICT" ]]; then
  if [[ -n "$EDIT_ID" ]]; then
    printf '{"ok":true,"message_id":%s,"chat_id":%s,"edited":true}\n' "$_STRICT_MSG_ID" "$CHAT_ID"
    exit 0
  fi
  printf '{"ok":true,"message_id":%s,"chat_id":%s}\n' "$_STRICT_MSG_ID" "$CHAT_ID"
  exit 0
fi
echo "[push] sent OK (bot ${TOKEN%%:*} → chat ${CHAT_ID})"
exit 0
