#!/usr/bin/env python3
# test-lane: fast
"""Locks for routines/onboarding-owner-gate.py (DGN-1606) plus the
onboarding-gate-stop.py injected-turn abstention that shipped with it.

Measured incident (2026-09-20, dogany-health): a machine-injected
restart-verify turn -- zero owner utterances in the session -- completed
first-contact onboarding alone: filled all five identity fields with
invented values and deleted the ONBOARDING_PENDING marker.  The "identity
fields are written on explicit user request" rule existed only as prose.

Contract under test (the forcing point at the write):
  * pending marker + machine-opened turn (boundary starts with the
    injected-turn mark) + Edit targeting the identity file -> DENY JSON
    (permissionDecision deny) + OWNER_DENY log line.
  * same but the tool is Bash mentioning the identity basename -> DENY
    (the measured incident deleted the block via `sed -i`).
  * pending + OWNER-opened turn (genuine boundary, no mark) -> silent:
    a real first-contact onboarding edits the identity file freely.
  * marker absent -> silent (steady state).
  * pending + non-interactive session (no PROJECT_ROOT) -> DENY even
    without transcript evidence: no owner can be speaking there.
  * non-identity write target in a machine-opened turn -> silent.
  * unreadable/absent transcript in an interactive session -> silent
    (deny needs POSITIVE machine-opened identification; never wedge a
    real mint).
  * kill switch GATE_ONBOARDING_OWNER=off -> non-blocking systemMessage
    off-notice instead of the deny.
  * fail-open: garbage stdin exits 0 silently.

onboarding-gate-stop.py abstention (same mark, reader side):
  * pending + machine-opened turn + zero engagement -> the Stop gate no
    longer blocks (SKIP_INJECTED log): there is no owner to onboard in an
    injected turn, so onboarding is not demanded into the void.
  * pending + owner-opened turn + zero engagement -> still blocks
    (the DGN-1543 R7 ambush is unchanged where an owner IS present).
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
OWNER_GATE = os.path.join(ROOT, "onboarding-owner-gate.py")
STOP_GATE = os.path.join(ROOT, "onboarding-gate-stop.py")

MARKER = "<!-- ONBOARDING_PENDING -->"
MARK = "[bridge:injected-turn]"

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append(f"{name}: got {got!r}, want {want!r}")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def identity_body(marker=True):
    lines = []
    if marker:
        lines.append(MARKER)
        lines.append("<!-- FIRST-CONTACT ONBOARDING -- test stub block -->")
    lines += [
        "# AGENT",
        "## Identity",
        "- Name: **(set at onboarding)**",
        "- Emoji: (set at onboarding)",
        "## Relationship",
        "- Call the user **(set at onboarding)**.",
        "- Tone: (set at onboarding)",
        "- Humor: (set at onboarding)",
    ]
    return "\n".join(lines) + "\n"


def mk_instance(root, marker=True, conf_extra=""):
    write(os.path.join(root, "identity", "hot.custom.agent.md"),
          identity_body(marker=marker))
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=en\n" + conf_extra)
    return os.path.join(root, "identity", "hot.custom.agent.md")


def mk_transcript(root, boundary, text="working"):
    """One user boundary (string content) + one assistant entry.  text=None
    -> the assistant entry carries only an unrelated tool call (a turn with
    no owner-visible text, the only shape the Stop seat still blocks --
    DGN-1703)."""
    block = ({"type": "text", "text": text} if text is not None else
             {"type": "tool_use", "name": "Bash", "input": {"command": "date"}})
    entries = [
        {"type": "user", "message": {"content": boundary}},
        {"type": "assistant", "message": {"content": [block]}},
    ]
    path = os.path.join(root, "transcript.jsonl")
    write(path, "\n".join(json.dumps(e, ensure_ascii=False)
                          for e in entries) + "\n")
    return path


INJECTED_BOUNDARY = MARK + "\n[restart-verify] check the restart"
OWNER_BOUNDARY = "hello, are you my new agent?"


def run_owner_gate(root, tool_name, tool_input, transcript=None,
                   interactive=True, raw_stdin=None):
    payload = raw_stdin if raw_stdin is not None else json.dumps({
        "cwd": root,
        "transcript_path": transcript or "",
        "tool_name": tool_name,
        "tool_input": tool_input,
    })
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_ONBOARDING_OWNER", "GATE_ALL", "GATE_SWITCH_CONF",
              "GATE_SWITCH_LOG"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    if interactive:
        env["PROJECT_ROOT"] = root
    return subprocess.run(
        [sys.executable, OWNER_GATE], input=payload,
        capture_output=True, text=True, timeout=15, env=env)


def run_stop_gate(root, transcript):
    payload = json.dumps({
        "cwd": root,
        "transcript_path": transcript,
        "stop_hook_active": False,
        "hook_event_name": "Stop",
    })
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_STOP_ONBOARDING", "GATE_STOP_ALL", "GATE_SWITCH_CONF",
              "GATE_SWITCH_LOG"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    env["PROJECT_ROOT"] = root
    return subprocess.run(
        [sys.executable, STOP_GATE], input=payload,
        capture_output=True, text=True, timeout=15, env=env)


def log_text(root):
    path = os.path.join(root, ".telegram_bot", "logs", "onboarding-gate.log")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def out_json(proc):
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def is_deny(out):
    return (bool(out)
            and out.get("hookSpecificOutput", {}).get("permissionDecision")
            == "deny")


def edit_input(identity_path):
    return {"file_path": identity_path, "old_string": "a", "new_string": "b"}


def test_deny_injected_edit():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d)
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_owner_gate(d, "Edit", edit_input(ident), t)
        check("inj-edit.rc", proc.returncode, 0)
        out = out_json(proc)
        check("inj-edit.deny", is_deny(out), True)
        check("inj-edit.reason",
              "owner-gate" in out["hookSpecificOutput"][
                  "permissionDecisionReason"], True)
        check("inj-edit.logged", "OWNER_DENY" in log_text(d), True)


def test_deny_injected_bash():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_owner_gate(
            d, "Bash",
            {"command": "sed -i '' '1,10d' identity/hot.custom.agent.md"}, t)
        check("inj-bash.rc", proc.returncode, 0)
        check("inj-bash.deny", is_deny(out_json(proc)), True)


def test_allow_owner_turn():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d)
        t = mk_transcript(d, OWNER_BOUNDARY)
        proc = run_owner_gate(d, "Edit", edit_input(ident), t)
        check("owner.rc", proc.returncode, 0)
        check("owner.stdout", proc.stdout.strip(), "")


def test_allow_marker_absent():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d, marker=False)
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_owner_gate(d, "Edit", edit_input(ident), t)
        check("done.rc", proc.returncode, 0)
        check("done.stdout", proc.stdout.strip(), "")


def test_deny_headless_session():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d)
        proc = run_owner_gate(d, "Edit", edit_input(ident),
                              transcript=None, interactive=False)
        check("headless.rc", proc.returncode, 0)
        check("headless.deny", is_deny(out_json(proc)), True)


def test_allow_other_target():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_owner_gate(
            d, "Edit",
            {"file_path": os.path.join(d, "files", "notes.md"),
             "old_string": "a", "new_string": "b"}, t)
        check("other.rc", proc.returncode, 0)
        check("other.stdout", proc.stdout.strip(), "")


def test_allow_when_transcript_unreadable():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d)
        proc = run_owner_gate(d, "Edit", edit_input(ident),
                              transcript=os.path.join(d, "nope.jsonl"))
        check("noscript.rc", proc.returncode, 0)
        check("noscript.stdout", proc.stdout.strip(), "")


def test_switch_off_is_notice_not_deny():
    with tempfile.TemporaryDirectory() as d:
        ident = mk_instance(d, conf_extra="GATE_ONBOARDING_OWNER=off\n")
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_owner_gate(d, "Edit", edit_input(ident), t)
        check("off.rc", proc.returncode, 0)
        out = out_json(proc)
        check("off.not_deny", is_deny(out), False)
        check("off.notice", bool(out) and "systemMessage" in out, True)
        check("off.logged", "OWNER_OFF_SUPPRESSED" in log_text(d), True)


def test_failopen_garbage_stdin():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        proc = run_owner_gate(d, "Edit", {}, raw_stdin="not json {{{")
        check("garbage.rc", proc.returncode, 0)
        check("garbage.stdout", proc.stdout.strip(), "")


def test_stop_gate_abstains_in_injected_turn():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, INJECTED_BOUNDARY)
        proc = run_stop_gate(d, t)
        check("skip.rc", proc.returncode, 0)
        check("skip.stdout", proc.stdout.strip(), "")
        check("skip.logged", "SKIP_INJECTED" in log_text(d), True)


def test_stop_gate_still_blocks_owner_turn():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, OWNER_BOUNDARY, text=None)
        proc = run_stop_gate(d, t)
        check("ambush.rc", proc.returncode, 0)
        out = out_json(proc)
        check("ambush.block", bool(out) and out.get("decision"), "block")


def test_stop_gate_owner_turn_with_text_is_a_miss_not_a_skip():
    # DGN-1703: an owner turn whose reply already streamed is not blocked --
    # but it is still judged as an OWNER turn (MISS_VISIBLE), never skipped
    # as machine-injected.
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, OWNER_BOUNDARY)
        proc = run_stop_gate(d, t)
        check("ownertext.stdout", proc.stdout.strip(), "")
        check("ownertext.miss", "MISS_VISIBLE" in log_text(d), True)
        check("ownertext.not_skip", "SKIP_INJECTED" in log_text(d), False)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    if _failures:
        print("FAIL test-dgn1606-onboarding-owner-gate")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("OK test-dgn1606-onboarding-owner-gate (%d tests)" % len(tests))


if __name__ == "__main__":
    main()
