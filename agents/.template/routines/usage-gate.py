#!/usr/bin/env python3
"""PreToolUse plan-aware usage gate for heavy dispatches (DGN-546).

Sibling of token-gate.py (approval gate); this one gates on ACCOUNT USAGE.
Fires only on heavy dispatches (Task/Agent/Workflow tools, deep-research
skill). Checks cached 5h/7d window utilization against plan-specific
thresholds (PLAN= in config/agent.conf) and denies when a window is too
hot, so one big dispatch never burns the headroom daily work and
user-facing crons need. Below threshold: fully silent (zero injection).
7d burn-rate produces a NON-blocking one-line hint only (never blocks).
Usage comes from a TTL file cache refreshed via claude-usage.sh --json
with a hard timeout. Fails open (allow) on any error: never blocks a
dispatch on plumbing. One-shot approval sentinel bypasses a deny.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HEAVY_TOOLS = {"Workflow", "Agent", "Task"}
HEAVY_SKILLS = {"deep-research"}

CACHE_NAME = ".usage-cache.json"
CACHE_TTL_SEC = 300
SENTINEL_NAME = ".usage-gate-approved"
SENTINEL_TTL_SEC = 600
FETCH_TIMEOUT_SEC = 3

SEVEN_DAYS_SEC = 7 * 86400

# plan slug -> (thr_5h, thr_7d) in utilization %. Spec: DGN-546 section B.
# 7d threshold always above 5h: the 5h window resets cheap (pace signal),
# the 7d window resets weekly (lockout costs days) but ~70% is normal pace,
# so only a thin top reserve is kept.
PLAN_THRESHOLDS = {
    "pro": (75, 88),
    "team_standard": (82, 91),
    "team_premium": (88, 94),
    "max_5x": (90, 95),
    "max_20x": (92, 96),
    "enterprise": (92, 96),
    "free": (60, 78),
}
DEFAULT_PLAN = "pro"  # unset/unknown -> conservative

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def read_plan(cwd):
    """PLAN= slug from config/agent.conf; unknown/missing -> pro."""
    candidates = [
        os.path.join(cwd, "config", "agent.conf"),
        os.path.join(SCRIPT_DIR, "..", "config", "agent.conf"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("PLAN="):
                        slug = line.split("=", 1)[1].strip()
                        return slug if slug in PLAN_THRESHOLDS else DEFAULT_PLAN
        except OSError:
            continue
    return DEFAULT_PLAN


def fetch_usage():
    """Refresh usage via claude-usage.sh --json (hard timeout, may raise)."""
    usage_sh = os.path.join(SCRIPT_DIR, "claude-usage.sh")
    out = subprocess.run(
        ["bash", usage_sh, "--json"],
        capture_output=True, text=True, timeout=FETCH_TIMEOUT_SEC,
    )
    if out.returncode != 0:
        raise RuntimeError("usage lookup failed")
    data = json.loads(out.stdout)
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    return {
        "five_hour_util": float(five.get("utilization", 0)),
        "seven_day_util": float(seven.get("utilization", 0)),
        "seven_day_resets_at": seven.get("resets_at", "") or "",
        "fetched_at": time.time(),
    }


def get_usage(cwd):
    """TTL file cache; stale/missing -> live refresh; None on any failure."""
    cache = os.path.join(cwd, ".claude", CACHE_NAME)
    try:
        if time.time() - os.path.getmtime(cache) <= CACHE_TTL_SEC:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass  # missing/stale/corrupt cache -> refresh
    try:
        usage = fetch_usage()
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(usage, f)
        os.replace(tmp, cache)
        return usage
    except Exception:
        return None  # fail open: never block a dispatch on the network


def consume_fresh_sentinel(cwd):
    """True if a fresh one-shot approval sentinel existed (and was consumed)."""
    sentinel = os.path.join(cwd, ".claude", SENTINEL_NAME)
    try:
        if time.time() - os.path.getmtime(sentinel) <= SENTINEL_TTL_SEC:
            os.remove(sentinel)
            return True
    except OSError:
        pass
    return False


def emit(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def burn_rate_hint(plan, sd, resets_at):
    """One-line 7d burn-rate hint, or None. Spec section C: never blocks."""
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    now = datetime.now(timezone.utc)
    # window elapsed fraction: window start = resets_at - 7d
    e = (now.timestamp() - (reset_dt.timestamp() - SEVEN_DAYS_SEC)) / SEVEN_DAYS_SEC
    if e < 0.15:
        return None  # dead zone: too early in the window to project
    p = sd / max(e, 0.1)
    if p < 110 or sd < 50:
        return None
    days_left = round((100.0 - sd) * (e * 7.0) / sd, 1)
    return (
        "Usage pace hint (plan " + plan + ", non-blocking): 7d window at "
        + str(int(round(sd))) + "% with " + str(int(round(e * 100)))
        + "% of the window elapsed. At this pace the 7d window exhausts "
        "before reset; ~" + str(days_left) + " days left. Proceeding; "
        "consider pacing heavy dispatches."
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: never break the agent on bad input

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    skill = (
        tool_input.get("skill")
        or tool_input.get("name")
        or tool_input.get("skill_name")
        or ""
    )
    is_heavy = tool_name in HEAVY_TOOLS or (
        tool_name == "Skill" and skill in HEAVY_SKILLS
    )
    if not is_heavy:
        sys.exit(0)  # pass through

    usage = get_usage(cwd)
    if usage is None:
        sys.exit(0)  # fail open
    try:
        fh = float(usage.get("five_hour_util", 0))
        sd = float(usage.get("seven_day_util", 0))
    except (TypeError, ValueError):
        sys.exit(0)

    plan = read_plan(cwd)
    thr_5h, thr_7d = PLAN_THRESHOLDS[plan]

    if fh >= thr_5h or sd >= thr_7d:
        if consume_fresh_sentinel(cwd):
            sys.exit(0)  # user-approved one-shot: allow silently
        label = skill or tool_name
        sentinel = os.path.join(cwd, ".claude", SENTINEL_NAME)
        emit("deny", (
            "Heavy dispatch '" + label + "' blocked by plan-aware usage gate. "
            "Plan " + plan + ": five_hour=" + str(int(round(fh))) + "% "
            "(threshold " + str(thr_5h) + "%), seven_day=" + str(int(round(sd)))
            + "% (threshold " + str(thr_7d) + "%). The usage window is too hot "
            "for a big dispatch -- it would burn the headroom daily work needs. "
            "STOP and get user approval first (state the cost and the remaining "
            "window). After the user approves, run: touch '" + sentinel + "' "
            "then retry."
        ))
        sys.exit(0)

    hint = burn_rate_hint(plan, sd, usage.get("seven_day_resets_at", ""))
    if hint:
        emit("allow", hint)
    sys.exit(0)  # below threshold and no hint: fully silent


if __name__ == "__main__":
    main()
