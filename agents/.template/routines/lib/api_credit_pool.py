#!/usr/bin/env python3
"""Monthly API credit pool for the dispatch claude-api runtime (DGN-1963).

Owner 2026-10-09 (dec-339, paraphrased): the Max plan carries a monthly API
credit (no rollover) that pays for claude -p / Agent SDK / API calls made with
an API key of the linked Console org. It does not raise the interactive
limits, so it is a SEPARATE lane: claude-api = the claude-code worker with
ANTHROPIC_API_KEY in its env, billed to the pool instead of the 5h/7d windows.

This module DECIDES and KEEPS THE BOOKS; it never reads the key. The key's
presence is a flag from dispatch-detached.sh (api_key_present), the Claude
usage snapshot is the same claude_usage_snapshot() payload the DGN-1874
headroom policy reads (stdin), and the 5h projection is computed with that
policy's own window()/burn_rate() -- no second rule.

Ledger: one JSON line per claude-api attempt, finished or not,
  {"ts": <epoch>, "month": "YYYY-MM", "runid": .., "model": .., "cost_usd": ..,
   "estimated": bool, "basis": .., "cause": .., "attempt": ..}
cost_usd = the sum of total_cost_usd over the run's stream "result" events
(a DGN-1898 529 re-run adds a second one). DGN-1978: a run that timed out,
was killed or cancelled has no result event, so its cost is estimated from
the cumulative per-message usage the stream carried so far (token counts x
PRICE_PER_MTOK, estimated=true); a stream with neither falls back to the
model's median run cost from this ledger. Never no row. The month is local
time; spend for a month = the sum of its lines, so the pool resets on the
1st by construction (no rollover, nothing to clear).

Budget (2026-10-09 follow-up, "budget from the plan, truth from the
account"): the monthly budget comes from PLAN= (config/agent.conf, the same
slug the usage gate reads): max_5x -> $100, max_20x -> $200, team_* ->
API_CREDIT_SEAT_USD x API_CREDIT_SEATS (per-seat amount is not a fixed
constant here, so it must be stated; seats default 1). Other plans carry no
credit -> off. API_CREDIT_MONTHLY_USD, when set, overrides the plan
(0 / garbage = explicitly off).

Spend truth, strongest first:
  1. Exhausted marker: a claude-api worker whose stream/stderr carries the
     API's insufficient-credit error (credit balance too low / billing_error)
     writes a month-scoped marker; the pool reads as exhausted until the 1st,
     whatever the ledger says (spend outside dispatch is invisible to it).
  2. Console cost report (DGN-1978, owner 2026-10-10: "the real amount comes
     from the console"), refetched per dispatch decision behind a
     CONSOLE_MAX_AGE (15 min) cache with an Admin API key
     (ANTHROPIC_ADMIN_KEY_FILE, or keychain item dogany-anthropic-admin).
     r2 (measured 2026-10-10 14:59): the report LAGS -- it said $7.73 for the
     month while the Console credit page said $104.17 and the ledger alone
     held $42.57 for that day, so the report is trusted for CLOSED days only:
       spent = sum over UTC days d < open_from of max(report[d], ledger[d])
             + ledger rows (estimated rows included) on days >= open_from
     where open_from = the latest daily bucket the report returned (that day
     is still filling in). Per day the larger of report and ledger wins, so
     the figure is never below the ledger for the same window. A failed
     fetch keeps the last good days, marked stale with its age
     (truth=console-stale).
  3. Owner seed: a figure read off the Console by the owner (source
     owner-reported: month, org_spent_usd, fetched_at; CLI "seed") counts as
     seed + ledger rows after it, and wins over a LOWER computed figure until
     the report closes the seed's UTC day (open_from > that day): from then
     the closed report days include everything the seed saw. A cost-report
     fetch keeps the seed alongside (owner_seed).
  4. Without an Admin key: the owner seed + ledger after it, or the ledger
     alone -- shown plainly as truth=estimated, under the same guards.
  No endpoint reads the remaining credit-grant balance of a Console org
  (checked 2026-10-10: the Admin API reference lists members, invites,
  workspaces, API keys, rate limits, usage/cost reports; spend_limits is
  Claude Enterprise only and holds caps, not a balance). The balance is
  therefore budget - spent, with spent built as above.
Account scope (DGN-1978 r3, measured 2026-10-10): the credit pool is ONE
account (one key, one grant), but each agent instance used to keep its own
books under its .telegram_bot/dispatch/ -- on one Mac instance A $42.57 +
instance B $67.30 = $109.87 ~ Console $104.17, and each paced against the
full budget, so both kept spending. The books now live once per credit
account per host:
  $HOME/.dogany/api-credit/<fp>/ledger.jsonl   (+ ledger.jsonl.lock)
                                /exhausted.json /reconcile.json /inflight/
fp = the first 12 hex of sha256(the pool API key); the key itself is never
stored (dispatch pipes it to "account" on stdin, as with the admin key).
Every append takes an exclusive flock on ledger.jsonl.lock (the duplicate
check runs under the same lock); every instance reads the same file, so the
pace / budget rules see the whole account's spend. On first use each
instance's old books are merged in (ledger rows deduped by (runid, attempt);
the exhausted marker and reconcile state copied when the shared one is
missing or older), and the old files are replaced by a pointer note
({"migrated_to": ..}); the instance keeps api-credit-account.json naming the
account dir for read-only surfaces (summary). Rerunning is idempotent, and
rows an old-code worker appends after the cut-over are merged on the next
use. Cross-host: instances on ANOTHER machine keep their own host ledger;
only the console cost report's closed days (truth=console) cover them --
the open day and any host without an Admin key see this host's ledger only.
In-flight (DGN-1978): every running claude-api worker registers itself
(inflight add, removed at finalize); committed = spent + the per-model median
run cost of each live one, and the pace / budget admission compares
committed, not spent, so N parallel starts cannot all slip under one line.

Selection overlay, applied on top of the measured auto pick (claude-code /
codex), in order:
  d  off        no budget (plan without credit, override 0) -> keep the pick.
  d  key        key missing -> keep the pick (silent fallback, reason recorded).
  d  exhausted  account said credit too low this month, or month spend >=
                budget -> keep the pick.
  -  codex      the pick is codex on codex_use_it_or_lose_it -> keep codex
                (its weekly budget is lost within 24h; the monthly pool is
                not). Documented exception, recorded like the others.
  c  month end  within the last MONTH_END_DAYS days of the month -> claude-api
                (burn the remainder; pacing not applied, budget still caps).
  a+b overflow  Claude 5h projected past SPILL_PROJ (or, with no burn data,
                5h used at the usage-gate line) AND spend < allowed, where
                allowed = budget x elapsed-month fraction + reserve
                -> claude-api, ahead of the codex spill.
  a  pacing     5h overflow but spend >= allowed -> keep the pick.
  e  pass first no 5h overflow, spend < allowed, the pick is claude-code and
                a held Claude reset pass covers the weekly window (the window
                is refillable, so the subscription is spent first and the
                pass becomes redeemable) -> keep claude-code.
  e  pace spend no 5h overflow, spend < allowed -> claude-api (follow-up 2,
                owner 2026-10-09: the credit does not roll over, so spend
                tracks the pace line instead of waiting for an overflow).
     at pace    no 5h overflow, spend >= allowed -> keep the pick.
  Claude usage unreadable -> no overflow claim; the pace rule does not need
  the usage read (it is the pool's own books).
  This overlay is the FALLBACK order: runtime_loss.py first ranks every
  eligible pool by what it would lose, and only a tie or a low-loss situation
  reaches the measured pick + overlay.

Loss (runtime_loss.py, owner 2026-10-09 20:09): pool_loss() = the dollars
left that the month-end takes at the current spend rate, remaining - rate x
time left, rate = spent / elapsed month (before LOSS_MIN_DAYS have elapsed
the pace rate budget / month is assumed: a young month says nothing yet).
Ranked as a share of the budget x reset_passes.loss_weight(time to month
end) -- the same urgency weight as the windows; exhausted or off -> not a
candidate.

CLI:
  api_credit_pool.py decide --budget B --ledger F --base-runtime R
      --base-verdict V --key-present 0|1 [--reserve USD] [--now EPOCH]
      stdin = claude_usage_snapshot() JSON or empty
      -> "runtime<TAB>verdict<TAB>detail"   (runtime = claude-api or R)
  api_credit_pool.py record --ledger F --stream S --runid ID --model M
      [--cause C] [--attempt N] [--now]
      -> "cost_usd=<x> [estimated <basis>] month=<YYYY-MM> spent=<y>"
         (rc 1 only when the stream is missing/empty: nothing ran)
  api_credit_pool.py inflight add|remove|list --dir D [--runid] [--model]
      [--pid]   (list -> one "runid model pid" line per live worker)
  api_credit_pool.py status --budget B --ledger F [--reserve USD] [--now]
      -> "api_pool month=.. budget=.. spent=.. allowed=.. ..."
         (rc 1 when off, rc 2 when the month's budget is spent)
  api_credit_pool.py budget [--override USD] [--plan SLUG] [--seats N]
      [--seat-usd USD]  -> "<usd><TAB><source>" (rc 1 = off, usd empty)
  api_credit_pool.py exhausted --marker M --stream S [--stderr E] [--runid]
      -> rc 0 + "credit_exhausted month=.. evidence=.." when the API said the
         credit balance is too low (marker written); rc 1 otherwise
  api_credit_pool.py reconcile --state F [--base-url URL] [--now]
      [--max-age SEC]   stdin = Admin API key (never argv)
      -> "org_spent=$x month=.. closed=$c before YYYY-MM-DD" (rc 0),
         "fresh .." (rc 0, no fetch), rc 1 on a failed fetch (last good
         days kept, error recorded)
  api_credit_pool.py seed --state F --usd X [--at EPOCH] [--now]
      -> records the owner-read Console figure (source owner-reported)
  decide / status also take [--marker M] [--reconcile F] [--budget-source T]
      [--inflight D] [--admin-key-present 0|1]; status --json prints the
      pool_state() dict.
  api_credit_pool.py account --legacy-dir D [--home H] [--now]
      stdin = the pool API key (never argv, never stored)
      -> "<account dir><TAB><fp><TAB><migration text>" after the one-time,
         idempotent merge of D's old books (rc 1 = no key on stdin)
  api_credit_pool.py summary --workspace W [--now]
      -> pool_state() JSON from W's config/agent.conf (env wins) and the
         dispatch books under W/.telegram_bot/dispatch, or {"off": text}
         (the /passes surface shows the month's remaining credit from it)
"""
import argparse
import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reset_passes as rp  # noqa: E402
import runtime_headroom as rh  # noqa: E402

