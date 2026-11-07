"""Absolute durable due events, grouped before launch and hosted by the bridge."""

# Support the installed direct-script CLI without importing bridge configuration.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys

from bridge import callback_routes
from bridge.session_activation import load_activation, require_active
from bridge.session_state import (Refusal, atomic_json, chat_lock, data_dir,
                                  digest, integer, locked, now_ms, read_json)

logger = logging.getLogger(__name__)
EVENT_ID = re.compile(r"[a-z0-9_]{1,40}\.[A-Za-z0-9_-]{1,64}")


class SurfaceAccess:
    """M1 remains the sole writer of surface state and transport cancellation."""
    def __init__(self, root):
        self.root = Path(root)

    def current(self, chat):
        path = data_dir(self.root) / (str(chat) + ".json")
        if not path.exists():
            return None
        value = read_json(path)
        if value.get("schema") != "live_surface/1" or value.get("chat_id") != chat:
            raise Refusal(40, "invalid scheduler surface snapshot")
        # Open is a privileged lifecycle call. The scheduler can use the sole
        # installed principal for that chat, never guess among several users.
        if value.get("state") == "open":
            route = callback_routes.installed(self.root, value["owner"])
            require_active(self.root, value["owner"], digest(route), value.get("generation", 0))
            config_path = route.get("env", {}).get("LIVE_ADAPTER_CONFIG")
            if config_path is None:
                raise Refusal(18, "scheduler principal configuration unavailable")
            from bridge.session_state import adapter_config, contained
            config = adapter_config(read_json(contained(self.root, config_path)))
            principals = [p for p in config["identities"] if p["chat_id"] == chat]
            if len(principals) != 1 or value.get("principal", principals[0]) != principals[0]:
                raise Refusal(18, "scheduler principal is missing, ambiguous or changed")
            value = dict(value, principal=principals[0])
        return value

    def raise_due_floor(self, chat, surface, epoch, deadline):
        try:
            from bridge import live_surface
            live_surface.raise_due_floor(self.root, chat, surface, epoch, deadline)
        except (ImportError, AttributeError) as exc:
            raise Refusal(40, "surface coordinator due-floor API unavailable") from exc

    def cancel_group(self, chat, op):
        try:
            from bridge import live_surface
            live_surface.cancel_due_group(self.root, chat, op)
        except (ImportError, AttributeError) as exc:
            raise Refusal(40, "surface coordinator cancellation API unavailable") from exc


