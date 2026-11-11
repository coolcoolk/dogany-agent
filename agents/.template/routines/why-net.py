#!/usr/bin/env python3
"""why-net.py -- v2.7 T1 Why net CLI (spec docs/design/v27-t1-why-net-LOCKED.md
section 3; P1 dispatcher + floor verbs, P2 elicitation verbs).

Exit codes:
  0 ok / 2 usage
  3 REFUSED by the floor (nothing written)
  4 REFUSED by budget or never-ask (P2), or the net is off (reason=off)
  1 internal error (fail-closed for confirm)
Every refusal line on stdout is a machine signal
(`signal::WHY_REFUSED reason=..`), dropped by the bridge outbound gate.

P1 verbs: propose confirm attach revise pivot retire interpret seed
          render show status; derive --audit (hash chain + confirm re-proof).
P2 verbs: ask (--reveal) enqueue ups derive morning
          (routines/lib/why_net/{ask,ups,derive,morning}.py).
rev 3:    gate (inline before irreversible / trial work), read (one
          signal's reading), retro-section / retro-shown (the daily retro
          slot's provider, lib/why_net/retro.py).
P3 verbs: head, lint-ticket (--resolve) (lib/why_net/surfaces.py); ups
          step 6 and the derive correction pass (lib/why_net/corrections.py).
seed:     decision-ledger import (lib/why_net/seed.py).
AMEND-1:  act (A2 decision / A3 correction; --machine A4-A6 from the ticket
          lint), act-void (--erase), show --acts, derive --audit re-proves
          acts and settles (lib/why_net/acts.py; the shared confirm
          mechanics are lib/confirm_mechanics.py).
AMEND-2:  frame (the Why asked inside the working turn: pick / infer /
          proceed, --answer for a free-text reason), lookup (read-only),
          metacog-reset, erase-frames (lib/why_net/frame.py, metacog.py).
M1:       record-ticket / record-execution append citations and execution
          receipts to the same event log. show --ticket and lint-ticket
          --trace expose the path, actual confirmation and all re-checks.

Agent path for Why work (AMEND-2, S-P1a): call `frame` before
consequential work; it asks the owner inside the reply or records a
proceed.  A Why node exists only because the owner answered: `propose` is
not the agent's door to a new Why (derive --audit reports an agent
propose with no owner answer as unanswered-propose).
"""

import argparse
import glob
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(_HERE, "lib"))

import machine_signal as ms  # noqa: E402
from why_net import WhyRefusal  # noqa: E402
from why_net import ask as A  # noqa: E402
from why_net import budget  # noqa: E402
from why_net import events as E  # noqa: E402
from why_net import store  # noqa: E402


class _Lazy(object):
    """A why_net module imported on first use (AMEND-2 G4: the frame verb
    runs on the turn's hot path and never needs most of them)."""

    def __init__(self, name):
        self._name, self._mod = name, None

    def __getattr__(self, attr):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module("why_net." + self._name)
        return getattr(self._mod, attr)


ACT = _Lazy("acts")
C = _Lazy("corrections")
D = _Lazy("derive")
M = _Lazy("morning")
U = _Lazy("ups")
R = _Lazy("render")
RETRO = _Lazy("retro")
SEED = _Lazy("seed")
S = _Lazy("surfaces")
# why_net.acts.KINDS (pinned equal by test-why-net-amend2), spelled here so
# building the parser does not import the act module
ACT_KINDS = ("decision", "correction", "accept", "reject", "initiate")

EXIT_OK, EXIT_INTERNAL, EXIT_USAGE, EXIT_FLOOR, EXIT_BUDGET = 0, 1, 2, 3, 4