RESERVE_FRACTION = 0.03   # default "small reserve": 3% of the monthly budget
MONTH_END_DAYS = 3        # last N days of the month -> burn the remainder
LOSS_MIN_DAYS = 3.0       # pool_loss: before this much of the month, spend is assumed on pace
# Monthly API credit carried by the plan (owner 2026-10-09). team_* plans
# are per seat with the seat amount stated in config (API_CREDIT_SEAT_USD).
PLAN_CREDIT_USD = {"max_5x": 100.0, "max_20x": 200.0}
TEAM_PLANS = ("team_standard", "team_premium")
# The API's insufficient-credit error as claude -p surfaces it: the 400
# "Your credit balance is too low to access the Anthropic API" (result text
# "Credit balance is too low"), the SDK's assistant error "billing_error".
CREDIT_EXHAUSTED_RE = re.compile(
    r"credit balance (?:is )?too low|insufficient[ _]credits?|billing_error", re.I)
CONSOLE_MAX_AGE = 900     # console cost report: refetched per decision past 15 min
RECONCILE_MAX_AGE = CONSOLE_MAX_AGE  # (name kept for the CLI default)
RECONCILE_RETRY_SECS = 120  # after a failed fetch, the next try waits this long
COST_REPORT_PATH = "/v1/organizations/cost_report"
COST_REPORT_SOURCE = "cost_report"
OWNER_SOURCE = "owner-reported"
# USD per million tokens: (input, output, cache read); cache writes are
# 1.25x input (5m) / 2x input (1h). First substring match wins, so specific
# ids come before their family. Bare aliases resolve to the current model of
# the family (dispatch-model-display.tsv). Unknown -> the dearest row: an
# estimate that errs low is the failure this exists to prevent.
PRICE_PER_MTOK = (
    ("fable-5-1", 10.0, 50.0, 0.25), ("mythos-5-1", 10.0, 50.0, 0.25),
    ("fable", 10.0, 50.0, 1.0), ("mythos", 10.0, 50.0, 1.0),
    ("opus-5-5", 4.0, 20.0, 0.20), ("opus-5", 5.0, 25.0, 0.50),
    ("opus-4", 5.0, 25.0, 0.50), ("opus", 4.0, 20.0, 0.20),
    ("sonnet-5", 2.0, 10.0, 0.20), ("sonnet-4", 3.0, 15.0, 0.30),
    ("sonnet", 2.0, 10.0, 0.20),
    ("haiku-5", 0.10, 0.50, 0.01), ("haiku-4", 1.0, 5.0, 0.10),
    ("haiku", 0.10, 0.50, 0.01),
)
PRICE_UNKNOWN = ("unknown", 10.0, 50.0, 1.0)
# DGN-1978 r3: the shared per-account books on this host (module doc).
ACCOUNT_ROOT = os.path.join(".dogany", "api-credit")   # under $HOME
ACCOUNT_FILES = {"ledger": "ledger.jsonl", "marker": "exhausted.json",
                 "reconcile": "reconcile.json", "inflight": "inflight"}
