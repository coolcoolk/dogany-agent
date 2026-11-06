"""Validate the installer argv-valued agent.conf fragment and merged config."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import sys


def declared_keys(manifest, known):
    keys = json.loads(manifest.read_text()).get("argv_conf_keys", [])
    if (not isinstance(keys, list)
            or any(not isinstance(k, str) or k not in known for k in keys)):
        raise ValueError("argv_conf_keys must be an array of framework argv conf keys: "
                         + ", ".join(known))
    return set(keys)


def read_bindings(path, known):
    text = path.read_text() if path.is_file() else ""
    # No declaration or assignment means no new dependency for existing packs.
    if not any(re.search(r"(?m)^\s*(?:export\s+)?'?" + re.escape(k)
                         + r"'?\s*(?:=|$)", text) for k in known):
        return []
    from dotenv.parser import parse_stream

    records = []
    owner = None
    for b in parse_stream(io.StringIO(text)):
        # Markers are only recognized in comment bindings, never in a value.
        if b.key is None and not b.error:
            for line in b.original.string.splitlines():
                m = re.fullmatch(r"# DOGANY-PACK:(.+):(BEGIN|END)", line)
                if m:
                    owner = m[1] if m[2] == "BEGIN" else None
        key = b.key
        if b.error:
            for k in known:
                if re.search(r"(?m)^\s*(?:export\s+)?'?" + re.escape(k)
                             + r"'?\s*=", b.original.string):
                    key = k
                    break
        if key in known:
            # parse_stream's original can start with leading blank lines.
            offset = len(b.original.string) - len(b.original.string.lstrip())
            line = b.original.line + b.original.string[:offset].count("\n")
            records.append((key, b.value, line, owner, b.error))
    return records


def argv_value(key, value, location):
    try:
        argv = json.loads(value or "null")
    except ValueError:
        argv = None
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(s, str) or not s or "\x00" in s for s in argv)):
        raise ValueError(f"{key} must be a JSON argv ({location})")
    return argv


def payload_file(value, root, payload):
    path = Path(value)
    target = path if path.is_absolute() else root / path
    try:
        relative = target.resolve().relative_to(root.resolve())
        candidate = payload / relative
        candidate.resolve().relative_to(payload.resolve())
    except ValueError:
        return None
    return candidate


def resolvable(key, argv, root, payload, location):
    command = payload_file(argv[0], root, payload)
    if command is not None and command.is_file() and os.access(command, os.X_OK):
        installed = root / command.relative_to(payload)
        if installed.is_file() and os.access(installed, os.X_OK):
            return
    # Absolute interpreters may execute a non-executable payload script.
    interpreter = Path(argv[0])
    if interpreter.is_absolute() and interpreter.is_file() and os.access(interpreter, os.X_OK):
        script = payload_file(argv[1], root, payload) if len(argv) > 1 else None
        if script is not None and script.is_file():
            if (root / script.relative_to(payload)).is_file():
                return
    raise ValueError(f"{key} argv is not resolvable from the installed payload ({location})")


def validate(args):
    known = args.keys
    required = declared_keys(args.manifest, known)
    body = read_bindings(args.body, known)
    selected = required | {r[0] for r in body}
    if not selected:
        return
    original = read_bindings(args.source, known)
    for key in sorted(selected):
        records = [r for r in body if r[0] == key]
        if not records:
            raise ValueError(f"{key} must be a JSON argv (agent.conf.add missing declared key)")
        source_lines = [r[2] for r in original if r[0] == key]
        for index, (_, value, line, _, error) in enumerate(records):
            source_line = source_lines[index] if index < len(source_lines) else line
            location = f"agent.conf.add line {source_line}"
            argv = argv_value(key, None if error else value, location)
            resolvable(key, argv, args.root, args.payload, location)
    merged = read_bindings(args.conf, known)
    for key, _, line, owner, _ in merged:
        if key in selected and owner is not None and owner != args.pack:
            raise ValueError(f"{key} is already defined in pack {owner}'s block "
                             f"(agent.conf line {line})")
    if args.phase == "fragment":
        return
    for key in sorted(selected):
        records = [r for r in merged if r[0] == key]
        if not records:
            raise ValueError(f"{key} must be a JSON argv (agent.conf missing declared key)")
        winner = records[-1]
        for _, _, line, owner, _ in records:
            if owner is None:
                print(f"WARN: {key} outside a pack block at agent.conf line {line}; "
                      f"dotenv last-value wins: agent.conf line {winner[2]} value={winner[1]}")
        argv = argv_value(key, None if winner[4] else winner[1],
                          f"agent.conf line {winner[2]}")
        resolvable(key, argv, args.root, args.payload, f"agent.conf line {winner[2]}")
        print(f"argv_key.{key}=" + json.dumps(argv, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fragment", "merged"))
    for name in ("manifest", "body", "source", "conf", "root", "payload"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pack", required=True)
    parser.add_argument("keys", nargs="+")
    args = parser.parse_args()
    try:
        validate(args)
    except (OSError, ValueError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
