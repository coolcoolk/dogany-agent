#!/usr/bin/env python3
"""minting_policy.py -- pure recommendation policy for the mint-agent module.

DGN-243 / DGN-238 decision 10, implemented verbatim:
- evaluated ONLY at weekly review (caller = weekly_eval.py from weekly-review.sh).
- threshold: last-2-weeks domain conversation share >= 20% OR accumulated
  records >= 30, AND no dedicated agent for the domain.
- max 2 recommendations per domain.
- 1st decline -> 4-week (28-day) cooldown.
- 2nd recommendation only when the metric EXCEEDS 1.5x threshold
  (share > 30% OR records > 45; "1.5배 초과" -> strict greater-than).
- 2nd decline -> permanent silence (user-initiated minting only).
- decline history persisted (state file owned by minting_state.py callers).

Conservative defaults for spec gaps (recorded as OPEN QUESTIONS in DGN-243):
- a recommendation with NO user response counts against the cap and starts
  the same 28-day spacing from its own date (prevents weekly re-fire from
  burning the 2nd slot).
- when several domains qualify in the same week, only the top one fires
  (ordered by share desc, then records desc) -- anti-spam.

Pure functions only: no file IO here except the shared state load/save
helpers (atomic write) used by the CLI scripts.
"""

import contextlib
import datetime as _dt
import fcntl
import json
import os
import tempfile
import time

DEFAULT_CFG = {
    "share_threshold": 0.20,
    "records_threshold": 30,
    "escalation_mult": 1.5,
    "cooldown_days": 28,
    "max_recommendations": 2,
}

STATE_VERSION = 1


# ---------------------------------------------------------------- state io
def empty_state():
    return {"version": STATE_VERSION, "domains": {}}


def load_state(path):
    if not os.path.exists(path):
        return empty_state()
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("version", STATE_VERSION)
    state.setdefault("domains", {})
    return state


LOCK_WAIT_S = 30.0


@contextlib.contextmanager
def state_lock(path, wait_s=LOCK_WAIT_S):
    """ONE exclusive lock for every writer of a minting-state.json (DGN-1683
    grill 4). The lock file is a stable sidecar (<state>.lock, never replaced
    -- save_state replaces the state file itself, so locking it would lock a
    dead inode). Callers hold it across load -> modify -> save; a lost write
    in either direction (accept vs weekly reap vs compensation) is exactly
    what an unlocked read-modify-write allows. Blocks up to wait_s, then
    raises TimeoutError: a stuck writer is a loud failure, never a skipped
    lock."""
    lock_path = os.path.abspath(path) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        end = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise TimeoutError(f"minting state lock busy: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)                      # closing the fd drops the flock


def save_state(path, state):
    """Atomic write: tmp file in the same dir, then rename."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".minting-state.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def domain_state(state, domain):
    return state["domains"].setdefault(
        domain, {"recommendations": [], "declines": [], "dedicated_agent": None}
    )


# ---------------------------------------------------------------- policy
def _parse_date(s):
    return _dt.date.fromisoformat(s)


def evaluate_domain(domain, metrics, dstate, today, cfg=None):
    """Evaluate ONE domain. Returns a decision dict.

    metrics: {"share": float 0..1, "records": int}
    dstate:  {"recommendations": [iso-date...], "declines": [iso-date...],
              "dedicated_agent": str|None}
    today:   datetime.date
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    share = float(metrics.get("share", 0.0))
    records = int(metrics.get("records", 0))
    recs = sorted(dstate.get("recommendations", []))
    declines = sorted(dstate.get("declines", []))

    def out(decision, reason, rec_no=None):
        return {
            "domain": domain,
            "decision": decision,
            "reason": reason,
            "rec_no": rec_no,
            "share": round(share, 4),
            "records": records,
        }

    if dstate.get("dedicated_agent"):
        return out("skip", "dedicated-agent-exists")
    if len(declines) >= 2:
        return out("skip", "silenced-permanent")
    if dstate.get("silenced"):
        return out("skip", "silenced-explicit")

    # Same-day re-fire (weekly review re-run): repeat the block, no new slot.
    if recs and recs[-1] == today.isoformat():
        return out("recommend", "refire-same-day", rec_no=len(recs))

    if len(recs) >= cfg["max_recommendations"]:
        return out("skip", "max-recommendations-reached")

    if not recs:
        if share >= cfg["share_threshold"] or records >= cfg["records_threshold"]:
            return out("recommend", "threshold-met", rec_no=1)
        return out("skip", "below-threshold")

    # Second (last) recommendation lane.
    last_event = max(recs[-1], declines[-1] if declines else recs[-1])
    if (today - _parse_date(last_event)).days < cfg["cooldown_days"]:
        return out("skip", "cooldown")
    esc_share = cfg["share_threshold"] * cfg["escalation_mult"]
    esc_records = cfg["records_threshold"] * cfg["escalation_mult"]
    # "1.5x EXCEEDED" -> strict greater-than.
    if share > esc_share or records > esc_records:
        return out("recommend", "escalated-threshold-exceeded", rec_no=2)
    return out("skip", "below-escalated-threshold")


def evaluate_all(metrics_by_domain, state, today, cfg=None):
    """Evaluate every domain; keep only the top-1 recommendation (anti-spam).

    Returns (winner_or_None, all_decisions).
    """
    decisions = []
    for domain, metrics in sorted(metrics_by_domain.items()):
        d = evaluate_domain(domain, metrics, domain_state(state, domain), today, cfg)
        decisions.append(d)
    fired = [d for d in decisions if d["decision"] == "recommend"]
    fired.sort(key=lambda d: (-d["share"], -d["records"], d["domain"]))
    winner = fired[0] if fired else None
    for d in fired[1:]:
        d["decision"] = "skip"
        d["reason"] = "deferred-top1"
    return winner, decisions
