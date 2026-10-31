#!/usr/bin/env python3
"""push_audience_lint.py -- DGN-1585 S3: every push.sh call site in this repo
must declare --audience.

WHY. DGN-1585 gives push.sh an --audience owner|operator axis (S1) with a
config-resolved operator route (S2). The default is owner (fail-safe for
callers outside this repo). But a default that new callers can silently rely
on is exactly the drift DGN-1585's own write-up warns about ("선언은
드리프트한다 -- 새 호출처가 아무것도 안 적으면 기본값으로 흘러간다"): every
call site INSIDE this repo must declare its audience explicitly, so a future
operator-grade alert that forgets to declare fails the build instead of
silently landing on the owner's screen. This lint is that forcing point.

SCOPE. "Call site" here means a real repo invocation of push.sh or its
forwarding wrapper push-gated.sh (packs/health-trainer) that composes an
actual message (a literal --text / --prompt / --photo argument in the same
statement). Pure passthrough wrappers (push-gated.sh's own
`exec ... "${ARGS[@]}"`, cron-guard.sh's `defer_push() { ... "$PUSH_SH" "$@"
... }`) carry no audience decision of their own -- the decision rides in
whatever the CALLER passed, so the lint holds the CALLER to declaring it, not
the forwarder.

Deliberately OUT of scope (documented, not a silent narrowing):
  - Any path with a /tests/ or /fixtures/ path segment. The push.sh test
    suites (agents/.template/routines/tests/test-push-*.sh) drive the REAL
    push.sh against a sandboxed instance to test push.sh's OWN guards
    (DGN-1122 / DGN-966 / DGN-1528 / button / retry) -- an axis orthogonal to
    WHO receives the message, and every one of those tests already runs
    against a FAKE_ENV, never a live chat. Forcing --audience into every
    assertion call site there would be scope creep into test-suite churn
    this order does not ask for. New PRODUCTION call sites are exactly what
    this lint exists to catch; the repo's own test-lane discovery doctrine
    (git-hooks/run-tests.sh, DGN-1267) draws the same fixtures/tests
    boundary for its own purposes.
  - Comment lines and docstrings/prose (mentioning push.sh in a sentence is
    not invoking it).

DETECTION is a heuristic, same posture as this repo's other structural lints
(bridge/machine_gate.py's own shape detector, user-gate-lint.py's C4 "lower
bound" reachability): it merges each file into logical statements (shell:
backslash-continuation; python: bracket-depth), then flags a statement that
(a) carries a literal message flag (--text/--prompt/--photo) and (b) names
push.sh/push-gated.sh -- either directly, via a variable this file itself
bound to a `...push(-gated)?.sh` path (PASS 1's alias table), or via the
substring "push" appearing case-insensitively in the statement (covers
push_cmd/PUSH_SH/defer_push-style names without needing full call-graph
resolution) -- but does NOT carry a literal --audience. False positives cost
a build failure with a clear fix (add --audience); false negatives are a
known, accepted gap (a dynamically-constructed argv this lint cannot trace
statically), not a growing exception list.

Exit 0 = clean. Exit 1 = at least one undeclared call site (all printed).
stdlib only, no third-party deps.
"""

import os
import re
import subprocess
import sys

PUSH_BASENAMES = ("push.sh", "push-gated.sh")
MESSAGE_FLAGS = ("--text", "--prompt", "--photo")
AUDIENCE_FLAG = "--audience"

# Path segments that take a file out of scope entirely (see module docstring
# "Deliberately OUT of scope"). Matched against any path component, so it
# catches both agents/.template/routines/tests/... and scripts/tests/....
EXCLUDE_SEGMENTS = {"tests", "fixtures", ".git", "__pycache__",
                    "node_modules", ".worktrees"}

SCAN_SUFFIXES = (".sh", ".py")

