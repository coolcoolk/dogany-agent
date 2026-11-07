#!/usr/bin/env python3
"""Provision and diagnose runtime tools declared by installed product.yaml units.

The deliberately small YAML surface is::

    requires:
      runtime_tools:
        - name: agent-browser
          command: agent-browser
          install: npm install -g agent-browser && agent-browser install
          paths:
            - ~/.local/bin/agent-browser

``command`` defaults to ``name``.  A tool is present when that command is on
PATH or any declared path exists and is executable.  This parser is kept
dependency-free because it runs before an instance virtualenv is available.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


FIELDS = {"name", "command", "install", "paths"}


def _value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def read_requirements(manifest):
    """Read only requires.runtime_tools, ignoring every other YAML surface."""
    tools, current = [], None
    in_requires = in_tools = in_paths = False
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        text = raw.split(" #", 1)[0].rstrip()
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        indent = len(text) - len(text.lstrip(" "))
        body = text.strip()
        if indent == 0:
            in_requires = body == "requires:"
            in_tools = in_paths = False
            continue
        if not in_requires:
            continue
        if indent == 2:
            in_tools = body == "runtime_tools:"
            in_paths = False
            continue
        if not in_tools:
            continue
        if indent == 4 and body.startswith("- "):
            if current and current.get("name"):
                tools.append(current)
            current, in_paths = {"paths": []}, False
            body = body[2:].strip()
            if ":" in body:
                key, value = body.split(":", 1)
                if key in FIELDS:
                    current[key] = _value(value)
            continue
        if current is None:
            continue
        if indent == 6 and ":" in body:
            key, value = body.split(":", 1)
            in_paths = key == "paths"
            if key in FIELDS and key != "paths":
                current[key] = _value(value)
            continue
        if in_paths and indent >= 8 and body.startswith("- "):
            current["paths"].append(_value(body[2:]))
    if current and current.get("name"):
        tools.append(current)
    return tools


def discover(root):
    found = {}
    for manifest in sorted(Path(root).rglob("product.yaml")):
        if any(part in {".git", ".worktrees"} for part in manifest.parts):
            continue
        for tool in read_requirements(manifest):
            name = tool["name"]
            if name in found and found[name] != tool:
                raise ValueError("conflicting runtime tool declaration: %s" % name)
            found[name] = tool
    return list(found.values())


def present(tool):
    command = tool.get("command") or tool["name"]
    if shutil.which(command):
        return True
    for raw in tool.get("paths", []):
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        # Kits may name either the executable itself or a search directory
        # (the latter mirrors scripts such as route.sh that extend PATH).
        candidate = path / command if path.is_dir() else path
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return True
    return False


def report_missing(tool):
    install = tool.get("install", "").strip()
    fix = install or "install %s and put it on PATH" % tool["name"]
    print("runtime tool missing: %s; fix: %s" % (tool["name"], fix), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("provision", "doctor"))
    parser.add_argument("root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    try:
        tools = discover(args.root)
    except (OSError, UnicodeError, ValueError) as exc:
        print("runtime tool declarations invalid: %s" % exc, file=sys.stderr)
        return 2
    for tool in tools:
        if present(tool):
            continue
        install = tool.get("install", "").strip()
        if args.mode == "provision" and install and not args.dry_run:
            print("[tools] Installing required runtime tool: %s" % tool["name"])
            if subprocess.call(["/bin/sh", "-c", install]) == 0 and present(tool):
                continue
        if args.mode == "provision" and args.dry_run and install:
            print("[dry-run] Would install required runtime tool %s: %s" %
                  (tool["name"], install))
            continue
        report_missing(tool)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
