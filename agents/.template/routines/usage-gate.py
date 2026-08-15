#!/usr/bin/env python3
"""PreToolUse plan-aware usage gate for heavy dispatches (DGN-546, DGN-835).

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

DGN-835 over-blocking relief (three additive layers on the deny path):
  1. MUST-USE editor exemption: allowlisted lightweight subagent types on
     cheap models pass silently below an absolute backstop cap.
  2. Reset-proximity relief (7d window ONLY; opt-in per calibrated plan,
     currently max_20x only): over-threshold dispatches may still pass when
     the 7d reset is near, bounded by RELIEF_CAP and a flock-atomic
     time-bucket dispatch ledger (herd control; the bucket is shared across
     cache misses so a parallel fan-out cannot reset the counter -- N1).
  3. Sentinel is an integer COUNT (touch/empty = 1, "echo N >" = N) so
     one approval can cover a parallel fan-out of N dispatches.
State files (cache/sentinel/ledger) are anchored to the WORKSPACE ROOT
(SCRIPT_DIR/..), never the payload cwd -- a cwd-dependent path splits the
state across .claude/ vs <subdir>/.claude/ (observed DGN-835).

NOTE: instance-local routines/usage-gate.sh (autonomous-spawn front gate,
90/95 fail-CLOSED) is a SEPARATE surface and deliberately gets NO
relief/allowlist -- unattended spawns keep the conservative fail-closed
policy; this hook (live heavy dispatches, fail-open) is the only relief
surface (DGN-835 M3: explicit exclusion).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import fcntl  # POSIX only (macOS/Linux instances)
except ImportError:  # pragma: no cover - non-POSIX fallback: best-effort ledger
    fcntl = None

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

# ---- DGN-835: reset-proximity relief (7d window ONLY) ----
# RELIEF_CAP stays strictly BELOW the automatic-pipeline defer threshold
# (memory-engine/memory.py CONSOLIDATE_USAGE_EXHAUSTION_PCT = 99.0): the
# 97..99% band is an untouchable reserve for automatic work (consolidate
# etc.), so interactive relief can never starve it (DGN-835 F1).
RELIEF_CAP = 97.0
# Relief window: 7d reset must be within this horizon. 7d ONLY -- the 5h
# window is always <= 5h from reset, so applying T_RELIEF there would erase
# the 5h pace signal permanently (DGN-835 K1). 5h hot -> no relief, period.
T_RELIEF_SEC = 24 * 3600
# Estimated 7d-window cost of ONE heavy dispatch, in utilization points, per
# CALIBRATED plan. Enters the pass predicate as "projected position after
# this dispatch" (sd_eff + C <= RELIEF_CAP) so a snapshot pass cannot punch
# through the cap (DGN-835 F2).
# Relief is strictly OPT-IN per plan: a plan absent from this table is DENIED
# relief outright (relief_check bails first thing) and keeps the plain gate.
# Rationale (DGN-835 final-grill FATAL-1): the cap predicate compares against
# RELIEF_CAP (97) regardless of the plan's own thr_7d, so a small plan (e.g.
# pro, thr_7d=88) would open a [88, 97) relief band that no single
# uncalibrated C can police -- a small plan's real per-dispatch cost exceeds
# any guessed default and the F2 punch-through returns. Owner decision
# 2026-08-12: max_20x ONLY (C=0.5 conservative; the 97->99 2pt buffer absorbs
# estimation error). Enabling another plan = calibrate its real dispatch cost
# first, then register it here.
DISPATCH_COST = {"max_20x": 0.5}
# Relief ledger: counts relief-granted dispatches inside the SAME time bucket
# so a herd cannot all pass on one usage reading (each grant inflates sd_eff
# by C). The epoch key is a floor(now / CACHE_TTL_SEC) TIME BUCKET, NOT the
# per-hook fetched_at (DGN-835 N1): on a cache miss every parallel hook runs
# its own fetch_usage() and stamps a distinct fetched_at=time.time(), so a
# fetched_at-keyed ledger gave every hook n=0 -> 100% herd punch-through.
# All hooks arriving within one CACHE_TTL_SEC window share one bucket
# regardless of cache hit/miss, so the ledger accumulates and the herd is
# bounded. The bucket length equals the cache TTL, so it also naturally
# rotates roughly when the cached snapshot would refresh.
LEDGER_NAME = ".usage-relief-ledger.json"


def _relief_epoch_key(now=None):
    """DGN-835 N1: floor(now / CACHE_TTL_SEC) time bucket shared by every hook
    in the same window (cache-hit or cache-miss), so the flock-atomic ledger
    accumulates across a parallel fan-out instead of resetting per fetch."""
    return int((now if now is not None else time.time()) // CACHE_TTL_SEC)

# ---- DGN-835: MUST-USE lightweight editor exemption ----
# Baseline write-rules FORCE these subagents (AGENT.md "Baseline doc
# editing" etc.); blocking them pushes rule-deviating direct edits. Exempt
# only when the requested model is a cheap tier (fable/opus/unset -> the
# exemption is VOID: a big model wearing an allowlisted name gets no free
# ride) and both windows sit below the absolute backstop cap.
EXEMPT_SUBAGENTS = {
    "baseline-editor",
    "propagation-editor",
    "release-closer",
    "doctrine-reviewer",
}
EXEMPT_MODELS = {"sonnet", "haiku"}
# CAP_EXEMPT = 99 deliberately ALIGNS with the automatic-pipeline defer
# threshold (memory-engine/memory.py CONSOLIDATE_USAGE_EXHAUSTION_PCT):
# below 99 a sonnet/haiku single-file edit is negligible; at/above 99 the
# account is functionally exhausted and nothing should dispatch. Keep the
# two constants in lockstep (owner decision 2026-08-12, DGN-835).
CAP_EXEMPT = 99.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Workspace root anchor for ALL gate state (cache / sentinel / ledger).
# NEVER derive these from the payload cwd: subagents and worktree shells
# report differing cwds and the state fractures across .claude dirs.
WORKSPACE_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
STATE_DIR = os.path.join(WORKSPACE_ROOT, ".claude")


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
        # DGN-835 (fixes DGN-726 5h mis-schedule at the source): keep the 5h
        # reset boundary too. Additive cache key -- readers use .get(...,"")
        # so an old-shape cache file needs no migration.
        "five_hour_resets_at": five.get("resets_at", "") or "",
        "fetched_at": time.time(),
    }


def get_usage(cwd=None):
    """TTL file cache; stale/missing -> live refresh; None on any failure.

    cwd is accepted for backward compatibility (memory-engine/memory.py
    passes it) but IGNORED for path anchoring -- the cache always lives at
    <workspace root>/.claude/ (DGN-835 path-anchor migration).
    """
    cache = os.path.join(STATE_DIR, CACHE_NAME)
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


def consume_fresh_sentinel(cwd=None):
    """True if a fresh approval sentinel existed (one unit was consumed).

    DGN-835: the sentinel body is an integer COUNT of approved dispatches.
    Empty file / bare `touch` = 1 (legacy behavior preserved); `echo N >`
    = N, so one user approval can cover an N-wide parallel fan-out instead
    of dying after the first dispatch. Each consume decrements by one; the
    file is removed at zero. The original mtime is preserved on rewrite so
    the TTL window never silently extends.
    """
    sentinel = os.path.join(STATE_DIR, SENTINEL_NAME)
    try:
        st = os.stat(sentinel)
        if time.time() - st.st_mtime > SENTINEL_TTL_SEC:
            return False
        try:
            with open(sentinel, encoding="utf-8") as f:
                raw = f.read().strip()
            count = int(raw) if raw else 1
        except (OSError, ValueError):
            count = 1
        if count <= 1:
            os.remove(sentinel)
        else:
            tmp = sentinel + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(count - 1))
            os.replace(tmp, sentinel)
            os.utime(sentinel, (st.st_atime, st.st_mtime))
        return True
    except OSError:
        pass
    return False


def exempt_dispatch(tool_name, tool_input, fh, sd):
    """DGN-835 A2: MUST-USE lightweight editor allowlist.

    ALL three legs must hold: allowlisted subagent_type AND cheap model
    (explicitly requested; unset/fable/opus -> exemption VOID) AND both
    windows below CAP_EXEMPT. tool_input.subagent_type presence verified
    against live hook payloads (Agent/Task dispatch carries
    {description, model, prompt, subagent_type}).
    """
    if tool_name not in HEAVY_TOOLS:
        return False
    subagent = str(tool_input.get("subagent_type") or "").strip()
    model = str(tool_input.get("model") or "").strip().lower()
    if subagent not in EXEMPT_SUBAGENTS:
        return False
    if model not in EXEMPT_MODELS:
        return False
    return max(fh, sd) < CAP_EXEMPT


def _ledger_path():
    return os.path.join(STATE_DIR, LEDGER_NAME)


def ledger_count(epoch_key):
    """Relief grants already charged inside this TIME BUCKET (DGN-835 N1).

    epoch_key = floor(now / CACHE_TTL_SEC); a new bucket resets the count to
    0. Corrupt/missing ledger -> 0.
    """
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("epoch_key") == epoch_key:
            return max(0, int(d.get("count", 0)))
    except (OSError, ValueError, TypeError):
        pass
    return 0


def ledger_charge(epoch_key, sd, cost, thr_7d):
    """Atomically read+decide+bump the relief ledger for this TIME BUCKET
    (DGN-835 MAJOR-B + N1). The whole read-decide-write runs under
    fcntl.flock, and the bucket key is floor(now / CACHE_TTL_SEC) -- shared
    by every hook in the window whether it hit or missed the cache -- so a
    parallel fan-out cannot each stamp a distinct per-hook fetched_at, all
    observe n=0 and punch through (the N1 defect). Returns (n, sd_eff) on a
    granted charge (n = grants already recorded BEFORE this one), else None.

    Failure policy: any IO/lock error DENIES (returns None). Relief is a
    comfort layer on top of the gate, so it degrades to the plain deny path
    rather than risking an uncounted herd (deliberate inversion of the
    gate's own fail-open rule, which still applies to the gate itself).
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_ledger_path(), "a+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                d = json.load(f)
            except ValueError:
                d = {}
            n = 0
            if isinstance(d, dict) and d.get("epoch_key") == epoch_key:
                try:
                    n = max(0, int(d.get("count", 0)))
                except (TypeError, ValueError):
                    n = 0
            sd_eff = sd + n * cost
            if sd_eff < thr_7d:
                # ledger cannot pull below sd (n>=0), so this only re-states
                # cond 3; kept explicit to match the locked predicate verbatim.
                return None
            if sd_eff + cost > RELIEF_CAP:
                return None
            # in-place rewrite (NOT tmp+os.replace): swapping the inode would
            # detach waiters already flocked on the old fd. All mutators take
            # the same lock first, so truncate+write is race-free here.
            f.seek(0)
            f.truncate()
            json.dump({"epoch_key": epoch_key, "count": n + 1}, f)
            f.flush()
            return n, sd_eff
    except OSError:
        return None


def relief_check(plan, thr_5h, thr_7d, fh, sd, usage):
    """DGN-835 A1: reset-proximity relief. Returns an allow-reason string
    when relief passes (and charges the ledger), else None (caller denies).

    Gate 0 (FATAL-1): relief is opt-in per calibrated plan -- a plan absent
    from DISPATCH_COST is denied relief outright (small-plan bands would
    otherwise open up to RELIEF_CAP far past their own thresholds).

    Pass predicate (ALL five, spec-verbatim):
      1. fh < thr_5h              -- 5h window NOT hot (5h excluded from relief)
      2. 0 < R <= T_RELIEF_SEC    -- 7d reset within the relief horizon
      3. sd_eff >= thr_7d         -- genuinely in the blocked band
      4. sd_eff + C <= RELIEF_CAP -- projected post-dispatch position under cap
      5. cache success            -- parseable snapshot with a real fetched_at
    where sd_eff = sd + n*C (n = relief grants already charged in this TIME
    BUCKET via the flock-atomic ledger; conditions 3+4 are evaluated INSIDE
    the locked charge so parallel hooks serialize). The ledger key is a
    floor(now / CACHE_TTL_SEC) bucket, NOT the per-hook fetched_at, so a
    parallel fan-out shares one counter even across cache misses (N1).
    """
    if plan not in DISPATCH_COST:
        return None  # FATAL-1: uncalibrated plan -> plain gate, no relief
    if fh >= thr_5h:
        return None
    if sd < thr_7d:
        return None
    # condition 5: relief only on a healthy snapshot (fetched_at + reset time).
    # fetched_at gates the snapshot's validity ONLY -- it is NOT the ledger
    # key (that is a shared time bucket; see _relief_epoch_key / N1).
    fetched_at = usage.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    resets_at = usage.get("seven_day_resets_at", "") or ""
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    remaining = reset_dt.timestamp() - time.time()
    if not (0 < remaining <= T_RELIEF_SEC):
        return None
    cost = DISPATCH_COST[plan]
    charged = ledger_charge(_relief_epoch_key(), sd, cost, thr_7d)
    if charged is None:
        return None
    n, sd_eff = charged
    hours = int(remaining // 3600)
    minutes = int((remaining % 3600) // 60)
    return (
        "Usage relief (plan " + plan + ", reset-proximity): seven_day="
        + str(int(round(sd))) + "% >= threshold " + str(thr_7d) + "%, but the "
        "7d window resets in " + str(hours) + "h " + str(minutes) + "m "
        "(<= 24h) and the projected position after this dispatch ("
        + ("%.1f" % (sd_eff + cost)) + "%) stays under the "
        + str(int(RELIEF_CAP)) + "% relief cap (grant #" + str(n + 1)
        + " on this snapshot). Proceeding -- pace additional heavy dispatches; "
        "the 97-99% band is reserved for automatic pipelines."
    )


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

    # DGN-835 housekeeping: pre-anchor gate versions kept the usage CACHE
    # under the PAYLOAD cwd's .claude, fracturing it per-subdir
    # (worklog/.claude etc.). Sweep exactly that one orphan filename when the
    # payload cwd is not the workspace root. CACHE ONLY -- deliberately NOT
    # the approval sentinel: cwd may be ANOTHER instance's workspace root,
    # and deleting its sentinel would cancel a legitimate approval there
    # (its cache is just a 300s TTL artifact, safe to drop). Best-effort,
    # silent -- never blocks the dispatch.
    orphan_state = os.path.join(cwd, ".claude")
    if os.path.realpath(orphan_state) != os.path.realpath(STATE_DIR):
        try:
            os.remove(os.path.join(orphan_state, CACHE_NAME))
        except OSError:
            pass

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
        # DGN-835 layer 1: MUST-USE lightweight editors pass silently.
        if exempt_dispatch(tool_name, tool_input, fh, sd):
            sys.exit(0)
        # DGN-835 layer 2: reset-proximity relief (7d only, capped, ledgered).
        relief = relief_check(plan, thr_5h, thr_7d, fh, sd, usage)
        if relief:
            emit("allow", relief)
            sys.exit(0)
        # Layer 3: explicit user approval sentinel (integer count).
        if consume_fresh_sentinel():
            sys.exit(0)  # user-approved: allow silently
        label = skill or tool_name
        sentinel = os.path.join(STATE_DIR, SENTINEL_NAME)
        emit("deny", (
            "Heavy dispatch '" + label + "' blocked by plan-aware usage gate. "
            "Plan " + plan + ": five_hour=" + str(int(round(fh))) + "% "
            "(threshold " + str(thr_5h) + "%), seven_day=" + str(int(round(sd)))
            + "% (threshold " + str(thr_7d) + "%). The usage window is too hot "
            "for a big dispatch -- it would burn the headroom daily work needs. "
            "STOP and get user approval first (state the cost and the remaining "
            "window). After the user approves, run: touch '" + sentinel + "' "
            "then retry (for an N-wide parallel fan-out, write the count "
            "instead: echo N > '" + sentinel + "')."
        ))
        sys.exit(0)

    hint = burn_rate_hint(plan, sd, usage.get("seven_day_resets_at", ""))
    if hint:
        emit("allow", hint)
    sys.exit(0)  # below threshold and no hint: fully silent


if __name__ == "__main__":
    main()
