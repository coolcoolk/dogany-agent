"""i18n resolver for bridge user-facing strings.

Truly-additive shim: messages.py binds each constant to t("<key>") at import
time, so call sites are unchanged and still do their own .format(...). t()
returns the RAW template (placeholders intact).

Locale is read from config.config.locale (env LOCALE, validated to ko/en, else
en). Resolution order: active locale catalog -> en fallback. A key missing from
BOTH catalogs is a programming error (typo) and raises KeyError so it surfaces
loudly instead of rendering blank.
"""

from bridge.config import config
from bridge.i18n import en, ko

# Registry of available catalogs. en is the canonical/fallback catalog.
STRINGS_FOR = {
    "en": en.STRINGS,
    "ko": ko.STRINGS,
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
