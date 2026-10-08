#!/usr/bin/env python3
# test-lane: fast
# test-covers: *    # tree scanner, measured 0.34s (DGN-1734): runs on every push
"""DGN-1691: fixture-based session background work selftest."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sf", HERE.parent / "status-footer.py")
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)
START = datetime.fromisoformat("2026-09-24T05:00:00+00:00").timestamp()
# The board also reads the owner-notice state (DGN-1769); never the host's.
os.environ["DOGANY_BG_NOTICE_STATE"] = os.path.join(
    tempfile.mkdtemp(prefix="footer-bg-"), "absent-state.json")


class BackgroundTests(unittest.TestCase):
    def check_fixture(self, path):
        fixture = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(e) for e in fixture["entries"]))
            with patch.object(sf.time, "time", return_value=START + fixture["age"]):
                rows = sf._collect_active_subagents(str(transcript))
                self.assertIsNotNone(rows)
                self.assertEqual(bool(rows), fixture["visible"])
                if fixture["visible"]:
                    # Row format (DGN-1691 phase 2, DGN-1820): a background job
                    # has no model and its slot is internal:
                    # "<name> \u00b7 <HH:MM> (<N>\ubd84~)".
                    stamp = sf.time.strftime("%H:%M", sf.time.localtime(START))
                    self.assertEqual(len(rows), 1)
                    self.assertRegex(rows[0], "^"
                                     "Run full tests \u00b7 %s \\(%d\ubd84~\\)$"
                                     % (stamp, fixture["age"] // 60))
                    board = sf._build_dashboard([], rows, [])
                    self.assertIn(rows[0], board)
                    self.assertIn("[%s]" % sf.LIVE_LABEL, board)

    def test_subagent_and_shell_share_live_section(self):
        fixture = json.loads((HERE / "fixtures/footer-background/open.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent-abc123.jsonl").write_text("{}")
            (root / "agent-abc123.meta.json").write_text(
                json.dumps({"description": "Existing subagent"}))
            entries = fixture["entries"] + [{"type": "user", "message": {
                "content": "Async agent launched successfully. agentId: abc123"}}]
            transcript = root / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(e) for e in entries))
            with patch.object(sf, "_subagents_dir", return_value=tmp), patch.object(
                    sf.time, "time", return_value=START + 60):
                rows = sf._collect_active_subagents(str(transcript))
            self.assertEqual(rows[0], "Existing subagent")
            self.assertEqual(len(rows), 2)
            self.assertIn("Run full tests", rows[1])

    def test_missing_transcript_preserves_failure(self):
        self.assertIsNone(sf._collect_active_subagents("/missing/session.jsonl"))


class NoticedLongJobTests(unittest.TestCase):
    """DGN-1769 repro: a leader's dev.60 release job (bpm3gwazb), launched
    06:32:52 KST, START sent, still running at the 07:08 measurement, was
    missing from the workbench.  Transcript entries are the live launch pair
    (paths shortened); the state entry mirrors the live bg-notice-state."""
    LAUNCH = datetime.fromisoformat("2026-09-27T21:32:52.735+00:00").timestamp()
    ENTRIES = [
        {"type": "assistant", "timestamp": "2026-09-27T21:32:52.735Z",
         "message": {"content": [{"type": "tool_use",
                                  "id": "toolu_019RouEFDFuLyNsEGuxS7W9y",
                                  "name": "Bash", "input": {
            "command": "cd /x/dogany-agent && bash scripts/release.sh --dev "
                       "> /x/release-dev60.log 2>&1; tail -25 /x/release-dev60.log",
            "description": "\ub2e4\uc74c \uac1c\ubc1c\ud310 \ubc1c\ud589 "
                           "(\uc804\uccb4 \uac80\uc0ac \ud3ec\ud568)",
            "run_in_background": True}}]}},
        {"type": "user", "timestamp": "2026-09-27T21:32:52.985Z",
         "message": {"content": [{"tool_use_id": "toolu_019RouEFDFuLyNsEGuxS7W9y",
                                  "type": "tool_result", "is_error": False,
                                  "content": "Command running in background with "
                                  "ID: bpm3gwazb. Output is being written to: "
                                  "/tmp/tasks/bpm3gwazb.output."}]}},
    ]

    def rows(self, age, state):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(e) for e in self.ENTRIES))
            state_file = Path(tmp) / "bg-notice-state.json"
            if state is not None:
                state_file.write_text(json.dumps({"bpm3gwazb": state}))
            with patch.dict(os.environ, {"DOGANY_BG_NOTICE_STATE": str(state_file)}), \
                    patch.object(sf.time, "time", return_value=self.LAUNCH + age):
                return sf._collect_active_subagents(str(transcript))

    def live_state(self, **kw):
        rec = {"end_sent": False, "name": "release.sh: \ub2e4\uc74c \uac1c\ubc1c\ud310",
               "queued": self.LAUNCH, "recorded": self.LAUNCH, "slot": "bash",
               "start": self.LAUNCH, "start_sent": True, "tool": "Bash"}
        rec.update(kw)
        return rec

    def test_noticed_job_stays_past_ceiling(self):
        rows = self.rows(36 * 60, self.live_state())
        self.assertEqual(len(rows), 1, rows)
        stamp = sf.time.strftime("%H:%M", sf.time.localtime(self.LAUNCH))
        self.assertEqual(rows[0], "\ub2e4\uc74c \uac1c\ubc1c\ud310 \ubc1c\ud589 "
                         "(\uc804\uccb4 \uac80\uc0ac \ud3ec\ud568) \u00b7 %s (36\ubd84~)" % stamp)

    def test_without_notice_record_ceiling_still_applies(self):
        self.assertEqual(self.rows(36 * 60, None), [])
        self.assertEqual(self.rows(36 * 60, self.live_state(start_sent=False)), [])

    def test_end_sent_drops_the_row(self):
        self.assertEqual(self.rows(36 * 60, self.live_state(end_sent=True)), [])

    def test_noticed_job_has_an_upper_bound(self):
        self.assertEqual(self.rows(sf.BG_NOTICED_MAX_SECS + 60, self.live_state()), [])

    def test_inside_ceiling_needs_no_record(self):
        self.assertEqual(len(self.rows(5 * 60, None)), 1)


for fixture_path in sorted((HERE / "fixtures/footer-background").glob("*.json")):
    setattr(BackgroundTests, "test_" + fixture_path.stem.replace("-", "_"),
            lambda self, path=fixture_path: self.check_fixture(path))

if __name__ == "__main__":
    unittest.main()
