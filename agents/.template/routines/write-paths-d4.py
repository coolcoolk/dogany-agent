#!/usr/bin/env python3
"""PreToolUse write-path rules added by v2.7 D4 -- wired in DETECT mode.
Spec: docs/v27-D3-D4-spec.md section 4 (reworked after
docs/v27-D3-D4-grill.md section E and docs/v27-D3-D4-grill-r2.md R2-10).

ONE more row of write-paths-gate.py WRITE_PATH_RULES:
    ("d4", ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"),
     "write-paths-d4.py")
(the settings matcher of the write-paths entry carries NotebookEdit, F26)
so the existing rows stay what they are and are not duplicated here:
  K04 memory      cc-memory-write-guard.py (memories/, CC shadow store,
                  why-net store, transcripts) -- Edit-family only
  K05 skill-landing  skill-landing-gate.py (a NEW SKILL.md)
  K06 onboarding-owner  onboarding-owner-gate.py (identity file while
                  ONBOARDING_PENDING)
D4 imports K04's path predicates for its Bash extension instead of copying
them.

Rows, in deny order (one deny per call; the first row that fires wins):
  framework  HF13/HF04  instance paths the framework owns and updates
             refresh: bridge/ memory-engine/ routines/ scripts/ vendors/
             playbooks/ git-hooks/ rules/ (except *.custom.*), the hub
             files, .claude/settings.json, .claude/agents/,
             .claude/skills-bundle/, framework skills (dogany-*); and a
             HEAD-moving git op (pull / merge / rebase / am / apply /
             cherry-pick / revert / checkout <ref> / switch) run in the
             instance root (Q7, DGN-887).  Consume fixes via self-update; a
             PoC copy under files/ is fine.  R2-10: also the instance's
             .git/config, .git/hooks/ and .git/info/ (Edit, Bash redirect,
             `git config` writes, `chmod -x` -- whose symbolic mode is the
             first operand, not a flag), and in the RELEASE REPO named by
             config/agent.conf RELEASE_REPO= the device files (release /
             publish scripts, release-approval.py, git-hooks/, the shared
             device libs, .git/config|hooks|info and the approvals store)
             -- baseline (path A yes) instead when RELEASE_MAINTAINER=1.
  baseline   HF04/HA29/HF23/(self)
             - persona file identity/hot.custom.agent.md and the overlay
               rules/hot.custom.md: only the baseline-editor subagent, and
               only with a user yes for this file (hot promotion needs the
               user's OK).  ONBOARDING_PENDING -> this row steps aside (K06
               governs first contact).
             - an existing SKILL.md: only the baseline-editor subagent
               (a NEW SKILL.md is K05's).
             - owner file identity/hot.custom.owner.md: main session only
               (never a subagent), user yes for this file, and every added
               line passes the line-shape lint (one-line fact with
               (YYYY-MM-DD, source); no imperative procedure wording).
             - the hook / switch surface (F19): .claude/settings.local.json,
               .mcp.json, and GATE_* lines of config/agent.conf -- a user
               yes, path A only (asked, shown, answered).
             - every Bash write to the three baseline files or a SKILL.md
               is denied, yes or not (F21): only an Edit-family call can be
               linted and diffed.  Use Edit -- or Write for a NEW SKILL.md,
               which K05 checks only on the Edit family (review bug 10).
  owner-data HF19       lifekit DB (database/lifekit*.db, kit_life*) by any
             tool except its owners (lifekit.sh / life-engine.sh / the
             service SDK); memory-engine/ state; Bash writes into K04's
             protected stores (K04 covers the Edit-family only); the gate
             state the devices trust (.telegram_bot/state/bash-gate/,
             write-paths/, why-net/, self-restart-launch/,
             options-bubbles.json), any tool (R2-7 / R2-8).
  baseline   Q-R4       an inline interpreter (python3 / perl / ruby / node
             with -c / -e or a heredoc body) whose code names a baseline
             file, .git/config, the owner-turn ledger or a transcript dir:
             "use Edit" (a cheap row, not a parser).
  layout     HF18       the first write per session under the instance root
             but outside files/ and memories/ is denied ONCE with the
             playbooks/layout.md pointer -- unless this session already
             Read layout.md.

Path matching (F22): both the root and the target are realpath'd, and on a
case-insensitive filesystem (darwin; the test seam WRITE_PATHS_CASEFOLD=1|0
overrides under DOGANY_TEST_MODE=1 only, like WRITE_PATHS_ROOT) the compare
is casefolded, so /root/RULES/hot.framework.md and a
symlinked __PROJECT_ROOT__ reach the same rows.

Out of scope: any target outside the instance root (a dispatch worker's
worktree, /tmp, the scratchpad).  The framework canonical checkout is
maintainer turf and exempt from every row -- recognised by agents/.template
AND the absence of both .telegram_bot/ and identity/, which every instance
carries (F20: `mkdir agents/.template` in an instance no longer turns D4
off).

Mode (WRITE_PATHS_D4_MODE, set by the settings line; one parse shared with
D3, bash_safety.gate_mode: case and whitespace ignored, unknown -> detect):
off | detect (default: log only; a baseline ask is recorded as a SHADOW ask and the log
line carries SHADOW_YES / SHADOW_NO, R2-12) | block.  Switch: gate-switch
slug `write-paths-d4` (GATE_WRITE_PATHS_D4; masters GATE_ALL and
GATE_STOP_ALL); off -> the off-notice, never a silent pass (F26).  A baseline
yes needs the reply to END with the exact question line the deny prints,
`Edit `<path>`? (binding <key>)` (R2-1), anchored at the call's own tool_use
entry (R2-13).  Fail mode per row: framework /
owner-data / layout fail OPEN on an internal error (a path match is
deterministic; an error is the gate's bug, not the agent's); baseline fails
CLOSED -- an error while judging a baseline target, or a crash on a payload
that names one, is a deny (F23; the combiner keeps a deny printed before a
raise).
Always exits 0.
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(SCRIPT_DIR, "lib")
GATE_SLUG = "write-paths-d4"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
ONBOARDING_MARKER = "<!-- ONBOARDING_PENDING -->"
PERSONA = "identity/hot.custom.agent.md"
OWNER = "identity/hot.custom.owner.md"
OVERLAY = "rules/hot.custom.md"
LAYOUT = "playbooks/layout.md"
AGENT_CONF = "config/agent.conf"
SWITCH_FILES = (".claude/settings.local.json", ".mcp.json")
BASELINE_EDITOR = "baseline-editor"
LAYOUT_ACK_TTL = 7 * 86400
APPROVED = "approved"  # an agent.conf GATE_* edit the user said yes to

FRAMEWORK_DIRS = ("bridge", "memory-engine", "routines", "scripts", "vendors",
                  "playbooks", "git-hooks", "rules")
FRAMEWORK_FILES = ("AGENTS.md", "CLAUDE.md", "RULES.md", "CONSTITUTION.md",
                   "CONTRACT.md", "product.yaml", ".claude/settings.json")
FRAMEWORK_PREFIXES = (".claude/agents/", ".claude/skills-bundle/",
                      ".claude/skills/dogany-")
ENGINE_STATE_RE = re.compile(r"^memory-engine/.*\.(db|sqlite3?|json|jsonl|"
                             r"pkl|npy|faiss|idx|index)$|^memory-engine/"
                             r"(state|index|data)/")
LIFEKIT_RE = re.compile(r"(^|/)database/(lifekit|kit_life)[^/]*$|"
                        r"(lifekit|kit_life)[^/ ]*\.db\b")
LIFEKIT_OWNERS = ("lifekit.sh", "life-engine.sh", "life_engine.py")
SQL_WRITE = re.compile(r"\b(insert|update|delete|replace|drop|alter|create|"
                       r"vacuum|attach|reindex)\b|\.(import|restore|read)\b",
                       re.I)
OWNER_LINE_RE = re.compile(r"^- .+\(\d{4}-\d{2}-\d{2}, ?[^)]+\)\s*$")
PROCEDURE_ARROWS = ("->", "=>")  # language words: user_yes_vocab `procedure`
GATE_LINE_RE = re.compile(r"^\s*(export\s+)?GATE_[A-Z0-9_]*\s*=", re.M)
BASH_WRITERS = {"cp", "mv", "install", "ln", "rsync", "touch", "mkdir",
                "truncate", "tee", "sed", "perl", "rm", "unlink", "trash.sh",
                "chmod", "chown", "patch", "dd"}
HEAD_MOVERS = {"pull", "merge", "rebase", "am", "apply", "cherry-pick",
               "revert", "switch"}
# R2-10: git's own config / hooks decide whether git-hooks/pre-push runs
GIT_FRAMEWORK = (".git/config", ".git/hooks/", ".git/info/")
# R2-7 / R2-8: the gate state the script-side devices and the sentinel trust
GATE_STATE_PREFIXES = (".telegram_bot/state/bash-gate/",
                       ".telegram_bot/state/write-paths/",
                       ".telegram_bot/state/why-net/",
                       ".telegram_bot/state/self-restart-launch/",
                       ".telegram_bot/options-bubbles.json")
# R2-10: device files in the release repo (relative to RELEASE_REPO)



_MODS = {}


def _load(name, path):
    """Load a sibling module once per process (review section 3: user_yes
    was re-executed for every owner-file line)."""
    mod = _MODS.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[name] = mod
    return mod


def _bs():
    return _load("bash_safety_d4", os.path.join(LIB, "bash_safety.py"))


def _uy():
    return _load("user_yes_d4", os.path.join(LIB, "user_yes.py"))


MODE_KEY = "WRITE_PATHS_D4_MODE"


def gate_mode(env):
    """D4 observes only, including installs that previously selected block."""
    return "off" if (env.get(MODE_KEY) or "").strip().lower() == "off" else "detect"


def test_mode(env):
    """Test seams (WRITE_PATHS_ROOT, WRITE_PATHS_CASEFOLD) are honoured only
    under DOGANY_TEST_MODE=1, as lib/device_gate.py does.  Unlike
    device_gate.test_mode, CLAUDECODE is not checked: a hook always runs
    under Claude Code, and its env comes from the CLI's settings, not from
    the agent's Bash call."""
    return env.get("DOGANY_TEST_MODE") == "1"


