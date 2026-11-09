#!/usr/bin/env python3
# test-lane: slow    # 40 hook/baseline subprocess pairs; measured 5.269s before, 10.068s after
# test-tier: 2
# test-component: why-net elicitation and ask copy
# test-covers: agents/.template/routines/lib/why_net/ask.py agents/.template/routines/lib/why_net/budget.py agents/.template/config/i18n/ko.json agents/.template/config/i18n/en.json
# promise: non-confirm asks retain their original stems; confirm asks keep the candidate on its own line
# promise: an already-picked Why renders dated transcription wording and the add-to-ledger label
"""v2.7 T1 P2 -- Why net elicitation loop (docs/design/v27-t1-why-net-LOCKED.md).

Fixture ids (spec section 7): tap shapes T1a-T1i, elicitation E1-E17
(E12 needs the P1 `head` verb + P3 session-recap and is not covered here),
plus the label-width parity pin against bridge/options.py and the `ups`
p95 < 150 ms latency gate (CPU work always; wall when the host is quiet). Synthetic data only (tests/fixtures/why-net/).

Ported onto P1's real store / events / resolver API (grill fix 4).  Grill
fix 3 changes two tap fixtures on purpose: a number handle ("N<beon>") is a
pick only on a morning bubble matched by its message id (T1c); on an inline
Why ask a typed or tapped number is free text (T1c inline, T1d), because the
width gate keeps a Why keyboard from degrading (V-7) and a bare number may
answer another [[OPTIONS]] list.  The attacks themselves are replayed in
test-why-net-grill.py.
"""
import ast
import io
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUTINES = HERE.parent
TEMPLATE = ROUTINES.parent
FIX = HERE / "fixtures" / "why-net"
sys.path.insert(0, str(ROUTINES / "lib"))
sys.path.insert(0, str(ROUTINES))

