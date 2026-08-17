"""DGN-376 T2 / DGN-686 v2: register guard (drop-only) + is_error handling.

The register guard's fragment detectors (DGN-430: tool names / send_file::
marker / internal paths / scheduler terms) stay in _register_findings for the
log-warn contract, but the GUARD itself acts ONLY on the locale-register
finding (DGN-686 direction lock 2026-08-02):
  - a pure-English block (zero Hangul, prose >= _LOCALE_MIN_LEN) on a
    ko-locale instance is DROPPED whole (returns ""), with a WARNING;
  - otherwise the text passes through UNCHANGED.
There is NO fragment-masking tier -- partial leaks are an upstream concern.
BRIDGE_REGISTER_GUARD=0 is an emergency bypass (all text passes unchanged).

is_error results are classified (auth / transient / other) and mapped to a
LOCKED ko notice; the English detail is logged to stderr only.

Coverage:
  (a) _register_findings: each detector fires on a leak sample, quiet on clean.
  (b) _register_guard: drop-only two-way (locale drop / pass unchanged),
      code/URL/send_file prose exemptions, kill-switch bypass.
  (c) _classify_error_result: auth / transient / other, auth precedence.
  (d) three-seat wiring: ingestion, proactive, finalize route through the guard.
"""

import logging
import re
import unittest
from unittest.mock import patch

from bridge import sdk_bridge


class RegisterFindingsTest(unittest.TestCase):
    def test_tool_name_call_form_flagged(self):
        self.assertIn("tool-name",
                      sdk_bridge._register_findings("I ran Bash(ls) for you"))

    def test_tool_name_prose_not_flagged(self):
        # Common English words in prose must not trip the tool-name detector.
        # Locale pinned to en: on a ko deploy env (LOCALE=ko riding in via the
        # live project .env) the DGN-429 language detector would legitimately
        # flag this all-English sentence and muddy the assertion under test.
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertEqual(
                [],
                sdk_bridge._register_findings("Please read and edit the note."))

    def test_send_file_marker_flagged(self):
        self.assertIn(
            "send_file-marker",
            sdk_bridge._register_findings("here it is send_file:: /x/y.png"))

    def test_internal_path_flagged(self):
        self.assertIn(
            "internal-path",
            sdk_bridge._register_findings("saved to /Users/me/dogany/x.md"))

    def test_date_slash_not_flagged_as_path(self):
        # Locale pinned to en (see test_tool_name_prose_not_flagged).
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertEqual(
                [],
                sdk_bridge._register_findings("Let's meet on 7/23 and/or later."))

    def test_scheduler_term_flagged(self):
        self.assertIn(
            "scheduler-term",
            sdk_bridge._register_findings("I registered a launchd job."))

    def test_clean_message_no_findings(self):
        self.assertEqual(
            [], sdk_bridge._register_findings("일정에 매일 알림을 걸어뒀어요."))

    def test_locale_register_slip_ko(self):
        long_en = ("This is a fully English answer with no Korean at all, "
                   "which on a Korean-locale instance is a register slip.")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertIn("locale-register",
                          sdk_bridge._register_findings(long_en))

    def test_locale_short_english_exempt(self):
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings("OK done."))

    def test_locale_en_instance_english_ok(self):
        long_en = "A perfectly normal long English answer on an en instance." * 3
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertNotIn("locale-register",
                             sdk_bridge._register_findings(long_en))


class RegisterGuardBehaviourTest(unittest.TestCase):
    """DGN-686 v2 drop-only: exactly two return paths (drop "" / unchanged).

    The guard is gated by BRIDGE_REGISTER_GUARD; the test env exports it as 0
    (an emergency-bypass value), so guard-ON behaviour tests patch it True.
    """

    def setUp(self):
        # Force the guard ON for behaviour tests (the shell/test env may set
        # BRIDGE_REGISTER_GUARD=0). The kill-switch test overrides this.
        self._guard = patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True)
        self._guard.start()
        self.addCleanup(self._guard.stop)

    # --- drop tier ----------------------------------------------------------
    def test_locale_register_drops_block(self):
        long_en = ("This is just a background task notification and there is "
                   "nothing to report to the owner right now at all.")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            with self.assertLogs(sdk_bridge.logger, level="WARNING") as cm:
                out = sdk_bridge._register_guard(long_en)
        self.assertEqual("", out)
        self.assertTrue(any("locale-register" in m for m in cm.output))

    # --- no fragment-mask tier: a ko body with a fragment passes UNCHANGED ---
    def test_fragment_in_ko_body_passes_unchanged(self):
        # DGN-686 removed masking: a tool name / path inside a Korean body is
        # NOT touched by the bridge (upstream leader-summary owns that).
        leaky = "결과를 /Users/me/dogany/x.md 파일에 저장해뒀습니다. Bash(ls) 확인함."
        with patch.object(sdk_bridge.config, "locale", "ko"):
            out = sdk_bridge._register_guard(leaky)
        self.assertEqual(leaky, out)

    def test_options_marker_passes_intact(self):
        ko_options = ("배포 방식을 골라주세요.\n\n"
                      "1. 즉시 배포\n2. 잠시 대기\n\n[[OPTIONS]]")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual(ko_options, sdk_bridge._register_guard(ko_options))

    def test_send_file_delivery_line_untouched(self):
        msg = ("요청하신 리포트입니다.\n"
               "send_file:: /Users/me/dogany/files/outbox/report.png")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual(msg, sdk_bridge._register_guard(msg))

    def test_bare_send_file_line_not_dropped(self):
        # A long bare delivery line is a machine marker, excluded from prose --
        # it must survive the drop tier even with zero Hangul.
        msg = ("send_file:: /Users/somebody/dogany/dev-crew/agents/main/"
               "files/outbox/weekly-status-report-with-a-long-name.png")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual(msg, sdk_bridge._register_guard(msg))

    def test_no_warn_on_clean(self):
        # No drop => no WARNING, text untouched. Captured-record list rather
        # than assertNoLogs (3.10+) so the suite runs on any 3.x.
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        handler.setLevel(logging.WARNING)
        sdk_bridge.logger.addHandler(handler)
        try:
            with patch.object(sdk_bridge.config, "locale", "ko"):
                out = sdk_bridge._register_guard("모든 게 정상입니다.")
        finally:
            sdk_bridge.logger.removeHandler(handler)
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        self.assertEqual([], warnings)
        self.assertEqual("모든 게 정상입니다.", out)

    def test_en_locale_long_english_passes(self):
        long_en = "A perfectly normal long English answer on an en instance." * 3
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertEqual(long_en, sdk_bridge._register_guard(long_en))

    def test_empty_text_noop(self):
        self.assertEqual("", sdk_bridge._register_guard(""))

    # --- kill-switch (emergency bypass) -------------------------------------
    def test_gated_off_passes_english_unchanged(self):
        long_en = ("This is just a background task notification and there is "
                   "nothing to report to the owner right now at all.")
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", False):
            with patch.object(sdk_bridge.config, "locale", "ko"):
                self.assertEqual(long_en, sdk_bridge._register_guard(long_en))

    # --- MAJOR-2 code / URL prose exemption (retained) ----------------------
    def test_code_block_only_passes(self):
        code = ("```python\n"
                "def add(a, b):\n"
                "    return a + b  # a long enough English-only code body\n"
                "```")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual(code, sdk_bridge._register_guard(code))

    def test_bare_url_only_passes(self):
        url = "http://console.example.com:8484/decision/dec-042-some-long-slug-here"
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual(url, sdk_bridge._register_guard(url))

    def test_english_prose_around_code_still_drops(self):
        # Exemption is scoped: real English PROSE outside the fence still counts.
        mixed = ("Here is a fairly long English explanation of the change with "
                 "no Korean at all in the surrounding prose whatsoever.\n"
                 "```\ncode\n```")
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual("", sdk_bridge._register_guard(mixed))


