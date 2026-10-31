#!/usr/bin/env python3
# test-lane: fast
"""Tier-1 tests for in-session-dispatch-gate.py (DGN-1414).

Spec cases:
  A: Agent call with no 'model'                                -> DENY
  B: Task call with 'model' but no IN-SESSION: declaration     -> DENY
  C: IN-SESSION: reason shorter than 10 chars                  -> DENY
  D: model present AND a valid IN-SESSION: line (>=10 chars)   -> ALLOW
  E: a non-Agent/Task tool name                                -> ALLOW (silent
     pass-through)

Supporting cases:
  F: blank/whitespace-only model                               -> DENY
  G: declaration only in description                          -> DENY
  H: malformed JSON on stdin                                   -> fail-open ALLOW

Run: python3 routines/tests/test-dgn1414-in-session-dispatch-gate.py
"""
import json
import os
import subprocess
import sys

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "in-session-dispatch-gate.py")
PYTHON = sys.executable

VALID_REASON = "IN-SESSION: needed this turn, result required immediately"


def _run(payload):
    raw = json.dumps(payload) if not isinstance(payload, str) else payload
    result = subprocess.run([PYTHON, GATE], input=raw, capture_output=True,
                            text=True, timeout=10)
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


def _call(tool_name="Task", model="sonnet", prompt=None, description=None):
    tool_input = {}
    if model is not None:
        tool_input["model"] = model
    if prompt is not None:
        tool_input["prompt"] = prompt
    if description is not None:
        tool_input["description"] = description
    return {"tool_name": tool_name, "tool_input": tool_input}


# ---------------------------------------------------------------------------
def test_A_missing_model_denies():
    out = _run(_call(tool_name="Agent", model=None,
                      prompt="do the thing\n" + VALID_REASON))
    assert _is_deny(out), "A: expected DENY, got %s" % out
    assert "model" in _reason(out), "A: deny must name the missing field: %s" % (
        _reason(out))
    assert "dispatch-detached.sh" in _reason(out), (
        "A: deny must show the detached invocation: %s" % _reason(out))
    print("A PASS: model missing -> DENY")


def test_B_missing_declaration_denies():
    out = _run(_call(tool_name="Task", model="sonnet",
                      prompt="just do the thing, no declaration here"))
    assert _is_deny(out), "B: expected DENY, got %s" % out
    assert "IN-SESSION" in _reason(out), (
        "B: deny must name the missing declaration: %s" % _reason(out))
    print("B PASS: IN-SESSION declaration missing -> DENY")


def test_M_deny_names_the_live_write_lane():
    # DGN-1458: the deny text must route live workspace writes to the plan
    # lane instead of leaving "in-session" as the only way to write them.
    out = _run(_call(tool_name="Task", model="sonnet",
                      prompt="write into the live instance database"))
    assert _is_deny(out), "M: expected DENY, got %s" % out
    reason = _reason(out)
    assert "--live-target <path>" in reason, "M: plan lane missing: %s" % reason
    assert "--apply-plan <runid>" in reason, "M: applier missing: %s" % reason
    print("M PASS: deny names the DGN-1458 plan lane for live writes")


def test_C_short_reason_denies():
    out = _run(_call(tool_name="Task", model="sonnet",
                      prompt="do the thing\nIN-SESSION: short"))
    assert _is_deny(out), "C: expected DENY, got %s" % out
    print("C PASS: IN-SESSION reason under 10 chars -> DENY")


def test_D_model_and_declaration_allows():
    out = _run(_call(tool_name="Task", model="sonnet",
                      prompt="do the thing\n" + VALID_REASON))
    assert _is_allow(out), "D: expected ALLOW, got %s" % out
    print("D PASS: model present + valid IN-SESSION line -> ALLOW")


def test_E_other_tool_allows_silently():
    out = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert _is_allow(out), "E: expected ALLOW, got %s" % out
    assert out == {}, "E: non-Agent/Task tool must be a silent pass-through: %s" % out
    print("E PASS: non-Agent/Task tool name -> ALLOW (silent)")


