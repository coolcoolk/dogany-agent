"""DGN-1958: an open decision is carried to the newest message, as a
Telegram reply to its original.

A decision menu posted once and then buried under ~20 later replies that
said "waiting on the buttons above" (user, 2026-10-09).  The bridge now
tracks the chat's live menu (its newest options bubble, unanswered) and,
once that menu is no longer the newest bot message, re-renders it as the
closing block of the next reply that has no menu of its own -- sent with
reply_parameters pointing at the ORIGINAL decision bubble.  The bubble it
was carried from loses its buttons (one live menu per chat).

test-lane: fast
test-tier: 1
component: bridge decision carry
promise: a carried menu never shows a NO_PUSH sentinel as its question
promise: a stored menu whose question is only a sentinel is settled on read and never carried
promise: a sentinel-only body records no sentinel question
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("telegram") is None:
    sys.modules.setdefault("telegram", MagicMock())

import bridge.bot as bot_mod  # noqa: E402
import bridge.sdk_bridge as sdk_mod  # noqa: E402
from bridge.bot import TelegramBot  # noqa: E402
from bridge.options import _number_handle_label, has_options_marker  # noqa: E402
from bridge.tests._hostroot import host_file, skip_without_host  # noqa: E402

MENU = "Which one should we ship first?\n1. Alpha\n2. Beta\n[[OPTIONS]]"
CHAT = 42

UPS = host_file("routines/lib/why_net/ups.py").parent


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "options-bubbles.json"
        for target in (
            patch.object(sdk_mod, "options_bubbles_path", lambda: self.path),
            patch.object(bot_mod, "PROJECT_ROOT", Path(self.tmp.name)),
            patch.object(bot_mod, "DECISION_CARRY", True),
            patch.object(bot_mod, "MENU_FOLLOW", True),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.next_id = 100
        self.sends = []
        self.bot = TelegramBot.__new__(TelegramBot)
        self.bot.application = MagicMock()
        self.bot.application.bot.send_message = AsyncMock(side_effect=self._send)
        self.bot.application.bot.edit_message_reply_markup = AsyncMock()
        self.bot._send_file_paths = AsyncMock()
        self.refuse_reply = False

    async def _send(self, chat_id, text, **kwargs):
        if self.refuse_reply and kwargs.get("reply_parameters") is not None:
            raise RuntimeError("Bad Request: message to be replied not found")
        self.next_id += 1
        self.sends.append(SimpleNamespace(chat_id=chat_id, text=text,
                                          message_id=self.next_id, **kwargs))
        return SimpleNamespace(message_id=self.next_id)

    def rows(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def reply(self, content, carrier_id=None):
        """One final reply: the body went out as `carrier_id`, then the
        shared artifact tail runs (the seat every send path ends in)."""
        from bridge.options import strip_consumed_options, strip_options_marker
        force = has_options_marker(content)
        display, _ = strip_options_marker(content)
        options = None
        if force:
            display, options = strip_consumed_options(display)
        if carrier_id is None:
            self.next_id += 1
            carrier_id = self.next_id
        self.bot._attach_keyboard = AsyncMock(return_value=True)
        asyncio.run(self.bot._send_content_artifacts(
            CHAT, content, force, options=options or None,
            body=display, carrier_id=carrier_id))
        return carrier_id

    def carries(self):
        return [s for s in self.sends if s.reply_markup is not None]


class CarryTest(_Tmp):
    def test_menu_then_two_plain_replies_carries_on_the_second(self):
        menu_id = self.reply(MENU)
        [row] = self.rows()
        self.assertEqual(row["message_id"], menu_id)
        self.assertEqual(row["menu"]["labels"], ["Alpha", "Beta"])
        self.assertEqual(row["menu"]["question"], "Which one should we ship first?")

        self.reply("Here is the context you asked for.")
        self.assertEqual(self.carries(), [])  # the menu was still the newest

        self.reply("And one more detail.")
        [carry] = self.carries()
        self.assertEqual(carry.reply_parameters.message_id, menu_id)
        self.assertTrue(carry.reply_parameters.allow_sending_without_reply)
        self.assertEqual(
            carry.text,
            "Which one should we ship first?\n\n1. Alpha\n2. Beta")
        buttons = [b.text for r in carry.reply_markup.inline_keyboard for b in r]
        self.assertEqual(buttons, ["1. Alpha", "2. Beta"])
        # old buttons retired: one live menu per chat
        self.bot.application.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=menu_id, reply_markup=None)
        live = sdk_mod.live_decision_menu(CHAT)
        self.assertEqual(live["message_id"], carry.message_id)
        self.assertEqual(live["carried_from"], menu_id)

    def test_a_decision_is_carried_only_once(self):
        # FDGN-615: one lift while buried; the carried copy stays put.
        menu_id = self.reply(MENU)
        for _ in range(6):
            self.reply("plain")
        [carry] = self.carries()
        self.assertEqual(carry.reply_parameters.message_id, menu_id)
        live = sdk_mod.live_decision_menu(CHAT)
        self.assertEqual(live["message_id"], carry.message_id)
        self.bot.application.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=menu_id, reply_markup=None)

    def test_a_stale_decision_is_never_carried(self):
        # FDGN-615: a question older than DECISION_CARRY_MAX_AGE_S stays on
        # its own bubble with its buttons.
        menu_id = self.reply(MENU)
        rows = self.rows()
        rows[0]["ts"] -= bot_mod.DECISION_CARRY_MAX_AGE_S + 1
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        for _ in range(3):
            self.reply("plain")
        self.assertEqual(self.carries(), [])
        self.bot.application.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(sdk_mod.live_decision_menu(CHAT)["message_id"], menu_id)

    def test_a_fresh_decision_under_the_limit_is_carried(self):
        self.reply(MENU)
        rows = self.rows()
        rows[0]["ts"] -= bot_mod.DECISION_CARRY_MAX_AGE_S - 60
        self.path.write_text(json.dumps(rows), encoding="utf-8")
        self.reply("plain")
        self.reply("plain")
        self.assertEqual(len(self.carries()), 1)

    def test_original_deleted_carries_without_the_reply_link(self):
        self.reply(MENU)
        self.reply("plain one")
        self.refuse_reply = True
        self.reply("plain two")
        [carry] = self.carries()
        self.assertIsNone(getattr(carry, "reply_parameters", None))
        self.assertEqual(sdk_mod.live_decision_menu(CHAT)["message_id"],
                         carry.message_id)

    def test_reply_with_its_own_menu_does_not_carry(self):
        self.reply(MENU)
        self.reply("plain")
        new_id = self.reply("Now this?\n1. Yes\n2. No\n[[OPTIONS]]")
        self.assertEqual(self.carries(), [])
        live = sdk_mod.live_decision_menu(CHAT)
        self.assertEqual((live["message_id"], live["menu"]["labels"]),
                         (new_id, ["Yes", "No"]))

    def test_tapped_menu_does_not_carry(self):
        menu_id = self.reply(MENU)
        TelegramBot._clear_open_decision(CHAT, message_id=menu_id)
        self.reply("plain")
        self.reply("plain")
        self.assertEqual(self.carries(), [])
        self.assertIsNone(sdk_mod.live_decision_menu(CHAT))

    def test_tap_on_the_carried_bubble_answers_it(self):
        self.reply(MENU)
        self.reply("plain")
        self.reply("plain")
        [carry] = self.carries()
        TelegramBot._clear_open_decision(CHAT, message_id=carry.message_id)
        self.assertIsNone(sdk_mod.live_decision_menu(CHAT))

    def test_typed_number_or_label_answers(self):
        for answer in ("2", "1. Alpha", "beta", _number_handle_label(2)):
            self.path.unlink(missing_ok=True)
            self.reply(MENU)
            TelegramBot._clear_open_decision(CHAT, text=answer)
            self.assertIsNone(sdk_mod.live_decision_menu(CHAT), answer)
        for other in ("3", "tell me more about alpha first"):
            self.path.unlink(missing_ok=True)
            self.reply(MENU)
            TelegramBot._clear_open_decision(CHAT, text=other)
            self.assertIsNotNone(sdk_mod.live_decision_menu(CHAT), other)

    def test_fenced_example_menu_is_never_tracked(self):
        self.reply("An example of the grammar:\n```\nPick\n1. A\n2. B\n"
                   "[[OPTIONS]]\n```\nThat is all.")
        self.reply("plain")
        self.reply("plain")
        self.assertEqual(self.carries(), [])
        self.assertIsNone(sdk_mod.live_decision_menu(CHAT))

    def test_other_chat_is_untouched(self):
        self.reply(MENU)
        for _ in range(2):
            asyncio.run(self.bot._send_content_artifacts(
                7, "plain", False, body="plain", carrier_id=999))
        self.assertEqual(self.carries(), [])

    def test_switch_off_never_carries(self):
        with patch.object(bot_mod, "DECISION_CARRY", False):
            self.reply(MENU)
            for _ in range(3):
                self.reply("plain")
        self.assertEqual(self.carries(), [])

    def test_menu_follow_off_never_reposts_an_unanswered_menu(self):
        with patch.object(bot_mod, "MENU_FOLLOW", False):
            menu_id = self.reply(MENU)
            self.reply("notice one")
            self.reply("notice two")
        self.assertEqual(self.carries(), [])
        self.bot.application.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(sdk_mod.live_decision_menu(CHAT)["message_id"], menu_id)

    def test_menu_follow_default_tracks_only_manifest(self):
        from bridge.config import menu_follow_default
        root = Path(self.tmp.name)
        self.assertFalse(menu_follow_default(root))
        manifest = root / "config/packs/agent_po.manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        self.assertTrue(menu_follow_default(root))

    def test_lead_in_when_the_question_line_is_unknown(self):
        self.reply("1. Alpha\n2. Beta\n[[OPTIONS]]")
        self.reply("plain")
        self.reply("plain")
        [carry] = self.carries()
        self.assertTrue(carry.text.startswith(bot_mod.messages.DECISION_CARRY_LEAD))

    def _seed(self, question, message_id=7):
        sdk_mod.publish_options_bubble(
            CHAT, message_id, ["1. a", "2. b"],
            menu={"question": question, "labels": ["a", "b"],
                  "answered": False, "covered": True})

    def test_sentinel_only_stored_question_is_settled_never_carried(self):
        self._seed("NO_PUSH")
        self.assertIsNone(sdk_mod.live_decision_menu(CHAT))
        self.assertTrue(self.rows()[-1]["menu"]["answered"])
        self.reply("plain")
        self.assertEqual(self.carries(), [])

    def test_send_refuses_a_sentinel_question_and_settles_it(self):
        self._seed("<b>NO_PUSH</b>")
        row = json.loads(self.path.read_text(encoding="utf-8"))[-1]
        self.assertFalse(asyncio.run(self.bot._send_decision_carry(CHAT, row)))
        self.assertEqual(self.sends, [])
        self.assertTrue(self.rows()[-1]["menu"]["answered"])

    def test_carry_text_drops_a_sentinel_line_of_the_question(self):
        self._seed("Ship it?\nNO_PUSH")
        self.reply("plain")
        [carry] = self.carries()
        self.assertEqual(carry.text, "Ship it?\n\n1. a\n2. b")

    def test_sentinel_only_body_records_no_sentinel_question(self):
        self.assertNotEqual(TelegramBot._carry_question("1. a\n2. b\nNO_PUSH"), "NO_PUSH")
        self.assertIsNone(TelegramBot._carry_question("1. a\n2. b\nNO_PUSH"))

    def test_upward_pointer_without_carry_is_logged(self):
        self.reply(MENU)
        with self.assertLogs(bot_mod.logger, level="INFO") as logs:
            self.reply("위 버튼에서 골라 주세요.")
        self.assertTrue(any("upward pointer without carry" in m for m in logs.output))
        with self.assertLogs(bot_mod.logger, level="INFO") as logs:
            self.reply("Still waiting above.")
        self.assertFalse(any("upward pointer" in m for m in logs.output))

    # ESTATE:BEGIN the ledger link is a canonical adapter; the public build has no ledger
    @unittest.skipUnless(*skip_without_host("routines/decision-ledger.py"))
    def test_ledger_entry_is_linked_when_configured(self):
        # Rework A: the bridge itself writes the row at send time.
        import shutil
        root = Path(self.tmp.name)
        shutil.copytree(host_file("routines"), root / "routines",
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        (root / "config").mkdir()
        (root / "config/agent.conf").write_text(
            "DECISION_LEDGER_PATH=worklog/_DECISIONS.md\n")
        self.reply(MENU)
        entry = sdk_mod.live_decision_menu(CHAT)["menu"]["ledger_id"]
        self.assertTrue(entry.startswith("dec-"))
        self.assertIn(entry, (root / "worklog/_DECISIONS.md").read_text())
    # ESTATE:END


@unittest.skipUnless(*skip_without_host("routines/lib/why_net/ups.py"))
class WhyNetOriginTest(_Tmp):
    """A tap on a carried bubble binds to the ask its origin carried."""

    def test_carried_bubble_resolves_to_its_origin(self):
        sys.path.insert(0, str(UPS.parent))
        try:
            from why_net import ups
        finally:
            sys.path.remove(str(UPS.parent))
        menu_id = self.reply(MENU)
        self.reply("plain")
        self.reply("plain")
        [carry] = self.carries()
        ctx = SimpleNamespace(bot_dir=str(self.path.parent))
        self.assertEqual(ups.bubble_origin(ctx, CHAT, carry.message_id), menu_id)
        self.assertEqual(ups.bubble_origin(ctx, CHAT, 5), 5)
        ask = {"issued_ts": 0, "shown": [{"label": "Alpha"}, {"label": "Beta"}]}
        self.assertEqual(ups.carrier_bubble(ctx, ask, CHAT), menu_id)


if __name__ == "__main__":
    unittest.main()
