#!/bin/bash
# remind.sh -- timed-event alert poller (appointments + tasks), 5-min cadence.
#
# Selection lives in database/remind_select.py (DGN-273 notify policy):
#   NULL/'default' -> lead alert (task 30 min, appt 120 min) + on-time alert
#   'silent'       -> nothing
#   'start_only'   -> on-time alert only
#   'custom'       -> notify_lead_min lead alert + on-time alert
# This script is a thin pipe: read due alerts, dedup via daily sent markers
# (one send per key; keys include the start instant so a moved event
# re-alerts), localize the header, push.
#
# Usage: remind.sh [--dry] [--owner <name>] [--require-delivery]
# Exit:  0 ok (incl. non-lifekit no-op) / 1 config error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# remind-owner-v1: scoped selection never retries without its owner filter.
DRY=0
REQUIRE_DELIVERY=0
DELIVERY_FAILED=0
OWNER_ARGS=()
usage() { echo "Usage: remind.sh [--dry] [--owner <name>] [--require-delivery]" >&2; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry) DRY=1; shift ;;
    # One-shot selector callbacks need a failed transport to leave their arm
    # live.  The ordinary heartbeat retains its historical exit-0 behavior.
    --require-delivery) REQUIRE_DELIVERY=1; shift ;;
    --owner)
      [[ -n "${2:-}" && "$2" != -* ]] || { echo "[remind] --owner requires a name" >&2; usage; exit 1; }
      OWNER_ARGS=(--owner "$2"); shift 2 ;;
    *) echo "[remind] unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done
export LIFEKIT_INSTANCE_ROOT="$AGENT_DIR"

SELECT_PY="$AGENT_DIR/database/remind_select.py"
PUSH="$SCRIPT_DIR/push.sh"
# No remind_select.py = the NORMAL state of a non-lifekit mint (the selector
# ships with the lifekit pack, DGN-803 LS-5 grill G7) -> silent no-op, exit 0.
# Same tolerance contract as backup-data.sh. NEVER a hard fail: this runs on
# a 5-min cron and a hard fail here is pure error spam.
[[ -f "$SELECT_PY" ]] || exit 0
[[ -f "$PUSH" ]] || { echo "[remind] push.sh not found: $PUSH" >&2; exit 1; }

SENT_DIR="$AGENT_DIR/.remind_sent"
mkdir -p "$SENT_DIR"
SENT_LOCK_ROOT="$SENT_DIR/.locks"
mkdir -p "$SENT_LOCK_ROOT"
TODAY_SENT="$SENT_DIR/$(date +%Y-%m-%d).sent"
touch "$TODAY_SENT"

# ---- timezone (derive from agent.conf DISPLAY_TZ; fallback Asia/Seoul) ----
DISPLAY_TZ="$(grep -E '^DISPLAY_TZ=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
export TZ="${DISPLAY_TZ:-Asia/Seoul}"

# ---- locale (same pattern as morning-brief.sh) ----
AGENT_LANG="$(grep -E '^AGENT_LANG=' "$AGENT_DIR/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
AGENT_LANG="${AGENT_LANG:-en}"
# i18n_get <key> -> value ('' if missing). DGN-1789: the shared layered
# lookup (kit override > framework default, lang then en) -- never a direct
# read of config/i18n/<lang>.json, which holds framework defaults only; the
# remind.<kind>_<alert> keys a kit adds live in the kit's own layer.
i18n_get() {
  python3 "$SCRIPT_DIR/lib/i18n_lookup.py" "$AGENT_DIR" "$AGENT_LANG" "$1" 2>/dev/null || true
}

_fmt_lead() { # _fmt_lead <lead_min> <lang> -> "2h before"/"45min before" (en)
              #                              or "2시간"/"45분"            (ko)
  local m="$1" lang="${2:-en}"
  if (( m % 60 == 0 )); then
    local h=$(( m / 60 ))
    [[ "$lang" == "ko" ]] && echo "${h}시간" || echo "${h}h before"
  else
    [[ "$lang" == "ko" ]] && echo "${m}분" || echo "${m}min before"
  fi
}

