#!/usr/bin/env python3
"""Dispatch runtime choice by what would be lost (owner 2026-10-09 20:09).

Owner 2026-10-09 20:09 (paraphrased): there is no fixed priority -- the
priority of a resource depends on the situation. 19:58: the Claude
subscription holds a reset pass too. This replaces the fixed expiry order of
19:55 (codex pass > cloud > API > subscription): every run reads each pool's
live state and goes to the pool whose unused capacity is most at risk of
being lost before its next reset or expiry, at the current burn rate.

This module only RANKS. dispatch-detached.sh runs every hard gate first
(tier binding, codex quota ceiling, codex hook verification for writes,
connector-needing runs never on claude-api, repo-contained and the active
cap for cloud; the local lanes' caps stay at the spawn slot gate) and passes
only the pools that cleared them (--pools); the vetoed ones are named
(--veto) for the decision line. Two vetoes are read here, from the
same state: a Claude window at the usage-gate refusal line or its 5h window
projected past the spill line (the subscription cannot take the run now)
and a spent API pool. DGN-1991: a third Claude veto, the live reserve
(reset_passes.slack()): the Claude weekly window holds a pass but its
slack after the live sessions' burn to the reset is below 0 while Codex
cleared its gates with slack left -- the pass is budget, not licence to
plan Claude to 100% for workers when Codex can take the run. Estimates, in each
pool's own unit, ranked as one number:
  claude    reset_passes.window_loss() over the Claude 7d window (5h + 7d
            used% and resets from the usage snapshot, held passes covering
            the weekly window); points of one week.
  codex     the same over the Codex quota window and its passes.
  cloud     the credit is not metered here, only its window end
            (CLOUD_FIRST_UNTIL) and the cloud cap: idle slots now are credit
            unspent -- 100 x (1 - active/cap) x weight(time to window end).
  api       api_credit_pool.pool_loss(): dollars the month end takes at the
            current spend rate, as a share of the budget x weight (shown in $).
            Vetoed when api_credit_pool.admit() refuses one more run: spent +
            the expected cost of in-flight pool runs past budget or, outside
            the month-end burn, past the pace line (DGN-1978).
Every estimate carries the same urgency weight, reset_passes.loss_weight():
1 within 7 days of its deadline, then 7 / days.
An unread pool has no estimate (unknown is not a claim) and ranks as 0.
Pick: the highest estimate. Below LOSS_FLOOR, or within LOSS_TIE of the
runner-up -> "fallback": the caller keeps today's order (cloud-first while
its window lasts, then the headroom score and the API pool overlay).

CLI: stdin = claude_usage_snapshot() JSON or empty
  runtime_loss.py rank --pools P,P.. [--veto pool=why ...]
      [--claude-pass S --codex-pass S | --conf agent.conf (env > conf)]
      [--codex-used U --codex-resets-at E
       --codex-window-mins M] [--cloud-until D --cloud-cap N --cloud-active N]
      [--budget B --ledger F --marker M --reconcile F --reserve USD
       --budget-source T] [--api-now T (the month clock only)]
      [--claude-live-burn PCT_PER_H] [--now T]
  -> "pick<TAB>verdict<TAB>line": pick = claude-code|codex|cloud|claude-api|
     fallback, verdict = loss_score_pick|loss_tie|loss_low, line e.g.
     "loss: codex 31% (3 passes, first 10-23) > claude 18% > cloud 9% > api $4 -> codex"
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_credit_pool as ap  # noqa: E402
import reset_passes as rp  # noqa: E402
import runtime_headroom as rh  # noqa: E402

LOSS_FLOOR = 10.0      # top estimate below this -> nothing is really at risk
LOSS_TIE = 5.0         # top within this of the runner-up -> a tie

POOLS = ("claude-code", "codex", "cloud", "claude-api")
NAME = {"claude-code": "claude", "codex": "codex", "cloud": "cloud", "claude-api": "api"}


def _pass_txt(w):
    n = w.get("passes") or 0
    if not n:
        return ""
    return f" ({n} pass{'es' if n != 1 else ''}, first {rp._short(w['first'])})"


def claude_blocked(usage, history, now):
    """A veto reason, or '': a Claude window at the usage-gate refusal line
    (rh.CLAUDE_BLOCK_5H / _7D), or the 5h window projected past
    rh.SPILL_PROJ at the recent burn -- the subscription cannot take the run
    now, whatever the week would lose (the headroom policy's own spill)."""
    if usage is None:
        return ""
    for key, line, name in (("five_hour_util", rh.CLAUDE_BLOCK_5H, "5h"),
                            ("seven_day_util", rh.CLAUDE_BLOCK_7D, "7d")):
        v = rp._num(usage.get(key))
        if v is not None and v >= line:
            return f"usage gate: {name} {v:.0f}% >= {line:.0f}%"
    over, txt = ap.five_hour_overflow(usage, history, now)
    if over and "proj5h=" in txt:
        return f"5h {txt.split(' (')[0]}"
    return ""


def claude_live_short(usage, claude_spec, codex_spec, codex_used, codex_resets_at,
                      codex_window_mins, now, live_burn=rp.LIVE_BURN_7D):
    """A veto reason, or '' (header, DGN-1991): Claude 7d holds a pass, its
    slack after the live reserve to the reset is < 0, and Codex (already
    past its gates) has slack > 0."""
    if usage is None:
        return ""
    c = rp.slack(claude_spec, usage.get("seven_day_util"), usage.get("seven_day_resets_at"),
                 now, "7d", live_burn)
    if not c or not c["short"]:
        return ""
    x = rp.slack(codex_spec, codex_used, codex_resets_at, now,
                 rp.window_name(codex_window_mins or 10080))
    if not x or x["slack"] <= 0:
        return ""
    return f"Claude live reserve: 7d {c['text']}; codex {x['text']}"


def claude_loss(usage, spec, now):
    """-> (score|None, shown, detail) over the Claude weekly window."""
    if usage is None:
        return None, "n/a", "claude usage unread"
    w = rp.window_loss(spec, usage.get("seven_day_util"), usage.get("seven_day_resets_at"),
                       now, "7d")
    if w["loss"] is None:
        return None, "n/a", "claude 7d unread"
    return w["loss"], f"{w['loss']:.0f}%{_pass_txt(w)}", f"claude 7d {w['text']}"


def codex_loss(spec, used, resets_at, window_mins, now):
    w = rp.window_loss(spec, used, resets_at, now, rp.window_name(window_mins or 10080))
    if w["loss"] is None:
        return None, "n/a", "codex quota unread"
    return w["loss"], f"{w['loss']:.0f}%{_pass_txt(w)}", f"codex {w['text']}"


def cloud_loss(until, cap, active, now):
    try:
        end = rp.parse_expiry(until) if until else None
    except ValueError:
        end = None
    try:
        cap, active = int(cap), int(active or 0)
    except (TypeError, ValueError):
        cap = None
    if end is None or not cap:
        return None, "n/a", "cloud window or cap unread"
    days = max(0.0, end - now) / 86400
    idle = max(0.0, 1.0 - active / cap)
    score = 100.0 * idle * rp.loss_weight(end - now)
    return score, f"{score:.0f}%", (f"cloud idle {active}/{cap} slots, window ends "
                                    f"{rp._short(end)} ({days:.0f}d) -> {score:.0f}%")


def api_loss(budget, ledger, reserve, now, marker, reconcile, source,
             inflight=None, admin_key=None):
    """-> (score, shown, detail), or a veto reason (str) when one more pool
    run is not admitted (spent, or spent + in-flight past budget / pace)."""
    p = ap.pool_state(budget, ledger, reserve, now, marker, reconcile, source,
                      inflight, admin_key)
    ok, why = ap.admit(p)
    if not ok:
        return why
    got = ap.pool_loss(p, now)
    if got is None:
        return "credit spent"
    lost, share, txt = got
    return share, f"${lost:.0f}", f"api {txt}"


def rank(scores, vetoes):
    """scores: {pool: (score|None, shown, detail)} for the eligible pools;
    vetoes: [(pool, why)]. -> (pick, verdict, line)."""
    order = sorted(scores, key=lambda k: (-(scores[k][0] or 0.0), POOLS.index(k)))
    parts = [f"{NAME[k]} {scores[k][1]}" for k in order]
    line = "loss: " + (" > ".join(parts) if parts else "no eligible pool")
    top = (scores[order[0]][0] or 0.0) if order else 0.0
    second = (scores[order[1]][0] or 0.0) if len(order) > 1 else 0.0
    if not order or top < LOSS_FLOOR:
        pick, verdict = "fallback", "loss_low"
        line += f" -> fallback (top {top:.0f}% < {LOSS_FLOOR:.0f}%)"
    elif top - second < LOSS_TIE:
        pick, verdict = "fallback", "loss_tie"
        line += (f" -> fallback (tie: {NAME[order[0]]} {top:.0f}% vs "
                 f"{NAME[order[1]]} {second:.0f}%)")
    else:
        pick, verdict = order[0], "loss_score_pick"
        line += f" -> {NAME[pick]}"
    if vetoes:
        line += "; vetoed: " + ", ".join(f"{NAME.get(k, k)} ({why})" for k, why in vetoes)
    line += " [" + "; ".join(scores[k][2] for k in order) + "]"
    return pick, verdict, line


def _pass_spec(given, name, conf):
    """The pass spec: the caller's value, else env > conf (api_pool_conf's
    order, whitespace dropped) -- saves the shell two conf reads per run."""
    if given is not None:
        return given
    v = os.environ.get(name, "")
    if not v and conf:
        try:
            from conf_reader import conf_get
            v = conf_get(name, conf) or ""
        except Exception:  # noqa: BLE001 -- unreadable conf = no pass
            v = ""
    return "".join(str(v).split())


def _veto(raw):
    k, _, why = raw.partition("=")
    return k.strip(), (why.strip() or "gate")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rank")
    r.add_argument("--pools", default="")
    r.add_argument("--veto", action="append", default=[])
    r.add_argument("--claude-pass", default=None)
    r.add_argument("--codex-pass", default=None)
    r.add_argument("--conf", default="")
    r.add_argument("--codex-used", default="")
    r.add_argument("--codex-resets-at", default="")
    r.add_argument("--codex-window-mins", default="10080")
    r.add_argument("--cloud-until", default="")
    r.add_argument("--cloud-cap", default="")
    r.add_argument("--cloud-active", default="0")
    r.add_argument("--budget", default="")
    r.add_argument("--budget-source", default="")
    r.add_argument("--ledger", default="/dev/null")
    r.add_argument("--marker", default=None)
    r.add_argument("--reconcile", default=None)
    r.add_argument("--reserve", default="")
    r.add_argument("--inflight", default=None)
    r.add_argument("--admin-key-present", default="")
    r.add_argument("--api-now", type=float, default=None)
    r.add_argument("--claude-live-burn", type=float, default=rp.LIVE_BURN_7D)
    r.add_argument("--now", type=float, default=None)
    a = p.parse_args(argv)
    now = a.now if a.now is not None else time.time()
    usage, history = ap._read_snapshot(sys.stdin.read())
    claude_pass = _pass_spec(a.claude_pass, "RESET_PASSES_CLAUDE", a.conf)
    codex_pass = _pass_spec(a.codex_pass, "RESET_PASSES_CODEX", a.conf)
    pools = [k for k in (x.strip() for x in a.pools.split(",")) if k in POOLS]
    vetoes = [_veto(v) for v in a.veto if v.strip()]
    scores = {}
    for k in pools:
        try:
            if k == "claude-code":
                why = claude_blocked(usage, history, now)
                if not why and "codex" in pools:
                    why = claude_live_short(usage, claude_pass, codex_pass, a.codex_used,
                                            a.codex_resets_at, a.codex_window_mins, now,
                                            a.claude_live_burn)
                if why:
                    vetoes.append((k, why))
                    continue
                scores[k] = claude_loss(usage, claude_pass, now)
            elif k == "codex":
                scores[k] = codex_loss(codex_pass, a.codex_used, a.codex_resets_at,
                                       a.codex_window_mins, now)
            elif k == "cloud":
                scores[k] = cloud_loss(a.cloud_until, a.cloud_cap, a.cloud_active, now)
            else:
                budget = ap.parse_budget(a.budget)
                if budget is None:
                    continue          # pool off: not a candidate, nothing to say
                got = api_loss(budget, a.ledger, ap._reserve(a.reserve),
                               now if a.api_now is None else a.api_now,
                               a.marker, a.reconcile, a.budget_source,
                               a.inflight, ap._flag(a.admin_key_present))
                if isinstance(got, str):
                    vetoes.append((k, got))
                    continue
                scores[k] = got
        except Exception as e:  # noqa: BLE001 -- one bad read is no estimate, never a crash
            scores[k] = (None, "n/a", f"{NAME[k]} estimate failed ({type(e).__name__})")
    print("\t".join(rank(scores, vetoes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
