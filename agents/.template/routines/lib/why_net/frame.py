"""why_net.frame -- the Why asked inside the working turn (AMEND-2,
docs/design/v27-t1-why-net-AMEND-2.md; DGN-2028 slice 3).

Inside the working turn the agent calls one verb, `why-net frame`, and gets
one of three outcomes (2.4, the decision table):
  pick     the owner picks among the live linked hypotheses;
  infer    the agent shows 2-3 Whys it inferred, plus "both" (exactly two),
           "another reason" and skip / proceed;
  proceed  the agent carries on without asking, optionally recording an
           assumption (unconfirmed frame evidence, never a Why node).

`frame` writes ONE row in ONE file (2.5): the asks `create` row (+ `refused`
on a budget refusal; never `queued`) for pick / infer, a frames.jsonl
`proceed` row for proceed.  It writes no Why node and no events row.  The
frame is keyed by the turn-ledger otl of the current owner turn (R5), so a
retry replays the same row and prints the same bytes; the budget is never
re-checked on a replay (FR10b).  One escalation proceed -> pick / infer per
otl is allowed (F22).

A Why node appears only when the owner answers (D1): `ups` births the tapped
candidate(s) (U3), and `frame --answer` records a free-text answer as path
(a) owner words, (b) the agent's rewording or (c) nothing (2.7).

Locks (2.6, F3): F0 unlocked (parse, target, metacog snapshot) -> F1 one
bounded `store.locked()` (FRAME_LOCK_WAIT, every write *_unlocked, no
transcript read) -> F2 the turn-frame `why` transition after release (its
own flock; every replay re-runs it) -> F3 print.  --answer: A0 resolves the
quote before the lock, A1 one bounded lock.

Also here: `lookup` (2.8, read-only), `metacog-reset` (2.11, owner-gated),
the owner erase of frame text (F25) and the current-frame read `gate` uses
(2.10).  Pure stdlib.  English/ASCII only.
"""

import json
import os
import sys
import unicodedata

from . import WhyRefusal, ask, budget, events, store
from . import nodes as N

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import machine_signal  # noqa: E402
from why_net import LazyModule  # noqa: E402
why_owner_turn = LazyModule("why_owner_turn")

EXIT_OK, EXIT_INTERNAL, EXIT_USAGE, EXIT_FLOOR, EXIT_BUDGET = 0, 1, 2, 3, 4

# 2.6: every frame-side acquisition is bounded (ups waits 1.5 s and skips
# the turn past it, so a frame section must never block forever).
FRAME_LOCK_WAIT = 2.0
MODES = ("pick", "infer", "proceed")
STAKES = ("high", "normal")
MAX_CANDS = 3
# S-P2a: owner turns of the session ledgered this close together count as
# one reply's tail (a mid-turn N+1); recorded on the create row when >= 2.
LEDGER_TAIL_WINDOW = 120
LOOKUP_WHYS, LOOKUP_ASKS = 50, 20
OPEN_STATES = ("create", "shown", "awaiting")


def classify_work_effect(unit, evidence_resolver):
    """D1: classify intended effects from trusted, cited scope facts.

    Surface nouns, ticket prefixes, urgency and model-selected modes are
    irrelevant. Accepted restoration/continuation requires original scope
    evidence; absent authority is a hold, never a new Why question.
    """
    effect = evidence_resolver.work_effect(unit)
    if effect in ("record", "errand", "question", "bugfix", "confirmed-continuation",
                  "new-work", "hold-authority", "hold-provenance"):
        return effect
    return "unknown"


