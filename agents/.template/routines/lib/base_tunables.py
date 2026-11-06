"""Local planning policy authority (DGN-2065 slices 1-2), stdlib only.

Trusted service code owns this object and its SQLite file. Clients receive opaque
credentials, never database access. Provision/enroll/register/advance_epoch are
administrator entry points, NOT client RPCs. Enrollment consumes ownership already
verified by the host's authentication adapter; OS/provider/crew identity is never
inferred here. No transport or live identity adapter is installed by this slice.
One subject uses one designated store; opening a missing store never creates a
fallback. The only client verbs are resolve_tunable, authorize_selection (human
session only), and select_tunable. Agents can apply an exact human-authorized act
but cannot manufacture one. No scheduling or legacy import is installed.
Context observations are storage/read only; no inference or provider readers.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid

from base_context import ContextStorage, CONTEXT_SCHEMA

KEY = "planning.focus.length"
# Closed section-6 catalog. Source references are design evidence, not runtime
# dependencies or claims of consumer cutover. No trailing-buffer key is active.
CATALOG = {
    "planning.focus.break": (20, "Break guidance; no automatic insertion", "F03; work-career-focus-block-rules-082:119-124"),
    "planning.reminder.task_lead": (30, "Task reminder lead", "T08; remind_select.py:90-98,125; LIFEKIT_LEAD_TASK_MIN"),
    "planning.reminder.appointment_lead": (120, "Appointment reminder lead", "T08; remind_select.py:90-98,125; LIFEKIT_LEAD_APPOINTMENT_MIN"),
    "planning.task_duration.omitted_oneoff": (60, "Occupancy fallback only when one-off duration is omitted", "T10; kit_life.py:9551-9553; absent LIFEKIT_DEFAULT_DURATION_MIN"),
    "planning.task_duration.omitted_recurring": (30, "Occupancy fallback only when recurring duration is omitted", "T11; routine_projection.py:55-57,293,442,467; routine_roller.py:834; absent LIFEKIT_DEFAULT_DURATION_MIN"),
    "planning.buffer.appointment_arrival": (15, "Front arrival buffer; zero disables; never trailing", "T13; kit_life.py:9554-9563,14038"),
    "planning.task_size.max_estimate": (60, "Split proposal threshold; missing estimate remains unknown; not an occupancy fallback", "planning-vocab-wiring-DESIGN.md:8,68,130,174; confirmed starting proposal"),
}

_EXTRA_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS extra_definitions (key TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, recorded_at REAL NOT NULL, PRIMARY KEY(key,revision))",
    "CREATE TABLE IF NOT EXISTS extra_selections (key TEXT NOT NULL, subject TEXT NOT NULL REFERENCES subjects(id), revision INTEGER NOT NULL, definition INTEGER NOT NULL, epoch INTEGER NOT NULL, value REAL, act_ref TEXT UNIQUE NOT NULL, recorded_at REAL NOT NULL, PRIMARY KEY(key,subject,revision), FOREIGN KEY(key,definition) REFERENCES extra_definitions(key,revision))",
)

SEMANTICS = "Focus guidance duration; product starting setting, not measured optimum"


class TunableError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EffectiveVersion:
    store_id: str
    subject_id: str
    key: str
    authority_epoch: int
    definition_revision: int
    selection_revision: int


@dataclass(frozen=True)
class Provenance:
    source: str
    owner: str
    release: str
    validation_revision: int
    act_ref: str | None
    source_ref: str | None = None
    semantics: str | None = None


@dataclass(frozen=True)
class Known:
    value: float
    type: str
    unit: str
    provenance: Provenance
    identity: EffectiveVersion
    kind: str = "known"


@dataclass(frozen=True)
class Unknown:
    reason: str
    type: str
    unit: str
    provenance: Provenance
    identity: EffectiveVersion
    kind: str = "unknown"

    def __bool__(self):
        raise TypeError("Unknown tunable has no truth value")


def _digest(token):
    if not isinstance(token, str):
        raise TunableError("unauthorized")
    return hashlib.sha256(token.encode()).hexdigest()


def _duration(value, key=KEY):
    if type(value) not in (int, float):
        raise TunableError("invalid_duration")
    try:
        canonical = float(value)
    except (OverflowError, ValueError):
        raise TunableError("invalid_duration") from None
    if not math.isfinite(canonical) or canonical < 0 or (canonical == 0 and key != "planning.buffer.appointment_arrival"):
        raise TunableError("invalid_duration")
    return canonical


_SCHEMA = """
CREATE TABLE metadata (store_id TEXT NOT NULL);
CREATE TABLE subjects (id TEXT PRIMARY KEY, epoch INTEGER NOT NULL CHECK(epoch>0));
CREATE TABLE actors (credential TEXT PRIMARY KEY, actor_id TEXT UNIQUE NOT NULL,
 subject TEXT NOT NULL REFERENCES subjects(id), role TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE definitions (revision INTEGER PRIMARY KEY, payload TEXT NOT NULL,
 recorded_at REAL NOT NULL);
CREATE TABLE selections (subject TEXT NOT NULL REFERENCES subjects(id),
 revision INTEGER NOT NULL, definition INTEGER NOT NULL REFERENCES definitions(revision),
 epoch INTEGER NOT NULL, value REAL, act_ref TEXT UNIQUE NOT NULL,
 recorded_at REAL NOT NULL, PRIMARY KEY(subject,revision));
CREATE TABLE acts (credential TEXT PRIMARY KEY, ref TEXT UNIQUE NOT NULL,
 subject TEXT NOT NULL REFERENCES subjects(id), human TEXT NOT NULL REFERENCES actors(credential),
 candidate REAL, expected TEXT NOT NULL, applied INTEGER,
 recorded_at REAL NOT NULL);
CREATE TABLE authority_events (subject TEXT NOT NULL REFERENCES subjects(id),
 epoch INTEGER NOT NULL, watermark INTEGER NOT NULL, recorded_at REAL NOT NULL,
 PRIMARY KEY(subject,epoch));
"""


@contextmanager
def _connection(path):
    """Every connection, including bootstrap, must opt into durable commits."""
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        settings = [('journal_mode', 'DELETE', 'delete'),
                    ('synchronous', 'EXTRA', 3), ('foreign_keys', 'ON', 1)]
        if sys.platform == 'darwin':
            settings.append(('fullfsync', 'ON', 1))
        for name, value, expected in settings:
            db.execute(f"PRAGMA {name}={value}")
            actual = db.execute(f"PRAGMA {name}").fetchone()
            if actual is None or actual[0] != expected:
                raise TunableError("authority_unavailable")
        yield db
    except sqlite3.Error as exc:
        raise TunableError("authority_unavailable") from exc
    finally:
        if db is not None:
            db.close()


def _rename_exclusive(source, destination):
    """Atomic publication without replacing even a concurrently created store."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        # AT_FDCWD, RENAME_NOREPLACE; unsupported kernels/filesystems fail closed.
    else:
        raise TunableError("authority_unavailable")
    rename.restype = ctypes.c_int
    if rename(*args):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def _full_sync(fd):
    os.fsync(fd)
    if sys.platform == 'darwin':
        # Python builds do not all expose the Darwin fcntl constant.
        fcntl.fcntl(fd, getattr(fcntl, 'F_FULLFSYNC', 51))


class Authority(ContextStorage):
    def __init__(self, path):
        self.path = Path(path).resolve()
        # mode=rw prevents silent creation of an instance-local fallback.
        with self._transaction() as db:
            self.store_id = self._validate_store(db)

    @staticmethod
    def _validate_store(db):
        # Probe every required column, including tables not needed by a read.
        columns = dict(metadata='store_id', subjects='id,epoch',
                       actors='credential,actor_id,subject,role,active',
                       definitions='revision,payload,recorded_at',
                       selections='subject,revision,definition,epoch,value,act_ref,recorded_at',
                       acts='credential,ref,subject,human,candidate,expected,applied,recorded_at',
                       authority_events='subject,epoch,watermark,recorded_at')
        for table, names in columns.items():
            db.execute(f"SELECT {names} FROM {table} LIMIT 0")
        metadata = db.execute("SELECT store_id FROM metadata").fetchall()
        initial = db.execute("SELECT payload FROM definitions WHERE revision=1").fetchone()
        if len(metadata) != 1 or initial is None:
            raise TunableError("authority_unavailable")
        try:
            uuid.UUID(metadata[0][0])
            payload = json.loads(initial[0])
            if (not isinstance(payload, dict) or payload.get('default') != 90
                    or payload.get('unknown_reason') is not None
                    or payload.get('type') != 'duration' or payload.get('unit') != 'minute'
                    or payload.get('owner') != 'base' or payload.get('semantics') != SEMANTICS
                    or not payload.get('release')):
                raise ValueError('incomplete initial definition')
        except (ValueError, TypeError, AttributeError) as exc:
            raise TunableError("authority_unavailable") from exc
        return metadata[0][0]

    @classmethod
    def create(cls, path, *, release):
        # Resolve the parent only: an existing destination symlink must also
        # refuse creation, never redirect it to an otherwise missing target.
        path = Path(path)
        path = path.parent.resolve() / path.name
        temporary = None
        fd = directory = None
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            fd, name = tempfile.mkstemp(prefix=f'.{path.name}.bootstrap-', dir=path.parent)
            temporary = Path(name)  # mkstemp is exclusive and mode 0600.
            with _connection(temporary) as db:
                db.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA)
                db.execute("INSERT INTO metadata VALUES (?)", (str(uuid.uuid4()),))
                db.commit()
            # A private bootstrap object can register the default before the
            # public constructor admits this store. No clients see this path.
            bootstrap = cls.__new__(cls)
            bootstrap.path = temporary
            bootstrap.register_definition(1, default=90, release=release)
            bootstrap.install_slice2(release=release)
            authority = cls(temporary)  # Validate before publication.
            _full_sync(fd)
            os.close(fd)
            fd = None  # Do not close a raw DB fd after other clients can open it.
            _rename_exclusive(temporary, path)
            _full_sync(directory)
            authority.path = path
            return authority
        except FileExistsError:
            raise  # Never replace an existing authority (valid or incomplete).
        except OSError as exc:
            raise TunableError("authority_unavailable") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)
            if temporary is not None:
                for suffix in ('', '-journal', '-wal', '-shm'):
                    Path(str(temporary) + suffix).unlink(missing_ok=True)

    @contextmanager
    def _transaction(self):
        with _connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def install_slice2(self, *, release):
        """Trusted explicit additive upgrade, atomic and retryable for slice-1 stores.

        Does not import legacy settings or change focus history/authority epoch.
        No automatic upgrade on open; old stores retain focus service until this
        administrative operation is requested.
        """
        if not isinstance(release, str) or not release.strip():
            raise TunableError("invalid_definition")
        with self._transaction() as db:
            for statement in _EXTRA_SCHEMA + CONTEXT_SCHEMA:
                db.execute(statement)
            for key, (default, semantics, source) in CATALOG.items():
                if db.execute("SELECT 1 FROM extra_definitions WHERE key=?", (key,)).fetchone():
                    continue
                payload = self._definition_payload(key, default, None, release)
                db.execute("INSERT INTO extra_definitions VALUES (?,1,?,?)", (key, payload, time.time()))

    @staticmethod
    def _definition_payload(key, default, unknown_reason, release):
        if key != KEY and key not in CATALOG:
            raise TunableError("unregistered_key")
        if default is None:
            if not isinstance(unknown_reason, str) or not unknown_reason.strip():
                raise TunableError("invalid_default")
        else:
            default = _duration(default, key)
            if unknown_reason is not None:
                raise TunableError("invalid_default")
        payload = dict(default=default, unknown_reason=unknown_reason,
                       release=release, type="duration", unit="minute", owner="base",
                       semantics=SEMANTICS if key == KEY else CATALOG[key][1])
        if key != KEY:
            payload['source_ref'] = CATALOG[key][2]
            payload['validation'] = 'nonnegative_finite' if key == "planning.buffer.appointment_arrival" else 'positive_finite'
        return json.dumps(payload, sort_keys=True)

    def register_definition(self, revision, *, default=None, unknown_reason=None, release, key=KEY):
        """Trusted base registrar; immutable meaning and canonical minutes."""
        if type(revision) is not int or revision < 1 or not isinstance(release, str) or not release.strip():
            raise TunableError("invalid_definition")
        payload = self._definition_payload(key, default, unknown_reason, release)
        table, clause, args = ('definitions', '', ()) if key == KEY else ('extra_definitions', ' WHERE key=?', (key,))
        with self._transaction() as db:
            rows = db.execute(f"SELECT revision,payload FROM {table}{clause} ORDER BY revision DESC", args).fetchall()
            for row in rows:
                if row['revision'] == revision:
                    if row['payload'] != payload:
                        raise TunableError("definition_collision")
                    return
            if revision != (rows[0]['revision'] if rows else 0) + 1:
                raise TunableError("definition_revision_conflict")
            if key == KEY:
                db.execute("INSERT INTO definitions VALUES (?,?,?)", (revision, payload, time.time()))
            else:
                db.execute("INSERT INTO extra_definitions VALUES (?,?,?,?)", (key, revision, payload, time.time()))

    def provision_subject(self, subject_id, human_actor_id):
        """Trusted identity adapter: enroll a verified human, returning a secret."""
        token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            db.execute("INSERT INTO subjects VALUES (?,1)", (subject_id,))
            db.execute("INSERT INTO actors VALUES (?,?,?,'human',1)",
                       (_digest(token), human_actor_id, subject_id))
            db.execute("INSERT INTO authority_events VALUES (?,1,0,?)", (subject_id, time.time()))
        return token

    def enroll_verified_agent(self, subject_id, actor_id):
        """Trusted adapter attests ownership; grants that human's focus reads.

        No per-agent preference copy or extra sharing consent. No reparenting an
        existing actor to another human; uniqueness rejects ownership collisions.
        """
        token = secrets.token_urlsafe(32)
        with self._transaction() as db:
            db.execute("INSERT INTO actors VALUES (?,?,?,'agent',1)",
                       (_digest(token), actor_id, subject_id))
        return token

    def revoke(self, actor_id):
        with self._transaction() as db:
            db.execute("UPDATE actors SET active=0 WHERE actor_id=?", (actor_id,))

    def _actor(self, db, token):
        actor = db.execute("SELECT * FROM actors WHERE credential=? AND active=1",
                           (_digest(token),)).fetchone()
        if actor is None:
            raise TunableError("unauthorized")
        return actor

    def _current(self, db, subject, key=KEY):
        if key != KEY:
            if key not in CATALOG:
                raise TunableError("unregistered_key")
            definition = db.execute("SELECT * FROM extra_definitions WHERE key=? ORDER BY revision DESC LIMIT 1", (key,)).fetchone()
            if definition is None:
                raise TunableError("authority_unavailable")
            selection = db.execute("SELECT * FROM extra_selections WHERE key=? AND subject=? ORDER BY revision DESC LIMIT 1", (key, subject)).fetchone()
            epoch = db.execute("SELECT epoch FROM subjects WHERE id=?", (subject,)).fetchone()[0]
            return definition, selection, EffectiveVersion(self.store_id, subject, key, epoch,
                definition['revision'], selection['revision'] if selection else 0)
        definition = db.execute("SELECT * FROM definitions ORDER BY revision DESC LIMIT 1").fetchone()
        selection = db.execute("SELECT * FROM selections WHERE subject=? ORDER BY revision DESC LIMIT 1",
                               (subject,)).fetchone()
        epoch = db.execute("SELECT epoch FROM subjects WHERE id=?", (subject,)).fetchone()[0]
        version = EffectiveVersion(self.store_id, subject, KEY, epoch,
                                   definition['revision'], selection['revision'] if selection else 0)
        return definition, selection, version

    def resolve_tunable(self, credential, key=KEY, expected_type="duration", accepted_unit="minute"):
        with self._transaction() as db:
            actor = self._actor(db, credential)  # Authenticate before key disclosure.
            if key != KEY and key not in CATALOG:
                raise TunableError("unregistered_key")
            if expected_type != "duration":
                raise TunableError("type_mismatch")
            if accepted_unit not in ("minute", "second"):
                raise TunableError("incompatible_unit")
            definition, selection, version = self._current(db, actor['subject'], key)
            payload = json.loads(definition['payload'])
            source = "default" if selection is None else "reset" if selection['value'] is None else "selection"
            provenance = Provenance(source, "base", payload['release'],
                                    selection['definition'] if selection else definition['revision'],
                                    selection['act_ref'] if selection else None,
                                    payload.get('source_ref'), payload['semantics'])
            value = selection['value'] if source == "selection" else payload['default']
            if value is None:
                return Unknown(payload['unknown_reason'], "duration", accepted_unit, provenance, version)
            presented = value * (60 if accepted_unit == "second" else 1)
            if not math.isfinite(presented):
                raise TunableError("presentation_overflow")
            return Known(presented, "duration", accepted_unit, provenance, version)

    @staticmethod
    def _version_json(version):
        if not isinstance(version, EffectiveVersion):
            raise TunableError("stale_selection")
        return json.dumps(version.__dict__, sort_keys=True)

    def authorize_selection(self, human_credential, candidate, expected_version):
        """Authenticated human session confirms exactly candidate (None=reset).

        The trusted UI/auth adapter calls this only on a human act. This is not
        an agent's claim of consent. Stale displayed defaults/epochs are rejected.
        """
        with self._transaction() as db:
            human = self._actor(db, human_credential)
            if human['role'] != 'human':
                raise TunableError("unauthorized")
            self._version_json(expected_version)
            _, _, current = self._current(db, human['subject'], expected_version.key)
            if expected_version != current:
                raise TunableError("stale_selection")
            if candidate is not None:
                candidate = _duration(candidate, current.key)
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO acts VALUES (?,?,?,?,?,?,NULL,?)",
                       (_digest(token), str(uuid.uuid4()), human['subject'], human['credential'],
                        candidate, self._version_json(current), time.time()))
            return token

    def select_tunable(self, credential, human_act):
        """Apply the sealed candidate with epoch + full effective-version CAS."""
        with self._transaction() as db:
            actor = self._actor(db, credential)
            act = db.execute("SELECT a.* FROM acts a JOIN actors h ON h.credential=a.human "
                             "WHERE a.credential=? AND h.active=1", (_digest(human_act),)).fetchone()
            if act is None or act['subject'] != actor['subject']:
                raise TunableError("unauthorized")
            if act['applied'] is not None:
                return act['applied']  # Exact retry never overwrites subsequent choices.
            expected = json.loads(act['expected'])
            _, _, current = self._current(db, actor['subject'], expected['key'])
            if expected['authority_epoch'] != current.authority_epoch:
                raise TunableError("stale_epoch")
            if act['expected'] != self._version_json(current):
                raise TunableError("stale_selection")
            revision = current.selection_revision + 1
            values = (actor['subject'], revision, current.definition_revision,
                      current.authority_epoch, act['candidate'], act['ref'], time.time())
            if current.key == KEY:
                db.execute("INSERT INTO selections VALUES (?,?,?,?,?,?,?)", values)
            else:
                db.execute("INSERT INTO extra_selections VALUES (?,?,?,?,?,?,?,?)", (current.key,) + values)
            db.execute("UPDATE acts SET applied=? WHERE credential=?", (revision, act['credential']))
            return revision

    def advance_epoch(self, subject_id, expected_epoch):
        """Trusted fresh-family fence; retains every acknowledged selection.

        Not a legacy import/cutover API. No legacy persistence writer is enrolled
        in this slice. A future importer must implement freeze/drain separately.
        """
        with self._transaction() as db:
            _, _, current = self._current(db, subject_id)
            if current.authority_epoch != expected_epoch:
                raise TunableError("stale_epoch")
            epoch = expected_epoch + 1
            db.execute("UPDATE subjects SET epoch=? WHERE id=?", (epoch, subject_id))
            db.execute("INSERT INTO authority_events VALUES (?,?,?,?)",
                       (subject_id, epoch, current.selection_revision, time.time()))
            return epoch
