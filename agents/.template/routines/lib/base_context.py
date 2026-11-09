"""Typed context archive for DGN-2065; no inference, aggregation or live readers.

Writes are trusted adapter operations, after source ownership/evidence verification.
Reads require the same subject credentials as policy reads. Only exact event reads
are exposed: this archive does not choose a current cognitive band, compare a user
report with an inference, or supply planning decisions from historical reports.
"""
from copy import deepcopy
import json
import math
import time

CONTEXT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS context_observations (subject TEXT NOT NULL REFERENCES subjects(id), resource TEXT NOT NULL, source TEXT NOT NULL, event TEXT NOT NULL, revision INTEGER NOT NULL, epoch INTEGER NOT NULL, payload TEXT NOT NULL, received_at REAL NOT NULL, PRIMARY KEY(subject,resource,source,event,revision,epoch))",
)
BANDS = ('low', 'moderate', 'high', 'overloaded')
SIGNALS = ('user_words', 'task_deferral_streak', 'sleep', 'late_activity')


def _fail():
    from base_tunables import TunableError
    raise TunableError('invalid_observation')


def _text(value):
    if not isinstance(value, str) or not value.strip():
        _fail()


def _number(value):
    if type(value) not in (int, float):
        _fail()
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        _fail()


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail()


def _times(value):
    for name in ('measured_at', 'fresh_until', 'expires_at'):
        _number(value[name])
    if not value['measured_at'] <= value['fresh_until'] <= value['expires_at'] or value['measured_at'] == value['expires_at']:
        _fail()


def _measure(value, *, band=False):
    _fields(value, ('kind',), ('value', 'reason', 'measured_at', 'fresh_until', 'expires_at'))
    if value['kind'] == 'known':
        if 'reason' in value or 'value' not in value:
            _fail()
        if band:
            if value['value'] not in BANDS:
                _fail()
        elif type(value['value']) is not int or value['value'] < 0:
            _fail()
    elif value['kind'] in ('unknown', 'unavailable'):
        if 'value' in value or 'reason' not in value:
            _fail()
        _text(value['reason'])
    else:
        _fail()
    if band:
        if set(value) & {'measured_at', 'fresh_until', 'expires_at'}:
            _fail()
    else:
        if not {'measured_at', 'fresh_until', 'expires_at'} <= set(value):
            _fail()
        _times(value)


def validate_observation(observation):
    """Validate the complete discriminated record; never infer absent measures."""
    _fields(observation, ('resource', 'source', 'event', 'revision', 'identity_epoch',
                         'measured_at', 'fresh_until', 'expires_at', 'reader_version',
                         'schema_version', 'payload'))
    for name in ('resource', 'source', 'event', 'reader_version'):
        _text(observation[name])
    for name in ('revision', 'identity_epoch'):
        if type(observation[name]) is not int or observation[name] < 1:
            _fail()
    if type(observation['schema_version']) is not int or observation['schema_version'] != 1:
        _fail()
    _times(observation)
    payload = observation['payload']
    if not isinstance(payload, dict):
        _fail()
    kind = payload.get('kind')
    if kind == 'cognitive_band':
        if observation['resource'] != 'cognitive_load':
            _fail()
        _fields(payload, ('kind', 'measure', 'unit', 'report_ref', 'signal', 'evidence_refs', 'scope'),
                ('interaction_id', 'estimator_version'))
        if payload['unit'] != 'load_band' or payload['signal'] not in SIGNALS or payload['scope'] not in ('interaction', 'explicit_validity'):
            _fail()
        _measure(payload['measure'], band=True)
        _text(payload['report_ref'])
        if not isinstance(payload['evidence_refs'], list) or not payload['evidence_refs']:
            _fail()
        for ref in payload['evidence_refs']:
            _text(ref)
        if payload['scope'] == 'interaction':
            _text(payload.get('interaction_id'))
        elif 'interaction_id' in payload:
            _fail()
        if payload['signal'] != 'user_words':
            _text(payload.get('estimator_version'))
        elif 'estimator_version' in payload:
            _fail()
        return
    if kind not in ('token_usage_fact', 'token_balance_snapshot') or observation['resource'] != 'agent_tokens':
        _fail()
    common = ('kind', 'unit', 'provider', 'account', 'model', 'tokenizer', 'method', 'measures')
    specific = ('request_id', 'session_id', 'attempt_id', 'coverage', 'cached_input_semantics') if kind == 'token_usage_fact' else ('host_book', 'allowance_metric', 'window_id', 'window_start', 'window_end', 'reset_at')
    _fields(payload, common + specific)
    if payload['unit'] != 'model_token' or payload['method'] not in ('provider-reported', 'runtime-counted'):
        _fail()
    for name in ('provider', 'account', 'model', 'tokenizer'):
        _text(payload[name])
    names = ('input', 'output', 'cached_input') if kind == 'token_usage_fact' else ('used', 'limit', 'remaining')
    _fields(payload['measures'], names)
    for measure in payload['measures'].values():
        _measure(measure)
        if measure['measured_at'] > observation['measured_at'] or measure['expires_at'] > observation['expires_at']:
            _fail()
    if kind == 'token_usage_fact':
        for name in ('request_id', 'session_id', 'attempt_id'):
            _text(payload[name])
        if payload['coverage'] not in ('partial', 'complete') or payload['cached_input_semantics'] not in ('subset', 'disjoint'):
            _fail()
        total, cached = (payload['measures'][name] for name in ('input', 'cached_input'))
        if payload['cached_input_semantics'] == 'subset' and total['kind'] == cached['kind'] == 'known' and cached['value'] > total['value']:
            _fail()
    else:
        for name in ('host_book', 'allowance_metric', 'window_id'):
            _text(payload[name])
        if payload['allowance_metric'] != 'model_token' or payload['method'] != 'provider-reported':
            _fail()
        for name in ('window_start', 'window_end', 'reset_at'):
            _number(payload[name])
        if not payload['window_start'] <= observation['measured_at'] < payload['window_end'] <= payload['reset_at']:
            _fail()


