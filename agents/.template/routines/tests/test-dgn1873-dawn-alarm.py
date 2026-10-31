#!/usr/bin/env python3
# test-lane: fast
"""DGN-1873 dec-252: the dawn failure alarm's reason classifier (L2) and
next-fire retry time (L3) -- routines/lib/dawn_alarm.py.

  R. reason classifier: each of the four classes, the no-class fallback, the
     dec-252 priority order, and "this run's own log bytes" (offset reads).
  H. host-off: a boot/wake between the last scheduled fire and the run start.
  N. next fire: StartCalendarInterval daily / weekday / list / day-of-month,
     StartInterval, the dmq-* step -> dawn-queue plist, no plist -> None.
  W. rendering: day word, period word, minutes, en, and the L3 sentence.

Run: python3 routines/tests/test-dgn1873-dawn-alarm.py
Exit: 0 all pass, 1 any fail.
"""

import datetime as dt
import importlib.util
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib", "dawn_alarm.py")
spec = importlib.util.spec_from_file_location("dawn_alarm", LIB)
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)

PASS = FAIL = 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS: %s" % desc)
    else:
        FAIL += 1
        print("  FAIL: %s%s" % (desc, (" -- " + str(detail)) if detail else ""))


def eq(desc, got, want):
    check(desc, got == want, "got=%r want=%r" % (got, want))


def ep(y, mo, d, h, mi=0):
    return int(dt.datetime(y, mo, d, h, mi).timestamp())


TMP = tempfile.mkdtemp(prefix="dgn1873-dawn-alarm.")
AGENTS = os.path.join(TMP, "LaunchAgents")
os.makedirs(AGENTS)
os.environ["DOGANY_LAUNCHAGENTS_DIR"] = AGENTS
os.environ.pop("XPC_SERVICE_NAME", None)


def write_plist(label, **keys):
    with open(os.path.join(AGENTS, label + ".plist"), "wb") as fh:
        plistlib.dump(dict(Label=label, **keys), fh)


# ---------------------------------------------------------------------------
print("--- R: reason classifier (dec-252 five lines) ---")
eq("sqlite 'database is locked' -> line 1",
   da.classify(1, "sqlite3.OperationalError: database is locked"), 1)
eq("memory.py rc=3 'database busy' note -> line 1",
   da.classify(1, "[classify-inbox] ... not refreshed (database busy) -- reindex deferred"), 1)
eq("usage-limit text -> line 2", da.classify(1, "Claude AI usage limit reached|1759800000"), 2)
eq("bare exit 75 (usage-defer fell through) -> line 2", da.classify(75, ""), 2)
eq("fresh usage-defer marker -> line 2", da.classify(1, "", usage_marker_fresh=True), 2)
eq("exit 124 (timeout kill) -> line 3", da.classify(124, ""), 3)
eq("'timed out' in the log -> line 3", da.classify(1, "subprocess.TimeoutExpired: timed out after 900s"), 3)
eq("host booted/woke after the fire -> line 4", da.classify(1, "", host_off=True), 4)
eq("nothing matches -> line 5 (fallback)", da.classify(1, "Traceback: KeyError 'x'"), 5)
eq("empty evidence -> line 5", da.classify(2), 5)
eq("priority: lock beats usage/timeout/host-off",
   da.classify(124, "database is locked; usage limit", True, True), 1)
eq("priority: usage beats timeout", da.classify(124, "rate limit hit"), 2)
eq("priority: timeout beats host-off", da.classify(124, "", host_off=True), 3)
check("'busy_timeout' alone is not a timeout", da.classify(1, "PRAGMA busy_timeout=30000") == 5)
eq("line 1 verbatim", da.reason_line(1), "기억 저장소를 다른 작업이 쓰고 있어서 끝까지 정리하지 못했어요.")
eq("line 2 verbatim", da.reason_line(2), "사용 한도에 걸려서 멈췄어요.")
eq("line 3 verbatim", da.reason_line(3), "정해진 시간 안에 끝나지 않았어요.")
eq("line 4 verbatim", da.reason_line(4), "새벽에 Mac이 꺼져 있어서 돌지 못했어요.")
eq("line 5 verbatim", da.reason_line(5), "원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요.")
eq("unknown key -> line 5", da.reason_line(99), da.reason_line(5))

log = os.path.join(TMP, "shared.log")
with open(log, "w") as fh:
    fh.write("old step: database is locked\n")
off = os.path.getsize(log)
with open(log, "a") as fh:
    fh.write("this run: plain failure\n")
eq("offset read returns only this run's bytes", da.read_run_log("%s@%d" % (log, off)),
   "this run: plain failure\n")
