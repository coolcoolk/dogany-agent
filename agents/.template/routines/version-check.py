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
# Public repo URL for the raw CHANGELOG (DGN-687: release-note fold source).
_REMOTE_CHANGELOG_URL = (
    "https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/CHANGELOG.md"
)
# Public repo raw URL pattern for release note files (DGN-738).
_REMOTE_RELEASE_NOTE_URL = (
    "https://raw.githubusercontent.com/coolcoolk/dogany-agent/main/releases/{}.md"
)
_REMOTE_TIMEOUT_S = 2
_CACHE_TTL_S = 6 * 3600  # 6 hours
_NOTES_MAX_LINES = 30  # cap the release-note fold body (Telegram 4096 limit)
_RENAG_TTL_S = 7 * 24 * 3600  # DGN-735: re-nag at most weekly per version


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


def _version_tuple(v):
    """Parse a dotted version into a tuple of ints, or None if unparseable.

    Drops any pre-release/build suffix (everything after the first '-' or '+')
    and reads the leading digits of each dotted chunk. A chunk with no leading
    digit makes the whole string unparseable (returns None) so the caller can
    fall back to a conservative comparison.
    """
    core = v.strip().split("-")[0].split("+")[0]
    parts = []
    for chunk in core.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _is_newer(candidate, current):
    """True only if `candidate` is a strictly newer version than `current`.

    Numeric dotted comparison (semver-style, zero-padded to equal length). The
    update nudge must fire ONLY when the other side is genuinely ahead -- a
    plain `!=` false-alarms when the local build is newer than a lagging public
    repo (the DGN-349 defect). Falls back to plain inequality only when either
    string cannot be parsed numerically, so exotic version schemes still nudge
    rather than silently regressing.
    """
    ct = _version_tuple(candidate)
    cur = _version_tuple(current)
    if ct is None or cur is None:
        return candidate != current
    n = max(len(ct), len(cur))
    ct = ct + (0,) * (n - len(ct))
    cur = cur + (0,) * (n - len(cur))
    return ct > cur


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


