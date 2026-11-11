"""why_net.store -- the append-only JSONL store (spec section 1).

Root: $DOGANY_BOT_DATA_DIR or <root>/.telegram_bot, then state/why-net/
(same derivation as status-footer _bot_data_dir).  <root> is the caller's
`root` argument, else $DOGANY_ROOT, else the instance this file lives in
(P3's surfaces are called by hooks that already know their root).

Every append runs under fcntl.flock on state/why-net/.lock.  The row is
built INSIDE the lock by a caller-supplied builder, so id allocation and
floor checks (cycle, dedupe) see the same file state the append lands on.

Integrity: every row of a chained file carries `prev` = sha256 of the
previous raw line (without its newline).  The first row carries GENESIS.
audit() re-validates the chain; it detects, it never refuses.

Final grill (non-blocking, fixed on build): the store holds the owner's
words verbatim, so directories are created 0700 and files 0600; and a
caller may bound its wait for the lock (LOCK_WAIT, set by the
UserPromptSubmit hook) -- past it, StoreBusy is raised and the hook skips
the turn and logs instead of stalling every owner turn on a stuck holder.
"""

import datetime
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
import tempfile
import time
import unicodedata

# lib/why_net/store.py -> lib -> routines -> <root>
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__)))))

EVENTS = "events.jsonl"
PROBES = "probes.jsonl"
ASKS = "asks.jsonl"
CORRECTIONS = "corrections.jsonl"
OWNER_TURNS = "owner-turns.jsonl"
# Owner-turn ledger (grill fix 1): one row per REAL owner prompt, written only
# by the UserPromptSubmit hook (`why-net ups`) from the hook stdin + a fresh
# bridge inbound-context.  The resolver accepts a quote only from a turn whose
# sha256 is in here.  Append-only and chained, unlike the owner-turns ring.
TURN_LEDGER = "turn-ledger.jsonl"
# AMEND-1 1.2: owner decisions and actions linked to a Why node.  Same hash
# chain, lock and write-deny as the rest of the store.
ACTS = "acts.jsonl"
# AMEND-2 2.5: the frame path's own record -- `proceed` rows (an unconfirmed
# assumption, never a Why node) and `answer` rows (free-text answer paths
# a/b/c).  Same chain and lock as the rest of the store.
FRAMES = "frames.jsonl"
# AMEND-2 2.11: owner-gated metacog reset ({target: {"at", "otl"}}); not a
# chained file (frames.jsonl stays proceed + answers).
METACOG_RESET = "metacog-reset.json"
# Instance-scoped first-appearance flags; retained across sessions/languages.
TERM_GLOSSES = "term-glosses.jsonl"
CARDS = "problem-cards.jsonl"

# Files whose rows carry the `prev` hash chain (spec 1 + 2.7).
CHAINED = (EVENTS, PROBES, ASKS, CORRECTIONS, TURN_LEDGER, ACTS, FRAMES, CARDS)

GENESIS = "0" * 64

DIR_MODE, FILE_MODE = 0o700, 0o600
# Seconds a locked() caller waits before StoreBusy; None = block (CLI verbs).
LOCK_WAIT = None
LOCK_POLL = 0.05


class StoreBusy(RuntimeError):
    """The store lock was not free within LOCK_WAIT."""


def _makedirs(d):
    os.makedirs(d, mode=DIR_MODE, exist_ok=True)


def _open_private(p, flags, text_mode):
    fd = os.open(p, flags | os.O_CREAT, FILE_MODE)
    return os.fdopen(fd, text_mode, encoding="utf-8")


_CANONICAL_ROOT = ContextVar("why_store_instance", default=None)


@contextmanager
def canonical(root):
    """Bind supported stage doors to their explicit instance without changing
    legacy callers that deliberately supply a separate bot-data directory.
    """
    token = _CANONICAL_ROOT.set(os.path.realpath(root))
    try:
        yield
    finally:
        _CANONICAL_ROOT.reset(token)


def root_dir():
    return os.environ.get("DOGANY_ROOT") or _ROOT_DIR


def bot_data_dir(root=None):
    canonical = _CANONICAL_ROOT.get()
    if canonical is not None:
        return os.path.join(canonical, ".telegram_bot")
    return os.environ.get("DOGANY_BOT_DATA_DIR") or os.path.join(
        root or root_dir(), ".telegram_bot")


