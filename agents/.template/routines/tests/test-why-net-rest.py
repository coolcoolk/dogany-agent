#!/usr/bin/env python3
# test-lane: fast
# test-tier: 1
# test-component: why-net correction, seed and ask copy
# test-covers: agents/.template/routines/lib/why_net/ask.py agents/.template/config/i18n/ko.json agents/.template/config/i18n/en.json agents/.template/config/framework-dictionary.json agents/.template/routines/lib/framework_dictionary.py agents/.template/routines/lib/why_net/store.py
# promise: confirm copy preserves the candidate and uses the persona address or clean omission
"""v2.7 T1 Why net -- the pieces left after P1-P3 (LOCKED spec 3, 4.1, 4.4, 5.5).

  TCorr            T-corr: a correction on a work item with 2+ open readings
                   queues one `correction` ask (4.1 T-corr, 4.4 step 6).
  Seed             `why-net seed --from <decision ledger>`: owner lines only,
                   confirmed worded_by=owner, ledger-seed source; the audit
                   backs a seed confirm only from the seed run (R1l kept).
  ConsoleContract  stub reader of the console data contract
                   (docs/design/v27-t1-why-net-console-contract.md) over
                   derived/nodes.json + why-net.md.
  CopyParity       H4: ask copy keys exist in i18n ko/en, button labels fit.

Synthetic data only.  Run: python3 routines/tests/test-why-net-rest.py
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
ROUTINES = os.path.dirname(HERE)
CLI = os.path.join(ROUTINES, "why-net.py")
sys.path.insert(0, os.path.join(ROUTINES, "lib"))

from why_net import budget, store  # noqa: E402


def _load_p3():
    spec = importlib.util.spec_from_file_location(
        "_why_p3", os.path.join(HERE, "test-why-net-p3.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P3 = _load_p3()
CORR = P3.CORR
NEUTRAL = P3.NEUTRAL


class Base(unittest.TestCase):
    def setUp(self):
        self.i = P3.Instance()
        # an empty store: these cases build their own nets
        os.remove(store.path("events.jsonl", self.i.root))

    def tearDown(self):
        self.i.close()

    def env(self, **extra):
        return self.i.env(CLAUDE_CONFIG_DIR=os.path.join(self.i.root, "cc"),
                          **extra)

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI] + list(args),
                              capture_output=True, text=True, env=self.env(),
                              timeout=60)

    def propose(self, text, work=None):
        args = ["propose", "--text", text, "--scope", "framework"]
        if work:
            args += ["--work", work]
        out = self.cli(*args)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return re.search(r"why=(why-\d+)", out.stdout).group(1)

    def ups(self, prompt, sid="s1", transcript=None):
        self.i.inbound(prompt)
        payload = {"prompt": prompt, "session_id": sid,
                   "hook_event_name": "UserPromptSubmit"}
        if transcript:
            payload["transcript_path"] = transcript
        out = subprocess.run([sys.executable, CLI, "ups"],
                             input=json.dumps(payload, ensure_ascii=False),
                             capture_output=True, text=True, env=self.env(),
                             timeout=60)
        self.assertEqual((out.returncode, out.stdout), (0, ""))
        return out

    def asks(self):
        return budget.fold_asks(store.read_rows(store.ASKS, self.i.root))

    def tcorr_asks(self):
        return [a for a in self.asks().values()
                if a.get("trigger") == "correction"]

    def corrections(self):
        return [r for r in store.read_rows(store.CORRECTIONS, self.i.root)
                if r.get("id")]


# ---------------------------------------------------------------------------
# T-corr
# ---------------------------------------------------------------------------

class TCorr(Base):
    def two_readings(self, work="DGN-12"):
        a = self.propose("Keep the old log format for the console", work)
        b = self.propose("Logs must be readable on a phone", work)
        return a, b

    def test_TC1_correction_with_two_readings_queues_one_ask(self):
        a, b = self.two_readings()
        self.ups("DGN-12 " + CORR)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC2_one_reading_queues_nothing(self):
        self.propose("Keep the old log format for the console", "DGN-12")
        self.ups("DGN-12 " + CORR)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC3_ticket_on_a_confirmed_why_is_never_a_trigger(self):
        shutil.copy(P3.NET, store.path("events.jsonl", self.i.root))  # why-0001 confirmed
        self.two_readings()
        self.i.ticket("DGN-12", why="why-0001")
        self.ups("DGN-12 " + CORR)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC3b_ticket_hypothesis_why_counts_as_a_reading(self):
        a = self.propose("Keep the old log format for the console", "DGN-12")
        b = self.propose("Ship before the weekend")
        self.i.ticket("DGN-12", why=b)
        self.ups("DGN-12 " + CORR)
        self.assertEqual(self.tcorr_asks(), [])
        self.assertEqual(len(self.corrections()), 1)

    def test_TC4_no_work_item_queues_nothing(self):
        self.two_readings()
        self.ups(CORR)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC5_work_item_from_the_ticket_the_session_wrote(self):
        self.two_readings()
        path = self.i.ticket("DGN-12")
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        tp = self.i.transcript("s1", [
            P3.user("fix the log", now, "u1"),
            P3.agent("done", now, tool=path)])
        self.ups(CORR, transcript=tp)
        self.assertEqual(self.tcorr_asks(), [])
        self.assertEqual(len(self.corrections()), 1)

    def test_TC6_replay_queues_once(self):
        self.two_readings()
        self.ups("DGN-12 " + CORR)
        self.ups("DGN-12 " + CORR)
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC7_neutral_turn_queues_nothing(self):
        self.two_readings()
        self.ups("DGN-12 " + NEUTRAL)
        self.assertEqual(self.corrections(), [])
        self.assertEqual(self.tcorr_asks(), [])

    def test_TC8_dispatch_worker_queues_nothing(self):
        self.two_readings()
        self.i.inbound("DGN-12 " + CORR)
        subprocess.run([sys.executable, CLI, "ups"],
                       input=json.dumps({"prompt": "DGN-12 " + CORR,
                                         "session_id": "s1"}),
                       capture_output=True, text=True,
                       env=self.env(DOGANY_DISPATCH_WORKER="run-1"), timeout=60)
        self.assertEqual(self.tcorr_asks(), [])


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------

def write_ledger(root, entries, pending=()):
    """entries: [(n, answer)] confirmed DGN-1952 machine entries, options
    'Machine writes it / Keep the persona line'."""
    lines = ["# Decisions", ""]
    for n, answer in entries:
        lines += [
            '## dec-%d -- pick a way (2026-10-0%d) [confirmed 2026-10-0%d 21:1%d: "%s"]'
            % (n, n % 9 + 1, n % 9 + 1, n % 10, answer),
            "<!-- decision-ledger id=dec-%d state=confirmed -->" % n,
            "- Options: 1. Machine writes it / 2. Keep the persona line",
            "- Recommended: Machine writes it", ""]
    for n in pending:
        lines += ["## dec-%d -- open (2026-10-08) [pending 2026-10-08 09:00]" % n,
                  "<!-- decision-ledger id=dec-%d state=pending -->" % n, ""]
    path = os.path.join(root, "worklog", "_DECISIONS.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


OWNER_LINE = "고객이 직접 고르게 해, 우리가 정하지 말고"


class Seed(Base):
    def nodes(self):
        out = self.cli("show")
        self.assertEqual(out.returncode, 0)
        return json.loads(out.stdout)

    def audit(self):
        return self.cli("derive", "--audit", "--no-model").stdout

    def test_SD1_owner_lines_only(self):
        write_ledger(self.i.root, [(1, OWNER_LINE), (2, "2"), (3, "Machine writes it"),
                                   (4, "1. Machine writes it"), (5, "2번")],
                     pending=(6,))
        out = self.cli("seed", "--from", "worklog/_DECISIONS.md")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("imported=1", out.stdout)
        self.assertIn("skipped_pick=4", out.stdout)
        nodes = self.nodes()
        self.assertEqual(len(nodes), 1)
        n = list(nodes.values())[0]
        self.assertEqual(n["text"].encode("utf-8"), OWNER_LINE.encode("utf-8"))
        self.assertEqual((n["status"], n["worded_by"], n["owner_words"]),
                         ("confirmed-provisional", "owner", ["seed:dec-1"]))
        self.assertIsNone(n["settle"])  # AMEND-1 5.1 SD1: seed -> provisional
        self.assertEqual(n["source"]["kind"], "ledger-seed")
        self.assertEqual(n["confirm"]["source"]["dec"], "dec-1")
        self.assertTrue(n["said_at"].startswith("2026-10-02T21:11"))

    def test_SD2_seed_run_is_backed_by_the_audit(self):
        write_ledger(self.i.root, [(1, OWNER_LINE), (2, "다음 달까지는 무료로 두자")])
        self.assertEqual(self.cli("seed", "--from", "worklog/_DECISIONS.md").returncode, 0)
        out = self.audit()
        self.assertIn("WHY_AUDIT_OK", out)
        self.assertNotIn("UNBACKED", out)

    def test_SD3_ledger_edited_after_seed_is_unbacked(self):
        path = write_ledger(self.i.root, [(1, OWNER_LINE)])
        self.cli("seed", "--from", "worklog/_DECISIONS.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(OWNER_LINE, "something else"))
        out = self.audit()
        self.assertIn("WHY_AUDIT_UNBACKED", out)
        self.assertIn("seed-not-in-ledger", out)

    def test_SD4_seed_confirm_after_agent_events_is_unbacked(self):
        write_ledger(self.i.root, [(1, OWNER_LINE)])
        self.cli("seed", "--from", "worklog/_DECISIONS.md")
        w = self.propose("x")
        rows = store.read_rows(store.EVENTS, self.i.root)
        src = dict(rows[1]["data"]["source"])
        forged = {"ev": "confirm", "id": "evt-%06d" % (len(rows) + 1), "why": w,
                  "at": store.now_iso(), "actor": "seed",
                  "data": {"quote": OWNER_LINE, "worded_by": "owner", "probe": None,
                           "said_at": None, "source": src}}
        with open(store.path(store.EVENTS, self.i.root), "rb") as fh:
            last = fh.read().rstrip(b"\n").split(b"\n")[-1]
        import hashlib
        forged["prev"] = hashlib.sha256(last).hexdigest()
        with open(store.path(store.EVENTS, self.i.root), "a", encoding="utf-8") as fh:
            fh.write(store.dumps(forged) + "\n")
        out = self.audit()
        self.assertIn("seed-unbacked", out)

    def test_R1l_still_refuses_on_non_empty_events(self):
        write_ledger(self.i.root, [(1, OWNER_LINE)])
        self.propose("x")
        out = self.cli("seed", "--from", "worklog/_DECISIONS.md")
        self.assertEqual(out.returncode, 3)
        self.assertIn("events-not-empty", out.stdout)

    def test_SD5_missing_source_refuses(self):
        out = self.cli("seed", "--from", "worklog/nope.md")
        self.assertEqual(out.returncode, 3)
        self.assertIn("seed-source-missing", out.stdout)
        self.assertEqual(store.read_rows(store.EVENTS, self.i.root), [])

    def test_SD7_number_led_owner_words_are_not_a_pick(self):
        # DGN-1967 bug 15: _PICK_RX dropped "1. ..." owner words as a pick
        write_ledger(self.i.root, [(1, "1.5 hours is too long, rethink"),
                                   (2, "No. 2"), (3, "2.")])
        out = self.cli("seed", "--from", "worklog/_DECISIONS.md")
        self.assertIn("imported=1", out.stdout)
        self.assertIn("skipped_pick=2", out.stdout)

    def test_SD8_said_at_carries_the_instance_offset(self):
        # DGN-1967 bug 20: the ledger stamps agent-local time; the seed read
        # it in the host zone
        with open(os.path.join(self.i.root, "config", "agent.conf"), "a") as fh:
            fh.write("AGENT_TZ_OFFSET_HOURS=14\n")
        write_ledger(self.i.root, [(1, OWNER_LINE)])
        self.cli("seed", "--from", "worklog/_DECISIONS.md")
        n = list(self.nodes().values())[0]
        self.assertEqual(n["said_at"], "2026-10-02T21:11:00+14:00")

    def test_SD9_label_with_slash_is_one_label(self):
        # DGN-1967 bug 17: " / " inside a label split it
        from why_net import acts, seed
        path = os.path.join(self.i.root, "worklog", "_DECISIONS.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('## dec-4 -- x (2026-10-08) [confirmed 2026-10-08 10:05: "Keep A / B"]\n'
                     "<!-- decision-ledger id=dec-4 state=confirmed -->\n"
                     "- Options: 1. Keep A / B / 2. Drop\n")
        ctx = budget.Ctx(root=self.i.root)
        e = seed.parse(ctx, path)[0]
        self.assertEqual((e["labels"], e["kind"]), (["Keep A / B", "Drop"], "pick"))
        with open(os.path.join(self.i.root, "config", "agent.conf"), "a") as fh:
            fh.write("DECISION_LEDGER_PATH=worklog/_DECISIONS.md\n")
        ctx = budget.Ctx(root=self.i.root)
        self.assertEqual(acts.decisions(ctx)["dec-4"]["labels"], ["Keep A / B", "Drop"])

    def test_SD6_instance_tag_format_is_read_from_conf(self):
        with open(os.path.join(self.i.root, "config", "agent.conf"), "a") as fh:
            fh.write('DECISION_LEDGER_CONFIRMED_TAG=<{status} | {answer}>\n')
        path = os.path.join(self.i.root, "worklog", "_DECISIONS.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## dec-7 -- x (2026-10-08) <confirmed 2026-10-08 10:05 | %s>\n"
                     "- Options: 1. A / 2. B\n" % OWNER_LINE)
        out = self.cli("seed", "--from", "worklog/_DECISIONS.md")
        self.assertIn("imported=1", out.stdout)
        self.assertEqual(list(self.nodes().values())[0]["owner_words"], ["seed:dec-7"])


# ---------------------------------------------------------------------------
# console contract (stub reader; the console UI lives in its own repo)
# ---------------------------------------------------------------------------

def read_why_view(data_dir):
    """Stub of the console `/why` reader: only derived/nodes.json and
    why-net.md, read-only, per docs/design/v27-t1-why-net-console-contract.md."""
    base = os.path.join(data_dir, "state", "why-net")
    view = {"by_status": {}, "owner_words": {}, "asks": []}
    try:
        with open(os.path.join(base, "derived", "nodes.json"), encoding="utf-8") as fh:
            nodes = json.load(fh)
    except OSError:
        nodes = {}
    for wid, n in nodes.items():
        view["by_status"].setdefault(n["status"], []).append(
            {"id": wid, "text": n["text"], "support": n["support"],
             "counter": n["counter"], "revisions": n["revisions"]})
    try:
        with open(os.path.join(base, "why-net.md"), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        lines = []
    cur_node = cur_ask = None
    in_asks = False
    for line in lines:
        if line.startswith("## "):
            in_asks = line == "## open asks"
            continue
        m = re.match(r"^- (why-\d+) ", line)
        if m and not in_asks:
            cur_node = m.group(1)
            continue
        m = re.match(r"^  - owner_words: (\S+) (\".*\")$", line)
        if m and cur_node:
            view["owner_words"].setdefault(cur_node, []).append(
                (m.group(1), json.loads(m.group(2))))
            continue
        m = re.match(r"^- (ask-\d+) state=(\S+) kind=(\S+) trigger=(\S+) topic=(\S+)", line)
        if m and in_asks:
            cur_ask = {"ask": m.group(1), "state": m.group(2), "trigger": m.group(4),
                       "topic": m.group(5), "labels": []}
            view["asks"].append(cur_ask)
            continue
        m = re.match(r"^  - (\d+)\. (\".*?\")(?: \((why-\d+)\))?$", line)
        if m and in_asks and cur_ask:
            cur_ask["labels"].append((int(m.group(1)), json.loads(m.group(2)), m.group(3)))
    return view


class ConsoleContract(Base):
    def test_empty_store_is_an_empty_view(self):
        v = read_why_view(self.i.bot)
        self.assertEqual(v, {"by_status": {}, "owner_words": {}, "asks": []})

    def test_view_after_seed_propose_and_queue(self):
        write_ledger(self.i.root, [(1, OWNER_LINE)])
        self.assertEqual(self.cli("seed", "--from", "worklog/_DECISIONS.md").returncode, 0)
        a = self.propose("Logs must be readable on a phone", "DGN-12")
        b = self.propose("Keep the old log format\nfor the console", "DGN-12")
        self.ups("DGN-12 " + CORR)
        # Keep the console's historical-row reader under test after retirement.
        self.assertEqual(self.tcorr_asks(), [])
        from legacy_why_fixtures import historical_queue
        with patch.dict(os.environ, self.env(), clear=True):
            historical_queue(budget.Ctx(root=self.i.root), "why", "correction", "DGN-12", [a, b], work="DGN-12")
        out = self.cli("derive", "--no-model")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        v = read_why_view(self.i.bot)
        # AMEND-1 5.1 SD1/ConsoleContract: a seed lands provisional
        self.assertEqual([n["text"] for n in v["by_status"]["confirmed-provisional"]],
                         [OWNER_LINE])
        self.assertNotIn("confirmed", v["by_status"])
        self.assertEqual(sorted(n["id"] for n in v["by_status"]["hypothesis"]), [a, b])
        self.assertEqual(v["owner_words"]["why-0001"], [("seed:dec-1", OWNER_LINE)])
        self.assertEqual(len(v["asks"]), 1)
        ask = v["asks"][0]
        self.assertEqual((ask["state"], ask["trigger"], ask["topic"]),
                         ("queued", "correction", "DGN-12"))
        self.assertEqual([x[2] for x in ask["labels"] if x[2]], [a, b])
        self.assertIn("Keep the old log format\nfor the console", [x[1] for x in ask["labels"]])
        # read-only: the reader never created or changed a file
        with open(os.path.join(store.store_dir(self.i.root), "why-net.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("[가설]", md)

    def test_render_verb_writes_the_same_grammar(self):
        self.propose("x")
        out = self.cli("render")
        self.assertEqual(out.returncode, 0)
        self.assertIn("WHY_OK", out.stdout)
        with open(store.path("why-net.md", self.i.root), encoding="utf-8") as fh:
            md = fh.read().split("\n")
        self.assertEqual(md[0], "# Why net (read-only render; source: events.jsonl)")
        self.assertIn("  - support: 0.0, counter: 0", md)


# ---------------------------------------------------------------------------
# H4 copy parity (the copy itself is user-confirmed before switch-on)
# ---------------------------------------------------------------------------

class CopyParity(unittest.TestCase):
    def catalog(self, lang):
        path = os.path.join(os.path.dirname(ROUTINES), "config", "i18n", lang + ".json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def lookup(self, cat, key):
        return cat[key]  # flat dotted keys

    def test_H4_keys_exist_in_ko_and_en_and_labels_fit(self):
        from why_net import ask
        for lang in ("ko", "en"):
            cat = self.catalog(lang)
            for key in ask.COPY_KEYS_CONFIRMED + ask.COPY_KEYS_PENDING_OWNER_CONFIRM:
                val = self.lookup(cat, key)
                self.assertTrue(isinstance(val, str) and val.strip(), (lang, key))
            for key in ("why.ask.escape", "why.ask.confirm_yes", "why.ask.confirm_no"):
                self.assertLessEqual(ask.label_width(self.lookup(cat, key)),
                                     ask.BUTTON_LABEL_MAX_WIDTH, (lang, key))
            self.assertIn("{text}", self.lookup(cat, "why.ask.stem.confirm"))

    def test_confirm_persona_address_and_omission_keep_verbatim_candidate(self):
        from why_net import ask
        saved = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
                    os.environ.pop(key)
            with tempfile.TemporaryDirectory(prefix="why-confirm-copy-") as root:
                config = os.path.join(root, "config")
                os.makedirs(config)
                shutil.copytree(os.path.join(os.path.dirname(ROUTINES), "config", "i18n"),
                                os.path.join(config, "i18n"))
                conf = os.path.join(config, "agent.conf")
                # A non-canonical Unicode spelling and embedded punctuation
                # must survive exactly, even when the stem has an address.
                candidate = "Cafe\u0301 makes planning simple: choose freely."
                known = {"why-0001": {"text": candidate, "status": "hypothesis"}}
                for lang in ("ko", "en"):
                    with open(conf, "w") as fh:
                        fh.write("AGENT_LANG=%s\nDASHBOARD_OWNER_TERM=Config term\n" % lang)
                    ctx = budget.Ctx(root=root, conf_path=conf)
                    for address in ("Captain", ""):
                        # Reuse the persona reader; its value takes precedence
                        # over owner-term config. No baseline identity is edited.
                        with patch("voice_vocab._persona_field", return_value=address), \
                                patch("owner_term.owner_term", return_value="the user"):
                            shown, err = ask.build_slots(ctx, ["why-0001"], "confirm", known)
                            self.assertIsNone(err)
                            text = ask.render(ctx, "confirm", shown, known, "ripe",
                                              {"topic": "dec-700", "work_ref": "DGN-700"})
                        cat = self.catalog(lang)
                        key = "why.ask.stem.confirm" if address else "why.ask.stem.confirm_no_address"
                        expected = cat[key].replace("{address_subject}", ask._ko_subject(address)) \
                            .replace("{address}", address).replace("{text}", candidate)
                        self.assertEqual(text, expected + "\n[[OPTIONS: " + cat["why.ask.confirm_yes"]
                                         + " | " + cat["why.ask.confirm_no"] + "]]")
                        self.assertEqual(text.splitlines()[1], candidate)
                        self.assertEqual(len(shown), 2)
                        self.assertTrue(shown[1]["wrong"])
                    # promise: the ko stem attaches the right subject particle to the address
                    if lang == "ko":
                        for addr, want in (("\ud615\ub2d8", "\ud615\ub2d8\uc774 "),
                                           ("\ucc3d\uc870\uc8fc", "\ucc3d\uc870\uc8fc\uac00 ")):
                            with patch("voice_vocab._persona_field", return_value=addr):
                                self.assertIn(want, ask.render(ctx, "confirm", shown, known, "ripe"))
                    with patch("voice_vocab._persona_field", return_value=""):
                        self.assertIn("Config term", ask.render(ctx, "confirm", shown, known, "ripe"))
                    identity = os.path.join(root, "identity")
                    os.makedirs(identity, exist_ok=True)
                    for field in ('- Call the user **Persona term**.',
                                  '- Address the user as **"Persona term"** (English).'):
                        with open(os.path.join(identity, "hot.custom.agent.md"), "w") as fh:
                            fh.write(field + "\n")
                        rendered = ask.render(ctx, "confirm", shown, known, "ripe")
                        self.assertIn("Persona term", rendered)
                        self.assertNotIn("Config term", rendered)

        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_non_confirm_shapes_keep_exact_stems_and_line_breaks(self):
        from why_net import ask
        ctx = SimpleNamespace(root="unused")
        known = {"why-0001": {"text": "First purpose.\nKeep this line.", "status": "hypothesis"},
                 "why-0002": {"text": "Second purpose.", "status": "hypothesis"}}
        cases = [(shape, "before-work", "why.ask.stem." + shape.replace("-", "_"), False)
                 for shape in ("contrast", "set", "pole", "scatter", "act-confirm", "split")]
        cases += [("contrast", "fork", "why.ask.stem.mixed", True),
                  ("contrast", "fork-mixed", "why.ask.stem.mixed", False),
                  ("set", "reading", "why.ask.stem.reading", False),
                  ("readings", "readings", "why.retro.head", False),
                  ("set", "turn", "why.ask.stem.turn", False)]
        for lang in ("ko", "en"):
            cat = self.catalog(lang)
            cat["why.ask.stem.turn"] = "This work: choose a purpose.\nKeep this line."
            for shape, trigger, key, both in cases:
                with self.subTest(lang=lang, shape=shape, trigger=trigger, both=both):
                    shown = [{"slot": 1, "why": "why-0001", "side": "said", "label": "First"},
                             {"slot": 2, "why": "why-0002", "side": "acted", "label": "Second"}]
                    if both:
                        shown[0]["both"] = list(known)
                    stem = cat[key].replace("{text}", known["why-0001"]["text"]) \
                        .replace("{said}", known["why-0001"]["text"]) \
                        .replace("{acted}", known["why-0002"]["text"]) \
                        .replace("{signal}", "The owner's signal.")
                    hint = "" if key in ("why.ask.stem.mixed", "why.ask.stem.reading",
                                              "why.ask.stem.turn") else cat["why.ask.free_hint"]
                    expected = "\n".join(line for line in (stem, hint) if line) \
                        + "\n[[OPTIONS: First | Second]]"
                    with patch.object(ask, "t", side_effect=lambda ctx, key: cat[key]), \
                            patch("why_net.retro.signal_label", return_value="The owner's signal."), \
                            patch("why_net.surfaces.find_ticket", side_effect=AssertionError("ticket lookup")):
                        self.assertEqual(ask.render(ctx, shape, shown, known, trigger,
                                                    {"work_ref": "DGN-700", "target": "kit/internal-title"}),
                                         expected)

    def test_every_asking_shape_has_a_stem(self):
        from why_net import ask
        for shape in ask.SHAPES:
            if shape == "reveal":
                continue  # reveal never prints copy
            self.assertIn(ask.stem_key(shape), ask.COPY_KEYS_CONFIRMED
                          + ask.COPY_KEYS_PENDING_OWNER_CONFIRM)
        self.assertIn(ask.stem_key("contrast", "fork-mixed"), ask.COPY_KEYS_CONFIRMED)
        self.assertIn(ask.stem_key("contrast", None, both=True), ask.COPY_KEYS_CONFIRMED)
        self.assertIn(ask.stem_key("set", "reading"), ask.COPY_KEYS_PENDING_OWNER_CONFIRM)

    def test_amend1_console_keys_and_slots(self):
        # AMEND-1 section 4: 5b-5f, 10-10c exist in ko and en; slots kept
        from why_net import ask
        for lang in ("ko", "en"):
            cat = self.catalog(lang)
            for key in ask.COPY_KEYS_CONFIRMED + ask.CONSOLE_KEYS_CONFIRMED + ask.COPY_KEYS_PENDING_OWNER_CONFIRM:
                self.assertTrue(isinstance(cat.get(key), str) and cat[key].strip(), (lang, key))
            self.assertIn("{text}", cat["why.ask.stem.act_confirm"])
            self.assertIn("{said}", cat["why.ask.stem.split"])
            self.assertIn("{acted}", cat["why.ask.stem.split"])
            self.assertIn("{date}", cat["why.pivot.basis"])
            self.assertIn("{n}", cat["why.pivot.basis"])
            for key in ("why.ask.split.said", "why.ask.split.acted"):
                self.assertLessEqual(ask.label_width(cat[key]), ask.BUTTON_LABEL_MAX_WIDTH)
            # the model name is design-only (section 4)
            for key in ask.COPY_KEYS_CONFIRMED + ask.CONSOLE_KEYS_CONFIRMED + ask.COPY_KEYS_PENDING_OWNER_CONFIRM:
                self.assertNotIn("\ud669\uae08\ub098\uc120", cat[key])



class FirstAppearanceGloss(unittest.TestCase):
    # promise: ko asks preserve the exact owner-picked stems and show Why once
    # promise: gloss claims survive fresh contexts, language changes and store copies
    # promise: only present terms get independent dictionary-backed glosses
    # promise: kit layers add definitions but cannot redefine framework term ids
    def setUp(self):
        self.saved = dict(os.environ)
        for key in list(os.environ):
            if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
                os.environ.pop(key)
        self.tmp = tempfile.TemporaryDirectory(prefix="why-gloss-")
        self.root = self.tmp.name
        shutil.copytree(os.path.join(os.path.dirname(ROUTINES), "config"),
                        os.path.join(self.root, "config"))
        self.conf = os.path.join(self.root, "config", "agent.conf")
        self.set_lang("ko")
        self.known = {"why-0001": {"text": "First purpose.", "status": "hypothesis"},
                      "why-0002": {"text": "Second purpose.", "status": "hypothesis"}}
        self.shown = [{"slot": 1, "why": "why-0001", "side": "said", "label": "First"},
                      {"slot": 2, "why": "why-0002", "side": "acted", "label": "Second"}]

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.saved)

    def set_lang(self, lang):
        with open(self.conf, "w") as fh:
            fh.write("AGENT_LANG=" + lang + "\n")
        self.ctx = budget.Ctx(root=self.root, conf_path=self.conf)

    def issue(self, shape="contrast", trigger="before-work"):
        from why_net import ask
        return ask.render_with_gloss(self.ctx, shape, self.shown, self.known, trigger)

    def test_owner_copy_first_and_second_in_each_shape(self):
        from why_net import ask
        stems = {
            "contrast": "\uc774 \uc791\uc5c5, \uc5b4\ub5a4 Why\uc5d0 \ub354 \uac00\uae4c\uc6cc\uc694?",
            "set": "\uc774 \uc791\uc5c5\uc758 Why, \ud574\ub2f9\ud558\ub294 \uac78 \uace8\ub77c\uc8fc\uc138\uc694.",
            "act-confirm": "\ucd5c\uadfc \uacb0\uc815\uacfc \uc791\uc5c5\uc774 \uc774 Why \ucabd\uc774\uc5c8\uc5b4\uc694. \ub9de\ub098\uc694? First purpose.",
            "split": '\ub9d0\uc500\uc740 "First purpose." \ucabd\uc774\uc5c8\ub294\ub370, \uacb0\uc815\uc740 "Second purpose." \ucabd\uc774\uc5c8\uc5b4\uc694. \uc5b4\ub290 \ucabd\uc774 Why\uc5d0 \uac00\uae4c\uc6cc\uc694?',
            "mixed": "\uc774 \uc791\uc5c5\uc5d0\ub294 Why\uac00 \ub450 \uac00\uc9c0 \uc788\ub294 \uac83 \uac19\uc544\uc694. \ub458 \ub2e4 \ub9de\uc73c\uba74 '\ub458 \ub2e4'\ub97c, \uacbd\uc6b0\uc5d0 \ub530\ub77c \ub2e4\ub974\uba74 \uc5b4\ub5a4 \uacbd\uc6b0\uc778\uc9c0 \uc801\uc5b4\uc8fc\uc138\uc694."
        }
        for shape, stem in stems.items():
            with self.subTest(shape=shape):
                flag = store.path(store.TERM_GLOSSES, self.root)
                if os.path.exists(flag):
                    os.unlink(flag)
                args = ("contrast", "fork-mixed") if shape == "mixed" else (shape, "before-work")
                plain = stem
                if shape != "mixed":
                    plain += "\n\ub2e4\ub978 Why\uba74 \uadf8\ub300\ub85c \uc801\uc5b4\uc8fc\uc138\uc694."
                first = self.issue(*args)
                second = self.issue(*args)
                marker = "\n[[OPTIONS: First | Second]]"
                # promise: the gloss sits right under the question line
                q, _, tail = plain.partition("\n")
                glossed = q + "\nWhy: \ubbff\uc74c\uacfc \ubaa9\uc801" + ("\n" + tail if tail else "")
                self.assertEqual(first, glossed + marker)
                self.assertEqual(second, plain + marker)
                self.assertEqual(ask.t(self.ctx, "why.ask.subject.framework"), "\ub3c4\uac00\ub2c8 \ud070 Why")

    def test_persistence_copy_and_language_switch(self):
        self.assertIn("Why: \ubbff\uc74c\uacfc \ubaa9\uc801", self.issue())
        copied = os.path.join(self.root, "copy")
        shutil.copytree(store.store_dir(self.root), store.store_dir(copied))
        self.set_lang("en")
        self.shown[0]["label"] = "Why clarity"
        self.assertNotIn("Why: belief and purpose", self.issue())
        self.ctx = budget.Ctx(root=copied, conf_path=self.conf)
        self.assertNotIn("Why: belief and purpose", self.issue())
        fresh = os.path.join(self.root, "fresh")
        self.ctx = budget.Ctx(root=fresh, conf_path=self.conf)
        from why_net import ask
        # Use the real English catalog on a separate instance.
        shutil.copytree(os.path.join(self.root, "config"), os.path.join(fresh, "config"))
        self.assertIn("Why: belief and purpose", self.issue())

    def test_only_present_terms_are_claimed_and_dictionary_is_source(self):
        from why_net import ask
        import framework_dictionary as dictionary
        self.assertNotIn("Why:", ask.render_with_gloss(
            self.ctx, "confirm", self.shown, self.known, "ripe"))
        self.assertFalse(os.path.exists(store.path(store.TERM_GLOSSES, self.root)))
        self.known["why-0001"]["text"] = "How\uc640 What\uc744 Why\ub85c \uc5f0\uacb0\ud574\uc694."
        text = ask.render_with_gloss(self.ctx, "confirm", self.shown, self.known, "ripe")
        for term in ("why", "how", "what"):
            self.assertEqual(text.count(ask.t(self.ctx, "why.ask.gloss." + term)), 1)
        self.assertEqual({r["term"] for r in store.read_rows(store.TERM_GLOSSES, self.root)},
                         {"why", "how", "what"})
        path = os.path.join(self.root, "config", dictionary.FILE)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["framework"]["how"]["ko"] = "How: dictionary source"
        with open(path, "w") as fh:
            json.dump(data, fh)
        self.assertEqual(ask.t(self.ctx, "why.ask.gloss.how"), "How: dictionary source")
        for lang in ("ko", "en"):
            self.set_lang(lang)
            self.assertNotIn("[[framework:", ask.t(self.ctx, "why.ask.gloss.what"))

    def test_term_matching_and_independent_first_appearance(self):
        from why_net import ask
        self.known["why-0001"]["text"] = "Somewhat Whysoever However WHY why how what."
        text = ask.render_with_gloss(self.ctx, "confirm", self.shown, self.known)
        self.assertNotIn("Why:", text)
        self.assertFalse(os.path.exists(store.path(store.TERM_GLOSSES, self.root)))
        self.assertIn("Why: \ubbff\uc74c\uacfc \ubaa9\uc801", self.issue())
        self.known["why-0001"]["text"] = "How\ub97c \uc815\ud574\uc694."
        text = ask.render_with_gloss(self.ctx, "confirm", self.shown, self.known)
        self.assertIn("How: \ubc29\ubc95", text)
        self.assertNotIn("What: \uacb0\uacfc\ubb3c", text)
        self.assertNotIn("Why: \ubbff\uc74c\uacfc \ubaa9\uc801", text)

    def test_kit_layer_adds_but_never_redefines_framework_terms(self):
        import framework_dictionary as dictionary
        packs = os.path.join(self.root, "config", "packs")
        os.makedirs(packs, exist_ok=True)
        path = os.path.join(packs, "sample." + dictionary.FILE)
        def write(data):
            with open(path, "w") as fh:
                json.dump(data, fh)
        value = {"ko": "sample", "en": "sample"}
        write({"sample": {"extra": value}})
        self.assertEqual(dictionary.load(self.root)["sample"]["extra"], value)
        self.assertIn("product_user", dictionary.load(self.root)["devkit"])
        for term in dictionary.load(self.root)["framework"]:
            for layer in ("framework", "sample"):
                write({layer: {term: value}})
                with self.assertRaisesRegex(ValueError, "redefines framework"):
                    dictionary.load(self.root)
        write({"sample": {"extra": value}})
        self.assertEqual(dictionary.load(self.root)["sample"]["extra"], value)


if __name__ == "__main__":
    unittest.main(verbosity=1)
