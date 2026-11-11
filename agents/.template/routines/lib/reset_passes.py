#!/usr/bin/env python3
"""Expiring reset passes in the dispatch selector (DGN-1965).

Owner 2026-10-09 11:25 (paraphrased; verbatim in worklog DGN-1965): there is a
Claude weekly reset pass (x1, until 10-23) and Codex reset passes (~x3) --
lots of time-limited stock. Standing owner principle: no fixed caps; spend the
weekly share before it resets; adapt to what is left.

Owner 2026-10-09 11:29: the pass inventory is a USER SETTING. The user owns
it through the bridge's reset-pass surface (/passes, also a button under
/usage: list, add, remove -- buttons only); the agent may OFFER to register a
pass the user mentions in chat, and writes only after the user confirms. Both
go through the add/remove verbs below; nobody hand-edits the line.

Inventory (DGN-1983: ONE store per account per host, shared by every agent
on the Mac -- $HOME/.dogany/reset-passes/<claude|codex>/<account fp>/
passes.json, section "shared per-account store" below; env still wins; an
instance conf line from before is merged in once and replaced by a pointer.
Nothing here ever redeems a pass -- that is the user's action). The spec
string, stored in the store's "spec" field (env: same form):
  RESET_PASSES_CLAUDE='<count>|<expiry1>:<kind1>,<expiry2>:<kind2>,...'
  RESET_PASSES_CODEX='<count>|<expiry1>:<kind1>,...'
One entry per pass, written soonest first. Expiry: an ISO date YYYY-MM-DD
(valid through the end of that local day), YYYY-MM-DDTHH:MM (local time,
exact), or 'unknown'. Kind (owner 2026-10-09, dec-340): what THAT pass
restores -- the vendor page says "the 5-hour limit, the weekly limit, or
both", and it differs per pass, not per runtime:
  full     both the 5h and the weekly limit
  5h       the 5-hour limit only
  weekly   the weekly limit only
  unknown  not confirmed -> planned as weekly (the conservative reading)
An entry with no ':<kind>' reads as unknown, so every older form still reads:
'<count>|<e1>,<e2>,...' (follow-up 1), '<count>|<date>' (DGN-1965: a single
expiry with count > 1 is shared by every pass) and '<count>|' (none
confirmed). Fewer entries than the count -> the rest are 'unknown:unknown'.
An 'unknown' expiry counts as held but never as expiring; an expired pass no
longer counts. Unset, count 0, all expired or malformed -> no pass (today's
behaviour); a malformed value is reported as such, never guessed. The count
is always rewritten from the entries (format_spec), so it cannot drift.

Which windows a pass covers (COVERS): full -> 5h + 7d, 5h -> 5h, weekly and
unknown -> 7d. A window no live pass covers is planned exactly as without a
pass. Per window, the selector plans the SOONEST-expiring covering pass
first: the boost / expiring prompt look at that pass; later ones stay in
stock.

Modes for one runtime's window (used% + reset epoch):
  none          no live pass covers this window
                -> the selector behaves as before DGN-1965.
  held          a covering pass is in hand: the window is refillable, so plan
                toward 100% (no reserve, no weekly pace/open penalty).
  expiry_boost  held, the soonest covering pass expires within EXPIRY_SOON_H and the
                window is not yet near spent -> raise dispatch concurrency
                so the window is used up and the pass becomes redeemable.
  near_spent    held and used >= NEAR_SPENT -> one owner line ("redeem and
                run again") instead of throttling; still planned to 100%.

Prompts (owner push, at most once per key): near_spent once per window (key =
runtime + window + window reset hour; it names the window that is nearly
full, 5h or weekly; one line per runtime, the weekly one when both are);
expiring once per pass expiry (key = runtime + expiry) when the soonest
covering pass expires within EXPIRY_PROMPT_H.

CLI (dispatch-detached.sh; an empty field prints as "-" so a bash
`IFS=$'\t' read` never collapses it):
  mode --runtime R --spec S --used U [--resets-at E] [--window 5h|7d] [--now T]
      -> "mode<TAB>prompt_kind<TAB>prompt_key<TAB>detail"
  review --claude-spec S --codex-spec S [--codex-used U --codex-resets-at E
         --codex-window-mins M] [--now T], stdin = {"usage": get_usage() dict}
      -> line 1: the pass state for the decision record; then one
         "PROMPT<TAB>runtime<TAB>kind<TAB>key<TAB>expiry<TAB>window" line per
         prompt due (window 5h|7d)
  once --stamp FILE --key K   rc 0 = first time (recorded), rc 1 = already sent
  loss --runtime R --spec S --used U [--resets-at E] [--window 5h|7d] [--now T]
      -> "loss<TAB>detail" (window loss, below; loss "-" = unread)

Window loss (owner 2026-10-09 20:09: "there is no fixed priority -- the
priority of a resource depends on the situation"; it replaces the fixed
expiry order of 19:55): window_loss() estimates, from live state, how many
points of one window's capacity expire unused at the current burn rate. The
burn rate is used% over the elapsed share of the window (below
LOSS_MIN_ELAPSED of the window, or with the reset unknown, the on-pace rate
is assumed: a young window says nothing yet). The window is walked forward
at that rate the way the selector plans it: when it fills, the soonest live
covering pass is redeemed (the window restarts from 0, its reset unchanged);
at a reset it starts again from 0. Two parts, summed:
  window  the unused part at the NEXT reset (100 - used - rate x time, or
          the refilled window's remainder after a redemption).
  passes  100 for each covering pass with a known expiry within
          LOSS_MAX_HORIZON_D that the walk reaches its expiry without
          redeeming -- the burn never fills the window in time.
Each part is weighted by loss_weight(): 1 within LOSS_HORIZON_D, then
LOSS_HORIZON_D / days -- a loss weeks out can still be caught up, one due
tomorrow cannot. 'unknown'-expiry passes never expire, so they are never at
risk. runtime_loss.py ranks it against the other pools (same weight); every
per-runtime gate still applies.
Live reserve (DGN-1991, owner 2026-10-11 10:39-10:41): the Claude
subscription also carries the owner's main conversation and their own use
elsewhere; Codex is worker-only. A held Claude pass is real budget, but it
does not by itself plan the Claude weekly window to 100% for workers while
another runtime has slack. slack() = (100 - used) + 100 x live covering
passes - live burn x hours to the window reset (reset unknown: a full
window). Claude's live burn is LIVE_BURN_7D; a worker-only pool's is 0. A
Claude 7d window with a held pass and slack < 0 is "short": the workers'
share is already spoken for by the live sessions up to the reset, so the
callers (runtime_loss veto, runtime_headroom 7d pacing) keep workers off
Claude while Codex has slack. No pass, or no other runtime with slack ->
nothing changes.
Settings verbs (the bridge surface and the agent's confirmed offer; --conf
names the instance conf only for the one-time migration):
  list   --conf agent.conf               -> JSON inventory of both runtimes
  add    --conf agent.conf --runtime R --expiry D|unknown [--kind K]
  remove --conf agent.conf --runtime R --expiry D|unknown [--kind K]  (one pass)
      add/remove print the new JSON inventory; rc 2 = refused (message on
      stderr). They write the account store only; an env override is
      reported ("env_override") and left alone.
  specs  [--conf agent.conf]  -> "<claude spec><TAB><codex spec>" ('-' =
      none), env > store: what dispatch-detached.sh plans with.
  summary [--conf agent.conf] [--locale ko|en] -> the /usage block
      (routines/claude-usage.sh) from the same store.
"""
import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from datetime import datetime

