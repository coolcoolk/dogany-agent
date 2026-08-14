#!/usr/bin/env python3
"""DGN-849 gate: kit-agnostic calendar color seam + timed subtype blocks
+ A2 base-kit variable-ization (config-first, zero-delta fallbacks).

Asserts:
  (a) ZERO-DELTA: no config/calendar-colors.json -> the color seam is off,
      legacy status color markers (done->'10', expired->'8') and undecorated
      titles are preserved byte-identically; every A2 constant resolves to
      its prior canonical literal.
  (b) GENERIC: an arbitrary color axis (a fake 'project' field, then a fake
      FK-resolved 'proj_tier' axis) yields colorIds from config alone --
      zero adapter code change, zero domain knowledge in code.
  (c) D2 axis ruling: config armed -> color-source owns colorId (status
      markers retired), status rides gcal status + title decoration.
  (d) Timed subtype blocks: presence/travel/prep project as subtype-labeled
      TIMED blocks (banner retired), push body == projection title (F1),
      and inbound echo hashes match.
  (e) Inbound title write-back strips mirror-authored prefixes.
  (f) Tolerance: malformed config / hostile resolve identifiers degrade to
      no-color, never raise.
  (g) A2 knobs reflect config when set; malformed ints fall back.

The gws / SDK / http layers are STUBBED -- no network. The adapter is
imported from a scratch copy so ../config/ path resolution is real.

Run: python3 tests/mirror/test_dgn849_color_seam.py    (exit 0 = pass)
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_MIRROR = os.path.join(REPO_ROOT, "mirror")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _build_scratch(colors=None, lifekit_conf=None):
    """Scratch instance: copied mirror package + optional config files."""
    root = tempfile.mkdtemp(prefix="dgn849-")
    mirror_dir = os.path.join(root, "mirror")
    os.makedirs(mirror_dir)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_i18n.py",
              "mirror_colors.py", "mirror_state.sql"):
        shutil.copy2(os.path.join(SRC_MIRROR, f), os.path.join(mirror_dir, f))
    os.makedirs(os.path.join(root, "config"))
    if colors is not None:
        with open(os.path.join(root, "config", "calendar-colors.json"),
                  "w") as fh:
            if isinstance(colors, str):
                fh.write(colors)          # raw payload (malformed-json case)
            else:
                json.dump(colors, fh)
    if lifekit_conf is not None:
        with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
            fh.write(lifekit_conf)
    return root


def _import_adapter(root):
    for name in ("adapter", "sdk_bridge", "notify", "http_direct",
                 "mirror_i18n", "mirror_colors"):
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
    sys.modules["mirror_colors"]._reset_cache()
    return mod


def _ev(**kw):
    """Minimal event-row dict fixture (bare-dict tolerant paths)."""
    base = {
        "ulid": "01TESTULID0000000000000000", "kind": "task",
        "title": "write report", "note": "", "location": "",
        "schedule_kind": "timed", "start_at": "2026-08-20T01:00:00Z",
        "end_at": "2026-08-20T02:00:00Z", "display_tz": "Asia/Seoul",
        "slot_exclusive": 0, "status": "open", "settled_outcome": None,
        "persons": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# (a) zero-delta: no color config
# ---------------------------------------------------------------------------

def test_zero_delta_no_config():
    print("A1 zero-delta (no calendar-colors.json):")
    root = _build_scratch()
    A = _import_adapter(root)
    mc = sys.modules["mirror_colors"]
    _check("seam off: active() is False", mc.active() is False)
    _check("open -> (confirmed, None)",
           A.event_to_cal_status(_ev()) == ("confirmed", None))
    _check("done keeps legacy marker '10'",
           A.event_to_cal_status(_ev(status="done")) == ("confirmed", "10"))
    _check("expired keeps legacy marker '8'",
           A.event_to_cal_status(_ev(status="expired")) == ("confirmed", "8"))
    _check("abandoned -> (cancelled, None)",
           A.event_to_cal_status(_ev(status="abandoned"))
           == ("cancelled", None))
    # Full projection of a plain timed task: frozen current-behavior dict.
    proj = A.calendar_projection_from_event(_ev())
    _check("plain task projection byte-identical to prior contract",
           proj == {"title": "write report", "note": "", "location": "",
                    "start_at": "2026-08-20T01:00:00Z",
                    "end_at": "2026-08-20T02:00:00Z",
                    "schedule_kind": "timed", "gcal_status": "confirmed",
                    "color_id": None, "transparency": "transparent",
                    "persons": ""}, repr(proj))
    done = A.calendar_projection_from_event(_ev(status="done"))
    _check("done title UNdecorated when seam off (zero-delta)",
           done["title"] == "write report", repr(done["title"]))
    _check("done color still '10' when seam off", done["color_id"] == "10")
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b) generic axis: fake 'project' field, then FK-resolved 'proj_tier'
# ---------------------------------------------------------------------------

def test_generic_direct_axis():
    print("A1 generic axis (direct event field, fake 'project'):")
    root = _build_scratch(colors={
        "color_source": "project",
        "color_map": {"apollo": "5", "hermes": "9"}})
    A = _import_adapter(root)
    _check("mapped value -> configured colorId",
           A.event_to_cal_status(_ev(project="apollo"))
           == ("confirmed", "5"))
    _check("second mapping independent",
           A.event_to_cal_status(_ev(project="hermes"))
           == ("confirmed", "9"))
    _check("unmapped value -> no color",
           A.event_to_cal_status(_ev(project="zeus"))
           == ("confirmed", None))
    _check("missing source field -> no color",
           A.event_to_cal_status(_ev()) == ("confirmed", None))
    shutil.rmtree(root, ignore_errors=True)

    # colorId typo guard: values outside gcal '1'..'11' never reach the
    # push body (an invalid colorId 400s non-retryably and stalls the row).
    root = _build_scratch(colors={
        "color_source": "project",
        "color_map": {"apollo": "42", "hermes": "sage", "ares": "11"}})
    A = _import_adapter(root)
    _check("out-of-range colorId dropped to no-color",
           A.event_to_cal_status(_ev(project="apollo"))
           == ("confirmed", None))
    _check("non-numeric colorId dropped to no-color",
           A.event_to_cal_status(_ev(project="hermes"))
           == ("confirmed", None))
    _check("boundary colorId '11' accepted",
           A.event_to_cal_status(_ev(project="ares"))
           == ("confirmed", "11"))
    shutil.rmtree(root, ignore_errors=True)


def test_generic_fk_resolved_axis():
    print("A1 generic axis behind a foreign key (config resolve, no code):")
    root = _build_scratch(colors={
        "color_source": "proj_tier",
        "color_map": {"gold": "11", "silver": "7"},
        "resolve": {"proj_tier": {"via": "project_id", "table": "projects",
                                  "key": "id", "select": "tier"}}})
    A = _import_adapter(root)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, tier TEXT)")
    conn.execute("INSERT INTO projects VALUES (1, 'gold'), (2, 'silver')")
    ev = A.enrich_event(conn, _ev(project_id=1))
    _check("enrich attaches config-declared derived field",
           ev.get("proj_tier") == "gold", repr(ev.get("proj_tier")))
    _check("FK-resolved axis -> colorId, zero adapter change",
           A.event_to_cal_status(ev) == ("confirmed", "11"))
    ev2 = A.enrich_event(conn, _ev(project_id=2))
    _check("second row resolves independently",
           A.event_to_cal_status(ev2) == ("confirmed", "7"))
    ev3 = A.enrich_event(conn, _ev())   # no FK value
    _check("row without FK -> no color, no crash",
           A.event_to_cal_status(ev3) == ("confirmed", None))
    conn.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c) D2 axis ruling: color config armed -> status markers retired
# ---------------------------------------------------------------------------

def test_d2_axis_conflict_ruling():
    print("A1 D2 ruling (config armed): color axis owns colorId:")
    root = _build_scratch(colors={
        "color_source": "project", "color_map": {"apollo": "5"}})
    A = _import_adapter(root)
    st = A.event_to_cal_status(_ev(status="done", project="apollo"))
    _check("done -> axis color '5', NOT legacy '10'",
           st == ("confirmed", "5"), repr(st))
    st = A.event_to_cal_status(_ev(status="expired", project="apollo"))
    _check("expired -> axis color '5', NOT legacy '8'",
           st == ("confirmed", "5"), repr(st))
    st = A.event_to_cal_status(_ev(status="done"))
    _check("done w/o axis value -> None (markers retired)",
           st == ("confirmed", None), repr(st))
    _check("abandoned still -> (cancelled, None)",
           A.event_to_cal_status(_ev(status="abandoned"))
           == ("cancelled", None))
    # R&R: with color armed but NO status_decor declared, terminal status
    # gets NO decoration (framework holds no decoration text).
    p = A.calendar_projection_from_event(_ev(status="done"))
    _check("done title UNdecorated when kit declares no status_decor",
           p["title"] == "write report", repr(p["title"]))
    shutil.rmtree(root, ignore_errors=True)

    # Kit declares decoration text -> it (and only it) rides the title.
    root = _build_scratch(colors={
        "color_source": "project", "color_map": {"apollo": "5"},
        "status_decor": {"done": "D:", "expired": "X:"}})
    A = _import_adapter(root)
    p = A.calendar_projection_from_event(_ev(status="done"))
    _check("done title decorated with KIT-supplied mark",
           p["title"] == "D:" + "write report", repr(p["title"]))
    p = A.calendar_projection_from_event(_ev(status="expired"))
    _check("expired title decorated with KIT-supplied mark",
           p["title"] == "X:" + "write report", repr(p["title"]))
    _check("open title undecorated when armed",
           A.calendar_projection_from_event(_ev())["title"]
           == "write report")
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (d) timed subtype blocks (presence / travel / prep)
# ---------------------------------------------------------------------------

# A kit color config that also declares subtype label TEXT + a wrap. All
# label strings are KIT content (this fixture stands in for what Skull
# publishes); the framework carries none.
_KIT_LABELS = {
    "color_source": "project", "color_map": {"apollo": "5"},
    "subtype_fmt": "[{label}] {title}",
    "subtype_labels": {"presence": "STAY", "travel": "MOVE", "prep": "PREP"},
}


def test_timed_subtype_blocks_neutral_fallback():
    print("A1 R&R: subtype blocks with NO config -> neutral raw-key label:")
    root = _build_scratch()   # no calendar-colors.json
    A = _import_adapter(root)
    pres = _ev(kind="presence", title="jeju stay", location="Jeju",
               start_at="2026-08-20T05:00:00Z", end_at="2026-08-22T09:00:00Z")
    p = A.calendar_projection_from_event(pres)
    _check("presence projects TIMED (not all_day banner)",
           p["schedule_kind"] == "timed", repr(p["schedule_kind"]))
    _check("presence keeps the row's own instants",
           (p["start_at"], p["end_at"])
           == ("2026-08-20T05:00:00Z", "2026-08-22T09:00:00Z"),
           repr((p["start_at"], p["end_at"])))
    # Neutral fallback: raw subtype KEY as label, default "{label} {title}"
    # wrap -- a code value, NEVER a domain/locale word.
    _check("presence neutral fallback = raw key + default wrap",
           p["title"] == "presence Jeju", repr(p["title"]))
    trav = _ev(block_class="travel", derived_role="before", title="to office")
    _check("travel neutral fallback = raw key",
           A.calendar_projection_from_event(trav)["title"]
           == "travel to office")
    prep = _ev(block_class="travel", derived_role="before_prep",
               title="pack bag")
    _check("prep neutral fallback = raw key (prep beats travel)",
           A.calendar_projection_from_event(prep)["title"]
           == "prep pack bag")
    _check("plain task unlabeled",
           A.calendar_projection_from_event(_ev())["title"]
           == "write report")
    shutil.rmtree(root, ignore_errors=True)


def test_timed_subtype_blocks_kit_labels():
    print("A1 subtype blocks with kit-declared labels (Skull content):")
    root = _build_scratch(colors=_KIT_LABELS)
    A = _import_adapter(root)
    pres = _ev(kind="presence", title="jeju stay", location="Jeju",
               start_at="2026-08-20T05:00:00Z", end_at="2026-08-22T09:00:00Z")
    p = A.calendar_projection_from_event(pres)
    _check("presence title = kit label + place",
           p["title"] == "[STAY] Jeju", repr(p["title"]))
    _check("presence stays transparent (non-blocking)",
           p["transparency"] == "transparent")
    trav = _ev(block_class="travel", derived_role="before", title="to office")
    _check("travel block title = kit label",
           A.calendar_projection_from_event(trav)["title"]
           == "[MOVE] to office")
    prep = _ev(block_class="travel", derived_role="before_prep",
               title="pack bag")
    _check("prep block title = kit label (prep beats travel)",
           A.calendar_projection_from_event(prep)["title"]
           == "[PREP] pack bag")
    _check("plain task unlabeled",
           A.calendar_projection_from_event(_ev())["title"]
           == "write report")
    # F1: body build uses the SAME title seam + timed dateTime span.
    body = A._build_cal_body(pres, "surfid001")
    _check("body summary == projection title (one seam)",
           body["summary"] == p["title"], repr(body["summary"]))
    _check("body start is a dateTime (not banner date)",
           "dateTime" in body.get("start", {}), repr(body.get("start")))
    # F1 echo: inbound projection of the pushed item hashes identically.
    item = {"summary": body["summary"], "description": "",
            "location": pres["location"], "status": "confirmed",
            "transparency": body["transparency"],
            "start": body["start"], "end": body["end"],
            "extendedProperties": body["extendedProperties"]}
    p_in = A.calendar_projection_from_item(item)
    _check("push/inbound presence projections hash-identical (echo)",
           A.projection_hash(p_in) == A.projection_hash(p),
           "\n    push=%r\n    inbd=%r" % (p, p_in))
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (e) inbound title strip
# ---------------------------------------------------------------------------

def test_strip_title_decorations():
    print("A1 inbound title strip (mirror prefixes never enter SoT):")
    # Config-less instance strips NOTHING (no declared prefixes).
    root = _build_scratch()
    A = _import_adapter(root)
    _check("config-less strips nothing (raw-key label kept as user text)",
           A._strip_title_decorations("presence Jeju") == "presence Jeju")
    _check("None tolerated (config-less)",
           A._strip_title_decorations(None) == "")
    shutil.rmtree(root, ignore_errors=True)

    # With kit labels + decor declared, the exact declared prefixes strip.
    root = _build_scratch(colors=dict(
        _KIT_LABELS, status_decor={"done": "D:", "expired": "X:"}))
    A = _import_adapter(root)
    _check("kit subtype label prefix stripped",
           A._strip_title_decorations("[MOVE] to office") == "to office")
    _check("kit status decor + label stripped",
           A._strip_title_decorations("D:[MOVE] to office") == "to office")
    _check("plain title untouched",
           A._strip_title_decorations("plain title") == "plain title")
    _check("None tolerated", A._strip_title_decorations(None) == "")
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (f) tolerance: malformed config / hostile resolve identifiers
# ---------------------------------------------------------------------------

def test_config_tolerance():
    print("A1 tolerance (malformed config degrades to no-color):")
    root = _build_scratch(colors="{not valid json!!")
    A = _import_adapter(root)
    mc = sys.modules["mirror_colors"]
    _check("malformed json -> seam off", mc.active() is False)
    _check("malformed json -> legacy markers intact",
           A.event_to_cal_status(_ev(status="done")) == ("confirmed", "10"))
    shutil.rmtree(root, ignore_errors=True)

    root = _build_scratch(colors={
        "color_source": "x",
        "color_map": {"1": "4"},
        "resolve": {"x": {"via": "area_id", "table": "areas; DROP TABLE e",
                          "key": "id", "select": "domain"}}})
    A = _import_adapter(root)
    mc = sys.modules["mirror_colors"]
    _check("hostile identifier -> resolve spec dropped",
           mc.resolves() == {}, repr(mc.resolves()))
    conn = sqlite3.connect(":memory:")
    ev = A.enrich_event(conn, _ev(area_id=1))
    _check("enrich no-crash with dropped spec", ev.get("x") is None)
    conn.close()
    shutil.rmtree(root, ignore_errors=True)

    root = _build_scratch(colors={"color_source": "y",
                                  "color_map": {"a": "3"},
                                  "resolve": {"y": {"via": "area_id",
                                                    "table": "missing_tbl",
                                                    "key": "id",
                                                    "select": "v"}}})
    A = _import_adapter(root)
    conn = sqlite3.connect(":memory:")
    ev = A.enrich_event(conn, _ev(area_id=1))
    _check("missing lookup table -> skip, no crash", ev.get("y") is None)
    conn.close()
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (g) A2: config-first constants, zero-delta fallbacks
# ---------------------------------------------------------------------------

A2_CANON = {
    "CB_CANCELLED_THRESHOLD": 10,
    "OUTBOX_MAX_ATTEMPTS": 5,
    "OUTBOX_LEASE_SECONDS": 300,
    "NOTIFY_AGG_THRESHOLD": 3,
    "SWEEP_GRACE_SECONDS": 0,
    "PLACEHOLDER_SECONDS": 3600,
    "NOTION_IMPORT_MIRROR_CUTOFF": "2026-07-12",
    "NOTION_IMPORT_MIRROR_MIN_START": "2026-07-12T02:00:00Z",
}


def test_a2_zero_delta():
    print("A2 zero-delta (no lifekit.conf -> prior canonical literals):")
    root = _build_scratch()
    A = _import_adapter(root)
    for name, want in sorted(A2_CANON.items()):
        got = getattr(A, name)
        _check("%s == %r" % (name, want), got == want, repr(got))
    _check("_acquire_drain_lock default lease bound at import == 300",
           A._acquire_drain_lock.__defaults__[-1] == 300,
           repr(A._acquire_drain_lock.__defaults__))
    shutil.rmtree(root, ignore_errors=True)


def test_a2_config_present():
    print("A2 config present (knobs reflect config):")
    root = _build_scratch(lifekit_conf=(
        "MIRROR_CB_CANCELLED_THRESHOLD=25\n"
        "MIRROR_OUTBOX_MAX_ATTEMPTS=9\n"
        "MIRROR_OUTBOX_LEASE_SECONDS=600\n"
        "MIRROR_NOTIFY_AGG_THRESHOLD=7\n"
        "MIRROR_SWEEP_GRACE_SECONDS=120\n"
        "MIRROR_PLACEHOLDER_SECONDS=1800\n"
        "MIRROR_NOTION_IMPORT_CUTOFF=2027-01-01\n"
        "MIRROR_NOTION_IMPORT_MIN_START=2027-01-01T00:00:00Z\n"))
    A = _import_adapter(root)
    _check("CB threshold from config", A.CB_CANCELLED_THRESHOLD == 25)
    _check("outbox attempts from config", A.OUTBOX_MAX_ATTEMPTS == 9)
    _check("outbox lease from config", A.OUTBOX_LEASE_SECONDS == 600)
    _check("notify agg from config", A.NOTIFY_AGG_THRESHOLD == 7)
    _check("sweep grace from config", A.SWEEP_GRACE_SECONDS == 120)
    _check("placeholder from config", A.PLACEHOLDER_SECONDS == 1800)
    _check("notion cutoff from config",
           A.NOTION_IMPORT_MIRROR_CUTOFF == "2027-01-01")
    _check("notion min-start from config",
           A.NOTION_IMPORT_MIRROR_MIN_START == "2027-01-01T00:00:00Z")
    # New-instance inheritance regression: the configured cutoff governs
    # in_mirror_scope, so an instance's import epoch is ITS config, not a
    # first-owner literal baked into base-kit.
    row = {"recurrence_id": "r1", "created_by": "notion-import",
           "rec_date": "2026-12-31", "start_at": "2026-12-31T01:00:00Z",
           "is_routine": 1}
    _check("import row below configured cutoff -> out of scope",
           A.in_mirror_scope(row) is False)
    shutil.rmtree(root, ignore_errors=True)


def test_a2_malformed_int_falls_back():
    print("A2 malformed int -> canonical fallback:")
    root = _build_scratch(lifekit_conf=(
        "MIRROR_CB_CANCELLED_THRESHOLD=lots\n"
        "MIRROR_SWEEP_GRACE_SECONDS=\n"))
    A = _import_adapter(root)
    _check("non-numeric -> default 10", A.CB_CANCELLED_THRESHOLD == 10)
    _check("empty -> default 0", A.SWEEP_GRACE_SECONDS == 0)
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main():
    test_zero_delta_no_config()
    test_generic_direct_axis()
    test_generic_fk_resolved_axis()
    test_d2_axis_conflict_ruling()
    test_timed_subtype_blocks_neutral_fallback()
    test_timed_subtype_blocks_kit_labels()
    test_strip_title_decorations()
    test_config_tolerance()
    test_a2_zero_delta()
    test_a2_config_present()
    test_a2_malformed_int_falls_back()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
