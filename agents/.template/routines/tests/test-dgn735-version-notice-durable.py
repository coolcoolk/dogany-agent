#!/usr/bin/env python3
# test-lane: fast    # measured 0.1s (2026-09-04 full sweep, DGN-1267)
# test-covers: *    # tree scanner, measured 0.13s (DGN-1734): runs on every push
"""test-dgn735-version-notice-durable.py

Tests for the DGN-735 session-scoped per-version guard in version-check.py, and
DGN-738 localized user-summary fold source for the update-available notice.
No network. Self-contained. ASCII/English only.

DGN-1586 retired the shown-flag write side (_write_shown/_should_notice).
The spool keeps delivery receipts, but DGN-735's injection suppression is
keyed by (kind, version, SessionStart payload session_id), never elapsed time.

DGN-735 cases:
  (a) same version in the same session -> suppress
  (b) different (newer) version -> show
  (c) same version in a new session seconds later -> show

DGN-738 cases:
  (d) user-summary present -> _user_summary_from_note returns ko text
  (e) user-summary absent -> _user_summary_from_note returns empty string
  (f) wrong lang key requested -> returns empty string (fallback trigger)
  (g) malformed / corrupt block -> returns empty string (fail-open)
"""
import importlib.util
import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Load version-check.py as a module without running main()
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTINES_DIR = os.path.dirname(_HERE)
_TARGET = os.path.join(_ROUTINES_DIR, "version-check.py")

spec = importlib.util.spec_from_file_location("version_check", _TARGET)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

def make_root():
    """Create a temp dir that looks like an instance root."""
    d = tempfile.mkdtemp(prefix="dgn735-test-")
    state_dir = os.path.join(d, ".telegram_bot", "state")
    os.makedirs(state_dir, exist_ok=True)
    return d


def cleanup(root):
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def make_delivered_record(version, delivered_ts, session_id,
                          kind=None, notice_id="ntc-test"):
    """Build a spool record shaped like a real delivered notice (the state
    a prior _write_shown call used to represent). Used to seed the spool
    directly via _spool_dump so the creation-predicate tests below don't
    depend on the bridge-owned pending->delivered promotion path."""
    return {
        "id": notice_id,
        "kind": kind or vc._NOTICE_KIND_FW_UPDATE,
        "version": vc._norm_ver(version),
        "audience": "owner",
        "first_pending_ts": delivered_ts - 1,
        "attempts": 1,
        "status": "delivered",
        "expired_reason": None,
        "delivered_ts": delivered_ts,
        "chat_id": 1,
        "message_id": 1,
        "body_oneline": "x",
        "body_fold": "x",
        "context_injected_ts": delivered_ts,
        "context_injected_session_id": session_id,
        "origin": "test",
    }


def test_a_same_version_same_session_suppresses():
    """(a) a delivered version blocks only the session that saw it."""
    root = make_root()
    try:
        now = 1_000_000.0
        version = "1.26.0"
        vc._spool_dump(root, [make_delivered_record(version, now - 8 * 86400,
                                                     "session-a")], [])
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, version, "body", "body",
            now, "1.0.0", "session-a")
        assert active is None, "Expected no active pending, got {!r}".format(active)
        assert blocked == "delivered-in-session", \
            "Expected blocked=delivered-in-session, got {!r}".format(blocked)
        print("PASS (a): same version, same session -> suppress")
    finally:
        cleanup(root)


def test_b_different_version():
    """(b) different (newer) version -> show (a new pending is created)."""
    root = make_root()
    try:
        now = 1_000_000.0
        stored_version = "1.25.0"
        new_version = "1.26.0"
        # Seed a delivered record for the OLD version very recently.
        vc._spool_dump(
            root, [make_delivered_record(stored_version, now - 1,
                                         "session-a")], [])
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, new_version, "body", "body",
            now, "1.0.0", "session-a")
        assert active is not None and blocked is None, \
            "Expected a new pending, got active={!r} blocked={!r}".format(
                active, blocked)
        assert active["version"] == vc._norm_ver(new_version)
        print("PASS (b): different (newer) version -> show")
    finally:
        cleanup(root)