def _root(env):
    seam = env.get("WRITE_PATHS_ROOT") if test_mode(env) else None
    return os.path.normpath(seam or os.path.dirname(SCRIPT_DIR))


def _data_dir(root, env):
    return env.get("DOGANY_BOT_DATA_DIR") or os.path.join(root, ".telegram_bot")


def casefold_fs(env):
    v = env.get("WRITE_PATHS_CASEFOLD") if test_mode(env) else None
    if v in ("0", "1"):
        return v == "1"
    return sys.platform == "darwin"


class Paths(object):
    """Root-relative path resolution (F22): realpath both sides, casefold
    on a case-insensitive filesystem.  rel() returns the matching key (the
    casefolded form when folding) and the display form."""

    def __init__(self, root, cwd, fold):
        self.root = root
        self.cwd = cwd or root
        self.fold = fold
        self.roots = []
        for r in (os.path.normpath(root), os.path.realpath(root)):
            r = r.casefold() if fold else r
            if r not in self.roots:
                self.roots.append(r)

    def k(self, s):
        return s.casefold() if self.fold else s

    def rel(self, path):
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        for cand in (os.path.normpath(p), os.path.realpath(p)):
            c = self.k(cand)
            for r in self.roots:
                if c != r and c.startswith(r + os.sep):
                    return c[len(r) + 1:].replace(os.sep, "/"), \
                        cand[len(r) + 1:].replace(os.sep, "/")
        return None, None

    def is_root(self, path):
        p = path if os.path.isabs(path) else os.path.join(self.cwd, path)
        for cand in (os.path.normpath(p), os.path.realpath(p)):
            if self.k(cand) in self.roots:
                return True
        return False


