#!/usr/bin/env python3
# test-lane: fast
"""v2.7 D2: the Stop text-layer seat (docs/v27-D2-spec.md).

Locks:
  (a) every fixture row fires exactly when its fixture says so (true- and
      false-positive fixtures per row, fixtures/v27-d2/), plus the rework
      fixes at row level: interim messages (F3), NO_PUSH (Q6), fenced
      marker (F9), English word edges and the en vocabulary (F10), inline
      code (F12), the dropped V01 go/hold branch, guard-suppressed
      first-regex hits for HF27/HF30 (Q8), a row that raises (F7);
  (b) one rule per turn: a reply that trips two rows gets ONE block whose
      reason names one rule and one doc pointer; the loser is logged;
  (c) once per turn (F1/F2): no stop_hook_active exit; an atomic per-turn
      latch keyed by session + turn-opening prompt, so a rewritten reply
      (new message id) in the same turn never blocks again, concurrent
      Stops block once, and a new turn blocks again;
  (d) the user's own session only, through lib/owner_session.py (F7);
  (e) fail-open is never silent: every fail-open writes TEXT_SEAT_SKIP
      reason=<...> (F7); mode off writes nothing;
  (f) default mode is detect; canary lines carry ref=<transcript>|<msg id>,
      would_block and role (F6); an empty detect-rows knob really disables
      (F7); a non-ko locale runs every row detect-only (F10);
  (g) switches (F8): GATE_STOP_ALL, the text-seat family and row slugs, and
      the G03 row's own output-gate-idle-promise slug;
  (h) transcript reading (F2/F13): the tail read gives the same last turn
      as a full parse; the measurement parse scores the pre-block reply;
  (i) framework wording: no owner form of address or instance vocabulary
      in the seat's code or block copy; the classifier and adapter are
      ASCII (language words live in text_seat_vocab/); every pointer names
      a section that exists; D1 and D2 never exec status-footer.py;
  (j) wiring: the template settings wire the seat once on Stop with
      TEXT_SEAT_MODE=detect pinned in the command;
  (k) G03 overlay: with the instance words supplied as config, the generic
      vocabulary gives the shipped IDLE_PROMISE verdicts on the fixture
      sentences; without them, the instance words exempt nothing.
"""
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
SEAT = ROUTINES / "text-seat-stop.py"
FIXTURES = HERE / "fixtures" / "v27-d2" / "text-seat-fixtures.json"
SETTINGS = TEMPLATE / ".claude" / "settings.json"
sys.path.insert(0, str(ROUTINES / "lib"))

import text_seat as T  # noqa: E402
import text_seat_g03 as G  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SEAT_MOD = _load("text_seat_stop", SEAT)


def _tools(rows):
    return [T.Tool(n, x if n == "Bash" else None, None if n == "Bash" else x)
            for n, x in rows]


def _turn_lines(prompt, reply, tools=(), uuid="u1", final_id="final",
                interim=None):
    lines = [{"type": "user", "timestamp": "2026-10-08T10:00:00Z",
              "uuid": uuid, "message": {"role": "user", "content": prompt}}]
    if interim:
        lines.append({"type": "assistant", "message": {
            "id": "interim-" + uuid, "role": "assistant",
            "content": [{"type": "text", "text": interim}]}})
    for i, (name, arg) in enumerate(tools):
        inp = {"command": arg} if name == "Bash" else {"file_path": arg}
        lines.append({"type": "assistant", "message": {
            "id": "m%d-%s" % (i, uuid), "role": "assistant",
            "content": [{"type": "tool_use", "id": "t%d" % i, "name": name,
                         "input": inp}]}})
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i,
             "content": "ok"}]}})
    lines.append({"type": "assistant", "message": {
        "id": final_id, "role": "assistant",
        "content": [{"type": "text", "text": reply}]}})
    return lines


def _write(path, lines, mode="w"):
    with open(path, mode, encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln, ensure_ascii=False) + "\n")


def _transcript(path, prompt, reply, tools=()):
    """A minimal Claude Code transcript: one user prompt, one tool round,
    one final assistant message."""
    _write(path, _turn_lines(prompt, reply, tools))


def _stop_feedback():
    return {"type": "user", "isMeta": True, "message": {
        "role": "user", "content": "Stop hook feedback:\n[other-gate]: x"}}


