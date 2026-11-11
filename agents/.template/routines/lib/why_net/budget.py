"""why_net.budget -- conf keys, never-ask checks and tuning (spec 2.6, 4.2).

`check()` runs the never-ask list in the frozen order and returns
(refuse_reason | None, flags). `tune()` computes the effective inline cap and
reveal-only window; derive runs it nightly and ups runs it at once on an
annoyance row. budget.json is the only place tuning is written (never conf).

Grill fix 10: budget.json is a cache, not the authority for a halving.  The
annoyance halving and reveal-only window are recomputed from the annoy rows
in probes.jsonl on every check (`annoy_state`), so deleting budget.json never
brings the full cap back.  Topic keys are canonicalised (`canonical_topic`)
before any topic rule reads or writes them.
"""

import os
import re
import sys
import time
import unicodedata

from . import store

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
from conf_reader import conf_get  # noqa: E402

KST = 9 * 3600
DAY = 86400
DAY_BOUNDARY_HOUR = 5  # 05:00 KST

DEFAULTS = {
    "WHY_NET": "off",
    "WHY_ASK_INLINE_DAY": "3",
    "WHY_ASK_INLINE_GAP_MIN": "30",
    "WHY_ASK_PER_TURN": "1",
    "WHY_ASK_MORNING_MAX": "3",
    "WHY_ASK_MORNING_WINDOW": "05:00-09:00",
    "WHY_ASK_TOPIC_MAX": "2",
    "WHY_ASK_PARK_DAYS": "7",
    "WHY_ASK_HARD_DAY": "6",
    "WHY_QUIET": "23:00-05:00",
    "WHY_RUSH_MODE": "log",
    # AMEND-1 2.1: the act side (rev-1 weight keys are never added)
    "WHY_SETTLE_WINS": "2",
    "WHY_ACT_WINDOW_N": "4",
    "WHY_ACT_WINDOW_DAYS": "30",
    "WHY_STALE_DAYS": "90",
    # AMEND-1 rev 3: the retro slot (DGN-1961) owns the Why section once it
    # is on.  DGN-1961: the switch is the framework's RETRO_SLOT
    # (lib/retro_slot.py); this key stays as its alias.
    "WHY_RETRO_SLOT": "off",
    # Final grill B2: the bare 07:00 push is opt-IN.  Off (default) the
    # morning run sends nothing; queued asks wait for the retro slot or an
    # inline gate.
    "WHY_MORNING_FALLBACK": "off",
    # Final grill B3: items one retro day hands out (one per turn).
    # DGN-1961: may go down, never above 3 (retro.cap clamps).
    "WHY_RETRO_MAX": "3",
    # a Why read / acted on across this many work items is an umbrella:
    # never confirmed (trial-run rule)
    "WHY_UMBRELLA_WORKS": "3",
}
INLINE_DAY_BOUNDS = (1, 5)

# Owner-speech corpus (matched, never shown): the spec 4.2 rush words.
RUSH_WORDS = ("\ube68\ub9ac", "\uae09\ud574", "\uc9c0\uae08 \ubc14\ub85c",
              "\uadf8\ub0e5 \ud574", "\ub098\uc911\uc5d0", "\uc774\ub530")
# Stored in owner-turns rush_words[] when the turn itself is a short
# imperative (the ring keeps no text, spec 1).
SHORT_IMPERATIVE = "<short-imperative>"
SHORT_LEN = 8
RUSH_BURST_N = 3
RUSH_BURST_SEC = 120


AGENT_ROOT = store.root_dir()  # $DOGANY_ROOT, else the instance this file lives in

ANNOY_HALVE_DAYS = 3

# Grill fix 10: one spelling per topic.  DGN tickets fold leading zeros and
# case ("dgn-0010 " == "DGN-10"); Why and decision ids keep their frozen
# padded form (2.3 topic enum).  Anything else is not a topic.
_TOPIC_RE = re.compile(r"^\s*(dgn|why|dec)-0*(\d{1,7})\s*$", re.IGNORECASE)
# AMEND-2 2.2 (F5): a turn ask with no ticket goes by its target.
TGT_PREFIX = "tgt:"

