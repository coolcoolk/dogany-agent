# test-lane: fast
# test-tier: 0
# test-component: memory
# test-prereq: tmp-files, pytest
# test-new-component: Restore the instance-only compressor_guard boundary; no canonical suite owns these cases.
# test-covers: agents/.template/memory-engine/memory.py
# DGN-446: compressor hardening regression tests.
# Guards (a) scaffolding strip, (b) structured-input gate, (c) fragment cap.
# Fully sandboxed: no real LLM calls, no real file writes (dry-run / monkeypatch).
#
# Run: python3 -m pytest memory-engine/tests/test_dgn446_compressor_guard.py -v

import argparse
import contextlib
import io
import os
import sys

import pytest

ENGINE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
sys.path.insert(0, ENGINE_DIR)

for key in list(os.environ):
    if key.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_")):
        os.environ.pop(key)

import memory  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_args(**kwargs):
    """Build a Namespace that looks like the argparse result for cmd_write."""
    defaults = dict(
        text=None,
        source="test",
        file="inbox.md",
        section=None,
        dry_run=True,   # dry_run=True everywhere: no real file writes
        force=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def run_write(monkeypatch, raw_text, compressor_items, force=False):
    """
    Run cmd_write with:
    - compress_with_haiku stubbed to return compressor_items
    - dry_run=True (no file writes)
    Returns (rc, stdout_str, stderr_str).
    """
    monkeypatch.setattr(memory, "compress_with_haiku", lambda text, **kw: list(compressor_items))

    args = make_args(text=raw_text, force=force)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = memory.cmd_write(args)
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# (b) structured-input gate
# ---------------------------------------------------------------------------

STRUCTURED_BULLET = """\
- Track commissions user PAYS on each trade
- Portfolio rebalancing quarterly
- Max drawdown tolerance 15%
- Commission per trade: $7.99
- Broker: Interactive Brokers
"""

STRUCTURED_HEADERS = """\
## Investment System Requirements

### Commission Tracking
Track commissions the user pays.

### Rebalancing
Quarterly rebalancing cycle.
"""

STRUCTURED_NUMBERED = """\
1. User wants to track trade commissions paid
2. Portfolio rebalancing on a quarterly cadence
3. Max drawdown tolerance is 15 percent
4. Broker is Interactive Brokers
5. Commission per trade is approximately eight dollars
"""

CONVERSATIONAL = (
    "I want to make sure we track the commissions I pay on each trade going forward. "
    "I use Interactive Brokers and each trade costs about eight dollars. "
    "Also please remember I like to rebalance my portfolio once a quarter."
)


def test_bullet_dense_input_refused(monkeypatch):
    """Bullet-heavy input (>35% bullet lines) must be refused with rc=2."""
    rc, out, err = run_write(monkeypatch, STRUCTURED_BULLET, ["some fact"])
    assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
    assert "structured" in err or "belongs in a ticket" in err


def test_header_dense_input_refused(monkeypatch):
    """Input with 2+ markdown headers must be refused with rc=2."""
    rc, out, err = run_write(monkeypatch, STRUCTURED_HEADERS, ["some fact"])
    assert rc == 2, f"expected rc=2, got {rc}"
    assert "structured" in err or "belongs in a ticket" in err


def test_numbered_list_input_refused(monkeypatch):
    """Numbered-list-heavy input must be refused with rc=2."""
    rc, out, err = run_write(monkeypatch, STRUCTURED_NUMBERED, ["some fact"])
    assert rc == 2, f"expected rc=2, got {rc}"
    assert "structured" in err or "belongs in a ticket" in err


def test_force_bypasses_shape_gate(monkeypatch):
    """--force must bypass the shape gate and proceed (dry-run succeeds)."""
    rc, out, err = run_write(monkeypatch, STRUCTURED_BULLET, ["track commissions"], force=True)
    # Should NOT refuse (rc 0 because dry_run, not rc 2)
    assert rc == 0, f"expected rc=0 with --force, got {rc}; err={err!r}"


def test_conversational_input_passes(monkeypatch):
    """Conversational prose must pass the gate and get stored (dry-run)."""
    rc, out, err = run_write(monkeypatch, CONVERSATIONAL, ["track commissions"])
    assert rc == 0, f"conversational input refused unexpectedly; rc={rc}; err={err!r}"
    assert "track commissions" in out


# ---------------------------------------------------------------------------
# (a) scaffolding strip
# ---------------------------------------------------------------------------

SCAFFOLDING_OUTPUT_LINES = [
    "I'll extract the persistent facts from this text.",
    "User's investment-related system requirements and preferences:",
    "Output:",
    "Track commissions on each trade",
    "Here are the facts I found:",
    "Interactive Brokers",
    "Let me summarize the key persistent facts:",
]

CLEAN_OUTPUT_LINES = [
    "Track commissions on each trade",
    "Interactive Brokers",
]


def make_fake_compressor_raw(raw_lines):
    """Fake compressor that returns a pre-set list (bypasses LLM but goes through
    the scaffold-strip logic that lives INSIDE compress_with_haiku).
    We test the strip logic directly via _SCAFFOLDING_RES here."""

    def fake(text, prompt_prefix=None, model=None):
        # Simulate the strip logic that is now inside compress_with_haiku
        result = []
        for ln in raw_lines:
            s = ln.strip()
            if not s:
                continue
            import re
            s = re.sub(r"^[-*\u2022]\s+", "", s)
            s = re.sub(r"^\d+[.)]\s+", "", s)
            s = s.strip()
            if not s:
                continue
            if s.upper() == "NONE":
                return []
            if any(pat.search(s) for pat in memory._SCAFFOLDING_RES):
                continue
            result.append(s)
        return result

    return fake


def test_scaffolding_lines_stripped(monkeypatch):
    """Scaffolding lines in extractor output must be stripped; real facts survive."""
    monkeypatch.setattr(
        memory, "compress_with_haiku",
        make_fake_compressor_raw(SCAFFOLDING_OUTPUT_LINES)
    )
    args = make_args(text=CONVERSATIONAL, force=False)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = memory.cmd_write(args)
    out = out_buf.getvalue()

    assert rc == 0, f"unexpected failure rc={rc}; err={err_buf.getvalue()!r}"
    # Real facts must be present
    assert "Track commissions" in out or "track commissions" in out
    assert "Interactive Brokers" in out or "interactive brokers" in out.lower()
    # Scaffolding must NOT be present
    assert "I'll extract" not in out
    assert "Output:" not in out
    assert "Let me" not in out
    assert "Here are" not in out
    assert "User's investment" not in out


def test_clean_extractor_output_unchanged(monkeypatch):
    """Clean extractor output (no scaffolding) must pass through unmodified."""
    monkeypatch.setattr(
        memory, "compress_with_haiku",
        make_fake_compressor_raw(CLEAN_OUTPUT_LINES)
    )
    args = make_args(text=CONVERSATIONAL, force=False)
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf):
        rc = memory.cmd_write(args)
    out = out_buf.getvalue()

    assert rc == 0
    assert "Track commissions" in out
    assert "Interactive Brokers" in out


