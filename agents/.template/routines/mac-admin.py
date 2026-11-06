#!/usr/bin/env python3
"""Terminal-free macOS maintenance. Never accept, capture, or print passwords.

Usage: mac-admin.py restart-fseventsd
       mac-admin.py -- /absolute/command [argument ...]
Only the exact fixed whitelist gets non-interactive sudo. All other commands,
and unavailable whitelist grants, go through the native administrator dialog.
Child output is intentionally discarded: report only the route and outcome.
"""
import os
import shlex
import subprocess
import sys

WHITELIST = (("/usr/bin/killall", "fseventsd"),)


def emit(line):
    print(line, flush=True)


def run(argv, script=None):
    try:
        return subprocess.run(
            argv, input=script, stdin=subprocess.DEVNULL if script is None else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, check=False,
        ).returncode
    except OSError:
        return 127


def main(args):
    if os.uname().sysname != "Darwin":
        emit("status=not-supported platform=non-macOS")
        return 3
    if args == ["restart-fseventsd"]:
        command = WHITELIST[0]
    elif len(args) > 1 and args[0] == "--" and os.path.isabs(args[1]):
        command = tuple(args[1:])
    else:
        emit("status=invalid-request")
        return 2
    if any("\x00" in arg for arg in command):
        emit("status=invalid-request")
        return 2

    emit("attempt=whitelist")
    if command in WHITELIST and run(["sudo", "-n", "--", *command]) == 0:
        emit("status=completed route=whitelist")
        return 0

    emit("attempt=dialog")
    # Two separate quoting layers: shell argv, then an AppleScript string.
    shell = " ".join(shlex.quote(arg) for arg in command)
    literal = shell.replace("\\", "\\\\").replace('"', '\\"')
    literal = literal.replace("\n", "\\n").replace("\r", "\\r")
    script = 'do shell script "' + literal + '" with administrator privileges\n'
    if run(["osascript", "-"], script) == 0:
        emit("status=completed route=dialog")
        return 0

    emit("attempt=report")
    emit("status=needs-attention reason=admin-dialog-cancelled-or-unavailable-or-command-failed")
    emit("guidance=Report the failed maintenance in chat; never request a password or terminal command.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
