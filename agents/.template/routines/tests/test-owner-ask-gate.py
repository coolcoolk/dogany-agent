#!/usr/bin/env python3
# test-lane: fast
# test-component: owner-ask-gate
# test-tier: 0
# test-prereq: tmp-files, subprocess
# test-new-component: Renames and extends the existing owner-ask gate suite; no extra suite.
# test-covers: agents/.template/routines/lib/kit_readable_facts.py agents/.template/config/i18n/*.json
"""DGN-2032 rev2: the owner-ask gate keys on means TRIED (tool calls).

Real turns (fixtures/dgn2032-owner-ask.json): the 14:07 incident
(2 search + grep -> blocked, try the source first), the same reply after a
WebFetch of the insurer page with one ask (passes), the KIPRIS reply that
fetched other pages but not the source it named (blocked), a dev
agent's search-only Codex reply (blocked), and passing tried-means replies.
Adapter: block / record / re-prompt / non-owner / switch-off / blind.
"""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROUTINES = Path(__file__).resolve().parents[1]
TEMPLATE = ROUTINES.parent
sys.path.insert(0, str(ROUTINES / "lib"))
import output_gate as og

FX = json.loads((ROUTINES / "tests/fixtures/dgn2032-owner-ask.json")
                .read_text(encoding="utf-8"))
CASES = {k: v for k, v in FX.items() if isinstance(v, dict)}


def verdict(case, avail=True):
    return og.owner_ask_verdict(case["reply"], case["blob"], avail)


class Verdict(unittest.TestCase):
    def test_real_and_constructed_cases(self):
        for name, case in CASES.items():
            with self.subTest(name=name):
                v = verdict(case)
                if case["expect"] is None:
                    self.assertIsNone(v)
                    continue
                self.assertEqual(case["expect"], v["state"], v)
                self.assertEqual(case["expect"] in ("shallow", "unopened",
                                                    "multi"), v["block"])

    def test_e2e_a_incident_blocks_with_try_the_source_first(self):
        v = verdict(CASES["incident_1407"])
        self.assertEqual((2, 0, 2), (v["search"], v["fetch"], v["asks"]))
        self.assertTrue(v["block"])
        self.assertIn("try the source first", v["reason"])
        self.assertIn("https://www.kbinsure.co.kr/CG313020001.ec", v["reason"])
        self.assertTrue(v["reason"].isascii() or "‘" not in v["reason"])

    def test_e2e_b_fetch_plus_one_ask_passes(self):
        v = verdict(CASES["e2e_b_after_fetch_one_ask"])
        self.assertEqual(("tried", False, 1), (v["state"], v["block"],
                                              v["asks"]))

    def test_citation_line_alone_never_counts(self):
        case = CASES["incident_1407"]
        self.assertIn("확인한 수단:", case["reply"])
        self.assertTrue(verdict(case)["block"])

    def test_kipris_named_source_must_be_opened(self):
        v = verdict(CASES["kipris_unopened"])
        self.assertEqual(["https://www.kipris.or.kr"], v["unopened"])
        self.assertIn("Not opened: https://www.kipris.or.kr", v["reason"])
        self.assertFalse(verdict(CASES["kipris_browsed"])["block"])

    def test_multi_ask_after_tries_narrows_to_one(self):
        v = verdict(CASES["multi_after_fetch"])
        self.assertTrue(v["block"])
        self.assertIn("ONE specific value", v["reason"])

    def test_record_only_paths(self):
        inc = CASES["incident_1407"]
        blind = og.owner_ask_verdict(inc["reply"], None, True)
        self.assertEqual(("blind", False), (blind["state"], blind["block"]))
        for avail in (None, False):
            v = verdict(inc, avail)
            self.assertEqual(("no_tool", False), (v["state"], v["block"]))
        v = og.owner_ask_verdict(inc["reply"], "TOOL Read /tmp/pet.txt", True)
        self.assertEqual(("no_search", False), (v["state"], v["block"]))

    def test_quoted_code_and_owner_words_are_not_asks(self):
        for text in FX["no_ask"] + ["", None]:
            with self.subTest(text=text):
                self.assertIsNone(og.owner_ask_verdict(
                    text, "TOOL WebSearch ", True))

    def test_means_classes(self):
        blob = FX["means_classes"]
        self.assertEqual((2, 4), og.owner_ask_means(blob))
        self.assertEqual({"example", "b", "c", "d"}, og.owner_ask_opened(blob))


def _turn(tp, reply, tools, sid_attach=True):
    rows = []
    if sid_attach:
        rows.append({"type": "attachment", "attachment": {
            "type": "deferred_tools_delta",
            "addedNames": ["WebFetch", "WebSearch"]}})
    rows.append({"type": "user", "message": {"content": "look it up"}})
    content = [{"type": "tool_use", "id": "t%d" % i, "name": n, "input": inp}
               for i, (n, inp) in enumerate(tools)]
    rows.append({"type": "assistant", "message": {"id": "m1",
                                                  "content": content}})
    rows.append({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": "ok"}]}})
    rows.append({"type": "assistant", "message": {"id": "m2", "content": [
        {"type": "text", "text": reply}]}})
    tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                  + "\n", encoding="utf-8")


SEARCH2 = [("WebSearch", {"query": "a"}), ("WebSearch", {"query": "b"}),
           ("Bash", {"command": "grep -n 39874 /tmp/pet.txt"})]








if __name__ == "__main__":
    unittest.main()
