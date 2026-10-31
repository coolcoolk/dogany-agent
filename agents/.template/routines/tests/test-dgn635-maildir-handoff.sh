#!/usr/bin/env bash
# test-lane: fast
# DGN-1462 canonical port of the maintainer's DGN-635 T8/T8b sweep regressions.
# The retired send/consume/CLI cases belong to the engine and notify suites:
# test-dgn1379-handoff-lib-landing.py and test-dgn1774-consume-notify.py.
# Fixtures only; no real push, launchctl, systemctl or network calls.
#
# DGN-1989 (reviewed loss): an instance that still carries the older
# 285-line suite trips the content-drift guard on update. Its T1-T7/T9 cases
# drive entrypoints the template no longer ships (handoff_cli.py,
# handoff-send.sh, handoff-to-<peer>.sh, handoff-consume.sh, --to-root), so
# they are not ported back. Replacements: T1/T2 -> lib-landing D3/D4 (D7
# asserts --to-root is gone, so T9 is retired on purpose); T3 ->
# slug-consume-layer test_9; T6 incl. the DGN-1561 no-owner-push rule ->
# consume-notify tests 1-4 and 8 (notify.py has no push path); T4/T5/T7
# covered the retired send wrappers only. The sweep's old long rationale
# comment and its unchecked find/"clean" path are superseded by the shorter
# comment and the scan-completeness logic in handoff-stale-sweep.sh.
set -euo pipefail
ROUTINES="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$ROUTINES" <<'PY'
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

routines = Path(sys.argv[1])
repo = routines.parents[2]
passed = 0

def check(condition, description):
    global passed
    assert condition, description
    passed += 1
    print('PASS: ' + description)

