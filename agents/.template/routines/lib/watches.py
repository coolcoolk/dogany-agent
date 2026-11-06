#!/usr/bin/env python3
"""Standing watches: one-tap on/off for skills that watch something (DGN-1966).

A skill that runs a standing watch (e.g. "tell me when the PO replies")
declares it in its SKILL.md frontmatter.  The owner switches it on and off
from the workbench pin; a deliberate off is recorded with the health
observer's retire verb, so it never reads as a crash ("a scheduled job
vanished, no retire record").

DECLARATION (SKILL.md frontmatter, one block per skill):

  watch:
    id: po-reply              slug, <=32 chars ([a-z0-9-]); rides callback data
    label: PO reply watch     owner-language row label (or a board string key)
    routine: po-reply-watch   routine-ctl.sh name of the job it arms
    script: .claude/skills/po-watch/run.sh   the job's script (root-relative)
    schedule: "09:00"         routine-ctl schedule: "HH:MM" | "<dow> HH:MM"
    cadence: 1d               expected run interval (<n>m | <n>h | <n>d)
    follow_presence: true     optional: off while the user works at the Mac

  The job it arms is com.telegram-skill-bot.<agent>.<routine> -- the label
  routine-ctl.sh schedules and the health observer watches.  The watch's
  script reports each run with `watches.py record <id> ok|error`.

STATE: <BOT_DATA_DIR>/watches.json, written ONLY by this module (the board
derives a view from it and never writes it; same rule as _UNPARK.md).
Per watch: id, skill, label, routine, schedule_label, cadence_secs,
follow_presence, enabled (the ACTUAL state after actuation, never the
request), updated_ts, last_run_ts, last_result (ok/error), health
(ok/stale/error), override (the last manual toggle, see presence).

SWITCH (the gate-switch.py pattern: a registry, one switch per entry, an
on/off/status CLI -- not a third switch mechanism).  The registry is built
from the SKILL.md declarations.  The switch has no conf key of its own: the
scheduler IS the state, so `enabled` is read back from it after actuation.
  off  routine-ctl.sh disable <routine>, then
       health-observer.py --retire <label> --why "watch off: ..." --force
       (down first, then the marker: the observer clears a marker it sees on
       a still-loaded label -- routine-ctl.sh disable's own DGN-1610 order),
       then status must say "not scheduled".
  on   health-observer.py --unretire <label>, then routine-ctl.sh enable;
       status must say "scheduled".  A failed arm re-retires the label so
       the half-done on cannot alarm either.
  `enabled` is written only after the scheduler confirms the new state; on
  any failure the actual state is re-read and recorded instead.

PRESENCE (follow_presence): on when the user is away, off while they work at
the Mac.  There is no presence/idle signal in the framework today, so only
the switch exists: `watches.py presence at_mac|away` applies a presence
change, and read_presence_signal() is the TODO hook a future signal plugs
into (`watches.py presence-sync`).  A manual toggle always wins until the
NEXT presence change: a report of the presence already recorded changes
nothing.

CLI:
  watches.py status [--json]
  watches.py on|off <id> [--by WHO]
  watches.py toggle <id> on|off [--by WHO]       (the pin button's verb)
  watches.py record <id> ok|error                (the watch job reports a run)
  watches.py sync                                (read actual states back)
  watches.py presence at_mac|away
  watches.py presence-sync                       (TODO signal hook; no-op now)

Exit: 0 ok | 1 actuation failed (state re-read and recorded) | 2 usage /
unknown watch | 3 state file unreadable.

Test seams: DOGANY_BOT_DATA_DIR (workbench_state), DOGANY_SKILLS_DIR,
DOGANY_WATCH_ROUTINE_CTL, DOGANY_WATCH_HEALTH_OBSERVER,
DOGANY_WATCH_AGENT_NAME, DOGANY_WATCH_NO_REFRESH=1 (no --board-refresh
after a health change).  Stdlib only.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workbench_state  # noqa: E402

ROOT_DIR = workbench_state.ROOT_DIR
WATCHES_FILE = "watches.json"
WATCHES_LOCK = ".watches.lock"
LABEL_PREFIX = "com.telegram-skill-bot."

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ROUTINE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SCHEDULE_RE = re.compile(r"^(?:(mon|tue|wed|thu|fri|sat|sun) )?([0-2][0-9]:[0-5][0-9])$")
CADENCE_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")
_CADENCE_UNIT = {"m": 60, "h": 3600, "d": 86400}
PRESENCES = ("at_mac", "away")
# A watch is stale when its last run (or, never run, its arming) is older
# than STALE_FACTOR cadences plus a fixed grace (scheduler jitter, a Mac
# that slept through one slot).
STALE_FACTOR = 2
STALE_GRACE_SECS = 300
RUN_TIMEOUT_SECS = 60


class WatchesUnreadable(Exception):
    """watches.json exists but cannot be read: the board must not take this
    for "no watches" (it would clear on a false empty)."""


# ---------------------------------------------------------------------------
# Declarations (the registry)
# ---------------------------------------------------------------------------

def skills_dir():
    return os.environ.get("DOGANY_SKILLS_DIR") or os.path.join(
        ROOT_DIR, ".claude", "skills")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _frontmatter_watch(path):
    """The raw `watch:` block of one SKILL.md as {key: str}, or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read(65536).splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    block, inside = None, False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not inside:
            if re.match(r"^watch:\s*$", line):
                inside, block = True, {}
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            break  # next top-level key ends the block
        m = re.match(r"^\s+([a-z_]+):\s*(.*?)\s*$", line)
        if m:
            block[m.group(1)] = _unquote(m.group(2))
    return block


