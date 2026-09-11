#!/bin/bash
# bridge/watchdog.sh -- external polling watchdog (DGN-140, layer 2).
#
# Runs every ~2 minutes from launchd (macOS) or a systemd user timer (Linux).
# The bridge writes .telegram_bot/poll_heartbeat on every getUpdates round
# trip; when that file stops advancing, the bridge process is a zombie
# (alive, receiving nothing) and gets a full service restart.
#
# Two-strike design absorbs sleep/wake: the first run after wake sees a stale
# mtime and only ARMS a strike; the restart fires on a later run only if the
# heartbeat still has not advanced after a grace period. A recovered bridge
# advances the heartbeat between runs and the strike is cleared.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/.telegram_bot"
HEARTBEAT="$DATA_DIR/poll_heartbeat"
STRIKE="$DATA_DIR/watchdog_strike"
RESTARTS="$DATA_DIR/watchdog_restarts"
RATELIMIT_MARKER="$DATA_DIR/watchdog_ratelimited"
BUSDOWN_MARKER="$DATA_DIR/watchdog_busdown"
# DGN-888 vanished-label backstop (macOS/launchd only): marker written by
# watchdog_setup.sh declaring the monitored bridge service plist. Format:
# line 1 = plist absolute path, line 2 = launchd Label. The watchdog trusts
# ONLY this marker -- it never guesses a label -> path mapping. No marker =
# no recovery (fresh install / manual mode stays untouched).
SERVICE_PLIST_MARKER="$DATA_DIR/.service_plist"
RECOVER_FAILS="$DATA_DIR/watchdog_recover_fails"      # consecutive failure count
RECOVERFAIL_MARKER="$DATA_DIR/watchdog_recoverfail"   # notify-once suppression
LOG_DIR="$DATA_DIR/logs"
LOG_FILE="$LOG_DIR/watchdog.log"

STALE_S=180        # heartbeat mtime older than this = stale
STRIKE_GRACE_S=90  # min seconds between arming a strike and restarting
RATE_WINDOW_S=3600 # trailing window for the restart rate limit
RATE_MAX=3         # max restarts inside the window
RECOVER_FAIL_NOTIFY_N=3 # consecutive recovery failures before the single notify

LABEL=""
UNIT=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --label)   LABEL="$2"; shift 2 ;;
    --unit)    UNIT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
if [ -z "$LABEL" ] && [ -z "$UNIT" ]; then
  echo "usage: watchdog.sh --label <launchd label> | --unit <systemd unit> [--dry-run]" >&2
  exit 1
fi

log() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] $*"
    return 0
  fi
  mkdir -p "$LOG_DIR"
  # Bounded log: truncate past 500KB instead of rotating (boring on purpose).
  if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 512000 ]; then
    : > "$LOG_FILE"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

mtime_of() {
  # macOS (BSD stat) first, GNU stat fallback.
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null
}

clear_strike() {
  [ "$DRY_RUN" = "1" ] && return 0
  # A fresh heartbeat ends any incident: also clear recovery-failure state so
  # a FUTURE vanished-label incident notifies again (marker is per-incident).
  rm -f "$STRIKE" "$RATELIMIT_MARKER" "$RECOVER_FAILS" "$RECOVERFAIL_MARKER"
}

# Read the launchd Label key from a plist (mirrors watchdog_setup.sh
# plist_label: plutil, then PlistBuddy, then a grep fallback; empty on failure).
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
    label="$(grep -A1 '<key>Label</key>' "$plist" 2>/dev/null \
             | grep '<string>' | head -n1 \
             | sed -E 's#.*<string>(.*)</string>.*#\1#')"
  fi
  printf '%s' "$label"
}