def judge_work(frame, evidence_resolver, node_resolver=None):
    """FDGN-594 D1-D6: single structured policy entry for new executors.

    This verdict is independent of legacy optional exposure budgets, queues,
    WHY_NET and frame --mode. A tree fit and work approval stay separate.
    The trusted runtime owns the frame/proof/record binding, not model JSON.
    """
    from . import intake, tree
    out = {"intake": "technical-refusal", "tree": {"state": "none"},
           "execution": "read/intake-only", "report_scope": "own-agent"}
    if evidence_resolver is None:
        return dict(out, reason="proof-unavailable")
    try:
        if not evidence_resolver.current_binding(frame):
            return dict(out, reason="runtime-unbound")
        unit = frame["unit"]
        out.update(work_key=unit["work_key"], scope_revision=unit.get("scope_revision"))
        effect = classify_work_effect(unit, evidence_resolver)
        if effect in ("record", "errand", "question", "bugfix", "confirmed-continuation"):
            if frame.get("nodes") is not None and unit.get("candidate_fits"):
                matched = tree.match_purpose(
                    frame["nodes"], unit.get("candidate_fits", ()),
                    frame.get("binding", {}).get("owner_id"), frame.get("as_of"),
                    unit.get("purpose_scope"), evidence_resolver)
                if matched.get("state") == "linked":
                    node = frame["nodes"][matched["node"]]
                    evidence_resolver.record_agent_link(
                        unit, node, list(frame["nodes"].values()), frame.get("as_of"))
                out["tree"] = matched
            return dict(out, intake="exempt(" + effect + ")", execution="existing-scope")
        if effect != "new-work":
            return dict(out, reason=effect)
        context = dict(frame.get("binding", {}), **{
            k: unit.get(k) for k in ("work_key", "scope_revision", "scope_digest", "targets")})
        record = evidence_resolver.read_work_record(unit)
        checked = intake.advance_intake(record, context, evidence_resolver, node_resolver)
        out.update(intake=checked["intake"], slot_validation=checked,
                   record_uri=record.get("record_uri"), record_revision=record.get("revision"))
        if frame.get("nodes") is not None:
            out["tree"] = tree.match_purpose(
                frame["nodes"], unit.get("candidate_fits", ()), context.get("owner_id"),
                frame.get("as_of"), unit.get("purpose_scope"), evidence_resolver)
            if out["tree"].get("state") == "linked":
                node = frame["nodes"][out["tree"]["node"]]
                candidates = [frame["nodes"][wid] for wid in sorted(frame["nodes"])
                              if frame["nodes"][wid].get("status") not in
                              ("pivoted", "retired")]
                evidence_resolver.record_agent_link(unit, node, candidates,
                                                     frame.get("as_of"))
        # D3/D4: all four slots are mandatory, including scoped go-ahead.
        # D5: unresolved leaf categorization alone cannot veto valid work.
        if checked["state"] in ("confirmed", "go-ahead"):
            out["execution"] = "build-allowed"
        return out
    except Exception:  # missing/crashed/unavailable adapters fail closed
        return dict(out, intake="technical-refusal", execution="read/intake-only",
                    reason="proof-unavailable")


class _Out(object):
    """The bytes one frame call prints: stdout lines, stderr lines, exit."""

    def __init__(self, rc=EXIT_OK):
        self.rc, self.stdout, self.stderr = rc, [], []
        self.transition = None


def _sig(token, *parts, **kw):
    return machine_signal.signal(token, *parts, **kw)


def _refusal(reason, rc=None, **kw):
    o = _Out(rc if rc is not None else (EXIT_USAGE if reason in ("no-topic",)
                                        else EXIT_FLOOR))
    o.stdout.append(_sig("WHY_REFUSED", reason=reason, **kw))
    return o


# ---- target (2.2) ---------------------------------------------------------

def resolve_target(root, target, scope=None):
    """`<pack>/<target>` validated against the installed contracts
    (base_contract.load_installed).  -> (target, scope): with no scope the
    target's why_scope (else `framework`); a disagreeing scope refuses
    `scope-target-mismatch`.  `base/<id>` is reserved (no base registry)."""
    if not isinstance(target, str) or not N.TARGET_RE.match(target):
        raise WhyRefusal("unknown-target")
    pack, tid = target.split("/", 1)
    if pack == "base":
        raise WhyRefusal("unknown-target")
    import base_contract
    for pid, c in base_contract.load_installed(root):
        if pid != pack:
            continue
        for t in c.get("targets") or []:
            if t.get("id") == tid:
                ws = t.get("why_scope")
                if scope and ws and scope != ws:
                    raise WhyRefusal("scope-target-mismatch")
                return target, scope or ws or "framework"
    raise WhyRefusal("unknown-target")


# ---- frame identity (2.5) ---------------------------------------------------

# G4: the frame reads only the ledger's tail (the newest owner turns); a
# frame binds the current turn, and a retry names a recent one.
LEDGER_TAIL_BYTES = 256 * 1024


def session_ledger(env=None):
    """(session id, its owner-turn rows in the ledger tail): the session of
    the newest owner-turn row, vetoed by an env naming another one -- the
    rule of why_owner_turn.current_session, over a bounded tail read."""
    env = os.environ if env is None else env
    rows = [r for r in store.read_tail_rows(store.TURN_LEDGER, LEDGER_TAIL_BYTES)
            if not r.get("skip")]
    sid = rows[-1].get("session_id") if rows else None
    named = env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID")
    if named and named != sid:
        sid = None
    return sid, ([r for r in rows if r.get("session_id") == sid] if sid else [])


def turn_frame_row(ctx, sid, request_id):
    """The TURN_FRAME issued row of this owner turn (same bridge request
    id), or None when TURN_FRAME is off or the hook recorded none."""
    if not request_id or not sid or not turn_frame_on(ctx):
        return None
    try:
        import turn_frame as tfl
        rows = tfl.read_rows(tfl.store_path(ctx.root, sid))
    except Exception:  # noqa: BLE001 -- the cross-reference is optional
        return None
    for r in reversed(rows):
        if r.get("ev") == "issued" and r.get("frame_id") and r.get("request_id") == request_id:
            return r
    return None


