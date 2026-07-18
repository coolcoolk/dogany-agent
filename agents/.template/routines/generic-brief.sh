#!/bin/bash
# generic-brief.sh {morning|retro|weekly} -- kit-neutral briefing skeleton +
# runtime composition (DGN-227 E1 layer 1 / E2 routing / DGN-420).
#
# Sources are kit-neutral and PII-free by construction: role prose (config-
# driven, form-of-address already substituted at onboarding), today's/next-day
# schedule via the mirror when present, memory-engine highlights when present,
# own domain section (layer-2 pack output when present), and -- when acting as
# main -- peer section aggregation via routines/lib/handoff-aggregate (E1-3).
#
# Routing (DGN-227 E2-2/E3): reads BRIEF_ROUTING from config/agent.conf at
# RUN time.
#   standalone (no main) -> self-publish the composed briefing (proactive push).
#   submit (main present) -> do NOT utter; write the section file into the main
#     peer's files/handoff inbox as report.section.<slot> (generation=domain;
#     aggregation=main -- the 2026-07-10 submission-box model, reused verbatim).
# Key precedence: BRIEF_ROUTING wins; absent -> peer-key fallback
# (HANDOFF_PEER_MAIN, then legacy HANDOFF_PEER_AG); conflict -> loud log.
#
# P20 loud-fail (E2-2): in submit mode, if the main peer root / handoff inbox
# is absent or unwritable, (a) push a one-a-day throttled warning, (b) fall
# back to a standalone utterance this run so the briefing is never silently
# lost. "unset = quietly exit 0" holds ONLY for the standalone normal case.
#
# Config-driven briefing times (DGN-420; DGN-422 onboarding writes them):
#   BRIEF_TIME_MORNING (default 07:00), BRIEF_TIME_RETRO (default 22:00),
#   BRIEF_TIME_WEEKLY  (default Sun 20:00). Consumed here as the wording clock;
#   the launchd calendar interval is regenerated from the same keys by the
#   DGN-422 onboarding step (documented seam -- these keys are that seam).
#
# Transport seam (test-only): DOGANY_BRIEF_SINK, when set, captures the composed
# utterance / submit action to that file instead of touching push.sh or a live
# channel. Honored only for sandbox rehearsal; live runs go through push.sh.
set -euo pipefail

SLOT="${1:-}"
case "$SLOT" in
  morning|retro|weekly) : ;;
  *) echo "usage: generic-brief.sh {morning|retro|weekly}" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/config/agent.conf"
PUSH_SH="$ROOT/routines/push.sh"

conf_get() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# ---------------------------------------------------------------------------
# config-driven briefing times (DGN-420 seam for DGN-422)
# ---------------------------------------------------------------------------
BRIEF_TIME_MORNING="$(conf_get BRIEF_TIME_MORNING)"; BRIEF_TIME_MORNING="${BRIEF_TIME_MORNING:-07:00}"
BRIEF_TIME_RETRO="$(conf_get BRIEF_TIME_RETRO)";     BRIEF_TIME_RETRO="${BRIEF_TIME_RETRO:-22:00}"
# weekly default carries a weekday component: "Sun 20:00".
BRIEF_TIME_WEEKLY="$(conf_get BRIEF_TIME_WEEKLY)";   BRIEF_TIME_WEEKLY="${BRIEF_TIME_WEEKLY:-Sun 20:00}"
case "$SLOT" in
  morning) SLOT_TIME="$BRIEF_TIME_MORNING" ;;
  retro)   SLOT_TIME="$BRIEF_TIME_RETRO" ;;
  weekly)  SLOT_TIME="$BRIEF_TIME_WEEKLY" ;;
esac

# ---------------------------------------------------------------------------
# routing decision (E2-2 precedence: BRIEF_ROUTING > peer-key fallback > conflict)
# ---------------------------------------------------------------------------
ROUTING="$(conf_get BRIEF_ROUTING)"
PEER_MAIN="$(conf_get HANDOFF_PEER_MAIN)"
PEER_AG="$(conf_get HANDOFF_PEER_AG)"
MAIN_ROOT="${PEER_MAIN:-$PEER_AG}"   # briefing-reader alias (E2-2 rule 2)

if [[ -z "$ROUTING" ]]; then
  if [[ -n "$PEER_MAIN" || -n "$PEER_AG" ]]; then ROUTING="submit"; else ROUTING="standalone"; fi
elif [[ "$ROUTING" == "standalone" && ( -n "$PEER_MAIN" || -n "$PEER_AG" ) ]]; then
  echo "[generic-brief] WARN: BRIEF_ROUTING=standalone but a briefing peer key is set -- BRIEF_ROUTING wins (E2-2 rule 3)" >&2
fi

# ---------------------------------------------------------------------------
# locale (i18n, DGN-210): ko | en. Address already substituted at onboarding.
# ---------------------------------------------------------------------------
AGENT_LANG="$(conf_get AGENT_LANG | tr -d '[:space:]')"; AGENT_LANG="${AGENT_LANG:-en}"
I18N="$ROOT/config/i18n/${AGENT_LANG}.json"
i18n_get() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$I18N" "$1" 2>/dev/null || true; }
ADDRESS="$(conf_get AGENT_ADDRESS | tr -d '[:space:]')"
[[ -z "$ADDRESS" ]] && ADDRESS="$(i18n_get address)"
TONE="$(i18n_get tone_guide)"

