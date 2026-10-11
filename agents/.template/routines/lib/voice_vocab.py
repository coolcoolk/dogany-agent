#!/usr/bin/env python3
"""voice_vocab.py -- DERIVED voice vocabulary for speech recognition (DGN-1694).

Owner ruling 2026-09-24 16:43-16:50: the user never maintains a word list.
This module is a READER over name owners the estate already has; it keeps
no list of its own beyond the framework's proper nouns.

Tier 0 (always in, fixed order):
  framework proper nouns (FRAMEWORK_TERMS below), model names from
  dispatch-model-display.tsv, this instance's agent display name + owner
  term, then the other estate agents' display names.
Tier 1 (ranked by last_used desc, then cut by the consumer's budget):
  installed skill display_name frontmatter (last_used = skill-usage ledger),
  pack catalog name_ko, and kit entities via the kit vocabulary verb
  (vocab_verb in packs/CONTRACT.md; DGN-1694). Absent sources are silent.

Output: an ordered, deduplicated term list cached at
<instance>/.telegram_bot/voice-vocab.json. The bridge reads the cache when it
builds its transcriber, puts the explicit machine/instance vocabulary in
front, and cuts the whole to the recognizer's prompt budget. Regenerated on
bridge start (ext boot hook), mint and update -- never per voice message.

CLI:  python3 voice_vocab.py <instance-root> [--write] [--budget-bytes N]
      prints the ordered list (tier, source, term); --write refreshes the
      cache; --budget-bytes also marks where a UTF-8 byte budget cuts.
Pure stdlib. English/ASCII only (Korean terms are \\u escapes).
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime

CACHE_SCHEMA = 1
CACHE_NAME = "voice-vocab.json"
# Framework proper nouns (tier 0): Dogany, kit, pack, module, minting,
# bridge, landing, Telegram -- the words speech models mangle most.
FRAMEWORK_TERMS = (
    "\ub3c4\uac00\ub2c8", "Dogany", "\ud0b7", "\ud329", "\ubaa8\ub4c8",
    "\ubbfc\ud305", "\ube0c\ub9bf\uc9c0", "\ucc29\uc9c0",
    "\ud154\ub808\uadf8\ub7a8",
)
KIT_VOCAB_FIELD = "vocab_verb"
KIT_TIMEOUT_S = 5.0
KIT_MAX_BYTES = 64 * 1024
MAX_TERM_CHARS = 40
MAX_TIER1 = 200

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
_PLACEHOLDER_RE = re.compile(r"^(\[agent\]|__[A-Za-z0-9_]+__)$")
_SLUG_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _conf(path, key):
    """KEY=value lookup, quote-stripped (conf_reader contract, local copy so
    the bridge boot hook can run this file standalone)."""
    for line in _read(path).splitlines():
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v.strip()
    return ""


def _clean(value):
    v = (value or "").strip().strip("*").strip()
    if not v or _PLACEHOLDER_RE.match(v) or "set at onboarding" in v:
        return ""
    # "Name (gloss)" -> "Name": the gloss is a translation, not a spoken name.
    v = re.sub(r"\s*\([^)]*\)\s*$", "", v).strip()
    if not v:
        return ""
    if len(v) > MAX_TERM_CHARS or "\n" in v:
        return ""
    return v


def _persona_field(root, pattern):
    for rel in (os.path.join("identity", "hot.custom.agent.md"), "AGENT.md"):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            for line in _read(path).splitlines():
                m = re.match(pattern, line)
                if m:
                    return _clean(m.group(1))
            return ""
    return ""


def agent_names(root):
    """Display names of one instance: mint manifest label, persona Name.
    A lowercase slug label (my-agent) is an identifier, not a name."""
    return [n for n in (
        _clean(_conf(os.path.join(root, ".instance.conf"), "DOGANY_AGENT_LABEL")),
        _persona_field(root, r"^\s*-\s*Name:\s*(.*)$"),
    ) if n and not _SLUG_RE.match(n)]


def owner_terms(root):
    """How this instance addresses its owner: manifest label, env/agent.conf
    owner term (owner_term.py order), persona 'Call (the) user' line. The
    owner_term.py hardcoded fallback is NOT used: vocabulary is never
    invented."""
    return [t for t in (
        _clean(_conf(os.path.join(root, ".instance.conf"), "DOGANY_USER_LABEL")),
        _clean(os.environ.get("DOGANY_OWNER_TERM", "")),
        _clean(_conf(os.path.join(root, "config", "agent.conf"), "DASHBOARD_OWNER_TERM")),
        _persona_field(root, r"^\s*-\s*Call (?:the )?user:?\s*\*\*(.+?)\*\*"),
    ) if t]


def model_names(lib_dir=_LIB_DIR):
    """Model words from dispatch-model-display.tsv: the model column plus the
    alphabetic words of the display column, Title-cased (fable, 6 astra ->
    Fable, Astra)."""
    names = []
    for line in _read(os.path.join(lib_dir, "dispatch-model-display.tsv")).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        for word in [cols[1]] + cols[3].split():
            if word.isalpha():
                names.append(word.capitalize())
    return names


def _estate_candidates(home):
    """Every root the discover_instances.sh sources name: the install
    registry (~/.dogany/instances), the lite_instance pointer, then the
    fixed-depth fallback scan (incl. install.sh's default ~/.dogany/main)."""
    dot = os.path.join(home, ".dogany")
    for line in _read(os.path.join(dot, "instances")).splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1].startswith("/"):
            yield cols[1]
    lines = _read(os.path.join(dot, "lite_instance")).splitlines()
    if lines and lines[0].strip().startswith("/"):
        yield lines[0].rstrip()
    for pattern in (".dogany/main/", ".dogany/agents/*/", "dogany/*/",
                    "dogany/*/agents/*/", "dogany/*/poc/*/"):
        for d in sorted(glob.glob(os.path.join(home, pattern))):
            yield d


