#!/usr/bin/env python3
"""owner_carry.py -- carry the owner's STABLE profile facts into a newborn (DGN-1719).

A newborn's identity/hot.custom.owner.md is minted as an empty scaffold, so
onboarding Q3 had no owner Name to derive an address-term candidate from
(rehearsal 2026-09-26 item B8: fewer than two candidates -> no buttons).
Mint now copies the parent instance's one-line profile facts across.

What is carried (rules/hot.framework.md Memory routing -- the owner file holds
STABLE PROFILE FACTS ONLY, one-line facts with (date, source)):
  - top-level labeled bullets ("- Key: value" / "- **Key:** value") that are
    a single line (no indented continuation),
  - each line verbatim, keeping its own (date, source), plus a
    "(carried from <parent> at mint, <date>)" source note.
What is never carried:
  - HTML comments, headings, nested bullets, multi-line entries,
  - procedure / rule lines (owner-file rules forbid them anyway),
  - address-term lines: the address term is a per-agent Q3 answer, and any
    occurrence of the parent's own term inside a carried line is replaced
    by the neutral word "owner" (owner decision 2026-09-15, DGN-1464),
  - anything from the parent's persona (only the owner file is read).
Keys already present in the newborn's owner file are left alone (re-run safe).
No parent owner file / no facts -> the newborn file is unchanged.

CLI: owner_carry.py --parent <root> --child <root> [--date YYYY-MM-DD]
     [--parent-label <name>] [--dry-run]
rc 0 = done (including nothing to carry); rc 2 = usage / child file missing.
Pure stdlib. English/ASCII only (Korean patterns are \\u escapes).
"""
import argparse
import datetime
import os
import re
import sys
import tempfile

OWNER_REL = os.path.join("identity", "hot.custom.owner.md")
LEGACY_OWNER_REL = "USER.md"
PERSONA_RELS = (os.path.join("identity", "hot.custom.agent.md"), "PROFILE.md", "AGENT.md")
NEUTRAL_TERM = "owner"

_FACT_RE = re.compile(
    r"^-\s+(?:\*\*)?(?P<key>[^*:\n]{1,40}?)\s*(?::\s*\*\*|\*\*\s*:|:)\s*(?P<val>\S.*)$")
# Address-term keys: "address", "call (the) user", "honorific", "nickname",
# Korean "hoching" (address term).
_ADDRESS_KEY_RE = re.compile(
    r"address|\bcall\b|honorific|nickname|호칭", re.I)
# Procedure / rule wording (English + Korean "always", "never", "must",
# "do not", "rule", "procedure", "forbidden").
_PROCEDURE_RE = re.compile(
    r"\b(always|never|must|should|do not|don't|rule|procedure|workflow|"
    r"output format)\b|항상|절대|반드시|"
    r"하지 말|금지|규칙|절차", re.I)
_CALL_LINE_RE = re.compile(r"^\s*-\s*Call (?:the )?user:?\s*\*\*(.+?)\*\*", re.I)
_PLACEHOLDER_RE = re.compile(r"set at onboarding|^__[A-Za-z0-9_]+__$|^you$", re.I)


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
    return "" if (not v or _PLACEHOLDER_RE.search(v)) else v


def owner_file(root):
    for rel in (OWNER_REL, LEGACY_OWNER_REL):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            return path
    return ""


def parent_terms(parent):
    """Every form the parent uses to address the owner."""
    terms = [
        _usable(_conf(os.path.join(parent, ".instance.conf"), "DOGANY_USER_LABEL")),
        _usable(_conf(os.path.join(parent, "config", "agent.conf"), "DASHBOARD_OWNER_TERM")),
    ]
    for rel in PERSONA_RELS:
        path = os.path.join(parent, rel)
        if os.path.isfile(path):
            for line in _read(path).splitlines():
                m = _CALL_LINE_RE.match(line)
                if m:
                    terms.append(_usable(m.group(1)))
                    break
            break
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return sorted(out, key=len, reverse=True)


def _identity_name(root):
    """The agent's onboarding name via the single resolver
    agents/.template/routines/lib/agent_name.py (DGN-1744); "" when unnamed
    or the resolver is unreachable."""
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.normpath(os.path.join(here, "..", "..", "..", "agents", ".template",
                                             "routines", "lib", "agent_name.py"))
        spec = importlib.util.spec_from_file_location("dogany_agent_name", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return _usable(module.identity_name(root))
    except Exception:
        return ""


def parent_label(parent):
    return (_identity_name(parent)
            or _usable(_conf(os.path.join(parent, ".instance.conf"), "DOGANY_AGENT_LABEL"))
            or os.path.basename(os.path.normpath(parent)))


def _key(key):
    return re.sub(r"\s+", " ", key.strip().strip("*").strip()).lower()


def stable_facts(text):
    """[(key, line)] of single-line labeled top-level facts."""
    lines = re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines()
    facts = []
    for i, line in enumerate(lines):
        m = _FACT_RE.match(line.rstrip())
        if not m:
            continue
        nxt = next((l for l in lines[i + 1:] if l.strip()), "")
        if nxt[:1].isspace():
            continue                      # multi-line entry: not a one-line fact
        key = m.group("key")
        if _ADDRESS_KEY_RE.search(key) or _PROCEDURE_RE.search(line):
            continue
        facts.append((_key(key), line.rstrip()))
    return facts


def existing_keys(text):
    keys = set()
    for line in re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines():
        m = _FACT_RE.match(line.rstrip())
        if m:
            keys.add(_key(m.group("key")))
    return keys


def carry(parent, child, date, label=None, dry_run=False):
    """Returns the list of lines appended (empty = unchanged)."""
    src = owner_file(parent)
    dst = os.path.join(child, OWNER_REL)
    if not src:
        return []
    if not os.path.isfile(dst):
        raise FileNotFoundError(dst)
    label = label or parent_label(parent)
    terms = parent_terms(parent)
    current = _read(dst)
    have = existing_keys(current)
    note = "(carried from %s at mint, %s)" % (label, date)
    added = []
    for key, line in stable_facts(_read(src)):
        if key in have:
            continue
        for t in terms:
            line = line.replace(t, NEUTRAL_TERM)
        added.append("%s %s" % (line, note))
        have.add(key)
    if added and not dry_run:
        body = current if current.endswith("\n") or not current else current + "\n"
        body += "\n" + "\n".join(added) + "\n"
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".owner-carry.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            os.chmod(tmp, os.stat(dst).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, dst)
    return added


def main(argv):
    ap = argparse.ArgumentParser(prog="owner_carry.py")
    ap.add_argument("--parent", required=True)
    ap.add_argument("--child", required=True)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--parent-label", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not owner_file(a.parent):
        print("[owner-carry] SKIP no owner file in parent %s -- newborn owner file unchanged" % a.parent)
        return 0
    try:
        added = carry(a.parent, a.child, a.date, a.parent_label, a.dry_run)
    except FileNotFoundError as e:
        print("[owner-carry] ERROR newborn owner file missing: %s" % e, file=sys.stderr)
        return 2
    verb = "would carry" if a.dry_run else "carried"
    print("[owner-carry] %s %d profile fact line(s) into %s" % (verb, len(added), os.path.join(a.child, OWNER_REL)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
