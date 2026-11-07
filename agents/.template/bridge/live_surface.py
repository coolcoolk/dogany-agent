"""Durable, opt-in per-chat frame transport (DGN-2085 F1 revision 8.1).

The checkpoint owns transport truth. The injected delivery service owns its
transactions; it must never call back into this module while holding one.
No domain decoder or domain database lives here.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from contextvars import ContextVar
import copy
import fcntl
from functools import wraps
import hashlib
import hmac
import inspect
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import zlib
import sys
import tempfile
import time
from html.parser import HTMLParser


class Refusal(Exception):
    def __init__(self, rc, reason, **details):
        super().__init__(reason)
        self.rc = rc
        self.result = dict(ok=False, diagnostic=reason, **details)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def load_request(stream):
    """Reject ambiguous JSON before any signature or transport decision."""
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 16, "duplicate JSON key")
            value[key] = item
        return value
    def invalid_number(value):
        raise Refusal(16, "non-integer JSON number")
    try:
        value = json.load(stream, object_pairs_hook=pairs, parse_float=invalid_number, parse_constant=invalid_number)
        canonical(value).decode("utf-8")
    except (json.JSONDecodeError, UnicodeError) as error:
        raise Refusal(16, "invalid JSON encoding") from error
    return value


def mac(key, value):
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


def positive(value):
    return type(value) is int and value > 0


def require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)


def boundary(function):
    @wraps(function)
    async def checked(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except OSError as error:
            raise Refusal(40, "checkpoint or authority storage unavailable") from error
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            kind = type(error).__name__
            detail = f": {error}" if isinstance(error, (KeyError, AttributeError)) else ""
            raise Refusal(16, f"invalid structured request: {kind}{detail}") from error
    return checked


def atomic(path, value):
    """Private checkpoint plus directory fsync before acknowledging admission."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def private_bytes(path, raw):
    from bridge.session_state import atomic_bytes as write_bytes, Refusal as StorageRefusal
    try:
        write_bytes(path, raw)
    except StorageRefusal as error:
        raise Refusal(error.rc, "private asset or export storage unavailable") from error


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise Refusal(40, "checkpoint unreadable") from error


class _Entities(HTMLParser):
    tags = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
            "span", "tg-spoiler", "a", "code", "pre", "blockquote", "tg-emoji"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        require(tag in self.tags, 16, "unsupported entity")
        permitted = {"a": {"href"}, "span": {"class"}, "code": {"class"}, "blockquote": {"expandable"},
                     "tg-emoji": {"emoji-id"}}
        require(all(k in permitted.get(tag, frozenset()) for k, _ in attrs), 16, "unsupported entity attributes")
        if tag == "a":
            require(any(k == "href" and isinstance(v, str) and v.startswith(("https://", "http://", "tg://"))
                        for k, v in attrs), 16, "invalid entity link")
        if tag == "span":
            require(attrs == [("class", "tg-spoiler")], 16, "invalid spoiler entity")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        require(bool(self.stack) and self.stack.pop() == tag, 16, "unbalanced entity")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_startendtag(self, tag, attrs):
        raise Refusal(16, "unsupported entity")


def rendered_length(text, parse_mode=None):
    require(isinstance(text, str), 16, "text must be a string")
    require(parse_mode in (None, "HTML"), 16, "unsupported parse mode")
    if parse_mode:
        parser = _Entities()
        parser.feed(text)
        parser.close()
        require(not parser.stack, 16, "unclosed entity")
        text = "".join(parser.parts)
    return len(text)


