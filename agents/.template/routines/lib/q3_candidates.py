#!/usr/bin/env python3
"""q3_candidates.py -- DERIVED onboarding Q3 address-term candidates (DGN-1719).

The dogany-user-onboarding SKILL (Q3 CANDIDATES) allows only two declared
sources, in this order:
  (1) the owner profile's name -- the Name line of this instance's
      identity/hot.custom.owner.md (mint carries it from the parent,
      scripts/pack/lib/owner_carry.py). Korean instances offer it as the
      given name + the honorific suffix with NO space (owner-confirmed
      rehearsal sample 2026-09-26: "<given>nim", never "<given> nim").
  (2) the address term the estate's leader agent already uses -- the leader
      persona's "Call the user" line (the older "Call user:" form is read
      too). The leader is the instance that minted this one, recorded by
      mint as .instance.conf DOGANY_LEADER_ROOT.
Missing, blank and unset-placeholder values are ignored, surrounding
whitespace is trimmed and exact duplicates collapse. Nothing is invented:
zero results means the bare Q3 question, no buttons; one or more results
means the Q3-with-candidates question and candidate buttons.

CLI:  python3 q3_candidates.py <instance-root> [--lang ko|en] [--json]
      prints one candidate per line (or a JSON list). Always rc 0.
Pure stdlib. English/ASCII only (Korean text is \\u escapes).
"""
import json
import os
import re
import sys

HONORIFIC_KO = "님"                 # suffix glued to the given name
_HANGUL_NAME_RE = re.compile(r"[가-힣]{2,4}")
_NAME_LINE_RE = re.compile(r"^\s*-\s*(?:\*\*)?Name\s*(?::\s*\*\*|\*\*\s*:|:)\s*(.+)$", re.I)
_CALL_LINE_RE = re.compile(r"^\s*-\s*Call (?:the )?user:?\s*\*\*(.+?)\*\*", re.I)
_PAREN_RE = re.compile(r"\(([^()]*)\)")
# Provenance parentheticals ("(2026-09-26, owner)", "(carried from X at
# mint ...)") are metadata, not part of the name.
_PROVENANCE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|carried from|source", re.I)
_PLACEHOLDER_RE = re.compile(r"set at onboarding|^__[A-Za-z0-9_]+__$|^\[[^\]]*\]$|^you$", re.I)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _conf(path, key):
    for line in _read(path).splitlines():
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v.strip()
    return ""


def _usable(value):
    v = (value or "").strip().strip("*").strip()
    if not v or _PLACEHOLDER_RE.search(v):
        return ""
    return v


def _strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def owner_name(root):
    """Raw Name value of the owner file, provenance parentheticals removed."""
    for rel in (os.path.join("identity", "hot.custom.owner.md"), "USER.md"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        for line in _strip_comments(_read(path)).splitlines():
            m = _NAME_LINE_RE.match(line)
            if not m:
                continue
            value = _PAREN_RE.sub(
                lambda p: "" if _PROVENANCE_RE.search(p.group(1)) else p.group(0),
                m.group(1))
            return _usable(value)
        return ""
    return ""


def owner_name_candidate(name, lang):
    """ko: Hangul given name + honorific suffix, no space. Other languages:
    the name as written minus a trailing gloss."""
    if not name:
        return ""
    if lang == "ko":
        m = _HANGUL_NAME_RE.search(name)
        if not m:
            return ""
        full = m.group(0)
        # A name already written with the suffix ("<given>nim") must not get a
        # second one: the suffix is not part of the name.
        if full.endswith(HONORIFIC_KO) and len(full) > 2:
            full = full[:-len(HONORIFIC_KO)]
        given = full[2:] if len(full) == 4 else full[1:] if len(full) == 3 else full
        return given + HONORIFIC_KO
    return _usable(re.sub(r"\s*\([^)]*\)\s*$", "", name))


def leader_root(root):
    lead = _conf(os.path.join(root, ".instance.conf"), "DOGANY_LEADER_ROOT")
    if not lead:
        return ""
    lead = os.path.expanduser(lead)
    try:
        if os.path.realpath(lead) == os.path.realpath(root):
            return ""
    except OSError:
        return ""
    return lead if os.path.isdir(lead) else ""


def leader_term(root):
    lead = leader_root(root)
    if not lead:
        return ""
    for rel in (os.path.join("identity", "hot.custom.agent.md"), "PROFILE.md", "AGENT.md"):
        path = os.path.join(lead, rel)
        if os.path.isfile(path):
            for line in _read(path).splitlines():
                m = _CALL_LINE_RE.match(line)
                if m:
                    return _usable(m.group(1))
            return ""
    return ""


def instance_lang(root):
    return _conf(os.path.join(root, "config", "agent.conf"), "AGENT_LANG") or "en"


def candidates(root, lang=None):
    lang = lang or instance_lang(root)
    out = []
    for c in (owner_name_candidate(owner_name(root), lang), leader_term(root)):
        c = (c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    lang = None
    if "--lang" in argv:
        i = argv.index("--lang")
        lang = argv[i + 1] if i + 1 < len(argv) else None
        args = [a for a in args if a != lang]
    root = args[0] if args else os.getcwd()
    try:
        found = candidates(root, lang)
    except Exception:
        found = []
    if "--json" in argv:
        print(json.dumps(found, ensure_ascii=False))
    else:
        for c in found:
            print(c)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
