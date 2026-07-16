#!/usr/bin/env python3
"""DGN-364 gate: V15 multi-calendar mirror adapter promotion (design v3).

Covers the section-7 test matrix of DGN-364-design-v3-locked.md:
  Fixtures: F-legacy, F-v15, F-empty, F-mixed, F-partial, F-checklist-fb,
            F-half-legacy, F-dupid, F-emptystring, F-v15-nochecklist
            (incl. the R2-6 checklist-only variant).
  Tests:    T-route, T-sync-legacy, T-drift, T-snapshot-unpin, T-isolation,
            T-cursor-reset, T-ensure, T-adopt-crash, T-provision-idempotent,
            T-schema, T-notify-ops (R2-2 prefix assertions), T-script-rail,
            T-cleanup, T-drift-guard.  ALL non-optional.

All fixtures are throwaway DBs (tmp dirs) -- no live state DB is ever
touched. gws / http_direct are monkeypatched; no network.

Run: python3 tests/mirror/test_v15_promotion.py   (exit 0 = pass)
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_MIRROR = os.path.join(REPO_ROOT, "mirror")
UPDATE_SH = os.path.join(REPO_ROOT, "update.sh")
TEMPLATE_ROUTINES = os.path.join(REPO_ROOT, "agents", ".template", "routines")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


# ---------------------------------------------------------------------------
# Scratch instance + adapter import (tests/mirror convention, cf. test_s7)
# ---------------------------------------------------------------------------

def _build_scratch(conf_lines=None, instance_lines=None):
    root = tempfile.mkdtemp(prefix="dgn364-")
    mirror_dir = os.path.join(root, "mirror")
    os.makedirs(mirror_dir, exist_ok=True)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_i18n.py",
              "mirror_state.sql"):
        shutil.copy2(os.path.join(SRC_MIRROR, f), os.path.join(mirror_dir, f))
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
        fh.write("# scratch\n")
        for line in (conf_lines or []):
            fh.write(line + "\n")
    with open(os.path.join(root, ".instance.conf"), "w") as fh:
        fh.write("DOGANY_AGENT_NAME=testbot\n")
        for line in (instance_lines or []):
            fh.write(line + "\n")
    return root


def _import_adapter(root):
    """Import a fresh adapter from the scratch mirror dir with surface deps
    stubbed (no gws / network / real SDK)."""
    for name in ("adapter", "sdk_bridge", "notify", "http_direct",
                 "mirror_i18n", "reconcile"):
        sys.modules.pop(name, None)
    sdk_stub = types.ModuleType("sdk_bridge")
    sdk_stub.ec = types.ModuleType("ec")
    http_stub = types.ModuleType("http_direct")
    http_stub.HttpError = type("HttpError", (Exception,), {})
    notify_stub = types.ModuleType("notify")
    notify_stub.notify = lambda *a, **k: None
    sys.modules["sdk_bridge"] = sdk_stub
    sys.modules["http_direct"] = http_stub
    sys.modules["notify"] = notify_stub
    sys.path.insert(0, os.path.join(root, "mirror"))
    import adapter  # noqa: F401
    mod = sys.modules["adapter"]
    mod._reset_conf_cache()
    return mod


def _open_state(A, root=None):
    """Open a throwaway state DB (file-backed so re-opens see it)."""
    if root is None:
        A.STATE_DB_PATH = ":memory:"
    else:
        A.STATE_DB_PATH = os.path.join(root, "mirror", "mirror_state.db")
    return A.open_state_db()


def _engrave(A, state, **keys):
    for k, v in keys.items():
        A.set_state(state, k, v)


def _log_harness(A):
    logs = []
    A.mirror_log = lambda conn, cat, ulid=None, detail=None: logs.append(
        (cat, ulid, detail))
    return logs


def _open_src_db():
    """Minimal in-memory source (event) DB (cf. test_s7)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE event (
            id              INTEGER PRIMARY KEY,
            ulid            TEXT NOT NULL UNIQUE,
            kind            TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            note            TEXT,
            schedule_kind   TEXT NOT NULL DEFAULT 'timed',
            start_at        TEXT,
            end_at          TEXT,
            display_tz      TEXT NOT NULL DEFAULT 'Asia/Seoul',
            open_ended      INTEGER NOT NULL DEFAULT 0,
            slot_exclusive  INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'open',
            completion_rule TEXT NOT NULL DEFAULT 'all',
            version         INTEGER NOT NULL DEFAULT 0,
            settled_at      TEXT,
            settled_by      TEXT,
            settled_outcome TEXT,
            owning_agent    TEXT NOT NULL DEFAULT 'agent',
            created_by      TEXT NOT NULL DEFAULT 'agent',
            is_routine      INTEGER DEFAULT 0,
            location        TEXT,
            location_url    TEXT,
            purpose         TEXT,
            summary         TEXT,
            recurrence_id   TEXT,
            rec_date        TEXT,
            block_class     TEXT
        )
    """)
    conn.commit()
    return conn


def _event(ulid, kind="appointment", schedule_kind="timed",
           block_class=None, **extra):
    ev = {
        "ulid": ulid, "kind": kind, "schedule_kind": schedule_kind,
        "title": "T", "note": "", "location": None, "location_url": None,
        "purpose": None, "summary": None, "status": "open",
        "settled_outcome": None, "settled_at": None, "version": 0,
        "recurrence_id": None, "is_routine": 0, "slot_exclusive": 0,
        "start_at": "2026-07-10T08:00:00Z", "end_at": "2026-07-10T09:00:00Z",
        "display_tz": "Asia/Seoul", "open_ended": 0,
    }
    if block_class is not None:
        ev["block_class"] = block_class
    ev.update(extra)
    return ev


ULID = "01ABCDEF000000000000000001"


# ---------------------------------------------------------------------------
# Resolver fixtures (F-legacy .. F-v15-nochecklist)
# ---------------------------------------------------------------------------

def test_resolver_fixtures():
    print("(resolver) fixture matrix F-legacy/F-v15/F-empty/F-mixed/"
          "F-partial/F-checklist-fb/F-half-legacy/F-emptystring/"
          "F-v15-nochecklist:")
    root = _build_scratch()
    A = _import_adapter(root)

    # F-legacy
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="C1", agent_tasklist_id="T1",
             cal_sync_token="tok0")
    t = A.get_mirror_targets(state)
    _check("F-legacy mode=legacy", t["mode"] == "legacy", t)
    _check("F-legacy fan-out dict",
           t["cal_ids"] == {"appt": "C1", "task": "C1", "travel": "C1"},
           t["cal_ids"])
    _check("F-legacy checklist=T1", t["checklist_id"] == "T1", t)
    state.close()

    # F-v15
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B", cal_id_travel="C",
             gtasks_checklist_id="T2")
    t = A.get_mirror_targets(state)
    _check("F-v15 mode=multi", t["mode"] == "multi", t)
    _check("F-v15 cal_ids",
           t["cal_ids"] == {"appt": "A", "task": "B", "travel": "C"},
           t["cal_ids"])
    _check("F-v15 checklist=T2", t["checklist_id"] == "T2", t)
    state.close()

    # F-empty
    state = _open_state(A)
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorUnconfigured as e:
        raised = e
    _check("F-empty raises MirrorUnconfigured", raised is not None, "no raise")
    msg = str(raised or "")
    for key in ("cal_id_appt", "cal_id_task", "cal_id_travel",
                "agent_calendar_id", "agent_tasklist_id"):
        _check("F-empty message names %s" % key, key in msg, msg)
    ids = A.get_cal_ids(state)
    _check("F-empty get_cal_ids all-None no raise",
           ids == {"appt": None, "task": None, "travel": None}, ids)
    state.close()

    # F-mixed: ag_-style AND agent_* AND all V15 keys, distinct values.
    state = _open_state(A)
    _engrave(A, state, ag_calendar_id="AGC", ag_tasklist_id="AGT",
             agent_calendar_id="LC", agent_tasklist_id="LT",
             cal_id_appt="A", cal_id_task="B", cal_id_travel="C",
             gtasks_checklist_id="T2")
    t = A.get_mirror_targets(state)
    _check("F-mixed V15 wholesale precedence",
           t["mode"] == "multi"
           and t["cal_ids"] == {"appt": "A", "task": "B", "travel": "C"}
           and t["checklist_id"] == "T2", t)
    scan_ids = [cid for cid, _k in A._calendar_scan_set(t["cal_ids"])]
    _check("F-mixed legacy ids never in scan set",
           "LC" not in scan_ids and "AGC" not in scan_ids, scan_ids)
    drain_routing_ids = list(t["cal_ids"].values())
    _check("F-mixed legacy ids never in drain-routing dict",
           "LC" not in drain_routing_ids and "AGC" not in drain_routing_ids,
           drain_routing_ids)
    state.close()

    # F-partial: appt + task only (+ checklist)
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B",
             gtasks_checklist_id="T2")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorConfigError as e:
        raised = e
    _check("F-partial raises MirrorConfigError", raised is not None, "no raise")
    _check("F-partial provenance=multi",
           getattr(raised, "provenance", None) == "multi", raised)
    _check("F-partial names cal_id_travel",
           "cal_id_travel" in str(raised)
           and "cal_id_travel" in getattr(raised, "missing_keys", ()),
           (str(raised), getattr(raised, "missing_keys", ())))
    state.close()

    # F-checklist-fb: 3 V15 cal keys + agent_tasklist_id, NO gtasks key
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B", cal_id_travel="C",
             agent_tasklist_id="LT")
    t = A.get_mirror_targets(state)
    _check("F-checklist-fb falls back to agent_tasklist_id",
           t["checklist_id"] == "LT", t)
    state.close()

    # F-half-legacy: agent_calendar_id only
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="C1")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorConfigError as e:
        raised = e
    _check("F-half-legacy raises MirrorConfigError(provenance=legacy)",
           raised is not None
           and getattr(raised, "provenance", None) == "legacy", raised)
    _check("F-half-legacy names agent_tasklist_id",
           "agent_tasklist_id" in str(raised)
           and "agent_tasklist_id" in getattr(raised, "missing_keys", ()),
           str(raised))
    state.close()

    # F-emptystring: '' == NOT engraved everywhere.
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorUnconfigured as e:
        raised = e
    _check("F-emptystring appt='' alone -> MirrorUnconfigured",
           raised is not None, "no raise")
    state.close()
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="", cal_id_task="", cal_id_travel="")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorUnconfigured as e:
        raised = e
    _check("F-emptystring all-'' -> MirrorUnconfigured (like F-empty)",
           raised is not None, "no raise")
    state.close()
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="", cal_id_task="B",
             gtasks_checklist_id="T2")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorConfigError as e:
        raised = e
    _check("F-emptystring ''+real-id mix -> MirrorConfigError",
           raised is not None
           and getattr(raised, "provenance", None) == "multi", raised)
    _check("F-emptystring '' never enters a scan set",
           A._calendar_scan_set({"appt": "", "task": "B", "travel": None})
           == [("B", None)],
           A._calendar_scan_set({"appt": "", "task": "B", "travel": None}))
    state.close()

    # F-v15-nochecklist (R2-4): 3 cal keys, NO checklist keys at all.
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B", cal_id_travel="C")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorConfigError as e:
        raised = e
    _check("F-v15-nochecklist -> MirrorConfigError(provenance=multi)",
           raised is not None
           and getattr(raised, "provenance", None) == "multi", raised)
    _check("F-v15-nochecklist names BOTH checklist-chain keys",
           "gtasks_checklist_id" in str(raised)
           and "agent_tasklist_id" in str(raised)
           and set(getattr(raised, "missing_keys", ()))
           == {"gtasks_checklist_id", "agent_tasklist_id"}, str(raised))
    state.close()

    # R2-6 variant (normal/non-optional): ONLY gtasks_checklist_id engraved.
    state = _open_state(A)
    _engrave(A, state, gtasks_checklist_id="T2")
    raised = None
    try:
        A.get_mirror_targets(state)
    except A.MirrorUnconfigured as e:
        raised = e
    _check("R2-6 checklist-only -> MirrorUnconfigured", raised is not None,
           "no raise")
    msg = str(raised or "")
    _check("R2-6 accurate 'calendar routing ids' wording",
           "calendar routing ids" in msg, msg)
    _check("R2-6 never claims 'no mirror surface ids'",
           "no mirror surface ids" not in msg, msg)
    state.close()


# ---------------------------------------------------------------------------
# Scan-set / cursor behavior (F-legacy poll, F-v15 poll, F-dupid)
# ---------------------------------------------------------------------------

class CalListGws(object):
    """Fake gws serving calendar events.list; records (calendarId, syncToken).
    tokens_out: calendarId -> nextSyncToken (absent = no token in response).
    raise_for: calendarId -> GwsError code to raise (always)."""

    def __init__(self, A, tokens_out=None, raise_for=None):
        self.A = A
        self.tokens_out = tokens_out or {}
        self.raise_for = raise_for or {}
        self.pulls = []   # (calendarId, syncToken)

    def __call__(self, *args, **kwargs):
        if args[:3] == ("calendar", "events", "list"):
            params = json.loads(args[args.index("--params") + 1])
            cid = params.get("calendarId")
            self.pulls.append((cid, params.get("syncToken")))
            if cid in self.raise_for:
                raise self.A.GwsError("boom", code=self.raise_for[cid])
            resp = {"items": []}
            if cid in self.tokens_out:
                resp["nextSyncToken"] = self.tokens_out[cid]
            return resp
        raise AssertionError("unexpected gws call: %s" % (args,))


def _stub_cycle_steps(A):
    """Stub the non-calendar steps so poll_cycle runs without src/gws."""
    A.sweep_step = lambda s, src: []
    A.pull_tasks = lambda tl, s, src: []
    A.outbox_drain = lambda s, src, cal, tl: {"pushed": 0, "status": "ok"}


def test_scanset_and_cursors():
    print("(scan-set) F-legacy one plain pull / F-v15 namespaced / F-dupid:")
    # F-legacy poll: ONE pull, cal_key=None, plain cursor consumed + written.
    root = _build_scratch()
    A = _import_adapter(root)
    _log_harness(A)
    _stub_cycle_steps(A)
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="C1", agent_tasklist_id="T1",
             cal_sync_token="tok0")
    t = A.get_mirror_targets(state)
    fake = CalListGws(A, tokens_out={"C1": "tok1"})
    A.gws = fake
    out = A.poll_cycle(state, "SRC", t["cal_ids"], t["checklist_id"])
    _check("F-legacy exactly ONE calendar pull", fake.pulls == [("C1", "tok0")],
           fake.pulls)
    _check("F-legacy plain cursor consumed (tok0) + advanced (tok1)",
           A.get_state(state, "cal_sync_token") == "tok1",
           A.get_state(state, "cal_sync_token"))
    _check("F-legacy no namespaced cursor written",
           A.get_state(state, "cal_sync_token:appt") is None,
           A.get_state(state, "cal_sync_token:appt"))
    _check("F-legacy out keys == legacy shape byte-identical",
           set(out) == {"sweep", "calendar", "tasks", "drain",
                        "overlap_recheck"}, set(out))
    state.close()

    # F-v15 poll: 3 pulls in appt,task,travel order, namespaced cursors.
    root = _build_scratch()
    A = _import_adapter(root)
    _log_harness(A)
    _stub_cycle_steps(A)
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B", cal_id_travel="C",
             gtasks_checklist_id="T2")
    _engrave(A, state, **{"cal_sync_token:appt": "ta",
                          "cal_sync_token:task": "tb",
                          "cal_sync_token:travel": "tc"})
    t = A.get_mirror_targets(state)
    fake = CalListGws(A, tokens_out={"A": "ta2", "B": "tb2", "C": "tc2"})
    A.gws = fake
    out = A.poll_cycle(state, "SRC", t["cal_ids"], t["checklist_id"])
    _check("F-v15 three pulls in appt,task,travel order",
           fake.pulls == [("A", "ta"), ("B", "tb"), ("C", "tc")], fake.pulls)
    _check("F-v15 namespaced cursors advanced",
           A.get_state(state, "cal_sync_token:appt") == "ta2"
           and A.get_state(state, "cal_sync_token:task") == "tb2"
           and A.get_state(state, "cal_sync_token:travel") == "tc2",
           "cursors wrong")
    _check("F-v15 plain cursor untouched",
           A.get_state(state, "cal_sync_token") is None,
           A.get_state(state, "cal_sync_token"))
    _check("F-v15 out keys calendar:appt/task/travel",
           {"calendar:appt", "calendar:task", "calendar:travel"} <= set(out),
           set(out))
    state.close()

    # F-dupid: {appt:X, task:X, travel:Y} -> exactly two pulls,
    # X under cal_key='appt', Y under cal_key='travel'; namespaced only.
    root = _build_scratch()
    A = _import_adapter(root)
    _log_harness(A)
    _stub_cycle_steps(A)
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="X", cal_id_task="X", cal_id_travel="Y",
             gtasks_checklist_id="T2")
    scan = A._calendar_scan_set(A.get_mirror_targets(state)["cal_ids"])
    _check("F-dupid scan set = [(X,'appt'), (Y,'travel')]",
           scan == [("X", "appt"), ("Y", "travel")], scan)
    fake = CalListGws(A, tokens_out={"X": "tx", "Y": "ty"})
    A.gws = fake
    out = A.poll_cycle(state, "SRC",
                       A.get_mirror_targets(state)["cal_ids"], "T2")
    _check("F-dupid exactly two pulls",
           [p[0] for p in fake.pulls] == ["X", "Y"], fake.pulls)
    _check("F-dupid cursors namespaced (appt + travel), no plain write",
           A.get_state(state, "cal_sync_token:appt") == "tx"
           and A.get_state(state, "cal_sync_token:travel") == "ty"
           and A.get_state(state, "cal_sync_token") is None,
           "cursor keys wrong")
    _check("F-dupid out keys calendar:appt + calendar:travel only",
           "calendar:appt" in out and "calendar:travel" in out
           and "calendar" not in out and "calendar:task" not in out,
           set(out))
    state.close()

    # F-v15 drain pass-through: outbox_drain hands the DICT to sync_event.
    root = _build_scratch()
    A = _import_adapter(root)
    _log_harness(A)
    state = _open_state(A)
    src = _open_src_db()
    src.execute("INSERT INTO event (ulid, kind, title, start_at, end_at) "
                "VALUES (?, 'appointment', 'T', '2026-07-10T08:00:00Z', "
                "'2026-07-10T09:00:00Z')", (ULID,))
    src.commit()
    A.outbox_enqueue(state, ULID)
    seen = []
    A.sync_event = lambda ev, cal_ids, tl, s, sc=None: seen.append(cal_ids)
    cal_ids = {"appt": "A", "task": "B", "travel": "C"}
    A.outbox_drain(state, src, cal_ids, "T2")
    _check("drain passes the cal_ids dict through to sync_event",
           seen == [cal_ids], seen)
    state.close()
    src.close()


# ---------------------------------------------------------------------------
# T-route
# ---------------------------------------------------------------------------

def test_route():
    print("(T-route) route_cal_key kind/block_class matrix:")
    root = _build_scratch()
    A = _import_adapter(root)
    _check("appointment -> appt",
           A.route_cal_key(_event("u", kind="appointment")) == "appt", "")
    _check("task + block_class=travel -> travel",
           A.route_cal_key(_event("u", kind="task",
                                  block_class="travel")) == "travel", "")
    _check("task + block_class=None -> task",
           A.route_cal_key(_event("u", kind="task",
                                  block_class=None)) == "task", "")
    # A5: block_class column absent from older schemas -> .get() None -> task.
    ev = _event("u", kind="task")
    ev.pop("block_class", None)
    _check("task + MISSING block_class column -> task (A5)",
           A.route_cal_key(ev) == "task", "")
    _check("route() untimed task -> ('tasks','checklist')",
           A.route(_event("u", kind="task", schedule_kind="untimed"))
           == ("tasks", "checklist"), "")
    _check("route() appointment -> ('calendar','appt')",
           A.route(_event("u", kind="appointment")) == ("calendar", "appt"),
           "")


# ---------------------------------------------------------------------------
# T-sync-legacy (string shim equivalence)
# ---------------------------------------------------------------------------

def _spy_push_env(A):
    """Neutralize surface calls for sync_event runs; record push targets."""
    pushes = []

    def _push_cal(event, cal_id, state_conn, src_conn=None):
        pushes.append(cal_id)
        return {"id": "GID", "etag": "E"}
    A.push_calendar = _push_cal
    A.push_tasks = lambda ev, tl, s, sc=None: {"id": "TID", "etag": "E"}
    A.gws = lambda *a, **k: (_ for _ in ()).throw(
        A.GwsError("not found", code=404))
    A.gws_delete = lambda *a, **k: True
    return pushes


def test_sync_legacy_shim():
    print("(T-sync-legacy) string cal_ids == dict-of-one-id results:")
    results = []
    for cal_arg in ("C1", {"appt": "C1", "task": "C1", "travel": "C1"}):
        root = _build_scratch()
        A = _import_adapter(root)
        _log_harness(A)
        state = _open_state(A)
        pushes = _spy_push_env(A)
        r = A.sync_event(_event(ULID), cal_arg, "TL", state, None)
        results.append((r, list(pushes)))
        state.close()
    r_str, p_str = results[0]
    r_dict, p_dict = results[1]
    _check("push target identical (C1)", p_str == p_dict == ["C1"],
           (p_str, p_dict))
    _check("result dicts identical", r_str == r_dict, (r_str, r_dict))
    _check("calendar_key recorded", r_str.get("calendar_key") == "appt", r_str)


# ---------------------------------------------------------------------------
# T-drift + T-snapshot-unpin (M5)
# ---------------------------------------------------------------------------

def test_drift_follow_snapshot():
    print("(T-drift) snapshot pinned to an ENGRAVED sibling -> follow + log:")
    root = _build_scratch()
    A = _import_adapter(root)
    logs = _log_harness(A)
    state = _open_state(A)
    pushes = _spy_push_env(A)
    ev = _event(ULID)   # appointment -> routed 'appt' -> A
    proj = A.calendar_projection_from_event(ev)
    A.store_push_snapshot(state, ULID, "calendar", proj, calendar_id="B")
    cal_ids = {"appt": "A", "task": "B", "travel": "C"}
    A.sync_event(ev, cal_ids, "TL", state, None)
    _check("push followed the snapshot calendar (B)", pushes == ["B"], pushes)
    drift = [l for l in logs if l[0] == "calendar_drift_outbound"]
    _check("exactly one calendar_drift_outbound line", len(drift) == 1, logs)
    _check("no unpin line",
           not any(l[0] == "calendar_snapshot_unpin" for l in logs), logs)
    _check("snapshot calendar_id kept (B, COALESCE via follow)",
           A.snapshot_calendar_id(state, ULID) == "B",
           A.snapshot_calendar_id(state, ULID))
    state.close()


def test_snapshot_unpin():
    print("(T-snapshot-unpin) dead pin -> routed push + refresh + one line:")
    root = _build_scratch()
    A = _import_adapter(root)
    logs = _log_harness(A)
    state = _open_state(A)
    pushes = _spy_push_env(A)
    ev = _event(ULID)   # appointment -> routed A
    proj = A.calendar_projection_from_event(ev)
    A.store_push_snapshot(state, ULID, "calendar", proj, calendar_id="DEAD")
    cal_ids = {"appt": "A", "task": "B", "travel": "C"}
    A.sync_event(ev, cal_ids, "TL", state, None)
    _check("push followed the ROUTED id (A), not the dead pin",
           pushes == ["A"], pushes)
    unpin = [l for l in logs if l[0] == "calendar_snapshot_unpin"]
    _check("exactly one calendar_snapshot_unpin line", len(unpin) == 1, logs)
    _check("unpin line carries old + new id",
           unpin and "DEAD" in (unpin[0][2] or "")
           and "A" in (unpin[0][2] or ""), unpin)
    _check("no retry consumed (single push call)", len(pushes) == 1, pushes)
    _check("snapshot calendar_id refreshed to routed id (A)",
           A.snapshot_calendar_id(state, ULID) == "A",
           A.snapshot_calendar_id(state, ULID))
    state.close()

    # Calendar-level 404 during a PINNED push clears the stored calendar_id.
    root = _build_scratch()
    A = _import_adapter(root)
    logs = _log_harness(A)
    state = _open_state(A)

    def _push_404(event, cal_id, state_conn, src_conn=None):
        raise A.GwsError("calendar notFound", code=404)
    A.push_calendar = _push_404
    A.gws = lambda *a, **k: {}
    A.gws_delete = lambda *a, **k: True
    ev = _event(ULID)
    proj = A.calendar_projection_from_event(ev)
    A.store_push_snapshot(state, ULID, "calendar", proj, calendar_id="B")
    cal_ids = {"appt": "A", "task": "B", "travel": "C"}   # B engraved -> pin
    raised = None
    try:
        A.sync_event(ev, cal_ids, "TL", state, None)
    except A.GwsError as e:
        raised = e
    _check("GwsError propagates (retry accounting stays in drain)",
           raised is not None, "no raise")
    _check("calendar-level 404 on pinned push cleared stored calendar_id",
           A.snapshot_calendar_id(state, ULID) is None,
           A.snapshot_calendar_id(state, ULID))
    state.close()


# ---------------------------------------------------------------------------
# T-isolation (merged S5 + V15, 6.6)
# ---------------------------------------------------------------------------

def test_isolation_multi():
    print("(T-isolation) appt pull raises -> task/travel/tasks/drain run:")
    root = _build_scratch()
    A = _import_adapter(root)
    _log_harness(A)
    calls = []
    A.sweep_step = lambda s, src: (calls.append("sweep") or [])

    def _pull(cal_id, s, src, cal_key=None):
        calls.append("calendar:%s" % cal_key)
        if cal_key == "appt":
            raise A.GwsError("boom", code=404)
        return []
    A.pull_calendar = _pull
    A.pull_tasks = lambda tl, s, src: (calls.append("tasks") or [])
    A.outbox_drain = lambda s, src, cal, tl: (
        calls.append("drain") or {"pushed": 1})
    out = A.poll_cycle("S", "SRC", {"appt": "A", "task": "B", "travel": "C"},
                       "TL")
    _check("all later steps ran",
           calls == ["sweep", "calendar:appt", "calendar:task",
                     "calendar:travel", "tasks", "drain"], calls)
    _check("failed step carries error dict",
           isinstance(out.get("calendar:appt"), dict)
           and "error" in out["calendar:appt"], out.get("calendar:appt"))
    _check("sibling pulls carry raw returns",
           out.get("calendar:task") == [] and out.get("calendar:travel") == [],
           out)
    _check("drain ran", out.get("drain") == {"pushed": 1}, out.get("drain"))


# ---------------------------------------------------------------------------
# T-cursor-reset (TM-6)
# ---------------------------------------------------------------------------

def test_cursor_reset_containment():
    print("(T-cursor-reset) 410/400 on calendar:task resets ONLY its cursor:")
    for code in (410, 400):
        root = _build_scratch()
        A = _import_adapter(root)
        _log_harness(A)
        _stub_cycle_steps(A)
        state = _open_state(A)
        _engrave(A, state, cal_id_appt="A", cal_id_task="B",
                 cal_id_travel="C", gtasks_checklist_id="T2")
        _engrave(A, state, **{"cal_sync_token:appt": "ta",
                              "cal_sync_token:task": "tb",
                              "cal_sync_token:travel": "tc",
                              "cal_sync_token": "plainX"})
        # A/C succeed WITHOUT emitting a new token (cursors byte-untouched);
        # B raises `code` on every attempt (reset happens, retry raises out,
        # S5 isolation catches it).
        A.gws = CalListGws(A, tokens_out={}, raise_for={"B": code})
        t = A.get_mirror_targets(state)
        out = A.poll_cycle(state, "SRC", t["cal_ids"], t["checklist_id"])
        _check("[%d] task cursor reset to ''" % code,
               A.get_state(state, "cal_sync_token:task") == "",
               repr(A.get_state(state, "cal_sync_token:task")))
        _check("[%d] appt cursor byte-untouched" % code,
               A.get_state(state, "cal_sync_token:appt") == "ta",
               A.get_state(state, "cal_sync_token:appt"))
        _check("[%d] travel cursor byte-untouched" % code,
               A.get_state(state, "cal_sync_token:travel") == "tc",
               A.get_state(state, "cal_sync_token:travel"))
        _check("[%d] plain legacy cursor byte-untouched" % code,
               A.get_state(state, "cal_sync_token") == "plainX",
               A.get_state(state, "cal_sync_token"))
        _check("[%d] failure isolated to calendar:task step" % code,
               isinstance(out.get("calendar:task"), dict)
               and "error" in out["calendar:task"], out.get("calendar:task"))
        state.close()


# ---------------------------------------------------------------------------
# T-ensure (M3 routing table)
# ---------------------------------------------------------------------------

class ProvisionGws(object):
    """Fake gws for provision/bootstrap flows (cf. test_s2 FakeGws)."""

    def __init__(self, cal_items=None, tl_items=None):
        self.cal_items = cal_items or []
        self.tl_items = tl_items or []
        self.calls = []
        self.insert_ctr = [0]

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        a = args
        if a[:3] in (("calendar", "calendarList", "list"),
                     ("calendar", "calendars", "list")):
            return {"items": self.cal_items}
        if a[:3] == ("tasks", "tasklists", "list"):
            return {"items": self.tl_items}
        if a[:3] == ("calendar", "calendars", "get"):
            params = json.loads(a[a.index("--params") + 1])
            for it in self.cal_items:
                if it.get("id") == params.get("calendarId"):
                    return it
            return {}
        if a[:3] == ("calendar", "calendars", "insert"):
            self.insert_ctr[0] += 1
            return {"id": "NEWCAL%d" % self.insert_ctr[0]}
        if a[:3] == ("calendar", "calendars", "patch"):
            return {"id": "PATCHED"}
        if a[:3] == ("tasks", "tasklists", "insert"):
            return {"id": "NEWTL"}
        if a[:3] == ("tasks", "tasklists", "update"):
            return {"id": "UPDTL"}
        if a[:3] == ("tasks", "tasklists", "get"):
            return {}
        raise AssertionError("unexpected gws call: %s" % (a,))

    def verbs(self):
        return [args[:3] for args, _ in self.calls]


def _spies(A):
    """Wrap provision + bootstrap with call counters."""
    counts = {"provision": 0, "bootstrap": 0}
    real_prov = A.provision_category_calendars
    real_boot = A.bootstrap

    def prov(state, i18n_t=None):
        counts["provision"] += 1
        return real_prov(state, i18n_t)

    def boot(state):
        counts["bootstrap"] += 1
        # Repair semantics stand-in: engrave the missing legacy key(s)
        # (the real bootstrap does marker/name re-discovery via gws).
        if not A._is_engraved(A.get_state(state, "agent_calendar_id")):
            A.set_state(state, "agent_calendar_id", "RECOVERED_CAL")
        if not A._is_engraved(A.get_state(state, "agent_tasklist_id")):
            A.set_state(state, "agent_tasklist_id", "RECOVERED_TL")
        return (A.get_state(state, "agent_calendar_id"),
                A.get_state(state, "agent_tasklist_id"))
    A.provision_category_calendars = prov
    A.bootstrap = boot
    return counts


def test_ensure_routing():
    print("(T-ensure) M3 routing table:")
    # (a) F-legacy -> unchanged, provision NOT called.
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="C1", agent_tasklist_id="T1")
    counts = _spies(A)
    t = A.ensure_mirror_engraved(state)
    _check("(a) legacy targets returned unchanged",
           t["mode"] == "legacy" and t["checklist_id"] == "T1", t)
    _check("(a) provision NOT called, bootstrap NOT called",
           counts == {"provision": 0, "bootstrap": 0}, counts)
    state.close()

    # (b) F-empty -> provision called, all 4 keys engraved.
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    counts = _spies(A)
    A.gws = ProvisionGws()
    t = A.ensure_mirror_engraved(state)
    _check("(b) provision called once",
           counts["provision"] == 1 and counts["bootstrap"] == 0, counts)
    engraved = [A.get_state(state, k) for k in
                ("cal_id_appt", "cal_id_task", "cal_id_travel",
                 "gtasks_checklist_id")]
    _check("(b) all 4 V15 keys engraved", all(engraved), engraved)
    _check("(b) resolves multi afterwards", t["mode"] == "multi", t)
    state.close()

    # (c) F-partial -> provision fills ONLY the missing key.
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B",
             gtasks_checklist_id="T2")
    counts = _spies(A)
    A.gws = ProvisionGws()
    t = A.ensure_mirror_engraved(state)
    _check("(c) provision called", counts["provision"] == 1, counts)
    _check("(c) pre-existing ids untouched",
           A.get_state(state, "cal_id_appt") == "A"
           and A.get_state(state, "cal_id_task") == "B"
           and A.get_state(state, "gtasks_checklist_id") == "T2",
           "pre-existing ids changed")
    _check("(c) only missing key filled (cal_id_travel)",
           A.get_state(state, "cal_id_travel") == "NEWCAL1",
           A.get_state(state, "cal_id_travel"))
    state.close()

    # (d) F-half-legacy -> bootstrap called, provision NEVER.
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="C1")
    counts = _spies(A)
    t = A.ensure_mirror_engraved(state)
    _check("(d) bootstrap called, provision NEVER",
           counts == {"provision": 0, "bootstrap": 1}, counts)
    _check("(d) resulting state resolves mode=legacy",
           t["mode"] == "legacy" and t["cal_ids"]["appt"] == "C1", t)
    state.close()


# ---------------------------------------------------------------------------
# T-adopt-crash (M4 rules 1+2) + T-provision-idempotent
# ---------------------------------------------------------------------------

def test_adopt_crash_window():
    print("(T-adopt-crash) marked-orphan adopt + exact-token negatives:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    marker = A._frozen_cal_marker(state)
    fake = ProvisionGws(cal_items=[
        {"id": "ORPH", "summary": "whatever",
         "description": "%s category=appt" % marker},
    ])
    A.gws = fake
    ids = A.provision_category_calendars(state)
    _check("marked orphan ADOPTED for appt (engraved, no twin)",
           A.get_state(state, "cal_id_appt") == "ORPH", ids)
    inserts = [v for v in fake.verbs()
               if v == ("calendar", "calendars", "insert")]
    _check("no twin created for appt (2 inserts: task+travel only)",
           len(inserts) == 2, fake.verbs())
    state.close()

    # Negative pins: 'category=appt-x' token and a DIFFERENT category token
    # are NOT adopted for 'appt' (exact-token rule).
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    marker = A._frozen_cal_marker(state)
    fake = ProvisionGws(cal_items=[
        {"id": "BADTOKEN", "summary": "x",
         "description": "%s category=appt-x" % marker},
        {"id": "OTHERCAT", "summary": "y",
         "description": "%s category=task" % marker},
    ])
    A.gws = fake
    A.provision_category_calendars(state)
    _check("'category=appt-x' NOT adopted for appt",
           A.get_state(state, "cal_id_appt") != "BADTOKEN",
           A.get_state(state, "cal_id_appt"))
    _check("marker with DIFFERENT category token NOT adopted for appt",
           A.get_state(state, "cal_id_appt") != "OTHERCAT",
           A.get_state(state, "cal_id_appt"))
    _check("OTHERCAT correctly bound to its own category (task)",
           A.get_state(state, "cal_id_task") == "OTHERCAT",
           A.get_state(state, "cal_id_task"))
    state.close()

    # Unmarked same-name calendar still raises BootstrapAmbiguous (gate off).
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    names, _cl = A._resolve_category_names()
    fake = ProvisionGws(cal_items=[
        {"id": "FOREIGN", "summary": names["appt"], "description": ""},
    ])
    A.gws = fake
    raised = None
    try:
        A.provision_category_calendars(state)
    except A.BootstrapAmbiguous as e:
        raised = e
    _check("unmarked same-name raises BootstrapAmbiguous absent the gate",
           raised is not None, "no raise")
    _check("no state engraved while ambiguous (no partial provision)",
           A.get_state(state, "cal_id_task") is None,
           A.get_state(state, "cal_id_task"))
    state.close()


def test_provision_idempotent_and_gate_set():
    print("(T-provision-idempotent) no-op re-run + m6 candidate-SET consent:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    _engrave(A, state, cal_id_appt="A", cal_id_task="B", cal_id_travel="C",
             gtasks_checklist_id="T2")
    fake = ProvisionGws()
    A.gws = fake
    ids = A.provision_category_calendars(state)
    _check("complete state re-run is a no-op (zero gws calls)",
           len(fake.calls) == 0, fake.verbs())
    _check("re-run returns the engraved ids",
           ids == {"appt": "A", "task": "B", "travel": "C",
                   "checklist": "T2"}, ids)
    state.close()

    # m6: gate off -> ONE BootstrapAmbiguous carrying the full candidate SET
    # (3 same-name calendars + same-title tasklist = 4 candidates).
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    names, checklist_name = A._resolve_category_names()
    cal_items = [{"id": "F%s" % k, "summary": names[k], "description": ""}
                 for k in ("appt", "task", "travel")]
    fake = ProvisionGws(cal_items=cal_items,
                        tl_items=[{"id": "FTL", "title": checklist_name}])
    A.gws = fake
    raised = None
    try:
        A.provision_category_calendars(state)
    except A.BootstrapAmbiguous as e:
        raised = e
    _check("gate off: ONE signal with the full 4-candidate set",
           raised is not None and len(raised.candidates) == 4,
           getattr(raised, "candidates", None))
    state.close()

    # m6: gate on -> the SAME set adopts in one run (calendars stamped).
    root = _build_scratch(conf_lines=["MIRROR_ADOPT_UNMARKED=true"])
    A = _import_adapter(root)
    state = _open_state(A)
    names, checklist_name = A._resolve_category_names()
    cal_items = [{"id": "F%s" % k, "summary": names[k], "description": ""}
                 for k in ("appt", "task", "travel")]
    fake = ProvisionGws(cal_items=cal_items,
                        tl_items=[{"id": "FTL", "title": checklist_name}])
    A.gws = fake
    ids = A.provision_category_calendars(state)
    _check("gate on: full candidate set adopted in one exchange",
           ids == {"appt": "Fappt", "task": "Ftask", "travel": "Ftravel",
                   "checklist": "FTL"}, ids)
    patches = [v for v in fake.verbs()
               if v == ("calendar", "calendars", "patch")]
    _check("gated adopt stamped marker+category on the 3 calendars",
           len(patches) == 3, fake.verbs())
    state.close()


# ---------------------------------------------------------------------------
# T-schema (idempotent calendar_id ALTER)
# ---------------------------------------------------------------------------

def test_schema_migration():
    print("(T-schema) push_snapshot.calendar_id added exactly once:")
    root = _build_scratch()
    A = _import_adapter(root)
    db_path = os.path.join(root, "mirror", "pre_promotion_state.db")
    # Build a PRE-promotion state DB: schema only, no calendar_id column.
    pre = sqlite3.connect(db_path)
    schema = open(os.path.join(root, "mirror", "mirror_state.sql")).read()
    pre.executescript(schema)
    pre.commit()
    cols = [r[1] for r in pre.execute("PRAGMA table_info(push_snapshot)")]
    pre.close()
    _check("pre-promotion DB has no calendar_id column",
           "calendar_id" not in cols, cols)
    A.STATE_DB_PATH = db_path
    conn = A.open_state_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(push_snapshot)")]
    _check("first open adds calendar_id",
           cols.count("calendar_id") == 1, cols)
    conn.close()
    conn = A.open_state_db()   # second open = no-op
    cols = [r[1] for r in conn.execute("PRAGMA table_info(push_snapshot)")]
    _check("second open is a no-op (still exactly one calendar_id)",
           cols.count("calendar_id") == 1, cols)
    conn.close()


# ---------------------------------------------------------------------------
# T-notify-ops (2.8, R2-2 prefix assertions)
# ---------------------------------------------------------------------------

def _import_notify(root):
    for name in ("notify", "mirror_i18n"):
        sys.modules.pop(name, None)
    sys.path.insert(0, os.path.join(root, "mirror"))
    import notify  # noqa: F401
    import mirror_i18n  # noqa: F401
    sys.modules["mirror_i18n"]._reset_cache()
    return sys.modules["notify"]


def _write_push_fake(root, log_path, exit_code=0):
    routines = os.path.join(root, "routines")
    os.makedirs(routines, exist_ok=True)
    push = os.path.join(routines, "push.sh")
    with open(push, "w") as fh:
        fh.write("#!/bin/bash\n"
                 "printf '%s\\x1f' \"$@\" >> \"" + log_path + "\"\n"
                 "printf '\\n' >> \"" + log_path + "\"\n"
                 "exit " + str(exit_code) + "\n")
    os.chmod(push, 0o755)
    return push


def _push_calls(log_path):
    if not os.path.exists(log_path):
        return []
    out = []
    for line in open(log_path).read().splitlines():
        if line:
            out.append(line.split("\x1f")[:-1])
    return out


def test_notify_ops():
    print("(T-notify-ops) kind routing + ops prefix presence/absence:")
    # deliver_pending: 2-arg fn receives kind; 1-arg fn stays compatible.
    root = _build_scratch()
    A = _import_adapter(root)
    N = _import_notify(root)
    state = _open_state(A)
    N.notify(state, "reconcile_report", None, dedup=False, checked=1,
             missing=0, mismatch=0, orphan=0, deleted_held=0, verdict="V")
    N.notify(state, "outbox_exhausted", ULID, title="X")
    got2 = []
    N.deliver_pending(state, deliver_fn=lambda m, k: got2.append((m, k)))
    _check("2-arg deliver_fn receives kind",
           [k for _m, k in got2] == ["reconcile_report", "outbox_exhausted"],
           got2)
    N.notify(state, "outbox_exhausted", ULID, title="Y")
    got1 = []
    N.deliver_pending(state, deliver_fn=lambda m: got1.append(m))
    _check("1-arg deliver_fn stays backward compatible", len(got1) == 1, got1)
    state.close()

    # Ops routing: MIRROR_OPS_ENV_FILE set + exists -> --env + prefix.
    ops_env = tempfile.NamedTemporaryFile(prefix="dgn364-ops-", suffix=".env",
                                          delete=False)
    ops_env.write(b"BOT=x\n")
    ops_env.close()
    root = _build_scratch(
        conf_lines=["MIRROR_OPS_ENV_FILE=%s" % ops_env.name],
        instance_lines=["DOGANY_AGENT_LABEL=TestBot"])
    N = _import_notify(root)
    log = os.path.join(root, "push.log")
    _write_push_fake(root, log)
    expected_prefix = N._ops_prefix({"DOGANY_AGENT_LABEL": "TestBot"})
    _check("resolved prefix is parameterized with the label",
           "TestBot" in expected_prefix, repr(expected_prefix))
    N.push_sh_deliver("weekly report body", kind="reconcile_report")
    calls = _push_calls(log)
    _check("ops kind routed via --env <ops env file>",
           len(calls) == 1 and "--env" in calls[0]
           and ops_env.name in calls[0], calls)
    msg = calls[0][calls[0].index("--text") + 1] if calls else ""
    _check("R2-2 prefix PRESENT in ops-path message bytes",
           msg.startswith(expected_prefix), repr(msg))
    # Non-ops kind stays on the persona channel even with conf set.
    os.unlink(log)
    N.push_sh_deliver("plain notice", kind="outbox_exhausted")
    calls = _push_calls(log)
    _check("non-ops kind stays persona-channel (no --env)",
           len(calls) == 1 and "--env" not in calls[0], calls)

    # Fallback: conf key unset -> persona channel, prefix ABSENT.
    root = _build_scratch(instance_lines=["DOGANY_AGENT_LABEL=TestBot"])
    N = _import_notify(root)
    log = os.path.join(root, "push.log")
    _write_push_fake(root, log)
    N.push_sh_deliver("weekly report body", kind="reconcile_report")
    calls = _push_calls(log)
    _check("unset conf -> persona fallback (no --env)",
           len(calls) == 1 and "--env" not in calls[0], calls)
    msg = calls[0][calls[0].index("--text") + 1] if calls else ""
    _check("R2-2 prefix ABSENT from fallback-path message",
           expected_prefix not in msg and msg == "weekly report body",
           repr(msg))

    # Fallback: conf set but file MISSING -> persona channel.
    root = _build_scratch(
        conf_lines=["MIRROR_OPS_ENV_FILE=/nonexistent/ops.env"],
        instance_lines=["DOGANY_AGENT_LABEL=TestBot"])
    N = _import_notify(root)
    log = os.path.join(root, "push.log")
    _write_push_fake(root, log)
    N.push_sh_deliver("weekly report body", kind="reconcile_report")
    calls = _push_calls(log)
    _check("missing ops env file -> persona fallback",
           len(calls) == 1 and "--env" not in calls[0], calls)
    os.unlink(ops_env.name)


# ---------------------------------------------------------------------------
# T-cleanup (R2-4; pins 2.5)
# ---------------------------------------------------------------------------

def test_cleanup_direct_keys():
    print("(T-cleanup) generic keys only; ag_* poison never deleted; de-dup:")
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    # Generic keys engraved; A appears under two keys (de-dup pin); the
    # checklist id T appears under both tasklist keys (de-dup pin). Poison:
    # Ag-literal keys with DISTINCT sentinel values.
    _engrave(A, state,
             cal_id_appt="A", cal_id_task="B", cal_id_travel="A",
             agent_calendar_id="C",
             gtasks_checklist_id="T", agent_tasklist_id="T",
             ag_calendar_id="AG_POISON_CAL", ag_tasklist_id="AG_POISON_TL")
    deleted = {"calendar": [], "tasklist": []}

    def _spy_delete(*args):
        params = json.loads(args[args.index("--params") + 1])
        if args[:3] == ("calendar", "calendars", "delete"):
            deleted["calendar"].append(params["calendarId"])
        elif args[:3] == ("tasks", "tasklists", "delete"):
            deleted["tasklist"].append(params["tasklist"])
        return True
    A.gws_delete = _spy_delete
    A.cleanup(state)
    _check("calendar deletes = distinct engraved generic ids {A,B,C}",
           sorted(deleted["calendar"]) == ["A", "B", "C"], deleted)
    _check("tasklist deletes = {T} (deleted once despite two keys)",
           deleted["tasklist"] == ["T"], deleted)
    _check("Ag-literal sentinel ids NEVER reach a delete call",
           "AG_POISON_CAL" not in deleted["calendar"]
           and "AG_POISON_TL" not in deleted["tasklist"], deleted)
    state.close()

    # Partial state: whatever exists gets deleted, missing keys skipped.
    root = _build_scratch()
    A = _import_adapter(root)
    state = _open_state(A)
    _engrave(A, state, agent_calendar_id="ONLYCAL", cal_id_task="")
    deleted = {"calendar": [], "tasklist": []}
    A.gws_delete = _spy_delete
    A.cleanup(state)
    _check("partial state: engraved id deleted, '' skipped, no raise",
           deleted["calendar"] == ["ONLYCAL"] and deleted["tasklist"] == [],
           deleted)
    state.close()


# ---------------------------------------------------------------------------
# T-drift-guard (R2-4; pins 2.7b) -- subprocess over the FIXED update.sh
# ---------------------------------------------------------------------------

def _run_guard_snippet(script):
    """Run a bash snippet with update.sh's extractor + guard sourced."""
    src = open(UPDATE_SH).read()

    def _fn(name):
        m = re.search(r"^%s\(\) \{.*?^\}" % re.escape(name), src,
                      re.MULTILINE | re.DOTALL)
        if not m:
            raise AssertionError("function %s not found in update.sh" % name)
        return m.group(0)
    prelude = "\n".join([
        "msg() { printf '%s\\n' \"$2\"; }",   # stub update.sh's msg()
        _fn("extract_ver_sdk_bridge_py"),
        _fn("drift_guard_file"),
    ])
    return subprocess.run(["bash", "-c", prelude + "\n" + script],
                          capture_output=True, text=True)


