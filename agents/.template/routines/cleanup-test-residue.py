#!/usr/bin/env python3
"""Sweep old, idle, git-safe test scratch. Preview unless --apply.

The daily cleanup entry applies by default. Rename into ONE private dated
Trash folder, then empty that folder only; failed checks always keep data.
"""
import argparse
import datetime
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

MARKER = '.dogany-test-scratch'
MARKER_VALUE = 'dogany-test-scratch-v1\n'
# Legacy migration only. These are measured test/dispatch naming conventions,
# never a general family-name wildcard. Names alone do
# not authorize a move: all recursive age/owner/evidence/git/live gates apply.
LEGACY = re.compile(
    r'(?:program-plan-test-|program-rehearsal-|bridge-test-|model-state-test-|'
    r'model-cmd-test-|dgn[0-9]{3,}[-.]|run-tests-disc\.|session-config-install-|'
    r'test-pack-lanes-|update-apply-notices-|run-suite\.|ledger-rcv-|'
    r'dogany-merge-gate-|kit-dev[0-9]+-|kit[0-9]+-|health-dev[0-9]+-|'
    r'skull-wt-|skull-kit-dev[0-9]+|cooking-agent-|calorie-replay-|mig[0-9]+-parity)'  # estate-scrub:allow(measured legacy residue prefixes; matching disk names is the migration contract)
    r'[A-Za-z0-9_.-]+\Z|tmp\.[A-Za-z0-9]{4,}\Z')
PROTECTED = ('com.apple.', 'claude-', 'agent-browser-', 'dogany-dispatch')


class Keep(Exception):
    pass


def command(argv):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(name, None)
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30, env=env)
    if result.returncode or (argv[0] == 'lsof' and result.stderr.strip()):
        raise Keep('command failed: ' + argv[0])
    return result.stdout


def busy_paths():
    # Full own-uid snapshot includes open files AND cwd. On Linux, /proc is a
    # portable fallback for minimal hosts that do not install lsof.
    try:
        raw = command(['lsof', '-n', '-P', '-u', str(os.getuid()), '-Fpn'])
        paths = [Path(os.path.realpath(os.fsdecode(line[1:])))
                 for line in raw.splitlines() if line.startswith(b'n/')]
    except FileNotFoundError:
        paths = []
        proc = Path('/proc')
        for process in proc.iterdir() if proc.is_dir() else ():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                links = [process / 'cwd'] + list((process / 'fd').iterdir())
                paths.extend(Path(os.path.realpath(link)) for link in links)
            except (FileNotFoundError, PermissionError, OSError):
                continue
    if not paths:
        raise Keep('empty process file snapshot')
    return paths


def idle(path, busy):
    if any(p == path or path in p.parents for p in busy):
        raise Keep('open file or cwd')


