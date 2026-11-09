#!/usr/bin/env python3
# test-lane: fast
# test-component: memory-classification
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: Real engine checkpoints through wrapper and queue; retry tests stub the engine, chunk tests omit the queue.
# promise: a complete initial run schedules no continuation; an exhausted queue budget yields and keeps the pending index
# promise: a slot wait consumes budget without starting a chunk; a parallel scheduled wrapper never touches saved work
# promise: idle mode runs only saved work, respects the runner lock and a cancelled idle worker is reaped
# promise: classifier failure keeps the automatic retry contract; retained items alert only after two dawns
# promise: the completion report is sent only when its timeout fits and the reindex tail is bounded
# Multi-round drain / dawn-history cases: test-memory-classification-carryover.py (slow).
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


    def test_idle_lifecycle_cancellation_reaps_its_worker(self):
        (self.inst / ".classify_inbox_carryover").touch()
        pidfile = self.home / "idle-pid"
        # A real child, with the same group ownership as the queue. Never a
        # bridge/service process; cancellation must leave no worker running.
        self.script(self.inst / "routines/dawn-memory-queue.sh", """
            exec /usr/bin/python3 -c 'import os, time
from pathlib import Path
Path(os.environ["HOME"], "idle-pid").write_text(str(os.getpid()))
time.sleep(60)'
        """)
        method = self.idle_method()
        real_sleep = asyncio.sleep
        async def tick(seconds):
            await real_sleep(0 if seconds == 300 else seconds)
        async def exercise():
            task = asyncio.create_task(method(SimpleNamespace(_any_user_turn_running=lambda: False)))
            try:
                for _ in range(200):
                    if pidfile.exists():
                        break
                    await real_sleep(0.01)
                self.assertTrue(pidfile.exists(), "idle worker did not start")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        with patch.dict(os.environ, self.env, clear=True), patch.object(asyncio, "sleep", tick):
            asyncio.run(exercise())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        self.assertTrue((self.inst / ".classify_inbox_carryover").exists())

    def test_parallel_scheduled_wrapper_yields_without_touching_saved_work(self):
        self.seed(100)
        carry = self.inst / ".classify_inbox_carryover"
        carry.touch()
        (self.inst / ".classify_reindex_pending").touch()
        before = (self.mem / "inbox.md").read_bytes()
        with (self.inst / "memory-engine/.classify-run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.run_check("2026-10-14")
            self.assertEqual(busy.returncode, 0, busy.stdout + busy.stderr)
            self.assertIn("classification already running", busy.stdout)
            self.assertEqual((self.mem / "inbox.md").read_bytes(), before)
            self.assertTrue(carry.exists())
            self.assertFalse((self.home / "events").exists())
        resumed = self.run_queue("2026-10-14")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.progress()["remaining"], 0)
        self.assertEqual(self.alerts(), [])

    def test_exhausted_queue_budget_yields_and_keeps_pending_index(self):
        self.seed(2300)
        (self.inst / ".classify_reindex_pending").touch()
        (self.inst / ".classify_inbox_carryover").touch()
        result = self.run_queue("2026-10-14", TEST_EXPIRED_AFTER_SLOT="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("continuations=0", result.stdout)
        self.assertIn("yielding until idle", result.stdout)
        self.assertEqual(self.progress()["remaining"], 2300)
        self.assertEqual((self.home / "classify-runs").read_text(), "1")
        self.assertTrue((self.inst / ".classify_reindex_pending").exists())
        self.assertEqual(self.alerts(), [])

    def test_idle_mode_only_runs_saved_work_and_respects_runner_lock(self):
        self.seed(10)
        args = ["/bin/bash", str(QUEUE), "--carryover-root", str(self.inst)]
        env = dict(self.env, TEST_DAWN_DATE="2026-10-14")
        empty = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertFalse((self.home / "events").exists())
        (self.inst / ".classify_inbox_carryover").touch()
        lock = self.home / ".dogany/cron-queue/dmq-runner/runner.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(os.getpid()))
        busy = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(busy.returncode, 1, busy.stdout + busy.stderr)
        self.assertIn("another dmq instance is running", busy.stdout)
        self.assertFalse((self.home / "events").exists())
        self.assertTrue((self.inst / ".classify_inbox_carryover").exists())

    def test_complete_initial_run_does_not_schedule_continuation(self):
        self.seed(10)
        result = self.run_queue("2026-10-14")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("continuations=0", result.stdout)
        self.assertEqual(self.events(), ["a:consolidate", "a:classify"])



    def test_retained_items_alert_only_after_two_dawns(self):
        self.seed(100)
        first = self.run_queue("2026-10-14", TEST_MODE="retain")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.progress()["processed"], 0)
        self.assertEqual(self.alerts(), [])
        second = self.run_queue("2026-10-15", TEST_MODE="retain")
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertIn("verdict=DEGRADED", second.stdout)
        self.assertFalse((self.inst / ".classify_inbox_last").exists())
        self.assertEqual(self.alerts(), ["failure-alert"])

    def test_classifier_failure_keeps_the_automatic_retry_contract(self):
        self.seed(100)
        for expected in (78, 1):
            result = subprocess.run(
                ["/bin/bash", str(self.inst / "routines/classify-inbox-check.sh")],
                env=dict(self.env, TEST_MODE="fail"), text=True,
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(self.progress()["remaining"], 100)
        self.assertFalse((self.inst / ".classify_inbox_last").exists())



    def test_slot_wait_consumes_budget_without_starting_a_chunk(self):
        self.seed(2300)
        (self.inst / ".classify_inbox_carryover").touch()
        (self.inst / ".classify_reindex_pending").touch()
        result = subprocess.run(
            ["/bin/bash", str(self.inst / "routines/classify-inbox-check.sh")],
            env=dict(self.env, DOGANY_CLASSIFY_DEADLINE_EPOCH="1"),
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.progress()["processed"], 0)
        self.assertEqual(self.progress()["pending_chunks"], 23)
        self.assertNotIn("inbox-classified", (self.mem / "topic.md").read_text())
        self.assertTrue((self.inst / ".classify_reindex_pending").exists())

    def test_completion_report_is_preserved_only_when_timeout_fits(self):
        spec = importlib.util.spec_from_file_location("memory_report_test", MEMORY)
        memory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(memory)
        for tail_time, should_send in ((100, True), (800, False)):
            self.seed(1)
            clock = [0]
            def reindex(deadline):
                clock[0] = tail_time
                return True
            with patch.object(memory, "MEMORIES_DIR", str(self.mem)), \
                    patch.object(memory, "_classify_deadline", return_value=840), \
                    patch.object(memory.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(memory, "_run_classifier", return_value=({1: ("FILE", "topic.md")}, True)), \
                    patch.object(memory, "_classify_reindex", side_effect=reindex), \
                    patch.object(memory, "_send_silent_report", return_value=True) as send, \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = memory.cmd_classify_inbox(argparse.Namespace(dry_run=False, no_push=False))
                self.assertEqual(rc, 0)
                self.assertEqual(send.called, should_send)

    def test_reindex_tail_is_bounded(self):
        spec = importlib.util.spec_from_file_location("memory_test", MEMORY)
        memory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(memory)
        with patch.object(memory.time, "monotonic", return_value=100), \
                patch.object(memory.subprocess, "run", side_effect=subprocess.TimeoutExpired("index", 15)) as run:
            self.assertFalse(memory._classify_reindex(120))
            self.assertEqual(run.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
