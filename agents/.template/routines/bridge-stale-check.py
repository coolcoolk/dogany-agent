#!/usr/bin/env python3
"""bridge-stale-check.py -- SessionStart hook: read the bridge reconcile
report and surface a LONG-LIVED preserved divergence to the agent (DGN-1240).

Why this exists (the defect class): update.sh's per-file bridge reconcile
(DGN-593) classifies a both-sides-changed file as CONFLICT, refuses to land
it, preserves the local copy, prints two WARN lines, and records the state in
.claude/bridge-reconcile.report. Every part of that machine worked in the
live DGN-1240 incident -- and nobody read any of it for 12 days, while 37
bridge tests failed and a canonical output-discipline contract sat undelivered.
The missing part was not the record but the READER (DGN-1218 anchor). This
hook is that reader.

What is NORMAL vs ABNORMAL (the noise predicate): on a bridge-development
instance a CONFLICT is the RESTING state -- local edits exist, so every
update run reports conflicts, and alerting on mere existence would turn this
into background noise whose reader disappears again (the DGN-1240 defect,
reproduced one layer up). The predicate is therefore DURATION, per file:

    fire only when at least one file flagged CONFLICT / STALE-PRESERVE in the
    CURRENT report has not actually landed for >= _STALE_DAYS days.

"Last actually landed" is recovered from .claude/bridge-backups/: update.sh
backs up the instance copy as <relpath>.user-YYYYMMDD-HHMMSS whenever it
writes a bridge file, so the newest backup timestamp OF THAT FILE is the last
machine write (landing/backfill/accept). The report itself cannot answer
"since when" -- it is a STATE file overwritten on every update run (DGN-593),
which is exactly why duration must be reconstructed from a source that
survives report rewrites. The backup filename timestamp is used (not mtime:
filenames are written once at creation and immune to touch/copy skew). A
flagged file with NO backup at all (both-sides-changed before any landing --
an instance's three local-born test files) gets its clock from a first-seen latch
kept by this hook itself (.telegram_bot/state/bridge-stale-first-seen: JSON
{relpath: epoch}, pruned when a file leaves the report). The report's own ts
line is NOT a usable clock for these: it is refreshed by every update run,
so under a weekly update cadence a never-landed conflict would never age
past the threshold and never fire -- the same silent-forever hole this
reader exists to close. The latch's only reader is this hook (a measurement
latch, not a signal ledger); the signal carrier stays the context injection.

Live calibration (DGN-1240, a live instance, 2026-09-03): sdk_bridge.py/bot.py last
landed 2026-08-20 (14 days, abnormal -> fires); i18n/en.py last written
2026-09-01 by the DGN-1181 backfill (2 days, normal churn -> not counted).
Had this hook existed, it would have fired on day 7 (2026-08-27), six days
before the incident was actually noticed.

RECEIVER (measured, DGN-1232 rule): SessionStart additionalContext reaches
the AGENT's context only -- it carries no relay instruction, so no string
here reaches the owner surface (contrast version-check.py, whose notices
explicitly instruct verbatim relay). Agent-facing classification: no owner
approval gate on this copy, and the text tells the agent NOT to relay it raw.
The action it demands (schedule an accept/back-land pass) is agent work.

Anti-noise throttle: at most one injection per _RENAG_TTL_S (3 days) while
the condition holds, via the reserved _NAG_KEY entry in the same first-seen
latch file (one state file, one reader: this hook itself on the next
session start). A suppression latch, not a signal ledger; the signal
carrier stays the forced context injection (a reader that cannot be
skipped once the hook fires). The nag epoch survives the per-file pruning
on purpose: a conflict that resolves and recurs within the TTL stays
throttled, exactly as it always has.

Overwrite race (report rewritten by a concurrent update run mid-read): the
report is read in one pass; an empty/partial/unparseable read degrades to
silence this session and self-corrects next session (fail-open). Every
fire/hold/no-op decision is appended to
.telegram_bot/state/bridge-stale-check.log so "no alert" is distinguishable
from "the hook never ran" (same observability discipline as DGN-1068).

Design: strictly fail-open for session safety -- any error exits 0 silently.
Output: JSON on stdout matching other SessionStart hooks.
"""
import json
import os
import re
import sys
import time

