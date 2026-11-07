"""Owner-session guard shared by the user-session hooks (v2.7 D1/D2).

The bridge persists the live owner session id in
<root>/.telegram_bot/sessions.json (status-footer.py, DGN-198 doctrine: one
writer per shared surface).  A hook that must run only in the user's own
session (the text-layer pointer, the Stop text seat, the workbench footer)
matches the transcript's session id against that file.

One small stdlib-only module instead of each hook exec-ing the 3,900-line
status-footer.py to borrow its function: an import or syntax error there
must not silently switch the other hooks off (v2.7 D2 grill F7).
status-footer.py's own _is_owner_session delegates here, so there is one
copy of the rule.

status() tells "not the owner" apart from "could not tell" so a caller can
log its fail-open instead of going quiet:
  "owner" -- the transcript's session id is listed;
  "other" -- it is not listed, there is no transcript path, or there is no
             sessions file at all (no bridge: a CLI-only instance);
  "error" -- the sessions file exists but cannot be read or parsed.
"""
import json
import os

SESSIONS_FILE = os.path.join(".telegram_bot", "sessions.json")
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__))))


def sessions_path(root=None):
    return (os.environ.get("DOGANY_SESSIONS_FILE")
            or os.path.join(root or _DEFAULT_ROOT, SESSIONS_FILE))


def session_id(transcript_path):
    sess = os.path.basename(transcript_path or "")
    return sess[:-6] if sess.endswith(".jsonl") else sess


def status(transcript_path, root=None):
    """"owner" | "other" | "error" (see the module docstring)."""
    try:
        sess = session_id(transcript_path)
        if not sess:
            return "other"
        path = sessions_path(root)
        if not os.path.exists(path):
            return "other"
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return "error"
        for val in data.values():
            if isinstance(val, dict) and val.get("session_id") == sess:
                return "owner"
        return "other"
    except Exception:
        return "error"


def is_owner_session(transcript_path, root=None):
    """Fail-closed boolean form (status-footer's historical contract)."""
    return status(transcript_path, root) == "owner"


def hook_status(data, root=None):
    """status() for a hook payload: a subagent context (payload carries
    agent_id) is never the owner's session."""
    if not isinstance(data, dict) or data.get("agent_id"):
        return "other"
    return status(data.get("transcript_path") or "", root)
