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
import errno
import os
import re
import sys
import tempfile
import time
from pathlib import Path

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
    # The one routines-side atomic writer (why_net.store, DGN-1967 step 4);
    # private: the file stays 0600, as mkstemp made it before.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "lib"))
    from why_net import store
    store.atomic_write(path, text, private=True)


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
    elif layer == "local" and scope == "local":
        # DGN-1910 F6: the registration check above only runs under --root, so
        # a wrong root used to skip it silently. A local skill belongs to one
        # instance; pass that instance as --root. scope: shared stays legal
        # here -- the shared tier lives outside every instance root.
        out.append("%s: local skill outside the instance root %s -- run the lint with "
                   "--root <the instance that owns it>" % (label, rroot))
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


# FDGN-579: the update plan is a declared, exact transformation, not a
# semantic similarity verdict. One implementation serves hooks and the CLI.
def content_hash(text):
    import hashlib
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def normalized_instruction(text):
    text = re.sub(r'(?m)^\s*(?:[-*+] |\d+[.)] )', '', text)
    return re.sub(r'\s+', ' ', re.sub(r'[*_`#]', '', text)).strip().casefold()


def instruction_blocks(text):
    body = text[len(_frontmatter_text(text)):]
    body = re.sub(r'(?m)^#{1,6} [^\n]*$', '', body)
    return [normalized_instruction(block)
            for block in re.split(r'\n\s*\n|\n(?=\s*(?:[-*+] |\d+[.)] ))', body)
            if normalized_instruction(block)]


def _section(text, heading):
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        if line.strip() == heading:
            starts.append(offset)
        offset += len(line)
    if len(starts) != 1 or not re.match(r'^#{1,6} ', heading):
        raise ValueError('ambiguous-section')
    start = starts[0]
    level = len(heading.split(' ', 1)[0])
    match = re.search(r'(?m)^#{1,%d} ' % level, text[start + len(heading):])
    end = start + len(heading) + match.start() if match else len(text)
    return start, end


def _frontmatter_text(text):
    match = re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)', text, re.S)
    return match.group() if match else ''


def check_update(plan, candidate, disk_before=None):
    """Return stable diagnostics; never infer an undeclared semantic ruling."""
    required = ('target', 'before', 'before_hash', 'session', 'turn',
                'authorization', 'scope', 'changes')
    if not isinstance(plan, dict) or any(not plan.get(k) for k in required):
        return ['invalid-plan']
    before = plan['before']
    if content_hash(before) != plan['before_hash']:
        return ['baseline-hash-mismatch']
    if disk_before is not None and disk_before != before:
        return ['stale-baseline']
    if candidate == before:
        return ['no-change']
    if _frontmatter_text(candidate) != _frontmatter_text(before):
        return ['frontmatter-changed']
    expected = before
    try:
        for change in plan['changes']:
            section = change['section']
            start, end = _section(expected, section)
            old, new = change.get('old', ''), change['new']
            if old:
                if expected.count(old) != 1 or old not in expected[start:end]:
                    return ['ambiguous-anchor']
                expected = expected[:start] + expected[start:end].replace(old, new, 1) + expected[end:]
                if old != new and not change.get('move') and normalized_instruction(old) in instruction_blocks(candidate):
                    return ['superseded-instruction-retained']
            else:
                if not change.get('reviewed_no_existing_home') or not change.get('reason'):
                    return ['new-topic-needs-reviewed-home']
                heading = re.match(r'#{1,6} .+', new)
                if not heading or heading.group() in before:
                    return ['new-topic-already-has-home']
                expected = expected[:end] + new + expected[end:]
            if change.get('move'):
                dest = change['move']['section']
                refs = change['move'].get('reference_repairs')
                if not refs:
                    return ['move-needs-reference-repairs']
                a, b = _section(expected, dest)
                expected = expected[:b] + change['move']['instruction'] + expected[b:]
                for repair in refs:
                    if expected.count(repair['old']) != 1:
                        return ['ambiguous-reference']
                    expected = expected.replace(repair['old'], repair['new'], 1)
                    if repair['old'] in candidate:
                        return ['stale-reference']
        for behavior in plan.get('behaviors', []):
            if not isinstance(behavior, dict) or not behavior.get('statement') or not any(
                    behavior.get('section') == c.get('section') and
                    behavior.get('instruction') == c.get('new') and c.get('new')
                    for c in plan['changes']):
                return ['unmapped-behavior']
        if expected != candidate:
            return ['unmapped-change']
        # Presentation normalization is shared by retention and duplicate checks.
        blocks = instruction_blocks(candidate)
        if len(blocks) != len(set(blocks)):
            return ['duplicate-instruction']
    except (KeyError, TypeError, ValueError):
        return ['invalid-mapping']
    return []


