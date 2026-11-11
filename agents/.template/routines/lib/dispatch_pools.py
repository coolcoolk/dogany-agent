"""DGN-2345: read dispatch resource books at turn time, without live probes.

Only the quota reader publishes codex-quota.json. Everything else comes from
existing dispatcher snapshots, account pointers, pass stores and lane caps.
Missing/stale evidence is unknown; a held pass never bypasses a hard gate.
"""
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from conf_reader import conf_get
import reset_passes
import api_credit_pool as api
import resource_settings

CODEX_TTL = 300
CODEX_LAST_KNOWN_TTL = 86400  # older than a day -> unknown; between TTLs -> shown with its age
LANE_TTL = 900  # host_dispatcher.LANE_CAP_TTL_S


def read_json(path):
    try:
        doc = json.loads(Path(path).read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def shared_home():
    return Path(os.environ.get('DOGANY_SHARED_HOME') or Path.home() / '.dogany')


def codex_cache_path():
    return shared_home() / 'usage/codex-quota.json'


def codex_auth_stamp():
    # Metadata only: never open auth.json or a credential store in a hook.
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'
    try:
        st = path.stat()
        return [str(path), st.st_mtime_ns, st.st_size]
    except OSError:
        return [str(path), None, None]


def publish_codex(result):
    """Cache only public quota fields, including failures to invalidate a hit.

    Dispatch still probes live. Its ordinary quota reader publishes this
    bounded-age view for passive surfaces; no hook launches an app-server.
    """
    path = codex_cache_path()
    fields = ('ok', 'used_percent', 'resets_at', 'window_duration_mins')
    doc = {k: result[k] for k in fields if k in result}
    doc.update(fetched_at=time.time(), auth_stamp=codex_auth_stamp())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(doc, stream)
        temp.replace(path)
    except OSError:
        if 'temp' in locals():
            temp.unlink(missing_ok=True)


def fresh(at, ttl, now):
    try:
        return 0 <= now - float(at) <= float(ttl) < math.inf
    except (TypeError, ValueError):
        return False


def percent(value):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= 100)


def future(value, now):
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            stamp = dt.timestamp() if dt.tzinfo else 0
        except ValueError:
            return False
    return math.isfinite(stamp) and stamp > now


def snapshot(now):
    folder = Path(os.environ.get('DOGANY_USAGE_SNAPSHOT_DIR') or Path.home() / '.dogany/usage')
    path = folder / 'snapshot.json'
    try:
        if fresh(path.stat().st_mtime,
                 os.environ.get('DOGANY_USAGE_SNAPSHOT_TTL_SEC', '300'), now):
            return read_json(path)
    except OSError:
        pass
    return {}


def pass_state(runtime, conf, now):
    key = reset_passes.CONF_KEY[runtime]
    spec = os.environ.get(key) or conf_get(key, conf)
    base = Path.home() / reset_passes.STORE_ROOT / runtime
    book = read_json(base / ('.book-reset-passes-' + runtime + '.json'))
    account = book.get('components', {}).get(runtime + '.account')
    if not account:
        path = (shared_home() / 'books/codex-quota/.book-codex-quota.json'
                if runtime == 'codex' else
                Path(os.environ.get('DOGANY_USAGE_SNAPSHOT_DIR') or Path.home() / '.dogany/usage') / '.book-claude-usage.json')
        account = read_json(path).get('components', {}).get(runtime + '.account')
    known_empty = False
    if not spec and account:
        doc = read_json(base / account / reset_passes.STORE_FILE)
        if doc and not doc.get('migration_error'):
            spec = reset_passes.store_spec(base / account / reset_passes.STORE_FILE)
            known_empty = True
        else:
            return {'count': None, 'expiry': None}, account
    if not spec and not known_empty:
        return {'count': None, 'expiry': None}, account
    try:
        reset_passes.parse_passes(spec)
    except ValueError:
        return {'count': None, 'expiry': None}, account
    return reset_passes.state(spec, now), account