with tempfile.TemporaryDirectory(prefix='dgn1462-') as tmp:
    work = Path(tmp)
    root = work / 'demo-agent'
    box = work / 'handoff' / 'demo-agent'
    session = root / '.telegram_bot/session-inbox'
    legacy = root / 'files/handoff/inbox'
    for directory in (box / 'inbox', box / 'dead', box / 'done', session, legacy, root / 'config'):
        directory.mkdir(parents=True, exist_ok=True)
    (root / 'config/agent.conf').write_text('SLUG=demo-agent\n')
    push_log = work / 'push.log'
    push = work / 'push.sh'
    push.write_text('#!/bin/bash\nprintf called >> "' + str(push_log) + '"\nexit 4\n')
    push.chmod(0o755)
    env = dict(os.environ, AGENT_ROOT=str(root), DOGANY_HANDOFF_ROOT=str(box.parent),
               HANDOFF_SESSION_INBOX=str(session), HANDOFF_PUSH_SH=str(push),
               HANDOFF_STALE_HOURS='24', HANDOFF_DONE_KEEP_DAYS='60')
    env.pop('HANDOFF_SELF_SLUG', None)
    def run(**extra):
        return subprocess.run(['bash', str(routines / 'handoff-stale-sweep.sh')],
                              env=dict(env, **extra), capture_output=True, text=True)
    stale = []
    for directory in (box / 'inbox', box / 'dead', legacy):
        path = directory / 'stale.md'
        path.write_text('stale\n')
        os.utime(path, (time.time() - 48 * 3600,) * 2)
        stale.append(path)
    fresh = box / 'inbox/fresh.md'
    fresh.write_text('fresh\n')
    ignored = box / 'inbox/old.txt'
    ignored.write_text('not mail\n')
    os.utime(ignored, (time.time() - 48 * 3600,) * 2)
    old_done = box / 'done/old.md'
    old_done.write_text('consumed\n')
    os.utime(old_done, (time.time() - 62 * 86400,) * 2)
    recent_done = box / 'done/recent.md'
    recent_done.write_text('consumed\n')
    out = run()
    notices = list(session.glob('*.md'))
    check(out.returncode == 0, 'T8 stale-found exits 0')
    check(len(notices) == 1, 'T8 exactly one session notice')
    text = notices[0].read_text()
    check(all(str(p) in text for p in stale), 'T8 own inbox, dead and legacy paths delivered')
    check(str(fresh) not in text and str(ignored) not in text, 'fresh and non-mail files excluded')
    check("mailbox 'demo-agent'" in text, 'recipient comes from instance config')
    check(not push_log.exists(), 'T8 owner push never called')
    check(all(p.exists() for p in stale), 'T8 stale mail stays for retry')
    check(not old_done.exists() and recent_done.exists(), 'done retention prunes only old consumed mail')
    check(not list(session.glob('.*.md')), 'atomic delivery leaves no hidden temp')
    notices[0].unlink()
    out = run(HANDOFF_SESSION_INBOX=str(work / 'missing'))
    check(out.returncode == 0 and 'delivery FAILED' in out.stderr, 'T8b delivery failure is fail-open')
    check(all(str(p) in out.stderr for p in stale), 'T8b failed delivery retains diagnostic paths')
    check(not push_log.exists(), 'T8b no owner fallback')
    out = run()
    check(out.returncode == 0 and len(list(session.glob('*.md'))) == 1, 'next tick retries successfully')
    for p in session.glob('*.md'):
        p.unlink()
    failbin = work / 'failbin'
    failbin.mkdir()
    mover = failbin / 'mv'
    mover.write_text('#!/bin/bash\nexit 1\n')
    mover.chmod(0o755)
    out = run(PATH=str(failbin) + ':' + os.environ['PATH'])
    check(out.returncode == 0 and 'delivery FAILED' in out.stderr,
          'rename failure is fail-open')
    check(not list(session.iterdir()), 'rename failure cleans unpublished temp notice')
    for p in stale:
        p.unlink()
    out = run()
    check(out.returncode == 0 and 'scanned 3 directories, 0 stale files' in out.stdout,
          'fresh-only scan explicitly reports coverage')
    check(not list(session.glob('*.md')), 'fresh-only sends no notice')
    legacy.rmdir()
    out = run()
    check('scanned 2 directories, 0 stale files' in out.stdout, 'absent optional legacy inbox is valid')
    (box / 'dead').rmdir()
    out = run()
    check('scan incomplete' in out.stderr and '0 stale' not in out.stdout,
          'missing own mailbox never reports clean')
    partial = box / 'inbox/partial.md'
    partial.write_text('stale\n')
    os.utime(partial, (time.time() - 48 * 3600,) * 2)
    out = run()
    check('scan incomplete' in out.stderr and any(str(partial) in p.read_text() for p in session.glob('*.md')),
          'partial scan still delivers found stale mail')
    partial.unlink()
    for p in session.glob('*.md'):
        p.unlink()
    fresh.unlink()
    ignored.unlink()
    (box / 'inbox').rmdir()
    out = run()
    check(out.returncode == 0 and 'scan incomplete' in out.stderr and '0 stale' not in out.stdout,
          'zero available scan directories cannot look clean')
    (box / 'inbox').mkdir()
    (box / 'dead').mkdir()
    fakebin = work / 'bin'
    fakebin.mkdir()
    finder = fakebin / 'find'
    finder.write_text('#!/bin/bash\necho synthetic-scan-error >&2\nexit 1\n')
    finder.chmod(0o755)
    out = run(PATH=str(fakebin) + ':' + os.environ['PATH'])
    check('scan incomplete' in out.stderr and '0 stale' not in out.stdout,
          'find failure never reports clean')
    (root / 'config/agent.conf').unlink()
    out = run()
    check(out.returncode == 1 and 'SLUG= missing' in out.stderr, 'missing slug remains deployment error')

    unit = 'com.telegram-skill-bot.telegram-agent.handoff-stale-sweep'
    plist = plistlib.loads((routines / (unit + '.plist')).read_bytes())
    check(plist['StartCalendarInterval'] == {'Hour': 8, 'Minute': 40} and not plist['RunAtLoad'],
          'launchd daily 08:40 schedule matches existing instances')
    check(plist['ProgramArguments'][-1] == '__PROJECT_ROOT__/routines/handoff-stale-sweep.sh'
          and '__PROJECT_ROOT__/routines/cron-guard.sh' in plist['ProgramArguments'],
          'launchd runs canonical sweep through cron guard')
    check(unit + '.plist' not in (routines / 'plists.defer').read_text().splitlines(), 'sweep is not deferred')
    service = (routines / (unit + '.service')).read_text()
    timer = (routines / (unit + '.timer')).read_text()
    check('routines/cron-guard.sh' in service and 'routines/handoff-stale-sweep.sh' in service
          and 'OnCalendar=*-*-* 08:40:00' in timer
          and 'Unit=com.telegram-skill-bot.__AGENT_NAME__.handoff-stale-sweep.service' in timer,
          'systemd template mirrors daily guarded sweep')
    check('"handoff-stale-sweep" "routines/handoff-stale-sweep.sh" "*-*-* 08:40:00"'
          in (repo / 'install.sh').read_text(), 'Linux installer default timer is wired')

    # Run the real update against a minimal offline distribution and instance.
    distribution = work / 'repo'
    template = distribution / 'agents/.template/routines'
    template.mkdir(parents=True)
    shutil.copy2(repo / 'update.sh', distribution / 'update.sh')
    lib = distribution / 'scripts/pack/lib'
    lib.mkdir(parents=True)
    for name in ('semver_range.py', 'content_drift.py', 'bridge_dependencies.py'):
        shutil.copy2(repo / 'scripts/pack/lib' / name, lib / name)
    (distribution / 'VERSION').write_text('9.9.9-test\n')
    for path in routines.glob('*handoff-stale-sweep*'):
        shutil.copy2(path, template / path.name)
    instance = work / 'instance'
    for directory in ('config', 'bridge', 'routines', 'memory-engine', '.claude'):
        (instance / directory).mkdir(parents=True, exist_ok=True)
    (instance / 'PROFILE.md').write_text('# Test instance\n')
    (instance / 'config/agent.conf').write_text('AGENT_LANG=en\nSLUG=demo-agent\n')
    (instance / '.instance.conf').write_text(
        'DOGANY_AGENT_NAME=demo-agent\nDOGANY_AGENT_LABEL=demo-agent\nDOGANY_USER_LABEL=you\n'
        'DOGANY_AGENT_PREFIX=[agent]\nDOGANY_UPDATE_CHANNEL=release\nDOGANY_REPO_ROOT=' + str(distribution) + '\n')
    (instance / 'routines/handoff-stale-sweep.sh').write_text('# old owner-push copy\nexit 4\n')
    private_home = work / 'home'
    private_home.mkdir()
    update = subprocess.run(['bash', str(distribution / 'update.sh'), '--root', str(instance),
                             '--no-pull', '--yes'], env=dict(os.environ, HOME=str(private_home), DOGANY_LANG='en'),
                            capture_output=True, text=True)
    check(update.returncode == 0, 'real offline update succeeds' + (': ' + update.stderr[-500:] if update.returncode else ''))
    installed = instance / 'routines/handoff-stale-sweep.sh'
    check(installed.read_bytes() == (routines / installed.name).read_bytes(), 'update replaces old instance-shaped script')
    for suffix in ('.plist', '.service', '.timer'):
        path = instance / 'routines' / ('com.telegram-skill-bot.demo-agent.handoff-stale-sweep' + suffix)
        check(path.exists() and '__PROJECT_ROOT__' not in path.read_text()
              and '__AGENT_NAME__' not in path.read_text(), 'update renders and renames ' + suffix)
    check(not list((instance / 'routines').glob('*telegram-agent*')), 'update leaves no generic duplicate units')
print('%d passed, 0 failed' % passed)
PY
