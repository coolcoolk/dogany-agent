#!/usr/bin/env python3
"""secret_scan.py -- one secret-shape floor shared by the mint and the
skill-creator (DGN-1910 F2/F7, locked spec 3.5).

API (frozen):
    scan(text, mode) -> [Hit(cat, start, end)]   mode = 'strict' | 'body'
    redact(text)     -> str                      body-mode hits replaced

CLI (frozen):
    secret_scan.py --mode strict|body FILE...
    prints '<cat> <file>:<line>' per hit, NEVER the matched text.
    exit 0 clean / 1 hits / 2 usage (bad args, unreadable file).

Categories, both modes:
    key_prefix      sk- / sk-ant- / ghp_ / gho_ / github_pat_ / xox[abposr]-
                    with a token body, AKIA + 16, AIza + 35, '-----BEGIN'
    telegram_token  the K1 bot-token shape (superset of mint_flow.mask)
    high_entropy    >=32 chars of [A-Za-z0-9+/_=-], Shannon entropy >= 4.0,
                    with a digit + an upper + a lower letter and a >=16-char
                    run between / _ - (see _entropy_hit)
    kr_rrn          KR resident id \\d{6}-?[1-8]\\d{6} (digit-bounded)
    card            Luhn-valid 13-19 digit number (space/hyphen separators)
    webhook         hooks.slack.com/... , discord(app).com/api/webhooks/...
    keyword_value   a credential keyword WITH a value after it (the value
                    must look like one: ':'/'=', a digit, or a particle +
                    non-Hangul value; see _kw_value_hit)
strict only (spec fields at validate):
    keyword         the bare credential keyword alone

NOT caught, by design (stated in spec 10, kept out of every claim): names
and institutions (professor, school, company) and free-form account details
without a keyword.

Binary files (a NUL byte) are skipped by the CLI, like `grep -I`.
"""
import collections
import math
import re
import sys
import unicodedata

Hit = collections.namedtuple("Hit", "cat start end")

MODES = ("strict", "body")

_TOK = r"[A-Za-z0-9_-]"
_KEY_RES = [
    # sk-ant- is a sub-shape of sk-; one pattern covers both.
    re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-)?" + _TOK + r"{10,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:ghp|gho)_[A-Za-z0-9]{10,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_" + _TOK + r"{10,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[abposr]-" + _TOK + r"{8,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])"),
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"-----BEGIN"),
]
# K1 shape (mint_flow._TOKEN_RE), digit-bounded on the left so a longer
# number does not split.
_TELEGRAM_RE = re.compile(r"(?<!\d)\d{3,}:[A-Za-z0-9_-]{20,}")
_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_RRN_RE = re.compile(r"(?<!\d)\d{6}-?[1-8]\d{6}(?!\d)")
_CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d])")
_WEBHOOK_RE = re.compile(
    r"hooks\.slack\.com/\S*|discord(?:app)?\.com/api/webhooks/\S*", re.I)

# Keywords: latin ones are word-bounded (else 'pin' fires inside
# 'spinning'); Korean ones are not (a particle follows them directly).
_KW_LATIN = r"(?<![A-Za-z0-9_-])(?:password|passwd|pw|pin|otp)(?![A-Za-z0-9_])"
_KW_KO = "(?:\ube44\ubc88|\ube44\ubc00\ubc88\ud638|\uacc4\uc88c|\uce74\ub4dc\ubc88\ud638)"
_KW = "(?:" + _KW_LATIN + "|" + _KW_KO + ")"
# spec: keyword \s* [:= particle]? \s* \S{3,}  (particles: eun/neun/i/ga).
# Groups: 1 keyword, 2 separator, 3 value. _kw_value_hit() then demands a
# value that looks like one (see there).
_SEP = "[:=\uc740\ub294\uc774\uac00]"
_KW_VALUE_RE = re.compile("(" + _KW + r")\s*(" + _SEP + r"?)\s*(\S{3,})", re.I)
_KW_BARE_RE = re.compile(_KW, re.I)
_HANGUL_RE = re.compile("[\uac00-\ud7a3]")

_ENTROPY_MIN = 4.0
_PLACEHOLDERS = ("<redacted>", "<token>")


def _entropy_hit(tok):
    """Spec floor (>=32 chars, entropy >= 4.0) plus a mixed-class demand: a
    digit, an upper and a lower letter. Without it every long path in the
    shipped canon ('__PROJECT_ROOT__/bridge/self_restart') reads as a key
    (C16 census). Generated keys carry all three classes, and one unbroken
    run of >= 16 chars between / _ - separators."""
    if not (_entropy(tok) >= _ENTROPY_MIN and any(c.isdigit() for c in tok)
            and any(c.isupper() for c in tok) and any(c.islower() for c in tok)):
        return False
    # A path or slug ('DGN-1505-accept-path/verification/selftest') is short
    # words joined by / _ -; a key has at least one long unbroken run.
    return max(len(seg) for seg in re.split(r"[/_-]", tok)) >= 16


