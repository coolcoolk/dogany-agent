#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
# test-component: review-evidence
# component: review-evidence
# test-new-component: M1-4 G5 merge-path evidence gate; no existing suite owns the review receipt rows
# test-tier: 0
# test-prereq: tmp-files git
# test-covers: agents/.template/routines/lib/review_evidence.py agents/.template/routines/lib/stage_judge.py agents/.template/routines/lib/dispatch_artifacts.py agents/.template/routines/lib/ticket_receipts.py
# promise: a worker test alone never satisfies the review evidence gate
# promise: a done verdict plus before and a passing after of one scenario satisfies the gate
# promise: before and after of different scenarios do not satisfy the gate
# promise: a failing after or an identical before and after ref does not satisfy the gate
# promise: every observed and pinned scenario must pass, including retained completed scenarios
# promise: later negative reviewer evidence invalidates earlier acceptance for the same candidate
# promise: candidate-bound evidence cannot be borrowed from another candidate or legacy unbound rows
# promise: malformed review rows fail closed instead of exposing an older passing review
# promise: review rows ride the receipt chain without breaking ticket lifecycle replay
# promise: S4 replay: merge is refused without regression evidence and lands in dev with it
# promise: a report-only child of an unmerged code branch is refused and leaves the target untouched
# promise: a canonical or dev/release target is refused before any hook or merge runs
# promise: an interrupted landing restores the target index and worktree and removes the lock
# promise: the merge path offers no live-apply route
"""Review evidence on the ticket receipt (M1-4 G5)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True
for name in list(os.environ):
    if name.startswith(("GIT_CONFIG", "DOGANY_", "DGN_")) or name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        os.environ.pop(name)
LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import review_evidence as R  # noqa: E402
import ticket_receipts as T  # noqa: E402
import dispatch_artifacts as D  # noqa: E402

AT = "2026-10-22T14:00:00Z"
TICKET = "DGN-9001"


def tests_row(path):
    T.append(path, {"schema": T.SCHEMA, "writer": "devkit-ticket", "ticket": TICKET, "op": "progress",
                    "values": {"event": "tests", "ref": "3/3 passed", "line": "x"}, "at": AT})


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "ticket-writes.jsonl")

    def reg(self, scenario, phase, result, ref):
        R.record_regression(self.ledger, TICKET, scenario, phase, result, ref, AT)

    def test_worker_test_alone(self):
        tests_row(self.ledger)
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        self.assertIn("worker test", R.missing(self.ledger, TICKET))
        self.assertIn("no done", R.missing(os.path.join(self.tmp.name, "none.jsonl"), TICKET))

    def test_done_before_after(self):
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        self.reg("dispatch-flow", "before", "pass", "base1")
        self.reg("dispatch-flow", "after", "pass", "head1")
        self.assertIsNone(R.missing(self.ledger, TICKET))

    def test_scenarios_must_match(self):
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        self.reg("flow-a", "before", "pass", "base1")
        self.reg("flow-b", "after", "pass", "head1")
        self.assertIsNotNone(R.missing(self.ledger, TICKET))

    def test_failing_or_same_ref(self):
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        self.reg("f", "before", "pass", "r1")
        self.reg("f", "after", "fail", "r2")
        self.assertIsNotNone(R.missing(self.ledger, TICKET))
        self.reg("f", "after", "pass", "r1")
        self.assertIsNotNone(R.missing(self.ledger, TICKET))

    def test_mixed_scenarios_and_missing_pin(self):
        R.record_done(self.ledger, TICKET, "review", AT)
        for scenario in ("passing", "failing"):
            self.reg(scenario, "before", "pass", "base")
            self.reg(scenario, "after", "pass" if scenario == "passing" else "fail", "head")
        self.assertIn("failing", R.missing(self.ledger, TICKET))
        self.reg("failing", "after", "pass", "head")
        self.assertIsNone(R.missing(self.ledger, TICKET))
        self.assertIn("absent", R.missing(self.ledger, TICKET, required_scenarios=["absent"]))

    def test_retained_baseline_must_pass(self):
        R.record_done(self.ledger, TICKET, "review", AT)
        self.reg("flow", "before", "fail", "base")
        self.reg("flow", "after", "pass", "head")
        self.assertIsNone(R.missing(self.ledger, TICKET))
        self.assertIn("flow", R.missing(self.ledger, TICKET, prior_completed_scenarios=["flow"]))

    def test_negative_review_invalidates_legacy_pass(self):
        T.append(self.ledger, R._row(TICKET, "done", {"ref": "review"}, AT))
        self.reg("flow", "before", "pass", "base")
        self.reg("flow", "after", "pass", "head")
        self.assertIsNone(R.missing(self.ledger, TICKET))
        R.record_done(self.ledger, TICKET, "review", AT, verdict="fail")
        self.assertIn("VERIFY_REVIEW_PASS", R.missing(self.ledger, TICKET))
        self.assertEqual(T._rows(self.ledger, TICKET)[-1]["values"]["verdict"], "fail")

    def test_candidate_binding_and_later_failure(self):
        for candidate in ("candidate-a", "candidate-b"):
            R.record_done(self.ledger, TICKET, "review", AT, candidate=candidate)
            for phase, ref in (("before", "base"), ("after", "head")):
                R.record_regression(self.ledger, TICKET, "flow", phase, "pass", ref, AT,
                                    candidate=candidate)
        R.record_done(self.ledger, TICKET, "re-review", AT, verdict="fail", candidate="candidate-a")
        self.assertIn("VERIFY_REVIEW_PASS", R.missing(self.ledger, TICKET, candidate="candidate-a"))
        self.assertIsNone(R.missing(self.ledger, TICKET, candidate="candidate-b"))
        self.assertIsNotNone(R.missing(self.ledger, TICKET, candidate="candidate-c"))
        self.assertIn("VERIFY_CANDIDATE_REQUIRED", R.missing(self.ledger, TICKET))

    def test_unbound_rows_cannot_verify_a_candidate(self):
        R.record_done(self.ledger, TICKET, "review", AT)
        self.reg("flow", "before", "pass", "base")
        self.reg("flow", "after", "pass", "head")
        self.assertIn("no done", R.missing(self.ledger, TICKET, candidate="candidate"))

    def test_malformed_review_cannot_reveal_an_earlier_pass(self):
        R.record_done(self.ledger, TICKET, "review", AT)
        self.reg("flow", "before", "pass", "base")
        self.reg("flow", "after", "pass", "head")
        T.append(self.ledger, R._row(TICKET, "done", {"ref": "review", "verdict": []}, AT))
        self.assertIn("VERIFY_EVIDENCE_INVALID", R.missing(self.ledger, TICKET))

    def test_chain_and_lifecycle(self):
        src = os.path.join(self.tmp.name, "t.md")
        before = "---\nstatus: open\n---\nbody\n"
        Path(src).write_text(before)
        anchor = T.anchor(self.ledger, TICKET)
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        after = "---\nstatus: \"wip\"\n---\nbody\n"
        T.append(self.ledger, T.receipt(TICKET, src, before, after, "status",
                                        {"status": "wip"}, AT))
        self.assertTrue(T.proves_lifecycle(self.ledger, TICKET, src, T.sha(before),
                                           T.sha(after), anchor, before, after))
        self.assertEqual(T.receipt_issues(self.ledger), [])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class ReplayBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.root = root
        self.target, self.worker = root / "main", root / "wt"
        self.target.mkdir()
        git(self.target, "init", "-q", "-b", "inst-work")
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            git(self.target, "config", k, v)
        (self.target / "README").write_text("x")
        git(self.target, "add", "."); git(self.target, "commit", "-qm", "base")
        self.base = git(self.target, "rev-parse", "HEAD")
        git(self.target, "worktree", "add", "-q", "-b", "wk", str(self.worker))
        (self.worker / "files/reports").mkdir(parents=True)
        (self.worker / "files/reports/r.md").write_text("report")
        git(self.worker, "add", "."); git(self.worker, "commit", "-qm", "artifact")
        # run dir three levels under the "instance root" that holds the receipts
        self.inst = root / "inst"
        self.run_dir = self.inst / "a" / "b" / "run1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "run.env").write_text("TICKET=" + TICKET + "\n")
        self.ledger = str(T.ledger_path(self.inst))
        stub = types.ModuleType("why_net")
        stub.WhyRefusal = type("WhyRefusal", (Exception,), {})
        stub.r3 = types.SimpleNamespace(is_registered=lambda t, r: True,
                                        registered_boundary=lambda *a, **k: {"decision": "allow"})
        hd = types.ModuleType("host_dispatcher")
        hd._run_env = lambda p: dict(l.split("=", 1) for l in Path(p).read_text().split())
        saved = {k: sys.modules.get(k) for k in ("why_net", "host_dispatcher")}
        sys.modules.update(why_net=stub, host_dispatcher=hd)
        self.addCleanup(lambda: [sys.modules.__setitem__(k, v) if v else sys.modules.pop(k, None)
                                 for k, v in saved.items()])

    def land(self):
        return D.land(self.target, self.worker, self.base, "wk", str(self.run_dir))


class S4Replay(ReplayBase):
    def test_refused_then_merged(self):
        tests_row(self.ledger)  # the worker test is green and recorded
        out = self.land()
        self.assertEqual(out["state"], "branch-and-review")
        self.assertIn("review evidence", out["note"])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.base)
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        R.record_regression(self.ledger, TICKET, "dispatch-flow", "before", "pass", "base1", AT)
        R.record_regression(self.ledger, TICKET, "dispatch-flow", "after", "pass", "head1", AT)
        out = self.land()
        self.assertEqual(out["state"], "landed")
        self.assertEqual(out["branch"], "inst-work")
        self.assertTrue((self.target / "files/reports/r.md").is_file())

    def test_no_live_apply_route(self):
        src = (LIB / "review_evidence.py").read_text() + (LIB / "dispatch_artifacts.py").read_text()
        for word in ("update.sh", "systemctl", "launchctl", "restart", "apply_to_live"):
            self.assertNotIn(word, src.replace("T17 stays OFF", ""))


class Guards(ReplayBase):
    """DGN-2306: the merge may only introduce artifact paths, never into canonical/protected targets."""

    def setUp(self):
        super().setUp()
        tests_row(self.ledger)
        R.record_done(self.ledger, TICKET, "abc1234", AT)
        R.record_regression(self.ledger, TICKET, "f", "before", "pass", "b1", AT)
        R.record_regression(self.ledger, TICKET, "f", "after", "pass", "h1", AT)

    def lock(self):
        return self.target / ".git" / "dispatch-artifact-landing.lock"

    def test_report_only_child_of_unmerged_code(self):
        # the target has not merged the code commit the worker's base carries
        git(self.target, "checkout", "-q", "-b", "code")
        (self.target / "prod.py").write_text("production")
        git(self.target, "add", "."); git(self.target, "commit", "-qm", "code")
        code = git(self.target, "rev-parse", "HEAD")
        git(self.target, "checkout", "-q", "inst-work")
        git(self.worker, "reset", "-q", "--hard", code)
        (self.worker / "files/reports").mkdir(parents=True, exist_ok=True)
        (self.worker / "files/reports/r.md").write_text("report")
        git(self.worker, "add", "."); git(self.worker, "commit", "-qm", "report")
        head = git(self.target, "rev-parse", "HEAD")
        out = D.land(self.target, self.worker, code, "wk", str(self.run_dir))
        self.assertEqual(out["state"], "branch-and-review")
        self.assertIn("non-artifact", out["note"])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.target, "status", "--porcelain"), "")
        self.assertFalse((self.target / "prod.py").exists())
        self.assertFalse(self.lock().exists())

    def hook_marker(self, name):
        marker = self.root / (name + ".ran")
        hook = self.target / ".git/hooks" / name
        hook.write_text("#!/bin/sh\ntouch %s\n" % marker)
        hook.chmod(0o755)
        return marker

    def test_protected_targets_refused_before_hooks(self):
        for branch in ("dev", "release/1.2"):
            git(self.target, "checkout", "-q", "-B", branch)
            out = self.land()
            self.assertEqual(out["state"], "branch-and-review", branch)
            self.assertIn("protected", out["note"])
            self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.base)
        git(self.target, "checkout", "-q", "-B", "inst-work")
        (self.target / ".dogany-canonical").write_text("")
        git(self.target, "add", "."); git(self.target, "commit", "-qm", "marker")
        marker = self.hook_marker("pre-commit")
        out = self.land()
        self.assertIn("canonical", out["note"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.target / "files/reports/r.md").exists())
        self.assertFalse(self.lock().exists())

    def test_interrupted_landing_restores_target(self):
        (self.target / "local.txt").write_text("owner scratch")  # unrelated untracked work survives
        head = git(self.target, "rev-parse", "HEAD")
        hook = self.target / ".git/hooks/reference-transaction"
        hook.write_text("#!/bin/sh\n[ \"$1\" = prepared ] && kill -TERM %d\nsleep 3 >/dev/null 2>&1\n" % os.getpid())
        hook.chmod(0o755)
        out = self.land()
        self.assertEqual(out["state"], "branch-and-review")
        self.assertIn("interrupted", out["note"])
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.target, "status", "--porcelain"), "?? local.txt")
        self.assertFalse((self.target / "files/reports/r.md").exists())
        self.assertFalse(self.lock().exists())
        hook.unlink()
        out = self.land(); self.assertEqual(out["state"], "landed", out)  # nothing stale blocks the next landing


if __name__ == "__main__":
    unittest.main()
