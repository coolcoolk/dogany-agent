#!/usr/bin/env python3
"""test-dgn742-725-notice-features.py

Tests for DGN-742 (ko_detail/en_detail second fold in update-completion notice)
and DGN-725 (DGN ticket-ref strip in CHANGELOG fallback path).

No network. Self-contained. ASCII / Korean only where needed.

DGN-742 cases (version-check.py parser -- shared with self-update.sh AWK):
  (a) ko_detail present -> _user_summary_from_note extracts it correctly
  (b) en_detail present -> _user_summary_from_note extracts it correctly
  (c) ko_detail absent (only ko/en keys) -> returns empty string
  (d) truncation: a long detail string is capped at ~2500 chars in self-update
      logic (tested via the shell script behaviour; here we test parser only)

DGN-725 cases (version-check.py _changelog_section / _strip_ticket_refs):
  (e) parenthesized ref (DGN-NNN) stripped from CHANGELOG scrape
  (f) bare DGN-NNN ref stripped
  (g) multiple refs in one section all stripped
  (h) no DGN refs -> text unchanged
  (i) empty changelog body -> empty string returned
  (j) fallback output (via _changelog_section) contains no DGN- substring
"""
import importlib.util
import os
import sys

# ---------------------------------------------------------------------------
# Load version-check.py as a module without running main()
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROUTINES_DIR = os.path.dirname(_HERE)
_TARGET = os.path.join(_ROUTINES_DIR, "version-check.py")

spec = importlib.util.spec_from_file_location("version_check", _TARGET)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOTE_WITH_DETAIL = """\
# v1.27.0 -- Notice improvements

Date: 2026-08-06

<!-- user-summary
ko: |
  업데이트 알림이 개선됐습니다.
en: |
  Update notice improved.
ko_detail: |
  이번 업데이트에서는 업데이트 완료 알림에 자세한 변경 내용을 추가했습니다.
  내용이 길 경우 자동으로 축약됩니다.
en_detail: |
  This update adds a detailed changes fold to the update-completion notice.
  Long content is automatically truncated.
-->

## Changes

Improvements to the update notice system.
"""

_NOTE_WITHOUT_DETAIL = """\
# v1.26.1 -- Maintenance bundle

Date: 2026-08-05

<!-- user-summary
ko: |
  업데이트 알림 개선.
en: |
  Update notice fix.
-->

## Changes

No detail keys here.
"""

_CHANGELOG_WITH_REFS = """\
## [1.27.0] - 2026-08-06

Framework notice improvements. (DGN-742, DGN-725)

### Added
- Detail fold in update-completion notice. (DGN-742)
- Ticket-ref strip in CHANGELOG fallback. (DGN-725)

### Changed
- Version check fallback now strips DGN-725 refs. DGN-742 DGN-725

## [1.26.1] - 2026-08-05

Other stuff.
"""

_CHANGELOG_NO_REFS = """\
## [1.27.0] - 2026-08-06

Notice improvements.

### Added
- Better notices.

## [1.26.1] - 2026-08-05

Older.
"""

_CHANGELOG_EMPTY_SECTION = """\
## [1.27.0] - 2026-08-06

## [1.26.1] - 2026-08-05
"""


# ---------------------------------------------------------------------------
# DGN-742 tests: ko_detail / en_detail parsing
# ---------------------------------------------------------------------------

def test_a_ko_detail_present():
    """(a) ko_detail present -> correct Korean detail extracted."""
    result = vc._user_summary_from_note(_NOTE_WITH_DETAIL, "ko_detail")
    assert "이번 업데이트에서는" in result, \
        "Expected ko_detail content, got: {!r}".format(result)
    assert "자동으로 축약됩니다" in result, \
        "Expected second ko_detail line, got: {!r}".format(result)
    # Must NOT contain en_detail text
    assert "truncated" not in result, \
        "ko_detail must not contain en_detail text, got: {!r}".format(result)
    print("PASS (a): ko_detail present -> Korean detail extracted correctly")


def test_b_en_detail_present():
    """(b) en_detail present -> correct English detail extracted."""
    result = vc._user_summary_from_note(_NOTE_WITH_DETAIL, "en_detail")
    assert "adds a detailed changes fold" in result, \
        "Expected en_detail content, got: {!r}".format(result)
    assert "automatically truncated" in result, \
        "Expected second en_detail line, got: {!r}".format(result)
    # Must NOT contain ko_detail text
    assert "업데이트에서는" not in result, \
        "en_detail must not contain ko_detail text, got: {!r}".format(result)
    print("PASS (b): en_detail present -> English detail extracted correctly")


def test_c_detail_absent_no_detail_keys():
    """(c) ko_detail absent (note only has ko/en) -> empty string (no second fold)."""
    result = vc._user_summary_from_note(_NOTE_WITHOUT_DETAIL, "ko_detail")
    assert result == "", \
        "Expected '' when ko_detail absent, got: {!r}".format(result)
    print("PASS (c): ko_detail absent -> empty string (parity: no second fold)")


