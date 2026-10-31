#!/usr/bin/env python3
"""PostToolUse hook: lint status in worklog/<TICKET>-*.md frontmatter.

Fires after Edit/Write/MultiEdit on a worklog ticket file. Three checks (all
advisory forcing-point warnings via additionalContext; this hook never
blocks -- real write-control lives structurally in the interactive-offload
def hard-limit plus the head merge preflight, per DGN-730 item 5, decision
D1):

  (1) enum-value: status: must be one of the allowed enum values (DGN-1013
      item 1 -- see worklog/_TEMPLATE.md for the canonical list).
  (2) transition-scope: a transition INTO done must carry evidence in the
      file (logged != done, DGN-730 item 3). Old status comes from git HEAD
      so legacy done entries that are NOT changing this edit are never
      flagged (false-positive = 0). A born-done file (no HEAD version, or
      first commit is already done) is treated as a done transition and
      must show evidence too, or it becomes a no-check escape hatch for a
      large share of real done events.

  (3) display-title (DGN-1754): a ticket that is not done/dismissed must
      carry "display_title.<lang>" for the instance language (AGENT_LANG
      base code).  Owner surfaces show that display title; without it they
      show the id alone, never the internal "title" key.  The advisory
      names the title REGISTER from DISPLAY_NAMES (owner 2026-10-02): off
      (dev / canonical-manager instance, builder owner) -> keep technical
      terms in their original form; on or unset (end-user instance) ->
      plain words, no jargon.  Same knob as the RULES Output display-name
      register, so one setting governs both.

Evidence = a value-companion frontmatter/body token (not the bare word,
which appears as prose in schema-quoting tickets): merge_sha / branch_sha
followed by a real hex sha, or verifier_verdict: PASS. Prose mentions like
`merge_sha` with no value, or JSON placeholders like "<hex>", do not match.

Portability (DGN-1013 carryback): the original DGN-730 version hardcoded
the "DGN-" ticket prefix, so it silently no-op'd for any instance whose
ticket ids use a different prefix or a prefixless glob (the exact class of
bug DGN-1052 fixed in ticket-hygiene.sh). This version resolves the ticket
file glob the same way: env TICKET_FILE_GLOB -> config/agent.conf
TICKET_FILE_GLOB= -> "<TICKET_PREFIX>-*.md" fallback, TICKET_PREFIX itself
resolving env -> config/agent.conf -> unset (glob resolution then yields
"*.md", matching every worklog file -- a safe default: this hook is
advisory-only and reads status: itself, so a wide glob costs nothing extra
it wasn't already going to check via the parent-dir/reserved-name gate).

Fails open on any error (bad input, missing field, git absent, exception).
"""
import fnmatch
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from conf_reader import conf_get
except Exception:
    def conf_get(key, conf_path=None):
        return ""

VALID_STATUS = frozenset(
    ["open", "wip", "blocked", "done", "parked", "backlog", "dismissed"]
)

# Value-companion evidence patterns. A real sha is >=7 hex chars; a placeholder
# like <hex> or <worktree HEAD short sha> never matches. verifier_verdict must
# carry an explicit PASS.
EVIDENCE_PATTERNS = (
    re.compile(r"merge_sha\W+[0-9a-f]{7,}", re.IGNORECASE),
    re.compile(r"branch_sha\W+[0-9a-f]{7,}", re.IGNORECASE),
    re.compile(r"verifier_verdict\W+PASS", re.IGNORECASE),
)


def _root_dir():
    # This file lives at <workspace_root>/routines/ticket-status-enum-lint.py.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _conf_get(key):
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
        _root_dir(), "config", "agent.conf")
    return conf_get(key, path)


def _ticket_file_glob():
    """Resolve the worklog ticket filename glob (DGN-1052 precedent, same
    env -> conf -> prefix-fallback order as ticket-hygiene.sh)."""
    glob = os.environ.get("TICKET_FILE_GLOB") or _conf_get("TICKET_FILE_GLOB")
    if glob:
        return glob
    prefix = os.environ.get("TICKET_PREFIX") or _conf_get("TICKET_PREFIX")
    return "%s-*.md" % prefix if prefix else "*.md"


