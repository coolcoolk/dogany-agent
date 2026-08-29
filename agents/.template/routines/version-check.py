#!/usr/bin/env python3
"""version-check.py -- SessionStart hook: notify when a newer framework exists.

Three check modes (in order; ALL are fail-open and never block a session).
Checks 1/2 answer "haven't received it yet"; check 3 answers a DIFFERENT
question -- "received it, but the running process hasn't picked it up yet"
(DGN-986 [e]; the Ag incident class). Checks 1/2 always exit(0) right after
emitting, so check 3 only runs when NEITHER of them already fired -- see the
ordering rationale in check 3's comment below.

1. Local check (always active): compares the built version (.instance.conf
   DOGANY_FW_VERSION) against the latest STABLE tag (vX.Y.Z, no pre-release
   suffix) of the local source repo (DOGANY_REPO_ROOT). Useful when the user
   cloned the repo and updates it.

   DGN-1068: the repo VERSION file is NOT an advertising source. release.sh
   bumps VERSION before minting the tag, so mid-release VERSION names a
   version that does not exist yet (the "ghost window" -- the v1.43.0
   incident, 2026-08-24: an instance advertised an untagged, unpushed
   version to the owner). A tag is what an update can actually deliver
   (self-update.sh channel=release installs the latest stable tag), so the
   tag is the truth source. VERSION is read only to LOG an open ghost
   window, never to trigger the notice.

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

3. Landed-but-not-running check (DGN-986 [e]): compares the boot snapshot's
   `fw_version` (.telegram_bot/runtime-snapshot.json, written by
   bridge/boot_snapshot.py at bridge startup -- DGN-986 [b]) against
   .instance.conf DOGANY_FW_VERSION. If the conf side is strictly newer, the
   update landed on disk but the currently-running process booted from an
   older version and was never restarted to consume it. Pre-2.0 instances
   have no snapshot file at all -- that is a silent skip, not a warning
   (fail-open, same as every other absence in this hook). A snapshot whose
   `pid` is no longer alive is ALSO a silent skip (crash-restart race: the
   process that wrote it already exited, so its version claim can no longer
   be attributed to "the currently running process" -- see _pid_alive). No
   mtime fallback is used here (unlike /health's 2nd-tier heuristic) -- a
   false positive in a user-facing nudge is worse than a missed one.

   DGN-1068: before advertising a remote candidate (check 2), the tag ref is
   confirmed to exist upstream (GET of the raw VERSION file AT the tag ref --
   200 proves the tag exists). Unconfirmable (404 or network failure) -> the
   notice is HELD, not shown (fail-closed for advertising: better to stay
   quiet than to advertise a version that cannot be downloaded).

Design: strictly fail-open for SESSION safety (any missing file, parse
error, network error, or unexpected condition results in exit 0 with no
output, so a session is NEVER blocked) but fail-CLOSED for ADVERTISING (a
version whose tag cannot be confirmed is never advertised). Every
advertise/hold/no-op decision is appended to
.telegram_bot/state/version-check.log so "no update found" is
distinguishable from "the hook never ran" (DGN-1068).

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
# Raw VERSION file AT a tag ref (DGN-1068 tag-existence confirmation): a 200
# with content proves the tag exists upstream; 404 / network failure -> the
# candidate is unconfirmable and must NOT be advertised (fail-closed).
_REMOTE_TAG_VERSION_URL = (
    "https://raw.githubusercontent.com/coolcoolk/dogany-agent/{}/VERSION"
)
_LOG_MAX_BYTES = 65536  # crude rotation cap for the decision log
_REMOTE_TIMEOUT_S = 2
_CACHE_TTL_S = 6 * 3600  # 6 hours
_NOTES_MAX_LINES = 30  # cap the release-note fold body (Telegram 4096 limit)
_RENAG_TTL_S = 7 * 24 * 3600  # DGN-735: re-nag at most weekly per version
# DGN-986 [e]: boot snapshot written by bridge/boot_snapshot.py (schema=1
# locked in the DGN-986 spec). Path is INSTANCE_ROOT/.telegram_bot/
# runtime-snapshot.json -- read as plain stdlib JSON (no bridge/ import: this
# hook is a standalone stdlib-only script, same discipline as the rest of
# this file).
_SNAPSHOT_RELATIVE_PATH = os.path.join(".telegram_bot", "runtime-snapshot.json")
_SNAPSHOT_SCHEMA_KNOWN = 1
# _should_notice/_write_shown share ONE state file keyed by version string
# equality. Without a prefix, the "landed but not running" key (built_version)
# could collide with a later "new version exists" key (repo/remote version)
# if that later remote version ever equals a previously-notified built
# version string -- the two notices would then falsely suppress each other
# through the shared TTL file. The prefix keeps the two notice kinds in
# disjoint key namespaces with a one-line change (no new state file, no
# signature change to _should_notice/_read_shown/_write_shown).
_UNCONSUMED_KEY_PREFIX = "unconsumed:"
# DGN-1067: detail section cap mirrors the completion notice (dec-107 ~2500
# chars + continuation hint; self-update.sh maybe_restart truncation).
_DETAIL_MAX_CHARS = 2500
_DETAIL_TRUNC_HINT = "…(이하 생략 — 더 궁금하시면 물어봐 주세요)"


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


def _latest_stable_tag(repo_root):
    """DGN-1068: highest STABLE tag (vX.Y.Z, no pre-release suffix) of the
    local source repo, or '' on any failure (no git binary, not a git
    checkout, timeout).

    Mirrors self-update.sh resolve_channel_tag() channel=release exactly:
    version-descending sort with versionsort.suffix=- ; first tag without a
    hyphen wins (hyphen = pre-release, e.g. v1.42.0-dev.1). '' means the
    local check cannot advertise anything -- fail-closed for advertising;
    the remote check below still covers real releases."""
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", repo_root, "-c", "versionsort.suffix=-",
             "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return ""
        for tag in proc.stdout.split("\n"):
            tag = tag.strip()
            if tag and "-" not in tag:
                return tag
    except Exception:
        pass
    return ""


def _confirm_remote_tag(version):
    """DGN-1068: True only when tag v<version> is confirmed to exist on the
    public repo. GETs the raw VERSION file AT the tag ref -- any non-empty
    200 body proves the ref exists. 404 or network failure -> False, and the
    caller must HOLD the notice (fail-closed for advertising)."""
    vtag = "v" + version.lstrip("v")
    return bool(_fetch_remote_version(
        _REMOTE_TAG_VERSION_URL.format(vtag), _REMOTE_TIMEOUT_S))


def _log_decision(instance_root, text):
    """DGN-1068: append one audit line per advertise/hold/no-op decision to
    .telegram_bot/state/version-check.log, so a HELD advertisement and a
    zero-result run are both observable (silence is indistinguishable from
    "never ran" otherwise). Crude rotation: file over _LOG_MAX_BYTES is
    truncated before append. Fail silent -- logging must never block."""
    try:
        path = os.path.join(
            instance_root, ".telegram_bot", "state", "version-check.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a"
        try:
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        with open(path, mode, encoding="utf-8") as fh:
            fh.write("{} {}\n".format(stamp, text))
    except Exception:
        pass


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
    Applied to CHANGELOG fallback output only; user-summary sections go
    through _strip_tickets (the completion-notice pipeline mirror) instead.
    """
    import re
    # Remove parenthesized ticket refs first (may leave stray whitespace).
    text = re.sub(r'\(\s*DGN-\d+\s*\)', '', text)
    # Remove bare ticket refs (preceded by whitespace or start of content).
    text = re.sub(r'\s*DGN-\d+', '', text)
    return text