_STALE_DAYS = 7           # < 7d unlanded = normal dev churn; >= 7d = stuck
_RENAG_TTL_S = 3 * 24 * 3600   # re-nag at most every 3 days while stale
_LOG_MAX_BYTES = 65536    # crude rotation cap for the decision log
_REPORT_RELPATH = os.path.join(".claude", "bridge-reconcile.report")
_BACKUPS_RELPATH = os.path.join(".claude", "bridge-backups")
_FIRST_SEEN_RELPATH = os.path.join(
    ".telegram_bot", "state", "bridge-stale-first-seen")
# Reserved latch key for the last-nag epoch ('/' cannot appear in it, so it
# can never collide with a report relpath).
_NAG_KEY = "__nag"
_LOG_RELPATH = os.path.join(".telegram_bot", "state", "bridge-stale-check.log")
# update.sh backup suffix: <name>.user-YYYYMMDD-HHMMSS
_BACKUP_SUFFIX_RE = re.compile(r"\.user-(\d{8})-(\d{6})$")


def _log(instance_root, text):
    """Append one decision line; truncate-then-append over the cap. Silent."""
    try:
        path = os.path.join(instance_root, _LOG_RELPATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a"
        try:
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        with open(path, mode, encoding="utf-8") as fh:
            fh.write("{} {}\n".format(stamp, text))
    except Exception:
        pass


def _parse_report(path):
    """Return (report_ts_epoch_or_None, {relpath: set(flags)}).

    Flags are 'CONFLICT' / 'STALE-PRESERVE'. LANDED/BACKFILLED/other lines are
    ignored. Raises nothing: any malformed line is skipped; an unreadable file
    propagates to the caller as an exception (caller exits silently)."""
    ts = None
    flagged = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("# ts:"):
                raw = line[len("# ts:"):].strip()
                try:
                    # e.g. 2026-09-01T20:38:23+0900 -- parse the local part;
                    # the offset is the writer's own local zone (same host).
                    ts = time.mktime(
                        time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    ts = None
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] in ("CONFLICT", "STALE-PRESERVE"):
                flagged.setdefault(parts[1].strip(), set()).add(parts[0])
    return ts, flagged


def _last_landed_epoch(instance_root, relpath):
    """Newest backup timestamp for relpath (filename-encoded), or None.

    Backups live at .claude/bridge-backups/<relpath>.user-YYYYMMDD-HHMMSS.
    The FILENAME timestamp is authoritative (written once at creation)."""
    try:
        bdir = os.path.join(instance_root, _BACKUPS_RELPATH,
                            os.path.dirname(relpath))
        base = os.path.basename(relpath)
        newest = None
        for name in os.listdir(bdir):
            if not name.startswith(base + ".user-"):
                continue
            m = _BACKUP_SUFFIX_RE.search(name)
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(
                    m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
            except Exception:
                continue
            if newest is None or ts > newest:
                newest = ts
        return newest
    except Exception:
        return None


def _load_first_seen(instance_root):
    """{relpath: first_seen_epoch, _NAG_KEY: last_nag_epoch} latch, {} on
    any failure (fail-open: a lost latch only restarts clocks and re-allows
    one nag, delaying/repeating -- never faking -- an alert)."""
    try:
        path = os.path.join(instance_root, _FIRST_SEEN_RELPATH)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: float(v) for k, v in data.items()
                    if isinstance(k, str)}
    except Exception:
        pass
    return {}


def _save_first_seen(instance_root, latch):
    try:
        path = os.path.join(instance_root, _FIRST_SEEN_RELPATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(latch, fh)
    except Exception:
        pass


def _emit(stale, report_ts, now):
    """stale: list of (relpath, flags, age_days) with age >= _STALE_DAYS."""
    stale.sort(key=lambda t: -t[2])
    oldest_rel, _, oldest_age = stale[0]
    lines = ", ".join(
        "{} ({}, {}d unlanded)".format(rel, "+".join(sorted(flags)), int(age))
        for rel, flags, age in stale)
    ts_disp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(report_ts))
               if report_ts else "unknown")
    note = (
        "[bridge-stale] The update machinery has been refusing to land "
        "canonical bridge/ files for {age}+ days (threshold {th}d) while "
        "preserving local copies: {files}. Report ts: {rts} "
        "(.claude/bridge-reconcile.report). This is the DGN-1240 trap "
        "class: canonical fixes are NOT on this disk, and the warning has "
        "had no reader. ACTION for the agent: this session or today, open "
        "or refresh a worklog ticket for a bridge accept/back-land pass "
        "and schedule it -- do NOT silently drop this. Do NOT auto-accept "
        "files inside an unrelated session, and do NOT relay this raw "
        "notice to the owner (agent-facing); if the owner is affected "
        "(e.g. failing bridge tests, undelivered contracts), summarize in "
        "your own words per your output rules."
    ).format(age=int(oldest_age), th=_STALE_DAYS, files=lines, rts=ts_disp)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))
    return oldest_rel, oldest_age


