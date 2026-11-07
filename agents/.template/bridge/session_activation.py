"""Durable engine selection and fail-closed capability probing.

The transport coordinator supplies authenticated transition evidence. Installing
routes records intent only; neither environment flags nor route deletion grant
authority. No domain implementation or database migration lives here.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import re
import stat
import subprocess
import sys

from bridge.session_state import (Refusal, atomic_json, canonical, chat_lock,
                                  data_dir, digest, integer, locked, now_ms,
                                  pack_name, read_json)
from bridge import callback_routes

PHASES = ("staged", "activating", "active", "deactivating", "legacy")
logger = logging.getLogger(__name__)


def activation_path(root, pack):
    return Path(root) / "config/packs" / (pack_name(pack) + ".session_activation.json")


def load_activation(root, pack):
    path = activation_path(root, pack)
    if not path.exists():
        return {"schema": "session_activation/1", "generation": 0, "phase": "legacy",
                "selected_engine": "legacy", "route_digest": None, "legacy_fence": None,
                "v2_fence": None, "retirement_evidence": None, "transition_checkpoint": None}
    value = read_json(path)
    fields = ["generation", "legacy_fence", "phase", "retirement_evidence", "route_digest",
              "schema", "selected_engine", "transition_checkpoint", "v2_fence"]
    if (not isinstance(value, dict) or sorted(value) != fields
            or value["schema"] != "session_activation/1" or not integer(value["generation"], 1)
            or value["phase"] not in PHASES or value["selected_engine"] not in ("legacy", "v2")
            or (value["phase"] == "active" and value["selected_engine"] != "v2")
            or (value["phase"] in ("legacy", "staged", "activating") and value["selected_engine"] != "legacy")
            or (value["phase"] == "deactivating" and value["selected_engine"] != "v2")):
        raise Refusal(40, "invalid activation authority")
    return value


def require_active(root, pack, route_digest=None, generation=None):
    value = load_activation(root, pack)
    if value["phase"] != "active" or value["selected_engine"] != "v2":
        raise Refusal(10, "session route is inactive or transitioning")
    if (route_digest is not None and value["route_digest"] != route_digest
            or generation is not None and value["generation"] != generation):
        raise Refusal(35, "activation generation or installed route changed")
    if not value["legacy_fence"] or value["retirement_evidence"] is None:
        raise Refusal(35, "activation lacks retirement/fence evidence")
    return value


def stage(root, pack, route_digest):
    with locked(activation_path(root, pack).with_suffix(".lock")):
        current = load_activation(root, pack)
        if current["phase"] not in ("staged", "legacy"):
            raise Refusal(35, "deactivate before replacing active routes")
        current.update(schema="session_activation/1", generation=max(1, current["generation"]),
                       phase="staged", selected_engine="legacy", route_digest=route_digest,
                       transition_checkpoint=None)
        atomic_json(activation_path(root, pack), current)
        return current


def transition(root, pack, chat, target, coordinator):
    """Resume a crash-safe transition, using the coordinator's trusted evidence.

    validate_staged verifies schema/fallback and nonmutating pack probes.
    prepare_transition fences actual I/O and returns a durable recovery identity.
    transition_evidence drains/reconciles and checks every retirement receipt.
    It returns None while pending; no caller-supplied boolean can activate.
    """
    if target not in ("v2", "legacy"):
        raise Refusal(2, "invalid selected engine")
    path = activation_path(root, pack)
    # Lock ordering is chat barrier then activation authority everywhere.
    with chat_lock(root, chat), locked(path.with_suffix(".lock")):
        current = load_activation(root, pack)
        if current["phase"] == ("active" if target == "v2" else "legacy"):
            return current
        route = callback_routes.installed(root, pack)
        if current["route_digest"] != digest(route):
            raise Refusal(33, "staged route digest mismatch")
        pending_phase = "activating" if target == "v2" else "deactivating"
        if current["phase"] != pending_phase:
            allowed = ("staged", "legacy") if target == "v2" else ("active",)
            if current["phase"] not in allowed:
                raise Refusal(35, "finish original transition before changing direction")
            coordinator.validate_staged(pack, route, target)
            current.update(generation=current["generation"] + 1, phase=pending_phase,
                           transition_checkpoint={"chat_id": chat, "target": target,
                                                  "retire_prefixes": route.get("retire_prefixes", []),
                                                  "id": secrets.token_hex(16)})
            atomic_json(path, current)
        if current["transition_checkpoint"]["chat_id"] != chat:
            raise Refusal(35, "transition belongs to a different chat")
        coordinator.prepare_transition(pack, current)
        evidence = coordinator.transition_evidence(pack, current)
        if evidence is None:
            return current
        coordinator.validate_transition_evidence(pack, current, evidence)
        required = ("legacy_fence", "retirement_evidence") if target == "v2" else ("v2_fence", "retirement_evidence")
        if (not isinstance(evidence, dict) or any(evidence.get(k) is None for k in required)
                or not evidence[required[0]]):
            raise Refusal(35, "transition retirement/fence evidence incomplete")
        current.update({k: evidence[k] for k in required})
        if target == "v2":
            # The kit admits the new generation only with its closure; a
            # replay after a crash here re-presents the identical closure.
            hook = getattr(coordinator, "activate_closure", None)
            if hook is not None:
                hook(pack, current)
        current.update(phase="active" if target == "v2" else "legacy", selected_engine=target)
        if target == "legacy":
            current["generation"] += 1
        atomic_json(path, current)
        return current


def authorize(root, pack, generation, engine, recovery=None):
    """Called under the chat barrier immediately before actual transport I/O."""
    value = load_activation(root, pack)
    if (engine == "legacy" and value["phase"] in ("legacy", "staged")
            and not value["legacy_fence"] and not value["v2_fence"]):
        return value
    if value["generation"] != generation:
        raise Refusal(10, "obsolete worker generation")
    if value["phase"] in ("activating", "deactivating"):
        if recovery is None or not recovery.authorize_transition(value):
            raise Refusal(10, "transition blocks ordinary worker I/O")
    elif engine != value["selected_engine"]:
        raise Refusal(10, "inactive worker engine")
    return value


class TransitionCoordinator:
    """Persist the framework side of B4; transport/domain services own I/O.

    The trusted io object must fence ALL pack writers, including legacy
    non-session producers, and reconcile a lost retirement acknowledgement
    by the same checkpoint/target. A receipt is never inferred from an empty
    keyboard, a subprocess exit, or a timeout. The inventory is captured only
    after the writer fence and drain, and is immutable across recovery.
    """
    def __init__(self, root, io):
        self.root = Path(root)
        self.io = io

    def path(self, current):
        return data_dir(self.root) / "transitions" / (current["transition_checkpoint"]["id"] + ".json")

    def validate_staged(self, pack, route, target):
        # The domain owner checks the actual installed schema, additive
        # allocation above 081 and shipped forward fallback, without migration.
        self.io.validate_compatibility(pack, route, target)

    def prepare_transition(self, pack, current):
        path = self.path(current)
        checkpoint = current["transition_checkpoint"]
        engine = "legacy" if checkpoint["target"] == "v2" else "v2"
        if not path.exists():
            atomic_json(path, {"pack": pack, "generation": current["generation"],
                              "checkpoint": checkpoint, "engine": engine,
                              "fence": None, "inventory": None, "receipts": []})
        value = read_json(path)
        if (value["pack"] != pack or value["generation"] != current["generation"]
                or value["checkpoint"] != checkpoint or value["engine"] != engine):
            raise Refusal(35, "transition checkpoint changed")
        if value["fence"] is None:
            value["fence"] = self.io.fence_writers(pack, engine, current["generation"], checkpoint)
            if not value["fence"]:
                raise Refusal(35, "actual writer fence unavailable")
            self.io.verify_writer_fence(pack, engine, value["fence"], checkpoint)
            atomic_json(path, value)
        if engine == "v2":
            self.io.verify_writer_fence(pack, engine, value["fence"], checkpoint)
            from bridge.due_events import DueEvents
            DueEvents(self.root).cancel_undispatched(pack, current["generation"])

    def transition_evidence(self, pack, current):
        path = self.path(current)
        value = read_json(path)
        checkpoint = current["transition_checkpoint"]
        self.io.verify_writer_fence(pack, value["engine"], value["fence"], checkpoint)
        if self.io.drain(pack, value["engine"], value["fence"], checkpoint) is not True:
            return None
        if value["inventory"] is None:
            inventory = self.io.inventory(pack, value["engine"], checkpoint)
            if (not isinstance(inventory, list) or any(
                    not isinstance(row, dict) or sorted(row) != ["chat_id", "message_id"]
                    or not integer(row["chat_id"]) or not integer(row["message_id"], 1)
                    for row in inventory)
                    or len({(r["chat_id"], r["message_id"]) for r in inventory}) != len(inventory)):
                raise Refusal(35, "invalid complete keyboard inventory")
            value["inventory"] = inventory
            atomic_json(path, value)
        for target in value["inventory"][len(value["receipts"]):]:
            receipt = self.io.retire(pack, target, value["fence"], checkpoint)
            if receipt is None:
                return None
            self.io.verify_retirement(pack, target, receipt, checkpoint)
            value["receipts"].append(receipt)
            atomic_json(path, value)
        # drain includes admitted command/frame dependencies and late receipts,
        # not merely the number of running child processes.
        if self.io.drain(pack, value["engine"], value["fence"], checkpoint) is not True:
            return None
        key = "legacy_fence" if value["engine"] == "legacy" else "v2_fence"
        return {key: value["fence"], "retirement_evidence": {
            "complete": True, "worker_fenced": True, "inventory": value["inventory"],
            "receipts": value["receipts"], "checkpoint": checkpoint["id"]}}

    def activate_closure(self, pack, current):
        """Seat for an io that binds installer absence into the kit closure."""
        hook = getattr(self.io, "activate_closure", None)
        if hook is None:
            return None
        value = read_json(self.path(current))
        return hook(pack, value["fence"], current["transition_checkpoint"], value["inventory"], value["receipts"])

    def validate_transition_evidence(self, pack, current, evidence):
        self.io.validate_compatibility(pack, callback_routes.installed(self.root, pack),
                                       current["transition_checkpoint"]["target"])
        value = read_json(self.path(current))
        checkpoint = current["transition_checkpoint"]
        key = "legacy_fence" if value["engine"] == "legacy" else "v2_fence"
        retirement = evidence.get("retirement_evidence", {})
        if (retirement != {"complete": True, "worker_fenced": True,
                           "inventory": value["inventory"], "receipts": value["receipts"],
                           "checkpoint": checkpoint["id"]}
                or value["inventory"] is None
                or len(value["receipts"]) != len(value["inventory"])
                or evidence.get(key) != value["fence"]):
            raise Refusal(35, "transition lacks exact inventory acknowledgements")
        self.io.verify_writer_fence(pack, value["engine"], value["fence"], checkpoint)
        for target, receipt in zip(value["inventory"], value["receipts"]):
            self.io.verify_retirement(pack, target, receipt, checkpoint)
        if self.io.drain(pack, value["engine"], value["fence"], checkpoint) is not True:
            raise Refusal(35, "transition I/O or admitted dependency remains unresolved")


def framework_version(root):
    """Installed framework version: a source tree carries VERSION; an installed
    instance records its version at install time (no VERSION file there)."""
    root = Path(root)
    version_file = root / "VERSION"
    if version_file.is_file():
        return version_file.read_text().strip()
    version = _recorded_framework_version(root)
    if version:
        return version
    raise Refusal(33, "installed framework version unknown (no VERSION, no recorded instance version)")


def _recorded_framework_version(root):
    """The framework version the installer recorded for this instance, or None."""
    # ESTATE:BEGIN the installed-instance config file and its version key are the estate install layout
    try:
        for line in (root / ".instance.conf").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DOGANY_FW_VERSION" and value.strip():
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None
    # ESTATE:ELSE
    # return None
    # ESTATE:END


def capabilities(root, surface_apis, artifact_codec):
    """M1 supplies its actual features; the kit side supplies pinned artifact encoding."""
    apis = dict(callback_routes.APIS)
    for key in ("live_surface_api", "ledger_api"):
        apis[key] = surface_apis.get(key, [])
    apis["features"] = surface_apis.get("features", [])
    root = Path(root)
    apis["framework"] = framework_version(root)
    manifests = {
        "framework": ["bridge/live_surface.py", "bridge/due_events.py",
                      "bridge/callback_routes.py", "routines/push.sh"],
    }
    # Never substitute bridge JSON encoding for the pinned kit artifact codec.
    apis["artifacts"] = artifact_codec.artifacts(root, manifests) if artifact_codec else {}
    if artifact_codec:
        # The instance carries no installer; its digest is the installer seat's measurement.
        apis["artifacts"]["installer"] = recorded_installer_digest(root)
    return apis


class OriginalCommandRecovery:
    """Process dependency boundary; a missing kit authority never becomes replay.

    The host supplies a trusted object, never an argv response or receipt. Its
    command_lookup validates the pinned command codec and caller credential.
    recover_original owns the kit's atomic exact-command retry/receipt import.
    """
    def __init__(self, root, authority=None):
        self.root, self.authority = Path(root), authority

    def method(self, name):
        if self.authority is None:
            raise Refusal(17, "original-command authority unavailable")
        method = getattr(self.authority, name, None)
        if not callable(method):
            raise Refusal(17, "original-command authority unavailable: " + name)
        return method

    def checked(self, record):
        validate_binding = self.method("validate_binding")
        envelope = callback_routes.authenticated(self.root, record["envelope"])
        path = callback_routes.ingress_path(self.root, envelope["chat_id"], envelope["envelope_id"])
        if read_json(path) != record:
            raise Refusal(17, "original ingress snapshot changed")
        route = callback_routes.installed(self.root, record["pack"])
        require_active(self.root, record["pack"], record["route_digest"], record["generation"])
        if (route != record["route"] or digest(envelope) != record["envelope_digest"]
                or digest(record["config"]) != record["config_digest"]
                or digest(record["env"]) != record["env_digest"]
                or record["env"] != callback_routes.child_overrides(self.root, route)
                or record["principal"] not in record["config"]["identities"]
                or (record["principal"]["chat_id"], record["principal"]["user_id"]) !=
                   (envelope["chat_id"], envelope["user_id"])):
            raise Refusal(17, "original-command ingress authority changed")
        if record["command_binding"] is None:
            raise Refusal(24, "original-command binding unavailable; lookup required")
        validate_binding(record["command_binding"], record)
        return path

    def authorize_ingress_recovery(self, record):
        self.checked(record)
        return True

    def lookup(self, record):
        self.checked(record)
        result = self.method("command_lookup")(record)
        binding = record["command_binding"]
        if (not isinstance(result, dict) or result.get("state") not in ("bound", "committed", "rejected")
                or result.get("command_digest") != binding["command_digest"]
                or result.get("command_bytes") != binding["command_bytes"]
                or result.get("binding_id") != binding.get("binding_id")
                or result.get("S") != binding.get("S") or result.get("P") != binding.get("P")
                or not isinstance(result.get("S"), dict) or not isinstance(result.get("P"), dict)
                or type(result.get("invocation_started")) is not bool
                or not integer(result.get("invocation_generation"), 0)):
            raise Refusal(17, "original-command lookup binding mismatch")
        return result

    def recover_original(self, record, path):
        if Path(path) != self.checked(record):
            raise Refusal(17, "original-command path changed")
        original = self.lookup(record)
        # The kit authority must distinguish unknown/in-flight from aborted
        # using its transaction log. A framework subprocess exit cannot do so.
        return self.method("recover_original")(record, path, original)

    def committed_snapshot(self, record):
        original = self.lookup(record)
        if original["state"] != "committed" or original.get("receipt") is None:
            raise Refusal(24, "committed-snapshot source pending original command")
        source = getattr(self.authority, "committed_snapshot", None)
        if not callable(source):
            raise Refusal(17, "committed-snapshot source unavailable")
        return source(original)

    def publisher_exports(self, record):
        """Project authenticated original ingress for the existing pack publisher.

        Source references and kit digests come from the validated original
        command binding. The framework never resolves a replacement command.
        """
        from bridge import live_surface
        self.checked(record)
        binding, envelope = record["command_binding"], record["envelope"]
        scope, principal = binding.get("S"), binding.get("P")
        source = binding.get("source_ref")
        surface = envelope.get("surface")
        if (not isinstance(scope, dict) or not isinstance(principal, dict) or not isinstance(source, dict)
                or not surface or scope.get("chat_id") != envelope["chat_id"]
                or scope.get("surface_id") != surface["surface_id"] or scope.get("epoch") != surface["epoch"]
                or scope.get("session_id") != binding["session_id"]
                or principal != {"publisher_id": record["pack"], "actor": record["principal"]["actor"],
                                 "member_id": record["principal"]["member_id"]}
                or source.get("ref") != envelope["envelope_id"]):
            raise Refusal(17, "publisher original scope/principal/source binding unavailable")
        value = dict(S=scope, P=principal, envelope_id=envelope["envelope_id"],
                     envelope_digest=record["envelope_digest"], command_digest=binding["command_digest"], source_ref=source)
        if envelope["kind"] == "tap":
            value["callback_data"] = envelope["prefix"] + envelope["payload"]
        coordinator = live_surface.Coordinator(self.root)
        group_export = None
        if envelope["kind"] == "due":
            from bridge.due_events import DueEvents
            groups = [g for g in DueEvents(self.root).groups() if g["op"] == envelope["envelope_id"]]
            if (len(groups) != 1 or groups[0]["envelope"] != envelope
                    or groups[0]["state"] != "firing" or groups[0]["generation"] != record["generation"]
                    or groups[0]["route_digest"] != record["route_digest"]):
                raise Refusal(17, "publisher canonical due group unavailable")
            group = groups[0]
            group_export = coordinator._export(record["pack"], "due_group",
                dict(S=scope, envelope_id=group["op"], envelope_digest=group["binding_digest"], events=envelope["events"]),
                identity=group["op"])
        exported = coordinator._export(record["pack"], "envelope", value, identity=envelope["envelope_id"])
        return exported, group_export


RECOVERY_AUTHORITY = re.compile(r"[a-z][a-z0-9_]{1,40}")


def recovery_authority_path(root, authority_id):
    return data_dir(root) / "recovery-authorities" / (authority_id + ".key")


ACTIVATION_KEYS = ("publisher", "coordinator", "recovery")


def _provision_key(path, others, label):
    """Create one 32-byte 0600 key at path once; keep a valid one; else refuse.

    Never a symlink, never overwritten, never equal to any key in others
    ((label, path) pairs). Returns created (bool).
    """
    folder = path.parent
    if folder.is_symlink() or folder.exists() and not folder.is_dir():
        raise Refusal(17, "%s key directory must be a real directory" % label)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def separate(key):
        for name, other in others:
            try:
                if other != path and other.is_file() and not other.is_symlink() and other.read_bytes() == key:
                    raise Refusal(17, "%s key must differ from the %s key" % (label, name))
            except OSError as exc:
                raise Refusal(17, "%s key unreadable" % name) from exc

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(17, "%s key is not a regular file" % label)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise Refusal(17, "%s key must be mode 0600" % label)
        if info.st_size < 32:
            raise Refusal(17, "%s key too short" % label)
        separate(path.read_bytes())
        return False
    key = secrets.token_bytes(32)
    separate(key)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    return True


def activation_key_paths(root, pack, authority_id):
    """The three keys the kit's activation_authority reads (installed root only)."""
    base = data_dir(root)
    return dict(publisher=base / "publishers" / (pack_name(pack) + ".key"),
                coordinator=base / "coordinator.key",
                recovery=recovery_authority_path(root, authority_id))


