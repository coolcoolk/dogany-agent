#!/usr/bin/env python3
"""DGN-268 S1 gate: config seam / parameterization (zero behavior delta).

Asserts the pure-parameterization contract of stage S1:
  (a) NO config file present  -> every parameterized constant resolves to its
      prior canonical literal (the zero-delta guarantee).
  (b) config supplies MIRROR_CAL_NAME / MIRROR_TASKLIST_NAME / MIRROR_TZ /
      DOGANY_AGENT_NAME -> the resolved values reflect the config.
  (c) marker freeze: once the first bootstrap writes 'cal_marker' into the
      mirror_state KV, changing MIRROR_CAL_NAME / the agent slug does NOT
      change the resolved marker (a rename must not orphan the calendar).

The gws / SDK / http layers are STUBBED -- no network, no Google API, no
Telegram push. The adapter is imported from a throwaway scratch copy of the
mirror package so the ../config/lifekit.conf path resolution is exercised for
real (config file lives next to the scratch mirror dir, not the repo one).

Run: python3 tests/mirror/test_s1_config_seam.py    (exit 0 = pass)
Also runs under pytest if installed (plain test_* functions).
"""
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_MIRROR = os.path.join(REPO_ROOT, "mirror")

# Canonical literals as they stood BEFORE parameterization (the zero-delta
# reference). If any of these change, S1 is no longer pure parameterization.
CANON_CAL_SUMMARY = "<agent-calendar-name>"
CANON_TASKLIST_TITLE = "<agent-calendar-name>"   # H2: tasklist default = cal
CANON_TZ = "Asia/Seoul"

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _build_scratch(agent_name=None, cal_name=None, tasklist_name=None,
                   tz=None, agent_label=None):
    """Create scratch/mirror (copied package) + optional scratch config.
    Returns the scratch root. If all conf args are None, NO config file is
    written -> the fresh-checkout / zero-delta path."""
    root = tempfile.mkdtemp(prefix="dgn268-s1-")
    mirror_dir = os.path.join(root, "mirror")
    os.makedirs(mirror_dir)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_state.sql"):
        shutil.copy2(os.path.join(SRC_MIRROR, f), os.path.join(mirror_dir, f))

    lk_keys = {}
    if cal_name is not None:
        lk_keys["MIRROR_CAL_NAME"] = cal_name
    if tasklist_name is not None:
        lk_keys["MIRROR_TASKLIST_NAME"] = tasklist_name
    if tz is not None:
        lk_keys["MIRROR_TZ"] = tz
    if lk_keys:
        os.makedirs(os.path.join(root, "config"))
        with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
            fh.write("# scratch lifekit.conf\n")
            for k, v in lk_keys.items():
                fh.write("%s=%s\n" % (k, v))

    inst = {}
    if agent_name is not None:
        inst["DOGANY_AGENT_NAME"] = agent_name
    if agent_label is not None:
        inst["DOGANY_AGENT_LABEL"] = agent_label
    if inst:
        with open(os.path.join(root, ".instance.conf"), "w") as fh:
            fh.write("# scratch .instance.conf\n")
            for k, v in inst.items():
                fh.write("%s=%s\n" % (k, v))
    return root


def _import_adapter(root):
    """Fresh import of the scratch adapter with surface deps stubbed."""
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
    mod._reset_conf_cache()   # ignore any cache from a prior import
    return mod


# ---------------------------------------------------------------------------
# (a) zero-delta: no config -> prior canonical literals
# ---------------------------------------------------------------------------

def test_zero_delta_no_config():
    print("zero-delta (no config file):")
    root = _build_scratch()   # nothing written
    A = _import_adapter(root)
    _check("CAL_SUMMARY == canonical placeholder",
           A.SANDBOX_CAL_SUMMARY == CANON_CAL_SUMMARY,
           repr(A.SANDBOX_CAL_SUMMARY))
    _check("TASKLIST_TITLE == CAL_SUMMARY (H2 default)",
           A.SANDBOX_TASKLIST_TITLE == CANON_TASKLIST_TITLE,
           repr(A.SANDBOX_TASKLIST_TITLE))
    _check("DISPLAY_TZ_NAME falls back to Asia/Seoul",
           A.DISPLAY_TZ_NAME == CANON_TZ, repr(A.DISPLAY_TZ_NAME))
    _check("_load_conf() is empty on fresh checkout",
           A._load_conf() == {}, repr(A._load_conf()))
    _check("marker derives dogany-mirror-agent (default slug)",
           A.CAL_DESCRIPTION_MARKER == "dogany-mirror-agent",
           repr(A.CAL_DESCRIPTION_MARKER))
    # H4 default-arg zero-delta: signature defaults bound at import must equal
    # the prior literal too (they are what all the projection math uses).
    _check("utc_instant default display_tz == Asia/Seoul",
           A.utc_instant_to_gcal_datetime.__defaults__[-1] == CANON_TZ,
           repr(A.utc_instant_to_gcal_datetime.__defaults__))


