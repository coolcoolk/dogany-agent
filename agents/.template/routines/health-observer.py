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


def is_machine_steward(root=None):
    """Same representative rule as dogany-mac-optimize/gate.sh.

    Only DOGANY_AGENT_CLASS=main (or the historical absent/unknown -> main
    default) may emit a machine alert.  The shared state still protects the
    unlikely case of two simultaneously configured main instances.
    """
    root = root or _instance_root()
    try:
        with open(os.path.join(root, ".instance.conf"),
                  encoding="utf-8", errors="replace") as fh:
            values = [line.split("=", 1)[1].strip().strip('"').strip("'")
                      for line in fh if line.startswith("DOGANY_AGENT_CLASS=")]
        return not values or values[-1].lower() != "domain"
    except OSError:
        return True


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
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception as exc:
        if errors is not None:
            errors.append({"path": path,
                           "error": "%s: %s" % (type(exc).__name__, exc)})
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


def update_scheduled(label, entry, info, now):
    """Failure-streak accounting for one scheduled job. Returns events."""
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
        structural = last_exit in STRUCTURAL_EXITS
        threshold = (STRUCTURAL_STREAK_NOTIFY if structural
                     else GENERAL_STREAK_NOTIFY)
        if entry["fail_streak"] >= threshold and not entry.get("notified_at"):
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
    else:
        # No new run since last poll: same failure is NOT double-counted.
        entry["last_exit"] = last_exit
    return events


def update_resident(label, entry, info, list_pid, now):
    """Keepalive resident service: judged on PID presence, not exit codes."""
    events = []
    pid = list_pid if list_pid is not None else info.get("pid")
    if pid is None:
        if not entry.get("down_notified_at"):
            if not entry.get("down_since"):
                entry["down_since"] = now
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


def detect_losses(jobs, loaded_labels, now, is_mine=None):
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
    """
    events = []
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
# dropped. Several steward-class instances can exist (every crew leader is
# one); the locked, shared jobs.json lets only the first of them confirm it.

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
        if os.path.isdir(member) and is_machine_steward(member):
            return _real(member)
    return None


def loss_recipient(label, instance_roots, crews):
    """Leader root that announces ``label``'s loss, or None (= steward)."""
    root = (instance_roots or {}).get(_instance_of(label).lower())
    if not root:
        return None
    owner = _real(root)
    for kit in sorted(crews or {}):
        if any(_real(m) == owner for m in crews[kit]):
            return crew_leader(crews[kit])
    return None


def loss_is_mine(label, obs):
    """Whether the observer described by obs["self_root"] announces label."""
    self_root = _real(obs["self_root"])
    roots = obs.get("instance_roots") or {}
    leader = loss_recipient(label, roots, obs.get("crews") or {})
    if leader:
        slugs = [s for s, r in roots.items() if r and _real(r) == leader]
        loaded = {lbl.lower() for lbl in obs.get("loaded") or {}}
        if any((LABEL_PREFIX + s + ".health-observer").lower() in loaded
               for s in slugs):
            return leader == self_root
    return is_machine_steward(self_root)


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
    }
    Returns (events, warnings). Mutates state in place.
    """
    jobs = state.setdefault("jobs", {})
    events = []
    parse_errors = []

    for label, msg in sorted((obs.get("print_errors") or {}).items()):
        parse_errors.append({"label": label, "error": msg, "at": now})

    # DGN-1442: an unparseable plist = its label is missing from the disk
    # set = a silent coverage hole. Recorded per file so /health shows it.
    for e in obs.get("plist_errors") or []:
        parse_errors.append({
            "label": e["path"],
            "error": "plist unparseable (unit not observed): %s" % e["error"],
            "at": now,
        })

    for label in sorted(obs["loaded"]):
        entry = jobs.setdefault(label, {"first_seen_at": now})
        entry["last_seen_at"] = now
        # Reappearance clears loss markers (a later disappearance is a NEW
        # loss event) and any stale retired marker (the job is back).
        entry["lost_at"] = None
        entry["lost_notified_at"] = None
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
            events.extend(update_scheduled(label, entry, info, now))
        elif bucket == BUCKET_RESIDENT:
            events.extend(update_resident(
                label, entry, info, obs["loaded"][label], now))
        else:
            # bridge: axis-B territory, no exit accounting here.
            # waiting: future one-shot, nothing to account yet.
            entry["runs"] = info.get("runs")
            entry["last_exit"] = (info.get("last_exit")
                                  if info.get("has_exit_line") else None)

    is_mine = None
    if obs.get("self_root"):
        def is_mine(label):
            return loss_is_mine(label, obs)
    events.extend(detect_losses(jobs, set(obs["loaded"]), now, is_mine))

    warnings = diff_disk_loaded(
        obs["disk"], set(obs["loaded"]), obs["printinfo"], jobs,
        obs["launch_agents_dir"])

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
        return "확인 후 스컬 편으로 조치 경로까지 보고드립니다."
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
              "processes or delete data."]
    os.makedirs(inbox, exist_ok=True)
    name = "machine-health-%d-%d.md" % (int(time.time() * 1000), os.getpid())
    tmp = os.path.join(inbox, ".%s.tmp" % name)
    with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, os.path.join(inbox, name))


def observe_machine_metrics(root=None, readings=None, now_epoch=None):
    """Serialized shared-state poll. Returns new events; never sends a push."""
    import fcntl
    root = root or _instance_root()
    if not is_machine_steward(root):
        return []
    os.makedirs(state_dir(), exist_ok=True)
    lock_path = os.path.join(state_dir(), ".machine.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = load_machine_state()
        events = poll_machine_metrics(
            state, readings if readings is not None else collect_machine_readings(root),
            time.time() if now_epoch is None else now_epoch, machine_thresholds(root))
        save_machine_state(state)
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
# collection (subprocess layer -- NOT exercised by unit tests)
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout)


def _report_plist_errors(plist_errors):
    """DGN-1442 fail-loud: count + every file name on stderr (launchd log).
    Never raises, never stops the poll (fail-open for the other units)."""
    if not plist_errors:
        return
    sys.stderr.write(
        "[health-observer] COVERAGE HOLE: %d plist(s) unparseable -- their "
        "labels are NOT observed\n" % len(plist_errors))
    for e in plist_errors:
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


USAGE = (
    "usage: health-observer.py [--dry-run]\n"
    "       health-observer.py --status\n"
    "       health-observer.py --retire LABEL --why TEXT [--force]\n"
    "       health-observer.py --unretire LABEL\n"
    "       health-observer.py --list-retired\n"
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
        elif tok == "--why":
            i += 1
            if i >= n:
                return {"error": "missing TEXT after --why"}
            why = argv[i]
            i += 1
        else:
            return {"error": "unrecognized argument: %s" % tok}
    return {"action": action, "label": label, "why": why, "force": force,
            "dry_run": dry_run}


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
