#!/usr/bin/env python3
# test-lane: fast    # in-process component calls and scratch transcripts
# test-component: why-net owner-turn confirmation floor
# test-tier: 1
# test-prereq: tmp-files, subprocess, python3 (synthetic transcripts only)
# test-covers: agents/.template/routines/lib/why_owner_turn.py agents/.template/routines/lib/why_net/events.py agents/.template/routines/lib/why_net/ups.py agents/.template/routines/lib/why_net/retro.py
# promise: verified past owner wording confirms without a new tap
# promise: explicit exact-candidate picks confirm while retaining owner-turn refusal checks
# promise: selection confirmation audits re-prove the original candidate and owner turn
# promise: arbitrary menu labels select only their matching numbered body item
# promise: surrounding unnumbered prose cannot lend a candidate to the selected item
# promise: repeated selected candidates and ambiguous menus refuse
# promise: candidate binding is verbatim, without Unicode or whitespace folding
# promise: the nearest ancestral menu wins across ordinary intervening turns
# promise: single-candidate quotations bind only the affirmative question slot
# promise: typed numeric replies bind the exact candidate through the same menu checks
# promise: persona confirm labels bind by slot while changed candidates refuse
# promise: a negative confirm queues the existing wrong-reading follow-up
"""v2.7 T1 P1: Why net substrate + floor (docs/design/v27-t1-why-net-LOCKED.md).

Fixture ids (spec section 7, Floor / AC1):
  R1a-R1m, P1, L1, L2, S1.  Plus: the write guard's pre-existing rows
  (a)/(b) still deny/allow as before, marker parity against the owners of
  each marker list, and the former P1 stubs (head, lint-ticket) are built.

Synthetic data only: tests/fixtures/why-net/transcript-floor.jsonl is copied
into a temp HOME as ~/.claude/projects/<enc ws>/<session>.jsonl.  No real
transcript is read.  Every user entry of the fixture is first submitted
through `why-net ups` with a fresh bridge inbound-context, exactly as a live
turn is: the owner turns land in the owner-turn ledger, the injected ones do
not (grill fix 1).  The grill attacks themselves live in
test-why-net-grill.py.
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

HERE = Path(__file__).resolve().parent
ROUTINES = HERE.parent
ROOT = ROUTINES.parent
FIX = HERE / "fixtures" / "why-net"
CLI = ROUTINES / "why-net.py"
GUARD = ROUTINES / "cc-memory-write-guard.py"
sys.path.insert(0, str(ROUTINES / "lib"))

import why_owner_turn as wot  # noqa: E402
from why_net import budget as WB, ups as WU  # noqa: E402

SESSION = "sess-why-0001"
OWNER = "Ship the morning brief so the owner reads one thing first"
OWNER2 = "Drop the old intake form; it costs the owner a minute a day"
TAP = "\uc804\ubd80 \ucd94\ucc9c\ub300\ub85c (\ucd94\ucc9c)"
HYP = "[\uac00\uc124]"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load("_why_floor_cli", CLI)


class Base(unittest.TestCase):
    def setUp(self):
        self.now = 1792329408.0
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="why-net-floor-"))
        self.home = os.path.join(self.tmp, "home")
        self.ws = os.path.join(self.tmp, "ws")
        self.data = os.path.join(self.tmp, "bot")
        os.makedirs(self.ws)
        proj = os.path.join(self.home, ".claude", "projects",
                            wot.encode_cwd(self.ws))
        os.makedirs(proj)
        shutil.copy(FIX / "transcript-floor.jsonl",
                    os.path.join(proj, SESSION + ".jsonl"))
        self.conf = os.path.join(self.tmp, "agent.conf")
        with open(self.conf, "w") as fh:
            fh.write("WHY_NET=on\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))
                    and k not in ("CLAUDE_SESSION_ID",
                                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR")}
        self.env.update({"HOME": self.home, "CLAUDE_PROJECT_DIR": self.ws,
                         "DOGANY_BOT_DATA_DIR": self.data, "CLAUDECODE": "1",
                         "DOGANY_AGENT_CONF": self.conf,
                         "CLAUDE_CODE_SESSION_ID": SESSION})
        self._saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        self.store_dir = os.path.join(self.data, "state", "why-net")
        self.events = os.path.join(self.store_dir, "events.jsonl")
        self.transcript = os.path.join(proj, SESSION + ".jsonl")
        self.submit_fixture_turns()

    def submit(self, prompt, session=SESSION, source="message", **inbound):
        """One live turn: the bridge publishes inbound-context, then Claude
        Code runs the UserPromptSubmit hook."""
        os.makedirs(self.data, exist_ok=True)
        self._rid = getattr(self, "_rid", 0) + 1
        ctx = dict({"schema": 1, "request_id": "req-%d" % self._rid,
                    "source": source, "message_id": 100 + self._rid,
                    "text_sha": wot.turn_sha(prompt),
                    "ts": self.now}, **inbound)
        with open(os.path.join(self.data, "inbound-context.json"), "w") as fh:
            json.dump(ctx, fh)
        WU.run(WB.Ctx(now=self.now), {"prompt": prompt, "session_id": session,
                          "transcript_path": self.transcript,
                          "hook_event_name": "UserPromptSubmit"})

    def submit_fixture_turns(self):
        with open(self.transcript, encoding="utf-8") as fh:
            for line in fh:
                e = json.loads(line)
                if e.get("type") != "user" or e.get("isMeta"):
                    continue  # Claude Code runs no UserPromptSubmit for meta
                c = e["message"]["content"]
                if isinstance(c, list):
                    c = "\n".join(b.get("text", "") for b in c
                                  if b.get("type") == "text")
                if c:
                    self.submit(c)

    def tearDown(self):
        wot._store().bind_clock(None)
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, env=None):
        saved = dict(os.environ)
        out, err = io.StringIO(), io.StringIO()
        try:
            os.environ.clear()
            os.environ.update(env or self.env)
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(args, ctx=WB.Ctx(root=str(ROOT), now=self.now))
            return rc, out.getvalue(), err.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def events_bytes(self):
        try:
            return Path(self.events).read_bytes()
        except FileNotFoundError:
            return b""

    def propose(self, text, *parents):
        args = ["propose", "--text", text, "--scope", "framework"]
        for p in parents:
            args += ["--parent", p]
        rc, out, err = self.run_cli(*args)
        self.assertEqual(rc, 0, out + err)
        return re.search(r"why=(why-\d{4})", out).group(1)

    def node(self, wid):
        rc, out, _ = self.run_cli("show", "--why", wid)
        self.assertEqual(rc, 0, out)
        return json.loads(out)[wid]

    def assert_refused(self, args, reason=None, env=None):
        before = self.events_bytes()
        rc, out, err = self.run_cli(*args, env=env)
        self.assertEqual(rc, 3, out + err)
        self.assertTrue(out.startswith("signal::WHY_REFUSED"), out)
        if reason:
            self.assertIn("reason=" + reason, out)
        self.assertEqual(self.events_bytes(), before, "events changed")


class QueuedOwnerConfirm(Base):
    """DGN-2174: the owner's mid-turn words have the same confirmation floor."""

    QUOTE = "Make the demo simple so the owner can finish planning."

    def record(self, text, *, entry=None, ledger=True, on_chain=True):
        if ledger:
            self.submit(text)
        rows = [json.loads(s) for s in Path(self.transcript).read_text().splitlines()]
        serial = getattr(self, "_serial", 0) + 1
        self._serial = serial
        uid = "queued-owner-%d" % serial
        row = dict(entry) if entry is not None else {
            "type": "attachment", "attachment": {
                "type": "queued_command", "commandMode": "prompt", "prompt": text}}
        row.update(uuid=uid, parentUuid=rows[-1]["uuid"], sessionId=SESSION,
                   timestamp="2026-10-18T10:00:00Z")
        # The runtime continues its tool call after delivering the queued input.
        tail = {"type": "assistant", "uuid": "continued-%d" % serial,
                "parentUuid": uid if on_chain else rows[-1]["uuid"],
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tool-%d" % serial,
                     "name": "Bash", "input": {}}]}}
        with open(self.transcript, "a", encoding="utf-8") as fh:
            for r in (row, tail):
                fh.write(json.dumps(r) + "\n")
        return uid

    def confirm_args(self, wid, quote, uid):
        return ["confirm", "--why", wid, "--session", SESSION,
                "--quote", quote, "--uuid", uid]

    def test_queued_prompt_with_or_without_external_user_type_confirms(self):
        for external in (False, True):
            with self.subTest(external=external):
                entry = {"type": "attachment", "attachment": {
                    "type": "queued_command", "commandMode": "prompt",
                    "prompt": self.QUOTE}}
                if external:
                    entry["userType"] = "external"
                uid = self.record(self.QUOTE, entry=entry)
                wid = self.propose(self.QUOTE)
                rc, out, err = self.run_cli(*self.confirm_args(wid, self.QUOTE, uid))
                self.assertEqual(rc, 0, out + err)
                node = self.node(wid)
                self.assertEqual((node["status"], node["worded_by"]),
                                 ("confirmed-provisional", "owner"))
                event = json.loads(self.events_bytes().splitlines()[-1])["data"]
                self.assertEqual(event["quote"], self.QUOTE)
                self.assertEqual(event["source"]["message_uuid"], uid)
                self.assertEqual(event["otl"], wot.turn_otl(SESSION, uid))
                self.assertIsInstance(wot.resolve(self.QUOTE, SESSION, uid, audit=True),
                                      wot.TurnRef)

    def test_whitespace_only_matching_preserves_owner_bytes_for_both_shapes(self):
        text = "Make\t the demo\n simple so the owner can finish planning."
        for queued in (False, True):
            with self.subTest(queued=queued):
                entry = None if queued else {
                    "type": "user", "userType": "external",
                    "message": {"role": "user", "content": text}}
                uid = self.record(text, entry=entry)
                wid = self.propose("  " + text + "\n")
                rc, out, err = self.run_cli(*self.confirm_args(wid, self.QUOTE, uid))
                self.assertEqual(rc, 0, out + err)
                event = json.loads(self.events_bytes().splitlines()[-1])["data"]
                self.assertEqual(event["quote"], text)

    def test_injected_copy_of_same_words_never_confirms(self):
        self.record(self.QUOTE)  # Identical genuine words elsewhere cannot lend authority.
        for prefix in ("[bridge:injected-turn]", "[bridge:other-machine-turn]",
                       "[cron-inject]", "<task-notification>", "<system-reminder>"):
            for queued in (False, True):
                with self.subTest(prefix=prefix, queued=queued):
                    text = " \n" + prefix + "\n" + self.QUOTE
                    entry = None if queued else {
                        "type": "user", "userType": "external",
                        "message": {"role": "user", "content": text}}
                    uid = self.record(text, entry=entry)
                    wid = self.propose(self.QUOTE)
                    self.assert_refused(self.confirm_args(wid, self.QUOTE, uid),
                                        "quote-not-owner-turn")

    def test_unknown_or_machine_attachment_shapes_refuse(self):
        for override in ({"userType": "internal"}, {"isMeta": True},
                         {"isSidechain": True}, {"isCompactSummary": True},
                         {"toolUseResult": {}},
                         {"attachment": {"type": "queued_command", "prompt": self.QUOTE}},
                         {"attachment": {"type": "queued_command", "commandMode": "task-notification",
                                         "prompt": self.QUOTE}},
                         {"attachment": {"type": "hook_additional_context", "commandMode": "prompt",
                                         "prompt": self.QUOTE}},
                         {"attachment": {"type": "queued_command", "commandMode": "prompt",
                                         "prompt": [{"type": "text", "text": self.QUOTE}]}}):
            with self.subTest(override=override):
                entry = {"type": "attachment", "attachment": {
                    "type": "queued_command", "commandMode": "prompt", "prompt": self.QUOTE}}
                entry.update(override)
                uid = self.record(self.QUOTE, entry=entry)
                wid = self.propose(self.QUOTE)
                self.assert_refused(self.confirm_args(wid, self.QUOTE, uid),
                                    "quote-not-owner-turn")

    def test_queued_turn_still_needs_ledger_and_main_chain(self):
        for ledger, on_chain, reason in ((False, True, "turn-not-in-ledger"),
                                         (True, False, "turn-off-chain")):
            with self.subTest(ledger=ledger, on_chain=on_chain):
                uid = self.record(self.QUOTE, ledger=ledger, on_chain=on_chain)
                wid = self.propose(self.QUOTE)
                self.assert_refused(self.confirm_args(wid, self.QUOTE, uid), reason)

    def test_queued_text_cannot_paraphrase_or_drop_clause_or_question(self):
        for text, quote, reason in (
                (self.QUOTE, self.QUOTE.replace("simple", "easy"), "quote-not-owner-turn"),
                (self.QUOTE, self.QUOTE.lower(), "quote-not-owner-turn"),
                (self.QUOTE, self.QUOTE[:-1] + "!", "quote-not-owner-turn"),
                (self.QUOTE, "so the owner can finish planning.", "quote-not-clause"),
                (self.QUOTE[:-1] + "?", self.QUOTE[:-1], "quote-question"),
                ("> " + self.QUOTE, self.QUOTE, "quote-markup")):
            with self.subTest(text=text, quote=quote):
                uid = self.record(text)
                wid = self.propose(quote)
                self.assert_refused(self.confirm_args(wid, quote, uid), reason)

    def test_agent_echo_is_refused_in_queued_prompt_even_with_new_spacing(self):
        self.record("Synthetic planning request", entry={
            "type": "assistant", "message": {"role": "assistant", "content": self.QUOTE}})
        uid = self.record(self.QUOTE.replace(" ", "\t  "))
        wid = self.propose(self.QUOTE)
        self.assert_refused(self.confirm_args(wid, self.QUOTE, uid), "quote-echo")

    def test_queued_whitespace_match_does_not_relax_proposed_words(self):
        uid = self.record(self.QUOTE)
        wid = self.propose(self.QUOTE.replace("simple", "easy"))
        self.assert_refused(self.confirm_args(wid, self.QUOTE, uid), "text-not-quote")