# Final grill B1 (dec-338 "on as a feature, off by default"): with WHY_NET
# off every elicitation and write verb refuses (exit 4, reason=off) and
# writes nothing -- so nothing from the off period (a dispatch's new-work
# enqueue, a gate's queued ask) is pushed once the net is turned on.  Still
# served when off: ups (the sentinel ledger row only), show, status, head,
# render, lint-ticket --resolve, derive (--audit only), morning and
# retro-section (both send nothing), act-void (owner erase).
OFF_REFUSED = ("propose", "confirm", "attach", "revise", "pivot", "retire",
               "interpret", "seed", "ask", "enqueue", "gate", "read",
               "retro-shown", "act", "lint-ticket",
               # AMEND-2 (slice 3): the in-turn frame and the metacog reset
               "frame", "metacog-reset")
OFF_REFUSED += ("record-ticket", "record-execution", "start-gate", "record-judgment", "record-authority")
# M1-4 G5 CLI: review evidence rows on the ticket receipt chain.
# Reading Verify evidence is mandatory even when elicitation is disabled.
OFF_REFUSED += ("review-done", "review-regression")


# AMEND-2 2.11 / 2.2: refusals that are usage errors (exit 2), not floors.
USAGE_REASONS = ("cause-required", "bad-cause", "no-topic")


def _refused(reason, **kv):
    print(ms.signal("WHY_REFUSED", reason=reason, **kv))
    return EXIT_USAGE if reason in USAGE_REASONS else EXIT_FLOOR


def _ok(row):
    print(ms.signal("WHY_OK", ev=row.get("ev", ""), id=row.get("id", ""),
                    why=row.get("why", "")))
    return EXIT_OK


def cmd_propose(a):
    scope, target = a.scope, None
    if a.target:
        # AMEND-2 2.2: a target is validated against the installed contracts
        from why_net import frame as F
        target, scope = F.resolve_target(a.ctx.root, a.target, a.scope)
    if not scope:
        return EXIT_USAGE
    return _ok(E.propose(a.text, scope, a.parent or [], a.work,
                         os.environ.get("CLAUDE_CODE_SESSION_ID")
                         or os.environ.get("CLAUDE_SESSION_ID"), target=target))


def cmd_confirm(a):
    return _ok(E.confirm_quote(a.why, a.quote, a.session, a.uuid))


def cmd_attach(a):
    return _ok(E.attach(a.why, a.parent))


def cmd_revise(a):
    return _ok(E.revise(a.why, a.text, a.probe, cause=a.cause,
                        change_kind=a.change_kind,
                        refine_evidence=_json_arg(a.refine_evidence)))


def _json_arg(path):
    if path is None:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_record_ticket(a):
    from why_net import citations
    return _ok(citations.record_ticket(
        a.path, _json_arg(a.definition), a.target, a.era, a.supersedes,
        a.confirmed_by, a.why or [], a.depends_on or [], a.uses or [], a.ctx.root))


def cmd_record_execution(a):
    from why_net import citations
    return _ok(citations.execution(a.ticket, a.run, a.session, a.span,
                                   _json_arg(a.cost), a.ctx.root))


def _review_ledger(a):
    from record_provider import configured
    provider = configured()
    if provider is None:
        raise ValueError("review-record-provider-unavailable")
    return provider.review, provider.review.default_path(a.ctx.root)


def _review_at(a):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(a.ctx.now)))


def _review_recorded(a, kind):
    print("review %s recorded: %s" % (kind, a.ticket))
    return EXIT_OK


def cmd_review_done(a):
    R, ledger = _review_ledger(a)
    try:
        R.record_done(ledger, a.ticket, a.ref, _review_at(a), a.verdict, a.candidate)
    except ValueError:
        return _refused("bad-review-evidence")
    if a.verdict != "pass":
        return _refused("review-verdict-fail")
    return _review_recorded(a, "done")


def cmd_review_regression(a):
    R, ledger = _review_ledger(a)
    try:
        R.record_regression(ledger, a.ticket, a.scenario, a.phase, a.result,
                            a.ref, _review_at(a), a.candidate)
    except ValueError:
        return _refused("bad-review-evidence")
    return _review_recorded(a, "regression")


