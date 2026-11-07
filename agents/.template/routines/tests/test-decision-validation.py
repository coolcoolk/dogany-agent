#!/usr/bin/env python3
# test-lane: fast
# test-tier: 0
# test-component: decision_validation
# test-prereq: tmp-files, sqlite3, subprocess
# test-covers: agents/.template/routines/lib/decision_validation.py agents/.template/routines/lib/decision_validation_sources.py agents/.template/routines/decision-ledger.py
# test-new-component: New read/audit component; capture and confirmation have a separate existing suite.
# one promise: audit snapshots retain the full population and separate judgments from outcomes.
"""Temp-only observation, blind review and acceptance material tests; no sleeps."""
import hashlib
import sqlite3
import time
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# Fixtures never inherit repository/runtime routing or git configuration.
for key in list(os.environ):
    if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        os.environ.pop(key)
HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "decision-ledger.py"
sys.path.insert(0, str(HERE.parent / "lib"))
import decision_validation as v
spec = importlib.util.spec_from_file_location("validation_ledger", HOOK)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)
START = "2026-10-14T00:00:00+09:00"
END = "2026-10-21T00:00:00+09:00"
NOW = "2026-10-21T00:00:00+00:00"
LATER = "2026-10-21T01:00:00+00:00"


class Validation(unittest.TestCase):
    # "No slow work" is bounded by counts, not wall time: a wall-clock cap
    # fails under host contention (load5=23 on 8 CPU read 1.015s) without any
    # code getting slower. Sleeps and subprocess launches are what slow work
    # would add, so both are counted.
    MAX_SUBPROCESS_RUNS = 5

    def setUp(self):
        self.sleeps = []
        self.subprocess_runs = 0
        real_run = subprocess.run
        def counted_run(*args, **kwargs):
            self.subprocess_runs += 1
            return real_run(*args, **kwargs)
        for target, wrapper in (("time.sleep", lambda s=0, *a: self.sleeps.append(s)),
                                ("subprocess.run", counted_run)):
            patcher = mock.patch(target, side_effect=wrapper)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="decision-validation-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger.md"
        self.ledger.write_text("")
        self.details = self.root / "details"
        self.details.mkdir()

    def tearDown(self):
        self.assertEqual([], self.sleeps)
        self.assertLessEqual(self.subprocess_runs, Validation.MAX_SUBPROCESS_RUNS)

    def ref(self, name="receipt"):
        path = self.root / name
        path.write_text("actual receipt data")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def entry(self, n, states=None, kinds=None, **extra):
        data = dict(at="2026-10-15T01:00:00+09:00", kinds=kinds or ["question"],
                    states=states or ["unanswered"], outcome_known=False,
                    input={"target": "existing-target", "version": "v1", "context": "safe pre-outcome context"})
        data.update(extra)
        return ("## dec-%d -- question (2026-10-15)\n" % n +
                "<!-- decision-validation " + json.dumps(data) + " -->\n")

    def population(self, text):
        self.ledger.write_text(text)
        return v.inventory(dl, self.ledger, self.details, START, END, "fixture ledger + details")

    def freeze(self, text):
        return v.sample(self.population(text), "v3.1")

    def judgment(self, ident="dec-1", **extra):
        ident = "case-" + v.digest(["v3.1", [START, END], ident])
        data = dict(id=ident, ask_act="act", decision_owner="agent", distance=3,
                    reason="The existing target path has distance 3.", reviewer="reviewer")
        data.update(extra)
        return data

    def outcome(self, ident="dec-1", **extra):
        data = dict(id=ident, recorded_at=LATER, gold_restorable=True,
                    reviewer="independent reviewer", reason="Restored from source evidence.",
                    target="existing-target", version="v1", decision_owner="agent", distance=3,
                    evidence=self.ref(), ask_needed=False, act_needed=True,
                    asked=False, acted=True)
        data.update(extra)
        return data

    def test_markdown_pending_status_survives_without_annotation(self):
        p = self.population("## dec-990004 -- pending (2026-10-20)\nStatus: pending\n")
        self.assertEqual(["unanswered", "uncollected"], p["records"][0]["states"])
        self.assertEqual({}, p["records"][0]["input"])

    def test_undated_machine_marker_keeps_current_answer_separate(self):
        p = self.population("## dec-1 (2026-10-20)\n<!-- decision-ledger id=dec-1 state=confirmed -->\n")
        self.assertNotIn("answered", p["records"][0]["states"])
        self.assertIn(["answered"], p["records"][0]["current_state"])
        self.assertFalse(p["records"][0]["outcome_known"])

    def test_old_pending_status_carries_into_zero_answer_week(self):
        p = self.population("# dec-386\nStatus: pending (2026-10-12 06:2x)\n")
        self.assertEqual(["dec-386"], [r["id"] for r in p["records"]])
        self.assertEqual("carry-in", p["records"][0]["window_membership"])
        self.assertIn("unanswered", p["records"][0]["states"])

    def test_old_pending_frontmatter_carries_in(self):
        (self.details / "dec-172.md").write_text("---\nid: dec-172\nstatus: pending\nopened: 2026-09-24\n---\n")
        p = self.population("")
        self.assertEqual("carry-in", p["records"][0]["window_membership"])
        self.assertEqual("dec-172", p["records"][0]["id"])

    def test_old_annotated_unanswered_keeps_performance_denominator(self):
        m, b = self.freeze(self.entry(1, at="2026-10-12T01:00:00+09:00"))
        self.assertEqual(["dec-1"], m["selected_ids"])
        r = v.reveal(m, v.assess(m, b, [self.judgment()], now=lambda: NOW), [], now=lambda: LATER)
        self.assertEqual((1, {"U": 1}), (r["unique_denominator"], r["unique_errors"]))
        self.assertEqual(0, r["strata"]["S2/answered"]["denominator"])

    def test_excluded_records_keep_id_and_window_reason(self):
        p = self.population(self.entry(1, ["answered"], at="2026-10-12T01:00:00+09:00",
            outcome_known=True, outcome_at="2026-10-12T02:00:00+09:00"))
        self.assertEqual([dict(id="dec-1", reason="outside-window-without-unresolved-state")], p["excluded"])

    def test_diagnostics_cannot_displace_twelve_blind_performance_cases(self):
        m, b = self.freeze("".join(self.entry(i, outcome_known=i > 12,
            outcome_at="2026-10-16T01:00:00+09:00" if i > 12 else None) for i in range(1, 113)))
        self.assertEqual(12, m["strata"]["S2/unanswered"]["population_n"])
        self.assertEqual(12, len(b["cases"]))
        self.assertFalse(set(m["selected_ids"]) & set(m["diagnostic_ids"]))
        pre = v.assess(m, b, [self.judgment("dec-%d" % i) for i in range(1, 13)], now=lambda: NOW)
        r = v.reveal(m, pre, [], now=lambda: LATER)
        self.assertEqual(12, r["unique_denominator"])
        self.assertEqual(dict(observed=112, diagnostic=100, synthetic=0, performance=12, sampled=12), r["counts"])

    def test_blind_slug_is_opaque_and_restored_only_at_reveal(self):
        ident = "dec-2026-owner-rejected"
        m, b = self.freeze(self.entry(1).replace("dec-1 --", ident + " --"))
        self.assertNotIn(ident, json.dumps(b))
        case = b["cases"][0]["id"]
        self.assertEqual(ident, m["case_ids"][case])
        pre = v.assess(m, b, [self.judgment(ident)], now=lambda: NOW)
        r = v.reveal(m, pre, [self.outcome(ident)], now=lambda: LATER)
        self.assertEqual([], r["case_errors"][ident])
        self.assertEqual(b, self.freeze(self.entry(1).replace("dec-1 --", ident + " --"))[1])

    def test_all_silent_failed_states_and_zero_answers_survive(self):
        states = ["unanswered", "held", "rejected", "not-executed", "cancelled", "failed-run", "uncollected"]
        manifest, _ = self.freeze("".join(self.entry(i, [state], ["question", "execution"])
                                           for i, state in enumerate(states, 1)))
        self.assertEqual(7, len(manifest["selected_ids"]))
        self.assertEqual(0, manifest["strata"]["S2/answered"]["population_n"])
        for state in states:
            for s in ("S1/", "S2/"):
                self.assertEqual(1, manifest["strata"][s + state]["sample_n"])

    def test_seed_reproduces_sorted_stratified_draw(self):
        text = "".join(self.entry(i) for i in range(1, 36))
        manifest, _ = self.freeze(text)
        again, _ = self.freeze("".join(self.entry(i) for i in reversed(range(1, 36))))
        a = manifest["strata"]["S2/unanswered"]
        self.assertEqual(a, again["strata"]["S2/unanswered"])
        self.assertEqual((35, 12), (a["population_n"], a["sample_n"]))
        self.assertNotEqual(a["sample_ids"], v.sample(self.population(text), "v3.1", 99)[0]["strata"]["S2/unanswered"]["sample_ids"])

    def test_native_ask_lifecycle_is_folded_without_new_questions(self):
        asks = self.root / "asks.jsonl"
        rows = [dict(ask="ask-1", ev="create", at="2026-10-15T00:00:00Z"),
                dict(ask="ask-1", ev="shown", at="2026-10-15T01:00:00Z"),
                dict(ask="ask-2", ev="create", at="2026-10-15T00:00:00Z"),
                dict(ask="ask-2", ev="queued", at="2026-10-15T01:00:00Z"),
                dict(ask="ask-old", ev="create", at="2026-10-01T00:00:00Z"),
                dict(ask="ask-old", ev="shown", at="2026-10-01T01:00:00Z"),
                dict(ask="ask-old", ev="answered", at="2026-10-30T01:00:00Z"),
                dict(ask="ask-resolved", ev="create", at="2026-10-01T00:00:00Z"),
                dict(ask="ask-resolved", ev="answered", at="2026-10-16T01:00:00Z")]
        asks.write_text("\n".join(json.dumps(r) for r in rows))
        before = asks.read_bytes()
        p = v.inventory(dl, self.ledger, self.details, START, END, "native ask copy", asks)
        by_id = {r["id"]: r for r in p["records"]}
        self.assertEqual(4, len(by_id))
        self.assertEqual("carry-in", by_id["ask-old"]["window_membership"])
        self.assertIn("unanswered", by_id["ask-old"]["states"])
        self.assertNotIn("answered", by_id["ask-old"]["states"])
        self.assertIn("answered", by_id["ask-resolved"]["states"])
        self.assertIn("unanswered", by_id["ask-1"]["states"])
        self.assertIn("not-executed", by_id["ask-2"]["states"])
        self.assertNotIn("unanswered", by_id["ask-2"]["states"])
        self.assertEqual(before, asks.read_bytes())

    def test_closed_details_mirror_and_duplicate_ids(self):
        (self.details / "dec-1.md").write_text(self.entry(1))
        (self.details / "dec-2.md").write_text(self.entry(2))
        population = self.population(self.entry(1) * 2)
        self.assertEqual(["dec-1", "dec-2"], [r["id"] for r in population["records"]])
        self.assertIn("duplicate-id", population["records"][0]["issues"])
        self.assertIn("uncollected", population["records"][0]["states"])

    def test_conflicting_mirror_never_becomes_blind(self):
        (self.details / "dec-1.md").write_text(self.entry(1, ["held"]))
        manifest, blind = self.freeze(self.entry(1))
        self.assertIn("conflicting-mirrors", manifest["records"][0]["issues"])
        self.assertEqual([], blind["cases"])

    def test_legacy_unknown_time_and_missing_details_are_visible(self):
        self.details.rmdir()
        p = self.population("## dec-1 -- undated legacy\n")
        self.assertEqual(1, len(p["records"]))
        self.assertEqual("details-unavailable", p["defects"][0]["problem"])
        self.assertIn("uncollected", p["records"][0]["states"])

    def test_index_comments_are_not_decisions_and_slugs_remain_distinct(self):
        p = self.population("# A comment mentioning dec-123 and a later date 2026-10-15\n" +
            "## dec-2026-10-15-a (2026-10-15)\ntext\n" +
            "## dec-2026-10-15-b (2026-10-15)\ntext\n")
        self.assertEqual(["dec-2026-10-15-a", "dec-2026-10-15-b"], [r["id"] for r in p["records"]])

    def test_window_excludes_old_but_not_unknown_time(self):
        p = self.population(self.entry(1, states=["answered"], at="2026-10-10T00:00:00+09:00",
            outcome_known=True, outcome_at="2026-10-10T01:00:00+09:00") + self.entry(2))
        self.assertEqual(["dec-2"], [r["id"] for r in p["records"]])

    def test_invalid_metadata_retains_id_as_u(self):
        p = self.population("## dec-1 (2026-10-15)\n<!-- decision-validation {broken} -->\n")
        self.assertIn("uncollected", p["records"][0]["states"])
        self.assertTrue(p["records"][0]["issues"])

    def test_blind_packet_contains_only_input_not_outcome_or_selection(self):
        manifest, blind = self.freeze(self.entry(1) + "- Answer: SECRET_OUTCOME\n- Why chosen: SECRET_REASON\n")
        text = json.dumps(blind)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("states", text)
        self.assertNotIn("strata", text)
        self.assertEqual(["dec-1"], [manifest["case_ids"][c["id"]] for c in blind["cases"]])
        with self.assertRaises(ValueError):
            v.assess(manifest, blind, [self.judgment(outcome="secret")])

    def test_known_and_synthetic_do_not_become_blind_performance(self):
        manifest, blind = self.freeze(self.entry(1, outcome_known=True, outcome_at="2026-10-16T01:00:00+09:00") +
                                      self.entry(2, synthetic=True))
        self.assertEqual([], blind["cases"])
        self.assertEqual(["dec-1"], manifest["diagnostic_ids"])
        self.assertEqual(["dec-2"], manifest["synthetic_ids"])
        self.assertEqual([], manifest["selected_ids"])

    def test_uhheavy_and_unique_denominator(self):
        manifest, blind = self.freeze(self.entry(1, ["uncollected", "failed-run"], ["execution", "question"]))
        pre = v.assess(manifest, blind, [], now=lambda: NOW)
        report = v.reveal(manifest, pre, [], now=lambda: LATER)
        self.assertEqual(1, report["unique_denominator"])
        self.assertEqual({"U": 1}, report["unique_errors"])
        self.assertEqual("indeterminate", report["strata"]["S1/failed-run"]["verdict"])
        self.assertFalse(report["M2_complete"] or report["M3_complete"])

    def test_reveal_binds_assessment_and_rejects_early_duplicate_outcomes(self):
        manifest, blind = self.freeze(self.entry(1))
        pre = v.assess(manifest, blind, [self.judgment()], now=lambda: NOW)
        with self.assertRaises(ValueError):
            v.reveal(manifest, pre, [self.outcome(recorded_at=START)], now=lambda: LATER)
        with self.assertRaises(ValueError):
            v.reveal(manifest, pre, [self.outcome(), self.outcome()], now=lambda: LATER)
        manifest["rule_version"] = "other"
        with self.assertRaises(ValueError):
            v.reveal(manifest, pre, [], now=lambda: LATER)

    def test_miss_and_unnecessary_counts_use_prejudgment_and_restored_gold(self):
        manifest, blind = self.freeze(self.entry(1))
        pre = v.assess(manifest, blind, [self.judgment()], now=lambda: NOW)
        report = v.reveal(manifest, pre, [self.outcome(ask_needed=True, act_needed=False)], now=lambda: LATER)
        self.assertEqual(["miss-question", "unnecessary-execution"], report["case_errors"]["dec-1"])
        pre = v.assess(manifest, blind, [self.judgment(ask_act="ask")], now=lambda: NOW)
        report = v.reveal(manifest, pre, [self.outcome()], now=lambda: LATER)
        self.assertEqual(["miss-execution", "over-ask"], report["case_errors"]["dec-1"])
        report = v.reveal(manifest, pre, [self.outcome(gold_restorable=False)], now=lambda: LATER)
        self.assertEqual(["U"], report["case_errors"]["dec-1"])

    def test_outcome_pairing_and_receipt_defects_are_u(self):
        manifest, blind = self.freeze(self.entry(1))
        pre = v.assess(manifest, blind, [self.judgment()], now=lambda: NOW)
        report = v.reveal(manifest, pre, [self.outcome(target="other")], now=lambda: LATER)
        self.assertEqual(["U"], report["case_errors"]["dec-1"])
        self.assertEqual(["target-mismatch"], report["input_defects"]["dec-1"])
        gold = self.outcome()
        Path(gold["evidence"]["path"]).write_text("wrong receipt")
        report = v.reveal(manifest, pre, [gold], now=lambda: LATER)
        self.assertEqual(["U"], report["case_errors"]["dec-1"])

    def test_proof_evidence_is_separate_and_carry_over_never_done(self):
        ref = self.ref()
        text = self.entry(1, runs=[dict(id="real-run", status="done", receipt=ref)],
                          proofs=[dict(product="devkit-0.1", criterion="DK-0.1-01", evidence=[ref])],
                          disposition=dict(state="carry-over", destination="next", owner_confirmation=ref,
                                           remaining_impact="User flow still missing.",
                                           define=dict(pointed_line="line", how="how", what="what", done="done")))
        products = v.acceptance(self.population(text)["records"])
        self.assertFalse(products["devkit-0.1"]["dispositions"][0]["counts_as_done"])
        self.assertTrue(products["devkit-0.1"]["dispositions"][0]["supported"])
        self.assertNotIn("DK-0.1-01", products["devkit-0.1"]["missing_criteria"])
        self.assertIn("DK-0.1-01", products["devkit-0.1"]["unfinished_criteria"])
        self.assertEqual(11, len(products["framework-2.7"]["missing_criteria"]))
        self.assertEqual(18, len(products["agent-dev-0.1"]["missing_criteria"]))
        Path(ref["path"]).write_text("different candidate")
        self.assertIn("DK-0.1-01", v.acceptance(self.population(text)["records"])["devkit-0.1"]["missing_criteria"])

    def test_failed_run_and_unsupported_cancel_remain_unfinished(self):
        records = self.population(self.entry(1, runs=[dict(id="run", status="failed")],
                    proofs=[dict(product="framework-2.7", criterion="AC5", evidence=[self.ref()])],
                    disposition=dict(state="cancel")))["records"]
        self.assertIn("failed-run", records[0]["states"])
        product = v.acceptance(records)["framework-2.7"]
        self.assertIn("AC5", product["missing_criteria"])
        self.assertFalse(product["dispositions"][0]["supported"])
        manifest, blind = self.freeze(self.entry(2, disposition=dict(state="cancel")))
        pre = v.assess(manifest, blind, [self.judgment("dec-2")], now=lambda: NOW)
        report = v.reveal(manifest, pre, [], now=lambda: LATER)
        self.assertEqual("cancel", report["dispositions"][0]["detail"]["state"])

    def test_cli_outcomes_cannot_open_without_persisted_assessment(self):
        self.population(self.entry(1))
        bundle = self.root / "bundle"
        def run(*args):
            return subprocess.run([sys.executable, str(HOOK), "validation", *args],
                                  capture_output=True, text=True, timeout=60)
        result = run("snapshot", "--ledger", str(self.ledger), "--details", str(self.details),
                     "--start", START, "--end", END, "--access-scope", "fixture", "--rule-version", "v3.1", "--out", str(bundle))
        self.assertEqual(0, result.returncode, result.stderr)
        outcomes = self.root / "outcomes.json"
        outcomes.write_text("[]")
        self.assertEqual(2, run("reveal", "--bundle", str(bundle), "--outcomes", str(outcomes)).returncode)
        judgments = self.root / "judgments.json"
        judgments.write_text(json.dumps([self.judgment()]))
        self.assertEqual(0, run("assess", "--bundle", str(bundle), "--judgments", str(judgments)).returncode)
        self.assertEqual(2, run("assess", "--bundle", str(bundle), "--judgments", str(judgments)).returncode)
        self.assertEqual(0, run("reveal", "--bundle", str(bundle), "--outcomes", str(outcomes)).returncode)
        self.assertTrue((bundle / "acceptance.json").exists())


