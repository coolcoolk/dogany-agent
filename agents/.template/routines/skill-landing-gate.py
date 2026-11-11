#!/usr/bin/env python3
"""PreToolUse forcing point: a new skill's landing location is the USER's call.

Blocks the write that lands a NEW SKILL.md unless a fresh declaration for that
exact skill exists in <instance>/.claude/.skill-landing-declared.

WHY (DGN-1394).  The skill-creator standard says "classify it, then route",
but the sentence has no subject: nothing ever puts the question to the USER,
so the authoring agent classifies alone and reflexively picks the narrowest
scope.  Live case: an agent authored a skill, never asked, landed it
instance-local; the owner then wanted a sibling agent to use it, which cost a
five-artifact retrofit.  Prose cannot fix this (the prose is what failed), and
a scanner is not enforcement -- it reports after the fact.  So the rule is
wired to the decision point it governs: the write itself.

TRIGGER CHOICE.  The gate fires on the Write/Edit/MultiEdit that CREATES a
SKILL.md, NOT on invocation of the skill-creator skill.  The live case is
exactly the bypass an invocation-keyed gate would miss: the agent authored the
file without ever invoking the skill.  Keying on the artifact closes that path
by construction -- there is no way to land a skill without writing its
SKILL.md.

DECLARATION SHAPE -- two independent axes, not one enum:
  location: local | shared | framework
      local     = this instance only
      shared    = <DOGANY_HOME>/shared-skills, the incubation tier
                  (agent-writable, agent-driven promotion flow)
      framework = product canonical, carried by a release
  activate_for: <none | name, name, ...>
      per-agent activation fan-out (symlinks).  Multi-select, default OFF.

`crew` is deliberately NOT selectable.  The crew mount
(<DOGANY_HOME>/crews/<kit>/shared-skills) has exactly one writer, the pack
installer; offering it to an authoring agent would manufacture a one-writer
violation on every single use.  A write into that mount is denied outright.

BINDING.  The declaration names the skill it answers for, so one answer cannot
silently authorize the next skill.  A TTL is a second guard against an ancient
declaration being reused.

DURABLE RECORD (DGN-1476).  The declaration expires with its TTL, so the
answer it carries evaporates minutes after landing -- the live incident left
"where / whose / what scope" all unanswerable POST-landing even though the
gate had run.  So the USER's answer must also land in the artifact itself: a
Write that creates a SKILL.md must carry a frontmatter line
`scope: <declared location>`.  Missing or mismatching -> deny.  Only Write is
judged (it is the creation path and the only payload carrying full content);
Edit/MultiEdit payloads have no full content to read, so they stay under the
declaration checks alone.

Model: mirror routines/decomp-gate.py (declare -> sentinel -> allow) and the
I/O contract of routines/cc-memory-write-guard.py:
  - reads the PreToolUse JSON payload from stdin
  - emits deny via hookSpecificOutput/permissionDecision JSON on stdout
  - always exits 0 (the decision field, not the exit code, signals deny)

Fails open on any error: a gate malfunction must never break the agent.
"""
import json
import os
import sys
import time

SENTINEL_NAME = ".skill-landing-declared"
DECLARATION_TTL_SEC = 3600  # one authoring session
GATED_TOOLS = {"Write", "Edit", "MultiEdit"}
SKILL_FILE = "SKILL.md"

LOCATIONS = ("local", "shared", "framework")
NO_ACTIVATION = ("none", "-", "off", "[]")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'lib'))
from hook_paths import instance_root


def _home():
    """Machine-global dogany home (same resolution as routines/skill-usage.py)."""
    return os.environ.get("DOGANY_HOME") or os.path.expanduser("~/.dogany")


def _shared_root():
    return os.path.join(_home(), "shared-skills")


def _crews_root():
    return os.path.join(_home(), "crews")


# --- central switch (DGN-1231) ---------------------------------------------

_GATE_SLUG = "skill-landing"
_GATE_SWITCH_PATH = os.path.join(_HERE, "gate-switch.py")


