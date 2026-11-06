#!/usr/bin/env python3
# test-lane: fast
# test-tier: 0
# component: dispatch-return-onboarding-hook
# promise: Stop never refuses a finished dispatch-return reply, including in-flight delivery.
# promise: Stop retains unresolved denials and consumes PASS/MISS_VISIBLE denials.
# promise: Pending injected turns retain the SKIP_INJECTED recording.
# promise: Pending owner prompts receive onboarding guidance before generation.
"""DGN-1941 fixtures under the 2.7.0 dec-650 contract: Stop records only;
it never re-prompts after a reply. The tool latch still denies work before
delivery and permits the follow-up in the same turn after delivery.

Fixtures contain only latch/denial rows and a two-entry transcript. The
pending-onboarding case adds only a marker and one unset identity field.
No instance/service tree, installer, git remote or network is needed.

Live shape (dev instance, 2026-10-08 15:01 / 15:24 / 15:40 / 16:00 / 16:00:41,
session a3b8e4b7 transcript): Read out.log -> Bash (merge / inspection) with
no text yet -> denied (correct) -> the model writes the result line, which
the bridge delivers (latch visible), and ENDS the turn -- on an invented
"[[OPTIONS: 카드 받기]]" menu (DR_PASS in onboarding-gate.log) or on a bare
"blocked, next time" line (MISS_VISIBLE). Before this fix both stops were
let through; the owner had to tap to get the merge done.
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
STOP = os.path.join(ROUTINES, "onboarding-gate-stop.py")
SID = "owner-session"

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


def clean_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_DISPATCH_RETURN_RESULT_FIRST", "GATE_ALL", "GATE_STOP_ALL",
              "GATE_SWITCH_CONF", "GATE_SWITCH_LOG", "DISPATCH_RETURN_CONTEXT_PATH"):
        env.pop(k, None)
    # Delivery states are explicit fixture inputs; never wait for a clock.
    env["DISPATCH_RETURN_GRACE_SECONDS"] = "0"
    env["DISPATCH_RETURN_DELIVERY_WAIT_SECONDS"] = "0"
    env["DISPATCH_RETURN_STOP_DELIVERY_WAIT_SECONDS"] = "0"
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
    tmp = ctx_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(row, fh)
    os.replace(tmp, ctx_path(root))


def run_gate(root, command):
    env = clean_env()
    env["DISPATCH_RETURN_CONTEXT_PATH"] = ctx_path(root)
    payload = {"cwd": root, "session_id": SID, "tool_name": "Bash",
               "tool_input": {"command": command}}
    proc = subprocess.run([sys.executable, "-B", GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)
    check("tool.rc", proc.returncode, 0)
    return proc


def run_stop(root, final, active=False, onboarding_file=None):
    entries = [{"type": "user", "message": {
        "content": "[bridge:injected-turn]\n[dispatch-return] done"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}}]
    tpath = os.path.join(root, "transcript.jsonl")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")
    payload = {"cwd": root, "hook_event_name": "Stop", "session_id": SID,
               "transcript_path": tpath, "last_assistant_message": final,
               "stop_hook_active": active}
    env = clean_env()
    env["PROJECT_ROOT"] = root
    env["ONBOARDING_FILE"] = onboarding_file or root
    proc = subprocess.run([sys.executable, "-B", STOP], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)
    check("stop.rc", proc.returncode, 0)
    return proc


def out_of(proc):
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def log_text(root):
    path = os.path.join(root, ".telegram_bot", "logs", "onboarding-gate.log")
    return open(path, encoding="utf-8").read() if os.path.isfile(path) else ""


def denial(root):
    try:
        with open(denied_path(root), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


MERGE = "cd /agent; git merge --no-edit dsp/v27-ds-realcards-r3 2>&1 | tail -1"
INVENTED_MENU = ("사용자, 이번에도 결과를 받는 중에 차단이 걸려서 카드를 아직 합치지 "
                 "못했어요. 아래 버튼을 누르시면 바로 합칠게요.\n\n[[OPTIONS: 카드 받기]]")


def test_live_1540_shape_records_without_reprompting_invented_menu():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)                                   # latch closed
        denied = out_of(run_gate(d, MERGE))            # Bash before any text
        check("1540.denied", (denied or {}).get("hookSpecificOutput", {})
              .get("permissionDecision"), "deny")
        # dec-650: guidance that still exists is in the pre-tool deny,
        # before the follow-up; Stop records the outcome without emitting
        # a DGN-1941 resume reason after the reply.
        check("1540.guidance", "follow-up tools open in this same turn" in
              (denied or {}).get("hookSpecificOutput", {})
              .get("permissionDecisionReason", ""), True)
        # The model writes the result line; the bridge delivers it with its
        # keyboard (DGN-1732 options_delivered) -> latch open.
        write_ctx(d, visible=True, options_delivered=True, delivered_via="first_text")
        out = out_of(run_stop(d, INVENTED_MENU))
        # dec-650: retain the 2.6.5 recording/consumption assertions;
        # only the Stop block and its resume reason are retired.
        check("1540.no_block", out, None)
        check("1540.logged", "DR_RESUME" in log_text(d), True)
        check("1540.denial_kept", (denial(d) or {}).get("turn_id"), "turn-1")
        # The re-run passes the open gate and clears the denial ...
        check("1540.rerun_allowed", run_gate(d, MERGE).stdout.strip(), "")
        check("1540.cleared", denial(d), None)
        # ... so the next stop has nothing to say.
        check("1540.next_stop_silent", run_stop(d, "합쳤어요.", active=True).stdout.strip(), "")


def test_live_1501_shape_marker_less_final_does_not_reprompt():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True, delivered_via="first_text")
        out = out_of(run_stop(d, "사용자, 3차 검증 결과는 BLOCK이에요. 다음에 이어서 할게요."))
        # dec-650 A6: marker-less replies cannot trigger a Stop refusal.
        check("1501.no_block", out, None)
        check("1501.logged", "DR_RESUME" in log_text(d), True)
        check("1501.denial_kept", (denial(d) or {}).get("turn_id"), "turn-1")
        check("1501.not_miss", "DR_MISS_VISIBLE" in log_text(d), False)


def test_active_stop_does_not_reenter_retired_dispatch_forcing():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True, options_delivered=True)
        # dec-650: active Stop retains DR_PASS recording and consumes the
        # denial as in 2.6.5, without refusing the delivered keyboard.
        proc = run_stop(d, "", active=True)
        check("once.no_block", proc.stdout.strip(), "")
        check("once.pass", "DR_PASS" in log_text(d), True)
        check("once.denial_consumed", denial(d), None)


def test_stop_during_in_flight_delivery_does_not_reprompt():
    # dec-650 A6: Stop no longer waits for delivery to force a resume.
    # Exercise the in-flight and delivered states explicitly, without a timer.
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, delivering=True)
        out = out_of(run_stop(d, "result line"))
        check("race.no_block", out, None)
        check("race.miss_visible", "DR_MISS_VISIBLE" in log_text(d), True)
        check("race.denial_consumed", denial(d), None)
        write_ctx(d, visible=True)
        check("race.rerun_allowed", run_gate(d, MERGE).stdout.strip(), "")
        check("race.cleared", denial(d), None)


def test_closed_latch_keeps_tool_denial_without_stop_block():
    # dec-650 A6: a closed latch still denies tools, but the retired
    # DGN-1715 Stop refusal and DR_BLOCK log must not return.
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        out = out_of(run_stop(d, "I will merge it next conversation."))
        check("closed.no_block", out, None)
        check("closed.not_resume", "DR_RESUME" in log_text(d), False)
        check("closed.miss_recorded", "DR_MISS" in log_text(d), True)
        check("closed.denial_kept", (denial(d) or {}).get("turn_id"), "turn-1")
        check("closed.tool_still_denied", (out_of(run_gate(d, MERGE)) or {})
              .get("hookSpecificOutput", {}).get("permissionDecision"), "deny")


def test_pending_injected_turn_records_and_next_owner_prompt_guides():
    # dec-650 A6: preserve Stop recording, with guidance before generation.
    with tempfile.TemporaryDirectory() as d:
        identity = os.path.join(d, "pending-fixture.md")
        with open(identity, "w", encoding="utf-8") as fh:
            fh.write("<!-- ONBOARDING_PENDING -->\n- Name: (set at onboarding)\n")
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True)
        proc = run_stop(d, INVENTED_MENU, onboarding_file=identity)
        check("pending.stop_rc", proc.returncode, 0)
        check("pending.no_block", proc.stdout.strip(), "")
        check("pending.recorded", "SKIP_INJECTED" in log_text(d), True)
        check("pending.denial_kept", (denial(d) or {}).get("turn_id"), "turn-1")
        payload = {"cwd": d, "hook_event_name": "UserPromptSubmit",
                   "session_id": SID, "prompt": "Please continue."}
        env = clean_env()
        env["PROJECT_ROOT"] = d
        env["ONBOARDING_FILE"] = identity
        proc = subprocess.run([sys.executable, "-B", STOP], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("pending.prompt_rc", proc.returncode, 0)
        out = out_of(proc) or {}
        check("pending.prompt_no_block", out.get("decision"), None)
        context = out.get("hookSpecificOutput", {})
        check("pending.prompt_event", context.get("hookEventName"), "UserPromptSubmit")
        check("pending.guidance", "Before answering:" in context.get("additionalContext", ""), True)
        check("pending.pre_generation", "PRE_GENERATION" in log_text(d), True)


def test_no_denial_no_resume():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d, visible=True)
        check("nodenial.silent", run_stop(d, "result, merged.").stdout.strip(), "")


def test_switch_off_no_resume():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True)
        entries = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "r"}]}}]
        tpath = os.path.join(d, "t.jsonl")
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entries[0]) + "\n")
        payload = {"cwd": d, "hook_event_name": "Stop", "session_id": SID,
                   "transcript_path": tpath, "last_assistant_message": "r"}
        env = clean_env()
        env["PROJECT_ROOT"] = d
        env["ONBOARDING_FILE"] = d
        env["GATE_DISPATCH_RETURN_RESULT_FIRST"] = "off"
        proc = subprocess.run([sys.executable, "-B", STOP], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=15)
        check("switch.no_block", '"block"' in proc.stdout, False)


for name, fn in sorted(globals().items()):
    if name.startswith("test_") and callable(fn):
        fn()

if _failures:
    print("FAIL test-dgn1941-resume-after-open")
    for f in _failures:
        print("  " + f)
    sys.exit(1)
print("OK test-dgn1941-resume-after-open")
