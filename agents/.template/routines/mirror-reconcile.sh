#!/bin/bash
# mirror-reconcile.sh -- DGN-180 W12 weekly reconcile (Sun 21:30).
# Optional module: only runs when mirror/ dir and config/lifekit.conf
# MIRROR_MODULE=on are both present (flag gate, DGN-258/259).
set -euo pipefail
# Install home: routines/ (plist convention); modules live in
# mirror/ -- resolve relative, no absolute home paths.
cd "$(dirname "${BASH_SOURCE[0]}")/../mirror"
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