eq("a log that shrank (rotated) is read whole", da.read_run_log("%s@%d" % (log, 10 ** 6)),
   open(log).read())
eq("missing log -> empty", da.read_run_log(os.path.join(TMP, "nope.log") + "@0"), "")

# CLI end to end (the call cron-guard makes).
cli = [sys.executable, LIB, "reason", "--rc", "1", "--key", "--log", "%s@%d" % (log, off)]
eq("CLI: old lock line outside the offset -> 5",
   subprocess.run(cli, capture_output=True, text=True).stdout.strip(), "5")
with open(log, "a") as fh:
    fh.write("sqlite3.OperationalError: database is locked\n")
eq("CLI: this run's lock line -> 1",
   subprocess.run(cli, capture_output=True, text=True).stdout.strip(), "1")
marker = os.path.join(TMP, "usage.json")
open(marker, "w").close()
mt = int(os.path.getmtime(marker))
eq("CLI: usage marker written this run -> 2",
   subprocess.run([sys.executable, LIB, "reason", "--rc", "1", "--key", "--usage-marker", marker,
                   "--run-start", str(mt)], capture_output=True, text=True).stdout.strip(), "2")
eq("CLI: usage marker older than the run -> 5",
   subprocess.run([sys.executable, LIB, "reason", "--rc", "1", "--key", "--usage-marker", marker,
                   "--run-start", str(mt + 60)], capture_output=True, text=True).stdout.strip(), "5")

# ---------------------------------------------------------------------------
print("\n--- H: host-off (boot/wake between the last fire and the run) ---")
write_plist("com.t.ag.consolidate-0430", StartCalendarInterval={"Hour": 4, "Minute": 30})
run = ep(2026, 10, 6, 7, 10)          # launchd fired the missed 04:30 run at 07:10
os.environ["DOGANY_BOOT_EPOCH"] = str(ep(2026, 9, 1, 12))
os.environ["DOGANY_WAKE_EPOCH"] = str(ep(2026, 10, 6, 7, 9))
check("woke at 07:09 after the 04:30 fire -> host off", da.host_was_off("com.t.ag.consolidate-0430", run))
os.environ["DOGANY_WAKE_EPOCH"] = str(ep(2026, 10, 6, 3, 0))
check("woke before the fire -> not host off", not da.host_was_off("com.t.ag.consolidate-0430", run))
os.environ["DOGANY_WAKE_EPOCH"] = "0"
os.environ["DOGANY_BOOT_EPOCH"] = str(ep(2026, 10, 6, 7, 0))
check("booted at 07:00 after the fire -> host off", da.host_was_off("com.t.ag.consolidate-0430", run))
check("no plist -> never host off", not da.host_was_off("com.t.ag.unknown", run))
eq("CLI: host off -> 4",
   subprocess.run([sys.executable, LIB, "reason", "--rc", "1", "--key", "--label",
                   "com.t.ag.consolidate-0430", "--run-start", str(run)],
                  capture_output=True, text=True).stdout.strip(), "4")
os.environ["DOGANY_BOOT_EPOCH"] = "1"
os.environ["DOGANY_WAKE_EPOCH"] = "0"

# ---------------------------------------------------------------------------
print("\n--- N: next scheduled fire ---")
now = ep(2026, 10, 6, 4, 35)          # Tuesday
daily4 = [{"Hour": 4, "Minute": 0}]
eq("daily 04:00 at 04:35 -> tomorrow 04:00", da.next_fire(daily4, now), ep(2026, 10, 7, 4))
eq("daily 05:00 at 04:35 -> today 05:00", da.next_fire([{"Hour": 5, "Minute": 0}], now),
   ep(2026, 10, 6, 5))
eq("exactly at the fire -> the next one (strictly after)", da.next_fire(daily4, ep(2026, 10, 6, 4)),
   ep(2026, 10, 7, 4))
eq("weekday 0 (Sunday) -> Sun Oct 11", da.next_fire([{"Weekday": 0, "Hour": 3, "Minute": 17}], now),
   ep(2026, 10, 11, 3, 17))
eq("weekday 7 is Sunday too", da.next_fire([{"Weekday": 7, "Hour": 3, "Minute": 17}], now),
   ep(2026, 10, 11, 3, 17))
eq("list -> the earliest entry", da.next_fire([{"Hour": 21, "Minute": 0}, {"Hour": 7, "Minute": 30}], now),
   ep(2026, 10, 6, 7, 30))
