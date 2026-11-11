"""retro_slot -- the framework daily retro slot (DGN-1961; dec-337 / dec-338).

One owner message a day (or a week: RETRO_SLOT_CADENCE), owned by the
framework.  Kit retro content plugs in
as ordered sections; the Why net's confirm questions are one framework
section (the bare 07:00 Why push is off while the slot is on).  Weekly recap
and monthly text sections are cadence add-ons.  The feature ships OFF: conf
RETRO_SLOT=on is the one switch.

Slot contract
-------------
- Time: conf RETRO_SLOT_TIME (default 21:00, instance-configurable); the
  deferred part goes out at RETRO_SLOT_MORNING_TIME (default 08:00) the
  next morning.  Nothing is sent inside RETRO_SLOT_QUIET (23:00-05:00).
  The unit fires every few minutes; `tick` decides (so moving the time is
  a conf edit, no unit reload).
- One owner message per retro day: title + the sections' bodies in order.
  Every section empty -> no message (the day is still marked done).
- Title carries the year: "2026-10-07(<weekday>)" (weekday names are i18n).
- Cadence (DGN-1961, owner 2026-10-11): conf RETRO_SLOT_CADENCE = daily
  (default) | weekly; RETRO_SLOT_WEEKDAY (default Sun) names the weekly day.
  An unknown value runs as daily (weekday: Sun) and logs
  `signal::RETRO_SLOT_CONF_UNKNOWN` every beat -- never silently off.
  daily:  one message every retro day.  On the weekly day the message is
          the weekly recap and REPLACES the daily one (only sections
          registered for "weekly" render); the last day of a month adds
          the "monthly" sections without picture delivery.
  weekly: no daily message.  On the weekly day at RETRO_SLOT_TIME one
          message covers the 7 retro days ending that day (`Day.days`):
          a "weekly" section renders once for the week; a daily-only
          section is rendered once per day of the week (each body under
          its day label), so no day's content is lost; a "monthly"
          section renders (with the month-end day) when a month ended
          inside the week.  The interactive walk is the same one item per
          turn with the stop button; the Why net hands out up to 7 daily
          budgets in that one walk (`why_net.retro.cap`).  The morning
          defer and the quiet window apply unchanged.
  The plist is the same 5-minute beat in both modes: `tick` decides.
- Cadence proposal: the slot measures, over a trailing window, the items a
  day that would enter the retro and how many retro days got an answer.
  Low activity or mostly unanswered retros while daily -> it PROPOSES
  weekly; high activity while weekly -> it proposes daily.  One line with
  two buttons, through the slot message path (see `proposal_due`).  The
  conf changes only on the person's tap / typed button label
  (`proposal_answer`, called by the owner-turn hook); never by itself.

Section hook (the kit-facing contract)
--------------------------------------
A kit ships `payload/routines/bundle/retro_sections/<name>.py`, installed
as `routines/bundle/retro_sections/<name>.py`, defining

    def register(slot):
        slot.register("<name>", <order>, render, defer=False,
                      cadences=("daily",))

    def render(day, ctx):        # -> body text, or None for nothing
        ...

`day` is a `Day` (date, cadences, week_start, days, label(ctx)); `ctx` carries
`root`, `now`, `conf(key)`.  A section registered with defer=True may return
`DEFER` (e.g. the day's last appointment ends late): it is left out of the
evening message and rendered again for the same day the next morning, as its
own short message.  A body may also be a dict {"text", "buttons",
"on_sent"}: an interactive section (at most one per message; the first in
order wins).  `on_sent(ctx, message_id, chat_id, full_text)` binds the sent
message.  register(..., next=fn): fn(ctx) returns the interactive section's
next item for the turn after the owner answered (the Why section hands out
one item per turn).  register(..., count=fn): fn(ctx, start, end) -> how many
items the section had for the retro days start..end (dates, inclusive); it
feeds the cadence proposal (optional).  A kit section that
raises is skipped, never blocking the others.  Instance-local modules in
`routines/retro_sections/` load first.  A module name present in both places
is loaded only from the instance-local path (even if it raises); the skipped
bundle duplicate is noted in `slot.errors`.

Picture delivery is deferred to 3.0. The plan keeps a null picture field for
kit callers; picture hooks never run in 2.7.0. Existing pin state is untouched.

Copy: owner-facing strings live in one i18n block (config/i18n/{ko,en}.json,
keys `retro.slot.*`); COPY_KEYS_PENDING_OWNER_CONFIRM below await the owner,
COPY_KEYS_CONFIRMED are confirmed (cadence proposal, dec-370).  Owner wording
never says "Why".

State: <bot data dir>/retro-slot.json {"days": {"YYYY-MM-DD": {"evening",
"deferred", "morning", "message_id", "interactive", "proposal"}},
"cadence": {"mode", "since"}, "proposal": {..}}, private, 14 days kept.

Pure stdlib.  English/ASCII only.
"""

import calendar
import datetime
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

KST = 9 * 3600
DAY_BOUNDARY_HOUR = 5        # same retro-day boundary as the Why net
KEEP_DAYS = 14
SEND_TRIES = 3

DEFAULTS = {
    "RETRO_SLOT": "off",
    "RETRO_SLOT_TIME": "21:00",
    "RETRO_SLOT_MORNING_TIME": "08:00",
    "RETRO_SLOT_QUIET": "23:00-05:00",
    "RETRO_SLOT_CADENCE": "daily",
    "RETRO_SLOT_WEEKDAY": "Sun",
    # the cadence proposal (DGN-1961, 2026-10-11)
    "RETRO_SLOT_PROPOSE": "on",
    "RETRO_SLOT_PROPOSE_WINDOW_DAYS": "14",
    "RETRO_SLOT_PROPOSE_LOW_ITEMS": "1",
    "RETRO_SLOT_PROPOSE_HIGH_ITEMS": "3",
    "RETRO_SLOT_PROPOSE_LOW_ANSWER": "0.3",
    "RETRO_SLOT_PROPOSE_MIN_DAYS": "4",
}

