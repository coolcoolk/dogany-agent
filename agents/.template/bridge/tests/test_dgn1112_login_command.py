"""DGN-1112: /login -- terminal-free Claude re-login relayed over Telegram.

Every test drives a FAKE CLI child (a Python script standing in for
`claude auth login` / `claude auth status`) and a fake Telegram transport.
No real `claude` binary is ever executed and no credential store is read or
written: the fake records what reached its stdin in a temp file.

Covers (DGN-1857 research 4(b) acceptance list):
  - manual URL split across stdout chunks, wrapped in ANSI colour + OSC 8;
  - URL validation (foreign host / localhost callback rejected, never sent);
  - code -> the same child's stdin; wrong state / malformed / attempt cap;
  - non-owner and group chat never start a flow;
  - owner text while pending is consumed (never reaches the model) and the
    code never appears in a log record;
  - timeout, /cancel, duplicate /login, one flow per credential store
    (in-process and across managers sharing the lock dir);
  - env credential override refused;
  - child failure, child death before the URL, `auth status` not logged in;
  - success -> on_success -> the SDK layer recreates an idle stream at the
    next request boundary and leaves an in-flight one alone.
"""

import asyncio
import logging
import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN setup

from bridge import auth_login, messages
from bridge import bot as _bot
from bridge import sdk_bridge as _sdk

STATE = "St4te_abc-123"
CODE = f"AuthC0de-xyz.987#{STATE}"
GOOD_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c"
    "&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference+user%3Aprofile&code_challenge=chal"
    f"&code_challenge_method=S256&state={STATE}"
)

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    """Fake `claude` for the /login tests. Never touches any credential."""
    import os, signal, sys, time
    mode = os.environ.get("FAKE_MODE", "ok")
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    record = os.environ["FAKE_RECORD"]
    status_file = os.environ["FAKE_STATUS"]
    args = sys.argv[1:]
    if args[:2] == ["auth", "status"]:
        try:
            print(open(status_file).read())
        except OSError:
            print('{{"loggedIn": false}}')
        sys.exit(0)
    assert args[:2] == ["auth", "login"], args
    with open(record, "a") as f:
        f.write("ARGS " + " ".join(args) + " BROWSER=" + os.environ.get("BROWSER", "") + "\\n")
    out = sys.stdout
    if mode == "die":
        sys.stderr.write("Login failed: boom\\n")
        sys.exit(1)
    url = os.environ["FAKE_URL"]
    out.write("\\x1b[2mOpening browser to sign in\\u2026\\x1b[0m\\n")
    out.flush()
    time.sleep(0.05)
    linked = "\\x1b]8;;" + url + "\\x07" + url + "\\x1b]8;;\\x07"
    line = "If the browser didn't open, visit: " + linked + "\\n"
    third = len(line) // 3
    for part in (line[:third], line[third:2 * third], line[2 * third:]):
        out.write(part)
        out.flush()
        time.sleep(0.05)
    out.write("Paste code here if prompted > ")
    out.flush()
    got = sys.stdin.readline()
    with open(record, "a") as f:
        f.write("STDIN " + got)
    if mode == "hang":
        time.sleep(60)
    if mode == "fail":
        sys.stderr.write("Login failed: Authentication failed: Invalid authorization code\\n")
        sys.exit(1)
    if mode == "status_false":
        out.write("Login successful.\\n")
        sys.exit(0)
    with open(status_file, "w") as f:
        f.write('{{"loggedIn": true, "authMethod": "claude.ai"}}')
    out.write("Login successful.\\n")
    sys.exit(0)
