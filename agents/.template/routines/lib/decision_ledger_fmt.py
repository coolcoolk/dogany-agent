"""decision_ledger_fmt -- the decision ledger's one format (DGN-1967 step 5).

The DGN-1952 ledger (routines/decision-ledger.py writes it) is read by the
Why net seed (why_net/seed.py) and the dec join (why_net/acts.py).  Each kept
its own marker, options and pick regexes; they live here now, imported by
all three:

  DEFAULTS / id_regex / ANSWER_MAX   the entry templates and id pattern
  MARK_RE / mark()                   "<!-- decision-ledger id=.. state=.. -->"
  options_text / options_of_line     the "- Options: 1. a / 2. b" line
  pick / answers_menu                which option a reply picks, if any
  read_inbound / request_id_for      the bridge's per-turn inbound-context
                                     (moved from why_net/ups.py)
  tz / now                           the instance clock (lib/tz_offset.sh)

Options separator: labels are written "1. a / 2. b" (unchanged, the
owner's ledger reads the same).  A label may itself hold " / " ("Keep A /
B"), so the reader splits only at " / " followed by the NEXT item number
("2. "): the numbering is the separator.  Residual: a label holding
" / <its successor number>. " still splits there.

Pick (bug 15): a reply picks option N only when it is
  - a bare number in range ("2", "2.", "2)"),
  - "N. " / "N) " followed by option N's own label,
  - an option's label, or
  - a bridge number handle ("No.2", "2<beon>"; whitespace ignored).
"1.5 hours is too long" and "1. but cheaper" are the owner's words, and
with no labels parsed no number picks anything.

Pure stdlib.  English/ASCII only.
"""

import datetime
import glob
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODE_ROOT = os.path.dirname(os.path.dirname(_HERE))

DEFAULTS = {
    "DECISION_LEDGER_ID_FORMAT": "dec-{n}",
    "DECISION_LEDGER_HEADING": "## {id} -- {question} ({date}) {tag}",
    "DECISION_LEDGER_PENDING_STATUS": "pending {date} {time}",
    "DECISION_LEDGER_CONFIRMED_STATUS": "confirmed {date} {time}",
    "DECISION_LEDGER_PENDING_TAG": "[{status}]",
    "DECISION_LEDGER_CONFIRMED_TAG": "[{status}: \"{answer}\"]",
}
QUESTION_MAX = 140
ANSWER_MAX = 300
DECISION_KINDS = ("shape", "scope", "fork")
DECISION_KIND_ALIASES = {"shape": "shape", "scope": "scope", "fork": "fork",
                         "\ubaa8\uc591": "shape", "\ubc94\uc704": "scope",
                         "\uac08\ub9bc\uae38": "fork"}


def decision_kind(value):
    """Canonical menu kind, or None for a missing/unknown declaration."""
    if not isinstance(value, str):
        return None
    return DECISION_KIND_ALIASES.get(value.strip().casefold())


def id_regex(fmt):
    head, sep, tail = fmt.partition("{n}")
    if not sep:
        raise ValueError("DECISION_LEDGER_ID_FORMAT has no {n}")
    return re.compile(r"(?<![\w-])" + re.escape(head) + r"(\d+)"
                      + re.escape(tail) + r"(?!\d)")


# ---------------------------------------------------------------------------
# The machine mark
# ---------------------------------------------------------------------------

MARK_RE = re.compile(
    r"<!--\s*decision-ledger id=(\S+) state=(pending|confirmed)"
    r"(?: req=(\S+))?\s*-->")


def mark(dec_id, state="pending", req=None):
    return "<!-- decision-ledger id=%s state=%s%s -->" % (
        dec_id, state, " req=%s" % req if req else "")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

OPTIONS_HEAD = "- Options: "
OPTION_SEP = " / "
NO_OPTIONS = "(none parsed)"
_ITEM_RX = re.compile(r"^\s*(\d+)\.\s*(.*?)\s*$", re.DOTALL)


def options_text(labels):
    """"1. a / 2. b" (NO_OPTIONS when there are none)."""
    return OPTION_SEP.join("%d. %s" % (i + 1, label)
                           for i, label in enumerate(labels)) or NO_OPTIONS


def parse_options(text):
    """options_text() -> labels.  Splits only at OPTION_SEP + the next item
    number, so a label holding " / " stays whole."""
    m = _ITEM_RX.match(text or "")
    if not m or m.group(1) != "1":
        return []
    labels, rest, n = [], m.group(2), 1
    while True:
        cut = rest.find("%s%d." % (OPTION_SEP, n + 1))
        if cut < 0:
            labels.append(rest.strip())
            return [x for x in labels if x]
        labels.append(rest[:cut].strip())
        rest = rest[cut + len(OPTION_SEP):]
        n += 1
        rest = _ITEM_RX.match(rest).group(2)


def options_of_line(line):
    """The labels of a ledger "- Options: ..." line, else None."""
    if not line.startswith(OPTIONS_HEAD):
        return None
    return parse_options(line[len(OPTIONS_HEAD):].rstrip())


