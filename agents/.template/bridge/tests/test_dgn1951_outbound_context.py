"""DGN-1951: an agent must know what it pushed.

Sends that bypass the live session (push script, routines, job notices,
relays) are recorded in the outbound ledger; the session never sees them.
On each owner turn the bridge prefixes the owner's text with those pushes
(this chat only, since the agent's previous turn ended, bounded), the full
text of a pushed bubble the owner replied to, and -- for a short reply to a
push that asked something -- the question being answered.

Covers:
  1. The reported sequence: a push ending in a question, then a one-word
     owner reply -> the turn text carries the push and "The user is
     answering: <question>".
  2. Reply-to a pushed bubble -> its full text verbatim, even when it is
     older than the window and longer than the per-push cut.
  3. Replies to this bot carry the quote even without a push ledger row.
     Explicit replies never infer an answer to an unrelated push.
  4. Bound + truncation: newest MAX_PUSHES, oldest first, an omitted count,
     long bodies cut with an ellipsis.
  5. Per-chat isolation: another chat's pushes and turn-end marks never mix.
  6. The turn-end mark: pushes sent before the agent's last turn ended are
     not shown again; the bridge marks it on every ResultMessage.
  7. Transcript readers cut the block off again (owner_region / clean).
  8. The bot hook: the composed text is what process_message receives.
  9. DGN-2132: an opt: button tap composes like a reply-to on the tapped
     bubble (pushed or in-session): bubble text verbatim + the answering
     line + the chosen label reach process_message.
 10. DGN-2239: a typed reply to an older in-session bubble survives a hard
     session rotation and reaches the new agent turn with its full quote.

test-lane: fast
test-tier: 1
component: bridge outbound context
promise: an opt: tap sends the tapped bubble text, the answering line and the label
promise: the tapped bubble rides even when the push ledger does not hold it
promise: a plain bot reply reaches the agent turn verbatim with a neutral heading
promise: explicit replies never answer an unrelated recent push question
promise: only this bot's replies are trusted, including during application re-init
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bridge import outbound_context as oc
from bridge.tests._hostroot import host_file, skip_without_host

CHAT = 777
OTHER_CHAT = 888
NOW = 1_800_000_000.0
# The owner's one-word reply ("commit", in the owner's language).
COMMIT = "커밋"
QUESTION_KO = (
    "이대로 커밋해도 될지"
)  # "whether it is fine to commit as is" -- no question mark


_KEEP_ENV = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "TZ", "PYTHONPATH",
                       "VIRTUAL_ENV", "SYSTEMROOT"})


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _isolate_env(case):
    # ESTATE:BEGIN isolate inherited estate settings as well as Git configuration
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG_", "DOGANY_", "DGN_"))}
    # ESTATE:ELSE
    # env = {k: v for k, v in os.environ.items()
    #        if not k.startswith("GIT_CONFIG_")}
    # ESTATE:END
    patcher = patch.dict(os.environ, env, clear=True)
    patcher.start()
    case.addCleanup(patcher.stop)


class _Root:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dgn1951-")
        self.data = Path(self.tmp.name) / ".telegram_bot"
        self.data.mkdir(parents=True)
        self.ledger = self.data / oc.LEDGER_FILENAME

    def push(self, body, ts, chat=CHAT, **extra):
        row = {"ts": _iso(ts), "chat_id": str(chat), "body": body, "audience": "owner"}
        row.update(extra)
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def compose(self, text, chat=CHAT, reply_to=None, now=NOW):
        return oc.compose(text, chat, self.data, reply_to=reply_to, now=now)

    def close(self):
        self.tmp.cleanup()


class OutboundContextTest(unittest.TestCase):
    def setUp(self):
        _isolate_env(self)
        self.r = _Root()
        oc.mark_turn_end(self.r.data, CHAT, NOW - 3600)

    def tearDown(self):
        self.r.close()

    # 1 ---------------------------------------------------------------------
    def test_push_question_then_short_reply_carries_push_and_question(self):
        body = ("<b>PO answer</b>\nQA passed on the checkout flow.\n"
                "Your call is needed: " + QUESTION_KO)
        self.r.push(body, NOW - 60)
        out = self.r.compose(COMMIT)
        self.assertTrue(out.startswith(oc.OPEN_MARK + "\n" + oc.HEADER))
        self.assertIn("PO answer\nQA passed on the checkout flow.", out)  # tags gone
        self.assertNotIn("<b>", out)
        self.assertIn(oc.ANSWERING.format(question="Your call is needed: " + QUESTION_KO), out)
        self.assertTrue(out.endswith(oc.CLOSE_MARK + "\n" + COMMIT))

    # FDGN-639 -------------------------------------------------------------
    def test_recovered_row_without_provenance_is_not_credited_to_model(self):
        self.r.push("Recovered tool result", NOW - 10)
        out = self.r.compose("continue")
        self.assertIn("provenance=unknown", out)
        self.assertNotIn("Messages you sent", out)

    def test_service_card_keeps_bridge_provenance_and_exact_body(self):
        body = "<b>Usage</b>\n5h: 42%\nWeekly: 73%"
        self.r.push(body, NOW - 10, source="bridge",
                    producer="bot:_cmd_usage")
        out = self.r.compose("thanks")
        self.assertIn("provenance=bridge-service:bot:_cmd_usage", out)
        self.assertIn("Usage\n5h: 42%\nWeekly: 73%", out)
        self.assertIn("do not rewrite it", out)

    def test_write_ack_relay_names_only_writer_supplied_effect(self):
        self.r.push("Saved 12", NOW - 10, source="service",
                    effect="set repetitions=12")
        out = self.r.compose("ok")
        self.assertIn("effect=set repetitions=12", out)
        self.assertNotIn("effect=Saved 12", out)

    def test_numeric_write_before_turn_is_later_state_not_duplicate_owner_input(self):
        self.r.push("12", NOW - 20, source="service",
                    effect="set repetitions=12")
        out = self.r.compose("show me the plan")
        self.assertIn("provenance=bridge-service; effect=set repetitions=12", out)
        self.assertTrue(out.endswith("show me the plan"))

    def test_newer_keyboard_stays_after_earlier_service_prose(self):
        self.r.push("Write completed.", NOW - 20, source="service")
        self.r.push("Choose the next step", NOW - 10, source="bridge",
                    producer="keyboard")
        out = self.r.compose("2")
        self.assertLess(out.index("Write completed."), out.index("Choose the next step"))
        self.assertIn("A newer keyboard/service item is later state", out)

    def test_question_mark_push_is_answered(self):
        self.r.push("Deploy is ready.\nShip it now?", NOW - 10)
        out = self.r.compose("yes")
        self.assertIn("The user is answering: Ship it now?", out)

    def test_long_reply_is_not_framed_as_answer(self):
        self.r.push("Ship it now?", NOW - 10)
        out = self.r.compose("x" * (oc.SHORT_REPLY_CHARS + 1))
        self.assertIn("Ship it now?", out)
        self.assertNotIn("The user is answering", out)

    def test_statement_push_registers_no_question(self):
        self.r.push("Backup finished.", NOW - 10)
        out = self.r.compose("ok")
        self.assertIn("Backup finished.", out)
        self.assertNotIn("The user is answering", out)

    # 2 ---------------------------------------------------------------------
    def test_reply_to_push_injects_full_bubble_verbatim(self):
        long_body = "Report line.\n" + ("detail " * 200).strip() + "\nMerge it?"
        # Older than the window AND the turn mark: only reply-to can surface it.
        self.r.push(long_body, NOW - 5 * 86400, message_id=4242)
        oc.mark_turn_end(self.r.data, CHAT, NOW - 30)
        shown = long_body  # Telegram hands back the plain text the owner saw
        out = self.r.compose(COMMIT, reply_to={
            "message_id": 4242, "text": shown, "from_bot": True})
        self.assertIn(oc.REPLY_HEAD + "\n" + shown + "\n", out)
        self.assertNotIn(oc.HEADER, out)  # nothing new in the window
        self.assertIn("The user is answering: Merge it?", out)

    def test_reply_to_push_matched_by_text_without_message_id(self):
        self.r.push("<i>Nightly sync</i> done.\nRetry the two failures?", NOW - 9 * 3600)
        oc.mark_turn_end(self.r.data, CHAT, NOW - 30)
        out = self.r.compose("go", reply_to={
            "message_id": 99, "text": "Nightly sync done.\nRetry the two failures?",
            "from_bot": True})
        self.assertIn(oc.REPLY_HEAD, out)
        self.assertIn("The user is answering: Retry the two failures?", out)

    # 3 ---------------------------------------------------------------------
    def test_no_push_no_injection(self):
        self.assertEqual(self.r.compose(COMMIT), COMMIT)

    def test_no_ledger_file_no_injection(self):
        self.r.ledger.unlink(missing_ok=True)
        self.assertEqual(self.r.compose("hello"), "hello")

    def test_reply_to_non_push_bubble_injects_full_quote(self):
        shown = "A normal in-session answer.\n" + "detail " * 200
        out = self.r.compose("why?", reply_to={
            "message_id": 5, "text": shown, "from_bot": True})
        self.assertEqual(out, "\n".join([
            oc.OPEN_MARK, oc.PLAIN_REPLY_HEAD, shown, oc.CLOSE_MARK, "why?"]))

    def test_explicit_replies_never_infer_unrelated_push_question(self):
        self.r.push("Unrelated release. Ship it now?", NOW - 10)
        for sender, shown in ((True, "A normal answer."), (True, None),
                              (True, "   "), (False, "Someone else's text.")):
            with self.subTest(sender=sender, shown=shown):
                out = self.r.compose("why?", reply_to={
                    "message_id": 5, "text": shown, "from_bot": sender})
                self.assertNotIn("The user is answering:", out)
        out = self.r.compose("yes", reply_to={
            "message_id": 5, "text": "Revisit the older topic?", "from_bot": True})
        self.assertIn("The user is answering: Revisit the older topic?", out)
        self.assertNotIn("The user is answering: Unrelated", out)

    def test_reply_to_push_without_telegram_text_uses_ledger(self):
        self.r.push("<b>Older report.</b>\nRetry it?", NOW - 5 * 86400, message_id=42)
        for shown in (None, "", " \n\t"):
            with self.subTest(shown=shown):
                out = self.r.compose("yes", reply_to={
                    "message_id": 42, "text": shown, "from_bot": True})
                self.assertEqual(out, "\n".join([
                    oc.OPEN_MARK, oc.REPLY_HEAD, "Older report.\nRetry it?",
                    "The user is answering: Retry it?", oc.CLOSE_MARK, "yes"]))

    def test_dgn2239_telegram_quote_takes_precedence_over_ledger_body(self):
        self.r.push("<b>Original bubble.</b>", NOW - 5 * 86400, message_id=42)
        shown = "  Edited <literal> & bubble.\n" + "detail " * 200
        out = self.r.compose("why?", reply_to={
            "message_id": 42, "text": shown, "from_bot": True})
        self.assertEqual(out, "\n".join([
            oc.OPEN_MARK, oc.REPLY_HEAD, shown, oc.CLOSE_MARK, "why?"]))

    def test_reply_to_owner_message_injects_nothing(self):
        self.r.push("Same text", NOW - 7200)  # before the mark: not in window
        out = self.r.compose("again", reply_to={
            "message_id": 6, "text": "Same text", "from_bot": False})
        self.assertEqual(out, "again")

    def test_edit_rows_and_blank_bodies_are_not_pushes(self):
        self.r.push("Progress 50%", NOW - 20, tag="edit", message_id="12")
        self.r.push("<b></b>  ", NOW - 10)
        self.assertEqual(self.r.compose("hi"), "hi")

    # 4 ---------------------------------------------------------------------
    def test_bound_and_truncation(self):
        for i in range(7):
            self.r.push("push %d" % i, NOW - 600 + i)
        self.r.push("L" * (oc.MAX_PUSH_CHARS + 50), NOW - 5)
        out = self.r.compose("hm")
        block = out[:out.index(oc.CLOSE_MARK)]
        self.assertIn(oc.OMITTED.format(n=3), block)
        for i in (0, 1, 2):
            self.assertNotIn("push %d\n" % i, block)
        order = [block.index("push %d" % i) for i in (3, 4, 5, 6)]
        self.assertEqual(order, sorted(order))  # oldest first, newest last
        self.assertIn("L" * oc.MAX_PUSH_CHARS + oc.ELLIPSIS, block)
        self.assertNotIn("L" * (oc.MAX_PUSH_CHARS + 1), block)
        self.assertGreater(block.rindex(oc.ELLIPSIS), order[-1])
        self.assertEqual(block.count(oc.ITEM_HEAD.split("{")[0]), oc.MAX_PUSHES)

    def test_items_carry_send_time(self):
        self.r.push("ping", NOW - 60)
        out = self.r.compose("pong")
        self.assertIn(oc.ITEM_HEAD.format(when=oc._when(NOW - 60)), out)

    # 5 ---------------------------------------------------------------------
    def test_per_chat_isolation(self):
        self.r.push("for the other chat?", NOW - 30, chat=OTHER_CHAT)
        self.assertEqual(self.r.compose(COMMIT), COMMIT)
        oc.mark_turn_end(self.r.data, OTHER_CHAT, NOW - 3600)
        out = self.r.compose("yes", chat=OTHER_CHAT)
        self.assertIn("for the other chat?", out)
        # a turn ending in one chat does not move the other's window
        self.r.push("mine", NOW - 20)
        oc.mark_turn_end(self.r.data, OTHER_CHAT, NOW - 1)
        self.assertIn("mine", self.r.compose("ok"))

    def test_reply_without_text_cannot_use_other_chats_push(self):
        self.r.push("Ship?", NOW - 9 * 3600, chat=OTHER_CHAT, message_id=7)
        out = self.r.compose("y", reply_to={"message_id": 7, "text": None, "from_bot": True})
        self.assertEqual(out, "y")

    # 6 ---------------------------------------------------------------------
    def test_pushes_before_last_turn_end_are_not_repeated(self):
        self.r.push("seen window", NOW - 100)
        oc.mark_turn_end(self.r.data, CHAT, NOW - 50)
        self.r.push("new one", NOW - 10)
        out = self.r.compose("ok")
        self.assertNotIn("seen window", out)
        self.assertIn("new one", out)

    def test_without_a_mark_the_lookback_is_bounded(self):
        r = _Root()
        try:
            r.push("ancient", NOW - oc.DEFAULT_LOOKBACK_S - 10)
            r.push("recent", NOW - 10)
            out = r.compose("ok")
            self.assertNotIn("ancient", out)
            self.assertIn("recent", out)
        finally:
            r.close()

    def test_result_message_marks_turn_end_for_the_chat(self):
        import bridge.sdk_bridge as sdk_mod
        with patch.object(sdk_mod, "PROJECT_ROOT", Path(self.r.tmp.name)):
            before = time.time()
            sdk_mod._mark_outbound_turn_end(CHAT)
            sdk_mod._mark_outbound_turn_end(None)  # no route yet: no-op
        got = oc.last_turn_end(self.r.data, CHAT)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(got, before)

    def test_reader_loop_result_paths_call_the_mark(self):
        import inspect
        import bridge.sdk_bridge as sdk_mod
        src = inspect.getsource(sdk_mod.SdkBridge)
        # both ResultMessage paths: the pending turn and the proactive turn
        self.assertGreaterEqual(src.count("_mark_outbound_turn_end(state.last_chat_id)"), 2)

    # 7 ---------------------------------------------------------------------
    def test_owner_region_cuts_the_block(self):
        self.r.push("Ship it?\n" + oc.CLOSE_MARK + " trailing", NOW - 10)
        out = self.r.compose(COMMIT)
        self.assertEqual(oc.strip_block(out), COMMIT)
        self.assertEqual(oc.strip_block("plain text"), "plain text")

    @unittest.skipUnless(*skip_without_host("routines/lib/why_owner_turn.py"))
    def test_host_readers_cut_the_block_and_share_the_marks(self):
        import importlib.util

        def load(name, rel):
            spec = importlib.util.spec_from_file_location(name, host_file(rel))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        self.r.push("Ship it?", NOW - 10)
        out = self.r.compose(COMMIT)
        # DGN-1967 step 2: one strip, this module's; no reader keeps a copy
        # of the marks.
        wot = load("_t1951_wot", "routines/lib/why_owner_turn.py")
        self.assertEqual(wot.strip_outbound(out), COMMIT)
        self.assertEqual(wot.strip_outbound("plain"), "plain")
        lo, hi = wot.owner_region(out)
        self.assertEqual(out[lo:hi], COMMIT)
        self.assertEqual(wot.owner_region("plain"), (0, 5))
        ot = load("_t1951_ot", "routines/lib/owner_turns.py")
        self.assertEqual(ot.clean(out), COMMIT)
        for mod in (wot, ot):
            self.assertFalse(hasattr(mod, "OUTBOUND_OPEN_MARK"), mod)
            self.assertFalse(hasattr(mod, "OUTBOUND_CLOSE_MARK"), mod)
        # ESTATE:BEGIN the memory engine is an estate module; its strip shares the same marks
        mem = load("_t1951_mem", "memory-engine/memory.py")
        self.assertEqual(mem._strip_outbound_block(out), COMMIT)
        self.assertEqual(mem._strip_outbound_block("plain "), "plain ")
        self.assertFalse(hasattr(mem, "OUTBOUND_OPEN_MARK"), mem)
        self.assertFalse(hasattr(mem, "OUTBOUND_CLOSE_MARK"), mem)
        # ESTATE:END


# 8 -------------------------------------------------------------------------
class BotHookTest(unittest.TestCase):
    def setUp(self):
        _isolate_env(self)
        self.r = _Root()

    def tearDown(self):
        self.r.close()

    def _bot(self):
        from bridge.bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.application = SimpleNamespace(bot=SimpleNamespace(id=1001))
        return bot

    def _message(self, reply=None):
        return SimpleNamespace(reply_to_message=reply)

    def test_with_outbound_context_reads_project_ledger(self):
        import bridge.bot as bot_mod
        now = time.time()
        oc.mark_turn_end(self.r.data, CHAT, now - 300)
        self.r.push("Ready to commit this?", now - 60)
        with patch.object(bot_mod, "PROJECT_ROOT", Path(self.r.tmp.name)):
            out = self._bot()._with_outbound_context(COMMIT, CHAT, self._message())
        self.assertIn("Ready to commit this?", out)
        self.assertIn("The user is answering: Ready to commit this?", out)
        self.assertTrue(out.endswith("\n" + COMMIT))

    def test_reply_to_info_only_trusts_this_bots_bubbles(self):
        bot = self._bot()
        mine = SimpleNamespace(message_id=5, text="t", caption=None,
                               from_user=SimpleNamespace(is_bot=True, id=1001))
        other_bot = SimpleNamespace(message_id=6, text="t", caption=None,
                                    from_user=SimpleNamespace(is_bot=True, id=2002))
        human = SimpleNamespace(message_id=7, text="t", caption=None,
                                from_user=SimpleNamespace(is_bot=False, id=1))
        self.assertTrue(bot._reply_to_info(self._message(mine))["from_bot"])
        self.assertFalse(bot._reply_to_info(self._message(other_bot))["from_bot"])
        self.assertFalse(bot._reply_to_info(self._message(human))["from_bot"])
        self.assertIsNone(bot._reply_to_info(self._message(None)))
        photo = SimpleNamespace(message_id=8, text=None, caption="cap",
                                from_user=SimpleNamespace(is_bot=True, id=1001))
        self.assertEqual(bot._reply_to_info(self._message(photo))["text"], "cap")

    def test_compose_failure_sends_text_unchanged(self):
        with patch.object(oc, "compose", side_effect=RuntimeError("boom")):
            out = self._bot()._with_outbound_context("hi", CHAT, self._message())
        self.assertEqual(out, "hi")

    def test_reply_identity_during_application_reinitialization(self):
        bot = self._bot()
        transport = bot.application.bot
        bot.application = None
        for sender_id, expected in ((1001, True), (2002, False)):
            reply = SimpleNamespace(message_id=5, text="Older topic.", caption=None,
                                    from_user=SimpleNamespace(is_bot=True, id=sender_id))
            message = self._message(reply)
            message.get_bot = lambda: transport
            self.assertIs(bot._reply_to_info(message)["from_bot"], expected)
        message.get_bot = lambda: None
        self.assertFalse(bot._reply_to_info(message)["from_bot"])

    def _reply_turn(self, body, *, push=False, sender_id=1001,
                    hard_rotate=False, owner_text="why?"):
        """Telegram JSON -> real turn handler -> SDK boundary -> reply delivery."""
        from telegram import Update
        import bridge.bot as bot_mod
        from bridge.sdk_bridge import ChatResponse
        bot = self._bot()
        bot.application.bot.send_chat_action = AsyncMock()
        now = time.time()
        oc.mark_turn_end(self.r.data, CHAT, now - 300)
        self.r.push("Unrelated release. Ship it now?", now - 10)
        if push:
            self.r.push(body, now - 5 * 86400, message_id=5)
        raw = {
            "update_id": 99,
            "message": {"message_id": 6, "date": int(now), "text": owner_text,
                        "chat": {"id": CHAT, "type": "private"},
                        "from": {"id": CHAT, "is_bot": False, "first_name": "Owner"},
                        "reply_to_message": {
                            "message_id": 5, "date": int(now - 5 * 86400),
                            "chat": {"id": CHAT, "type": "private"},
                            "from": {"id": sender_id, "is_bot": True, "first_name": "Bot"}}}}
        if body is not None:
            raw["message"]["reply_to_message"]["text"] = body
        update = Update.de_json(raw, bot.application.bot)
        response = ChatResponse(content="Fixture agent answer.")
        pm = AsyncMock(return_value=response)
        bot._finish_turn_with_auto_resume = AsyncMock(return_value=response)
        bot._save_session_id = AsyncMock()
        bot._reply_smart = AsyncMock()
        bot._permission_callback = None
        bot._proactive_push = None
        bot._runtime_active_sessions = {CHAT}
        session = {"session_id": None, "model": "m"}
        if hard_rotate:
            session.update({
                "session_id": "previous-session",
                bot_mod.CONTEXT_FILL_KEY: {
                    "session_id": "previous-session", "ratio": 0.60},
            })
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=session)
        sm.should_start_new_session = AsyncMock(return_value=False)
        sm.update_session = AsyncMock()
        sm.set_last_user_message_at = AsyncMock()
        with patch.object(bot_mod, "PROJECT_ROOT", Path(self.r.tmp.name)), \
             patch.object(bot_mod, "session_manager", sm), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.sdk_bridge, "turn_in_flight", return_value=False), \
             patch.object(bot_mod.model_state, "take_start_notice", return_value=None), \
             patch.object(bot_mod.limit_gate, "active_until", return_value=None), \
             patch.object(bot_mod.config, "session_rotate_hard_ratio", 0.55), \
             patch.object(bot_mod.config, "session_rotate_notice", False), \
             patch.object(type(bot), "_get_real_model", lambda s, x: "m"), \
             patch.object(type(bot), "_effective_session_id", lambda s, u, x: x.get("session_id")):
            asyncio.run(bot._process_user_message_text(update, CHAT, update.message.text))
        pm.assert_awaited_once()
        bot._reply_smart.assert_awaited_once()
        self.assertEqual(bot._reply_smart.await_args.args[:2],
                         (update.message, response.content))
        sent = pm.await_args.kwargs["user_message"]
        self.assertEqual(oc.strip_block(sent), owner_text)
        if hard_rotate:
            self.assertTrue(pm.await_args.kwargs["new_session"])
            self.assertIsNone(pm.await_args.kwargs["session_id"])
            sm.update_session.assert_awaited_once_with(CHAT, session)
            self.assertIsNone(session[bot_mod.CONTEXT_FILL_KEY])
            self.assertNotIn(CHAT, bot._runtime_active_sessions)
        return sent

    def test_dgn2239_typed_reply_keeps_older_quote_after_hard_rotation(self):
        body = "  Older in-session answer.\n<literal> & details\n" + "detail " * 200
        owner_text = "\ubc84\ud2bc\uc73c\ub85c \uc904\uac70\uc57c?"
        sent = self._reply_turn(body, hard_rotate=True, owner_text=owner_text)
        self.assertIsNone(oc.find_push([str(self.r.ledger)], CHAT, 5, body))
        self.assertIn(oc.PLAIN_REPLY_HEAD + "\n" + body + "\n", sent)
        self.assertNotIn("The user is answering:", sent)
        self.assertTrue(sent.endswith(oc.CLOSE_MARK + "\n" + owner_text))

    def test_plain_bot_reply_reaches_agent_turn_with_older_quote(self):
        body = "Older migration result.\nThe archive is retained for audit."
        sent = self._reply_turn(body)
        self.assertIn(oc.PLAIN_REPLY_HEAD + "\n" + body + "\n", sent)
        self.assertNotIn("The user is answering:", sent)

    def test_explicit_unquotable_reply_does_not_answer_recent_push(self):
        sent = self._reply_turn(None)
        self.assertNotIn("The user is answering:", sent)

    def test_reply_to_other_bot_does_not_quote_or_answer_recent_push(self):
        sent = self._reply_turn("Other bot's question?", sender_id=2002)
        self.assertNotIn("Other bot's question?", sent)
        self.assertNotIn("The user is answering:", sent)

    def test_push_reply_preserves_existing_context_rendering(self):
        body = "Older push report.\nRetry it?"
        sent = self._reply_turn(body, push=True)
        self.assertIn(oc.REPLY_HEAD + "\n" + body + "\n", sent)
        self.assertIn("The user is answering: Retry it?", sent)

    def test_process_message_receives_composed_text(self):
        import bridge.bot as bot_mod
        bot = self._bot()
        bot._with_outbound_context = MagicMock(return_value="BLOCK\nhi")
        bot._message_ts = MagicMock(return_value=None)
        captured = {}

        async def fake_process_message(**kw):
            captured.update(kw)
            raise RuntimeError("stop here")

        message = MagicMock()
        message.chat.send_action = AsyncMock()
        message.reply_text = AsyncMock()
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=CHAT))
        session = {"session_id": None, "model": "m"}
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=session)
        sm.should_start_new_session = AsyncMock(return_value=False)
        sm.update_session = AsyncMock()
        sm.set_last_user_message_at = AsyncMock()
        with patch.object(bot_mod, "session_manager", sm), \
             patch.object(bot_mod.sdk_bridge, "process_message", side_effect=fake_process_message), \
             patch.object(bot_mod.model_state, "take_start_notice", return_value=None), \
             patch.object(bot_mod.limit_gate, "active_until", return_value=None), \
             patch.object(type(bot), "_get_real_model", lambda self, s: "m", create=True), \
             patch.object(type(bot), "_effective_session_id", lambda self, u, s: None, create=True), \
             patch.object(type(bot), "_permission_callback", None, create=True), \
             patch.object(type(bot), "_proactive_push", None, create=True):
            asyncio.run(bot._process_user_message_text(update, 1, "hi"))
        self.assertEqual(captured.get("user_message"), "BLOCK\nhi")
        bot._with_outbound_context.assert_called_once_with("hi", CHAT, message)

    # 9 ---------------------------------------------------------------------
    BUBBLE = "Nothing logged for dinner. Did you eat anything?"
    LABEL = "Log it"

    def test_compose_tapped_bubble_rides_without_ledger_row(self):
        out = oc.compose(
            self.LABEL, CHAT, self.r.data, now=time.time(),
            reply_to={"message_id": 9, "text": self.BUBBLE,
                      "from_bot": True, "tapped": True})
        self.assertEqual(out, "\n".join([
            oc.OPEN_MARK, oc.REPLY_HEAD, self.BUBBLE,
            "The user is answering: " + self.BUBBLE, oc.CLOSE_MARK, self.LABEL]))

    def test_opt_tap_user_message_carries_bubble_question_and_label(self):
        import bridge.bot as bot_mod
        bot = self._bot()
        bot._save_session_id = AsyncMock()
        bot._drain_pending_texts = AsyncMock()
        bot._permission_callback = AsyncMock()
        bot._proactive_push = None
        bot._maybe_capture_outside_approval = AsyncMock()
        bot._send_smart = AsyncMock()
        bot._strip_keyboard_and_confirm = AsyncMock()
        bot._user_run_tasks = {}
        bot._user_queue_locks = {}
        bot._active_tasks = {}
        bot._user_pending_texts = {}
        bot._runtime_active_sessions = set()
        bot._typing_refresh_tasks = {}
        bot.application.bot.send_chat_action = AsyncMock()
        button = SimpleNamespace(text=self.LABEL, callback_data="opt:1")
        msg = MagicMock()
        msg.text = self.BUBBLE
        msg.caption = None
        msg.text_html = None
        msg.message_id = 41
        msg.reply_markup = SimpleNamespace(inline_keyboard=[[button]])
        msg.date = datetime.now(timezone.utc)
        query = MagicMock()
        query.data = "opt:1"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=CHAT),
            effective_user=SimpleNamespace(id=5), message=None,
            callback_query=query)
        pm = AsyncMock(return_value=None)
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value={"session_id": None, "model": "m"})
        # allowlist, not a denylist: inherited project/git config must not
        # steer the handler, whatever its prefix.
        env = {k: v for k, v in os.environ.items()
               if k in _KEEP_ENV or k.startswith("LC_")}

        async def scenario():
            await bot._handle_callback(update, MagicMock())
            await asyncio.gather(*list(bot._user_run_tasks.get(5, set())))

        with patch.dict(os.environ, env, clear=True), \
             patch.object(bot_mod, "PROJECT_ROOT", Path(self.r.tmp.name)), \
             patch.object(bot_mod, "session_manager", sm), \
             patch.object(bot_mod.sdk_bridge, "process_message", pm), \
             patch.object(bot_mod.config, "allowed_user_ids", [5]), \
             patch.object(type(bot), "_effective_session_id", lambda s, u, x: None, create=True), \
             patch.object(type(bot), "_finish_turn_with_auto_resume",
                          AsyncMock(return_value=None)):
            asyncio.run(scenario())
        sent = pm.await_args.kwargs["user_message"]
        self.assertIn(self.BUBBLE, sent)
        self.assertIn("The user is answering: " + self.BUBBLE, sent)
        self.assertTrue(sent.endswith("\n" + self.LABEL), sent)


if __name__ == "__main__":
    unittest.main()