def _parse_declaration(skill, raw):
    """A validated declaration dict, or (None, reason)."""
    wid = raw.get("id", "")
    if not ID_RE.match(wid):
        return None, "bad id %r" % wid
    label = (raw.get("label") or "").strip()
    if not label:
        return None, "no label"
    routine = raw.get("routine", "")
    if not ROUTINE_RE.match(routine):
        return None, "bad routine %r" % routine
    script = raw.get("script", "")
    if not script or script.startswith("/") or ".." in script.split("/"):
        return None, "bad script %r" % script
    schedule = raw.get("schedule", "")
    if not SCHEDULE_RE.match(schedule):
        return None, "bad schedule %r" % schedule
    m = CADENCE_RE.match(raw.get("cadence", ""))
    if not m:
        return None, "bad cadence %r" % raw.get("cadence", "")
    follow = raw.get("follow_presence", "false").strip().lower()
    if follow not in ("true", "false", "yes", "no"):
        return None, "bad follow_presence %r" % follow
    return {
        "id": wid,
        "skill": skill,
        "label": label,
        "routine": routine,
        "script": script,
        "schedule": schedule,
        "cadence_secs": int(m.group(1)) * _CADENCE_UNIT[m.group(2)],
        "follow_presence": follow in ("true", "yes"),
    }, None


def declarations(warn=None):
    """{id: declaration} from every <skills>/*/SKILL.md `watch:` block.  A
    malformed block or a duplicate id is skipped (one `warn` line each); the
    first declaration of an id (skill-name order) wins."""
    out = {}
    for path in sorted(glob.glob(os.path.join(skills_dir(), "*", "SKILL.md"))):
        raw = _frontmatter_watch(path)
        if raw is None:
            continue
        skill = os.path.basename(os.path.dirname(path))
        decl, why = _parse_declaration(skill, raw)
        if decl is None or decl["id"] in out:
            if warn is not None:
                warn("[watches] %s: %s" % (
                    skill, why or "duplicate id %r" % decl["id"]))
            continue
        out[decl["id"]] = decl
    return out


def agent_name():
    env = os.environ.get("DOGANY_WATCH_AGENT_NAME")
    if env:
        return env
    try:
        with open(os.path.join(ROOT_DIR, ".instance.conf"), "r",
                  encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DOGANY_AGENT_NAME="):
                    name = line.split("=", 1)[1].strip()
                    if name:
                        return name
    except OSError:
        pass
    return os.path.basename(ROOT_DIR)


def schedule_label(decl):
    """The launchd label routine-ctl.sh gives the watch's job."""
    return "%s%s.%s" % (LABEL_PREFIX, agent_name(), decl["routine"])


# ---------------------------------------------------------------------------
# State file (sole writer)
# ---------------------------------------------------------------------------

def state_path():
    return os.path.join(workbench_state.bot_data_dir(), WATCHES_FILE)


def load_state():
    """The state dict; {} when the file is absent.  Raises WatchesUnreadable
    when it exists but is unreadable or malformed."""
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "watches": {}}
    except Exception as exc:
        raise WatchesUnreadable(str(exc))
    if not isinstance(data, dict) or not isinstance(data.get("watches", {}),
                                                    dict):
        raise WatchesUnreadable("malformed watches.json")
    data.setdefault("version", 1)
    data.setdefault("watches", {})
    return data


