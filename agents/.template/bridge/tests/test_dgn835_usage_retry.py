"""DGN-835: usage-defer manual retry -- button (usageretry:<label>) AND the
/usageretry <label> slash command (DGN-841 A: the STALE-gate-free route).

Covers the safety rails the design grill flagged (replay security / live-turn
non-consumption / saturation re-fail):
  (a) label charset validation: path-traversal shaped labels are rejected
      before any filesystem access.
  (b) missing replay file -> degrade notice, nothing executed.
  (c) usage preflight: 7d window >= 99% -> NOT executed, "not enough" edit,
      keyboard KEPT for a later tap.
  (d) usage lookup failure -> NOT executed, keyboard kept.
  (e) sufficient headroom -> replay launched DETACHED via subprocess.Popen
      (start_new_session), never through sdk_bridge.process_message (no live
      turn consumed), confirmation edit drops the keyboard.
  (f) /usageretry slash: same core (shared _usage_retry_run), replies via
      message (no keyboard mechanics), arg validation, traversal rejection.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import bridge.bot as bot_mod
from bridge.bot import TelegramBot
from bridge import messages


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()
        self.message.reply_markup = "KEEP-ME"
        self.edits = []

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


@pytest.fixture
def bot():
    # __new__: the handler needs no __init__ state (no application, no session).
    return TelegramBot.__new__(TelegramBot)


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    defer_dir = tmp_path / ".dogany" / "usage-defer"
    defer_dir.mkdir(parents=True)
    return defer_dir


def run(coro):
    return asyncio.run(coro)


def _write_replay(defer_dir, label):
    replay = defer_dir / f"{label}.replay"
    replay.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    os.chmod(replay, 0o700)
    return replay


LABEL = "com.telegram-skill-bot.test.consolidate-0430"


# --- (a) charset validation ---

@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "a/b",
    "a b",
    "label;rm",
    "",
    "x" * 200,
])
def test_bad_label_rejected(bot, sandbox_home, bad, monkeypatch):
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    q = FakeQuery(f"usageretry:{bad}")
    run(bot._handle_usage_retry_callback(q, q.data))
    assert called["popen"] == 0, "malformed label must never execute anything"
    assert q.edits and q.edits[0][0] == messages.USAGE_RETRY_BAD_LABEL


# --- (b) missing replay file ---

def test_missing_replay_degrades(bot, sandbox_home, monkeypatch):
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    q = FakeQuery(f"usageretry:{LABEL}")
    run(bot._handle_usage_retry_callback(q, q.data))
    assert called["popen"] == 0
    assert q.edits and q.edits[0][0] == messages.USAGE_RETRY_NO_REPLAY.format(label=LABEL)


# --- (c) preflight: still exhausted -> no execution, keyboard kept ---

def test_preflight_blocks_when_exhausted(bot, sandbox_home, monkeypatch):
    _write_replay(sandbox_home, LABEL)
    monkeypatch.setattr(
        bot, "_fetch_usage_json",
        lambda: {"seven_day": {"utilization": 99.4, "resets_at": "2026-08-13T13:00:00Z"}},
    )
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    q = FakeQuery(f"usageretry:{LABEL}")
    run(bot._handle_usage_retry_callback(q, q.data))
    assert called["popen"] == 0, "saturated window must NOT execute (would just re-fail)"
    text, kb = q.edits[0]
    assert "99" in text
    assert "2026-08-13T13:00:00Z" in text
    assert kb == "KEEP-ME", "button must survive for a later tap"


# --- (d) lookup failure -> no execution, keyboard kept ---

def test_preflight_lookup_failure_keeps_button(bot, sandbox_home, monkeypatch):
    _write_replay(sandbox_home, LABEL)
    monkeypatch.setattr(bot, "_fetch_usage_json", lambda: None)
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    q = FakeQuery(f"usageretry:{LABEL}")
    run(bot._handle_usage_retry_callback(q, q.data))
    assert called["popen"] == 0
    text, kb = q.edits[0]
    assert text == messages.USAGE_RETRY_LOOKUP_FAILED
    assert kb == "KEEP-ME"


# --- (e) sufficient headroom -> detached replay, no live turn ---

def test_sufficient_headroom_launches_detached(bot, sandbox_home, monkeypatch):
    replay = _write_replay(sandbox_home, LABEL)
    monkeypatch.setattr(
        bot, "_fetch_usage_json",
        lambda: {"seven_day": {"utilization": 42.0, "resets_at": "2026-08-13T13:00:00Z"}},
    )
    popen_calls = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return MagicMock()

    monkeypatch.setattr(bot_mod.subprocess, "Popen", fake_popen)
    # process_message must never be touched (no live turn consumed).
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message",
                        MagicMock(side_effect=AssertionError("live turn consumed!")))

    q = FakeQuery(f"usageretry:{LABEL}")
    run(bot._handle_usage_retry_callback(q, q.data))

    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv == ["/bin/bash", str(replay)]
    assert kwargs.get("start_new_session") is True, "replay must run detached"
    text, kb = q.edits[0]
    assert text == messages.USAGE_RETRY_STARTED.format(label=LABEL)
    assert kb is None, "confirmation edit drops the one-shot button"


def test_preflight_threshold_aligned_with_defer():
    # lockstep guard: preflight == memory-engine defer threshold (99).
    assert TelegramBot._USAGE_RETRY_PREFLIGHT_PCT == 99.0


# --- (f) /usageretry slash command (DGN-841 A) ---

class FakeSlashMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self):
        self.message = FakeSlashMessage()


class FakeContext:
    def __init__(self, args):
        self.args = args


async def _allow_access(update):
    return True


def test_slash_launches_replay(bot, sandbox_home, monkeypatch):
    replay = _write_replay(sandbox_home, LABEL)
    monkeypatch.setattr(bot, "_check_access", _allow_access)
    monkeypatch.setattr(
        bot, "_fetch_usage_json",
        lambda: {"seven_day": {"utilization": 42.0, "resets_at": "2026-08-13T13:00:00Z"}},
    )
    popen_calls = []
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda argv, **k: popen_calls.append((argv, k)) or MagicMock())
    monkeypatch.setattr(bot_mod.sdk_bridge, "process_message",
                        MagicMock(side_effect=AssertionError("live turn consumed!")))

    upd = FakeUpdate()
    run(bot._cmd_usage_retry(upd, FakeContext([LABEL])))
    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv == ["/bin/bash", str(replay)]
    assert kwargs.get("start_new_session") is True
    assert upd.message.replies == [messages.USAGE_RETRY_STARTED.format(label=LABEL)]


def test_slash_preflight_blocks_when_exhausted(bot, sandbox_home, monkeypatch):
    _write_replay(sandbox_home, LABEL)
    monkeypatch.setattr(bot, "_check_access", _allow_access)
    monkeypatch.setattr(
        bot, "_fetch_usage_json",
        lambda: {"seven_day": {"utilization": 99.4, "resets_at": "2026-08-13T13:00:00Z"}},
    )
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    upd = FakeUpdate()
    run(bot._cmd_usage_retry(upd, FakeContext([LABEL])))
    assert called["popen"] == 0
    assert "99" in upd.message.replies[0]


@pytest.mark.parametrize("args", [[], ["a", "b"]])
def test_slash_arg_validation(bot, sandbox_home, args, monkeypatch):
    monkeypatch.setattr(bot, "_check_access", _allow_access)
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    upd = FakeUpdate()
    run(bot._cmd_usage_retry(upd, FakeContext(args)))
    assert called["popen"] == 0
    assert upd.message.replies == [messages.USAGE_RETRY_USAGE]


def test_slash_rejects_traversal_label(bot, sandbox_home, monkeypatch):
    monkeypatch.setattr(bot, "_check_access", _allow_access)
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    upd = FakeUpdate()
    run(bot._cmd_usage_retry(upd, FakeContext(["../../etc/passwd"])))
    assert called["popen"] == 0
    assert upd.message.replies == [messages.USAGE_RETRY_BAD_LABEL]


def test_slash_denied_without_access(bot, sandbox_home, monkeypatch):
    async def _deny(update):
        return False
    monkeypatch.setattr(bot, "_check_access", _deny)
    called = {"popen": 0}
    monkeypatch.setattr(bot_mod.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", called["popen"] + 1))
    upd = FakeUpdate()
    run(bot._cmd_usage_retry(upd, FakeContext([LABEL])))
    assert called["popen"] == 0
    assert upd.message.replies == []
