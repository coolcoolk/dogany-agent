#!/usr/bin/env python3
# test-lane: fast
"""Locks for cc-memory-write-guard.py (DGN-479), run through
write-paths-gate.py (the wired hook since v2.7 merge #9, dec-278).

The memory rule had no fixture of its own before the merge; these cases
pin its pre-merge behaviour so the merge can show the same verdicts at
the new home. GATE_UNDER_TEST=cc-memory-write-guard.py runs the same
cases against the standalone rule script (cases marked merged-only are
skipped there).

Cases:
  A  Write into the CC shadow store .claude/projects/<slug>/memory/  -> DENY
  B  Edit / MultiEdit into the engine store memories/               -> DENY
  C  path that only CONTAINS "memories" (not a segment)             -> ALLOW
  D  ordinary file                                                  -> ALLOW
  E  Bash mentioning memories/ (rule does not judge Bash)           -> ALLOW
  F  bad JSON / missing file_path                                   -> ALLOW
  G  merged-only: a new SKILL.md under memories/ trips BOTH the
     memory rule and the skill-landing rule -> one deny carrying both
     reasons, memory rule first (row order)
  H  merged-only: the `# wires-rules:` header names exactly the rule
     table's scripts (enforcement-claim-lint V2 reads that header)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
UNDER_TEST = os.environ.get("GATE_UNDER_TEST") or "write-paths-gate.py"
GATE = os.path.join(_HERE, "..", UNDER_TEST)
MERGED = UNDER_TEST == "write-paths-gate.py"

TMP = tempfile.mkdtemp(prefix="dgn479-")
CWD = os.path.join(TMP, "instance")
os.makedirs(CWD)

ENV = dict(os.environ)
ENV["DOGANY_HOME"] = os.path.join(TMP, "dogany-home")
ENV["GATE_SWITCH_CONF"] = os.path.join(TMP, "no-such-agent.conf")
for _k in ("GATE_ALL", "GATE_SKILL_LANDING", "GATE_ONBOARDING_OWNER",
           "ONBOARDING_FILE", "PROJECT_ROOT"):
    ENV.pop(_k, None)

_failures = []


def run(payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    res = subprocess.run([sys.executable, GATE], input=raw,
                         capture_output=True, text=True, timeout=15, env=ENV)
    if res.returncode != 0:
        _failures.append("non-zero exit %d: %s" % (res.returncode, res.stderr))
    out = res.stdout.strip()
    return json.loads(out) if out else {}


def reason(out):
    hso = out.get("hookSpecificOutput") or {}
    if hso.get("permissionDecision") != "deny":
        return None
    return hso.get("permissionDecisionReason", "")


def check(name, ok, detail=""):
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        _failures.append("%s %s" % (name, detail))


def edit(tool, path):
    ti = {"file_path": path}
    if tool == "Write":
        ti["content"] = "x\n"
    elif tool == "Edit":
        ti.update(old_string="a", new_string="b")
    else:
        ti["edits"] = [{"old_string": "a", "new_string": "b"}]
    return {"tool_name": tool, "cwd": CWD, "tool_input": ti}


def main():
    try:
        shadow = os.path.join(TMP, "home", ".claude", "projects", "-x-y",
                              "memory", "note.md")
        r = reason(run(edit("Write", shadow)))
        check("A shadow store write denied",
              r is not None and "Engine-owned memory" in r, r)

        engine = os.path.join(CWD, "memories", "people.md")
        for tool in ("Edit", "MultiEdit"):
            r = reason(run(edit(tool, engine)))
            check("B engine store %s denied" % tool,
                  r is not None and "Engine-owned memory" in r, r)

        check("C non-segment 'memories' allowed",
              run(edit("Write", os.path.join(CWD, "files", "memories-old",
                                             "a.md"))) == {})
        check("D ordinary file allowed",
              run(edit("Write", os.path.join(CWD, "files", "tmp",
                                             "a.md"))) == {})
        check("E Bash not judged",
              run({"tool_name": "Bash", "cwd": CWD,
                   "tool_input": {"command": "cat memories/people.md"}}) == {})
        check("F bad JSON fails open", run("{not json") == {})
        check("F missing file_path fails open",
              run({"tool_name": "Write", "cwd": CWD, "tool_input": {}}) == {})

        if MERGED:
            # lockstep: the `# wires-rules:` header (read by
            # enforcement-claim-lint V2) names exactly the table's scripts.
            import importlib.util
            spec = importlib.util.spec_from_file_location("wpg", GATE)
            wpg = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(wpg)
            with open(GATE, encoding="utf-8") as fh:
                hdr = [ln for ln in fh.read().splitlines()[:5]
                       if ln.startswith("# wires-rules:")]
            declared = [v.strip() for v in
                        hdr[0].split(":", 1)[1].split(",")] if hdr else []
            check("H wires-rules header == WRITE_PATH_RULES scripts",
                  declared == [r[2] for r in wpg.WRITE_PATH_RULES], declared)

            skill = os.path.join(ENV["DOGANY_HOME"], "shared-skills",
                                 "memories", "x", "SKILL.md")
            r = reason(run(edit("Write", skill)))
            ok = (r is not None and "Engine-owned memory" in r
                  and "landing declaration" in r
                  and r.index("Engine-owned memory")
                  < r.index("landing declaration"))
            check("G both rules deny -> one deny, both reasons, row order",
                  ok, r)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    if _failures:
        print("FAILED test-dgn479-memory-write-guard (%s): %s"
              % (UNDER_TEST, _failures))
        sys.exit(1)
    print("OK test-dgn479-memory-write-guard (%s)" % UNDER_TEST)


if __name__ == "__main__":
    main()