def test_drift_guard():
    print("(T-drift-guard) extractor tuple/list + guard SKIP/PROCEED:")
    d = tempfile.mkdtemp(prefix="dgn364-guard-")
    tuple_pin = os.path.join(d, "tuple_pin.py")
    with open(tuple_pin, "w") as fh:
        fh.write("ALLOWED_USER_VERSIONS = (7, 8)  # tuple pin\n")
    list_pin = os.path.join(d, "list_pin.py")
    with open(list_pin, "w") as fh:
        fh.write("ALLOWED_USER_VERSIONS = [7, 8]\n")
    ahead_pin = os.path.join(d, "ahead_pin.py")
    with open(ahead_pin, "w") as fh:
        fh.write("ALLOWED_USER_VERSIONS = (8, 9)\n")

    # (a) tuple-pin file -> extractor parses (7, 8) -> prints 8.
    r = _run_guard_snippet('extract_ver_sdk_bridge_py "%s"' % tuple_pin)
    _check("(a) tuple pin parsed (max=8)", r.stdout.strip() == "8",
           (r.returncode, r.stdout, r.stderr))
    # ... and the guard can compare/SKIP: instance (8,9) ahead of fw (7,8).
    r = _run_guard_snippet(
        'if drift_guard_file "mirror/sdk_bridge.py" "%s" "%s" '
        '"sdk_bridge_py"; then echo PROCEED; else echo SKIP; fi'
        % (tuple_pin, ahead_pin))
    _check("(a) guard SKIPs when instance tuple pin is ahead",
           r.stdout.strip().endswith("SKIP"),
           (r.returncode, r.stdout, r.stderr))
    _check("(a) loud warning block printed on SKIP",
           "REVERSE-DRIFT GUARD" in (r.stdout + r.stderr),
           (r.stdout, r.stderr))

    # (b) list-pin parses identically.
    r = _run_guard_snippet('extract_ver_sdk_bridge_py "%s"' % list_pin)
    _check("(b) list pin parsed (max=8)", r.stdout.strip() == "8",
           (r.returncode, r.stdout, r.stderr))
    r = _run_guard_snippet(
        'if drift_guard_file "mirror/sdk_bridge.py" "%s" "%s" '
        '"sdk_bridge_py"; then echo PROCEED; else echo SKIP; fi'
        % (list_pin, ahead_pin))
    _check("(b) guard compares tuple-vs-list identically (SKIP)",
           r.stdout.strip().endswith("SKIP"),
           (r.returncode, r.stdout, r.stderr))

    # (c) missing instance file -> first-install PROCEED (no exclude).
    r = _run_guard_snippet(
        'if drift_guard_file "mirror/sdk_bridge.py" "%s" "%s" '
        '"sdk_bridge_py"; then echo PROCEED; else echo SKIP; fi'
        % (tuple_pin, os.path.join(d, "missing.py")))
    _check("(c) missing instance file -> PROCEED",
           r.stdout.strip().endswith("PROCEED"),
           (r.returncode, r.stdout, r.stderr))

    # Regression pin: canonical mirror/sdk_bridge.py's live tuple pin is
    # parseable by the fixed extractor (the shipped guard can engage).
    r = _run_guard_snippet('extract_ver_sdk_bridge_py "%s"'
                           % os.path.join(SRC_MIRROR, "sdk_bridge.py"))
    _check("canonical sdk_bridge.py pin parses (max=8)",
           r.stdout.strip() == "8", (r.returncode, r.stdout, r.stderr))
    # Guard entry present at the CORRECT path; wrong-path entry deleted.
    upd = open(UPDATE_SH).read()
    _check("GUARDED_FILES carries mirror/sdk_bridge.py:sdk_bridge_py",
           '"mirror/sdk_bridge.py:sdk_bridge_py"' in upd, "entry missing")
    _check("wrong-path database/sdk_bridge.py entry deleted",
           '"database/sdk_bridge.py:sdk_bridge_py"' not in upd,
           "stale entry present")
    # Section-order swap (m7): mirror rsync before routines rsync.
    mirror_pos = upd.find('"$REPO_ROOT/mirror/" "$INSTANCE/mirror/"')
    routines_pos = upd.find('"$TEMPLATE/routines/" "$INSTANCE/routines/"')
    _check("m7 section-order swap: mirror/ rsync BEFORE routines/ rsync",
           0 < mirror_pos < routines_pos, (mirror_pos, routines_pos))
    _check("anchored exclude '/sdk_bridge.py' used (leading slash)",
           "--exclude '/sdk_bridge.py'" in upd, "anchored exclude missing")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# T-script-rail (TM-8) -- shell-level, python block stubbed
