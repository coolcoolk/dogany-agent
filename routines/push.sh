#!/bin/bash
# push.sh — 능동 루틴 핵심 부품 (인스턴스 인식)
# 헤드리스 claude로 내용 생성 → 텔레그램으로 능동 푸시
#
# 봇 토큰/대상은 "자기 인스턴스"의 .env에서 읽는다:
#   <스크립트>/../runtime/.env  (예: <workspace>/runtime/.env)
#   토큰이 비었거나 플레이스홀더면 전역 ~/telegram_bot/.env 로 폴백.
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
INSTANCE_ENV="$SCRIPT_DIR/../runtime/.env"
GLOBAL_ENV="$HOME/telegram_bot/.env"
PLACEHOLDER="your_bot_token_here"

MODEL="haiku"
PROMPT=""
RAW_TEXT=""
ENV_OVERRIDE=""
PHOTO_PATH=""
CAPTION=""
SILENT=""   # --silent 면 disable_notification=true (무음 발송: 알림 소리/배지 없이)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)   MODEL="$2"; shift 2 ;;
    --prompt)  PROMPT="$2"; shift 2 ;;
    --text)    RAW_TEXT="$2"; shift 2 ;;
    --photo)   PHOTO_PATH="$2"; shift 2 ;;
    --caption) CAPTION="$2"; shift 2 ;;
    --env)     ENV_OVERRIDE="$2"; shift 2 ;;
    --silent)  SILENT="true"; shift 1 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ---- .env 골라 토큰/대상 읽기 ----
read_kv() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; }

ENV_FILE="${ENV_OVERRIDE:-$INSTANCE_ENV}"
TOKEN=""; CHAT_ID=""
if [[ -f "$ENV_FILE" ]]; then
  TOKEN="$(read_kv TELEGRAM_BOT_TOKEN "$ENV_FILE")"
  CHAT_ID="$(read_kv ALLOWED_USER_IDS "$ENV_FILE" | cut -d, -f1)"
fi
# 토큰이 비었거나 플레이스홀더면 전역으로 폴백
if [[ -z "$TOKEN" || "$TOKEN" == "$PLACEHOLDER" ]]; then
  TOKEN="$(read_kv TELEGRAM_BOT_TOKEN "$GLOBAL_ENV")"
  [[ -z "$CHAT_ID" ]] && CHAT_ID="$(read_kv ALLOWED_USER_IDS "$GLOBAL_ENV" | cut -d, -f1)"
fi
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
  BODY="$(claude -p "$PROMPT" --model "$MODEL" 2>/dev/null)"
  [[ -z "$BODY" ]] && { echo "claude returned empty" >&2; exit 1; }
else
  echo "need --prompt or --text" >&2; exit 1
fi
BODY="${BODY:0:4000}"   # 텔레그램 4096자 제한

# ---- 전송 (plain text) ----
# --silent 시 disable_notification=true 로 무음 발송(알림 소리/진동 없이 도착).
SILENT_ARGS=()
[[ -n "$SILENT" ]] && SILENT_ARGS=(--data-urlencode "disable_notification=true")
HTTP_CODE="$(curl -s -o /tmp/push_resp.json -w '%{http_code}' \
  "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  ${SILENT_ARGS[@]+"${SILENT_ARGS[@]}"} \
  --data-urlencode "text=${BODY}")"

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "[push] sent OK (bot ${TOKEN%%:*} → chat ${CHAT_ID})"
  exit 0
else
  echo "[push] telegram failed (HTTP $HTTP_CODE):" >&2; cat /tmp/push_resp.json >&2; exit 2
fi
