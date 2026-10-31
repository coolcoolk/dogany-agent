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


def run(root, stdout, command='bash database/kit_life.sh meal-add 점심 쌀국수 450'):
    payload = {
        'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'cwd': root,
        'session_id': 'dgn1942-%d' % os.getpid(),
        'prompt_uuid': os.urandom(6).hex(),
        'tool_input': {'command': command},
        'tool_response': {'stdout': stdout, 'stderr': ''},
    }
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout.strip()


with tempfile.TemporaryDirectory() as d:
    card = os.path.join(d, '.claude/skills/diet-status/card.py')
    os.makedirs(os.path.dirname(card))
    open(card, 'w').write('# stub\n')

    rec = '15808\t헤이준 소고기 쌀국수 (450g)\t234'
    compound = '{"found": false}\n{"ok": true, "x": 1}\nAUTO-LINKED\t4\t제목\n' + rec + '\n'
    rc, out = run(d, compound)
    check('compound stdout, record last -> exit 0', rc == 0)
    check('compound stdout, record last -> order', card in out)

    rc, out = run(d, 'AUTO-LINKED\t4\t제목\n' + rec + '\n')
    check('AUTO-LINKED first -> order', card in out)

    rc, out = run(d, '{"found": false}\n{"ok": true}\nAUTO-LINKED\tx\ty\n')
    check('no record line -> exit 0', rc == 0)
    check('no record line -> no order', out == '')

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-dgn1942-card-followup-multiline (all checks)')
