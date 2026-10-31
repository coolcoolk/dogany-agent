#!/usr/bin/env python3
# test-lane: fast
"""DGN-1458 plan lane: the applier (routines/plan-apply.py) and the
dispatcher wiring (dispatch-detached.sh --live-target / --apply-plan).

Model-free, hermetic: every live target is a temp dir. The planner's kernel
boundary is judged with fixtures/sandbox_exec_model.py (seatbelt
last-match-wins model); the real kernel is covered on macOS by
test-dispatch-observe.sh.
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.dirname(HERE)
APPLIER = os.path.join(ROUTINES, "plan-apply.py")
DISPATCH = os.path.join(ROUTINES, "dispatch-detached.sh")
SBMODEL = os.path.join(HERE, "fixtures", "sandbox_exec_model.py")

spec = importlib.util.spec_from_file_location("plan_apply", APPLIER)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.live = os.path.join(base, "live")
        self.outside = os.path.join(base, "outside")
        self.rundir = os.path.join(base, "run")
        os.makedirs(os.path.join(self.live, "database"))
        os.makedirs(self.outside)
        os.makedirs(os.path.join(self.rundir, "plan"))
        self.conf = os.path.join(self.live, "agent.conf")
        with open(self.conf, "w") as fh:
            fh.write("A=1\n")
        self.db = os.path.join(self.live, "database", "t.db")
        c = sqlite3.connect(self.db)
        c.executescript("create table t(x); insert into t values(1),(2),(3);")
        c.commit()
        c.close()
        self.status("completed")

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, result):
        with open(os.path.join(self.rundir, "status.json"), "w") as fh:
            json.dump({"result": result}, fh)

    def plan(self, ops, version=1):
        with open(os.path.join(self.rundir, "plan", "plan.json"), "w") as fh:
            json.dump({"version": version, "summary": "test", "ops": ops}, fh)

    def run_applier(self, *extra):
        return pa.main(["--rundir", self.rundir, "--target", self.live] + list(extra))

    def rows(self):
        c = sqlite3.connect(self.db)
        n = c.execute("select count(*) from t").fetchone()[0]
        c.close()
        return n

    def read(self, p):
        with open(p) as fh:
            return fh.read()


class ApplierTest(Fixture):
    def good_ops(self):
        return [
            {"op": "write", "path": self.conf, "content": "A=2\n", "expect_sha256": sha("A=1\n")},
            {"op": "write", "path": os.path.join(self.live, "new.md"), "content": "hi\n",
             "expect_sha256": "absent"},
            {"op": "sqlite", "db": self.db, "sql": "delete from t where x > 1;", "expect_changes": 2},
        ]

    def test_A_apply_all_ops(self):
        self.plan(self.good_ops())
        self.assertEqual(self.run_applier("--apply"), 0)
        self.assertEqual(self.read(self.conf), "A=2\n")
        self.assertEqual(self.read(os.path.join(self.live, "new.md")), "hi\n")
        self.assertEqual(self.rows(), 1)
        rec = json.load(open(os.path.join(self.rundir, "plan-receipt.json")))
        self.assertEqual(rec["state"], "applied")
        self.assertTrue(os.path.isfile(os.path.join(self.rundir, "plan-backup", "00-agent.conf")))

    def test_B_dry_run_writes_nothing(self):
        self.plan(self.good_ops())
        self.assertEqual(self.run_applier(), 0)
        self.assertEqual(self.read(self.conf), "A=1\n")
        self.assertEqual(self.rows(), 3)
        self.assertFalse(os.path.exists(os.path.join(self.rundir, "plan-receipt.json")))

    def test_C_path_outside_targets_refused(self):
        self.plan([{"op": "write", "path": os.path.join(self.outside, "x"), "content": "x",
                    "expect_sha256": "absent"}])
        self.assertEqual(self.run_applier("--apply"), 2)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "x")))

    def test_D_dotdot_and_symlink_refused(self):
        self.plan([{"op": "write", "path": self.live + "/../outside/x", "content": "x",
                    "expect_sha256": "absent"}])
        self.assertEqual(self.run_applier("--apply"), 2)
        link = os.path.join(self.live, "link")
        os.symlink(os.path.join(self.outside, "y"), link)
        self.plan([{"op": "write", "path": link, "content": "x", "expect_sha256": "absent"}])
        self.assertEqual(self.run_applier("--apply"), 2)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "y")))

    def test_E_stale_precondition_applies_nothing(self):
        ops = self.good_ops()
        self.plan(ops)
        with open(self.conf, "w") as fh:
            fh.write("A=9\n")  # the live file moved after planning
        self.assertEqual(self.run_applier("--apply"), 3)
        self.assertEqual(self.read(self.conf), "A=9\n")
        self.assertFalse(os.path.exists(os.path.join(self.live, "new.md")))
        self.assertEqual(self.rows(), 3)

    def test_F_rowcount_guard_rolls_everything_back(self):
        ops = self.good_ops()
        ops[2]["expect_changes"] = 1  # the planner expected 1 row, 2 would change
        self.plan(ops)
        self.assertEqual(self.run_applier("--apply"), 4)
        self.assertEqual(self.rows(), 3)
        self.assertEqual(self.read(self.conf), "A=1\n")
        self.assertFalse(os.path.exists(os.path.join(self.live, "new.md")))
        rec = json.load(open(os.path.join(self.rundir, "plan-receipt.json")))
        self.assertEqual(rec["state"], "failed-rolled-back")

    def test_G_applies_once(self):
        self.plan([{"op": "write", "path": os.path.join(self.live, "n"), "content": "1",
                    "expect_sha256": "absent"}])
        self.assertEqual(self.run_applier("--apply"), 0)
        os.unlink(os.path.join(self.live, "n"))
        self.assertEqual(self.run_applier("--apply"), 3)
        self.assertFalse(os.path.exists(os.path.join(self.live, "n")))

    def test_H_unfinished_run_not_applied(self):
        self.plan(self.good_ops())
        self.status("timed-out")
        self.assertEqual(self.run_applier("--apply"), 3)
        self.assertEqual(self.read(self.conf), "A=1\n")

    def test_I_check_verdicts(self):
        import io
        import contextlib

        def check():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.run_applier("--check")
            return rc, buf.getvalue().strip()

        self.assertEqual(check(), (0, "absent"))
        self.plan(self.good_ops())
        self.assertEqual(check(), (0, "ready: 3 op(s)"))
        self.plan(self.good_ops(), version=2)
        rc, out = check()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("invalid: "), out)

    def test_J_schema_refusals(self):
        bad = [
            [],
            [{"op": "chmod", "path": self.conf}],
            [{"op": "delete", "path": self.conf, "expect_sha256": "absent"}],
            [{"op": "sqlite", "db": self.db, "sql": "delete from t", "expect_changes": True}],
            [{"op": "write", "path": self.conf, "content": "x", "expect_sha256": sha("A=1\n")},
             {"op": "delete", "path": self.conf, "expect_sha256": sha("A=1\n")}],
        ]
        for ops in bad:
            self.plan(ops)
            self.assertEqual(self.run_applier("--apply"), 2, ops)
        self.assertEqual(self.read(self.conf), "A=1\n")

    def test_K_delete_with_matching_sha(self):
        self.plan([{"op": "delete", "path": self.conf, "expect_sha256": sha("A=1\n")}])
        self.assertEqual(self.run_applier("--apply"), 0)
        self.assertFalse(os.path.exists(self.conf))


def bash(script, env=None):
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run(["bash", "-c", "DISPATCH_TEST=1 source \"$DD\"; set +e\n" + script],
                          env=dict(full, DD=DISPATCH), capture_output=True, text=True)


class DispatcherWiringTest(Fixture):
    def test_L_live_target_requires_read_only(self):
        r = bash('READ_ONLY=0; WT_SLUG=t; LIVE_TARGETS_ARG=("%s"); validate_live_targets; echo rc=$?' % self.live)
        self.assertIn("rc=1", r.stdout)
        self.assertIn("--live-target requires --read-only", r.stderr)

    def test_M_live_target_must_exist_and_be_absolute(self):
        r = bash('READ_ONLY=1; LIVE_TARGETS_ARG=("rel/path"); validate_live_targets; echo rc=$?')
        self.assertIn("rc=1", r.stdout)
        r = bash('READ_ONLY=1; LIVE_TARGETS_ARG=("%s/nope"); validate_live_targets; echo rc=$?' % self.live)
        self.assertIn("rc=1", r.stdout)
        r = bash('READ_ONLY=1; LIVE_TARGETS_ARG=("%s" "%s"); validate_live_targets; echo rc=$?; printf "%%s" "$LIVE_TARGETS"'
                 % (self.live, self.conf))
        self.assertIn("rc=0", r.stdout)
        self.assertIn(self.live + "\n" + self.conf, r.stdout)

    def test_N_planner_profile_opens_only_the_plan_dir(self):
        prof = os.path.join(self.rundir, "observe.sb")
        wd = os.path.join(self.outside, "wd")
        os.makedirs(wd)
        r = bash('WORKSPACE="%s"; WORKTREE_BASE="%s/wtb"; RUNTIME=claude-code; PLAN_DIR="%s/plan"; '
                 'LIVE_TARGETS="%s"; render_observe_profile "%s" "%s" 11111111-2222-3333-4444-555555555555; echo rc=$?'
                 % (os.path.dirname(self.rundir), self.outside, self.rundir, self.live, prof, wd))
        self.assertIn("rc=0", r.stdout, r.stderr)

        def v(path):
            return subprocess.run(["python3", SBMODEL, "verdict", prof, "file-write*", path],
                                  capture_output=True, text=True).stdout.strip()

        self.assertEqual(v(os.path.join(self.rundir, "plan", "plan.json")), "allow")
        self.assertEqual(v(os.path.join(self.rundir, "status.json")), "deny")
        self.assertEqual(v(os.path.join(self.rundir, "observe.sb")), "deny")
        self.assertEqual(v(self.conf), "deny")
        self.assertEqual(v(self.db), "deny")
        self.assertEqual(v(os.path.join(wd, "x")), "deny")
        with open(prof) as fh:
            self.assertIn("PLAN LANE", fh.read().splitlines()[-2])

    def test_O_observe_profile_unchanged_without_plan_lane(self):
        prof_a = os.path.join(self.rundir, "a.sb")
        prof_b = os.path.join(self.rundir, "b.sb")
        wd = os.path.join(self.outside, "wd")
        os.makedirs(wd)
        common = ('WORKSPACE="%s"; WORKTREE_BASE="%s/wtb"; RUNTIME=claude-code; '
                  % (os.path.dirname(self.rundir), self.outside))
        bash(common + 'render_observe_profile "%s" "%s" 11111111-2222-3333-4444-555555555555' % (prof_a, wd))
        bash(common + 'PLAN_DIR=""; LIVE_TARGETS=""; render_observe_profile "%s" "%s" 11111111-2222-3333-4444-555555555555'
             % (prof_b, wd))
        a, b = self.read(prof_a), self.read(prof_b)
        self.assertEqual(a, b)
        self.assertNotIn("PLAN LANE", a)

    def test_P_backstop_carries_the_plan_contract(self):
        r = bash('WT_PATH=""; PLAN_DIR="%s/plan"; LIVE_TARGETS="%s"; worker_backstop' % (self.rundir, self.live))
        self.assertIn("PLAN LANE", r.stdout)
        self.assertIn(os.path.join(self.rundir, "plan", "plan.json"), r.stdout)
        self.assertIn(self.live, r.stdout)
        self.assertIn('"expect_changes"', r.stdout)
        r = bash('WT_PATH=""; PLAN_DIR=""; LIVE_TARGETS=""; worker_backstop')
        self.assertNotIn("PLAN LANE", r.stdout)

    def test_Q_access_refusal_points_at_the_lane(self):
        r = bash('RUNTIME=claude-code; READ_ONLY=0; WRITE_REQUESTED=0; WT_SLUG=""; validate_access_mode; echo rc=$?')
        self.assertIn("rc=1", r.stdout)
        self.assertIn("--read-only --live-target <path>", r.stderr)
        self.assertIn("--apply-plan <runid>", r.stderr)
        self.assertNotIn("no detached execution lane", r.stderr)

    def test_R_apply_plan_mode_end_to_end(self):
        ws = os.path.dirname(self.rundir)
        ddir = os.path.join(ws, ".telegram_bot", "dispatch")
        os.makedirs(ddir)
        rd = os.path.join(ddir, "dsp-20260927-000000-0001")
        os.rename(self.rundir, rd)
        self.rundir = rd
        with open(os.path.join(rd, "run.env"), "w") as fh:
            fh.write("RUNID=dsp-20260927-000000-0001\nLIVE_TARGETS=%s\n" % self.live)
        self.plan([{"op": "write", "path": self.conf, "content": "A=3\n", "expect_sha256": sha("A=1\n")}])
        env = {"DISPATCH_DIR_OVERRIDE": ddir}
        r = bash('DISPATCH_DIR="%s"; CANCEL_RUNID=dsp-20260927-000000-0001; BACKFILL_APPLY=0; do_apply_plan; echo rc=$?' % ddir, env)
        self.assertIn("DRY RUN", r.stdout, r.stderr)
        self.assertEqual(self.read(self.conf), "A=1\n")
        r = bash('DISPATCH_DIR="%s"; CANCEL_RUNID=dsp-20260927-000000-0001; BACKFILL_APPLY=1; do_apply_plan; echo rc=$?' % ddir, env)
        self.assertIn("rc=0", r.stdout, r.stderr)
        self.assertEqual(self.read(self.conf), "A=3\n")
        with open(os.path.join(rd, "run.env"), "w") as fh:
            fh.write("RUNID=dsp-20260927-000000-0001\n")
        r = bash('DISPATCH_DIR="%s"; CANCEL_RUNID=dsp-20260927-000000-0001; BACKFILL_APPLY=1; do_apply_plan; echo rc=$?' % ddir, env)
        self.assertIn("rc=2", r.stdout)
        self.assertIn("not a plan-lane run", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
