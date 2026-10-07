#!/usr/bin/env python3
"""Central on/off switch for the instance's blocking gates (DGN-1231).

TARGET LAYER (DGN-1226 declaration): Layer 2 -- a dev agent gating a dev
agent.  Declaration only; no layer-based branching is implemented (the
layer model is unsettled).

WHY (DGN-1231, owner order "이거 설정처럼 껐다켰다 가능하면 좋겠어"):
the gates built for the agent also stand in front of the OWNER (DGN-1226
layer-1 problem).  Before this module, 1 of 6 blocking gates had a kill
switch (DOGANY_SEED_GATE) and its name was private to that gate.  This
module gives every blocking gate the SAME switch, in one place, with one
status command.

LOCATION + NAME RATIONALE (candidates weighed):
  - config/agent.conf keys  <- CHOSEN.  Precedent: USAGE_GATE= already
    lives there with identical off-value semantics and env-override; the
    gates already read agent.conf via the same candidate-path resolution
    (done-claim-gate TICKET_FILE_GLOB / usage-gate PLAN), so no new read
    mechanism; the file is git-tracked, so an off state is a visible diff;
    and "like a setting" (the owner's words) points at the config home.
  - env vars only  <- REJECTED as the primary store: hook processes get
    the HARNESS env, not the owner's shell env; a var exported in a
    terminal never reaches a bridge session's hooks, so env-only switching
    silently fails for the exact user it serves.  Env stays as a
    session-scoped OVERRIDE (wins over conf), matching USAGE_GATE.
  - dedicated file (e.g. config/gates.conf)  <- REJECTED: a second config
    surface to discover/back up/propagate, with no capability the existing
    conf lacks.

KEY SCHEME (default = ON when unset; off values: off/false/0/no/disabled,
matching usage-gate/seed-gate precedent):
  GATE_ALL=off                   master for all PreToolUse BLOCKING gates
  GATE_<NAME>=off|on             individual gate (see GATES below)
  GATE_STOP_ALL=off              master for Stop BLOCKING nags (separate
                                 group -- see below)
  GATE_SEND_ALL=off              master for bridge send-time normalizers
                                 (DGN-983; separate group again -- they edit
                                 an outgoing message, they block nothing)
  Precedence per gate: individual explicit value (env > conf) beats its
  master; master beats default.  So GATE_ALL=off + GATE_DONE_CLAIM=on
  keeps exactly one gate armed.  Env of the same key beats conf.

BACK-COMPAT: DOGANY_SEED_GATE (env or conf) keeps working as a legacy
alias for GATE_SEED_LIVE and is consulted after the new name.  The seed
gate's docstring always claimed conf support; this module makes it real.

STOP-HOOK GROUP JUDGMENT (mission part C): PreToolUse deny and Stop nag
are NOT one switch.  A PreToolUse deny blocks a specific action mid-work
(acute, the layer-1 problem); a Stop nag delays turn end (chronic).  One
master killing both would let "unblock me now" silently also disable
turn-end integrity checks -- exactly the DGN-1223 quiet-off class.  So:
  - Stop hooks that actually BLOCK ({"decision": "block"}) get switches
    under their own master GATE_STOP_ALL: decomp-ledger check-stop,
    skill-feedback-gate --stop.
  - Stop hooks that only WARN or DETECT are EXCLUDED from switching
    entirely: undelivered-verdict-gate (systemMessage warn-only -- it IS
    a quiet-failure alarm, DGN-1223 countermeasure), feature-living-stale-lint
    (report-only, exit 1 is non-blocking for a Stop hook).
    output-gate-stop WAS in this excluded set while it was purely
    detect-only; DGN-1279 (2026-09-04) armed exactly one of its rules
    (IDLE_PROMISE, narrow layer), so it now carries a switch for that rule
    alone -- family slug `output-gate-idle-promise`, plus one slug per
    blocking SHAPE added in rev2 (`-ticket`, `-merge`); dispatch, the v1
    shape, keeps the family slug as its only switch.  Its six detect-only rules
    stay unswitchable for the reason above.  Switching a
    pure observation channel off only deletes visibility.
  - usage-gate / token-gate keep their own pre-existing keys (USAGE_GATE)
    and are out of DGN-1231 scope; status mentions them for completeness.

OFF-STATE VISIBILITY (mission core risk -- "off and forgotten is a gate
that does not exist, silently"): gates consult this switch ONLY at the
moment they were about to deny/block.  When the switch is off at that
moment, the gate emits a non-blocking {"systemMessage": ...} notice
("this would have been blocked") instead of going silent, and this module
appends one line to .claude/gate-switch-suppressed.log (append-only, no
cron, no scan -- the log is read on demand by `status`).  So a forgotten
off state re-surfaces exactly when it starts to matter, through the
already-wired hook invocation -- no new cron/scan (DGN-1194 3/3 consumer
failure; DGN-1221 constraint).

WHO FLIPS THE SWITCH: the legitimate operator is the OWNER.  The agent
editing its own conf to dodge a gate is a discipline violation this
module cannot mechanically prevent (the agent has Bash); the honest
mitigations are structural: conf is git-tracked (visible diff), every
suppressed would-fire is logged and notice-surfaced, and the CLI stamps
off lines with a date.  Stated per DGN-1194 grill discipline, not hidden.

INSTANCE REGISTRY EXTENSION (DGN-1680): an instance that runs a gate the
canon does not ship (e.g. a local park-gate guard) registers it in
config/gate-switch.custom.conf, NOT by editing GATES above -- editing the
canon dict freezes this whole file against updates for a 5-line data row.
The name follows the *.custom.* ownership convention (update.sh never
writes it).  Read from the directory of the resolved agent.conf.  One row
per line, '#' comments and blank lines ignored:

  slug | KEY | kind | script [| LEGACY_ENV,LEGACY_ENV]

  kind    blocking (master GATE_ALL) | stop-nag (master GATE_STOP_ALL)
  KEY     must look like GATE_<NAME>
The master is derived from kind, so a row cannot invent its own master.
Missing file = no-op.  A malformed row is skipped with one stderr line
naming the file, line and reason (never a crash: this runs in hooks).  A
row whose slug or key collides with a canonical gate (or an earlier row)
is dropped -- the canonical row wins -- with one stderr line per process.

CLI:
  gate-switch.py status [--conf PATH]
  gate-switch.py off all|stop-all|send-all|<gate> [...] [--reason WHY] [--conf PATH]
  gate-switch.py on  all|stop-all|send-all|<gate> [...] [--conf PATH]

Fail-open contract (toward PROTECTION, inverted from the gates' own
fail-open): if this module errors or is absent, callers treat the gate as
ENABLED.  A broken switch must never silently disarm a gate.
"""
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OFF_VALUES = {"off", "false", "0", "no", "disabled"}