DAILY, WEEKLY, BOTH, MONTHLY = "daily", "weekly", "both", "monthly"
CADENCES = (DAILY, WEEKLY, MONTHLY)
WHY_SECTION = "why"
WHY_ORDER = 900               # framework section: after the kit sections
LINK_REVIEW_SECTION = "why-link-review"
SECTIONS_DIR = os.path.join("routines", "retro_sections")
BUNDLE_SECTIONS_DIR = os.path.join("routines", "bundle", "retro_sections")
STATE_FILE = "retro-slot.json"

# DGN-1961: every owner-facing string of the slot, one block, owner review.
COPY_KEYS_PENDING_OWNER_CONFIRM = (
    "retro.slot.title.daily", "retro.slot.title.weekly",
    "retro.slot.title.morning", "retro.slot.weekdays",
    "retro.slot.why.settled",
)
# DGN-1961 cadence (2026-10-11): the proposal line and its buttons.
# Owner-confirmed as drafted (dec-370, 2026-10-11).
COPY_KEYS_CONFIRMED = (
    "retro.slot.missed.morning", "retro.slot.missed.start",
    "retro.slot.cadence.weekly.low", "retro.slot.cadence.weekly.skip",
    "retro.slot.cadence.daily.high", "retro.slot.cadence.btn.weekly",
    "retro.slot.cadence.btn.daily", "retro.slot.cadence.btn.keep",
)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS_FULL = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                 "sunday")
PROPOSAL_TTL = 7 * 86400      # a proposal button binds for a week at most
CADENCE_SECTION = "cadence"   # the plan's interactive name for the proposal


class _Defer(object):
    def __repr__(self):
        return "DEFER"


DEFER = _Defer()


def slot_on(ctx):
    """The one switch.  WHY_RETRO_SLOT=on (the Why net's pre-slot key) is
    read as an alias so an instance that set it keeps its meaning."""
    for key in ("RETRO_SLOT", "WHY_RETRO_SLOT"):
        if (conf(ctx, key) or "off").strip().lower() == "on":
            return True
    return False


def conf(ctx, key):
    v = ctx.conf(key)
    return v if v else DEFAULTS.get(key, "")


def _num(ctx, key):
    try:
        return float(conf(ctx, key))
    except ValueError:
        return float(DEFAULTS[key])


def cadence(ctx):
    """-> (mode, weekday 0-6, [(key, bad value)]).  An unknown value runs
    as the default (daily / Sunday) and is reported, never dropped."""
    bad = []
    mode = (conf(ctx, "RETRO_SLOT_CADENCE") or DAILY).strip().lower()
    if mode not in (DAILY, WEEKLY):
        bad.append(("RETRO_SLOT_CADENCE", mode))
        mode = DAILY
    raw = (conf(ctx, "RETRO_SLOT_WEEKDAY") or "sun").strip().lower()
    wd = None
    if raw in WEEKDAYS:
        wd = WEEKDAYS.index(raw)
    elif raw in WEEKDAYS_FULL:
        wd = WEEKDAYS_FULL.index(raw)
    elif raw.isdigit() and 1 <= int(raw) <= 7:   # ISO: 1 = Monday ... 7 = Sunday
        wd = int(raw) - 1
    if wd is None:
        bad.append(("RETRO_SLOT_WEEKDAY", raw))
        wd = 6
    return mode, wd, bad


def make_day(ctx, date):
    """The Day the running cadence renders for `date`."""
    mode, wd, _ = cadence(ctx)
    return Day(date, weekday=wd, mode=mode)


def span_days(ctx):
    """Retro days one message covers: 7 in weekly mode, else 1."""
    return 7 if cadence(ctx)[0] == WEEKLY else 1


def t(ctx, key):
    import i18n_lookup
    lang = ctx.conf("AGENT_LANG") or "en"
    return i18n_lookup.lookup(ctx.root, lang, key)


# ---- clock -----------------------------------------------------------------

def _local(ts):
    return datetime.datetime.utcfromtimestamp(ts + KST)


def _hm(spec, default):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", spec or "")
    if not m:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", default)
    return int(m.group(1)) * 60 + int(m.group(2))


def _minutes(ts):
    lt = _local(ts)
    return lt.hour * 60 + lt.minute


def in_quiet(ctx, ts):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})",
                 conf(ctx, "RETRO_SLOT_QUIET") or "")
    if not m:
        return False
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    cur = _minutes(ts)
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def day_ts(date):
    """Epoch of a retro day's start (05:00 KST)."""
    return calendar.timegm(date.timetuple()) + DAY_BOUNDARY_HOUR * 3600 - KST


def retro_date(ts):
    """The retro day a moment belongs to (05:00 boundary, KST)."""
    return (_local(ts) - datetime.timedelta(hours=DAY_BOUNDARY_HOUR)).date()


def _month_end(d):
    return (d + datetime.timedelta(days=1)).day == 1


