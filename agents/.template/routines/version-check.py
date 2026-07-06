#!/usr/bin/env python3
"""version-check.py -- SessionStart hook: notify when a newer framework exists.

Two check modes (in order; both are fail-open and never block a session):

1. Local check (always active): compares the built version (.instance.conf
   DOGANY_FW_VERSION) against the local source repo VERSION file
   (DOGANY_REPO_ROOT). Useful when the user cloned the repo and updates it.

2. Remote check (opt-in only): if DOGANY_VERSION_CHECK=1 is set in the
   instance .env, fetches the raw VERSION file from the public GitHub repo
   over HTTPS (2-second timeout, fail-silent) and nudges if a newer version
   exists. This is a plain GET to a static file. Zero data is sent beyond the
   HTTP request itself -- no token, no id, no payload. Documented below and in
   .env.example.

   PRIVACY: The remote check sends ONLY a GET request to
     https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION
   No user data, no auth token, no instance metadata. The server sees only your
   IP (the same it sees when you install via git clone). Default is OFF.

Design: strictly fail-open. Any missing file, parse error, network error, or
unexpected condition results in exit 0 with no output, so a session is NEVER
blocked.

Output: JSON on stdout matching other SessionStart hooks.
"""
import json
import os
import sys


# Public repo URL for the raw VERSION file (remote opt-in check).
_REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION"
)
_REMOTE_TIMEOUT_S = 2


def _read_conf(path):
    """Parse a simple KEY=VALUE conf file into a dict (ignores comments)."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
    except Exception:
        return {}
    return out


def _read_version(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readline().strip()
    except Exception:
        return ""


def _fetch_remote_version(url, timeout):
    """Fetch the remote VERSION string. Returns empty string on any error.

    PRIVACY: plain GET to a static file. No data sent beyond the request.
    Timeout is hard-capped so the session is never noticeably delayed.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(64).decode("utf-8", errors="ignore")
            return raw.strip().split("\n")[0].strip()
    except Exception:
        return ""


def _env_flag(instance_root, key):
    """Read a single env key from the instance .telegram_bot/.env. Fail silent."""
    env_path = os.path.join(instance_root, ".telegram_bot", ".env")
    conf = _read_conf(env_path)
    return conf.get(key, "")


def _emit_note(built_version, remote_version, source_label):
    note = (
        "[Dogany framework update available] This instance was built from "
        "framework version {built}, but {source} now has version {remote}. "
        "Tell the user, in their language, that a new Dogany version is "
        "available and offer to run ./update.sh to update. "
        "Do NOT auto-update -- ask the user first. update.sh only "
        "refreshes framework code and preserves memories, .env, databases, "
        "and user-authored skills."
    ).format(built=built_version, remote=remote_version, source=source_label)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))


def main():
    # SessionStart delivers a JSON payload on stdin; consume it so the pipe closes.
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Instance root is two levels up from this hook (routines/ -> root).
    here = os.path.dirname(os.path.abspath(__file__))
    instance_root = os.path.dirname(here)

    conf = _read_conf(os.path.join(instance_root, ".instance.conf"))
    built_version = conf.get("DOGANY_FW_VERSION", "")
    repo_root = conf.get("DOGANY_REPO_ROOT", "")

    if not built_version:
        sys.exit(0)

    # --- 1) Local check ---
    if repo_root:
        repo_version = _read_version(os.path.join(repo_root, "VERSION"))
        if repo_version and repo_version != "unknown" and repo_version != built_version:
            _emit_note(built_version, repo_version,
                       "the local source repo at " + repo_root)
            sys.exit(0)

    # --- 2) Remote check (opt-in: DOGANY_VERSION_CHECK=1 in instance .env) ---
    # PRIVACY: sends ONLY a GET to raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION.
    # No user data, no token, no instance metadata. Default OFF.
    version_check_flag = _env_flag(instance_root, "DOGANY_VERSION_CHECK")
    if version_check_flag.strip() == "1":
        remote_version = _fetch_remote_version(_REMOTE_VERSION_URL, _REMOTE_TIMEOUT_S)
        if (remote_version
                and remote_version != "unknown"
                and remote_version != built_version):
            _emit_note(built_version, remote_version,
                       "the upstream dogany-agent public repo")
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
