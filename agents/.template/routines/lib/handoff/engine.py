#!/usr/bin/env python3
"""DGN-1379 R1 -- engine submit + consume entry points (D9, LOCKED).

D9 decision (design v3.1 sec.4.2 Delta5, carried unchanged through
v3.2/v3.3/v3.4): the engine gets exactly one submit entry point,
`--to-slug`-only:

    python3 routines/lib/handoff/engine.py submit \\
      --to-slug <slug> --type <T> --payload-json <J> [--body-file F]

`--to-root` is deliberately NOT implemented -- not "not yet", permanently
not. That absence is S9's "legacy root submit does not exist in the new
engine" invariant made a fact about the argument surface instead of a
sentence someone could get out of sync with the code. Internally this
calls handoff.submit_to_slug directly; no new submission primitive is
introduced (design v3.1: zero new primitives -- the ladder rule: never
build what a direct call already covers).

DGN-1379-SPEC-consume-entrypoint (owner-locked, 2026-09-19) S1: the
engine also gets exactly one consume entry point:

    python3 routines/lib/handoff/engine.py consume [--cap N]

Same ladder as submit: internally this calls handoff.consume() directly
(zero new consumption primitive). No `--to-root`/`--peer` argument --
the entry point derives its own root by climbing up from its own file
location (own_root(), already used by submit above); no hardcoded path.

S3' (LOCKED, that spec's scope): this module was to import ONLY handoff.py
from this package for the bare-entrypoint landing above. DGN-1379-SPEC-
slug-consume-layer (2026-09-19, this ticket's R1-completion spec)
explicitly extends that scope: its S2/S3 require this module to drive
capability.py + builtins.py (its own scope boundary: "capability.py /
builtins.py 본체 -- 호출만 한다" -- call only, but call they now are). It
must still NEVER import handoff_cli.py -- the cli is retired permanently
(D1: template-resident lib, handoff_cli.py never distributed). There is
no `import handoff_cli` anywhere below; the machine-checkable half of S3'
(`test ! -e .../handoff_cli.py` in the template tree) lives in
tests/install_smoke.sh, and routines/tests/test-dgn1379-handoff-lib-
landing.py asserts this module's source never references handoff_cli
even if a stray copy is later dropped next to it at runtime.

DGN-1774: consume ends with a notify pass (notify.py) -- every message
still in the inbox after dispatch gets one session-inbox inject. A type
with no handler is never silently "left" any more.

English/ASCII only. No live side effects beyond the one submit/consume
(+ its notify pass) this CLI performs when invoked.
"""

import argparse
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import capability  # noqa: E402  -- DGN-1379-SPEC-slug-consume-layer S2
import handoff  # noqa: E402
import notify  # noqa: E402  -- DGN-1774 notify pass after dispatch


def _load_builtins_module():
    """builtins.py cannot be reached via a bare `import builtins`: Python
    always has the real stdlib `builtins` module cached in sys.modules
    before any user code runs, so a plain import silently resolves to
    that instead (see routines/lib/handoff/builtins.py's own NAMING
    LANDMINE docstring, which names this loader as the required pattern
    for exactly this R2/slug-consume-layer dispatch-loop consumer). Load
    by explicit file path under a private name instead.
    """
    path = os.path.join(_HERE, "builtins.py")
    spec = importlib.util.spec_from_file_location(
        "dgn1379_slug_consume_builtins", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handoff_builtins = _load_builtins_module()  # noqa: E402


def own_root():
    """<root>/routines/lib/handoff/engine.py -> <root>."""
    return os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))


def own_slug(root=None):
    """Derive the sender's own slug for the auto-generated `from` field.

    R1 hardness case 1 (design v3.3 Delta5', 2nd grill's requirement):
    engine submit derives meta["from"] from the sender's own root -- the
    raw primitive (handoff.submit_to_slug) does NOT auto-generate `from`,
    so this derivation belongs here, not in handoff.py. Resolution order
    matches the onboarding leg's own slug derivation (routines/
    onboarding-check.py): config/agent.conf SLUG= first, else the
    workspace directory's own basename, lowercased.
    """
    root = root or own_root()
    conf_lib = os.path.join(root, "routines", "lib")
    slug = ""
    added = conf_lib not in sys.path
    if added:
        sys.path.insert(0, conf_lib)
    try:
        import conf_reader
        slug = conf_reader.conf_get(
            "SLUG", os.path.join(root, "config", "agent.conf"))
    except ImportError:
        slug = ""
    finally:
        if added:
            sys.path.remove(conf_lib)
    return slug or os.path.basename(root.rstrip(os.sep)).lower()


