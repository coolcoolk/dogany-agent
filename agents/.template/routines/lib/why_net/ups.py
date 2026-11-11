"""why_net.ups -- UserPromptSubmit answer resolution (spec 4.4).

Writes the owner-turn ledger, owner-turns, probes, ask lifecycle and the tap
confirm.  When the retro slot answers the owner itself, injects a narrow
model-facing notice so the model does not start a competing settings flow;
any exception is logged and swallowed (exit 0). Step 6 (P3, AC10a): an owner turn that reads as a re-correction
goes to the corrections tray (corrections.capture), only after the gate
below admitted it. FDGN-594 retains that dated evidence without creating a
correction-triggered question.

Owner-turn gate (grill fixes 1, 2, 6, 11c).  A prompt is an owner turn only
when ALL hold, else ups does nothing at all (no ledger row, no probe, no ring
row, no ask closed):
  - not inside a dispatch worker (why_owner_turn.DISPATCH_ENV_MARKER);
  - no injection marker (why_owner_turn.is_injected: P1's union of the
    recap, memory-noise, bridge and Stop-hook lists) -- an injected or
    forwarded prompt that the inbound names still consumes its request_id
    (a `skip` row, grill-2 fix 7);
  - the bridge's inbound-context.json is fresh, carries a request_id no
    earlier ledger row used, is not marked `forwarded`, and its `text_sha`
    is this prompt's (grill-2 fixes 1, 7).  Workers, `claude -p` cron
    sessions and bridge-injected turns never get one.
  Every file here is agent-writable, so this gate alone proves nothing:
  the resolver also needs the turn on the transcript's main chain, and the
  nightly audit re-derives every ledger row from it (why_owner_turn
  check 6, audit_ledger).
Then one ledger row {sha256(NFC prompt), session_id, request_id, ..} is
appended; the resolver (why_owner_turn) accepts quotes only from ledgered
turns.

Answer resolution (grill fixes 3, 8):
  - a callback whose message_id is a morning bubble resolves to that ask
    only; other inline shapes never resolve by number ("No.N", "1, 3"):
    an inline Why ask passed the width gate so its keyboard never degrades
    (V-7), and a bare number may answer another [[OPTIONS]] list;
  - an inline ask is eligible only when the reply before this turn really
    carried it (ask_carried: one OPTIONS list, its labels, its printed
    question verbatim; then it is marked `shown`);
  - a tap resolves by the keyboard that carried it (tap_slot: the bridge's
    keyboard + option_index), a typed answer by exact label
    (grill-2 fix 6); an inline tap only on the bubble the bridge reports
    carried the ask (carrier_bubble, grill-3 fix 5), a morning tap only
    on an unedited bubble (bubble_text_sha, grill-3 fix 11);
  - nothing older than IGNORE_AFTER (24 h) is eligible, derive or not.
  - a confirm may restyle its stem and two labels while keeping its exact
    candidate; a typed numeric reply binds its verified inline slots.

AMEND-1: every probe stores the turn-ledger id (`otl`) of its turn; a
reveal pick also stores the menu's recommended label (`recommended`, from
decision-ledger.parse_menu over the carrying reply; None = unreadable ->
the A1 act is delegated, fail closed).  A pick on a Why ask makes the Why
provisional (events.confirm_tap); a pick on a split ask writes `resolve`
and runs the act pass at once (2.3); a pick on a fork-from-mixed ask
re-anchors the picked side.  Acts and settles say nothing (N8).

AMEND-1 rev 3: a "both" pick writes `cowhy` (or `resolve` side=both on a
split); a tap on a retro readings line records that reading as wrong and
queues its follow-up (why_net.retro); a pick on a follow-up becomes the
signal's reading.

DGN-1961: a verified owner turn is handed to the retro slot's cadence
proposal (`retro_slot.proposal_answer`): a tap on it, or its button label
typed, is the only thing that switches RETRO_SLOT_CADENCE -- even with the
net off.

AMEND-2 (DGN-2028 slice 3; trigger `turn` only, every other ask is
unchanged):
  - "another reason" (`other` slot) is a `free` tap; the ask goes
    `awaiting` (not declined) and the next typed owner turn binds to it;
  - a tap on the bubble that carried a turn ask binds whatever the previous
    owner turn was (carrier bubble, still IGNORE_AFTER) and proves `shown`;
  - a pick births the tapped candidate(s) in ONE lock section after the
    main one (`materialize`, U3): propose + confirm per slot, `cowhy` for
    "both"; the tap's turn is resolved before that lock (S-P1b).
  - P3.4: the retro is view-only -- a typed message no longer binds as a
    retro answer and no next retro item is requested.
"""

import json
import os
import re
import sys
import traceback

from . import WhyRefusal, budget, corrections, events, store
from . import nodes as N

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import why_owner_turn  # noqa: E402
import decision_ledger_fmt as fmt  # noqa: E402

RING_MAX = 50
# Grill-3 fix 5: the bridge's record of which bubble carried each
# [[OPTIONS]] keyboard it rendered (bridge/sdk_bridge publish_options_bubble).
OPTIONS_BUBBLES = "options-bubbles.json"
IGNORE_AFTER = 24 * 3600
ANNOY_TSV = os.path.join(_LIB, "why_annoyance_patterns.tsv")
ANNOY_SCOPES = ("any", "after-ask")

_N_BEON = re.compile("^(\\d+)\\s*\ubc88$")  # "N<beon>" number handle
_N_NO = re.compile(r"^No\.\s*(\d+)$", re.IGNORECASE)
_N_DOT = re.compile(r"^(\d+)\.\s*(.+)$")
_MULTI = re.compile("^\\d+\\s*(?:\ubc88)?(?:\\s*[,\uff0c]\\s*\\d+\\s*(?:\ubc88)?)+$")
_OPTIONS = re.compile(r"\[\[OPTIONS:(.*?)\]\]", re.DOTALL)


