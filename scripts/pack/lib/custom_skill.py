#!/usr/bin/env python3
"""custom_skill.py -- owner-described custom skills of a custom staff agent
(DGN-1910, locked spec 3.2 / 3.4 / 3.8). Pure: reads files, writes only the
rendered SKILL.md (render), never the network or a DB.

CLI (frozen, spec 3.4; exit 0 ok / 1 finding / 2 usage):
  validate --design FILE --flow MINT-FLOW.json --kit-root PAYLOAD --reserved FILE
      every 3.2 rule + 3.8. One JSON line per finding:
        {"code":"<code>","skill":"<name>","field":"<field>"}
      A finding line never echoes the text that tripped it.
  facts    --design FILE --kit-root PAYLOAD --lang ko
      {"<name>":"<facts line>"}
  render   --design FILE --root NEWBORN --kit-root PAYLOAD
      writes <root>/.claude/skills/<name>/SKILL.md from
      custom_skill_template.md; prints {"<name>":"<sha256 of SKILL.md>"}.
      Runs the flow-free rules first and refuses (rc 1) on any finding.
      All or nothing: on rc != 0 every SKILL.md this run wrote is undone
      (prior bytes restored, a dir it made removed), so a caller takes
      rendered names only from the rc 0 sha line (rendered_names()).
  verify   --design FILE --root NEWBORN
      re-renders in memory: on-disk sha == render, '## Bounds' byte-equal
      to the template. prints {"<name>":"ok"}; a failure is a finding line
      with code "verify".
  --design - reads the design from stdin. --reserved may repeat (the build
  re-check passes the reserved file plus the newborn's linked names).

Design payload (spec 3.3): {"role": "...", "skills": [...], "custom_skills": [<spec>...]}
Spec (3.2): {"name","display_name","purpose","cite","triggers","steps","uses_kit","data"}

Finding codes -> rules (DESIGN_REJECTED <code>):
  charset            F4: value != NFKC(value); a Cc/Cf/Zl/Zp/Co/Cs char;
                     '#', backtick or '---'; a leading '>' '[' '{' '|'
  grammar            schema (missing/unknown key, wrong type), name shape,
                     data not none|notes, and the steps grammar (ASCII,
                     charset after allowed tokens, hyphen-word rule, word deny)
  limit              counts and lengths (0..3 skills, kit + custom >= 1,
                     display_name 2-24, purpose <= 80, 1..6 triggers <= 40,
                     1..12 steps <= 160, one line each)
  secret             secret_scan strict hit in any text field
  cite               empty cite, a missing index, a non-owner slot
  trigger_not_owner  a trigger not a substring of a cited owner slot
  uses_kit           an id outside design.skills or outside RECORD_SKILLS, a dup
  decoy              purpose uses a closed-vocabulary word the owner never said
  collision          'dogany-' prefix, a reserved / kit name, a dup in the design

CITE SLOTS (interface owned here). A cite entry is either an integer index
into the owner-slot list, in conversation order:
    0                      flow.request
    1 .. C                 flow.context[0..C-1].answer
    C+1 .. C+K             flow.choice_words[0..K-1]
    C+K+1 .. C+K+D         flow.design_words[0..D-1]
or the slot path itself: "request", "context[i].answer", "choice_words[i]",
"design_words[i]". Any other path (e.g. "context[0].asked", the AGENT's
question) is a non-owner slot. An empty slot counts as missing.
choice_words / design_words items may be strings or {"text": ...} objects.

CLOSED VOCABULARY (decoy check; also importable for the role check): kit
skill ids, kit bundle display names (bundle.<id>.name), module names
(module.<m>.name) and catalog domains[] (packs/catalog.json beside this
checkout, when present).

SINGLE SOURCES (DGN-1967 step 12). The mint flow, screens, worker and
mint_run.sh import these instead of keeping copies: the F4 charset
(field_charset_ok, TAB refused), secret_scan_lib() (callers fail closed when
it is None), canonical_bytes, owner_slots, the bundle.conf reader
(bundle_rows), the SKILL.md frontmatter reader and the cap MAX_CUSTOM.

Owner copy: the facts vocabulary (spec 12, F3c) is DRAFT -- the owner
confirms it before ship. It is kept here as \\u escapes (code is ASCII).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import subprocess
import tempfile
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]          # scripts/pack/lib -> checkout root
TEMPLATE = HERE / "custom_skill_template.md"


def _import_secret_scan():
    """routines/lib/secret_scan (section 3.5), or None when absent. Importing
    this module never exits: the CLI refuses rc 2 without it, and every
    importer must fail closed on None (secret_scan_lib)."""
    cands = [REPO / "agents" / ".template" / "routines" / "lib"]
    if os.environ.get("DOGANY_ROOT"):
        cands.append(Path(os.environ["DOGANY_ROOT"]) / "routines" / "lib")
    for c in cands:
        if (c / "secret_scan.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            break
    try:
        import secret_scan
    except ImportError:
        return None
    return secret_scan


ss = _import_secret_scan()


def secret_scan_lib():
    """The one secret_scan module every custom-agent path scans with; None
    when the framework lacks it (callers refuse, never a narrower regex)."""
    return ss


MAX_CUSTOM = 3          # custom skills per design (dec-293); the one cap literal
SPEC_KEYS = ("name", "display_name", "purpose", "cite", "triggers", "steps", "uses_kit", "data")
DATA_VALUES = ("none", "notes")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
NON_RECORD_MODULES = ("module_core", "module_brief")
CHARSET_BANNED_CATS = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs")
STEP_CHARSET_RE = re.compile(r"^[A-Za-z0-9 ,.'()-]*$")
HYPHEN_WORD_RE = re.compile(r"(?<![a-z0-9-])[a-z0-9]+(?:-[a-z0-9]+)+(?![a-z0-9-])")
DENY_WORD_RE = re.compile(
    r"\b(?:sqlite|sqlite3|sql|bash|shell|sudo|curl|wget|chmod|rm|launchctl|"
    r"crontab|systemctl|kit_life|lifekit)\b", re.I)
OWNER_WORD_RE = re.compile(r"\bowner\b")
SLOT_PATH_RE = re.compile(r"^(?:(request)|context\[(\d+)\]\.answer|(choice_words|design_words)\[(\d+)\])$")

# DRAFT owner copy (spec 12 facts vocabulary; owner confirms before ship).
FACT_RECORD_SUFFIX = "\uc5d0 \uae30\ub85d"                 # "<module>e girok"
FACT_NOTES = "\uba54\ubaa8 \ubcf4\uad00"                    # "memo bogwan"
FACT_NONE = "\ub530\ub85c \uae30\ub85d \uc548 \ud568"       # "ttaro girok an ham"
FACT_JOIN = " \u00b7 "

DATA_LINES = {
    "none": "keep nothing beyond the kit features above.",
    "notes": "append dated lines to data/{name}.md; no secrets; read it before answering status.",
}


class Usage(Exception):
    """rc 2: bad arguments or unreadable / unparsable input."""


# ------------------------------------------------------------------ helpers
def bundle_rows(text):
    """A kit bundle.conf's rows as tab-split columns: each line stripped,
    blank and '#' lines dropped, '@' directive rows kept. The one reader;
    each caller filters the rows it means (menu, always-on, ids)."""
    return [line.strip().split("\t") for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def canonical_bytes(obj):
    """Spec 3.3 canonical form (the bytes custom_sha is taken over)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def norm(text):
    """NFKC + casefold + whitespace collapse (the trigger/decoy comparison)."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _finding(code, skill, field):
    return {"code": code, "skill": skill, "field": field}


def _load_json(path, what):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read()
        else:
            raw = Path(path).read_bytes()
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Usage("cannot read %s %s (%s)" % (what, path, exc.__class__.__name__))


# ---------------------------------------------------------------- kit facts
class Kit:
    """What one kit payload (or an instance root carrying service/<kit>/)
    says: menu rows with their module, always-on ids, payload skill names,
    i18n strings."""

    def __init__(self, root, instance_root=None, no_kit=False):
        self.root = Path(root)
        self.instance_root = instance_root or root
        # no_kit: the owner has no life kit (custom staff without a kit): an empty
        # menu / vocabulary, nothing to read -- never a guess at some other bundle.conf
        confs = [] if no_kit else sorted(self.root.glob("service/*/bundle.conf"))
        if no_kit:
            self.kit = ""
            self.menu, self.always_on = {}, set()
            self.names, self.record_skills, self._i18n = set(), [], {}
            return
        if len(confs) != 1:
            raise Usage("kit root %s: expected exactly one service/<kit>/bundle.conf, found %d"
                        % (root, len(confs)))
        self.kit = confs[0].parent.name
        self.menu = {}          # id -> module (kind=skill rows, file order)
        self.always_on = set()
        for cols in bundle_rows(confs[0].read_text(encoding="utf-8")):
            if cols[0] == "@always-on" and len(cols) == 2:
                self.always_on.add(cols[1])
            if cols[0].startswith("@"):
                continue
            if len(cols) >= 5 and cols[1] == "skill":
                self.menu[cols[0]] = cols[4]
        bundle = self.root / "skills-bundle"
        payload = {d.name for d in bundle.iterdir() if d.is_dir()} if bundle.is_dir() else set()
        self.names = set(self.menu) | self.always_on | payload
        self.record_skills = [s for s, m in self.menu.items() if m not in NON_RECORD_MODULES]
        self._i18n = {}

    def i18n(self, lang):
        if lang not in self._i18n:
            cands = [self.root / "config" / "i18n" / ("%s.bundle.json" % lang),
                     self.root / "config" / "packs" / ("%s.i18n.%s.json" % (self.kit, lang)),
                     self.root / "service" / self.kit / "i18n" / ("%s.json" % lang)]
            data = None
            for c in cands:
                if c.is_file():
                    try:
                        data = json.loads(c.read_text(encoding="utf-8"))
                    except ValueError:
                        raise Usage("kit i18n %s is not JSON" % c)
                    break
            self._i18n[lang] = data if isinstance(data, dict) else {}
        return self._i18n[lang]

    def module_name(self, module, lang="ko"):
        return self.i18n(lang).get("module.%s.name" % module)

    def closed_vocab(self, lang="ko", catalog=None):
        """Words a draft may only use when the owner said them (spec 4.2a)."""
        words = set(self.names)
        for k, v in self.i18n(lang).items():
            if isinstance(v, str) and (re.fullmatch(r"module\.[a-z0-9_]+\.name", k)
                                       or re.fullmatch(r"bundle\.[a-z0-9_]+\.name", k)):
                words.add(v)
        cat = catalog if catalog is not None else default_catalog(self.instance_root)
        for row in (cat or {}).get("packs", []):
            for d in row.get("domains") or []:
                if isinstance(d, str):
                    words.add(d)
        return {w for w in (norm(x) for x in words) if w}


def default_catalog(instance_root=None):
    try:
        p = subprocess.run(
            ["bash", str(HERE / "canonical_catalog.sh"), str(REPO),
             str(instance_root or ""), "custom_skill"],
            check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def facts_line(spec, kit, lang="ko"):
    """F3c: machine-derived, never drafted. None when a module has no name."""
    parts, seen = [], set()
    for sid in spec.get("uses_kit") or []:
        module = kit.menu.get(sid)
        if module is None:
            return None
        if module in seen:
            continue
        seen.add(module)
        label = kit.module_name(module, lang)
        if not label:
            return None
        parts.append(label + FACT_RECORD_SUFFIX)
    if spec.get("data") == "notes":
        parts.append(FACT_NOTES)
    return FACT_JOIN.join(parts) if parts else FACT_NONE


# -------------------------------------------------------------- owner slots
def _word_text(item):
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("text", "answer", "words"):
            if isinstance(item.get(k), str):
                return item[k]
    return None


def owner_slots(flow):
    """[(path, text|None)] in the cite index order documented above."""
    slots = [("request", flow.get("request") if isinstance(flow.get("request"), str) else None)]
    for i, c in enumerate(flow.get("context") or []):
        ans = c.get("answer") if isinstance(c, dict) else None
        slots.append(("context[%d].answer" % i, ans if isinstance(ans, str) else None))
    for key in ("choice_words", "design_words"):
        for i, w in enumerate(flow.get(key) or []):
            slots.append(("%s[%d]" % (key, i), _word_text(w)))
    return slots


def resolve_cite(ref, flow):
    """Owner text for one cite entry, or None (missing / non-owner / empty)."""
    slots = owner_slots(flow)
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        if 0 <= ref < len(slots):
            text = slots[ref][1]
            return text if text and text.strip() else None
        return None
    if isinstance(ref, str) and SLOT_PATH_RE.match(ref):
        text = dict(slots).get(ref)
        return text if text and text.strip() else None
    return None


def owner_words(flow):
    return "\n".join(t for _p, t in owner_slots(flow) if t)


# ------------------------------------------------------------------- rules
def field_charset_ok(value):
    """F4 for one stored value (and the role line, mint_screens): NFKC-stable,
    no Cc/Cf/Zl/Zp/Co/Cs character except a newline (one-line is the limit
    rule's to judge), so TAB is refused; no '#', backtick or '---'; no
    leading '>', '[', '{' or '|'."""
    if value != unicodedata.normalize("NFKC", value):
        return False
    if any(unicodedata.category(c) in CHARSET_BANNED_CATS and c != "\n" for c in value):
        return False
    if "#" in value or "`" in value or "---" in value:
        return False
    lead = value.lstrip()
    if lead[:1] in (">", "[", "{", "|"):
        return False
    return True


def step_grammar_ok(line, name, uses_kit, notes):
    """F3a for one step line (length is checked by the caller)."""
    if not line.isascii():
        return False
    rest = line
    if notes:
        rest = rest.replace("data/%s.md" % name, " ")
    for sid in uses_kit:
        rest = re.sub(r"(?<![A-Za-z0-9-])" + re.escape(sid) + r"(?![A-Za-z0-9-])", " ", rest)
    rest = OWNER_WORD_RE.sub(" ", rest)
    if not STEP_CHARSET_RE.match(rest):
        return False
    for w in HYPHEN_WORD_RE.findall(rest.lower()):
        if w != name:
            return False
    return not DENY_WORD_RE.search(rest)


def _uses_vocab(term, text):
    if term.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def _one_line(value):
    return "\n" not in value and "\r" not in value


def spec_findings(spec, idx, design_skills, kit, flow=None, reserved=None, vocab=None):
    """Findings for one custom spec. flow=None skips the owner-slot rules
    (cite, trigger_not_owner, decoy); reserved=None skips the reserved file."""
    out = []
    label = spec.get("name") if isinstance(spec, dict) and isinstance(spec.get("name"), str) else "#%d" % idx

    def add(code, field):
        f = _finding(code, label, field)
        if f not in out:
            out.append(f)

    if not isinstance(spec, dict):
        add("grammar", "spec")
        return out
    for k in spec:
        if k not in SPEC_KEYS:
            add("grammar", k)
    for k in SPEC_KEYS:
        if k not in spec:
            add("grammar", k)
    # text fields: type + F4 + secret
    texts = {}
    for k in ("name", "display_name", "purpose"):
        v = spec.get(k)
        if k in spec and not isinstance(v, str):
            add("grammar", k)
        elif isinstance(v, str):
            texts[k] = [v]
    for k in ("triggers", "steps"):
        v = spec.get(k)
        if k in spec and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            add("grammar", k)
        elif isinstance(v, list):
            texts[k] = v
    for k, vals in texts.items():
        for v in vals:
            if not field_charset_ok(v):
                add("charset", k)
            if ss.scan(v, "strict"):
                add("secret", k)
    name = spec.get("name") if isinstance(spec.get("name"), str) else ""
    # name
    if "name" in texts:
        if not NAME_RE.match(name):
            add("grammar", "name")
        if name.startswith("dogany-") or name in kit.names or (reserved and name in reserved):
            add("collision", "name")
    # display_name / purpose
    dn = spec.get("display_name")
    if isinstance(dn, str) and not (2 <= len(dn.strip()) <= 24 and len(dn) <= 24 and _one_line(dn)):
        add("limit", "display_name")
    purpose = spec.get("purpose")
    if isinstance(purpose, str) and not (purpose.strip() and len(purpose) <= 80 and _one_line(purpose)):
        add("limit", "purpose")
    # triggers
    triggers = spec.get("triggers") if isinstance(spec.get("triggers"), list) else None
    if triggers is not None:
        if not 1 <= len(triggers) <= 6:
            add("limit", "triggers")
        for t in triggers:
            if isinstance(t, str) and not (t.strip() and len(t) <= 40 and _one_line(t)):
                add("limit", "triggers")
    # data
    data = spec.get("data")
    if "data" in spec and data not in DATA_VALUES:
        add("grammar", "data")
    # uses_kit
    uses = spec.get("uses_kit")
    uses_ok = []
    if "uses_kit" in spec:
        if not (isinstance(uses, list) and all(isinstance(x, str) for x in uses)):
            add("grammar", "uses_kit")
        else:
            if len(set(uses)) != len(uses):
                add("uses_kit", "uses_kit")
            for sid in uses:
                if sid not in design_skills or sid not in kit.record_skills:
                    add("uses_kit", "uses_kit")
                else:
                    uses_ok.append(sid)
    # steps
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else None
    if steps is not None:
        if not 1 <= len(steps) <= 12:
            add("limit", "steps")
        for s in steps:
            if not isinstance(s, str):
                continue
            if not (s.strip() and len(s) <= 160 and _one_line(s)):
                add("limit", "steps")
            if not step_grammar_ok(s, name, uses_ok, data == "notes"):
                add("grammar", "steps")
    # owner-slot rules
    if flow is not None:
        cite = spec.get("cite")
        cited = []
        if not isinstance(cite, list) or not cite:
            add("cite", "cite")
        else:
            for ref in cite:
                text = resolve_cite(ref, flow)
                if text is None:
                    add("cite", "cite")
                else:
                    cited.append(norm(text))
        if triggers is not None and cited:
            for t in triggers:
                if isinstance(t, str) and not any(norm(t) and norm(t) in c for c in cited):
                    add("trigger_not_owner", "triggers")
        if isinstance(purpose, str):
            said = norm(owner_words(flow))
            text = norm(purpose)
            for term in (vocab if vocab is not None else kit.closed_vocab()):
                if _uses_vocab(term, text) and not _uses_vocab(term, said):
                    add("decoy", "purpose")
                    break
    elif "cite" in spec and not (isinstance(spec["cite"], list) and spec["cite"]):
        add("cite", "cite")
    return out


def design_findings(design, kit, flow=None, reserved=None):
    if not isinstance(design, dict):
        return [_finding("grammar", "", "design")]
    out = []
    skills = design.get("skills", [])
    if not (isinstance(skills, list) and all(isinstance(x, str) for x in skills)):
        out.append(_finding("grammar", "", "skills"))
        skills = []
    customs = design.get("custom_skills", [])
    if not isinstance(customs, list):
        return out + [_finding("grammar", "", "custom_skills")]
    if len(customs) > MAX_CUSTOM:
        out.append(_finding("limit", "", "custom_skills"))
    if len(skills) + len(customs) < 1:
        out.append(_finding("limit", "", "custom_skills"))
    vocab = kit.closed_vocab() if flow is not None else None
    seen = set()
    for i, spec in enumerate(customs):
        for f in spec_findings(spec, i, skills, kit, flow, reserved, vocab):
            if f not in out:
                out.append(f)
        n = spec.get("name") if isinstance(spec, dict) else None
        if isinstance(n, str):
            if n in seen:
                f = _finding("collision", n, "name")
                if f not in out:
                    out.append(f)
            seen.add(n)
    return out


def read_reserved(paths):
    names = set()
    for p in paths or []:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except OSError as exc:
            raise Usage("cannot read reserved file %s (%s)" % (p, exc.strerror))
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                names.add(line)
    return names


# ------------------------------------------------------------------ render
def template_text():
    return TEMPLATE.read_text(encoding="utf-8")


def section(text, heading):
    """Bytes of one '## <heading>' section: from its heading line up to (not
    including) the next '## ' heading or EOF. None when absent."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.rstrip("\n") == "## " + heading:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "".join(lines[start:end])


def description(spec):
    purpose = spec["purpose"].strip()
    if not purpose.endswith((".", "!", "?")):
        purpose += "."
    trig = "; ".join(spec["triggers"])
    return "%s Use when the owner says: %s. Query/edit this feature." % (purpose, trig)


def render_text(spec):
    values = {
        "name": spec["name"],
        "display_name_json": json.dumps(spec["display_name"], ensure_ascii=False),
        "description_json": json.dumps(description(spec), ensure_ascii=False),
        "owner_lines": "\n".join("> " + x for x in [spec["purpose"]] + list(spec["triggers"])),
        "step_lines": "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(spec["steps"])),
        "data_line": DATA_LINES[spec["data"]].format(name=spec["name"]),
    }
    # One pass: a placeholder inside an owner value is never expanded.
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda m: values[m.group(1)], template_text())


