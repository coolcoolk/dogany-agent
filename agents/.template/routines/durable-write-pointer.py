#!/usr/bin/env python3
"""PreToolUse durable-write pointer (v2.7 D6).

Where a durable fact or a work item belongs (identity file, SKILL.md,
files/ subdir, the engine's memories/, the ticket surface) lives in one cold
section, playbooks/layout.md -- "Routing: where a durable fact or work item
goes". It is not hot-loaded: a Write/Edit about to land under one of those
homes is the only moment the rule can be acted on, so this hook injects ONE
conditional line there and the model reads the section only when the write
really is a durable fact or work item. Writes anywhere else are a no-op.

Same shape as the D1 text-layer pointer (routines/text-layer-pointer.py):
owner session only via routines/lib/owner_session.py (a subagent context, a
dispatched worker, a cron session are all no-ops); never blocks, never
denies; always exit 0 and fail open on any error -- a missing pointer must
never break a write.
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
ROUTING_DOC = "playbooks/layout.md -- Routing"
POINTER = ("If you are about to store a durable fact or work item, read %s "
           "first; otherwise ignore." % ROUTING_DOC)
# Top-level homes whose writes the routing rule governs. worklog/ is the
# ticket surface; .claude/skills/ is where an instance keeps its SKILL.md.
HOMES = ("identity/", "skills/", ".claude/skills/", "files/", "memories/",
         "worklog/")
TOOLS = ("Write", "Edit", "MultiEdit")


def _is_owner(data):
    """The shared ownership guard (lib/owner_session.py), never a copy."""
    try:
        sys.path.insert(0, os.path.join(SCRIPT_DIR, "lib"))
        import owner_session
    except Exception:
        return False
    return owner_session.hook_status(data, ROOT) == "owner"


def _rel(path, cwd):
    """Path relative to the instance root, or None when it lies outside."""
    if not os.path.isabs(path):
        path = os.path.join(cwd or ROOT, path)
    root = os.path.realpath(ROOT)
    path = os.path.realpath(path)
    if path != root and not path.startswith(root + os.sep):
        return None
    return os.path.relpath(path, root).replace(os.sep, "/")


def is_durable_home(path, cwd=None):
    rel = _rel(path, cwd) if path else None
    return bool(rel) and rel.startswith(HOMES)


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or data.get("tool_name") not in TOOLS:
            return 0
        tin = data.get("tool_input") or {}
        path = tin.get("file_path") if isinstance(tin, dict) else None
        if not isinstance(path, str) or not is_durable_home(
                path.strip(), data.get("cwd")):
            return 0
        if not _is_owner(data):
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": POINTER,
        }}))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
