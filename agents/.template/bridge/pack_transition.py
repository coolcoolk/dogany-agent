"""Default pack-wide transition authority for a REPLACEMENT install.

TransitionCoordinator (session_activation) drives the B4 legacy->v2 sequence
through an injected io object. For a pack whose install replaced its previous
engine (pack_replace.py: absence attestation present), the bridge host binds
this adapter by default, so activation no longer refuses with "pack-wide
transition authority unavailable" (rc 35):

  validate_compatibility  forward only (no rollback, dec-460) and the
                          installer's absence attestation still verifies
  fence_writers / verify_writer_fence / import+verify retirement / activate
                          the installed kit's trusted Authority seats
  drain                   no admitted retired-command invocation is running
                          and no retired notice is still in transport
  inventory               the exact legacy keyboards (attestation + rescan)
  retire                  live_surface TP maintenance, A imported by the kit
  activate_closure        trainer_installation_closure/1 signed with the
                          authority key, the kit activation and the private
                          kit configuration's attestation re-signed

Every seat refuses (Refusal 35) rather than infer authority it cannot verify.
"""
import asyncio
import concurrent.futures
import copy
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sys
import time

from bridge import pack_revocation
from bridge.session_state import Refusal

CLOSURE_SCHEMA = "trainer_installation_closure/1"
FACTS = ("drain", "deferred", "callbacks", "healthkit")


def replaced_packs(root):
    return sorted(p.name[:-len(".replacement-attestation.json")]
                  for p in (Path(root) / "config/packs").glob("*.replacement-attestation.json"))


def installed_kit_authority(root):
    """The installed kit's Authority, from its own pinned private config."""
    database = Path(root).resolve() / "database"
    if not (database / "session_kit_binding/__init__.py").is_file():
        raise Refusal(35, "installed kit authority unavailable")
    if str(database) not in sys.path:
        sys.path.insert(0, str(database))
    binding = importlib.import_module("session_kit_binding")
    try:
        config, service_key = binding.installed_configuration()
        return binding.activation_authority(config), config, service_key
    except Exception as error:  # the kit raises its own CommandError
        raise Refusal(35, "installed kit authority refused: " + str(error)) from error


