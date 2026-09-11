#!/bin/bash
# enforcement-claim-lint.sh -- governing-doc enforcement-claim linter (DGN-1006,
# carried to the canonical template by DGN-1079-group2-carryback).
# Catches baseline/governing docs asserting enforcement machinery (hook, gate,
# guard script, automated check) that does not exist or is not wired.
# Report-only / advisory.  Never edits any file.
#
# Root cause it targets (DGN-1005 audit, run live on the Metal dev-crew
# instance): design-time prose written in the indicative mood ("blocks",
# "enforces", "BLOCK") citing a design ticket, never reconciled after
# build/retire.  Docs using the honest unbuilt marker ("[미구현: ...]",
# "hook not built", "RETIRED") stayed truthful -- so an explicit marker
# on/near the line SUPPRESSES the claim.
#
# Detection = co-occurrence, to keep false positives low:
#   an ENFORCEMENT VERB (blocks/enforces/gates/denies/refuses/prevents/rejects/
#   mechanically/fails-closed; ko: 차단/막는다/강제한다/기계강제/실차단 ...)
#   AND a MECHANICAL ARTIFACT signal (*.sh / *.py path, git hook name,
#   PreToolUse-family hook, cron/launchd, a hyphenated tool name containing
#   lint/gate/guard/sweep/watch), OR an UPPERCASE verdict token (BLOCK/DENY/
#   REJECT) next to a gate word, OR the noun-form "<script> ... gate"
#   (existence-checked only).  Prose-only human gates ("owner approval
#   gate") carry no artifact and are never flagged.
#   Scan unit = LOGICAL bullet (wrapped continuation lines join their bullet).
#
# Verification depth (documented limits at bottom):
#   V1 script path        -> file resolvable under configured roots?
#   V2 claude-code hook   -> basename referenced in .claude/settings*.json?
#   V3 git hook claim     -> wired hook file (core.hooksPath / .git/hooks)
#                            exists AND claim evidence tokens (backticked
#                            literals, ENV_FLAG=, subject keywords like
#                            "main"/"secret") appear in its NON-COMMENT lines.
#   V4 cron/launchd claim -> a plist/timer/service in routines/ references
#                            the named script.
#   V5 tool-name claim    -> a script matching the hyphenated name exists.
#   V6 verdict-token claim-> some script has a non-comment BLOCK/DENY/REJECT
#                            within 5 lines of a claim subject word.
#
# Suppression (honest / historical prose never trips it):
#   S1 unbuilt markers on the line or the line above: [미구현 ...], not built,
#      unbuilt, planned:, no mechanical gate, fail-open, retired, 은퇴
#   S2 correction/history prose: never built, was never, superseded,
#      존재한 적 없, 없었다, 거짓, old rule, misinformation, deprecated, 기각
#   S3 fenced code blocks and blockquote (>) lines are skipped (quoted text)
#   S4 a leading-blockquote [미구현 ...] within the first 15 lines suppresses
#      the whole file (house style)
#   S5 this lint and its test are excluded from the scan set
#
# Usage:
#   enforcement-claim-lint.sh              # scan default governing-doc set
#   enforcement-claim-lint.sh FILE...      # scan only the given files
#   enforcement-claim-lint.sh --list      # print the default target set
#
# Config (env):
#   ECL_ROOT         workspace root        (default: repo root above script)
#   ECL_HOOK_REPOS   colon list of git repos whose hooks back git-hook claims
#                    (default: ECL_ROOT only; add more repos via this env var
#                    for setups where governing docs also make claims about a
#                    separate repo's hooks -- e.g. a dev-crew instance whose
#                    docs describe both its own machinery and the framework
#                    repo's)
#   ECL_SCRIPT_DIRS  colon list of dirs searched for scripts/implementations
#                    (default: ROOT/routines:ROOT/scripts:ROOT/bridge:
#                     ROOT/git-hooks)
#   ECL_SETTINGS     colon list of claude settings files for hook wiring
#                    (default: ROOT/.claude/settings.json + settings.local.json)
#   ECL_SOT_DIR      sot/ dir (default: ROOT/sot, unset if absent)
#   ECL_DEBUG=1      print every evaluated candidate with pass/FAIL verdict
#                    (use for the manual self-grill of a clean run)
#
# Exit codes:
#   0 = no violations found (or --help / --list)
#   1 = one or more violations found
#   2 = usage / file-not-found error
#
# KNOWN BLIND SPOTS (do not over-trust -- that recreates the DGN-1005 defect):
#   - Claims about BEHAVIOR INSIDE an existing, wired file ("hook also checks
#     X") verify only by token presence, not semantics; a hook that merely
#     mentions the token (in code) passes.
#   - Prose-only enforcement claims with no artifact signal are invisible.
#   - Numbers/schedules ("fires daily 07:30") are not checked against plists.
#   - A false claim sharing its line with a suppression word (e.g. "retired")
#     escapes.  - Claims about remote/other-host machinery are unverifiable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  sed -n '2,/^set -euo/{ /^set -euo/d; s/^# \{0,1\}//; p; }' "$0"
  exit 0