def _load_gate_switch():
    """Central gate on/off switch (DGN-1231).  None -> switch machinery
    absent/broken: the gate stays armed (fail toward protection)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gate_switch", _GATE_SWITCH_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _deny_or_notice(reason, cwd=None):
    """Consulted only at the would-deny moment: switched off -> emit a
    non-blocking off-notice instead of the deny (DGN-1231 visibility)."""
    gs = _load_gate_switch()
    if gs is not None:
        try:
            if not gs.is_enabled(_GATE_SLUG, cwd):
                print(json.dumps(gs.off_notice(_GATE_SLUG, reason, cwd),
                                 ensure_ascii=False))
                return
        except Exception:
            pass  # broken switch never disarms the gate
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))


# --- path predicates --------------------------------------------------------

def _under(path, root):
    """True if `path` sits inside directory `root` (segment-anchored)."""
    try:
        path = os.path.normpath(path)
        root = os.path.normpath(root)
    except Exception:
        return False
    return path == root or path.startswith(root + os.sep)


def _in_crew_mount(path):
    """True if the target is inside an installer-owned crew shared mount:
    <DOGANY_HOME>/crews/<kit>/shared-skills/..."""
    if not _under(path, _crews_root()):
        return False
    rel = os.path.relpath(os.path.normpath(path),
                          os.path.normpath(_crews_root()))
    parts = rel.split(os.sep)
    return len(parts) >= 2 and parts[1] == "shared-skills"


def _agents_root():
    return os.path.join(_home(), "agents")


def _instance_of(path):
    """'<X>' when path sits in <DOGANY_HOME>/agents/<X>/..., else None.
    Judged on both the lexical and the resolved path, so a symlink hop
    neither hides nor fakes the owning instance."""
    out = set()
    for root in {os.path.normpath(_agents_root()), os.path.realpath(_agents_root())}:
        for p in {os.path.normpath(path), os.path.realpath(path)}:
            if _under(p, root) and p != root:
                out.add(os.path.relpath(p, root).split(os.sep)[0])
    return out


def _skill_tree_owners(path):
    """Instances whose .claude/skills*/ tree the path writes into."""
    owners = set()
    for root in {os.path.normpath(_agents_root()), os.path.realpath(_agents_root())}:
        for p in {os.path.normpath(path), os.path.realpath(path)}:
            if not _under(p, root) or p == root:
                continue
            parts = os.path.relpath(p, root).split(os.sep)
            if len(parts) >= 4 and parts[1] == ".claude" and parts[2].startswith("skills"):
                owners.add(parts[0])
    return owners


def _in_installed_skill_tree(path):
    """True when the lexical or resolved target is in an installed tree.

    Installed trees are the current instance's .claude/skills directory, any
    instance .claude/skills* directory under DOGANY_HOME/agents, the shared
    incubation root, and an installer-owned crew shared-skills mount.  Checking
    both views prevents ``..`` and symlink hops from escaping this boundary.
    """
    paths = {os.path.normpath(path), os.path.realpath(path)}
    current_roots = {
        os.path.normpath(os.path.join(instance_root(), ".claude", "skills")),
        os.path.realpath(os.path.join(instance_root(), ".claude", "skills")),
    }
    shared_roots = {
        os.path.normpath(_shared_root()), os.path.realpath(_shared_root())}
    for candidate in paths:
        if any(_under(candidate, root) for root in current_roots | shared_roots):
            return True
        if _skill_tree_owners(candidate) or _in_crew_mount(candidate):
            return True
    return False


def _caller_root(cwd):
    """Realpath of the caller's instance root: <agents>/<X> when cwd sits in
    an instance under DOGANY_HOME, else cwd itself."""
    real = os.path.realpath(cwd)
    root = os.path.realpath(_agents_root())
    if _under(real, root) and real != root:
        return os.path.join(root, os.path.relpath(real, root).split(os.sep)[0])
    return real


def _cross_root_owner(path, cwd):
    """DGN-1910 F5: the other instance whose skill tree this write lands in,
    or None. New AND existing files: a leader never edits a newborn's skills
    after the mint (the machine render does not use the Write tool)."""
    caller = os.path.basename(_caller_root(cwd))
    caller_in_home = bool(_instance_of(cwd))
    for owner in sorted(_skill_tree_owners(path)):
        if not caller_in_home or owner != caller:
            return owner
    return None


def _is_new_skill_file(path):
    """True if this write CREATES a SKILL.md (an existing one is an edit of an
    already-landed skill, whose location question was answered at landing)."""
    if os.path.basename(path) != SKILL_FILE:
        return False
    return not os.path.exists(path)


# --- declaration ------------------------------------------------------------

def _read_declaration(sentinel):
    """Parse the sentinel into a key -> value map.

    Returns None when there is no usable declaration (absent / expired), and
    {} when the file exists and is fresh but carries no parsable key.
    Raises nothing: unreadable/unstattable sentinels raise OSError to the
    caller's fail-open handler.
    """
    if not os.path.exists(sentinel):
        return None
    age = time.time() - os.path.getmtime(sentinel)
    if not (0 <= age <= DECLARATION_TTL_SEC):
        return None  # expired, or future mtime (clock skew / tamper)
    text = open(sentinel, encoding="utf-8", errors="replace").read()
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


def _declared_skill_matches(declared, target_path, target_name):
    """The declaration must name THIS skill: its name, its directory, or the
    SKILL.md path itself all count."""
    value = (declared or "").strip().rstrip("/\\")
    if not value:
        return False
    if os.path.normpath(value) == os.path.normpath(target_path):
        return True
    base = os.path.basename(value) or value
    if base == SKILL_FILE:
        base = os.path.basename(os.path.dirname(value))
    return base == target_name


def _frontmatter_scope(content):
    """Return the `scope:` value from the leading frontmatter block, or None.

    None means "no readable scope line": absent block, unterminated block,
    or a block without the key all look the same to the write being judged.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return None
        if stripped.lower().startswith("scope:"):
            return stripped.partition(":")[2].strip().lower()
    return None


