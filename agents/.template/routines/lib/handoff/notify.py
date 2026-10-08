#!/usr/bin/env python3
"""DGN-1774 -- notify pass for `engine.py consume` (session inject).

Why this exists: the template handoff-watch plist runs
`handoff-engine.sh consume`. Before this module, that path only
dispatched through config/handoff.capabilities; a type with no handler
(or an absent capabilities file -- every live instance) was counted as
"left" and nothing else happened. The message sat in the inbox and the
session was never told. One instance's watcher was switched onto that path by
the update/mint_run plist refresh and its inbox piled up silently from
2026-09-27 on, while other instances (on their own instance-local
handoff-consume.sh notifier) kept receiving.

Contract (same as the instance-local handoff-consume.sh notifiers, the
DGN-635 session-triage model): every *.md file still in the own slug
inbox (plus the legacy <root>/files/handoff/inbox, transition only)
after the handler pass is un-triaged. Each one gets exactly ONE session
turn injected into <root>/.telegram_bot/session-inbox (the bridge's
DGN-217 watcher). The inject text points at the inbox file; the session
triages and moves it to done/ -- file location IS the ack state. This
module never moves, archives or records a message as consumed.

Re-notify suppression: <mailbox>/.notify.idx holds already-notified
inbox paths (same file + format as handoff-consume.sh, so an instance
moving between the two paths neither loses nor repeats notifications).
Entries are pruned once the file leaves the inbox. A path is recorded
ONLY after its inject succeeded: a failed inject is logged loudly and
retried on the next run instead of being marked done and forgotten.
flock on <mailbox>/.notify.lock keeps concurrent runs single.

Fallback on inject failure: log only (handoff.log + stderr), no owner
push -- DGN-1561 posture (owner opted out of handoff push traffic on
2026-09-15; a path in the text also trips push.sh's execution-identifier
guard). The file stays in the inbox and is retried next run.

Test seams: HANDOFF_SESSION_INBOX overrides the session-inbox dir;
DOGANY_HANDOFF_ROOT (handoff.mailbox_base) overrides the mailbox base.

English/ASCII only.
"""

import fcntl
import json
import os

import handoff

NOTIFY_IDX_NAME = ".notify.idx"
NOTIFY_LOCK_NAME = ".notify.lock"


def session_inbox_dir(root):
    return os.environ.get(
        "HANDOFF_SESSION_INBOX",
        os.path.join(root, ".telegram_bot", "session-inbox"))


def _log(root, msg):
    try:
        logdir = os.path.join(root, ".telegram_bot", "logs")
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, "handoff.log"), "a") as fh:
            fh.write("%s %s\n" % (handoff.now_utc(), msg))
    except OSError:
        pass


def _scan(box):
    if not os.path.isdir(box):
        return []
    return sorted(os.path.join(box, n) for n in os.listdir(box)
                  if n.endswith(".md") and os.path.isfile(os.path.join(box, n)))


def _inject(session_inbox, text, handoff_id):
    """Atomic drop into the bridge session-inbox (*.md only is picked up).
    Returns None on success, else the failure reason."""
    if not os.path.isdir(session_inbox):
        return "session-inbox missing: %s" % session_inbox
    fname = "handoff-%s-%s.md" % (
        handoff.now_utc().replace(":", ""), (handoff_id or "0")[-8:])
    tmp = os.path.join(session_inbox, "." + fname)
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
        os.rename(tmp, os.path.join(session_inbox, fname))
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "inject write failed: %s" % e
    return None


def inject_text(path, done_path, sender, mtype, subject):
    return (
        "[cron-inject] handoff arrived -- from %s, type %s, subject: %s.\n"
        "Read the INBOX file (%s), triage it NOW (absorb into an existing "
        "ticket / new ticket / backlog / dismiss), apply bookkeeping, then "
        "move the file out of the inbox:\n"
        "  mv '%s' '%s'\n"
        "(inbox = un-triaged; the move is the ack). Report the verdict to "
        "the owner in Korean, briefly. If it duplicates an already-triaged "
        "item, say so in one line.\n"
        "REPLY BACK (DGN-1119): moving the file to done/ is an ACK, NOT a "
        "reply -- the sender never sees it. If this message asks anything "
        "of you (a question, a decision, a confirmation, or a correction of "
        "the sender's record), send the answer back BEFORE the mv:\n"
        "  routines/handoff-engine.sh submit --to-slug %s --type "
        "decision.notice --payload-json '{\"subject\": \"<subject>\"}' "
        "--body-file <body-file>\n"
        "Pure one-way notices needing nothing from you: skip the reply "
        "(a forced reply on every notice is noise)."
        % (sender, mtype, subject, path, path, done_path, sender))


def _describe(path):
    try:
        with open(path) as fh:
            meta, _body = handoff.parse_message(fh.read())
    except (ValueError, OSError):
        meta = {}
    sender = str(meta.get("from") or "?")
    mtype = str(meta.get("type") or "?")
    payload = meta.get("payload")
    subject = ""
    if isinstance(payload, dict):
        subject = str(payload.get("subject") or "")
    return str(meta.get("id") or ""), sender, mtype, subject or mtype


def notify_pending(root, slug, log=None):
    """Inject one session turn per not-yet-notified inbox file.

    Returns {"notified": n, "pending": p, "failed": f, "lock_busy": b}.
    Never raises for a per-file problem; an unparseable file is still
    surfaced (sender/type '?') -- the session decides what it is.
    """
    log = log or (lambda s: None)
    stats = {"notified": 0, "pending": 0, "failed": 0, "lock_busy": False}
    mailbox = handoff.mailbox_root(slug)
    if not os.path.isdir(mailbox):
        return stats
    lock_fd = os.open(os.path.join(mailbox, NOTIFY_LOCK_NAME),
                      os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stats["lock_busy"] = True
            return stats
        idx = os.path.join(mailbox, NOTIFY_IDX_NAME)
        notified = set()
        if os.path.isfile(idx):
            with open(idx) as fh:
                notified = {ln.strip() for ln in fh if ln.strip()}

        present = [(p, handoff.mailbox_done(slug))
                   for p in _scan(handoff.mailbox_inbox(slug))]
        present += [(p, handoff.archive_dir(root))
                    for p in _scan(handoff.inbox_dir(root))]
        stats["pending"] = len(present)
        session_inbox = session_inbox_dir(root)
        for path, done_dir in present:
            if path in notified:
                continue
            ulid, sender, mtype, subject = _describe(path)
            fname = os.path.basename(path)
            text = inject_text(path, os.path.join(done_dir, fname),
                               sender, mtype, subject)
            err = _inject(session_inbox, text, ulid)
            if err is None:
                notified.add(path)
                stats["notified"] += 1
                _log(root, "notify (inject) %s from=%s type=%s"
                     % (fname, sender, mtype))
            else:
                stats["failed"] += 1
                msg = ("notify (inject FAILED, retry next run) %s from=%s "
                       "type=%s subject=%s file=%s -- %s"
                       % (fname, sender, mtype, subject, path, err))
                _log(root, msg)
                log(msg)

        keep = sorted(notified & {p for p, _ in present})
        tmp = idx + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("".join(p + "\n" for p in keep))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, idx)
        _log(root, "notify stats: %s" % json.dumps(stats))
        return stats
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