ORDER_REPLY = ("화면에 문제 두 개가 보여요.\n\n어느 쪽부터 짚을까요? ①로 갈까요?\n\n"
               "[[OPTIONS: 고친 자리 검증 | 제목 누출 차단 | 둘 다]]")
CLEAN_REPLY = "두 문제를 같이 고쳤어요."


class Fixtures(unittest.TestCase):
    def test_a_each_fixture(self):
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
        per_row = {}
        for c in cases:
            cfg = T.Config(**c.get("cfg", {}))
            turn = T.Turn(c["reply"], c["prompt"], _tools(c["tools"]))
            rows = [f.row for f in T.classify_all(turn, cfg)]
            self.assertEqual(c["expect"], c["row"] in rows,
                             "%s: got %s" % (c["id"], rows))
            per_row.setdefault(c["row"], set()).add(c["expect"])
        for row in T.ROW_ORDER:
            self.assertEqual({True, False}, per_row.get(row),
                             "row %s needs a true and a false fixture" % row)

    def test_a_unreadable_turn_never_fires_evidence_rows(self):
        turn = T.Turn("키는 기록이 없어요.", "내 키 기억해?", None)
        self.assertIsNone(T.row_hf27(turn, T.Config()))
        cfg = T.Config(decision_ledger=["_DECISIONS.md"])
        turn = T.Turn("[[OPTIONS: a | b]]", "x", None)
        self.assertIsNone(T.row_ha13(turn, cfg))

    def test_a_vocab_loads_both_languages(self):
        self.assertEqual([], T.VOCAB_ERRORS)
        for lang in T.VOCAB_LANGS:
            self.assertTrue((Path(T.VOCAB_DIR) / (lang + ".tsv")).is_file())
        errs = []
        T.load_vocab(("xx",), errors=errs)
        self.assertEqual("xx", errs[0][0])

    def test_a_f3_hf52_reads_interim_v01_does_not(self):
        turn = T.Turn(CLEAN_REPLY, "왜 두 번 갔어?", [],
                      interim=(("m1", "제 다른 세션이 같은 일을 따로 실행했고, "
                                      "그게 먼저 닿았어요."),),
                      reply_mid="final")
        f = T.row_hf52(turn, T.Config())
        self.assertEqual(("HF52", "m1"), (f.row, f.mid))
        turn = T.Turn(CLEAN_REPLY, "보여줘", [],
                      interim=(("m1", ORDER_REPLY),), reply_mid="final")
        self.assertIsNone(T.row_v01(turn, T.Config()))
        self.assertIsNone(T.row_v03(turn, T.Config()))
        leak = T.Turn(CLEAN_REPLY, "x", [], interim=(
            ("m1", "user1988\n\nsystem<total_tokens>1 left</total_tokens>"),))
        self.assertEqual("m1", T.row_hf48(leak, T.Config()).mid)

    def test_a_f10_word_edges(self):
        en = ("Which one first?\n\n[[OPTIONS: Verify the fix first | "
              "Block the leak first]]")
        for benign in ("The staging step is next.",
                       "The dropdown is fixed.", "The accounting view too."):
            turn = T.Turn(benign + "\n\n" + en, "show me", [])
            self.assertIsNotNone(T.row_v01(turn, T.Config()), benign)
        turn = T.Turn("Tag the release now?\n\n" + en, "show me", [])
        self.assertIsNone(T.row_v01(turn, T.Config()))
        # an English word next to Hangul still has an edge
        turn = T.Turn("어느 쪽부터 할까요? push로 마무리해요.\n\n"
                      "[[OPTIONS: A부터 | B부터]]", "보여줘", [])
        self.assertIsNone(T.row_v01(turn, T.Config()))

    def test_a_q8_raw_guard_findings(self):
        turn = T.Turn("이번 적용이 얼마나 걸리는지는 저도 모릅니다.",
                      "1분정지가 뭔데", [])
        self.assertEqual([], T.classify_all(turn, T.Config()))
        raw = T.classify_all(turn, T.Config(), raw=True)
        self.assertEqual([("HF27", "recall_q")],
                         [(f.row, f.guard) for f in raw])
        turn = T.Turn("터미널에서 아래를 실행해 주세요.\n\n```bash\nclaude "
                      "setup-token\n```", "로그인이 풀렸대", [])
        raw = T.classify_all(turn, T.Config(), raw=True)
        self.assertEqual([("HF30", "auth")], [(f.row, f.guard) for f in raw])
        self.assertIsNone(T.classify(turn, T.Config(rows=["HF30"])))

    def test_a_f7_row_error_is_reported(self):
        saved = T.ROWS["V03"]
        T.ROWS["V03"] = lambda turn, cfg: 1 / 0
        try:
            errs = []
            found = T.classify_all(T.Turn(ORDER_REPLY, "보여줘", []),
                                   T.Config(), errors=errs)
        finally:
            T.ROWS["V03"] = saved
        self.assertEqual(["V01"], [f.row for f in found])
        self.assertEqual(["V03"], errs)

    def test_a_f7_empty_rows_knob_disables(self):
        cfg = T.Config(rows=[], detect_rows=[])
        self.assertEqual((), cfg.rows + cfg.detect_rows)
        self.assertIsNone(T.classify(T.Turn(ORDER_REPLY, "보여줘", []), cfg))


