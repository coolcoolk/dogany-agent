# test-lane: lib historical Why queue fixture writer, never a runtime producer
"""Seed pre-retirement rows to retain migration/answer/read-only coverage."""
from why_net import ask, budget, events, store


def historical_queue(ctx, kind, trigger, topic, cands, shape=None, work=None):
    before = store_bytes(ctx)
    if ask.enqueue(ctx, kind, trigger, topic, cands, shape=shape, work=work) != (None, "retired"):
        raise AssertionError("retired enqueue became a producer")
    if store_bytes(ctx) != before:
        raise AssertionError("retired enqueue wrote state")
    shape = shape or ask.default_shape(len(cands))
    with store.locked():
        known = events.nodes()
        shown, error = ask.build_slots(ctx, cands, shape, known)
        if error:
            raise AssertionError(error)
        aid = ask._new_ask_id()
        common = dict(kind=kind, trigger=trigger, shape=shape,
                      topic=budget.canonical_topic(topic), work_ref=work,
                      channel='morning', session_id=None, bubble_message_id=None,
                      shown=shown, refuse_reason=None, cand_texts=ask.cand_texts(cands, known))
        ask._write([ask._base(ctx, aid, 'create', **common), ask._base(ctx, aid, 'queued')])
        return aid, None


def historical_inline(ctx, argv):
    """Import a synthetic pre-FDGN-594 menu, never call a retired producer.

    v4 sections 1/2 retire independent asks; section 3.3 retains old evidence
    and its readers. Callers first exercise the real refusal, then use this
    fixture to test existing answer provenance, audits, acts and projections.
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("verb")
    for key in ("trigger", "shape", "topic", "work"):
        parser.add_argument("--" + key)
    parser.add_argument("--cand", action="append", default=[])
    parser.add_argument("--opt", action="append", default=[])
    parser.add_argument("--reveal", action="store_true")
    parser.add_argument("--urgent", action="store_true")
    a = parser.parse_args(argv)
    with store.locked():
        known = events.nodes()
        if a.reveal:
            shown = []
            for i, spec in enumerate(a.opt, 1):
                label, wid = spec.rsplit("=", 1)
                slot = dict(slot=i, why=wid or None, label=label)
                if not wid and label == ask.t(ctx, "why.ask.escape"):
                    slot["escape"] = True
                shown.append(slot)
            error = ask.reveal_error(shown, known)
            shape, trigger = "reveal", "reveal"
        else:
            shape, trigger = a.shape, a.trigger
            shown, error = ask.build_slots(ctx, a.cand, shape, known)
            error = error or ask.label_error(shown)
        if error:
            return (4 if a.reveal else 3), ask._signal(error) + "\n"
        aid = ask._new_ask_id()
        row = dict(kind="why", trigger=trigger, shape=shape,
                   topic=budget.canonical_topic(a.topic), work_ref=a.work,
                   channel="inline", session_id=ask.current_session(ctx),
                   bubble_message_id=None, shown=shown, refuse_reason=None,
                   cand_texts=ask.cand_texts(a.cand, known), issued_ts=ctx.now)
        text = "" if a.reveal else ask.render_with_gloss(
            ctx, shape, shown, known, trigger, row, locked=True)
        ask._write([ask._base(ctx, aid, "create", rendered=text, **row)])
        return 0, text + "\n" if text else ""


def store_bytes(ctx):
    """Check every existing state byte, including non-log state files."""
    from pathlib import Path
    base = Path(ctx.ddir)
    return {str(p.relative_to(base)): p.read_bytes()
            for p in base.rglob("*") if p.is_file()}


if __name__ == "__main__":
    # Standalone fixture importer used by the subprocess provenance harness.
    import json
    import sys
    rc, output = historical_inline(budget.Ctx(), sys.argv[1:])
    print(json.dumps(dict(rc=rc, output=output)))


def historical_frame(ctx, otl, cands, target=None, work=None, mode="infer",
                     scope="kit:kit_dev", stakes="normal", span=None, tf=None):
    """Import one historical turn menu for real answer/read/audit consumers."""
    from why_net import frame
    with store.locked():
        known = events.nodes()
        aid = ask._new_ask_id()
        shown, error = ask.turn_slots(ctx, cands, mode, known)
        if error:
            raise AssertionError(error)
        entries = [dict(text=known[c]["text"] if c in known else c,
            text_key=budget.text_key(known[c]["text"] if c in known else c),
            why=c if c in known else None, span=span) for c in cands]
        row = dict(kind="why", trigger="turn", shape=ask.default_shape(len(cands)),
            topic=work or budget.TGT_PREFIX + target, work_ref=work, channel="inline",
            session_id=ask.current_session(ctx), bubble_message_id=None, shown=shown,
            refuse_reason=None, cand_texts=[c["text"] for c in entries], issued_ts=ctx.now,
            frame_otl=otl, tf=tf, mode=mode, target=target, stakes=stakes, scope=scope,
            cands=entries, escalated_from=None, slot="why", asked_cands=list(cands),
            cs="cs-" + aid.split("-", 1)[1])
        text = ask.render(ctx, row["shape"], shown, known, "turn", row)
        ask._write([ask._base(ctx, aid, "create", rendered=text, **row)])
    frame._transition(ctx, dict(otl=otl, tf=tf, mode=mode, ask=aid,
                               disposition="asked", sid=row["session_id"]))
    return aid, text
