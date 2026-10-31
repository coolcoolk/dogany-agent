#!/usr/bin/env python3
# test-lane: fast
# test-dgn1809-card-followup.py -- DGN-1809: card-followup.py orders the
# diet-status card ONLY when .claude/skills{,-bundle}/diet-status/card.py is
# installed, naming that path; otherwise silent. No text fallback, and the
# retired diet-log/card.py path is never ordered.
import json, os, subprocess, sys, tempfile

HOOK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     '..', 'card-followup.py'))
fails = []


def check(name, ok):
    if not ok:
        fails.append(name)


def run(root, command='bash database/kit_life.sh meal-add 점심 김치찌개 520',
        stdout='12\t김치찌개\t520\n'):
    payload = {
        'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'cwd': root,
        'session_id': 'dgn1809-%d' % os.getpid(),
        'prompt_uuid': os.urandom(6).hex(),
        'tool_input': {'command': command},
        'tool_response': {'stdout': stdout, 'stderr': ''},
    }
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout.strip()


def place(root, rel):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').write('# stub\n')
    return path


with tempfile.TemporaryDirectory() as d:
    # (1) nothing installed -> silent (incl. a stale diet-log/card.py)
    place(d, '.claude/skills/diet-log/card.py')
    rc, out = run(d)
    check('no diet-status card -> exit 0', rc == 0)
    check('no diet-status card -> no order', out == '')

for rel in ('.claude/skills/diet-status/card.py',
            '.claude/skills-bundle/diet-status/card.py'):
    with tempfile.TemporaryDirectory() as d:
        card = place(d, rel)
        rc, out = run(d)
        check('%s -> exit 0' % rel, rc == 0)
        ctx = ''
        try:
            ctx = json.loads(out)['hookSpecificOutput']['additionalContext']
        except Exception:
            pass
        check('%s -> order fires' % rel, bool(ctx))
        check('%s -> names the installed path' % rel, card in ctx)
        check('%s -> no diet-log path' % rel, 'diet-log/card.py' not in ctx)
        check('%s -> no text fallback' % rel, 'as text' not in ctx)
        # workout-add orders too; a read-only call does not
        rc, out = run(d, command='bash database/kit_life.sh workout-add run 30',
                      stdout='7\trun\t30\n')
        check('%s -> workout-add order' % rel, bool(out))
        rc, out = run(d, command='bash database/kit_life.sh meal-day')
        check('%s -> read-only call silent' % rel, out == '')

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-dgn1809-card-followup (all checks)')
