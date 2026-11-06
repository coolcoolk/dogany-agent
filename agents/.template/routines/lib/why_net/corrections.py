"""AC10a correction tray (v2.7 T1 piece P3, LOCKED spec 2.7 / 4.4 step 6).

  capture(prompt, ...)   UserPromptSubmit step 6: a strong-cue turn
                         (recorrection.routing, the nightly predicate)
                         -> one corrections.jsonl record (pattern detector);
                         a weak-cue turn waits for the nightly pass.
  nightly_pass(...)      derive's correction pass: owner turns of the last
                         window through the AC5 hybrid classifier. A C2 turn
                         already in the tray gets its message_uuid as a
                         separate backfill row; a new one becomes a record
                         (detector pattern:<cue> or model:C2).

Record (2.7): {"id","said_at","text","session_id","message_uuid",
"detector","dedupe","consumed_by","prev"}. dedupe = sha256(session_id \\0
text); a byte-identical repeat in one session counts once (known loss).

capture() runs only for a prompt `why-net ups` already admitted as an owner
turn (fresh bridge inbound naming this prompt, ledger row written; ups.run),
so an agent's own `why-net ups` call cannot put words in the tray.  The tray
never stores machine text: injected / recap / recall echoes and
machine-opened turns are dropped (is_machine_text: why_owner_turn
is_injected, the one definition the AC5 reader shares), and a detached worker's
prompt (why_owner_turn.DISPATCH_ENV_MARKER in env) is not an owner turn.

English/ASCII only. Stdlib only.
"""

import datetime
import glob
import hashlib
import importlib.util
import os
import re
import sys

from . import store

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import why_owner_turn  # noqa: E402

FILE = store.CORRECTIONS
DISPATCH_ENV = why_owner_turn.DISPATCH_ENV_MARKER
NIGHTLY_WINDOW_H = 26
NIGHTLY_MAX_MODEL_CALLS = 60


def dedupe_key(session_id, text):
    return hashlib.sha256(("%s\0%s" % (session_id or "", text)).encode(
        "utf-8")).hexdigest()


def _recorrection():
    import recorrection
    return recorrection


def is_machine_text(text):
    """True when a user-role text is machine-made or carries an injected
    block (echoed recap / recall / hook context)."""
    if not (text or "").strip():
        return True
    return why_owner_turn.is_injected(text)


def _is_owner_text(text):
    return not is_machine_text(text)


def capture(prompt, session_id, said_at=None, message_uuid=None, root=None,
            env=None, otl=None, transcript_path=None):
    """Step 6. -> the new record, or None (no hit, machine text, dispatch
    worker, or already in the tray).  AMEND-1 6.1: a record also keeps the
    owner turn (`otl`) and the work item it is about (`work`,
    turn_work_item), the A3 linking floor."""
    env = os.environ if env is None else env
    if env.get(DISPATCH_ENV):
        return None
    if not isinstance(prompt, str) or not _is_owner_text(prompt):
        return None
    # DGN-1967 bug 19: the nightly pass's predicate.  Only a strong cue is a
    # verdict without the model; a weak-cue turn is left to the nightly pass
    # (which skips keys already in the tray, so a live weak-cue record was
    # never re-judged).
    route, cue = _recorrection().routing(prompt)
    if route != "strong":
        return None
    key = dedupe_key(session_id, prompt)
    work = turn_work_item(prompt, transcript_path)
    said_at = said_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds")

    def build(rows):
        if any(r.get("dedupe") == key for r in rows if r.get("id")):
            return []
        return [{"id": store.next_id(rows, "cor", 4),
                 "said_at": said_at, "text": prompt,
                 "session_id": session_id, "message_uuid": message_uuid,
                 "detector": "pattern:" + cue, "dedupe": key,
                 "consumed_by": None, "otl": otl, "work": work}]

    new = store.append_many(FILE, build, root)
    return new[0] if new else None


# ---------------------------------------------------------------------------
# nightly pass
# ---------------------------------------------------------------------------

def owner_turns_with_ids(proj_dir, since_utc):
    """-> [(session_id, uuid, utc_ts, text, prev_agent)] owner turns in the
    window, via the AC5 reader (lib/owner_turns.py) plus the marker filter."""
    import owner_turns
    out = []
    for path in sorted(glob.glob(os.path.join(proj_dir, "*.jsonl"))):
        try:
            mtime = datetime.datetime.fromtimestamp(
                os.path.getmtime(path), datetime.timezone.utc)
        except OSError:
            continue
        if since_utc is not None and mtime < since_utc:
            continue
        turns, marker = owner_turns._session_turns(path)
        if len(turns) <= 1 and not marker:
            continue  # one-shot = detached worker / scheduled job
        sid = os.path.basename(path)[:-len(".jsonl")]
        for ts, uid, text, prev in turns:
            if since_utc is not None and ts < since_utc:
                continue
            if not _is_owner_text(text):
                continue
            out.append((sid, uid, ts, text, prev))
    out.sort(key=lambda r: r[2])
    return out


