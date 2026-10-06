#!/usr/bin/env python3
"""machine_signal -- the ONE way a script prints a machine signal line (DGN-1209).

Emitting through this helper IS the registration: the line goes out with the
in-band sigil `signal::` and the bridge's outbound gate
(bridge/machine_gate.py) DROPS it on every owner-facing rail, or STRIPs the
prefix when the payload is an owner sentence. No token list travels between
pack and kit, and `2>&1` cannot splice the signal back into the body because
the decision rides in the text, not the fd.

    signal::RAMP_DECISION exercise=squat decision=ramp rungs=2   -> dropped
    signal::LOAD_DECISION: 8회 성공이라 2.5kg 올렸습니다              -> "8회 성공이라 ..."

A raw print("RAMP_DECISION ...") that bypasses this helper still PASSES the
gate (the gate never censors by shape) but raises a loud, dedup'd alert to
the owner -- that alert is how a missing registration gets found.

Standalone, stdlib only, importable from any pack script:

    sys.path.insert(0, "<agent-root>/routines/lib"); import machine_signal as ms
    print(ms.signal("RAMP_DECISION", exercise="squat", decision="ramp", rungs=2))
    print(ms.say("LOAD_DECISION", "8회 성공이라 2.5kg 올렸습니다"))

CLI (for shell emitters):

    machine_signal.py RAMP_DECISION exercise=squat decision=ramp
    machine_signal.py --say LOAD_DECISION "8회 성공이라 2.5kg 올렸습니다"

SIGIL MUST equal bridge.machine_gate.MACHINE_SIGNAL_MARKER (pinned by
bridge/tests/test_dgn1209_machine_line_gate.py).
"""

from __future__ import annotations

import re
import sys
from typing import Any

SIGIL = "signal::"
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _check_token(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise ValueError(
            "machine signal token must match ^[A-Z][A-Z0-9_]*$ (got %r)" % (token,)
        )
    return token


def _one_line(value: Any) -> str:
    # A signal is exactly one line; embedded newlines would let the tail
    # escape the gate as ordinary prose.
    return str(value).replace("\r", " ").replace("\n", " ")


def signal(token: str, *parts: Any, **fields: Any) -> str:
    """kv form: `signal::TOKEN part ... key=value ...` -- machine-only, DROPPED
    on owner surfaces."""
    _check_token(token)
    payload = [_one_line(p) for p in parts]
    payload += ["%s=%s" % (k, _one_line(v)) for k, v in fields.items()]
    return SIGIL + " ".join([token] + payload) if payload else SIGIL + token


def say(token: str, text: str) -> str:
    """colon form: `signal::TOKEN: <owner sentence>` -- the prefix is machine
    and is STRIPPED on owner surfaces; the sentence reaches the owner."""
    _check_token(token)
    sentence = _one_line(text).strip()
    if not sentence:
        # Nothing to say -> plain kv-form signal (dropped), never a blank line.
        return SIGIL + token
    return "%s%s: %s" % (SIGIL, token, sentence)


def emit(line: str, file=None) -> None:
    print(line, file=file or sys.stdout, flush=True)


def _main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("usage: machine_signal.py TOKEN [part|key=value ...]")
        print("       machine_signal.py --say TOKEN <sentence...>")
        return 2 if not argv else 0
    try:
        if argv[0] == "--say":
            if len(argv) < 2:
                raise ValueError("--say needs TOKEN")
            emit(say(argv[1], " ".join(argv[2:])))
        else:
            token, rest = argv[0], argv[1:]
            fields = {}
            parts = []
            for item in rest:
                k, sep, v = item.partition("=")
                if sep and k and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                    fields[k] = v
                else:
                    parts.append(item)
            emit(signal(token, *parts, **fields))
    except ValueError as e:
        print("machine_signal: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
