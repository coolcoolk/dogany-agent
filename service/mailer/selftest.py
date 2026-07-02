"""Mock-SMTP self-test for service.mailer. No real network, no real send.

Monkeypatches smtplib.SMTP with a fake that records starttls/login/send_message
calls and the composed message. Verifies configured + unconfigured cases and that
the app password never leaks into the returned result.

Run: python3 service/mailer/selftest.py   (from repo root)
Exit 0 = all assertions passed.
"""

import os
import sys

# Import the module under a fresh state. Add repo root so `service` is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)

import smtplib  # noqa: E402
from service import mailer  # noqa: E402

APP_PW = "super-secret-app-pw-XYZ"


class FakeSMTP:
    """Records SMTP interactions instead of touching the network."""

    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.ehlo_count = 0
        self.starttls_called = False
        self.login_args = None
        self.sent_message = None
        self.sent_from = None
        self.sent_to = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self, *a, **k):
        self.ehlo_count += 1

    def starttls(self, *a, **k):
        self.starttls_called = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent_message = msg
        self.sent_from = from_addr
        self.sent_to = to_addrs


def _reset_env():
    for k in ("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "EMAIL_CC",
              "SMTP_HOST", "SMTP_PORT", "PROJECT_ROOT"):
        os.environ.pop(k, None)


def test_configured():
    _reset_env()
    os.environ["EMAIL_ADDRESS"] = "sender@example.com"
    os.environ["EMAIL_APP_PASSWORD"] = APP_PW
    os.environ["EMAIL_CC"] = "owner@example.com"

    FakeSMTP.instances = []
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        smtplib.SMTP = orig

    assert res["ok"] is True, res
    assert len(FakeSMTP.instances) == 1, "SMTP should be constructed once"
    smtp = FakeSMTP.instances[0]

    assert smtp.host == "smtp.gmail.com", smtp.host
    assert smtp.port == 587, smtp.port
    assert smtp.starttls_called is True, "STARTTLS must be called"
    assert smtp.login_args == ("sender@example.com", APP_PW), smtp.login_args

    msg = smtp.sent_message
    assert msg["To"] == "a@b.com", msg["To"]
    assert "owner@example.com" in msg["Cc"], msg["Cc"]
    assert msg["Subject"] == "s", msg["Subject"]
    assert msg.get_content().strip() == "hi", repr(msg.get_content())
    # envelope recipients include the auto-CC
    assert "owner@example.com" in smtp.sent_to, smtp.sent_to
    assert "a@b.com" in smtp.sent_to, smtp.sent_to

    # App password must NOT leak into the returned result.
    assert APP_PW not in repr(res), "app password leaked into result!"
    print("PASS configured: STARTTLS + login(pw from config) + To/Cc/Subject/body OK")


def test_no_double_cc():
    """Owner already a recipient -> not added twice."""
    _reset_env()
    os.environ["EMAIL_ADDRESS"] = "sender@example.com"
    os.environ["EMAIL_APP_PASSWORD"] = APP_PW
    os.environ["EMAIL_CC"] = "owner@example.com"

    FakeSMTP.instances = []
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        res = mailer.send(to="owner@example.com", subject="s", body="hi")
    finally:
        smtplib.SMTP = orig
    assert res["ok"] is True, res
    assert res["cc"] == [], res["cc"]  # owner already in To -> no dup CC
    print("PASS no-double-cc: owner already recipient -> no duplicate CC")


def test_unconfigured():
    _reset_env()  # no creds at all

    FakeSMTP.instances = []
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        smtplib.SMTP = orig

    assert res["ok"] is False, res
    assert res["connected"] is False, res
    assert res["error"] == "email not connected", res
    assert len(FakeSMTP.instances) == 0, "must NOT attempt SMTP when unconfigured"
    print("PASS unconfigured: returns not-connected, no SMTP attempt, no raise")


def test_password_never_in_log_string():
    """The graceful message + success result must never carry the password."""
    _reset_env()
    os.environ["EMAIL_ADDRESS"] = "sender@example.com"
    os.environ["EMAIL_APP_PASSWORD"] = APP_PW
    os.environ["EMAIL_CC"] = "owner@example.com"

    FakeSMTP.instances = []
    orig = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        res = mailer.send(to="a@b.com", subject="s", body="hi")
    finally:
        smtplib.SMTP = orig
    assert APP_PW not in str(res), "password in str(result)!"
    assert APP_PW not in repr(res), "password in repr(result)!"
    print("PASS secrecy: app password absent from returned result string")


if __name__ == "__main__":
    test_configured()
    test_no_double_cc()
    test_unconfigured()
    test_password_never_in_log_string()
    print("ALL PASS")