from why_net import ask, budget, derive, events, morning, store, ups  # noqa: E402
from legacy_why_fixtures import historical_queue
import why_owner_turn  # noqa: E402
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("why_net_cli", ROUTINES / "why-net.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# 2026-10-08 10:00 KST
T0 = 1791421200.0
MIN = 60
SID = "sess-aaaa"
CHAT = 4242  # the owner chat the bridge publishes as inbound chat_id
CLEAR_ENV = ("DOGANY_BOT_DATA_DIR", "DOGANY_AGENT_CONF", "CLAUDE_SESSION_ID",
             "CLAUDE_CODE_SESSION_ID", "DOGANY_DISPATCH_WORKER", "CLAUDE_CONFIG_DIR",
             "CLAUDE_PROJECT_DIR")


_OPTS = re.compile(r"\[\[OPTIONS:(.*?)\]\]", re.S)


def tap_keyboard(prompt, labels):
    """What the bridge publishes for a tap (bot._tap_binding): the keyboard
    as rendered -- bare labels, "N. label", or degraded "N<beon>" handles,
    whichever form the tapped text has -- and the tapped 1-based index."""
    if re.match(r"^\d+\. ", prompt):
        kb = ["%d. %s" % (i, l) for i, l in enumerate(labels, 1)]
    elif re.match("^\\d+\ubc88$", prompt):
        kb = ["%d\ubc88" % i for i in range(1, len(labels) + 1)]
    else:
        kb = list(labels)
    return {"keyboard": kb, "option_index": kb.index(prompt) + 1 if prompt in kb else None,
            "callback_data": "opt:x", "callback_query_id": "q-x"}


def keyboard_labels(transcript, store_rows, message_id):
    """The labels of the keyboard a tap came from: the morning bubble with
    that message id, else the last OPTIONS list of the last reply."""
    shown_ids = [r.get("ask") for r in store_rows if r.get("bubble_message_id") == message_id
                 and r.get("ev") == "shown"]
    for r in store_rows:   # a retro bubble was sent with its stop button
        if r.get("ev") == "shown" and r.get("ask") in shown_ids and r.get("retro_shown"):
            return [s["label"] for s in r["retro_shown"]]
    for r in store_rows:
        if r.get("ev") == "create" and r.get("ask") in shown_ids:
            return [s["label"] for s in r.get("shown") or []]
    last = None
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                e = json.loads(line)
                if e.get("type") == "assistant":
                    for c in (e.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            for m in _OPTS.finditer(c["text"]):
                                last = [x.strip() for x in m.group(1).split("|")]
    except FileNotFoundError:
        pass
    return last or []


def iso(ts):
    return store.iso_at(ts)


class Base(unittest.TestCase):
    """Live-shaped turns: every owner turn publishes a fresh bridge
    inbound-context (unique request_id) and runs `ups` with the session's
    exact transcript path; the turn is then appended to that transcript,
    and every passing ask's text is appended as the agent's reply."""

    def setUp(self):
        keys = set(CLEAR_ENV + ("CLAUDECODE",)) | {
            k for k in os.environ if k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
        self._saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp(prefix="why-net-")
        self.root = self.tmp
        os.makedirs(os.path.join(self.root, "config"))
        shutil.copytree(TEMPLATE / "config" / "i18n", os.path.join(self.root, "config", "i18n"))
        self.write_conf("")
        self.cfg = os.path.join(self.tmp, "claude-config")
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(self.root, ".telegram_bot")
        os.environ["CLAUDECODE"] = "1"
        self.ctx = budget.Ctx(root=self.root, now=T0)
        self.ddir = self.ctx.ddir
        os.makedirs(self.ddir, exist_ok=True)
        self.rid = 0
        self.bubble_seq = 9000
        self.last_bubble = None  # the newest [[OPTIONS]] bubble the bridge sent

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_conf(self, extra):
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("AGENT_LANG=ko\nWHY_NET=on\n" + extra)

    def at(self, ts):
        self.ctx._now = ts

    # -- helpers ----------------------------------------------------------
    def transcript(self, session=SID):
        enc = "".join(c if c.isalnum() else "-" for c in self.root)
        d = Path(self.cfg) / "projects" / enc
        d.mkdir(parents=True, exist_ok=True)
        return d / (session + ".jsonl")

    def _entry(self, session, entry):
        """Claude Code appends: each entry links to the one before it
        (parentUuid main chain, grill-2 fixes 1-3)."""
        entry.setdefault("timestamp", iso(self.ctx.now))
        self.rid += 1
        entry.setdefault("uuid", "e-%d" % self.rid)
        entry.setdefault("parentUuid", self._last_uuid(session))
        with open(self.transcript(session), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _last_uuid(self, session):
        try:
            with open(self.transcript(session), encoding="utf-8") as fh:
                lines = [l for l in fh if l.strip()]
        except FileNotFoundError:
            return None
        return json.loads(lines[-1]).get("uuid") if lines else None

    def reply(self, text, session=SID):
        """The agent's reply; an [[OPTIONS]] keyboard rides a bubble the
        bridge records (bridge/sdk_bridge.publish_options_bubble)."""
        self._entry(session, {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}})
        lists = [[x.strip() for x in m.group(1).split("|")] for m in _OPTS.finditer(text)]
        if lists:
            self.bubble_seq += 1
            p = os.path.join(self.ctx.bot_dir, "options-bubbles.json")
            try:
                with open(p, encoding="utf-8") as fh:
                    rows = json.load(fh)
            except FileNotFoundError:
                rows = []
            rows.append({"chat_id": CHAT, "message_id": self.bubble_seq,
                         "keyboard": lists[-1], "ts": self.ctx.now})
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            self.last_bubble = self.bubble_seq

    def propose(self, text):
        return events.propose(text, "framework")["why"]

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv), ctx=self.ctx)
        return rc, buf.getvalue()

    def assert_retired(self, *argv):
        from legacy_why_fixtures import store_bytes
        before = store_bytes(self.ctx)
        rc, output = self.cli(*argv)
        self.assertEqual(rc, 3, output)
        self.assertIn("retired", output)
        self.assertNotIn("[[OPTIONS:", output)
        self.assertEqual(store_bytes(self.ctx), before)

    def historical_cli(self, *argv):
        from legacy_why_fixtures import historical_inline, store_bytes
        before = store_bytes(self.ctx)
        rc, output = self.cli(*argv)
        self.assertEqual((rc, store_bytes(self.ctx)), (3, before), output)
        self.assertIn("retired-use-four-slot-intake", output)
        return historical_inline(self.ctx, argv)

    def ups(self, prompt, source="message", message_id=None, session=SID, **extra):
        self.rid += 1
        if source == "callback" and message_id is None:
            message_id = self.last_bubble  # a tap on the newest bubble
        if source == "callback" and "keyboard" not in extra:
            extra = dict(tap_keyboard(prompt, keyboard_labels(
                self.transcript(session), store.read_rows(store.ASKS), message_id)), **extra)
        if source == "callback" and "bubble_text_sha" not in extra:
            for r in store.read_rows(store.ASKS):  # an unedited morning bubble
                if r.get("bubble_message_id") == message_id and r.get("bubble_text_sha"):
                    extra = dict(extra, bubble_text_sha=r["bubble_text_sha"])
        with open(os.path.join(self.ctx.bot_dir, "inbound-context.json"), "w") as fh:
            json.dump(dict({"schema": 1, "request_id": "req-%d" % self.rid,
                            "source": source, "message_id": message_id, "chat_id": CHAT,
                            "text_sha": why_owner_turn.turn_sha(prompt),
                            "ts": self.ctx.now}, **extra), fh)
        ups.run(self.ctx, {"prompt": prompt, "session_id": session,
                           "transcript_path": str(self.transcript(session))})
        self._entry(session, {"type": "user", "userType": "external",
                              "uuid": "u-%d" % self.rid,
                              "message": {"role": "user", "content": prompt}})

    def owner_turn(self, text="\uc9c4\ud589\ud574\uc918 \uc774\uac70 \uba3c\uc800", session=SID):
        self.ups(text, session=session)

    def asks(self):
        return budget.fold_asks(store.read_rows(store.ASKS))

    def probes(self):
        return [p for p in store.read_rows(store.PROBES) if "id" in p]

    def node(self, wid):
        return events.nodes()[wid]

    def historical_inline(self, cands, trigger="fork", shape="contrast", topic="DGN-1001",
                   session=SID, send=True):
        args = ["ask", "--trigger", trigger, "--topic", topic, "--shape", shape]
        for c in cands:
            args += ["--cand", c]
        rc, out = self.historical_cli(*args)
        if rc == 0 and send:
            self.reply(out, session)  # the agent sends the printed ask
        return rc, out

    def show_morning(self, aid, mid, at=None):
        store.append_row(store.ASKS, {"ask": aid, "ev": "shown",
                                      "at": iso(T0 if at is None else at),
                                      "channel": "morning", "bubble_message_id": mid,
                                      "bubble_chat_id": CHAT,
                                      "bubble_text_sha": store.bubble_text_sha("bubble %s" % mid)})


class AskCopy(Base):
    def test_every_shape_and_trigger_preserves_original_stem(self):
        wid = self.propose("Use the chosen direction.\nKeep the purpose visible.")
        known = events.nodes()
        cases = [(shape, "before-work") for shape in ask.SHAPES if shape != "reveal"]
        cases += [("confirm", trigger) for trigger in ask.ENQUEUE_TRIGGERS + ("turn",)]
        for shape, trigger in cases:
            shown = [{"slot": 1, "why": wid, "side": "said", "label": "Choose"},
                     {"slot": 2, "why": wid, "side": "acted", "label": "Other"}]
            with self.subTest(shape=shape, trigger=trigger):
                text = ask.render(self.ctx, shape, shown, known, trigger,
                                  {"work_ref": "DGN-1001"})
                if shape == "confirm" and trigger not in ("turn", "reading"):
                    self.assertEqual("\n".join(text.splitlines()[1:-1]), known[wid]["text"])
                else:
                    stem = ask.t(self.ctx, ask.stem_key(shape, trigger))
                    stem = stem.replace("{text}", known[wid]["text"]) \
                        .replace("{said}", known[wid]["text"]) \
                        .replace("{acted}", known[wid]["text"]).replace("{signal}", "")
                    self.assertTrue(text.startswith(stem + "\n"), text)
                self.assertNotIn("DGN-1001", text)
                for field in ("{text}", "{said}", "{acted}"):
                    self.assertNotIn(field, text)
        text = ask.render(self.ctx, "confirm", shown, known, "turn",
                          {"target": "devkit/product-direction"})
        self.assertTrue(text.startswith(ask.t(self.ctx, "why.ask.stem.turn") + "\n"))
        self.assertNotIn("devkit/product-direction", text)

    def test_confirm_omits_context_prefix_and_preserves_candidate_line(self):
        self.write_conf("AGENT_LANG=ko\n")
        local = Path(self.root) / "config" / "i18n.local"
        local.mkdir()
        (local / "ko.json").write_text(json.dumps({
            "why.ask.stem.confirm": "\uc774 \uc774\uc720\uac00 \ub9de\ub098\uc694? {text}"}))
        wid = self.propose("A concrete purpose.")
        self.owner_turn("Start the concrete work.")
        rc, text = self.historical_inline([wid], trigger="ripe", shape="confirm", topic="dec-489")
        self.assertEqual(rc, 0, text)
        first = text.splitlines()[0]
        self.assertEqual(first, ask.t(self.ctx, "why.ask.stem.confirm_no_address").splitlines()[0])
        self.assertEqual(text.splitlines()[1], "A concrete purpose.")
        self.assertEqual(next(iter(self.asks().values()))["rendered"], text.strip())

    def test_transcription_has_pick_date_subject_sentence_and_add_label(self):
        self.write_conf("AGENT_LANG=ko\n")
        wid = self.propose("A concrete purpose.")
        known = events.nodes()
        known[wid]["confirm"] = {"said_at": "2026-10-18T13:16:48Z"}
        known[wid]["status"] = "confirmed-provisional"
        shown, err = ask.build_slots(self.ctx, [wid], "confirm", known)
        self.assertIsNone(err)
        text = ask.render(self.ctx, "confirm", shown, known, "ripe")
        self.assertEqual(text.splitlines()[0], ask.t(self.ctx, "why.ask.transcribe").format(
            date="10/18", subject=ask.t(self.ctx, "why.ask.subject.framework"),
            text=known[wid]["text"]))
        self.assertEqual(shown[0]["label"], ask.t(self.ctx, "why.ask.transcribe_yes"))
        self.assertIn("[[OPTIONS: " + shown[0]["label"], text)
        self.assertNotIn("?", text)
        self.assertEqual(len(text.splitlines()), 2)
        known[wid]["confirm"]["said_at"] = None
        undated = ask.render(self.ctx, "confirm", shown, known, "ripe")
        self.assertEqual(undated.splitlines()[0], ask.t(self.ctx, "why.ask.transcribe.undated").format(
            subject=ask.t(self.ctx, "why.ask.subject.framework"), text=known[wid]["text"]))
        known[wid]["status"] = "contested"
        shown, err = ask.build_slots(self.ctx, [wid], "confirm", known)
        self.assertIsNone(err)
        self.assertEqual(shown[0]["label"], ask.t(self.ctx, "why.ask.confirm_yes"))
        contested = ask.render(self.ctx, "confirm", shown, known, "contest")
        self.assertEqual(contested.splitlines()[1], known[wid]["text"])
        self.assertNotIn(ask.t(self.ctx, "why.ask.transcribe_yes"), contested)


class TapShapes(Base):
    def test_T1a_unnumbered_label_tap_confirms(self):
        # also R1g / E3: tap = owner turn, worded_by stays agent
        w1 = self.propose("\uc804\ubd80 \ucd94\ucc9c\ub300\ub85c (\ucd94\ucc9c)")
        w2 = self.propose("\ud558\ub098\uc529 \uc9c1\uc811 \uace0\ub974\uae30")
        self.owner_turn()
        self.at(T0 + 10)
        rc, out = self.historical_inline([w1, w2])
        self.assertEqual(rc, 0, out)
        self.at(T0 + 40)
        self.ups("\uc804\ubd80 \ucd94\ucc9c\ub300\ub85c (\ucd94\ucc9c)", source="callback")
        [p] = self.probes()
        # AMEND-1 rev 3: slot 1 of a 2-Why ask is "both"
        self.assertEqual((p["verdict"], p["slots"], p["outcome"]), ("pick", [2], "answered"))
        self.assertEqual(p["source"]["inbound_source"], "callback")
        n = self.node(w1)
        # AMEND-1 5.1 R1g/E3: a tap makes the Why provisional; the probe
        # carries the otl of its turn and the confirm anchors there
        self.assertEqual(n["status"], "confirmed-provisional")
        self.assertTrue(p["otl"].startswith("otl-"))
        self.assertEqual(n["anchor"], p["otl"])
        self.assertEqual(n["worded_by"], "agent")
        self.assertEqual(n["owner_words"], [p["id"]])
        self.assertEqual(n["confirm"]["source"]["kind"], "tap")
        self.assertEqual(self.node(w2)["status"], "hypothesis")

    def _two(self):
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        self.owner_turn()
        self.at(T0 + 10)
        self.assertEqual(self.historical_inline([w1, w2])[0], 0)
        self.at(T0 + 40)
        return w1, w2

    def test_T1b_numbered_label(self):
        _, w2 = self._two()
        self.ups("3. B\ub77c\ubca8", source="callback")   # rev 3: "both" is 1.
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["slots"]), ("pick", [3]))
        self.assertEqual(self.node(w2)["status"], "confirmed-provisional")

    def test_T1c_degraded_number_handle(self):
        # A degraded "N<beon>" keyboard is resolved by slot only on a morning
        # bubble matched by its message id (grill fix 3); an inline Why ask
        # passed the width gate, so a number there is not a pick.
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        aid, _ = historical_queue(self.ctx, "why", "new-work", "DGN-55", [w1, w2])
        self.show_morning(aid, 900)
        self.owner_turn()
        self.at(T0 + 40)
        self.ups("3\ubc88", source="callback", message_id=900)   # rev 3: "both" is 1
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["slots"], p["ask"]), ("pick", [3], aid))
        self.assertEqual(self.node(w2)["status"], "confirmed-provisional")

    def test_T1c_inline_number_is_not_a_pick(self):
        w1, w2 = self._two()
        self.ups("2\ubc88", source="callback")
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["slots"]), ("free", []))
        self.assertEqual(self.node(w2)["status"], "hypothesis")

    def test_T1d_typed_number_message_source(self):
        # grill fix 3 (B2): a typed number may answer another [[OPTIONS]]
        # list in the same reply -- free, never a pick of the Why ask.
        w1, _ = self._two()
        self.ups("1\ubc88", source="message", message_id=10)
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["slots"]), ("free", []))
        self.assertEqual(p["source"]["inbound_source"], "message")
        self.assertEqual(self.node(w1)["status"], "hypothesis")

    def test_T1e_free_text_no_node_change(self):
        w1, w2 = self._two()
        text = "\uc74c \ub458 \ub2e4 \uc544\ub2c8\uace0  \uc0ac\uc2e4\uc740 \ube44\uc6a9 \ub54c\ubb38\uc774\uc57c\n"
        self.ups(text, source="message", message_id=10)
        [p] = self.probes()
        self.assertEqual(p["verdict"], "free")
        self.assertEqual(p["verbatim"].encode("utf-8"), text.encode("utf-8"))
        self.assertEqual(self.node(w1)["status"], "hypothesis")
        self.assertEqual(self.node(w2)["status"], "hypothesis")

    def test_T1e2_push_then_reply_reads_the_owner_words_only(self):
        # DGN-1967 bug 2: the bridge prefixes the owner's turn with the
        # pushes sent since the last turn; those are the agent's words.
        block = ("[bridge:outbound-context]\n--- sent 10:00 ---\n"
                 "\uc790\uafb8 \ud0c0\uc784\uc544\uc6c3\n"
                 "[/bridge:outbound-context]\n")
        _, w2 = self._two()
        self.ups(block + "B\ub77c\ubca8", source="message", message_id=10)
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["slots"]), ("pick", [3]))
        self.assertEqual(p["verbatim"], "B\ub77c\ubca8")
        self.assertEqual(self.node(w2)["status"], "confirmed-provisional")

    def test_T1e3_push_correction_cue_is_not_the_owners(self):
        # DGN-1967 bug 2: a correction cue in the pushed text ("no, not
        # that ...") is the agent's words; the owner's reply "ok, go on"
        # corrects nothing.  The cue is a strong one: since bug 19 only a
        # strong cue records live (a weak one waits for the nightly pass).
        cue = ("\uc544\ub2c8 \uadf8\uac8c \uc544\ub2c8\ub77c \ub85c\uc9c1\uc744 "
               "\uace0\uccd0\uc57c \ud55c\ub2e4\ub294 \uac70\uc57c")
        block = ("[bridge:outbound-context]\n--- sent 10:00 ---\n"
                 + cue + "\n[/bridge:outbound-context]\n")
        self.ups(block + "\uc88b\uc544 \uc9c4\ud589\ud574", source="message")
        self.assertEqual(store.read_rows(store.CORRECTIONS), [])
        self.at(T0 + 40)
        self.ups(block + cue, source="message")
        [rec] = store.read_rows(store.CORRECTIONS)
        self.assertEqual(rec["text"], cue)

    def test_T1f_no_eligible_ask_two_turns_later(self):
        self._two()
        self.ups("\uc624\ub298 \ub0a0\uc528 \uc5b4\ub54c", source="message", message_id=10)   # turn 1 (free)
        self.at(T0 + 100)
        self.ups("1\ubc88", source="message", message_id=11)            # turn 2
        self.assertEqual(len(self.probes()), 1)
        # a fresh session turn with no ask shown since its previous turn
        self.at(T0 + 200)
        self.ups("1\ubc88", source="message", message_id=12)
        self.assertEqual(len(self.probes()), 1)

    def test_T1g_escape_is_defer(self):
        w1, _ = self._two()
        self.ups("\uc9c0\uae08\uc740 \ud328\uc2a4", source="callback")
        [p] = self.probes()
        self.assertEqual((p["verdict"], p["outcome"]), ("defer", "declined"))
        self.assertEqual(self.node(w1)["status"], "hypothesis")
        self.assertEqual(list(self.asks().values())[0]["state"], "declined")

    def test_T1h_label_width_refused(self):
        w1 = self.propose("\uc774 \uc791\uc5c5\uc740 \uc7a5\uae30\uc801\uc73c\ub85c \uc6b4\uc601 \ube44\uc6a9\uc744 \uc904\uc774\uae30 \uc704\ud55c \uac83")
        w2 = self.propose("\uc9e7\uc740 \uc774\uc720")
        self.owner_turn()
        rc, out = self.historical_inline([w1, w2])
        self.assertEqual(rc, 3)
        self.assertIn("signal::WHY_REFUSED", out)
        self.assertIn("reason=label-width", out)
        self.assertEqual(store.read_rows(store.ASKS), [])

    def test_T1i_morning_bubble_wins_over_open_inline(self):
        wm = self.propose("\uc544\uce68 \uc9c8\ubb38 \uc774\uc720")
        aid, _ = historical_queue(self.ctx, "why", "ripe", wm, [wm], shape="confirm")
        self.show_morning(aid, 777)
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        self.owner_turn()
        self.at(T0 + 10)
        self.assertEqual(self.historical_inline([w1, w2])[0], 0)
        self.at(T0 + 40)
        self.ups(ask.t(self.ctx, "why.ask.confirm_yes"), source="callback", message_id=777)
        [p] = self.probes()
        self.assertEqual(p["ask"], aid)
        self.assertEqual(p["source"]["bubble_message_id"], 777)
        self.assertEqual(self.node(wm)["status"], "confirmed-provisional")
        self.assertEqual(self.node(w1)["status"], "hypothesis")
        # GRILL (g)/(h): a later tap on the closed bubble never lands on the
        # inline ask opened after the first tap
        self.at(T0 + 45)
        self.historical_cli("ask", "--reveal", "--topic", "DGN-11", "--opt", "x=" + w1, "--opt", "y=" + w2)
        self.at(T0 + 50)
        self.ups(ask.t(self.ctx, "why.ask.confirm_yes"), source="callback", message_id=777)
        self.assertEqual(len(self.probes()), 1)


