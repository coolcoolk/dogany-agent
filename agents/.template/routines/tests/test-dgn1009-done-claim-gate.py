#!/usr/bin/env python3
"""Tier-1 tests for done-claim-gate.py (DGN-898).

Tests mirror the spec cases A-E:
  A: Edit setting status:done WITH verify: line -> ALLOW
  B: Edit setting status:done WITHOUT any evidence -> DENY
  C: Edit to a worklog file NOT touching status -> ALLOW
  D: Write of a non-worklog file -> ALLOW
  E: Bad JSON input -> fail-open (ALLOW)

MultiEdit cases F-J (DGN-1009 hole, additive):
  F: MultiEdit introducing status:done, no evidence anywhere -> DENY
  G: MultiEdit done flip + evidence in a DIFFERENT entry of the batch -> ALLOW
  H: MultiEdit done flip + evidence already on disk -> ALLOW
  I: MultiEdit with malformed edits (non-list / non-dict entry) -> fail-open
  J: MultiEdit not touching status:done -> ALLOW

Run: python3 routines/test_done_claim_gate.py
"""
import json
import os
import subprocess
import sys
import tempfile

GATE = os.path.join(os.path.dirname(__file__), "..", "done-claim-gate.py")
PYTHON = sys.executable

WORKLOG_PATH = "/some/repo/worklog/DGN-898-done-definition.md"
NON_WORKLOG_PATH = "/some/repo/files/tmp/scratch.md"


def _run(payload: object) -> dict:
    """Run gate with payload as stdin JSON. Returns parsed stdout dict (or {})."""
    raw = json.dumps(payload) if not isinstance(payload, str) else payload
    result = subprocess.run(
        [PYTHON, GATE],
        input=raw,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Gate exited non-zero: {result.returncode}"
    stdout = result.stdout.strip()
    if stdout:
        return json.loads(stdout)
    return {}


def _is_deny(output: dict) -> bool:
    return (
        output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


def _is_allow(output: dict) -> bool:
    return not _is_deny(output)


# ---------------------------------------------------------------------------
# Test A: Edit setting status:done WITH verify: line -> ALLOW
# ---------------------------------------------------------------------------
def test_A():
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": WORKLOG_PATH,
            "old_string": "status: open",
            "new_string": "status: done\nverify: self-tested all paths manually",
        },
    }
    out = _run(payload)
    assert _is_allow(out), f"A: expected ALLOW, got DENY. output={out}"
    print("A PASS: Edit with status:done + verify: -> ALLOW")


# ---------------------------------------------------------------------------
# Test B: Edit setting status:done WITHOUT any evidence -> DENY
# ---------------------------------------------------------------------------
def test_B():
    # Write a temporary on-disk file with no evidence markers so the
    # fallback file-read also finds nothing.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="DGN-TEST-",
        dir=tempfile.gettempdir(),
        delete=False,
    ) as fh:
        fh.write("---\nid: DGN-TEST\nstatus: open\n---\n\nSome body text.\n")
        tmp_path = fh.name

    # Rename to match worklog/DGN-*.md detection (parent dir must be "worklog").
    worklog_dir = os.path.join(tempfile.gettempdir(), "worklog")
    os.makedirs(worklog_dir, exist_ok=True)
    target_path = os.path.join(worklog_dir, "DGN-TEST-no-evidence.md")
    os.replace(tmp_path, target_path)

    try:
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": target_path,
                "old_string": "status: open",
                "new_string": "status: done",
            },
        }
        out = _run(payload)
        assert _is_deny(out), f"B: expected DENY, got ALLOW. output={out}"
        print("B PASS: Edit with status:done + no evidence -> DENY")
    finally:
        try:
            os.unlink(target_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test C: Edit to a worklog file NOT touching status -> ALLOW
# ---------------------------------------------------------------------------
def test_C():
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": WORKLOG_PATH,
            "old_string": "## Background",
            "new_string": "## Background (updated)",
        },
    }
    out = _run(payload)
    assert _is_allow(out), f"C: expected ALLOW, got DENY. output={out}"
    print("C PASS: Edit worklog without status:done in new_string -> ALLOW")


# ---------------------------------------------------------------------------
# Test D: Write of a non-worklog file -> ALLOW
# ---------------------------------------------------------------------------
def test_D():
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": NON_WORKLOG_PATH,
            "content": "status: done\n(no evidence line)\n",
        },
    }
    out = _run(payload)
    assert _is_allow(out), f"D: expected ALLOW (non-worklog path), got DENY. output={out}"
    print("D PASS: Write to non-worklog file -> ALLOW (regardless of content)")


# ---------------------------------------------------------------------------
# Test E: Bad JSON input -> fail-open (ALLOW)
# ---------------------------------------------------------------------------
def test_E():
    out = _run("{this is not valid json")
    assert _is_allow(out), f"E: expected fail-open ALLOW on bad JSON, got DENY. output={out}"
    print("E PASS: Bad JSON input -> fail-open ALLOW")


