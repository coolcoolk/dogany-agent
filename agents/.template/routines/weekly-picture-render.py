#!/usr/bin/env python3
"""Render the approved weekly H2 feed; no transport or pin side effects."""
import argparse
import json
from pathlib import Path
import sys

from lib.weekly_picture import DependencyError, render


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--in', dest='feed', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--check-out', type=Path)
    args = parser.parse_args(argv)
    if args.check_out is not None and args.out.resolve() == args.check_out.resolve():
        parser.error('--out and --check-out must be different paths')
    if args.feed.resolve() in {p.resolve() for p in (args.out, args.check_out) if p is not None}:
        parser.error('output paths must differ from --in')
    try:
        feed = json.loads(args.feed.read_text(encoding='utf-8'))
        result = render(feed, args.out, args.check_out)
    except (DependencyError, ValueError, OSError) as exc:
        print('weekly-picture-render: {}'.format(exc), file=sys.stderr)
        return 2
    print('wrote {} (1800x2520)'.format(args.out))
    for omitted in result['overflow']:
        print('weekly-picture-render: {}'.format(omitted), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
