#!/usr/bin/env python3
# test-lane: fast
"""DGN-1952: machine writer + confirmer regression coverage.

routines/decision-ledger.py, two seats:
  writer  explicit classified pending capture -> entry + per-decision file;
  stop    already captured same-turn menu -> no duplicate;
  ups   the next user message answers that menu -> the entry is CONFIRMED
        with the user's words verbatim and the time.
Cases (ticket work list): menu out -> pending; answer -> confirmed (number
prefix, bare number, label text); non-answer / a turn in between -> stays
pending; non-menu, fenced example, inline mention -> nothing; worker /
subagent / headless session -> nothing; the agent already wrote the entry
(Edit or Bash) -> no duplicate; a re-prompted Stop -> no duplicate; config
unset -> no-op; concurrent writers -> distinct ids; instance-shaped
templates; writer failure cleanup; template wiring.
"""
import importlib.util
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.normpath(os.path.join(HERE, ".."))
TEMPLATE = os.path.dirname(ROUTINES)
HOOK = os.path.join(ROUTINES, "decision-ledger.py")
SID = "owner-session"

SEED = (
    "# Decisions\n\n"
    "## dec-330 -- drop two lines (2026-10-08) [confirmed 2026-10-08 21:12]\n"
    "- body\n\n"
    "## dec-331 -- relocate a rule (2026-10-08) [confirmed 2026-10-08 21:36]\n"
    "- body\n"
)

DECLARATION = ("Class: What\nNot-How: The owner chooses the required outcome.\n"
               "Kind: scope\n")

MENU = (
    "Two ways to keep the ledger complete.\n\n"
    "1. Machine writes it (rec)\n"
    "2. Keep the persona line\n\n"
    "Which one should we take?\n"
    "[[OPTIONS: Machine writes it | Keep the persona line]]"
)


