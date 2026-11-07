#!/usr/bin/env python3
"""DGN-2048: DONE is the user's unit of work -- the dispatch forcing points.

Canon (rules/hot.framework.md -- Done): DONE is defined on the unit of work
as the user experiences it, no-regression is part of DONE, and the
verification gate is set by the dispatcher from that unit BEFORE dispatch.
This reads the two ends of a dispatch:

  brief  -- the dispatcher's brief carries, near the top:
              Unit of work: <the whole thing the user experiences>
              E2E gate:     <the end-to-end, user-screen check that proves it>
  return -- the worker's final report carries:
              Open items: <what still does not work vs the unit of work,
                           or "none">
              Re-checked: <the previously-working behaviours re-checked>

Labels are matched at line start, case-insensitive, after optional markdown
bullets/heading/bold, with a short gloss allowed before the colon
("Open items vs the unit of work:", "Re-checked (previously working):").
A brief line needs >= BRIEF_MIN_CHARS of content. A return label counts
when it has inline content or a non-blank line right under it (a list).
Lines inside ``` fences do not count.

Verdicts (one line, tab-separated; fields hold no tab/newline):
  brief  <ok|missing:unit|missing:gate|missing:unit,gate>\t<unit>\t<gate>
  return <ok|missing:open-items|missing:rechecked|
          missing:open-items,rechecked>\t<open items>\t<re-checked>

ADVISORY for the brief (dispatch has no refusal: LOCKED Why-net 5.3,
dec-317; same contract as brief_why_what.py). The return verdict marks the
dispatch-return drop "report incomplete"; it never changes the run result.
Exit 0 for any readable file, 2 for an unreadable one / bad usage.

Usage: done_blocks.py brief|return <file>
"""

import re
import sys

BRIEF_MIN_CHARS = 6
FIELD_MAX = 200

_PREFIX = r"^\s*(?:[-*+]\s+|#{1,6}\s+|>\s*)*(?:\*\*|__)?"
_COLON = r"\s*(?:\*\*|__)?\s*[:\uff1a]\s*(?:\*\*|__)?\s*(.*)$"

# key -> label alternatives (regex, no capture groups)
BRIEF_LABELS = {
    "unit": r"unit[- ]of[- ]work",
    "gate": r"(?:e2e|end[- ]to[- ]end)(?:[- ](?:verification|acceptance))?[- ]gate",
}
RETURN_LABELS = {
    "open-items": r"open[- ]items|\uc544\uc9c1 \uc548 \ub418\ub294 (?:\uac83|\uac70)",
    "rechecked": (r"re-?checked|regression[- ]re-?check|"
                  r"previously[- ]working(?:[- ]behaviou?rs?)?[- ]re-?checked"),
}


def _label_re(alts):
    return re.compile(_PREFIX + r"(?:%s)(?:[^:\uff1a\n]{0,48})?" % alts + _COLON,
                      re.IGNORECASE)


_BRIEF_RES = {k: _label_re(v) for k, v in BRIEF_LABELS.items()}
_RETURN_RES = {k: _label_re(v) for k, v in RETURN_LABELS.items()}


def _clean(text):
    text = re.sub(r"[\t\r\n]+", " ", text).strip()
    text = re.sub(r"(\*\*|__)$", "", text).strip()
    return text[:FIELD_MAX]


def _lines(text):
    """Lines outside ``` fences."""
    out, fence = [], False
    for ln in (text or "").splitlines():
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(ln)
    return out


def scan_brief(text):
    found = {k: "" for k in BRIEF_LABELS}
    for ln in _lines(text):
        for key, rx in _BRIEF_RES.items():
            m = rx.match(ln)
            if m and not found[key] and len(_clean(m.group(1))) >= BRIEF_MIN_CHARS:
                found[key] = _clean(m.group(1))
    return found


def scan_return(text):
    """The LAST qualifying block of each label wins: the final report sits
    at the end of out.log, after any narration that quoted a label."""
    found = {k: "" for k in RETURN_LABELS}
    lines = _lines((text or "").split("\n--- stderr ---\n", 1)[0])
    for i, ln in enumerate(lines):
        for key, rx in _RETURN_RES.items():
            m = rx.match(ln)
            if not m:
                continue
            body = _clean(m.group(1))
            if not body:
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt and not any(r.match(nxt) for r in _RETURN_RES.values()):
                    body = _clean(nxt)
            if body:
                found[key] = body
    return found


def verdict(found):
    missing = [k for k in found if not found[k]]
    return "ok" if not missing else "missing:" + ",".join(missing)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("brief", "return"):
        sys.stderr.write("usage: done_blocks.py brief|return <file>\n")
        return 2
    try:
        with open(argv[2], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write("done_blocks: cannot read %s: %s\n" % (argv[2], exc))
        return 2
    found = scan_brief(text) if argv[1] == "brief" else scan_return(text)
    sys.stdout.write("\t".join([verdict(found)] + list(found.values())) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