def skill_md_path(root, name):
    return Path(root) / ".claude" / "skills" / name / "SKILL.md"


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".SKILL.md.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------- final-lint check 5 (spec 6)
def frontmatter(text):
    lines = text.splitlines()
    fm = {}
    if not lines or lines[0].strip() != "---":
        return fm
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", ln)
        if m:
            fm[m.group(1)] = m.group(2).strip("'\"")
    return fm


def lint_rendered(text, kit_names):
    """Final-lint check 5 for an origin: mint-custom SKILL.md: every Steps
    line passes the steps grammar (uses_kit read back = the steps' hyphen
    tokens that are kit names), and the Bounds section is present.
    Returns a list of finding strings (never the offending text)."""
    out = []
    name = frontmatter(text).get("name", "")
    if section(text, "Bounds") is None:
        out.append("no '## Bounds' section")
    steps = section(text, "Steps")
    if steps is None:
        out.append("no '## Steps' section")
        return out
    data = section(text, "Data") or ""
    notes = ("data/%s.md" % name) in data
    n = 0
    for ln in steps.splitlines()[1:]:
        if not ln.strip():
            continue
        n += 1
        m = re.match(r"^\d+\. (.*)$", ln)
        if not m:
            out.append("Steps item %d is not a numbered step line" % n)
            continue
        step = m.group(1)
        uses = [w for w in HYPHEN_WORD_RE.findall(step.lower()) if w in kit_names]
        if len(step) > 160 or not field_charset_ok(step) \
                or not step_grammar_ok(step, name, uses, notes):
            out.append("Steps item %d breaks the steps grammar" % n)
    if n == 0:
        out.append("Steps section has no step")
    return out


