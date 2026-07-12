"""Self-test for service.mailer (DGN-268 S4: gws gmail transport).

Monkeypatches subprocess.run so no real gws call / network happens: the fake
answers `gws auth status` (authed with gmail.send or not) and records the
`gws gmail users messages send` invocation (its --json raw payload). Verifies
configured + unconfigured cases, the auto-CC, and that the composed To/Cc/
Subject/body round-trip through the base64url raw message.

Run: python3 service/mailer/selftest.py   (from repo root). Exit 0 = pass.
"""

import base64
import json
import os
import sys
from email import message_from_bytes
from email.policy import default as _default_policy

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)

import subprocess  # noqa: E402
from service import mailer  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Records gws calls; answers auth status + send. scopes controls whether
    the account is authed with gmail.send."""

    def __init__(self, has_send_scope=True):
        self.has_send_scope = has_send_scope
        self.sends = []   # list of parsed raw messages

    def __call__(self, cmd, capture_output=False, text=False, **kw):
        # auth status
        if cmd[:3] == ["gws", "auth", "status"]:
            scopes = ["https://www.googleapis.com/auth/calendar",
                      "https://www.googleapis.com/auth/tasks"]
            if self.has_send_scope:
                scopes.append("https://www.googleapis.com/auth/gmail.send")
            return FakeCompleted(0, json.dumps({"scopes": scopes}))
        # gmail send
        if cmd[:5] == ["gws", "gmail", "users", "messages", "send"]:
            body = json.loads(cmd[cmd.index("--json") + 1])
            raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
            self.sends.append(message_from_bytes(raw, policy=_default_policy))
            return FakeCompleted(0, json.dumps({"id": "SENT1"}))
        raise AssertionError("unexpected gws call: %s" % (cmd,))


def _reset_env():
    for k in ("EMAIL_ADDRESS", "EMAIL_CC", "PROJECT_ROOT"):
        os.environ.pop(k, None)


def test_configured():
    _reset_env()
    os.environ["EMAIL_ADDRESS"] = "sender@example.com"
    os.environ["EMAIL_CC"] = "owner@example.com"
    fake = FakeRun(has_send_scope=True)
    orig = subprocess.run
    subprocess.run = fake
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        subprocess.run = orig
    assert res["ok"] is True, res
    assert len(fake.sends) == 1, "gws send should be called once"
    msg = fake.sends[0]
    assert msg["To"] == "a@b.com", msg["To"]
    assert "owner@example.com" in (msg["Cc"] or ""), msg["Cc"]
    assert msg["Subject"] == "s", msg["Subject"]
    assert msg.get_content().strip() == "hi", repr(msg.get_content())
    assert "owner@example.com" in res["cc"], res["cc"]
    print("PASS configured: gws send + To/Cc/Subject/body round-trip OK")


def test_no_double_cc():
    _reset_env()
    os.environ["EMAIL_ADDRESS"] = "sender@example.com"
    os.environ["EMAIL_CC"] = "owner@example.com"
    fake = FakeRun(has_send_scope=True)
    orig = subprocess.run
    subprocess.run = fake
    try:
        res = mailer.send(to="owner@example.com", subject="s", body="hi")
    finally:
        subprocess.run = orig
    assert res["ok"] is True, res
    assert res["cc"] == [], res["cc"]  # owner already in To -> no dup CC
    print("PASS no-double-cc: owner already recipient -> no duplicate CC")


def test_unconfigured_no_scope():
    """gws authed but WITHOUT gmail.send -> not connected, no send attempt."""
    _reset_env()
    fake = FakeRun(has_send_scope=False)
    orig = subprocess.run
    subprocess.run = fake
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        subprocess.run = orig
    assert res["ok"] is False, res
    assert res["connected"] is False, res
    assert res["error"] == "email not connected", res
    assert len(fake.sends) == 0, "must NOT attempt send when unconnected"
    # message points at Google onboarding, not app password.
    assert "Google" in res["message"], res["message"]
    assert "password" not in res["message"].lower(), res["message"]
    print("PASS unconfigured: no gmail.send scope -> not connected, no send")


def test_gws_missing():
    """gws CLI absent -> not connected (no crash)."""
    _reset_env()

    def _raise(*a, **k):
        raise FileNotFoundError("gws")
    orig = subprocess.run
    subprocess.run = _raise
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        subprocess.run = orig
    assert res["ok"] is False and res["connected"] is False, res
    print("PASS gws-missing: absent CLI -> graceful not-connected")


if __name__ == "__main__":
    test_configured()
    test_no_double_cc()
    test_unconfigured_no_scope()
    test_gws_missing()
    print("ALL PASS")
