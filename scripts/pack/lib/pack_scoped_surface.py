#!/usr/bin/env python3
"""Enumerate and digest the install-visible surface of an extracted pack."""

import hashlib
import json
import os
import stat
import sys


# Root metadata intentionally inspected by install-side gates.  Everything else
# at the repository root is authoring material and cannot be installed.
ROOT_METADATA_FILES = (
    "CHANGELOG.md",
    "RELEASES.md",
    "RELEASE-NOTES.md",
    "CONTRACT.md",
)
ROOT_METADATA_DIRS = ("releases",)


def roots(package):
    manifest = os.path.join(package, "pack-manifest.json")
    payload_root = "payload"
    try:
        with open(manifest, encoding="utf-8") as stream:
            value = json.load(stream).get("payload_root", payload_root)
        if isinstance(value, str) and value:
            payload_root = value
    except (OSError, ValueError, AttributeError):
        pass  # compat-lint owns the manifest diagnostic; still bind its bytes.
    # Never let an untrusted payload_root turn this pre-lint optimization into
    # a read outside the extraction.  compat-lint will issue the actual C1
    # boundary verdict from the manifest.
    candidate = os.path.realpath(os.path.join(package, payload_root))
    package_real = os.path.realpath(package)
    try:
        payload_names = (payload_root,) if os.path.commonpath((candidate, package_real)) == package_real else ()
    except ValueError:
        payload_names = ()
    names = ("pack-manifest.json",) + payload_names + ROOT_METADATA_FILES + ROOT_METADATA_DIRS
    result = []
    for name in names:
        path = os.path.join(package, name)
        if os.path.lexists(path) and path not in result:
            result.append(path)
    return result


def add(h, path, label):
    h.update(label.encode("utf-8", "surrogateescape") + b"\0")
    try:
        st = os.lstat(path)
    except OSError:
        h.update(b"absent\0")
        return
    h.update(("%o" % stat.S_IMODE(st.st_mode)).encode() + b"\0")
    if stat.S_ISLNK(st.st_mode):
        h.update(os.readlink(path).encode("utf-8", "surrogateescape"))
    elif stat.S_ISREG(st.st_mode):
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)


def digest(package):
    h = hashlib.sha256()
    for root in roots(package):
        rel = os.path.relpath(root, package)
        add(h, root, rel)
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs.sort()
            files.sort()
            for name in dirs + files:
                path = os.path.join(base, name)
                add(h, path, os.path.relpath(path, package))
    return h.hexdigest()


if __name__ == "__main__":
    command, package = sys.argv[1:3]
    if command == "roots":
        for path in roots(package):
            print(path)
    elif command == "digest":
        print(digest(package))
    else:
        raise SystemExit("usage: pack_scoped_surface.py {roots|digest} PACKAGE")
