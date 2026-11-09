#!/usr/bin/env python3
# wires-rules: cc-memory-write-guard.py, skill-landing-gate.py, onboarding-owner-gate.py, write-paths-d4.py
"""PreToolUse write-path gate: every path rule for Edit/Write/MultiEdit/Bash
in ONE process (v2.7 merge #9, dec-278: K04 + K05 + K06).

WHY.  Three separate PreToolUse entries (cc-memory-write-guard.py,
skill-landing-gate.py, onboarding-owner-gate.py) each spawned a Python
process on every Edit/Write -- 3 processes per edit that all re-parse the
same stdin payload (refactor-gates.md F1/F8).  This hook is the one entry;
the rules are rows of WRITE_PATH_RULES below.

BEHAVIOUR-NEUTRAL BY CONSTRUCTION.  A row names the rule script whose
main() is the rule body, unchanged.  Each rule runs in-process against the
same raw payload, with its own stdin/stdout, exactly as if the harness had
spawned it.  The rule scripts stay runnable on their own (Codex adapter,
skills/dogany-skill-creator/steps.sh, instance settings.local.json).

COMBINING (what the harness did with three parallel hooks):
  - every rule runs; none short-circuits another (a deny from one rule
    never hides another rule's audit-log side effect)
  - zero outputs -> print nothing (allow)
  - one output   -> printed verbatim (byte-identical to the old hook)
  - two or more  -> one JSON: deny if any rule denied (reasons joined in
    row order), every systemMessage (gate-switch off-notices) kept

Fail-open per rule: a rule that raises is skipped; a malformed rule output
is dropped.  One exception (v2.7 D3/D4 grill F23): a rule that already
printed a deny before it raised still denies -- a crash after the decision
never turns a fail-closed row open.  Always exits 0 (the decision field
signals deny).
"""
import importlib.util
import io
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# (slug, tools the rule judges, rule script).  Order = output order.
WRITE_PATH_RULES = (
    ("memory", ("Edit", "Write", "MultiEdit"),
     "cc-memory-write-guard.py"),
    ("skill-landing", ("Edit", "Write", "MultiEdit"),
     "skill-landing-gate.py"),
    ("onboarding-owner", ("Edit", "Write", "MultiEdit", "Bash"),
     "onboarding-owner-gate.py"),
    # v2.7 D4 (docs/v27-D3-D4-spec.md section 4): framework / baseline /
    # owner-data / layout.  Wired in DETECT (WRITE_PATHS_D4_MODE=detect on
    # the settings line): it logs, it never denies until block.
    ("d4", ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"),
     "write-paths-d4.py"),
)


def _load(script):
    path = os.path.join(SCRIPT_DIR, script)
    name = "write_paths_" + script[:-3].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_rule(script, raw):
    """Run one rule script's main() on `raw` stdin; return its stdout."""
    saved = (sys.stdin, sys.stdout, sys.argv)
    buf = io.StringIO()
    try:
        sys.stdin = io.StringIO(raw)
        sys.stdout = buf
        sys.argv = [os.path.join(SCRIPT_DIR, script)]
        mod = _load(script)
        if mod is not None:
            mod.main()
    except SystemExit:
        pass
    except BaseException:
        # fail open: a broken rule never blocks the tool call -- unless it
        # already printed its deny (F23: the decision was made)
        out = buf.getvalue()
        return out if '"permissionDecision": "deny"' in out else ""
    finally:
        sys.stdin, sys.stdout, sys.argv = saved
    return buf.getvalue()


def combine(outputs):
    """Fold rule outputs (stripped stdout strings) into one hook output."""
    if len(outputs) == 1:
        return outputs[0]
    reasons, messages = [], []
    for out in outputs:
        try:
            obj = json.loads(out)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        hso = obj.get("hookSpecificOutput") or {}
        if hso.get("permissionDecision") == "deny":
            reasons.append(hso.get("permissionDecisionReason", ""))
        if obj.get("systemMessage"):
            messages.append(obj["systemMessage"])
    merged = {}
    if messages:
        merged["systemMessage"] = "\n".join(messages)
    if reasons:
        merged["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n\n".join(reasons),
        }
    return json.dumps(merged, ensure_ascii=False) if merged else ""


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)
    try:
        tool_name = json.loads(raw).get("tool_name", "")
    except Exception:
        tool_name = None  # each rule applies its own bad-input contract
    outputs = []
    for _slug, tools, script in WRITE_PATH_RULES:
        if tool_name is not None and tool_name not in tools:
            continue
        out = run_rule(script, raw).strip()
        if out:
            outputs.append(out)
    if outputs:
        text = combine(outputs)
        if text:
            print(text, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