class PastOwnerConfirm(Base):
    QUOTE = QueuedOwnerConfirm.QUOTE
    record = QueuedOwnerConfirm.record
    TAP = "1. Confirm this sentence"

    def pick(self, candidate=None, *, source="callback", ledger=True,
             on_chain=True, injected=False, outbound=False, menu=None,
             interlude=(), past=True, **inbound):
        candidate = candidate or self.QUOTE
        menu = menu or (candidate + "\n\nConfirm this sentence?\n\n"
                        "[[OPTIONS]]\n1. Confirm this sentence\n2. Revise")
        self.record("request", entry={"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": menu}]}}, ledger=False)
        for kind, content in interlude:
            if kind == "assistant":
                self.record("request", entry={"type": kind, "message": {
                    "role": kind, "content": content}}, ledger=False)
            else:
                self.record(content)
        text = self.TAP
        if injected:
            text = "[bridge:injected-turn]\n" + text
        if outbound:
            text = "[bridge:outbound-context]\n" + candidate + "\n[/bridge:outbound-context]\n" + text
        if ledger:
            self.submit(text, source=source, **inbound)
        uid = self.record(text, ledger=False, on_chain=on_chain)
        if past:
            self.submit("Current owner request", session="current-session")
            self.env["CLAUDE_CODE_SESSION_ID"] = "current-session"
            os.environ["CLAUDE_CODE_SESSION_ID"] = "current-session"
        return uid

    def confirm(self, wid, uid):
        from why_net import events
        return events.confirm_quote(wid, self.TAP, SESSION, uid)

    def test_past_typed_words_use_original_turn_and_keep_general_session_gate(self):
        from why_net import events
        uid = self.record(self.QUOTE)
        self.submit("Current request", session="current-session")
        self.env["CLAUDE_CODE_SESSION_ID"] = "current-session"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "current-session"
        self.assertEqual(wot.resolve(self.QUOTE, SESSION, uid).reason, "not-current-session")
        wid = events.propose(self.QUOTE, "framework")["why"]
        row = events.confirm_quote(wid, self.QUOTE, SESSION, uid)
        self.assertEqual(row["data"]["otl"], wot.turn_otl(SESSION, uid))
        self.assertEqual(row["data"]["worded_by"], "owner")

    def test_past_callback_with_outbound_context_confirms_exact_candidate_and_audits(self):
        from why_net import events
        uid = self.pick(outbound=True)
        wid = events.propose(self.QUOTE, "framework")["why"]
        row = self.confirm(wid, uid)
        self.assertEqual(row["data"]["candidate"], self.QUOTE)
        self.assertEqual(row["data"]["quote"], self.TAP)
        self.assertEqual(row["data"]["worded_by"], "agent")
        self.assertEqual(row["data"]["source"]["message_uuid"], uid)
        self.assertEqual(row["data"]["otl"], wot.turn_otl(SESSION, uid))
        self.assertEqual(events.nodes()[wid]["status"], "confirmed-provisional")
        self.assertEqual(events.audit_confirms(), [])
        # An audit re-reads the selection, rather than trusting the event.
        Path(self.transcript).write_text(Path(self.transcript).read_text().replace(
            "Confirm this sentence?", "Revise this sentence?"))
        self.assertEqual(events.audit_confirms()[0]["reason"], "quote-tap")

    def test_past_typed_selection_also_requires_exact_menu(self):
        from why_net import events
        uid = self.pick(source="message")
        wid = events.propose(self.QUOTE, "framework")["why"]
        self.assertEqual(self.confirm(wid, uid)["data"]["worded_by"], "agent")

    def test_forged_past_selection_needs_ledger(self):
        from why_net import events, WhyRefusal
        uid = self.pick(ledger=False)
        wid = events.propose(self.QUOTE, "framework")["why"]
        before = self.events_bytes()
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "turn-not-in-ledger")
        self.assertEqual(self.events_bytes(), before)

    def test_ledgered_past_selection_needs_main_chain(self):
        from why_net import events, WhyRefusal
        uid = self.pick(on_chain=False)
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "turn-off-chain")

    def test_injected_past_selection_stays_refused(self):
        from why_net import events, WhyRefusal
        uid = self.pick(injected=True)
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "quote-not-owner-turn")

    def test_past_selection_cannot_confirm_a_different_or_negated_candidate(self):
        from why_net import events, WhyRefusal
        uid = self.pick(candidate="Do not: " + self.QUOTE)
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "quote-tap")

    def test_past_pick_rejects_contradictory_question_and_keyboard_mismatch(self):
        from why_net import events, WhyRefusal
        menu = self.QUOTE + "\n\nConfirm this sentence? Do not confirm it.\n\n[[OPTIONS: Confirm this sentence | Revise]]"
        uid = self.pick(menu=menu)
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal):
            self.confirm(wid, uid)
        uid = self.pick(keyboard=["1. Confirm this sentence", "2. Revise"], option_index=2)
        with self.assertRaises(WhyRefusal):
            self.confirm(wid, uid)

    def test_labeled_menu_and_bound_keyboard_confirm(self):
        from why_net import events
        menu = self.QUOTE + "\n\nConfirm this sentence?\n\n[[OPTIONS: Confirm this sentence | Revise]]"
        uid = self.pick(menu=menu, keyboard=["1. Confirm this sentence", "2. Revise"], option_index=1)
        wid = events.propose(self.QUOTE, "framework")["why"]
        self.assertEqual(self.confirm(wid, uid)["data"]["candidate"], self.QUOTE)

    def menu_ref(self, menu, tap="1. Short label", **kwargs):
        self.TAP = tap
        uid = self.pick(menu=menu, **kwargs)
        return wot.resolve(tap, SESSION, uid, allow_past=True, candidate=self.QUOTE)

    def numbered(self, marker="[[OPTIONS: Short label | Other label]]"):
        return 'Candidates:\n\n1. "' + self.QUOTE + '" (preferred)\n2. "Another goal."\n\n' + marker

    def test_arbitrary_labels_bind_numbered_items_in_both_marker_shapes(self):
        for marker in ("[[OPTIONS: Short label | Other label]]",
                       "[[OPTIONS]]\n1. Short label\n2. Other label"):
            with self.subTest(marker=marker):
                ref = self.menu_ref(self.numbered(marker))
                self.assertIsInstance(ref, wot.TurnRef)

    def test_nonfirst_numbered_item_confirms_in_current_session(self):
        menu = '1. "Another goal."\n2. "' + self.QUOTE + '"\n[[OPTIONS: One | Two]]'
        ref = self.menu_ref(menu, "2. Two", past=False,
                            keyboard=["1. One", "2. Two"], option_index=2)
        self.assertIsInstance(ref, wot.TurnRef)
        from why_net import events
        wid = events.propose(self.QUOTE, "framework")["why"]
        row = events.confirm_quote(wid, self.TAP, SESSION, ref.message_uuid)
        self.assertEqual(row["data"]["candidate"], self.QUOTE)
        self.assertEqual(events.audit_confirms(), [])

    def test_pick_cannot_borrow_candidate_from_another_item_or_preamble(self):
        for menu in (self.numbered(), self.QUOTE + '\n1. First\n2. Second\n[[OPTIONS: Short label | Other label]]'):
            with self.subTest(menu=menu):
                self.assertEqual(self.menu_ref(menu, "2. Other label").reason, "quote-tap")

    def test_repeated_candidate_inside_selected_item_refuses(self):
        for menu in (self.numbered().replace('(preferred)', self.QUOTE),
                     self.numbered().replace('(preferred)', '\n   ' + self.QUOTE)):
            with self.subTest(menu=menu):
                self.assertEqual(self.menu_ref(menu).reason, "quote-tap")

    def test_unselected_repetition_does_not_change_exact_selected_item(self):
        # The owner chooses a slot; another option can share its wording and
        # add a different rider without changing what the chosen slot says.
        menu = self.numbered().replace('Another goal.', self.QUOTE + ' Then change it.')
        self.assertIsInstance(self.menu_ref(menu), wot.TurnRef)

    def test_unnumbered_tail_cannot_supply_candidate_to_last_item(self):
        menu = ('1. First choice\n2. Second choice\n\nA separate note: ' + self.QUOTE
                + '\n[[OPTIONS: Short label | Other label]]')
        self.assertEqual(self.menu_ref(menu, '2. Other label').reason, 'quote-tap')

    def test_candidate_spacing_and_unicode_must_be_verbatim(self):
        for candidate, rendered in ((self.QUOTE, self.QUOTE.replace(' ', '  ', 1)),
                                    ('Caf\u00e9 helps users.', 'Cafe\u0301 helps users.')):
            with self.subTest(rendered=rendered):
                self.QUOTE = candidate
                self.assertEqual(self.menu_ref(self.numbered().replace(candidate, rendered)).reason,
                                 "quote-tap")

    def test_ambiguous_or_malformed_numbering_and_markers_refuse(self):
        menu = self.numbered()
        for bad in (menu.replace('2. "', '1. "'), menu.replace('2. "', '3. "'),
                    menu.replace('2. "Another goal."', ''),
                    menu.replace('Other label', 'Short label'),
                    menu + '\nUnexpected rider', '[[OPTIONS: Old | Menu]]\n' + menu,
                    menu.replace('[[OPTIONS: Short label | Other label]]',
                                 '[[OPTIONS]]\n1. Short label\n3. Other label'),
                    '```\n' + menu + '\n```'):
            with self.subTest(menu=bad):
                self.assertEqual(self.menu_ref(bad).reason, "quote-tap")

    def test_nearest_menu_survives_nonmenu_reply_and_owner_clarification(self):
        ref = self.menu_ref(self.numbered(), interlude=(
            ("owner", "Here is the definition of the term."),
            ("assistant", "The definition is recorded. Please pick from the earlier candidates.")))
        self.assertIsInstance(ref, wot.TurnRef)

    def test_nearer_different_or_malformed_menu_makes_old_pick_stale(self):
        for newer in ('1. "Different goal."\n2. "Another goal."\n[[OPTIONS: Short label | Other label]]',
                      'Unparseable [[OPTIONS]] menu'):
            with self.subTest(newer=newer):
                ref = self.menu_ref(self.numbered(), interlude=(("assistant", newer),))
                self.assertEqual(ref.reason, "quote-tap")

    def test_off_chain_menu_cannot_lend_candidate_authority(self):
        self.record("request", entry={"type": "assistant", "message": {
            "role": "assistant", "content": self.numbered()}}, ledger=False, on_chain=False)
        ref = self.menu_ref('No candidate.\n[[OPTIONS: Short label | Other label]]')
        self.assertEqual(ref.reason, "quote-tap")

    def test_generic_single_candidate_quote_affirms_only_first_slot(self):
        menu = '"' + self.QUOTE + '"\n\nUse this wording?\n[[OPTIONS: Use this wording | Change it]]'
        self.assertIsInstance(self.menu_ref(menu, "1. Use this wording"), wot.TurnRef)
        self.assertEqual(self.menu_ref(menu, "2. Change it").reason, "quote-tap")
        for bad in (menu.replace('"' + self.QUOTE + '"', 'Do not: ' + self.QUOTE),
                    menu.replace('"' + self.QUOTE + '"', '"Another goal."\n\n"' + self.QUOTE + '"'),
                    menu.replace('Use this wording?', 'Use this wording? Do not use it.'),
                    menu.replace('Use this wording?', 'A different question?'),
                    menu.replace('Change it]]', 'Change it | Third choice]]')):
            with self.subTest(menu=bad):
                self.assertEqual(self.menu_ref(bad, "1. Use this wording").reason, "quote-tap")

    def test_numeric_owner_reply_confirms_each_supported_spelling_and_audits(self):
        from why_net import events
        for n, reply in enumerate(("1", "1\ubc88", "1.")):
            with self.subTest(reply=reply):
                self.QUOTE = "Keep planning simple %d." % n
                ref = self.menu_ref(self.numbered("[[OPTIONS]]\n1. Short label\n2. Other label"),
                                    reply, source="message")
                self.assertIsInstance(ref, wot.TurnRef)
                wid = events.propose(self.QUOTE, "framework")["why"]
                event = events.confirm_quote(wid, reply, SESSION, ref.message_uuid)
                self.assertEqual(event["data"]["worded_by"], "agent")
                self.assertEqual(event["data"]["candidate"], self.QUOTE)
                self.assertEqual(events.audit_confirms(), [])

    def test_numeric_nonfirst_slot_and_nearest_ancestral_menu(self):
        menu = '1. Another goal.\n2. ' + self.QUOTE + '\n[[OPTIONS]]\n1. One\n2. Two'
        self.assertIsInstance(self.menu_ref(menu, "2.", source="message", interlude=(
            ("owner", "A clarification."), ("assistant", "Please use the earlier choices."))),
            wot.TurnRef)
        self.assertEqual(self.menu_ref(menu, "1", source="message").reason, "quote-tap")
        self.assertEqual(self.menu_ref(menu, "2", source="message", interlude=(
            ("assistant", "No candidates.\n[[OPTIONS: One | Two]]"),)).reason, "quote-tap")

    def test_numeric_invalid_forged_injected_voice_and_worker_still_refuse(self):
        from why_net import events, WhyRefusal
        for reply in ("0", "3", "-1", "01", "1 yes", "1\ubc88 please", "1.0"):
            self.assertEqual(self.menu_ref(self.numbered(), reply, source="message").reason, "quote-tap")
        for kwargs, reason in (({"ledger": False}, "turn-not-in-ledger"),
                               ({"injected": True}, "quote-not-owner-turn"),
                               ({"on_chain": False}, "turn-off-chain"),
                               ({"source": "voice"}, "quote-voice")):
            self.TAP = "1"
            uid = self.pick(menu=self.numbered(), **kwargs)
            ref = wot.resolve("1", SESSION, uid, allow_past=True, candidate=self.QUOTE)
            self.assertEqual(ref.reason, reason)
        # A numeric callback is not a typed owner pick, and cannot bypass
        # the bridge keyboard/index proof.
        self.assertEqual(self.menu_ref(self.numbered(), "1", source="callback").reason, "quote-tap")
        self.TAP = "1. Short label"
        uid = self.pick(menu=self.numbered(), source="message")
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "text-not-quote")
        self.TAP = "1"
        uid = self.pick(menu=self.numbered(), source="message")
        os.environ[wot.DISPATCH_ENV_MARKER] = "worker"
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "dispatch-worker")

    def test_persona_confirm_menu_restyled_labels_bind_only_yes(self):
        menu = "Is this your real aim, Captain? Aye!\n" + self.QUOTE + "\n[[OPTIONS: Aye | Try again]]"
        self.assertIsInstance(self.menu_ref(menu, "1. Aye", keyboard=["1. Aye", "2. Try again"],
                                            option_index=1), wot.TurnRef)
        for reply in ("2. Try again", "1. Wrong keyboard"):
            self.assertEqual(self.menu_ref(menu, reply).reason, "quote-tap")
        for bad in (menu.replace(self.QUOTE, self.QUOTE.replace("simple", "easy")),
                    menu.replace("Aye!", "Do not use it."),
                    menu.replace("Try again]]", "Try again | Skip]]"),
                    menu.replace(self.QUOTE, self.QUOTE + "\nExtra instructions.")):
            self.assertEqual(self.menu_ref(bad, "1. Aye").reason, "quote-tap")

    def styled_ask(self):
        from why_net import ask, events, store
        wid = events.propose(self.QUOTE, "framework", work="DGN-700")["why"]
        ctx = WB.Ctx(root=str(ROOT), now=self.now)
        known = events.nodes()
        shown, err = ask.build_slots(ctx, [wid], "confirm", known)
        self.assertIsNone(err)
        row = {"ask": "ask-7000", "ev": "create", "at": store.iso_at(self.now),
               "issued_ts": self.now, "kind": "why", "shape": "confirm", "trigger": "ripe",
               "channel": "inline", "topic": "DGN-700", "work_ref": "DGN-700",
               "session_id": SESSION, "shown": shown, "cand_texts": [self.QUOTE],
               "rendered": ask.render(ctx, "confirm", shown, known, "ripe")}
        store.append_row(store.ASKS, row)
        text = "Is this your real aim, Captain? Aye!\n" + self.QUOTE + "\n[[OPTIONS: Aye | Try again]]"
        self.record("request", ledger=False, entry={"type": "assistant", "message": {
            "role": "assistant", "content": text}})
        self.now += 1
        return wid, text

    def test_restyled_confirm_hook_preserves_slot_binding_and_keyboard_checks(self):
        from why_net import events, store
        wid, text = self.styled_ask()
        self.assertFalse(WU.ask_carried(text.replace(self.QUOTE, "Another candidate."),
                                      WB.fold_asks(store.read_rows(store.ASKS))["ask-7000"]))
        os.makedirs(self.data, exist_ok=True)
        Path(self.data, "options-bubbles.json").write_text(json.dumps([{
            "ts": self.now - 0.5, "message_id": 900, "chat_id": 4242,
            "keyboard": ["1. Aye", "2. Try again"]}]))
        self.submit("1. Aye", source="callback", message_id=900, chat_id=4242,
                    keyboard=["1. Aye", "2. Try again"], option_index=1)
        self.assertEqual(events.nodes()[wid]["status"], "confirmed-provisional")
        probe = store.read_rows(store.PROBES)[-1]
        self.assertEqual(probe["slots"], [1])
        self.assertEqual(probe["shown"][0]["label"], "Aye")

    def test_numeric_confirm_hook_answers_the_printed_persona_ask(self):
        from why_net import events
        wid, _ = self.styled_ask()
        self.submit("1\ubc88")
        self.assertEqual(events.nodes()[wid]["status"], "confirmed-provisional")

    def test_negative_confirm_keeps_wrong_reading_without_new_followup(self):
        from why_net import events, retro, store
        wid, _ = self.styled_ask()
        self.submit("2\ubc88")
        self.assertEqual(events.nodes()[wid]["status"], "hypothesis")
        wrong = [p for p in store.read_rows(store.PROBES) if "reading_wrong" in p]
        self.assertEqual(wrong[-1]["whys"], [wid])
        followups = [a for a in WB.fold_asks(store.read_rows(store.ASKS)).values()
                     if a.get("trigger") == "reading"]
        self.assertEqual(followups, [])  # FDGN-594: keep correction evidence only.
        asks = WB.fold_asks(store.read_rows(store.ASKS))
        self.assertEqual(asks["ask-7000"]["state"], "answered")
        self.assertEqual(WB.topic_check(WB.Ctx(now=self.now), "DGN-700", asks), (None, False))

    def test_past_selection_does_not_bypass_runtime_worker_or_voice_refusals(self):
        from why_net import events, WhyRefusal
        uid = self.pick(source="voice")
        wid = events.propose(self.QUOTE, "framework")["why"]
        with self.assertRaises(WhyRefusal) as r:
            self.confirm(wid, uid)
        self.assertEqual(r.exception.reason, "quote-voice")
        for key, value, reason in ((wot.DISPATCH_ENV_MARKER, "worker", "dispatch-worker"),
                                   ("CLAUDECODE", "0", "resolver-unavailable")):
            os.environ[key] = value
            with self.assertRaises(WhyRefusal) as r:
                self.confirm(wid, uid)
            self.assertEqual(r.exception.reason, reason)
            os.environ.pop(key)


