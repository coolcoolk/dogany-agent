#!/usr/bin/env python3
"""Claude-vs-Codex headroom policy for dispatch auto runtime selection (DGN-1874).

Owner 2026-10-06 (dec-254, paraphrased; verbatim in worklog DGN-1874): Claude
has two windows (5h + 7d) and Codex one (7d) -- weigh both; and when the
Claude 5h burn speed says it will run out, spill to Codex even at 5h ~50%.
The defect it replaces: auto selection read ONLY the codex
weekly quota and took codex whenever it was <= the owner ceiling, never
weighing Claude's headroom.

This module only DECIDES. It reads nothing live: dispatch-detached.sh feeds it
the existing readers' output -- usage-gate.py get_usage() (5h + 7d used% and
resets_at, 300s TTL cache) plus usage_history() samples on stdin, and the
codex_quota_gate() read (used%, resets_at, window) as arguments. The existing
gates (codex owner ceiling, tier binding, write-hook trust, claude usage-gate,
both-blocked defer) stay where they are; this only orders the two lanes.

Policy, in order:
  per window   pace = elapsed% - used% (positive = under pace). An unknown
               reset counts elapsed as 0 (conservative: pace = -used).
  1 lose-it    codex 7d resets within LOSE_IT_HOURS with >= LOSE_IT_UNUSED
               points unused -> codex (budget that is not spent is lost).
  1b claude    lose-it: Claude 7d resets no later than the 5h window and 7d used
               < CLAUDE_UIOLI_7D_MAX -> ignore the 5h burn projection and pace; claude
               unless ACTUAL 5h used >= CLAUDE_UIOLI_5H (then rules 2-4).
  2 claude     both Claude windows open (< OPEN_MAX) and the tighter of the
               5h/7d paces >= -PACE_TOL and the 5h velocity projection
               (used% + recent burn x time-to-reset) <= SPILL_PROJ -> claude.
               No burn-rate data -> the projection is skipped (used% only),
               and the detail says so.
  3 spill      Claude tight (closed / off pace / 5h projection) and codex
               open + on pace -> codex.
  4 both tight a Claude window at the usage-gate refusal line -> codex;
               otherwise the less tight lane by score (Claude = tighter of
               5h pace, 7d pace, 100 - 5h projection; codex = 7d pace); a tie
               keeps Claude.
  Claude usage unreadable -> codex (unknown is not permission to spend the
  only account; the codex read already passed its gate).

Reset passes (DGN-1965, lib/reset_passes.py): what a pass restores is per
pass (kind full / 5h / weekly / unknown = weekly), so each window is checked
on its own: a Claude window a held pass covers drops out of the open/pace
checks (a full pass frees both; the other window still governs), and so
does the 5h projection when the 5h window is covered; the Codex window read
counts as open when a held pass covers it. A covering pass expiring soon
with its window unspent picks that lane right after rule 1
(codex_pass_expiring, then claude_pass_expiring unless 5h used >=
CLAUDE_UIOLI_5H). The usage-gate refusal lines (rule 4) stay hard: a pass
must be redeemed before that window runs again. No pass -> exactly the rules
above.

Live reserve (DGN-1991, reset_passes.slack()): the Claude 7d pass only
frees the weekly window from the open/pace checks while Claude's slack
after the live sessions' burn to the reset stays >= 0, or Codex has no
slack. Otherwise the 7d window paces as without a pass (detail: "live
reserve short"), so a held Claude pass no longer reads "claude open" while
Codex can take the run. No Claude pass -> unchanged.

CLI (dispatch-detached.sh): stdin = {"usage": get_usage() dict,
"history": [samples]} or empty; prints "runtime<TAB>verdict<TAB>detail".
"""
import argparse
import json
import sys
import time
from datetime import datetime

import reset_passes

FIVE_H = 5 * 3600
SEVEN_D = 7 * 86400

OPEN_MAX = 85.0          # owner: Claude default only while both windows < 85%
PACE_TOL = 10.0          # points behind pace still counted "on pace"
CLAUDE_UIOLI_5H = 85.0   # use-it-or-lose-it: spill only when ACTUAL 5h used >= this
CLAUDE_UIOLI_7D_MAX = 98.0  # ... while the 7d window still has budget left (not OPEN_MAX: the
                            # weekly tail above 85% is exactly what would be lost)
SPILL_PROJ = 95.0        # owner: 5h projected past ~95% -> spill to codex
LOSE_IT_HOURS = 24.0     # codex 7d reset this close = use it or lose it
LOSE_IT_UNUSED = 15.0    # ... with at least this many points still unused
CLAUDE_BLOCK_5H = 90.0   # usage-gate.sh refusal lines (THRESHOLD_FIVE_HOUR /
CLAUDE_BLOCK_7D = 95.0   # THRESHOLD_SEVEN_DAY defaults)
BURN_MIN_SPAN = 600      # a burn sample must be >= 10 min older than now ...
BURN_MAX_AGE = 5400      # ... and <= 90 min old ("recent")
SAME_WINDOW_SKEW = 120   # resets_at jitter tolerated between samples


