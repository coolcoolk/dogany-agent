#!/usr/bin/env python3
# test-lane: fast
"""DGN-1446: stub-aware PostToolUse Skill notice (O2) + Stop canary (O1)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE = os.path.join(ROOT, "skill-feedback-gate.py")
STOP = os.path.join(ROOT, "output-gate-stop.py")


def run(script, args, payload, env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, script] + args,
                          input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=20)


class StubNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {"DOGANY_HOME": self.tmp, "DOGANY_SLUG": "t", "HOME": self.tmp}

    def rec(self, resp):
        return run(GATE, ["--record"], {
            "hook_event_name": "PostToolUse", "tool_name": "Skill",
            "tool_input": {"skill": "agent-browser"}, "tool_response": resp,
            "cwd": self.tmp, "session_id": "s1"}, self.env)

    def test_stub_emits_context(self):
        r = self.rec("This file is a discovery stub, not the usage guide.")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        h = out["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PostToolUse")
        self.assertIn("Run the command", h["additionalContext"])

    def test_stub_in_dict_response(self):
        r = self.rec({"content": [{"text": "Not The Usage Guide"}]})
        self.assertIn("additionalContext", r.stdout)

    def test_normal_body_silent(self):
        r = self.rec("# Real skill\nDo the steps.")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


class StubNoticeRealShape(unittest.TestCase):
    """The real Skill tool_response is metadata only; the body is on disk."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {"DOGANY_HOME": self.tmp, "DOGANY_SLUG": "t", "HOME": self.tmp}

    def rec(self, body):
        d = os.path.join(self.tmp, ".claude", "skills", "agent-browser")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write(body)
        return run(GATE, ["--record"], {
            "hook_event_name": "PostToolUse", "tool_name": "Skill",
            "tool_input": {"skill": "agent-browser"},
            "tool_response": {"success": True, "commandName": "agent-browser"},
            "cwd": self.tmp, "session_id": "s1"}, self.env)

    def test_metadata_response_stub_on_disk(self):
        r = self.rec("This file is a discovery stub, not the usage guide.")
        self.assertIn("additionalContext", r.stdout)

    def test_metadata_response_normal_on_disk(self):
        r = self.rec("# Real skill\nDo the steps.")
        self.assertEqual(r.stdout.strip(), "")


class StopCanary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "canary.log")
        self.tp = os.path.join(self.tmp, "t.jsonl")

    def go(self, entries):
        with open(self.tp, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return run(STOP, [], {"hook_event_name": "Stop", "cwd": self.tmp,
                              "transcript_path": self.tp},
                   {"DOGANY_OUTPUT_GATE_LOG": self.log})

    @staticmethod
    def user(t):
        return {"type": "user", "message": {"content": t}}

    @staticmethod
    def tool(name, inp):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}}

    def logged(self):
        try:
            return open(self.log).read()
        except OSError:
            return ""

    def test_skill_last_logs_never_blocks(self):
        r = self.go([self.user("open it"),
                     self.tool("Skill", {"skill": "agent-browser"})])
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("block", r.stdout)
        self.assertIn("SKILL_LAST_TOOL  skill=agent-browser", self.logged())

    def test_later_tool_no_log(self):
        r = self.go([self.user("open it"),
                     self.tool("Skill", {"skill": "agent-browser"}),
                     self.tool("Bash", {"command": "agent-browser open x"})])
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("SKILL_LAST_TOOL", self.logged())


if __name__ == "__main__":
    unittest.main()
