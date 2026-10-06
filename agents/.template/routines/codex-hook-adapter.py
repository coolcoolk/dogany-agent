#!/usr/bin/env python3
"""Translate Codex hook payloads for the existing blocking gates.

Codex exposes apply_patch as one tool whose patch text carries every target
path. The legacy gates consume one Claude-shaped Write/Edit payload containing
tool_input.file_path. This adapter parses every supported apply_patch target
and invokes the selected legacy gate once for each target.

The adapter deliberately denies an apply_patch payload with no parseable,
supported targets. Allowing it would recreate the fail-open path this adapter
exists to remove. Legacy gates retain their own malformed-input policy; an
unparseable legacy-gate response is denied here because it cannot establish a
safe allow decision.

Codex concepts without a safe Claude analogue are not synthesized: shell
commands that write files, arbitrary MCP tool writes, and non-apply_patch
file mutations are outside this adapter's scope.

MISSING-SCRIPT POLICY (DGN-1560). The gate battery is not identical in every
tree, so "the script is not here" is a case this adapter answers explicitly
rather than folding into the generic non-zero path:
  - A gate in GATES whose script is absent -> DENY, with a refusal that names
    the missing path. Fail-closed is kept (an unanswerable write gate must not
    let the write through); what changes is that the operator can read the
    cause. The bare "legacy gate exited non-zero: 2" it replaces was
    indistinguishable from a gate that ran and crashed, and it repeated until
    dsp-20260918-085228-2803 timed out after 47 minutes with every apply_patch
    denied and its finished patch unwritten.
  - A gate in OPTIONAL_GATES whose script is absent -> pass through with a
    stderr note. The name is wired machine-wide but the mechanism is
    deliberately instance-local; there is no gate in this tree to fail closed
    on. Where the script IS present it runs and enforces exactly as before.
  - The Stop checker absent -> ALLOW with a stderr note, matching
    codex-hook-launcher.py's own Stop policy for a gateless instance. A Stop
    checker is a completeness check, not a write gate.
"""
import argparse
import json
import os
import re
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))

# Gate name -> the script this tree ships for it. Every entry here is a gate
# this adapter ENFORCES: if its script is absent the write is denied, loudly
# (see run_gate).
GATES = {
    "memory": "cc-memory-write-guard.py",
    "skill-landing": "skill-landing-gate.py",
    "tmp-artifact": "tmp-artifact-gate.py",
    "done-claim": "done-claim-gate.py",
    "in-session-dispatch": "in-session-dispatch-gate.py",
    "dispatch-return-result-first": "dispatch-return-result-first-gate.py",
}
STOP_SCRIPT = "decomp-ledger.py"

# Gate names this adapter honours only where the tree actually ships the
# script, because the backing mechanism is deliberately instance-local
# (DGN-1560 part A). Present -> it runs and enforces, unchanged. Absent ->
# pass through with a note; it is not a missing-install defect.
#
# `decomp` is the recorded case. done-claim-gate.py's own header states that
# decomp-gate.py / decomp-ledger.py (DGN-1009 item 3) were NOT carried in the
# DGN-1079-group2-carryback pass: the work-SET decomposition they force is a
# forcing point for a Lane-routing doctrine that lives only in one dev
# instance's persona ("Decomposition mandatory: output work-SET, fan out in
# one message"), not in this template's identity/hot.custom.agent.md -- whose
# "Default parallel autonomy" section is scoped to execution ORDER and asks
# for no enumeration at all. Shipping the gate without the doctrine it
# enforces is the false-forcing-point defect DGN-1006 targets, so the file
# stays out and the name comes out of GATES.
#
# It stays a legal --gate value on purpose, and resolves by PRESENCE rather
# than by deletion, for two reasons. (1) The estate's MACHINE-WIDE managed
# hook (/etc/codex/requirements.toml, DGN-1444) wires `--gate decomp` for
# every codex session on the host. codex-hook-launcher.py passes this
# adapter's exit code straight out (`sys.exit(result.returncode)`), and 2 is
# codex's blocking-error code -- so dropping the choice would turn argparse
# usage text into a block of every spawn_agent: the same write-dead shape,
# one matcher over. (2) Instances that ARM the gate carry this adapter too;
# removing the name outright would propagate a silent disarm of a gate that
# is live there. Absent here, armed there, one file.
#
# Measured caveat (2026-09-18, a dev instance): "armed there" is mechanical,
# not doctrinal. That instance arms decomp-gate.py in .claude/settings.local.json while
# its persona's parallel-autonomy section is scoped to ORDER only and never
# declares the work-SET enumeration the gate demands -- i.e. a gate forcing
# a rule its own instance has not declared, which is the DGN-1006 shape this
# comment cites one paragraph up. That is an instance-ARMING question (which
# instances arm it, and whether the doctrine belongs in their persona), NOT
# a reason to change how this adapter resolves the name. Do not "fix" it
# here.
OPTIONAL_GATES = {
    "decomp": "decomp-gate.py",
}
OPTIONAL_GATES_REASON = {
    "decomp": "decomp-gate.py is instance-local by design (DGN-1009 work-SET "
              "doctrine, kept out per DGN-1006); this tree ships no such gate",
}
ALL_GATES = dict(GATES, **OPTIONAL_GATES)

