#!/usr/bin/env python3
"""Claude Code statusLine for a Dogany instance (DGN-1716).

Prints "dogany <agent display name>" under the Claude Code input box, so a
local `dogany` session shows WHICH agent it is talking to. Wired from
.claude/settings.json "statusLine" (type command).

Display name = the "- Name:" line of the instance's identity file, newest
layout first (identity/hot.custom.agent.md, PROFILE.md, AGENT.md), markdown
bold stripped. An unset template value ("(set at onboarding)", a
double-underscore token, {{x}}) or a missing line falls back to DOGANY_AGENT_NAME in
.instance.conf, then the instance folder name -- the previous output.
Mirror of scripts/dogany instance_name(); keep the two in step.

The instance root is this file's grandparent (routines/..), not the session
cwd, so the label stays right after a `cd` inside the session. Never fails:
any error prints the folder-name fallback and exits 0.
"""
import os
import re
import sys

IDENTITY_FILES = ("identity/hot.custom.agent.md", "PROFILE.md", "AGENT.md")
NAME_LINE = re.compile(r"^- Name:[ \t]*(.*?)[ \t]*$")


def _unset(value):
    return (not value) or value.startswith("(") or "__" in value or "{{" in value


def _conf_value(root, key):
    try:
        with open(os.path.join(root, ".instance.conf"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip()
    except OSError:
        pass
    return ""


def agent_name(root):
    for rel in IDENTITY_FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        name = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = NAME_LINE.match(line.rstrip("\n"))
                    if m:
                        name = m.group(1).replace("**", "").strip()
                        break
        except OSError:
            pass
        if not _unset(name):
            return name
        break
    return _conf_value(root, "DOGANY_AGENT_NAME") or os.path.basename(root.rstrip("/"))


def main():
    try:
        sys.stdin.read()  # the session JSON; not needed, drain it
    except Exception:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        name = agent_name(root)
    except Exception:
        name = os.path.basename(root.rstrip("/"))
    print("dogany " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