NEAR_SPENT = 90.0        # window used% at which the owner is asked to redeem
EXPIRY_SOON_H = 72.0     # a pass this close to expiry boosts concurrency
EXPIRY_PROMPT_H = 24.0   # ... and this close gets its own one-line prompt
LOSS_MIN_ELAPSED = 0.15  # window loss: below this elapsed share the burn is assumed on pace
LOSS_HORIZON_D = 7.0     # a loss this close counts in full; further out, horizon / days
LOSS_MAX_HORIZON_D = 35.0  # passes expiring later are not scored (weight would be < 0.2)
LIVE_BURN_7D = 1.2       # %/h of the Claude weekly window the live sessions burn with no
                         # dispatch (DISPATCH_LIVE_BURN_7D; dispatch-detached.sh passes its value)
WINDOW_SECS = {"5h": 5 * 3600, "7d": 7 * 86400}

RUNTIMES = ("claude", "codex")
CONF_KEY = {"claude": "RESET_PASSES_CLAUDE", "codex": "RESET_PASSES_CODEX"}
UNKNOWN = "unknown"
KINDS = ("full", "5h", "weekly", UNKNOWN)
# Which quota windows a pass of each kind restores; unknown = weekly.
COVERS = {"full": ("5h", "7d"), "5h": ("5h",), "weekly": ("7d",), UNKNOWN: ("7d",)}
WINDOW_TEXT = {"5h": "5h", "7d": "weekly"}

_SPEC = re.compile(r"^\s*(\d+)\s*\|\s*(.*?)\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$")


def parse_expiry(tok):
    """'YYYY-MM-DD' (inclusive, end of that local day) | 'YYYY-MM-DDTHH:MM'
    (local, exact) | 'unknown' -> epoch or None; raises ValueError."""
    tok = str(tok).strip()
    if tok.lower() in ("", UNKNOWN):
        return None
    if not _DATE.match(tok):
        raise ValueError(f"bad expiry {tok!r} (want YYYY-MM-DD, YYYY-MM-DDTHH:MM or unknown)")
    if "T" in tok:
        t = time.strptime(tok, "%Y-%m-%dT%H:%M")
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1))
    t = time.strptime(tok, "%Y-%m-%d")
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def parse_kind(kind):
    """'full' | '5h' | 'weekly' | 'unknown' (case-insensitive; '' = unknown)."""
    k = str(kind or "").strip().lower() or UNKNOWN
    if k not in KINDS:
        raise ValueError(f"bad kind {kind!r} (want {', '.join(KINDS)})")
    return k


def parse_entry(tok):
    """'<expiry>[:<kind>]' -> (expiry token, kind). The kind is the part after
    the LAST ':' when it names a kind ('2026-10-23T05:50' has no kind: its
    ':50' is the minute); no kind -> unknown."""
    tok = str(tok).strip()
    exp, kind = tok, UNKNOWN
    if ":" in tok:
        head, tail = tok.rsplit(":", 1)
        if tail.strip().lower() in KINDS:
            exp, kind = head.strip(), tail.strip().lower()
    parse_expiry(exp)
    return (UNKNOWN if exp.lower() in ("", UNKNOWN) else exp), kind