MASTER_BLOCKING = "GATE_ALL"
MASTER_STOP = "GATE_STOP_ALL"
# DGN-983: send-time normalizers (the bridge edits a message before it goes
# out).  Their own group: turning Stop nags off must not also stop the bridge
# from normalizing, and the reverse.
MASTER_SEND = "GATE_SEND_ALL"

# slug -> spec.  kind: "blocking" (PreToolUse deny) | "stop-nag" (Stop block)
# | "send-normalize" (bridge send-time edit, DGN-983).
GATES = {
    "seed-live": {
        "key": "GATE_SEED_LIVE", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "seed-live-consistency-gate.py",
        "legacy": ("DOGANY_SEED_GATE",),
    },
    "rule-prescription": {
        "key": "GATE_RULE_PRESCRIPTION", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "rule-prescription-gate.py",
        "legacy": (),
    },
    "register-canon": {
        "key": "GATE_REGISTER_CANON", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "register-canon-gate.py",
        "legacy": (),
    },
    "decomp": {
        "key": "GATE_DECOMP", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "decomp-gate.py",
        "legacy": (),
    },
    "done-claim": {
        "key": "GATE_DONE_CLAIM", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "done-claim-gate.py",
        "legacy": (),
    },
    "tmp-artifact": {
        "key": "GATE_TMP_ARTIFACT", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "tmp-artifact-gate.py",
        "legacy": (),
    },
    "skill-landing": {
        "key": "GATE_SKILL_LANDING", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "skill-landing-gate.py",
        "legacy": (),
    },
    "onboarding-owner": {
        # DGN-1606: while ONBOARDING_PENDING is present, identity-file
        # writes are denied in turns that positively had no owner in them
        # (machine-injected boundary, or a non-interactive session).  The
        # measured incident: an injected restart-verify turn completed
        # onboarding alone and self-named.
        "key": "GATE_ONBOARDING_OWNER", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "onboarding-owner-gate.py",
        "legacy": (),
    },
    "dispatch-return-result-first": {
        "key": "GATE_DISPATCH_RETURN_RESULT_FIRST", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "dispatch-return-result-first-gate.py",
        "legacy": (),
    },
    "bg-job-name": {
        # DGN-1691 phase 2: a background Bash/Monitor/Workflow launch must
        # carry an owner-language `description` -- it is the name the owner
        # start/end push and the workbench row render.
        "key": "GATE_BG_JOB_NAME", "master": MASTER_BLOCKING,
        "kind": "blocking", "script": "bg-job-notice.py --gate",
        "legacy": (),
    },
    "decomp-ledger-stop": {
        "key": "GATE_STOP_DECOMP_LEDGER", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "decomp-ledger.py check-stop",
        "legacy": (),
    },
    "skill-feedback-stop": {
        "key": "GATE_STOP_SKILL_FEEDBACK", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "skill-feedback-gate.py --stop",
        "legacy": (),
    },
    "output-gate-idle-promise": {
        # DGN-1279: output-gate-stop.py became partly blocking -- the
        # IDLE_PROMISE narrow layer only.  Its other six rules stay
        # detect-only and are NOT switchable (switching an observation
        # channel off only deletes visibility -- see the note above).
        "key": "GATE_STOP_OUTPUT_IDLE_PROMISE", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "output-gate-stop.py",
        "legacy": (),
    },
    "output-gate-idle-promise-ticket": {
        # DGN-1279 rev2: per-SHAPE switch.  The block layer's whitelist grew
        # from one shape to three, and a shape that misfires must be killable
        # without disarming the two that measured clean.  Off here OR off on
        # the family slug above suppresses this shape's block.
        "key": "GATE_STOP_OUTPUT_IDLE_PROMISE_TICKET", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "output-gate-stop.py",
        "legacy": (),
    },
    "output-gate-idle-promise-merge": {
        "key": "GATE_STOP_OUTPUT_IDLE_PROMISE_MERGE", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "output-gate-stop.py",
        "legacy": (),
    },
    "output-gate-idle-promise-carrier": {
        # DGN-1308: the inverted default -- any commitment outside the three
        # named shapes blocks unless the turn carries it (commit, dispatch,
        # handoff, background job, ticket write, wakeup/cron).  Killable on
        # its own so a misfire does not disarm the measured shapes.
        "key": "GATE_STOP_OUTPUT_IDLE_PROMISE_CARRIER", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "output-gate-stop.py",
        "legacy": (),
    },
    "output-gate-idrill-prose": {
        # DGN-983 / DGN-1527: output_gate IDRILL_PROSE_Q.  The bridge removes
        # a prose count-question from a message that arms an idrill keyboard
        # and counts the removal.  Off -> the message goes out untouched and
        # the would-fire is still counted (suppressed log + canary line), so
        # switching it off never erases the measurement.
        "key": "GATE_SEND_OUTPUT_IDRILL_PROSE", "master": MASTER_SEND,
        "kind": "send-normalize", "script": "bridge/artifacts.py",
        "legacy": (),
    },
    "output-gate-term-dict": {
        # DGN-1500: output_gate TERM_LEAK over the layered term dictionary
        # (routines/lib/term_dict.py).  The bridge counts every listed
        # mistranslation and rewrites only entries marked safe-to-rewrite.
        # Off -> nothing is rewritten; every hit is still counted (canary
        # line stage=suppressed-off + suppressed log), never blocked.
        "key": "GATE_SEND_OUTPUT_TERM_DICT", "master": MASTER_SEND,
        "kind": "send-normalize", "script": "bridge/artifacts.py",
        "legacy": (),
    },
    "onboarding-gate": {
        # DGN-1543 R7: first-contact onboarding forcing point.  While the
        # identity file carries ONBOARDING_PENDING, an owner-facing turn
        # with zero onboarding engagement (no identity-file touch, no
        # dogany-user-onboarding use) is Stop-blocked once per turn.
        "key": "GATE_STOP_ONBOARDING", "master": MASTER_STOP,
        "kind": "stop-nag", "script": "onboarding-gate-stop.py",
        "legacy": (),
    },
}

