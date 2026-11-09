#!/usr/bin/env python3
"""Retire explicitly listed framework files and their exact hook registrations.

The manifest contains old instance-relative paths, not basename patterns. Bundle
and custom hooks therefore remain owned by their installers. Files with unknown
contents are retained; settings keep all unrelated values and hook metadata.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile


def retired_command(command, root, paths):
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    roots = {str(Path(root).absolute()), str(Path(root).resolve()),
             "__PROJECT_ROOT__", "$PROJECT_ROOT", "${PROJECT_ROOT}"}
    exact = {str(Path(prefix) / path) for prefix in roots for path in paths}
    exact.update(paths)
    exact.update("./" + path for path in paths)
    # Match an executable or an interpreter's script, never an echo/comment
    # whose data happens to name the retired path.
    while tokens and ("=" in tokens[0] or Path(tokens[0]).name == "env"):
        tokens.pop(0)
    if not tokens:
        return False
    if tokens[0] in exact:
        return True
    interpreter = Path(tokens[0]).name
    if interpreter in ("bash", "sh", "zsh", "python", "python3", "python3.11", "python3.12", "python3.13"):
        args = tokens[1:]
        if "-c" in args or "-m" in args:
            return False
        return any(token in exact for token in args)
    return False


def clean_settings(document, root, paths):
    result = copy.deepcopy(document)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            if retired_command(entry.get("command"), root, paths):
                continue
            children = entry.get("hooks")
            if isinstance(children, list):
                remaining = [hook for hook in children if not (
                    isinstance(hook, dict) and
                    retired_command(hook.get("command"), root, paths))]
                if children and not remaining:
                    continue
                entry["hooks"] = remaining
            kept.append(entry)
        hooks[event] = kept
    return result


def atomic_json(path, document):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".new.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def retire(root, manifest, apply=False, preserved=()):
    root = Path(root)
    paths = manifest["files"]
    for relative, record in paths.items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or relative.startswith("routines/bundle/"):
            raise ValueError("retirement must name an old framework path")
        if not isinstance(record.get("sha256"), list):
            raise ValueError("retirement requires an explicit hash list")
    hooks = [path for path, record in paths.items() if record.get("hook")]
    edits = []
    # Parse both documents before changing either. Malformed local settings
    # must not be erased or bypassed during a framework settings refresh.
    for relative in (".claude/settings.json", ".claude/settings.local.json"):
        target = root / relative
        if relative in preserved or not target.exists():
            continue
        if target.is_symlink():
            raise ValueError("refusing to rewrite symlinked settings: " + relative)
        document = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("settings must be an object: " + relative)
        cleaned = clean_settings(document, root, hooks)
        if cleaned != document:
            edits.append((relative, target, cleaned))
    actions = []
    for relative, target, cleaned in edits:
        actions.append("settings\t" + relative)
        if apply:
            atomic_json(target, cleaned)
    for relative, record in paths.items():
        target = root / relative
        if relative in preserved or not target.is_file() or target.is_symlink():
            continue
        if hashlib.sha256(target.read_bytes()).hexdigest() not in record["sha256"]:
            actions.append("kept-modified\t" + relative)
            continue
        actions.append("remove\t" + relative)
        if apply:
            target.unlink()
    return actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preserve", action="append", default=[])
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    for action in retire(args.root, manifest, args.apply, args.preserve):
        print(action)


if __name__ == "__main__":
    main()