def photo_dimensions(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, shape, ended, data = 8, None, False, False
        while offset + 12 <= len(raw):
            length = struct.unpack(">I", raw[offset:offset + 4])[0]
            kind = raw[offset + 4:offset + 8]
            end = offset + 8 + length
            require(end + 4 <= len(raw), 16, "truncated photo")
            chunk = raw[offset + 8:end]
            require(zlib.crc32(kind + chunk) & 0xffffffff == struct.unpack(">I", raw[end:end + 4])[0],
                    16, "photo checksum invalid")
            if shape is None:
                require(kind == b"IHDR" and length == 13, 16, "invalid photo header")
                width, height = struct.unpack(">II", chunk[:8])
                shape = (width, height)
                require(chunk[10:] in (b"\x00\x00\x00", b"\x00\x00\x01"), 16, "unsupported photo encoding")
            if kind == b"IDAT":
                data = True
            offset = end + 4
            if kind == b"IEND":
                ended = length == 0 and offset == len(raw)
                break
        require(shape is not None and ended and data, 16, "incomplete photo")
        return shape
    require(raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9"), 16, "unsupported photo")
    offset = 2
    while offset + 4 <= len(raw):
        require(raw[offset] == 255, 16, "invalid photo marker")
        marker = raw[offset + 1]
        offset += 2
        if marker == 255:
            offset -= 1
            continue
        length = int.from_bytes(raw[offset:offset + 2], "big")
        require(length >= 2 and offset + length <= len(raw), 16, "invalid photo segment")
        if marker in (0xc0, 0xc1, 0xc2):
            require(length >= 8, 16, "invalid photo dimensions")
            return (int.from_bytes(raw[offset + 5:offset + 7], "big"), int.from_bytes(raw[offset + 3:offset + 5], "big"))
        offset += length
    raise Refusal(16, "photo dimensions unavailable")


# Only these results discharge an ordinary stage. Backoff is never completion.
EXPIRABLE_NOTICE_STATES = frozenset(("pending", "dispatching", "quarantined", "dropped"))
SATISFIED = {"ok", "unchanged", "absent", "retained_cleanup", "branch_not_selected"}
OUTCOMES = ["ok", "unchanged", "absent", "retained_cleanup"]
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SLOT = re.compile(r"^[a-z][a-z0-9_]{1,15}$")
_OWNER = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EMPTY_BRANCHES = {"schema": "delivery_branches/1", "groups": []}


def plan_budgets(stages, manifest):
    """Count shared work and executable alternatives, including provisional edits."""
    slots = dict.fromkeys((f["slot"] for f in stages), 0)
    for f in stages:
        if f["mode"] in ("edit", "retire"):
            slots[f["slot"]] += 1
    by_id = {f["K"]["stage_id"]: f for f in stages}
    members = {sid for g in manifest["groups"] for c in g["choices"] for sid in c["stage_ids"]}
    def count(items, notifications=False):
        return sum(f["mode"] == "send" and (not notifications or not f["notification"]["silent"]) for f in items)
    shared = [f for f in stages if f["K"]["stage_id"] not in members]
    maxima = []
    for notifications in (False, True):
        maxima.append(count(shared, notifications) + sum(max(count([by_id[sid] for sid in c["stage_ids"]], notifications)
                       for c in g["choices"]) for g in manifest["groups"]))
    return dict(max_sends=maxima[0], max_notifications=maxima[1], max_edits_by_slot=slots)


def validate_plan_preimage(value):
    """The rev8 wire object is strict before canonical encoding or hashing."""
    require(isinstance(value, dict) and frozenset(value) == {
        "S", "D", "frame_digest", "receipt_digest", "stages", "branch_manifest", "budgets"}, 16, "invalid plan preimage")
    def exact(v, keys):
        require(isinstance(v, dict) and frozenset(v) == frozenset(keys.split()), 16, "invalid plan object fields")
    def sha(v):
        require(isinstance(v, str) and _DIGEST.fullmatch(v), 16, "invalid plan digest field")
    def string(v):
        require(isinstance(v, str) and bool(v), 16, "invalid plan string")
        try:
            v.encode("utf-8")
        except UnicodeError as error:
            raise Refusal(16, "invalid Unicode scalar") from error
    exact(value["S"], "session_id chat_id surface_id epoch")
    scope = value["S"]
    require(positive(scope["session_id"]) and type(scope["chat_id"]) is int and positive(scope["epoch"]), 16, "invalid plan scope")
    string(scope["surface_id"])
    exact(value["D"], "outbox_id delivery_key")
    for v in value["D"].values():
        string(v)
    sha(value["frame_digest"]); sha(value["receipt_digest"])
    stages = value["stages"]
    require(isinstance(stages, list) and bool(stages), 16, "invalid bind stages")
    seen = []
    for stage in stages:
        exact(stage, "K slot mode target payload_digest depends_on protection allowed_outcomes notification")
        exact(stage["K"], "chat_id epoch op stage_id")
        k = stage["K"]
        require(type(k["chat_id"]) is int and k["chat_id"] == scope["chat_id"] and
                positive(k["epoch"]) and k["epoch"] == scope["epoch"], 16, "invalid stage scope")
        string(k["op"]); string(k["stage_id"]); string(stage["slot"])
        require(k["stage_id"] not in seen and stage["mode"] in ("send", "edit", "retire", "delete"), 16, "invalid bind identity")
        deps = stage["depends_on"]
        require(isinstance(deps, list) and deps == [sid for sid in seen if sid in deps], 16, "invalid bind dependencies")
        target = stage["target"]
        if stage["mode"] == "send":
            require(target is None, 16, "send target must be null")
        else:
            require(isinstance(target, dict) and (frozenset(target) == {"message_id"} and positive(target["message_id"]) or
                    frozenset(target) == {"stage_id"} and target["stage_id"] in seen), 16, "invalid bind target")
        sha(stage["payload_digest"])
        exact(stage["notification"], "class silent")
        string(stage["notification"]["class"])
        require(type(stage["notification"]["silent"]) is bool and
                (stage["mode"] == "send" or stage["notification"]["silent"] is True), 16, "invalid bind notification")
        protection = stage["protection"]
        exact(protection, "chat_id message_id body_digest protected_guidance source_frame_digest")
        require(type(protection["chat_id"]) is int and protection["chat_id"] == scope["chat_id"] and
                (protection["message_id"] is None or positive(protection["message_id"])) and
                type(protection["protected_guidance"]) is bool, 16, "invalid bind protection")
        require(protection["message_id"] == (target.get("message_id") if target else None), 16, "protection target mismatch")
        sha(protection["body_digest"]); sha(protection["source_frame_digest"])
        outcomes = stage["allowed_outcomes"]
        allowed = {"send": ["ok"], "edit": ["ok", "unchanged"], "retire": ["ok", "unchanged", "absent"],
                   "delete": OUTCOMES}[stage["mode"]]
        require(isinstance(outcomes, list) and bool(outcomes) and outcomes == [v for v in allowed if v in outcomes],
                16, "invalid bind outcomes")
        require(stage["mode"] != "delete" or protection["protected_guidance"] is False, 23, "protected bind deletion")
        seen.append(k["stage_id"])
    exact(value["branch_manifest"], "schema groups")
    require(value["branch_manifest"]["schema"] == "delivery_branches/1" and
            isinstance(value["branch_manifest"]["groups"], list), 16, "invalid bind manifest")
    budgets = value["budgets"]
    exact(budgets, "max_sends max_notifications max_edits_by_slot")
    require(all(type(v) is int and v >= 0 for v in (budgets["max_sends"], budgets["max_notifications"])) and
            isinstance(budgets["max_edits_by_slot"], dict) and
            all(type(v) is int and v >= 0 for v in budgets["max_edits_by_slot"].values()), 16, "invalid bind budgets")
    require(budgets == plan_budgets(stages, value["branch_manifest"]), 16, "bind budget mismatch")
    # Validate all nested JSON scalars, including the opaque manifest, before encoding.
    def scalars(v):
        if isinstance(v, dict):
            for k, item in v.items():
                string(k); scalars(item)
        elif isinstance(v, list):
            for item in v: scalars(item)
        elif isinstance(v, str):
            try: v.encode("utf-8")
            except UnicodeError as error: raise Refusal(16, "invalid Unicode scalar") from error
        else:
            require(v is None or type(v) in (bool, int), 16, "invalid plan scalar")
    scalars(value)
    return value


def sealed_payload_digest(payload):
    """Rev8.2 Q1: hash the adapter's object, excluding coordinator metadata.

    Existing coordinator frames retain their frozen rev8 encoding. Native
    frames carry the original sealed object; its seal is never replaced with
    a digest of the Telegram projection or checkpoint asset paths.
    """
    if "sealed_payload" in payload:
        return digest(payload["sealed_payload"])
    return digest(payload)


def native_projection(payload, photo=None):
    """Render the frozen pack object into Telegram fields without re-rendering."""
    require(isinstance(payload, dict), 16, "native payload must be an object")
    base = {"frame", "mode", "slot", "target", "notification"}
    content_stage = payload.get("mode") not in ("retire", "delete")
    require(frozenset(payload) == base | ({"content", "keyboard"} if content_stage else frozenset()),
            16, "invalid native payload fields")
    notification = payload["notification"]
    require(isinstance(notification, dict) and frozenset(notification) == {"class", "silent"} and
            type(notification["silent"]) is bool, 16, "invalid native notification")
    mode, target = payload["mode"], payload["target"]
    require(mode in ("send", "edit", "retire", "delete") and
            (target is None if mode == "send" else isinstance(target, dict) and
             frozenset(target) == {"message_id"} and positive(target["message_id"])), 16, "invalid native target")
    result = {k: copy.deepcopy(payload[k]) for k in ("frame", "mode", "slot", "target")}
    result.update(notification_class=notification["class"], silent=notification["silent"])
    if content_stage:
        content, keyboard = payload["content"], payload["keyboard"]
        require(isinstance(content, dict) and frozenset(content) == {"text", "entities", "media"} and
                isinstance(content["text"], str) and isinstance(content["entities"], list),
                16, "invalid native content")
        # Explicit UTF-16 offsets are preserved, including astral characters.
        positions, offset = {0}, 0
        for char in content["text"]:
            require(not 0xd800 <= ord(char) <= 0xdfff, 16, "invalid native Unicode scalar")
            offset += len(char.encode("utf-16-le")) // 2
            positions.add(offset)
        for entity in content["entities"]:
            require(isinstance(entity, dict) and isinstance(entity.get("type"), str) and
                    type(entity.get("offset")) is int and positive(entity.get("length")) and
                    entity["offset"] in positions and entity["offset"] + entity["length"] in positions,
                    16, "invalid native UTF-16 entity")
        result.update(text=content["text"], entities=copy.deepcopy(content["entities"]))
        if keyboard is not None:
            require(isinstance(keyboard, dict) and frozenset(keyboard) == {"action_token", "input_stage", "rows"} and
                    isinstance(keyboard["rows"], list), 16, "invalid native keyboard")
            result["buttons"] = []
            for row in keyboard["rows"]:
                require(isinstance(row, list) and bool(row), 16, "invalid native keyboard row")
                converted = []
                for button in row:
                    require(isinstance(button, dict) and frozenset(button) == {"label", "callback_data"},
                            16, "invalid native button")
                    converted.append(dict(text=button["label"], callback_data=button["callback_data"]))
                result["buttons"].append(converted)
        media = content["media"]
        if media is not None:
            require(isinstance(media, dict) and frozenset(media) == {"artifact_digest", "artifact_ref", "kind", "mime_type"} and
                    media["kind"] == "image" and media["mime_type"] in ("image/png", "image/jpeg") and
                    isinstance(media["artifact_ref"], str) and isinstance(photo, dict) and
                    photo.get("digest") == media["artifact_digest"], 16, "native photo snapshot required")
            result["photo"] = copy.deepcopy(photo)
        else:
            require(photo is None, 16, "unexpected native photo")
    else:
        require(photo is None, 16, "retirement cannot carry media")
    return result


def native_frame(stage, payload, photo=None):
    """Compare the kit's existing seal before constructing a coordinator frame."""
    require(digest(payload) == stage["payload_digest"], 17, "native sealed payload mismatch")
    projected = native_projection(payload, photo)
    require(all(projected[k] == stage[k] for k in ("mode", "slot", "target")) and
            payload["notification"] == stage["notification"], 17, "native stage binding mismatch")
    return dict(projected, stage_id=stage["K"]["stage_id"],
                protection=copy.deepcopy(stage["protection"]),
                allowed_outcomes=copy.deepcopy(stage["allowed_outcomes"]),
                depends_on=copy.deepcopy(stage["depends_on"]), sealed_payload=copy.deepcopy(payload))


def checked_native_frame(frame):
    """Reject divergent projections before admission and render sealed bytes at I/O."""
    projected = native_projection(frame["sealed_payload"], frame.get("photo"))
    fields = {"frame", "mode", "slot", "target", "notification_class", "silent", "text", "entities", "buttons", "photo"}
    require({k: frame[k] for k in fields if k in frame} == projected and "parse_mode" not in frame,
            17, "native projection differs from sealed payload")
    return dict(frame, **projected)


def plan_preimage(request, validate=True):
    stages = []
    for f in request["frames"]:
        stages.append(dict(K=dict(chat_id=request["delivery"]["S"]["chat_id"], epoch=request["epoch"],
                           op=request["op"], stage_id=f["stage_id"]), slot=f["slot"], mode=f["mode"],
                           target=None if f["mode"] == "send" else f.get("target", {"message_id": f.get("message_id")}),
                           payload_digest=sealed_payload_digest(f), depends_on=f.get("depends_on", []), protection=f["protection"],
                           allowed_outcomes=f["allowed_outcomes"], notification=dict(
                               **{"class": f["notification_class"]}, silent=f["silent"])))
    value = dict(S=request["delivery"]["S"], D=request["delivery"]["D"],
                 frame_digest=request["validation"]["frame_digest"], receipt_digest=request["validation"]["receipt_digest"],
                 stages=stages, branch_manifest=request["branch_manifest"], budgets=plan_budgets(stages, request["branch_manifest"]))
    return validate_plan_preimage(value) if validate else value


def authenticated_export(key, kind, value):
    body = dict(kind=kind, value=value)
    return dict(body, mac=mac(key, body))


TRANSITION_PLAN = "transition_retirement_plan/1"
TRANSITION_ACK = "transition_retirement_ack/1"
TRANSITION_FENCE = ("pack", "engine", "generation", "checkpoint_digest", "token")
RECEIPT_EXPORT = ("K", "seq", "message_id", "target", "result", "payload_digest", "lease_fence",
                  "evidence_digest", "retry_after", "next_attempt_at_ms", "selection_digest")


def transition_retirement_frame(target):
    """Frozen markup-only preimage; byte-identical to the kit's retirement_frame."""
    return dict(stage_id="retire", frame="card", mode="retire", slot="card",
                allowed_outcomes=["ok", "unchanged"], target={"message_id": target["message_id"]},
                silent=True, notification_class="live_card")


def _message_gone(response):
    """True when Telegram says the edit target no longer exists."""
    if not isinstance(response, dict) or response.get("ok") is not False or response.get("error_code") not in (400, 404):
        return False
    text = response.get("description")
    if not isinstance(text, str):
        return False
    text = text.lower()
    return "message to edit not found" in text or "message not found" in text or \
        ("message can't be edited" in text and "deleted" in text)


def transition_plan(fence, checkpoint, target):
    """Transition-specific maintenance constructor: TP, bound to F and Q.

    Unlike notice maintenance (op turn:<chat>:notice_*, coordinator_fence =
    surface generation), TP's stage key is epoch 0 and transition-scoped,
    H(TP) is the plan digest and the pack token F.token is the lease fence.
    """
    require(isinstance(fence, dict) and all(k in fence for k in TRANSITION_FENCE) and
            positive(fence["token"]) and positive(fence["generation"]), 16, "transition fence required")
    require(isinstance(checkpoint, dict) and isinstance(checkpoint.get("id"), str) and checkpoint["id"],
            16, "transition checkpoint required")
    require(isinstance(target, dict) and sorted(target) == ["chat_id", "message_id"] and
            type(target["chat_id"]) is int and positive(target["message_id"]), 16, "exact retirement target required")
    tp = dict(schema=TRANSITION_PLAN, **{k: fence[k] for k in TRANSITION_FENCE}, target=dict(target),
              K=dict(chat_id=target["chat_id"], epoch=0,
                     op="transition:" + checkpoint["id"] + ":" + str(target["message_id"]), stage_id="retire"),
              payload_digest=digest(transition_retirement_frame(target)))
    return tp


def _absent_recovery_ok(recovery, receipt, tp):
    if not isinstance(recovery, dict) or sorted(recovery) != ["evidence", "record"]:
        return False
    record, evidence = recovery["record"], recovery["evidence"]
    return (isinstance(evidence, dict) and bool(evidence) and isinstance(record, dict) and
            receipt["evidence_digest"] == digest(evidence) and record.get("evidence_digest") == digest(evidence) and
            record.get("resolution") == "absent" and record.get("message_id") is None and
            record.get("quiescence") is True and record.get("target") == receipt["target"] and
            record.get("K") == tp["K"] and record.get("seq") == receipt["seq"] and
            record.get("plan_digest") == receipt["plan_digest"] and
            record.get("payload_digest") == receipt["payload_digest"] and "auth" in record)


def verify_transition_ack(ack, fence, checkpoint, coordinator_key, publisher_key):
    """Framework-side verifier of A before the kit importer sees it."""
    require(isinstance(ack, dict) and sorted(ack) == ["export", "plan", "receipt", "recovery", "schema"] and
            ack["schema"] == TRANSITION_ACK, 16, "transition acknowledgement shape")
    tp = ack["plan"]
    require(tp == transition_plan(fence, checkpoint, tp.get("target")), 17, "transition plan differs from F/Q")
    receipt = ack["receipt"]
    require(isinstance(receipt, dict) and isinstance(receipt.get("auth"), str) and
            hmac.compare_digest(receipt["auth"], mac(coordinator_key, {k: v for k, v in receipt.items() if k != "auth"})),
            17, "invalid coordinator receipt")
    require(receipt["K"] == tp["K"] and receipt["D"] is None and receipt["plan_digest"] == digest(tp) and
            receipt["lease_fence"] == tp["token"] and receipt["payload_digest"] == tp["payload_digest"] and
            receipt["target"] == {"message_id": tp["target"]["message_id"]} and receipt["silent"] is True and
            ((receipt["result"] in ("ok", "unchanged") and receipt["message_id"] == tp["target"]["message_id"] and
              receipt["evidence_digest"] is None and ack["recovery"] is None) or
             (receipt["result"] == "absent" and receipt["message_id"] is None and
              _absent_recovery_ok(ack["recovery"], receipt, tp))), 24, "retirement not established")
    require(ack["export"] == authenticated_export(publisher_key, "receipt", {k: receipt[k] for k in RECEIPT_EXPORT}),
            17, "receipt export differs")
    return tp


class Coordinator:
    NOTICE_CLAIM_TIMEOUT_MS = 120000

    def __init__(self, root, delivery_service=None, transport=None, clock=None,
                 notification_policy=None, before_io=None, attempt_started=None):
        self.before_io, self.attempt_started = before_io, attempt_started
        self.root = Path(root).resolve()
        self.base = self.root / ".telegram_bot/live-surface"
        self.delivery_service = delivery_service or command_delivery_service(self.root)
        self.transport = transport
        self.now = clock or (lambda: int(time.time() * 1000))
        self.notification_policy = notification_policy or self._notification

    @staticmethod
    def _notification(name):
        # Reuse the instance's policy; standalone fixtures need no bot runtime.
        from bridge.config import notify_silent
        return notify_silent(name)

    @contextlib.asynccontextmanager
    async def lock(self, chat):
        require(type(chat) is int, 2, "chat must be an integer")
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.base / (str(chat) + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def state(self, chat):
        try:
            value = read(self.base / (str(chat) + ".json"))
            if value is not None:
                require(value.get("schema") == "live_surface/1", 40, "invalid surface state")
            if value is not None:
                value.setdefault("transient_slots", [])
            return value
        except (ValueError, Refusal) as error:
            # The surface view is fail-open by contract; coordinator attempts
            # remain fail-closed and cannot be erased by this compatibility rule.
            print("live surface state unreadable: " + str(error), file=sys.stderr)
            return None

    def save_state(self, state):
        atomic(self.base / (str(state["chat_id"]) + ".json"), state)

    def control(self, chat):
        return read(self.base / (str(chat) + ".transport.json"),
                    {"barrier": None, "not_before_ms": 0, "ordinary": {}, "notices": {}})

    def save_control(self, chat, value):
        atomic(self.base / (str(chat) + ".transport.json"), value)

    def plan_path(self, chat, epoch, op):
        return self.base / "frames" / str(chat) / str(epoch) / (hashlib.sha256(op.encode()).hexdigest() + ".json")

    def plans(self, chat, epoch=None):
        directory = self.base / "frames" / str(chat)
        paths = (directory / str(epoch)).glob("*.json") if epoch else directory.glob("*/*.json")
        return [(path, read(path)) for path in sorted(paths)]

    def _quarantine_notice(self, notice, path, plan, reason):
        """Make an authorized, ambiguous send terminal without erasing its evidence."""
        notice.update(status="quarantined", quarantined_at_ms=self.now(), diagnostic=reason)
        plan["quarantined"] = True
        plan["complete"] = True
        atomic(path, plan)

    def _recover_notice_claim(self, notice, path, plan):
        """Recover only abandoned reservations which provably preceded I/O."""
        if notice.get("status") != "dispatching" or plan.get("complete"):
            return False
        reserved = [s for s in plan["stages"] if s["status"] == "reserved" and s.get("attempts")]
        if not reserved:
            return False
        attempt = reserved[0]["attempts"][-1]
        if self.now() - attempt.get("started_at_ms", self.now()) < self.NOTICE_CLAIM_TIMEOUT_MS:
            return False
        if attempt.get("io_authorized"):
            reserved[0]["status"] = "uncertain"
            self._quarantine_notice(notice, path, plan, "authorized notice claim expired")
        else:
            reserved[0]["status"] = "pending"
            attempt["status"] = "abandoned_before_io"
            atomic(path, plan)
            notice["status"] = "pending"
        return True

    def key(self, publisher):
        require(isinstance(publisher, str) and _OWNER.fullmatch(publisher), 17, "unknown publisher")
        path = self.base / "publishers" / (publisher + ".key")
        try:
            require(stat.S_IMODE(path.stat().st_mode) == 0o600 and not path.is_symlink(),
                    17, "publisher key must be private")
            key = path.read_bytes()
            require(len(key) >= 32, 17, "invalid publisher key")
            require((self.root / "config/packs" / (publisher + ".receipt")).is_file(),
                    17, "publisher is not installed")
            return key
        except OSError as error:
            raise Refusal(17, "publisher key unavailable") from error

    def publisher_config(self, owner):
        require(isinstance(owner, str) and _OWNER.fullmatch(owner), 17, "unknown publisher")
        return read(self.root / "config/packs" / (owner + ".publisher.json"), {})

    def activation(self, owner):
        return read(self.root / "config/packs" / (owner + ".session_activation.json"))

    def activated(self):
        for path in (self.root / "config/packs").glob("*.session_activation.json"):
            value = read(path)
            if value and value.get("schema") == "session_activation/1" and value.get("phase") in (
                    "active", "activating", "deactivating"):
                return True
        return False

    def probe(self, owner, prefix):
        from bridge import callback_routes, session_activation
        services = m2_services(self.root)
        try:
            result = session_activation.probe(self.root, prefix, current_capabilities=lambda:
                session_activation.capabilities(self.root, services.get("surface_apis", {}), services.get("artifact_codec")))
            require(callback_routes.locate(self.root, prefix) == owner, 33, "probe owner mismatch")
            return result
        except session_activation.Refusal as error:
            raise Refusal(error.rc, str(error)) from error

    def native_capabilities(self):
        from bridge import session_activation
        return session_activation.capabilities(self.root, m2_services(self.root).get("surface_apis", {}), None)

    def runtime_receipt(self, owner):
        """Fresh kit runtime receipt for this open (None: owner has no kit session config)."""
        from bridge import session_activation
        try:
            return session_activation.publish_session_open_receipt(
                self.root, owner, current_capabilities=self.native_capabilities, clock=self.now)
        except session_activation.Refusal as error:
            raise Refusal(error.rc, "session open refused: " + str(error)) from error

    def journal(self, state, event, **values):
        row = dict(ts_ms=self.now(), chat_id=state["chat_id"], surface_id=state["surface_id"],
                   epoch=state["epoch"], ev=event, **values)
        with open(self.base / "journal.jsonl", "ab") as stream:
            stream.write(canonical(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _transient_slots(self, owner, answer_slot, previous=None):
        transient = self.publisher_config(owner).get("transient_slots", [])
        require(transient in ([], ["answer"]) and type(answer_slot) is bool and
                answer_slot == (transient == ["answer"]) and
                (previous is None or previous.get("transient_slots", []) == transient), 2, "transient-slot-mismatch")
        return copy.deepcopy(transient)

    @boundary
    async def open(self, chat, owner, surface, prefix, slots=None, action_slot=None,
                   model_final="answer", lease_s=5400, takeover=False, attach_dirs=(), adopt=None, answer_slot=False):
        require(_OWNER.fullmatch(owner) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", surface), 2, "invalid identity")
        transient = self._transient_slots(owner, answer_slot)
        slots = slots or ["card"]
        action_slot = action_slot or slots[0]
        require(1 <= len(slots) <= 4 and len(frozenset(slots)) == len(slots) and
                all(_SLOT.fullmatch(s) and s != "answer" for s in slots) and action_slot in slots, 2, "invalid slots")
        require(model_final in ("answer", "drop") and type(lease_s) is int and 0 < lease_s <= 21600,
                2, "invalid policy or lease")
        require(len(attach_dirs) <= 2, 2, "too many attachment directories")
        for relative in attach_dirs:
            path = Path(relative)
            require(not path.is_absolute() and ".." not in path.parts and
                    (self.root / path).resolve().is_relative_to(self.root), 2, "invalid attachment directory")
        self.probe(owner, prefix)
        runtime = self.runtime_receipt(owner)
        async with self.lock(chat):
            old, control = self.state(chat), self.control(chat)
            if old and old["state"] == "open":
                if old["owner"] == owner and old["surface_id"] == surface:
                    self._transient_slots(owner, answer_slot, old)
                    return dict(ok=True, epoch=old["epoch"], envelope_id=f"open:{chat}:{old['epoch']}",
                                opened_at_ms=old["opened_at_ms"])
                require(not self._pending(chat, old["epoch"]), 14, "prior epoch recovery pending")
                require(takeover, 14, "another surface is open")
                # Retirement is mandatory; never discard a keyboard on takeover.
                require(not any(s["has_keyboard"] for s in old["slots"].values()), 14, "retirement pending")
                old.update(state="closed", close_reason="takeover", closed_at_ms=self.now())
                self.save_state(old)
            require(not any(not p["complete"] for _, p in self.plans(chat)), 13, "prior transport recovery pending")
            control["barrier"] = "opening"
            for item in control["ordinary"].values():
                item["muted"] = True
            self.save_control(chat, control)
            require(not any(i.get("status") in ("reserved", "uncertain")
                            for i in control["ordinary"].values()), 13, "pre-open transport recovery pending")
            require(not any(i.get("has_keyboard") for i in control["ordinary"].values()),
                    13, "pre-open keyboard retirement pending")
            bindings = {s: {"message_id": None, "has_keyboard": False, "retired_ids": []} for s in slots}
            for slot, message_id in (adopt or {}).items():
                require(slot in bindings and self._ledger_contains(chat, message_id), 2, "adoption has no ledger receipt")
                bindings[slot]["message_id"] = message_id
            epoch = old["epoch"] + 1 if old else 1
            state = dict(schema="live_surface/1", chat_id=chat, state="open", final=False,
                         epoch=epoch, owner=owner, surface_id=surface, prefix=prefix,
                         model_final=model_final, slots=bindings, action_slot=action_slot, transient_slots=transient,
                         attach_dirs=list(attach_dirs), opened_at_ms=self.now(), lease_s=lease_s,
                         lease_until_ms=self.now() + lease_s * 1000, closed_at_ms=None,
                         close_stale_at_ms=None, close_reason=None,
                         generation=self.activation(owner)["generation"], envelopes={}, commit_order=0)
            op = f"open:{chat}:{epoch}"
            state["envelopes"][op] = {"kind": "open"}
            if runtime is not None:
                state["runtime_receipt_id"] = runtime["receipt_id"]
            self.save_state(state)
            control["barrier"] = None
            self.save_control(chat, control)
            self.journal(state, "open", **({} if runtime is None else {"runtime_receipt_id": runtime["receipt_id"]}))
            return dict(ok=True, epoch=epoch, envelope_id=op, opened_at_ms=state["opened_at_ms"])

    def _ledger_contains(self, chat, message_id):
        path = self.root / ".telegram_bot/outbound-ledger.jsonl"
        if not path.exists():
            return False
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                if str(row.get("chat_id")) == str(chat) and str(row.get("message_id")) == str(message_id):
                    return True
            except ValueError:
                continue
        return False

    def _due_items(self, state):
        # The frozen due store is owner-scoped. Matching the opaque surface
        # and owner conservatively pins recovery; the scheduler resolves chat.
        if state is None:
            return []
        items = []
        for path in (self.root / ".telegram_bot/due").glob("*.json"):
            item = read(path, {})
            if item.get("schema") == "due_event/1" and item.get("owner") == state["owner"] and item.get("surface") == {
                    "surface_id": state["surface_id"], "epoch": state["epoch"]} and item.get("state") in ("pending", "firing"):
                items.append(item)
        return items

    def _pending(self, chat, epoch):
        if any(not plan["complete"] for _, plan in self.plans(chat, epoch)):
            return True
        state = self.state(chat)
        if state and state["epoch"] == epoch and self._due_items(state):
            return True
        for path in (self.base / "ingress").rglob("*.json"):
            item = read(path, {})
            envelope = item.get("envelope", {})
            if envelope.get("chat_id") == chat and envelope.get("surface", {}).get("epoch") == epoch and \
                    item.get("dispatch_state") not in ("complete", "cancelled", "fenced"):
                return True
        return False

    @boundary
    async def issue(self, chat, kind, event_id, **binding):
        """Trusted ingress calls this after access/authentication, before launch."""
        require(kind in ("tap", "due", "turn"), 2, "invalid event kind")
        async with self.lock(chat):
            state = self.state(chat)
            require(state and state["state"] == "open", 10, "surface closed")
            op = f"{kind}:{event_id}"
            entry = dict(kind=kind, **binding)
            previous = state["envelopes"].get(op)
            require(previous is None or previous == entry, 16, "changed event binding")
            state["envelopes"][op] = entry
            self.save_state(state)
            return op

    @boundary
    async def admit_publisher_ingress(self, record, recovery):
        """Authenticate original ingress, then admit that exact op to its epoch."""
        require(callable(getattr(recovery, "checked", None)) and
                callable(getattr(recovery, "publisher_exports", None)), 17,
                "publisher ingress recovery must be session_activation.OriginalCommandRecovery")
        recovery.checked(record)
        envelope = record["envelope"]
        surface = envelope.get("surface")
        require(isinstance(surface, dict), 17, "publisher ingress has no original surface")
        async with self.lock(envelope["chat_id"]):
            recovery.checked(record)
            state = self._surface(envelope["chat_id"], surface["surface_id"], surface["epoch"])
            require(state["owner"] == record["pack"] and state["generation"] == record["generation"],
                    19, "publisher ingress generation moved")
            entry = dict(kind=envelope["kind"], ingress_digest=record["envelope_digest"])
            op = envelope["envelope_id"]
            previous = state["envelopes"].get(op)
            require(previous is None or previous == entry, 16, "publisher ingress changed")
            state["envelopes"][op] = entry
            self.save_state(state)
        return recovery.publisher_exports(record)

    @boundary
    async def touch(self, chat, surface, epoch, lease_s=5400):
        async with self.lock(chat):
            state = self._surface(chat, surface, epoch)
            require(type(lease_s) is int and 0 < lease_s <= 21600, 2, "invalid lease")
            state["lease_until_ms"] = max([state["lease_until_ms"], self.now() + lease_s * 1000] +
                                          [item["due_at_ms"] + 600000 for item in self._due_items(state)])
            self.save_state(state)
            self.journal(state, "touch")
            return dict(ok=True, lease_until_ms=state["lease_until_ms"])

    def _surface(self, chat, surface, epoch, op=None):
        state = self.state(chat)
        require(state and state["surface_id"] == surface and state["epoch"] == epoch,
                10, "surface or epoch fenced")
        require(state["state"] == "open" or (op == f"close:{chat}:{epoch}" and not state["final"]),
                10, "surface closed")
        return state

    @boundary
    async def close(self, chat, surface, epoch, reason="done", final=False):
        require(reason in ("done", "abandon"), 2, "invalid close reason")
        async with self.lock(chat):
            state = self.state(chat)
            require(state and state["surface_id"] == surface and state["epoch"] == epoch, 10, "epoch fenced")
            already = state["state"] == "closed"
            op = f"close:{chat}:{epoch}"
            if final:
                plan = read(self.plan_path(chat, epoch, op))
                require(plan and plan["complete"] and not self._pending(chat, epoch) and
                        not any(s["has_keyboard"] for s in state["slots"].values()), 12, "closeout pending")
                state["final"] = True
            if not already:
                state.update(state="closed", closed_at_ms=self.now(), close_stale_at_ms=self.now() + 1800000,
                             close_reason=reason)
                state["envelopes"][op] = {"kind": "close"}
            self.save_state(state)
            self.journal(state, "close")
            summary = self.metrics(chat, epoch)
            self.journal(state, "summary", **summary)
            return dict(ok=True, already_closed=already, envelope_id=op, summary=summary)

    async def _service(self, operation, args, publisher):
        require(self.delivery_service is not None, 40, "delivery authority unavailable")
        expires = self.now() + 120000
        body = dict(schema="live_delivery/1", operation=operation, args=args,
                    publisher_id=publisher, expires_at_ms=expires)
        request = dict(schema="live_delivery/1", operation=operation, args=args,
                       auth=dict(publisher_id=publisher, expires_at_ms=expires,
                                 mac=mac(self.key(publisher), body)))
        response = self.delivery_service(request)
        if inspect.isawaitable(response):
            try:
                response = await asyncio.wait_for(response, timeout=10)
            except asyncio.TimeoutError as error:
                raise Refusal(40, "delivery authority timeout") from error
        require(isinstance(response, dict), 40, "invalid delivery authority response")
        if response.get("ok") is not True:
            code = response.get("error", {}).get("code")
            refused_rc = {"AUTH_FAILED": 17, "INVALID_SELECTOR": 16, "INVALID_SCHEMA": 16,
                          "LEASE_EXPIRED": 19, "FENCE_STALE": 19, "CLAIM_BUSY": 19, "PROTECTED_CONTENT": 23,
                          "CONFLICT": 16, "PLAN_MISMATCH": 16, "RECEIPT_MISMATCH": 16,
                          "TRANSITION_INVALID": 16, "STAGES_PENDING": 24}.get(code, 40)
            raise Refusal(refused_rc, "delivery authority refused", service_error=code)
        return response["result"]

    async def _lease(self, plan, minimum_remaining_ms=0):
        delivery = plan["delivery"]
        require(delivery["L"]["lease_until_ms"] > self.now(), 19, "delivery lease expired")
        args = {key: delivery[key] for key in ("S", "P", "D", "L")}
        result = await self._service("delivery_check", args, delivery["P"]["publisher_id"])
        require(result.get("current") is True and result.get("L") == delivery["L"], 19, "delivery fence obsolete")
        if delivery["L"]["lease_until_ms"] - self.now() < minimum_remaining_ms:
            renewed = await self._service("delivery_renew", dict(args, ttl_ms=30000), delivery["P"]["publisher_id"])
            lease = renewed.get("L")
            require(isinstance(lease, dict) and lease.get("holder_id") == delivery["L"]["holder_id"] and
                    lease.get("lease_fence") == delivery["L"]["lease_fence"] and
                    type(lease.get("lease_until_ms")) is int and lease["lease_until_ms"] >= self.now() + minimum_remaining_ms,
                    19, "renewal cannot cover bounded attempt")
            plan.setdefault("lease_history", []).append(copy.deepcopy(lease))
            delivery["L"] = lease

    @boundary
    async def bind_command(self, chat, op, record, auth):
        """Persist the trusted adapter's exact opaque snapshots before seal.

        Kit typed objects use their own serialized bytes. This boundary hashes
        those supplied bytes, never a guessed replacement serialization.
        """
        async with self.lock(chat):
            S, P = record["S"], record["P"]
            state = self._surface(chat, S["surface_id"], S["epoch"], op)
            require(op in state["envelopes"] and S["chat_id"] == chat and positive(S["session_id"]),
                    18, "command surface mismatch")
            require(P["publisher_id"] == state["owner"] and
                    hmac.compare_digest(mac(self.key(state["owner"]), record), auth), 17, "command binding MAC invalid")
            for raw_name, digest_name in (("command_bytes", "command_digest"), ("receipt_bytes", "receipt_digest"),
                                           ("frame_bytes", "frame_digest")):
                require(isinstance(record.get(raw_name), str) and
                        "sha256:" + hashlib.sha256(record[raw_name].encode()).hexdigest() == record.get(digest_name),
                        16, "typed snapshot digest mismatch")
            require(digest(record["approval"]) == record["approval_digest"] and
                    digest(record["protection"]) == record["protection_digest"] and positive(record["revision"]),
                    18, "approval or protection binding invalid")
            require(isinstance(record.get("binding_id"), str) and bool(record["binding_id"]), 16, "binding id missing")
            path = self.base / "bindings" / str(chat) / str(S["epoch"]) / (hashlib.sha256(op.encode()).hexdigest() + ".json")
            previous = read(path)
            require(previous is None or previous == record, 16, "command binding changed")
            atomic(path, record)
            return dict(recorded=True, binding_id=record["binding_id"])

    def _binding(self, request, state):
        path = self.base / "bindings" / str(state["chat_id"]) / str(state["epoch"]) / (hashlib.sha256(request["op"].encode()).hexdigest() + ".json")
        record = read(path)
        require(record is not None, 17, "stored command binding required")
        validation = request["validation"]
        require(record["S"] == request["delivery"]["S"] and record["P"] == request["delivery"]["P"], 18, "command identity mismatch")
        for field in ("binding_id", "frame_digest", "receipt_digest", "revision", "approval_digest", "protection_digest"):
            require(record[field] == validation[field], 17 if field == "receipt_digest" else 18, "command proof mismatch")
        return record

    def _auth(self, request, state):
        validation, delivery = request.get("validation"), request.get("delivery")
        require(isinstance(validation, dict) and isinstance(delivery, dict), 16, "authenticated plan required")
        fields = {"schema", "publisher_id", "binding_id", "frame_digest", "plan_digest", "receipt_digest",
                  "session_id", "revision", "epoch", "approval_digest", "protection_digest", "auth"}
        require(frozenset(validation) == fields and validation["schema"] == "validated_plan/1", 16, "invalid validation record")
        require(positive(validation["session_id"]) and positive(validation["epoch"]) and positive(validation["revision"]),
                16, "invalid validation wire types")
        require(frozenset(delivery) == {"S", "P", "D", "L"}, 16, "invalid delivery record")
        S, P, D, L = (delivery[k] for k in ("S", "P", "D", "L"))
        require(all(isinstance(v, dict) for v in (S, P, D, L)), 16, "invalid delivery objects")
        require(frozenset(S) == {"session_id", "chat_id", "surface_id", "epoch"} and
                positive(S["session_id"]) and type(S["chat_id"]) is int and positive(S["epoch"]), 16, "invalid session identity")
        require(frozenset(P) == {"publisher_id", "actor", "member_id"} and all(isinstance(v, str) and v for v in P.values()) and
                frozenset(D) == {"outbox_id", "delivery_key"} and all(isinstance(v, str) and v for v in D.values()) and
                frozenset(L) == {"holder_id", "lease_fence", "lease_until_ms"} and isinstance(L["holder_id"], str) and
                bool(L["holder_id"]) and positive(L["lease_fence"]) and positive(L["lease_until_ms"]), 16, "invalid delivery identity")
        require(S == dict(session_id=validation["session_id"], chat_id=state["chat_id"],
                          surface_id=state["surface_id"], epoch=state["epoch"]) and
                validation["epoch"] == state["epoch"] and positive(validation["revision"]), 18, "revision or session mismatch")
        require(validation["publisher_id"] == state["owner"] == P["publisher_id"], 17, "publisher owner mismatch")
        for field in ("frame_digest", "plan_digest", "receipt_digest", "approval_digest", "protection_digest"):
            require(isinstance(validation[field], str) and _DIGEST.fullmatch(validation[field]), 16, "invalid digest")
        require(validation["plan_digest"] == digest(plan_preimage(request)),
                16, "plan digest mismatch")
        unsigned = {k: v for k, v in validation.items() if k != "auth"}
        expected = mac(self.key(state["owner"]), dict(validation=unsigned, delivery=delivery,
                                                       plan_digest=validation["plan_digest"]))
        require(isinstance(validation["auth"], str) and hmac.compare_digest(expected, validation["auth"]), 17, "publisher MAC invalid")

    def _preflight(self, request, state, branch_path=False, no_io=()):
        frames = request["frames"]
        if not branch_path and request["branch_manifest"].get("groups"):
            self._branches(request["branch_manifest"], frames, state)
            paths = [(frames, frozenset())]
            for group in request["branch_manifest"]["groups"]:
                choices = [frozenset(c["stage_ids"]) for c in group["choices"]]
                paths = [([f for f in path if f["stage_id"] not in losers], omitted | losers)
                         for path, omitted in paths for losers in choices]
            for frames_path, omitted in paths:
                self._preflight(dict(request, frames=frames_path), state, branch_path=True, no_io=omitted)
            return

        require(isinstance(frames, list) and bool(frames), 16, "empty plan")
        ids, edits, sends, final_keyboard, retired = [], {}, {}, False, False
        action = state["action_slot"]
        current = state["slots"][action]
        op = request["op"]
        require(op in state["envelopes"], 12, "envelope was not issued")
        kind = state["envelopes"][op]["kind"]
        for index, frame in enumerate(frames):
            allowed = {"stage_id", "frame", "slot", "mode", "message_id", "text", "buttons", "photo", "parse_mode",
                       "target", "protection", "allowed_outcomes", "depends_on", "silent", "notification_class",
                       "sealed_payload", "entities"}
            require(isinstance(frame, dict) and not (frozenset(frame) - allowed), 16, "unknown stage fields")
            if "sealed_payload" in frame:
                checked_native_frame(frame)
            sid, slot, mode = frame.get("stage_id"), frame.get("slot", action), frame.get("mode")
            require(isinstance(sid, str) and _STAGE.fullmatch(sid) and sid not in ids, 16, "invalid stage id")
            if frame.get("frame") == "inert-preview":
                from bridge.inert_preview import validate_frame
                validate_frame(frame)
                require(not final_keyboard and kind == "turn" and
                        frame.get("allowed_outcomes") == ["ok"] and
                        frame.get("depends_on", []) == [] and
                        frame.get("notification_class") == "inert_preview" and
                        frame.get("silent") is self.notification_policy("inert_preview") and
                        0 < rendered_length(frame.get("text", ""), frame.get("parse_mode")) <= 4096,
                        16, "invalid preview envelope or content")
                ids.append(sid)
                continue
            require((slot in state["slots"] or slot == "answer" and "answer" in state.get("transient_slots", [])) and mode in ("send", "edit", "retire", "delete") and
                    frame.get("frame") in ("card", "answer", "repost", "close", "end"), 16, "invalid stage")
            require((frame["frame"] == "answer") == (slot == "answer") and
                    (slot != "answer" or mode == "send" and frame.get("target") is None and
                     frame.get("message_id") is None and kind == "turn" and not frame.get("buttons")),
                    16, "invalid transient answer stage")
            kind_modes = {"card": {"send", "edit", "retire"}, "answer": {"send"},
                          "repost": {"send", "retire", "delete"}, "close": {"send", "edit", "retire"}, "end": {"send"}}
            require(mode in kind_modes[frame["frame"]], 16, "mode incompatible with frame")
            if frame["frame"] == "card" and mode == "edit":
                require(kind in ("open", "tap", "due", "turn"), 12, "card edit envelope refused")
            deps = frame.get("depends_on", [])
            require(isinstance(deps, list) and len(frozenset(deps)) == len(deps) and all(d in ids or d in no_io for d in deps), 16, "invalid dependency order")
            ids.append(sid)
            outcomes = frame.get("allowed_outcomes")
            require(isinstance(outcomes, list) and outcomes and outcomes == [v for v in OUTCOMES if v in outcomes],
                    16, "invalid terminal permissions")
            permitted = {"send": {"ok"}, "edit": {"ok", "unchanged"}, "retire": {"ok", "unchanged", "absent"},
                         "delete": {"ok", "unchanged", "absent", "retained_cleanup"}}[mode]
            require(all(v in permitted for v in outcomes), 16, "outcome incompatible with mode")
            require(mode in ("retire", "delete") or not any(v in outcomes for v in ("absent", "retained_cleanup")),
                    16, "content cannot be satisfied by absence")
            require(mode == "delete" or "retained_cleanup" not in outcomes, 16, "invalid retention permission")
            require(not final_keyboard or mode == "delete", 12, "content follows action keyboard")
            buttons = frame.get("buttons")
            if buttons:
                require(frame["frame"] in ("card", "repost"), 12, "frame cannot carry keyboard")
                require(slot == action and mode in ("send", "edit") and index == max(
                    i for i, f in enumerate(frames) if f["mode"] != "delete"), 12, "action keyboard must be last")
                require(isinstance(buttons, list) and len(buttons) <= 100, 16, "invalid keyboard")
                for row in buttons:
                    require(isinstance(row, list) and 0 < len(row) <= 8, 16, "invalid keyboard row")
                    for button in row:
                        require(isinstance(button, dict) and frozenset(button) == {"text", "callback_data"} and
                                isinstance(button["text"], str) and bool(button["text"]) and
                                isinstance(button["callback_data"], str) and 0 < len(button["callback_data"].encode()) <= 64,
                                16, "invalid button")
                final_keyboard = True
            target = frame.get("target", {"message_id": frame.get("message_id")})
            require(mode != "send" or frame.get("target") is None and frame.get("message_id") is None, 16, "send target must be null")
            if mode != "send":
                require(isinstance(target, dict) and (frozenset(target) == {"message_id"} and positive(target["message_id"]) or
                        frozenset(target) == {"stage_id"} and target["stage_id"] in ids[:-1]), 16, "invalid target")
            if mode in ("retire", "delete"):
                require("text" not in frame and "photo" not in frame and not buttons, 16, "retirement must preserve body")
                if mode == "retire" and slot == action:
                    require(target == {"message_id": current["message_id"]}, 12, "retirement targets wrong action")
                    retired = True
            else:
                text = frame.get("text", "")
                require((0 if frame.get("photo") else 1) <= rendered_length(text, frame.get("parse_mode")) <= (1024 if frame.get("photo") else 4096),
                        16, "render overflow")
            protection = frame.get("protection")
            if mode == "delete":
                require(isinstance(protection, dict) and protection.get("protected_guidance") is False,
                        23, "protected or unknown cleanup")
                require(retired and any(f.get("frame") == "repost" and f["mode"] == "send" for f in frames[:index]),
                        23, "cleanup requires retired replacement")
            existing = state["slots"].get(slot, {})
            old_protection = existing.get("protection", {})
            if mode == "delete" or (mode == "edit" and old_protection.get("protected_guidance", True)):
                if mode == "edit":
                    require(not protection or protection.get("protected_guidance") is not False,
                            23, "protection cannot be downgraded")
                    native_update = "sealed_payload" in frame and slot != action
                    if native_update:
                        require(protection and protection.get("body_digest") == digest(frame["sealed_payload"]["content"]),
                                23, "native content protection mismatch")
                    require(native_update or not existing.get("body") or frame.get("text") == existing["body"],
                            23, "protected body substitution")
                else:
                    require(protection.get("message_id") == target.get("message_id") and
                            protection.get("chat_id") == state["chat_id"] and
                            protection.get("body_digest") == existing.get("body_digest") and
                            old_protection.get("protected_guidance") is False, 23, "cleanup protection mismatch")
            if mode in ("edit", "retire"):
                edits[slot] = edits.get(slot, 0) + 1
                require(edits[slot] <= 2, 12, "slot edit budget")
                require(target == {"message_id": existing["message_id"]}, 12, "edit must target current slot")
            if mode == "send":
                name = frame["frame"]
                sends[name] = sends.get(name, 0) + 1
                if name == "card":
                    # DGN-2424: a card is born by the surface open or by the
                    # owner turn whose command the kit admitted (the kit binds
                    # every stage to that one envelope); taps and dues never
                    # create a card. Either way only into an empty slot.
                    require(kind in ("open", "turn"), 12, "card requires open or turn envelope")
                    require(existing["message_id"] is None and
                            sum(f["mode"] == "send" and f.get("slot", action) == slot for f in frames[:index]) == 0,
                            12, "slot already occupied")
                elif name == "answer":
                    require(kind == "turn" and sends[name] == 1 and not buttons, 12, "answer budget")
                elif name == "repost":
                    require(kind in ("tap", "due", "turn") and slot == action and retired and sends[name] == 1,
                            12, "repost budget or retirement")
                elif name == "end":
                    require(kind == "close" and sends[name] <= 3 and not buttons, 12, "end budget")
                else:
                    require(kind in ("close", "tap", "turn") and sends[name] == 1 and not buttons, 12, "close budget")
                if current["has_keyboard"] and not (kind == "open"):
                    require(retired, 12, "content requires prior retirement")
            if frame.get("photo"):
                require(mode == "send" and frame["frame"] in ("answer", "end"), 16, "invalid photo stage")
            if frame["frame"] == "answer" and current["message_id"]:
                require(any(f["frame"] == "repost" and f["mode"] == "send" for f in frames[index + 1:]),
                        12, "answer requires action repost")
            notify_class = ("live_repost" if frame["frame"] == "repost" and kind == "due" else
                            "live_card" if frame["frame"] in ("card", "repost") else "live_" + frame["frame"])
            require(frame.get("notification_class") == notify_class and
                    (frame.get("silent") is self.notification_policy(notify_class) if mode == "send" else frame.get("silent") is True), 16, "notification policy mismatch")
        require(kind != "turn" or sends.get("answer", 0) + sends.get("close", 0) <= 1, 12, "combined owner answer budget")
        require(kind != "due" or sends.get("repost", 0) == 1, 12, "due requires action repost")
        if not branch_path:
            return self._branches(request["branch_manifest"], frames, state)

    def _branches(self, manifest, frames, state):
        require(isinstance(manifest, dict) and frozenset(manifest) == {"schema", "groups"} and
                manifest["schema"] == "delivery_branches/1" and isinstance(manifest["groups"], list),
                16, "invalid branch manifest")
        positions = {f["stage_id"]: i for i, f in enumerate(frames)}
        require(len(positions) == len(frames), 16, "duplicate stage identity")
        by_id = {f["stage_id"]: f for f in frames}
        members, group_ids, prior_position = {}, [], -1
        fields = {"group_id", "default_choice_id", "replacement_choice_id", "choices", "default_when",
                  "replacement_when", "unselected_outcome"}
        for group in manifest["groups"]:
            require(isinstance(group, dict) and frozenset(group) == fields, 16, "invalid branch group")
            gid, default, replacement = (group[k] for k in ("group_id", "default_choice_id", "replacement_choice_id"))
            require(all(isinstance(v, str) and v for v in (gid, default, replacement)) and
                    gid not in group_ids and default != replacement, 16, "invalid branch identity")
            group_ids.append(gid)
            require(group["unselected_outcome"] == "branch_not_selected" and
                    isinstance(group["choices"], list) and len(group["choices"]) == 2, 16, "invalid branch choices")
            first = len(frames)
            for choice, name in zip(group["choices"], (default, replacement)):
                require(frozenset(choice) == {"choice_id", "stage_ids"} and choice["choice_id"] == name and
                        isinstance(choice["stage_ids"], list) and bool(choice["stage_ids"]), 16, "invalid choice")
                ids = choice["stage_ids"]
                require(all(i in positions and i not in members for i in ids) and len(frozenset(ids)) == len(ids) and
                        ids == sorted(ids, key=positions.get), 16, "foreign or overlapping branch members")
                first = min(first, positions[ids[0]])
                for sid in ids:
                    members[sid] = (gid, name)
            require(first > prior_position, 16, "unordered branch groups")
            prior_position = first
            default_when, replacement_when = group["default_when"], group["replacement_when"]
            require(frozenset(default_when) == {"stage_id", "results"} and default_when["results"] == ["ok", "unchanged"] and
                    default_when["stage_id"] in group["choices"][0]["stage_ids"], 16, "invalid default predicate")
            final = by_id[default_when["stage_id"]]
            require(final["mode"] == "edit" and final.get("slot", state["action_slot"]) == state["action_slot"] and
                    final.get("allowed_outcomes") == ["ok", "unchanged"], 16, "default must deliver final action")
            require(frozenset(replacement_when) == {"stage_ids", "result", "target", "evidence", "quiescence_required"} and
                    replacement_when["result"] == "absent" and replacement_when["evidence"] == "authoritative_target_absence" and
                    replacement_when["quiescence_required"] is True, 16, "invalid absence predicate")
            triggers = replacement_when["stage_ids"]
            require(isinstance(triggers, list) and triggers and len(frozenset(triggers)) == len(triggers) and
                    all(i in by_id for i in triggers) and triggers == sorted(triggers, key=positions.get), 16, "invalid absence triggers")
            for sid in triggers:
                trigger = by_id[sid]
                require(trigger["mode"] == "retire" or sid == final["stage_id"], 16, "invalid absence trigger mode")
                require(trigger.get("target", {"message_id": trigger.get("message_id")}) == replacement_when["target"] and
                        (sid == final["stage_id"] or sid not in members), 16, "wrong absence target")
            require(replacement_when["target"] == {"message_id": state["slots"][state["action_slot"]]["message_id"]},
                    16, "absence target is not current action")
            replacement_frames = [by_id[i] for i in group["choices"][1]["stage_ids"]]
            require(sum(f["mode"] == "send" and f["frame"] == "repost" and f["allowed_outcomes"] == ["ok"]
                        for f in replacement_frames) == 1 and
                    all(f["mode"] == "delete" or f["frame"] == "repost" for f in replacement_frames),
                    16, "replacement must deliver one action")
            for choice in group["choices"]:
                losers = frozenset(i for c in group["choices"] if c != choice for i in c["stage_ids"])
                require(all((by_id[i].get("target") or {}).get("stage_id") not in losers for i in choice["stage_ids"]),
                        16, "branch depends on losing transport")
        return members

    async def _select(self, state, path, plan, stage):
        sid, status = stage["frame"]["stage_id"], stage["status"]
        for group in plan["request"]["branch_manifest"]["groups"]:
            existing = plan.setdefault("branch_selections", {}).get(group["group_id"])
            if existing:
                continue
            default, replacement = group["default_when"], group["replacement_when"]
            choice = None
            if sid == default["stage_id"] and status in default["results"]:
                choice = group["default_choice_id"]
            elif sid in replacement["stage_ids"] and status == "absent" and stage.get("recovery"):
                choice = group["replacement_choice_id"]
            if choice is None:
                continue
            receipt = stage["receipt"]
            export = self._export_receipt(state, plan, receipt)
            plan.setdefault("trigger_receipts", {})[digest(export)] = copy.deepcopy(receipt)
            delivery = plan["delivery"]
            selection = dict(schema="delivery_branch_selection/1", S=delivery["S"], D=delivery["D"],
                             plan_digest=plan["plan_digest"], group_id=group["group_id"], choice_id=choice,
                             trigger={k: receipt[k] for k in ("K", "seq", "result", "payload_digest", "target", "evidence_digest")},
                             lease_fence=delivery["L"]["lease_fence"])
            selection["trigger"]["receipt_digest"] = digest(export)
            selection["selection_digest"] = digest(selection)
            selection["auth"] = dict(publisher_id=state["owner"], mac=mac(self.key(state["owner"]), selection))
            # Persist intent before the kit call. A lost reply retries this exact
            # selection, including the old fence; it never chooses again.
            plan.setdefault("selection_intents", {})[group["group_id"]] = selection
            atomic(path, plan)
            await self._persist_selection(state, path, plan, selection)

    async def _persist_selection(self, state, path, plan, selection):
        delivery = plan["delivery"]
        args = {k: delivery[k] for k in ("S", "P", "D", "L")}
        args["selection"] = selection
        result = await self._service("delivery_select", args, state["owner"])
        require(result.get("selection_digest") == selection["selection_digest"], 16, "selection acknowledgement mismatch")
        plan.setdefault("branch_selections", {})[selection["group_id"]] = selection
        atomic(path, plan)
        group = next(g for g in plan["request"]["branch_manifest"]["groups"] if g["group_id"] == selection["group_id"])
        losing = next(c for c in group["choices"] if c["choice_id"] != selection["choice_id"])["stage_ids"]
        for stage in plan["stages"]:
            if stage["frame"]["stage_id"] not in losing or stage["status"] == "branch_not_selected":
                continue
            require(stage["status"] not in ("reserved", "uncertain", "ok", "unchanged"), 13, "losing attempt unresolved")
            stage["seq"] += 1
            receipt = self._receipt(state, plan, stage, "branch_not_selected", None)
            receipt.pop("auth")
            receipt["evidence_digest"] = selection["trigger"]["evidence_digest"]
            receipt["selection_digest"] = selection["selection_digest"]
            receipt["auth"] = mac(self._receipt_key(state, plan), receipt)
            stage.update(status="branch_not_selected", receipt=receipt, kit_ack=False)
            stage.setdefault("no_io_receipts", []).append(receipt)
            self._export_receipt(state, plan, receipt)
            atomic(path, plan)

    @boundary
    async def submit_frames(self, chat, request, execute=True):
        require(isinstance(request, dict) and frozenset(request) == {"surface", "epoch", "op", "validation", "delivery", "frames", "branch_manifest"},
                16, "invalid frame request")
        require(positive(request["epoch"]) and isinstance(request["surface"], str) and
                isinstance(request["op"], str) and bool(request["op"]), 16, "invalid frame identity types")
        async with self.lock(chat):
            state = self._surface(chat, request["surface"], request["epoch"], request["op"])
            control = self.control(chat)
            require(control["barrier"] is None, 13, "chat barrier pending")
            activation = self.activation(state["owner"])
            require(activation and activation["phase"] == "active" and activation["generation"] == state["generation"],
                    19, "activation generation fenced")
            path = self.plan_path(chat, state["epoch"], request["op"])
            plan = read(path)
            if plan:
                original = plan["request"]
                # L may renew/reacquire; all immutable bindings remain exact.
                stable_old, stable_new = copy.deepcopy(original), copy.deepcopy(request)
                for value in (stable_old, stable_new):
                    value["delivery"].pop("L")
                    value["validation"].pop("auth")
                require(stable_old == stable_new, 2, "sealed plan changed")
                self._auth(request, state)
                if plan["complete"]:
                    return 11, self._results(plan)
                plan["delivery"] = request["delivery"]
            else:
                self._auth(request, state)
                binding = self._binding(request, state)
                try:
                    self._preflight(request, state)
                except Refusal as error:
                    if error.rc == 16:
                        diagnostic_path = self.base / "refusals" / str(chat) / (hashlib.sha256(request["op"].encode()).hexdigest() + ".json")
                        atomic(diagnostic_path, dict(request=request, diagnostic=error.result, pending=True))
                    raise
                # Any earlier unresolved frame owns the chat until reconciliation.
                require(not any(not p["complete"] for _, p in self.plans(chat)), 13, "earlier frame unresolved")
                plan = dict(request=copy.deepcopy(request), delivery=copy.deepcopy(request["delivery"]),
                            plan_digest=request["validation"]["plan_digest"], command_binding=binding, complete=False,
                            stages=[], kit_acknowledgements=[], completion_receipt=None)
                snapshots = []
                for frame in request["frames"]:
                    snapshot = copy.deepcopy(frame)
                    if snapshot.get("photo"):
                        snapshot["photo"] = self._photo(path, snapshot["photo"])
                    snapshots.append(snapshot)
                await self._lease(plan)
                preimage = plan_preimage(request)
                await self._service("delivery_bind", dict(S=request["delivery"]["S"], P=request["delivery"]["P"],
                    D=request["delivery"]["D"], L=request["delivery"]["L"], plan_digest=plan["plan_digest"],
                    **{k: preimage[k] for k in ("frame_digest", "receipt_digest", "stages", "branch_manifest", "budgets")}), state["owner"])
                plan["bind_preimage"] = preimage
                plan["bind_bytes"] = canonical(preimage).decode("utf-8")
                remote = await self._service("delivery_read", {k: request["delivery"][k] for k in ("S", "P", "D")}, state["owner"])
                require(remote.get("plan_digest") == plan["plan_digest"] and
                        remote.get("receipt_digest") == request["validation"]["receipt_digest"] and
                        remote.get("branch_manifest") == request["branch_manifest"], 17, "kit binding mismatch")
                for frame, snapshot in zip(request["frames"], snapshots):
                    # Compare the kit's frozen B to each exact original stage.
                    K = dict(chat_id=chat, epoch=state["epoch"], op=request["op"], stage_id=frame["stage_id"])
                    bound = next((s for s in remote.get("stages", []) if s.get("B", {}).get("K") == K), None)
                    require(bound and bound["B"] == dict(K=K, D=request["delivery"]["D"],
                            plan_digest=plan["plan_digest"], payload_digest=sealed_payload_digest(frame)), 17, "kit stage binding mismatch")
                    plan["stages"].append(dict(frame=snapshot, payload_digest=sealed_payload_digest(frame), status="pending", seq=0, attempts=[], kit_ack=False))
                atomic(path, plan)
            if not execute:
                return 0, dict(ok=True, plan_digest=plan["plan_digest"], sealed=True)
            return await self._drive(chat, state, control, path, plan)

    @boundary
    async def verify(self, chat, request):
        """Validate and seal a complete authenticated plan without transport."""
        return await self.submit_frames(chat, request, execute=False)

    def _sealed_stage(self, chat, surface, epoch, op, stage_id):
        state = self._surface(chat, surface, epoch, op)
        path = self.plan_path(chat, epoch, op)
        plan = read(path)
        require(plan and not plan.get("maintenance"), 17, "authenticated sealed domain plan required")
        self._auth(plan["request"], state)
        self._binding(plan["request"], state)
        stage = next((item for item in plan["stages"] if item["frame"]["stage_id"] == stage_id), None)
        require(stage is not None, 16, "stage absent from sealed plan")
        return state, path, plan, stage

    @boundary
    async def reserve(self, chat, surface, epoch, op, stage_id, frame, mode, slot=None,
                      message_id=None, keyboard=False, photo=False):
        async with self.lock(chat):
            state, path, plan, stage = self._sealed_stage(chat, surface, epoch, op, stage_id)
            expected = stage["frame"]
            require(expected["frame"] == frame and expected["mode"] == mode and
                    expected.get("slot", state["action_slot"]) == (slot or state["action_slot"]) and
                    bool(expected.get("buttons")) is keyboard and bool(expected.get("photo")) is photo,
                    16, "reserve descriptor differs from sealed stage")
            target = None if mode == "send" else expected.get("target", {"message_id": expected.get("message_id")})
            if mode != "send":
                require(target == {"message_id": message_id}, 16, "reserve target differs from sealed stage")
            control = self.control(chat)
            require(control["barrier"] is None and self.now() >= control["not_before_ms"], 24, "chat transport pending")
            if stage["status"] in SATISFIED:
                return 11, copy.deepcopy(stage["receipt"])
            require(stage["status"] not in ("reserved", "uncertain") or
                    mode != "send" and stage["status"] == "uncertain", 13, "reserved stage needs recovery")
            require(not any(s is not stage and s["status"] in ("reserved", "uncertain") for s in plan["stages"]),
                    13, "earlier stage uncertain")
            require(all(s["status"] in SATISFIED and s["kit_ack"] for s in plan["stages"][:plan["stages"].index(stage)]),
                    24, "prior stage acknowledgement pending")
            require(self.now() >= stage.get("next_attempt_at_ms", 0), 24, "stage backoff pending")
            await self._lease(plan, minimum_remaining_ms=17000)
            self._reserve_attempt(state, path, plan, stage, target)
            return 0, dict(ok=True, seq=stage["seq"], silent=expected["silent"])

    def _reserve_attempt(self, state, path, plan, stage, target):
        require(stage.get("resolved_target", target) == target, 16, "resolved target changed")
        stage["resolved_target"] = target
        stage["seq"] += 1
        stage["status"] = "reserved"
        stage["attempts"].append(dict(seq=stage["seq"], status="reserved", lease_fence=self._attempt_fence(plan),
                                      started_at_ms=self.now(), target=target, io_authorized=False))
        atomic(path, plan)
        self.journal(state, "reserve", op=plan["request"]["op"], stage_id=stage["frame"]["stage_id"], seq=stage["seq"])

    @boundary
    async def commit(self, chat, op, stage_id, seq, result, message_id=None, evidence=None):
        # A raw caller cannot mint success, nonacceptance, absence or backoff.
        # Actual transport/recovery has already durably authenticated the receipt.
        async with self.lock(chat):
            matches = [(path, plan) for path, plan in self.plans(chat) if plan["request"]["op"] == op]
            require(len(matches) == 1, 2, "unknown or ambiguous operation")
            path, plan = matches[0]
            state = self.state(chat)
            require(state and state["epoch"] == plan["request"]["epoch"], 10, "commit epoch fenced")
            stage = next((item for item in plan["stages"] if item["frame"]["stage_id"] == stage_id), None)
            require(stage and positive(seq) and stage["seq"] == seq, 16, "commit sequence mismatch")
            receipt = stage.get("receipt")
            require(receipt and receipt["seq"] == seq, 17, "coordinator transport evidence required")
            unsigned = {key: value for key, value in receipt.items() if key != "auth"}
            require(hmac.compare_digest(mac(self._receipt_key(state, plan), unsigned), receipt.get("auth", "")),
                    17, "commit receipt authentication invalid")
            require(receipt["result"] == result and receipt["message_id"] == message_id,
                    16, "commit contradicts transport receipt")
            if evidence is not None:
                require(stage.get("recovery") and evidence == stage.get("recovery_record_id"),
                        17, "commit evidence differs from authenticated recovery")
            state["lease_until_ms"] = max(state["lease_until_ms"], self.now() + state.get("lease_s", 5400)*1000)
            self.save_state(state)
            return dict(ok=True)

    @boundary
    async def complete(self, chat, surface, epoch):
        return await self.close(chat, surface, epoch, final=True)

    @boundary
    async def submit_push(self, chat, request, *, surface, epoch, op, stage_id, frame,
                          slot=None, text="", buttons=None, photo="", message_id=None):
        """Project frozen shell flags onto an authenticated one-stage plan."""
        require(isinstance(request, dict) and request.get("surface") == surface and
                request.get("epoch") == epoch and request.get("op") == op and
                len(request.get("frames", [])) == 1, 16, "session flags differ from sealed plan")
        sealed = request["frames"][0]
        state = self.state(chat)
        resolved_slot = slot or (state["action_slot"] if state else "card")
        target = sealed.get("target", {"message_id": sealed.get("message_id")})
        require(sealed.get("stage_id") == stage_id and sealed.get("frame") == frame and
                sealed.get("slot", resolved_slot) == resolved_slot and sealed.get("text", "") == text and
                sealed.get("buttons", []) == (buttons or []) and
                (sealed.get("photo") or {}).get("path", "") == photo and
                sealed.get("mode") == ("edit" if message_id else "send") and
                (not message_id or target == {"message_id": message_id}),
                16, "session content differs from sealed plan")
        rc, result = await self.submit_frames(chat, request)
        if rc not in (0, 11):
            return rc, result
        receipt = result["results"][0]
        return rc, dict(ok=True, message_id=receipt["message_id"], chat_id=chat,
            edited=sealed["mode"] == "edit", unchanged=receipt["result"] == "unchanged",
            surface=surface, epoch=epoch, slot=resolved_slot, op=op, stage_id=stage_id,
            seq=receipt["seq"], silent=sealed["silent"])

    async def _drive(self, chat, state, control, path, plan):
        require(plan["request"]["op"] not in control.get("cancelled_ops", {}), 10, "due group cancelled before transport")
        self._recover_bindings(state, plan)
        for selection in list(plan.get("selection_intents", {}).values()):
            await self._persist_selection(state, path, plan, selection)
        for candidate in plan["stages"]:
            await self._select(state, path, plan, candidate)
        for stage in plan["stages"]:
            if stage["status"] in SATISFIED:
                if plan.get("maintenance"):
                    stage["kit_ack"] = True
                if not stage["kit_ack"]:
                    atomic(path, plan)
                    return 24, self._results(plan)
                continue
            if stage["status"] == "reserved":
                stage["status"] = "uncertain" if stage["attempts"][-1].get("io_authorized", True) else "pending"
                if stage["status"] == "uncertain":
                    receipt = self._receipt(state, plan, stage, "uncertain", None)
                    stage["receipt"] = receipt
                    stage["attempts"][-1].update(status="uncertain", receipt=receipt)
                atomic(path, plan)
            mode = stage["frame"]["mode"]
            if stage["status"] == "uncertain" and mode == "send":
                return 13, self._results(plan)
            if stage.get("transient_errors", 0) >= 3:
                return 13, self._results(plan)
            if self.now() < max(control["not_before_ms"], stage.get("next_attempt_at_ms", 0)):
                return 24, self._results(plan)
            require(all(s["kit_ack"] for s in plan["stages"] if s["status"] == "branch_not_selected"),
                    24, "losing branch journal pending")
            if not plan.get("maintenance"):
                await self._lease(plan)
            rc = await self._execute(chat, state, control, path, plan, stage)
            if rc:
                return rc, self._results(plan)
            await self._select(state, path, plan, stage)
            # Transport truth persists before the external journal acknowledgement.
            if plan.get("maintenance"):
                stage["kit_ack"] = True
                atomic(path, plan)
                continue
            return 24, self._results(plan)
        if plan.get("maintenance"):
            plan["completion_receipt"] = {"complete": True}
        if not plan["completion_receipt"]:
            return 24, self._results(plan)
        plan["complete"] = True
        atomic(path, plan)
        return 0, self._results(plan)


    def _photo(self, plan_path, supplied):
        require(isinstance(supplied, dict) and frozenset(supplied) == {"path", "digest"}, 16, "photo snapshot required")
        path = Path(supplied["path"])
        path = path if path.is_absolute() else self.root / path
        require(path.resolve().is_relative_to(self.root), 16, "photo outside instance")
        raw = path.read_bytes()
        require(0 < len(raw) <= 10 * 1024 * 1024 and
                (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")), 16, "unsupported photo")
        width, height = photo_dimensions(raw)
        require(width > 0 and height > 0 and width + height <= 10000 and max(width, height) <= min(width, height) * 20,
                16, "photo dimensions refused")
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        require(actual == supplied["digest"], 16, "photo digest mismatch")
        plan_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = plan_path.parent / (actual.split(":")[1] + ".photo")
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600) if not target.exists() else None
        if fd is not None:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        require(hashlib.sha256(target.read_bytes()).hexdigest() == actual.split(":")[1], 40, "photo snapshot corrupt")
        return dict(path=str(target), digest=actual)

    def _receipt_key(self, state, plan):
        if not plan.get("maintenance"):
            return self.key(state["owner"])
        path = self.base / "coordinator.key"
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        return path.read_bytes()

    @staticmethod
    def _attempt_fence(plan):
        return plan["coordinator_fence"] if plan.get("maintenance") else plan["delivery"]["L"]["lease_fence"]

    def _receipt(self, state, plan, stage, result, message_id, **extra):
        frame = stage["frame"]
        receipt = dict(K=dict(chat_id=state["chat_id"], epoch=state["epoch"], op=plan["request"]["op"],
                              stage_id=frame["stage_id"]), D=None if plan.get("maintenance") else plan["delivery"]["D"], plan_digest=plan["plan_digest"],
                       seq=stage["seq"], message_id=message_id, target=stage.get("resolved_target", frame.get("target", {"message_id": frame.get("message_id")}))
                       if frame["mode"] != "send" else None,
                       result=result, payload_digest=stage["payload_digest"], lease_fence=(
                           stage["attempts"][-1]["lease_fence"] if result != "branch_not_selected" and stage["attempts"] and
                           stage["attempts"][-1]["seq"] == stage["seq"] else self._attempt_fence(plan)),
                       evidence_digest=None, selection_digest=None, retry_after=None, next_attempt_at_ms=None,
                       silent=frame["silent"], **extra)
        receipt["auth"] = mac(self._receipt_key(state, plan), receipt)
        return receipt

    def _export(self, publisher, kind, value, identity=None):
        exported = authenticated_export(self.key(publisher), kind, value)
        directory = self.base / "kit-exports" / publisher
        aliases = [digest(value)]
        if kind == "receipt":
            aliases.append(digest(exported))
        elif identity is not None:
            aliases = [digest(identity)]
        for alias in aliases:
            private_bytes(directory / (kind + "-" + alias.split(":")[1] + ".json"), canonical(exported))
        return exported

    def _export_receipt(self, state, plan, receipt):
        fields = ("K", "seq", "message_id", "target", "result", "payload_digest", "lease_fence",
                  "evidence_digest", "retry_after", "next_attempt_at_ms", "selection_digest")
        return self._export(state["owner"], "receipt", {k: receipt[k] for k in fields})

    async def _execute(self, chat, state, control, path, plan, stage):
        frame = stage["frame"]
        if "sealed_payload" in frame:
            require(sealed_payload_digest(frame) == stage["payload_digest"], 17, "native checkpoint seal changed")
            frame = checked_native_frame(frame)
        for dependency in frame.get("depends_on", []):
            prior_stage = next(s for s in plan["stages"] if s["frame"]["stage_id"] == dependency)
            require(prior_stage["status"] in SATISFIED and prior_stage["kit_ack"], 24, "stage dependency pending")
        target = None if frame["mode"] == "send" else frame.get("target", {"message_id": frame.get("message_id")})
        if target and "stage_id" in target:
            prior = next(s for s in plan["stages"] if s["frame"]["stage_id"] == target["stage_id"])
            require(prior["status"] in SATISFIED and positive(prior.get("receipt", {}).get("message_id")), 24, "target dependency pending")
            target = {"message_id": prior["receipt"]["message_id"]}
        self._reserve_attempt(state, path, plan, stage, target)
        if not plan.get("maintenance") and self.attempt_started is not None:
            B = dict(K=dict(chat_id=chat, epoch=state["epoch"], op=plan["request"]["op"], stage_id=frame["stage_id"]),
                     D=plan["delivery"]["D"], plan_digest=plan["plan_digest"], payload_digest=stage["payload_digest"])
            started = self.attempt_started(B, stage["seq"])
            if inspect.isawaitable(started):
                await started
        # Authorization is checked at the last point before bounded I/O.
        if not plan.get("maintenance"):
            await self._lease(plan, minimum_remaining_ms=17000)
        if not plan.get("maintenance") and self.before_io is not None:
            checked = self.before_io()
            if inspect.isawaitable(checked):
                await checked
        if state.get("owner"):
            activation = self.activation(state["owner"])
            require(activation and activation["generation"] == state["generation"] and activation["phase"] == "active",
                    19, "generation changed")
        require(self.transport is not None, 40, "transport unavailable")
        stage["attempts"][-1]["io_authorized"] = True
        atomic(path, plan)
        payload = dict(frame=frame, chat_id=chat, target=target, timeout_s=15,
                       K=dict(chat_id=chat, epoch=state["epoch"], op=plan["request"]["op"], stage_id=frame["stage_id"]),
                       seq=stage["seq"], lease_fence=self._attempt_fence(plan))
        try:
            with authorized_io(chat):
                response = self.transport(payload)
                if inspect.isawaitable(response):
                    response = await asyncio.wait_for(response, timeout=16)
        except (Exception, asyncio.CancelledError) as error:
            stage["status"] = "uncertain"
            receipt = self._receipt(state, plan, stage, "uncertain", None)
            stage["receipt"] = receipt
            stage["attempts"][-1].update(status="uncertain", diagnostic=type(error).__name__, receipt=receipt)
            if not plan.get("maintenance"):
                self._export_receipt(state, plan, receipt)
            atomic(path, plan)
            if isinstance(error, asyncio.CancelledError):
                raise
            return 13
        result, message_id = self._classify(response, frame, target, chat)
        gone = result == "failed" and bool(plan.get("transition_plan")) and frame["mode"] == "retire" \
            and _message_gone(response)
        if gone:
            result, message_id = "absent", None
        extra = {}
        native = None
        if result == "failed" and frame["mode"] == "delete" and response.get("error_code") in (400, 403) and any(
                wording in response.get("description", "").lower() for wording in ("message cannot be deleted", "message can't be deleted")):
            require(frame.get("protection", {}).get("protected_guidance") is False and
                    "retained_cleanup" in frame["allowed_outcomes"] and
                    any(s["frame"]["mode"] == "retire" and s["status"] in ("ok", "unchanged", "absent") and s["kit_ack"]
                        for s in plan["stages"]) and
                    any(s["frame"]["frame"] == "repost" and s["status"] == "ok" and s["kit_ack"]
                        for s in plan["stages"]), 23, "native cleanup retention unproven")
            result = "retained_cleanup"
            native = dict(permanent_delete_prohibition=True, raw_response=response.get("raw_response", canonical(response).decode()))
        if result == "rate_limited":
            parameters = response.get("parameters")
            wait = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if type(wait) is not int or wait < 0:
                wait = 60
                stage["backoff_diagnostic"] = "invalid retry_after; conservative backoff"
            next_at = self.now() + wait * 1000
            stage["next_attempt_at_ms"] = next_at
            control["not_before_ms"] = max(control["not_before_ms"], next_at)
            self.save_control(chat, control)
            retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if type(retry) is not int or retry < 0:
                retry = None
            extra = dict(retry_after=retry, next_attempt_at_ms=next_at)
            native = dict(response_received_at_ms=self.now(), retry_after=retry,
                          raw_response=response.get("raw_response", canonical(response).decode()))
            if retry is None:
                native["diagnostic"] = stage["backoff_diagnostic"]
        elif result in ("failed", "uncertain") and frame["mode"] != "send":
            stage["transient_errors"] = stage.get("transient_errors", 0) + 1
        if result in SATISFIED and not gone:
            require(result in frame["allowed_outcomes"], 16, "transport outcome not permitted")
        stage["status"] = result
        stage["raw_response"] = response
        receipt = self._receipt(state, plan, stage, result, message_id)
        receipt.update(extra)
        if gone:
            # Kit-accepted absent receipt: the Telegram observation is the
            # recovery evidence; the authority seat authenticates the record.
            evidence = dict(method="editMessageReplyMarkup", error=response["description"], chat_id=chat,
                            message_id=target["message_id"], observed_at_ms=self.now())
            receipt.pop("auth")
            receipt["evidence_digest"] = digest(evidence)
            receipt["auth"] = mac(self._receipt_key(state, plan), receipt)
            stage["absent_evidence"] = evidence
        if native is not None:
            evidence = dict(K=receipt["K"], seq=receipt["seq"], plan_digest=plan["plan_digest"],
                            payload_digest=stage["payload_digest"], target=target, result=result, observation=native)
            receipt.pop("auth")
            receipt["evidence_digest"] = digest(evidence)
            receipt["auth"] = mac(self._receipt_key(state, plan), receipt)
            stage["native_evidence"] = evidence
            if not plan.get("maintenance"):
                self._export(state["owner"], "evidence", evidence)
        stage["receipt"] = receipt
        stage["attempts"][-1].update(status=result, receipt=receipt, response_received_at_ms=self.now())
        if not plan.get("maintenance"):
            self._export_receipt(state, plan, receipt)
        atomic(path, plan)
        self._recover_bindings(state, plan)
        self.journal(state, "commit", op=plan["request"]["op"], stage_id=frame["stage_id"],
                     seq=stage["seq"], result=result)
        return 0 if result in SATISFIED else 24 if result == "rate_limited" else 13

    @staticmethod
    def _classify(response, frame, target, chat=None):
        if not isinstance(response, dict):
            return "uncertain", None
        if response.get("ok") is True:
            result = response.get("result")
            if frame["mode"] in ("retire", "delete") and result is True:
                return "ok", target.get("message_id")
            if isinstance(result, dict) and positive(result.get("message_id")):
                if "chat" in result and (not isinstance(result["chat"], dict) or
                        type(result["chat"].get("id")) is not int or result["chat"]["id"] != chat):
                    return "uncertain", None
                if frame["mode"] == "edit" and result["message_id"] != target.get("message_id"):
                    return "uncertain", None
                return "ok", result["message_id"]
            return "uncertain", None
        if response.get("ok") is not False or type(response.get("error_code")) is not int:
            return "uncertain", None
        if response["error_code"] == 429:
            return "rate_limited", None
        if response["error_code"] not in (400, 401, 403, 404):
            return "uncertain", None
        description = response.get("description", "")
        if not isinstance(description, str):
            return "uncertain", None
        description = description.lower()
        if frame["mode"] in ("edit", "retire") and "message is not modified" in description:
            return "unchanged", target.get("message_id")
        # Target absence and permanent cleanup require authenticated recovery;
        # a generic 400/403 never proves either terminal alternative.
        return "failed", None

    def _recover_bindings(self, state, plan):
        changed = False
        for stage in plan["stages"]:
            receipt_key = digest([plan["request"]["op"], stage["frame"]["stage_id"], stage["seq"]])
            if receipt_key in state.get("applied_receipts", []) or stage["status"] not in ("ok", "unchanged", "absent", "retained_cleanup"):
                continue
            frame = stage["frame"]
            slot = state["slots"].get(frame.get("slot", state["action_slot"]), {})
            if frame["mode"] == "retire":
                slot["has_keyboard"] = False
            elif stage["status"] in ("ok", "unchanged") and frame["mode"] in ("send", "edit") and frame["frame"] in ("card", "repost", "close"):
                old = slot["message_id"]
                new = stage["receipt"]["message_id"]
                if old and old != new and old not in slot["retired_ids"]:
                    slot["retired_ids"].append(old)
                slot.update(message_id=new, has_keyboard=bool(frame.get("buttons")), body=frame.get("text", ""),
                            buttons=frame.get("buttons"), parse_mode=frame.get("parse_mode"), body_digest=digest(frame["sealed_payload"]["content"] if "sealed_payload" in frame else frame.get("text", "")),
                            protection=frame.get("protection") or {"protected_guidance": True})
                if "sealed_payload" in frame:
                    slot["sealed_payload"] = copy.deepcopy(frame["sealed_payload"])
                else:
                    slot.pop("sealed_payload", None)
                state["commit_order"] += 1
                slot["commit_order"] = state["commit_order"]
            state["lease_until_ms"] = max(state["lease_until_ms"], self.now() + state.get("lease_s", 5400) * 1000)
            ledger = self.root / ".telegram_bot/outbound-ledger.jsonl"
            row = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now() / 1000)),
                       chat_id=str(state["chat_id"]), body=frame.get("text", ""), audience="owner",
                       tag="edit" if frame["mode"] in ("edit", "retire") else "send",
                       message_id=str(stage["receipt"]["message_id"]), kind=frame["mode"], silent=frame["silent"],
                       surface=state["surface_id"], epoch=state["epoch"], op=plan["request"]["op"], frame=frame["frame"],
                       slot=frame.get("slot", state["action_slot"]), stage_id=frame["stage_id"], surface_open=state["state"] == "open",
                       source="bridge", receipt_id=receipt_key, commit_order=state["commit_order"])
            existing_rows = ledger.read_text().splitlines() if ledger.exists() else []
            if not any(json.loads(line).get("receipt_id") == receipt_key for line in existing_rows if line.strip()):
                from bridge.notice_spool import append_ledger
                append_ledger(self.root, row, durable=True)
            state.setdefault("applied_receipts", []).append(receipt_key)
            changed = True
        if changed:
            current = self.state(state["chat_id"])
            if state.get("owner") and current and current["epoch"] == state["epoch"]:
                self.save_state(state)

    def _results(self, plan):
        return dict(ok=plan["complete"], complete=plan["complete"], results=[
            dict(stage_id=s["frame"]["stage_id"], slot=s["frame"].get("slot"), seq=s["seq"],
                 message_id=s.get("receipt", {}).get("message_id"), result=s["status"] if s["seq"] else "skipped",
                 receipt=s.get("receipt")) for s in plan["stages"]])

    @boundary
    async def delivery_receipts(self, chat, request, auth):
        async with self.lock(chat):
            plan = self._bound_plan(chat, request, auth)
            return dict(bindings=[dict(K=s["receipt"]["K"], D=plan["delivery"]["D"], plan_digest=plan["plan_digest"],
                                       payload_digest=s["receipt"]["payload_digest"]) for s in plan["stages"] if s.get("receipt")],
                        branch_manifest=plan["request"]["branch_manifest"], branch_selections=list(plan.get("branch_selections", {}).values()),
                        attempts=[dict(K=s.get("receipt", {}).get("K"), **a) for s in plan["stages"] for a in s["attempts"]] +
                        [dict(K=r["K"], seq=r["seq"], lease_fence=r["lease_fence"], status=r["result"], receipt=r)
                         for s in plan["stages"] for r in s.get("no_io_receipts", [])] +
                        [dict(K=r["K"], seq=r["seq"], lease_fence=r["lease_fence"], status=r["result"], receipt=r)
                         for r in plan.get("trigger_receipts", {}).values()
                         if not any(a.get("receipt") == r for s in plan["stages"] for a in s["attempts"])],
                        kit_acknowledgements=plan["kit_acknowledgements"], complete=plan["complete"])

    def _bound_plan(self, chat, request, auth):
        S = request["S"]
        path = self.plan_path(chat, S["epoch"], request["op"])
        plan = read(path)
        require(plan is not None, 2, "unknown plan")
        publisher = plan["delivery"]["P"]["publisher_id"]
        require(hmac.compare_digest(mac(self.key(publisher), request), auth), 17, "export authentication invalid")
        require(S == plan["delivery"]["S"] and request["D"] == plan["delivery"]["D"] and
                request["plan_digest"] == plan["plan_digest"], 16, "delivery binding mismatch")
        return plan

    @boundary
    async def delivery_ack(self, chat, request, auth):
        async with self.lock(chat):
            plan = self._bound_plan(chat, request, auth)
            for item in request["kit_results"]:
                stage = next((s for s in plan["stages"] if s.get("receipt", {}).get("K") == item.get("K")), None)
                require(stage and stage.get("receipt") and item.get("entry_id") and item.get("event_id"),
                        16, "acknowledgement lacks transport result")
                # Checkpoint trust is the installed publisher MAC over exact kit
                # results, never an unauthenticated caller's success boolean.
                stage["kit_ack"] = stage["status"] in SATISFIED
                if item not in plan["kit_acknowledgements"]:
                    plan["kit_acknowledgements"].append(item)
            completion = request.get("completion_receipt")
            if completion is not None:
                require(all(s["status"] in SATISFIED and s["kit_ack"] for s in plan["stages"]), 24, "dependencies pending")
                require(completion.get("complete") is True, 16, "invalid completion")
                plan["completion_receipt"] = completion
                plan["complete"] = True
            else:
                plan["delivery"]["L"] = request["L"]
                await self._lease(plan)
            path = self.plan_path(chat, request["S"]["epoch"], request["op"])
            atomic(path, plan)
            return dict(recorded=True, complete=plan["complete"])

    @boundary
    async def reconcile(self, chat, epoch, op, stage_id, evidence):
        async with self.lock(chat):
            path = self.plan_path(chat, epoch, op)
            plan = read(path)
            require(plan is not None, 2, "unknown plan")
            stage = next((s for s in plan["stages"] if s["frame"]["stage_id"] == stage_id), None)
            require(stage and stage["attempts"], 16, "unknown attempt")
            require(stage["frame"].get("notification_class") != "owner_decision",
                    17, "PO notice requires explicit leader confirmation")
            bundle = read(self.base / "recovery" / (evidence + ".json")) if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", evidence) else None
            require(isinstance(bundle, dict) and frozenset(bundle) == {"record", "evidence"}, 17, "signed recovery evidence required")
            record, observation = bundle["record"], bundle["evidence"]
            require(isinstance(record, dict) and isinstance(observation, dict) and bool(observation), 16, "invalid recovery bundle")
            require(frozenset(record) == {"authority_id", "K", "seq", "plan_digest", "payload_digest", "target",
                    "resolution", "message_id", "evidence_digest", "quiescence", "issued_at_ms", "auth"}, 16, "invalid recovery record")
            require(positive(record["seq"]) and type(record["issued_at_ms"]) is int and 0 <= record["issued_at_ms"] <= self.now() and isinstance(record["K"], dict) and
                    type(record["K"].get("chat_id")) is int and (positive(record["K"].get("epoch")) or
                    plan.get("maintenance") and type(record["K"].get("epoch")) is int and record["K"]["epoch"] == 0),
                    16, "invalid recovery wire types")
            signature = record.get("auth")
            unsigned = {k: v for k, v in record.items() if k != "auth"}
            authority = record.get("authority_id", "")
            require(re.fullmatch(r"[a-z][a-z0-9_]{1,40}", authority), 17, "unknown recovery authority")
            key_path = self.base / "recovery-authorities" / (authority + ".key")
            require(key_path.is_file() and stat.S_IMODE(key_path.stat().st_mode) == 0o600, 17, "recovery authority unavailable")
            require(isinstance(signature, dict) and frozenset(signature) == {"mac"} and
                    isinstance(signature["mac"], str) and len(key_path.read_bytes()) >= 32 and
                    key_path.read_bytes() != self._receipt_key(plan.get("surface_snapshot") or self.state(chat), plan) and
                    hmac.compare_digest(mac(key_path.read_bytes(), unsigned), signature["mac"]),
                    17, "recovery MAC invalid")
            K = dict(chat_id=chat, epoch=epoch, op=op, stage_id=stage_id)
            target = stage.get("resolved_target", stage["frame"].get("target", {"message_id": stage["frame"].get("message_id")})) if stage["frame"]["mode"] != "send" else None
            require(record.get("K") == K and record.get("seq") == stage["seq"] and
                    record.get("plan_digest") == plan["plan_digest"] and record.get("payload_digest") == stage["payload_digest"] and
                    record.get("target") == target, 16, "recovery binding mismatch")
            require(record.get("quiescence") is True and _DIGEST.fullmatch(record.get("evidence_digest", "")),
                    13, "recovery evidence incomplete")
            evidence_path = self.base / "recovery/evidence" / (record["evidence_digest"].split(":")[1] + ".json")
            require(read(evidence_path) == observation, 13, "recovery observation store mismatch")
            require(isinstance(observation, dict) and digest(observation) == record["evidence_digest"] and
                    observation.get("kind") in ("telegram_result", "manual_observation") and observation.get("K") == K and
                    observation.get("seq") == stage["seq"] and observation.get("payload_digest") == stage["payload_digest"] and
                    observation.get("target") == target and observation.get("resolution") == record["resolution"] and
                    observation.get("message_id") == record["message_id"], 13, "exact recovery observation required")
            require(stage["status"] not in SATISFIED, 16, "terminal stage cannot change")
            result = record.get("resolution")
            mode = stage["frame"]["mode"]
            require(result in ("ok", "unchanged", "absent"), 16, "invalid resolution")
            require(target is None or frozenset(target) == {"message_id"} and positive(record["target"]["message_id"]), 16, "invalid recovery target type")
            require(result != "absent" or record["message_id"] is None, 16, "absence must have null message id")
            if result in ("ok", "unchanged"):
                require(mode == "send" or record.get("message_id") == target.get("message_id"), 16, "recovered target mismatch")
                require(positive(record.get("message_id")), 16, "present evidence lacks id")
            state = plan.get("surface_snapshot") or self.state(chat)
            receipt = self._receipt(state, plan, stage, result, record.get("message_id"))
            receipt.pop("auth")
            receipt["evidence_digest"] = digest(bundle)
            receipt["auth"] = mac(self._receipt_key(state, plan), receipt)
            if not plan.get("maintenance"):
                self._export(state["owner"], "evidence", bundle)
                self._export_receipt(state, plan, receipt)
            stage["recovery_record_id"] = evidence
            stage["recovery"] = record
            stage["recovery_evidence"] = observation
            stage["receipt"] = receipt
            stage["status"] = "pending" if result == "absent" and mode == "send" else result
            if plan.get("maintenance") and plan.pop("quarantined", False):
                prefix = f"turn:{chat}:notice_"
                if op.startswith(prefix):
                    control = self.control(chat)
                    notice = control["notices"].get(op[len(prefix):])
                    if notice is not None and notice.get("status") == "quarantined":
                        notice["status"] = "pending"
                        notice.pop("quarantined_at_ms", None)
                        plan["complete"] = False
                        self.save_control(chat, control)
            branch_trigger = any(stage_id in g["replacement_when"]["stage_ids"] for g in plan["request"]["branch_manifest"]["groups"])
            if stage["status"] in SATISFIED and not (result == "absent" and branch_trigger):
                require(result in stage["frame"]["allowed_outcomes"], 16, "unsealed terminal outcome")
            stage["transient_errors"] = 0
            atomic(path, plan)
            self._recover_bindings(state, plan)
            await self._select(state, path, plan, stage)
            self.journal(state, "reconcile", op=op, stage_id=stage_id, result=result)
            return dict(ok=True, op=op, stage_id=stage_id, recorded=result)

    def metrics(self, chat, epoch):
        counters = dict(messages=0, edits=0, notifications=0, end_cards=0, transport_attempts=0,
                        rate_limits=0, retry_wait_ms=0, satisfied_absent=0, retained_cleanup=0)
        for _, plan in self.plans(chat, epoch):
            for stage in plan["stages"]:
                frame = stage["frame"]
                counters["transport_attempts"] += sum(a.get("io_authorized", True) for a in stage["attempts"])
                counters["rate_limits"] += sum(a["status"] == "rate_limited" for a in stage["attempts"])
                counters["retry_wait_ms"] += sum((a.get("receipt", {}).get("retry_after") or 0) * 1000 for a in stage["attempts"])
                if stage["status"] in ("ok", "unchanged"):
                    if frame["mode"] == "send":
                        counters["messages"] += 1
                        counters["notifications"] += int(not frame["silent"])
                        counters["end_cards"] += int(frame["frame"] == "end")
                    elif frame["mode"] == "edit":
                        counters["edits"] += 1
                counters["satisfied_absent"] += int(stage["status"] == "absent")
                counters["retained_cleanup"] += int(stage["status"] == "retained_cleanup")
        return counters

    @boundary
    async def enqueue_notice(self, chat, producer, event_id, payload, notification_class="system"):
        require(isinstance(producer, str) and producer and isinstance(event_id, str) and event_id,
                2, "stable notice identity required")
        if notification_class == "owner_decision":
            self._po_notice_gate(chat, producer, event_id, payload)
        key = hashlib.sha256(canonical([producer, event_id])).hexdigest()
        async with self.lock(chat):
            control = self.control(chat)
            intended = dict(producer=producer, event_id=event_id, payload=payload,
                            notification_class=notification_class)
            retired = self._retired_owner(intended)
            require(not retired, 9, "notice refused: pack %s retired by replacement" % retired)
            prior = control["notices"].get(key)
            if prior:
                require(prior["intended"] == intended, 16, "notice payload changed")
            else:
                if notification_class == "owner_decision":
                    # Disable may have happened while admission awaited chat.
                    self._po_notice_gate(chat, producer, event_id, payload)
                control["notices"][key] = self._notice_record(control, intended, self.state(chat))
                self.save_control(chat, control)
            if notification_class == "owner_decision" and control["notices"][key].get("delivery_state") == "prepared":
                control["notices"][key]["delivery_state"] = "admitted"
                self.save_control(chat, control)
            return dict(ok=True, accepted=True, delivered=bool(prior and prior["status"] == "ok"), notice_id=key)

    @boundary
    async def expire_notices(self, chat, producer, notification_class, admission_orders,
                             expect_count, expect_journal_sha256, reason, apply):
        """Drop an operator-selected, snapshot-bound selection without invoking transport."""
        async with self.lock(chat):
            control_path = self.base / (str(chat) + ".transport.json")
            raw = control_path.read_bytes()
            require(hashlib.sha256(raw).hexdigest() == expect_journal_sha256,
                    17, "notice journal snapshot changed")
            control = self.control(chat)
            low, high = admission_orders
            selected = [(key, notice) for key, notice in control["notices"].items()
                        if (producer is None or notice["intended"]["producer"] == producer) and
                        (notification_class is None or notice["intended"]["notification_class"] == notification_class) and
                        low <= notice.get("admission_order", 0) <= high]
            selected.sort(key=lambda item: (item[1].get("admission_order", 0), item[0]))
            require(len(selected) == expect_count, 17, "notice selection count changed")
            # A delivered or confirmed notice is terminal; expiry never rewrites it.
            require(all(notice["status"] in EXPIRABLE_NOTICE_STATES for _, notice in selected),
                    17, "selection contains a notice that is not expirable")
            entries = [dict(notice_id=key, admission_order=notice.get("admission_order", 0),
                            producer=notice["intended"]["producer"],
                            notification_class=notice["intended"]["notification_class"],
                            status=notice["status"]) for key, notice in selected]
            changed = [item for item in selected if item[1]["status"] != "dropped"]
            if apply and changed:
                stamp = self.now()
                backup = self.base / "backups" / (str(chat) + "-expire-" + str(stamp) + ".transport.json")
                private_bytes(backup, raw)
                for key, notice in changed:
                    epoch = notice.get("epoch", 0)
                    path = self.plan_path(chat, epoch, f"turn:{chat}:notice_{key}")
                    plan = read(path)
                    if plan is not None and any(s["status"] in ("uncertain", "reserved") for s in plan["stages"]):
                        archive = self.base / "archive" / "expired" / str(chat) / str(epoch) / path.name
                        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.replace(path, archive)
                    notice.update(status="dropped", expired_at_ms=stamp, reason=reason)
                self.save_control(chat, control)
            return dict(ok=True, apply=apply, matched=len(selected), changed=len(changed) if apply else 0,
                        journal_sha256=expect_journal_sha256, entries=entries)

    @boundary
    async def push_preview(self, chat, candidate_id, text, buttons, *, silent=False, thread=None):
        """Strict receipt, not admission: one stable candidate, one native message."""
        from bridge.inert_preview import prepare
        payload = prepare(self.root, candidate_id, text, buttons)
        payload.update(disable_notification=silent, link_preview_options={"is_disabled": True})
        if thread is not None:
            require(positive(thread), 5, "invalid preview thread")
            payload["message_thread_id"] = thread
        admission = await self.enqueue_notice(chat, "inert-preview", candidate_id, payload, "inert_preview")
        await self.flush_notices(chat)
        notice = self.control(chat)["notices"][admission["notice_id"]]
        receipt = notice.get("receipt", {})
        results = receipt.get("results", [])
        delivered = notice["status"] == "ok" and len(results) == 1 and \
            results[0]["result"] == "ok" and positive(results[0].get("message_id"))
        result = dict(ok=delivered, delivered=delivered, accepted=True, candidate_id=candidate_id,
                      chat_id=chat, message_id=results[0]["message_id"] if delivered else None,
                      notice_id=admission["notice_id"], receipt=receipt if delivered else None)
        if delivered:
            return 0, result
        epoch = notice.get("epoch", 0)
        plan = read(self.plan_path(chat, epoch, f"turn:{chat}:notice_{admission['notice_id']}"))
        status = plan["stages"][0]["status"] if plan else "pending"
        result["status"] = status
        return (6 if status in ("uncertain", "reserved") else 2 if status == "failed" else 5), result
    def _po_notice_gate(self, chat, producer, event_id, payload):
        """Narrow pack seam, independent of live-session activation."""
        config = read(self.root / ".telegram_bot/po-seat/config.json")
        require((self.root / "config/packs/agent_po.receipt").is_file() and
                isinstance(config, dict) and config.get("enabled") is True,
                35, "PO notice requires installed agent_po and enabled seat configuration")
        require(producer == "agent_po" and re.fullmatch(r"po-decision:[0-9a-f]{64}", event_id),
                16, "PO decision notice requires its custody decision key")
        require(isinstance(payload, dict) and frozenset(payload) in ({"text"}, {"text", "buttons", "po_decision"}) and
                isinstance(payload["text"], str) and 0 < rendered_length(payload["text"], None) <= 4096,
                16, "PO decision requires one bounded text body")
        from bridge.po_decisions import frozen, buttons
        menu = frozen(self.root, event_id.split(":", 1)[1], chat, payload["text"])
        if menu is not None or "po_decision" in payload:
            require(menu is not None and menu == payload.get("po_decision") and
                    payload.get("buttons") == buttons(menu), 16, "PO keyboard differs from frozen menu")

    @boundary
    async def po_notice(self, chat, decision_key, text):
        """Admit the frozen body and return actual coordinator delivery evidence.

        Unknown sends remain held until the leader explicitly confirms them.
        Maintenance publishes the original identity once and never resends.
        """
        from bridge.po_decisions import frozen, buttons
        menu = frozen(self.root, decision_key, chat, text)
        payload = {"text": text}
        if menu:
            payload.update(po_decision=menu, buttons=buttons(menu))
        admitted = await self.enqueue_notice(chat, "agent_po", "po-decision:" + decision_key,
                                             payload, notification_class="owner_decision")
        if not admitted.get("ok"):
            return admitted
        await self.recover_po_notices(chat)
        await self.flush_notices(chat)
        async with self.lock(chat):
            notice = self.control(chat)["notices"][admitted["notice_id"]]
            return dict(ok=True, accepted=True, delivered=notice["status"] == "ok",
                        notice_id=admitted["notice_id"], state=notice.get("delivery_state"),
                        delivery=copy.deepcopy(notice.get("delivery")))

    def _hold_po_notice(self, chat, notice_id, notice, path, plan):
        from routines.lib.return_route import publish_inbox_hold
        stage = plan["stages"][0]
        hold = plan.get("leader_hold")
        if hold is None:
            config = read(self.root / ".telegram_bot/po-seat/config.json") or {}
            leader = config.get("leader_workspace", str(self.root.resolve()))
            require(isinstance(leader, str) and Path(leader).is_absolute(),
                    16, "leader workspace must be absolute")
            identity = dict(K=dict(chat_id=chat, epoch=notice["epoch"], op=plan["request"]["op"],
                                  stage_id=stage["frame"]["stage_id"]),
                            seq=stage["seq"], plan_digest=plan["plan_digest"],
                            payload_digest=stage["payload_digest"], frame=copy.deepcopy(stage["frame"]),
                            attempts=copy.deepcopy(stage["attempts"]))
            hold = dict(schema="po-leader-hold/1", source_root=str(self.root.resolve()),
                        leader_workspace=str(Path(leader).resolve()), notice_id=notice_id, identity=identity)
            hold["hold_id"] = digest(hold).split(":")[1]
            plan["leader_hold"] = hold
            atomic(path, plan)
        notice.update(delivery_state="held", leader_hold=copy.deepcopy(hold))
        # The immutable plan precedes publication. A consumed receipt under the
        # same leader barrier closes the publish/ack crash window.
        publish_inbox_hold(hold["leader_workspace"], hold["hold_id"], hold, "po-notice",
            "[po-notice-held]\nThe original owner notice is held. Never resend automatically. "
            "Inspect the exact delivery identity, confirm the original transport is quiescent, "
            "then use po-notice-confirm from this leader workspace.")

    @boundary
    async def recover_po_notices(self, chat):
        """Recover admitted handoffs after disable; never run an observer/send."""
        held = 0
        async with self.lock(chat):
            control = self.control(chat)
            for notice_id, notice in control["notices"].items():
                if (notice["intended"]["notification_class"] != "owner_decision" or
                        notice["status"] in ("ok", "dropped")):
                    continue
                path = self.plan_path(chat, notice.get("epoch", 0), f"turn:{chat}:notice_{notice_id}")
                plan = read(path)
                if plan and not plan.get("complete") and (plan.get("leader_hold") or any(
                        s["status"] in ("uncertain", "reserved") and s["attempts"] for s in plan["stages"])):
                    self._hold_po_notice(chat, notice_id, notice, path, plan)
                    held += 1
            self.save_control(chat, control)
        return dict(ok=True, recovered=0, held=held)

    @boundary
    async def confirm_po_notice(self, chat, notice_id, hold_id, leader_root, resolution,
                                message_id=None, quiescent=False):
        """Explicit local leader action; consume authority durably before I/O.

        Delivered imports the original native message. Absent grants exactly
        one transport call, after the leader has checked/fenced the old writer.
        A lost result or process death never grants another send.
        """
        async with self.lock(chat):
            control = self.control(chat)
            notice = control["notices"].get(notice_id)
            require(notice is not None and notice["intended"]["notification_class"] == "owner_decision",
                    16, "unknown held PO notice")
            path = self.plan_path(chat, notice.get("epoch", 0), f"turn:{chat}:notice_{notice_id}")
            plan = read(path)
            require(plan is not None and plan.get("leader_hold"), 16, "PO notice is not held")
            hold = plan["leader_hold"]
            require(hold["hold_id"] == hold_id and leader_root is not None and
                    str(Path(leader_root).resolve()) == hold["leader_workspace"],
                    17, "leader hold identity mismatch")
            require(not plan.get("leader_action") and notice["status"] != "ok",
                    16, "leader confirmation already consumed")
            require(quiescent is True, 13, "leader must confirm original transport quiescence")
            require(resolution in ("delivered", "absent") and
                    (positive(message_id) if resolution == "delivered" else message_id is None),
                    16, "leader resolution requires exact message identity")
            state = self.state(chat) or plan["surface_snapshot"]
            require(control["barrier"] is None and self._notice_released(self.state(chat), chat, notice),
                    13, "PO decision waits for all live surfaces and keyboards")
            stage = plan["stages"][0]
            identity = hold["identity"]
            require(identity["seq"] == stage["seq"] and identity["plan_digest"] == plan["plan_digest"] and
                    identity["payload_digest"] == stage["payload_digest"] and identity["frame"] == stage["frame"],
                    16, "held delivery identity changed")
            plan["leader_action"] = dict(hold_id=hold_id, resolution=resolution, message_id=message_id,
                                         quiescent=True, confirmed_at_ms=self.now())
            atomic(path, plan)
            if resolution == "delivered":
                stage["receipt"] = self._receipt(state, plan, stage, "ok", message_id,
                                                 leader_hold_id=hold_id)
                stage["status"] = "ok"
                stage["attempts"][-1].update(status="ok", receipt=stage["receipt"])
                stage["kit_ack"] = True
                plan.update(complete=True, completion_receipt={"complete": True})
                atomic(path, plan)
                self._recover_bindings(state, plan)
                rc, result = 0, self._results(plan)
            else:
                stage.update(status="pending", transient_errors=0)
                atomic(path, plan)
                rc, result = await self._drive(chat, state, control, path, plan)
            if rc == 0:
                self._finish_po_notice(notice, plan, result)
            else:
                notice["delivery_state"] = "held"
            self.save_control(chat, control)
            return dict(ok=rc == 0, delivered=notice["status"] == "ok", notice_id=notice_id,
                        state=notice["delivery_state"], delivery=copy.deepcopy(notice.get("delivery")))

    def _finish_po_notice(self, notice, plan, result):
        stage = plan["stages"][0]
        notice.update(status="ok", receipt=result, delivered_at_ms=self.now(),
            delivery_state="delivered", delivery=dict(op=plan["request"]["op"], epoch=notice["epoch"],
                stage="notice_body", seq=stage["seq"], chat_id=plan["surface_snapshot"]["chat_id"],
                message_id=stage["receipt"]["message_id"], payload_digest=stage["payload_digest"],
                receipt=copy.deepcopy(stage["receipt"])))

    def _retired_owner(self, intended):
        """The replaced pack a notice belongs to; its output never releases.

        A damaged revocation record holds every notice (Refusal) instead of
        releasing a retired engine's output or dropping unrelated notices.
        """
        # ESTATE:BEGIN replaced-pack notice quarantine -- bridge.pack_revocation is manifest-excluded (DGN-2085 M4)
        from bridge import pack_revocation
        try:
            return pack_revocation.notice_owner(self.root, intended)
        except (OSError, ValueError) as error:
            raise Refusal(40, "pack revocation unreadable; notices held: " + str(error)) from error
        # ESTATE:ELSE
        # return None
        # ESTATE:END

    def _notice_record(self, control, intended, state):
        order = max((n.get("admission_order", 0) for n in control["notices"].values()), default=0) + 1
        record = dict(intended=intended, status="pending", admitted_at_ms=self.now(), admission_order=order,
                      release_at_ms=self._notice_deadline(state))
        if intended["notification_class"] == "owner_decision":
            record["delivery_state"] = "prepared"
        return record

    def _notice_deadline(self, state):
        if not state or state["state"] == "closed":
            return None
        horizon = [item["due_at_ms"] + item.get("coalesce_ms", 2000) for item in self._due_items(state)]
        return min(state["lease_until_ms"], max(horizon)) if horizon else state["lease_until_ms"]

    def _notice_released(self, state, chat, notice):
        if notice["intended"]["notification_class"] == "inert_preview":
            # Inert keyboards need neither the product action seat nor its timer.
            return True
        if notice["intended"]["notification_class"] == "owner_decision":
            return not (state and (state["state"] == "open" or
                        any(slot.get("has_keyboard") for slot in state["slots"].values())))
        if not state:
            return True
        if state["state"] == "closed":
            return not any(slot.get("has_keyboard") for slot in state["slots"].values())
        deadline = self._notice_deadline(state)
        if notice.get("release_at_ms") is not None:
            deadline = min(deadline, notice["release_at_ms"])
        # A newly scheduled timer or renewed lease cannot postpone an already
        # admitted notice. Old spools acquire their first deadline on polling.
        notice["release_at_ms"] = deadline
        if self.now() >= deadline:
            return True
        horizon = [item["due_at_ms"] + item.get("coalesce_ms", 2000) for item in self._due_items(state)]
        if horizon:
            return False
        return not self._pending(chat, state["epoch"])

    def _maintenance_plan(self, chat, state, notice_id, payload, notification_class):
        """Bridge-only constructor. No publication input can select this path."""
        op = f"turn:{chat}:notice_{notice_id}"
        if state is None:
            state = dict(schema="live_surface/1", chat_id=chat, surface_id="maintenance", epoch=0, owner=None,
                         state="closed", slots={"card": {"message_id": None, "has_keyboard": False, "retired_ids": []}},
                         action_slot="card", commit_order=0, lease_until_ms=0)
        slot = state["action_slot"]
        action = state["slots"][slot]
        frames = []
        def stage(sid, frame, mode, **values):
            return dict(stage_id=sid, frame=frame, mode=mode, slot=slot, allowed_outcomes=["ok", "unchanged"]
                        if mode in ("retire", "edit") else ["ok"], **values)
        active = state["state"] == "open"
        if notification_class == "owner_decision":
            require(not active and not any(s.get("has_keyboard") for s in state["slots"].values()),
                    13, "PO decision waits for all live surfaces and keyboards")
            require(frozenset(payload) in ({"text"}, {"text", "buttons", "po_decision"}),
                    16, "PO decision requires its frozen menu")
        preview = notification_class == "inert_preview"
        live = active and action["message_id"] is not None and not preview
        if live:
            require(action.get("body") and action.get("buttons"), 24, "validated action snapshot unavailable")
            frames.append(stage("action_retire", "card", "retire", target={"message_id": action["message_id"]},
                                silent=True, notification_class="live_card"))
        require(frozenset(payload) <= {"text", "parse_mode", "disable_notification", "buttons", "photo", "document", "raw_request", "reply_parameters", "message_thread_id", "link_preview_options", "preview_source_digest", "po_decision"} and
                (0 if payload.get("photo") or payload.get("document") or payload.get("raw_request") else 1) <= rendered_length(payload.get("text", ""), payload.get("parse_mode")) <= (1024 if payload.get("photo") or payload.get("document") else 4096),
                16, "notice rendering requires bounded text")
        frames.append(stage("notice_body", "inert-preview" if preview else "answer", "send", text=payload["text"], parse_mode=payload.get("parse_mode"),
                            silent=payload["disable_notification"] if "disable_notification" in payload else self.notification_policy(notification_class),
                            notification_class=notification_class, buttons=[] if active and not preview else payload.get("buttons") or []))
        for asset in ("photo", "document", "raw_request", "reply_parameters", "message_thread_id", "link_preview_options"):
            if payload.get(asset):
                frames[-1][asset] = copy.deepcopy(payload[asset])
        if preview:
            from bridge.inert_preview import validate_frame
            frames[-1]["slot"] = "preview"
            validate_frame(frames[-1])
        raw = frames[-1].get("raw_request")
        if raw:
            parameters = raw["parameters"]
            require(parameters.get("chat_id") in (chat, str(chat)), 16, "notice raw chat changed")
            if active:
                # Rule 4 permits only the action role to own a live keyboard.
                # Retain the original markup in the durable intended payload;
                # its active-session projection carries content only.
                parameters.pop("reply_markup", None)
                require(raw["method"] not in ("forwardMessage", "forwardMessages", "copyMessages"),
                        24, "notice markup cannot be controlled while surface is open")
                if raw["method"] == "copyMessage":
                    parameters["reply_markup"] = {"inline_keyboard": []}
                if raw["method"].startswith("edit"):
                    parameters["reply_markup"] = {"inline_keyboard": []}
                protected = {mid for slot_state in state["slots"].values()
                             for mid in [slot_state.get("message_id"), *slot_state.get("retired_ids", [])] if mid}
                if parameters.get("message_id") in protected and raw["method"] == "editMessageText":
                    raw["method"] = "sendMessage"
                    parameters.pop("message_id")
                elif parameters.get("message_id") in protected:
                    require(raw["method"] == "editMessageReplyMarkup", 24, "notice would overwrite committed guidance")
            if raw["method"].startswith(("edit", "delete")):
                require(positive(parameters.get("message_id")), 16, "notice edit target unavailable")
                frames[-1].update(mode="edit" if raw["method"].startswith("edit") else "delete",
                                  target={"message_id": parameters["message_id"]}, silent=True)
        if live:
            frames.append(stage("action_repost", "repost", "send", text=action["body"], buttons=action["buttons"],
                                protection=action.get("protection"), parse_mode=action.get("parse_mode"), silent=self.notification_policy("live_card"),
                                notification_class="live_card"))
            if "sealed_payload" in action:
                # Maintenance owns this new repost; preserve the original
                # native content/entities and freeze its own transport object.
                sealed = copy.deepcopy(action["sealed_payload"])
                sealed.update(frame="repost", mode="send", target=None,
                              notification={"class": "live_card", "silent": frames[-1]["silent"]})
                frames[-1].pop("parse_mode", None)
                frames[-1].update(native_projection(sealed), sealed_payload=sealed)
        plan = dict(request=dict(op=op, frames=frames, branch_manifest=copy.deepcopy(_EMPTY_BRANCHES)),
                    coordinator_fence=state.get("generation", 1), maintenance=True, surface_snapshot=copy.deepcopy(state), complete=False, completion_receipt=None,
                    kit_acknowledgements=[], plan_digest=digest(dict(frames=frames, branch_manifest=_EMPTY_BRANCHES)),
                    stages=[dict(frame=f, payload_digest=sealed_payload_digest(f), status="pending", seq=0, attempts=[], kit_ack=False) for f in frames])
        return state, plan

    async def transition_retire(self, pack, fence, checkpoint, target, held_chat=None, absent_record=None):
        """Drive one TP markup retirement; return A, or None while pending.

        The plan lives under epoch 0 of the target chat. Its receipt is the
        ordinary maintenance receipt (coordinator key), with D null,
        plan_digest H(TP) and lease_fence F.token; the export is the
        unchanged publisher projection. No surface owner is bound, so the
        activating generation never authorizes this I/O path. held_chat is
        the chat whose barrier the calling transition already holds (the
        same lock file); every other target chat is locked here.

        When Telegram reports the legacy message gone, the receipt is the
        kit-accepted "absent" one and absent_record(receipt, evidence, tp)
        (the bound authority seat) returns the authenticated recovery record;
        without that seat the retirement stays pending.
        """
        tp = transition_plan(fence, checkpoint, target)
        chat = target["chat_id"]
        frame = transition_retirement_frame(target)
        async with (contextlib.nullcontext() if chat == held_chat else self.lock(chat)):
            control = self.control(chat)
            # Outside frames/: a pending retirement never blocks the chat's
            # ordinary frames and notices ("earlier frame pending").
            path = self.base / "transition-frames" / str(chat) / (hashlib.sha256(tp["K"]["op"].encode()).hexdigest() + ".json")
            plan = read(path)
            state = dict(schema="live_surface/1", chat_id=chat, surface_id="transition", epoch=0, owner=None,
                         state="closed", slots={"card": {"message_id": None, "has_keyboard": False, "retired_ids": []}},
                         action_slot="card", commit_order=0, lease_until_ms=0)
            if plan is None:
                plan = dict(request=dict(op=tp["K"]["op"], frames=[frame], branch_manifest=copy.deepcopy(_EMPTY_BRANCHES)),
                            coordinator_fence=tp["token"], maintenance=True, transition_plan=tp,
                            surface_snapshot=copy.deepcopy(state), complete=False, completion_receipt=None,
                            kit_acknowledgements=[], plan_digest=digest(tp),
                            stages=[dict(frame=frame, payload_digest=sealed_payload_digest(frame), status="pending",
                                         seq=0, attempts=[], kit_ack=False)])
                atomic(path, plan)
            require(plan.get("transition_plan") == tp and plan["plan_digest"] == digest(tp) and
                    plan["coordinator_fence"] == tp["token"] and
                    plan["stages"][0]["payload_digest"] == tp["payload_digest"], 17, "transition plan changed")
            rc, _ = await self._drive(chat, state, control, path, plan)
            if rc:
                return None
            receipt = plan["stages"][0]["receipt"]
            export = self._export(pack, "receipt", {k: receipt[k] for k in RECEIPT_EXPORT})
            recovery = None
            if receipt["result"] == "absent":
                if absent_record is None:
                    return None
                evidence = plan["stages"][0]["absent_evidence"]
                recovery = dict(record=absent_record(receipt, evidence, tp), evidence=evidence)
            ack = dict(schema=TRANSITION_ACK, plan=tp, receipt=receipt, export=export, recovery=recovery)
            verify_transition_ack(ack, fence, checkpoint, self._receipt_key(state, plan), self.key(pack))
            return ack

    @boundary
    async def flush_notices(self, chat):
        async with self.lock(chat):
            state, control = self.state(chat), self.control(chat)
            require(control["barrier"] is None, 13, "open barrier pending")
            for notice_id, notice in sorted(control["notices"].items(), key=lambda pair: (
                    pair[1].get("admission_order", 0), pair[1]["admitted_at_ms"], pair[0])):
                if notice["status"] in ("ok", "dropped", "quarantined", "failed"):
                    continue
                owner_decision = notice["intended"]["notification_class"] == "owner_decision"
                if owner_decision and not self._notice_released(state, chat, notice):
                    return dict(ok=False, pending=True)
                op = f"turn:{chat}:notice_{notice_id}"
                epoch = notice.get("epoch", state["epoch"] if state else 0)
                path = self.plan_path(chat, epoch, op)
                plan = read(path)
                if plan is not None and self._recover_notice_claim(notice, path, plan):
                    self.save_control(chat, control)
                    if notice["status"] == "quarantined":
                        continue
                retired = self._retired_owner(notice["intended"])
                if retired and (plan is None or not any(a.get("io_authorized") for st in plan["stages"]
                                                        for a in st["attempts"])):
                    # Deferred output of a replaced engine is dropped, never
                    # released at its deadline. Output already handed to
                    # transport keeps its normal reconciliation below.
                    if plan is not None:
                        path.unlink()
                    notice.update(status="dropped", dropped_at_ms=self.now(),
                                  diagnostic="pack %s retired by replacement" % retired)
                    self.save_control(chat, control)
                    continue
                if plan is None:
                    released = self._notice_released(state, chat, notice)
                    self.save_control(chat, control)
                    if not released:
                        return dict(ok=False, pending=True)
                    require(not any(not p["complete"] for _, p in self.plans(chat)), 13, "earlier frame pending")
                    try:
                        state, plan = self._maintenance_plan(chat, state, notice_id, notice["intended"]["payload"],
                                                              notice["intended"]["notification_class"])
                    except Refusal as error:
                        if error.rc != 16:
                            raise
                        notice.update(status="failed", failed_at_ms=self.now(), diagnostic=str(error))
                        self.save_control(chat, control)
                        continue
                    notice.update(epoch=state["epoch"], status="dispatching")
                    if owner_decision:
                        notice["delivery_state"] = "sending"
                    atomic(path, plan)
                    self.save_control(chat, control)
                else:
                    if state is None:
                        state = plan.get("surface_snapshot")
                    require(state is not None and state["epoch"] == epoch, 13, "notice delivery binding needs recovery")
                if owner_decision and not plan.get("complete") and (plan.get("leader_hold") or any(
                        s["status"] in ("uncertain", "reserved") and s["attempts"] for s in plan["stages"])):
                    self._hold_po_notice(chat, notice_id, notice, path, plan)
                    self.save_control(chat, control)
                    return dict(ok=False, pending=True, held=True)
                rc, result = await self._drive(chat, state, control, path, plan)
                if rc:
                    if owner_decision:
                        notice["delivery_state"] = "sending"
                        if any(s["status"] in ("uncertain", "reserved") for s in plan["stages"]):
                            self._hold_po_notice(chat, notice_id, notice, path, plan)
                        self.save_control(chat, control)
                    uncertain = any(s["status"] == "uncertain" and
                                    any(a.get("io_authorized") for a in s.get("attempts", []))
                                    for s in plan["stages"])
                    if uncertain and not owner_decision:
                        self._quarantine_notice(notice, path, plan, "authorized notice outcome uncertain")
                        self.save_control(chat, control)
                        continue
                    if any(s["status"] == "failed" for s in plan["stages"]):
                        notice.update(status="failed", failed_at_ms=self.now(), diagnostic="permanent transport failure")
                        plan["complete"] = True
                        atomic(path, plan)
                        self.save_control(chat, control)
                        continue
                    return dict(ok=False, pending=True, rc=rc, result=result)
                notice.update(status="ok", receipt=result, delivered_at_ms=self.now())
                if owner_decision:
                    self._finish_po_notice(notice, plan, result)
                self.save_control(chat, control)
                if state.get("owner"):
                    self.journal(state, "flush", notice_id=notice_id)
            return dict(ok=True)

    @boundary
    async def capture_origin(self, chat, message_id=None, kind="turn", envelope_id=None):
        require(kind in ("turn", "tap", "due"), 2, "invalid origin kind")
        op = envelope_id or f"{kind}:{chat}:{message_id or secrets.token_hex(16)}"
        if not self.activated():
            # Preserve the original in-flight identity without touching inactive
            # storage. It becomes durable if a later surface owns delivery.
            return dict(kind=kind, envelope_id=op, epoch=None, surface=None, owner_turn_id=op if kind == "turn" else None)
        async with self.lock(chat):
            control = self.control(chat)
            origins = control.setdefault("origins", {})
            if op in origins:
                require(origins[op]["kind"] == kind, 16, "original turn identity changed")
                return copy.deepcopy(origins[op])
            state = self.state(chat)
            origin = dict(kind=kind, envelope_id=op, epoch=None, surface=None, owner_turn_id=op if kind == "turn" else None)
            if state and state["state"] == "open":
                origin.update(epoch=state["epoch"], surface=state["surface_id"])
                prior = state["envelopes"].get(op)
                require(prior is None or prior["kind"] == kind, 16, "origin binding changed")
                state["envelopes"][op] = prior or dict(kind=kind)
                self.save_state(state)
            origins[op] = copy.deepcopy(origin)
            self.save_control(chat, control)
            return origin

    @boundary
    async def model_final(self, chat, origin, text, paths=()):
        """Return handled=True only when the coordinator owns this delivery.

        The immutable original identity survives current-surface relocation;
        no old domain command is rebound or executed here.
        """
        if not self.activated():
            return False
        async with self.lock(chat):
            state, control = self.state(chat), self.control(chat)
            live = bool(state and state["state"] == "open")
            origin = origin or dict(kind="turn", epoch=None, surface=None,
                                     envelope_id=f"turn:{chat}:{secrets.token_hex(16)}")
            owner_turn = origin["kind"] == "turn"
            same = live and origin.get("epoch") == state["epoch"] and origin.get("surface") == state["surface_id"]
            if not owner_turn and not same:
                if state:
                    self.journal(state, "fence", op=origin["envelope_id"])
                return True
            original = origin["envelope_id"]
            key = hashlib.sha256(original.encode()).hexdigest()
            turns = control.setdefault("model_turns", {})
            saved = turns.get(key)
            if not live and saved is None:
                return False
            require(state is not None, 13, "preserved answer needs surface recovery")
            if not live:
                require(not any(s.get("has_keyboard") for s in state["slots"].values()), 24, "close retirement pending")
            if state["model_final"] == "drop" and same:
                self.journal(state, "drop", op=origin["envelope_id"], length=len(text), sha256=hashlib.sha256(text.encode()).hexdigest())
                return True
            if not owner_turn:
                # Tap/due output is never an ordinary answer or attachment.
                self.journal(state, "drop", op=origin["envelope_id"], length=len(text), sha256=hashlib.sha256(text.encode()).hexdigest())
                return True
            if saved and saved.get("complete"):
                return True
            # A kit answer consumes the same answer seat.
            for _, prior in self.plans(chat):
                if prior["request"]["op"] == original and any(
                        stage["frame"]["frame"] == "answer" and stage["status"] in SATISFIED
                        for stage in prior["stages"]):
                    return True
            if saved is None:
                attachments = [self._snapshot_attachment(chat, key, p) for p in paths]
                saved = dict(origin=copy.deepcopy(origin), text=text, paths=[str(p) for p in paths],
                             attachments=attachments, complete=False)
                turns[key] = saved
                self.save_control(chat, control)
            require(saved["origin"] == origin and saved["text"] == text, 16, "model answer identity changed")
            require(control["barrier"] is None, 13, "model answer waits for open barrier")
            op = f"turn:{chat}:notice_{key}"
            epoch = saved.get("delivery_epoch", state["epoch"])
            path = self.plan_path(chat, epoch, op)
            plan = read(path)
            if plan and (epoch != state["epoch"] or not live and plan["surface_snapshot"]["state"] == "open"):
                require(plan.get("maintenance") and all(stage["seq"] == 0 for stage in plan["stages"]),
                        13, "preserved answer has prior authorized transport")
                # No reservation/attempt exists. Re-evaluate only the delivery
                # ordering; the original owner identity and assets stay fixed.
                plan.update(complete=True, superseded_before_io=True)
                atomic(path, plan)
                epoch = state["epoch"]
                path = self.plan_path(chat, epoch, op)
                plan = None
                saved["delivery_epoch"] = epoch
                self.save_control(chat, control)
            if plan is None:
                require(not any(not p["complete"] for _, p in self.plans(chat)), 13, "earlier frame pending")
                eligible, retained = [], []
                attachments = saved.get("attachments")
                if attachments is None:
                    # Older checkpoints have no immutable asset; freeze them
                    # before any new authorization and retain that snapshot.
                    attachments = [self._snapshot_attachment(chat, key, p) for p in saved["paths"]]
                    saved["attachments"] = attachments
                    self.save_control(chat, control)
                for attachment in attachments:
                    candidate = Path(attachment["source"])
                    allowed = any(candidate.is_relative_to((self.root / d).resolve()) for d in state.get("attach_dirs", []))
                    if (allowed or not live) and candidate.suffix.lower() in (".png", ".jpg", ".jpeg") and not eligible:
                        eligible.append(attachment)
                    elif not same:
                        retained.append(attachment)
                saved["retained_attachments"] = [a["source"] for a in retained]
                saved["retained_snapshots"] = retained
                self.save_control(chat, control)
                # Preflight the entire answer before retiring any markup.
                photo = None
                if eligible:
                    photo = self._photo(path, {k: eligible[0][k] for k in ("path", "digest")})
                if not text and not photo:
                    saved["complete"] = True
                    self.save_control(chat, control)
                    return True
                require((0 if photo else 1) <= rendered_length(text) <= (1024 if photo else 4096), 16, "model answer render overflow")
                payload = dict(text=text)
                if photo:
                    payload["photo"] = photo
                _, plan = self._maintenance_plan(chat, state, key, payload, "live_answer")
                plan["origin"] = copy.deepcopy(origin)
                atomic(path, plan)
                saved["delivery_epoch"] = state["epoch"]
                self.save_control(chat, control)
            require(epoch == state["epoch"], 13, "preserved answer needs prior surface recovery")
            rc, _ = await self._drive(chat, state, control, path, plan)
            saved["complete"] = rc == 0
            self.save_control(chat, control)
            require(rc == 0, rc, "model answer delivery pending")
            return True

    async def resume_model_answers(self, chat):
        # model_final performs its own chat serialization and keeps uncertainty
        # quarantined. Only previously admitted immutable answers are retried.
        for turn in self.control(chat).get("model_turns", {}).values():
            if not turn.get("complete"):
                await self.model_final(chat, turn["origin"], turn["text"], turn["paths"])

    def _snapshot_attachment(self, chat, turn, supplied):
        supplied = Path(supplied)
        source = (supplied if supplied.is_absolute() else self.root / supplied).resolve()
        require(source.is_relative_to(self.root) and source.is_file(), 16, "attachment outside instance")
        require(source.stat().st_size <= 50 * 1024 * 1024, 16, "attachment too large")
        raw = source.read_bytes()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        target = self.base / "attachments" / str(chat) / hashlib.sha256(turn.encode()).hexdigest() / (actual.split(":")[1] + source.suffix.lower())
        if not target.exists():
            private_bytes(target, raw)
        require("sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() == actual, 40, "attachment snapshot corrupt")
        return dict(source=str(source), path=str(target), digest=actual, filename=source.name)

    @boundary
    async def flush_retained(self, chat):
        async with self.lock(chat):
            state, control = self.state(chat), self.control(chat)
            if control["barrier"] or state and (state["state"] == "open" or any(s.get("has_keyboard") for s in state["slots"].values())):
                return dict(ok=False, pending=True)
            for key, turn in control.get("model_turns", {}).items():
                if not turn.get("complete"):
                    continue
                for index, attachment in enumerate(turn.get("retained_snapshots", [])):
                    if attachment.get("delivered"):
                        continue
                    identity = key + "_file_" + str(index)
                    op = f"turn:{chat}:notice_{identity}"
                    epoch = state["epoch"] if state else 0
                    path = self.plan_path(chat, epoch, op)
                    plan = read(path)
                    if plan is None:
                        require(not any(not p["complete"] for _, p in self.plans(chat)), 13, "retained attachment waits for prior frame")
                        _, plan = self._maintenance_plan(chat, state, identity, dict(text="", document={
                            k: attachment[k] for k in ("path", "digest", "filename")}), "file")
                        plan["origin"] = turn["origin"]
                        atomic(path, plan)
                    delivery_state = state or plan["surface_snapshot"]
                    rc, receipt = await self._drive(chat, delivery_state, control, path, plan)
                    if rc:
                        return dict(ok=False, pending=True, rc=rc)
                    attachment.update(delivered=True, receipt=receipt)
                    self.save_control(chat, control)
            return dict(ok=True)

    async def ordinary(self, chat, identity, operation, body=None, has_keyboard=False):
        """Actual-I/O check serialized with open, including retries."""
        if not self.activated():
            return await operation()
        async with self.lock(chat):
            return await self._ordinary_locked(chat, identity, operation, body, has_keyboard)

    async def _ordinary_locked(self, chat, identity, operation, body=None, has_keyboard=False):
        control, state = self.control(chat), self.state(chat)
        old = control["ordinary"].get(identity)
        require(not any(not plan["complete"] for _, plan in self.plans(chat)), 13, "ordinary waits for prior transport")
        if control["barrier"] or (state and state["state"] == "open") or (old and old.get("muted")):
            return None
        require(not old or old.get("status") not in ("reserved", "uncertain"), 13, "ordinary transport uncertain")
        history = list(old.get("history", [])) if old else []
        if old and old.get("status") == "ok":
            history.append({k: v for k, v in old.items() if k != "history"})
        item = dict(status="reserved", muted=False, body=body, has_keyboard=has_keyboard, history=history)
        control["ordinary"][identity] = item
        self.save_control(chat, control)
        try:
            with authorized_io(chat):
                result = await asyncio.wait_for(operation(), 16)
        except BaseException as error:
            # A decoded Bot API rejection is definitive nonacceptance. A
            # local timeout/network error remains potentially accepted.
            item["status"] = "failed" if type(error).__name__ in ("BadRequest", "RetryAfter", "Forbidden") else "uncertain"
            self.save_control(chat, control)
            raise
        item.update(status="ok", message_id=getattr(result, "message_id", None))
        if isinstance(result, tuple) and len(result) == 2 and type(result[0]) is int:
            # The HTTP guard runs before PTB decodes the response. An HTTP 5xx
            # or malformed reply cannot be counted as confirmed nonacceptance.
            try:
                response = json.loads(result[1])
            except (ValueError, TypeError):
                response = {}
            item["status"] = ("ok" if response.get("ok") is True else
                              "failed" if 400 <= result[0] < 500 or response.get("error_code") == 429 else "uncertain")
            if isinstance(response.get("result"), dict):
                item["message_id"] = response["result"].get("message_id")
        self.save_control(chat, control)
        return result

    def context(self, chat, turn_id):
        state = self.state(chat)
        if not state or state["state"] != "open":
            return ""
        slots = [{"slot": k, "message_id": v["message_id"], "body": v.get("body", "")} for k, v in state["slots"].items()]
        value = dict(envelope_id=turn_id, surface=state["surface_id"], epoch=state["epoch"],
                     owner=state["owner"], model_final=state["model_final"], slots=slots)
        return "[bridge:live-surface]\n" + canonical(value).decode() + "\n[/bridge:live-surface]"


_RAW_IO_CHAT = ContextVar("live_surface_authorized_io", default=None)
_HTTP_RESPONSE = ContextVar("live_surface_http_response", default=None)


@contextlib.contextmanager
def authorized_io(chat):
    token = _RAW_IO_CHAT.set(chat)
    try:
        yield
    finally:
        _RAW_IO_CHAT.reset(token)


async def guarded_request(root, endpoint, request_data, operation):
    """Actual-I/O guard for every application send/edit/delete, including files."""
    if not root or request_data is None or not endpoint.startswith(("send", "edit", "delete", "copy", "forward")) or endpoint == "sendChatAction":
        return await operation()
    coordinator = Coordinator(root)
    if not coordinator.activated():
        return await operation()
    parameters = request_data.parameters
    chat = parameters.get("chat_id")
    if isinstance(chat, str) and re.fullmatch(r"-?[0-9]+", chat):
        chat = int(chat)
    require(type(chat) is int, 16, "content request lacks chat identity")
    if _RAW_IO_CHAT.get() == chat:
        result = await operation()
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], bytes):
            _HTTP_RESPONSE.set(result[1].decode("utf-8"))
        return result
    async with coordinator.lock(chat):
        state, control = coordinator.state(chat), coordinator.control(chat)
        if not control["barrier"] and not (state and state["state"] == "open"):
            return await coordinator._ordinary_locked(chat, "raw:" + secrets.token_hex(16), operation,
                body=parameters.get("text", parameters.get("caption")), has_keyboard=bool(parameters.get("reply_markup")))
        files = {}
        for field, supplied in request_data.multipart_data.items():
            filename, raw = supplied[:2]
            if hasattr(raw, "read"):
                raw = raw.read()
            require(isinstance(raw, bytes) and len(raw) <= 50 * 1024 * 1024, 16, "raw asset cannot be snapshotted")
            sha = "sha256:" + hashlib.sha256(raw).hexdigest()
            target = coordinator.base / "raw-assets" / sha.split(":")[1]
            if not target.exists():
                private_bytes(target, raw)
            require("sha256:" + hashlib.sha256(target.read_bytes()).hexdigest() == sha, 40, "raw asset snapshot corrupt")
            files[field] = dict(path=str(target), digest=sha, filename=filename or "attachment")
        raw_request = dict(method=endpoint, parameters=parameters, files=files)
        payload = dict(text=parameters.get("text", parameters.get("caption", "")), raw_request=raw_request)
        if "disable_notification" in parameters:
            payload["disable_notification"] = parameters["disable_notification"]
        identity = digest(raw_request)
        key = hashlib.sha256(canonical(["raw-transport", identity])).hexdigest()
        if key not in control["notices"]:
            control["notices"][key] = coordinator._notice_record(control, dict(producer="raw-transport", event_id=identity,
                payload=payload, notification_class="system"), state)
            coordinator.save_control(chat, control)
    # No Telegram receipt exists. Never fabricate success to satisfy a caller.
    from telegram.error import TelegramError
    raise TelegramError("content admitted for ordered notice delivery")


def command_delivery_service(root):
    """Installed publisher argv is a local JSON service, never shell text."""
    root = Path(root).resolve()
    async def invoke(request):
        publisher = request["auth"]["publisher_id"]
        coordinator = Coordinator(root, delivery_service=invoke)
        config = coordinator.publisher_config(publisher)
        argv = config.get("delivery_argv")
        require(isinstance(argv, list) and bool(argv) and all(isinstance(v, str) and v and "\x00" not in v for v in argv),
                40, "installed delivery argv unavailable")
        executable = (root / argv[0]).resolve()
        require(not Path(argv[0]).is_absolute() and executable.is_relative_to(root) and executable.is_file(),
                17, "delivery argv must name an installed executable")
        process = await asyncio.create_subprocess_exec(str(executable), *argv[1:], cwd=root,
                    env=dict(os.environ, PROJECT_ROOT=str(root), INSTANCE_ROOT=str(root)),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=4 * 1024 * 1024)
        try:
            output, _ = await asyncio.wait_for(process.communicate(canonical(request) + b"\n"), 9)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        require(process.returncode == 0 and len(output) <= 4 * 1024 * 1024, 40, "delivery command failed")
        try:
            return load_request(io.StringIO(output.decode("utf-8")))
        except (Refusal, UnicodeError) as error:
            raise Refusal(40, "delivery command returned invalid JSON") from error
    return invoke


def cli_transport(root):
    async def execute(payload):
        # Load credentials only for an authorized actual attempt, after seal.
        os.environ["PROJECT_ROOT"] = str(Path(root).resolve())
        from bridge.config import config
        from telegram import Bot
        from bridge.owner_belt import OwnerGuardRequest
        request = OwnerGuardRequest()
        request.surface_root = root
        async with Bot(config.telegram_bot_token, request=request) as bot:
            return await ptb_transport(bot)(payload)
    return execute


async def maintenance_once(root, bot, continuation=None):
    coordinator = Coordinator(root, transport=ptb_transport(bot))
    from bridge import po_decisions
    if po_decisions.installed(root):
        await asyncio.to_thread(po_decisions.retry_pending, root)
    active = coordinator.activated()
    # Disable closes admission, not the recovery obligation for an already
    # admitted external send. The per-chat filter below admits no new notice.
    for path in sorted(coordinator.base.glob("*.transport.json")):
        chat = int(path.name.split(".")[0])
        try:
            if not active and not any(n["intended"]["notification_class"] == "owner_decision" and
                    n["status"] not in ("ok", "dropped") for n in coordinator.control(chat)["notices"].values()):
                continue
            await coordinator.recover_po_notices(chat)
            if active:
                await coordinator.resume_model_answers(chat)
            await coordinator.flush_notices(chat)
            if active:
                await coordinator.flush_retained(chat)
            if continuation is not None:
                async with coordinator.lock(chat):
                    control = coordinator.control(chat)
                    pending = [(key, copy.deepcopy(n["continuation"])) for key, n in control["notices"].items()
                               if n["status"] == "ok" and n.get("continuation_state") == "pending"]
                for key, value in pending:
                    async with coordinator.lock(chat):
                        control = coordinator.control(chat)
                        control["notices"][key]["continuation_state"] = "started"
                        coordinator.save_control(chat, control)
                    await continuation(value)
                    async with coordinator.lock(chat):
                        control = coordinator.control(chat)
                        control["notices"][key]["continuation_state"] = "complete"
                        coordinator.save_control(chat, control)
        except (Refusal, OSError, ValueError) as error:
            import logging
            logging.getLogger(__name__).warning("Surface maintenance remains pending for %s: %s", chat, error)


async def maintenance_loop(root, bot, continuation=None):
    while True:
        await maintenance_once(root, bot, continuation)
        await asyncio.sleep(1)


def raise_due_floor(root, chat, surface, epoch, deadline):
    """Scheduler-only: caller already holds the same chat barrier as transport."""
    from bridge.session_state import Refusal as ServiceRefusal, integer
    coordinator = Coordinator(root)
    state = coordinator.state(chat)
    if (not state or state["state"] != "open" or state["surface_id"] != surface
            or state["epoch"] != epoch or not integer(deadline, 1)):
        raise ServiceRefusal(10, "due floor surface closed or moved")
    active = coordinator.activation(state["owner"])
    if not active or active["phase"] != "active" or active["generation"] != state["generation"]:
        raise ServiceRefusal(35, "due floor activation generation fenced")
    state["lease_until_ms"] = max(state["lease_until_ms"], deadline)
    coordinator.save_state(state)


def cancel_due_group(root, chat, op):
    """Scheduler-only: preserve authorized attempts; block all fresh attempts."""
    from bridge.session_state import Refusal as ServiceRefusal
    from bridge.due_events import DueEvents
    coordinator = Coordinator(root)
    group = read(DueEvents(root).group_path(op))
    from bridge import callback_routes
    if (not group or group["op"] != op or group["chat_id"] != chat or group["state"] not in ("cancelled", "fenced")
            or callback_routes.authenticated(root, group["envelope"])["envelope_id"] != op
            or digest(group["envelope"]) != group["binding_digest"]):
        raise ServiceRefusal(17, "canonical cancelled due group required")
    # Persist cancellation even when no frame has been staged yet. A late
    # publisher cannot cross the transport barrier after cancellation returns.
    control = coordinator.control(chat)
    control.setdefault("cancelled_ops", {})[op] = group["binding_digest"]
    coordinator.save_control(chat, control)
    for _, plan in coordinator.plans(chat):
        if plan["request"]["op"] == op and any(s["status"] in ("reserved", "uncertain") and
                any(a.get("io_authorized") for a in s["attempts"]) for s in plan["stages"]):
            raise ServiceRefusal(24, "cancelled due group has an authorized attempt pending reconciliation")


def m2_services(root, authorities=None):
    # ESTATE:BEGIN installed pinned-kit artifact provider has no public counterpart
    from bridge.session_artifacts import PinnedArtifactCodec
    from bridge.session_state import Refusal as ServiceRefusal
    from bridge.session_activation import (OriginalCommandRecovery, InvocationProofProducer,
                                           RequiredTransitionIO)
    from bridge.due_events import SurfaceAccess
    from bridge.pack_transition import default_transition_io
    try:
        codec = PinnedArtifactCodec(root)
    except (ServiceRefusal, OSError, ValueError):
        return {}
    # These are process-injected dependencies. No receipt, environment flag or
    # child result may instantiate a transaction or pack-wide writer authority.
    authorities = authorities or {}
    recovery = OriginalCommandRecovery(root, authorities.get("commands"))
    return {"artifact_codec": codec,
            "surface_apis": {"live_surface_api": [1], "ledger_api": [2],
                             "features": ["edit_acknowledgement", "delivery_receipt_reconciliation"]},
            "surface": SurfaceAccess(root),
            "transition_io": RequiredTransitionIO(authorities.get("transition_io") or default_transition_io(root)),
            "recovery": recovery,
            "invocation_proof": InvocationProofProducer(root, recovery, authorities.get("invocations")),
            "committed_snapshot": recovery.committed_snapshot,
            "publisher_exports": recovery.publisher_exports}
    # ESTATE:ELSE
    # return {}
    # ESTATE:END


_MODEL_ORIGIN = ContextVar("live_surface_original_turn", default=None)


@contextlib.contextmanager
def inherit_model_origin(origin):
    token = _MODEL_ORIGIN.set(copy.deepcopy(origin))
    try:
        yield
    finally:
        _MODEL_ORIGIN.reset(token)


def stamp_model_origin(function):
    @wraps(function)
    async def stamped(*args, **kwargs):
        origin = kwargs.get("origin_surface") or _MODEL_ORIGIN.get()
        if origin is None:
            from bridge.sdk_bridge import PROJECT_ROOT
            bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
            inbound = bound.get("inbound") or {}
            origin = await Coordinator(PROJECT_ROOT).capture_origin(bound["chat_id"], inbound.get("message_id"))
        kwargs["origin_surface"] = origin
        result = await function(*args, **kwargs)
        result.origin_surface = origin
        return result
    return stamped


async def deliver_model_final(root, bot, chat, origin, text, paths=()):
    return await Coordinator(root, transport=ptb_transport(bot)).model_final(chat, origin, text, paths)


async def framework_notice_send(root, producer, send, *args, **kwargs):
    """Durable admission is not a Telegram receipt; default-off calls are exact."""
    notice_id = kwargs.pop("_notice_id", None)
    continuation = kwargs.pop("_continuation", None)
    coordinator = Coordinator(root)
    if not coordinator.activated():
        return await send(*args, **kwargs)
    receiver = getattr(send, "__self__", None)
    is_reply = getattr(send, "__name__", "") == "reply_text"
    chat = getattr(receiver, "chat_id", None) if is_reply else kwargs.get("chat_id", args[0] if args else None)
    if type(chat) is not int:
        return await send(*args, **kwargs)
    text = kwargs.get("text", args[0 if is_reply else 1] if len(args) > (0 if is_reply else 1) else "")
    payload = dict(text=text, parse_mode=kwargs.get("parse_mode"))
    if kwargs.get("disable_notification") is not None:
        payload["disable_notification"] = kwargs["disable_notification"]
    async with coordinator.lock(chat):
        state, control = coordinator.state(chat), coordinator.control(chat)
        stable_key = hashlib.sha256(canonical([producer, notice_id])).hexdigest() if notice_id else None
        admitted = control["notices"].get(stable_key) if stable_key else None
        if admitted or control["barrier"] or state and state["state"] == "open":
            for field in ("reply_parameters", "message_thread_id", "link_preview_options"):
                value = kwargs.get(field)
                if value is not None:
                    payload[field] = value.to_dict() if hasattr(value, "to_dict") else value
        if admitted:
            markup = kwargs.get("reply_markup")
            if markup is not None:
                encoded = markup.to_dict() if hasattr(markup, "to_dict") else markup
                payload["buttons"] = encoded["inline_keyboard"]
            require(admitted["intended"]["payload"] == payload, 16, "notice identity payload changed")
        if admitted and admitted["status"] == "ok":
            from types import SimpleNamespace
            receipt = next(r for r in admitted["receipt"]["results"] if r["stage_id"] == "notice_body")
            return SimpleNamespace(message_id=receipt["message_id"], chat_id=chat)
        if not admitted and not control["barrier"] and not (state and state["state"] == "open"):
            markup = kwargs.get("reply_markup")
            keyboard = bool(getattr(markup, "inline_keyboard", markup))
            return await coordinator._ordinary_locked(chat, "framework:" + secrets.token_hex(16),
                lambda: send(*args, **kwargs), body=text, has_keyboard=keyboard)
        markup = kwargs.get("reply_markup")
        if markup is not None:
            encoded = markup.to_dict() if hasattr(markup, "to_dict") else markup
            require(isinstance(encoded, dict) and "inline_keyboard" in encoded, 16, "notice keyboard cannot be serialized")
            payload["buttons"] = encoded["inline_keyboard"]
        prior = next((n for n in control["notices"].values() if n["status"] != "ok" and
                      n["intended"]["producer"] == producer and n["intended"]["payload"] == payload), None)
        event_id = notice_id or (prior["intended"]["event_id"] if prior else secrets.token_hex(16))
        key = hashlib.sha256(canonical([producer, event_id])).hexdigest()
        if prior is None:
            control["notices"][key] = coordinator._notice_record(control, dict(producer=producer, event_id=event_id,
                payload=payload, notification_class="system"), state)
            if continuation is not None:
                control["notices"][key]["continuation"] = copy.deepcopy(continuation() if callable(continuation) else continuation)
                control["notices"][key]["continuation_state"] = "pending"
            coordinator.save_control(chat, control)
    return None


def suppress_stream(root, chat):
    coordinator = Coordinator(root)
    if not coordinator.activated():
        return False
    state = coordinator.state(chat)
    return bool(coordinator.control(chat)["barrier"] or state and state["state"] == "open")


def ptb_transport(bot):
    """Only the frame executor invokes this bounded transport, never a retry wrapper."""
    async def execute(payload):
        from telegram import InlineKeyboardMarkup
        frame, chat, target = payload["frame"], payload["chat_id"], payload["target"]
        if "sealed_payload" in frame:
            frame = checked_native_frame(frame)
            require(target == frame["target"], 17, "native transport target changed")
        kwargs = dict(chat_id=chat, connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=2)
        mode = frame["mode"]
        _HTTP_RESPONSE.set(None)
        try:
            if frame.get("raw_request"):
                raw_request = frame["raw_request"]
                parameters = copy.deepcopy(raw_request["parameters"])
                from telegram import InputFile
                for field, asset in raw_request["files"].items():
                    raw = Path(asset["path"]).read_bytes()
                    require("sha256:" + hashlib.sha256(raw).hexdigest() == asset["digest"], 40, "raw asset snapshot changed")
                    parameters[field] = InputFile(raw, filename=asset["filename"])
                result = await bot.do_api_request(raw_request["method"], api_kwargs=parameters,
                    connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=2)
                if result is True:
                    result = dict(message_id=parameters.get("message_id"), chat={"id": chat})
                if isinstance(result, list):
                    require(bool(result), 40, "empty media result")
                    result = result[0]
                return dict(ok=True, result=result)
            elif mode == "retire":
                result = await bot.edit_message_reply_markup(message_id=target["message_id"],
                            reply_markup=InlineKeyboardMarkup([]), **kwargs)
            elif mode == "delete":
                result = await bot.delete_message(message_id=target["message_id"], **kwargs)
            else:
                kwargs["reply_markup"] = InlineKeyboardMarkup(frame.get("buttons") or [])
                from telegram import ReplyParameters, LinkPreviewOptions
                if frame.get("reply_parameters"):
                    kwargs["reply_parameters"] = ReplyParameters.de_json(frame["reply_parameters"], bot)
                if frame.get("message_thread_id") is not None:
                    kwargs["message_thread_id"] = frame["message_thread_id"]
                if frame.get("link_preview_options"):
                    kwargs["link_preview_options"] = LinkPreviewOptions.de_json(frame["link_preview_options"], bot)
                if "entities" in frame:
                    kwargs["caption_entities" if frame.get("photo") else "entities"] = frame["entities"]
                if frame.get("parse_mode"):
                    kwargs["parse_mode"] = frame["parse_mode"]
                if mode == "send":
                    kwargs["disable_notification"] = frame["silent"]
                if frame.get("document"):
                    asset = frame["document"]
                    raw = Path(asset["path"]).read_bytes()
                    require("sha256:" + hashlib.sha256(raw).hexdigest() == asset["digest"], 40, "attachment snapshot changed")
                    result = await bot.send_document(document=raw, filename=asset["filename"], caption=frame.get("text") or None, **kwargs)
                elif frame.get("photo"):
                    raw = Path(frame["photo"]["path"]).read_bytes()
                    require("sha256:" + hashlib.sha256(raw).hexdigest() == frame["photo"]["digest"], 40, "photo snapshot changed")
                    result = await bot.send_photo(photo=raw, caption=frame["text"], **kwargs)
                elif mode == "edit":
                    result = await bot.edit_message_text(message_id=target["message_id"], text=frame["text"], **kwargs)
                else:
                    result = await bot.send_message(text=frame["text"], **kwargs)
            return dict(ok=True, result=True if result is True else dict(message_id=result.message_id, chat={"id": result.chat_id}))
        except Exception as error:
            from telegram.error import BadRequest, Forbidden, RetryAfter
            if isinstance(error, RetryAfter):
                retry = error.retry_after
                if hasattr(retry, "total_seconds"):
                    retry = retry.total_seconds()
                return dict(ok=False, error_code=429, parameters={"retry_after": int(retry)}, description=str(error), raw_response=_HTTP_RESPONSE.get() or canonical(dict(description=str(error))).decode())
            if isinstance(error, (BadRequest, Forbidden)):
                return dict(ok=False, error_code=400 if isinstance(error, BadRequest) else 403, description=str(error), raw_response=_HTTP_RESPONSE.get() or canonical(dict(description=str(error))).decode())
            raise
    return execute


async def submit_frames(root, chat, request, **dependencies):
    return await Coordinator(root, **dependencies).submit_frames(chat, request)


async def reserve(root, chat, **kwargs):
    dependencies = kwargs.pop("dependencies", {})
    return await Coordinator(root, **dependencies).reserve(chat, **kwargs)


async def commit(root, chat, **kwargs):
    dependencies = kwargs.pop("dependencies", {})
    return await Coordinator(root, **dependencies).commit(chat, **kwargs)


async def verify(root, chat, request, **dependencies):
    return await Coordinator(root, **dependencies).verify(chat, request)


async def complete(root, chat, surface, epoch, **dependencies):
    return await Coordinator(root, **dependencies).complete(chat, surface, epoch)


async def guarded_stream(bot, chat, identity, operation, body=None):
    root = os.environ.get("PROJECT_ROOT")
    if not root:
        return await operation()
    return await Coordinator(root).ordinary(chat, identity, operation, body=body)


def render_push_notice(root, text, buttons, strict=False):
    """Use the shell rail's shared artifact renderer before durable admission."""
    from bridge.formatting import sanitize_message_for_telegram
    clean, keyboard = text, None
    # ESTATE:BEGIN shell arm rendering uses the same declared keyboard as the bot
    from bridge.artifacts import initial_keyboard_json
    from bridge.formatting import strip_idrill_marker
    clean, arm_id = strip_idrill_marker(text)
    keyboard = initial_keyboard_json(Path(root), arm_id) if arm_id else None
    # ESTATE:END
    from bridge import machine_gate
    gated = machine_gate.gate_machine_lines(clean, machine_gate.RAIL_CONSUMER).text
    require(not machine_gate.find_execution_identifiers(gated), 16, "notice contains execution identifiers")
    html = sanitize_message_for_telegram(clean, rail=machine_gate.RAIL_CONSUMER)
    if strict:
        require(clean == text and gated == text and bool(html.strip()), 16, "strict notice rendering changed intended text")
    payloads = []
    if html.strip():
        payloads.append(dict(text=html, parse_mode="HTML", buttons=buttons))
    if keyboard:
        payloads.append(dict(text=keyboard["text"], buttons=keyboard["reply_markup"]["inline_keyboard"]))
    for payload in payloads:
        require(0 < rendered_length(payload["text"], payload.get("parse_mode")) <= 4096,
                16, "notice render overflow")
    return payloads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("capabilities", "open", "touch", "close", "frame-submit", "metrics", "enqueue-notice", "expire-notices", "po-notice", "po-notice-confirm", "po-notice-capabilities", "push-notice", "push-preview", "push-session", "reserve", "commit", "verify", "complete", "bind-command", "delivery-receipts", "delivery-ack", "reconcile"))
    parser.add_argument("--root", default=os.environ.get("PROJECT_ROOT"))
    parser.add_argument("--chat", type=int)
    parser.add_argument("--owner")
    parser.add_argument("--surface")
    parser.add_argument("--prefix")
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--slots", default="card")
    parser.add_argument("--action-slot")
    parser.add_argument("--answer-slot", action="store_true")
    parser.add_argument("--model-final", default="answer")
    parser.add_argument("--lease-s", type=int, default=5400)
    parser.add_argument("--takeover", action="store_true")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--reason", default="done")
    parser.add_argument("--producer")
    parser.add_argument("--notification-class")
    parser.add_argument("--admission-orders", default="0-9223372036854775807")
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--expect-journal-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--event-id")
    parser.add_argument("--text", default="")
    parser.add_argument("--candidate-id")
    parser.add_argument("--notice-id")
    parser.add_argument("--hold-id")
    parser.add_argument("--leader-root")
    parser.add_argument("--resolution", choices=("delivered", "absent"))
    parser.add_argument("--quiescent", action="store_true")
    parser.add_argument("--expect-file", type=Path)
    parser.add_argument("--caption", default="")
    parser.add_argument("--button", default="")
    parser.add_argument("--parse-mode", choices=("HTML",))
    parser.add_argument("--render-notice", action="store_true")
    parser.add_argument("--strict-notice", action="store_true")
    parser.add_argument("--notice-keyboard-file", type=Path)
    parser.add_argument("--link-preview", action="store_true")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--thread", type=int)
    parser.add_argument("--op")
    parser.add_argument("--stage")
    parser.add_argument("--frame")
    parser.add_argument("--slot")
    parser.add_argument("--buttons-json", default="[]")
    parser.add_argument("--photo", nargs="?", const=True, default="")
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--mode", choices=("send", "edit", "retire", "delete"))
    parser.add_argument("--keyboard", action="store_true")
    parser.add_argument("--seq", type=int)
    parser.add_argument("--result", choices=("ok", "unchanged", "uncertain", "failed", "absent", "retained_cleanup", "rate_limited"))
    parser.add_argument("--evidence")
    parser.add_argument("--attach-dir", action="append", default=[])
    parser.add_argument("--adopt", action="append", default=[])
    args = parser.parse_args()
    if args.root is None:
        parser.error("instance root required")
    if args.verb == "po-notice-capabilities":
        print("apo14-po-leader-hold/1")
        return 0
    if args.verb == "capabilities":
        from bridge import session_activation
        try:
            services = m2_services(args.root)
            value = session_activation.capabilities(args.root, services.get("surface_apis", {}), services.get("artifact_codec"))
        except (Refusal, session_activation.Refusal, OSError, ValueError) as error:
            print(canonical(dict(ok=False, diagnostic=str(error))).decode())
            return getattr(error, "rc", 40)
        print(canonical(value).decode())
        return 0
    if args.chat is None:
        # Reuse the existing ownership precedence rather than a new owner parser.
        os.environ["PROJECT_ROOT"] = str(Path(args.root).resolve())
        from bridge.countdown import CountdownDriver
        args.chat = CountdownDriver._owner_chat_id(None)
        if args.chat is None:
            parser.error("no owner chat configured; use --chat")
    coordinator = Coordinator(args.root, transport=cli_transport(args.root))
    async def run():
        if args.verb == "expire-notices":
            match = re.fullmatch(r"(\d+)-(\d+)", args.admission_orders)
            require(match is not None and int(match.group(1)) <= int(match.group(2)) and
                    args.expect_count is not None and args.expect_count >= 0 and
                    isinstance(args.expect_journal_sha256, str) and
                    re.fullmatch(r"[0-9a-f]{64}", args.expect_journal_sha256),
                    2, "expire-notices requires a bounded selection and snapshot")
            return 0, await coordinator.expire_notices(
                args.chat, args.producer, args.notification_class,
                (int(match.group(1)), int(match.group(2))), args.expect_count,
                args.expect_journal_sha256, args.reason, args.apply)
        if args.verb == "push-preview":
            if args.expect_file:
                require(args.expect_file.read_bytes() == args.text.encode("utf-8"),
                        5, "preview differs from intended bytes")
            return await coordinator.push_preview(args.chat, args.candidate_id, args.text,
                json.loads(args.buttons_json), silent=args.silent, thread=args.thread)
        if args.verb in ("bind-command", "delivery-receipts", "delivery-ack"):
            value = load_request(sys.stdin)
            if args.verb == "bind-command":
                return 0, await coordinator.bind_command(args.chat, args.op, value["record"], value["auth"])
            function = coordinator.delivery_ack if args.verb == "delivery-ack" else coordinator.delivery_receipts
            return 0, await function(args.chat, value["request"], value["auth"])
        if args.verb == "po-notice-confirm":
            require(args.leader_root is not None and Path.cwd().resolve() == Path(args.leader_root).resolve(),
                    17, "confirmation must run from the leader workspace")
            return 0, await coordinator.confirm_po_notice(args.chat, args.notice_id, args.hold_id,
                args.leader_root, args.resolution, args.message_id, args.quiescent)
        if args.verb == "po-notice":
            return 0, await coordinator.po_notice(args.chat, args.event_id or "", args.text)
        if args.verb == "reconcile":
            return 0, await coordinator.reconcile(args.chat, args.epoch, args.op, args.stage, args.evidence)
        if args.verb == "open":
            return 0, await coordinator.open(args.chat, args.owner, args.surface, args.prefix,
                                             slots=args.slots.split(","), action_slot=args.action_slot,
                                             model_final=args.model_final, lease_s=args.lease_s, takeover=args.takeover,
                                             attach_dirs=args.attach_dir, answer_slot=args.answer_slot,
                                             adopt={item.split("=", 1)[0]: int(item.split("=", 1)[1]) for item in args.adopt})
        if args.verb == "touch":
            return 0, await coordinator.touch(args.chat, args.surface, args.epoch, args.lease_s)
        if args.verb == "close":
            return 0, await coordinator.close(args.chat, args.surface, args.epoch, args.reason, args.final)
        if args.verb == "verify":
            return await coordinator.verify(args.chat, load_request(sys.stdin))
        if args.verb == "reserve":
            return await coordinator.reserve(args.chat, args.surface, args.epoch, args.op, args.stage,
                args.frame, args.mode, args.slot, args.message_id, args.keyboard, args.photo is True)
        if args.verb == "commit":
            return 0, await coordinator.commit(args.chat, args.op, args.stage, args.seq,
                args.result, args.message_id, args.evidence)
        if args.verb == "complete":
            return 0, await coordinator.complete(args.chat, args.surface, args.epoch)
        if args.verb == "metrics":
            return 0, coordinator.metrics(args.chat, args.epoch)
        if args.verb == "push-notice":
            if not coordinator.activated():
                return 90, None
            require(bool(args.text) or bool(args.photo) or bool(args.notice_keyboard_file),
                    16, "framework notice requires precomposed content")
            identity = args.event_id or secrets.token_hex(16)
            buttons = json.loads(args.buttons_json)
            if args.button:
                label, separator, callback = args.button.partition("::")
                require(separator and label and 0 < len(callback.encode()) <= 64, 16, "invalid notice button")
                buttons = [[dict(text=label, callback_data=callback)]]
            text_payloads = (render_push_notice(args.root, args.text, buttons, args.strict_notice)
                             if args.render_notice else [dict(text=args.text, buttons=buttons,
                                                             parse_mode=args.parse_mode)]) if args.text else []
            if args.notice_keyboard_file:
                with args.notice_keyboard_file.open() as stream:
                    keyboard = load_request(stream)
                require(frozenset(keyboard) == {"text", "reply_markup"} and
                        frozenset(keyboard["reply_markup"]) == {"inline_keyboard"}, 16, "invalid rendered notice keyboard")
                text_payloads.append(dict(text=keyboard["text"], buttons=keyboard["reply_markup"]["inline_keyboard"]))
            for payload in text_payloads:
                require(0 < rendered_length(payload["text"], payload.get("parse_mode")) <= 4096,
                        16, "notice render overflow")
                require(isinstance(payload["buttons"], list), 16, "invalid notice buttons")
                for row in payload["buttons"]:
                    require(isinstance(row, list) and bool(row), 16, "invalid notice button row")
                    for button in row:
                        require(isinstance(button, dict) and frozenset(button) in (
                            {"text", "callback_data"}, {"text", "url"}) and isinstance(button["text"], str) and button["text"].strip(),
                            16, "invalid notice button")
                        if "callback_data" in button:
                            require(isinstance(button["callback_data"], str) and 0 < len(button["callback_data"].encode()) <= 64,
                                    16, "invalid notice callback")
                        else:
                            require(isinstance(button["url"], str) and button["url"].startswith("https://"), 16, "invalid notice URL")
            if args.photo:
                asset = coordinator._snapshot_attachment(args.chat, identity, args.photo)
                payload = dict(text=args.caption, raw_request=dict(method="sendPhoto", parameters=dict(
                    chat_id=args.chat, caption=args.caption, photo="attach://photo", disable_notification=args.silent),
                    files={"photo": {k: asset[k] for k in ("path", "digest", "filename")}}))
                await coordinator.enqueue_notice(args.chat, args.producer, identity + ":photo", payload)
            event_ids = [identity + ":photo"] if args.photo else []
            for index, payload in enumerate(text_payloads):
                payload["disable_notification"] = args.silent
                payload["link_preview_options"] = {"is_disabled": not args.link_preview}
                if (args.message_id and index == 0) or args.thread:
                    parameters = dict(chat_id=args.chat, text=payload["text"], disable_notification=args.silent,
                                      link_preview_options=payload["link_preview_options"])
                    if args.message_id and index == 0:
                        parameters["message_id"] = args.message_id
                    if args.thread:
                        parameters["message_thread_id"] = args.thread
                    if payload.get("parse_mode"):
                        parameters["parse_mode"] = payload["parse_mode"]
                    if payload.get("buttons"):
                        parameters["reply_markup"] = {"inline_keyboard": payload["buttons"]}
                    payload["raw_request"] = dict(method="editMessageText" if args.message_id and index == 0 else "sendMessage", parameters=parameters, files={})
                event_id = identity + (":text" if index == 0 else ":keyboard")
                event_ids.append(event_id)
                await coordinator.enqueue_notice(args.chat, args.producer, event_id, payload)
            await coordinator.flush_notices(args.chat)
            control = coordinator.control(args.chat)
            notices = [n for n in control["notices"].values() if n["intended"]["producer"] == args.producer and
                       n["intended"]["event_id"] in event_ids]
            return 0, dict(ok=True, accepted=True, delivered=all(n["status"] == "ok" for n in notices), notice_id=identity)
        if args.verb == "push-session":
            return await coordinator.submit_push(args.chat, load_request(sys.stdin), surface=args.surface,
                epoch=args.epoch, op=args.op, stage_id=args.stage, frame=args.frame, slot=args.slot,
                text=args.text, buttons=json.loads(args.buttons_json), photo=args.photo, message_id=args.message_id)
        if args.verb == "enqueue-notice":
            return 0, await coordinator.enqueue_notice(args.chat, args.producer, args.event_id, load_request(sys.stdin))
        return await coordinator.submit_frames(args.chat, load_request(sys.stdin))
    try:
        rc, result = asyncio.run(run())
    except Refusal as error:
        rc, result = error.rc, error.result
    except (OSError, ValueError, TypeError, KeyError) as error:
        rc, result = 40, dict(ok=False, diagnostic=str(error))
    if result is not None:
        print(canonical(result).decode())
    return rc


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.setdefault("bridge.live_surface", sys.modules[__name__])
    sys.exit(main())
