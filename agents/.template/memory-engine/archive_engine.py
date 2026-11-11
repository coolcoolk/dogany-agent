"""Proof-backed, crash-recoverable memory archive storage.

This module deliberately has no dependency on the search database.  Active
markdown is authoritative; the index can always be rebuilt after a committed
operation.  Archive state lives outside ``memories/``.
"""

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile


SCHEMA = "memory-archive-envelope-v1"
EVIDENCE_SCHEMA = "memory-archive-evidence-v1"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{2,255}$")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def _atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stable_item_id(instance_id, scan_id, relative_path, byte_start, payload):
    """Return the immutable legacy-v2 identity specified by FDGN-620."""
    return "legacy-v2:%s:%s:%s:%d:%s" % (
        instance_id, scan_id, relative_path, byte_start, _hash(payload))


def transcript_identity(row):
    """Cache rowids are disposable; identity includes the exact indexed text."""
    return _hash(_json_bytes([row["session"], row["ts"], row["role"],
                              _hash(row["text"].encode("utf-8"))]))


def ownership_equivalent(memory_claim, owner_claim):
    """Fail-closed, scope-aware equivalence used before automatic archive."""
    dimensions = ("subject", "target", "condition", "polarity", "modality",
                  "authority", "normalized_claim")
    if not isinstance(memory_claim, dict) or not isinstance(owner_claim, dict):
        return False
    return all(memory_claim.get(key) is not None and
               memory_claim.get(key) == owner_claim.get(key) for key in dimensions)


def rolling_remove(source, expected_sha256, spans):
    """Remove immutable byte spans, refusing a stale rolling source hash."""
    if _hash(source) != expected_sha256:
        raise ArchiveError("rolling source hash mismatch; regenerate dry-run")
    ordered = sorted(spans)
    if any(start < 0 or end < start or end > len(source) for start, end in ordered):
        raise ArchiveError("invalid replay span")
    if any(ordered[n][0] < ordered[n - 1][1] for n in range(1, len(ordered))):
        raise ArchiveError("overlapping replay spans")
    output = bytearray()
    cursor = 0
    for start, end in ordered:
        output.extend(source[cursor:start])
        cursor = end
    output.extend(source[cursor:])
    data = bytes(output)
    return data, _hash(data)


class ArchiveError(RuntimeError):
    pass


