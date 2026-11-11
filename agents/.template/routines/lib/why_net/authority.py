"""FDGN-594 single-writer, revision-CAS tree authority.

A deployment descriptor is mandatory; an offline client never elects itself.
The same engine serves local and SSH stdio. Import proofs are signed by an
enrolled runtime provider; deeper deltas consume an exact displayed capability.
Legacy local event stores are NOT silently imported as canonical authority.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .intake import digest
from .tree import agent_tree_view, owner_backed_reusable
from .evidence import EvidenceResolver

PROTOCOL = 1
AUTHORITY = "dogany-mac-mini-primary-v1"
MUTATIONS = {"import_proof", "register_work", "append_work_link", "apply_node_delta",
             "register_child", "import_legacy_tree"}


def atomic(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".txn-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, sort_keys=True, ensure_ascii=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def locked(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(root / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        end = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("authority-busy")
                time.sleep(0.025)
        yield
    finally:
        os.close(fd)


class Authority:
    def __init__(self, descriptor, principal):
        self.descriptor = descriptor
        self.principal = principal
        if (descriptor.get("protocol_version") != PROTOCOL
                or descriptor.get("authority_id") != AUTHORITY
                or descriptor.get("authority_host_identity") != Path(descriptor.get(
                    "host_identity_file", "/etc/dogany/authority-host-id")).read_text().strip()
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", descriptor.get("owner_key", ""))):
            raise ValueError("authority-unavailable")
        self.owner = descriptor["owner_key"]
        # Only the enrolled authority host may open this path for writes.
        base = Path(descriptor.get("data_root") or Path.home() / ".dogany/owners")
        self.root = base / self.owner / "why-net"
        self.permission = descriptor.get("principals", {}).get(principal)
        if not self.permission:
            raise ValueError("principal-unavailable")

    def _prepare_proof(self, payload):
        bundle = payload["bundle"]
        provider = self.descriptor["proof_providers"][bundle["issuer"]]
        key = Path(provider["key_file"]).read_bytes()
        signature = hmac.new(key, digest(bundle["fact"]).encode(), hashlib.sha256).hexdigest()
        fact = bundle["fact"]
        if (not hmac.compare_digest(signature, bundle["signature"])
                or fact.get("owner_id") != self.owner
                or fact.get("kind") not in provider["kinds"]):
            raise ValueError("invalid-proof")
        return copy.deepcopy(fact)

    def _proof(self, state, ref, kind):
        fact = state["proofs"].get(ref, {})
        if fact.get("owner_id") != self.owner or fact.get("kind") != kind:
            raise ValueError("invalid-proof")
        return fact

    def _apply(self, state, op, p, prepared):
        if op == "import_legacy_tree":
            if prepared.get("kind") != "legacy-tree-import":
                raise ValueError("migration-proof-kind")
            from .migration import apply
            result = apply(state, prepared, self.owner, self.permission["agent_id"])
            self._validate_graph(state["nodes"])
            return result
        if op == "import_proof":
            ref = p["ref"]
            if ref in state["proofs"] and state["proofs"][ref] != prepared:
                raise ValueError("proof-ref-reused")
            state["proofs"][ref] = prepared
            return {"proof_ref": ref}
        if op == "register_work":
            work = p["work"]
            if work.get("owner_id") != self.owner or work.get("agent_id") != self.permission["agent_id"]:
                raise ValueError("work-owner-mismatch")
            key = work["work_key"]
            if key in state["works"] and state["works"][key] != work:
                raise ValueError("work-alias-conflict")
            # Persist the owning adapter once; adding a kit cannot fork records.
            if not work.get("record_uri") or not work.get("scope_digest"):
                raise ValueError("work-record-unavailable")
            state["works"][key] = work
            return {"work_key": key}
        if op == "append_work_link":
            link = copy.deepcopy(p["link"])
            work = state["works"][link["work_key"]]
            node = state["nodes"][link["node_id"]]
            if (work["agent_id"] != self.permission["agent_id"]
                    or link.get("owner_id") != self.owner
                    or link.get("agent_id") != work["agent_id"]
                    or link.get("scope_digest") != work["scope_digest"]
                    or link.get("node_revision") != node["revision"]
                    or not link.get("relation")):
                raise ValueError("link-unbound")
            resolver = EvidenceResolver(state["proofs"].__getitem__, lambda: {"owner_id": self.owner})
            if not owner_backed_reusable(node, self.owner, p["as_of"], work["purpose_scope"], resolver):
                raise ValueError("node-not-reusable")
            identity = ("owner_id", "agent_id", "work_key", "scope_digest", "node_id", "node_revision")
            for existing in state["links"]:
                if all(existing.get(k) == link.get(k) for k in identity):
                    return {"link": existing}
            # An inferred fit is useful immediately, but remains an agent
            # reading until the owner reviews it in a retro.
            link.setdefault("owner_confirmed", False)
            link.setdefault("linked_at", p["as_of"])
            state["links"].append(link)
            return {"link": link}
        if op == "apply_node_delta":
            node = p["node"]
            capability = self._proof(state, p["capability_ref"], "displayed-node-delta")
            previous = state["nodes"].get(node["id"])
            before = previous.get("revision") if previous else None
            if (capability.get("node_id") != node["id"]
                    or capability.get("before_revision") != before
                    or capability.get("after_hash") != digest(node)
                    or not capability.get("delivered_carrier")
                    or not capability.get("owner_reply_ref")
                    or p["capability_ref"] in state["consumed"]
                    or node.get("owner_id") != self.owner
                    or node.get("revision") != (before or 0) + 1):
                raise ValueError("delta-unbound")
            self._proof(state, capability["owner_reply_ref"], "owner-message")
            state["nodes"][node["id"]] = node
            self._validate_graph(state["nodes"])
            state["consumed"].append(p["capability_ref"])
            return {"node_id": node["id"], "node_revision": node["revision"]}
        if op == "register_child":
            child = p["child"]
            work = state["works"][child["work_key"]]
            if (work["agent_id"] != self.permission["agent_id"]
                    or child.get("scope_digest") != work["scope_digest"]
                    or not all(child.get(k) for k in ("consumer", "script_hash", "parent_request", "receipt_ref"))):
                raise ValueError("child-unbound")
            authorization = self._proof(state, child["receipt_ref"], "work-authorization")
            if any(authorization.get(k) != child.get(k) for k in (
                    "work_key", "scope_digest", "consumer", "script_hash", "parent_request")):
                raise ValueError("child-custody-mismatch")
            if child["child_id"] in state["children"] and state["children"][child["child_id"]] != child:
                raise ValueError("child-conflict")
            state["children"][child["child_id"]] = child
            return {"child_id": child["child_id"]}
        raise ValueError("unknown-operation")

    @staticmethod
    def _validate_graph(nodes):
        def visit(wid, path):
            if wid in path:
                raise ValueError("tree-cycle")
            for parent in nodes[wid].get("parents", []):
                if parent not in nodes:
                    raise ValueError("parent-unavailable")
                visit(parent, path | {wid})
        for wid in nodes:
            visit(wid, set())

    def transact(self, request):
        try:
            op = request["operation"]
            if (request.get("protocol") != PROTOCOL or request.get("authority_id") != AUTHORITY
                    or request.get("owner_key") != self.owner or request.get("principal") != self.principal
                    or op not in self.permission["actions"]):
                return {"status": "unauthorized"}
            # Prepare provider I/O outside the bounded authority lock.
            prepared = self._prepare_proof(request["payload"]) if op in ("import_proof", "import_legacy_tree") else None
            with locked(self.root):
                head = self.root / "head.json"
                state = json.loads(head.read_text()) if head.exists() else dict(
                    revision=0, nodes={}, works={}, links=[], proofs={}, consumed=[], children={}, requests={})
                if op == "read_snapshot":
                    if request.get("payload", {}).get("view") == "report":
                        view = agent_tree_view(self.owner, self.permission["agent_id"], state["nodes"], state["links"], state.get("node_origins", {}))
                    else:
                        # Matching uses the shared owner tree. Only report
                        # projection is limited to an agent's own branches.
                        view = {"nodes": state["nodes"], "links": state["links"]}
                    imports = {key: dict(source_sha256=value["source_sha256"],
                                         count=len(value["nodes"]))
                               for key, value in state.get("legacy_imports", {}).items()}
                    return dict(status="snapshot", authority_id=AUTHORITY,
                                revision=state["revision"], legacy_imports=imports,
                                works=state["works"], **view)
                rid, fingerprint = request["request_id"], digest(request)
                if not isinstance(rid, str) or not rid:
                    return {"status": "invalid-proof"}
                old = state["requests"].get(rid)
                if old:
                    return old["response"] if old["fingerprint"] == fingerprint else {"status": "invalid-proof"}
                if request.get("expected_revision") != state["revision"]:
                    return {"status": "conflict", "current_revision": state["revision"]}
                updated = copy.deepcopy(state)
                result = self._apply(updated, op, request["payload"], prepared)
                updated["revision"] += 1
                response = dict(status="committed", revision=updated["revision"], transaction_id=digest([rid, fingerprint]), result=result)
                updated["requests"][rid] = dict(fingerprint=fingerprint, response=response)
                # Only head is visible. A crash before head publication leaves
                # an uncommitted bundle; retry safely republishes the same bytes.
                atomic(self.root / "journal" / (response["transaction_id"] + ".json"), updated)
                atomic(head, updated)
                return response
        except TimeoutError:
            return {"status": "unavailable"}
        except (KeyError, ValueError, TypeError):
            return {"status": "invalid-proof"}
        except OSError:
            return {"status": "unavailable"}


def transact(request, descriptor_path):
    """Deployment-pinned local/SSH client; unavailable never means fallback."""
    import shlex
    import subprocess
    try:
        d = json.loads(Path(descriptor_path).read_text())
        if d.get("authority_id") != AUTHORITY or d.get("protocol_version") != PROTOCOL:
            return {"status": "unavailable"}
        engine = d["remote_engine_absolute_path"]
        if not os.path.isabs(engine):
            return {"status": "unavailable"}
        principal = d["principal_id"]
        command = ["python3", "-B", engine, "--stdio", "--descriptor", d["engine_descriptor"], "--principal", principal]
        if d.get("transport") == "local":
            if d.get("authority_host_identity") != Path(d.get(
                    "host_identity_file", "/etc/dogany/authority-host-id")).read_text().strip():
                return {"status": "unavailable"}
        elif d.get("transport") == "ssh":
            route = d["SSH_route"]
            if not re.fullmatch(r"[A-Za-z0-9_.@-]+", route) or route.startswith("-"):
                return {"status": "unavailable"}
            known_hosts = d["known_hosts_file"]
            keys = [" ".join(line.split()[-2:]) for line in Path(known_hosts).read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
            if d["pinned_SSH_host_key"] not in keys:
                return {"status": "unavailable"}
            command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                       "-o", "UserKnownHostsFile=" + known_hosts, "-o", "ConnectTimeout=2", route,
                       " ".join(shlex.quote(arg) for arg in command)]
        else:
            return {"status": "unavailable"}
        p = subprocess.run(command, input=json.dumps(request) + "\n", text=True,
                           capture_output=True, timeout=4)
        result = json.loads(p.stdout) if p.returncode == 0 else None
        return result if isinstance(result, dict) else {"status": "unavailable"}
    except (KeyError, ValueError, OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}


def shared_nodes(root):
    """Descriptor-bound reader; no local fallback after enrollment."""
    path = Path(root) / "config/why-authority.json"
    descriptor = json.loads(path.read_text())
    response = transact(dict(protocol=PROTOCOL, authority_id=AUTHORITY,
        principal=descriptor["principal_id"], owner_key=descriptor["owner_key"],
        operation="read_snapshot", payload={}), path)
    if response.get("status") != "snapshot":
        raise ValueError("shared-tree-unavailable")
    return response["nodes"]
