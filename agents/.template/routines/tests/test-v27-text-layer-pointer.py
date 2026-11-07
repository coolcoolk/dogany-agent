#!/usr/bin/env python3
# test-lane: fast
"""v2.7 D1: the text-layer pointer and the doc it points to.

Locks:
  (a) owner session: every PostToolUse emits exactly the one conditional
      line, naming rules/cold.framework.text.md, and the doc exists;
  (b) subagent / non-owner session / garbage stdin: no output, exit 0;
  (c) the template settings.json wires the hook on PostToolUse for every tool;
  (d) one home: each moved rule lives in the text doc and in NO always-loaded
      surface (hot.framework.md, the injected vendor half, the bridge
      grammar fragment) -- a moved rule left behind is a duplicate;
  (e) the container test settles who uses the text: read-only -> blockquote,
      paste/run -> code block;
  (f) the pointer and the doc name "the user", never a form of address.

promise: FDGN-578 -- the owner-language line also rides UserPromptSubmit,
  so a tool-free (injected) turn carries it; the settings wire it there.
promise: DGN-2423 -- the language line names the bridge's LOCALE (the
  system prompt's language) even when AGENT_LANG disagrees or is absent.
"""
import shutil
import atexit
import importlib.util
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
HOOK = ROUTINES / "text-layer-pointer.py"
DOC = TEMPLATE / "rules" / "cold.framework.text.md"
HOT = TEMPLATE / "rules" / "hot.framework.md"
VENDOR = TEMPLATE / "vendors" / "telegram.md"
PERSONA = TEMPLATE / "identity" / "hot.custom.agent.md"
EN = TEMPLATE / "bridge" / "i18n" / "en.py"
SETTINGS = TEMPLATE / ".claude" / "settings.json"
INJECT_MARKER = "<!-- dogany:inject-below -->"
POINTER = ("If your next step is the reply to the user, read "
           "rules/cold.framework.text.md first; otherwise ignore.")

# One fragment per rule moved into the text doc (hot Output section and the
# vendor contract's Markup / Containers / Tables / Sample fidelity / Routing).
MOVED = (
    "response skeleton (conclusion / grounds+model / decision",
    "never silent-then-dump",
    "Never expose internal mechanics",
    "DISPLAY_NAMES on: name each thing by the surface",
    "persona\n  display-name only -- never the instance directory",
    "never lock-register words",
    "One self across sessions",
    "a container is EARNED",
    "is the rest skippable?",
    "Expandable blockquote",
    "SECTION_GLYPHS",
    "Tier 1: values / numbers / status only",
    "Tier 3: large multi-column CJK grid",
    "CJK text never aligns in a code block",
    "Wrapping\n  prose UX in a code block is a violation",
    "Detail / history / aged info",
)


def _run(payload, sessions_file, conf=None, bridge_env=None, locale=None):
    env = dict(os.environ)
    env.pop("LOCALE", None)
    if locale is not None:
        env["LOCALE"] = locale
    env["DOGANY_BRIDGE_ENV"] = bridge_env or os.path.join(
        os.path.dirname(sessions_file), "no-such-bridge.env")
    env["DOGANY_SESSIONS_FILE"] = sessions_file
    env["DOGANY_AGENT_CONF"] = conf or os.path.join(
        os.path.dirname(sessions_file), "no-such-agent.conf")
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=raw, env=env,
                          capture_output=True, text=True, timeout=30)


def _flat(text):
    return " ".join(text.split())


class PointerHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v27d1-")
        atexit.register(shutil.rmtree, self.tmp, ignore_errors=True)  # DGN-2090: leave no temp dir
        self.sessions = os.path.join(self.tmp, "sessions.json")
        with open(self.sessions, "w") as fh:
            json.dump({"chat:1": {"session_id": "sess-owner"}}, fh)

    def _payload(self, sess, **extra):
        d = {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "tool_response": "ok",
             "transcript_path": "/x/proj/%s.jsonl" % sess}
        d.update(extra)
        return d

    def test_a_owner_session_gets_exactly_the_pointer(self):
        p = _run(self._payload("sess-owner"), self.sessions)
        self.assertEqual(0, p.returncode, p.stderr)
        out = json.loads(p.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual("PostToolUse", hso["hookEventName"])
        self.assertEqual(POINTER, hso["additionalContext"])
        self.assertEqual(1, len(hso["additionalContext"].splitlines()))
        self.assertTrue(DOC.is_file(), DOC)

    def _conf(self, lang):
        path = os.path.join(self.tmp, "agent-%s.conf" % lang)
        with open(path, "w") as fh:
            fh.write("AGENT_LANG=%s\n" % lang)
        return path

    def test_a_dgn2188_owner_language_line_rides_every_tool_call(self):
        p = _run(self._payload("sess-owner"), self.sessions,
                 self._conf("ko"))
        self.assertEqual(0, p.returncode, p.stderr)
        lines = json.loads(p.stdout)["hookSpecificOutput"][
            "additionalContext"].splitlines()
        self.assertEqual(2, len(lines))
        self.assertIn("narration between tool calls", lines[0])
        self.assertTrue(lines[0].endswith("is in Korean."), lines[0])
        self.assertEqual(POINTER, lines[1])

    def test_a_dgn2188_english_or_unknown_language_adds_nothing(self):
        for lang in ("en", "en_US", "xx"):
            p = _run(self._payload("sess-owner"), self.sessions,
                     self._conf(lang))
            hso = json.loads(p.stdout)["hookSpecificOutput"]
            self.assertEqual(POINTER, hso["additionalContext"], lang)

    def test_a_fdgn578_language_line_rides_every_turn(self):
        payload = {"hook_event_name": "UserPromptSubmit",
                   "prompt": "[dispatch-return] canceled\n- cancel_by: self",
                   "transcript_path": "/x/proj/sess-owner.jsonl"}
        p = _run(payload, self.sessions, self._conf("ko"))
        self.assertEqual(0, p.returncode, p.stderr)
        hso = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual("UserPromptSubmit", hso["hookEventName"])
        self.assertTrue(hso["additionalContext"].endswith("is in Korean."))
        self.assertNotIn(POINTER, hso["additionalContext"])
        p = _run(payload, self.sessions, self._conf("en"))
        self.assertEqual((0, ""), (p.returncode, p.stdout))
        p = _run(dict(payload, transcript_path="/x/proj/sess-cron.jsonl"),
                 self.sessions, self._conf("ko"))
        self.assertEqual((0, ""), (p.returncode, p.stdout))

    def test_a_dgn2423_bridge_locale_wins_over_agent_lang(self):
        env_path = os.path.join(self.tmp, "bridge.env")
        with open(env_path, "w") as fh:
            fh.write("COUNTDOWN_GLYPH_SET=dot\nLOCALE=ko  # set by mint\n")
        for event in ("PostToolUse", "UserPromptSubmit"):
            payload = self._payload("sess-owner", hook_event_name=event)
            for conf in (self._conf("en"), None):
                p = _run(payload, self.sessions, conf, bridge_env=env_path)
                ctx = json.loads(p.stdout)["hookSpecificOutput"][
                    "additionalContext"]
                self.assertIn("is in Korean.", ctx, (event, conf))
        p = _run(self._payload("sess-owner"), self.sessions,
                 self._conf("en"), locale="ko")
        self.assertIn("is in Korean.", p.stdout)
        p = _run(self._payload("sess-owner"), self.sessions,
                 self._conf("ko"), locale="en")
        self.assertEqual(POINTER, json.loads(p.stdout)[
            "hookSpecificOutput"]["additionalContext"])

    def test_b_dgn2188_subagent_gets_no_language_line(self):
        p = _run(self._payload("sess-owner", agent_id="a1"), self.sessions,
                 self._conf("ko"))
        self.assertEqual((0, ""), (p.returncode, p.stdout))

    def test_b_subagent_is_silent(self):
        p = _run(self._payload("sess-owner", agent_id="a1"), self.sessions)
        self.assertEqual((0, ""), (p.returncode, p.stdout))

    def test_b_non_owner_session_is_silent(self):
        p = _run(self._payload("sess-cron"), self.sessions)
        self.assertEqual((0, ""), (p.returncode, p.stdout))

    def test_b_garbage_is_silent(self):
        for raw in ("", "not json", "[1, 2]"):
            p = _run(raw, self.sessions)
            self.assertEqual((0, ""), (p.returncode, p.stdout), raw)


class Wiring(unittest.TestCase):
    def test_c_settings_wire_every_tool(self):
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
        rows = [m for m in hooks["PostToolUse"]
                if any("routines/text-layer-pointer.py" in h["command"]
                       for h in m["hooks"])]
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("*", rows[0]["matcher"])
        ups = [h for m in hooks["UserPromptSubmit"] for h in m["hooks"]
               if "routines/text-layer-pointer.py" in h["command"]]
        self.assertEqual(1, len(ups), ups)


class OneHome(unittest.TestCase):
    def setUp(self):
        self.doc = DOC.read_text(encoding="utf-8")
        self.hot = HOT.read_text(encoding="utf-8")
        self.persona = PERSONA.read_text(encoding="utf-8")
        vendor = VENDOR.read_text(encoding="utf-8")
        self.assertIn(INJECT_MARKER, vendor)
        self.vendor_injected = vendor.split(INJECT_MARKER, 1)[1]
        spec = importlib.util.spec_from_file_location("en_catalog", EN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.fragment = mod.STRINGS["system_prompt"]

    def test_d_each_moved_rule_lives_in_the_doc_only(self):
        for frag in MOVED:
            needle = _flat(frag)
            self.assertIn(needle, _flat(self.doc), "missing from doc: " + frag)
            for name, text in (("hot.framework.md", self.hot),
                               ("vendor injected half", self.vendor_injected),
                               ("bridge fragment", self.fragment)):
                self.assertNotIn(needle, _flat(text),
                                 "duplicate left in %s: %s" % (name, frag))

    def test_d_display_name_rule_left_the_persona_template(self):
        # DGN-1967 DD-18: moved verbatim into the Register section.
        needle = _flat(MOVED[4])
        self.assertIn(needle, _flat(self.doc))
        self.assertNotIn(needle, _flat(self.persona))

    def test_d_hot_points_at_the_doc(self):
        self.assertIn("rules/cold.framework.text.md", self.hot)

    def test_e_container_test_is_who_uses_the_text(self):
        flat = _flat(self.doc)
        self.assertIn("The container test is WHO USES the text", flat)
        self.assertIn("The reader only reads it", flat)
        self.assertIn("-> blockquote", flat)
        self.assertIn("The reader pastes or runs it", flat)
        self.assertIn("-> code block", flat)

    def test_f_framework_wording_names_the_user(self):
        for text in (POINTER, self.doc):
            self.assertIn("the user", text)
            self.assertNotIn("\ud615\ub2d8", text)  # an owner form of address


if __name__ == "__main__":
    unittest.main()
