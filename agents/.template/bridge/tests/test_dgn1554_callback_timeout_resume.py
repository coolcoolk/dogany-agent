"""DGN-1554: slash-command / options / retry callbacks never checked
`timed_out` -- auto-resume was never even attempted on those three paths,
even though the two "normal" paths (plain text turn, tap-to-continue resume
callback) already did the right thing.

Fix shape: every SDK-turn call site routes its response through ONE seam
instead of hand-checking `timed_out` at the call site. In this tree that seam
is `TelegramBot._finish_turn_with_auto_resume` (DGN-1523 closed the same three
bypass paths first; an earlier public cut named the seam `_finish_turn`). The
seam runs `_auto_resume_loop` and, if the turn is still timed out afterwards
(AUTO_RESUME off, or resumes exhausted), sends the tap-to-continue notice and
returns `None` -- the caller must stop. A call site that skips this seam
cannot special-case timeout at all, because it never sees `timed_out`
directly. Here the session id is persisted by the caller right after the seam
returns a live response (DGN-1523 shape), not inside the seam.

This file pins:
  1. the structural invariant (exactly one seam, no call site hand-rolls the
     `timed_out` check any more -- DGN-1554's own coming-back clause);
  2. behavioral coverage for the seam in isolation (success passthrough,
     AUTO_RESUME off, resumes exhausted);
  3. end-to-end coverage that each of the three previously-broken call sites
     (`_exec_slash_command`, the `opt:` callback, `_handle_retry_callback`)
     actually attempts auto-resume on a timed-out turn.
"""

import ast
import asyncio
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bridge.tests.conftest  # noqa: F401 -- hermetic PROJECT_ROOT / TOKEN

from bridge import bot as bot_mod
from bridge import messages
from bridge import sdk_bridge as sdk_bridge_mod
from bridge.sdk_bridge import ChatResponse


# ---------------------------------------------------------------------------
# 1. Structural invariant: ONE seam, no duplicated `timed_out` branches.
# ---------------------------------------------------------------------------


def _bot_tree():
    return ast.parse(inspect.getsource(bot_mod))


def _enclosing_funcs_with_timed_out_check(tree):
    """Return the name of every function/method whose body directly contains
    a `getattr(<x>, "timed_out", ...)` call (not inside a nested def)."""
    hits = []

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "getattr"
                and len(child.args) >= 2
                and isinstance(child.args[1], ast.Constant)
                and child.args[1].value == "timed_out"
            ):
                hits.append(owner)
            walk(child, owner)

    walk(tree, None)
    return hits


def test_timed_out_is_only_checked_inside_the_seam():
    """Only `_auto_resume_loop` (the loop condition) and
    `_finish_turn_with_auto_resume` (the seam itself) may check `timed_out`. A
    sixth call site that hand-rolls its own check instead of calling the seam
    would show up here."""
    owners = set(_enclosing_funcs_with_timed_out_check(_bot_tree()))
    allowed = {"_auto_resume_loop", "_finish_turn_with_auto_resume"}
    assert owners == allowed, (
        f"timed_out is checked outside the single seam: {owners - allowed}"
    )


def test_five_call_sites_route_through_the_seam():
    """Every SDK-turn call site (2 previously-working + 3 previously-broken)
    calls `self._finish_turn_with_auto_resume(...)` exactly once each -- 5
    total. This is the DGN-665 TestCallSiteWiring count (5 request-response
    seats), reused here to pin that each of those 5 now shares the timeout seam too."""
    tree = _bot_tree()
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_finish_turn_with_auto_resume"
    ]
    assert len(calls) == 5, f"expected 5 seam call sites, got {len(calls)}"


# ---------------------------------------------------------------------------
# Shared test harness (mirrors test_dgn911 / test_dgn922 style).
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        sent_msg = MagicMock()
        sent_msg.message_id = 9999
        return sent_msg

    async def send_chat_action(self, *a, **k):
        pass


class _FakeApp:
    def __init__(self, fake_bot):
        self.bot = fake_bot


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def send_action(self, *a, **k):
        pass


class _FakeMessage:
    def __init__(self, chat, date=None):
        self.chat = chat
        self.date = date or datetime.now(timezone.utc)
        self.message_id = 1
        self.caption = None
        self.replies = []

    async def reply_text(self, text, *a, **k):
        self.replies.append(text)


class _FakeQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, *a, **k):
        self.edits.append(text)


class _FakeUpdate:
    def __init__(self, chat_id=1, date=None, user_id=None, callback_data=None):
        chat = _FakeChat(chat_id)
        self.effective_chat = chat
        self.message = _FakeMessage(chat, date=date)
        self.effective_user = SimpleNamespace(id=user_id if user_id else chat_id)
        if callback_data is not None:
            self.callback_query = _FakeQuery(callback_data, message=self.message)
        else:
            self.callback_query = None


