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
    "migration.request",    # DGN-277 finding 9: domain agent -> main agent migration request
)

ARCHIVE_KEEP_DAYS = 60
EMPTY_SCANS_TO_EXIT = 2
DEFAULT_RUN_CAP = 10          # dec-013 per-run processing cap (config-tunable)

VERDICT_DONE = "done"
VERDICT_LEAVE = "leave"


def section_still_valid(meta, now):
    """Same-day validity predicate for report.section.* (grill-final
    FATAL-1). A section's expires field equals the aggregation deadline
    the main agent's briefing fires AT -- so `expires < now` alone must never
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


def dead_dir(root):
    """DGN-635 FIX-1: expired/unhandled messages land here, never a silent
    idx+archive. Receiver-owned (created on demand by the consume side)."""
    return os.path.join(handoff_dir(root), "dead")


def dead_notes_path(root):
    return os.path.join(dead_dir(root), "notes.log")


def ensure_channel(root):
    """Create the channel directory skeleton (idempotent)."""
    for d in (inbox_dir(root), attachments_dir(root), archive_dir(root),
              os.path.join(root, "files", "tmp")):
        os.makedirs(d, exist_ok=True)


# -- maildir-by-slug mailboxes (DGN-635 spec v1, grill v2 LOCKED) ------------
# Fixed home-anchored tree, addressed by immutable slug:
#   ~/.dogany/handoff/<slug>/{inbox,done,dead,tmp}
# The address IS the rule -- no registry/tsv/resolve-peer layer. The
# RECIPIENT owns provisioning (mint-time); a sender NEVER creates a mailbox
# (grill v2 FATAL-1 guard: missing mailbox = loud fail, no makedirs).
# DOGANY_HANDOFF_ROOT env overrides the base for tests/fixtures only.
def mailbox_base():
    return os.environ.get(
        "DOGANY_HANDOFF_ROOT",
        os.path.join(os.path.expanduser("~"), ".dogany", "handoff"))


def mailbox_root(slug):
    return os.path.join(mailbox_base(), slug)


def mailbox_inbox(slug):
    return os.path.join(mailbox_root(slug), "inbox")


def mailbox_done(slug):
    return os.path.join(mailbox_root(slug), "done")


def mailbox_dead(slug):
    return os.path.join(mailbox_root(slug), "dead")


def mailbox_tmp(slug):
    return os.path.join(mailbox_root(slug), "tmp")


class MailboxMissingError(RuntimeError):
    """Slug mailbox not provisioned. Senders must fail LOUD here -- never
    create the tree themselves (recipient/mint owns provisioning)."""


def submit_to_slug(to_slug, meta, body=""):
    """Slug-addressed submit (DGN-635): atomic drop into
    ~/.dogany/handoff/<to_slug>/inbox/.

    REQUIRES the recipient mailbox tree to pre-exist -- raises
    MailboxMissingError naming the missing path otherwise (no ensure/
    makedirs on the sender path, spec FATAL-1 guard). The tmp file lives
    INSIDE the mailbox tree so os.replace stays same-filesystem atomic.
    meta must carry from/type; to/id/created are filled. Returns the
    final inbox path.
    """
    root = mailbox_root(to_slug)
    box = mailbox_inbox(to_slug)
    tmp_dir = mailbox_tmp(to_slug)
    for d in (root, box, tmp_dir):
        if not os.path.isdir(d):
            raise MailboxMissingError(
                "handoff mailbox missing: %s -- recipient '%s' mailbox is "
                "not provisioned (%s/{inbox,done,dead,tmp}); the sender "
                "never creates it. Run routines/handoff-provision.sh on the "
                "recipient's own instance ('%s') first -- it provisions its "
                "own slug only and cannot provision a peer."
                % (d, to_slug, root, to_slug))
    meta = dict(meta)
    meta["to"] = to_slug
    meta.setdefault("id", new_ulid())
    meta.setdefault("created", now_utc())
    meta.setdefault("reply_to", None)
    if meta.get("type") not in TYPES_V1:
        raise ValueError("handoff type not in v1 closed list: %r"
                         % meta.get("type"))
    fname = message_filename(meta)
    tmp_path = os.path.join(tmp_dir, fname + ".part")
    with open(tmp_path, "w") as f:
        f.write(dump_message(meta, body))
        f.flush()
        os.fsync(f.fileno())
    final = os.path.join(box, fname)
    if os.path.exists(final):
        # belt: never overwrite a DIFFERENT pending message (full ulid is
        # unique by contract).
        final = os.path.join(box, "%s-%s-%s.md"
                             % (meta["created"][:10].replace("-", ""),
                                meta["type"], meta["id"]))
    os.replace(tmp_path, final)   # atomic on same filesystem
    return final


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


def dead_message(root, path, note=None, now=None):
    """DGN-635 FIX-1: park an expired/unhandled message in dead/ (visible,
    sweep-covered) instead of a silent idx+archive. Note goes to
    dead/notes.log for audit. Receiver-owned dir, created on demand."""
    os.makedirs(dead_dir(root), exist_ok=True)
    dst = os.path.join(dead_dir(root), os.path.basename(path))
    os.replace(path, dst)
    if note:
        with open(dead_notes_path(root), "a") as f:
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
            log=None, on_dead=None):
    """Consume the inbox under the single-consumer lock.

    handlers: {type_or_prefix: callable(meta, body, path) -> verdict}.
      Exact type match wins; a key ending in '.' matches as prefix
      (e.g. 'report.section.'). Verdicts: VERDICT_DONE (idx + archive),
      VERDICT_LEAVE (stay in inbox). A handler exception leaves the file
      in the inbox for the next sweep (ulid not recorded -> replay).
    cap: max handler invocations this run (dec-013). Dedup/expiry
      archivals are bookkeeping, not processing -- they do not count.
    on_dead: callable(meta, dead_path) fired when an expired message is
      moved to dead/ (DGN-635 FIX-1 -- wire the owner push here; the CLI
      wrappers pass a push.sh-backed callback). Best-effort: an on_dead
      exception is logged, never fatal.
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
                    # DGN-635 FIX-1: expired-unhandled goes to dead/ + owner
                    # push (via on_dead), NEVER a silent idx+archive. The
                    # ulid is not recorded: the message was never consumed.
                    dst = dead_message(root, path,
                                       note="expired unhandled (expires=%s)"
                                       % exp, now=now)
                    if on_dead is not None:
                        try:
                            on_dead(meta, dst)
                        except Exception as e:    # noqa: BLE001 -- push is
                            # best-effort; the dead/ file + sweep remain.
                            log("on_dead callback failed: %s" % e)
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