def turn_frame_on(ctx):
    """TURN_FRAME as turn_frame.frame_enabled reads it (env DOGANY_TURN_FRAME,
    then the conf), without importing the hook lane's module."""
    raw = os.environ.get("DOGANY_TURN_FRAME")
    if raw is None or raw == "":
        from conf_reader import conf_get
        raw = conf_get("TURN_FRAME", ctx.conf_path) or ""
    return raw.strip().lower() in ("1", "on", "true", "yes")


def frames_rows():
    return store.read_rows(store.FRAMES)


def turn_asks(asks, otl=None):
    """Turn asks (of one frame otl), oldest first."""
    out = [a for a in asks.values() if a.get("trigger") == "turn"
           and (otl is None or a.get("frame_otl") == otl)]
    return sorted(out, key=lambda a: a["ask"])


def proceed_rows(otl, rows=None):
    return [r for r in (frames_rows() if rows is None else rows)
            if r.get("ev") == "proceed" and r.get("otl") == otl]


def has_record(otl, asks, rows):
    return bool(turn_asks(asks, otl)) or bool(proceed_rows(otl, rows))


def current_frame(ctx):
    """The current frame as `gate` reads it: {"otl", "asks" (turn asks of
    this otl), "proceed" (its proceed rows), "stakes", "tf"}; otl None when
    the session has no owner turn ledgered."""
    sid, rows = session_ledger()
    out = {"otl": None, "asks": [], "proceed": [], "stakes": "normal", "tf": None,
           "session": sid}
    if not rows:
        return out
    row = rows[-1]
    out["otl"] = row["id"]
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    out["asks"] = turn_asks(asks, row["id"])
    out["proceed"] = proceed_rows(row["id"])
    tfr = turn_frame_row(ctx, sid, row.get("request_id"))
    if tfr is not None:
        out["tf"] = tfr["frame_id"]
        out["stakes"] = "high" if tfr.get("stakes") == "high" else "normal"
    else:
        for r in out["asks"] + out["proceed"]:
            if r.get("stakes") == "high":
                out["stakes"] = "high"
    return out


def ledger_tail(rows, now):
    """S-P2a (cosmetic): how many owner turns of the session arrived inside
    LEDGER_TAIL_WINDOW before now -- a frame bound to a mid-turn N+1 shows
    `ledger_tail: 2` on the console."""
    n = 0
    for r in reversed(rows):
        at = store.parse_iso(r.get("at"))
        if at is None or now - at > LEDGER_TAIL_WINDOW:
            break
        n += 1
    return n


# ---- metacog snapshot (MD 8) ------------------------------------------------

def metacog_band(target):
    """The derived band of a target; missing / corrupt -> unknown (MC6)."""
    from . import metacog
    return metacog.band(target)


# ---- the frame verb -----------------------------------------------------------

def _linked(ctx, work, target, known):
    """Live Whys linked to the frame: the work item's (work_whys) and the
    target's (node `target`)."""
    out = list(ask.work_whys(ctx, work, known)) if work else []
    for wid in sorted(known):
        n = known[wid]
        if target and n.get("target") == target and n["status"] not in N.TERMINAL \
                and wid not in out:
            out.append(wid)
    return out


def _order(members, known, band):
    """Metacog orders candidates, never requires or confirms one (MD 8): a
    settled target leads with its best-supported hypothesis; otherwise the
    stable id order."""
    if band == "settled":
        return sorted(members, key=lambda w: (-(known[w].get("words_support")
                                               or known[w].get("support") or 0.0), w))
    return sorted(members)


def _reoffer(cands, known, err_lines):
    """F14 re-offer: a candidate whose wording is a live hypothesis is shown
    as that node; one equal to a provisional / confirmed node is dropped
    (N7, `cand-dropped=already-held`); one equal to a retired node stays a
    text slot.  Returns the candidate list (ids or texts), deduplicated."""
    # text_key equality is NFC + strip equality; compare that directly
    # (no hash per node -- G4)
    norm = {}
    for c in cands:
        if not c.startswith("why-"):
            norm.setdefault(unicodedata.normalize("NFC", c).strip(), [])
    for wid in sorted(known):
        k = unicodedata.normalize("NFC", known[wid]["text"] or "").strip()
        if k in norm:
            norm[k].append(known[wid])
    out = []
    for c in cands:
        if c.startswith("why-"):
            n = known.get(c)
            if n is None or n["status"] in N.TERMINAL:
                raise WhyRefusal("unknown-why" if n is None else "terminal-status")
            if n["status"] in N.LIVE_CONFIRMED:
                err_lines.append(_sig("WHY_FRAME", cand_dropped="already-held", why=c))
                continue
            entry = c
        else:
            hits = norm.get(unicodedata.normalize("NFC", c).strip()) or []
            hyp = [n for n in hits if n["status"] == "hypothesis"]
            held = [n for n in hits if n["status"] in N.LIVE_CONFIRMED + ("contested",)]
            if hyp:
                entry = hyp[0]["id"]
            elif held:
                err_lines.append(_sig("WHY_FRAME", cand_dropped="already-held",
                                      why=held[0]["id"]))
                continue
            else:
                entry = c
        if entry not in out:
            out.append(entry)
    return out


