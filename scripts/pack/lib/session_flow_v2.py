"""Installer validation/staging for the frozen session_flow_v2 capability.

Capability-less manifests are an exact no-op. Runtime attestation is supplied by
the installed live-surface implementation, never reconstructed with a different
artifact encoder in the installer.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

# Canonical installer and installed instance both contain the bridge package.
REPO = Path(__file__).resolve().parents[3]
BRIDGE_ROOT = (REPO / "agents/.template").resolve()


def _require_own_bridge():
    """Hard-refuse unless 'bridge' resolves from REPO/agents/.template only."""
    import importlib.util
    sys.path.insert(0, str(BRIDGE_ROOT))
    try:
        spec = importlib.util.find_spec("bridge")
    except (ImportError, ValueError):
        spec = None
    origin = getattr(spec, "origin", None)
    try:
        inside = origin is not None and Path(origin).resolve().is_relative_to(BRIDGE_ROOT)
    except (OSError, ValueError):
        inside = False
    if not inside:
        sys.stderr.write("session_flow_v2: refused: bridge package must resolve from %s "
                         "(found: %s)\n" % (BRIDGE_ROOT, origin or "none"))
        raise SystemExit(2)


_require_own_bridge()
from bridge import callback_routes
from bridge.session_activation import activation_path, load_activation
from bridge.session_state import (Refusal, adapter_config, atomic_bytes, atomic_json,
                                  canonical, contained, digest, locked, pack_name,
                                  read_json)
from argv_key_validate import argv_value, read_bindings, resolvable
from semver_range import satisfies


FIELDS = {"protocol", "live_surface_api", "callback_route_api", "due_event_api",
          "routes", "due_argv", "probe_argv", "env", "retire_prefixes"}


def declaration(manifest):
    caps = manifest.get("capabilities", {})
    if not isinstance(caps, dict):
        raise Refusal(2, "capabilities must be an object")
    if "session_flow_v2" not in caps:
        return None
    value = caps["session_flow_v2"]
    if not isinstance(value, dict) or value.keys() - FIELDS:
        raise Refusal(2, "session_flow_v2 must be an object with frozen fields")
    for key in ("protocol", "live_surface_api", "callback_route_api", "due_event_api"):
        if type(value.get(key)) is not int or value[key] not in callback_routes.APIS[key]:
            raise Refusal(31, "framework too old for api " + key + "=" + str(value.get(key)))
    pack = pack_name(manifest.get("id") or manifest.get("name"))
    return {"schema": "callback_routes/1", "pack": pack,
            **{k: v for k, v in value.items() if k not in callback_routes.APIS}}


def validate(manifest, payload, root=None, version=None, installed_files=False):
    route = declaration(manifest)
    if route is None:
        return None
    payload = Path(payload)
    callback_routes.validate_route(route, payload, check_files=False)
    for argv in [r["argv"] for r in route["routes"]] + [route["due_argv"], route["probe_argv"]]:
        argv_value("session_flow_v2", json.dumps(argv), "manifest")
        # F0 validation against the payload at preflight, against delivered
        # instance files immediately before recording installed authority.
        resolvable("session_flow_v2", argv, payload, payload, "manifest")
        if root is not None and installed_files:
            resolvable("session_flow_v2", argv, Path(root), payload, "installed manifest")
    for key, rel in route.get("env", {}).items():
        path = payload / rel
        if not path.is_file() and root is not None:
            path = contained(root, rel)
        else:
            path = contained(payload, rel)
        if key == "LIVE_ADAPTER_CONFIG":
            adapter_config(read_json(path))
    if "LIVE_ADAPTER_CONFIG" not in route.get("env", {}):
        raise Refusal(16, "session_flow_v2 requires LIVE_ADAPTER_CONFIG")
    if version is not None and satisfies(version, manifest.get("requires_framework", "")) is not True:
        raise Refusal(31, "requires_framework is not satisfied")
    if root is not None:
        root = Path(root)
        current = load_activation(root, route["pack"])
        if current["phase"] not in ("staged", "legacy"):
            raise Refusal(35, "finish deactivation before reinstall/uninstall")
        for other in callback_routes.candidates(root):
            if other == route["pack"]:
                continue
            rows = callback_routes.receipt(root, other)
            foreign = rows.get("cap.session_flow_v2.routes", "").split(",")
            if any(r["prefix"] in foreign for r in route["routes"]):
                raise Refusal(2, "callback prefix already owned by another pack")
        if route.get("retire_prefixes"):
            bindings = read_bindings(root / "config/agent.conf", ["FLOW_TAP_COMMAND"])
            if any(b[3] != route["pack"] for b in bindings):
                raise Refusal(2, "cannot retire a foreign or owner-configured legacy command")
    return route


def installed_capabilities(root):
    root = Path(root)
    python = root / "bridge/venv/bin/python"
    interpreter = str(python) if python.is_file() else sys.executable
    try:
        result = subprocess.run([interpreter, str(root / "bridge/live_surface.py"), "capabilities", "--root", str(root)],
                                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=False)
        if result.returncode:
            raise Refusal(31, "live-surface capability attestation unavailable")
        value = json.loads(result.stdout)
        if (any(n not in value.get(k, []) for k, values in callback_routes.APIS.items() for n in values)
                or any(f not in value.get("features", []) for f in
                       ("edit_acknowledgement", "delivery_receipt_reconciliation"))
                or sorted(value.get("artifacts", {})) != ["framework", "installer"]
                or any(not isinstance(v, str) or not v.startswith("sha256:") or len(v) != 71
                       for v in value["artifacts"].values())):
            raise Refusal(31, "live-surface API/artifact attestation invalid")
        return value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Refusal(31, "live-surface capability attestation unavailable") from exc


def record(manifest, payload, root, capabilities=None):
    if declaration(manifest) is None:
        return
    root = Path(root)
    caps = capabilities or installed_capabilities(root)
    pack = declaration(manifest)["pack"]
    path = root / "config/packs" / (pack + ".receipt")
    with locked(root / "config/packs/.callback_routes.lock"), locked(activation_path(root, pack).with_suffix(".lock")):
        route = validate(manifest, payload, root, caps["framework"], installed_files=True)
        current = load_activation(root, pack)
        if current["phase"] not in ("staged", "legacy"):
            raise Refusal(35, "deactivation required before staging capability")
        prior = path.read_text() if path.exists() else ""
        lines = [line for line in prior.splitlines() if not line.startswith(
            ("cap.session_flow_v2", "framework_version=", "framework_artifacts=", "installer="))]
        rows = {"cap.session_flow_v2": callback_routes.CAP_LINE,
                "cap.session_flow_v2.routes": ",".join(r["prefix"] for r in route["routes"]),
                "cap.session_flow_v2.retire_prefixes": ",".join(route.get("retire_prefixes", [])),
                "cap.session_flow_v2.route_digest": digest(route),
                "framework_version": caps["framework"],
                "framework_artifacts": caps["artifacts"]["framework"],
                "installer": caps["artifacts"]["installer"]}
        for key in ("due_argv", "probe_argv"):
            rows["cap.session_flow_v2." + key] = canonical(route[key]).decode("utf-8")
        # No cross-file atomicity: all readers compare receipt digest/argv.
        # The activation authority is published last; interruption stays legacy.
        atomic_json(root / "config/packs" / (pack + ".callback_routes.json"), route)
        atomic_bytes(path, ("\n".join(lines + [k + "=" + v for k, v in rows.items()]) + "\n").encode())
        for rel in route.get("env", {}).values():
            if rel == route["env"].get("LIVE_ADAPTER_CONFIG"):
                adapter_path = contained(root, rel)
                atomic_json(adapter_path, adapter_config(read_json(adapter_path)))
        current.update(schema="session_activation/1", generation=max(1, current["generation"]),
                       phase="staged", selected_engine="legacy", route_digest=digest(route),
                       transition_checkpoint=None)
        atomic_json(activation_path(root, pack), current)


def guard_pack(root, pack):
    if load_activation(root, pack_name(pack))["phase"] not in ("legacy", "staged"):
        raise Refusal(35, "deactivation required before modifying installed pack")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=("validate", "record", "guard", "guard-pack"))
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("payload", type=Path, nargs="?")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--pack")
    args = parser.parse_args(argv)
    try:
        if args.verb == "guard-pack":
            if args.root is None or args.pack is None:
                raise Refusal(2, "guard-pack requires --root and --pack")
            guard_pack(args.root, args.pack)
            return 0
        if args.manifest is None or args.payload is None:
            raise Refusal(2, "manifest and payload are required")
        manifest = read_json(args.manifest)
        if args.verb == "guard":
            if args.root is None:
                raise Refusal(2, "guard requires an instance root")
            guard_pack(args.root, args.pack or manifest.get("id") or manifest.get("name"))
            return 0
        if declaration(manifest) is None:
            return 0
        if args.verb == "record":
            if args.root is None:
                raise Refusal(2, "record requires an instance root")
            record(manifest, args.payload, args.root)
        else:
            validate(manifest, args.payload, args.root, args.version)
        return 0
    except (Refusal, ValueError, OSError) as exc:
        print("session_flow_v2: " + str(exc), file=sys.stderr)
        return getattr(exc, "rc", 2)


if __name__ == "__main__":
    sys.exit(main())
