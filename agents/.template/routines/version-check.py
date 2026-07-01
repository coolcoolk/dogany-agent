#!/usr/bin/env python3
"""version-check.py -- SessionStart hook: notify when a newer framework exists.

Compares the framework version this instance was built from
(.instance.conf -> DOGANY_FW_VERSION) against the source repo's current
VERSION file (located via .instance.conf -> DOGANY_REPO_ROOT). If they differ,
it injects an additionalContext note telling the agent to inform the user that
a new Dogany version is available and to offer running ./update.sh. The agent
ASKS the user; it never auto-updates.

Design: strictly fail-open. Any missing file, parse error, or unexpected
condition results in exit 0 with no output, so a session is NEVER blocked.
Output protocol matches other SessionStart hooks: a JSON object on stdout with
hookSpecificOutput.additionalContext (wrapped as a system reminder, not shown
in the chat transcript).
"""
import json
import os
import sys


def _read_conf(path):
    """Parse a simple KEY=VALUE conf file into a dict (ignores comments)."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
    except Exception:
        return {}
    return out


def _read_version(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readline().strip()
    except Exception:
        return ""


def main():
    # SessionStart delivers a JSON payload on stdin; we do not need its fields,
    # but consume it so the pipe closes cleanly.
    try:
        sys.stdin.read()
    except Exception:
        pass

    # The instance root is two levels up from this hook (routines/ -> root).
    here = os.path.dirname(os.path.abspath(__file__))
    instance_root = os.path.dirname(here)

    conf = _read_conf(os.path.join(instance_root, ".instance.conf"))
    built_version = conf.get("DOGANY_FW_VERSION", "")
    repo_root = conf.get("DOGANY_REPO_ROOT", "")

    # No manifest / no repo pointer -> nothing to compare. Stay silent.
    if not built_version or not repo_root:
        sys.exit(0)

    repo_version = _read_version(os.path.join(repo_root, "VERSION"))
    if not repo_version:
        sys.exit(0)

    # Versions match (or repo unknown) -> no notice.
    if repo_version == built_version or repo_version == "unknown":
        sys.exit(0)

    note = (
        "[Dogany framework update available] This instance was built from "
        "framework version {built}, but the source repo now has version {repo}. "
        "Tell the user, in their language, that a new Dogany version is "
        "available and offer to run ./update.sh (from the repo at {repo_root}) "
        "to update. Do NOT auto-update -- ask the user first. update.sh only "
        "refreshes framework code and preserves memories, .env, databases, and "
        "user-authored skills."
    ).format(built=built_version, repo=repo_version, repo_root=repo_root)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