def store_dir(root=None):
    return os.path.join(bot_data_dir(root), "state", "why-net")


def path(name, root=None):
    return os.path.join(store_dir(root), name)


# DGN-1967 bug 1: the invocation's clock.  budget.Ctx binds it when it is
# built with an injected `now`, so a row stamped here and the ctx.now that
# later reads it (acts.flags, the act-ripe window) agree.  Unbound -- every
# production invocation -- it is the wall clock.
_CLOCK = None


def bind_clock(clock):
    """clock: a no-argument callable returning epoch seconds, or None."""
    global _CLOCK
    _CLOCK = clock


def now_iso():
    if _CLOCK is not None:
        return iso_at(_CLOCK())
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def iso_at(ts):
    """Epoch seconds -> the store's ISO form (the elicitation loop runs on an
    injectable clock, so it stamps rows with this, not now_iso())."""
    if ts is None:
        return now_iso()
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s):
    """ISO8601 (Z or offset; no offset = host local) -> an aware datetime;
    None on anything else.  The one timestamp parser of the Why net and its
    transcript readers (DGN-1967 step 4)."""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def parse_iso(s):
    """ISO8601 (Z or offset) -> epoch float; None on anything else."""
    dt = parse_dt(s)
    return None if dt is None else dt.timestamp()


def nfc(s):
    """NFC form of s (None -> "")."""
    return unicodedata.normalize("NFC", s or "")


# A line that asks: it ends in a question mark (ASCII or full-width) or, in
# Korean prose, on an interrogative ending (an owner asks without "?").
QUESTION_MARKS = ("?", "\uff1f")
QUESTION_ENDINGS = ("\uae4c\uc694", "\ub098\uc694", "\uac00\uc694", "\ub294\uac00",
                    "\uc744\uae4c", "\ud560\uae4c", "\uc77c\uae4c", "\uc778\uac00",
                    "\ub0d0")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def bubble_text_sha(text):
    """Grill-3 fix 11: sha256 of a bubble's text, NFC with every whitespace
    run removed.  Lockstep with bridge/sdk_bridge.bubble_text_sha (the
    bridge publishes it for the tapped bubble; a bridge test pins parity)."""
    if not isinstance(text, str):
        return None
    norm = "".join(unicodedata.normalize("NFC", text).split())
    return sha256_text(norm)


def dumps(obj):
    # ensure_ascii=False keeps owner verbatim readable; the decoded value is
    # byte-exact either way.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _raw_lines(name, root=None):
    try:
        with open(path(name, root), "r", encoding="utf-8") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    return [l for l in data.split("\n") if l != ""]


def jsonl_iter(p, errors="strict"):
    """Every JSON object line of the file at `p`, in order (malformed lines
    and non-objects skipped).  A missing file raises OSError; a bad byte
    raises UnicodeDecodeError unless `errors` says otherwise."""
    with open(p, encoding="utf-8", errors=errors) as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


# AMEND-2 2.6 (G4): one frame call parses each store file at most once.
# Inside `cached_reads()` read_rows memoises by (path, size, mtime), so a
# write inside the scope (it changes size / mtime) is always re-read.
# Callers inside the scope must not mutate the rows they get.
_READ_CACHE = []


class cached_reads(object):
    def __enter__(self):
        _READ_CACHE.append({})
        return self

    def __exit__(self, *exc):
        _READ_CACHE.pop()
        return False


def read_rows(name, root=None):
    """All parseable rows of a store file, in order.  A malformed line is
    skipped here (audit() reports it)."""
    if name == CARDS:
        return _strict_cards(root)
    if _READ_CACHE:
        p = path(name, root)
        try:
            st = os.stat(p)
            key = (p, st.st_size, st.st_mtime_ns)
        except OSError:
            key = (p, None, None)
        memo = _READ_CACHE[-1]
        if key not in memo:
            memo[key] = _parse_rows(_raw_lines(name, root))
        return memo[key]
    return _parse_rows(_raw_lines(name, root))


