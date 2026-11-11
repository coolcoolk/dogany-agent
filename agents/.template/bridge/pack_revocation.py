"""Pack-scoped retirement of a replaced engine's live entrypoints.

A replacement pack (manifest replacement_manifest, coexistence false) retires
its previous engine at install. The installer persists one durable record per
pack, config/packs/<pack>.revocation.json, BEFORE the first replacement byte
lands. Every seat that can still execute or emit for the old engine reads it:

  * idrill execution (bridge/bot.py) refuses an arm whose declared command
    belongs to the pack, checked after the arm is loaded and immediately
    before the command runs; arms of other consumers keep working;
  * pack callback prefixes (flow|) are refused at dispatch;
  * queued and new framework notices carrying the pack's buttons are dropped
    instead of being released at their deadline.

The record is never removed: there is no rollback (forward-fix only), and a
later install of the same pack rewrites it with the same or wider scope.
Running admitted invocations register a marker before their revocation check,
so the installer can drain exactly the ones that passed it.
"""
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

SCHEMA = "pack_revocation/1"
ATTESTATION_SCHEMA = "pack_replacement_absence/1"
INTERPRETERS = frozenset(("python", "python3", "bash", "sh", "zsh", "env"))
ARM_KEYS = ("cmd", "cmd_skip2", "followup_cmd")


class Revoked(Exception):
    def __init__(self, pack, reason):
        super().__init__(reason)
        self.pack = pack


def record_path(root, pack):
    return Path(root) / "config/packs" / (pack + ".revocation.json")


def invocation_dir(root):
    return Path(root) / ".telegram_bot/live-surface/pack-invocations"


def _valid(value):
    def strings(items):
        return isinstance(items, list) and all(isinstance(i, str) and i for i in items)
    return (isinstance(value, dict) and value.get("schema") == SCHEMA
            and isinstance(value.get("pack"), str) and value["pack"]
            and strings(value.get("callback_prefixes")) and strings(value.get("arm_command_paths"))
            and strings(value.get("arm_command_keys")) and strings(value.get("retired_arm_ids", []))
            and strings(value.get("notice_producers", [])))