class Elicitation(Base):
    def test_E1_ticket_without_why_queues_once(self):
        # stub lint call: lint-ticket (P3) enqueues new-work for a ticket with no why
        # (a) v4 section 2: repeated ticket events never enqueue.
        for _ in range(2):
            self.assert_retired("enqueue", "--kind", "why", "--trigger", "new-work", "--topic", "DGN-9001")
        self.assertEqual(self.asks(), {})

    def test_E2_fork_render_and_no_fork(self):
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        self.owner_turn()
        rc, out = self.historical_inline([w1, w2])
        self.assertEqual(rc, 0)
        lines = out.strip().split("\n")
        # AMEND-1 rev 3: two Whys -> stem 5f (its own free-text line, no
        # hint) and "both" as the first button
        self.assertEqual(lines[0], ask.t(self.ctx, "why.ask.stem.mixed"))
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], ask.t(self.ctx, "why.ask.gloss.why"))
        self.assertEqual(lines[2], "[[OPTIONS: \ub458 \ub2e4 | A\ub77c\ubca8 | B\ub77c\ubca8 | \uc9c0\uae08\uc740 \ud328\uc2a4]]")
        for bad in ("why-", "ask-", "prb-", "signal::"):
            self.assertNotIn(bad, out)
        rc, out = self.cli("ask", "--trigger", "fork", "--topic", "DGN-1002", "--shape", "contrast", "--cand", w1)
        self.assertEqual(rc, 3)
        self.assertIn("reason=retired", out)

    def test_E4_confirm_without_owner_turn_refused(self):
        w = self.propose("\ucd9c\uc2dc \uc804\uc5d0 \uc548\uc815\uc131\uc744 \uba3c\uc800 \ud655\ubcf4\ud558\ub824\ub294 \uac70\uc8e0")
        rc, out = self.cli("confirm", "--why", w, "--quote", "\ucd9c\uc2dc \uc804\uc5d0 \uc548\uc815\uc131\uc744 \uba3c\uc800 \ud655\ubcf4\ud558\ub824\ub294 \uac70\uc8e0")
        self.assertEqual(rc, 3)
        self.owner_turn("\uc624\ub298 \ubb50\ubd80\ud130 \ud560\uae4c?", session="s-e4")
        shutil.copy(FIX / "transcript-e4-assistant-only.jsonl", self.transcript("s-e4"))
        rc, out = self.cli("confirm", "--why", w, "--quote", "\ucd9c\uc2dc \uc804\uc5d0 \uc548\uc815\uc131\uc744 \uba3c\uc800 \ud655\ubcf4\ud558\ub824\ub294 \uac70\uc8e0",
                           "--session", "s-e4")
        self.assertEqual(rc, 3)
        self.assertIn("reason=quote-not-owner-turn", out)
        self.assertEqual(self.node(w)["status"], "hypothesis")

    def _spaced_inline(self, n, topic_fmt="DGN-20%02d"):
        rcs = []
        for i in range(n):
            self.at(T0 + i * 31 * MIN)
            self.owner_turn()
            self.at(T0 + i * 31 * MIN + 5)
            w1, w2 = self.propose("A%d" % i), self.propose("B%d" % i)
            rcs.append(self.historical_inline([w1, w2], topic=topic_fmt % i))
        return rcs

    def test_E5_fourth_inline_ask_queued(self):
        # (a) v4 section 2: attention budgets cannot recreate independent asks.
        w1, w2 = self.propose("A"), self.propose("B")
        for i in range(4):
            self.at(T0 + i * 31 * MIN)
            self.owner_turn()
            self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-%d" % (2000 + i), "--shape", "contrast", "--cand", w1, "--cand", w2)
        self.assertEqual(self.asks(), {})

    def test_E6_rush(self):
        # (a) v4 section 2: neither rush configuration restores an ask path.
        w1, w2 = self.propose("A"), self.propose("B")
        for mode in ("enforce", "log"):
            self.write_conf("WHY_RUSH_MODE=" + mode + "\n")
            self.owner_turn("go")
            self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-1003", "--shape", "contrast", "--cand", w1, "--cand", w2)

    def test_E6b_urgent_and_dispatch_return(self):
        # (a) v4 D2/D4: urgency/worker returns grant no legacy question privilege.
        w1, w2 = self.propose("A"), self.propose("B")
        self.owner_turn()
        self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-1", "--shape", "contrast", "--cand", w1, "--cand", w2, "--urgent")
        Path(self.ctx.bot_dir, "dispatch-return-context.json").write_text("{}")
        self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-2", "--shape", "contrast", "--cand", w1, "--cand", w2)

    def test_E7_topic_ignored_twice_parked(self):
        w1, w2 = self.propose("A"), self.propose("B")
        # one session per day: the ask gets no owner turn after it (ignored)
        for day in range(2):
            self.at(T0 + day * 2 * 86400 + 50)
            self.owner_turn(session="s-day%d" % day)
            self.at(T0 + day * 2 * 86400 + 55)
            self.assertEqual(self.historical_inline([w1, w2], topic="DGN-7",
                                             session="s-day%d" % day)[0], 0)
            self.at(T0 + day * 2 * 86400 + 86400 + 60)
            # lifecycle pass only: full derive would also tune (a 0 % answer
            # rate halves the cap), which is E15's business, not E7's
            with store.locked():
                derive.mark_lifecycle(self.ctx, self.asks())
        states = [a["state"] for a in self.asks().values()]
        self.assertEqual(states, ["ignored", "ignored"])
        self.at(T0 + 4 * 86400 + 50)
        self.owner_turn(session="s-day2")
        self.at(T0 + 4 * 86400 + 55)
        self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-7", "--shape", "contrast", "--cand", w1, "--cand", w2)
        self.assertEqual([a["state"] for a in self.asks().values()], ["ignored", "ignored"])

    def test_E8_annoyance_halves_cap_at_once(self):
        w1, w2 = self.propose("A"), self.propose("B")
        self.owner_turn()
        self.at(T0 + 5)
        self.assertEqual(self.historical_inline([w1, w2])[0], 0)
        self.at(T0 + 30)
        self.ups("\uc544 \uadf8\ub9cc \ubb3c\uc5b4\ubd10 \uadf8\ub0e5 \ud574", source="message", message_id=3)
        annoy = [p for p in store.read_rows(store.PROBES) if "annoy" in p]
        self.assertEqual(len(annoy), 1)
        self.assertTrue(annoy[0]["annoy"].startswith("ask-"))
        b = budget.load_budget(self.ctx)
        self.assertEqual(b["inline_cap"], 1)
        self.assertEqual(store.parse_iso(b["reveal_only_until"]), T0 + 30 + 3 * 86400)
        self.at(T0 + 40 * MIN)
        self.owner_turn()
        self.assert_retired("ask", "--trigger", "fork", "--topic", "DGN-8", "--shape", "contrast", "--cand", w1, "--cand", w2)

    def test_E9_reveal_pick(self):
        w1, w2 = self.propose("A"), self.propose("B")
        self.owner_turn()
        self.at(T0 + 5)
        rc, out = self.historical_cli("ask", "--reveal", "--topic", "DGN-9", "--opt", "\uc548\uc815\uc131 \uba3c\uc800=" + w1,
                           "--opt", "\uc18d\ub3c4 \uba3c\uc800=" + w2)
        self.assertEqual((rc, out), (0, ""))
        self.reply("[[OPTIONS: \uc548\uc815\uc131 \uba3c\uc800 | \uc18d\ub3c4 \uba3c\uc800]]")  # the agent's own keyboard
        self.at(T0 + 30)
        self.ups("\uc18d\ub3c4 \uba3c\uc800", source="callback")
        [p] = self.probes()
        self.assertEqual((p["trigger"], p["verdict"], p["slots"]), ("reveal", "pick", [2]))
        self.assertEqual(self.node(w2)["status"], "hypothesis")
        # budget counters unchanged: a budgeted inline ask still passes this turn
        self.at(T0 + 60)
        self.owner_turn()
        self.at(T0 + 65)
        self.assertEqual(self.historical_inline([w1, w2], topic="DGN-10")[0], 0)

    def _confirmed_by_tap(self):
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        self.owner_turn()
        self.at(T0 + 5)
        self.historical_inline([w1, w2])
        self.at(T0 + 30)
        self.ups("B\ub77c\ubca8", source="callback")
        return w2, self.probes()[0]["id"]

    def test_E10_global_reject_contests(self):
        w, pid = self._confirmed_by_tap()
        before = self.node(w)["text"]
        rc, _ = self.cli("interpret", pid, "--verdict", "reject", "--why", w, "--scope", "global",
                         "--note", "owner said not that")
        self.assertEqual(rc, 0)
        self.at(T0 + 3600)
        derive.run(self.ctx)
        n = self.node(w)
        self.assertEqual(n["status"], "contested")
        self.assertEqual(n["text"], before)
        self.assertEqual(len(n["revisions"]), 1)
        self.assertEqual(n["revisions"][0]["text_before"], before)
        q = [a for a in self.asks().values() if a["trigger"] == "contest"]
        self.assertEqual(q, [])  # v4 section 2: no independent contest ask
        derive.run(self.ctx)   # idempotent
        self.assertEqual(len([e for e in store.read_rows(store.EVENTS) if e["ev"] == "contest"]), 1)

    def test_E11_morning_three_bubbles_in_priority(self):
        """DGN-2028 AMEND-2 P3.4: the morning fallback sends nothing (gated
        with the view-only retro); the AMEND-1 priority it used now orders
        the queued asks a `gate` pulls inline (derive.morning_order)."""
        wc, wr = self.propose("C\uc774\uc720"), self.propose("R\uc774\uc720")
        u = []
        for i in range(3):
            self.at(T0 - 3600 * (10 - i))
            u.append(historical_queue(self.ctx, "understanding", "new-work", "DGN-%d" % (300 + i),
                                 ["\uc774\ud574%d" % i])[0])
        self.at(T0)
        ar, _ = historical_queue(self.ctx, "why", "ripe", wr, [wr], shape="confirm")
        ac, _ = historical_queue(self.ctx, "why", "contest", wc, [wc], shape="confirm")
        sent = []

        def sender(ctx, text, buttons):
            sent.append((text, json.loads(buttons)))
            return 0, 1000 + len(sent)
        self.write_conf("WHY_MORNING_FALLBACK=on\n")   # final grill B2: opt-in
        self.at(T0 - 3 * 3600)   # 07:00 KST
        out = io.StringIO()
        self.assertEqual(morning.run(self.ctx, sender=sender, out=out), 0)
        self.assertEqual(sent, [])
        self.assertIn("reason=retro-off", out.getvalue())
        order = [a["ask"] for a in derive.morning_order(self.asks())]
        self.assertEqual(order[:3], [ac, ar, u[0]])
        self.assertTrue(all(self.asks()[x]["state"] == "queued" for x in (ac, ar) + tuple(u)))

    def test_E13_support_two_ripens(self):
        w1, w2 = self.propose("A"), self.propose("B")
        for i in range(2):
            self.at(T0 + i * 100)
            self.owner_turn()
            self.at(T0 + i * 100 + 5)
            self.historical_cli("ask", "--reveal", "--topic", "DGN-13", "--opt", "x%d=%s" % (i, w1),
                     "--opt", "y%d=%s" % (i, w2))
            self.reply("[[OPTIONS: x%d | y%d]]" % (i, i))
            self.at(T0 + i * 100 + 30)
            self.ups("x%d" % i, source="callback")
        self.at(T0 + 3600)
        derive.run(self.ctx)
        with open(os.path.join(self.ddir, "derived", "nodes.json")) as fh:
            n = json.load(fh)[w1]
        # AMEND-1 F3: a reveal pick is an act (A1), never words -- no words
        # support, so no words ripe; the two A1 choice wins ripen it as acts
        self.assertEqual((n["status"], n["support"], n["counter"]), ("hypothesis", 0.0, 0))
        self.assertEqual([a for a in self.asks().values() if a["trigger"] == "ripe"], [])
        q = [a for a in self.asks().values() if a["trigger"] == "act-ripe"]
        # v4 section 2: evidence may ripen, derive never creates an ask.
        self.assertEqual(q, [])
        self.assertTrue(store.read_rows(store.ACTS))

    def test_E14_label_absent_marks_not_shown(self):
        w1, w2 = self.propose("A\ub77c\ubca8"), self.propose("B\ub77c\ubca8")
        sid = "s-e14"
        self.at(T0)
        self.owner_turn(session=sid)
        self.at(T0 + 10)
        self.assertEqual(self.historical_inline([w1, w2])[0], 0)
        shutil.copy(FIX / "transcript-e14-label-absent.jsonl", self.transcript(sid))
        self.at(T0 + 20 * MIN)
        derive.run(self.ctx)
        [a] = self.asks().values()
        self.assertEqual(a["state"], "not_shown")
        rate, n = budget.answer_rate(self.asks(), self.ctx.now)
        self.assertEqual((rate, n), (None, 0))

    def _resolved(self, n_answered, n_ignored):
        for i in range(n_answered + n_ignored):
            aid = "ask-%04d" % (i + 1)
            ev = "answered" if i < n_answered else "ignored"
            for e in ("create", "shown", ev):
                row = {"ask": aid, "ev": e, "at": iso(T0 - 86400 + i)}
                if e == "create":
                    row.update(kind="why", trigger="fork", shape="contrast", topic="DGN-%d" % i,
                               channel="inline", session_id=SID, shown=[])
                store.append_row(store.ASKS, row)

    def test_E15_tuning_raise(self):
        self._resolved(3, 1)   # 75 %
        b = budget.tune(self.ctx)
        self.assertEqual(b["inline_cap"], 4)
        self.assertNotIn("reveal_only_until", b)

    def test_E15_tuning_halve(self):
        self._resolved(7, 13)  # 35 %
        b = budget.tune(self.ctx)
        self.assertEqual(b["inline_cap"], 1)
        self.assertEqual(store.parse_iso(b["reveal_only_until"]), T0 + 3 * 86400)

    def test_E16_morning_outside_window(self):
        w = self.propose("R")
        historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")
        self.write_conf("WHY_MORNING_FALLBACK=on\n")   # final grill B2: opt-in
        calls = []
        self.at(T0 - 5.5 * 3600)   # 04:30 KST
        out = io.StringIO()
        rc = morning.run(self.ctx, sender=lambda *a: calls.append(a) or (0, 1), out=out)
        self.assertEqual(rc, 4)
        self.assertEqual(calls, [])
        self.assertIn("reason=quiet-hours", out.getvalue())

    def test_E17_push_uncertain_stays_queued(self):
        w = self.propose("R")
        aid, _ = historical_queue(self.ctx, "why", "ripe", w, [w], shape="confirm")
        self.write_conf("WHY_MORNING_FALLBACK=on\n")   # final grill B2: opt-in
        self.at(T0 - 3 * 3600)
        morning.run(self.ctx, sender=lambda *a: (6, None), out=io.StringIO())
        a = self.asks()[aid]
        self.assertEqual(a["state"], "queued")
        self.assertNotIn("shown", a["events"])

    def test_off_switch_writes_nothing(self):
        self.write_conf("WHY_NET=off\n")
        with open(os.path.join(self.root, "config", "agent.conf"), "w") as fh:
            fh.write("WHY_NET=off\n")
        ups.run(self.ctx, {"prompt": "1\ubc88", "session_id": SID})
        self.assertEqual(store.read_rows(store.OWNER_TURNS), [])

    def test_injected_turns_are_not_owner_turns(self):
        self.ups("[bridge:injected-turn] background result")
        self.ups("Stop hook feedback: fix it")
        self.assertEqual(store.read_rows(store.OWNER_TURNS), [])

    def test_double_tap_one_probe(self):
        w1, w2 = self.propose("A"), self.propose("B")
        self.owner_turn()
        self.at(T0 + 5)
        self.historical_inline([w1, w2])
        self.at(T0 + 30)
        self.ups("A", source="callback")
        self.ups("A", source="callback")
        self.assertEqual(len(self.probes()), 1)


