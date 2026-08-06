#!/usr/bin/env python3
# extract_section.py -- single-source AGENT.md section extractor (DGN-441 W-A).
#
# Both the snapshot publish path (AGENT.md.add + .source-sync baseline) and the
# finalize drift report read a source AGENT.md section the SAME way. Keeping the
# algorithm in one physical module prevents a third divergent copy: if the
# extraction rule ever changes, snapshot and finalize move in lockstep.
#
# Extraction rule (byte-equivalent to the legacy inline blocks in
# pack_publish.sh and refresh-source-sync.sh): a section is the heading line
# through to the next same-or-higher-level heading, with trailing blank lines
# and per-line trailing whitespace stripped. A missing heading returns None.
#
# Usage as a library:  from extract_section import extract_section, section_sha
# Usage as a CLI    :  extract_section.py <source_file> <heading>
#                        -> prints the extracted section text (rc 0), or exits
#                           rc 4 when the heading is not found.

import hashlib
import sys


def heading_level(h):
    return len(h) - len(h.lstrip("#"))


def extract_section(content, heading):
    """Return the section text for `heading` in `content`, or None if absent."""
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
            if heading_level(lines[i]) <= level:
                end = i
                break
    section_lines = lines[start:end]
    while section_lines and section_lines[-1].strip() == "":
        section_lines.pop()
    return "\n".join(section_lines)


def section_sha(content, heading):
    """Return (sha256hex, section_text) for `heading`, or (None, None)."""
    text = extract_section(content, heading)
    if text is None:
        return None, None
    normalized = "\n".join(line.rstrip() for line in text.split("\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), text


def _main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: extract_section.py <source_file> <heading>\n")
        return 2
    src, heading = argv[1], argv[2]
    content = open(src, encoding="utf-8").read()
    text = extract_section(content, heading)
    if text is None:
        sys.stderr.write("SECTION_NOT_FOUND %s\n" % heading)
        return 4
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
