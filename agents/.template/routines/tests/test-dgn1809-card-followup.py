#!/usr/bin/env python3
# test-lane: fast
# test-dgn1809-card-followup.py -- card-followup v2 (supersedes the DGN-1809
# card.py order): the hook never orders an image or a renderer, whether or not
# a diet-status renderer is installed; the only order is the text relay.
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
        'session_id': sid or 'dgn1809-%d-%s' % (os.getpid(), os.urandom(3).hex()),
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

REC = '15808\t쌀국수\t234\n'
BLOCK = 'MEAL_CARD_BEGIN\ncard\nMEAL_CARD_END\nMEAL_CARD_RELAY relay once\n'

src = open(HOOK).read()
for word in ('card.py', 'send_file', 'matplotlib'):
    check('hook source has no %s' % word, word not in src)

with tempfile.TemporaryDirectory() as d:
    stub = os.path.join(d, '.claude/skills/diet-status/card.py')
    os.makedirs(os.path.dirname(stub))
    open(stub, 'w').write('# stub\n')
    payload = {
        'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'cwd': d,
        'session_id': 'dgn1809-%d' % os.getpid(), 'prompt_uuid': os.urandom(4).hex(),
        'tool_input': {'command': 'bash database/kit_life.sh meal-add a b 1'},
        'tool_response': {'stdout': REC + BLOCK, 'stderr': ''},
    }
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=10)
    c = ctx(p.stdout.strip())
    check('installed renderer -> still relay order', 'MEAL_CARD_RELAY' in c)
    for word in ('card.py', 'send_file', 'matplotlib', 'render', stub):
        check('order text has no %s' % word, word not in c)

rc, out = run(REC + BLOCK, command='bash database/kit_life.sh meal-day')
check('read-only call -> silent', rc == 0 and out == '')
rc, out = run(REC + BLOCK, command='echo hi')
check('non-record command -> silent', out == '')

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-dgn1809-card-followup (all checks)')