def format_entry(p):
    return f"{p[0]}:{p[1]}"


def parse_passes(spec):
    """spec -> list of (expiry token, kind), one per pass, sorted soonest
    first; None = unset; ValueError = malformed."""
    if spec is None or not str(spec).strip():
        return None
    m = _SPEC.match(str(spec).strip().strip("'\""))
    if not m:
        raise ValueError(f"malformed reset pass spec {spec!r} "
                         f"(want 'count|expiry:kind,expiry:kind,...')")
    count, rest = int(m.group(1)), m.group(2)
    if count == 0:
        return []
    toks = [t.strip() for t in rest.split(",")] if rest else []
    passes = [parse_entry(t) for t in toks if t]
    if len(passes) == 1 and count > 1:
        passes = passes * count      # DGN-1965 single-expiry form: shared date
    if len(passes) > count:
        raise ValueError(f"reset pass spec {spec!r} lists {len(passes)} passes for count {count}")
    passes += [(UNKNOWN, UNKNOWN)] * (count - len(passes))
    return sorted(passes, key=_sort_key)


def _sort_key(p):
    e = parse_expiry(p[0])
    return (e is None, e or 0.0, KINDS.index(p[1]))


def format_spec(passes):
    """Inverse of parse_passes: '' for no pass; the count is derived."""
    passes = sorted(passes, key=_sort_key)
    return f"{len(passes)}|{','.join(format_entry(p) for p in passes)}" if passes else ""


def parse(spec):
    """-> (count, soonest known expiry epoch or None); None = unset (the
    DGN-1965 view, kept for callers that only need the head of the stock)."""
    passes = parse_passes(spec)
    if passes is None:
        return None
    known = [e for e in (parse_expiry(p[0]) for p in passes) if e is not None]
    return len(passes), (min(known) if known else None)


def _dur(sec):
    sec = max(0, int(sec))
    d, sec = divmod(sec, 86400)
    h = sec // 3600
    return f"{d}d{h}h" if d else f"{h}h"


def _label(epoch):
    """Owner-facing expiry label (local); a date-only expiry shows its day."""
    lt = time.localtime(epoch)
    if (lt.tm_hour, lt.tm_min) == (0, 0):
        return time.strftime("%Y-%m-%d", time.localtime(epoch - 1))
    return time.strftime("%Y-%m-%d %H:%M", lt)


def state(spec, now, window=None):
    """-> {held, count, expiry (soonest), to_expiry, expiries, expired, text}
    over the live passes; `window` ('5h' | '7d') keeps only the passes whose
    kind covers it (None = every pass)."""
    empty = {"held": False, "count": 0, "expiry": None, "to_expiry": None,
             "expiries": [], "expired": 0}
    try:
        passes = parse_passes(spec)
    except ValueError as e:
        return dict(empty, text=f"invalid ({e}); treated as no pass")
    if passes is None:
        return dict(empty, text="none")
    if not passes:
        return dict(empty, text="x0 (none in hand)")
    eps = [(parse_expiry(x), k) for x, k in passes]
    live = [(e, k) for e, k in eps if e is None or e > now]
    expired = len(eps) - len(live)
    if not live:
        return dict(empty, expired=expired,
                    text=f"x0 ({expired} EXPIRED, treated as no pass)")
    txt = f"x{len(live)} exp=" + ",".join(
        ("unknown" if e is None else _label(e)) + f":{k}" for e, k in live)
    if expired:
        txt += f" +{expired} expired"
    cover = live if window is None else [(e, k) for e, k in live if window in COVERS[k]]
    if not cover:
        return dict(empty, expired=expired,
                    text=f"{txt}; none covers the {WINDOW_TEXT.get(window, window)} window")
    known = [e for e, _ in cover if e is not None]
    soon = min(known) if known else None
    if soon is not None:
        txt += f" (next in {_dur(soon - now)})"
    return {"held": True, "count": len(cover), "expiry": soon,
            "to_expiry": None if soon is None else soon - now,
            "expiries": [e for e, _ in cover], "expired": expired, "text": txt}


def window_name(window_mins):
    """Quota window length -> '5h' | '7d' (anything under a day is the 5h)."""
    try:
        return "5h" if float(window_mins) < 1440 else "7d"
    except (TypeError, ValueError):
        return "7d"


def mode(runtime, spec, used, resets_at, now, window="7d"):
    """-> (mode, prompt_kind, prompt_key, detail) for one runtime's window,
    planned on the soonest live pass whose kind covers that window."""
    st = state(spec, now, window)
    head = f"{runtime} pass {st['text']}"
    if not st["held"]:
        return "none", "", "", head
    wl = WINDOW_TEXT.get(window, window)
    used = float(used)
    if used >= NEAR_SPENT:
        # Window key bucketed to the hour: reset stamps jitter between reads.
        win = "unknown" if resets_at in (None, "") else str(round(float(resets_at) / 3600))
        key = f"{runtime}|near_spent|{window}|{win}"
        return ("near_spent", "near_spent", key,
                f"{head}; {wl} used={used:.0f}% >= {NEAR_SPENT:.0f}% -> "
                f"plan to 100%, ask the owner to redeem (never redeemed here)")
    soon = st["to_expiry"] is not None and st["to_expiry"] <= EXPIRY_SOON_H * 3600
    if soon:
        kind = key = ""
        if st["to_expiry"] <= EXPIRY_PROMPT_H * 3600:
            kind, key = "expiring", f"{runtime}|expiring|{int(st['expiry'])}"
        return ("expiry_boost", kind, key,
                f"{head}; {wl} used={used:.0f}% with the soonest pass expiring within "
                f"{EXPIRY_SOON_H:.0f}h -> boost concurrency ({wl} term dropped)")
    return ("held", "", "",
            f"{head}; {wl} used={used:.0f}% -> window refillable, planned to 100% (no reserve)")