def _frame_signal(row, err=None):
    """The stderr line of an issued / refused turn ask (rebuilt from the
    stored row, so a replay prints the same bytes)."""
    kw = {"otl": row.get("frame_otl"), "ask": row["ask"]}
    if row.get("refuse_reason") or "refused" in (row.get("events") or []):
        kw.update(mode="proceed", asked=row.get("mode"),
                  reason=row.get("refuse_reason") or "refused")
    else:
        kw["mode"] = row.get("mode")
        if row.get("asked") and row.get("asked") != row.get("mode"):
            kw["asked"] = row["asked"]
    if row.get("escalated_from"):
        kw["escalated_from"] = row["escalated_from"]
    return _sig("WHY_FRAME", **kw)


def _proceed_signal(r):
    kw = {"otl": r.get("otl"), "mode": "proceed"}
    if r.get("asked"):
        kw["asked"] = r["asked"]
    if r.get("reason"):
        kw["reason"] = r["reason"]
    if r.get("refuse_reason"):
        kw["refused"] = r["refuse_reason"]
    return _sig("WHY_FRAME", **kw)


def _replay_ask(ctx, a, differs):
    """Reprint a stored turn ask: its rendered text (or its refusal), plus
    its signal; nothing is written and the budget is not re-checked."""
    o = _Out()
    refused = "refused" in a["events"]
    if refused:
        o.rc = EXIT_BUDGET
        o.stdout.append(ask._signal(a.get("refuse_reason") or "refused",
                                    topic=a.get("topic"), ask=a["ask"]))
    else:
        o.stdout.append(a.get("rendered") or "")
    o.stderr.append(_frame_signal(a))
    if differs:
        o.stderr.append(_sig("WHY_FRAME_REPLAYED", ask=a["ask"], differs=1))
    o.transition = {"otl": a.get("frame_otl"), "tf": a.get("tf"), "mode": a.get("mode"),
                    "ask": a["ask"], "disposition": "proceed" if refused else "asked"}
    return o


def _replay_proceed(r, differs):
    o = _Out()
    o.stderr.append(_proceed_signal(r))
    if differs:
        o.stderr.append(_sig("WHY_FRAME_REPLAYED", otl=r.get("otl"), differs=1))
    o.transition = {"otl": r.get("otl"), "tf": r.get("tf"), "mode": "proceed",
                    "ask": None, "disposition": "proceed"}
    return o


def _never_ask(ctx):
    """L 4.2 rules 1-3 that make a frame `proceed` (urgent has no flag on
    the frame verb): a pending dispatch return, a rush under enforce."""
    if os.path.exists(os.path.join(ctx.bot_dir, "dispatch-return-context.json")):
        return "dispatch-return-pending"
    if budget.is_rush(budget.owner_turns(ctx)) \
            and ctx.conf("WHY_RUSH_MODE").lower() == "enforce":
        return "rush"
    return None


def _decide(ctx, a, work, target, known, band, err_lines):
    """The 2.4 decision table -> (mode, cands, reason, refuse_reason)."""
    req = a.mode
    na = _never_ask(ctx)
    if na:
        return "proceed", [], None, na
    linked = _linked(ctx, work, target, known)
    if a.why_adequate:
        n = known.get(a.why_adequate)
        if n is None or n["status"] not in N.LIVE_CONFIRMED or a.why_adequate not in linked:
            raise WhyRefusal("why-not-adequate")
        return "proceed", [], "why-adequate", None
    if req == "proceed":
        return "proceed", [], None, None
    if a.bounded:
        return "proceed", [], "bounded", None
    hyps = [w for w in linked if known[w]["status"] == "hypothesis"]
    held = [w for w in linked if known[w]["status"] in N.LIVE_CONFIRMED]
    if len(hyps) >= 2:
        return "pick", _order(hyps, known, band)[:MAX_CANDS], None, None
    if req == "pick":
        return "proceed", [], "provisional-held" if held else "too-few-members", None
    if not a.ambiguous:
        return "proceed", [], "not-ambiguous", None
    cands = _reoffer(list(a.cand or []), known, err_lines)[:MAX_CANDS]
    if len(cands) < 2:
        return "proceed", [], "too-few-cands", None
    return "infer", cands, None, None