class NativeSources(unittest.TestCase):
    entry = Validation.entry
    population = Validation.population
    # Reuse fixture helpers; the original tests run only once.
    def setUp(self):
        Validation.setUp(self)
        import decision_validation_sources
        self.adapter = decision_validation_sources

    def tearDown(self):
        Validation.tearDown(self)

    def write_runs(self, events):
        path = self.root / "runs.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events))
        return path

    def test_known_id_missing_event_remains_u_performance(self):
        path = self.write_runs([dict(id="dispatch-uncollected-known", at="2026-10-20T12:00:00Z", surface="dispatch")])
        p = v.inventory(dl, self.ledger, self.details, START, END, "partial run", runs=path)
        m, b = v.sample(p, "v3.1")
        self.assertEqual(["dispatch-uncollected-known"], m["selected_ids"])
        self.assertEqual("dispatch-uncollected-known", p["defects"][0]["id"])
        r = v.reveal(m, v.assess(m, b, [], now=lambda: NOW), [], now=lambda: LATER)
        self.assertEqual((1, {"U": 1}), (r["unique_denominator"], r["unique_errors"]))

    def test_incomplete_event_after_old_closed_run_remains_observed(self):
        path = self.write_runs([
            dict(id="reused-partial", event="open", surface="dispatch", at="2026-10-01T01:00:00Z"),
            dict(id="reused-partial", event="close", state="done", at="2026-10-01T02:00:00Z"),
            dict(id="reused-partial", surface="dispatch", at="2026-10-20T12:00:00Z")])
        p = v.inventory(dl, self.ledger, self.details, START, END, "partial retry", runs=path)
        self.assertEqual(["reused-partial"], [r["id"] for r in p["records"]])
        self.assertIn("unreadable-run-row", p["records"][0]["issues"])
        m, _ = v.sample(p, "v3.1")
        self.assertEqual(["reused-partial"], m["selected_ids"])

    def test_known_id_missing_time_remains_identifiable(self):
        path = self.write_runs([dict(id="partial-time", event="open", surface="dispatch")])
        p = v.inventory(dl, self.ledger, self.details, START, END, "partial time", runs=path)
        self.assertEqual(["partial-time"], p["undated_ids"])
        self.assertIn("run-time-unknown", p["records"][0]["issues"])

    def test_unidentified_rows_have_separate_observation_count(self):
        path = self.write_runs([{}, "bad", []])
        p = v.inventory(dl, self.ledger, self.details, START, END, "unidentified", runs=path)
        self.assertEqual(3, p["unidentified_observations_n"])
        self.assertEqual([], p["records"])

    def test_kit_future_snapshot_cannot_supply_past_answer(self):
        path = self.root / "kit.sqlite"
        with sqlite3.connect(path) as c:
            c.executescript("""CREATE TABLE ticket(id,human_label,ulid,created_at,status,updated_at);
            CREATE TABLE decision_overlay(ticket_id,state);
            CREATE TABLE decision_action(target,action,status,act_id,decided_at);
            INSERT INTO ticket VALUES(1,'dec-1','ULID','2026-10-20T01:00:00Z','open','2026-10-20T01:00:00Z');
            INSERT INTO decision_overlay VALUES(1,'pending');""")
        first = self.adapter.kit_records(path, v.timestamp(END), v.timestamp)[0]
        self.assertIn("unanswered", first["states"])
        with sqlite3.connect(path) as c:
            c.execute("UPDATE decision_overlay SET state='confirmed'")
            c.execute("UPDATE ticket SET updated_at='2026-10-22T12:00:00Z'")
        later = self.adapter.kit_records(path, v.timestamp(END), v.timestamp)[0]
        self.assertEqual(["uncollected"], later["states"])
        self.assertEqual("confirmed", later["current_state"]["overlay"])
        self.assertIn("kit-state-at-cutoff-unknown", later["issues"])

    def test_native_offset_and_failed_retry_preserve_real_time(self):
        self.assertEqual(v.timestamp("2026-10-15T01:00:00+09:00"),
                         v.timestamp("2026-10-15T01:00:00+0900"))
        events = [dict(id="run", event="open", surface="dispatch", at="2026-10-15T01:00:00+0900"),
                  dict(id="run", event="close", state="failed", at="2026-10-15T02:00:00+0900"),
                  dict(id="run", event="open", surface="dispatch", at="2026-10-16T01:00:00+0900"),
                  dict(id="run", event="close", state="done", at="2026-10-16T02:00:00+0900")]
        path = self.write_runs(events)
        records, defects = self.adapter.terminal_records(path.read_text(), v.timestamp(END), v.timestamp)
        self.assertFalse(defects)
        self.assertEqual(["failed", "done"], [r["status"] for r in records[0]["runs"]])
        self.assertIn("failed-run", records[0]["states"])

    def test_pending_run_carries_in_and_future_result_is_withheld(self):
        path = self.write_runs([
            dict(id="run", event="open", surface="dispatch", at="2026-10-13T01:00:00+0900"),
            dict(id="run", event="close", state="failed", at="2026-10-22T01:00:00+0900")])
        result = v.inventory(dl, self.ledger, self.details, START, END, "run fixture", runs=path)
        self.assertEqual("carry-in", result["records"][0]["window_membership"])
        self.assertNotIn("failed-run", result["records"][0]["states"])
        self.assertEqual("running", result["records"][0]["runs"][0]["status"])

    def test_kit_all_actions_and_read_only_bytes(self):
        path = self.root / "kit.sqlite"
        with sqlite3.connect(path) as conn:
            conn.executescript("""CREATE TABLE ticket(id,human_label,ulid,created_at,status,updated_at);
            CREATE TABLE decision_overlay(ticket_id,state);
            CREATE TABLE decision_action(target,action,status,act_id,decided_at);
            INSERT INTO ticket VALUES(1,'dec-1','ULID','2026-10-15T01:00:00Z','parked','2026-10-15T01:00:00Z');
            INSERT INTO decision_overlay VALUES(1,'pending');
            INSERT INTO decision_action VALUES(1,'hold','pending','a','2026-10-15T02:00:00Z');
            INSERT INTO decision_action VALUES(1,'reject','failed:adapter','b','2026-10-16T02:00:00Z');""")
        before = path.read_bytes()
        result = v.inventory(dl, self.ledger, self.details, START, END, "kit fixture", kit_db=path)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual({"uncollected", "unanswered", "held", "rejected", "not-executed", "failed-run"},
                         set(result["records"][0]["states"]))

    def test_linked_run_uses_original_judgment_denominator(self):
        self.ledger.write_text(self.entry(1, kinds=["execution"],
                              runs=[dict(id="native-run", status="running")]))
        path = self.write_runs([
            dict(id="native-run", event="open", surface="dispatch", at="2026-10-15T01:00:00+0900"),
            dict(id="native-run", event="close", state="failed", at="2026-10-16T01:00:00+0900")])
        population = v.inventory(dl, self.ledger, self.details, START, END, "linked fixture", runs=path)
        self.assertEqual(["dec-1"], [r["id"] for r in population["records"]])
        self.assertIn("failed-run", population["records"][0]["states"])
        manifest, _ = v.sample(population, "v3.1")
        self.assertEqual(["dec-1"], manifest["selected_ids"])

    def test_corrupt_and_missing_start_receipts_remain_defects(self):
        path = self.write_runs([dict(id="orphan", event="close", state="done",
                                     at="2026-10-15T01:00:00+0900")])
        path.write_text(path.read_text() + "\ncorrupt\n")
        population = v.inventory(dl, self.ledger, self.details, START, END, "missing fixture", runs=path)
        self.assertEqual({"unreadable-run-row", "missing-start-receipt"},
                         {d["problem"] for d in population["defects"]})
        self.assertIn("uncollected", population["records"][0]["states"])

    def test_legacy_snapshot_cli_accepts_native_sources(self):
        path = self.write_runs([dict(id="run", event="open", surface="dispatch",
                                     at="2026-10-15T01:00:00+0900")])
        result = v.cli(dl, ["snapshot", "--ledger", str(self.ledger), "--runs", str(path),
                "--start", START, "--end", END, "--access-scope", "native fixture",
                "--rule-version", "v3.1", "--out", str(self.root / "bundle")])
        self.assertEqual(0, result)
        manifest = v.read_json(self.root / "bundle/manifest.private.json")
        self.assertEqual(["run"], manifest["selected_ids"])
        self.assertEqual([], manifest["diagnostic_ids"])

    def run_population(self, events):
        return v.inventory(dl, self.ledger, self.details, START, END, "lifecycle evidence", runs=self.write_runs(events))

    def assert_u(self, population, ident):
        manifest, blind = v.sample(population, "v3.1")
        report = v.reveal(manifest, v.assess(manifest, blind, []), [])
        self.assertIn(ident, manifest["selected_ids"])
        self.assertEqual(["U"], report["case_errors"][ident])
        row = next(r for r in population["records"] if r["id"] == ident)
        self.assertFalse(row["outcome_known"])
        self.assertTrue(row["outcome_unknown_reason"])
        return row

    def test_heartbeat_only_keeps_identity_in_u(self):
        p = self.run_population([dict(id="beat", event="beat", at="2026-10-20T12:00:00Z")])
        row = self.assert_u(p, "beat")
        self.assertIn("missing-start-receipt", row["issues"])

    def test_open_without_surface_keeps_identity_in_u(self):
        p = self.run_population([dict(id="open", event="open", at="2026-10-20T12:00:00Z")])
        row = self.assert_u(p, "open")
        self.assertIn("unknown-run-surface", row["issues"])

    def test_unknown_surface_is_visible_in_u(self):
        p = self.run_population([dict(id="cron", event="open", surface="cron", at="2026-10-20T12:00:00Z")])
        self.assert_u(p, "cron")

    def test_unfinished_native_run_is_u(self):
        p = self.run_population([dict(id="run", event="open", surface="dispatch", at="2026-10-20T12:00:00Z")])
        self.assert_u(p, "run")

    def test_known_old_result_cannot_hide_current_retry(self):
        p = self.run_population([
            dict(id="retry", event="open", surface="dispatch", at="2026-10-12T10:00:00Z"),
            dict(id="retry", event="close", state="done", at="2026-10-12T11:00:00Z"),
            dict(id="retry", event="open", surface="dispatch", at="2026-10-20T12:00:00Z")])
        row = self.assert_u(p, "retry")
        self.assertEqual(["done", "running"], [r["status"] for r in row["runs"]])

    def test_partial_heartbeat_retry_after_done_is_u(self):
        p = self.run_population([
            dict(id="retry", event="open", surface="dispatch", at="2026-10-12T10:00:00Z"),
            dict(id="retry", event="close", state="done", at="2026-10-12T11:00:00Z"),
            dict(id="retry", event="beat", at="2026-10-20T12:00:00Z")])
        self.assert_u(p, "retry")

    def test_unknown_close_result_is_u(self):
        p = self.run_population([
            dict(id="run", event="open", surface="dispatch", at="2026-10-20T10:00:00Z"),
            dict(id="run", event="close", at="2026-10-20T11:00:00Z")])
        self.assert_u(p, "run")

    def test_undated_close_cannot_prove_known_outcome(self):
        p = self.run_population([
            dict(id="run", event="open", surface="dispatch", at="2026-10-20T10:00:00Z"),
            dict(id="run", event="close", state="done")])
        self.assert_u(p, "run")

    def test_dated_completed_run_is_diagnostic(self):
        p = self.run_population([
            dict(id="run", event="open", surface="dispatch", at="2026-10-20T10:00:00Z"),
            dict(id="run", event="close", state="done", at="2026-10-20T11:00:00Z")])
        self.assertEqual(["run"], v.sample(p, "v3.1")[0]["diagnostic_ids"])

    def test_terminal_events_use_event_time_order(self):
        p = self.run_population([
            dict(id="run", event="close", state="done", at="2026-10-20T11:00:00Z"),
            dict(id="run", event="open", surface="dispatch", at="2026-10-20T10:00:00Z")])
        self.assertTrue(p["records"][0]["outcome_known"])

    def test_old_markdown_answer_in_window_is_member(self):
        p = self.population("## dec-1 (2026-10-12)\nRaised: 2026-10-12 10:00\nStatus: confirmed 2026-10-20 10:00\n")
        self.assertEqual(["dec-1"], [r["id"] for r in p["records"]])
        row = p["records"][0]
        self.assertEqual("dec-1", row["id"])
        self.assertEqual(v.timestamp("2026-10-12T10:00:00+09:00").isoformat(), row["created_at"])
        self.assertEqual(v.timestamp("2026-10-20T10:00:00+09:00").isoformat(), row["last_observed_at"])

    def test_markdown_future_answer_keeps_past_unknown(self):
        p = self.population("## dec-1 (2026-10-20)\nRaised: 2026-10-20 10:00\n<!-- decision-ledger id=dec-1 state=confirmed -->\nStatus: confirmed 2026-10-22 10:00\n")
        row = self.assert_u(p, "dec-1")
        self.assertNotIn("answered", row["states"])
        self.assertIn(["answered"], row["current_state"])

    def test_markdown_answer_exactly_at_cutoff_is_withheld(self):
        p = self.population("## dec-1 (2026-10-20)\nStatus: confirmed 2026-10-21 00:00\n")
        row = self.assert_u(p, "dec-1")
        self.assertNotIn("answered", row["states"])

    def test_markdown_fractional_cutoff_is_u(self):
        # DGN-2202: 15:30 UTC is after the midnight KST cutoff.
        p = self.population("## dec-1 (2026-10-20)\n"
                            "Status: confirmed 2026-10-20T15:30:00.123Z\n")
        row = self.assert_u(p, "dec-1")
        self.assertIsNone(row["outcome_at"])
        self.assertNotIn("answered", row["states"])
        manifest, blind = v.sample(p, "v3.1")
        report = v.reveal(manifest, v.assess(manifest, blind, []), [])
        self.assertEqual({"U": 1}, report["unique_errors"])

    def test_markdown_offset_controls_preserve_full_time(self):
        for clock in ("2026-10-20T14:30:00Z", "2026-10-20T23:30:00+09:00",
                      "2026-10-20T23:30:00+0900", "2026-10-20T09:30:00-05:00",
                      "2026-10-20T14:30:00.123Z", "2026-10-20T23:30:00.123+09:00",
                      "2026-10-20T23:30:00.123+0900", "2026-10-20T09:30:00.123-05:00"):
            with self.subTest(clock=clock):
                p = self.population("## dec-1 (2026-10-20)\nStatus: confirmed " + clock + "\n")
                row = p["records"][0]
                self.assertEqual(v.timestamp(clock).isoformat(), row["outcome_at"])
                self.assertEqual(row["outcome_at"], row["last_observed_at"])
                self.assertTrue(row["outcome_known"])
                self.assertEqual(["dec-1"], v.sample(p, "v3.1")[0]["diagnostic_ids"])

    def test_old_undatable_markdown_answer_is_u(self):
        p = self.population("## dec-1 (2026-10-12)\nStatus: confirmed unknown\n")
        self.assert_u(p, "dec-1")

    def test_known_flag_without_dated_outcome_is_u(self):
        self.assert_u(self.population(self.entry(1, outcome_known=True)), "dec-1")

    def test_undated_observed_list_is_u_with_reason(self):
        # DGN-2203: an explicit uncollected observation defeats a known result.
        p = self.population(self.entry(1, states=["answered"], outcome_known=True,
            outcome_at="2026-10-16T01:00:00+09:00", observed_at=[None]))
        row = self.assert_u(p, "dec-1")
        self.assertEqual("outcome-state-uncertain", row["outcome_unknown_reason"])
        self.assertIn("last-observation-time-unknown", row["issues"])
        manifest, blind = v.sample(p, "v3.1")
        report = v.reveal(manifest, v.assess(manifest, blind, []), [])
        self.assertEqual({"U": 1}, report["unique_errors"])

    def test_undated_observed_list_prevents_old_outcome_exclusion(self):
        p = self.population(self.entry(1, states=["answered"], outcome_known=True,
            at="2026-10-12T01:00:00+09:00", outcome_at="2026-10-12T02:00:00+09:00",
            observed_at=["2026-10-12T02:00:00+09:00", None]))
        self.assertEqual([], p["excluded"])
        self.assert_u(p, "dec-1")

    def test_optional_or_dated_observed_list_remains_diagnostic(self):
        for extra in ({}, {"observed_at": []},
                      {"observed_at": ["2026-10-15T01:00:00+09:00"]}):
            with self.subTest(extra=extra):
                p = self.population(self.entry(1, states=["answered"], outcome_known=True,
                    outcome_at="2026-10-16T01:00:00+09:00", **extra))
                row = p["records"][0]
                self.assertTrue(row["outcome_known"])
                self.assertIsNone(row["outcome_unknown_reason"])
                self.assertNotIn("last-observation-time-unknown", row["issues"])
                self.assertEqual(["dec-1"], v.sample(p, "v3.1")[0]["diagnostic_ids"])

    def test_current_annotation_cannot_hide_uncertain_native_state(self):
        self.ledger.write_text(self.entry(1, outcome_known=True, outcome_at="2026-10-16T01:00:00+09:00",
                                         runs=[dict(id="native", status="running")]))
        p = self.run_population([dict(id="native", event="beat", at="2026-10-20T12:00:00Z")])
        self.assert_u(p, "dec-1")

    def test_future_only_run_retains_exclusion_identity(self):
        p = self.run_population([dict(id="future", event="open", surface="dispatch", at="2026-10-22T12:00:00Z")])
        self.assertEqual([dict(id="future", reason="created-after-window")], p["excluded"])

    def test_old_kit_action_in_window_is_member(self):
        path = self.root / "old-kit.sqlite"
        with sqlite3.connect(path) as c:
            c.executescript("""CREATE TABLE ticket(id,human_label,ulid,created_at,status,updated_at);
            CREATE TABLE decision_overlay(ticket_id,state);
            CREATE TABLE decision_action(target,action,status,act_id,decided_at);
            INSERT INTO ticket VALUES(1,'DGN-017','ULID','2026-10-12T01:00:00Z','open','2026-10-20T03:00:00Z');
            INSERT INTO decision_overlay VALUES(1,'confirmed');
            INSERT INTO decision_action VALUES(1,'approve','applied','a','2026-10-20T02:00:00Z');""")
        p = v.inventory(dl, self.ledger, self.details, START, END, "old kit", kit_db=path)
        self.assertEqual(["DGN-017"], [r["id"] for r in p["records"]])

    def test_action_without_overlay_keeps_kit_identity(self):
        path = self.root / "action-kit.sqlite"
        with sqlite3.connect(path) as c:
            c.executescript("""CREATE TABLE ticket(id,human_label,ulid,created_at,status,updated_at);
            CREATE TABLE decision_overlay(ticket_id,state);
            CREATE TABLE decision_action(target,action,status,act_id,decided_at);
            INSERT INTO ticket VALUES(1,'DGN-017','ULID','2026-10-12T01:00:00Z','open','2026-10-12T01:00:00Z');
            INSERT INTO decision_action VALUES(1,'approve','pending','a','2026-10-20T02:00:00Z');""")
        p = v.inventory(dl, self.ledger, self.details, START, END, "action kit", kit_db=path)
        self.assert_u(p, "DGN-017")

    def ask_population(self, rows):
        path = self.root / "asks.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows))
        return v.inventory(dl, self.ledger, self.details, START, END, "ask lifecycle", asks=path)

    def test_answer_without_ask_start_is_u(self):
        p = self.ask_population([dict(ask="ask-orphan", ev="answered", at="2026-10-20T10:00:00Z")])
        self.assert_u(p, "ask-orphan")

    def test_duplicate_ask_create_is_u(self):
        p = self.ask_population([
            dict(ask="ask-dup", ev="create", at="2026-10-20T10:00:00Z"),
            dict(ask="ask-dup", ev="create", at="2026-10-20T11:00:00Z"),
            dict(ask="ask-dup", ev="answered", at="2026-10-20T12:00:00Z")])
        self.assert_u(p, "ask-dup")

    def test_undated_ask_state_is_u(self):
        p = self.ask_population([
            dict(ask="ask-unknown", ev="create", at="2026-10-12T10:00:00Z"),
            dict(ask="ask-unknown", ev="answered")])
        self.assert_u(p, "ask-unknown")

    def test_future_only_ask_retains_exclusion_identity(self):
        p = self.ask_population([dict(ask="ask-future", ev="create", at="2026-10-22T10:00:00Z")])
        self.assertEqual([dict(id="ask-future", reason="created-after-window")], p["excluded"])

    def test_unknown_ask_event_retains_identity(self):
        p = self.ask_population([dict(ask="ask-beat", ev={"unknown": 1}, at="2026-10-20T10:00:00Z")])
        self.assert_u(p, "ask-beat")

    def test_future_annotated_answer_is_past_u(self):
        p = self.population(self.entry(1, states=["answered"], outcome_known=True,
                                       outcome_at="2026-10-22T10:00:00Z"))
        row = self.assert_u(p, "dec-1")
        self.assertNotIn("answered", row["states"])

    def test_later_transition_invalidates_older_outcome_claim(self):
        p = self.population(self.entry(1, outcome_known=True, outcome_at="2026-10-15T02:00:00+09:00",
                                       last_observed_at="2026-10-20T10:00:00Z"))
        self.assert_u(p, "dec-1")

    def test_old_outcome_with_in_window_transition_stays_u(self):
        p = self.population(self.entry(1, states=["held"], at="2026-10-12T01:00:00+09:00",
            outcome_known=True, outcome_at="2026-10-12T02:00:00+09:00",
            last_transition_at="2026-10-20T03:00:00+09:00"))
        row = self.assert_u(p, "dec-1")
        self.assertEqual(v.timestamp("2026-10-20T03:00:00+09:00").isoformat(), row["last_observed_at"])
        self.assertEqual([], p["excluded"])

    def test_in_window_outcome_before_transition_stays_u(self):
        p = self.population(self.entry(1, states=["held"], outcome_known=True,
            outcome_at="2026-10-15T02:00:00+09:00",
            last_transition_at="2026-10-20T03:00:00+09:00"))
        self.assert_u(p, "dec-1")

    def test_unparseable_transition_retains_old_id_with_reason(self):
        p = self.population(self.entry(1, states=["held"], at="2026-10-12T01:00:00+09:00",
            outcome_known=True, outcome_at="2026-10-12T02:00:00+09:00",
            last_transition_at="not-a-date"))
        row = self.assert_u(p, "dec-1")
        self.assertIn("last-transition-time-unknown", row["issues"])

    def test_transition_before_outcome_remains_diagnostic(self):
        p = self.population(self.entry(1, states=["answered"], outcome_known=True,
            outcome_at="2026-10-20T04:00:00+09:00",
            last_transition_at="2026-10-20T03:00:00+09:00"))
        self.assertEqual(["dec-1"], v.sample(p, "v3.1")[0]["diagnostic_ids"])

    def test_post_window_transition_preserves_past_diagnostic(self):
        p = self.population(self.entry(1, states=["answered"], outcome_known=True,
            outcome_at="2026-10-20T04:00:00+09:00",
            last_transition_at="2026-10-22T03:00:00+09:00"))
        self.assertEqual(["dec-1"], v.sample(p, "v3.1")[0]["diagnostic_ids"])

    def test_real_writer_dated_confirmation_is_diagnostic(self):
        (self.root / "config").mkdir()
        (self.root / "config/agent.conf").write_text(
            "DECISION_LEDGER_PATH=ledger.md\nDECISION_LEDGER_DIR=details\n")
        menu = dict(question="Which option?", labels=["Option A", "Option B"],
                    recommended="1", **{"class": "What"},
                    not_how="The owner chooses the outcome.", kind="scope")
        moment = v.dt.datetime.fromisoformat("2026-10-20T10:00:00+09:00")
        with mock.patch.object(dl, "ROOT", str(self.root)):
            record = dl.write_pending(str(self.ledger), str(self.details), menu, moment)
            pending = dl.record_for(record["id"])
            self.assertEqual("pending", pending["state"])
            self.assertTrue(dl.confirm(str(self.ledger), pending, "Option A", moment))
            self.assertEqual("confirmed", dl.record_for(record["id"])["state"])
            p = v.inventory(dl, self.ledger, self.details, START, END, "real writer")
        self.assertEqual([record["id"]], v.sample(p, "v3.1")[0]["diagnostic_ids"])
        self.assertEqual(moment.astimezone(v.dt.timezone.utc).isoformat(), p["records"][0]["outcome_at"])
        self.assertNotIn("markdown-state-at-cutoff-unknown", p["records"][0]["issues"])

    def test_mirror_status_must_match_marker_state(self):
        (self.details / "dec-1.md").write_text("# dec-1\nStatus: pending 2026-10-20 10:00\n")
        p = self.population("## dec-1 (2026-10-20)\n<!-- decision-ledger id=dec-1 state=confirmed -->\n")
        row = self.assert_u(p, "dec-1")
        self.assertNotIn("answered", row["states"])

    def test_stale_pending_marker_does_not_borrow_older_pending_status(self):
        p = self.population(
            "## dec-1 (2026-10-20)\n"
            "Status: pending 2026-10-20 09:00\n"
            "Status: confirmed 2026-10-20 10:00\n"
            "<!-- decision-ledger id=dec-1 state=pending -->\n")
        row = self.assert_u(p, "dec-1")
        self.assertIn("markdown-state-at-cutoff-unknown", row["issues"])
        self.assertIn("unanswered", row["states"])

    def test_mirror_status_cannot_resolve_another_id(self):
        (self.details / "dec-2.md").write_text("# dec-2\nStatus: confirmed 2026-10-20 10:00\n")
        p = self.population("## dec-1 (2026-10-20)\n<!-- decision-ledger id=dec-1 state=confirmed -->\n")
        row = self.assert_u(p, "dec-1")
        self.assertIsNone(row["outcome_at"])

    def test_mirror_future_confirmation_cannot_supply_past_outcome(self):
        (self.details / "dec-1.md").write_text("# dec-1\nStatus: confirmed 2026-10-22 10:00\n")
        p = self.population("## dec-1 (2026-10-20)\n<!-- decision-ledger id=dec-1 state=confirmed -->\n")
        row = self.assert_u(p, "dec-1")
        self.assertNotIn("answered", row["states"])

    def test_markdown_future_reopen_preserves_dated_diagnostic(self):
        p = self.population("## dec-1\nRaised: 2026-10-12 10:00\n"
            "Status: confirmed 2026-10-20 10:00\nStatus: pending 2026-10-22 10:00\n")
        self.assertEqual(["dec-1"], v.sample(p, "v3.1")[0]["diagnostic_ids"])
        self.assertNotIn("unanswered", p["records"][0]["states"])
        self.assertIn(["answered", "unanswered"], p["records"][0]["current_state"])

    def test_markdown_in_window_reopen_invalidates_dated_outcome(self):
        p = self.population("## dec-1\nRaised: 2026-10-12 10:00\n"
            "Status: confirmed 2026-10-15 10:00\nStatus: pending 2026-10-20 10:00\n")
        row = self.assert_u(p, "dec-1")
        self.assertIn("unanswered", row["states"])

    def test_heartbeat_does_not_invent_creation_time(self):
        p = self.run_population([dict(id="beat", event="beat", at="2026-10-20T12:00:00Z")])
        self.assertIsNone(p["records"][0]["created_at"])

    def test_heartbeat_does_not_replace_last_transition(self):
        p = self.run_population([
            dict(id="run", event="open", surface="dispatch", at="2026-10-20T10:00:00Z"),
            dict(id="run", event="beat", at="2026-10-20T11:00:00Z")])
        self.assertEqual(v.timestamp("2026-10-20T10:00:00Z").isoformat(), p["records"][0]["last_transition_at"])

    def test_invalid_observation_list_retains_identity_as_u(self):
        p = self.population(self.entry(1, outcome_known=True, observed_at=None))
        self.assert_u(p, "dec-1")

    def test_invalid_issue_list_retains_identity_as_u(self):
        p = self.population(self.entry(1, outcome_known=True, issues=None))
        self.assert_u(p, "dec-1")


if __name__ == "__main__":
    unittest.main()
