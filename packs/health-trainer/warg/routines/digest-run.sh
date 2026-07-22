#!/bin/bash
# digest-run.sh -- migration data digest + first-consult offer
#                  (DGN-238 phase-1 section 6, gap build 2026-07-13)
#
# Flow:
#   single-run Python flock guard (macOS has no flock(1); uses fcntl.flock)
#   -> headless claude digest (WARG_DIGEST_CMD override for tests)
#   -> shell verifies digest file exists + non-empty (deterministic)
#   -> on success: set consult_state=ready
#   -> fire push-gated.sh --trigger first-consult-offer with teaser body
#   -> on failure: leave consult_state=digesting
#     (daily watchdog in daily_job.py reverts stuck digesting->pending_data
#      after 24h; that belt already exists)
#
# Test seams (unset in deployment):
#   WARG_DIGEST_CMD   argv: <prompt-file> <warg-root>; replaces headless claude
#   WARG_PUSH_CMD     replaces push-gated.sh
#
# Environment:
#   WARG_ROOT         auto-detected from script location if unset
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARG_ROOT="${WARG_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LIB_DIR="${HANDOFF_LIB_DIR:-$SCRIPT_DIR/lib}"
PROMPT_FILE="$SCRIPT_DIR/prompts/digest.md"
LOCK_FILE="$WARG_ROOT/.telegram_bot/state/digest-run.lock"
LOG_FILE="$WARG_ROOT/.telegram_bot/logs/digest.log"
DB_PATH="$WARG_ROOT/database/lifekit.db"
CONSULT_DIR="$WARG_ROOT/files/consult"
PUSH_CMD="${WARG_PUSH_CMD:-$SCRIPT_DIR/push-gated.sh}"

TODAY="$(date +%Y-%m-%d)"
DIGEST_FILE="$CONSULT_DIR/digest-${TODAY}.md"

_log() {
  mkdir -p "$(dirname "$LOG_FILE")"
  printf '%s %s\n' "$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))')" "$*" >> "$LOG_FILE"
}

_set_cfg() {
  # deterministic config key write (same pattern as daily_job.py _set_cfg)
  python3 - "$DB_PATH" "$1" "$2" <<'PY'
import sys, sqlite3
db, key, value = sys.argv[1:4]
conn = sqlite3.connect(db)
conn.execute(
    "INSERT INTO config (key, value) VALUES (?,?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    (key, value))
conn.commit()
conn.close()
PY
}

_get_cfg() {
  python3 - "$DB_PATH" "$1" <<'PY'
import sys, sqlite3
db, key = sys.argv[1:3]
conn = sqlite3.connect(db)
row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
conn.close()
print(row[0] if row else "")
PY
}

mkdir -p "$(dirname "$LOCK_FILE")"
mkdir -p "$CONSULT_DIR"

# single-run guard: Python fcntl.flock (macOS has no flock(1) command).
# Strategy: acquire non-blocking; if busy, exit. The lock FD is kept open
# for the lifetime of a child Python process that holds it until we send
# SIGTERM on exit. We use a simpler PID-file approach instead:
#   - try to acquire the lock via Python (exits 1 if busy)
#   - hold via a background Python sleep that keeps the fd open
#   - clean up on EXIT trap
_LOCK_PID_FILE="$LOCK_FILE.pid"

python3 - "$LOCK_FILE" "$_LOCK_PID_FILE" <<'PY'
import fcntl, os, sys, time

lock_path = sys.argv[1]
pid_path  = sys.argv[2]

fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    # lock busy: another instance running
    sys.exit(1)

# Lock acquired. Write our PID so the parent can kill us on exit.
with open(pid_path, "w") as f:
    f.write(str(os.getpid()) + "\n")

# Keep the fd open (holding the lock) until killed.
# We re-exec as a simple sleep loop in the background after this.
# But we can't exec background from Python cleanly across the shell call,
# so: daemonise this process itself to hold the lock.
import signal
def cleanup(*_):
    try: os.unlink(pid_path)
    except OSError: pass
    sys.exit(0)
signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)

# Daemonize: fork + setsid, parent exits so the shell _acquire step returns.
pid = os.fork()
if pid > 0:
    # parent: write child pid and exit so the shell continues
    with open(pid_path, "w") as f:
        f.write(str(pid) + "\n")
    sys.exit(0)

# child (daemon): holds the lock fd open
os.setsid()
# keep fd; sleep until killed
while True:
    time.sleep(60)
PY
LOCK_STATUS=$?

if [ "$LOCK_STATUS" -ne 0 ]; then
  _LOCK_PID_CONTENT=""
  if [ -f "$_LOCK_PID_FILE" ]; then
    _LOCK_PID_CONTENT="$(cat "$_LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  _log "digest-run: already running (lock held by pid=${_LOCK_PID_CONTENT:-unknown} lock=$LOCK_FILE), exiting"
  exit 0
fi

_release_lock() {
  if [ -f "$_LOCK_PID_FILE" ]; then
    LPID="$(cat "$_LOCK_PID_FILE" 2>/dev/null || true)"
    [ -n "$LPID" ] && kill "$LPID" 2>/dev/null || true
    rm -f "$_LOCK_PID_FILE" "$LOCK_FILE" 2>/dev/null || true
  fi
}
trap '_release_lock' EXIT

_log "digest-run: starting (today=$TODAY)"

# run headless claude (or the test stub)
if [ -n "${WARG_DIGEST_CMD:-}" ]; then
  "$WARG_DIGEST_CMD" "$PROMPT_FILE" "$WARG_ROOT"
  EXIT_CODE=$?
else
  # env -u strips CLAUDECODE + CLAUDE_CODE_ENTRYPOINT so headless claude
  # does not see an enclosing session and refuses to start.
  (cd "$WARG_ROOT" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p "$(cat "$PROMPT_FILE")" --allowedTools "Bash,Read,Write,Edit,Glob,Grep")
  EXIT_CODE=$?
fi

# deterministic verification: shell (not the model) checks the output
if [ "$EXIT_CODE" -ne 0 ]; then
  _log "digest-run: headless claude exited $EXIT_CODE (CLAUDECODE=${CLAUDECODE:-unset}) -- leaving digesting (watchdog belt active)"
  exit 1
fi

if [ ! -s "$DIGEST_FILE" ]; then
  _log "digest-run: digest file missing or empty (expected=$DIGEST_FILE) -- leaving digesting"
  exit 1
fi

# success path: flip state, fire push
_set_cfg "consult_state" "ready"
_log "digest-run: consult_state=ready; digest file=$DIGEST_FILE"

TEASER="기록 정리가 끝났습니다. 기록 보니까 몇 가지 눈에 띄는 게 있는데, 편하실 때 첫 상담 시작해볼까요?"
"$PUSH_CMD" --trigger first-consult-offer --text "$TEASER" || {
  _log "digest-run: push gated or failed (cap exhausted?); consult_state already ready"
}

_log "digest-run: done"
