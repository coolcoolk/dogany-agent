#!/usr/bin/env python3
"""Dawn (unattended) failure alarm -- reason line + next-try line (DGN-1873).

cron-guard.sh --unattended renders its owner alarm from this file:

    L1  the dawn headline naming agent + job       (cron-guard.sh)
    L2  one machine-classified reason line         (reason_line)
    L3  "<next fire> I will try again"             (retry_line)

L2 is classified from THIS run's own evidence only -- its exit code, the log
bytes it wrote, and host wake/boot times -- never guessed from the label.
L3's time is the job's next scheduled fire (the launchd StartCalendarInterval
of its own plist, or of the dawn queue's plist for a dmq-* step), never a
hardcoded hour.

CLI (cron-guard.sh):
    dawn_alarm.py reason --rc N --run-start EPOCH [--log FILE ...]
                         [--usage-marker FILE] [--label LABEL]
    dawn_alarm.py retry  --label LABEL [--lang ko|en]
    dawn_alarm.py when   --label LABEL [--lang ko|en]

Test seams (env): DOGANY_LAUNCHAGENTS_DIR (plist dir), DOGANY_NOW_EPOCH
(clock), DOGANY_BOOT_EPOCH / DOGANY_WAKE_EPOCH (host boot / last wake).
"""

import argparse
import datetime as _dt
import glob
import os
import plistlib
import re
import subprocess
import sys

# Owner-approved copy (2026-10-06, dec-252): the five reason lines, verbatim.
# The last one is used only when no class matches. Do not re-word without a
# fresh owner pass.
REASON_LINES = {
    1: "기억 저장소를 다른 작업이 쓰고 있어서 끝까지 정리하지 못했어요.",
    2: "사용 한도에 걸려서 멈췄어요.",
    3: "정해진 시간 안에 끝나지 않았어요.",
    4: "새벽에 Mac이 꺼져 있어서 돌지 못했어요.",
    5: "원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요.",
}
REASON_LOCKED, REASON_USAGE, REASON_TIMEOUT, REASON_HOST_OFF, REASON_UNKNOWN = 1, 2, 3, 4, 5

# Owner-approved copy (2026-10-06, dec-252): L3 = <when_text> + this frame;
# the time words come from the job's next fire, never a fixed hour.
RETRY_SUFFIX_KO = " 다시 시도할게요."

# Owner-approved wording (dec-255, 2026-10-06, DGN-1873): the next fire could
# not be read (no launchd plist -- e.g. a systemd timer host).
RETRY_UNKNOWN_KO = "다음 정기 실행 때 다시 시도할게요."
WHEN_UNKNOWN = {"ko": "다음 정기 실행 때", "en": "at the next scheduled run"}

# ---- reason classifier ------------------------------------------------------
_LOCKED_RE = re.compile(
    r"database is locked|database table is locked|database busy|SQLITE_BUSY", re.I)
_USAGE_RE = re.compile(
    r"usage[ _-]?limit|usage-defer|rate[ _-]?limit|hit your limit|limit reached"
    r"|사용량 한도|사용 한도", re.I)
_TIMEOUT_RE = re.compile(
    r"\btimed out\b|TimeoutExpired|TIMEOUT-KILL|Terminated: 15|Killed: 9", re.I)
RC_USAGE_DEFER = 75
RC_TIMEOUT = (124, 137)   # timeout(1)/gtimeout, and SIGKILL after its grace
LOG_READ_CAP = 256 * 1024


def classify(rc, log_text="", usage_marker_fresh=False, host_off=False):
    """Map one run's evidence to a REASON_LINES key, in dec-252 order:
    sqlite lock -> usage limit/defer -> timeout -> host off -> unknown."""
    text = log_text or ""
    if _LOCKED_RE.search(text):
        return REASON_LOCKED
    if rc == RC_USAGE_DEFER or usage_marker_fresh or _USAGE_RE.search(text):
        return REASON_USAGE
    if rc in RC_TIMEOUT or _TIMEOUT_RE.search(text):
        return REASON_TIMEOUT
    if host_off:
        return REASON_HOST_OFF
    return REASON_UNKNOWN