def test_F_blank_model_denies():
    out = _run(_call(tool_name="Agent", model="   ",
                      prompt="do the thing\n" + VALID_REASON))
    assert _is_deny(out), "F: expected DENY, got %s" % out
    print("F PASS: whitespace-only model -> DENY")


def test_G_description_only_denies():
    out = _run(_call(tool_name="Task", model="opus", prompt=None,
                      description="do the thing\n" + VALID_REASON))
    assert _is_deny(out), "G: expected DENY, got %s" % out
    print("G PASS: declaration outside prompt -> DENY")


def test_H_bad_input_fails_open():
    out = _run("{not valid json")
    assert _is_allow(out), "H: bad JSON must fail open: %s" % out
    print("H PASS: malformed stdin -> fail-open ALLOW")


def test_I_reason_must_be_on_same_line():
    for tool in ("Agent", "Task"):
        for newline in ("\n", "\r\n", "\r"):
            out = _run(_call(tool_name=tool,
                            prompt="IN-SESSION:  " + newline + "1234567890"))
            assert _is_deny(out), "I: next-line reason accepted: %s" % out
    print("I PASS: next-line text cannot supply the reason")


def test_J_any_valid_declaration_allows():
    for tool in ("Agent", "Task"):
        for newline in ("\n", "\r\n", "\r"):
            out = _run(_call(tool_name=tool,
                            prompt="IN-SESSION: short" + newline +
                            "IN-SESSION: 1234567890"))
            assert out == {}, "J: valid declaration must pass silently: %s" % out
    print("J PASS: any qualifying prompt line allows")


def test_K_boundary_and_anchor():
    for tool in ("Agent", "Task"):
        for reason, deny in (("123456789", True), ("1234567890", False),
                             ("  123456789  ", True)):
            out = _run(_call(tool_name=tool, prompt="IN-SESSION: " + reason))
            assert _is_deny(out) == deny, "K: wrong length boundary: %s" % out
        out = _run(_call(tool_name=tool, prompt="quoted IN-SESSION: 1234567890"))
        assert _is_deny(out), "K: inline declaration accepted"
    print("K PASS: length boundary and line anchor")


def test_L_independent_hook_wiring():
    settings = os.path.join(os.path.dirname(GATE), "..", ".claude", "settings.json")
    with open(settings) as source:
        hooks = json.load(source)["hooks"]["PreToolUse"]
    entries = [entry for entry in hooks if any(
        "in-session-dispatch-gate.py" in hook.get("command", "")
        for hook in entry["hooks"])]
    assert len(entries) == 1 and entries[0]["matcher"] == "Agent|Task"
    assert any(entry["matcher"] == "Skill|Workflow|Agent|Task" and any(
        "usage-gate.py" in hook.get("command", "") for hook in entry["hooks"])
        for entry in hooks), "L: existing usage gate wiring must remain"
    print("L PASS: independent dispatch and usage hooks remain wired")


def main():
    tests = [test_A_missing_model_denies,
             test_B_missing_declaration_denies,
             test_C_short_reason_denies,
             test_D_model_and_declaration_allows,
             test_E_other_tool_allows_silently,
             test_F_blank_model_denies,
             test_G_description_only_denies,
             test_H_bad_input_fails_open,
             test_I_reason_must_be_on_same_line,
             test_J_any_valid_declaration_allows,
             test_K_boundary_and_anchor,
             test_L_independent_hook_wiring,
             test_M_deny_names_the_live_write_lane]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print("FAIL: %s: %s" % (t.__name__, exc))
            failures.append(t.__name__)
        except Exception as exc:
            print("ERROR: %s: %s" % (t.__name__, exc))
            failures.append(t.__name__)

    print()
    if failures:
        print("FAILED: %s" % failures)
        sys.exit(1)
    print("All tests PASSED (A-M)")
    sys.exit(0)


if __name__ == "__main__":
    main()