fi

LIST_MODE=0
if [ "${1:-}" = "--list" ]; then
  LIST_MODE=1
  shift
fi

ECL_ROOT="${ECL_ROOT:-$REPO_ROOT}"
export ECL_ROOT ECL_LIST_MODE="$LIST_MODE"

python3 - "$@" << 'PYEOF'
import sys, os, re, json, subprocess

ROOT = os.environ["ECL_ROOT"]
LIST_MODE = os.environ.get("ECL_LIST_MODE") == "1"
DEBUG = os.environ.get("ECL_DEBUG") == "1"   # print every candidate + verdict
SELF_NAMES = {"enforcement-claim-lint.sh", "test-dgn1006-enforcement-claim-lint.sh"}

def env_paths(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return [p for p in v.split(":") if p]

HOOK_REPOS = env_paths("ECL_HOOK_REPOS",
    [r for r in (ROOT,) if os.path.isdir(os.path.join(r, ".git"))])

_default_script_dirs = [os.path.join(ROOT, d)
                        for d in ("routines", "scripts", "bridge", "git-hooks")]
SCRIPT_DIRS = [d for d in env_paths("ECL_SCRIPT_DIRS", _default_script_dirs) if os.path.isdir(d)]

SETTINGS_FILES = [f for f in env_paths("ECL_SETTINGS",
    [os.path.join(ROOT, ".claude", "settings.json"),
     os.path.join(ROOT, ".claude", "settings.local.json")]) if os.path.isfile(f)]

_sot = os.environ.get("ECL_SOT_DIR")
if not _sot:
    _sot = os.path.join(ROOT, "sot")
SOT_DIR = _sot

# ---------------------------------------------------------------------------
# Target set
# ---------------------------------------------------------------------------
def default_targets():
    import glob as g
    out = []
    # Identity / governing docs. AGENT.md is dual-accept (DGN-773): a
    # compat symlink to PROFILE.md post-rename, or a real pre-rename file --
    # os.path.isfile follows the symlink either way, so no special-casing
    # is needed here.  AGENTS.md is the 2.0+ loader hub; the imported files
    # are scanned directly too since the lint reads files, not the
    # @-import graph.  The legacy root names and the DGN-1141 stage-5
    # layout (rules/ + identity/ + vendors/ + playbooks/) are BOTH listed:
    # each entry is isfile-guarded, so pre-relayout instances match the
    # first group and relayout instances the second; the realpath dedup
    # below drops the compat-symlink double-hit.
    for name in ("AGENT.md", "AGENTS.md", "CONSTITUTION.md", "CONTRACT.md",
                 "DISCIPLINE.md", "PROFILE.md", "USER.md", "RULES.md",
                 "bridge.md", "telegram.md", "CLAUDE.md",
                 os.path.join("rules", "hot.framework.md"),
                 os.path.join("rules", "hot.custom.md"),
                 os.path.join("rules", "cold.framework.why.md"),
                 os.path.join("identity", "hot.custom.agent.md"),
                 os.path.join("identity", "hot.custom.owner.md"),
                 os.path.join("vendors", "telegram.md"),
                 os.path.join("vendors", "claudecode.md")):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            out.append(p)
    out += sorted(g.glob(os.path.join(ROOT, "playbooks", "*.md")))
    # Compat symlinks (PROFILE.md -> identity/hot.custom.agent.md, ...) make the
    # same real file reachable under two listed names -- scan it once.
    _seen = set()
    _deduped = []
    for p in out:
        rp = os.path.realpath(p)
        if rp in _seen:
            continue
        _seen.add(rp)
        _deduped.append(p)
    out = _deduped
    out += sorted(g.glob(os.path.join(ROOT, "docs", "*.md")))
    if os.path.isdir(SOT_DIR):
        out += sorted(g.glob(os.path.join(SOT_DIR, "*.md")))
    out += sorted(g.glob(os.path.join(ROOT, ".claude", "agents", "*.md")))
    out += sorted(g.glob(os.path.join(ROOT, ".claude", "skills", "**", "SKILL.md"), recursive=True))
    return [p for p in out if os.path.basename(p) not in SELF_NAMES]

targets = [a for a in sys.argv[1:]] or default_targets()
targets = [t for t in targets if os.path.basename(t) not in SELF_NAMES]

if LIST_MODE:
    for t in targets:
        print(t)
    sys.exit(0)

missing = [t for t in targets if not os.path.isfile(t)]
if missing:
    for t in missing:
        print(f"[enforcement-claim-lint] ERROR: file not found: {t}", file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------
ENFORCE_RE = re.compile(
    r"\bblocks\b|\bblocked by\b|\benforce[sd]\b|(?<!-)\bgates\b|\bgating\b"
    r"|\brefuses?\b|\bprevents\b|\brejects\b|\bdenies\b"
    r"|\bfails?[ -]closed\b|\bmechanically\b"
    r"|차단|막는다|막아준다|막아준|막힌다|강제한다|강제된다|기계강제"
    r"|기계로 강제|실차단|거부한다",
    re.IGNORECASE)

SCRIPT_PATH_RE = re.compile(r"[\w~][\w./~-]*\.(?:sh|py)\b")
CLAUDE_HOOK_RE = re.compile(
    r"\b(PreToolUse|PostToolUse|UserPromptSubmit|SessionStart|PreCompact)\b|\bStop hook\b")
GIT_HOOK_RE = re.compile(r"\b(pre-commit|pre-push|post-commit|pre-merge-commit)\b",
                         re.IGNORECASE)
HOOK_WORD_RE = re.compile(r"\bhooks?\b|훅")
CRON_RE = re.compile(r"\bcron\b|크론|launchd|launchctl|crontab")
TOOLNAME_RE = re.compile(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")
TOOLNAME_KEYWORDS = ("lint", "gate", "guard", "sweep", "watch")
TOOLNAME_EXCLUDE = {"pre-commit", "pre-push", "post-commit", "pre-merge-commit",
                    "no-verify", "fail-open", "fails-open"}
VERDICT_RE = re.compile(r"\b(?:BLOCK|DENY|REJECT)S?\b")   # case-sensitive
GATEWORD_RE = re.compile(r"[Gg]ate|게이트|[Gg]uard")

# Same-line suppression: honest markers AND correction/history prose.
SUPPRESS_RE = re.compile(
    # S1 honest unbuilt markers
    r"\[미구현|미구현\s*[:\]]|not built|unbuilt|no mechanical gate|planned:"
    r"|retired|은퇴|fail[s]?[ -]open"
    # S2 correction / history prose
    r"|never built|was never|superseded|존재한 적 없|없었다|거짓|old rule"
    r"|misinformation|no longer|deprecated|기각|과거에는|used to",
    re.IGNORECASE)
# Previous-line suppression: ONLY explicit unbuilt markers.  Broad words like
# "retired" on a NEIGHBOURING line must not shield an unrelated claim below it
# (that exact pattern hid a real gap-gate BLOCK claim in Metal-side testing).
SUPPRESS_PREV_RE = re.compile(
    r"\[미구현|미구현\s*[:\]]|not built|unbuilt|no mechanical gate|planned:",
    re.IGNORECASE)

BACKTICK_RE = re.compile(r"`([^`]+)`")
ENVFLAG_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,})=")
SUBJECT_KEYWORDS = ("main", "secret", "worklog", "bridge", "VERSION", "CHANGELOG",
                    "pin", "detached")
TOKEN_WHITELIST = {"--no-verify", "--force", "HEAD", "main", ".git"}
ENVFLAG_WHITELIST = {"PATH", "HOME", "PLAN", "SECTION_GLYPHS"}
VERDICT_STOPWORDS = {"gate", "gates", "block", "blocks", "deny", "warn", "step",
                     "through", "only", "with", "this", "that", "must", "detail",
                     "reject", "when", "then", "else", "over", "into", "from",
                     "the", "and", "for", "not", "are", "was", "has", "its",
                     "per", "via", "one", "two", "all", "any", "out", "non",
                     "run", "docs", "dgn", "sot", "md"}

# ---------------------------------------------------------------------------
# Verification helpers (with memoization)
# ---------------------------------------------------------------------------
_cache = {}

def script_files():
    if "scripts" not in _cache:
        out = []
        for d in SCRIPT_DIRS:
            for base, dirs, files in os.walk(d):
                if base.count(os.sep) - d.count(os.sep) > 2:
                    dirs[:] = []
                    continue
                dirs[:] = [x for x in dirs if x not in ("venv", "node_modules",
                                                        "__pycache__")]
                for f in files:
                    if f.endswith((".sh", ".py")) or (base.endswith(("git-hooks", "hooks"))):
                        out.append(os.path.join(base, f))
        _cache["scripts"] = out
    return _cache["scripts"]

def impl_files():
    """script_files minus test files -- a test exercising (or documenting) a
    behavior is NOT the enforcement machinery itself."""
    return [f for f in script_files()
            if "/tests/" not in f and not os.path.basename(f).startswith(("test-", "test_"))]

def workspace_files():
    """Bounded existence oracle: all .sh/.py basenames anywhere reasonable
    under ROOT and the hook repos (depth <= 4, junk dirs pruned)."""
    if "ws" not in _cache:
        out = {}
        prune = {"venv", "node_modules", "__pycache__", ".git", "worktrees",
                 "_archive"}
        for root in [ROOT] + HOOK_REPOS:
            for base, dirs, files in os.walk(root):
                if base.count(os.sep) - root.count(os.sep) > 4:
                    dirs[:] = []
                    continue
                dirs[:] = [x for x in dirs if x not in prune]
                for f in files:
                    if f.endswith((".sh", ".py")):
                        out.setdefault(f, os.path.join(base, f))
        _cache["ws"] = out
    return _cache["ws"]

PLACEHOLDER_SKIP = object()   # mint-time token, unresolvable pre-mint by design

def resolve_script(tok):
    tok = tok.strip().lstrip("(").rstrip(").,;:")
    # __PROJECT_ROOT__ is a literal mint-time placeholder substituted by
    # mint.sh into the real instance path -- it is never a real directory in
    # the shipped template tree, so a template-doc claim naming
    # "__PROJECT_ROOT__/some/path" cannot be verified pre-mint.  Resolve the
    # remainder relative to ROOT on a best-effort basis (catches genuinely
    # template-local scripts, e.g. __PROJECT_ROOT__/bridge/self_restart.sh);
    # if that also misses, treat it as unresolvable-by-design rather than a
    # false "not found" (the KNOWN BLIND SPOTS class this lint documents).
    if tok.startswith("__PROJECT_ROOT__/") or tok == "__PROJECT_ROOT__":
        rest = tok[len("__PROJECT_ROOT__"):].lstrip("/")
        if rest:
            c = os.path.join(ROOT, rest)
            if os.path.isfile(c):
                return c
        return PLACEHOLDER_SKIP
    cands = []
    if tok.startswith("~"):
        cands.append(os.path.expanduser(tok))
    elif os.path.isabs(tok):
        cands.append(tok)
    else:
        cands.append(os.path.join(ROOT, tok))
        for r in HOOK_REPOS:
            cands.append(os.path.join(r, tok))
    for c in cands:
        if os.path.isfile(c):
            return c
    base = os.path.basename(tok)
    for f in script_files():
        if os.path.basename(f) == base:
            return f
    return workspace_files().get(base)

def settings_text():
    if "settings" not in _cache:
        buf = ""
        for f in SETTINGS_FILES:
            try:
                buf += open(f, encoding="utf-8").read()
            except OSError:
                pass
        _cache["settings"] = buf
    return _cache["settings"]

def wired_git_hooks(hooktype):
    key = ("hooks", hooktype)
    if key not in _cache:
        out = []
        for repo in HOOK_REPOS:
            hooksdir = None
            try:
                r = subprocess.run(["git", "-C", repo, "config", "core.hooksPath"],
                                   capture_output=True, text=True, timeout=10)
                hp = r.stdout.strip()
                if hp:
                    hooksdir = hp if os.path.isabs(hp) else os.path.join(repo, hp)
            except Exception:
                pass
            if not hooksdir:
                hooksdir = os.path.join(repo, ".git", "hooks")
            hf = os.path.join(hooksdir, hooktype)
            if os.path.isfile(hf):
                out.append(hf)
        _cache[key] = out
    return _cache[key]

def noncomment(path):
    """Non-comment lines of an implementation file.  For .py, triple-quoted
    docstring blocks count as comments too (prose, not machinery)."""
    key = ("nc", path)
    if key not in _cache:
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            lines = []
        out = []
        in_doc = False
        is_py = path.endswith(".py")
        for l in lines:
            ls = l.lstrip()
            if is_py:
                quotes = ls.count('"""') + ls.count("'''")
                if in_doc:
                    if quotes:
                        in_doc = False
                    continue
                if quotes == 1:
                    in_doc = True
                    continue
                if quotes >= 2:      # one-line docstring
                    continue
            if ls.startswith("#"):
                continue
            out.append(l)
        _cache[key] = out
    return _cache[key]

def claim_evidence_tokens(line):
    """Backticked literals + ENV_FLAG= names + curated subject keywords."""
    toks = set()
    for t in BACKTICK_RE.findall(line):
        t = t.strip()
        if " " in t or t in TOKEN_WHITELIST or t.endswith((".sh", ".py", ".md")):
            continue
        if t.startswith("--"):
            continue  # CLI flags of git itself (--no-verify etc.)
        toks.add(t)
    for t in ENVFLAG_RE.findall(line):
        if t not in ENVFLAG_WHITELIST:
            toks.add(t)
    for kw in SUBJECT_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", line):
            toks.add(kw)
    return toks

def verify_git_hook(hooktype, line):
    hooks = wired_git_hooks(hooktype)
    if not hooks:
        return (f"no wired {hooktype} hook found in any configured repo "
                f"({':'.join(HOOK_REPOS) or 'none configured'})")
    toks = claim_evidence_tokens(line)
    if not toks:
        return None  # existence+wiring is all we can check
    best_missing = None
    for hf in hooks:
        body = "\n".join(noncomment(hf))
        missing = [t for t in toks if t not in body]
        if not missing:
            return None
        if best_missing is None or len(missing) < len(best_missing):
            best_missing = missing
    return (f"wired {hooktype} hook exists but claim tokens "
            f"{best_missing} absent from its non-comment lines "
            f"(comment-only mention does not count)")

def verify_claude_hook(basename):
    if basename in settings_text():
        return None
    return (f"script exists but '{basename}' is not referenced in "
            f".claude/settings*.json hook wiring")

def verify_cron(basename):
    key = ("plists",)
    if key not in _cache:
        buf = ""
        rdir = os.path.join(ROOT, "routines")
        if os.path.isdir(rdir):
            for f in os.listdir(rdir):
                if f.endswith((".plist", ".timer", ".service")):
                    try:
                        buf += open(os.path.join(rdir, f), encoding="utf-8",
                                    errors="replace").read()
                    except OSError:
                        pass
        _cache[key] = buf
    if basename in _cache[key]:
        return None
    return (f"script exists but no plist/timer/service under routines/ "
            f"references '{basename}' (cron/launchd claim unwired)")

def verify_toolname(name):
    for f in impl_files():
        b = os.path.basename(f)
        if b == name or b.startswith(name + "."):
            return None
    return f"no script matching '{name}[.sh|.py]' found under configured script dirs"

def verify_verdict(line):
    m = None
    for sent in re.split(r"(?<=[.?!])\s+|(?<=\.)\s{2,}", line):
        if VERDICT_RE.search(sent):
            m = sent
            break
    if m is None:
        m = line
    subjects = set()
    for w in re.findall(r"[a-z]{3,}", m):
        if w not in VERDICT_STOPWORDS:
            subjects.add(w)
    if not subjects:
        return ("verdict-token claim (BLOCK/DENY/REJECT) with no extractable "
                "subject -- unresolvable, manual review")
    for f in impl_files():
        lines = noncomment(f)
        vidx = [i for i, l in enumerate(lines) if VERDICT_RE.search(l)]
        if not vidx:
            continue
        sidx = [i for i, l in enumerate(lines)
                if any(re.search(r"\b" + re.escape(s) + r"\b", l, re.IGNORECASE)
                       for s in subjects)]
        if any(abs(v - s) <= 5 for v in vidx for s in sidx):
            return None
    return (f"verdict-token claim: no script implements a BLOCK/DENY/REJECT "
            f"near subjects {sorted(subjects)[:6]} -- machinery not found")

# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
violations = []
notes = []

for path in targets:
    with open(path, encoding="utf-8") as fh:
        raw_lines = fh.read().splitlines()

    # S4: whole-file suppression -- house-style leading [미구현 ...] blockquote
    if any("[미구현" in l for l in raw_lines[:15] if l.lstrip().startswith(">")):
        notes.append(f"{path}: whole-file [미구현] marker -- skipped")
        continue

    # -- Group physical lines into LOGICAL UNITS (a bullet plus its wrapped
    #    continuation lines).  A suppression marker anywhere in the unit
    #    covers the whole unit -- wrapped bullets in RATIONALE-style docs
    #    put "RETIRED" on line 1 and the claim tokens on line 3.
    BULLET_START_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
    units = []          # (start_lineno, joined_text)
    in_fence = False
    cur = {"start": None, "lines": []}

    def flush(cur=cur, units=units):
        if cur["lines"]:
            units.append((cur["start"], " ".join(cur["lines"])))
        cur["start"], cur["lines"] = None, []

    for lineno, line in enumerate(raw_lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            flush()
            continue
        if in_fence or not stripped or stripped.startswith(">"):   # S3
            flush()
            continue
        if stripped.startswith("#") or BULLET_START_RE.match(line):
            flush()
            cur["start"], cur["lines"] = lineno, [stripped]
        elif cur["lines"]:
            cur["lines"].append(stripped)   # continuation of current unit
        else:
            cur["start"], cur["lines"] = lineno, [stripped]
    flush()

    prev_text = ""
    for start_lineno, stripped in units:
        if "enforcement-claim-lint" in stripped:   # S5 self-reference
            prev_text = stripped
            continue

        # S1/S2 suppression: broad within the unit itself; marker-only on the
        # immediately preceding unit
        if SUPPRESS_RE.search(stripped) or SUPPRESS_PREV_RE.search(prev_text):
            prev_text = stripped
            continue
        prev_text = stripped
        lineno = start_lineno

        has_enforce = bool(ENFORCE_RE.search(stripped))
        has_verdict = bool(VERDICT_RE.search(stripped) and GATEWORD_RE.search(stripped))
        # Noun-form claim: "<script> gate" (e.g. "promote.sh --check gate")
        # asserts gating machinery even without an enforcement verb --
        # existence-checked only (weakest claim, weakest check).
        has_gate_script = bool(GATEWORD_RE.search(stripped)
                               and SCRIPT_PATH_RE.search(stripped))
        if not (has_enforce or has_verdict or has_gate_script):
            continue

        findings = []       # (artifact, problem)
        checked_any = False

        # V1: explicit script paths (+ V2 claude-hook wiring, V4 cron wiring)
        script_toks = (SCRIPT_PATH_RE.findall(stripped)
                       if (has_enforce or has_gate_script) else [])
        resolved_bases = []
        for tok in script_toks:
            checked_any = True
            resolved = resolve_script(tok)
            if resolved is PLACEHOLDER_SKIP:
                continue  # unresolvable mint-time placeholder, not a claim to verify
            if resolved is None:
                findings.append((tok, "named script not found under any configured root"))
                continue
            resolved_bases.append(os.path.basename(resolved))
        # V2/V4: the claim passes if ANY named script is wired -- helper
        # scripts a hook reuses need not appear in the wiring themselves.
        # Wiring is only demanded of INDICATIVE enforcement claims; noun-form
        # "X gate" mentions get the existence check alone.
        if not has_enforce:
            resolved_bases = []
        if resolved_bases and CLAUDE_HOOK_RE.search(stripped):
            probs = [verify_claude_hook(b) for b in resolved_bases]
            if all(probs):
                findings.append((resolved_bases[0], probs[0]))
        if resolved_bases and CRON_RE.search(stripped):
            probs = [verify_cron(b) for b in resolved_bases]
            if all(probs):
                findings.append((resolved_bases[0], probs[0]))

        # V3: git hook claims
        if has_enforce:
            for hooktype in {h.lower() for h in GIT_HOOK_RE.findall(stripped)}:
                if HOOK_WORD_RE.search(stripped) or "guard" in stripped.lower():
                    checked_any = True
                    prob = verify_git_hook(hooktype, stripped)
                    if prob:
                        findings.append((hooktype + " hook", prob))

        # V5: hyphenated tool names (only when no explicit path already covers it)
        if has_enforce and not script_toks:
            for name in set(TOOLNAME_RE.findall(stripped)):
                if name in TOOLNAME_EXCLUDE:
                    continue
                # keyword must be an exact hyphen component ("usage-gate" yes,
                # "owner-gates" no) -- prevents prose compounds tripping V5
                if not any(k in name.split("-") for k in TOOLNAME_KEYWORDS):
                    continue
                # "<name> rule" is a policy reference, not a tool artifact
                if re.search(re.escape(name) + r"\s+rule\b", stripped):
                    continue
                checked_any = True
                prob = verify_toolname(name)
                if prob:
                    findings.append((name, prob))

        # V6: verdict-token claims with no named artifact
        if has_verdict and not checked_any:
            checked_any = True
            prob = verify_verdict(stripped)
            if prob:
                findings.append(("(unnamed verdict machinery)", prob))

        # Unresolvable: enforcement verb + claude-hook family, nothing named
        if (has_enforce and not checked_any and CLAUDE_HOOK_RE.search(stripped)):
            findings.append(("(unnamed hook)",
                             "enforcement claim names a hook family but no "
                             "resolvable artifact -- unresolvable, manual review"))

        if DEBUG and checked_any:
            verdict = "FAIL" if findings else "pass"
            print(f"[debug] {verdict} {path}:{lineno} :: {stripped[:110]}")

        for artifact, problem in findings:
            violations.append({
                "file": path, "lineno": lineno,
                "claim": stripped if len(stripped) <= 240 else stripped[:237] + "...",
                "artifact": artifact, "found": problem,
            })

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
print(f"enforcement-claim-lint: scanned {len(targets)} file(s)")
print("=" * 60)
for n in notes:
    print(f"NOTE: {n}")

if not violations:
    print("RESULT: OK -- 0 enforcement-claim violations found.")
    sys.exit(0)

print(f"RESULT: VIOLATIONS -- {len(violations)} issue(s) found\n")
for v in violations:
    print(f"{v['file']}:{v['lineno']}")
    print(f"  CLAIM   : {v['claim']}")
    print(f"  ARTIFACT: {v['artifact']}")
    print(f"  FOUND   : {v['found']}")
    print()
sys.exit(1)
PYEOF