# Record a restart/recovery attempt into the RESTARTS ledger and prune entries
# older than the window (bounded state). Every recovery attempt counts here,
# success OR failure, so a malformed plist cannot hot-loop bootstrap attempts
# past the RATE_MAX/RATE_WINDOW_S gate (DGN-888 blocker ii).
record_attempt() {
  echo "$now" >> "$RESTARTS"
  local tmp="$RESTARTS.tmp"
  : > "$tmp"
  while read -r ts; do
    [ -n "$ts" ] || continue
    [ $(( now - ts )) -lt "$RATE_WINDOW_S" ] && echo "$ts" >> "$tmp"
  done < "$RESTARTS"
  mv -f "$tmp" "$RESTARTS"
}

notify() {
  # Best-effort user notification via the instance push script; never fatal.
  # push.sh --text is a plain curl to the Telegram API -- deliberately
  # bus-independent, so a BUS-DOWN alert survives a dead systemd user bus.
  #
  # The log line is FIRST and unconditional: push.sh is an instance file, so a
  # tree without one (the generated public build has no routines/) would make
  # every alert this watchdog raises completely silent -- a last-resort net
  # that says nothing when it fires. The log is the floor under that.
  log "notify: $1"
  if [ -x "$PROJECT_ROOT/routines/push.sh" ]; then
    "$PROJECT_ROOT/routines/push.sh" --text "$1" >/dev/null 2>&1 || true
  fi
}

# Locale for the DGN-888 recovery notify strings. Mirrors routines/
# self-update.sh msg(): env DOGANY_LANG wins, else the instance's
# config/agent.conf AGENT_LANG, else en. Shell-native on purpose (no
# bridge/i18n Python resolver): the watchdog is a last-resort net that must
# still speak when Python or the bus is dead.
_ENV_LANG="${DOGANY_LANG:-}"
DOGANY_LANG="${DOGANY_LANG:-en}"
if [ -z "$_ENV_LANG" ]; then
  _CONF_LANG="$(sed -n 's/^AGENT_LANG=//p' "$PROJECT_ROOT/config/agent.conf" 2>/dev/null | head -n1)"
  if [ -n "$_CONF_LANG" ]; then DOGANY_LANG="$_CONF_LANG"; fi
fi

# Locale-aware notify: $1 = ko text, $2 = en text. Picks by DOGANY_LANG
# (default/fallback en) and routes through notify() -- still bus-independent.
notify_lang() {
  if [ "$DOGANY_LANG" = "ko" ]; then notify "$1"; else notify "$2"; fi
}

# DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
# Do not treat this wording as final; a test pins this marker to the strings.
#
# Owner-actionable recovery steps, appended to each notice this watchdog sends
# from a state it cannot recover by itself. WHY they exist: the previous copy
# ended in "ask me and I will walk you through it", which offers a channel
# that is broken by definition -- with the bridge down, the owner's message
# cannot reach the agent to be answered (owner ruling 2026-09-11). A notice
# about an unrecoverable state must therefore name an action he can take
# WITHOUT the agent. Shape follows the approved 2026-08-16 watchdog copy: the
# message, then the recovery line on its own line.
#
# ORDER IS SIMPLEST FIRST, per the owner's own sequencing. Step 1 needs only
# the phone he is already holding and works in the common case where the bot
# process is alive but wedged (the chat command is registered owner-only in
# bridge/bot.py, DGN-997). Step 2 needs no agent at all: the local launcher
# (scripts/dogany) asks the service manager directly. No privileged command is
# offered at this level -- the one that exists is per-incident and appears
# only where it is the actual fix (handle_busdown), never first.
RECOVERY_STEPS_KO='복구 1: 채팅에 /restart 를 보내 주세요.'
# ESTATE:BEGIN the local CLI launcher named here (dogany start) is Dogany-branded; the OSS fork ships no such launcher, so this step does not exist in the public build
RECOVERY_STEPS_KO="${RECOVERY_STEPS_KO}
복구 2: 그래도 안 올라오면 기기를 켜고 터미널에서 dogany start 를 실행해 주세요."
# ESTATE:END
RECOVERY_STEPS_EN='Recover 1: send /restart in the chat.'
# ESTATE:BEGIN the local CLI launcher named here (dogany start) is Dogany-branded; the OSS fork ships no such launcher, so this step does not exist in the public build
RECOVERY_STEPS_EN="${RECOVERY_STEPS_EN}
Recover 2: if that does not bring it back, turn the machine on and run: dogany start"
# ESTATE:END

