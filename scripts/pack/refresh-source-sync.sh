#!/bin/bash
# refresh-source-sync.sh -- regenerate packs/dev/.source-sync after a
# conformance pass.
#
# Run this after reviewing the dev pack AGENT.md.add against the source
# sections in the source AGENT.md and confirming the pack fragment still
# reflects the intent correctly.  The new snapshot replaces the old one so
# release-preflight resets its drift baseline to NOW.
#
# The section list is NOT hardcoded in this script (section headings are
# private per-instance data).  On a refresh run the headings are read from
# the existing .source-sync file.  To add or change tracked sections, pass
# a sections file (one heading per line) via --sections-file.
#
# Usage:
#   refresh-source-sync.sh [--source-file <path>] [--sections-file <path>]
#
#   --source-file <path>
#       Path to the source AGENT.md to hash.  Required: either this flag or
#       the DEV_PACK_SOURCE_FILE env var must be set.  The source file path
#       is machine-specific and is never stored in this repo.
#
#   --sections-file <path>
#       Plain-text file listing section headings to track, one per line.
#       When absent, headings are read from the existing .source-sync file
#       (round-trip refresh: update hashes only, preserve the section list).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYNC_FILE="$REPO_ROOT/packs/dev/.source-sync"

# Determine source file: CLI flag > env var > existing .source-sync recorded
# path (legacy fallback, pre-pathless format) > conventional default.
SOURCE_FILE="${DEV_PACK_SOURCE_FILE:-}"
SECTIONS_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-file)   SOURCE_FILE="$2"; shift 2 ;;
    --sections-file) SECTIONS_FILE="$2"; shift 2 ;;
    *) echo "[refresh-source-sync] unknown option: $1" >&2; exit 1 ;;
  esac
done

# No source file supplied -- require explicit argument rather than guessing a
# machine-specific path.  The source AGENT.md location differs per instance;
# there is no safe universal default.
if [[ -z "$SOURCE_FILE" ]]; then
    echo "[refresh-source-sync] ERROR: source file not specified." >&2
    echo "  Pass --source-file <path> or set DEV_PACK_SOURCE_FILE." >&2
    exit 1
fi

if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "[refresh-source-sync] ERROR: source file not found: $SOURCE_FILE" >&2
    echo "  Pass --source-file <path> or set DEV_PACK_SOURCE_FILE." >&2
    exit 1
fi

TODAY="$(date '+%Y-%m-%d')"

echo "[refresh-source-sync] source: $SOURCE_FILE"
echo "[refresh-source-sync] output: $SYNC_FILE"
echo "[refresh-source-sync] date:   $TODAY"

python3 - "$SOURCE_FILE" "$SYNC_FILE" "$TODAY" "${SECTIONS_FILE:-}" <<'PYEOF'
import sys, hashlib, os

source_file, sync_file, today, sections_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

# Determine the section list:
#   1. --sections-file supplied -> read headings from it (one per line)
#   2. existing .source-sync -> read headings from current data rows
#   3. neither -> empty list (write header-only file with a warning)
def heading_level(h):
    return len(h) - len(h.lstrip("#"))

headings = []
if sections_file and os.path.isfile(sections_file):
    # Sections file: one Markdown heading per line (lines starting with #).
    # Blank lines are skipped; no comment syntax (headings themselves start
    # with # so a comment prefix would collide).
    with open(sections_file, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.strip():
                headings.append(line)
    print(f"  sections-file: {len(headings)} headings from {sections_file}")
elif os.path.isfile(sync_file):
    # Round-trip refresh: read headings from existing .source-sync data rows.
    # Supports both old format (<hash>\t<source_file>\t<heading>) and new
    # pathless format (<hash>\t<heading>\t<snapshot_date>) by detecting which
    # field looks like a heading (starts with #).
    with open(sync_file, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            # New format: parts[1] is the heading (starts with #)
            # Old format: parts[2] is the heading (parts[1] is a file path)
            if parts[1].startswith("#"):
                headings.append(parts[1])
            elif len(parts) >= 3 and parts[2].startswith("#"):
                headings.append(parts[2])
    print(f"  round-trip refresh: {len(headings)} headings from existing .source-sync")
else:
    print("  WARN: no --sections-file and no existing .source-sync -- writing empty baseline", file=sys.stderr)

def extract_section(content, heading):
    level = heading_level(heading)
    lines = content.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#"):
            hcount = heading_level(lines[i])
            if hcount <= level:
                end = i
                break
    section_lines = lines[start:end]
    while section_lines and section_lines[-1].strip() == "":
        section_lines.pop()
    return "\n".join(section_lines)

content = open(source_file, encoding="utf-8").read()
rows = []
for heading in headings:
    text = extract_section(content, heading)
    if text is None:
        print(f"  WARN: section not found: {heading!r}", file=sys.stderr)
        continue
    normalized = "\n".join(line.rstrip() for line in text.split("\n"))
    h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    rows.append((h, heading, today))
    print(f"  hashed: {h[:16]}...  {heading}")

header = f"""\
# dev pack source conformance baseline
# Generated by: scripts/pack/refresh-source-sync.sh
# Snapshot date: {today}
#
# Each entry records the sha256 of one pack-mirrored section (heading text
# to the next same-or-higher-level heading, trailing blank lines stripped,
# trailing whitespace per line stripped).
#
# release-preflight reads this file and warns ONLY when a listed section
# has changed since this snapshot. If the source file is absent (other
# machines), the check is skipped with an explicit note.
#
# Format:  <sha256hex>  <section_heading>  <snapshot_date>
# Fields are tab-separated. The source file path is NOT stored here (it is
# machine-specific private data); the caller supplies it via the
# DEV_PACK_SOURCE_FILE env var or the --source-file flag.
# Do not hand-edit; regenerate via:
#   scripts/pack/refresh-source-sync.sh
#   (pass --source-file <path> or set DEV_PACK_SOURCE_FILE)
"""

with open(sync_file, "w", encoding="utf-8") as fh:
    fh.write(header + "\n")
    for h, heading, snap_date in rows:
        fh.write(f"{h}\t{heading}\t{snap_date}\n")

print(f"  wrote {len(rows)} entries -> {sync_file}")
PYEOF

echo "[refresh-source-sync] done."
