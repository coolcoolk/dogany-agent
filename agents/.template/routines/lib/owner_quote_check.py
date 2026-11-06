#!/usr/bin/env python3
"""owner_quote_check -- the one CLI door the script-side devices use to ask
"did the owner say yes to THIS action?".

v2.7 D3/D4 (docs/v27-D3-D4-spec.md section 6; grill F7/F17/F18, round 2
R2-6 to R2-8).  The devices that replaced hook rows (bridge/self_restart.sh's
owner-only flags, scripts/release-approval.py, git-hooks/pre-push) all ask
here, and the answer uses the SAME binding as the D3/D4 hook's path A / B
(lib/user_yes.device_yes), never "some sentence the owner once said":

  - the session is the live caller's, from the D3 hook's record
    (device_gate.live_caller, R2-7) -- never env, never "the newest ledger
    row on the machine";
  - the quote must be inside Y, the newest owner turn of that session,
    ledgered (with this transcript), not voice, under an hour old;
  - a tap is accepted only with the bridge's ledgered device_question:
    the newest menu's explicitly approving button on the exact, unedited
    question bubble (message id, keyboard, slot and body hash all match);
  - Y is a bound yes answering a reply whose LAST line is the device's own
    question line (--question, e.g. "Release `v2.7.0` @ `abc123def456`?"),
    or Y names the device's object (--object: the tag, the URL host/path;
    --intent: a path-B intent such as service/restart).

Round 1's form (`--quote` alone: any whole clause of any ledgered owner turn
of the session) is gone: "thanks." approved a release (R2-6).

Usage:
  owner_quote_check.py --device D --quote "<owner words>"
                       [--question LINE ...] [--object S ...] [--intent I]
  owner_quote_check.py --audit --device D --session S --message-uuid U
                       --transcript T --quote Q [--question ...] [--object ...]

Exit: 0 resolved (stdout: one JSON line), 1 refused (stdout:
`refused <reason>`), 2 usage error or resolver unavailable.  Fail-closed.
ACCIDENT-grade (device_gate docstring): it stops a slip, not an adversary
with Bash.

Pure stdlib.  English/ASCII only.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _device_gate():
    spec = importlib.util.spec_from_file_location(
        "device_gate_for_oqc", os.path.join(HERE, "device_gate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--device", choices=("self_restart",))
    ap.add_argument("--quote", required=True)
    ap.add_argument("--question", action="append", default=[])
    ap.add_argument("--object", action="append", default=[])
    ap.add_argument("--intent")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--session")
    ap.add_argument("--message-uuid")
    ap.add_argument("--transcript")
    a = ap.parse_args(argv)
    if not a.device:
        print("refused device-required")
        return 1
    try:
        dg = _device_gate()
    except Exception as exc:  # noqa: BLE001 -- fail closed, say why
        print("unavailable device-gate-import:%s" % type(exc).__name__)
        return 2
    try:
        if a.audit:
            ok, got = dg.audit(a.session, a.message_uuid, a.transcript,
                               a.quote, a.question, a.object, a.intent)
        else:
            ok, got = dg.resolve(a.device, a.quote, a.question, a.object,
                                 a.intent)
    except Exception as exc:  # noqa: BLE001 -- a broken ledger never says yes
        print("refused resolver-error:%s" % type(exc).__name__)
        return 1
    if not ok:
        print("refused %s" % got)
        return 1
    print(json.dumps(dict(got, runtime="claude-code"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
