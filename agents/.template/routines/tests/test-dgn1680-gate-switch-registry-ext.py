#!/usr/bin/env python3
# test-lane: fast
"""Locks for the gate-switch instance registry extension (DGN-1680).

An instance registers a local gate in config/gate-switch.custom.conf
instead of editing the canonical GATES dict.  Contract:
  * no file -> no-op (canonical registry only, no stderr noise).
  * one valid row -> resolvable, switchable via off/on, listed in status,
    and honored by the master (GATE_ALL=off turns it off).
  * slug collision with a canonical gate -> canonical kept, one stderr line.
  * key collision (canonical key, master key) -> row dropped, loud.
  * malformed rows (field count, slug, key, kind, empty script) -> skipped
    loudly, valid neighbours still load, exit code stays 0 (hooks).
  * binary garbage file -> no crash.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "gate-switch.py"))

_failures = []


def check(name, got, want):
    if got != want:
        _failures.append("%s: got %r, want %r" % (name, got, want))


def check_in(name, needle, hay):
    if needle not in hay:
        _failures.append("%s: %r not in %r" % (name, needle, hay[:400]))


def run(cfgdir, *args):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GATE_") and k != "DOGANY_SEED_GATE"}
    env["GATE_SWITCH_CONF"] = os.path.join(cfgdir, "agent.conf")
    env["GATE_SWITCH_LOG"] = os.path.join(cfgdir, "suppressed.log")
    p = subprocess.run([sys.executable, SCRIPT] + list(args), env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


def resolve(cfgdir, slug, extra_env=None):
    code = ("import importlib.util as u;"
            "s=u.spec_from_file_location('gs',%r);m=u.module_from_spec(s);"
            "s.loader.exec_module(m);print(m.resolve(%r))" % (SCRIPT, slug))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GATE_")}
    env["GATE_SWITCH_CONF"] = os.path.join(cfgdir, "agent.conf")
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.stdout.strip(), p.stderr


def mkcfg(ext=None, raw=None):
    d = tempfile.mkdtemp(prefix="gs1680-")
    open(os.path.join(d, "agent.conf"), "w").write("")
    p = os.path.join(d, "gate-switch.custom.conf")
    if ext is not None:
        open(p, "w", encoding="utf-8").write(ext)
    if raw is not None:
        open(p, "wb").write(raw)
    return d


PARK = "park-gate | GATE_PARK | blocking | park-gate-guard.py\n"

# 1. no file: no-op
d = mkcfg()
rc, out, err = run(d, "status")
check("nofile rc", rc, 0)
check("nofile stderr", err, "")
check("nofile no instance rows", "instance row" in out, False)
out1, err1 = resolve(d, "park-gate")
check_in("nofile unknown slug", "unknown gate", out1)

# 2. one extra row
d = mkcfg("# instance gates\n" + PARK)
rc, out, err = run(d, "status")
check("row rc", rc, 0)
check("row stderr", err, "")
check_in("row in status", "park-gate", out)
check_in("row marked", "instance row", out)
out1, _ = resolve(d, "park-gate")
check("row default on", out1, "(True, 'default on')")
rc, out, err = run(d, "off", "park-gate", "--reason", "t")
check("row off rc", rc, 0)
check_in("row off conf key", "GATE_PARK=off", open(
    os.path.join(d, "agent.conf")).read())
out1, _ = resolve(d, "park-gate")
check_in("row off resolves", "False", out1)
rc, out, err = run(d, "on", "park-gate")
check("row on rc", rc, 0)
check("row on clears key", "GATE_PARK" in open(
    os.path.join(d, "agent.conf")).read(), False)
out1, _ = resolve(d, "park-gate", {"GATE_ALL": "off"})
check_in("row honors master", "False", out1)
# stop-nag row takes the stop master
d = mkcfg("stopper | GATE_STOP_X | stop-nag | x.py --stop | LEGACY_X\n")
out1, _ = resolve(d, "stopper", {"GATE_STOP_ALL": "off"})
check_in("stop row master", "False", out1)
out1, _ = resolve(d, "stopper", {"GATE_ALL": "off"})
check_in("stop row ignores blocking master", "True", out1)
out1, _ = resolve(d, "stopper", {"LEGACY_X": "off"})
check_in("legacy alias", "False", out1)

# 3. collisions: canonical wins, logged
d = mkcfg("done-claim | GATE_MINE | blocking | evil.py\n"
          "other | GATE_DONE_CLAIM | blocking | evil.py\n"
          "third | GATE_ALL | blocking | evil.py\n"
          "fourth | GATE_FOURTH | blocking | ok.py | DOGANY_SEED_GATE\n"
          + PARK + PARK)
rc, out, err = run(d, "status")
check("collision rc", rc, 0)
check("collision slug msg once", err.count("slug 'done-claim' collides"), 1)
check_in("collision key msg", "'other' collides", err)
check_in("collision master msg", "'third' collides", err)
check_in("collision legacy msg", "'fourth' collides", err)
check_in("dup msg", "duplicated", err)
check("canonical script kept", "evil.py" in out, False)
out1, _ = resolve(d, "done-claim", {"GATE_DONE_CLAIM": "off"})
check_in("canonical key still drives done-claim", "False", out1)
out1, _ = resolve(d, "other")
check_in("dropped row unknown", "unknown gate", out1)
_, err1 = resolve(d, "done-claim")
check("printed once per process", err1.count("slug 'done-claim' collides"), 1)
check_in("status lists problems", "problem(s)", out)

# 4. malformed rows: loud skip, neighbours survive
d = mkcfg("just-one-field\n"
          "Bad_Slug | GATE_A | blocking | a.py\n"
          "s2 | NOTGATE | blocking | a.py\n"
          "s3 | GATE_S3 | sometimes | a.py\n"
          "s4 | GATE_S4 | blocking |\n"
          "s5 | GATE_S5 | blocking | a.py | lower-case\n"
          + PARK)
rc, out, err = run(d, "status")
check("malformed rc", rc, 0)
for frag in ("want 'slug", "bad slug", "bad key", "bad kind",
             "empty script", "bad legacy"):
    check_in("malformed " + frag, frag, err)
check("malformed lines", err.count("row skipped"), 6)
check_in("valid neighbour loaded", "park-gate", out)
out1, _ = resolve(d, "park-gate")
check("neighbour resolves", out1, "(True, 'default on')")

# 5. binary garbage: no crash
d = mkcfg(raw=b"\xff\xfe\x00\x01|||\n\x80\x81")
rc, out, err = run(d, "status")
check("garbage rc", rc, 0)
check_in("garbage still prints canonical", "seed-live", out)

if _failures:
    print("FAIL (%d)" % len(_failures))
    for f in _failures:
        print("  " + f)
    sys.exit(1)
print("OK test-dgn1680-gate-switch-registry-ext")
