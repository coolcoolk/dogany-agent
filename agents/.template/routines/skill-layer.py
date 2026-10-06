#!/usr/bin/env python3
"""skill-layer.py -- the local-layer verb (DGN-1526 R4 + R5).

Two layers, named (lockstep with scripts/pack/pack_install.sh "the two
layers, named"):

  shipped  bytes a pack or the framework delivers and owns. Installs and
           framework updates overwrite them.
  local    owner-made bytes no installer overwrites. Registered as an
           UNTAGGED line in <root>/.claude/.dogany-preserve. update.sh has
           always honored every preserve line; contract-class pack installs
           honor untagged lines since DGN-1526 R2. Pack-tagged lines
           ('# pack:<id>') are shipped-layer bookkeeping, not holds.

WHY A VERB (R5): the machinery (preserve list, content-drift --preserve,
install ledger) only reaches the user if an agent can register a path
without hand-editing a dotfile. This is that entry point.

Subcommands:
  register PATH     add PATH to the local layer (idempotent: an existing
                    covering entry = no-op). A directory gets a trailing '/'
                    entry. A skill dir must declare `layer: local` in its
                    SKILL.md frontmatter first -- the author picks the layer,
                    this verb records it.
  unregister PATH   remove PATH's own untagged entry (idempotent).
  list              print the local layer (and the shipped bookkeeping count).
  lint              R4: fail when a skill has no layer choice, or when the
                    choice contradicts scope / registration.
                      --skill DIR  one skill (the check the creator runs)
                      --all        every owner-made skill in the instance
  --dry-run         (register / unregister) print what would change, write
                    nothing.

Exit: 0 ok / no-op, 1 lint findings, 2 refused (bad path, contradiction,
usage). Writes are atomic (temp file + rename in the same dir).
"""
import argparse
import os
import re
import sys
import tempfile

PRESERVE_REL = ".claude/.dogany-preserve"
LAYERS = ("shipped", "local")
LOCAL_TAG = "# layer:local (routines/skill-layer.py register)"
PRESERVE_HEADER = (
    "# .dogany-preserve -- instance-local files update.sh must NOT refresh.\n"
    "# One instance-root-relative path per line (trailing '/' = directory).\n"
    "# Lines tagged '# pack:<pack-id>' are managed by pack_install.sh.\n"
)
PACK_TAG_RE = re.compile(r"#\s*pack(:|-owned:)")


def default_root():
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.dirname(here)
    if os.path.isdir(os.path.join(cand, ".claude")):
        return cand
    return os.getcwd()


def parse_entry(raw):
    """(entry, is_pack_tagged) for one preserve line; entry '' = no entry.
    Parsing parity with update.sh PRESERVE_ENTRIES / pack_install.sh
    _local_layer_load."""
    tagged = bool(PACK_TAG_RE.search(raw))
    line = raw.split("#", 1)[0].strip()
    if line.startswith("./"):
        line = line[2:]
    line = line.lstrip("/")
    if ".." in line:
        return "", tagged
    return line, tagged


def read_preserve(root):
    path = os.path.join(root, PRESERVE_REL)
    if not os.path.isfile(path):
        return None, []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text, text.splitlines()


def local_entries(lines):
    out = []
    for raw in lines:
        e, tagged = parse_entry(raw)
        if e and not tagged:
            out.append(e)
    return out


def covering(entries, rel, is_dir):
    """First local entry that already holds rel (or rel/ for a dir)."""
    # A dir is held only by a trailing-'/' entry (update.sh is_preserved:
    # a bare 'dir' line matches the exact path, never the files inside).
    target = rel + "/" if is_dir else rel
    for e in entries:
        if e == target:
            return e
        if e.endswith("/") and target.startswith(e):
            return e
    return None


def to_rel(root, path):
    """Instance-root-relative, lexical (preserve entries are lexical)."""
    ab = path if os.path.isabs(path) else os.path.join(root, path)
    ab = os.path.normpath(ab)
    rroot = os.path.normpath(root)
    if ab == rroot or not ab.startswith(rroot + os.sep):
        return None
    return os.path.relpath(ab, rroot)


