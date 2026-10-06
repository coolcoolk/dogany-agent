#!/usr/bin/env python3
"""Codex payload refusal tests for routines/codex-hook-adapter.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util


HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "codex-hook-adapter.py")
LEDGER = os.path.join(HERE, "decomp-ledger.py")
SPEC = importlib.util.spec_from_file_location("codex_hook_adapter", ADAPTER)
ADAPTER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER_MODULE)


def patch(*headers, body="+text"):
    return "\n".join(["*** Begin Patch", *headers, body, "*** End Patch", ""])


def run(gate, payload, env=None, adapter=ADAPTER):
    result = subprocess.run(
        [sys.executable, adapter, "--gate", gate], input=json.dumps(payload),
        text=True, capture_output=True, timeout=20, env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def bare_adapter(tmp, name):
    """Copy the adapter alone into an empty dir and return the copy's path.

    The adapter resolves every gate script next to its own file, so a copy
    with no siblings IS the "gate script deliberately removed" case -- and it
    stays that case no matter which gate axes later land in this tree.
    """
    root = os.path.join(tmp, name)
    os.makedirs(root, exist_ok=True)
    copy = os.path.join(root, "codex-hook-adapter.py")
    shutil.copy(ADAPTER, copy)
    return copy


def reason_of(doc):
    return doc.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def run_stop(payload, env, adapter=ADAPTER):
    result = subprocess.run(
        [sys.executable, adapter, "--stop"], input=json.dumps(payload),
        text=True, capture_output=True, timeout=20, env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


def denied(doc):
    return doc.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def apply(command, cwd):
    return {"tool_name": "apply_patch", "cwd": cwd,
            "tool_input": {"command": command}}


def test_memory_deny_and_allow(tmp):
    out = run("memory", apply(patch("*** Update File: memories/private.md"), tmp))
    assert denied(out), out
    out = run("memory", apply(patch("*** Update File: files/notes.md"), tmp))
    assert not denied(out), out


def test_skill_deny_and_allow(tmp):
    out = run("skill-landing", apply(patch("*** Add File: skills/new/SKILL.md"), tmp))
    assert denied(out), out
    out = run("skill-landing", apply(patch("*** Add File: files/notes.md"), tmp))
    assert not denied(out), out


def test_tmp_deny_all_targets_and_allow(tmp):
    out = run("tmp-artifact", apply(patch(
        "*** Add File: files/tmp/scratch.txt",
        "*** Add File: files/tmp/release-spec.md"), tmp))
    assert denied(out), out
    out = run("tmp-artifact", apply(patch("*** Add File: files/tmp/scratch.txt"), tmp))
    assert not denied(out), out


def test_done_deny_and_allow(tmp):
    content = "+---\n+status: done\n+---"
    out = run("done-claim", apply(patch("*** Update File: worklog/DGN-1.md", body=content), tmp))
    assert denied(out), out
    content = "+---\n+status: done\n+verify: test passed\n+---"
    out = run("done-claim", apply(patch("*** Update File: worklog/DGN-1.md", body=content), tmp))
    assert not denied(out), out


def test_spawn_gates_deny_and_allow(tmp):
    payload = {"tool_name": "spawn_agent", "cwd": tmp,
               "tool_input": {"message": "do work", "model": "sonnet"}}
    payload["tool_input"]["message"] += "\nIN-SESSION: needed immediately in this turn"
    out = run("in-session-dispatch", payload)
    assert not denied(out), out
    payload["tool_input"].pop("model")
    out = run("in-session-dispatch", payload)
    assert denied(out), out


def test_malformed_fails_closed(tmp):
    out = run("tmp-artifact", apply("not an apply patch", tmp))
    assert denied(out), out
    result = subprocess.run([sys.executable, ADAPTER, "--gate", "tmp-artifact"],
                            input="{bad", text=True, capture_output=True, timeout=20)
    assert result.returncode == 0
    assert denied(json.loads(result.stdout)), result.stdout


def test_malformed_legacy_output_fails_closed(tmp):
    decision, _ = ADAPTER_MODULE.gate_decision("not-json")
    assert decision is None
    assert ADAPTER_MODULE.stop_reason("not-json") is None


def test_stop_blocks_open_declaration(tmp):
    state = os.path.join(tmp, "state", "decomp-ledger.log")
    env = dict(os.environ, DECOMP_LEDGER_PATH=state)
    result = subprocess.run(
        [sys.executable, LEDGER, "declare", "unfinished item"], text=True,
        capture_output=True, timeout=20, env=env,
    )
    assert result.returncode == 0, result.stderr
    out = run_stop({"cwd": tmp}, env)
    assert out.get("decision") == "block", out


def test_stop_allows_clean_ledger(tmp):
    state = os.path.join(tmp, "clean-state", "decomp-ledger.log")
    env = dict(os.environ, DECOMP_LEDGER_PATH=state)
    out = run_stop({"cwd": tmp}, env)
    assert out == {}, out


def gate_scripts_present(*names):
    """True when every named gate/ledger script exists next to the adapter.

    The canonical template ships the adapter before some Stop axes land
    (decomp-ledger.py, kept instance-local with decomp-gate.py -- see the
    adapter's NOT_ENFORCED_HERE note). The cases that need such a script only
    run where it exists, so this suite passes identically on an instance with
    the full battery (zero skips) and on a bare template checkout (loud SKIP
    lines). The missing-script BEHAVIOUR itself is not skipped: the DGN-1560
    tests below build their own bare tree and always run.
    """
    return all(os.path.exists(os.path.join(HERE, n)) for n in names)


def test_missing_gate_deny_names_the_missing_path(tmp):
    """DGN-1560: an absent gate script still denies, and says which one."""
    adapter = bare_adapter(tmp, "no-gates")
    out = run("tmp-artifact", apply(patch("*** Add File: files/notes.md"), tmp),
              adapter=adapter)
    assert denied(out), out
    reason = reason_of(out)
    expected = os.path.join(os.path.dirname(adapter), "tmp-artifact-gate.py")
    assert expected in reason, reason
    assert "MISSING" in reason, reason
    # the whole point: not the bare exit code the 47-minute dispatch saw
    assert "exited non-zero" not in reason, reason


def test_missing_gate_deny_covers_every_enforced_gate(tmp):
    """Every name in GATES gets the named-path refusal, not just one."""
    adapter = bare_adapter(tmp, "no-gates-sweep")
    root = os.path.dirname(adapter)
    for gate, script in ADAPTER_MODULE.GATES.items():
        if gate == "in-session-dispatch":
            payload = {"tool_name": "spawn_agent", "cwd": tmp,
                       "tool_input": {"message": "do work", "model": "sonnet"}}
        else:
            payload = apply(patch("*** Add File: files/notes.md"), tmp)
        out = run(gate, payload, adapter=adapter)
        assert denied(out), (gate, out)
        assert os.path.join(root, script) in reason_of(out), (gate, out)


def test_optional_gate_passes_through_where_absent(tmp):
    """An optional gate this tree does not ship is recognized, not a crash.

    The managed hook wires --gate decomp machine-wide (DGN-1444); an unknown
    choice would exit 2, which codex-hook-launcher.py reads as a crashed
    adapter and turns into a deny of every spawn_agent. Run against a bare
    tree so the assertion holds on an instance that DOES ship the gate too.
    """
    adapter = bare_adapter(tmp, "no-optional")
    payload = {"tool_name": "spawn_agent", "cwd": tmp,
               "tool_input": {"message": "do work", "model": "sonnet"}}
    result = subprocess.run(
        [sys.executable, adapter, "--gate", "decomp"], input=json.dumps(payload),
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout
    assert "decomp" in result.stderr and "not enforced" in result.stderr, result.stderr


def test_optional_gate_still_enforced_where_present(tmp):
    """Where decomp-gate.py DOES ship, the adapter runs it exactly as before.

    Absent here, armed there, one file: DGN-1560 must not disarm the
    instances that carry the doctrine this gate forces.
    """
    payload = {"tool_name": "spawn_agent", "cwd": tmp,
               "tool_input": {"message": "do work", "model": "sonnet"}}
    out = run("decomp", payload)
    assert denied(out), out
    state = os.path.join(tmp, "optional-state", "decomp-ledger.log")
    env = dict(os.environ, DECOMP_LEDGER_PATH=state)
    sentinel = os.path.join(tmp, ".claude", ".decomp-declared")
    result = subprocess.run(
        [sys.executable, LEDGER, "declare", "single item, no parallel",
         "--sentinel", sentinel], text=True, capture_output=True, timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    out = run("decomp", payload, env)
    assert not denied(out), out


def test_missing_stop_checker_allows_and_names_the_path(tmp):
    """An absent Stop checker allows the turn to end -- and says why."""
    adapter = bare_adapter(tmp, "no-stop")
    result = subprocess.run(
        [sys.executable, adapter, "--stop"], input=json.dumps({"cwd": tmp}),
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout
    expected = os.path.join(os.path.dirname(adapter), "decomp-ledger.py")
    assert expected in result.stderr, result.stderr


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tests = [
            (test_memory_deny_and_allow, ()),
            (test_skill_deny_and_allow, ()),
            (test_tmp_deny_all_targets_and_allow, ("tmp-artifact-gate.py",)),
            (test_done_deny_and_allow, ()),
            (test_spawn_gates_deny_and_allow, ()),
            # malformed payloads are denied before any gate script runs,
            # so this case needs no gate axis present
            (test_malformed_fails_closed, ()),
            (test_malformed_legacy_output_fails_closed, ()),
            (test_stop_blocks_open_declaration, ("decomp-ledger.py",)),
            (test_stop_allows_clean_ledger, ("decomp-ledger.py",)),
            # DGN-1560: these build their own bare tree, so they never skip
            (test_missing_gate_deny_names_the_missing_path, ()),
            (test_missing_gate_deny_covers_every_enforced_gate, ()),
            (test_optional_gate_passes_through_where_absent, ()),
            (test_optional_gate_still_enforced_where_present,
             ("decomp-gate.py", "decomp-ledger.py")),
            (test_missing_stop_checker_allows_and_names_the_path, ()),
        ]
        skipped = 0
        for test, needs in tests:
            if needs and not gate_scripts_present(*needs):
                skipped += 1
                print("SKIP", test.__name__,
                      "(gate axis not landed here: %s)" % ", ".join(needs))
                continue
            test(tmp)
            print("PASS", test.__name__)
    if skipped:
        print("RESULT: Codex adapter refusal tests passed"
              " (%d skipped: gate axes not landed in this tree)" % skipped)
    else:
        print("RESULT: all Codex adapter refusal tests passed")


if __name__ == "__main__":
    main()