def _activation_targets(raw):
    """Parse activate_for into a list of agent names.  Absent value, or any of
    the explicit 'no activation' spellings, yields an empty list."""
    value = (raw or "").strip()
    if not value or value.lower() in NO_ACTIVATION:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


# --- messages ---------------------------------------------------------------

def _declare_instruction(sentinel, skill_name):
    return (
        "mkdir -p '" + os.path.dirname(sentinel) + "' && printf "
        "'skill: " + skill_name + "\\nlocation: <local|shared|framework>\\n"
        "activate_for: <none | name, name>\\n' > '" + sentinel + "'"
    )


def _ask_reason(sentinel, skill_name, head):
    return (
        head + " Landing a skill decides WHO may use it, and that choice "
        "belongs to the USER, not to the authoring agent. ASK THE USER, in "
        "their own language, before this write. Two independent axes. "
        "(1) location -- exactly one of: "
        "local = this instance only; "
        "shared = " + _shared_root() + " (machine-wide incubation tier, "
        "agent-writable); "
        "framework = product canonical, ships with a release. "
        "The crew mount (" + _crews_root() + "/<kit>/shared-skills) is NOT an "
        "option: it has a single writer, the pack installer. "
        "(2) activate_for -- which other agents get the skill activated "
        "(per-agent symlink fan-out). Multi-select, default OFF; "
        "location: local requires activate_for: none. "
        "Record the USER's answer, then retry this write: " +
        _declare_instruction(sentinel, skill_name) +
        " . Do not answer on the user's behalf, and do not pick the narrowest "
        "scope by default."
    )