def git_safe(repo):
    def git(*args):
        return command(['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                        '-C', str(repo), *args])
    # Exclude only the harness control files. Untracked product still keeps
    # the tree, as do a failed Git command, an unborn HEAD, or no remote proof.
    if git('status', '--porcelain=v1', '-z', '--untracked-files=all', '--', '.',
           ':(exclude)' + MARKER, ':(exclude)owner.pid'):
        raise Keep('dirty git tree')
    head = git('rev-parse', '--verify', 'HEAD').strip()
    refs = git('for-each-ref', '--format=%(refname)', '--contains=' + head.decode('ascii'),
               'refs/remotes/').splitlines()
    if not any(not ref.endswith(b'/HEAD') for ref in refs):
        raise Keep('git HEAD has no pushed reference')


def inspect(path, cutoff, busy):
    if path.name.startswith(PROTECTED):
        raise Keep('protected service name')
    if not str(path).isascii() or any(c in str(path) for c in '\n\r\t\\'):
        raise Keep('unrepresentable lsof path')
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise Keep('not a real file or directory')
    marker = path / MARKER
    marked = False
    if marker.exists() or marker.is_symlink():
        st = marker.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise Keep('invalid marker')
        marked = marker.read_text() == MARKER_VALUE
        if not marked:
            raise Keep('invalid marker')
    if not marked and not LEGACY.fullmatch(path.name):
        raise Keep('unrecognized scratch')
    idle(path, busy)
    pending, signature, repos = [path], [], []
    while pending:
        item = pending.pop()
        if not item.name.isascii() or any(c in item.name for c in '\n\r\t\\'):
            raise Keep('unrepresentable lsof descendant')
        if 'evidence' in item.name.lower() or item.name.startswith(PROTECTED):
            raise Keep('protected descendant')
        st = item.lstat()
        if (st.st_uid != os.getuid() or st.st_mtime >= cutoff
                or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))):
            raise Keep('fresh, foreign-owned, linked or special content')
        signature.append((str(item.relative_to(path)), st.st_dev, st.st_ino,
                          st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        if item.name == 'owner.pid':
            pid = item.read_text().strip()
            if not pid.isdigit() or int(pid) <= 0:
                raise Keep('unknown scratch owner')
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                pass
            except OSError:
                raise Keep('unknown scratch owner')
            else:
                raise Keep('live scratch owner')
        if stat.S_ISDIR(st.st_mode):
            children = list(item.iterdir())
            if any(c.name == '.git' for c in children):
                repos.append(item)
            # Bare repositories also contain commits worth protecting.
            if (item / 'HEAD').is_file() and (item / 'objects').is_dir():
                raise Keep('bare git repository')
            pending.extend(children)
    for repo in repos:
        git_safe(repo)
    return sorted(signature)


def roots_default():
    return [Path('/private/tmp'), Path(tempfile.gettempdir()),
            Path(os.environ.get('DOGANY_TEST_SCRATCH') or
                 str(Path.home() / '.dogany/scratch/tests'))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, action='append', help='repeat to replace default roots')
    parser.add_argument('--trash', type=Path, default=Path.home() / '.Trash')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    trash = args.trash.absolute()
    # Never follow a caller-supplied symlink into someone else's Trash tree.
    if any(p.is_symlink() for p in [trash, *trash.parents]):
        parser.error('symlink in trash path')
    trash = trash.resolve()
    roots = sorted(set(p.resolve() for p in (args.root or roots_default())))
    for root in roots:
        if (root == Path('/') or root == trash or root in trash.parents
                or trash in root.parents):
            parser.error('unsafe root/trash relationship')
    cutoff = time.time() - 3 * 3600
    folder = None
    moved = 0
    failed = False
    try:
        busy = busy_paths()
    except (Keep, OSError, subprocess.TimeoutExpired) as exc:
        print('skip-all\t' + str(exc))
        return 1
    for root in roots:
        if not root.exists():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            print('skip-root\t' + str(root) + '\t' + str(exc))
            failed = True
            continue
        for path in entries:
            try:
                signature = inspect(path, cutoff, busy)
                if not args.apply:
                    print('would-trash\t' + str(path))
                    continue
                # Refresh process and filesystem/Git proof immediately before
                # each atomic same-device rename. EXDEV keeps the source.
                busy = busy_paths()
                if inspect(path, cutoff, busy) != signature:
                    raise Keep('changed during inspection')
                if folder is None:
                    trash.mkdir(mode=0o700, parents=True, exist_ok=True)
                    st = trash.lstat()
                    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                        raise Keep('unsafe trash owner/type')
                    folder = trash / ('dogany-test-residue-' +
                                      datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex)
                    folder.mkdir(mode=0o700)
                dest = folder / (path.name + '.' + uuid.uuid4().hex)
                path.rename(dest)
                moved += 1
                print('trashed\t' + str(path) + '\t' + str(dest), flush=True)
            except Keep:
                continue
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                print('skip\t' + str(path) + '\t' + str(exc))
                failed = True
    if folder is not None:
        # The only recursive delete in this program. Never empty ~/.Trash,
        # never enumerate or retry an older run's folder. No copy/delete fallback.
        try:
            shutil.rmtree(folder)
            print('emptied\t' + str(folder), flush=True)
        except OSError as exc:
            print('trash-kept\t' + str(folder) + '\t' + str(exc))
            failed = True
    print('residue: %d moved' % moved)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