def estate_roots(home):
    """Instance roots on this machine -- the discover_instances.sh rule:
    a directory with .instance.conf named by the registry, the pointer, or
    the fixed estate depths."""
    seen, roots = set(), []
    for d in _estate_candidates(home):
        if not os.path.isfile(os.path.join(d, ".instance.conf")):
            continue
        phys = os.path.realpath(d)
        if phys not in seen:
            seen.add(phys)
            roots.append(phys)
    return sorted(roots)


# --------------------------------------------------------------------------
# tier 1 sources
# --------------------------------------------------------------------------

def _epoch(value):
    """last_used -> epoch seconds; accepts epoch numbers or ISO-8601."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _slug(root):
    return (os.environ.get("DOGANY_SLUG")
            or _conf(os.path.join(root, "config", "agent.conf"), "SLUG")
            or _conf(os.path.join(root, ".instance.conf"), "DOGANY_AGENT_NAME"))


def skill_usage(root, dogany_home):
    """skill name -> last use epoch, from ~/.dogany/skill-usage/<slug>-*.jsonl."""
    slug, last = _slug(root), {}
    if not slug:
        return last
    for path in glob.glob(os.path.join(dogany_home, "skill-usage", slug + "-*.jsonl")):
        for line in _read(path).splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("slug", slug) != slug:
                continue
            skill, ts = ev.get("skill"), _epoch(ev.get("ts"))
            if isinstance(skill, str) and ts > last.get(skill, 0.0):
                last[skill] = ts
    return last


def skill_display_names(root, dogany_home):
    usage = skill_usage(root, dogany_home)
    out = []
    for skill_md in sorted(glob.glob(os.path.join(root, ".claude", "skills", "*", "SKILL.md"))):
        text = _read(skill_md)
        if not text.startswith("---"):
            continue
        front = text.split("---", 2)[1] if text.count("---") >= 2 else ""
        m = re.search(r"^display_name:\s*(.+)$", front, re.MULTILINE)
        name = _clean(m.group(1).strip().strip("\"'")) if m else ""
        if name:
            skill = os.path.basename(os.path.dirname(skill_md))
            out.append((name, usage.get(skill, 0.0), "skill:" + skill))
    return out


def _catalog(root):
    """The pack catalog of the repo this instance updates from."""
    repo = _conf(os.path.join(root, ".instance.conf"), "DOGANY_REPO_ROOT")
    if not repo:
        return "", []
    path = os.path.join(repo, "packs", "catalog.json")
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f).get("packs") or []
    except (OSError, ValueError, AttributeError):
        return "", []
    return os.path.dirname(os.path.abspath(path)), rows if isinstance(rows, list) else []


def _receipts(root):
    """Installed packs: config/packs/<id>.receipt -> (id, class)."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "config", "packs", "*.receipt"))):
        pack = _conf(path, "pack") or os.path.basename(path)[:-len(".receipt")]
        out.append((pack, _conf(path, "class")))
    return out


def pack_display_names(catalog_rows, receipts):
    """Catalog name_ko, installed packs first. Pack use is not tracked, so
    last_used stays 0 (a receipt mtime is install time, not use)."""
    installed = {pack for pack, _cls in receipts}
    rows = [r for r in catalog_rows if isinstance(r, dict)]
    rows.sort(key=lambda r: r.get("id") not in installed)
    out = []
    for row in rows:
        name = _clean(row.get("name_ko") or "")
        if name:
            out.append((name, 0.0, "pack:%s" % row.get("id")))
    return out


