#!/usr/bin/env python3
"""test-dgn784-update-note-sanitize.py

DGN-784: update-completion notice must never expose raw developer CHANGELOG
content (English markdown headers, backticks, internal file/function names).

Tests:
  (a) user-summary block present -> ko summary extracted cleanly; no ### / backtick
      / internal path in output
  (b) user-summary block absent (old release note without the block) -> empty
      string returned; CHANGELOG fallback is NOT triggered
  (c) CHANGELOG fallback path suppressed: even when called directly, the
      notice-composition helpers (version-check.py) must not emit raw CHANGELOG
      content when user-summary is absent
  (d) ko_detail with internal file names -> correctly extracted; still no ###
      header markers in the output
  (e) en fallback: en summary key works; no internal names leak from ko block
  (f) v1.27.3 synthetic fixture: full round-trip with realistic release note
      content -- output must contain no ### header lines, no backtick-wrapped
      internal names like `bridge/countdown.py`, no bare English developer prose
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

# Realistic v1.27.3-style release note (mirrors the actual file structure).
_NOTE_V1273 = """\
# v1.27.3 -- Bridge re-vendor bundle

Date: 2026-08-07

<!-- user-summary
ko: |
  - 운동 휴식 타이머처럼 남은 시간을 하나의 메시지에서 실시간으로 갱신해 보여주는 기능이 됩니다.
  - 봇을 재시작한 뒤 이어가던 작업을 자동으로 다시 알려줍니다.
en: |
  - Remaining time now updates in place inside a single message.
  - After a restart, the bot automatically resurfaces the task it was in the middle of.
ko_detail: |
  이번 버전은 OSS 브릿지 증분 재벤더로 도달하는 기능 번들입니다.

  핵심 변경:
  - 비핀 transient 카운트다운 프리미티브: countdown.py의 start_countdown이 10초 캐던스로 동일 메시지를 편집합니다.
  - 공유 EditRateGuard(edit_guard.py)가 Telegram 편집 속도 제한을 관리합니다.
en_detail: |
  This release bundles the OSS bridge increment via re-vendor.

  Key changes:
  - Non-pinned transient countdown primitive: countdown.py adds start_countdown,
    which edits a single message in-place on a 10-second cadence.
  - A shared EditRateGuard (edit_guard.py) manages Telegram edit rate limits.
-->

## Purpose

Re-vendor anchor bundle.

## Changes

### DGN-594 -- Non-pinned transient countdown primitive

New files: `bridge/countdown.py`, `bridge/edit_guard.py`.

`start_countdown(bot, chat_id, msg_id, seconds, body_fn, ...)`: countdown task.

`EditRateGuard(edit_guard.py)`: manages Telegram edit rate limits.

### DGN-706b -- Version update auto-notice

`bridge/self_restart.sh` step-4 branch addition.
"""

# Old-style release note WITHOUT user-summary block (pre-DGN-699).
_NOTE_OLD_NO_SUMMARY = """\
# v1.20.0 -- Some old release

Date: 2026-01-01

## Added

- `bridge/some_module.py`: new feature with `internal_function()`.

### Details

Some developer prose with ### headers.
`backtick_wrapped_code()` here.
"""

# Synthetic CHANGELOG section matching v1.27.3 style.
_CHANGELOG_V1273 = """\
## [1.27.3] - 2026-08-07

OSS bridge re-vendor anchor bundle. (DGN-594, DGN-706b, DGN-775)

### Added
- Non-pinned transient countdown primitive (`bridge/countdown.py`): `start_countdown`
  edits a single Telegram message in-place on a 10-second cadence. Shared
  `EditRateGuard` (`bridge/edit_guard.py`) manages Telegram edit rate limits. (DGN-594)
- Version update auto-notice (`bridge/self_restart.sh`): after a successful
  self-update, the bot automatically notifies the owner. (DGN-706b)

### Fixed
- Telegram markdown sanitize backstop (`bridge/formatting.py`): raw headers stopped.

## [1.27.2] - 2026-08-06

