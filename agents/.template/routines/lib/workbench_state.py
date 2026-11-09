"""Workbench state shared by the hooks (DGN-1967 S7).

One owner for the small on-disk records the workbench hooks read and write,
so no hook has to load another hook's file to reach them:

  owner gate         is_owner_session -- the bridge's live owner session
                     (.telegram_bot/sessions.json); fail-closed
  stop records       stop_record / record_agent_stop -- one JSON per
                     deliberately stopped subagent (status-footer Rev 14)
  board-owner record record_board_owner / board_owner_transcript -- the
                     owner transcript --board-refresh rebuilds from
                     (DGN-1915)
  refresh token      bump_board_refresh_token / board_refresh_token -- the
                     trailing-edge debounce token of --board-refresh

Importers: routines/status-footer.py (re-exports the old private names),
routines/bg-job-notice.py (owner gate), routines/subagent-stop-record.py
(stop-record writer).  Stdlib only; every reader is fail-open toward the
conservative direction its caller documents.
"""

import json
import os
import re
import time

# <workspace_root>/routines/lib/workbench_state.py -> <workspace_root>
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))

SESSIONS_FILE = ".telegram_bot/sessions.json"
BOARD_OWNER_FILE = "workbench-owner.json"
BOARD_REFRESH_REQUEST = "board-refresh.request"
STOP_ID_RE = re.compile(r"^[0-9a-fA-F]{1,64}$")


def bot_data_dir():
    """BOT_DATA_DIR = <workspace_root>/.telegram_bot, same derivation as the
    bridge (config.py: PROJECT_ROOT / ".telegram_bot").  Env override
    DOGANY_BOT_DATA_DIR (test isolation)."""
    return os.environ.get("DOGANY_BOT_DATA_DIR") or os.path.join(
        ROOT_DIR, ".telegram_bot")


def atomic_write(path, text):
    """Write `text` to `path` via tmp + os.replace (parent dirs created)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Owner gate (DGN-214 grill FATAL-1)
# ---------------------------------------------------------------------------
# The Stop hook fires in EVERY Claude session under this project config:
# headless crons, junior worktree sessions, subagent contexts, terminals.
# Only the bridge's live owner session may write the owner surfaces -- one
# writer per shared surface (DGN-198).  The bridge persists the owner
# session id in .telegram_bot/sessions.json; match against it.  Fail-closed:
# unreadable state -> not the owner.

def is_owner_session(transcript_path, root=None):
    # One copy of the rule: lib/owner_session.py (v2.7 D2 grill F7).  Same
    # contract: fail-closed, DOGANY_SESSIONS_FILE override.
    try:
        import owner_session
        return owner_session.is_owner_session(transcript_path,
                                              root or ROOT_DIR)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Deliberate-stop records (status-footer Rev 14)
# ---------------------------------------------------------------------------
# Written by whoever performs the stop, at the moment of the stop.  The board
# drops the agent the instant the record exists; the file stays as the
# session's own stop history.  Never derived by scanning: a stop nobody
# records is indistinguishable from a crash, which is the conservative
# direction (the presumed-dead alarm still fires).

def stops_dir():
    """Directory holding one stop record per stopped agent.  Env override
    DOGANY_SUBAGENT_STOPS_DIR (test isolation)."""
    return os.environ.get("DOGANY_SUBAGENT_STOPS_DIR") or os.path.join(
        ROOT_DIR, ".telegram_bot", "subagent-stops")


def stop_record_path(agent_id):
    """Path for an agent's stop record, or None if the id is not a plain hex
    agent id (the id reaches this from a transcript and from argv, so it is
    never interpolated into a path unvalidated)."""
    if not agent_id or not STOP_ID_RE.match(agent_id):
        return None
    return os.path.join(stops_dir(), "%s.json" % agent_id)


def stop_record(agent_id):
    """{"reason": str, "ts": int} if this agent was deliberately stopped,
    else None.  Every failure mode (missing / unreadable / malformed file,
    missing or non-int ts) degrades to None on purpose: a broken stop record
    must never be able to suppress the death alarm."""
    try:
        path = stop_record_path(agent_id)
        if not path or not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rec = json.load(fh)
        if not isinstance(rec, dict):
            return None
        ts = int(rec.get("ts"))
        reason = (rec.get("reason") or "").strip()
        return {"reason": reason, "ts": ts}
    except Exception:
        return None


def record_agent_stop(agent_id, reason=""):
    """Write the stop record for `agent_id`.  True on success.  Rewriting an
    existing record is intentional: the newest stop is the one the history
    should show."""
    path = stop_record_path(agent_id)
    if not path:
        return False
    try:
        atomic_write(path, json.dumps(
            {"agent_id": agent_id,
             "reason": (reason or "").strip(),
             "ts": int(time.time())},
            ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Board-owner record + refresh token (DGN-1915 out-of-turn refresh)
# ---------------------------------------------------------------------------

def record_board_owner(transcript_path):
    """Remember the owner transcript for --board-refresh.  Fail-open."""
    try:
        path = os.path.join(bot_data_dir(), BOARD_OWNER_FILE)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if json.load(fh).get("transcript_path") == transcript_path:
                    return
        except Exception:
            pass
        atomic_write(path, json.dumps({"transcript_path": transcript_path,
                                       "ts": int(time.time())}))
    except Exception:
        pass


def board_owner_transcript():
    """The recorded owner transcript, or None when it is absent or no
    longer the live owner session (fail-closed)."""
    try:
        with open(os.path.join(bot_data_dir(), BOARD_OWNER_FILE), "r",
                  encoding="utf-8") as fh:
            path = json.load(fh).get("transcript_path") or ""
    except Exception:
        return None
    if not isinstance(path, str) or not is_owner_session(path):
        return None
    return path


def bump_board_refresh_token():
    """Move the refresh request token (raises OSError on a write failure)."""
    atomic_write(os.path.join(bot_data_dir(), BOARD_REFRESH_REQUEST),
                 "%d.%d" % (time.time_ns(), os.getpid()))


def board_refresh_token():
    try:
        with open(os.path.join(bot_data_dir(), BOARD_REFRESH_REQUEST), "r",
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""
