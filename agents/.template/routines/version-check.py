#!/usr/bin/env python3
"""version-check.py -- SessionStart hook: notify when a newer framework exists.

Two check modes (in order; both are fail-open and never block a session):

1. Local check (always active): compares the built version (.instance.conf
   DOGANY_FW_VERSION) against the local source repo VERSION file
   (DOGANY_REPO_ROOT). Useful when the user cloned the repo and updates it.

2. Remote check (default ON, opt-out): fetches the raw VERSION file from the
   public GitHub repo over HTTPS (2-second timeout, fail-silent) and nudges if
   a newer version exists. This is a plain GET to a static file. Zero data is
   sent beyond the HTTP request itself -- no token, no id, no payload.
   Set DOGANY_VERSION_CHECK=0 (or false/no/off, case-insensitive) in the
   instance .env to disable. Documented below and in .env.example.

   PRIVACY: The remote check sends ONLY a GET request to
     https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION
   No user data, no auth token, no instance metadata. The server sees only your
   IP (the same it sees when you install via git clone). Default is ON.
   To opt out, set DOGANY_VERSION_CHECK=0 in your instance .telegram_bot/.env.

   Throttle: the result of a successful remote check is cached for 6 hours in
   .telegram_bot/state/version-check-cache. If the cache is younger than 6
   hours the cached version string is used and no network call is made. Cache
   read/write failures are silently ignored (fail-open).

Design: strictly fail-open. Any missing file, parse error, network error, or
unexpected condition results in exit 0 with no output, so a session is NEVER
blocked.

Output: JSON on stdout matching other SessionStart hooks.
"""
import json
import os
import sys
import time


# Public repo URL for the raw VERSION file (remote check).
_REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION"
)
_REMOTE_TIMEOUT_S = 2
_CACHE_TTL_S = 6 * 3600  # 6 hours


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


def _cache_path(instance_root):
    """Return the path to the version-check cache file."""
    return os.path.join(instance_root, ".telegram_bot", "state", "version-check-cache")


def _read_cache(instance_root):
    """Return (timestamp, version_string) from cache, or (0, '') on any failure."""
    try:
        path = _cache_path(instance_root)
        with open(path, "r", encoding="utf-8") as fh:
            line = fh.readline().strip()
        ts_str, _, ver = line.partition(" ")
        return float(ts_str), ver.strip()
    except Exception:
        return 0.0, ""


def _write_cache(instance_root, version_string):
    """Persist (now, version_string) to cache. Fail silent on any error."""
    try:
        path = _cache_path(instance_root)
        state_dir = os.path.dirname(path)
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(time.time(), version_string))
    except Exception:
        pass


def _env_flag(instance_root, key):
    """Read a single env key from the instance .telegram_bot/.env. Fail silent."""
    env_path = os.path.join(instance_root, ".telegram_bot", ".env")
    conf = _read_conf(env_path)
    return conf.get(key, "")


def _remote_check_enabled(instance_root):
    """Return True unless DOGANY_VERSION_CHECK is set to a falsy value."""
    val = _env_flag(instance_root, "DOGANY_VERSION_CHECK").strip().lower()
    # Unset or empty -> ON (default). Explicit opt-out values -> OFF.
    if val in ("0", "false", "no", "off"):
        return False
    return True


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

    # --- 2) Remote check (default ON; opt-out: DOGANY_VERSION_CHECK=0 in instance .env) ---
    # PRIVACY: sends ONLY a GET to raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION.
    # No user data, no token, no instance metadata. Default ON.
    if not _remote_check_enabled(instance_root):
        sys.exit(0)

    # Throttle: use cached result if younger than 6 hours.
    cached_ts, cached_ver = _read_cache(instance_root)
    now = time.time()
    if cached_ver and (now - cached_ts) < _CACHE_TTL_S:
        remote_version = cached_ver
    else:
        remote_version = _fetch_remote_version(_REMOTE_VERSION_URL, _REMOTE_TIMEOUT_S)
        if remote_version:
            _write_cache(instance_root, remote_version)

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
