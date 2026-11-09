#!/usr/bin/env python3
# test-lane: fast
"""DGN-1995: fresh named windows supersede pre-reset dispatch prose.

Offline: a temporary snapshot and a copied claude-usage.sh with a rejecting
curl stub. The 13:47 fixture must yield weekly 98% left (5h 93% left).
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROUTINES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTINES / "lib"))
from usage_summary import owner_usage_line, snapshot_context

spec = importlib.util.spec_from_file_location(
    "memory_dgn1995", ROUTINES.parent / "memory-engine" / "memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)

EXPECTED = "weekly 98% left (5h 93% left)"


class OwnerUsageTest(unittest.TestCase):
    def test_ultra_owner_line_weekly_only(self):
        self.assertEqual(owner_usage_line({"five_hour": None,
            "seven_day": {"utilization": 51}}), "weekly 49% left")

    def test_ultra_consolidation_labels_weekly_only(self):
        self.assertEqual(memory._usage_window_text({"five_hour_absent": True,
            "seven_day_util": 99}), "7d=99%")

    def test_ultra_consolidation_defers_to_weekly_reset(self):
        usage = {"five_hour_absent": True, "seven_day_util": 99,
                 "seven_day_resets_at": "2099-11-05T02:00:00Z"}
        self.assertTrue(memory._usage_exhausted(usage))
        self.assertEqual(memory._exhausted_window(usage), "seven_day")
        self.assertEqual(memory._next_usage_reset_iso(usage), usage["seven_day_resets_at"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dgn1995-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapdir = self.root / "usage"
        self.snapdir.mkdir()
        self.snapshot = self.snapdir / "snapshot.json"
        self.env = patch.dict(os.environ, {
            "DOGANY_USAGE_SNAPSHOT_DIR": str(self.snapdir),
            "DOGANY_USAGE_SNAPSHOT_TTL_SEC": "300",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.data = {
            "five_hour": {"utilization": 7.0, "resets_at": "2099-10-11T09:00:00Z"},
            "seven_day": {"utilization": 2.0, "resets_at": "2099-10-15T09:00:00Z"},
        }
        self.write_snapshot()

    def write_snapshot(self):
        self.snapshot.write_text(json.dumps(self.data))

    def test_ticket_snapshot(self):
        # Exact output, not a substring that could conceal swapped windows.
        self.assertEqual(owner_usage_line(self.data), EXPECTED)
        self.assertEqual(self.data["five_hour"]["utilization"], 7.0)
        self.assertEqual(self.data["seven_day"]["utilization"], 2.0)

    def test_boundaries_and_fractional_percent(self):
        self.data["five_hour"]["utilization"] = 100
        self.data["seven_day"]["utilization"] = 0
        self.assertEqual(owner_usage_line(self.data), "weekly 100% left (5h 0% left)")
        self.data["seven_day"]["utilization"] = 2.25
        self.assertEqual(owner_usage_line(self.data), "weekly 97.75% left (5h 0% left)")

    def test_invalid_percent_never_becomes_available_quota(self):
        for invalid in (None, True, "7", -1, 101, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                self.data["seven_day"]["utilization"] = invalid
                self.assertEqual(owner_usage_line(self.data),
                                 "weekly unavailable (5h 93% left)")
        for invalid in ({}, [], None, {"seven_day": []}):
            self.assertEqual(owner_usage_line(invalid),
                             "weekly unavailable (5h unavailable)")

    def test_hook_injects_fresh_line_before_historical_recall(self):
        old = "Historical dispatch: 7d remaining=7%"
        context = memory._hook_compose(old, "same-day prose", "ledger")
        self.assertIn('owner_usage_line="' + EXPECTED + '"', context)
        self.assertLess(context.index(EXPECTED), context.index(old))
        self.assertIn("supersedes earlier conversation/dispatch figures", context)
        # The no-recall early exit takes the same compose path.
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            memory._emit_empty()
        self.assertIn(EXPECTED, json.loads(output.getvalue())[
            "hookSpecificOutput"]["additionalContext"])

    def test_next_turn_reads_redeemed_reset(self):
        self.data["seven_day"]["utilization"] = 93
        self.write_snapshot()
        self.assertIn("weekly 7% left", memory._hook_usage_context())
        self.data["seven_day"]["utilization"] = 2
        self.write_snapshot()
        self.assertIn(EXPECTED, memory._hook_usage_context())

    def test_missing_stale_future_or_malformed_snapshot_is_unknown(self):
        for delta in (-301, 60):
            now = time.time() + delta
            os.utime(self.snapshot, (now, now))
            self.assertIn("Current usage unavailable", snapshot_context())
            self.assertNotIn(EXPECTED, snapshot_context())
        for raw in ("broken", "[]", "null"):
            self.snapshot.write_text(raw)
            self.assertIn("Current usage unavailable", snapshot_context())
        self.snapshot.unlink()
        self.assertIn("Current usage unavailable", memory._hook_usage_context())

    def test_crossed_reset_does_not_claim_old_quota(self):
        self.data["seven_day"]["resets_at"] = "2000-01-01T00:00:00Z"
        self.write_snapshot()
        self.assertIn("weekly unavailable (5h 93% left)", snapshot_context())

    def test_report_json_and_gate_preserve_used_semantics(self):
        ws = self.root / "workspace"
        dest = ws / "routines"
        (dest / "lib").mkdir(parents=True)
        (ws / ".claude").mkdir()
        for name in ("claude-usage.sh", "usage-gate.sh"):
            shutil.copy2(ROUTINES / name, dest / name)
        shutil.copy2(ROUTINES / "lib" / "usage_summary.py", dest / "lib")
        shutil.copy2(ROUTINES / "lib" / "usage_card.py", dest / "lib")
        bindir = self.root / "bin"
        bindir.mkdir()
        curl = bindir / "curl"
        curl.write_text("#!/bin/sh\necho unexpected-network-call >&2\nexit 91\n")
        curl.chmod(0o755)
        env = dict(os.environ, HOME=str(self.root), LOCALE="en",
                   PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        def run(script, *args):
            result = subprocess.run(["bash", str(dest / script), *args],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("unexpected-network-call", result.stderr)
            return result.stdout
        doc = json.loads(run("claude-usage.sh", "--json"))
        self.assertEqual(doc["owner_usage_line"], EXPECTED)
        self.assertEqual(doc["five_hour"], self.data["five_hour"])
        self.assertEqual(doc["seven_day"], self.data["seven_day"])
        report = run("claude-usage.sh")
        self.assertNotIn(EXPECTED, report)   # DGN-2060: card has no summary line
        self.assertIn("] 7%", report)
        self.assertIn("] 2%", report)
        self.assertIn("USAGE_GATE: pass five_hour=7 seven_day=2", run("usage-gate.sh"))
        # Failed refresh: the report still discloses the age of its numbers.
        ago = time.time() - 400
        os.utime(self.snapshot, (ago, ago))
        (self.snapdir / "backoff-until").write_text(str(int(time.time()) + 300))
        stale = run("claude-usage.sh")
        self.assertIn("live lookup failed -- last value from", stale)

    def offline_reader(self):
        ws = self.root / "ultra-workspace"
        dest = ws / "routines"
        (dest / "lib").mkdir(parents=True)
        for name in ("claude-usage.sh", "usage-gate.sh", "usage-gate.py"):
            shutil.copy2(ROUTINES / name, dest / name)
        for name in ("usage_summary.py", "usage_card.py"):
            shutil.copy2(ROUTINES / "lib" / name, dest / "lib")
        credentials = self.root / ".claude"
        credentials.mkdir()
        (credentials / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fixture", "expiresAt": 4102444800000}}))
        bindir = self.root / "bin"
        bindir.mkdir()
        calls = self.root / "curl-calls"
        curl = bindir / "curl"
        curl.write_text('#!/bin/sh\ncat >/dev/null\necho call >> "$STUB_CALLS"\n'
                        'printf \'{}\\n__HTTP_STATUS__:429\'\n')
        curl.chmod(0o755)
        env = dict(os.environ, HOME=str(self.root), LOCALE="en", STUB_CALLS=str(calls),
                   PATH=str(bindir) + os.pathsep + os.environ["PATH"])

        def run(script, *args):
            return subprocess.run(["bash", str(dest / script), *args], env=env,
                                  capture_output=True, text=True, timeout=10)
        return run, calls, ws

    def test_ultra_failed_refresh_preserves_only_known_windows(self):
        run, calls, ws = self.offline_reader()
        self.data["five_hour"] = None
        self.write_snapshot()
        ago = time.time() - 400
        os.utime(self.snapshot, (ago, ago))
        doc = json.loads(run("claude-usage.sh", "--json").stdout)
        self.assertEqual(calls.read_text().splitlines(), ["call"])
        self.assertEqual(doc["_snapshot"]["source"], "stale")
        self.assertEqual(doc["owner_usage_line"], "weekly 98% left")
        self.assertIsNone(doc["five_hour"])
        card = run("claude-usage.sh").stdout
        self.assertIn("live lookup failed", card)
        self.assertNotIn("\n5h\n", card)
        gate = run("usage-gate.sh")
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn("USAGE_GATE: pass seven_day=2", gate.stdout)

        # Mutate only the weekly reset: null 5h cannot waive an unknown 7d.
        self.data["seven_day"]["resets_at"] = "2000-01-01T00:00:00Z"
        self.write_snapshot()
        (self.snapdir / "backoff-until").unlink()
        doc = json.loads(run("claude-usage.sh", "--json").stdout)
        self.assertEqual(calls.read_text().splitlines(), ["call", "call"])
        self.assertEqual(doc["_snapshot"]["source"], "stale")
        self.assertEqual(doc["owner_usage_line"], "weekly unavailable")
        card = run("claude-usage.sh").stdout
        self.assertNotIn("\n5h\n", card)
        self.assertIn("[" + "?" * 20 + "] ?%", card)
        (ws / ".claude").mkdir()
        sentinel = ws / ".claude/.usage-gate-approved"
        sentinel.write_text("1")
        self.assertEqual(run("usage-gate.sh").returncode, 2)
        self.assertEqual(sentinel.read_text(), "1")

    def test_missing_five_hour_after_failed_refresh_is_unknown(self):
        run, _, _ = self.offline_reader()
        del self.data["five_hour"]
        self.write_snapshot()
        ago = time.time() - 400
        os.utime(self.snapshot, (ago, ago))
        self.assertIn("(5h unavailable)", json.loads(run("claude-usage.sh", "--json").stdout)["owner_usage_line"])
        self.assertIn("\n5h\n", run("claude-usage.sh").stdout)
        self.assertEqual(run("usage-gate.sh").returncode, 2)

    def test_ultra_local_fallback_cannot_outlive_weekly_reset(self):
        run, _, ws = self.offline_reader()
        (ws / "routines/claude-usage.sh").write_text("#!/bin/sh\nexit 1\n")
        (ws / ".claude").mkdir()
        cache = ws / ".claude/.usage-cache.json"
        data = {"five_hour_absent": True, "seven_day_util": 2,
                "seven_day_resets_at": "2099-01-01T00:00:00Z"}
        cache.write_text(json.dumps(data))
        self.assertEqual(run("usage-gate.sh").returncode, 0)
        data["seven_day_resets_at"] = "2000-01-01T00:00:00Z"
        cache.write_text(json.dumps(data))
        self.assertEqual(run("usage-gate.sh").returncode, 2)


if __name__ == "__main__":
    unittest.main()
