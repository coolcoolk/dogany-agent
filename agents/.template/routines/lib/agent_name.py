#!/usr/bin/env python3
"""The agent's owner-facing name, one resolver for every surface (DGN-1744).

The name an owner gives the agent at onboarding lives in ONE place: the
"- Name:" line of the identity file (identity/hot.custom.agent.md, newest
layout first; PROFILE.md, AGENT.md for older layouts), markdown bold
stripped.  .instance.conf DOGANY_AGENT_LABEL / DOGANY_AGENT_NAME are written
once by mint.sh at birth -- before the agent has a name -- and nothing keeps
them in step with a later naming or rename.  So they are only the fallback
for an agent that has no name yet:

    identity "- Name:" -> DOGANY_AGENT_LABEL -> DOGANY_AGENT_NAME -> fallback
    -> the instance folder name

An unset template value ("(set at onboarding)", a double-underscore token,
{{x}}) is no name.  Same identity read as statusline.py agent_name() and scripts/dogany
instance_name().

Repo-side callers load this file from agents/.template/routines/lib/; an
instance carries it as routines/lib/agent_name.py.

CLI: agent_name.py <instance-root> [--no-label] [--fallback TEXT]
prints the name (never fails; exit 0).
"""
import os
import re
import sys

IDENTITY_FILES = ("identity/hot.custom.agent.md", "PROFILE.md", "AGENT.md")
_NAME_LINE = re.compile(r"^- Name:[ \t]*(.*?)[ \t]*$")


def _unset(value):
    return (not value) or value.startswith("(") or "__" in value or "{{" in value


def _conf_value(root, key):
    try:
        with open(os.path.join(root, ".instance.conf"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip().strip("\"'")
    except OSError:
        pass
    return ""


def identity_name(root):
    """The onboarding name, "" while the agent has none."""
    for rel in IDENTITY_FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        name = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _NAME_LINE.match(line.rstrip("\n"))
                    if m:
                        name = m.group(1).replace("**", "").strip()
                        break
        except OSError:
            pass
        return "" if _unset(name) else name
    return ""


def display_name(root, fallback="", label=True):
    """Owner-facing name of the instance at `root`.  label=False skips the
    mint-time DOGANY_AGENT_LABEL (a surface whose language is not the
    label's), never the identity name."""
    root = root or ""
    name = identity_name(root) if root else ""
    if name:
        return name
    for key in (("DOGANY_AGENT_LABEL", "DOGANY_AGENT_NAME") if label else ("DOGANY_AGENT_NAME",)):
        value = _conf_value(root, key) if root else ""
        if not _unset(value):
            return value
    if fallback or not root:
        return fallback
    return os.path.basename(os.path.normpath(root))


def main(argv):
    args = list(argv)
    label = True
    fallback = ""
    if "--no-label" in args:
        args.remove("--no-label")
        label = False
    if "--fallback" in args:
        at = args.index("--fallback")
        fallback = args[at + 1] if at + 1 < len(args) else ""
        del args[at:at + 2]
    root = args[0] if args else ""
    try:
        print(display_name(root, fallback, label))
    except Exception:
        print(fallback or (os.path.basename(os.path.normpath(root)) if root else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