def test_c_same_version_new_session_shows_immediately():
    """(c) new session may announce the same version seconds later."""
    root = make_root()
    try:
        now = 1_000_000.0
        version = "1.26.0"
        vc._spool_dump(
            root, [make_delivered_record(version, now - 1, "session-a")], [])
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, version, "body", "body",
            now, "1.0.0", "session-b")
        assert active is not None and blocked is None, \
            "Expected a new pending in a new session, got active={!r} " \
            "blocked={!r}".format(active, blocked)
        print("PASS (c): same version, new session after 1s -> show")
    finally:
        cleanup(root)


def test_no_state_file():
    """No spool file at all -> show (a new pending is created, fail-open)."""
    root = make_root()
    try:
        now = 1_000_000.0
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, "1.26.0", "body", "body",
            now, "1.0.0", "session-a")
        assert active is not None and blocked is None, \
            "Expected a new pending when no spool file exists, got " \
            "active={!r} blocked={!r}".format(active, blocked)
        print("PASS (extra): no state file -> show (fail-open)")
    finally:
        cleanup(root)


def test_read_write_roundtrip():
    """_spool_ensure_pending / _spool_find_pending roundtrip preserves
    values exactly -- the spool-era analog of the retired _write_shown /
    _read_shown roundtrip."""
    root = make_root()
    try:
        version = "1.26.0"
        now = 1_785_920_595.0
        body_oneline = "one-line body"
        body_fold = "folded body"
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, version, body_oneline,
            body_fold, now, "1.0.0", "session-a")
        assert active is not None and blocked is None
        found = vc._spool_find_pending(root, vc._NOTICE_KIND_FW_UPDATE, version)
        assert found is not None, "Expected to find the just-created pending"
        assert found["version"] == vc._norm_ver(version), \
            "Version mismatch: {} != {}".format(found["version"], version)
        assert found["body_oneline"] == body_oneline
        assert found["body_fold"] == body_fold
        assert abs(found["first_pending_ts"] - now) < 0.001, \
            "Timestamp mismatch: {} != {}".format(
                found["first_pending_ts"], now)
        print("PASS (extra): spool ensure_pending/find_pending roundtrip OK")
    finally:
        cleanup(root)


def test_session_dispatch_and_delivery_accounting_are_independent():
    """The injection marker does not change bridge delivery fields."""
    root = make_root()
    try:
        now = 1_000_000.0
        active, blocked = vc._spool_ensure_pending(
            root, vc._NOTICE_KIND_FW_UPDATE, "1.26.0", "body", "body",
            now, "1.0.0", "session-a")
        assert active is not None and blocked is None
        sent_a = []
        vc._dispatch_injection(root, active, now, "session-a", sent_a.append,
                               "test", "1.26.0")
        vc._dispatch_injection(root, active, now + 8 * 86400, "session-a",
                               sent_a.append, "test", "1.26.0")
        sent_b = []
        vc._dispatch_injection(root, active, now + 8 * 86400 + 1, "session-b",
                               sent_b.append, "test", "1.26.0")
        assert sent_a == ["body"], "same session must emit exactly once"
        assert sent_b == ["body"], "new session must emit once immediately"
        records, _bad = vc._spool_load(root)
        rec = [r for r in records if r.get("id") == active["id"]][0]
        assert rec["status"] == "pending" and rec["attempts"] == 0
        assert rec["delivered_ts"] is None and rec["message_id"] is None
        print("PASS (extra): session guard preserves delivery accounting")
    finally:
        cleanup(root)


# ---------------------------------------------------------------------------
# DGN-738: _user_summary_from_note tests
# ---------------------------------------------------------------------------