def _epoch(v):
    """ISO8601 (claude) or epoch int/str (codex) -> float epoch, else None."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _dur(sec):
    sec = max(0, int(sec))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    return (f"{d}d" if d else "") + (f"{h}h" if d or h else "") + f"{m}m"


def window(used, resets_at, length, now):
    """One quota window: used%, elapsed%, pace, seconds to reset (None if unknown)."""
    reset = _epoch(resets_at)
    to_reset = None if reset is None else max(0.0, reset - now)
    if to_reset is None or to_reset > length:
        elapsed = None
    else:
        elapsed = (length - to_reset) / length * 100.0
    pace = (elapsed if elapsed is not None else 0.0) - used
    return {"used": used, "elapsed": elapsed, "pace": pace,
            "to_reset": to_reset, "reset": reset}


def _fmt(name, w):
    s = f"{name} used={w['used']:.0f}%"
    if w["elapsed"] is None:
        s += " elapsed=n/a(reset unknown; pace=-used)"
    else:
        s += f" elapsed={w['elapsed']:.0f}% pace={w['pace']:+.0f}"
    if w["to_reset"] is not None:
        s += f" reset_in={_dur(w['to_reset'])}"
    return s


def burn_rate(usage, history, now):
    """Recent 5h burn in %/sec from recorded samples in the SAME 5h window, or
    None when no usable sample exists. Never extrapolated from one point."""
    cur_t = float(usage.get("fetched_at") or now)
    cur_u = float(usage.get("five_hour_util", 0))
    cur_reset = _epoch(usage.get("five_hour_resets_at"))
    if cur_reset is None:
        return None
    best = None
    for s in history or []:
        try:
            t = float(s.get("fetched_at"))
            u = float(s.get("five_hour_util"))
        except (TypeError, ValueError):
            continue
        r = _epoch(s.get("five_hour_resets_at"))
        if r is None or abs(r - cur_reset) > SAME_WINDOW_SKEW:
            continue
        age = cur_t - t
        if BURN_MIN_SPAN <= age <= BURN_MAX_AGE and (best is None or t < best[0]):
            best = (t, u)
    if best is None:
        return None
    return max(0.0, (cur_u - best[1]) / (cur_t - best[0]))


def decide(usage, history, codex_used, codex_resets_at, codex_window_mins, now,
           claude_pass="", codex_pass="", claude_live_burn=reset_passes.LIVE_BURN_7D):
    """-> (runtime, verdict, detail). usage None = Claude read failed."""
    cx = window(float(codex_used), codex_resets_at,
                float(codex_window_mins or 10080) * 60, now)
    cx_txt = _fmt("codex 7d", cx)
    # The Codex window the quota read reports is refillable while a held pass
    # covers it (per pass kind).
    xp = reset_passes.mode("codex", codex_pass, cx["used"], cx["reset"], now,
                           reset_passes.window_name(codex_window_mins or 10080))[0]
    if xp != "none":
        cx_txt += f" pass={xp}"
    if usage is None:
        return ("codex", "claude_usage_unavailable",
                f"claude usage read FAILED (unknown is not permission); {cx_txt}")
    c5 = window(float(usage.get("five_hour_util", 0)),
                usage.get("five_hour_resets_at"), FIVE_H, now)
    c7 = window(float(usage.get("seven_day_util", 0)),
                usage.get("seven_day_resets_at"), SEVEN_D, now)
    cp5 = reset_passes.mode("claude", claude_pass, c5["used"], c5["reset"], now, "5h")[0]
    cp7 = reset_passes.mode("claude", claude_pass, c7["used"], c7["reset"], now, "7d")[0]
    live_txt = ""
    if cp7 != "none":
        cs = reset_passes.slack(claude_pass, c7["used"], c7["reset"], now, "7d",
                                claude_live_burn)
        xs = reset_passes.slack(codex_pass, cx["used"], cx["reset"], now,
                                reset_passes.window_name(codex_window_mins or 10080))
        if cs and cs["short"] and xs and xs["slack"] > 0:
            cp7 = "none"
            live_txt = f" pass held, live reserve short ({cs['text']}) -> paced"
    rate = burn_rate(usage, history, now)
    proj = None
    if rate is None:
        burn_txt = "burn=n/a (no recorded 5h usage history in window; used% only)"
    elif c5["to_reset"] is None:
        burn_txt = f"burn={rate * 60:.2f}%/min proj=n/a (5h reset unknown)"
    else:
        proj = c5["used"] + rate * c5["to_reset"]
        burn_txt = f"burn={rate * 60:.2f}%/min proj5h={proj:.0f}%"
    # A window a held pass covers is refillable: only the others pace. Both
    # covered (a full pass) -> neutral pace 0.
    paced = [w["pace"] for w, p in ((c5, cp5), (c7, cp7)) if p == "none"]
    claude_pace = min(paced) if paced else 0.0
    if cp5 != "none":
        proj = None
        burn_txt += " (5h refillable: projection not applied)"
    claude_score = min(claude_pace, 100.0 - proj) if proj is not None else claude_pace
    c5_txt = _fmt('5h', c5) + (f" pass={cp5} (refillable)" if cp5 != "none" else "")
    c7_txt = _fmt('7d', c7) + (f" pass={cp7} (refillable)" if cp7 != "none" else live_txt)
    detail = (f"claude {c5_txt} {burn_txt}; {c7_txt}; "
              f"claude_eff_pace={claude_pace:+.0f}; {cx_txt}")

    if (cx["to_reset"] is not None and cx["to_reset"] <= LOSE_IT_HOURS * 3600
            and 100.0 - cx["used"] >= LOSE_IT_UNUSED):
        return ("codex", "codex_use_it_or_lose_it",
                f"{detail} -> codex 7d resets in {_dur(cx['to_reset'])} with "
                f"{100 - cx['used']:.0f}% unused (<= {LOSE_IT_HOURS:.0f}h, >= {LOSE_IT_UNUSED:.0f}%)")

    if xp == "expiry_boost":
        return ("codex", "codex_pass_expiring",
                f"{detail} -> codex reset pass expiring with the weekly window unspent")
    if "expiry_boost" in (cp5, cp7) and c5["used"] < CLAUDE_UIOLI_5H:
        return ("claude-code", "claude_pass_expiring",
                f"{detail} -> claude reset pass expiring with its window unspent; "
                f"5h used={c5['used']:.0f}% < {CLAUDE_UIOLI_5H:.0f}%")

    if (c5["to_reset"] is not None and c7["to_reset"] is not None
            and c7["to_reset"] <= c5["to_reset"] and c7["used"] < CLAUDE_UIOLI_7D_MAX
            and c5["used"] < CLAUDE_UIOLI_5H):
        return ("claude-code", "claude_use_it_or_lose_it",
                f"{detail} -> claude 7d resets in {_dur(c7['to_reset'])} "
                f"(<= 5h reset in {_dur(c5['to_reset'])}) with {100 - c7['used']:.0f}% "
                f"unused; 5h projection ignored, actual 5h used={c5['used']:.0f}% "
                f"< {CLAUDE_UIOLI_5H:.0f}%")

    why = []
    if ((cp5 == "none" and c5["used"] >= OPEN_MAX)
            or (cp7 == "none" and c7["used"] >= OPEN_MAX)):
        why.append(f"window >= {OPEN_MAX:.0f}%")
    if claude_pace < -PACE_TOL:
        why.append(f"off pace ({claude_pace:+.0f} < -{PACE_TOL:.0f})")
    if proj is not None and proj > SPILL_PROJ:
        why.append(f"5h projection {proj:.0f}% > {SPILL_PROJ:.0f}%")
    if not why:
        return ("claude-code", "claude_headroom_on_pace",
                f"{detail} -> claude open and on pace")

    tight = "claude tight: " + ", ".join(why)
    if xp != "none" or (cx["used"] < OPEN_MAX and cx["pace"] >= -PACE_TOL):
        verdict = ("claude_5h_velocity_spill" if proj is not None and proj > SPILL_PROJ
                   else "claude_tight_codex_open")
        return ("codex", verdict, f"{detail} -> {tight}; codex open and on pace")
    if c5["used"] >= CLAUDE_BLOCK_5H or c7["used"] >= CLAUDE_BLOCK_7D:
        return ("codex", "both_tight_claude_blocked",
                f"{detail} -> {tight}; claude at the usage-gate line, codex tight but under its ceiling")
    pick = "codex" if cx["pace"] > claude_score else "claude-code"
    return (pick, "both_tight_less_tight",
            f"{detail} -> {tight}; codex tight too; score claude={claude_score:+.0f} "
            f"codex={cx['pace']:+.0f} -> {pick}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--codex-used", type=float, required=True)
    p.add_argument("--codex-resets-at", default="")
    p.add_argument("--codex-window-mins", default="10080")
    p.add_argument("--now", type=float, default=None)
    p.add_argument("--claude-pass", default="")
    p.add_argument("--codex-pass", default="")
    p.add_argument("--claude-live-burn", type=float, default=reset_passes.LIVE_BURN_7D)
    a = p.parse_args(argv)
    now = a.now if a.now is not None else time.time()
    usage, history = None, []
    raw = sys.stdin.read().strip()
    if raw:
        try:
            doc = json.loads(raw)
            usage = doc.get("usage")
            history = doc.get("history") or []
            if usage is not None:
                for k in ("five_hour_util", "seven_day_util"):
                    v = float(usage[k])
                    if not 0 <= v <= 100:
                        raise ValueError(k)
        except (ValueError, KeyError, TypeError, AttributeError):
            usage, history = None, []
    try:
        wm = float(a.codex_window_mins)
    except ValueError:
        wm = 10080
    rt, verdict, detail = decide(usage, history, a.codex_used, a.codex_resets_at,
                                 wm, now, a.claude_pass, a.codex_pass, a.claude_live_burn)
    print(f"{rt}\t{verdict}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