def records(root):
    """All revocation records. A damaged record fails closed (ValueError)."""
    directory = Path(root) / "config/packs"
    found = []
    for path in sorted(directory.glob("*.revocation.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("unreadable pack revocation record: " + path.name) from error
        if not _valid(value) or path.name != value["pack"] + ".revocation.json":
            raise ValueError("invalid pack revocation record: " + path.name)
        found.append(value)
    return found


def write(root, value):
    if not _valid(value):
        raise ValueError("invalid pack revocation record")
    path = record_path(root, value["pack"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _script(argv):
    """executable_or_interpreter_script: the file an argv actually runs."""
    if not argv:
        return None
    head = Path(argv[0]).name
    if head.startswith("python") or head in INTERPRETERS:
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if head == "env":
            rest = [a for a in rest if "=" not in a]
            return _script(rest) if rest else None
        return rest[0] if rest else None
    return argv[0]


def _relative(root, path):
    root = Path(root).resolve()
    candidate = Path(path)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return None


def argv_owner(root, argv, revoked=None):
    """The revoked pack whose retired command this argv runs, or None."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None
    script = _script(argv)
    relative = _relative(root, script) if script else None
    if relative is None:
        return None
    for value in records(root) if revoked is None else revoked:
        if relative in value["arm_command_paths"]:
            return value["pack"]
    return None


def arm_owner(root, arm, arm_id=None, revoked=None):
    revoked = records(root) if revoked is None else revoked
    for value in revoked:
        if arm_id is not None and arm_id in value.get("retired_arm_ids", []):
            return value["pack"]
    if not isinstance(arm, dict):
        return None
    for value in revoked:
        for key in value["arm_command_keys"]:
            if argv_owner(root, arm.get(key), [value]):
                return value["pack"]
    return None


def callback_owner(root, data, revoked=None):
    """A pack callback route (prefix) or a retired/pack-owned idrill arm."""
    if not isinstance(data, str):
        return None
    revoked = records(root) if revoked is None else revoked
    for value in revoked:
        if any(data.startswith(prefix) for prefix in value["callback_prefixes"]):
            return value["pack"]
    if revoked and data.startswith("idrill:"):
        arm_id = data.split(":")[1] if data.count(":") >= 1 else ""
        from bridge import artifacts
        arm = artifacts.read_arm(Path(root), arm_id) if arm_id else None
        return arm_owner(root, arm, arm_id, revoked)
    return None


def _buttons(payload):
    rows = []
    if isinstance(payload, dict):
        rows.extend(payload.get("buttons") or [])
        raw = payload.get("raw_request")
        markup = raw.get("parameters", {}).get("reply_markup") if isinstance(raw, dict) else None
        if isinstance(markup, dict):
            rows.extend(markup.get("inline_keyboard") or [])
    for row in rows:
        for button in row if isinstance(row, list) else []:
            if isinstance(button, dict):
                yield button.get("callback_data")


def notice_owner(root, intended):
    """The revoked pack a queued notice belongs to, or None."""
    revoked = records(root)
    if not revoked or not isinstance(intended, dict):
        return None
    for value in revoked:
        if intended.get("producer") in value.get("notice_producers", []):
            return value["pack"]
    for data in _buttons(intended.get("payload")):
        owner = callback_owner(root, data, revoked)
        if owner:
            return owner
    return None


def keyboard_owner(root, rows):
    return notice_owner(root, dict(producer=None, payload=dict(buttons=rows)))


@contextlib.contextmanager
def invocation(root, argv, clock=None):
    """Register, then check: an admitted run is visible to the drain."""
    directory = invocation_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (secrets.token_hex(12) + ".json")
    now = clock() if clock else int(time.time() * 1000)
    path.write_text(json.dumps(dict(pid=os.getpid(), argv=argv, started_at_ms=now)), encoding="utf-8")
    try:
        owner = argv_owner(root, argv)
        if owner:
            raise Revoked(owner, "pack command retired by replacement")
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def admitted(root, pack, alive=_alive):
    """Live registered invocations of the pack's retired commands."""
    directory = invocation_dir(root)
    value = next((r for r in records(root) if r["pack"] == pack), None)
    if value is None:
        return []
    found = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(marker, dict) or type(marker.get("pid")) is not int:
            continue
        if argv_owner(root, marker.get("argv"), [value]) and alive(marker["pid"]):
            found.append(marker)
    return found


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def mac(key, value):
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


def installer_key_path(root):
    return Path(root) / ".telegram_bot/live-surface/installer.key"


def attestation_path(root, pack):
    return Path(root) / "config/packs" / (pack + ".replacement-attestation.json")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _need(condition, reason):
    if not condition:
        raise ValueError(reason)


def legacy_targets(root, directories, owner_chat=None):
    """Exact {chat_id,message_id} keyboards in legacy surface registries.

    A record carrying buttons whose target cannot be resolved exactly is an
    unknown live legacy target and refuses (ValueError).
    """
    root = Path(root)
    if owner_chat is None:
        lock = root / ".telegram_bot/owner.lock"
        raw = lock.read_text(encoding="utf-8").strip() if lock.is_file() else ""
        owner_chat = int(raw) if re.fullmatch(r"-?[0-9]+", raw) else None
    found = {}
    for rel in directories:
        directory = root / rel
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ValueError("unreadable legacy surface record: " + path.name) from error
            if not isinstance(record, dict) or not record.get("buttons"):
                continue
            chat = record.get("chat_id")
            chat = owner_chat if chat is None else chat
            _need(type(record.get("message_id")) is int and record["message_id"] > 0 and type(chat) is int,
                  "unknown live legacy target: " + path.name)
            found[(chat, record["message_id"])] = dict(chat_id=chat, message_id=record["message_id"])
    return [found[k] for k in sorted(found)]


def verify_attestation(root, pack, key=None):
    """The installer's absence attestation is authentic and still true."""
    root = Path(root).resolve()
    value = json.loads(attestation_path(root, pack).read_text(encoding="utf-8"))
    if key is None:
        path = installer_key_path(root)
        _need(not path.is_symlink() and (path.stat().st_mode & 0o077) == 0, "installer key must be private")
        key = path.read_bytes()
    _need(isinstance(value, dict) and value.get("schema") == ATTESTATION_SCHEMA and value.get("pack") == pack,
          "absence attestation schema differs")
    auth = value.get("auth") or {}
    body = {k: v for k, v in value.items() if k != "auth"}
    _need(isinstance(auth.get("mac"), str) and hmac.compare_digest(auth["mac"], mac(key, body)),
          "absence attestation is not authentic")
    _need(value["installed_root"] == str(root), "attestation names another root")
    _need(value["remaining_paths"] == [] and value["previous_manifest"] and
          value["previous_manifest"] == sorted(set(value["previous_manifest"])) and
          value["manifest_digest"] == digest(value["previous_manifest"]), "attestation records remaining resources")
    inventory_path = root / value["replacement_manifest"]
    inventory = json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.is_file() else None
    _need(inventory is not None and digest(inventory) == value["replacement_digest"],
          "installed replacement inventory differs from the attestation")
    hashes = {r["path"]: r["sha256"] for r in inventory["resources"]
              if r.get("disposition") == "replace" and "sha256" in r}
    for rel in value["previous_manifest"]:
        _need(isinstance(rel, str) and rel and not rel.startswith("/") and ".." not in rel.split("/"),
              "attestation path escapes the instance")
        path = root / rel
        if rel in hashes:
            _need(path.is_file() and _sha(path) == hashes[rel], "previous engine bytes returned at " + rel)
        else:
            _need(not (path.exists() or path.is_symlink()), "retired resource returned: " + rel)
    for name in ("drain", "deferred", "callbacks", "healthkit"):
        fact = value["facts"].get(name)
        _need(isinstance(fact, dict) and fact.get("complete") is True and
              fact.get("evidence_digest") == digest(fact.get("evidence")), "incomplete attested fact: " + name)
    return value


def main(argv=None):
    """push.sh seat: rc 9 refuses a revoked pack's keyboard notice."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("check-push",))
    parser.add_argument("--root", required=True)
    parser.add_argument("--buttons-json", default="[]")
    parser.add_argument("--keyboard-file")
    parser.add_argument("--button", default="")
    args = parser.parse_args(argv)
    try:
        rows = list(json.loads(args.buttons_json or "[]"))
        if args.button:
            label, _, callback = args.button.partition("::")
            rows.append([dict(text=label, callback_data=callback)])
        if args.keyboard_file:
            keyboard = json.loads(Path(args.keyboard_file).read_text(encoding="utf-8"))
            rows = list(rows) + list(keyboard.get("reply_markup", {}).get("inline_keyboard", []))
        owner = keyboard_owner(args.root, rows)
    except (OSError, ValueError, AttributeError, TypeError) as error:
        print("pack revocation check failed closed: " + str(error), file=__import__("sys").stderr)
        return 9
    if owner:
        print("notice refused: pack " + owner + " retired by replacement", file=__import__("sys").stderr)
        return 9
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
