#!/bin/bash
# mirror-reconcile.sh -- DGN-180 W12 weekly reconcile (Sun 21:30).
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
# DGN-268 S3/S4 safety rail (S5 hardening slice): module ON but Google auth may
# be absent/incomplete -> warn at most once/day (shared stamp with mirror-poll
# so the two do not double-warn), then exit 0. No crash, no traceback. Fast
# path = gws present + auth status; the S4 preflight (when present) checks the
# scope set the MIRROR needs -- calendar + tasks ONLY (DGN-268 FIX 2). gmail.send
# is NOT required to run the mirror (it gates the mailer + onboarding preflight).
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
if [ -x "/opt/homebrew/bin/python3" ]; then
    PY=/opt/homebrew/bin/python3
else
    PY=python3
fi
"$PY" - <<'PY'
import time
import adapter, notify, reconcile
state = adapter.open_state_db()
src = adapter.get_src_conn()
cal_id = adapter.get_state(state, "agent_calendar_id")
tl_id = adapter.get_state(state, "agent_tasklist_id")
summary = None
for _attempt in range(5):  # poll cycle may hold the mirror lock briefly
    summary = reconcile.run_reconcile(state, src, cal_id, tl_id, repair=True)
    if summary.get("status") != "locked":
        break
    time.sleep(60)
adapter.outbox_drain(state, src, cal_id, tl_id)
notify.deliver_pending(state, deliver_fn=notify.push_sh_deliver)
print({k: v for k, v in (summary or {}).items() if not k.endswith("_detail")})
PY