class Floor(Base):
    def test_R1a_confirm_without_quote(self):
        w = self.propose(OWNER)
        self.assert_refused(["confirm", "--why", w, "--session", SESSION])

    def test_R1b_one_word_changed(self):
        w = self.propose(OWNER.replace("one", "two"))
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", OWNER.replace("one", "two")],
                            "quote-not-owner-turn")

    def test_R1c_assistant_turn(self):
        q = "ASSISTANT-ONLY: keep the board small"
        w = self.propose(q)
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", q], "quote-not-owner-turn")

    def test_R1d_recall_and_recap_and_machine_turns(self):
        for q in ("RECALL-ONLY: owner prefers short replies",
                  "RECAP-ONLY: last session tail",
                  "META-ONLY: caveat block",
                  "TOOLRESULT-ONLY: ok",
                  "TASKNOTE-ONLY: agent done"):
            w = self.propose(q)
            self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                                 "--quote", q], "quote-not-owner-turn")

    def test_R1f_attach_cycle(self):
        a = self.propose("root why")
        b = self.propose("child why", a)
        self.assert_refused(["attach", "--why", a, "--parent", b], "cycle")
        self.assert_refused(["attach", "--why", a, "--parent", a], "cycle")

    def test_R1h_injected_turn(self):
        q = "INJECTED-ONLY: dispatch result arrived"
        w = self.propose(q)
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", q], "quote-not-owner-turn")

    def test_R1i_stop_hook_feedback(self):
        q = "STOPHOOK-ONLY: footer missing"
        w = self.propose(q)
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", q], "quote-not-owner-turn")

    def test_R1j_dispatch_worker_env(self):
        w = self.propose(OWNER)
        env = dict(self.env, **{wot.DISPATCH_ENV_MARKER: "run-123"})
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", OWNER], "dispatch-worker", env=env)

    def test_R1k_codex_runtime(self):
        w = self.propose(OWNER)
        env = dict(self.env)
        env.pop("CLAUDECODE")
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", OWNER], "resolver-unavailable", env=env)
        # Missing transcript is the same fail-closed reason.
        os.remove(self.transcript)
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", OWNER], "resolver-unavailable")

    def test_R1l_seed_with_events(self):
        self.propose("anything")
        self.assert_refused(["seed", "--from", "worklog/_DECISIONS.md"],
                            "events-not-empty")

    def test_R1m_retire_rules(self):
        c = self.propose(OWNER)
        rc, out, _ = self.run_cli("confirm", "--why", c, "--session", SESSION,
                                  "--quote", OWNER)
        self.assertEqual(rc, 0, out)
        self.assert_refused(["retire", "--why", c], "quote-required")
        h = self.propose("agent hypothesis with owner words")
        # grill-3 fix 1: the probe answers an ask that showed this Why
        prb = self._probe("free text answer", [{"slot": 1, "why": h, "label": "x"}])
        rc, out, _ = self.run_cli("revise", "--why", h, "--text",
                                  "agent hypothesis, revised", "--probe", prb)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.node(h)["owner_words"], [prb])
        self.assert_refused(["retire", "--why", h], "quote-required")
        bare = self.propose("bare agent hypothesis")
        rc, out, _ = self.run_cli("retire", "--why", bare)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.node(bare)["status"], "retired")
        # A confirmed node retires with a resolvable owner quote.
        rc, out, _ = self.run_cli("retire", "--why", c, "--quote", OWNER2,
                                  "--session", SESSION)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.node(c)["status"], "retired")
        self.assertEqual(self.node(c)["text"], OWNER)  # text never deleted

    def test_P1_verbatim_owner_quote(self):
        w = self.propose(OWNER)
        rc, out, _ = self.run_cli("confirm", "--why", w, "--session", SESSION,
                                  "--quote", OWNER)
        self.assertEqual(rc, 0, out)
        n = self.node(w)
        # AMEND-1 5.1 P1: a quote makes the Why provisional, never confirmed;
        # the confirm event carries the otl of the quoted owner turn.
        self.assertEqual(n["status"], "confirmed-provisional")
        self.assertTrue(n["anchor"].startswith("otl-"), n["anchor"])
        ev = [json.loads(l) for l in Path(self.events).read_text().splitlines()]
        self.assertEqual(ev[-1]["data"]["otl"], n["anchor"])
        self.assertEqual(n["worded_by"], "owner")
        self.assertEqual(n["text"], OWNER)
        self.assertEqual(n["confirm"]["source"]["message_uuid"], "u-owner-1")
        self.assertEqual(n["confirm"]["source"]["kind"], "turn")
        self.assertEqual(n["said_at"], "2026-10-07T22:00:00Z")
        # text-blocks content resolves too; --uuid pins the entry.
        w2 = self.propose(OWNER2)
        self.assert_refused(["confirm", "--why", w2, "--session", SESSION,
                             "--quote", OWNER2, "--uuid", "u-owner-1"],
                            "quote-not-owner-turn")
        rc, out, _ = self.run_cli("confirm", "--why", w2, "--session", SESSION,
                                  "--quote", OWNER2, "--uuid", "u-owner-2")
        self.assertEqual(rc, 0, out)

    def test_confirm_quote_must_equal_text(self):
        w = self.propose("agent wording of the owner's aim")
        self.assert_refused(["confirm", "--why", w, "--session", SESSION,
                             "--quote", OWNER], "text-not-quote")

    def _probe(self, verbatim, shown, slots=(), verdict="free",
               inbound="message", uuid=None):
        from why_net import events as E
        row = E.write_probe({
            "ask": "ask-0001", "trigger": "fork", "shown": shown,
            "verdict": verdict, "slots": list(slots), "verbatim": verbatim,
            "source": {"session_id": SESSION, "message_uuid": uuid,
                       "inbound_source": inbound, "bubble_message_id": None}})
        return row["id"]

    def test_R1g_tap_confirm(self):
        from why_net import events as E
        w = self.propose("Owner wants one-tap decisions in the morning")
        other = self.propose("Owner wants fewer pings overall")
        shown = [{"slot": 1, "why": w, "label": TAP},
                 {"slot": 2, "why": other, "label": "B"}]
        prb = self._probe(TAP, shown, [1], "pick", "callback", "u-tap-1")
        # Double-tap redelivery dedupes to the same probe (GRILL h).
        self.assertEqual(self._probe(TAP, shown, [1], "pick", "callback",
                                     "u-tap-1"), prb)
        # The unpicked candidate cannot be confirmed from this tap.
        with self.assertRaises(Exception) as cm:
            E.confirm_tap(other, prb)
        self.assertEqual(cm.exception.reason, "why-not-picked")
        E.confirm_tap(w, prb)
        n = self.node(w)
        # AMEND-1 5.1 R1g: a tap makes the Why provisional; no settle.
        self.assertEqual(n["status"], "confirmed-provisional")
        self.assertIsNone(n["settle"])
        self.assertTrue(n["anchor"].startswith("otl-"))
        self.assertEqual(n["worded_by"], "agent")
        self.assertEqual(n["owner_words"], [prb])
        self.assertEqual(n["confirm"]["source"]["kind"], "tap")
        self.assertEqual(n["confirm"]["probe"], prb)
        self.assertEqual(n["confirm"]["source"]["message_uuid"], "u-tap-1")
        ev = [json.loads(l) for l in Path(self.events).read_text().splitlines()]
        self.assertEqual(ev[-1]["actor"], "ups-hook")

    def test_tap_confirm_needs_owner_turn(self):
        from why_net import events as E
        w = self.propose("x")
        shown = [{"slot": 1, "why": w, "label": "INJECTED-ONLY"}]
        for verbatim, reason in (
                ("[bridge:injected-turn]\nINJECTED-ONLY: dispatch result arrived",
                 "quote-not-owner-turn"),
                ("INJECTED-ONLY: dispatch result arrived", "turn-not-in-ledger")):
            prb = self._probe(verbatim, shown, [1], "pick", "callback")
            before = self.events_bytes()
            with self.assertRaises(Exception) as cm:
                E.confirm_tap(w, prb)
            self.assertEqual(cm.exception.reason, reason)
            self.assertEqual(self.events_bytes(), before)

    def test_interpret_row_never_edits_verbatim(self):
        w = self.propose("y")
        prb = self._probe("free answer", [])
        rc, out, _ = self.run_cli("interpret", prb, "--verdict", "closest",
                                  "--why", w, "--note", "near slot 1")
        self.assertEqual(rc, 0, out)
        rows = [json.loads(l) for l in Path(self.store_dir, "probes.jsonl")
                .read_text().splitlines()]
        self.assertEqual(rows[0]["verbatim"], "free answer")
        self.assertEqual(rows[1]["interp"], prb)
        self.assert_refused(["interpret", "prb-9999", "--verdict", "closest",
                             "--why", w], "unknown-probe")

    def test_p3_verbs_built(self):
        # head / lint-ticket were P1 stubs (exit 2 not-built); P3 wired them.
        rc, out, _ = self.run_cli("lint-ticket")
        self.assertEqual(rc, 2)  # no path, no --resolve: usage
        self.assertNotIn("reason=not-built", out)
        rc, out, _ = self.run_cli("head")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("reason=not-built", out)


