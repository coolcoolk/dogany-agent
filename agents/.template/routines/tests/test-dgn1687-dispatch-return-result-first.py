#!/usr/bin/env python3
# test-lane: fast
"""DGN-1687 result-first gate: block work before visible result, then allow."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SID = "owner-session"
GATE = os.path.normpath(os.path.join(HERE, "..", "dispatch-return-result-first-gate.py"))


def run(payload, context=None, grace="0.2"):
    env = dict(os.environ)
    env.pop("GATE_DISPATCH_RETURN_RESULT_FIRST", None)
    env.pop("GATE_ALL", None)
    env["DISPATCH_RETURN_GRACE_SECONDS"] = grace
    if context:
        env["DISPATCH_RETURN_CONTEXT_PATH"] = context
    else:
        env.pop("DISPATCH_RETURN_CONTEXT_PATH", None)
    return subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=15)


def deny(proc):
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    return json.loads(proc.stdout)["hookSpecificOutput"].get("permissionDecision") == "deny"


with tempfile.TemporaryDirectory() as root:
    context = os.path.join(root, "dispatch-return-context.json")
    payload = {"cwd": root, "tool_name": "Bash", "session_id": SID,
               "tool_input": {"command": "git merge feature/result"}}
    with open(context, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "turn_id": "turn-1", "session_id": SID, "visible": False,
                   "ts": time.time()}, fh)
    blocked = run(payload, context)
    assert deny(blocked), blocked.stdout
    assert "result line" in blocked.stdout, blocked.stdout

    # Read-only result inspection remains available before the owner-visible line.
    shown = run({"cwd": root, "tool_name": "Bash",
                 "tool_input": {"command": "git show HEAD"}}, context)
    assert shown.returncode == 0 and not shown.stdout.strip(), shown.stdout

    with open(context, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "turn_id": "turn-1", "session_id": SID, "visible": True,
                   "ts": time.time()}, fh)
    allowed = run(payload, context)
    assert allowed.returncode == 0 and not allowed.stdout.strip(), allowed.stdout

    # DGN-1688: an intentional self-cancel recovery is exempt even if a
    # stale context file survived a bridge interruption.
    with open(context, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "turn_id": "turn-1", "session_id": SID, "visible": False,
                   "cancel_by": "self", "ts": time.time()}, fh)
    exempt = run(payload, context)
    assert exempt.returncode == 0 and not exempt.stdout.strip(), exempt.stdout

    # DGN-1715: the CLI runs this hook right after the text block, while the
    # bridge is still pushing it.  An in-flight delivery is waited out, not
    # denied; a failed delivery still denies.
    def write(delay, **fields):
        def _do():
            time.sleep(delay)
            row = {"schema": 1, "turn_id": "turn-1", "session_id": SID, "visible": False, "ts": time.time()}
            row.update(fields)
            with open(context + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(row, fh)
            os.replace(context + ".tmp", context)
        return threading.Thread(target=_do)

    write(0, delivering=True).run()
    flip = write(0.5, visible=True)
    flip.start()
    started = time.monotonic()
    waited = run(payload, context)
    flip.join()
    assert waited.returncode == 0 and not waited.stdout.strip(), waited.stdout
    assert time.monotonic() - started >= 0.4

    write(0, delivering=True).run()
    failed = write(0.3, delivering=False)
    failed.start()
    refused = run(payload, context)
    failed.join()
    assert deny(refused), refused.stdout

    # The hook can even beat the bridge to the text block: the grace window
    # sees the delivery start, then its success.
    write(0).run()
    begin, done = write(0.2, delivering=True), write(0.6, visible=True)
    begin.start(); done.start()
    raced = run(payload, context, grace="1.0")
    begin.join(); done.join()
    assert raced.returncode == 0 and not raced.stdout.strip(), raced.stdout

    # DGN-1738: the record is instance-wide; only the owning session is gated.
    write(0).run()
    assert deny(run(payload, context)), "owner session must stay blocked"
    other = run(dict(payload, session_id="detached-worker"), context)
    assert other.returncode == 0 and not other.stdout.strip(), other.stdout
    nosid = run({k: v for k, v in payload.items() if k != "session_id"}, context)
    assert nosid.returncode == 0 and not nosid.stdout.strip(), nosid.stdout
    # A record without a session id cannot name an owner: fail open.
    with open(context, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "turn_id": "turn-1", "visible": False,
                   "ts": time.time()}, fh)
    norec = run(payload, context)
    assert norec.returncode == 0 and not norec.stdout.strip(), norec.stdout

    # An ordinary owner turn has no bridge latch, even if it starts work directly.
    owner = run(payload)
    assert owner.returncode == 0 and not owner.stdout.strip(), owner.stdout

print("OK test-dgn1687-dispatch-return-result-first")