def main():
    try:
        sys.stdin.read()  # consume the SessionStart payload; close the pipe
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    instance_root = os.path.dirname(here)

    report_path = os.path.join(instance_root, _REPORT_RELPATH)
    if not os.path.isfile(report_path):
        sys.exit(0)  # instance never ran a bridge reconcile -- nothing to read

    try:
        report_ts, flagged = _parse_report(report_path)
    except Exception:
        sys.exit(0)  # unreadable/partial (overwrite race) -- silent, retry next session

    now = time.time()
    # First-seen latch: clock source for flagged files that have NO backup
    # (never machine-written). Prune entries that left the report, so a
    # resolved-then-recurring conflict restarts its clock. Pruning runs
    # BEFORE the clean-report early exit -- otherwise a report that goes
    # clean would leave dead entries whose stale clocks fire a false alarm
    # the day the same path ever conflicts again.
    latch = _load_first_seen(instance_root)
    pruned = {k: v for k, v in latch.items()
              if k in flagged or k == _NAG_KEY}
    for relpath in flagged:
        pruned.setdefault(relpath, now)
    if pruned != latch:
        _save_first_seen(instance_root, pruned)
    latch = pruned

    if not flagged:
        _log(instance_root, "no-op: report clean (no CONFLICT/STALE lines)")
        sys.exit(0)

    stale = []
    for relpath, flags in flagged.items():
        landed = _last_landed_epoch(instance_root, relpath)
        if landed is None:
            landed = latch.get(relpath, now)  # no backup ever -> latch clock
        age_days = (now - landed) / 86400.0
        if age_days >= _STALE_DAYS:
            stale.append((relpath, flags, age_days))

    if not stale:
        _log(instance_root,
             "no-op: {} flagged file(s), all under {}d (normal dev churn)"
             .format(len(flagged), _STALE_DAYS))
        sys.exit(0)

    last_nag = latch.get(_NAG_KEY, 0.0)
    if (now - last_nag) < _RENAG_TTL_S:
        _log(instance_root,
             "hold: {} stale file(s) but nagged {:.1f}h ago (TTL {}h)"
             .format(len(stale), (now - last_nag) / 3600.0,
                     _RENAG_TTL_S // 3600))
        sys.exit(0)

    oldest_rel, oldest_age = _emit(stale, report_ts, now)
    latch[_NAG_KEY] = now
    _save_first_seen(instance_root, latch)
    _log(instance_root,
         "fire: {} stale file(s), oldest {} at {:.0f}d"
         .format(len(stale), oldest_rel, oldest_age))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
