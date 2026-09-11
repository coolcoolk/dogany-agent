#!/usr/bin/env python3
"""Resolve the life-data engine's file stem (DGN-1238).

The engine has shipped under two stems: ``lifekit`` (original) and
``kit_life`` (pack rename, 2026-08-28).  Live fleets exist on BOTH
coordinates at once, so framework code must never hardcode one stem again:
it resolves the stem per instance, and any future rename is absorbed by
extending KNOWN_STEMS (or pinning LIFE_ENGINE=) instead of editing N call
sites.  Shell twin: routines/lib/life-engine.sh -- keep the two resolution
contracts identical.

Resolution order (stem_in_dir / resolve_conf):
  1. explicit pin: config/agent.conf LIFE_ENGINE=<stem>.  Only the pinned
     stem is considered; a pin whose files are missing resolves to None
     (the configured-but-missing case -- see warn_missing).
  2. probe every known stem against the files the caller needs.
  3. nothing found -> None.  The caller MUST keep its pre-existing
     "kit absent" path, so a kitless mint behaves exactly as before.

Fail-open: nothing here raises for filesystem trouble; hook callers must
never crash or block a turn because of this module.  Pure stdlib.
"""

import os
import sys

try:
    from conf_reader import conf_get as _conf_get
except Exception:  # keep the resolver importable even without the parser
    def _conf_get(key, conf_path=None):
        try:
            with open(conf_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
        return ""

__all__ = [
    "KNOWN_STEMS", "pinned_stem", "stem_in_dir", "resolve_conf",
    "configured", "warn_missing", "command_names",
]

KNOWN_STEMS = ("kit_life", "lifekit")

_ANY_SURFACE_EXTS = ("py", "db", "sh")


def pinned_stem(agent_root):
    """LIFE_ENGINE= pin from config/agent.conf, or None."""
    try:
        val = _conf_get("LIFE_ENGINE",
                        os.path.join(agent_root, "config", "agent.conf"))
        return val or None
    except Exception:
        return None


def stem_in_dir(dbdir, exts=("py", "db"), pin=None):
    """First stem for which every ``<dbdir>/<stem>.<ext>`` exists, else None.

    ``pin`` restricts the probe to that single stem.  Existence check only;
    callers keep their own executability/content gates.  A mixed directory
    (e.g. kit_life.py next to a lone lifekit.db) resolves to None exactly
    like the pre-DGN-1238 single-name probe did -- fail-safe, no guessing.
    """
    stems = (pin,) if pin else KNOWN_STEMS
    try:
        for s in stems:
            if all(os.path.isfile(os.path.join(dbdir, "%s.%s" % (s, e)))
                   for e in exts):
                return s
    except Exception:
        pass
    return None


def resolve_conf(agent_root):
    """Path of the existing config/<stem>.conf (pin honored), or None."""
    pin = pinned_stem(agent_root)
    stems = (pin,) if pin else KNOWN_STEMS
    try:
        for s in stems:
            p = os.path.join(agent_root, "config", "%s.conf" % s)
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    return None


def configured(agent_root):
    """True iff the instance SAYS it has the life engine.

    Signals: LIFE_ENGINE= pinned, KIT= names a life kit, or a known kit conf
    carries LIFEKIT=on.  This is what separates "kit absent (normal no-op)"
    from "kit configured but engine files not found" -- the silent-failure
    class DGN-1238 exists to close.
    """
    try:
        agent_conf = os.path.join(agent_root, "config", "agent.conf")
        if _conf_get("LIFE_ENGINE", agent_conf):
            return True
        if (_conf_get("KIT", agent_conf) or "").lower() in KNOWN_STEMS:
            return True
        for s in KNOWN_STEMS:
            conf = os.path.join(agent_root, "config", "%s.conf" % s)
            if _conf_get("LIFEKIT", conf) == "on":
                return True
    except Exception:
        pass
    return False


def warn_missing(agent_root, caller):
    """Configured-but-not-found warning: never raises, never blocks.

    Throttled to once per day (stamp file): one stderr line + one line
    appended to .telegram_bot/life-engine.warn.log.
    """
    try:
        import datetime
        d = os.path.join(agent_root, ".telegram_bot")
        stamp = os.path.join(d, "life-engine.warn.stamp")
        today = datetime.date.today().isoformat()
        try:
            with open(stamp, encoding="utf-8") as fh:
                if fh.read().strip() == today:
                    return
        except Exception:
            pass
        sys.stderr.write(
            "[life-engine] configured but engine files not found under known "
            "stems (%s) in %s -- caller=%s (DGN-1238)\n"
            % ("/".join(KNOWN_STEMS), agent_root, caller))
        os.makedirs(d, exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:
            fh.write(today)
        with open(os.path.join(d, "life-engine.warn.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s %s: life engine configured but files not found "
                     "(stems: %s)\n" % (today, caller, "/".join(KNOWN_STEMS)))
    except Exception:
        pass


def command_names(exts=("sh", "py")):
    """CLI filenames for every known stem (e.g. for command-string matching):
    ('kit_life.sh', 'kit_life.py', 'lifekit.sh', 'lifekit.py')."""
    return tuple("%s.%s" % (s, e) for s in KNOWN_STEMS for e in exts)
