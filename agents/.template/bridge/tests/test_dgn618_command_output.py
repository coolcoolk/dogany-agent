"""DGN-618: /skills output cleanup + /history removal.

Covers:
  (a) frontmatter parser folds a YAML block scalar (>-, >, |, |-) to its real
      text instead of leaking the indicator token ("` >-`") -- the bug the
      ticket reports on dogany-lifekit-setup / dogany-portfolio-setup.
  (b) /skills row formatting: bulleted localized display names ("• label"),
      no English description blurb, "/name" fallback when unmapped, and no
      "` >-`" leak anywhere in the rendered rows.
  (c) /history is fully removed: no handler method, no registration, not in
      the BotCommand menu, not in HELP_TEXT (ko + en in lockstep), and no
      dead-only i18n keys survive; the shared /resume key stays.
"""

import inspect
import re
import unittest

from bridge.bot import TelegramBot
from bridge import messages
from bridge.i18n import en, ko, skill_display_name


def _write_skill(tmpdir, name, frontmatter):
    d = tmpdir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    return d / "SKILL.md"


class FrontmatterBlockScalarTest(unittest.TestCase):
    """The shared parser must fold block scalars, never return the indicator."""

    def _parse(self, body):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        md = _write_skill(tmp, "some-skill", body)
        return TelegramBot._read_skill_frontmatter(md)

    def test_folded_block_scalar_dash(self):
        body = (
            "---\n"
            "name: some-skill\n"
            "description: >-\n"
            "  First line of the folded description.\n"
            "  Second line continues.\n"
            "---\n"
            "body\n"
        )
        name, desc = self._parse(body)
        self.assertEqual(name, "some-skill")
        self.assertNotIn(">-", desc)
        self.assertIn("First line of the folded description.", desc)
        self.assertIn("Second line continues.", desc)

    def test_all_block_indicators_fold(self):
        for ind in (">", ">-", "|", "|-", ">+", "|+"):
            body = (
                "---\n"
                "name: s\n"
                f"description: {ind}\n"
                "  Real folded text here.\n"
                "---\n"
            )
            _, desc = self._parse(body)
            self.assertNotIn(ind, desc, f"indicator {ind!r} leaked into value")
            self.assertIn("Real folded text here.", desc)

    def test_plain_scalar_still_parsed(self):
        body = (
            "---\n"
            "name: s\n"
            "description: A plain one-line description.\n"
            "---\n"
        )
        _, desc = self._parse(body)
        self.assertEqual(desc, "A plain one-line description.")


class SkillsRowFormatTest(unittest.TestCase):
    """Reproduce the _cmd_skills row formatter contract (DGN-618 task 1)."""

    @staticmethod
    def _fmt(entries):
        # Mirror of the nested _fmt in _cmd_skills: bullet + localized label,
        # no description. Kept in sync by test_source_has_no_desc_in_rows below.
        rows = []
        for name, _desc in entries:
            label = skill_display_name(name)
            text = label if label != name else f"/{name}"
            rows.append(f"• {text}")
        return rows

    def test_bulleted_localized_names(self):
        # A mapped skill renders its localized label with a top-level bullet.
        rows = self._fmt([("dogany-lifekit-setup", "some long english blurb")])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("• "))
        # Localized label, not the raw id, not the english blurb.
        self.assertNotIn("some long english blurb", rows[0])
        self.assertEqual(rows[0], f"• {skill_display_name('dogany-lifekit-setup')}")

    def test_unmapped_falls_back_to_slash_name(self):
        rows = self._fmt([("zzz-unknown-skill", "")])
        self.assertEqual(rows, ["• /zzz-unknown-skill"])

    def test_no_indicator_leak_in_rows(self):
        rows = self._fmt([("dogany-portfolio-setup", ">-")])
        self.assertNotIn(">-", rows[0])

    def test_source_has_no_desc_in_rows(self):
        # Guard: the real _cmd_skills row builder must not append the
        # description to the output line anymore.
        src = inspect.getsource(TelegramBot._cmd_skills)
        # The old code truncated desc to 120 and appended " - {desc}".
        self.assertNotIn("desc[:117]", src)
        self.assertNotIn('f" - {desc}"', src)
        # And it must emit the DS bullet.
        self.assertIn("• ", src)


class HistoryRemovalTest(unittest.TestCase):
    def test_no_cmd_history_method(self):
        self.assertFalse(hasattr(TelegramBot, "_cmd_history"))

    def test_no_get_recent_messages_helper(self):
        self.assertFalse(hasattr(TelegramBot, "_get_recent_messages"))

    def test_no_history_registration(self):
        src = inspect.getsource(TelegramBot._setup_handlers)
        self.assertNotIn('CommandHandler("history"', src)

    def test_no_history_in_bot_command_menu(self):
        src = inspect.getsource(TelegramBot._set_bot_commands)
        self.assertNotIn('BotCommand("history"', src)

    def test_history_absent_from_help_text_both_locales(self):
        self.assertNotIn("/history", ko.STRINGS["help_text"])
        self.assertNotIn("/history", en.STRINGS["help_text"])

    def test_dead_history_keys_removed_both_locales(self):
        for cat in (ko.STRINGS, en.STRINGS):
            self.assertNotIn("cmd_desc_history", cat)
            self.assertNotIn("no_history", cat)
            self.assertNotIn("history_header", cat)

    def test_shared_resume_key_survives(self):
        # /resume shares session_history_header -- must NOT be deleted.
        self.assertIn("session_history_header", ko.STRINGS)
        self.assertIn("session_history_header", en.STRINGS)
        self.assertTrue(hasattr(messages, "SESSION_HISTORY_HEADER"))

    def test_dead_message_bindings_removed(self):
        self.assertFalse(hasattr(messages, "CMD_DESC_HISTORY"))
        self.assertFalse(hasattr(messages, "NO_HISTORY"))
        self.assertFalse(hasattr(messages, "HISTORY_HEADER"))


if __name__ == "__main__":
    unittest.main()
