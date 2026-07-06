"""i18n resolver for bridge user-facing strings.

Truly-additive shim: messages.py binds each constant to t("<key>") at import
time, so call sites are unchanged and still do their own .format(...). t()
returns the RAW template (placeholders intact).

Locale is read from config.config.locale (env LOCALE, validated to ko/en, else
en). Resolution order: active locale catalog -> en fallback. A key missing from
BOTH catalogs is a programming error (typo) and raises KeyError so it surfaces
loudly instead of rendering blank.

skill_display_name(skill_id) -> localized label for user-facing skill listings
(DGN-102). Fail-open: unmapped IDs return the raw ID, never KeyError.
"""

from bridge.config import config
from bridge.i18n import en, ko

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


def t(key: str) -> str:
    """Return the raw template for `key` in the active locale, English fallback.

    Missing in the active locale -> fall back to en. Missing in en as well ->
    KeyError (a typo'd key must fail loudly, not render as an empty string).
    """
    active = STRINGS_FOR.get(config.locale, en.STRINGS)
    value = active.get(key)
    if value is not None:
        return value
    if key in en.STRINGS:
        return en.STRINGS[key]
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