def _strip_tickets(text):
    """DGN-1067: line-by-line mirror of self-update.sh strip_tickets()
    (DGN-785 safety net). Applied to every user-summary section body so the
    availability notice renders the SAME text as the completion notice for
    the same release file. Sed pipeline mirrored: parenthesized DGN refs,
    bare DGN refs, space-run collapse, trailing ' --' strip, leading
    whitespace strip."""
    import re
    out = []
    for line in text.split("\n"):
        line = re.sub(r'\([ \t]*DGN-[0-9]+[^)]*\)', '', line)
        line = re.sub(r'[ \t]*DGN-[0-9]+', '', line)
        line = re.sub(r'  +', ' ', line)
        line = re.sub(r'[ \t]*--[ \t]*$', '', line)
        line = re.sub(r'^[ \t]*', '', line)
        out.append(line)
    return "\n".join(out)


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


def _user_summary_from_note(note_text, lang, max_lines=_NOTES_MAX_LINES):
    """DGN-738: extract the localized user-summary for <lang> from a release-
    note file's text. Returns '' if absent (fail-open; caller falls back to
    CHANGELOG scrape).

    max_lines (DGN-1067): the shell user_summary() has NO line cap; the
    _NOTES_MAX_LINES cap is this side's extra guard for the summary section.
    The detail section passes max_lines=None so its only cap is the
    completion-notice char cap (_DETAIL_MAX_CHARS) -- otherwise a long
    bullet list under 2500 chars would silently lose lines here while the
    completion notice shows all of them.

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
        if max_lines is not None:
            out = out[:max_lines]
        return "\n".join(out)
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


# ---------------------------------------------------------------------------
# DGN-986 [e]: boot snapshot read (3rd comparison -- "received but not used")
# ---------------------------------------------------------------------------

def _pid_alive(pid):
    """Liveness-only check for a pid (NOT an identity check -- a dead pid can
    theoretically be reused by an unrelated process within the same short
    window; accepted residual risk, see report). Returns True/False/None:
      - False: the pid is confirmed dead (ProcessLookupError) -- the process
        that wrote the snapshot has already exited (crash-restart race: the
        old snapshot is still on disk but no longer describes "the currently
        running process").
      - True: a process with this pid exists (kill(pid, 0) succeeded, or
        PermissionError -- exists but owned by someone else, still 'alive').
      - None: could not determine (bad pid type/value, or any other OSError)
        -- caller must treat this the same as False (fail toward silence,
        never toward a possibly-wrong user-facing claim)."""
    try:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None
    return True


def _read_snapshot_fw_version(instance_root):
    """Return the boot snapshot's fw_version string, or None when the
    comparison must be silently skipped: no snapshot file (pre-2.0 instance),
    unreadable/malformed JSON, unknown schema (forward-compat -- a future
    schema bump must not make an old hook misread a reshaped field), or a
    stale snapshot (pid no longer alive/unconfirmed -- see _pid_alive). Never
    raises; every failure path is a plain `return None`."""
    try:
        path = os.path.join(instance_root, _SNAPSHOT_RELATIVE_PATH)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    try:
        if not isinstance(data, dict):
            return None
        if data.get("schema") != _SNAPSHOT_SCHEMA_KNOWN:
            return None
        if _pid_alive(data.get("pid")) is not True:
            return None
        fw = data.get("fw_version")
        if isinstance(fw, str) and fw.strip():
            return fw.strip()
        return None
    except Exception:
        return None


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


def _section_glyphs():
    """DGN-1067: DOGANY_SECTION_GLYPHS resolution, mirroring self-update.sh
    (DGN-829 parse): space-separated positional glyphs read from the process
    env -- 1st=summary, 2nd=detail (same positions as the completion notice;
    its 3rd=try slot is unused here). Empty/unset or a missing token falls
    back to the per-position default ▶ (owner pick 2026-08-24 -- see the
    self-update.sh glyph comment for the rationale)."""
    toks = os.environ.get("DOGANY_SECTION_GLYPHS", "").split()
    sg1 = toks[0] if len(toks) > 0 else ""
    sg2 = toks[1] if len(toks) > 1 else ""
    return (sg1 or "▶", sg2 or "▶")


def _build_user_notice(remote_version, notes, detail, lang):
    """DGN-687 notice #1 (update available). Korean copy is LOCKED
    (owner-approved 2026-08-01). en mirrors the structure.

    DGN-1067: restores the DGN-687 locked fold semantics -- "folded = 1-line
    summary, expanded = detailed release notes" -- which dec-107's
    completion-only detail scope had shadowed on this surface. The fold now
    carries the SAME labelled section skeleton as the completion notice
    (self-update.sh maybe_restart): <b>-wrapped labels, glyph default ▶,
    DOGANY_SECTION_GLYPHS override, blank line between sections, empty
    section dropped label-and-all. Two sections only (summary + detail);
    the _try section (DGN-785) is deliberately NOT shown here -- at
    availability time the user has not updated yet, so "try this" would
    invite actions the instance cannot perform. Detail body is hard-capped
    at _DETAIL_MAX_CHARS with the completion notice's continuation hint
    (dec-107 cap, mirrored).

    Layout (owner-confirmed 2026-08-12, DGN-846): the header and action are
    PLAIN lines OUTSIDE the fold; ONLY the labelled sections sit inside one
    <blockquote expandable> so the notes collapse while the call-to-action
    stays plainly visible above the fold:

        도가니 업데이트 있어요 · vX        (plain)
        지금 업데이트하실래요?              (plain)
        <blockquote expandable><b>▶ 업데이트 요약</b>
        <notes...>

        <b>▶ 업데이트 상세</b>
        <detail...></blockquote>

    With no sections at all the fold is dropped entirely -- header + action,
    2 plain lines, no blockquote (safe silence, DGN-784).

    dec-094 UX gate: the ko/en section label copy below mirrors the
    completion notice verbatim, but is PENDING owner confirmation for THIS
    surface (draft; the previous label here was "▸ 업데이트 노트").

    The relay send path renders Telegram HTML (bridge sends prose at
    parse_mode=HTML; whitelists <blockquote expandable> passthrough). The
    plain header/action lines carry no tags and pass through unchanged. The
    DGN-846 finalize gate (contains_telegram_html -> HTML edit) still fires
    because the notes fold carries the blockquote tag. The DGN-788 raw-tag
    leak (streamed finalize skipping its HTML edit on a no-op conversion) is
    fixed bridge-side in DGN-846; dec-117's plaintext detour is reverted.

    DGN-851: the header carries the PRODUCT name, which is config data --
    resolved from config/i18n key 'update.available' ({version} slot); the
    in-code copy below is the zero-delta fallback (locked ko copy) for
    instances whose locale files lack the key. The override stays a plain
    line, so it composes with the layout above unchanged."""
    ver = "v" + remote_version.lstrip("v")
    sg1, sg2 = _section_glyphs()
    if lang == "ko":
        header = "도가니 업데이트 있어요 · {}".format(ver)
        action = "지금 업데이트하실래요?"
        label_summary = "<b>{} 업데이트 요약</b>".format(sg1)
        label_detail = "<b>{} 업데이트 상세</b>".format(sg2)
    else:
        header = "Dogany update available · {}".format(ver)
        action = "Update now?"
        label_summary = "<b>{} At a glance</b>".format(sg1)
        label_detail = "<b>{} What changed</b>".format(sg2)
    header_tpl = _i18n_get(lang, "update.available")
    if header_tpl:
        header = header_tpl.replace("{version}", ver)
    sections = []
    if notes:
        sections.append("{}\n{}".format(label_summary, notes))
    if detail:
        detail_body = detail
        if len(detail_body) > _DETAIL_MAX_CHARS:
            # len()/slice count characters (str), matching the completion
            # notice's `cut -c` character-count truncation under UTF-8.
            detail_body = (detail_body[:_DETAIL_MAX_CHARS]
                           + "\n" + _DETAIL_TRUNC_HINT)
        sections.append("{}\n{}".format(label_detail, detail_body))
    if sections:
        return "{}\n{}\n<blockquote expandable>{}</blockquote>".format(
            header, action, "\n\n".join(sections))
    return "{}\n{}".format(header, action)


def _emit_note(built_version, remote_version, source_label, notes, detail, lang):
    message = _build_user_notice(remote_version, notes, detail, lang)
    note = (
        "[Dogany framework update available] This instance was built from "
        "framework version {built}, but {source} now has version {remote}. "
        "Relay the pre-formatted notice between the NOTICE markers below to "
        "the user VERBATIM as your reply -- do not translate, rewrite, or "
        "wrap it in a code block (the <blockquote> tags are Telegram HTML "
        "rendered by the bridge; this notice is injected at most once per "
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


def _build_pending_restart_notice(built_version, lang):
    """DGN-986 [e] notice #3 (landed, not yet consumed).

    DRAFT COPY -- NOT YET OWNER-CONFIRMED (dec-094 UX-facing gate). Same
    "header + action, 2 plain lines, no fold" shape as _build_user_notice's
    no-notes branch: there is nothing to fold here (same target version, no
    release notes to show -- the only fact is "not restarted yet")."""
    ver = "v" + built_version.lstrip("v")
    if lang == "ko":
        header = "업데이트는 받았는데 아직 안 쓰고 있어요 · {}".format(ver)
        action = "재기동하면 반영돼요. 지금 재기동할까요?"
    else:
        header = "Update received but not running yet · {}".format(ver)
        action = "A restart will apply it. Restart now?"
    header_tpl = _i18n_get(lang, "update.pending_restart")
    if header_tpl:
        header = header_tpl.replace("{version}", ver)
    action_tpl = _i18n_get(lang, "update.pending_restart_action")
    if action_tpl:
        action = action_tpl
    return "{}\n{}".format(header, action)


def _emit_pending_restart_note(built_version, lang):
    message = _build_pending_restart_notice(built_version, lang)
    note = (
        "[Dogany framework update landed but not yet running] This "
        "instance's config (.instance.conf DOGANY_FW_VERSION) already shows "
        "{built}, but the boot snapshot of the currently running bridge "
        "process shows an older version -- the process was never restarted "
        "to pick up the update it already has (DGN-986 [e]). Relay the "
        "pre-formatted notice between the NOTICE markers below to the user "
        "VERBATIM as your reply -- do not translate, rewrite, or wrap it in "
        "a code block. Do NOT restart automatically -- wait for the user's "
        "approval, then run bridge/self_restart.sh --trigger user --reason "
        "\"DGN-986 pending-update restart\" as the LAST tool call of that "
        "turn (relative to the instance root; DGN-715 owner-conversational, "
        "no forced landing).\n"
        "---NOTICE---\n{message}\n---END NOTICE---"
    ).format(built=built_version, message=message)
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
    # DGN-1068: candidate = latest STABLE tag, NOT the VERSION file. VERSION
    # means "the next number release.sh will cut" (bumped BEFORE the tag is
    # minted); a tag means "a version an update can deliver". Advertising
    # from VERSION opened the ghost window (v1.43.0 incident: untagged,
    # unpushed version advertised to the owner). The tag lookup is local
    # (git tag), so this path stays fully offline-capable.
    local_tag_ver = ""
    remote_version = ""
    if repo_root:
        local_tag_ver = _latest_stable_tag(repo_root).lstrip("v")
        # Observability: VERSION ahead of the latest stable tag = a ghost
        # window is open right now. Log it (never advertise it).
        repo_version = _read_version(os.path.join(repo_root, "VERSION"))
        if (repo_version and repo_version != "unknown"
                and (not local_tag_ver
                     or _is_newer(repo_version, local_tag_ver))):
            _log_decision(instance_root,
                          "hold(local): VERSION {} has no stable tag yet "
                          "(latest stable tag: {}) -> not advertised "
                          "(ghost window, DGN-1068)".format(
                              repo_version,
                              "v" + local_tag_ver if local_tag_ver
                              else "none"))
        if local_tag_ver and _is_newer(local_tag_ver, built_version):
            # DGN-735: suppress if same version shown within weekly TTL.
            if not _should_notice(instance_root, local_tag_ver, now):
                _log_decision(instance_root,
                              "suppress(local): v{} already shown within "
                              "TTL".format(local_tag_ver))
                sys.exit(0)
            # DGN-738: prefer localized user-summary from releases/vX.Y.Z.md.
            # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/
            # developer-facing (DGN-210) and contains raw markdown, backticks,
            # and internal names that must never reach user-facing output.
            # When the user-summary block is absent, show no fold body.
            # DGN-1067: same extraction pipeline as the completion notice
            # (user_summary per section key + strip_tickets safety net);
            # detail key = <lang>_detail, uncapped lines (char cap at render).
            # DGN-1068: the advertised version is the TAG, not the VERSION file
            # -- releases/ doc is looked up by the tag too, so a prepared-but-
            # untagged version can never drive a notice.
            vdisp = "v" + local_tag_ver
            notes_path = os.path.join(repo_root, "releases", vdisp + ".md")
            notes = ""
            detail = ""
            try:
                with open(notes_path, "r", encoding="utf-8") as _fh:
                    _note_text = _fh.read()
                notes = _strip_tickets(_user_summary_from_note(_note_text, lang))
                detail = _strip_tickets(_user_summary_from_note(
                    _note_text, lang + "_detail", max_lines=None))
            except Exception:
                notes = ""
                detail = ""
            _emit_note(built_version, local_tag_ver,
                       "the local source repo at " + repo_root,
                       notes, detail, lang)
            _write_shown(instance_root, local_tag_ver, now)
            _log_decision(instance_root,
                          "advertise(local): v{} (built {})".format(
                              local_tag_ver, built_version))
            sys.exit(0)

    # --- 2) Remote check (default ON; opt-out: DOGANY_VERSION_CHECK=0 in instance .env) ---
    # PRIVACY: sends ONLY a GET to raw.githubusercontent.com/coolcoolk/dogany-agent/main/VERSION.
    # No user data, no token, no instance metadata. Default ON.
    #
    # DGN-986 [e]: this used to be an early `sys.exit(0)` when disabled --
    # changed to a wrapping `if` so the opt-out only skips the NETWORK check.
    # Check 3 below is a purely local file comparison and must still run even
    # when the user has opted out of the remote GitHub fetch (the two toggles
    # are unrelated: one is a privacy/network preference, the other detects
    # an update that already landed on THIS disk).
    if _remote_check_enabled(instance_root):
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
                _log_decision(instance_root,
                              "suppress(remote): v{} already shown within "
                              "TTL".format(remote_version.lstrip("v")))
                sys.exit(0)
            # DGN-1068: confirm the tag actually exists upstream before
            # advertising. The public mirror main branch could carry a bumped
            # VERSION ahead of its tags (same ghost shape as the local window).
            # Unconfirmable (404 or network failure) -> HOLD, never advertise
            # (fail-closed): an unreachable network also means the update
            # itself could not be downloaded, so staying quiet is correct.
            # Next session start retries.
            if not _confirm_remote_tag(remote_version):
                _log_decision(instance_root,
                              "hold(remote): VERSION {} newer than built {} "
                              "but tag v{} not confirmed upstream (absent or "
                              "network failure) -> not advertised "
                              "(fail-closed, DGN-1068)".format(
                                  remote_version, built_version,
                                  remote_version.lstrip("v")))
                sys.exit(0)
            # DGN-738: prefer localized user-summary from the public release note.
            # DGN-784: CHANGELOG fallback removed. The CHANGELOG is English/
            # developer-facing (DGN-210) and contains raw markdown, backticks,
            # and internal names that must never reach user-facing output.
            # When the user-summary block is absent or fetch fails, show no fold body.
            # DGN-1067: same extraction pipeline as the completion notice
            # (user_summary per section key + strip_tickets safety net).
            notes = ""
            detail = ""
            try:
                vdisp = "v" + remote_version.lstrip("v")
                note_url = _REMOTE_RELEASE_NOTE_URL.format(vdisp)
                note_text = _fetch_remote_text(note_url, _REMOTE_TIMEOUT_S)
                if note_text:
                    notes = _strip_tickets(_user_summary_from_note(note_text, lang))
                    detail = _strip_tickets(_user_summary_from_note(
                        note_text, lang + "_detail", max_lines=None))
            except Exception:
                notes = ""
                detail = ""
            _emit_note(built_version, remote_version,
                       "the upstream dogany-agent public repo",
                       notes, detail, lang)
            _write_shown(instance_root, remote_version, now)
            _log_decision(instance_root,
                          "advertise(remote): v{} (built {}, tag "
                          "confirmed)".format(
                              remote_version.lstrip("v"), built_version))
            sys.exit(0)
    else:
        # DGN-986 [e]: no early exit here (unlike the pre-986 shape) -- fall
        # through to check 3, which is unaffected by the network opt-out.
        _log_decision(instance_root,
                      "no-op(remote): disabled (DOGANY_VERSION_CHECK=0)")

    # --- 3) Landed-but-not-running check (DGN-986 [e]) ---
    # Reached when neither check 1 nor check 2 already fired above (both
    # exit(0) right after emitting their notice), REGARDLESS of whether the
    # remote check is enabled (see the comment on check 2's `if` above).
    #
    # Placement rationale (checked LAST, not first): "not yet received" and
    # "received but not running" can BOTH be true at once -- an even newer
    # version can appear on the repo/remote side while this instance is still
    # sitting on an already-landed-but-unrestarted update. When both are
    # true, "a newer version exists" is the single more-actionable message:
    # following its CTA runs self-update.sh, which restarts the bridge
    # itself (DGN-685) and so ALSO clears this landed-but-not-running gap in
    # the same action. Showing this notice FIRST instead would only fix the
    # smaller problem (a restart) and leave the bigger one (the newer
    # version) to fire again on the very next session -- two notices for one
    # underlying "go update/restart" action. Checks 1/2 above already
    # returned (via sys.exit) if they had something to say; this block is
    # therefore skipped for free whenever they did.
    snapshot_fw = _read_snapshot_fw_version(instance_root)
    if snapshot_fw and _is_newer(built_version, snapshot_fw):
        key = _UNCONSUMED_KEY_PREFIX + built_version
        if _should_notice(instance_root, key, now):
            _emit_pending_restart_note(built_version, lang)
            _write_shown(instance_root, key, now)

    # DGN-1068: zero-result run leaves a trace too, so "no update found" is
    # distinguishable from "the hook never ran".
    _log_decision(instance_root,
                  "no-op: nothing newer (built {}, local-tag {}, remote "
                  "{})".format(built_version, local_tag_ver or "-",
                               remote_version or "-"))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
