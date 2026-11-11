# test-component: bridge.session rotation (context-fill capture + next-message rotation)
# test-tier: 0
# test-prereq: tmp-files
# test-new-component: context-fill rotation is new in session.py/sdk_bridge.py; no existing suite owns session rotation (session suites cover inbox/inject only)
"""Session rotation: the 24 h idle rule plus context-fill rotation (DGN-1403).

The bridge records each turn's context fill from the SDK usage stream
(last main-agent AssistantMessage usage: input + cache_read +
cache_creation, over the model's context window) into sessions.json, bound
to the session id. At the next owner message: fill >= HARD ratio rotates
with the fixed notice; fill >= SOFT ratio (or a compaction already seen) and
an idle gap >= the soft minutes rotates silently; the 24 h idle rule is
unchanged.

test-lane: fast
test-tier: 1
component: bridge session
promise: fill is the last main-agent call's input+cache_read+cache_creation; subagent usage and missing usage never count
promise: a fake SDK stream crossing SOFT then HARD rotates exactly once on the next message, with the fixed notice
promise: SOFT fill rotates silently only after the idle gap; a short gap keeps the session
promise: no rotation while a turn of the user is in flight (never mid-turn)
promise: a stream without usage never rotates on the fill signal
promise: the 24 h idle path still rotates, silently
promise: a compaction seen in the session counts as SOFT crossed
promise: a HARD rotation sends no owner notice by default; SESSION_ROTATE_NOTICE=true restores it
promise: the rotation log line (idle/hard/soft) carries trigger, fill ratio, old -> new session id and recap=unknown
promise: a failed new-session start gives the owner exactly one notice (the existing generic error)
promise: thresholds are ratios with env overrides; off disables, percents read as ratios
"""

import asyncio
import os
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

import bridge.bot as bot_mod
import bridge.sdk_bridge as sdk_mod
import bridge.session as session_mod
from bridge import messages
from bridge.config import Config
from bridge.sdk_bridge import SdkBridge, _UserStreamState

USER = 7
CHAT = 7
WINDOW = 1_000_000
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


def _rotation_config(**over):
    base = dict(
        session_rotate_hard_ratio=0.50,
        session_rotate_soft_ratio=0.40,
        session_rotate_soft_idle_minutes=360.0,
        session_context_window_tokens=WINDOW,
        auto_new_session_after_hours=24.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _assistant(fill, model="claude-test", parent=None, cached=True):
    usage = None
    if fill is not None:
        usage = ({"input_tokens": 10, "cache_read_input_tokens": fill - 110,
                  "cache_creation_input_tokens": 100, "output_tokens": 50}
                 if cached else {"input_tokens": fill, "output_tokens": 5})
    return AssistantMessage(content=[TextBlock(text="ok")], model=model,
                            parent_tool_use_id=parent, usage=usage)


def _result(sid, usage=None, num_turns=3, window=WINDOW):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=num_turns, session_id=sid, usage=usage,
        model_usage={"claude-test": {"contextWindow": window}} if window else None,
    )


class _Stream:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    async def receive_messages(self):
        for m in self._msgs:
            yield m


