"""Per-user long-lived Claude SDK streaming bridge.

Each user gets a persistent ClaudeSDKClient. Messages are serialized: only one
query is in flight at a time (the reader_loop attributes all streamed text to
the head request), while later messages queue immediately for a fast-typing UX.
Handles streaming drafts, AskUserQuestion degradation, the timeout/preserve +
resume capture path, and a single reconnect-retry on transient SDK errors.
"""

import asyncio
import logging
import os
import re
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from bridge import messages
from bridge.config import (
    BRIDGE_SCAFFOLD_GUARD,
    CLAUDE_CLI_PATH,
    PROCESS_TIMEOUT,
    config,
)
from bridge.options import OPTIONS_MARKER, classify_is_choice, has_numbered_list
from bridge.permissions import extract_outside_paths, extract_protected_paths

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"]).resolve()

ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "NotebookEdit",
    "TodoWrite",
    "Bash",
]

TYPING_INTERVAL = 4  # seconds; Telegram typing status expires after ~5s

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# DGN-086: placeholder-flake detection pattern.
# Matches Korean phrases the agent uses when reporting a subagent
# delegation handoff, which a role-confused subagent echoes back verbatim
# instead of executing the task. Common observations:
#   - "동생이 아직 작업 중입니다. 완료 알림이 오면 결과를 먼저 보고드리겠습니다"
#   - "백그라운드 정찰 에이전트 완료 대기 중"
#   - "구현 서브에이전트 실행 중, 완료 통보 대기"
#   - Any variant of "<agent noun> 작업중/실행중/완료 대기/통보 대기"
_PLACEHOLDER_FLAKE_RE = re.compile(
    r"(동생이?\s*(아직\s*)?작업\s*중|"
    r"서브에이전트\s*(실행|작업)\s*중|"
    r"완료\s*(알림|통보)\S*\s*(대기|오면)|"
    r"백그라운드\s*(정찰\s*)?에이전트\s*완료\s*대기)",
    re.IGNORECASE,
)

# Permission callback: async (chat_id, user_id, tool_name, tool_input) -> result
PermissionCallback = Callable[[int, int, str, Dict[str, Any]], Awaitable]
TypingCallback = Callable[[], Awaitable[Any]]
# Proactive push callback: async (chat_id, content, has_options) -> None.
# Delivers main-agent output that has no pending request to answer.
ProactivePushCallback = Callable[[int, str, bool], Awaitable[Any]]

_NON_RETRYABLE = (
    "Invalid token",
    "Permission denied",
    "No such file",
    "Configuration error",
    "AttributeError",
    "KeyError",
    "ValueError",
    "TypeError",
)
_RETRYABLE_TYPES = (
    "TimeoutError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "BrokenPipeError",
    "OSError",
)
_RETRYABLE_MSG = ("timeout", "connection", "refused", "unreachable", "exit code -15", "exit code -9")


def _is_retryable_sdk_error(error: Exception) -> bool:
    msg = str(error)
    if any(p in msg for p in _NON_RETRYABLE):
        return False
    if type(error).__name__ in _RETRYABLE_TYPES:
        return True
    return any(p in msg.lower() for p in _RETRYABLE_MSG)


def _no_pending_guard(tool_name: str, tool_input: Any):
    """Default-deny guard for the no-pending (proactive/background) branch.

    With no user turn to answer a one-time confirm, a protected-zone or
    out-of-root path is hard-denied; everything else is allowed so background
    work still runs. (F4)
    """
    protected = extract_protected_paths(tool_name, tool_input, PROJECT_ROOT)
    outside = extract_outside_paths(
        tool_name, tool_input, PROJECT_ROOT, config.extra_allowed_roots
    )
    if protected or outside:
        return PermissionResultDeny(message=messages.OUTSIDE_PATH_DENY_NO_CONFIRM)
    return PermissionResultAllow()


# DGN-285 (leak class 2): harness-owned injection-signature line prefixes.
# Persona output can never legitimately OPEN a line with these -- they are
# emitted by the harness's UserPromptSubmit hook plumbing. Observed verbatim
# in model-side transcript-regurgitation leaks (Darkwarg + Warg, 2026-07-14),
# where the poison arrived INSIDE a genuine text block and the block-type
# filter could not help. Exact line-prefix match only, no fuzzy matching.
_SCAFFOLD_SIGNATURES = (
    "system UserPromptSubmit hook",
    "UserPromptSubmit hook additional context",
    "UserPromptSubmit hook success",
)


