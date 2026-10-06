#!/usr/bin/env python3
"""PreToolUse owner-gate on onboarding identity writes (DGN-1606).

WHY THIS EXISTS (measured 2026-09-20, dogany-health): a freshly restarted
pending instance received two session-inbox injections (outbound-record +
restart-verify), the SessionStart onboarding signal fired into that turn,
and the model COMPLETED onboarding alone -- it filled all five identity
fields (name/emoji/address/tone/humor) with values it invented and deleted
the ONBOARDING_PENDING marker, with ZERO owner utterances in the whole
session.  The rule it broke ("identity fields are written on explicit user
request", rules/hot.framework.md Edit rights) existed only as prose.  This
hook is that rule's forcing point, wired at the execution decision-point it
governs: the WRITE to the identity file.

ENFORCEMENT MODEL (PreToolUse deny; cc-memory-write-guard.py I/O contract):
  While the identity file still carries <!-- ONBOARDING_PENDING -->, a
  tool call that writes/targets the identity file is DENIED when the
  current turn positively had no owner in it:
    - the session is non-interactive (cron / detached / headless -- an
      owner cannot be speaking there), or
    - the turn's boundary message is machine-opened: it starts with the
      harness injected-turn mark (bridge/sdk_bridge.py INJECTED_TURN_MARK,
      stamped by inject_background_turn) or a belt spool prefix.
  Everything else passes: marker gone (normal life), non-identity targets,
  owner-opened turns (a real first-contact onboarding edits freely).

WHAT COUNTS AS TARGETING THE IDENTITY FILE:
  - Edit/Write/MultiEdit whose file_path resolves to the identity file.
  - Bash whose command string mentions the identity file's basename
    (measured incident shape: the block deletion ran via `sed -i`).
    Coarse on purpose; the deny reason points at the Read tool, which
    stays available for the onboarding-gate-stop touch requirement.

HONEST LIMITS (stated per DGN-1194 discipline, not hidden):
  - A model determined to dodge can write the file through an unmentioned
    path (constructed filename, python inline).  Same residual as every
    gate in the house; mitigations are the audit log + visible conf diffs
    (gate-switch.py WHO FLIPS THE SWITCH note).
  - Boundary classification needs a readable transcript; when it cannot be
    POSITIVELY established that the turn is machine-opened, the gate
    allows (an interactive owner-present onboarding must never wedge --
    tonight's-mint constraint).

Scope guards (all fail toward ALLOW -- a guard malfunction must never
block the tool pipeline, token-gate.py contract):
  - No identity file resolved / file missing -> allow (mint repair
    territory; nothing to protect).
  - Marker absent -> allow (steady state, one file read).
  - Kill switch: routines/gate-switch.py slug `onboarding-owner`
    (GATE_ONBOARDING_OWNER / master GATE_ALL); off -> non-blocking
    systemMessage notice instead of the deny, event audit-logged.
  - Any error -> exit 0 silently.

Observability: denies append to <BOT_DATA_DIR>/logs/onboarding-gate.log
(the stop-gate's log, one surface for the whole onboarding machinery):
OWNER_DENY / OWNER_OFF_SUPPRESSED.

stdin(JSON): {tool_name, tool_input, cwd, transcript_path, ...}
stdout: hookSpecificOutput/permissionDecision deny JSON, or nothing.
Always exits 0 (the decision field signals deny, not the exit code).
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MARKER = "<!-- ONBOARDING_PENDING -->"
_GATE_SLUG = "onboarding-owner"
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit")

# Model-facing deny reason (English per RULES Code=English/ASCII).
DENY_REASON = (
    "Onboarding owner-gate (DGN-1606): identity-file writes are denied in "
    "this turn. The identity file still carries the ONBOARDING_PENDING "
    "marker and the current turn was opened by a machine-injected message "
    "or runs in a headless session -- there is no owner utterance in it. "
    "The five identity fields (name / emoji / form of address / tone / "
    "humor) are the OWNER's answers to the onboarding questions; never "
    "infer, borrow, or fill them yourself, and never delete the onboarding "
    "block or the marker outside a real owner conversation. Leave the "
    "identity file untouched and continue this turn's actual task; "
    "onboarding runs when the owner speaks (reading the identity file via "
    "the Read tool remains allowed)."
)


def _load_sibling(filename, modname):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            modname, os.path.join(SCRIPT_DIR, filename))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def targets_identity(tool_name, tool_input, cwd, identity_path):
    """True iff this tool call touches the identity file.  Edit-family by
    resolved-path equality; Bash by basename mention in the command."""
    base = os.path.basename(identity_path)
    if tool_name in _WRITE_TOOLS:
        fp = tool_input.get("file_path")
        if not isinstance(fp, str) or not fp.strip():
            return False
        fp = fp.strip()
        if not os.path.isabs(fp):
            fp = os.path.join(cwd, fp)
        try:
            if os.path.realpath(fp) == os.path.realpath(identity_path):
                return True
        except Exception:
            pass
        return os.path.basename(fp) == base
    if tool_name == "Bash":
        cmd = tool_input.get("command")
        return isinstance(cmd, str) and base in cmd
    return False


def emit_deny(cwd):
    """Print the deny (or, switch off, the non-blocking off-notice).
    Returns True when the deny actually went out."""
    gs = _load_sibling("gate-switch.py", "gate_switch_for_owner_gate")
    if gs is not None:
        try:
            if not gs.is_enabled(_GATE_SLUG, cwd):
                print(json.dumps(gs.off_notice(_GATE_SLUG, DENY_REASON, cwd),
                                 ensure_ascii=False), flush=True)
                return False
        except Exception:
            pass  # a broken switch never disarms the gate
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }, ensure_ascii=False), flush=True)
    return True


def main():
    try:
        try:
            data = json.load(sys.stdin)
        except Exception:
            sys.exit(0)
        if not isinstance(data, dict):
            sys.exit(0)
        tool_name = data.get("tool_name", "")
        if tool_name not in _WRITE_TOOLS + ("Bash",):
            sys.exit(0)
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            sys.exit(0)
        cwd = str(data.get("cwd") or os.getcwd())

        # Identity resolution rides onboarding-check.py (ONBOARDING_FILE env
        # honored, dual-accept chain) -- the single resolver, never forked.
        oc = _load_sibling("onboarding-check.py", "onboarding_check_for_owner")
        if oc is None:
            sys.exit(0)
        try:
            identity_path, _collision = oc.resolve_target(data)
        except Exception:
            sys.exit(0)
        if not identity_path or not os.path.isfile(identity_path):
            sys.exit(0)

        if not targets_identity(tool_name, tool_input, cwd, identity_path):
            sys.exit(0)

        try:
            with open(identity_path, encoding="utf-8") as f:
                pending = MARKER in f.read()
        except Exception:
            sys.exit(0)
        if not pending:
            sys.exit(0)

        # Owner-presence classification rides the stop-gate module (single
        # source of the session predicate, the boundary parser, and the
        # injected-turn mark).  Loader failure -> allow (fail-open).
        og = _load_sibling("onboarding-gate-stop.py", "onboarding_stop_for_owner")
        if og is None:
            sys.exit(0)

        if og.session_is_interactive():
            # Owner CAN be here -- deny only on positive machine-opened
            # identification; any doubt allows (never wedge a real mint).
            if not og.boundary_is_injected(data.get("transcript_path") or ""):
                sys.exit(0)
            detail = ("machine-opened turn (injected boundary) tried to "
                      "write %s while pending" % identity_path)
        else:
            detail = ("non-interactive session tried to write %s while "
                      "pending" % identity_path)

        denied = emit_deny(cwd)
        try:
            og._log(cwd, "OWNER_DENY" if denied else "OWNER_OFF_SUPPRESSED",
                    "%s (tool=%s)" % (detail, tool_name))
        except Exception:
            pass
        sys.exit(0)

    except SystemExit:
        raise
    except BaseException:
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)


if __name__ == "__main__":
    main()
