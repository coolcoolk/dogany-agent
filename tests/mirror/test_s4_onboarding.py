#!/usr/bin/env python3
"""DGN-268 S4 gate: onboarding preflight + i18n + mailer re-wire + install.sh.

Owner decision (S4): per-user OAuth, agent-guided onboarding, ONE Google login
covering calendar + tasks + gmail.send, app-password fully removed.

Checks:
  1. Preflight (routines/mirror-setup-check.sh) with a FAKE gws: all-present ->
     exit 0; missing gmail.send scope -> exit 1 + reports it; gws absent ->
     exit 1.
  2. i18n load: AGENT_LANG=en -> en string; missing key -> ko-literal fallback
     (zero-delta); ko values byte-match the notify.py TEMPLATES.
  3. Mailer send via a fake gws recording the +send call: correct To/Subject/
     body/CC; unconnected (no gmail.send scope) -> graceful not-connected.
  4. install.sh: no EMAIL_APP_PASSWORD live reference; step labels consistent
     ([N/9], 1..9); no smtplib in the mailer.

Run: python3 tests/mirror/test_s4_onboarding.py   (exit 0 = pass)
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from email import message_from_bytes
from email.policy import default as _default_policy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_MIRROR = os.path.join(REPO_ROOT, "mirror")
PREFLIGHT = os.path.join(REPO_ROOT, "agents", ".template", "routines",
                        "mirror-setup-check.sh")
INSTALL = os.path.join(REPO_ROOT, "install.sh")

_failures = []


def _check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        _failures.append(name)


def _fake_gws_bin(scopes, present=True):
    """A dir with a fake `gws` whose `auth status` reports `scopes`. present=
    False writes no gws (CLI absent)."""
    d = tempfile.mkdtemp(prefix="dgn268-s4bin-")
    if present:
        status = json.dumps({"scopes": scopes})
        script = ("#!/bin/bash\n"
                  'if [ "$1" = "auth" ] && [ "$2" = "status" ]; then\n'
                  "  cat <<'JSON'\n%s\nJSON\n  exit 0\nfi\nexit 0\n" % status)
        with open(os.path.join(d, "gws"), "w") as fh:
            fh.write(script)
        os.chmod(os.path.join(d, "gws"), 0o755)
    return d


_CAL = "https://www.googleapis.com/auth/calendar"
_TASKS = "https://www.googleapis.com/auth/tasks"
_SEND = "https://www.googleapis.com/auth/gmail.send"


def test_preflight():
    print("(1) preflight report logic:")
    # all present
    d = _fake_gws_bin([_CAL, _TASKS, _SEND])
    env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
    r = subprocess.run(["bash", PREFLIGHT], capture_output=True, text=True,
                       env=env)
    _check("all-present -> exit 0", r.returncode == 0,
           "rc=%d out=%s" % (r.returncode, r.stdout))
    _check("reports auth+scopes OK", "auth + scopes" in r.stdout, r.stdout)
    shutil.rmtree(d, ignore_errors=True)

    # missing gmail.send
    d = _fake_gws_bin([_CAL, _TASKS])
    env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
    r = subprocess.run(["bash", PREFLIGHT], capture_output=True, text=True,
                       env=env)
    _check("missing gmail.send -> exit 1", r.returncode == 1, r.stdout)
    _check("names the missing scope", "gmail.send" in r.stdout, r.stdout)
    shutil.rmtree(d, ignore_errors=True)

    # gws absent: PATH without gws. Use an empty dir + a minimal PATH that still
    # has python3/bash but no gws. Simplest: point PATH at a dir with only bash
    # symlinks is fragile -> instead run with a sentinel that shadows gws to a
    # non-existent command by using an empty override dir FIRST then a PATH that
    # lacks gws. We approximate by removing gws via a wrapper dir that has a
    # 'gws' that is NOT executable is complex; instead assert the quiet path.
    d_empty = tempfile.mkdtemp(prefix="dgn268-s4nogws-")
    # Build a PATH containing only the dirs needed for bash/python3 but not gws.
    needed = set()
    for tool in ("bash", "python3", "cat", "grep", "sed"):
        p = shutil.which(tool)
        if p:
            needed.add(os.path.dirname(p))
    env = dict(os.environ, PATH=os.pathsep.join(sorted(needed)) or "/bin")
    if shutil.which("gws", path=env["PATH"]) is None:
        r = subprocess.run(["bash", PREFLIGHT], capture_output=True, text=True,
                           env=env)
        _check("gws absent -> exit 1", r.returncode == 1, r.stdout)
        _check("reports gws MISSING", "gws CLI" in r.stdout, r.stdout)
    else:
        _check("gws absent case (skipped -- gws on minimal PATH)", True)
    shutil.rmtree(d_empty, ignore_errors=True)


def _import_i18n(root, lang):
    """Fresh import of mirror_i18n from a scratch mirror with agent.conf
    AGENT_LANG=<lang> and the real locale files copied in."""
    for name in ("mirror_i18n",):
        sys.modules.pop(name, None)
    mdir = os.path.join(root, "mirror")
    os.makedirs(mdir, exist_ok=True)
    shutil.copy2(os.path.join(SRC_MIRROR, "mirror_i18n.py"),
                 os.path.join(mdir, "mirror_i18n.py"))
    cfg = os.path.join(root, "config")
    os.makedirs(os.path.join(cfg, "i18n"), exist_ok=True)
    with open(os.path.join(cfg, "agent.conf"), "w") as fh:
        fh.write("AGENT_LANG=%s\n" % lang)
    for loc in ("en", "ko"):
        shutil.copy2(
            os.path.join(REPO_ROOT, "agents", ".template", "config", "i18n",
                         "%s.json" % loc),
            os.path.join(cfg, "i18n", "%s.json" % loc))
    sys.path.insert(0, mdir)
    import mirror_i18n  # noqa: F401
    m = sys.modules["mirror_i18n"]
    m._reset_cache()
    return m


def test_i18n():
    print("(2) i18n load + zero-delta fallback:")
    root = tempfile.mkdtemp(prefix="dgn268-s4i18n-")
    m = _import_i18n(root, "en")
    _check("en selected -> en cal_description",
           m.t("mirror.cal_description", "FALLBACK").startswith("Managed by"),
           m.t("mirror.cal_description", "FALLBACK"))
    # missing key -> fallback verbatim (zero-delta)
    _check("missing key -> fallback verbatim",
           m.t("mirror.does_not_exist", u"KO-LITERAL") == u"KO-LITERAL",
           "fallback not returned")
    shutil.rmtree(root, ignore_errors=True)

    # ko selected -> ko string
    root = tempfile.mkdtemp(prefix="dgn268-s4i18nko-")
    m = _import_i18n(root, "ko")
    val = m.t("mirror.reconcile_verdict_attention", u"FB")
    _check("ko selected -> ko verdict string", val == u"확인 필요", repr(val))
    shutil.rmtree(root, ignore_errors=True)

    # ko locale values byte-match the notify.py TEMPLATES (zero-delta proof)
    ko = json.load(open(os.path.join(
        REPO_ROOT, "agents", ".template", "config", "i18n", "ko.json")))
    for name in ("sdk_bridge", "http_direct"):
        sys.modules.pop(name, None)
        sys.modules[name] = types.ModuleType(name)
    sys.modules["http_direct"].HttpError = type("E", (Exception,), {})
    sys.modules["sdk_bridge"].ec = types.ModuleType("ec")
    sys.modules.pop("notify", None)
    sys.modules.pop("mirror_i18n", None)
    sys.path.insert(0, SRC_MIRROR)
    import notify  # noqa: F401
    mism = [k for k in notify.TEMPLATES
            if ko.get("mirror.%s" % k) != notify.TEMPLATES[k]]
    _check("ko locale byte-matches notify TEMPLATES (zero-delta)",
           not mism, str(mism))


class _FakeRun:
    def __init__(self, has_send=True):
        self.has_send = has_send
        self.sends = []

    def __call__(self, cmd, capture_output=False, text=False, **kw):
        if cmd[:3] == ["gws", "auth", "status"]:
            scopes = [_CAL, _TASKS] + ([_SEND] if self.has_send else [])
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps({"scopes": scopes}), stderr="")
        if cmd[:5] == ["gws", "gmail", "users", "messages", "send"]:
            body = json.loads(cmd[cmd.index("--json") + 1])
            raw = base64.urlsafe_b64decode(body["raw"].encode())
            self.sends.append(message_from_bytes(raw, policy=_default_policy))
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps({"id": "M1"}), stderr="")
        raise AssertionError("unexpected: %s" % (cmd,))


def test_mailer():
    print("(3) mailer send via fake gws +send:")
    sys.path.insert(0, REPO_ROOT)
    for m in ("service", "service.mailer"):
        sys.modules.pop(m, None)
    from service import mailer
    import subprocess as _sp
    for k in ("EMAIL_ADDRESS", "EMAIL_CC", "PROJECT_ROOT"):
        os.environ.pop(k, None)
    os.environ["EMAIL_ADDRESS"] = "sender@x.com"
    os.environ["EMAIL_CC"] = "owner@x.com"

    fake = _FakeRun(has_send=True)
    orig = _sp.run
    _sp.run = fake
    try:
        res = mailer.send(to="a@b.com", subject="Hello", body="Body text")
    finally:
        _sp.run = orig
    _check("send ok", res.get("ok") is True, res)
    _check("gws +send called once", len(fake.sends) == 1, len(fake.sends))
    if fake.sends:
        msg = fake.sends[0]
        _check("To correct", msg["To"] == "a@b.com", msg["To"])
        _check("Subject correct", msg["Subject"] == "Hello", msg["Subject"])
        _check("body correct", msg.get_content().strip() == "Body text",
               repr(msg.get_content()))
        _check("owner auto-CC'd", "owner@x.com" in (msg["Cc"] or ""), msg["Cc"])

    # not connected (no gmail.send scope)
    fake2 = _FakeRun(has_send=False)
    _sp.run = fake2
    try:
        res2 = mailer.send(to="a@b.com", subject="s", body="b")
    finally:
        _sp.run = orig
    _check("no gmail.send scope -> not connected",
           res2.get("ok") is False and res2.get("connected") is False, res2)
    _check("not-connected points at Google (not app password)",
           "Google" in res2["message"] and
           "password" not in res2["message"].lower(), res2["message"])
    for k in ("EMAIL_ADDRESS", "EMAIL_CC"):
        os.environ.pop(k, None)


def test_install_sh():
    print("(4) install.sh app-password removed + step labels consistent:")
    txt = open(INSTALL).read()
    # No LIVE EMAIL_APP_PASSWORD reference (comments describing removal are ok).
    live = [ln for ln in txt.splitlines()
            if "EMAIL_APP_PASSWORD" in ln and not ln.lstrip().startswith("#")]
    _check("no live EMAIL_APP_PASSWORD reference", not live, str(live[:3]))
    live_pw = [ln for ln in txt.splitlines()
               if "DOGANY_EMAIL_PW" in ln and not ln.lstrip().startswith("#")]
    _check("no live DOGANY_EMAIL_PW reference", not live_pw, str(live_pw[:3]))
    # Step labels: all [N/M] share the same M, and cover 1..M once (x2 = ko+en).
    labels = re.findall(r"\[(\d+)/(\d+)\]", txt)
    denoms = {m for _n, m in labels}
    _check("single step denominator", len(denoms) == 1, str(denoms))
    if denoms:
        M = int(denoms.pop())
        nums = sorted({int(n) for n, _m in labels})
        _check("labels cover 1..N contiguously",
               nums == list(range(1, M + 1)), str(nums))
    # mailer transport is gws, not smtplib.
    mailer_txt = open(os.path.join(REPO_ROOT, "service", "mailer",
                                   "__init__.py")).read()
    _check("mailer no longer imports smtplib",
           "import smtplib" not in mailer_txt, "smtplib still imported")
    _check("mailer uses gws gmail send",
           "gmail" in mailer_txt and "messages" in mailer_txt, "no gws send")


def main():
    test_preflight()
    test_i18n()
    test_mailer()
    test_install_sh()
    print()
    if _failures:
        print("FAILED: %d check(s): %s" % (len(_failures), ", ".join(_failures)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
