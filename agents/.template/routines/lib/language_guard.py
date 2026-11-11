"""Script-share scoring for user-visible assistant prose (FDGN-686)."""

import re


SCRIPT_SHARE_THRESHOLD = 0.30
MIN_SCORED_UNITS = 4

_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_NAMED_CONTAINER_RE = re.compile(
    r"(?ims)^\s*:::\s*(?:preview|quote)\s*$.*?^\s*:::\s*$"
)
_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"(?:https?://|tg://)\S+")
_PATH_RE = re.compile(
    r"(?<!\w)(?:[A-Za-z]:)?(?:[~/]|\.{1,2}/)[^\s,;:!?]+"
    r"|(?<!\w)[\w.-]+(?:/[\w.@+~-]+)+(?:\.[A-Za-z0-9]+)?"
)
_MARKER_RE = re.compile(
    r"(?ms)^\s*(?:\[\[OPTIONS[^\n]*\]\]|send_file::[^\n]*|NO_PUSH)\s*$"
)
_PRODUCT_RE = re.compile(
    r"\b(?:Claude\s+Code|Telegram|GitHub|Claude|Codex|OpenAI)\b",
    re.IGNORECASE,
)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_IDENTIFIER_RE = re.compile(
    r"(?<!\w)(?:[A-Z][A-Z0-9]*-\d+|[A-Za-z0-9]+(?:[_./-][A-Za-z0-9]+)+)(?!\w)"
)
_SCRIPT_RE = {"ko": re.compile(r"[\uac00-\ud7a3]")}


def normalized_language(value: str) -> str:
    """Return a base language code suitable for script enforcement."""
    return (value or "").strip().lower().replace("_", "-").split("-", 1)[0]


def prose_for_script_measure(text: str) -> str:
    """Remove machine material and allowed product names from scored prose."""
    prose = _NAMED_CONTAINER_RE.sub(" ", text or "")
    prose = _BLOCKQUOTE_LINE_RE.sub(" ", prose)
    prose = _FENCED_CODE_RE.sub(" ", prose)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    prose = _URL_RE.sub(" ", prose)
    prose = _PATH_RE.sub(" ", prose)
    prose = _IDENTIFIER_RE.sub(" ", prose)
    prose = _MARKER_RE.sub(" ", prose)
    return _PRODUCT_RE.sub(" ", prose)


def script_share(text: str, language: str):
    """Return (share, script letters, all scored letters), or None if unsupported."""
    script_re = _SCRIPT_RE.get(normalized_language(language))
    if script_re is None:
        return None
    prose = prose_for_script_measure(text)
    latin_words = len(_LATIN_WORD_RE.findall(prose))
    script_letters = len(script_re.findall(prose))
    scored_units = script_letters + latin_words
    return ((script_letters / scored_units) if scored_units else 1.0,
            script_letters, scored_units)


def is_off_language(text: str, language: str,
                    threshold: float = SCRIPT_SHARE_THRESHOLD) -> bool:
    """True only when measurable prose is below the requested script share."""
    measured = script_share(text, language)
    return bool(measured and measured[2] >= MIN_SCORED_UNITS
                and measured[0] < threshold)