# --- instance registry extension (DGN-1680) ---------------------------------

EXT_NAME = "gate-switch.custom.conf"
_KINDS = {"blocking": MASTER_BLOCKING, "stop-nag": MASTER_STOP}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEY_RE = re.compile(r"^GATE_[A-Z0-9_]+$")
_LEGACY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ext_cache = {}    # ext path -> (rows, problems)
_ext_warned = set()  # (ext path, message) already printed this process


def _ext_path(conf):
    return os.path.join(os.path.dirname(conf), EXT_NAME)


def _reserved_keys():
    keys = {MASTER_BLOCKING, MASTER_STOP, MASTER_SEND}
    for spec in GATES.values():
        keys.add(spec["key"])
        keys.update(spec["legacy"])
    return keys


def _parse_ext(path):
    """(rows, problems) from one extension file.  Never raises."""
    rows, problems = {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.read().splitlines()
    except FileNotFoundError:
        return rows, problems
    except OSError as e:
        return rows, ["cannot read %s: %s" % (path, e)]
    reserved = _reserved_keys()
    seen_keys = set()
    for n, raw in enumerate(raw_lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        where = "%s:%d" % (path, n)
        parts = [p.strip() for p in line.split("|")]
        if len(parts) not in (4, 5):
            problems.append("%s: want 'slug | KEY | kind | script [| LEGACY,..]'"
                            " (got %d fields) -- row skipped"
                            % (where, len(parts)))
            continue
        slug, key, kind, script = parts[:4]
        legacy = tuple(x.strip() for x in parts[4].split(",")
                       if x.strip()) if len(parts) == 5 else ()
        if not _SLUG_RE.match(slug):
            problems.append("%s: bad slug %r -- row skipped" % (where, slug))
        elif not _KEY_RE.match(key):
            problems.append("%s: bad key %r (want GATE_<NAME>) -- row skipped"
                            % (where, key))
        elif kind not in _KINDS:
            problems.append("%s: bad kind %r (blocking|stop-nag) -- row "
                            "skipped" % (where, kind))
        elif not script:
            problems.append("%s: empty script -- row skipped" % where)
        elif not all(_LEGACY_RE.match(x) for x in legacy):
            problems.append("%s: bad legacy name in %r -- row skipped"
                            % (where, parts[4]))
        elif slug in GATES:
            problems.append("%s: slug %r collides with a canonical gate -- "
                            "canonical kept, row ignored" % (where, slug))
        elif slug in rows:
            problems.append("%s: slug %r duplicated in this file -- first "
                            "kept" % (where, slug))
        elif {key, *legacy} & (reserved | seen_keys):
            problems.append("%s: key/legacy of %r collides with a canonical "
                            "or earlier key -- row ignored" % (where, slug))
        else:
            rows[slug] = {
                "key": key, "master": _KINDS[kind], "kind": kind,
                "script": script, "legacy": legacy, "custom": True,
            }
            seen_keys.update({key, *legacy})
    return rows, problems


def ext_problems(conf):
    """Problems found in the extension file beside `conf` (for status)."""
    return _load_ext(conf)[1]


def _load_ext(conf):
    path = _ext_path(conf)
    if path not in _ext_cache:
        try:
            _ext_cache[path] = _parse_ext(path)
        except Exception as e:  # a hook must never crash on this file
            _ext_cache[path] = ({}, ["%s: unreadable (%s) -- ignored"
                                     % (path, e)])
    rows, problems = _ext_cache[path]
    for msg in problems:
        if (path, msg) not in _ext_warned:
            _ext_warned.add((path, msg))
            try:
                print("[gate-switch] " + msg, file=sys.stderr)
            except Exception:
                pass
    return _ext_cache[path]


def registry(conf=None, cwd=None):
    """Canonical GATES plus the valid instance rows.  Canonical wins."""
    try:
        extra = _load_ext(conf or conf_path(cwd))[0]
    except Exception:
        extra = {}
    if not extra:
        return GATES
    merged = dict(GATES)
    merged.update(extra)
    return merged


# Wired Stop hooks deliberately OUTSIDE the switch (warn/detect-only --
# they block nothing; switching them off only deletes visibility).
EXCLUDED = (
    ("undelivered-verdict-gate.py", "warn-only systemMessage; DGN-1223 "
                                    "quiet-failure alarm itself"),
    ("output-gate-stop.py (7 detect rules)",
     "WALL/SKELETON/DONE_CLAIM/SOT_UNREAD/MOMENTUM/DEPRECATED_TERM/"
     "IDRILL_PROSE_Q are detect-only; its IDLE_PROMISE narrow layer blocks "
     "and IS switchable above as output-gate-idle-promise plus one slug per "
     "blocking shape (DGN-1279 rev2); IDRILL_PROSE_Q's bridge normalizer is "
     "switchable above as output-gate-idrill-prose (DGN-983)"),
    ("feature-living-stale-lint.py", "report-only; exit 1 non-blocking"),
)


# --- conf reading -----------------------------------------------------------

def conf_path(cwd=None):
    """First existing config/agent.conf candidate (env override first --
    tests/CLI); falls back to the script-relative path even if absent."""
    override = os.environ.get("GATE_SWITCH_CONF", "").strip()
    if override:
        return override
    candidates = []
    if cwd:
        candidates.append(os.path.join(cwd, "config", "agent.conf"))
    candidates.append(os.path.join(os.getcwd(), "config", "agent.conf"))
    fallback = os.path.normpath(
        os.path.join(SCRIPT_DIR, "..", "config", "agent.conf"))
    candidates.append(fallback)
    for path in candidates:
        if os.path.isfile(path):
            return path
    return fallback


def _read_conf_value(path, key):
    """Raw value of the last active `KEY=` line, or None if absent."""
    val = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    val = line.split("=", 1)[1].split("#", 1)[0].strip()
    except OSError:
        return None
    return val


def _is_off(value):
    return value is not None and value.strip().lower() in OFF_VALUES


# --- resolution -------------------------------------------------------------

def resolve(slug, cwd=None):
    """Return (enabled: bool, source: str) for one gate.

    Individual explicit value (env new-name > env legacy > conf new-name >
    conf legacy) beats master (env > conf) beats default-on.  Unknown slug
    resolves enabled (fail toward protection).
    """
    path = conf_path(cwd)
    spec = registry(path).get(slug)
    if spec is None:
        return True, "unknown gate (default on)"
    # individual explicit value
    for src, key in (
            [("env", spec["key"])]
            + [("env", k) for k in spec["legacy"]]
            + [("conf", spec["key"])]
            + [("conf", k) for k in spec["legacy"]]):
        if src == "env":
            raw = os.environ.get(key)
            if raw is not None and raw.strip():
                return (not _is_off(raw)), "env %s=%s" % (key, raw.strip())
        else:
            raw = _read_conf_value(path, key)
            if raw is not None and raw != "":
                return (not _is_off(raw)), "conf %s=%s" % (key, raw)
    # master
    master = spec["master"]
    raw = os.environ.get(master)
    if raw is not None and raw.strip():
        return (not _is_off(raw)), "env %s=%s" % (master, raw.strip())
    raw = _read_conf_value(path, master)
    if raw is not None and raw != "":
        return (not _is_off(raw)), "conf %s=%s" % (master, raw)
    return True, "default on"


def is_enabled(slug, cwd=None):
    """Switch verdict for gate `slug`.  Any internal error -> True
    (a broken switch must never disarm a gate)."""
    try:
        return resolve(slug, cwd)[0]
    except Exception:
        return True


# --- off-state visibility ---------------------------------------------------

def _suppress_log_path(cwd=None):
    override = os.environ.get("GATE_SWITCH_LOG", "").strip()
    if override:
        return override
    root = os.path.dirname(os.path.dirname(conf_path(cwd)))
    return os.path.join(root, ".claude", "gate-switch-suppressed.log")


def _log_suppressed(slug, reason, cwd=None):
    try:
        path = _suppress_log_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S"), slug,
                " ".join(reason.split())[:160]))
    except Exception:
        pass  # visibility is best-effort; never wedge the calling gate