LEGACY_FILES = {"ledger": "api-credit-ledger.jsonl", "marker": "api-credit-exhausted.json",
                "reconcile": "api-credit-reconcile.json", "inflight": "api-inflight"}
ACCOUNT_POINTER = "api-credit-account.json"   # in the instance dispatch dir
ACCOUNT_SCOPE = "account pool, all agents on this Mac"
DEFAULT_RUN_USD = 5.0     # expected run cost with no ledger history at all
MEDIAN_WINDOW = 50        # last N measured rows of a family feed its median


def parse_budget(raw):
    """USD float > 0, else None (unset / 0 / garbage = runtime off)."""
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def resolve_budget(override, plan, seats="", seat_usd=""):
    """(budget USD | None, source text). A non-empty override always wins
    (0 / garbage = explicitly off); else the plan's credit."""
    override = (override or "").strip()
    if override:
        v = parse_budget(override)
        if v is None:
            return None, f"off (API_CREDIT_MONTHLY_USD={override})"
        # DGN-1978: the budget is the real credit amount. An override equal to
        # the plan's credit is a confirmation; a different one is said so.
        pc = PLAN_CREDIT_USD.get((plan or "").strip())
        if pc is not None and abs(pc - v) < 0.005:
            return v, f"plan {plan.strip()} (API_CREDIT_MONTHLY_USD agrees)"
        if pc is not None:
            return v, (f"API_CREDIT_MONTHLY_USD override (plan {plan.strip()} "
                       f"carries ${pc:g})")
        return v, "API_CREDIT_MONTHLY_USD override"
    plan = (plan or "").strip()
    if plan in PLAN_CREDIT_USD:
        return PLAN_CREDIT_USD[plan], f"plan {plan}"
    if plan in TEAM_PLANS:
        per = parse_budget(seat_usd)
        if per is None:
            return None, f"off (plan {plan}: API_CREDIT_SEAT_USD unset)"
        try:
            n = int(str(seats).strip() or "1")
        except ValueError:
            n = 0
        if n < 1:
            return None, f"off (plan {plan}: API_CREDIT_SEATS={seats} invalid)"
        return per * n, f"plan {plan} {n} seat(s) x ${per:.2f}"
    return None, f"off (plan {plan or 'unset'} carries no API credit)"


def month_key(now):
    return time.strftime("%Y-%m", time.localtime(now))


def month_clock(now):
    """(elapsed fraction of the local month, whole days left after today)."""
    lt = time.localtime(now)
    days = calendar.monthrange(lt.tm_year, lt.tm_mon)[1]
    start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    y, m = (lt.tm_year + 1, 1) if lt.tm_mon == 12 else (lt.tm_year, lt.tm_mon + 1)
    end = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
    frac = min(1.0, max(0.0, (now - start) / (end - start)))
    return frac, days - lt.tm_mday


