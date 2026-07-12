#!/usr/bin/env python3
"""DGN-268 S3 gate: delivery wiring + cron safety rails.

The mirror engine lives at repo-root mirror/ but nothing shipped it into a
minted instance, so the cron flag-gates always hit `[ ! -d mirror ] && exit 0`.
S3 wires delivery (mint copies mirror/ code, update refreshes code only) and
adds Linux systemd parity + a once/day unauth warning so the poller does not
crash-loop.

Checks (all shell-level; NO network, NO real Google/Telegram):
  1. mint copies mirror/ CODE into the instance and NO *.db.
  2. mint renders + renames the Linux systemd units (poll .timer has
     OnUnitActiveSec=300; reconcile .timer weekly), placeholders substituted.
  3. flag OFF (MIRROR_MODULE unset/off) -> both shells exit 0, silent.
  4. flag ON + no auth -> exactly ONE warning (stamp throttle), exit 0, no
     traceback; a second run the same day is silent.
  5. update.sh refresh NEVER touches a sentinel mirror_state.db (it must
     survive byte-identical) while code files DO refresh.

Uses a fake `gws` (always unauth) and a fake `push.sh` (records calls) on
PATH for the safety-rail cases. mint runs with --no-venv (fast, no secrets).

Run: python3 tests/mirror/test_s3_delivery.py   (exit 0 = pass)
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
MINT = os.path.join(REPO_ROOT, "scripts", "mint.sh")
UPDATE = os.path.join(REPO_ROOT, "update.sh")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _run(cmd, env=None, cwd=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=e, cwd=cwd)


def _mint(root, name="probebot"):
    return _run(["bash", MINT, "--root", root, "--name", name, "--no-venv"],
                env={"DOGANY_BOT_TOKEN": ""})


def _write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def _fake_bin_dir(gws_authed=False, push_log=None):
    """A dir with fake `gws` (auth status exit 1 unless authed) and `push.sh`
    that appends its --text to push_log. Returned dir goes on PATH."""
    d = tempfile.mkdtemp(prefix="dgn268-bin-")
    gws_exit = "0" if gws_authed else "1"
    _write(os.path.join(d, "gws"),
           "#!/bin/bash\n"
           'if [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit %s; fi\n'
           "exit 0\n" % gws_exit)
    os.chmod(os.path.join(d, "gws"), 0o755)
    return d


# ---------------------------------------------------------------------------


def test_mint_delivers_mirror_code_no_db():
    print("(1) mint copies mirror/ code, no *.db:")
    root = tempfile.mkdtemp(prefix="dgn268-mint-")
    shutil.rmtree(root)
    r = _mint(root)
    _check("mint exited 0", r.returncode == 0, r.stderr[-400:])
    mdir = os.path.join(root, "mirror")
    _check("mirror/ present", os.path.isdir(mdir), mdir)
    for f in ("adapter.py", "reconcile.py", "notify.py", "mirror_state.sql"):
        _check("mirror/%s shipped" % f,
               os.path.isfile(os.path.join(mdir, f)), f)
    dbs = []
    for dp, _dn, fns in os.walk(mdir):
        dbs += [f for f in fns if f.endswith(".db")]
    _check("NO *.db in shipped mirror/", not dbs, str(dbs))
    shutil.rmtree(root, ignore_errors=True)


def test_mint_renders_systemd_units():
    print("(2) mint renders + renames Linux systemd units:")
    root = tempfile.mkdtemp(prefix="dgn268-mint-")
    shutil.rmtree(root)
    _mint(root, name="unitbot")
    rd = os.path.join(root, "routines")
    poll_timer = os.path.join(
        rd, "com.telegram-skill-bot.unitbot.mirror-poll.timer")
    poll_svc = os.path.join(
        rd, "com.telegram-skill-bot.unitbot.mirror-poll.service")
    rec_timer = os.path.join(
        rd, "com.telegram-skill-bot.unitbot.mirror-reconcile.timer")
    _check("poll .timer renamed with agent name",
           os.path.isfile(poll_timer), poll_timer)
    _check("poll .service renamed with agent name",
           os.path.isfile(poll_svc), poll_svc)
    _check("reconcile .timer renamed with agent name",
           os.path.isfile(rec_timer), rec_timer)
    if os.path.isfile(poll_timer):
        body = open(poll_timer).read()
        _check("poll timer fires every 300s", "OnUnitActiveSec=300" in body,
               body)
        _check("no placeholder survivors in poll timer",
               "__AGENT_NAME__" not in body and "telegram-agent" not in body,
               body)
    if os.path.isfile(rec_timer):
        body = open(rec_timer).read()
        _check("reconcile timer weekly (Sun 21:30)",
               "OnCalendar=Sun *-*-* 21:30:00" in body, body)
    if os.path.isfile(poll_svc):
        body = open(poll_svc).read()
        _check("poll service ExecStart substituted (real root)",
               root in body and "__PROJECT_ROOT__" not in body, body)
    shutil.rmtree(root, ignore_errors=True)


def _stage_shell_env(mirror_module):
    """Minimal instance layout to exercise a mirror shell directly:
    routines/ (real shells + a fake push.sh) + config/lifekit.conf +
    an empty mirror/ dir so the dir-presence gate passes."""
    root = tempfile.mkdtemp(prefix="dgn268-shell-")
    os.makedirs(os.path.join(root, "routines"))
    os.makedirs(os.path.join(root, "mirror"))
    os.makedirs(os.path.join(root, "config"))
    os.makedirs(os.path.join(root, ".telegram_bot"))
    for sh in ("mirror-poll.sh", "mirror-reconcile.sh"):
        shutil.copy2(
            os.path.join(REPO_ROOT, "agents", ".template", "routines", sh),
            os.path.join(root, "routines", sh))
    push_log = os.path.join(root, "push.log")
    _write(os.path.join(root, "routines", "push.sh"),
           "#!/bin/bash\n"
           'txt=""\n'
           'while [ $# -gt 0 ]; do case "$1" in --text) txt="$2"; shift 2;; '
           '*) shift;; esac; done\n'
           'printf "%%s\\n" "$txt" >> "%s"\n' % push_log)
    os.chmod(os.path.join(root, "routines", "push.sh"), 0o755)
    conf = "LIFEKIT=on\n"
    if mirror_module is not None:
        conf += "MIRROR_MODULE=%s\n" % mirror_module
    _write(os.path.join(root, "config", "lifekit.conf"), conf)
    return root, push_log


def test_flag_off_silent_exit0():
    print("(3) flag OFF -> both shells exit 0, silent:")
    for mod in ("off", None):  # explicit off, and absent
        root, push_log = _stage_shell_env(mod)
        for sh in ("mirror-poll.sh", "mirror-reconcile.sh"):
            r = _run(["bash", os.path.join(root, "routines", sh)])
            label = "%s (MIRROR_MODULE=%s)" % (sh, mod)
            _check("%s exit 0" % label, r.returncode == 0,
                   "rc=%d err=%s" % (r.returncode, r.stderr[-200:]))
            _check("%s no stdout" % label, r.stdout.strip() == "",
                   repr(r.stdout))
            _check("%s no warning pushed" % label,
                   not os.path.exists(push_log), "push fired")
        shutil.rmtree(root, ignore_errors=True)


def test_flag_on_unauth_warns_once():
    print("(4) flag ON + no auth -> one warning, exit 0, no traceback:")
    root, push_log = _stage_shell_env("on")
    bindir = _fake_bin_dir(gws_authed=False)
    env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
    poll = os.path.join(root, "routines", "mirror-poll.sh")
    r1 = _run(["bash", poll], env=env)
    _check("first run exit 0", r1.returncode == 0,
           "rc=%d err=%s" % (r1.returncode, r1.stderr[-300:]))
    _check("no python traceback", "Traceback" not in (r1.stdout + r1.stderr),
           (r1.stdout + r1.stderr)[-300:])
    warns = (open(push_log).read().strip().splitlines()
             if os.path.exists(push_log) else [])
    _check("exactly one warning pushed", len(warns) == 1, str(warns))
    # second run same day -> throttled (still one total).
    r2 = _run(["bash", poll], env=env)
    _check("second run exit 0", r2.returncode == 0, r2.stderr[-200:])
    warns2 = (open(push_log).read().strip().splitlines()
              if os.path.exists(push_log) else [])
    _check("still exactly one warning (throttled once/day)",
           len(warns2) == 1, str(warns2))
    # reconcile shares the stamp -> also silent same day.
    rec = os.path.join(root, "routines", "mirror-reconcile.sh")
    _run(["bash", rec], env=env)
    warns3 = (open(push_log).read().strip().splitlines()
              if os.path.exists(push_log) else [])
    _check("reconcile shares stamp (no double-warn same day)",
           len(warns3) == 1, str(warns3))
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(bindir, ignore_errors=True)


def test_update_preserves_state_db():
    print("(5) update.sh refresh never touches mirror_state.db:")
    # Mint a real instance, then plant a sentinel state db + mutate a code
    # file, run update, assert the db survived byte-identical and code was
    # refreshed back to the framework version.
    root = tempfile.mkdtemp(prefix="dgn268-upd-")
    shutil.rmtree(root)
    _mint(root, name="updbot")
    mdir = os.path.join(root, "mirror")
    sentinel = os.path.join(mdir, "mirror_state.db")
    _write(sentinel, "SENTINEL-LIVE-STATE-DO-NOT-TOUCH")
    # tamper a shipped code file so a refresh is observable
    adapter = os.path.join(mdir, "adapter.py")
    _write(adapter, "# tampered\n")
    r = _run(["bash", UPDATE, "--root", root, "--no-pull", "--yes"])
    _check("update exited 0", r.returncode == 0,
           "rc=%d err=%s" % (r.returncode, r.stderr[-400:]))
    _check("sentinel mirror_state.db survived byte-identical",
           os.path.exists(sentinel) and
           open(sentinel).read() == "SENTINEL-LIVE-STATE-DO-NOT-TOUCH",
           "db changed or gone")
    _check("adapter.py refreshed from framework (tamper gone)",
           os.path.exists(adapter) and "# tampered" not in open(adapter).read(),
           "code not refreshed")
    shutil.rmtree(root, ignore_errors=True)


def main():
    test_mint_delivers_mirror_code_no_db()
    test_mint_renders_systemd_units()
    test_flag_off_silent_exit0()
    test_flag_on_unauth_warns_once()
    test_update_preserves_state_db()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
