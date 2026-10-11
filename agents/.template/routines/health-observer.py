#!/usr/bin/env python3
"""health-observer.py -- DGN-986 axis A: scheduled-job silent-death observer.

Locked spec: worklog DGN-986 (sections "2. axis A", revisions R1/R2/R3, D2
poll-interval amendment = 5 min). Evidence ledger: DGN-989 (one instance's scheduled
jobs drift -- the live case R1-2 exists to catch).

What this does, once per poll (launchd StartInterval 300 s):
  1. Enumerates loaded launchd jobs (``launchctl list``) with the
     ``com.telegram-skill-bot.`` prefix -- machine-global, all instances.
  2. Reads per-label ``launchctl print gui/<uid>/<label>`` and parses the
     4-state exit field (R3):
       (i)   ``last exit code`` line ABSENT (job running right now)
             -> judgment held, streak unchanged;
       (ii)  ``(never exited)``            -> waiting bucket (future one-shot);
       (iii) negative (signal death, e.g. -15) -> NOT structural, general streak;
       (iv)  positive -> 126/127/78 are structural (no self-heal).
     ``runs`` delta < 0 = counter reset (job reloaded) -> episode reset,
     accumulated failures discarded. Parse failures are themselves recorded in
     the state file so /health can surface them (the observer must not die
     silently).
  3. Classifies (R2): resident bridge = membership in the Label set read from
     ``*.newbridge.plist`` files (NEVER a dot-count heuristic; plist FILE NAME
     is not the label -- read the ``Label`` key); remaining labels whose
     ``launchctl print`` top-level ``properties`` carry ``keepalive`` =
     resident-service bucket (excluded from exit accounting; judged instead on
     "should be up but has no PID"); ``runs == 0 && (never exited)`` = waiting
     bucket. Everything else = scheduled bucket. All counts are computed
     dynamically -- no constants.
  4. Failure-streak accounting (spec axis A): per label, runs increased AND
     last exit != 0 -> streak +1; exit 0 observed -> streak reset = episode
     end. Notify thresholds: general streak >= 3, structural exit streak >= 2.
     ONE push per episode (``notified_at``); recovery then re-failure = new
     episode = new push.
  5. Label-loss events (R1, DGN-1427 amendment): the state file keeps the
     known label set. Every poll, ``known - currently loaded`` = lost labels.
     The FIRST miss only starts a grace window (``lost_at``, no event) --
     confirmation, not a single dropped poll, since a planned-restart gap can
     land inside one poll window (live case: 2m15s main-agent redeploy caught
     by exactly one poll, 2026-09-12). Only once the label is STILL missing
     ``LOST_GRACE_SECONDS`` after that first miss does an unretired loss fire
     as a notify event (fail-loud, same rank as a failure streak). Retired
     markers (``retired: {when, why}``) suppress the event at fire time, same
     as before.
     DGN-1900: a burst (LOSS_BURST_MIN+ labels confirmed in one poll) is
     held once and fires only when a later poll re-confirms it, and never
     from an observer whose own instance just restarted (partial launchctl
     read). Machine talk and unresolvable losses go to ONE steward
     (explicit ``~/.dogany/machine-steward`` marker, else the first crew's
     representative); a PoC never stewards.
     DGN-1990: a loss whose first miss falls inside an update apply run's
     ledger window (``~/.dogany/update-ledger``) on an instance that run
     touches is held as the run's (``update_hold`` on the entry), not
     announced -- see UPDATE_SETTLE_SECONDS.
     DGN-1670: the same one-announcer route covers EVERY per-unit alert
     (failure streak and resident down too, not only losses), and no
     observer ever announces a unit of an instance whose owner chat
     differs from its own (see ``loss_is_mine``).
  6. Disk-plist vs loaded diff (R1-2): plists are enumerated from
     ``~/Library/LaunchAgents`` AND every instance ``routines/`` directory
     (instance roots resolved from newbridge ``ProgramArguments --path``).
     On disk but not loaded = info-only record (STATIC DISK STATE IS NOT A
     SIGNAL -- a plist on disk is no evidence the job should run; only
     transitions warn). Loaded but NO LaunchAgents plist carries the label
     (Label-KEY set membership, never path equality) = "won't survive
     reboot" WARN. Loaded from a different file while a LaunchAgents copy
     exists = plist_divergence: identical content -> info-only record;
     differing content -> WARN ("a reboot starts this job with different
     configuration"). All recorded in the state file for /health with a
     level field (warn/info) so part [c] can render them apart (DGN-986
     success criterion 6; live proof = DGN-989: 2 loaded-outside
     jobs; divergence live proof = an instance bridge label,
     LaunchAgents copy vs loaded repo copy differing in
     EnvironmentVariables.PATH).

  7. Dawn memory queue never-ran nights (DGN-1582 r3, dec-236): a due night
     with no queue log at all (Mac off at 04:00) is read from the logs the
     queue already writes; 2 consecutive -> one owner + one operator notice
     per streak (steward-gated, machine-level state ``dawn.json``).

  8. Instance drive stale (DGN-1655): this instance's framework-owned
     ``<agent>.instance-drive`` label is LOADED but the newest reserved
     ``terminal-state-sweep`` beat in its terminal-state ledger is older than
     TTL/2 (two missed drive periods; the number comes from the ledger's own
     policy, never a copy here). One warning episode -> one session-inbox
     drop for the owning agent; the episode clears silently once a fresh beat
     shows. Covers the shape the loss/exit rules cannot: a unit that stays
     loaded but stops firing (runs never advances). Reads the ledger only --
     it never creates or renews the beat. Per-instance state
     ``drive-<agent>.json``. Label loss itself stays on rule 3.

READ-ONLY ABSOLUTE PRINCIPLE: this observer never touches a job. No load /
unload / bootout, no writes to any other agent's files. It only reads, writes
its own state file, and notifies.

State persistence: ``~/.dogany/health-observer/jobs.json`` (machine-global,
reboot-surviving -- never /tmp; an episode can span days). Atomic writes
(tmp + os.replace). A corrupt state file is quarantined (renamed) and recorded
as a warning -- known-set knowledge is lost for that cycle (accepted; the
fresh baseline cannot produce false loss events).

Notifications reuse ``routines/push.sh`` (NO new notify surface). Every push
carries a CTA (no dead ends): self-explanatory sentence CTAs in the body plus
one inline button using the ``opt:`` callback (bridge resolves the tapped
button's text into a session user message -- bot.py opt: handler /
options.resolve_choice, the exact session-reach mechanism DGN-986 constraint 5
names). NOTE (spec-vs-system gap, reported at handoff): spec section 7(ga)
declares an IDRILL 2-button keyboard, but an IDRILL final-step tap fires an
arm-declared argv subprocess (IDRILL-ARM-CONTRACT.md section 4) -- it cannot
deliver the tapped sentence to the session as a user message. Wiring both
sentences as session-reaching buttons needs either a bridge-side injection
rail (part [c] lane) or argv invention. This build ships ONE opt: button
(recommended action) + both sentence CTAs; swap is isolated in compose_*().

Accepted residual limits (spec R8): a fail->recover cycle entirely inside one
poll window (runs +2, last exit 0) is invisible by design; Linux
systemd --user twin is OUT OF SCOPE (unverified) -- non-darwin exits 0
silently.

The observer itself is wrapped by cron-guard.sh (see the shipped plist
template) and writes a heartbeat timestamp every poll, so /health can show
when the observer last ran -- the observer's observer (DGN-985).

CLI:
  health-observer.py                     one poll cycle (default)
  health-observer.py --dry-run           poll, but print notifications to
                                         stdout instead of pushing
  health-observer.py --retire LABEL --why TEXT [--force]
                                          mark an intentional removal. TEXT is
                                          mandatory. LABEL must be known
                                          (in state, or currently loaded on
                                          darwin) unless --force is given.
  health-observer.py --unretire LABEL    drop the marker
  health-observer.py --status            dump the state file (JSON)
  health-observer.py --list-retired      list every retired marker
  health-observer.py --machine-alerts on|off|status [--until WHEN]
                                         DGN-1911 machine-alert switch in
                                         config/agent.conf (status is
                                         read-only; --until = time-boxed off)
  health-observer.py --help | -h         print this usage

  DGN-1610: any argument outside this set is a usage error (exit 2) on
  stderr -- it never falls through to a live poll.
"""

import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from scheduled_job_names import display_name

LABEL_PREFIX = "com.telegram-skill-bot."

# Structural exits: cannot self-heal (127 = script/binary missing, 126 = not
# executable/permission, 78 = EX_CONFIG). Negative signal exits are NEVER
# structural (R3 iii) -- and never appear here since members are positive.
STRUCTURAL_EXITS = frozenset((126, 127, 78))
GENERAL_STREAK_NOTIFY = 3
STRUCTURAL_STREAK_NOTIFY = 2

# DGN-1427: a poll landing inside a planned-restart gap must not fire as a
# loss (live case 2026-09-12: main-agent bot self-restart on a kit deploy, measured
# down window 2m15s = 135s, caught by exactly one poll). 240s is chosen as
# comfortably above that measured gap (+~78% margin, room for a slightly
# slower restart) while staying under the 300s launchd poll interval, so a
# label that is genuinely gone is still confirmed on the very next scheduled
# poll -- no long-tail delay on a real vanish.
LOST_GRACE_SECONDS = 240

# DGN-1900: a mass loss (LOSS_BURST_MIN+ labels confirmed in one poll) is a
# partial launchctl read until proven otherwise -- live case 2026-10-07: a
# self-updated instance restarted at 06:35 and its observer pushed "23 jobs
# vanished" at 06:41 while every job was loaded.  A burst is held once and
# fires only if a LATER poll (>= LOSS_BURST_RECONFIRM_SECONDS after the hold,
# i.e. not a second observer firing in the same minute) still confirms it.
# An observer whose own bridge restarted within OWN_RESTART_GRACE_SECONDS
# (or is not loaded right now) never fires a burst.  A single-label loss
# keeps the plain LOST_GRACE_SECONDS rule.
LOSS_BURST_MIN = 5
LOSS_BURST_RECONFIRM_SECONDS = 60
OWN_RESTART_GRACE_SECONDS = 600

# DGN-1990: an update apply run (scripts/pack/update_surface.py ledger,
# <shared home>/update-ledger/<id>.json + .start/.result) bootouts every unit
# of the instances it updates.  A loss whose first miss falls inside the
# run's window -- start to result, plus UPDATE_SETTLE_SECONDS for the next
# poll to notice and the restart to land -- is the run's, not a vanish:
# live case 2026-10-11 00:09Z, run 4ea6b51d stopped 23 units of one
# instance, finished in 9 s and "23 jobs vanished without a retire record"
# went out 7 minutes later.  The hold lasts while the run is open, until
# UPDATE_SETTLE_SECONDS after its result (a unit still missing then is a
# real loss), and -- for an instance the result reports as left down --
# until a recorded restart of that instance plus the settle time (the run's
# own result notice already told the owner).  A run with no result after
# UPDATE_OPEN_MAX_SECONDS is treated as ended then.
UPDATE_SETTLE_SECONDS = 900
UPDATE_OPEN_MAX_SECONDS = 3 * 60 * 60
UPDATE_LOOKBACK_SECONDS = 2 * 24 * 60 * 60
UPDATE_START_SLACK_SECONDS = 60

NEVER_EXITED = "(never exited)"
STATE_SCHEMA = 1

# DGN-1713 machine-health defaults.  They live here as the fail-open values;
# an instance can tune them in config/agent.conf without changing the shared
# machine contract.  A condition is announced once per unhealthy episode.
# DGN-1773: a static breach is not a signal.  While still breached, a
# condition re-alerts only when its reading worsens by a step since the last
# notice (disk/swap; load keeps only its sustain rule) or after the long
# reminder interval.  The old HEALTH_OBSERVER_MACHINE_ALERT_WINDOW_SECONDS
# key is no longer read.
DEFAULT_SWAP_WARN_MB = 4096
DEFAULT_LOAD_WARN_MULTIPLIER = 1.5
DEFAULT_LOAD_WARN_SECONDS = 15 * 60
DEFAULT_DISK_WARN_PCT = 85
DEFAULT_DISK_STEP_PCT = 2
DEFAULT_SWAP_STEP_MB = 1024
DEFAULT_MACHINE_REMIND_SECONDS = 24 * 60 * 60
# DGN-1879: hysteresis.  A notified breach clears only once the reading drops
# to threshold minus a margin or lower (disk 85% -> clears at 83%; swap
# 4096 MB -> at 3584 MB; load 1.5x ncpu -> at 1.25x ncpu), and a breach that
# re-crosses within the re-arm window after clearing resumes the old episode
# (same breach_started_at, same last notice) instead of starting a new one.
DEFAULT_DISK_CLEAR_MARGIN_PCT = 2
DEFAULT_SWAP_CLEAR_MARGIN_MB = 512
DEFAULT_LOAD_CLEAR_MARGIN = 0.25
DEFAULT_MACHINE_REARM_SECONDS = 6 * 60 * 60

