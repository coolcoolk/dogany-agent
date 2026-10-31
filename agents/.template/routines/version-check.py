#!/usr/bin/env python3
"""version-check.py -- SessionStart hook: notify when a newer framework exists.

Check modes (in order; ALL are fail-open and never block a session).

0. Dev-channel check (DGN-1668, runs INSTEAD of checks 1+2 when
   .instance.conf DOGANY_UPDATE_CHANNEL=dev): resolves the newest dev-line
   tag from the LOCAL canonical clone at DOGANY_REPO_ROOT -- the same
   manifest field self-update.sh resolves its update source from, and the
   same repo it installs from. Offline, no credentials.

   Why it has to exist: dev tags are minted in the canonical repository and
   never reach the public mirror. Check 2 polls that mirror, so a dev
   subscriber's channel cannot exist at the URL it asks; check 1 filters
   pre-release tags out by design. Both then answer "" -- byte-identical to
   "up to date". A dev instance five tags behind advertised nothing and
   cached the mirror's stale release number as its "newest remote".

   A host with no readable clone does NOT fall silent: it reports that the
   dev channel cannot be checked here (_emit_dev_unavailable_note). Silence
   there would rebuild the same defect for external instances.

   Other channels are untouched -- release/staging/undeclared keep checks
   1+2 exactly as written below.
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

DGN-1417 round 3 (estate hosts): when DOGANY_REPO_ROOT carries
scripts/pack/update_surface.py, checks 0-2 stand down. The session-start
notice there covers framework + kits + packs in ONE message built by the
bridge from the same planner/ledger /update uses; this hook only leaves the
request (.telegram_bot/state/update-offer-request) and supersedes any pending
framework-only notice. Check 3 still runs.

DGN-1675: every check above is OWNER-SESSION only -- a SessionStart without
the bridge surface marker (DOGANY_BRIDGE_SESSION=1: headless cron, dispatch
workers, plain CLI) exits before any spool creation or injection, so one
version yields at most one notice per owner conversation session.

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
# DGN-1748: tags release.sh has minted but not yet proven on origin carry a
# ref here (set before its gate 6, lifted only after gate 8). Same literal as
# release.sh UNPUBLISHED_NS and self-update.sh resolve_channel_tag().
_UNPUBLISHED_NS = "refs/dogany/unpublished/"

# DGN-1668: dev-channel tag whitelist, character-for-character the regex
# self-update.sh resolve_channel_tag() applies for channel=dev. The two must
# stay lockstep: this hook advertises exactly what that script would install.
_DEV_TAG_PATTERN = r"^v[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]+)?$"
_LOG_MAX_BYTES = 65536  # crude rotation cap for the decision log
_REMOTE_TIMEOUT_S = 2
_CACHE_TTL_S = 6 * 3600  # 6 hours
_NOTES_MAX_LINES = 30  # cap the release-note fold body (Telegram 4096 limit)
# DGN-986 [e]: boot snapshot written by bridge/boot_snapshot.py (schema=1
# locked in the DGN-986 spec). Path is INSTANCE_ROOT/.telegram_bot/
# runtime-snapshot.json -- read as plain stdlib JSON (no bridge/ import: this
# hook is a standalone stdlib-only script, same discipline as the rest of
# this file).
_SNAPSHOT_RELATIVE_PATH = os.path.join(".telegram_bot", "runtime-snapshot.json")
_SNAPSHOT_SCHEMA_KNOWN = 1
# Legacy shown-flag key namespace prefix for the "landed but not running"
# notice (kept the two notice kinds disjoint in the ONE shared state file).
# DGN-1586: the shown flag is retired as a write target; this prefix is now
# the KIND DISCRIMINATOR for the one-shot legacy importer
# (_spool_migrate_legacy) -- `unconsumed:<ver>` maps to kind=pending-restart
# and version comparison sees only the stripped pure version (spec MINOR-2).
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


def _prerelease_key(v):
    """DGN-1668: semver pre-release precedence key for a version whose
    release core already compared EQUAL.

    Returns a tuple that sorts by SemVer 2.0.0 rule 11.3/11.4:
      - no pre-release suffix -> (1,): a release outranks any of its own
        pre-releases (v2.6.0 > v2.6.0-dev.38).
      - `-dev.38` -> (0, (1, "dev"), (0, 38)): identifiers compared left to
        right; numeric identifiers compare numerically and rank BELOW
        alphanumeric ones. Every element is a 2-tuple led by an int, so the
        comparison never puts a str next to an int.
    Build metadata (+...) is ignored, as semver requires."""
    core = v.strip().lstrip("v").split("+")[0]
    _, sep, pre = core.partition("-")
    if not sep or not pre:
        return (1,)
    ids = []
    for ident in pre.split("."):
        if ident.isdigit():
            ids.append((0, int(ident)))
        else:
            ids.append((1, ident))
    return tuple([0] + ids)


def _is_newer(candidate, current):
    """True only if `candidate` is a strictly newer version than `current`.

    Numeric dotted comparison (semver-style, zero-padded to equal length). The
    update nudge must fire ONLY when the other side is genuinely ahead -- a
    plain `!=` false-alarms when the local build is newer than a lagging public
    repo (the DGN-349 defect). Falls back to plain inequality only when either
    string cannot be parsed numerically, so exotic version schemes still nudge
    rather than silently regressing.

    DGN-1668: when the release cores tie, pre-release identifiers decide
    (_prerelease_key). Without that tie-break a dev-channel instance can
    never see its own line move -- _version_tuple DROPS the suffix, so
    2.6.0-dev.38 and 2.6.0-dev.33 compare equal and every dev tag after the
    consumed one is invisible. Suffix-free inputs are unaffected: both sides
    key to (1,), so the added comparison is a no-op for the stable path.
    """
    ct = _version_tuple(candidate)
    cur = _version_tuple(current)
    if ct is None or cur is None:
        return candidate != current
    n = max(len(ct), len(cur))
    ct = ct + (0,) * (n - len(ct))
    cur = cur + (0,) * (n - len(cur))
    if ct != cur:
        return ct > cur
    return _prerelease_key(candidate) > _prerelease_key(current)


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


def _repo_tags(repo_root):
    """Version-descending `v*` tag list of the local source repo, or None on
    any failure (no git binary, not a git checkout, timeout).

    Mirrors self-update.sh resolve_channel_tag()'s listing exactly, including
    the DGN-1748 unpublished-hold filter; versionsort.suffix=- so a promoted vX.Y.Z outranks vX.Y.Z-dev.N (git's
    default sorts -dev ABOVE its stable). None is distinct from []: the
    first means "could not read this repo at all", the second means "read it,
    it has no v* tags" -- DGN-1668 needs that distinction to tell the user
    WHY the dev channel cannot be checked."""
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", repo_root, "-c", "versionsort.suffix=-",
             "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            return None
        tags = [t.strip() for t in proc.stdout.split("\n") if t.strip()]
        # DGN-1748: same hold resolve_channel_tag() honours -- a tag
        # release.sh minted but has not yet proven on origin is never offered.
        hold = subprocess.run(
            ["git", "-C", repo_root, "for-each-ref",
             "--format=%(refname:lstrip=3)", _UNPUBLISHED_NS],
            capture_output=True, text=True, timeout=5)
        if hold.returncode != 0:
            return None
        held = set(h.strip() for h in hold.stdout.split("\n") if h.strip())
        return [t for t in tags if t not in held]
    except Exception:
        return None


def _latest_stable_tag(repo_root):
    """DGN-1068: highest STABLE tag (vX.Y.Z, no pre-release suffix) of the
    local source repo, or '' on any failure (no git binary, not a git
    checkout, timeout).

    Mirrors self-update.sh resolve_channel_tag() channel=release exactly:
    version-descending sort with versionsort.suffix=- ; first tag without a
    hyphen wins (hyphen = pre-release, e.g. v1.42.0-dev.1). '' means the
    local check cannot advertise anything -- fail-closed for advertising;
    the remote check below still covers real releases."""
    for tag in _repo_tags(repo_root) or []:
        if "-" not in tag:
            return tag
    return ""


def _latest_dev_tag(repo_root):
    """DGN-1668: highest tag on the DEV line of the local source repo.

    Mirrors self-update.sh resolve_channel_tag() channel=dev exactly -- the
    same whitelist regex `^v[0-9]+.[0-9]+.[0-9]+(-dev.[0-9]+)?$` over the
    same version-descending listing. Stable tags are IN the whitelist on
    purpose (a dev subscriber advances to vX.Y.Z the moment it is cut and
    never regresses to an -rc/-dev below it), which is why the dev channel
    needs no separate stable lookup.

    Returns (tag, reason):
      - ("v2.6.0-dev.38", "")  -- resolved
      - ("", "<reason>")       -- the dev channel CANNOT be checked here, and
        the reason is user-reportable. Never returns ("", "") -- silence is
        the defect this ticket exists to remove."""
    if not repo_root:
        return "", "no DOGANY_REPO_ROOT declared in .instance.conf"
    if not os.path.isdir(repo_root):
        return "", "framework repo not found on this host: " + repo_root
    tags = _repo_tags(repo_root)
    if tags is None:
        return "", "not a readable git checkout: " + repo_root
    import re
    dev_tag_re = re.compile(_DEV_TAG_PATTERN)
    for tag in tags:
        if dev_tag_re.match(tag):
            return tag, ""
    return "", "no dev-line tag (vX.Y.Z[-dev.N]) in " + repo_root


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


def _resolve_update_channel(conf):
    """DGN-1668: this instance's update channel -- env wins, .instance.conf
    is the fallback. That precedence is self-update.sh's, verbatim
    (UPDATE_CHANNEL="${DOGANY_UPDATE_CHANNEL:-$(sed ... "$CONF")}"): the hook
    must advertise from the SAME channel the update would actually install
    from, or it polls a place its own updates never land.

    '' means undeclared. self-update.sh resolves that as 'release' while
    warning loudly (DGN-1569); this hook keeps the pre-DGN-1668 path for it,
    which IS the release path -- so the effective behaviour matches without
    duplicating the warning on a surface that cannot act on it."""
    val = os.environ.get("DOGANY_UPDATE_CHANNEL", "")
    if not val:
        val = conf.get("DOGANY_UPDATE_CHANNEL", "")
    return val.strip()


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


# DGN-735 legacy: durable per-version shown flag. DGN-1586 RETIRES the
# write side ("shown at emission" was the defect: the flag recorded context
# injection, not owner delivery). The file is now read ONLY by the one-shot
# legacy importer below (_spool_migrate_legacy); suppression is owned by the
# notice spool's creation predicate and the SessionStart session_id marker.

def _notice_shown_path(instance_root):
    """Return path to the durable version-notice-shown state file (legacy)."""
    return os.path.join(
        instance_root, ".telegram_bot", "state", "version-notice-shown")


def _read_shown(instance_root):
    """Return (version_str, ts_float) from the legacy shown-flag file.
    Reads one line 'VERSION TS'; on ANY error returns ('', 0.0). Fail-open.
    DGN-1586: importer-only reader (see _spool_migrate_legacy)."""
    try:
        path = _notice_shown_path(instance_root)
        with open(path, "r", encoding="utf-8") as fh:
            line = fh.readline().strip()
        version_str, _, ts_str = line.partition(" ")
        return version_str.strip(), float(ts_str.strip())
    except Exception:
        return "", 0.0


# ---------------------------------------------------------------------------
# DGN-1586: notice spool -- producer side.
#
# CONTRACT: the spool file/lock/record schema is SHARED with the bridge
# consumer (bridge/notice_spool.py). This hook stays standalone stdlib-only
# (same discipline as the snapshot reader above), so the contract is the
# FILE + LOCK protocol, not shared code -- the footer-sidecar precedent.
# Any schema change here must land in bridge/notice_spool.py in the same
# change, and vice versa.
#
# - Records live in .telegram_bot/state/notice-spool.jsonl (one JSON object
#   per line); every read-modify-write runs under an exclusive flock on the
#   SEPARATE fixed lock file notice-spool.lock and rewrites the jsonl
#   atomically (tmp + os.replace). Unparseable lines are preserved verbatim
#   (operator-owned repair) and logged as diagnostics.
# - status: pending -> delivered (real carrier receipt, bridge-owned) or
#   expired (reason: resolved / superseded / attempts_exhausted).
# - Creation predicate (spec 3.4), evaluated inside the lock against the
#   freshest state, key = (kind, normalized version):
#     1. same-key pending exists            -> keep it (no new record)
#     2. same-key delivered in this session -> hold
#     3. same-key attempts_exhausted,       -> hold (manual re-arm owns
#        not re-armed                          revival)
#     4. same-key resolved/superseded       -> hold (terminal; no auto
#                                              revival for the same target)
#     5. strictly newer same-kind pending   -> hold (a lower cached
#                                              candidate never comes back)
#     6. otherwise                          -> create a new-id pending;
#        creating a new fw-update pending and closing lower-version
#        fw-update pendings as expired(superseded) is ONE change under the
#        same lock (same for a pending-restart whose built moved).
# ---------------------------------------------------------------------------

_SPOOL_RELATIVE_PATH = os.path.join(
    ".telegram_bot", "state", "notice-spool.jsonl")
_SPOOL_LOCK_RELATIVE_PATH = os.path.join(
    ".telegram_bot", "state", "notice-spool.lock")
_NOTICE_KIND_FW_UPDATE = "fw-update"
_NOTICE_KIND_PENDING_RESTART = "pending-restart"
# Management record id: marks the one-shot legacy import as done so the
# importer is idempotent across sessions (spec 4 Q4).
_NOTICE_MIGRATION_ID = "mgmt:legacy-import"


def _norm_ver(version):
    return (version or "").strip().lstrip("v")


def _installed_build(built_version, fw_tag):
    """DGN-1723: the FULL installed framework build for owner-facing copy.
    DOGANY_FW_VERSION is always the bare X.Y.Z; the exact (pre-release)
    build is DOGANY_FW_TAG, valid only while its X.Y.Z still equals it.
    Also read by scripts/pack/update_surface.py (DGN-1790 /update versions)."""
    ft = _norm_ver(fw_tag)
    if ft and ft.split("-", 1)[0].split("+", 1)[0] == \
            (built_version or "").lstrip("v").split("-", 1)[0].split("+", 1)[0]:
        return ft
    return built_version


def _spool_path(instance_root):
    return os.path.join(instance_root, _SPOOL_RELATIVE_PATH)


def _spool_acquire_lock(instance_root):
    """Open + exclusively flock the fixed spool lock file; returns the open
    handle (caller releases via _spool_release_lock). The lock file is
    separate from the jsonl because the jsonl inode is replaced while the
    lock is held."""
    import fcntl
    path = os.path.join(instance_root, _SPOOL_LOCK_RELATIVE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _spool_release_lock(fh):
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _spool_load(instance_root):
    """Return (records, bad_lines). Missing file -> ([], []). Unparseable
    lines are carried in bad_lines so a rewrite never destroys them."""
    records = []
    bad = []
    try:
        with open(_spool_path(instance_root), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        records.append(rec)
                    else:
                        bad.append(line)
                except Exception:
                    bad.append(line)
    except FileNotFoundError:
        return [], []
    except Exception:
        return [], []
    return records, bad


def _spool_dump(instance_root, records, bad):
    """Atomic rewrite (tmp + os.replace); bad raw lines ride at the tail."""
    path = _spool_path(instance_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for line in bad:
            fh.write(line + "\n")
    os.replace(tmp, path)


def _spool_new_id(now):
    return "ntc-{}-{}".format(int(now * 1000), os.urandom(4).hex())


def _spool_migrate_legacy(instance_root, records, now):
    """One-shot importer of the legacy shown flag (spec 4 Q4). The legacy
    `unconsumed:` prefix is the kind discriminator (pending-restart);
    version comparison sees only the stripped pure version. The compat
    record keeps the legacy timestamp, origin=legacy-assumed and
    message_id=null: it is a suppression-policy compat value, never real
    delivery evidence. Malformed entries stay diagnostics -- they never
    fabricate a receipt. Returns True when records changed."""
    for rec in records:
        if rec.get("id") == _NOTICE_MIGRATION_ID:
            return False
    version_str, ts = _read_shown(instance_root)
    if version_str:
        if version_str.startswith(_UNCONSUMED_KEY_PREFIX):
            kind = _NOTICE_KIND_PENDING_RESTART
            ver = version_str[len(_UNCONSUMED_KEY_PREFIX):]
        else:
            kind = _NOTICE_KIND_FW_UPDATE
            ver = version_str
        ver = _norm_ver(ver)
        if ver and ts > 0:
            records.append({
                "id": _spool_new_id(now),
                "kind": kind,
                "version": ver,
                "audience": "owner",
                "first_pending_ts": ts,
                "attempts": 0,
                "status": "delivered",
                "expired_reason": None,
                "delivered_ts": ts,
                "chat_id": None,
                "message_id": None,
                "body_oneline": "",
                "body_fold": "",
                "context_injected_ts": ts,
                "origin": "legacy-assumed",
            })
        else:
            _log_decision(
                instance_root,
                "notice-spool import: malformed legacy shown entry "
                "{!r} -> diagnostic only, no record".format(version_str))
    records.append({
        "id": _NOTICE_MIGRATION_ID,
        "kind": "management",
        "migrated_ts": now,
    })
    return True


def _spool_expire(rec, reason, now):
    rec["status"] = "expired"
    rec["expired_reason"] = reason
    rec["expired_ts"] = now


def _spool_reconcile(records, built_version, snapshot_fw, now,
                     fw_reference=None):
    """Expire pendings whose kind-specific RESOLUTION condition is met
    (spec 3.2): fw-update -> target no longer newer than built;
    pending-restart -> snapshot_fw == built (EXACTLY that predicate; a
    missing/stale snapshot is never resolution evidence). Returns True when
    records changed.

    DGN-1668: the two predicates need DIFFERENT reference points on a dev
    channel, so fw_reference splits them. .instance.conf carries the BASE
    version (DOGANY_FW_VERSION=2.6.0) and the consumed TAG
    (DOGANY_FW_TAG=v2.6.0-dev.33) separately; 2.6.0 is semver-GREATER than
    2.6.0-dev.38, so reconciling a dev-tag pending against the base version
    would expire it as 'resolved' on the very next session -- created once,
    killed before delivery. fw-update therefore reconciles against
    fw_reference (the consumed tag on dev), while pending-restart keeps
    built_version, which is what the boot snapshot actually records."""
    changed = False
    fw_ref = fw_reference or built_version
    for rec in records:
        if rec.get("status") != "pending":
            continue
        kind = rec.get("kind")
        ver = _norm_ver(rec.get("version"))
        if kind == _NOTICE_KIND_FW_UPDATE:
            if fw_ref and not _is_newer(ver, fw_ref):
                _spool_expire(rec, "resolved", now)
                changed = True
        elif kind == _NOTICE_KIND_PENDING_RESTART:
            if (snapshot_fw is not None and built_version
                    and snapshot_fw == built_version):
                _spool_expire(rec, "resolved", now)
                changed = True
    return changed


def _spool_key_state(records, kind, version, session_id):
    """Creation-predicate lookup for key (kind, normalized version).
    Returns (active_pending_or_None, blocked_reason_or_None); (None, None)
    means creation is allowed."""
    norm = _norm_ver(version)
    same = [r for r in records
            if r.get("kind") == kind and _norm_ver(r.get("version")) == norm]
    for r in same:
        if r.get("status") == "pending":
            return r, None
    for r in records:
        if (r.get("kind") == kind and r.get("status") == "pending"
                and _is_newer(_norm_ver(r.get("version")), norm)):
            return None, "newer-pending"
    # A delivered record is durable receipt evidence, not a wall-clock
    # suppression lease.  It blocks only the session that already received
    # this version's context; another session may create its own pending round.
    if session_id and any(
            r.get("status") == "delivered"
            and r.get("context_injected_session_id") == session_id
            for r in same):
        return None, "delivered-in-session"
    for r in same:
        if (r.get("status") == "expired"
                and r.get("expired_reason") == "attempts_exhausted"
                and not r.get("rearmed")):
            return None, "attempts-exhausted"
    for r in same:
        if (r.get("status") == "expired"
                and r.get("expired_reason") in ("resolved", "superseded")):
            return None, "closed-" + r.get("expired_reason")
    return None, None


def _spool_ensure_pending(instance_root, kind, version, body_oneline,
                          body_fold, now, built_version, session_id="",
                          fw_reference=None):
    """Run the full producer transaction under the lock: legacy import,
    reconcile, creation predicate, create + supersede as one change.
    Returns (active_pending_copy_or_None, blocked_reason_or_None).
    Fail-open: any error returns (None, 'error') and never raises."""
    try:
        fh = _spool_acquire_lock(instance_root)
    except Exception:
        return None, "error"
    try:
        records, bad = _spool_load(instance_root)
        changed = _spool_migrate_legacy(instance_root, records, now)
        snapshot_fw = _read_snapshot_fw_version(instance_root)
        changed = _spool_reconcile(
            records, built_version, snapshot_fw, now,
            fw_reference=fw_reference) or changed
        active, blocked = _spool_key_state(records, kind, version, session_id)
        if active is None and blocked is None:
            norm = _norm_ver(version)
            # Supersede: closing the lower-version pendings of the same
            # kind and creating the new pending is ONE change under this
            # lock (spec 3.4).
            for r in records:
                if (r.get("kind") == kind and r.get("status") == "pending"
                        and _is_newer(norm, _norm_ver(r.get("version")))):
                    _spool_expire(r, "superseded", now)
            active = {
                "id": _spool_new_id(now),
                "kind": kind,
                "version": norm,
                "audience": "owner",
                "first_pending_ts": now,
                "attempts": 0,
                "status": "pending",
                "expired_reason": None,
                "delivered_ts": None,
                "chat_id": None,
                "message_id": None,
                "body_oneline": body_oneline or "",
                "body_fold": body_fold or "",
                "context_injected_ts": None,
                "context_injected_session_id": None,
                "origin": "version-check",
            }
            records.append(active)
            changed = True
        if changed:
            _spool_dump(instance_root, records, bad)
        return (dict(active) if active is not None else None), blocked
    except Exception:
        return None, "error"
    finally:
        _spool_release_lock(fh)


def _spool_find_pending(instance_root, kind, version):
    """Read-only same-key pending lookup (used to skip the network
    tag-confirm + note fetch when a pending already exists). Fail-open."""
    try:
        if not os.path.isfile(_spool_path(instance_root)):
            return None
        fh = _spool_acquire_lock(instance_root)
    except Exception:
        return None
    try:
        records, _bad = _spool_load(instance_root)
        norm = _norm_ver(version)
        for r in records:
            if (r.get("kind") == kind and r.get("status") == "pending"
                    and _norm_ver(r.get("version")) == norm):
                return dict(r)
        return None
    except Exception:
        return None
    finally:
        _spool_release_lock(fh)


def _spool_mark_injected(instance_root, notice_id, now, session_id):
    """Persist the SessionStart payload session_id that received context.

    This is injection state only; it is intentionally independent from the
    bridge-owned pending -> delivered receipt transition.  The timestamp stays
    as diagnostic evidence and backwards-compatible record data, never as a
    suppression clock.  Fail-silent.
    """
    try:
        fh = _spool_acquire_lock(instance_root)
    except Exception:
        return
    try:
        records, bad = _spool_load(instance_root)
        for r in records:
            if r.get("id") == notice_id:
                r["context_injected_ts"] = now
                r["context_injected_session_id"] = session_id
                _spool_dump(instance_root, records, bad)
                return
    except Exception:
        pass
    finally:
        _spool_release_lock(fh)


def _spool_reconcile_pass(instance_root, built_version, now,
                          fw_reference=None):
    """Standalone reconcile so resolution expiry also lands on runs where
    no generation condition fires (e.g. the owner just updated). No-op when
    the spool does not exist yet -- a no-op run never creates it."""
    try:
        if not os.path.isfile(_spool_path(instance_root)):
            return
        fh = _spool_acquire_lock(instance_root)
    except Exception:
        return
    try:
        records, bad = _spool_load(instance_root)
        snapshot_fw = _read_snapshot_fw_version(instance_root)
        if _spool_reconcile(records, built_version, snapshot_fw, now,
                            fw_reference=fw_reference):
            _spool_dump(instance_root, records, bad)
    except Exception:
        pass
    finally:
        _spool_release_lock(fh)


def _is_bridge_surface():
    """DGN-1586 spec 3.3: surface declaration is owned by the session
    spawner. The bridge injects DOGANY_BRIDGE_SESSION=1 into its SDK child
    env; non-bridge launchers (CLI, detached) never carry it -- detached
    explicitly unsets it at its exec boundary. Shared env vars like
    TELEGRAM_BOT_TOKEN are NOT surface evidence.

    DGN-1675: this is also the OWNER-SESSION gate of the whole notice path
    (see main()): a session without the marker produces nothing."""
    return os.environ.get("DOGANY_BRIDGE_SESSION") == "1"


def _dispatch_injection(instance_root, active, now, session_id, emit, label,
                        log_ver):
    """Decide whether/how THIS session gets the notice context.

    The hook's SessionStart payload session_id is the canonical session
    identity: sdk_bridge.py creates the SDK session and preserves that id in
    StreamState.last_session_id.  One pending notice is injected once for that
    identifier, regardless of surface.  This does not touch delivery receipts.
    """
    body = (active.get("body_fold") or active.get("body_oneline") or "").strip()
    if not body:
        _log_decision(instance_root,
                      "hold({}): v{} pending has no stored body -> no "
                      "injection (diagnostic)".format(label, log_ver))
        return
    if not session_id:
        _log_decision(instance_root,
                      "hold({}): v{} missing SessionStart session_id -> no "
                      "injection".format(label, log_ver))
        return
    if active.get("context_injected_session_id") != session_id:
        emit(body)
        _spool_mark_injected(instance_root, active.get("id"), now, session_id)
        active["context_injected_ts"] = now
        active["context_injected_session_id"] = session_id
        _log_decision(instance_root,
                      "advertise({}): v{} context injected for session {} "
                      "(notice {})".format(label, log_ver, session_id,
                                             active.get("id")))
    else:
        _log_decision(instance_root,
                      "suppress({}): v{} context already injected for "
                      "session {}".format(label, log_ver, session_id))


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
    """Shared layered i18n lookup (routines/lib/i18n_lookup.py, the python
    twin of agentlib.sh i18n(), DGN-1789): per language kit override, else
    framework default; active lang first, then en; '' when the key is missing
    or anything fails (fail-open, DGN-851). Instance root = parent of this
    hook's dir."""
    here = os.path.dirname(os.path.abspath(__file__))
    instance_root = os.path.dirname(here)
    try:
        sys.path.insert(0, os.path.join(here, "lib"))
        import i18n_lookup
        return i18n_lookup.lookup(instance_root, lang, key)
    except Exception:
        return ""


