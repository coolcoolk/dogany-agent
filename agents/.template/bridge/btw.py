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
from typing import Any, Dict, List, Optional, Set

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from bridge import messages
from bridge.config import CLAUDE_MAX_BUFFER_SIZE, PROCESS_TIMEOUT, resolve_claude_cli
from bridge.sdk_bridge import PROJECT_ROOT as _PROJECT_ROOT
from bridge.sdk_bridge import (
    ALLOWED_TOOLS,
    _ANSI_RE,
    _compose_system_prompt,
    _register_guard,
    _scaffold_guard,
)

logger = logging.getLogger(__name__)

# Maximum seconds to wait for a fork turn response.
#
# DGN-1177: was min(PROCESS_TIMEOUT, 120) on the assumption that side
# questions are lightweight. Falsified live (bot.log, 2026-08-20):
# the FIRST fork turn pays cold CLI spawn + a full fork of the main session
# history (multi-MB for a long-lived session) + the model call on that full
# context, and kept exceeding 120s (also documented in the DGN-953 fork-leak
# test), so every /btw on a heavy session died as BTW_FORK_FAILED. A fork
# turn does strictly MORE work than a main turn, so it gets the same budget.
# Fork tasks run in the separate _btw_fork_tasks set (DGN-922 FIX 4), so a
# long fork turn never blocks the main conversation.
BTW_TURN_TIMEOUT = PROCESS_TIMEOUT

# Per-user cap on live fork state entries (reply-to message_ids tracked).
# Older entries evicted LRU when the cap is hit -- prevents unbounded growth
# from a user who spams /btw without ever replying to any fork bubble.
_BTW_MAX_FORKS_PER_USER = 10

# Worst-case seconds the SDK transport needs to shut a fork CLI child down.
# Read off the subprocess transport's own close() ladder: up to 5s to take the
# stdin write lock, up to 5s waiting for a graceful exit after stdin EOF, up
# to 5s after SIGTERM, up to 5s after SIGKILL.
_SDK_SHUTDOWN_LADDER = 20.0