def _freshness(record, now):
    return 'fresh' if now < record['fresh_until'] else 'bounded_stale' if now < record['expires_at'] else 'expired'


class ContextStorage:
    def store_observation(self, subject_id, observation):
        """Trusted adapter only, NOT a client RPC. Does not infer from evidence.

        The adapter verifies source/account ownership and the referenced signal;
        source strings themselves are never credentials. Receipt time is local.
        Subject epoch invalidates context alongside pending policy acts.
        """
        from base_tunables import TunableError
        validate_observation(observation)
        encoded = json.dumps(observation, sort_keys=True, allow_nan=False)
        with self._transaction() as db:
            subject = db.execute('SELECT epoch FROM subjects WHERE id=?', (subject_id,)).fetchone()
            if subject is None:
                raise TunableError('unauthorized')
            if subject[0] != observation['identity_epoch']:
                raise TunableError('stale_epoch')
            identity = (subject_id, observation['resource'], observation['source'], observation['event'], observation['revision'], subject[0])
            old = db.execute('SELECT payload FROM context_observations WHERE subject=? AND resource=? AND source=? AND event=? AND revision=? AND epoch=?', identity).fetchone()
            if old:
                if old[0] != encoded:
                    raise TunableError('observation_collision')
                return  # Duplicates cannot refresh received_at or freshness.
            db.execute('INSERT INTO context_observations VALUES (?,?,?,?,?,?,?,?)', identity + (encoded, time.time()))

    def read_observation(self, credential, resource, source, event, revision, *, identity_epoch, now=None, allow_bounded_stale=False, interaction_id=None):
        """Exact authorized archive read, with separately gated usable payload.

        No latest/current resolver is exposed. User-word precedence, conflict
        arbitration, revocation of individual evidence, corrections, aggregation
        and provider identity changes require a future reader adapter. Never use
        these exact historical reads as a current cognitive-load decision.
        """
        from base_tunables import TunableError
        with self._transaction() as db:
            actor = self._actor(db, credential)
            epoch = db.execute('SELECT epoch FROM subjects WHERE id=?', (actor['subject'],)).fetchone()[0]
            row = db.execute('SELECT payload,received_at FROM context_observations WHERE subject=? AND resource=? AND source=? AND event=? AND revision=? AND epoch=?',
                             (actor['subject'], resource, source, event, revision, identity_epoch)).fetchone()
            if row is None:
                raise TunableError('observation_not_found')
            record = json.loads(row['payload'])
            now = time.time() if now is None else now
            _number(now)
            freshness = _freshness(record, now)
            reason = 'stale_epoch' if epoch != identity_epoch else 'not_yet_measured' if now < record['measured_at'] else freshness if freshness == 'expired' or (freshness == 'bounded_stale' and not allow_bounded_stale) else None
            payload = deepcopy(record['payload'])
            if not reason and payload['kind'] == 'cognitive_band' and payload['scope'] == 'interaction' and payload['interaction_id'] != interaction_id:
                reason = 'interaction_mismatch'
            usable = {'kind': 'unknown', 'reason': reason} if reason else payload
            measure_freshness = {}
            if 'measures' in payload:
                for name, measure in payload['measures'].items():
                    state = _freshness(measure, now)
                    measure_freshness[name] = state
                    if not reason and (now < measure['measured_at'] or state == 'expired' or (state == 'bounded_stale' and not allow_bounded_stale)):
                        payload['measures'][name] = {'kind': 'unknown', 'reason': 'not_yet_measured' if now < measure['measured_at'] else state}
            return dict(store_id=self.store_id, subject_id=actor['subject'], observation=record,
                        received_at=row['received_at'], freshness=freshness,
                        measure_freshness=measure_freshness, usable_payload=usable)
