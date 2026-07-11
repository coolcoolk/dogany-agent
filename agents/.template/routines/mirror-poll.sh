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
_MODULE_VAL="$(grep '^MIRROR_MODULE=' "$_CONF" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
if [ "$_MODULE_VAL" != "on" ]; then exit 0; fi
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
