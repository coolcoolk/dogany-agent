"""
DGN-240 shared instant module (spec v3 section 3.1, T11 front half).

SINGLE OWNER of every recurrence instant computation. materialize (roller),
rule-conformance (roller), and the advisory conflict probe all import THIS
module -- no other instant computation implementation may exist in the
codebase (M-A: kills the systematic-delta root of conformance oscillation).

PURE functions only: no DB access, no clock access (now()/today()), no
file IO. Same input = same output (H-INST-PURE / H-INST-VECTORS gates).

Cadence DSL v1 (closed grammar, spec 1.2):
    cadence := "D"                      -- daily
             | "W:" DAY ("," DAY)*      -- weekly day-set, DAY in MON..SUN
             | "I:" N "@" YYYY-MM-DD    -- every N days from anchor, N>=2

KST-single (no DST) scope inherited from DGN-179; the tz math still goes
through zoneinfo (no fixed offsets) so the module stays correct if
display_tz ever changes.
"""

import datetime
import re
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_DAY_INDEX = {name: i for i, name in enumerate(DAY_NAMES)}  # Mon=0 .. Sun=6

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_INTERVAL_RE = re.compile(r"^I:(\d+)@(\d{4}-\d{2}-\d{2})$")

DEFAULT_DURATION_MIN = 30
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_cadence(cadence):
    """Validate + decompose a cadence string.
    Returns one of:
        ("D",)
        ("W", frozenset_of_weekday_ints)     -- Mon=0 .. Sun=6
        ("I", n, anchor_date)                -- n >= 2, anchor datetime.date
    Raises ValueError on anything outside the closed grammar."""
    if cadence == "D":
        return ("D",)
    if cadence is not None and cadence.startswith("W:"):
        parts = cadence[2:].split(",")
        if not parts or any(p not in _DAY_INDEX for p in parts):
            raise ValueError("bad weekly cadence: %r" % cadence)
        return ("W", frozenset(_DAY_INDEX[p] for p in parts))
    m = _INTERVAL_RE.match(cadence or "")
    if m:
        n = int(m.group(1))
        if n < 2:
            raise ValueError("interval cadence needs N>=2: %r" % cadence)
        try:
            anchor = datetime.date.fromisoformat(m.group(2))
        except ValueError:
            raise ValueError("bad interval anchor date: %r" % cadence)
        return ("I", n, anchor)
    raise ValueError("bad cadence: %r" % cadence)


def occurs(cadence, d):
    """Spec 1.2 occurs(): does cadence fire on local date d?
    d: datetime.date or 'YYYY-MM-DD' string."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    parsed = parse_cadence(cadence)
    if parsed[0] == "D":
        return True
    if parsed[0] == "W":
        return d.weekday() in parsed[1]
    _tag, n, anchor = parsed
    return d >= anchor and (d - anchor).days % n == 0


def _as_date(d):
    return datetime.date.fromisoformat(d) if isinstance(d, str) else d


def _utc(dt_local):
    return dt_local.astimezone(timezone.utc).strftime(UTC_FMT)


def rule_instants(defn, d):
    """(start_at, end_at, schedule_kind) for def occurrence on local date d.

    defn: mapping with keys schedule_kind, time_of_day, duration_min,
    display_tz (db row object or plain dict). d: date or 'YYYY-MM-DD'.

    timed:   start = d + time_of_day in display_tz; end = start + duration
             (duration_min NULL -> 30, spec 1.3 default).
    all_day: day-block [d 00:00 local, d+1 00:00 local) -- byte-identical
             convention to lifekit.all_day_instants (H-INST-EQ gate).
    """
    d = _as_date(d)
    sk = defn["schedule_kind"]
    tz = ZoneInfo(defn["display_tz"] or "Asia/Seoul")
    if sk == "timed":
        tod = defn["time_of_day"]
        if tod is None or not _TIME_RE.match(tod):
            raise ValueError("timed def needs time_of_day HH:MM, got %r" % tod)
        hh, mm = int(tod[:2]), int(tod[3:])
        dur = defn["duration_min"]
        dur = DEFAULT_DURATION_MIN if dur is None else int(dur)
        start_local = datetime.datetime(d.year, d.month, d.day, hh, mm, 0,
                                        tzinfo=tz)
        end_local = start_local + timedelta(minutes=dur)
        return _utc(start_local), _utc(end_local), "timed"
    if sk == "all_day":
        start_local = datetime.datetime(d.year, d.month, d.day, 0, 0, 0,
                                        tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        return _utc(start_local), _utc(end_local), "all_day"
    raise ValueError("routine def schedule_kind must be timed|all_day, got %r"
                     % sk)


def occurrence_dates(defn, from_date, to_date):
    """All local dates in [from_date, to_date] (INCLUSIVE) where the def's
    cadence fires, clamped to [def.start_date, def.end_date]. Pure expansion
    helper shared by project_virtual and the roller's materialize loop."""
    from_date = _as_date(from_date)
    to_date = _as_date(to_date)
    lo = max(from_date, _as_date(defn["start_date"]))
    hi = to_date
    if defn["end_date"] is not None:
        hi = min(hi, _as_date(defn["end_date"]))
    out = []
    d = lo
    while d <= hi:
        if occurs(defn["cadence"], d):
            out.append(d)
        d += timedelta(days=1)
    return out


def project_virtual(defs, from_date, to_date):
    """Spec 3.1: [(recurrence_id, title, start_at, end_at, exclusive)] for
    every occurrence of the given defs in [from_date, to_date] (inclusive).
    Input = def rows + date bounds only; deterministic, no clock, no DB."""
    hits = []
    for defn in defs:
        if defn["cadence"] is None:
            continue
        for d in occurrence_dates(defn, from_date, to_date):
            sa, ea, _sk = rule_instants(defn, d)
            hits.append((defn["recurrence_id"], defn["title"], sa, ea,
                         defn["exclusive"]))
    return hits
