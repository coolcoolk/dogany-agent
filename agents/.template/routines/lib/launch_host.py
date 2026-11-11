#!/usr/bin/env python3
"""launch-host library, Python twin (v2.7 T6 P2a, owner dec-319).

The bridge launch-host seam (design docs/design/v27-t6-launch-host-design.md
sections 3-4). routines/lib/launch-host.sh is the CLI that callers exec; it
performs the launchd ACTIONS (kickstart / bootout / bootstrap / enable) and
delegates every READ verb to this module, so host resolution, the lease and
the doctor report have exactly one implementation. healthcmd.py and
health-observer.py import this module directly (read side only).

Host resolution (design 4.1):
  1. lease <root>/.telegram_bot/state/launch-host.json
     {host, epoch, since, by, reason, state} -- written ONLY by write_lease()
     under flock (I1).
  2. no lease -> "launchd" (every instance that was never adopted).
  3. .instance.conf DOGANY_LAUNCH_HOST=auto|launchd|app is POLICY only: it
     decides whether update/install attempts an adopt, and never overrides a
     live lease. Default "launchd".
  A corrupt lease resolves to launchd plus a warning (design section 7).

Scope of this build (P2a): launchd backend, lease, doctor. The app backend
and adopt/release are P5; nothing here execs the app host CLI (doctor only
checks that the declared control path exists and is executable).

Never takes bridge.lock: a probe that grabbed the lock, even briefly, could
make a starting bridge exit 75. The holder is read from the file and checked
with kill(pid, 0).

CLI (used by launch-host.sh; JSON on stdout):
  launch_host.py host   <root>
  launch_host.py status <root> [--label L]
  launch_host.py doctor <root> [--label L] [--home H]
  launch_host.py label  <root> [--label L]     (prints label and plist path)
  launch_host.py lease-write <root> --host H --by B --reason R [--state S]
Exit: 0 ok, 1 error, 2 usage.

English/ASCII only. Pure stdlib.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOSTS = ("launchd", "app")
POLICIES = ("auto", "launchd", "app")
DEFAULT_HOST = "launchd"
DEFAULT_POLICY = "launchd"
LEASE_REL = os.path.join(".telegram_bot", "state", "launch-host.json")
LOCK_REL = os.path.join(".telegram_bot", "state", "bridge.lock")
HEARTBEAT_REL = os.path.join(".telegram_bot", "poll_heartbeat")
SERVICE_MARKER_REL = os.path.join(".telegram_bot", ".service_plist")
LABEL_PREFIX = "com.telegram-skill-bot."
APP_FILE = "com.moha.dogany.json"
COMPANION_FILE = "com.moha.dogany.dogany.json"
WATCHDOG_OFF = ("off", "0", "false", "no", "disabled")
PLACEHOLDER_RE = re.compile(r"__(AGENT_NAME|PROJECT_ROOT|HOME)__")


# --- small helpers ----------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _launchctl():
    return os.environ.get("DOGANY_LAUNCHCTL") or "launchctl"


def _run(argv, timeout=10):
    """Run argv; return (rc, stdout). rc None when the binary is missing."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _conf_value(root, key):
    """First KEY= line of <root>/.instance.conf, quotes stripped ('' if none).
    Same normalization as routines/lib/conf_reader.py conf_get."""
    try:
        text = Path(root, ".instance.conf").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            v = line[len(key) + 1:].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return ""


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --- lease -------------------------------------------------------------------

def lease_path(root):
    return Path(root) / LEASE_REL


def read_lease(root):
    """Return (lease_dict_or_None, warning_or_None).

    Absent -> (None, None). Unreadable / not JSON / host not launchd|app /
    epoch not a non-negative int -> (None, warning): the caller resolves to
    launchd and reports the warning (I7)."""
    p = lease_path(root)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, "lease unreadable (%s): %s" % (p, e)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "lease corrupt (not JSON): %s" % p
    if not isinstance(doc, dict):
        return None, "lease corrupt (root not an object): %s" % p
    if doc.get("host") not in HOSTS:
        return None, "lease corrupt (host=%r): %s" % (doc.get("host"), p)
    ep = doc.get("epoch")
    if not isinstance(ep, int) or isinstance(ep, bool) or ep < 0:
        return None, "lease corrupt (epoch=%r): %s" % (ep, p)
    return doc, None