def kit_names_of(root):
    """Kit skill names an instance (or payload) carries, for check 5."""
    names = set()
    r = Path(root)
    for conf in r.glob("service/*/bundle.conf"):
        try:
            for cols in bundle_rows(conf.read_text(encoding="utf-8")):
                if cols[0] == "@always-on" and len(cols) == 2:
                    names.add(cols[1])
                elif len(cols) >= 2 and cols[1] == "skill" and not cols[0].startswith("@"):
                    names.add(cols[0])
        except OSError:
            pass
    for d in (r / ".claude" / "skills-bundle", r / "skills-bundle"):
        if d.is_dir():
            names.update(x.name for x in d.iterdir() if x.is_dir())
    return names


# --------------------------------------------------------------------- CLI
def _print_findings(findings):
    for f in findings:
        print(json.dumps(f, ensure_ascii=False))  # code, skill, field (spec order)


def _customs(design):
    c = design.get("custom_skills", []) if isinstance(design, dict) else None
    if not isinstance(c, list):
        raise Usage("design has no custom_skills list")
    return c


def cmd_validate(a):
    design = _load_json(a.design, "design")
    flow = _load_json(a.flow, "flow")
    if not isinstance(flow, dict):
        raise Usage("flow %s is not a JSON object" % a.flow)
    kit = Kit(a.kit_root, a.instance_root, a.no_kit)
    findings = design_findings(design, kit, flow, read_reserved(a.reserved))
    _print_findings(findings)
    return 1 if findings else 0