# True when a captured systemctl stderr indicates the systemd user bus is
# unreachable (the #10205 /run/user shadowing landmine, or a dead user
# manager). Distinct from an unknown/unregistered unit.
is_bus_error() {
  case "$1" in
    *"connect to bus"*|*"Failed to connect"*) return 0 ;;
    *) return 1 ;;
  esac
}

# Handle a detected user-bus outage on the systemd path: log the honest limit
# (the watchdog cannot restart anything with the bus down and does NOT
# self-heal it), then notify ONCE per incident via the bus-independent push
# path. The marker is cleared by clear_busdown() on the next healthy probe.
handle_busdown() {
  local uid; uid="$(id -u)"
  log "decision: user bus unreachable -- cannot restart; manual recovery: sudo systemctl restart user@${uid}"
  if [ "$DRY_RUN" = "0" ] && [ ! -f "$BUSDOWN_MARKER" ]; then
    touch "$BUSDOWN_MARKER"
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final; a test pins this marker to the
    # strings. This call site uses notify_lang -- the ko/en helper that
    # already lived in this file and that only two of five call sites used.
    # The two shared steps come first (RECOVERY_STEPS_*); the privileged
    # session restart is appended LAST and only here, because a dead user bus
    # is the one state in which neither shared step can work -- the chat
    # command needs a live bot and the launcher's service call needs the bus.
    notify_lang \
      "⚠️ 지금은 제가 스스로 다시 일어설 수 없는 상태예요.
${RECOVERY_STEPS_KO}
복구 3: 그래도 안 되면 관리자 권한으로 사용자 세션을 다시 시작해 주세요: sudo systemctl restart user@${uid}" \
      "⚠️ I cannot bring myself back up right now.
${RECOVERY_STEPS_EN}
Recover 3: if it is still down, restart the user session as admin: sudo systemctl restart user@${uid}"
  fi
}

# Clear the bus-down incident marker after a healthy probe; log the recovery
# once (only when a marker was actually present).
clear_busdown() {
  [ "$DRY_RUN" = "1" ] && return 0
  if [ -f "$BUSDOWN_MARKER" ]; then
    rm -f "$BUSDOWN_MARKER"
    log "decision: user bus reachable again -- cleared bus-down marker"
  fi
}