def test_d_detail_does_not_bleed_into_summary():
    """(d) ko key returns summary only, not detail (key isolation)."""
    ko_result = vc._user_summary_from_note(_NOTE_WITH_DETAIL, "ko")
    assert "업데이트 알림이 개선됐습니다" in ko_result, \
        "Expected ko summary, got: {!r}".format(ko_result)
    # Ensure ko does NOT include the ko_detail content
    assert "이번 업데이트에서는" not in ko_result, \
        "ko summary must not include ko_detail content, got: {!r}".format(ko_result)
    print("PASS (d): ko summary key isolated -- detail content does not bleed in")


# ---------------------------------------------------------------------------
# DGN-725 tests: ticket-ref stripping in CHANGELOG fallback
# ---------------------------------------------------------------------------

def test_e_parenthesized_ref_stripped():
    """(e) parenthesized (DGN-NNN) refs removed from CHANGELOG scrape."""
    result = vc._changelog_section(_CHANGELOG_WITH_REFS, "1.27.0")
    assert "DGN-" not in result, \
        "Expected no DGN- in output after strip, got: {!r}".format(result)
    print("PASS (e): parenthesized (DGN-NNN) refs stripped from CHANGELOG fallback")


def test_f_bare_ref_stripped():
    """(f) bare DGN-NNN refs removed from CHANGELOG scrape."""
    # _strip_ticket_refs directly
    text = "Some change DGN-742 and another one."
    result = vc._strip_ticket_refs(text)
    assert "DGN-" not in result, \
        "Expected no DGN- after strip, got: {!r}".format(result)
    print("PASS (f): bare DGN-NNN ref stripped by _strip_ticket_refs")


def test_g_multiple_refs_all_stripped():
    """(g) multiple DGN refs in one section all stripped."""
    result = vc._changelog_section(_CHANGELOG_WITH_REFS, "1.27.0")
    assert "DGN-" not in result, \
        "Expected no DGN- refs in multi-ref section, got: {!r}".format(result)
    # Ensure meaningful content remains
    assert "Detail fold" in result or "notice" in result.lower(), \
        "Meaningful content should remain after stripping, got: {!r}".format(result)
    print("PASS (g): multiple DGN refs all stripped, content preserved")


def test_h_no_refs_text_preserved():
    """(h) no DGN refs -> text content preserved unchanged."""
    result = vc._changelog_section(_CHANGELOG_NO_REFS, "1.27.0")
    assert "Better notices" in result, \
        "Expected 'Better notices' to survive strip, got: {!r}".format(result)
    assert "DGN-" not in result, \
        "No DGN- expected (none in source), got: {!r}".format(result)
    print("PASS (h): no DGN refs -> text content preserved")


def test_i_empty_section_returns_empty():
    """(i) empty CHANGELOG section body -> returns empty string."""
    result = vc._changelog_section(_CHANGELOG_EMPTY_SECTION, "1.27.0")
    assert result == "", \
        "Expected '' for empty section, got: {!r}".format(result)
    print("PASS (i): empty CHANGELOG section -> empty string")


def test_j_fallback_output_contains_no_dgn():
    """(j) full _changelog_section call: output must contain no 'DGN-' substring."""
    result = vc._changelog_section(_CHANGELOG_WITH_REFS, "1.27.0")
    assert "DGN-" not in result, \
        "FAIL: fallback output still contains DGN- ref: {!r}".format(result)
    print("PASS (j): _changelog_section output contains no DGN- substring")


def test_k_strip_ticket_refs_direct():
    """(k) _strip_ticket_refs with mixed parenthesized and bare refs."""
    text = "Added feature (DGN-123). Also DGN-456 fixed. Normal text."
    result = vc._strip_ticket_refs(text)
    assert "DGN-" not in result, \
        "Expected no DGN- after strip, got: {!r}".format(result)
    assert "Added feature" in result, \
        "Surrounding text should be preserved, got: {!r}".format(result)
    assert "Also" in result, \
        "Context words should survive, got: {!r}".format(result)
    assert "Normal text" in result, \
        "Trailing text should survive, got: {!r}".format(result)
    print("PASS (k): _strip_ticket_refs handles mixed ref styles correctly")


if __name__ == "__main__":
    tests = [
        test_a_ko_detail_present,
        test_b_en_detail_present,
        test_c_detail_absent_no_detail_keys,
        test_d_detail_does_not_bleed_into_summary,
        test_e_parenthesized_ref_stripped,
        test_f_bare_ref_stripped,
        test_g_multiple_refs_all_stripped,
        test_h_no_refs_text_preserved,
        test_i_empty_section_returns_empty,
        test_j_fallback_output_contains_no_dgn,
        test_k_strip_ticket_refs_direct,
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