class Day(object):
    """One retro message as a section sees it.  daily mode: one day
    (`days` = [date]); weekly mode: the 7 retro days ending on `date`."""

    def __init__(self, date, weekday=6, mode=DAILY):
        self.date = date
        self.mode = mode
        self.weekday = weekday
        self.weekly = date.weekday() == weekday                 # the recap day
        self.week_start = date - datetime.timedelta(days=6)
        if mode == WEEKLY:
            self.days = [self.week_start + datetime.timedelta(days=i) for i in range(7)]
            self.cadences = {WEEKLY}
            ends = [d for d in self.days if _month_end(d)]
            self.month_end = ends[-1] if ends else None
        else:
            self.days = [date]
            self.cadences = {WEEKLY if self.weekly else DAILY}
            self.month_end = date if _month_end(date) else None
        self.monthly = self.month_end is not None
        if self.monthly:
            self.cadences.add(MONTHLY)

    @property
    def iso(self):
        return self.date.isoformat()

    def label(self, ctx, date=None):
        """YYYY-MM-DD(<weekday>): the year always shown."""
        d = date or self.date
        names = [n.strip() for n in (t(ctx, "retro.slot.weekdays") or "").split(",")]
        if len(names) != 7 or not all(names):
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return "%s(%s)" % (d.isoformat(), names[d.weekday()])

    def title(self, ctx, phase="evening"):
        if phase == "morning":
            key = "retro.slot.title.morning"
        else:
            key = "retro.slot.title.weekly" if self.weekly else "retro.slot.title.daily"
        return (t(ctx, key) or "{date}").replace("{date}", self.label(ctx)).replace(
            "{start}", self.label(ctx, self.week_start))


# ---- registry --------------------------------------------------------------

class Section(object):
    def __init__(self, name, order, render, defer=False, cadences=(DAILY,), kit=None,
                 next=None, count=None):
        self.name, self.order, self.render = name, order, render
        self.defer, self.kit, self.next, self.count = bool(defer), kit, next, count
        self.cadences = set(cadences)


class Slot(object):
    def __init__(self):
        self.sections = {}
        self.picture = no_picture
        self.errors = []

    def register(self, name, order, render, defer=False, cadences=(DAILY,), kit=None,
                 next=None, count=None):
        if not name or not isinstance(name, str):
            raise ValueError("section-name")
        if name in self.sections:
            raise ValueError("section-duplicate:%s" % name)
        bad = set(cadences) - set(CADENCES)
        if bad or not cadences:
            raise ValueError("section-cadence")
        if not callable(render) or any(f is not None and not callable(f)
                                       for f in (next, count)):
            raise ValueError("section-render")
        self.sections[name] = Section(name, int(order), render, defer, cadences, kit, next,
                                      count)
        return self.sections[name]

    def set_picture(self, fn):
        self.picture = fn if callable(fn) else no_picture

    def ordered(self):
        return sorted(self.sections.values(), key=lambda s: (s.order, s.name))


def no_picture(day, ctx):
    """The shipped opener-picture hook: no picture (design not confirmed)."""
    return None