COMMENT_RE = re.compile(r"^\s*#")
# PASS 1: `varname=...push(-gated)?.sh...` assignment (shell `local a=1 b=...`
# style: multiple assignments per physical line, hence finditer not match).
_ASSIGN_RE = re.compile(
    r"(?:^|[\s;])([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(\"[^\"]*\"|'[^']*'|\S*)"
)
_PUSH_VALUE_RE = re.compile(r"push(?:-gated)?\.sh")


def _is_excluded(path, root):
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    return any(p in EXCLUDE_SEGMENTS for p in parts)


def find_repo_root(start):
    """Walk up from `start` for the directory carrying both VERSION and
    install.sh -- the two files that uniquely mark this repo's true root
    (agents/.template/ ships product.yaml but neither of these)."""
    cur = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(cur, "VERSION")) and \
                os.path.isfile(os.path.join(cur, "install.sh")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_ignored(root, paths):
    """Paths git would ignore, as a set. Empty set when root is not a git
    checkout (a released tarball, a mint fixture) -- absence of git is not a
    reason to refuse to lint, it just means nothing is ignorable.

    DGN-1585 follow-up: without this the lint walks LOCAL untracked residue.
    On a developer checkout `agents/main/` is a gitignored scaffold instance,
    and its copies of bundle/daily-retro.sh and lib/run-notify.sh tripped the
    lint on a tree whose TRACKED content is clean. The repo already has this
    convention -- the secret sweep scans `mode=tracked` for the same
    reason. Scanning what git does not carry makes the gate machine-specific,
    which is the opposite of a gate.
    """
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", root, "check-ignore", "--stdin"],
            input="\n".join(paths), capture_output=True, text=True)
    except (OSError, ValueError):
        return set()
    # rc 0 = some ignored, 1 = none ignored, 128 = not a git repo.
    if proc.returncode not in (0, 1):
        return set()
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def iter_scan_files(root):
    candidates = list(_iter_candidate_files(root))
    ignored = _git_ignored(root, candidates)
    for path in candidates:
        if path in ignored:
            continue
        yield path


