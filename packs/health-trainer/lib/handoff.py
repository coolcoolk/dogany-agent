#!/usr/bin/env python3
"""agent handoff channel v1 -- DGN-238 v3 section 5.1 (+ dec-013 B').

Physical contract (spec verbatim):
  - inbox  : <recipient_root>/files/handoff/inbox/          (producers write here)
  - attach : <recipient_root>/files/handoff/inbox/attachments/
  - archive: <recipient_root>/files/handoff/archive/        (consumer-owned)
  - idx    : <recipient_root>/files/handoff/consumed.idx    (ulid per line)
  - lock   : <recipient_root>/files/handoff/.consume.lock   (single consumer)
  - message: single md file, YAML frontmatter + markdown body,
             filename <YYYYMMDD>-<type>-<ulid8>.md
  - write protocol: producer writes into recipient files/tmp/, then one
    atomic rename into inbox. Immutable after placement.
  - consume protocol (order FIXED): (1) process -> (2) append ulid to
    consumed.idx + fsync -> (3) move to archive. A sweep that finds a ulid
    already in consumed.idx skips processing and archives only. Crash
    between 1 and 2 = re-sweep re-processes; safe because every type
    handler is idempotent on the ulid key.
  - single consumer rule: EVERY path that reads inbox or moves files to
    archive holds .consume.lock (flock).
  - per-run cap (dec-013): at most N handler invocations per consume run;
    the remainder stays in inbox for the next run.
  - rescan loop: after acquiring the lock, rescan until 2 consecutive
    scans yield no actionable message.
  - retention: monthly consumed.idx rotation (keep current + previous
    month), archive files older than 60 days deleted.

Frontmatter is a STRICT YAML SUBSET owned by this module on both ends
(writer + parser, stdlib only -- no PyYAML dependency in launchd context):
top-level scalar keys plus one nested one-level map under `payload:`.
The output is valid YAML, so a later swap to a full parser changes no
wire bytes.

English/ASCII only. No live side effects: every function takes explicit
roots/paths.
"""

import datetime
import errno
import fcntl
import json
import os
import secrets
import shutil
import time

TYPES_V1 = (
    "report.section.morning",
    "report.section.retro",
    "report.section.weekly",
    "proposal.schedule",
    "decision.notice",
    "redirect.utterance",   # active only under dec-013 branch B' (adopted)
    "migration.request",    # DGN-277 finding 9: domain agent -> Ag migration request
)

ARCHIVE_KEEP_DAYS = 60
EMPTY_SCANS_TO_EXIT = 2
DEFAULT_RUN_CAP = 10          # dec-013 per-run processing cap (config-tunable)

VERDICT_DONE = "done"
VERDICT_LEAVE = "leave"


def section_still_valid(meta, now):
    """Same-day validity predicate for report.section.* (grill-final
    FATAL-1). A section's expires field equals the aggregation deadline
    the Ag briefing fires AT -- so `expires < now` alone must never
    archive a section on its own target day (the aggregation step, or a
    delayed briefing later the same day, is still its consumer). Archive-
    expiry for sections therefore fires only once the target day is over
    (created day < now day, UTC). Non-section types keep plain expires
    semantics."""
    if not str(meta.get("type", "")).startswith("report.section."):
        return False
    return str(meta.get("created", ""))[:10] == str(now)[:10]


# -- ulid ------------------------------------------------------------------
# Same algorithm as database/lifekit.py new_ulid (48-bit ms timestamp +
# 80 random bits, Crockford base32). Duplicated here (15 lines) so the
# channel library stays importable on both sides without cross-tree
# sys.path surgery; the value format is byte-compatible.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value, length):
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(ts_ms=None):
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    ts_part = _encode(ts_ms & ((1 << 48) - 1), 10)
    rand_part = _encode(secrets.randbits(80), 16)
    return ts_part + rand_part


# -- time ------------------------------------------------------------------
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


# -- paths -----------------------------------------------------------------
def handoff_dir(root):
    return os.path.join(root, "files", "handoff")


def inbox_dir(root):
    return os.path.join(handoff_dir(root), "inbox")


def attachments_dir(root):
    return os.path.join(inbox_dir(root), "attachments")


def archive_dir(root):
    return os.path.join(handoff_dir(root), "archive")


