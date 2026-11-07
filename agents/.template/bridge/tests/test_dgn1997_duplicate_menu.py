"""DGN-1997 (v2.7): only an IDENTICAL repeated menu is consolidated.

Owner 2026-10-11: menus a,b and A,B were merged into one keyboard (abAB) and
pulled away from their questions. Now different menus keep their own buttons
on their own messages; only the SAME question (identical button set AND
identical normalized body) posted again is kept as one live copy, sent as a Telegram reply to the original question message with a
pointer line, and the older duplicate's buttons are removed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_mod
from bridge.i18n import en, ko
from bridge.options import LiveMenuRegistry, menu_body_key, menu_labels_key

MENU_A = "Ship order?\n\n1. alpha\n2. beta\n[[OPTIONS]]\n"
MENU_B = "Which region?\n\n1. Seoul\n2. Tokyo\n[[OPTIONS]]\n"
CHAT = 42


class _Harness:
    def __init__(self):
        self.bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        self.bot.application = MagicMock()
        self.bot._send_file_paths = AsyncMock()
        tg = self.tg = self.bot.application.bot
        self.next_id = 500
        self.sends = []
        self.refuse_reply = False

        async def _send(chat_id, text, **kw):
            if self.refuse_reply and kw.get("reply_parameters") is not None:
                raise RuntimeError("Bad Request: message to be replied not found")
            self.next_id += 1
            self.sends.append(SimpleNamespace(text=text, message_id=self.next_id, **kw))
            return SimpleNamespace(message_id=self.next_id)

        tg.send_message = AsyncMock(side_effect=_send)
        tg.edit_message_reply_markup = AsyncMock()
        tg.edit_message_text = AsyncMock()

    def menu(self, content, carrier_id):
        asyncio.run(self.bot._send_content_artifacts(
            CHAT, content, force_options=True, body=content, carrier_id=carrier_id,
        ))

    def markup_calls(self):
        return [(c.kwargs["message_id"], c.kwargs["reply_markup"] is not None)
                for c in self.tg.edit_message_reply_markup.await_args_list]


def test_labels_key_ignores_markup_space_and_case():
    assert menu_labels_key(["**Ship** now", "wait"]) == menu_labels_key(["ship  now", "WAIT"])
    assert menu_labels_key(["a", "b"]) != menu_labels_key(["b", "a"])
    assert menu_labels_key([]) == ()


def test_body_key_ignores_markup_space_and_case():
    assert menu_body_key("**Ship** order?\n\n1. alpha") == menu_body_key("ship  order?\n1. ALPHA")
    assert menu_body_key("Ship order?") != menu_body_key("Ship invoice?")
    assert menu_body_key(None) == menu_body_key("  \n") == ""


def test_registry_tracks_every_live_menu_and_finds_identical_questions():
    reg = LiveMenuRegistry()
    reg.add(1, 10, ["a", "b"], body_key="q1")
    reg.add(1, 11, ["A2", "B2"], body_key="q2")
    reg.add(1, 12, ["a", "b"], body_key="q3")  # same labels, other question
    reg.add(2, 50, ["a", "b"], body_key="q1")
    assert reg.live(1) == [10, 11, 12]  # nothing retired
    assert [d["message_id"] for d in reg.duplicates(1, ["a", "b"], "q1")] == [10]
    assert reg.duplicates(1, ["a", "c"], "q1") == []
    assert reg.duplicates(1, ["a", "b"], "q9") == []
    assert reg.duplicates(1, ["a", "b"]) == []  # no body key -> doubt -> none
    assert reg.duplicates(1, [], "q1") == []
    reg.answered(1, 10)
    assert reg.duplicates(1, ["a", "b"], "q1") == []  # answered -> not live


def test_different_menus_both_stay_tappable_on_their_own_messages():
    h = _Harness()
    h.menu(MENU_A, 100)
    h.menu(MENU_B, 200)
    # Each keyboard attached to its own body; nothing removed, no extra send.
    assert h.markup_calls() == [(100, True), (200, True)]
    h.tg.send_message.assert_not_awaited()
    assert h.bot._live_menu_registry().live(CHAT) == [100, 200]
    # No merged keyboard: each message carries only its own two buttons.
    kbs = [c.kwargs["reply_markup"] for c in h.tg.edit_message_reply_markup.await_args_list]
    texts = [[b.text for row in kb.inline_keyboard for b in row] for kb in kbs]
    assert texts == [["1. alpha", "2. beta"], ["1. Seoul", "2. Tokyo"]]


def test_reposted_menu_becomes_one_live_reply_copy_of_the_original():
    h = _Harness()
    h.menu(MENU_A, 100)
    h.menu(MENU_B, 200)
    h.menu(MENU_A, 300)
    # One live copy: a reply to the ORIGINAL A message with the pointer line.
    assert len(h.sends) == 1
    copy = h.sends[0]
    assert copy.text == messages.OPTIONS_DUPLICATE_POINTER
    assert copy.reply_parameters.message_id == 100
    assert copy.reply_parameters.allow_sending_without_reply is True
    assert [b.text for row in copy.reply_markup.inline_keyboard for b in row] == [
        "1. alpha", "2. beta"]
    # The re-posted body got no keyboard; the older A lost its buttons; B
    # was never touched.
    assert h.markup_calls() == [(100, True), (200, True), (100, False)]
    assert h.bot._live_menu_registry().live(CHAT) == [200, copy.message_id]
    # The bubble record points the copy back at its origin (Why net).
    rows = sdk_mod._read_options_bubbles()
    assert rows[-1]["message_id"] == copy.message_id
    assert rows[-1]["carried_from"] == 100


def test_third_repost_still_replies_to_the_first_original():
    h = _Harness()
    h.menu(MENU_A, 100)
    h.menu(MENU_A, 200)
    first = h.sends[-1].message_id
    h.menu(MENU_A, 300)
    assert h.sends[-1].reply_parameters.message_id == 100
    assert h.markup_calls()[-1] == (first, False)
    assert h.bot._live_menu_registry().live(CHAT) == [h.sends[-1].message_id]


def test_answered_menu_is_not_consolidated():
    h = _Harness()
    h.menu(MENU_A, 100)
    h.bot._live_menu_registry().answered(CHAT, 100)  # the owner tapped it
    h.menu(MENU_A, 200)
    h.tg.send_message.assert_not_awaited()
    assert h.markup_calls() == [(100, True), (200, True)]


def test_refused_reply_link_sends_the_copy_unlinked():
    h = _Harness()
    h.menu(MENU_A, 100)
    h.refuse_reply = True
    h.menu(MENU_A, 200)
    assert len(h.sends) == 1
    assert getattr(h.sends[0], "reply_parameters", None) is None
    assert h.markup_calls()[-1] == (100, False)


def test_pointer_copy_is_a_pending_owner_confirm_draft():
    assert ko.STRINGS["options_duplicate_pointer"] == "여기서 확인해 주세요"
    assert en.STRINGS["options_duplicate_pointer"]
    for mod in (en, ko):
        src = open(mod.__file__, encoding="utf-8").read()
        i = src.index('"options_duplicate_pointer"')
        assert "PENDING_OWNER_CONFIRM" in src[max(0, i - 300):i]


# Review fix: generic labels repeat across DIFFERENT questions (real today:
# "1. confirm as is / 2. fix wording" on two copy reviews). Labels alone must
# never consolidate -- a tap would answer the wrong question.
REVIEW_1 = "Copy review: onboarding welcome text.\n\n1. confirm as is\n2. fix wording\n[[OPTIONS]]\n"
REVIEW_2 = "Copy review: retro cadence proposal.\n\n1. confirm as is\n2. fix wording\n[[OPTIONS]]\n"


def test_same_labels_different_question_stay_two_separate_live_menus():
    h = _Harness()
    h.menu(REVIEW_1, 100)
    h.menu(REVIEW_2, 200)
    h.tg.send_message.assert_not_awaited()  # no reply-copy
    assert h.markup_calls() == [(100, True), (200, True)]  # old buttons untouched
    assert h.bot._live_menu_registry().live(CHAT) == [100, 200]


def test_same_labels_same_question_reformatted_is_consolidated():
    h = _Harness()
    h.menu(REVIEW_1, 100)
    h.menu(REVIEW_1.replace("Copy review:", "**Copy  review:**"), 200)
    assert len(h.sends) == 1
    assert h.sends[0].reply_parameters.message_id == 100
    assert h.markup_calls() == [(100, True), (100, False)]
    assert h.bot._live_menu_registry().live(CHAT) == [h.sends[0].message_id]


def test_shared_ledger_id_with_reworded_body_is_not_consolidated(monkeypatch):
    """The DGN-1952 ledger allocates a fresh id per menu turn and its pending
    pointer matches on labels only: a shared id is a stale pointer, not the
    same decision. No consolidation on it."""
    monkeypatch.setattr(
        bot_mod.TelegramBot, "_decision_ledger_pending_id",
        staticmethod(lambda labels, body=None: "D-0042"),
    )
    h = _Harness()
    h.menu(REVIEW_1, 100)
    h.menu(REVIEW_2, 200)
    h.tg.send_message.assert_not_awaited()
    assert h.markup_calls() == [(100, True), (200, True)]


def test_carried_menu_keeps_its_question_body_key():
    reg = LiveMenuRegistry()
    reg.add(1, 10, ["a", "b"], body_key="q1")
    assert reg.body_key(1, 10) == "q1"
    assert reg.body_key(1, 99) == ""
