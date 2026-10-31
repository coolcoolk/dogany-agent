#!/usr/bin/env python3
# test-lane: fast
"""DGN-1941: a dispatch-return turn whose follow-up was denied BEFORE the
result line, and whose result line then reached the owner, must not end on
the gate-induced stall: the Stop seat re-prompts once to re-run the denied
step (verdict RESUME) because the latch is open now.

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
import threading
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
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "GATE_DISPATCH_RETURN_RESULT_FIRST", "GATE_ALL", "GATE_STOP_ALL",
              "GATE_SWITCH_CONF", "DISPATCH_RETURN_CONTEXT_PATH"):
        env.pop(k, None)
    env["DISPATCH_RETURN_GRACE_SECONDS"] = "0.1"
    env["DISPATCH_RETURN_DELIVERY_WAIT_SECONDS"] = "0.2"
    env["DISPATCH_RETURN_STOP_DELIVERY_WAIT_SECONDS"] = "2.0"
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
    return subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


def run_stop(root, final, active=False):
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
    env["ONBOARDING_FILE"] = root
    return subprocess.run([sys.executable, STOP], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


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
INVENTED_MENU = ("형님, 이번에도 결과를 받는 중에 차단이 걸려서 카드를 아직 합치지 "
                 "못했어요. 아래 버튼을 누르시면 바로 합칠게요.\n\n[[OPTIONS: 카드 받기]]")


def test_live_1540_shape_resumes_instead_of_ending_on_invented_menu():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)                                   # latch closed
        denied = out_of(run_gate(d, MERGE))            # Bash before any text
        check("1540.denied", (denied or {}).get("hookSpecificOutput", {})
              .get("permissionDecision"), "deny")
        # The model writes the result line; the bridge delivers it with its
        # keyboard (DGN-1732 options_delivered) -> latch open.
        write_ctx(d, visible=True, options_delivered=True, delivered_via="first_text")
        out = out_of(run_stop(d, INVENTED_MENU))
        check("1540.block", (out or {}).get("decision"), "block")
        check("1540.resume_reason", "DGN-1941" in (out or {}).get("reason", ""), True)
        check("1540.logged", "DR_RESUME" in log_text(d), True)
        check("1540.denial_kept", (denial(d) or {}).get("turn_id"), "turn-1")
        # The re-run passes the open gate and clears the denial ...
        check("1540.rerun_allowed", run_gate(d, MERGE).stdout.strip(), "")
        check("1540.cleared", denial(d), None)
        # ... so the next stop has nothing to say.
        check("1540.next_stop_silent", run_stop(d, "합쳤어요.", active=True).stdout.strip(), "")


def test_live_1501_shape_marker_less_final_resumes():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True, delivered_via="first_text")
        out = out_of(run_stop(d, "형님, 3차 검증 결과는 BLOCK이에요. 다음에 이어서 할게요."))
        check("1501.block", (out or {}).get("decision"), "block")
        check("1501.not_miss", "DR_MISS_VISIBLE" in log_text(d), False)


def test_resume_is_once_then_old_semantics():
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, visible=True, options_delivered=True)
        # Second stop without re-running: the model judged it a real owner
        # decision; the delivered keyboard stands (DGN-1732 PASS), no loop.
        proc = run_stop(d, "", active=True)
        check("once.no_block", proc.stdout.strip(), "")
        check("once.pass", "DR_PASS" in log_text(d), True)


def test_stop_waits_for_in_flight_delivery():
    # The Stop hook can run before the bridge's push returns: delivering ->
    # visible mid-wait still resumes (the DGN-1715 race, Stop side).
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        write_ctx(d, delivering=True)
        flip = threading.Timer(0.5, lambda: write_ctx(d, visible=True))
        flip.start()
        try:
            out = out_of(run_stop(d, "result line"))
        finally:
            flip.cancel()
        check("race.block", (out or {}).get("decision"), "block")
        check("race.resume", "DR_RESUME" in log_text(d), True)


def test_closed_latch_keeps_dgn1715_block():
    # The push failed (latch still closed): tools would still be denied, so
    # RESUME must not fire; the DGN-1715 marker block stands.
    with tempfile.TemporaryDirectory() as d:
        write_ctx(d)
        run_gate(d, MERGE)
        out = out_of(run_stop(d, "I will merge it next conversation."))
        check("closed.block", (out or {}).get("decision"), "block")
        check("closed.not_resume", "DR_RESUME" in log_text(d), False)
        check("closed.dr_block", "DR_BLOCK" in log_text(d), True)


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
        proc = subprocess.run([sys.executable, STOP], input=json.dumps(payload),
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