class _Env:
    """Real SessionManager on a tmp sessions.json, shared by bot + bridge,
    with the rotation config patched into bridge.session."""

    def __init__(self, notice=False, fail_start=False, **cfg):
        self.cfg = _rotation_config(**cfg)
        self.notice = notice
        self.fail_start = fail_start

    def __enter__(self):
        # Rotation config is patched below; retain the harness environment,
        # including its subprocess guards and scratch directory redirects.
        self._td = TemporaryDirectory()
        self.cfg.session_store_path = Path(self._td.name) / "sessions.json"
        self._patches = [patch.object(session_mod, "config", self.cfg),
                         patch.object(sdk_mod, "config", self.cfg)]
        for p in self._patches:
            p.start()
        self.manager = session_mod.SessionManager()
        for target in (bot_mod, sdk_mod):
            p = patch.object(target, "session_manager", self.manager)
            p.start()
            self._patches.append(p)
        p = patch.object(bot_mod.config, "session_rotate_notice", self.notice)
        p.start()
        self._patches.append(p)
        self.bridge = SdkBridge()
        self.state = _UserStreamState(client=MagicMock(), model=None)
        self.sids = iter(f"sid-{i}" for i in range(2, 50))
        self.turns = []
        self.notices = []
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._td.cleanup()
        return False

    def session(self):
        return asyncio.run(self.manager.get_session(USER))

    def seed(self, sid="sid-1", last=T0):
        asyncio.run(self.manager.update_session(USER, {"session_id": sid}))
        asyncio.run(self.manager.set_last_user_message_at(USER, last))

    def stream(self, *msgs):
        """Run msgs through the real reader loop (no pending request: the
        observer runs before the proactive branch, which is stubbed)."""
        self.state.client = _Stream(msgs)
        with patch.object(self.bridge, "_handle_proactive_message", AsyncMock()):
            asyncio.run(self.bridge._reader_loop(USER, self.state))

    def message(self, at, in_flight=False):
        """One owner message through bot._process_user_message_text."""
        bot = bot_mod.TelegramBot.__new__(bot_mod.TelegramBot)
        bot.application = SimpleNamespace(bot=SimpleNamespace(id=1001))
        bot._runtime_active_sessions = set()
        bot._with_outbound_context = lambda text, chat_id, message: text
        bot._settle_turn = AsyncMock(return_value=None)
        message = MagicMock()
        message.date = at
        message.chat.send_action = AsyncMock()
        message.reply_text = AsyncMock(side_effect=lambda text, **kw: self.notices.append(text))
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=CHAT))

        async def fake_process_message(**kw):
            self.turns.append(kw.get("new_session"))
            if self.fail_start and kw.get("new_session"):
                raise RuntimeError("start failed")
            sess = await self.manager.get_session(USER)
            if not sess.get("session_id"):
                await self.manager.update_session(USER, {"session_id": next(self.sids)})

        with patch.object(bot_mod.sdk_bridge, "process_message", side_effect=fake_process_message), \
             patch.object(bot_mod.sdk_bridge, "turn_in_flight", return_value=in_flight), \
             patch.object(bot_mod.model_state, "take_start_notice", return_value=None), \
             patch.object(bot_mod.limit_gate, "active_until", return_value=None), \
             patch.object(type(bot), "_get_real_model", lambda self, s: "m", create=True), \
             patch.object(type(bot), "_effective_session_id", lambda self, u, s: s.get("session_id"), create=True), \
             patch.object(type(bot), "_permission_callback", None, create=True), \
             patch.object(type(bot), "_proactive_push", None, create=True):
            asyncio.run(bot._process_user_message_text(update, USER, "hi"))
        return self.turns[-1]


class TestFillCapture(unittest.TestCase):
    def test_fixture_preserves_subprocess_guards(self):
        before = dict(os.environ)
        with _Env():
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(dict(os.environ), before)

    def test_last_main_call_is_the_fill(self):
        with _Env() as env:
            env.seed()
            env.stream(_assistant(100_000), _assistant(420_000),
                       _assistant(900_000, parent="tool-1"),
                       _result("sid-1", usage={"input_tokens": 9_000_000}))
            fill = env.session()[session_mod.CONTEXT_FILL_KEY]
        self.assertEqual((fill["tokens"], fill["window"], fill["ratio"]),
                         (420_000, WINDOW, 0.42))
        self.assertEqual(fill["session_id"], "sid-1")

    def test_window_from_model_usage_and_default(self):
        self.assertEqual(session_mod.context_window_of(
            {"claude-test[1m]": {"contextWindow": 1_000_000},
             "claude-haiku": {"contextWindow": 200_000}}, "claude-test", 5), 1_000_000)
        self.assertEqual(session_mod.context_window_of(None, "m", 777), 777)

    def test_missing_usage_records_nothing(self):
        self.assertIsNone(session_mod.usage_fill_tokens(None))
        self.assertIsNone(session_mod.usage_fill_tokens({"output_tokens": 5}))
        with _Env() as env:
            env.seed()
            env.stream(_assistant(None), _result("sid-1", usage=None))
            self.assertNotIn(session_mod.CONTEXT_FILL_KEY, env.session())

    def test_single_call_turn_falls_back_to_result_usage(self):
        with _Env() as env:
            env.seed()
            env.stream(_assistant(None), _result("sid-1", num_turns=1,
                       usage={"input_tokens": 500_000}))
            self.assertEqual(env.session()[session_mod.CONTEXT_FILL_KEY]["ratio"], 0.5)


