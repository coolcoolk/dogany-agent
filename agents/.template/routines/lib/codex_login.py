#!/usr/bin/env python3
"""codex_login.py -- Codex sign-in state and the API-key login (DGN-1985).

One owner of "is Codex onboarded on this host". The flag is DERIVED, never
hand-set: onboarded == `codex login status` reports a login. Readers (the
/usage passes view, DGN-1984; the dispatcher's codex runtime; the bridge's
/login Codex screen) call this module instead of parsing the CLI themselves.

codex-cli 0.161.0, measured: `codex login --device-auth`,
`codex login --with-api-key` (key read from stdin), `codex login status`.
`login status` writes its one line to stderr (stdout is read too):
  "Logged in using ChatGPT"                 rc 0 -> method chatgpt
  "Logged in using an API key - <masked>"   rc 0 -> method api_key
  "Not logged in"                           rc 1 -> logged out
  anything else / timeout / rc!=0           -> error (not onboarded)
The masked key tail on the api_key line is never returned or logged.

API-key path: the key is collected by the secret intake (DGN-1981: chat ->
keychain), read back here through the SecretIntake interface and written to
the child's stdin only. It is never an argv entry, never printed, never
logged; the child's output is reduced to a reason code before it leaves.
load_intake adapts the shared store to the named read_secret interface and
reports "intake_unavailable" if the store module cannot be loaded.

Verbs (stdout is one JSON object, except resolve):
  status    {"onboarded": bool, "state": logged_in|logged_out|no_cli|error,
             "method": chatgpt|api_key|null}            exit 0
  onboarded exit 0 when onboarded, 1 otherwise (shell readers)
  resolve   the codex binary path; exit 1 when none
  apikey --secret NAME
            {"ok": bool, "reason": str, ...status fields} exit 0 ok, 1 not
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

STATUS_TIMEOUT_S = 20.0
LOGIN_TIMEOUT_S = 60.0
SECRET_NAME = "codex-api-key"          # the intake's name for this key
INTAKE_LIB = "secret_store.py"         # DGN-1981 host lib, sibling of this one

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_IN_CHATGPT = re.compile(r"^\s*logged in using chatgpt\b", re.I | re.M)
_IN_APIKEY = re.compile(r"^\s*logged in using an api key\b", re.I | re.M)
_IN_ANY = re.compile(r"^\s*logged in\b", re.I | re.M)
_OUT = re.compile(r"^\s*not logged in\b", re.I | re.M)


def strip_ansi(text):
    return _ANSI_RE.sub("", text or "")


def parse_status(rc, text):
    """(rc, combined stdout+stderr) of `codex login status` -> status dict."""
    clean = strip_ansi(text)
    if _OUT.search(clean):
        return {"onboarded": False, "state": "logged_out", "method": None}
    if rc == 0 and _IN_ANY.search(clean):
        method = ("chatgpt" if _IN_CHATGPT.search(clean)
                  else "api_key" if _IN_APIKEY.search(clean) else None)
        return {"onboarded": True, "state": "logged_in", "method": method}
    return {"onboarded": False, "state": "error", "method": None}


def resolve_codex(env=None):
    """Path of the codex CLI: $CODEX_BIN, then PATH, then the usual install
    dirs (a launchd/systemd PATH is short). None when not installed."""
    env = os.environ if env is None else env
    explicit = env.get("CODEX_BIN")
    if explicit:
        p = os.path.expanduser(explicit)
        return p if os.access(p, os.X_OK) else None
    found = shutil.which("codex", path=env.get("PATH"))
    if found:
        return found
    home = env.get("HOME") or os.path.expanduser("~")
    for p in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex",
              os.path.join(home, ".local", "bin", "codex"),
              os.path.join(home, ".npm-global", "bin", "codex")):
        if os.access(p, os.X_OK):
            return p
    return None


def status(env=None, timeout=STATUS_TIMEOUT_S):
    """The derived onboarded flag, read live from the CLI."""
    env = dict(os.environ if env is None else env)
    cli = resolve_codex(env)
    if not cli:
        return {"onboarded": False, "state": "no_cli", "method": None}
    try:
        proc = subprocess.run([cli, "login", "status"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout,
                              env=env, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {"onboarded": False, "state": "error", "method": None}
    return parse_status(proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or ""))


class StoreIntake:
    """Named read adapter for the shared store; keys never leave via stdout."""

    def __init__(self, store):
        self._store = store

    def read_secret(self, name):
        if name != SECRET_NAME:
            return None
        return self._store.get("dogany-codex-api-key", "dogany")


def load_intake(lib_dir=None):
    """Read the Codex slot through DGN-1981's shared secret store."""
    lib_dir = lib_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(lib_dir, INTAKE_LIB)
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("codex_secret_store", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return StoreIntake(mod) if callable(getattr(mod, "get", None)) else None


def login_with_api_key(intake, name=SECRET_NAME, env=None, timeout=LOGIN_TIMEOUT_S):
    """Read the key from the intake and pipe it to `codex login --with-api-key`.
    -> {"ok", "reason", ...status}. The key stays in this frame only."""
    if intake is None:
        return dict(status(env), ok=False, reason="intake_unavailable")
    env = dict(os.environ if env is None else env)
    cli = resolve_codex(env)
    if not cli:
        return {"ok": False, "reason": "no_cli", "onboarded": False,
                "state": "no_cli", "method": None}
    try:
        key = intake.read_secret(name)
    except Exception:  # noqa: BLE001 -- never echo what the intake raised
        key = None
    if isinstance(key, str):
        key = key.encode()
    if not key or not key.strip():
        return dict(status(env), ok=False, reason="no_secret")
    try:
        proc = subprocess.run([cli, "login", "--with-api-key"], input=key.strip() + b"\n",
                              capture_output=True, timeout=timeout, env=env)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = None
    except OSError:
        rc = None
    finally:
        key = None  # drop the only reference
    if rc is None:
        return dict(status(env), ok=False, reason="login_error")
    st = status(env)
    if rc != 0:
        return dict(st, ok=False, reason="login_failed")
    if not st["onboarded"]:
        return dict(st, ok=False, reason="status_not_logged_in")
    return dict(st, ok=True, reason="ok")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("onboarded")
    sub.add_parser("resolve")
    k = sub.add_parser("apikey")
    k.add_argument("--secret", default=SECRET_NAME)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status()))
        return 0
    if a.cmd == "onboarded":
        return 0 if status()["onboarded"] else 1
    if a.cmd == "resolve":
        cli = resolve_codex()
        if not cli:
            return 1
        print(cli)
        return 0
    res = login_with_api_key(load_intake(), a.secret)
    print(json.dumps(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
