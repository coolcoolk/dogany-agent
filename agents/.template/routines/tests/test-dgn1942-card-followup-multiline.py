#!/usr/bin/env python3
# test-lane: fast
# test-dgn1942-card-followup-multiline.py -- DGN-1942: card-followup.py's
# success check scans every stdout line for the record, not just line 1.
import json, os, subprocess, sys, tempfile

HOOK = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     '..', 'card-followup.py'))
fails = []


def check(name, ok):
    if not ok:
        fails.append(name)


def run(stdout, command='bash database/kit_life.sh meal-add 점심 쌀국수 450',
        sid=None, turn=None):
    payload = {
        'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
        'session_id': sid or 'dgn1942-%d-%s' % (os.getpid(), os.urandom(3).hex()),
        'prompt_uuid': turn or os.urandom(6).hex(),
        'tool_input': {'command': command},
        'tool_response': {'stdout': stdout, 'stderr': ''},
    }
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout.strip()


def ctx(out):
    try:
        return json.loads(out)['hookSpecificOutput']['additionalContext']
    except Exception:
        return ''

BLOCK = 'MEAL_CARD_BEGIN\ncard\nMEAL_CARD_END\nMEAL_CARD_RELAY relay once\n'

rec = '15808\t헤이준 소고기 쌀국수 (450g)\t234'
compound = '{"found": false}\n{"ok": true, "x": 1}\nAUTO-LINKED\t4\t제목\n' + rec + '\n' + BLOCK
rc, out = run(compound)
check('compound stdout, record mid -> exit 0', rc == 0)
check('compound stdout, record mid -> relay order', 'MEAL_CARD_RELAY' in ctx(out))

rc, out = run('AUTO-LINKED\t4\t제목\n' + rec + '\n' + BLOCK)
check('AUTO-LINKED first -> relay order', 'MEAL_CARD_RELAY' in ctx(out))

rc, out = run('{"found": false}\n{"ok": true}\nAUTO-LINKED\tx\ty\n' + BLOCK)
check('no record line -> exit 0', rc == 0)
check('no record line -> no order', out == '')

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-dgn1942-card-followup-multiline (all checks)')
