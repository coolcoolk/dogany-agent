"""DGN-1981: /key -- take one key from the chat, store it, delete the message.

Every test runs against a temp HOME with the REAL routines/lib/secret_store.py
pinned to its file backend (no keychain, no network); the verify call is a
stub. Telegram is MagicMock/AsyncMock objects (object.__new__ harness, same as
the /login tests).

Covers:
  - /key: owner only, private chat only, unknown slot -> usage, arms one intake;
  - the next message is consumed before any route (no session lookup, no
    model dispatch), deleted from the chat FIRST, stored, confirmed without
    echo; it is one-shot and expires;
  - verify rejected -> not stored; verify unknown -> stored, said so;
  - invalid shape -> not stored; keychain_locked -> its own copy;
  - delete failure -> the owner is told to delete it;
  - /cancel; a key pasted without /key is deleted and never routed;
  - the key never appears in any log record (all loggers, DEBUG), any reply,
    the session store, or any file under HOME other than the store entry.
"""

import hashlib
import logging
import sys
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bridge import bot as _bot
from bridge import codex_onboard, messages, secret_intake

KEY = "sk-ant-api03-FIXTURE-dgn1981-intake-never-print-me"


class _Slot:
    def __init__(self, name, service, account, kind):
        self.name, self.service, self.account, self.kind = name, service, account, kind


SLOTS = {
    "api-credit": _Slot("api-credit", "dogany-anthropic-api-credit", "dogany", "anthropic_api"),
    "api-admin": _Slot("api-admin", "dogany-anthropic-admin", "dogany", "anthropic_admin"),
}


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dgn1981-intake-")
        self.home = self.tmp.name
        self.secrets = os.path.join(self.home, ".dogany", "secrets")
        self._env = patch.dict(os.environ, {
            "HOME": self.home, "DOGANY_SECRET_BACKEND": "file",
            "DOGANY_SECRET_DIR": self.secrets})
        self._env.start()
        self.store = secret_intake.load_lib("secret_store")
        self.verdict = "ok"
        self.verify_calls = []
        self.clock = _Clock()
        # Capture EVERY log record from every logger, at DEBUG.
        self.records = []
        handler = logging.Handler(level=logging.DEBUG)
        handler.emit = lambda rec: self.records.append(rec)
        self._root = logging.getLogger()
        self._old_level = self._root.level
        self._root.addHandler(handler)
        self._root.setLevel(logging.DEBUG)
        self._handler = handler

    def tearDown(self):
        self._root.removeHandler(self._handler)
        self._root.setLevel(self._old_level)
        self._env.stop()
        self.tmp.cleanup()

    def verify(self, kind, key):
        self.verify_calls.append(kind)
        return self.verdict

    def manager(self):
        return secret_intake.SecretIntakeManager(
            store=self.store, verify=self.verify, slots=SLOTS, clock=self.clock)

    def log_text(self):
        out = []
        for r in self.records:
            out.append(r.getMessage())
            if r.exc_info:
                out.append(logging.Formatter().formatException(r.exc_info))
        return "\n".join(out)

    def files_outside_store(self):
        out = []
        for dp, _, fns in os.walk(self.home):
            if dp == self.secrets:
                continue
            for fn in fns:
                out.append(Path(dp, fn).read_text(errors="replace"))
        return "\n".join(out)

    def assert_never_leaked(self, bot=None):
        self.assertNotIn(KEY, self.log_text())
        self.assertNotIn(KEY, self.files_outside_store())
        if bot is not None:
            for call in bot.application.bot.send_message.await_args_list:
                self.assertNotIn(KEY, str(call))