hdr() { # hdr <kind> <alert> <lead_min> -> localized header line
  local key="remind.${1}_${2}" tpl time_str
  tpl="$(i18n_get "$key")"
  # DGN-1789: a kind with no key of its own (e.g. a kit's kind whose layer is
  # missing) falls back to the framework's generic header for the same alert,
  # then to reminder_header -- all i18n keys the framework ships in every
  # locale, so a missing key never renders an English literal on a ko
  # instance. Nothing resolvable -> no header line (never a literal).
  [[ -n "$tpl" ]] || tpl="$(i18n_get "remind.generic_${2}")"
  [[ -n "$tpl" ]] || tpl="$(i18n_get "reminder_header")"
  if [[ -z "$tpl" ]]; then
    echo "[remind] WARN: no i18n header for ${key} (lang=$AGENT_LANG) -- sending body only" >&2
    return 0
  fi
  time_str="$(_fmt_lead "$3" "$AGENT_LANG")"
  tpl="${tpl//\{time\}/$time_str}"
  # backward-compat: some locales may still use {min} -- replace with raw number
  printf '%s\n' "${tpl//\{min\}/$3}"
}

# --body: the kit composes the body itself (key kind alert lead body buttons;
# lines joined by \x1f -- a prep/travel alert's bold next step, then its
# why-line; buttons = Telegram URL-button rows as JSON, or empty). A selector
# that predates --body exits non-zero on the flag -> legacy columns.
BODY_MODE=1
if ! ROWS="$(python3 "$SELECT_PY" ${OWNER_ARGS[@]+"${OWNER_ARGS[@]}"} --body 2>/dev/null)"; then
  # An old or broken selector must never widen a constrained poller.
  if [[ ${#OWNER_ARGS[@]} -gt 0 ]]; then
    echo "[remind] scoped selector failed; refusing unrestricted fallback" >&2
    exit 1
  fi
  BODY_MODE=0
  ROWS="$(python3 "$SELECT_PY" 2>/dev/null || true)"
fi
[[ -z "$ROWS" ]] && exit 0

# URL buttons (map links on prep/travel alerts) need a push.sh that knows
# --url-buttons-json; an older push.sh would refuse the unknown arg and the
# alert would be lost, so ask once and drop the buttons when it cannot.
URL_BUTTONS=0
"$PUSH" --capabilities 2>/dev/null | grep -qw url-buttons && URL_BUTTONS=1

while IFS=$'\t' read -r key kind alert lead hhmm title location purpose persons; do
  [[ -z "$key" ]] && continue
  grep -qxF "$key" "$TODAY_SENT" && continue
  LOCK_ID="$(printf '%s' "$key" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
  LOCK_DIR="$SENT_LOCK_ROOT/$LOCK_ID"
  mkdir "$LOCK_DIR" 2>/dev/null || continue
  if [[ -z "$kind" || -z "$alert" ]]; then
    echo "[remind] skipped invalid row ($key): missing kind or alert" >&2
    rmdir "$LOCK_DIR"
    continue
  fi
  BUTTONS=""
  if [[ "$BODY_MODE" == "1" ]]; then
    # 6 columns (no tab inside the body or the JSON): the body lands in the
    # 5th var, the buttons JSON (possibly empty) in the 6th
    BODY="${hhmm//$'\x1f'/$'\n'}"
    [[ "$URL_BUTTONS" == "1" ]] && BUTTONS="$title"
  else
    BODY="$title"
    [[ -n "$hhmm" ]] && BODY+=" · $hhmm"
    [[ -n "$location" ]] && BODY+=" · $location"
    [[ -n "$purpose" ]] && BODY+=" · $purpose"
    [[ -n "$persons" ]] && BODY+=" · $persons"
  fi
  if [[ -z "${BODY//[[:space:]]/}" ]]; then
    echo "[remind] skipped invalid row ($key): empty body" >&2
    rmdir "$LOCK_DIR"
    continue
  fi
  HEADER="$(hdr "$kind" "$alert" "$lead")"
  if [[ -n "$HEADER" ]]; then
    MSG="$HEADER
$BODY"
  else
    MSG="$BODY"
  fi
  if [[ "$DRY" == "1" ]]; then
    echo "[DRY] $MSG"
    rmdir "$LOCK_DIR"
    continue
  fi
  PUSH_ARGS=(--text "$MSG" --audience owner)
  [[ -n "$BUTTONS" ]] && PUSH_ARGS+=(--url-buttons-json "$BUTTONS")
  if "$PUSH" "${PUSH_ARGS[@]}"; then
    echo "$key" >> "$TODAY_SENT"
  else
    echo "[remind] push failed ($key)" >&2
    DELIVERY_FAILED=1
  fi
  rmdir "$LOCK_DIR"
done <<< "$ROWS"

[[ "$REQUIRE_DELIVERY" == "1" && "$DELIVERY_FAILED" == "1" ]] && exit 1
exit 0
