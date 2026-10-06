#!/usr/bin/env python3
"""DGN-1683 WP4: read-only facts about the mint flow, for shell callers.

Two questions the shell machinery must answer WITHOUT importing the flow engine:

  pipeline-check   may this mint_run.sh pipeline run start? (legacy retirement
                   enforcement, grill v4 "legacy retirement": the pipeline
                   refuses a controller-less run while mint-flow.json is active)
  receipt-verify   does this flow receipt prove that <root> is the unfinished
                   target of the flow that is being aborted? (teardown's
                   flow-receipt extension, grill v4 fix 8)

Both only READ config/mint-flow.json (atomic replace makes an unlocked read
safe) and never write. Exit codes:

  pipeline-check   0 -> prints "free" (no active flow) or "controlled"
                   3 -> refused (reason on stderr)
  receipt-verify   0 -> prints "FLOW" or "FLOW-STOPPED"; 1 -> not proven

Anything unreadable/unparseable while mint-flow.json exists is treated as
ACTIVE (fail closed): a corrupt flow file must not open the legacy path.
"""
import argparse
import json
import os
import sys
from pathlib import Path

TERMINAL_FREE = ('DONE', 'EXPIRED', 'CANCELLED', 'FAILED_EMPTY')   # nothing blocks a new run
ABORTING = ('ABORT_PENDING', 'CLEANING')


def flow_path(instance_root):
    return Path(instance_root) / 'config' / 'mint-flow.json'


def read_flow(instance_root):
    """(None, None) when there is no flow file; (flow, None) when readable;
    (None, 'unreadable') when it exists but cannot be judged."""
    p = flow_path(instance_root)
    if not p.exists():
        return None, None
    try:
        flow = json.loads(p.read_text())
        if not isinstance(flow, dict) or not isinstance(flow.get('state'), str):
            return None, 'unreadable'
        return flow, None
    except (OSError, ValueError):
        return None, 'unreadable'


def candidate_roots(instance_root=None, env=None):
    """The ordering-instance roots whose flow file could govern this run:
    the explicit --instance-root, $DOGANY_INSTANCE_ROOT, and the cwd when it
    is an instance root. A run that names none of them cannot be judged here
    (operator lane) -- stated limitation, not a silent skip."""
    env = os.environ if env is None else env
    seen, out = set(), []
    cands = [instance_root, env.get('DOGANY_INSTANCE_ROOT')]
    if (Path.cwd() / '.instance.conf').is_file():
        cands.append(str(Path.cwd()))
    for c in cands:
        if c and os.path.realpath(c) not in seen:
            seen.add(os.path.realpath(c))
            out.append(c)
    return out


def pipeline_check(instance_root, slug, env=None):
    """-> ('free'|'controlled', None) or (None, reason)."""
    env = os.environ if env is None else env
    flow_id, nonce = env.get('DOGANY_MINT_CONTROLLER_FLOW'), env.get('DOGANY_MINT_CONTROLLER_NONCE')
    for root in candidate_roots(instance_root, env):
        flow, err = read_flow(root)
        if err:
            return None, 'mint-flow.json under %s cannot be read (fail closed)' % root
        if flow is None or flow['state'] in TERMINAL_FREE:
            continue
        if not (flow_id and nonce):
            return None, ('a mint flow (%s) is active at %s and this run has no controller: '
                          'use the mint verb, not the legacy pipeline' % (flow['state'], root))
        worker = flow.get('worker') if isinstance(flow.get('worker'), dict) else {}
        target = flow.get('target') if isinstance(flow.get('target'), dict) else {}
        if flow.get('flow_id') != flow_id or worker.get('nonce') != nonce:
            return None, 'controller credentials do not match the active flow at %s' % root
        if flow['state'] != 'BUILDING':
            return None, 'the active flow at %s is %s, not BUILDING: no pipeline run may start' % (root, flow['state'])
        if target.get('slug') and target['slug'] != slug:
            return None, 'the active flow at %s owns target %s, not %s' % (root, target['slug'], slug)
        return 'controlled', None
    if flow_id or nonce:
        return None, 'controller credentials were supplied but no matching active flow exists'
    return 'free', None


def receipt_verify(receipt_path, root, instance_root):
    """-> 'FLOW' | 'FLOW-STOPPED' | None. Proves: the receipt names THIS root,
    the flow that wrote it is the one now aborting, and the flow file agrees."""
    try:
        rc = json.loads(Path(receipt_path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rc, dict) or rc.get('schema') != 1 or not rc.get('flow_id') or not rc.get('slug'):
        return None
    if os.path.realpath(str(rc.get('root') or '/nonexistent')) != os.path.realpath(root):
        return None
    flow, err = read_flow(instance_root)
    if err or flow is None or flow.get('flow_id') != rc['flow_id'] or flow['state'] not in ABORTING:
        return None
    target = flow.get('target') if isinstance(flow.get('target'), dict) else {}
    if target.get('slug') != rc['slug']:
        return None
    return 'FLOW-STOPPED' if rc.get('newborn_stopped') is True else 'FLOW'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='mint_flow_guard', add_help=False)
    sub = ap.add_subparsers(dest='cmd')
    a = sub.add_parser('pipeline-check')
    a.add_argument('--instance-root')
    a.add_argument('--slug', default='')
    b = sub.add_parser('receipt-verify')
    b.add_argument('--receipt', required=True)
    b.add_argument('--root', required=True)
    b.add_argument('--instance-root', required=True)
    args = ap.parse_args(argv)
    if args.cmd == 'pipeline-check':
        verdict, reason = pipeline_check(args.instance_root, args.slug)
        if verdict is None:
            print('[mint-flow-guard] REFUSE: ' + reason, file=sys.stderr)
            return 3
        print(verdict)
        return 0
    if args.cmd == 'receipt-verify':
        ev = receipt_verify(args.receipt, args.root, args.instance_root)
        if ev is None:
            return 1
        print(ev)
        return 0
    print('usage: mint_flow_guard.py {pipeline-check|receipt-verify} ...', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