# DGN-964: self-healing service-plist marker backfill (macOS/launchd only).
# The DGN-888 vanished-label backstop is inert without $SERVICE_PLIST_MARKER,
# and the marker is written ONLY by watchdog_setup.sh -- existing installs
# that never re-ran setup, or whose bridge/ held ambiguous legacy plists,
# silently had no backstop (2026-08-21 incident: 21 min down while this
# watchdog kept logging "not registered, skipping"). Fix: while the label IS
# still registered -- the one moment the label->plist truth is OBSERVABLE
# from launchd itself -- verify/refresh the marker here. This is a VERIFIED
# resolution (launchd's own loaded-plist path via `launchctl print`), never a
# guess, so the DGN-888 "the watchdog never guesses a label->plist mapping"
# rule holds. A genuinely fresh/manual install has no registered label on a
# healthy heartbeat, so "no marker = no recovery" stays true for it.
backfill_service_marker() {
  [ "$DRY_RUN" = "1" ] && return 0
  [ -n "$LABEL" ] || return 0  # launchd path only; systemd has no plist marker
  # Cheap steady-state check first: a marker that still points at a real,
  # placeholder-free plist whose Label matches needs nothing (no launchctl).
  local cur_plist
  if [ -f "$SERVICE_PLIST_MARKER" ]; then
    cur_plist="$(head -n1 "$SERVICE_PLIST_MARKER" 2>/dev/null)"
    if [ -n "$cur_plist" ] && [ -f "$cur_plist" ] \
       && ! grep -qE '__(AGENT_NAME|PROJECT_ROOT|HOME)__' "$cur_plist" \
       && [ "$(plist_label "$cur_plist")" = "$LABEL" ]; then
      return 0
    fi
  fi
  # Marker missing or invalid: ask launchd which plist it actually loaded for
  # this label. The first "path = " line of `launchctl print` is the service
  # plist; "stdout path ="/"stderr path =" lines do not match the anchor.
  # Any parse miss (older/newer launchctl output format) falls through to a
  # no-op: behavior degrades to pre-DGN-964, never to a wrong marker.
  local loaded
  loaded="$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
            | sed -n 's/^[[:space:]]*path = //p' | head -n1)"
  case "$loaded" in *.plist) ;; *) return 0 ;; esac
  [ -f "$loaded" ] || return 0
  # Same guards as the recovery path below: placeholders + Label match.
  if grep -qE '__(AGENT_NAME|PROJECT_ROOT|HOME)__' "$loaded"; then
    return 0
  fi
  if [ "$(plist_label "$loaded")" != "$LABEL" ]; then
    return 0
  fi
  printf '%s\n%s\n' "$loaded" "$LABEL" > "$SERVICE_PLIST_MARKER"
  log "marker: service plist marker backfilled from launchd truth ($loaded)"
}

now="$(date +%s)"

# --- decision ---------------------------------------------------------------

if [ ! -f "$HEARTBEAT" ]; then
  log "decision: heartbeat file missing ($HEARTBEAT), bridge may not have started yet, skipping"
  exit 0
fi

hb_mtime="$(mtime_of "$HEARTBEAT")"
if [ -z "$hb_mtime" ]; then
  log "decision: cannot stat heartbeat file, skipping"
  exit 0
fi
age=$(( now - hb_mtime ))

if [ "$age" -lt "$STALE_S" ]; then
  log "decision: heartbeat fresh (age ${age}s), clearing strike"
  clear_strike
  # DGN-964: healthy heartbeat = the moment launchd truth is observable;
  # verify/refresh the DGN-888 recovery marker so it exists BEFORE the label
  # ever vanishes. No-op in steady state (marker already valid).
  backfill_service_marker
  exit 0
fi

if [ ! -f "$STRIKE" ]; then
  log "decision: heartbeat stale (age ${age}s), arming strike"
  if [ "$DRY_RUN" = "0" ]; then
    echo "$hb_mtime $now" > "$STRIKE"
  fi
  exit 0
fi

strike_mtime=0; strike_time=0
read -r strike_mtime strike_time < "$STRIKE" 2>/dev/null || true
strike_mtime="${strike_mtime:-0}"
strike_time="${strike_time:-0}"

if [ "$hb_mtime" -gt "$strike_mtime" ]; then
  log "decision: heartbeat advanced since strike ($strike_mtime -> $hb_mtime), clearing strike"
  clear_strike
  exit 0
fi

if [ $(( now - strike_time )) -lt "$STRIKE_GRACE_S" ]; then
  log "decision: strike armed, grace not elapsed ($(( now - strike_time ))s < ${STRIKE_GRACE_S}s), waiting"
  exit 0
fi

# --- restart path ------------------------------------------------------------