# Budget for one fork reap (client disconnect).
#
# DGN-1343: this was 3.0, i.e. SHORTER than the ladder above, and that is not
# merely "give up waiting" -- close() runs its terminate/kill escalation
# inside an anyio shield, and an asyncio.wait_for cancellation pierces that
# shield, so the escalation is SKIPPED. Measured against the real transport
# with a child that ignores stdin EOF (the shape of a fork still working on
# its turn): a 3.0s budget aborts close() with TimeoutError and the child
# survives; a 20.0s budget reaps it. So the short budget turned every reap
# into a no-op for exactly the busy forks that need reaping.
#
# This is a backstop, not a delay: an idle fork exits on stdin EOF in
# milliseconds, and fork turns run off the main conversation lane, so the
# worst case cannot stall the main session.
_FORK_REAP_TIMEOUT = _SDK_SHUTDOWN_LADDER + 5.0


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
        # DGN-1343: strong references to in-flight reaps. asyncio keeps only a
        # weak reference to a running task, so a reap with no owner here can be
        # garbage-collected mid-flight and leave the CLI child alive. Entries
        # remove themselves on completion.
        self._pending_reaps: Set["asyncio.Task[None]"] = set()

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
                self._spawn_reap(evicted.client)
        forks[state.anchor_message_id] = state

    @staticmethod
    async def _quiet_disconnect(client: ClaudeSDKClient) -> None:
        """Disconnect a fork SDK client, swallowing all errors.

        Disconnecting is what actually kills the forked CLI child, so the
        budget has to outlast the transport's own shutdown ladder -- see
        _FORK_REAP_TIMEOUT.
        """
        try:
            await asyncio.wait_for(client.disconnect(), timeout=_FORK_REAP_TIMEOUT)
        except Exception as e:
            logger.debug("btw fork client disconnect failed (non-fatal): %s", e)

    def _spawn_reap(self, client: ClaudeSDKClient) -> "asyncio.Task[None]":
        """Start a tracked background reap of one fork client."""
        task = asyncio.create_task(self._quiet_disconnect(client))
        self._pending_reaps.add(task)
        task.add_done_callback(self._pending_reaps.discard)
        return task

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
        """Reap a fork the bridge has given up on.

        DGN-953 Phase-1b: a failed first turn used to raise without
        disconnecting the fork client -- the forked CLI subprocess
        (claude --fork-session) stayed alive as a permanent orphan, one new
        zombie per retry. Disconnect the client (fail-soft) and deregister the
        fork so the dead state is never reused. Safe when client is None or
        already disconnected; the original failure still propagates.

        DGN-1343: deregister FIRST, and shield the disconnect. /stop cancels
        outstanding fork tasks, so this coroutine is routinely reached while
        its own task is being cancelled; an unshielded await would hand that
        cancellation straight to the disconnect -- killing the reap instead of
        the fork -- and would also skip the deregistration below it.
        """
        client = fork.client
        fork.client = None
        self._deregister_fork(user_id, fork)
        if client is not None:
            await asyncio.shield(self._spawn_reap(client))

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
        cli_path = resolve_claude_cli()  # DGN-1814: same CLI as the main session
        if cli_path:
            opts["cli_path"] = cli_path
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

        DGN-1343: this method is the SINGLE reap decision point for a fork.
        Reaping used to be wired into each individual failure branch inside the
        turn bodies, and the branch that returns empty text was never wired up
        -- so the bridge told the user the side question had failed while the
        fork's CLI child kept running under the same working directory, the
        same identity and the same tool permissions, acting on its own
        (observed: it wrote to a real ledger). The same shape of hole had
        already been fixed once per entry point (DGN-034) and grew back at the
        next new entry point, so the decision lives at the one gate every turn
        must pass instead: any outcome that is not usable text -- raise,
        timeout, cancellation, or empty -- reaps here. A new failure branch
        added inside a turn body cannot reintroduce the leak.
        """
        async with fork.lock:
            try:
                if not fork.initialized:
                    answer = await self._run_first_turn(user_id, fork, question)
                else:
                    answer = await self._run_continuation_turn(user_id, fork, question)
            except BaseException:
                # BaseException, not Exception: /stop cancels fork tasks and a
                # cancelled turn leaks exactly like a failed one.
                await self._cleanup_failed_turn(user_id, fork)
                raise
            if not answer:
                await self._cleanup_failed_turn(user_id, fork)
            return answer

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
        # DGN-1343: publish the client on the fork BEFORE connecting. connect()
        # is what spawns the CLI child, so a client that is only held in this
        # local is already reapable-but-unreachable if connect raises midway --
        # publishing first is what lets the single reap gate in run_fork_turn
        # own every client this manager ever creates.
        fork.client = client
        try:
            await client.connect()
        except Exception as e:
            logger.error("btw fork connect failed for user %s: %s", user_id, e)
            raise

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
                    if _is_stray_result(msg, stats):
                        continue
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
            # A clientless fork is unusable; raising drops it at the reap gate
            # so replies stop hitting a dead entry.
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
                    if _is_stray_result(msg, stats):
                        continue
                    break

        try:
            await asyncio.wait_for(_read(), timeout=BTW_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("btw fork continuation timed out for user %s", user_id)
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
        "stray_results": 0,
        "result_is_error": False,
    }


def _is_stray_result(msg: ResultMessage, stats: Dict[str, Any]) -> bool:
    """DGN-1408: is this ResultMessage a stray turn boundary, not our answer?

    receive_messages() yields every frame on the transport, but a forked or
    resumed session can run turns the bridge never asked for: Claude Code
    auto-enqueues the resumed session's pending background-task notifications
    as inputs at CLI startup, opens a turn for each, and emits a ResultMessage
    per turn. Measured live (bot.log 2026-09-08 18:47:58 + fork transcript
    45822502): the notification was enqueued 20ms before the bridge's
    question, its turn was coalesced away with zero assistant output, and its
    ResultMessage reached the reader FIRST -- the old unconditional break
    adopted it as the answer, told the user the side question failed, and the
    real answer streamed into a reader that had already returned.

    A result frame is stray exactly when the model has said nothing yet in
    this read (assistant_msgs == 0) and the result is not an error. Both
    other shapes keep the old contract: an error result is an honest failure
    and ends the read; a result after any AssistantMessage is our turn's
    boundary (genuine empty answers stay instant). The skip is bounded by the
    caller's BTW_TURN_TIMEOUT wait_for, so a stream that only ever produces
    stray results still ends at the timeout and reaps at the DGN-1343 gate.
    """
    if getattr(msg, "is_error", False):
        stats["result_is_error"] = True
        return False
    if stats["assistant_msgs"] > 0:
        return False
    stats["stray_results"] += 1
    # ESTATE:BEGIN ticket id in the stray-skip log line -- estate operators grep logs by ticket; a public runtime string carries no internal ticket id (publish gate T tier 2)
    logger.info(
        "DGN-1408: skipping stray fork turn result #%d (no assistant "
        "message yet in this read; likely an auto-enqueued task-notification "
        "turn); continuing to read",
        stats["stray_results"],
    )
    # ESTATE:ELSE
    # logger.info(
    #     "skipping stray fork turn result #%d (no assistant "
    #     "message yet in this read; likely an auto-enqueued task-notification "
    #     "turn); continuing to read",
    #     stats["stray_results"],
    # )
    # ESTATE:END
    return True


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
      - stray_results>0 (DGN-1408): non-error results with no assistant
        output were skipped and the answer still never arrived.
      - result_is_error=True (DGN-1408): the turn ended on an error result.
    Privacy: only counts/lengths/flags are logged -- never the question or
    response content.
    """
    logger.warning(
        "DGN-953 btw fork %s turn returned empty text for user %s: "
        "assistant_msgs=%d parent_skipped=%d text_blocks=%d raw_len=%d "
        "guarded_len=%d result_seen=%s fork_session_found=%s "
        "stray_results=%d result_is_error=%s",
        turn,
        user_id,
        stats["assistant_msgs"],
        stats["parent_skipped"],
        stats["text_blocks"],
        stats["raw_len"],
        guarded_len,
        stats["result_seen"],
        fork_session_found,
        stats["stray_results"],
        stats["result_is_error"],
    )


def _clean_response(text: str) -> str:
    """Strip ANSI codes and non-printable chars, then strip whitespace."""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32 or c in "\n\r\t")
    return cleaned.strip()
