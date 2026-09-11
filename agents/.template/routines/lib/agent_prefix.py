#!/usr/bin/env python3
"""agent_prefix.py -- THE notify-prefix resolver (DGN-828 read-side fix).

Every notify reader resolves the per-agent signature prefix through THIS one
implementation -- bridge/self_restart.sh, routines/cron-guard.sh,
routines/terminal-state-ledger.py, plus the substitution writers update.sh /
scripts/mint.sh / scripts/pack/pack_install.sh. Never re-encode the order
anywhere else (shell callers invoke the CLI below).

Resolution order (owner decision 2026-09-08):
  1. .instance.conf DOGANY_AGENT_PREFIX -- non-empty AND not a placeholder
     sentinel. Existing installs with a real value are unchanged.
  2. identity/hot.custom.agent.md "- Emoji:" line (legacy pre-DGN-1141
     layout: AGENT.md). The persona file filled at onboarding is the truth;
     nothing has to be COPIED anywhere for the emoji to take effect -- that
     copy step (prose, dogany-user-onboarding SKILL.md) is exactly what
     failed on live installs and put "[agent]" on a user's screen.
  3. "" (empty string) -- the sanctioned degraded output: the message renders
     with NO prefix. A placeholder ([agent], __AGENT_PREFIX__, unfilled
     "(set at onboarding)" template text) must NEVER reach a message body.

CLI:  python3 agent_prefix.py <instance-root>
      -> resolved prefix on stdout (empty output when unset; always exit 0,
         so shell callers can `$(... || true)` and get the safe empty value).
Lib:  resolve(root) -> str
      padded(root)  -> str  ("<prefix> " with ONE trailing space, or "" --
                             prepend verbatim, no spacing artifacts)
"""
import os
import re
import sys

# Placeholder sentinels that must never be emitted: the historical mint/update
# fallback literal, any unsubstituted __TOKEN__ mint placeholder, and the
# unfilled onboarding template value (matched by _clean below).
_SENTINEL_RE = re.compile(r"^(\[agent\]|__[A-Za-z0-9_]+__)$", re.IGNORECASE)


def _clean(value):
    """Trim a candidate value; return '' when it is any placeholder shape."""
    v = (value or "").strip()
    # Defensive markdown-bold strip: the persona template renders some fields
    # as **value** -- an emoji answer never legitimately contains '*'.
    v = v.strip("*").strip()
    if not v or _SENTINEL_RE.match(v) or "set at onboarding" in v:
        return ""
    return v


def _from_instance_conf(root):
    path = os.path.join(root, ".instance.conf")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("DOGANY_AGENT_PREFIX="):
                    return _clean(line.split("=", 1)[1])
    except OSError:
        pass
    return ""


def _from_persona(root):
    # Newest layout first; the FIRST persona file that exists decides (never
    # mix layouts -- a stale pre-migration AGENT.md must not override).
    for rel in (os.path.join("identity", "hot.custom.agent.md"), "AGENT.md"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^\s*-\s*Emoji:\s*(.*)$", line)
                    if m:
                        return _clean(m.group(1))
        except OSError:
            pass
        return ""
    return ""


def resolve(root):
    return _from_instance_conf(root) or _from_persona(root)


def padded(root):
    p = resolve(root)
    return p + " " if p else ""


if __name__ == "__main__":
    print(resolve(sys.argv[1] if len(sys.argv) > 1 else "."))