def idx_path(root):
    return os.path.join(handoff_dir(root), "consumed.idx")


def lock_path(root):
    return os.path.join(handoff_dir(root), ".consume.lock")


def notes_log_path(root):
    return os.path.join(archive_dir(root), "notes.log")


def ensure_channel(root):
    """Create the channel directory skeleton (idempotent)."""
    for d in (inbox_dir(root), attachments_dir(root), archive_dir(root),
              os.path.join(root, "files", "tmp")):
        os.makedirs(d, exist_ok=True)


# -- frontmatter subset ----------------------------------------------------
def _dump_scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    # quote anything that is not a plainly safe token
    safe = s and all(c.isalnum() or c in "-_.:/@ " for c in s) and \
        s == s.strip() and not s.startswith(("-", "?", "&", "*")) and \
        s.lower() not in ("null", "true", "false", "~") and \
        not _looks_numeric(s)
    return s if safe else json.dumps(s)


def _looks_numeric(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _parse_scalar(tok):
    tok = tok.strip()
    if tok == "" or tok == "null" or tok == "~":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    if tok.startswith('"'):
        return json.loads(tok)
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def dump_message(meta, body=""):
    """Serialize meta (flat keys + optional payload dict) + body to text."""
    lines = ["---"]
    for key in ("id", "from", "to", "type", "created", "reply_to", "expires"):
        if key in meta:
            lines.append("%s: %s" % (key, _dump_scalar(meta[key])))
    payload = meta.get("payload")
    if payload is not None:
        lines.append("payload:")
        for k in payload:
            lines.append("  %s: %s" % (k, _dump_scalar(payload[k])))
    lines.append("---")
    text = "\n".join(lines) + "\n"
    if body:
        text += body.rstrip("\n") + "\n"
    return text


def parse_message(text):
    """Parse a channel message -> (meta dict, body str). Strict subset."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("handoff message: missing frontmatter open")
    meta = {}
    payload = None
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            i += 1
            break
        if line.startswith("  ") and payload is not None:
            k, _, v = line.strip().partition(":")
            payload[k.strip()] = _parse_scalar(v)
        elif line.strip() == "payload:":
            payload = {}
            meta["payload"] = payload
        elif ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = _parse_scalar(v)
            if k.strip() != "payload":
                payload = None
        i += 1
    else:
        raise ValueError("handoff message: missing frontmatter close")
    body = "\n".join(lines[i:]).strip("\n")
    return meta, body


# -- submit (producer side) -------------------------------------------------
def message_filename(meta):
    # <ulid8> = the LAST 8 chars (random bits). The first 8 are coarse
    # timestamp bits and collide for any two messages within hours --
    # a same-name overwrite would silently destroy a distinct message.
    day = meta["created"][:10].replace("-", "")
    return "%s-%s-%s.md" % (day, meta["type"], meta["id"][-8:])


def submit(recipient_root, meta, body="", attachments=None):
    """Producer entrypoint: atomic drop into the recipient inbox.

    meta must carry from/to/type; id/created are filled if absent.
    attachments: list of source file paths, copied into
    inbox/attachments/<ulid8>-<basename> BEFORE the md drop (so an
    event-ping consumer never sees a message whose attachments are
    missing). payload gains 'attachments' with the relative paths.
    Returns the final inbox path.
    """
    meta = dict(meta)
    meta.setdefault("id", new_ulid())
    meta.setdefault("created", now_utc())
    meta.setdefault("reply_to", None)
    if meta.get("type") not in TYPES_V1:
        raise ValueError("handoff type not in v1 closed list: %r"
                         % meta.get("type"))
    ensure_channel(recipient_root)
    if attachments:
        rels = []
        for src in attachments:
            rel = os.path.join("attachments",
                               "%s-%s" % (meta["id"][:8], os.path.basename(src)))
            dst = os.path.join(inbox_dir(recipient_root), rel)
            shutil.copy2(src, dst)
            rels.append(rel)
        payload = dict(meta.get("payload") or {})
        payload["attachments"] = ",".join(rels)
        meta["payload"] = payload
    fname = message_filename(meta)
    tmp_dir = os.path.join(recipient_root, "files", "tmp")
    tmp_path = os.path.join(tmp_dir, fname + ".part")
    with open(tmp_path, "w") as f:
        f.write(dump_message(meta, body))
        f.flush()
        os.fsync(f.fileno())
    final = os.path.join(inbox_dir(recipient_root), fname)
    if os.path.exists(final):
        # belt: never overwrite a DIFFERENT pending message; fall back to
        # the full ulid (unique by contract).
        final = os.path.join(inbox_dir(recipient_root),
                             "%s-%s-%s.md" % (meta["created"][:10]
                                              .replace("-", ""),
                                              meta["type"], meta["id"]))
    os.replace(tmp_path, final)   # atomic on same filesystem
    return final


# -- lock -------------------------------------------------------------------
class ConsumeLock(object):
    """flock(2) on files/handoff/.consume.lock. Held by every inbox reader."""

    def __init__(self, root):
        self.path = lock_path(root)
        self._fd = None

    def acquire(self, blocking=False):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES):
                return False
            raise
        self._fd = fd
        return True

    def release(self):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        if not self.acquire(blocking=False):
            raise RuntimeError("consume lock busy: %s" % self.path)
        return self

    def __exit__(self, *exc):
        self.release()


# -- consumed.idx ------------------------------------------------------------
def consumed_ids(root):
    """Set of consumed ulids: consumed.idx plus rotated consumed.idx.*."""
    ids = set()
    hdir = handoff_dir(root)
    if not os.path.isdir(hdir):
        return ids
    for name in os.listdir(hdir):
        if name == "consumed.idx" or name.startswith("consumed.idx."):
            with open(os.path.join(hdir, name)) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ids.add(line)
    return ids


def record_consumed(root, ulid):
    """Step (2): append + fsync BEFORE the archive move."""
    with open(idx_path(root), "a") as f:
        f.write(ulid + "\n")
        f.flush()
        os.fsync(f.fileno())


def archive_message(root, path, note=None, now=None):
    """Step (3): move to archive. Optional reason note (never silent loss)."""
    os.makedirs(archive_dir(root), exist_ok=True)
    dst = os.path.join(archive_dir(root), os.path.basename(path))
    os.replace(path, dst)
    if note:
        with open(notes_log_path(root), "a") as f:
            f.write("%s %s %s\n" % (now or now_utc(), os.path.basename(path),
                                    note))
    return dst


# -- consume (consumer side) --------------------------------------------------
def _scan(root):
    box = inbox_dir(root)
    if not os.path.isdir(box):
        return []
    names = sorted(n for n in os.listdir(box)
                   if n.endswith(".md") and
                   os.path.isfile(os.path.join(box, n)))
    return [os.path.join(box, n) for n in names]


def consume(root, handlers, cap=DEFAULT_RUN_CAP, now=None, lock=None,
            log=None):
    """Consume the inbox under the single-consumer lock.

    handlers: {type_or_prefix: callable(meta, body, path) -> verdict}.
      Exact type match wins; a key ending in '.' matches as prefix
      (e.g. 'report.section.'). Verdicts: VERDICT_DONE (idx + archive),
      VERDICT_LEAVE (stay in inbox). A handler exception leaves the file
      in the inbox for the next sweep (ulid not recorded -> replay).
    cap: max handler invocations this run (dec-013). Dedup/expiry
      archivals are bookkeeping, not processing -- they do not count.
    Returns stats dict.
    """
    now = now or now_utc()
    log = log or (lambda s: None)
    stats = {"processed": 0, "deduped": 0, "expired": 0, "left": 0,
             "errors": 0, "scans": 0, "lock_busy": False}
    own_lock = None
    if lock is None:
        own_lock = ConsumeLock(root)
        if not own_lock.acquire(blocking=False):
            stats["lock_busy"] = True
            return stats
    try:
        seen_left = set()
        empty_scans = 0
        while empty_scans < EMPTY_SCANS_TO_EXIT:
            stats["scans"] += 1
            actionable = 0
            for path in _scan(root):
                if path in seen_left:
                    continue
                try:
                    with open(path) as f:
                        meta, body = parse_message(f.read())
                except (ValueError, OSError) as e:
                    archive_message(root, path,
                                    note="unparseable: %s" % e, now=now)
                    stats["errors"] += 1
                    actionable += 1
                    continue
                ulid = str(meta.get("id") or "")
                if not ulid:
                    archive_message(root, path, note="missing id", now=now)
                    stats["errors"] += 1
                    actionable += 1
                    continue
                if ulid in consumed_ids(root):
                    # dedup: already processed (crash between idx and
                    # archive, or duplicate drop) -> archive only.
                    archive_message(root, path, note="dedup: already consumed",
                                    now=now)
                    stats["deduped"] += 1
                    actionable += 1
                    continue
                exp = meta.get("expires")
                if exp and str(exp) < now and not section_still_valid(meta,
                                                                      now):
                    record_consumed(root, ulid)
                    archive_message(root, path,
                                    note="expired unconsumed (expires=%s)"
                                    % exp, now=now)
                    stats["expired"] += 1
                    actionable += 1
                    continue
                handler = _resolve_handler(handlers, str(meta.get("type")))
                if handler is None:
                    seen_left.add(path)
                    stats["left"] += 1
                    log("no handler for type=%s, leaving %s"
                        % (meta.get("type"), os.path.basename(path)))
                    continue
                if stats["processed"] >= cap:
                    # cap reached: remainder waits for the next run.
                    return stats
                try:
                    verdict = handler(meta, body, path)
                except Exception as e:            # noqa: BLE001 -- belt: any
                    # handler crash leaves the message for the next sweep
                    seen_left.add(path)
                    stats["errors"] += 1
                    log("handler error on %s: %s"
                        % (os.path.basename(path), e))
                    continue
                stats["processed"] += 1
                actionable += 1
                if verdict == VERDICT_LEAVE:
                    seen_left.add(path)
                    stats["left"] += 1
                    stats["processed"] -= 1   # leave is not processing
                    actionable -= 1
                    continue
                record_consumed(root, ulid)                    # step 2
                archive_message(root, path, now=now)           # step 3
            if actionable == 0:
                empty_scans += 1
            else:
                empty_scans = 0
        return stats
    finally:
        if own_lock is not None:
            own_lock.release()


def _resolve_handler(handlers, mtype):
    if mtype in handlers:
        return handlers[mtype]
    for key, fn in handlers.items():
        if key.endswith(".") and mtype.startswith(key):
            return fn
    return None


# -- retention ----------------------------------------------------------------
def retention(root, now_dt=None):
    """Monthly idx rotation + 60-day archive cleanup (5.1 retention policy).

    Caller runs this from the daily job on day 1 of the month (rotation)
    and daily (archive cleanup). Rotation: consumed.idx ->
    consumed.idx.<YYYYMM of previous month>; keep current + previous only.

    Holds .consume.lock (blocking): rotation replaces the idx and the
    cleanup deletes archive files, so this path follows the 5.1 single-
    consumer rule like every other idx/archive toucher (grill-final
    MINOR-3 -- was harmless by atomicity, now also by the letter).
    """
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    hdir = handoff_dir(root)
    if not os.path.isdir(hdir):
        return []
    lock = ConsumeLock(root)
    lock.acquire(blocking=True)
    try:
        return _retention_locked(root, now_dt, hdir)
    finally:
        lock.release()


def _retention_locked(root, now_dt, hdir):
    actions = []
    if now_dt.day == 1 and os.path.isfile(idx_path(root)):
        prev = (now_dt.replace(day=1) - datetime.timedelta(days=1))
        rotated = idx_path(root) + "." + prev.strftime("%Y%m")
        os.replace(idx_path(root), rotated)
        actions.append("rotated consumed.idx -> %s" % os.path.basename(rotated))
        # keep only the newest rotated file
        rots = sorted(n for n in os.listdir(hdir)
                      if n.startswith("consumed.idx."))
        for name in rots[:-1]:
            os.remove(os.path.join(hdir, name))
            actions.append("dropped old idx %s" % name)
    cutoff = now_dt - datetime.timedelta(days=ARCHIVE_KEEP_DAYS)
    adir = archive_dir(root)
    if os.path.isdir(adir):
        for name in os.listdir(adir):
            p = os.path.join(adir, name)
            if name == "notes.log" or not os.path.isfile(p):
                continue
            if datetime.datetime.fromtimestamp(
                    os.path.getmtime(p), datetime.timezone.utc) < cutoff:
                os.remove(p)
                actions.append("dropped archived %s (>%dd)"
                               % (name, ARCHIVE_KEEP_DAYS))
    return actions
