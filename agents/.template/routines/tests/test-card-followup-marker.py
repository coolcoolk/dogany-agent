#!/usr/bin/env python3
# test-lane: fast
# test-component: card-followup
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: the v2 marker contract (relay once / silent) replaces the image-order cases the older card-followup suites still pin.
# test-card-followup-marker.py -- v2 contract: meal-add with MEAL_CARD_BEGIN and
# no CARD_POSTED -> one relay order; CARD_POSTED / old kit / workout-add -> silent;
# line-anchored matching; dedupe unchanged.
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
        'session_id': sid or 'marker-%d-%s' % (os.getpid(), os.urandom(3).hex()),
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

rc, out = run(REC + BLOCK)
check('MEAL_CARD block -> relay order', rc == 0 and 'MEAL_CARD_RELAY' in ctx(out))
rc, out = run(BLOCK + REC)
check('block before record -> relay order', 'MEAL_CARD_RELAY' in ctx(out))
rc, out = run(REC + BLOCK + 'CARD_POSTED\n')
check('CARD_POSTED with block -> silent', rc == 0 and out == '')
rc, out = run(REC + 'CARD_POSTED\n')
check('CARD_POSTED alone -> silent', rc == 0 and out == '')
rc, out = run(REC)
check('old kit (no marker) -> silent', rc == 0 and out == '')
rc, out = run('MEAL_CARD\nsent\n' + REC)
check('legacy bare MEAL_CARD -> silent', rc == 0 and out == '')
rc, out = run(REC + 'MEAL_CARD_BEGINX\n')
check('MEAL_CARD_BEGINX is not a marker -> silent', out == '')
rc, out = run('15808\tMEAL_CARD_BEGIN 샐러드\t200\n')
check('word inside food name -> silent', out == '')

wk = 'bash database/kit_life.sh workout-add 러닝 30'
rc, out = run('7\t러닝\t30\n' + BLOCK, command=wk)
check('workout-add -> silent', rc == 0 and out == '')

sid = 'dedupe-%d' % os.getpid()
rc, o1 = run(REC + BLOCK, sid=sid, turn='t1')
rc, o2 = run(REC + BLOCK, sid=sid, turn='t1')
check('dedupe: first orders, same turn second silent', o1 != '' and o2 == '')

if fails:
    for f in fails:
        print('FAIL', f)
    sys.exit(1)
print('PASS test-card-followup-marker (all checks)')