def _section_glyphs():
    """DGN-1067: DOGANY_SECTION_GLYPHS resolution, mirroring self-update.sh
    (DGN-829 parse): space-separated positional glyphs read from the process
    env -- 1st=summary, 2nd=detail (same positions as the completion notice;
    its 3rd=try slot is unused here). Empty/unset or a missing token falls
    back to the per-position default ▸ (owner pick 2026-09-08, superseding the
    2026-08-24 ▶ pick -- see the self-update.sh glyph comment for the full
    rationale). U+25B8 is NOT in the Unicode emoji set, so it always renders as
    a text glyph; U+25B6 ▶ IS, and mobile clients drew it as a colour play-button
    emoji on a real user's install. Keep this default emoji-presentation-free."""
    toks = os.environ.get("DOGANY_SECTION_GLYPHS", "").split()
    sg1 = toks[0] if len(toks) > 0 else ""
    sg2 = toks[1] if len(toks) > 1 else ""
    return (sg1 or "▸", sg2 or "▸")


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

        Dogany 업데이트 있어요 · vX        (plain)
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
        header = "Dogany 업데이트 있어요 · {}".format(ver)
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


def _emit_note(built_version, remote_version, source_label, message, lang):
    """Inject the fw-update context. DGN-1586 spec 3.3: the instruction
    forks on the session surface. Bridge sessions get the machine-delivery
    framing (the bridge synthesizes the notice into the owner reply and
    owns delivery accounting; the model owns explanation and post-approval
    execution). Non-bridge sessions keep the preformatted relay role --
    machine delivery accounting is bridge-scope only. `message` is the
    stored spool body so injected and synthesized text always match."""
    if _is_bridge_surface():
        note = (
            "[Dogany framework update available] This instance was built "
            "from framework version {built}, but {source} now has version "
            "{remote}. DELIVERY IS MACHINE-OWNED (DGN-1586): the bridge "
            "appends the pre-formatted notice between the NOTICE markers "
            "below to the tail of your next final reply to the user and "
            "records delivery accounting itself. Do NOT relay, quote, or "
            "restate the notice yourself -- that would duplicate the "
            "machine-appended copy. You own: explaining the update when "
            "the user asks, and -- ONLY after the user's explicit approval "
            "-- running routines/self-update.sh as the LAST tool call of "
            "that turn (it wires the bridge restart itself, DGN-685; end "
            "the turn right after with the single install-complete line it "
            "prints). The update preserves memories, .env, databases, and "
            "user-authored skills.\n"
            "---NOTICE---\n{message}\n---END NOTICE---"
        ).format(built=built_version, remote=remote_version,
                 source=source_label, message=message)
    else:
        note = (
            "[Dogany framework update available] This instance was built "
            "from framework version {built}, but {source} now has version "
            "{remote}. Relay the pre-formatted notice between the NOTICE "
            "markers below to the user VERBATIM as your reply -- do not "
            "translate, rewrite, or "
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


def _build_dev_unavailable_notice(lang):
    """DGN-1668 notice #4 (the dev channel cannot be checked on this host).

    DRAFT COPY -- NOT YET OWNER-CONFIRMED (dec-094 UX-facing gate), same
    standing as _build_pending_restart_notice. Shape follows that notice
    exactly: header + action, 2 plain lines, no fold (there is nothing to
    fold -- the only fact is that the source could not be read).

    Why this notice has to exist at all: before this ticket a dev-channel
    instance printed NOTHING whether it was up to date or structurally
    unable to look. Those two states produced byte-identical output, so the
    owner could not tell 'no update' from 'no eyes'. Staying quiet here
    would reproduce the exact defect the rest of this change removes.

    The machine-readable reason is NOT in the user body -- it goes to the
    decision log and to the agent-facing wrapper in
    _emit_dev_unavailable_note, where a path is useful. The user body says
    what is true for them: this host cannot check."""
    if lang == "ko":
        header = "업데이트 확인 불가 · dev 채널"
        action = ("이 기기에서는 dev 채널을 확인할 수 없어요 "
                  "(프레임워크 저장소를 못 찾았습니다).")
    else:
        header = "Update check unavailable · dev channel"
        action = ("This host cannot check the dev channel -- the framework "
                  "repository could not be read.")
    header_tpl = _i18n_get(lang, "update.dev_unavailable")
    if header_tpl:
        header = header_tpl
    action_tpl = _i18n_get(lang, "update.dev_unavailable_action")
    if action_tpl:
        action = action_tpl
    return "{}\n{}".format(header, action)


def _emit_dev_unavailable_note(reason, message, lang):
    """Inject the dev-unavailable context.

    NO surface fork here, unlike _emit_note / _emit_pending_restart_note.
    Those two are spool-backed, and the bridge's machine delivery
    (DGN-1586) synthesizes from a spool record. This notice has no spool
    record -- it carries no version key and has no resolution predicate to
    reconcile against -- so telling a bridge session 'the machine will
    deliver it' would be a lie that ends in silence. Relay wording on both
    surfaces is the honest shape."""
    note = (
        "[Dogany dev channel cannot be checked on this host] This instance "
        "subscribes to the dev update channel (DOGANY_UPDATE_CHANNEL=dev), "
        "whose tags exist only in the canonical framework repository -- the "
        "public mirror carries none of them, so there is nowhere else to "
        "look. This host could not read that repository: {reason}. Until "
        "that is fixed the instance cannot tell a newer dev build from no "
        "dev build at all. Relay the pre-formatted notice between the "
        "NOTICE markers below to the user VERBATIM as your reply -- do not "
        "translate, rewrite, or wrap it in a code block. The fix is either "
        "a reachable clone at .instance.conf DOGANY_REPO_ROOT or a channel "
        "the host can actually follow (DOGANY_UPDATE_CHANNEL=release); do "
        "NOT change either one without the user's explicit approval.\n"
        "---NOTICE---\n{message}\n---END NOTICE---"
    ).format(reason=reason, message=message)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))


