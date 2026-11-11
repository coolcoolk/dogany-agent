#!/usr/bin/env python3
"""Preview feature switch (DGN-1976): the one place the agent turns a v2.7
preview feature on or off when the owner asks.  No config file edit by the owner.

Features ship OFF (dec-338) or, where the feature predates the switch,
keep their current behaviour while the key is unset (see DEFAULTS below).

STORE: config/agent.conf, the same KEY=value home gate-switch.py writes
(no new store).  The file is read and rewritten through gate-switch.py's
own line helpers (_read_conf_lines / _write_conf_lines: atomic replace).
The value line is written bare (`WHY_NET=on`, no trailing comment):
conf_reader.conf_get, which the feature readers use, does not strip an
inline comment, so `WHY_NET=on  # stamp` would read as not-on.

UNITS: a feature with a background beat (the Why net morning ask) ships its unit deferred (routines/plists.defer).  `on` loads
that one unit through `routines/lib/routine-ctl.sh load <name>`; `off`
unschedules it through `routine-ctl.sh disable <name>` (the sanctioned
teardown).  The conf key alone already silences the feature: a loaded
beat with the key off sends nothing.

The requested feature and direction are the selection. The legacy --confirm
argument is accepted but no additional owner proof is required.

Usage:
  preview-switch.py list                    every feature and its state
  preview-switch.py status <feature>
  preview-switch.py on <feature> [--confirm "<owner reply>"]
  preview-switch.py off <feature>

Output: one JSON object on stdout.
Exit: 0 ok | 2 usage / unknown feature (the result lists the known ones) |
      3 conf written but the unit step failed | 10 needs the owner's yes.

Env (tests / unusual layouts): DOGANY_AGENT_CONF (conf path; else
gate-switch.py conf_path()), PREVIEW_SWITCH_OS (platform override),
PREVIEW_SWITCH_ROUTINE_CTL (routine-ctl.sh path).
"""

