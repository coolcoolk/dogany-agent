#!/usr/bin/env python3
# test-component: session-job-notices
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-lane: manual devkit-owned suite; run against extracted-devkit/framework
"""DGN-1691 phase 2: session background jobs follow the sibling protocol.

Locks:
  (a) language gate: a background launch whose description is not in the
      instance language is denied; an owner-language one passes; foreground
      Bash, non-owner sessions and English instances are left alone;
  (b) START batching: a shell job's START joins the dispatch start-notice
      queue -- one `▶️ ... 작업 시작 (N건)` message with a sibling spawned
      in the same window;
  (c) END exactly-once across Stop-hook reruns, only after a START;
  (d) failure path: failed -> 작업실패 line, killed -> 멈췄습니다 line,
      self-stopped -> quiet, no status -> quiet;
  (e) workbench row: "<name> · <HH:MM> (<N>분~)" -- the description alone,
      the same name in START/END; the slot (CLI name for Bash, tool name
      otherwise) is internal and shows only when there is no description
      (DGN-1820, superseding the DGN-1727 "<slot>: " prefix);
  (f) Monitor EVENTS do not close a job; old history never notifies.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# The hooks stand down inside a dispatch worker (DOGANY_DISPATCH_WORKER); the
# fixtures model an owner session, so a run launched from a worker must not
# inherit it (13 reds otherwise: empty hook output, no state.json).
from lib_resource_settings_isolation import isolate_resource_settings
isolate_resource_settings()

os.environ.pop("DOGANY_DISPATCH_WORKER", None)

HERE = Path(__file__).resolve().parent
ROUTINES = HERE.parent
REPO = ROUTINES.parent
sys.path.insert(0, str(ROUTINES / "lib"))
import bg_job_notice as bg  # noqa: E402

spec = importlib.util.spec_from_file_location("sf", ROUTINES / "status-footer.py")
sf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sf)

SESSION = "sess-owner-1"
# Dispatcher stand-in: one argv line per call, written in a single append so
# two detached senders can never interleave.
STUB = '#!/bin/bash\nline="$(printf "%%s|" "$@")"\necho "$line" >> "%s"\n'


def _read_lines(path):
    with open(path) as fh:
        return [l for l in fh.read().splitlines() if l]


def _await_lines(path, want, timeout=15.0):
    """Lines of a log written by DETACHED senders: poll until `want` lines
    exist or the timeout passes (a fixed sleep flaked under the parallel
    test lane, dev.73 cut 2026-10-05). Returns whatever is there at the end."""
    deadline = time.time() + timeout
    lines = []
    while time.time() < deadline:
        if os.path.exists(path):
            lines = _read_lines(path)
            if len(lines) >= want:
                break
        time.sleep(0.05)
    # Settle: a late duplicate send must still be caught by the count check.
    time.sleep(0.3)
    return _read_lines(path) if os.path.exists(path) else lines
HOOK = ROUTINES / "bg-job-notice.py"


class Env(object):
    """Scratch state/conf/sessions; a recording sender stands in for the
    dispatch rails unless a test wires the real script."""

    def __init__(self, lang="ko"):
        self.tmp = tempfile.mkdtemp(prefix="bgnotice-")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.conf = os.path.join(self.tmp, "agent.conf")
        with open(self.conf, "w") as fh:
            fh.write("AGENT_LANG=%s\n" % lang)
        self.sessions = os.path.join(self.tmp, "sessions.json")
        with open(self.sessions, "w") as fh:
            json.dump({"chat": {"session_id": SESSION}}, fh)
        self.transcript = os.path.join(self.tmp, SESSION + ".jsonl")
        self.sent = []
        self.env = {
            "DOGANY_BOT_DATA_DIR": self.data,
            "DOGANY_AGENT_CONF": self.conf,
            "DOGANY_SESSIONS_FILE": self.sessions,
            "DOGANY_BG_NOTICE_STATE": os.path.join(self.data, "state.json"),
        }
        self._patch = patch.dict(os.environ, self.env)
        self._patch.start()

    def sender(self, argv):
        self.sent.append(list(argv))

    def close(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, mode, payload, extra_env=None):
        env = dict(os.environ)
        env.update(extra_env or {})
        out = subprocess.run(
            [sys.executable, str(HOOK), mode], input=json.dumps(payload),
            capture_output=True, text=True, env=env, timeout=30)
        self.assertrc = out.returncode
        return out.stdout.strip()


def bash_launch(desc, background=True, session=SESSION):
    return {"tool_name": "Bash", "transcript_path": "/x/%s.jsonl" % session,
            "tool_input": {"command": "run-tests.sh", "description": desc,
                           "run_in_background": background}}


def agent_launch(desc, tool="Agent", background=None, session=SESSION,
                 subagent="claude"):
    inp = {"description": desc, "prompt": "do it", "subagent_type": subagent}
    if background is not None:
        inp["run_in_background"] = background
    return {"tool_name": tool, "transcript_path": "/x/%s.jsonl" % session,
            "tool_input": inp}






class NoticeStateTests(unittest.TestCase):
    def setUp(self):
        self.e = Env("ko")

    def tearDown(self):
        self.e.close()

    def start(self, tid="b1", tool="Bash", name="전체 테스트 실행", at=1000.0):
        return bg.record_start(tid, tool, name, at, now=at, sender=self.e.sender)

    def test_start_once(self):
        self.assertEqual(self.start(), "sent")
        self.assertEqual(self.start(), "dup")
        self.assertEqual(self.e.sent, [["--start-notice-enqueue", "--display-name",
                                        "전체 테스트 실행", "--model-slot", "Shell"]])

    def test_slot_is_internal_in_start_and_end(self):
        # DGN-1820: the owner-visible name is the description alone; the
        # slot rides only as --model-slot and in the state record.
        bg.record_start("g1", "Bash", "저장소 업로드", 1000.0, now=1000.0,
                        sender=self.e.sender, slot="git")
        self.assertEqual(self.e.sent[0], [
            "--start-notice-enqueue", "--display-name", "저장소 업로드",
            "--model-slot", "git"])
        bg.record_end("g1", "completed", 1330.0, now=1400.0, sender=self.e.sender)
        end = self.e.sent[-1]
        self.assertEqual(end[end.index("--display-name") + 1], "저장소 업로드")
        self.assertEqual(end[end.index("--model-slot") + 1], "git")
        with open(bg.state_path()) as fh:
            rec = json.load(fh)["g1"]
        self.assertEqual((rec["name"], rec["slot"]), ("저장소 업로드", "git"))

    def test_owner_screen_repro_no_command_head(self):
        # DGN-1820 repro (owner 2026-10-01 17:48): "+%s): 헬스팩 ..." on
        # screen; the command opened with S=$(date +%s).
        cmd = "S=$(date +%s); bash x.sh"
        slot = bg.model_slot("Bash", {"command": cmd})
        bg.record_start("h1", "Bash", "헬스팩 테스트 다시 확인", 1000.0,
                        now=1000.0, sender=self.e.sender, slot=slot)
        name = self.e.sent[0][self.e.sent[0].index("--display-name") + 1]
        self.assertEqual(name, "헬스팩 테스트 다시 확인")
        self.assertNotIn("%s", name)
        self.assertNotIn(":", name)

    def test_pre_1820_state_end_drops_the_prefix(self):
        # A job STARTed by the old code stored "<slot>: <description>".
        state = {"s1": {"tool": "Bash", "name": "sleep: 헬스팩 통합본 테스트 완료 대기",
                        "slot": "sleep", "start": 1000.0, "recorded": 1000.0,
                        "queued": 1000.0, "start_sent": True, "end_sent": False}}
        with open(bg.state_path(), "w") as fh:
            json.dump(state, fh)
        bg.record_end("s1", "completed", 1330.0, now=1400.0, sender=self.e.sender)
        end = self.e.sent[-1]
        self.assertEqual(end[end.index("--display-name") + 1],
                         "헬스팩 통합본 테스트 완료 대기")

    def test_owner_label(self):
        self.assertEqual(bg.owner_label("git", "저장소 업로드"), "저장소 업로드")
        self.assertEqual(bg.owner_label("git", ""), "git")
        self.assertEqual(bg.owner_label("git", "git"), "git")
        self.assertEqual(bg.owner_label("", "저장소 업로드"), "저장소 업로드")

    def test_started_open_ids(self):
        # DGN-1769: the workbench keeps a job the owner was told started.
        self.assertEqual(bg.started_open_ids(), set())
        self.start("b1")
        self.start("b2")
        self.start("b3", name="Run full tests")  # refused: never started
        bg.record_end("b2", "completed", 1300.0, now=1300.0, sender=self.e.sender)
        self.assertEqual(bg.started_open_ids(), {"b1"})

    def test_refused_name_never_notifies(self):
        self.assertEqual(self.start(name="Run full tests"), "refused")
        self.assertEqual(bg.record_end("b1", "completed", 2000.0, now=2000.0,
                                       sender=self.e.sender), "skip")
        self.assertEqual(self.e.sent, [])

    def test_end_exactly_once_with_elapsed(self):
        self.start(at=1000.0)
        closed = {"b1": ("completed", 1330.0)}
        for _ in range(3):  # three Stop-hook reruns over the same transcript
            bg.sync_from_scan({}, closed, 1200, now=1400.0, sender=self.e.sender)
        ends = [a for a in self.e.sent if a[0] == "--end-notice"]
        self.assertEqual(len(ends), 1)
        argv = ends[0]
        self.assertEqual(argv[argv.index("--result") + 1], "completed")
        self.assertEqual(argv[argv.index("--elapsed-secs") + 1], "330")
        self.assertEqual(argv[argv.index("--model-slot") + 1], "Shell")
        self.assertEqual(argv[argv.index("--end-clock") + 1],
                         time.strftime("%H:%M", time.localtime(1330.0)))
        self.assertNotIn("--not-before", argv)  # START window long passed

    def test_end_waits_for_its_start_window(self):
        self.start(at=1000.0)
        bg.record_end("b1", "completed", 1003.0, now=1003.0, sender=self.e.sender)
        argv = self.e.sent[-1]
        self.assertEqual(argv[argv.index("--not-before") + 1],
                         str(int(1000.0 + bg.start_window() + 2)))

    def test_end_without_start_is_skipped(self):
        bg.sync_from_scan({}, {"old": ("completed", 5.0)}, 1200, now=10.0,
                          sender=self.e.sender)
        self.assertEqual(self.e.sent, [])

    def test_failure_statuses_pass_through(self):
        for tid, status in (("f1", "failed"), ("k1", "killed")):
            self.start(tid=tid)
            bg.record_end(tid, status, 1100.0, now=1100.0, sender=self.e.sender)
            argv = self.e.sent[-1]
            self.assertEqual(argv[argv.index("--result") + 1], status)

    def test_self_stop_and_missing_status_are_quiet(self):
        self.start(tid="k1")
        self.assertTrue(bg.mark_self_stopped("k1", now=1001.0))
        self.assertEqual(bg.record_end("k1", "killed", 1100.0, now=1100.0,
                                       sender=self.e.sender), "quiet")
        self.start(tid="n1")
        self.assertEqual(bg.record_end("n1", "", 1100.0, now=1100.0,
                                       sender=self.e.sender), "quiet")
        self.assertEqual([a for a in self.e.sent if a[0] == "--end-notice"], [])

    def test_catchup_only_fresh_open_jobs(self):
        open_jobs = {"fresh": (1, "새 작업", 9900.0, "Monitor"),
                     "old": (0, "옛 작업", 100.0, "Bash")}
        bg.sync_from_scan(open_jobs, {}, 1200, now=10000.0, sender=self.e.sender)
        self.assertEqual(self.e.sent, [["--start-notice-enqueue", "--display-name",
                                        "새 작업", "--model-slot", "Monitor"]])


class LaunchedHookTests(unittest.TestCase):
    """PostToolUse --launched against a stub dispatcher that logs argv."""

    def setUp(self):
        self.e = Env("ko")
        self.log = os.path.join(self.e.tmp, "argv.log")
        stub = os.path.join(self.e.tmp, "dispatch-stub.sh")
        with open(stub, "w") as fh:
            fh.write(STUB % self.log)
        os.chmod(stub, 0o755)
        self.extra = {"DOGANY_BG_NOTICE_DISPATCH": stub}

    def tearDown(self):
        self.e.close()

    def calls(self):
        for _ in range(50):
            if os.path.exists(self.log):
                break
            time.sleep(0.05)
        time.sleep(0.1)
        try:
            return _read_lines(self.log)
        except FileNotFoundError:
            return []

    def state(self):
        with open(self.e.env["DOGANY_BG_NOTICE_STATE"]) as fh:
            return json.load(fh)

    def test_bash_ack_queues_start_once(self):
        payload = bash_launch("전체 테스트 실행")
        payload["tool_response"] = {"stdout": "", "backgroundTaskId": "bwnx1"}
        self.e.hook("--launched", payload, self.extra)
        self.e.hook("--launched", payload, self.extra)
        self.assertEqual(self.calls(), [
            "--start-notice-enqueue|--display-name|전체 테스트 실행|"
            "--model-slot|run-tests.sh|"])

    def test_auto_moved_start_is_backdated(self):
        payload = bash_launch("긴 빌드", background=False)
        payload["tool_response"] = {"backgroundTaskId": "bauto",
                                    "timedOutAfterMs": 120000}
        before = time.time()
        self.e.hook("--launched", payload, self.extra)
        self.assertLess(self.state()["bauto"]["start"], before - 110)

    def test_monitor_task_id_and_non_owner(self):
        payload = {"tool_name": "Monitor", "transcript_path": "/x/%s.jsonl" % SESSION,
                   "tool_input": {"description": "로그 감시"},
                   "tool_response": {"taskId": "m1", "persistent": False}}
        self.e.hook("--launched", payload, self.extra)
        other = dict(payload, transcript_path="/x/other.jsonl",
                     tool_response={"taskId": "m2"})
        self.e.hook("--launched", other, self.extra)
        self.assertEqual(sorted(self.state()), ["m1"])

    def test_taskstop_marks_self_stopped(self):
        payload = bash_launch("전체 테스트 실행")
        payload["tool_response"] = {"backgroundTaskId": "bs1"}
        self.e.hook("--launched", payload, self.extra)
        self.e.hook("--launched", {
            "tool_name": "TaskStop", "transcript_path": "/x/%s.jsonl" % SESSION,
            "tool_input": {"task_id": "bs1"},
            "tool_response": {"message": "Successfully stopped task: bs1",
                              "task_id": "bs1"}}, self.extra)
        self.assertTrue(self.state()["bs1"].get("self_stopped"))


def _launch_entries(tid="bg-1", desc="전체 테스트 실행", stamp="2026-09-24T05:00:00Z"):
    return [
        {"type": "assistant", "timestamp": stamp, "message": {"content": [
            {"type": "tool_use", "id": "call-" + tid, "name": "Bash",
             "input": {"description": desc, "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "call-" + tid,
             "content": "Command running in background with ID: %s. Output "
                        "is being written to: /tmp/o" % tid}]}},
    ]


def _notice(tid, body, stamp="2026-09-24T05:05:30Z"):
    return {"type": "attachment", "timestamp": stamp, "attachment": {
        "type": "queued_command", "commandMode": "task-notification",
        "prompt": "<task-notification>\n<task-id>%s</task-id>\n%s\n"
                  "</task-notification>" % (tid, body)}}


START = sf.datetime.fromisoformat("2026-09-24T05:00:00+00:00").timestamp()


class FooterTests(unittest.TestCase):
    def scan(self, entries, age=90):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in entries))
            out = {}
            with patch.object(sf.time, "time", return_value=START + age):
                rows = sf._collect_active_subagents(str(path), out)
            return rows, out

    def test_row_format(self):
        rows, _ = self.scan(_launch_entries(), age=6 * 60 + 5)
        stamp = time.strftime("%H:%M", time.localtime(START))
        # DGN-1820: the description alone, no slot prefix.
        self.assertEqual(rows, ["전체 테스트 실행: %s (%s)"
                                % (stamp, sf._board_str("minutes") % 6)])

    def test_row_no_slot_prefix_for_any_tool(self):
        for tool, desc in (("Monitor", "로그 감시"), ("Bash", "클라우드 대기")):
            entries = _launch_entries(desc=desc)
            entries[0]["message"]["content"][0]["name"] = tool
            if tool == "Bash":
                entries[0]["message"]["content"][0]["input"]["command"] = \
                    "cloud-wait.sh --poll"
            else:
                entries[1]["message"]["content"][0]["content"] = \
                    "Monitor started (task bg-1, timeout 300000ms)."
            rows, _ = self.scan(entries, age=90)
            self.assertEqual(len(rows), 1, (tool, rows))
            self.assertTrue(rows[0].startswith(desc + ": "), rows)

    def test_row_no_description_is_command_name_alone(self):
        entries = _launch_entries(desc="")
        entries[0]["message"]["content"][0]["input"]["command"] = "git fetch"
        entries[0]["message"]["content"][0]["input"].pop("description")
        rows, out = self.scan(entries, age=90)
        stamp = time.strftime("%H:%M", time.localtime(START))
        self.assertEqual(rows, ["git: %s (%s)"
                                % (stamp, sf._board_str("minutes") % 1)])

    def test_monitor_event_does_not_close(self):
        entries = _launch_entries() + [_notice(
            "bg-1", "<summary>Monitor event: \"x\"</summary>\n<event>line</event>")]
        rows, out = self.scan(entries)
        self.assertEqual(len(rows), 1)
        self.assertEqual(out["closed"], {})

    def test_close_carries_status_and_time(self):
        entries = _launch_entries() + [_notice("bg-1", "<status>failed</status>")]
        rows, out = self.scan(entries)
        self.assertEqual(rows, [])
        self.assertEqual(out["closed"], {"bg-1": ("failed", START + 330)})


class StopHookEndToEnd(unittest.TestCase):
    """status-footer.py main() as the Stop hook: END once across reruns."""

    def test_end_once_across_stop_reruns(self):
        e = Env("ko")
        try:
            log = os.path.join(e.tmp, "argv.log")
            stub = os.path.join(e.tmp, "stub.sh")
            with open(stub, "w") as fh:
                fh.write(STUB % log)
            os.chmod(stub, 0o755)
            now = time.time()
            iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
            entries = _launch_entries(stamp=iso(now - 300))
            Path(e.transcript).write_text(
                "\n".join(json.dumps(x) for x in entries) + "\n")
            env = dict(os.environ, DOGANY_BG_NOTICE_DISPATCH=stub,
                       DOGANY_DECISIONS_FILE=os.path.join(e.tmp, "none.md"),
                       DOGANY_LEDGER_FILE=os.path.join(e.tmp, "none-ledger.md"))
            payload = json.dumps({"transcript_path": e.transcript})

            def stop():
                subprocess.run([sys.executable, str(ROUTINES / "status-footer.py")],
                               input=payload, text=True, env=env, timeout=30,
                               capture_output=True)
            stop()  # catch-up START (the PostToolUse hook "missed" it)
            with open(e.transcript, "a") as fh:
                fh.write(json.dumps(_notice(
                    "bg-1", "<status>completed</status>", stamp=iso(now))) + "\n")
            stop()
            stop()
            lines = sorted(_await_lines(log, 2), reverse=True)  # start, end
            self.assertEqual(len(lines), 2, lines)
            self.assertTrue(lines[0].startswith("--start-notice-enqueue|"))
            self.assertTrue(lines[1].startswith("--end-notice|--display-name|"
                                                "전체 테스트 실행|--model-slot|Shell|"
                                                "--result|completed|"))
            self.assertIn("--elapsed-secs|300|", lines[1])
        finally:
            e.close()


class RailsTests(unittest.TestCase):
    """The real dispatch rails in a scratch workspace: a shell job START
    batches with a sibling dispatch START; END renders the sibling block."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bgrails-")
        ws = Path(self.tmp) / "ws"
        for d in ("routines/lib", "config", "bridge", ".telegram_bot"):
            (ws / d).mkdir(parents=True)
        shutil.copy(ROUTINES / "dispatch-detached.sh", ws / "routines")
        # model_display.py ships beside its TSV in the real tree; without it
        # the label degrades to the raw alias (DGN-1227 fail-open).
        for f in ("dispatch_storage.py", "conf_reader.py", "agentlib.sh", "dispatch-model-display.tsv",
                  "dispatch-tier-bindings.tsv", "model_display.py",
                  "run_block.py", "i18n_lookup.py"):
            shutil.copy(ROUTINES / "lib" / f, ws / "routines/lib")
        shutil.copytree(ROUTINES.parent / "config/i18n", ws / "config/i18n")
        shutil.copy(REPO / "bridge/machine_gate.py", ws / "bridge")
        (ws / "config/agent.conf").write_text("AGENT_LANG=ko\n")
        self.push_log = Path(self.tmp) / "push.log"
        push = ws / "routines/push.sh"
        push.write_text('#!/bin/bash\nwhile [[ $# -gt 0 ]]; do case "$1" in '
                        '--text) T="$2"; shift 2;; *) shift;; esac; done\n'
                        'printf "%s\\n===MSG-END===\\n" "$T" >> "$PUSH_LOG"\n')
        push.chmod(0o755)
        self.script = str(ws / "routines/dispatch-detached.sh")
        self.env = dict(os.environ, PUSH_LOG=str(self.push_log),
                        DOGANY_START_NOTICE_WINDOW="2")
        self.env["AGENT_LANG"] = "ko"
        self.env["DOGANY_RUN_BLOCK_HOST"] = "Fake Host"
        self.env.pop("DOGANY_SIBLING_TERM", None)
        self.env.pop("DOGANY_LIVE_LABEL", None)
        self.conf = ws / "config/agent.conf"

    def tearDown(self):
        subprocess.run(["pkill", "-f", self.tmp], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_rail(self, *args):
        return subprocess.run([self.script] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=30)

    def messages(self):
        if not self.push_log.exists():
            return []
        return [m.strip("\n") for m in
                self.push_log.read_text().split("===MSG-END===") if m.strip()]

    def test_start_batches_with_sibling(self):
        self.run_rail("--start-notice-enqueue", "--display-name", "전체 테스트 실행",
                      "--model-slot", "Shell")
        subprocess.run(["bash", "-c", 'source "$0"; MODEL=opus; '
                        'start_notice_arm "서브에이전트 작업"', self.script],
                       env=dict(self.env, DISPATCH_TEST="1"),
                       capture_output=True, timeout=30)
        for _ in range(80):
            if self.messages():
                break
            time.sleep(0.1)
        msgs = self.messages()
        self.assertEqual(len(msgs), 1, msgs)
        lines = msgs[0].splitlines()
        # DGN-1767: a mixed burst titles with the background term.
        self.assertEqual(lines[0], "▶️ 백그라운드 작업 시작 (2건)")
        # DGN-1727: a background job line is the description alone.
        self.assertEqual(lines[1], "<blockquote>전체 테스트 실행")
        self.assertRegex(lines[2], r"^\u2022 스크립트, \d\d:\d\d</blockquote>$")
        # DGN-2373: a dispatch row is the same confirmed run_block quote.
        self.assertEqual(lines[3], "<blockquote>서브에이전트 작업")
        self.assertRegex(lines[4], r"^\u2022 서브에이전트: Claude Opus 5.5, \d\d:\d\d</blockquote>$")

    def wait_messages(self):
        for _ in range(80):
            if self.messages():
                break
            time.sleep(0.1)
        return self.messages()

    def test_start_job_only(self):
        # DGN-1767: a lone shell job's START is not a sibling's work.
        self.run_rail("--start-notice-enqueue", "--display-name", "전체 테스트 실행",
                      "--model-slot", "Shell")
        msgs = self.wait_messages()
        self.assertEqual(len(msgs), 1, msgs)
        lines = msgs[0].splitlines()
        self.assertEqual(lines[0], "▶️ 백그라운드 작업 시작")
        self.assertEqual(lines[1], "<blockquote>전체 테스트 실행")
        self.assertRegex(lines[2], r"^\u2022 스크립트, \d\d:\d\d</blockquote>$")
        self.assertEqual(len(lines), 3)

    def test_start_dispatch_only(self):
        subprocess.run(["bash", "-c", 'source "$0"; MODEL=opus; '
                        'start_notice_arm "서브에이전트 작업"', self.script],
                       env=dict(self.env, DISPATCH_TEST="1"),
                       capture_output=True, timeout=30)
        # The flusher the sourced arm spawned inherits DISPATCH_TEST and
        # never runs main, so flush the queue through the real mode.
        self.run_rail("--start-notice-flush")
        msgs = self.wait_messages()
        self.assertEqual(len(msgs), 1, msgs)
        lines = msgs[0].splitlines()
        self.assertEqual(lines[0], "▶️ 백그라운드 작업 시작")
        self.assertEqual(lines[1], "<blockquote>서브에이전트 작업")
        self.assertRegex(lines[2], r"^\u2022 서브에이전트: Claude Opus 5.5, \d\d:\d\d</blockquote>$")

    def test_script_dispatch_retains_no_model_notices(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$0"; RUNTIME=script; MODEL=""; '
             'start_notice_arm "script task"', self.script],
            env=dict(self.env, DISPATCH_TEST="1"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.run_rail("--start-notice-flush")
        messages = self.wait_messages()
        self.assertEqual(len(messages), 1, messages)
        self.assertRegex(messages[0], r"<blockquote>script task\n• 스크립트, \d\d:\d\d</blockquote>$")
        result = subprocess.run(
            ["bash", "-c", 'source "$0"; RUNTIME=script; MODEL=""; '
             'end_notice_text completed "script task" 14:05 330', self.script],
            env=dict(self.env, DISPATCH_TEST="1"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "✅ 백그라운드 작업 완료\n"
                         "<blockquote>script task\n• 스크립트, 14:05 (5분)</blockquote>")

    def test_start_conf_term_override(self):
        self.conf.write_text('AGENT_LANG=ko\nDASHBOARD_LIVE_LABEL="내 작업 중"\n'
                             'DASHBOARD_SIBLING_TERM=서브에이전트\n')
        self.run_rail("--start-notice-enqueue", "--display-name", "전체 테스트 실행",
                      "--model-slot", "Shell")
        msgs = self.wait_messages()
        self.assertEqual(len(msgs), 1, msgs)
        self.assertEqual(msgs[0].splitlines()[0], "▶️ 내 작업 시작")

    def test_end_shapes(self):
        self.run_rail("--end-notice", "--display-name", "전체 테스트 실행",
                      "--model-slot", "Shell", "--result", "completed",
                      "--elapsed-secs", "330", "--end-clock", "14:05")
        self.run_rail("--end-notice", "--display-name", "푸시", "--model-slot",
                      "Shell", "--result", "failed")
        self.run_rail("--end-notice", "--display-name", "로그 감시",
                      "--model-slot", "Monitor", "--result", "killed")
        self.assertEqual(self.messages(), [
            "✅ 백그라운드 작업 완료\n<blockquote>전체 테스트 실행\n\u2022 스크립트, 14:05 (5분)</blockquote>",
            "⚠️ 작업실패: 푸시 - 원인 확인하고 다시 알려드릴게요.",
            "⚠️ 로그 감시 작업이 멈췄습니다.",
        ])

    def test_dispatch_end_title_is_background_term(self):
        # DGN-1769 (owner 2026-09-28 07:08): a dispatch END titles like its
        # START -- the background term, never the sibling term beside it.
        for conf, want in (("AGENT_LANG=ko\nDASHBOARD_SIBLING_TERM=서브에이전트\n",
                            "✅ 백그라운드 작업 완료"),
                           ('AGENT_LANG=ko\nDASHBOARD_LIVE_LABEL="내 작업 중"\n'
                            "DASHBOARD_SIBLING_TERM=서브에이전트\n", "✅ 내 작업 완료")):
            self.conf.write_text(conf)
            out = subprocess.run(
                ["bash", "-c", 'source "$0"; MODEL=opus; '
                 'end_notice_text completed "서브에이전트 작업" 14:05 330', self.script],
                env=dict(self.env, DISPATCH_TEST="1"),
                capture_output=True, text=True, timeout=30).stdout
            self.assertEqual(out.splitlines()[0], want, out)
            self.assertRegex(out.splitlines()[1], r"^• .*: 서브에이전트 작업 14:05 \(5분\)$")

    # DGN-2229 rendered fixtures: one case per (runtime, billing, device).
    # (label, runtime, billing, host, kind, expected tag text in the line)
    CASES = [
        ("local subscription", "claude-code", "", "", "subagent", ""),
        ("claude cloud subscription", "cloud", "", "", "subagent", " (Cloud)"),
        ("claude cloud credit", "cloud", "credit", "", "subagent", " (Cloud credit)"),
        ("codex cloud", "codex-cloud", "", "", "subagent", " (Cloud)"),
        ("local api credit", "claude-api", "", "", "subagent", " (API credit)"),
        ("other device", "codex", "", "Other Host", "subagent", " (Other Host)"),
        ("script on this device", "", "", "", "script", None),
    ]

    def block_env(self, runtime, billing, host):
        return dict(self.env, DISPATCH_TEST="1", RUN_BILLING=billing, RUN_HOST=host)

    def test_block_fixtures_workbench_and_dispatch_end(self):
        shown = []
        for label, rt, billing, host, kind, tag in self.CASES:
            env = self.block_env(rt, billing, host)
            if kind == "script":
                start = ('source "$0"; run_block script "" "" "title" 14:51')
                end = ('source "$0"; MODEL_SLOT=Shell; end_notice_text completed '
                       '"title" 14:57 360')
                want = "\u2022 스크립트, 14:51"
            else:
                start = ('source "$0"; run_block subagent "Claude Sonnet 5.5" %s "title" 14:51' % rt)
                end = ('source "$0"; RUNTIME=%s; MODEL=sonnet; MODEL_OBSERVED_JSON=\'["claude-sonnet-5-5"]\'; '
                       'end_notice_text completed "title" 14:57 360' % rt)
                want = "\u2022 서브에이전트: Claude Sonnet 5.5%s, 14:51" % tag
            got_s = subprocess.run(["bash", "-c", start, self.script], env=env,
                                   capture_output=True, text=True, timeout=30).stdout
            got_e = subprocess.run(["bash", "-c", end, self.script], env=env,
                                   capture_output=True, text=True, timeout=30).stdout
            self.assertEqual(got_s, "<blockquote>title\n%s</blockquote>" % want, label)
            self.assertEqual(got_e.splitlines()[0], "✅ 백그라운드 작업 완료", label)
            if kind == "script":
                expected = "<blockquote>title\n%s</blockquote>" % want.replace("14:51", "14:57 (6분)")
            else:
                display = "Codex" if rt == "codex-cloud" else "Claude Sonnet 5.5"
                suffix = {"cloud": " (Cloud)", "codex-cloud": " (Cloud)",
                          "claude-api": " (API)"}.get(rt, "")
                expected = "• %s%s: title 14:57 (6분)" % (display, suffix)
            self.assertEqual(got_e.split("\n", 1)[1], expected, label)
            shown.append("[%s: workbench block / dispatch END]\n%s\n%s" % (label, got_s, got_e))
        print("\n\n".join(shown))

    def test_block_renderer_failure_preserves_model_and_escaped_shape(self):
        renderer = Path(self.script).parent / "lib/run_block.py"
        for state in ("absent", "raises", "empty"):
            with self.subTest(state=state):
                if renderer.exists():
                    renderer.unlink()
                if state != "absent":
                    renderer.write_text("raise RuntimeError('broken renderer')\n"
                                        if state == "raises" else "")
                result = subprocess.run(
                    ["bash", "-c", 'source "$0"; run_block subagent "opus" '
                     'claude-code "title <&>" 14:57 "6분"', self.script],
                    env=dict(self.env, DISPATCH_TEST="1"),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '<blockquote>title &lt;&amp;&gt;\n'
                                 '• subagent: opus, 14:57 (6분)</blockquote>')

    def test_block_batched_two_jobs(self):
        self.run_rail("--start-notice-enqueue", "--display-name", "job one",
                      "--model-slot", "Shell")
        subprocess.run(["bash", "-c", 'source "$0"; MODEL=opus; RUNTIME=codex-cloud; '
                        'start_notice_arm "job two"', self.script],
                       env=dict(self.env, DISPATCH_TEST="1"),
                       capture_output=True, timeout=30)
        msgs = self.wait_messages()
        self.assertEqual(len(msgs), 1, msgs)
        # DGN-2373: every job (script or dispatch) is its own run_block quote.
        self.assertEqual(msgs[0].count("<blockquote>"), 2)
        self.assertEqual(msgs[0].count("</blockquote>"), 2)
        self.assertIn("<blockquote>job two\n", msgs[0])
        self.assertRegex(msgs[0], r"• 서브에이전트: Codex \(Cloud\), \d\d:\d\d</blockquote>")
        print("[batched, two jobs]\n" + msgs[0])

    def test_bad_args_refused_without_push(self):
        r = self.run_rail("--end-notice", "--display-name", "a\tb",
                          "--model-slot", "Shell", "--result", "completed")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.messages(), [])



class ModelSlotTests(unittest.TestCase):
    """Owner ruling 2026-09-24: the slot is the CLI the job runs."""
    def test_cli_name(self):
        m = bg.model_slot
        self.assertEqual(m("Bash", {"command": "cd ~/x && git push -q origin dev"}), "git")
        self.assertEqual(m("Bash", {"command": "env -u X python3 t.py"}), "t.py")
        self.assertEqual(m("Bash", {"command": "FOO=1 sudo -n du -sh x"}), "du")
        self.assertEqual(m("Bash", {"command":
                           "{ for t in one two; do python3 run.py $t; done; } > log"}),
                         "run.py")
        self.assertEqual(m("Bash", {"command": "( python3 run.py )"}), "run.py")
        self.assertEqual(m("Bash", {"command": "time bash x.sh"}), "x.sh")
        self.assertEqual(m("Bash", {"command": "! grep needle haystack"}), "grep")
        self.assertEqual(m("Bash", {"command":
                           "for; while; until; if; then; else; elif; do; done; fi; "
                           "case; esac; select; function; time; !; {; }"}), "Shell")
        self.assertEqual(m("Bash", {"command": ""}), "Shell")
        self.assertEqual(m("Monitor", {"command": "tail -f x"}), "Monitor")

    def test_interpreter_hands_slot_to_script(self):
        # DGN-1769: the live dev.60 release job read "bash".
        m = bg.model_slot
        self.assertEqual(m("Bash", {"command":
                           "cd /Users/x/dogany-agent && bash scripts/release.sh "
                           "--channel dev 2>&1 | tail -40"}), "release.sh")
        self.assertEqual(m("Bash", {"command": "sh -e ./run.sh"}), "run.sh")
        self.assertEqual(m("Bash", {"command": "zsh -x ~/bin/sync.zsh a"}), "sync.zsh")
        self.assertEqual(m("Bash", {"command": "python3 -u tools/scan.py x"}), "scan.py")
        self.assertEqual(m("Bash", {"command": "python3.12 a/b.py"}), "b.py")
        # Inline code or no operand keeps the interpreter.
        self.assertEqual(m("Bash", {"command": "bash -c 'sleep 5'"}), "bash")
        self.assertEqual(m("Bash", {"command": "python3 -m pytest tests"}), "python3")
        self.assertEqual(m("Bash", {"command": "python3 -c 'print(1)'"}), "python3")
        self.assertEqual(m("Bash", {"command": "bash"}), "bash")
        # Not an interpreter: unchanged.
        self.assertEqual(m("Bash", {"command": "node server.js"}), "node")
        self.assertEqual(m("Workflow", None), "Workflow")

    def test_leading_assignments_and_wrappers(self):
        # DGN-1820: S=$(date +%s) read as the slot "+%s)".
        m = bg.model_slot
        self.assertEqual(m("Bash", {"command": "S=$(date +%s); bash x.sh"}), "x.sh")
        self.assertEqual(m("Bash", {"command":
                           "S=$(date +%s)\nbash scripts/x.sh > log 2>&1"}), "x.sh")
        self.assertEqual(m("Bash", {"command": "X=$(ls | wc -l); make all"}), "make")
        self.assertEqual(m("Bash", {"command": "T=`date`; git status"}), "git")
        self.assertEqual(m("Bash", {"command": "N=$((1+2)) ./run.sh"}), "run.sh")
        self.assertEqual(m("Bash", {"command":
                           "FOO=\"$(a; b)\" env -u Y node s.js"}), "node")
        self.assertEqual(m("Bash", {"command":
                           "A=1; (cd d && python3 r.py)"}), "r.py")
        self.assertEqual(m("Bash", {"command": "cd d && (make test)"}), "make")
        self.assertEqual(m("Bash", {"command": "export P=$(pwd); cargo build"}), "cargo")
        # Nothing command-shaped: the tool fallback, never residue.
        self.assertEqual(m("Bash", {"command": "S=$(date +%s); $CMD go"}), "Shell")
        self.assertEqual(m("Bash", {"command": "S=$(date +%s)"}), "Shell")
        self.assertEqual(m("Bash", {"command": "S=$(date +%s"}), "Shell")

class MonitorLifecycleTests(unittest.TestCase):
    """DGN-1938: a Monitor watch row closes on expiry, TaskStop and exit."""
    T0 = 1_000_000.0
    TID = "mon_1"

    def setUp(self):
        self.e = Env("ko")
        self.tmp = tempfile.mkdtemp(prefix="mon-")

    def tearDown(self):
        self.e.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stamp(self, off):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.T0 + off))

    def transcript(self, timeout_ms=60000, stop=None, persistent=False):
        inp = {"description": "로그 감시", "timeout_ms": timeout_ms}
        if persistent:
            inp["persistent"] = True
        entries = [
            {"type": "assistant", "timestamp": self.stamp(0), "message": {
                "content": [{"type": "tool_use", "id": "c1", "name": "Monitor",
                             "input": inp}]}},
            {"type": "user", "timestamp": self.stamp(1), "message": {
                "content": [{"type": "tool_result", "tool_use_id": "c1",
                             "content": "Monitor started (task %s, timeout "
                                        "%dms). You will be notified."
                                        % (self.TID, timeout_ms)}]}}]
        if stop:
            entries += [
                {"type": "assistant", "timestamp": self.stamp(10), "message": {
                    "content": [{"type": "tool_use", "id": "c2",
                                 "name": "TaskStop",
                                 "input": {"task_id": self.TID}}]}},
                {"type": "user", "timestamp": self.stamp(11), "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "c2",
                                 "content": "Successfully stopped task: %s"
                                            % self.TID}]}}]
        path = os.path.join(self.tmp, "session.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in entries))
        return path

    def collect(self, path, at):
        out = {}
        with patch.object(sf.time, "time", return_value=self.T0 + at):
            rows = sf._collect_active_subagents(path, background_out=out)
        return rows, out

    def started(self):
        bg.record_start(self.TID, "Monitor", "로그 감시", self.T0,
                        now=self.T0, sender=self.e.sender,
                        expires=self.T0 + 60)
        self.e.sent.clear()

    def ends(self):
        return [a for a in self.e.sent if a[0] == "--end-notice"]

    def test_expiry_closes_row_and_sends_end_once(self):
        self.started()
        path = self.transcript()
        rows, out = self.collect(path, 30)       # inside the timeout
        self.assertEqual(len(rows), 1)
        self.assertEqual(out["closed"], {})
        rows, out = self.collect(path, 61)       # past it, no notice written
        self.assertEqual(rows, [])
        self.assertEqual(out["closed"][self.TID][2], "expired")
        for _ in range(2):                        # Stop-hook rescans
            bg.sync_from_scan(out["open"], out["closed"], 1200,
                              now=self.T0 + 61, sender=self.e.sender)
        ends = self.ends()
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][ends[0].index("--result") + 1], "completed")
        self.assertEqual(bg.started_open_ids(), set())

    def test_persistent_monitor_never_expires(self):
        rows, out = self.collect(self.transcript(persistent=True), 900)
        self.assertEqual(len(rows), 1)
        self.assertEqual(out["closed"], {})

    def test_sweep_closes_expired_without_a_transcript(self):
        self.started()
        self.assertEqual(bg.sweep_liveness(now=self.T0 + 70, sender=self.e.sender), {})
        out = bg.sweep_liveness(now=self.T0 + 61 + bg.EXPIRY_GRACE_SECS,
                                sender=self.e.sender)
        self.assertEqual(out, {self.TID: "sent"})
        self.assertEqual(len(self.ends()), 1)
        self.assertEqual(bg.sweep_liveness(now=self.T0 + 500,
                                           sender=self.e.sender), {})

    def test_taskstop_closes_row_quietly(self):
        self.started()
        path = self.transcript(stop=True)
        rows, out = self.collect(path, 20)
        self.assertEqual(rows, [])
        self.assertEqual(out["closed"][self.TID][:1], ("killed",))
        # The PostToolUse hook was missed (no mark_self_stopped): the scan
        # alone must keep the END quiet.
        bg.sync_from_scan(out["open"], out["closed"], 1200,
                          now=self.T0 + 20, sender=self.e.sender)
        self.assertEqual(self.ends(), [])
        self.assertEqual(bg.started_open_ids(), set())

    def test_failed_taskstop_leaves_row_open(self):
        path = self.transcript(stop=True)
        with open(path) as fh:
            text = fh.read().replace("Successfully stopped", "No such task:")
        with open(path, "w") as fh:
            fh.write(text)
        rows, out = self.collect(path, 20)
        self.assertEqual(len(rows), 1)

    def test_process_gone_closes_quietly_and_never_signals(self):
        bg.record_start(self.TID, "Monitor", "로그 감시", self.T0, now=self.T0,
                        sender=self.e.sender, pid=424242)
        self.e.sent.clear()
        alive = {"v": True}
        self.assertEqual(bg.sweep_liveness(now=self.T0 + 5, sender=self.e.sender,
                                           alive=lambda p: alive["v"]), {})
        alive["v"] = False
        self.assertEqual(bg.sweep_liveness(now=self.T0 + 6, sender=self.e.sender,
                                           alive=lambda p: alive["v"]),
                         {self.TID: "quiet"})
        self.assertEqual(self.ends(), [])
        self.assertEqual(bg.started_open_ids(), set())
        with patch.object(os, "kill", side_effect=ProcessLookupError) as k:
            self.assertFalse(bg.pid_alive(424242))
            k.assert_called_once_with(424242, 0)   # probe only, never a kill

    def test_hook_records_expiry_and_pid(self):
        payload = {"tool_name": "Monitor", "transcript_path": "/x/%s.jsonl" % SESSION,
                   "tool_input": {"description": "로그 감시", "timeout_ms": 90000},
                   "tool_response": {"taskId": self.TID, "pid": 31337}}
        self.e.hook("--launched", payload, {"DOGANY_BG_NOTICE_DISPATCH": "/bin/true"})
        with open(bg.state_path()) as fh:
            rec = json.load(fh)[self.TID]
        self.assertEqual(rec["pid"], 31337)
        self.assertAlmostEqual(rec["expires"] - rec["start"], 90.0, delta=2)


if __name__ == "__main__":
    unittest.main()