def _short(epoch):
    """'MM-DD' (or 'MM-DD HH:MM') of an expiry label, for the decision line."""
    return _label(epoch)[5:]


def loss_weight(seconds):
    """Urgency of a loss `seconds` away: 1 within LOSS_HORIZON_D, then
    LOSS_HORIZON_D / days (capacity lost a month out can still be caught up)."""
    days = max(0.0, seconds) / 86400
    return min(1.0, LOSS_HORIZON_D / days) if days > 0 else 1.0


def window_loss(spec, used, resets_at, now, window="7d"):
    """-> {loss (points of one window, None = unread), passes, first, rate,
    text} for one quota window (header, Window loss)."""
    used = _num(used)
    if used is None:
        return {"loss": None, "passes": 0, "first": None, "rate": None, "text": "unread"}
    length = WINDOW_SECS.get(window, WINDOW_SECS["7d"])
    reset = _epoch(resets_at)
    to_reset = None if reset is None else max(0.0, reset - now)
    elapsed = None if to_reset is None or to_reset > length else length - to_reset
    if elapsed is not None and elapsed >= LOSS_MIN_ELAPSED * length:
        rate, rsrc = used / elapsed, "burn"
    else:
        rate, rsrc = 100.0 / length, "pace (window young or reset unknown)"
    st = state(spec, now, window)
    known = sorted(e for e in st["expiries"] if e is not None) if st["held"] else []
    horizon = now + LOSS_MAX_HORIZON_D * 86400
    stock = [e for e in known if e <= horizon]
    # Walk the window forward at the burn rate: full -> redeem the soonest
    # live pass (the selector plans to 100%); reset -> the unused part of
    # the FIRST reset is the window's loss; an expiry with the pass unused
    # loses that pass.
    t, u, r = now, min(100.0, max(0.0, used)), reset
    win_loss, lost, first_reset = None, [], True
    while True:
        events = []                       # (epoch, tie order, kind)
        if stock:
            events.append((stock[0], 0, "expiry"))
        if rate > 0 and u < 100.0:
            events.append((t + (100.0 - u) / rate, 1, "fill"))
        if r is not None and (first_reset or stock):
            events.append((r, 2, "reset"))
        if not events:
            break
        nxt, _, kind = min(events)
        u, t = min(100.0, u + rate * max(0.0, nxt - t)), nxt
        if kind == "expiry":
            lost.append(stock.pop(0))
        elif kind == "fill":
            u = 100.0                     # exact: no float creep below full
            if stock:
                stock.pop(0)
                u = 0.0
        else:
            if first_reset:
                win_loss, first_reset = (100.0 - u, r), False
            u, r = 0.0, r + length
    loss, parts = 0.0, []
    if win_loss is not None:
        w = win_loss[0] * loss_weight(win_loss[1] - now)
        loss += w
        parts.append(f"window {w:.0f} by {_short(win_loss[1])}")
    for e in lost:
        w = 100.0 * loss_weight(e - now)
        loss += w
        parts.append(f"pass {_short(e)} unredeemed {w:.0f}")
    txt = f"used={used:.0f}% {rsrc}={rate * 86400:.1f}%/d"
    if to_reset is not None:
        txt += f" reset_in={_dur(to_reset)}"
    n = len(known)
    if st["held"] and not n:
        txt += " pass expiry unknown (never at risk)"
    txt += f" -> {loss:.0f}% at risk" + (f" ({', '.join(parts)})" if parts else "")
    return {"loss": loss, "passes": n, "first": known[0] if n else None,
            "rate": rate, "text": txt}


def slack(spec, used, resets_at, now, window="7d", live_burn=0.0):
    """-> {slack, reserve, passes, held, short, text} or None (used unread):
    the window's refillable headroom left after the live reserve to its
    reset (header, Live reserve). short = held pass and slack < 0."""
    used = _num(used)
    if used is None:
        return None
    reset = _epoch(resets_at)
    length = WINDOW_SECS.get(window, WINDOW_SECS["7d"])
    to_reset = length if reset is None else min(length, max(0.0, reset - now))
    st = state(spec, now, window)
    n = st["count"] if st["held"] else 0
    reserve = max(0.0, float(live_burn)) * to_reset / 3600
    left = 100.0 - min(100.0, max(0.0, used))
    sl = left + 100.0 * n - reserve
    txt = f"{left:.0f}% left"
    if n:
        txt += f" + {n} pass{'es' if n != 1 else ''}"
    if reserve:
        txt += (f" - live {float(live_burn):g}%/h x {to_reset / 3600:.0f}h to reset "
                f"= {reserve:.0f}%")
    txt += f" -> slack {sl:.0f}%"
    return {"slack": sl, "reserve": reserve, "passes": n, "held": st["held"],
            "short": st["held"] and sl < 0, "text": txt}