def update_context(data):
    """Bind hooks and CLI checks to the same genuine owner prompt."""
    import json
    session = str(data.get('session_id') or data.get('thread_id') or '')
    turn = str(data.get('prompt_id') or data.get('turn_id') or '')
    explicit_turn = bool(turn)
    if not turn or not session:
        try:
            with open(data.get('transcript_path') or '', encoding='utf-8') as fh:
                for line in fh:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
                    # Native Codex JSONL, as well as Claude's user records.
                    if row.get('type') == 'session_meta' and not session:
                        session = str(payload.get('id') or '')
                    if not explicit_turn and (row.get('type') == 'turn_context' or (row.get('type') == 'event_msg' and payload.get('type') == 'task_started')):
                        turn = str(payload.get('turn_id') or turn)
                    if row.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user':
                        if not turn:
                            turn = content_hash(json.dumps(row, sort_keys=True))
                    if row.get('type') != 'user'  or row.get('isMeta') or row.get('isSidechain'):
                        continue
                    msg = row.get('message') or {}
                    c = msg.get('content')
                    if isinstance(c, list) and any(b.get('type') == 'tool_result' for b in c if isinstance(b, dict)):
                        continue
                    if not explicit_turn and (isinstance(c, str) or isinstance(c, list) and any(b.get('type') == 'text' for b in c if isinstance(b, dict))):
                        turn = str(row.get('uuid') or msg.get('id') or content_hash(json.dumps(row, sort_keys=True)))
        except (OSError, ValueError, TypeError):
            pass
    return session, turn


def update_plan_path(root, target, session, turn):
    key = content_hash('\n'.join((os.path.realpath(target), session, turn)))
    return os.path.join(root, '.claude', 'skill-updates', key + '.json')


def load_update_plan(root, target, session, turn):
    import json
    path = update_plan_path(root, target, session, turn)
    with open(path, encoding='utf-8') as fh:
        plan = json.load(fh)
    if (plan['target'], plan['session'], plan['turn']) != (os.path.realpath(target), session, turn):
        raise ValueError('context-mismatch')
    return plan


def registered_pack_source(target):
    """True when target is below a source repo in the pack registry."""
    shared = os.environ.get('DOGANY_SHARED_HOME', os.path.join(os.path.expanduser('~'), '.dogany'))
    registry = os.environ.get('DOGANY_PACK_SOURCES', os.path.join(shared, 'pack-sources.conf'))
    try:
        with open(registry, encoding='utf-8') as fh:
            rows = fh.read().splitlines()
    except OSError:
        return False
    target = os.path.realpath(target)
    for row in rows:
        if not row or row.startswith('#') or '=' not in row:
            continue
        key, value = row.split('=', 1)
        if not key.endswith('.repo') or not value:
            continue
        source = os.path.realpath(os.path.expanduser(value))
        if target.startswith(source + os.sep):
            return True
    return False


