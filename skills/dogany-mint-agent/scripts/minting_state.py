#!/usr/bin/env python3
"""minting_state.py -- CRUD for the mint-agent decline/recommendation state.

State file: <agent>/config/minting-state.json (non-secret, per-instance,
survives re-mint -- not part of the template rsync set). This module's
scripts are the ONLY writers.

Commands:
  show [--json]                     print state summary; refuse stale bindings
  validate                          read-only existence check for mint preflight
  decline <domain> [--date D]       record a user decline (1st -> 4-week
                                    cooldown, 2nd -> permanent silence)
  accept <domain> --agent <slug> --path <dir>
                                    record mint completion (dedicated agent
                                    exists -> never recommend again). <dir> is
                                    the actual landing directory (DGN-1505:
                                    minting knows where it put the agent --
                                    write it down, so existence checks later
                                    read a fact instead of guessing a sibling
                                    location; estate has 2+ agent roots and
                                    guessing one is wrong for the other).
                                    DGN-1725: also writes the newborn's
                                    parent pointer <dir>/config/
                                    minting-parent.json (this --state file),
                                    so retirement finds the ledger to clear
                                    without being told the parent instance.
  clear <domain> --agent <slug> [--path <dir>]
                                    undo accept (DGN-1480): mirrors accept --
                                    clears dedicated_agent + minted_at +
                                    agent_path, leaves recs/declines untouched.
                                    Run this when the minted agent is deleted,
                                    else the ghost dedicated_agent blocks the
                                    domain's recommendation forever. --agent
                                    must match the recorded slug (refuses on
                                    mismatch -- guards against clearing the
                                    wrong domain). With --path (DGN-1683
                                    compensation) the recorded agent_path
                                    must ALSO resolve to <dir>: slug and
                                    physical path both, or refuse.
  silence <domain>                  explicit permanent silence on user demand
                                    ("추천 그만해")
  record-rec <domain> [--date D]    append a recommendation date (normally
                                    done by weekly_eval.py, exposed for repair)
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import minting_policy as pol  # noqa: E402


def _default_agent_dir_guess():
    # __file__-derived LAST-RESORT guess: scripts -> mint-agent -> skills ->
    # .claude -> <agent root>, OR ... -> consume root -> <agent root> when
    # reached through the activation symlink into $DOGANY_CONSUME_ROOT
    # (DGN-1513) -- realpath() resolves the symlink, so this only lands on
    # the real instance root if DOGANY_CONSUME_ROOT sits exactly one path
    # segment below it. Never trusted directly -- see resolve_agent_dir().
    return os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))


def resolve_agent_dir():
    """Instance-root resolution ladder (DGN-1513 w1), same shape as
    weekly_eval.py's resolve_agent_dir(): $DOGANY_INSTANCE_ROOT > cwd's own
    .instance.conf > the __file__-derived guess, validated (.instance.conf
    must exist there) before use. Only reached when --state was not given
    explicitly (an explicit --state always wins outright, see main()).
    """
    env = os.environ.get("DOGANY_INSTANCE_ROOT")
    if env:
        return os.path.abspath(env)
    cwd = os.getcwd()
    if os.path.isfile(os.path.join(cwd, ".instance.conf")):
        return cwd
    guess = _default_agent_dir_guess()
    if not os.path.isfile(os.path.join(guess, ".instance.conf")):
        print("ERROR: could not resolve the instance root for the default "
              "--state path (no DOGANY_INSTANCE_ROOT, cwd is not an "
              f"instance root, and the derived guess {guess!r} has no "
              ".instance.conf) -- pass --state explicitly (DGN-1513 w1)",
              file=sys.stderr)
        sys.exit(1)
    return guess


def default_state_path():
    return os.path.join(resolve_agent_dir(), "config", "minting-state.json")


PARENT_RECORD = os.path.join("config", "minting-parent.json")


def write_parent_record(agent_dir, slug, domain, state_path):
    """DGN-1725: record, inside the newborn, which ledger holds its binding.

    Best effort by design: the ledger row is already saved, and retirement
    refuses to report complete when it cannot resolve the parent, so a
    missing pointer degrades to an explicit retire.partial, never to a
    silently kept binding. Returns an error string or None.
    """
    path = os.path.join(agent_dir, PARENT_RECORD)
    state = os.path.realpath(state_path)
    try:
        record = {}
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path) as f:
                record = json.load(f)
        if not isinstance(record, dict) or record.get("agent") != slug or \
                record.get("state") != state:
            record = {}
        domains = set(record.get("domains") or [])
        domains.add(domain)
        record = {"schema": 1, "agent": slug, "state": state,
                  "domains": sorted(domains)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp-%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        return str(e)
    return None


def stale_bindings(state):
    """An unverifiable legacy binding is stale too; never guess its location."""
    return [domain for domain, row in state.get("domains", {}).items()
            if row.get("dedicated_agent") and
            (not isinstance(row.get("agent_path"), str) or
             not os.path.isabs(row["agent_path"]) or
             not os.path.isdir(row["agent_path"]))]


def stance(ds, today):
    """Human-readable stance for one domain."""
    if ds.get("dedicated_agent"):
        return f"minted -> {ds['dedicated_agent']} (no more recommendations)"
    if ds.get("silenced") or len(ds.get("declines", [])) >= 2:
        return "silenced permanently (user-initiated minting only)"
    declines = sorted(ds.get("declines", []))
    recs = sorted(ds.get("recommendations", []))
    if len(recs) >= 2:
        return "recommendation cap reached (2/2)"
    if recs or declines:
        last = max(recs + declines)
        until = dt.date.fromisoformat(last) + dt.timedelta(days=28)
        if today < until:
            return f"cooldown until {until.isoformat()} (then 1.5x threshold)"
        return "eligible for 2nd (last) recommendation at 1.5x threshold"
    return "open (no history)"


def main():
    ap = argparse.ArgumentParser(description="mint-agent state CRUD")
    ap.add_argument("command",
                    choices=["show", "validate", "decline", "accept", "clear", "silence",
                             "record-rec"])
    ap.add_argument("domain", nargs="?")
    ap.add_argument("--agent", default=None, help="minted agent slug (accept)")
    ap.add_argument("--path", default=None,
                    help="landing directory (accept; DGN-1505 existence-check anchor)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD override")
    ap.add_argument("--state", default=None,
                    help="state json path override (DGN-1513 w1: default is "
                         "a validated resolution ladder, not a bare guess)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    state_path = args.state or default_state_path()
    # DGN-1683 grill 4: every mutating command runs load -> modify -> save
    # under the ONE shared state lock (weekly_eval.py and the mint flow's
    # accept compensation take the same lock).
    mutating = args.command not in ("show", "validate")
    with (pol.state_lock(state_path) if mutating else contextlib.nullcontext()):
        return _run(args, state_path)


def _run(args, state_path):
    today = dt.date.today()
    date = args.date or today.isoformat()
    state = pol.load_state(state_path)

    if args.command in ("show", "validate"):
        stale = stale_bindings(state)
        if stale:
            print("mint.ledger-stale: dedicated agent has no existing absolute "
                  "agent_path: " + ", ".join(sorted(stale)) +
                  "; retire or clear the stale binding before minting", file=sys.stderr)
            return 1
        if args.command == "validate":
            return 0

    if args.command == "show":
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            if not state["domains"]:
                print("(empty state -- no minting history)")
            for domain, ds in sorted(state["domains"].items()):
                print(f"{domain}: {stance(ds, today)}  "
                      f"recs={ds.get('recommendations', [])} "
                      f"declines={ds.get('declines', [])}")
        return 0

    if not args.domain:
        print("ERROR: <domain> required", file=sys.stderr)
        return 1
    ds = pol.domain_state(state, args.domain)

    if args.command == "decline":
        ds["declines"].append(date)
    elif args.command == "accept":
        if not args.agent:
            print("ERROR: accept requires --agent <slug>", file=sys.stderr)
            return 1
        if not args.path:
            print("ERROR: accept requires --path <dir> (DGN-1505: existence "
                  "checks read this back instead of guessing a sibling "
                  "location)", file=sys.stderr)
            return 1
        path = os.path.abspath(args.path)
        if not os.path.isdir(path):
            print(f"ERROR: --path {path!r} is not a directory", file=sys.stderr)
            return 1
        ds["dedicated_agent"] = args.agent
        ds["minted_at"] = date
        ds["agent_path"] = path
        pol.save_state(state_path, state)
        err = write_parent_record(path, args.agent, args.domain, state_path)
        if err:
            print(f"WARN: parent record {os.path.join(path, PARENT_RECORD)!r} "
                  f"not written ({err}); retiring {args.agent} will need "
                  f"--instance-root for this ledger (DGN-1725)", file=sys.stderr)
        print(f"{args.domain}: {stance(ds, today)}")
        return 0
    elif args.command == "clear":
        if not args.agent:
            print("ERROR: clear requires --agent <slug>", file=sys.stderr)
            return 1
        if ds.get("dedicated_agent") != args.agent:
            print(f"ERROR: recorded dedicated_agent is "
                  f"{ds.get('dedicated_agent')!r}, not {args.agent!r} -- "
                  f"refusing (wrong slug or domain, or already cleared)",
                  file=sys.stderr)
            return 1
        if args.path is not None:
            recorded = ds.get("agent_path")
            if not isinstance(recorded, str) or \
                    os.path.realpath(recorded) != os.path.realpath(args.path):
                print(f"ERROR: recorded agent_path is {recorded!r}, not "
                      f"{args.path!r} -- refusing (slug matches, path does not)",
                      file=sys.stderr)
                return 1
        ds["dedicated_agent"] = None
        ds.pop("minted_at", None)
        ds.pop("agent_path", None)
    elif args.command == "silence":
        ds["silenced"] = True
    elif args.command == "record-rec":
        ds["recommendations"].append(date)

    pol.save_state(state_path, state)
    print(f"{args.domain}: {stance(ds, today)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