def _epoch(v):
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


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def review(claude_spec, codex_spec, usage, codex_used, codex_resets_at, now,
           codex_window="7d"):
    """-> (summary, [(runtime, kind, key, expiry_date, window)]) over every
    window read: Claude 5h + 7d, the one Codex window. At most one near_spent
    line per runtime (the weekly one when both windows are nearly full) and
    one expiring line per pass expiry."""
    u = usage or {}
    claude_windows = [("7d", _num(u.get("seven_day_util")), _epoch(u.get("seven_day_resets_at")))]
    if not u.get("five_hour_absent"):
        claude_windows.insert(0, ("5h", _num(u.get("five_hour_util")),
                                  _epoch(u.get("five_hour_resets_at"))))
    lanes = (("claude", claude_spec, claude_windows),
             ("codex", codex_spec, (
                (codex_window, _num(codex_used), _epoch(codex_resets_at)),)))
    parts, prompts = [], []
    for rt, spec, wins in lanes:
        st = state(spec, now)
        due = {}
        for win, used, reset in wins:
            wl = WINDOW_TEXT[win]
            if used is None:
                parts.append(f"{rt} pass {st['text']}; {wl} unread -> mode=none")
                continue
            m, kind, key, detail = mode(rt, spec, used, reset, now, win)
            parts.append(f"{detail} [mode={m}]")
            if kind:
                wst = state(spec, now, win)
                exp = "" if wst["expiry"] is None else _label(wst["expiry"])
                due.setdefault(kind, {})[key] = (rt, kind, key, exp, win)
        near = list(due.get("near_spent", {}).values())
        if near:
            prompts.append(next((p for p in near if p[4] == "7d"), near[0]))
        prompts.extend(due.get("expiring", {}).values())
    return "; ".join(parts), prompts


def once(stamp, key):
    """rc 0 when `key` was not yet recorded in `stamp` (and records it)."""
    os.makedirs(os.path.dirname(os.path.abspath(stamp)), exist_ok=True)
    with open(stamp, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        if key in (line.rstrip("\n") for line in f):
            return 1
        f.write(key + "\n")
        f.flush()
    return 0


# --- settings verbs: the user's own surface writes through these ----------
# (conf line format of the pre-DGN-1983 instance setting; read for migration)

_CONF_LINE = re.compile(r"^(RESET_PASSES_(CLAUDE|CODEX))=(.*)$")


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


# --- shared per-account store (DGN-1983) ----------------------------------
#
# Owner 2026-10-10 19:09: a second agent on the same Mac said no pass was
# registered -- the passes lived in ONE instance's agent.conf while the
# account (and so its passes) is shared by every agent on the host. Same
# defect class as DGN-1978 (per-instance books on a shared pool). The
# inventory now lives once per account per host, next to the shared API
# credit books:
#   $HOME/.dogany/reset-passes/claude/<fp>/passes.json   (+ .lock)
#   $HOME/.dogany/reset-passes/codex/<fp>/passes.json
# fp = first 12 hex of sha256(the account id) (api_credit_pool's
# key_fingerprint; the id itself is never stored): Claude = oauthAccount
# accountUuid (else emailAddress) in $CLAUDE_CONFIG_DIR/.claude.json or
# ~/.claude.json; Codex = tokens.account_id in ${CODEX_HOME:-~/.codex}/auth.json.
# No readable id -> fp 'local' (one host-wide store); once an id resolves,
# the 'local' passes merge into that account's store (the local file keeps a
# moved_to note). Writes run under api_credit_pool.ledger_lock.
# One-time migration (on any read or write with --conf): an uncommented
# RESET_PASSES_* line merges into the store and is replaced by a pointer
# comment. Merge = the same pass seen on two instances counts once: per
# expiry, the known kinds union by multiplicity (max, not sum) and the
# expiry's total is the largest any one source held, so an unknown-kind
# entry is absorbed by a known-kind one with the same expiry. Union is
# idempotent, so a rerun or two instances migrating at once are safe. This
# moves passes the user entered; it never invents one.

STORE_ROOT = os.path.join(".dogany", "reset-passes")     # under $HOME
STORE_FILE = "passes.json"
LOCAL_ACCOUNT = "local"
STORE_SCOPE = "account store, all agents on this Mac"
_POINTER = ("# {key} moved to the shared per-account store {path} "
            "(DGN-1983, {day}); edit with /passes. Was: '{raw}'{note}")


def _ap():
    import api_credit_pool  # lazy: api_credit_pool imports this module
    return api_credit_pool


def _home(home=None):
    return home or os.path.expanduser("~")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def account_id(runtime, env=None, home=None):
    """The signed-in account of `runtime` on this host, or None."""
    env = os.environ if env is None else env
    h = _home(home)
    if runtime == "claude":
        cfg = env.get("CLAUDE_CONFIG_DIR", "")
        paths = ([os.path.join(cfg, ".claude.json")] if cfg else []) + \
            [os.path.join(h, ".claude.json")]
        for path in paths:
            acct = (_read_json(path) or {}).get("oauthAccount") or {}
            v = acct.get("accountUuid") or acct.get("emailAddress") \
                if isinstance(acct, dict) else None
            if v:
                return str(v)
    elif runtime == "codex":
        auth = os.path.join(env.get("CODEX_HOME") or os.path.join(h, ".codex"), "auth.json")
        tok = (_read_json(auth) or {}).get("tokens") or {}
        v = tok.get("account_id") if isinstance(tok, dict) else None
        if v:
            return str(v)
    return None


def onboarded(runtime, env=None, home=None):
    """DGN-1984: whether the user onboarded `runtime` on this host -- Claude
    always (the agent runs on it); Codex when a Codex account is signed in
    (the same auth.json account_id the store is keyed by). Surfaces hide a
    runtime that is not onboarded; the store and the dispatcher do not."""
    if runtime == "claude":
        return True
    if runtime == "codex":
        # One owner of the Codex onboarded flag (DGN-1985): codex_login reads
        # `codex login status` live. Its answer wins when it has one; no CLI
        # or an unreadable answer falls back to the auth.json account below.
        st = _codex_login_status(env)
        if st is not None:
            return st
    return account_id(runtime, env, home) is not None


def _codex_login_status(env=None):
    """True / False from routines/lib/codex_login.py, None when it cannot
    say (lib absent, CLI absent, error)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_login.py")
    if not os.path.isfile(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_dogany_codex_login", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        st = mod.status(env)
    except Exception:
        return None
    state = (st or {}).get("state")
    if state == "logged_in":
        return True
    if state == "logged_out":
        return False
    return None


def store_path(runtime, fp, home=None):
    return os.path.join(_home(home), STORE_ROOT, runtime, fp, STORE_FILE)


def resolve_store(runtime, env=None, home=None):
    """-> (store path, fp) of `runtime`'s account on this host."""
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}")
    # An explicit home is an isolation boundary (tests and alternate hosts):
    # do not let this process's CODEX_HOME redirect the account lookup.
    account_env = {} if home is not None and env is None else env
    aid = account_id(runtime, account_env, home)
    fp = _ap().key_fingerprint(aid) if aid else LOCAL_ACCOUNT
    # DGN-1979 p2: the store already follows the account (one per fp); a
    # switch is recorded once on the host decision ledger. The previous
    # account's store stays as it is (its passes come back with it).
    try:
        import book_identity
        book_identity.check("reset-passes-" + runtime, os.path.join(_home(home), STORE_ROOT, runtime),
                            {runtime + ".account": fp}, home=home)
    except Exception:  # noqa: BLE001 -- the record never fails a pass read
        pass
    return store_path(runtime, fp, home), fp