# -- slug mailbox consume (DGN-1379-SPEC-slug-consume-layer S1) -------------
# consume() above is bound to the legacy <root>/files/handoff/{inbox,
# archive} layout. The slug mailbox (~/.dogany/handoff/<slug>/{inbox,done,
# dead,tmp}) is a different physical layout, so it gets its own consumer
# function rather than a parameterized consume() -- consume()'s external
# contract (signature/return/behaviour) is frozen and must not change.
# consume_mailbox() duplicates consume()'s scan/parse/dedup/expire/handler/
# verdict loop on purpose (spec: "extraction only as a helper BOTH
# functions call; if extraction is risky, duplicate -- duplication beats
# regression"). Only the move targets differ:
#   verdict done / dedup / unparseable / missing-id -> mailbox_done(slug)
#   expired (unhandled)                              -> mailbox_dead(slug)
#   verdict leave                                    -> stays in inbox
# (this mirrors consume()'s own mapping: archive_message() is its "done"
# sink for all four of those cases, dead_message() is expiry-only.)


def mailbox_idx_path(slug):
    return os.path.join(mailbox_root(slug), "consumed.idx")


def mailbox_lock_path(slug):
    return os.path.join(mailbox_root(slug), ".consume.lock")


def mailbox_notes_path(slug):
    return os.path.join(mailbox_done(slug), "notes.log")


def mailbox_dead_notes_path(slug):
    return os.path.join(mailbox_dead(slug), "notes.log")


class MailboxConsumeLock(object):
    """flock(2) on <mailbox_root(slug)>/.consume.lock -- the slug-mailbox
    single-consumer lock. Deliberately a SEPARATE file from the legacy
    ConsumeLock's files/handoff/.consume.lock (S1: "슬러그 메일함 기준으로
    잡는다, 레거시 락과 별개 파일")."""

    def __init__(self, slug):
        self.path = mailbox_lock_path(slug)
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