def _fetch_remote_text(url, timeout, max_bytes=131072):
    """Fetch a remote text file. Returns empty string on any error (fail-open).

    PRIVACY: plain GET to a static file, same contract as the VERSION fetch.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read(max_bytes).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_ticket_refs(text):
    """DGN-725: remove (DGN-NNN) and bare DGN-NNN ticket references.

    Covers parenthesized refs like '(DGN-738)' and bare refs like 'DGN-738'.
    Applied to CHANGELOG fallback output only; user-summary is already clean.
    """
    import re
    # Remove parenthesized ticket refs first (may leave stray whitespace).
    text = re.sub(r'\(\s*DGN-\d+\s*\)', '', text)
    # Remove bare ticket refs (preceded by whitespace or start of content).
    text = re.sub(r'\s*DGN-\d+', '', text)
    return text


def _changelog_section(text, version):
    """Extract the CHANGELOG body for `version` (lines between its
    '## [x.y.z]' heading and the next '## [' heading). '' = not found.

    DGN-725: ticket refs are stripped from the output so internal ticket
    numbers never reach the user-facing notice.
    """
    marker = "## [" + version.lstrip("v") + "]"
    grab = False
    out = []
    for line in text.split("\n"):
        if not grab:
            if line.startswith(marker):
                grab = True
            continue
        if line.startswith("## ["):
            break
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    raw = "\n".join(out[:_NOTES_MAX_LINES])
    return _strip_ticket_refs(raw)


def _release_notes_local(repo_root, version):
    """Release notes from the local source repo CHANGELOG. '' on any failure."""
    try:
        path = os.path.join(repo_root, "CHANGELOG.md")
        with open(path, "r", encoding="utf-8") as fh:
            return _changelog_section(fh.read(), version)
    except Exception:
        return ""


def _release_notes_remote(version):
    """Release notes from the upstream public CHANGELOG. '' on any failure."""
    try:
        text = _fetch_remote_text(_REMOTE_CHANGELOG_URL, _REMOTE_TIMEOUT_S)
        if not text:
            return ""
        return _changelog_section(text, version)
    except Exception:
        return ""


def _user_summary_from_note(note_text, lang):
    """DGN-738: extract the localized user-summary for <lang> from a release-
    note file's text. Returns '' if absent (fail-open; caller falls back to
    CHANGELOG scrape).

    Mirrors the self-update.sh user_summary() AWK logic exactly:
      - Enter the block on the '<!-- user-summary' opener.
      - Exit on '-->'.
      - A 'key: |' line opens a YAML block scalar; record its indent level.
      - Continuation lines (indented deeper than the key) belong to that scalar.
      - A line at or shallower than the key indent closes the scalar.
      - Leading indentation is stripped to contind columns (the first non-blank
        continuation line sets contind).
    """
    try:
        in_block = False
        grab = False
        key_ind = 0
        cont_ind = -1
        out = []
        for raw_line in note_text.split("\n"):
            if not in_block:
                stripped = raw_line.strip()
                if stripped == "<!-- user-summary" or stripped.startswith("<!-- user-summary "):
                    in_block = True
                continue
            if raw_line.strip() == "-->":
                break
            # Detect 'key: |' line (YAML block scalar opener).
            import re
            m = re.match(r'^([ \t]*)([A-Za-z0-9_]+):[ \t]*\|[ \t]*$', raw_line)
            if m:
                key = m.group(2)
                key_ind = len(m.group(1).expandtabs(8))
                grab = (key == lang)
                cont_ind = -1
                continue
            if grab:
                if not raw_line.strip():
                    # Blank line inside the scalar.
                    out.append("")
                    continue
                cur_ind = len(raw_line) - len(raw_line.lstrip())
                if cur_ind <= key_ind:
                    # Dedented -> scalar ended.
                    grab = False
                    continue
                if cont_ind < 0:
                    cont_ind = cur_ind
                out.append(raw_line[cont_ind:])
        # Drop leading/trailing blank lines (mirror awk | sed -e '/./,$!d').
        while out and not out[0]:
            out.pop(0)
        while out and not out[-1]:
            out.pop()
        return "\n".join(out[:_NOTES_MAX_LINES])
    except Exception:
        return ""


def _resolve_lang(instance_root):
    """User-notice language: env DOGANY_LANG > config/agent.conf AGENT_LANG > en."""
    lang = os.environ.get("DOGANY_LANG", "").strip()
    if lang:
        return lang
    conf = _read_conf(os.path.join(instance_root, "config", "agent.conf"))
    return conf.get("AGENT_LANG", "").strip() or "en"


# DGN-735: durable per-version guard (supersedes DGN-687 session_id guard).
# The notice is suppressed if the same version was shown within _RENAG_TTL_S.

def _notice_shown_path(instance_root):
    """Return path to the durable version-notice-shown state file."""
    return os.path.join(
        instance_root, ".telegram_bot", "state", "version-notice-shown")


def _read_shown(instance_root):
    """Return (version_str, ts_float) from the shown-flag file.
    Reads one line 'VERSION TS'; on ANY error returns ('', 0.0). Fail-open."""
    try:
        path = _notice_shown_path(instance_root)
        with open(path, "r", encoding="utf-8") as fh:
            line = fh.readline().strip()
        version_str, _, ts_str = line.partition(" ")
        return version_str.strip(), float(ts_str.strip())
    except Exception:
        return "", 0.0


def _write_shown(instance_root, version, now):
    """Persist '{version} {now}' to the shown-flag file. Fail silent."""
    try:
        path = _notice_shown_path(instance_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(version, now))
    except Exception:
        pass


def _should_notice(instance_root, version, now):
    """Return True (show notice) unless the same version was shown within TTL."""
    stored_version, stored_ts = _read_shown(instance_root)
    if stored_version == version and (now - stored_ts) < _RENAG_TTL_S:
        return False
    return True


def _i18n_get(lang, key):
    """config/i18n/<lang>.json lookup (agentlib.sh i18n() mirror): active
    lang first, then en; '' when the key/file is missing or unreadable
    (fail-open, DGN-851). Instance root = parent of this hook's dir."""
    here = os.path.dirname(os.path.abspath(__file__))
    instance_root = os.path.dirname(here)
    for lg in (lang, "en"):
        path = os.path.join(instance_root, "config", "i18n",
                            "{}.json".format(lg))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                val = json.load(fh).get(key)
        except Exception:
            continue
        if isinstance(val, str) and val:
            return val
    return ""


def _build_user_notice(remote_version, notes, lang):
    """DGN-687 notice #1 (update available). Korean copy is LOCKED
    (owner-approved 2026-08-01; single-fold layout owner-directed 2026-08-04,
    DGN-718). en mirrors the structure. Plain text -- SessionStart relay has
    no parse_mode so HTML tags are not rendered (dec-117). Layout preserved:
    header / action / fold_label + notes when present; no-notes = header +
    action only.

    DGN-851: the header carries the PRODUCT name, which is config data --
    resolved from config/i18n key 'update.available' ({version} slot); the
    in-code copy below is the zero-delta fallback (locked ko copy) for
    instances whose locale files lack the key."""
    ver = "v" + remote_version.lstrip("v")
    if lang == "ko":
        header = "도가니 업데이트 있어요 · {}".format(ver)
        action = "지금 업데이트하실래요?"
        fold_label = "▸ 업데이트 노트"
    else:
        header = "Dogany update available · {}".format(ver)
        action = "Update now?"
        fold_label = "▸ Update notes"
    header_tpl = _i18n_get(lang, "update.available")
    if header_tpl:
        header = header_tpl.replace("{version}", ver)
    if notes:
        return "{}\n{}\n{}\n{}".format(header, action, fold_label, notes)
    return "{}\n{}".format(header, action)


