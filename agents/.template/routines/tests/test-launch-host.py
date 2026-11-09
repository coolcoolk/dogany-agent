#!/usr/bin/env python3
# test-lane: fast
"""v2.7 T6 P2a (dec-319): launch-host library (sh + py twin), launchd backend,
lease, doctor.

Hermetic: temp HOME, temp instance roots, a stateful fake `launchctl` passed
via DOGANY_LAUNCHCTL. Never touches live launchd, ~/.transporter or a real
~/.dogany.

Locks:
  (a) resolver: lease beats policy beats default; corrupt lease -> launchd +
      warning; policy never overrides (or creates) a lease;
  (b) lease writer: epoch+1, atomic, serialised under flock (N writers ->
      epoch N);
  (c) label resolution: service marker > .instance.conf; a placeholder or
      mismatched marker is ignored; nothing resolvable -> refused, no guess;
  (d) launchd backend argv = today's calls (start/restart/stop), the P1
      restart_wait marker on restart, DGN-1640 bounded stop verification;
  (e) host=app lease: start/stop/restart refuse with 69 and make ZERO
      launchctl calls (I1/I6); adopt/release are 69 (P5);
  (f) status + doctor reports, incl. the I1/I2/R2 cross-checks and that
      doctor never takes bridge.lock;
  (g) shell host verb == python twin.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
SH = LIB / "launch-host.sh"
sys.path.insert(0, str(LIB))
import launch_host as lh  # noqa: E402

FAKE_LC = r'''#!/bin/bash
# stateful fake launchctl: $FAKE_LC_STATE/{loaded,disabled}/<label>, calls log
S="$FAKE_LC_STATE"; mkdir -p "$S/loaded" "$S/disabled"
echo "$*" >> "$S/calls"
lbl() { printf '%s' "${1##*/}"; }
case "$1" in
  list)
    printf 'PID\tStatus\tLabel\n'
    for f in "$S"/loaded/*; do [ -e "$f" ] || continue
      printf '%s\t%s\t%s\n' "$(cat "$f")" "0" "$(basename "$f")"; done ;;
  print)
    [ -e "$S/loaded/$(lbl "$2")" ] && { echo "path = /x.plist"; exit 0; }; exit 113 ;;
  print-disabled)
    echo "disabled services = {"
    for f in "$S"/disabled/*; do [ -e "$f" ] || continue
      printf '\t"%s" => disabled\n' "$(basename "$f")"; done
    echo "}" ;;
  bootout)
    l="$(lbl "$2")"; [ -e "$S/loaded/$l" ] || exit 3
    pid="$(cat "$S/loaded/$l")"
    if [ ! -e "$S/stuck" ]; then
      rm -f "$S/loaded/$l"; [ "$pid" != "-" ] && kill "$pid" 2>/dev/null
    fi
    exit 0 ;;
  kickstart)
    shift; [ "$1" = "-k" ] && shift
    [ -e "$S/loaded/$(lbl "$1")" ] || exit 113 ;;
  enable)  rm -f "$S/disabled/$(lbl "$2")" ;;
  disable) touch "$S/disabled/$(lbl "$2")" ;;
  bootstrap)
    l="$(grep -A1 '<key>Label</key>' "$3" | sed -n 's#.*<string>\(.*\)</string>.*#\1#p')"
    [ -n "$l" ] || exit 5
    [ -e "$S/disabled/$l" ] && exit 37
    echo "-" > "$S/loaded/$l" ;;
  *) exit 64 ;;
esac
'''

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
</dict>
</plist>
"""

