#!/usr/bin/env python3
# test-lane: fast
"""Tier-1 tests for skill-landing-gate.py (DGN-1394).

Spec cases:
  A: Write of a NEW SKILL.md with no declaration              -> DENY
  B: same write after a valid declaration                     -> ALLOW
  C: location: local + non-empty activate_for                 -> DENY
  D: unknown location value                                   -> DENY, loudly
     (the message must name the bad value and all three valid ones)
  E: a write that is NOT a new SKILL.md                       -> ALLOW
     (E1 ordinary file, E2 SKILL.md that already exists, E3 non-write tool)
  F: BYPASS -- SKILL.md authored directly, no skill invoked   -> DENY
     (the payload carries no skill context at all; the gate is keyed on the
     artifact, so there is nothing to skip)

Supporting cases:
  G: declaration names a DIFFERENT skill                      -> DENY
  H: declaration answers location but omits activate_for      -> DENY
  I: expired declaration (older than the TTL)                 -> DENY
  J: write into the installer-owned crew shared mount         -> DENY
  K: location: shared + path outside the shared tier          -> DENY
  L: location: shared + path inside the shared tier           -> ALLOW
  M: bad JSON on stdin                                        -> fail-open ALLOW

Run: python3 agents/.template/routines/tests/test-dgn1394-skill-landing-gate.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "skill-landing-gate.py")
PYTHON = sys.executable

ROOT = tempfile.mkdtemp(prefix="dgn1394-")
HOME = os.path.join(ROOT, "dogany-home")          # DOGANY_HOME
CWD = os.path.join(ROOT, "instance")              # agent instance root
SENTINEL = os.path.join(CWD, ".claude", ".skill-landing-declared")
SHARED = os.path.join(HOME, "shared-skills")
CREW = os.path.join(HOME, "crews", "health-trainer", "shared-skills")

LOCAL_SKILL = os.path.join(CWD, ".claude", "skills", "note-taker", "SKILL.md")
SHARED_SKILL = os.path.join(SHARED, "note-taker", "SKILL.md")
CREW_SKILL = os.path.join(CREW, "note-taker", "SKILL.md")
PLAIN_FILE = os.path.join(CWD, "files", "tmp", "scratch.md")

ENV = dict(os.environ)
ENV["DOGANY_HOME"] = HOME
# Isolate from any real instance config so a local GATE_ALL=off cannot
# silently turn the assertions into false passes.
ENV["GATE_SWITCH_CONF"] = os.path.join(ROOT, "no-such-agent.conf")
for _key in ("GATE_ALL", "GATE_SKILL_LANDING"):
    ENV.pop(_key, None)


def _run(payload):
    raw = json.dumps(payload) if not isinstance(payload, str) else payload
    result = subprocess.run([PYTHON, GATE], input=raw, capture_output=True,
                            text=True, timeout=10, env=ENV)
    assert result.returncode == 0, (
        "Gate exited non-zero: %s / %s" % (result.returncode, result.stderr))
    out = result.stdout.strip()
    return json.loads(out) if out else {}


def _decision(output):
    return output.get("hookSpecificOutput", {}).get("permissionDecision")


def _is_deny(output):
    return _decision(output) == "deny"


def _is_allow(output):
    return not _is_deny(output)


def _reason(output):
    return output.get("hookSpecificOutput", {}).get(
        "permissionDecisionReason", "")


def _write(payload_path, tool="Write"):
    return {"tool_name": tool, "cwd": CWD,
            "tool_input": {"file_path": payload_path,
                           "content": "---\nname: note-taker\n---\n"}}


def _declare(skill=None, location=None, activate_for=None, age_sec=0):
    """Write the sentinel. A key whose value is None is omitted entirely."""
    os.makedirs(os.path.dirname(SENTINEL), exist_ok=True)
    lines = []
    if skill is not None:
        lines.append("skill: %s" % skill)
    if location is not None:
        lines.append("location: %s" % location)
    if activate_for is not None:
        lines.append("activate_for: %s" % activate_for)
    with open(SENTINEL, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    if age_sec:
        past = time.time() - age_sec
        os.utime(SENTINEL, (past, past))


def _undeclare():
    try:
        os.unlink(SENTINEL)
    except OSError:
        pass


def _setup():
    for d in (os.path.dirname(SENTINEL), SHARED, CREW,
              os.path.dirname(PLAIN_FILE)):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
def test_A_no_declaration_denies():
    _undeclare()
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "A: expected DENY, got %s" % out
    reason = _reason(out)
    for token in ("local", "shared", "framework", "USER"):
        assert token in reason, "A: deny message must name %r: %s" % (
            token, reason)
    assert SHARED in reason, "A: deny message must name the shared tier path"
    print("A PASS: new SKILL.md without a declaration -> DENY "
          "(message names all three locations and points at the USER)")


def test_B_declaration_allows():
    _declare(skill="note-taker", location="local", activate_for="none")
    out = _run(_write(LOCAL_SKILL))
    assert _is_allow(out), "B: expected ALLOW, got %s" % out
    print("B PASS: same write after a valid declaration -> ALLOW")


def test_C_local_requires_empty_activation():
    _declare(skill="note-taker", location="local",
             activate_for="agent-one, agent-two")
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "C: expected DENY, got %s" % out
    assert "local" in _reason(out) and "activate_for" in _reason(out), \
        "C: deny message must explain the contradiction: %s" % _reason(out)
    print("C PASS: location local + non-empty activate_for -> DENY")


def test_D_unknown_location_rejected_loudly():
    _declare(skill="note-taker", location="crew", activate_for="none")
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "D: expected DENY, got %s" % out
    reason = _reason(out)
    assert "Unknown landing location" in reason, \
        "D: rejection must be explicit: %s" % reason
    assert "'crew'" in reason, "D: must quote the rejected value: %s" % reason
    for token in ("local", "shared", "framework"):
        assert token in reason, "D: must list %r: %s" % (token, reason)
    print("D PASS: unknown location -> DENY naming the bad value and the "
          "three valid ones")


def test_E_non_skill_writes_unaffected():
    _undeclare()
    out = _run(_write(PLAIN_FILE))
    assert _is_allow(out), "E1: ordinary file write must pass: %s" % out

    existing = os.path.join(SHARED, "already-landed", "SKILL.md")
    os.makedirs(os.path.dirname(existing), exist_ok=True)
    with open(existing, "w", encoding="utf-8") as fh:
        fh.write("---\nname: already-landed\n---\n")
    out = _run({"tool_name": "Edit", "cwd": CWD,
                "tool_input": {"file_path": existing,
                               "old_string": "a", "new_string": "b"}})
    assert _is_allow(out), "E2: edit of an existing SKILL.md must pass: %s" % out

    out = _run({"tool_name": "Read", "cwd": CWD,
                "tool_input": {"file_path": LOCAL_SKILL}})
    assert _is_allow(out), "E3: non-write tool must pass: %s" % out
    print("E PASS: non-new-SKILL.md writes unaffected (ordinary file / "
          "existing SKILL.md / non-write tool)")


def test_F_bypass_closed():
    """The live failure: the agent never invoked the skill-creator skill.
    The payload below carries no skill context whatsoever -- it is a bare
    Write. The gate keys on the artifact, so the bypass does not exist."""
    _undeclare()
    out = _run({"tool_name": "Write", "cwd": CWD,
                "tool_input": {"file_path": LOCAL_SKILL,
                               "content": "---\nname: note-taker\n"
                                          "description: hand-authored\n---\n"}})
    assert _is_deny(out), "F: direct authoring must still be blocked: %s" % out
    assert "ASK THE USER" in _reason(out), \
        "F: deny must instruct asking the user: %s" % _reason(out)
    print("F PASS: SKILL.md authored directly, no skill invoked -> still DENY")


def test_G_declaration_for_another_skill_denies():
    _declare(skill="other-skill", location="shared", activate_for="none")
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "G: expected DENY, got %s" % out
    assert "other-skill" in _reason(out), \
        "G: deny must name the mismatching declaration: %s" % _reason(out)
    print("G PASS: declaration bound to another skill -> DENY")


def test_H_missing_activation_axis_denies():
    _declare(skill="note-taker", location="local", activate_for=None)
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "H: expected DENY, got %s" % out
    assert "activate_for" in _reason(out), \
        "H: deny must name the missing axis: %s" % _reason(out)
    print("H PASS: location answered but activate_for omitted -> DENY")


def test_I_expired_declaration_denies():
    _declare(skill="note-taker", location="local", activate_for="none",
             age_sec=7200)
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "I: expected DENY on an expired declaration: %s" % out
    print("I PASS: declaration older than the TTL -> DENY")


def test_J_crew_mount_denies():
    _declare(skill="note-taker", location="shared", activate_for="none")
    out = _run(_write(CREW_SKILL))
    assert _is_deny(out), "J: expected DENY, got %s" % out
    reason = _reason(out)
    assert "installer" in reason, \
        "J: deny must name the single writer: %s" % reason
    print("J PASS: authoring into the installer-owned crew mount -> DENY "
          "(declaration cannot authorize it)")


def test_K_shared_declaration_local_path_denies():
    _declare(skill="note-taker", location="shared", activate_for="agent-one")
    out = _run(_write(LOCAL_SKILL))
    assert _is_deny(out), "K: expected DENY, got %s" % out
    assert SHARED in _reason(out), \
        "K: deny must name the required destination: %s" % _reason(out)
    print("K PASS: location shared but path outside the shared tier -> DENY")


def test_L_shared_declaration_shared_path_allows():
    _declare(skill="note-taker", location="shared", activate_for="agent-one")
    out = _run(_write(SHARED_SKILL))
    assert _is_allow(out), "L: expected ALLOW, got %s" % out
    print("L PASS: location shared + path inside the shared tier -> ALLOW")


def test_M_bad_input_fails_open():
    out = _run("{not valid json")
    assert _is_allow(out), "M: bad JSON must fail open: %s" % out
    print("M PASS: malformed stdin -> fail-open ALLOW")


def main():
    _setup()
    tests = [test_A_no_declaration_denies,
             test_B_declaration_allows,
             test_C_local_requires_empty_activation,
             test_D_unknown_location_rejected_loudly,
             test_E_non_skill_writes_unaffected,
             test_F_bypass_closed,
             test_G_declaration_for_another_skill_denies,
             test_H_missing_activation_axis_denies,
             test_I_expired_declaration_denies,
             test_J_crew_mount_denies,
             test_K_shared_declaration_local_path_denies,
             test_L_shared_declaration_shared_path_allows,
             test_M_bad_input_fails_open]
    failures = []
    try:
        for t in tests:
            try:
                t()
            except AssertionError as exc:
                print("FAIL: %s: %s" % (t.__name__, exc))
                failures.append(t.__name__)
            except Exception as exc:
                print("ERROR: %s: %s" % (t.__name__, exc))
                failures.append(t.__name__)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)

    print()
    if failures:
        print("FAILED: %s" % failures)
        sys.exit(1)
    print("All tests PASSED (A-M)")
    sys.exit(0)


if __name__ == "__main__":
    main()
