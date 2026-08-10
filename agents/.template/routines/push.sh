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
#
# 종료코드: 0 성공 / 1 설정오류 / 2 텔레그램 전송 실패

set -euo pipefail

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
    --html)    shift 1 ;;  # DGN-822: deprecated no-op (sanitize pipe always sends HTML)
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

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

_send_message() {
  # Usage: _send_message <parse_mode_arg...>
  # parse_mode_arg is empty for plain, or "--data-urlencode parse_mode=HTML" for HTML.
  curl -s -o /tmp/push_resp.json -w '%{http_code}' \
    "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    ${SILENT_ARGS[@]+"${SILENT_ARGS[@]}"} \
    "$@" \
    --data-urlencode "text=${BODY}"
}

if [[ -n "$SANITIZED" ]]; then
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

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "[push] sent OK (bot ${TOKEN%%:*} → chat ${CHAT_ID})"
  exit 0
else
  echo "[push] telegram failed (HTTP $HTTP_CODE):" >&2; cat /tmp/push_resp.json >&2; exit 2
fi