# ---------------------------------------------------------------------------

def _stage_rail_env(script_name, py_rc, push_exit=0):
    """Scratch instance with the REAL template script, its python block
    stubbed to a forced exit code; authed fake gws; recording push.sh."""
    root = tempfile.mkdtemp(prefix="dgn364-rail-")
    os.makedirs(os.path.join(root, "routines"))
    os.makedirs(os.path.join(root, "mirror"))
    os.makedirs(os.path.join(root, "config"))
    os.makedirs(os.path.join(root, ".telegram_bot"))
    body = open(os.path.join(TEMPLATE_ROUTINES, script_name)).read()
    stub = ("\"$PY\" - <<'PY'\nimport sys\nsys.exit(%d)\nPY\n" % py_rc)
    new_body, n = re.subn(r'"\$PY" - <<\'PY\'\n.*?\nPY\n', stub, body,
                          flags=re.DOTALL)
    assert n == 1, "python heredoc not found in %s" % script_name
    spath = os.path.join(root, "routines", script_name)
    with open(spath, "w") as fh:
        fh.write(new_body)
    os.chmod(spath, 0o755)
    log = os.path.join(root, "push.log")
    _write_push_fake(root, log, exit_code=push_exit)
    with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
        fh.write("MIRROR_MODULE=on\n")
    # Fake authed gws on PATH.
    bindir = tempfile.mkdtemp(prefix="dgn364-bin-")
    gws = os.path.join(bindir, "gws")
    with open(gws, "w") as fh:
        fh.write("#!/bin/bash\nexit 0\n")
    os.chmod(gws, 0o755)
    return root, log, bindir, spath


