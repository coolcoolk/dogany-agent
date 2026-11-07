#!/usr/bin/env python3
"""PostToolUse text-layer pointer (v2.7 D1).

How a reply to the user is written (shape, containers, register, wording)
lives in one cold doc, rules/cold.framework.text.md. It is not hot-loaded:
a PostToolUse hook cannot know whether the next step is another tool call
or the final reply, so after every tool call in the user's session it
injects ONE conditional line and the model reads the doc only when it is
about to write. The only other line is the user's language (DGN-2188),
unconditional because narration between tool calls is user-visible too.

FDGN-578: the language line also rides UserPromptSubmit, so EVERY turn
carries it -- including a bridge-injected turn (dispatch-return,
quiet-recovery) that writes its reply without any tool call, where no
PostToolUse ever fires.  That event gets the language line only (the
conditional pointer stays a per-tool-call line); English or unset
language -> no output.

Owner session only (routines/lib/owner_session.py, the guard the
workbench footer and the Stop text seat share); a subagent context (payload
carries agent_id), a dispatched worker, a cron session are all no-ops. Always exit
0; fail open on any error -- a missing pointer must never break a turn.
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
TEXT_DOC = "rules/cold.framework.text.md"
POINTER = ("If your next step is the reply to the user, read %s first; "
           "otherwise ignore." % TEXT_DOC)
# DGN-2188: the language line is NOT conditional. Narration written between
# tool calls reaches the user live, and the conditional pointer above does
# not cover it (the next step there is another tool call), so after a long
# English tool chain the narration drifted into English. One short line per
# tool call keeps the user's language in front of every user-visible word.
LANG_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese",
              "zh": "Chinese"}
LANG_LINE = ("Every word the user sees -- narration between tool calls and "
             "the final reply -- is in %s.")


def _bridge_locale():
    """The bridge's own reply language (DGN-2423): env LOCALE, else LOCALE in
    the instance bridge env file -- the value the system prompt's
    output-language rule is built from (bridge/config.py, ko/en only).
    Reads only the LOCALE line. None when LOCALE is set nowhere."""
    raw = os.environ.get("LOCALE")
    if raw is None:
        path = os.environ.get("DOGANY_BRIDGE_ENV") or os.path.join(
            os.path.dirname(SCRIPT_DIR), ".telegram_bot", ".env")
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("LOCALE="):
                        raw = line.split("=", 1)[1].split("#", 1)[0]
                        raw = raw.strip().strip('"').strip("'")
        except OSError:
            return None
    if raw is None:
        return None
    raw = raw.strip().lower()
    return raw if raw in ("ko", "en") else "en"


def _lang_line():
    """The language line for the owner's language, or "" when it is unset,
    unknown or English (English needs no reminder).  DGN-2423: the bridge's
    LOCALE wins, so this line always names the same language as the system
    prompt; AGENT_LANG (config/agent.conf) only when LOCALE is set nowhere."""
    try:
        code = _bridge_locale()
        if code is None:
            sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
            from conf_reader import conf_get
            path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
                os.path.dirname(SCRIPT_DIR), "config", "agent.conf")
            code = (conf_get("AGENT_LANG", path) or "").strip().lower()
            code = code.replace("_", "-").split("-")[0]
    except Exception:
        return ""
    name = LANG_NAMES.get(code)
    if not name or code == "en":
        return ""
    return LANG_LINE % name


def _is_owner(data):
    """The shared ownership guard (lib/owner_session.py), never a copy."""
    try:
        sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
        import owner_session
    except Exception:
        return False
    return owner_session.hook_status(
        data, os.path.dirname(SCRIPT_DIR)) == "owner"


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or not _is_owner(data):
            return 0
        if data.get("hook_event_name") == "UserPromptSubmit":
            line = _lang_line()
            if line:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": line,
                }}))
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(
                x for x in (_lang_line(), POINTER) if x),
        }}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
