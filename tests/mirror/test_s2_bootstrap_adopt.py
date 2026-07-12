#!/usr/bin/env python3
"""DGN-268 S2 gate: bootstrap adopt-or-create guard.

A general user may already own a calendar/tasklist whose name collides with
the agent's. S1 re-discovery would silently adopt that foreign surface and
lifekit would start writing into it. S2 fixes the policy:

  marker match (our marker in the description)      -> adopt (ours).
  bare summary/title match WITHOUT our marker       -> AMBIGUOUS: never
      auto-adopt/create; raise BootstrapAmbiguous so onboarding can ask.
  same, but MIRROR_ADOPT_UNMARKED=true (user said 'adopt') -> adopt + stamp
      our marker (calendar) so the next run is unambiguous.
  no match                                          -> create + stamp.

Cases:
  (a) nothing exists            -> creates cal+tasklist, marker stamped.
  (b) marker-match calendar     -> adopts, NO insert.
  (c) summary-match, gate unset -> BootstrapAmbiguous, NO insert, NO adopt.
  (d) summary-match, gate=true  -> adopts + stamps marker (patch called).
  (e) tasklist title collision  -> BootstrapAmbiguous (same guard).

Uses a FAKE gws layer (records every call; no network). Runs against the
scratch mirror copy so ../config/lifekit.conf resolution is exercised.

Run: python3 tests/mirror/test_s2_bootstrap_adopt.py   (exit 0 = pass)
"""
import json
import os
import shutil
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


def _build_scratch(adopt_unmarked=None, agent_name="testbot", cal_name=None):
    """scratch/mirror copy + optional config (MIRROR_ADOPT_UNMARKED, cal
    name, agent slug)."""
    root = tempfile.mkdtemp(prefix="dgn268-s2-")
    mirror_dir = os.path.join(root, "mirror")
    os.makedirs(mirror_dir)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_state.sql"):
        shutil.copy2(os.path.join(SRC_MIRROR, f), os.path.join(mirror_dir, f))
    os.makedirs(os.path.join(root, "config"))
    with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
        fh.write("# scratch\n")
        if cal_name is not None:
            fh.write("MIRROR_CAL_NAME=%s\n" % cal_name)
        if adopt_unmarked is not None:
            fh.write("MIRROR_ADOPT_UNMARKED=%s\n" % adopt_unmarked)
    with open(os.path.join(root, ".instance.conf"), "w") as fh:
        fh.write("DOGANY_AGENT_NAME=%s\n" % agent_name)
    return root


def _import_adapter(root):
    for name in ("adapter", "sdk_bridge", "notify", "http_direct"):
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


class FakeGws(object):
    """Records every gws(...) call and answers list/get from a fixture.
    cal_items / tl_items are the calendarList / tasklists 'list' contents.
    insert/patch return synthetic ids and are recorded for assertions."""
    def __init__(self, cal_items=None, tl_items=None):
        self.cal_items = cal_items or []
        self.tl_items = tl_items or []
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        a = args
        if a[:3] == ("calendar", "calendarList", "list"):
            return {"items": self.cal_items}
        if a[:3] == ("calendar", "calendars", "list"):
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
            return {"id": "NEWCAL"}
        if a[:3] == ("calendar", "calendars", "patch"):
            return {"id": "PATCHED"}
        if a[:3] == ("tasks", "tasklists", "insert"):
            return {"id": "NEWTL"}
        if a[:3] == ("tasks", "tasklists", "get"):
            return {}
        raise AssertionError("unexpected gws call: %s" % (a,))

    def verbs(self):
        return [args[:3] for args, _ in self.calls]

    def has(self, triple):
        return triple in self.verbs()


def _fresh_state(A):
    return A.open_state_db()


def test_a_nothing_exists_creates_and_stamps():
    print("(a) nothing exists -> create cal+tasklist, marker stamped:")
    root = _build_scratch()
    A = _import_adapter(root)
    A.gws = FakeGws(cal_items=[], tl_items=[])
    state = _fresh_state(A)
    cal_id, tl_id = A.bootstrap(state)
    _check("calendar created", cal_id == "NEWCAL", cal_id)
    _check("tasklist created", tl_id == "NEWTL", tl_id)
    # created description carries the frozen marker + product text, not sandbox
    insert_call = [kw for args, kw in A.gws.calls
                   if args[:3] == ("calendar", "calendars", "insert")][0]
    desc = insert_call["body"]["description"]
    _check("marker stamped in created description",
           "dogany-mirror-testbot" in desc, desc)
    _check("description is the product text (not 'safe to delete')",
           "Safe to edit; do not delete." in desc and "sandbox" not in desc,
           desc)
    state.close()