class TestManager(_Base):

    async def test_store_one_shot(self):
        m = self.manager()
        self.assertTrue(m.start(1, 10, "API-Credit"))
        self.assertIsNotNone(m.pending_for(1, 10))
        self.assertIsNone(m.pending_for(1, 11))  # other chat
        self.assertIsNone(m.pending_for(2, 10))  # other user
        self.assertEqual(await m.consume(1, 10, "  " + KEY + "\n"), (secret_intake.STORED, ""))
        self.assertEqual(self.store.get("dogany-anthropic-api-credit", "dogany"), KEY)
        self.assertIsNone(m.pending_for(1, 10))  # one message only
        self.assertEqual(self.verify_calls, ["anthropic_api"])
        f = next(Path(self.secrets).iterdir())
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assert_never_leaked()

    async def test_unknown_slot(self):
        m = self.manager()
        self.assertFalse(m.start(1, 10, "nope"))
        self.assertFalse(m.start(1, 10, ""))
        self.assertIsNone(m.pending_for(1, 10))

    async def test_expiry(self):
        m = self.manager()
        m.start(1, 10, "api-admin")
        self.clock.t += secret_intake.INTAKE_TIMEOUT_S + 1
        self.assertIsNone(m.pending_for(1, 10))
        self.assertEqual((await m.consume(1, 10, KEY))[0], secret_intake.INVALID)
        self.assertIsNone(self.store.get("dogany-anthropic-admin", "dogany"))

    async def test_rejected_is_not_stored(self):
        self.verdict = "rejected"
        m = self.manager()
        m.start(1, 10, "api-credit")
        self.assertEqual((await m.consume(1, 10, KEY))[0], secret_intake.REJECTED)
        self.assertIsNone(self.store.get("dogany-anthropic-api-credit", "dogany"))
        self.assert_never_leaked()

    async def test_unknown_verdict_stores(self):
        self.verdict = "unknown"
        m = self.manager()
        m.start(1, 10, "api-credit")
        self.assertEqual((await m.consume(1, 10, KEY))[0], secret_intake.STORED_UNVERIFIED)
        self.assertEqual(self.store.get("dogany-anthropic-api-credit", "dogany"), KEY)

    async def test_invalid_shape_never_verified(self):
        m = self.manager()
        for bad in ("two words " + KEY, "", "x" * 600, 'q"' + KEY):
            m.start(1, 10, "api-credit")
            self.assertEqual((await m.consume(1, 10, bad))[0], secret_intake.INVALID)
        self.assertEqual(self.verify_calls, [])
        self.assertFalse(os.path.exists(self.secrets))

    async def test_store_error_code(self):
        store = MagicMock()
        store.valid_value = self.store.valid_value
        err = self.store.SecretStoreError("keychain_locked", "locked")
        store.set = MagicMock(side_effect=err)
        m = secret_intake.SecretIntakeManager(
            store=store, verify=self.verify, slots=SLOTS, clock=self.clock)
        m.start(1, 10, "api-credit")
        self.assertEqual(await m.consume(1, 10, KEY), (secret_intake.STORE_FAILED, "keychain_locked"))
        self.assert_never_leaked()

    async def test_verify_crash_counts_as_unknown(self):
        def boom(kind, key):
            raise RuntimeError("network " + key)  # even a leaky error stays inside
        m = secret_intake.SecretIntakeManager(
            store=self.store, verify=boom, slots=SLOTS, clock=self.clock)
        m.start(1, 10, "api-credit")
        self.assertEqual((await m.consume(1, 10, KEY))[0], secret_intake.STORED_UNVERIFIED)
        self.assert_never_leaked()

    def test_default_wiring_loads_the_real_libs(self):
        m = secret_intake.SecretIntakeManager()
        self.assertEqual(m.slot_names(), ["api-admin", "api-credit", "codex-api-key"])


# ---------------------------------------------------------------------------
# Bot layer
# ---------------------------------------------------------------------------

def _make_bot():
    bot = object.__new__(_bot.TelegramBot)
    bot.application = MagicMock()
    bot.application.handlers = {0: []}
    bot.application.bot.send_message = AsyncMock()
    return bot


