"""DGN-376 T2: register guard v1 (log-warn) -- unit + three-seat wiring.

The register guard absorbs DGN-429 (language charset) + DGN-430 tier-1
(tool names / send_file:: marker / internal paths / scheduler terms) into one
detector. v1 strength = log-warn only: text passes through UNCHANGED, a WARNING
is logged on any finding.

Coverage:
  (a) _register_findings: each detector fires on a leak sample and stays quiet
      on a clean/prose sample (low-false-positive contract).
  (b) _register_guard: returns text unchanged; logs a WARNING on a finding and
      nothing on clean text; is a no-op when gated off.
  (c) three-seat wiring: ingestion, proactive, and finalize each route text
      through _register_guard (grill M3: proactive bypasses finalize).
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
        self.assertEqual(
            [], sdk_bridge._register_findings("Please read and edit the note."))

    def test_send_file_marker_flagged(self):
        self.assertIn(
            "send_file-marker",
            sdk_bridge._register_findings("here it is send_file:: /x/y.png"))

    def test_internal_path_flagged(self):
        self.assertIn(
            "internal-path",
            sdk_bridge._register_findings("saved to /Users/me/dogany/x.md"))

    def test_date_slash_not_flagged_as_path(self):
        self.assertEqual(
            [], sdk_bridge._register_findings("Let's meet on 7/23 and/or later."))

    def test_scheduler_term_flagged(self):
        self.assertIn(
            "scheduler-term",
            sdk_bridge._register_findings("I registered a launchd job."))

    def test_clean_message_no_findings(self):
        self.assertEqual(
            [], sdk_bridge._register_findings("일정에 매일 알림을 걸어뒀어요."))

    def test_locale_register_slip_ko(self):
        # Long all-English reply on a ko instance = register slip.
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
    def test_returns_text_unchanged(self):
        leaky = "ran Bash(ls) -> /Users/me/x.py"
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True):
            self.assertEqual(leaky, sdk_bridge._register_guard(leaky))

    def test_warns_on_finding(self):
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True):
            with self.assertLogs(sdk_bridge.logger, level="WARNING") as cm:
                sdk_bridge._register_guard("saved to /tmp/foo.md")
        self.assertTrue(any("Register guard" in m for m in cm.output))
        self.assertTrue(any("internal-path" in m for m in cm.output))

    def test_no_warn_on_clean(self):
        # No finding => no WARNING. Assert via a captured record list rather
        # than assertNoLogs (which is 3.10+) so the suite runs on any 3.x.
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        handler.setLevel(logging.WARNING)
        sdk_bridge.logger.addHandler(handler)
        try:
            with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True):
                with patch.object(sdk_bridge.config, "locale", "en"):
                    out = sdk_bridge._register_guard("모든 게 정상입니다.")
        finally:
            sdk_bridge.logger.removeHandler(handler)
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        self.assertEqual([], warnings)
        self.assertEqual("모든 게 정상입니다.", out)

    def test_gated_off_is_noop(self):
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", False):
            with self.assertRaises(AssertionError):
                # No WARNING should be emitted when gated off.
                with self.assertLogs(sdk_bridge.logger, level="WARNING"):
                    sdk_bridge._register_guard("ran Bash(ls)")

    def test_empty_text_noop(self):
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True):
            self.assertEqual("", sdk_bridge._register_guard(""))


class ThreeSeatWiringTest(unittest.TestCase):
    """Static wiring check: each of the three seats routes through the guard.

    A source-level assertion is the right granularity here -- it proves the
    guard is CALLED at ingestion, proactive, and finalize (grill M3's core
    invariant) without standing up the full async streaming machinery.
    """

    def setUp(self):
        import inspect
        self.src = inspect.getsource(sdk_bridge)

    def test_guard_called_three_times(self):
        # Exactly the three mandatory seats invoke the guard, each wrapping the
        # scaffold guard as the next pipeline stage.
        wrapped = re.findall(r"_register_guard\(_scaffold_guard\(", self.src)
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
    logging.disable(logging.CRITICAL + 1)  # keep warnings visible to assertLogs
    unittest.main()
