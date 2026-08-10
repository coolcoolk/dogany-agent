#!/usr/bin/env python3
"""DGN-268 merge-gate fixes (final grill NO-GO items).

FIX 1: update.sh must substitute + rename the Linux systemd units (*.service/
       *.timer) -- else an updated Linux instance ships units with literal
       __PROJECT_ROOT__/__AGENT_NAME__/__HOME__, unrenamed, silently.
FIX 2: the cron rails must gate on calendar+tasks ONLY, not gmail.send -- a
       user without gmail.send must keep their calendar/tasks mirror RUNNING.
FIX 3: the pinned auth command must use `--scopes <full URLs>` (not `-s`, which
       only limits the scope picker by service name).
FIX 4: no EMAIL_APP_PASSWORD / DOGANY_EMAIL_PW / SMTP survives in .env.example
       or README.

Run: python3 tests/mirror/test_s6_mergegate.py   (exit 0 = pass)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
MINT = os.path.join(REPO_ROOT, "scripts", "mint.sh")
UPDATE = os.path.join(REPO_ROOT, "update.sh")
PREFLIGHT = os.path.join(REPO_ROOT, "agents", ".template", "routines",
                        "mirror-setup-check.sh")
TPL_ROUTINES = os.path.join(REPO_ROOT, "agents", ".template", "routines")

_CAL = "https://www.googleapis.com/auth/calendar"
_TASKS = "https://www.googleapis.com/auth/tasks"
_SEND = "https://www.googleapis.com/auth/gmail.send"

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _run(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=e)


def _mint(root, name="mgbot"):
    return _run(["bash", MINT, "--root", root, "--name", name, "--no-venv"],
                env={"DOGANY_BOT_TOKEN": ""})


def _fake_gws_bin(scopes):
    d = tempfile.mkdtemp(prefix="dgn268-mgbin-")
    status = json.dumps({"scopes": scopes})
    with open(os.path.join(d, "gws"), "w") as fh:
        fh.write("#!/bin/bash\n"
                 'if [ "$1" = "auth" ] && [ "$2" = "status" ]; then\n'
                 "  cat <<'JSON'\n%s\nJSON\n  exit 0\nfi\nexit 0\n" % status)
    os.chmod(os.path.join(d, "gws"), 0o755)
    return d


def test_update_substitutes_units():
    print("(FIX1) update.sh substitutes + renames systemd units:")
    root = tempfile.mkdtemp(prefix="dgn268-mgupd-")
    shutil.rmtree(root)
    _mint(root, name="updunit")
    rd = os.path.join(root, "routines")
    # Simulate an update shipping a fresh GENERIC unit with raw placeholders
    # (as the template ships it, before substitution/rename).
    generic = os.path.join(
        rd, "com.telegram-skill-bot.telegram-agent.mirror-poll.timer")
    with open(generic, "w") as fh:
        fh.write("[Timer]\nOnUnitActiveSec=300\n"
                 "Unit=com.telegram-skill-bot.__AGENT_NAME__.mirror-poll.service\n"
                 "# root=__PROJECT_ROOT__ home=__HOME__\n")
    r = _run(["bash", UPDATE, "--root", root, "--no-pull", "--yes"])
    _check("update exit 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-300:]))
    # The generic file must be renamed to the agent-named one and substituted.
    named = os.path.join(
        rd, "com.telegram-skill-bot.updunit.mirror-poll.timer")
    _check("generic unit renamed to agent name",
           not os.path.exists(generic) and os.path.exists(named),
           "generic=%s named=%s" % (os.path.exists(generic),
                                    os.path.exists(named)))
    if os.path.exists(named):
        body = open(named).read()
        _check("no __AGENT_NAME__ placeholder left",
               "__AGENT_NAME__" not in body, body)
        _check("no __PROJECT_ROOT__/__HOME__ placeholder left",
               "__PROJECT_ROOT__" not in body and "__HOME__" not in body, body)
        _check("agent name substituted in body", "updunit" in body, body)
    shutil.rmtree(root, ignore_errors=True)


def _stage_rail(root, module="on"):
    """Minimal instance to run mirror-poll.sh directly with the real preflight
    reachable (so the rail's --require calendar,tasks path is exercised)."""
    os.makedirs(os.path.join(root, "routines"))
    os.makedirs(os.path.join(root, "mirror"))
    os.makedirs(os.path.join(root, "config"))
    os.makedirs(os.path.join(root, ".telegram_bot"))
    for sh in ("mirror-poll.sh", "mirror-reconcile.sh", "mirror-setup-check.sh"):
        dst = os.path.join(root, "routines", sh)
        shutil.copy2(os.path.join(TPL_ROUTINES, sh), dst)
        os.chmod(dst, 0o755)
    push_log = os.path.join(root, "push.log")
    with open(os.path.join(root, "routines", "push.sh"), "w") as fh:
        fh.write("#!/bin/bash\n"
                 'txt=""\n'
                 'while [ $# -gt 0 ]; do case "$1" in --text) txt="$2"; shift 2;; '
                 '*) shift;; esac; done\n'
                 'printf "%%s\\n" "$txt" >> "%s"\n' % push_log)
    os.chmod(os.path.join(root, "routines", "push.sh"), 0o755)
    with open(os.path.join(root, "config", "lifekit.conf"), "w") as fh:
        fh.write("LIFEKIT=on\nMIRROR_MODULE=%s\n" % module)
    # The poll cycle body needs to NOT actually run python mirror import (no db);
    # we only care that the rail lets it THROUGH (not halt). Stub the mirror so
    # the cd + python step is harmless: leave mirror/ empty -> the python here-doc
    # will fail to import adapter. To isolate the RAIL decision, we assert on
    # whether the unauth warning fired, not on the cycle body.
    return push_log


def test_rail_runs_on_calendar_tasks_only():
    print("(FIX2) rail keeps mirror RUNNING on a calendar+tasks-only grant:")
    # gmail.send-less grant -> the rail's --require calendar,tasks must PASS,
    # so NO unauth warning is pushed (the rail lets the cycle proceed).
    root = tempfile.mkdtemp(prefix="dgn268-mgrail-")
    push_log = _stage_rail(root, module="on")
    bindir = _fake_gws_bin([_CAL, _TASKS])   # NO gmail.send
    env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
    r = _run(["bash", os.path.join(root, "routines", "mirror-poll.sh")], env=env)
    # The rail should NOT have short-circuited with the unauth warning. It may
    # still exit non-zero from the cycle body (no real db/mirror) -- that is the
    # cycle, not the rail. The RAIL decision is observable via the stamp/push:
    warned = os.path.exists(push_log) and os.path.getsize(push_log) > 0
    _check("no unauth warning on calendar+tasks-only grant (rail did NOT halt)",
           not warned,
           "warning fired: %s" % (open(push_log).read() if warned else ""))
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(bindir, ignore_errors=True)

    # Contrast: an UNauthed grant (no scopes at all) SHOULD warn.
    root = tempfile.mkdtemp(prefix="dgn268-mgrail2-")
    push_log = _stage_rail(root, module="on")
    bindir = _fake_gws_bin([])   # no scopes
    env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
    _run(["bash", os.path.join(root, "routines", "mirror-poll.sh")], env=env)
    warned2 = os.path.exists(push_log) and os.path.getsize(push_log) > 0
    _check("unauth (no scopes) still warns", warned2, "no warning fired")
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(bindir, ignore_errors=True)


def test_preflight_require_subset():
    print("(FIX2) mirror-setup-check --require calendar,tasks vs full:")
    bindir = _fake_gws_bin([_CAL, _TASKS])   # gmail.send-less
    env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
    r_sub = _run(["bash", PREFLIGHT, "--require", "calendar,tasks"], env=env)
    _check("--require calendar,tasks PASSES on gmail.send-less grant",
           r_sub.returncode == 0, r_sub.stdout)
    r_full = _run(["bash", PREFLIGHT], env=env)
    _check("full check FAILS on gmail.send-less grant",
           r_full.returncode == 1, r_full.stdout)
    _check("full check names gmail.send missing",
           "gmail.send" in r_full.stdout, r_full.stdout)
    shutil.rmtree(bindir, ignore_errors=True)

    # exact-URL match: a calendar.readonly-only grant must NOT satisfy calendar.
    bindir = _fake_gws_bin([_CAL + ".readonly", _TASKS])
    env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
    r = _run(["bash", PREFLIGHT, "--require", "calendar,tasks"], env=env)
    _check("calendar.readonly does NOT satisfy calendar (exact-URL match)",
           r.returncode == 1, r.stdout)
    shutil.rmtree(bindir, ignore_errors=True)


def test_auth_command_uses_scopes_flag():
    print("(FIX3) pinned auth command uses --scopes (not -s):")
    txt = open(PREFLIGHT).read()
    _check("preflight pins `gws auth login --scopes`",
           "gws auth login --scopes" in txt, "not found")
    _check("preflight passes the gmail.send scope URL",
           _SEND in txt, "gmail.send URL absent")
    _check("preflight does NOT hand `-s calendar,tasks,gmail.send`",
           "-s calendar,tasks,gmail.send" not in txt, "old -s command present")
    # SKILL step 3 (edited by baseline-editor) -- check if present yet.
    skill = os.path.join(REPO_ROOT, "skills", "dogany-lifekit-setup",
                         "SKILL.md")
    stxt = open(skill).read()
    if "gws auth login" in stxt:
        _check("SKILL pins `--scopes` (not the -s form)",
               "--scopes" in stxt and "-s calendar,tasks,gmail.send" not in stxt,
               "SKILL still uses -s form")
    else:
        _check("SKILL auth command (skipped -- SKILL not yet edited)", True)


def test_no_app_password_residue():
    print("(FIX4) no app-password / SMTP residue in .env.example + README:")
    files = [
        os.path.join(REPO_ROOT, "agents", ".template", "bridge",
                     ".env.example"),
        os.path.join(REPO_ROOT, "agents", ".template", ".telegram_bot",
                     ".env.example"),
        os.path.join(REPO_ROOT, "README.md"),
        os.path.join(REPO_ROOT, "README-ko.md"),
    ]
    for f in files:
        txt = open(f).read()
        base = os.path.relpath(f, REPO_ROOT)
        _check("%s: no EMAIL_APP_PASSWORD" % base,
               "EMAIL_APP_PASSWORD" not in txt, "present")
        _check("%s: no DOGANY_EMAIL_PW" % base,
               "DOGANY_EMAIL_PW" not in txt, "present")
        _check("%s: no SMTP_HOST/SMTP_PORT" % base,
               "SMTP_HOST" not in txt and "SMTP_PORT" not in txt, "present")


def main():
    test_update_substitutes_units()
    test_rail_runs_on_calendar_tasks_only()
    test_preflight_require_subset()
    test_auth_command_uses_scopes_flag()
    test_no_app_password_residue()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
