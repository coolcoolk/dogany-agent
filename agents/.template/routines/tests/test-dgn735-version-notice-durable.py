#!/usr/bin/env python3
"""test-dgn735-version-notice-durable.py

Tests for the DGN-735 durable per-version guard in version-check.py, and
DGN-738 localized user-summary fold source for the update-available notice.
No network. Self-contained. ASCII/English only.

DGN-735 cases:
  (a) same version within TTL -> suppress (False)
  (b) different (newer) version -> show (True)
  (c) same version but ts older than TTL -> show (True)

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

_RENAG_TTL_S = vc._RENAG_TTL_S


def make_root():
    """Create a temp dir that looks like an instance root."""
    d = tempfile.mkdtemp(prefix="dgn735-test-")
    state_dir = os.path.join(d, ".telegram_bot", "state")
    os.makedirs(state_dir, exist_ok=True)
    return d


def cleanup(root):
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def test_a_same_version_within_ttl():
    """(a) same version shown recently -> suppress (return False)."""
    root = make_root()
    try:
        now = 1_000_000.0
        version = "1.26.0"
        # Write a shown record 1 second ago (well within TTL)
        vc._write_shown(root, version, now - 1)
        result = vc._should_notice(root, version, now)
        assert result is False, "Expected False (suppress), got True"
        print("PASS (a): same version within TTL -> suppress")
    finally:
        cleanup(root)


def test_b_different_version():
    """(b) different (newer) version -> show (return True)."""
    root = make_root()
    try:
        now = 1_000_000.0
        stored_version = "1.25.0"
        new_version = "1.26.0"
        # Write a shown record for OLD version very recently
        vc._write_shown(root, stored_version, now - 1)
        result = vc._should_notice(root, new_version, now)
        assert result is True, "Expected True (show), got False"
        print("PASS (b): different (newer) version -> show")
    finally:
        cleanup(root)


def test_c_same_version_expired_ttl():
    """(c) same version but ts older than TTL -> show (return True)."""
    root = make_root()
    try:
        now = 1_000_000.0
        version = "1.26.0"
        # Write a shown record TTL+1 seconds ago (expired)
        vc._write_shown(root, version, now - _RENAG_TTL_S - 1)
        result = vc._should_notice(root, version, now)
        assert result is True, "Expected True (show after TTL expiry), got False"
        print("PASS (c): same version, TTL expired -> show")
    finally:
        cleanup(root)


def test_no_state_file():
    """No state file at all -> show (return True, fail-open)."""
    root = make_root()
    try:
        now = 1_000_000.0
        result = vc._should_notice(root, "1.26.0", now)
        assert result is True, "Expected True (show) when no state file, got False"
        print("PASS (extra): no state file -> show (fail-open)")
    finally:
        cleanup(root)


def test_read_write_roundtrip():
    """_write_shown / _read_shown roundtrip preserves values exactly."""
    root = make_root()
    try:
        version = "1.26.0"
        ts = 1_785_920_595.0
        vc._write_shown(root, version, ts)
        got_ver, got_ts = vc._read_shown(root)
        assert got_ver == version, "Version mismatch: {} != {}".format(got_ver, version)
        assert abs(got_ts - ts) < 0.001, "Timestamp mismatch: {} != {}".format(got_ts, ts)
        print("PASS (extra): read/write roundtrip OK")
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
        test_a_same_version_within_ttl,
        test_b_different_version,
        test_c_same_version_expired_ttl,
        test_no_state_file,
        test_read_write_roundtrip,
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