import importlib.util
import json
import os
import platform
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(SCRIPT_DIR, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from conf_reader import conf_get  # noqa: E402

EXIT_USAGE = 2
EXIT_UNIT = 3
EXIT_CONFIRM = 10

# id -> key: conf key | legacy: keys read as the same switch (cleared on
# off) | default: value while unset | unit: routine-ctl name of the beat |
# then: what the agent does after `on` (skill to run), or "".
FEATURES = {
    "why-net": {"key": "WHY_NET", "legacy": (), "default": "off",
                "unit": "why-morning", "then": ""},
    "computer-use": {"key": "COMPUTER_USE", "legacy": (), "default": "on",
                     "unit": "", "then": "dogany-computer-use"},
    "custom-agent": {"key": "CUSTOM_AGENT", "legacy": (), "default": "on",
                     "unit": "", "then": ""},
}
ALIASES = {
    "why": "why-net", "why_net": "why-net", "whynet": "why-net",
    "computer_use": "computer-use", "computer": "computer-use",
    "custom-agents": "custom-agent", "custom_agent": "custom-agent",
    "custom": "custom-agent",
}
ON_VALUES = ("on",)


def _gate_switch():
    spec = importlib.util.spec_from_file_location(
        "gate_switch", os.path.join(SCRIPT_DIR, "gate-switch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GS = _gate_switch()


def conf_path():
    return os.environ.get("DOGANY_AGENT_CONF", "").strip() or GS.conf_path()


def resolve_id(name):
    n = (name or "").strip().lower()
    return n if n in FEATURES else ALIASES.get(n)


def state(fid, path):
    """('on'|'off', source)."""
    spec = FEATURES[fid]
    raw = conf_get(spec["key"], path)
    if raw:
        return ("on" if raw.lower() in ON_VALUES else "off"), \
            "conf %s=%s" % (spec["key"], raw)
    for key in spec["legacy"]:
        raw = conf_get(key, path)
        if raw and raw.lower() in ON_VALUES:
            return "on", "conf %s=%s (legacy)" % (key, raw)
    return spec["default"], "default"


def _set(path, key, value):
    lines, out, done = GS._read_conf_lines(path), [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            if not done:
                out.append("%s=%s" % (key, value))
                done = True
            continue
        out.append(line)
    if not done:
        out.append("%s=%s" % (key, value))
    GS._write_conf_lines(path, out)


def _os():
    return os.environ.get("PREVIEW_SWITCH_OS", "").strip() or platform.system()


def unit_step(verb, name):
    """Run routine-ctl for the feature's beat -> (status, detail)."""
    if not name:
        return "none", ""
    if _os() != "Darwin":
        # the beats ship as launchd units only; no systemd twin yet
        return "not_on_this_os", "%s has no unit for %s" % (name, _os())
    rctl = os.environ.get("PREVIEW_SWITCH_ROUTINE_CTL", "").strip() or \
        os.path.join(LIB, "routine-ctl.sh")
    try:
        r = subprocess.run(["bash", rctl, verb, name], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "failed", str(exc)[:200]
    detail = (r.stdout + r.stderr).strip()[-300:]
    return ("ok" if r.returncode == 0 else "failed"), detail


def confirm_line(fid, path):
    import i18n_lookup
    root = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    lang = conf_get("AGENT_LANG", path) or "en"
    return i18n_lookup.lookup(root, lang, "preview.confirm." + fid)


def _row(fid, path):
    st, src = state(fid, path)
    return {"feature": fid, "key": FEATURES[fid]["key"], "state": st,
            "source": src}


def _emit(obj, rc=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True)
                     + "\n")
    return rc


def _unknown(name):
    return _emit({"ok": False, "error": "unknown_feature",
                  "asked": name, "known": sorted(FEATURES)}, EXIT_USAGE)


def cmd_on(fid, path, reply):
    spec = FEATURES[fid]
    _set(path, spec["key"], "on")
    unit, detail = unit_step("load", spec["unit"])
    out = dict(_row(fid, path), ok=unit != "failed", unit=unit,
               then=spec["then"])
    if detail:
        out["unit_detail"] = detail
    return _emit(out, EXIT_UNIT if unit == "failed" else 0)


def cmd_off(fid, path):
    spec = FEATURES[fid]
    _set(path, spec["key"], "off")
    for key in spec["legacy"]:
        GS._del_key(path, key)
    unit, detail = unit_step("disable", spec["unit"])
    out = dict(_row(fid, path), ok=unit != "failed", unit=unit)
    if detail:
        out["unit_detail"] = detail
    return _emit(out, EXIT_UNIT if unit == "failed" else 0)


def main(argv):
    args = list(argv)
    reply = None
    if "--confirm" in args:
        i = args.index("--confirm")
        if i + 1 >= len(args):
            return _emit({"ok": False, "error": "usage",
                          "usage": "--confirm needs the owner's reply"},
                         EXIT_USAGE)
        reply = args[i + 1]
        del args[i:i + 2]
    if len(args) == 2 and resolve_id(args[0]) and args[1] in ("on", "off", "status"):
        args = [args[1], args[0]]
    if not args or args[0] not in ("list", "status", "on", "off"):
        return _emit({"ok": False, "error": "usage",
                      "usage": "list | status <feature> | on <feature> "
                               "[--confirm <reply>] | off <feature>",
                      "known": sorted(FEATURES)}, EXIT_USAGE)
    path = conf_path()
    verb = args[0]
    if verb == "list":
        return _emit({"ok": True,
                      "features": [_row(f, path) for f in sorted(FEATURES)]})
    if len(args) != 2:
        return _emit({"ok": False, "error": "usage",
                      "known": sorted(FEATURES)}, EXIT_USAGE)
    fid = resolve_id(args[1])
    if fid is None:
        return _unknown(args[1])
    if verb == "status":
        return _emit(dict(_row(fid, path), ok=True))
    if verb == "on":
        return cmd_on(fid, path, reply)
    return cmd_off(fid, path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