def write_lease(root, host, by, reason, state="steady"):
    """Atomically write the lease under flock; epoch = previous + 1.

    The only lease writer (I1). A corrupt previous lease counts as epoch 0.
    Returns the written dict."""
    if host not in HOSTS:
        raise ValueError("host must be one of %s" % (HOSTS,))
    p = lease_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        prev, _warn = read_lease(root)
        epoch = (prev or {}).get("epoch", 0) + 1
        doc = {"host": host, "epoch": epoch, "since": _now_iso(), "by": by,
               "reason": reason, "state": state}
        fd, tmp = tempfile.mkstemp(prefix=".launch-host-", dir=str(p.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write((json.dumps(doc, indent=2) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(p))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return doc
    finally:
        os.close(lock_fd)


def read_policy(root):
    """Return (policy, warning_or_None). Invalid value -> default + warning."""
    v = _conf_value(root, "DOGANY_LAUNCH_HOST").strip().lower()
    if not v:
        return DEFAULT_POLICY, None
    if v in POLICIES:
        return v, None
    return DEFAULT_POLICY, "DOGANY_LAUNCH_HOST=%r invalid; using %s" % (v, DEFAULT_POLICY)


def resolve_host(root):
    """Effective host: lease beats default; policy never overrides a lease."""
    warnings = []
    lease, lease_warn = read_lease(root)
    if lease_warn:
        warnings.append(lease_warn)
    policy, w = read_policy(root)
    if w:
        warnings.append(w)
    if lease is not None:
        host, source = lease["host"], "lease"
    else:
        host, source = DEFAULT_HOST, "corrupt-lease" if lease_warn else "default"
    return {"host": host, "source": source, "policy": policy,
            "lease": lease, "warnings": warnings}


# --- label / plist -----------------------------------------------------------

def plist_label(path):
    """Label key of a plist (plistlib; empty on failure)."""
    import plistlib
    try:
        with open(path, "rb") as f:
            doc = plistlib.load(f)
    except Exception:
        return ""
    v = doc.get("Label") if isinstance(doc, dict) else None
    return v if isinstance(v, str) else ""


def resolve_label(root, override=None):
    """Return (label, plist_path_or_None, source).

    Order: explicit override; the DGN-888 service marker
    (.telegram_bot/.service_plist: line 1 plist, line 2 label) when its plist
    is placeholder-free and its Label matches; .instance.conf
    DOGANY_AGENT_NAME -> com.telegram-skill-bot.<name>. The plist for a
    non-marker label is the bridge/*.plist (not *.watchdog.plist) whose Label
    matches. ('', None, 'none') when nothing resolves -- never a guess."""
    root = Path(root)
    marker_label, marker_plist = "", None
    try:
        lines = (root / SERVICE_MARKER_REL).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
        mp = Path(lines[0].strip())
        try:
            ok = mp.is_file() and not PLACEHOLDER_RE.search(
                mp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            ok = False
        if ok and plist_label(mp) == lines[1].strip():
            marker_label, marker_plist = lines[1].strip(), str(mp)

    def _find_plist(label):
        if label == marker_label and marker_plist:
            return marker_plist
        for p in sorted((root / "bridge").glob("*.plist")):
            if p.name.endswith(".watchdog.plist"):
                continue
            if plist_label(p) == label:
                return str(p)
        return None

    if override:
        return override, _find_plist(override), "override"
    if marker_label:
        return marker_label, marker_plist, "service-marker"
    name = _conf_value(root, "DOGANY_AGENT_NAME").strip()
    if name and "__" not in name:
        label = LABEL_PREFIX + name
        return label, _find_plist(label), "instance-conf"
    return "", None, "none"


# --- launchd probes (read only) ----------------------------------------------

def launchd_list_row(label):
    """(available, pid_or_None, last_exit_or_None) from `launchctl list`."""
    rc, out = _run([_launchctl(), "list"])
    if rc is None:
        return False, None, None
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 3 and parts[2].strip() == label:
            pid = int(parts[0]) if parts[0].strip().isdigit() else None
            try:
                last = int(parts[1])
            except ValueError:
                last = None
            return True, pid, last
    return True, None, None


def launchd_domain_services(timeout=45):
    """(services {label: pid_or_None}, error) from `launchctl print gui/<uid>`.

    DGN-1482 P1a (`dogany agent list`): one domain read for every label, the
    row grammar of mint_retire.py service_labels (DGN-1636: the status column
    may be a "(pe)" token). Fail closed: a non-zero rc, a foreign answer or an
    unparseable row returns (None, why) -- an unreadable domain is UNKNOWN,
    never "not loaded" (`launchctl list` answers rc 1 and no rows inside a
    sandbox while the domain print works)."""
    domain = "gui/%d" % os.getuid()
    try:
        p = subprocess.run([_launchctl(), "print", domain], capture_output=True,
                           text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "launchctl not runnable: %s" % e
    if p.returncode != 0:
        return None, "launchctl print %s rc %d: %s" % (
            domain, p.returncode, (p.stderr or p.stdout).strip()[:200])
    if not p.stdout.startswith(domain + " = {"):
        return None, "unrecognized domain response for " + domain
    block = re.search(r"\n\s*services = \{\n(.*?)\n\s*\}", p.stdout, re.S)
    if block is None:
        return None, "cannot enumerate services of " + domain
    services = {}
    for line in block[1].splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*(\d+|-)\s+(?:-?\d+|-|\(\w+\))\s+(.+?)\s*", line)
        if m is None:
            return None, "unknown launchd services row: " + line.strip()[:120]
        services[m.group(2)] = int(m.group(1)) if m.group(1).isdigit() else None
    return services, None


def launchd_loaded(label):
    """True/False from `launchctl print gui/<uid>/<label>`; None if no launchctl."""
    rc, _ = _run([_launchctl(), "print", "gui/%d/%s" % (os.getuid(), label)])
    if rc is None:
        return None
    return rc == 0


def launchd_disabled(label):
    """True/False from `launchctl print-disabled gui/<uid>`; None if unknown."""
    rc, out = _run([_launchctl(), "print-disabled", "gui/%d" % os.getuid()])
    if rc is None or rc != 0:
        return None
    pat = re.compile(r'"%s"\s*=>\s*(\w+)' % re.escape(label))
    m = pat.search(out)
    if not m:
        return False
    return m.group(1).lower() in ("true", "disabled")


def read_lock_holder(root):
    """bridge.lock record + liveness. Never takes the lock."""
    p = Path(root) / LOCK_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    pid = doc.get("pid")
    return {"pid": pid, "host": doc.get("host"), "started_at": doc.get("started_at"),
            "alive": _pid_alive(pid)}


def heartbeat_age(root, now=None):
    try:
        mt = (Path(root) / HEARTBEAT_REL).stat().st_mtime
    except OSError:
        return None
    return max(0, int((time.time() if now is None else now) - mt))


# --- verbs -------------------------------------------------------------------

def host_report(root):
    r = resolve_host(root)
    return {"root": str(root), "host": r["host"], "source": r["source"],
            "policy": r["policy"], "warnings": r["warnings"]}


def status_report(root, label=None):
    """{host, running, pid, since, last_exit, heartbeat_age} (design 4.2)."""
    r = resolve_host(root)
    label, _plist, _src = resolve_label(root, label)
    warnings = list(r["warnings"])
    out = {"root": str(root), "host": r["host"], "label": label or None,
           "running": None, "pid": None, "since": None, "last_exit": None,
           "heartbeat_age": heartbeat_age(root), "warnings": warnings}
    holder = read_lock_holder(root)
    if r["host"] == "app":
        # P5 adds the app backend (host-status file probe). Until then the
        # lock holder is the only evidence; never fall back to launchd here.
        warnings.append("app backend not available in this build (P5)")
        if holder and holder["alive"] and holder.get("host") == "app":
            out.update(running=True, pid=holder["pid"], since=holder.get("started_at"))
        return out
    if not label:
        warnings.append("bridge label unresolved")
        return out
    avail, pid, last = launchd_list_row(label)
    if not avail:
        warnings.append("launchctl unavailable")
        return out
    out["last_exit"] = last
    if pid is not None and _pid_alive(pid):
        out.update(running=True, pid=pid)
        if holder and holder.get("pid") == pid:
            out["since"] = holder.get("started_at")
    else:
        out["running"] = False
    return out


def _app_control(home):
    """(control_path_or_None, problem_or_None) from the app file.

    Minimal read of launch_host.{supported,control} only; the strict
    contract reader lives in scripts/lib/connection_files.py (P3)."""
    p = Path(home) / ".dogany" / "integrations" / APP_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "app file absent"
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "app file unreadable or not JSON"
    lh = doc.get("launch_host") if isinstance(doc, dict) else None
    if not isinstance(lh, dict) or lh.get("supported") is not True:
        return None, "launch_host not supported by app"
    ctl = lh.get("control")
    if not isinstance(ctl, str) or not os.path.isabs(ctl):
        return None, "launch_host.control missing or not absolute"
    return ctl, None


def _companion_entry(home, label):
    p = Path(home) / ".dogany" / "integrations" / COMPANION_FILE
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"present": False, "reason": "companion absent"}
    except (OSError, ValueError, UnicodeDecodeError):
        return {"present": False, "reason": "companion unreadable or not JSON"}
    agents = ((doc.get("launch") or {}) if isinstance(doc, dict) else {}).get("agents")
    if not isinstance(agents, list):
        return {"present": False, "reason": "no launch block"}
    for a in agents:
        if isinstance(a, dict) and a.get("id") == label:
            return {"present": True, "desired": a.get("desired"),
                    "lease_epoch": a.get("lease_epoch")}
    return {"present": False, "reason": "no entry for label"}


def doctor_report(root, label=None, home=None):
    """Lease, plist state, host CLI reachability, companion entry, flock
    holder (design 4.2). Problems are listed in `errors` (exit 1 from the
    CLI) and `warnings` (informational)."""
    home = home or os.environ.get("HOME", "")
    r = resolve_host(root)
    label, plist, label_src = resolve_label(root, label)
    errors, warnings = [], list(r["warnings"])
    rep = {"root": str(root), "host": r["host"], "source": r["source"],
           "policy": r["policy"], "lease": r["lease"],
           "label": label or None, "label_source": label_src, "plist": plist}

    if not label:
        errors.append("bridge label unresolved (no service marker, no DOGANY_AGENT_NAME)")
        loaded = disabled = None
    else:
        loaded, disabled = launchd_loaded(label), launchd_disabled(label)
    rep["plist_state"] = {"file": bool(plist and os.path.isfile(plist)),
                          "loaded": loaded, "disabled": disabled}
    if label and plist is None:
        errors.append("bridge plist not found for %s (I2: launchd fallback must stay on disk)" % label)
    if loaded is None and label:
        warnings.append("launchctl unavailable; plist load state unknown")

    ctl, why = _app_control(home)
    rep["host_cli"] = {"control": ctl,
                       "reachable": bool(ctl and os.path.isfile(ctl) and os.access(ctl, os.X_OK)),
                       "reason": why}
    if ctl and not rep["host_cli"]["reachable"]:
        rep["host_cli"]["reason"] = "control not an executable file"

    rep["companion"] = _companion_entry(home, label) if label else {"present": False}
    holder = read_lock_holder(root)
    rep["lock_holder"] = holder

    wd = _conf_value(root, "DOGANY_WATCHDOG").strip().strip("'\"").lower()
    rep["watchdog_off"] = wd in WATCHDOG_OFF

    # Cross-checks (invariants I1, I2, R2).
    if r["host"] == "launchd":
        if disabled is True:
            errors.append("host=launchd but the bridge label is launchctl-disabled (zero launchers)")
        if rep["companion"].get("present") and rep["companion"].get("desired") == "running":
            errors.append("host=launchd but companion asks the app to run this agent (two launchers)")
    else:
        if loaded is True:
            errors.append("host=app but the launchd bridge label is still loaded (two launchers)")
        if not rep["host_cli"]["reachable"]:
            errors.append("host=app but the app host CLI is not reachable (%s)" % rep["host_cli"]["reason"])
        if rep["watchdog_off"]:
            errors.append("host=app with DOGANY_WATCHDOG=off: no fallback detector (R2)")
    if holder and holder["alive"] and holder.get("host") in HOSTS and holder.get("host") != r["host"]:
        errors.append("bridge.lock holder pid %s was started by %s but host=%s"
                      % (holder.get("pid"), holder.get("host"), r["host"]))
    rep["errors"] = errors
    rep["warnings"] = warnings
    rep["ok"] = not errors
    return rep


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="launch_host.py")
    sub = ap.add_subparsers(dest="verb")
    for v in ("host", "status", "doctor", "label"):
        s = sub.add_parser(v)
        s.add_argument("root")
        s.add_argument("--label", default=None)
        s.add_argument("--home", default=None)
    lw = sub.add_parser("lease-write")
    lw.add_argument("root")
    lw.add_argument("--host", required=True, choices=HOSTS)
    lw.add_argument("--by", required=True)
    lw.add_argument("--reason", required=True)
    lw.add_argument("--state", default="steady")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not a.verb:
        ap.print_usage(sys.stderr)
        return 2
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print(json.dumps({"error": "instance root not a directory", "root": root}))
        return 1
    if a.verb == "host":
        rep = host_report(root)
    elif a.verb == "status":
        rep = status_report(root, a.label)
    elif a.verb == "doctor":
        rep = doctor_report(root, a.label, a.home)
        print(json.dumps(rep, indent=2, ensure_ascii=True))
        return 0 if rep["ok"] else 1
    elif a.verb == "label":
        label, plist, src = resolve_label(root, a.label)
        rep = {"label": label or None, "plist": plist, "source": src}
        print(json.dumps(rep, ensure_ascii=True))
        return 0 if label else 1
    else:
        rep = write_lease(root, a.host, a.by, a.reason, a.state)
    print(json.dumps(rep, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
