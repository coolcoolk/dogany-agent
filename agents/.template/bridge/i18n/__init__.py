"""i18n resolver for bridge user-facing strings.

Truly-additive shim: messages.py binds each constant to t("<key>") at import
time, so call sites are unchanged and still do their own .format(...). t()
returns the RAW template (placeholders intact).

Locale is read from config.config.locale (env LOCALE, validated to ko/en, else
en). Resolution order: active locale catalog -> en fallback.

Import-safety invariant (DGN-712): t() is called at MODULE IMPORT time by
messages.py, so a key missing from BOTH catalogs must NOT hard-crash the import
-- otherwise an instance whose i18n drifted (a new code-referenced key never
landed) fails to import bridge.bot, never writes its poll heartbeat, and the
watchdog restart-loops until it is rate-limited (live DOWN). A missing key is
still a bug, so it is logged loudly, but t() degrades to a safe visible
fallback (the key name itself) instead of raising. Callers that need
raise-on-missing semantics use t_strict().

skill_display_name(skill_id) -> localized label for user-facing skill listings
(DGN-102). Fail-open: unmapped IDs return the raw ID, never KeyError.
"""

import logging

from bridge.config import config
from bridge.i18n import en, ko

logger = logging.getLogger(__name__)

# Registry of available catalogs. en is the canonical/fallback catalog.
STRINGS_FOR = {
    "en": en.STRINGS,
    "ko": ko.STRINGS,
}

# Registry of skill display-name catalogs (DGN-102).
DISPLAY_NAMES_FOR = {
    "en": en.SKILL_DISPLAY_NAMES,
    "ko": ko.SKILL_DISPLAY_NAMES,
}


def _lookup(key: str):
    """Resolve `key`: active locale first, then the en fallback. None if absent."""
    active = STRINGS_FOR.get(config.locale, en.STRINGS)
    value = active.get(key)
    if value is not None:
        return value
    return en.STRINGS.get(key)


def t(key: str) -> str:
    """Return the raw template for `key` in the active locale, English fallback.

    Import-safe (DGN-712): a key missing from BOTH catalogs is logged as an
    error and the raw key name is returned as a visible fallback -- it never
    raises, so module-level bindings in messages.py cannot brick bridge import
    on an instance with a stale/partial i18n. Use t_strict() where a hard
    failure on a missing key is genuinely wanted (e.g. release lint).
    """
    value = _lookup(key)
    if value is not None:
        return value
    logger.error(
        "i18n key %r is not defined in locale %r or en; "
        "rendering the raw key as fallback (stale/partial i18n?)",
        key,
        config.locale,
    )
    return key


def t_strict(key: str) -> str:
    """Like t() but raises KeyError on a missing key.

    For non-import-time paths that want a loud failure (tests, release lint).
    """
    value = _lookup(key)
    if value is not None:
        return value
    raise KeyError(
        f"i18n key {key!r} is not defined in locale {config.locale!r} or en"
    )


def skill_display_name(skill_id: str) -> str:
    """Return a localized display label for the given skill folder ID.

    Resolution order: active locale catalog -> en catalog -> raw skill_id.
    Never raises; an unmapped ID falls back to the raw ID itself (fail-open).

    Usage::

        from bridge.i18n import skill_display_name
        label = skill_display_name("diet-log")  # -> "식단 기록" (ko) or "Diet Log" (en)
    """
    active = DISPLAY_NAMES_FOR.get(config.locale, en.SKILL_DISPLAY_NAMES)
    value = active.get(skill_id)
    if value is not None:
        return value
    # en fallback
    en_value = en.SKILL_DISPLAY_NAMES.get(skill_id)
    if en_value is not None:
        return en_value
    # Final fallback: return the raw ID so the caller always gets a string.
    return skill_id