eq("day of month 1 -> Nov 1", da.next_fire([{"Day": 1, "Hour": 4, "Minute": 0}], now), ep(2026, 11, 1, 4))
eq("Minute only -> the next hour's :15", da.next_fire([{"Minute": 15}], now), ep(2026, 10, 6, 5, 15))
eq("StartInterval -> now + interval", da.next_fire({"interval": 3600}, now), now + 3600)
eq("no schedule -> None", da.next_fire(None, now), None)
eq("prev fire of daily 04:00 at 04:35 -> today 04:00", da.prev_fire(daily4, now), ep(2026, 10, 6, 4))

write_plist("com.t.ag.classify-inbox-0500", StartCalendarInterval={"Hour": 5, "Minute": 0})
eq("own plist read", da.load_schedule("com.t.ag.classify-inbox-0500"), [{"Hour": 5, "Minute": 0}])
eq("dmq step, no queue plist -> None", da.load_schedule("com.t.ag.dmq-classify"), None)
write_plist("com.t.host.dawn-memory-queue", StartCalendarInterval={"Hour": 4, "Minute": 0})
eq("dmq step -> the dawn queue's plist (glob)", da.load_schedule("com.t.ag.dmq-classify"), daily4)
write_plist("com.t.other.dawn-memory-queue", StartCalendarInterval={"Hour": 3, "Minute": 45})
os.environ["XPC_SERVICE_NAME"] = "com.t.other.dawn-memory-queue"
eq("dmq step -> the queue launchd started us from (XPC_SERVICE_NAME) first",
   da.load_schedule("com.t.ag.dmq-consolidate"), [{"Hour": 3, "Minute": 45}])
os.environ.pop("XPC_SERVICE_NAME")
eq("a non-dmq label never borrows the queue's schedule", da.load_schedule("com.t.ag.no-plist"), None)
write_plist("com.t.ag.poll", StartInterval=900)
eq("StartInterval plist", da.load_schedule("com.t.ag.poll"), {"interval": 900})

# ---------------------------------------------------------------------------
print("\n--- W: rendering ---")
eq("tomorrow dawn 4", da.when_text(ep(2026, 10, 7, 4), now), "내일 새벽 4시쯤")
eq("today dawn 5", da.when_text(ep(2026, 10, 6, 5), now), "오늘 새벽 5시쯤")
eq("day after tomorrow", da.when_text(ep(2026, 10, 8, 4), now), "모레 새벽 4시쯤")
eq("further out -> month/day", da.when_text(ep(2026, 10, 11, 3, 17), now), "10월 11일 새벽 3시 17분쯤")
eq("morning", da.when_text(ep(2026, 10, 6, 7, 30), now), "오늘 아침 7시 30분쯤")
eq("forenoon", da.when_text(ep(2026, 10, 6, 10), now), "오늘 오전 10시쯤")
eq("afternoon 12h clock", da.when_text(ep(2026, 10, 6, 15), now), "오늘 오후 3시쯤")
eq("evening", da.when_text(ep(2026, 10, 6, 19), now), "오늘 저녁 7시쯤")
eq("night", da.when_text(ep(2026, 10, 6, 22), now), "오늘 밤 10시쯤")
eq("midnight hour reads 12", da.when_text(ep(2026, 10, 7, 0, 30), now), "내일 새벽 12시 30분쯤")
eq("en", da.when_text(ep(2026, 10, 7, 4), now, "en"), "tomorrow around 4 AM")
eq("en minutes + PM", da.when_text(ep(2026, 10, 6, 15, 5), now, "en"), "today around 3:05 PM")
eq("L3 for a dmq step at 04:35 (the dec-252 sentence)",
   da.retry_line("com.t.ag.dmq-classify", now), "내일 새벽 4시쯤 다시 시도할게요.")
eq("L3 from the job's own plist (failed 05:20)",
   da.retry_line("com.t.ag.classify-inbox-0500", ep(2026, 10, 6, 5, 20)),
   "내일 새벽 5시쯤 다시 시도할게요.")
eq("L3 without a readable schedule -> the no-time draft",
   da.retry_line("com.t.ag.no-plist", now), "다음 정기 실행 때 다시 시도할게요.")
os.environ["DOGANY_NOW_EPOCH"] = str(now)
eq("CLI when --lang en",
   subprocess.run([sys.executable, LIB, "when", "--label", "com.t.ag.dmq-classify", "--lang", "en"],
                  capture_output=True, text=True).stdout.strip(), "tomorrow around 4 AM")
eq("CLI when, unknown schedule (ko)",
   subprocess.run([sys.executable, LIB, "when", "--label", "com.t.ag.none", "--lang", "ko"],
                  capture_output=True, text=True).stdout.strip(), "다음 정기 실행 때")

shutil.rmtree(TMP, ignore_errors=True)
print("\n=== %d passed, %d failed ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