def _save_state(data):
    workbench_state.atomic_write(
        state_path(), json.dumps(data, ensure_ascii=False, indent=1,
                                 sort_keys=True) + "\n")


class _Locked(object):
    """Serialize every read-modify-write of watches.json."""

    def __enter__(self):
        os.makedirs(workbench_state.bot_data_dir(), exist_ok=True)
        self._fh = open(os.path.join(workbench_state.bot_data_dir(),
                                     WATCHES_LOCK), "a+")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False


def compute_health(entry, now=None):
    """ok / stale / error for a switched-on watch (a switched-off one is
    always ok: nothing is expected of it)."""
    if not entry.get("enabled"):
        return "ok"
    if entry.get("last_result") == "error":
        return "error"
    now = time.time() if now is None else now
    cadence = entry.get("cadence_secs") or 0
    ref = entry.get("last_run_ts") or entry.get("updated_ts") or 0
    if cadence and ref and now - ref > STALE_FACTOR * cadence + STALE_GRACE_SECS:
        return "stale"
    return "ok"


def _entry(data, decl):
    """The state entry for `decl`, created and refreshed from it."""
    entry = data["watches"].setdefault(decl["id"], {"enabled": False,
                                                    "updated_ts": 0})
    entry.update({
        "id": decl["id"],
        "skill": decl["skill"],
        "label": decl["label"],
        "routine": decl["routine"],
        "schedule_label": schedule_label(decl),
        "cadence_secs": decl["cadence_secs"],
        "follow_presence": decl["follow_presence"],
    })
    entry.setdefault("last_run_ts", None)
    entry.setdefault("last_result", None)
    return entry


# ---------------------------------------------------------------------------
# Actuation
# ---------------------------------------------------------------------------

def _routine_ctl():
    return os.environ.get("DOGANY_WATCH_ROUTINE_CTL") or os.path.join(
        ROOT_DIR, "routines", "lib", "routine-ctl.sh")


def _observer():
    return os.environ.get("DOGANY_WATCH_HEALTH_OBSERVER") or os.path.join(
        ROOT_DIR, "routines", "health-observer.py")


def _run(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=RUN_TIMEOUT_SECS)
    except Exception as exc:
        return 99, str(exc)
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()


def _ctl(*args):
    return _run(["bash", _routine_ctl()] + list(args))


def _obs(*args):
    return _run([sys.executable, _observer()] + list(args))


def scheduled(decl):
    """True / False from routine-ctl.sh status, None when it cannot tell."""
    rc, _ = _ctl("status", decl["routine"])
    if rc == 0:
        return True
    if rc == 3:
        return False
    return None


def _disarm(decl, why):
    rc, err = _ctl("disable", decl["routine"])
    # routine-ctl.sh disable exits 2 when ITS OWN retire marker failed (the
    # unit is already down); the explicit retire below covers it.
    if rc not in (0, 2):
        return "disarm failed (rc=%d): %s" % (rc, err)
    rc, err = _obs("--retire", schedule_label(decl), "--why", why, "--force")
    if rc != 0:
        return "retire record failed (rc=%d): %s" % (rc, err)
    if scheduled(decl) is not False:
        return "still scheduled after disarm"
    return None


