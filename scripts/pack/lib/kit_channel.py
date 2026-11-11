#!/usr/bin/env python3
"""DGN-2206: judge kit floors against the catalog's stable release tags.

Read-only: reuse the coordinate resolver, never fetch or create a tag. An
unreadable release catalog is distinct from a measured unavailable floor.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from semver_range import cmp_precedence, is_prerelease_pack, parse_semver_full, satisfies


UNAVAILABLE = "kit.channel-unavailable"


def read_command(argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=20,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0")).stdout.strip()


def check(manifest, catalog, channel, instance_conf=None):
    if channel in ("dev", "staging", "main"):
        return {}
    rk = manifest.get("requires_kit")
    if not manifest.get("contract_version") and manifest.get("kind") != "kit":
        return {}
    if not isinstance(rk, dict) or set(rk) != {"kit", "range"}:
        return {}  # The contract/form gate owns malformed declarations.
    kit, constraint = rk["kit"], rk["range"]
    if not isinstance(kit, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kit):
        return {}
    if not isinstance(constraint, str):
        return {}
    floors = []
    for token in constraint.split():
        match = re.fullmatch(r"(>=|>|==)(.+)", token)
        if match:
            if parse_semver_full(match[2]) is None:
                raise ValueError("unparseable kit floor: " + match[2])
            floors.append((match[1], match[2]))
    if not floors:
        return {}
    # A satisfied install does not need a kit release or an auto-upgrade.
    if instance_conf is not None and Path(instance_conf).exists():
        for line in Path(instance_conf).read_text().splitlines():
            if line.startswith("DOGANY_PACKS="):
                for entry in line.split("=", 1)[1].strip("\"'").split(","):
                    name, _, version = entry.partition("@")
                    if name == kit and satisfies(version, constraint) is True:
                        return {}
                break
    required = floors[0][1]
    for _, version in floors[1:]:
        if cmp_precedence(version, required) > 0:
            required = version
    detail = {"reason_code": UNAVAILABLE, "kit": kit, "required_version": required,
              "range": constraint, "channel": "stable"}
    # Publishing forbids prerelease floors even after a stable kit catches
    # up. Installing an older pack may use that now-available stable kit.
    if instance_conf is None and any(is_prerelease_pack(kit, version) for _, version in floors):
        return dict(detail, cause="prerelease-floor")
    cat = json.loads(Path(catalog).read_text())
    row = next((r for r in cat["packs"] if r.get("id") == kit), None)
    if row is None:
        raise ValueError("required kit is absent from the catalog: " + kit)
    coords = Path(__file__).with_name("pack_coords.sh")
    directory = read_command(["bash", str(coords), "row-dir", str(catalog), kit])
    # Catalog coordinates may be independent repos OR in-tree kit dirs.
    # Git resolves their containing repository; the kit tag namespace is exact.
    tags = None
    if directory and Path(directory).is_dir():
        try:
            tags = read_command(["git", "-C", directory, "for-each-ref",
                                 "--format=%(refname:strip=2)", "refs/tags/pack/%s/v*" % kit]).splitlines()
        except subprocess.CalledProcessError:
            pass  # Unpacked coordinate: use the catalog's public source.
    if tags is None and row.get("source_url"):
        refs = read_command(["git", "-c", "credential.interactive=false", "ls-remote",
                             "--tags", "--refs", row["source_url"],
                             "refs/tags/pack/%s/v*" % kit])
        tags = [line.split()[-1][len("refs/tags/"):] for line in refs.splitlines()]
    elif tags is None:
        raise ValueError("required kit release tags are unreadable: " + kit)
    prefix = "pack/%s/v" % kit
    stable = [tag[len(prefix):] for tag in tags if tag.startswith(prefix)
              and is_prerelease_pack(kit, tag[len(prefix):]) is False]
    top = None
    for version in stable:
        if top is None or cmp_precedence(version, top) > 0:
            top = version
    if top is None or any(cmp_precedence(version, top) > 0
                          or (op == ">" and cmp_precedence(version, top) == 0)
                          for op, version in floors):
        return dict(detail, cause="floor-above-stable", stable_top=top)
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("catalog")
    parser.add_argument("channel", choices=("stable", "release", "dev", "staging", "main"))
    parser.add_argument("--instance-conf")
    args = parser.parse_args()
    try:
        result = check(json.loads(Path(args.manifest).read_text()), args.catalog,
                       args.channel, args.instance_conf)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {"reason_code": "kit.channel-unverifiable", "detail": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 1 if result else 0


if __name__ == "__main__":
    sys.exit(main())