BUCKET_BRIDGE = "bridge"
BUCKET_RESIDENT = "resident_service"
BUCKET_WAITING = "waiting"
BUCKET_SCHEDULED = "scheduled"


def now_iso():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _seconds_between(earlier_iso, later_iso):
    """Best-effort elapsed seconds between two ISO timestamps.

    A parse failure (opaque/malformed timestamp) is treated as "grace
    already elapsed" -- the observer must fail loud (R1 principle), never
    silently suppress a loss it can no longer time.
    """
    import datetime
    try:
        earlier = datetime.datetime.fromisoformat(earlier_iso)
        later = datetime.datetime.fromisoformat(later_iso)
        return (later - earlier).total_seconds()
    except (ValueError, TypeError):
        return LOST_GRACE_SECONDS


def state_dir():
    return os.environ.get(
        "DOGANY_HEALTH_OBSERVER_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".dogany", "health-observer"),
    )


def state_path():
    return os.path.join(state_dir(), "jobs.json")


def machine_state_path():
    """DGN-1713 shared, host-level condition state (not per instance)."""
    return os.path.join(state_dir(), "machine.json")


def _instance_root():
    return os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def _conf_value(key, root=None):
    """Small, quote-aware agent.conf reader; unreadable/missing -> empty."""
    path = os.path.join(root or _instance_root(), "config", "agent.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _conf_number(key, default, root=None, minimum=0):
    try:
        value = float(_conf_value(key, root))
        return value if value >= minimum else default
    except ValueError:
        return default


def _conf_positive(key, default, root=None):
    """Like _conf_number, but 0/negative falls back to the default.

    A zero step or reminder would re-alert a static breach every poll --
    exactly the DGN-1773 defect -- so it is never a valid setting.
    """
    value = _conf_number(key, default, root)
    return value if value > 0 else default


def _class_representative(root):
    """Crew representative CLASS rule (gate.sh / install.sh / crew_rows):
    DOGANY_AGENT_CLASS other than "domain" (absent/unknown -> main).

    This is only the crew-leader rule.  It is NOT the machine steward rule
    any more (DGN-1900): every PoC and every crew leader is class=main.
    """
    try:
        with open(os.path.join(root, ".instance.conf"),
                  encoding="utf-8", errors="replace") as fh:
            values = [line.split("=", 1)[1].strip().strip('"').strip("'")
                      for line in fh if line.startswith("DOGANY_AGENT_CLASS=")]
        return not values or values[-1].lower() != "domain"
    except OSError:
        return True


def is_poc_root(root):
    """PoC instance layout (~/dogany/<x>/poc/<name>, scripts/dogany
    list_instances).  A PoC never speaks for the machine (DGN-1900)."""
    parent = os.path.basename(os.path.dirname(_real(root)))
    return parent.lower() == "poc"


def steward_marker_path():
    """DGN-1900: the ONE designated machine steward, named explicitly.

    One line: the absolute instance root of the crew representative that
    speaks for the machine (machine-health, dawn never-ran, unresolvable
    label losses).  Machine-global, next to the crews home.
    """
    shared = os.environ.get(
        "DOGANY_SHARED_HOME", os.path.join(os.path.expanduser("~"), ".dogany"))
    return os.environ.get("DOGANY_MACHINE_STEWARD_FILE",
                          os.path.join(shared, "machine-steward"))


def read_steward_marker(path=None):
    """Designated steward root (real path), or None when unset/unreadable."""
    try:
        with open(path or steward_marker_path(), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return _real(line)
    except OSError:
        pass
    return None


def machine_steward(crews=None):
    """DGN-1900: the single steward root, or None (= class fallback).

    1. the explicit marker (``steward_marker_path``), unless it names a PoC
       or a root that is not there;
    2. else, when crews exist: the leader of the first crew (kit order)
       that has a non-PoC leader -- the crew representative;
    3. else None: no crew on the machine (a standalone install) -- the
       caller falls back to the class rule for its own root.
    """
    marked = read_steward_marker()
    if marked and os.path.isdir(marked) and not is_poc_root(marked):
        return marked
    crews = read_crews() if crews is None else crews
    for kit in sorted(crews or {}):
        leader = crew_leader(crews[kit])
        if leader and not is_poc_root(leader):
            return leader
    return None


def is_machine_steward(root=None, crews=None):
    """Whether ``root`` is THE machine steward (one voice per machine).

    DGN-1900: the old rule accepted any class!=domain instance, so every PoC
    and every crew leader was a "steward" and each pushed machine talk on
    its own bot.  Now exactly one root qualifies (``machine_steward``); a
    PoC never does.  Only a machine with no marker and no crew at all falls
    back to the class rule (a single standalone install).
    """
    root = _real(root or _instance_root())
    if is_poc_root(root):
        return False
    steward = machine_steward(crews)
    if steward:
        return root == steward
    return _class_representative(root)


# ---------------------------------------------------------------------------
# parsers (pure -- unit-tested against fixtures, never call launchctl here)
# ---------------------------------------------------------------------------

def parse_launchctl_list(text):
    """``launchctl list`` -> {label: pid_or_None} for our prefix only.

    Format: PID<TAB>Status<TAB>Label; PID column is "-" when not running.
    Labels never legitimately contain tabs; split on whitespace max twice so
    an exotic label with spaces still lands whole in the third field.
    """
    jobs = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, label = parts[0].strip(), parts[2].strip()
        if not label.startswith(LABEL_PREFIX):
            continue
        jobs[label] = int(pid_s) if pid_s.isdigit() else None
    return jobs


def parse_launchctl_print(text):
    """Parse one ``launchctl print gui/<uid>/<label>`` output (R3 4-state).

    Returns a dict:
      runs        int or None (None = parse failure)
      has_exit_line  bool -- False = state (i), judgment held
      last_exit   int (may be negative) | NEVER_EXITED | None
      keepalive   bool -- from the TOP-LEVEL ``properties = a | b | c`` line
                  only. Deeper ``keepalive = 0`` lines inside sub-blocks are
                  event-channel noise and must be ignored (verified live:
                  a live <slug>.<job> label prints ten ``keepalive = 0`` sub-lines
                  while its properties line has no keepalive).
      plist_path  str or None -- top-level ``path = `` line (bootstrap source;
                  the R1-2 reboot-survival judgment reads this). ``stdout
                  path`` / ``stderr path`` lines do not match.
      pid         int or None
      state       str or None (first ``state = `` line = top level)
      parse_ok    bool (runs parsed)
    Never raises; the caller records parse_ok=False in the state file.
    """
    info = {
        "runs": None,
        "has_exit_line": False,
        "last_exit": None,
        "keepalive": False,
        "plist_path": None,
        "pid": None,
        "state": None,
        "parse_ok": False,
    }
    try:
        for raw in text.splitlines():
            line = raw.strip()
            if info["plist_path"] is None and line.startswith("path = "):
                info["plist_path"] = line[len("path = "):].strip()
            elif info["state"] is None and line.startswith("state = "):
                info["state"] = line[len("state = "):].strip()
            elif info["runs"] is None and line.startswith("runs = "):
                v = line[len("runs = "):].strip()
                try:
                    info["runs"] = int(v)
                except ValueError:
                    pass
            elif info["pid"] is None and line.startswith("pid = "):
                v = line[len("pid = "):].strip()
                try:
                    info["pid"] = int(v)
                except ValueError:
                    pass
            elif not info["has_exit_line"] and line.startswith("last exit code = "):
                info["has_exit_line"] = True
                v = line[len("last exit code = "):].strip()
                if v == NEVER_EXITED:
                    info["last_exit"] = NEVER_EXITED
                else:
                    try:
                        info["last_exit"] = int(v)  # handles negatives (-15)
                    except ValueError:
                        info["last_exit"] = None  # anomaly: held, recorded
            elif line.startswith("properties = "):
                props = [p.strip() for p in line[len("properties = "):].split("|")]
                if "keepalive" in props:
                    info["keepalive"] = True
    except Exception:
        return info
    info["parse_ok"] = info["runs"] is not None
    return info


def read_plist(path, errors=None):
    """Read a plist file (xml or binary) -> dict, or None on any failure.

    DGN-1442: a parse failure is a COVERAGE HOLE (the unit's Label never
    reaches the label set, so every judgment about it is void). When the
    caller passes an ``errors`` list, each failure is appended as
    ``{"path", "error"}`` -- the caller surfaces it; the read itself stays
    fail-open (None) so the remaining units keep being observed.
    """
    import plistlib
    import re

    def note(error, level=None):
        # One record per file: bridge_map_from_dir and scan_disk both read
        # every LaunchAgents newbridge plist.
        if errors is not None and not any(e["path"] == path for e in errors):
            rec = {"path": path, "error": error}
            if level:
                rec["level"] = level
            errors.append(rec)

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        note("%s: %s" % (type(exc).__name__, exc))
        return None
    if not data.strip():
        # DGN-1990: an empty file is no unit launchd could load either --
        # nothing is hidden from the label scan, so it is no coverage hole.
        note("empty file (no unit; launchd cannot load it either)", "info")
        return None
    try:
        return plistlib.loads(data)
    except Exception as exc:
        first = "%s: %s" % (type(exc).__name__, exc)
    # DGN-1990: an XML comment with a double hyphen (illegal XML, accepted
    # by plutil and launchd) must not cost the unit its observation: retry
    # with every comment removed and keep the finding as an info record.
    stripped = re.sub(rb"<!--.*?-->", b"", data, flags=re.S)
    if stripped != data:
        try:
            pdict = plistlib.loads(stripped)
        except Exception:
            pdict = None
        if pdict is not None:
            note("illegal XML comment (read with comments removed): %s"
                 % first, "info")
            return pdict
    note(first)
    return None


def plist_label(pdict):
    label = (pdict or {}).get("Label")
    return label if isinstance(label, str) else None


def bridge_map_from_dir(launch_agents_dir, errors=None):
    """{bridge_label: instance_root} from ``*.newbridge.plist`` files.

    R2: residency = membership in the Label SET read from newbridge plists
    (the plist FILE NAME is not the label -- e.g. a file named
    <slug>.newbridge.plist can carry Label com.telegram-skill-bot.<slug>).
    R4: instance root = the ProgramArguments element after ``--path``
    (WorkingDirectory is useless -- verified all-identical live).
    """
    bridges = {}
    try:
        names = sorted(os.listdir(launch_agents_dir))
    except OSError:
        return bridges
    for name in names:
        if not name.endswith(".newbridge.plist"):
            continue
        pd = read_plist(os.path.join(launch_agents_dir, name), errors)
        label = plist_label(pd)
        if not label or not label.startswith(LABEL_PREFIX) or "__" in label:
            continue
        root = None
        args = pd.get("ProgramArguments")
        if isinstance(args, list):
            for i, a in enumerate(args):
                if a == "--path" and i + 1 < len(args):
                    root = args[i + 1]
                    break
        bridges[label] = root
    return bridges


def scan_disk(launch_agents_dir, routines_dirs, errors=None):
    """Enumerate on-disk job plists -> {label: {paths, in_launch_agents}}.

    Reads the ``Label`` KEY of every ``*.plist`` (name-suffix exact, so
    ``.plist.bak-*`` backups are excluded); skips labels outside our prefix
    and unrendered templates (un-substituted slug placeholders). Unparseable plists are
    appended to ``errors`` when given (DGN-1442, see ``read_plist``).
    """
    disk = {}

    def scan(directory, is_la):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            if not name.endswith(".plist"):
                continue
            path = os.path.join(directory, name)
            label = plist_label(read_plist(path, errors))
            if not label or not label.startswith(LABEL_PREFIX) or "__" in label:
                continue
            entry = disk.setdefault(
                label, {"paths": [], "la_paths": [], "in_launch_agents": False})
            entry["paths"].append(path)
            if is_la:
                entry["la_paths"].append(path)
            entry["in_launch_agents"] = entry["in_launch_agents"] or is_la

    scan(launch_agents_dir, True)
    for d in routines_dirs:
        scan(d, False)
    return disk


def plists_equivalent(path_a, path_b):
    """Normalized plist content comparison for the divergence judgment.

    Parse both files with plistlib and compare the parsed objects -- the same
    format-independent normalization ``plutil -p`` performs (xml vs binary vs
    key order never false-positives), done in-process so tests stay
    subprocess-free. Any parse failure falls back to a sha256 byte compare.
    Returns True (equivalent) / False (differ) / None (unreadable).
    """
    import plistlib
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            return plistlib.load(fa) == plistlib.load(fb)
    except Exception:
        pass
    import hashlib
    try:
        digests = []
        for p in (path_a, path_b):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                h.update(f.read())
            digests.append(h.hexdigest())
        return digests[0] == digests[1]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# classification (R2)
# ---------------------------------------------------------------------------

def classify_job(label, info, bridge_labels):
    if label in bridge_labels:
        return BUCKET_BRIDGE
    if info.get("keepalive"):
        return BUCKET_RESIDENT
    if info.get("last_exit") == NEVER_EXITED and (info.get("runs") or 0) == 0:
        return BUCKET_WAITING
    return BUCKET_SCHEDULED


# ---------------------------------------------------------------------------
# per-bucket state machines
# ---------------------------------------------------------------------------

def _reset_episode(entry):
    entry["fail_streak"] = 0
    entry["episode_started_at"] = None
    entry["notified_at"] = None


def update_scheduled(label, entry, info, now, may_notify=True):
    """Failure-streak accounting for one scheduled job. Returns events.

    DGN-1670: ``may_notify`` = this observer announces the label (see
    ``loss_is_mine``). Any observer keeps the shared streak; only the
    announcer sets ``notified_at`` and emits the event, so a streak another
    observer counted still fires once the announcer polls.
    """
    events = []
    prev_runs = entry.get("runs")
    runs = info["runs"]
    last_exit = info["last_exit"]
    entry.setdefault("fail_streak", 0)

    # R3: runs delta < 0 = counter reset (reload) -> episode reset, discard.
    if prev_runs is not None and runs < prev_runs:
        entry["runs"] = runs
        entry["last_exit"] = last_exit if info["has_exit_line"] else None
        _reset_episode(entry)
        return events

    # R3 (i): exit line absent = running right now -> hold, streak unchanged.
    if not info["has_exit_line"]:
        entry["runs"] = runs
        return events

    # R3 (ii): never exited -> nothing to account.
    if last_exit == NEVER_EXITED:
        entry["runs"] = runs
        entry["last_exit"] = NEVER_EXITED
        return events

    # Exit line present but unparseable -> hold (caller logged the anomaly).
    if last_exit is None:
        entry["runs"] = runs
        return events

    if last_exit == 0:
        entry["runs"] = runs
        entry["last_exit"] = 0
        _reset_episode(entry)  # recovery = episode end
        return events

    # Failure exit (positive or negative). Negative = signal death: general
    # streak, never structural (R3 iii).
    if prev_runs is None:
        # First-ever observation: baseline only, no increment ("runs increased
        # vs previous poll" needs a previous poll). No first-run false alarms.
        entry["runs"] = runs
        entry["last_exit"] = last_exit
        return events

    if runs > prev_runs:
        entry["fail_streak"] = entry.get("fail_streak", 0) + 1
        if not entry.get("episode_started_at"):
            entry["episode_started_at"] = now
        entry["runs"] = runs
        entry["last_exit"] = last_exit
    else:
        # No new run since last poll: same failure is NOT double-counted.
        entry["last_exit"] = last_exit
    structural = last_exit in STRUCTURAL_EXITS
    threshold = (STRUCTURAL_STREAK_NOTIFY if structural
                 else GENERAL_STREAK_NOTIFY)
    if (entry["fail_streak"] >= threshold and not entry.get("notified_at")
            and may_notify):
        # Marker set BEFORE the push attempt (cron-guard discipline: a
        # push failure must not re-notify next poll; /health still shows
        # the ongoing state).
        entry["notified_at"] = now
        events.append({
            "kind": "fail_streak",
            "label": label,
            "streak": entry["fail_streak"],
            "exit": last_exit,
            "structural": structural,
            "episode_started_at": entry["episode_started_at"],
        })
    return events


def update_resident(label, entry, info, list_pid, now, may_notify=True):
    """Keepalive resident service: judged on PID presence, not exit codes.
    DGN-1670: only the announcer (``may_notify``) marks and emits."""
    events = []
    pid = list_pid if list_pid is not None else info.get("pid")
    if pid is None:
        if not entry.get("down_since"):
            entry["down_since"] = now
        if not entry.get("down_notified_at") and may_notify:
            entry["down_notified_at"] = now
            events.append({
                "kind": "resident_down",
                "label": label,
                "down_since": entry["down_since"],
            })
    else:
        entry["down_since"] = None
        entry["down_notified_at"] = None
    entry["pid"] = pid
    entry["runs"] = info.get("runs")
    return events


def detect_losses(jobs, loaded_labels, now, is_mine=None, state=None,
                  restarting=False, update_hold=None):
    """R1 (DGN-1427 amendment): known-set minus loaded-set = lost labels.

    A single missing poll is NOT enough to notify -- a planned-restart gap
    can land inside one poll window (live case: 2m15s main-agent redeploy caught
    by exactly one poll, 2026-09-12). The label must still be missing
    LOST_GRACE_SECONDS after the FIRST miss before an unretired loss fires;
    a retired marker still suppresses at fire time, same as before.

    DGN-1745: ``is_mine(label)`` says whether THIS observer is the one that
    announces the label (see ``loss_is_mine``). A loss routed elsewhere is
    only recorded (``lost_at``) and left un-notified, so the recipient's own
    observer confirms it on its poll. None = no routing (every loss is ours).

    DGN-1900: ``state`` (the shared state dict) enables the mass-loss burst
    gate (``_burst_gate``); ``restarting`` = this observer's own instance
    restarted recently, so it never fires a burst.

    DGN-1990: ``update_hold(label, entry)`` returns the id of an update run
    that owns this loss (``update_run_hold``) -- held, not fired, and not
    counted toward a burst; the id is kept on the entry for /health.
    """
    events = []
    due = []
    for label, entry in jobs.items():
        if label in loaded_labels:
            continue
        if entry.get("lost_notified_at"):
            continue  # already fired for this episode
        if not entry.get("lost_at"):
            entry["lost_at"] = now  # first miss: start the grace window only
            continue
        if _seconds_between(entry["lost_at"], now) < LOST_GRACE_SECONDS:
            continue  # still inside grace -- could be a planned restart
        if entry.get("retired"):
            continue  # intentional removal -> suppressed, no event
        run_id = update_hold(label, entry) if update_hold else None
        if run_id:
            entry["update_hold"] = run_id
            continue  # stopped by an update run -> its result reports it
        entry.pop("update_hold", None)
        due.append(label)
    if state is not None and not _burst_gate(state, due, now, restarting):
        return events
    for label in due:
        entry = jobs[label]
        if is_mine is not None and not is_mine(label):
            continue  # another agent announces it; record silently
        entry["lost_notified_at"] = now
        events.append({
            "kind": "label_lost",
            "label": label,
            "last_seen_at": entry.get("last_seen_at"),
            "bucket": entry.get("bucket"),
        })
    return events


def _burst_gate(state, due, now, restarting):
    """DGN-1900: whether the confirmed losses ``due`` may fire this poll.

    Fewer than LOSS_BURST_MIN -> yes (and any old hold is dropped).  A burst
    -> held in ``state["loss_burst"]`` (shared, so every observer sees one
    hold) and fires only on a later poll at least
    LOSS_BURST_RECONFIRM_SECONDS after the hold, never from an observer
    whose own instance is restarting.  Labels that come back in between
    leave ``due`` on their own (reappearance clears lost_at).
    """
    if len(due) < LOSS_BURST_MIN:
        state.pop("loss_burst", None)
        return True
    hold = state.get("loss_burst")
    if not hold or not hold.get("held_at"):
        state["loss_burst"] = {"held_at": now, "count": len(due)}
        return False
    if restarting:
        return False
    return _seconds_between(hold["held_at"], now) >= LOSS_BURST_RECONFIRM_SECONDS


def _epoch(iso):
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def update_ledger_dir():
    """Same resolution as scripts/pack/update_surface.py ledger_dir()."""
    shared = os.environ.get(
        "DOGANY_SHARED_HOME", os.path.join(os.path.expanduser("~"), ".dogany"))
    return os.path.join(shared, "update-ledger")


def _ledger_roots(ledger):
    """Instance roots an apply run touches, or None when it names none."""
    roots = set()
    for item in ledger.get("items") or []:
        for key in ("member_roots", "pack_roots"):
            roots.update(r for r in item.get(key) or [] if isinstance(r, str))
        for line in item.get("lines") or []:
            if isinstance(line, str):
                roots.update(f for f in line.split("\t") if f.startswith("/"))
    result = ledger.get("result") or {}
    for member in result.get("stopped") or []:
        if isinstance(member, dict) and member.get("root"):
            roots.add(member["root"])
    for item in result.get("items") or []:
        for member in item.get("members") or []:
            if isinstance(member, dict) and member.get("root"):
                roots.add(member["root"])
    return {_real(r) for r in roots} or None


def _ledger_left_down(ledger):
    result = ledger.get("result") or {}
    down = set()
    record = result.get("record") or {}
    for member in record.get("bridges_left_down_members") or []:
        if isinstance(member, dict) and member.get("root"):
            down.add(_real(member["root"]))
    for item in result.get("items") or []:
        for member in item.get("members") or []:
            if (isinstance(member, dict) and member.get("root")
                    and member.get("status") == "left_down"):
                down.add(_real(member["root"]))
    return down


def read_update_runs(directory=None, now_epoch=None):
    """Recent update apply runs from the ledger dir. READ-ONLY.

    [{id, start, end (None = still running), roots (set, None = host-wide),
      left_down (set of roots), restarts {root: epoch}}].  A ledger that
    never started (no ``started_at`` and no ``.start`` claim) is no run.
    """
    directory = directory or update_ledger_dir()
    now_epoch = time.time() if now_epoch is None else now_epoch
    runs = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return runs
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            if now_epoch - os.path.getmtime(path) > UPDATE_LOOKBACK_SECONDS:
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                ledger = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(ledger, dict):
            continue
        start = ledger.get("started_at")
        if not isinstance(start, (int, float)):
            try:
                with open(path + ".start", encoding="utf-8") as fh:
                    start = float(fh.read().split()[1])
            except (OSError, ValueError, IndexError):
                continue
        end = (ledger.get("result") or {}).get("finished_at")
        if not isinstance(end, (int, float)):
            try:
                end = os.path.getmtime(path + ".result")
            except OSError:
                end = None
        restarts = {}
        for rec in ledger.get("restarts") or []:
            if (isinstance(rec, dict) and rec.get("root")
                    and isinstance(rec.get("at"), (int, float))):
                root = _real(rec["root"])
                restarts[root] = max(restarts.get(root, 0), rec["at"])
        runs.append({"id": ledger.get("id") or name[:-len(".json")],
                     "start": float(start),
                     "end": float(end) if end is not None else None,
                     "roots": _ledger_roots(ledger),
                     "left_down": _ledger_left_down(ledger),
                     "restarts": restarts})
    return runs


def update_run_hold(lost_at, owner_root, runs, now):
    """DGN-1990: id of the update run a loss belongs to and is still held
    by, else None.  ``lost_at`` / ``now`` are ISO; run times are epochs."""
    lost, now_e = _epoch(lost_at), _epoch(now)
    if lost is None or now_e is None:
        return None
    owner = _real(owner_root) if owner_root else None
    for run in runs or ():
        if run["roots"] is not None and owner not in run["roots"]:
            continue
        start, end = run["start"], run["end"]
        if end is None and now_e - start > UPDATE_OPEN_MAX_SECONDS:
            end = start + UPDATE_OPEN_MAX_SECONDS
        if lost < start - UPDATE_START_SLACK_SECONDS:
            continue  # gone before the run began: not the run's
        if end is not None and lost > end + UPDATE_SETTLE_SECONDS:
            continue  # gone after the run settled: not the run's
        if end is None:
            return run["id"]
        if owner is not None and owner in run["left_down"]:
            restarted = (run.get("restarts") or {}).get(owner)
            if restarted is None or restarted < end:
                return run["id"]
            if now_e < restarted + UPDATE_SETTLE_SECONDS:
                return run["id"]
            continue
        if now_e < end + UPDATE_SETTLE_SECONDS:
            return run["id"]
    return None


def own_restarting(jobs, loaded, self_bridges, now):
    """DGN-1900: this observer's own bridge restarted within
    OWN_RESTART_GRACE_SECONDS, or is not loaded right now."""
    for label in self_bridges or ():
        if label not in loaded:
            return True
        at = (jobs.get(label) or {}).get("restarted_at")
        if at and _seconds_between(at, now) < OWN_RESTART_GRACE_SECONDS:
            return True
    return False


def diff_disk_loaded(disk, loaded_labels, printinfo, jobs, launch_agents_dir):
    """R1-2 bidirectional diff. Returns warnings (state-file surface).

    PRINCIPLE: static disk state is NOT a signal -- only TRANSITIONS warn
    (label_lost / reboot_nonsurvivor / plist_divergence / fail streak /
    resident down). A plist sitting on disk is no evidence the job SHOULD
    run: installers lay down every template regardless of opt-in, successor
    rails (e.g. dawn-memory-queue running consolidate for all instances
    under its own label) retire jobs but leave plists behind, and no
    declared source of truth for "jobs this instance must run" exists --
    inferring one produces false alarms (coordinator correction 2026-08-21:
    all 59 live not_loaded entries were benign). Do NOT add successor-rail
    inference here; info-demotion makes inference unnecessary.

    - on disk, not loaded -> kind=not_loaded, level=INFO (record only for
      /health; never a warning, never a push; suppressed entirely by a
      retired marker).
    - loaded, but NO LaunchAgents plist carries this LABEL ->
      kind=reboot_nonsurvivor, level=warn. Judged by Label-KEY set membership
      over LaunchAgents plists -- NEVER by the loaded copy's path (a job
      legitimately bootstrapped from a repo copy while an equal LaunchAgents
      copy exists must not warn; live false positive: an instance bridge
      label, coordinator correction 2026-08-21).
    - loaded from a path that is not the label's LaunchAgents copy, while a
      LaunchAgents copy EXISTS -> kind=plist_divergence:
        * copies equivalent (normalized compare) -> level=info, record only;
        * copies differ (or unreadable)          -> level=warn ("a reboot
          will start this job with different configuration").
    """
    warnings = []
    la_real = os.path.realpath(launch_agents_dir)
    la_labels = {lbl for lbl, e in disk.items() if e.get("la_paths")}

    for label in sorted(loaded_labels):
        info = printinfo.get(label) or {}
        pp = info.get("plist_path")
        pp_in_la = bool(pp) and (
            os.path.realpath(os.path.dirname(pp)) == la_real)

        # Membership first; the loaded path is only a scan-failure backstop
        # (an unreadable LaunchAgents dir must not storm false nonsurvivors).
        if label not in la_labels and not pp_in_la:
            warnings.append({
                "kind": "reboot_nonsurvivor",
                "label": label,
                "level": "warn",
                "detail": pp or ",".join(disk.get(label, {}).get("paths", [])),
            })
            continue

        # LaunchAgents copy exists: divergence check when the loaded copy is
        # a different file than the label's LaunchAgents copy.
        la_paths = (disk.get(label) or {}).get("la_paths") or []
        if pp and la_paths and not pp_in_la:
            la_reals = {os.path.realpath(p) for p in la_paths}
            if os.path.realpath(pp) not in la_reals:
                eq = plists_equivalent(pp, la_paths[0])
                warnings.append({
                    "kind": "plist_divergence",
                    "label": label,
                    "level": "info" if eq is True else "warn",
                    "detail": "loaded=%s vs launchagents=%s (%s)" % (
                        pp, la_paths[0],
                        "identical content" if eq is True else
                        ("contents differ -- a reboot starts this job with "
                         "different configuration" if eq is False
                         else "comparison unavailable")),
                })

    for label in sorted(disk):
        if label in loaded_labels:
            continue
        if (jobs.get(label) or {}).get("retired"):
            continue
        # Static disk state is not a signal -- info-only (see docstring).
        warnings.append({
            "kind": "not_loaded",
            "label": label,
            "level": "info",
            "detail": ",".join(disk[label]["paths"]),
        })
    return warnings


# ---------------------------------------------------------------------------
# loss routing (DGN-1745): one announcer per lost label
# ---------------------------------------------------------------------------
#
# Every instance runs its own observer and every observer sees every label on
# the machine, so an unrouted loss was pushed by whichever observers happened
# to confirm it -- each through its own bot. A lost label is now announced
# only by the leader of the crew that owns it:
#   label -> owning instance (label slug -> newbridge --path root, R4)
#         -> owning crew (<crews home>/<kit>/crew.conf member= lines)
#         -> crew leader: the first member, in crew.conf order, whose
#            DOGANY_AGENT_CLASS is not "domain" (crew.conf has no leader
#            field; same representative rule as scripts/dogany crew_rows,
#            dogany-mac-optimize gate.sh and is_machine_steward above).
# Anything unresolvable -- unknown slug, instance in no crew, crew without a
# leader, or a leader whose own observer is not loaded (nobody would ever
# confirm it) -- goes to the machine steward, never to every agent and never
# dropped. DGN-1900: the steward is ONE root (explicit marker, else the
# first crew's representative; a PoC never), see machine_steward(); an
# owner in no crew is its own leader.

def crew_dir():
    """Crews home, same resolution as scripts/dogany / update_surface.py."""
    shared = os.environ.get(
        "DOGANY_SHARED_HOME", os.path.join(os.path.expanduser("~"), ".dogany"))
    return os.environ.get("DOGANY_CREW_DIR", os.path.join(shared, "crews"))


def read_crews(directory=None):
    """{kit: [member root, ...]} in crew.conf order; unreadable -> skipped."""
    directory = directory or crew_dir()
    crews = {}
    try:
        kits = sorted(os.listdir(directory))
    except OSError:
        return crews
    for kit in kits:
        try:
            with open(os.path.join(directory, kit, "crew.conf"),
                      encoding="utf-8", errors="replace") as fh:
                members = [line.strip()[len("member="):].strip()
                           for line in fh if line.strip().startswith("member=")]
        except OSError:
            continue
        if members:
            crews[kit] = members
    return crews


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


def crew_leader(members):
    """First existing member whose class is not domain, or None."""
    for member in members:
        if os.path.isdir(member) and _class_representative(member):
            return _real(member)
    return None


def loss_recipient(label, instance_roots, crews):
    """Leader root that announces ``label``'s loss, or None (= steward).

    DGN-1900: an owner in no crew is its own crew (same as update_surface
    offer_scope: a standalone agent is its own leader), so its losses go to
    it, not to the steward fallback -- unless it is a domain instance.
    """
    root = (instance_roots or {}).get(_instance_of(label).lower())
    if not root:
        return None
    owner = _real(root)
    for kit in sorted(crews or {}):
        if any(_real(m) == owner for m in crews[kit]):
            return crew_leader(crews[kit])
    if os.path.isdir(owner) and _class_representative(owner):
        return owner
    return None


def owner_chat(root):
    """The owner chat id push.sh would address for ``root`` (first
    ALLOWED_USER_IDS entry in .telegram_bot/.env, else owner.lock), or None.
    Read-only; only that one key is read from the .env file."""
    bot = os.path.join(root, ".telegram_bot")
    try:
        with open(os.path.join(bot, ".env"), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("ALLOWED_USER_IDS="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    first = value.split(",")[0].strip()
                    if first:
                        return first
    except OSError:
        pass
    try:
        with open(os.path.join(bot, "owner.lock"), encoding="utf-8",
                  errors="replace") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _same_owner(root_a, root_b, obs):
    """DGN-1670 privacy rule, fail-closed: True only when both owner chats
    are known and equal (or it is the same instance).  An unreadable owner
    is treated as a different owner, so its units are announced only by
    its own instance and it never announces anyone else's."""
    if root_a == root_b:
        return True
    memo = obs.setdefault("_owner_chats", {})
    for root in (root_a, root_b):
        if root not in memo:
            memo[root] = owner_chat(root)
    a, b = memo[root_a], memo[root_b]
    return bool(a and b and a == b)


def _observer_loaded(root, obs):
    roots = obs.get("instance_roots") or {}
    slugs = [s for s, r in roots.items() if r and _real(r) == root]
    loaded = {lbl.lower() for lbl in obs.get("loaded") or {}}
    return any((LABEL_PREFIX + s + ".health-observer").lower() in loaded
               for s in slugs)


def _stewards(obs):
    """Every root ``is_machine_steward`` can be true for: the one steward,
    or (no marker, no crew) each non-PoC representative instance."""
    crews = obs.get("crews") or {}
    steward = machine_steward(crews)
    if steward:
        return [steward]
    return sorted({_real(r) for r in (obs.get("instance_roots") or {}).values()
                   if r and os.path.isdir(r) and not is_poc_root(r)
                   and _class_representative(r)})


def loss_is_mine(label, obs):
    """Whether the observer described by obs["self_root"] announces label.

    Used for every per-unit alert (loss, failure streak, resident down).
    Route (DGN-1745/1900): the owning crew's leader when its observer is
    loaded, else the machine steward.  DGN-1670 privacy rule on top: an
    observer never announces a unit whose owning instance has a different
    owner chat -- live case 2026-10-09 23:46Z, a PoC handed to another user
    pushed a failure streak of the operator's unit into that user's chat.
    When the routed announcer is barred by that rule, the owning instance's
    own observer announces it; nobody else does.
    """
    self_root = _real(obs["self_root"])
    crews = obs.get("crews") or {}
    roots = obs.get("instance_roots") or {}
    owner_root = roots.get(_instance_of(label).lower())
    owner = _real(owner_root) if owner_root else None
    leader = loss_recipient(label, roots, crews)
    if not (leader and _observer_loaded(leader, obs)):
        leader = None
    if owner is None:
        # No owning instance: a machine-level unit, steward only.
        if leader:
            return leader == self_root
        return is_machine_steward(self_root, crews)
    if not _same_owner(self_root, owner, obs):
        return False
    if leader:
        if leader == self_root:
            return True
        barred = not _same_owner(leader, owner, obs)
    else:
        if is_machine_steward(self_root, crews):
            return True
        barred = not any(_same_owner(s, owner, obs) for s in _stewards(obs))
    return barred and self_root == owner


# ---------------------------------------------------------------------------
# poll orchestration (pure -- observation in, state + events out)
# ---------------------------------------------------------------------------

def poll(state, obs, now):
    """One poll cycle over a collected observation.

    obs = {
      loaded: {label: pid|None},          # launchctl list
      printinfo: {label: parsed-info},    # launchctl print per label
      print_errors: {label: msg},         # per-label collection failures
      disk: {label: {paths, in_launch_agents}},
      bridge_labels: set,
      launch_agents_dir: str,
      # DGN-1745 loss routing (optional; absent self_root = no routing):
      self_root: str,                     # this observer's instance root
      instance_roots: {slug_lower: root}, # from newbridge --path
      crews: {kit: [member root, ...]},   # crew.conf member= lines
      # DGN-1900 (optional): this instance's own bridge labels
      self_bridges: [label, ...],
      # DGN-1990 (optional): read_update_runs() -- update apply windows
      update_runs: [run, ...],
    }
    Returns (events, warnings). Mutates state in place.
    """
    jobs = state.setdefault("jobs", {})
    events = []
    parse_errors = []

    # DGN-1745 / DGN-1670: one announcer per unit, for EVERY per-unit alert
    # (loss, failure streak, resident down) -- never an observer of another
    # owner.  None = no routing (no self_root: every alert is ours).
    is_mine = None
    if obs.get("self_root"):
        def is_mine(label):
            return loss_is_mine(label, obs)

    for label, msg in sorted((obs.get("print_errors") or {}).items()):
        parse_errors.append({"label": label, "error": msg, "at": now})

    # DGN-1442: an unparseable plist = its label is missing from the disk
    # set = a silent coverage hole. Recorded per file so /health shows it.
    # DGN-1990: info-level plist notes (empty file, comment read around)
    # hide no unit -- they go to the info records below, not here.
    for e in obs.get("plist_errors") or []:
        if e.get("level") == "info":
            continue
        parse_errors.append({
            "label": e["path"],
            "error": "plist unparseable (unit not observed): %s" % e["error"],
            "at": now,
        })

    for label in sorted(obs["loaded"]):
        entry = jobs.setdefault(label, {"first_seen_at": now})
        # DGN-1900: a bridge whose pid changed (or that comes back from a
        # miss) restarted -- recorded on the shared entry so the owning
        # instance's observer sees it whoever polled first.
        if label in obs["bridge_labels"]:
            pid = obs["loaded"][label]
            if pid is not None and (entry.get("lost_at") or (
                    "bridge_pid" in entry and entry["bridge_pid"] != pid)):
                entry["restarted_at"] = now
            if pid is not None:
                entry["bridge_pid"] = pid
        entry["last_seen_at"] = now
        # Reappearance clears loss markers (a later disappearance is a NEW
        # loss event) and any stale retired marker (the job is back).
        entry["lost_at"] = None
        entry["lost_notified_at"] = None
        entry.pop("update_hold", None)
        if entry.get("retired"):
            entry.pop("retired", None)

        info = obs["printinfo"].get(label)
        if info is None:
            continue  # collection failed; already in parse_errors
        if not info.get("parse_ok"):
            parse_errors.append({
                "label": label,
                "error": "launchctl print output not parseable (no runs field)",
                "at": now,
            })
            continue
        if info["has_exit_line"] and info["last_exit"] is None:
            parse_errors.append({
                "label": label,
                "error": "last exit code value not parseable",
                "at": now,
            })

        bucket = classify_job(label, info, obs["bridge_labels"])
        entry["bucket"] = bucket
        if bucket == BUCKET_SCHEDULED:
            events.extend(update_scheduled(label, entry, info, now,
                                           is_mine is None or is_mine(label)))
        elif bucket == BUCKET_RESIDENT:
            events.extend(update_resident(
                label, entry, info, obs["loaded"][label], now,
                is_mine is None or is_mine(label)))
        else:
            # bridge: axis-B territory, no exit accounting here.
            # waiting: future one-shot, nothing to account yet.
            entry["runs"] = info.get("runs")
            entry["last_exit"] = (info.get("last_exit")
                                  if info.get("has_exit_line") else None)

    restarting = own_restarting(jobs, obs["loaded"],
                                obs.get("self_bridges"), now)
    update_hold = None
    if obs.get("update_runs"):
        roots = obs.get("instance_roots") or {}

        def update_hold(label, entry):
            return update_run_hold(entry.get("lost_at"),
                                   roots.get(_instance_of(label).lower()),
                                   obs["update_runs"], now)
    events.extend(detect_losses(jobs, set(obs["loaded"]), now, is_mine,
                                state=state, restarting=restarting,
                                update_hold=update_hold))

    warnings = diff_disk_loaded(
        obs["disk"], set(obs["loaded"]), obs["printinfo"], jobs,
        obs["launch_agents_dir"])
    for e in obs.get("plist_errors") or []:
        if e.get("level") == "info":
            warnings.append({"kind": "plist_note", "label": e["path"],
                             "level": "info", "detail": e["error"]})

    # Preserve first_seen_at across polls for persisting warnings.
    prev = {(w.get("kind"), w.get("label")): w
            for w in state.get("warnings", [])}
    for w in warnings:
        old = prev.get((w["kind"], w["label"]))
        w["first_seen_at"] = old.get("first_seen_at", now) if old else now

    # Dynamic counts only -- constants are forbidden (R2).
    counts = {}
    for label, entry in jobs.items():
        if entry.get("lost_at"):
            counts["lost"] = counts.get("lost", 0) + 1
        else:
            b = entry.get("bucket") or "unknown"
            counts[b] = counts.get(b, 0) + 1

    state["schema"] = STATE_SCHEMA
    state["heartbeat_at"] = now
    state["warnings"] = warnings
    state["parse_errors"] = parse_errors
    state["counts"] = counts
    return events, warnings


# ---------------------------------------------------------------------------
# notification copy (Korean locale copy per DGN-986 section 7(ga) template;
# the loss / resident-down variants follow the same approved skeleton and are
# flagged UX-gate-pending in the handoff report, dec-094)
# ---------------------------------------------------------------------------

def _friendly(label):
    """Owner copy shares cron-guard's scheduled-job vocabulary.

    Unknown labels intentionally resolve to a generic phrase.  Every caller
    puts the raw label in its folded technical detail, so an internal slug
    never leaks into owner-facing alarm copy.
    """
    return display_name(label=label)


def _instance_of(label):
    rest = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else label
    return rest.split(".")[0]


def _investigate_desc(label, self_slug):
    # Lane conformance (spec section 2): a domain-live job's fix path runs
    # through the domain lane / owner conversation, not this instance.
    if self_slug and _instance_of(label).lower() != str(self_slug).lower():
        return "확인 후 담당 에이전트를 통해 조치 경로까지 보고드립니다."
    return "로그를 확인해 원인과 수리안을 보고드립니다."


def _exit_note(code):
    notes = {
        127: " (실행할 스크립트 부재 -- 자연치유 불가 유형)",
        126: " (실행 권한 문제 -- 자연치유 불가 유형)",
        78: " (설정 오류 -- 자연치유 불가 유형)",
    }
    if isinstance(code, int) and code < 0:
        return " (시그널 종료)"
    return notes.get(code, "")


def compose_fail_streak(ev, self_slug):
    """(가) approved copy skeleton. Returns (text, button_spec)."""
    label = ev["label"]
    friendly = _friendly(label)
    cta1 = "%s 작업 실패 원인 봐줘" % friendly
    cta2 = "%s 작업 없애줘" % friendly
    text = (
        "⚠️ 스케줄 작업 하나가 계속 실패하고 있어요.\n"
        "**%s** 작업이 %s번 연속 실패했습니다.\n\n"
        "다음 행동 -- 버튼을 누르시거나 그대로 말씀해 주세요:\n"
        "· \"%s\" (추천) -- %s\n"
        "· \"%s\" -- 더 필요 없는 작업이면 스케줄에서 내립니다.\n"
        ">! 기술 상세\n"
        "> 잡 라벨: %s\n"
        "> 종료코드: %s%s\n"
        "> 실패 이력: 연속 %s회, 최초 실패 %s\n"
        "> 이 건 알림은 이번 1회 -- 지속 상태는 /health 에 상시 표시됩니다"
    ) % (
        friendly, ev["streak"], cta1, _investigate_desc(label, self_slug),
        cta2, label, ev["exit"], _exit_note(ev["exit"]), ev["streak"],
        ev.get("episode_started_at") or "?",
    )
    return text, "%s::opt:1" % cta1


def compose_label_lost(events, self_slug):
    """R1 loss event copy. Aggregates a multi-loss storm into ONE push."""
    if len(events) == 1:
        ev = events[0]
        label = ev["label"]
        friendly = _friendly(label)
        cta1 = "%s 작업 왜 사라졌는지 봐줘" % friendly
        cta2 = "%s 작업은 일부러 내린 거야" % friendly
        text = (
            "⚠️ 스케줄 작업 하나가 스케줄에서 사라졌어요.\n"
            "**%s** 작업이 더는 등록돼 있지 않습니다(은퇴 기록 없음). "
            "의도한 정리가 아니면 이 작업은 지금 조용히 멈춘 상태예요.\n\n"
            "다음 행동 -- 버튼을 누르시거나 그대로 말씀해 주세요:\n"
            "· \"%s\" (추천) -- %s\n"
            "· \"%s\" -- 의도적 제거로 기록하고 다시 알리지 않습니다.\n"
            ">! 기술 상세\n"
            "> 잡 라벨: %s\n"
            "> 마지막 관측: %s\n"
            "> 이 건 알림은 이번 1회 -- 지속 상태는 /health 에 상시 표시됩니다"
        ) % (friendly, cta1, _investigate_desc(label, self_slug), cta2,
             label, ev.get("last_seen_at") or "?")
        return text, "%s::opt:1" % cta1

    names = " · ".join(_friendly(e["label"]) for e in events)
    cta1 = "사라진 스케줄 작업들 봐줘"
    lines = "\n".join("> %s" % e["label"] for e in events)
    text = (
        "⚠️ 스케줄 작업 %s개가 한꺼번에 스케줄에서 사라졌어요.\n"
        "%s -- 전부 은퇴 기록 없이 내려가 있습니다. "
        "의도한 정리가 아니면 지금 조용히 멈춘 상태예요.\n\n"
        "다음 행동 -- 버튼을 누르시거나 그대로 말씀해 주세요:\n"
        "· \"%s\" (추천) -- 각각의 원인과 복구/은퇴 판정을 보고드립니다.\n"
        ">! 기술 상세\n%s\n"
        "> 이 건 알림은 이번 1회 -- 지속 상태는 /health 에 상시 표시됩니다"
    ) % (len(events), names, cta1, lines)
    return text, "%s::opt:1" % cta1


def compose_resident_down(ev, self_slug):
    label = ev["label"]
    friendly = _friendly(label)
    cta1 = "%s 서비스 상태 봐줘" % friendly
    text = (
        "⚠️ 상주 서비스 하나가 내려가 있어요.\n"
        "**%s** 서비스는 항상 떠 있어야 하는데 지금 프로세스가 없습니다.\n\n"
        "다음 행동 -- 버튼을 누르시거나 그대로 말씀해 주세요:\n"
        "· \"%s\" (추천) -- %s\n"
        ">! 기술 상세\n"
        "> 잡 라벨: %s\n"
        "> 다운 관측: %s\n"
        "> 이 건 알림은 이번 1회 -- 지속 상태는 /health 에 상시 표시됩니다"
    ) % (friendly, cta1, _investigate_desc(label, self_slug), label,
         ev.get("down_since") or "?")
    return text, "%s::opt:1" % cta1


def compose_notifications(events, self_slug):
    """Events -> [(text, button_spec)] with loss-storm aggregation."""
    out = []
    losses = [e for e in events if e["kind"] == "label_lost"]
    for ev in events:
        if ev["kind"] == "fail_streak":
            out.append(compose_fail_streak(ev, self_slug))
        elif ev["kind"] == "resident_down":
            out.append(compose_resident_down(ev, self_slug))
    if losses:
        out.append(compose_label_lost(losses, self_slug))
    return out


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

def load_state():
    """Load jobs.json; quarantine a corrupt file instead of dying silently."""
    path = state_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
        return data, None
    except FileNotFoundError:
        return {}, None
    except Exception as exc:
        quarantine = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(path, quarantine)
        except OSError:
            quarantine = None
        return {}, {
            "kind": "state_corrupt_recovered",
            "label": "-",
            "detail": "jobs.json unreadable (%s); quarantined to %s"
                      % (exc.__class__.__name__, quarantine or "n/a"),
        }


def save_state(state):
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".jobs.json.tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, state_path())


# ---------------------------------------------------------------------------
# DGN-1713 machine metrics (shared host state; alert only)
# ---------------------------------------------------------------------------

def parse_key_values(text):
    """``loadcheck.sh preflight`` output -> plain key/value mapping."""
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _number(values, key):
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return None


def collect_machine_readings(root=None):
    """Read the same four preflight values as loadcheck.sh.

    On macOS prefer the DGN-1712 carrier when it is installed.  Linux has no
    launchd job observation, but its inexpensive standard readings let this
    alert-only path remain useful there too.  Unknown values are omitted,
    never guessed.
    """
    root = root or _instance_root()
    candidates = [
        os.path.join(root, ".claude", "skills", "dogany-mac-load-check",
                     "loadcheck.sh"),
        os.path.join(root, "skills", "dogany-mac-load-check", "loadcheck.sh"),
    ]
    if sys.platform == "darwin":
        for script in candidates:
            if os.path.isfile(script) and os.access(script, os.X_OK):
                try:
                    proc = _run(["/bin/bash", script, "preflight"], timeout=20)
                    if proc.returncode == 0:
                        return parse_key_values(proc.stdout)
                except Exception:
                    pass

    values = {"platform": sys.platform, "ncpu": str(os.cpu_count() or "")}
    try:
        values["load5"] = str(os.getloadavg()[1])
    except (AttributeError, OSError):
        pass
    try:
        usage = shutil.disk_usage("/System/Volumes/Data" if
                                  os.path.isdir("/System/Volumes/Data") else "/")
        if usage.total:
            values["disk_used_pct"] = str(int(usage.used * 100 / usage.total))
    except OSError:
        pass
    # Cheap Linux best effort. macOS reaches this only if its carrier is not
    # installed, where swap remains unknown instead of using a made-up value.
    if sys.platform.startswith("linux"):
        try:
            mem = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, value = line.split(":", 1)
                    mem[key] = int(value.split()[0])
            values["swap_used_mb"] = str(max(
                0, (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024))
        except (OSError, ValueError, IndexError):
            pass
    return values


def machine_thresholds(root=None):
    return {
        "swap_mb": _conf_number("HEALTH_OBSERVER_SWAP_WARN_MB",
                                 DEFAULT_SWAP_WARN_MB, root),
        "load_multiplier": _conf_number("HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER",
                                         DEFAULT_LOAD_WARN_MULTIPLIER, root),
        "load_seconds": int(_conf_number("HEALTH_OBSERVER_LOAD_WARN_SECONDS",
                                           DEFAULT_LOAD_WARN_SECONDS, root)),
        "disk_pct": _conf_number("HEALTH_OBSERVER_DISK_WARN_PCT",
                                  DEFAULT_DISK_WARN_PCT, root),
        "disk_step_pct": _conf_positive("HEALTH_OBSERVER_DISK_STEP_PCT",
                                        DEFAULT_DISK_STEP_PCT, root),
        "swap_step_mb": _conf_positive("HEALTH_OBSERVER_SWAP_STEP_MB",
                                       DEFAULT_SWAP_STEP_MB, root),
        "remind_seconds": int(_conf_positive(
            "HEALTH_OBSERVER_MACHINE_REMIND_SECONDS",
            DEFAULT_MACHINE_REMIND_SECONDS, root)),
        # DGN-1879: 0 is a valid setting here (no band / no re-arm memory).
        "disk_clear_margin_pct": _conf_number(
            "HEALTH_OBSERVER_DISK_CLEAR_MARGIN_PCT",
            DEFAULT_DISK_CLEAR_MARGIN_PCT, root),
        "swap_clear_margin_mb": _conf_number(
            "HEALTH_OBSERVER_SWAP_CLEAR_MARGIN_MB",
            DEFAULT_SWAP_CLEAR_MARGIN_MB, root),
        "load_clear_margin": _conf_number(
            "HEALTH_OBSERVER_LOAD_CLEAR_MARGIN",
            DEFAULT_LOAD_CLEAR_MARGIN, root),
        "rearm_seconds": int(_conf_number(
            "HEALTH_OBSERVER_MACHINE_REARM_SECONDS",
            DEFAULT_MACHINE_REARM_SECONDS, root)),
    }


# ---------------------------------------------------------------------------
# DGN-1911 machine-alert on/off switch
# ---------------------------------------------------------------------------
# Thresholds stay thresholds.  "Turn the machine alerts off" is this switch,
# never a threshold raised to an unreachable value (the 2026-10-07 interim
# hack, which this module detects and migrates).  Default on; an unknown
# value also reads as on (fail-open: alerts are the safe default).  The
# optional off-until is a time-boxed mute: ISO 8601 date or datetime, naive
# values are local time, a bare date means 00:00 local on that day.
MACHINE_ALERTS_KEY = "HEALTH_OBSERVER_MACHINE_ALERTS"
MACHINE_ALERTS_UNTIL_KEY = "HEALTH_OBSERVER_MACHINE_ALERTS_OFF_UNTIL"
_SWITCH_OFF = ("off", "no", "false", "0", "disabled")
# A threshold at or above these can never fire on a real machine: disk use
# cannot exceed 100%, 1 TiB of swap, load at 100x the core count, a load
# sustain window of a year.  Only these are migrated; any lower custom
# threshold is the owner's real tuning and is left alone.
LEGACY_MUTE_LIMITS = {
    "HEALTH_OBSERVER_DISK_WARN_PCT": 100,
    "HEALTH_OBSERVER_SWAP_WARN_MB": 1024 * 1024,
    "HEALTH_OBSERVER_LOAD_WARN_MULTIPLIER": 100,
    "HEALTH_OBSERVER_LOAD_WARN_SECONDS": 365 * 24 * 60 * 60,
}
LEGACY_MUTE_TAG = "# DGN-1911 retired (unreachable threshold; use %s):" \
    % MACHINE_ALERTS_KEY


def _conf_path(root=None):
    return os.path.join(root or _instance_root(), "config", "agent.conf")


def parse_until(text):
    """Off-until value -> epoch seconds, or None when empty/unparseable."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            day = datetime.date.fromisoformat(text)
            moment = datetime.datetime(day.year, day.month, day.day)
        else:
            moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return time.mktime(moment.timetuple())
    return moment.timestamp()


def machine_alerts_mode(root=None, now_epoch=None):
    """Read the switch: {"on": bool, "switch": raw, "until": raw, ...}.

    Off when the switch says off (until the off-until moment, when one is
    set and parses), or when only an off-until in the future is set.  An
    off-until that cannot be parsed leaves an explicit off in force (the
    owner asked for quiet) and is ignored otherwise.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    switch = _conf_value(MACHINE_ALERTS_KEY, root).lower()
    until_raw = _conf_value(MACHINE_ALERTS_UNTIL_KEY, root)
    until = parse_until(until_raw)
    if switch in _SWITCH_OFF:
        on = until is not None and now_epoch >= until
    else:
        on = not (until is not None and now_epoch < until)
    return {"on": on, "switch": switch or "on(default)", "until": until_raw,
            "until_epoch": until,
            "legacy_mute": legacy_mute_lines(root)}


def legacy_mute_lines(root=None):
    """Active agent.conf lines that mute alerts by an unreachable threshold.

    Returns [(index, key, value)] over the file's lines (0-based index).
    """
    found = []
    try:
        with open(_conf_path(root), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return found
    for i, line in enumerate(lines):
        text = line.strip()
        for key, limit in LEGACY_MUTE_LIMITS.items():
            if not text.startswith(key + "="):
                continue
            raw = text.split("=", 1)[1].strip().strip('"').strip("'")
            try:
                if float(raw) >= limit:
                    found.append((i, key, raw))
            except ValueError:
                pass
    return found


def set_machine_alerts(on, root=None, until=None, stamp=None):
    """Write the switch into agent.conf (the instance's settings file).

    Also migrates the legacy unreachable-threshold lines (commented out
    with a tag, never deleted) so the switch is the one thing that decides.
    A backup of the previous file is kept next to it when anything changes.
    Returns {"changed": bool, "migrated": [...], "backup": path or None}.
    """
    path = _conf_path(root)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            original = fh.read()
    except FileNotFoundError:
        original = ""
    lines = original.splitlines()
    migrated = []
    for i, key, value in legacy_mute_lines(root):
        migrated.append("%s=%s" % (key, value))
        lines[i] = "%s %s" % (LEGACY_MUTE_TAG, lines[i].strip())

    def upsert(key, value):
        for i, line in enumerate(lines):
            if line.strip().startswith(key + "="):
                if value is None:
                    lines.pop(i)
                else:
                    lines[i] = "%s=%s" % (key, value)
                return
        if value is not None:
            if not any(MACHINE_ALERTS_KEY in ln for ln in lines):
                lines.append("# Machine-health alerts on/off (DGN-1911).")
            lines.append("%s=%s" % (key, value))

    upsert(MACHINE_ALERTS_KEY, "on" if on else "off")
    upsert(MACHINE_ALERTS_UNTIL_KEY, None if on or not until else until)
    text = "\n".join(lines) + "\n"
    if text == original:
        return {"changed": False, "migrated": migrated, "backup": None}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = None
    if original:
        stamp = stamp or time.strftime("%Y%m%d%H%M%S")
        backup = "%s.bak-dgn1911-%s" % (path, stamp)
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write(original)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return {"changed": True, "migrated": migrated, "backup": backup}


def poll_machine_metrics(state, readings, now_epoch, thresholds):
    """Update shared condition episodes and return newly-notifiable facts.

    A healthy reading clears an episode silently.  While unhealthy, each
    condition alerts once (``notice: breach``), then again only when its
    reading has worsened by its step since the last notice (``worsened``;
    disk and swap only) or the reminder interval has passed since the last
    notice (``reminder``) -- regardless of how many observer instances run.
    Load alone must first stay over its limit for its configured 15-minute
    sustain window.  (DGN-1773: static state is not a signal.)

    DGN-1879 hysteresis: once notified, an episode survives readings in the
    band above its clear level (threshold minus margin) up to its threshold
    -- held silently, no notice -- and clears only at or below that level.
    A zero margin is the plain threshold.
    A not-yet-notified episode (load still inside its sustain window) keeps
    the plain threshold, so the sustain rule still means "stayed over".  A
    cleared notified episode is remembered for the re-arm window; a breach
    within it resumes that episode rather than announcing a new one.
    """
    conditions = state.setdefault("conditions", {})
    cleared = state.setdefault("cleared", {})
    swap = _number(readings, "swap_used_mb")
    load5, ncpu = _number(readings, "load5"), _number(readings, "ncpu")
    disk = _number(readings, "disk_used_pct")
    load_ok = load5 is not None and ncpu is not None and ncpu > 0
    load_limit = None if ncpu is None else ncpu * thresholds["load_multiplier"]
    load_clear = None if ncpu is None else ncpu * max(
        0, thresholds["load_multiplier"] - thresholds["load_clear_margin"])
    swap_clear = thresholds["swap_mb"] - thresholds["swap_clear_margin_mb"]
    disk_clear = thresholds["disk_pct"] - thresholds["disk_clear_margin_pct"]
    # kind -> (unhealthy, held, sustain seconds, worsening value, step, facts)
    # held = at/under the threshold but still above the clear level.
    checks = {
        "swap": (swap is not None and swap > thresholds["swap_mb"],
                 swap is not None and swap > swap_clear, 0,
                 swap, thresholds["swap_step_mb"],
                 {"swap_used_mb": swap, "threshold_mb": thresholds["swap_mb"]}),
        "load": (load_ok and load5 > load_limit,
                 load_ok and load5 > load_clear,
                 thresholds["load_seconds"], None, None,
                 {"load5": load5, "ncpu": ncpu, "threshold": load_limit}),
        "disk": (disk is not None and disk > thresholds["disk_pct"],
                 disk is not None and disk > disk_clear, 0,
                 disk, thresholds["disk_step_pct"],
                 {"disk_used_pct": disk, "threshold_pct": thresholds["disk_pct"]}),
    }
    events = []
    for kind, (unhealthy, held, wait_seconds, value, step, facts) in checks.items():
        memory = cleared.get(kind)
        if memory and now_epoch - memory.get("cleared_at", 0) >= \
                thresholds["rearm_seconds"]:
            cleared.pop(kind, None)
            memory = None
        entry = conditions.get(kind)
        if not unhealthy:
            if entry and held and entry.get("notified_at") is not None:
                entry["last_seen_at"] = now_epoch
                entry["facts"] = facts
                continue  # inside the band: the episode is still open
            if entry and entry.get("notified_at") is not None:
                cleared[kind] = {
                    k: entry[k] for k in
                    ("breach_started_at", "notified_at", "notified_value")
                    if k in entry}
                cleared[kind]["cleared_at"] = now_epoch
            conditions.pop(kind, None)
            continue
        if entry is None:
            entry = conditions[kind] = {}
            if memory:  # re-crossed within the re-arm window: same episode
                entry.update({k: v for k, v in memory.items()
                              if k != "cleared_at"})
                cleared.pop(kind, None)
        started = entry.setdefault("breach_started_at", now_epoch)
        entry["last_seen_at"] = now_epoch
        entry["facts"] = facts
        if now_epoch - started < wait_seconds:
            continue
        last_notice = entry.get("notified_at")
        last_value = entry.get("notified_value")
        if last_notice is None:
            notice = "breach"
        elif (step is not None and value is not None and
              last_value is not None and value >= last_value + step):
            notice = "worsened"
        elif now_epoch - last_notice >= thresholds["remind_seconds"]:
            notice = "reminder"
        else:
            continue
        # Recorded before the inbox drop: no storms.
        entry["notified_at"] = now_epoch
        entry["notified_value"] = value
        event = {"kind": kind, "notice": notice, "breach_started_at": started}
        if notice == "worsened":
            event["last_notice_value"] = last_value
        events.append(dict(event, **facts))
    state["updated_at"] = now_epoch
    return events


def load_machine_state():
    try:
        with open(machine_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_machine_state(state):
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".machine.json.tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, machine_state_path())


def write_machine_inbox(events, root=None):
    """Give the owning agent facts; it, not this routine, addresses owner."""
    root = root or _instance_root()
    inbox = os.path.join(root, ".telegram_bot", "session-inbox")
    lines = ["[machine-health] machine metrics need owner-facing judgment.", ""]
    for event in events:
        lines.append("- %s: %s" % (event["kind"], json.dumps(
            {k: v for k, v in event.items() if k != "kind"},
            ensure_ascii=False, sort_keys=True)))
    lines += ["", "Agent instruction: assess these machine-wide readings and "
              "speak to the owner in your own words. Alert only; do not kill "
              "processes or delete data.",
              "If the owner asks to stop these alerts, use the switch "
              "(DGN-1911): `python3 routines/health-observer.py "
              "--machine-alerts off [--until YYYY-MM-DD]`. Never raise a "
              "threshold to silence them. Tell the owner they are off and "
              "that asking you turns them back on."]
    os.makedirs(inbox, exist_ok=True)
    name = "machine-health-%d-%d.md" % (int(time.time() * 1000), os.getpid())
    tmp = os.path.join(inbox, ".%s.tmp" % name)
    with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(inbox, name))


def write_legacy_mute_note(lines_found, root=None):
    """One agent note: this instance mutes alerts by unreachable thresholds
    (the pre-switch workaround).  The agent migrates; the owner's choice
    (alerts off) is kept, so no owner question is needed."""
    root = root or _instance_root()
    inbox = os.path.join(root, ".telegram_bot", "session-inbox")
    body = ["[machine-health] machine alerts are muted by unreachable "
            "thresholds in config/agent.conf (DGN-1911 migration):", ""]
    body += ["- %s=%s" % (key, value) for _, key, value in lines_found]
    body += ["", "Agent instruction: these lines were a workaround for a "
             "missing on/off switch. Run `python3 routines/health-observer.py "
             "--machine-alerts off` once; it keeps alerts off, retires these "
             "lines (commented, backup kept) and sets the real switch. No "
             "owner message is needed unless the owner asks."]
    os.makedirs(inbox, exist_ok=True)
    name = "machine-health-legacy-%d-%d.md" % (int(time.time() * 1000),
                                               os.getpid())
    tmp = os.path.join(inbox, ".%s.tmp" % name)
    with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(body) + "\n")
    os.replace(tmp, os.path.join(inbox, name))


def observe_machine_metrics(root=None, readings=None, now_epoch=None):
    """Serialized shared-state poll. Returns new events; never sends a push."""
    import fcntl
    root = root or _instance_root()
    if not is_machine_steward(root):
        return []
    now_epoch = time.time() if now_epoch is None else now_epoch
    mode = machine_alerts_mode(root, now_epoch)
    os.makedirs(state_dir(), exist_ok=True)
    lock_path = os.path.join(state_dir(), ".machine.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_machine_state()
        # DGN-1911: an instance still muted by the legacy threshold hack
        # gets one note telling its agent to move to the switch.
        legacy_note = False
        if mode["legacy_mute"]:
            noted = state.setdefault("legacy_mute_noted", {})
            if root not in noted:
                noted[root] = now_epoch
                legacy_note = True
        else:
            state.get("legacy_mute_noted", {}).pop(root, None)
        events = []
        # Switched off: no reading, no episode bookkeeping.  An episode
        # still open when alerts come back on resumes its own rules.
        if mode["on"]:
            events = poll_machine_metrics(
                state, readings if readings is not None
                else collect_machine_readings(root),
                now_epoch, machine_thresholds(root))
        save_machine_state(state)
    if legacy_note:
        write_legacy_mute_note(mode["legacy_mute"], root)
    if events:
        write_machine_inbox(events, root)
    return events


# ---------------------------------------------------------------------------
# DGN-1582 r3 (dec-236): dawn memory queue nights that never ran at all
# ---------------------------------------------------------------------------
# A night on which the dawn memory queue never started (Mac off at 04:00) is
# invisible to every other rail: no run -> no exit code, no cron-guard, no
# launchd record, and a Mac shut down at 04:00 gets no catch-up run on boot.
# The queue already leaves one log per night it starts
# (~/.dogany/logs/dawn-memory-queue/queue-YYYYMMDD.log, plus
# instance-<slug>-YYYYMMDD.log), so this poll reads those: a due night with
# neither is a never-ran night. Two consecutive ones -> one owner notice and
# one operator notice per streak; any night that ran ends the streak.
# Machine-level fact, so machine-level state + the steward gate (one notice
# per machine, however many observers poll).
DAWN_HOUR = 4
# Night D is judged once D 04:30 local has passed (the queue's own log is
# written within seconds of its 04:00 start).
DAWN_DUE_SECONDS = 30 * 60
# The notifiable condition must hold this long across polls: a Mac waking
# from sleep starts the missed queue run and this poll together, and the
# queue must win that race before a night is called missed.
DAWN_CONFIRM_SECONDS = 600
DAWN_NOTIFY_NIGHTS = 2
DAWN_LOOKBACK_NIGHTS = 14


def dawn_paths():
    home = os.path.expanduser("~")
    return {
        "registry": os.environ.get("DOGANY_DMQ_REGISTRY_DIR") or os.path.join(
            home, ".dogany", "memory-queue", "instances.d"),
        "logs": os.environ.get("DOGANY_DMQ_LOG_DIR") or os.path.join(
            home, ".dogany", "logs", "dawn-memory-queue"),
        "lock": os.environ.get("DOGANY_DMQ_LOCK_DIR") or os.path.join(
            home, ".dogany", "cron-queue", "dmq-runner", "runner.lock"),
    }


def dawn_queue_since(registry_dir):
    """Earliest registration (conf mtime) among ENABLED queue instances, or
    None when nothing is registered -- nights before it are never judged."""
    since = None
    try:
        names = sorted(os.listdir(registry_dir))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".conf"):
            continue
        path = os.path.join(registry_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                enabled = [ln.split("=", 1)[1].strip() for ln in fh
                           if ln.startswith("ENABLED=")]
            if enabled and enabled[-1] != "1":
                continue
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        since = mtime if since is None else min(since, mtime)
    return since


def _dawn_ran(log_dir, day):
    stamp = day.strftime("%Y%m%d")
    if os.path.exists(os.path.join(log_dir, "queue-%s.log" % stamp)):
        return True
    return bool(glob.glob(os.path.join(log_dir, "instance-*-%s.log" % stamp)))


def missed_dawn_nights(now_epoch, since_epoch, log_dir):
    """Consecutive due nights with no queue run, newest first ("YYYYMMDD").
    Stops at the first night that ran, at the queue's registration, and at
    DAWN_LOOKBACK_NIGHTS."""
    today = datetime.date.fromtimestamp(now_epoch)
    missed = []
    for back in range(DAWN_LOOKBACK_NIGHTS + 1):
        day = today - datetime.timedelta(days=back)
        start = time.mktime((day.year, day.month, day.day, DAWN_HOUR, 0, 0,
                             0, 0, -1))
        if start + DAWN_DUE_SECONDS > now_epoch:
            continue
        if start < since_epoch or _dawn_ran(log_dir, day):
            break
        missed.append(day.strftime("%Y%m%d"))
        if len(missed) >= DAWN_LOOKBACK_NIGHTS:
            break
    return missed


def poll_dawn_runs(state, missed, now_epoch, running=False):
    """Advance the never-ran streak; return an event when it first becomes
    notifiable (>= DAWN_NOTIFY_NIGHTS, confirmed), else None. Once per
    streak: a night that ran clears the state."""
    if not missed:
        state.clear()
        return None
    oldest = missed[-1]
    if (state.get("streak_first") and oldest > state["streak_first"]
            and len(missed) < DAWN_LOOKBACK_NIGHTS):
        state.clear()          # a run happened between the two streaks
    state.setdefault("streak_first", oldest)
    state["nights"] = len(missed)
    if len(missed) < DAWN_NOTIFY_NIGHTS or state.get("notified_at"):
        return None
    if running:
        state.pop("pending_since", None)
        return None
    pending = state.setdefault("pending_since", now_epoch)
    if now_epoch - pending < DAWN_CONFIRM_SECONDS:
        return None
    state["notified_at"] = now_epoch
    return {"kind": "dawn_never_ran", "nights": len(missed),
            "first_night": state["streak_first"]}


def dawn_state_path():
    return os.path.join(state_dir(), "dawn.json")


def observe_dawn_runs(root=None, now_epoch=None, paths=None):
    """Serialized shared-state poll of never-ran nights. Steward-gated; a
    machine with no enabled queue instance, or whose queue never logged a
    single run (not installed), is never judged. Returns new events."""
    import fcntl
    root = root or _instance_root()
    if not is_machine_steward(root):
        return []
    paths = paths or dawn_paths()
    since = dawn_queue_since(paths["registry"])
    if since is None:
        return []
    try:
        if not any(n.startswith("queue-") for n in os.listdir(paths["logs"])):
            return []
    except OSError:
        return []
    now_epoch = time.time() if now_epoch is None else now_epoch
    missed = missed_dawn_nights(now_epoch, since, paths["logs"])
    running = os.path.isdir(paths["lock"])
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), ".dawn.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(dawn_state_path(), encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        event = poll_dawn_runs(state, missed, now_epoch, running)
        # Saved before any push: the once-per-streak marker survives a
        # failed send (episode-once contract).
        tmp = dawn_state_path() + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, dawn_state_path())
    return [event] if event else []


def compose_dawn_never_ran(root=None):
    """Owner-confirmed copy (dec-236), from config/i18n by AGENT_LANG."""
    root = root or _instance_root()
    text = ""
    try:
        from i18n_lookup import lookup
        text = lookup(root, _conf_value("AGENT_LANG", root) or "en",
                      "health.dawn_never_ran_body")
    except Exception:
        pass
    return text or ("Your Mac was off before dawn, so memory cleanup has not "
                    "run for two nights now. Leave it on around 4 a.m. and "
                    "cleanup will resume the next day.")


def notify_dawn_never_ran(events, root=None, dry_run=False, sender=None):
    """One owner notice + one operator notice per event (dec-236). The
    operator leg never falls back to the owner (no double owner message)."""
    sender = sender or send_push
    for _ev in events:
        text = compose_dawn_never_ran(root)
        for audience in ("owner", "operator"):
            if dry_run:
                print("--- notification (dry-run; audience=%s) ---\n%s\n"
                      % (audience, text))
            else:
                sender(text, None, audience=audience)


# ---------------------------------------------------------------------------
# DGN-1655: instance drive loaded but not beating (TTL/2 early warning)
# ---------------------------------------------------------------------------

def _load_ledger(root):
    """The instance's own terminal-state-ledger.py as a module (its policy
    and readers are the single source; nothing is re-derived here). None
    when the instance has no ledger engine."""
    import importlib.util
    path = os.path.join(root, "routines", "terminal-state-ledger.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(
        "tsl_%d" % abs(hash(os.path.realpath(path))), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def poll_drive(state, last_beat, warn_age, now_epoch):
    """Advance the drive-stale episode; return an event the first time it
    becomes notifiable, else None.

    stale = newest drive beat older than warn_age; a loaded drive that has
    NEVER beaten is timed from when this observer first saw it so (a unit
    loaded seconds ago is not stale). Once notified, the episode stays quiet
    until a fresh beat clears it (episode-once contract); a later stall is a
    new episode."""
    if last_beat is not None and now_epoch - last_beat <= warn_age:
        state.clear()
        return None
    if last_beat is None:
        since = state.setdefault("no_beat_since", now_epoch)
        age = now_epoch - since
    else:
        state.pop("no_beat_since", None)
        age = now_epoch - last_beat
    if age <= warn_age:
        return None
    state["age"] = int(age)
    if state.get("notified_at"):
        return None
    state["notified_at"] = now_epoch
    state["last_beat"] = last_beat
    return {"kind": "drive_stale", "age": int(age), "warn_age": warn_age,
            "last_beat": last_beat}


def drive_state_path(label):
    return os.path.join(state_dir(), "drive-%s.json" % label.rsplit(".", 2)[-2])


def write_drive_inbox(event, label, root=None):
    """Facts for the owning agent; it, not this routine, addresses the owner."""
    root = root or _instance_root()
    inbox = os.path.join(root, ".telegram_bot", "session-inbox")
    last = event.get("last_beat")
    lines = [
        "[instance-drive] the terminal-state sweep drive is loaded but has not "
        "beaten for %ds (warning age %ds = TTL/2, DGN-1655)."
        % (event["age"], event["warn_age"]),
        "",
        "- label: %s" % label,
        "- last drive beat epoch: %s" % (last if last is not None else "none"),
        "",
        "Agent instruction: check the unit (launchctl print gui/$UID/%s: runs, "
        "last exit code) and its log .telegram_bot/logs/instance-drive.*.log. "
        "If the host simply slept, say so; if the unit stopped firing, re-run "
        "`bash routines/lib/drive-setup.sh install <root>` with the owner's "
        "OK. Never start mirror-poll or another feature timer to make the "
        "beat graph green -- that is the mask DGN-1655 removed. Ledger "
        "verdict: `python3 routines/terminal-state-ledger.py check`." % label,
    ]
    os.makedirs(inbox, exist_ok=True)
    name = "instance-drive-%d-%d.md" % (int(time.time() * 1000), os.getpid())
    tmp = os.path.join(inbox, ".%s.tmp" % name)
    with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(inbox, name))


def observe_instance_drive(loaded, root=None, now_epoch=None):
    """One poll of THIS instance's drive. Returns new events (0 or 1)."""
    import fcntl
    root = root or _instance_root()
    tsl = _load_ledger(root)
    if tsl is None:
        return []
    label = tsl.drive_label(root)
    if label not in loaded:
        return []          # absence / loss is rule 3's (240s confirmation)
    warn_age = tsl.drive_policy()["warn_age"]
    events, _corrupt = tsl.read_events()
    last = tsl.last_drive_beat_epoch(events)
    now_epoch = time.time() if now_epoch is None else now_epoch
    os.makedirs(state_dir(), exist_ok=True)
    path = drive_state_path(label)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        event = poll_drive(state, last, warn_age, now_epoch)
        # Saved before the drop: the once-per-episode marker survives a
        # failed write (episode-once contract).
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, path)
    if event:
        write_drive_inbox(event, label, root)
    return [event] if event else []


# ---------------------------------------------------------------------------
# collection (subprocess layer -- NOT exercised by unit tests)
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout)


def _report_plist_errors(plist_errors):
    """DGN-1442 fail-loud: count + every file name on stderr (launchd log).
    Never raises, never stops the poll (fail-open for the other units)."""
    holes = [e for e in plist_errors or () if e.get("level") != "info"]
    for e in plist_errors or ():
        if e.get("level") == "info":
            sys.stderr.write("[health-observer] plist note: %s (%s)\n"
                             % (e["path"], e["error"]))
    if not holes:
        return
    sys.stderr.write(
        "[health-observer] COVERAGE HOLE: %d plist(s) unparseable -- their "
        "labels are NOT observed\n" % len(holes))
    for e in holes:
        sys.stderr.write("[health-observer]   unparseable: %s (%s)\n"
                         % (e["path"], e["error"]))


def collect_observation():
    """Read launchd + disk. READ-ONLY: list/print/plist reads only."""
    uid = os.getuid()
    home = os.path.expanduser("~")
    launch_agents_dir = os.path.join(home, "Library", "LaunchAgents")

    proc = _run(["launchctl", "list"])
    if proc.returncode != 0:
        raise RuntimeError("launchctl list failed: %s" % proc.stderr.strip())
    loaded = parse_launchctl_list(proc.stdout)

    printinfo, print_errors = {}, {}
    for label in sorted(loaded):
        try:
            p = _run(["launchctl", "print", "gui/%d/%s" % (uid, label)])
            if p.returncode != 0:
                print_errors[label] = ("launchctl print rc=%d: %s"
                                       % (p.returncode, p.stderr.strip()[:200]))
                continue
            printinfo[label] = parse_launchctl_print(p.stdout)
        except Exception as exc:
            print_errors[label] = "collection error: %s" % exc

    plist_errors = []
    bridges = bridge_map_from_dir(launch_agents_dir, plist_errors)
    routines_dirs = []
    for root in bridges.values():
        if root:
            rd = os.path.join(root, "routines")
            if os.path.isdir(rd):
                routines_dirs.append(rd)
    disk = scan_disk(launch_agents_dir, routines_dirs, plist_errors)
    _report_plist_errors(plist_errors)

    # Self slug: the bridge whose instance root is this script's own root.
    script_root = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    self_slug = None
    for blabel, root in bridges.items():
        if root and os.path.realpath(root) == script_root:
            self_slug = _instance_of(blabel)
            break

    return {
        "loaded": loaded,
        "printinfo": printinfo,
        "print_errors": print_errors,
        "disk": disk,
        "plist_errors": plist_errors,
        "bridge_labels": set(bridges),
        "launch_agents_dir": launch_agents_dir,
        "self_root": script_root,
        "instance_roots": {_instance_of(bl).lower(): root
                           for bl, root in bridges.items() if root},
        "crews": read_crews(),
        "update_runs": read_update_runs(),
        "self_bridges": sorted(
            bl for bl, root in bridges.items()
            if root and os.path.realpath(root) == script_root),
    }, self_slug


def send_push(text, button_spec, audience="owner"):
    """Notify via routines/push.sh (existing surface; no new notify rail).
    audience="operator" is operator-only (--no-owner-fallback): no operator
    configured -> push.sh sends nothing and answers rc 9, not a failure."""
    push_sh = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "push.sh")
    if not os.path.isfile(push_sh):
        sys.stderr.write("[health-observer] push.sh not found: %s\n" % push_sh)
        return False
    cmd = ["/bin/bash", push_sh, "--text", text, "--audience", audience]
    if audience == "operator":
        cmd.append("--no-owner-fallback")
    if button_spec:
        cmd += ["--button", button_spec]
    try:
        proc = _run(cmd, timeout=60)
        if audience == "operator" and proc.returncode == 9:
            return True
        if proc.returncode != 0:
            sys.stderr.write("[health-observer] push failed rc=%d: %s\n"
                             % (proc.returncode, proc.stderr.strip()[:300]))
            return False
        return True
    except Exception as exc:
        sys.stderr.write("[health-observer] push error: %s\n" % exc)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _label_loaded_live(label):
    """darwin-only fallback for cmd_retire's known-label predicate: a label
    the observer's state has not seen yet but that IS currently loaded (a
    legitimate "retire a brand-new label before the first poll sees it"
    case, DGN-1610 5-1(b))."""
    if sys.platform != "darwin":
        return False
    try:
        proc = _run(["launchctl", "list"])
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    return label in parse_launchctl_list(proc.stdout)


def cmd_retire(label, why, force=False):
    """DGN-1610: --retire used to accept any string and always return 0,
    so a typo'd label reported success while the real unit kept alerting
    (worklog DGN-1610, 20:12 section). Now rc=3 rejects an unknown label
    (state unchanged) unless --force overrides it."""
    state, _ = load_state()
    jobs = state.setdefault("jobs", {})
    known = label in jobs or _label_loaded_live(label)
    if not known and not force:
        sys.stderr.write(
            "[health-observer] unknown label (not in state, not loaded): "
            "%s\n" % label)
        sys.stderr.write(
            "[health-observer] pass --force to retire a label the observer "
            "has not seen yet\n")
        return 3
    entry = jobs.setdefault(label, {"first_seen_at": now_iso()})
    when = now_iso()
    entry["retired"] = {"when": when, "why": why}
    save_state(state)
    print("retired: %s\t%s\t%s" % (label, when, why))
    return 0


def cmd_unretire(label):
    state, _ = load_state()
    entry = (state.get("jobs") or {}).get(label)
    if entry and entry.pop("retired", None):
        save_state(state)
        print("unretired: %s" % label)
    else:
        print("no retired marker for: %s" % label)
    return 0


def cmd_status():
    state, _ = load_state()
    json.dump(state, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_list_retired():
    """DGN-1610: there was no surface to confirm a marker actually landed --
    the 20:12 section had to infer it indirectly from a poll's 0-events
    line. This gives a direct readback."""
    state, _ = load_state()
    jobs = state.get("jobs", {})
    lines = []
    for label in sorted(jobs):
        retired = jobs[label].get("retired")
        if retired:
            lines.append("%s\t%s\t%s" % (
                label, retired.get("when", ""), retired.get("why", "")))
    if not lines:
        print("no retired markers")
    else:
        for line in lines:
            print(line)
    return 0


def cmd_machine_alerts(choice, until=None, root=None):
    """DGN-1911: the settings path for the machine-alert switch.

    Prints key=value facts for the agent (never owner copy).  ``status``
    is read-only; ``on``/``off`` write config/agent.conf and retire any
    legacy unreachable-threshold lines.
    """
    out = sys.stdout
    if choice != "status":
        result = set_machine_alerts(choice == "on", root, until)
        out.write("changed=%s\n" % ("yes" if result["changed"] else "no"))
        for line in result["migrated"]:
            out.write("migrated=%s\n" % line)
        if result["backup"]:
            out.write("backup=%s\n" % result["backup"])
    mode = machine_alerts_mode(root)
    out.write("machine_alerts=%s\n" % ("on" if mode["on"] else "off"))
    out.write("switch=%s\n" % mode["switch"])
    if mode["until"]:
        out.write("off_until=%s\n" % mode["until"])
    for _, key, value in mode["legacy_mute"]:
        out.write("legacy_mute=%s=%s\n" % (key, value))
    if not is_machine_steward(root):
        out.write("note=this instance is not the machine steward; "
                  "its observer never sends machine alerts\n")
    return 0


USAGE = (
    "usage: health-observer.py [--dry-run]\n"
    "       health-observer.py --status\n"
    "       health-observer.py --retire LABEL --why TEXT [--force]\n"
    "       health-observer.py --unretire LABEL\n"
    "       health-observer.py --list-retired\n"
    "       health-observer.py --machine-alerts on|off|status "
    "[--until YYYY-MM-DD[THH:MM]]\n"
    "       health-observer.py --help\n"
)


def parse_argv(argv):
    """Explicit parse (DGN-1610): the old code fell through any unrecognized
    flag straight into a live poll (a typo'd flag like --list-retired could
    fire a real push). Every token here is either a known flag/value or the
    whole call is a usage error -- poll never runs on an unrecognized
    argument. Returns a dict with either "error" or "action" set."""
    action = "poll"
    label = None
    why = None
    force = False
    dry_run = False
    alerts = None
    until = None
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok in ("--help", "-h"):
            return {"action": "help"}
        if tok == "--status":
            action = "status"
            i += 1
        elif tok == "--list-retired":
            action = "list-retired"
            i += 1
        elif tok == "--dry-run":
            dry_run = True
            i += 1
        elif tok == "--force":
            force = True
            i += 1
        elif tok == "--retire":
            action = "retire"
            i += 1
            if i >= n:
                return {"error": "missing LABEL after --retire"}
            label = argv[i]
            i += 1
        elif tok == "--unretire":
            action = "unretire"
            i += 1
            if i >= n:
                return {"error": "missing LABEL after --unretire"}
            label = argv[i]
            i += 1
        elif tok == "--machine-alerts":
            action = "machine-alerts"
            i += 1
            if i >= n or argv[i] not in ("on", "off", "status"):
                return {"error": "--machine-alerts takes on, off or status"}
            alerts = argv[i]
            i += 1
        elif tok == "--until":
            i += 1
            if i >= n or parse_until(argv[i]) is None:
                return {"error": "--until takes an ISO date or datetime"}
            until = argv[i]
            i += 1
        elif tok == "--why":
            i += 1
            if i >= n:
                return {"error": "missing TEXT after --why"}
            why = argv[i]
            i += 1
        else:
            return {"error": "unrecognized argument: %s" % tok}
    if until is not None and alerts != "off":
        return {"error": "--until only goes with --machine-alerts off"}
    return {"action": action, "label": label, "why": why, "force": force,
            "dry_run": dry_run, "alerts": alerts, "until": until}


def main(argv):
    parsed = parse_argv(argv)
    if "error" in parsed:
        sys.stderr.write(USAGE)
        sys.stderr.write("[health-observer] %s\n" % parsed["error"])
        return 2

    action = parsed["action"]
    if action == "help":
        sys.stdout.write(USAGE)
        return 0
    if action == "status":
        return cmd_status()
    if action == "list-retired":
        return cmd_list_retired()
    if action == "retire":
        if not parsed["label"] or not parsed["why"]:
            sys.stderr.write(USAGE)
            sys.stderr.write(
                "[health-observer] --retire requires LABEL and a "
                "non-empty --why\n")
            return 2
        return cmd_retire(parsed["label"], parsed["why"], parsed["force"])
    if action == "unretire":
        return cmd_unretire(parsed["label"])
    if action == "machine-alerts":
        return cmd_machine_alerts(parsed["alerts"], parsed["until"])

    # action == "poll". Machine readings are independent of launchd and are
    # deliberately alert-only.  The session-inbox route lets the steward
    # choose owner-facing language; this observer never sends a canned push.
    machine_events = observe_machine_metrics()

    # Linux systemd --user twin remains out of scope for scheduled-job
    # observation.  Cheap Linux machine readings above are still useful.
    if sys.platform != "darwin":
        if machine_events:
            sys.stderr.write("[health-observer] machine metrics: %d new condition(s)\n"
                             % len(machine_events))
        return 0

    dry_run = parsed["dry_run"]

    # DGN-1582 r3 (dec-236): nights the dawn memory queue never started.
    try:
        notify_dawn_never_ran(observe_dawn_runs(), dry_run=dry_run)
    except Exception as exc:   # one rail must never stop the job poll
        sys.stderr.write("[health-observer] dawn never-ran check failed: %s\n"
                         % exc)

    obs, self_slug = collect_observation()

    # DGN-1655 rule 8: loaded drive label + drive beat older than TTL/2.
    try:
        observe_instance_drive(obs["loaded"])
    except Exception as exc:   # one rail must never stop the job poll
        sys.stderr.write("[health-observer] instance drive check failed: %s\n"
                         % exc)

    # DGN-1745: every instance's observer shares jobs.json. Serialize the
    # read-poll-save (same pattern as the machine.json lock) so two observers
    # firing together cannot both confirm -- and both push -- one loss.
    import fcntl
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), ".jobs.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state, corrupt_warning = load_state()
        now = now_iso()
        events, _warnings = poll(state, obs, now)
        if corrupt_warning:
            corrupt_warning["first_seen_at"] = now
            state["warnings"] = state.get("warnings", []) + [corrupt_warning]

        # Save FIRST: notified_at markers must survive a push failure so the
        # same episode is never re-pushed (episode-once contract).
        save_state(state)

    for text, button in compose_notifications(events, self_slug):
        if dry_run:
            print("--- notification (dry-run; button=%r) ---\n%s\n"
                  % (button, text))
        else:
            send_push(text, button)

    records = state.get("warnings", [])
    n_warn = sum(1 for w in records if w.get("level") == "warn")
    sys.stderr.write(
        "[health-observer] poll ok: %d loaded, %d events, %d warn / %d info "
        "records, %d parse errors\n"
        % (len(obs["loaded"]), len(events), n_warn, len(records) - n_warn,
           len(state.get("parse_errors", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
