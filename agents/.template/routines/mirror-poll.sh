#!/bin/bash
# mirror-poll.sh -- DGN-180 poller cycle (cron target via cron-guard, 5min).
# sweep -> inbound pulls -> outbox drain -> notify delivery.
# Optional module: only runs when mirror/ dir and config/lifekit.conf
# MIRROR_MODULE=on are both present (flag gate, DGN-258/259).
set -euo pipefail
# Flag gate (DGN-260): exit 0 silently when mirror module is disabled or absent.
_AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_CONF="$_AGENT_ROOT/config/lifekit.conf"
if [ ! -d "$_AGENT_ROOT/mirror" ]; then exit 0; fi
if [ ! -f "$_CONF" ]; then exit 0; fi
# `|| true`: under `set -euo pipefail`, a missing MIRROR_MODULE key makes grep
# exit 1 and (pipefail) fail the whole substitution -> the routine would exit
# non-zero instead of the intended silent skip. Absent key == off == exit 0.
_MODULE_VAL="$( { grep '^MIRROR_MODULE=' "$_CONF" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
if [ "$_MODULE_VAL" != "on" ]; then exit 0; fi
# DGN-268 S3/S4 safety rail (S5 hardening slice): module is ON but Google auth
# may be absent/incomplete. Do NOT crash-loop every 5 min -- probe auth, and
# when it is missing/unauthed emit at most ONE warning per day (stamp-file
# throttle), then exit 0. Fast path: gws present + `gws auth status` exit 0.
# When the S4 preflight is present it ALSO enforces the scope set the MIRROR
# needs -- calendar + tasks ONLY (DGN-268 FIX 2). gmail.send is NOT required to
# run the calendar/tasks mirror; a user without it must not have their whole
# sync halt (that scope gates the mailer + onboarding preflight, not the rail).
_MIRROR_UNAUTH=0
if ! command -v gws >/dev/null 2>&1 || ! gws auth status >/dev/null 2>&1; then
  _MIRROR_UNAUTH=1
elif [ -x "$_AGENT_ROOT/routines/mirror-setup-check.sh" ]; then
  "$_AGENT_ROOT/routines/mirror-setup-check.sh" --quiet --require calendar,tasks >/dev/null 2>&1 || _MIRROR_UNAUTH=1
fi
if [ "$_MIRROR_UNAUTH" = "1" ]; then
  _STAMP="$_AGENT_ROOT/.telegram_bot/mirror-unauth.stamp"
  _TODAY="$(date -u +%Y-%m-%d)"
  if [ "$(cat "$_STAMP" 2>/dev/null || true)" != "$_TODAY" ]; then
    printf '%s' "$_TODAY" > "$_STAMP" 2>/dev/null || true
    "$_AGENT_ROOT/routines/push.sh" --text \
      "Calendar sync is on but not connected to Google yet. Ask me to connect your calendar to start syncing." \
      >/dev/null 2>&1 || true
  fi
  exit 0
fi
# Install home: routines/ (plist convention); modules live in
# mirror/ -- resolve relative, no absolute home paths.
cd "$_AGENT_ROOT/mirror"
# Interpreter: needs `cryptography` (http_direct AES-GCM).
# Use homebrew python3 if present; otherwise fall back to system python3.
if [ -x "/opt/homebrew/bin/python3" ]; then
    PY=/opt/homebrew/bin/python3
else
    PY=python3
fi
"$PY" - <<'PY'
import adapter, notify
state = adapter.open_state_db()
src = adapter.get_src_conn()
cal_id = adapter.get_state(state, "agent_calendar_id")
tl_id = adapter.get_state(state, "agent_tasklist_id")
out = adapter.poll_cycle(state, src, cal_id, tl_id)
n = notify.deliver_pending(state, deliver_fn=notify.push_sh_deliver)
print("cycle:", {k: (len(v) if isinstance(v, list) else v)
                 for k, v in out.items()}, "notified:", n)
PY
