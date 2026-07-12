"""service.mailer -- stable SDK facade for sending email.

Skills import THIS package (service.mailer), never the transport directly. The
module name is 'mailer' on purpose: naming it 'email' would shadow the Python
stdlib 'email' package we rely on to build MIME messages.

DGN-268 S4 -- transport is the Google Workspace CLI `gws gmail users messages
send` (per-user OAuth, one Google login covering calendar + tasks + gmail.send).
The SMTP + app-password path is removed: email now connects through the SAME
Google auth the agent's mirror onboarding sets up. No shared secret, no
EMAIL_APP_PASSWORD.

Config is read from the INSTANCE .env (gitignored), NOT hardcoded. Keys:
  EMAIL_ADDRESS   sender identity (From:); optional -- gws sends as the authed
                  account, so this is only a display/From hint.
  EMAIL_CC        owner address auto-CC'd on every send (RULES: CC the user).

"Connected" now means: the gws CLI is installed AND authed with the gmail.send
scope. send() checks this and returns a clear "not connected" result (never
raises) so the agent can point the user at Google onboarding instead of
crashing.
"""

import base64
import json
import os
import subprocess
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

__all__ = ["send", "is_configured", "load_config"]


def _project_env_path():
    """Resolve the instance .env path (PROJECT_ROOT/.telegram_bot/.env).

    PROJECT_ROOT is set by the bridge before import. If unset (e.g. a bare skill
    call), fall back to the current environment only -- no crash.
    """
    root = os.environ.get("PROJECT_ROOT")
    if not root:
        return None
    return Path(root) / ".telegram_bot" / ".env"


def _read_env_file(path):
    """Minimal .env parser: KEY=VALUE lines, no external dependency."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def load_config():
    """Return the mailer config dict from instance .env + process env.

    Process env takes precedence over the .env file. Only the transport-neutral
    keys remain (EMAIL_ADDRESS / EMAIL_CC); the SMTP + app-password keys are
    gone with the gws re-wire.
    """
    cfg = {}
    env_path = _project_env_path()
    if env_path is not None and env_path.exists():
        cfg.update(_read_env_file(env_path))
    for key in ("EMAIL_ADDRESS", "EMAIL_CC"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def _gws_authed_with_send():
    """True when gws is installed AND authed with the gmail.send scope.

    A missing CLI, an unauthed account, or a missing scope all mean "not
    connected" -- the agent should route the user to Google onboarding.
    """
    try:
        proc = subprocess.run(["gws", "auth", "status"],
                              capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    try:
        scopes = json.loads(proc.stdout).get("scopes", [])
    except (ValueError, TypeError):
        return False
    return any("/auth/gmail.send" in s for s in scopes)


def is_configured(cfg=None):
    """True when email can actually be sent: gws installed + authed with the
    gmail.send scope. EMAIL_ADDRESS is no longer required (gws sends as the
    authed account)."""
    return _gws_authed_with_send()


def _as_list(value):
    """Normalize a to/cc arg (str or iterable) into a clean list of addresses."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [v.strip() for v in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    return [str(v).strip() for v in value if str(v).strip()]


def _not_connected_result():
    return {
        "ok": False,
        "connected": False,
        "error": "email not connected",
        "message": (
            "Email is not connected. Connect your Google account during agent "
            "setup (the same login that enables calendar sync) -- ask me to "
            "connect Google, then I can send mail."
        ),
    }


def _build_raw(sender, to_list, cc_list, subject, body, attachments):
    """Build an RFC822 message and return its base64url-encoded 'raw' form for
    the Gmail API (gws gmail users messages send). Returns (raw, error_dict):
    error_dict is None on success, else a ready-to-return failure result."""
    msg = EmailMessage()
    if sender:
        msg["From"] = formataddr((None, sender))
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject or ""
    msg.set_content(body or "")

    for path in (attachments or []):
        try:
            p = Path(path)
            data = p.read_bytes()
            msg.add_attachment(
                data, maintype="application", subtype="octet-stream",
                filename=p.name)
        except OSError as exc:
            return None, {
                "ok": False, "connected": True, "error": "attachment error",
                "message": "Cannot read attachment %s: %s" % (path, exc)}

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw, None


def send(to, subject, body, cc=None, attachments=None):
    """Send an email via `gws gmail users messages send`. Returns a result dict
    (never raises on a missing-config or normal send error -- errors come back
    in the dict).

    Args:
        to:          recipient address, or list/comma-string of addresses.
        subject:     subject line.
        body:        plain-text body.
        cc:          optional extra CC address(es). The owner's EMAIL_CC is
                     ALWAYS added (RULES: CC user's mail) unless already present.
        attachments: optional list of file paths to attach.

    Returns dict with keys: ok(bool), connected(bool), and on success
        to(list), cc(list), subject(str); on failure error(str)/message(str).
    """
    cfg = load_config()

    if not is_configured(cfg):
        return _not_connected_result()

    sender = cfg.get("EMAIL_ADDRESS") or ""

    to_list = _as_list(to)
    if not to_list:
        return {
            "ok": False, "connected": True, "error": "no recipient",
            "message": "No recipient address provided."}

    cc_list = _as_list(cc)
    # Auto-CC the owner unless already present (case-insensitive) in to or cc.
    owner_cc = (cfg.get("EMAIL_CC") or "").strip()
    if owner_cc:
        seen = {a.lower() for a in to_list + cc_list}
        if owner_cc.lower() not in seen:
            cc_list.append(owner_cc)

    raw, err = _build_raw(sender, to_list, cc_list, subject, body, attachments)
    if err is not None:
        return err

    try:
        proc = subprocess.run(
            ["gws", "gmail", "users", "messages", "send",
             "--params", json.dumps({"userId": "me"}),
             "--json", json.dumps({"raw": raw})],
            capture_output=True, text=True)
    except (OSError, FileNotFoundError) as exc:
        return {
            "ok": False, "connected": True, "error": "send failed",
            "message": "gws send failed: %s" % (exc,)}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        return {
            "ok": False, "connected": True, "error": "send failed",
            "message": "gws send failed: %s" % (detail or "unknown error")}
    # A successful gws call returns the sent message JSON; a body with an
    # "error" object is an API-level failure even on exit 0.
    try:
        resp = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        resp = {}
    if isinstance(resp.get("error"), dict):
        return {
            "ok": False, "connected": True, "error": "send failed",
            "message": "gws send failed: %s"
                       % resp["error"].get("message", "API error")}

    return {
        "ok": True, "connected": True,
        "to": to_list, "cc": cc_list, "subject": subject or "",
    }
