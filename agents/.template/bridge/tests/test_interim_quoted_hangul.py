"""Interim locale gate judges script presence OUTSIDE quoted spans
(rehearsal 2026-10-05: an English step note quoting the owner's Korean
answer leaked because the quoted name supplied the only Hangul)."""
import unittest
from unittest.mock import patch

from bridge import sdk_bridge


def off(text, locale="ko"):
    with patch.object(sdk_bridge.config, "locale", locale):
        return sdk_bridge._interim_off_locale(text)


class InterimQuotedHangul(unittest.TestCase):
    def test_english_note_quoting_korean_is_off_locale(self):
        self.assertTrue(off('Name answer: "도스". Record it in the identity file, then send q2.'))
        self.assertTrue(off("Owner said “네”, moving on to q3."))

    def test_korean_prose_kept(self):
        self.assertFalse(off('이름을 "도스"로 저장했어요.'))
        self.assertFalse(off('"ALLOW" 버튼을 눌러 주세요.'))

    def test_en_locale_noop(self):
        self.assertFalse(off('Name answer: "x". Record it.', locale="en"))


if __name__ == "__main__":
    unittest.main()