def own_slug_strict(root=None):
    """Consume-only slug resolution (DGN-1379-SPEC-slug-consume-layer S3).

    Unlike own_slug() above (used by submit's `from` auto-derivation,
    which MAY fall back to the workspace basename -- guessing a sender
    identity is harmless), consume must never guess which mailbox to
    drain: "슬러그 없이 어느 메일함을 비울지 추측 금지". Returns the
    config/agent.conf SLUG= value, or None if absent/empty -- the
    caller's cue to fail closed.
    """
    root = root or own_root()
    conf_lib = os.path.join(root, "routines", "lib")
    added = conf_lib not in sys.path
    if added:
        sys.path.insert(0, conf_lib)
    try:
        import conf_reader
        slug = conf_reader.conf_get(
            "SLUG", os.path.join(root, "config", "agent.conf"))
    except ImportError:
        slug = ""
    finally:
        if added:
            sys.path.remove(conf_lib)
    return slug or None


def _leave_handler(meta, body, path):
    return handoff.VERDICT_LEAVE


def _bind_builtin(fn, root):
    def _handler(meta, body, path, _fn=fn, _root=root):
        return _fn(meta, body, path, _root)
    return _handler


def handlers_from_capabilities(root, log=None):
    """DGN-1379-SPEC-slug-consume-layer S2: <root>/config/
    handoff.capabilities -> a handler table for handoff.consume_mailbox().

    mode -> handler:
      builtin:<name>  builtins.resolve_builtin(name), root-bound into a
                      3-arg closure (builtins take (meta, body, path,
                      root); consume_mailbox's handler contract, inherited
                      from consume(), is (meta, body, path)).
      leave           always VERDICT_LEAVE.
      notify          leave here; the file is then surfaced by consume's
                      notify pass (DGN-1774, notify.notify_pending) like
                      every other message still in the inbox.
      exec            NOT implemented -- out of this spec's scope (the
                      exec verdict protocol needs its own timeout/idem/
                      emits/hop-gate design, per S2). Resolves to leave,
                      does not increment errors, logs one line.

    File absent -> {} (empty table, logged, never raises: an un-authored
    capabilities file must not block consumption itself).
    File present but malformed -> capability.CapabilityFileInvalid
    propagates unchanged (fail-closed, per S2 / DGN-1495(3): "부재 = 통과
    / 파손 = 중단").
    """
    log = log or (lambda s: None)
    path = os.path.join(root, "config", "handoff.capabilities")
    if not os.path.isfile(path):
        log("handoff.capabilities absent at %s -- consuming with an empty "
            "handler table (every undeclared type leaves the file in the "
            "inbox and is surfaced by the notify pass)" % path)
        return {}
    entries = capability.load(path)   # CapabilityFileInvalid propagates
    handlers = {}
    for entry in entries:
        if entry.mode == "builtin":
            fn = handoff_builtins.resolve_builtin(entry.builtin_name)
            if fn is None:
                log("line %d: unknown builtin %r for selector %r -- "
                    "leaving" % (entry.line_no, entry.builtin_name,
                                 entry.selector))
                handlers[entry.selector] = _leave_handler
                continue
            handlers[entry.selector] = _bind_builtin(fn, root)
        elif entry.mode == "leave":
            handlers[entry.selector] = _leave_handler
        elif entry.mode == "notify":
            handlers[entry.selector] = _leave_handler
        elif entry.mode == "exec":
            log("line %d: exec mode for selector %r is out of scope "
                "(DGN-1379-SPEC-slug-consume-layer S2) -- leaving"
                % (entry.line_no, entry.selector))
            handlers[entry.selector] = _leave_handler
    return handlers


class InvalidSlugError(ValueError):
    """Raised by validate_slug -- see R1 hardness case 2."""


def validate_slug(slug):
    """R1 hardness case 2 (design v3.3 Delta5'): reject path separators,
    absolute paths, and '..' in a --to-slug argument. handoff.mailbox_root
    joins the slug straight into a path with os.path.join and trusts it --
    this is the one new input surface D9 adds, so it is the one place that
    must guarantee that trust. Returns the slug unchanged when valid."""
    if not slug or slug in (".", ".."):
        raise InvalidSlugError("invalid --to-slug: %r" % slug)
    if os.path.isabs(slug):
        raise InvalidSlugError("invalid --to-slug (absolute path): %r" % slug)
    if os.sep in slug or (os.altsep and os.altsep in slug):
        raise InvalidSlugError(
            "invalid --to-slug (path separator): %r" % slug)
    if ".." in slug.split(os.sep):
        raise InvalidSlugError("invalid --to-slug (contains ..): %r" % slug)
    return slug


def submit(to_slug, msg_type, payload=None, body="", from_slug=None):
    """Programmatic submit -- what `engine.py submit` wraps as a CLI.

    Raises InvalidSlugError for a malformed to_slug, and
    handoff.MailboxMissingError when the recipient slug mailbox is not
    provisioned (D9 (ii): closed failure, never a silent ensure/create --
    the sender never provisions, per handoff.py's FATAL-1 contract).
    Returns the final inbox path on success.
    """
    to_slug = validate_slug(to_slug)
    meta = {"from": from_slug or own_slug(), "type": msg_type}
    if payload is not None:
        meta["payload"] = payload
    return handoff.submit_to_slug(to_slug, meta, body=body)