def cmd_facts(a):
    if a.lang != "ko":
        raise Usage("facts: only --lang ko has a facts vocabulary (spec 12, DRAFT)")
    design = _load_json(a.design, "design")
    kit = Kit(a.kit_root, no_kit=a.no_kit)
    out, findings = {}, []
    for i, spec in enumerate(_customs(design)):
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            findings.append(_finding("grammar", "#%d" % i, "name"))
            continue
        line = facts_line(spec, kit, a.lang)
        if line is None:
            findings.append(_finding("uses_kit", spec["name"], "uses_kit"))
            continue
        out[spec["name"]] = line
    if findings:
        _print_findings(findings)
        return 1
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


def _undo_writes(done):
    """Reverse one render run, newest first: restore prior bytes, else remove
    the file and the dirs this run created (empty ones only)."""
    for target, prior, made in reversed(done):
        try:
            if prior is not None:
                _atomic_write(target, prior)
                continue
            if target.exists() and not target.is_symlink():
                target.unlink()
            for d in made:
                d.rmdir()
        except OSError as exc:
            print("custom_skill: render undo left %s (%s)" % (target.parent.name, exc.__class__.__name__),
                  file=sys.stderr)


def cmd_render(a):
    design = _load_json(a.design, "design")
    root = Path(a.root)
    if not root.is_dir():
        raise Usage("render: root %s is not a directory" % root)
    kit = Kit(a.kit_root, a.instance_root, a.no_kit)
    findings = design_findings(design, kit)
    if findings:
        _print_findings(findings)
        return 1
    out, done, ok = {}, [], False
    try:
        for spec in _customs(design):
            target = skill_md_path(root, spec["name"])
            skills_dir = target.parent.parent
            if target.parent.is_symlink() or target.is_symlink() or skills_dir.is_symlink():
                _print_findings([_finding("collision", spec["name"], "name")])
                return 1
            data = render_text(spec).encode("utf-8")
            prior = target.read_bytes() if target.is_file() else None
            made = [d for d in (target.parent, skills_dir, skills_dir.parent) if not d.exists()]
            done.append((target, prior, made))
            _atomic_write(target, data)
            out[spec["name"]] = sha256(data)
        ok = True
    finally:
        if not ok:
            _undo_writes(done)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


