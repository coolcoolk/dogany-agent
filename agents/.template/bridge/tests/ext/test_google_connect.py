# test-component: bridge.google_connect
# test-tier: 1
# test-prereq: tmp-files, subprocess
# test-new-component: Google consent paste ingress and completion relay are a new bridge boundary.
"""DGN-1854 W2: Google connect paste intercept, completion relay, C15 ask.

Every test drives a FAKE `service.google` CLI (a Python script standing in
for `python3 -m service.google`) against a temp store directory. No real
Google endpoint, token store, keychain or ~/.dogany is touched.

Done criteria covered (LOCKED spec section 9):
  D11+  a pasted loopback URL with no pending flow is consumed, deleted,
        answered C7 and never reaches model input; group chat: deleted, no
        finish, private_only. The paste wins over a pending /login.
  D8    bridge log scan: after the intercept runs (with the fake CLI writing
        token-shaped text to stdout/stderr), no log record carries `code=`,
        `ya29.`, `1//`, the state or the pasted URL.
  D23   (prompt part) two expired flows with no callback -> exactly one C15
        with two option buttons; the answer is relayed to the session.
  3.5   a finish result is dropped into session-inbox and the real
        _session_inbox_loop injects it as a (non-quiet) background turn.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import textwrap
import time
import types
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import bot as _bot
from bridge import messages
from bridge.ext.dogany_estate import google_connect as gc

STATE = "Zt9xQ2-abcDEF_0123456789abcdefghijklmnopq"
CODE = "4/0AVG7fiQ-fakeAuthCodeValue"
CHAT = 4242
PASTE = (
    "http://127.0.0.1:53111/?state=%s&code=%s"
    "&scope=email%%20openid%%20https://www.googleapis.com/auth/calendar" % (STATE, CODE)
)

FAKE_CLI = textwrap.dedent('''\
    """Fake `python3 -m service.google` for the W2 tests."""
    import json, os, sys
    with open(os.environ["FAKE_RECORD"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    sys.stderr.write("debug ya29.LEAKED-ACCESS 1//LEAKED-REFRESH code=LEAKED\\n")
    if sys.argv[1:2] == ["status"]:
        print(json.dumps({"connected": True, "account": "owner@example.com"}))
        sys.exit(0)
    if sys.argv[1:2] == ["_expired-prompt"]:
        print(json.dumps({"key": "google.login.blocked_screen_ask"}))
        sys.exit(0)
    print(os.environ.get("FAKE_RESULT", '{"ok": true, "class": "ok"}'))
''')


class _Env:
    """Temp store + fake framework root + fake CLI, patched into gc."""

    def __init__(self, case: unittest.TestCase, result=None):
        self.tmp = Path(mkdtemp())
        case.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store"
        (self.store / "pending").mkdir(parents=True)
        self.fw = self.tmp / "fw"
        (self.fw / "service" / "google").mkdir(parents=True)
        (self.fw / "service" / "google" / "__main__.py").write_text("")
        self.cli = self.tmp / "fake_cli.py"
        self.cli.write_text(FAKE_CLI)
        self.record = self.tmp / "record.jsonl"
        self.data_dir = self.tmp / "botdata"
        self.data_dir.mkdir()
        env = {"FAKE_RECORD": str(self.record)}
        if result is not None:
            env["FAKE_RESULT"] = json.dumps(result)
        patches = [
            patch.object(gc, "store_root", lambda: self.store),
            patch.object(gc, "framework_root", lambda _root: self.fw),
            patch.object(gc, "cli_argv", lambda: [sys.executable, str(self.cli)]),
            patch.dict(os.environ, env),
        ]
        for p in patches:
            p.start()
            case.addCleanup(p.stop)
        self.origin = gc.origin_for(_bot.PROJECT_ROOT, CHAT)

    def pending(self, state=STATE, origin=None, expires_in=600, done=False,
                account_expected=None, chat=CHAT):
        data = {
            "verifier": "v" * 43, "scopes": ["calendar", "tasks"], "port": 53111,
            "origin": origin or gc.origin_for(_bot.PROJECT_ROOT, chat),
            "account_expected": account_expected,
            "expires_at": time.time() + expires_in, "done": done,
        }
        (self.store / "pending" / ("%s.json" % state)).write_text(json.dumps(data))

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(l) for l in self.record.read_text().splitlines()]

    def inbox(self):
        d = self.data_dir / "session-inbox"
        return sorted(d.glob("*")) if d.is_dir() else []


def _make_bot():
    bot = object.__new__(_bot.TelegramBot)
    bot.application = MagicMock()
    bot.application.bot.send_message = AsyncMock()
    return bot


def _make_update(text, user_id=1, chat_id=CHAT, chat_type="private"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.chat_id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    return update


def _sent(bot):
    return [c.kwargs.get("text") for c in bot.application.bot.send_message.await_args_list]


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class TestMatcher(unittest.TestCase):

    def test_spec_matcher(self):
        yes = [
            PASTE,
            "  http://localhost/?code=abc&state=x",
            "http://[::1]:8080/cb?state=s&error=access_denied",
            "https://127.0.0.1:1/?code=z",
        ]
        no = [
            "https://example.com/?code=abc",
            "here: http://127.0.0.1:5/?code=abc",
            "http://127.0.0.1:5/?state=abc",
            "http://127.0.0.2/?code=abc",
            "",
            None,
        ]
        for text in yes:
            self.assertTrue(gc.is_paste(text), text)
        for text in no:
            self.assertFalse(gc.is_paste(text), text)

    def test_state_charset_guard(self):
        self.assertEqual(gc.pasted_state(PASTE), STATE)
        self.assertIsNone(gc.pasted_state("http://127.0.0.1/?code=a&state=../../etc"))
        self.assertIsNone(gc.read_pending("../x"))

    def test_callback_kind_has_no_ttl(self):
        self.assertIsNone(_bot._callback_ttl(gc.CB_BLOCK))
        self.assertIsNone(_bot._callback_ttl(gc.CB_NONE))


class TestIntercept(unittest.IsolatedAsyncioTestCase):

    async def _handle(self, env, bot, update, login_pending=False):
        manager = MagicMock()
        manager.pending_for = MagicMock(return_value=object() if login_pending else None)
        manager.submit = AsyncMock(return_value=True)
        bot._login_manager = manager
        cap = _LogCapture()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(cap)
        root.setLevel(logging.DEBUG)
        try:
            with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
                 patch.object(_bot, "config", MagicMock(bot_data_dir=env.data_dir)), \
                 patch.object(_bot, "session_manager") as sm, \
                 patch.object(bot, "_dispatch_text_task", new=AsyncMock()) as dispatch, \
                 patch.object(bot, "_maybe_buffer_split_part", new=AsyncMock()) as split:
                await bot._handle_text_message(update, MagicMock())
        finally:
            root.removeHandler(cap)
            root.setLevel(old_level)
        # Never reached any model route, nor the /login code claim.
        dispatch.assert_not_called()
        split.assert_not_called()
        sm.get_session.assert_not_called()
        manager.submit.assert_not_called()
        return cap.lines

    def _assert_clean_logs(self, lines):
        # D8: bridge log scan.
        blob = "\n".join(lines)
        for needle in ("code=", "ya29.", "1//", STATE, CODE, "127.0.0.1:53111", "LEAKED"):
            self.assertNotIn(needle, blob)
        self.assertTrue(any("google-paste: consumed len=%d" % len(PASTE) in l for l in lines))

    async def test_no_pending_flow_consumed_deleted_c7(self):
        env = _Env(self)
        bot = _make_bot()
        update = _make_update(PASTE)
        lines = await self._handle(env, bot, update, login_pending=True)
        update.message.delete.assert_awaited_once()
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.expired"]])
        self.assertEqual(env.calls(), [])
        self.assertEqual(env.inbox(), [])
        self._assert_clean_logs(lines)

    async def test_pending_for_other_chat_is_c7_no_finish(self):
        env = _Env(self)
        env.pending(origin="otherinst:%d" % CHAT)
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.expired"]])
        self.assertEqual(env.calls(), [])

    async def test_expired_pending_is_c7_no_finish(self):
        env = _Env(self)
        env.pending(expires_in=-5)
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.expired"]])
        self.assertEqual(env.calls(), [])

    async def test_delete_failure_is_best_effort(self):
        env = _Env(self)
        bot = _make_bot()
        update = _make_update(PASTE)
        update.message.delete = AsyncMock(side_effect=RuntimeError("no rights"))
        await self._handle(env, bot, update)
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.expired"]])

    async def test_pending_match_runs_frozen_finish_and_relays(self):
        env = _Env(self, result={"ok": True, "class": "ok", "sub": "1234567890"})
        env.pending()
        bot = _make_bot()
        update = _make_update(PASTE)
        lines = await self._handle(env, bot, update)
        update.message.delete.assert_awaited_once()
        self.assertEqual(env.calls(), [[
            "connect", "finish", "--url", PASTE, "--origin", env.origin, "--json",
        ]])
        self.assertTrue(env.origin.endswith(":%d" % CHAT))
        # Owner 2026-11-10: a paste is acknowledged at once; success then
        # has no further bridge reply (the relayed turn speaks).
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.received"]])
        files = env.inbox()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".md")
        body = files[0].read_text()
        self.assertTrue(body.startswith("[google-connect-result] class=ok sub=1234567890\n"))
        self.assertFalse(body.startswith(_bot.QUIET_INJECT_PREFIXES))
        self.assertNotIn(CODE, body)
        self._assert_clean_logs(lines)
        # The paste counts as a callback hit for the 6.1 heuristic.
        self.assertIn(STATE, gc.load_ledger(env.data_dir)["hits"])

    async def test_current_service_owns_completion_relay(self):
        env = _Env(self, result={"ok": True, "class": "ok", "sub": "42"})
        env.pending()
        path = env.store / "pending" / (STATE + ".json")
        pending = json.loads(path.read_text())
        pending["instance_path"] = str(_bot.PROJECT_ROOT)
        path.write_text(json.dumps(pending))
        await self._handle(env, _make_bot(), _make_update(PASTE))
        self.assertEqual(len(env.calls()), 1)
        self.assertEqual(env.inbox(), [])

    async def test_denied_class_gets_its_key_and_relay(self):
        env = _Env(self, result={"ok": False, "class": "consent_denied"})
        env.pending()
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.received"], messages.GOOGLE_LOGIN["google.login.denied"]])
        self.assertIn("class=consent_denied sub=-", env.inbox()[0].read_text())

    async def test_flow_done_is_silent(self):
        env = _Env(self, result={"ok": False, "class": "flow_done"})
        env.pending()
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.received"]])  # only the receipt
        self.assertEqual(env.inbox(), [])

    async def test_unknown_class_falls_back_to_internal(self):
        env = _Env(self, result={"ok": False, "class": "brand_new_class"})
        env.pending()
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.received"], messages.GOOGLE_LOGIN["google.login.internal"]])

    async def test_account_mismatch_renders_c14_with_expected_email(self):
        env = _Env(self, result={"ok": False, "class": "account_mismatch"})
        env.pending(account_expected="999")
        bot = _make_bot()
        await self._handle(env, bot, _make_update(PASTE))
        self.assertEqual(env.calls()[1], ["status", "--account", "999", "--json"])
        self.assertEqual(_sent(bot), [
            messages.GOOGLE_LOGIN["google.login.received"],
            messages.GOOGLE_LOGIN["google.login.account_mismatch"].format(email="owner@example.com")
        ])

    async def test_group_chat_deleted_no_finish_private_only(self):
        env = _Env(self)
        env.pending(chat=-100777)
        env.pending()
        bot = _make_bot()
        update = _make_update(PASTE, chat_id=-100777, chat_type="supergroup")
        lines = await self._handle(env, bot, update)
        update.message.delete.assert_awaited_once()
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.private_only"]])
        self.assertEqual(env.calls(), [])
        self.assertEqual(env.inbox(), [])
        self.assertNotIn("code=", "\n".join(lines))

    async def test_non_paste_text_passes_through(self):
        bot = _make_bot()
        update = _make_update("hello http://127.0.0.1:5/?code=x")
        self.assertFalse(await bot._maybe_consume_google_paste(update, 1, update.message.text))
        update.message.delete.assert_not_called()


class _StopLoop(Exception):
    pass


class TestRelayInjection(unittest.TestCase):

    def test_real_inbox_loop_injects_relay_non_quiet(self):
        env = _Env(self)
        gc.write_relay(env.data_dir / "session-inbox", gc.relay_text("ok", "42"))
        injected = []

        async def inject(uid, text, quiet=False):
            injected.append((uid, text, quiet))
            return True

        fake_bridge = MagicMock()
        fake_bridge.ensure_owner_stream = AsyncMock(return_value=True)
        fake_bridge.inject_background_turn = AsyncMock(side_effect=inject)
        fake_config = MagicMock(bot_data_dir=env.data_dir, allowed_user_ids=[1])
        fake_sm = MagicMock()
        fake_sm.get_session = AsyncMock(return_value={"model": "sonnet"})
        ticks = {"n": 0}

        async def fake_sleep(_s):
            ticks["n"] += 1
            if ticks["n"] > 1:
                raise _StopLoop

        ns = types.SimpleNamespace(
            _proactive_push=AsyncMock(), _user_turn_active=lambda _u: False,
            _google_block_ask_tick=AsyncMock(),
            _get_real_model=lambda session: session["model"],
        )
        with patch.object(_bot, "config", fake_config), \
             patch.object(_bot, "sdk_bridge", fake_bridge), \
             patch.object(_bot, "session_manager", fake_sm), \
             patch.object(_bot.asyncio, "sleep", fake_sleep):
            try:
                asyncio.run(_bot.TelegramBot._session_inbox_loop(ns))
            except _StopLoop:
                pass
        self.assertEqual(len(injected), 1)
        uid, text, quiet = injected[0]
        self.assertEqual(uid, 1)
        self.assertFalse(quiet)
        self.assertTrue(text.startswith("[google-connect-result] class=ok sub=42"))
        self.assertEqual(env.inbox(), [])
        ns._google_block_ask_tick.assert_awaited_once()


class TestBlockScreenAsk(unittest.IsolatedAsyncioTestCase):

    async def test_landed_google_claim_and_cap_marker(self):
        # Run the existing W1 internal CLI hooks in child processes against
        # a temporary HOME. No consent listener or real service is started.
        from service import google
        from service.google import store
        from tempfile import TemporaryDirectory
        repo = Path(__file__).resolve().parents[5]
        with TemporaryDirectory(prefix="calendar-bridge-") as scratch:
            root = Path(scratch) / "instance"
            root.mkdir()
            botdata = root / ".telegram_bot"
            origin = gc.origin_for(root, CHAT)
            with patch.dict(os.environ, {"HOME": scratch}), \
                 patch.object(_bot, "PROJECT_ROOT", root), \
                 patch.object(_bot, "config", MagicMock(bot_data_dir=botdata)), \
                 patch.object(gc, "framework_root", return_value=repo):
                with store.lock():
                    for i in (1, 2):
                        store.write(store.root() / "pending" / (str(i) * 43 + ".json"),
                                    {"origin": origin, "created": time.time() - 1500 + i,
                                     "expires_at": time.time() - 900 + i,
                                     "done": False, "callback_hit": False})
                bot = _make_bot()
                await bot._google_block_ask_tick()
                await bot._google_block_ask_tick()
                self.assertEqual(len(self._asks(bot)), 1)
                query = MagicMock()
                query.message.chat_id = CHAT
                query.edit_message_reply_markup = AsyncMock()
                await bot._handle_google_block_answer(query, gc.CB_BLOCK)
                self.assertEqual(google.status()["paused"], "cap_suspected")
                self.assertGreater(store.read(store.root() / "cap.json")["until"], time.time())

    async def _tick(self, env, bot):
        with patch.object(_bot, "config", MagicMock(bot_data_dir=env.data_dir)):
            await bot._google_block_ask_tick()

    def _asks(self, bot):
        return [c for c in bot.application.bot.send_message.await_args_list
                if c.kwargs.get("text") == messages.GOOGLE_LOGIN["google.login.blocked_screen_ask"]]

    async def test_two_expired_no_callback_asks_exactly_once(self):
        env = _Env(self)
        env.pending(state="a" * 43, expires_in=-1200)
        env.pending(state="b" * 43, expires_in=-60)
        bot = _make_bot()
        await self._tick(env, bot)
        await self._tick(env, bot)
        env.pending(state="c" * 43, expires_in=-30)  # a third expiry, same day
        await self._tick(env, bot)
        asks = self._asks(bot)
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0].kwargs["chat_id"], CHAT)
        rows = asks[0].kwargs["reply_markup"].inline_keyboard
        self.assertEqual([[b.callback_data for b in r] for r in rows],
                         [[gc.CB_BLOCK], [gc.CB_NONE]])
        self.assertEqual([r[0].text for r in rows], [
            messages.GOOGLE_LOGIN["google.login.blocked_screen_yes"],
            messages.GOOGLE_LOGIN["google.login.blocked_screen_no"],
        ])

    async def test_no_ask_when_not_two_consecutive_no_callback(self):
        cases = {
            "one_expired": [("a", -60, False)],
            "latest_still_live": [("a", -1200, False), ("b", -60, False), ("c", 300, False)],
            "latest_done": [("a", -1200, False), ("b", -60, True)],
            "older_than_24h": [("a", -90000, False), ("b", -60, False)],
            "other_instance": [],
        }
        for name, flows in cases.items():
            with self.subTest(name):
                env = _Env(self)
                for state, exp, done in flows:
                    env.pending(state=state * 43, expires_in=exp, done=done)
                if name == "other_instance":
                    env.pending(state="a" * 43, origin="elsewhere:%d" % CHAT, expires_in=-1200)
                    env.pending(state="b" * 43, origin="elsewhere:%d" % CHAT, expires_in=-60)
                bot = _make_bot()
                await self._tick(env, bot)
                self.assertEqual(self._asks(bot), [])

    async def test_pasted_flow_is_a_callback_hit(self):
        env = _Env(self)
        env.pending(state="a" * 43, expires_in=-1200)
        env.pending(state="b" * 43, expires_in=-60)
        gc.note_callback_hit(env.data_dir, "b" * 43)
        bot = _make_bot()
        await self._tick(env, bot)
        self.assertEqual(self._asks(bot), [])

    async def test_block_answer_sends_c11_and_relays_cap_suspected(self):
        env = _Env(self)
        bot = _make_bot()
        query = MagicMock()
        query.message.chat_id = CHAT
        query.edit_message_reply_markup = AsyncMock()
        with patch.object(_bot, "config", MagicMock(bot_data_dir=env.data_dir)):
            await bot._handle_google_block_answer(query, gc.CB_BLOCK)
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.assertEqual(_sent(bot), [messages.GOOGLE_LOGIN["google.login.cap"]])
        self.assertTrue(env.inbox()[0].read_text().startswith(
            "[google-connect-result] class=cap_suspected"))

    async def test_none_answer_relays_without_owner_text(self):
        env = _Env(self)
        bot = _make_bot()
        query = MagicMock()
        query.message.chat_id = CHAT
        query.edit_message_reply_markup = AsyncMock()
        with patch.object(_bot, "config", MagicMock(bot_data_dir=env.data_dir)):
            await bot._handle_google_block_answer(query, gc.CB_NONE)
        self.assertEqual(_sent(bot), [])
        self.assertTrue(env.inbox()[0].read_text().startswith(
            "[google-connect-result] class=flow_expired"))


if __name__ == "__main__":
    unittest.main()
