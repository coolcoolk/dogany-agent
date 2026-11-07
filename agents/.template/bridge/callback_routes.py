"""Installed callback routes, authenticated ingress and publisher checkpoints.

The pack shim owns domain decoding and recovery. A successful child exit is
never evidence of completed delivery. Recovery must use the stored command.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from bridge.session_state import (Refusal, adapter_config, argv_valid, atomic_bytes,
                                  atomic_json, canonical, chat_lock, contained,
                                  data_dir, digest, integer, locked, now_ms,
                                  pack_name, read_json)

APIS = {"protocol": [2], "live_surface_api": [1], "callback_route_api": [1],
        "due_event_api": [1], "ledger_api": [2]}
CAP_LINE = "protocol:2,live_surface_api:1,callback_route_api:1,due_event_api:1"
PREFIX = re.compile(r"[a-z][a-z0-9]{1,11}\|")
FORBIDDEN_ENV = ("LD_", "DYLD_", "PYTHON", "TELEGRAM")


def validate_route(value, root, check_files=True):
    required = {"schema", "pack", "routes", "due_argv", "probe_argv"}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {"env", "retire_prefixes"}
            or value["schema"] != "callback_routes/1"):
        raise Refusal(2, "invalid callback route schema")
    pack_name(value["pack"])
    if not isinstance(value["routes"], list) or not 1 <= len(value["routes"]) <= 4:
        raise Refusal(2, "invalid callback routes")
    prefixes = []
    for row in value["routes"]:
        if (not isinstance(row, dict) or not {"prefix", "argv", "fallback"} <= row.keys()
                or row.keys() - {"prefix", "argv", "fallback", "timeout_s"}
                or not isinstance(row["prefix"], str) or not PREFIX.fullmatch(row["prefix"])
                or row["prefix"] == "flow|" or row["prefix"] in prefixes
                or row["fallback"] != "none"
                or not integer(row.get("timeout_s", 8), 1) or row.get("timeout_s", 8) > 10):
            raise Refusal(2, "invalid or reserved callback route")
        prefixes.append(row["prefix"])
    for argv in [r["argv"] for r in value["routes"]] + [value["due_argv"], value["probe_argv"]]:
        argv_valid(argv)
        # Interpreter argv may be absolute, but no executable is resolved via PATH.
        if Path(argv[0]).is_absolute():
            if check_files and not os.access(argv[0], os.X_OK):
                raise Refusal(2, "interpreter unavailable")
            if len(argv) < 2:
                raise Refusal(2, "absolute interpreter requires a payload script")
            contained(root, argv[1], exists=check_files)
        else:
            path = contained(root, argv[0], exists=check_files)
            if check_files and not os.access(path, os.X_OK):
                raise Refusal(2, "route executable unavailable")
    retired = value.get("retire_prefixes", [])
    if retired not in ([], ["flow|"]):
        raise Refusal(2, "invalid retired prefixes")
    env = value.get("env", {})
    if not isinstance(env, dict) or len(env) > 4:
        raise Refusal(2, "invalid route environment")
    for key, val in env.items():
        if (not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", key)
                or key in ("PATH", "HOME", "INSTANCE_ROOT")
                or key.startswith(FORBIDDEN_ENV)):
            raise Refusal(2, "reserved route environment key")
        contained(root, val, exists=check_files)
    return value


def receipt(root, pack):
    path = Path(root) / "config/packs" / (pack_name(pack) + ".receipt")
    try:
        rows = {}
        for line in path.read_text().splitlines():
            key, sep, val = line.partition("=")
            if sep:
                if key.startswith("cap.session_flow_v2") and key in rows:
                    raise Refusal(33, "duplicate capability receipt key")
                rows[key] = val
        return rows
    except OSError as exc:
        raise Refusal(33, "missing installed route receipt") from exc


def installed(root, pack):
    rows = receipt(root, pack)
    value = validate_route(read_json(Path(root) / "config/packs" / (pack + ".callback_routes.json")), root)
    try:
        if (value["pack"] != pack or rows.get("cap.session_flow_v2") != CAP_LINE
                or rows.get("cap.session_flow_v2.route_digest") != digest(value)
                or rows.get("cap.session_flow_v2.routes") != ",".join(r["prefix"] for r in value["routes"])
                or rows.get("cap.session_flow_v2.retire_prefixes", "") != ",".join(value.get("retire_prefixes", []))
                or any(json.loads(rows["cap.session_flow_v2." + key]) != value[key]
                       for key in ("due_argv", "probe_argv"))):
            raise Refusal(33, "route projection differs from installed authority")
    except (KeyError, ValueError) as exc:
        raise Refusal(33, "malformed installed route receipt") from exc
    return value


def child_overrides(root, route):
    return {"INSTANCE_ROOT": str(Path(root).resolve()),
            **{k: str(contained(root, v)) for k, v in route.get("env", {}).items()}}


def child_env(root, route):
    return dict(os.environ, **child_overrides(root, route))


def executable(root, argv):
    argv_valid(argv)
    return [str(contained(root, argv[0])) if not Path(argv[0]).is_absolute() else argv[0], *argv[1:]]


def candidates(root):
    directory = Path(root) / "config/packs"
    return [p.name[:-8] for p in sorted(directory.glob("*.receipt"))
            if "cap.session_flow_v2=" in p.read_text()]


def locate(root, data):
    matches = []
    if data.startswith("flow|"):
        # Runtime revocation survives deletion/partial replacement of receipts.
        # Only a completed deactivation restores the original callback route.
        from bridge.session_activation import load_activation
        for path in (Path(root) / "config/packs").glob("*.session_activation.json"):
            pack = path.name[:-len(".session_activation.json")]
            current = load_activation(root, pack)
            checkpoint = current["transition_checkpoint"] or {}
            if (current["phase"] in ("activating", "active", "deactivating")
                    and "flow|" in checkpoint.get("retire_prefixes", [])):
                matches.append(pack)
        if len(matches) > 1:
            raise Refusal(33, "legacy revocation has multiple owners")
        if matches:
            return matches[0]
    for pack in candidates(root):
        rows = receipt(root, pack)
        prefixes = rows.get("cap.session_flow_v2.routes", "").split(",")
        if any(data.startswith(p) for p in prefixes if PREFIX.fullmatch(p)):
            matches.append(pack)
        elif data.startswith("flow|") and rows.get("cap.session_flow_v2.retire_prefixes") == "flow|":
            # Staging alone leaves the original route untouched.
            from bridge.session_activation import load_activation
            state = load_activation(root, pack)
            if state["phase"] in ("activating", "active", "deactivating"):
                matches.append(pack)
    if len(matches) > 1:
        raise Refusal(33, "callback prefix has multiple owners")
    return matches[0] if matches else None


def key_bytes(root):
    path = data_dir(root) / "envelope.key"
    with locked(path.with_suffix(".lock")):
        if not path.exists():
            atomic_bytes(path, secrets.token_hex(32).encode("ascii"))
        try:
            key = bytes.fromhex(path.read_text())
            if len(key) != 32 or path.stat().st_mode & 0o077:
                raise ValueError("invalid key")
            return key
        except (OSError, ValueError) as exc:
            raise Refusal(40, "invalid envelope key") from exc


def sign(root, value):
    value = dict(value)
    value.pop("mac", None)
    value["mac"] = hmac.new(key_bytes(root), canonical(value), hashlib.sha256).hexdigest()
    return value


def authenticated(root, envelope):
    if not isinstance(envelope, dict):
        raise Refusal(20, "invalid envelope")
    unsigned = dict(envelope)
    supplied = unsigned.pop("mac", "")
    expected = hmac.new(key_bytes(root), canonical(unsigned), hashlib.sha256).hexdigest()
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        raise Refusal(20, "invalid envelope authentication")
    if (envelope.get("v") != 1 or envelope.get("kind") not in ("tap", "due", "turn", "probe")
            or not integer(envelope.get("chat_id")) or not integer(envelope.get("user_id"))
            or not integer(envelope.get("issued_at_ms"))
            or not re.fullmatch(r"[0-9a-f]{32}", envelope.get("nonce", ""))
            or not isinstance(envelope.get("envelope_id"), str)):
        raise Refusal(20, "invalid authenticated envelope schema")
    return envelope


def ingress_path(root, chat, op):
    return data_dir(root) / "ingress" / str(chat) / (hashlib.sha256(op.encode()).hexdigest() + ".json")


def admit(root, pack, envelope, argv, timeout_s, owner_text=None):
    authenticated(root, envelope)
    path = ingress_path(root, envelope["chat_id"], envelope["envelope_id"])
    with chat_lock(root, envelope["chat_id"]):
        if path.exists():
            old = read_json(path)
            if old["envelope"] != envelope:
                raise Refusal(22, "changed bytes for admitted identity")
            return old
        route = installed(root, pack)
        from bridge.session_activation import require_active
        active = require_active(root, pack, digest(route))
        due_recovery = False
        if envelope["kind"] == "due":
            group_path = Path(root) / ".telegram_bot/due/groups" / (hashlib.sha256(envelope["envelope_id"].encode()).hexdigest() + ".json")
            group = read_json(group_path)
            due_recovery = (group["envelope"] == envelope and group["binding_digest"] == digest(envelope)
                            and group["state"] == "firing" and group["route_digest"] == digest(route)
                            and group["generation"] == active["generation"])
            if not due_recovery:
                raise Refusal(22, "due ingress differs from canonical group")
        if not due_recovery and not -5000 <= now_ms() - envelope["issued_at_ms"] <= 120000:
            raise Refusal(21, "initial envelope expired")
        permitted = ([r["argv"] for r in route["routes"]] if envelope["kind"] == "tap"
                     else [route["due_argv"]] if envelope["kind"] == "due" else [None])
        if argv not in permitted or envelope["kind"] == "probe":
            raise Refusal(15, "ingress argv is not installed for this event kind")
        config_path = route.get("env", {}).get("LIVE_ADAPTER_CONFIG")
        if config_path is None:
            raise Refusal(16, "adapter configuration missing")
        config = adapter_config(read_json(contained(root, config_path)))
        principals = [r for r in config["identities"] if
                      (r["chat_id"], r["user_id"]) == (envelope["chat_id"], envelope["user_id"])]
        if len(principals) != 1:
            raise Refusal(18, "unmapped authenticated principal")
        record = {"schema": "session_ingress/1", "pack": pack, "envelope": envelope,
                  "envelope_digest": digest(envelope), "principal": principals[0],
                  "route": route, "route_digest": digest(route), "argv": argv,
                  "env": child_overrides(root, route), "env_digest": digest(child_overrides(root, route)),
                  "timeout_s": timeout_s,
                  "config": config, "config_digest": digest(config),
                  "generation": active["generation"], "dispatch_state": "accepted",
                  "incarnation": None, "command_binding": None, "acknowledgement": None,
                  "callback_answer": "unsent", "toast": None, "owner_text": owner_text}
        # No nonce is consumed until the complete ingress has reached durable storage.
        atomic_json(path, record)
        return record


def verify(root, envelope, recovery=False):
    authenticated(root, envelope)
    path = ingress_path(root, envelope["chat_id"], envelope["envelope_id"])
    with chat_lock(root, envelope["chat_id"]):
        record = read_json(path)
        if record["envelope"] != envelope:
            raise Refusal(22, "envelope differs from durable admission")
        if recovery and (not hasattr(recovery, "authorize_ingress_recovery")
                         or not recovery.authorize_ingress_recovery(record)):
            raise Refusal(17, "authenticated recovery authority required")
        if not recovery and not -5000 <= now_ms() - envelope["issued_at_ms"] <= 120000:
            raise Refusal(21, "initial envelope expired")
        nonce = data_dir(root) / "nonces" / (envelope["nonce"] + ".json")
        if nonce.exists():
            prior = read_json(nonce)
            if not recovery or prior["ingress"] != str(path) or prior["digest"] != record["envelope_digest"]:
                raise Refusal(22, "nonce already consumed")
        else:
            atomic_json(nonce, {"ingress": str(path), "digest": record["envelope_digest"]})
        return {"ok": True, "envelope_id": envelope["envelope_id"], "envelope": envelope}


def bind_command(root, envelope, binding, codec):
    """Trusted shim supplies the pinned kit codec; no bridge digest substitution.

    codec.validate_binding checks exact command bytes, caller credential, typed
    session/principal/source and the kit's own canonical command digest.
    """
    verify(root, envelope, recovery=codec)
    path = ingress_path(root, envelope["chat_id"], envelope["envelope_id"])
    with chat_lock(root, envelope["chat_id"]):
        record = read_json(path)
        codec.validate_binding(binding, record)
        if (not integer(binding.get("session_id"), 1)
                or not isinstance(binding.get("command_bytes"), str)
                or not binding.get("command_digest") or not binding.get("caller")):
            raise Refusal(16, "invalid typed command binding")
        old = record["command_binding"]
        if old is not None and old != binding:
            raise Refusal(16, "command changed for original envelope")
        record["command_binding"] = binding
        record["dispatch_state"] = "bound"
        atomic_json(path, record)
        return binding


def record_acknowledgement(root, envelope, acknowledgement, codec, delivery_complete=False):
    path = ingress_path(root, envelope["chat_id"], envelope["envelope_id"])
    with chat_lock(root, envelope["chat_id"]):
        record = read_json(path)
        if record["envelope"] != authenticated(root, envelope) or record["command_binding"] is None:
            raise Refusal(16, "acknowledgement has no original command binding")
        codec.validate_acknowledgement(acknowledgement, record)
        fields = ["current_revision", "diagnostic_key", "model_fallback", "receipt", "schema", "session_id", "status"]
        status = acknowledgement.get("status")
        if (sorted(acknowledgement) != fields
                or not integer(acknowledgement["session_id"], 1)
                or acknowledgement["session_id"] != record["command_binding"]["session_id"]
                or status not in ("ACCEPTED", "DELIVERY_PENDING", "MOVED", "INVALID", "UNAVAILABLE")):
            raise Refusal(16, "invalid pinned acknowledgement")
        old = record["acknowledgement"]
        if old is not None and old != acknowledgement:
            raise Refusal(16, "changed original acknowledgement")
        if delivery_complete:
            codec.validate_completion(record, acknowledgement)
        record["acknowledgement"] = acknowledgement
        record["dispatch_state"] = ("complete" if delivery_complete or status in
                                    ("MOVED", "INVALID", "UNAVAILABLE") else "delivery_pending")
        copies = record["config"]["toast_copy"]
        selected = next((r for r in copies if r["status"] == status and
                         r["diagnostic_key"] == acknowledgement["diagnostic_key"]), None)
        selected = selected or next((r for r in copies if r["status"] == status and
                                     r["diagnostic_key"] is None), None)
        record["toast"] = ({"toast": selected["text"], "alert": selected["alert"]} if selected else
                           {"toast": "", "alert": False, "fallback":
                            "stale" if status == "MOVED" else "failed" if status in
                            ("INVALID", "UNAVAILABLE") else None})
        record["adapter_rc"] = 3 if status == "MOVED" else 4 if status in ("INVALID", "UNAVAILABLE") else 0
        atomic_json(path, record)
        return record


def launch(root, path):
    """One initial launch only. Ambiguous invocations require trusted recovery."""
    path = Path(path)
    with locked(path.with_suffix(".run.lock"), blocking=False, inherited=True) as runner:
        record = read_json(path)
        if record["dispatch_state"] != "accepted":
            return record
        with chat_lock(root, record["envelope"]["chat_id"]):
            record = read_json(path)
            if record["dispatch_state"] != "accepted":
                return record
            from bridge.session_activation import require_active
            require_active(root, record["pack"], record["route_digest"], record["generation"])
            if installed(root, record["pack"]) != record["route"]:
                raise Refusal(33, "installed route changed after admission")
            if (record.get("env_digest") != digest(record["env"])
                    or record["env"] != child_overrides(root, record["route"])):
                raise Refusal(33, "installed child environment changed after admission")
            envelope = authenticated(root, record["envelope"])
            if digest(envelope) != record["envelope_digest"]:
                raise Refusal(20, "durable envelope digest changed")
            surface = envelope.get("surface")
            if surface:
                current = read_json(data_dir(root) / (str(envelope["chat_id"]) + ".json"))
                if (current["state"] != "open" or current["epoch"] != surface["epoch"]
                        or current["surface_id"] != surface["surface_id"]):
                    raise Refusal(10, "ingress surface was explicitly closed or moved")
            if envelope["kind"] == "due":
                group_path = Path(root) / ".telegram_bot/due/groups" / (hashlib.sha256(envelope["envelope_id"].encode()).hexdigest() + ".json")
                if read_json(group_path)["state"] != "firing":
                    raise Refusal(10, "due group was cancelled")
            if record["argv"] is None:
                raise Refusal(10, "owner turns are resolved by the trusted publisher")
            record["dispatch_state"] = "launching"
            record["incarnation"] = secrets.token_hex(16)
            atomic_json(path, record)
            try:
                process = subprocess.Popen(executable(root, record["argv"]), cwd=root,
                                           env=dict(os.environ, **record["env"]), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           pass_fds=(runner.fileno(),))
            except OSError:
                process = None
        try:
            if process is None:
                raise OSError("route launch failed")
            try:
                stdout, _ = process.communicate(canonical(envelope) + b"\n", timeout=record["timeout_s"])
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            result = subprocess.CompletedProcess(record["argv"], process.returncode, stdout)
            lines = result.stdout.decode("utf-8").splitlines()
            toast = json.loads(lines[0]) if len(lines) == 1 else {"toast": "", "alert": False}
            if (len(lines) > 1 or not isinstance(toast, dict)
                    or sorted(toast) != ["alert", "toast"] or not isinstance(toast["toast"], str)
                    or len(toast["toast"]) > 200 or type(toast["alert"]) is not bool
                    or (result.returncode == 0 and toast["alert"])):
                raise ValueError("invalid callback response")
            fallback = "stale" if result.returncode == 3 else "failed" if result.returncode else None
            if result.returncode not in (0, 3, 4):
                toast = {"toast": "", "alert": False}
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            result = None
            toast, fallback = {"toast": "", "alert": False}, "failed"
        with chat_lock(root, record["envelope"]["chat_id"]):
            record = read_json(path)
            # Preserve checkpoints written by the trusted shim while the child ran.
            if result is None or result.returncode not in (0, 3, 4):
                record["toast"] = {"toast": "", "alert": False, "fallback": "failed"}
            elif record["toast"] is None:
                record["toast"] = dict(toast, fallback=fallback)
            record["child_rc"] = result.returncode if result else None
            record["child_returned"] = True
            if record["dispatch_state"] == "launching":
                record["dispatch_state"] = "delivery_pending"
            atomic_json(path, record)
            return record


class RecoveryAuthority:
    """Bind the installed trusted recovery service to authenticated ingress.

    The service performs kit command_lookup first and owns exact-command
    replay/receipt import. It must keep unknown/in-flight transactions pending
    and use the rev8 invocation proof, never infer abort from process death.
    Its authority comes from M1's process-local service provider, not stdin.
    """
    def __init__(self, root, service):
        self.root = Path(root)
        self.service = service

    def recover_original(self, record, path):
        envelope = authenticated(self.root, record["envelope"])
        if (Path(path) != ingress_path(self.root, envelope["chat_id"], envelope["envelope_id"])
                or digest(envelope) != record["envelope_digest"]
                or digest(record["route"]) != record["route_digest"]
                or digest(record["config"]) != record["config_digest"]
                or record["principal"] not in record["config"]["identities"]
                or (record["principal"]["chat_id"], record["principal"]["user_id"]) !=
                   (envelope["chat_id"], envelope["user_id"])):
            raise Refusal(17, "recovery ingress authority changed")
        if not self.service.authorize_ingress_recovery(record):
            raise Refusal(17, "original recovery binding unavailable")
        # A crash before command binding cannot be repaired by resolving a new
        # command here. The kit service may look up an original committed bind.
        return self.service.recover_original(record, path)


def recover(root, path, authority):
    """No fresh resolver invocation after an ambiguous initial launch."""
    path = Path(path)
    with locked(path.with_suffix(".run.lock"), blocking=False):
        record = read_json(path)
        # The publisher owns command_lookup and exact-command idempotent replay.
        # It must validate activation/close/cancel and old transaction authority.
        return authority.recover_original(record, path)


@dataclass(frozen=True)
class ToastResult:
    handled: bool
    toast: str = ""
    alert: bool = False
    rc: int = 0


class Router:
    def __init__(self, root, failed="", stale=""):
        self.root, self.failed, self.stale = Path(root), failed, stale

    def claimed(self, data):
        """Failure-only prefix hints never authorize command execution."""
        if not isinstance(data, str):
            return False
        for path in (self.root / "config/packs").glob("*.receipt"):
            try:
                lines = path.read_text().splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                if line.startswith("cap.session_flow_v2.routes="):
                    prefixes = line.partition("=")[2].split(",")
                    if any(PREFIX.fullmatch(prefix) and data.startswith(prefix) for prefix in prefixes):
                        return True
            if data.startswith("flow|") and "cap.session_flow_v2.retire_prefixes=flow|" in lines:
                from bridge.session_activation import load_activation
                try:
                    current = load_activation(self.root, path.name[:-8])
                    if current["phase"] in ("activating", "active", "deactivating"):
                        return True
                except Refusal:
                    return True
        return False

    def handles(self, data):
        if not isinstance(data, str):
            return False
        try:
            return locate(self.root, data) is not None
        except (Refusal, OSError, ValueError):
            return self.claimed(data)

    async def _answer_once(self, update, transport, text, alert, ingress=None):
        query, chat = update.callback_query, update.effective_chat.id
        answer_path = data_dir(self.root) / "callback-answers" / str(chat) / (hashlib.sha256(str(query.id).encode()).hexdigest() + ".json")
        identity = digest({"chat": chat, "user": update.effective_user.id, "data": query.data})
        with chat_lock(self.root, chat):
            if answer_path.exists():
                if read_json(answer_path)["identity"] != identity:
                    raise Refusal(22, "callback identity changed")
                return
            record = read_json(ingress) if ingress is not None and ingress.exists() else None
            if record and record["callback_answer"] != "unsent":
                return
            expired = record is not None and now_ms() - record["envelope"]["issued_at_ms"] > 120000
            answer = {"identity": identity, "text": text, "alert": alert,
                      "state": "unknown" if expired else "attempted"}
            atomic_json(answer_path, answer)
            if record:
                record["callback_answer"] = answer["state"]
                atomic_json(ingress, record)
        if expired:
            return
        try:
            await transport.answer(text=text, show_alert=alert)
            answer["state"] = "confirmed"
        except Exception:
            answer["state"] = "unknown"
        with chat_lock(self.root, chat):
            atomic_json(answer_path, answer)
            if record:
                current = read_json(ingress)
                current["callback_answer"] = answer["state"]
                atomic_json(ingress, current)

    async def route_v2(self, update_like, transport=None):
        query = update_like.callback_query
        transport = transport or query
        path = None
        try:
            pack = locate(self.root, query.data)
            if pack is None:
                return ToastResult(False)
            from bridge.session_activation import require_active
            require_active(self.root, pack)
            if query.data.startswith("flow|"):
                await self._answer_once(update_like, transport, self.stale, False)
                return ToastResult(True, self.stale, rc=3)
            route = installed(self.root, pack)
            row = next(r for r in route["routes"] if query.data.startswith(r["prefix"]))
            chat, user = update_like.effective_chat.id, update_like.effective_user.id
            op = "tap:" + str(query.id)
            path = ingress_path(self.root, chat, op)
            if path.exists():
                record = read_json(path)
                env = record["envelope"]
                if (env["user_id"] != user or env["payload"] != query.data[len(row["prefix"]):]
                        or env.get("message_id") != query.message.message_id):
                    raise Refusal(22, "duplicate callback changed identity")
            else:
                surface_path = data_dir(self.root) / (str(chat) + ".json")
                surface = read_json(surface_path) if surface_path.exists() else None
                if surface is None or surface["state"] != "open":
                    raise Refusal(10, "no active surface")
                date = getattr(query.message, "date", None)
                env = sign(self.root, {"v": 1, "kind": "tap", "envelope_id": op,
                           "prefix": row["prefix"], "payload": query.data[len(row["prefix"]):],
                           "chat_id": chat, "user_id": user,
                           "message_id": query.message.message_id,
                           "message_date_ms": int(date.timestamp() * 1000) if date else None,
                           "surface": {k: surface[k] for k in ("surface_id", "epoch", "slots")},
                           "redelivery": False, "issued_at_ms": now_ms(), "nonce": secrets.token_hex(16)})
                admit(self.root, pack, env, row["argv"], row.get("timeout_s", 8))
            record = await asyncio.to_thread(launch, self.root, path)
            result = record["toast"] or {"toast": "", "alert": False, "fallback": "failed"}
            text = result["toast"] or (self.stale if result.get("fallback") == "stale" else
                                       self.failed if result.get("fallback") == "failed" else "")
            await self._answer_once(update_like, transport, text, result["alert"], path)
            return ToastResult(True, text, result["alert"])
        except (Refusal, OSError, ValueError, KeyError, StopIteration) as exc:
            text = self.stale if getattr(exc, "rc", 0) == 10 else self.failed
            try:
                await self._answer_once(update_like, transport, text, False, path)
            except (Refusal, OSError, ValueError, KeyError):
                # Storage failure cannot authorize an uncheckpointed answer.
                pass
            return ToastResult(True, text, rc=getattr(exc, "rc", 40))


async def route_v2(update_like, transport):
    """Replay seam: transport supplies root, generic copy and answer()."""
    return await Router(transport.root, transport.failed, transport.stale).route_v2(update_like, transport)


def issue_turn(root, update_like, text):
    """Called after access/secret intake; inactive instances perform no writes."""
    if not data_dir(root).exists():
        return None
    chat, user = update_like.effective_chat.id, update_like.effective_user.id
    surface_path = data_dir(root) / (str(chat) + ".json")
    if not surface_path.exists():
        return None
    surface = read_json(surface_path)
    if surface["state"] != "open":
        return None
    from bridge.session_activation import require_active
    require_active(root, surface["owner"])
    op = "turn:" + str(chat) + ":" + str(update_like.message.message_id)
    path = ingress_path(root, chat, op)
    if path.exists():
        record = read_json(path)
        if record["owner_text"] != text or record["envelope"]["user_id"] != user:
            raise Refusal(22, "owner turn identity changed")
        return record["envelope"]
    envelope = sign(root, {"v": 1, "kind": "turn", "envelope_id": op,
                          "chat_id": chat, "user_id": user,
                          "message_id": update_like.message.message_id,
                          "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                          "surface": {k: surface[k] for k in ("surface_id", "epoch", "slots")},
                          "issued_at_ms": now_ms(), "nonce": secrets.token_hex(16)})
    admit(root, surface["owner"], envelope, None, 0, owner_text=text)
    return envelope


def turn_envelope(root, envelope_id):
    match = re.fullmatch(r"turn:(-?[0-9]+):[0-9]+", envelope_id)
    if match is None:
        raise Refusal(2, "invalid owner turn id")
    path = ingress_path(root, int(match[1]), envelope_id)
    if not path.exists():
        raise Refusal(2, "unknown owner turn")
    record = read_json(path)
    if record["envelope"]["kind"] != "turn":
        raise Refusal(2, "not an owner turn")
    return authenticated(root, record["envelope"])


def poll_ingress(root, recovery=None):
    """Startup recovery never depends on Telegram delivering an update again."""
    directory = data_dir(root) / "ingress"
    if not directory.exists():
        return
    for path in sorted(directory.glob("*/*.json")):
        record = read_json(path)
        authenticated(root, record["envelope"])
        # Due groups own launch, cancellation and the bounded recovery budget.
        # The generic poller must not retry them before/after that budget.
        if record["envelope"]["kind"] == "due":
            continue
        if record["dispatch_state"] == "accepted" and record["argv"] is not None:
            try:
                launch(root, path)
            except Refusal as exc:
                if exc.rc not in (10, 24, 35):
                    raise
        elif record["dispatch_state"] not in ("complete", "accepted") and recovery is not None:
            recover(root, path, recovery)