# GRILL FIX: never kick a service that is not actually registered (fresh
# installs in manual mode, renamed labels). Verify the target exists first.
#
# DGN-888 backstop: a bootout can leave the label completely UNregistered
# (2026-08-15 incident: 18 min down while the watchdog kept skipping). When
# the registrar's marker declares a matching plist, arm a recovery attempt
# (enable + bootstrap) instead of skipping -- but only validate here; the
# attempt itself runs AFTER the rate gate below (blocker ii). Marker absent
# or invalid -> keep the original skip (fresh install / manual mode safe).
RECOVER=0
RECOVER_PLIST=""
if [ -n "$LABEL" ]; then
  if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    if [ ! -f "$SERVICE_PLIST_MARKER" ]; then
      log "decision: service label not registered ($LABEL), skipping"
      exit 0
    fi
    RECOVER_PLIST="$(head -n1 "$SERVICE_PLIST_MARKER" 2>/dev/null)"
    if [ -z "$RECOVER_PLIST" ] || [ ! -f "$RECOVER_PLIST" ]; then
      log "warn: label not registered ($LABEL) and marker plist missing (${RECOVER_PLIST:-empty}), skipping"
      exit 0
    fi
    # Placeholder guard (mirrors watchdog_setup.sh): never bootstrap a plist
    # still carrying mint placeholders.
    if grep -qE '__(AGENT_NAME|PROJECT_ROOT|HOME)__' "$RECOVER_PLIST"; then
      log "warn: label not registered ($LABEL) and marker plist has unsubstituted placeholders ($RECOVER_PLIST), skipping"
      exit 0
    fi
    marker_label="$(plist_label "$RECOVER_PLIST")"
    if [ "$marker_label" != "$LABEL" ]; then
      log "warn: label not registered ($LABEL) and marker plist Label mismatch ('$marker_label' != '$LABEL'), skipping"
      exit 0
    fi
    RECOVER=1
    log "decision: service label not registered ($LABEL), marker plist valid ($RECOVER_PLIST) -- recovery armed"
  fi
else
  # 'systemctl cat' fails when the unit file is unknown OR when the systemd
  # user bus is unreachable. Capture stderr so we can tell the two apart: a
  # bus outage is a distinct, notify-worthy condition (the watchdog cannot
  # restart anything), NOT a missing unit.
  cat_err="$(systemctl --user cat "$UNIT" 2>&1 >/dev/null)"
  cat_rc=$?
  if [ "$cat_rc" -ne 0 ]; then
    if is_bus_error "$cat_err"; then
      handle_busdown
      exit 0
    fi
    log "decision: service unit not registered ($UNIT), skipping"
    exit 0
  fi
  # Probe succeeded: the bus is healthy -- clear any prior bus-down incident.
  clear_busdown
fi

# Rate limit: at most RATE_MAX restarts per trailing RATE_WINDOW_S.
recent=0
if [ -f "$RESTARTS" ]; then
  while read -r ts; do
    [ -n "$ts" ] || continue
    [ $(( now - ts )) -lt "$RATE_WINDOW_S" ] && recent=$(( recent + 1 ))
  done < "$RESTARTS"
fi
if [ "$recent" -ge "$RATE_MAX" ]; then
  log "decision: rate limited ($recent restarts in last ${RATE_WINDOW_S}s), not restarting"
  if [ "$DRY_RUN" = "0" ] && [ ! -f "$RATELIMIT_MARKER" ]; then
    touch "$RATELIMIT_MARKER"
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final. Was English-only through notify()
    # and named the component plus its rate-limit mechanism; now it says what
    # the owner can act on, in his language, through notify_lang.
    notify_lang \
      "⚠️ 여러 번 되살려 봤는데도 계속 멈춰 있어요.
${RECOVERY_STEPS_KO}" \
      "⚠️ I keep stalling even after several restarts.
${RECOVERY_STEPS_EN}"
  fi
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  if [ "$RECOVER" = "1" ]; then
    log "decision: would recover vanished label now (enable + bootstrap $RECOVER_PLIST)"
  else
    log "decision: would restart service now (heartbeat stalled ${age}s, strike unchanged)"
  fi
  exit 0
fi