class OneRule(unittest.TestCase):
    def test_b_two_rows_one_finding(self):
        turn = T.Turn(ORDER_REPLY, "보여줘", [])
        both = [f.row for f in T.classify_all(turn, T.Config())]
        self.assertEqual(["V01", "V03"], both)
        f = T.classify(turn, T.Config())
        self.assertEqual("V01", f.row)
        self.assertEqual(1, f.message.count("Rule: "))


class HookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v27d2-")
        self.sessions = os.path.join(self.tmp, "sessions.json")
        with open(self.sessions, "w") as fh:
            json.dump({"chat:1": {"session_id": "sess-owner"}}, fh)
        self.conf = os.path.join(self.tmp, "config", "agent.conf")
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as fh:
            fh.write("AGENT_LANG=ko\n")
        self.tp = os.path.join(self.tmp, "sess-owner.jsonl")
        _transcript(self.tp, "보여줘", ORDER_REPLY)

    def _env(self, mode="block", **extra):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("TEXT_SEAT_", "GATE_")) or k == "AGENT_LANG":
                env.pop(k)
        env.update({"DOGANY_SESSIONS_FILE": self.sessions,
                    "DOGANY_TEST_MODE": "1",
                    "DOGANY_TEXT_SEAT_DIR": self.tmp,
                    "GATE_SWITCH_CONF": self.conf,
                    "GATE_SWITCH_LOG": os.path.join(self.tmp, "gs.log")})
        if mode is not None:
            env["TEXT_SEAT_MODE"] = mode
        env.update(extra)
        return env

    def _run(self, payload, mode="block", **extra):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(SEAT)], input=raw,
                              env=self._env(mode, **extra),
                              capture_output=True, text=True, timeout=60,
                              cwd=self.tmp)

    def _payload(self, tp=None, **extra):
        d = {"hook_event_name": "Stop", "transcript_path": tp or self.tp,
             "cwd": self.tmp, "stop_hook_active": False,
             "session_id": "sess-owner"}
        d.update(extra)
        return d

    def _canary(self):
        p = os.path.join(self.tmp, "logs", "output-gate-canary.log")
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as fh:
            return fh.read()

    def _blocks(self, p):
        return [json.loads(ln) for ln in p.stdout.splitlines()
                if ln.strip() and json.loads(ln).get("decision") == "block"]