# Role prose (generic life-assistant default; DGN-420 confirmed input). The
# instance's own ROLE_PROSE (onboarding/pack stamp) wins when present; the
# generic default is the fallback so a blank/no-pack domain still speaks a role.
ROLE_PROSE="$(conf_get ROLE_PROSE)"
if [[ -z "$ROLE_PROSE" ]]; then
  if [[ "$AGENT_LANG" == "ko" ]]; then
    ROLE_PROSE="당신의 일상을 곁에서 챙기는 생활비서예요. 일정과 할 일, 기록을 한곳에 정리해 두고, 필요한 걸 제때 챙겨 하루를 한결 가볍게 만들어 드립니다."
  else
    ROLE_PROSE="I'm your life assistant, looking after your day alongside you. I keep your schedule, to-dos, and notes organized in one place, and handle what's needed on time so your day feels lighter."
  fi
fi

TODAY="$(date +%F)"

# ---------------------------------------------------------------------------
# kit-neutral content sources (all best-effort; absence never aborts)
# ---------------------------------------------------------------------------
# (1) schedule -- mirror when present (kit-neutral; no lifekit.db dependency).
SCHED_TXT=""
MIRROR_SH="$ROOT/mirror/mirror.sh"
if [[ -x "$MIRROR_SH" || -f "$MIRROR_SH" ]]; then
  SCHED_TXT="$("$MIRROR_SH" today 2>/dev/null || true)"
fi
# (2) memory highlights -- memory-engine when present.
MEM_TXT=""
MEM_SH="$ROOT/memory-engine/recall.sh"
if [[ -x "$MEM_SH" || -f "$MEM_SH" ]]; then
  MEM_TXT="$("$MEM_SH" highlights "$SLOT" 2>/dev/null || true)"
fi
# (3) own domain section -- layer-2 pack output when present, else minimal.
OWN_SECTION=""
OWN_GEN="$ROOT/routines/domain-section.sh"
if [[ -x "$OWN_GEN" || -f "$OWN_GEN" ]]; then
  OWN_SECTION="$(bash "$OWN_GEN" "$SLOT" 2>/dev/null || true)"
fi

# ---------------------------------------------------------------------------
# submit-mode preflight (E2-2): peer root + writable handoff inbox.
# Returns 0 = submit target healthy, 1 = P20 loud-fail (invalid peer).
# ---------------------------------------------------------------------------
SUBMIT_INBOX=""
submit_target_ok() {
  [[ -n "$MAIN_ROOT" && -d "$MAIN_ROOT" ]] || return 1
  local inbox="$MAIN_ROOT/files/handoff"
  mkdir -p "$inbox" 2>/dev/null || return 1
  [[ -w "$inbox" ]] || return 1
  SUBMIT_INBOX="$inbox"
  return 0
}

# P20 warning push, throttled to once a day (E2-2 (a)).
p20_warn() {
  local stamp="$ROOT/.telegram_bot/logs/.p20-warn.$SLOT.$TODAY"
  local msg_ko="[브리핑] $SLOT: 메인 피어(${MAIN_ROOT:-미지정}) 제출 대상이 무효라 이번 회차는 직접 발화로 폴백했어요 (제출함 경로 점검 필요)."
  local msg_en="[briefing] $SLOT: main peer (${MAIN_ROOT:-unset}) submit target is invalid; this run fell back to a direct utterance (check the submission-box path)."
  local msg; [[ "$AGENT_LANG" == "ko" ]] && msg="$msg_ko" || msg="$msg_en"
  echo "[generic-brief] P20 loud-fail: $msg" >&2
  if [[ -n "${DOGANY_BRIEF_SINK:-}" ]]; then
    printf 'P20-WARN slot=%s peer=%s :: %s\n' "$SLOT" "${MAIN_ROOT:-unset}" "$msg" >> "$DOGANY_BRIEF_SINK"
    return 0
  fi
  mkdir -p "$(dirname "$stamp")" 2>/dev/null || true
  if [[ ! -f "$stamp" ]]; then
    : > "$stamp" 2>/dev/null || true
    [[ -f "$PUSH_SH" ]] && "$PUSH_SH" --text "$msg" >/dev/null 2>&1 || true
  fi
}