if [ "$RECOVER" = "1" ]; then
  # DGN-888 recovery sequence (blocker iii): bootout can leave disabled=true,
  # which makes bootstrap a silent no-op -- enable FIRST, then bootstrap. The
  # plist has RunAtLoad=true, so a successful bootstrap already starts the
  # process: no kickstart afterward (a kickstart would double-launch).
  log "decision: recovering vanished label $LABEL (heartbeat stalled ${age}s): enable + bootstrap $RECOVER_PLIST"
  launchctl enable "gui/$(id -u)/$LABEL" >>"$LOG_FILE" 2>&1 || true
  if launchctl bootstrap "gui/$(id -u)" "$RECOVER_PLIST" >>"$LOG_FILE" 2>&1; then
    log "recovery: bootstrap succeeded for $LABEL"
    record_attempt
    rm -f "$STRIKE" "$RATELIMIT_MARKER" "$RECOVER_FAILS" "$RECOVERFAIL_MARKER"
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final. The component name and the
    # registration mechanism are gone from both legs; the outcome stays.
    notify_lang \
      "잠깐 내려가 있었는데 자동으로 되살렸어요. 지금은 정상이에요." \
      "I had gone down; I brought myself back automatically. Back up now."
    exit 0
  fi
  # Bootstrap failed: teardown after a bootout is async, so a transient EIO
  # self-heals on a later cycle -- leave the strike armed and retry next run.
  # The attempt still lands in the RESTARTS ledger (rate limit stays honest).
  # After RECOVER_FAIL_NOTIFY_N consecutive failures notify ONCE per incident
  # (marker-suppressed, same pattern as BUSDOWN/RATELIMIT).
  fails="$(cat "$RECOVER_FAILS" 2>/dev/null || true)"
  case "$fails" in ''|*[!0-9]*) fails=0 ;; esac
  fails=$(( fails + 1 ))
  echo "$fails" > "$RECOVER_FAILS"
  log "recovery: bootstrap failed for $LABEL (consecutive failure $fails), leaving for next cycle"
  if [ "$fails" -ge "$RECOVER_FAIL_NOTIFY_N" ] && [ ! -f "$RECOVERFAIL_MARKER" ]; then
    touch "$RECOVERFAIL_MARKER"
    # DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
    # Do not treat this wording as final. The recovery SHELL COMMAND is gone
    # from the owner text (it is on the log line below, for the maintainer);
    # the attempt count is a plain quantity, not an identifier, so it stays.
    recover_cmd="launchctl bootstrap gui/$(id -u) \"$RECOVER_PLIST\""
    log "decision: manual recovery command for this incident: $recover_cmd"
    notify_lang \
      "$(printf '⚠️ 자동 복구를 %s번 시도했는데 계속 실패하고 있어요.\n%s' "$fails" "$RECOVERY_STEPS_KO")" \
      "$(printf '⚠️ Automatic recovery has failed %s times in a row.\n%s' "$fails" "$RECOVERY_STEPS_EN")"
  fi
  record_attempt
  exit 0
fi

log "decision: restarting service (heartbeat stalled ${age}s, strike unchanged)"
if [ -n "$LABEL" ]; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >>"$LOG_FILE" 2>&1 || log "kickstart failed for $LABEL"
else
  restart_err="$(systemctl --user restart "$UNIT" 2>&1)"
  restart_rc=$?
  if [ -n "$restart_err" ]; then
    printf '%s\n' "$restart_err" >>"$LOG_FILE"
  fi
  if [ "$restart_rc" -ne 0 ]; then
    if is_bus_error "$restart_err"; then
      # The bus died between the probe and the restart: notify once, do not
      # record a "restart" that never happened (keeps the rate limit honest).
      handle_busdown
      exit 0
    fi
    log "systemctl restart failed for $UNIT"
  fi
fi

# Record the restart and prune entries older than the window (bounded state).
record_attempt
rm -f "$STRIKE" "$RATELIMIT_MARKER"

# DRAFT owner-facing copy -- WORDING PENDING OWNER CONFIRMATION (UX gate).
# Do not treat this wording as final. Was English-only through notify() and
# named the component and its heartbeat mechanism.
notify_lang \
  "잠깐 멈춰 있었는데 자동으로 되살렸어요. 지금은 정상이에요." \
  "I had stalled for a moment and restarted myself automatically. Back up now."
exit 0