def _make_bot():
    b = bot_mod.TelegramBot()
    b.application = _FakeApp(_FakeBot())
    return b


def _upd(user_id, callback_data=None):
    return _FakeUpdate(chat_id=user_id, user_id=user_id, callback_data=callback_data)


async def _await_all_tasks(b, user_id, max_rounds=12):
    seen = set()
    for _ in range(max_rounds):
        tasks = [t for t in b._user_run_tasks.get(user_id, set()) if t not in seen]
        if not tasks:
            break
        seen.update(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


def _timed_out_response(resume_sid="resume-sess"):
    return ChatResponse(content="", timed_out=True, resume_session_id=resume_sid)


def _final_response(text="all done", session_id="final-sess"):
    return ChatResponse(content=text, session_id=session_id)


# ---------------------------------------------------------------------------
# 2. The seam in isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_turn_success_passthrough(monkeypatch):
    b = _make_bot()
    user_id = 1554

    async def resume_caller(cont):
        raise AssertionError("resume_caller must not be invoked on a non-timed-out response")

    saved = []
    monkeypatch.setattr(
        b, "_save_session_id", AsyncMock(side_effect=lambda uid, r: saved.append((uid, r)))
    )
    # In this tree the CALLER persists the session id after the seam hands
    # back a live response (DGN-1523 shape); the seam itself only settles.

    response = _final_response()
    result = await b._finish_turn_with_auto_resume(
        user_id=user_id, chat_id=user_id, response=response, resume_caller=resume_caller
    )
    assert result is response
    assert saved == []


@pytest.mark.asyncio
async def test_finish_turn_auto_resume_off_sends_button_exactly_once(monkeypatch):
    """AUTO_RESUME off: _auto_resume_loop's while-condition short-circuits
    immediately (never calls resume_caller), and the seam falls straight
    to the button notice -- sent exactly once, no STILL_WORKING at all."""
    b = _make_bot()
    user_id = 1555
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", False)

    async def resume_caller(cont):
        raise AssertionError("resume must not be attempted while AUTO_RESUME is off")

    response = _timed_out_response()
    result = await b._finish_turn_with_auto_resume(
        user_id=user_id, chat_id=user_id, response=response, resume_caller=resume_caller
    )
    assert result is None
    fake_bot = b.application.bot
    still_working = [t for _, t in fake_bot.sent if t == messages.STILL_WORKING]
    button_notices = [t for _, t in fake_bot.sent if t == messages.TIMEOUT_TAP_NOTICE]
    assert still_working == [], "no auto-resume attempted -> no STILL_WORKING notice"
    assert len(button_notices) == 1, f"expected exactly one button notice, got {fake_bot.sent}"


@pytest.mark.asyncio
async def test_finish_turn_resumes_exhausted_falls_through_to_button(monkeypatch):
    """AUTO_RESUME on, every resume attempt still times out: STILL_WORKING is
    sent exactly once (dedup, not once per attempt), then once resumes are
    exhausted the button notice fires -- exactly once, no double-send."""
    b = _make_bot()
    user_id = 1556
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 2)

    attempts = []

    async def resume_caller(cont):
        attempts.append(cont)
        return _timed_out_response()

    response = _timed_out_response()
    result = await b._finish_turn_with_auto_resume(
        user_id=user_id, chat_id=user_id, response=response, resume_caller=resume_caller
    )
    assert result is None
    assert len(attempts) == 2, "must retry up to AUTO_RESUME_MAX before giving up"
    fake_bot = b.application.bot
    still_working = [t for _, t in fake_bot.sent if t == messages.STILL_WORKING]
    button_notices = [t for _, t in fake_bot.sent if t == messages.TIMEOUT_TAP_NOTICE]
    assert len(still_working) == 1, f"STILL_WORKING must be deduped, got {fake_bot.sent}"
    assert len(button_notices) == 1, f"expected exactly one button notice, got {fake_bot.sent}"


@pytest.mark.asyncio
async def test_finish_turn_resume_succeeds_no_button(monkeypatch):
    """AUTO_RESUME on, the first resume attempt returns a live response: no
    button notice, the seam hands back the resumed response."""
    b = _make_bot()
    user_id = 1557
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 2)

    async def resume_caller(cont):
        return _final_response(text="resumed!")

    response = _timed_out_response()
    result = await b._finish_turn_with_auto_resume(
        user_id=user_id, chat_id=user_id, response=response, resume_caller=resume_caller
    )
    assert result is not None
    assert result.content == "resumed!"
    fake_bot = b.application.bot
    button_notices = [t for _, t in fake_bot.sent if t == messages.TIMEOUT_TAP_NOTICE]
    assert button_notices == []