class TestRotationAtNextMessage(unittest.TestCase):
    def test_soft_then_hard_rotates_once_with_notice(self):
        with _Env(notice=True) as env:
            env.seed()
            env.stream(_assistant(410_000), _result("sid-1"))   # SOFT crossed
            self.assertFalse(env.message(T0 + timedelta(minutes=5)))
            env.stream(_assistant(520_000), _result("sid-1"))   # HARD crossed
            self.assertTrue(env.message(T0 + timedelta(minutes=10)))
            self.assertEqual(env.notices, [messages.SESSION_ROTATED_HARD])
            self.assertFalse(env.message(T0 + timedelta(minutes=11)))
            self.assertEqual(env.turns, [False, True, False])
            self.assertEqual(env.session()["session_id"], "sid-2")
            self.assertIsNone(env.session()[session_mod.CONTEXT_FILL_KEY])

    def test_soft_rotates_silently_after_idle_gap_only(self):
        with _Env() as env:
            env.seed()
            env.stream(_assistant(450_000), _result("sid-1"))
            self.assertFalse(env.message(T0 + timedelta(minutes=359)))
            env.stream(_assistant(450_000), _result("sid-1"))
            last = T0 + timedelta(minutes=359)
            self.assertTrue(env.message(last + timedelta(minutes=360)))
            self.assertFalse(env.message(last + timedelta(minutes=361)))
            self.assertEqual(env.turns, [False, True, False])
            self.assertEqual(env.notices, [])

    def test_never_mid_turn(self):
        with _Env(notice=True) as env:
            env.seed()
            env.stream(_assistant(800_000), _result("sid-1"))
            self.assertFalse(env.message(T0 + timedelta(minutes=1), in_flight=True))
            self.assertTrue(env.message(T0 + timedelta(minutes=2)))
            self.assertEqual(env.notices, [messages.SESSION_ROTATED_HARD])

    def test_stream_without_usage_never_rotates(self):
        with _Env() as env:
            env.seed()
            # result usage sums every call of a multi-call turn: not a fill
            env.stream(_assistant(None), _result("sid-1", usage={"input_tokens": 9_000_000}))
            env.stream(_assistant(None), _result("sid-1", usage=None, window=None))
            self.assertFalse(env.message(T0 + timedelta(hours=23)))
            self.assertEqual(env.notices, [])

    def test_24h_idle_still_rotates_silently(self):
        with _Env() as env:
            env.seed()
            self.assertTrue(env.message(T0 + timedelta(hours=24, minutes=1)))
            self.assertEqual(env.notices, [])

    def test_fill_of_an_earlier_session_is_inert(self):
        with _Env() as env:
            env.seed()
            env.stream(_assistant(900_000), _result("sid-1"))
            asyncio.run(env.manager.update_session(USER, {"session_id": "sid-9"}))
            self.assertFalse(env.message(T0 + timedelta(minutes=1)))

    def test_compaction_counts_as_soft(self):
        with _Env() as env:
            env.seed()
            env.stream(SystemMessage(subtype="compact_boundary", data={}),
                       _assistant(60_000), _result("sid-1"))
            env.stream(_assistant(70_000), _result("sid-1"))   # flag is sticky
            self.assertFalse(env.message(T0 + timedelta(hours=1)))
            self.assertTrue(env.message(T0 + timedelta(hours=7)))
            self.assertEqual(env.notices, [])


