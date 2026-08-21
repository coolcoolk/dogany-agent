"""DGN-429 (+DGN-139): final-output language guard -- hybrid v1.

Leg 1 (prompt): _compose_system_prompt() appends the model-facing output-
language rule (i18n key output_lang_prompt) naming the configured locale's
language; gated by OUTPUT_LANG_GUARD.

Leg 2 (detector backstop): the locale-register finding inside
_register_findings upgrades from the DGN-376 coarse zero-Hangul rule to the
DGN-429 locked charset heuristic -- strip code fences / inline code / URLs,
then fire iff ascii_alpha >= 20 AND hangul < 5 AND en_ratio > 0.70. v1 stays
log-warn only (text passes unchanged); the error path skips the language
detector (English error descriptions are by design).

Coverage:
  (a) detector: catches mostly-English replies with sprinkled Hangul (the
      case the old zero-Hangul rule missed) and stays quiet on Korean prose,
      code-only, inline-code-heavy, and link-only replies (false-positive
      contract), on en locale, and under the min-alpha floor.
  (b) gates: OUTPUT_LANG_GUARD=0 disables only the language axis (leak
      detectors keep firing); lang_check=False (error path) does the same.
  (c) prompt leg: composed system prompt carries the language rule with the
      right language name; gate off returns the base fragment unchanged;
      wiring check that the SDK options use _compose_system_prompt.
  (d) log-warn contract: text is returned unchanged even on a finding.
"""

import re
import unittest
from unittest.mock import patch

from bridge import messages, sdk_bridge

# Mostly-English final reply with a couple of Hangul characters sprinkled in:
# the DGN-376 zero-Hangul rule missed this shape, the DGN-429 heuristic must
# catch it (alpha >> 20, hangul < 5, ratio ~1.0).
SPRINKLED_LEAK = (
    "Now I see the problem 네. The scheduler was never armed, so the morning "
    "briefing silently skipped. I re-armed it and verified the next fire time."
)

KOREAN_PROSE = (
    "아침 브리핑이 조용히 건너뛰어진 원인을 찾았습니다. 스케줄이 등록되지 "
    "않아서였고, 다시 등록한 뒤 다음 실행 시각까지 확인했습니다."
)

CODE_ONLY_REPLY = (
    "```\n"
    "def morning_briefing():\n"
    "    schedule.every().day.at('07:30').do(push_briefing)\n"
    "    return schedule.next_run()\n"
    "```"
)


class LangSlipDetectorTest(unittest.TestCase):
    """Detector behavior with the language gate pinned ON.

    The gate is pinned (not inherited from the ambient environment) so a
    deploy env that exports OUTPUT_LANG_GUARD=0 -- e.g. a dev agent -- cannot
    turn the negative assertions into trivial passes.
    """

    def setUp(self):
        patcher = patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sprinkled_hangul_english_reply_flagged(self):
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertIn(
                "locale-register", sdk_bridge._register_findings(SPRINKLED_LEAK)
            )

    def test_all_english_reply_flagged(self):
        long_en = (
            "This is a fully English answer with no Korean at all, which on "
            "a Korean-locale instance is a register slip."
        )
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertIn(
                "locale-register", sdk_bridge._register_findings(long_en)
            )

    def test_korean_prose_not_flagged(self):
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(KOREAN_PROSE))

    def test_korean_quoting_english_terms_not_flagged(self):
        mixed = (
            "브릿지의 stop reason 게이트가 end turn 메시지만 스트리밍하도록 "
            "바뀌었고, interim narration 은 이제 표시되지 않습니다."
        )
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(mixed))

    def test_code_only_reply_not_flagged(self):
        # Regression vs the DGN-376 rule: a long zero-Hangul code block used to
        # satisfy (len>=80 AND no Hangul). Code fences are stripped first now.
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(CODE_ONLY_REPLY))

    def test_unclosed_code_fence_stripped(self):
        # Streamed partials can carry an unterminated fence; it must still be
        # stripped rather than counted as English prose.
        partial = "```\nselect count(distinct user_id) from events where day"
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(partial))

    def test_inline_code_stripped(self):
        text = "실행: `git rebase --continue origin main topic branch cleanup`"
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(text))

    def test_link_only_reply_not_flagged(self):
        text = "여기요: https://github.com/anthropics/claude-agent-sdk-python"
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings(text))

    def test_short_english_under_alpha_floor_exempt(self):
        with patch.object(sdk_bridge.config, "locale", "ko"):
            self.assertEqual([], sdk_bridge._register_findings("OK done."))

    def test_en_locale_never_flagged(self):
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertNotIn(
                "locale-register", sdk_bridge._register_findings(SPRINKLED_LEAK)
            )