def _kw_value_hit(m):
    """The spec shape alone fires on prose ('does NOT pin exact scopes',
    '--password-stdin'), so the value must look like a value: an explicit
    ':' / '=', or a digit in it, or a Korean particle followed by a value
    with no Hangul ('<kw>-eun hunter2', not '<kw>-ga piryohamyeon')."""
    sep, val = m.group(2), m.group(3)
    if val.startswith(_PLACEHOLDERS):
        return False  # already redacted: redact() output scans clean
    if sep in (":", "="):
        return True
    if any(c.isdigit() for c in val):
        return True
    return bool(sep) and not _HANGUL_RE.search(val)


def _entropy(s):
    n = len(s)
    counts = collections.Counter(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan(text, mode):
    """Every secret-shaped span in text, sorted by start. The caller decides
    what to print; a Hit carries offsets only, never the text."""
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % (MODES,))
    hits = []
    for rx in _KEY_RES:
        hits += [Hit("key_prefix", m.start(), m.end()) for m in rx.finditer(text)]
    hits += [Hit("telegram_token", m.start(), m.end()) for m in _TELEGRAM_RE.finditer(text)]
    for m in _ENTROPY_RE.finditer(text):
        if _entropy_hit(m.group(0)):
            hits.append(Hit("high_entropy", m.start(), m.end()))
    hits += [Hit("kr_rrn", m.start(), m.end()) for m in _RRN_RE.finditer(text)]
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            hits.append(Hit("card", m.start(), m.end()))
    hits += [Hit("webhook", m.start(), m.end()) for m in _WEBHOOK_RE.finditer(text)]
    # keyword_value: the span is the VALUE, so redact() keeps the keyword
    # readable ('<kw> <redacted>') and drops only what follows it.
    hits += [Hit("keyword_value", m.start(3), m.end(3))
             for m in _KW_VALUE_RE.finditer(text) if _kw_value_hit(m)]
    if mode == "strict":
        hits += [Hit("keyword", m.start(), m.end()) for m in _KW_BARE_RE.finditer(text)]
    hits.sort(key=lambda h: (h.start, h.end, h.cat))
    return hits


def redact(text):
    """text with every body-mode hit replaced. A Telegram token becomes
    '<token>' (byte-compatible with the mint_flow.mask() it supersedes);
    every other shape becomes '<redacted>'."""
    if not text:
        return text
    spans = []
    for h in scan(text, "body"):
        if spans and h.start < spans[-1][1]:
            s, e, cats = spans[-1]
            spans[-1] = (s, max(e, h.end), cats | {h.cat})
        else:
            spans.append((h.start, h.end, {h.cat}))
    out, pos = [], 0
    for s, e, cats in spans:
        out.append(text[pos:s])
        out.append("<token>" if "telegram_token" in cats else "<redacted>")
        pos = e
    out.append(text[pos:])
    return "".join(out)


# --- charset floor (spec 3.2 F4 / 6 check 4); used by custom_skill.py and
# steps.sh final-lint. Categories banned anywhere: Cc (except \t \n), Cf,
# Zl, Zp, Co, Cs.
BANNED_CATS = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs")
_CC_OK = ("\t", "\n")


def bad_chars(text):
    """[(offset, category)] for each banned character; never the char itself."""
    out = []
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat in BANNED_CATS and ch not in _CC_OK:
            out.append((i, cat))
    return out


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def read_text(path):
    """(text, None) / (None, 'binary') / raises OSError."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if b"\0" in raw:
        return None, "binary"
    return raw.decode("utf-8", errors="replace"), None


def main(argv):
    usage = "usage: secret_scan.py --mode strict|body FILE..."
    if len(argv) < 3 or argv[0] != "--mode" or argv[1] not in MODES:
        print(usage, file=sys.stderr)
        return 2
    mode, files = argv[1], argv[2:]
    found = False
    for path in files:
        try:
            text, skip = read_text(path)
        except OSError as exc:
            print("secret_scan: cannot read %s (%s)" % (path, exc.strerror), file=sys.stderr)
            return 2
        if skip:
            continue
        seen = set()
        for h in scan(text, mode):
            key = (h.cat, line_of(text, h.start))
            if key in seen:
                continue
            seen.add(key)
            print("%s %s:%d" % (h.cat, path, key[1]))
            found = True
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