PATCH_HEADER = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$")
MOVE_HEADER = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def deny(reason):
    # Codex's PreToolUse contract has no top-level "decision": "deny" shape.
    # It recognizes hookSpecificOutput.permissionDecision == "deny" (current)
    # or the legacy {"decision": "block", ...} shape -- never plain "deny".
    # Confirmed against the shipped hooks.json spec (learn.chatgpt.com/docs/hooks.md).
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))


def note(message):
    """Say something to the operator without emitting a hook decision."""
    print("codex-hook-adapter: " + message, file=sys.stderr)


def patch_targets(command):
    """Return all apply_patch targets, or None for an unsafe unknown shape."""
    if not isinstance(command, str) or not command.strip():
        return None
    lines = command.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        return None
    if not any(line.strip() == "*** End Patch" for line in lines):
        return None
    targets = []
    for line in lines:
        match = PATCH_HEADER.match(line)
        if match:
            path = match.group(1).strip()
            if not path or path == "/dev/null":
                return None
            targets.append(path)
            continue
        match = MOVE_HEADER.match(line)
        if match:
            path = match.group(1).strip()
            if not path or path == "/dev/null":
                return None
            targets.append(path)
    return list(dict.fromkeys(targets)) or None


def patch_written_content(command):
    """Expose added patch text as the closest available write-content view."""
    lines = []
    for line in command.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return "\n".join(lines)


def normalize_apply_patch(data):
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    targets = patch_targets(command)
    if targets is None:
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    return [
        {
            "tool_name": "Write",
            "cwd": cwd,
            "tool_input": {
                "file_path": path if os.path.isabs(path) else os.path.join(cwd, path),
                "content": patch_written_content(command),
            },
        }
        for path in targets
    ]


def normalize_spawn(data):
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    message = tool_input.get("message")
    if not isinstance(message, str):
        return None
    normalized_input = {"prompt": message}
    if "model" in tool_input:
        normalized_input["model"] = tool_input["model"]
    return [{
        "tool_name": "Agent",
        "cwd": str(data.get("cwd") or os.getcwd()),
        "tool_input": normalized_input,
    }]


def normalized_payloads(data, gate):
    tool_name = data.get("tool_name")
    if gate in ("memory", "skill-landing", "tmp-artifact", "done-claim"):
        if tool_name != "apply_patch":
            return []
        return normalize_apply_patch(data)
    if gate in ("decomp", "in-session-dispatch"):
        if tool_name != "spawn_agent":
            return []
        return normalize_spawn(data)
    if gate == "dispatch-return-result-first":
        return [data]
    return None


def gate_decision(output):
    """Read the Claude legacy-gate result without accepting malformed output."""
    if not output.strip():
        return "allow", ""
    try:
        doc = json.loads(output)
    except Exception:
        return None, "legacy gate emitted malformed JSON"
    if not isinstance(doc, dict):
        return None, "legacy gate emitted a non-object JSON value"
    hook = doc.get("hookSpecificOutput")
    if not isinstance(hook, dict):
        return None, "legacy gate emitted an unknown JSON shape"
    decision = hook.get("permissionDecision")
    reason = hook.get("permissionDecisionReason", "")
    if decision == "deny" and isinstance(reason, str):
        return "deny", reason
    if decision in (None, "allow"):
        return "allow", ""
    return None, "legacy gate emitted an invalid decision"