def begin_update(root, target, mapping, session, turn, authorization, scope):
    import json
    target = os.path.realpath(target)
    skill = os.path.basename(target) == 'SKILL.md'
    local_rule = target == os.path.join(os.path.realpath(root), 'rules', 'hot.custom.md') and scope == 'local'
    if not (skill or local_rule) or scope not in ('local', 'shared', 'framework'):
        raise ValueError('invalid-target-or-scope')
    if not session or not turn or not authorization:
        raise ValueError('missing-context-or-authorization')
    with open(target, encoding='utf-8') as fh:
        before = fh.read()
    source_skill = skill and scope == 'shared' and registered_pack_source(target)
    if skill and not source_skill and ('scope: ' + scope) not in _frontmatter_text(before):
        raise ValueError('scope-mismatch')
    plan = dict(mapping, target=target, before=before, before_hash=content_hash(before),
                session=session, turn=turn, authorization=authorization, scope=scope)
    path = update_plan_path(root, target, session, turn)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Exclusive creation: retries cannot silently replace the captured baseline.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as fh:
        json.dump(plan, fh, ensure_ascii=False, sort_keys=True)
    return path


def verify_update(root, target, session, turn, candidate=None, applied=False):
    import json
    plan = load_update_plan(root, target, session, turn)
    with open(plan['target'], encoding='utf-8') as fh:
        disk = fh.read()
    text = disk if candidate is None else candidate
    bad = check_update(plan, text, None if applied else disk)
    if applied and candidate is not None and text != disk:
        bad.append('disk-content-mismatch')
    if not bad and os.path.basename(plan['target']) == 'SKILL.md':
        entries = local_entries(read_preserve(root)[1])
        bad += lint_skill(root, os.path.dirname(plan['target']), entries)
    evidence = dict(target=plan['target'], scope=plan['scope'], session=session, turn=turn,
                    before_hash=plan['before_hash'], after_hash=content_hash(text),
                    plan_hash=content_hash(json.dumps(plan, ensure_ascii=False, sort_keys=True)),
                    behaviors=plan.get('behaviors', []), applied=applied)
    if applied and not bad:
        atomic_write(update_plan_path(root, target, session, turn) + '.receipt', json.dumps(evidence))
    return bad, evidence


def persisted_behaviors(root, session, turn):
    """Only applied, still-current, authorized plans for this exact turn."""
    import glob
    import json
    out = []
    if not session or not turn:
        return out
    for path in glob.glob(os.path.join(root, '.claude', 'skill-updates', '*.json.receipt')):
        try:
            receipt = json.loads(Path(path).read_text(encoding='utf-8'))
            if (receipt.get('session'), receipt.get('turn')) != (session, turn):
                continue
            plan = load_update_plan(root, receipt['target'], session, turn)
            text = Path(receipt['target']).read_text(encoding='utf-8')
            if check_update(plan, text) or content_hash(text) != receipt['after_hash']:
                continue
            if content_hash(json.dumps(plan, ensure_ascii=False, sort_keys=True)) != receipt['plan_hash']:
                continue
            if os.path.basename(receipt['target']) == 'SKILL.md' and lint_skill(root, os.path.dirname(receipt['target']), local_entries(read_preserve(root)[1])):
                continue
            # A framework checkout awaiting install must not claim instance-wide behavior.
            if receipt['scope'] == 'framework':
                continue
            out.extend(b['statement'] for b in receipt.get('behaviors', []))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def turn_snapshot_paths(root, session, turn):
    """Stable locations shared by capture and audit in separate hook processes."""
    name = content_hash(session + '\n' + turn) + '.snapshot'
    namespace = content_hash(os.path.realpath(root))
    worktree = os.environ.get('DOGANY_DISPATCH_WORKTREE')
    # Dispatch workers without a writable worktree are observe jobs.  Their
    # instance is sealed by seatbelt, while TMPDIR is an explicit writable
    # carve-out.  Do not make the PreToolUse hook probe the sealed instance.
    if os.environ.get('DOGANY_DISPATCH_WORKER') and not worktree:
        try:
            tmp = tempfile.gettempdir()
        except OSError:
            return  # no usable temporary directory; capture will record the miss
        yield os.path.join(tmp, 'dogany-skill-updates-%s' % os.getuid(), namespace, name)
        return
    yield os.path.join(root, '.claude', 'skill-updates', name)
    # Include the instruction root: one worker can visit several workspaces in
    # the same session/turn. Never borrow another workspace's before hashes.
    if worktree:
        yield os.path.join(worktree, '.claude', 'skill-updates', 'snapshots', namespace, name)
    try:
        tmp = tempfile.gettempdir()
    except OSError:
        return  # no usable temporary directory; capture will record the miss
    yield os.path.join(tmp, 'dogany-skill-updates-%s' % os.getuid(), namespace, name)


