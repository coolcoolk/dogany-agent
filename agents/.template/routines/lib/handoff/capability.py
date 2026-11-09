#!/usr/bin/env python3
"""DGN-1379 R1 -- capability declaration parser (config/handoff.capabilities).

Locks the capability-model grammar from DGN-1379 design v2 sec.2.2 / v3
sec.3.2 (unchanged since -- carried forward through v3.1/v3.2/v3.3/v3.4):

  # <selector> <mode> [key=value ...]
  redirect.utterance exec handler=routines/redirect-respond.sh timeout=600 idem=ulid
  decision.notice builtin:notice-append idem=ulid post=routines/handoff-on-notice.sh
  proposal.schedule exec handler=routines/handoff-proposal.sh idem=domain:created_by-cas emits=decision.notice
  report.section. leave consumer=briefing-aggregation
  * notify renotify_after=6h

Grammar (locked, do not extend without a design amendment):
  - one declaration per line, fields space-separated, '#' starts a comment
    that runs to end of line.
  - NO line continuation ('\\' has no meaning here) -- the grammar has no
    multi-line form.
  - mode is one of: exec | builtin:<name> | leave | notify.
      exec    requires handler= and idem=
      leave   requires consumer= (capability model requirement d: leave is
              a first-class verdict, not a failure or silent consumption)
      builtin:<name> and notify take no required keys.
  - selector resolution: exact match wins; else the longest selector ending
    in '.' that is a prefix of the type; else '*'. Selectors are sorted by
    length (descending) at load time so resolution never depends on file
    order (the legacy _resolve_handler dict-insertion-order bug this
    grammar replaces).
  - parsing is FILE-LEVEL fail-closed: a single malformed line (unknown
    mode, missing required key, duplicate selector, malformed key=value)
    invalidates the WHOLE file. Row-level tolerance is exactly the defect
    this grammar exists to forbid (design v2 sec.2.2): a broken exec line
    must never silently fall through to '*' and turn an RPC into a mute
    notify.
  - an undeclared type is never consumed -- callers must treat
    resolve(type) returning None as "leave", never as an error
    (design v2 sec.2.2: "an undeclared type is NEVER consumed").

English/ASCII only. No live side effects: this module only parses text.
"""

import re

VALID_MODES_SIMPLE = ("leave", "notify")
_KV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


class CapabilityFileInvalid(ValueError):
    """Raised for the whole file when any single line fails to parse.

    Callers must treat this as: every type leaves, status becomes
    INVALID(line N), and the owner is pushed once (design v2 sec.2.2 /
    ticket requirement 'file unit fail-closed'). This module raises; it
    does not itself leave/push/log -- that is the engine's job.
    """


class Entry(object):
    """One parsed capability line."""

    __slots__ = ("selector", "mode", "builtin_name", "options", "line_no")

    def __init__(self, selector, mode, builtin_name, options, line_no):
        self.selector = selector
        self.mode = mode              # "exec" | "builtin" | "leave" | "notify"
        self.builtin_name = builtin_name   # set only when mode == "builtin"
        self.options = options        # dict of key -> value (strings)
        self.line_no = line_no

    def __repr__(self):
        return "Entry(%r, %r, opts=%r)" % (self.selector, self.mode,
                                            self.options)


def _parse_mode(token, line_no):
    if token in VALID_MODES_SIMPLE:
        return token, None
    if token == "exec":
        return "exec", None
    if token.startswith("builtin:") and len(token) > len("builtin:"):
        return "builtin", token[len("builtin:"):]
    raise CapabilityFileInvalid(
        "line %d: unknown mode %r (expected exec | builtin:<name> | "
        "leave | notify)" % (line_no, token))


def _parse_options(tokens, line_no):
    options = {}
    for tok in tokens:
        if not _KV_RE.match(tok):
            raise CapabilityFileInvalid(
                "line %d: malformed key=value token %r" % (line_no, tok))
        key, _, value = tok.partition("=")
        if key in options:
            raise CapabilityFileInvalid(
                "line %d: duplicate key %r" % (line_no, key))
        options[key] = value
    return options


def _strip_comment(line):
    # '#' is a comment marker only outside of key=value tokens; the grammar
    # never puts '#' inside a value, so a plain split is exact and matches
    # the locked example table (design v2 sec.2.2) byte for byte.
    idx = line.find("#")
    return line if idx == -1 else line[:idx]


def parse_line(line, line_no):
    """Parse one non-blank, non-comment-only line -> Entry.

    Raises CapabilityFileInvalid on any grammar violation.
    """
    body = _strip_comment(line).strip()
    if not body:
        return None
    parts = body.split()
    if len(parts) < 2:
        raise CapabilityFileInvalid(
            "line %d: expected '<selector> <mode> [key=value ...]', got %r"
            % (line_no, line))
    selector, mode_token = parts[0], parts[1]
    mode, builtin_name = _parse_mode(mode_token, line_no)
    options = _parse_options(parts[2:], line_no)
    if mode == "exec":
        missing = [k for k in ("handler", "idem") if k not in options]
        if missing:
            raise CapabilityFileInvalid(
                "line %d: exec requires %s" % (line_no, "+".join(missing)))
    if mode == "leave" and "consumer" not in options:
        raise CapabilityFileInvalid(
            "line %d: leave requires consumer=" % line_no)
    return Entry(selector, mode, builtin_name, options, line_no)


def parse_text(text):
    """Parse the full file text -> list[Entry] (selector-length descending).

    Whole-file fail-closed: raises CapabilityFileInvalid (naming the first
    offending line) on any error, including duplicate selectors across
    lines. Never returns a partial result.
    """
    entries = []
    seen_selectors = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        entry = parse_line(raw, line_no)
        if entry is None:
            continue
        if entry.selector in seen_selectors:
            raise CapabilityFileInvalid(
                "line %d: duplicate selector %r (first declared at line %d)"
                % (line_no, entry.selector, seen_selectors[entry.selector]))
        seen_selectors[entry.selector] = line_no
        entries.append(entry)
    entries.sort(key=lambda e: len(e.selector), reverse=True)
    return entries


def load(path):
    """Read + parse a capabilities file. Raises CapabilityFileInvalid or
    OSError (missing/unreadable file) -- both are the caller's cue to run
    the "file unit fail-closed" path (all types leave, status set, owner
    pushed once)."""
    with open(path, encoding="utf-8") as fh:
        return parse_text(fh.read())


def resolve(entries, mtype):
    """Resolve a message type against parsed entries.

    Priority: exact match > longest '.'-suffixed prefix match > '*'.
    Returns the matching Entry, or None (undeclared type -- caller must
    treat as leave, per the ticket's capability-model requirement: an
    undeclared type is never consumed).
    """
    exact = None
    prefix = None
    wildcard = None
    for entry in entries:      # already length-descending from parse_text
        if entry.selector == mtype:
            exact = entry
            break
        if entry.selector == "*":
            if wildcard is None:
                wildcard = entry
            continue
        if entry.selector.endswith(".") and mtype.startswith(entry.selector):
            if prefix is None:
                prefix = entry
    return exact or prefix or wildcard
