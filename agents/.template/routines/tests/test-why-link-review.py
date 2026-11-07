#!/usr/bin/env python3
# test-lane: fast
# test-tier: 0
# test-component: retro review of inferred work-to-Why links
# test-prereq: python3 stdlib
# test-new-component: the retro link-review projection and its live-record path are new (FDGN-617); no existing suite owns them
# test-covers: agents/.template/routines/lib/why_net/link_review.py agents/.template/routines/lib/why_net/records.py
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from why_net import intake, link_review, runtime, store
from why_net.frame import judge_work
from why_net.records import TicketlessRecords
from why_net.runtime_intake import write_intake
from term_registry import schema_labels
import retro_slot
import why_owner_turn


class Review(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.records = runtime.state_dir(self.root) / "records"
        self.records.mkdir(parents=True)

    def seed(self, count=9):
        links = [dict(work_key="opaque-%d" % i, node_id="node-%d" % i,
                      node_revision=1, linked_at=count-i, owner_confirmed=False,
                      work_label="Owner request %d" % i, why_label="Why %d" % i,
                      why_candidates=[dict(id="other", revision=2, label="Other Why")])
                 for i in range(count)]
        (self.records / "record.json").write_text(json.dumps(dict(agent_links=links)))
        return links

    def test_live_record_oldest_first_and_seven_max_without_raw_ids(self):
        self.seed()
        rows = link_review.pending(self.root)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["work"], "Owner request 8")
        menu = link_review.menu(rows, {"heading": "Review", "all": "All correct",
                                      "fix": "{n}. Fix"})
        self.assertNotIn("opaque-", menu["text"])
        self.assertNotIn("node-", menu["text"])

    def test_no_unconfirmed_link_has_no_message(self):
        links = self.seed(1)
        links[0]["owner_confirmed"] = True
        (self.records / "record.json").write_text(json.dumps(dict(agent_links=links)))
        self.assertIsNone(link_review.menu(link_review.pending(self.root), {}))

    def test_confirm_fix_and_unlink_write_same_record(self):
        self.seed(3)
        rows = link_review.pending(self.root)
        self.assertTrue(link_review.update(self.root, [rows[0]["link"]], confirmed_at=10))
        rows = link_review.pending(self.root)
        self.assertTrue(link_review.update(self.root, [rows[0]["link"]],
            node=dict(id="other", revision=2, label="Other Why"), confirmed_at=11))
        rows = link_review.pending(self.root)
        self.assertTrue(link_review.update(self.root, [rows[0]["link"]], node=None,
                                           confirmed_at=12))
        state = json.loads((self.records / "record.json").read_text())
        self.assertEqual(len(state["agent_links"]), 2)
        self.assertTrue(all(row["owner_confirmed"] for row in state["agent_links"]))

    def test_unreadable_label_is_skipped_and_logged(self):
        links = self.seed(1)
        links[0]["work_label"] = ""
        (self.records / "record.json").write_text(json.dumps(dict(agent_links=links)))
        logs = []
        self.assertEqual(link_review.pending(self.root, logger=logs.append), [])
        self.assertEqual(len(logs), 1)

    def test_real_intake_frame_link_is_listed_in_retro_plan(self):
        """The frame-to-owning-record path, not a hand-written marker, feeds retro."""
        (self.root / "config/i18n").mkdir(parents=True)
        source_i18n = pathlib.Path(__file__).resolve().parents[4] / "agents/.template/config/i18n"
        for name in ("terms.json", "en.json"):
            shutil.copy2(source_i18n / name, self.root / "config/i18n" / name)
        (self.root / "config/agent.conf").write_text('WHY_NET="on"\n')
        (self.root / "config/why-runtime.json").write_text(json.dumps(
            dict(owner_id="owner-a", runtime="bridge", chat_id=42)))

        session = "link-review-session"
        words = "Please keep the stalled-work monitor reliable."
        transcript = (self.root / "claude/projects" /
                      why_owner_turn.encode_cwd(str(self.root)) / (session + ".jsonl"))
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(dict(type="user", uuid="u1", userType="external",
            sessionId=session, message=dict(role="user", content=words))) + "\n")
        old_config = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "claude")
        self.addCleanup(lambda: (os.environ.pop("CLAUDE_CONFIG_DIR", None)
                                 if old_config is None else
                                 os.environ.__setitem__("CLAUDE_CONFIG_DIR", old_config)))
        with store.canonical(self.root):
            store.append_row(store.TURN_LEDGER, dict(id="otl-000001", at="2026-10-30T00:00:00Z",
                session_id=session, request_id="request-1", sha=why_owner_turn.turn_sha(words),
                chat_id=42))
        locator = dict(session=session, ledger_id="otl-000001", message_uuid="u1")
        unit = dict(work_key="monitor", scope_revision=1,
                    scope_digest="monitor-reliability", targets=["skill/monitor"])
        write_intake(self.root, current=locator, unit=unit, expected_revision=0,
            symptom=words, problem="Stalled work can go unnoticed.",
            why="", direction="Keep the existing monitor reliable.")

        owner = runtime.owner_message(self.root, **locator)
        binding = dict(owner_id="owner-a", runtime="bridge", session_id=session,
                       request_id="request-1", owner_text_hash=owner["source_hash"])
        node = dict(id="why-monitor", revision="confirm-1", status="confirmed",
                    owner_id="owner-a", scope="framework",
                    text="Keep existing monitoring dependable.", confirmation_ref="confirm-1")
        fit = dict(node_id=node["id"], revision=node["revision"], relation="same purpose")
        adapter = TicketlessRecords(self.root, schema_labels(str(self.root), "why_intake_v4"))
        slots = adapter.read_slots(unit["work_key"])
        unit.update(effect_ref="effect:monitor", record_ref=slots["record_uri"],
                    purpose_scope="framework", candidate_fits=[fit])
        facts = {
            "owner:current": owner,
            unit["effect_ref"]: dict(unit, kind="work-effect", owner_id="owner-a",
                                     effect="question", future_behavior=False, provenance="bound"),
            slots["record_uri"]: dict(kind="work-record", owner_id="owner-a",
                work_key=unit["work_key"], slots=slots,
                semantic_digest=intake.digest(intake.semantic_binding(dict(binding, **{
                    key: unit[key] for key in ("work_key", "scope_revision", "scope_digest", "targets")
                }), slots))),
            "confirm-1": dict(kind="node-confirmation", owner_id="owner-a",
                node_id=node["id"], revision=node["revision"],
                wording_hash=intake.digest(node["text"]), scope="framework",
                source_order=1, valid_through=20),
        }
        frame = dict(binding=binding, units=[unit], nodes={node["id"]: node}, as_of=10)
        runtime.publish_snapshot(self.root, frame, facts, [], owner_sources={"owner:current": locator}, records={
            slots["record_uri"]: dict(provider="owning-record", work_key=unit["work_key"],
                                       revision=slots["revision"], slots_hash=intake.digest(slots))})
        loaded, resolver = runtime.load_runtime(self.root, dict(session_id=session))
        verdict = judge_work(dict(loaded, unit=loaded["units"][0]), resolver)
        self.assertEqual(verdict["tree"]["state"], "linked")

        state = adapter.state(unit["work_key"])
        self.assertEqual(len(state.get("agent_links", [])), 1)
        marker = state["agent_links"][0]
        self.assertEqual(marker["work_label"], "\u201c" + words + "\u201d")
        self.assertNotEqual(marker["work_label"], unit["work_key"])
        self.assertIs(marker["owner_confirmed"], False)

        ctx = types.SimpleNamespace(root=str(self.root), now=10,
                                    conf=lambda key: "en" if key == "AGENT_LANG" else "")
        day = retro_slot.Day(__import__("datetime").date(2026, 10, 30))
        plan = retro_slot.compose(ctx, retro_slot.build(ctx, with_kits=False), day)
        review = plan["section_bodies"][retro_slot.LINK_REVIEW_SECTION]
        self.assertIn(words, review)
        self.assertIn(node["text"], review)


if __name__ == "__main__":
    unittest.main()