def _emit_pending_restart_note(built_version, message, lang):
    """Inject the pending-restart context. Same DGN-1586 surface fork as
    _emit_note -- BOTH emitters carry it (this kind has its own relay
    wording, so a single-emitter fork would leave this one behind)."""
    if _is_bridge_surface():
        note = (
            "[Dogany framework update landed but not yet running] This "
            "instance's config (.instance.conf DOGANY_FW_VERSION) already shows "
            "{built}, but the boot snapshot of the currently running bridge "
            "process shows an older version -- the process was never restarted "
            "to pick up the update it already has (DGN-986 [e]). DELIVERY IS "
            "MACHINE-OWNED (DGN-1586): the bridge appends the pre-formatted "
            "notice between the NOTICE markers below to the tail of your next "
            "final reply to the user and records delivery accounting itself. "
            "Do NOT relay, quote, or restate the notice yourself. You own: "
            "explaining it when the user asks, and -- ONLY after the user's "
            "explicit approval -- running bridge/self_restart.sh --trigger "
            "user --reason \"DGN-986 pending-update restart\" as the LAST "
            "tool call of that turn (relative to the instance root; DGN-715 "
            "owner-conversational, no forced landing).\n"
            "---NOTICE---\n{message}\n---END NOTICE---"
        ).format(built=built_version, message=message)
    else:
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


