#!/usr/bin/env python3
# test-lane: fast    # measured ~0.5s (2026-09-18)
"""Locks for routines/onboarding-gate-stop.py (DGN-1543 R7) plus the
onboarding-check.py source-contract change that shipped with it.

Contract under test (the forcing point):
  * pending marker + owner-facing session + zero onboarding engagement in
    the current turn + NO owner-visible text -> Stop BLOCK (decision:block
    JSON) + BLOCK log line.
  * DGN-1703: same, but the turn's text already reached the owner -> NO
    block (a re-prompt would be a second owner message), MISS_VISIBLE log,
    and the next UserPromptSubmit injection carries a one-shot reminder.
  * DGN-1703 widened surface: a Bash command that reads
    identity/hot.custom.owner.md (the measured 09:23 turn), an Edit of
    the owner file, a Read of an onboarding skill file -> silent pass; a
    Bash command touching none of them does not count.
  * pending + identity-file touch (Read tool_use) this turn -> silent pass.
  * pending + dogany-user-onboarding skill touch this turn -> silent pass.
  * marker absent -> silent (steady state), and the pending->cleared
    transition logs RELEASED exactly once (arm sentinel).
  * stop_hook_active -> silent (one re-prompt per turn, never a loop).
  * non-interactive session (no PROJECT_ROOT / DOGANY_SESSION=auto) ->
    silent: cron/headless runners are never gated.
  * SubagentStop -> silent.
  * all five identity fields filled but marker left behind -> the gate
    CLEARS the marker itself (AUTO_CLEAR log) and does not block: the
    forcing point has a machine exit.
  * kill switch (agent.conf GATE_STOP_ONBOARDING=off) -> non-blocking
    systemMessage off-notice instead of the block.
  * fail-open: garbage stdin exits 0 silently.

onboarding-check.py source contract (DGN-1543 R7):
  * source=resume + marker present -> onboarding context IS injected
    (the bridge resumes persisted sessions; the old startup|clear filter
    dropped the signal there -- the measured 2026-09-17 hole).
  * source=resume + onboarding done -> silent even with an offer pending
    (offer signals keep the startup|clear filter).
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
GATE = os.path.join(ROOT, "onboarding-gate-stop.py")
CHECK = os.path.join(ROOT, "onboarding-check.py")

MARKER = "<!-- ONBOARDING_PENDING -->"

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append(f"{name}: got {got!r}, want {want!r}")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def identity_body(marker=True, filled=False):
    lines = []
    if marker:
        lines.append(MARKER)
        lines.append("<!-- FIRST-CONTACT ONBOARDING -- test stub block -->")
    lines.append("# AGENT")
    lines.append("## Identity")
    if filled:
        lines.append("- Name: **Kim Warg**")
        lines.append("- Emoji: \U0001F98A")
    else:
        lines.append("- Name: **(set at onboarding)**")
        lines.append("- Emoji: (set at onboarding)")
    lines.append("## Role")
    lines.append("- Primary focus: (set at onboarding -- one prose line "
                 "naming the main hat)")
    lines.append("## Relationship")
    if filled:
        lines.append("- Call the user **boss**.")
        lines.append("- Tone: warm")
        lines.append("- Humor: 30%")
    else:
        lines.append("- Call the user **(set at onboarding)**.")
        lines.append("- Tone: (set at onboarding)")
        lines.append("- Humor: (set at onboarding)")
    return "\n".join(lines) + "\n"


def mk_instance(root, marker=True, filled=False, conf_extra=""):
    write(os.path.join(root, "identity", "hot.custom.agent.md"),
          identity_body(marker=marker, filled=filled))
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=en\n" + conf_extra)


def mk_transcript(root, touch=None, text=True, tool=None):
    """Minimal JSONL: one genuine user boundary, then assistant entries.
    touch: None | 'identity' | 'skill' -> adds a tool_use in the current
    turn referencing that surface.  tool: an explicit (name, input) tool_use
    to add.  text=False -> the turn ends with no owner-visible text."""
    entries = [
        {"type": "user", "message": {"content": "hello there"}},
    ]
    if touch == "identity":
        entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": os.path.join(
                 root, "identity", "hot.custom.agent.md")}},
        ]}})
    elif touch == "skill":
        entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "dogany-user-onboarding"}},
        ]}})
    if tool is not None:
        entries.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": tool[0], "input": tool[1]},
        ]}})
    if text:
        entries.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "greeting text"},
        ]}})
    path = os.path.join(root, "transcript.jsonl")
    write(path, "\n".join(json.dumps(e, ensure_ascii=False)
                          for e in entries) + "\n")
    return path


def run_gate(root, transcript=None, stop_hook_active=False,
             interactive=True, hook_event_name="Stop", raw_stdin=None):
    payload = raw_stdin if raw_stdin is not None else json.dumps({
        "cwd": root,
        "transcript_path": transcript or "",
        "stop_hook_active": stop_hook_active,
        "hook_event_name": hook_event_name,
    })
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_STOP_ONBOARDING", "GATE_STOP_ALL", "GATE_SWITCH_CONF"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    if interactive:
        env["PROJECT_ROOT"] = root
    return subprocess.run(
        [sys.executable, GATE], input=payload,
        capture_output=True, text=True, timeout=15, env=env)


def run_check(root, source):
    payload = json.dumps({"cwd": root, "source": source})
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "AGENT_CONF_FILE", "INSTANCE_CONF_FILE",
              "LIFEKIT_FILE"):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, CHECK], input=payload,
        capture_output=True, text=True, timeout=15, env=env)


def log_text(root):
    path = os.path.join(root, ".telegram_bot", "logs", "onboarding-gate.log")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def decision_of(proc):
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def test_block_on_zero_engagement():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, touch=None, text=False)
        proc = run_gate(d, t)
        check("block.rc", proc.returncode, 0)
        out = decision_of(proc)
        check("block.decision", bool(out) and out.get("decision"), "block")
        check("block.reason_names_gate",
              bool(out) and "Onboarding gate" in out.get("reason", ""), True)
        check("block.logged", "BLOCK" in log_text(d), True)


def test_pass_on_identity_touch():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, touch="identity")
        proc = run_gate(d, t)
        check("touch.rc", proc.returncode, 0)
        check("touch.stdout", proc.stdout.strip(), "")
        check("touch.logged", "PASS_TOUCH" in log_text(d), True)


def test_pass_on_skill_touch():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, touch="skill")
        proc = run_gate(d, t)
        check("skill.rc", proc.returncode, 0)
        check("skill.stdout", proc.stdout.strip(), "")


def test_silent_when_marker_absent():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, marker=False)
        t = mk_transcript(d)
        proc = run_gate(d, t)
        check("done.rc", proc.returncode, 0)
        check("done.stdout", proc.stdout.strip(), "")


def test_release_transition_logged_once():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d)
        run_gate(d, t)  # pending -> arms + blocks
        # model clears the marker (simulated self-edit)
        write(os.path.join(d, "identity", "hot.custom.agent.md"),
              identity_body(marker=False))
        run_gate(d, t)
        check("release.logged", "RELEASED" in log_text(d), True)
        run_gate(d, t)
        check("release.once", log_text(d).count("RELEASED"), 1)


def test_stop_hook_active_never_loops():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d)
        proc = run_gate(d, t, stop_hook_active=True)
        check("loopguard.rc", proc.returncode, 0)
        check("loopguard.stdout", proc.stdout.strip(), "")


def test_non_interactive_silent():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d)
        proc = run_gate(d, t, interactive=False)
        check("headless.rc", proc.returncode, 0)
        check("headless.stdout", proc.stdout.strip(), "")


def test_subagent_stop_silent():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d)
        proc = run_gate(d, t, hook_event_name="SubagentStop")
        check("subagent.rc", proc.returncode, 0)
        check("subagent.stdout", proc.stdout.strip(), "")


def test_auto_clear_when_fields_filled():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, marker=True, filled=True)
        t = mk_transcript(d)
        proc = run_gate(d, t)
        check("autoclear.rc", proc.returncode, 0)
        check("autoclear.stdout", proc.stdout.strip(), "")
        with open(os.path.join(d, "identity", "hot.custom.agent.md"),
                  encoding="utf-8") as f:
            body = f.read()
        check("autoclear.marker_gone", MARKER in body, False)
        check("autoclear.logged", "AUTO_CLEAR" in log_text(d), True)


def test_kill_switch_off_notice():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, conf_extra="GATE_STOP_ONBOARDING=off\n")
        t = mk_transcript(d, text=False)
        proc = run_gate(d, t)
        check("switch.rc", proc.returncode, 0)
        out = decision_of(proc)
        check("switch.no_block",
              bool(out) and out.get("decision") is None, True)
        check("switch.notice",
              bool(out) and "systemMessage" in out, True)


def _silent_pass(name, root, transcript):
    proc = run_gate(root, transcript)
    check(name + ".rc", proc.returncode, 0)
    check(name + ".stdout", proc.stdout.strip(), "")
    check(name + ".pass_touch", "PASS_TOUCH" in log_text(root), True)


def test_pass_on_bash_read_of_owner_file():
    # The measured 2026-09-25 09:23 turn, verbatim command shape.
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        cmd = ("cd %s; cat identity/hot.custom.owner.md; grep -E "
               "'LEADER|MIGRATION_PEER|HANDOFF_PEER' config/agent.conf; "
               "ls .." % d)
        t = mk_transcript(d, tool=("Bash", {"command": cmd}), text=False)
        _silent_pass("bash_owner", d, t)


def test_pass_on_owner_file_edit_and_skill_file_read():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, tool=("Edit", {
            "file_path": os.path.join(d, "identity", "hot.custom.owner.md"),
            "old_string": "a", "new_string": "b"}), text=False)
        _silent_pass("edit_owner", d, t)
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, tool=("Read", {"file_path": os.path.join(
            d, ".claude", "skills", "dogany-user-onboarding", "SKILL.md")}),
            text=False)
        _silent_pass("read_skill", d, t)
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, tool=("Bash", {
            "command": "sed -n 1,40p \"identity/hot.custom.agent.md\"|head"}),
            text=False)
        _silent_pass("bash_agent_quoted_pipe", d, t)


def test_unrelated_bash_is_not_engagement():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, tool=("Bash", {
            "command": "grep -E 'LEADER' config/agent.conf; ls identity"}),
            text=False)
        proc = run_gate(d, t)
        out = decision_of(proc)
        check("unrelated.block", bool(out) and out.get("decision"), "block")


def run_prompt(root, prompt="next"):
    payload = json.dumps({"cwd": root, "hook_event_name": "UserPromptSubmit",
                          "prompt": prompt})
    return run_gate(root, raw_stdin=payload)


def test_visible_text_turn_is_not_blocked_and_reminded_next_prompt():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, touch=None, text=True)
        proc = run_gate(d, t)
        check("miss.rc", proc.returncode, 0)
        check("miss.no_block", proc.stdout.strip(), "")
        check("miss.logged", "MISS_VISIBLE" in log_text(d), True)
        check("miss.not_blocked_log", "  BLOCK  " in log_text(d), False)
        # Next owner prompt: reminder rides the pre-generation injection once.
        out = decision_of(run_prompt(d))
        ctx = (out or {}).get("hookSpecificOutput", {}).get(
            "additionalContext", "")
        check("miss.reminder", "PREVIOUS reply ended without" in ctx, True)
        check("miss.contract_kept", "Before answering:" in ctx, True)
        out2 = decision_of(run_prompt(d))
        ctx2 = (out2 or {}).get("hookSpecificOutput", {}).get(
            "additionalContext", "")
        check("miss.one_shot", "PREVIOUS reply ended without" in ctx2, False)
        check("miss.contract_every_prompt", "Before answering:" in ctx2, True)


def test_last_assistant_message_counts_as_visible():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        t = mk_transcript(d, text=False)
        payload = json.dumps({"cwd": d, "transcript_path": t,
                              "hook_event_name": "Stop",
                              "stop_hook_active": False,
                              "last_assistant_message": "hello"})
        proc = run_gate(d, raw_stdin=payload)
        check("lam.no_block", proc.stdout.strip(), "")
        check("lam.miss", "MISS_VISIBLE" in log_text(d), True)


def test_unreadable_transcript_never_blocks():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        proc = run_gate(d, os.path.join(d, "missing.jsonl"))
        check("unreadable.stdout", proc.stdout.strip(), "")


def test_fail_open_on_garbage_stdin():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        proc = run_gate(d, raw_stdin="not json at all")
        check("failopen.rc", proc.returncode, 0)
        check("failopen.stdout", proc.stdout.strip(), "")


def test_check_fires_on_resume_while_pending():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d)
        proc = run_check(d, source="resume")
        check("check_resume.rc", proc.returncode, 0)
        out = proc.stdout.strip()
        check("check_resume.injected", bool(out), True)
        if out:
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            check("check_resume.onboarding_ctx",
                  "[onboarding needed]" in ctx, True)


def test_check_offers_stay_startup_only():
    with tempfile.TemporaryDirectory() as d:
        mk_instance(d, marker=False, conf_extra="KIT=pending\n")
        # DGN-1817: an installed life kit, so kit-select is not suppressed
        # as uninstallable.
        write(os.path.join(d, "service", "kit_life", "bundle.conf"), "# fixture\n")
        proc = run_check(d, source="resume")
        check("check_offer_resume.rc", proc.returncode, 0)
        check("check_offer_resume.silent", proc.stdout.strip(), "")
        proc = run_check(d, source="startup")
        out = proc.stdout.strip()
        check("check_offer_startup.fires",
              bool(out) and "[kit selection pending]" in out, True)


def main():
    test_block_on_zero_engagement()
    test_pass_on_identity_touch()
    test_pass_on_skill_touch()
    test_silent_when_marker_absent()
    test_release_transition_logged_once()
    test_stop_hook_active_never_loops()
    test_non_interactive_silent()
    test_subagent_stop_silent()
    test_auto_clear_when_fields_filled()
    test_kill_switch_off_notice()
    test_fail_open_on_garbage_stdin()
    test_pass_on_bash_read_of_owner_file()
    test_pass_on_owner_file_edit_and_skill_file_read()
    test_unrelated_bash_is_not_engagement()
    test_visible_text_turn_is_not_blocked_and_reminded_next_prompt()
    test_last_assistant_message_counts_as_visible()
    test_unreadable_transcript_never_blocks()
    test_check_fires_on_resume_while_pending()
    test_check_offers_stay_startup_only()
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-onboarding-gate-stop (19 tests)")


if __name__ == "__main__":
    main()
