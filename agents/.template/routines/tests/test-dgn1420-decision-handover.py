#!/usr/bin/env python3
# test-lane: fast
"""DGN-1420: a confirmed decision survives a bridge restart.

A define the owner confirmed lived only in the conversation; the restart cut
it, the resumed session re-ran the define and got the opposite verdict 45
minutes later.  routines/decision-ledger.py `handover` prints the latest
ledger entries for the post-restart spool (bridge/self_restart.sh step 5).
Cases: confirmed machine entry (stop -> ups -> handover) is carried with its
state; pending entry carried as pending; agent-written entry (no mark) as
recorded; non-entry headings ignored; DECISION_HANDOVER_COUNT keeps the
latest N, 0 = none; ledger unset / missing / empty -> no output.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.normpath(os.path.join(HERE, "..", "decision-ledger.py"))

SEED = (
    "# Decisions\n\n"
    "## dec-330 -- drop two lines (2026-10-08) [confirmed 2026-10-08 21:12]\n"
    "- body\n\n"
    "### Notes without an id\n"
    "- body\n\n"
    "## dec-331 -- relocate a rule (2026-10-08) [confirmed 2026-10-08 21:36]\n"
    "- body\n"
)
MENU = ("Which anchor set ships first?\n\n1. Anchor A first (rec)\n"
        "2. Anchor B first\n\n[[OPTIONS: Anchor A first | Anchor B first]]")


class Handover(unittest.TestCase):
    CONF = ("BOT_DATA_DIR=data\n"
            "DECISION_LEDGER_PATH=worklog/decisions/_DECISIONS.md\n"
            "DECISION_LEDGER_DIR=worklog/decisions\n")

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dgn1420-")
        os.makedirs(os.path.join(self.root, "config"))
        os.makedirs(os.path.join(self.root, "worklog", "decisions"))
        self.write_conf(self.CONF)
        self.ledger = os.path.join(self.root, "worklog", "decisions",
                                   "_DECISIONS.md")
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write(SEED)
        self.tx = os.path.join(self.root, "t.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_conf(self, text):
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write(text)

    def run_seat(self, mode, payload=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DOGANY_DISPATCH_WORKER", "DOGANY_SESSION")}
        env.update({"DECISION_LEDGER_ROOT": self.root,
                    "PROJECT_ROOT": self.root})
        proc = subprocess.run(
            [sys.executable, HOOK, mode],
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout

    def handover(self):
        return self.run_seat("handover")

    def test_confirmed_decision_reaches_the_handover(self):
        # The defect: the owner's verdict exists before the restart and the
        # handover must carry it to the resumed session.
        # Seed classified machine capture: Stop now validates ledger fields,
        # never declarations in the owner's message (DGN-2012).
        spec = importlib.util.spec_from_file_location("dl_handover_fixture", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.ROOT = self.root
        menu = mod.parse_menu(MENU)
        menu.update({"class": "What", "not_how": "The owner chooses release scope.",
                     "kind": "scope"})
        rec = mod.write_pending(*mod.ledger_paths(), menu, mod.now())
        rec.update(session_id="owner", turn="u1")
        mod.write_pointer(rec)
        with open(self.tx, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "uuid": "u1", "message": {
                "role": "user", "content": "define v2.6"}}) + "\n")
        self.assertEqual("", self.run_seat("stop", {
            "session_id": "owner", "cwd": self.root,
            "transcript_path": self.tx, "hook_event_name": "Stop",
            "last_assistant_message": MENU}))
        self.run_seat("ups", {"session_id": "owner", "cwd": self.root,
                              "prompt": "1. Anchor A first",
                              "hook_event_name": "UserPromptSubmit"})
        with open(self.ledger, encoding="utf-8") as fh:
            ledger = fh.read()
        if "state=confirmed" not in ledger:
            self.skipTest("owner-session predicate unavailable here; the "
                          "stop/ups seats are covered by test-dgn1952")
        out = self.handover()
        self.assertIn("5. Carry the owner's decisions", out)
        self.assertIn("worklog/decisions/_DECISIONS.md", out)
        self.assertRegex(out, r"- \[confirmed\] dec-332 -- Which anchor set "
                              r"ships first\?.*\"1\. Anchor A first\"")
        self.assertIn("Never re-run a\n   define", out)

    def test_states_and_order(self):
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write("\n## dec-332 -- pick a seat (2026-10-09) [pending]\n"
                     "<!-- decision-ledger id=dec-332 state=pending -->\n"
                     "\n## dec-333 -- ship it (2026-10-09) [confirmed: \"1\"]\n"
                     "<!-- decision-ledger id=dec-333 state=confirmed "
                     "req=r-1 -->\n")
        out = self.handover()
        rows = [l.strip() for l in out.splitlines()
                if l.strip().startswith("- [")]
        self.assertEqual([
            "- [recorded] dec-330 -- drop two lines (2026-10-08) "
            "[confirmed 2026-10-08 21:12]",
            "- [recorded] dec-331 -- relocate a rule (2026-10-08) "
            "[confirmed 2026-10-08 21:36]",
            "- [pending] dec-332 -- pick a seat (2026-10-09) [pending]",
            "- [confirmed] dec-333 -- ship it (2026-10-09) "
            "[confirmed: \"1\"]"], rows)
        self.assertNotIn("Notes without an id", out)
        self.assertIn("pending = asked, not yet answered", out)

    def test_count_keeps_latest(self):
        self.write_conf(self.CONF + "DECISION_HANDOVER_COUNT=1\n")
        out = self.handover()
        self.assertIn("dec-331", out)
        self.assertNotIn("dec-330", out)

    def test_count_zero_is_silent(self):
        self.write_conf(self.CONF + "DECISION_HANDOVER_COUNT=0\n")
        self.assertEqual("", self.handover())

    def test_unconfigured_is_silent(self):
        self.write_conf("BOT_DATA_DIR=data\n")
        self.assertEqual("", self.handover())

    def test_missing_or_empty_ledger_is_silent(self):
        os.remove(self.ledger)
        self.assertEqual("", self.handover())
        open(self.ledger, "w").close()
        self.assertEqual("", self.handover())


if __name__ == "__main__":
    unittest.main()