def test_script_rail():
    print("(T-script-rail) exit-3 rail, stamp-after-push, both scripts:")
    for script in ("mirror-poll.sh", "mirror-reconcile.sh"):
        # exit 3 -> push fired + stamp written + script exits 0.
        root, log, bindir, spath = _stage_rail_env(script, py_rc=3)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        r1 = subprocess.run(["bash", spath], capture_output=True, text=True,
                            env=env)
        stamp = os.path.join(root, ".telegram_bot", "mirror-unengraved.stamp")
        _check("[%s] exit 3 -> script exits 0" % script, r1.returncode == 0,
               (r1.returncode, r1.stderr[-300:]))
        _check("[%s] push fired once" % script, len(_push_calls(log)) == 1,
               _push_calls(log))
        _check("[%s] stamp written after successful push" % script,
               os.path.exists(stamp), "no stamp")
        # second run same UTC day -> NO second push, still exit 0.
        r2 = subprocess.run(["bash", spath], capture_output=True, text=True,
                            env=env)
        _check("[%s] second run same day: no second push" % script,
               r2.returncode == 0 and len(_push_calls(log)) == 1,
               (r2.returncode, _push_calls(log)))
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(bindir, ignore_errors=True)

        # push.sh failure -> NO stamp (stamp-after-push order) + retry next.
        root, log, bindir, spath = _stage_rail_env(script, py_rc=3,
                                                   push_exit=1)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        r1 = subprocess.run(["bash", spath], capture_output=True, text=True,
                            env=env)
        stamp = os.path.join(root, ".telegram_bot", "mirror-unengraved.stamp")
        _check("[%s] failed push -> NO stamp written (m3 order)" % script,
               r1.returncode == 0 and not os.path.exists(stamp),
               (r1.returncode, os.path.exists(stamp)))
        subprocess.run(["bash", spath], capture_output=True, text=True,
                       env=env)
        _check("[%s] next run pushes again after failed push" % script,
               len(_push_calls(log)) == 2, _push_calls(log))
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(bindir, ignore_errors=True)

        # python exit 0 -> no rail action, exit 0, no push.
        root, log, bindir, spath = _stage_rail_env(script, py_rc=0)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        r = subprocess.run(["bash", spath], capture_output=True, text=True,
                           env=env)
        _check("[%s] python exit 0 -> exit 0, no rail push" % script,
               r.returncode == 0 and len(_push_calls(log)) == 0,
               (r.returncode, _push_calls(log)))
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(bindir, ignore_errors=True)

        # python exit 1 -> propagates as script exit 1 (real failures loud).
        root, log, bindir, spath = _stage_rail_env(script, py_rc=1)
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env["PATH"]
        r = subprocess.run(["bash", spath], capture_output=True, text=True,
                           env=env)
        _check("[%s] python exit 1 propagates as script exit 1" % script,
               r.returncode == 1 and len(_push_calls(log)) == 0,
               (r.returncode, _push_calls(log)))
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(bindir, ignore_errors=True)

    # Zero raw state-key reads remain in the shipped scripts (property a).
    for script in ("mirror-poll.sh", "mirror-reconcile.sh"):
        body = open(os.path.join(TEMPLATE_ROUTINES, script)).read()
        _check("[%s] no raw get_state key reads in python block" % script,
               'get_state(state, "agent_calendar_id")' not in body
               and 'get_state(state, "agent_tasklist_id")' not in body
               and "get_mirror_targets" in body, "raw key read present")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    test_resolver_fixtures()
    test_scanset_and_cursors()
    test_route()
    test_sync_legacy_shim()
    test_drift_follow_snapshot()
    test_snapshot_unpin()
    test_isolation_multi()
    test_cursor_reset_containment()
    test_ensure_routing()
    test_adopt_crash_window()
    test_provision_idempotent_and_gate_set()
    test_schema_migration()
    test_notify_ops()
    test_cleanup_direct_keys()
    test_drift_guard()
    test_script_rail()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