def _transcript(path, prompt="hi", tools=()):
    rows = [{"type": "user", "uuid": "u-" + prompt,
             "message": {"role": "user", "content": prompt}}]
    for i, (name, args) in enumerate(tools):
        rows.append({"type": "assistant", "uuid": "a%d" % i, "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "t%d" % i, "name": name,
                 "input": args}]}})
        rows.append({"type": "user", "uuid": "r%d" % i, "message": {
            "role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t%d" % i,
                 "content": "ok"}]}})
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class Base(unittest.TestCase):
    CONF = (
        "BOT_DATA_DIR=data\n"
        "DECISION_LEDGER_PATH=worklog/decisions/_DECISIONS.md\n"
        "DECISION_LEDGER_DIR=worklog/decisions\n"
    )

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dgn1952-")
        os.makedirs(os.path.join(self.root, "config"))
        os.makedirs(os.path.join(self.root, "worklog", "decisions"))
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write(self.CONF)
        self.ledger = os.path.join(self.root, "worklog", "decisions",
                                   "_DECISIONS.md")
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write(SEED)
        self.tx = os.path.join(self.root, "t.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DOGANY_DISPATCH_WORKER", "DOGANY_SESSION",
                            "PROJECT_ROOT")}
        env.update({"DECISION_LEDGER_ROOT": self.root,
                    "PROJECT_ROOT": self.root})
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run_hook(self, mode, payload, **env):
        proc = subprocess.run(
            [sys.executable, HOOK, mode], input=json.dumps(payload),
            capture_output=True, text=True, env=self.env(**env), timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("", proc.stdout)
        return proc

    def hook_module(self):
        spec = importlib.util.spec_from_file_location("dl_capture_fixture", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.ROOT = self.root
        return mod

    def capture_pending(self, final, prompt="hi"):
        """Exercise the machine writer + pointer independently of Stop policy.

        DGN-2012 now requires persisted class fields before Stop. This suite
        retains writer/confirmer coverage; the owning DGN-2012 suite invokes
        the real Stop directly with unmodified owner replies.
        """
        mod = self.hook_module()
        pointer = mod.read_pointer()
        if pointer and pointer.get("turn") == "u-" + prompt:
            return
        menu = mod.parse_menu(final)
        menu.update(mod.class_fields(DECLARATION))
        rec = mod.write_pending(*mod.ledger_paths(), menu, mod.now())
        rec.update(session_id=SID, turn="u-" + prompt)
        mod.write_pointer(rec)
        mod.log("PENDING", "id=%s labels=%d" % (rec["id"], len(rec["labels"])))

    def capture(self, final, prompt="hi", tools=(), expect_block=False, **env):
        mod = self.hook_module()
        blocks = [{"name": name, "input": args} for name, args in tools]
        owner_env = self.env(**{k: v for k, v in env.items()
                               if k not in ("payload", "sid")})
        payload_guard = env.get("payload", {})
        paths = mod.ledger_paths()
        if (paths and mod._DRF.has_options_marker(final)
                and not mod.turn_wrote_ledger(blocks, *paths, self.root)
                and owner_env.get("PROJECT_ROOT")
                and not owner_env.get("DOGANY_DISPATCH_WORKER")
                and owner_env.get("DOGANY_SESSION") != "cron"
                and not payload_guard.get("agent_id")):
            with mock.patch.dict(os.environ, owner_env, clear=True):
                self.capture_pending(final, prompt)
        _transcript(self.tx, prompt, tools)
        payload = {"session_id": env.pop("sid", SID), "cwd": self.root,
                   "transcript_path": self.tx, "hook_event_name": "Stop",
                   "last_assistant_message": final}
        payload.update(env.pop("payload", {}))
        return self.run_hook("stop", payload, **env)

    def ups(self, prompt, sid=SID):
        return self.run_hook("ups", {
            "session_id": sid, "cwd": self.root, "prompt": prompt,
            "hook_event_name": "UserPromptSubmit"})

    def ledger_text(self):
        with open(self.ledger, encoding="utf-8") as fh:
            return fh.read()

    def dec_file(self, dec_id):
        return os.path.join(self.root, "worklog", "decisions", dec_id + ".md")

    def log_text(self):
        try:
            with open(os.path.join(self.root, "data", "logs",
                                   "decision-ledger.log")) as fh:
                return fh.read()
        except OSError:
            return ""

    def assertUntouched(self):
        self.assertEqual(SEED, self.ledger_text())
        self.assertFalse(os.path.exists(self.dec_file("dec-332")))


class MenuToPending(Base):
    def test_menu_writes_pending_entry_and_file(self):
        self.capture(MENU)
        text = self.ledger_text()
        self.assertTrue(text.startswith(SEED))
        new = text[len(SEED):]
        self.assertRegex(new, r"\n## dec-332 -- Which one should we take\? "
                              r"\(\d{4}-\d\d-\d\d\) \[pending \d{4}-\d\d-\d\d "
                              r"\d\d:\d\d\]\n")
        self.assertIn("<!-- decision-ledger id=dec-332 state=pending -->", new)
        self.assertIn("- Options: 1. Machine writes it / "
                      "2. Keep the persona line", new)
        self.assertIn("- Recommended: 1. Machine writes it\n", new)
        self.assertEqual(1, new.count("## dec-"))
        with open(self.dec_file("dec-332"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(body.startswith("# dec-332\nStatus: pending "))
        self.assertIn("See _DECISIONS.md dec-332.", body)
        self.assertIn("PENDING  id=dec-332", self.log_text())

    def test_bare_marker_with_numbered_run(self):
        self.capture("Pick the path?\n1. Alpha\n2. Beta (rec)\n[[OPTIONS]]")
        new = self.ledger_text()[len(SEED):]
        self.assertIn("-- Pick the path? (", new)
        self.assertIn("- Options: 1. Alpha / 2. Beta (rec)", new)
        self.assertIn("- Recommended: 2. Beta\n", new)

    def test_id_counts_per_decision_files_too(self):
        open(self.dec_file("dec-340"), "w").close()
        self.capture(MENU)
        self.assertIn("## dec-341 --", self.ledger_text())

    def test_second_stop_same_turn_no_duplicate(self):
        self.capture(MENU)
        self.capture(MENU, payload={"stop_hook_active": True})
        self.assertEqual(1, self.ledger_text().count("state=pending"))


class AnswerToConfirmed(Base):
    def _confirmed(self, answer):
        self.capture(MENU)
        self.ups(answer)
        text = self.ledger_text()
        self.assertIn("<!-- decision-ledger id=dec-332 state=confirmed -->",
                      text)
        self.assertNotIn("state=pending", text)
        self.assertRegex(text, r"## dec-332 -- .*\[confirmed \d{4}-\d\d-\d\d "
                               r"\d\d:\d\d: \"" + re.escape(answer) + r"\"\]\n")
        self.assertNotIn("[pending", text)
        with open(self.dec_file("dec-332"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertRegex(body, r"Status: confirmed .*\nAnswer: \""
                         + re.escape(answer) + "\"")
        self.assertIn("CONFIRMED  id=dec-332", self.log_text())

    def test_button_tap_number_prefix(self):
        self._confirmed("1. Machine writes it")

    def test_bare_number(self):
        self._confirmed("2")

    def test_label_text(self):
        self._confirmed("Keep the persona line")

    def test_number_handle(self):
        self._confirmed("No.2")

    def _inbound(self, prompt, rid="req-77", age=0):
        import hashlib
        import time
        import unicodedata
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)
        sha = hashlib.sha256(unicodedata.normalize("NFC", prompt).encode(
            "utf-8")).hexdigest()
        with open(os.path.join(self.root, "data", "inbound-context.json"), "w") as fh:
            json.dump({"request_id": rid, "ts": time.time() - age, "text_sha": sha}, fh)

    def test_amend1_machine_confirm_carries_req(self):
        # Why net AMEND-1 6.2: the answering turn's bridge request id
        self.capture(MENU)
        self._inbound("1")
        self.ups("1")
        self.assertIn("<!-- decision-ledger id=dec-332 state=confirmed req=req-77 -->",
                      self.ledger_text())

    def test_amend1_no_req_without_a_matching_fresh_inbound(self):
        for inbound in (None, ("another prompt", 0), ("1", 3600)):
            self.setUp()
            self.capture(MENU)
            if inbound:
                self._inbound(inbound[0], age=inbound[1])
            self.ups("1")
            text = self.ledger_text()
            self.assertIn("<!-- decision-ledger id=dec-332 state=confirmed -->", text)
            self.assertNotIn("req=", text)
            self.tearDown()

    def test_amend1_agent_written_entry_has_no_req(self):
        self.capture(MENU, tools=[("Edit", {"file_path": self.ledger})], expect_block=True)
        self._inbound("1")
        self.ups("1")
        self.assertNotIn("req=", self.ledger_text())

    def test_push_then_reply_is_the_answer(self):
        # DGN-1967 bug 2: the bridge prefixes the owner's turn with the
        # pushes sent since the last turn; only the owner's words answer.
        self.capture(MENU)
        prompt = ("[bridge:outbound-context]\n--- sent 10:00 ---\n"
                  "Build is green. Merge now?\n[/bridge:outbound-context]\n1")
        self._inbound(prompt)
        self.ups(prompt)
        text = self.ledger_text()
        self.assertIn("<!-- decision-ledger id=dec-332 state=confirmed req=req-77 -->", text)
        self.assertIn('"1"]', text)
        self.assertNotIn("outbound-context", text)

    def test_non_answer_leaves_pending(self):
        self.capture(MENU)
        self.ups("wait, what does option 1 cost?")
        self.assertIn("state=pending", self.ledger_text())
        self.ups("1")  # pointer consumed: a late number confirms nothing
        self.assertIn("state=pending", self.ledger_text())

    def test_out_of_range_number_leaves_pending(self):
        self.capture(MENU)
        self.ups("3")
        self.assertIn("state=pending", self.ledger_text())

    def test_turn_in_between_leaves_pending(self):
        self.capture(MENU)
        self.ups("next")  # Real owner ingress clears the prior menu pointer.
        self.capture("Plain reply, no menu.", prompt="next")
        self.ups("1")
        self.assertIn("state=pending", self.ledger_text())

    def test_other_session_answer_leaves_pending(self):
        self.capture(MENU)
        self.ups("1", sid="another-session")
        self.assertIn("state=pending", self.ledger_text())

    def test_injected_turn_keeps_pointer(self):
        self.capture(MENU)
        self.ups("[bridge:injected-turn] cron notice")
        self.ups("1")
        self.assertIn("state=confirmed", self.ledger_text())


class TightAnswer(Base):
    """DGN-1967 step 5 (bugs 15-17, 20)."""

    def test_number_led_words_are_not_a_pick(self):
        # bug 15: "1.5 hours ..." read as option 1
        for words in ("1.5 hours is too long, rethink", "1. but cheaper",
                      "2) not that one either"):
            with self.subTest(words=words):
                self.setUp()
                self.capture(MENU)
                self.ups(words)
                self.assertIn("state=pending", self.ledger_text())
                self.tearDown()

    def test_bare_number_with_dot_and_spaced_handle(self):
        for words in ("2.", "1)", "No. 2"):
            with self.subTest(words=words):
                self.setUp()
                self._picked(words)
                self.tearDown()

    def _picked(self, words):
        self.capture(MENU)
        self.ups(words)
        self.assertIn("state=confirmed", self.ledger_text())

    def test_no_labels_no_number_pick(self):
        # bug 15: with no labels parsed any number counted
        self.capture("Pick one.\n[[OPTIONS]]")
        self.assertIn("- Options: (none parsed)", self.ledger_text())
        self.ups("7")
        self.assertIn("state=pending", self.ledger_text())

    def test_reading_the_ledger_with_bash_is_not_writing_it(self):
        # bug 16: cat / grep named the ledger and counted as the agent's write
        for cmd in ("cat worklog/decisions/_DECISIONS.md",
                    "grep -n dec- worklog/decisions/_DECISIONS.md | tail -3",
                    "ls worklog/decisions/"):
            with self.subTest(cmd=cmd):
                self.setUp()
                self.capture(MENU, tools=[("Bash", {"command": cmd})])
                self.assertIn("## dec-332", self.ledger_text())
                self.tearDown()

    def test_bash_writes_still_count(self):
        for cmd in ("cd worklog/decisions && echo x >> _DECISIONS.md",
                    "sed -i s/a/b/ worklog/decisions/_DECISIONS.md",
                    "echo x | tee -a worklog/decisions/_DECISIONS.md",
                    "cp /tmp/x worklog/decisions/dec-332.md",
                    "python3 -c 'open(\"worklog/decisions/_DECISIONS.md\",\"a\")'"):
            with self.subTest(cmd=cmd):
                self.setUp()
                self.capture(MENU, tools=[("Bash", {"command": cmd})], expect_block=True)
                self.assertUntouched()
                self.tearDown()

    def test_label_with_slash_round_trips(self):
        # bug 17: the " / " option separator split a "Keep A / B" label
        self.capture("Which?\n[[OPTIONS: Keep A / B | Drop it]]")
        self.assertIn("- Options: 1. Keep A / B / 2. Drop it", self.ledger_text())
        sys.path.insert(0, os.path.join(ROUTINES, "lib"))
        import decision_ledger_fmt as fmt
        line = [l for l in self.ledger_text().split("\n")
                if l.startswith("- Options: ")][-1]
        self.assertEqual(fmt.options_of_line(line), ["Keep A / B", "Drop it"])
        self.ups("Keep A / B")
        self.assertIn("state=confirmed", self.ledger_text())

    def test_crew_offset_when_the_agent_sets_none(self):
        # bug 20: the ledger read AGENT_TZ_OFFSET_HOURS only (and took 5.5)
        import datetime
        crew = os.path.join(self.root, "crews")
        os.makedirs(crew)
        with open(os.path.join(crew, "crew.conf"), "w") as fh:
            fh.write("TZ_OFFSET_HOURS=14\n")
        with open(os.path.join(self.root, "config", "agent.conf"), "a") as fh:
            fh.write("AGENT_TZ_OFFSET_HOURS=5.5\n")
        tz = datetime.timezone(datetime.timedelta(hours=14))
        before = datetime.datetime.now(tz)
        self.capture(MENU, DOGANY_CREW_DIR=crew)
        after = datetime.datetime.now(tz)
        stamps = {m.strftime("pending %Y-%m-%d %H:%M") for m in (before, after)}
        self.assertTrue(any(st in self.ledger_text() for st in stamps),
                        (stamps, self.ledger_text()[-400:]))


class NothingWritten(Base):
    def test_non_menu_reply(self):
        self.capture("Done. Tests are green.")
        self.assertUntouched()

    def test_fenced_example_marker(self):
        self.capture("Example of the marker:\n```\n1. a\n2. b\n[[OPTIONS]]\n```\n"
                  "That is how it looks.")
        self.assertUntouched()

    def test_inline_mention(self):
        self.capture("End a menu with the [[OPTIONS]] marker line.")
        self.assertUntouched()

    def test_dispatch_worker(self):
        self.capture(MENU, DOGANY_DISPATCH_WORKER="run-1")
        self.assertUntouched()

    def test_headless_session(self):
        self.capture(MENU, PROJECT_ROOT=None)
        self.assertUntouched()

    def test_cron_override(self):
        self.capture(MENU, DOGANY_SESSION="cron")
        self.assertUntouched()

    def test_subagent_payload(self):
        self.capture(MENU, payload={"agent_id": "sub-1"})
        self.assertUntouched()

    def test_agent_wrote_entry_with_edit(self):
        self.capture(MENU, tools=[("Edit", {"file_path": self.ledger,
                                         "old_string": "a",
                                         "new_string": "b"})], expect_block=True)
        self.assertUntouched()
        self.assertNotIn("REFUSED_CLASS", self.log_text())

    def test_agent_wrote_dec_file(self):
        self.capture(MENU, tools=[("Write", {
            "file_path": os.path.join(self.root, "worklog", "decisions",
                                      "dec-332.md"), "content": "x"})], expect_block=True)
        self.assertEqual(SEED, self.ledger_text())

    def test_agent_wrote_entry_with_bash(self):
        self.capture(MENU, tools=[("Bash", {
            "command": "printf x >> worklog/decisions/_DECISIONS.md"})], expect_block=True)
        self.assertUntouched()

    def test_unrelated_tool_still_writes(self):
        self.capture(MENU, tools=[("Bash", {"command": "ls"})])
        self.assertIn("## dec-332", self.ledger_text())


class Unconfigured(Base):
    CONF = "BOT_DATA_DIR=data\n"

    def test_no_ledger_configured_is_noop(self):
        self.capture(MENU)
        self.ups("1")
        self.assertUntouched()
        self.assertFalse(os.path.exists(os.path.join(self.root, "data")))


class InstanceFormat(Base):
    # Non-ASCII instance copy, written as escapes (code stays ASCII).
    CONF = Base.CONF + (
        "DECISION_LEDGER_PENDING_STATUS='\ub300\uae30 {date} {time}'\n"
        "DECISION_LEDGER_CONFIRMED_STATUS='\ud655\uc815 {date} {time}'\n"
        "DECISION_LEDGER_CONFIRMED_TAG='[{status}: the user \"{answer}\"]'\n"
    )

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.root, "config", "agent.conf"), "w",
                  encoding="utf-8") as fh:
            fh.write(self.CONF)

    def test_configured_shape(self):
        self.capture(MENU)
        self.ups("1. Machine writes it")
        self.assertRegex(
            self.ledger_text(),
            r"\n## dec-332 -- Which one should we take\? \(\d{4}-\d\d-\d\d\) "
            r"\[\ud655\uc815 \d{4}-\d\d-\d\d \d\d:\d\d: the user "
            r"\"1\. Machine writes it\"\]\n")
        with open(self.dec_file("dec-332"), encoding="utf-8") as fh:
            self.assertIn("Status: \ud655\uc815 ", fh.read())


def _concurrent_write(args):
    root, idx = args
    os.environ["DECISION_LEDGER_ROOT"] = root
    import importlib.util
    spec = importlib.util.spec_from_file_location("dl_%d" % idx, HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    paths = mod.ledger_paths()
    menu = {"question": "q%d?" % idx, "labels": ["a", "b"], "recommended": "",
            "class": "What", "not_how": "The owner chooses the outcome.",
            "kind": "fork"}
    return mod.write_pending(paths[0], paths[1], menu, mod.now())["id"]


class Concurrency(Base):
    def test_parallel_writers_get_distinct_ids(self):
        with multiprocessing.get_context("spawn").Pool(6) as pool:
            ids = pool.map(_concurrent_write, [(self.root, i) for i in range(12)])
        self.assertEqual(12, len(set(ids)), ids)
        text = self.ledger_text()
        for i in range(332, 344):
            self.assertEqual(1, text.count("## dec-%d --" % i), i)
            self.assertTrue(os.path.isfile(self.dec_file("dec-%d" % i)))


class FailOpen(Base):
    CONF = Base.CONF.replace("worklog/decisions/_DECISIONS.md",
                             "missing-dir-file/_DECISIONS.md").replace(
        "DECISION_LEDGER_DIR=worklog/decisions",
        "DECISION_LEDGER_DIR=worklog/decisions/blocker/sub")

    def test_pending_writer_error_leaves_no_partial_entry(self):
        with open(os.path.join(self.root, "worklog", "decisions", "blocker"),
                  "w") as fh:
            fh.write("a file where a dir is expected")
        with self.assertRaises(OSError):
            self.capture_pending(MENU)
        self.assertUntouched()


class Wiring(unittest.TestCase):
    def test_wired_on_ups_without_stop(self):
        with open(os.path.join(TEMPLATE, ".claude", "settings.json")) as fh:
            hooks = json.load(fh)["hooks"]
        for event, mode in (("UserPromptSubmit", "ups"),):
            cmds = [h["command"] for m in hooks.get(event, [])
                    for h in m["hooks"]]
            self.assertEqual(1, sum(c.endswith(
                "/routines/decision-ledger.py " + mode) for c in cmds), event)

    def test_template_conf_ships_off(self):
        sys.path.insert(0, os.path.join(ROUTINES, "lib"))
        from conf_reader import conf_get
        self.assertEqual("", conf_get(
            "DECISION_LEDGER_PATH",
            os.path.join(TEMPLATE, "config", "agent.conf")))


class KeyedPending(Base):
    def test_cross_source_replay_returns_one_pending_id_and_preserves_owner_authority(self):
        mod = self.hook_module()
        key = mod.decision_key("owner", "product", "subject", "scope", "revision-1")
        menu = dict(question="Which outcome?", labels=["A", "B"], recommended="A",
                    **{"class": "What", "not_how": "The owner chooses the outcome.",
                       "kind": "scope"})
        menu["event_ids"] = ["return:one"]
        first = mod.capture_pending_once(key, menu)
        menu.update(question="Another phrasing?", event_ids=["alert:two"])
        replay = mod.capture_pending_once(key, menu)
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(replay["event_ids"], ["alert:two", "return:one"])
        with open(self.ledger) as stream:
            text = stream.read()
        self.assertEqual(text.count("Decision-Key: " + key), 1)
        self.assertIn("state=pending", text)
        self.assertNotIn("Another phrasing?", text)
        self.assertNotIn("req=", text)

    def test_capture_journal_repairs_each_partial_publication_before_next_writer(self):
        from unittest.mock import patch
        for boundary in (1, 2):
            with self.subTest(boundary=boundary):
                mod = self.hook_module()
                key = mod.decision_key("owner", "product", "subject", "scope", str(boundary))
                menu = dict(question="Outcome?", labels=["A", "B"], recommended="A",
                            **{"class": "Why", "not_how": "The owner sets the purpose.",
                               "kind": "fork"})
                original = mod._pending_text
                calls = []
                def crash(path, text):
                    calls.append(path)
                    if len(calls) == boundary:
                        raise OSError("fixture crash")
                    return original(path, text)
                with patch.object(mod, "_pending_text", crash):
                    with self.assertRaises(OSError):
                        mod.capture_pending_once(key, menu)
                result = mod.capture_pending_once(key, menu)
                self.assertTrue(os.path.isfile(result["file"]))
                with open(self.ledger) as stream:
                    self.assertEqual(stream.read().count("Decision-Key: " + key), 1)
                self.assertEqual(result["id"], mod.capture_pending_once(key, menu)["id"])


if __name__ == "__main__":
    unittest.main()