def rendered_names(stdout, rc):
    """Names a render run left on disk: only from rc 0's one sha line
    ({"<name>": "<sha256>"}, every name in NAME_RE). Anything else (finding
    lines, a traceback, rc != 0) -> [] -- a refused render undid itself."""
    if rc != 0:
        return []
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if len(lines) != 1:
        return []
    try:
        doc = json.loads(lines[0])
    except ValueError:
        return []
    if not isinstance(doc, dict) or not all(
            isinstance(k, str) and NAME_RE.match(k) and isinstance(v, str)
            and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in doc.items()):
        return []
    return sorted(doc)


def cmd_verify(a):
    design = _load_json(a.design, "design")
    root = Path(a.root)
    findings, out = [], {}
    bounds_tpl = section(template_text(), "Bounds")
    for i, spec in enumerate(_customs(design)):
        if not (isinstance(spec, dict) and isinstance(spec.get("name"), str)
                and NAME_RE.match(spec["name"])):
            findings.append(_finding("grammar", "#%d" % i, "name"))  # never a path
            continue
        try:
            want = render_text(spec).encode("utf-8")
        except (KeyError, TypeError, AttributeError):
            findings.append(_finding("grammar", "#%d" % i, "spec"))
            continue
        name = spec["name"]
        path = skill_md_path(root, name)
        try:
            have = path.read_bytes()
        except OSError:
            findings.append(_finding("verify", name, "missing"))
            continue
        if sha256(have) != sha256(want):
            findings.append(_finding("verify", name, "sha"))
        if section(have.decode("utf-8", errors="replace"), "Bounds") != bounds_tpl:
            findings.append(_finding("verify", name, "bounds"))
        if not any(f["skill"] == name for f in findings):
            out[name] = "ok"
    if findings:
        _print_findings(findings)
        return 1
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def main(argv):
    ap = _Parser(prog="custom_skill.py", description="validate / facts / render / verify "
                 "the owner-described custom skills of a custom staff agent (DGN-1910)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("validate")
    p.add_argument("--design", required=True)
    p.add_argument("--flow", required=True)
    p.add_argument("--instance-root")
    p.add_argument("--kit-root", required=True)
    p.add_argument("--no-kit", action="store_true")
    p.add_argument("--reserved", required=True, action="append")
    p = sub.add_parser("facts")
    p.add_argument("--design", required=True)
    p.add_argument("--kit-root", required=True)
    p.add_argument("--no-kit", action="store_true")
    p.add_argument("--lang", required=True)
    p = sub.add_parser("render")
    p.add_argument("--design", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--instance-root")
    p.add_argument("--kit-root", required=True)
    p.add_argument("--no-kit", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("--design", required=True)
    p.add_argument("--root", required=True)
    try:
        a = ap.parse_args(argv)
        if not a.cmd:
            raise Usage("a subcommand is required: validate | facts | render | verify")
        if ss is None:
            raise Usage("routines/lib/secret_scan.py not found (set DOGANY_ROOT)")
        return {"validate": cmd_validate, "facts": cmd_facts,
                "render": cmd_render, "verify": cmd_verify}[a.cmd](a)
    except Usage as exc:
        print("custom_skill: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
