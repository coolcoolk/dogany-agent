"""DGN-1420: the post-restart spool hands confirmed decisions to the resumed
session, not only in-flight work.

Observed: a define the owner confirmed before a self-update restart lived
only in the cut conversation; the restart spool said "no in-flight task",
the resumed session re-ran the same define and got the opposite verdict.
Covers the REAL drop_verify_spool from self_restart.sh, run against a temp
instance whose routines/ is the host tree: with a ledger the spool carries
step 5 (the latest entries, confirmed verdict included), with no ledger
configured the spool is unchanged, with no host seat it is unchanged.
"""

import os
import subprocess
from pathlib import Path

import pytest

from bridge.tests._hostroot import host_file, requires_host

BRIDGE_DIR = Path(__file__).resolve().parents[1]
SRC = (BRIDGE_DIR / "self_restart.sh").read_text(encoding="utf-8")

LEDGER = (
    "# Decisions\n\n"
    "## dec-41 -- release scope: anchor set (2026-09-12) "
    "[confirmed 2026-09-12 11:35: \"1. merge crew comms into this release\"]\n"
    "<!-- decision-ledger id=dec-41 state=confirmed -->\n"
    "- Options: 1. merge crew comms into this release / 2. defer it\n"
)


def _fn(name):
    body = SRC[SRC.index(name + "() {"):]
    return body[:body.index("\n}\n") + 3]


def _instance(tmp_path, conf, seat=True):
    root = tmp_path / "inst"
    (root / "config").mkdir(parents=True)
    (root / "config" / "agent.conf").write_text(conf, encoding="utf-8")
    (root / "records" / "decisions").mkdir(parents=True)
    (root / "records" / "decisions" / "_DECISIONS.md").write_text(
        LEDGER, encoding="utf-8")
    if seat:
        # abspath (not realpath) keeps the seat's instance root = this root
        os.symlink(host_file("routines"), root / "routines")
    return root


def _spool(root, resume_intent=""):
    spool = root / ".telegram_bot" / "session-inbox"
    script = "\n".join([
        'INSTANCE_ROOT="$1"; SPOOL_DIR="$2"; RESUME_INTENT="$3"',
        'REASON="framework self-update"; OLD_PID=1; NEW_PID=2',
        'LABEL=com.example.agent; MARKER_LOG=/dev/null',
        _fn("drop_verify_spool"),
        "drop_verify_spool",
    ])
    env = {k: v for k, v in os.environ.items()
           if k not in ("DECISION_LEDGER_ROOT", "PROJECT_ROOT")}
    proc = subprocess.run(["bash", "-c", script, "x", str(root), str(spool),
                           resume_intent], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    files = sorted(spool.glob("restart-verify-*.md"))
    assert len(files) == 1, proc.stdout + proc.stderr
    return files[0].read_text(encoding="utf-8")


ON = "DECISION_LEDGER_PATH=records/decisions/_DECISIONS.md\n"


@requires_host("routines/decision-ledger.py")
def test_confirmed_decision_rides_the_restart_spool(tmp_path):
    text = _spool(_instance(tmp_path, ON))
    assert "NO specific in-flight\n   task" in text
    assert "5. Carry the owner's decisions" in text
    assert ("- [confirmed] dec-41 -- release scope: anchor set (2026-09-12)"
            in text)
    assert "merge crew comms into this release" in text
    assert "Never re-run a\n   define" in text
    # step 5 sits with the steps, before the closing instructions
    assert text.index("5. Carry") < text.index("If everything is healthy")


@requires_host("routines/decision-ledger.py")
def test_decisions_ride_alongside_resume_intent(tmp_path):
    text = _spool(_instance(tmp_path, ON), "finishing the scope doc")
    assert "finishing the scope doc" in text
    assert "- [confirmed] dec-41" in text


@requires_host("routines/decision-ledger.py")
def test_no_ledger_configured_keeps_spool_as_before(tmp_path):
    text = _spool(_instance(tmp_path, "AGENT_LANG=en\n"))
    assert "5. Carry" not in text and "dec-41" not in text
    assert "NO specific in-flight" in text


def test_no_host_seat_keeps_spool_as_before(tmp_path):
    text = _spool(_instance(tmp_path, ON, seat=False))
    assert "5. Carry" not in text and "dec-41" not in text