def _count_module():
    """routines/recorrection-count.py (masking + the one model call)."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "recorrection-count.py")
    spec = importlib.util.spec_from_file_location("_whynet_recorr_count", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def default_model():
    """-> (model_fn, mask_fn, prev_fn) from the AC5 counter, or None when the
    secret mask cannot load (then no text leaves the machine)."""
    try:
        cm = _count_module()
        redact = cm.load_redactor()
        if redact is None:
            return None
        return (cm.call_sonnet,
                lambda t: cm.mask_for_model(t, redact),
                lambda p: cm.prev_for_model(p, redact))
    except Exception:
        return None


def nightly_pass(proj_dir, root=None, now=None, window_h=NIGHTLY_WINDOW_H,
                 model=None, max_calls=NIGHTLY_MAX_MODEL_CALLS):
    """-> {"turns", "records", "backfills", "model_calls"}. model =
    (model_fn, mask_fn, prev_fn) or None (patterns only: uncertain turns
    are skipped, never counted)."""
    rc = _recorrection()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=window_h)
    turns = owner_turns_with_ids(proj_dir, since)
    stats = {"turns": len(turns), "records": 0, "backfills": 0,
             "model_calls": 0}
    calls = [0]

    def model_fn(prompt):
        calls[0] += 1
        return model[0](prompt)

    have = {r.get("dedupe") for r in store.read_rows(FILE, root)
            if r.get("id")}
    hits = []
    for sid, uid, ts, text, prev in turns:
        key = dedupe_key(sid, text)
        route, cue = rc.routing(text)
        if key in have:
            label = None  # already in the tray: backfill only, no model call
        elif route == "decided_n":
            label = "N"
        elif route == "strong":
            label = "C2"
        elif model is None or calls[0] >= max_calls:
            label = None
        else:
            label, cue, _called = rc.classify_hybrid(
                model[1](text), model[2](prev), model_fn)
        hits.append((sid, uid, ts, text, key, label, route, cue))

    def build(rows):
        recs = {r["dedupe"]: r for r in rows if r.get("id") and
                r.get("dedupe")}
        backfilled = {r.get("backfill") for r in rows if r.get("backfill")}
        new = []
        work_rows = list(rows)
        for sid, uid, ts, text, key, label, route, cue in hits:
            have = recs.get(key)
            if have is not None:
                # In the tray already (ups): only the uuid is missing.
                if uid and not have.get("message_uuid") and \
                        have["id"] not in backfilled:
                    new.append({"backfill": have["id"], "message_uuid": uid})
                    backfilled.add(have["id"])
                    stats["backfills"] += 1
                continue
            if label != "C2":
                continue
            rec = {"id": store.next_id(work_rows, "cor", 4),
                   "said_at": ts.astimezone().isoformat(timespec="seconds"),
                   "text": text, "session_id": sid, "message_uuid": uid,
                   "detector": ("pattern:" + cue) if route == "strong"
                   else "model:C2",
                   "dedupe": key, "consumed_by": None}
            new.append(rec)
            work_rows.append(rec)
            recs[key] = rec
            stats["records"] += 1
        return new

    store.append_many(FILE, build, root)
    stats["model_calls"] = calls[0]
    return stats


# ---------------------------------------------------------------------------
# T-corr (spec 4.1 / 4.4 step 6, second half)
# ---------------------------------------------------------------------------

_WORK_RX = re.compile(r"\b[Dd][Gg][Nn]-\d{1,7}\b")
# AMEND-1 2.1: a provisional node is not an open reading (N7) -- only acts
# move it, so a correction there never asks for words again.
OPEN_READING = ("hypothesis", "contested")


def turn_work_item(prompt, transcript_path=None):
    """The work item a correction turn is about: a DGN ticket the prompt
    names (first one), else the ticket this session last wrote
    (surfaces.touched_tickets).  -> canonical topic "DGN-N" or None."""
    from . import budget, surfaces
    m = _WORK_RX.search(prompt or "")
    if m:
        return budget.canonical_topic(m.group(0))
    if transcript_path:
        for p in surfaces.touched_tickets(transcript_path):
            topic = budget.canonical_topic(surfaces.ticket_id_of(p))
            if topic:
                return topic
    return None


def open_readings(work, nodes, root=None):
    """Open readings of a work item: live hypothesis / contested Whys
    proposed for it (`propose --work`), plus the ticket's own `why:` ids
    that are not confirmed.  AMEND-1 rev 3 (multiple Whys): a confirmed or
    provisional Why on the item is not a reading and is never asked about
    again, but it no longer closes the item -- another open Why on the same
    item may still be a second reason (it ripens per node, T-ripe /
    T-act-ripe).  The T-corr ask itself stays silent on an item with a
    confirmed or provisional Why (4.1 "never a trigger", unchanged).
    -> [why ids], oldest first."""
    from . import budget, surfaces
    root = root or store.root_dir()
    wdir = os.path.join(root, "worklog")
    path = surfaces.find_ticket(work, wdir)
    tw = surfaces.ticket_why(path, nodes, wdir) if path else {"whys": [], "state": "none"}
    if tw["state"] == "confirmed":
        return []  # some Why of the item is confirmed or provisional
    out = []
    for wid in sorted(nodes):
        n = nodes[wid]
        if n.get("status") in OPEN_READING and \
                budget.canonical_topic(n.get("work") or "") == work:
            out.append(wid)
    for wid in tw.get("whys") or []:
        if (nodes.get(wid) or {}).get("status") in OPEN_READING and wid not in out:
            out.append(wid)
    return out


def tcorr_enqueue(ctx, record, prompt, transcript_path=None, root=None):
    """Retired compatibility entry; correction capture never solicits Why."""
    return None