def settle(wid):
    """Fold-level settle for head fixtures (the act pass is AMEND-1's job,
    tests/test-why-net-amend1.py)."""
    from why_net import events as E, store as S
    n = E.nodes()[wid]
    with S.locked():
        E.settle(wid, ["act-0001", "act-0002"], n["anchor_evt"])


class Head(Base):
    def test_L1_two_parent_paths_no_dup(self):
        from why_net import events as E, nodes as N
        root = self.propose(OWNER)
        self.run_cli("confirm", "--why", root, "--session", SESSION,
                     "--quote", OWNER)
        b1 = self.propose("mid one", root)
        b2 = self.propose("mid two", root)
        leaf = self.propose("leaf", b1, b2)
        self.propose("unrelated", root)
        for w in (b1, b2, leaf):  # confirm via tap probes
            prb = self._tap(w)
            E.confirm_tap(w, prb)
        for w in (root, b1, b2, leaf):  # AMEND-1: only a settle confirms
            settle(w)
        lines = N.head_lines(E.nodes(), [leaf])
        self.assertLessEqual(len(lines), 5)
        self.assertEqual(len(lines), len(set(lines)))
        self.assertEqual(lines[0], "- " + OWNER)  # root first, once
        self.assertEqual(lines[-1], "- leaf")
        self.assertIn("- mid one", lines)
        self.assertIn("- mid two", lines)
        self.assertNotIn("- unrelated", lines)
        self.assertEqual(N.head_lines(E.nodes(), []),
                         ["Why unresolved for current work"])

    def test_L1_provisional_line(self):
        # AMEND-1 5.1 L1/L2: the confirmed path, then at most 1 [im-si] line
        from why_net import events as E, nodes as N
        root = self.propose(OWNER)
        self.run_cli("confirm", "--why", root, "--session", SESSION, "--quote", OWNER)
        settle(root)
        p1, p2 = self.propose("prov near", root), self.propose("prov far", root)
        leaf = self.propose("leaf hyp", p1, p2)
        for w in (p1, p2):
            E.confirm_tap(w, self._tap(w))
        lines = N.head_lines(E.nodes(), [leaf])
        self.assertEqual(lines, ["- " + OWNER, N.PROV_TAG + " prov near",
                                 HYP + " leaf hyp"])

    def _tap(self, w):
        from why_net import events as E
        return E.write_probe({
            "ask": "ask-" + w, "trigger": "fork", "verdict": "pick",
            "slots": [1], "verbatim": TAP,
            "shown": [{"slot": 1, "why": w, "label": TAP}],
            "source": {"session_id": SESSION, "message_uuid": "u-tap-1",
                       "inbound_source": "callback"}})["id"]

    def test_L2_one_hypothesis_line(self):
        from why_net import events as E, nodes as N
        root = self.propose(OWNER)
        self.run_cli("confirm", "--why", root, "--session", SESSION,
                     "--quote", OWNER)
        settle(root)
        h1 = self.propose("hyp near", root)
        h2 = self.propose("hyp far", root)
        leaf = self.propose("leaf hyp", h1, h2)
        lines = N.head_lines(E.nodes(), [leaf])
        tagged = [l for l in lines if l.startswith(HYP + " ")]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0], HYP + " leaf hyp")
        self.assertIn("- " + OWNER, lines)
        self.assertLessEqual(len(lines), 5)


