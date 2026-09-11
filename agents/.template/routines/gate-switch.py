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

CLI:
  gate-switch.py status [--conf PATH]
  gate-switch.py off all|stop-all|<gate> [...] [--reason WHY] [--conf PATH]
  gate-switch.py on  all|stop-all|<gate> [...] [--conf PATH]

Fail-open contract (toward PROTECTION, inverted from the gates' own
fail-open): if this module errors or is absent, callers treat the gate as
ENABLED.  A broken switch must never silently disarm a gate.
"""
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OFF_VALUES = {"off", "false", "0", "no", "disabled"}

MASTER_BLOCKING = "GATE_ALL"
MASTER_STOP = "GATE_STOP_ALL"

# slug -> spec.  kind: "blocking" (PreToolUse deny) | "stop-nag" (Stop block).
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
}

# Wired Stop hooks deliberately OUTSIDE the switch (warn/detect-only --
# they block nothing; switching them off only deletes visibility).
EXCLUDED = (
    ("undelivered-verdict-gate.py", "warn-only systemMessage; DGN-1223 "
                                    "quiet-failure alarm itself"),
    ("output-gate-stop.py (6 detect rules)",
     "WALL/SKELETON/DONE_CLAIM/SOT_UNREAD/MOMENTUM/DEPRECATED_TERM are "
     "detect-only; its IDLE_PROMISE narrow layer blocks and IS switchable "
     "above as output-gate-idle-promise plus one slug per blocking shape "
     "(DGN-1279 rev2)"),
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
    spec = GATES.get(slug)
    if spec is None:
        return True, "unknown gate (default on)"
    path = conf_path(cwd)
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


def _targets_to_slugs(targets):
    """Expand CLI targets; returns (slugs, masters) or raises ValueError."""
    slugs, masters = [], []
    for t in targets:
        if t == "all":
            masters.append(MASTER_BLOCKING)
        elif t == "stop-all":
            masters.append(MASTER_STOP)
        elif t in GATES:
            slugs.append(t)
        else:
            raise ValueError(
                "unknown gate %r (known: %s, all, stop-all)"
                % (t, ", ".join(GATES)))
    return slugs, masters


def cmd_off(targets, path, reason=""):
    slugs, masters = _targets_to_slugs(targets)
    for master in masters:
        _set_key(path, master, "off", reason)
        print("OFF  %s (master) -> %s" % (master, path))
    for slug in slugs:
        _set_key(path, GATES[slug]["key"], "off", reason)
        print("OFF  %s (%s) -> %s" % (slug, GATES[slug]["key"], path))
    print("꺼짐 상태는 해당 게이트가 차단했을 순간마다 통지로 "
          "노출됩니다. 조회: gate-switch.py status")
    return 0


def cmd_on(targets, path):
    slugs, masters = _targets_to_slugs(targets)
    for master in masters:
        _del_key(path, master)
        # restoring a master also clears individual off-keys in its group:
        # "on all" must mean everything in the group is on, not "master on
        # but stale individual offs keep winning".
        for slug, spec in GATES.items():
            if spec["master"] == master:
                _del_key(path, spec["key"])
                for legacy in spec["legacy"]:
                    _del_key(path, legacy)
        print("ON   %s (master restored to default; group keys cleared)"
              % master)
    for slug in slugs:
        spec = GATES[slug]
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
    for kind, master, title in (
            ("blocking", MASTER_BLOCKING, "PreToolUse blocking gates"),
            ("stop-nag", MASTER_STOP, "Stop blocking nags")):
        raw = os.environ.get(master) or _read_conf_value(path, master)
        mstate = "off" if _is_off(raw) else ("on" if raw else "on (default)")
        print("[%s]  master %s: %s" % (title, master, mstate))
        for slug, spec in GATES.items():
            if spec["kind"] != kind:
                continue
            enabled, source = resolve(slug, cwd)
            line = "  %-20s %-4s (%s)" % (
                slug, "on" if enabled else "OFF", source)
            if slug in sup:
                cnt, ts = sup[slug]
                line += "  [suppressed would-fire x%d, last %s]" % (cnt, ts)
            print(line)
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
            print("usage: gate-switch.py %s all|stop-all|<gate> [...]" % cmd,
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