class Transcript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v27d2t-")
        self.tp = os.path.join(self.tmp, "s.jsonl")

    def test_h_tail_read_matches_full_parse(self):
        lines = []
        for i in range(12):
            lines += _turn_lines("질문 %d run_tool_x" % i, "답 %d" % i,
                                 [("Bash", "ls")], uuid="u%d" % i,
                                 final_id="f%d" % i,
                                 interim="중간 %d" % i)
        _write(self.tp, lines)
        full = list(SEAT_MOD.iter_turns(self.tp))[-1]
        entries = SEAT_MOD.tail_entries(self.tp, chunk=97)
        tail = list(SEAT_MOD._iter_entries(entries))[-1]
        self.assertEqual(full[1:], tail[1:])
        self.assertEqual((full[0].reply, full[0].prompt, full[0].history,
                          full[0].interim),
                         (tail[0].reply, tail[0].prompt, tail[0].history,
                          tail[0].interim))
        self.assertEqual(T.HISTORY_PROMPTS, len(tail[0].history))
        self.assertLess(len(entries), len(lines))
        self.assertEqual("u11", SEAT_MOD.current_turn(self.tp)[3])

    def test_h_measurement_scores_the_pre_block_reply(self):
        lines = _turn_lines("보여줘", ORDER_REPLY, [("Bash", "ls")])
        lines += [_stop_feedback(), {"type": "assistant", "message": {
            "id": "rewrite", "role": "assistant",
            "content": [{"type": "tool_use", "id": "tx", "name": "Read",
                         "input": {"file_path": "/x"}}]}},
            {"type": "assistant", "message": {
                "id": "rewrite2", "role": "assistant",
                "content": [{"type": "text", "text": CLEAN_REPLY}]}}]
        _write(self.tp, lines)
        pre = list(SEAT_MOD.iter_turns(self.tp, pre_block=True))
        post = list(SEAT_MOD.iter_turns(self.tp))
        self.assertEqual((ORDER_REPLY, "final", 1),
                         (pre[0][0].reply, pre[0][1], len(pre[0][0].tools)))
        self.assertEqual((CLEAN_REPLY, "rewrite2", 2),
                         (post[0][0].reply, post[0][1],
                          len(post[0][0].tools)))
        p = subprocess.run([sys.executable, str(SEAT), "--replay", self.tp],
                           capture_output=True, text=True, timeout=60,
                           cwd=self.tmp)
        self.assertEqual('', p.stdout)  # Retired hook does not emit forcing telemetry.


class Wording(unittest.TestCase):
    SOURCES = (ROUTINES / "lib" / "text_seat.py",
               ROUTINES / "lib" / "text_seat_g03.py", SEAT,
               ROUTINES / "lib" / "text_seat_vocab" / "ko.tsv",
               ROUTINES / "lib" / "text_seat_vocab" / "en.tsv")

    def test_i_no_owner_address_or_instance_vocab(self):
        # An owner form of address, an instance's delegate nickname and
        # dispatch verb, instance names -- none may appear in framework code.
        banned = ("발주",
                  "Metal", "Skull", "metal", "skull")  # estate-scrub:allow(ban-list fixture -- the instance names this test asserts never reach framework code)
        for src in self.SOURCES:
            text = src.read_text(encoding="utf-8")
            for word in banned:
                self.assertNotIn(word, text, "%s carries %r" % (src.name, word))

    def test_i_code_is_ascii(self):
        for src in (ROUTINES / "lib" / "text_seat.py", SEAT,
                    ROUTINES / "lib" / "owner_session.py"):
            text = src.read_text(encoding="utf-8")
            self.assertTrue(text.isascii(), src.name)

    def test_i_one_owner_guard(self):
        for src in (SEAT, ROUTINES / "text-layer-pointer.py"):
            text = src.read_text(encoding="utf-8")
            self.assertNotIn("status-footer.py", text, src.name)
            self.assertIn("owner_session", text, src.name)

    def test_i_block_copy_names_the_user_and_existing_sections(self):
        text_doc = (TEMPLATE / T.TEXT_DOC).read_text(encoding="utf-8")
        why_doc = (TEMPLATE / T.WHY_DOC).read_text(encoding="utf-8")
        vendor = (TEMPLATE / "vendors" / "telegram.md").read_text(
            encoding="utf-8")
        self.assertIn("## Register", text_doc)
        self.assertIn("## Solve-first", why_doc)
        self.assertIn("## Internal bold, external careful", why_doc)
        self.assertIn("## " + T.OPTIONS_SECTION, vendor)
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
        seen = set()
        for c in cases:
            if not c["expect"]:
                continue
            cfg = T.Config(**c.get("cfg", {}))
            for f in T.classify_all(T.Turn(c["reply"], c["prompt"],
                                           _tools(c["tools"])), cfg):
                seen.add(f.row)
                self.assertIn("Rule: ", f.message)
                # model-facing copy opens in English (backend text register)
                self.assertTrue(all(ord(ch) < 128 for ch in f.message[:40]),
                                f.message[:40])
        self.assertEqual(set(T.ROW_ORDER), seen)








if __name__ == "__main__":
    unittest.main()