# --- targets ------------------------------------------------------------------

def _sed_files(args):
    """File operands of a sed call (F26: BSD `sed -i '' s/a/b/ f` -- the
    empty suffix is not the script)."""
    files, script_given, k, inplace = [], False, 0, False
    while k < len(args):
        a = args[k]
        if a in ("-e", "-f", "--expression", "--file"):
            script_given, k = True, k + 2
            continue
        if a == "-i" or a == "-I":
            inplace = True
            nxt = args[k + 1] if k + 1 < len(args) else None
            if nxt is not None and (nxt == "" or (
                    nxt.startswith(".") and "/" not in nxt
                    and _sed_operands(args[k + 2:]) >= (
                        1 if _sed_script_flag(args) else 2))):
                k += 2  # BSD suffix argument
                continue
            k += 1
            continue
        if a.startswith("-i") or a.startswith("--in-place"):
            inplace = True
            k += 1
            continue
        if a.startswith("-"):
            k += 1
            continue
        files.append(a)
        k += 1
    if not inplace:
        return []
    return files if script_given else files[1:]


def _sed_script_flag(args):
    return any(a in ("-e", "-f", "--expression", "--file") or
               a.startswith(("--expression=", "--file=")) for a in args)


def _sed_operands(args):
    """Non-flag words of a sed tail, -e / -f values left out."""
    n, k = 0, 0
    while k < len(args):
        if args[k] in ("-e", "-f", "--expression", "--file"):
            k += 2
            continue
        if not args[k].startswith("-"):
            n += 1
        k += 1
    return n


