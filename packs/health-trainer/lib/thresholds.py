#!/usr/bin/env python3
"""Transition threshold constants + predicates -- DGN-238 v3 section 1.2.

OQ-L RESOLVED by owner precedent (2026-07-10): defaults below are adopted
as config-key defaults, owner-tunable at weekly review.

  ledger_short_pass_rate      = 80   # % of target freq over window_weeks -> mid proposal
  ledger_mid_fail_weeks       = 2    # consecutive weeks below fail rate
  ledger_mid_fail_rate        = 50   # % weekly adherence threshold
  ledger_long_disengage_days  = 28   # no goal-linked activity rows for N days
  ledger_review_every         = 4    # every Nth weekly run = ladder_review

The constants live in Warg's lifekit config TABLE (same home as the other
owner-tunable keys); register_defaults() is the mint-time step.

mid demotion proposal fires ONLY when ALL THREE hold (DGN-229 AND join):
  (1) mid failure  : ledger_mid_fail_weeks CONSECUTIVE weeks with
                     adherence strictly below ledger_mid_fail_rate;
  (2) long disengage: zero goal-linked activity rows (workouts + meals +
                     metric_log inserts in Warg's own db) in the last
                     ledger_long_disengage_days days;
  (3) phase minimum : mid detail.min_weeks elapsed -- before that the
                     demotion NEVER fires (safety cap precedence).

Boundary semantics (pinned here, tested):
  - short pass: adherence >= pass rate (exactly 80% passes).
  - mid fail week: adherence < fail rate (exactly 50% is NOT a fail week).
  - disengage window: an activity row dated exactly N days ago still
    counts as engagement (inclusive window -- conservative: blocks
    demotion on the boundary).
  - phase minimum: elapsed_days >= min_weeks*7 opens the gate.

English/ASCII only, pure functions, no I/O beyond the config reads.
"""

import datetime

DEFAULTS = {
    "ledger_short_pass_rate": 80,
    "ledger_mid_fail_weeks": 2,
    "ledger_mid_fail_rate": 50,
    "ledger_long_disengage_days": 28,
    "ledger_review_every": 4,
}


def register_defaults(conn):
    """Insert-if-absent the constants into the lifekit config table.
    Owner-tunable afterwards via the normal config verb. Returns actions."""
    actions = []
    for key, val in DEFAULTS.items():
        row = conn.execute("SELECT 1 FROM config WHERE key=?",
                           (key,)).fetchone()
        if row:
            continue
        conn.execute("INSERT INTO config (key, value) VALUES (?,?)",
                     (key, str(val)))
        actions.append("registered %s=%s" % (key, val))
    conn.commit()
    return actions


def load_constants(conn=None):
    """Read constants from the config table, falling back to DEFAULTS."""
    consts = dict(DEFAULTS)
    if conn is not None:
        for key in DEFAULTS:
            row = conn.execute("SELECT value FROM config WHERE key=?",
                               (key,)).fetchone()
            if row is not None:
                consts[key] = int(float(row[0]))
    return consts


# -- adherence arithmetic ------------------------------------------------------
def week_adherence(logged_sessions, denominator):
    """Weekly adherence in percent. Zero denominator = undefined -> treated
    as 0% (no scheduled target means no adherence signal, never a pass)."""
    if denominator <= 0:
        return 0.0
    return logged_sessions * 100.0 / denominator


def week_denominator(mid_detail, l1_conn=None):
    """Pinned denominator rule (v3 1.2, discretion re-entry sealed):
    routine_def present in L1 -> goal-linked weekly scheduled item count;
    absent -> mid detail.freq_per_week.

    The routine_def branch is NOT implementable yet: DGN-240 landed on Ag
    (2026-07-10, user_version 6) but its schema carries NO goal-link
    column, so the goal-linked count query shape is still undefined
    (OPEN QUESTION OQ-Q in the package docs). Until a goal link exists
    the freq_per_week fallback applies even though the table is present
    (the LIVE path now); the return tags the source so the weekly run
    can surface the downgrade honestly."""
    freq = mid_detail.get("freq_per_week")
    if freq is None:
        raise ValueError("mid detail lacks freq_per_week (schema delta "
                         "requires it at phase entry)")
    if l1_conn is not None:
        has_def = l1_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='routine_def'").fetchone()
        if has_def:
            return int(freq), "freq_per_week (routine_def present but "\
                              "goal-link query undefined until DGN-240 -- OQ-Q)"
    return int(freq), "freq_per_week"


# -- transition predicates -------------------------------------------------------
def short_pass(logged_sessions, freq_per_week, window_weeks, consts,
               window_elapsed=True):
    """short success = window elapsed AND adherence >= pass rate."""
    if not window_elapsed:
        return False
    target = freq_per_week * window_weeks
    return week_adherence(logged_sessions, target) >= \
        consts["ledger_short_pass_rate"]


def mid_fail_streak(weekly_rates, consts):
    """(1) mid failure: the most recent ledger_mid_fail_weeks entries of
    weekly_rates (oldest -> newest, percent) are ALL strictly below the
    fail rate."""
    need = consts["ledger_mid_fail_weeks"]
    if len(weekly_rates) < need:
        return False
    return all(r < consts["ledger_mid_fail_rate"]
               for r in weekly_rates[-need:])


def long_disengaged(activity_dates, today, consts):
    """(2) long disengage: no activity row in the last N days (inclusive
    boundary counts as engagement)."""
    edge = (datetime.date.fromisoformat(today) - datetime.timedelta(
        days=consts["ledger_long_disengage_days"])).isoformat()
    return not any(d >= edge for d in activity_dates)


def min_weeks_elapsed(starts_on, today, min_weeks):
    """(3) phase minimum elapsed."""
    days = (datetime.date.fromisoformat(today)
            - datetime.date.fromisoformat(starts_on)).days
    return days >= int(min_weeks) * 7


def demotion_due(weekly_rates, activity_dates, starts_on, min_weeks, today,
                 consts):
    """AND of all three -- the ONLY demotion trigger (spec verbatim)."""
    return (mid_fail_streak(weekly_rates, consts)
            and long_disengaged(activity_dates, today, consts)
            and min_weeks_elapsed(starts_on, today, min_weeks))


def review_due(weekly_run_index, consts):
    """(recheck) every Nth weekly run = ladder_review. 1-based index:
    runs 4, 8, 12, ... with the default of 4."""
    every = consts["ledger_review_every"]
    return weekly_run_index > 0 and weekly_run_index % every == 0
