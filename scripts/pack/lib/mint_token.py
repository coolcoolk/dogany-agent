#!/usr/bin/env python3
"""DGN-1683 WP2: token adapter for the mint verb (TokenAdapter seam of mint_flow.py).

check(raw_input, flow, ctx) sees the owner's full pasted text in memory only:
  1. extract exactly one bot token (a whole BotFather message is fine)
  2. shape check                      -> TOKEN_FORMAT   (0 or >1 distinct candidates)
  3. getMe through an injectable transport, bounded retry on network trouble
                                      -> TOKEN_INVALID  (Telegram refused the token)
                                      -> TOKEN_NETWORK  (still unreachable after retries)
  4. reuse check on the numeric bot id against the ordering bot and every managed
     instance bot                     -> TOKEN_REUSED   (inventory unreadable -> blocked)
  5. attempt to delete the owner's token message (ctx.message_id in ctx.chat_id)
                                      -> TOKEN_DELETE_FAILED is log-only, never a screen
     DGN-1844: the delete record (token_msg) rides on EVERY result that saw a
     token, accepted or not, so the engine persists it and retries a failed
     delete on later verb calls; it names why Telegram refused (`error`). A
     first attempt lost to the network is retried once more as soon as getMe
     has proven Telegram reachable.

Outcome -> engine result (mint_flow.Engine._token understands exactly these):
  accepted            {"kind": "accepted", "token_msg": {...}, "bot": {...}, "credential": Secret}
  TOKEN_FORMAT        {"kind": "not_accepted", "screen": "N2", "facts": {"outcome": ...}}
  TOKEN_INVALID       {"kind": "not_accepted", "screen": "N3", ...}
  TOKEN_REUSED        {"kind": "not_accepted", "screen": "N4", "facts": {..display_name, username}}
  TOKEN_NETWORK /     {"kind": "not_accepted", "screen": "N5", ...}   (PROBLEM slot: N5 is free
  INVENTORY_UNAVAILABLE                                                since delete-failed is log-only)

The token never leaves this module except inside the Secret handed to the worker:
Secret has no repr/str/json form, every log line goes through a mask, transport
errors are stripped of URLs/messages (they embed the token), and nothing here is
written to disk.

Managed-bot inventory (existing registry of instance bots): the estate instance
scan in scripts/pack/lib/discover_instances.sh (_discover_instances: every root
carrying .instance.conf under $HOME/.dogany/agents, $HOME/dogany/*, .../agents/*,
.../poc/*), each instance's bot = numeric prefix of TELEGRAM_BOT_TOKEN in
<root>/.telegram_bot/.env (then <root>/runtime/.env) -- the same env order
routines/push.sh uses. The ordering bot is the same lookup on --instance-root.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Whole-token grammar (owner integration ruling I6): BotFather tails vary in
# length.  Keep the delimiter rule, but accept the documented 30..50 range.
# run of token characters is a malformed paste, not a token plus a tail.
_CANDIDATE_RE = re.compile(r'(?<![0-9A-Za-z_-])[0-9]+:[A-Za-z0-9_-]{30,50}(?![0-9A-Za-z_-])')
_ANY_TOKEN_RE = re.compile(r'\d{3,}:[A-Za-z0-9_-]{20,}')
_ENV_TOKEN_RE = re.compile(r'^([0-9]+):[A-Za-z0-9_-]{30,50}$')

OUTCOME_SCREEN = {
    'TOKEN_FORMAT': 'N2',
    'TOKEN_INVALID': 'N3',
    'TOKEN_REUSED': 'N4',
    'TOKEN_NETWORK': 'N5',
    'INVENTORY_UNAVAILABLE': 'N5',
}

GETME_ATTEMPTS = 3
DELETE_ATTEMPTS = 2
RETRY_DELAYS = (0.5, 1.5)
RETRY_AFTER_CAP = 5.0
HTTP_TIMEOUT = 5.0
DELETE_RETRY_CAP = 3            # total delete attempts recorded in token_msg
EARLIER_MAX = 4                 # older undeleted token messages kept for retry (DGN-1844)
ERROR_MAX = 120                 # characters of Telegram's refusal kept in token_msg


def mask(text):
    return _ANY_TOKEN_RE.sub('<token>', text)


def _log(event, **fields):
    if os.environ.get('DOGANY_MINT_FLOW_DEBUG'):
        line = json.dumps({'event': event, **fields}, sort_keys=True, default=str)
        print(mask(line), file=sys.stderr)


class Secret:
    """Holds the verified token for the worker's child environment only."""
    __slots__ = ('_v',)

    def __init__(self, value):
        object.__setattr__(self, '_v', value)

    def __setattr__(self, k, v):
        raise AttributeError('Secret is immutable')

    def reveal(self):
        return self._v

    def __repr__(self):
        return '<Secret>'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('Secret is not serializable')