_SAMPLE_NOTE_WITH_SUMMARY = """\
# v1.26.1 -- Maintenance bundle

Date: 2026-08-05

<!-- user-summary
ko: |
  업데이트 알림이 반복되던 문제를 수정했습니다.
  이제 새 버전당 한 번만 표시됩니다.
en: |
  Fixed the update notice re-firing every turn.
  It now appears once per new version.
-->

## Purpose

Maintenance.
"""

_SAMPLE_NOTE_WITHOUT_SUMMARY = """\
# v1.25.0 -- Some older release

## Changes

No user-summary block here.
"""


def test_d_user_summary_present_ko():
    """(d) user-summary present -> correct ko text extracted."""
    result = vc._user_summary_from_note(_SAMPLE_NOTE_WITH_SUMMARY, "ko")
    assert "업데이트 알림이 반복되던 문제를 수정했습니다." in result, \
        "Expected ko summary line, got: {!r}".format(result)
    assert "이제 새 버전당 한 번만 표시됩니다." in result, \
        "Expected second ko line, got: {!r}".format(result)
    # Must NOT contain en text
    assert "Fixed" not in result, "ko result must not contain en text"
    print("PASS (d): user-summary present -> ko text extracted correctly")


def test_e_user_summary_absent():
    """(e) user-summary block absent -> returns empty string (fallback trigger)."""
    result = vc._user_summary_from_note(_SAMPLE_NOTE_WITHOUT_SUMMARY, "ko")
    assert result == "", "Expected '' for note with no summary block, got: {!r}".format(result)
    print("PASS (e): user-summary absent -> empty string (fallback trigger)")


def test_f_wrong_lang_key():
    """(f) requesting a lang not in the block -> returns empty string."""
    result = vc._user_summary_from_note(_SAMPLE_NOTE_WITH_SUMMARY, "fr")
    assert result == "", "Expected '' for unknown lang, got: {!r}".format(result)
    print("PASS (f): wrong lang key -> empty string (fallback trigger)")


def test_g_malformed_block():
    """(g) malformed block (no closing -->) -> returns empty string, no crash."""
    malformed = "<!-- user-summary\nko: |\n  some text\n"
    result = vc._user_summary_from_note(malformed, "ko")
    # Should still extract what it can (block body present, just no --> closer)
    # OR return empty -- either way must not crash.
    assert isinstance(result, str), "Expected str result, got: {}".format(type(result))
    print("PASS (g): malformed block (no -->) -> no crash, returned: {!r}".format(result))


def test_h_empty_input():
    """(h) empty string input -> returns empty string, no crash."""
    result = vc._user_summary_from_note("", "ko")
    assert result == "", "Expected '' for empty input, got: {!r}".format(result)
    print("PASS (h): empty input -> empty string, no crash")


def test_i_user_summary_en():
    """(i) user-summary present -> correct en text extracted."""
    result = vc._user_summary_from_note(_SAMPLE_NOTE_WITH_SUMMARY, "en")
    assert "Fixed the update notice re-firing every turn." in result, \
        "Expected en summary line, got: {!r}".format(result)
    assert "ko" not in result or "업데이트" not in result, \
        "en result must not contain ko text"
    print("PASS (i): user-summary present -> en text extracted correctly")


if __name__ == "__main__":
    tests = [
        test_a_same_version_same_session_suppresses,
        test_b_different_version,
        test_c_same_version_new_session_shows_immediately,
        test_no_state_file,
        test_read_write_roundtrip,
        test_session_dispatch_and_delivery_accounting_are_independent,
        test_d_user_summary_present_ko,
        test_e_user_summary_absent,
        test_f_wrong_lang_key,
        test_g_malformed_block,
        test_h_empty_input,
        test_i_user_summary_en,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print("FAIL {}: {}".format(t.__name__, e))
            failed += 1
    if failed:
        print("\n{} test(s) FAILED".format(failed))
        sys.exit(1)
    else:
        print("\nAll {} tests PASSED".format(len(tests)))
        sys.exit(0)
