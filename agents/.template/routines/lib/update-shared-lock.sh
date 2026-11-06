#!/bin/sh
# DGN-2129 shared-phase protocol. The /update pool holds this descriptor
# across the child lifetime. Standalone callers have no descriptor: no-op.
# Only private payload work unlocks it; source resolution and checkout
# finalization (including consume advancement) keep it held.
update_shared_lock() {
  [ -n "${DOGANY_UPDATE_SHARED_FD:-}" ] || return 0
  python3 - "$1" <<'PY'
import fcntl
import os
import sys
fd = int(os.environ['DOGANY_UPDATE_SHARED_FD'])
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[1] == 'acquire' else fcntl.LOCK_UN)
PY
}
