#!/usr/bin/env python3
"""Set or clear a persistent framework version pin with its reason (DGN-1447).

Usage: python3 routines/update-pin.py --root INSTANCE set TAG --reason TEXT
       python3 routines/update-pin.py --root INSTANCE clear

Only records intent; does not resolve tags, update code, or restart services.
Existing pins without reasons are grandfathered by update readers. New pins
and replacements through this command always require a nonblank reason.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

KEYS = ("DOGANY_UPDATE_PIN", "DOGANY_UPDATE_PIN_REASON_TAG", "DOGANY_UPDATE_PIN_REASON")


def write_pin(root, tag, reason):
    if tag is not None:
        # A conservative, shell-safe framework tag spelling; existence is the
        # updater's existing responsibility, including unpublished-tag guards.
        if not re.fullmatch(r"v?[0-9][0-9A-Za-z.+_-]*", tag):
            raise ValueError("pin must be a framework version tag")
        if not reason or not reason.strip():
            raise ValueError("a nonblank --reason is required to set or replace a pin")
        if any(ord(c) < 32 or ord(c) == 127 for c in reason):
            raise ValueError("reason must be one line without control characters")
    conf = root / ".instance.conf"
    if conf.is_symlink() or not conf.is_file():
        raise ValueError("instance must have a regular, non-symlink .instance.conf")
    before = conf.read_text(encoding="utf-8")
    mode = stat.S_IMODE(conf.stat().st_mode)
    # Drop duplicate owned records as well; readers consistently take the first.
    lines = [line for line in before.splitlines(keepends=True)
             if line.split("=", 1)[0] not in KEYS]
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    text += "DOGANY_UPDATE_PIN=" + (tag or "") + "\n"
    if tag is not None:
        text += "DOGANY_UPDATE_PIN_REASON_TAG=" + tag + "\n"
        # Conf is also sourced by legacy callers. Shell quote, never interpolate
        # raw reason text into executable config. Readers display it as data.
        text += "DOGANY_UPDATE_PIN_REASON=" + shlex.quote(reason.strip()) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".instance.conf.pin-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if conf.read_text(encoding="utf-8") != before:
            raise ValueError("instance config changed during pin edit; retry")
        os.replace(temporary, conf)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest="command", required=True)
    setting = commands.add_parser("set", help="record a pin and mandatory reason")
    setting.add_argument("tag")
    setting.add_argument("--reason", required=True)
    commands.add_parser("clear", help="remove the pin and its reason together")
    args = parser.parse_args()
    try:
        write_pin(args.root, args.tag if args.command == "set" else None,
                  args.reason if args.command == "set" else None)
    except (OSError, ValueError) as exc:
        print("[update-pin] refused: " + str(exc), file=sys.stderr)
        return 2
    print("[update-pin] " + ("pin and reason recorded" if args.command == "set" else "pin cleared"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