def cmd_review_check(a):
    R, ledger = _review_ledger(a)
    reason = R.missing(ledger, a.ticket, a.candidate, a.required_scenario,
                       a.prior_completed_scenario)
    print(reason or "ok")
    return EXIT_FLOOR if reason else EXIT_OK


def cmd_start_gate(a):
    from why_net import r3
    result = r3.evaluate(_json_arg(a.request), a.ctx.root)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return EXIT_OK if result["decision"] == "allow" else EXIT_FLOOR


def cmd_record_authority(a):
    from why_net import r3
    return _ok(r3.record_authority(_json_arg(a.request), a.session, a.uuid, a.ctx.root))


def cmd_record_judgment(a):
    from why_net import r3
    return _ok(r3.record_judgment(_json_arg(a.judgment), a.session, a.uuid, a.ctx.root))


def cmd_end(a):
    return _ok(E.end(a.verb, a.why, getattr(a, "to", None), a.quote,
                     a.session, a.uuid, cause=a.cause))


def cmd_interpret(a):
    row = E.interpret(a.probe, a.verdict, a.why, a.note, a.scope)
    print(ms.signal("WHY_OK", interp=row["interp"], verdict=row["verdict"]))
    return EXIT_OK


def cmd_seed(a):
    if not store.is_empty(store.EVENTS):
        return _refused("events-not-empty")
    stats = SEED.run(a.ctx, a.src)
    print(ms.signal("WHY_SEED", **stats))
    return EXIT_OK


def cmd_derive(a):
    if a.audit:
        breaks = store.audit()
        for b in breaks:
            print(ms.signal("WHY_AUDIT_BREAK", file=b["file"], line=b["line"],
                            kind=b["kind"]))
        # Grill fix 5: a recomputed chain proves nothing about who wrote a
        # row, so every confirm is re-proven against the resolver / probes.
        unbacked = E.audit_confirms()
        for u in unbacked:
            print(ms.signal("WHY_AUDIT_UNBACKED", evt=u["evt"], why=u["why"],
                            reason=u["reason"]))
        # Grill-2 fixes 1-3: the ledger rows themselves are re-derived from
        # the transcript's main chain; a row with no real turn is a forgery.
        forged = E.audit_ledger()
        for f in forged:
            print(ms.signal("WHY_AUDIT_UNBACKED_TURN", row=f["row"],
                            session=f["session_id"], reason=f["reason"]))
        # AMEND-1: every act re-proven against its ref, every settle against
        # I1 / I2 / I5 and the library verdict; old reveal maps checked.
        acts_bad = ACT.audit(a.ctx)
        for f in acts_bad:
            print(ms.signal("WHY_AUDIT_UNBACKED", evt=f["id"], why=f["why"] or "",
                            reason=f["reason"]))
        if not breaks and not unbacked and not forged and not acts_bad:
            print(ms.signal("WHY_AUDIT_OK"))
        # S-P1a (detect-only, not an integrity break): an agent propose with
        # no owner answer behind it once the frame path is live.
        for wid in E.audit_unanswered_propose():
            print(ms.signal("WHY_AUDIT_UNANSWERED_PROPOSE", why=wid))
    if not a.ctx.enabled():
        return EXIT_OK  # final grill B1: off -> the audit only, no derived/
    D.run(a.ctx)
    if a.ctx.enabled():
        # P3 correction pass (AC10a): owner turns of the last window through
        # the AC5 hybrid classifier; --no-model keeps it to the patterns.
        import owner_turns
        model = None if a.no_model else C.default_model()
        stats = C.nightly_pass(owner_turns.project_dir(a.ctx.root), model=model)
        if stats["records"] or stats["backfills"]:
            print(ms.signal("WHY_DERIVE", "corrections", **stats))
        # The console reads derived/nodes.json + why-net.md (spec 5.5):
        # both are refreshed by the same nightly run.
        R.write()
    return EXIT_OK  # detect-only, never a refusal (S1)


