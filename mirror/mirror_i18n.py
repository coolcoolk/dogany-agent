"""DGN-268 S4: locale lookup for mirror user-facing strings.

The mirror lives at <instance>/mirror/; locale files live at
<instance>/config/i18n/<lang>.json and the selected language in
<instance>/config/agent.conf (AGENT_LANG). This helper resolves a key to its
localized string, mirroring the shell `i18n()` contract in
routines/lib/agentlib.sh: try AGENT_LANG, then 'en', then a caller-supplied
fallback (the current ko literal -- so an instance with NO locale file for the
key behaves byte-identically to before i18n, i.e. zero-delta for Ag).

English/ASCII only in code; the localized VALUES live in the JSON files.
No external deps; the parse is cached.
"""

import json
import os

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_I18N_DIR = os.path.normpath(os.path.join(_MODULE_DIR, "..", "config", "i18n"))
_AGENT_CONF = os.path.normpath(
    os.path.join(_MODULE_DIR, "..", "config", "agent.conf"))

_LANG_CACHE = None       # resolved AGENT_LANG
_BUNDLE_CACHE = {}       # lang -> dict (parsed json), {} on miss


def _agent_lang():
    """AGENT_LANG from config/agent.conf; default 'en'. Any error -> 'en'."""
    global _LANG_CACHE
    if _LANG_CACHE is None:
        lang = "en"
        try:
            with open(_AGENT_CONF, "r") as fh:
                for raw in fh:
                    line = raw.strip()
                    if line.startswith("AGENT_LANG="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            lang = val
                        break
        except (OSError, IOError):
            pass
        _LANG_CACHE = lang
    return _LANG_CACHE


def _bundle(lang):
    """Parsed locale bundle for `lang`, cached. Missing/invalid file -> {}."""
    if lang not in _BUNDLE_CACHE:
        data = {}
        try:
            with open(os.path.join(_I18N_DIR, "%s.json" % lang), "r") as fh:
                data = json.load(fh)
        except (OSError, IOError, ValueError):
            data = {}
        _BUNDLE_CACHE[lang] = data if isinstance(data, dict) else {}
    return _BUNDLE_CACHE[lang]


def _reset_cache():
    """Test seam: drop cached lang + bundles so new config is re-read."""
    global _LANG_CACHE
    _LANG_CACHE = None
    _BUNDLE_CACHE.clear()


def t(key, fallback, **fmt):
    """Localized string for `key`: AGENT_LANG bundle, then en, then `fallback`
    (the current ko literal -- guarantees zero-delta when no locale file has
    the key). `fmt` fields are applied with str.format when present."""
    val = None
    for lang in (_agent_lang(), "en"):
        b = _bundle(lang)
        if key in b:
            val = b[key]
            break
    if val is None:
        val = fallback
    if fmt:
        try:
            return val.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return val
    return val
