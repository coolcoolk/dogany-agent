#!/usr/bin/env python3
"""Installed driver protocol v1; no provider selection or import-time I/O."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

API = "framework-driver/1"
ENTRY_NAME = re.compile(r"[a-z][a-z0-9-]{0,99}")
__all__ = ("API", "resolve", "DriverError")


class DriverError(ValueError):
    """Required installed provider is missing, stale, or unsupported."""


def _contained(base, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise DriverError("expected a contained relative path")
    if any(part in (".", "..") for part in value.split("/")):
        raise DriverError("non-canonical driver path")
    path = (base / value).resolve()
    if path == base or base not in path.parents:
        raise DriverError("driver path escapes installed payload")
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve(root, pack, entry, *, required=True):
    """Return a verified executable or None for an absent optional descriptor.

    Only absence is optional. A broken registration always refuses. Descriptors
    are staged by the pack installer; this reader never writes or activates one.
    """
    if not isinstance(pack, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", pack):
        raise DriverError("invalid pack id")
    if not isinstance(entry, str) or not ENTRY_NAME.fullmatch(entry):
        raise DriverError("unknown driver entry")
    root = Path(root).resolve()
    descriptor = root / "config/packs" / (pack + ".drivers.json")
    try:
        try:
            descriptor.lstat()
        except FileNotFoundError:
            if not required:
                return None
            raise DriverError("required pack driver descriptor absent")
        descriptor = _contained(root, "config/packs/" + pack + ".drivers.json")
        doc = json.loads(descriptor.read_text())
        if not isinstance(doc, dict) or doc.get("api") != API or doc.get("pack") != pack:
            raise DriverError("unsupported driver API or pack identity")
        records = doc.get("entries")
        if not isinstance(records, dict) or not records or any(not isinstance(key, str) or not ENTRY_NAME.fullmatch(key) for key in records) or entry not in records:
            raise DriverError("required entry not registered")
        marker = root / "config/packs" / (pack + ".receipt.inprogress")
        try:
            marker.lstat()
        except FileNotFoundError:
            pass
        else:
            raise DriverError("pack installation is incomplete")
        receipt = _contained(root, "config/packs/" + pack + ".receipt")
        if _digest(receipt) != doc.get("receipt_sha256"):
            raise DriverError("driver receipt digest mismatch")
        fields, anchors = {}, []
        for line in receipt.read_text().splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                raise DriverError("malformed install receipt")
            if key == "anchor":
                anchors.append(value)
            else:
                if key in fields:
                    raise DriverError("duplicate install receipt field")
                fields[key] = value
        if (fields.get("schema") != "1" or fields.get("pack") != pack
                or fields.get("status") != "delivery-complete"
                or not fields.get("version") or fields["version"] != doc.get("version")
                or not fields.get("exec") or fields["exec"] != doc.get("generation")
                or fields.get("anchor_count") != str(len(anchors))):
            raise DriverError("incompatible, truncated or stale install receipt")
        for anchor in anchors:
            if not _contained(root, anchor).exists():
                raise DriverError("installed receipt anchor missing")
        payload = _contained(root, doc.get("payload_root"))
        namespace = _contained(root, "service/" + pack)
        if namespace not in payload.parents:
            raise DriverError("payload must belong to installed pack service")
        record = records[entry]
        if not isinstance(record, dict):
            raise DriverError("malformed driver entry")
        target = _contained(payload, record.get("path"))
        if not target.is_file() or not os.access(target, os.X_OK):
            raise DriverError("driver executable missing or not executable")
        if _digest(target) != record.get("sha256"):
            raise DriverError("installed driver digest mismatch")
        return str(target)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DriverError("driver resolution refused: %s" % error) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--pack", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.argv or args.argv[0] != "--":
        parser.error("driver arguments must follow --")
    try:
        target = resolve(args.root, args.pack, args.entry)
        os.execv(target, [target] + args.argv[1:])
    except (DriverError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 78


if __name__ == "__main__":
    sys.exit(main())