# ---------------------------------------------------------------------------
# (b) config-present -> values reflect config
# ---------------------------------------------------------------------------

def test_config_present_reflects_values():
    print("config present (values reflect config):")
    root = _build_scratch(agent_name="testbot", cal_name="TESTNAME",
                          tasklist_name="TESTTASKS", tz="America/New_York")
    A = _import_adapter(root)
    _check("CAL_SUMMARY == MIRROR_CAL_NAME",
           A.SANDBOX_CAL_SUMMARY == "TESTNAME", repr(A.SANDBOX_CAL_SUMMARY))
    _check("TASKLIST_TITLE == MIRROR_TASKLIST_NAME",
           A.SANDBOX_TASKLIST_TITLE == "TESTTASKS",
           repr(A.SANDBOX_TASKLIST_TITLE))
    _check("DISPLAY_TZ_NAME == MIRROR_TZ",
           A.DISPLAY_TZ_NAME == "America/New_York", repr(A.DISPLAY_TZ_NAME))
    _check("marker == dogany-mirror-<agent slug>",
           A.CAL_DESCRIPTION_MARKER == "dogany-mirror-testbot",
           repr(A.CAL_DESCRIPTION_MARKER))


def test_tasklist_defaults_to_cal_name():
    print("H2: tasklist name defaults to calendar name when unset:")
    root = _build_scratch(cal_name="ONLYCAL")   # no MIRROR_TASKLIST_NAME
    A = _import_adapter(root)
    _check("TASKLIST_TITLE inherits MIRROR_CAL_NAME",
           A.SANDBOX_TASKLIST_TITLE == "ONLYCAL",
           repr(A.SANDBOX_TASKLIST_TITLE))


def test_cal_name_falls_back_to_agent_label():
    print("H1: cal name falls back to .instance.conf agent label:")
    root = _build_scratch(agent_label=u"김메탈")  # 김메탈
    A = _import_adapter(root)
    _check("CAL_SUMMARY == DOGANY_AGENT_LABEL when MIRROR_CAL_NAME absent",
           A.SANDBOX_CAL_SUMMARY == u"김메탈",
           repr(A.SANDBOX_CAL_SUMMARY))


# ---------------------------------------------------------------------------
# (c) marker freeze: rename must not change the resolved marker
# ---------------------------------------------------------------------------

def test_marker_freeze_survives_rename():
    print("H3: marker freeze survives a rename:")
    # First bootstrap: agent 'origbot' -> marker dogany-mirror-origbot.
    root = _build_scratch(agent_name="origbot")
    A = _import_adapter(root)
    state = A.open_state_db()          # fresh scratch state db (next to module)
    frozen = A._frozen_cal_marker(state)
    _check("first freeze == derived marker",
           frozen == "dogany-mirror-origbot", repr(frozen))
    _check("KV now holds the frozen marker",
           A.get_state(state, "cal_marker") == "dogany-mirror-origbot",
           repr(A.get_state(state, "cal_marker")))

    # Now simulate a rename: the DERIVED marker would change, but the KV is
    # authoritative. Monkeypatch the derived value and re-read from KV.
    A.CAL_DESCRIPTION_MARKER = "dogany-mirror-renamedbot"
    still = A._frozen_cal_marker(state)
    _check("frozen marker unchanged after rename (no orphan)",
           still == "dogany-mirror-origbot", repr(still))
    state.close()


# ---------------------------------------------------------------------------
# runner (plain style; pytest also collects the test_* functions above)
# ---------------------------------------------------------------------------

def main():
    test_zero_delta_no_config()
    test_config_present_reflects_values()
    test_tasklist_defaults_to_cal_name()
    test_cal_name_falls_back_to_agent_label()
    test_marker_freeze_survives_rename()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