class LabelWidthParity(unittest.TestCase):
    """Vendored _label_width / _BUTTON_LABEL_MAX_WIDTH equal bridge/options.py."""

    def test_parity(self):
        src = (TEMPLATE / "bridge" / "options.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        const = fn = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "_BUTTON_LABEL_MAX_WIDTH" for t in node.targets):
                const = ast.literal_eval(node.value)
            if isinstance(node, ast.FunctionDef) and node.name == "_label_width":
                fn = node
        self.assertEqual(const, ask.BUTTON_LABEL_MAX_WIDTH)
        mod = ast.Module(body=[fn], type_ignores=[])
        ns = {"unicodedata": unicodedata}
        exec(compile(mod, "options._label_width", "exec"), ns)
        for s in ("", "abc", "\uc804\ubd80 \ucd94\ucc9c\ub300\ub85c (\ucd94\ucc9c)", "\uff21\u5168\u89d2 x\t", "No.3", "2\ubc88 a b c"):
            self.assertEqual(ns["_label_width"](s), ask.label_width(s), s)


class AskSession(Base):
    def test_env_veto_is_not_overridden_by_the_env(self):
        # DGN-1967 bug 18: the resolver refuses a session id from another
        # session; current_session's env fallback handed it back anyway.
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-other"
        self.assertEqual(ask.current_session(None), "sess-other")  # no ledger yet
        store.append_row(store.TURN_LEDGER, {"id": "otl-000001", "session_id": SID,
                                             "sha": "x", "request_id": "r1"})
        self.assertIsNone(ask.current_session(None))
        os.environ["CLAUDE_CODE_SESSION_ID"] = SID
        self.assertEqual(ask.current_session(None), SID)


def _child_run(argv, payload, env):
    """One child run -> (proc, wall s, user+sys CPU s of that child)."""
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t = time.perf_counter()
    p = subprocess.run(argv, input=payload, capture_output=True, text=True, env=env)
    wall = time.perf_counter() - t
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return p, wall, (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)


class UpsLatency(unittest.TestCase):
    """The `ups` hook runs on every owner turn: p95 < 150 ms (spec).

    Wall time alone is a host-load meter on a shared Mac (p95 270-1100 ms
    measured at load5 12-43 with the hook unchanged), so the gate is split:

    - work (always): the hook child's own CPU time (user+sys) p95, deflated
      by the slowdown a bare interpreter start (same std imports) shows in
      the SAME run, interleaved sample by sample.  Run-queue waiting is not
      CPU time, and core/frequency inflation is divided out by the baseline,
      so this holds on a loaded host; every extra import, store scan or
      per-call spike is CPU work and shows 1:1 (a +100 ms busy loop measured
      cpu_adj 174 ms at load 30 -> red).
    - latency (quiet host): the original wall p95 < 150 ms, enforced exactly
      when the same-run baseline proves the host quiet (bare interpreter
      off-CPU p95 <= 30 ms) and the 1-min load average is under half the cores.  This is the only check a blocking wait (sleep,
      lock, network) can trip; on a loaded run its wall number is noise, so
      the check says it was skipped and why instead of passing silently.
    """

    N = 40
    BUDGET_S = 0.150
    QUIET_OFFCPU_S = 0.030

    def test_p95_under_150ms(self):
        tmp = tempfile.mkdtemp(prefix="why-net-lat-")
        try:
            conf = os.path.join(tmp, "agent.conf")
            with open(conf, "w") as fh:
                fh.write("WHY_NET=on\n")
            bot = os.path.join(tmp, "bot")
            os.makedirs(bot)
            env = dict(os.environ, DOGANY_BOT_DATA_DIR=bot, DOGANY_AGENT_CONF=conf,
                       CLAUDE_CONFIG_DIR=os.path.join(tmp, "cc"))
            for k in ("DOGANY_DISPATCH_WORKER", "CLAUDE_CODE_SESSION_ID"):
                env.pop(k, None)
            payload = json.dumps({"prompt": "\uc774\uac70 \uc9c4\ud589\ud574\uc918", "session_id": "lat"})
            hook = [sys.executable, str(ROUTINES / "why-net.py"), "ups"]
            base = [sys.executable, "-c", "import json, os, sys, sqlite3"]
            h_wall, h_cpu, b_wall, b_cpu = [], [], [], []
            for i in range(self.N):
                with open(os.path.join(bot, "inbound-context.json"), "w") as fh:
                    json.dump({"schema": 1, "request_id": "lat-%d" % i,
                               "source": "message", "ts": time.time(),
                               "text_sha": why_owner_turn.turn_sha(
                                   "\uc774\uac70 \uc9c4\ud589\ud574\uc918")}, fh)
                _, w, c = _child_run(base, "", env)
                b_wall.append(w)
                b_cpu.append(c)
                p, w, c = _child_run(hook, payload, env)
                h_wall.append(w)
                h_cpu.append(c)
                self.assertEqual((p.returncode, p.stdout), (0, ""))

            def pct(xs, q):
                xs = sorted(xs)
                return xs[max(0, int(len(xs) * q) - 1)]

            slow = max(1.0, pct(b_cpu, 0.5) / max(min(b_cpu), 1e-4))
            cpu95 = pct(h_cpu, 0.95) / slow
            wall95 = pct(h_wall, 0.95)
            b_off95 = pct([w - c for w, c in zip(b_wall, b_cpu)], 0.95)
            note = ("ups wall p95 %.0f ms, cpu p95 %.0f ms (deflated x%.2f), "
                    "baseline off-cpu p95 %.0f ms"
                    % (wall95 * 1000, cpu95 * 1000, slow, b_off95 * 1000))
            self.assertLess(cpu95, self.BUDGET_S, "work over budget: " + note)
            # A quiet baseline sample alone can miss run-queue pressure that the
            # heavier hook (sqlite, file reads) still pays: also require the
            # 1-min load average under half the cores before trusting wall time.
            load1 = os.getloadavg()[0]
            quiet_load = load1 < 0.5 * (os.cpu_count() or 1)
            note += ", load1 %.1f" % load1
            if b_off95 <= self.QUIET_OFFCPU_S and quiet_load:
                self.assertLess(wall95, self.BUDGET_S, "latency over budget: " + note)
            else:
                print("\n  [UpsLatency] host busy -> wall check skipped, cpu check "
                      "enforced (%s)" % note, file=sys.stderr)
            os.environ["DOGANY_BOT_DATA_DIR"] = bot
            try:
                self.assertEqual(len(store.read_rows(store.OWNER_TURNS)), self.N)
                self.assertEqual(len(store.read_rows(store.TURN_LEDGER)), self.N)
            finally:
                os.environ.pop("DOGANY_BOT_DATA_DIR", None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    unittest.main(verbosity=2)