def cached_plan(now):
    cache = read_json(shared_home() / 'books/identity-cache.json')
    # The CLI owns refreshing these entries. No credential reads here.
    import book_identity
    for path in book_identity._claude_cred_paths(os.environ, str(Path.home())):
        key = 'cred:' + path
        row = cache.get(key)
        if isinstance(row, dict):
            try:
                st = Path(key[5:]).stat()
                if row.get('key') == [st.st_mtime_ns, st.st_size] and row.get('value'):
                    return row['value']
            except OSError:
                pass
    row = cache.get('keychain') or {}
    if isinstance(row, dict) and fresh(row.get('at'), book_identity.KEYCHAIN_RECHECK_S, now):
        return row.get('value') or ''
    return ''


def _read_pools(workspace=None, now=None):
    """One read-only resource view; no run.env, network, credential or writes."""
    now = time.time() if now is None else now
    workspace = Path(workspace or Path(__file__).resolve().parents[2])
    conf = workspace / 'config/agent.conf'
    dispatch = workspace / '.telegram_bot/dispatch'
    def val(key):
        return os.environ.get(key) or conf_get(key, conf)
    books = api.instance_books(str(dispatch))
    api.configure_reset_day(os.environ.get('API_CREDIT_RESET_DAY'), ledger=books['ledger'],
                            legacy=conf_get('API_CREDIT_RESET_DAY', conf), migrate=False)
    pools = {k: {'state': 'unknown'} for k in ('claude', 'codex', 'api')}
    doc = snapshot(now)
    cp, claude_account = pass_state('claude', conf, now)
    xp, codex_account = pass_state('codex', conf, now)
    c = pools['claude']
    c['passes'] = cp
    for key, label in (('five_hour', '5h'), ('seven_day', '7d')):
        row = doc.get(key) or {}
        used = row.get('utilization') if isinstance(row, dict) else None
        c[label] = used if percent(used) and future(row.get('resets_at'), now) else None
    absent5 = 'five_hour' in doc and doc['five_hour'] is None
    if (absent5 or c['5h'] is not None) and c['7d'] is not None:
        try:
            c['state'] = ('closed' if (not absent5 and c['5h'] >= float(val('LOOP_CAP_FIVE_HOUR') or 90))
                          or c['7d'] >= float(val('LOOP_CAP_SEVEN_DAY') or 95) else 'open')
        except ValueError:
            pass
    x = pools['codex']
    x['passes'] = xp
    ceiling = resource_settings.get('codex', codex_account)['max_used_percent']
    quota = read_json(codex_cache_path())
    if (quota.get('ok') is True and quota.get('auth_stamp') == codex_auth_stamp()
            and fresh(quota.get('fetched_at'), CODEX_LAST_KNOWN_TTL, now)
            and type(quota.get('used_percent')) is int
            and percent(quota['used_percent']) and future(quota.get('resets_at'), now)
            and quota.get('window_duration_mins') == 10080):
        x.update(used=quota['used_percent'], resets_at=quota['resets_at'],
                 state='open' if quota['used_percent'] <= ceiling else 'closed')
        if not fresh(quota.get('fetched_at'), CODEX_TTL, now):
            x['age'] = now - float(quota['fetched_at'])
    a = pools['api']
    budget, source = api.resolve_budget(val('API_CREDIT_MONTHLY_USD'), val('PLAN'),
                                       val('API_CREDIT_SEATS'), val('API_CREDIT_SEAT_USD'), cached_plan(now))
    evidence = (api.exhausted_marker(books['marker'], now)
                or api.reconcile_doc(books['reconcile'], now)
                or Path(books['ledger']).is_file())
    if budget is not None and math.isfinite(budget) and evidence:
        st = api.pool_state(budget, books['ledger'], api._reserve(val('API_CREDIT_RESERVE_USD')),
                            now, books['marker'], books['reconcile'], source, books['inflight'],
                            read_only=True)
        a.update(state='open' if api.admit(st)[0] else 'closed',
                 remaining=st['remaining'], exhausted=st['exhausted'],
                 reset_at=st['reset_at'])
    accounts = {'claude-code': claude_account,
                'codex': codex_account,
                'claude-api': Path(books['ledger']).parent.name}
    caps = {}
    for lane, row in read_json(shared_home() / 'dispatcher/lanes.json').items():
        runtime, _, account = lane.partition('@')
        if (account != accounts.get(runtime) or not isinstance(row, dict)
                or not fresh(row.get('at'), LANE_TTL, now)):
            continue
        cap = row.get('cap')
        if type(cap) is int and cap >= 0:
            caps[runtime] = cap
    names = {'claude-code': 'claude', 'codex': 'codex',
             'claude-api': 'api'}
    for name, pool in pools.items():
        lane_caps = [cap for rt, cap in caps.items() if names.get(rt) == name]
        if pool['state'] == 'open' and lane_caps and max(lane_caps) == 0:
            pool.update(state='closed', cap=0)
    opened = [k for k, p in pools.items() if p['state'] == 'open']
    open_caps = [cap for rt, cap in caps.items() if names.get(rt) in opened]
    all_closed = all(p['state'] == 'closed' for p in pools.values())
    return {'pools': pools, 'open': opened, 'verdict': 'parallel OK' if opened else 'hold',
            'caps': caps, 'cap': max(open_caps) if open_caps else (0 if all_closed else None)}