def reason_line(key):
    return REASON_LINES.get(key, REASON_LINES[REASON_UNKNOWN])


def read_run_log(spec):
    """spec = PATH or PATH@OFFSET: the bytes PATH gained after OFFSET (the
    size cron-guard saw before the run) -- this run's own output. A file that
    shrank (rotated) is read whole. Capped to the last LOG_READ_CAP bytes."""
    path, _, off = spec.rpartition("@") if "@" in spec else (spec, "", "0")
    try:
        off = int(off or 0)
    except ValueError:
        off = 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size < off:
                off = 0
            fh.seek(max(off, size - LOG_READ_CAP))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _sysctl_epoch(name):
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 0


def host_wake_epochs():
    """(boot, last wake) epochs; 0 = unknown / never slept."""
    boot = os.environ.get("DOGANY_BOOT_EPOCH")
    wake = os.environ.get("DOGANY_WAKE_EPOCH")
    return (int(boot) if boot else _sysctl_epoch("kern.boottime"),
            int(wake) if wake else _sysctl_epoch("kern.waketime"))


def host_was_off(label, run_start):
    """True when the host booted or woke after the job's last scheduled fire
    and before this run started: launchd fired the missed run on wake."""
    sched = load_schedule(label)
    if not sched:
        return False
    last = prev_fire(sched, run_start)
    if last is None:
        return False
    return any(last < t <= run_start for t in host_wake_epochs() if t)


# ---- schedule / next fire ---------------------------------------------------
def _agents_dir():
    return os.environ.get("DOGANY_LAUNCHAGENTS_DIR") or os.path.expanduser(
        "~/Library/LaunchAgents")


def plist_candidates(label):
    """The job's own plist; a dawn-queue step (<ns>.<slug>.dmq-*) has none and
    runs on the queue's schedule -- the plist launchd started us from
    (XPC_SERVICE_NAME), else the host's one *.dawn-memory-queue.plist."""
    d = _agents_dir()
    out = [os.path.join(d, label + ".plist")]
    if label.rsplit(".", 1)[-1].startswith("dmq-"):
        xpc = os.environ.get("XPC_SERVICE_NAME", "")
        if xpc.endswith(".dawn-memory-queue"):
            out.append(os.path.join(d, xpc + ".plist"))
        out.extend(sorted(glob.glob(os.path.join(d, "*.dawn-memory-queue.plist"))))
    return out


def load_schedule(label):
    """[{Minute/Hour/Day/Weekday/Month}...] from the first readable plist, or
    {"interval": N} for StartInterval; None when no schedule is readable."""
    for path in plist_candidates(label):
        try:
            with open(path, "rb") as fh:
                p = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        sci = p.get("StartCalendarInterval")
        if isinstance(sci, dict):
            return [sci]
        if isinstance(sci, list) and sci:
            return [x for x in sci if isinstance(x, dict)] or None
        if isinstance(p.get("StartInterval"), int) and p["StartInterval"] > 0:
            return {"interval": p["StartInterval"]}
    return None


def _matches_day(entry, day):
    if "Month" in entry and entry["Month"] != day.month:
        return False
    if "Day" in entry and entry["Day"] != day.day:
        return False
    if "Weekday" in entry:
        # launchd: 0 and 7 are Sunday; Python: Monday == 0.
        if entry["Weekday"] % 7 != (day.weekday() + 1) % 7:
            return False
    return True


def _fires_on(entry, day):
    if not _matches_day(entry, day):
        return []
    hours = [entry["Hour"]] if "Hour" in entry else range(24)
    minutes = [entry["Minute"]] if "Minute" in entry else range(60)
    return [_dt.datetime(day.year, day.month, day.day, h, m)
            for h in hours for m in minutes]