def _make_update(text, user_id=1, chat_id=10, chat_type="private"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.message.text = text
    update.message.chat_id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    return update


def _ctx(*args):
    ctx = MagicMock()
    ctx.args = list(args)
    return ctx


class TestBotLayer(_Base):

    def bot(self):
        bot = _make_bot()
        bot._secret_intake = self.manager()
        return bot

    def sent(self, bot):
        return [c.kwargs["text"] for c in bot.application.bot.send_message.await_args_list]

    async def _key_cmd(self, bot, *args, **kw):
        update = _make_update("/key " + " ".join(args), **kw)
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)):
            await bot._cmd_key(update, _ctx(*args))
        return update

    async def _text(self, bot, text, **kw):
        update = _make_update(text, **kw)
        order = []
        update.message.delete = AsyncMock(side_effect=lambda: order.append("delete"))
        bot.application.bot.send_message = AsyncMock(
            side_effect=lambda **k: order.append("send"))
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(_bot, "session_manager") as sm, \
             patch.object(bot, "_dispatch_text_task", new=AsyncMock()) as dispatch, \
             patch.object(bot, "_maybe_buffer_split_part", new=AsyncMock(return_value=False)), \
             patch.object(bot, "_maybe_capture_outside_approval", new=AsyncMock(return_value=False)), \
             patch.object(bot, "_clear_open_decision", new=MagicMock()):
            sm.get_session = AsyncMock(return_value={})
            sm.update_session = AsyncMock()
            await bot._handle_text_message(update, MagicMock())
        return update, sm, dispatch, order

    async def test_non_owner_never_arms(self):
        bot = self.bot()
        update = _make_update("/key api-credit", user_id=666)
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=False)):
            await bot._cmd_key(update, _ctx("api-credit"))
        update.message.reply_text.assert_not_called()
        self.assertIsNone(bot._secret_intake.pending_for(666, 10))

    async def test_group_chat_refused(self):
        bot = self.bot()
        update = await self._key_cmd(bot, "api-credit", chat_type="group")
        update.message.reply_text.assert_awaited_once_with(messages.KEY_PRIVATE_ONLY)
        self.assertIsNone(bot._secret_intake.pending_for(1, 10))

    async def test_usage_on_missing_or_unknown_slot(self):
        bot = self.bot()
        for args in ((), ("nope",)):
            update = await self._key_cmd(bot, *args)
            text = update.message.reply_text.await_args.args[0]
            self.assertEqual(text, messages.KEY_USAGE.format(slots="api-admin, api-credit"))
        self.assertIsNone(bot._secret_intake.pending_for(1, 10))

    async def test_full_flow_never_reaches_the_model(self):
        bot = self.bot()
        update = await self._key_cmd(bot, "api-credit")
        update.message.reply_text.assert_awaited_once()
        self.assertIn("api-credit", update.message.reply_text.await_args.args[0])
        update, sm, dispatch, order = await self._text(bot, KEY)
        self.assertEqual(order, ["delete", "send"])  # deleted before anything is said
        dispatch.assert_not_called()
        sm.get_session.assert_not_called()
        sm.update_session.assert_not_called()
        update.message.reply_text.assert_not_called()  # no reply-to the deleted message
        self.assertEqual(self.sent(bot), [messages.KEY_STORED])
        self.assertEqual(self.store.get("dogany-anthropic-api-credit", "dogany"), KEY)
        self.assert_never_leaked(bot)
        # one-shot: the following message flows normally
        update, sm, dispatch, _ = await self._text(bot, "hello")
        dispatch.assert_awaited_once()
        update.message.delete.assert_not_called()

    async def test_outcome_copy(self):
        cases = (
            ("rejected", KEY, messages.KEY_REJECTED),
            ("unknown", KEY, messages.KEY_STORED_UNVERIFIED),
            ("ok", "not a key at all", messages.KEY_INVALID),
        )
        for verdict, text, want in cases:
            self.verdict = verdict
            bot = self.bot()
            await self._key_cmd(bot, "api-admin")
            update, _, dispatch, _ = await self._text(bot, text)
            dispatch.assert_not_called()
            self.assertEqual(self.sent(bot), [want])
        self.assert_never_leaked()

    async def test_locked_keychain_copy(self):
        bot = self.bot()
        bot._secret_intake = secret_intake.SecretIntakeManager(
            store=MagicMock(valid_value=self.store.valid_value,
                            set=MagicMock(side_effect=self.store.SecretStoreError("keychain_locked", "x"))),
            verify=self.verify, slots=SLOTS, clock=self.clock)
        await self._key_cmd(bot, "api-credit")
        await self._text(bot, KEY)
        self.assertEqual(self.sent(bot), [messages.KEY_STORE_LOCKED])
        bot._secret_intake = secret_intake.SecretIntakeManager(
            store=MagicMock(valid_value=self.store.valid_value,
                            set=MagicMock(side_effect=self.store.SecretStoreError("insecure_perms", "x"))),
            verify=self.verify, slots=SLOTS, clock=self.clock)
        await self._key_cmd(bot, "api-credit")
        await self._text(bot, KEY)
        self.assertEqual(self.sent(bot), [messages.KEY_STORE_FAILED.format(code="insecure_perms")])
        self.assert_never_leaked()

    async def test_delete_failure_is_told(self):
        bot = self.bot()
        await self._key_cmd(bot, "api-credit")
        update = _make_update(KEY)
        update.message.delete = AsyncMock(side_effect=RuntimeError("can't delete " + KEY))
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(_bot, "session_manager"), \
             patch.object(bot, "_dispatch_text_task", new=AsyncMock()) as dispatch:
            await bot._handle_text_message(update, MagicMock())
        dispatch.assert_not_called()
        self.assertEqual(self.sent(bot), [messages.KEY_DELETE_FAILED])
        self.assertIsNone(self.store.get("dogany-anthropic-api-credit", "dogany"))
        self.assertIsNone(bot._secret_intake.pending_for(1, 10))
        self.assert_never_leaked(bot)

    async def test_cancel(self):
        bot = self.bot()
        await self._key_cmd(bot, "api-credit")
        update = _make_update("/cancel")
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(bot, "_exec_slash_command", new=AsyncMock()) as exec_cmd:
            await bot._handle_skill_command(update, MagicMock())
        update.message.reply_text.assert_awaited_once_with(messages.KEY_CANCELLED)
        exec_cmd.assert_not_called()
        self.assertIsNone(bot._secret_intake.pending_for(1, 10))
        # nothing pending: /cancel goes on to the skill forwarder as before
        update = _make_update("/cancel")
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(bot, "_exec_slash_command", new=AsyncMock()) as exec_cmd:
            await bot._handle_skill_command(update, MagicMock())
        exec_cmd.assert_awaited_once()

    async def test_expired_intake_then_stray_key_is_still_deleted(self):
        bot = self.bot()
        await self._key_cmd(bot, "api-credit")
        self.clock.t += secret_intake.INTAKE_TIMEOUT_S + 1
        update, sm, dispatch, order = await self._text(bot, "here it is: " + KEY)
        self.assertEqual(order, ["delete", "send"])
        dispatch.assert_not_called()
        sm.get_session.assert_not_called()
        self.assertEqual(self.sent(bot), [messages.KEY_STRAY_DELETED])
        self.assertFalse(os.path.exists(self.secrets))  # not stored without /key
        self.assert_never_leaked(bot)

    async def test_stray_key_without_any_intake(self):
        bot = _make_bot()  # never used /key
        update, sm, dispatch, _ = await self._text(bot, KEY)
        dispatch.assert_not_called()
        self.assertEqual(self.sent(bot), [messages.KEY_STRAY_DELETED])

    async def test_plain_text_untouched(self):
        bot = _make_bot()
        update, sm, dispatch, _ = await self._text(bot, "my sk-ant- notes are short")
        dispatch.assert_awaited_once()
        update.message.delete.assert_not_called()

    def test_key_registered_in_group_zero(self):
        src = Path(_bot.__file__).read_text()
        self.assertIn('CommandHandler("key", self._cmd_key)', src)
        # consumed before the session lookup and every other route
        body = src[src.index("async def _handle_text_message"):]
        self.assertLess(body.index("_maybe_consume_secret"), body.index("session_manager.get_session"))
        self.assertLess(body.index("_maybe_consume_secret"), body.index("_maybe_buffer_split_part"))