def _save(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, sort_keys=True) + "\n")
    os.replace(tmp, path)


def store_spec(path):
    """The stored spec ('' = none); a moved-away 'local' store reads empty."""
    doc = _read_json(path) or {}
    return "" if doc.get("moved_to") else str(doc.get("spec") or "")


def merge_passes(*sources):
    """Union of pass lists, one pass seen in several sources counted once
    (header above): per expiry, known kinds by max multiplicity; the
    expiry's total = the most any one source held there."""
    from collections import Counter
    known, total = Counter(), Counter()
    for ps in sources:
        c = Counter(ps)
        known |= Counter({p: n for p, n in c.items() if p[1] != UNKNOWN})
        per = Counter()
        for (exp, _), n in c.items():
            per[exp] += n
        total |= per
    out = list(known.elements())
    for exp, n in total.items():
        have = sum(m for p, m in known.items() if p[0] == exp)
        out += [(exp, UNKNOWN)] * max(0, n - have)
    return sorted(out, key=_sort_key)


def _conf_raw(conf, runtime):
    """The raw value of the uncommented RESET_PASSES_<RT> line, or None."""
    key = CONF_KEY[runtime]
    try:
        with open(conf, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        m = _CONF_LINE.match(line.strip())
        if m and m.group(1) == key:
            return _unquote(m.group(3))
    return None


def _conf_pointer(conf, runtime, path, raw, note, now):
    """Replace the runtime's conf line(s) with one pointer comment."""
    key = CONF_KEY[runtime]
    with open(conf, encoding="utf-8") as f:
        lines = f.read().splitlines()
    ptr = _POINTER.format(key=key, path=path, raw=raw, note=note,
                          day=time.strftime("%Y-%m-%d", time.localtime(now)))
    out, done = [], False
    for line in lines:
        m = _CONF_LINE.match(line.strip())
        if m and m.group(1) == key:
            if not done:
                out.append(ptr)
            done = True
            continue
        out.append(line)
    tmp = f"{conf}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, conf)


def migrate(runtime, conf, now=None, env=None, home=None):
    """Fold the 'local' store (once an account resolves) and the instance
    conf line into the account store. Idempotent. -> (path, fp, merged
    sources)."""
    now = time.time() if now is None else now
    path, fp = resolve_store(runtime, env, home)
    local = store_path(runtime, LOCAL_ACCOUNT, home)
    raw = _conf_raw(conf, runtime) if conf else None
    lspec = store_spec(local) if fp != LOCAL_ACCOUNT else ""
    if raw is None and not lspec:
        return path, fp, []
    done = []
    with _ap().ledger_lock(path):
        doc = _read_json(path) or {}
        cur = parse_passes(doc.get("spec") or "") or []
        srcs = [cur]
        if lspec:
            try:
                srcs.append(parse_passes(lspec) or [])
                done.append({"from": local, "spec": lspec, "ts": int(now)})
            except ValueError:
                pass
        note = ""
        if raw is not None:
            try:
                srcs.append(parse_passes(raw) or [])
                done.append({"from": os.path.abspath(conf), "spec": raw, "ts": int(now)})
            except ValueError as e:
                note = f" -- NOT merged, malformed ({e}); add it again with /passes"
                # Shown as invalid in /passes until the user's next edit.
                doc.update(migration_error=f"old line {raw!r} not merged: {e}",
                           runtime=runtime, account=fp, updated_at=int(now))
                _save(path, doc)
        if done:
            doc.update(spec=format_spec(merge_passes(*srcs)), runtime=runtime,
                       account=fp, updated_at=int(now),
                       migrated=(doc.get("migrated") or []) + done)
            _save(path, doc)
        if lspec:
            ldoc = _read_json(local) or {}
            ldoc.update(moved_to=path, ts=int(now))
            _save(local, ldoc)
        if raw is not None:
            _conf_pointer(conf, runtime, path, raw, note, now)
    return path, fp, done


def effective_spec(runtime, conf=None, env=None, home=None):
    """What the selector plans with: env RESET_PASSES_<RT> wins (as before),
    else the account store (after the one-time migration of `conf`)."""
    env = os.environ if env is None else env
    ev = env.get(CONF_KEY[runtime], "")
    if ev:
        return ev
    try:
        path, _, _ = migrate(runtime, conf, env=env, home=home)
    except (OSError, ValueError):
        path, _ = resolve_store(runtime, env, home)
    return store_spec(path)


def inventory(conf, now, env=None, home=None):
    """-> {runtime: {spec, valid, env_override, store, account, scope,
    passes: [{expiry, kind, label, expired, next}]}} -- what the settings
    surface renders. `next` marks the one pass planned first (the soonest
    live known expiry)."""
    env = os.environ if env is None else env
    out = {}
    for rt in RUNTIMES:
        path, fp, _ = migrate(rt, conf, now, env, home)
        stored = store_spec(path)
        ev = env.get(CONF_KEY[rt], "")
        spec = ev or stored
        row = {"spec": stored, "env_override": bool(ev), "valid": True, "passes": [],
               "store": path, "account": fp, "scope": STORE_SCOPE,
               "onboarded": onboarded(rt, env, home)}
        try:
            passes = parse_passes(spec) or []
        except ValueError as e:
            row.update(valid=False, error=str(e))
            passes = []
        merr = (_read_json(path) or {}).get("migration_error")
        if merr and not ev:
            row.update(valid=False, error=merr)
        soon = state(spec, now)["expiry"]
        for exp, kind in passes:
            e = parse_expiry(exp)
            nxt = soon is not None and e == soon
            soon = None if nxt else soon
            row["passes"].append({
                "expiry": exp, "kind": kind,
                "label": "unknown" if e is None else _label(e),
                "expired": e is not None and e <= now,
                "next": nxt})
        out[rt] = row
    return out


def _edit(conf, runtime, fn, home=None):
    """Run fn(passes) -> passes|None on the account store under its lock;
    None = no change. -> fn's result is not None."""
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}")
    path, fp, _ = migrate(runtime, conf, home=home)
    with _ap().ledger_lock(path):
        doc = _read_json(path) or {}
        try:
            passes = parse_passes(doc.get("spec") or "") or []
        except ValueError:
            passes = []      # a malformed store is replaced by the user's own entry
        new = fn(passes)
        if new is None:
            return False
        doc.update(spec=format_spec(new), runtime=runtime, account=fp,
                   updated_at=int(time.time()))
        doc.pop("migration_error", None)
        _save(path, doc)
    return True