def turn_snapshot_path(root, session, turn):
    paths = turn_snapshot_paths(root, session, turn)
    primary = next(paths)
    if os.path.isfile(primary):
        return primary
    return next((path for path in paths if os.path.isfile(path)), primary)


def prune_turn_snapshots(directory, clock=None):
    """Keep the intersection of the newest 200 and the last two days.

    Cleanup is best effort: another hook can remove a file or permissions can
    change after capture. Neither should invalidate an already saved snapshot.
    """
    cutoff = (clock or time.time)() - 2 * 24 * 60 * 60
    snapshots = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.name.endswith('.snapshot') and entry.is_file(follow_symlinks=False):
                        snapshots.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
                except OSError:
                    continue
    except OSError:
        return
    for index, (modified, path) in enumerate(sorted(snapshots, reverse=True)):
        if index >= 200 or modified < cutoff:
            try:
                os.unlink(path)
            except OSError:
                pass


def capture_update_turn(root, session, turn, clock=None):
    """Capture existing instruction homes once, before any tool can write.

    Follow mounted skill directories, deduplicating real directories to avoid
    symlink cycles. Hashes suffice: the u1 plan still owns full before text.
    Return False and log one line if none of the snapshot locations is writable.
    """
    import json
    if not session or not turn:
        raise ValueError('missing-turn-context')
    path = turn_snapshot_path(root, session, turn)
    if os.path.isfile(path):
        return
    targets = {os.path.realpath(os.path.join(root, 'rules', 'hot.custom.md'))}
    seen = set()
    for rel in ('.claude/skills', '.agents/skills', 'skills'):
        for base, dirs, files in os.walk(os.path.join(root, rel), followlinks=True):
            real = os.path.realpath(base)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
            if 'SKILL.md' in files:
                targets.add(os.path.realpath(os.path.join(base, 'SKILL.md')))
    hashes = {p: content_hash(Path(p).read_text(encoding='utf-8'))
              for p in sorted(targets) if os.path.isfile(p)}
    misses = []
    for index, path in enumerate(turn_snapshot_paths(root, session, turn)):
        try:
            os.makedirs(os.path.dirname(path), mode=0o700 if index else 0o777, exist_ok=True)
            try:
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                               'w', encoding='utf-8') as fh:
                    json.dump(dict(session=session, turn=turn, hashes=hashes), fh)
            except FileExistsError:
                return  # another hook already captured this same turn
        except OSError as exc:
            if index == 0 and not (isinstance(exc, PermissionError) or
                                      exc.errno in (errno.EPERM, errno.EACCES, errno.EROFS)):
                raise  # preserve normal-path diagnostics for unrelated failures
            misses.append('%s: %s' % (path, exc))
            continue
        prune_turn_snapshots(os.path.dirname(path), clock=clock)
        return
    print('skill-layer: instruction snapshot skipped: ' + '; '.join(misses), file=sys.stderr)
    return False


def audit_update_turn(root, session, turn):
    import json
    path = turn_snapshot_path(root, session, turn)
    if not os.path.isfile(path):
        return []  # older/uninstrumented sessions retain their existing path
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
        if (snapshot['session'], snapshot['turn']) != (session, turn):
            return ['snapshot-context-mismatch']
        bad = []
        for target, before_hash in snapshot['hashes'].items():
            if not os.path.isfile(target):
                bad.append('unplanned-instruction-deletion')
                continue
            disk = Path(target).read_text(encoding='utf-8')
            if content_hash(disk) == before_hash:
                continue
            try:
                plan = load_update_plan(root, target, session, turn)
                if plan['before_hash'] != before_hash:
                    bad.append('plan-after-unplanned-write')
            except (OSError, ValueError, KeyError, TypeError):
                bad.append('unplanned-instruction-write')
        return bad
    except (OSError, ValueError, KeyError, TypeError):
        return ['invalid-turn-snapshot']