# ---------------------------------------------------------------------------
# DGN-1417 round 3 (owner rehearsal 2026-09-26 section D): on an estate host
# the session-start notice covers EVERY layer -- framework + kits + packs --
# in one message with its own apply buttons.  It is built by the bridge after
# the owner's reply, from the same planner + ledger /update uses
# (DOGANY_REPO_ROOT/scripts/pack/update_surface.py), and asked once per new
# version set.  This hook therefore does not measure the framework itself on
# such a host (no second measurement): it leaves a REQUEST for the bridge and
# retires any pending framework-only notice so the owner never gets both.
# Hosts without that machinery (the public tree) keep checks 0-2 unchanged.
# ---------------------------------------------------------------------------
_UPDATE_OFFER_REQUEST_RELATIVE_PATH = os.path.join(
    ".telegram_bot", "state", "update-offer-request")


def _estate_update_surface(repo_root):
    return bool(repo_root) and os.path.isfile(
        os.path.join(repo_root, "scripts", "pack", "update_surface.py"))


def _request_update_offer(instance_root, source, session_id, now):
    """Atomic request file; the bridge takes it (rename) after its next
    reply to the owner.  Fail-open like everything in this hook."""
    try:
        path = os.path.join(instance_root, _UPDATE_OFFER_REQUEST_RELATIVE_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.{}".format(os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"source": source, "session_id": session_id or "",
                       "ts": int(now)}, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _spool_supersede_fw_update(instance_root, now):
    """Expire pending framework-only notices (superseded by the all-layer
    notice) so the bridge never appends one to a reply.  Returns count."""
    try:
        if not os.path.isfile(_spool_path(instance_root)):
            return 0
        fh = _spool_acquire_lock(instance_root)
    except Exception:
        return 0
    try:
        records, bad = _spool_load(instance_root)
        count = 0
        for rec in records:
            if (rec.get("kind") == _NOTICE_KIND_FW_UPDATE
                    and rec.get("status") == "pending"):
                _spool_expire(rec, "superseded", now)
                count += 1
        if count:
            _spool_dump(instance_root, records, bad)
        return count
    except Exception:
        return 0
    finally:
        _spool_release_lock(fh)