def canonical_topic(topic):
    if isinstance(topic, str) and topic.startswith(TGT_PREFIX):
        from .nodes import TARGET_RE
        return topic if TARGET_RE.match(topic[len(TGT_PREFIX):]) else None
    m = _TOPIC_RE.match(topic or "")
    if not m:
        return None
    kind, num = m.group(1).lower(), int(m.group(2))
    if kind == "dgn":
        return "DGN-%d" % num
    if kind == "why":
        return "why-%04d" % num
    return "dec-%03d" % num


class Ctx(object):
    """Root, conf path and clock for one invocation.  The store location is
    P1's (store.store_dir(): $DOGANY_BOT_DATA_DIR or <root>/.telegram_bot)."""

    def __init__(self, root=None, now=None, conf_path=None):
        self.root = root or AGENT_ROOT
        self.ddir = store.store_dir()
        self.bot_dir = store.bot_data_dir()
        self.conf_path = conf_path or os.environ.get("DOGANY_AGENT_CONF") or os.path.join(
            self.root, "config", "agent.conf")
        self._now = now
        if now is not None:
            # an injected clock stamps the store's rows too (DGN-1967 bug 1)
            store.bind_clock(lambda: self.now)

    @property
    def now(self):
        return time.time() if self._now is None else self._now

    def conf(self, key):
        return conf_get(key, self.conf_path) or DEFAULTS.get(key, "")

    def conf_int(self, key):
        try:
            return int(self.conf(key))
        except ValueError:
            return int(DEFAULTS[key])

    def enabled(self):
        return self.conf("WHY_NET").lower() == "on"


# ---- time helpers ---------------------------------------------------------

def day_start(ts):
    """Epoch of the 05:00 KST day boundary at or before ts."""
    local = ts + KST - DAY_BOUNDARY_HOUR * 3600
    return ts - (local % DAY)