def off_notice(slug, reason, cwd=None):
    """Payload a gate prints INSTEAD of its deny/block when switched off at
    a would-fire moment.  systemMessage is non-blocking; the suppressed
    event is also appended to the audit log."""
    _log_suppressed(slug, reason, cwd)
    try:
        _enabled, source = resolve(slug, cwd)
    except Exception:
        source = "?"
    return {"systemMessage": (
        "[게이트 꺼짐] %s: 방금 동작은 이 게이트가 켜져 있었다면 차단됐을 "
        "상황입니다 (%s). 다시 켜기: python3 routines/gate-switch.py on %s "
        "(현재 상태: gate-switch.py status). 차단 사유였을 내용: %s"
        % (slug, source, slug, " ".join(reason.split())[:200]))}


# --- CLI: conf writing ------------------------------------------------------

def _write_conf_lines(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path)


def _read_conf_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def _set_key(path, key, value, reason=""):
    """Replace the active `KEY=` line or append one, stamped with a date
    (and the optional --reason, so the off line carries its own why)."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if reason:
        stamp += " -- " + " ".join(reason.split())[:80]
    new_line = "%s=%s  # gate-switch %s" % (key, value, stamp)
    lines = _read_conf_lines(path)
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            if not done:
                out.append(new_line)
                done = True
            continue  # drop duplicate active lines for the same key
        out.append(line)
    if not done:
        out.append(new_line)
    _write_conf_lines(path, out)


def _del_key(path, key):
    lines = _read_conf_lines(path)
    out = [l for l in lines if not l.strip().startswith(key + "=")]
    if out != lines:
        _write_conf_lines(path, out)


def _targets_to_slugs(targets, gates):
    """Expand CLI targets; returns (slugs, masters) or raises ValueError."""
    slugs, masters = [], []
    for t in targets:
        if t == "all":
            masters.append(MASTER_BLOCKING)
        elif t == "stop-all":
            masters.append(MASTER_STOP)
        elif t == "send-all":
            masters.append(MASTER_SEND)
        elif t in gates:
            slugs.append(t)
        else:
            raise ValueError(
                "unknown gate %r (known: %s, all, stop-all, send-all)"
                % (t, ", ".join(gates)))
    return slugs, masters


def cmd_off(targets, path, reason=""):
    gates = registry(path)
    slugs, masters = _targets_to_slugs(targets, gates)
    for master in masters:
        _set_key(path, master, "off", reason)
        print("OFF  %s (master) -> %s" % (master, path))
    for slug in slugs:
        _set_key(path, gates[slug]["key"], "off", reason)
        print("OFF  %s (%s) -> %s" % (slug, gates[slug]["key"], path))
    print("꺼짐 상태는 해당 게이트가 차단했을 순간마다 통지로 "
          "노출됩니다. 조회: gate-switch.py status")
    return 0


def cmd_on(targets, path):
    gates = registry(path)
    slugs, masters = _targets_to_slugs(targets, gates)
    for master in masters:
        _del_key(path, master)
        # restoring a master also clears individual off-keys in its group:
        # "on all" must mean everything in the group is on, not "master on
        # but stale individual offs keep winning".
        for slug, spec in gates.items():
            if spec["master"] == master:
                _del_key(path, spec["key"])
                for legacy in spec["legacy"]:
                    _del_key(path, legacy)
        print("ON   %s (master restored to default; group keys cleared)"
              % master)
    for slug in slugs:
        spec = gates[slug]
        for legacy in spec["legacy"]:
            _del_key(path, legacy)
        # if the master still resolves off, an explicit =on is required for
        # this gate to arm; otherwise deleting the key restores default-on.
        master_raw = os.environ.get(spec["master"]) or _read_conf_value(
            path, spec["master"])
        if _is_off(master_raw):
            _set_key(path, spec["key"], "on")
            print("ON   %s (%s=on; master %s is off, explicit on wins)"
                  % (slug, spec["key"], spec["master"]))
        else:
            _del_key(path, spec["key"])
            print("ON   %s (%s removed; default on)" % (slug, spec["key"]))
    return 0


def _suppressed_summary(cwd=None):
    """slug -> (count, last_ts_string) from the suppressed log."""
    out = {}
    try:
        with open(_suppress_log_path(cwd), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    ts, slug = parts[0], parts[1]
                    cnt, _ = out.get(slug, (0, ""))
                    out[slug] = (cnt + 1, ts)
    except OSError:
        pass
    return out


def cmd_status(path, cwd=None):
    print("gate-switch status  (conf: %s)" % path)
    sup = _suppressed_summary(cwd)
    gates = registry(path)
    for kind, master, title in (
            ("blocking", MASTER_BLOCKING, "PreToolUse blocking gates"),
            ("stop-nag", MASTER_STOP, "Stop blocking nags"),
            ("send-normalize", MASTER_SEND, "Bridge send-time normalizers")):
        raw = os.environ.get(master) or _read_conf_value(path, master)
        mstate = "off" if _is_off(raw) else ("on" if raw else "on (default)")
        print("[%s]  master %s: %s" % (title, master, mstate))
        for slug, spec in gates.items():
            if spec["kind"] != kind:
                continue
            enabled, source = resolve(slug, cwd)
            line = "  %-20s %-4s (%s)" % (
                slug, "on" if enabled else "OFF", source)
            if spec.get("custom"):
                line += "  [instance row: %s]" % EXT_NAME
            if slug in sup:
                cnt, ts = sup[slug]
                line += "  [suppressed would-fire x%d, last %s]" % (cnt, ts)
            print(line)
    problems = ext_problems(path)
    if problems:
        print("[instance registry %s -- %d problem(s)]"
              % (EXT_NAME, len(problems)))
        for msg in problems:
            print("  " + msg)
    print("[excluded from switching -- warn/detect-only, block nothing]")
    for script, why in EXCLUDED:
        print("  %-32s %s" % (script, why))
    print("[separate pre-existing switches]")
    print("  usage-gate.py: USAGE_GATE= / token-gate.py (own mechanisms)")
    return 0


def main(argv):
    args = list(argv)
    path = None
    reason = ""
    for flag in ("--conf", "--reason"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print("%s requires a value" % flag, file=sys.stderr)
                return 2
            if flag == "--conf":
                path = args[i + 1]
            else:
                reason = args[i + 1]
            del args[i:i + 2]
    if not args:
        print(__doc__.split("CLI:")[1].split("Fail-open")[0])
        return 2
    cmd, targets = args[0], args[1:]
    if path is None:
        path = conf_path()
    if cmd == "status":
        return cmd_status(path)
    if cmd in ("off", "on"):
        if not targets:
            print("usage: gate-switch.py %s all|stop-all|send-all|<gate> [...]" % cmd,
                  file=sys.stderr)
            return 2
        try:
            if cmd == "off":
                rc = cmd_off(targets, path, reason)
            else:
                rc = cmd_on(targets, path)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print()
        cmd_status(path)
        return rc
    print("unknown command %r (status | off | on)" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
