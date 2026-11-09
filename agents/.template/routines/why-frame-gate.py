#!/usr/bin/env python3
"""Non-blocking pre-answer guidance and compatibility hook entry point.

Why intake is an in-reply card, not an executor seat. Keep this executable so
older installed settings remain safe while their hook rows are removed. The
PreToolUse, dispatch, and ``--stop`` forms all deliberately exit successfully
without producing a hook decision: no tool or reply may be blocked, retried,
or rewritten by the Why net.
"""
import json
import os
import sys


GUIDANCE = (
    "WHY_NET pre-answer: For a new owner request that creates or changes future "
    "behavior, when no confirmed Why fits, call routines/why-card.py exactly once "
    "with 2-3 one-line Why candidates and what you are doing now. Continue the work "
    "in this turn. Silently link a fitting confirmed Why. Do not show a card for a "
    "bug fix, record, errand, question, confirmed continuation, disabling a feature, "
    "or a second time for the same work. Never delay tools, the reply, or Stop."
)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    enabled = os.environ.get("WHY_NET", "").lower() == "on"
    if not enabled:
        try:
            sys.path.insert(0, os.path.join(root, "routines", "lib"))
            from conf_reader import conf_get
            enabled = (conf_get("WHY_NET", os.path.join(root, "config", "agent.conf")) or "off").lower() == "on"
        except (ImportError, OSError, ValueError):
            enabled = False
    if "--stop" in sys.argv or not enabled:
        return None
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return None
    if data.get("hook_event_name") == "UserPromptSubmit":
        context = GUIDANCE
        try:
            if os.path.join(root, "routines", "lib") not in sys.path:
                sys.path.insert(0, os.path.join(root, "routines", "lib"))
            from why_net import owner_card
            consumed = owner_card.consume_hook(root, data.get("session_id"), data.get("prompt", ""))
            if consumed and consumed.get("prompt"):
                context = consumed["prompt"] + "\n\n" + context
            elif consumed and consumed.get("why"):
                context = "Confirmed Why stored verbatim; continue without another card.\n\n" + context
        except (ImportError, OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
              "additionalContext": context}}))


if __name__ == "__main__":
    main()
