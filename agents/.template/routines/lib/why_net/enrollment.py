"""Local installation enrollment, shared by an owner's instances on this host.

Only the explicit update command bootstraps a local authority. Runtime readers
never elect one when a descriptor or remote endpoint is unavailable.
"""
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from . import authority, runtime
from .intake import digest


def publish(path, value):
    if not path.exists() or json.loads(path.read_text()) != value:
        authority.atomic(path, value)


def enroll(root):
    root = Path(root).resolve()
    if not runtime.enabled(root):
        return dict(status="skipped", reason="why-net-off")
    owner = runtime.enrolment(root)["owner_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner):
        raise ValueError("identity-unavailable")
    client_path = root / "config/why-authority.json"
    migration_path = root / "config/why-migration.json"
    if client_path.exists():
        client = json.loads(client_path.read_text())
        if client.get("owner_key") != owner:
            raise ValueError("enrollment-owner-mismatch")
        # An explicit deployment, including SSH, is never replaced by local
        # bootstrap. Incomplete hand-configured deployments remain held.
        if not client.get("installation_enrollment"):
            if not migration_path.exists():
                raise ValueError("migration-enrollment-required")
            return dict(status="enrolled", owner_id=owner)
    home = Path.home() / ".dogany/why-authority"
    if client_path.exists() and client.get("engine_descriptor") != str(home / "owners" / owner / "engine.json"):
        raise ValueError("enrollment-installation-mismatch")
    with authority.locked(home):
        host_file = home / "host.json"
        host = json.loads(host_file.read_text()) if host_file.exists() else dict(uuid=str(uuid.uuid4()))
        publish(host_file, host)
        host_id = home / "host-id"
        # Authority's host pin is a plain-text identifier, not a credential.
        if not host_id.exists():
            fd = os.open(host_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(host["uuid"] + "\n")
        if host_id.read_text().strip() != host["uuid"]:
            raise ValueError("enrollment-host-mismatch")
        identity_file = root / "config/why-instance.json"
        identity = json.loads(identity_file.read_text()) if identity_file.exists() else dict(
            instance_uuid=str(uuid.uuid4()), owner_id=owner)
        if identity.get("owner_id") != owner:
            raise ValueError("enrollment-owner-mismatch")
        publish(identity_file, identity)
        principal = "instance-" + identity["instance_uuid"]
        # A versioned private engine survives removal/re-mint of any one
        # instance. Publish dependencies before the descriptor points at it.
        source = Path(__file__).resolve().parent
        files = {"lib/why_net/" + name: (source / name).read_bytes() for name in (
            "__init__.py", "authority.py", "intake.py", "evidence.py", "tree.py",
            "nodes.py", "migration.py", "store.py")}
        files["why-authority.py"] = (source.parents[1] / "why-authority.py").read_bytes()
        version = digest({name: data.hex() for name, data in files.items()})
        engine = home / "engines" / version
        for name, data in files.items():
            target = engine / name
            if target.exists() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
        owner_dir = home / "owners" / owner
        descriptor_path = owner_dir / "engine.json"
        descriptor = json.loads(descriptor_path.read_text()) if descriptor_path.exists() else dict(
            protocol_version=authority.PROTOCOL, authority_id=authority.AUTHORITY,
            owner_key=owner, authority_host_identity=host["uuid"],
            host_identity_file=str(host_id), data_root=str(home / "owners"),
            principals={}, proof_providers={})
        if (descriptor.get("owner_key") != owner
                or descriptor.get("authority_host_identity") != host["uuid"]):
            raise ValueError("enrollment-descriptor-mismatch")
        issuer = "legacy-" + principal
        key_file = owner_dir / (issuer + ".key")
        key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not key_file.exists():
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(secrets.token_bytes(32))
        os.chmod(key_file, 0o600)
        if len(key_file.read_bytes()) != 32:
            raise ValueError("enrollment-key-invalid")
        descriptor["principals"][principal] = dict(agent_id=principal,
            actions=["read_snapshot", "import_legacy_tree"])
        descriptor["proof_providers"][issuer] = dict(key_file=str(key_file), kinds=["legacy-tree-import"])
        publish(descriptor_path, descriptor)
        client = dict(protocol_version=authority.PROTOCOL, authority_id=authority.AUTHORITY,
            owner_key=owner, principal_id=principal, transport="local",
            authority_host_identity=host["uuid"], host_identity_file=str(host_id),
            remote_engine_absolute_path=str(engine / "why-authority.py"),
            engine_descriptor=str(descriptor_path), installation_enrollment=1)
        publish(migration_path, dict(source=dict(host_uuid=host["uuid"],
            instance_uuid=identity["instance_uuid"], log_kind="events.jsonl"),
            issuer=issuer, key_file=str(key_file)))
        publish(client_path, client)
    request = dict(protocol=authority.PROTOCOL, authority_id=authority.AUTHORITY,
        owner_key=owner, principal=principal, operation="read_snapshot", payload={})
    if authority.transact(request, client_path).get("status") != "snapshot":
        raise ValueError("enrollment-authority-unavailable")
    return dict(status="enrolled", owner_id=owner, principal_id=principal)
