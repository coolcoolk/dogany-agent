#!/usr/bin/env python3
"""Handoff channel CLI -- shared by the shell entrypoints on both sides.

Subcommands:
  consume   --root R --side {warg|ag} [--cap N] [--now ISO] [--db PATH]
            [--peer name=root ...]
  submit    --to-root R --from A --to B --type T [--body-file F]
            [--payload-json J] [--expires ISO] [--attach F ...]
  retention --root R [--now ISO]

Side handler maps (v3 5.1 type list, dec-013 B' active):
  warg: redirect.utterance -> processor command (headless claude in the
        deployed instance; HANDOFF_REDIRECT_CMD overrides -- REQUIRED in
        sandbox: with no processor configured the message is LEFT in the
        inbox and an error is logged, never silently consumed).
        decision.notice   -> ulid-deduped append to
        files/handoff/notices.log (memory accrual input; append is
        idempotent on the ulid key).
        report.section.*  -> should never arrive at Warg: left + logged.
  ag  : proposal.schedule -> lib/proposal.py executor against the
        instance db (idempotent: created_by stamp / CAS), then a
        decision.notice submit back to the peer inbox.
        report.section.*  -> LEFT for the briefing aggregation step
        (aggregation = the consume path for sections; the sweep must not
        eat them early). Expired sections are archived generically with
        a reason note.

English/ASCII only.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handoff                      # noqa: E402
# NOTE: lib/proposal.py (and its vendor lifekit import) loads lazily
# inside the proposal handler -- the warg-side consume path must work
# without any lifekit on sys.path.


def _log(root):
    logdir = os.path.join(root, ".telegram_bot", "logs")
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, "handoff.log")

    def write(msg):
        with open(path, "a") as f:
            f.write("%s %s\n" % (handoff.now_utc(), msg))
    return write


def _notice_append(root, meta):
    """decision.notice: idempotent append keyed on ulid."""
    path = os.path.join(handoff.handoff_dir(root), "notices.log")
    ulid = str(meta["id"])
    if os.path.isfile(path):
        with open(path) as f:
            if any(line.split("\t", 1)[0] == ulid for line in f):
                return handoff.VERDICT_DONE
    with open(path, "a") as f:
        f.write("%s\t%s\n" % (ulid, json.dumps(meta.get("payload") or {})))
    return handoff.VERDICT_DONE


def _get_cfg(db_path, key):
    """Read a config key from the Warg lifekit.db. Returns str or None."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT value FROM config WHERE key=?",
                           (key,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _set_cfg_db(db_path, key, value):
    """Write a config key to the Warg lifekit.db (same upsert as daily_job)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))
    conn.commit()
    conn.close()


def _spawn_digest(root, log):
    """Spawn digest-run.sh detached (start_new_session). Non-blocking; consume
    returns immediately. Test override: WARG_DIGEST_RUN_CMD env var replaces
    the shell command string (allows stubbing without a real claude invocation).
    """
    cmd_override = os.environ.get("WARG_DIGEST_RUN_CMD")
    if cmd_override:
        # test seam: run the stub command (must be a list-serialised command)
        import shlex
        parts = shlex.split(cmd_override)
        proc = subprocess.Popen(parts)
        log("digest spawn (stub): pid %s" % proc.pid)
        return
    digest_sh = os.path.join(root, "routines", "digest-run.sh")
    if not os.path.isfile(digest_sh):
        log("digest spawn FAILED: digest-run.sh not found at %s" % digest_sh)
        return
    # Scrub CLAUDECODE vars so the spawned digest job can itself invoke
    # headless claude without triggering the "cannot be launched inside
    # another Claude Code session" guard.
    scrubbed = dict(os.environ)
    scrubbed.pop("CLAUDECODE", None)
    scrubbed.pop("CLAUDE_CODE_ENTRYPOINT", None)
    logdir = os.path.join(root, ".telegram_bot", "logs")
    os.makedirs(logdir, exist_ok=True)
    spawn_log = os.path.join(logdir, "digest-spawn.log")
    # Use start_new_session=True instead of nohup: creates a new process
    # group + session so the process survives launchd job teardown without
    # needing a controlling terminal (nohup can fail with "can't detach
    # from console" under launchd and swallows all output via DEVNULL).
    proc = subprocess.Popen(
        [digest_sh],
        stdout=open(spawn_log, "ab"),
        stderr=subprocess.STDOUT,
        cwd=root,
        close_fds=True,
        start_new_session=True,
        env=scrubbed,
    )
    log("digest spawned: pid %s spawn-log=%s" % (proc.pid, spawn_log))


def _handle_migration_complete(root, log, db_path=None):
    """migration_complete notice side-effect (DGN-238 section 6 chain):
    - Read consult_state; if already digesting/ready/done: log + skip.
    - Else: set consult_state=digesting + spawn digest-run.sh DETACHED.
    Idempotent: safe to call on duplicate consume (notices.log dedup also
    guards replay at the outer level, but this is a belt).
    """
    db = db_path or os.path.join(root, "database", "lifekit.db")
    state = _get_cfg(db, "consult_state")
    if state in ("digesting", "ready", "done"):
        log("migration_complete: consult_state=%r, skipping re-spawn" % state)
        return
    _set_cfg_db(db, "consult_state", "digesting")
    log("migration_complete: consult_state=digesting")
    _spawn_digest(root, log)


def warg_handlers(root, log, db_path=None):
    """Handler map for the Warg consume side.

    db_path: Warg lifekit.db path override (tests; default
    <root>/database/lifekit.db). Used for the migration_complete side
    effect only.
    """
    def on_redirect(meta, body, path):
        cmd = os.environ.get("HANDOFF_REDIRECT_CMD")
        if not cmd:
            deployed = os.path.join(root, "routines", "redirect-respond.sh")
            if os.path.isfile(deployed):
                cmd = deployed
        if not cmd:
            raise RuntimeError("no redirect processor configured "
                               "(HANDOFF_REDIRECT_CMD / redirect-respond.sh)")
        subprocess.run([cmd, path], check=True, timeout=600)
        return handoff.VERDICT_DONE

    def on_notice(meta, body, path):
        result = _notice_append(root, meta)
        # migration_complete side effect: set digesting + spawn digest job
        payload = meta.get("payload") or {}
        if str(payload.get("event") or "") == "migration_complete":
            _handle_migration_complete(root, log, db_path=db_path)
        return result

    def on_section(meta, body, path):
        log("report.section at WARG inbox (misrouted): leaving %s"
            % os.path.basename(path))
        return handoff.VERDICT_LEAVE

    return {"redirect.utterance": on_redirect,
            "decision.notice": on_notice,
            "report.section.": on_section}


def ag_handlers(root, log, db_path=None, peers=None):
    peers = peers or {}

    def on_proposal(meta, body, path):
        import sqlite3
        db = db_path or os.path.join(root, "database", "lifekit.db")
        sys.path.insert(0, os.path.join(root, "database"))
        import proposal as proposal_mod   # lazy: needs vendor lifekit
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            notice = proposal_mod.execute(conn, meta)
        finally:
            conn.close()
        peer_root = peers.get(str(meta.get("from")))
        if peer_root:
            handoff.submit(peer_root, notice)
            log("proposal %s -> %s; notice sent"
                % (meta["id"], notice["payload"]["status"]))
        else:
            log("proposal %s -> %s; NO peer root for %r (notice not sent)"
                % (meta["id"], notice["payload"]["status"], meta.get("from")))
        return handoff.VERDICT_DONE

    def on_migration_request(meta, body, path):
        """DGN-277 finding 9: run the domain migration and reply
        decision.notice {event: migration_complete} to the requester.

        payload fields (required):
          domain      -- identifies which migration script to run
                         (currently only "health" is supported)
          target_root -- absolute path to the requester's agent root;
                         migration writes to <target_root>/database/lifekit.db

        Idempotent: the migration script itself is idempotent (keyed on
        ag_source_id / (date,metric) / etc.) so re-consume on crash replay
        is safe.  On any failure the message stays unconsumed for re-sweep
        (VERDICT_LEAVE via exception bubble-up to the consume() error path).
        """
        payload = meta.get("payload") or {}
        domain = str(payload.get("domain") or "")
        target_root = str(payload.get("target_root") or "")
        msg_id = str(meta.get("id") or "")

        log("migration.request %s: domain=%r target_root=%r"
            % (msg_id, domain, target_root))

        # -- resolve migration tooling by domain --------------------------------
        migration_tool = _resolve_migration_tool(root, domain)
        if migration_tool is None:
            # Unknown domain: loud log + leave for re-sweep / human triage.
            log("migration.request %s: UNKNOWN domain %r -- leaving in inbox"
                % (msg_id, domain))
            raise RuntimeError("unknown migration domain: %r" % domain)

        # -- validate target ----------------------------------------------------
        if not target_root:
            log("migration.request %s: missing target_root -- leaving"
                % msg_id)
            raise RuntimeError("migration.request: missing target_root")

        target_db = os.path.join(target_root, "database", "lifekit.db")
        log("migration.request %s: target_db=%s" % (msg_id, target_db))

        # -- run migration (env seam: MIGRATION_CMD overrides in tests) ---------
        cmd_override = os.environ.get("MIGRATION_CMD")
        if cmd_override:
            import shlex
            cmd = shlex.split(cmd_override) + ["--apply", "--target", target_db]
            log("migration.request %s: running stub cmd: %s" % (msg_id, cmd))
        else:
            cmd = ["python3", migration_tool, "--apply", "--target", target_db]
            log("migration.request %s: running %s" % (msg_id, cmd))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log("migration.request %s: FAILED (rc=%d) stderr=%s"
                % (msg_id, result.returncode, result.stderr[:500]))
            raise RuntimeError("migration script exited %d" % result.returncode)

        log("migration.request %s: migration OK (rc=0)" % msg_id)

        # -- reply decision.notice {event: migration_complete} ------------------
        requester_name = str(meta.get("from") or "unknown")
        # requester root: try peers map first; fall back to payload target_root
        # (the requester_root = target_root because the requester is the new
        # agent running at that path)
        requester_root = peers.get(requester_name) or target_root
        notice_meta = {
            "from": "ag",
            "to": requester_name,
            "type": "decision.notice",
            "payload": {"event": "migration_complete",
                        "domain": domain,
                        "reply_to_id": msg_id},
        }
        notice_path = handoff.submit(requester_root, notice_meta)
        log("migration.request %s: notice submitted to %s (%s)"
            % (msg_id, requester_root, os.path.basename(notice_path)))

        return handoff.VERDICT_DONE

    def on_section(meta, body, path):
        # sections are consumed by the briefing aggregation step, not the
        # sweep. Leave verdict keeps them in the inbox untouched.
        return handoff.VERDICT_LEAVE

    return {"proposal.schedule": on_proposal,
            "migration.request": on_migration_request,
            "report.section.": on_section}


# -- domain -> migration script resolver ----------------------------------------
_DOMAIN_MIGRATION_MAP = {
    "health": "database/migrations/health_to_warg.py",
}


def _resolve_migration_tool(ag_root, domain):
    """Return absolute path to the migration script for domain, or None."""
    rel = _DOMAIN_MIGRATION_MAP.get(domain)
    if rel is None:
        return None
    candidate = os.path.join(ag_root, rel)
    if not os.path.isfile(candidate):
        return None
    return candidate


def main(argv=None):
    ap = argparse.ArgumentParser(prog="handoff_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("consume")
    c.add_argument("--root", required=True)
    c.add_argument("--side", choices=("warg", "ag"), required=True)
    c.add_argument("--cap", type=int,
                   default=int(os.environ.get("HANDOFF_RUN_CAP",
                                              handoff.DEFAULT_RUN_CAP)))
    c.add_argument("--now", default=None)
    c.add_argument("--db", default=None,
                   help="db override (tests; default <root>/database/lifekit.db)")
    c.add_argument("--peer", action="append", default=[],
                   help="name=root mapping for notice replies")

    s = sub.add_parser("submit")
    s.add_argument("--to-root", required=True)
    s.add_argument("--from", dest="from_", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--type", required=True)
    s.add_argument("--body-file", default=None)
    s.add_argument("--payload-json", default=None)
    s.add_argument("--expires", default=None)
    s.add_argument("--reply-to", default=None)
    s.add_argument("--attach", action="append", default=[])

    r = sub.add_parser("retention")
    r.add_argument("--root", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "consume":
        log = _log(args.root)
        peers = dict(p.split("=", 1) for p in args.peer)
        if args.side == "warg":
            handlers = warg_handlers(args.root, log, db_path=args.db)
        else:
            handlers = ag_handlers(args.root, log, db_path=args.db,
                                   peers=peers)
        stats = handoff.consume(args.root, handlers, cap=args.cap,
                                now=args.now, log=log)
        print(json.dumps(stats))
        if stats["lock_busy"]:
            log("consume: lock busy, exiting (running instance rescans)")
            return 0
        log("consume: %s" % json.dumps(stats))
        return 0

    if args.cmd == "submit":
        body = ""
        if args.body_file:
            with open(args.body_file) as f:
                body = f.read()
        meta = {"from": args.from_, "to": args.to, "type": args.type}
        if args.payload_json:
            meta["payload"] = json.loads(args.payload_json)
        if args.expires:
            meta["expires"] = args.expires
        if args.reply_to:
            meta["reply_to"] = args.reply_to
        path = handoff.submit(args.to_root, meta, body,
                              attachments=args.attach or None)
        print(path)
        return 0

    if args.cmd == "retention":
        for a in handoff.retention(args.root):
            print(a)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