def next_fire(sched, now_epoch):
    """Epoch of the first scheduled fire strictly after now_epoch (local
    time), or None."""
    if not sched:
        return None
    if isinstance(sched, dict):
        return int(now_epoch) + int(sched["interval"])
    now = _dt.datetime.fromtimestamp(now_epoch)
    start = now.date()
    for d in range(0, 400):
        day = start + _dt.timedelta(days=d)
        best = None
        for entry in sched:
            for t in _fires_on(entry, day):
                if t > now and (best is None or t < best):
                    best = t
        if best is not None:
            return int(best.timestamp())
    return None


def prev_fire(sched, now_epoch):
    """Epoch of the last scheduled fire at or before now_epoch, or None."""
    if not sched or isinstance(sched, dict):
        return None
    now = _dt.datetime.fromtimestamp(now_epoch)
    start = now.date()
    for d in range(0, 400):
        day = start - _dt.timedelta(days=d)
        best = None
        for entry in sched:
            for t in _fires_on(entry, day):
                if t <= now and (best is None or t > best):
                    best = t
        if best is not None:
            return int(best.timestamp())
    return None


# ---- rendering ----------------------------------------------------------------
def _period_ko(h):
    if h < 6:
        return "새벽"
    if h < 9:
        return "아침"
    if h < 12:
        return "오전"
    if h < 18:
        return "오후"
    if h < 21:
        return "저녁"
    return "밤"


def when_text(fire_epoch, now_epoch, lang="ko"):
    """Day + period + clock words ("tomorrow around 4 AM" in en) for a fire
    time relative to now (local calendar days)."""
    t = _dt.datetime.fromtimestamp(fire_epoch)
    days = (t.date() - _dt.datetime.fromtimestamp(now_epoch).date()).days
    h12 = t.hour % 12 or 12
    if lang == "en":
        day = {0: "today", 1: "tomorrow"}.get(days, "on %s %d" % (t.strftime("%b"), t.day))
        clock = "%d:%02d" % (h12, t.minute) if t.minute else "%d" % h12
        return "%s around %s %s" % (day, clock, "AM" if t.hour < 12 else "PM")
    day = {0: "오늘", 1: "내일", 2: "모레"}.get(days, "%d월 %d일" % (t.month, t.day))
    clock = "%d시 %d분" % (h12, t.minute) if t.minute else "%d시" % h12
    return "%s %s %s쯤" % (day, _period_ko(t.hour), clock)


def _now():
    v = os.environ.get("DOGANY_NOW_EPOCH")
    return int(v) if v else int(_dt.datetime.now().timestamp())


def when_for_label(label, lang="ko", now_epoch=None):
    now_epoch = _now() if now_epoch is None else now_epoch
    fire = next_fire(load_schedule(label), now_epoch)
    if fire is None:
        return None
    return when_text(fire, now_epoch, lang)


def retry_line(label, now_epoch=None):
    when = when_for_label(label, "ko", now_epoch)
    return when + RETRY_SUFFIX_KO if when else RETRY_UNKNOWN_KO


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reason")
    r.add_argument("--rc", type=int, required=True)
    r.add_argument("--run-start", type=int, default=0)
    r.add_argument("--log", action="append", default=[])
    r.add_argument("--usage-marker", default="")
    r.add_argument("--label", default="")
    r.add_argument("--key", action="store_true", help="print the class number only")
    t = sub.add_parser("retry")
    t.add_argument("--label", required=True)
    w = sub.add_parser("when")
    w.add_argument("--label", required=True)
    w.add_argument("--lang", default="ko")
    a = ap.parse_args(argv)

    if a.cmd == "reason":
        text = "\n".join(read_run_log(s) for s in a.log)
        fresh = False
        if a.usage_marker and a.run_start:
            try:
                fresh = os.path.getmtime(a.usage_marker) >= a.run_start
            except OSError:
                fresh = False
        off = bool(a.label and a.run_start and host_was_off(a.label, a.run_start))
        key = classify(a.rc, text, fresh, off)
        print(key if a.key else reason_line(key))
    elif a.cmd == "retry":
        print(retry_line(a.label))
    else:
        print(when_for_label(a.label, a.lang) or WHEN_UNKNOWN.get(a.lang, WHEN_UNKNOWN["en"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