def month_spent(ledger, now, since=None):
    """Sum of this month's ledger rows (only rows with ts > since if given)."""
    month, total = month_key(now), 0.0
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("month") == month and \
                            (since is None or float(row.get("ts") or 0) > since):
                        total += float(row.get("cost_usd") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    return total


def _read_json(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def exhausted_marker(marker, now):
    """The marker doc when it is for this month, else None (stale = reset)."""
    doc = _read_json(marker)
    return doc if doc and doc.get("month") == month_key(now) else None


def reconcile_doc(state, now):
    """This month's reconcile state doc (figure and/or last fetch error)."""
    doc = _read_json(state)
    return doc if doc and doc.get("month") == month_key(now) else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def utc_day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def ledger_days(ledger, now):
    """This month's ledger rows summed per UTC day (the report's buckets)."""
    month, out = month_key(now), {}
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("month") == month:
                        d = utc_day(float(row.get("ts") or 0))
                        out[d] = out.get(d, 0.0) + float(row.get("cost_usd") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    return out


def owner_seed(doc):
    """(usd, at) of this month's owner-reported figure in a reconcile doc
    (top level when nothing else wrote the file, else under owner_seed)."""
    if not doc:
        return None
    seed = doc if doc.get("source") == OWNER_SOURCE else doc.get("owner_seed")
    if not isinstance(seed, dict):
        return None
    usd, at = _num(seed.get("org_spent_usd")), _num(seed.get("fetched_at"))
    return None if usd is None or at is None else (usd, at)


def report_days(doc):
    """(days {YYYY-MM-DD: usd}, open_from, fetched_at) of a cost-report doc,
    or None. A doc without per-day buckets (r1 state) is not usable: its
    month total mixed closed days with the lagging open one."""
    if not doc or doc.get("source") != COST_REPORT_SOURCE:
        return None
    days, at = doc.get("days"), _num(doc.get("fetched_at"))
    if not isinstance(days, dict) or at is None:
        return None
    return ({k: _num(v) or 0.0 for k, v in days.items()},
            str(doc.get("open_from") or ""), at)


def reconciled(state, now):
    """(figure USD, at) behind this month's truth: the cost report's closed
    days (fetch time), else the owner seed; None when neither exists."""
    doc = reconcile_doc(state, now)
    rep = report_days(doc)
    if rep is not None:
        days, open_from, at = rep
        return sum(v for d, v in days.items() if d < open_from), at
    return owner_seed(doc)


def _age(secs):
    secs = max(0, int(secs))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d{secs % 86400 // 3600}h"


def _hm(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def spend_truth(reconcile, ledger, now, admin_key=None):
    """-> (spent, spent_src, truth). truth: console (cost report within
    CONSOLE_MAX_AGE), console-stale (last good cost report, older -- fetch
    failing or not yet retried), estimated (no cost report: the owner seed
    or the ledger alone). See the module doc for the closed-day rule."""
    doc = reconcile_doc(reconcile, now)
    led = ledger_days(ledger, now)
    nokey = " (no admin key)" if admin_key is False else ""
    rep = report_days(doc)
    seed = owner_seed(doc)
    if rep is not None:
        days, open_from, at = rep
        closed = sum(max(days.get(d, 0.0), led.get(d, 0.0))
                     for d in set(days) | set(led) if d < open_from)
        spent = closed + sum(v for d, v in led.items() if d >= open_from)
        age = now - at
        src = (f"org cost report days before {open_from[5:]} ({_age(age)} old) + "
               f"ledger from {open_from[5:]}")
        truth = "console"
        if age > CONSOLE_MAX_AGE:
            err = doc.get("last_error")
            why = f"; last fetch failed: {err}" if err else ""
            src = (f"org cost report STALE {_age(age)} old{why}; days before "
                   f"{open_from[5:]} + ledger from {open_from[5:]}{nokey}")
            truth = "console-stale"
        if seed is not None and utc_day(seed[1]) >= open_from:
            est = seed[0] + month_spent(ledger, now, since=seed[1])
            if est > spent:
                src = (f"owner-reported + ledger since {_hm(seed[1])} (above {src} "
                       f"${spent:.2f})")
                spent = est
        return spent, src, truth
    spent, src = sum(led.values()), "ledger" + nokey
    if seed is not None:
        est = seed[0] + month_spent(ledger, now, since=seed[1])
        if est >= spent:
            spent, src = est, f"owner-reported + ledger since {_hm(seed[1])}{nokey}"
    return spent, src, "estimated"


def model_family(model):
    m = (model or "").lower()
    for fam in ("fable", "mythos", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return "unknown"


def expected_run_cost(ledger, model):
    """(USD, basis): the median cost of this model family's recent measured
    rows (exact or usage-estimated; median fallbacks never feed a median),
    else every family's, else DEFAULT_RUN_USD."""
    fam, mine, alls = model_family(model), [], []
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("basis") == "median":
                        continue
                    c = float(row.get("cost_usd") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if c <= 0:
                    continue
                alls.append(c)
                if model_family(row.get("model")) == fam:
                    mine.append(c)
    except OSError:
        pass
    for xs, what in ((mine, f"{fam} median"), (alls, "all-model median")):
        xs = sorted(xs[-MEDIAN_WINDOW:])
        if xs:
            n = len(xs)
            med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
            return med, f"{what} of {n}"
    return DEFAULT_RUN_USD, "default (no ledger history)"


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def inflight_add(d, runid, model, pid, now):
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{runid}.json")
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"runid": runid, "model": model, "pid": int(pid), "ts": int(now)}, fh)
    os.replace(tmp, path)


def inflight_remove(d, runid):
    try:
        os.unlink(os.path.join(d, f"{runid}.json"))
    except OSError:
        pass


def inflight_list(d):
    """Live in-flight entries; an entry whose worker pid is gone is dropped."""
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        doc = _read_json(os.path.join(d, n))
        if not doc:
            continue
        if not _pid_alive(doc.get("pid")):
            inflight_remove(d, n[:-5])
            continue
        out.append(doc)
    return out


def key_fingerprint(key):
    """First 12 hex of sha256(key): names the account, never reveals it."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:12]


def account_dir(fp, home=None):
    return os.path.join(home or os.path.expanduser("~"), ACCOUNT_ROOT, fp)


def account_paths(adir):
    return {k: os.path.join(adir, v) for k, v in ACCOUNT_FILES.items()}


def ledger_scope(ledger):
    """(scope text, fp|None) from where the ledger lives."""
    parent = os.path.dirname(os.path.abspath(ledger or ""))
    if os.path.basename(ledger or "") == ACCOUNT_FILES["ledger"] and \
            os.path.basename(os.path.dirname(parent)) == os.path.basename(ACCOUNT_ROOT):
        fp = os.path.basename(parent)
        return f"{ACCOUNT_SCOPE}; key {fp}", fp
    return "this agent only (instance ledger)", None


@contextlib.contextmanager
def ledger_lock(ledger):
    """Exclusive flock for appends to (and migrations into) a ledger."""
    os.makedirs(os.path.dirname(os.path.abspath(ledger)), exist_ok=True)
    with open(ledger + ".lock", "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _ledger_rows(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _row_key(row):
    """Dedupe key: (runid, attempt) -- one charge per attempt of a run; a row
    without a runid is keyed by its whole content."""
    if row.get("runid"):
        return ("run", str(row["runid"]), str(row.get("attempt") or ""))
    return ("row", json.dumps(row, sort_keys=True))


def _pointer(path, doc):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc) + "\n")
    os.replace(tmp, path)


def _newer_doc(old, new):
    """True when the legacy doc `old` should replace the shared `new`."""
    if not old or not old.get("month") or old.get("migrated_to"):
        return False
    if not new or not new.get("month"):
        return True
    if old["month"] != new["month"]:
        return old["month"] > new["month"]
    return (_num(old.get("fetched_at") or old.get("ts")) or 0) > \
        (_num(new.get("fetched_at") or new.get("ts")) or 0)


def migrate(legacy_dir, adir, now):
    """Merge one instance's old books into the account dir (idempotent).
    -> {"merged": n, "duplicates": n, "copied": [...], "moved_inflight": n}."""
    shared, out = account_paths(adir), {"merged": 0, "duplicates": 0, "copied": [],
                                        "moved_inflight": 0}
    note = {"migrated_to": adir, "ts": int(now),
            "note": "DGN-1978 r3: the API-credit books are one per account on this host"}
    old_ledger = os.path.join(legacy_dir, LEGACY_FILES["ledger"])
    with ledger_lock(shared["ledger"]):
        old_rows = [r for r in _ledger_rows(old_ledger) if not r.get("migrated_to")]
        if old_rows:
            seen = {_row_key(r) for r in _ledger_rows(shared["ledger"])}
            new = []
            for r in old_rows:
                k = _row_key(r)
                if k in seen:
                    out["duplicates"] += 1
                    continue
                seen.add(k)
                new.append(r)
            if new:
                with open(shared["ledger"], "a", encoding="utf-8") as fh:
                    fh.write("".join(json.dumps(r) + "\n" for r in new))
            out["merged"] = len(new)
            backup = old_ledger + ".pre-migration"
            with open(backup, "a", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in old_rows))
            _pointer(old_ledger, dict(note, file=shared["ledger"], rows=len(old_rows),
                                      merged=len(new), backup=backup))
        for k in ("marker", "reconcile"):
            old = os.path.join(legacy_dir, LEGACY_FILES[k])
            doc = _read_json(old)
            if doc is None or doc.get("migrated_to"):
                continue
            if _newer_doc(doc, _read_json(shared[k])):
                _write_state(shared[k], doc)
                out["copied"].append(k)
            _pointer(old, dict(note, file=shared[k]))
        old_inf = os.path.join(legacy_dir, LEGACY_FILES["inflight"])
        try:
            names = [n for n in os.listdir(old_inf) if n.endswith(".json")]
        except OSError:
            names = []
        if names:
            os.makedirs(shared["inflight"], exist_ok=True)
        for n in names:
            try:
                os.replace(os.path.join(old_inf, n), os.path.join(shared["inflight"], n))
                out["moved_inflight"] += 1
            except OSError:
                continue
    try:
        _write_state(os.path.join(legacy_dir, ACCOUNT_POINTER),
                     {"dir": adir, "fingerprint": os.path.basename(adir), "ts": int(now)})
    except OSError:
        pass
    return out


def resolve_account(key, legacy_dir, now, home=None):
    """-> (account dir, fp, migration result) for the pool key."""
    fp = key_fingerprint(key)
    adir = account_dir(fp, home)
    os.makedirs(adir, exist_ok=True)
    res = migrate(legacy_dir, adir, now) if legacy_dir else {}
    return adir, fp, res


def instance_books(dispatch_dir):
    """The books a workspace's dispatch uses: the account dir its pointer
    names (when it exists), else the instance's own legacy files."""
    doc = _read_json(os.path.join(dispatch_dir, ACCOUNT_POINTER))
    adir = doc.get("dir") if doc else None
    if adir and os.path.isdir(adir):
        return account_paths(adir)
    return {k: os.path.join(dispatch_dir, v) for k, v in LEGACY_FILES.items()}


def pool_state(budget, ledger, reserve, now, marker=None, reconcile=None,
               source="", inflight=None, admin_key=None):
    spent, spent_src, truth = spend_truth(reconcile, ledger, now, admin_key)
    rec = reconciled(reconcile, now)
    truth_age = None if rec is None else max(0.0, now - rec[1])
    running = inflight_list(inflight) if inflight else []
    inflight_usd = sum(expected_run_cost(ledger, r.get("model"))[0] for r in running)
    committed = spent + inflight_usd
    mark = exhausted_marker(marker, now)
    frac, days_left = month_clock(now)
    if reserve is None:
        reserve = budget * RESERVE_FRACTION
    allowed = min(budget, budget * frac + reserve)
    return {"budget": budget, "spent": spent, "frac": frac, "days_left": days_left,
            "reserve": reserve, "allowed": allowed, "remaining": max(0.0, budget - spent),
            "month": month_key(now), "spent_src": spent_src, "source": source,
            "truth": truth, "truth_age": truth_age, "inflight_n": len(running), "inflight_usd": inflight_usd,
            "committed": committed, "headroom": max(0.0, budget - committed),
            "exhausted_signal": mark, "scope": ledger_scope(ledger)[0],
            "exhausted": mark is not None or spent >= budget}


def admit(p):
    """(ok, why) for one more pool run: committed (spent + in-flight
    expected) under the budget and, outside the month-end burn, under the
    pace line. The overlay and the loss ranker both ask this."""
    if p["exhausted"]:
        return False, "credit spent"
    if p["committed"] >= p["budget"]:
        return False, (f"spent ${p['spent']:.2f} + {p['inflight_n']} in flight "
                       f"${p['inflight_usd']:.2f} >= budget ${p['budget']:.2f}")
    if p["days_left"] >= MONTH_END_DAYS and p["committed"] >= p["allowed"]:
        return False, (f"over pace: spent ${p['spent']:.2f} + {p['inflight_n']} in "
                       f"flight ${p['inflight_usd']:.2f} >= allowed ${p['allowed']:.2f}")
    return True, ""


def month_secs(now):
    """(seconds elapsed in the local month, seconds left)."""
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    y, m = (lt.tm_year + 1, 1) if lt.tm_mon == 12 else (lt.tm_year, lt.tm_mon + 1)
    end = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
    return max(0.0, now - start), max(0.0, end - now)


def pool_loss(p, now):
    """-> (lost USD, share of the budget %, text) for a pool_state() dict
    (header, Loss); None when the pool is exhausted (not a candidate). The
    in-flight runs' expected cost is already gone from what can be lost."""
    if p["exhausted"]:
        return None
    elapsed, left = month_secs(now)
    if elapsed >= LOSS_MIN_DAYS * 86400:
        rate, rsrc = p["spent"] / elapsed, "burn"
    else:
        rate, rsrc = p["budget"] / (elapsed + left), "pace (month young)"
    lost = max(0.0, p.get("headroom", p["remaining"]) - rate * left)
    w = rp.loss_weight(left)
    txt = (f"spent=${p['spent']:.2f}/${p['budget']:.2f} pace=${p['allowed']:.2f} "
           f"{rsrc}=${rate * 86400:.2f}/d days_left={p['days_left']} "
           f"-> ${lost:.2f} lost at month end (weight {w:.2f})")
    return lost, lost / p["budget"] * 100.0 * w, txt


def _pool_txt(p):
    src = f" ({p['source']})" if p.get("source") else ""
    txt = (f"api_pool month={p['month']} budget=${p['budget']:.2f}{src} "
           f"spent=${p['spent']:.2f} [{p.get('spent_src', 'ledger')}] "
           f"truth={p.get('truth', 'estimated')} ")
    if p.get("scope"):
        txt += f"scope=[{p['scope']}] "
    if p.get("inflight_n"):
        txt += (f"in_flight={p['inflight_n']} (~${p['inflight_usd']:.2f}) "
                f"committed=${p['committed']:.2f} ")
    txt += (f"allowed=${p['allowed']:.2f} (budget x {p['frac'] * 100:.0f}% elapsed + "
            f"reserve ${p['reserve']:.2f}) remaining=${p['remaining']:.2f} "
            f"days_left={p['days_left']}")
    if p.get("exhausted_signal"):
        m = p["exhausted_signal"]
        txt += (f" ACCOUNT_CREDIT_EXHAUSTED (run {m.get('runid') or '?'}: "
                f"{m.get('evidence') or 'credit too low'}; until the 1st)")
    return txt


def five_hour_overflow(usage, history, now):
    """(overflow bool|None, text). None = Claude usage unknown."""
    if usage is None:
        return None, "claude usage unreadable (no overflow claim)"
    c5 = rh.window(float(usage.get("five_hour_util", 0)),
                   usage.get("five_hour_resets_at"), rh.FIVE_H, now)
    rate = rh.burn_rate(usage, history, now)
    if rate is not None and c5["to_reset"] is not None:
        proj = c5["used"] + rate * c5["to_reset"]
        txt = (f"5h used={c5['used']:.0f}% burn={rate * 60:.2f}%/min proj5h={proj:.0f}% "
               f"(spill line {rh.SPILL_PROJ:.0f}%)")
        return proj > rh.SPILL_PROJ, txt
    txt = (f"5h used={c5['used']:.0f}% burn=n/a (used% vs usage-gate line "
           f"{rh.CLAUDE_BLOCK_5H:.0f}%)")
    return c5["used"] >= rh.CLAUDE_BLOCK_5H, txt


def claude_weekly_pass(spec, now):
    """(held bool, text): a live Claude reset pass covers the weekly window."""
    if not spec:
        return False, "claude pass none"
    try:
        st = rp.state(spec, now, "7d")
    except Exception as e:  # noqa: BLE001 -- a bad spec is no pass, never a crash
        return False, f"claude pass unreadable ({type(e).__name__})"
    return bool(st.get("held")), f"claude pass {st.get('text')}"


def decide(budget, ledger, base_rt, base_verdict, key_present, usage, history,
           reserve, now, marker=None, reconcile=None, source="", claude_pass="",
           inflight=None, admin_key=None):
    """-> (runtime, verdict, detail); runtime is 'claude-api' or base_rt."""
    if budget is None:
        return base_rt, "api_pool_off", f"api_pool {source or 'off (no budget)'}"
    p = pool_state(budget, ledger, reserve, now, marker, reconcile, source,
                   inflight, admin_key)
    pool = _pool_txt(p)
    if not key_present:
        return base_rt, "api_key_missing", f"{pool} -> API key missing; kept {base_rt}"
    if p["exhausted_signal"]:
        return base_rt, "api_credit_exhausted", \
            f"{pool} -> the API said the credit balance is too low; kept {base_rt}"
    if p["spent"] >= budget:
        return base_rt, "api_budget_exhausted", f"{pool} -> budget exhausted; kept {base_rt}"
    if p["committed"] >= budget:
        return base_rt, "api_inflight_hold", \
            f"{pool} -> {admit(p)[1]}; kept {base_rt}"
    over, over_txt = five_hour_overflow(usage, history, now)
    detail = f"{pool}; {over_txt}"
    if base_rt == "codex" and base_verdict == "codex_use_it_or_lose_it":
        return base_rt, "api_codex_use_it_or_lose_it", \
            f"{detail} -> codex weekly budget expires first; kept codex"
    if p["days_left"] < MONTH_END_DAYS:
        return "claude-api", "api_month_end_burn", \
            (f"{detail} -> last {MONTH_END_DAYS} days of the month, "
             f"${p['remaining']:.2f} would be lost; claude-api")
    if over:
        if p["committed"] < p["allowed"]:
            return "claude-api", "api_5h_overflow_paced", \
                f"{detail} -> 5h overflow, spend under pacing allowance; claude-api before {base_rt}"
        return base_rt, "api_pacing_hold", \
            f"{detail} -> 5h overflow but spend >= pacing allowance; kept {base_rt}"
    pace = f"spent=${p['spent']:.2f} < allowed=${p['allowed']:.2f}"
    if p["inflight_n"]:
        pace = (f"spent=${p['spent']:.2f} + in flight ${p['inflight_usd']:.2f} < "
                f"allowed=${p['allowed']:.2f}")
    order = "order: month-end burn > 5h overflow > reset-pass subscription first > pace spend"
    if p["committed"] < p["allowed"]:
        held, pass_txt = claude_weekly_pass(claude_pass, now)
        if held and base_rt == "claude-code":
            return base_rt, "api_pace_pass_first", \
                (f"{detail}; {pass_txt} -> {pace} but a held pass makes the Claude weekly "
                 f"window refillable: subscription first, pool waits ({order}); kept {base_rt}")
        return "claude-api", "api_pace_spend", \
            (f"{detail} -> {pace}: under the pace line, credit does not roll over "
             f"({order}); claude-api")
    return base_rt, "api_not_needed", \
        (f"{detail} -> no 5h overflow, spent=${p['spent']:.2f} "
         f"(+ in flight ${p['inflight_usd']:.2f}) >= "
         f"allowed=${p['allowed']:.2f} (at pace); kept {base_rt}")


def stream_cost(path):
    """Sum of total_cost_usd over 'result' events, or None when none carry it."""
    total, seen = 0.0, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("type") == "result" \
                        and ev.get("total_cost_usd") is not None:
                    try:
                        total += float(ev["total_cost_usd"])
                        seen = True
                    except (TypeError, ValueError):
                        continue
    except OSError:
        return None
    return total if seen else None


def model_rates(model):
    m = (model or "").lower()
    for row in PRICE_PER_MTOK:
        if row[0] in m:
            return row
    return PRICE_UNKNOWN


def usage_cost(usage, model):
    """USD for one API message's usage block at PRICE_PER_MTOK."""
    _, rin, rout, rread = model_rates(model)

    def n(k, src=usage):
        try:
            return float(src.get(k) or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0
    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and (n("ephemeral_5m_input_tokens", cc) or
                                 n("ephemeral_1h_input_tokens", cc)):
        write = (n("ephemeral_5m_input_tokens", cc) * 1.25 +
                 n("ephemeral_1h_input_tokens", cc) * 2.0) * rin
    else:
        write = n("cache_creation_input_tokens") * 1.25 * rin
    return (n("input_tokens") * rin + n("output_tokens") * rout +
            n("cache_read_input_tokens") * rread + write) / 1e6


def stream_usage(path, model=""):
    """(estimated USD, API messages seen, any event seen) from the cumulative
    per-message usage of a stream-json run, result or not. claude -p emits one
    "assistant" event per content block, each carrying its message's usage, so
    a message is counted once (by id, its largest output count); subagent
    messages carry their own ids and count too."""
    msgs, events = {}, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                events = True
                if ev.get("type") != "assistant":
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                mid = msg.get("id") or f"line{i}"
                u = msg["usage"]
                prev = msgs.get(mid)
                try:
                    out = float(u.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    out = 0.0
                if prev is None or out >= prev[0]:
                    msgs[mid] = (out, u, msg.get("model") or model)
    except OSError:
        return 0.0, 0, False
    return sum(usage_cost(u, m) for _, u, m in msgs.values()), len(msgs), events


def _recorded(ledger, runid, attempt):
    if not runid or not attempt:
        return None
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("runid") == runid and \
                        str(row.get("attempt") or "") == str(attempt):
                    return row
    except OSError:
        pass
    return None


def record_row(ledger, stream, runid, model, now, cause="", attempt=""):
    """Charge one attempt to the ledger -> the row, or None only when the
    stream is missing or holds no event at all (nothing ran). The same
    (runid, attempt) is charged once (worker loop vs cancel trap)."""
    prev = _recorded(ledger, runid, attempt)
    if prev is not None:
        return dict(prev, duplicate=True)
    cost, est, basis = stream_cost(stream), False, "result"
    if cost is None:
        cost, nmsg, events = stream_usage(stream, model)
        if not events:
            return None
        est = True
        if nmsg:
            basis = f"stream usage ({nmsg} msgs)"
        else:
            cost, basis = expected_run_cost(ledger, model)
            basis = f"median: {basis}"
    row = {"ts": int(now), "month": month_key(now), "runid": runid,
           "model": model, "cost_usd": round(cost, 6)}
    if est:
        row.update(estimated=True, basis="median" if basis.startswith("median") else "usage",
                   basis_detail=basis)
    if cause:
        row["cause"] = cause
    if attempt:
        row["attempt"] = str(attempt)
    # DGN-1978 r3: every agent on the host appends to the account ledger;
    # the duplicate check is repeated under the lock so two writers (worker
    # loop vs cancel trap, or two instances) never charge one attempt twice.
    with ledger_lock(ledger):
        prev = _recorded(ledger, runid, attempt)
        if prev is not None:
            return dict(prev, duplicate=True)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    return row


def record(ledger, stream, runid, model, now, cause="", attempt=""):
    row = record_row(ledger, stream, runid, model, now, cause, attempt)
    return None if row is None else row["cost_usd"]


def _error_texts(ev):
    """Texts of a stream event that are API/CLI error surfaces, never the
    model's own prose (a worker discussing billing must not trip this)."""
    out = []
    t = ev.get("type")
    if t == "result" and ev.get("is_error"):
        out.append(str(ev.get("result") or ""))
        out.extend(str(e) for e in (ev.get("errors") or []))
    elif t == "assistant" and (ev.get("error") or ev.get("isApiErrorMessage")):
        out.append(str(ev.get("error") or ""))
        msg = ev.get("message")
        if isinstance(msg, dict):
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
    elif t == "system" and "error" in str(ev.get("subtype") or ""):
        out.append(json.dumps(ev))
    return out


def credit_exhausted_evidence(stream, stderr=None):
    """First matching insufficient-credit line from the run, or None."""
    try:
        with open(stream, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                for txt in _error_texts(ev):
                    m = CREDIT_EXHAUSTED_RE.search(txt)
                    if m:
                        return f"stream {ev.get('type')}: {txt.strip()[:160]}"
    except OSError:
        pass
    if stderr:
        try:
            with open(stderr, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if CREDIT_EXHAUSTED_RE.search(line):
                        return f"stderr: {line.strip()[:160]}"
        except OSError:
            pass
    return None


def mark_exhausted(marker, stream, stderr, runid, now):
    ev = credit_exhausted_evidence(stream, stderr)
    if ev is None:
        return None
    doc = {"month": month_key(now), "ts": int(now), "runid": runid, "evidence": ev}
    os.makedirs(os.path.dirname(os.path.abspath(marker)), exist_ok=True)
    tmp = f"{marker}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, marker)
    return doc


def month_start_utc_iso(now):
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))


def fetch_org_days(key, base_url, now, timeout=15):
    """Month-to-date org spend from the Admin API cost report as
    ({UTC day YYYY-MM-DD: usd}, latest bucket day). Buckets are UTC days;
    amounts are cents. Every interval comes back, empty ones included, so the
    latest bucket is the day still filling in."""
    import urllib.parse    # here, not at the top: every selection imports
    import urllib.request  # this module, only the reconcile needs them
    params = {"starting_at": month_start_utc_iso(now), "bucket_width": "1d",
              "limit": "31"}
    days, page = {}, None
    for _ in range(10):
        q = dict(params, **({"page": page} if page else {}))
        req = urllib.request.Request(
            base_url.rstrip("/") + COST_REPORT_PATH + "?" + urllib.parse.urlencode(q),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
        for bucket in doc.get("data") or []:
            day = str(bucket.get("starting_at") or "")[:10]
            if len(day) != 10:
                raise ValueError(f"cost report bucket without starting_at: {bucket!r:.80}")
            cents = sum(float(r.get("amount") or 0) for r in bucket.get("results") or []
                        if (r.get("currency") or "USD") == "USD")
            days[day] = days.get(day, 0.0) + cents / 100.0
        page = doc.get("next_page")
        if not doc.get("has_more") or not page:
            break
    return days, (max(days) if days else utc_day(now))


def _write_state(state, doc):
    os.makedirs(os.path.dirname(os.path.abspath(state)), exist_ok=True)
    tmp = f"{state}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, state)


def _seed_of(doc):
    """The owner seed sub-doc to carry over into a rewritten state."""
    if not doc:
        return None
    if doc.get("source") == OWNER_SOURCE:
        return {k: doc[k] for k in ("org_spent_usd", "fetched_at", "source") if k in doc}
    seed = doc.get("owner_seed")
    return seed if isinstance(seed, dict) else None


def reconcile(state, key, base_url, now, max_age=RECONCILE_MAX_AGE):
    """-> (status, text). status: fresh | fetched | failed. Only a cost-report
    doc with per-day buckets counts as fresh. A failure keeps the last good
    days (they read as console-stale) and records the error; the next try
    waits RECONCILE_RETRY_SECS so a dead endpoint does not tax every
    decision. An owner seed is carried along (owner_seed)."""
    doc = reconcile_doc(state, now) or {}
    rep = report_days(doc)
    if rep is not None and now - rep[2] < max_age:
        return "fresh", f"fresh org_spent=${_num(doc.get('org_spent_usd')) or 0:.2f} " \
                        f"month={month_key(now)}"
    last_try = _num(doc.get("last_attempt_at")) or 0.0
    if doc.get("last_error") and now - last_try < RECONCILE_RETRY_SECS:
        return "failed", (f"cost report fetch failed {_age(now - last_try)} ago "
                          f"({doc['last_error']}); retry after {RECONCILE_RETRY_SECS}s")
    try:
        days, open_from = fetch_org_days(key, base_url, now)
    except Exception as e:  # noqa: BLE001 -- the last good figure stays, marked stale
        err = f"{type(e).__name__}: {str(e)[:120]}"
        _write_state(state, dict(doc, month=month_key(now), last_error=err,
                                 last_attempt_at=int(now)))
        return "failed", f"cost report fetch failed: {err}"
    total = sum(days.values())
    closed = sum(v for d, v in days.items() if d < open_from)
    new = {"month": month_key(now), "fetched_at": int(now),
           "org_spent_usd": round(total, 6), "source": COST_REPORT_SOURCE,
           "days": {d: round(v, 6) for d, v in sorted(days.items())},
           "open_from": open_from, "last_attempt_at": int(now)}
    seed = _seed_of(doc)
    if seed:
        new["owner_seed"] = seed
    _write_state(state, new)
    return "fetched", (f"org_spent=${total:.2f} month={month_key(now)} "
                       f"closed=${closed:.2f} before {open_from}")


def seed(state, usd, at, now):
    """Record the owner's Console reading (source owner-reported); a
    cost-report doc already there keeps its days and gains owner_seed."""
    doc = reconcile_doc(state, now) or {}
    s = {"org_spent_usd": round(float(usd), 6), "fetched_at": int(at),
         "source": OWNER_SOURCE}
    if report_days(doc) is not None or doc.get("last_error"):
        _write_state(state, dict(doc, month=month_key(now), owner_seed=s))
    else:
        _write_state(state, dict(s, month=month_key(now)))
    return s


def _read_snapshot(raw):
    """claude_usage_snapshot() JSON -> (usage|None, history)."""
    raw = (raw or "").strip()
    if not raw:
        return None, []
    try:
        doc = json.loads(raw)
        usage = doc.get("usage")
        if usage is not None:
            v = float(usage["five_hour_util"])
            if not 0 <= v <= 100:
                raise ValueError("five_hour_util")
        return usage, doc.get("history") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, []


def _reserve(raw):
    try:
        v = float(raw)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def summary(workspace, now):
    """pool_state() for a workspace, read the way dispatch reads it (env >
    config/agent.conf; the dispatch books) -- for read-only surfaces."""
    from conf_reader import conf_get
    conf = os.path.join(workspace, "config", "agent.conf")

    def val(k):
        v = os.environ.get(k, "")
        if not v:
            try:
                v = conf_get(k, conf) or ""
            except Exception:  # noqa: BLE001 -- unreadable conf = unset
                v = ""
        return str(v).strip()
    budget, src = resolve_budget(val("API_CREDIT_MONTHLY_USD"), val("PLAN"),
                                 val("API_CREDIT_SEATS"), val("API_CREDIT_SEAT_USD"))
    if budget is None:
        return {"off": src}
    b = instance_books(os.path.join(workspace, ".telegram_bot", "dispatch"))
    st = pool_state(budget, b["ledger"], _reserve(val("API_CREDIT_RESERVE_USD")), now,
                    b["marker"], b["reconcile"], src, b["inflight"])
    st["exhausted_signal"] = bool(st["exhausted_signal"])
    return st


def _flag(raw):
    """'1' -> True, '0' -> False, anything else -> None (not said)."""
    return {"1": True, "0": False}.get(str(raw or "").strip())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--budget", default="")
    d.add_argument("--ledger", required=True)
    d.add_argument("--base-runtime", required=True)
    d.add_argument("--base-verdict", default="")
    d.add_argument("--key-present", default="0")
    d.add_argument("--reserve", default="")
    d.add_argument("--now", type=float, default=None)
    d.add_argument("--marker", default=None)
    d.add_argument("--reconcile", default=None)
    d.add_argument("--budget-source", default="")
    d.add_argument("--claude-pass", default="")
    d.add_argument("--inflight", default=None)
    d.add_argument("--admin-key-present", default="")
    r = sub.add_parser("record")
    r.add_argument("--ledger", required=True)
    r.add_argument("--stream", required=True)
    r.add_argument("--runid", default="")
    r.add_argument("--model", default="")
    r.add_argument("--cause", default="")
    r.add_argument("--attempt", default="")
    r.add_argument("--now", type=float, default=None)
    s = sub.add_parser("status")
    s.add_argument("--budget", default="")
    s.add_argument("--ledger", required=True)
    s.add_argument("--reserve", default="")
    s.add_argument("--now", type=float, default=None)
    s.add_argument("--marker", default=None)
    s.add_argument("--reconcile", default=None)
    s.add_argument("--budget-source", default="")
    s.add_argument("--inflight", default=None)
    s.add_argument("--admin-key-present", default="")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("inflight")
    f.add_argument("op", choices=("add", "remove", "list"))
    f.add_argument("--dir", required=True)
    f.add_argument("--runid", default="")
    f.add_argument("--model", default="")
    f.add_argument("--pid", default="")
    f.add_argument("--now", type=float, default=None)
    ac = sub.add_parser("account")
    ac.add_argument("--legacy-dir", default="")
    ac.add_argument("--home", default=None)
    ac.add_argument("--now", type=float, default=None)
    m = sub.add_parser("summary")
    m.add_argument("--workspace", required=True)
    m.add_argument("--now", type=float, default=None)
    b = sub.add_parser("budget")
    b.add_argument("--override", default="")
    b.add_argument("--plan", default="")
    b.add_argument("--seats", default="")
    b.add_argument("--seat-usd", default="")
    x = sub.add_parser("exhausted")
    x.add_argument("--marker", required=True)
    x.add_argument("--stream", required=True)
    x.add_argument("--stderr", default=None)
    x.add_argument("--runid", default="")
    x.add_argument("--now", type=float, default=None)
    c = sub.add_parser("reconcile")
    c.add_argument("--state", required=True)
    c.add_argument("--base-url", default="https://api.anthropic.com")
    c.add_argument("--max-age", type=float, default=RECONCILE_MAX_AGE)
    c.add_argument("--now", type=float, default=None)
    o = sub.add_parser("seed")
    o.add_argument("--state", required=True)
    o.add_argument("--usd", type=float, required=True)
    o.add_argument("--at", type=float, default=None)
    o.add_argument("--now", type=float, default=None)
    a = p.parse_args(argv)
    now = getattr(a, "now", None)
    now = now if now is not None else time.time()

    if a.cmd == "decide":
        usage, history = _read_snapshot(sys.stdin.read())
        rt, verdict, detail = decide(parse_budget(a.budget), a.ledger, a.base_runtime,
                                     a.base_verdict, a.key_present == "1", usage,
                                     history, _reserve(a.reserve), now, a.marker,
                                     a.reconcile, a.budget_source, a.claude_pass,
                                     a.inflight, _flag(a.admin_key_present))
        print(f"{rt}\t{verdict}\t{detail}")
        return 0
    if a.cmd == "record":
        row = record_row(a.ledger, a.stream, a.runid, a.model, now, a.cause, a.attempt)
        if row is None:
            print("cost_usd=n/a (empty stream: nothing ran)")
            return 1
        est = f" estimated {row['basis_detail']}" if row.get("estimated") else ""
        dup = " (already charged)" if row.get("duplicate") else ""
        print(f"cost_usd={row['cost_usd']:.4f}{est}{dup} month={month_key(now)} "
              f"spent={month_spent(a.ledger, now):.4f}")
        return 0
    if a.cmd == "account":
        key = sys.stdin.read().strip()
        if not key:
            print("no pool key on stdin")
            return 1
        adir, fp, res = resolve_account(key, a.legacy_dir, now, a.home)
        txt = (f"merged={res.get('merged', 0)} duplicates={res.get('duplicates', 0)} "
               f"copied={','.join(res.get('copied') or []) or '-'} "
               f"inflight_moved={res.get('moved_inflight', 0)}")
        print(f"{adir}\t{fp}\t{txt}")
        return 0
    if a.cmd == "summary":
        print(json.dumps(summary(a.workspace, now)))
        return 0
    if a.cmd == "inflight":
        if a.op == "add":
            inflight_add(a.dir, a.runid, a.model, a.pid or os.getppid(), now)
        elif a.op == "remove":
            inflight_remove(a.dir, a.runid)
        else:
            for r in inflight_list(a.dir):
                print(f"{r.get('runid')} {r.get('model')} {r.get('pid')}")
        return 0
    if a.cmd == "budget":
        usd, src = resolve_budget(a.override, a.plan, a.seats, a.seat_usd)
        print(f"{'' if usd is None else f'{usd:g}'}\t{src}")
        return 1 if usd is None else 0
    if a.cmd == "exhausted":
        doc = mark_exhausted(a.marker, a.stream, a.stderr, a.runid, now)
        if doc is None:
            return 1
        print(f"credit_exhausted month={doc['month']} evidence={doc['evidence']}")
        return 0
    if a.cmd == "seed":
        sd = seed(a.state, a.usd, a.at if a.at is not None else now, now)
        print(f"owner-reported org_spent=${sd['org_spent_usd']:.2f} at "
              f"{_hm(sd['fetched_at'])} month={month_key(now)}")
        return 0
    if a.cmd == "reconcile":
        key = sys.stdin.read().strip()
        if not key:
            print("no admin key on stdin")
            return 1
        status, txt = reconcile(a.state, key, a.base_url, now, a.max_age)
        print(txt)
        return 1 if status == "failed" else 0
    budget = parse_budget(a.budget)
    if budget is None:
        print(f"api_pool {a.budget_source or 'off (no budget)'}")
        return 1
    st = pool_state(budget, a.ledger, _reserve(a.reserve), now, a.marker,
                    a.reconcile, a.budget_source, a.inflight,
                    _flag(a.admin_key_present))
    if a.json:
        print(json.dumps(dict(st, text=_pool_txt(st))))
    else:
        print(_pool_txt(st))
    return 2 if st["exhausted"] else 0


if __name__ == "__main__":
    sys.exit(main())