def add(conf, runtime, expiry, kind=UNKNOWN, home=None):
    """Register one pass (a user action); refuses an invalid or already-
    expired expiry or an unknown kind."""
    kind = parse_kind(kind)
    e = parse_expiry(expiry)
    if e is not None and e <= time.time():
        raise ValueError(f"expiry {expiry} is already past")
    tok = UNKNOWN if e is None else expiry.strip()
    _edit(conf, runtime, lambda ps: ps + [(tok, kind)], home)


def remove(conf, runtime, expiry, kind="", home=None):
    """Drop ONE pass with that expiry (and kind, when given); False when none
    matched."""
    want = UNKNOWN if str(expiry).strip().lower() in ("", UNKNOWN) else str(expiry).strip()
    k = parse_kind(kind) if str(kind or "").strip() else None

    def drop(ps):
        for p in ps:
            if p[0] == want and (k is None or p[1] == k):
                ps.remove(p)
                return ps
        return None
    return _edit(conf, runtime, drop, home)


_SUMMARY_T = {
    "en": {"title": "Reset passes (shared by all agents on this Mac)",
           "none": "none", "next": "next", "set": "edit: /passes"},
    "ko": {"title": "\ub9ac\uc14b\uad8c (\uc774 Mac \uacf5\uc6a9)",
           "none": "\uc5c6\uc74c", "next": "\ub2e4\uc74c", "set": "\uc124\uc815: /passes"},
}