def _log(ctx, msg):
    try:
        d = os.path.join(ctx.root, "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "why-net.log"), "a", encoding="utf-8") as fh:
            fh.write("%s ups %s\n" % (store.iso_at(ctx.now), msg))
    except OSError:
        pass


def load_annoy_patterns(path=ANNOY_TSV):
    """[(pattern, scope)]: TSV `pattern<TAB>scope<TAB>note`; scope `any`
    fires on any owner turn, `after-ask` only on a turn that answers a Why
    ask (grill fix 11: "...sorry to bother you..." is not about asking)."""
    pats = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                scope = cols[1].strip() if len(cols) > 1 else "after-ask"
                if cols[0] and scope in ANNOY_SCOPES:
                    pats.append((cols[0], scope))
    except OSError:
        pass
    return pats


def read_inbound(ctx):
    """The bridge's per-turn inbound-context, or {} when absent / stale /
    without a request_id (decision_ledger_fmt's reader, shared with the
    decision ledger)."""
    return fmt.read_inbound(ctx.bot_dir, ctx.now)


def inbound_for(inbound, prompt):
    """True when the inbound-context names THIS prompt: the bridge's
    text_sha (sha256 of the NFC prompt it sent) equals the prompt's (grill-2
    fixes 1, 7).  A prompt the bridge did not send -- a cron session inside
    the freshness window, an agent's own `why-net ups` call with a made-up
    prompt -- never claims the owner's request_id.  An inbound without a
    text_sha (pre-fix bridge) binds nothing: fail closed."""
    sha = inbound.get("text_sha")
    return isinstance(sha, str) and sha == why_owner_turn.turn_sha(prompt)


def options_lists(text):
    """Every [[OPTIONS: a | b]] marker in an assistant text, as label lists."""
    return [[x.strip() for x in m.group(1).split("|")]
            for m in _OPTIONS.finditer(text or "")]


def confirm_shown(text, ask):
    """Restyled confirm copy keeps the candidate and the two slot meanings."""
    shown = ask.get("shown") or []
    candidates = ask.get("cand_texts") or []
    if ask.get("shape") != "confirm" or len(shown) != 2 \
            or not shown[1].get("wrong") or len(candidates) != 1:
        return None
    match = re.fullmatch(r"(.*?)\n\[\[OPTIONS:\s*([^\n]+)\]\]\s*", text or "", re.S)
    if not match or "[[OPTIONS" in match.group(1) or "```" in text \
            or not why_owner_turn.confirm_body(match.group(1), candidates[0]):
        return None
    labels = [s.strip() for s in match.group(2).split("|")]
    if len(labels) != 2:
        return None
    actual = [dict(s, label=label) for s, label in zip(shown, labels)]
    from .ask import label_error
    return actual if label_error(actual) is None else None


def ask_carried(text, ask):
    """Grill-2 fix 6: a reply carried an ask only when it holds exactly ONE
    [[OPTIONS]] list, that list is the ask's labels in order, and (for an
    `ask` that printed its copy) the whole rendered block -- question line,
    hint, marker -- appears verbatim.  The same labels under another
    question, or a second list reusing a label, carry nothing.  Grill-3
    fix 9: that block ends the reply and each label appears exactly once,
    inside the OPTIONS list. Confirm copy may restyle its stem and labels
    while retaining its verbatim candidate and two ordered slots."""
    if confirm_shown(text, ask) is not None:
        return True
    lists = options_lists(text)
    want = [store.nfc(s["label"]).strip() for s in ask.get("shown") or []]
    if not want or len(lists) != 1 or [store.nfc(x) for x in lists[0]] != want:
        return False
    if ask.get("trigger") == "reveal":
        return True  # the agent's own OPTIONS: no printed copy to match
    rendered = ask.get("rendered")
    if not isinstance(rendered, str):
        return False
    # Grill-3 fix 9: the printed block ENDS the reply, and no label appears
    # outside its OPTIONS list -- a line after the block, or one naming a
    # button ("pressing X means ..."), would redefine what the tap says.
    body, block = store.nfc(text or "").rstrip(), store.nfc(rendered).rstrip()
    if not body.endswith(block):
        return False
    # rev 3: the printed block itself may name a button (5f names "both");
    # only the agent's text before it is checked.
    rest = _OPTIONS.sub("", body[:len(body) - len(block)])
    return not any(lab in rest for lab in want)


def _button_forms(label, i, numeric_ok):
    forms = {store.nfc(label), store.nfc("%d. %s" % (i, label))}
    if numeric_ok:
        forms.update({"%d\ubc88" % i, "No.%d" % i})
    return forms


def tap_slot(prompt, inbound, shown, numeric_ok):
    """Grill-2 fix 6: a tap binds to the keyboard that carried it, as the
    bridge saw it (inbound `keyboard` + `option_index`), never to label text
    alone.  The keyboard must be this ask's, button for button, and the
    prompt the tapped button's text.  Returns (verdict, slots) or None."""
    kb, idx = inbound.get("keyboard"), inbound.get("option_index")
    if not isinstance(kb, list) or isinstance(idx, bool) or not isinstance(idx, int):
        return None
    if len(kb) != len(shown) or not 1 <= idx <= len(kb):
        return None
    for i, (button, s) in enumerate(zip(kb, shown), 1):
        if not isinstance(button, str) or store.nfc(button) not in _button_forms(
                s["label"], i, numeric_ok):
            return None
    if store.nfc(prompt).strip() != store.nfc(kb[idx - 1]).strip():
        return None
    s = shown[idx - 1]
    if s.get("other"):
        return "free", [s["slot"]]   # AMEND-2 2.7: "another reason" (F8)
    return ("defer" if s.get("escape") else "pick"), [s["slot"]]


def carrier_bubble(ctx, ask, chat_id):
    """Grill-3 fix 5: the message id of the bubble that carried an inline
    ask's keyboard -- the first bubble the bridge rendered in this chat at
    or after the ask was issued whose buttons are the ask's.  An older
    bubble with the same labels asked another question.  None when the
    bridge recorded no such bubble (fail closed: the tap binds nothing)."""
    since = ask.get("issued_ts")
    if not isinstance(since, (int, float)) or isinstance(since, bool):
        since = ask.get("sent_at")
    if chat_id is None or since is None:
        return None
    try:
        with open(os.path.join(ctx.bot_dir, OPTIONS_BUBBLES), encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return None
    shown = ask.get("shown") or []
    best = None
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("chat_id") != chat_id:
            continue
        if r.get("carried_from") is not None:
            continue  # DGN-1958: a re-render; its origin carried the ask
        ts, kb = r.get("ts"), r.get("keyboard")
        if not isinstance(ts, (int, float)) or ts < since:
            continue
        if not isinstance(kb, list) or len(kb) != len(shown):
            continue
        if not all(isinstance(b, str) and store.nfc(b) in _button_forms(s["label"], i, False)
                   for i, (b, s) in enumerate(zip(kb, shown), 1)):
            continue
        if best is None or ts < best[0]:
            best = (ts, r.get("message_id"))
    return best[1] if best else None


def bubble_origin(ctx, chat_id, message_id):
    """DGN-1958: the bubble an open decision was first sent on.  The bridge
    re-renders an unanswered menu below newer replies (a carried bubble,
    `carried_from` = the original's message id), so a tap on the carried
    bubble answers the ask its origin carried.  Any other id is its own
    origin."""
    try:
        with open(os.path.join(ctx.bot_dir, OPTIONS_BUBBLES), encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return message_id
    for r in rows if isinstance(rows, list) else []:
        if (isinstance(r, dict) and r.get("chat_id") == chat_id
                and r.get("message_id") == message_id
                and isinstance(r.get("carried_from"), int)):
            return r["carried_from"]
    return message_id


def _assistant_texts(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"]
    return []


def last_assistant_text(path, prompt):
    """The assistant reply right before this owner prompt (V-1 safe: works
    whether or not the prompt's own entry is in the transcript yet)."""
    if not path:
        return None
    cur, closed, last_user = [], None, None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                if e.get("type") == "assistant":
                    cur.extend(_assistant_texts(e))
                    continue
                text = why_owner_turn.entry_text(e)
                if text is None:
                    continue  # tool results etc. live inside a reply
                closed, cur, last_user = cur, [], text
    except (OSError, UnicodeDecodeError):
        return None
    if last_user is not None and store.nfc(last_user) == store.nfc(prompt) and not cur:
        return "\n".join(closed or [])
    return "\n".join(cur)


def match_slots(prompt, shown, numeric_ok):
    """Returns (verdict, slots) with verdict in pick/pick-multi/defer/free.
    Exact label or "N. label" always; number handles only when numeric_ok
    (a morning bubble matched by its message id)."""
    p = store.nfc(prompt).strip()
    by_slot = {s["slot"]: s for s in shown}
    for s in shown:
        if p == store.nfc(s["label"]):
            return ("defer" if s.get("escape") else "pick"), [s["slot"]]
    m = _N_DOT.match(p)
    if m and int(m.group(1)) in by_slot \
            and m.group(2).strip() == store.nfc(by_slot[int(m.group(1))]["label"]):
        s = by_slot[int(m.group(1))]
        return ("defer" if s.get("escape") else "pick"), [s["slot"]]
    if not numeric_ok:
        return "free", []
    m = _N_BEON.match(p) or _N_NO.match(p)
    if m and int(m.group(1)) in by_slot:
        s = by_slot[int(m.group(1))]
        return ("defer" if s.get("escape") else "pick"), [s["slot"]]
    if _MULTI.match(p):
        nums = [int(x) for x in re.findall(r"\d+", p)]
        if nums and all(n in by_slot and not by_slot[n].get("escape") for n in nums):
            return "pick-multi", sorted(set(nums))
    return "free", []


def update_ring(ctx, session_id, prompt, is_answer, source):
    rows = store.read_rows(store.OWNER_TURNS)
    words = budget.rush_words_in(prompt)
    p = prompt.strip()
    if (source != "callback" and not is_answer and len(p) <= budget.SHORT_LEN
            and not p.endswith(store.QUESTION_MARKS)):
        words.append(budget.SHORT_IMPERATIVE)
    rows.append({"ts": store.iso_at(ctx.now), "session_id": session_id,
                 "len": len(prompt), "rush_words": words})
    store.write_lines_atomic(store.OWNER_TURNS, rows[-RING_MAX:])


def _fresh(ctx, at):
    return at is not None and ctx.now - at <= IGNORE_AFTER


def eligible_ask(ctx, asks, session_id, inbound, prev_ts, prev_any=None):
    """(ask, via) or (None, None).  via = "bubble" | "inline" | "carrier" |
    "awaiting".

    AMEND-2 2.7 (trigger `turn` only): a typed owner turn right after an
    "another reason" tap binds the ask that waits for it (`awaiting`), and
    a tap on the bubble that carried a turn ask binds it whatever the
    previous owner turn was (`carrier`, F15)."""
    if inbound.get("source") != "callback":
        aw = None
        for a in asks.values():
            if a.get("trigger") != "turn" or a.get("state") != "awaiting" \
                    or a.get("session_id") != session_id:
                continue
            at = a.get("awaiting_at")
            if not _fresh(ctx, at) or (prev_ts is not None and at < prev_ts):
                continue
            if aw is None or (at, a["ask"]) > (aw["awaiting_at"], aw["ask"]):
                aw = a
        if aw is not None:
            return aw, "awaiting"
    if inbound.get("source") == "callback" and inbound.get("message_id") is not None:
        mid = bubble_origin(ctx, inbound.get("chat_id"), inbound["message_id"])
        best = None
        for a in asks.values():
            if a.get("trigger") != "turn" or a.get("session_id") != session_id:
                continue
            if a.get("state") not in ("create", "shown") or a.get("sent_at") is None \
                    or not _fresh(ctx, a["sent_at"]):
                continue
            if carrier_bubble(ctx, a, inbound.get("chat_id")) != mid:
                continue
            if best is None or (a["sent_at"], a["ask"]) > (best["sent_at"], best["ask"]):
                best = a
        if best is not None:
            return best, "carrier"
    if inbound.get("source") == "callback" and inbound.get("message_id") is not None:
        for a in asks.values():
            if a.get("channel") == "morning" and a.get("bubble_message_id") == inbound["message_id"]:
                # grill-2 N14: a message id is unique per chat only; grill-3
                # fix 11: a bubble with no recorded chat id binds nothing
                if a.get("bubble_chat_id") is None \
                        or inbound.get("chat_id") != a["bubble_chat_id"]:
                    return None, None
                # grill-3 fix 11: the bubble as tapped must be the bubble as
                # sent (push.sh --edit can reword it under the same keyboard)
                if not a.get("bubble_text_sha") \
                        or inbound.get("bubble_text_sha") != a["bubble_text_sha"]:
                    return None, None
                # a stale, repeated or expired tap on a bubble resolves to nothing
                if a.get("state") == "shown" and _fresh(ctx, a.get("shown_at")):
                    return a, "bubble"
                return None, None
    best = None
    for a in asks.values():
        if a.get("channel") != "inline" or a.get("session_id") != session_id:
            continue
        if a.get("state") not in ("create", "shown") or a.get("sent_at") is None:
            continue
        if prev_ts is not None and a["sent_at"] < prev_ts:
            continue
        if not _fresh(ctx, a["sent_at"]):
            continue
        if best is None or (a["sent_at"], a["ask"]) > (best["sent_at"], best["ask"]):
            best = a
    if best:
        return best, "inline"
    # AMEND-2 P3.4 (MD 9): the retro is a view-only picture; a typed
    # message no longer binds as the answer to a retro bubble.
    return None, None


def keyboard(ask):
    """The slots as the owner saw them: a retro bubble carries its stop
    button too (retro.with_stop, recorded at `retro-shown`)."""
    return ask.get("retro_shown") or ask.get("shown") or []


def turn_source(inbound):
    """The ledger source of an owner turn: voice, else the bridge's source
    (message / callback / ...)."""
    return "voice" if inbound.get("voice") else (inbound.get("source") or "message")


def device_tap_question(ctx, inbound):
    """Snapshot a verified device tap into the durable owner ledger.

    The newest keyboard in this chat must be the exact, unedited bubble
    tapped, with an explicitly approving slot. Never follow carry pointers.
    """
    mid, chat = inbound.get("message_id"), inbound.get("chat_id")
    idx, kb = inbound.get("option_index"), inbound.get("keyboard")
    if (inbound.get("source") != "callback" or chat is None
            or not isinstance(mid, int) or isinstance(mid, bool)
            or not isinstance(idx, int) or isinstance(idx, bool)
            or not isinstance(kb, list) or not 1 <= idx <= len(kb)):
        return None
    try:
        with open(os.path.join(ctx.bot_dir, OPTIONS_BUBBLES), encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if not isinstance(row, dict) or row.get("chat_id") != chat:
            continue
        approval = row.get("device_approval")
        if (row.get("message_id") != mid or row.get("carried_from") is not None
                or row.get("keyboard") != kb or not isinstance(approval, dict)
                or approval.get("approve_indices") != [idx]
                or not isinstance(approval.get("bubble_text_sha"), str)
                or approval["bubble_text_sha"] != inbound.get("bubble_text_sha")):
            return None
        question = approval.get("question")
        return question if isinstance(question, str) and question else None
    return None


def _ledger_row(ctx, session_id, prompt, inbound, payload, owner, src):
    """`prompt` is the full text the bridge sent; `owner` the owner's own
    region of it.  When they differ (an outbound-context block, DGN-1967
    bug 2) the row also carries owner_sha, so a probe that keeps only the
    owner's words still resolves to this turn.  `src`: turn_source()."""
    def build(rows):
        if any(r.get("request_id") == inbound["request_id"] for r in rows):
            raise _Reused()
        row = {"id": store.next_id(rows, "otl", 6), "at": store.iso_at(ctx.now),
                "session_id": session_id, "sha": why_owner_turn.turn_sha(prompt),
                "len": len(prompt), "request_id": inbound["request_id"],
                "source": src,
                "chat_id": inbound.get("chat_id"),
                "message_id": inbound.get("message_id"),
                "text_sha": inbound.get("text_sha"),
                "transcript_path": payload.get("transcript_path")}
        if payload.get("owner_delivery") == "inflight":
            row["owner_delivery"] = "inflight"
        if src == "callback":
            for key in ("keyboard", "option_index", "bubble_text_sha", "callback_data"):
                if key in inbound:
                    row[key] = inbound[key]
            question = device_tap_question(ctx, inbound)
            if question is not None:
                row["device_question"] = question
        if owner is not None and owner != prompt:
            row["owner_sha"] = why_owner_turn.turn_sha(owner)
        # The live bridge supplies authenticated Telegram custody before UPS.
        # Preserve it in the existing chained ledger, not a parallel store.
        carrier = inbound.get("why_carrier")
        if ctx.enabled() and isinstance(carrier, dict) and src == "message":
            words = why_owner_turn.owner_words(owner)
            row["telegram_owner"] = dict(carrier, owner_text=words,
                owner_hash=why_owner_turn.turn_sha(words))
            # Redelivery of one native message retains its original arrival;
            # replaying an early reply after an ACK cannot make it approval.
            previous = next((r["telegram_owner"] for r in rows
                if r.get("telegram_owner") and r.get("chat_id") == row["chat_id"]
                and r.get("message_id") == row["message_id"]), None)
            if previous is not None:
                if (previous.get("owner_text") != words or previous.get("reply_to_carrier_id")
                        != carrier.get("reply_to_carrier_id")):
                    raise ValueError("owner-carrier-identity-changed")
                row["telegram_owner"] = dict(previous)
        return row
    return store.append_unlocked(store.TURN_LEDGER, build)


class _Reused(Exception):
    pass


def _consume(ctx, session_id, inbound, reason):
    """A ledger row that only uses up a request_id (`skip`: no sha, never an
    owner turn; why_owner_turn.ledger_rows drops it)."""
    def build(rows):
        if any(r.get("request_id") == inbound["request_id"] for r in rows):
            raise _Reused()
        return {"id": store.next_id(rows, "otl", 6), "at": store.iso_at(ctx.now),
                "session_id": session_id, "skip": reason,
                "request_id": inbound["request_id"],
                "chat_id": inbound.get("chat_id"),
                "message_id": inbound.get("message_id")}
    try:
        store.append(store.TURN_LEDGER, build)
    except _Reused:
        pass


# r3 B1: the owner-turn ledger is a shared primitive of the v2.7 user-yes
# sentinel (D3 bash gate, D4 write paths, the script devices), not a Why-net
# feature.  With WHY_NET off, ups still writes the ledger row (and the `skip`
# row) whenever a D3 / D4 hook line runs in detect or block, and does
# nothing else.
SENTINEL_HOOKS = (("bash-safety-gate.py", "BASH_GATE_MODE"),
                  ("write-paths-gate.py", "WRITE_PATHS_D4_MODE"))


def _hook_commands(node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str):
            yield cmd
        for v in node.values():
            for c in _hook_commands(v):
                yield c
    elif isinstance(node, list):
        for v in node:
            for c in _hook_commands(v):
                yield c


def sentinel_active(ctx):
    """True when this instance's settings run D3 or D4 in detect or block.
    A hook line without its mode variable runs detect (the gates' default).
    Unreadable settings count as active: a ledger row only records an owner
    turn, it authorizes nothing by itself."""
    path = os.path.join(ctx.root, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            hooks = (json.load(fh) or {}).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return True
    for cmd in _hook_commands(hooks.get("PreToolUse") or []):
        for script, var in SENTINEL_HOOKS:
            if script not in cmd:
                continue
            m = re.search(r"\b%s=(\S+)" % var, cmd)
            if (m.group(1) if m else "detect").strip("'\"").lower() != "off":
                return True
    return False


def run(ctx, payload):
    prompt = payload.get("prompt")
    session_id = payload.get("session_id")
    if not isinstance(prompt, str) or not isinstance(session_id, str) or not session_id:
        return
    why_on = ctx.enabled()
    import retro_slot
    slot_ask = retro_slot.proposal_open(ctx)
    link_review = retro_slot.link_review_open(ctx)
    owner_prompt = why_owner_turn.strip_outbound(prompt)
    missed_start = owner_prompt.strip() == (
        retro_slot.t(ctx, "retro.slot.missed.start") or "").strip()
    if not why_on and not slot_ask and not link_review and not missed_start and not sentinel_active(ctx):
        return
    if os.environ.get(why_owner_turn.DISPATCH_ENV_MARKER):
        return
    # DGN-1967 bug 2: the pushes the bridge prefixes to the owner's turn are
    # the agent's words.  `full` (the text the bridge sent) only binds the
    # turn: inbound text_sha, the ledger row's sha, the transcript entry.
    full, prompt = prompt, owner_prompt
    inbound = read_inbound(ctx)
    if not inbound or not inbound_for(inbound, full):
        return  # no owner inbound behind this prompt (cron / worker / injected)
    skip = ("injected" if why_owner_turn.is_injected(prompt)
            else "forwarded" if inbound.get("forwarded") else None)
    if skip:
        # Grill-2 fix 7: a skipped owner turn still USES its request_id, so
        # nothing else claims it inside the freshness window.
        _consume(ctx, session_id, inbound, skip)
        _log(ctx, "%s turn skipped request_id=%s" % (skip, inbound["request_id"]))
        return
    if missed_start:
        try:
            retro_slot.missed_answer(ctx, prompt)
        except Exception:  # noqa: BLE001 -- the slot never breaks the hook
            _log(ctx, "retro missed start: " + traceback.format_exc().replace("\n", " | "))
    if slot_ask:
        # DGN-1961: the retro slot's cadence proposal binds only to a
        # verified owner turn (this gate); the slot owns the conf write.
        try:
            retro_slot.proposal_answer(ctx, inbound, prompt)
        except Exception:  # noqa: BLE001 -- the slot never breaks the hook
            _log(ctx, "retro proposal: " + traceback.format_exc().replace("\n", " | "))
        if not why_on and not sentinel_active(ctx):
            return
    if link_review:
        try:
            retro_slot.link_review_answer(ctx, inbound, prompt)
        except Exception:  # noqa: BLE001 -- the review never breaks the hook
            _log(ctx, "retro link review: " + traceback.format_exc().replace("\n", " | "))
    # Grill-2 fix 4: a Whisper transcript is not the owner's typed words.
    # It is ledgered as source=voice: it may seed a hypothesis, never a
    # confirm (no quote confirm, no pick confirm; a tap is never voice).
    src = turn_source(inbound)
    if not why_on:
        try:
            with store.locked():
                _ledger_row(ctx, session_id, full, inbound, payload, prompt, src)
        except _Reused:
            pass
        return  # Why-off records the owner turn without consuming its question.
    picks = []
    split = None
    turn_pick = None
    co = None
    wrong = []
    reading = None
    ask = None
    text = None
    tpath = payload.get("transcript_path") or why_owner_turn.transcript_path(session_id)
    with store.locked():
        try:
            lrow = _ledger_row(ctx, session_id, full, inbound, payload, prompt, src)
        except _Reused:
            return
        turns = store.read_rows(store.OWNER_TURNS)
        prev_ts = None
        for t in reversed(turns):
            if t.get("session_id") == session_id:
                prev_ts = store.parse_iso(t.get("ts"))
                break
        prev_any = store.parse_iso(turns[-1].get("ts")) if turns else None
        asks = budget.fold_asks(store.read_rows(store.ASKS))
        ask, via = eligible_ask(ctx, asks, session_id, inbound, prev_ts, prev_any)
        if ask is not None and via == "carrier" and ask.get("state") == "create":
            # F15: the carrier bubble proves the ask was shown (the last
            # reply may be the one to N+1, so ask_carried is not consulted)
            store.append_row_unlocked(store.ASKS, {
                "ask": ask["ask"], "ev": "shown", "at": store.iso_at(ask["sent_at"]),
                "verified_at": store.iso_at(ctx.now), "by": "ups-carrier"})
        if ask is not None and via == "inline":
            # the reply before this turn, read once: the reveal's
            # recommended label below reads the same text
            text = last_assistant_text(tpath, full)
            if not ask_carried(text, ask):
                ask = None  # printed, never sent: derive settles it (E14)
            else:
                actual = confirm_shown(text, ask)
                if actual is not None:
                    ask = dict(ask, shown=actual, rendered=text.rstrip())
                if ask.get("state") == "create":
                    store.append_row_unlocked(store.ASKS, {
                        "ask": ask["ask"], "ev": "shown",
                        "at": store.iso_at(ask["sent_at"]), "shown": ask["shown"],
                        "rendered": ask.get("rendered"),
                        "verified_at": store.iso_at(ctx.now), "by": "ups"})
        if ask is not None and via == "inline" and src == "callback":
            mid = inbound.get("message_id")
            if mid is not None:
                mid = bubble_origin(ctx, inbound.get("chat_id"), mid)
            if mid is None or mid != carrier_bubble(ctx, ask, inbound.get("chat_id")):
                ask = None  # grill-3 fix 5: a tap on another bubble
        if ask is not None and via == "awaiting":
            verdict, slots = "free", []   # the typed reason after "another reason"
        elif ask is not None and src == "callback":
            verdict, slots = tap_slot(prompt, inbound, keyboard(ask),
                                      via == "bubble") or ("free", [])
        elif ask is not None:
            verdict, slots = match_slots(prompt, keyboard(ask), False)
            if via == "inline" and src == "message" and confirm_shown(text, ask) is not None:
                n = why_owner_turn.numeric_slot(prompt, len(keyboard(ask)))
                if n is not None:
                    s = keyboard(ask)[n - 1]
                    verdict, slots = ("defer" if s.get("escape") else "pick"), [s["slot"]]
        else:
            verdict, slots = "none", []
        if ask is not None and ask.get("trigger") == "turn" and verdict == "pick" \
                and any(s.get("other") for s in keyboard(ask) if s["slot"] in slots):
            verdict = "free"   # the "another reason" label typed: as its tap
        if ask is not None and any(s.get("stop") for s in keyboard(ask)
                                   if s["slot"] in slots):
            verdict = "stop"   # final grill B3: "that's all for today"
        update_ring(ctx, session_id, prompt,
                    verdict in ("pick", "pick-multi", "defer", "stop"), src)
        if verdict == "stop":
            from . import retro
            retro.stop(ctx, ask)
        elif ask is not None:
            rec = None
            if ask.get("trigger") == "reveal":
                rec = recommended_label(text)
            outcome = None
            wrong_reading = verdict == "defer" and src != "voice" and any(
                s.get("wrong") for s in ask.get("shown") or [] if s["slot"] in slots)
            if wrong_reading:
                outcome = "answered"  # a correction must not park its follow-up for seven days
            if ask.get("trigger") == "turn" and verdict == "free" and any(
                    s.get("other") for s in keyboard(ask) if s["slot"] in slots):
                outcome = "awaiting"   # F8: the ask waits for the typed reason
            probe = _record_probe(ctx, ask, verdict, slots, prompt, session_id, src,
                                  lrow.get("id"), rec, outcome)
            if probe and verdict in ("pick", "pick-multi") and ask.get("trigger") == "turn" \
                    and src != "voice":
                turn_pick = (ask, probe, slots)
            elif probe and verdict in ("pick", "pick-multi") and ask.get("trigger") != "reveal" \
                    and src != "voice":
                by_slot = {s["slot"]: s for s in ask.get("shown") or []}
                picks = [(by_slot[n]["why"], probe["id"]) for n in slots
                         if (by_slot.get(n) or {}).get("why")]
                # rev 3: "both" is a first-class answer (co-Whys)
                both = next((by_slot[n]["both"] for n in slots
                             if (by_slot.get(n) or {}).get("both")), None)
                if both:
                    co = (both, probe)
                    picks = []
                if ask.get("trigger") == "act-contest":
                    split = (ask, probe, slots)
                    picks, co = [], None
                if ask.get("trigger") == "readings":
                    # a tap on a retro readings line = "this reading is wrong"
                    wrong = [(by_slot[n].get("signal"), by_slot[n].get("reading"), probe["id"])
                             for n in slots if (by_slot.get(n) or {}).get("signal")]
                    picks, co = [], None
                if ask.get("trigger") == "reading":
                    reading = (ask, [w for w, _ in picks] or list(both or []))
            elif probe and wrong_reading:
                whys = [s["why"] for s in ask["shown"] if s.get("why")]
                wrong = [(ask.get("signal") or ask["ask"], whys, probe["id"])]
        annoy = _annoy_match(prompt, ask is not None)
        if annoy:
            newest = max((a for a in asks.values() if a.get("sent_at") is not None),
                         key=lambda a: (a["sent_at"], a["ask"]), default=None)
            store.append_row_unlocked(store.PROBES, {
                "annoy": newest["ask"] if newest else None, "pattern": annoy,
                "session_id": session_id, "at": store.iso_at(ctx.now)})
    # Q1 as amended (1.3): a pick on a shown candidate makes it provisional
    # through P1's floor writer; a split pick resolves the act-contest.
    if split is not None:
        try:
            _split_answer(ctx, *split)
        except WhyRefusal as r:
            _log(ctx, "resolve refused ask=%s reason=%s" % (split[0]["ask"], r.reason))
    if turn_pick is not None:
        materialize(ctx, *turn_pick)   # AMEND-2 2.6 U3 (trigger turn only)
    if co is not None:
        (w1, w2), probe = co
        try:
            with store.locked():
                events.co_whys(w1, w2, probe["id"], probe.get("otl"))
        except WhyRefusal as r:
            _log(ctx, "cowhy refused ask=%s reason=%s" % (ask["ask"], r.reason))
    from . import retro
    for sig, whys, pid in wrong:
        retro.on_wrong(ctx, sig, whys or [], pid)
    if reading is not None:
        retro.on_reading_answer(ctx, *reading)
    known = events.nodes()
    mixed = ask is not None and ask.get("trigger") == "fork-mixed"
    for wid, pid in picks:
        node = known.get(wid)
        if not node or node["status"] in N.TERMINAL:
            continue
        if node["status"] in N.LIVE_CONFIRMED and not mixed:
            continue
        try:
            events.confirm_tap(wid, pid, reanchor=mixed)
        except WhyRefusal as r:
            _log(ctx, "confirm_tap refused why=%s probe=%s reason=%s" % (wid, pid, r.reason))
    if annoy:
        budget.tune(ctx, annoy_now=True)
    # Step 6 (P3, AC10a): past the owner-turn gate only, so the tray holds
    # what the owner sent, never an agent's own `why-net ups` call.
    try:
        # AMEND-1 6.1: the record keeps its work item and owner turn, the
        # A3 linking floor (`why-net act --kind correction`).
        rec = corrections.capture(prompt, session_id, said_at=store.iso_at(ctx.now),
                                  otl=lrow.get("id"), transcript_path=tpath)
        # FDGN-594: retain dated correction evidence without asking.
    except Exception:  # noqa: BLE001 -- the tray never breaks the hook
        _log(ctx, "corrections capture: " + traceback.format_exc().replace("\n", " | "))
    return


_DL = []


def _decision_ledger():
    """routines/decision-ledger.py (the DGN-1952 menu parser), loaded once."""
    if not _DL:
        import importlib.util
        path = os.path.join(os.path.dirname(_LIB), "decision-ledger.py")
        spec = importlib.util.spec_from_file_location("_why_ups_decision_ledger", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DL.append(mod)
    return _DL[0]


_NUM_PREFIX = re.compile(r"^\s*\d+\.\s*")


def recommended_label(text):
    """AMEND-1 1.2 delegated slot: the recommended option label of the menu
    the reply carried; "" when the menu names none; None when the reply
    cannot be read or parsed (fail closed: the A1 is delegated)."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        menu = _decision_ledger().parse_menu(text)
    except Exception:  # noqa: BLE001 -- unreadable = delegated
        return None
    if not menu:
        return None
    return _NUM_PREFIX.sub("", menu.get("recommended") or "").strip()


def _split_answer(ctx, ask, probe, slots):
    """A pick on an act-contest split ask -> resolve (2.3), then the act
    pass so the acted side may settle in the same pass."""
    from . import acts
    by_slot = {s["slot"]: s for s in ask.get("shown") or []}
    s = by_slot.get(slots[0]) if slots else None
    if not s or s.get("escape") or s.get("side") not in ("said", "acted", "both"):
        return
    said = next((x["why"] for x in by_slot.values() if x.get("side") == "said"), None)
    acted = next((x["why"] for x in by_slot.values() if x.get("side") == "acted"), None)
    if not said or not acted:
        return
    with store.locked():
        evs = store.read_rows(store.EVENTS)
        contest_evt, carry = acts.contest_carry(evs, said, acts.act_rows())
        events.resolve(said, s["side"], acted, contest_evt, probe.get("otl"),
                       carry_from=carry, probe=probe["id"])
    acts.act_pass(ctx)


def _annoy_match(prompt, answers_ask):
    p = store.nfc(prompt)
    for pat, scope in load_annoy_patterns():
        if scope == "after-ask" and not answers_ask:
            continue
        if store.nfc(pat) in p:
            return pat
    return None


def _record_probe(ctx, ask, verdict, slots, prompt, session_id, src, otl=None,
                  recommended=None, outcome=None):
    outcome = outcome or ("declined" if verdict == "defer" else "answered")
    bubble = ask.get("bubble_message_id") if ask.get("channel") == "morning" else None
    fields = {
        "at": store.iso_at(ctx.now), "ask": ask["ask"],
        "trigger": ask.get("trigger"), "work_ref": ask.get("work_ref"),
        "shown": ask.get("shown"), "verdict": verdict, "slots": slots,
        "verbatim": prompt,
        "source": {"runtime": why_owner_turn.RUNTIME, "session_id": session_id,
                   "message_uuid": None, "inbound_source": src,
                   "bubble_message_id": bubble},
        "outcome": outcome, "otl": otl, "recommended": recommended,
    }
    row, created = events.write_probe_unlocked(fields)
    if not created:
        return None  # double-tap redelivery: the first row stands (GRILL h)
    store.append_row_unlocked(store.ASKS, {"ask": ask["ask"], "ev": outcome,
                                           "at": store.iso_at(ctx.now)})
    return row


def _cand_slots(ask):
    """slot number -> the create row's candidate entry (turn_slots order)."""
    plain = [s for s in ask.get("shown") or [] if not (s.get("both") or s.get("both_slots")
                                                      or s.get("other") or s.get("escape"))]
    return {s["slot"]: c for s, c in zip(plain, ask.get("cands") or [])}


def materialize(ctx, ask, probe, slots):
    """AMEND-2 2.6 U3 (trigger `turn` only): a tap or "both" on a turn ask.
    S-P1b: the probe's turn is resolved BEFORE the lock (resolve_prompt is a
    transcript read); then one `store.locked()` re-checks the probe and, per
    answered slot, proposes the born node (text slot; reused when already
    born, F16) and confirms it (confirm_tap_unlocked), then writes `cowhy`
    for "both".  A pick-mode slot names its node, which is confirmed as
    today.  A refused check writes nothing: no node is born without its
    confirm.  Refusals are logged as today."""
    src = probe.get("source") or {}
    ref = why_owner_turn.resolve_prompt(probe.get("verbatim"), src.get("session_id"))
    if isinstance(ref, why_owner_turn.Refusal):
        _log(ctx, "turn pick refused ask=%s probe=%s reason=%s"
             % (ask["ask"], probe["id"], ref.reason))
        return
    by_slot = {s["slot"]: s for s in ask.get("shown") or []}
    picked = sorted(events.picked_slots(dict(probe, shown=ask.get("shown"))))
    cands = _cand_slots(ask)
    pair = next((list(by_slot[n]["both"]) for n in slots
                 if (by_slot.get(n) or {}).get("both")), None)
    both_born = any((by_slot.get(n) or {}).get("both_slots") for n in slots)
    try:
        with store.locked():
            pr = events._probe(store.read_rows(store.PROBES), probe["id"])
            if pr.get("verdict") not in ("pick", "pick-multi") \
                    or (pr.get("source") or {}).get("inbound_source") == "voice":
                return
            wids = []
            for n in picked:
                s = by_slot.get(n) or {}
                if pair is not None or s.get("both_slots") or s.get("both") \
                        or s.get("other") or s.get("escape"):
                    continue
                wid = s.get("why")
                if wid is None:
                    c = cands.get(n) or {}
                    ev = [{"otl": ask.get("frame_otl"), "role": "frame",
                           "span": c.get("span")},
                          {"otl": pr.get("otl"), "role": "answer", "span": None}]
                    born = {"ask": ask["ask"], "cs": ask.get("cs"), "slot": n,
                            "probe": pr["id"]}
                    wid = events.propose_unlocked(
                        c.get("text") or s.get("label"), ask.get("scope") or "framework",
                        (), ask.get("work_ref"), src.get("session_id"),
                        evidence=ev, target=ask.get("target"), born=born)["why"]
                wids.append(wid)
                node = events.N.fold(store.read_rows(store.EVENTS)).get(wid) or {}
                if node.get("status") in N.TERMINAL or node.get("status") in N.LIVE_CONFIRMED:
                    continue
                try:
                    events.confirm_tap_unlocked(wid, pr["id"], ref=ref)
                except WhyRefusal as r:
                    _log(ctx, "confirm_tap refused why=%s probe=%s reason=%s"
                         % (wid, pr["id"], r.reason))
            if both_born and len(wids) == 2:
                pair = wids
            if pair is not None and len(pair) == 2 and not any(
                    e.get("ev") == "cowhy" and (e.get("data") or {}).get("probe") == pr["id"]
                    for e in store.read_rows(store.EVENTS)):
                try:
                    events.co_whys(pair[0], pair[1], pr["id"], pr.get("otl"))
                except WhyRefusal as r:
                    _log(ctx, "cowhy refused ask=%s reason=%s" % (ask["ask"], r.reason))
    except store.StoreBusy:
        _log(ctx, "turn pick: store lock busy ask=%s probe=%s" % (ask["ask"], probe["id"]))


# Final grill (store lock): the hook waits at most this long for the store
# lock, then skips the turn and logs (fail-open; never a 5 s stall).
LOCK_WAIT = 1.5


def main(ctx, stdin=None):
    """Hook entry: always exit 0; print only machine-handled context JSON."""
    store.LOCK_WAIT = LOCK_WAIT
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            note = run(ctx, payload)
            if note:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": note,
                }}))
    except store.StoreBusy:
        _log(ctx, "store lock busy > %.1fs: turn skipped" % LOCK_WAIT)
    except Exception:  # noqa: BLE001 -- fail-open hook
        _log(ctx, traceback.format_exc().replace("\n", " | "))
    return 0