def extract_token(text):
    """The distinct token candidates in text, in order of appearance."""
    seen = []
    for m in _CANDIDATE_RE.finditer(text or ''):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


# -------------------------------------------------------------- transport
class TransportError(Exception):
    """Network-class failure. Carries no message: URLs and reasons embed the token."""
    def __init__(self, retry_after=None):
        super().__init__('transport failure')
        self.retry_after = retry_after


class Transport:
    """call(method, token, params) -> the decoded Telegram JSON object
    ({"ok": bool, "result": .., "error_code": int, "parameters": {..}}), also for
    HTTP error statuses that carry a JSON body. Raises TransportError when there
    is no usable answer (DNS, timeout, reset, non-JSON, undecodable)."""
    def call(self, method, token, params=None):
        raise NotImplementedError


class UrlLibTransport(Transport):
    def __init__(self, base_url='https://api.telegram.org', timeout=HTTP_TIMEOUT):
        self.base_url, self.timeout = base_url.rstrip('/'), timeout

    def call(self, method, token, params=None):
        url = '%s/bot%s/%s' % (self.base_url, token, method)
        body = json.dumps(params).encode() if params else None
        req = urllib.request.Request(url, data=body, method='POST' if body else 'GET',
                                     headers={'Content-Type': 'application/json'} if body else {})
        try:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read(1 << 20)
            except urllib.error.HTTPError as exc:
                raw = exc.read(1 << 20)
            obj = json.loads(raw.decode('utf-8'))
        except Exception:                               # noqa: BLE001 -- message may hold the token
            raise TransportError() from None
        if not isinstance(obj, dict):
            raise TransportError()
        return obj


def _retry_after(obj):
    p = obj.get('parameters')
    ra = p.get('retry_after') if isinstance(p, dict) else None
    return ra if isinstance(ra, (int, float)) and not isinstance(ra, bool) else None


def _transient(obj):
    code = obj.get('error_code')
    return isinstance(code, int) and (code == 429 or code >= 500)


def _refusal(obj):
    """Telegram's own reason for a non-ok answer, safe to persist."""
    code = obj.get('error_code')
    desc = obj.get('description')
    desc = ' '.join(desc.split()) if isinstance(desc, str) else ''
    return mask('tg %s: %s' % (code if isinstance(code, int) else '?', desc))[:ERROR_MAX]


def unsettled(token_msg):
    """True while a token message may still sit in the chat and a retry can
    still remove it (an id is known, not deleted)."""
    t = token_msg if isinstance(token_msg, dict) else {}
    return t.get('delete_result') != 'deleted' and t.get('message_id') is not None \
        and t.get('chat_id') is not None


# -------------------------------------------------------------- inventory
class InventoryError(Exception):
    pass


