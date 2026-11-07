#!/usr/bin/env python3
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
# test-component: local-suite-dispatch
# test-tier: 0
# test-prereq: tmp-files, subprocess
# test-new-component: Sourced local full-suite brief refusal is independent of the dispatcher spawn/probe suites.
"""DGN-2225: a local dispatch slot never carries a full suite.

On 2026-10-22 about ten runs were pinned local (--local-why "live kit DB
copy") while their briefs bundled a full suite; the cloud lane (cap 8) sat
idle while the Mac's local cap fell to 0. dispatch-detached.sh now refuses a
local run (an explicit --local-why, or any runtime that resolves local) whose
brief asks for a full suite / the run-tests lane, with a split instruction.

Locks:
  local codex + full-suite brief    -> refused (rc 1), split instruction,
                                       decision recorded local_suite_refused;
  --local-why + full-suite brief    -> refused, the reason is echoed;
  local + replay-only brief         -> allowed;
  local + a line that negates it    -> allowed ("do not run the full suite");
  cloud / codex-cloud + full suite  -> allowed;
  claude-code / claude-api + suite  -> allowed (owner 2026-11-02: run through
                                       the gated test runner on this Mac);
  do_spawn calls the gate after runtime selection and again after run_gates
  (a slot walk may move a cloud run local), releasing the slot there.
Offline: the script is sourced (DISPATCH_TEST=1); no worker, no network.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DD = ROOT / "routines" / "dispatch-detached.sh"

FULL_SUITE_BRIEFS = (
    "Replay the kit DB copy, then run the full suite.\n",
    "After the fix: bash git-hooks/run-tests.sh and report.\n",
    "E2E gate: run DGN_TEST_LANE=all until green.\n",
    "Run all suites before returning.\n",
    "Finish: run the whole test suite (run-tests lane).\n",
    "Do the replay, then run the full suite, no exceptions.\n",
    "- run git-hooks/run-tests.sh --gate and paste rc.\n",
)
REPLAY_BRIEFS = (
    "Replay the 2026-10-22 events against the live kit DB copy and write the "
    "diff to files/reports/replay.md.\n",
    "Replay only. Do not run the full suite: it runs in cloud.\n",
    "Never run run-tests.sh here; just replay the live data.\n",
    # DGN-2225 r2 P1-3: context mentions are not asks
    "Context: the full suite is red on dev since DGN-2190; not your problem.\n",
    "The run-tests.sh pre-push gate already passed on the cloud worker.\n",
    "Background: yesterday's full test run took 40 min and pinned load5 at 27.\n",
    "Read git-hooks/run-tests.sh to learn the lane names, then edit only the "
    "replay script.\n",
    "Owner rule: suites go to the cloud; this local run only replays live data.\n",
    "Real-code grill: DGN-2225 live-session gate + local full-suite dispatch "
    "refusal\n",
    "Report the DGN_TEST_LANE=all result from the cloud run in your summary.\n",
    "Leave the full suite to the cloud run (dsp-...-cloud).\n",
    "The cloud run already covers all suites; you handle the replay.\n",
    "Do NOT run suites here; the full suite is red on dev.\n",
)


def run(tmp, body, brief):
    Path(tmp, "task.md").write_text(brief)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
    env.update(DISPATCH_TEST="1", TEST_TMP=tmp)
    script = ('set --\nsource "%s"\nDECISION_DIR="$TEST_TMP/decisions"\n'
              'TASK_FILE="$TEST_TMP/task.md"; LABEL=t\n' % DD) + body
    return subprocess.run(["bash", "-c", script], env=env, text=True,
                          capture_output=True, cwd=tmp, timeout=60)


class LocalSuiteGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dgn2225-dd-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gate(self, brief, runtime="claude-code", local_why=""):
        r = run(self.tmp, 'RUNTIME=%s; LOCAL_WHY="%s"\n'
                'rc=0; local_suite_gate || rc=$?; echo "RC=$rc"\n'
                % (runtime, local_why), brief)
        m = re.search(r"RC=(\d+)", r.stdout)
        self.assertIsNotNone(m, r.stderr)
        return int(m.group(1)), r.stderr

    def decisions(self):
        d = Path(self.tmp, "decisions")
        return [json.loads(p.read_text()) for p in d.glob("*.json")] \
            if d.is_dir() else []

    def test_local_full_suite_refused(self):
        for brief in FULL_SUITE_BRIEFS:
            # Owner 2026-11-02: only local codex is refused now; claude-code /
            # claude-api run suites via the gated runner (test below).
            for rt in ("codex",):
                rc, err = self.gate(brief, rt)
                self.assertEqual(1, rc, (rt, brief, err))
                self.assertIn("REFUSED (DGN-2225)", err)
                self.assertIn("cloud run on the repo", err)
                self.assertIn("--repo-contained", err)
                self.assertIn("live-data replay", err)
        self.assertTrue(any(d.get("reason_code") == "local_suite_refused"
                            for d in self.decisions()), len(self.decisions()))

    def test_claude_subscription_and_api_accept_suite_briefs(self):
        # Owner 2026-11-02: subscription / API credit execute suites on this
        # Mac through git-hooks/run-tests.sh --gate; no refusal, no record.
        for brief in FULL_SUITE_BRIEFS:
            for rt in ("claude-code", "claude-api"):
                rc, err = self.gate(brief, rt, "live kit DB copy on this Mac")
                self.assertEqual(0, rc, (rt, brief, err))
                self.assertNotIn("REFUSED", err)
        self.assertEqual([], self.decisions())

    def test_local_why_full_suite_refused(self):
        rc, err = self.gate(FULL_SUITE_BRIEFS[0], "codex",
                            "live kit DB copy on this Mac")
        self.assertEqual(1, rc)
        self.assertIn("--local-why: live kit DB copy on this Mac", err)

    def test_local_replay_only_allowed(self):
        for brief in REPLAY_BRIEFS:
            rc, err = self.gate(brief, "claude-code",
                                "live kit DB copy on this Mac")
            self.assertEqual(0, rc, (brief, err))
            self.assertEqual("", err.strip())

    def test_cloud_full_suite_allowed(self):
        for brief in FULL_SUITE_BRIEFS:
            for rt in ("cloud", "cloud-credit", "codex-cloud"):
                rc, err = self.gate(brief, rt)
                self.assertEqual(0, rc, (rt, brief, err))

    def test_attached_ticket_is_evidence_on_every_runtime(self):
        # The reported w1-dgn2258 replay, plus a local run to prove that
        # exempting cloud-credit alone does not mask the attachment bug.
        Path(self.tmp, "ticket.md").write_text("# run-tests regression\nRun the full lane.\n")
        for runtime in ("cloud-credit", "cloud", "codex-cloud", "codex", "claude-code"):
            with self.subTest(runtime=runtime):
                r = run(self.tmp, '''
RUNTIME=%s
ATTACH_FILES=("$TEST_TMP/ticket.md")
cloud_task_overhead() { echo 0; }
attach_inline || exit 90
grep -q 'Run the full lane' "$TASK_FILE" || exit 91
local_suite_gate
''' % runtime, "Fix the refusal diagnostic; only replay the specific fixture.\n")
                self.assertEqual(0, r.returncode, r.stdout + r.stderr)
                self.assertNotIn("REFUSED", r.stderr)

    def test_attaching_evidence_does_not_hide_a_local_instruction(self):
        Path(self.tmp, "evidence.md").write_text("A harmless ticket.\n")
        r = run(self.tmp, '''
RUNTIME=codex; ATTACH_FILES=("$TEST_TMP/evidence.md")
cloud_task_overhead() { echo 0; }
attach_inline || exit 90
local_suite_gate
''', "Run the full lane locally.\n")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        for expected in ("REFUSED", "runtime=codex", "--repo-contained", "cloud run on the repo"):
            self.assertIn(expected, r.stderr)

    def test_snapshot_replay_preserves_instruction_scope_and_prompt_bytes(self):
        Path(self.tmp, "ticket.md").write_text("# run-tests\nRun the full lane locally.\n")
        r = run(self.tmp, '''
RUNTIME=codex; ATTACH_FILES=("$TEST_TMP/ticket.md")
cloud_task_overhead() { echo 0; }
attach_inline || exit 90
original="$TASK_FILE"
snapshot_dispatch_inputs "$TEST_TMP/snapshot.md" || exit 91
rm "$TEST_TMP/task.md" "$TEST_TMP/ticket.md"
# Re-enter the normal inline and gate path as a drained/deferred request.
TASK_FILE="$SNAP_TASK_FILE"; ATTACH_BRIEF_FILE=""
ATTACH_FILES=("${SNAP_ATTACH_FILES[@]}")
attach_inline || exit 92
cmp "$original" "$TASK_FILE" || exit 93
rm "$original"
local_suite_gate
''', "Fix the named refusal and replay only its fixture.\n")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertNotIn("REFUSED", r.stderr)

    def test_do_spawn_wires_the_gate(self):
        text = DD.read_text()
        body = text[text.index("\ndo_spawn() {"):]
        body = body[:body.index("\n}\n")]
        sel = body.index("select_default_runtime ||")
        first = body.index("local_suite_gate || exit 2")
        self.assertLess(sel, first)
        self.assertLess(first, body.index("bottleneck_gate"))
        gates = body.index("run_gates || rc=$?")
        post = body.index("if ! local_suite_gate; then", gates)
        self.assertIn("active_count_decr",
                      body[post:body.index("fi", post)])
        self.assertLess(post, body.index("resolve_effort"))


if __name__ == "__main__":
    unittest.main()
