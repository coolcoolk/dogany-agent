"""DGN-902: /btw fork -- ephemeral side conversation.

Forks the current main session context into an isolated SDK client using
ClaudeAgentOptions(fork_session=True, resume=<session_id>). The fork:
  - READ: inherits the full session history up to the fork point.
  - WRITE: writes to its own new session ID, never to the main session.

Fork state is keyed by the message_id of the 💭 bubble sent in reply.
Subsequent user messages that reply to any known fork bubble are routed
into that fork's session instead of the main session history.

Public surface (consumed by bot.py):
  - BtwForkState: dataclass representing one fork's live state.
  - BtwForkManager: per-bot instance that owns the fork table and SDK clients.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from bridge import messages
from bridge.config import CLAUDE_CLI_PATH, CLAUDE_MAX_BUFFER_SIZE, PROCESS_TIMEOUT
from bridge.sdk_bridge import PROJECT_ROOT as _PROJECT_ROOT
from bridge.sdk_bridge import (
    ALLOWED_TOOLS,
    _ANSI_RE,
    _compose_system_prompt,
    _register_guard,
    _scaffold_guard,
)

logger = logging.getLogger(__name__)

# Maximum seconds to wait for a fork turn response (shorter than the main
# PROCESS_TIMEOUT since side questions are expected to be lightweight).
BTW_TURN_TIMEOUT = min(PROCESS_TIMEOUT, 120)

# Per-user cap on live fork state entries (reply-to message_ids tracked).
# Older entries evicted LRU when the cap is hit -- prevents unbounded growth
# from a user who spams /btw without ever replying to any fork bubble.
_BTW_MAX_FORKS_PER_USER = 10


@dataclass
class BtwForkState:
    """Runtime state for a single btw fork session.

    Keyed by the message_id of the 💭 bubble (the first fork reply sent to
    the user). The fork's session_id is discovered from the first SystemMessage
    emitted by the SDK after fork_session=True, and stored here so subsequent
    turns in this fork continue in the same isolated session.
    """

    # The message_id of the 💭 bubble that anchors this fork in Telegram.
    anchor_message_id: int
    # The main session_id this fork was spawned from (read, not written).
    spawned_from_session_id: str
    # The fork's own session_id (discovered from the first SDK SystemMessage).
    # None until the first SDK response arrives.
    fork_session_id: Optional[str] = None
    # The dedicated SDK client for this fork. Connected once, reused for all
    # subsequent turns in this fork. None until the fork's first turn is sent.
    client: Optional[ClaudeSDKClient] = None
    # Lock: only one fork turn runs at a time per fork (no concurrent questions
    # within the same fork session).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # True after the first turn (fork_session_id discovered); a connected
    # client with fork_session_id is ready for subsequent turns.
    initialized: bool = False


class BtwForkManager:
    """Owns the fork table and SDK client lifecycle for all /btw forks.

    One instance per TelegramBot. The table is:
      _forks: Dict[user_id, Dict[anchor_message_id, BtwForkState]]

    The LRU eviction cap (_BTW_MAX_FORKS_PER_USER) prevents unbounded growth.
    """

    def __init__(self) -> None:
        # user_id -> {anchor_message_id -> BtwForkState}
        self._forks: Dict[int, Dict[int, BtwForkState]] = {}

    def _user_forks(self, user_id: int) -> Dict[int, BtwForkState]:
        return self._forks.setdefault(user_id, {})

    def lookup_fork(self, user_id: int, anchor_message_id: int) -> Optional[BtwForkState]:
        """Return the fork anchored at anchor_message_id for user_id, or None."""
        return self._user_forks(user_id).get(anchor_message_id)

    def register_fork(self, user_id: int, state: BtwForkState) -> None:
        """Register a new fork. Evicts LRU entries when the per-user cap is hit."""
        forks = self._user_forks(user_id)
        if len(forks) >= _BTW_MAX_FORKS_PER_USER:
            # Evict the oldest entry (dict preserves insertion order in Python 3.7+).
            oldest_key = next(iter(forks))
            evicted = forks.pop(oldest_key)
            logger.debug(
                "btw LRU evict for user %s: anchor_mid=%d", user_id, oldest_key
            )
            # Best-effort disconnect the evicted fork's client without blocking.
            if evicted.client is not None:
                asyncio.create_task(self._quiet_disconnect(evicted.client))
        forks[state.anchor_message_id] = state

    @staticmethod
    async def _quiet_disconnect(client: ClaudeSDKClient) -> None:
        """Disconnect a fork SDK client, swallowing all errors."""
        try:
            await asyncio.wait_for(client.disconnect(), timeout=3.0)
        except Exception as e:
            logger.debug("btw fork client disconnect failed (non-fatal): %s", e)

    def _make_fork_client(self, session_id: str) -> ClaudeSDKClient:
        """Build a new ClaudeSDKClient configured to fork from session_id.

        fork_session=True + resume=session_id: the SDK reads the named session
        history and writes subsequent turns to a BRAND NEW session_id, so the
        main session is never polluted.
        """
        opts: Dict[str, Any] = {
            "cwd": str(_PROJECT_ROOT),
            "allowed_tools": ALLOWED_TOOLS,
            "disallowed_tools": ["AskUserQuestion"],
            "system_prompt": _compose_system_prompt(),
            "permission_mode": "default",
            "max_buffer_size": CLAUDE_MAX_BUFFER_SIZE,
            # Core fork magic: resume the main session context, but write all
            # turns to a new isolated session ID.
            "fork_session": True,
            "resume": session_id,
        }
        if CLAUDE_CLI_PATH:
            opts["cli_path"] = CLAUDE_CLI_PATH
        return ClaudeSDKClient(options=ClaudeAgentOptions(**opts))

    async def run_fork_turn(
        self,
        user_id: int,
        fork: BtwForkState,
        question: str,
    ) -> str:
        """Run one turn in the given fork and return the assembled response text.

        First turn: creates and connects the client, seeds the fork session from
        the main session via fork_session=True + resume=<session_id>.
        Subsequent turns: reuse the connected client, continuing with the
        fork's own session_id (isolated from main).

        Returns the response text string (clean, scaffold- and register-guarded).
        Raises on timeout or unrecoverable error (caller converts to user-facing
        error notice).
        """
        async with fork.lock:
            if not fork.initialized:
                return await self._run_first_turn(user_id, fork, question)
            return await self._run_continuation_turn(user_id, fork, question)

    async def _run_first_turn(
        self,
        user_id: int,
        fork: BtwForkState,
        question: str,
    ) -> str:
        """Create + connect a new fork client and run the first question.

        The fork client uses fork_session=True, so the SDK reads the named
        main session but writes output to a new session_id. We capture that
        new session_id from the first SystemMessage so subsequent turns can
        continue in the same isolated fork.
        """
        client = self._make_fork_client(fork.spawned_from_session_id)
        try:
            await client.connect()
        except Exception as e:
            logger.error("btw fork connect failed for user %s: %s", user_id, e)
            raise

        fork.client = client
        fork_session_id: Optional[str] = None
        texts: List[str] = []

        async def _read() -> None:
            nonlocal fork_session_id
            await client.query(question, session_id=fork.spawned_from_session_id)
            async for msg in client.receive_messages():
                if isinstance(msg, SystemMessage):
                    data = getattr(msg, "data", None)
                    sid = data.get("session_id") if isinstance(data, dict) else None
                    if sid:
                        fork_session_id = sid
                elif isinstance(msg, AssistantMessage):
                    if getattr(msg, "session_id", None):
                        fork_session_id = msg.session_id
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            guarded = _register_guard(_scaffold_guard(block.text))
                            if guarded:
                                texts.append(guarded)
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        fork_session_id = msg.session_id
                    break

        try:
            await asyncio.wait_for(_read(), timeout=BTW_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("btw fork first turn timed out for user %s", user_id)
            raise

        if fork_session_id:
            fork.fork_session_id = fork_session_id
            fork.initialized = True
        else:
            # No session_id discovered -- still mark initialized so we don't
            # loop. Subsequent turns use "default" which may start fresh, but
            # that is safer than hanging.
            fork.initialized = True
            logger.warning(
                "btw fork: no session_id from first turn for user %s; "
                "subsequent turns may not chain correctly",
                user_id,
            )

        raw = "\n".join(texts)
        return _clean_response(raw)

    async def _run_continuation_turn(
        self,
        user_id: int,
        fork: BtwForkState,
        question: str,
    ) -> str:
        """Run a follow-up question in an established fork session.

        Uses the fork's own session_id (fork_session_id), NOT the main session.
        The client was already connected during the first turn.
        """
        client = fork.client
        if client is None:
            raise RuntimeError("btw fork continuation called but client is None")

        session_id = fork.fork_session_id or "default"
        texts: List[str] = []

        async def _read() -> None:
            await client.query(question, session_id=session_id)
            async for msg in client.receive_messages():
                if isinstance(msg, SystemMessage):
                    pass
                elif isinstance(msg, AssistantMessage):
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            guarded = _register_guard(_scaffold_guard(block.text))
                            if guarded:
                                texts.append(guarded)
                elif isinstance(msg, ResultMessage):
                    break

        try:
            await asyncio.wait_for(_read(), timeout=BTW_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("btw fork continuation timed out for user %s", user_id)
            raise

        raw = "\n".join(texts)
        return _clean_response(raw)


def _clean_response(text: str) -> str:
    """Strip ANSI codes and non-printable chars, then strip whitespace."""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32 or c in "\n\r\t")
    return cleaned.strip()
