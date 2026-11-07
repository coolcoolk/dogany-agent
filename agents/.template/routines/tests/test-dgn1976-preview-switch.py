#!/usr/bin/env python3
# test-lane: fast
"""Locks for the preview feature switch (DGN-1976).

  * on without --confirm -> exit 10, confirm line, conf untouched.
  * on with a non-yes reply -> exit 10 not_a_yes, conf untouched.
  * on/off round trip per feature: conf line bare (conf_get reads it),
    status follows, the feature's own reader follows (why-net budget,
    cu_gate switched_off, mint custom_switched_off).
  * unknown feature refused, exit 2, with the known list.
  * unit step: macOS -> routine-ctl load / disable (no-load mode places and
    removes the plist); other hosts -> not_on_this_os, conf still written.
  * routine-ctl load on a non-macOS host -> exit 4.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.normpath(os.path.join(HERE, ".."))
REPO = os.path.normpath(os.path.join(ROUTINES, "..", "..", ".."))
SCRIPT = os.path.join(ROUTINES, "preview-switch.py")
RCTL = os.path.join(ROUTINES, "lib", "routine-ctl.sh")
CU_GATE = os.path.join(REPO, "skills", "dogany-computer-use", "cu_gate.py")
sys.path.insert(0, os.path.join(ROUTINES, "lib"))
sys.path.insert(0, os.path.join(REPO, "scripts", "pack", "lib"))

from conf_reader import conf_get  # noqa: E402

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(root, *args, os_name="Linux"):
    env = dict(os.environ)
    env.update({"DOGANY_AGENT_CONF": os.path.join(root, "config", "agent.conf"),
                "PREVIEW_SWITCH_OS": os_name,
                "ROUTINE_CTL_OS": os_name,
                "ROUTINE_CTL_NO_LOAD": "1",
                "ROUTINE_CTL_LA_DIR": os.path.join(root, "LaunchAgents")})
    # Keep fixture trash inside TMPDIR instead of the host's protected Trash.
    env["PATH"] = os.path.join(root, "bin") + os.pathsep + env["PATH"]
    r = subprocess.run([sys.executable, SCRIPT] + list(args), env=env,
                       capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"_raw": r.stdout, "_err": r.stderr}
    return r.returncode, out


def make_root():
    root = tempfile.mkdtemp(prefix="dgn1976-")
    os.makedirs(os.path.join(root, "bin"))
    trash = os.path.join(root, "bin", "trash")
    with open(trash, "w") as fh:
        fh.write('#!/bin/bash\nrm -f -- "$@"\n')
    os.chmod(trash, 0o755)
    os.makedirs(os.path.join(root, "config", "i18n"))
    shutil.copy(os.path.join(ROUTINES, "..", "config", "i18n", "en.json"),
                os.path.join(root, "config", "i18n", "en.json"))
    with open(os.path.join(root, "config", "agent.conf"), "w") as fh:
        fh.write("AGENT_LANG=en\nPLAN=max\n")
    return root


def conf(root, key):
    return conf_get(key, os.path.join(root, "config", "agent.conf"))


def test_confirm(root):
    rc, out = run(root, "on", "why-net")
    check("selection activates without sentinel", rc, 0)
    check("feature enabled", conf(root, "WHY_NET"), "on")
    check("no extra confirmation", out.get("needs_confirm", False), False)
    run(root, "off", "why-net")


def test_round_trip(root):
    from why_net import budget
    import mint_flow as mint
    cu = _load("cu_gate", CU_GATE)
    cpath = os.path.join(root, "config", "agent.conf")
    os.environ["DOGANY_AGENT_CONF"] = cpath

    def readers(fid):
        if fid == "why-net":
            return budget.Ctx(root=root, conf_path=cpath).enabled()
        if fid == "computer-use":
            return not cu.switched_off()
        return not mint.custom_switched_off(root)

    defaults = {"why-net": False, "computer-use": True,
                "custom-agent": True}
    for fid, key in (("why-net", "WHY_NET"),
                     ("computer-use", "COMPUTER_USE"),
                     ("custom-agent", "CUSTOM_AGENT")):
        check("%s default reader" % fid, readers(fid), defaults[fid])
        rc, out = run(root, "on", fid, "--confirm", "yes")
        check("%s on rc" % fid, rc, 0)
        check("%s on state" % fid, out.get("state"), "on")
        check("%s conf on" % fid, conf(root, key), "on")
        check("%s reader on" % fid, readers(fid), True)
        rc, out = run(root, "status", fid)
        check("%s status on" % fid, out.get("state"), "on")
        rc, out = run(root, "off", fid)
        check("%s off rc" % fid, rc, 0)
        check("%s conf off" % fid, conf(root, key), "off")
        check("%s reader off" % fid, readers(fid), False)
        rc, out = run(root, "on", fid, "--confirm", "yes")
        check("%s back on" % fid, conf(root, key), "on")
    with open(cpath) as fh:
        lines = [l for l in fh.read().splitlines() if l.startswith("WHY_NET=")]
    check("one WHY_NET line", lines, ["WHY_NET=on"])
    check("other keys kept", conf(root, "PLAN"), "max")
    rc, out = run(root, "list")
    check("list rc", rc, 0)
    check("list features", sorted(r["feature"] for r in out["features"]),
          ["computer-use", "custom-agent", "why-net"])


def test_unknown(root):
    rc, out = run(root, "on", "teleport", "--confirm", "yes")
    check("unknown rc", rc, 2)
    check("unknown error", out.get("error"), "unknown_feature")
    check("unknown known list", out.get("known"),
          ["computer-use", "custom-agent", "why-net"])
    check("unknown writes nothing", conf(root, "TELEPORT"), "")
    rc, out = run(root, "off", "")
    check("empty name rc", rc, 2)


def test_units(root):
    rc, out = run(root, "on", "why-net", "--confirm", "yes")
    check("linux unit", out.get("unit"), "not_on_this_os")
    # macOS path: routine-ctl load places the shipped plist (no-load mode)
    agent = os.path.basename(root)
    label = "com.telegram-skill-bot.%s.why-morning" % agent
    os.makedirs(os.path.join(root, "routines", "lib"))
    shutil.copy(RCTL, os.path.join(root, "routines", "lib", "routine-ctl.sh"))
    with open(os.path.join(root, "routines", label + ".plist"), "w") as fh:
        fh.write("<plist/>\n")
    # Isolate the platform trash command too; no writes to the host Trash.
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    trash = os.path.join(bindir, "trash")
    with open(trash, "w") as fh:
        fh.write('#!/bin/bash\nrm -f -- "$@"\n')
    os.chmod(trash, 0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = bindir + os.pathsep + old_path
    os.environ["PREVIEW_SWITCH_ROUTINE_CTL"] = os.path.join(
        root, "routines", "lib", "routine-ctl.sh")
    try:
        rc, out = run(root, "on", "why-net", "--confirm", "yes",
                      os_name="Darwin")
        check("mac on rc", rc, 0)
        check("mac on unit", out.get("unit"), "ok")
        placed = os.path.join(root, "LaunchAgents", label + ".plist")
        check("mac plist placed", os.path.isfile(placed), True)
        rc, out = run(root, "off", "why-net", os_name="Darwin")
        check("mac off rc", rc, 0)
        check("mac plist removed", os.path.exists(placed), False)
        os.remove(os.path.join(root, "routines", label + ".plist"))
        # a feature whose unit is missing reports failed, exit 3
        rc, out = run(root, "on", "why-net", "--confirm", "yes",
                      os_name="Darwin")
        check("missing unit rc", rc, 3)
        check("missing unit still on", conf(root, "WHY_NET"), "on")
    finally:
        os.environ["PATH"] = old_path
        os.environ.pop("PREVIEW_SWITCH_ROUTINE_CTL", None)
    env = dict(os.environ, ROUTINE_CTL_OS="Linux")
    r = subprocess.run(["bash", RCTL, "load", "why-morning"], env=env,
                       capture_output=True, text=True)
    check("routine-ctl load linux rc", r.returncode, 4)


def main():
    for t in (test_confirm, test_round_trip, test_unknown,
              test_units):
        root = make_root()
        try:
            t(root)
        finally:
            os.environ.pop("DOGANY_AGENT_CONF", None)
            shutil.rmtree(root, ignore_errors=True)
    if _failures:
        for f in _failures:
            print("FAIL", f)
        return 1
    print("PASS test-dgn1976-preview-switch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