class MemoryArchiveEngine:
    def __init__(self, memories_dir, root, instance_id="default", clock=None):
        self.memories_dir = os.path.realpath(memories_dir)
        self.root = os.path.realpath(root)
        self.instance_id = instance_id
        self.clock = clock or (lambda: datetime.datetime.now(
            datetime.timezone.utc).isoformat().replace("+00:00", "Z"))
        if self.root == self.memories_dir or self.root.startswith(self.memories_dir + os.sep):
            raise ArchiveError("archive root must be outside memories")
        self.items = os.path.join(self.root, "archive", "items")
        self.objects = os.path.join(self.root, "archive", "objects", "sha256")
        self.audit = os.path.join(self.root, "audit", "events.jsonl")
        self.journal = os.path.join(self.root, "journal", "transaction.json")
        self.tokens = os.path.join(self.root, "delete-tokens")
        self.tombstones = os.path.join(self.root, "archive", "tombstones")
        self.lock_path = os.path.join(self.root, ".transaction.lock")
        self.transcripts = os.path.join(self.root, "archive", "transcripts.json")

    def transcript_state(self):
        """Durable transcript snapshots/exclusions, independent of state.db.

        Call under transaction() when coordinating with a cache writer.
        A clean read creates no files (including for CLI dry runs).
        """
        if not os.path.isfile(self.transcripts):
            return {"schema": "transcript-archive-v1",
                    "instance_id": self.instance_id, "archives": []}
        with open(self.transcripts, encoding="ascii") as stream:
            state = json.load(stream)
        if (state.get("schema") != "transcript-archive-v1" or
                state.get("instance_id") != self.instance_id):
            raise ArchiveError("transcript archive scope mismatch")
        for batch in state["archives"]:
            if _hash(_json_bytes(batch["rows"])) != batch["payload_sha256"]:
                raise ArchiveError("transcript archive payload hash mismatch")
        return state

    def save_transcript_state(self, state):
        """Caller holds transaction(); commit snapshots and exclusions together.

        The cache is reconciled after this durable journal commit. A crash in
        between is repaired by the next init/index, just like markdown replay.
        """
        self.journaled_replace(self.transcripts, _json_bytes(state))

    def archive_transcripts(self, state, rows, selector):
        """Archive one explicit selection; caller holds transaction()."""
        archive_id = "transcript-" + secrets.token_hex(12)
        batch = {"archive_id": archive_id, "state": "archived",
                 "archived_at": self.clock(), "selector": selector,
                 "rows": rows, "payload_sha256": _hash(_json_bytes(rows))}
        state["archives"].append(batch)
        self.save_transcript_state(state)
        self._audit("transcript-archive", archive_id, {"count": len(rows)})
        return archive_id

    def restore_transcripts(self, state, archive_id):
        """Release one selection; caller validates cache conflicts first."""
        batch = next((b for b in state["archives"]
                      if b["archive_id"] == archive_id), None)
        if not batch or batch["state"] != "archived":
            raise ArchiveError("transcript selection is not archived: %s" % archive_id)
        batch["state"] = "restored"
        batch["restored_at"] = self.clock()
        self.save_transcript_state(state)
        self._audit("transcript-restore", archive_id, {"count": len(batch["rows"])})
        return batch

    def needs_recovery(self):
        """True when a half-finished transaction is on disk (creates nothing)."""
        return os.path.isfile(self.journal)

    def journaled_replace(self, target, data):
        """Atomically replace ``target`` through the recovery journal.

        Caller holds ``transaction()``.  The new bytes are fully written to a
        temp file and the journal is durable before the rename, so a kill at
        any point is completed (or discarded) by the next ``recover()``.
        """
        directory = os.path.dirname(target)
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".txn-", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _atomic(self.journal, _json_bytes({"replacements": [
            {"temp": temp, "target": target, "sha256": _hash(data)}]}))
        if os.environ.get("MEMORY_TEST_KILL_AT") == "after-journal":
            os.kill(os.getpid(), 9)
        self.recover()
        return True

    @contextlib.contextmanager
    def transaction(self):
        os.makedirs(self.root, exist_ok=True)
        with open(self.lock_path, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.recover()
            yield

    def recover(self):
        """Complete an interrupted atomic file replacement before any read."""
        if not os.path.isfile(self.journal):
            return False
        with open(self.journal, "r", encoding="utf-8") as stream:
            row = json.load(stream)
        for op in row.get("replacements", []):
            temp, target = op["temp"], op["target"]
            if os.path.isfile(temp):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(temp, target)
            if not os.path.isfile(target) or _hash(open(target, "rb").read()) != op["sha256"]:
                raise ArchiveError("journal recovery hash mismatch")
        os.unlink(self.journal)
        return True

    def _item_path(self, item_id):
        if not _ID_RE.fullmatch(item_id):
            raise ArchiveError("invalid item id")
        return os.path.join(self.items, item_id, "envelope.json")

    def _load(self, item_id):
        path = self._item_path(item_id)
        if not os.path.isfile(path):
            raise ArchiveError("item is not archived: %s" % item_id)
        with open(path, "r", encoding="ascii") as stream:
            row = json.load(stream)
        if row.get("schema") != SCHEMA or row.get("instance_id") != self.instance_id:
            raise ArchiveError("archive envelope scope mismatch")
        return row

    def _audit(self, action, item_id, details=None):
        os.makedirs(os.path.dirname(self.audit), exist_ok=True)
        previous = "0" * 64
        if os.path.isfile(self.audit):
            with open(self.audit, "rb") as stream:
                lines = [x for x in stream.read().splitlines() if x]
            if lines:
                previous = json.loads(lines[-1])["event_hash"]
        event = {"schema": "memory-audit-v1", "instance_id": self.instance_id,
                 "action": action, "item_id": item_id, "at": self.clock(),
                 "previous_hash": previous, "details": details or {}}
        event["event_hash"] = _hash(_json_bytes(event))
        with open(self.audit, "ab") as stream:
            stream.write(_json_bytes(event))
            stream.flush()
            os.fsync(stream.fileno())
        return event["event_hash"]

    def archive_bytes(self, item_id, payload, source_relative, evidence, dry_run=False):
        """Archive exact bytes after validating destination ownership evidence.

        File rewriting is intentionally a caller operation: this primitive is
        also used by replay, which journals archive creation and source rewrite
        together.  It never accepts similarity or a boolean as proof.
        """
        if not isinstance(payload, bytes) or not payload:
            raise ArchiveError("non-empty byte payload required")
        required = ("destination_ref", "destination_version", "binding_version",
                    "normalized_claim_hash", "receipt")
        if not isinstance(evidence, dict) or any(not evidence.get(k) for k in required):
            raise ArchiveError("complete ownership evidence required")
        if evidence.get("instance_id", self.instance_id) != self.instance_id:
            raise ArchiveError("evidence instance mismatch")
        result = {"item_id": item_id, "payload_sha256": _hash(payload),
                  "source_relative": source_relative}
        if dry_run:
            return result
        with self.transaction():
            path = self._item_path(item_id)
            if os.path.exists(path):
                old = self._load(item_id)
                if old["payload_sha256"] != result["payload_sha256"]:
                    raise ArchiveError("same item id has different bytes")
                return result
            digest = result["payload_sha256"]
            obj = os.path.join(self.objects, digest)
            if not os.path.isfile(obj):
                _atomic(obj, payload)
            proof = dict(evidence)
            proof.update({"schema": EVIDENCE_SCHEMA, "instance_id": self.instance_id,
                          "item_id": item_id, "source_sha256": digest,
                          "archive_payload_sha256": digest})
            proof_hash = _hash(_json_bytes(proof))
            envelope = {"schema": SCHEMA, "instance_id": self.instance_id,
                        "item_id": item_id, "state": "archived",
                        "payload_sha256": digest, "object_ref": digest,
                        "source_relative": source_relative,
                        "archived_at": self.clock(), "evidence": proof,
                        "evidence_sha256": proof_hash}
            envelope["audit_hash"] = self._audit("archive", item_id,
                                                   {"evidence_sha256": proof_hash})
            _atomic(path, _json_bytes(envelope))
            return result

    def restore(self, item_id, to="original"):
        with self.transaction():
            row = self._load(item_id)
            obj = os.path.join(self.objects, row["object_ref"])
            try:
                payload = open(obj, "rb").read()
            except OSError as exc:
                raise ArchiveError("archive payload missing") from exc
            if _hash(payload) != row["payload_sha256"]:
                raise ArchiveError("archive payload hash mismatch")
            relative = row["source_relative"]
            target = os.path.join(self.memories_dir, relative)
            if to == "inbox" or not os.path.isfile(target):
                target = os.path.join(self.memories_dir, "inbox.md")
                prefix = ("<!-- restored-origin: %s item-id: %s -->\n" %
                          (relative, item_id)).encode("utf-8")
            else:
                prefix = b""
            current = open(target, "rb").read() if os.path.isfile(target) else b""
            marker = ("item-id: %s" % item_id).encode("ascii")
            if marker in current:
                raise ArchiveError("same item id is already active")
            addition = prefix + payload
            if current and not current.endswith(b"\n"):
                current += b"\n"
            _atomic(target, current + addition)
            self._audit("restore", item_id, {"target": os.path.relpath(target, self.memories_dir)})
            shutil.rmtree(os.path.dirname(self._item_path(item_id)))
            return target

    def preview_delete(self, item_ids, owner_turn, ttl_seconds=300):
        ids = sorted(set(item_ids))
        if not ids or any("*" in x or not _ID_RE.fullmatch(x) for x in ids):
            raise ArchiveError("explicit item ids required")
        if not re.fullmatch(r"otl-[1-9][0-9]*", owner_turn or ""):
            raise ArchiveError("verified owner-turn ledger ref required")
        with self.transaction():
            for item_id in ids:
                self._load(item_id)
            token = secrets.token_urlsafe(24)
            expires = datetime.datetime.now(datetime.timezone.utc).timestamp() + ttl_seconds
            _atomic(os.path.join(self.tokens, token + ".json"), _json_bytes(
                {"ids": ids, "owner_turn": owner_turn, "expires": expires}))
            return token

    def delete(self, item_ids, owner_turn, token):
        ids = sorted(set(item_ids))
        token_path = os.path.join(self.tokens, os.path.basename(token) + ".json")
        with self.transaction():
            try:
                proof = json.load(open(token_path, "r", encoding="ascii"))
            except OSError as exc:
                raise ArchiveError("invalid or reused confirmation token") from exc
            if (proof["ids"] != ids or proof["owner_turn"] != owner_turn or
                    proof["expires"] < datetime.datetime.now(datetime.timezone.utc).timestamp()):
                raise ArchiveError("confirmation does not match this deletion")
            os.unlink(token_path)  # single use even if a later integrity check fails
            rows = [(item_id, self._load(item_id)) for item_id in ids]
            for item_id, row in rows:
                envelope_path = self._item_path(item_id)
                os.unlink(envelope_path)
                shutil.rmtree(os.path.dirname(envelope_path))
                # Shared content is destroyed only after the last envelope ref.
                referenced = False
                if os.path.isdir(self.items):
                    for base, _, files in os.walk(self.items):
                        if "envelope.json" in files:
                            other = json.load(open(os.path.join(base, "envelope.json"), encoding="ascii"))
                            if other.get("object_ref") == row["object_ref"]:
                                referenced = True
                                break
                if not referenced:
                    obj = os.path.join(self.objects, row["object_ref"])
                    if os.path.isfile(obj):
                        os.unlink(obj)
                tomb = {"schema": "memory-delete-tombstone-v1", "instance_id": self.instance_id,
                        "item_id": item_id, "former_payload_sha256": row["payload_sha256"],
                        "evidence_sha256": row["evidence_sha256"], "deleted_at": self.clock(),
                        "owner_turn": owner_turn}
                self._audit("delete", item_id, {"owner_turn": owner_turn})
                _atomic(os.path.join(self.tombstones, item_id + ".json"), _json_bytes(tomb))


def exact_text_b64(text):
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return base64.b64encode(data).decode("ascii"), _hash(data)