def _emit_note(built_version, remote_version, source_label, notes, lang):
    message = _build_user_notice(remote_version, notes, lang)
    note = (
        "[Dogany framework update available] This instance was built from "
        "framework version {built}, but {source} now has version {remote}. "
        "Relay the pre-formatted notice between the NOTICE markers below to "
        "the user VERBATIM as your reply -- do not translate, rewrite, or "
        "wrap it in a code block (this notice is injected at most once per "
        "session). Do NOT auto-update -- wait for the user's approval, then "
        "run routines/self-update.sh as the LAST tool call of that turn (it "
        "wires the bridge restart itself, DGN-685; end the turn right after "
        "with the single install-complete line it prints). The update "
        "preserves memories, .env, databases, and user-authored skills.\n"
        "---NOTICE---\n{message}\n---END NOTICE---"
    ).format(built=built_version, remote=remote_version,
             source=source_label, message=message)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))


def main():
    # SessionStart delivers a JSON payload on stdin; consume it so the pipe
    # closes. session_id kept for context but no longer gates anything (DGN-735).
    session_id = ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = str(payload.get("session_id", "") or "")  # noqa: F841
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

    now = time.time()
    lang = _resolve_lang(instance_root)

    # --- 1) Local check ---
    if repo_root:
        repo_version = _read_version(os.path.join(repo_root, "VERSION"))
        if repo_version and repo_version != "unknown" and _is_newer(repo_version, built_version):
            # DGN-735: suppress if same version shown within weekly TTL.
            if not _should_notice(instance_root, repo_version, now):
                sys.exit(0)
            # DGN-738: prefer localized user-summary from releases/vX.Y.Z.md.
            # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/
            # developer-facing (DGN-210) and contains raw markdown, backticks,
            # and internal names that must never reach user-facing output.
            # When the user-summary block is absent, show no fold body.
            vdisp = "v" + repo_version.lstrip("v")
            notes_path = os.path.join(repo_root, "releases", vdisp + ".md")
            try:
                with open(notes_path, "r", encoding="utf-8") as _fh:
                    _note_text = _fh.read()
                notes = _user_summary_from_note(_note_text, lang)
            except Exception:
                notes = ""
            _emit_note(built_version, repo_version,
                       "the local source repo at " + repo_root,
                       notes, lang)
            _write_shown(instance_root, repo_version, now)
            sys.exit(0)

    # --- 2) Remote check (default ON; opt-out: DOGANY_VERSION_CHECK=0 in instance .env) ---
    # PRIVACY: sends ONLY a GET to raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION.
    # No user data, no token, no instance metadata. Default ON.
    if not _remote_check_enabled(instance_root):
        sys.exit(0)

    # Throttle: use cached result if younger than 6 hours.
    cached_ts, cached_ver = _read_cache(instance_root)
    if cached_ver and (now - cached_ts) < _CACHE_TTL_S:
        remote_version = cached_ver
    else:
        remote_version = _fetch_remote_version(_REMOTE_VERSION_URL, _REMOTE_TIMEOUT_S)
        if remote_version:
            _write_cache(instance_root, remote_version)

    if (remote_version
            and remote_version != "unknown"
            and _is_newer(remote_version, built_version)):
        # DGN-735: suppress if same version shown within weekly TTL.
        if not _should_notice(instance_root, remote_version, now):
            sys.exit(0)
        # DGN-738: prefer localized user-summary from the public release note.
        # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/
        # developer-facing (DGN-210) and contains raw markdown, backticks,
        # and internal names that must never reach user-facing output.
        # When the user-summary block is absent or fetch fails, show no fold body.
        notes = ""
        try:
            vdisp = "v" + remote_version.lstrip("v")
            note_url = _REMOTE_RELEASE_NOTE_URL.format(vdisp)
            note_text = _fetch_remote_text(note_url, _REMOTE_TIMEOUT_S)
            if note_text:
                notes = _user_summary_from_note(note_text, lang)
        except Exception:
            notes = ""
        _emit_note(built_version, remote_version,
                   "the upstream dogany-agent public repo",
                   notes, lang)
        _write_shown(instance_root, remote_version, now)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