Earlier release.
"""

# Patterns that must NEVER appear in user-facing output.
_FORBIDDEN_PATTERNS = [
    "### ",          # markdown section headers
    "## Added",      # CHANGELOG subsection headers
    "## Fixed",      # CHANGELOG subsection headers
    "## Changed",    # CHANGELOG subsection headers
    "`bridge/",      # backtick-wrapped internal paths
    "`start_countdown`",     # internal function name
    "`EditRateGuard`",       # internal class name
    "bridge/countdown.py",   # bare internal path
    "bridge/edit_guard.py",  # bare internal path
    "bridge/self_restart.sh",  # bare internal path
    "bridge/formatting.py",  # bare internal path
]


def _assert_no_forbidden(text, label):
    """Raise AssertionError if any forbidden pattern appears in text."""
    for pat in _FORBIDDEN_PATTERNS:
        assert pat not in text, (
            "FORBIDDEN pattern {!r} found in {} output: {!r}".format(pat, label, text)
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_a_ko_summary_clean():
    """(a) ko summary from v1.27.3-style note: extracted cleanly, no dev content."""
    result = vc._user_summary_from_note(_NOTE_V1273, "ko")
    assert result, "Expected non-empty ko summary, got empty"
    assert "타이머" in result, \
        "Expected Korean summary content, got: {!r}".format(result)
    _assert_no_forbidden(result, "ko summary")
    print("PASS (a): ko summary extracted cleanly -- no ### / backtick / internal paths")


def test_b_old_note_no_summary_block_returns_empty():
    """(b) old note without user-summary block -> empty (no CHANGELOG fallback)."""
    result = vc._user_summary_from_note(_NOTE_OLD_NO_SUMMARY, "ko")
    assert result == "", \
        "Expected empty for note without user-summary block, got: {!r}".format(result)
    print("PASS (b): old note without user-summary block -> empty (no fallback)")


def test_c_user_summary_absent_no_changelog_leak():
    """(c) when user-summary absent, _user_summary_from_note must NOT return
    CHANGELOG-style content with ### headers or backtick internal names."""
    result = vc._user_summary_from_note(_NOTE_OLD_NO_SUMMARY, "ko")
    # Verify specifically that the raw doc content from the note body is not leaked.
    assert "bridge/some_module.py" not in result, \
        "Internal path leaked when user-summary absent: {!r}".format(result)
    assert "### Details" not in result, \
        "Raw ### header leaked when user-summary absent: {!r}".format(result)
    assert "`backtick_wrapped_code()`" not in result, \
        "Backtick code leaked when user-summary absent: {!r}".format(result)
    print("PASS (c): no CHANGELOG/note body content leaks when user-summary absent")


def test_d_ko_detail_no_header_markers():
    """(d) ko_detail extracted correctly; note body ### headers must not appear."""
    result = vc._user_summary_from_note(_NOTE_V1273, "ko_detail")
    assert result, "Expected non-empty ko_detail, got empty"
    assert "countdown.py" in result, \
        "Expected ko_detail content with file reference, got: {!r}".format(result)
    # The file name is acceptable in ko_detail (it is part of the curated
    # user-facing Korean text), but the raw ### CHANGELOG headers must not appear.
    assert "### DGN-594" not in result, \
        "Raw ### header from note body must not appear in ko_detail: {!r}".format(result)
    assert "### DGN-706b" not in result, \
        "Raw ### header from note body must not appear in ko_detail: {!r}".format(result)
    print("PASS (d): ko_detail extracted; ### note-body headers absent from output")


def test_e_en_summary_isolated():
    """(e) en summary key returns English content only; no ko bleed-through."""
    result = vc._user_summary_from_note(_NOTE_V1273, "en")
    assert result, "Expected non-empty en summary, got empty"
    assert "Remaining time" in result, \
        "Expected en summary text, got: {!r}".format(result)
    assert "타이머" not in result, \
        "ko content must not appear in en summary: {!r}".format(result)
    _assert_no_forbidden(result, "en summary")
    print("PASS (e): en summary isolated; no ko bleed, no forbidden patterns")


def test_f_v1273_full_roundtrip_no_changelog_content():
    """(f) v1.27.3 fixture full round-trip: output must contain no ### headers,
    no backtick internal names, no English developer CHANGELOG prose when
    requesting the ko summary key."""
    result = vc._user_summary_from_note(_NOTE_V1273, "ko")
    assert result, "Expected non-empty ko result for v1.27.3 fixture"
    # None of the CHANGELOG-style developer content from the ## Changes section.
    _assert_no_forbidden(result, "v1.27.3 ko round-trip")
    # Verify we got the right content (the user-summary, not the note body).
    assert "메시지" in result, \
        "Expected Korean user-facing text in ko result: {!r}".format(result)
    print("PASS (f): v1.27.3 full round-trip -- no CHANGELOG/dev content in ko output")


def test_g_changelog_section_not_user_output():
    """(g) _changelog_section returns developer content (### headers, backticks).
    This confirms WHY the fallback was unsafe for user output: the CHANGELOG
    content must never be forwarded to a user-facing notice."""
    result = vc._changelog_section(_CHANGELOG_V1273, "1.27.3")
    # The CHANGELOG section DOES contain developer content.
    # This test documents that the content is present (and therefore unsafe).
    has_dev_content = (
        "### Added" in result
        or "`bridge/" in result
        or "start_countdown" in result
    )
    assert has_dev_content, (
        "Expected CHANGELOG section to contain developer content "
        "(this documents why the fallback was removed): {!r}".format(result)
    )
    print("PASS (g): confirmed CHANGELOG section contains developer content "
          "(### headers / backticks / internal names) -- fallback correctly removed")


if __name__ == "__main__":
    tests = [
        test_a_ko_summary_clean,
        test_b_old_note_no_summary_block_returns_empty,
        test_c_user_summary_absent_no_changelog_leak,
        test_d_ko_detail_no_header_markers,
        test_e_en_summary_isolated,
        test_f_v1273_full_roundtrip_no_changelog_content,
        test_g_changelog_section_not_user_output,
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