def _moved_from(asks, work, target, members):
    """Pick mode pulls a matching queued ask inline (as `gate` does)."""
    from . import derive
    keys = set(members) | {work, (budget.TGT_PREFIX + target) if target else None}
    keys.discard(None)
    for q in derive.morning_order(asks):
        if q.get("trigger") not in ask.GATE_TRIGGERS or q.get("kind") != "why":
            continue
        if keys & ({q.get("topic"), q.get("work_ref")}
                   | {s.get("why") for s in q.get("shown") or []}):
            return q["ask"]
    return None


def _frame_locked(ctx, a, target, scope, work, topic, band):
    """F1: everything under one bounded store lock; writes are *_unlocked."""
    sid, rows = session_ledger()
    if not rows:
        return _refusal("unknown-frame")
    ask_rows = store.read_rows(store.ASKS)
    frows = frames_rows()
    newest = rows[-1]
    # G4: the asks any budget rule can read, plus every turn ask of the
    # otls this call may bind (a replay is found whatever its age)
    otls = {newest["id"], a.otl} - {None}
    asks = budget.fold_asks_window(ctx, ask_rows, {
        r["ask"] for r in ask_rows if r.get("frame_otl") in otls and r.get("ask")})
    if a.otl:
        row = next((r for r in rows if r.get("id") == a.otl), None)
        if row is None or (row is not newest and not has_record(a.otl, asks, frows)):
            return _refusal("unknown-frame")
    else:
        row = newest
    otl = row["id"]
    tfr = turn_frame_row(ctx, sid, row.get("request_id"))
    tf = tfr["frame_id"] if tfr else None
    if tfr is not None:
        stakes = "high" if tfr.get("stakes") == "high" else "normal"
    else:
        stakes = a.stakes or "normal"
    mine = turn_asks(asks, otl)
    prows = proceed_rows(otl, frows)
    req_cands = list(a.cand or [])

    def differs_from(stored_mode, stored_target, stored_work, stored_cands=None):
        if a.mode != stored_mode or (target or None) != stored_target \
                or (work or None) != stored_work:
            return True
        return stored_cands is not None and sorted(stored_cands) != sorted(req_cands)

    if mine:
        t0 = mine[-1]
        o = _replay_ask(ctx, t0, differs_from(t0.get("asked") or t0.get("mode"),
                                              t0.get("target"), t0.get("work_ref"),
                                              t0.get("asked_cands") or []))
        o.transition["sid"] = sid
        return o
    if prows and a.mode == "proceed":
        p0 = prows[0]
        o = _replay_proceed(p0, differs_from("proceed", p0.get("target"), p0.get("work")))
        o.transition["sid"] = sid
        return o
    known = events.nodes()
    err_lines = []
    mode, cands, reason, refuse = _decide(ctx, a, work, target, known, band, err_lines)
    if mode != "proceed" and not ask.copy_ready(ctx, ask.COPY_KEYS_TURN):
        # copy is an owner gate: a placeholder never reaches the owner
        mode, cands, reason, refuse = "proceed", [], None, "copy-pending"
    if mode == "proceed":
        if prows:   # an escalation the table refused: the stored row stands
            o = _replay_proceed(prows[0], True)
            o.stderr[:0] = err_lines
            o.transition["sid"] = sid
            return o
        prow = {"ev": "proceed", "otl": otl, "tf": tf, "target": target, "work": work,
                "stakes": stakes, "assumption": a.assume or None,
                "refuse_reason": refuse, "reason": reason,
                "asked": a.mode if a.mode != "proceed" else None,
                "at": store.iso_at(ctx.now), "slot": "why"}
        store.append_unlocked(store.FRAMES, lambda _rows: prow)
        o = _Out()
        o.stderr.extend(err_lines + [_proceed_signal(prow)])
        o.transition = {"otl": otl, "tf": tf, "mode": "proceed", "ask": None,
                        "disposition": "proceed", "sid": sid}
        return o
    entries = []
    for c in cands:
        n = known.get(c)
        text = n["text"] if n else c
        entries.append({"text": text, "text_key": budget.text_key(text),
                        "why": c if n else None, "span": a.span or None})
    fields = {"frame_otl": otl, "tf": tf, "mode": mode, "target": target,
              "stakes": stakes, "scope": scope, "cands": entries,
              "escalated_from": "proceed" if prows else None, "slot": "why"}
    if a.mode != mode:
        fields["asked"] = a.mode
    fields["asked_cands"] = req_cands
    tail = ledger_tail(rows, ctx.now)
    if tail >= 2:
        fields["ledger_tail"] = tail
    # a queued ask may be older than the budget window: pick mode reads all
    moved = _moved_from(budget.fold_asks(ask_rows), work, target, cands) \
        if mode == "pick" else None
    shape = ask.default_shape(len(cands))
    rc, res = ask.issue_inline_unlocked(ctx, "turn", shape, topic, cands, work, False,
                                        None, moved, frame=fields, queue=False,
                                        session_id=sid, known=known, asks=asks)
    o = _Out(rc)
    if res.get("ask") is None or not res.get("rows"):
        # refused before any row (label / N7 / umbrella): nothing written
        o.stdout.append(res.get("signal") or "")
        return o
    row0 = dict(next(r for r in res["rows"] if r.get("ask") == res["ask"]
                     and r.get("ev") == "create"))
    if rc == EXIT_OK:
        o.stdout.append(res["text"])
        row0["events"] = ["create"]
        o.stderr.extend(err_lines + [_frame_signal(row0)])
        o.transition = {"otl": otl, "tf": tf, "mode": mode, "ask": res["ask"],
                        "disposition": "asked", "sid": sid}
    else:
        o.stdout.append(res["signal"])
        row0.update(events=["create", "refused"], refuse_reason=res.get("reason"))
        o.stderr.extend(err_lines + [_frame_signal(row0)])
        o.transition = {"otl": otl, "tf": tf, "mode": mode, "ask": res["ask"],
                        "disposition": "proceed", "sid": sid}
    return o