def read_pools(workspace=None, now=None):
    """A damaged book must not remove the existing Claude turn context."""
    try:
        return _read_pools(workspace, now)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        unknown = {k: {'state': 'unknown'} for k in ('claude', 'codex', 'api')}
        return {'pools': unknown, 'open': [], 'verdict': 'hold', 'caps': {}, 'cap': None}


def date_label(value):
    if value is None:
        return 'unknown'
    return reset_passes._label(value)[5:10]


def render(view, locale='en'):
    ko = locale == 'ko'
    states = {'open': '\uac00\ub2a5', 'closed': '\ub2eb\ud798', 'unknown': '\ud655\uc778 \ubd88\uac00'} if ko else {}
    parts = []
    for name in ('claude', 'codex', 'api'):
        p = view['pools'][name]
        label = states.get(p['state'], p['state'])
        details = []
        if name == 'claude':
            details += [f"{w} {p[w]:g}% " + ("\uc0ac\uc6a9" if ko else "used") for w in ('5h', '7d') if p.get(w) is not None]
        if name == 'codex' and 'used' in p:
            details.append(('%d%% \uc0ac\uc6a9' if ko else '%d%% used') % p['used'])
            if p.get('age') is not None:
                mins = int(p['age'] // 60)
                age = ('%dm' % mins) if mins < 120 else ('%dh' % (mins // 60))
                details.append(age + (' \uc804' if ko else ' old'))
        if name in ('claude', 'codex'):
            ps = p.get('passes') or {}
            n = ps.get('count')
            details.append(('\ub9ac\uc14b\uad8c ' if ko else 'passes ') + (str(n) if n is not None else '?'))
            if ps.get('expiry'):
                details.append(('\ub9cc\ub8cc ' if ko else 'expires ') + date_label(ps['expiry']))
        if 'remaining' in p:
            details.append('$%g' % round(p['remaining'], 2))
        if p.get('cap') == 0:
            details.append('cap 0' if not ko else '\ub3d9\uc2dc \ud55c\ub3c4 0')
        if p.get('exhausted'):
            label = '\uc794\uc561 \uc5c6\uc74c' if ko else 'no balance'
            details = [d for d in details if not d.startswith('$')]
        parts.append(name + ' ' + label + (' (' + ', '.join(details) + ')' if details else ''))
    opened = ', '.join(view['open'])
    verdict = ('\ubcd1\ub82c \uac00\ub2a5' if ko else 'parallel OK') + ' (' + opened + ')' if opened else ('\ubcf4\ub958' if ko else 'hold')
    if not opened:
        verdict += (' (\uc5f4\ub9b0 \ud480 \uc5c6\uc74c)' if ko else ' (no confirmed open pools)')
    cap = str(view['cap']) if view['cap'] is not None else ('\ud655\uc778 \ubd88\uac00' if ko else 'unknown')
    return ('[\ubc30\uc815 \uc790\uc6d0] ' if ko else '[Dispatch pools] ') + ' \u00b7 '.join(parts) + ' -> ' + verdict + (', \ub3d9\uc2dc \ud55c\ub3c4 ' if ko else ', cap ') + cap