def run_gate(gate, payload):
    path = os.path.join(HERE, ALL_GATES[gate])
    if not os.path.isfile(path):
        # A gate named in GATES whose script is absent is a CONFIGURATION
        # defect, not a policy answer, and it deserves its own sentence. It
        # still DENIES -- a write gate that cannot be consulted must not let
        # the write through -- but the refusal names the path so the operator
        # can fix the install, instead of a bare "legacy gate exited
        # non-zero: 2" repeating until the dispatch times out (DGN-1560:
        # dsp-20260918-085228-2803 burned 47 minutes on exactly that).
        return None, (
            "gate script is MISSING: %s -- nothing can answer for this gate, "
            "so the write stays denied. Install that script in this tree, or "
            "drop `--gate %s` from the codex hook wiring." % (path, gate)
        )
    try:
        result = subprocess.run(
            [sys.executable, path], input=json.dumps(payload), text=True,
            capture_output=True, timeout=15,
        )
    except Exception as exc:
        return None, "legacy gate %s could not run: %s" % (path, exc)
    if result.returncode != 0:
        return None, "legacy gate %s exited non-zero: %s" % (path, result.returncode)
    return gate_decision(result.stdout)


def run_stop(data):
    """Run the existing declaration/landing exit check in Codex Stop form."""
    path = os.path.join(HERE, STOP_SCRIPT)
    if not os.path.isfile(path):
        # A Stop checker is a completeness check, not a write gate. With the
        # ledger script absent nothing can ever have been declared to it, so
        # there is nothing to carry -- blocking would only loop the worker
        # until the dispatch timeout without protecting anything.
        # codex-hook-launcher.py already makes this exact call one layer up
        # (estate instance with no gate battery: PreToolUse denies, --stop
        # allows with a note); this mirrors it rather than inventing a second
        # policy. A ledger that RUNS and fails still blocks, below.
        note("Stop checker is MISSING: %s -- allowing Stop. Install that "
             "script in this tree, or drop `--stop` from the codex hook "
             "wiring." % path)
        return
    legacy = dict(data)
    legacy["hook_event_name"] = "Stop"
    try:
        result = subprocess.run(
            [sys.executable, path, "check-stop"], input=json.dumps(legacy),
            text=True, capture_output=True, timeout=15,
        )
    except Exception as exc:
        block("Stop gate %s could not run: %s" % (path, exc))
        return
    if result.returncode != 0:
        block("Stop gate %s exited non-zero: %s" % (path, result.returncode))
        return
    if not result.stdout.strip():
        return
    reason = stop_reason(result.stdout)
    if reason is not None:
        block(reason)
        return
    block("Stop gate emitted malformed output")


def stop_reason(output):
    """Return a valid legacy Stop block reason, else None."""
    try:
        doc = json.loads(output)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    reason = doc.get("reason")
    if doc.get("decision") == "block" and isinstance(reason, str):
        return reason
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", choices=sorted(ALL_GATES))
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.gate in OPTIONAL_GATES and not os.path.isfile(
            os.path.join(HERE, OPTIONAL_GATES[args.gate])):
        note("--gate %s is not enforced in this tree: %s. Passing through "
             "with no decision." % (args.gate, OPTIONAL_GATES_REASON[args.gate]))
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        deny("Codex hook payload is malformed; refusing because no gate can inspect it")
        return
    if not isinstance(data, dict):
        deny("Codex hook payload is not an object; refusing because no gate can inspect it")
        return
    if args.stop:
        run_stop(data)
        return
    payloads = normalized_payloads(data, args.gate)
    if payloads is None:
        deny("Codex hook payload cannot be normalized safely")
        return
    if not payloads:
        return
    for payload in payloads:
        decision, reason = run_gate(args.gate, payload)
        if decision is None:
            deny("%s gate fail-closed: %s" % (args.gate, reason))
            return
        if decision == "deny":
            deny(reason)
            return


if __name__ == "__main__":
    main()