class Audit(Base):
    def test_S1_chain_break_detect_only(self):
        a = self.propose("one")
        self.propose("two", a)
        rc, out, _ = self.run_cli("derive", "--audit")
        self.assertEqual((rc, out.strip()), (0, "signal::WHY_AUDIT_OK"))
        # A hand-append (Bash redirection, GRILL i) breaks the chain.
        with open(self.events, "a") as fh:
            fh.write('{"ev":"confirm","why":"%s","actor":"agent",'
                     '"data":{},"prev":"forged"}\n' % a)
        rc, out, _ = self.run_cli("derive", "--audit")
        self.assertEqual(rc, 0)  # no refusal
        self.assertIn("signal::WHY_AUDIT_BREAK", out)
        self.assertIn("file=events.jsonl line=3 kind=prev-mismatch", out)


    def test_S2_audit_reproves_legit_confirms_from_cron_env(self):
        # Grill fix 5: the audit re-runs the resolver / probe checks.  A
        # nightly cron has no CLAUDE_PROJECT_DIR and another cwd; the
        # transcript path the hook recorded in the ledger is used instead.
        # Grill-2: only when it lies in THIS agent's project dir (live: the
        # instance root; here the cron's cwd stands in for it).
        from why_net import events as E
        w = self.propose(OWNER)
        rc, out, _ = self.run_cli("confirm", "--why", w, "--session", SESSION,
                                  "--quote", OWNER)
        self.assertEqual(rc, 0, out)
        t = self.propose("tap why")
        prb = E.write_probe({
            "ask": "ask-0001", "trigger": "fork", "verdict": "pick", "slots": [1],
            "verbatim": TAP, "shown": [{"slot": 1, "why": t, "label": TAP}],
            "source": {"session_id": SESSION, "inbound_source": "callback"}})["id"]
        E.confirm_tap(t, prb)
        cron = {k: v for k, v in self.env.items()
                if k not in ("CLAUDE_PROJECT_DIR", "CLAUDECODE",
                             "CLAUDE_CODE_SESSION_ID")}
        p = subprocess.run([sys.executable, str(CLI), "derive", "--audit"],
                           capture_output=True, text=True, env=cron, cwd=self.ws,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("signal::WHY_AUDIT_OK", p.stdout)
        # The same recorded path seen from another agent's dir is not used.
        p = subprocess.run([sys.executable, str(CLI), "derive", "--audit"],
                           capture_output=True, text=True, env=cron, cwd=self.tmp,
                           timeout=60)
        self.assertNotIn("signal::WHY_AUDIT_OK", p.stdout)


class Guard(unittest.TestCase):
    def deny(self, tool, path):
        p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(
            {"tool_name": tool, "tool_input": {"file_path": path}}),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        if not p.stdout.strip():
            return None
        return json.loads(p.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"]

    def test_R1e_why_net_store_denied(self):
        for tool in ("Edit", "Write", "MultiEdit"):
            for path in ("/x/ws/.telegram_bot/state/why-net/events.jsonl",
                         "/x/ws/.telegram_bot/state/why-net",
                         "C:\\ws\\.telegram_bot\\state\\why-net\\probes.jsonl"):
                msg = self.deny(tool, path)
                self.assertIsNotNone(msg, (tool, path))
                self.assertIn("Why net store", msg)
        self.assertIsNone(self.deny("Read",
                          "/x/.telegram_bot/state/why-net/events.jsonl"))
        self.assertIsNone(self.deny("Write",
                          "/x/.telegram_bot/state/why-network/a.json"))
        self.assertIsNone(self.deny("Write", "/x/.telegram_bot/state/a.json"))

    def test_transcripts_denied(self):
        # Grill fix 1: a hand-written transcript is a forged owner turn.
        for tool in ("Edit", "Write", "MultiEdit"):
            msg = self.deny(tool, "/h/.claude/projects/-forged/sess-x.jsonl")
            self.assertIn("transcript", msg)
        self.assertIsNone(self.deny("Write", "/h/.claude/projects/-x/notes.md"))
        self.assertIsNone(self.deny("Read", "/h/.claude/projects/-x/s.jsonl"))

    def test_existing_rows_unchanged(self):
        self.assertIn("Engine-owned memory",
                      self.deny("Write", "/x/ws/memories/a.md"))
        self.assertIn("Engine-owned memory", self.deny(
            "Edit", "/h/.claude/projects/-x-ws/memory/MEMORY.md"))
        self.assertIsNone(self.deny("Write", "/x/ws/files/memories.md"))
        self.assertIsNone(self.deny("Write", "/x/ws/files/a.md"))
        p = subprocess.run([sys.executable, str(GUARD)], input="not json",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


class MarkerParity(unittest.TestCase):
    def test_recap_markers_match_owner(self):
        recap = _load("_recap", ROUTINES / "session-recap.py")
        self.assertEqual(tuple(recap._INJECTION_MARKERS), wot.RECAP_MARKERS)
        self.assertEqual(tuple(recap._INJECTION_MARKERS),
                         wot._FALLBACK_RECAP_MARKERS)

    def test_noise_markers_match_owner(self):
        mem = _load("_mem", ROOT / "memory-engine" / "memory.py")
        self.assertEqual(tuple(mem.TRANSCRIPT_NOISE_MARKERS),
                         wot._FALLBACK_NOISE_MARKERS)
        for m in mem.TRANSCRIPT_NOISE_MARKERS:
            self.assertIn(m, wot.PREFIX_MARKERS)

    def test_bridge_injected_mark_parity(self):
        # The bridge is not imported (OSS, untouched); its literal is pinned.
        src = (ROOT / "bridge" / "sdk_bridge.py").read_text(encoding="utf-8")
        m = re.search(r'^INJECTED_TURN_MARK = "([^"]+)"', src, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), wot.BRIDGE_INJECTED_TURN_MARK)
        self.assertIn(wot.BRIDGE_INJECTED_TURN_MARK, wot.PREFIX_MARKERS)
        self.assertIn("Stop hook feedback:", wot.PREFIX_MARKERS)

    def test_extra_prefix_markers_are_real(self):
        # Each stricter-only extra must be a machine head some producer
        # really emits (a dead literal would hide a renamed producer).
        bridge = (ROOT / "bridge" / "sdk_bridge.py").read_text(encoding="utf-8")
        dispatch = (ROUTINES / "dispatch-detached.sh").read_text(encoding="utf-8")
        for m in wot.EXTRA_PREFIX_MARKERS:
            self.assertIn(m, wot.PREFIX_MARKERS)
            self.assertIn('"%s' % m, bridge)
            self.assertIn('f"%s' % m, dispatch)

    def test_bridge_wrapper_strings_parity(self):
        # grill-3 fix 7: the photo / document wrapper copies equal the
        # bridge's i18n catalogs (live import and fallback alike).
        for lang in ("en", "ko"):
            live = _load("_i18n_" + lang, ROOT / "bridge" / "i18n" / (lang + ".py")).STRINGS
            for k in wot.WRAPPER_KEYS + (wot.CAPTION_KEY, wot.RESUME_KEY):
                self.assertEqual(wot._FALLBACK_WRAPPER_STRINGS[lang][k], live[k], (lang, k))
        self.assertEqual(len(wot.WRAPPER_HEADS), 6)
        self.assertEqual(len(wot.CAPTION_LABELS), 2)

    def test_dispatch_marker_is_exported(self):
        # V-9: the exact variable dispatch-detached.sh exports to workers.
        src = (ROUTINES / "dispatch-detached.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            src.count(wot.DISPATCH_ENV_MARKER + '="$RUNID"'), 2)


class SharedHelpers(unittest.TestCase):
    """DGN-1967 step 4: one copy of the small text / time helpers, in
    why_net.store."""

    def setUp(self):
        from why_net import store
        self.store = store
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="why-net-helpers-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_copies_are_the_store_helpers(self):
        from why_net import acts, events, surfaces
        for mod in (acts, events, WU):
            self.assertFalse(hasattr(mod, "_nfc"), mod)
        self.assertFalse(hasattr(surfaces, "now_iso"))
        self.assertFalse(hasattr(wot, "QUESTION_ENDINGS"))
        self.assertEqual(wot._nfc("e\u0301"), "\u00e9")
        self.assertEqual(self.store.nfc(None), "")
        self.assertEqual(wot.question_verdict("x", "\ud560\uae4c"),
                         "quote-question")

    def test_parse(self):
        st = self.store
        self.assertEqual(st.parse_iso("1970-01-01T00:00:10Z"), 10.0)
        self.assertEqual(st.parse_iso("1970-01-01T09:00:10+09:00"), 10.0)
        self.assertIsNone(st.parse_iso(""))
        self.assertIsNone(st.parse_iso(None))
        self.assertIsNone(st.parse_iso("yesterday"))
        self.assertIsNotNone(st.parse_dt("2026-01-01T10:00:00").tzinfo)

    def test_atomic_write_modes(self):
        st = self.store
        priv = os.path.join(self.tmp, "p", "a.json")
        st.atomic_write(priv, "x")
        self.assertEqual(os.stat(priv).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(priv)).st_mode & 0o777, 0o700)
        mask = os.umask(0o022)
        try:
            pub = os.path.join(self.tmp, "b.md")
            st.atomic_write(pub, "y", private=False)
        finally:
            os.umask(mask)
        self.assertEqual(os.stat(pub).st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["b.md", "p"])

    def test_jsonl_iter(self):
        p = os.path.join(self.tmp, "t.jsonl")
        with open(p, "w") as fh:
            fh.write('{"a": 1}\nnot json\n[1]\n{"b": 2}\n')
        self.assertEqual(list(self.store.jsonl_iter(p)), [{"a": 1}, {"b": 2}])

    def test_full_width_question_is_not_a_short_imperative(self):
        # ups.update_ring read only an ASCII "?" as a question.
        class Ctx(object):
            now = 1000.0
        old = os.environ.get("DOGANY_BOT_DATA_DIR")
        os.environ["DOGANY_BOT_DATA_DIR"] = self.tmp
        try:
            WU.update_ring(Ctx(), "s", "\ub410\uc5b4\uff1f", False, "text")
            WU.update_ring(Ctx(), "s", "\ub410\uc5b4", False, "text")
            rows = self.store.read_rows(self.store.OWNER_TURNS)
        finally:
            if old is None:
                os.environ.pop("DOGANY_BOT_DATA_DIR")
            else:
                os.environ["DOGANY_BOT_DATA_DIR"] = old
        self.assertEqual([WB.SHORT_IMPERATIVE in r["rush_words"] for r in rows],
                         [False, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
