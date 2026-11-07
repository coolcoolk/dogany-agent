#!/usr/bin/env python3
"""Run declared framework suites serially through the enclosing budget gate.

Default scope is the current branch's changed files and their test-covers
bindings. --all selects all declared fast suites. Slow suites require --slow.
This entry has no CI, release, offload, or publication policy.
"""
import argparse
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def main():
    root = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel') or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--slow', action='store_true')
    parser.add_argument('--suite', action='append', default=[], help='explicit bounded suite subset')
    parser.add_argument('--merge-check', metavar='BASE',
                        help='run affected portable Tier-0 suites since BASE')
    options = parser.parse_args()
    args = {flag for flag in ('--list', '--all', '--slow') if getattr(options, flag[2:])}
    base = (options.merge_check or os.environ.get('DOGANY_DISPATCH_BASE_SHA')
            or git(root, 'merge-base', 'HEAD', '@{upstream}') or 'HEAD^')
    changed = set(git(root, 'diff', '--name-only', base or 'HEAD^', 'HEAD').splitlines())
    changed.update(git(root, 'diff', '--name-only', 'HEAD').splitlines())
    paths = git(root, 'ls-files', '-z').split('\0')
    selected, deferred = [], []
    host = os.environ.get('DGN_TEST_HOST', '')
    rows_path = os.environ.get('DGN_TEST_ROWS', '')
    rows = open(rows_path, 'a') if rows_path else None
    for rel in paths:
        p = root / rel
        if (not rel or rel.startswith('extracted-devkit/') or '/fixtures/' in rel
                or p.suffix not in ('.py', '.sh') or not p.is_file()):
            continue
        header = '\n'.join(p.read_text(errors='replace').splitlines()[:60])
        lane = re.search(r'^#\s*test-lane:\s*(\w+)', header, re.M)
        if not lane or lane[1] not in ('fast', 'slow'):
            continue
        covers = re.findall(r'^#\s*test-covers:\s*(.*)', header, re.M)
        patterns = ' '.join(covers).split()
        affected = rel in changed or any(fnmatch.fnmatchcase(c, pattern) for c in changed for pattern in patterns)
        tier = re.search(r'^#\s*test-tier:\s*(\d+)', header, re.M)
        if options.merge_check and (not tier or tier[1] != '0'):
            continue
        marked_macos = bool(re.search(r'^#\s*test-host:\s*macos\s+\S', header, re.M))
        if options.merge_check and host == 'linux' and marked_macos:
            continue
        if options.suite and rel not in options.suite:
            continue
        if not options.suite and '--all' not in args and not affected:
            continue
        if lane[1] == 'slow' and '--slow' not in args:
            deferred.append(rel)
        else:
            selected.append(rel)
    missing = set(options.suite) - set(selected) - set(deferred)
    if missing:
        parser.error('undeclared or unavailable suite: ' + ', '.join(sorted(missing)))
    for rel in deferred:
        print('DEFERRED slow: ' + rel, flush=True)
    failures = 0
    try:
        for rel in selected:
            print(('SELECTED ' if '--list' in args else 'RUN ') + rel, flush=True)
            if '--list' in args:
                continue
            command = [sys.executable, '-B', rel] if rel.endswith('.py') else ['bash', rel]
            started = time.monotonic()
            p = subprocess.run(command, cwd=root)
            elapsed = max(0, round(time.monotonic() - started))
            outcome = 'pass' if p.returncode == 0 else 'fail'
            if rows:
                rows.write('suite %s %s %s\n' % (rel, outcome, elapsed))
                rows.flush()
            failures += p.returncode != 0
        if rows:
            rows.write('end failures=%d degraded=0\n' % failures)
            rows.flush()
    finally:
        if rows:
            rows.close()
    print('framework tests: selected=%d deferred=%d failures=%d degraded=0' %
          (len(selected), len(deferred), failures))
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