def test_b_marker_match_adopts_no_insert():
    print("(b) marker-match calendar -> adopt, NO insert:")
    root = _build_scratch()
    A = _import_adapter(root)
    marker = "dogany-mirror-testbot"
    A.gws = FakeGws(
        cal_items=[{"id": "MINE", "summary": "whatever",
                    "description": "%s -- Managed by the agent" % marker}],
        tl_items=[])
    state = _fresh_state(A)
    cal_id, _tl = A.bootstrap(state)
    _check("adopted the marker-match calendar", cal_id == "MINE", cal_id)
    _check("NO calendar insert happened",
           not A.gws.has(("calendar", "calendars", "insert")),
           str(A.gws.verbs()))
    state.close()


def test_c_summary_match_gate_off_signals_ambiguous():
    print("(c) summary-match, gate unset -> ambiguous, NO insert/adopt:")
    root = _build_scratch(cal_name="MyCal")  # MIRROR_ADOPT_UNMARKED unset
    A = _import_adapter(root)
    A.gws = FakeGws(
        cal_items=[{"id": "FOREIGN", "summary": "MyCal",
                    "description": "the user's own unrelated calendar"}],
        tl_items=[])
    state = _fresh_state(A)
    raised = None
    try:
        A.bootstrap(state)
    except A.BootstrapAmbiguous as e:
        raised = e
    _check("BootstrapAmbiguous raised", raised is not None,
           "no exception")
    if raised is not None:
        cand = raised.candidates
        _check("candidate is the foreign calendar",
               any(c["candidate_id"] == "FOREIGN"
                   and c["surface"] == "calendar" for c in cand),
               str(cand))
    _check("NO calendar insert happened",
           not A.gws.has(("calendar", "calendars", "insert")),
           str(A.gws.verbs()))
    _check("NO calendar patch (adopt-stamp) happened",
           not A.gws.has(("calendar", "calendars", "patch")),
           str(A.gws.verbs()))
    _check("agent_calendar_id NOT set (no partial bootstrap)",
           A.get_state(state, "agent_calendar_id") is None,
           repr(A.get_state(state, "agent_calendar_id")))
    state.close()


def test_d_summary_match_gate_on_adopts_and_stamps():
    print("(d) summary-match, gate=true -> adopt + stamp marker:")
    root = _build_scratch(adopt_unmarked="true", cal_name="MyCal")
    A = _import_adapter(root)
    A.gws = FakeGws(
        cal_items=[{"id": "FOREIGN", "summary": "MyCal",
                    "description": "the user's own calendar"}],
        tl_items=[])
    state = _fresh_state(A)
    cal_id, _tl = A.bootstrap(state)
    _check("adopted the summary-match calendar", cal_id == "FOREIGN", cal_id)
    _check("marker STAMPED via calendars patch",
           A.gws.has(("calendar", "calendars", "patch")),
           str(A.gws.verbs()))
    # the patch body must contain our marker, and preserve the user's text
    patch = [kw for args, kw in A.gws.calls
             if args[:3] == ("calendar", "calendars", "patch")][0]
    pdesc = patch["body"]["description"]
    _check("stamp keeps user's original description text",
           "the user's own calendar" in pdesc and
           "dogany-mirror-testbot" in pdesc, pdesc)
    _check("NO calendar insert (adopted, not created)",
           not A.gws.has(("calendar", "calendars", "insert")),
           str(A.gws.verbs()))
    state.close()


def test_e_tasklist_title_collision_signals_ambiguous():
    print("(e) tasklist title collision, gate off -> ambiguous:")
    # calendar side clean (no collision) so ONLY the tasklist is ambiguous.
    root = _build_scratch(cal_name="UniqueCal")
    A = _import_adapter(root)
    A.gws = FakeGws(
        cal_items=[],  # no calendar collision -> would create
        tl_items=[{"id": "FOREIGNTL", "title": "UniqueCal"}])  # title == cal
    state = _fresh_state(A)
    raised = None
    try:
        A.bootstrap(state)
    except A.BootstrapAmbiguous as e:
        raised = e
    _check("BootstrapAmbiguous raised for tasklist", raised is not None,
           "no exception")
    if raised is not None:
        _check("candidate is the foreign tasklist",
               any(c["candidate_id"] == "FOREIGNTL"
                   and c["surface"] == "tasklist"
                   for c in raised.candidates), str(raised.candidates))
    _check("NO tasklist insert happened",
           not A.gws.has(("tasks", "tasklists", "insert")),
           str(A.gws.verbs()))
    # No partial bootstrap: even though the calendar had no collision, the
    # ambiguous tasklist blocks the whole run -> calendar not created either.
    _check("NO calendar insert (whole run blocked, no partial state)",
           not A.gws.has(("calendar", "calendars", "insert")),
           str(A.gws.verbs()))
    _check("agent_tasklist_id NOT set",
           A.get_state(state, "agent_tasklist_id") is None,
           repr(A.get_state(state, "agent_tasklist_id")))
    state.close()


def main():
    test_a_nothing_exists_creates_and_stamps()
    test_b_marker_match_adopts_no_insert()
    test_c_summary_match_gate_off_signals_ambiguous()
    test_d_summary_match_gate_on_adopts_and_stamps()
    test_e_tasklist_title_collision_signals_ambiguous()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
