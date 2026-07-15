"""
DGN-180 GCal/Tasks surface adapter -- PRODUCTION build (mirror/).
Staged by step-4 prep from the sandbox ground truth (harness 108/108);
delta vs sandbox = paths/surface constants + live-SDK seam only.

DGN-240 S8 delta (staged copy, apply at S9 step 3 -- see
sandbox/dgn240-build/patches/adapter.py.patch.md):
  T1  in_mirror_scope: materialized routine instances IN scope;
      notion-import out (7.4); anchor-less belt kept (N1).
  T2  sweep candidate query: expired-task terminal filter (2.3, OQ-3)
      + scope-comment refresh.
  T12 tasks due-clear inbound on a routine instance: 'rejected_untimed'
      terminal classification (log + snapshot advance + re-echo, 5.5 M-C).
Original header follows.

DGN-180 GCal/Tasks surface adapter -- sandbox implementation, fix round 2.
Spec: DGN-180 v3 canonical (W0-W14) + grill-3 findings doc
(worklog/DGN-180-grill3-realcode-20260709.md) -- both LAW.
SoT = sqlite event (DGN-179 v5 LOCK).
NO live cutover. Writes only to disposable dgn180-sandbox calendar/tasklist.
English/ASCII only.

Fix map (grill-3):
  F1  projections: inbound converted to sqlite representation FIRST, hashed
      over the SAME canonical field dict as push (calendar_projection_* /
      tasks_projection_*).
  F2  pageToken loops on both pulls; syncToken saved only from final page;
      tasks watermark advances only after full traversal.
  F3  foreign-item guard (_extract_ulid) + per-item try/except in both pulls.
  F4  inbound completed -> sdk_bridge.settle_done (SDK force_settle verb).
  M5  inbound calendar apply = per-field 3-way vs stored snapshot projection;
      schedule via sdk_bridge.bypass_schedule_apply (version/updated_at/
      schedule_kind re-derivation/recompute), content via content_update.
  M6  etag guard: compare-then-put (gws cannot send If-Match header -- see
      OPEN QUESTION); mismatch -> pull-first 3-way re-merge, then push.
  M7  mirror_outbox drain worker: claim/lease, exponential backoff on
      403/429/5xx, resumable backfill, single-flight lock.
  M8  GTasks inbound consumes title/notes/due + needsAction reopen
      (sdk_bridge.unsettle, DGN-180 verb).
  M9  in_mirror_scope: recurrence_id/is_routine rows excluded + counter log.
  M10 revive dead-end: second 404 -> generation-suffixed surface id.
  M11 get_src_conn -> 179 get_conn discipline (WAL/busy_timeout/retry).
  m12 bootstrap re-discovery by description marker (calendar).
  m14 open-ended placeholder end computed via display_tz (no +09:00 literal).
  m15 comment fixed: sqlite all_day end_at and GCal end.date are BOTH
      exclusive -> direct date conversion, no day shift.
  m16 missing GCal description key = explicit empty note ('').
  m17 sentinel strip is non-anchored (text below the sentinel survives).
  +   circuit breaker on mass-cancelled inbound (W10).
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import sdk_bridge
import notify as notify_mod
import http_direct
import mirror_i18n

# Self-locating paths (module home = mirror/): SoT DB = ../database/
# lifekit.db, mirror state lives next to the module. No absolute home paths.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# DGN-268 S1: config seam. Per-instance surface identity (calendar/tasklist
# names, marker, display tz) is parameterized out of source literals into the
# instance config so the mirror is a product, not a single-instance fixture.
#
# Sources (both optional; a fresh dev checkout has NEITHER -> {} / defaults):
#   ../config/lifekit.conf   -- per-instance lifekit activation + mirror keys
#   ../.instance.conf        -- non-secret instance manifest (agent label etc.)
# Format = shell-style KEY=value, '#' comments, blank lines ignored.
#
# ZERO-DELTA GUARANTEE: every default below equals the prior canonical literal
# (or a system-derived value that falls back to it), so behavior is byte-
# identical when no config file is present. S1 changes NO sync logic.
# ---------------------------------------------------------------------------

_CONF_CACHE = None


def _parse_conf_file(path, into):
    """Merge KEY=value pairs from a shell-style conf file into `into`.
    Existing keys are NOT overwritten (earlier source wins). Missing file =
    no-op. Tolerant: malformed lines are skipped, never raised."""
    try:
        with open(path, "r") as fh:
            lines = fh.readlines()
    except (OSError, IOError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in into:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        into[key] = val


def _load_conf():
    """Parse the instance config once, cached. lifekit.conf is the primary
    source; .instance.conf provides fallback identity keys (agent label).
    Returns {} when neither file exists (fresh checkout)."""
    global _CONF_CACHE
    if _CONF_CACHE is None:
        conf = {}
        _parse_conf_file(
            os.path.join(_MODULE_DIR, "..", "config", "lifekit.conf"), conf)
        _parse_conf_file(
            os.path.join(_MODULE_DIR, "..", ".instance.conf"), conf)
        _CONF_CACHE = conf
    return _CONF_CACHE


def _reset_conf_cache():
    """Test seam: drop the cached parse so a new config file is re-read."""
    global _CONF_CACHE
    _CONF_CACHE = None


def _agent_slug():
    """Instance agent slug (lowercase name) for marker/label derivation.
    Default 'agent' keeps derived strings well-formed on a bare checkout."""
    conf = _load_conf()
    return conf.get("DOGANY_AGENT_NAME") or "agent"


def _resolve_display_tz():
    """Display timezone. Config-supplied MIRROR_TZ wins; when absent the
    default is the prior canonical literal "Asia/Seoul" UNCONDITIONALLY on
    every platform (strict S1 zero-delta). System-tz autodetect is a value-
    add that belongs in S4 onboarding (the installer already knows the
    instance TZ and wires MIRROR_TZ explicitly), and it is cross-platform
    fragile (a fixed-offset tzinfo like 'KST' has no ZoneInfo key)."""
    conf = _load_conf()
    return conf.get("MIRROR_TZ") or "Asia/Seoul"


def _resolve_cal_summary():
    """Calendar name. Default = agent label (.instance.conf) when present,
    else the prior canonical placeholder literal (zero-delta)."""
    conf = _load_conf()
    return (conf.get("MIRROR_CAL_NAME")
            or conf.get("DOGANY_AGENT_LABEL")
            or "<agent-calendar-name>")


def _resolve_tasklist_title():
    """Tasklist name. Default = the resolved calendar name (spec H2)."""
    conf = _load_conf()
    return conf.get("MIRROR_TASKLIST_NAME") or _resolve_cal_summary()


def _derived_cal_marker():
    """Per-instance calendar description marker: dogany-mirror-<agent> (spec
    H3). Survives a calendar rename because bootstrap FREEZES the value that
    first created the calendar into the mirror_state KV (key 'cal_marker')
    and reads KV-first thereafter."""
    return "dogany-mirror-%s" % _agent_slug()


# Module-level surface identity (config-resolved at import; defaults preserve
# the prior canonical literals when no config file is present).
DISPLAY_TZ_NAME = _resolve_display_tz()
SANDBOX_CAL_SUMMARY = _resolve_cal_summary()
SANDBOX_TASKLIST_TITLE = _resolve_tasklist_title()
CAL_DESCRIPTION_MARKER = _derived_cal_marker()

# H6 (DGN-268 S1): calendar description text. Product string now (was the
# sandbox "Safe to delete." literal). DGN-268 S4: resolved through mirror_i18n
# by AGENT_LANG (i18n key 'mirror.cal_description'); the English literal here is
# the fallback used verbatim when the key/locale file is absent (zero-delta).
CAL_DESCRIPTION_TEXT = mirror_i18n.t(
    "mirror.cal_description",
    "Managed by the agent -- two-way synced with your assistant. "
    "Safe to edit; do not delete.")
DB_PATH = os.path.normpath(os.path.join(_MODULE_DIR, "..", "database", "lifekit.db"))
STATE_DB_PATH = os.path.join(_MODULE_DIR, "mirror_state.db")

CB_CANCELLED_THRESHOLD = 10   # W10 circuit breaker: mass-cancelled per poll
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_LEASE_SECONDS = 300
# 412 included (grill-5 finding 9): a double-412 hot-edit window is a
# transient contention state -- requeue within the attempts budget.
RETRYABLE_HTTP = {403, 412, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# ulid <-> calendar-safe hex id (W2/V2)
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_MAP = {c: i for i, c in enumerate(_CROCKFORD)}
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SENTINEL_RE = re.compile(r"\n?\[ulid:([^\]]+)\]")

def ulid_to_hex(ulid: str) -> str:
    """ULID (26 Crockford base32 chars) -> 32-char lowercase hex.
    hex charset is a subset of base32hex -> legal Calendar event id."""
    ulid = ulid.upper()
    val = 0
    for c in ulid:
        val = val * 32 + _CROCKFORD_MAP[c]
    return format(val, "032x")

def hex_to_ulid(hex_id: str) -> str:
    """32-char hex -> ULID string. Caller MUST verify _HEX32_RE first (F3)."""
    val = int(hex_id, 16)
    result = []
    for _ in range(26):
        result.append(_CROCKFORD[val & 31])
        val >>= 5
    return "".join(reversed(result))


# ---------------------------------------------------------------------------
# Routing predicate (V1) + mirror scope (M9)
# ---------------------------------------------------------------------------

def route_surface(event: dict) -> str:
    """'calendar' or 'tasks'. Routing only -- scope filter is separate (M9)."""
    if event["kind"] == "appointment":
        return "calendar"
    if event["kind"] == "task":
        return "calendar" if event["schedule_kind"] == "timed" else "tasks"
    raise ValueError("Unknown kind: %r" % event["kind"])


def in_mirror_scope(event: dict) -> bool:
    """DGN-240 5.1 (T1, replaces the M9 safe default): materialized routine
    instances ARE mirror scope. notion-import history stays out (7.4);
    anchor-less routine rows stay out (N1 belt)."""
    if event.get("recurrence_id"):
        return event.get("created_by") != "notion-import"   # 7.4
    return not event.get("is_routine")   # N1 belt: anchor-less stays out


# ---------------------------------------------------------------------------
# Time conversion (179 M4 rule: display_tz for all_day, UTC for timed)
# ---------------------------------------------------------------------------

def _to_rfc3339_local(ts_utc: str, tz: ZoneInfo) -> str:
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(tz)
    offset = dt_local.utcoffset()
    total_secs = int(offset.total_seconds())
    sign = "+" if total_secs >= 0 else "-"
    h, m = divmod(abs(total_secs) // 60, 60)
    return dt_local.strftime("%Y-%m-%dT%H:%M:%S") + ("%s%02d:%02d" % (sign, h, m))


def utc_instant_to_gcal_datetime(start_at, end_at, schedule_kind,
                                  display_tz=DISPLAY_TZ_NAME):
    """sqlite canonical UTC instants -> GCal EventDateTime pair.
    timed -> dateTime+timeZone; all_day -> date via display_tz.
    m14: open-ended placeholder end (start+1h) goes through the SAME
    display_tz conversion (no hardcoded offset)."""
    tz = ZoneInfo(display_tz)
    if schedule_kind == "timed":
        start_dt = {"dateTime": _to_rfc3339_local(start_at, tz), "timeZone": display_tz}
        if end_at:
            end_dt = {"dateTime": _to_rfc3339_local(end_at, tz), "timeZone": display_tz}
        else:
            dt = datetime.strptime(start_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ph = (dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_dt = {"dateTime": _to_rfc3339_local(ph, tz), "timeZone": display_tz}
        return start_dt, end_dt
    elif schedule_kind == "all_day":
        def utc_to_date(ts_utc):
            dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).strftime("%Y-%m-%d")
        start_dt = {"date": utc_to_date(start_at)}
        if end_at:
            # m15: sqlite all_day end_at is exclusive next-midnight AND GCal
            # end.date is exclusive -> direct conversion, no day shift.
            end_dt = {"date": utc_to_date(end_at)}
        else:
            # No end stored: single-day block = start date + 1 day (exclusive).
            d = datetime.strptime(utc_to_date(start_at), "%Y-%m-%d") + timedelta(days=1)
            end_dt = {"date": d.strftime("%Y-%m-%d")}
        return start_dt, end_dt
    raise ValueError("Unexpected schedule_kind for Calendar: %r" % schedule_kind)


def gcal_datetime_to_utc_instants(start_obj, end_obj, display_tz=DISPLAY_TZ_NAME):
    """GCal EventDateTime pair -> sqlite UTC instants (start_at, end_at)."""
    tz = ZoneInfo(display_tz)

    def parse_dt(obj):
        if not obj:
            return None
        if "dateTime" in obj:
            try:
                dt = datetime.fromisoformat(obj["dateTime"])
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                return None
        if "date" in obj:
            d = datetime.strptime(obj["date"], "%Y-%m-%d")
            return d.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return None

    return parse_dt(start_obj), parse_dt(end_obj)


def task_due_from_event(event: dict):
    """GTasks due date (YYYY-MM-DD) for all_day tasks; untimed -> None."""
    if event["schedule_kind"] == "all_day" and event.get("start_at"):
        tz = ZoneInfo(event.get("display_tz") or DISPLAY_TZ_NAME)
        dt = datetime.strptime(event["start_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%Y-%m-%d")
    return None


def all_day_instants_from_date(date_str, display_tz=DISPLAY_TZ_NAME):
    """YYYY-MM-DD -> (start_at, end_at) canonical UTC instants for a one-day
    all_day block (local midnight .. next local midnight, exclusive)."""
    tz = ZoneInfo(display_tz)
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    start = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (d + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


# ---------------------------------------------------------------------------
# GWS CLI wrapper
# ---------------------------------------------------------------------------

class GwsError(RuntimeError):
    def __init__(self, msg, code=0, reason=""):
        super().__init__(msg)
        self.http_code = code
        self.reason = reason


def gws(*args, body=None) -> dict:
    """Call gws CLI, return parsed JSON. Raises GwsError on API errors."""
    cmd = ["gws"] + list(args)
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr_clean = "\n".join(
        l for l in result.stderr.splitlines() if "keyring" not in l.lower())
    if not result.stdout.strip():
        raise GwsError("gws no output: %s\nArgs: %s" % (stderr_clean, args))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise GwsError("gws invalid JSON: %s" % result.stdout[:200])
    if "error" in data and isinstance(data["error"], dict):
        err = data["error"]
        raise GwsError("API error %s %s: %s" % (
            err.get("code", 0), err.get("reason", ""), err.get("message", "")),
            code=err.get("code", 0), reason=err.get("reason", ""))
    return data


def gws_delete(*args) -> bool:
    """Delete commands return 204 No Content (empty stdout, exit 0)."""
    cmd = ["gws"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True
    try:
        data = json.loads(result.stdout)
        err = data.get("error", {})
        raise GwsError("API error %s: %s" % (err.get("code", 0), err.get("message", "")),
                       code=err.get("code", 0), reason=err.get("reason", ""))
    except (json.JSONDecodeError, AttributeError):
        raise GwsError("gws delete failed (exit %d): %s"
                       % (result.returncode, result.stderr[:200]))


# ---------------------------------------------------------------------------
# State DB helpers
# ---------------------------------------------------------------------------

def open_state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    schema_path = os.path.join(os.path.dirname(__file__), "mirror_state.sql")
    conn.executescript(open(schema_path).read())
    try:  # pre-existing state DBs: add reconcile requeue column (X10b-6)
        conn.execute(
            "ALTER TABLE mirror_outbox ADD COLUMN requeues "
            "INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:  # g5-4 terminal state for repeatedly-failed rows
        conn.execute(
            "ALTER TABLE mirror_outbox ADD COLUMN dead "
            "INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM mirror_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute(
        "INSERT INTO mirror_state(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mirror_log(conn, category, event_ulid=None, detail=None):
    conn.execute(
        "INSERT INTO mirror_log(ts, event_ulid, category, detail) VALUES(?,?,?,?)",
        (_now_iso(), event_ulid, category, detail))
    conn.commit()


# ---------------------------------------------------------------------------
# W6 promotion: surface bookkeeping home = event-table columns (v3 W6
# "필수 승격", v4 X10b). Spec basis: v3 W6 mandates event.gcal_event_id /
# event.gtask_id as REQUIRED bookkeeping columns -- adding nullable mirror
# columns is DGN-180-owned and does not touch 179 v5-locked semantics
# (no CHECK, no derivation, no slot predicate involvement). Bookkeeping
# writes do NOT bump version/updated_at (not a domain mutation; W6: push
# txn records/clears). State KV becomes cache-only fallback (src_conn
# absent or columns not yet migrated).
# ---------------------------------------------------------------------------

BK_KV_KEY = {
    "gcal_event_id": "gcal_id:%s",
    "gtask_id": "gtask_id:%s",
    "gcal_etag": "etag:cal:%s",
    "gtask_etag": "etag:task:%s",
}
def _bk_cols_available(src_conn):
    """No caching: id(conn)-keyed caches risk GC id reuse (grill-5 NIT) and
    PRAGMA table_info is an in-memory schema lookup (cheap per call)."""
    if src_conn is None:
        return False
    try:
        cols = {r[1] for r in src_conn.execute("PRAGMA table_info(event)")}
        return "gcal_event_id" in cols
    except sqlite3.Error:
        return False


def bk_get(state_conn, src_conn, ulid, field):
    """Read surface bookkeeping: promoted column first, KV cache fallback."""
    if _bk_cols_available(src_conn):
        row = src_conn.execute(
            "SELECT %s FROM event WHERE ulid=?" % field, (ulid,)).fetchone()
        if row is not None and row[0]:
            return row[0]
    return get_state(state_conn, BK_KV_KEY[field] % ulid) or None


def bk_set(state_conn, src_conn, ulid, field, value):
    """Write bookkeeping to the promoted home (event column) when available;
    always mirror to KV (cache-only during transition). No version bump."""
    if _bk_cols_available(src_conn):
        src_conn.execute(
            "UPDATE event SET %s=? WHERE ulid=?" % field,
            (value or None, ulid))
        src_conn.commit()
    set_state(state_conn, BK_KV_KEY[field] % ulid, value or "")


IFMATCH_STATS = {"puts": 0, "http412": 0}


def _direct(fn, *args, **kwargs):
    """grill-5 finding 1: every direct-HTTPS failure must re-enter the
    outbox retry classifier as GwsError. HttpError keeps its HTTP code;
    token-endpoint / network / store-rotation (InvalidTag) failures map to
    GwsError 503 (retryable infra)."""
    try:
        return fn(*args, **kwargs)
    except http_direct.HttpError as e:
        raise GwsError("direct HTTP %d: %s" % (e.code, e), code=e.code) from e
    except GwsError:
        raise
    except Exception as e:
        raise GwsError("direct lane infra error: %s" % e, code=503) from e


def _title_of(src_conn, ulid):
    if src_conn is None:
        return ulid
    row = src_conn.execute(
        "SELECT title FROM event WHERE ulid=?", (ulid,)).fetchone()
    return row["title"] if row else ulid


def _notify_overlap(state_conn, src_conn, ulid, detail):
    notify_mod.notify(state_conn, "overlap_notice", ulid,
                      title=_title_of(src_conn, ulid), detail=detail)


# DGN-333 (MAJOR-5 rev): per-apply overlap DETECTION stays (audit line at the
# apply site), but the user NOTIFICATION is deferred to the end of the sync
# cycle -- mid-batch sequential applies create transient overlaps against
# not-yet-moved rows (false alarms). Candidates collected here are re-checked
# against the FINAL state by overlap_flush() (poll_cycle owns the flush; one
# poller cycle = one process, so module state is cycle-local).
_OVERLAP_PENDING = []  # list of (ulid, detail-at-detection)


def _overlap_defer(ulid, detail):
    if not any(u == ulid for u, _d in _OVERLAP_PENDING):
        _OVERLAP_PENDING.append((ulid, detail))


def overlap_flush(state_conn, src_conn):
    """Batch-end recheck of deferred overlap candidates (DGN-333). Notifies
    only overlaps that still exist in the final state; at most one notice
    per overlap pair. Returns the number of notices queued."""
    pending, _OVERLAP_PENDING[:] = list(_OVERLAP_PENDING), []
    notified_pairs = set()
    n = 0
    for ulid, detail in pending:
        hit, warning = sdk_bridge.mirror_overlap_recheck(src_conn, ulid)
        if hit is None:
            mirror_log(state_conn, "overlap_recheck_cleared", ulid,
                       "transient mid-batch overlap resolved (was: %s)"
                       % detail)
            continue
        pair = frozenset((ulid, hit))
        if pair in notified_pairs:
            continue
        notified_pairs.add(pair)
        mirror_log(state_conn, "overlap_recheck_confirmed", ulid, warning)
        _notify_overlap(state_conn, src_conn, ulid, warning)
        n += 1
    return n


# ---------------------------------------------------------------------------
# F1: canonical surface projections (SAME dict on push and inbound sides)
# All values in sqlite representation, canonicalized: None-note -> '',
# missing surface keys -> defaults matching what push emits.
# ---------------------------------------------------------------------------

def event_to_cal_status(event: dict):
    """W4: (gcal_status, colorId|None)."""
    outcome = event.get("settled_outcome")
    status = event.get("status", "open")
    if outcome == "abandoned" or status == "abandoned":
        return "cancelled", None
    if outcome == "done" or status == "done":
        return "confirmed", "10"   # done marker (sage)
    if status == "expired":
        return "confirmed", "8"    # expired marker (graphite)
    return "confirmed", None


def calendar_projection_from_event(event: dict) -> dict:
    """What the Calendar surface SHOULD hold for this event, expressed in
    sqlite representation. start/end are roundtripped through the exact push
    conversion so placeholder ends (open-ended) hash identically."""
    gcal_status, color_id = event_to_cal_status(event)
    start_at = end_at = None
    if event.get("schedule_kind") in ("timed", "all_day") and event.get("start_at"):
        s_obj, e_obj = utc_instant_to_gcal_datetime(
            event["start_at"], event.get("end_at"), event["schedule_kind"],
            event.get("display_tz") or DISPLAY_TZ_NAME)
        start_at, end_at = gcal_datetime_to_utc_instants(
            s_obj, e_obj, event.get("display_tz") or DISPLAY_TZ_NAME)
    return {
        "title": event.get("title") or "",
        "note": event.get("note") or "",
        "location": event.get("location") or "",
        "start_at": start_at,
        "end_at": end_at,
        "schedule_kind": event.get("schedule_kind"),
        "gcal_status": gcal_status,
        "color_id": color_id,
        "transparency": "opaque" if event.get("slot_exclusive") else "transparent",
    }


def calendar_projection_from_item(item: dict, display_tz=DISPLAY_TZ_NAME) -> dict:
    """Inbound GCal item -> the SAME canonical dict (F1). m16: missing
    description key = explicit empty note."""
    start_obj = item.get("start", {}) or {}
    schedule_kind = "all_day" if "date" in start_obj else "timed"
    start_at, end_at = gcal_datetime_to_utc_instants(
        start_obj, item.get("end", {}) or {}, display_tz)
    return {
        "title": item.get("summary", "") or "",
        "note": item.get("description", "") or "",
        "location": item.get("location", "") or "",
        "start_at": start_at,
        "end_at": end_at,
        "schedule_kind": schedule_kind,
        "gcal_status": item.get("status", "confirmed"),
        "color_id": item.get("colorId"),
        "transparency": item.get("transparency", "opaque"),
    }


def tasks_projection_from_event(event: dict) -> dict:
    outcome = event.get("settled_outcome")
    status = event.get("status", "open")
    completed = (outcome == "done" or status == "done")
    return {
        "title": event.get("title") or "",
        "note": event.get("note") or "",
        "due_date": task_due_from_event(event),
        "status": "completed" if completed else "needsAction",
    }


def tasks_projection_from_item(item: dict) -> dict:
    due = item.get("due")
    return {
        "title": item.get("title", "") or "",
        "note": _strip_sentinel(item.get("notes", "")),
        "due_date": due[:10] if due else None,
        "status": item.get("status", "needsAction"),
    }


def projection_hash(proj: dict) -> str:
    return hashlib.sha256(json.dumps(proj, sort_keys=True).encode()).hexdigest()


def compute_push_hash(surface: str, event: dict) -> str:
    proj = (calendar_projection_from_event(event) if surface == "calendar"
            else tasks_projection_from_event(event))
    return projection_hash(proj)


def store_push_snapshot(conn, event_ulid, surface, proj: dict):
    conn.execute(
        "INSERT INTO push_snapshot(event_ulid, surface, field_hash, field_json, pushed_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(event_ulid) DO UPDATE SET "
        "surface=excluded.surface, field_hash=excluded.field_hash, "
        "field_json=excluded.field_json, pushed_at=excluded.pushed_at",
        (event_ulid, surface, projection_hash(proj),
         json.dumps(proj, sort_keys=True), _now_iso()))
    conn.commit()


def load_push_snapshot(conn, event_ulid):
    """Returns (hash, projection_dict) or (None, {})."""
    row = conn.execute(
        "SELECT field_hash, field_json FROM push_snapshot WHERE event_ulid=?",
        (event_ulid,)).fetchone()
    if row is None:
        return None, {}
    try:
        return row["field_hash"], json.loads(row["field_json"])
    except Exception:
        return row["field_hash"], {}


# ---------------------------------------------------------------------------
# F3: foreign-item guard
# ---------------------------------------------------------------------------

def _extract_ulid(item: dict):
    """Ours iff extendedProperties.private.ulid present OR id matches
    ^[0-9a-f]{32}$ (our deterministic encoding). Else None = foreign."""
    ulid = (item.get("extendedProperties", {}) or {}).get("private", {}).get("ulid")
    if ulid:
        return ulid
    event_id = item.get("id", "") or ""
    if _HEX32_RE.match(event_id):
        return hex_to_ulid(event_id)
    return None


# ---------------------------------------------------------------------------
# Bootstrap (m12: description-marker re-discovery for the calendar)
# ---------------------------------------------------------------------------

def _frozen_cal_marker(conn):
    """DGN-268 S1 (H3): the calendar description marker, KV-first. The first
    bootstrap freezes the derived marker into mirror_state (key 'cal_marker');
    every later run reads that frozen value. This makes a later rename of
    MIRROR_CAL_NAME / the agent slug safe -- the calendar is re-discovered by
    the marker that CREATED it, never orphaned. Legacy state DBs with no
    frozen marker fall back to the derived value (and freeze it)."""
    marker = get_state(conn, "cal_marker")
    if not marker:
        marker = CAL_DESCRIPTION_MARKER
        set_state(conn, "cal_marker", marker)
    return marker


class BootstrapAmbiguous(RuntimeError):
    """DGN-268 S2: bootstrap found a same-name surface that does NOT carry our
    marker -- a foreign/ambiguous calendar or tasklist that must NOT be
    silently adopted or written into. Carries the candidate(s) so the S4
    onboarding layer can ask the user 'adopt this existing one, or create a
    new one under a different name?'. Setting MIRROR_ADOPT_UNMARKED=true (the
    user answered 'adopt') suppresses the signal and adopts + stamps."""
    def __init__(self, candidates):
        self.candidates = candidates   # list of dicts (surface/candidate_id/..)
        summary = ", ".join(
            "%s '%s' (%s)" % (c["surface"], c["summary"], c["candidate_id"])
            for c in candidates)
        super().__init__("ambiguous existing surface(s): %s" % summary)


def _adopt_unmarked_enabled():
    """Config gate MIRROR_ADOPT_UNMARKED (default false). Onboarding sets it
    true after the user explicitly chooses to adopt an existing same-name
    surface. Only 'true'/'1'/'yes' (case-insensitive) enable adoption."""
    val = (_load_conf().get("MIRROR_ADOPT_UNMARKED") or "").strip().lower()
    return val in ("true", "1", "yes", "on")


def bootstrap(conn):
    """Resolve (or create) the agent's calendar + tasklist, return (cal_id,
    tl_id). Adopt-or-create policy (DGN-268 S2): a marker match is ours ->
    adopt; a bare summary/title match WITHOUT our marker is ambiguous ->
    NEVER auto-adopt or write into it unless MIRROR_ADOPT_UNMARKED=true (then
    adopt + stamp the marker so the next run is unambiguous); no match ->
    create + stamp. Ambiguous surfaces with the gate off collect into a
    single BootstrapAmbiguous signal (no state mutation, no inserts) for the
    onboarding layer to resolve."""
    cal_id = get_state(conn, "agent_calendar_id")
    tl_id = get_state(conn, "agent_tasklist_id")
    cal_marker = _frozen_cal_marker(conn)
    adopt_unmarked = _adopt_unmarked_enabled()

    if cal_id:
        try:
            gws("calendar", "calendars", "get",
                "--params", json.dumps({"calendarId": cal_id}))
        except Exception:
            cal_id = None
    if tl_id:
        try:
            gws("tasks", "tasklists", "get",
                "--params", json.dumps({"tasklist": tl_id}))
        except Exception:
            tl_id = None

    # --- Phase 1: resolve without mutating state. Decide per surface whether
    # it is already known, adoptable (marker or gated summary), ambiguous, or
    # to-be-created. Nothing is inserted/adopted until all ambiguity is clear.
    ambiguous = []
    cal_action = tl_action = None   # ("adopt", id) | ("create", None) | None

    if not cal_id:
        # m12: primary re-discovery key = description marker (survives rename);
        # a bare summary match is NOT trusted (S2: could be a foreign cal).
        cal_list = gws("calendar", "calendarList", "list")
        marker_hit = summary_hit = None
        for item in cal_list.get("items", []):
            if cal_marker in (item.get("description") or ""):
                marker_hit = item["id"]
                break
            if item.get("summary") == SANDBOX_CAL_SUMMARY:
                summary_hit = item["id"]
        if marker_hit:
            cal_action = ("adopt", marker_hit)
        elif summary_hit and adopt_unmarked:
            cal_action = ("adopt_unmarked", summary_hit)
        elif summary_hit:
            ambiguous.append({"surface": "calendar",
                              "candidate_id": summary_hit,
                              "summary": SANDBOX_CAL_SUMMARY})
        else:
            cal_action = ("create", None)

    if not tl_id:
        # Tasklist has no description field -> title match is the ONLY
        # re-discovery key. That makes a bare title collision even more
        # likely to be foreign, so the same S2 guard applies.
        tl_list = gws("tasks", "tasklists", "list")
        title_hit = None
        for item in tl_list.get("items", []):
            if item.get("title") == SANDBOX_TASKLIST_TITLE:
                title_hit = item["id"]
                break
        if title_hit and adopt_unmarked:
            tl_action = ("adopt_unmarked", title_hit)
        elif title_hit:
            ambiguous.append({"surface": "tasklist",
                              "candidate_id": title_hit,
                              "summary": SANDBOX_TASKLIST_TITLE})
        elif not title_hit:
            tl_action = ("create", None)

    if ambiguous:
        # Guard off + a foreign same-name surface present: signal, do NOT
        # create or adopt anything this run (no partial bootstrap).
        raise BootstrapAmbiguous(ambiguous)

    # --- Phase 2: commit the resolved actions (all unambiguous now). ---
    if cal_action is not None:
        verb, hit = cal_action
        if verb == "adopt":
            cal_id = hit
            print("[bootstrap] Re-discovered calendar (marker): %s" % cal_id)
        elif verb == "adopt_unmarked":
            cal_id = hit
            # Stamp our marker into the description so the next run is
            # unambiguous (marker match, no longer summary-only).
            _stamp_calendar_marker(cal_id, cal_marker)
            print("[bootstrap] Adopted unmarked calendar + stamped: %s" % cal_id)
        else:  # create
            r = gws("calendar", "calendars", "insert", body={
                "summary": SANDBOX_CAL_SUMMARY,
                "description": "%s -- %s" % (cal_marker, CAL_DESCRIPTION_TEXT),
                "timeZone": DISPLAY_TZ_NAME})
            cal_id = r["id"]
            print("[bootstrap] Created calendar: %s" % cal_id)
    set_state(conn, "agent_calendar_id", cal_id)

    if tl_action is not None:
        verb, hit = tl_action
        if verb == "adopt_unmarked":
            tl_id = hit
            print("[bootstrap] Adopted unmarked tasklist: %s" % tl_id)
        else:  # create
            r = gws("tasks", "tasklists", "insert",
                    body={"title": SANDBOX_TASKLIST_TITLE})
            tl_id = r["id"]
            print("[bootstrap] Created tasklist: %s" % tl_id)
    set_state(conn, "agent_tasklist_id", tl_id)
    return cal_id, tl_id


def _stamp_calendar_marker(cal_id, cal_marker):
    """Adopt-unmarked: write our marker into an existing calendar's
    description so future bootstraps re-discover it by marker (unambiguous),
    not by the weaker summary match. Preserves any existing description text
    the user had, appending our marker line if absent."""
    try:
        cur = gws("calendar", "calendars", "get",
                  "--params", json.dumps({"calendarId": cal_id}))
    except Exception:
        cur = {}
    existing = (cur.get("description") or "").strip()
    if cal_marker in existing:
        return
    marker_line = "%s -- %s" % (cal_marker, CAL_DESCRIPTION_TEXT)
    new_desc = (existing + "\n" + marker_line) if existing else marker_line
    gws("calendar", "calendars", "patch",
        "--params", json.dumps({"calendarId": cal_id}),
        body={"description": new_desc})


# ---------------------------------------------------------------------------
# Sentinel helpers (m17: non-anchored strip)
# ---------------------------------------------------------------------------

def _sentinel_str(ulid):
    return "\n[ulid:%s]" % ulid

def _notes_with_sentinel(note, ulid):
    base = note or ""
    sentinel = _sentinel_str(ulid)
    if sentinel in base:
        return base
    return base + sentinel

def _strip_sentinel(notes):
    """Remove sentinel tag(s) wherever they appear (m17: user may have typed
    text below the sentinel -- do not require end-of-string)."""
    if not notes:
        return ""
    return _SENTINEL_RE.sub("", notes)


# ---------------------------------------------------------------------------
# Surface id bookkeeping (M10 generation ids)
# ---------------------------------------------------------------------------

def _cal_surface_id(state_conn, ulid, src_conn=None):
    """Active Calendar surface id: promoted bookkeeping (W6) or hex(ulid)."""
    return bk_get(state_conn, src_conn, ulid, "gcal_event_id") or ulid_to_hex(ulid)


def _next_generation_id(state_conn, ulid, src_conn=None):
    """M10: retire the dead surface id, mint hex+'g'+N (charset stays inside
    base32hex 0-9 a-v; extProps still carries the true ulid). Generation
    counter stays in state KV (internal); the ACTIVE id lives in the W6
    bookkeeping home."""
    gen = int(get_state(state_conn, "gcal_gen:%s" % ulid) or "0") + 1
    set_state(state_conn, "gcal_gen:%s" % ulid, str(gen))
    new_id = "%sg%d" % (ulid_to_hex(ulid), gen)
    bk_set(state_conn, src_conn, ulid, "gcal_event_id", new_id)
    return new_id


# ---------------------------------------------------------------------------
# Outbound: Calendar (W2/W3/W4 + M6 etag + M10)
# ---------------------------------------------------------------------------

def _build_cal_body(event, surface_id):
    gcal_status, color_id = event_to_cal_status(event)
    private_props = {"ulid": event["ulid"], "version": str(event.get("version", 0))}
    if event.get("location_url"):
        private_props["location_url"] = event["location_url"]
    if event.get("purpose"):
        private_props["purpose"] = event["purpose"]
    if event.get("summary"):
        private_props["summary_text"] = event["summary"]
    body = {
        "id": surface_id,
        "summary": event.get("title") or "",
        "description": event.get("note") or "",
        "status": gcal_status,
        "transparency": "opaque" if event.get("slot_exclusive") else "transparent",
        "extendedProperties": {"private": private_props},
        # Notifications stay Telegram-only (W0): native reminders OFF.
        "reminders": {"useDefault": False, "overrides": []},
    }
    if event.get("location"):
        body["location"] = event["location"]
    if color_id:
        body["colorId"] = color_id
    sk = event.get("schedule_kind")
    if sk in ("timed", "all_day") and event.get("start_at"):
        s_obj, e_obj = utc_instant_to_gcal_datetime(
            event["start_at"], event.get("end_at"), sk,
            event.get("display_tz") or DISPLAY_TZ_NAME)
        body["start"] = s_obj
        body["end"] = e_obj
    return body


def push_calendar(event, cal_id, state_conn, src_conn=None):
    """Upsert one event to Calendar.
    M6 etag guard (compare-then-put emulation; gws cannot send If-Match):
      get current item -> if etag != stored etag AND a snapshot exists,
      run the 3-way merge FIRST (user's surface edits land in sqlite),
      reload the event, then push.
    M10: insert 409 -> revive update -> second 404 -> generation id."""
    ulid = event["ulid"]
    surface_id = _cal_surface_id(state_conn, ulid, src_conn)

    current = None
    try:
        current = gws("calendar", "events", "get", "--params",
                      json.dumps({"calendarId": cal_id, "eventId": surface_id}))
    except GwsError as e:
        if e.http_code != 404:
            raise

    if current is not None and current.get("status") != "cancelled":
        stored_etag = bk_get(state_conn, src_conn, ulid, "gcal_etag")
        if (stored_etag and current.get("etag") != stored_etag
                and src_conn is not None):
            # Surface changed since our last push -> merge before overwriting.
            _apply_calendar_3way(current, state_conn, src_conn)
            row = src_conn.execute(
                "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
            if row is not None:
                event = dict(row)

    body = _build_cal_body(event, surface_id)
    params_update = {"calendarId": cal_id, "eventId": surface_id}
    params_insert = {"calendarId": cal_id}

    if current is not None:
        # X10b-1 (grill-4 finding 5b): main update lane = direct HTTPS with
        # a REAL If-Match on the freshest known etag. 412 -> pull-first
        # 3-way merge -> refetch -> single retry. Retires the get->put race.
        status, resp = _direct(http_direct.cal_update_ifmatch,
                               cal_id, surface_id, body, current.get("etag"))
        IFMATCH_STATS["puts"] += 1
        if status == 412:
            IFMATCH_STATS["http412"] += 1
            _st, fresh = _direct(http_direct.cal_get, cal_id, surface_id)
            if fresh is not None and src_conn is not None:
                _apply_calendar_3way(fresh, state_conn, src_conn)
                row = src_conn.execute(
                    "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
                if row is not None:
                    event = dict(row)
                body = _build_cal_body(event, surface_id)
            status, resp = _direct(http_direct.cal_update_ifmatch,
                                   cal_id, surface_id, body,
                                   (fresh or {}).get("etag"))
            IFMATCH_STATS["puts"] += 1
            if status == 412:
                raise GwsError("If-Match 412 twice for %s" % surface_id,
                               code=412, reason="preconditionFailed")
    else:
        try:
            resp = gws("calendar", "events", "insert",
                       "--params", json.dumps(params_insert), body=body)
        except GwsError as e:
            if e.http_code != 409:
                raise
            # Tombstone exists -> revive via update (W3).
            try:
                resp = gws("calendar", "events", "update",
                           "--params", json.dumps(params_update),
                           body={**body, "status": "confirmed"})
            except GwsError as e2:
                if e2.http_code != 404:
                    raise
                # M10 dead-end: id purged server-side. Retire + regenerate.
                new_id = _next_generation_id(state_conn, ulid, src_conn)
                mirror_log(state_conn, "surface_id_regenerated", ulid,
                           "old=%s new=%s" % (surface_id, new_id))
                body = _build_cal_body(event, new_id)
                resp = gws("calendar", "events", "insert",
                           "--params", json.dumps(params_insert), body=body)

    if resp.get("etag"):
        bk_set(state_conn, src_conn, ulid, "gcal_etag", resp["etag"])
    return resp


# ---------------------------------------------------------------------------
# Outbound: GTasks (W2b/W8 + M6 etag)
# ---------------------------------------------------------------------------

def _build_task_body(event, ulid):
    proj = tasks_projection_from_event(event)
    body = {"title": proj["title"],
            "notes": _notes_with_sentinel(event.get("note"), ulid),
            "status": proj["status"]}
    if proj["due_date"]:
        body["due"] = "%sT00:00:00.000Z" % proj["due_date"]
    if proj["status"] == "completed" and event.get("settled_at"):
        body["completed"] = event["settled_at"].replace("Z", ".000Z")
    return body


def push_tasks(event, tl_id, state_conn, src_conn=None):
    ulid = event["ulid"]

    # grill-4 finding 4 (W2b): abandoned task -> surface delete (symmetric
    # with calendar cancelled). Never insert/resurrect an abandoned task.
    if (event.get("settled_outcome") == "abandoned"
            or event.get("status") == "abandoned"):
        gtask_id = bk_get(state_conn, src_conn, ulid, "gtask_id")
        if not gtask_id:
            gtask_id = _find_task_by_sentinel(ulid, tl_id)  # crash-window
        if gtask_id:
            try:
                gws_delete("tasks", "tasks", "delete", "--params",
                           json.dumps({"tasklist": tl_id, "task": gtask_id}))
            except GwsError as e:
                if e.http_code != 404:
                    raise
        bk_set(state_conn, src_conn, ulid, "gtask_id", "")
        bk_set(state_conn, src_conn, ulid, "gtask_etag", "")
        return {"deleted_abandoned": True}

    body = _build_task_body(event, ulid)
    gtask_id = bk_get(state_conn, src_conn, ulid, "gtask_id")
    resp = None

    if gtask_id:
        current = None
        try:
            current = gws("tasks", "tasks", "get", "--params",
                          json.dumps({"tasklist": tl_id, "task": gtask_id}))
        except GwsError as e:
            if e.http_code == 404:
                gtask_id = None
            else:
                raise
        if gtask_id:
            stored_etag = bk_get(state_conn, src_conn, ulid, "gtask_etag")
            if (stored_etag and current is not None
                    and current.get("etag") != stored_etag
                    and src_conn is not None):
                # M6: surface changed since last push -> merge first.
                _apply_tasks_3way(current, tl_id, state_conn, src_conn)
                row = src_conn.execute(
                    "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
                if row is not None:
                    event = dict(row)
                    body = _build_task_body(event, ulid)
            resp = gws("tasks", "tasks", "update", "--params",
                       json.dumps({"tasklist": tl_id, "task": gtask_id}),
                       body={**body, "id": gtask_id})

    if not gtask_id:
        # W8 crash-window dedup: full-tasklist sentinel scan before insert.
        existing_id = _find_task_by_sentinel(ulid, tl_id)
        if existing_id:
            gtask_id = existing_id
            bk_set(state_conn, src_conn, ulid, "gtask_id", gtask_id)
            resp = gws("tasks", "tasks", "update", "--params",
                       json.dumps({"tasklist": tl_id, "task": gtask_id}),
                       body={**body, "id": gtask_id})
        else:
            resp = gws("tasks", "tasks", "insert", "--params",
                       json.dumps({"tasklist": tl_id}), body=body)
            gtask_id = resp.get("id")
            if gtask_id:
                bk_set(state_conn, src_conn, ulid, "gtask_id", gtask_id)

    if resp is not None and resp.get("etag"):
        bk_set(state_conn, src_conn, ulid, "gtask_etag", resp["etag"])
    return resp


def _find_task_by_sentinel(ulid, tl_id):
    """Full-tasklist paged scan for [ulid:XXXX] (W8 crash-window dedup)."""
    sentinel = "[ulid:%s]" % ulid
    page_token = None
    while True:
        params = {"tasklist": tl_id, "showHidden": True, "showCompleted": True,
                  "showDeleted": False, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = gws("tasks", "tasks", "list", "--params", json.dumps(params))
        for task in resp.get("items", []):
            if sentinel in (task.get("notes") or ""):
                return task["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return None


# ---------------------------------------------------------------------------
# sync(ulid): single outbox op (W6)
# ---------------------------------------------------------------------------

def sync_event(event, cal_id, tl_id, state_conn, src_conn=None):
    """1. route  2. upsert to target  3. clean residual on the other surface.
    M9: routine/recurrence rows are refused (not mirror scope)."""
    ulid = event["ulid"]
    if not in_mirror_scope(event):
        mirror_log(state_conn, "routine_skip", ulid,
                   "recurrence_id=%r is_routine=%r"
                   % (event.get("recurrence_id"), event.get("is_routine")))
        return {"ulid": ulid, "target": None, "status": "skipped_routine"}

    target = route_surface(event)
    result = {"ulid": ulid, "target": target, "status": "ok", "error": None}

    if target == "calendar":
        resp = push_calendar(event, cal_id, state_conn, src_conn)
        gtask_id = bk_get(state_conn, src_conn, ulid, "gtask_id")
        if gtask_id:
            # m13: 404 = already gone -> clear bookkeeping either way.
            try:
                gws_delete("tasks", "tasks", "delete", "--params",
                           json.dumps({"tasklist": tl_id, "task": gtask_id}))
            except GwsError as e:
                if e.http_code != 404:
                    result["cleanup_warning"] = str(e)
            bk_set(state_conn, src_conn, ulid, "gtask_id", "")
        result["gcal_id"] = resp.get("id")
        result["etag"] = resp.get("etag")
    else:
        resp = push_tasks(event, tl_id, state_conn, src_conn)
        surface_id = _cal_surface_id(state_conn, ulid, src_conn)
        try:
            existing = gws("calendar", "events", "get", "--params",
                           json.dumps({"calendarId": cal_id, "eventId": surface_id}))
            if existing.get("status") != "cancelled":
                gws("calendar", "events", "update", "--params",
                    json.dumps({"calendarId": cal_id, "eventId": surface_id}),
                    body={**existing, "status": "cancelled"})
        except GwsError:
            pass  # not present -> nothing to clean
        if resp.get("deleted_abandoned"):
            # finding 4: surface task removed -- drop the snapshot so a
            # (spec-illegal) resurrection would be re-derived, not echoed.
            state_conn.execute(
                "DELETE FROM push_snapshot WHERE event_ulid=?", (ulid,))
            state_conn.commit()
            result["status"] = "deleted_abandoned"
            return result
        result["gtask_id"] = resp.get("id")
        result["etag"] = resp.get("etag")

    # grill-4 finding 1: snapshot must reflect what was ACTUALLY pushed.
    # push_calendar/push_tasks may have 3-way-merged surface edits into
    # sqlite first (etag guard) -- re-load the row so the stored projection
    # matches the pushed (merged) state, not the stale caller dict.
    if src_conn is not None:
        fresh = src_conn.execute(
            "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
        if fresh is not None:
            event = dict(fresh)
    proj = (calendar_projection_from_event(event) if target == "calendar"
            else tasks_projection_from_event(event))
    store_push_snapshot(state_conn, ulid, target, proj)
    return result


# ---------------------------------------------------------------------------
# Verb result discipline (grill-4 finding 2) + surface timestamp (finding 6)
# ---------------------------------------------------------------------------

VERB_OK = ("applied", "already_done", "noop")


def _run_verb(fn):
    """Run an sdk_bridge verb. cas_fail -> ONE inline retry (the verb re-reads
    id/version itself, so the retry sees the fresh version). Returns the
    final result string."""
    res = fn()
    if res == "cas_fail":
        res = fn()
    return res


def _run_verb_tuple(fn):
    """Same, for verbs returning (result, overlap_warning)."""
    res, warn = fn()
    if res == "cas_fail":
        res, warn = fn()
    return res, warn


def _canonical_from_rfc3339(ts):
    """RFC3339 (e.g. '2026-07-09T04:04:49.000Z') -> canonical
    'YYYY-MM-DDThh:mm:ssZ' via parse->reformat (NEVER string truncation).
    Returns None for absent/garbage input (caller falls back + logs)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError):
        return None