def provision_activation_keys(root, pack, authority_id):
    """Installer seat: every key the kit's activation authority reads.

    The kit (session_kit_binding.activation_authority) and live_surface only
    READ live-surface/publishers/<pack>.key, coordinator.key and
    recovery-authorities/<authority_id>.key; nothing at activation may mint
    its own authority. The installer owns the live-surface data dir and the
    pack ledger and runs before any activation, so it creates them here: 32
    bytes from secrets, mode 0600, never a symlink, pairwise distinct and
    distinct from installer.key. An existing valid key is kept; a wrong type,
    mode or size refuses (never overwritten). Returns a list of
    (name, path, created) in ACTIVATION_KEYS order.
    """
    if not isinstance(authority_id, str) or not RECOVERY_AUTHORITY.fullmatch(authority_id):
        raise Refusal(17, "invalid recovery authority id")
    paths = activation_key_paths(root, pack, authority_id)
    labels = dict(publisher="publisher", coordinator="coordinator", recovery="recovery authority")
    others = [(labels[name], paths[name]) for name in ACTIVATION_KEYS]
    others.append(("installer", data_dir(root) / "installer.key"))
    return [(name, paths[name], _provision_key(paths[name], others, labels[name])) for name in ACTIVATION_KEYS]


class InvocationProofProducer:
    """Sign the frozen rev8 proof only inside the kit writer's fenced scope.

    The process-injected authority proves transaction rollback and holds the
    old invocation generation quiescent through export. Neither NOT_FOUND nor
    a dead PID supplies that authority. Kit retry still independently checks
    its transaction log and consumes the generation atomically.
    """
    def __init__(self, root, recovery, authority=None):
        self.root, self.recovery, self.authority = Path(root), recovery, authority

    def produce(self, record, authority_id):
        from bridge import live_surface
        if (self.authority is None or not callable(getattr(self.authority, "fence_invocation", None))
                or not callable(getattr(self.authority, "transaction_aborted", None))):
            raise Refusal(17, "invocation transaction/quiescence authority unavailable")
        if not isinstance(authority_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", authority_id):
            raise Refusal(17, "invalid invocation recovery authority")
        self.recovery.checked(record)
        # The authority's context includes its actual writer fence and domain
        # transaction-log check; it cannot be a caller-provided proof boolean.
        with self.authority.fence_invocation(record):
            original = self.recovery.lookup(record)
            if (original["state"] != "bound" or original["invocation_started"] is not True
                    or not integer(original["invocation_generation"], 1)
                    or self.authority.transaction_aborted(record, original) is not True):
                raise Refusal(24, "invocation rollback or quiescence unproven")
            base = data_dir(self.root)
            path = base / "recovery-authorities" / (authority_id + ".key")
            try:
                if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
                    raise Refusal(17, "invocation recovery key must be private")
                key = path.read_bytes()
                publisher = live_surface.Coordinator(self.root).key(record["pack"])
                if len(key) < 32 or key == publisher:
                    raise Refusal(17, "invocation recovery key must be separate")
            except OSError as exc:
                raise Refusal(17, "invocation recovery key unavailable") from exc
            proof = {k: original[k] for k in ("S", "P", "binding_id", "command_digest", "invocation_generation")}
            proof.update(authority_id=authority_id, outcome="aborted", quiescence=True)
            proof["auth"] = {"mac": live_surface.mac(key, proof)}
            envelope = record["envelope"]
            return live_surface.Coordinator(self.root)._export(record["pack"], "invocation", proof,
                                                               identity=envelope["envelope_id"])


class RequiredTransitionIO:
    """Expose the existing transition seam without inventing pack writer facts."""
    def __init__(self, authority=None):
        self.authority = authority

    def activate_closure(self, *args):
        # Optional seat: an authority without it activates as before.
        hook = getattr(self.authority, "activate_closure", None)
        return hook(*args) if callable(hook) else None

    def __getattr__(self, method):
        if method not in ("validate_compatibility", "fence_writers", "verify_writer_fence", "drain",
                          "inventory", "retire", "verify_retirement"):
            raise AttributeError(method)
        def invoke(*args):
            if self.authority is None or not callable(getattr(self.authority, method, None)):
                raise Refusal(35, "pack-wide transition authority unavailable: " + method)
            return getattr(self.authority, method)(*args)
        return invoke


class BridgeSessionRuntime:
    """Process-owned M2 services, supplied by the integrated M1 coordinator.

    m2_services is a Python dependency seam, not a pack wire operation. It
    returns surface_apis and the pinned artifact_codec. Missing dependencies
    advertise no surface features; an inactive installation remains untouched.
    Never load authority from an environment flag or publisher-supplied JSON.
    """
    def __init__(self, root, services=None):
        self.root = Path(root)
        self.started_at_ms = now_ms()
        self.next_beat_ms = 0
        self.apis = None
        if services is None:
            try:
                from bridge import live_surface
            except ImportError:
                services = {}
            else:
                provider = getattr(live_surface, "m2_services", None)
                try:
                    services = provider(self.root) if provider else {}
                except (Refusal, OSError, ValueError) as exc:
                    logger.warning("Session services unavailable: %s", exc)
                    services = {}
        self.services = services

    def publish(self, due_active=True):
        if not callback_routes.candidates(self.root):
            return
        now = now_ms()
        if due_active and now < self.next_beat_ms:
            return
        if self.apis is None:
            self.apis = capabilities(self.root, self.services.get("surface_apis", {}),
                                     self.services.get("artifact_codec"))
        value = heartbeat(self.root, self.apis, due_active, self.started_at_ms)
        self.next_beat_ms = now + 10000
        return value

    def resume_transitions(self):
        io = self.services.get("transition_io")
        if io is None:
            return
        coordinator = TransitionCoordinator(self.root, io)
        for pack in callback_routes.candidates(self.root):
            current = load_activation(self.root, pack)
            if current["phase"] in ("activating", "deactivating"):
                checkpoint = current["transition_checkpoint"]
                transition(self.root, pack, checkpoint["chat_id"], checkpoint["target"], coordinator)

    def poll(self):
        try:
            self.resume_transitions()
        except (Refusal, OSError, ValueError) as exc:
            logger.warning("Session transition remains unavailable: %s", exc)

    async def beat_loop(self):
        # Handler lookup/transport drain can take longer than ten seconds.
        # Heartbeats have their own lifecycle so busy recovery stays observable.
        while True:
            try:
                self.publish()
            except (Refusal, OSError, ValueError) as exc:
                logger.warning("Session heartbeat remains unavailable: %s", exc)
            await asyncio.sleep(10)

    async def run(self):
        from bridge.due_events import DueEvents
        service = self.services.get("recovery")
        recovery = callback_routes.RecoveryAuthority(self.root, service) if service else None
        driver = DueEvents(self.root, surface=self.services.get("surface"), recovery=recovery)
        beat_task = asyncio.create_task(self.beat_loop())
        try:
            await driver.run(on_poll=self.poll)
        finally:
            beat_task.cancel()
            try:
                await beat_task
            except asyncio.CancelledError:
                pass
            try:
                self.publish(due_active=False)
            except (Refusal, OSError, ValueError) as exc:
                logger.warning("Session heartbeat shutdown unavailable: %s", exc)


def heartbeat(root, apis, due_active, started_at_ms):
    packs = {}
    routes, retired = [], []
    for pack in callback_routes.candidates(root):
        route = callback_routes.installed(root, pack)
        current = load_activation(root, pack)
        if current["phase"] == "active":
            require_active(root, pack, digest(route))
            routes.extend(r["prefix"] for r in route["routes"])
            retired.extend(route.get("retire_prefixes", []))
            packs[pack] = current
    value = {"pid": os.getpid(), "started_at_ms": started_at_ms, "beat_ms": now_ms(),
             "apis": apis, "routes": routes, "retired": retired, "due_active": due_active,
             "activations": packs}
    atomic_json(data_dir(root) / "bridge.json", value)
    return value


def probe_nonce(nonce=None):
    nonce = nonce or secrets.token_hex(16)
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise Refusal(2, "invalid probe nonce")
    return nonce


def probe(root, prefix, need=None, nonce=None, current_capabilities=None):
    nonce = probe_nonce(nonce)
    path = data_dir(root) / "bridge.json"
    if not path.exists():
        raise Refusal(30, "bridge heartbeat missing")
    beat = read_json(path)
    age = now_ms() - beat.get("beat_ms", 0)
    if not integer(beat.get("pid"), 1) or not 0 <= age <= 30000:
        raise Refusal(30, "bridge heartbeat expired")
    try:
        os.kill(beat["pid"], 0)
    except (OSError, OverflowError) as exc:
        raise Refusal(30, "bridge process unavailable") from exc
    apis = beat.get("apis", {})
    if any(not all(n in apis.get(k, []) for n in values)
           for k, values in callback_routes.APIS.items()) or any(
               f not in apis.get("features", []) for f in
               ("edit_acknowledgement", "delivery_receipt_reconciliation")):
        raise Refusal(31, "running bridge API or feature unavailable")
    if prefix not in beat.get("routes", []):
        raise Refusal(32, "prefix not loaded by running bridge")
    pack = callback_routes.locate(root, prefix)
    if pack is None:
        raise Refusal(32, "installed prefix missing")
    if not (Path(root) / "config/packs" / (pack + ".callback_routes.json")).exists():
        raise Refusal(32, "installed route projection missing")
    route = callback_routes.installed(root, pack)
    state = require_active(root, pack, digest(route))
    if (beat.get("activations", {}).get(pack) != state
            or any(p not in beat.get("retired", []) for p in route.get("retire_prefixes", []))):
        raise Refusal(35, "running activation or legacy retirement mismatch")
    # Current files must be independently measured, never echo stale receipt hashes.
    if current_capabilities is None:
        raise Refusal(33, "pinned artifact codec unavailable")
    measured = current_capabilities()
    rows = callback_routes.receipt(root, pack)
    if (measured.get("artifacts") != apis.get("artifacts")
            or measured.get("framework") != rows.get("framework_version")
            or any(measured["artifacts"].get(k) != rows.get(r) for k, r in
                   (("framework", "framework_artifacts"), ("installer", "installer")))):
        raise Refusal(33, "installed artifact attestation mismatch")
    env = callback_routes.sign(root, {"v": 1, "kind": "probe", "envelope_id": "probe:" + nonce,
                                     "chat_id": state["transition_checkpoint"]["chat_id"],
                                     "user_id": state["transition_checkpoint"]["chat_id"],
                                     "issued_at_ms": now_ms(), "nonce": nonce})
    try:
        result = subprocess.run(callback_routes.executable(root, route["probe_argv"]),
                                cwd=root, env=callback_routes.child_env(root, route),
                                input=canonical(env) + b"\n", stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=8, check=False)
        if result.returncode != 0 or json.loads(result.stdout) != {"probe": nonce}:
            raise Refusal(33, "installed route probe failed")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refusal(33, "installed route probe failed") from exc
    if need == "due" and beat.get("due_active") is not True:
        raise Refusal(34, "due scheduler unavailable")
    return {"ok": True, "bridge_pid": beat["pid"], "heartbeat_age_ms": age,
            "routes_revoked": True, "nonce": nonce, "checked_at_ms": now_ms(),
            "artifacts": measured["artifacts"], "generation": state["generation"]}


# The kit's frozen CapabilityChallenge fields (session_v2_contract/transport.py)
# and the floor its config-install accepts (schema 78; K1 admission needs 83).
KIT_REQUIRED = {"kit_schema": 78, "frame_gate_api": 1, "scheduler_api": 1,
                "edit_acknowledgement": True, "delivery_receipt_reconciliation": True,
                "epoch_cancellation": True, "silent_countdown": True, "no_model_fallback": True}
KIT_OWNERS = ("pack", "kit", "framework")
# The installer runs from the framework source / channel payload, never from
# the instance, so its digest is measured from the running installer tree.
INSTALLER_FILES = ("scripts/pack/pack_install.sh", "scripts/pack/lib/pack_replace.py")
# Installer-owned record of that measurement, the instance's only installer artifact.
INSTALLER_RECORD = "config/packs/installer.artifact.json"
INSTALLER_RECORD_SCHEMA = "installer_artifact/1"


def installer_record(measured_files):
    """The record of one installer measurement ({relpath: sha256} over INSTALLER_FILES)."""
    files = dict(measured_files)
    return {"schema": INSTALLER_RECORD_SCHEMA, "files": files, "installer": digest(files)}


def checked_installer_record(value):
    if (not isinstance(value, dict) or sorted(value) != ["files", "installer", "schema"]
            or value["schema"] != INSTALLER_RECORD_SCHEMA or not isinstance(value["files"], dict)
            or sorted(value["files"]) != sorted(INSTALLER_FILES)
            or any(not isinstance(v, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", v)
                   for v in value["files"].values())
            or value["installer"] != digest(value["files"])):
        raise Refusal(33, "installer artifact record invalid")
    return value


def write_installer_record(root, record, receipt):
    """Installer seat: persist the digest the capability receipt carried (mode 0600)."""
    checked_installer_record(record)
    if not isinstance(receipt, dict) or record["installer"] != (receipt.get("artifacts") or {}).get("installer"):
        raise Refusal(33, "installer artifact record differs from the capability receipt")
    path = Path(root) / INSTALLER_RECORD
    if path.is_symlink():
        raise Refusal(33, "installer artifact record is a symlink")
    atomic_json(path, record)
    return path


def recorded_installer_digest(root):
    """Runtime: the installer digest recorded at install; missing or tampered refuses."""
    path = Path(root) / INSTALLER_RECORD
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise Refusal(33, "artifact manifest path is unavailable (installer record missing)") from exc
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise Refusal(33, "installer artifact record is not a private regular file")
    return checked_installer_record(read_json(path))["installer"]


def installed_kit_schema(root):
    """PRAGMA user_version of the installed shared kit DB, opened read-only."""
    import sqlite3
    path = (Path(root) / "database/kit_life.db").resolve()
    if not path.is_file():
        raise Refusal(33, "installed kit database missing")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise Refusal(33, "installed kit schema unreadable") from exc


# Kit session_kit_binding CONFIG_RECEIPT_FIELDS: never part of the configuration digest.
CONFIG_RECEIPT_FIELDS = frozenset(("service_key_hex", "config_digest", "challenge", "capability_receipt",
                                   "capability_signature", "legacy_retirement", "installation_closure"))


def bound_config_digest(described, installer_digest):
    """The kit's configuration_digest over the --describe configuration with the
    framework-measured installer digest added (the kit's validate_attestation
    recomputes exactly this; --describe alone cannot know the installer digest).
    Same canonical JSON + sha256 as the kit's session_command.digest."""
    config = json.loads(json.dumps(described))
    digests = config.get("artifact_digests")
    if not isinstance(digests, dict):
        raise Refusal(33, "kit descriptor artifact digests invalid")
    digests["installer"] = installer_digest
    facts = {k: v for k, v in config.items() if k not in CONFIG_RECEIPT_FIELDS}
    if isinstance(facts.get("activation"), dict):
        facts["activation"] = {k: v for k, v in facts["activation"].items() if k != "generation"}
    return digest(facts)


# The kit's additive /2 program handshake (session_program.capability_response):
# both sides carry PROGRAM_SCHEMA, and the challenge's required and the
# receipt's measured kit schema are at least PROGRAM_KIT_SCHEMA. A descriptor
# without required_features keeps the scalar /1 challenge/receipt byte-for-byte.
PROGRAM_SCHEMA = "session_program_capability/1"
PROGRAM_FACTS_SCHEMA = "session_program_facts/2"
PROGRAM_KIT_SCHEMA = 84
PROGRAM_KIT_FILES = ("database/session_program.py", "database/session_kit_binding/__init__.py")
# Run in a child (-I, installed root only): import the installed kit's program
# contract and load the installed pack exactly as the kit's runtime does, then
# report what both actually export. Nothing from the descriptor reaches it.
PROGRAM_MEASURE = r"""
import json, sys
root, pack = sys.argv[1], sys.argv[2]
sys.path.insert(0, root + "/database")
import session_program as kit
from session_kit_binding import load_pack
runtime = load_pack(pack)
print(json.dumps({
    "kit_features": sorted(getattr(kit, "FORMAT_FEATURES", ())),
    "kit_minimum_schema": getattr(kit, "MINIMUM_SCHEMA", None),
    "kit_handshake": callable(getattr(kit, "capability_response", None)),
    "kit_commands": callable(getattr(kit, "command", None)),
    "pack_features": sorted(getattr(runtime, "PROGRAM_FEATURES", ())),
    "commands": getattr(runtime, "PROGRAM_COMMAND_VERSION", None),
    "projection": getattr(runtime, "PROGRAM_PROJECTION_VERSION", None),
    "prepared": callable(getattr(runtime, "build_prepared_program", None))}))
"""


def program_required(described):
    """The descriptor's required /2 program features, or None for a /1 install."""
    if "required_features" not in described and described.get("schema") != PROGRAM_FACTS_SCHEMA:
        return None
    value = described.get("required_features")
    if (described.get("schema") != PROGRAM_FACTS_SCHEMA or not isinstance(value, list) or not value
            or any(not isinstance(v, str) for v in value) or len(frozenset(value)) != len(value)):
        raise Refusal(33, "kit descriptor program features invalid")
    return sorted(value)


def measure_program(root, described, files):
    """The /2 program features the INSTALLED kit and pack actually export.

    Only descriptor-pinned bytes (just re-hashed against the installed root)
    are imported, in an isolated child; the result is the intersection of the
    kit's native format features and the pack's executable PROGRAM_FEATURES,
    and only when the pack implements command/projection version 1.
    """
    kit = frozenset((described["artifact_files"] or {}).get("kit") or ())
    pack_files = frozenset((described["artifact_files"] or {}).get("pack") or ())
    pack = described.get("pack_v2_path")
    if not isinstance(pack, str) or not Path(pack).resolve().is_relative_to(root) or not Path(pack).is_dir():
        raise Refusal(33, "kit descriptor pack_v2_path invalid")
    pack = Path(pack).resolve()
    sources = [p for p in sorted(pack.iterdir()) if p.suffix in (".py", ".json")]
    pinned = [(root / rel, kit) for rel in PROGRAM_KIT_FILES] + [(p, pack_files) for p in sources]
    for path, owned in pinned:
        if str(path) not in owned or str(path) not in files:
            raise Refusal(33, "program measurement input is not pinned by the descriptor: " + str(path))
    try:
        result = subprocess.run([sys.executable, "-I", "-c", PROGRAM_MEASURE, str(root), str(pack)],
                                cwd=root, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refusal(31, "installed program capability unmeasurable") from exc
    if not isinstance(value, dict):
        raise Refusal(31, "installed program capability unmeasurable: "
                      + (result.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1])
    if (value.get("kit_handshake") is not True or value.get("kit_commands") is not True
            or not integer(value.get("kit_minimum_schema"), 1) or value["kit_minimum_schema"] < PROGRAM_KIT_SCHEMA):
        raise Refusal(31, "installed kit lacks the /2 program handshake")
    if value.get("commands") != 1 or value.get("projection") != 1 or value.get("prepared") is not True:
        raise Refusal(31, "installed pack lacks /2 program commands or projection")
    features = sorted(frozenset(value.get("kit_features") or ()) & frozenset(value.get("pack_features") or ()))
    return {"schema": PROGRAM_SCHEMA, "features": features, "commands": 1, "projection": 1}


def measure_installed_files(root, described, since):
    """Re-hash every descriptor-pinned file under the installed root.

    Returns ({path: sha256}, {owner: artifact digest}) for KIT_OWNERS; a file
    whose bytes differ from its pinned digest refuses (changed since `since`).
    """
    files = {}
    for row in described["files"] if isinstance(described["files"], list) else [None]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise Refusal(33, "kit descriptor file row invalid")
        path = Path(row["path"])
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise Refusal(33, "kit artifact outside the installed root or missing: " + row["path"])
        files[row["path"]] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if files[row["path"]] != row.get("digest"):
            raise Refusal(33, "installed artifact changed since " + since + ": " + row["path"])
    artifacts = {}
    for owner in KIT_OWNERS:
        paths = (described["artifact_files"] or {}).get(owner) if isinstance(described["artifact_files"], dict) else None
        if not isinstance(paths, list) or not paths or any(p not in files for p in paths):
            raise Refusal(33, owner + " artifacts are not pinned by the descriptor")
        artifacts[owner] = digest({p: files[p] for p in sorted(paths)})
    return files, artifacts


def measure_capabilities(root, apis, required):
    """The kit CapabilityChallenge fields measured from the framework's own
    advertisement and the installed DB schema; any field below `required` refuses."""
    features = apis.get("features", [])
    frame_gate = max(apis.get("live_surface_api") or [0])
    scheduler = max(apis.get("due_event_api") or [0])
    measured = {"kit_schema": installed_kit_schema(root), "frame_gate_api": frame_gate, "scheduler_api": scheduler,
                "edit_acknowledgement": "edit_acknowledgement" in features,
                "delivery_receipt_reconciliation": "delivery_receipt_reconciliation" in features,
                # DueEvents.cancel(surface, epoch) / cancel_undispatched(generation)
                "epoch_cancellation": scheduler >= 1,
                # live_card frames are sent silent; model_final "drop" never falls back
                "silent_countdown": frame_gate >= 1, "no_model_fallback": frame_gate >= 1}
    for name, floor in required.items():
        if name not in measured:
            raise Refusal(31, "framework cannot measure kit capability " + name)
        short = measured[name] is not True if floor is True else measured[name] < floor
        if short:
            raise Refusal(31, "framework lacks kit capability " + name)
    return measured


def install_probe(root, described, nonce=None, current_capabilities=None, clock=now_ms, installer_files=None):
    """Native capability proof for the installer seat's kit config-install.

    probe() needs an ACTIVE pack whose route the running bridge has loaded.
    config-install stage 1 runs before activation (it bootstraps the kit
    authority fence_writers needs), so this seat measures the installed
    root instead: every --describe artifact digest is recomputed from the
    installed bytes, the framework's own API/feature advertisement and the
    installed DB schema supply the capabilities, and the receipt pins the
    nonce, installed root, configuration digest and the four artifact
    digests. The installer digest is never read from the descriptor: it is
    measured over installer_files ({relpath: absolute path}, the RUNNING
    installer tree) and a descriptor installer entry is ignored. Returns
    {"challenge", "capability_receipt", "installer_record"} (the record the
    installer seat persists via write_installer_record); refuses otherwise.
    A /2 program descriptor (required_features) adds the program handshake:
    the challenge carries the required features at kit schema 84, the receipt
    the features measured from the installed kit and pack (measure_program).
    """
    nonce = probe_nonce(nonce)
    root = Path(root).resolve()
    if not isinstance(described, dict) or not {"installed_root", "config_digest", "files", "artifact_files",
                                                "artifact_digests", "capability_max_age_ms"} <= described.keys():
        raise Refusal(33, "kit configuration descriptor incomplete")
    if (not isinstance(described["installed_root"], str)
            or Path(described["installed_root"]).resolve() != root):
        raise Refusal(33, "kit descriptor names a different installed root")
    if (not isinstance(described["config_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", described["config_digest"])
            or not integer(described["capability_max_age_ms"], 1)):
        raise Refusal(33, "kit descriptor digest or capability age invalid")
    files, artifacts = measure_installed_files(root, described, "describe")
    declared = described["artifact_digests"]
    if not isinstance(declared, dict) or {k: v for k, v in declared.items() if k != "installer"} != artifacts:
        raise Refusal(33, "kit artifact digests differ from installed bytes")
    if "installer" in declared or "installer" in (described["artifact_files"] or {}):
        logger.warning("install_probe: descriptor installer entry ignored; using measured installer digest")
    if not isinstance(installer_files, dict) or sorted(installer_files) != sorted(INSTALLER_FILES):
        raise Refusal(33, "installer files not supplied")
    measured_files = {}
    for rel, location in sorted(installer_files.items()):
        path = Path(location)
        if not path.is_file():
            raise Refusal(33, "running installer file missing: " + rel)
        measured_files[rel] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    record = installer_record(measured_files)
    artifacts["installer"] = record["installer"]
    if current_capabilities is None:
        raise Refusal(33, "framework capability measurement unavailable")
    measured = measure_capabilities(root, current_capabilities(), KIT_REQUIRED)
    required, program = dict(KIT_REQUIRED), None
    wanted = program_required(described)
    if wanted is not None:
        if measured["kit_schema"] < PROGRAM_KIT_SCHEMA:
            raise Refusal(31, "framework lacks kit capability kit_schema (program execution requires schema 84)")
        program = measure_program(root, described, files)
        missing = [f for f in wanted if f not in program["features"]]
        if missing:
            raise Refusal(31, "installed kit/pack lacks program features " + ",".join(missing))
        required["kit_schema"] = PROGRAM_KIT_SCHEMA
    challenge = {"schema": 2, "kind": "session_flow_v2.challenge", "nonce": nonce,
                 "no_mutation": True, "required": required}
    receipt = {"schema": 2, "kind": "session_flow_v2.receipt", "nonce": nonce, "receipt_id": "install:" + nonce,
               "handler": "native_callback", "no_mutation": True, "capabilities": measured,
               "config_digest": bound_config_digest(described, artifacts["installer"]),
               "installed_root": described["installed_root"],
               "artifacts": artifacts, "checked_at": clock()}
    if program is not None:
        challenge["program"] = dict(program, features=wanted)
        receipt["program"] = program
    return {"challenge": challenge, "capability_receipt": receipt, "installer_record": record}


# DGN-2085 O1: the install receipt stays install proof; every session open
# carries a fresh runtime receipt the kit age-checks instead. The privileged
# framework seat writes it beside the kit's private configuration, signed with
# the same service key as the install attestation; the install config is
# never rewritten.
KIT_CONFIG = "config/kit_life/session-v2.json"
RUNTIME_RECEIPT = "config/kit_life/session-v2.runtime-receipt.json"
RUNTIME_RECEIPT_SCHEMA = "session_runtime_receipt/1"
SESSION_OPEN = "session_open"
KIT_ATTESTATION_FIELDS = ("challenge", "capability_receipt", "legacy_retirement", "installation_closure")


def installed_kit_config(root, owner):
    """The kit's private session configuration when `owner` is its pack, else None."""
    path = Path(root) / KIT_CONFIG
    if not path.exists() and not path.is_symlink():
        return None
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise Refusal(33, "installed kit configuration unreadable") from exc
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
        raise Refusal(33, "installed kit configuration is not a private regular file")
    config = read_json(path)
    if not isinstance(config, dict):
        raise Refusal(33, "installed kit configuration invalid")
    packs = ((config.get("activation") or {}).get("pack"), (config.get("delivery") or {}).get("publisher_id"))
    return config if owner in packs else None


def session_open_probe(root, config, nonce=None, current_capabilities=None, clock=now_ms):
    """Runtime capability receipt for one session open.

    The same measurement as install_probe, taken now against the INSTALLED
    configuration: every pinned file re-hashed, the per-owner artifact and
    configuration digests recomputed (installer from the installer seat's
    record), the framework capabilities and installed DB schema re-measured
    and, for a /2 program install, the kit/pack program features re-measured.
    Anything below what the install challenge required refuses: a kit or pack
    that changed underneath never inherits the install receipt. The result has
    the install receipt's schema plus purpose="session_open" and the install
    receipt id; nonce and checked_at are fresh. The install receipt is only read.
    """
    nonce = probe_nonce(nonce)
    root = Path(root).resolve()
    if not isinstance(config, dict) or not {"installed_root", "config_digest", "files", "artifact_files",
                                            "artifact_digests", "challenge", "capability_receipt"} <= config.keys():
        raise Refusal(33, "installed kit configuration incomplete")
    install, challenge = config["capability_receipt"], config["challenge"]
    if (not isinstance(install, dict) or not isinstance(challenge, dict)
            or not isinstance(install.get("receipt_id"), str) or not isinstance(challenge.get("required"), dict)):
        raise Refusal(33, "installed kit configuration carries no install receipt")
    if (not isinstance(config["installed_root"], str) or Path(config["installed_root"]).resolve() != root
            or install.get("installed_root") != config["installed_root"]):
        raise Refusal(33, "installed kit configuration names a different installed root")
    files, artifacts = measure_installed_files(root, config, "install")
    artifacts["installer"] = recorded_installer_digest(root)
    if artifacts != config["artifact_digests"] or artifacts != install.get("artifacts"):
        raise Refusal(33, "installed kit, pack or framework artifacts differ from the install")
    bound = bound_config_digest(config, artifacts["installer"])
    if bound != config["config_digest"] or bound != install.get("config_digest"):
        raise Refusal(33, "installed kit configuration digest differs from the install")
    if current_capabilities is None:
        raise Refusal(33, "framework capability measurement unavailable")
    measured = measure_capabilities(root, current_capabilities(), challenge["required"])
    program = None
    if program_required(config) is not None or "program" in challenge:
        wanted = (challenge.get("program") or {}).get("features")
        if not isinstance(wanted, list):
            raise Refusal(33, "installed kit challenge program features invalid")
        program = measure_program(root, config, files)
        missing = [f for f in wanted if f not in program["features"]]
        if missing:
            raise Refusal(31, "installed kit/pack lacks program features " + ",".join(missing))
    receipt = {"schema": 2, "kind": "session_flow_v2.receipt", "nonce": nonce, "receipt_id": SESSION_OPEN + ":" + nonce,
               "handler": "native_callback", "no_mutation": True, "capabilities": measured,
               "config_digest": bound, "installed_root": config["installed_root"],
               "artifacts": artifacts, "checked_at": clock(),
               "purpose": SESSION_OPEN, "install_receipt_id": install["receipt_id"]}
    if program is not None:
        receipt["program"] = program
    return receipt


def publish_session_open_receipt(root, owner, current_capabilities=None, clock=now_ms, nonce=None):
    """Session-open seat: measure and hand the kit a fresh runtime receipt.

    Returns the receipt, or None when `owner` has no installed kit session
    configuration. A configuration whose install attestation no longer
    verifies, or a measurement that refuses, refuses the open; the previous
    runtime receipt is removed first so it can never be reused.
    """
    config = installed_kit_config(root, owner)
    if config is None:
        return None
    target = Path(root) / RUNTIME_RECEIPT
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise Refusal(40, "stale runtime receipt could not be removed") from exc
    try:
        key = bytes.fromhex(config["service_key_hex"])
        attested = {k: config[k] for k in KIT_ATTESTATION_FIELDS}
        signature = config["capability_signature"]
    except (KeyError, TypeError, ValueError) as exc:
        raise Refusal(33, "installed kit attestation incomplete") from exc
    if len(key) < 32 or not isinstance(signature, str) or not hmac.compare_digest(
            hmac.new(key, canonical(attested), hashlib.sha256).hexdigest(), signature):
        raise Refusal(33, "installed kit attestation invalid")
    receipt = session_open_probe(root, config, nonce=nonce, current_capabilities=current_capabilities, clock=clock)
    atomic_json(target, {"schema": RUNTIME_RECEIPT_SCHEMA, "capability_receipt": receipt,
                         "capability_signature": hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()})
    return receipt