def _mailbox_scan(slug):
    box = mailbox_inbox(slug)
    if not os.path.isdir(box):
        return []
    names = sorted(n for n in os.listdir(box)
                   if n.endswith(".md") and
                   os.path.isfile(os.path.join(box, n)))
    return [os.path.join(box, n) for n in names]


def _mailbox_consumed_ids(slug):
    ids = set()
    idx = mailbox_idx_path(slug)
    if os.path.isfile(idx):
        with open(idx) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(line)
    return ids


def _mailbox_record_consumed(slug, ulid):
    idx = mailbox_idx_path(slug)
    os.makedirs(os.path.dirname(idx), exist_ok=True)
    with open(idx, "a") as f:
        f.write(ulid + "\n")
        f.flush()
        os.fsync(f.fileno())


def _mailbox_move_done(slug, path, note=None, now=None):
    os.makedirs(mailbox_done(slug), exist_ok=True)
    dst = os.path.join(mailbox_done(slug), os.path.basename(path))
    os.replace(path, dst)
    if note:
        with open(mailbox_notes_path(slug), "a") as f:
            f.write("%s %s %s\n" % (now or now_utc(), os.path.basename(path),
                                    note))
    return dst


def _mailbox_move_dead(slug, path, note=None, now=None):
    os.makedirs(mailbox_dead(slug), exist_ok=True)
    dst = os.path.join(mailbox_dead(slug), os.path.basename(path))
    os.replace(path, dst)
    if note:
        with open(mailbox_dead_notes_path(slug), "a") as f:
            f.write("%s %s %s\n" % (now or now_utc(), os.path.basename(path),
                                    note))
    return dst


def consume_mailbox(slug, handlers, cap=DEFAULT_RUN_CAP, now=None,
                    lock=None, log=None, on_dead=None):
    """Slug-mailbox counterpart of consume() -- same handler contract,
    same stats dict shape (keys + order), same rescan-until-idle loop.
    See the module-level comment above for the move-target mapping.
    """
    now = now or now_utc()
    log = log or (lambda s: None)
    stats = {"processed": 0, "deduped": 0, "expired": 0, "left": 0,
             "errors": 0, "scans": 0, "lock_busy": False}
    own_lock = None
    if lock is None:
        own_lock = MailboxConsumeLock(slug)
        if not own_lock.acquire(blocking=False):
            stats["lock_busy"] = True
            return stats
    try:
        seen_left = set()
        empty_scans = 0
        while empty_scans < EMPTY_SCANS_TO_EXIT:
            stats["scans"] += 1
            actionable = 0
            for path in _mailbox_scan(slug):
                if path in seen_left:
                    continue
                try:
                    with open(path) as f:
                        meta, body = parse_message(f.read())
                except (ValueError, OSError) as e:
                    _mailbox_move_done(slug, path,
                                       note="unparseable: %s" % e, now=now)
                    stats["errors"] += 1
                    actionable += 1
                    continue
                ulid = str(meta.get("id") or "")
                if not ulid:
                    _mailbox_move_done(slug, path, note="missing id", now=now)
                    stats["errors"] += 1
                    actionable += 1
                    continue
                if ulid in _mailbox_consumed_ids(slug):
                    # dedup: already processed (crash between idx and
                    # done-move, or duplicate drop) -> move-only.
                    _mailbox_move_done(slug, path,
                                       note="dedup: already consumed", now=now)
                    stats["deduped"] += 1
                    actionable += 1
                    continue
                exp = meta.get("expires")
                if exp and str(exp) < now and not section_still_valid(meta,
                                                                      now):
                    dst = _mailbox_move_dead(
                        slug, path,
                        note="expired unhandled (expires=%s)" % exp, now=now)
                    if on_dead is not None:
                        try:
                            on_dead(meta, dst)
                        except Exception as e:    # noqa: BLE001 -- push is
                            # best-effort; the dead/ file + sweep remain.
                            log("on_dead callback failed: %s" % e)
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
                _mailbox_record_consumed(slug, ulid)
                _mailbox_move_done(slug, path, now=now)
            if actionable == 0:
                empty_scans += 1
            else:
                empty_scans = 0
        return stats
    finally:
        if own_lock is not None:
            own_lock.release()


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
