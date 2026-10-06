#!/usr/bin/env python3
"""Single conf parser for KEY=value conf files (config/agent.conf and kin).

Root fix for DGN-916: agent.conf is sourced by bash (cron-guard,
generic-brief, secret-sweep, ...), so values containing spaces MUST be
quoted (DGN-899).  Bash strips those quotes automatically; python readers
must strip them too.  Before this module every python consumer parsed the
file with its own inline `split("=", 1)[1].strip()` variant -- some
stripped quotes, some did not, and the mismatch leaked quotes into
user-facing output (status-footer live label bug).  This module is the one
place that owns KEY=value lookup + normalization; python consumers call
conf_get() and never hand-roll the parse again.

Contract:
  - Lookup: first line that starts with "<key>=" wins (after whitespace
    strip); comment/blank lines never match because they cannot start
    with the key.
  - Normalization: value is whitespace-stripped, then surrounding double
    or single quotes are stripped (same normalization the bash `source`
    contract applies).
  - Fail-open: missing file, unreadable file, or missing key -> "".
    Never raises for filesystem trouble; hook callers must not crash.

Promoted from routines/output-gate-stop.py _read_agent_role /
status-footer.py _conf_get (the pre-existing quote-aware parsers).
Pure stdlib.  English/ASCII only.
"""

import os

__all__ = ["conf_get"]


def conf_get(key, conf_path=None):
    """Return the normalized value for `key` from a KEY=value conf file.

    Args:
        key: conf key name (e.g. "PLAN", "DASHBOARD_LIVE_LABEL").
        conf_path: absolute path to the conf file.  None -> resolve
            <cwd>/config/agent.conf (matches the historical default of
            the hook consumers).

    Returns:
        Normalized value string, or "" when the file or key is absent
        (fail-open -- callers chain their own defaults via `or`).
    """
    path = conf_path or os.path.join(os.getcwd(), "config", "agent.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""