def usage_block(inv, locale="en"):
    """-> the /usage block (claude-usage.sh): one line per runtime, live
    passes only, from inventory(). A runtime not onboarded is left out
    (DGN-1984)."""
    T = _SUMMARY_T.get(locale, _SUMMARY_T["en"])
    lines = [T["title"]]
    for rt in RUNTIMES:
        row = inv.get(rt) or {}
        if row.get("onboarded") is False:
            continue
        live = [p for p in row.get("passes") or [] if not p.get("expired")]
        txt = f"{rt.capitalize()}: "
        if not live:
            txt += T["none"]
        else:
            nxt = next((p for p in live if p.get("next")), live[0])
            lbl = nxt["label"] if nxt["label"] == UNKNOWN else nxt["label"][5:]
            txt += f"x{len(live)} ({T['next']} {lbl} {nxt['kind']})"
        lines.append(txt)
    lines.append(T["set"])
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mode")
    m.add_argument("--runtime", required=True)
    m.add_argument("--spec", default="")
    m.add_argument("--used", type=float, required=True)
    m.add_argument("--resets-at", default="")
    m.add_argument("--window", default="7d", choices=("5h", "7d"))
    m.add_argument("--now", type=float, default=None)
    s = sub.add_parser("slack")
    s.add_argument("--runtime", required=True)
    s.add_argument("--spec", default="")
    s.add_argument("--used", type=float, required=True)
    s.add_argument("--resets-at", default="")
    s.add_argument("--window", default="7d", choices=("5h", "7d"))
    s.add_argument("--live-burn", type=float, default=0.0)
    s.add_argument("--now", type=float, default=None)
    r = sub.add_parser("review")
    r.add_argument("--claude-spec", default="")
    r.add_argument("--codex-spec", default="")
    r.add_argument("--codex-used", default="")
    r.add_argument("--codex-resets-at", default="")
    r.add_argument("--codex-window-mins", default="10080")
    r.add_argument("--now", type=float, default=None)
    x = sub.add_parser("loss")
    x.add_argument("--runtime", required=True)
    x.add_argument("--spec", default="")
    x.add_argument("--used", default="")
    x.add_argument("--resets-at", default="")
    x.add_argument("--window", default="7d", choices=("5h", "7d"))
    x.add_argument("--now", type=float, default=None)
    o = sub.add_parser("once")
    o.add_argument("--stamp", required=True)
    o.add_argument("--key", required=True)
    sp = sub.add_parser("specs")
    sp.add_argument("--conf", default="")
    sm = sub.add_parser("summary")
    sm.add_argument("--conf", default="")
    sm.add_argument("--locale", default="en")
    for verb in ("list", "add", "remove"):
        v = sub.add_parser(verb)
        v.add_argument("--conf", required=True)
        if verb != "list":
            v.add_argument("--runtime", required=True, choices=RUNTIMES)
            v.add_argument("--expiry", default=UNKNOWN)
            v.add_argument("--kind", default=UNKNOWN if verb == "add" else "")
    a = p.parse_args(argv)
    if a.cmd == "once":
        return once(a.stamp, a.key)
    if a.cmd == "summary":
        print(usage_block(inventory(a.conf or None, time.time()), a.locale.strip().lower()))
        return 0
    if a.cmd == "specs":
        out = []
        for rt in RUNTIMES:
            try:
                out.append(effective_spec(rt, a.conf or None) or "-")
            except (OSError, ValueError) as e:
                print(f"{rt}: {e}", file=sys.stderr)
                out.append("-")
        print("\t".join(out))
        return 0
    if a.cmd in ("list", "add", "remove"):
        try:
            if a.cmd == "add":
                add(a.conf, a.runtime, a.expiry, a.kind)
            elif a.cmd == "remove" and not remove(a.conf, a.runtime, a.expiry, a.kind):
                print(f"no {a.runtime} pass with expiry {a.expiry}"
                      + (f" and kind {a.kind}" if a.kind else ""), file=sys.stderr)
                return 2
        except (ValueError, OSError) as e:
            print(str(e), file=sys.stderr)
            return 2
        print(json.dumps(inventory(a.conf, time.time()), ensure_ascii=False))
        return 0
    now = a.now if a.now is not None else time.time()
    if a.cmd == "loss":
        w = window_loss(a.spec, a.used, a.resets_at, now, a.window)
        loss = "-" if w["loss"] is None else f"{w['loss']:.1f}"
        print(f"{loss}\t{a.runtime} {w['text']}")
        return 0
    if a.cmd == "slack":
        out = slack(a.spec, a.used, a.resets_at, now, a.window, a.live_burn)
        if out is None:
            return 1
        print(f"{out['slack']:g}\t{out['text']}")
        return 0
    if a.cmd == "review":
        usage = None
        raw = sys.stdin.read().strip()
        if raw:
            try:
                usage = json.loads(raw).get("usage")
            except (ValueError, AttributeError):
                usage = None
        summary, prompts = review(a.claude_spec, a.codex_spec, usage,
                                  a.codex_used, a.codex_resets_at, now,
                                  window_name(a.codex_window_mins))
        print(summary)
        for row in prompts:
            print("\t".join(["PROMPT"] + [c or "-" for c in row]))
        return 0
    print("\t".join(c or "-" for c in mode(a.runtime, a.spec, a.used, a.resets_at, now,
                                            a.window)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