def read_tail_rows(name, max_bytes, root=None):
    """The parseable rows in the last `max_bytes` of a store file (the first,
    possibly partial, line of the window is dropped).  For bounded reads
    that only need the newest rows (AMEND-2 G4: the frame's ledger
    lookup)."""
    try:
        with open(path(name, root), "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except FileNotFoundError:
        return []
    lines = raw.decode("utf-8", "replace").split("\n")
    if size > max_bytes:
        lines = lines[1:]
    return _parse_rows([l for l in lines if l != ""])


def _parse_rows(lines):
    # AMEND-2 G4: one C-level parse of the whole file (about half the cost
    # of a call per line); any malformed line falls back to the per-line
    # loop, which skips exactly that line (same rows either way).
    if lines:
        try:
            parsed = json.loads("[" + ",".join(lines) + "]")
        except ValueError:
            parsed = None
        if parsed is not None and len(parsed) == len(lines):
            return [obj for obj in parsed if isinstance(obj, dict)]
    rows = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def is_empty(name):
    return not _raw_lines(name)


class _Lock:
    def __init__(self, root=None, wait=None):
        self._root = root
        self._wait = LOCK_WAIT if wait is None else wait

    def __enter__(self):
        _makedirs(store_dir(self._root))
        self._fh = _open_private(path(".lock", self._root), os.O_RDWR | os.O_APPEND, "a+")
        if self._wait is None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            return self
        deadline = time.monotonic() + self._wait
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    raise StoreBusy(path(".lock", self._root))
                time.sleep(LOCK_POLL)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
        return False


def locked(root=None, wait=None):
    """Exclusive store lock (spec 1).  Re-entrance is not supported."""
    return _Lock(root, wait)


def _strict_cards(root=None, recover=False):
    """Card rows never skip corruption. Only a torn, uncommitted tail heals.

    Recovery is allowed only by the writer under the store lock. Readers
    return unavailable until that recovery has taken place.
    """
    p = path(CARDS, root)
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return []
    if raw and not raw.endswith(b"\n"):
        if not recover:
            raise ValueError("torn card tail")
        raw = raw[:raw.rfind(b"\n") + 1]
        with open(p, "r+b") as fh:
            fh.truncate(len(raw))
            fh.flush()
            os.fsync(fh.fileno())
    rows, prev = [], GENESIS
    for line in raw.decode("utf-8").splitlines():
        obj = json.loads(line)
        if not isinstance(obj, dict) or obj.get("prev") != prev:
            raise ValueError("invalid card chain")
        rows.append(obj)
        prev = sha256_text(line)
    return rows


def append(name, build, root=None):
    """Append one row to `name` under the store lock.

    `build(rows)` receives the current parsed rows of `name` and returns the
    row dict to append (it may raise WhyRefusal; then nothing is written).
    For chained files `prev` is set here.  Returns the appended row."""
    with locked(root):
        return append_unlocked(name, build, root)


def append_unlocked(name, build, root=None):
    """append() for a caller that already holds locked()."""
    return _write_rows(name, lambda rows: [build(rows)], root)[0]


def append_many(name, build, root=None):
    """append() of 0..n rows: `build(rows)` returns a list, every row is
    chained to the one before it.  Returns the appended rows."""
    with locked(root):
        return _write_rows(name, build, root)


def _write_rows(name, build, root):
    if name == CARDS:
        _strict_cards(root, recover=True)
    lines = _raw_lines(name, root)
    new = build(read_rows(name, root)) or []
    out = []
    for obj in new:
        if name in CHAINED:
            obj["prev"] = sha256_text(lines[-1]) if lines else GENESIS
        line = dumps(obj)
        if "\n" in line:
            raise ValueError("row serialised to more than one line")
        out.append(line)
        lines.append(line)
    if out:
        with _open_private(path(name, root), os.O_WRONLY | os.O_APPEND, "a") as fh:
            fh.write("".join(l + "\n" for l in out))
            fh.flush()
            os.fsync(fh.fileno())
    return new


def rewrite_chain(name, rows, root=None):
    """AMEND-1 F14 owner-request erasure: rewrite a chained file from `rows`
    (prev recomputed, GENESIS first).  Caller holds locked(); the only
    non-append write of a chained file, and its caller writes the audit
    line (an act-void event) first."""
    lines, prev = [], None
    for obj in rows:
        obj = dict(obj)
        obj["prev"] = sha256_text(prev) if prev is not None else GENESIS
        prev = dumps(obj)
        lines.append(prev)
    atomic_write(path(name, root), "".join(l + "\n" for l in lines))


def _last_line(name, root=None, chunk=65536):
    """The last raw line of a store file ('' when empty / absent), read from
    the end -- the chain's `prev` without parsing the file."""
    try:
        with open(path(name, root), "rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            buf = b""
            pos = end
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                body = buf.rstrip(b"\n")
                if b"\n" in body or pos == 0:
                    lines = [l for l in body.split(b"\n") if l != b""]
                    return lines[-1].decode("utf-8") if lines else ""
    except FileNotFoundError:
        return ""
    return ""


def append_rows_ready_unlocked(name, objs, root=None):
    """Append ready-made rows (no builder, so the file is not parsed): for a
    chained file `prev` comes from the last line alone.  Same bytes as
    append_row_unlocked row by row.  Caller holds locked()."""
    last = _last_line(name, root) if name in CHAINED else ""
    out = []
    for obj in objs:
        if name in CHAINED:
            obj["prev"] = sha256_text(last) if last else GENESIS
        line = dumps(obj)
        if "\n" in line:
            raise ValueError("row serialised to more than one line")
        out.append(line)
        last = line
    if out:
        _makedirs(store_dir(root))
        with _open_private(path(name, root), os.O_WRONLY | os.O_APPEND, "a") as fh:
            fh.write("".join(l + "\n" for l in out))
            fh.flush()
            os.fsync(fh.fileno())
    return objs


def append_row(name, obj, root=None):
    """append() of a ready-made row (no floor check in the builder)."""
    return append(name, lambda rows: obj, root)


def append_row_unlocked(name, obj, root=None):
    return append_unlocked(name, lambda rows: obj, root)


def atomic_write(dest, text, private=True):
    """Write `text` to `dest` through a temp file in the same directory and
    one rename, so a reader never sees a half-written file.  private (the
    store's own files): missing directories 0700, the file 0600; else the
    umask decides, as for a plain open().  The one routines-side atomic
    writer (DGN-1967 step 4)."""
    d = os.path.dirname(dest) or "."
    if private:
        _makedirs(d)
    else:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(dest) + ".",
                               suffix=".tmp", dir=d)
    try:
        if not private:
            mask = os.umask(0)
            os.umask(mask)
            os.fchmod(fd, 0o666 & ~mask)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(dest, obj):
    """derived/* writer (rebuildable, never hand-written)."""
    atomic_write(dest, json.dumps(obj, ensure_ascii=False, indent=1,
                                  sort_keys=True))


def write_lines_atomic(name, rows):
    """The one non-append store file (owner-turns ring, spec 1)."""
    atomic_write(path(name), "".join(dumps(r) + "\n" for r in rows))


def derived_path(name, root=None):
    return os.path.join(store_dir(root), "derived", name)


def read_derived(name, root=None):
    """A derived/* JSON document, or None when absent or unreadable."""
    try:
        with open(derived_path(name, root), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def next_id(rows, prefix, width, field="id"):
    """Monotonic instance-local id: max existing numeric suffix + 1."""
    top = 0
    for r in rows:
        v = r.get(field)
        if isinstance(v, str) and v.startswith(prefix + "-"):
            tail = v[len(prefix) + 1:]
            if tail.isdigit():
                top = max(top, int(tail))
    return "%s-%0*d" % (prefix, width, top + 1)


def audit(names=CHAINED):
    """Re-validate the hash chain of each chained file.

    Returns a list of breaks: {"file", "line" (1-based), "kind"} where kind is
    "bad-json" or "prev-mismatch".  Detect-only (spec 1, fixture S1)."""
    breaks = []
    for name in names:
        prev_line = None
        for i, line in enumerate(_raw_lines(name), 1):
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError
            except ValueError:
                breaks.append({"file": name, "line": i, "kind": "bad-json"})
                prev_line = line
                continue
            want = sha256_text(prev_line) if prev_line is not None else GENESIS
            if obj.get("prev") != want:
                breaks.append({"file": name, "line": i,
                               "kind": "prev-mismatch"})
            prev_line = line
    return breaks