def _transition(ctx, t):
    """F2 (after the why-net lock is released): the turn-frame `why` row,
    once per (tf, ask).  Best effort: Stop infers nothing from a missing
    row (MD 4)."""
    if not t or not t.get("tf"):
        return
    try:
        import time
        import turn_frame as tfl
        path = tfl.store_path(ctx.root, t.get("sid"))
        for r in tfl.read_rows(path):
            if r.get("ev") == "why" and r.get("frame_id") == t["tf"] \
                    and r.get("ask") == t.get("ask"):
                return
        tfl.append_row(path, {"v": tfl.FRAME_VERSION, "ev": "why", "frame_id": t["tf"],
                              "otl": t.get("otl"), "mode": t.get("mode"),
                              "ask": t.get("ask"), "disposition": t.get("disposition"),
                              "ts": time.time()})
    except Exception:  # noqa: BLE001 -- best effort (FR10c heals on replay)
        pass


# test hooks (FR11): called inside F1 / A1 with the lock held
HOLD_HOOK = [None]


def _bounded_lock():
    old = store.LOCK_WAIT
    store.LOCK_WAIT = FRAME_LOCK_WAIT
    return old


def cmd_frame(ctx, a, out=None, err=None):
    # Legacy candidate/proceed policy cannot expose another owner question or
    # authorize work. Historical answer ingestion remains a non-asking reader.
    if ctx.enabled() and not a.answer:
        (out or sys.stdout).write(_sig("WHY_REFUSED", reason="retired-use-four-slot-intake") + "\n")
        return EXIT_FLOOR
    out = out or sys.stdout
    err = err or sys.stderr
    if a.answer:
        return cmd_answer(ctx, a, out, err)
    if a.mode not in MODES or (a.stakes and a.stakes not in STAKES):
        return EXIT_USAGE
    if len(a.cand or []) > MAX_CANDS:
        return EXIT_USAGE
    # F0, unlocked: target, topic, metacog snapshot.  No transcript.
    try:
        target, scope = resolve_target(ctx.root, a.target) if a.target else (None, "framework")
    except WhyRefusal as r:
        return _emit(out, err, _refusal(r.reason))
    work = None
    if a.work:
        work = budget.canonical_topic(a.work)
        if work is None or work.startswith(budget.TGT_PREFIX):
            return _emit(out, err, _refusal("bad-topic"))
    topic = work or ((budget.TGT_PREFIX + target) if target else None)
    if a.mode in ("pick", "infer") and topic is None:
        return _emit(out, err, _refusal("no-topic"))
    band = metacog_band(target)
    old = _bounded_lock()
    try:
        with store.locked(), store.cached_reads():
            if HOLD_HOOK[0]:
                HOLD_HOOK[0]()
            o = _frame_locked(ctx, a, target, scope, work, topic, band)
    except store.StoreBusy:
        return _emit(out, err, _refusal("store-busy", EXIT_BUDGET))
    except WhyRefusal as r:
        return _emit(out, err, _refusal(r.reason))
    finally:
        store.LOCK_WAIT = old
    _transition(ctx, o.transition)        # F2
    return _emit(out, err, o)             # F3


def _emit(out, err, o):
    for line in o.stdout:
        out.write(line + "\n")
    for line in o.stderr:
        err.write(line + "\n")
    return o.rc


# ---- frame --answer (2.7) -------------------------------------------------------

def _answer_row(ask_id, probe_id, rows=None):
    for r in frames_rows() if rows is None else rows:
        if r.get("ev") == "answer" and r.get("ask") == ask_id and r.get("probe") == probe_id:
            return r
    return None


def _answer_signal(r):
    return _sig("WHY_FRAME_ANSWER", ask=r.get("ask"), probe=r.get("probe"),
                path=r.get("path"), node=r.get("node") or "-")