def load_kit_sections(slot, root):
    """Import local then bundled sections and call each module's register(slot).
    Local module names win; duplicates and broken modules are noted, not fatal.
    """
    seen = set()
    for directory in (SECTIONS_DIR, BUNDLE_SECTIONS_DIR):
        for path in sorted(glob.glob(os.path.join(root, directory, "*.py"))):
            name = os.path.splitext(os.path.basename(path))[0]
            if name.startswith("_"):
                continue
            if name in seen:
                slot.errors.append("%s:duplicate-module:instance-local-wins" % name)
                continue
            seen.add(name)
            try:
                spec = importlib.util.spec_from_file_location("retro_section_" + name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.register(slot)
            except Exception as e:  # a kit never breaks the slot
                slot.errors.append("%s:%s" % (name, type(e).__name__))
    return slot


def build(ctx, with_kits=True):
    """The slot as it runs: the framework Why section + the kit sections."""
    slot = Slot()
    slot.register(WHY_SECTION, WHY_ORDER, why_render, cadences=CADENCES, kit="framework",
                  count=why_count)
    slot.register(LINK_REVIEW_SECTION, WHY_ORDER + 1, link_review_render,
                  cadences=CADENCES, kit="framework")
    if with_kits:
        load_kit_sections(slot, ctx.root)
    return slot


# ---- the Why section (framework) -----------------------------------------

def why_render(day, ctx):
    """The Why net's section (DGN-2028 AMEND-2 P3.4): a view-only picture --
    confirmed purposes and the agent's unconfirmed assumptions of the span.
    No buttons, no follow-up item.  Empty when the Why net is off (dec-338)
    or the span holds nothing."""
    sys.path.insert(0, _LIB)
    from why_net import retro
    wctx = _why_ctx(ctx)
    if not wctx.enabled():
        return None
    sec = retro.section(wctx)
    if not sec.get("text"):
        return None
    return {"text": sec["text"]}


def link_review_render(day, ctx):
    """One bounded decision menu after the picture, shared by both cadences."""
    from why_net import link_review
    return link_review.menu(link_review.pending(ctx.root), {
        "heading": t(ctx, "retro.link_review.heading"),
        "all": t(ctx, "retro.link_review.all"),
        "fix": t(ctx, "retro.link_review.fix"),
    })


def link_review_open(ctx):
    """Only a delivered, still-fresh review is eligible for an answer."""
    review = load_state(ctx).get("link_review") or {}
    return bool(review.get("rows") and review.get("message_id") is not None
                and ctx.now - review.get("shown_at", 0) <= 30 * 60)


def link_review_answer(ctx, inbound, prompt, sender=None):
    """Resolve all-correct, open a correction menu, or apply its pick."""
    import unicodedata
    from why_net import link_review
    sender = sender or DEFAULT_SENDER
    state = load_state(ctx)
    review = state.get("link_review") or {}
    if not link_review_open(ctx):
        return None
    pending_fix = state.get("link_review_fix")
    target = pending_fix or review
    if (inbound or {}).get("source") == "callback" and (
            inbound.get("message_id") != target.get("message_id") or
            (target.get("chat_id") is not None and
             inbound.get("chat_id") != target.get("chat_id"))):
        return None
    rows = review["rows"]
    said = unicodedata.normalize("NFC", prompt or "").strip()
    if pending_fix:
        row, choices = pending_fix["row"], pending_fix["choices"]
        selected = next((choice for choice in choices if said == choice["label"]), None)
        if said == t(ctx, "retro.link_review.unlink"):
            node = None
        elif selected is not None:
            node = selected
        elif said == t(ctx, "retro.link_review.new"):
            return "new"
        else:
            return None
        if not link_review.update(ctx.root, [row["link"]], node=node,
                                  confirmed_at=ctx.now):
            return None
        state.pop("link_review_fix", None)
        state.pop("link_review", None)
        save_state(ctx, state, _local(ctx.now).date())
        return "fixed"
    if said == t(ctx, "retro.link_review.all"):
        if not link_review.update(ctx.root, [row["link"] for row in rows],
                                  confirmed_at=ctx.now):
            return None
        state.pop("link_review", None)
        save_state(ctx, state, _local(ctx.now).date())
        return "all"
    fixes = {t(ctx, "retro.link_review.fix").replace("{n}", str(i)): row
             for i, row in enumerate(rows, 1)}
    row = fixes.get(said)
    if row is None:
        return None
    choices = [node for node in row.get("candidates", []) if node.get("label")][:3]
    labels = [choice["label"] for choice in choices]
    labels += [t(ctx, "retro.link_review.new"), t(ctx, "retro.link_review.unlink")]
    sent = sender(ctx, "%s -> ?" % row["work"], [_button(label, i + 1)
                                                  for i, label in enumerate(labels)])
    if not sent or sent[0] != 0:
        return None
    state["link_review_fix"] = {"row": row, "choices": choices,
                                "message_id": sent[1], "chat_id": sent[2]}
    save_state(ctx, state, _local(ctx.now).date())
    return "fix"


def _why_ctx(ctx):
    from why_net import budget
    if isinstance(ctx, budget.Ctx):
        return ctx
    return budget.Ctx(root=ctx.root, now=ctx.now, conf_path=getattr(ctx, "conf_path", None))


def why_count(ctx, start, end):
    """Items the Why section had for the retro days start..end: signals the
    agent read in the span plus Why asks queued for the retro in it
    (follow-ups of a reading are not counted again).  None while the Why
    net is off (nothing to measure)."""
    from why_net import ask, budget, store
    wctx = _why_ctx(ctx)
    if not wctx.enabled():
        return None
    lo, hi = day_ts(start), day_ts(end + datetime.timedelta(days=1))

    def inside(t):
        return t is not None and lo <= t < hi

    sigs = {r["reading"] for r in store.read_rows(store.PROBES)
            if r.get("reading") and inside(store.parse_iso(r.get("at")))}
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    queued = [a for a in asks.values() if a.get("kind") == "why"
              and a.get("channel") == "morning" and a.get("trigger") not in ask.RETRO_TRIGGERS
              and inside(a.get("created_at"))]
    return len(sigs) + len(queued)


def why_engagement(ctx, start, end):
    """-> (retro days in start..end on which a Why item was shown, how many
    of those days got an answer: a tap or a typed reply; a stop alone or
    silence is no answer)."""
    from why_net import budget, store
    lo, hi = day_ts(start), day_ts(end + datetime.timedelta(days=1))
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    days = {}
    for r in store.read_rows(store.ASKS):
        if r.get("ev") != "shown" or r.get("via") != "retro":
            continue
        at = store.parse_iso(r.get("at"))
        if at is None or not lo <= at < hi:
            continue
        evs = (asks.get(r["ask"]) or {}).get("events") or []
        d = retro_date(at)
        days[d] = days.get(d, False) or any(e in ("answered", "declined") for e in evs)
    return len(days), sum(1 for v in days.values() if v)


def why_waiting(ctx):
    """The Why walk is not over for today: its last item waits for the
    owner, or the owner stopped (then nothing more is sent today)."""
    from why_net import budget, retro, store
    wctx = _why_ctx(ctx)
    shown_rows, stopped = retro._retro_rows(wctx)
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    return stopped or retro._waiting(wctx, asks, shown_rows)


# ---- composition -----------------------------------------------------------

def _body(r):
    if r is None or r is DEFER:
        return None
    if isinstance(r, dict):
        return r if (r.get("text") or "").strip() else None
    if isinstance(r, str):
        return {"text": r} if r.strip() else None
    return None


def _month_day(day):
    """Weekly mode: monthly parts see the month-end day inside the week."""
    if day.mode == WEEKLY and day.month_end is not None and day.month_end != day.date:
        return Day(day.month_end)
    return day


def _renders(s, day, morning):
    """[(Day, label fn | None)] one section is rendered with.  daily mode:
    the day itself.  weekly mode: a section registered for "weekly" sees the
    week once; a daily-only section is rendered once per day of the week,
    each body under its day label (a deferred part next morning: the
    recap day only); a monthly-only section sees the month-end day."""
    if day.mode != WEEKLY or WEEKLY in s.cadences:
        return [(day, None)]
    if DAILY in s.cadences:
        if morning:
            return [(Day(day.date, day.weekday), None)]
        return [(Day(d, day.weekday), (lambda c, d=d: day.label(c, d))) for d in day.days]
    return [(_month_day(day), None)]


def compose(ctx, slot, day, phase="evening", only=None):
    """-> plan {"day", "phase", "text", "buttons", "sections", "deferred",
    "picture", "interactive"} or a plan with text None (nothing to say).
    Renders the sections for `day`; phase "morning" renders only `only`
    (the names deferred the evening before)."""
    plan = {"day": day.iso, "phase": phase, "text": None, "buttons": None,
            "sections": [], "deferred": [], "picture": None, "interactive": None,
            "why_text": None, "section_bodies": {}, "skipped": []}
    bodies, inter = [], None
    section_bodies = {}
    for s in slot.ordered():
        if only is not None and s.name not in only:
            continue
        if only is None and not (s.cadences & day.cadences):
            if not (day.mode == WEEKLY and DAILY in s.cadences):
                continue
        parts = []
        for one, label in _renders(s, day, only is not None):
            try:
                r = s.render(one, ctx)
            except Exception as e:
                plan["skipped"].append("%s:%s" % (s.name, type(e).__name__))
                continue
            if r is DEFER:
                if not s.defer:
                    continue
                if one.date == day.date and phase == "evening":
                    if s.name not in plan["deferred"]:
                        plan["deferred"].append(s.name)
                else:
                    plan["skipped"].append("%s:defer-past" % s.name)
                continue
            b = _body(r)
            if b is None:
                continue
            if b.get("buttons"):
                if inter is not None:
                    plan["skipped"].append("%s:second-interactive" % s.name)
                    continue
                inter = (s.name, b)
            text = b["text"].strip()
            parts.append(label(ctx) + "\n" + text if label else text)
        if parts:
            joined = "\n\n".join(parts)
            bodies.append(joined)
            section_bodies[s.name] = joined
            plan["sections"].append(s.name)
    plan["why_text"] = section_bodies.get(WHY_SECTION)
    plan["section_bodies"] = section_bodies
    if not bodies:
        return plan
    plan["text"] = day.title(ctx, phase) + "\n\n" + "\n\n".join(bodies)
    if inter is not None:
        plan["interactive"] = inter[0]
        plan["buttons"] = inter[1]["buttons"]
        plan["_on_sent"] = inter[1].get("on_sent")
    return plan


# ---- state -----------------------------------------------------------------

def state_path(ctx):
    d = getattr(ctx, "bot_dir", None) or os.path.join(ctx.root, ".telegram_bot")
    return os.path.join(d, STATE_FILE)


def load_state(ctx):
    try:
        with open(state_path(ctx), encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict) and isinstance(st.get("days"), dict):
            return st
    except (OSError, ValueError):
        pass
    return {"days": {}}


def save_state(ctx, st, today):
    keep = (today - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    st["days"] = {k: v for k, v in st["days"].items() if k >= keep}
    p = state_path(ctx)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".retro-slot.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _retro_done(ctx, day, rec):
    """Whether the evening retro for ``day`` received an answer."""
    if rec.get("done") or rec.get("answered") or rec.get("completed"):
        return True
    try:
        return why_engagement(ctx, day, day)[1] > 0
    except Exception:
        return False


def morning_due(ctx, today):
    """Return yesterday's brief catch-up payload, read-only, or ``None``."""
    if not slot_on(ctx):
        return None
    day = today - datetime.timedelta(days=1)
    rec = (load_state(ctx).get("days") or {}).get(day.isoformat()) or {}
    if rec.get("evening") != "sent" or rec.get("morning_consumed") \
            or _retro_done(ctx, day, rec):
        return None
    return t(ctx, "retro.slot.missed.morning"), t(ctx, "retro.slot.missed.start")


def morning_consume(ctx, today):
    """Idempotently record that today's brief took the missed-retro nudge."""
    day = today - datetime.timedelta(days=1)
    st = load_state(ctx)
    rec = st["days"].get(day.isoformat())
    if rec is not None and not rec.get("morning_consumed"):
        rec["morning_consumed"] = today.isoformat()
        save_state(ctx, st, today)


def missed_answer(ctx, prompt, sender=None, out=None):
    """Resume yesterday's interactive walk, or reopen its view-only retro."""
    if not slot_on(ctx) or in_quiet(ctx, ctx.now):
        return False
    if (prompt or "").strip() != (t(ctx, "retro.slot.missed.start") or "").strip():
        return False
    today = _local(ctx.now).date()
    day = today - datetime.timedelta(days=1)
    st = load_state(ctx)
    rec = st["days"].get(day.isoformat()) or {}
    if rec.get("morning_consumed") != today.isoformat() \
            or rec.get("morning_started") or _retro_done(ctx, day, rec):
        return False
    if rec.get("interactive"):
        if next_item(ctx, sender=sender, out=out, day=day) is None:
            return False
    else:
        if rec.get("evening") != "sent":
            return False
        mode, wd, _ = cadence(ctx)
        plan = compose(ctx, build(ctx), Day(day, wd, mode), phase="morning")
        if plan["text"] is None or not _send(ctx, plan, sender or push_sh)[0]:
            return False
    # The next path can save a held cadence proposal; retain that write.
    st = load_state(ctx)
    st["days"][day.isoformat()]["morning_started"] = True
    save_state(ctx, st, today)
    return True


# ---- sending ---------------------------------------------------------------

class PushSender(object):
    """The installed text transport."""
    def _run(self, ctx, *args):
        cmd = [os.path.join(ctx.root, "routines", "push.sh")] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return 1, None, None
        mid = chat = None
        for line in reversed((p.stdout or "").strip().splitlines()):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("ok"):
                mid, chat = row.get("message_id"), row.get("chat_id")
                break
        return p.returncode, mid, chat

    def __call__(self, ctx, text, buttons):
        args = ["--strict", "--text", text]
        if buttons:
            args += ["--buttons-json", json.dumps(buttons, ensure_ascii=False)]
        return self._run(ctx, *args)



DEFAULT_SENDER = PushSender()


def push_sh(ctx, text, buttons):
    """Default sender: push.sh --strict (with the keyboard when there is
    one).  -> (rc, message_id | None, chat_id | None)."""
    return DEFAULT_SENDER(ctx, text, buttons)


def _send(ctx, plan, sender):
    """-> (ok, message_id, chat_id)."""
    res = tuple(sender(ctx, plan["text"], plan["buttons"]))
    rc, mid = res[0], res[1] if len(res) > 1 else None
    chat = res[2] if len(res) > 2 else None
    if rc != 0:
        return False, None, None
    cb = plan.get("_on_sent")
    if cb is not None and mid is not None:
        try:
            cb(ctx, mid, chat, plan["text"])
        except Exception:
            pass  # the message went out; an unbound tap is answered as text
    return True, mid, chat


def _signal(name, *args, **kv):
    import machine_signal
    return machine_signal.signal(name, *args, **kv)


def tick(ctx, sender=None, slot=None, out=None):
    """One scheduler beat.  Sends the evening message once per retro day
    (weekly mode: once, on the weekly day) at/after RETRO_SLOT_TIME, and the
    deferred morning message once at/after RETRO_SLOT_MORNING_TIME the next
    day.  -> list of what happened."""
    out = out if out is not None else sys.stdout
    if not slot_on(ctx):
        out.write(_signal("RETRO_SLOT", "off") + "\n")
        return []
    mode, wd, bad = cadence(ctx)
    for key, val in bad:   # runs as the default; said every beat, never silent
        out.write(_signal("RETRO_SLOT_CONF_UNKNOWN", key=key, value=val or "-",
                          fallback=DAILY if key == "RETRO_SLOT_CADENCE" else "sun") + "\n")
    sender = sender or DEFAULT_SENDER
    now = ctx.now
    if in_quiet(ctx, now):
        return []
    slot = slot or build(ctx)
    st = load_state(ctx)
    did = []
    lt = _local(now)
    cur = lt.hour * 60 + lt.minute
    today = lt.date()
    dirty = False
    if (st.get("cadence") or {}).get("mode") != mode:
        # the proposal measures a full window under the running cadence
        st["cadence"] = {"mode": mode, "since": today.isoformat()}
        dirty = True
    # morning: yesterday's deferred sections
    yday = (today - datetime.timedelta(days=1)).isoformat()
    yrec = st["days"].get(yday) or {}
    if yrec.get("deferred") and not yrec.get("morning") \
            and cur >= _hm(conf(ctx, "RETRO_SLOT_MORNING_TIME"), "08:00"):
        plan = compose(ctx, slot, Day(today - datetime.timedelta(days=1), wd, mode),
                       "morning", only=list(yrec["deferred"]))
        did.append(_deliver(ctx, plan, sender, yrec, "morning", out))
        st["days"][yday] = yrec
    # evening: today's message (weekly mode: the weekly day only)
    rec = st["days"].get(today.isoformat()) or {}
    done = rec.get("evening") in ("sent", "empty")
    if (mode == DAILY or today.weekday() == wd) and not done \
            and rec.get("tries", 0) < SEND_TRIES \
            and cur >= _hm(conf(ctx, "RETRO_SLOT_TIME"), "21:00") \
            and retro_date(now) == today:
        day = Day(today, wd, mode)
        plan = compose(ctx, slot, day, "evening")
        rec["deferred"] = plan["deferred"]
        prop = proposal_due(ctx, slot, st, today, mode)
        if prop is not None:
            plan = _with_proposal(ctx, plan, prop, day, rec)
        res = _deliver(ctx, plan, sender, rec, "evening", out)
        did.append(res)
        if res["sent"] and plan.get("proposal"):
            _proposal_shown(ctx, st, plan["proposal"], res, today)
            out.write(_signal("RETRO_SLOT", "proposal", to=plan["proposal"]["to"],
                              reason=plan["proposal"]["reason"]) + "\n")
        st["days"][today.isoformat()] = rec
    if did or dirty:
        save_state(ctx, st, today)
    return did


def _deliver(ctx, plan, sender, rec, phase, out):
    if plan["text"] is None:
        rec[phase] = "empty"
        out.write(_signal("RETRO_SLOT", phase, day=plan["day"], sent="none") + "\n")
        return {"phase": phase, "sent": False, "plan": plan}
    ok, mid, chat = _send(ctx, plan, sender)
    if not ok:
        rec["tries"] = rec.get("tries", 0) + 1
        out.write(_signal("RETRO_SLOT_PUSH_FAILED", phase, day=plan["day"]) + "\n")
        return {"phase": phase, "sent": False, "plan": plan, "failed": True}
    rec[phase] = "sent"
    if phase == "evening":
        rec["message_id"] = mid
        rec["interactive"] = plan["interactive"]
    out.write(_signal("RETRO_SLOT", phase, day=plan["day"],
                      sections=",".join(plan["sections"])) + "\n")
    return {"phase": phase, "sent": True, "plan": plan, "message_id": mid, "chat_id": chat}


def next_item(ctx, sender=None, slot=None, out=None, day=None):
    """The turn after the owner answered an interactive item: the section's
    next item as its own message (one per turn; the section decides when it
    has nothing more).  Only on a retro day whose evening message went out.
    When the walk is over, a cadence proposal held back for it goes out as
    the last item.  The morning brief may explicitly resume yesterday's day."""
    out = out if out is not None else sys.stdout
    if not slot_on(ctx) or in_quiet(ctx, ctx.now):
        return None
    st = load_state(ctx)
    rday = day if day is not None else retro_date(ctx.now)
    rec = st["days"].get(rday.isoformat()) or {}
    name = rec.get("interactive")
    if rec.get("evening") != "sent" or not name:
        return None
    slot = slot or build(ctx)
    s = slot.sections.get(name)
    b = None
    if s is not None and s.next is not None:
        try:
            b = _body(s.next(ctx))
        except Exception:
            return None
    if b is None:
        if name == WHY_SECTION and why_waiting(ctx):
            return None
        return _held_proposal(ctx, st, rec, rday, sender or push_sh, out)
    plan = {"day": rday.isoformat(), "text": b["text"],
            "buttons": b.get("buttons"), "_on_sent": b.get("on_sent")}
    ok, _, _ = _send(ctx, plan, sender or push_sh)
    out.write(_signal("RETRO_SLOT", "next", section=name, sent="yes" if ok else "failed")
              + "\n")
    return plan if ok else None


# ---- the cadence proposal (DGN-1961, owner 2026-10-11) ---------------------
#
# Measured over the trailing RETRO_SLOT_PROPOSE_WINDOW_DAYS retro days:
#   items/day   the items that would enter the retro (sections' `count`;
#               the Why section counts readings + queued Why asks)
#   answered    retro days with a shown item that got an answer
# daily  -> weekly  items/day < RETRO_SLOT_PROPOSE_LOW_ITEMS, or (at least
#                   RETRO_SLOT_PROPOSE_MIN_DAYS retro days with an item and)
#                   answered days / those days < RETRO_SLOT_PROPOSE_LOW_ANSWER
# weekly -> daily   items/day >= RETRO_SLOT_PROPOSE_HIGH_ITEMS
# Never before a full window ran under the current cadence, never twice
# inside one window (cooldown from the day the last proposal was shown,
# answered or not).  The low-activity rule needs every section to be
# countable (a kit section without `count` would under-count: no claim is
# made then; the answer-rate rule still applies).

def usage(ctx, slot, today):
    win = max(1, int(_num(ctx, "RETRO_SLOT_PROPOSE_WINDOW_DAYS")))
    start = today - datetime.timedelta(days=win - 1)
    items, complete = 0, True
    for s in slot.ordered():
        n = None
        if s.count is not None:
            try:
                n = s.count(ctx, start, today)
            except Exception:
                n = None
        if n is None:
            if s.kit != "framework":
                complete = False
            continue
        items += max(0, int(n))
    shown, answered = why_engagement(ctx, start, today)
    return {"window": win, "items": items, "per_day": items / float(win),
            "complete": complete, "retro_days": shown, "answered_days": answered}


def _iso_date(v):
    try:
        return datetime.date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def proposal_due(ctx, slot, st, today, mode):
    """-> {"to", "from", "reason", "usage"} when the slot should propose the
    other cadence tonight, else None.  Reads only; changes nothing."""
    if (conf(ctx, "RETRO_SLOT_PROPOSE") or "on").strip().lower() != "on":
        return None
    win = max(1, int(_num(ctx, "RETRO_SLOT_PROPOSE_WINDOW_DAYS")))
    since = _iso_date((st.get("cadence") or {}).get("since"))
    if since is None or (today - since).days < win:
        return None
    last = _iso_date((st.get("proposal") or {}).get("shown"))
    if last is not None and (today - last).days < win:
        return None
    u = usage(ctx, slot, today)
    to = reason = None
    if mode == DAILY:
        if u["complete"] and u["per_day"] < _num(ctx, "RETRO_SLOT_PROPOSE_LOW_ITEMS"):
            to, reason = WEEKLY, "low"
        elif u["retro_days"] >= _num(ctx, "RETRO_SLOT_PROPOSE_MIN_DAYS") and \
                u["answered_days"] < _num(ctx, "RETRO_SLOT_PROPOSE_LOW_ANSWER") * u["retro_days"]:
            to, reason = WEEKLY, "skip"
    elif u["per_day"] >= _num(ctx, "RETRO_SLOT_PROPOSE_HIGH_ITEMS"):
        to, reason = DAILY, "high"
    if to is None:
        return None
    return {"to": to, "from": mode, "reason": reason, "usage": u}


def _weekday_name(ctx):
    names = [n.strip() for n in (t(ctx, "retro.slot.weekdays") or "").split(",")]
    if len(names) != 7 or not all(names):
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return names[cadence(ctx)[1]]


def _button(label, i):
    cb = "opt:%s" % label
    if len(cb.encode("utf-8")) > 64:
        cb = "opt:%d" % i
    return [{"text": label, "callback_data": cb}]


def proposal_body(ctx, prop):
    """-> (line, buttons, {"yes": label, "keep": label})."""
    u = prop["usage"]
    key = {"low": "retro.slot.cadence.weekly.low", "skip": "retro.slot.cadence.weekly.skip",
           "high": "retro.slot.cadence.daily.high"}[prop["reason"]]
    line = (t(ctx, key) or "").replace("{days}", str(u["window"])).replace(
        "{n}", str(u["items"])).replace("{total}", str(u["retro_days"])).replace(
        "{skipped}", str(u["retro_days"] - u["answered_days"])).replace(
        "{weekday}", _weekday_name(ctx))
    yes = t(ctx, "retro.slot.cadence.btn." + prop["to"])
    keep = t(ctx, "retro.slot.cadence.btn.keep")
    return line, [_button(yes, 1), _button(keep, 2)], {"yes": yes, "keep": keep}


def _with_proposal(ctx, plan, prop, day, rec):
    """Puts the proposal on tonight's message: alone (title + line) when
    there is nothing else, as the closing part when the message has no
    interactive item; else it is held for the end of the walk."""
    if plan["text"] is not None and plan["interactive"] is not None:
        rec["proposal"] = dict(prop, held=True)
        return plan
    line, buttons, labels = proposal_body(ctx, prop)
    if plan["text"] is None:
        plan["text"] = day.title(ctx) + "\n\n" + line
    else:
        plan["text"] += "\n\n" + line
    plan["buttons"] = buttons
    plan["interactive"] = CADENCE_SECTION
    plan["sections"].append(CADENCE_SECTION)
    plan["proposal"] = dict(prop, labels=labels)
    return plan


def _proposal_shown(ctx, st, prop, res, today):
    st["proposal"] = {
        "to": prop["to"], "from": prop["from"], "reason": prop["reason"],
        "usage": prop["usage"], "labels": prop["labels"], "shown": today.isoformat(),
        "shown_at": ctx.now, "message_id": res.get("message_id"),
        "chat_id": res.get("chat_id"), "answer": None}


def _held_proposal(ctx, st, rec, rday, sender, out):
    prop = rec.get("proposal")
    if not prop or not prop.get("held"):
        return None
    last = _iso_date((st.get("proposal") or {}).get("shown"))
    if last == rday:
        return None
    line, buttons, labels = proposal_body(ctx, prop)
    plan = {"day": rday.isoformat(), "text": line, "buttons": buttons, "_on_sent": None}
    ok, mid, chat = _send(ctx, plan, sender)
    out.write(_signal("RETRO_SLOT", "proposal", to=prop["to"], reason=prop["reason"],
                      sent="yes" if ok else "failed") + "\n")
    if not ok:
        return None
    prop["held"] = False
    _proposal_shown(ctx, st, dict(prop, labels=labels),
                    {"message_id": mid, "chat_id": chat}, rday)
    save_state(ctx, st, _local(ctx.now).date())
    return plan


def proposal_answer(ctx, inbound, prompt):
    """The owner-turn hook (why_net.ups) hands every verified owner turn
    here.  A tap on the open proposal's message, or its button label typed
    exactly, answers it: the "yes" button writes RETRO_SLOT_CADENCE to the
    conf, "keep" changes nothing.  -> "yes" | "keep" | None (not an answer).
    The only place the slot ever edits the conf."""
    import unicodedata
    st = load_state(ctx)
    p = st.get("proposal") or {}
    labels = p.get("labels") or {}
    if not labels or p.get("answer") or p.get("message_id") is None:
        return None
    if ctx.now - (p.get("shown_at") or 0) > PROPOSAL_TTL:
        return None
    if (inbound or {}).get("source") == "callback":
        if inbound.get("message_id") != p["message_id"] or (
                p.get("chat_id") is not None and inbound.get("chat_id") != p["chat_id"]):
            return None
    said = unicodedata.normalize("NFC", prompt or "").strip()
    choice = next((k for k in ("yes", "keep")
                   if unicodedata.normalize("NFC", labels.get(k) or "").strip() == said), None)
    if choice is None:
        return None
    if choice == "yes" and p.get("to") in (DAILY, WEEKLY):
        set_conf(ctx, "RETRO_SLOT_CADENCE", p["to"])
    p["answer"], p["answered_at"] = choice, ctx.now
    st["proposal"] = p
    save_state(ctx, st, _local(ctx.now).date())
    return choice


def proposal_open(ctx):
    """Cheap check for the hook: a proposal is out and unanswered."""
    p = load_state(ctx).get("proposal") or {}
    return bool(p.get("labels")) and not p.get("answer") \
        and ctx.now - (p.get("shown_at") or 0) <= PROPOSAL_TTL



def set_conf(ctx, key, value):
    """Replace the active KEY= line of the instance conf (or append one)."""
    path = getattr(ctx, "conf_path", None) or os.path.join(ctx.root, "config", "agent.conf")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            if not done:
                out.append("%s=%s" % (key, value))
                done = True
            continue
        out.append(line)
    if not done:
        out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)


def main(argv=None):
    """Read/consume CLI used by kit morning briefs."""
    import argparse
    from why_net import budget
    p = argparse.ArgumentParser(prog="retro_slot.py")
    sub = p.add_subparsers(dest="verb")
    for verb in ("morning-due", "morning-consume"):
        q = sub.add_parser(verb)
        q.add_argument("--root", required=True)
        q.add_argument("--today", required=True)
    try:
        a = p.parse_args(sys.argv[1:] if argv is None else argv)
        if not a.verb:
            raise ValueError("verb")
        today = datetime.datetime.strptime(a.today, "%Y-%m-%d").date()
    except (SystemExit, ValueError):
        return 2
    root = os.path.abspath(a.root)
    # The CLI's explicit root owns both slot and Why state, even when called
    # by another instance. Restore the caller's override for in-process use.
    previous = os.environ.get("DOGANY_BOT_DATA_DIR")
    os.environ["DOGANY_BOT_DATA_DIR"] = os.path.join(root, ".telegram_bot")
    try:
        ctx = budget.Ctx(root=root, conf_path=os.path.join(root, "config", "agent.conf"))
        if a.verb == "morning-consume":
            morning_consume(ctx, today)
            return 0
        due = morning_due(ctx, today)
        if due is None:
            print("NO")
        else:
            print("DUE")
            print("LINE\t" + due[0])
            print("BUTTON\t" + due[1])
        return 0
    finally:
        if previous is None:
            os.environ.pop("DOGANY_BOT_DATA_DIR", None)
        else:
            os.environ["DOGANY_BOT_DATA_DIR"] = previous


if __name__ == "__main__":
    sys.exit(main())