class TestRotationNoticeAndLog(unittest.TestCase):
    def _hard(self, env):
        env.seed()
        env.stream(_assistant(520_000), _result("sid-1"))
        return env.message(T0 + timedelta(minutes=10))

    def test_hard_is_silent_by_default(self):
        with _Env() as env:
            self.assertTrue(self._hard(env))
            self.assertEqual(env.notices, [])

    def test_hard_notice_with_flag(self):
        with _Env(notice=True) as env:
            self._hard(env)
            self.assertEqual(env.notices, [messages.SESSION_ROTATED_HARD])

    def test_notice_flag_env(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t:t", "SESSION_ROTATE_NOTICE": "true"}):
            self.assertTrue(Config(_env_file=None).session_rotate_notice)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t:t"}):
            os.environ.pop("SESSION_ROTATE_NOTICE", None)
            self.assertFalse(Config(_env_file=None).session_rotate_notice)

    def _lines(self, cm):
        return [r for r in cm.output if "session rotation" in r]

    def test_log_line_hard(self):
        with _Env() as env, self.assertLogs("bridge.bot", "INFO") as cm:
            self._hard(env)
        (line,) = self._lines(cm)
        for part in ("trigger=hard", "fill=0.52", "old=sid-1 -> new=sid-2", "recap=unknown"):
            self.assertIn(part, line)

    def test_log_line_soft(self):
        with _Env() as env, self.assertLogs("bridge.bot", "INFO") as cm:
            env.seed()
            env.stream(_assistant(450_000), _result("sid-1"))
            env.message(T0 + timedelta(minutes=361))
        (line,) = self._lines(cm)
        for part in ("trigger=soft", "fill=0.45", "old=sid-1 -> new=sid-2", "recap=unknown"):
            self.assertIn(part, line)

    def test_log_line_idle(self):
        with _Env() as env, self.assertLogs("bridge.bot", "INFO") as cm:
            env.seed()
            env.message(T0 + timedelta(hours=25))
        (line,) = self._lines(cm)
        for part in ("trigger=idle", "fill=none", "old=sid-1 -> new=sid-2", "recap=unknown"):
            self.assertIn(part, line)

    def test_start_failure_gives_one_notice(self):
        with _Env(fail_start=True) as env:
            self._hard(env)
            self.assertEqual(env.notices, [messages.GENERIC_ERROR])


class TestRatioConfig(unittest.TestCase):
    def test_env_overrides_and_parsing(self):
        env = {"TELEGRAM_BOT_TOKEN": "test:token", "SESSION_ROTATE_HARD_RATIO": "55",
               "SESSION_ROTATE_SOFT_RATIO": "off", "SESSION_ROTATE_SOFT_IDLE_MINUTES": "180"}
        with patch.dict(os.environ, env):
            cfg = Config(_env_file=None)
        self.assertEqual(cfg.session_rotate_hard_ratio, 0.55)
        self.assertIsNone(cfg.session_rotate_soft_ratio)
        self.assertEqual(cfg.session_rotate_soft_idle_minutes, 180.0)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test:token"}):
            for key in ("SESSION_ROTATE_HARD_RATIO", "SESSION_ROTATE_SOFT_RATIO",
                        "SESSION_ROTATE_SOFT_IDLE_MINUTES"):
                os.environ.pop(key, None)
            cfg = Config(_env_file=None)
        self.assertEqual((cfg.session_rotate_hard_ratio, cfg.session_rotate_soft_ratio,
                          cfg.session_rotate_soft_idle_minutes), (0.5, 0.4, 360.0))


if __name__ == "__main__":
    unittest.main()