def _minutes_kst(ts):
    return int(((ts + KST) % DAY) // 60)


def _parse_window(spec):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", spec or "")
    if not m:
        return None
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return a, b


def in_window(ts, spec):
    w = _parse_window(spec)
    if w is None:
        return False
    a, b = w
    m = _minutes_kst(ts)
    return a <= m < b if a <= b else (m >= a or m < b)


# ---- ask fold -------------------------------------------------------------

# rev 3: `moved` = a queued ask a `gate` pulled inline (moved_to names it)
TERMINAL = ("answered", "declined", "ignored", "not_shown", "refused", "moved")


def fold_asks(rows):
    """ask id -> merged state. `state` = last lifecycle ev; `shown_at` epoch."""
    asks = {}
    for r in rows:
        aid = r.get("ask")
        if not aid:
            continue
        a = asks.setdefault(aid, {"ask": aid, "events": []})
        ev = r.get("ev")
        a["events"].append(ev)
        for k, v in r.items():
            if k in ("ev", "prev", "at"):
                continue
            if v is not None or k not in a:
                a[k] = v
        at = store.parse_iso(r.get("at"))
        if ev == "create":
            a["created_at"] = at
        if ev == "shown":
            a["shown_at"] = at
        if ev == "queued":
            a["queued_at"] = at
        if ev == "parked":
            a["parked_at"] = at
        if ev == "awaiting":
            # AMEND-2 2.7: "another reason" tapped; the typed reason follows
            a["awaiting_at"] = at
        if ev in ("answered", "declined", "ignored", "not_shown"):
            a["resolved_at"] = at
        if ev != "create":
            a["state"] = ev
        else:
            a.setdefault("state", "create")
    for a in asks.values():
        a["sent_at"] = sent_at(a)
    return asks


def budget_window_start(ctx):
    """The oldest instant any budget rule reads (topic park window, the day
    cap, the inline gap), less a day of margin: an ask whose every row is
    older than this cannot trip check()."""
    return min(ctx.now - ctx.conf_int("WHY_ASK_PARK_DAYS") * DAY, day_start(ctx.now)) - DAY


def fold_asks_window(ctx, rows, keep=()):
    """fold_asks over the asks with a row inside budget_window_start (plus
    the ask ids in `keep`), every row of each.  check() gives the same
    answer on it as on the full fold (rows are appended in time order and
    `at` is the store's fixed ISO form, so a string compare orders them).
    AMEND-2 G4: the frame verb's hot path."""
    cutoff = store.iso_at(budget_window_start(ctx))
    ids = set(keep)
    for r in rows:
        if (r.get("at") or "") >= cutoff and r.get("ask"):
            ids.add(r["ask"])
    return fold_asks([r for r in rows if r.get("ask") in ids])


def sent_at(a):
    """When an ask went to the owner, for the caps.  A morning ask is sent
    when push.sh returned its bubble (`shown`).  An inline ask is issued
    when the verb printed it (`create` that was not refused); whether the
    agent really sent it is proven later (`shown` / `not_shown`, grill fix
    3), but it counts against the caps from the moment it was issued."""
    if a.get("shown_at") is not None:
        return a["shown_at"]
    if "refused" in a["events"]:
        return None
    if a.get("channel") == "inline" and a.get("created_at") is not None:
        return a["created_at"]  # reveal asks too: eligible, never budgeted
    return None


def owner_turns(ctx):
    return store.read_rows(store.OWNER_TURNS)


# ---- tuning ---------------------------------------------------------------

def load_budget(ctx):
    import json
    try:
        with open(store.derived_path("budget.json"), encoding="utf-8") as fh:
            b = json.load(fh)
    except (OSError, ValueError):
        return {}
    return b if isinstance(b, dict) else {}


def _bounded(n):
    lo, hi = INLINE_DAY_BOUNDS
    return max(lo, min(hi, n))


def annoy_rows(ctx, since):
    return [p for p in store.read_rows(store.PROBES)
            if "annoy" in p and (store.parse_iso(p.get("at")) or 0) >= since]


def annoy_state(ctx):
    """(halved cap | None, reveal_only_until | None) from the annoy rows of
    the last ANNOY_HALVE_DAYS -- the store, not budget.json, is the source."""
    now = ctx.now
    rows = annoy_rows(ctx, now - ANNOY_HALVE_DAYS * DAY)
    if not rows:
        return None, None
    last = max(store.parse_iso(r.get("at")) or 0 for r in rows)
    base = _bounded(ctx.conf_int("WHY_ASK_INLINE_DAY"))
    return max(1, base // 2), last + ANNOY_HALVE_DAYS * DAY


def effective_inline_cap(ctx, b=None):
    b = load_budget(ctx) if b is None else b
    base = _bounded(ctx.conf_int("WHY_ASK_INLINE_DAY"))
    cap = b.get("inline_cap")
    cap = _bounded(int(cap)) if isinstance(cap, (int, float)) else base
    halved, _ = annoy_state(ctx)
    return min(cap, halved) if halved is not None else cap


def reveal_only_until(ctx, b=None):
    b = load_budget(ctx) if b is None else b
    ro = store.parse_iso(b.get("reveal_only_until"))
    _, annoy_ro = annoy_state(ctx)
    vals = [v for v in (ro, annoy_ro) if v is not None]
    return max(vals) if vals else None


def answer_rate(asks, now, days=7):
    """answered / (answered + declined + ignored) over shown, budgeted asks
    resolved in the window. not_shown and reveal asks are excluded."""
    lo = now - days * DAY
    num = den = 0
    for a in asks.values():
        if a.get("trigger") == "reveal" or "shown" not in a["events"]:
            continue
        st = a.get("state")
        if st not in ("answered", "declined", "ignored"):
            continue
        if (a.get("resolved_at") or 0) < lo:
            continue
        den += 1
        num += st == "answered"
    return (num / den if den else None), den


def tune(ctx, annoy_now=False):
    """Recompute and write derived/budget.json. Returns the dict."""
    b = load_budget(ctx)
    now = ctx.now
    cap = effective_inline_cap(ctx, b)
    asks = fold_asks(store.read_rows(store.ASKS))
    rate, n = answer_rate(asks, now)
    recent_annoy = annoy_rows(ctx, now - 7 * DAY)
    halved, annoy_ro = annoy_state(ctx)
    action = "hold"
    if annoy_now or halved is not None or (rate is not None and rate < 0.40):
        cap = max(1, cap // 2) if halved is None else min(cap, halved)
        ro = annoy_ro if annoy_ro is not None else now + 3 * DAY
        if annoy_now:
            ro = max(ro, now + 3 * DAY)
        b["reveal_only_until"] = store.iso_at(ro)
        action = "halve"
    elif rate is not None and rate >= 0.70 and not recent_annoy:
        cap = _bounded(cap + 1)
        action = "raise"
    b.update({"inline_cap": cap, "answer_rate_7d": rate, "answer_n_7d": n,
              "tuned_at": store.iso_at(now), "last_action": action})
    store.write_json_atomic(store.derived_path("budget.json"), b)
    return b


# ---- rush -----------------------------------------------------------------

def rush_words_in(text):
    return [w for w in RUSH_WORDS if w in (text or "")]


def is_rush(turns):
    """turns = owner-turns ring rows, oldest first."""
    if not turns:
        return False
    last = turns[-1]
    if SHORT_IMPERATIVE in (last.get("rush_words") or []):
        return True
    if len(turns) >= RUSH_BURST_N:
        a = store.parse_iso(turns[-RUSH_BURST_N].get("ts"))
        b = store.parse_iso(last.get("ts"))
        if a is not None and b is not None and b - a <= RUSH_BURST_SEC:
            return True
    for t in turns[-2:]:
        if any(w != SHORT_IMPERATIVE for w in (t.get("rush_words") or [])):
            return True
    return False


# ---- never-ask (4.2) ------------------------------------------------------

def _prev_owner_turn_ts(turns, session_id):
    for t in reversed(turns):
        if session_id is None or t.get("session_id") == session_id:
            return store.parse_iso(t.get("ts"))
    return None


def text_key(text):
    """Grill-3 fix 10: a candidate's wording as a topic key, so the same
    Whys re-proposed under fresh ids (or passed as literal --cand text) are
    the same topic."""
    if not isinstance(text, str) or not text.strip():
        return None
    return "txt:" + store.sha256_text(unicodedata.normalize("NFC", text).strip())


def topic_keys(topic, work=None, whys=(), texts=(), target=None):
    """Every key one topic goes by (grill-2 fix 9): its canonical topic, the
    canonical work item and each Why id it asks about.  A decline on any of
    them covers the others, so switching the key (`--topic why-0001 --work
    DGN-10` after DGN-10 was declined) is the same topic.  Grill-3 fix 10:
    plus the text_key of each candidate's wording."""
    keys = {canonical_topic(topic), canonical_topic(work)}
    keys.update(canonical_topic(w) for w in whys or ())
    keys.update(text_key(t) for t in texts or ())
    keys.discard(None)
    return keys


def ask_texts(a):
    """The candidate wordings of an ask row: `cand_texts` when recorded,
    else its non-escape labels (a `confirm` label is the yes-button, not
    the Why)."""
    if isinstance(a.get("cand_texts"), list):
        return a["cand_texts"]
    if a.get("shape") == "confirm":
        return []
    return [s.get("label") for s in a.get("shown") or [] if not s.get("escape")]


def ask_topic_keys(a):
    return topic_keys(a.get("topic"), a.get("work_ref"),
                      [s.get("why") for s in a.get("shown") or [] if s.get("why")],
                      ask_texts(a), a.get("target"))


def topic_check(ctx, topic, asks, keys=None):
    """The topic rules of 4.2 step 4 (also run by `enqueue`, grill fix 10).
    `keys` (topic_keys) are every alias of the topic; default the topic.
    Returns (reason | None, park_flag)."""
    now = ctx.now
    park = ctx.conf_int("WHY_ASK_PARK_DAYS") * DAY
    keys = set(keys or ()) | {topic}
    # Only an ask that declined, parked or was sent inside the park window
    # can trip a rule below; the time test runs before the (costlier) topic
    # keys.  Same result as testing every ask (AMEND-2 G4).
    tops = [a for a in asks.values()
            if a.get("trigger") != "reveal"
            and max(a.get("resolved_at") or 0, a.get("parked_at") or 0,
                    a.get("sent_at") or 0) >= now - park
            and ask_topic_keys(a) & keys]
    for a in tops:
        if a.get("state") == "declined" and (a.get("resolved_at") or 0) >= now - park:
            return "topic-declined-7d", False
    for a in tops:
        if "parked" in a["events"] and (a.get("parked_at") or a.get("resolved_at") or 0) >= now - park:
            return "topic-cap", False
    unanswered = [a for a in tops if a.get("sent_at") is not None
                  and a.get("state") not in ("answered", "not_shown")
                  and a["sent_at"] >= now - park]
    if len(unanswered) >= ctx.conf_int("WHY_ASK_TOPIC_MAX"):
        return "topic-cap", True
    return None, False


def check(ctx, topic, channel="inline", urgent=False, session_id=None, asks=None,
          keys=None):
    """Returns (reason | None, flags). Frozen order (4.2).  `topic` must be
    canonical (canonical_topic)."""
    flags = {}
    now = ctx.now
    if urgent:
        return "urgent", flags
    if os.path.exists(os.path.join(ctx.bot_dir, "dispatch-return-context.json")):
        return "dispatch-return-pending", flags
    turns = owner_turns(ctx)
    if channel == "inline" and is_rush(turns):
        if ctx.conf("WHY_RUSH_MODE").lower() == "enforce":
            return "rush", flags
        flags["rush_would_refuse"] = True
    asks = fold_asks(store.read_rows(store.ASKS)) if asks is None else asks
    reason, park = topic_check(ctx, topic, asks, keys)
    if reason:
        if park:
            flags["park"] = True
        return reason, flags
    if channel == "inline":
        b = load_budget(ctx)
        ro = reveal_only_until(ctx, b)
        budgeted = [a for a in asks.values() if a.get("trigger") != "reveal"
                    and a.get("sent_at") is not None]
        inline = [a for a in budgeted if a.get("channel") == "inline"]
        prev = _prev_owner_turn_ts(turns, session_id)
        this_turn = [a for a in inline if a.get("session_id") == session_id
                     and (prev is None or a["sent_at"] > prev)]
        if len(this_turn) >= ctx.conf_int("WHY_ASK_PER_TURN"):
            return "budget-turn", flags
        gap = ctx.conf_int("WHY_ASK_INLINE_GAP_MIN") * 60
        if any(a["sent_at"] > now - gap for a in inline):
            return "budget-30min", flags
        ds = day_start(now)
        today = [a for a in inline if a["sent_at"] >= ds]
        # hard-day and reveal-only have no enum value of their own (spec 2.3);
        # both report as budget-inline-day.
        if len(today) >= effective_inline_cap(ctx, b):
            return "budget-inline-day", flags
        if len([a for a in budgeted if a["sent_at"] >= ds]) >= ctx.conf_int("WHY_ASK_HARD_DAY"):
            return "budget-inline-day", flags
        if ro is not None and now < ro:
            return "budget-inline-day", flags
    else:
        # rev 3: the retro slot (DGN-1961) runs in the evening, so only the
        # quiet hours bound it; the morning fallback keeps its window.
        if in_window(now, ctx.conf("WHY_QUIET")) or (channel != "retro" and not in_window(
                now, ctx.conf("WHY_ASK_MORNING_WINDOW"))):
            return "quiet-hours", flags
        ds = day_start(now)
        shown_today = [a for a in asks.values() if a.get("trigger") != "reveal"
                       and (a.get("sent_at") or 0) >= ds]
        if len(shown_today) >= ctx.conf_int("WHY_ASK_HARD_DAY"):
            return "budget-inline-day", flags
    return None, flags
