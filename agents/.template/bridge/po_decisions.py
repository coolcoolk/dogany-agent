"""Bridge-owned PO menus and durable authenticated owner input.

The model never receives a confirmation tool. The pack responder invokes the
normal ledger confirmer using the original bridge request and native message.
"""
# ESTATE:BEGIN PO seat uses installed pack custody and native decision writers
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unicodedata


def frozen(root, key, chat, text):
    """Project a pack-frozen menu; plain availability notices have no menu."""
    root = Path(root)
    database = root / '.telegram_bot/po-seat/seat.sqlite3'
    if not database.exists():
        return None
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT home,body,receipt FROM decisions WHERE key=?', (key,)).fetchone()
    if not row:
        return None
    home, body, receipt = row
    body, receipt = json.loads(body), json.loads(receipt)
    if Path(home).resolve() != root.resolve() or str(body['chat']) != str(chat) or body['text'] != text:
        raise ValueError('PO notice differs from frozen decision')
    if not receipt.get('id') and 'menu' not in body and any(
            field in receipt for field in ('availability', 'terminal', 'job_limit')):
        return None
    labels = receipt.get('labels')
    if (not re.fullmatch(r'dec-[0-9]+', receipt.get('id', '')) or
            not isinstance(labels, list) or not 2 <= len(labels) <= 10 or
            labels != body['menu']['labels'] or any(not isinstance(s, str) or not s for s in labels)):
        raise ValueError('PO notice differs from frozen decision')
    return dict(key=key, id=receipt['id'], labels=labels)


def buttons(menu):
    return [[dict(text=label, callback_data='po:%s:%d' % (menu['id'], i))]
            for i, label in enumerate(menu['labels'], 1)]


def callback_prompt(root, chat, message_id, data):
    """A tap must name a delivered menu and its exact native message."""
    from bridge.live_surface import Coordinator
    match = re.fullmatch(r'po:(dec-[0-9]+):([1-9][0-9]?)', data or '')
    if not match:
        return None
    for notice in Coordinator(root).control(chat)['notices'].values():
        delivery = notice.get('delivery') or {}
        intended = notice.get('intended') or {}
        payload = intended.get('payload') or {}
        menu = payload.get('po_decision') or {}
        if (intended.get('notification_class') == 'owner_decision' and notice.get('status') == 'ok' and
                delivery.get('chat_id') == chat and delivery.get('message_id') == message_id and
                menu.get('id') == match[1] and int(match[2]) <= len(menu.get('labels', []))):
            return '%s: %s' % (match[1], match[2])
    return None


def installed(root):
    root = Path(root)
    return ((root / 'config/packs/agent_po.receipt').is_file() and
            (root / 'service/agent_po/pm-seat-owner-response.py').is_file())


def record_input(root, session, prompt, context):
    """Persist before execution. Errors retain the original request for retry."""
    from bridge.live_surface import atomic, read
    if not installed(root):
        return None
    if (not isinstance(prompt, str) or not isinstance(context, dict) or
            context.get('text_sha') != hashlib.sha256(unicodedata.normalize('NFC', prompt).encode()).hexdigest() or
            not re.fullmatch(r'[A-Za-z0-9._:-]+', context.get('request_id', ''))):
        raise ValueError('PO owner input lacks bridge evidence')
    key = hashlib.sha256(context['request_id'].encode()).hexdigest()
    directory = Path(root) / '.telegram_bot/po-owner-input' / key
    payload = dict(hook_event_name='UserPromptSubmit', session_id=session or 'bridge-owner', prompt=prompt)
    existing = read(directory / 'input.json')
    if existing is not None and existing != payload:
        raise ValueError('PO owner request changed')
    if existing is None:
        atomic(directory / 'inbound-context.json', context)
        atomic(directory / 'input.json', payload)
    return retry_input(root, directory)


def retry_input(root, directory):
    from bridge.live_surface import read
    prior = read(Path(directory) / 'result.json')
    if prior is not None:
        return prior
    env = dict(os.environ, PROJECT_ROOT=str(root), DECISION_LEDGER_ROOT=str(root), DOGANY_SESSION='owner')
    # A model worker must never use this bridge-owned confirmation path.
    if env.get('DOGANY_DISPATCH_WORKER'):
        raise ValueError('PO owner input cannot run inside a model worker')
    result = subprocess.run([sys.executable, '-B', '-m', 'bridge.po_decisions', 'consume',
                             str(root), str(directory)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError('PO responder retained request: ' + result.stderr[-400:])
    return json.loads(result.stdout)


def retry_pending(root):
    """The normal bridge maintenance beat retries durable owner input."""
    import logging
    for path in sorted((Path(root) / '.telegram_bot/po-owner-input').glob('*/input.json')):
        if (path.parent / 'result.json').exists():
            continue
        try:
            retry_input(root, path.parent)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            logging.getLogger(__name__).warning('PO owner input remains pending: %s', error)


def consume(root, directory):
    """Isolate per-request evidence from overlapping live turns.

    The native writer's reader sees the original timestamp and hash. Retry
    never renews stale evidence or changes the ledger confirmer or request ID.
    """
    from bridge.live_surface import atomic, read
    directory = Path(directory).resolve()
    directory.relative_to(Path(root).resolve() / '.telegram_bot/po-owner-input')
    path = Path(root) / 'service/agent_po/pm-seat-owner-response.py'
    spec = importlib.util.spec_from_file_location('bridge_po_responder', path)
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    native_load = handler.load

    def load(name, path):
        writer = native_load(name, path)
        if name == 'seat_owner_ledger':
            read_inbound = writer.fmt.read_inbound
            writer.fmt.read_inbound = lambda bot_dir, now_ts: read_inbound(str(directory), now_ts)
        return writer

    handler.load = load
    import fcntl
    with open(directory / 'consume.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = read(directory / 'result.json')
        if prior is not None:
            return prior
        import time
        # A stale request stays held; a retry cannot renew its authentication.
        writer = native_load('retry_native_ledger', Path(root) / 'routines/decision-ledger.py')
        if not writer.fmt.read_inbound(str(directory), time.time()):
            raise ValueError('original owner evidence expired; request retained')
        result = handler.handle(root, read(directory / 'input.json'))
        atomic(directory / 'result.json', result)
        return result


if __name__ == '__main__':
    if sys.argv[1:2] != ['consume'] or len(sys.argv) != 4:
        sys.exit(2)
    print(json.dumps(consume(sys.argv[2], sys.argv[3]), sort_keys=True))

# ESTATE:ELSE
# def installed(root):
#     return False
#
# def frozen(root, key, chat, text):
#     return None
#
# def buttons(menu):
#     return []
#
# def callback_prompt(root, chat, message_id, data):
#     return None
#
# def retry_pending(root):
#     return None
# ESTATE:END