def _cmd_submit(args):
    payload = json.loads(args.payload_json) if args.payload_json else None
    body = ""
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as fh:
            body = fh.read()
    try:
        path = submit(args.to_slug, args.type, payload=payload, body=body)
    except InvalidSlugError as exc:
        print("engine submit: REJECTED -- %s" % exc, file=sys.stderr)
        return 2
    except handoff.MailboxMissingError as exc:
        # D9 (ii): closed failure. The caller (e.g. onboarding-check.py's
        # migration leg) must branch to its own human-fallback message on
        # a non-zero exit here -- never print a success message.
        print("engine submit: CLOSED -- %s" % exc, file=sys.stderr)
        return 1
    print(path)
    return 0


def _cmd_consume(args):
    # DGN-1379-SPEC-slug-consume-layer S3: consume now drains this
    # instance's own SLUG mailbox via handoff.consume_mailbox(), dispatched
    # through config/handoff.capabilities (handlers_from_capabilities).
    # The legacy handoff.consume(root, {}) call is REMOVED per S3 --
    # legacy-root leftovers are DGN-1454 M-stage's job, not this
    # entrypoint's.
    root = own_root()
    cap = args.cap if args.cap is not None else handoff.DEFAULT_RUN_CAP

    def _log(msg):
        print(msg, file=sys.stderr)

    slug = own_slug_strict(root)
    if not slug:
        # S3: no guessing which mailbox to drain -- fail closed before
        # touching anything.
        print("engine consume: CLOSED -- SLUG= not set in "
              "config/agent.conf (refusing to guess which mailbox to "
              "drain)", file=sys.stderr)
        return 1
    try:
        handlers = handlers_from_capabilities(root, log=_log)
    except capability.CapabilityFileInvalid as exc:
        # S2 fail-closed: a present-but-malformed capabilities file must
        # stop consumption, not silently fall back to leave-everything.
        print("engine consume: CLOSED -- capabilities file invalid: %s"
              % exc, file=sys.stderr)
        # DGN-1774: consumption stops, but the session still hears about
        # what is waiting -- a broken config must not mute the inbox.
        _notify_pass(root, slug, _log)
        return 1
    stats = handoff.consume_mailbox(slug, handlers, cap=cap)
    print(json.dumps(stats))
    # DGN-1774: whatever the handler pass left in the inbox (no handler,
    # leave/notify mode, handler error, cap reached, lock busy) is
    # un-triaged -- inject it into the session exactly once. Before this,
    # "left" meant nobody was told (measured 2026-09-27..28). The stdout
    # line above keeps consume()'s stats shape; the notify summary goes
    # to stderr so that contract is unchanged. A notify pass that could
    # not inject is a processing error (rc 1), never a quiet success.
    nstats = _notify_pass(root, slug, _log)
    if nstats is None or nstats["failed"]:
        return 1
    # spec exit code: 0 normal, 1 processing error, 0 lock-busy (legacy
    # behavior carried over -- a concurrent invocation is not an error).
    if stats["lock_busy"]:
        return 0
    return 1 if stats["errors"] else 0


def _notify_pass(root, slug, log):
    """Run notify.notify_pending; never let it crash consume. Returns the
    stats dict, or None when the pass itself failed (logged)."""
    try:
        nstats = notify.notify_pending(root, slug, log=log)
    except Exception as exc:              # noqa: BLE001 -- surfaced below
        log("engine consume: notify pass FAILED: %s" % exc)
        return None
    log("engine consume: notify %s" % json.dumps(nstats))
    return nstats


def build_parser():
    parser = argparse.ArgumentParser(
        prog="engine.py",
        description="DGN-1379 handoff engine (R1: submit entry point only)")
    sub = parser.add_subparsers(dest="command", required=True)

    submit_p = sub.add_parser(
        "submit",
        help="submit a message to a slug mailbox (--to-slug only; "
             "--to-root is permanently unimplemented, D9)")
    submit_p.add_argument("--to-slug", required=True)
    submit_p.add_argument("--type", required=True, dest="type")
    submit_p.add_argument("--payload-json", default=None)
    submit_p.add_argument("--body-file", default=None)
    submit_p.set_defaults(func=_cmd_submit)

    consume_p = sub.add_parser(
        "consume",
        help="drain this instance's own SLUG mailbox via "
             "handoff.consume_mailbox(), dispatched through "
             "config/handoff.capabilities (no --to-root, no peer arg -- "
             "slug is self-derived, fail-closed if SLUG= is unset)")
    consume_p.add_argument("--cap", type=int, default=None)
    # --once/--json are NOT accepted. The spec's usage line listed them,
    # but handoff.consume() has no run-once knob (its rescan-until-idle
    # loop is internal) and the output is unconditionally the one-line
    # JSON summary. Accepting a flag that changes nothing lies to the
    # caller: it would exit 0 and let them believe it took effect. A
    # rejected unknown flag is the honest answer. Spec amended to match.
    consume_p.set_defaults(func=_cmd_consume)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