def _arm(decl, why):
    label = schedule_label(decl)
    rc, err = _obs("--unretire", label)
    if rc != 0:
        return "unretire failed (rc=%d): %s" % (rc, err)
    parts = decl["schedule"].split(" ")
    hhmm, dow = parts[-1], (parts[0] if len(parts) == 2 else "")
    args = ["enable", decl["routine"], decl["script"], hhmm]
    if dow:
        args.append(dow)
    rc, err = _ctl(*args)
    if rc != 0 or scheduled(decl) is not True:
        # Not armed: put the marker back so the half-done on stays silent.
        _obs("--retire", label, "--why", why + " (arm failed)", "--force")
        return "arm failed (rc=%d): %s" % (rc, err or "not scheduled")
    return None


def _apply(data, decl, want, by, now):
    """Drive one watch to `want`; record the ACTUAL state.  Returns an error
    string or None."""
    entry = _entry(data, decl)
    why = "watch %s: %s (%s %s)" % ("on" if want else "off", decl["id"], by,
                                     time.strftime("%Y-%m-%d %H:%M"))
    err = _arm(decl, why) if want else _disarm(decl, why)
    actual = want if err is None else scheduled(decl)
    if actual is not None and actual != entry.get("enabled"):
        entry["enabled"] = actual
        entry["updated_ts"] = int(now)
    elif err is None:
        entry["updated_ts"] = int(now)
    entry["health"] = compute_health(entry, now)
    return err