def audit_updates(root, session=None, turn=None, target=None):
    import glob
    import json
    plans = []
    for path in glob.glob(os.path.join(root, '.claude', 'skill-updates', '*.json')):
        try:
            plan = json.loads(Path(path).read_text(encoding='utf-8'))
            if target and plan.get('target') != os.path.realpath(target):
                continue
            if session is not None and (plan.get('session'), plan.get('turn')) != (session, turn):
                continue
            plans.append((os.path.getmtime(path), plan, path))
        except (OSError, ValueError, TypeError):
            return ['invalid-update-plan']
    # s8 without a context checks the newest plan for this target; Stop
    # checks every plan in the exact current turn (including shell writes).
    if target and session is None:
        plans = sorted(plans, key=lambda item: item[0])[-1:]
    bad = audit_update_turn(root, session, turn) if session and turn else []
    for _, plan, path in plans:
        try:
            disk = Path(plan['target']).read_text(encoding='utf-8')
            if disk == plan['before']:
                continue  # abandoned proposal, no persisted change claimed
            bad.extend(check_update(plan, disk))
            receipt = json.loads(Path(path + '.receipt').read_text(encoding='utf-8'))
            if receipt.get('after_hash') != content_hash(disk):
                bad.append('stale-applied-evidence')
        except (OSError, ValueError, KeyError, TypeError):
            bad.append('missing-applied-evidence')
    return bad


def cmd_update(args, root):
    import json
    try:
        if args.cmd == 'update-begin':
            with open(args.mapping, encoding='utf-8') as fh:
                mapping = json.load(fh)
            path = begin_update(root, args.target, mapping, args.session, args.turn, args.authorization, args.scope)
            print(json.dumps({'ok': True, 'plan': path}))
            return 0
        candidate = None
        if args.candidate:
            with open(args.candidate, encoding='utf-8') as fh:
                candidate = fh.read()
        bad, evidence = verify_update(root, args.target, args.session, args.turn, candidate, args.applied)
        print(json.dumps({'ok': not bad, 'diagnostics': bad, 'evidence': evidence if not bad else None}))
        if any(d in ('invalid-plan', 'invalid-mapping') for d in bad):
            return 2
        return 1 if bad else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'ok': False, 'diagnostics': ['invalid-input'], 'detail': str(exc)}))
        return 2


def main(argv):
    ap = argparse.ArgumentParser(
        prog="skill-layer.py",
        description="Register owner-made files and copied shipped-skill overrides in the local layer "
                    "(never overwritten by installs/updates), and lint each skill's layer choice "
                    "(shipped | local). Copy a shipped skill, set layer: local, then register the copy; "
                    "never register or edit shipped source in place.")
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
    p = sub.add_parser('update-audit', help='recheck captured changes on actual disk')
    p.add_argument('--target', required=True)
    for verb in ('update-begin', 'update-check'):
        p = sub.add_parser(verb, help='FDGN-579 exact skill update contract')
        p.add_argument('--target', required=True)
        p.add_argument('--session', required=True)
        p.add_argument('--turn', required=True)
        if verb == 'update-begin':
            p.add_argument('--mapping', required=True)
            p.add_argument('--authorization', required=True)
            p.add_argument('--scope', required=True, choices=('local', 'shared', 'framework'))
        else:
            p.add_argument('--candidate')
            p.add_argument('--applied', action='store_true')
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    root = os.path.abspath(args.root) if args.root else default_root()
    if not os.path.isdir(root):
        print("skill-layer: refused: instance root %s not found" % root)
        return 2
    if args.cmd == 'update-audit':
        bad = audit_updates(root, target=args.target)
        print('skill update audit: ' + (', '.join(bad) if bad else 'clean'))
        return 1 if bad else 0
    if args.cmd.startswith('update-'):
        return cmd_update(args, root)
    return {"register": cmd_register, "unregister": cmd_unregister,
            "list": cmd_list, "lint": cmd_lint}[args.cmd](args, root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