TARGET_DIR_TOOLS = ("cp", "mv", "install", "ln")


def _target_dir(args):
    """(dir, args without it) for -t DIR / --target-directory[=]DIR."""
    for k, a in enumerate(args):
        if a in ("-t", "--target-directory") and k + 1 < len(args):
            return args[k + 1], args[:k] + args[k + 2:]
        if a.startswith("--target-directory="):
            return a.split("=", 1)[1], args[:k] + args[k + 1:]
        if re.match(r"^-[abdfHilLnPprRsTuvx]*t", a):
            at = a.index("t")
            if at == len(a) - 1:
                if k + 1 < len(args):
                    return args[k + 1], args[:k] + args[k + 2:]
            else:
                return a[at + 1:], args[:k] + args[k + 1:]
    return None, args


def _cd_target(name, args, here):
    """The dir a top-level `cd` / `pushd` moves to, or None if unknown."""
    if name not in ("cd", "pushd"):
        return None
    ops = [a for a in args if not a.startswith("-")]
    d = ops[0] if ops else "~"
    if "$" in d or "`" in d:
        return None
    return _at(here, d)


def _at(here, p):
    if not here or not p or os.path.isabs(p) or p.startswith("~"):
        return p
    return os.path.join(here, p)


def bash_targets(cmd, bs):
    """[(path, verb)] a Bash command writes, moves or deletes (heuristic:
    redirections, tee, sed -i, cp/mv/install/ln/rsync destination (or
    -t DIR), mv/rm operands, touch/mkdir/truncate/chmod operands).  A
    relative target after a top-level `cd X` is X/target, for the rest of
    that ( .. ) subshell (review bug 13)."""
    out, cds = [], []  # cds: [(scope, dir)] of the cds still in force
    for seg in bs.segments(cmd):
        here = None
        if not seg.nested:
            cds = [c for c in cds if seg.scope[:len(c[0])] == c[0]]
            here = cds[-1][1] if cds else None
        start = len(out)
        _seg_targets(seg, bs, out)
        if here:
            out[start:] = [(_at(here, p), v) for p, v in out[start:]]
        if not seg.nested and not seg.pipe:
            name, args, _ = bs.command_words(seg)
            d = _cd_target(name, args, here)
            if d is not None:
                cds.append((seg.scope, d))
    return out


def _seg_targets(seg, bs, out):
    for op, tgt in seg.redirs:
        if tgt is not None and op in (">", ">>", ">|", "<>"):
            out.append((tgt.value, "redirect"))
    name, args, _ = bs.command_words(seg)
    if name == "git":
        out.extend(_git_config_target(args, bs))
        return
    if name not in BASH_WRITERS:
        return
    if name in TARGET_DIR_TOOLS:
        tdir, args = _target_dir(args)
        if tdir is not None:
            out.append((tdir, name))
            if name == "mv":
                out.extend((a, "mv") for a in args if not a.startswith("-"))
            return
    ops = [a for a in args if not a.startswith("-")]
    if name == "sed":
        out.extend((a, "sed -i") for a in _sed_files(args))
    elif name == "perl":
        if any(a.startswith("-i") or a.startswith("-pi") for a in args):
            out.extend((a, "perl -i") for a in ops[1:])
    elif name == "tee":
        out.extend((a, "tee") for a in ops)
    elif name in ("cp", "install", "ln", "rsync"):
        if ops:
            out.append((ops[-1], name))
    elif name == "mv":
        out.extend((a, "mv") for a in ops)
    elif name == "dd":
        out.extend((a[3:], "dd") for a in args if a.startswith("of="))
    elif name == "chmod" or name == "chown":
        # R2-10: the first non-flag word is the mode / owner even when
        # it starts with '-' (`chmod -x file`)
        rest = [a for a in args if a not in CHMOD_FLAGS
                and not a.startswith("--reference")]
        out.extend((a, name) for a in rest[1:])
    else:
        out.extend((a, name) for a in ops)