def update_checker():
    import importlib.util
    spec = importlib.util.spec_from_file_location('skill_update_layer', os.path.join(_HERE, 'skill-layer.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def existing_update_reason(data, path):
    if os.path.basename(path) != SKILL_FILE or not os.path.isfile(path):
        return None
    try:
        layer = update_checker()
        session, turn = layer.update_context(data)
        with open(path, encoding='utf-8') as fh:
            before = fh.read()
        ti = data.get('tool_input') or {}
        tool = data.get('tool_name')
        if tool == 'Write':
            candidate = ti['content']
        else:
            candidate = before
            edits = ti.get('edits', []) if tool == 'MultiEdit' else [ti]
            for edit in edits:
                old, new = edit['old_string'], edit['new_string']
                if not old or old not in candidate:
                    return 'skill update refused: missing edit anchor'
                if not edit.get('replace_all') and candidate.count(old) != 1:
                    return 'skill update refused: ambiguous edit anchor'
                candidate = candidate.replace(old, new, -1 if edit.get('replace_all') else 1)
        bad, _ = layer.verify_update(instance_root(), path, session, turn, candidate)
        if bad:
            return 'skill update refused: ' + ', '.join(bad)
    except (OSError, ValueError, KeyError, TypeError):
        return ('skill update refused: missing or invalid u1 plan. Run skill-layer.py '
                'update-begin with target, section/old/new mapping, session, turn, '
                'authorization reference and scope; replace the owning instruction in place.')
    return None


# --- main -------------------------------------------------------------------

def main():
    try:
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)  # fail open: bad input never blocks

        tool_name = data.get("tool_name", "")
        if tool_name not in GATED_TOOLS:
            sys.exit(0)

        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            sys.exit(0)
        file_path = tool_input.get("file_path") or ""
        if not isinstance(file_path, str) or not file_path.strip():
            sys.exit(0)  # no target -> cannot judge -> allow
        file_path = file_path.strip()

        cwd = str(data.get("cwd") or os.getcwd())
        file_path = os.path.abspath(os.path.join(cwd, file_path))

        # DGN-2398 u1: the surviving landing guard is scoped to the shared
        # incubation tree. Installed/local skill updates use the u1 flow and
        # must not revive the retired procedural landing refusal.
        if not _under(file_path, _shared_root()):
            sys.exit(0)

        # DGN-1910 F5: another instance's skill tree is never this caller's
        # to write, new file or existing.  Evaluated before the new-file-only
        # path.  A cross-root role (the estate PM) switches the gate off via
        # the gate-state switch; there is no code exception.
        other = _cross_root_owner(file_path, cwd)
        if other is not None:
            _deny_or_notice(
                "This write targets the skills of another agent ('" + other +
                "', under " + _agents_root() + "). An agent edits only its "
                "own skills; the other agent changes its skills in its own "
                "chat (skill-creator update flow). Ask the USER to make the "
                "change there instead.", cwd)
            sys.exit(0)

        # The creator's declaration-only probe is read-only. Existing-skill
        # writes still pass the update checker on every actual hook call.
        if '--declaration-only' in sys.argv and os.path.isfile(file_path):
            sys.exit(0)
        installed = _in_installed_skill_tree(file_path)
        if installed:
            update_reason = existing_update_reason(data, file_path)
            if update_reason:
                _deny_or_notice(update_reason, cwd)
                sys.exit(0)
        if not _is_new_skill_file(file_path):
            sys.exit(0)  # not a new skill landing -> unaffected
        if not installed:
            # Product-repository source SKILL.md files are governed by that
            # repository's review and merge gate, not installed-skill u1 or
            # landing declarations.
            sys.exit(0)

        skill_name = os.path.basename(os.path.dirname(file_path)) or "<skill>"
        sentinel = os.path.join(instance_root(), ".claude", SENTINEL_NAME)

        # Installer-owned mount: never authorable by an agent, declaration or
        # not.  Denied before the declaration is even consulted.
        if _in_crew_mount(file_path):
            _deny_or_notice(
                "Crew shared mount is installer-owned (single writer: the "
                "pack installer). An agent may not author a skill into " +
                _crews_root() + "/<kit>/shared-skills. Ask the USER which of "
                "the three authorable locations applies -- local (this "
                "instance only), shared (" + _shared_root() + ", the "
                "agent-writable incubation tier), or framework (product "
                "canonical, release-borne) -- and write it there instead. "
                "There is no migration between the crew mount and the shared "
                "tier.", cwd)
            sys.exit(0)

        try:
            fields = _read_declaration(sentinel)
        except OSError:
            sys.exit(0)  # cannot stat/read the sentinel -> fail open

        if fields is None:
            _deny_or_notice(_ask_reason(
                sentinel, skill_name,
                "This write creates a NEW skill '" + skill_name +
                "' and no landing declaration is on file."), cwd)
            sys.exit(0)

        if not _declared_skill_matches(fields.get("skill"), file_path,
                                       skill_name):
            _deny_or_notice(_ask_reason(
                sentinel, skill_name,
                "The landing declaration on file names skill '" +
                (fields.get("skill") or "<missing>") + "', not '" +
                skill_name + "'. One skill's answer does not carry to "
                "another."), cwd)
            sys.exit(0)

        location = (fields.get("location") or "").strip().lower()
        if not location:
            _deny_or_notice(_ask_reason(
                sentinel, skill_name,
                "The landing declaration for '" + skill_name +
                "' has no location."), cwd)
            sys.exit(0)
        if location not in LOCATIONS:
            _deny_or_notice(
                "Unknown landing location '" + (fields.get("location") or "") +
                "' declared for skill '" + skill_name + "'. Valid values are "
                "exactly: local (this instance only), shared (" +
                _shared_root() + ", the agent-writable incubation tier), "
                "framework (product canonical, release-borne). 'crew' is not "
                "among them -- the crew mount has a single writer, the pack "
                "installer. Ask the USER which one applies and rewrite the "
                "declaration: " + _declare_instruction(sentinel, skill_name),
                cwd)
            sys.exit(0)

        if "activate_for" not in fields:
            _deny_or_notice(_ask_reason(
                sentinel, skill_name,
                "The landing declaration for '" + skill_name + "' answers "
                "location but not activate_for; both axes must be answered "
                "(write 'activate_for: none' if no other agent gets it)."),
                cwd)
            sys.exit(0)

        targets = _activation_targets(fields.get("activate_for"))

        if location == "local" and targets:
            _deny_or_notice(
                "Contradictory landing declaration for skill '" + skill_name +
                "': location 'local' means this instance only, so "
                "activate_for must be empty, but it lists " +
                ", ".join(targets) + ". Ask the USER to settle it: either the "
                "skill is local and no other agent gets it (activate_for: "
                "none), or other agents do get it and the location is shared "
                "(" + _shared_root() + ") or framework. Then rewrite: " +
                _declare_instruction(sentinel, skill_name), cwd)
            sys.exit(0)

        # DGN-1910 F5: location local = this instance only, so the target
        # must sit under the caller's own instance root.
        if location == "local" and not _under(os.path.realpath(file_path),
                                              _caller_root(cwd)):
            _deny_or_notice(
                "Landing declaration says location 'local' (this instance "
                "only), but " + file_path + " is outside this instance's "
                "root (" + _caller_root(cwd) + "). Write the skill under "
                "this instance's .claude/skills/, or ask the USER whether "
                "the location is really shared or framework.", cwd)
            sys.exit(0)

        # Path/declaration consistency, checked only where it is mechanical:
        # the shared tier is an exact path prefix.  local vs framework cannot
        # be told apart from a path, so neither is checked.
        in_shared = _under(file_path, _shared_root())
        if in_shared and location != "shared":
            _deny_or_notice(
                "Landing declaration says location '" + location + "' but "
                "this write targets the shared incubation tier (" +
                _shared_root() + "). Declaration and destination must agree. "
                "Confirm with the USER which one is right, then fix the "
                "declaration or the path.", cwd)
            sys.exit(0)
        if location == "shared" and not in_shared:
            _deny_or_notice(
                "Landing declaration says location 'shared', so the skill "
                "must be written under " + _shared_root() + " (the "
                "agent-writable incubation tier), not " + file_path + ". "
                "Declaration and destination must agree.", cwd)
            sys.exit(0)

        # Durable record (DGN-1476): the declaration dies with its TTL, so
        # the USER's answer must also live in the artifact.  Judged on Write
        # only -- the creation path, and the only payload carrying content.
        if tool_name == "Write":
            content = tool_input.get("content")
            if isinstance(content, str):
                scope = _frontmatter_scope(content)
                if scope is None:
                    _deny_or_notice(
                        "The new skill '" + skill_name + "' has a valid "
                        "landing declaration, but the SKILL.md being written "
                        "does not record it: add a frontmatter line "
                        "'scope: " + location + "' (the declared location, "
                        "i.e. the USER's answer). The declaration file "
                        "expires; the scope line is the durable record that "
                        "survives it. Then retry this write.", cwd)
                    sys.exit(0)
                if scope != location:
                    _deny_or_notice(
                        "The SKILL.md for '" + skill_name + "' declares "
                        "'scope: " + scope + "' but the landing declaration "
                        "says location '" + location + "'. The two must "
                        "agree -- the scope line is the durable copy of the "
                        "USER's landing answer, not a second opinion. Fix "
                        "whichever is wrong (re-ask the USER if unsure), "
                        "then retry this write.", cwd)
                    sys.exit(0)

        sys.exit(0)  # declared and consistent -> allow
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # honor the fail-open contract on any unexpected error


if __name__ == "__main__":
    main()
