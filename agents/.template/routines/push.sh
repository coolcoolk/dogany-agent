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
#   push.sh --text "..." --button "라벨::callback_data"   (인라인 버튼 1개, DGN-835)
#     - reply_markup 1필드(단일 버튼)만 지원. callback_data는 텔레그램 64바이트 제한
#       -- 초과분은 여기서 가드해 버튼만 드롭하고 텍스트는 반드시 발송한다.
#     - 사진(sendPhoto) 경로에는 적용되지 않음(텍스트 발송 전용).
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
# 종료코드: 0 성공(키보드만 실패해도 0) / 1 설정오류 / 2 텔레그램 본문 전송 실패
#          / 3 테스트 맥락 가드 거부 (DGN-1122; PUSH_GUARD_OVERRIDE=send-anyway 로 명시 통과)

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
# DGN-822: --html is a deprecated no-op. Every text send now routes through the
# bridge sanitizer (bridge/formatting.py sanitize_message_for_telegram) and
# goes out as parse_mode=HTML; whitelisted Telegram tags in the body
# (<b> <i> <u> <s> <code> <pre> <blockquote> <a href>) still pass through.

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
    --html)    shift 1 ;;  # DGN-822: deprecated no-op (sanitize pipe always sends HTML)
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

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
read_kv() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; }

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
# no hardcoded recipient — chat id must come from .env (ALLOWED_USER_IDS).
if [[ -z "$CHAT_ID" ]]; then
  echo "no chat id found (set ALLOWED_USER_IDS in $ENV_FILE or $GLOBAL_ENV)" >&2; exit 1
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
SANITIZED="$(printf '%s' "$BODY" | "$_PUSH_PYTHON" -c "
import sys
sys.stdin.reconfigure(encoding=\"utf-8\", errors=\"replace\")
sys.stdout.reconfigure(encoding=\"utf-8\")
sys.path.insert(0, sys.argv[1])
from bridge.formatting import sanitize_message_for_telegram
sys.stdout.write(sanitize_message_for_telegram(sys.stdin.read()))
" "$(cd "$BRIDGE_DIR/.." && pwd)" 2>/dev/null)" || SANITIZED=""

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

_send_message() {
  # Usage: _send_message <parse_mode_arg...>
  # parse_mode_arg is empty for plain, or "--data-urlencode parse_mode=HTML" for HTML.
  curl -s -o /tmp/push_resp.json -w '%{http_code}' \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    ${SILENT_ARGS[@]+"${SILENT_ARGS[@]}"} \
    ${BUTTON_ARGS[@]+"${BUTTON_ARGS[@]}"} \
    "$@" \
    --data-urlencode "text=${BODY}"
}

# DGN-966: a marker-only push (body strips to whitespace after the idrill hop)
# skips the body bubble -- the keyboard message below carries the push.
_BODY_BLANK=""
if [[ -z "$(printf '%s' "$BODY" | tr -d '[:space:]')" ]]; then
  _BODY_BLANK="true"
fi

if [[ -n "$_BODY_BLANK" && -n "$IDRILL_KB_FILE" && -s "$IDRILL_KB_FILE" ]]; then
  HTTP_CODE="200"  # nothing to send; keyboard message is the push
elif [[ -n "$SANITIZED" ]]; then
  # DGN-822: sanitized send is ALWAYS parse_mode=HTML (absorbs the old --html
  # branch). On HTTP 400 (e.g. a tag split by the 4000-char trim) fall back
  # to the raw pre-sanitize body as plain text (DGN-688 behavior preserved).
  BODY="${SANITIZED:0:4000}"
  HTTP_CODE="$(_send_message --data-urlencode "parse_mode=HTML")"
  if [[ "$HTTP_CODE" == "400" ]]; then
    echo "[push] HTML send failed (HTTP 400); retrying raw body as plain text" >&2
    BODY="$RAW_BODY"
    HTTP_CODE="$(_send_message)"
  fi
else
  # Sanitizer unavailable (or body sanitized to empty): send the raw body as
  # plain text so the push always goes out.
  echo "[push] sanitize hop unavailable; sending raw body as plain text" >&2
  BODY="$RAW_BODY"
  HTTP_CODE="$(_send_message)"
fi

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "[push] telegram failed (HTTP $HTTP_CODE):" >&2; cat /tmp/push_resp.json >&2
  if [[ -n "$IDRILL_KB_FILE" ]]; then rm -f "$IDRILL_KB_FILE"; fi
  exit 2
fi

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

echo "[push] sent OK (bot ${TOKEN%%:*} → chat ${CHAT_ID})"
exit 0
