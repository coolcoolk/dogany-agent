# test-lane: fast
# test-tier: 0
# test-component: memory
# test-prereq: tmp-files, pytest
# test-new-component: Restore the instance-only workitem_gate boundary; no canonical suite owns these cases.
# test-covers: agents/.template/memory-engine/memory.py
# DGN-446 P1(b): work-item semantic gate regression tests.
# Guards the _looks_like_work_item detector and its integration in cmd_write.
# Fully sandboxed: no real LLM calls, no real file writes (dry-run / monkeypatch).
#
# Run: python3 -m pytest memory-engine/tests/test_dgn446_workitem_gate.py -v
# Run all DGN-446 tests together:
#   python3 -m pytest memory-engine/tests/test_dgn446_compressor_guard.py \
#                     memory-engine/tests/test_dgn446_workitem_gate.py -v

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
# Helpers (same pattern as test_dgn446_compressor_guard.py)
# ---------------------------------------------------------------------------

def make_args(**kwargs):
    defaults = dict(
        text=None,
        source="test",
        file="inbox.md",
        section=None,
        dry_run=True,
        force=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def run_write(monkeypatch, raw_text, compressor_items=None, force=False):
    """Run cmd_write with compressor stubbed. Returns (rc, stdout, stderr)."""
    if compressor_items is None:
        compressor_items = ["some extracted fact"]
    monkeypatch.setattr(memory, "compress_with_haiku", lambda text, **kw: list(compressor_items))
    args = make_args(text=raw_text, force=force)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = memory.cmd_write(args)
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# Unit tests for _looks_like_work_item directly
# ---------------------------------------------------------------------------

class TestLooksLikeWorkItemUnit:
    """Direct unit tests on the detector function."""

    # --- positives (should detect as work-item) ---

    def test_backlog_plus_future_framing(self):
        text = "\ubc31\ub85c\uadf8\uc5d0 \ucd94\uac00\ud574\uc57c \ud568 -- \ub2e4\uc74c sprint\uc5d0 authentication \ubaa8\ub4c8\uc744 \uad6c\ud604\ud560 \uac83"
        assert memory._looks_like_work_item(text), "backlog vocab + future framing must trigger"

    def test_todo_plus_future_framing(self):
        text = "TODO: rate limiting needs to be done before we ship"
        assert memory._looks_like_work_item(text), "TODO + needs-to-be-done must trigger"

    def test_parked_plus_future_framing(self):
        text = "This feature is parked for now and should be implemented after the v2 release"
        assert memory._looks_like_work_item(text), "parked + should-be-implemented must trigger"

    def test_legacy_localized_case_84(self):
        text = "\uc77c\ub2e8 \ubcf4\ub958. \ub098\uc911\uc5d0 \ud558\uae30\ub85c \uacb0\uc815\ud568."
        assert memory._looks_like_work_item(text), "\ubcf4\ub958 + \ub098\uc911\uc5d0 \ud558 must trigger"

    def test_legacy_localized_case_88(self):
        text = "\uc774\uc5f0\ub41c \ud0dc\uc2a4\ud06c: \uc778\uc99d \ubaa8\ub4c8 \ub9ac\ud329\ud1a0\ub9c1 \ud574\uc57c \ud55c\ub2e4"
        assert memory._looks_like_work_item(text), "\uc774\uc5f0 + \ud574\uc57c \ud55c\ub2e4 must trigger"

    def test_ticket_vocab_plus_status_prefix(self):
        text = "\ud2f0\ucf13\uc73c\ub85c \uc62c\ub824\uc918\nstatus: open"
        assert memory._looks_like_work_item(text), "\ud2f0\ucf13\uc73c\ub85c + status: prefix must trigger"

    def test_legacy_localized_case_96(self):
        text = "backlog: \ub098\uc911\uc5d0 \ud558\uae30\ub85c \ud55c \uae30\ub2a5 \ubaa9\ub85d"
        assert memory._looks_like_work_item(text), "backlog + \ub098\uc911\uc5d0 \ud558\uae30 must trigger"

    def test_p1_line_prefix_plus_todo(self):
        text = "TODO: fix the crash\nP1: this is urgent"
        assert memory._looks_like_work_item(text), "TODO + P1-line-prefix must trigger"

    def test_two_vocab_signals(self):
        # Two vocabulary signals from group A1 (no B needed)
        text = "backlog entry: parked until Q3"
        assert memory._looks_like_work_item(text), "two A1 vocab signals must trigger"

    # --- negatives (must NOT trigger) ---

    def test_ordinary_fact_no_trigger(self):
        # Simple preference fact -- no work-item signals
        text = "\ud615\ub2d8\uc740 \uc544\uce68\uc5d0 \uc544\uba54\ub9ac\uce74\ub178\ub97c \ub9c8\uc2e0\ub2e4"
        assert not memory._looks_like_work_item(text), "ordinary fact must not trigger"

    def test_p2_coffee_example(self):
        # Spec-required: P2 embedded mid-sentence is not a line prefix signal
        text = "\ud615\ub2d8\uc740 P2 \ucee4\ud53c\ub97c \uc88b\uc544\ud55c\ub2e4"
        assert not memory._looks_like_work_item(text), "mid-sentence P2 must not trigger"

    def test_single_todo_no_trigger(self):
        # Single weak token -- one signal only, not two
        text = "TODO: \ucee4\ud53c \uc0ac\uae30"
        # 'TODO' = 1 signal (A1). No B, no second A. Should NOT trigger.
        assert not memory._looks_like_work_item(text), "single TODO without second signal must not trigger"

    def test_single_backlog_mention_no_trigger(self):
        text = "\uc6b0\ub9ac \ud300 backlog\uac00 \uae38\ub2e4"
        assert not memory._looks_like_work_item(text), "single backlog mention without second signal must not trigger"

    def test_plain_future_framing_no_trigger(self):
        # Future framing alone (B signal) without any A signal -- should not trigger
        text = "\ub0b4\uc77c \ucee4\ud53c\ub97c \ub9c8\uc154\uc57c \ud568"
        assert not memory._looks_like_work_item(text), "future framing alone (no vocab signal) must not trigger"

    def test_job_info_with_priority_word(self):
        # The word 'priority' in a durable-fact sentence (not a line prefix)
        text = "\ud615\ub2d8\uc758 top priority\ub294 \uac74\uac15\uc774\uace0 \ub450 \ubc88\uc9f8\ub294 \ucee4\ub9ac\uc5b4\ub2e4"
        assert not memory._looks_like_work_item(text), "priority in natural prose (non-prefix) must not trigger"

    def test_empty_text(self):
        assert not memory._looks_like_work_item(""), "empty text must not trigger"

    def test_very_short_text(self):
        assert not memory._looks_like_work_item("TODO"), "very short text (< 10 chars) must not trigger"


# ---------------------------------------------------------------------------
# Integration tests via cmd_write
# ---------------------------------------------------------------------------

class TestWorkItemGateIntegration:
    """Integration tests: gate fires before the compressor is invoked."""

    # --- positives: work-item prose must be refused (rc=2) ---

    def test_backlog_sentence_refused(self, monkeypatch):
        """Backlog sentence with future framing must be refused with rc=2."""
        text = (
            "\ubc31\ub85c\uadf8\uc5d0 \ub123\uc5b4\uc918 -- \ub2e4\uc74c \uc2a4\ud504\ub9b0\ud2b8\uc5d0 rate-limiting \uae30\ub2a5\uc744 \uad6c\ud604\ud574\uc57c \ud568. "
            "\uc544\uc9c1 \uc2dc\uc791 \ubabb \ud588\uace0 \ubcf4\ub958 \uc0c1\ud0dc\uc57c."
        )
        rc, out, err = run_write(monkeypatch, text)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "work-item" in err
        assert "worklog/" in err
        assert "_TEMPLATE.md" in err

    def test_todo_list_prose_refused(self, monkeypatch):
        """TODO-flavored prose with needs-to-be-done must be refused."""
        text = (
            "TODO items from today's retro: "
            "the auth module needs to be done before launch, "
            "and the logging pipeline should be implemented next week."
        )
        rc, out, err = run_write(monkeypatch, text)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "work-item" in err

    def test_parked_feature_sentence_refused(self, monkeypatch):
        """Parked feature description with future-work framing must be refused."""
        text = (
            "The dark-mode feature is currently parked. "
            "It should be implemented after we finish the v2 migration."
        )
        rc, out, err = run_write(monkeypatch, text)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "work-item" in err

    def test_legacy_localized_case_190(self, monkeypatch):
        """Korean \uc774\uc5f0 + \ud574\uc57c \ud568 must be refused."""
        text = "\uc778\uc99d \ubaa8\ub4c8 \ub9ac\ud329\ud1a0\ub9c1\uc740 \uc774\uc5f0\ub410\uc5b4. \ub2e4\uc74c sprint\uc5d0 \ud574\uc57c \ud568."
        rc, out, err = run_write(monkeypatch, text)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "work-item" in err

    def test_ticket_steer_sentence_refused(self, monkeypatch):
        """\ud2f0\ucf13\uc73c\ub85c + status keyword must be refused."""
        text = "\ud2f0\ucf13\uc73c\ub85c \uc62c\ub824\uc918. \ud604\uc7ac status: open \uc774\uace0 priority: P1\uc774\uc57c."
        rc, out, err = run_write(monkeypatch, text)
        assert rc == 2, f"expected rc=2, got {rc}; stderr={err!r}"
        assert "work-item" in err

    # --- negatives: durable facts must pass through ---

    def test_ordinary_fact_passes(self, monkeypatch):
        """Durable preference fact must not be refused."""
        text = "\ud615\ub2d8\uc740 \uc544\uce68 5\uc2dc\uc5d0 \uae30\uc0c1\ud558\uace0 \uc544\uba54\ub9ac\uce74\ub178 \ud55c \uc794\uc73c\ub85c \ud558\ub8e8\ub97c \uc2dc\uc791\ud55c\ub2e4."
        rc, out, err = run_write(monkeypatch, text, compressor_items=["\ud615\ub2d8 \uc544\uce68 \uae30\uc0c1 5\uc2dc, \uc544\uba54\ub9ac\uce74\ub178"])
        assert rc == 0, f"ordinary fact refused unexpectedly; rc={rc}; err={err!r}"

    def test_p2_coffee_passes(self, monkeypatch):
        """Mid-sentence P2 token must not trigger the gate."""
        text = "\ud615\ub2d8\uc740 P2 \ucee4\ud53c\ub97c \uc88b\uc544\ud55c\ub2e4"
        rc, out, err = run_write(monkeypatch, text, compressor_items=["\ud615\ub2d8 P2 \ucee4\ud53c \uc120\ud638"])
        assert rc == 0, f"p2-coffee false-positive; rc={rc}; err={err!r}"

    def test_single_backlog_mention_passes(self, monkeypatch):
        """Single 'backlog' mention in factual context must not be refused."""
        text = "\uc6b0\ub9ac \ud300\uc758 backlog \uad00\ub9ac \ubc29\uc2dd\uc740 Notion\uc744 \uc4f4\ub2e4."
        rc, out, err = run_write(monkeypatch, text, compressor_items=["\ud300 backlog \uad00\ub9ac \ub3c4\uad6c: Notion"])
        assert rc == 0, f"single backlog mention refused unexpectedly; rc={rc}; err={err!r}"

    def test_priority_in_prose_passes(self, monkeypatch):
        """'priority' used as a durable-fact word (not line prefix) must not be refused."""
        text = "\ud615\ub2d8\uc758 \ucd5c\uc6b0\uc120 priority\ub294 \uac74\uac15 \uc720\uc9c0\uc774\uace0, \uadf8 \ub2e4\uc74c\uc740 \ucee4\ub9ac\uc5b4 \uc131\uc7a5\uc774\ub2e4."
        rc, out, err = run_write(monkeypatch, text, compressor_items=["\ucd5c\uc6b0\uc120 priority: \uac74\uac15"])
        assert rc == 0, f"priority-prose false-positive; rc={rc}; err={err!r}"

    def test_job_future_plan_no_work_vocab_passes(self, monkeypatch):
        """Future framing alone without work-item vocab must not be refused."""
        text = "\ub0b4\ub144\uc5d0\ub294 \ub370\uc774\ud130 \uc5d4\uc9c0\ub2c8\uc5b4\ub9c1 \uc5ed\ub7c9\uc744 \ud0a4\uc6cc\uc57c \ud560 \uac83 \uac19\uc544."
        rc, out, err = run_write(monkeypatch, text, compressor_items=["\ub370\uc774\ud130 \uc5d4\uc9c0\ub2c8\uc5b4\ub9c1 \uc5ed\ub7c9 \uac15\ud654 \uacc4\ud68d"])
        assert rc == 0, f"future framing alone refused unexpectedly; rc={rc}; err={err!r}"

    # --- --force bypass ---

    def test_force_bypasses_work_item_gate(self, monkeypatch):
        """--force must bypass the work-item gate and proceed (dry-run)."""
        text = (
            "\ubc31\ub85c\uadf8\uc5d0 \ub123\uc5b4\uc918 -- rate-limiting needs to be done before launch."
        )
        rc, out, err = run_write(monkeypatch, text, force=True)
        assert rc == 0, f"expected rc=0 with --force, got {rc}; err={err!r}"

    def test_force_bypasses_work_item_gate_korean(self, monkeypatch):
        """--force bypass works for Korean work-item prose too."""
        text = "\ubcf4\ub958\ub41c \ud0dc\uc2a4\ud06c: \uc778\uc99d \ubaa8\ub4c8 \ud574\uc57c \ud568."
        rc, out, err = run_write(monkeypatch, text, force=True)
        assert rc == 0, f"expected rc=0 with --force, got {rc}; err={err!r}"

    # --- P2 gate unaffected (regression) ---

    def test_p2_gate_still_fires_bullet_list(self, monkeypatch):
        """P2 structural gate (bullet-dense) must still fire when work-item gate would not."""
        # Bullet-dense input: P2 shape gate fires, no work-item vocab so P1b would not alone.
        bullet_block = (
            "- Track commissions user pays on each trade\n"
            "- Portfolio rebalancing quarterly\n"
            "- Max drawdown tolerance 15%\n"
            "- Commission per trade: $7.99\n"
            "- Broker: Interactive Brokers\n"
        )
        rc, out, err = run_write(monkeypatch, bullet_block)
        assert rc == 2, f"P2 structural gate must still fire; rc={rc}; err={err!r}"
        assert "structured" in err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-rfEs"]))