def cmd_answer(ctx, a, out, err):
    """`frame --answer --ask A --probe P (--quote|--reword|--none) [--cause]`:
    the agent records exactly one free-text path for a `free` probe of a
    turn ask.  Idempotent by (ask, probe)."""
    flags = [a.quote is not None, a.reword is not None, bool(a.none)]
    if sum(flags) != 1 or not a.ask or not a.probe:
        return EXIT_USAGE
    path = "a" if a.quote is not None else "b" if a.reword is not None else "c"
    try:
        cause = events.check_cause(a.cause)
    except WhyRefusal as r:
        return _emit(out, err, _refusal(r.reason, EXIT_USAGE))
    # A0, unlocked: the ask, the probe, the quote resolve (a transcript read)
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    ta = asks.get(a.ask)
    if ta is None or ta.get("trigger") != "turn":
        return _emit(out, err, _refusal("unknown-ask"))
    try:
        probe = events._probe(store.read_rows(store.PROBES), a.probe)
    except WhyRefusal as r:
        return _emit(out, err, _refusal(r.reason))
    if probe.get("ask") != a.ask:
        return _emit(out, err, _refusal("probe-not-of-ask"))
    if probe.get("verdict") != "free":
        return _emit(out, err, _refusal("probe-not-free"))
    prior = _answer_row(a.ask, a.probe)
    if prior is not None:
        out.write(_answer_signal(prior) + "\n")
        return EXIT_OK
    sid = (probe.get("source") or {}).get("session_id")
    ref, text = None, None
    if path == "a":
        ref = why_owner_turn.resolve(a.quote, sid)
        if isinstance(ref, why_owner_turn.Refusal):
            return _emit(out, err, _refusal(ref.reason))
        if why_owner_turn.turn_otl(ref.session_id, ref.message_uuid) != probe.get("otl"):
            return _emit(out, err, _refusal("answer-turn-mismatch"))
        text = why_owner_turn.owner_span(ref.text, a.quote)
    elif path == "b":
        text = a.reword
    # A1: one bounded lock
    old = _bounded_lock()
    try:
        with store.locked():
            if HOLD_HOOK[0]:
                HOLD_HOOK[0]()
            row = _answer_locked(ctx, ta, probe, path, text, ref, cause, a.quote)
    except store.StoreBusy:
        return _emit(out, err, _refusal("store-busy", EXIT_BUDGET))
    except WhyRefusal as r:
        return _emit(out, err, _refusal(r.reason))
    finally:
        store.LOCK_WAIT = old
    out.write(_answer_signal(row) + "\n")
    return EXIT_OK


def _answer_locked(ctx, ta, probe, path, text, ref, cause, quote):
    prior = _answer_row(ta["ask"], probe["id"])
    if prior is not None:
        return prior
    probe = events._probe(store.read_rows(store.PROBES), probe["id"])
    wid = None
    if path in ("a", "b"):
        ev = [{"otl": ta.get("frame_otl"), "role": "frame", "span": None},
              {"otl": probe.get("otl"), "role": "answer", "span": None}]
        born = {"ask": ta["ask"], "cs": ta.get("cs"), "slot": None, "probe": probe["id"]}
        row = events.propose_unlocked(
            text, ta.get("scope") or "framework", (), ta.get("work_ref"),
            (probe.get("source") or {}).get("session_id"),
            evidence=ev, target=ta.get("target"), born=born)
        wid = row["why"]
        if path == "a":
            n = events.N.fold(store.read_rows(store.EVENTS)).get(wid) or {}
            if n.get("status") not in N.LIVE_CONFIRMED:
                events.confirm_quote_unlocked(wid, quote, ref, probe.get("otl"))
    ans = {"ev": "answer", "ask": ta["ask"], "probe": probe["id"], "otl": probe.get("otl"),
           "path": path, "node": wid, "cause": cause, "at": store.iso_at(ctx.now),
           "slot": "why"}
    return store.append_unlocked(store.FRAMES, lambda _rows: ans)


# ---- lookup (2.8) -----------------------------------------------------------------

