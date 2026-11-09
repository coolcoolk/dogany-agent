#!/usr/bin/env python3
"""persona-audit -- nightly read-only drift audit of an instance's baseline
(DGN-1891).

The persona file (identity/hot.custom.agent.md), the owner profile
(identity/hot.custom.owner.md) and the skill set (.claude/skills/) drift away
from the installed template over time and nothing looked at them. This
routine diffs them and writes a PROPOSAL list. It never edits anything:
baseline edits are owner-gated (rules/hot.framework.md Edit rights) and go
through the baseline-editor path once the owner approves an item.

Checks (each proposal carries kind / summary / evidence):
  missing_workflow  a "### " workflow under the template's "## Workflows"
                    is absent from the instance persona.
  stale_history     a persona line (not a verbatim template line) that reads
                    as a history entry: an ISO date stamp or a history word.
                    The persona is read every session; history belongs in
                    memory, not in always-loaded baseline.
  duplicate_skill   an instance-authored skill (not dogany-*) that overlaps a
                    shipped dogany-* skill by name token or description.
  owner_profile_empty
                    the owner profile has no fact line while memory notes
                    mention profile facts (name / timezone / app).
  canon_covered     (DGN-2052, post-update only) a custom-layer line --
                    rules/hot.custom.md, a persona Workflows line that is not
                    a verbatim template line, or a local (non dogany-*)
                    skill's SKILL.md -- that a canon line ADDED by the last
                    update now covers: propose dropping it.
  canon_retired     (DGN-2052, post-update only) a custom-layer line that
                    echoes a canon line the update REMOVED and matches no
                    current canon line: propose a rewrite against the new
                    canon (or a drop).

Canon review input: update.sh keeps the pre-update rules/hot.framework.md at
<root>/memory-engine/.canon-prev/hot.framework.md whenever an update changes
the canon (the oldest snapshot wins until reviewed: the custom layers were
written against it). The canon checks diff that snapshot against the current
rules/hot.framework.md. When the review finds nothing left to tidy, the
snapshot is consumed (removed) -- the one file besides the output json this
routine touches; with no snapshot the canon checks do not run.

Template resolution: --template, else DOGANY_REPO_ROOT from .instance.conf
+ /agents/.template. No template -> missing_workflow / stale_history are
skipped (logged), the other checks still run.

Output: <root>/memory-engine/.persona-audit.json (atomic replace), plus one
"persona-audit: proposals=<n> ..." summary line on stdout. --render prints
the numbered DRAFT message text instead of the summary line.
Exit: 0 audit ran (any proposal count), 2 usage error (root missing).
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile

OUT_REL = os.path.join("memory-engine", ".persona-audit.json")
PERSONA_REL = os.path.join("identity", "hot.custom.agent.md")
OWNER_REL = os.path.join("identity", "hot.custom.owner.md")
SKILLS_REL = os.path.join(".claude", "skills")
MEMORIES_REL = "memories"
CANON_REL = os.path.join("rules", "hot.framework.md")
CUSTOM_RULES_REL = os.path.join("rules", "hot.custom.md")
CANON_PREV_REL = os.path.join("memory-engine", ".canon-prev", "hot.framework.md")

MAX_STALE = 10
MAX_EVIDENCE = 3

DATE_RE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
HISTORY_RE = re.compile(
    r"\b(history|changelog|previously|formerly|was changed|used to)\b"
    "|\uc774\ub825|\ubcc0\uacbd\ub428|\uc608\uc804\uc5d0",  # ko: history / changed / before
    re.IGNORECASE)
PROFILE_RE = re.compile(
    r"\b(timezone|time zone|tz|asia/|europe/|america/|utc[+-]|"
    r"owner name|user name|my name|call me|uses the app|app:)"
    "|\uc774\ub984|\uc2dc\uac04\ub300|\ud638\uce6d",  # ko: name / timezone / form of address
    re.IGNORECASE)
NAME_STOP = {"dogany", "skill", "the", "and", "for", "mac", "agent"}
WORD_RE = re.compile(r"[a-z][a-z0-9]{3,}")
DESC_STOP = {
    "this", "that", "with", "from", "when", "what", "also", "only", "each",
    "into", "used", "uses", "user", "owner", "agent", "skill", "triggers",
    "korean", "english", "fires", "output", "does", "covers", "about",
}
DESC_OVERLAP = 0.30
# Canon review (DGN-2052): a custom line "says the same" as a canon line when
# at least CANON_OVERLAP of its content tokens appear in that canon line; lines
# with fewer than CANON_MIN_TOKENS tokens are too short to judge.
CANON_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{2,}|[\uac00-\ud7a3]{2,}")
CANON_STOP = {
    "the", "and", "for", "not", "never", "only", "any", "all", "with", "from",
    "this", "that", "into", "when", "each", "are", "was", "via", "you", "your",
    "user", "agent", "owner",
}
CANON_OVERLAP = 0.7
CANON_MIN_TOKENS = 4


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def instance_conf(root):
    conf = {}
    text = read(os.path.join(root, ".instance.conf")) or ""
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"')
    return conf


def resolve_template(root, explicit):
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    repo = instance_conf(root).get("DOGANY_REPO_ROOT", "")
    cand = os.path.join(repo, "agents", ".template") if repo else ""
    return cand if cand and os.path.isdir(cand) else None


def section(text, title):
    """Body of the "## <title>" section (up to the next "## ")."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == title.lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def workflow_key(heading):
    return re.sub(r"\s*\(.*?\)\s*", " ", heading).strip().lower()