def main():
    # SessionStart delivers the canonical session_id in its JSON payload.
    # It is the session identity used by the bridge and by this notice guard.
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

    # DGN-1668: which channel this instance actually subscribes to decides
    # WHERE "newer" can be observed. Checks 1/2 below only ever answer for
    # the release line (local STABLE tags, public-mirror VERSION); a dev
    # subscriber asking them gets the same empty answer whether it is up to
    # date or structurally blind. Channel `dev` therefore takes its own path
    # (check 0) and those two are skipped for it -- never weakened for
    # anyone else.
    channel = _resolve_update_channel(conf)
    dev_channel = (channel == "dev")
    # The dev line's reference point is the CONSUMED TAG, not the base
    # version: .instance.conf carries DOGANY_FW_VERSION=2.6.0 alongside
    # DOGANY_FW_TAG=v2.6.0-dev.33, and by semver 2.6.0 already outranks
    # every 2.6.0-dev.N, so comparing against the base hides the whole dev
    # line. No tag stamp -> fall back to the base version, which is
    # conservative (only a HIGHER base can then advertise) and logged below
    # rather than silently assumed.
    fw_tag = conf.get("DOGANY_FW_TAG", "").strip()
    fw_reference = _norm_ver(fw_tag) or built_version
    # DGN-1723: every owner-facing "installed version" reads the FULL build.
    built_display = _installed_build(built_version, fw_tag)

    # DGN-1586: resolution expiry must land even on runs where no check
    # fires below (e.g. the owner just updated/restarted) -- otherwise a
    # stale pending waits for the next owner turn to be expired by the
    # bridge consumer. No-op when the spool does not exist yet.
    _spool_reconcile_pass(instance_root, built_version, now,
                          fw_reference=fw_reference if dev_channel else None)

    # DGN-1675: the update notice is an OWNER-conversation artifact (owner
    # rule 2026-08-02: at most once per owner session before update). The
    # spool creation predicate is session-scoped by design (DGN-735: a new
    # session id may open a new round after a delivery), so every SessionStart
    # that reaches it counts as "a session" -- and headless `claude -p` cron
    # sessions and dispatch workers fire SessionStart too. Live evidence: three
    # notices for one version, keyed to the bridge session, a cron session and
    # a worker session, each delivered on the next owner turn. Only the bridge
    # spawner declares the owner surface (DOGANY_BRIDGE_SESSION=1, see
    # _is_bridge_surface); every other session neither creates, injects nor
    # marks a notice. The reconcile pass above is state hygiene and stays.
    if not _is_bridge_surface():
        _log_decision(instance_root,
                      "skip(non-owner): session {} is not a bridge owner "
                      "session -> no update notice (DGN-1675)".format(
                          session_id or "-"))
        sys.exit(0)

    # DGN-1417 round 3: estate host -> request the all-layer notice (built
    # and sent by the bridge after this session's reply); checks 0-2 are the
    # public tree's framework-only path and stand down here.  Check 3 (landed
    # but not running) is a different fact and still runs below.
    estate = _estate_update_surface(repo_root)
    if estate:
        superseded = _spool_supersede_fw_update(instance_root, now)
        requested = _request_update_offer(instance_root, "session-start",
                                          session_id, now)
        _log_decision(instance_root,
                      "estate: all-layer update notice {} for session {} "
                      "(bridge builds it from update_surface.py offer; "
                      "{} framework-only pending(s) superseded, "
                      "DGN-1417)".format(
                          "requested" if requested else "NOT requested "
                          "(write failed)", session_id or "-", superseded))

    # --- 0) Dev-channel check (DGN-1668; runs INSTEAD of checks 1+2) ---
    # Dev tags are minted in the canonical repository and never reach the
    # public mirror, so the remote check (check 2) polls a URL on which this
    # instance's channel cannot exist, and the local check (check 1) filters
    # pre-release tags out by design. Both answer "" -- indistinguishable
    # from "up to date". The local clone already on this machine has the
    # tags, needs no network and no credentials, and is the same source
    # self-update.sh installs from, so the check reads THAT.
    #
    # The clone path is NOT a new constant: .instance.conf DOGANY_REPO_ROOT
    # is the manifest field self-update.sh already resolves the update
    # source from, and the field this hook's check 1 already reads.
    if dev_channel and not estate:
        dev_tag, dev_reason = _latest_dev_tag(repo_root)
        if not dev_tag:
            # Condition 2 of this ticket: a host without a readable clone
            # must SAY so. Falling silent here would rebuild the defect.
            _log_decision(instance_root,
                          "unavailable(dev): {} -> the dev channel cannot "
                          "be checked on this host (DGN-1668)".format(
                              dev_reason))
            _emit_dev_unavailable_note(
                dev_reason, _build_dev_unavailable_notice(lang), lang)
            sys.exit(0)
        if not fw_tag:
            _log_decision(instance_root,
                          "degraded(dev): no DOGANY_FW_TAG stamp -> "
                          "comparing {} against base version {} (dev-line "
                          "moves within this base stay invisible until the "
                          "next update stamps a tag)".format(
                              dev_tag, built_version))
        dev_ver = dev_tag.lstrip("v")
        if _is_newer(dev_ver, fw_reference):
            # Release notes come from the same clone the tag came from
            # (check 1's lookup, by tag name). A dev tag usually ships none
            # -- absent file -> header + action only, no fold (DGN-784 safe
            # silence). No upstream tag-confirm (DGN-1068) is needed or
            # possible here: the tag was READ from the very repo the update
            # installs from, which is stronger evidence than a 200.
            notes_path = os.path.join(repo_root, "releases", dev_tag + ".md")
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
            body_fold = _build_user_notice(dev_ver, notes, detail, lang)
            body_oneline = _build_user_notice(dev_ver, "", "", lang)
            active, blocked = _spool_ensure_pending(
                instance_root, _NOTICE_KIND_FW_UPDATE, dev_ver,
                body_oneline, body_fold, now, built_version, session_id,
                fw_reference=fw_reference)
            if active is None:
                _log_decision(instance_root,
                              "hold(dev): {} notice spool blocked "
                              "({})".format(dev_tag, blocked or "error"))
                sys.exit(0)
            _dispatch_injection(
                instance_root, active, now, session_id,
                lambda body: _emit_note(
                    fw_reference, dev_ver,
                    "the local canonical clone at " + repo_root
                    + " (dev channel)", body, lang),
                "dev", dev_ver)
            sys.exit(0)
        _log_decision(instance_root,
                      "no-op(dev): nothing newer on the dev line (consumed "
                      "{}, newest {} in {})".format(
                          fw_tag or built_version, dev_tag, repo_root))

    # --- 1) Local check ---
    # DGN-1068: candidate = latest STABLE tag, NOT the VERSION file. VERSION
    # means "the next number release.sh will cut" (bumped BEFORE the tag is
    # minted); a tag means "a version an update can deliver". Advertising
    # from VERSION opened the ghost window (v1.43.0 incident: untagged,
    # unpushed version advertised to the owner). The tag lookup is local
    # (git tag), so this path stays fully offline-capable.
    local_tag_ver = ""
    remote_version = ""
    if repo_root and not dev_channel and not estate:
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
            # DGN-1586: creation is spool-owned (idempotent per key,
            # session-scoped delivered suppression, supersede as one locked
            # change);
            # injection is a per-surface decision downstream of it.
            body_fold = _build_user_notice(local_tag_ver, notes, detail, lang)
            body_oneline = _build_user_notice(local_tag_ver, "", "", lang)
            active, blocked = _spool_ensure_pending(
                instance_root, _NOTICE_KIND_FW_UPDATE, local_tag_ver,
                body_oneline, body_fold, now, built_version, session_id)
            if active is None:
                _log_decision(instance_root,
                              "hold(local): v{} notice spool blocked "
                              "({})".format(local_tag_ver,
                                            blocked or "error"))
                sys.exit(0)
            _dispatch_injection(
                instance_root, active, now, session_id,
                lambda body: _emit_note(
                    built_display, local_tag_ver,
                    "the local source repo at " + repo_root, body, lang),
                "local", local_tag_ver)
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
    if estate:
        pass  # DGN-1417: the planner is the one measurement on this host
    elif _remote_check_enabled(instance_root) and not dev_channel:
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
            # DGN-1586: an existing same-key pending skips the network
            # tag-confirm + note fetch (both already happened at creation);
            # this session only decides injection for its own surface.
            active = _spool_find_pending(
                instance_root, _NOTICE_KIND_FW_UPDATE, remote_version)
            if active is not None:
                _dispatch_injection(
                    instance_root, active, now, session_id,
                    lambda body: _emit_note(
                        built_display, remote_version,
                        "the upstream dogany-agent public repo", body, lang),
                    "remote", remote_version.lstrip("v"))
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
            body_fold = _build_user_notice(remote_version, notes, detail, lang)
            body_oneline = _build_user_notice(remote_version, "", "", lang)
            active, blocked = _spool_ensure_pending(
                instance_root, _NOTICE_KIND_FW_UPDATE, remote_version,
                body_oneline, body_fold, now, built_version, session_id)
            if active is None:
                _log_decision(instance_root,
                              "hold(remote): v{} notice spool blocked "
                              "({})".format(remote_version.lstrip("v"),
                                            blocked or "error"))
                sys.exit(0)
            _dispatch_injection(
                instance_root, active, now, session_id,
                lambda body: _emit_note(
                    built_display, remote_version,
                    "the upstream dogany-agent public repo", body, lang),
                "remote", remote_version.lstrip("v"))
            sys.exit(0)
    elif dev_channel:
        # DGN-1668: a distinct reason from the privacy opt-out, and it must
        # read as one. The public mirror has no dev tags, so this fetch could
        # only ever return a stale release number and cache it as this
        # instance's "newest remote" -- which is exactly how the cache came
        # to hold 2.5.0 for a 2.6.0 instance. Check 0 above already answered
        # for this channel.
        _log_decision(instance_root,
                      "no-op(remote): skipped -- channel=dev is served by "
                      "the local canonical clone (the public mirror carries "
                      "no dev tags, DGN-1668)")
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
        # DGN-1586: kind=pending-restart, version = the CURRENT built (the
        # restart target). Its resolution predicate is snapshot_fw == built
        # (spec 3.2) -- spool-owned, never the fw-update expiry predicate.
        message = _build_pending_restart_notice(built_display, lang)
        active, blocked = _spool_ensure_pending(
            instance_root, _NOTICE_KIND_PENDING_RESTART, built_version,
            message, "", now, built_version, session_id)
        if active is None:
            _log_decision(instance_root,
                          "hold(restart): v{} notice spool blocked "
                          "({})".format(built_version, blocked or "error"))
        else:
            _dispatch_injection(
                instance_root, active, now, session_id,
                lambda body: _emit_pending_restart_note(
                    built_display, body, lang),
                "restart", built_version)

    # DGN-1068: zero-result run leaves a trace too, so "no update found" is
    # distinguishable from "the hook never ran".
    if estate:
        sys.exit(0)  # the request above is this run's trace
    _log_decision(instance_root,
                  "no-op: nothing newer (channel {}, built {}, local-tag {}, "
                  "remote {})".format(channel or "undeclared", built_version,
                                      local_tag_ver or "-",
                                      remote_version or "-"))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open guarantee: never block a session on this hook.
        sys.exit(0)