def cmd_lookup(ctx, a, out=None):
    """Read-only JSON: the Whys of a scope (target / work filters), the open
    asks on their topics, the target's metacog band.  Writes nothing."""
    out = out or sys.stdout
    if not a.scope or not N.SCOPE_RE.match(a.scope):
        return EXIT_USAGE
    work = budget.canonical_topic(a.work) if a.work else None
    doc = {"v": 1, "enabled": ctx.enabled(), "scope": a.scope, "target": a.target,
           "work": work, "whys": [], "open_asks": [], "metacog": "unknown",
           "preference": None, "state": None, "truncated": False}
    if not ctx.enabled():
        out.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
        return EXIT_OK
    known = events.N.fold(store.read_rows(store.EVENTS))
    linked = set(ask.work_whys(ctx, work, known)) if work else None
    whys = []
    for wid in sorted(known, reverse=True):
        n = known[wid]
        if n["scope"] != a.scope:
            continue
        if a.target and n.get("target") != a.target:
            continue
        if linked is not None and wid not in linked:
            continue
        whys.append(n)
    keys = {w["id"] for w in whys} | ({work} if work else set()) \
        | ({budget.TGT_PREFIX + a.target} if a.target else set())
    asks = budget.fold_asks(store.read_rows(store.ASKS))
    opn = [x for x in sorted(asks.values(), key=lambda x: x["ask"], reverse=True)
           if x.get("state") not in budget.TERMINAL and budget.ask_topic_keys(x) & keys]
    redact = bool(os.environ.get(why_owner_turn.DISPATCH_ENV_MARKER))
    doc["truncated"] = len(whys) > LOOKUP_WHYS or len(opn) > LOOKUP_ASKS
    for n in whys[:LOOKUP_WHYS]:
        w = {"id": n["id"], "status": n["status"], "worded_by": n["worded_by"],
             "scope": n["scope"], "target": n.get("target"),
             "evidence": len(n.get("evidence") or [])}
        if not redact:
            w["text"] = n["text"]
        doc["whys"].append(w)
    doc["open_asks"] = [{"ask": x["ask"], "state": x.get("state"), "trigger": x.get("trigger"),
                         "topic": x.get("topic"), "frame_otl": x.get("frame_otl")}
                        for x in opn[:LOOKUP_ASKS]]
    doc["metacog"] = metacog_band(a.target)
    if redact:
        doc["redacted"] = "dispatch-worker"
    out.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
    return EXIT_OK


# ---- metacog-reset (2.11) -----------------------------------------------------------

def cmd_metacog_reset(ctx, a, out=None):
    """Owner-gated: needs a current owner turn, refused in a dispatch
    worker.  Writes state/why-net/metacog-reset.json atomically under the
    store lock; derive ignores the target's evidence older than it."""
    out = out or sys.stdout
    if os.environ.get(why_owner_turn.DISPATCH_ENV_MARKER):
        out.write(_sig("WHY_REFUSED", reason="dispatch-worker") + "\n")
        return EXIT_FLOOR
    try:
        target, _scope = resolve_target(ctx.root, a.target)
    except WhyRefusal as r:
        out.write(_sig("WHY_REFUSED", reason=r.reason) + "\n")
        return EXIT_FLOOR
    _sid, rows = session_ledger()
    if not rows:
        out.write(_sig("WHY_REFUSED", reason="unknown-frame") + "\n")
        return EXIT_FLOOR
    with store.locked():
        p = store.path(store.METACOG_RESET)
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}
        doc[target] = {"at": store.iso_at(ctx.now), "otl": rows[-1]["id"]}
        store.atomic_write(p, json.dumps(doc, ensure_ascii=False, sort_keys=True))
    out.write(_sig("WHY_OK", metacog_reset=target) + "\n")
    return EXIT_OK


# ---- owner erase (F25) ----------------------------------------------------------------

def erase_target(ctx, target):
    """The owner's erase of the frame text for a target: an audit line
    first (an `act-void` event, reason owner-erase-target), then
    frames.jsonl is rewritten without `proceed.assumption` text and without
    the `answer` rows of that target, and the target's turn asks lose their
    candidate `text` / `span`.  Chains are recomputed (store.rewrite_chain).
    -> {"frames": n, "asks": n}."""
    if not N.TARGET_RE.match(target or ""):
        raise WhyRefusal("unknown-target")
    with store.locked():
        def build(rows):
            return events._event(rows, "act-void", None, "why-net",
                                 {"act": None, "reason": "owner-erase-target",
                                  "target": target})
        store.append_unlocked(store.EVENTS, build)
        asks = store.read_rows(store.ASKS)
        target_asks = {r.get("ask") for r in asks if r.get("ev") == "create"
                       and r.get("trigger") == "turn" and r.get("target") == target}
        frames, nf = [], 0
        for r in frames_rows():
            r = dict(r)
            if r.get("ev") == "proceed" and r.get("target") == target and r.get("assumption"):
                r["assumption"] = None
                nf += 1
            if r.get("ev") == "answer" and r.get("ask") in target_asks:
                nf += 1
                continue
            frames.append(r)
        if nf:
            store.rewrite_chain(store.FRAMES, frames)
        na = 0
        out_asks = []
        for r in asks:
            r = dict(r)
            if r.get("ask") in target_asks and r.get("ev") == "create" and r.get("cands"):
                r["cands"] = [dict(c, text=None, span=None) for c in r["cands"]]
                na += 1
            out_asks.append(r)
        if na:
            store.rewrite_chain(store.ASKS, out_asks)
    return {"frames": nf, "asks": na}