def _scaffold_guard(text: str) -> str:
    """Truncate outgoing user-facing text at the first scaffold-signature line.

    String-signature defense layer behind the structural block-type filter.
    Gated by BRIDGE_SCAFFOLD_GUARD (default on; channels that legitimately
    quote the signatures set it to 0). On truncation a WARNING with the
    dropped tail length is logged. If truncation would empty the text, the
    original is returned unchanged: the guard never blanks out a message.
    """
    if not BRIDGE_SCAFFOLD_GUARD or not text:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(_SCAFFOLD_SIGNATURES):
            kept = "".join(lines[:i]).rstrip()
            if not kept:
                logger.warning(
                    "Scaffold-leak guard: signature opens the text (%d chars); "
                    "left unchanged to avoid an empty message",
                    len(text),
                )
                return text
            logger.warning(
                "Scaffold-leak guard truncated outgoing text: dropped %d chars",
                len(text) - len(kept),
            )
            return kept
    return text


def _format_ask_user_question(tool_input: dict) -> str:
    """Degrade AskUserQuestion to plain numbered text for delivery."""
    lines: List[str] = []
    for q in tool_input.get("questions", []):
        question = q.get("question", "")
        if question:
            lines.append(question)
        options = q.get("options", [])
        if options:
            lines.append("")
        for i, opt in enumerate(options, 1):
            label = opt.get("label", "")
            desc = opt.get("description", "")
            lines.append(f"{i}. {label}" + (f" - {desc}" if desc else ""))
    return "\n".join(lines)


@dataclass
class ChatResponse:
    content: str
    success: bool = True
    error: Optional[str] = None
    session_id: Optional[str] = None
    has_options: bool = False
    streamed: bool = False
    timed_out: bool = False
    resume_session_id: Optional[str] = None
    partial_preserved: bool = False
    draft_message_ids: List[int] = field(default_factory=list)


@dataclass
class _PendingRequest:
    user_id: int
    chat_id: int
    model: Optional[str]
    requested_session_id: Optional[str]
    permission_callback: Optional[PermissionCallback]
    typing_callback: Optional[TypingCallback]
    future: asyncio.Future
    user_message: str = ""
    sent_session_id: str = "default"
    sent: bool = False
    last_typing_at: float = 0.0
    last_assistant_texts: List[str] = field(default_factory=list)
    synthetic_response: Optional[str] = None
    streaming_handler: Optional[Any] = None
    # DGN-086: count ToolUseBlocks in main-agent (non-subagent) messages for
    # placeholder-flake detection. Incremented in _reader_loop on each
    # AssistantMessage that has no parent_tool_use_id.
    tool_use_count: int = 0


@dataclass
class _UserStreamState:
    client: ClaudeSDKClient
    model: Optional[str]
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: Deque[_PendingRequest] = field(default_factory=deque)
    reader_task: Optional[asyncio.Task] = None
    typing_task: Optional[asyncio.Task] = None
    last_session_id: Optional[str] = None
    # Proactive push: delivery path for main-agent output that arrives with no
    # pending request (e.g. a subagent/background-task completion injects a new
    # turn into the main session). Captured from real requests in process_message.
    last_chat_id: Optional[int] = None
    proactive_push: Optional["ProactivePushCallback"] = None
    # Buffer for main-agent text blocks seen while pending is empty; flushed on
    # the trailing ResultMessage.
    proactive_texts: List[str] = field(default_factory=list)
    last_proactive_sent: Optional[str] = None