def _env_token(path):
    """The TELEGRAM_BOT_TOKEN of an env file when it is a real token. None: no file or
    no real token (placeholder). InventoryError: exists but unreadable."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError):
        raise InventoryError('unreadable env') from None
    for line in text.splitlines():
        if line.startswith('TELEGRAM_BOT_TOKEN='):
            v = ''.join(line.split('=', 1)[1].split()).strip('"\'')
            return v if _ENV_TOKEN_RE.match(v) else None
    return None


def instance_token(root):
    """Bot token of one instance root, env order as routines/push.sh (None: no bot)."""
    for rel in ('.telegram_bot/.env', 'runtime/.env'):
        tok = _env_token(Path(root) / rel)
        if tok is not None:
            return tok
    return None


def instance_bot_id(root):
    tok = instance_token(root)
    return int(tok.split(':', 1)[0]) if tok else None


def discover_roots(lib_dir=None):
    """Instance roots from the estate scan (discover_instances.sh, one truth source)."""
    lib = Path(lib_dir or Path(__file__).resolve().parent) / 'discover_instances.sh'
    try:
        p = subprocess.run(['bash', '-c', '. "$1"; _discover_instances', 'discover', str(lib)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise InventoryError('discovery failed') from None
    if p.returncode != 0:
        raise InventoryError('discovery failed')
    roots = []
    for line in p.stdout.splitlines():
        if ':' in line:
            roots.append(line.split(':', 1)[1])
    return roots


class Inventory:
    """ordering bot id + managed bot ids. Incomplete/unreadable -> InventoryError."""
    def __init__(self, instance_root, roots_fn=discover_roots):
        self.instance_root, self.roots_fn = instance_root, roots_fn

    def ids(self):
        ordering = instance_bot_id(self.instance_root)
        if ordering is None:
            raise InventoryError('ordering bot unreadable')
        managed = set()
        for root in self.roots_fn():
            bid = instance_bot_id(root)
            if bid is not None:
                managed.add(bid)
        return ordering, managed

    def ordering_token(self):
        try:
            return instance_token(self.instance_root)
        except InventoryError:
            return None


# ---------------------------------------------------------------- adapter
def _clean(v, n=64):
    return mask(str(v))[:n]


class TokenAdapter:
    """Real WP2 adapter. Drop-in for mint_flow.TokenAdapter:
        Adapters(token=TokenAdapter(instance_root))"""
    def __init__(self, instance_root, transport=None, inventory=None, sleep=time.sleep):
        self.instance_root = instance_root
        self.transport = transport or UrlLibTransport()
        self.inventory = inventory or Inventory(instance_root)
        self.sleep = sleep

    # -- outward-facing
    def check(self, raw_input, flow, ctx):
        cands = extract_token(raw_input)
        if len(cands) != 1:
            return self._refuse('TOKEN_FORMAT', flow, ctx, deletable=len(cands) > 1)
        token = cands[0]
        prefix = int(token.split(':', 1)[0])
        tmsg = self._delete_token_message(ctx)          # secret is in the chat: remove it first
        me = self._get_me(token)
        if me == 'network':
            return self._refuse('TOKEN_NETWORK', flow, ctx, tmsg=tmsg)
        if tmsg.get('delete_result') == 'failed' and tmsg.get('error') == 'transport':
            # getMe just reached Telegram: a first delete lost to the network
            # gets its second attempt now, not a turn later. A definitive
            # Telegram refusal is left to the next verb call.
            tmsg = self._retry_one(tmsg)
        if me is None or me.get('id') != prefix:
            return self._refuse('TOKEN_INVALID', flow, ctx, tmsg=tmsg)
        try:
            ordering, managed = self.inventory.ids()
        except InventoryError:
            return self._refuse('INVENTORY_UNAVAILABLE', flow, ctx, tmsg=tmsg)
        if prefix == ordering or prefix in managed:
            return self._refuse('TOKEN_REUSED', flow, ctx, tmsg=tmsg, bot=me)
        return {'kind': 'accepted', 'token_msg': tmsg,
                'bot': {'id': me['id'], 'username': _clean(me['username']),
                        'display_name': _clean(me.get('first_name') or '')},
                'credential': Secret(token)}

    def retry_delete(self, token_msg, ctx=None):
        """Bounded later retry (next verb call). Returns the updated token_msg dict;
        idempotent once deleted, capped at DELETE_RETRY_CAP total attempts.
        Older undeleted token messages kept under `earlier` (DGN-1844: a
        second paste must not drop the first one's retry) are retried too."""
        t = self._retry_one(token_msg)
        earlier = [self._retry_one(e) for e in (t.get('earlier') or []) if isinstance(e, dict)]
        earlier = [e for e in earlier if unsettled(e)][-EARLIER_MAX:]
        if earlier:
            t['earlier'] = earlier
        else:
            t.pop('earlier', None)
        return t

    def _retry_one(self, token_msg):
        t = dict(token_msg or {})
        if t.get('delete_result') == 'deleted' or t.get('attempts', 0) >= DELETE_RETRY_CAP:
            return t
        if t.get('message_id') is None or t.get('chat_id') is None:
            return t
        ok, why = self._delete(t['chat_id'], t['message_id'])
        t['attempts'] = t.get('attempts', 0) + 1
        t['delete_result'] = 'deleted' if ok else 'failed'
        if ok:
            t.pop('error', None)
        else:
            t['error'] = why
            _log('token.delete_failed', message_id=t['message_id'], attempts=t['attempts'], error=why)
        return t

    # -- internals
    def _refuse(self, outcome, flow, ctx, tmsg=None, bot=None, deletable=False):
        if deletable and tmsg is None:                  # several tokens pasted: still secrets
            tmsg = self._delete_token_message(ctx)
        facts = {'outcome': outcome}
        if bot is not None:
            facts['display_name'] = _clean(bot.get('first_name') or '')
            facts['username'] = _clean(bot['username'])
        _log('token.refused', outcome=outcome)
        res = {'kind': 'not_accepted', 'screen': OUTCOME_SCREEN[outcome], 'facts': facts}
        if tmsg is not None:
            res['token_msg'] = tmsg                     # DGN-1844: the engine keeps it for retry
        return res

    def _get_me(self, token):
        """dict(id, username, first_name) | None (token refused) | 'network'."""
        for attempt in range(GETME_ATTEMPTS):
            try:
                obj = self.transport.call('getMe', token)
            except TransportError as exc:
                delay = self._delay(attempt, exc.retry_after)
            else:
                if not obj.get('ok') and _transient(obj):
                    delay = self._delay(attempt, _retry_after(obj))
                else:
                    return self._parse_me(obj)
            if attempt + 1 < GETME_ATTEMPTS:
                self.sleep(delay)
        return 'network'

    @staticmethod
    def _delay(attempt, retry_after):
        d = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        if retry_after is not None:
            d = max(d, min(float(retry_after), RETRY_AFTER_CAP))
        return d

    @staticmethod
    def _parse_me(obj):
        r = obj.get('result')
        if obj.get('ok') is not True or not isinstance(r, dict):
            return None
        bid, uname = r.get('id'), r.get('username')
        if r.get('is_bot') is not True or isinstance(bid, bool) or not isinstance(bid, int) \
                or not isinstance(uname, str) or not uname:
            return None
        fn = r.get('first_name')
        return {'id': bid, 'username': uname, 'first_name': fn if isinstance(fn, str) else ''}

    def _delete_token_message(self, ctx):
        """Never raises. -> token_msg {message_id, chat_id, delete_result, attempts}."""
        mid, chat = getattr(ctx, 'message_id', None), getattr(ctx, 'chat_id', None)
        mid = self._num(mid)
        chat = self._num(chat)
        if mid is None or chat is None:
            _log('token.delete_failed', reason='no_id')
            return {'message_id': mid, 'chat_id': chat, 'delete_result': 'no_id', 'attempts': 0}
        ok, why = self._delete(chat, mid)
        t = {'message_id': mid, 'chat_id': chat,
             'delete_result': 'deleted' if ok else 'failed', 'attempts': 1}
        if not ok:
            t['error'] = why
            _log('token.delete_failed', message_id=mid, attempts=1, error=why)
        return t

    @staticmethod
    def _num(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    def _delete(self, chat_id, message_id):
        """-> (True, None) | (False, why). `why` names the refusal without any
        secret: 'no_ordering_token', 'transport', 'adapter_error', or
        'tg <error_code>: <description>' (masked, capped) as Telegram said it."""
        tok = self.inventory.ordering_token() if hasattr(self.inventory, 'ordering_token') else None
        if tok is None:
            return False, 'no_ordering_token'
        why = 'transport'
        for attempt in range(DELETE_ATTEMPTS):
            try:
                obj = self.transport.call('deleteMessage', tok,
                                          {'chat_id': chat_id, 'message_id': message_id})
            except TransportError:
                obj, why = None, 'transport'
            except Exception:                           # noqa: BLE001 -- fake/odd transports
                return False, 'adapter_error'
            if obj is not None:
                if obj.get('ok') is True:
                    return True, None
                why = _refusal(obj)
                if not _transient(obj):
                    return False, why
            if attempt + 1 < DELETE_ATTEMPTS:
                self.sleep(RETRY_DELAYS[0])
        return False, why