# ---------------------------------------------------------------------------
# Picks
# ---------------------------------------------------------------------------

_BARE_NUM = re.compile(r"^(\d+)\s*[.\u3001)\uff09]?$")
_NUM_LABEL = re.compile(r"^(\d+)\s*[.\u3001)\uff09]\s*(.+)$", re.DOTALL)
_NUM_SPACE = re.compile(r"^\d+\s*[.\u3001)\uff09]\s+\S")
_HANDLES = None


def _norm(text):
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _squash(text):
    return "".join(unicodedata.normalize("NFC", text).split()).casefold()


def number_handles():
    """option_number_handle of every bridge i18n catalog ("No.{n}", ...)."""
    global _HANDLES
    if _HANDLES is None:
        out = []
        for path in sorted(glob.glob(os.path.join(_CODE_ROOT, "bridge", "i18n",
                                                  "*.py"))):
            try:
                spec = importlib.util.spec_from_file_location(
                    "_dlf_i18n_" + os.path.basename(path)[:-3], path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                value = (getattr(mod, "STRINGS", {}) or {}).get(
                    "option_number_handle")
            except Exception:
                continue
            if isinstance(value, str) and "{n}" in value:
                out.append(value.strip())
        _HANDLES = out
    return _HANDLES


def pick(text, labels):
    """The 1-based option `text` picks, or None (see the module doc)."""
    t = (text or "").strip()
    count = len(labels)
    if not t or not count:
        return None
    norm = _norm(t)
    m = _BARE_NUM.match(t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= count else None
    m = _NUM_LABEL.match(t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= count and _norm(m.group(2)) == _norm(labels[n - 1]):
            return n
    for i, label in enumerate(labels):
        if label and norm == _norm(label):
            return i + 1
    squashed = _squash(t)
    for handle in number_handles():
        for n in range(1, count + 1):
            if squashed == _squash(handle.replace("{n}", str(n))):
                return n
    return None


def answers_menu(text, labels):
    return pick(text, labels) is not None


def pick_shaped(text, labels):
    """True when `text` reads as a menu pick, not the owner's own words (the
    Why net seed): a pick, or -- for an entry whose options were not
    parsed -- a bare number, a number handle or an "N. ..." reply, which
    cannot be told apart from a label tap."""
    if answers_menu(text, labels):
        return True
    if labels:
        return False
    t = (text or "").strip()
    if _BARE_NUM.match(t) or _NUM_SPACE.match(t):
        return True
    squashed = _squash(t)
    return any(re.match("^" + re.escape(_squash(h)).replace(
        re.escape("{n}"), r"\d+") + "$", squashed) for h in number_handles())


# ---------------------------------------------------------------------------
# The bridge's inbound-context (one reader for decision-ledger and ups)
# ---------------------------------------------------------------------------

INBOUND_FILE = "inbound-context.json"
INBOUND_MAX_AGE = 300  # seconds; an older inbound-context is an earlier turn
_REQ_ID = re.compile(r"^[A-Za-z0-9._:-]+$")


def read_inbound(bot_dir, now_ts):
    """The bridge's per-turn inbound-context, or {} when absent / stale /
    without a request_id."""
    try:
        with open(os.path.join(bot_dir, INBOUND_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    ts = d.get("ts")
    if not isinstance(ts, (int, float)) or abs(now_ts - ts) > INBOUND_MAX_AGE:
        return {}
    if not isinstance(d.get("request_id"), str) or not d["request_id"]:
        return {}
    return d


def text_sha(text):
    """The bridge's text_sha: sha256 of the NFC prompt it sent."""
    return hashlib.sha256(unicodedata.normalize("NFC", text or "").encode(
        "utf-8")).hexdigest()


def request_id_for(inbound, prompt):
    """The inbound's request id when it names THIS prompt, else None."""
    rid = (inbound or {}).get("request_id")
    if not rid or inbound.get("text_sha") != text_sha(prompt):
        return None
    return rid if _REQ_ID.match(rid) else None


# ---------------------------------------------------------------------------
# The instance clock
# ---------------------------------------------------------------------------

def tz(root):
    """The instance's UTC offset as a timezone, resolved by the ONE resolver
    (lib/tz_offset.sh: agent.conf AGENT_TZ_OFFSET_HOURS, else the crew
    TZ_OFFSET_HOURS, else the host); None when it cannot resolve (the
    caller then uses host local time)."""
    try:
        out = subprocess.run(
            ["bash", os.path.join(_HERE, "tz_offset.sh"), "resolve", root],
            capture_output=True, text=True, timeout=10)
        hours = int(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        hours = None
    if hours is None:
        return None
    return datetime.timezone(datetime.timedelta(hours=hours))


def now(root):
    """The instance wall clock (aware), host local time as the fallback."""
    zone = tz(root)
    return datetime.datetime.now(zone) if zone else \
        datetime.datetime.now().astimezone()
