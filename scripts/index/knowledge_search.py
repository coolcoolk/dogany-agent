#!/usr/bin/env python3
"""Local warehouse reader: one miss intake, resolver recheck, one silent retry.

Fenced excerpts are evidence, never live rule fields. The canonical developer
reader bypasses views for unpublished agent-engineering and never records misses.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys

from knowledge_handoff import TOKEN, build


def live_lines(text):
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        yield number, line


def search(directory, query):
    hits = []
    for path in sorted(directory.rglob('*.md')):
        if path.is_symlink():
            continue
        for number, line in live_lines(path.read_text(encoding='utf-8')):
            if query.casefold() in line.casefold():
                hits.append({'path': str(path), 'line': number, 'text': line})
    return hits


def append_miss(root, warehouse, module, gist):
    target = root / 'knowledge-custom' / warehouse / 'misses.log'
    # The product-user pair owns this store; never follow an alias into a
    # different instance or export tree.
    for path in (target, *target.parents):
        if path == root.parent:
            break
        if path.is_symlink():
            raise ValueError('miss intake requires instance-local real paths')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a', encoding='utf-8') as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        out.write('%s %s %s\n' % (stamp, module, ' '.join(gist.split())))


def main():
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument('--instance', type=Path)
    source.add_argument('--canonical', type=Path, help='local private KW checkout; agent-engineering direct read')
    ap.add_argument('--warehouse', default='kimwog')
    ap.add_argument('--module', default='?')
    ap.add_argument('--gist', help='question gist for the instance-local miss record')
    ap.add_argument('query')
    args = ap.parse_args()
    if not TOKEN.fullmatch(args.warehouse) or args.module != '?' and not TOKEN.fullmatch(args.module):
        ap.error('invalid warehouse/module token')
    if not args.query.strip():
        ap.error('query must not be empty')
    if args.canonical:
        directory = args.canonical / 'modules' / 'agent-engineering' / 'items'
        if not directory.is_dir():
            print('PENDING_OWNER_CONFIRM_KNOWLEDGE_NOT_READY', file=sys.stderr)
            return 3
        hits = search(directory, args.query)
    else:
        root = args.instance.resolve()
        directory = root / 'knowledge' / args.warehouse
        if not directory.is_dir():
            print('PENDING_OWNER_CONFIRM_KNOWLEDGE_NOT_READY', file=sys.stderr)
            return 3
        # Only active items/evidence; excluded .pack-items and tools cannot
        # masquerade as a live view. No unrelated memory-search miss intake.
        def lookup():
            return search(directory / 'items', args.query) + search(directory / 'e', args.query)
        hits = lookup()
        if not hits:
            append_miss(root, args.warehouse, args.module, args.gist or args.query)
            subprocess.run(['bash', str(Path(__file__).with_name('index.sh')), 'refresh',
                            'knowledge', '--if-older', '300', '--instance', str(root)],
                           stdout=sys.stderr, check=False)
            # Build may honestly report unavailable until WP9/WP10 land.
            with redirect_stdout(sys.stderr):
                build_rc = build(root, args.warehouse)
            if build_rc not in (0, 3):
                print('knowledge: recheck build failed; searching retained view', file=sys.stderr)
            hits = lookup()  # Retry cannot append a second record.
    print(json.dumps(hits, ensure_ascii=True))
    return 0 if hits else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print('knowledge search: ' + str(exc), file=sys.stderr)
        sys.exit(2)