class PackTransitionIO:
    def __init__(self, root, kit=None, coordinator=None, clock=None, config_path=None):
        self.root = Path(root).resolve()
        self._kit = kit
        self._coordinator = coordinator
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.config_path = config_path or self.root / "config/kit_life/session-v2.json"

    # -- bindings -----------------------------------------------------------
    def kit(self):
        if self._kit is None:
            self._kit = installed_kit_authority(self.root)
        return self._kit

    def authority(self):
        return self.kit()[0]

    def coordinator(self):
        if self._coordinator is None:
            from bridge import live_surface
            self._coordinator = live_surface.Coordinator(self.root, transport=live_surface.cli_transport(self.root))
        return self._coordinator

    def attestation(self, pack):
        try:
            return pack_revocation.verify_attestation(self.root, pack)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Refusal(35, "replacement absence attestation refused: " + str(error)) from error

    def call(self, name, *args):
        try:
            return getattr(self.authority(), name)(*args)
        except Refusal:
            raise
        except Exception as error:
            raise Refusal(35, "kit %s refused: %s" % (name, error)) from error

    # -- TransitionCoordinator seats ----------------------------------------
    def validate_compatibility(self, pack, route, target):
        if target != "v2":
            raise Refusal(35, "replacement installs have no rollback; fix forward (dec-460)")
        self.attestation(pack)
        if not isinstance(route, dict) or not route.get("retire_prefixes"):
            raise Refusal(35, "installed v2 route declares no retired prefixes")
        return True

    def fence_writers(self, pack, engine, generation, checkpoint):
        self.attestation(pack)
        return self.call("fence_writers", pack, engine, generation, checkpoint)

    def verify_writer_fence(self, pack, engine, fence, checkpoint):
        return self.call("verify_writer_fence", pack, engine, fence, checkpoint)

    def drain(self, pack, engine, fence, checkpoint):
        if pack_revocation.admitted(self.root, pack):
            return False
        base = self.root / ".telegram_bot/live-surface"
        for path in sorted(base.glob("*.transport.json")) if base.is_dir() else []:
            control = json.loads(path.read_text(encoding="utf-8"))
            for notice in control.get("notices", {}).values():
                if notice.get("status") == "dispatching" and \
                        pack_revocation.notice_owner(self.root, notice.get("intended")) == pack:
                    return False
        return True

    def inventory(self, pack, engine, checkpoint):
        value = self.attestation(pack)
        inventory = json.loads((self.root / value["replacement_manifest"]).read_text(encoding="utf-8"))
        directories = [r["path"] for r in inventory["resources"] if r.get("kind") == "surface_registry"]
        try:
            rescanned = pack_revocation.legacy_targets(self.root, directories)
        except ValueError as error:
            raise Refusal(35, str(error)) from error
        found = {(t["chat_id"], t["message_id"]): t for t in value["inventory"] + rescanned}
        return [dict(found[k]) for k in sorted(found)]

    def _run(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        # Called from the bridge's poll inside its event loop: drive the
        # retirement on a private loop rather than nest asyncio.run.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()

    def retire(self, pack, target, fence, checkpoint):
        from bridge import live_surface
        try:
            # TransitionCoordinator runs under the checkpoint chat's barrier.
            ack = self._run(self.coordinator().transition_retire(pack, fence, checkpoint, target,
                                                                 held_chat=checkpoint["chat_id"],
                                                                 absent_record=self.absent_record))
        except live_surface.Refusal as error:
            raise Refusal(35, "transition retirement refused: " + str(error)) from error
        if ack is None:
            return None
        return self.call("import_retirement", pack, target, ack, fence, checkpoint)

    def absent_record(self, receipt, evidence, tp):
        """Authority seat: authenticated recovery record for a gone target."""
        authority = self.authority()
        kit = sys.modules.get(type(authority).__module__)
        sign = getattr(kit, "authenticated", None)
        if not callable(sign):
            raise Refusal(35, "kit authenticated helper unavailable")
        record = dict(authority_id=authority.authority_id, K=receipt["K"], seq=receipt["seq"],
                      plan_digest=receipt["plan_digest"], payload_digest=receipt["payload_digest"],
                      target=receipt["target"], resolution="absent", message_id=None,
                      evidence_digest=receipt["evidence_digest"], quiescence=True,
                      issued_at_ms=min(self.clock(), authority.clock()))
        try:
            return sign(authority.key, record)
        except Exception as error:
            raise Refusal(35, "kit authenticated refused: " + str(error)) from error

    def verify_retirement(self, pack, target, receipt, checkpoint):
        return self.call("verify_retirement", pack, target, receipt, checkpoint)

    def activate_closure(self, pack, fence, checkpoint, inventory, receipts):
        """Bind installer absence + retirements into the kit's closure."""
        authority = self.authority()
        value = self.attestation(pack)
        closure = dict(schema=CLOSURE_SCHEMA, authority_id=authority.authority_id,
                       **{k: fence[k] for k in ("pack", "engine", "generation", "checkpoint_digest", "token")},
                       installed_root=authority.installed_root, config_digest=authority.config_digest,
                       capability_receipt_id=authority.capability_receipt_id,
                       previous_version=value["previous_version"] or "unknown",
                       replacement_version=value["replacement_version"] or "unknown",
                       previous_manifest=list(value["previous_manifest"]), manifest_digest=value["manifest_digest"],
                       remaining_paths=[], checked_at=min(self.clock(), authority.clock()),
                       inventory=[dict(t) for t in inventory], retirements=[dict(r) for r in receipts],
                       **{name: dict(complete=True, evidence_digest=value["facts"][name]["evidence_digest"])
                          for name in FACTS})
        closure["auth"] = dict(mac=pack_revocation.mac(authority.key, closure))
        self.call("activate", pack, "v2", fence["generation"], fence, checkpoint, closure)
        self.publish_closure(closure, fence, inventory, receipts)
        return closure

    def publish_closure(self, closure, fence, inventory, receipts):
        """Privileged seat: the kit's private config carries the closure."""
        kit = self.kit()
        config, service_key = kit[1], kit[2]
        if config is None:
            return
        value = copy.deepcopy(config)
        value["installation_closure"] = closure
        value["activation"] = dict(value["activation"], generation=fence["generation"])
        value["legacy_retirement"] = dict(
            capability_receipt_id=value["capability_receipt"]["receipt_id"], routes_revoked=True,
            workers_fenced=True, surfaces=[dict(t, markup_state="clear", acknowledged=True, ack_digest=r["ack_digest"])
                                           for t, r in zip(inventory, receipts)])
        attested = {k: value[k] for k in ("challenge", "capability_receipt", "legacy_retirement", "installation_closure")}
        value["capability_signature"] = hmac.new(service_key, pack_revocation.canonical(attested),
                                                 hashlib.sha256).hexdigest()
        path = Path(self.config_path)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        self._kit = (kit[0], value, service_key)


def default_transition_io(root):
    """Bound only where an install replaced a pack's previous engine."""
    return PackTransitionIO(root) if replaced_packs(root) else None