class DueEvents:
    def __init__(self, root, surface=None, recovery=None):
        self.root = Path(root)
        self.directory = self.root / ".telegram_bot/due"
        self.surface = surface or SurfaceAccess(root)
        self.recovery = recovery

    def path(self, event_id):
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise Refusal(2, "invalid due event id")
        return self.directory / (event_id + ".json")

    def group_path(self, op):
        return self.directory / "groups" / (hashlib.sha256(op.encode()).hexdigest() + ".json")

    def records(self):
        rows = [read_json(path) for path in sorted(self.directory.glob("*.json"))]
        for row in rows:
            if (not isinstance(row, dict) or row.get("schema") != "due_event/1"
                    or not EVENT_ID.fullmatch(row.get("id", ""))
                    or not integer(row.get("due_at_ms")) or not integer(row.get("chat_id"))
                    or row.get("state") not in ("pending", "firing", "done", "cancelled", "fenced", "failed")):
                raise Refusal(40, "corrupt due event")
        return rows

    def groups(self):
        rows = [read_json(path) for path in sorted((self.directory / "groups").glob("*.json"))]
        for row in rows:
            if (not isinstance(row, dict) or row.get("schema") != "due_group/1"
                    or not isinstance(row.get("members"), list) or not row["members"]
                    or row.get("state") not in ("firing", "done", "cancelled", "fenced", "failed")
                    or row.get("op") != row.get("envelope", {}).get("envelope_id")
                    or row.get("binding_digest") != digest(row.get("envelope"))):
                raise Refusal(40, "corrupt canonical due group")
            try:
                envelope = callback_routes.authenticated(self.root, row["envelope"])
                ids = [event["id"] for event in envelope["events"]]
                epoch = envelope["surface"]["epoch"] if envelope["surface"] else 0
                expected_op = "due:" + ids[0] + ":" + str(epoch)
                if (row["members"] != ids or row["op"] != expected_op
                        or row["chat_id"] != envelope["chat_id"]
                        or row["route_digest"] != digest(row["route"])
                        or row["aliases"] != {"due:" + i + ":" + str(epoch): expected_op for i in ids}):
                    raise Refusal(40, "canonical due membership or authority changed")
            except (Refusal, KeyError, ValueError, TypeError) as exc:
                raise Refusal(40, "corrupt canonical due group binding") from exc
        return rows

    def create(self, event_id, due_at_ms, argv, chat, surface=None, epoch=None,
               coalesce_key=None, coalesce_ms=2000, max_late_ms=3600000):
        path = self.path(event_id)
        owner = event_id.split(".", 1)[0]
        now = now_ms()
        if (not integer(chat) or not integer(due_at_ms) or not now < due_at_ms <= now + 86400000
                or not integer(coalesce_ms, 0) or coalesce_ms > 5000
                or not integer(max_late_ms, 0)
                or (surface is None) != (epoch is None)
                or surface is not None and (not isinstance(surface, str) or not surface or not integer(epoch, 1))
                or coalesce_key is not None and (not isinstance(coalesce_key, str) or not coalesce_key)):
            raise Refusal(2, "invalid absolute due event")
        with chat_lock(self.root, chat), locked(self.directory / ".lock"):
            try:
                route = callback_routes.installed(self.root, owner)
                if argv != route["due_argv"]:
                    raise Refusal(15, "due argv differs from installed authority")
            except Refusal as exc:
                raise Refusal(15, "installed due argv unavailable or mismatched") from exc
            active = require_active(self.root, owner, digest(route))
            current = self.surface.current(chat)
            if surface is not None and (current is None or current["state"] != "open"
                    or current["surface_id"] != surface or current["epoch"] != epoch
                    or current["owner"] != owner):
                raise Refusal(10, "due surface is closed or moved")
            rows = self.records()
            prior = next((row for row in rows if row["id"] == event_id), None)
            # Reusing a terminal/firing id could resurrect an old logical command.
            if prior and prior["state"] != "pending":
                raise Refusal(10, "due event identity already admitted or terminal")
            if any(event_id in g["members"] for g in self.groups()):
                raise Refusal(10, "due event is already a canonical group member")
            if sum(r["owner"] == owner and r["state"] == "pending" for r in rows) >= 64 and prior is None:
                raise Refusal(14, "owner has 64 pending due events")
            # A crash after floor extension but before event write only lengthens
            # an inactivity lease. The reverse ordering could lose a deadline.
            if surface is not None:
                self.surface.raise_due_floor(chat, surface, epoch, due_at_ms + 600000)
            record = {"schema": "due_event/1", "id": event_id, "owner": owner, "chat": "owner",
                      "chat_id": chat, "due_at_ms": due_at_ms,
                      "surface": {"surface_id": surface, "epoch": epoch} if surface else None,
                      "on_due": {"argv": argv, "timeout_s": 10},
                      "coalesce_key": coalesce_key or surface or event_id,
                      "coalesce_ms": coalesce_ms, "max_late_ms": max_late_ms,
                      "state": "pending", "attempt": 0, "created_at_ms": now,
                      "fired_at_ms": None, "route_digest": digest(route),
                      "generation": active["generation"]}
            atomic_json(path, record)
            return {"ok": True, "id": event_id, "replaced": prior is not None}

    def _project(self, group):
        for event_id in group["members"]:
            event = read_json(self.path(event_id))
            event.update(state=group["state"], group=group["op"], attempt=group["attempt"],
                         fired_at_ms=group["dispatch_at_ms"])
            atomic_json(self.path(event_id), event)

    def repair(self):
        # Must run before inspecting pending projections or inactivity expiry.
        for group in self.groups():
            with chat_lock(self.root, group["chat_id"]), locked(self.directory / ".lock"):
                group = read_json(self.group_path(group["op"]))
                self._project(group)

    def cancel(self, chat, event_id=None, surface=None, epoch=None):
        if event_id is None and (not surface or not integer(epoch, 1)):
            raise Refusal(2, "cancel requires an id or exact surface/epoch")
        if event_id is not None:
            self.path(event_id)
        count = 0
        with chat_lock(self.root, chat), locked(self.directory / ".lock"):
            groups = self.groups()
            rows = self.records()
            matched = [r for r in rows if r["chat_id"] == chat and r["state"] in ("pending", "firing")
                       and (r["id"] == event_id if event_id is not None else
                            r["surface"] == {"surface_id": surface, "epoch": epoch})]
            ids = [r["id"] for r in matched]
            for group in groups:
                if any(i in group["members"] for i in ids):
                    # The durable decision wins on restart even if drain is pending.
                    group["state"] = "cancelled"
                    group["drain_pending"] = True
                    atomic_json(self.group_path(group["op"]), group)
                    self._project(group)
                    self.surface.cancel_group(chat, group["op"])
                    group["drain_pending"] = False
                    atomic_json(self.group_path(group["op"]), group)
            for row in matched:
                row["state"] = "cancelled"
                atomic_json(self.path(row["id"]), row)
                count += 1
        return {"ok": True, "cancelled": count}

    def cancel_undispatched(self, owner, generation):
        """B4 only: the caller holds the transition and actual-writer fences.

        An admitted group retains its original ingress/command/delivery even
        if a crash left an event projection pending. Never cancel those frames.
        """
        current = load_activation(self.root, owner)
        if current["phase"] != "deactivating" or current["generation"] != generation:
            raise Refusal(35, "deadline retirement requires the deactivating generation")
        if not self.directory.exists():
            return
        with locked(self.directory / ".lock"):
            groups = [g for g in self.groups() if g["owner"] == owner]
            members = {i for g in groups for i in g["members"]}
            for group in groups:
                if group["state"] == "firing" and not Path(group["ingress"]).exists():
                    group["state"] = "cancelled"
                    atomic_json(self.group_path(group["op"]), group)
                    self._project(group)
            for row in self.records():
                if row["owner"] == owner and row["state"] == "pending" and row["id"] not in members:
                    row["state"] = "cancelled"
                    atomic_json(self.path(row["id"]), row)

    def _eligible_surface(self, row):
        if row["surface"] is None:
            return True
        current = self.surface.current(row["chat_id"])
        # Explicit close/new epoch wins; inactivity alone never cancels recovery.
        return (current is not None and current["state"] == "open"
                and current["epoch"] == row["surface"]["epoch"]
                and current["surface_id"] == row["surface"]["surface_id"])

    def group_due(self, dispatch_now=None):
        self.repair()
        dispatch_now = now_ms() if dispatch_now is None else dispatch_now
        created = []
        chats = sorted({r["chat_id"] for r in self.records() if r["state"] == "pending"})
        for chat in chats:
            with chat_lock(self.root, chat), locked(self.directory / ".lock"):
                rows = sorted((r for r in self.records() if r["chat_id"] == chat
                               and r["state"] == "pending" and r["due_at_ms"] <= dispatch_now),
                              key=lambda r: (r["due_at_ms"], r["id"]))
                while rows:
                    first = rows.pop(0)
                    if not self._eligible_surface(first):
                        first["state"] = "fenced"
                        atomic_json(self.path(first["id"]), first)
                        continue
                    if dispatch_now < first["due_at_ms"] + first["coalesce_ms"]:
                        continue
                    try:
                        route = callback_routes.installed(self.root, first["owner"])
                        active = require_active(self.root, first["owner"], first["route_digest"], first["generation"])
                        if route["due_argv"] != first["on_due"]["argv"]:
                            raise Refusal(15, "due argv changed")
                    except Refusal as exc:
                        first.update(state="failed", diagnostic=str(exc))
                        atomic_json(self.path(first["id"]), first)
                        continue
                    identity = lambda r: (r["owner"], r["chat_id"], r["surface"], r["coalesce_key"],
                                          r["route_digest"], r["on_due"], r["generation"])
                    members = [first] + [r for r in rows if identity(r) == identity(first)
                                        and r["due_at_ms"] <= first["due_at_ms"] + first["coalesce_ms"]]
                    rows = [r for r in rows if r not in members]
                    epoch = first["surface"]["epoch"] if first["surface"] else 0
                    op = "due:" + first["id"] + ":" + str(epoch)
                    ids = [r["id"] for r in members]
                    events = [{"id": r["id"], "due_at_ms": r["due_at_ms"],
                               "late_ms": dispatch_now - r["due_at_ms"]} for r in members]
                    current_surface = self.surface.current(chat)
                    principal = (current_surface.get("principal") if first["surface"] else
                                 active["transition_checkpoint"].get("principal"))
                    if (not isinstance(principal, dict) or principal.get("chat_id") != chat
                            or not integer(principal.get("user_id"))):
                        raise Refusal(18, "due work has no stored authenticated principal")
                    envelope = callback_routes.sign(self.root, {
                        "v": 1, "kind": "due", "envelope_id": op, "chat_id": chat,
                        "user_id": principal["user_id"], "surface": first["surface"], "events": events,
                        "redelivery": False, "attempt": 1, "issued_at_ms": dispatch_now,
                        "nonce": secrets.token_hex(16)})
                    group = {"schema": "due_group/1", "op": op, "members": ids,
                             "aliases": {"due:" + i + ":" + str(epoch): op for i in ids},
                             "owner": first["owner"], "chat_id": chat, "envelope": envelope,
                             "binding_digest": digest(envelope), "route": route,
                             "route_digest": first["route_digest"], "generation": first["generation"],
                             "dispatch_at_ms": dispatch_now, "state": "firing", "attempt": 0,
                             "ingress": str(callback_routes.ingress_path(self.root, chat, op))}
                    atomic_json(self.group_path(op), group)
                    self._project(group)
                    created.append(group)
        return created

    def dispatch(self, group):
        with chat_lock(self.root, group["chat_id"]), locked(self.directory / ".lock"):
            group = read_json(self.group_path(group["op"]))
            if group["state"] != "firing":
                return group
            first = read_json(self.path(group["members"][0]))
            if not self._eligible_surface(first):
                group["state"] = "fenced"
                atomic_json(self.group_path(group["op"]), group)
                self._project(group)
                return group
        # Admission owns its own chat lock. Recheck cancellation at the launch
        # barrier below, including the crash window after this admission.
        path = Path(group["ingress"])
        if not path.exists():
            callback_routes.admit(self.root, group["owner"], group["envelope"],
                                  group["route"]["due_argv"], 10)
        record = read_json(path)
        with chat_lock(self.root, group["chat_id"]), locked(self.directory / ".lock"):
            current = read_json(self.group_path(group["op"]))
            if current["state"] != "firing":
                return current
            if current["attempt"] == 0 and record["dispatch_state"] == "accepted":
                current["attempt"] = 1
            elif record["dispatch_state"] != "complete":
                if self.recovery is None or now_ms() < current.get("retry_at_ms", 0):
                    return current
                if current.get("recovery_attempts", 0) >= 2:
                    current.update(state="failed", unresolved=True)
                    atomic_json(self.group_path(current["op"]), current)
                    self._project(current)
                    logger.warning("Due handler recovery exhausted: %s", current["op"])
                    return current
                current["attempt"] += 1
                current["recovery_attempts"] = current.get("recovery_attempts", 0) + 1
            current["retry_at_ms"] = now_ms() + 5000
            atomic_json(self.group_path(current["op"]), current)
            self._project(current)
        try:
            if record["dispatch_state"] == "accepted":
                callback_routes.launch(self.root, path)
            elif record["dispatch_state"] != "complete" and self.recovery is not None:
                callback_routes.recover(self.root, path, self.recovery)
        except (Refusal, OSError, ValueError) as exc:
            logger.warning("Due handler remains pending: %s rc=%s", group["op"], getattr(exc, "rc", 40))
        record = read_json(path)
        with chat_lock(self.root, group["chat_id"]), locked(self.directory / ".lock"):
            current = read_json(self.group_path(group["op"]))
            if current["state"] == "firing" and record["dispatch_state"] == "complete":
                current["state"] = "done"
                atomic_json(self.group_path(current["op"]), current)
                self._project(current)
            elif current["state"] == "firing" and current.get("recovery_attempts", 0) >= 2:
                current.update(state="failed", unresolved=True)
                atomic_json(self.group_path(current["op"]), current)
                self._project(current)
                logger.warning("Due handler recovery exhausted: %s", current["op"])
            return current

    def tick(self):
        self.group_due()
        for group in self.groups():
            if group["state"] == "firing":
                self.dispatch(group)
            elif group.get("drain_pending"):
                self.surface.cancel_group(group["chat_id"], group["op"])
                with chat_lock(self.root, group["chat_id"]), locked(self.directory / ".lock"):
                    current = read_json(self.group_path(group["op"]))
                    current["drain_pending"] = False
                    atomic_json(self.group_path(group["op"]), current)

    async def run(self, on_poll=None):
        while True:
            try:
                if on_poll is not None:
                    await asyncio.to_thread(on_poll)
                # Recover admitted callbacks before polling new due work.
                await asyncio.to_thread(callback_routes.poll_ingress, self.root, self.recovery)
                # No files, locks or key are created in an inactive instance.
                if self.directory.exists():
                    await asyncio.to_thread(self.tick)
            except (Refusal, OSError, ValueError, KeyError) as exc:
                logger.warning("Due recovery remains pending: %s", exc)
            await asyncio.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("INSTANCE_ROOT", Path(__file__).resolve().parents[1])))
    parser.add_argument("--chat", type=int)
    verbs = parser.add_subparsers(dest="verb", required=True)
    create = verbs.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--due-at-ms", type=int, required=True)
    create.add_argument("--argv-json", required=True)
    create.add_argument("--surface")
    create.add_argument("--epoch", type=int)
    create.add_argument("--coalesce-key")
    create.add_argument("--coalesce-ms", type=int, default=2000)
    cancel = verbs.add_parser("cancel")
    cancel.add_argument("--id")
    cancel.add_argument("--surface")
    cancel.add_argument("--epoch", type=int)
    listing = verbs.add_parser("list")
    listing.add_argument("--owner")
    listing.add_argument("--surface")
    args = parser.parse_args(argv)
    try:
        due = DueEvents(args.root)
        if args.verb == "list":
            for row in due.records():
                if (args.owner is None or args.owner == row["owner"]) and (args.surface is None or
                        row["surface"] and args.surface == row["surface"]["surface_id"]):
                    print(json.dumps(row, ensure_ascii=True))
            return 0
        chat = args.chat
        if chat is None:
            from bridge import config as config_module, ownership
            mode, owner_id = ownership.resolve_owner(config_module.config.allowed_user_ids,
                                                      config_module.config.bot_data_dir)
            if mode == ownership.MODE_AUTHORITATIVE:
                chat = config_module.config.allowed_user_ids[0]
            elif mode == ownership.MODE_OWNER_LOCK:
                chat = owner_id
            else:
                raise Refusal(2, "instance owner unavailable")
        if args.verb == "create":
            result = due.create(args.id, args.due_at_ms, json.loads(args.argv_json), chat,
                                args.surface, args.epoch, args.coalesce_key, args.coalesce_ms)
        else:
            result = due.cancel(chat, args.id, args.surface, args.epoch)
        print(json.dumps(result))
        return 0
    except (Refusal, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return getattr(exc, "rc", 2)


if __name__ == "__main__":
    sys.exit(main())
