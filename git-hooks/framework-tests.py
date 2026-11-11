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


def known_red_entries(root):
    """Return live KNOWN-RED entries, including DGN-1780 bounds."""
    today = time.strftime('%Y-%m-%d', time.gmtime())
    entries = {}
    try:
        lines = (root / 'tests' / 'KNOWN-RED').read_text().splitlines()
    except OSError:
        return entries
    for line in lines:
        fields = line.split()
        if not fields or fields[0].startswith('#') or len(fields) < 3 or fields[2] < today:
            continue
        fails, ids = None, None
        for token in fields[3:]:
            if re.fullmatch(r'fails=\d+', token):
                fails = int(token[6:])
            elif token.startswith('ids=') and token[4:]:
                ids = set(token[4:].split(','))
            else:
                break
        entries[fields[0]] = (fields[1], fails, ids)
    return entries


def failure_observation(output):
    """Read the same explicit counters and FAIL[id] markers as the shell runner."""
    count = None
    for line in output.splitlines():
        summaries = re.findall(r'(?<![A-Za-z0-9_])(?:FAIL|fail)=(\d+)', line)
        if summaries:
            count = int(summaries[-1])
        if line.startswith('FAILED ('):
            failures = re.search(r'failures=(\d+)', line)
            errors = re.search(r'errors=(\d+)', line)
            if failures or errors:
                count = int(failures[1]) if failures else 0
                count += int(errors[1]) if errors else 0
        # Several portable suites use unittest/pytest or their own human-readable
        # summary instead of the shell lane's FAIL=N spelling.
        summaries = re.findall(r'(?<![A-Za-z0-9_])(\d+)\s+failed\b', line, re.I)
        if summaries:
            count = int(summaries[-1])
        summaries = re.findall(r'(?<![A-Za-z0-9_])FAIL\s+(\d+)\b', line)
        if summaries:
            count = int(summaries[-1])
        summaries = re.findall(r'^FAIL \((\d+)\)', line)
        if summaries:
            count = int(summaries[-1])
        if count is None and re.match(r'^FAIL(?:ED)? test[-_]', line):
            count = 1
        if count is None:
            progress = re.match(r'^([.EF]+)', line)
            if progress and ('E' in progress[1] or 'F' in progress[1]):
                count = progress[1].count('E') + progress[1].count('F')
    return count, set(re.findall(r'FAIL\[([^]]+)\]', output))


def within_known_red(output, fails_bound, ids_bound):
    count, ids = failure_observation(output)
    if ids_bound is not None and not ids.issubset(ids_bound):
        return False, count
    if fails_bound is not None and (count is None or count > fails_bound):
        return False, count
    return True, count


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
    parser.add_argument('--shard', metavar='K/N',
                        help='stable one-based shard of host-runnable selected suites')
    parser.add_argument('--merge-check', metavar='BASE',
                        help='run affected portable Tier-0 suites since BASE')
    options = parser.parse_args()
    args = {flag for flag in ('--list', '--all', '--slow') if getattr(options, flag[2:])}
    base = (options.merge_check or os.environ.get('DOGANY_DISPATCH_BASE_SHA')
            or git(root, 'merge-base', 'HEAD', '@{upstream}') or 'HEAD^')
    shard_k, shard_n = 1, 1
    if options.shard:
        match = re.fullmatch(r'([1-9]\d*)/([1-9]\d*)', options.shard)
        if not match or int(match[1]) > int(match[2]):
            parser.error('--shard must be k/n with 1 <= k <= n')
        shard_k, shard_n = int(match[1]), int(match[2])
    changed = set(git(root, 'diff', '--name-only', base or 'HEAD^', 'HEAD').splitlines())
    changed.update(git(root, 'diff', '--name-only', 'HEAD').splitlines())
    paths = git(root, 'ls-files', '-z').split('\0')
    selected, deferred, host_skipped = [], [], []
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
        if options.suite and rel not in options.suite:
            continue
        if not options.suite and '--all' not in args and not affected:
            continue
        # A '# test-host: macos' suite never runs on the Linux host; it is
        # recorded as a host-skip row so a split-host receipt is checkable.
        if host == 'linux' and marked_macos:
            host_skipped.append((rel, lane[1]))
            continue
        if lane[1] == 'slow' and '--slow' not in args:
            deferred.append(rel)
        else:
            selected.append(rel)
    missing = (set(options.suite) - set(selected) - set(deferred)
               - {rel for rel, _lane in host_skipped})
    if missing:
        parser.error('undeclared or unavailable suite: ' + ', '.join(sorted(missing)))
    selected = [rel for index, rel in enumerate(sorted(selected))
                if index % shard_n == shard_k - 1]
    if rows and shard_k == 1:
        for rel, lane in sorted(host_skipped):
            rows.write('host-skip suite %s %s\n' % (rel, lane))
    for rel in deferred:
        print('DEFERRED slow: ' + rel, flush=True)
    failures = 0
    known_red = known_red_entries(root)
    observed_red = []
    try:
        for rel in selected:
            print(('SELECTED ' if '--list' in args else 'RUN ') + rel, flush=True)
            if '--list' in args:
                continue
            command = [sys.executable, '-B', rel] if rel.endswith('.py') else ['bash', rel]
            started = time.monotonic()
            p = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            sys.stdout.write(p.stdout)
            sys.stdout.flush()
            elapsed = max(0, round(time.monotonic() - started))
            outcome = 'pass' if p.returncode == 0 else 'fail'
            entry = known_red.get(rel)
            if p.returncode != 0 and entry:
                ticket, bound, ids = entry
                bounded, count = within_known_red(p.stdout, bound, ids)
                if bounded:
                    outcome = 'KNOWN-RED'
                    observed_red.append((rel, count, bound, ticket))
            if rows:
                suffix = (' bound=%d' % entry[1]
                          if outcome == 'KNOWN-RED' and entry[1] is not None else '')
                rows.write('suite %s %s %s%s\n' % (rel, outcome, elapsed, suffix))
                rows.flush()
            failures += outcome == 'fail'
        if rows:
            rows.write('end failures=%d degraded=0\n' % failures)
            rows.flush()
    finally:
        if rows:
            rows.close()
    print('framework tests: selected=%d deferred=%d failures=%d degraded=0' %
          (len(selected), len(deferred), failures))
    for rel, count, bound, ticket in observed_red:
        shown = '?' if count is None else str(count)
        limit = '?' if bound is None else str(bound)
        print('KNOWN-RED %s fails=%s/%s (%s)' % (rel, shown, limit, ticket))
    return int(bool(failures))


if __name__ == '__main__':
    sys.exit(main())
