"""Non-blocking owner-picked Why card and its callback-bound confirmation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from . import store


def _copy(root):
    data = json.loads((Path(root) / "config/i18n/terms.json").read_text())
    return data["schemas"]["why_owner_card"]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state(root):
    return Path(root) / ".telegram_bot/state/why-net/owner-cards"


def render(root, candidates, now):
    if not 2 <= len(candidates) <= 3 or any(not isinstance(x, str) or not x.strip() or "\n" in x for x in candidates):
        raise ValueError("two-or-three-one-line-candidates-required")
    copy = _copy(root)
    body = copy["title"] + "\n\n"
    body += "\n".join("%d. %s" % (i, text) for i, text in enumerate(candidates, 1))
    body += "\n\n" + copy["now"] + ": " + now
    buttons = [[{"text": copy["choice"].format(index=i), "callback_data": "opt:" + copy["choice"].format(index=i)}
                for i in range(1, len(candidates) + 1)],
               [{"text": copy["other"], "callback_data": "opt:" + copy["other"]}]]
    return body, buttons


def show(root, session, work, candidates, now, push=None):
    """Render, push without gating the turn, and remember the exact bubble."""
    body, buttons = render(root, candidates, now)
    directory = _state(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (_sha(session + "\0" + work) + ".json")
    if path.exists():
        return json.loads(path.read_text())
    command = [str(push or Path(root) / "routines/push.sh"), "--strict", "--text", body,
               "--buttons-json", json.dumps(buttons, ensure_ascii=False, separators=(",", ":"))]
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode:
        raise RuntimeError("why-card-push-failed:%d" % completed.returncode)
    message_id = None
    ledger = Path(root) / ".telegram_bot/outbound-ledger.jsonl"
    if ledger.exists():
        rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
        match = next((row for row in reversed(rows) if row.get("body") == body and
                      row.get("keyboard") == [b["text"] for row in buttons for b in row]), None)
        if match:
            message_id = match.get("message_id")
    value = {"schema": 1, "session": session, "work": work, "candidates": candidates,
             "body": body, "bubble_text_sha": _sha(body), "message_id": message_id,
             "keyboard": [b["text"] for row in buttons for b in row], "awaiting_other": False,
             "shown_at": time.time_ns()}
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    return value


def confirm(root, pending, inbound):
    """Resolve only a tap belonging to this exact, unedited card."""
    required = (inbound.get("bubble_text_sha") == pending["bubble_text_sha"] and
                inbound.get("message_id") == pending["message_id"] and
                inbound.get("keyboard") == pending["keyboard"])
    index = inbound.get("option_index")
    if not required or type(index) is not int or not 1 <= index <= len(pending["keyboard"]):
        return None
    label = pending["keyboard"][index - 1]
    if inbound.get("callback_data") != "opt:" + label:
        return None
    if index <= len(pending["candidates"]):
        return _record(root, pending, pending["candidates"][index - 1], inbound)
    pending["awaiting_other"] = True
    return {"prompt": _copy(root)["other_prompt"]}


def typed(root, pending, words):
    if not pending.get("awaiting_other") or not isinstance(words, str) or not words.strip() or "\n" in words:
        return None
    return _record(root, pending, words, {"transport": "typed"})


def _record(root, pending, why, provenance):
    row = {"ev": "confirm", "why": why, "work": pending["work"], "session": pending["session"],
           "bubble_text_sha": pending["bubble_text_sha"], "message_id": pending["message_id"],
           "option_index": provenance.get("option_index"), "keyboard": pending["keyboard"],
           "callback_data": provenance.get("callback_data"), "at_ns": time.time_ns()}
    path = _state(root) / "confirmed.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    pending["confirmed_why"] = why
    pending["awaiting_other"] = False
    return row


def consume_hook(root, session, prompt):
    """Consume the bridge's latest bound callback, or the awaited typed line."""
    directory = _state(root)
    if not directory.exists() or not session:
        return None
    matches = []
    for path in directory.glob("*.json"):
        value = json.loads(path.read_text())
        if value.get("session") == session and not value.get("confirmed_why"):
            matches.append((value.get("shown_at", 0), path, value))
    if not matches:
        return None
    _, path, pending = max(matches)
    if pending.get("awaiting_other"):
        result = typed(root, pending, prompt)
    else:
        import why_owner_turn
        with store.canonical(root):
            rows = why_owner_turn.ledger_rows(session)
        row = rows[-1] if rows else {}
        result = confirm(root, pending, row) if row.get("source") == "callback" else None
    if result is not None:
        path.write_text(json.dumps(pending, ensure_ascii=False, sort_keys=True) + "\n")
    return result