# ---------------------------------------------------------------------------
# compose the standalone utterance DATA + PROMPT (self-contained briefing)
# ---------------------------------------------------------------------------
compose_data() {
  local data="[$SLOT $TODAY $SLOT_TIME]"$'\n\n'
  data+="# role"$'\n'"$ROLE_PROSE"$'\n'
  data+=$'\n'"# schedule"$'\n'
  if [[ -n "$SCHED_TXT" ]]; then data+="$SCHED_TXT"$'\n'; else data+="(none)"$'\n'; fi
  if [[ -n "$MEM_TXT" ]]; then data+=$'\n'"# highlights"$'\n'"$MEM_TXT"$'\n'; fi
  if [[ -n "$OWN_SECTION" ]]; then data+=$'\n'"# my section"$'\n'"$OWN_SECTION"$'\n'; fi
  # peer aggregation (only when acting as main -- BRIEF_PEERS present + lib present)
  local agg_lib="$ROOT/routines/lib/handoff-aggregate"
  local peers; peers="$(conf_get BRIEF_PEERS)"
  if [[ -n "$peers" && -f "$agg_lib" ]]; then
    # shellcheck source=/dev/null
    source "$agg_lib"
    local agg; agg="$(handoff_aggregate "$ROOT" "$SLOT" "$peers" 2>/dev/null || true)"
    [[ -n "$agg" ]] && data+=$'\n'"# peers"$'\n'"$agg"$'\n'
  fi
  printf '%s' "$data"
}

compose_prompt() {
  local data="$1"
  cat <<PROMPT
You are the user's assistant sending the ${SLOT} briefing over Telegram (scheduled ${SLOT_TIME}).
Write in the user's language (locale: ${AGENT_LANG}). Address the user as: ${ADDRESS:-"(no fixed form; write naturally)"}.
Your role: ${ROLE_PROSE}
Tone rules: ${TONE}

Compose a short, warm ${SLOT} briefing from the data below. Lead with a one-line greeting in your own voice; then render the schedule (omit the section entirely if none -- no filler); include highlights / my section / peer sections only when present, each verbatim as its own short block. Never invent facts or numbers. Output the message body only.

=== DATA ===
${data}
PROMPT
}

# emit a standalone utterance (proactive push, or the test sink).
utter_standalone() {
  local data prompt
  data="$(compose_data)"
  prompt="$(compose_prompt "$data")"
  if [[ -n "${DOGANY_BRIEF_SINK:-}" ]]; then
    {
      printf 'UTTER slot=%s routing=standalone lang=%s time=%s\n' "$SLOT" "$AGENT_LANG" "$SLOT_TIME"
      printf 'ROLE_PROSE:: %s\n' "$ROLE_PROSE"
      printf '=== PROMPT ===\n%s\n=== END ===\n' "$prompt"
    } >> "$DOGANY_BRIEF_SINK"
    return 0
  fi
  [[ -f "$PUSH_SH" ]] || { echo "[generic-brief] push.sh not found: $PUSH_SH" >&2; return 1; }
  exec "$PUSH_SH" --model haiku --prompt "$prompt"
}

# ---------------------------------------------------------------------------
# submit-mode: write the domain's own section into the main's handoff inbox
# (generation=domain). File type = report.section.<slot>; section cap 10 lines
# (weekly permits a coaching block -- DGN-238 inheritance).
# ---------------------------------------------------------------------------
build_section() {
  local body="" cap=10
  [[ "$SLOT" == "weekly" ]] && cap=40   # weekly coaching block allowance
  if [[ -n "$OWN_SECTION" ]]; then
    body="$OWN_SECTION"
  else
    # minimal own section from kit-neutral sources
    [[ -n "$SCHED_TXT" ]] && body+="$SCHED_TXT"$'\n'
    [[ -n "$MEM_TXT" ]] && body+="$MEM_TXT"$'\n'
    [[ -z "$body" ]] && body="(no section content this ${SLOT})"
  fi
  printf '%s\n' "$body" | sed '/^$/d' | head -n "$cap"
}

submit_section() {
  local section created stamp fname
  section="$(build_section)"
  created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  stamp="$(date +%Y%m%d-%H%M%S)"
  fname="${stamp}-report.section.${SLOT}-$(basename "$ROOT").md"
  local self_disp; self_disp="$(conf_get DOGANY_AGENT_LABEL)"
  [[ -n "$self_disp" ]] || self_disp="$(basename "$ROOT")"
  local content
  content="---"$'\n'"type: report.section.${SLOT}"$'\n'"from: $(basename "$ROOT")"$'\n'"display: ${self_disp}"$'\n'"created: ${created}"$'\n'"---"$'\n'"${section}"$'\n'
  if [[ -n "${DOGANY_BRIEF_SINK:-}" ]]; then
    printf 'SUBMIT slot=%s routing=submit peer=%s file=%s\n' "$SLOT" "$MAIN_ROOT" "$SUBMIT_INBOX/$fname" >> "$DOGANY_BRIEF_SINK"
    printf '%s' "$content" > "$SUBMIT_INBOX/$fname"
    return 0
  fi
  printf '%s' "$content" > "$SUBMIT_INBOX/$fname"
  echo "[generic-brief] submitted $SLOT section -> $SUBMIT_INBOX/$fname"
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
if [[ "$ROUTING" == "submit" ]]; then
  if submit_target_ok; then
    submit_section
    exit 0
  fi
  # P20: peer invalid -> loud warn (1/day) + standalone fallback this run.
  p20_warn
  utter_standalone
  exit 0
fi

# standalone (main absent): the domain's self-publish IS the only briefing.
utter_standalone
exit 0