def _prev_transcript(root):
    import owner_turns
    files = glob.glob(os.path.join(owner_turns.project_dir(root), "*.jsonl"))
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def cmd_head(a):
    tp = _prev_transcript(a.ctx.root)
    paths = S.touched_tickets(tp) if tp else []
    since = S.session_start_ts(tp) if tp else None
    for line in S.head_lines(a.ctx.root, a.budget_bytes, paths, since):
        print(line)
    return EXIT_OK


def cmd_lint_ticket(a):
    if a.resolve:
        print(S.resolve_ticket(a.resolve, a.ctx.root))
        return EXIT_OK
    if not a.path:
        return EXIT_USAGE
    if a.trace:
        from why_net import citations
        print(json.dumps(citations.trace(a.path, a.ctx.root), ensure_ascii=False, indent=2))
        return EXIT_OK
    r = S.lint_ticket(a.path, a.ctx.root)
    parts = ["state=" + r["state"]]
    for key in ("why", "via", "reason"):
        if r.get(key):
            parts.append("%s=%s" % (key, r[key]))
    print(" ".join(parts))
    return EXIT_OK


def cmd_ask(a):
    return A.cmd_ask(a.ctx, a)


def cmd_frame(a):
    from why_net import frame as F
    return F.cmd_frame(a.ctx, a)


def cmd_lookup(a):
    from why_net import frame as F
    return F.cmd_lookup(a.ctx, a)


def cmd_pm_route(a):
    import json
    from why_net import pm_side
    print(json.dumps(pm_side.route(a.ctx, a.why, a.session), sort_keys=True))
    return EXIT_OK


