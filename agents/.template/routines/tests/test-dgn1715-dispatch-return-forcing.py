#!/usr/bin/env python3
# test-lane: fast
"""DGN-1715 r2: a dispatch-return turn whose follow-up was denied must END on
a one-line proposal + [[OPTIONS]] marker, never a bare "next conversation"
promise.

Two seats under test:
  * routines/dispatch-return-result-first-gate.py: the deny reason carries the
    instruction and the denial is recorded; a later tool passing the open latch
    clears the record (resumed work is not a stall).  Existing deny semantics
    (see test-dgn1687) are untouched.
  * routines/onboarding-gate-stop.py (the shipped Stop/UserPromptSubmit seat):
    denied follow-up + final message without the marker -> block once;
    already-delivered text -> MISS_VISIBLE log + next-owner-prompt reminder;
    the marker test recognizes every shape (bare or labeled), so any
    language passes.

DGN-1715 r3: the marker test used to be a bare ``"[[OPTIONS]]" in text``
substring check, which missed the labeled shape "[[OPTIONS: a | b]]"
(DGN-992) -- a final ending on a labeled marker read as marker-less and got
force-blocked into a second menu on top of the one already on screen
(measured 2026-09-27, owner: notification arrives twice). See
test_stop_passes_labeled_options_marker / test_stop_still_blocks_marker_
mentioned_inline_or_in_fence below, and
test-dgn1715-options-marker-shapes.py for the shape-recognizer lockstep
against bridge/formatting.py.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.normpath(os.path.join(HERE, ".."))
GATE = os.path.join(ROUTINES, "dispatch-return-result-first-gate.py")
SID = "owner-session"
STOP = os.path.join(ROUTINES, "onboarding-gate-stop.py")

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


def clean_env():
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_DISPATCH_RETURN_RESULT_FIRST", "GATE_ALL", "GATE_STOP_ALL",
              "GATE_SWITCH_CONF", "DISPATCH_RETURN_CONTEXT_PATH"):
        env.pop(k, None)
    return env


def ctx_path(root):
    return os.path.join(root, ".telegram_bot", "dispatch-return-context.json")


def denied_path(root):
    return os.path.join(root, ".telegram_bot", "dispatch-return-denied.json")


def write_ctx(root, **fields):
    row = {"schema": 1, "turn_id": "turn-1", "session_id": SID,
           "visible": False, "ts": time.time()}
    row.update(fields)
    os.makedirs(os.path.dirname(ctx_path(root)), exist_ok=True)
    with open(ctx_path(root), "w", encoding="utf-8") as fh:
        json.dump(row, fh)


def run_gate(root, command="git merge feature/result", session_id=None):
    session_id = SID if session_id is None else session_id
    env = clean_env()
    env["DISPATCH_RETURN_CONTEXT_PATH"] = ctx_path(root)
    env["DISPATCH_RETURN_GRACE_SECONDS"] = "0.1"
    payload = {"cwd": root, "session_id": session_id, "tool_name": "Bash",
               "tool_input": {"command": command}}
    return subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


def transcript(root, texts, boundary="[bridge:injected-turn]\n[dispatch-return] done"):
    entries = [{"type": "user", "message": {"content": boundary}}]
    for t in texts:
        entries.append({"type": "assistant",
                        "message": {"content": [{"type": "text", "text": t}]}})
    path = os.path.join(root, "transcript.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    return path


def run_stop(root, final, texts=None, active=False, event="Stop", prompt=""):
    texts = [final] if texts is None else texts
    payload = {"cwd": root, "hook_event_name": event, "session_id": SID,
               "transcript_path": transcript(root, texts),
               "last_assistant_message": final, "stop_hook_active": active}
    if event == "UserPromptSubmit":
        payload = {"cwd": root, "hook_event_name": event, "prompt": prompt}
    env = clean_env()
    env["PROJECT_ROOT"] = root
    env["ONBOARDING_FILE"] = root
    # DGN-1941: the Stop seat waits out an in-flight delivery; keep it short.
    env["DISPATCH_RETURN_GRACE_SECONDS"] = "0.1"
    env["DISPATCH_RETURN_STOP_DELIVERY_WAIT_SECONDS"] = "0.2"
    return subprocess.run([sys.executable, STOP], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


def log_text(root):
    path = os.path.join(root, ".telegram_bot", "logs", "onboarding-gate.log")
    return open(path, encoding="utf-8").read() if os.path.isfile(path) else ""


def out_of(proc):
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def denial(root):
    try:
        with open(denied_path(root), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def arm_denial(root, **ctx):
    write_ctx(root, **ctx)
    check("arm.deny", run_gate(root).returncode, 0)


def test_gate_reason_and_record():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        proc = run_gate(d)
        reason = out_of(proc)["hookSpecificOutput"]["permissionDecisionReason"]
        check("gate.result_line_kept", "result line" in reason, True)
        check("gate.marker_named", "[[OPTIONS]]" in reason, True)
        check("gate.one_line_proposal", "one-line proposal" in reason, True)
        check("gate.no_bare_promise", "later conversation" in reason, True)
        check("gate.recorded", (denial(d) or {}).get("turn_id"), "turn-1")
        run_gate(d)
        check("gate.count", (denial(d) or {}).get("count"), 2)
        # A read-only inspection is allowed and is not a denial.
        os.remove(denied_path(d))
        shown = run_gate(d, "git show HEAD")
        check("gate.read_allowed", shown.stdout.strip(), "")
        check("gate.read_not_recorded", denial(d), None)


def test_gate_resume_clears_record():
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        write_ctx(d, visible=True)
        proc = run_gate(d)
        check("resume.allowed", proc.stdout.strip(), "")
        check("resume.cleared", denial(d), None)


def test_owner_turn_and_switch_off_record_nothing():
    with tempfile.TemporaryDirectory() as d:
        env = clean_env()
        payload = {"cwd": d, "tool_name": "Bash", "tool_input": {"command": "make"}}
        proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("owner.silent", proc.stdout.strip(), "")
        check("owner.no_record", denial(d), None)
        write_ctx(d)
        env = clean_env()
        env["GATE_DISPATCH_RETURN_RESULT_FIRST"] = "off"
        env["DISPATCH_RETURN_CONTEXT_PATH"] = ctx_path(d)
        env["DISPATCH_RETURN_GRACE_SECONDS"] = "0.1"
        proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("off.no_deny", "permissionDecision" in proc.stdout, False)
        check("off.no_record", denial(d), None)


def test_stop_blocks_final_without_marker():
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        # Buffered final message: the first text was an earlier progress line.
        proc = run_stop(d, "I will do the merge in the next conversation.",
                        texts=["result line", "I will do the merge in the next conversation."])
        out = out_of(proc)
        check("block.decision", (out or {}).get("decision"), "block")
        check("block.reason", "[[OPTIONS]]" in (out or {}).get("reason", ""), True)
        check("block.logged", "DR_BLOCK" in log_text(d), True)
        # Same instruction, non-Korean/English wording: the test is the marker.
        proc = run_stop(d, "明日にします",
                        texts=["result line", "明日にします"])
        check("block.any_language", (out_of(proc) or {}).get("decision"), "block")


def test_stop_passes_marker_in_any_language():
    for final in ("Merge the fix branch now?\n[[OPTIONS]]\n1. proceed\n2. hold",
                  "이어서 병합할까요?\n[[OPTIONS]]\n1. 진행\n2. 보류",
                  "続けますか？\n[[OPTIONS]]\n1. はい\n2. いいえ"):
        with tempfile.TemporaryDirectory() as d:
            arm_denial(d)
            proc = run_stop(d, final, texts=["result line", final])
            check("pass.silent", proc.stdout.strip(), "")
            check("pass.logged", "DR_PASS" in log_text(d), True)
            check("pass.consumed", denial(d), None)


def test_stop_passes_labeled_options_marker():
    # DGN-1715 r3: the labeled marker shape ("[[OPTIONS: a | b]]", DGN-992)
    # is not a substring of the bare "[[OPTIONS]]" token -- this is the exact
    # shape that read as marker-less and forced a duplicate menu.
    for final in ("Merge the fix branch now?\n[[OPTIONS: proceed | hold]]",
                  "이어서 병합할까요?\n[[OPTIONS: 진행 | 보류]]"):
        with tempfile.TemporaryDirectory() as d:
            arm_denial(d)
            proc = run_stop(d, final, texts=["result line", final])
            check("labeled.silent", proc.stdout.strip(), "")
            check("labeled.logged", "DR_PASS" in log_text(d), True)
            check("labeled.consumed", denial(d), None)


def test_stop_still_blocks_marker_mentioned_inline_or_in_fence():
    # No marker shape present -> forcing is unchanged: a marker mentioned
    # mid-sentence, or only inside a fenced code block, never arms a keyboard
    # and must still be treated as marker-less.
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        final = ("I will do the merge in the next conversation (the "
                 "[[OPTIONS]] marker only arms buttons on its own line).")
        proc = run_stop(d, final, texts=["result line", final])
        check("inline.block", (out_of(proc) or {}).get("decision"), "block")
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        final = "I will do the merge in the next conversation.\n```\n[[OPTIONS]]\n```"
        proc = run_stop(d, final, texts=["result line", final])
        check("fenced.block", (out_of(proc) or {}).get("decision"), "block")


def test_stop_passes_when_first_text_already_carried_the_keyboard():
    # DGN-1732: the delivered result-first text had the [[OPTIONS]] menu, so
    # a later marker-less final must not be re-prompted into a second one.
    # DGN-1941: the first stop with an open latch is a RESUME re-prompt
    # (test-dgn1941); this is the stop after it (stop_hook_active).
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        write_ctx(d, visible=True, options_delivered=True)
        proc = run_stop(d, "Noted the CI result as well.",
                        texts=["result\nProceed?\n[[OPTIONS]]\n1. proceed\n2. hold",
                               "Noted the CI result as well."], active=True)
        check("delivered_kb.no_block", proc.stdout.strip(), "")
        check("delivered_kb.logged", "DR_PASS" in log_text(d), True)
        check("delivered_kb.consumed", denial(d), None)


def test_proposal_instruction_forbids_repeating_a_delivered_menu():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        reason = out_of(run_gate(d))["hookSpecificOutput"]["permissionDecisionReason"]
        check("gate.no_repeat", "never repeat it" in reason, True)


def test_stop_miss_visible_reminds_next_prompt_once():
    with tempfile.TemporaryDirectory() as d:
        # The final message is the turn's only text and already on its way
        # (DGN-1941: an OPEN latch is a RESUME re-prompt instead, see
        # test-dgn1941; a delivery still in flight past the wait is a miss).
        arm_denial(d)
        write_ctx(d, delivering=True)
        proc = run_stop(d, "Next conversation I will merge it.")
        check("miss.no_block", proc.stdout.strip(), "")
        check("miss.logged", "DR_MISS_VISIBLE" in log_text(d), True)
        out = out_of(run_stop(d, "", event="UserPromptSubmit", prompt="ok"))
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("miss.reminder", "PREVIOUS dispatch-return turn" in ctx, True)
        again = run_stop(d, "", event="UserPromptSubmit", prompt="ok")
        check("miss.once", again.stdout.strip(), "")


def test_reminder_not_consumed_by_injected_prompt():
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        write_ctx(d, delivering=True)
        run_stop(d, "Next conversation.")
        inj = run_stop(d, "", event="UserPromptSubmit",
                       prompt="[bridge:injected-turn]\n[dispatch-return] x")
        check("inj.silent", inj.stdout.strip(), "")
        out = out_of(run_stop(d, "", event="UserPromptSubmit", prompt="ok"))
        check("inj.kept", "PREVIOUS dispatch-return turn" in json.dumps(out), True)


def test_stop_yields_after_one_reprompt():
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        proc = run_stop(d, "still no marker", texts=["result", "still no marker"], active=True)
        check("yield.no_block", proc.stdout.strip(), "")
        check("yield.logged", "DR_YIELD" in log_text(d), True)


def test_stop_silent_without_denial_or_latch():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        proc = run_stop(d, "plain final without a marker")
        check("nodenial.silent", proc.stdout.strip(), "")
        check("nodenial.no_log", "DR_" in log_text(d), False)
    with tempfile.TemporaryDirectory() as d:
        # Denial from an earlier turn never leaks into a new turn.
        arm_denial(d)
        write_ctx(d, turn_id="turn-2")
        proc = run_stop(d, "plain final without a marker")
        check("stale.silent", proc.stdout.strip(), "")
    with tempfile.TemporaryDirectory() as d:
        # An ordinary owner turn has no latch row at all.
        proc = run_stop(d, "plain final without a marker")
        check("owner.silent", proc.stdout.strip(), "")


def test_other_session_untouched():
    """DGN-1738: a detached worker shares the instance-wide record."""
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        proc = run_gate(d, session_id="detached-worker")
        check("other.gate_allows", proc.stdout.strip(), "")
        check("other.no_record", denial(d), None)
        arm_denial(d)
        payload = {"cwd": d, "hook_event_name": "Stop", "session_id": "detached-worker",
                   "transcript_path": transcript(d, ["r", "final"]),
                   "last_assistant_message": "final"}
        env = clean_env()
        env["PROJECT_ROOT"] = d
        env["ONBOARDING_FILE"] = d
        proc = subprocess.run([sys.executable, STOP], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("other.stop_no_block", '"block"' in proc.stdout, False)


def test_stop_switch_off_does_not_block():
    with tempfile.TemporaryDirectory() as d:
        arm_denial(d)
        payload = {"cwd": d, "hook_event_name": "Stop", "session_id": SID,
                   "transcript_path": transcript(d, ["r", "final"]),
                   "last_assistant_message": "final"}
        env = clean_env()
        env["PROJECT_ROOT"] = d
        env["ONBOARDING_FILE"] = d
        env["GATE_DISPATCH_RETURN_RESULT_FIRST"] = "off"
        proc = subprocess.run([sys.executable, STOP], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("switch.no_block", '"block"' in proc.stdout, False)


for name, fn in sorted(globals().items()):
    if name.startswith("test_") and callable(fn):
        fn()

if _failures:
    print("FAIL test-dgn1715-dispatch-return-forcing")
    for f in _failures:
        print("  " + f)
    sys.exit(1)
print("OK test-dgn1715-dispatch-return-forcing")