def _git_config_target(args, bs):
    """R2-10: a `git config` WRITE targets .git/config (or its --file); the
    write test is D3's (bash_safety.git_config_write)."""
    g = bs.git_argv(args, ".")
    if g.sub != "config":
        return []
    w = bs.git_config_write(g.rest)
    if w is None or w.scope in ("--global", "--system"):
        return []
    return [(w.file or os.path.join(g.where, ".git", "config"), "git config")]


def interpreter_hit(cmd, bs):
    """Q-R4: an inline interpreter whose code names a baseline file, git
    config, the ledger or a transcript dir."""
    for seg in bs.segments(cmd):
        name, args, _ = bs.command_words(seg)
        if name not in bs.INTERPRETERS:
            continue
        inline = any(a in ("-c", "-e", "-E") or a.startswith("-pi")
                     for a in args) or bool(seg.heredocs)
        if not inline:
            continue
        code = " ".join(args) + " " + " ".join(seg.heredocs)
        m = INTERP_TARGET_RE.search(code)
        if m:
            return m.group(0)
    return None




def head_movers(cmd, bs, paths):
    """Q7 / DGN-887: a HEAD-moving git op whose work tree is the instance
    root (cwd or -C)."""
    hits = []
    for seg in bs.segments(cmd):
        name, args, _ = bs.command_words(seg)
        if name != "git":
            continue
        g = bs.git_argv(args, paths.cwd)
        if g.sub is None:
            continue
        where, sub, rest = g.where, g.sub, g.rest
        moves = sub in HEAD_MOVERS or (
            sub == "checkout" and not {"-b", "-B", "--orphan"} & set(rest)
            and [a for a in rest if not a.startswith("-")])
        if not moves:
            continue
        rel_k, _ = paths.rel(os.path.join(where, "x"))
        if paths.is_root(where) or (rel_k is not None and
                                    not rel_k.startswith(("files/",
                                                          "memories/"))):
            hits.append("git %s" % sub)
    return hits


def edit_texts(tool, ti):
    """New text an Edit-family call adds (for the owner-file lint)."""
    if tool == "Write":
        return [ti.get("content") or ""]
    if tool == "Edit":
        return [ti.get("new_string") or ""]
    if tool == "MultiEdit":
        return [e.get("new_string") or "" for e in ti.get("edits") or []
                if isinstance(e, dict)]
    if tool == "NotebookEdit":
        return [ti.get("new_source") or ""]
    return []


def old_texts(tool, ti):
    if tool == "Edit":
        return [ti.get("old_string") or ""]
    if tool == "MultiEdit":
        return [e.get("old_string") or "" for e in ti.get("edits") or []
                if isinstance(e, dict)]
    return []


def _procedural(line):
    if any(a in line for a in PROCEDURE_ARROWS):
        return True
    return any(p.search(line) for p in _uy().vocab().get("procedure", ()))


def owner_lint(tool, ti, current):
    """Lines the edit adds that are not stable one-line profile facts."""
    bad = []
    old = set((current or "").splitlines())
    for text in edit_texts(tool, ti):
        for line in text.splitlines():
            s = line.strip()
            if not s or line in old or s.startswith("#"):
                continue
            if not OWNER_LINE_RE.match(s) or _procedural(s):
                bad.append(s[:80])
    return bad


def gate_lines_touched(tool, ti, current):
    """F19: an Edit-family call on config/agent.conf that adds, removes or
    changes a GATE_* line."""
    if tool == "Write":
        new = set(m.group(0) for m in re.finditer(
            r"^\s*(export\s+)?GATE_.*$", ti.get("content") or "", re.M))
        old = set(m.group(0) for m in re.finditer(
            r"^\s*(export\s+)?GATE_.*$", current or "", re.M))
        return new != old
    return any(GATE_LINE_RE.search(t) for t in
               edit_texts(tool, ti) + old_texts(tool, ti))


# --- layout acknowledgement ---------------------------------------------------