def _run_kit_verb(argv, cwd, timeout):
    """stdout bytes of a successful, bounded kit call; else None. The kit runs
    in its own process group so a timeout also kills its children."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return None
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        return None
    if proc.returncode != 0 or len(stdout) > KIT_MAX_BYTES:
        return None
    return stdout


def kit_entities(root, catalog_dir, catalog_rows, receipts, timeout=KIT_TIMEOUT_S):
    """Kit vocabulary via the ONE kit interface: a kit whose manifest
    declares `vocab_verb` answers `database/<kit>.sh <verb> --json` with
    [{"term": str, "last_used": epoch|ISO|null}]. Every failure is silent."""
    rows = {r.get("id"): r for r in catalog_rows if isinstance(r, dict)}
    out = []
    for kit, cls in receipts:
        if cls != "kit" or kit not in rows:
            continue
        pkg = rows[kit].get("package_dir") or ""
        manifest = os.path.join(pkg if os.path.isabs(pkg) else os.path.join(catalog_dir, pkg),
                                "pack-manifest.json")
        try:
            with open(manifest, encoding="utf-8") as f:
                verb = json.load(f).get(KIT_VOCAB_FIELD)
        except (OSError, ValueError, AttributeError):
            continue
        cli = os.path.join(root, "database", kit + ".sh")
        if not isinstance(verb, str) or not verb.strip() or not os.access(cli, os.X_OK):
            continue
        stdout = _run_kit_verb([cli, verb.strip(), "--json"], root, timeout)
        try:
            items = json.loads(stdout.decode("utf-8")) if stdout else None
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("term"), str):
                term = _clean(item["term"])
                if term:
                    out.append((term, _epoch(item.get("last_used")), "kit:" + kit))
    return out


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def derive(root, home=None, timeout=KIT_TIMEOUT_S):
    """Ordered, deduplicated [{"term", "tier", "source", "last_used"}]."""
    root = os.path.realpath(root)
    home = home or os.path.expanduser("~")
    dogany_home = os.environ.get("DOGANY_HOME") or os.path.join(home, ".dogany")
    tier0 = [(t, "framework") for t in FRAMEWORK_TERMS]
    tier0 += [(t, "model") for t in model_names()]
    tier0 += [(t, "agent") for t in agent_names(root)]
    tier0 += [(t, "owner") for t in owner_terms(root)]
    for other in estate_roots(home):
        if other != root:
            tier0 += [(t, "estate:" + os.path.basename(other)) for t in agent_names(other)]

    catalog_dir, rows = _catalog(root)
    receipts = _receipts(root)
    tier1 = (skill_display_names(root, dogany_home)
             + pack_display_names(rows, receipts)
             + kit_entities(root, catalog_dir, rows, receipts, timeout))
    # Stable sort: equal last_used keeps source order (skills, packs, kits).
    tier1.sort(key=lambda item: -item[1])

    seen, out = set(), []
    entries = [(t, 0, s, None) for t, s in tier0]
    entries += [(t, 1, s, lu or None) for t, lu, s in tier1]
    for term, tier, source, last_used in entries:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"term": term, "tier": tier, "source": source, "last_used": last_used})
    return [e for e in out if e["tier"] == 0] + [e for e in out if e["tier"] == 1][:MAX_TIER1]


def cache_path(root):
    return os.path.join(root, ".telegram_bot", CACHE_NAME)


def write_cache(root, home=None, timeout=KIT_TIMEOUT_S):
    """Atomically publish the derived list. Returns the entries written."""
    entries = derive(root, home=home, timeout=timeout)
    path = cache_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"schema": CACHE_SCHEMA, "generated_at": int(time.time()), "terms": entries}
    fd, tmp = tempfile.mkstemp(prefix=".voice-vocab.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return entries


def main(argv):
    args = list(argv)
    write = "--write" in args
    budget = None
    if "--budget-bytes" in args:
        budget = int(args[args.index("--budget-bytes") + 1])
    roots = [a for a in args if not a.startswith("--") and not a.isdigit()]
    if len(roots) != 1:
        print("usage: voice_vocab.py <instance-root> [--write] [--budget-bytes N]",
              file=sys.stderr)
        return 2
    root = roots[0]
    entries = write_cache(root) if write else derive(root)
    used = 1  # the recognizer prepends one space
    for e in entries:
        mark = ""
        if budget is not None:
            used += len(e["term"].encode("utf-8")) + (2 if used > 1 else 0)
            mark = "" if used <= budget else "  [cut]"
        print("%d\t%s\t%s%s" % (e["tier"], e["source"], e["term"], mark))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