class ErrorClassifyTest(unittest.TestCase):
    """DGN-686: is_error result -> auth / transient / other (LOCKED mapping)."""

    def test_auth_401(self):
        self.assertEqual(
            "auth",
            sdk_bridge._classify_error_result("HTTP 401 invalid_api_key"))

    def test_auth_authentication_word(self):
        self.assertEqual(
            "auth",
            sdk_bridge._classify_error_result("authentication_error: token expired"))

    def test_transient_overloaded(self):
        self.assertEqual(
            "transient",
            sdk_bridge._classify_error_result("Error 529 overloaded_error"))

    def test_transient_timeout(self):
        self.assertEqual(
            "transient",
            sdk_bridge._classify_error_result("upstream connection timed out"))

    def test_other_default(self):
        self.assertEqual(
            "other",
            sdk_bridge._classify_error_result("some unexpected internal failure"))

    def test_auth_precedence_over_transient(self):
        # A message carrying both an auth and a transient marker must NOT retry.
        self.assertEqual(
            "auth",
            sdk_bridge._classify_error_result(
                "401 unauthorized after connection timeout"))

    def test_locked_messages_present(self):
        from bridge import messages
        self.assertEqual(
            messages.ERROR_TRANSIENT_RETRY, "일시적으로 처리에 실패했어요. 다시 시도할까요?")
        self.assertEqual(
            messages.ERROR_AUTH_RELOGIN,
            "클로드에 다시 로그인 하신 후 알려주시면 복구하겠습니다.")
        self.assertEqual(
            messages.ERROR_GENERIC_RETRY, "처리 실패했어요. 다시 시도할까요?")

    # --- DGN-686 MAJOR-1: classifier is the single transient source ---------
    def test_retryable_sdk_error_catches_overloaded(self):
        # _classify_error_result now feeds _is_retryable_sdk_error, so a 529 /
        # overloaded raised exception is retryable even though _RETRYABLE_MSG
        # alone never listed it.
        self.assertTrue(
            sdk_bridge._is_retryable_sdk_error(Exception("Error 529 overloaded_error")))

    def test_retryable_sdk_error_still_catches_legacy_markers(self):
        self.assertTrue(
            sdk_bridge._is_retryable_sdk_error(Exception("connection refused")))

    def test_retryable_sdk_error_rejects_auth(self):
        # An auth failure must NOT be auto-retried (401 classifies as auth).
        self.assertFalse(
            sdk_bridge._is_retryable_sdk_error(Exception("HTTP 401 invalid_api_key")))


class ThreeSeatWiringTest(unittest.TestCase):
    """Static wiring check: each of the three seats routes through the guard.

    Source-level assertion proves the guard is CALLED at ingestion, proactive,
    and finalize (grill M3's core invariant) without standing up the async
    machinery.
    """

    def setUp(self):
        import inspect
        self.src = inspect.getsource(sdk_bridge)

    def test_guard_called_three_times(self):
        # Ingestion, proactive, and the finalize non-error branch each wrap the
        # scaffold guard. Tolerate whitespace between the two calls.
        wrapped = re.findall(r"_register_guard\(\s*_scaffold_guard\(", self.src)
        self.assertEqual(3, len(wrapped),
                         "expected 3 seats wrapping scaffold guard, got %d"
                         % len(wrapped))

    def test_ingestion_seat_present(self):
        self.assertIn("seat 1/3", self.src)

    def test_proactive_seat_present(self):
        self.assertIn("seat 2/3", self.src)

    def test_finalize_seat_present(self):
        self.assertIn("seat 3/3", self.src)


if __name__ == "__main__":
    unittest.main()
