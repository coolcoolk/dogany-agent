#!/usr/bin/env python3
# test-lane: slow
# test-component: memory-classification
# test-tier: 2
# test-prereq: tmp-files, subprocess
# test-new-component: slow half of the classify-carryover suite, split from test-classify-carryover.py to keep that file under the 5 s fast budget
# test-slow-reason: each case drives the real dawn queue for 2-4 chunk rounds (about 8 process spawns per run, 0.8-2.2 s each)
# promise: a busy dawn drains after the queue in fair, bounded rounds and a continuation empties without an extra run
# promise: a drained backlog does not alert on new notes next dawn; saved carryover bypasses a stale weekly marker
# promise: zero-progress and failed continuations yield or retain retry without alarm
# promise: non-shrinking backlog on two consecutive dawns alerts; same-day, gap and shrinking dawns do not
# promise: an idle caller defers a busy turn then drains after the deadline
"""DGN-2079: real chunk checkpoints -> wrapper -> queue verdict, offline."""
import argparse
import ast
import asyncio
import logging
import fcntl
import signal
from types import SimpleNamespace
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

TEMPLATE = Path(__file__).resolve().parents[2]
MEMORY = TEMPLATE / "memory-engine/memory.py"
CHECK = TEMPLATE / "routines/classify-inbox-check.sh"
QUEUE = TEMPLATE / "routines/dawn-memory-queue.sh"

# The real engine runs against scratch files with a deterministic LLM and
# clock. No API, index, push, service or long-running process is invoked.
DRIVER = '''
import argparse, importlib.util, json, os, sys, subprocess
from pathlib import Path
spec = importlib.util.spec_from_file_location("memory", os.environ["TEST_MEMORY"])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
clock = [0.0]
m.time.monotonic = lambda: clock[0]
def classify(items, topics):
    # The result must match the latest durable inbox before every new call,
    # including after a prior chunk succeeds and before a later one fails.
    with open(os.environ["DOGANY_CLASSIFY_PROGRESS_FILE"]) as f:
        checkpoint = json.load(f)
    assert checkpoint["remaining"] == len(m._inbox_items(os.path.join(m.MEMORIES_DIR, "inbox.md")))
    clock[0] += float(os.environ.get("TEST_CHUNK_SECONDS", "40"))
    if os.environ.get("TEST_MODE") == "fail":
        return {}, False
    if os.environ.get("TEST_MODE") == "retain":
        return {}, True
    return {i: ("FILE", "topic.md") for i in range(1, len(items) + 1)}, True
m._run_classifier = classify
m._classify_reindex = lambda deadline: True
m._send_silent_report = lambda report: (_ for _ in ()).throw(AssertionError("push"))
cmd = sys.argv[1]
if cmd == "inbox-count":
    sys.exit(m.cmd_inbox_count(None))
if cmd == "index":
    raise AssertionError("carryover must not reindex before classifying")
assert cmd == "classify-inbox"
runs_file = Path(os.environ["HOME"]) / "classify-runs"
runs = int(runs_file.read_text()) + 1 if runs_file.exists() else 1
runs_file.write_text(str(runs))
with (Path(os.environ["HOME"]) / "events").open("a") as f:
    print("a:classify", file=f)
if runs > 1:
    if os.environ.get("TEST_CONTINUATION_MODE"):
        os.environ["TEST_MODE"] = os.environ["TEST_CONTINUATION_MODE"]
    if os.environ.get("TEST_LOCK_PROBE"):
        result = subprocess.run(["/bin/bash", os.environ["TEST_QUEUE"]],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 1 and "another dmq instance is running" in result.stdout
if os.environ.get("TEST_QUEUE_RUN"):
    lock = Path(os.environ["HOME"]) / ".dogany/cron-queue/dmq-runner/runner.lock/pid"
    os.kill(int(lock.read_text()), 0)
    # Each invocation gets a fresh budget even when earlier chunks used it up.
    assert 800 < float(os.environ["DOGANY_CLASSIFY_DEADLINE_EPOCH"]) - m.time.time() <= 840
if os.environ.get("TEST_EXPIRED_AFTER_SLOT"):
    os.environ["DOGANY_CLASSIFY_DEADLINE_EPOCH"] = "1"
sys.exit(m.cmd_classify_inbox(argparse.Namespace(dry_run=False, no_push=True)))
'''


class CarryoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dgn2079-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inst = self.root / "instance"
        self.home = self.root / "home"
        self.mem = self.inst / "scratch-notes"
        self.bin = self.root / "bin"
        for path in (self.mem, self.bin, self.inst / "routines",
                     self.inst / "memory-engine", self.home):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CHECK, self.inst / "routines/classify-inbox-check.sh")
        shutil.copy2(QUEUE, self.inst / "routines/dawn-memory-queue.sh")
        (self.inst / "memory-engine/memory.py").write_text(textwrap.dedent(DRIVER))
        (self.mem / "topic.md").write_text("### Topic\n")
        self.env = dict(os.environ, HOME=str(self.home), MEMORIES_DIR=str(self.mem),
                        TEST_MEMORY=str(MEMORY), TEST_QUEUE=str(QUEUE), PYTHONDONTWRITEBYTECODE="1",
                        DMQ_CLI_UPDATE="0", DMQ_PERSONA_AUDIT="0", DMQ_ALERT_ROOT="",
                        PATH=str(self.bin) + ":" + os.environ["PATH"])
        for key in ("DOGANY_CLASSIFY_DEADLINE_EPOCH", "DOGANY_CLASSIFY_DAWN_DATE",
                    "DOGANY_CLASSIFY_PROGRESS_FILE", "DOGANY_CLASSIFY_CARRYOVER_FILE"):
            self.env.pop(key, None)
        self.script(self.bin / "date", '''
            case "$1" in
              +%H) if [ -f "$HOME/after-deadline" ]; then echo 05; else echo 04; fi ;;
              +%M) echo 00 ;;
              +%Y-%m-%d) echo "$TEST_DAWN_DATE" ;;
              *) exec /bin/date "$@" ;;
            esac
        ''')
        # Exercise the real queue plumbing without its watchdog processes.
        self.script(self.bin / "gtimeout", 'shift 2\nexec "$@"\n')
        self.script(self.inst / "routines/consolidate-0430.sh",
                    'echo "a:consolidate" >> "$HOME/events"\n')
        self.script(self.inst / "routines/cron-guard.sh", '''
            # --unattended)
            case " $* " in *" --queue heavy "*) ;; *) exit 99 ;; esac
            case " $* " in *" --unattended "*) ;; *) exit 99 ;; esac
            while [ "$1" != -- ]; do
                if [ "$1" = --label ]; then label="$2"; fi
                shift
            done
            export DOGANY_RETRY_MARKER="$HOME/.dogany/retry-scheduled/${label:-check}.json"
            mkdir -p "$(dirname "$DOGANY_RETRY_MARKER")"
            rm -f "$DOGANY_RETRY_MARKER"
            shift
            "$@"
            rc=$?
            if [ "$rc" -ne 0 ] && [ "$rc" -ne 78 ]; then echo "failure-alert" >> "$HOME/alerts"; fi
            exit "$rc"
        ''')
        (self.inst / ".instance.conf").touch()
        registry = self.home / ".dogany/memory-queue/instances.d"
        registry.mkdir(parents=True)
        (registry / "a.conf").write_text(f"ROOT={self.inst}\nENABLED=1\nPRIORITY=10\n")

    def script(self, path, source):
        path.write_text("#!/bin/bash\n" + textwrap.dedent(source))
        path.chmod(0o755)

    def seed(self, count):
        sep = chr(167) + "\n"
        (self.mem / "inbox.md").write_text(
            "<!-- dogany-format: section -->\n### Inbox\n" + sep
            + "".join(f"item-{i}\n" + sep for i in range(count)))

    def run_queue(self, day, **env):
        result = subprocess.run(["/bin/bash", str(QUEUE)],
                                env=dict(self.env, TEST_DAWN_DATE=day, TEST_QUEUE_RUN="1", **env),
                                text=True, capture_output=True, timeout=20)
        return result

    def run_check(self, day, **env):
        # Keep the original cross-dawn wrapper contract covered independently
        # of DMQ, which now drains successful carryover on the same day.
        return subprocess.run(
            ["/bin/bash", str(self.inst / "routines/cron-guard.sh"),
             "--queue", "heavy", "--unattended", "--",
             "/bin/bash", str(self.inst / "routines/classify-inbox-check.sh")],
            env=dict(self.env, TEST_DAWN_DATE=day, DOGANY_CLASSIFY_DAWN_DATE=day, **env),
            text=True, capture_output=True, timeout=20)

    def events(self):
        return (self.home / "events").read_text().splitlines()

    def progress(self):
        return json.loads((self.inst / "memory-engine/.classify-progress.json").read_text())

    def alerts(self):
        path = self.home / "alerts"
        return path.read_text().splitlines() if path.exists() else []

    def test_busy_dawn_drains_after_queue_in_fair_bounded_rounds(self):
        self.seed(3600)
        # A second instance must finish all initial steps before a resumes;
        # when both carry over, each gets one turn per continuation round.
        other = self.root / "other"
        (other / "routines").mkdir(parents=True)
        (other / ".instance.conf").touch()
        shutil.copy2(self.inst / "routines/cron-guard.sh", other / "routines/cron-guard.sh")
        self.script(other / "routines/consolidate-0430.sh",
                    'echo "b:consolidate" >> "$HOME/events"\n')
        self.script(other / "routines/classify-inbox-check.sh", '''
            echo "b:classify" >> "$HOME/events"
            if [ ! -f "$HOME/b-ran" ]; then
                echo "1 1" > "$DOGANY_CLASSIFY_CARRYOVER_FILE"
                touch "$HOME/b-ran"
            fi
        ''')
        (other / "routines/persona-audit.py").write_text(
            'import os\nfrom pathlib import Path\n'
            'h = Path(os.environ["HOME"])\n'
            'with (h / "events").open("a") as f: f.write("b:audit\\n")\n'
            '(h / "after-deadline").touch()\n')
        registry = self.home / ".dogany/memory-queue/instances.d"
        (registry / "b.conf").write_text(f"ROOT={other}\nENABLED=1\nPRIORITY=20\n")
        result = self.run_queue("2026-10-14", TEST_LOCK_PROBE="1", DMQ_PERSONA_AUDIT="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verdict=OK", result.stdout)
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertIn("continuations=3", result.stdout)
        self.assertIn("classify continuation scheduled after the dawn queue", result.stdout)
        self.assertEqual(self.events(), ["a:consolidate", "a:classify", "b:consolidate",
                                        "b:classify", "b:audit", "a:classify",
                                        "b:classify", "a:classify"])
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertEqual(self.progress()["processed"], 1000)
        self.assertEqual((self.mem / "topic.md").read_text().count("inbox-classified"), 3600)
        self.assertTrue((self.inst / ".classify_inbox_last").exists())
        self.assertFalse((self.inst / ".classify_inbox_carryover").exists())
        self.assertFalse((self.inst / ".classify_reindex_pending").exists())
        self.assertFalse((self.home / ".dogany/cron-queue/dmq-runner/runner.lock").exists())
        self.assertEqual(self.alerts(), [])

    def test_continuation_empties_without_an_extra_run(self):
        self.seed(2300)
        result = self.run_queue("2026-10-14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertIn("continuations=1", result.stdout)
        self.assertEqual(self.events(), ["a:consolidate", "a:classify", "a:classify"])
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertEqual(self.alerts(), [])

    def test_drained_backlog_does_not_alert_on_new_notes_next_dawn(self):
        for day in ("2026-10-14", "2026-10-15"):
            self.seed(2300)
            (self.inst / ".classify_inbox_last").unlink(missing_ok=True)
            result = self.run_queue(day)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("continuations=1", result.stdout)
            self.assertEqual(self.progress()["remaining"], 0)
            self.assertFalse((self.inst / "memory-engine/.classify-dawn-history.json").exists())
        self.assertEqual(self.alerts(), [])

    def test_zero_progress_continuation_yields_without_alarm(self):
        self.seed(2300)
        result = self.run_queue("2026-10-14", TEST_CONTINUATION_MODE="retain")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verdict=OK", result.stdout)
        self.assertIn("continuations=1", result.stdout)
        self.assertIn("yielding until idle", result.stdout)
        self.assertEqual((self.home / "classify-runs").read_text(), "2")
        self.assertEqual(self.progress()["processed"], 0)
        self.assertEqual(self.progress()["remaining"], 1000)
        self.assertTrue((self.inst / ".classify_inbox_carryover").exists())
        self.assertFalse((self.inst / ".classify_inbox_last").exists())
        self.assertTrue((self.inst / ".classify_reindex_pending").exists())
        self.assertEqual(self.alerts(), [])

    def test_failed_continuation_retains_retry_and_stops(self):
        self.seed(2300)
        result = self.run_queue("2026-10-14", TEST_CONTINUATION_MODE="fail")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("retry=1", result.stdout)
        self.assertIn("continuations=1", result.stdout)
        self.assertEqual((self.home / "classify-runs").read_text(), "2")
        self.assertEqual(self.progress()["remaining"], 1000)
        self.assertTrue((self.inst / ".classify_retry_pending").exists())
        self.assertEqual(self.alerts(), [])

    def idle_method(self):
        # Run the production coroutine without importing network/SDK clients.
        source = TEMPLATE / "bridge/bot.py"
        tree = ast.parse(source.read_text())
        method = next((n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "_classify_carryover_loop"), None)
        self.assertIsNotNone(method, "bridge has no idle continuation caller")
        lifecycle = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                         and n.name == "_run_async")
        calls = [n for n in ast.walk(lifecycle) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(n.func, ast.Attribute)
                            and n.func.attr == "_classify_carryover_loop" for n in calls))
        self.assertTrue(any(isinstance(n.func, ast.Attribute) and n.func.attr == "cancel"
                            and isinstance(n.func.value, ast.Name)
                            and n.func.value.id == "classify_carryover_task" for n in calls))
        scope = dict(Path=Path, PROJECT_ROOT=self.inst, asyncio=asyncio, os=os,
                     signal=signal, logger=logging.getLogger("idle-test"),
                     config=SimpleNamespace(allowed_user_ids=[1]))
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), scope)
        return scope["_classify_carryover_loop"]

    def test_idle_caller_defers_busy_turn_then_drains_after_deadline(self):
        self.seed(2300)
        (self.inst / ".classify_inbox_carryover").touch()
        (self.home / "after-deadline").touch()
        # Idle ticks must preserve yesterday's history until work is drained,
        # rather than raise a same-size dawn alarm while retrying today.
        history = self.inst / "memory-engine/.classify-dawn-history.json"
        history.write_text(json.dumps(dict(day="2026-10-14", remaining=1000)))
        method = self.idle_method()
        ticks = []
        async def tick(seconds):
            self.assertEqual(seconds, 300)
            ticks.append(seconds)
            if len(ticks) > 3:
                raise asyncio.CancelledError
        owner = SimpleNamespace(_any_user_turn_running=lambda: len(ticks) == 1)
        with patch.dict(os.environ, dict(self.env, TEST_DAWN_DATE="2026-10-15", TEST_QUEUE_RUN="1"), clear=True), \
                patch.object(asyncio, "sleep", tick):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(method(owner))
        self.assertEqual(self.events(), ["a:classify", "a:classify"])
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertTrue((self.inst / ".classify_inbox_last").exists())
        self.assertFalse((self.inst / ".classify_inbox_carryover").exists())
        self.assertFalse(history.exists())
        self.assertEqual(self.alerts(), [])






    def test_saved_carryover_bypasses_stale_weekly_marker(self):
        self.seed(2300)
        first = self.run_check("2026-10-14")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.progress()["remaining"], 1000)
        self.assertEqual(self.progress()["processed"], 1300)
        self.assertFalse((self.inst / ".classify_inbox_last").exists())
        self.assertTrue((self.inst / ".classify_reindex_pending").exists())
        (self.inst / ".classify_inbox_last").touch()
        second = self.run_check("2026-10-14")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertTrue((self.inst / ".classify_inbox_last").exists())
        self.assertFalse((self.inst / ".classify_inbox_carryover").exists())
        self.assertFalse((self.inst / ".classify_reindex_pending").exists())
        self.assertFalse((self.inst / "memory-engine/.classify-dawn-history.json").exists())
        # A resolved backlog must not become the baseline for fresh notes.
        self.seed(2300)
        (self.inst / ".classify_inbox_last").unlink()
        third = self.run_check("2026-10-16")
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertEqual(self.progress()["remaining"], 1000)
        self.assertEqual(self.alerts(), [])

    def test_nonshrinking_two_consecutive_dawns_alerts_despite_progress(self):
        self.seed(2300)
        self.assertEqual(self.run_check("2026-10-14").returncode, 0)
        # New incoming notes replace yesterday's drained work. The daily
        # remainder stays at 1000 despite 1300 items processed each dawn.
        self.seed(2300)
        second = self.run_check("2026-10-15")
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertIn("classification stalled", second.stdout)
        self.assertEqual(self.progress()["processed"], 1300)
        self.assertEqual(self.alerts(), ["failure-alert"])



    def test_same_day_and_gap_do_not_count_as_consecutive_dawns(self):
        for day in ("2026-10-14", "2026-10-14", "2026-10-16"):
            self.seed(2300)
            result = self.run_check(day)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.alerts(), [])

    def test_shrinking_carryover_stays_ok(self):
        for day, count in (("2026-10-14", 2300), ("2026-10-15", 2200)):
            self.seed(count)
            result = self.run_check(day)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(self.progress()["remaining"], 0)
        self.assertEqual(self.alerts(), [])





if __name__ == "__main__":
    unittest.main()
