#!/usr/bin/env python3
"""DGN-268 S5 gate: poll_cycle per-step exception isolation.

Real defect (Ag, 2026-07-12 09:21): one transient GwsError 404 in
pull_calendar aborted the WHOLE poll_cycle before outbox_drain ran, so nothing
got pushed -- the outbound lane was starved by an inbound 404. S5 isolates each
step (sweep / pull_calendar / pull_tasks / drain) so a failure in one does not
abort the others, and the DRAIN always runs.

Checks:
  1. pull_calendar raises GwsError(404) -> no exception propagates; sweep result
     present; tasks pulled; DRAIN STILL RAN; cycle["calendar"] carries the error
     (dict with error/http_code/transient), tagged transient; a persistent-error
     log row exists is NOT required, a transient one is.
  2. a persistent (non-retryable, non-404) GwsError -> tagged transient=False.
  3. all-success cycle -> byte-identical to the pre-S5 happy-path shape (the
     four keys hold the raw step returns, no {"error": ...} wrappers).
  4. a non-GwsError exception in a step -> caught, recorded, no propagation.

The step functions are monkeypatched on the adapter module (no gws, no DB
writes needed for the isolation logic). mirror_log is redirected to an
in-memory list so we can assert the failure was logged.

Run: python3 tests/mirror/test_s5_poll_isolation.py   (exit 0 = pass)
"""
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


def _import_adapter():
    """Import the real adapter with surface deps stubbed (no gws/DB/network)."""
    for name in ("adapter", "sdk_bridge", "notify", "http_direct",
                 "mirror_i18n"):
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
    sys.path.insert(0, SRC_MIRROR)
    import adapter  # noqa: F401
    return sys.modules["adapter"]


def _harness(A):
    """Redirect mirror_log to an in-memory list; return (logs, calls)."""
    logs = []
    calls = []
    A.mirror_log = lambda conn, cat, ulid=None, detail=None: logs.append(
        (cat, detail))
    return logs, calls


def test_pull_raises_drain_still_runs():
    print("(1) pull_calendar 404 -> drain still runs, no propagation:")
    A = _import_adapter()
    logs, calls = _harness(A)

    A.sweep_step = lambda s, src: (calls.append("sweep") or ["swept"])
    def _pull_cal(cal, s, src):
        calls.append("calendar")
        raise A.GwsError("boom 404", code=404)
    A.pull_calendar = _pull_cal
    A.pull_tasks = lambda tl, s, src: (calls.append("tasks") or ["t1"])
    A.outbox_drain = lambda s, src, cal, tl: (
        calls.append("drain") or {"pushed": 2, "status": "ok"})

    raised = None
    try:
        out = A.poll_cycle("STATE", "SRC", "CAL", "TL")
    except Exception as e:  # noqa: BLE001
        raised = e
    _check("no exception propagates", raised is None, repr(raised))
    _check("sweep ran", "sweep" in calls, calls)
    _check("tasks pulled after calendar failure", "tasks" in calls, calls)
    _check("DRAIN STILL RAN", "drain" in calls, calls)
    _check("drain ran AFTER the failed pull (order preserved)",
           calls == ["sweep", "calendar", "tasks", "drain"], calls)
    _check("sweep result present", out.get("sweep") == ["swept"], out.get("sweep"))
    _check("tasks result present", out.get("tasks") == ["t1"], out.get("tasks"))
    _check("drain result present", out.get("drain", {}).get("pushed") == 2,
           out.get("drain"))
    cal = out.get("calendar")
    _check("calendar carries error dict",
           isinstance(cal, dict) and "error" in cal, cal)
    _check("calendar error tagged transient (404)",
           isinstance(cal, dict) and cal.get("transient") is True
           and cal.get("http_code") == 404, cal)
    _check("transient failure was logged",
           any(c == "cycle_step_transient" for c, _d in logs), logs)


def test_persistent_error_tagged():
    print("(2) persistent (non-retryable) error tagged transient=False:")
    A = _import_adapter()
    logs, calls = _harness(A)
    A.sweep_step = lambda s, src: []
    def _pull_cal(cal, s, src):
        raise A.GwsError("bad request", code=400)  # 400 not in RETRYABLE_HTTP
    A.pull_calendar = _pull_cal
    A.pull_tasks = lambda tl, s, src: []
    A.outbox_drain = lambda s, src, cal, tl: {"pushed": 0}
    out = A.poll_cycle("S", "SR", "C", "T")
    cal = out["calendar"]
    _check("persistent error transient=False",
           cal.get("transient") is False and cal.get("http_code") == 400, cal)
    _check("persistent failure logged at persistent category",
           any(c == "cycle_step_persistent" for c, _d in logs), logs)
    _check("drain still ran after persistent pull error",
           out.get("drain") == {"pushed": 0}, out.get("drain"))


def test_all_success_zero_delta():
    print("(3) all-success cycle -> pre-S5 happy-path shape (zero-delta):")
    A = _import_adapter()
    _harness(A)
    A.sweep_step = lambda s, src: ["s1", "s2"]
    A.pull_calendar = lambda cal, s, src: ["c1"]
    A.pull_tasks = lambda tl, s, src: ["t1", "t2"]
    A.outbox_drain = lambda s, src, cal, tl: {"pushed": 3, "status": "ok"}
    out = A.poll_cycle("S", "SR", "C", "T")
    # Exactly the five keys, holding the raw step returns (no error wrappers).
    # DGN-333 added overlap_recheck; pin updated DGN-364 gate
    _check("keys unchanged", set(out) == {"sweep", "calendar", "tasks", "drain", "overlap_recheck"},
           set(out))
    _check("sweep raw return", out["sweep"] == ["s1", "s2"], out["sweep"])
    _check("calendar raw return", out["calendar"] == ["c1"], out["calendar"])
    _check("tasks raw return", out["tasks"] == ["t1", "t2"], out["tasks"])
    _check("drain raw return", out["drain"] == {"pushed": 3, "status": "ok"},
           out["drain"])
    _check("no error wrapper on any step",
           not any(isinstance(v, dict) and "error" in v for v in out.values()),
           out)


def test_non_gws_exception_caught():
    print("(4) non-GwsError in a step -> caught, recorded, no propagation:")
    A = _import_adapter()
    logs, calls = _harness(A)
    A.sweep_step = lambda s, src: (_ for _ in ()).throw(RuntimeError("weird"))
    A.pull_calendar = lambda cal, s, src: (calls.append("calendar") or [])
    A.pull_tasks = lambda tl, s, src: []
    A.outbox_drain = lambda s, src, cal, tl: (calls.append("drain") or {"pushed": 0})
    raised = None
    try:
        out = A.poll_cycle("S", "SR", "C", "T")
    except Exception as e:  # noqa: BLE001
        raised = e
    _check("no exception propagates", raised is None, repr(raised))
    _check("sweep error recorded",
           isinstance(out.get("sweep"), dict) and "error" in out["sweep"],
           out.get("sweep"))
    _check("later steps still ran (calendar + drain)",
           "calendar" in calls and "drain" in calls, calls)
    _check("non-gws error logged as persistent",
           any(c == "cycle_step_persistent" for c, _d in logs), logs)


def main():
    test_pull_raises_drain_still_runs()
    test_persistent_error_tagged()
    test_all_success_zero_delta()
    test_non_gws_exception_caught()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
