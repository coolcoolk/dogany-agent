#!/usr/bin/env python3
# test-lane: fast
"""Locks for the DGN-1632 PostToolUse continuation seat in
routines/onboarding-gate-stop.py.

Contract under test:
  * domain instance + an Edit that fills the LAST identity field + a pack
    continuation (config/packs/<id>.post_mint_next) -> PostToolUse
    additionalContext carrying the continuation verbatim + the reply shape
    rule (recap -> transition -> first question, no capability list).
  * the same edit while the ONBOARDING_PENDING marker is still in the file
    still yields the continuation (the selector is marker-independent).
  * not-last edit (a field still carries the placeholder) -> silent.
  * no continuation -> "no continuation" normal-close context.
  * later edit (fields already complete before it) -> silent.
  * Write with the harness originalFile -> transition detected.
  * main class / migration key / another file / non-interactive -> silent.
  * domain instance with no declared post_mint_next slot -> one SLOT_ABSENT
    line in the gate log naming instance + packs (no owner output).
  * template settings.json wires the gate on PostToolUse Edit|Write|MultiEdit.
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))
GATE = os.path.join(ROOT, "onboarding-gate-stop.py")
SETTINGS = os.path.join(ROOT, "..", ".claude", "settings.json")

MARKER = "<!-- ONBOARDING_PENDING -->"
PACK_CTX = "[pack_health] run routines/pack-health-next.sh; entry skill health-onboard"

_failures = []


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def identity(humor="30%", tone="warm", marker=False):
    lines = [MARKER] if marker else []
    lines += [
        "# AGENT", "## Identity", "- Name: **Warg**", "- Emoji: \U0001F43A",
        "## Role", "- Primary focus: health and fitness coach",
        "## Relationship", "- Call the user **boss**.",
        "- Tone: %s" % tone, "- Humor: %s" % humor,
    ]
    return "\n".join(lines) + "\n"


def mk(root, klass="domain", pack=True, conf_extra=""):
    write(os.path.join(root, ".instance.conf"),
          "DOGANY_AGENT_CLASS=%s\n" % klass)
    write(os.path.join(root, "config", "agent.conf"),
          "AGENT_LANG=en\nKIT=none\n" + conf_extra)
    if pack:
        write(os.path.join(root, "config", "packs",
                           "pack_health.post_mint_next"), PACK_CTX + "\n")


def run(root, tool_name, tool_input, tool_response=None, interactive=True):
    payload = {"hook_event_name": "PostToolUse", "cwd": root,
               "tool_name": tool_name, "tool_input": tool_input}
    if tool_response is not None:
        payload["tool_response"] = tool_response
    env = dict(os.environ)
    for k in ("ONBOARDING_FILE", "PROJECT_ROOT", "DOGANY_SESSION",
              "AGENT_CONF_FILE", "INSTANCE_CONF_FILE", "LIFEKIT_FILE"):
        env.pop(k, None)
    env["ONBOARDING_FILE"] = root
    if interactive:
        env["PROJECT_ROOT"] = root
    proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=15, env=env)
    out = proc.stdout.strip()
    if proc.returncode != 0:
        _failures.append("exit %d: %s" % (proc.returncode, proc.stderr))
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]


def last_field_edit(root, marker=False):
    """Apply the Humor edit on disk (PostToolUse runs after the tool) and
    return the Edit tool_input."""
    ident = os.path.join(root, "identity", "hot.custom.agent.md")
    write(ident, identity(humor="30%", marker=marker))
    return {"file_path": ident, "old_string": "- Humor: (set at onboarding)",
            "new_string": "- Humor: 30%"}


def test_last_field_with_pack_continuation():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        out = run(root, "Edit", last_field_edit(root))
        check("pack: fires", out is not None)
        if out:
            ctx = out["additionalContext"]
            check("pack: event", out["hookEventName"] == "PostToolUse")
            check("pack: verbatim continuation", PACK_CTX in ctx)
            check("pack: exists", "A continuation (domain onboarding) EXISTS" in ctx)
            check("pack: itemized echo", "ITEMIZED list" in ctx)
            check("pack: ends on question", "MUST END with that" in ctx)
            check("pack: no announcement end",
                  "never end on an announcement" in ctx)
            check("pack: no filler", "no vague care-taking promise" in ctx)
            check("pack: pack vocabulary verbatim",
                  "never paraphrase the pack's vocabulary" in ctx)
            check("pack: transition", "transition sentence" in ctx)
            check("pack: first question", "continuation's first question" in ctx)
            check("pack: no capability list", "NO capability list" in ctx)
            check("pack: no briefing-time step (dec-219)",
                  "briefing" not in ctx.lower())
        with open(os.path.join(root, ".telegram_bot", "logs",
                               "onboarding-gate.log")) as f:
            check("pack: logged", "CONTINUATION" in f.read())


def test_marker_still_present():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        out = run(root, "Edit", last_field_edit(root, marker=True))
        check("marker: continuation despite marker",
              out is not None and PACK_CTX in out["additionalContext"])


def test_not_last_edit_silent():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        ident = os.path.join(root, "identity", "hot.custom.agent.md")
        write(ident, identity(humor="(set at onboarding)", tone="warm"))
        out = run(root, "Edit", {"file_path": ident,
                                 "old_string": "- Tone: (set at onboarding)",
                                 "new_string": "- Tone: warm"})
        check("not-last: silent", out is None, repr(out))


def test_no_continuation_normal_close():
    with tempfile.TemporaryDirectory() as root:
        mk(root, pack=False)
        out = run(root, "Edit", last_field_edit(root))
        check("none: fires", out is not None)
        if out:
            ctx = out["additionalContext"]
            check("none: says absent", "NO continuation" in ctx)
            check("none: normal close", "normal close" in ctx)
            check("none: no fence", "continuation context ---" not in ctx)
            check("none: no briefing-time step (dec-219)",
                  "briefing" not in ctx.lower())


def test_later_edit_silent():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        ident = os.path.join(root, "identity", "hot.custom.agent.md")
        write(ident, identity())
        out = run(root, "Edit", {"file_path": ident,
                                 "old_string": "- Tone: calm",
                                 "new_string": "- Tone: warm"})
        check("later: silent", out is None, repr(out))


def test_write_with_original_file():
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        ident = os.path.join(root, "identity", "hot.custom.agent.md")
        write(ident, identity())
        before = identity(humor="(set at onboarding)", marker=True)
        out = run(root, "Write", {"file_path": ident, "content": identity()},
                  tool_response={"type": "update", "filePath": ident,
                                 "originalFile": before})
        check("write: fires", out is not None and PACK_CTX in out["additionalContext"])
        out = run(root, "Write", {"file_path": ident, "content": identity()},
                  tool_response={"type": "update", "filePath": ident,
                                 "originalFile": identity()})
        check("write: already filled -> silent", out is None)


def test_scope_guards_silent():
    with tempfile.TemporaryDirectory() as root:
        mk(root, klass="main")
        check("main: silent", run(root, "Edit", last_field_edit(root)) is None)
    with tempfile.TemporaryDirectory() as root:
        mk(root, conf_extra="MIGRATION_PEER=ag\n")
        check("migration: silent", run(root, "Edit", last_field_edit(root)) is None)
    with tempfile.TemporaryDirectory() as root:
        mk(root, conf_extra="HANDOFF_PEER_AG=ag\n")
        check("legacy migration: silent",
              run(root, "Edit", last_field_edit(root)) is None)
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        last_field_edit(root)
        other = os.path.join(root, "identity", "hot.custom.owner.md")
        write(other, "# USER\n")
        check("other file: silent", run(root, "Edit", {
            "file_path": other, "old_string": "x", "new_string": "# USER"}) is None)
    with tempfile.TemporaryDirectory() as root:
        mk(root)
        check("non-interactive: silent", run(
            root, "Edit", last_field_edit(root), interactive=False) is None)


def gate_log(root):
    try:
        with open(os.path.join(root, ".telegram_bot", "logs",
                               "onboarding-gate.log")) as f:
            return f.read()
    except OSError:
        return ""


def test_slot_absent_logged():
    with tempfile.TemporaryDirectory() as root:
        mk(root, pack=False)
        write(os.path.join(root, "config", "packs", "pack_health.requires_framework"),
              ">=1.0.0\n")
        out = run(root, "Edit", last_field_edit(root))
        log = gate_log(root)
        check("slot-absent: line logged", "SLOT_ABSENT" in log, log)
        check("slot-absent: names pack", "pack_health" in log, log)
        check("slot-absent: names instance", os.path.basename(root) in log, log)
        check("slot-absent: exactly one line", log.count("SLOT_ABSENT") == 1, log)
        check("slot-absent: no owner output beyond normal close",
              out is not None and "SLOT_ABSENT" not in out["additionalContext"])
    with tempfile.TemporaryDirectory() as root:
        mk(root)  # slot declared -> no SLOT_ABSENT line
        run(root, "Edit", last_field_edit(root))
        check("slot-present: not logged", "SLOT_ABSENT" not in gate_log(root))
    with tempfile.TemporaryDirectory() as root:
        mk(root, pack=False, conf_extra="POST_MINT_NEXT=stop\n")
        run(root, "Edit", last_field_edit(root))
        check("slot pinned in agent.conf: not logged",
              "SLOT_ABSENT" not in gate_log(root))
    with tempfile.TemporaryDirectory() as root:
        mk(root, klass="main", pack=False)
        run(root, "Edit", last_field_edit(root))
        check("main: not logged", "SLOT_ABSENT" not in gate_log(root))


def test_settings_wiring():
    with open(SETTINGS, encoding="utf-8") as f:
        hooks = json.load(f)["hooks"]["PostToolUse"]
    wired = [g.get("matcher", "") for g in hooks
             if any("onboarding-gate-stop.py" in h.get("command", "")
                    for h in g.get("hooks", []))]
    check("settings: wired", any(set(("Edit", "Write", "MultiEdit"))
                                 <= set(m.split("|")) for m in wired), repr(wired))


def main():
    test_last_field_with_pack_continuation()
    test_marker_still_present()
    test_not_last_edit_silent()
    test_no_continuation_normal_close()
    test_later_edit_silent()
    test_write_with_original_file()
    test_scope_guards_silent()
    test_slot_absent_logged()
    test_settings_wiring()
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-dgn1632-onboarding-continuation (9 tests)")


if __name__ == "__main__":
    main()