''').format(python=sys.executable)


class _Harness:
    def __init__(self, tmp: Path, mode: str = "ok", url: str = GOOD_URL, **timeouts):
        self.tmp = tmp
        self.cli = tmp / "claude"
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(0o755)
        self.record = tmp / "record.txt"
        self.status = tmp / "status.json"
        self.sent = []
        self.successes = 0
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp),
            "CLAUDE_CONFIG_DIR": str(tmp / "cfg"),
            "FAKE_MODE": mode,
            "FAKE_URL": url,
            "FAKE_RECORD": str(self.record),
            "FAKE_STATUS": str(self.status),
        }
        self.manager = self.make_manager(**timeouts)

    def make_manager(self, **timeouts):
        async def send(chat_id, text):
            self.sent.append((chat_id, text))

        def on_success():
            self.successes += 1

        return auth_login.LoginManager(
            send=send,
            cli_resolver=lambda: str(self.cli),
            on_success=on_success,
            lock_dir=str(self.tmp),
            env=self.env,
            **timeouts,
        )

    def texts(self):
        return [t for _c, t in self.sent]

    def record_text(self):
        return self.record.read_text() if self.record.exists() else ""

    async def wait_phase(self, phase, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            flow = self.manager.pending_for(1)
            if flow is not None and flow.phase == phase:
                return flow
            await asyncio.sleep(0.02)
        raise AssertionError(f"flow never reached {phase}: sent={self.texts()}")

    async def wait_done(self, timeout=10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.manager.pending_for(1) is None:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"flow never finished: sent={self.texts()}")


def _url_message(url=GOOD_URL):
    return messages.LOGIN_URL.format(url=url, minutes=10)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestUrlParsing(unittest.TestCase):

    def test_osc8_and_ansi_are_stripped(self):
        raw = ("\x1b[1mvisit:\x1b[0m \x1b]8;;" + GOOD_URL + "\x07" + GOOD_URL
               + "\x1b]8;;\x07\n")
        self.assertEqual(auth_login.extract_login_url(raw), GOOD_URL)

    def test_incomplete_line_is_not_parsed(self):
        raw = "visit: " + GOOD_URL[:40]
        self.assertIsNone(auth_login.extract_login_url(raw))
        self.assertEqual(auth_login.extract_login_url(raw + GOOD_URL[40:] + "\n"), GOOD_URL)

    def test_split_escape_sequence_across_chunks(self):
        raw = "visit: \x1b]8;;" + GOOD_URL + "\x07" + GOOD_URL + "\x1b]8;;\x07\n"
        cut = raw.index("\x1b]8;;\x07") + 2  # cut inside the closing OSC
        self.assertIsNone(auth_login.extract_login_url(raw[:cut]))
        self.assertEqual(auth_login.extract_login_url(raw[:cut] + raw[cut:]), GOOD_URL)

    def test_valid_manual_url_yields_state(self):
        self.assertEqual(auth_login.validate_login_url(GOOD_URL), STATE)

    def test_rejected_urls(self):
        bad = [
            GOOD_URL.replace("https://claude.com", "https://claude.com.evil.example"),
            GOOD_URL.replace("https://claude.com", "https://evil.example"),
            GOOD_URL.replace("https://claude.com", "http://claude.com"),
            GOOD_URL.replace("https://claude.com", "https://claude.com:8443"),
            GOOD_URL.replace("https://claude.com", "https://user@claude.com"),
            # the automatic (localhost callback) URL is useless on a phone
            GOOD_URL.replace(
                "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback",
                "http%3A%2F%2Flocalhost%3A5555%2Fcallback",
            ),
            GOOD_URL.replace(f"&state={STATE}", ""),
            GOOD_URL + "&state=second",
            GOOD_URL.replace("/cai/oauth/authorize", "/cai/oauth/other"),
            # dec-257: endpoints older CLIs used were never evidenced on this
            # build; they fail closed (owner gets the terminal guide).
            GOOD_URL.replace("https://claude.com/cai/oauth/authorize",
                             "https://claude.ai/oauth/authorize"),
            GOOD_URL.replace("platform.claude.com", "console.anthropic.com"),
        ]
        for url in bad:
            with self.subTest(url=url):
                self.assertIsNone(auth_login.validate_login_url(url))

    def test_env_override_detection(self):
        self.assertIsNone(auth_login.env_credential_override({}))
        self.assertEqual(
            auth_login.env_credential_override({"CLAUDE_CODE_OAUTH_TOKEN": "x"}),
            "CLAUDE_CODE_OAUTH_TOKEN",
        )


class TestFailureFallsBackToTerminalGuide(unittest.TestCase):
    """dec-257: a /login the bot cannot finish ends on the owner-confirmed
    DGN-1857 terminal guide, verbatim, in both locales."""

    def test_lead_lines_point_at_the_guide_without_restating_it(self):
        from bridge.i18n import en, ko
        for strings, word in ((ko.STRINGS, "터미널"), (en.STRINGS, "Terminal")):
            for key in ("login_failed", "login_no_cli"):
                with self.subTest(key=key, word=word):
                    lead = strings[key]
                    self.assertIn(word, lead)
                    self.assertNotIn("`", lead)
                    self.assertNotIn("auth login", lead)

    def test_active_locale_composition(self):
        self.assertEqual(
            messages.LOGIN_FAILED,
            messages.t("login_failed") + "\n\n" + messages.ERROR_AUTH_RELOGIN)
        self.assertEqual(
            messages.LOGIN_NO_CLI,
            messages.t("login_no_cli") + "\n\n" + messages.ERROR_AUTH_RELOGIN_STEPS)
        self.assertEqual(messages.LOGIN_GUIDE_NOTICES,
                         {messages.LOGIN_FAILED, messages.LOGIN_NO_CLI})

    def test_no_cli_guide_drops_its_first_sentence(self):
        # dec-266: with no CLI the login may not have expired, so the guide
        # after login_no_cli starts at the terminal steps.
        from bridge.i18n import en, ko
        for strings, gone, first in (
                (ko.STRINGS, "Claude 로그인이 만료됐어요.", "이 맥의 터미널에서"),
                (en.STRINGS, "Your Claude login has expired.", "On this Mac,")):
            with self.subTest(first=first):
                guide = strings["error_auth_relogin"]
                steps = guide.split(". ", 1)[1]
                self.assertTrue(guide.startswith(gone))
                self.assertTrue(steps.startswith(first))
                self.assertEqual(guide, gone + " " + steps)
        self.assertNotIn(messages.ERROR_AUTH_RELOGIN, messages.LOGIN_NO_CLI)
        self.assertTrue(messages.LOGIN_NO_CLI.endswith(messages.ERROR_AUTH_RELOGIN_STEPS))
        self.assertTrue(messages.LOGIN_FAILED.endswith(messages.ERROR_AUTH_RELOGIN))

    def test_user_recoverable_endings_do_not_carry_the_guide(self):
        # timeout / cancel / attempt cap / busy are fixed by another /login,
        # not by a terminal: no guide there.
        for text in (messages.LOGIN_TIMEOUT, messages.LOGIN_CANCELLED,
                     messages.LOGIN_CODE_ATTEMPTS, messages.LOGIN_BUSY,
                     messages.LOGIN_ENV_TOKEN):
            with self.subTest(text=text):
                self.assertNotIn(messages.ERROR_AUTH_RELOGIN, text)


# ---------------------------------------------------------------------------
# Flow with the fake child
# ---------------------------------------------------------------------------

class TestLoginFlow(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory(prefix="dgn1112-")
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_success_relays_url_feeds_code_and_reconnects(self):
        h = _Harness(self.tmp)
        with self.assertLogs("bridge.auth_login", level="DEBUG") as logs:
            self.assertEqual(await h.manager.start(1, 10), "started")
            await h.wait_phase("awaiting_code")
            self.assertEqual(h.texts(), [messages.LOGIN_PREPARING, _url_message()])
            self.assertTrue(await h.manager.submit(1, 10, f"  {CODE}\n"))
            await h.wait_done()
        self.assertEqual(h.successes, 1)
        self.assertEqual(h.texts()[-2:], [messages.LOGIN_CODE_RECEIVED, messages.LOGIN_SUCCESS])
        rec = h.record_text()
        self.assertIn("ARGS auth login --claudeai BROWSER=true", rec)
        self.assertIn(f"STDIN {CODE}\n", rec)
        # The code never reaches a log record.
        for line in logs.output:
            self.assertNotIn("AuthC0de", line)
            self.assertNotIn(STATE, line)

    async def test_wrong_state_is_not_fed_and_attempts_are_capped(self):
        h = _Harness(self.tmp)
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        self.assertTrue(await h.manager.submit(1, 10, "AuthC0de#someOtherState"))
        self.assertEqual(h.texts()[-1], messages.LOGIN_CODE_MISMATCH)
        self.assertTrue(await h.manager.submit(1, 10, "just chatting here"))
        self.assertEqual(h.texts()[-1], messages.LOGIN_CODE_INVALID)
        self.assertIsNotNone(h.manager.pending_for(1))
        self.assertTrue(await h.manager.submit(1, 10, "AuthC0de#stillWrong"))
        await h.wait_done()
        self.assertEqual(h.texts()[-1], messages.LOGIN_CODE_ATTEMPTS)
        self.assertNotIn("STDIN", h.record_text())
        self.assertEqual(h.successes, 0)

    async def test_other_user_or_chat_is_not_consumed(self):
        h = _Harness(self.tmp)
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        self.assertFalse(await h.manager.submit(2, 10, CODE))
        self.assertFalse(await h.manager.submit(1, 99, CODE))
        await h.manager.cancel(1)

    async def test_timeout_kills_child_and_releases_store(self):
        h = _Harness(self.tmp, code_timeout=0.4)
        await h.manager.start(1, 10)
        flow = await h.wait_phase("awaiting_code")
        await h.wait_done()
        self.assertEqual(h.texts()[-1], messages.LOGIN_TIMEOUT)
        self.assertIsNotNone(flow.proc.returncode)
        self.assertIsNone(flow.lock_fd)
        # store lock released: a new flow can start
        self.assertEqual(await h.manager.start(1, 10), "started")
        await h.manager.cancel(1)

    async def test_cancel(self):
        h = _Harness(self.tmp)
        await h.manager.start(1, 10)
        flow = await h.wait_phase("awaiting_code")
        self.assertTrue(await h.manager.cancel(1))
        self.assertEqual(h.texts()[-1], messages.LOGIN_CANCELLED)
        self.assertIsNotNone(flow.proc.returncode)
        self.assertIsNone(h.manager.pending_for(1))
        self.assertFalse(await h.manager.cancel(1))

    async def test_child_ignoring_sigterm_is_killed(self):
        h = _Harness(self.tmp, mode="stubborn")
        await h.manager.start(1, 10)
        flow = await h.wait_phase("awaiting_code")
        await h.manager.cancel(1)
        self.assertEqual(flow.proc.returncode, -9)

    async def test_duplicate_and_cross_manager_store_lock(self):
        h = _Harness(self.tmp)
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        self.assertEqual(await h.manager.start(1, 10), "duplicate")
        self.assertEqual(h.texts()[-1], messages.LOGIN_ALREADY_PENDING)
        # A second bridge process on the same credential store (same lock dir).
        other = h.make_manager()
        self.assertEqual(await other.start(1, 10), "busy")
        self.assertEqual(h.texts()[-1], messages.LOGIN_BUSY)
        await h.manager.cancel(1)
        self.assertEqual(await other.start(1, 10), "started")
        await other.cancel(1)

    async def test_send_failure_still_releases_the_store(self):
        h = _Harness(self.tmp)
        sent_ok = h.manager._send

        async def broken_send(chat_id, text):
            raise RuntimeError("telegram down")

        h.manager._send = broken_send
        self.assertEqual(await h.manager.start(1, 10), "started")
        await h.wait_done()
        h.manager._send = sent_ok
        self.assertEqual(await h.manager.start(1, 10), "started")
        await h.manager.cancel(1)

    async def test_env_credential_refused(self):
        h = _Harness(self.tmp)
        h.env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-fake"
        self.assertEqual(await h.manager.start(1, 10), "env_token")
        self.assertEqual(h.texts(), [messages.LOGIN_ENV_TOKEN])
        self.assertFalse(h.record.exists())

    async def test_no_cli_falls_back_to_terminal_guide(self):
        h = _Harness(self.tmp)
        h.manager._cli_resolver = lambda: None
        self.assertEqual(await h.manager.start(1, 10), "no_cli")
        self.assertEqual(h.texts(), [messages.LOGIN_NO_CLI])
        self.assertTrue(h.texts()[0].endswith(messages.ERROR_AUTH_RELOGIN_STEPS))
        self.assertFalse(h.record.exists())

    async def test_child_failure_after_code(self):
        h = _Harness(self.tmp, mode="fail")
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        await h.manager.submit(1, 10, CODE)
        await h.wait_done()
        self.assertEqual(h.texts()[-1], messages.LOGIN_FAILED)
        self.assertEqual(h.successes, 0)

    async def test_child_dies_before_url(self):
        h = _Harness(self.tmp, mode="die")
        await h.manager.start(1, 10)
        await h.wait_done()
        self.assertEqual(h.texts(), [messages.LOGIN_PREPARING, messages.LOGIN_FAILED])

    async def test_foreign_url_is_never_sent(self):
        evil = GOOD_URL.replace("https://claude.com", "https://evil.example")
        h = _Harness(self.tmp, url=evil)
        await h.manager.start(1, 10)
        await h.wait_done()
        self.assertEqual(h.texts(), [messages.LOGIN_PREPARING, messages.LOGIN_FAILED])
        self.assertFalse(any("evil.example" in t for t in h.texts()))

    async def test_exit_zero_but_status_not_logged_in(self):
        h = _Harness(self.tmp, mode="status_false")
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        await h.manager.submit(1, 10, CODE)
        await h.wait_done()
        self.assertEqual(h.texts()[-1], messages.LOGIN_FAILED)
        self.assertEqual(h.successes, 0)

    async def test_child_hangs_after_code(self):
        h = _Harness(self.tmp, mode="hang", exit_timeout=0.5)
        await h.manager.start(1, 10)
        await h.wait_phase("awaiting_code")
        await h.manager.submit(1, 10, CODE)
        self.assertTrue(await h.manager.submit(1, 10, CODE))  # duplicate paste
        self.assertEqual(h.texts()[-1], messages.LOGIN_WAIT)
        await h.wait_done()
        self.assertEqual(h.texts()[-1], messages.LOGIN_FAILED)
        self.assertEqual(h.record_text().count("STDIN"), 1)


# ---------------------------------------------------------------------------
# Bot layer (object.__new__ harness, fake Telegram objects)
# ---------------------------------------------------------------------------

def _make_bot():
    bot = object.__new__(_bot.TelegramBot)
    bot.application = MagicMock()
    bot.application.handlers = {0: []}
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


class TestBotLayer(unittest.IsolatedAsyncioTestCase):

    async def test_non_owner_never_starts(self):
        bot = _make_bot()
        update = _make_update("/login", user_id=666)
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=False)), \
             patch.object(_bot, "LoginManager") as mgr_cls:
            await bot._cmd_login(update, MagicMock())
        mgr_cls.assert_not_called()
        update.message.reply_text.assert_not_called()

    async def test_group_chat_refused(self):
        bot = _make_bot()
        update = _make_update("/login", chat_type="group")
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(_bot, "LoginManager") as mgr_cls:
            await bot._cmd_login(update, MagicMock())
        mgr_cls.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(messages.LOGIN_PRIVATE_ONLY)

    async def test_owner_starts_flow(self):
        bot = _make_bot()
        manager = MagicMock()
        manager.start = AsyncMock(return_value="started")
        bot._login_manager = manager
        update = _make_update("/login")
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)):
            await bot._cmd_login(update, MagicMock())
        manager.start.assert_awaited_once_with(1, 10)

    async def test_code_message_is_consumed_before_any_route(self):
        bot = _make_bot()
        manager = MagicMock()
        manager.pending_for = MagicMock(return_value=object())
        manager.submit = AsyncMock(return_value=True)
        bot._login_manager = manager
        update = _make_update(CODE)
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(_bot, "session_manager") as sm, \
             patch.object(bot, "_dispatch_text_task", new=AsyncMock()) as dispatch:
            await bot._handle_text_message(update, MagicMock())
        manager.submit.assert_awaited_once_with(1, 10, CODE)
        dispatch.assert_not_called()
        sm.get_session.assert_not_called()
        update.message.delete.assert_awaited_once()

    async def test_text_without_pending_login_flows_normally(self):
        bot = _make_bot()
        bot._login_manager = MagicMock()
        bot._login_manager.pending_for = MagicMock(return_value=None)
        bot._login_manager.submit = AsyncMock()
        update = _make_update("hello")
        self.assertFalse(await bot._maybe_consume_login_code(update, 1, "hello"))
        bot._login_manager.submit.assert_not_called()
        # no manager at all (never used /login): no-op
        self.assertFalse(await _make_bot()._maybe_consume_login_code(update, 1, "hello"))

    async def test_cancel_claimed_only_while_pending(self):
        bot = _make_bot()
        manager = MagicMock()
        manager.cancel = AsyncMock(return_value=True)
        bot._login_manager = manager
        update = _make_update("/cancel")
        with patch.object(bot, "_check_access", new=AsyncMock(return_value=True)), \
             patch.object(bot, "_exec_slash_command", new=AsyncMock()) as fwd:
            await bot._handle_skill_command(update, MagicMock())
            fwd.assert_not_called()
            manager.cancel = AsyncMock(return_value=False)
            await bot._handle_skill_command(update, MagicMock())
            fwd.assert_awaited_once()

    async def test_guide_notices_render_code_spans_as_html(self):
        bot = _make_bot()
        bot.application.bot.send_message = AsyncMock()
        await bot._send_login_notice(10, messages.LOGIN_FAILED)
        kwargs = bot.application.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("<code>claude auth login</code>", kwargs["text"])
        self.assertNotIn("`", kwargs["text"])

    async def test_url_notice_stays_plain_text(self):
        bot = _make_bot()
        bot.application.bot.send_message = AsyncMock()
        await bot._send_login_notice(10, _url_message())
        kwargs = bot.application.bot.send_message.await_args.kwargs
        self.assertNotIn("parse_mode", kwargs)
        self.assertEqual(kwargs["text"], _url_message())
        self.assertTrue(kwargs["disable_web_page_preview"])

    async def test_login_on_menu(self):
        # dec-266: copy approved, /login joins the menu.
        self.assertIn("login", [c for c, _ in _bot.COMMAND_MENU_SPEC])
        src = Path(_bot.__file__).read_text()
        self.assertIn('CommandHandler("login", self._cmd_login)', src)


# ---------------------------------------------------------------------------
# SDK layer: success -> next request recreates the client at an idle boundary
# ---------------------------------------------------------------------------

class TestCredentialRenewalReconnect(unittest.IsolatedAsyncioTestCase):

    def _bridge(self):
        bridge = _sdk.SdkBridge()
        created = []

        async def fake_create(user_id, model):
            state = _sdk._UserStreamState(client=MagicMock(), model=model)
            created.append(state)
            return state

        async def fake_disconnect(user_id, *a, **k):
            return bridge._streams.pop(user_id, None) is not None

        bridge._create_user_stream = fake_create
        bridge._disconnect_user_stream = fake_disconnect
        return bridge, created

    async def test_idle_stream_recreated_after_renewal(self):
        bridge, created = self._bridge()
        first = await bridge._get_or_create_stream(1, None, False)
        self.assertIs(await bridge._get_or_create_stream(1, None, False), first)
        bridge.mark_credentials_renewed()
        second = await bridge._get_or_create_stream(1, None, False)
        self.assertIsNot(second, first)
        self.assertEqual(len(created), 2)
        # renewed once -> recreated once
        self.assertIs(await bridge._get_or_create_stream(1, None, False), second)

    async def test_in_flight_stream_kept_until_idle(self):
        bridge, created = self._bridge()
        first = await bridge._get_or_create_stream(1, None, False)
        first.pending.append(MagicMock())
        bridge.mark_credentials_renewed()
        self.assertIs(await bridge._get_or_create_stream(1, None, False), first)
        first.pending.clear()
        self.assertIsNot(await bridge._get_or_create_stream(1, None, False), first)

    def test_bot_wires_success_to_sdk_renewal(self):
        bot = _make_bot()
        with patch.object(_bot.sdk_bridge, "mark_credentials_renewed") as renew:
            manager = bot._get_login_manager()
            manager._on_success()
        renew.assert_called_once()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
