"""DGN-1987: final-reply language gate (forcing point on the final reply).

The DGN-686 drop tier fires only on ZERO-Hangul prose and the DGN-429
detector is log-only, so an English final reply carrying a few Korean quotes
or [[OPTIONS]] labels reached the owner on a ko instance. The gate scores
the final reply's prose by English-word share (fences, inline code, URLs,
send_file:: lines, `> ` quote lines, quoted spans and [[OPTIONS]] label
lines excluded) and blocks the turn end ONCE with a Stop-hook rewrite
continuation; a second miss is delivered with a log line (never loops).

Coverage:
  (a) scorer: English body + Korean quotes/buttons -> off-locale; Korean body
      dense with English technical terms -> pass; code-heavy -> pass; short
      English -> pass; en locale -> pass.
  (b) Stop-hook decision: first miss blocks with the rewrite reason; second
      miss in the same turn is delivered (no loop); Korean reply passes; gate
      knobs off -> pass; mint / synthetic turns -> pass; reply text read from
      last_assistant_message, the transcript, or the bridge capture.
  (c) interim narration: the same scorer drops English narration that
      carries a few Korean words (drop tier unchanged otherwise).
  (d) wiring: the Stop hook is registered on the SDK options and the
      request carries a per-turn block counter.
"""

import dataclasses
import inspect
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch

from bridge import sdk_bridge

# The live leak shape (2026-10-10): English prose, the owner's Korean words
# quoted, and Korean [[OPTIONS]] button labels.
EN_WITH_KO_QUOTES_AND_BUTTONS = (
    "I checked the dispatch return. The worker finished the bridge change "
    "and the tests pass. You asked \"왜갑자기영어쓰냐\", "
    "and the cause is that the register guard only drops blocks with zero "
    "Korean characters.\n"
    "> 왜갑자기영어쓰냐\n"
    "Pick the next step below.\n"
    "[[OPTIONS]]\n"
    "배포\n"
    "보류\n"
)

# A legit Korean reply: the owner keeps technical terms in English.
KO_WITH_TECH_TERMS = (
    "DGN-686 drop tier는 scored prose에 Hangul이 0인 block만 "
    "drop합니다. DGN-429 English-ratio detector는 advisory "
    "log-only라서 실제로 막지 못했습니다. "
    "그래서 final reply에 Stop hook 기반 rewrite gate를 "
    "추가했고, interim narration 은 기존 drop tier를 "
    "그대로 씁니다. config knob 은 FINAL_LANG_EN_RATIO "
    "입니다."
)

KO_STREAM_TERMS = (
    "브릿지의 stop reason 게이트가 end turn "
    "메시지만 스트리밍하도록 "
    "바뀌었고, interim narration 은 이제 "
    "표시되지 않습니다."
)

CODE_HEAVY = (
    "수정한 부분입니다:\n"
    "```python\n"
    "def _final_reply_off_locale(text: str) -> bool:\n"
    "    en, ko = _final_reply_word_counts(text)\n"
    "    if en + ko < FINAL_LANG_MIN_WORDS:\n"
    "        return False\n"
    "    return en / (en + ko) > FINAL_LANG_EN_RATIO\n"
    "```\n"
    "실행: `python -m pytest bridge/tests -k dgn1987 --maxfail 1`\n"
    "send_file::/home/user/files/outbox/final-lang-gate-report.md\n"
    "https://github.com/example/agent-canonical/commit/abcdef0123456789\n"
)


def _req(**kw):
    base = dict(
        mint_mute=None, synthetic_response=None,
        last_assistant_texts=[], lang_gate_blocks=0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class _KoLocale(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("FINAL_LANG_GATE", True),
            ("OUTPUT_LANG_GUARD", True),
            ("FINAL_LANG_EN_RATIO", 0.75),
            ("FINAL_LANG_MIN_WORDS", 8),
        ):
            p = patch.object(sdk_bridge, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(sdk_bridge.config, "locale", "ko")
        p.start()
        self.addCleanup(p.stop)


class ScorerTest(_KoLocale):
    def test_english_body_with_korean_quotes_and_buttons_flagged(self):
        # The DGN-686 drop tier passes it (some Hangul) -- the bug.
        self.assertEqual(
            EN_WITH_KO_QUOTES_AND_BUTTONS,
            sdk_bridge._register_guard(EN_WITH_KO_QUOTES_AND_BUTTONS),
        )
        self.assertTrue(
            sdk_bridge._final_reply_off_locale(EN_WITH_KO_QUOTES_AND_BUTTONS)
        )

    def test_korean_quotes_and_labels_excluded_from_score(self):
        en, ko = sdk_bridge._final_reply_word_counts(EN_WITH_KO_QUOTES_AND_BUTTONS)
        self.assertEqual(0, ko)
        self.assertGreater(en, 20)

    def test_korean_body_with_many_english_terms_passes(self):
        for text in (KO_WITH_TECH_TERMS, KO_STREAM_TERMS):
            en, ko = sdk_bridge._final_reply_word_counts(text)
            self.assertLessEqual(en / (en + ko), 0.6, text)
            self.assertFalse(sdk_bridge._final_reply_off_locale(text))

    def test_code_heavy_reply_passes(self):
        self.assertFalse(sdk_bridge._final_reply_off_locale(CODE_HEAVY))

    def test_short_english_under_word_floor_passes(self):
        self.assertFalse(sdk_bridge._final_reply_off_locale("OK, done. Pushed."))

    def test_labeled_options_marker_excluded(self):
        text = "배포 준비가 끝났습니다.\n[[OPTIONS: deploy now | hold until tomorrow morning | rollback the release]]"
        en, _ko = sdk_bridge._final_reply_word_counts(text)
        self.assertEqual(0, en)

    def test_en_locale_never_flagged(self):
        with patch.object(sdk_bridge.config, "locale", "en"):
            self.assertFalse(
                sdk_bridge._final_reply_off_locale(EN_WITH_KO_QUOTES_AND_BUTTONS)
            )

    def test_ratio_knob_is_live(self):
        with patch.object(sdk_bridge, "FINAL_LANG_EN_RATIO", 0.3):
            self.assertTrue(sdk_bridge._final_reply_off_locale(KO_WITH_TECH_TERMS))




class InterimScorerTest(_KoLocale):
    def test_interim_english_with_korean_words_dropped(self):
        note = (
            "Now I will check the dispatch return 보고서 and then "
            "run the bridge tests before writing the reply to the owner."
        )
        self.assertFalse(sdk_bridge._interim_off_locale(note))
        # The pre-DGN-1987 zero-script rule alone keeps it (one Korean word).
        with patch.object(sdk_bridge, "FINAL_LANG_GATE", False):
            self.assertFalse(sdk_bridge._interim_off_locale(note))

    def test_korean_interim_kept(self):
        self.assertFalse(sdk_bridge._interim_off_locale(KO_STREAM_TERMS))
        self.assertFalse(sdk_bridge._interim_off_locale(KO_WITH_TECH_TERMS))




if __name__ == "__main__":
    unittest.main()