def _iter_candidate_files(root):
    self_path = os.path.abspath(__file__)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_SEGMENTS
                       and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(SCAN_SUFFIXES) and name != "template.plist" \
                    and not name.endswith(".plist"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.abspath(path) == self_path:
                continue
            if _is_excluded(path, root):
                continue
            yield path


_PY_TRIPLE_QUOTE_RE = re.compile(r'("""|\'\'\')(?:(?!\1).)*?\1', re.S)


def _blank_matched_block(m):
    # line-count-preserving blank: keep every newline inside the match so
    # later 1-based line numbers still point at the right source line.
    return "\n" * m.group(0).count("\n")


def _read_noncomment_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    if path.endswith(".py"):
        # Docstrings/triple-quoted strings are PROSE, not invocations --
        # `push_cmd: argv prefix for the gated push wrapper (e.g. [...,
        # '--text', ...])` in a docstring must not read as an undeclared
        # call site. Blanked, not deleted, so line numbers in findings still
        # point at the real source line.
        text = _PY_TRIPLE_QUOTE_RE.sub(_blank_matched_block, text)
    raw = text.split("\n")
    return [("" if COMMENT_RE.match(l) else l) for l in raw]


def _push_aliases(lines):
    """PASS 1: variable names this file bound to a push.sh/push-gated.sh
    path (`ps="$SCRIPT_DIR/push.sh"`-style). Catches opaque short names that
    do not themselves contain the substring 'push' (e.g. cron-guard.sh's
    local `ps`)."""
    aliases = set()
    for line in lines:
        for m in _ASSIGN_RE.finditer(line):
            name, value = m.group(1), m.group(2)
            if _PUSH_VALUE_RE.search(value):
                aliases.add(name)
    return aliases


def _merge_shell(lines):
    """Logical-line merge: backslash-continuation only (matches every
    multi-line push.sh call shape found in this repo; deliberately NOT
    bracket-depth for shell -- depth-counting `{}` would swallow an entire
    function body into one statement and blunt precision for no shape this
    repo actually has)."""
    out = []
    buf = []
    start = None
    for i, line in enumerate(lines, 1):
        if start is None:
            start = i
        buf.append(line)
        if line.rstrip().endswith("\\") and not line.rstrip().endswith("\\\\"):
            continue
        out.append((start, "\n".join(buf)))
        buf = []
        start = None
    if buf:
        out.append((start, "\n".join(buf)))
    return out


def _merge_python(lines):
    """Logical-line merge: bracket-depth ((), [], {}) -- python argv lists
    and subprocess.run(...) calls routinely span several physical lines."""
    out = []
    buf = []
    start = None
    depth = 0
    for i, line in enumerate(lines, 1):
        if start is None:
            start = i
        buf.append(line)
        depth += line.count("(") + line.count("[") + line.count("{")
        depth -= line.count(")") + line.count("]") + line.count("}")
        if depth > 0:
            continue
        out.append((start, "\n".join(buf)))
        buf = []
        start = None
        depth = 0
    if buf:
        out.append((start, "\n".join(buf)))
    return out


def _is_call_site(stmt, aliases):
    if not any(flag in stmt for flag in MESSAGE_FLAGS):
        return False
    if any(base in stmt for base in PUSH_BASENAMES):
        return True
    if "push" in stmt.lower():
        return True
    for alias in aliases:
        if re.search(r"(?<![A-Za-z0-9_])\$?%s(?![A-Za-z0-9_])" % re.escape(alias),
                     stmt):
            return True
    return False


def check_file(path, root, findings):
    lines = _read_noncomment_lines(path)
    if not any(lines):
        return
    aliases = _push_aliases(lines)
    merger = _merge_python if path.endswith(".py") else _merge_shell
    for start_line, stmt in merger(lines):
        if not stmt.strip():
            continue
        if not _is_call_site(stmt, aliases):
            continue
        if AUDIENCE_FLAG in stmt:
            continue
        rel = os.path.relpath(path, root)
        first = stmt.strip().splitlines()[0][:120]
        findings.append(
            "%s:%d: push.sh call site has a message flag (--text/--prompt/"
            "--photo) but no --audience -- declare owner or operator "
            "(DGN-1585 S3). Statement starts: %s" % (rel, start_line, first))


PLIST_STRING_RE = re.compile(r"<string>([^<]*)</string>")
PLIST_ARRAY_RE = re.compile(r"<array>(.*?)</array>", re.S)


def check_plist(path, root, findings):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return
    for arr_m in PLIST_ARRAY_RE.finditer(text):
        body = arr_m.group(1)
        strings = PLIST_STRING_RE.findall(body)
        if not any(s.endswith(base) for s in strings for base in PUSH_BASENAMES):
            continue
        if not any(f in strings for f in MESSAGE_FLAGS):
            continue
        if AUDIENCE_FLAG not in strings:
            rel = os.path.relpath(path, root)
            findings.append(
                "%s: push.sh ProgramArguments array has a message flag but "
                "no --audience string pair -- declare owner or operator "
                "(DGN-1585 S3)" % rel)


def scan(root):
    findings = []
    for path in iter_scan_files(root):
        if path.endswith(".plist"):
            check_plist(path, root, findings)
        else:
            check_file(path, root, findings)
    return findings


def main(argv):
    root = None
    for arg in argv[1:]:
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        if arg == "--root":
            continue
        root = arg
    if root is None:
        root = find_repo_root(os.path.dirname(os.path.abspath(__file__)))
    if root is None or not os.path.isdir(root):
        print("push_audience_lint: could not resolve repo root "
              "(pass one explicitly: push_audience_lint.py --root <repo>)",
              file=sys.stderr)
        return 2
    findings = scan(root)
    if findings:
        print("[push-audience-lint] %d undeclared push.sh call site(s):"
              % len(findings), file=sys.stderr)
        for f in findings:
            print("  XX " + f, file=sys.stderr)
        return 1
    print("[push-audience-lint] clean: every push.sh call site declares "
          "--audience")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
