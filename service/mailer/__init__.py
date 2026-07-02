"""service.mailer -- stable SDK facade for sending email.

Skills import THIS package (service.mailer), never smtplib directly. The module
name is 'mailer' on purpose: naming it 'email' would shadow the Python stdlib
'email' package that we rely on to build MIME messages.

Config is read from the INSTANCE .env (gitignored), NOT hardcoded. Keys:
  EMAIL_ADDRESS       sender / SMTP login user
  EMAIL_APP_PASSWORD  app password (Gmail app-password, never logged)
  EMAIL_CC            owner address auto-CC'd on every send
  SMTP_HOST           optional, default smtp.gmail.com
  SMTP_PORT           optional, default 587 (STARTTLS)

The .env is loaded from PROJECT_ROOT/.telegram_bot/.env (matches bridge/config.py
BOT_DATA_DIR), with a fallback to the process environment. No credentials live in
this repo -- they fill into the gitignored instance config at connect-time.

Graceful degradation: if the sender address or app password is missing, send()
returns a clear "email not connected" result instead of raising, so the agent can
tell the user to connect email rather than crashing.
"""

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

__all__ = ["send", "is_configured", "load_config"]

# Default SMTP endpoint (Gmail STARTTLS). Overridable via config.
_DEFAULT_SMTP_HOST = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = 587


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
    """Minimal .env parser: KEY=VALUE lines, no external dependency.

    We do not use python-dotenv here to keep the service import-light and usable
    from any cwd. Comments (#) and blank lines are skipped. Existing process env
    wins over the file (so an explicit export can override).
    """
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

    Process env takes precedence over the .env file. Missing keys are simply
    absent from the returned dict (callers check is_configured / send handles it).
    """
    cfg = {}
    env_path = _project_env_path()
    if env_path is not None and env_path.exists():
        cfg.update(_read_env_file(env_path))
    # Process env overrides file values.
    for key in ("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "EMAIL_CC",
                "SMTP_HOST", "SMTP_PORT"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def is_configured(cfg=None):
    """True when both sender address and app password are present."""
    if cfg is None:
        cfg = load_config()
    return bool(cfg.get("EMAIL_ADDRESS")) and bool(cfg.get("EMAIL_APP_PASSWORD"))


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
            "Email is not connected. Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD "
            "in the instance config (.telegram_bot/.env) to enable sending."
        ),
    }


def send(to, subject, body, cc=None, attachments=None):
    """Send an email via SMTP STARTTLS. Returns a result dict (never raises on
    a missing-config or normal SMTP error -- errors come back in the dict).

    Args:
        to:          recipient address, or list/comma-string of addresses.
        subject:     subject line.
        body:        plain-text body.
        cc:          optional extra CC address(es). The owner's EMAIL_CC is
                     ALWAYS added (RULES: CC user's mail) unless already present.
        attachments: optional list of file paths to attach.

    Returns dict with keys: ok(bool), connected(bool), and on success
        to(list), cc(list), subject(str); on failure error(str)/message(str).
    The app password is NEVER placed in the returned dict or any log line.
    """
    cfg = load_config()

    if not is_configured(cfg):
        return _not_connected_result()

    sender = cfg["EMAIL_ADDRESS"]
    password = cfg["EMAIL_APP_PASSWORD"]
    host = cfg.get("SMTP_HOST") or _DEFAULT_SMTP_HOST
    try:
        port = int(cfg.get("SMTP_PORT") or _DEFAULT_SMTP_PORT)
    except (TypeError, ValueError):
        port = _DEFAULT_SMTP_PORT

    to_list = _as_list(to)
    if not to_list:
        return {
            "ok": False,
            "connected": True,
            "error": "no recipient",
            "message": "No recipient address provided.",
        }

    cc_list = _as_list(cc)
    # Auto-CC the owner unless already present (case-insensitive) in to or cc.
    owner_cc = (cfg.get("EMAIL_CC") or "").strip()
    if owner_cc:
        seen = {a.lower() for a in to_list + cc_list}
        if owner_cc.lower() not in seen:
            cc_list.append(owner_cc)

    msg = EmailMessage()
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
                data,
                maintype="application",
                subtype="octet-stream",
                filename=p.name,
            )
        except OSError as exc:
            return {
                "ok": False,
                "connected": True,
                "error": "attachment error",
                "message": "Cannot read attachment %s: %s" % (path, exc),
            }

    # All recipients (To + Cc) get the message; smtplib needs the full envelope.
    all_rcpts = to_list + cc_list

    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(msg, from_addr=sender, to_addrs=all_rcpts)
    except (smtplib.SMTPException, OSError) as exc:
        # Never include the password. exc from smtplib carries only protocol info.
        return {
            "ok": False,
            "connected": True,
            "error": "send failed",
            "message": "SMTP send failed: %s" % (exc,),
        }

    return {
        "ok": True,
        "connected": True,
        "to": to_list,
        "cc": cc_list,
        "subject": subject or "",
    }