class LangGuardGatingTest(unittest.TestCase):
    def setUp(self):
        # Default gate state for this class is ON; individual tests override.
        patcher = patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_output_lang_guard_off_disables_language_only(self):
        leaky = SPRINKLED_LEAK + " saved under /tmp/briefing.md"
        with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
            with patch.object(sdk_bridge.config, "locale", "ko"):
                findings = sdk_bridge._register_findings(leaky)
        self.assertNotIn("locale-register", findings)
        self.assertIn("internal-path", findings)

    def test_error_path_lang_check_false_skips_language_only(self):
        leaky = SPRINKLED_LEAK + " saved under /tmp/briefing.md"
        with patch.object(sdk_bridge.config, "locale", "ko"):
            findings = sdk_bridge._register_findings(leaky, lang_check=False)
        self.assertNotIn("locale-register", findings)
        self.assertIn("internal-path", findings)

    def test_guard_returns_text_unchanged_on_language_finding(self):
        # Advisory contract under the DGN-686 v2 merge: the DGN-429 detector
        # flags a sprinkled-Hangul leak via _register_findings, but the guard
        # never mutates it -- the v2 drop tier acts only on ZERO-Hangul prose,
        # and SPRINKLED_LEAK carries Hangul, so it must pass unchanged.
        with patch.object(sdk_bridge, "BRIDGE_REGISTER_GUARD", True):
            with patch.object(sdk_bridge.config, "locale", "ko"):
                out = sdk_bridge._register_guard(SPRINKLED_LEAK)
                findings = sdk_bridge._register_findings(SPRINKLED_LEAK)
        self.assertEqual(SPRINKLED_LEAK, out)
        self.assertIn("locale-register", findings)

    def test_finalize_seat_skips_language_on_error_results(self):
        # Source-level wiring check (test_dgn376 idiom): the finalize seat must
        # skip the register guard entirely on is_error results (DGN-686 split;
        # supersedes the DGN-429 lang_check=not msg.is_error form) so English
        # error descriptions are never flagged as language slips.
        import inspect

        src = inspect.getsource(sdk_bridge)
        self.assertRegex(
            src,
            r"if not msg\.is_error:\s*"
            r"result_text = _register_guard\(_scaffold_guard\(result_text\)\)"
            r"\s*else:\s*"
            r"result_text = _scaffold_guard\(result_text\)",
        )


class OutputLangPromptTest(unittest.TestCase):
    def test_ko_locale_prompt_names_korean(self):
        with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", True):
            with patch.object(sdk_bridge.config, "locale", "ko"):
                prompt = sdk_bridge._compose_system_prompt()
        self.assertTrue(prompt.startswith(messages.SYSTEM_PROMPT))
        self.assertIn("## Output Language", prompt)
        self.assertIn("configured language is Korean", prompt)
        self.assertNotIn("{language}", prompt)

    def test_en_locale_prompt_names_english(self):
        with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", True):
            with patch.object(sdk_bridge.config, "locale", "en"):
                prompt = sdk_bridge._compose_system_prompt()
        self.assertIn("configured language is English", prompt)

    def test_gate_off_returns_base_fragment_unchanged(self):
        with patch.object(sdk_bridge, "OUTPUT_LANG_GUARD", False):
            self.assertEqual(
                messages.SYSTEM_PROMPT, sdk_bridge._compose_system_prompt()
            )

    def test_sdk_options_use_composed_prompt(self):
        # Wiring check: the SDK options seat consumes _compose_system_prompt()
        # instead of the bare messages.SYSTEM_PROMPT constant.
        import inspect

        src = inspect.getsource(sdk_bridge)
        self.assertRegex(
            src, r"\"system_prompt\":\s*_compose_system_prompt\(\)"
        )
        self.assertNotRegex(
            src, r"\"system_prompt\":\s*messages\.SYSTEM_PROMPT"
        )


if __name__ == "__main__":
    unittest.main()
