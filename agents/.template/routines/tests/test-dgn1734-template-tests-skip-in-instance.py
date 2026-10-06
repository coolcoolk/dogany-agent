#!/usr/bin/env python3
# test-lane: fast
"""DGN-1734 regression: the onboarding template-contract tests must SKIP (not
FAIL) when shipped into a live instance.

The framework update copies routines/tests/ under the instance root, where the
tree next to routines/ is the instance (onboarded persona, no
ONBOARDING_PENDING marker, .instance.conf present), not the unminted template.
This builds such a layout in a temp dir and runs both tests from it.

Run: python3 routines/tests/test-dgn1734-template-tests-skip-in-instance.py
Python 3 stdlib only.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.normpath(os.path.join(HERE, "..", ".."))
TESTS = ("test-onboarding-post-mint-slot.py", "test-dgn1632-w3-onboarding.py")
MARKER = "<!-- ONBOARDING_PENDING -->"

_failures = []


def check(name, cond, detail=""):
    if not cond:
        _failures.append("%s %s" % (name, detail))


def run(root, test, extra_env=None):
    env = dict(os.environ)
    env.pop("DOGANY_TEMPLATE_DIR", None)
    env.pop("ONBOARDING_HOOK_UNDER_TEST", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, os.path.join(root, "routines", "tests", test)],
        capture_output=True, text=True, env=env, cwd=root, timeout=300)


def mk_live_instance(dst):
    shutil.copytree(TEMPLATE, dst, symlinks=False)  # dereference like cp -aL
    with open(os.path.join(dst, ".instance.conf"), "w") as f:
        f.write("DOGANY_AGENT_CLASS=main\nDOGANY_AGENT_NAME=Metal\n")
    persona = os.path.join(dst, "identity", "hot.custom.agent.md")
    with open(persona, encoding="utf-8") as f:
        text = f.read()
    with open(persona, "w", encoding="utf-8") as f:
        f.write(text.replace(MARKER, "").replace("ONBOARDING_PENDING", "")
                + "\nOnboarded persona (instance-specific).\n")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        inst = os.path.join(tmp, "instance")
        mk_live_instance(inst)
        with open(os.path.join(inst, "identity", "hot.custom.agent.md"),
                  encoding="utf-8") as f:
            check("fixture.persona_onboarded", "ONBOARDING_PENDING" not in f.read())
        for t in TESTS:
            p = run(inst, t)
            out = p.stdout + p.stderr
            check("instance[%s].exit0" % t, p.returncode == 0, out[-400:])
            check("instance[%s].no_FAIL" % t, "FAIL" not in out, out[-400:])
            check("instance[%s].SKIP_with_reason" % t,
                  "SKIP DGN-1" in out and "NOT a pass" in out, out[-400:])
        # An explicit template dir re-enables the checks from the instance.
        p = run(inst, TESTS[1], {"DOGANY_TEMPLATE_DIR": TEMPLATE})
        check("instance+env[w3].exit0", p.returncode == 0, p.stdout + p.stderr)
        check("instance+env[w3].no_SKIP", "SKIP" not in p.stdout, p.stdout)
    # Canonical tree: template checks still run, no SKIP.
    for t in TESTS:
        p = run(TEMPLATE, t)
        check("canonical[%s].exit0" % t, p.returncode == 0, p.stdout + p.stderr)
        check("canonical[%s].no_SKIP" % t, "SKIP" not in p.stdout, p.stdout)
    if _failures:
        print("FAIL (%d)" % len(_failures))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("OK test-dgn1734-template-tests-skip-in-instance")


if __name__ == "__main__":
    main()
