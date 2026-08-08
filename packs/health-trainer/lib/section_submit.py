#!/usr/bin/env python3
"""Briefing section generation + submission (Warg side) -- v3 5.2.

Generation runs in Warg's own transcript context (memory accrual); this
module is the mechanical seam: per-type gen lock, same-day idempotence,
generator invocation, expires stamping, atomic submit to the Ag inbox.

expires semantics (grill-final FATAL-1): expires = the matching Ag
aggregation deadline, stamped as metadata. It does NOT bound aggregation
validity -- a section is valid for its whole target day (the aggregation
step fires AT the deadline, and a delayed briefing later the same day
must still pick it up). Archive-expiry uses the day-over rule
(handoff.section_still_valid); a section never leaks into the next day's
briefing because aggregation only accepts created-today messages.

Generator resolution:
  HANDOFF_SECTION_GENERATOR env (argv: <type>; stdout = section body)
  else: headless claude -p with routines/prompts/section-<type>.md
        (deployed instance; requires claude on PATH).

Section budget: 10-line cap (V4) is enforced mechanically here (truncate
+ note) as a belt; the prompt owns the real budget.

English/ASCII only (prompts instruct Korean user-facing output).
"""

import argparse
import datetime
import fcntl
import os
import subprocess
import sys
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handoff   # noqa: E402

SECTION_CAP_LINES = 10
# Ag aggregation deadlines, local time (plist-measured, v3 5.2)
DEADLINES = {"morning": (5, 0), "retro": (21, 0), "weekly": (22, 0)}


def deadline_utc(stype, now_local):
    h, m = DEADLINES[stype]
    dl = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if dl <= now_local:
        dl += datetime.timedelta(days=1)
    return dl.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate(warg_root, stype):
    gen = os.environ.get("HANDOFF_SECTION_GENERATOR")
    if gen:
        out = subprocess.run([gen, stype], check=True, timeout=600,
                             capture_output=True, text=True)
        return out.stdout
    prompt_path = os.path.join(warg_root, "routines", "prompts",
                               "section-%s.md" % stype)
    with open(prompt_path) as f:
        prompt = f.read()
    # Strip CLAUDECODE + CLAUDE_CODE_ENTRYPOINT so headless claude does not
    # see an enclosing session and refuses to start.  --allowedTools prevents
    # the interactive permission gate (no approver in an unattended job).
    headless_env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    out = subprocess.run(
        ["claude", "-p", prompt,
         "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep"],
        check=True, timeout=900, capture_output=True, text=True,
        cwd=warg_root, env=headless_env)
    return out.stdout


def cap_lines(body, cap=SECTION_CAP_LINES, allow_block=False):
    lines = body.rstrip("\n").split("\n")
    if allow_block or len(lines) <= cap:
        return body.rstrip("\n")
    return "\n".join(lines[:cap] + ["(섹션 캡으로 축약됨)"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--ag-root", required=True)
    ap.add_argument("--type", required=True,
                    choices=("morning", "retro", "weekly"))
    ap.add_argument("--tz", default="Asia/Seoul")
    args = ap.parse_args(argv)

    tz = zoneinfo.ZoneInfo(args.tz)
    now_local = datetime.datetime.now(tz)
    day = now_local.strftime("%Y%m%d")
    state_dir = os.path.join(args.root, ".telegram_bot", "state")
    os.makedirs(state_dir, exist_ok=True)

    # (b) same-day submitted state -> idempotent retry slot
    state_path = os.path.join(state_dir,
                              "section-%s.%s.submitted" % (args.type, day))
    if os.path.exists(state_path):
        print("already submitted today (%s)" % args.type)
        return 0

    # (a) per-type gen lock
    lock_file = os.path.join(state_dir, ".gen-%s.lock" % args.type)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("gen lock busy (%s): first run still alive, exiting"
              % args.type)
        os.close(fd)
        return 0
    try:
        if os.path.exists(state_path):   # re-check under lock
            return 0
        body = cap_lines(generate(args.root, args.type),
                         allow_block=(args.type == "weekly"))
        meta = {
            "from": "warg",
            "to": "ag",
            "type": "report.section.%s" % args.type,
            "expires": deadline_utc(args.type, now_local),
        }
        path = handoff.submit(args.ag_root, meta, body)
        with open(state_path, "w") as f:
            f.write(path + "\n")
        print(path)
        return 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