def _request_board_refresh():
    if os.environ.get("DOGANY_WATCH_NO_REFRESH") == "1":
        return
    script = os.path.join(ROOT_DIR, "routines", "status-footer.py")
    if not os.path.isfile(script):
        return
    try:
        subprocess.Popen([sys.executable, script, "--board-refresh"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def set_enabled(wid, want, by="agent"):
    """Switch one watch.  Returns (rc, message).  A manual switch records an
    override that holds until the next presence change."""
    decl = declarations().get(wid)
    if decl is None:
        return 2, "unknown watch: %s" % wid
    now = time.time()
    with _Locked():
        data = load_state()
        err = _apply(data, decl, want, by, now)
        entry = data["watches"][wid]
        if by != "presence":
            entry["override"] = {"enabled": entry["enabled"], "by": by,
                                 "ts": int(now)}
        _save_state(data)
    if err:
        return 1, "%s: %s (recorded state: %s)" % (
            wid, err, "on" if entry["enabled"] else "off")
    return 0, "%s: %s" % (wid, "on" if entry["enabled"] else "off")


def record_run(wid, result):
    """The watch job reports one run (ok / error)."""
    if result not in ("ok", "error"):
        return 2, "result must be ok or error"
    decl = declarations().get(wid)
    if decl is None:
        return 2, "unknown watch: %s" % wid
    now = time.time()
    with _Locked():
        data = load_state()
        entry = _entry(data, decl)
        before = entry.get("health")
        entry["last_run_ts"] = int(now)
        entry["last_result"] = result
        entry["health"] = compute_health(entry, now)
        _save_state(data)
    if entry["health"] != before:
        _request_board_refresh()
    return 0, "%s: %s" % (wid, result)


def sync():
    """Read every declared watch's actual state back from the scheduler."""
    now = time.time()
    with _Locked():
        data = load_state()
        for decl in declarations(warn=sys.stderr.write).values():
            entry = _entry(data, decl)
            actual = scheduled(decl)
            if actual is not None and actual != entry.get("enabled"):
                entry["enabled"] = actual
                entry["updated_ts"] = int(now)
            entry["health"] = compute_health(entry, now)
        _save_state(data)
    return 0, "synced"


# ---------------------------------------------------------------------------
# Presence (follow_presence)
# ---------------------------------------------------------------------------

def read_presence_signal():
    """TODO(DGN-1966): presence signal hook.

    The framework has no presence / idle signal today (nothing reads the
    Mac's input idle time or screen lock), and DGN-1966 forbids inventing
    one here.  When a signal exists, return "at_mac" or "away" from it; until
    then this returns None and `presence-sync` changes nothing.  The switch
    itself is apply_presence() / `watches.py presence at_mac|away`."""
    return None


def apply_presence(presence):
    """Apply a presence report to every follow_presence watch: at_mac -> off,
    away -> on.  Only a CHANGE of presence acts (and clears the manual
    overrides): repeating the recorded presence changes nothing, so a manual
    toggle wins until the next change."""
    if presence not in PRESENCES:
        return 2, "presence must be at_mac or away"
    now = time.time()
    errors = []
    with _Locked():
        data = load_state()
        prev = (data.get("presence") or {}).get("state")
        if prev == presence:
            return 0, "presence unchanged: %s" % presence
        data["presence"] = {"state": presence, "ts": int(now)}
        want = presence == "away"
        for decl in declarations().values():
            if not decl["follow_presence"]:
                continue
            entry = _entry(data, decl)
            entry.pop("override", None)
            err = _apply(data, decl, want, "presence", now)
            if err:
                errors.append("%s: %s" % (decl["id"], err))
        _save_state(data)
    if errors:
        return 1, "; ".join(errors)
    return 0, "presence: %s" % presence


# ---------------------------------------------------------------------------
# Board view (read-only)
# ---------------------------------------------------------------------------

def board_view(now=None):
    """Rows for the workbench "watches" section, one per DECLARED watch:
    {id, label, enabled (True/False, None = not checked yet), health,
    follow_presence, last_run_ts}.  Never writes.  Raises WatchesUnreadable
    when watches.json exists but cannot be read."""
    decls = declarations()
    if not decls:
        return []
    data = load_state()
    now = time.time() if now is None else now
    rows = []
    for wid, decl in decls.items():
        entry = data["watches"].get(wid)
        if entry is None:
            rows.append({"id": wid, "label": decl["label"], "enabled": None,
                         "health": "ok",
                         "follow_presence": decl["follow_presence"],
                         "last_run_ts": None})
            continue
        view = dict(entry, cadence_secs=decl["cadence_secs"])
        rows.append({"id": wid, "label": decl["label"],
                     "enabled": bool(entry.get("enabled")),
                     "health": compute_health(view, now),
                     "follow_presence": decl["follow_presence"],
                     "last_run_ts": entry.get("last_run_ts")})
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = (
    "usage: watches.py status [--json]\n"
    "       watches.py on|off <id> [--by WHO]\n"
    "       watches.py toggle <id> on|off [--by WHO]\n"
    "       watches.py record <id> ok|error\n"
    "       watches.py sync\n"
    "       watches.py presence at_mac|away\n"
    "       watches.py presence-sync\n")


def _cmd_status(as_json):
    try:
        rows = board_view()
    except WatchesUnreadable as exc:
        return 3, "watches.json unreadable: %s" % exc
    if as_json:
        return 0, json.dumps(rows, ensure_ascii=False)
    if not rows:
        return 0, "no watches declared"
    out = []
    for r in rows:
        state = {True: "on", False: "off", None: "unchecked"}[r["enabled"]]
        out.append("%s\t%s\t%s%s" % (r["id"], state, r["health"],
                                     "\tfollow_presence"
                                     if r["follow_presence"] else ""))
    return 0, "\n".join(out)


def main(argv):
    args = list(argv)
    by = "agent"
    if "--by" in args:
        i = args.index("--by")
        if i + 1 >= len(args) or not args[i + 1].strip():
            return 2, USAGE
        by = args[i + 1].strip()[:32]
        del args[i:i + 2]
    if not args:
        return 2, USAGE
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "status" and rest in ([], ["--json"]):
            return _cmd_status(rest == ["--json"])
        if cmd in ("on", "off") and len(rest) == 1:
            return set_enabled(rest[0], cmd == "on", by)
        if cmd == "toggle" and len(rest) == 2 and rest[1] in ("on", "off"):
            return set_enabled(rest[0], rest[1] == "on", by)
        if cmd == "record" and len(rest) == 2:
            return record_run(rest[0], rest[1])
        if cmd == "sync" and not rest:
            return sync()
        if cmd == "presence" and len(rest) == 1:
            return apply_presence(rest[0])
        if cmd == "presence-sync" and not rest:
            presence = read_presence_signal()
            if presence is None:
                return 0, "presence signal: none (TODO hook, nothing applied)"
            return apply_presence(presence)
    except WatchesUnreadable as exc:
        return 3, "watches.json unreadable: %s" % exc
    return 2, USAGE


if __name__ == "__main__":
    code, message = main(sys.argv[1:])
    if message:
        (sys.stdout if code == 0 else sys.stderr).write(message.rstrip("\n")
                                                        + "\n")
    sys.exit(code)
