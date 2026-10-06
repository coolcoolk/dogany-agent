#!/usr/bin/env python3
"""DGN-1379 R1 -- built-in capability handlers (mode 'builtin:<name>').

These are the handlers a `config/handoff.capabilities` line can select by
name instead of shelling out to an `exec` script (design v2 sec.2.2 mode
list: exec | builtin:<name> | leave | notify). R1 lands exactly one --
`notice-append` -- because it is the only one the ticket names concretely
(DGN-1379 crew-comms-machine.md, per-type machine handler table for a domain agent:
decision.notice ULID-dedups then appends to notices.log; it is a memory-
accumulation input, not a user-facing event). Adding a new builtin is a
capability-model change (a new mode value would not be); it belongs in a
future ticket, not a silent addition here.

Handler contract (shared with `exec` mode, capability model item c/d):
  handler(meta, body, path, root) -> handoff.VERDICT_DONE | VERDICT_LEAVE
  A handler must never raise for a well-formed, already-validated message
  (validation is the caller's job before dispatch); an exception here is
  the engine's cue to leave the message for the next sweep and count it
  as error, exactly like handoff.consume()'s handler contract.

No live wiring yet: nothing in this R1 landing calls these functions --
the dispatch loop that reads `config/handoff.capabilities` and drives
these handlers against a live slug mailbox is R2 (DGN-1379 design v3.1
sec.4.3). This file is a known, acknowledged R1 gap (design v3.1 sec.8
item 4: none of the existing instances import capability/engine/builtins
yet), not a defect -- the fixture tests in this landing exercise it
directly, without a consumer.

NAMING LANDMINE (flag for whoever writes the R2 consumer): this file is
named builtins.py per the locked R1 landing list, but Python's own
`builtins` stdlib module is ALWAYS already present in sys.modules before
any user code runs. A plain `import builtins` from R2 code will resolve
to the REAL stdlib module, not this file, silently -- it does not raise.
Load this file with importlib.util.spec_from_file_location under a
distinct registered name (see routines/tests/
test-dgn1379-handoff-lib-landing.py's _load_module for the pattern);
never via a bare `import builtins`.

English/ASCII only.
"""

import os

import handoff

NOTICES_IDX_NAME = "notices.idx"
NOTICES_LOG_NAME = "notices.log"


def _notices_idx_path(root):
    return os.path.join(handoff.handoff_dir(root), NOTICES_IDX_NAME)


def _notices_log_path(root):
    return os.path.join(handoff.handoff_dir(root), NOTICES_LOG_NAME)


def _notices_seen(root):
    idx = _notices_idx_path(root)
    seen = set()
    if os.path.isfile(idx):
        with open(idx, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    seen.add(line)
    return seen


def notice_append(meta, body, path, root):
    """builtin:notice-append -- decision.notice handler.

    ULID-dedups against <handoff-dir>/notices.idx (capability model item
    b: idempotent per-type key, here the message ulid -- the same shape
    as handoff.py's consumed.idx, kept as a SEPARATE file because a
    decision.notice is a memory-accumulation input, not the same
    consumption event as an inbox->archive move). A fresh message is
    appended to <handoff-dir>/notices.log as one plain-text record
    (from/created/type + body, matching the append-only ledger shape the
    ticket describes); a duplicate ulid is a silent no-op -- still
    VERDICT_DONE, because "already recorded" is success, not failure.

    Always returns handoff.VERDICT_DONE: this is a memory-accumulation
    sink, never a user-facing event (ticket: not a user-facing event) --
    there is no reason for it to leave a message in the inbox once its
    content is durably appended.
    """
    ulid = str(meta.get("id") or "")
    os.makedirs(handoff.handoff_dir(root), exist_ok=True)
    if ulid and ulid in _notices_seen(root):
        return handoff.VERDICT_DONE
    with open(_notices_log_path(root), "a", encoding="utf-8") as fh:
        fh.write("%s %s from=%s type=%s\n"
                  % (meta.get("created", handoff.now_utc()), ulid,
                     meta.get("from", ""), meta.get("type", "")))
        if body:
            fh.write("  %s\n" % body.replace("\n", "\n  "))
    if ulid:
        with open(_notices_idx_path(root), "a", encoding="utf-8") as fh:
            fh.write(ulid + "\n")
    return handoff.VERDICT_DONE


BUILTINS = {
    "notice-append": notice_append,
}


def resolve_builtin(name):
    """Return the callable for a `builtin:<name>` mode, or None for an
    unknown name -- the caller's cue for the same fail-safe leave-and-log
    branch as a missing `exec` handler script (capability model item f)."""
    return BUILTINS.get(name)