def frontmatter(skill_md):
    """{key: value} of the leading '---' block; {} when absent."""
    try:
        with open(skill_md, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return {}
    fm = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", ln)
        if m:
            fm[m.group(1)] = m.group(2).strip("'\"")
    return fm


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dogany-preserve.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def legacy_ledger_owners(root, rel):
    """Legacy-path packs whose install ledger lists rel (or a path under it).
    The legacy install path does not read the local layer (R2 is
    contract-class only), so a reinstall of these packs still overwrites."""
    owners = []
    d = os.path.join(root, "config", "packs")
    if not os.path.isdir(d):
        return owners
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".files"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        cls = ""
        hit = False
        for ln in lines:
            if ln.startswith("# class:"):
                cls = ln.split(":", 1)[1].strip()
            elif ln and not ln.startswith("#"):
                if ln == rel or ln.startswith(rel.rstrip("/") + "/"):
                    hit = True
        if hit and cls in ("", "legacy"):
            owners.append(fn[: -len(".files")])
    return owners


def cmd_register(args, root):
    rel = to_rel(root, args.path)
    if rel is None:
        print("skill-layer: refused: %s is not inside the instance root %s" % (args.path, root))
        return 2
    if rel == PRESERVE_REL or rel.split(os.sep)[0] == ".git":
        print("skill-layer: refused: %s cannot be a local-layer entry" % rel)
        return 2
    ab = os.path.join(root, rel)
    if not os.path.lexists(ab):
        print("skill-layer: refused: %s does not exist (register what is on disk)" % rel)
        return 2
    if os.path.islink(ab):
        print("skill-layer: refused: %s is a symlink (shipped-layer wiring) -- register its "
              "target instead: %s" % (rel, os.path.realpath(ab)))
        return 2
    is_dir = os.path.isdir(ab)
    skill_md = os.path.join(ab, "SKILL.md") if is_dir else (ab if os.path.basename(ab) == "SKILL.md" else None)
    if skill_md and os.path.isfile(skill_md):
        fm = frontmatter(skill_md) or {}
        layer = fm.get("layer", "")
        if layer != "local":
            print("skill-layer: refused: %s declares %s -- declare `layer: local` in its SKILL.md "
                  "frontmatter first (the author picks the layer; this verb records it)"
                  % (os.path.relpath(skill_md, root),
                     "layer: %s" % layer if layer else "no layer"))
            return 2
    text, lines = read_preserve(root)
    entries = local_entries(lines)
    cov = covering(entries, rel, is_dir)
    entry = rel + "/" if is_dir else rel
    if cov is not None:
        print("skill-layer: no-op: %s already in the local layer (covered by '%s')" % (entry, cov))
        return 0
    tagged_same = [raw for raw in lines if parse_entry(raw) == (entry, True)]
    new_line = "%s  %s\n" % (entry, LOCAL_TAG)
    if text is None:
        new_text = PRESERVE_HEADER + new_line
    else:
        new_text = text + ("" if text.endswith("\n") or not text else "\n") + new_line
    for owner in legacy_ledger_owners(root, entry):
        print("skill-layer: WARN: legacy-path pack '%s' installed %s -- its reinstall does not read "
              "the local layer and will still overwrite (update.sh and contract-class installs hold it)"
              % (owner, entry))
    if tagged_same:
        print("skill-layer: note: %s also has a pack-tagged (shipped bookkeeping) line; the untagged "
              "entry makes it local" % entry)
    if args.dry_run:
        print("skill-layer: dry-run: would %s %s: %s"
              % ("create" if text is None else "append to", PRESERVE_REL, new_line.rstrip("\n")))
        return 0
    atomic_write(os.path.join(root, PRESERVE_REL), new_text)
    print("skill-layer: registered local: %s (%s)" % (entry, PRESERVE_REL))
    return 0


def cmd_unregister(args, root):
    rel = to_rel(root, args.path)
    if rel is None:
        print("skill-layer: refused: %s is not inside the instance root %s" % (args.path, root))
        return 2
    text, lines = read_preserve(root)
    targets = {rel, rel + "/"}
    keep, gone = [], []
    for raw in lines:
        e, tagged = parse_entry(raw)
        if e in targets and not tagged:
            gone.append(raw)
        else:
            keep.append(raw)
    if not gone:
        print("skill-layer: no-op: %s has no local-layer entry of its own" % rel)
        return 0
    if args.dry_run:
        for g in gone:
            print("skill-layer: dry-run: would remove line: %s" % g)
        return 0
    atomic_write(os.path.join(root, PRESERVE_REL), "\n".join(keep) + "\n")
    for g in gone:
        print("skill-layer: unregistered: %s" % g)
    return 0


def cmd_list(args, root):
    text, lines = read_preserve(root)
    if text is None:
        print("skill-layer: no %s -- local layer empty" % PRESERVE_REL)
        return 0
    n_tagged = 0
    for raw in lines:
        e, tagged = parse_entry(raw)
        if not e:
            continue
        if tagged:
            n_tagged += 1
            continue
        mark = "" if os.path.lexists(os.path.join(root, e.rstrip("/"))) else "  [not on disk]"
        print("local  %s%s" % (e, mark))
    print("shipped-bookkeeping (pack-tagged) lines: %d" % n_tagged)
    return 0


def lint_skill(root, skill_dir, entries):
    """Findings for one skill dir (empty list = clean)."""
    out = []
    md = os.path.join(skill_dir, "SKILL.md")
    fm = frontmatter(md)
    label = to_rel(root, skill_dir) or skill_dir
    if fm is None:
        return ["%s: no SKILL.md" % label]
    layer = fm.get("layer", "")
    if not layer:
        out.append("%s: no `layer:` in SKILL.md frontmatter -- pick one: shipped | local "
                   "(DGN-1526 R4)" % label)
        return out
    if layer not in LAYERS:
        out.append("%s: layer '%s' is not one of: %s" % (label, layer, " | ".join(LAYERS)))
        return out
    scope = fm.get("scope", "")
    if scope == "framework" and layer != "shipped":
        out.append("%s: scope framework ships with the product -- layer must be shipped, not %s"
                   % (label, layer))
    if scope in ("local", "shared") and layer != "local":
        out.append("%s: scope %s is owner-made -- layer must be local, not %s" % (label, scope, layer))
    real = os.path.realpath(skill_dir)
    rroot = os.path.realpath(root)
    if real.startswith(rroot + os.sep):
        rel = os.path.relpath(real, rroot)
        held = covering(entries, rel, True) or covering(entries, os.path.join(rel, "SKILL.md"), False)
        if layer == "local" and held is None:
            out.append("%s: layer local but not registered -- run: routines/skill-layer.py register %s"
                       % (label, rel))
        if layer == "shipped" and held is not None:
            out.append("%s: layer shipped but held by local entry '%s' -- the two contradict"
                       % (label, held))
    return out


def owner_made_skills(root, entries):
    """Real (non-symlink) dirs in .claude/skills, plus skills-bundle dirs the
    owner holds. Symlinked skills are shipped wiring (packs / framework)."""
    found = []
    sk = os.path.join(root, ".claude", "skills")
    if os.path.isdir(sk):
        for n in sorted(os.listdir(sk)):
            p = os.path.join(sk, n)
            if os.path.isdir(p) and not os.path.islink(p):
                found.append(p)
    sb = os.path.join(root, ".claude", "skills-bundle")
    if os.path.isdir(sb):
        for n in sorted(os.listdir(sb)):
            p = os.path.join(sb, n)
            rel = os.path.join(".claude", "skills-bundle", n)
            if os.path.isdir(p) and (covering(entries, rel, True)
                                     or covering(entries, os.path.join(rel, "SKILL.md"), False)):
                found.append(p)
    return found


def cmd_lint(args, root):
    _text, lines = read_preserve(root)
    entries = local_entries(lines)
    if args.skill:
        targets = [os.path.abspath(args.skill)]
    else:
        targets = owner_made_skills(root, entries)
    findings = []
    for t in targets:
        findings.extend(lint_skill(root, t, entries))
    for f in findings:
        print("skill-layer lint: FAIL %s" % f)
    print("skill-layer lint: %d skill(s) checked, %d finding(s)" % (len(targets), len(findings)))
    return 1 if findings else 0


def main(argv):
    ap = argparse.ArgumentParser(
        prog="skill-layer.py",
        description="Register owner-made files in the local layer (never overwritten by "
                    "installs/updates) and lint each skill's layer choice (shipped | local).")
    ap.add_argument("--root", default=None, help="instance root (default: this script's instance)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("register", help="add PATH to the local layer (idempotent)")
    p.add_argument("path")
    p.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    p = sub.add_parser("unregister", help="remove PATH's own local-layer entry (idempotent)")
    p.add_argument("path")
    p.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    sub.add_parser("list", help="print the local layer")
    p = sub.add_parser("lint", help="fail when a skill's layer choice is missing or contradicts")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill", help="one skill dir")
    g.add_argument("--all", action="store_true", help="every owner-made skill in the instance")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    root = os.path.abspath(args.root) if args.root else default_root()
    if not os.path.isdir(root):
        print("skill-layer: refused: instance root %s not found" % root)
        return 2
    return {"register": cmd_register, "unregister": cmd_unregister,
            "list": cmd_list, "lint": cmd_lint}[args.cmd](args, root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