def read_frontmatter_status(path):
    """Return the status value from YAML frontmatter, or None if absent/unparseable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            in_front = False
            for line in fh:
                line = line.rstrip("\n")
                if not in_front:
                    if line == "---":
                        in_front = True
                    continue
                if line == "---":
                    return None
                if line.startswith("status:"):
                    return line[len("status:"):].strip()
    except OSError:
        pass
    return None


def status_from_text(text):
    """Parse frontmatter status out of a raw file text blob (git HEAD content)."""
    in_front = False
    for line in text.splitlines():
        if not in_front:
            if line == "---":
                in_front = True
            continue
        if line == "---":
            return None
        if line.startswith("status:"):
            return line[len("status:"):].strip()
    return None


def read_old_status(file_path):
    """Return status from the git HEAD version of file_path, or None.

    Uses `git -C <dir>` so it works inside worktrees (a repo-root-relative
    pathspec would fatal when the hook cwd is outside the file's repo). None
    covers a new/renamed file (not in HEAD) -- caller treats that as born-done.
    """
    directory = os.path.dirname(os.path.abspath(file_path))
    basename = os.path.basename(file_path)
    try:
        out = subprocess.run(
            ["git", "-C", directory, "show", "HEAD:./" + basename],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return status_from_text(out.stdout)


# Statuses that leave every owner surface -- no display title needed.
DISPLAY_TITLE_EXEMPT = frozenset(["done", "dismissed"])


def owner_lang():
    """AGENT_LANG base code (env DOGANY_LANG -> conf AGENT_LANG -> "en"),
    the same resolution status-footer.py _owner_lang uses."""
    lang = ((os.environ.get("DOGANY_LANG") or "").strip()
            or (_conf_get("AGENT_LANG") or "").strip() or "en")
    return re.split(r"[-_.]", lang.lower(), maxsplit=1)[0] or "en"


REGISTER_ORIGINAL = (
    "DISPLAY_NAMES=off (builder owner) -> keep technical terms in their "
    "original form (e.g. lifekit, pack, rollback, E2E smoke); never "
    "paraphrase them into plain words.")
REGISTER_PLAIN = (
    "DISPLAY_NAMES on (end-user owner) -> plain everyday words, no "
    "technical jargon.")


def display_title_register():
    """Title register line from DISPLAY_NAMES (env DOGANY_DISPLAY_NAMES ->
    conf DISPLAY_NAMES -> on).  Only an explicit off-word selects the
    original-term register."""
    v = ((os.environ.get("DOGANY_DISPLAY_NAMES") or "").strip()
         or (_conf_get("DISPLAY_NAMES") or "").strip()).lower()
    if v in ("off", "0", "false", "no"):
        return REGISTER_ORIGINAL
    return REGISTER_PLAIN


def has_display_title(path, lang):
    """True if the frontmatter carries a non-empty display_title.<lang>."""
    key = "display_title.%s:" % lang
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            in_front = False
            for line in fh:
                line = line.rstrip("\n")
                if not in_front:
                    if line == "---":
                        in_front = True
                    continue
                if line == "---":
                    return False
                if line.startswith(key):
                    return line[len(key):].strip().strip("\"'") != ""
    except OSError:
        return True  # fail open
    return False


def has_evidence(file_path):
    """True if the file contains any value-companion evidence token."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError:
        return True  # fail open -- cannot read, do not nag
    return any(p.search(body) for p in EVIDENCE_PATTERNS)


def is_worklog_ticket(file_path, glob=None):
    """Return True if file_path is a worklog/<glob> file, not a reserved
    ledger/template (basename starting with "_")."""
    basename = os.path.basename(file_path)
    if basename.startswith("_") or not basename.endswith(".md"):
        return False
    glob = glob if glob is not None else _ticket_file_glob()
    if not fnmatch.fnmatch(basename, glob):
        return False
    parent = os.path.basename(os.path.dirname(os.path.normpath(file_path)))
    return parent == "worklog"


def emit(msg):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")
        if tool_name not in ("Edit", "Write", "MultiEdit"):
            sys.exit(0)

        tool_input = data.get("tool_input") or {}
        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path.strip():
            sys.exit(0)

        file_path = file_path.strip()
        if not is_worklog_ticket(file_path):
            sys.exit(0)

        status = read_frontmatter_status(file_path)
        if status is None:
            sys.exit(0)

        fname = os.path.basename(file_path)

        # Check (1): enum value.
        if status not in VALID_STATUS:
            emit(
                "[status-enum-lint] VIOLATION in {fname}: status: '{val}' is not a "
                "valid enum value. Allowed: open | wip | blocked | done | parked | "
                "backlog | dismissed. Fix the frontmatter status field before "
                "proceeding.".format(fname=fname, val=status)
            )
            sys.exit(0)

        # Check (2): transition INTO done must carry evidence (logged != done).
        # Old status None (new/renamed/born-done) counts as a transition.
        if status == "done":
            old = read_old_status(file_path)
            if old != "done" and not has_evidence(file_path):
                emit(
                    "[status-enum-lint] FORCING-POINT in {fname}: status transitioned "
                    "to 'done' but no evidence found (logged != done). A done entry "
                    "must carry a value-companion evidence token: merge_sha: <sha>, "
                    "branch_sha: <sha>, or verifier_verdict: PASS. Add the evidence "
                    "or revert the status to its in-progress value.".format(fname=fname)
                )
                sys.exit(0)

        # Check (3): owner-language display title (DGN-1754).
        if status not in DISPLAY_TITLE_EXEMPT:
            lang = owner_lang()
            if not has_display_title(file_path, lang):
                emit(
                    "[status-enum-lint] DISPLAY-TITLE missing in {fname}: add "
                    "'display_title.{lang}: <title in the owner's language>' to "
                    "the frontmatter. Owner surfaces (workbench) show only the "
                    "ticket id without it; 'title' stays the internal key. "
                    "Register: {register}"
                    .format(fname=fname, lang=lang,
                            register=display_title_register())
                )

        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