class SdkBridge:
    """Routes Telegram messages through per-user persistent SDK streams."""

    def __init__(self) -> None:
        self.project_root = PROJECT_ROOT
        self._streams: Dict[int, _UserStreamState] = {}
        self._stream_init_locks: Dict[int, asyncio.Lock] = {}
        logger.info("SdkBridge initialized for %s", self.project_root)

    def _get_stream_init_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._stream_init_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_init_locks[user_id] = lock
        return lock

    async def _create_user_stream(
        self, user_id: int, model: Optional[str]
    ) -> _UserStreamState:
        state_holder: Dict[str, _UserStreamState] = {}

        async def can_use_tool(tool_name, tool_input, _context=None):
            if tool_name == "AskUserQuestion" and isinstance(tool_input, dict):
                formatted = _format_ask_user_question(tool_input)
                s = state_holder.get("state")
                if s and s.pending:
                    s.pending[0].synthetic_response = formatted
                return PermissionResultDeny(message=messages.ASK_USER_QUESTION_DENY)
            state = state_holder.get("state")
            if not state or not state.pending:
                # No pending request => a proactive/background turn with no user
                # to answer a confirm prompt. Do NOT blanket-allow: still enforce
                # the guard as a default-deny for protected/out-of-root paths
                # (there is no interactive one-time confirm available here). Other
                # tools remain allowed so background work can proceed. (F4)
                return _no_pending_guard(tool_name, tool_input)
            req = state.pending[0]
            if not req.permission_callback:
                return PermissionResultAllow()
            result = await req.permission_callback(
                req.chat_id, user_id, tool_name, tool_input
            )
            if isinstance(result, (PermissionResultAllow, PermissionResultDeny)):
                return result
            return PermissionResultAllow() if result else PermissionResultDeny()

        opts: Dict[str, Any] = {
            "cwd": str(self.project_root),
            "allowed_tools": ALLOWED_TOOLS,
            "disallowed_tools": ["AskUserQuestion"],
            "system_prompt": messages.SYSTEM_PROMPT,
            "can_use_tool": can_use_tool,
            "permission_mode": "default",
        }
        if model:
            opts["model"] = model
        if CLAUDE_CLI_PATH:
            # SDK >=0.2 exposes a supported cli_path option (no monkeypatch needed).
            opts["cli_path"] = CLAUDE_CLI_PATH

        client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
        await client.connect()
        state = _UserStreamState(client=client, model=model)
        state_holder["state"] = state
        state.reader_task = asyncio.create_task(self._reader_loop(user_id, state))
        state.typing_task = asyncio.create_task(self._typing_keepalive_loop(user_id, state))
        return state

    async def _disconnect_user_stream(
        self, user_id: int, cancel_message: Optional[str] = None
    ) -> bool:
        state = self._streams.pop(user_id, None)
        if not state:
            return False
        for task in (state.typing_task, state.reader_task):
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.error("Error cancelling task for user %s: %s", user_id, e)
        msg = cancel_message or messages.TASK_TERMINATED
        while state.pending:
            req = state.pending.popleft()
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content=msg,
                        success=False,
                        error=msg,
                        session_id=state.last_session_id,
                    )
                )
        # The SDK's disconnect() -> transport.close() runs its own graceful
        # sequence (stdin EOF -> wait 5s -> SIGTERM -> wait 5s -> SIGKILL), which
        # can exceed this 3s budget for a CLI busy mid-turn (the /stop case). When
        # wait_for() times out it CANCELS disconnect() before the SDK reaches its
        # kill step, orphaning the CLI subprocess. So on timeout/error we force-kill
        # the underlying CLI process ourselves.
        try:
            await asyncio.wait_for(state.client.disconnect(), timeout=3.0)
        except Exception as e:
            logger.error("Error disconnecting client for user %s: %s", user_id, e)
            self._force_kill_client_subprocess(state.client, user_id)
        return True

    @staticmethod
    def _force_kill_client_subprocess(client: ClaudeSDKClient, user_id: int) -> None:
        """Best-effort hard kill of the CLI subprocess behind an SDK client.

        Fallback when client.disconnect() times out or errors, so a busy `claude`
        CLI child can never outlive the session as an orphan. Reaches into SDK
        internals defensively so an SDK rename degrades to a logged warning.
        """
        try:
            transport = getattr(client, "_transport", None)
            proc = getattr(transport, "_process", None) if transport else None
            pid = getattr(proc, "pid", None) if proc else None
            if pid is None or getattr(proc, "returncode", None) is not None:
                return
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(
                    "Force-killed orphan CLI subprocess pid=%s for user %s",
                    pid,
                    user_id,
                )
            except ProcessLookupError:
                pass
        except Exception as e:  # noqa: BLE001 - teardown fallback must never raise
            logger.error(
                "Failed to force-kill CLI subprocess for user %s: %s", user_id, e
            )

    async def _get_or_create_stream(
        self, user_id: int, model: Optional[str], new_session: bool
    ) -> _UserStreamState:
        async with self._get_stream_init_lock(user_id):
            state = self._streams.get(user_id)
            if state and state.reader_task is not None and state.reader_task.done():
                logger.warning("Stale stream for user %s, recreating", user_id)
                await self._disconnect_user_stream(user_id)
                state = None
            if state and (new_session or state.model != model):
                await self._disconnect_user_stream(user_id)
                state = None
            if not state:
                state = await self._create_user_stream(user_id, model)
                self._streams[user_id] = state
            return state

    async def _typing_keepalive_loop(self, user_id: int, state: _UserStreamState) -> None:
        try:
            while True:
                await asyncio.sleep(TYPING_INTERVAL)
                if not state.pending:
                    continue
                req = state.pending[0]
                if not req.typing_callback:
                    continue
                now = asyncio.get_event_loop().time()
                if now - req.last_typing_at < TYPING_INTERVAL:
                    continue
                req.last_typing_at = now
                try:
                    await req.typing_callback()
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Typing keepalive crashed for user %s: %s", user_id, e)

    async def _dispatch_next_query(self, state: _UserStreamState) -> None:
        if not state.pending:
            return
        head = state.pending[0]
        if head.sent:
            return
        head.sent = True
        await state.client.query(head.user_message, session_id=head.sent_session_id)

    @staticmethod
    def _clean_response(response: str) -> str:
        cleaned = _ANSI_RE.sub("", response)
        cleaned = "".join(c for c in cleaned if ord(c) >= 32 or c in "\n\r\t")
        return cleaned.strip()

    async def _reader_loop(self, user_id: int, state: _UserStreamState) -> None:
        try:
            async for msg in state.client.receive_messages():
                if not state.pending:
                    # No request to answer. This happens when a subagent/background
                    # task completion injects a new turn into the main session. We
                    # must NOT drop the main agent's proactive output; route it to a
                    # proactive push instead. Subagent inner messages stay blocked.
                    await self._handle_proactive_message(user_id, state, msg)
                    continue
                req = state.pending[0]
                now = asyncio.get_event_loop().time()
                if req.typing_callback and now - req.last_typing_at >= TYPING_INTERVAL:
                    req.last_typing_at = now
                    try:
                        await req.typing_callback()
                    except Exception:
                        pass

                if isinstance(msg, SystemMessage):
                    data = getattr(msg, "data", None)
                    sid = data.get("session_id") if isinstance(data, dict) else None
                    if sid:
                        state.last_session_id = sid
                    continue

                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "session_id", None):
                        state.last_session_id = msg.session_id
                    # Skip subagent inner messages (parent_tool_use_id set).
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    req.last_assistant_texts = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            # DGN-285: guard at ingestion so both the final
                            # assembly and the live streaming drafts are clean.
                            block_text = _scaffold_guard(block.text)
                            req.last_assistant_texts.append(block_text)
                            if req.streaming_handler:
                                try:
                                    await req.streaming_handler.update_if_needed(block_text)
                                except Exception as e:
                                    logger.error("Streaming update failed: %s", e)
                        elif isinstance(block, ToolUseBlock):
                            # DGN-086: track main-agent tool uses for flake detection.
                            req.tool_use_count += 1
                    continue

                if isinstance(msg, ResultMessage):
                    state.last_session_id = msg.session_id or state.last_session_id
                    await self._finalize_result(user_id, state, req, msg)
                    state.pending.popleft()
                    try:
                        await self._dispatch_next_query(state)
                    except Exception as e:
                        logger.error("Failed to dispatch next query: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Reader loop crashed for user %s: %s", user_id, e, exc_info=True)
            if state.typing_task and not state.typing_task.done():
                state.typing_task.cancel()
            self._streams.pop(user_id, None)
            pending_copy = list(state.pending)
            state.pending.clear()
            for req in pending_copy:
                if req.streaming_handler:
                    try:
                        await req.streaming_handler.finalize_all()
                    except Exception:
                        pass
                if not req.future.done():
                    req.future.set_result(
                        ChatResponse(
                            content=messages.GENERIC_ERROR.format(error=e),
                            success=False,
                            error=str(e),
                            session_id=state.last_session_id,
                        )
                    )

    @staticmethod
    def _is_placeholder_flake(content: str) -> bool:
        """DGN-086: detect subagent persona-bleed placeholder responses.

        Returns True when the final content matches the known Korean pattern
        where a role-confused subagent echoes the agent's own delegation-visibility
        prose ("동생 작업중", "서브에이전트 완료 대기", etc.) instead of executing
        the assigned task.

        Used in _finalize_result to log a warning. Fail-silent (never raises).
        """
        try:
            return bool(_PLACEHOLDER_FLAKE_RE.search(content))
        except Exception:
            return False

    @staticmethod
    async def _maybe_mark_options(prev_message: str, content: str) -> str:
        """Append the [[OPTIONS]] marker if Haiku judges the trailing numbered
        list a pick-one menu. Runs only when a numbered list is present and the
        marker is absent. Fail-silent: any error leaves content unchanged.
        """
        if not (has_numbered_list(content) and OPTIONS_MARKER not in content):
            return content
        try:
            is_choice = await asyncio.to_thread(
                classify_is_choice, prev_message, content, CLAUDE_CLI_PATH
            )
            if is_choice:
                return f"{content}\n\n{OPTIONS_MARKER}"
        except Exception as e:
            logger.warning("Option classifier failed (no buttons): %s", e)
        return content

    async def _handle_proactive_message(
        self, user_id: int, state: _UserStreamState, msg: Any
    ) -> None:
        """Handle an SDK message that arrived with no pending request.

        - SystemMessage: refresh session_id only (parity with the normal path).
        - AssistantMessage with parent_tool_use_id (subagent inner): skip always.
        - AssistantMessage without parent_tool_use_id (main agent): buffer text.
        - ResultMessage: flush the buffered main-agent text as a proactive push.
        """
        if isinstance(msg, SystemMessage):
            data = getattr(msg, "data", None)
            sid = data.get("session_id") if isinstance(data, dict) else None
            if sid:
                state.last_session_id = sid
            return

        if isinstance(msg, AssistantMessage):
            if getattr(msg, "session_id", None):
                state.last_session_id = msg.session_id
            # Subagent inner output must never leak to the user.
            if getattr(msg, "parent_tool_use_id", None):
                return
            for block in msg.content:
                if isinstance(block, TextBlock):
                    state.proactive_texts.append(_scaffold_guard(block.text))
            return

        if isinstance(msg, ResultMessage):
            state.last_session_id = msg.session_id or state.last_session_id
            if getattr(msg, "is_error", False):
                # A no-pending turn ended in an error (e.g. model overloaded /
                # api_error after retries). No assistant text was buffered, so the
                # normal flush would silently drop it. Surface a notice instead.
                await self._flush_proactive_error(user_id, state)
            else:
                await self._flush_proactive(user_id, state)

    async def _flush_proactive_error(self, user_id: int, state: _UserStreamState) -> None:
        """Surface a failed no-pending (background/proactive) turn.

        Mirrors _flush_proactive's delivery guards but sends a fixed failure
        notice instead of buffered text (which is empty on an error result).
        """
        state.proactive_texts = []
        if state.last_chat_id is None or state.proactive_push is None:
            logger.warning(
                "Proactive error for user %s dropped: no chat_id/push callback", user_id
            )
            return
        notice = messages.PROACTIVE_TURN_FAILED
        if notice == state.last_proactive_sent:
            return
        try:
            await state.proactive_push(state.last_chat_id, notice, False)
            state.last_proactive_sent = notice
        except Exception as e:
            logger.error("Proactive error push failed for user %s: %s", user_id, e)

    async def _flush_proactive(self, user_id: int, state: _UserStreamState) -> None:
        """Deliver buffered main-agent text that arrived with no pending request.

        Called on a ResultMessage when state.pending is empty. Noise guards:
        empty/whitespace-only text is dropped; an identical consecutive push is
        suppressed. Missing chat_id or callback degrades to a logged skip (never
        crashes the reader loop). The normal request-response path never reaches
        here (it has a pending request), so this is regression-safe.
        """
        texts = state.proactive_texts
        state.proactive_texts = []
        if not texts:
            return
        content = self._clean_response("\n".join(texts))
        if not content:
            return
        # DGN-217: an injected background turn may decide there is nothing
        # worth telling the owner (no-op review). The agent signals that by
        # ending the turn with the bare sentinel; suppress the push entirely.
        # Tolerant match: harness machinery (a Stop-hook footer) may append
        # lines AFTER the sentinel; strict equality then leaks the raw
        # sentinel body to the owner chat. The sentinel is the turn's bare
        # final output, so trailing decoration after a leading NO_PUSH line
        # is still a suppressed turn.
        # DGN-234: the agent may also emit a report body and END with the
        # sentinel line ("... details in the ticket.\nNO_PUSH") -- the
        # instruction prose says "end your output with NO_PUSH", so accept
        # a trailing sentinel line too. Intent is silence either way.
        stripped = content.strip()
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if (
            stripped == "NO_PUSH"
            or stripped.startswith("NO_PUSH\n")
            or (lines and lines[-1] == "NO_PUSH")
        ):
            return
        if content == state.last_proactive_sent:
            return
        if state.last_chat_id is None or state.proactive_push is None:
            logger.warning(
                "Proactive output for user %s dropped: no chat_id/push callback", user_id
            )
            return
        dedup_key = content  # cleaned text, before any marker is appended
        content = await self._maybe_mark_options("", content)
        has_options = OPTIONS_MARKER in content or has_numbered_list(content)
        try:
            # _send_smart strips the marker and renders [[OPTIONS]] buttons itself.
            await state.proactive_push(state.last_chat_id, content, has_options)
            state.last_proactive_sent = dedup_key
        except Exception as e:
            logger.error("Proactive push failed for user %s: %s", user_id, e)

    async def _finalize_result(
        self, user_id: int, state: _UserStreamState, req: _PendingRequest, msg: ResultMessage
    ) -> None:
        # DGN-285: assemble user-facing text from the reader loop's TextBlock
        # capture (a structural block-type whitelist: thinking/tool blocks never
        # enter it) instead of trusting msg.result, a CLI-composed string that
        # sits outside that whitelist and can carry thinking/internal content
        # under degraded conditions. msg.result stays primary on error results
        # (it carries the error description) and remains the fallback for turns
        # that produced no main-agent TextBlock.
        block_text = "\n".join(req.last_assistant_texts)
        if msg.is_error or not block_text.strip():
            result_text = msg.result or block_text
        else:
            result_text = block_text
        # DGN-285 (leak class 2): signature guard also covers the msg.result
        # fallback path, which bypasses the guarded block capture above.
        result_text = _scaffold_guard(result_text)
        if req.streaming_handler:
            try:
                await req.streaming_handler.finalize_all()
            except Exception as e:
                logger.error("Streaming finalization failed: %s", e)
        draft_ids = (
            [d.message_id for d in req.streaming_handler.drafts]
            if req.streaming_handler
            else []
        )
        is_streamed = bool(req.streaming_handler and req.streaming_handler.drafts)

        if req.synthetic_response:
            content = self._clean_response(req.synthetic_response) or "(No response)"
        else:
            content = self._clean_response(result_text) or "(No response)"

        if msg.is_error:
            req.future.set_result(
                ChatResponse(
                    content=messages.PROCESSING_FAILED.format(error=content),
                    success=False,
                    error=content,
                    session_id=msg.session_id,
                    streamed=is_streamed,
                    draft_message_ids=draft_ids,
                )
            )
            return

        # DGN-086: placeholder-flake detection. Log a warning when the final
        # response matches the delegation-handoff placeholder pattern (subagent
        # role-confusion: inherited persona caused it to report "동생 작업중"
        # instead of executing). Annotates the log with tool_use_count so the
        # caller can distinguish the <=1 (original) from the >1 read-only case.
        if req.synthetic_response is None and self._is_placeholder_flake(content):
            logger.warning(
                "DGN-086 placeholder flake detected for user %s "
                "(tool_use_count=%d, num_turns=%d): response matches delegation-"
                "placeholder pattern -- subagent likely echoed the agent persona "
                "instead of executing. "
                "Nudge: reply asking the agent to continue directly as executor.",
                user_id,
                req.tool_use_count,
                msg.num_turns,
            )

        # Haiku auto-classifier: only when no synthetic response, a numbered list
        # is present, and the marker is absent. Fail-silent.
        if req.synthetic_response is None:
            content = await self._maybe_mark_options(req.user_message, content)

        has_options = req.synthetic_response is not None or has_numbered_list(content)
        if not req.future.done():
            req.future.set_result(
                ChatResponse(
                    content=content,
                    success=True,
                    session_id=msg.session_id,
                    has_options=has_options,
                    streamed=is_streamed,
                    draft_message_ids=draft_ids,
                )
            )

    async def inject_background_turn(self, user_id: int, text: str) -> bool:
        """DGN-217: inject a background/cron notification as a turn into the
        user's LIVE session, with no pending request attached.

        The turn's output flows through the existing no-pending path
        (_handle_proactive_message -> proactive push), so the agent both
        SEES the notification in-session and controls what (if anything)
        reaches the owner -- ending the turn with the bare sentinel NO_PUSH
        suppresses the push.

        Returns False (caller retries later) when:
        - no live stream exists for this user yet (bot just started), or
        - a real request is pending/in flight. Injecting then would race the
          reader loop, which attributes ALL output to pending[0] -- the
          injected turn's answer would masquerade as the user's answer.
        """
        state = self._streams.get(user_id)
        if state is None:
            return False
        if state.pending:
            return False
        async with state.send_lock:
            # Re-check under the lock: a user message may have arrived while
            # we were waiting for the lock.
            if state.pending:
                return False
            await state.client.query(
                text, session_id=state.last_session_id or "default"
            )
        return True

    async def process_message(
        self,
        user_message: str,
        user_id: int,
        chat_id: int,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        new_session: bool = False,
        permission_callback: Optional[PermissionCallback] = None,
        typing_callback: Optional[TypingCallback] = None,
        bot: Optional[Any] = None,
        proactive_push: Optional[ProactivePushCallback] = None,
    ) -> ChatResponse:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        streaming_handler = None
        if bot is not None:
            from bridge.streaming import StreamingMessageHandler

            streaming_handler = StreamingMessageHandler(bot, chat_id, user_id)

        request = _PendingRequest(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            requested_session_id=session_id,
            permission_callback=permission_callback,
            typing_callback=typing_callback,
            future=future,
            user_message=user_message,
            streaming_handler=streaming_handler,
        )
        state: Optional[_UserStreamState] = None
        try:
            state = await self._get_or_create_stream(user_id, model, new_session)
            # Capture the live delivery route so proactive output (output with no
            # pending request, e.g. a background-task completion turn) can still
            # reach this user's chat.
            state.last_chat_id = chat_id
            if proactive_push is not None:
                state.proactive_push = proactive_push
            async with state.send_lock:
                request.sent_session_id = session_id or state.last_session_id or "default"
                state.pending.append(request)
                await self._dispatch_next_query(state)
            return await asyncio.wait_for(future, timeout=PROCESS_TIMEOUT)

        except asyncio.CancelledError:
            if streaming_handler:
                try:
                    await streaming_handler.cancel()
                except Exception:
                    pass
            await self.stop(user_id)
            raise

        except asyncio.TimeoutError:
            logger.warning("Query timed out for user %s after %ss", user_id, PROCESS_TIMEOUT)
            resume_sid, partial = await self.handle_timeout_preserve(user_id)
            return ChatResponse(
                content=messages.TIMEOUT_PAUSED.format(timeout=PROCESS_TIMEOUT),
                success=False,
                error="timeout",
                session_id=resume_sid,
                timed_out=True,
                resume_session_id=resume_sid,
                partial_preserved=partial,
                streamed=partial,
            )

        except Exception as e:
            if state and request in state.pending:
                try:
                    state.pending.remove(request)
                except ValueError:
                    pass
            if _is_retryable_sdk_error(e):
                logger.warning("Retryable SDK error for user %s: %s — retrying", user_id, e)
                return await self._reconnect_and_retry(
                    user_id, chat_id, user_message, session_id, model,
                    permission_callback, typing_callback, bot, loop,
                )
            logger.error("Error processing message for user %s: %s", user_id, e, exc_info=True)
            return ChatResponse(
                content=messages.GENERIC_ERROR.format(error=e), success=False, error=str(e)
            )

    async def _reconnect_and_retry(
        self, user_id, chat_id, user_message, session_id, model,
        permission_callback, typing_callback, bot, loop,
    ) -> ChatResponse:
        await self._disconnect_user_stream(user_id)
        retry_future: asyncio.Future = loop.create_future()
        retry_handler = None
        if bot is not None:
            from bridge.streaming import StreamingMessageHandler

            retry_handler = StreamingMessageHandler(bot, chat_id, user_id)
        retry_request = _PendingRequest(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            requested_session_id=session_id,
            permission_callback=permission_callback,
            typing_callback=typing_callback,
            future=retry_future,
            user_message=user_message,
            streaming_handler=retry_handler,
        )
        try:
            retry_state = await self._get_or_create_stream(user_id, model, new_session=False)
            async with retry_state.send_lock:
                retry_request.sent_session_id = (
                    session_id or retry_state.last_session_id or "default"
                )
                retry_state.pending.append(retry_request)
                await self._dispatch_next_query(retry_state)
            return await asyncio.wait_for(retry_future, timeout=PROCESS_TIMEOUT)
        except Exception as retry_err:
            logger.error("Retry failed for user %s: %s", user_id, retry_err, exc_info=True)
            return ChatResponse(
                content=messages.GENERIC_ERROR.format(error=retry_err),
                success=False,
                error=str(retry_err),
            )

    async def stop(self, user_id: int) -> bool:
        return await self._disconnect_user_stream(
            user_id, cancel_message=messages.TASK_TERMINATED
        )

    async def handle_timeout_preserve(self, user_id: int) -> Tuple[Optional[str], bool]:
        """Preserve (finalize, not delete) partial drafts + capture resume sid."""
        state = self._streams.get(user_id)
        resume_session_id: Optional[str] = None
        partial_preserved = False
        if state:
            resume_session_id = state.last_session_id
            if state.pending:
                head = state.pending[0]
                if not resume_session_id:
                    if head.requested_session_id not in (None, "default"):
                        resume_session_id = head.requested_session_id
                    elif head.sent_session_id not in (None, "default"):
                        resume_session_id = head.sent_session_id
                if head.streaming_handler and getattr(head.streaming_handler, "drafts", None):
                    try:
                        await head.streaming_handler.finalize_all()
                        partial_preserved = True
                    except Exception as e:
                        logger.error("Timeout finalize failed for user %s: %s", user_id, e)
        await self._disconnect_user_stream(user_id, cancel_message=messages.STILL_WORKING)
        return resume_session_id, partial_preserved

    async def cancel_user_streaming(self, user_id: int) -> bool:
        state = self._streams.get(user_id)
        if not state or not state.pending:
            return False
        cancelled = False
        for req in state.pending:
            if req.streaming_handler:
                try:
                    await req.streaming_handler.cancel()
                    cancelled = True
                except Exception as e:
                    logger.error("Failed to cancel streaming for user %s: %s", user_id, e)
        return cancelled

    def user_has_streamed_output(self, user_id: int) -> bool:
        """DGN-163: did this user's live turn already stream partial output?

        The turn-death safety net uses this to choose between the "message not
        processed" notice and the softer "reply may be incomplete" variant. True
        when any pending request for this user has a streaming handler holding at
        least one draft bubble (mirrors handle_timeout_preserve's partial check).
        Read-only, sync, best-effort: never raises into the caller.
        """
        try:
            state = self._streams.get(user_id)
            if not state:
                return False
            for req in state.pending:
                handler = getattr(req, "streaming_handler", None)
                if handler is not None and getattr(handler, "drafts", None):
                    return True
        except Exception as e:
            logger.error("user_has_streamed_output check failed for %s: %s", user_id, e)
        return False


sdk_bridge = SdkBridge()