LABEL = "com.telegram-skill-bot.fx"
# built at runtime so mint placeholder substitution never rewrites it
TOKEN = "__" + "AGENT_NAME" + "__"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lh-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "agents" / "fx"
        (self.root / "bridge").mkdir(parents=True)
        (self.root / ".telegram_bot" / "state").mkdir(parents=True)
        (self.root / ".instance.conf").write_text("DOGANY_AGENT_NAME=fx\n")
        self.plist = self.root / "bridge" / ("%s.newbridge.plist" % LABEL)
        self.plist.write_text(PLIST % LABEL)
        (self.root / "bridge" / ("%s.watchdog.plist" % LABEL)).write_text(
            PLIST % (LABEL + ".watchdog"))
        self.state = self.tmp / "lc"
        self.state.mkdir()
        self.fake = self.tmp / "launchctl"
        self.fake.write_text(FAKE_LC)
        self.fake.chmod(0o755)
        self.procs = []
        self.env = dict(os.environ, HOME=str(self.home), FAKE_LC_STATE=str(self.state),
                        DOGANY_LAUNCHCTL=str(self.fake),
                        DOGANY_LAUNCH_HOST_STOP_POLL_TRIES="20",
                        DOGANY_LAUNCH_HOST_STOP_POLL_INTERVAL="0.05")
        self.env.pop("DOGANY_LAUNCHD_CAPTURE", None)
        self._old_env = {k: os.environ.get(k) for k in ("DOGANY_LAUNCHCTL", "FAKE_LC_STATE", "HOME")}
        os.environ.update(DOGANY_LAUNCHCTL=str(self.fake), FAKE_LC_STATE=str(self.state),
                          HOME=str(self.home))
        self.addCleanup(self._restore)

    def _restore(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(5)
            except Exception:
                pass
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def load(self, label=LABEL, live=True):
        pid = "-"
        if live:
            p = subprocess.Popen(["sleep", "300"])
            self.procs.append(p)
            pid = str(p.pid)
        (self.state / "loaded").mkdir(exist_ok=True)
        (self.state / "loaded" / label).write_text(pid)
        return None if pid == "-" else int(pid)

    def calls(self):
        f = self.state / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def action_calls(self):
        return [c for c in self.calls() if c.split()[0] not in ("list", "print", "print-disabled")]

    def sh(self, *args, env=None):
        p = subprocess.run(["bash", str(SH)] + list(args), env=env or self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            doc = None
        return p.returncode, doc, p

    def lease(self, host, epoch=1):
        lh.lease_path(self.root).write_text(json.dumps(
            {"host": host, "epoch": epoch, "since": "x", "by": "t", "reason": "t", "state": "steady"}))


class Resolver(Base):
    def test_default_launchd(self):
        r = lh.resolve_host(self.root)
        self.assertEqual((r["host"], r["source"], r["policy"]), ("launchd", "default", "launchd"))
        self.assertEqual(r["warnings"], [])

    def test_lease_beats_default(self):
        self.lease("app")
        self.assertEqual(lh.resolve_host(self.root)["host"], "app")

    def test_policy_never_creates_or_overrides_lease(self):
        with open(self.root / ".instance.conf", "a") as f:
            f.write('DOGANY_LAUNCH_HOST="app"\n')
        r = lh.resolve_host(self.root)
        self.assertEqual((r["host"], r["policy"]), ("launchd", "app"))
        self.assertFalse(lh.lease_path(self.root).exists())
        self.lease("launchd")
        self.assertEqual(lh.resolve_host(self.root)["host"], "launchd")

    def test_corrupt_lease_launchd_with_warning(self):
        for body in ("{not json", "[]", '{"host":"cron","epoch":1}', '{"host":"app","epoch":"1"}'):
            lh.lease_path(self.root).write_text(body)
            r = lh.resolve_host(self.root)
            self.assertEqual((r["host"], r["source"]), ("launchd", "corrupt-lease"), body)
            self.assertTrue(r["warnings"], body)

    def test_invalid_policy_warns(self):
        (self.root / ".instance.conf").write_text("DOGANY_LAUNCH_HOST=sometimes\n")
        r = lh.resolve_host(self.root)
        self.assertEqual(r["policy"], "launchd")
        self.assertTrue(any("invalid" in w for w in r["warnings"]))


class Lease(Base):
    def test_epoch_increments_and_fields(self):
        a = lh.write_lease(self.root, "app", "adopt", "owner", state="adopting")
        b = lh.write_lease(self.root, "launchd", "release", "fallback")
        self.assertEqual((a["epoch"], b["epoch"]), (1, 2))
        on_disk = json.loads(lh.lease_path(self.root).read_text())
        self.assertEqual(on_disk, b)
        self.assertEqual(set(on_disk), {"host", "epoch", "since", "by", "reason", "state"})
        leftovers = [p.name for p in lh.lease_path(self.root).parent.iterdir()
                     if p.name.startswith(".launch-host-")]
        self.assertEqual(leftovers, [])

    def test_corrupt_previous_counts_as_zero(self):
        lh.lease_path(self.root).write_text("garbage")
        self.assertEqual(lh.write_lease(self.root, "app", "t", "t")["epoch"], 1)

    def test_rejects_unknown_host(self):
        with self.assertRaises(ValueError):
            lh.write_lease(self.root, "cron", "t", "t")

    def test_concurrent_writers_serialised(self):
        n = 12
        procs = [subprocess.Popen([sys.executable, str(LIB / "launch_host.py"), "lease-write",
                                   str(self.root), "--host", "launchd", "--by", "t%d" % i,
                                   "--reason", "race"], stdout=subprocess.DEVNULL)
                 for i in range(n)]
        self.assertEqual([p.wait(30) for p in procs], [0] * n)
        self.assertEqual(lh.read_lease(self.root)[0]["epoch"], n)


class Label(Base):
    def test_conf_label_and_plist_skips_watchdog(self):
        label, plist, src = lh.resolve_label(self.root)
        self.assertEqual((label, plist, src), (LABEL, str(self.plist), "instance-conf"))

    def test_marker_beats_conf(self):
        other = self.root / "bridge" / "com.telegram-skill-bot.renamed.newbridge.plist"
        other.write_text(PLIST % "com.telegram-skill-bot.renamed")
        (self.root / ".telegram_bot" / ".service_plist").write_text(
            "%s\ncom.telegram-skill-bot.renamed\n" % other)
        self.assertEqual(lh.resolve_label(self.root)[:3],
                         ("com.telegram-skill-bot.renamed", str(other), "service-marker"))

    def test_bad_marker_ignored(self):
        bad = self.root / "bridge" / "tpl.plist"
        bad.write_text(PLIST % ("com.telegram-skill-bot." + TOKEN))
        (self.root / ".telegram_bot" / ".service_plist").write_text(
            "%s\ncom.telegram-skill-bot.%s\n" % (bad, TOKEN))
        self.assertEqual(lh.resolve_label(self.root)[2], "instance-conf")
        (self.root / ".telegram_bot" / ".service_plist").write_text("%s\nmismatch\n" % self.plist)
        self.assertEqual(lh.resolve_label(self.root)[2], "instance-conf")

    def test_nothing_resolvable(self):
        (self.root / ".instance.conf").write_text("DOGANY_AGENT_NAME=%s\n" % TOKEN)
        self.assertEqual(lh.resolve_label(self.root), ("", None, "none"))
        rc, doc, _ = self.sh("restart", str(self.root))
        self.assertEqual(rc, 4)
        self.assertFalse(doc["ok"])
        self.assertEqual(self.action_calls(), [])


class LaunchdBackend(Base):
    def test_start_not_loaded_enables_and_bootstraps(self):
        rc, doc, p = self.sh("start", str(self.root), "--reason", "test")
        self.assertEqual(rc, 0, p.stderr)
        self.assertTrue(doc["ok"])
        uid = os.getuid()
        self.assertEqual(self.action_calls(), [
            "enable gui/%d/%s" % (uid, LABEL),
            "bootstrap gui/%d %s" % (uid, self.plist)])
        log = (self.root / ".telegram_bot" / "logs" / "launch-host.log").read_text()
        self.assertIn("start host=launchd label=%s reason=test rc=0" % LABEL, log)

    def test_start_loaded_is_plain_kickstart(self):
        self.load()
        rc, _, _ = self.sh("start", str(self.root))
        self.assertEqual(rc, 0)
        self.assertEqual(self.action_calls(), ["kickstart gui/%d/%s" % (os.getuid(), LABEL)])

    def test_start_not_loaded_without_plist_refused(self):
        self.plist.unlink()
        rc, doc, _ = self.sh("start", str(self.root))
        self.assertEqual(rc, 4)
        self.assertEqual(self.action_calls(), [])

    def test_restart_kickstart_k_and_p1_marker(self):
        self.load()
        rc, doc, _ = self.sh("restart", str(self.root))
        self.assertEqual(rc, 0)
        self.assertEqual(self.action_calls(), ["kickstart -k gui/%d/%s" % (os.getuid(), LABEL)])
        self.assertTrue((self.root / ".telegram_bot" / "state" / "bridge.restart_wait").exists())

    def test_stop_bootout_then_verified_gone(self):
        pid = self.load()
        rc, doc, p = self.sh("stop", str(self.root))
        self.assertEqual(rc, 0, p.stdout + p.stderr)
        self.assertIn("verified gone", doc["detail"])
        self.assertEqual(self.action_calls(), ["bootout gui/%d/%s" % (os.getuid(), LABEL)])
        self.assertNotIn("disable", " ".join(self.calls()))  # stop never disables
        self.procs[0].wait(5)
        self.assertFalse(lh._pid_alive(pid))

    def test_stop_bounded_poll_fails_on_stuck_job(self):
        self.load()
        (self.state / "stuck").touch()
        t0 = time.monotonic()
        rc, doc, _ = self.sh("stop", str(self.root))
        self.assertEqual(rc, 1)
        self.assertIn("DGN-1640", doc["detail"])
        self.assertLess(time.monotonic() - t0, 20)

    def test_stop_not_loaded_is_verified_ok(self):
        rc, doc, _ = self.sh("stop", str(self.root))
        self.assertEqual(rc, 0)
        self.assertIn("not loaded", doc["detail"])

    def test_capture_golden(self):
        cap = self.tmp / "capture"
        env = dict(self.env, DOGANY_LAUNCHD_CAPTURE=str(cap))
        for verb in ("stop", "restart", "start"):
            rc, _, p = self.sh(verb, str(self.root), env=env)
            self.assertEqual(rc, 0, p.stdout + p.stderr)
        self.assertEqual(cap.read_text().splitlines(), [
            "launchctl bootout gui/UID/%s" % LABEL,
            "launchctl kickstart -k gui/UID/%s" % LABEL,
            "launchctl enable gui/UID/%s" % LABEL,
            "launchctl bootstrap gui/UID %s" % self.plist])
        self.assertEqual(self.action_calls(), [])

    def test_label_override(self):
        self.load("com.telegram-skill-bot.other")
        rc, _, _ = self.sh("restart", str(self.root), "--label", "com.telegram-skill-bot.other")
        self.assertEqual(rc, 0)
        self.assertEqual(self.action_calls(),
                         ["kickstart -k gui/%d/com.telegram-skill-bot.other" % os.getuid()])


class AppLeaseAndP5(Base):
    def test_app_lease_never_touches_launchd(self):
        self.lease("app")
        self.load()
        for verb in ("start", "stop", "restart"):
            rc, doc, _ = self.sh(verb, str(self.root))
            self.assertEqual(rc, 69, verb)
            self.assertIn("P5", doc["detail"])
        self.assertEqual(self.action_calls(), [])

    def test_adopt_release_not_in_build(self):
        for args in (("adopt", "--to", "app"), ("release", "--to", "launchd")):
            rc, doc, _ = self.sh(args[0], str(self.root), *args[1:])
            self.assertEqual(rc, 69)
        self.assertEqual(self.calls(), [])
        self.assertFalse(lh.lease_path(self.root).exists())

    def test_usage(self):
        self.assertEqual(self.sh("bogus", str(self.root))[0], 2)
        self.assertEqual(self.sh("start", str(self.tmp / "nope"))[0], 2)


class StatusDoctor(Base):
    def _lock(self, pid, host="launchd"):
        (self.root / ".telegram_bot" / "state" / "bridge.lock").write_text(json.dumps(
            {"pid": pid, "host": host, "started_at": "2026-10-08T00:00:00+00:00"}))

    def _app_file(self, control=None, supported=True):
        d = self.home / ".dogany" / "integrations"
        d.mkdir(parents=True, exist_ok=True)
        lhb = {"supported": supported, "version": 1}
        if control:
            lhb["control"] = str(control)
        (d / lh.APP_FILE).write_text(json.dumps({"schema_version": 1, "launch_host": lhb}))

    def test_status_running(self):
        pid = self.load()
        self._lock(pid)
        hb = self.root / ".telegram_bot" / "poll_heartbeat"
        hb.touch()
        os.utime(hb, (time.time() - 30, time.time() - 30))
        rc, doc, _ = self.sh("status", str(self.root))
        self.assertEqual(rc, 0)
        self.assertEqual((doc["host"], doc["running"], doc["pid"], doc["last_exit"]),
                         ("launchd", True, pid, 0))
        self.assertEqual(doc["since"], "2026-10-08T00:00:00+00:00")
        self.assertTrue(29 <= doc["heartbeat_age"] <= 40)

    def test_status_not_running(self):
        doc = lh.status_report(self.root)
        self.assertIs(doc["running"], False)

    def test_doctor_healthy(self):
        self.load()
        rc, doc, p = self.sh("doctor", str(self.root), "--home", str(self.home))
        self.assertEqual(rc, 0, p.stdout)
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["plist_state"], {"file": True, "loaded": True, "disabled": False})
        self.assertEqual(doc["host_cli"]["reason"], "app file absent")
        self.assertFalse(doc["companion"]["present"])

    def test_doctor_launchd_disabled_is_error(self):
        (self.state / "disabled").mkdir(exist_ok=True)
        (self.state / "disabled" / LABEL).touch()
        doc = lh.doctor_report(self.root, home=str(self.home))
        self.assertFalse(doc["ok"])
        self.assertTrue(any("zero launchers" in e for e in doc["errors"]))

    def test_doctor_companion_running_under_launchd_is_error(self):
        d = self.home / ".dogany" / "integrations"
        d.mkdir(parents=True)
        (d / lh.COMPANION_FILE).write_text(json.dumps({"schema_version": 1, "launch": {
            "schema_version": 1, "agents": [{"id": LABEL, "desired": "running", "lease_epoch": 1}]}}))
        doc = lh.doctor_report(self.root, home=str(self.home))
        self.assertEqual(doc["companion"]["desired"], "running")
        self.assertTrue(any("two launchers" in e for e in doc["errors"]))

    def test_doctor_app_lease_checks(self):
        self.lease("app")
        self.load()
        with open(self.root / ".instance.conf", "a") as f:
            f.write("DOGANY_WATCHDOG=off\n")
        doc = lh.doctor_report(self.root, home=str(self.home))
        errs = " | ".join(doc["errors"])
        self.assertIn("still loaded", errs)
        self.assertIn("not reachable", errs)
        self.assertIn("R2", errs)

    def test_doctor_host_cli_reachable(self):
        ctl = self.tmp / "dogany-host"
        ctl.write_text("#!/bin/sh\nexit 99\n")  # must NOT be executed
        ctl.chmod(0o755)
        self._app_file(ctl)
        doc = lh.doctor_report(self.root, home=str(self.home))
        self.assertTrue(doc["host_cli"]["reachable"])
        ctl.chmod(0o644)
        self.assertFalse(lh.doctor_report(self.root, home=str(self.home))["host_cli"]["reachable"])
        self._app_file(ctl, supported=False)
        self.assertEqual(lh.doctor_report(self.root, home=str(self.home))["host_cli"]["reason"],
                         "launch_host not supported by app")

    def test_doctor_lock_holder_host_mismatch(self):
        pid = self.load()
        self._lock(pid, host="app")
        doc = lh.doctor_report(self.root, home=str(self.home))
        self.assertTrue(doc["lock_holder"]["alive"])
        self.assertTrue(any("bridge.lock holder" in e for e in doc["errors"]))

    def test_doctor_never_takes_bridge_lock(self):
        lock = self.root / ".telegram_bot" / "state" / "bridge.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "host": "launchd"}))
        fd = os.open(str(lock), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = {}
        t = threading.Thread(target=lambda: out.update(d=lh.doctor_report(self.root, home=str(self.home))))
        t.start()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(out["d"]["lock_holder"]["pid"], os.getpid())


class Parity(Base):
    def test_shell_host_equals_python(self):
        for setup in (lambda: None, lambda: self.lease("app"),
                      lambda: lh.lease_path(self.root).write_text("bad")):
            setup()
            rc, doc, _ = self.sh("host", str(self.root))
            self.assertEqual(rc, 0)
            self.assertEqual(doc, json.loads(json.dumps(lh.host_report(str(self.root)))))


if __name__ == "__main__":
    unittest.main(verbosity=1)
