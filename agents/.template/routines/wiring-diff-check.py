#!/usr/bin/env python3
"""wiring-diff-check.py -- report hook wiring an instance has lost or doubled.

DGN-1658 (generalises the one-workspace detector behind DGN-1250). update.sh
regenerates .claude/settings.json and has dropped hooks before; a pack or a
hand edit can also register one command twice. This reads, never writes.

  baseline = the "# declare:" lines in this header (framework wiring)
             UNION routines/wiring-snapshot.custom.json (instance-owned,
             never touched by an update -- the *.custom.* convention)
  actual   = .claude/settings.json UNION .claude/settings.local.json

A baseline hook absent from actual is LOST. An (event, command) registered
more than once across actual is a DUPLICATE. A hook wired ONLY in
.claude/settings.local.json whose command runs a framework-owned routine
(a __PROJECT_ROOT__/routines/ path that is not *.custom.*) and that neither
the header nor the instance snapshot declares is LOCAL-ONLY: that file is
machine-local and gitignored, so the mechanism exists on one machine and can
never ship, update or be audited (DGN-1341 -- decomp-gate wired that way).
Ship it in agents/.template/.claude/settings.json, or declare it in the
instance snapshot. Every count names its unit.

Usage: wiring-diff-check.py --root <instance root> [--home <dir>]
Exit:  0 clean / 1 lost, duplicate or local-only found / 2 actual wiring unreadable.

wiring-snapshot.custom.json shape (any entry form; matcher omitted = any):
  {"wiring": [{"event": "Stop", "matcher": "Bash", "command": "..."},
              ["Stop", "routines/my-gate.py"]]}
A command without spaces is a path fragment: it matches any actual command
carrying a token that ends with it. Commands are compared with the instance
root written as __PROJECT_ROOT__ and $HOME as __HOME__.

Framework declarations -- kept equal to agents/.template/.claude/settings.json
by routines/tests/test-wiring-diff-check.py (event :: matcher or - :: command):
# declare: PreToolUse :: * :: /usr/bin/python3 __PROJECT_ROOT__/routines/skill-update-track.py
# declare: PreToolUse :: Skill|Workflow|Agent|Task :: /usr/bin/python3 __PROJECT_ROOT__/routines/usage-gate.py
# declare: PreToolUse :: Edit|Write|MultiEdit|NotebookEdit|Bash :: ONBOARDING_FILE=__PROJECT_ROOT__ WRITE_PATHS_D4_MODE=detect /usr/bin/python3 __PROJECT_ROOT__/routines/write-paths-gate.py
# declare: PreToolUse :: Bash :: BASH_GATE_MODE=detect /usr/bin/python3 __PROJECT_ROOT__/routines/bash-safety-gate.py
# declare: PreToolUse :: Write|Edit|MultiEdit :: /usr/bin/python3 __PROJECT_ROOT__/routines/done-claim-gate.py
# declare: PreToolUse :: Write|Edit|MultiEdit :: /usr/bin/python3 __PROJECT_ROOT__/routines/durable-write-pointer.py
# declare: PreToolUse :: Bash|Edit|Write|MultiEdit|Agent|Task|Skill|Workflow :: /usr/bin/python3 __PROJECT_ROOT__/routines/dispatch-return-result-first-gate.py
# declare: PostToolUse :: Bash :: /usr/bin/python3 __PROJECT_ROOT__/routines/card-followup.py
# declare: PostToolUse :: Skill :: /usr/bin/python3 __PROJECT_ROOT__/routines/skill-feedback-gate.py --record
# declare: PostToolUse :: TaskStop :: /usr/bin/python3 __PROJECT_ROOT__/routines/subagent-stop-record.py
# declare: PostToolUse :: Bash|Monitor|Workflow|TaskStop :: /usr/bin/python3 __PROJECT_ROOT__/routines/bg-job-notice.py --launched
# declare: PostToolUse :: Agent|Task :: /usr/bin/python3 __PROJECT_ROOT__/routines/fable-death-catch.py
# declare: PostToolUse :: Edit|Write|MultiEdit|Bash :: ONBOARDING_FILE=__PROJECT_ROOT__ /usr/bin/python3 __PROJECT_ROOT__/routines/onboarding-gate-stop.py
# declare: PostToolUse :: Edit|Write|MultiEdit :: /usr/bin/python3 __PROJECT_ROOT__/routines/ticket-status-enum-lint.py
# declare: PostToolUse :: * :: /usr/bin/python3 __PROJECT_ROOT__/routines/text-layer-pointer.py
# declare: SessionStart :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/session-recap.py
# declare: SessionStart :: - :: ONBOARDING_FILE=__PROJECT_ROOT__ /usr/bin/python3 __PROJECT_ROOT__/routines/onboarding-check.py
# declare: SessionStart :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/version-check.py
# declare: SessionStart :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/bridge-stale-check.py
# declare: SessionStart :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/loader-manifest.py
# declare: SessionStart :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/terms.py block
# declare: UserPromptSubmit :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/skill-update-track.py
# declare: UserPromptSubmit :: - :: ONBOARDING_FILE=__PROJECT_ROOT__ /usr/bin/python3 __PROJECT_ROOT__/routines/onboarding-gate-stop.py
# declare: UserPromptSubmit :: - :: /usr/bin/python3 __PROJECT_ROOT__/memory-engine/memory.py hook
# declare: UserPromptSubmit :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/why-net.py ups
# declare: UserPromptSubmit :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/decision-ledger.py ups
# declare: UserPromptSubmit :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/text-layer-pointer.py
# declare: Stop :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/status-footer.py
# declare: Stop :: - :: /usr/bin/python3 __PROJECT_ROOT__/routines/turn-frame.py
"""
import argparse
import json
import os
import sys
from collections import Counter