def read_layout_in_session(transcript_path):
    """True when the transcript holds a Read of playbooks/layout.md."""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                if "layout.md" not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                for b in ((e.get("message") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "tool_use" \
                            and b.get("name") == "Read" and str(
                                (b.get("input") or {}).get("file_path", "")
                            ).endswith(LAYOUT):
                        return True
    except (OSError, TypeError):
        return False
    return False


def _prune_acks(d, now):
    """F26: layout-ack marks are per session; drop the ones a week old."""
    try:
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if now - os.path.getmtime(p) > LAYOUT_ACK_TTL:
                os.remove(p)
    except OSError:
        pass


def layout_once(state, session_id, transcript_path, detect):
    """True -> deny now (first unacknowledged write of the session)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")
    d = os.path.join(state, "layout-ack")
    mark = os.path.join(d, safe)
    if os.path.exists(mark):
        return False
    seen = bool(transcript_path) and read_layout_in_session(transcript_path)
    if not detect:
        try:
            os.makedirs(d, exist_ok=True)
            _prune_acks(d, time.time())
            with open(mark, "w") as fh:
                fh.write("read\n" if seen else "pointed\n")
        except OSError:
            pass
    return not seen


# --- rows ---------------------------------------------------------------------

def is_framework(relk, fold):
    f = (lambda s: s.casefold()) if fold else (lambda s: s)
    if relk in {f(x) for x in FRAMEWORK_FILES} or \
            relk.startswith(tuple(f(x) for x in FRAMEWORK_PREFIXES)):
        return True
    top = relk.split("/", 1)[0]
    if top not in {f(x) for x in FRAMEWORK_DIRS}:
        return False
    if top == f("rules") and ".custom." in os.path.basename(relk):
        return False
    return True


def canonical_checkout(root):
    """F20: the framework's own checkout, never an instance.  Instances
    always carry .telegram_bot/ and identity/; the canonical root carries
    neither, and an agent cannot remove them cheaply."""
    return os.path.isdir(os.path.join(root, "agents", ".template")) and \
        not os.path.exists(os.path.join(root, ".telegram_bot")) and \
        not os.path.exists(os.path.join(root, "identity"))


def _baseline_key(relk, fold):
    f = (lambda s: s.casefold()) if fold else (lambda s: s)
    for name in (PERSONA, OVERLAY, OWNER) + SWITCH_FILES + (AGENT_CONF,):
        if relk == f(name):
            return name
    if os.path.basename(relk) == f("SKILL.md") and \
            not relk.startswith(f(".claude/skills/dogany-")):
        return "SKILL.md"
    return None


def decide(data, env=None):
    """[(row, reason)] for this call; empty = allow."""
    env = os.environ if env is None else env
    root = _root(env)
    if canonical_checkout(root):
        return []  # framework canonical checkout: maintainer turf
    tool = data.get("tool_name")
    ti = data.get("tool_input") or {}
    fold = casefold_fs(env)
    paths = Paths(root, data.get("cwd") or root, fold)
    detect = gate_mode(env) != "block"
    bs = _bs()
    k04 = _load("k04_for_d4", os.path.join(SCRIPT_DIR,
                                           "cc-memory-write-guard.py"))
    if tool in EDIT_TOOLS:
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        targets = [(p, tool)] if isinstance(p, str) and p.strip() else []
        cmd = None
    elif tool == "Bash":
        cmd = ti.get("command") or ""
        try:
            targets = bash_targets(cmd, bs)
        except bs.ParseError:
            targets = []
    else:
        return []
    hits = []
    if cmd:
        try:
            for what in head_movers(cmd, bs, paths):
                hits.append(("framework", "HF13",
                             "%s in the live instance root moves HEAD over "
                             "framework files (DGN-887): updates come only "
                             "through routines/self-update.sh." % what))
        except bs.ParseError:
            pass
    agent_id = data.get("agent_id")
    agent_type = data.get("agent_type") or ""
    if cmd:
        try:
            hit = interpreter_hit(cmd, bs)
        except bs.ParseError:
            hit = None
        if hit:
            hits.append(("baseline", "HF04",
                         "an inline interpreter names %s: baseline files, git "
                         "config, the owner-turn ledger and transcripts change "
                         "only through Edit (Q-R4). Use Edit." % hit))
    for path, verb in targets:
        relk, rel = paths.rel(path)
        absp = os.path.join(root, rel) if rel else path
        # owner-data: Bash into K04's stores (K04 itself judges Edit-family)
        if tool == "Bash" and (k04._is_blocked(absp) or k04._is_why_net(absp)
                               or k04._is_transcript(absp)):
            hits.append(("owner-data", "HF19",
                         "%s into an engine-owned store (%s): only the "
                         "engine writes there." % (verb, path)))
            continue
        if relk is None:
            continue
        if relk.startswith(paths.k("files/")) or \
                relk.startswith(paths.k("memories/")):
            continue
        if relk.startswith(tuple(paths.k(x) for x in GATE_STATE_PREFIXES)):
            hits.append(("owner-data", "HF19",
                         "%s on %s: gate state the safety devices trust is "
                         "written only by the hooks themselves." % (verb,
                                                                    rel)))
            continue
        if relk == paths.k(".git/config") or relk.startswith(
                tuple(paths.k(x) for x in GIT_FRAMEWORK[1:])):
            hits.append(("framework", "HF13",
                         "%s on %s: git's config and hooks decide whether "
                         "git-hooks/pre-push runs; the owner changes them, "
                         "never the agent (R2-10)." % (verb, rel)))
            continue
        if LIFEKIT_RE.search(relk) or ENGINE_STATE_RE.search(relk):
            hits.append(("owner-data", "HF19",
                         "%s on %s: owner data goes through its owner "
                         "(lifekit.sh / service SDK; memory-engine state is "
                         "engine-owned)." % (verb, rel)))
            continue
        name = _baseline_key(relk, fold)
        if name:
            try:
                r = _baseline(name, rel, root, tool, ti, data, env, agent_id,
                              agent_type, cmd)
            except Exception as exc:  # F23: baseline fails closed
                r = ("baseline", "HF04",
                     "%s: the gate could not judge this baseline write "
                     "(%s); it stays closed." % (rel, type(exc).__name__))
            if r == APPROVED:
                continue
            if r:
                hits.append(r)
                continue
            if name != AGENT_CONF:
                continue
            # agent.conf without a GATE_* change: an ordinary config write
        if is_framework(relk, fold):
            hits.append(("framework", "HF13",
                         "%s on %s: framework code is upstream-owned -- never "
                         "patch it locally; consume fixes via "
                         "routines/self-update.sh and report the bug "
                         "upstream (a PoC copy under files/ is fine)." % (
                             verb, rel)))
            continue
        hits.append(("layout", "HF18", rel))
    if tool == "Bash" and cmd and _lifekit_sql(cmd, bs):
        hits.append(("owner-data", "HF19",
                     "sqlite3 write on the lifekit DB: go through lifekit.sh "
                     "/ the service SDK."))
    # layout fires only when nothing else does, and once per session
    real = [h for h in hits if h[0] != "layout"]
    if real:
        return real
    if hits:
        state = os.path.join(_data_dir(root, env), "state", "write-paths")
        if layout_once(state, data.get("session_id"),
                       data.get("transcript_path"), detect):
            return [("layout", "HF18",
                     "First write outside files/ and memories/ this session "
                     "(%s): read playbooks/layout.md first -- the map of "
                     "what is yours, what is owner data and what the "
                     "framework owns -- then retry. This blocks once per "
                     "session." % hits[0][2])]
    return []




def _lifekit_sql(cmd, bs):
    try:
        for seg in bs.segments(cmd):
            name, args, _ = bs.command_words(seg)
            if name == "sqlite3" and any(LIFEKIT_RE.search(a) for a in args) \
                    and SQL_WRITE.search(" ".join(args[1:])):
                return True
            if name in LIFEKIT_OWNERS:
                return False
    except bs.ParseError:
        return False
    return False


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _baseline(name, rel, root, tool, ti, data, env, agent_id, agent_type,
              cmd):
    absp = os.path.join(root, rel)
    if name == AGENT_CONF:
        if tool == "Bash":
            if "GATE_" not in (cmd or ""):
                return None  # not a switch line: the layout pointer judges it
            return ("baseline", "HF04",
                    "%s: GATE_* switch lines change only by an Edit the user "
                    "approved, never by Bash. Use Edit." % rel)
        if not gate_lines_touched(tool, ti, _read(absp)):
            return None
        return _yes(rel, absp, data, env, "gate-switch",
                    "a GATE_* switch line of %s" % rel) or APPROVED
    if name in SWITCH_FILES:
        if tool == "Bash":
            return ("baseline", "HF04",
                    "%s holds hook / tool wiring: it changes only by an Edit "
                    "the user approved, never by Bash. Use Edit." % rel)
        return _yes(rel, absp, data, env, "gate-switch",
                    "%s (hook / tool wiring)" % rel)
    if name == PERSONA:
        if ONBOARDING_MARKER in _read(absp):
            return None  # K06 governs first contact
    if tool == "Bash" and name == "SKILL.md" and not os.path.exists(absp):
        # review bug 10: K05 (skill-landing-gate) judges a NEW SKILL.md but
        # runs for the Edit family only, so a Bash write would land unseen
        return ("baseline", "HF04",
                "%s: a new SKILL.md lands through the Write tool, where "
                "skill-landing-gate checks it; a Bash write is never "
                "allowed. Use Write." % rel)
    if tool == "Bash":
        # F21: only an Edit-family call can be linted and diffed
        return ("baseline", "HF04",
                "%s is baseline: a Bash write cannot be linted or diffed, so "
                "it is never allowed. Use Edit (via baseline-editor for the "
                "persona / overlay / a SKILL.md)." % rel)
    if name == "SKILL.md":
        if not os.path.exists(absp):
            return None  # a new SKILL.md is K05's
        if agent_type == BASELINE_EDITOR:
            return None
        return ("baseline", "HA29",
                "%s: SKILL.md edits go through the baseline-editor subagent "
                "(Agent tool, subagent_type baseline-editor)." % rel)
    if name == OWNER:
        if agent_id:
            return ("baseline", "HF23",
                    "%s: the owner file is edited by the main session only, "
                    "never a subagent." % rel)
        bad = owner_lint(tool, ti, _read(absp))
        if bad:
            return ("baseline", "HF23",
                    "%s holds stable profile facts only, one line each "
                    "as '- <fact> (YYYY-MM-DD, <source>)'; procedures, "
                    "formats and rules belong in a SKILL.md, the "
                    "persona workflows or the text layer. Rejected: %s"
                    % (rel, " | ".join(bad[:3])))
        return _yes(rel, absp, data, env, "owner-file", rel)
    if agent_type != BASELINE_EDITOR:
        return ("baseline", "HA29",
                "%s: persona / overlay edits go through the "
                "baseline-editor subagent, on the user's explicit "
                "request." % rel)
    return _yes(rel, absp, data, env, "baseline", rel)


def _yes(rel, absp, data, env, cls, what):
    """Owner proof is retired; engine-owned write rules live in the harness."""
    return None


def _log(root, env, line):
    try:
        d = os.path.join(_data_dir(root, env), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "write-paths-d4.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), line))
    except OSError:
        pass


BASELINE_HINTS = ("hot.custom", "SKILL.md", "settings.local.json", ".mcp.json",
                  "GATE_")


def names_baseline(data):
    """F23: does a payload the gate crashed on name a baseline target?"""
    ti = data.get("tool_input") or {}
    text = " ".join(str(ti.get(k) or "") for k in
                    ("file_path", "notebook_path", "command", "content",
                     "new_string"))
    return any(h.casefold() in text.casefold() for h in BASELINE_HINTS)


def _switch(cwd):
    try:
        gs = _load("gate_switch_d4", os.path.join(SCRIPT_DIR, "gate-switch.py"))
        return gs.is_enabled(GATE_SLUG, cwd), gs
    except Exception:
        return True, None  # a broken switch never disarms the gate


def main():
    env = os.environ
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)
    mode = gate_mode(env)
    root = _root(env)
    try:
        hits = decide(data, env)
    except Exception as exc:
        if not names_baseline(data):
            _log(root, env, "[%s] ERROR_OPEN %s" % (mode, type(exc).__name__))
            sys.exit(0)
        _log(root, env, "[%s] ERROR_DENY %s" % (mode, type(exc).__name__))
        hits = [("baseline", "HF04",
                 "the gate could not judge a call naming a baseline file "
                 "(%s); baseline fails closed." % type(exc).__name__)]
    # Ownership is independent of procedural detect/off settings and never
    # accepts an owner-approval sentinel as a direct-write override.
    owned = [hit for hit in hits if hit[0] == "owner-data"]
    if not owned:
        if hits and mode != "off":
            _log(root, env, "[detect] rows=" + ",".join(h[1] for h in hits))
        return
    slug, row, reason = owned[0]
    reason = "Engine-owned write guard (%s): %s" % (row, reason)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
