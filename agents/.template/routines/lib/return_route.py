#!/usr/bin/env python3
"""Generic durable dispatch returns and an optional workspace resolver.

DOGANY_PM_SEAT_RESOLVER takes the product slug and prints a workspace.
Without a resolver, returns go to the requesting workspace. No installed
kit state is read implicitly. Advanced queue activation belongs to its kit.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

URGENT = ("outage", "security", "hard_line")
URGENT_KEY = "- urgent:"
RESOLVER_ENV = "DOGANY_PM_SEAT_RESOLVER"
CAPABILITY = "dispatch-return-route/1"
ROUTE_FIELDS = ("schema", "instance_id", "kind", "destination", "product", "urgency",
                "job_kind", "parent_event_id", "config_generation", "runtime_policy")
_barriers = threading.local()


@contextlib.contextmanager
def inbox_publication_barrier(root, blocking=True):
    """Serialize selection/reservation, host state, publication and consumption.

    Synchronous host helpers may nest on the same thread. Async consumers
    must use a fresh module and nonblocking entry, retaining the descriptor
    across the injection and unlink. A process crash releases the flock.
    """
    # Refuse before mkdir/open even when a sourced test forgets its temp root.
    root = str(instance_root(root))
    held = getattr(_barriers, "held", {})
    if root in held:
        yield
        return
    path = Path(root) / ".telegram_bot/return-routes/publication.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        _barriers.held = dict(held, **{root: fd})
        yield
    finally:
        _barriers.held = held
        os.close(fd)


def instance_root(root):
    """Resolve an instance workspace without admitting the canonical source tree."""
    root = Path(root).resolve()
    # The marker sits at the checkout top; agents/.template (the template an
    # instance is minted from) is inside it and has none. Same rule as
    # General writers must never admit a framework source workspace.
    if (root / ".dogany-canonical").is_file() or ".template" in root.parts:
        raise ValueError("publication requires an instance workspace, not the framework checkout")
    return root


def legacy_route(record):
    # Enum emergency returns explicitly retain the leader bypass.
    return record is None or record["kind"] == "inbox" and not record["urgency"]


def publication_roots(root, record, legacy_inbox=None):
    roots = {str(Path(root).resolve())}
    if record and record["kind"] == "inbox" or record is None and legacy_inbox:
        inbox = Path(record["destination"] if record else legacy_inbox).resolve()
        # Historical callers may use an arbitrary inbox directory. It remains
        # under the submitter's barrier; only standard instance inboxes name
        # another seat whose activation must also be fenced.
        if inbox.name == "session-inbox" and inbox.parent.name == ".telegram_bot":
            roots.add(str(inbox.parent.parent))
    return sorted(roots)


@contextlib.contextmanager
def publisher_barriers(root, record=None, blocking=True, legacy_inbox=None):
    with contextlib.ExitStack() as locks:
        for workspace in publication_roots(root, record, legacy_inbox):
            locks.enter_context(inbox_publication_barrier(workspace, blocking))
        yield


def reservation_path(root, token):
    import re
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", token):
        raise ValueError("invalid return reservation")
    return instance_root(root) / ".telegram_bot/return-routes/reservations" / (token + ".json")


def release_reservation(root, token):
    if token:
        path = reservation_path(root, token)
        with inbox_publication_barrier(root):
            record = _read_inventory(path) if path.exists() else None
            # Selection already owns the source barrier; never wait for an
            # out-of-order destination lock while holding it.
            with contextlib.ExitStack() as locks:
                for workspace in publication_roots(root, record):
                    locks.enter_context(inbox_publication_barrier(workspace, blocking=False))
                for workspace in publication_roots(root, record):
                    target = reservation_path(workspace, token)
                    if target.exists():
                        target.unlink()
                        sync_dir(target.parent)


def reserve_route(root, token, record):
    """Called under the barrier before returning any legacy selection."""
    with publisher_barriers(root, record, blocking=False):
        if legacy_route(record):
            for workspace in publication_roots(root, record):
                immutable_json(reservation_path(workspace, token), record)


def _read_inventory(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid activation inventory: %s" % path)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, value):
    """Durable replacement, including the containing directory entry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def immutable_json(path, value):
    """Atomic create without overwrite; replay accepts only identical bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical(value) + b"\n"
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("immutable event payload conflict: %s" % path)
        sync_dir(path.parent)
    finally:
        os.unlink(tmp)


def publish_inbox_hold(leader_root, hold_id, payload, prefix, instruction):
    """One durable inbox publication, even across consume/publisher crashes.

    Consumer acknowledgement and publication share the leader barrier. The
    retained receipt prevents recreating a consumed drop after a lost reply.
    This is an explicit leader handoff, independent of legacy seat admission.
    """
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", hold_id):
        raise ValueError("invalid inbox hold identity")
    base = Path(leader_root) / ".telegram_bot"
    name = prefix + "-hold-" + hold_id + ".md"
    inbox = base / "session-inbox" / name
    consumed = base / (prefix + "-consumed") / name
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", prefix):
        raise ValueError("invalid inbox hold prefix")
    text = instruction + "\n" + canonical(payload).decode("ascii") + "\n"
    with inbox_publication_barrier(leader_root, blocking=False):
        if consumed.exists() or inbox.exists():
            prior = consumed if consumed.exists() else inbox
            if prior.read_text() != text:
                raise ValueError("inbox hold identity conflict")
            return
        inbox.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".po-notice-", dir=inbox.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(tmp, inbox)
            sync_dir(inbox.parent)
        finally:
            os.unlink(tmp)


def acknowledge_inbox_drop(path):
    """Caller retains the publication barrier through injection and receipt."""
    import re
    path = Path(path)
    held = re.fullmatch(r"([a-z][a-z0-9-]{0,63})-hold-[0-9a-f]{64}\.md", path.name)
    if held:
        receipt = path.parent.parent / (held[1] + "-consumed") / path.name
        receipt.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, receipt)
        sync_dir(receipt.parent)
        sync_dir(path.parent)
    else:
        path.unlink(missing_ok=True)


def queue_directory(value):
    if not isinstance(value, str) or not os.path.isabs(value):
        raise ValueError("queue destination must be an absolute registered directory")
    path = os.path.realpath(value)
    if "session-inbox" in Path(value).parts or "session-inbox" in Path(path).parts:
        raise ValueError("queue destination must be outside every session-inbox")
    return path


def retain_request(workspace, argv):
    """Keep a refused request and temporary evidence locally, without a route.

    Only routing inputs are recorded from the environment, never credentials.
    A repaired configuration can resolve the retained request on resubmission.
    """
    import base64
    root = instance_root(workspace)
    inputs = []
    for i, arg in enumerate(argv):
        if arg == "--":
            break
        if arg in ("--task", "--attach") and i + 1 < len(argv):
            path = Path(argv[i + 1])
            raw = path.read_bytes()
            inputs.append(dict(index=i + 1, path=str(path), sha256=sha256(raw),
                               body_base64=base64.b64encode(raw).decode("ascii")))
    request = dict(schema="unresolved-return-request/1", argv=list(argv), inputs=inputs,
                   routing_env={k: os.environ[k] for k in (
                       "DOGANY_RETURN_PRODUCT", "DOGANY_RETURN_URGENT", "DOGANY_RETURN_LEADER_WS",
                       "DOGANY_RETURN_ROUTE_CONFIG") if k in os.environ})
    path = root / ".telegram_bot/return-routes/unresolved" / (sha256(canonical(request)) + ".json")
    immutable_json(path, request)
    return str(path)


def validate_route(value):
    """Validate the pinned snapshot without consulting current registration.

    Rollback must drain already-pinned destinations. Only resolution consults
    registration; a replay verifies the complete record and its identity.
    """
    if not isinstance(value, dict) or set(value) != set(ROUTE_FIELDS) | {"route_id"}:
        raise ValueError("incomplete return-route/1 snapshot")
    fields = {key: value[key] for key in ROUTE_FIELDS}
    if (value["schema"] != "return-route/1" or value["kind"] not in ("inbox", "queue", "receipt")
            or value["job_kind"] not in ("work", "seat-review")
            or value["urgency"] not in ("",) + URGENT
            or not isinstance(value["instance_id"], str) or not value["instance_id"]
            or type(value["config_generation"]) is not int or value["config_generation"] < 0
            or not isinstance(value["destination"], str) or not os.path.isabs(value["destination"])
            or value["runtime_policy"] not in ("default", "codex-only")
            or any(value[k] is not None and not isinstance(value[k], str)
                   for k in ("product", "parent_event_id"))):
        raise ValueError("invalid return-route/1 fields")
    if value["job_kind"] == "seat-review":
        if value["kind"] != "receipt" or value["runtime_policy"] != "codex-only":
            raise ValueError("seat-review requires receipt and codex-only runtime")
    elif value["kind"] == "receipt":
        raise ValueError("ordinary work cannot use a model receipt route")
    if value["kind"] != "inbox":
        queue_directory(value["destination"])
    if value["route_id"] != sha256(canonical(fields)):
        raise ValueError("return route identity mismatch")
    return dict(value)


def event_for(route_record, event_id, **fields):
    r = validate_route(route_record)
    value = dict(schema="po-event/1", event_id=event_id, route_id=r["route_id"],
                 instance_id=r["instance_id"], kind=r["job_kind"], product=r["product"],
                 ticket=None, lane=None, runid=None, host_ticket=None,
                 parent_event_id=r["parent_event_id"], urgency=r["urgency"], result=None,
                 exit_code=None, head=None, report_path=None, report_sha256=None,
                 evidence_refs=[], created_at=timestamp())
    value.update(fields)
    return value


def persist_event(outbox, route_record, event):
    """Freeze intent before terminal/lifecycle mutation, then publish separately."""
    outbox = Path(outbox)
    path = outbox / "return.event.json"
    if path.exists():
        prior = json.loads(path.read_text())
        if prior["event_id"] != event["event_id"] or prior["route_id"] != event["route_id"]:
            raise ValueError("outbox identity conflict")
        event = prior
    else:
        immutable_json(path, event)
    immutable_json(outbox / "return.route.json", validate_route(route_record))
    if not (outbox / "return.publication.json").exists():
        atomic_json(outbox / "return.publication.json", dict(state="pending", event_id=event["event_id"]))
    return event


def publish_event(outbox, route_record):
    """Recoverable publication; failure retains a pending outbox, never live fallback."""
    outbox = Path(outbox)
    r = validate_route(route_record)
    event = json.loads((outbox / "return.event.json").read_text())
    if event["route_id"] != r["route_id"]:
        raise ValueError("event/route mismatch")
    if r["kind"] == "inbox":
        raise ValueError("legacy text publication belongs to its original writer")
    receipt = dict(event_id=event["event_id"], payload_sha256=sha256(canonical(event)),
                   destination=r["destination"], published_at=timestamp())
    try:
        immutable_json(Path(r["destination"]) / (sha256(event["event_id"].encode()) + ".json"), event)
    except ValueError as error:
        atomic_json(outbox / "return.publication.json", dict(receipt, state="conflict", error=str(error)))
        return "conflict"
    except OSError as error:
        atomic_json(outbox / "return.publication.json", dict(state="pending", event_id=event["event_id"], error=str(error)))
        return "pending"
    atomic_json(outbox / "return.receipt.json", receipt)
    atomic_json(outbox / "return.publication.json", dict(receipt, state="published"))
    return "published"


def urgency(flag):
    """The enum value, or "" -- free text never counts as urgent."""
    flag = str(flag or "").strip()
    return flag if flag in URGENT else ""


def pm_seat(product):
    cmd = os.environ.get(RESOLVER_ENV, "").strip()
    if not cmd or not product:
        return ""
    try:
        argv = shlex.split(cmd)
        if not argv:
            return ""
        r = subprocess.run(argv + [product], capture_output=True, text=True, timeout=5)
    except ValueError as exc:
        print("return-route: PM seat resolver command is invalid: %s" % exc,
              file=sys.stderr)
        return ""
    except (OSError, subprocess.SubprocessError) as exc:
        print("return-route: PM seat resolver could not run: %s" % exc,
              file=sys.stderr)
        return ""
    if r.returncode != 0:
        cause = r.stderr.strip() or "exit status %d" % r.returncode
        print("return-route: PM seat resolver failed: %s" % cause,
              file=sys.stderr)
        return ""
    return r.stdout.strip()


def route(product, flag, live, po_ws, leader_ws, seat_of=pm_seat):
    """{to, via, held, urgent}: via is leader | pm | po."""
    u = urgency(flag)
    if u and leader_ws:
        return {"to": leader_ws, "via": "leader", "held": False, "urgent": u}
    seat = seat_of(product)
    to, via = (seat, "pm") if seat else (po_ws, "po")
    return {"to": to, "via": via, "held": bool(live), "urgent": ""}


def inbox_urgent(text):
    """True for a drop carrying an enum '- urgent: <value>' line in its head."""
    for line in str(text).splitlines()[:40]:
        if line.startswith(URGENT_KEY):
            return urgency(line[len(URGENT_KEY):]) != ""
    return False


def main(argv):
    if len(argv) > 1 and argv[1] == "--capabilities":
        print(CAPABILITY)
        return 0
    if len(argv) > 1 and argv[1] == "--release-reservation":
        release_reservation(argv[2], argv[3])
        return 0
    if len(argv) > 1 and argv[1] == "--recover":
        outbox = Path(argv[2])
        print(publish_event(outbox, json.loads((outbox / "return.route.json").read_text())))
        return 0
    if len(argv) > 1 and argv[1] == "--return-route-json":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--return-route-json", action="store_true")
        parser.add_argument("--snapshot", default="")
        parser.add_argument("--reservation", default="")
        parser.add_argument("--product", default="")
        parser.add_argument("--urgent", default="")
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--leader", default="")
        parser.add_argument("--job-kind", default="work")
        parser.add_argument("--parent-event-id", default=None)
        parser.add_argument("--legacy-inbox", default=None)
        args = parser.parse_args(argv[1:])
        try:
            with inbox_publication_barrier(args.workspace):
                value = (validate_route(json.loads(args.snapshot)) if args.snapshot else
                         resolve_json(args.product, args.urgent, args.workspace, args.leader,
                                      args.job_kind, args.parent_event_id, args.legacy_inbox))
                if args.reservation:
                    reserve_route(args.workspace, args.reservation, value)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print("return-route: retained locally; resolution refused: %s" % error, file=sys.stderr)
            return 2
        print(canonical(value).decode())
        return 0
    if len(argv) < 4:
        print("usage: return_route.py <product> <urgent-flag|-> <po_ws> [leader_ws]", file=sys.stderr)
        return 2
    flag = "" if argv[2] == "-" else argv[2]
    r = route(argv[1], flag, False, argv[3], argv[4] if len(argv) > 4 else "")
    print(r["to"])
    return 0


def resolve_json(product, flag, workspace, leader_ws="", job_kind="work", parent_event_id=None,
                 legacy_inbox=None):
    """Pin the caller's return destination before admission.

    No resolver or seat configuration is required for ordinary dispatch.
    Kit dispatchers own their queue/receipt route creation and recovery.
    """
    if job_kind != "work":
        raise ValueError("this job kind requires a configured job driver")
    selected = route(product, flag, False, workspace, leader_ws)
    destination = os.path.join(selected["to"], ".telegram_bot", "session-inbox")
    if not product and not selected["urgent"] and legacy_inbox:
        destination = legacy_inbox
    fields = dict(schema="return-route/1", instance_id=sha256(os.path.realpath(workspace).encode())[:24],
                  kind="inbox", destination=destination, product=product or None,
                  urgency=selected["urgent"], job_kind=job_kind,
                  parent_event_id=parent_event_id or None, config_generation=0, runtime_policy="default")
    return validate_route(dict(fields, route_id=sha256(canonical(fields))))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