# ---------------------------------------------------------------------------
# 3. End-to-end: the three previously-broken call sites actually auto-resume.
# ---------------------------------------------------------------------------


def _patch_process_message(monkeypatch, responses):
    """responses: list of ChatResponse consumed in order across calls."""
    calls = []

    async def fake_process_message(*, user_message, **kwargs):
        calls.append(user_message)
        return responses[len(calls) - 1]

    monkeypatch.setattr(sdk_bridge_mod.sdk_bridge, "process_message", fake_process_message)
    return calls


@pytest.mark.asyncio
async def test_exec_slash_command_auto_resumes_on_timeout(monkeypatch):
    b = _make_bot()
    user_id = 15541
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 2)
    calls = _patch_process_message(
        monkeypatch, [_timed_out_response(), _final_response(text="slash resumed")]
    )

    update = _upd(user_id)
    await b._exec_slash_command(update, "/foo")
    await _await_all_tasks(b, user_id)

    assert len(calls) == 2, f"expected an initial call + one resume, got {calls}"
    assert calls[1] == messages.RESUME_CONTINUATION_PROMPT
    assert any("slash resumed" in r for r in update.message.replies), update.message.replies


@pytest.mark.asyncio
async def test_opt_callback_auto_resumes_on_timeout(monkeypatch):
    b = _make_bot()
    user_id = 15542
    monkeypatch.setattr(b, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 2)
    calls = _patch_process_message(
        monkeypatch, [_timed_out_response(), _final_response(text="option resumed")]
    )

    update = _upd(user_id, callback_data="opt:1")
    ctx = MagicMock()
    await b._handle_callback(update, ctx)
    await _await_all_tasks(b, user_id)

    assert len(calls) == 2, f"expected an initial call + one resume, got {calls}"
    assert calls[1] == messages.RESUME_CONTINUATION_PROMPT
    fake_bot = b.application.bot
    assert any("option resumed" in t for _, t in fake_bot.sent), fake_bot.sent


@pytest.mark.asyncio
async def test_retry_callback_auto_resumes_on_timeout(monkeypatch):
    from bridge.session import session_manager

    b = _make_bot()
    user_id = 15543
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", True)
    monkeypatch.setattr(bot_mod, "AUTO_RESUME_MAX", 2)
    calls = _patch_process_message(
        monkeypatch, [_timed_out_response(), _final_response(text="retry resumed")]
    )

    await session_manager.update_session(
        user_id, {"pending_retry": {"token": "tok-1", "user_message": "original text"}}
    )
    update = _upd(user_id, callback_data="retry:tok-1")
    chat = update.effective_chat
    query = update.callback_query

    await b._handle_retry_callback(update, query, user_id, chat)
    await _await_all_tasks(b, user_id)

    assert len(calls) == 2, f"expected an initial call + one resume, got {calls}"
    assert calls[0] == "original text"
    assert calls[1] == messages.RESUME_CONTINUATION_PROMPT
    fake_bot = b.application.bot
    assert any("retry resumed" in t for _, t in fake_bot.sent), fake_bot.sent


@pytest.mark.asyncio
async def test_retry_callback_auto_resume_off_sends_button_once_no_double_send(monkeypatch):
    """AUTO_RESUME off on the previously-broken retry path: exactly one
    button notice, no STILL_WORKING, no reply carrying the raw timed-out
    content -- proves the fix doesn't just add resume but also keeps the
    existing dedup/no-double-send guarantee on a path that never had it."""
    from bridge.session import session_manager

    b = _make_bot()
    user_id = 15544
    monkeypatch.setattr(bot_mod, "AUTO_RESUME", False)
    _patch_process_message(monkeypatch, [_timed_out_response()])

    await session_manager.update_session(
        user_id, {"pending_retry": {"token": "tok-2", "user_message": "original text"}}
    )
    update = _upd(user_id, callback_data="retry:tok-2")
    chat = update.effective_chat
    query = update.callback_query

    await b._handle_retry_callback(update, query, user_id, chat)
    await _await_all_tasks(b, user_id)

    fake_bot = b.application.bot
    still_working = [t for _, t in fake_bot.sent if t == messages.STILL_WORKING]
    button_notices = [t for _, t in fake_bot.sent if t == messages.TIMEOUT_TAP_NOTICE]
    assert still_working == []
    assert len(button_notices) == 1, f"expected exactly one button notice, got {fake_bot.sent}"