PLACEHOLDER_SECONDS = 3600  # open-ended outbound end = start + 1h (m14)


def _duration_seconds(start_ts, end_ts):
    """Seconds between two canonical UTC instants."""
    s = datetime.strptime(start_ts, "%Y-%m-%dT%H:%M:%SZ")
    e = datetime.strptime(end_ts, "%Y-%m-%dT%H:%M:%SZ")
    return int((e - s).total_seconds())


# ---------------------------------------------------------------------------
# M5/F1: inbound calendar 3-way apply
# ---------------------------------------------------------------------------

CAL_CONTENT_FIELDS = ("title", "note", "location")
CAL_SCHEDULE_FIELDS = ("start_at", "end_at", "schedule_kind")
CAL_SYSTEM_FIELDS = ("gcal_status", "color_id", "transparency")  # IN-ignored (W2)


def _apply_calendar_3way(item, state_conn, src_conn):
    """Per-field 3-way: snapshot (last reconcile) vs surface vs local sqlite.
    Surface wins ONLY on fields the surface actually changed; conflicting
    same-field local change -> surface wins + audit line (coordinator rule).
    System fields are IN-ignored per W2. Returns action string."""
    ulid = _extract_ulid(item)
    if ulid is None:
        return "foreign_skip"

    surface = calendar_projection_from_item(item)
    snap_hash, snap = load_push_snapshot(state_conn, ulid)
    if snap_hash is not None and projection_hash(surface) == snap_hash:
        return "echo_skip"

    row = src_conn.execute("SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
    if row is None:
        mirror_log(state_conn, "inbound_unknown_ulid", ulid, "calendar item")
        return "unknown_ulid"
    local = calendar_projection_from_event(dict(row))

    content_changes = {}
    schedule_changed = False
    for f in surface:
        if f in CAL_SYSTEM_FIELDS:
            continue
        surf_v = surface.get(f)
        snap_v = snap.get(f) if snap else None
        local_v = local.get(f)
        if surf_v == snap_v:
            continue  # surface did not change this field
        if local_v == surf_v:
            continue  # already in sync locally -- nothing to apply
        if local_v != snap_v and local_v != surf_v:
            mirror_log(state_conn, "conflict_surface_wins", ulid,
                       "field=%s local=%r surface=%r" % (f, local_v, surf_v))
        if f in CAL_CONTENT_FIELDS:
            if f == "location" and row["kind"] == "task":
                # Metal ruling (grill-6 g14): location is not a task column
                # in the 179 meta policy (event_set_meta would ValueError).
                # Explicit drop + audit line, never a crash path.
                mirror_log(state_conn, "field_dropped", ulid,
                           "location edit on task row -- not a task column")
                continue
            content_changes[f] = surf_v
        elif f in CAL_SCHEDULE_FIELDS:
            schedule_changed = True

    applied, failures = [], []
    if content_changes:
        res = _run_verb(lambda: sdk_bridge.content_update(
            src_conn, ulid, content_changes))
        (applied if res in VERB_OK else failures).append("content:%s" % res)
    if schedule_changed:
        new_start, new_end = surface["start_at"], surface["end_at"]
        # v4 ruling 1 (open-ended preservation, grill-4 finding 10 / OQ-1):
        # the outbound placeholder end (start+1h) is a mirror artifact and
        # must NEVER materialize into the SoT. If the local row is open-ended
        # (timed, end_at NULL) and the surface interval is EXACTLY the
        # placeholder duration, the user only dragged the start -> apply the
        # start shift, keep end NULL (open_ended flag preserved by the verb).
        # A differing duration = the user expressed a real end -> materialize
        # it (verb clears open_ended).
        if (row["schedule_kind"] == "timed" and row["end_at"] is None
                and surface["schedule_kind"] == "timed"
                and new_start and new_end
                and _duration_seconds(new_start, new_end) == PLACEHOLDER_SECONDS):
            new_end = None
        res, warning = _run_verb_tuple(lambda: sdk_bridge.bypass_schedule_apply(
            src_conn, ulid, surface["schedule_kind"], new_start, new_end))
        (applied if res in VERB_OK else failures).append("schedule:%s" % res)
        if warning:
            mirror_log(state_conn, "bypass_overlap_notice", ulid, warning)
            _overlap_defer(ulid, warning)  # DGN-333: notify at batch end
        if res in VERB_OK:
            # grill-4 finding 7: schedule edit may flip routing (e.g. timed
            # task dragged to all-day) -> re-converge surfaces via outbox.
            fresh = src_conn.execute(
                "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
            if fresh is not None and route_surface(dict(fresh)) != "calendar":
                outbox_enqueue(state_conn, ulid)
                mirror_log(state_conn, "routing_flip_enqueued", ulid,
                           "calendar -> %s" % route_surface(dict(fresh)))

    if failures:
        # grill-4 finding 2: verb failure is NOT success. No snapshot write
        # (next delivery retries the merge) + audit line.
        mirror_log(state_conn, "verb_failed", ulid,
                   "calendar apply failed=%s applied=%s" % (failures, applied))
        return "apply_failed(%s)" % ",".join(failures)
    if not applied:
        return "no_editable_change"
    # New reconcile point = what the surface holds now.
    store_push_snapshot(state_conn, ulid, "calendar", surface)
    if item.get("etag"):
        bk_set(state_conn, src_conn, ulid, "gcal_etag", item["etag"])
    return "applied(%s)" % ",".join(applied)


# ---------------------------------------------------------------------------
# F2/F3: inbound calendar pull (paged, guarded, circuit-broken)
# ---------------------------------------------------------------------------

NOTIFY_AGG_THRESHOLD = 3  # g5-4: aggregate per-kind per-drain above this


def _adopt_foreign_calendar(item, cal_id, state_conn, src_conn):
    """dec-009: an item WITHOUT our marker but IN the dedicated calendar is
    an owner hand-made entry -> adopt as event_add(kind='appointment') via
    the SDK verb, stamp extProps ulid on the surface object, notify.
    True-foreign guard: no start (schema-unadoptable) -> skip + log."""
    start_obj = item.get("start") or {}
    if not ("date" in start_obj or "dateTime" in start_obj):
        mirror_log(state_conn, "foreign_skip", None,
                   "calendar id=%s (no start -- unadoptable)" % item.get("id"))
        return {"id": item.get("id"), "action": "foreign_skip"}
    if src_conn is None:
        mirror_log(state_conn, "foreign_skip", None,
                   "calendar id=%s (no SoT conn)" % item.get("id"))
        return {"id": item.get("id"), "action": "foreign_skip"}

    title = (item.get("summary") or "").strip() or "(untitled)"
    # g5-... grill-6 g9: recurring events (master rrule or expanded instance)
    # are outside mirror scope until recurrence support (DGN-240) -- skip +
    # one owner notification, never adopt (instance-per-day adoption flood).
    if item.get("recurrence") or item.get("recurringEventId"):
        mirror_log(state_conn, "recurring_skipped", None,
                   "calendar id=%s title=%r" % (item.get("id"), title[:40]))
        notify_mod.notify(state_conn, "recurring_skipped",
                          item.get("recurringEventId") or item.get("id"),
                          title=title)
        return {"id": item.get("id"), "action": "recurring_skipped"}
    sk = "all_day" if "date" in start_obj else "timed"
    s_at, e_at = gcal_datetime_to_utc_instants(
        start_obj, item.get("end") or {})
    overlap_warn = None
    eid = sdk_bridge.ec.event_add(
        src_conn, kind="appointment", title=title, schedule_kind=sk,
        start_at=s_at, end_at=e_at, owning_agent="ag",
        created_by="gcal-inbound-create",
        note=item.get("description") or None)
    if eid is None:
        # FG-1 slot loss: owner reality wins (grill-6 F5 ruling) -- force
        # adopt through the bypass insert lane + overlap notice.
        eid, overlap_warn = sdk_bridge.bypass_event_add(
            src_conn, kind="appointment", title=title, schedule_kind=sk,
            start_at=s_at, end_at=e_at, owning_agent="ag",
            created_by="gcal-inbound-create",
            note=item.get("description") or None)
        mirror_log(state_conn, "adopt_slot_conflict_forced", None,
                   "calendar id=%s title=%r overlap=%s"
                   % (item.get("id"), title[:40], overlap_warn))
    row = src_conn.execute("SELECT * FROM event WHERE id=?", (eid,)).fetchone()
    ulid = row["ulid"]
    # Stamp the surface object so it is ours from now on.
    patched = gws("calendar", "events", "patch", "--params",
                  json.dumps({"calendarId": cal_id, "eventId": item["id"]}),
                  body={"extendedProperties": {"private": {
                      "ulid": ulid, "version": "0"}}})
    bk_set(state_conn, src_conn, ulid, "gcal_event_id", item["id"])
    if patched.get("etag"):
        bk_set(state_conn, src_conn, ulid, "gcal_etag", patched["etag"])
    store_push_snapshot(state_conn, ulid, "calendar",
                        calendar_projection_from_item(patched))
    notify_mod.notify(state_conn, "inbound_adopted", ulid, title=title)
    if overlap_warn:
        _overlap_defer(ulid, overlap_warn)  # DGN-333: notify at batch end
    mirror_log(state_conn, "inbound_adopted", ulid,
               "surface id=%s" % item.get("id"))
    return {"ulid": ulid, "action": "adopted"}


def pull_calendar(cal_id, state_conn, src_conn):
    """Public entry: acquires the single mirror lock shared with the drain
    (grill-4 finding 5a) -- pull and drain never interleave. If the lock is
    held, this poll is skipped (next poll catches up; cursors unmoved)."""
    if not _acquire_drain_lock(state_conn):
        mirror_log(state_conn, "pull_skipped_locked", None, "calendar poll")
        return []
    try:
        return _pull_calendar_locked(cal_id, state_conn, src_conn)
    finally:
        _release_drain_lock(state_conn)


def _pull_calendar_locked(cal_id, state_conn, src_conn):
    """syncToken incremental pull. F2: pageToken loop, nextSyncToken saved
    only after the final page. F3: foreign guard + per-item try/except.
    W10: mass-cancelled circuit breaker (finding 3: on trip the token is NOT
    saved so held deltas redeliver; own cancel echoes excluded from count).
    Finding 13: invalid-token 400 handled like 410 (reset + one retry)."""
    results = []
    items = []
    next_sync_token = None

    for attempt in range(2):
        sync_token = get_state(state_conn, "cal_sync_token")
        items = []
        page_token = None
        try:
            while True:
                # g9: singleEvents/showDeleted MUST match across initial and
                # incremental pulls (recurring representation consistency).
                if sync_token:
                    params = {"calendarId": cal_id, "syncToken": sync_token,
                              "singleEvents": True, "maxResults": 250}
                else:
                    params = {"calendarId": cal_id, "showDeleted": True,
                              "singleEvents": True, "maxResults": 250}
                if page_token:
                    params["pageToken"] = page_token
                resp = gws("calendar", "events", "list",
                           "--params", json.dumps(params))
                items.extend(resp.get("items", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    next_sync_token = resp.get("nextSyncToken")
                    break
            break
        except GwsError as e:
            # 410 GONE or invalid-token 400 -> reset + full resync (once).
            if e.http_code in (410, 400) and sync_token and attempt == 0:
                set_state(state_conn, "cal_sync_token", "")
                continue
            raise

    # W10 circuit breaker: mass-cancelled = likely calendar loss, not intent.
    # Finding 3: our own pushed cancels (snapshot gcal_status=='cancelled')
    # are echoes, not owner actions -- excluded from the count.
    ours_cancelled = []
    for i in items:
        if i.get("status") != "cancelled":
            continue
        u = _extract_ulid(i)
        if not u:
            continue
        _h, snap = load_push_snapshot(state_conn, u)
        if snap.get("gcal_status") == "cancelled":
            continue  # own cancel echo
        ours_cancelled.append(u)
    breaker_tripped = len(ours_cancelled) > CB_CANCELLED_THRESHOLD
    if breaker_tripped:
        mirror_log(state_conn, "circuit_breaker_tripped", None,
                   "cancelled=%d threshold=%d -- cancelled processing halted, "
                   "syncToken withheld (deltas redeliver), owner notification "
                   "required" % (len(ours_cancelled), CB_CANCELLED_THRESHOLD))
        notify_mod.notify(state_conn, "circuit_breaker", None,
                          count=len(ours_cancelled))

    for item in items:
        try:
            ulid = _extract_ulid(item)
            if ulid is None:
                if item.get("status") == "cancelled":
                    # foreign tombstone: nothing to adopt
                    results.append({"id": item.get("id"),
                                    "action": "foreign_skip"})
                    continue
                results.append(_adopt_foreign_calendar(
                    item, cal_id, state_conn, src_conn))
                continue
            if item.get("status") == "cancelled":
                # dec-011: an owner-deleted LIVE calendar-routed event is
                # cancelled in the SoT (abandoned) + one confirm notification.
                # Held under breaker trip; system tombstones (flip residue,
                # our own cancel echoes) stay no-op.
                action = ("cancelled_held_breaker" if breaker_tripped
                          else "cancelled_noted")
                if action == "cancelled_noted" and src_conn is not None:
                    row = src_conn.execute(
                        "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
                    _h2, snapc = load_push_snapshot(state_conn, ulid)
                    if (row is not None and row["settled_outcome"] is None
                            and route_surface(dict(row)) == "calendar"
                            and snapc.get("gcal_status") != "cancelled"):
                        res = _run_verb(lambda: sdk_bridge.cancel_abandoned(
                            src_conn, ulid, "gcal-owner-delete"))
                        if res in VERB_OK:
                            action = "cancelled_abandoned"
                            store_push_snapshot(
                                state_conn, ulid, "calendar",
                                calendar_projection_from_item(item))
                            notify_mod.notify(state_conn, "inbound_cancel",
                                              ulid, title=row["title"])
                        else:
                            mirror_log(state_conn, "verb_failed", ulid,
                                       "inbound cancel apply=%s" % res)
                results.append({"ulid": ulid, "action": action})
                continue
            action = _apply_calendar_3way(item, state_conn, src_conn)
            results.append({"ulid": ulid, "action": action})
        except Exception as e:  # F3: one bad item never kills the poll
            mirror_log(state_conn, "inbound_item_error", None,
                       "calendar id=%s err=%s" % (item.get("id", "?"), e))
            results.append({"id": item.get("id"), "action": "item_error",
                            "error": str(e)})

    # Finding 3: on breaker trip the token is NOT advanced -- the held
    # cancelled deltas redeliver on the next poll (W12 backstop absent).
    if next_sync_token and not breaker_tripped:
        set_state(state_conn, "cal_sync_token", next_sync_token)
    return results


# ---------------------------------------------------------------------------
# M8/F4/F2: inbound tasks pull + 3-way
# ---------------------------------------------------------------------------

TASK_CONTENT_FIELDS = ("title", "note")


def _apply_tasks_3way(item, tl_id, state_conn, src_conn):
    """M8: consume title/notes/due edits + completed (F4 settle verb) +
    needsAction reopen (unsettle verb). Same 3-way discipline as calendar.
    g5-8: ours-identification = gtask_id bookkeeping column FIRST (real
    tombstones may not preserve notes), sentinel as fallback."""
    notes = item.get("notes", "") or ""
    ulid = None
    tid = item.get("id")
    if tid and src_conn is not None and _bk_cols_available(src_conn):
        r0 = src_conn.execute(
            "SELECT ulid FROM event WHERE gtask_id=?", (tid,)).fetchone()
        if r0 is not None:
            ulid = r0["ulid"]
    if ulid is None:
        m = _SENTINEL_RE.search(notes)
        if not m:
            return "foreign_skip"
        ulid = m.group(1)

    if item.get("deleted"):
        # V3 updatedMin is INCLUSIVE (>=): tombstones redeliver -- id-dedup
        # via updated marker (MG180-8, harness E18).
        marker_key = "deleted_noted:%s" % ulid
        cur = item.get("updated") or "-"
        if get_state(state_conn, marker_key) == cur:
            return "echo_skip"  # tombstone already processed
        row_d = src_conn.execute(
            "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
        if (row_d is not None and row_d["settled_outcome"] is None
                and route_surface(dict(row_d)) == "tasks"):
            # dec-011: owner deleted our live task -> cancel (abandoned) +
            # bookkeeping clear (W2b delete semantics: no resurrection) +
            # one confirm notification. Marker set only on success so a
            # verb failure retries via reconcile owner_deleted backstop.
            res = _run_verb(lambda: sdk_bridge.cancel_abandoned(
                src_conn, ulid, "gtasks-owner-delete"))
            if res in VERB_OK:
                bk_set(state_conn, src_conn, ulid, "gtask_id", "")
                bk_set(state_conn, src_conn, ulid, "gtask_etag", "")
                state_conn.execute(
                    "DELETE FROM push_snapshot WHERE event_ulid=?", (ulid,))
                state_conn.commit()
                set_state(state_conn, marker_key, cur)
                mirror_log(state_conn, "task_deleted_abandoned", ulid,
                           "dec-011 applied (gtasks-owner-delete)")
                notify_mod.notify(state_conn, "task_deleted", ulid,
                                  title=row_d["title"])
                return "deleted_abandoned_applied"
            mirror_log(state_conn, "verb_failed", ulid,
                       "dec-011 cancel apply=%s" % res)
            return "apply_failed(cancel:%s)" % res
        # settled / non-tasks-routed / unknown: note once, no action.
        set_state(state_conn, marker_key, cur)
        mirror_log(state_conn, "task_deleted_noted", ulid,
                   "tombstone noted (system/settled row -- no action)")
        return "deleted_noted"

    surface = tasks_projection_from_item(item)
    snap_hash, snap = load_push_snapshot(state_conn, ulid)
    if snap_hash is not None and projection_hash(surface) == snap_hash:
        return "echo_skip"

    row = src_conn.execute("SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
    if row is None:
        mirror_log(state_conn, "inbound_unknown_ulid", ulid, "tasks item")
        return "unknown_ulid"
    event = dict(row)
    local = tasks_projection_from_event(event)

    applied, failures = [], []
    content_changes = {}
    for f in surface:
        surf_v = surface.get(f)
        snap_v = snap.get(f) if snap else None
        local_v = local.get(f)
        if surf_v == snap_v:
            continue
        if local_v == surf_v:
            continue  # already in sync locally -- nothing to apply
        if local_v != snap_v and local_v != surf_v:
            mirror_log(state_conn, "conflict_surface_wins", ulid,
                       "field=%s local=%r surface=%r" % (f, local_v, surf_v))
        if f in TASK_CONTENT_FIELDS:
            content_changes[f] = surf_v
        elif f == "due_date":
            if surf_v is None:
                res, warn = _run_verb_tuple(lambda: sdk_bridge.bypass_schedule_apply(
                    src_conn, ulid, "untimed", None, None))
                if res == "rejected_untimed":
                    # DGN-240 T12 (spec 5.5, M-C): a routine instance never
                    # goes untimed -- classified TERMINAL result, not a verb
                    # failure. (a) audit line; (b) bookkeeping advances:
                    # applied-class, so the end-of-function snapshot records
                    # the current surface projection (no 5-min re-fire);
                    # (c) re-echo: SoT keeps its due, so the next outbound
                    # drain restores due on the surface ("this edit is
                    # void" expressed by surface restoration).
                    mirror_log(state_conn, "routine_untimed_rejected", ulid,
                               "due-clear inbound on routine instance")
                    outbox_enqueue(state_conn, ulid)
                    applied.append("due:rejected_untimed")
                    continue
            else:
                s, e = all_day_instants_from_date(
                    surf_v, event.get("display_tz") or DISPLAY_TZ_NAME)
                res, warn = _run_verb_tuple(lambda: sdk_bridge.bypass_schedule_apply(
                    src_conn, ulid, "all_day", s, e))
            (applied if res in VERB_OK else failures).append("due:%s" % res)
            if warn:
                mirror_log(state_conn, "bypass_overlap_notice", ulid, warn)
                _overlap_defer(ulid, warn)  # DGN-333: notify at batch end
            if res in VERB_OK:
                fresh = src_conn.execute(
                    "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
                if fresh is not None and route_surface(dict(fresh)) != "tasks":
                    outbox_enqueue(state_conn, ulid)
                    mirror_log(state_conn, "routing_flip_enqueued", ulid,
                               "tasks -> %s" % route_surface(dict(fresh)))
        elif f == "status":
            if surf_v == "completed":
                # grill-4 finding 6: surface completion time, parse->reformat
                # to canonical Z; absent/garbage -> now_utc fallback + log.
                ts = _canonical_from_rfc3339(item.get("completed"))
                if ts is None:
                    mirror_log(state_conn, "completed_ts_fallback", ulid,
                               "completed=%r -> now_utc" % item.get("completed"))
                res = _run_verb(lambda: sdk_bridge.settle_done(
                    src_conn, ulid, "gtasks-inbound", ts))
                (applied if res in VERB_OK else failures).append("settle:%s" % res)
            else:  # needsAction reopen (M8, DGN-180 unsettle verb)
                res, warn = _run_verb_tuple(lambda: sdk_bridge.unsettle(
                    src_conn, ulid, "gtasks-inbound"))
                (applied if res in VERB_OK else failures).append("unsettle:%s" % res)
                if warn:
                    mirror_log(state_conn, "bypass_overlap_notice", ulid, warn)
                    _overlap_defer(ulid, warn)  # DGN-333: notify at batch end

    if content_changes:
        res = _run_verb(lambda: sdk_bridge.content_update(
            src_conn, ulid, content_changes))
        (applied if res in VERB_OK else failures).append("content:%s" % res)

    if failures:
        # grill-4 finding 2: NO snapshot write on failure + audit line.
        mirror_log(state_conn, "verb_failed", ulid,
                   "tasks apply failed=%s applied=%s" % (failures, applied))
        return "apply_failed(%s)" % ",".join(failures)
    if not applied:
        return "no_editable_change"
    store_push_snapshot(state_conn, ulid, "tasks", surface)
    if item.get("etag"):
        bk_set(state_conn, src_conn, ulid, "gtask_etag", item["etag"])
    return "applied(%s)" % ",".join(applied)


def pull_tasks(tl_id, state_conn, src_conn):
    """Public entry: single mirror lock shared with drain (finding 5a)."""
    if not _acquire_drain_lock(state_conn):
        mirror_log(state_conn, "pull_skipped_locked", None, "tasks poll")
        return []
    try:
        return _pull_tasks_locked(tl_id, state_conn, src_conn)
    finally:
        _release_drain_lock(state_conn)


def _pull_tasks_locked(tl_id, state_conn, src_conn):
    """updatedMin watermark pull. F2: pageToken loop, watermark advances only
    after the FULL traversal completes. F3-style per-item try/except."""
    watermark = get_state(state_conn, "tasks_updated_watermark")
    results = []
    items = []
    page_token = None

    while True:
        params = {"tasklist": tl_id, "showHidden": True, "showCompleted": True,
                  "showDeleted": True, "maxResults": 100}
        if watermark:
            params["updatedMin"] = watermark
        if page_token:
            params["pageToken"] = page_token
        resp = gws("tasks", "tasks", "list", "--params", json.dumps(params))
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    max_updated = watermark
    for item in items:
        try:
            action = _apply_tasks_3way(item, tl_id, state_conn, src_conn)
            results.append({"id": item.get("id"), "action": action})
        except Exception as e:  # per-item isolation
            mirror_log(state_conn, "inbound_item_error", None,
                       "tasks id=%s err=%s" % (item.get("id", "?"), e))
            results.append({"id": item.get("id"), "action": "item_error",
                            "error": str(e)})
        updated = item.get("updated", "")
        if updated and updated > (max_updated or ""):
            max_updated = updated

    # F2: watermark advances only here, after the full traversal. A poll-level
    # exception above never reaches this line -> watermark preserved.
    if max_updated and max_updated != watermark:
        set_state(state_conn, "tasks_updated_watermark", max_updated)
    return results


# ---------------------------------------------------------------------------
# M7: mirror_outbox drain worker
# ---------------------------------------------------------------------------

def outbox_enqueue(state_conn, event_ulid):
    """Idempotent enqueue (unique pending index collapses duplicates)."""
    now = _now_iso()
    try:
        state_conn.execute(
            "INSERT INTO mirror_outbox(event_ulid, op, status, created_at, updated_at) "
            "VALUES(?, 'sync', 'queued', ?, ?)", (event_ulid, now, now))
        state_conn.commit()
        return True
    except sqlite3.IntegrityError:
        state_conn.rollback()
        return False  # already pending


def outbox_backfill(state_conn, src_conn):
    """Resumable backfill: enqueue every in-scope event. Idempotent re-run
    (pending dedup via unique index; already-pushed rows converge on sync)."""
    count = 0
    for row in src_conn.execute("SELECT * FROM event").fetchall():
        ev = dict(row)
        if not in_mirror_scope(ev):
            continue
        if outbox_enqueue(state_conn, ev["ulid"]):
            count += 1
    return count


def _acquire_drain_lock(state_conn, lease_seconds=OUTBOX_LEASE_SECONDS):
    """Single-flight lock (MG180-9): mirror_state row with pid+expiry."""
    now = time.time()
    state_conn.execute("BEGIN IMMEDIATE")
    try:
        row = state_conn.execute(
            "SELECT value FROM mirror_state WHERE key='drain_lock'").fetchone()
        if row:
            try:
                lock = json.loads(row["value"])
                if lock.get("expires", 0) > now:
                    state_conn.rollback()
                    return False
            except Exception:
                pass
        state_conn.execute(
            "INSERT INTO mirror_state(key, value) VALUES('drain_lock', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"pid": os.getpid(), "expires": now + lease_seconds}),))
        state_conn.commit()
        return True
    except Exception:
        state_conn.rollback()
        raise


def _release_drain_lock(state_conn):
    state_conn.execute("DELETE FROM mirror_state WHERE key='drain_lock'")
    state_conn.commit()


def outbox_drain(state_conn, src_conn, cal_id, tl_id, max_items=50,
                 backoff_base=1.0, sleep_fn=time.sleep):
    """Drain the outbox: claim -> sync -> pushed | retry(backoff) | failed.
    - claim/lease: stale claims (lease expired) are reclaimed.
    - 403/429/5xx -> exponential backoff (base * 2^attempts), bounded by
      OUTBOX_MAX_ATTEMPTS, then failed + exhaustion log (owner warning stub).
    - single-flight via drain_lock (MG180-9).
    Returns summary dict."""
    if not _acquire_drain_lock(state_conn):
        return {"status": "locked", "pushed": 0, "retried": 0, "failed": 0}

    pushed = retried = failed = 0
    exhausted_rows = []  # g5-4: per-drain aggregation buffer
    try:
        now = _now_iso()
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=OUTBOX_LEASE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state_conn.execute(
            "UPDATE mirror_outbox SET status='claimed', lease_at=?, updated_at=? "
            "WHERE id IN (SELECT id FROM mirror_outbox WHERE status='queued' "
            "OR (status='claimed' AND lease_at < ?) ORDER BY id LIMIT ?)",
            (now, now, stale, max_items))
        state_conn.commit()

        rows = state_conn.execute(
            "SELECT * FROM mirror_outbox WHERE status='claimed' AND lease_at=? "
            "ORDER BY id", (now,)).fetchall()

        for row in rows:
            ulid = row["event_ulid"]
            attempts = row["attempts"]
            ev_row = src_conn.execute(
                "SELECT * FROM event WHERE ulid=?", (ulid,)).fetchone()
            if ev_row is None:
                state_conn.execute(
                    "UPDATE mirror_outbox SET status='failed', last_error=?, "
                    "updated_at=? WHERE id=?",
                    ("event row not found", _now_iso(), row["id"]))
                state_conn.commit()
                failed += 1
                continue
            try:
                sync_event(dict(ev_row), cal_id, tl_id, state_conn, src_conn)
                state_conn.execute(
                    "UPDATE mirror_outbox SET status='pushed', updated_at=? "
                    "WHERE id=?", (_now_iso(), row["id"]))
                state_conn.commit()
                pushed += 1
            except GwsError as e:
                attempts += 1
                if e.http_code in RETRYABLE_HTTP and attempts < OUTBOX_MAX_ATTEMPTS:
                    sleep_fn(backoff_base * (2 ** (attempts - 1)))
                    state_conn.execute(
                        "UPDATE mirror_outbox SET status='queued', attempts=?, "
                        "last_error=?, updated_at=? WHERE id=?",
                        (attempts, str(e), _now_iso(), row["id"]))
                    state_conn.commit()
                    retried += 1
                else:
                    state_conn.execute(
                        "UPDATE mirror_outbox SET status='failed', attempts=?, "
                        "last_error=?, updated_at=? WHERE id=?",
                        (attempts, str(e), _now_iso(), row["id"]))
                    state_conn.commit()
                    mirror_log(state_conn, "outbox_exhausted", ulid,
                               "attempts=%d err=%s -- owner warning required"
                               % (attempts, e))
                    exhausted_rows.append(
                        (ulid, _title_of(src_conn, ulid)))
                    failed += 1
            except Exception as e:
                state_conn.execute(
                    "UPDATE mirror_outbox SET status='failed', attempts=?, "
                    "last_error=?, updated_at=? WHERE id=?",
                    (attempts + 1, str(e), _now_iso(), row["id"]))
                state_conn.commit()
                failed += 1
    finally:
        _release_drain_lock(state_conn)

    # g5-4: per-kind per-drain aggregation -- above the threshold a single
    # aggregate message replaces the 1:1 flood.
    if exhausted_rows:
        if len(exhausted_rows) > NOTIFY_AGG_THRESHOLD:
            notify_mod.notify(state_conn, "outbox_exhausted_agg", None,
                              dedup=False, count=len(exhausted_rows))
        else:
            for u, t in exhausted_rows:
                notify_mod.notify(state_conn, "outbox_exhausted", u, title=t)

    return {"status": "ok", "pushed": pushed, "retried": retried, "failed": failed}


# ---------------------------------------------------------------------------
# X10b-3: time-based status sweep (V5 backbone / grill-4 finding 9).
# Metal ruling: the adapter poller owns the sweep step.
# ---------------------------------------------------------------------------

SWEEP_GRACE_SECONDS = 0  # dec-010 (owner ruling): grace = 0 -- expiry
# materializes immediately at effective end (exclusive-end semantics match
# the 179 predicate; no deferral window).


def sweep_step(state_conn, src_conn):
    """Materialize time-based status transitions on open rows with an
    elapsed deadline (idempotent; runs first in every poller cycle):
      - appointment + summary text present -> settle done (W5 promotion:
        summary = owner's active assert; time-gate already passed here).
      - otherwise -> recompute derived status (expired materialization).
    Changed rows are enqueued for surface re-push. Returns change list."""
    now = (datetime.now(timezone.utc)
           - timedelta(seconds=SWEEP_GRACE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    changed = []
    # DGN-240 T2 (spec 2.3, OQ-3): expired-task rows are terminal for this
    # sweep -- every escape from 'expired' goes through a verb inline
    # recompute (derive_status priority), so re-scanning them each poll is
    # pure waste on a monotonically growing set. Appointments stay in
    # (summary-done late promotion, OQ-3 carve-out).
    rows = src_conn.execute(
        "SELECT * FROM event WHERE settled_at IS NULL "
        "AND start_at IS NOT NULL "
        "AND NOT (kind='task' AND status='expired')").fetchall()
    for r in rows:
        ev = dict(r)
        if not in_mirror_scope(ev):
            # DGN-240 T1 landed: roller instances are now IN scope, so their
            # expiry IS materialized here (spec 2.3 sweep integration; the
            # grill-5 finding-10 revisit note is resolved). This skip now
            # excludes only notion-import history + anchor-less belt rows.
            continue
        eff_end = sdk_bridge.ec._cand_eff_end(
            ev["start_at"], ev["end_at"], ev["open_ended"])
        if eff_end == sdk_bridge.ec.INF_STR or now < eff_end:
            continue  # not elapsed (open-ended never expires by time)
        if ev["kind"] == "appointment" and (ev.get("summary") or "").strip():
            res = _run_verb(lambda: sdk_bridge.settle_done(
                src_conn, ev["ulid"], "sweep-summary-done"))
            if res == "applied":
                changed.append((ev["ulid"], "done"))
                outbox_enqueue(state_conn, ev["ulid"])
        else:
            # grill-5 finding 5: derive Python-side FIRST; open a write txn
            # only when the cached status actually differs. Otherwise the
            # monotonically growing expired backlog would take a no-op write
            # lock per row per poll on the live DB.
            live = [x[0] for x in src_conn.execute(
                "SELECT done FROM sub_event WHERE event_id=? AND tombstone=0",
                (ev["id"],)).fetchall()]
            expected = sdk_bridge.ec.derive_status(
                ev["schedule_kind"], ev["start_at"], ev["end_at"],
                ev["open_ended"], ev["settled_at"], ev["settled_outcome"],
                live, ev["completion_rule"], now=now)
            if expected == ev["status"]:
                continue  # cache already correct -- zero write txns
            new_status = sdk_bridge.recompute(src_conn, ev["ulid"])
            if new_status is not None and new_status != ev["status"]:
                changed.append((ev["ulid"], new_status))
                outbox_enqueue(state_conn, ev["ulid"])

    # DGN-302: abandoned-transition tombstone scan.
    # The main scan above is gated on settled_at IS NULL, which correctly
    # excludes settled rows. But abandoned rows (settled_outcome='abandoned'
    # OR status='abandoned') may still hold a push_snapshot from when they
    # were last mirrored as 'confirmed'. The cancel push was never enqueued
    # because the settlement happened outside the sweep window. Find every
    # abandoned in-scope row that has a snapshot whose gcal_status is NOT
    # already 'cancelled', and enqueue a sync so push_calendar projects the
    # correct 'cancelled' status. Convergence: on the second sweep the
    # snapshot is updated to gcal_status='cancelled' by the drain, so the
    # hash comparison below finds nothing new and no re-enqueue happens.
    abandoned_rows = src_conn.execute(
        "SELECT * FROM event WHERE "
        "(settled_outcome='abandoned' OR status='abandoned')").fetchall()
    for r in abandoned_rows:
        ev = dict(r)
        if not in_mirror_scope(ev):
            continue
        snap_hash, snap = load_push_snapshot(state_conn, ev["ulid"])
        if snap_hash is None:
            continue  # no snapshot: row was never pushed, nothing to cancel
        if snap.get("gcal_status") == "cancelled":
            continue  # snapshot already reflects cancelled -- converged
        if outbox_enqueue(state_conn, ev["ulid"]):
            changed.append((ev["ulid"], "abandoned"))

    return changed


def _run_cycle_step(out, key, state_conn, fn):
    """DGN-268 S5: run one poll-cycle step under its own exception guard so a
    failure in one step does NOT abort the others. On success the step's normal
    result lands in out[key]; on failure out[key] becomes {"error": "..."} (the
    stdout summary still prints; the failure is visible + counted) and the error
    is logged via mirror_log. Ordering + idempotency are preserved because each
    step is already independent and self-locking (pulls + drain each acquire and
    release the shared mirror lock in their own finally, so a raised step never
    holds the lock hostage). NOTHING propagates -- a transient inbound 404 must
    never crash the cron or block the outbound drain.

    Error texture matches the rest of the module: GwsError carries http_code, so
    we tag transient (RETRYABLE_HTTP or 404 not-found) vs persistent. Persistent
    errors are logged at a distinct category for the weekly reconcile / operator
    to notice; we do NOT invent a new per-cycle notify/alarm (a single transient
    404 must stay silent -- the once/day on-but-unauth warning already owns the
    auth-failure alarm)."""
    try:
        out[key] = fn()
        return True
    except GwsError as e:
        transient = (e.http_code == 404 or e.http_code in RETRYABLE_HTTP)
        category = ("cycle_step_transient" if transient
                    else "cycle_step_persistent")
        mirror_log(state_conn, category, None,
                   "step=%s http=%s: %s" % (key, e.http_code, e))
        out[key] = {"error": str(e), "http_code": e.http_code,
                    "transient": transient}
        return False
    except Exception as e:  # non-GwsError: unexpected, treat as persistent
        mirror_log(state_conn, "cycle_step_persistent", None,
                   "step=%s: %s" % (key, e))
        out[key] = {"error": str(e), "http_code": None, "transient": False}
        return False


def poll_cycle(state_conn, src_conn, cal_id, tl_id):
    """One full poller cycle (the cron target at cutover):
    sweep -> inbound pulls -> outbox drain.

    DGN-268 S5: each step is isolated (see _run_cycle_step). The outbound DRAIN
    always runs even if an inbound pull raised -- the outbound path must never
    be held hostage to an inbound 404 (the 2026-07-12 09:21 live-instance
    starvation: a transient pull_calendar 404 aborted the whole cycle before
    drain could push).
    Ordering (sweep -> pulls -> drain) and idempotency are unchanged; on the
    all-success path the returned dict is byte-identical to the pre-S5 shape."""
    out = {}
    _run_cycle_step(out, "sweep", state_conn,
                    lambda: sweep_step(state_conn, src_conn))
    _run_cycle_step(out, "calendar", state_conn,
                    lambda: pull_calendar(cal_id, state_conn, src_conn))
    _run_cycle_step(out, "tasks", state_conn,
                    lambda: pull_tasks(tl_id, state_conn, src_conn))
    # Drain runs unconditionally -- it is the outbound lane and must not be
    # skipped just because an inbound pull failed above.
    _run_cycle_step(out, "drain", state_conn,
                    lambda: outbox_drain(state_conn, src_conn, cal_id, tl_id))
    # DGN-333 (MAJOR-5 rev): batch-end recheck of deferred overlap notices --
    # only overlaps that survive the whole cycle reach the owner.
    out["overlap_recheck"] = overlap_flush(state_conn, src_conn)
    return out


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup(conn):
    result = {"calendar": None, "tasklist": None}
    cal_id = get_state(conn, "agent_calendar_id")
    tl_id = get_state(conn, "agent_tasklist_id")
    if cal_id:
        try:
            gws_delete("calendar", "calendars", "delete",
                       "--params", json.dumps({"calendarId": cal_id}))
            result["calendar"] = "deleted:%s" % cal_id
        except Exception as e:
            result["calendar"] = "ERROR:%s" % e
    if tl_id:
        try:
            gws_delete("tasks", "tasklists", "delete",
                       "--params", json.dumps({"tasklist": tl_id}))
            result["tasklist"] = "deleted:%s" % tl_id
        except Exception as e:
            result["tasklist"] = "ERROR:%s" % e
    return result


# ---------------------------------------------------------------------------
# Source DB (M11: 179 get_conn discipline via sdk_bridge)
# ---------------------------------------------------------------------------

def get_src_conn() -> sqlite3.Connection:
    return sdk_bridge.get_conn(DB_PATH)


def load_sample_events(src_conn) -> list:
    """~20 sample events covering the routing buckets (all 6 all_day appts in
    live data are done travel containers -- included as such)."""
    buckets = [
        ("appointment", "timed", None, 5),
        ("appointment", "all_day", "done", 2),
        ("task", "timed", None, 5),
        ("task", "all_day", None, 3),
        ("task", "untimed", None, 3),
        ("task", "timed", "done", 1),
        ("appointment", "timed", "done", 1),
    ]
    rows, seen = [], set()
    for kind, sk, outcome, n in buckets:
        if outcome:
            q = ("SELECT * FROM event WHERE kind=? AND schedule_kind=? "
                 "AND settled_outcome=? LIMIT ?")
            r = src_conn.execute(q, (kind, sk, outcome, n)).fetchall()
        else:
            q = ("SELECT * FROM event WHERE kind=? AND schedule_kind=? "
                 "AND settled_outcome IS NULL LIMIT ?")
            r = src_conn.execute(q, (kind, sk, n)).fetchall()
        for row in r:
            d = dict(row)
            if d["ulid"] not in seen and in_mirror_scope(d):
                seen.add(d["ulid"])
                rows.append(d)
    return rows
