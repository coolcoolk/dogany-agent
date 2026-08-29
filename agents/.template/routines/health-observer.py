#!/usr/bin/env python3
"""health-observer.py -- DGN-986 axis A: scheduled-job silent-death observer.

Locked spec: worklog DGN-986 (sections "2. axis A", revisions R1/R2/R3, D2
poll-interval amendment = 5 min). Evidence ledger: DGN-989 (Kojeni scheduled
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
  5. Label-loss events (R1): the state file keeps the known label set. Every
     poll, ``known - currently loaded`` = lost labels. A loss WITHOUT a
     ``retired: {when, why}`` marker is a first-class notify event (fail-loud,
     same rank as a failure streak). Retired markers suppress the event.
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
     success criterion 6; live proof = DGN-989: 2 loaded-outside Kojeni
     jobs; divergence live proof = com.telegram-skill-bot.metal,
     LaunchAgents copy vs loaded repo copy differing in
     EnvironmentVariables.PATH).

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
  health-observer.py --retire LABEL --why TEXT   mark an intentional removal
  health-observer.py --unretire LABEL            drop the marker
  health-observer.py --status                    dump the state file (JSON)
"""

import json
import os
import subprocess
import sys
import time

LABEL_PREFIX = "com.telegram-skill-bot."

# Structural exits: cannot self-heal (127 = script/binary missing, 126 = not
# executable/permission, 78 = EX_CONFIG). Negative signal exits are NEVER
# structural (R3 iii) -- and never appear here since members are positive.
STRUCTURAL_EXITS = frozenset((126, 127, 78))
GENERAL_STREAK_NOTIFY = 3
STRUCTURAL_STREAK_NOTIFY = 2

NEVER_EXITED = "(never exited)"
STATE_SCHEMA = 1

BUCKET_BRIDGE = "bridge"
BUCKET_RESIDENT = "resident_service"
BUCKET_WAITING = "waiting"
BUCKET_SCHEDULED = "scheduled"


def now_iso():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def state_dir():
    return os.environ.get(
        "DOGANY_HEALTH_OBSERVER_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".dogany", "health-observer"),
    )


def state_path():
    return os.path.join(state_dir(), "jobs.json")


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
                  Kojeni.jpy-vix-watch prints ten ``keepalive = 0`` sub-lines
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


def read_plist(path):
    """Read a plist file (xml or binary) -> dict, or None on any failure."""
    import plistlib
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


def plist_label(pdict):
    label = (pdict or {}).get("Label")
    return label if isinstance(label, str) else None


def bridge_map_from_dir(launch_agents_dir):
    """{bridge_label: instance_root} from ``*.newbridge.plist`` files.

    R2: residency = membership in the Label SET read from newbridge plists
    (the plist FILE NAME is not the label -- e.g. a file named
    Kojeni.newbridge.plist can carry Label com.telegram-skill-bot.Kojeni).
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
        pd = read_plist(os.path.join(launch_agents_dir, name))
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


def scan_disk(launch_agents_dir, routines_dirs):
    """Enumerate on-disk job plists -> {label: {paths, in_launch_agents}}.

    Reads the ``Label`` KEY of every ``*.plist`` (name-suffix exact, so
    ``.plist.bak-*`` backups are excluded); skips labels outside our prefix
    and unrendered templates (``__AGENT_NAME__`` placeholders).
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
            label = plist_label(read_plist(path))
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


def detect_losses(jobs, loaded_labels, now):
    """R1: known-set minus loaded-set = lost labels; unretired loss notifies."""
    events = []
    for label, entry in jobs.items():
        if label in loaded_labels:
            continue
        if entry.get("lost_at"):
            continue  # already accounted (event fired or suppressed earlier)
        entry["lost_at"] = now
        if entry.get("retired"):
            continue  # intentional removal -> suppressed, no event
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
      copy exists must not warn; live false positive: com.telegram-skill-bot.
      metal, coordinator correction 2026-08-21).
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
    }
    Returns (events, warnings). Mutates state in place.
    """
    jobs = state.setdefault("jobs", {})
    events = []
    parse_errors = []

    for label, msg in sorted((obs.get("print_errors") or {}).items()):
        parse_errors.append({"label": label, "error": msg, "at": now})

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

    events.extend(detect_losses(jobs, set(obs["loaded"]), now))

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
    return label[len(LABEL_PREFIX):].split(".")[-1] if label.startswith(LABEL_PREFIX) else label


def _instance_of(label):
    rest = label[len(LABEL_PREFIX):] if label.startswith(LABEL_PREFIX) else label
    return rest.split(".")[0]


def _investigate_desc(label, self_slug):
    # Lane conformance (spec section 2): a domain-live job's fix path runs
    # through Skull / owner conversation, not this instance.
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
# collection (subprocess layer -- NOT exercised by unit tests)
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout)


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

    bridges = bridge_map_from_dir(launch_agents_dir)
    routines_dirs = []
    for root in bridges.values():
        if root:
            rd = os.path.join(root, "routines")
            if os.path.isdir(rd):
                routines_dirs.append(rd)
    disk = scan_disk(launch_agents_dir, routines_dirs)

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
        "bridge_labels": set(bridges),
        "launch_agents_dir": launch_agents_dir,
    }, self_slug


def send_push(text, button_spec):
    """Notify via routines/push.sh (existing surface; no new notify rail)."""
    push_sh = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "push.sh")
    if not os.path.isfile(push_sh):
        sys.stderr.write("[health-observer] push.sh not found: %s\n" % push_sh)
        return False
    cmd = ["/bin/bash", push_sh, "--text", text]
    if button_spec:
        cmd += ["--button", button_spec]
    try:
        proc = _run(cmd, timeout=60)
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

def cmd_retire(label, why):
    state, _ = load_state()
    jobs = state.setdefault("jobs", {})
    entry = jobs.setdefault(label, {"first_seen_at": now_iso()})
    entry["retired"] = {"when": now_iso(), "why": why}
    save_state(state)
    print("retired: %s (%s)" % (label, why))
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


def main(argv):
    if "--status" in argv:
        return cmd_status()
    if "--retire" in argv:
        try:
            label = argv[argv.index("--retire") + 1]
        except IndexError:
            sys.stderr.write("usage: --retire LABEL [--why TEXT]\n")
            return 1
        why = ""
        if "--why" in argv:
            try:
                why = argv[argv.index("--why") + 1]
            except IndexError:
                pass
        return cmd_retire(label, why)
    if "--unretire" in argv:
        try:
            return cmd_unretire(argv[argv.index("--unretire") + 1])
        except IndexError:
            sys.stderr.write("usage: --unretire LABEL\n")
            return 1

    # Linux systemd --user twin: OUT OF SCOPE this build (spec-acknowledged
    # unverified). Silent success so a mistakenly-installed unit is harmless.
    if sys.platform != "darwin":
        return 0

    dry_run = "--dry-run" in argv

    obs, self_slug = collect_observation()
    state, corrupt_warning = load_state()
    now = now_iso()
    events, _warnings = poll(state, obs, now)
    if corrupt_warning:
        corrupt_warning["first_seen_at"] = now
        state["warnings"] = state.get("warnings", []) + [corrupt_warning]

    # Save FIRST: notified_at markers must survive a push failure so the same
    # episode is never re-pushed (episode-once contract).
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