def workflows(text):
    """Ordered [(key, heading)] of "### " headings under "## Workflows"."""
    body = strip_comments(section(text, "Workflows"))
    out = []
    for line in body.splitlines():
        if line.startswith("### "):
            h = line[4:].strip()
            out.append((workflow_key(h), h))
    return out


def check_missing_workflows(persona, template):
    have = {k for k, _ in workflows(persona)}
    props = []
    for key, heading in workflows(template):
        if key not in have:
            props.append({
                "kind": "missing_workflow",
                "summary": "Persona lacks the template workflow '%s'." % heading,
                "evidence": ["template: identity/hot.custom.agent.md ### %s" % heading],
                "apply": "add the template workflow block to the persona",
            })
    return props


def check_stale_history(persona, template):
    tmpl_lines = {l.strip() for l in template.splitlines()}
    hits = []
    for n, line in enumerate(strip_comments(persona).splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s in tmpl_lines:
            continue
        if DATE_RE.search(s) or HISTORY_RE.search(s):
            hits.append("line %d: %s" % (n, s[:160]))
    if not hits:
        return []
    return [{
        "kind": "stale_history",
        "summary": "Persona carries %d history line(s) read every session." % len(hits),
        "evidence": hits[:MAX_STALE],
        "apply": "drop the lines from the persona (memory keeps the history)",
    }]


def skill_meta(skill_dir):
    text = read(os.path.join(skill_dir, "SKILL.md")) or ""
    m = re.match(r"---\n(.*?)\n---", text, flags=re.DOTALL)
    desc = ""
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
    return desc


def name_tokens(name):
    return {t for t in re.split(r"[-_ ]+", name.lower()) if len(t) >= 3} - NAME_STOP


def desc_tokens(desc):
    return set(WORD_RE.findall(desc.lower())) - DESC_STOP


def check_duplicate_skills(root):
    sdir = os.path.join(root, SKILLS_REL)
    try:
        names = sorted(n for n in os.listdir(sdir)
                       if os.path.isfile(os.path.join(sdir, n, "SKILL.md")))
    except OSError:
        return []
    shipped = [n for n in names if n.startswith("dogany-")]
    custom = [n for n in names if not n.startswith("dogany-")]
    meta = {n: skill_meta(os.path.join(sdir, n)) for n in names}
    props = []
    for c in custom:
        ct, cd = name_tokens(c), desc_tokens(meta[c])
        for s in shipped:
            shared = ct & name_tokens(s)
            sd = desc_tokens(meta[s])
            overlap = len(cd & sd) / float(min(len(cd), len(sd))) if cd and sd else 0.0
            if shared or overlap >= DESC_OVERLAP:
                why = ("name token '%s'" % sorted(shared)[0]) if shared \
                    else ("description overlap %.0f%%" % (overlap * 100))
                props.append({
                    "kind": "duplicate_skill",
                    "summary": "Custom skill '%s' overlaps shipped skill '%s'." % (c, s),
                    "evidence": [why],
                    "apply": "retire the custom skill or merge it into the shipped one",
                })
    return props


def owner_has_facts(owner):
    for line in strip_comments(owner).splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return True
    return False


def memory_profile_hints(root):
    mdir = os.path.join(root, MEMORIES_REL)
    hints = []
    try:
        files = sorted(f for f in os.listdir(mdir) if f.endswith(".md"))
    except OSError:
        return hints
    for f in files:
        for n, line in enumerate((read(os.path.join(mdir, f)) or "").splitlines(), 1):
            if PROFILE_RE.search(line):
                hints.append("memories/%s:%d: %s" % (f, n, line.strip()[:160]))
                if len(hints) >= MAX_EVIDENCE:
                    return hints
    return hints


def check_owner_profile(root):
    owner = read(os.path.join(root, OWNER_REL))
    if owner is None or owner_has_facts(owner):
        return []
    hints = memory_profile_hints(root)
    if not hints:
        return []
    return [{
        "kind": "owner_profile_empty",
        "summary": "Owner profile is empty while memory holds profile facts.",
        "evidence": hints,
        "apply": "copy the confirmed facts into the owner profile (one line each, with date and source)",
    }]


def canon_tokens(line):
    toks = {t.strip("./-_") for t in CANON_TOKEN_RE.findall(line.lower())}
    return {t for t in toks if len(t) >= 2} - CANON_STOP


def rule_lines(text):
    """[(lineno, stripped)] of content lines: no blank, heading or comment."""
    out = []
    for n, line in enumerate(strip_comments(text).splitlines(), 1):
        s = line.strip()
        if s and not s.startswith("#") and s != "---":
            out.append((n, s))
    return out


def custom_layers(root, template):
    """[(label, rel, apply, [(lineno, line)])] -- the instance's custom layers."""
    layers = []
    text = read(os.path.join(root, CUSTOM_RULES_REL))
    if text:
        layers.append(("overlay", CUSTOM_RULES_REL,
                       "edit rules/hot.custom.md after the owner confirms the item",
                       rule_lines(text)))
    persona = read(os.path.join(root, PERSONA_REL))
    if persona:
        tmpl_lines = {l.strip() for l in (template or "").splitlines()}
        inside, lines = False, []
        for n, line in enumerate(strip_comments(persona).splitlines(), 1):
            if line.startswith("## "):
                inside = line[3:].strip().lower() == "workflows"
                continue
            s = line.strip()
            if inside and s and not s.startswith("#") and s not in tmpl_lines:
                lines.append((n, s))
        layers.append(("persona Workflows", PERSONA_REL,
                       "after the owner confirms, apply through the baseline-editor path",
                       lines))
    sdir = os.path.join(root, SKILLS_REL)
    try:
        names = sorted(n for n in os.listdir(sdir) if not n.startswith("dogany-")
                       and os.path.isfile(os.path.join(sdir, n, "SKILL.md")))
    except OSError:
        names = []
    for n in names:
        rel = os.path.join(SKILLS_REL, n, "SKILL.md")
        layers.append(("local skill '%s'" % n, rel,
                       "edit the local skill after the owner confirms the item",
                       rule_lines(read(os.path.join(root, rel)) or "")))
    return layers


def canon_match(toks, canon):
    """First canon line saying the same as a custom line with tokens toks."""
    for line, ctoks in canon:
        if len(toks & ctoks) >= CANON_OVERLAP * len(toks):
            return line
    return None


def check_canon_refactor(root, template):
    """DGN-2052: custom-layer lines the updated canon covers or retired.

    Returns (proposals, reviewed): reviewed is False when there is no
    pre-update canon snapshot to diff against."""
    old = read(os.path.join(root, CANON_PREV_REL))
    new = read(os.path.join(root, CANON_REL))
    if old is None or new is None:
        return [], False
    old_l = {s for _, s in rule_lines(old)}
    new_l = {s for _, s in rule_lines(new)}
    added = [(s, canon_tokens(s)) for s in sorted(new_l - old_l)]
    removed = [(s, canon_tokens(s)) for s in sorted(old_l - new_l)]
    current = [(s, canon_tokens(s)) for s in sorted(new_l)]
    props = []
    for label, rel, apply, lines in custom_layers(root, template):
        covered, retired = [], []
        for n, s in lines:
            toks = canon_tokens(s)
            if len(toks) < CANON_MIN_TOKENS:
                continue
            hit = canon_match(toks, added)
            if hit is not None:
                covered.append("%s:%d: %s  <- canon now: %s" % (rel, n, s[:120], hit[:120]))
                continue
            if canon_match(toks, current) is None:
                hit = canon_match(toks, removed)
                if hit is not None:
                    retired.append("%s:%d: %s  <- canon removed: %s" % (rel, n, s[:120], hit[:120]))
        if covered:
            props.append({
                "kind": "canon_covered",
                "summary": "The update's canon now covers %d line(s) in the %s." % (len(covered), label),
                "evidence": covered[:MAX_STALE],
                "apply": "drop the lines; " + apply,
            })
        if retired:
            props.append({
                "kind": "canon_retired",
                "summary": "%d line(s) in the %s echo canon the update replaced." % (len(retired), label),
                "evidence": retired[:MAX_STALE],
                "apply": "rewrite the lines against the new canon (or drop them); " + apply,
            })
    return props, True


def audit(root, template_dir):
    notes = []
    persona = read(os.path.join(root, PERSONA_REL))
    template = read(os.path.join(template_dir, PERSONA_REL)) if template_dir else None
    props = []
    if persona is None:
        notes.append("persona file missing")
    elif template is None:
        notes.append("template not resolved: workflow and history checks skipped")
    else:
        props += check_missing_workflows(persona, template)
        props += check_stale_history(persona, template)
    props += check_duplicate_skills(root)
    props += check_owner_profile(root)
    canon_props, reviewed = check_canon_refactor(root, template)
    props += canon_props
    if reviewed and not canon_props:
        # Review complete: nothing in the custom layers overlaps the canon
        # change, so the pre-update snapshot has served its purpose.
        try:
            os.remove(os.path.join(root, CANON_PREV_REL))
            notes.append("canon review: no overlap, snapshot consumed")
        except OSError:
            pass
    for i, p in enumerate(props, 1):
        p["id"] = i
    return props, notes


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".persona-audit.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def render(props):
    """DRAFT owner message (copy not yet owner-approved)."""
    if not props:
        return ""
    lines = ["[DRAFT] Baseline check found %d item(s) to tidy:" % len(props)]
    for p in props:
        lines.append("%d. %s" % (p["id"], p["summary"]))
    lines.append("Reply 'all' to apply every item, or pick the numbers.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="instance root")
    ap.add_argument("--template", help="template dir (default: DOGANY_REPO_ROOT/agents/.template)")
    ap.add_argument("--out", help="output json (default: <root>/%s)" % OUT_REL)
    ap.add_argument("--render", action="store_true", help="print the DRAFT message text")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print("persona-audit: root missing: %s" % root, file=sys.stderr)
        return 2
    tdir = resolve_template(root, a.template)
    props, notes = audit(root, tdir)
    out = a.out or os.path.join(root, OUT_REL)
    write_atomic(out, {
        "version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "template": tdir or "",
        "notes": notes,
        "proposals": props,
    })
    if a.render:
        text = render(props)
        if text:
            print(text)
        return 0
    kinds = {}
    for p in props:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    detail = " ".join("%s=%d" % kv for kv in sorted(kinds.items()))
    print("persona-audit: proposals=%d %s%s" % (
        len(props), detail, (" note=" + "; ".join(notes)) if notes else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