class TestCodexSeam(_Base):
    _text = TestBotLayer._text
    sent = TestBotLayer.sent

    async def arm(self, bot):
        query = MagicMock()
        query.message.chat.type = "private"
        query.message.chat.id = 10
        query.edit_message_reply_markup = AsyncMock()
        await bot._handle_login_callback(query, codex_onboard.CB_CX_KEY, 1, query.message.chat)
        self.assertEqual(bot._secret_intake.pending_for(1, 10).slot, "codex-api-key")

    async def test_real_callback_store_and_cli_stdin_only(self):
        bot = _make_bot()
        lib = Path(__file__).resolve().parents[2] / "routines" / "lib" / "codex_login.py"
        stub = Path(self.home) / "codex"
        # The fake CLI records only a digest; no key file outside the store.
        stub.write_text(
            f"#!{sys.executable}\n"
            "import hashlib, pathlib, sys\n"
            "d = pathlib.Path(__file__).parent\n"
            "with (d / 'argv.log').open('a') as f: f.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['login', 'status']:\n"
            "    ok = (d / 'digest').exists()\n"
            "    print('Logged in using an API key' if ok else 'Not logged in', file=sys.stderr)\n"
            "    sys.exit(0 if ok else 1)\n"
            "assert sys.argv[1:] == ['login', '--with-api-key']\n"
            "key = sys.stdin.read().strip()\n"
            "(d / 'digest').write_text(hashlib.sha256(key.encode()).hexdigest())\n"
            "print(key); print(key, file=sys.stderr)\n")
        stub.chmod(0o700)
        with patch.dict(os.environ, {"CODEX_BIN": str(stub)}), \
             patch.object(codex_onboard, "_lib", return_value=lib):
            await self.arm(bot)
            # A simultaneous older Claude login must never receive this key.
            bot._login_manager = MagicMock()
            bot._login_manager.pending_for.return_value = object()
            bot._login_manager.submit_code = AsyncMock()
            original = self.store.set
            def save(*args):
                self.assertTrue(update.message.delete.await_count)
                return original(*args)
            update = _make_update(KEY)
            with patch.object(self.store, "set", side_effect=save), \
                 patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
                 patch.object(_bot, "session_manager") as sm, \
                 patch.object(bot, "_maybe_consume_login_code", new=AsyncMock()) as oauth:
                await bot._handle_text_message(update, MagicMock())
            oauth.assert_not_called()
            sm.get_session.assert_not_called()
        self.assertEqual((Path(self.home) / "digest").read_text(), hashlib.sha256(KEY.encode()).hexdigest())
        self.assertIn("login --with-api-key", (Path(self.home) / "argv.log").read_text())
        self.assertEqual(self.store.get("dogany-codex-api-key", "dogany"), KEY)
        self.assertIn(codex_onboard.t("codex_success"), self.sent(bot))
        self.assertIsNone(bot._secret_intake.pending_for(1, 10))
        self.assert_never_leaked(bot)

    async def test_cancel_expire_replace_and_delete_failure_drop_callback(self):
        for end in ("cancel", "expire", "replace", "delete_failed"):
            bot = _make_bot()
            bot._secret_intake = secret_intake.SecretIntakeManager(clock=self.clock)
            with patch.object(codex_onboard, "run_lib") as cli:
                await self.arm(bot)
                if end == "cancel":
                    with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)):
                        await bot._handle_skill_command(_make_update("/cancel"), MagicMock())
                elif end == "expire":
                    self.clock.t += secret_intake.INTAKE_TIMEOUT_S + 1
                elif end == "replace":
                    bot._secret_intake.start(1, 10, "codex-api-key")
                update = _make_update(KEY)
                if end == "delete_failed":
                    update.message.delete.side_effect = RuntimeError(KEY)
                await bot._maybe_consume_secret(update, 1, KEY)
                cli.assert_not_called()
            self.assert_never_leaked(bot)

    async def test_callback_never_runs_after_rejection_or_store_error(self):
        for verdict in ("rejected", "ok"):
            done = AsyncMock()
            store = MagicMock(valid_value=self.store.valid_value)
            store.set.side_effect = RuntimeError(KEY)
            manager = secret_intake.SecretIntakeManager(
                store=store, verify=lambda *_: verdict, slots=SLOTS)
            manager.start(1, 10, "api-credit", on_stored=done)
            outcome, _ = await manager.consume(1, 10, KEY)
            self.assertIn(outcome, (secret_intake.REJECTED, secret_intake.STORE_FAILED))
            done.assert_not_called()
            self.assertIsNone(manager.pending_for(1, 10))
            self.assert_never_leaked()

    async def test_callback_exception_never_escapes_or_logs_key(self):
        manager = self.manager()
        done = AsyncMock(side_effect=RuntimeError(KEY))
        manager.start(1, 10, "api-credit", on_stored=done)
        self.assertEqual((await manager.consume(1, 10, KEY))[0], secret_intake.STORED)
        done.assert_awaited_once()
        self.assert_never_leaked()

    async def test_hostile_store_error_and_verdict_are_redacted(self):
        for verdict in ("ok", KEY):
            bot = _make_bot()
            store = MagicMock(valid_value=self.store.valid_value)
            if verdict == "ok":
                store.set.side_effect = self.store.SecretStoreError(KEY, KEY)
            bot._secret_intake = secret_intake.SecretIntakeManager(
                store=store, verify=lambda *_: verdict, slots=SLOTS)
            bot._secret_intake.start(1, 10, "api-credit")
            await self._text(bot, KEY)
            self.assert_never_leaked(bot)


class TestCopy(unittest.TestCase):

    def test_both_locales_carry_every_key_and_the_pending_mark(self):
        from bridge.i18n import en, ko
        keys = [k for k in vars(messages) if k.startswith("KEY_")]
        self.assertEqual(len(keys), 12)
        for mod in (en, ko):
            cat = mod.MESSAGES if hasattr(mod, "MESSAGES") else None
            src = Path(mod.__file__).read_text()
            self.assertIn("DGN-1981", src)
            self.assertIn("PENDING_OWNER_CONFIRM: all key_* copy", src)
            for k in keys:
                self.assertIn('"%s"' % k.lower(), src)
            if cat is not None:
                for k in keys:
                    self.assertIn(k.lower(), cat)

    def test_placeholders_format(self):
        messages.KEY_USAGE.format(slots="a")
        messages.KEY_PROMPT.format(slot="a", minutes=2)
        messages.KEY_STORE_FAILED.format(code="x")


if __name__ == "__main__":
    unittest.main()
