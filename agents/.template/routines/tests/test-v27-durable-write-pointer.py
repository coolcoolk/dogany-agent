#!/usr/bin/env python3
# test-lane: fast
"""v2.7 D6: the durable-write pointer and the section it points to.

Locks:
  (a) owner session, Write/Edit/MultiEdit under identity/, skills/,
      .claude/skills/, files/, memories/ or worklog/ (the ticket surface):
      exactly the one conditional line, naming playbooks/layout.md -- Routing;
  (b) any other path, a non-write tool, a subagent, a non-owner session,
      garbage stdin: no output, exit 0 (fail open);
  (c) the template settings.json wires the hook once, PreToolUse, on the
      write tools;
  (d) one home: each routing rule (HF17 files/ semantics, HF21 route durable
      knowledge, HF22 work items on the ticket surface) lives in the layout
      section; the only other copy allowed is the hot line still listed in
      PENDING_HOT_RELEASE (dec-198/AC9: hot lines go only after this fixture
      is green; delete the hot line and its entry together);
  (e) the pointer and the section name "the user", never a form of address.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTINES = HERE.parent
TEMPLATE = ROUTINES.parent
HOOK = ROUTINES / "durable-write-pointer.py"
DOC = TEMPLATE / "playbooks" / "layout.md"
HOT = TEMPLATE / "rules" / "hot.framework.md"
SETTINGS = TEMPLATE / ".claude" / "settings.json"
SECTION = "## Routing: where a durable fact or work item goes"
POINTER = ("If you are about to store a durable fact or work item, read "
           "playbooks/layout.md -- Routing first; otherwise ignore.")

# Row -> fragment of the rule text that moved into the layout section.
MOVED = {
    "HF17": "files/: inbox(owner-sent, not yet handled), docs(filed documents; "
            "routines/lib/filing.py is the only writer of docs/ and files/INDEX.md), "
            "outbox(send), tmp(scratch, daily-clean via cleanup routine), "
            "_archive(backups).",
    "HF21": "Route durable knowledge to its home: user fact -> "
            "identity/hot.custom.owner.md",
    "HF22": "Work-items (backlog, parked, tasks, pending decisions) belong to "
            "the ticket surface",
}
# Hot lines this device lets go once the fixture is green (not deleted yet).
# HF17/HF21/HF22 left hot in DGN-1967 pass 2 (dec-362).
PENDING_HOT_RELEASE = set()


def _run(payload, sessions_file):
    env = dict(os.environ)
    env["DOGANY_SESSIONS_FILE"] = sessions_file
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=raw, env=env,
                          capture_output=True, text=True, timeout=30)


def _flat(text):
    return " ".join(text.split())


class PointerHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v27d6-")
        self.sessions = os.path.join(self.tmp, "sessions.json")
        with open(self.sessions, "w") as fh:
            json.dump({"chat:1": {"session_id": "sess-owner"}}, fh)

    def _payload(self, path, sess="sess-owner", tool="Write", **extra):
        d = {"hook_event_name": "PreToolUse", "tool_name": tool,
             "tool_input": {"file_path": path, "content": "x"},
             "cwd": str(TEMPLATE),
             "transcript_path": "/x/proj/%s.jsonl" % sess}
        d.update(extra)
        return d

    def test_a_durable_homes_get_exactly_the_pointer(self):
        paths = [str(TEMPLATE / "identity" / "hot.custom.owner.md"),
                 str(TEMPLATE / "memories" / "note.md"),
                 str(TEMPLATE / "files" / "outbox" / "plan.md"),
                 str(TEMPLATE / ".claude" / "skills" / "x" / "SKILL.md"),
                 "skills/x/SKILL.md",
                 "worklog/T-1.md"]
        for path in paths:
            for tool in ("Write", "Edit", "MultiEdit"):
                p = _run(self._payload(path, tool=tool), self.sessions)
                self.assertEqual(0, p.returncode, p.stderr)
                hso = json.loads(p.stdout)["hookSpecificOutput"]
                self.assertEqual("PreToolUse", hso["hookEventName"])
                self.assertEqual(POINTER, hso["additionalContext"], path)
                self.assertNotIn("permissionDecision", hso)
        self.assertIn(SECTION, DOC.read_text(encoding="utf-8"))

    def test_b_other_paths_are_silent(self):
        for path in (str(TEMPLATE / "routines" / "x.py"),
                     str(TEMPLATE / "rules" / "hot.custom.md"),
                     "/elsewhere/files/x.md", "../files/x.md", ""):
            p = _run(self._payload(path), self.sessions)
            self.assertEqual((0, ""), (p.returncode, p.stdout), path)

    def test_b_non_write_tool_is_silent(self):
        d = self._payload("memories/x.md", tool="Read")
        self.assertEqual((0, ""), (lambda p: (p.returncode, p.stdout))(
            _run(d, self.sessions)))

    def test_b_subagent_and_non_owner_are_silent(self):
        for d in (self._payload("memories/x.md", agent_id="a1"),
                  self._payload("memories/x.md", sess="sess-cron")):
            p = _run(d, self.sessions)
            self.assertEqual((0, ""), (p.returncode, p.stdout))

    def test_b_garbage_is_silent(self):
        for raw in ("", "not json", "[1, 2]",
                    json.dumps({"tool_name": "Write", "tool_input": "x"})):
            p = _run(raw, self.sessions)
            self.assertEqual((0, ""), (p.returncode, p.stdout), raw)


class Wiring(unittest.TestCase):
    def test_c_settings_wire_write_tools_once(self):
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
        rows = [m for ev in hooks for m in hooks[ev]
                if any("routines/durable-write-pointer.py" in h["command"]
                       for h in m["hooks"])]
        self.assertEqual(1, len(rows), rows)
        self.assertIn(rows[0], hooks["PreToolUse"])
        self.assertEqual({"Write", "Edit", "MultiEdit"},
                         set(rows[0]["matcher"].split("|")))


class OneHome(unittest.TestCase):
    def setUp(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn(SECTION, doc)
        self.section = doc.split(SECTION, 1)[1].split("\n## ", 1)[0]
        self.hot = HOT.read_text(encoding="utf-8")

    def test_d_each_rule_lives_in_the_section(self):
        for row, frag in MOVED.items():
            self.assertIn(_flat(frag), _flat(self.section), row)
            in_hot = _flat(frag) in _flat(self.hot)
            if row in PENDING_HOT_RELEASE:
                self.assertTrue(in_hot, "%s left hot: drop it from "
                                "PENDING_HOT_RELEASE" % row)
            else:
                self.assertFalse(in_hot, "duplicate left hot: " + row)

    def test_e_wording_names_the_user(self):
        for text in (POINTER, self.section):
            self.assertTrue(text.isascii(), text)
            self.assertNotIn("\ud615\ub2d8", text)  # an owner form of address


if __name__ == "__main__":
    unittest.main()
