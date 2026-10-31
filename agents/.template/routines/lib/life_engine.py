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
    "conf_write_path", "resolve_bundle", "configured", "warn_missing",
    "command_names", "OFFER_KEY", "OFFER_FALLBACK_KEY", "OFFER_DEFAULT",
    "kit_text", "kit_offer", "INSTALLER_REL", "kit_installable",
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


def conf_write_path(agent_root):
    """Where a WRITER puts the kit conf (DGN-1676).

    The existing conf (resolve_conf, pin honored) when there is one, so a
    legacy instance keeps a single conf; otherwise config/<stem>.conf for the
    pinned stem, or the current name (KNOWN_STEMS[0]).  Never None.
    """
    p = resolve_conf(agent_root)
    if p:
        return p
    stem = pinned_stem(agent_root) or KNOWN_STEMS[0]
    return os.path.join(agent_root, "config", "%s.conf" % stem)


def resolve_bundle(agent_root):
    """Path of the existing service/<stem>/bundle.conf (pin honored), or None
    (DGN-1676: the bundle definition moved with the stem rename too)."""
    pin = pinned_stem(agent_root)
    stems = (pin,) if pin else KNOWN_STEMS
    try:
        for s in stems:
            p = os.path.join(agent_root, "service", s, "bundle.conf")
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


# DGN-1809: the first-install offer copy is the KIT's (i18n key
# life_setup.offer). For a kit that predates the key the framework carries a
# translated fallback (config/i18n/<lang>.json lifekit.offer_fallback, r2
# owner copy) and, last, this in-code English default (same text as the en
# key). Never fall back to the retired framework lifekit.offer copy.
OFFER_KEY = "life_setup.offer"
OFFER_FALLBACK_KEY = "lifekit.offer_fallback"
OFFER_DEFAULT = ("Lifekit is on. Tell me what you'd like to keep track of, "
                 "and we'll start there.")


def _langs(lang):
    base = (lang or "en").replace("_", "-").split("-")[0].lower() or "en"
    return tuple(dict.fromkeys((base, "en")))


def _json_str(path, key):
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            val = json.load(fh).get(key)
    except Exception:
        return None
    return val if isinstance(val, str) and val.strip() else None


def _kit_paths(agent_root, lg, bundle):
    paths = []
    if bundle:
        paths.append(os.path.join(os.path.dirname(bundle), "i18n",
                                  "%s.json" % lg))
    paths.append(os.path.join(agent_root, "config", "i18n",
                              "%s.bundle.json" % lg))
    return paths


def kit_text(agent_root, key, lang="en"):
    """A kit i18n string, or None.  Per language (`lang`, then en):
    <bundle-dir>/i18n/<lang>.json, then config/i18n/<lang>.bundle.json."""
    bundle = resolve_bundle(agent_root)
    for lg in _langs(lang):
        for p in _kit_paths(agent_root, lg, bundle):
            val = _json_str(p, key)
            if val:
                return val
    return None


def kit_offer(agent_root, lang="en"):
    """The first-install offer.  Per language (`lang`, then en): kit
    life_setup.offer (<bundle-dir>/i18n/<lang>.json, then
    config/i18n/<lang>.bundle.json), then the framework fallback
    config/i18n/<lang>.json lifekit.offer_fallback; last OFFER_DEFAULT."""
    bundle = resolve_bundle(agent_root)
    for lg in _langs(lang):
        for p in _kit_paths(agent_root, lg, bundle):
            val = _json_str(p, OFFER_KEY)
            if val:
                return val
        val = _json_str(os.path.join(agent_root, "config", "i18n",
                                     "%s.json" % lg), OFFER_FALLBACK_KEY)
        if val:
            return val
    return OFFER_DEFAULT


# DGN-1817: the life kit is offered only when it can actually be installed.
# Already installed (bundle= resolves) -> yes. Otherwise ask the framework's
# kit_first_install.sh probe (catalog row -> package_dir -> git work tree ->
# pack/kit_life/v<version> tag, the pack_install.sh source-tag predicate).
# Framework = DOGANY_REPO_ROOT in .instance.conf. Any doubt (no framework
# pointer, no script, timeout, error) -> False: fail-closed, the same rule
# the business kit follows until its bundle ships.
INSTALLER_REL = os.path.join("scripts", "pack", "kit_first_install.sh")


def kit_installable(agent_root, timeout=20):
    """True iff the life kit is installed or installable from its source."""
    try:
        if resolve_bundle(agent_root):
            return True
        repo = _conf_get("DOGANY_REPO_ROOT",
                         os.path.join(agent_root, ".instance.conf"))
        script = os.path.join(repo, INSTALLER_REL) if repo else ""
        if not script or not os.path.isfile(script):
            return False
        import subprocess
        r = subprocess.run(["bash", script, "probe", agent_root],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def _main(argv):
    """CLI (DGN-1676): `python3 routines/lib/life_engine.py <agent_root>`.

    Prints KEY=value lines for skills and shell callers:
      conf=<conf_write_path>   read AND write here (may not exist yet)
      conf_exists=1|0
      bundle=<resolve_bundle, empty when not found>
    `life_engine.py <agent_root> --offer [lang]` (DGN-1809) prints the
    first-install offer text instead (kit_offer; lang default AGENT_LANG).
    Always rc 0 with a root argument; rc 2 on usage error.
    """
    if len(argv) in (3, 4) and argv[2] == "--offer":
        root = os.path.abspath(argv[1])
        lang = argv[3] if len(argv) == 4 else (
            _conf_get("AGENT_LANG", os.path.join(root, "config", "agent.conf"))
            or "en")
        sys.stdout.write(kit_offer(root, lang) + "\n")
        return 0
    if len(argv) != 2:
        sys.stderr.write("usage: life_engine.py <agent_root> [--offer [lang]]\n")
        return 2
    root = os.path.abspath(argv[1])
    conf = conf_write_path(root)
    sys.stdout.write("conf=%s\nconf_exists=%d\nbundle=%s\n"
                     % (conf, 1 if os.path.isfile(conf) else 0,
                        resolve_bundle(root) or ""))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