SNAPSHOT_REL = "routines/wiring-snapshot.custom.json"
SETTINGS_RELS = (".claude/settings.json", ".claude/settings.local.json")


def header_declarations(text=None):
    """(event, matcher-or-None, command) for each '# declare:' header line."""
    if text is None:
        text = __doc__ or ""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("# declare:"):
            continue
        parts = [p.strip() for p in line[len("# declare:"):].split(" :: ", 2)]
        if len(parts) == 3:
            out.append((parts[0], None if parts[1] == "-" else parts[1], parts[2]))
    return out


def hook_items(data):
    """(event, matcher-or-None, command) for every command hook, in order."""
    out = []
    blocks = 0
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out, blocks
    for event, lst in hooks.items():
        if not isinstance(lst, list):
            continue
        for block in lst:
            if not isinstance(block, dict):
                continue
            blocks += 1
            matcher = block.get("matcher") or None
            if isinstance(block.get("command"), str):
                out.append((event, matcher, block["command"]))
                continue
            for h in block.get("hooks") or []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.append((event, matcher, h["command"]))
    return out, blocks


def snapshot_declarations(path):
    """Entries of the instance snapshot; missing file -> []; bad -> ValueError."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    rows = data.get("wiring") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("%s: no 'wiring' list" % path)
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("event") and row.get("command"):
            out.append((row["event"], row.get("matcher") or None, row["command"]))
        elif isinstance(row, list) and len(row) == 2:
            out.append((row[0], None, row[1]))
        elif isinstance(row, list) and len(row) == 3:
            out.append((row[0], row[1] or None, row[2]))
        else:
            raise ValueError("%s: unreadable entry %r" % (path, row))
    return out


def normalise(cmd, root, home):
    if root:
        cmd = cmd.replace(root.rstrip("/"), "__PROJECT_ROOT__")
    if home:
        cmd = cmd.replace(home.rstrip("/"), "__HOME__")
    return " ".join(cmd.split())


def matches(want, have):
    w_event, w_matcher, w_cmd = want
    h_event, h_matcher, h_cmd = have
    if w_event != h_event or (w_matcher is not None and w_matcher != h_matcher):
        return False
    if w_cmd == h_cmd:
        return True
    if " " in w_cmd:
        return False
    return any(t.strip("'\"").endswith(w_cmd) for t in h_cmd.replace("=", " ").split())


def diff(baseline, actual):
    """-> (lost baseline entries, [(event, command, registrations)])."""
    lost = [w for w in dict.fromkeys(baseline)
            if not any(matches(w, h) for h in actual)]
    counts = Counter((e, c) for e, _, c in actual)
    dups = [(e, c, n) for (e, c), n in counts.items() if n > 1]
    return lost, dups


FRAMEWORK_ROUTINE_PREFIX = "__PROJECT_ROOT__/routines/"


def framework_routine(cmd):
    """True when a normalised command runs a framework-owned routine."""
    for tok in cmd.replace("=", " ").split():
        tok = tok.strip("'\"")
        if tok.startswith(FRAMEWORK_ROUTINE_PREFIX) and ".custom." not in tok:
            return True
    return False


def local_only(local, declared):
    """Hooks of settings.local.json that run a framework routine nobody declares."""
    return [h for h in dict.fromkeys(local)
            if framework_routine(h[2]) and not any(matches(w, h) for w in declared)]


def run(root, home, out):
    actual, blocks, local = [], 0, []
    for rel in SETTINGS_RELS:
        path = os.path.join(root, rel)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            if rel == SETTINGS_RELS[0]:
                out.write("wiring-diff: ERROR %s missing -- wiring unchecked\n" % rel)
                return 2
            continue
        except (OSError, ValueError) as exc:
            out.write("wiring-diff: ERROR %s unreadable (%s) -- wiring unchecked\n"
                      % (rel, exc.__class__.__name__))
            return 2
        items, n = hook_items(data)
        actual += items
        blocks += n
        if rel == SETTINGS_RELS[1]:
            local = items
    actual = [(e, m, normalise(c, root, home)) for e, m, c in actual]
    local = [(e, m, normalise(c, root, home)) for e, m, c in local]
    baseline = [(e, m, normalise(c, root, home)) for e, m, c in header_declarations()]
    try:
        baseline += [(e, m, normalise(c, root, home))
                     for e, m, c in snapshot_declarations(os.path.join(root, SNAPSHOT_REL))]
    except (OSError, ValueError) as exc:
        out.write("wiring-diff: WARN %s unreadable (%s) -- instance baseline skipped\n"
                  % (SNAPSHOT_REL, exc))
    lost, dups = diff(baseline, actual)
    unshipped = local_only(local, baseline)
    out.write("wiring-diff: events %d, matcher_blocks %d, individual_hook_commands %d, "
              "duplicate_commands %d, baseline_hooks %d, lost_hooks %d, "
              "local_only_hooks %d\n"
              % (len({e for e, _, _ in actual}), blocks, len(actual), len(dups),
                 len(set(baseline)), len(lost), len(unshipped)))
    for e, m, c in lost:
        out.write("wiring-diff: LOST hook -- event %s, matcher %s, command %s\n"
                  % (e, m or "(any)", c))
    for e, c, n in dups:
        out.write("wiring-diff: DUPLICATE command -- event %s, registrations %d, command %s\n"
                  % (e, n, c))
    for e, m, c in unshipped:
        out.write("wiring-diff: LOCAL-ONLY hook (machine-local, never ships) -- event %s, "
                  "matcher %s, command %s\n" % (e, m or "(any)", c))
    return 1 if lost or dups or unshipped else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--home", default=os.environ.get("HOME", ""))
    args = ap.parse_args(argv)
    return run(os.path.abspath(args.root), args.home, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
