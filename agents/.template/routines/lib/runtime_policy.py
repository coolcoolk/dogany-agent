#!/usr/bin/env python3
"""May a refused run move from pool A to pool B? (DGN-2180, owner 2026-10-19, dec-497)

Pure decision: no I/O, no vendor names. Pools are described by the traits file
(lib/runtime-traits.tsv: account, seat, role main/background, window, credit
funded); dispatch-detached.sh feeds the facts (pace, credit left, why the
preferred pool refused) and acts on the verdict.

Owner: Claude is the main chat model -- background work may use it only while
its usage is under the elapsed-time pace line. The other account resets often
-- use it aggressively. No wait timers: a refused run queues on its preferred
pool instead of walking to a different subscription.

Rule, in order:
  1 stay      from == to -> allow.
  2 safety    a safety refusal (reason class "safety", e.g. hooks untrusted)
              never moves anywhere.
  3 exempt    a credit-funded destination with credit left -> allow (its own
              buffer logic is the dispatcher's, unchanged).
  4 account   a destination on ANOTHER account (subscription) -> deny, whatever
              refused the preferred pool: queue on the preferred pool.
  5 seat      the same account's other seat: a background destination -> allow;
              a main destination needs the account under pace (unknown pace
              = not under pace).

CLI:  runtime_policy.py check --traits F --from A --to B [--reason-class C]
          [--pace N|unknown] [--elapsed N|unknown] [--no-credit POOL]...
          [--pace-tol N]
      -> "allow|deny<TAB>code<TAB>detail"
      runtime_policy.py pace --traits F --pool P --used U --resets-at R [--now T]
      -> pace points (elapsed% - used%) or "unknown"
"""
import argparse
import sys
import time
from datetime import datetime

BAND_START = 30.0        # weekly allowance at the start; narrows to zero at reset
SAFETY = "safety"
FIELDS = ("pool", "account", "seat", "role", "reset_style", "window_days", "credit_funded")


def parse_traits(text):
    """TSV text -> {pool: {field: value}}; '#' lines and the header row skipped."""
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.rstrip("\n").split("\t")
        if cols[0] == "pool":
            continue
        if len(cols) != len(FIELDS):
            raise ValueError(f"traits row needs {len(FIELDS)} columns: {line!r}")
        row = dict(zip(FIELDS, cols))
        row["credit_funded"] = row["credit_funded"] == "1"
        out[row["pool"]] = row
    return out


def under_pace(pace, elapsed, tol=BAND_START):
    """Whether a weekly pace is inside the front-wide allowance band."""
    return (pace is not None and elapsed is not None
            and pace + 1e-9 >= -tol * (1.0 - elapsed / 100.0))


def may_move(traits, src, dst, reason_class="capacity", pace=None, elapsed=None,
             no_credit=(), tol=BAND_START):
    """-> (allowed, code, detail). `pace` is the destination account's
    elapsed% - used% (None = unknown); `no_credit` lists credit-funded pools
    whose credit is spent (they then count as ordinary subscription pools)."""
    if src not in traits or dst not in traits:
        return False, "unknown_pool", f"{src}->{dst}: pool missing from traits"
    a, b = traits[src], traits[dst]
    if src == dst:
        return True, "stay", f"{dst}: same pool"
    if reason_class == SAFETY:
        return False, "safety_never_moves", f"{src} refused for safety; {dst} not considered"
    if b["credit_funded"] and dst not in no_credit:
        return True, "credit_exempt", f"{dst} is credit-funded with credit left"
    if a["account"] != b["account"]:
        return (False, "other_subscription",
                f"{dst} is another subscription than {src}: queue on {src}")
    if b["role"] != "main":
        return True, "same_account_seat", f"{dst}: same account, background"
    if under_pace(pace, elapsed, tol):
        shown = "unknown" if pace is None else f"{pace:+.0f}"
        return True, "main_under_pace", f"{dst}: main pool under pace ({shown})"
    limit = None if elapsed is None else -tol * (1.0 - elapsed / 100.0)
    shown = "unknown" if pace is None or limit is None else f"{pace:+.0f} < {limit:+.0f}"
    return False, "main_off_pace", f"{dst} is a main pool and off pace ({shown}): queue on {src}"


def pace_points(used, resets_at, window_secs, now):
    """elapsed% - used% of one window; unknown reset counts elapsed as 0."""
    try:
        reset = float(resets_at)
    except (TypeError, ValueError):
        try:
            reset = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    to_reset = max(0.0, reset - now)
    if to_reset > window_secs:
        return None
    return (window_secs - to_reset) / window_secs * 100.0 - float(used)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--traits", required=True)
    c.add_argument("--from", dest="src", required=True)
    c.add_argument("--to", dest="dst", required=True)
    c.add_argument("--reason-class", default="capacity")
    c.add_argument("--pace", default="unknown")
    c.add_argument("--elapsed", default="unknown")
    c.add_argument("--no-credit", action="append", default=[])
    c.add_argument("--pace-tol", type=float, default=BAND_START)
    q = sub.add_parser("pace")
    q.add_argument("--traits", required=True)
    q.add_argument("--pool", required=True)
    q.add_argument("--used", required=True)
    q.add_argument("--resets-at", default="")
    q.add_argument("--now", type=float, default=None)
    a = p.parse_args(argv)
    with open(a.traits, encoding="utf-8") as f:
        traits = parse_traits(f.read())
    if a.cmd == "pace":
        days = float(traits[a.pool]["window_days"])
        now = a.now if a.now is not None else time.time()
        v = pace_points(a.used, a.resets_at, days * 86400, now)
        print("unknown" if v is None else f"{v:.2f}")
        return 0
    try:
        pace = float(a.pace)
    except ValueError:
        pace = None
    try:
        elapsed = float(a.elapsed)
    except ValueError:
        elapsed = None
    ok, code, detail = may_move(traits, a.src, a.dst, a.reason_class, pace, elapsed,
                                a.no_credit, a.pace_tol)
    print(f"{'allow' if ok else 'deny'}\t{code}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