# ---------------------------------------------------------------------------
# MultiEdit helpers (F-J)
# ---------------------------------------------------------------------------
def _make_worklog_ticket(name: str, body: str) -> str:
    worklog_dir = os.path.join(tempfile.gettempdir(), "worklog")
    os.makedirs(worklog_dir, exist_ok=True)
    path = os.path.join(worklog_dir, name)
    with open(path, "w") as fh:
        fh.write(body)
    return path


def _multiedit_payload(path: str, edits: object) -> dict:
    return {"tool_name": "MultiEdit",
            "tool_input": {"file_path": path, "edits": edits}}


# ---------------------------------------------------------------------------
# Test F: MultiEdit introducing status:done, no evidence anywhere -> DENY
# ---------------------------------------------------------------------------
def test_F():
    path = _make_worklog_ticket(
        "DGN-TEST-me-noev.md",
        "---\nid: DGN-TEST-ME\nstatus: open\n---\n\nSome body text.\n")
    try:
        out = _run(_multiedit_payload(path, [
            {"old_string": "status: open", "new_string": "status: done"},
            {"old_string": "Some body text.", "new_string": "Wrapped up."},
        ]))
        assert _is_deny(out), f"F: expected DENY, got ALLOW. output={out}"
        print("F PASS: MultiEdit status:done + no evidence -> DENY")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test G: evidence rides a DIFFERENT entry of the same batch -> ALLOW
# (batch-level judgment; per-entry judgment would false-positive here)
# ---------------------------------------------------------------------------
def test_G():
    path = _make_worklog_ticket(
        "DGN-TEST-me-batchev.md",
        "---\nid: DGN-TEST-ME\nstatus: open\n---\n\nSome body text.\n")
    try:
        out = _run(_multiedit_payload(path, [
            {"old_string": "status: open", "new_string": "status: done"},
            {"old_string": "Some body text.",
             "new_string": "verify: subcases run manually"},
        ]))
        assert _is_allow(out), f"G: expected ALLOW, got DENY. output={out}"
        print("G PASS: MultiEdit done flip + evidence in another entry -> ALLOW")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test H: evidence already on disk -> ALLOW
# ---------------------------------------------------------------------------
def test_H():
    path = _make_worklog_ticket(
        "DGN-TEST-me-diskev.md",
        "---\nid: DGN-TEST-ME\nstatus: open\n---\n\n"
        "evidence: prior session test log attached\n")
    try:
        out = _run(_multiedit_payload(path, [
            {"old_string": "status: open", "new_string": "status: done"},
        ]))
        assert _is_allow(out), f"H: expected ALLOW, got DENY. output={out}"
        print("H PASS: MultiEdit done flip + evidence on disk -> ALLOW")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test I: malformed edits payload -> fail-open ALLOW
# ---------------------------------------------------------------------------
def test_I():
    path = _make_worklog_ticket(
        "DGN-TEST-me-malformed.md",
        "---\nid: DGN-TEST-ME\nstatus: open\n---\n\nSome body text.\n")
    try:
        out = _run(_multiedit_payload(path, "status: done not a list"))
        assert _is_allow(out), f"I: expected fail-open ALLOW (non-list), got DENY. output={out}"
        out = _run(_multiedit_payload(path, [
            "status: done",
            {"old_string": "a", "new_string": "b"},
        ]))
        assert _is_allow(out), f"I: expected fail-open ALLOW (non-dict entry), got DENY. output={out}"
        print("I PASS: MultiEdit malformed edits -> fail-open ALLOW")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test J: MultiEdit not touching status:done -> ALLOW
# ---------------------------------------------------------------------------
def test_J():
    path = _make_worklog_ticket(
        "DGN-TEST-me-nodone.md",
        "---\nid: DGN-TEST-ME\nstatus: open\n---\n\nSome body text.\n")
    try:
        out = _run(_multiedit_payload(path, [
            {"old_string": "Some body text.", "new_string": "Updated body."},
        ]))
        assert _is_allow(out), f"J: expected ALLOW, got DENY. output={out}"
        print("J PASS: MultiEdit without status:done -> ALLOW")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def main():
    tests = [test_A, test_B, test_C, test_D, test_E,
             test_F, test_G, test_H, test_I, test_J]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"FAIL: {t.__name__}: {exc}")
            failures.append(t.__name__)
        except Exception as exc:
            print(f"ERROR: {t.__name__}: {exc}")
            failures.append(t.__name__)

    print()
    if failures:
        print(f"FAILED: {failures}")
        sys.exit(1)
    else:
        print("All tests PASSED (A-J)")
        sys.exit(0)


if __name__ == "__main__":
    main()