# ---------------------------------------------------------------------------
# (c) fragment cap
# ---------------------------------------------------------------------------

MANY_ITEMS = ["track commissions"] * 15   # 15 items > cap of 8


def test_fragment_cap_refused(monkeypatch):
    """Extractor returning more than WRITE_FRAGMENT_CAP items must be refused (rc=2)."""
    rc, out, err = run_write(monkeypatch, CONVERSATIONAL, MANY_ITEMS)
    assert rc == 2, f"expected rc=2 for fragment explosion, got {rc}; err={err!r}"
    assert "fragment" in err or "cap" in err


def test_fragment_cap_force_bypass(monkeypatch):
    """--force must bypass the fragment cap and proceed (dry-run)."""
    rc, out, err = run_write(monkeypatch, CONVERSATIONAL, MANY_ITEMS, force=True)
    assert rc == 0, f"expected rc=0 with --force on fragment cap, got {rc}; err={err!r}"


def test_at_cap_boundary_passes(monkeypatch):
    """Exactly WRITE_FRAGMENT_CAP items must not trigger the cap refusal."""
    items_at_cap = ["track commissions"] * memory.WRITE_FRAGMENT_CAP
    rc, out, err = run_write(monkeypatch, CONVERSATIONAL, items_at_cap)
    assert rc == 0, f"expected rc=0 at cap boundary ({memory.WRITE_FRAGMENT_CAP} items), got {rc}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