def cmd_pm_bundle(a):
    import datetime
    import json
    from why_net import pm_side
    day = a.day or datetime.datetime.utcnow().strftime("%Y-%m-%d")
    print(json.dumps(pm_side.bundle(day, root=a.ctx.root), ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def cmd_metacog_reset(a):
    from why_net import frame as F
    return F.cmd_metacog_reset(a.ctx, a)


def cmd_erase_frames(a):
    from why_net import frame as F
    res = F.erase_target(a.ctx, a.target)
    print(ms.signal("WHY_FRAMES_ERASED", target=a.target, frames=res["frames"],
                    asks=res["asks"]))
    return EXIT_OK


def cmd_enqueue(a):
    return A.cmd_enqueue(a.ctx, a)


def cmd_morning(a):
    return M.run(a.ctx)


def cmd_gate(a):
    return A.cmd_gate(a.ctx, a)


def cmd_read(a):
    if not a.signal or not a.why:
        return EXIT_USAGE
    row = RETRO.read(a.ctx, a.signal, a.why)
    print(ms.signal("WHY_READ", signal=row["reading"], whys=",".join(row["whys"])))
    return EXIT_OK


def cmd_retro_section(a):
    print(json.dumps(RETRO.section(a.ctx), ensure_ascii=False))
    return EXIT_OK


def cmd_retro_shown(a):
    if not a.ask or a.message_id is None:
        return EXIT_USAGE
    RETRO.shown(a.ctx, a.ask, a.message_id, a.chat_id, a.text)
    print(ms.signal("WHY_OK", shown=a.ask))
    return EXIT_OK


def cmd_render(a):
    print(ms.signal("WHY_OK", rendered=R.write()))
    return EXIT_OK


def cmd_act(a):
    if a.machine:
        row = ACT.record_machine(a.ctx, a.kind, a.ticket, a.session)
        if row is None:
            print(ms.signal("WHY_ACT", "none"))
            return EXIT_OK
        print(ms.signal("WHY_ACT", act=row["act"], why=row["why"], kind=row["kind"]))
        return EXIT_OK
    if not (a.kind and a.ref and a.why and a.direction):
        return EXIT_USAGE
    try:
        row = ACT.record(a.ctx, a.kind, a.ref, a.why, a.direction, a.note)
    except ValueError:
        return EXIT_USAGE  # note too long / bad direction (F14)
    print(ms.signal("WHY_ACT", act=row["act"], why=row["why"], kind=row["kind"]))
    return EXIT_OK


def cmd_act_void(a):
    rows = {r["act"]: r for r in ACT.act_rows()}
    if a.act not in rows:
        return _refused("unknown-act")
    try:
        _ok(E.act_void(a.act, rows[a.act].get("why")))
    except WhyRefusal as r:
        if not (a.erase and r.reason == "already-void"):
            raise
    if a.erase:
        ACT.erase(a.act)
        print(ms.signal("WHY_ACT_ERASED", act=a.act))
    return EXIT_OK


def cmd_show(a):
    if a.ticket:
        from why_net import citations
        path = S.find_ticket(a.ticket, os.path.join(a.ctx.root, "worklog"))
        if not path:
            return _refused("ticket-missing")
        print(json.dumps(citations.trace(path, a.ctx.root), ensure_ascii=False, indent=2))
        return EXIT_OK
    if getattr(a, "acts", False):
        rows = ACT.act_rows()
        if a.why:
            rows = [r for r in rows if r.get("why") == a.why]
        print(json.dumps([{k: r.get(k) for k in ("act", "at", "kind", "ref", "dec", "why",
                                                 "direction", "class", "link", "topic")}
                          for r in rows], ensure_ascii=False, indent=2))
        return EXIT_OK
    nodes = E.nodes()
    if a.why:
        if a.why not in nodes:
            return _refused("unknown-why")
        nodes = {a.why: nodes[a.why]}
    print(json.dumps(nodes, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_status(a):
    nodes = E.nodes()
    counts = {}
    for n in nodes.values():
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    doc = {"nodes": len(nodes), "by_status": counts}
    frames = store.read_rows(store.FRAMES)
    if frames:   # AMEND-2 2.5: proceed / answer rows (absent before any frame)
        doc["frames"] = len(frames)
    print(json.dumps(doc, sort_keys=True))
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(prog="why-net.py")
    sub = p.add_subparsers(dest="verb")

    s = sub.add_parser("propose")
    s.add_argument("--text", required=True)
    s.add_argument("--scope")
    s.add_argument("--parent", action="append")
    s.add_argument("--work")
    s.add_argument("--target")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("confirm")
    s.add_argument("--why", required=True)
    s.add_argument("--quote")
    s.add_argument("--session")
    s.add_argument("--uuid")
    s.set_defaults(fn=cmd_confirm)

    s = sub.add_parser("attach")
    s.add_argument("--why", required=True)
    s.add_argument("--parent", required=True)
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("revise")
    s.add_argument("--why", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--probe", required=True)
    s.add_argument("--cause")
    s.add_argument("--change-kind", choices=("node", "refine", "bend", "split", "end"))
    s.add_argument("--refine-evidence", help="JSON file with PM/R3 before/after judgment")
    s.set_defaults(fn=cmd_revise)

    for verb in ("pivot", "retire"):
        s = sub.add_parser(verb)
        s.add_argument("--why", required=True)
        if verb == "pivot":
            s.add_argument("--to")
        s.add_argument("--quote")
        s.add_argument("--session")
        s.add_argument("--uuid")
        s.add_argument("--cause")
        s.set_defaults(fn=cmd_end)

    s = sub.add_parser("interpret")
    s.add_argument("probe")
    s.add_argument("--verdict", required=True)
    s.add_argument("--why", required=True)
    s.add_argument("--scope", default="item")
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_interpret)

    s = sub.add_parser("seed")
    s.add_argument("--from", dest="src", required=True)
    s.set_defaults(fn=cmd_seed)

    s = sub.add_parser("derive")
    s.add_argument("--audit", action="store_true")
    s.add_argument("--no-model", action="store_true")
    s.set_defaults(fn=cmd_derive)

    s = sub.add_parser("ask")
    s.add_argument("--trigger")
    s.add_argument("--topic")
    s.add_argument("--shape")
    s.add_argument("--work")
    s.add_argument("--cand", action="append")
    s.add_argument("--urgent", action="store_true")
    s.add_argument("--reveal", action="store_true")
    s.add_argument("--opt", action="append")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("enqueue")
    s.add_argument("--kind", default="why")
    s.add_argument("--trigger")
    s.add_argument("--topic")
    s.add_argument("--shape")
    s.add_argument("--work")
    s.add_argument("--cand", action="append")
    s.set_defaults(fn=cmd_enqueue)

    sub.add_parser("morning").set_defaults(fn=cmd_morning)

    # AMEND-2: the Why inside the working turn
    s = sub.add_parser("frame")
    s.add_argument("--mode")
    s.add_argument("--target")
    s.add_argument("--work")
    s.add_argument("--cand", action="append")
    s.add_argument("--span")
    s.add_argument("--ambiguous", action="store_true")
    s.add_argument("--bounded", action="store_true")
    s.add_argument("--why-adequate", dest="why_adequate")
    s.add_argument("--assume")
    s.add_argument("--stakes")
    s.add_argument("--otl")
    s.add_argument("--answer", action="store_true")
    s.add_argument("--ask")
    s.add_argument("--probe")
    s.add_argument("--quote")
    s.add_argument("--reword")
    s.add_argument("--none", action="store_true")
    s.add_argument("--cause")
    s.set_defaults(fn=cmd_frame)
    s = sub.add_parser("lookup")
    s.add_argument("--scope")
    s.add_argument("--target")
    s.add_argument("--work")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_lookup)
    s = sub.add_parser("pm-route")
    s.add_argument("--why", required=True)
    s.add_argument("--session")
    s.set_defaults(fn=cmd_pm_route)
    s = sub.add_parser("pm-bundle")
    s.add_argument("--day")
    s.set_defaults(fn=cmd_pm_bundle)
    s = sub.add_parser("metacog-reset")
    s.add_argument("--target", required=True)
    s.set_defaults(fn=cmd_metacog_reset)
    s = sub.add_parser("erase-frames")
    s.add_argument("--target", required=True)
    s.set_defaults(fn=cmd_erase_frames)

    # AMEND-1 rev 3: inline gate, signal readings, the retro provider
    s = sub.add_parser("gate")
    s.add_argument("--work")
    s.add_argument("--target")
    s.add_argument("--before", choices=A.GATE_REASONS)
    s.set_defaults(fn=cmd_gate)
    s = sub.add_parser("read")
    s.add_argument("--signal")
    s.add_argument("--why", action="append")
    s.set_defaults(fn=cmd_read)
    sub.add_parser("retro-section").set_defaults(fn=cmd_retro_section)
    s = sub.add_parser("retro-shown")
    s.add_argument("--ask")
    s.add_argument("--message-id", type=int)
    s.add_argument("--chat-id", type=int)
    s.add_argument("--text", default="")
    s.set_defaults(fn=cmd_retro_shown)

    for verb, fn in (("render", cmd_render), ("status", cmd_status)):
        sub.add_parser(verb).set_defaults(fn=fn)
    s = sub.add_parser("show")
    s.add_argument("--ticket", help="Product ticket path or legacy ticket ID")
    s.add_argument("--why")
    s.add_argument("--acts", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("act")
    s.add_argument("--kind", choices=ACT_KINDS)
    s.add_argument("--ref")
    s.add_argument("--why")
    s.add_argument("--direction", choices=("consistent", "contradicts"))
    s.add_argument("--note")
    s.add_argument("--machine", action="store_true")
    s.add_argument("--ticket")
    s.add_argument("--session")
    s.set_defaults(fn=cmd_act)

    s = sub.add_parser("act-void")
    s.add_argument("--act", required=True)
    s.add_argument("--erase", action="store_true")
    s.set_defaults(fn=cmd_act_void)

    s = sub.add_parser("head")
    s.add_argument("--budget-bytes", type=int, default=None)
    s.set_defaults(fn=cmd_head)

    s = sub.add_parser("lint-ticket")
    s.add_argument("path", nargs="?")
    s.add_argument("--resolve", default=None)
    s.add_argument("--trace", action="store_true")
    s.set_defaults(fn=cmd_lint_ticket)

    s = sub.add_parser("record-ticket")
    s.add_argument("path")
    for key in ("definition", "target", "era", "supersedes", "confirmed-by"):
        s.add_argument("--" + key, required=True)
    for key in ("why", "depends-on", "uses"):
        s.add_argument("--" + key, action="append")
    s.set_defaults(fn=cmd_record_ticket)

    s = sub.add_parser("record-execution")
    s.add_argument("--ticket", required=True)
    ref = s.add_mutually_exclusive_group(required=True)
    ref.add_argument("--run")
    ref.add_argument("--session")
    s.add_argument("--span")
    s.add_argument("--cost", help="JSON file of observed resource deltas, once per execution")
    s.set_defaults(fn=cmd_record_execution)

    s = sub.add_parser("review-done")
    s.add_argument("--candidate", help="exact candidate digest for this review")
    s.add_argument("--ticket", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--verdict", required=True, choices=["pass", "fail"])
    s.set_defaults(fn=cmd_review_done)

    s = sub.add_parser("review-regression")
    s.add_argument("--candidate", help="exact candidate digest for this observation")
    s.add_argument("--ticket", required=True)
    s.add_argument("--scenario", required=True)
    s.add_argument("--phase", required=True, choices=["before", "after"])
    s.add_argument("--result", required=True, choices=["pass", "fail"])
    s.add_argument("--ref", required=True)
    s.set_defaults(fn=cmd_review_regression)

    s = sub.add_parser("review-check")
    s.add_argument("--candidate", help="exact candidate digest to check")
    s.add_argument("--required-scenario", action="append", default=[])
    s.add_argument("--prior-completed-scenario", action="append", default=[])
    s.add_argument("--ticket", required=True)
    s.set_defaults(fn=cmd_review_check)

    s = sub.add_parser("start-gate")
    s.add_argument("--request", required=True, help="R3 JSON request; stage selects the execution boundary")
    s.set_defaults(fn=cmd_start_gate)

    s = sub.add_parser("record-judgment")
    s.add_argument("--judgment", required=True, help="Exact work/hash judgment candidate JSON")
    s.add_argument("--session", required=True)
    s.add_argument("--uuid", required=True)
    s.set_defaults(fn=cmd_record_judgment)
    s = sub.add_parser("record-authority")
    s.add_argument("--request", required=True)
    s.add_argument("--session", required=True)
    s.add_argument("--uuid", required=True)
    s.set_defaults(fn=cmd_record_authority)
    return p


def main(argv=None, ctx=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ctx = ctx or budget.Ctx()
    if argv[:1] == ["ups"]:
        # UserPromptSubmit hook: stdin JSON, never prints, always exit 0.
        return U.main(ctx)
    parser = build_parser()
    try:
        a = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    a.ctx = ctx
    if not getattr(a, "fn", None):
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if a.verb in OFF_REFUSED and not ctx.enabled() \
            and not (a.verb == "lint-ticket" and a.resolve):
        # Final grill B1 (dec-338): default off writes and prints nothing.
        extra = {}
        if a.verb == "start-gate":
            # State the refusal for the caller: the build does not start on this gate.
            try:
                extra = dict(verb="start-gate", mode=_json_arg(a.request).get("mode", "unknown"),
                             stated="why-net-off:start-refused")
            except (OSError, ValueError, AttributeError):
                extra = dict(verb="start-gate", stated="why-net-off:start-refused")
        print(ms.signal("WHY_REFUSED", reason="off", **extra))
        return EXIT_BUDGET
    try:
        return a.fn(a)
    except WhyRefusal as r:
        return _refused(r.reason)
    except Exception as e:  # fail-closed: nothing is confirmed on error
        print(ms.signal("WHY_ERROR", verb=a.verb, error=type(e).__name__))
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
