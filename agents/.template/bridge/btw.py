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

    def _deregister_fork(self, user_id: int, fork: BtwForkState) -> None:
        """Remove the given fork from the table, if it is still registered.

        Identity-checked: only removes the entry when the stored object IS
        this fork, so a different (healthy) fork that happens to sit under
        the same anchor_message_id is never evicted.
        """
        forks = self._forks.get(user_id)
        if forks is not None and forks.get(fork.anchor_message_id) is fork:
            del forks[fork.anchor_message_id]
            logger.info(
                "DGN-953: deregistered failed btw fork for user %s (anchor_mid=%d)",
                user_id,
                fork.anchor_message_id,
            )

    async def _cleanup_failed_turn(self, user_id: int, fork: BtwForkState) -> None:
        """DGN-953 Phase-1b: reap a fork whose turn timed out or raised.

        Before this, a timed-out first turn raised without disconnecting the
        fork client -- the forked CLI subprocess (claude --fork-session)
        stayed alive as a permanent orphan (RAM leak), one new zombie per
        retry. Disconnect the client (fail-soft) and deregister the fork so
        the dead state is never reused. Safe when client is None or already
        disconnected; the original failure still propagates to the caller.
        """
        client = fork.client
        fork.client = None
        if client is not None:
            await self._quiet_disconnect(client)
        self._deregister_fork(user_id, fork)

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
            # DGN-953: reap any partially-spawned CLI and drop the dead fork.
            await self._quiet_disconnect(client)
            self._deregister_fork(user_id, fork)
            raise

        fork.client = client
        fork_session_id: Optional[str] = None
        texts: List[str] = []
        # DGN-953: cause-signal counters for the empty-turn diagnostic line.
        stats = _new_turn_stats()

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
                    stats["assistant_msgs"] += 1
                    if getattr(msg, "session_id", None):
                        fork_session_id = msg.session_id
                    if getattr(msg, "parent_tool_use_id", None):
                        stats["parent_skipped"] += 1
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            stats["text_blocks"] += 1
                            stats["raw_len"] += len(block.text)
                            guarded = _register_guard(_scaffold_guard(block.text))
                            if guarded:
                                texts.append(guarded)
                elif isinstance(msg, ResultMessage):
                    stats["result_seen"] = True
                    if msg.session_id:
                        fork_session_id = msg.session_id
                    break

        try:
            await asyncio.wait_for(_read(), timeout=BTW_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("btw fork first turn timed out for user %s", user_id)
            # DGN-953 Phase-1b: without this, the fork CLI subprocess stayed
            # alive forever after every 120s timeout (orphan RAM leak).
            await self._cleanup_failed_turn(user_id, fork)
            raise
        except Exception:
            await self._cleanup_failed_turn(user_id, fork)
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
        cleaned = _clean_response(raw)
        if not cleaned:
            _log_empty_turn(
                "first",
                user_id,
                stats,
                guarded_len=len(raw),
                fork_session_found=bool(fork_session_id),
            )
        return cleaned

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
            # DGN-953: a clientless fork is unusable -- drop it so replies do
            # not keep hitting a dead entry.
            self._deregister_fork(user_id, fork)
            raise RuntimeError("btw fork continuation called but client is None")

        session_id = fork.fork_session_id or "default"
        texts: List[str] = []
        # DGN-953: cause-signal counters for the empty-turn diagnostic line.
        stats = _new_turn_stats()

        async def _read() -> None:
            await client.query(question, session_id=session_id)
            async for msg in client.receive_messages():
                if isinstance(msg, SystemMessage):
                    pass
                elif isinstance(msg, AssistantMessage):
                    stats["assistant_msgs"] += 1
                    if getattr(msg, "parent_tool_use_id", None):
                        stats["parent_skipped"] += 1
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            stats["text_blocks"] += 1
                            stats["raw_len"] += len(block.text)
                            guarded = _register_guard(_scaffold_guard(block.text))
                            if guarded:
                                texts.append(guarded)
                elif isinstance(msg, ResultMessage):
                    stats["result_seen"] = True
                    break

        try:
            await asyncio.wait_for(_read(), timeout=BTW_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("btw fork continuation timed out for user %s", user_id)
            # DGN-953 Phase-1b: same orphan-CLI reap as the first turn.
            await self._cleanup_failed_turn(user_id, fork)
            raise
        except Exception:
            await self._cleanup_failed_turn(user_id, fork)
            raise

        raw = "\n".join(texts)
        cleaned = _clean_response(raw)
        if not cleaned:
            _log_empty_turn(
                "continuation",
                user_id,
                stats,
                guarded_len=len(raw),
                fork_session_found=bool(fork.fork_session_id),
            )
        return cleaned


def _new_turn_stats() -> Dict[str, Any]:
    """DGN-953: fresh cause-signal counters for one fork turn."""
    return {
        "assistant_msgs": 0,
        "parent_skipped": 0,
        "text_blocks": 0,
        "raw_len": 0,
        "result_seen": False,
    }


def _log_empty_turn(
    turn: str,
    user_id: int,
    stats: Dict[str, Any],
    *,
    guarded_len: int,
    fork_session_found: bool,
) -> None:
    """DGN-953: one-line diagnostic when a fork turn yields empty text.

    Before this, an empty fork turn produced ZERO log lines -- the user saw
    BTW_FORK_FAILED with no trace to diagnose from. Mirrors the DGN-876
    fold-drop pattern: enumerate every cause signal instead of naming one.
    The counts let the log reader distinguish:
      - assistant_msgs=0 / text_blocks=0: no assistant text arrived at all
        (result_seen tells whether a ResultMessage-only turn happened).
      - raw_len>0, guarded_len=0: TextBlocks arrived but the scaffold/register
        guards stripped every block.
      - guarded_len>0: guarded text survived but _clean_response (ANSI /
        non-printable / whitespace strip) emptied the remainder.
      - fork_session_found=False: fork session id was never discovered.
    Privacy: only counts/lengths/flags are logged -- never the question or
    response content.
    """
    logger.warning(
        "DGN-953 btw fork %s turn returned empty text for user %s: "
        "assistant_msgs=%d parent_skipped=%d text_blocks=%d raw_len=%d "
        "guarded_len=%d result_seen=%s fork_session_found=%s",
        turn,
        user_id,
        stats["assistant_msgs"],
        stats["parent_skipped"],
        stats["text_blocks"],
        stats["raw_len"],
        guarded_len,
        stats["result_seen"],
        fork_session_found,
    )


def _clean_response(text: str) -> str:
    """Strip ANSI codes and non-printable chars, then strip whitespace."""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32 or c in "\n\r\t")
    return cleaned.strip()
