"""Per-user long-lived Claude SDK streaming bridge.

Each user gets a persistent ClaudeSDKClient. Messages are serialized: only one
query is in flight at a time (the reader_loop attributes all streamed text to
the head request), while later messages queue immediately for a fast-typing UX.
Handles streaming drafts, AskUserQuestion degradation, the timeout/preserve +
resume capture path, and a single reconnect-retry on transient SDK errors.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# DGN-1016: task-lifecycle message types + the terminal-status vocabulary that
# feed the auto-interrupt background guard. These are NEWER than the SDK floor
# this bridge declares (requirements.txt: claude-agent-sdk>=0.1.72) and this
# module is imported at bridge boot, so a hard import would turn "guard
# unavailable" into "bridge dead" on an estate whose venv predates them
# (DGN-1010: the delivery path is not the verification path). When absent the
# tracker records nothing, live_task_count stays 0, and the auto-interrupt
# behaves exactly as it did before DGN-1016 -- status quo, not a regression.
try:
    from claude_agent_sdk import (
        TERMINAL_TASK_STATUSES,
        TaskNotificationMessage,
        TaskStartedMessage,
        TaskUpdatedMessage,
    )

    TASK_LIFECYCLE_AVAILABLE = True
except ImportError:  # SDK predates the typed task-lifecycle messages

    class _AbsentTaskMessage:
        """Sentinel: no real stream message is ever an instance of this, so
        every isinstance() check below is a cheap, always-false no-op."""

    TERMINAL_TASK_STATUSES = frozenset()
    TaskStartedMessage = _AbsentTaskMessage
    TaskNotificationMessage = _AbsentTaskMessage
    TaskUpdatedMessage = _AbsentTaskMessage
    TASK_LIFECYCLE_AVAILABLE = False

from bridge import live_model
from bridge import messages
from bridge import mint_gate
from bridge import notice_spool
from bridge.config import (
    BRIDGE_REGISTER_GUARD,
    BRIDGE_SCAFFOLD_GUARD,
    CLAUDE_MAX_BUFFER_SIZE,
    FOLD_UPDATE_INTERVAL,
    INTERIM_MODE,
    OUTPUT_LANG_GUARD,
    PROCESS_TIMEOUT,
    STREAM_INTERIM,
    TIMEOUT_STOP_GRACE,
    config,
    resolve_claude_cli,
)
from bridge.formatting import (
    FOLD_CAPTION_NORMAL,
    FOLD_CAPTION_STOPPED,
    FOLD_CAPTION_TIMEOUT,
    INTERIM_FOLD_SEPARATOR,
    INTERRUPT_FOLD_CAPTION,
    compose_interim_fold,
    split_promoted_interim,
    render_fold_final,
    render_fold_live,
    strip_no_push_sentinel,
)
from bridge.options import (
    OPTIONS_MARKER,
    classify_is_choice,
    has_numbered_list,
    has_options_marker,
    has_single_trailing_option,
    subtract_delivered,
)
from bridge.permissions import extract_outside_paths, extract_protected_paths
from bridge.session import session_manager

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(os.environ["PROJECT_ROOT"]).resolve()

# DGN-531: status-footer.py writes the canonical footer here; the bridge
# appends it once at finalize time, then clears the file so stale data never
# bleeds into a subsequent turn.
_FOOTER_SIDECAR = PROJECT_ROOT / ".telegram_bot" / "footer-sidecar.json"
# Pattern that matches a model-written [라이브] / [결정대기] footer block at
# the TAIL of the response body.  The bridge strips it before appending the
# sidecar footer so the hook is the sole author regardless of what the LLM
# wrote.
#
# DGN-816: the old pattern matched a marker ANYWHERE (even mid-word) and ate
# everything up to the next '[' via [^\[]*, so a legitimate mid-body mention
# of the literal strings [라이브]/[결정대기] truncated the user's message from
# that point to the end ("cut off mid-sentence" loss).  The canonical footer
# (status-footer.py _build_footer) is a trailing block of LINES: a bare
# "[라이브]" / "[결정대기]" header line (the legacy one-line form carries
# trailing text on the header line) followed by "- " bullet lines, appended at
# the very end of the message.  Match exactly that shape: a line-start marker
# line, then any run of bullet / marker lines (blank-line gaps tolerated),
# anchored to the END of the string.  Mid-body occurrences of the literal
# strings -- including line-start markers followed by ordinary prose lines --
# never match and are preserved.
_FOOTER_BLOCK_RE = re.compile(
    r"^\[(?:라이브|결정대기)\][^\n]*"
    r"(?:\n+(?:- [^\n]*|\[(?:라이브|결정대기)\][^\n]*))*"
    r"\s*\Z",
    re.MULTILINE,
)


def _consume_footer_sidecar(content: str) -> str:
    """Read the footer sidecar, strip a model-written trailing footer block
    from content, append the canonical footer once, then clear the sidecar.

    Returns the modified content string.  On any error returns content unchanged
    (fail-silent -- a missing footer is safer than a broken finalize).

    Contract:
    - Sidecar absent or unreadable: return content as-is.
    - Sidecar footer is empty string (noise-suppression turn): strip a
      model-written trailing footer block and return without appending
      anything.
    - Sidecar footer is non-empty: strip a trailing model-written block,
      append the canonical footer once at the end.
    - Mid-body occurrences of the literal strings [라이브]/[결정대기] are
      NEVER touched (DGN-816 over-deletion fix).
    - After consuming, overwrite the sidecar with {"footer": "", "ts": 0}
      (clear) so a subsequent turn never inherits a stale footer.
    """
    try:
        sidecar_path = _FOOTER_SIDECAR
        if not sidecar_path.is_file():
            return content
        with sidecar_path.open("r", encoding="utf-8") as fh:
            sidecar = json.load(fh)
        footer = (sidecar.get("footer") or "").strip()

        # Strip a trailing model-written footer block regardless of whether
        # the hook has anything to append (mid-body literals preserved).
        stripped = _FOOTER_BLOCK_RE.sub("", content).rstrip()

        if footer:
            result = stripped + "\n" + footer
        else:
            result = stripped

        # Clear the sidecar atomically so the next turn starts clean.
        try:
            tmp = str(sidecar_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"footer": "", "ts": 0}, fh)
            os.replace(tmp, str(sidecar_path))
        except Exception:
            pass  # clear failure is non-fatal

        return result if result else content
    except Exception:
        return content

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

# DGN-930: live-then-fold creation gate. The fold bubble is created on the
# FIRST interim block so dev-agent progress is visible LIVE from turn start
# (the growing plain-text bubble render_fold_live produces), then collapses to
# a caption + expandable quote at turn end while the final answer arrives as a
# separate message. This pulls the old DGN-699 D8 lazy gate (2nd interim) to
# the 1st per the DGN-930 2026-08-19 root spec -- an interim-1 + long-tool turn
# no longer shows nothing until the final answer.
# The char floor stays as an OR trigger (a single large interim also opens the
# bubble). Turns that emit ZERO interim blocks still never open a bubble and
# fall through to the finalize-time compose_interim_fold synthesis (DGN-682).
# Note: the T-gate (FOLD_CREATE_MIN_SECS, 8s) was removed at grill review
# because the gate is checked on interim ARRIVAL, so elapsed time is negligible;
# if a T-gate is ever needed, reintroduce it as a periodic check.
FOLD_CREATE_MIN_INTERIMS = 1
FOLD_CREATE_MIN_CHARS = 300

# DGN-581: budget for the soft-interrupt control request. A CLI stuck badly
# enough to not ack the control channel within this window is treated as an
# interrupt failure, and the caller falls back to the hard teardown.
INTERRUPT_SEND_TIMEOUT = 5.0  # seconds

# DGN-946: wall-clock budget for the pre-teardown fold flush. The run loop
# calls flush_folds_for_shutdown() on a REAL stop, before the Telegram HTTP
# client is torn down; the whole sweep shares this budget so a RetryAfter
# storm can never stall process shutdown past the restart window.
FOLD_SHUTDOWN_FLUSH_BUDGET = 5.0  # seconds

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

# DGN-670 M1: recovery only fires on SHORT final content. Placeholder flakes
# are one-liners; a genuine long report that merely QUOTES the flake
# vocabulary (e.g. discussing this very bug, or a detailed status report)
# must never be blocked and re-run.
_FLAKE_SHORT_CONTENT_MAX = 300

# DGN-670 F1: mechanical executor-contract injection into every Task prompt.
# The contract text is the SAME verbatim line the DGN-086 system_prompt
# section asks the model to include; the marker substring dedupes so a prompt
# that already carries it (model followed the instruction, or nested Task)
# is never double-prefixed. English on purpose (model-facing).
_EXECUTOR_CONTRACT_MARKER = "You are the direct executor of this task"
_EXECUTOR_CONTRACT_PREFIX = (
    "You are the direct executor of this task. You MUST perform the work "
    "yourself using the available tools. Do NOT delegate, defer, or report "
    "that you are waiting for another agent. Do NOT output placeholder "
    "messages like 'working in background' or 'waiting for completion'. "
    "Complete the task directly and output the result.\n\n"
)


async def _task_executor_contract_hook(input_data, tool_use_id, context):
    """DGN-670 F1: PreToolUse hook (matcher \"Task\") that prepends the
    executor contract to the Task prompt via updatedInput.

    Prevention replaces the dead can_use_tool path: Task is in ALLOWED_TOOLS,
    and the SDK does not invoke can_use_tool for allowed_tools calls, so a
    permission-callback rewrite never runs. PreToolUse hooks fire on every
    Task call regardless of the allow list.

    Idempotent (marker check) and fail-silent: any error returns {} so a
    missed rewrite degrades to today's behavior and never blocks a Task.
    """
    try:
        if not isinstance(input_data, dict):
            return {}
        if input_data.get("tool_name") != "Task":
            return {}
        tool_input = input_data.get("tool_input")
        if not isinstance(tool_input, dict):
            return {}
        prompt = tool_input.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return {}
        if _EXECUTOR_CONTRACT_MARKER in prompt:
            return {}
        updated = dict(tool_input)
        updated["prompt"] = _EXECUTOR_CONTRACT_PREFIX + prompt
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
            }
        }
    except Exception:  # noqa: BLE001 - prevention must never block a Task
        return {}

# Permission callback: async (chat_id, user_id, tool_name, tool_input) -> result
PermissionCallback = Callable[[int, int, str, Dict[str, Any]], Awaitable]
TypingCallback = Callable[[], Awaitable[Any]]
# Proactive push callback: async (chat_id, content, has_options,
# classifier_injected) -> None. Delivers main-agent output that has no pending
# request to answer. classifier_injected carries DGN-665 marker provenance.
ProactivePushCallback = Callable[[int, str, bool, bool], Awaitable[Any]]

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
# DGN-517: "maximum buffer size" added so a per-message JSON overflow (SDK
# raises this from the stdout framer when a single line exceeds max_buffer_size)
# routes through _reconnect_and_retry instead of killing the reader loop and
# losing the session. The underlying cause is a large tool result (e.g. base64
# image); the 16MB ceiling in config.py prevents the common case, but if a
# message still exceeds it the reader loop must survive.
_RETRYABLE_MSG = ("timeout", "connection", "refused", "unreachable", "exit code -15", "exit code -9", "maximum buffer size")


# DGN-686: failure classification, the SINGLE SOURCE OF TRUTH for "is this
# transient". A ResultMessage with is_error=True (and a raised SDK exception)
# carries an English failure text; we map it to one of three LOCKED outcomes:
#   - "transient": overloaded / 529 / 5xx / timeout / connection-class -> the
#     bot seat auto-retries ONCE, then offers a [retry] action if it fails.
#   - "auth": auth/token/401 class -> re-login needed, NO retry action.
#   - "other": everything else -> generic failure, offer a [retry] action.
# The user-facing detail is logged to stderr only, never shown.
_ERR_AUTH_MARKERS = (
    "401", "invalid_api_key", "authentication", "unauthorized",
    "invalid x-api-key", "permission_error", "oauth", "token expired",
    "authentication_error",
)
_ERR_TRANSIENT_MARKERS = (
    "overloaded", "529", "timeout", "timed out", "connection", "connect error",
    "connecterror", "unreachable", "temporarily", "503", "502", "500", "504",
    "rate_limit", "429",
)


# DGN-1857: Claude Code reports an auth failure as a SYNTHETIC assistant
# message (model "<synthetic>", error="authentication_failed"), not as an
# is_error result, so its text rode the normal answer path to the owner
# verbatim ("Failed to authenticate: OAuth session expired and could not be
# refreshed", 73 chars -- under the register guard's 80-char floor). The
# prefixes are the CLI's own strings (claude 2.1.288 bundle): -p / SDK mode
# opens every auth failure with "Failed to authenticate"; the interactive
# forms are middle-dot pairs; the last two are the pre-2.1 API-error shapes.
# Anchored at the START of a SHORT text so an answer that merely talks about
# OAuth or /login never matches.
_CLI_AUTH_FAILURE_PREFIXES = (
    "Failed to authenticate",
    "Login expired \u00b7",
    "OAuth token revoked \u00b7",
    "Not logged in \u00b7",
    "Please run /login \u00b7",
    "Invalid API key \u00b7",
    "Invalid auth token \u00b7",
    "Authentication required \u00b7",
    "OAuth token has expired",
    "API Error: 401",
)
_CLI_AUTH_FAILURE_MAX_LEN = 400


def _is_cli_auth_failure_text(text: str) -> bool:
    t = (text or "").strip()
    return 0 < len(t) <= _CLI_AUTH_FAILURE_MAX_LEN and t.startswith(
        _CLI_AUTH_FAILURE_PREFIXES
    )


def _cli_auth_failure(msg: Any) -> Optional[str]:
    """DGN-1857: the raw text of a main-thread AssistantMessage that is the
    CLI's auth-failure report, else None. The SDK's structural error flag
    (newer SDKs) or the text signature (any SDK) is enough."""
    text = "\n".join(
        b.text for b in msg.content if isinstance(b, TextBlock)
    ).strip()
    if getattr(msg, "error", None) == "authentication_failed":
        return text or "authentication_failed"
    if _is_cli_auth_failure_text(text):
        return text
    return None


def _classify_error_result(detail: str) -> str:
    """Classify a failure detail into 'auth' | 'transient' | 'other'.

    Auth takes precedence over transient (a 401 must never be retried). Match
    is substring/case-insensitive against the raw failure text.
    """
    if _is_cli_auth_failure_text(detail):
        return "auth"
    low = (detail or "").lower()
    if any(m in low for m in _ERR_AUTH_MARKERS):
        return "auth"
    if any(m in low for m in _ERR_TRANSIENT_MARKERS):
        return "transient"
    return "other"


def _is_retryable_sdk_error(error: Exception) -> bool:
    msg = str(error)
    if any(p in msg for p in _NON_RETRYABLE):
        return False
    if type(error).__name__ in _RETRYABLE_TYPES:
        return True
    # _classify_error_result is the single transient source (adds
    # overloaded/529/5xx coverage that _RETRYABLE_MSG lacks); keep the extra
    # process-plumbing signals (exit codes, buffer overflow) that are not a
    # register-level "transient" but still warrant a reconnect-retry.
    if _classify_error_result(msg) == "transient":
        return True
    return any(p in msg.lower() for p in _RETRYABLE_MSG)


def _effective_interim_mode() -> str:
    """DGN-682 D1: resolve the effective interim mode at call time.

    INTERIM_MODE (suppress|inline|fold) is the primary knob. The legacy
    STREAM_INTERIM boolean stays honored as a deprecated alias -- and as the
    symbol existing tests patch on this module: when the mode resolves to
    suppress but STREAM_INTERIM is truthy, the alias maps to inline. Reads the
    module globals at call time so unittest.mock.patch on either symbol works.
    """
    mode = INTERIM_MODE
    if mode == "suppress" and STREAM_INTERIM:
        return "inline"
    return mode


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
# in model-side transcript-regurgitation leaks (observed 2026-07-14),
# where the poison arrived INSIDE a genuine text block and the block-type
# filter could not help. Exact line-prefix match only, no fuzzy matching.
_SCAFFOLD_SIGNATURES = (
    "system UserPromptSubmit hook",
    "UserPromptSubmit hook additional context",
    "UserPromptSubmit hook success",
)

# DGN-1606: harness-owned injected-turn mark.  Every background injection
# (session-inbox / cron-inject, the inject_background_turn choke point) OPENS
# with this line.  An injected turn otherwise reaches the SDK through the
# same client.query() as a real owner message and is indistinguishable in
# the transcript (measured 2026-09-20: a session-inbox drop landed as a
# plain user entry, isMeta unset) -- this mark is the machine signal that
# says "no owner utterance opened this turn".
# ESTATE:BEGIN the mark's first consumers are the estate onboarding hooks
# First consumers:
# routines/onboarding-owner-gate.py (denies identity writes during a
# pending onboarding when the turn is machine-opened) and
# routines/onboarding-gate-stop.py (abstains from demanding onboarding in
# such turns).  Lockstep literal with INJECTED_TURN_MARK in
# routines/onboarding-gate-stop.py -- change both together.
# ESTATE:END
INJECTED_TURN_MARK = "[bridge:injected-turn]"

# DGN-1703: the CLI's Stop-hook re-prompt. When a Stop hook returns
# decision:block the CLI keeps the turn and feeds the reason back to the model
# as a main-thread user message whose text starts with this literal (measured
# on SDK 0.2.110/CLI 2.1.191 and SDK 0.2.159/CLI 2.1.281, 2026-09-25; the raw
# event also carries isSynthetic:true, which the SDK UserMessage drops). It is
# the ONLY in-stream signal that the text streamed just before it was a
# finished answer: both CLIs stream every AssistantMessage with
# stop_reason=None (the end_turn value exists only on the ResultMessage and in
# the rewritten transcript), so DGN-426's is_terminal never fires on a live
# stream and the DGN-1651 terminal-span retraction never arms.
STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"


def _is_stop_hook_feedback(msg: Any) -> bool:
    """True iff `msg` is the CLI's Stop-block re-prompt (main thread only)."""
    if getattr(msg, "parent_tool_use_id", None):
        return False
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content.lstrip().startswith(STOP_HOOK_FEEDBACK_PREFIX)
    if isinstance(content, list):
        return any(
            isinstance(b, TextBlock)
            and b.text.lstrip().startswith(STOP_HOOK_FEEDBACK_PREFIX)
            for b in content
        )
    return False


# ESTATE:BEGIN DGN-1850 the only blocking Stop gate that opts in is the estate onboarding gate, armed by a marker in the estate identity-file layout; the public build has no such gate, so the window never opens
# DGN-1850: the onboarding Stop gate's own arming signal. Lockstep literal with
# MARKER in routines/onboarding-gate-stop.py; the identity file resolution
# mirrors onboarding-check.py resolve_persona_md (newest layout first), which
# the gate reaches with ONBOARDING_FILE=<project root>.
ONBOARDING_PENDING_MARKER = "<!-- ONBOARDING_PENDING -->"
_IDENTITY_CANDIDATES = (
    ("identity", "hot.custom.agent.md"),
    ("PROFILE.md",),
    ("AGENT.md",),
)


def _onboarding_gate_armed(root: Path) -> bool:
    """True iff the identity file the onboarding Stop gate resolves still
    carries ONBOARDING_PENDING. A missing or unreadable file disarms the gate
    (resolve_identity in the gate), so it reads False here too."""
    for parts in _IDENTITY_CANDIDATES:
        path = root.joinpath(*parts)
        if path.is_file():
            try:
                return ONBOARDING_PENDING_MARKER in path.read_text(encoding="utf-8")
            except Exception:
                return False
    return False


def blocking_stop_gate_window(root: Optional[Path] = None) -> bool:
    """DGN-1850: can a BLOCKING Stop gate fire for this instance right now?

    A Stop hook sees the reply only after it was written, and by then the
    bridge has streamed it. While this returns True the bridge holds the
    turn's terminal answer off the owner surface until the turn's outcome is
    known (Stop-hook feedback -> discard; ResultMessage -> deliver), so a
    block never rewrites a bubble the owner is watching. Latched once per
    turn at request creation: the gate may still block in the turn that
    clears the marker (the RELEASED kit-select check).

    Opted in today: routines/onboarding-gate-stop.py (FC_BLOCK,
    FC_REPEAT_BLOCK, HANDOFF_BLOCK, the RELEASED kit check, the text-less
    BLOCK), keyed on the marker it arms on. Other blocking Stop gates that
    could OR their own machine signal in here: the dispatch-return
    result-first Stop seat (window = a live dispatch-return-context.json /
    _UserStreamState.dispatch_return_turn_id) and routines/output-gate-stop.py
    IDLE_PROMISE (no window signal today -- it can fire on any turn, so it
    needs one before it can opt in without holding every answer).
    """
    try:
        return _onboarding_gate_armed(root or PROJECT_ROOT)
    except Exception:
        return False
# ESTATE:ELSE
# def blocking_stop_gate_window(root: Optional[Path] = None) -> bool:
#     """Can a BLOCKING Stop gate fire for this instance right now?  While
#     True the bridge holds the turn's terminal answer off the owner surface
#     until the turn's outcome is known.  No gate opts in in this build."""
#     return False
# ESTATE:END


def _observe_live_model(msg: Any) -> None:
    """DGN-1814 r2: record the model id the CLI reported in its system/init
    message -- the only source of truth for "which model answered" (settings
    can say the same alias while the CLI resolves a different model).
    bridge/self_restart.sh reads the record to name a changed model in the
    restart notice. Never raises."""
    if getattr(msg, "subtype", None) != "init":
        return
    data = getattr(msg, "data", None)
    if isinstance(data, dict):
        live_model.observe(data.get("model"), live_model.state_path(config.bot_data_dir))


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


# DGN-376 T2 / DGN-686 v2: design-system register guard.
#
# v2 STRENGTH (DGN-686, direction lock 2026-08-02) = DROP-ONLY. The bridge
# guard does exactly one thing: a pure-English block (ZERO Hangul + prose of
# _LOCALE_MIN_LEN+ chars on a ko-locale instance) is DROPPED whole, with a
# WARNING. There is no fragment-masking tier -- partial leaks (a tool name or
# internal path riding inside an otherwise-Korean body) are an UPSTREAM concern
# (the leader-summary layer), not the bridge's; the bridge deliberately keeps
# its hands off them. The guard return is exactly two-way: "" on a
# locale-register drop, else the text UNCHANGED.
#
# The locale rule is the machine enforcement of DESIGN-SYSTEM.md R2 (text
# register) doctrine: on a ko instance the user-facing register stays Korean,
# and a long all-English reply is the framework's working-English leaking into
# the user channel. Low-false-positive: fenced-code and bare-URL/deep-link
# lines are excluded from the scored prose (see _locale_register_prose), so a
# code-only or link-only reply never trips the drop.
#
# _register_findings still carries the DGN-430 fragment detectors (tool name /
# send_file:: marker / internal path / scheduler term) for the log-warn unit
# tests and any future upstream consumer, but the guard itself acts ONLY on the
# locale-register finding.
#
# Gated by BRIDGE_REGISTER_GUARD (default ON): an emergency env bypass
# (BRIDGE_REGISTER_GUARD=0) passes all text through unchanged.

# DGN-430: internal tool names. Matched only as a whole word immediately
# followed by "(" -- i.e. a function/tool call form like "Bash(" -- so prose
# uses of the common English word ("read the file", "write it down") do not
# trip. Ordered longest-first is irrelevant here (word-boundary anchored).
_REGISTER_TOOL_NAMES = (
    "Bash", "Edit", "Write", "Read", "Grep", "Glob",
    "Task", "WebFetch", "WebSearch", "NotebookEdit",
    "TodoWrite", "MultiEdit", "ToolSearch",
)
_REGISTER_TOOL_RE = re.compile(
    r"\b(?:" + "|".join(_REGISTER_TOOL_NAMES) + r")\("
)

# DGN-430: the send_file:: delivery marker must be CONSUMED by the bridge, never
# echoed as literal prose. A line that merely starts with it is the legitimate
# delivery form; the leak we detect is the token appearing inline in text.
_REGISTER_MARKER_RE = re.compile(r"send_file::")

# DGN-430: filesystem path shapes that only ever come from internal plumbing --
# absolute home/tmp/root paths and the workspace-relative script dirs. Kept
# deliberately narrow (must contain a "/" segment) so ordinary text with a
# slash (dates "7/23", "and/or") does not match.
_REGISTER_PATH_RE = re.compile(
    r"(?:/Users/[\w.-]+|/home/[\w.-]+|/tmp/|/private/tmp/)[\w./-]*"
    # ESTATE:BEGIN estate directory names in the internal-path recognizer (L-ADAPT _REGISTER_PATH_RE); the generic `bridge/` shape survives publicly
    r"|\b(?:routines|memory-engine|bridge|worklog)/[\w./-]+\.(?:py|sh|md)\b"
    # ESTATE:ELSE
    # r"|\bbridge/[\w./-]+\.(?:py|sh|md)\b"
    # ESTATE:END
)

# DGN-430: OS scheduler / process-plumbing terminology that should surface to a
# user only as an outcome ("set a daily reminder"), never as the mechanism.
_REGISTER_SCHEDULER_RE = re.compile(
    r"\b(?:launchd|launchctl|systemd|crontab|cron job|com\.[\w.-]+\.plist)\b",
    re.IGNORECASE,
)

# Two language-axis layers share the charset primitives below:
#
# DGN-686 v2 (drop tier, direction lock 2026-08-02): on a ko-locale instance
# the register guard DROPS a block whose scored PROSE (see
# _locale_register_prose -- fenced-code, bare-URL/deep-link and send_file::
# delivery lines excluded) is _LOCALE_MIN_LEN+ chars with ZERO Hangul. That is
# the only blocking action; there is no fragment-masking tier.
#
# DGN-429 v1 (advisory detector, log-only): charset-class counting AFTER
# stripping code fences, inline code spans, and URLs, so code-heavy or
# link-heavy replies never inflate the English ratio (low-false-positive
# contract):
#   fires iff  ascii_alpha >= _LANG_MIN_ALPHA
#          and hangul_count < _LANG_MAX_HANGUL
#          and ascii_alpha / (ascii_alpha + hangul_count) > _LANG_EN_RATIO
# Thresholds are the DGN-429 locked v1 hypotheses (pre-measurement); v2 tunes
# them from observed false-positive rates. The detector rides
# _register_findings only (advisory: log/tests/upstream consumers) -- the
# DGN-686 drop tier above stays the sole blocking authority. Gated by
# OUTPUT_LANG_GUARD (language axis) on top of BRIDGE_REGISTER_GUARD.
_HANGUL_RE = re.compile(r"[가-힣]")
_LOCALE_MIN_LEN = 80
_ASCII_ALPHA_RE = re.compile(r"[A-Za-z]")
# DGN-429 fence stripper (span form, DOTALL): removes whole ```...``` regions
# (or an unclosed trailing fence) from the SCORED text. Distinct from the
# line-anchored _CODE_FENCE_RE toggle used by _locale_register_prose.
_LANG_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://\S+")
_LANG_MIN_ALPHA = 20
_LANG_MAX_HANGUL = 5
_LANG_EN_RATIO = 0.70

# DGN-686 MAJOR-2: prose-scoring exemptions for the locale-register drop tier.
#
# A line whose STRIPPED form starts with the send_file:: marker is the
# legitimate bridge-consumed delivery form (formatting.extract_send_marker_paths
# is line-start anchored the same way); it is a machine marker, not register,
# so it is excluded from the scored prose (a long bare marker path never drops
# a delivery turn).
_SEND_FILE_LINE_PREFIX = "send_file::"
# A fenced-code fence line (```lang) toggles a code region. Lines inside a code
# region are not natural-language register (a code/JSON/log block legitimately
# carries no Hangul), so they are excluded from the scored prose.
_CODE_FENCE_RE = re.compile(r"^```")
# A line that is ONLY a URL / console deep-link (optionally wrapped in <> or (),
# no surrounding prose) is machine address, not register. A deep-link-only
# answer must not drop for "English 80+, zero Hangul".
_BARE_URL_RE = re.compile(r"^[<(]?https?://\S+[>)]?$")


def _lang_slipped(text: str) -> bool:
    """DGN-429 charset heuristic: True when a ko-locale final text reads as
    English. Pure function of the text and config.locale; never raises on any
    str input (regex substitutions and character counting only).
    """
    locale = getattr(config, "locale", "") or ""
    if not locale.startswith("ko"):
        return False
    stripped = _LANG_CODE_FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _URL_RE.sub(" ", stripped)
    alpha = len(_ASCII_ALPHA_RE.findall(stripped))
    if alpha < _LANG_MIN_ALPHA:
        return False
    hangul = len(_HANGUL_RE.findall(stripped))
    if hangul >= _LANG_MAX_HANGUL:
        return False
    return alpha / (alpha + hangul) > _LANG_EN_RATIO


# Interim locale gate: an instance-language script per non-English locale
# (config.locale is normalized to ko/en at load, so en has no entry and the
# gate is a no-op there). Unlike the DGN-686 drop tier (_register_guard,
# _LOCALE_MIN_LEN+ prose, every block) this judges INTERIM blocks only and
# has no length floor: the live leaks were one-line step notes ("Run open
# without --fit first to get packs.", 43 chars) that the 80-char floor lets
# through. Terminal / final-answer text never passes through it.
_LOCALE_SCRIPT_RE = {"ko": _HANGUL_RE}
_INTERIM_QUOTED_RE = re.compile(
    "\"[^\"\\n]{1,80}\"|\u201c[^\u201d\\n]{1,80}\u201d|\u2018[^\u2019\\n]{1,80}\u2019|'[^'\\n]{1,80}'")


def _interim_off_locale(text: str) -> bool:
    """True when an interim block on a non-English instance carries Latin
    prose and no character of the instance's script.

    Mixed blocks (any instance-script character) are kept. Code material is
    recognized structurally, by the same exemptions the DGN-429 detector
    uses (fenced code, inline code spans, URLs): a block made only of those
    is kept, and so is a block with no Latin letter at all (emoji, digits,
    punctuation) -- neither is English narration. A bare unfenced path or
    command DOES count as Latin prose: on a live surface it is internal
    plumbing (DGN-430), and a "looks like a command" guess would also pass
    the leak shape itself, which quotes a flag. Pure function of the text and
    config.locale.
    """
    locale = getattr(config, "locale", "") or ""
    script_re = _LOCALE_SCRIPT_RE.get(locale)
    if script_re is None or not text:
        return False
    # Quoted spans are the owner's own words echoed inside the narration
    # (rehearsal 2026-10-05: "Name answer: \"<owner name>\". Record it in the identity
    # file, then send q2.' passed because of the one quoted name). Judge the
    # instance-script presence on the text OUTSIDE quotes.
    if script_re.search(_INTERIM_QUOTED_RE.sub(" ", text)):
        return False
    prose = _LANG_CODE_FENCE_RE.sub(" ", text)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    prose = _URL_RE.sub(" ", prose)
    return bool(_ASCII_ALPHA_RE.search(prose))


def _register_findings(text: str, lang_check: bool = True) -> List[str]:
    """Return a list of register-violation labels found in text (may be empty).

    Pure detector -- no side effects, no mutation. Used by _register_guard and
    directly by the tests. lang_check=False skips the DGN-429 locale-register
    detector (error-path texts carry English error descriptions by design and
    are out of the language guard's scope).
    """
    findings: List[str] = []
    if _REGISTER_TOOL_RE.search(text):
        findings.append("tool-name")
    if _REGISTER_MARKER_RE.search(text):
        findings.append("send_file-marker")
    if _REGISTER_PATH_RE.search(text):
        findings.append("internal-path")
    if _REGISTER_SCHEDULER_RE.search(text):
        findings.append("scheduler-term")
    if lang_check and OUTPUT_LANG_GUARD and _lang_slipped(text):
        findings.append("locale-register")
    return findings


def _locale_register_prose(text: str) -> str:
    """Return the natural-language PROSE of text for the locale tier.

    Excludes lines that are not register (DGN-686 MAJOR-2): send_file::
    delivery markers, fenced-code regions (```...```), and bare URL / deep-link
    lines. A code-only or link-only reply therefore scores as empty prose and
    can never trip the "English 80+, zero Hangul" drop.
    """
    kept: List[str] = []
    in_code = False
    for ln in text.split("\n"):
        stripped = ln.strip()
        if _CODE_FENCE_RE.match(stripped):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith(_SEND_FILE_LINE_PREFIX):
            continue
        if _BARE_URL_RE.match(stripped):
            continue
        kept.append(ln)
    return "\n".join(kept)


def _register_guard(text: str) -> str:
    """Drop-only register guard (DGN-686 v2, direction lock 2026-08-02).

    ONE action, exactly two return paths:
    - locale-register drop: on a ko-locale instance, if the scored prose (see
      _locale_register_prose -- fenced code, bare-URL/deep-link, and send_file::
      delivery lines excluded) is _LOCALE_MIN_LEN+ chars with ZERO Hangul, the
      whole block is a working-English leak into the user channel -> return ""
      and log a WARNING.
    - otherwise return text UNCHANGED.

    There is NO fragment-masking tier. Partial leaks (a tool name / internal
    path / scheduler term inside an otherwise-Korean body) are an upstream
    (leader-summary) concern, not the bridge's -- the bridge does not touch
    them here. [[OPTIONS]] markers and legitimate Korean text (turn-start
    preambles included) always pass. Gated by BRIDGE_REGISTER_GUARD: an
    emergency env bypass (=0) returns all text unchanged.
    """
    if not BRIDGE_REGISTER_GUARD or not text:
        return text
    locale = getattr(config, "locale", "") or ""
    if locale.startswith("ko") and not _HANGUL_RE.search(text):
        prose = _locale_register_prose(text)
        if len(prose) >= _LOCALE_MIN_LEN and not _HANGUL_RE.search(prose):
            logger.warning(
                "Register guard (v2) dropped outgoing block: locale-register "
                "(%d chars, zero Hangul on a ko-locale instance)",
                len(text),
            )
            return ""
    return text


# DGN-429 hybrid leg 1 (prompt): language name injected into the model-facing
# output-language rule. config.locale is normalized to ko/en; anything else
# already fell back to en at config load.
_LOCALE_LANGUAGE_NAMES = {"ko": "Korean", "en": "English"}


# --- DGN-1141 stage 4: vendor contract injection (spawner declares vendor) ---
# The bridge is the SPAWNER of its sessions and the only party that knows the
# active channel at session-creation time, so it declares the vendor and
# injects the vendor's judgment/expression contract (vendors/<name>.md) into
# the system prompt. Markdown @-chain loading cannot express this (vendor is a
# SESSION property, not a workspace property; @ includes are static and fail
# with exit 0 + empty stderr -- measured, DGN-1141-M2 section 2), so the
# contract must ride the spawner.
#
# Failure directions (DGN-1141-M2 section 3):
#   - vendors/ directory absent -> the vendor doc layer has not shipped yet
#     (pre-relayout instance): inject nothing, fail-OPEN. A session without a
#     vendor contract is the legitimate default, not an error.
#     Stage-5 decision (DGN-1141-M9): this carve-out is KEPT past the
#     template relayout -- live instances receive bridge code refreshes
#     BEFORE the update channel ships vendors/ (stage-6 copy-set entry), so
#     removing it here would boot-die every pre-migration instance on its
#     next update. Remove it in the SAME stage-6 commit that adds vendors/
#     to the update.sh copy set.
#   - vendors/ present but the DECLARED vendor's file missing/unreadable/empty
#     -> wiring error, fail-CLOSED: raise. The module-level validation call
#     below turns that into a bridge BOOT DIE (same seam as pydantic config
#     validation), observable in launchd logs -- never a silent contract-less
#     session, because doc-load failures are otherwise invisible.
#   - the DECLARED vendor's per-instance OVERLAY (vendors/custom.<name>.md,
#     DGN-818 C2) is OPTIONAL by construction: the name is instance-owned
#     (update.sh assert_instance_ownership_convention), so an operator may or
#     may not have written one. Absent -> inject nothing (fail-OPEN); present
#     but blank -> WARN and skip, never boot-die: an instance-owned file must
#     not be able to brick the bot. Composition is canonical-first,
#     overlay-second (DGN-1141 section 3.4).
_VENDOR_NAME = "telegram"
_VENDOR_DIR = PROJECT_ROOT / "vendors"
# Instance-owned overlay prefix. This name was RESERVED by update.sh:506 and
# asserted by tests/dgn773_t5_migrate_step_m_selftest.sh long before any code
# read it -- the "존재 != 배선" shape DGN-1141 named (class 8). DGN-818 C2
# judged: keep the reservation, add the reader. This constant is that reader.
_VENDOR_OVERLAY_PREFIX = "custom."

# --- DGN-1256: editor/model receiver split (injection boundary marker) ---
# ESTATE:BEGIN the vendor inject-below marker is Dogany-owned doc vocabulary (L-ADAPT _VENDOR_INJECT_MARKER, DGN-1256). Extent = the declaration cluster; widening to the whole vendor cluster (:706-807 + its call sites) is M2/C2
# A vendor contract has TWO readers and only one of them costs tokens. The
# framework-ownership warning, the overlay how-to, the doc-hub prohibition and
# the rationale index are addressed to the human EDITING the file; the model
# needs the rules. Before this marker the whole file rode every session's
# system prompt, so the editor's half was paid for on every turn.
#
# Contract: the FIRST line whose stripped form equals the marker ends the
# editor preamble; only what follows is injected. No marker -> the whole file
# is injected (back-compat: an existing or third-party vendor file must not
# change behavior). An HTML comment is used because it is invisible in every
# markdown renderer, so the file still reads as one document.
_VENDOR_INJECT_MARKER = "<!-- dogany:inject-below -->"
# ESTATE:ELSE
# _VENDOR_INJECT_MARKER = "<!-- bridge:inject-below -->"
# ESTATE:END


def _injectable_vendor_text(text: str) -> str:
    """Return the model-facing half of a vendor doc (DGN-1256).

    Everything up to and including the first marker line is editor-facing and
    dropped. Absent marker -> the text is returned unchanged, which is what
    makes this back-compatible with every vendor file written before the
    marker existed.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == _VENDOR_INJECT_MARKER:
            return "".join(lines[i + 1:])
    return text


class VendorContractMissing(RuntimeError):
    """Declared vendor contract file could not be loaded (fail-closed)."""


def _load_vendor_contract() -> str:
    """Return the declared vendor's contract text ("" in the no-vendors world).

    Read fresh on every compose (not cached) so a REFRESH of the contract doc
    reaches the NEXT session without a bridge restart (DGN-1141-M2 section 8-3:
    document-read semantics chosen over code-baked text). The refresh that
    matters is the framework's: vendors/telegram.md is framework-owned and
    update.sh reverts hand edits (backing them up first), so "an owner edit
    reaches the next session" -- the wording this comment used to carry -- was
    true for one session and then false (DGN-1141 stage 8, M11 MAJOR-6). Live
    re-read still earns its keep on a self-update, which lands new doc bytes
    under a running bridge.
    """
    if not _VENDOR_DIR.is_dir():
        return ""
    vendor_file = _VENDOR_DIR / f"{_VENDOR_NAME}.md"
    try:
        text = vendor_file.read_text(encoding="utf-8")
    except OSError as e:
        raise VendorContractMissing(
            f"vendor '{_VENDOR_NAME}' declared but contract file "
            f"{vendor_file} is unreadable: {e}"
        ) from e
    if not text.strip():
        raise VendorContractMissing(
            f"vendor '{_VENDOR_NAME}' contract file {vendor_file} is empty"
        )
    injectable = _injectable_vendor_text(text)
    if not injectable.strip():
        # Marker present with nothing under it is the same wiring error as an
        # empty file -- fail-CLOSED, for the same reason: a contract-less
        # session must never be silent.
        raise VendorContractMissing(
            f"vendor '{_VENDOR_NAME}' contract file {vendor_file} has no "
            f"content below the {_VENDOR_INJECT_MARKER} marker"
        )
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    # Observability INFO line (DGN-1142 section 3.1): line present = loaded
    # what; boot-die log = could not load; neither = bridge never came up.
    # DGN-1256: bytes= is what the MODEL receives (the whole point of the
    # shrink is unobservable otherwise); file_bytes= keeps the on-disk size,
    # and sha= still hashes the whole file so a doc version is identifiable.
    logger.info(
        "vendor=%s file=%s bytes=%d file_bytes=%d sha=%s",
        _VENDOR_NAME,
        vendor_file,
        len(injectable.encode("utf-8")),
        len(text.encode("utf-8")),
        sha,
    )
    return injectable


def _load_vendor_overlay() -> str:
    """Return the declared vendor's per-instance overlay text ("" when none).

    `vendors/custom.<vendor>.md` is the instance-owned half of the vendor
    contract: the framework refreshes `vendors/<vendor>.md` on every update and
    reverts hand edits, so the overlay is the ONLY place an instance (or
    another consumer bolting this bridge onto its own Claude Code) can state
    channel judgment rules that survive an update.

    Failure direction differs from the canonical contract ON PURPOSE. The
    canonical file is framework-shipped: missing/blank means the wiring broke,
    so it boot-dies (fail-CLOSED). The overlay is operator-written and
    optional: missing means "nothing to add" (fail-OPEN), and blank means the
    operator started one and left it empty -- worth a WARN, never a die.
    """
    if not _VENDOR_DIR.is_dir():
        return ""
    overlay_file = _VENDOR_DIR / f"{_VENDOR_OVERLAY_PREFIX}{_VENDOR_NAME}.md"
    try:
        text = overlay_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as e:
        # Present but unreadable is not "absent": say so instead of degrading
        # to silence, then continue without it.
        logger.warning("vendor overlay %s unreadable: %s", overlay_file, e)
        return ""
    if not text.strip():
        logger.warning(
            "vendor overlay %s is present but blank -- injecting nothing",
            overlay_file,
        )
        return ""
    injectable = _injectable_vendor_text(text)
    if not injectable.strip():
        # Same marker convention as the canonical contract, opposite failure
        # direction (DGN-818 C2): an instance-owned file may never boot-die.
        logger.warning(
            "vendor overlay %s has no content below the %s marker -- "
            "injecting nothing",
            overlay_file,
            _VENDOR_INJECT_MARKER,
        )
        return ""
    logger.info(
        "vendor-overlay=%s file=%s bytes=%d file_bytes=%d sha=%s",
        _VENDOR_NAME,
        overlay_file,
        len(injectable.encode("utf-8")),
        len(text.encode("utf-8")),
        hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
    )
    return injectable


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _warn_heading_collisions(*injectables) -> None:
    """Duplicate-section lint at compose time (DGN-1141-M7 section 6-1).

    No injectable may restate what another already teaches -- a shared '## '
    section title is the cheap tripwire for that (title-level check only, by
    design). Log-warn, never block: a collision is a doc bug, not a boot hazard.

    DGN-818 C2: the composition is N injectables now (contract / overlay /
    machine fragment / conditional fragments), so the lint compares every
    PAIR instead of the original hardcoded two. A two-injectable call behaves
    exactly as before.

    Each argument is a ``(label, text)`` pair; empty texts are skipped so an
    absent overlay cannot manufacture a collision.
    """
    present = [(lbl, txt) for lbl, txt in injectables if txt]
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            (a_lbl, a_txt), (b_lbl, b_txt) = present[i], present[j]
            shared = set(_HEADING_RE.findall(a_txt)) & set(
                _HEADING_RE.findall(b_txt)
            )
            if shared:
                logger.warning(
                    "injection overlap: '%s' and '%s' both declare section(s) "
                    "%s -- duplicate contract text drifts; each item lives in "
                    "exactly one injectable (DGN-1141 section 6-1)",
                    a_lbl,
                    b_lbl,
                    sorted(shared),
                )


def _compose_system_prompt() -> str:
    """Compose the bridge system prompt, appending the DGN-429 output-language
    rule (hybrid leg 1: prompt first, charset detector as backstop).

    DGN-1141 stage 4: fixed composition order -- vendor contract doc (judgment
    rules) + i18n machine fragment (channel grammar) + conditional fragments
    (fold / language). One item lives in exactly one injectable; the heading
    lint above trips when the vendor doc restates a machine section.

    DGN-699 FATAL-1: the fold register fragment is gated on the CURRENT
    effective interim mode so that suppress/inline/off turns never receive a
    premise that is false for them ("the user already saw your progress live").

    Gated by OUTPUT_LANG_GUARD: when off (e.g. a dev agent legitimately working
    in the English technical register) the language fragment is skipped.
    """
    vendor = _load_vendor_contract()
    overlay = _load_vendor_overlay()
    base = messages.SYSTEM_PROMPT
    _warn_heading_collisions(
        (f"vendors/{_VENDOR_NAME}.md", vendor),
        (f"vendors/{_VENDOR_OVERLAY_PREFIX}{_VENDOR_NAME}.md", overlay),
        ("machine fragment (messages.SYSTEM_PROMPT)", base),
    )
    # Canonical first, overlay second (DGN-1141 section 3.4): the overlay is
    # read AFTER the framework contract, so where the two speak to the same
    # point the instance's sentence is the later one the model sees.
    if overlay:
        base = overlay.rstrip() + "\n\n" + base.lstrip("\n")
    if vendor:
        base = vendor.rstrip() + ("\n\n" if overlay else "") + base
    if _effective_interim_mode() == "fold":
        base = base + messages.SYSTEM_PROMPT_FOLD_FRAGMENT
    if not OUTPUT_LANG_GUARD:
        return base
    locale = getattr(config, "locale", "") or "en"
    language = _LOCALE_LANGUAGE_NAMES.get(locale, "English")
    return base + messages.OUTPUT_LANG_PROMPT_TEMPLATE.format(language=language)


# Boot-time fail-closed validation (DGN-1141-M2 section 3): a declared vendor
# whose contract cannot be read must kill the bridge AT BOOT (this module is
# imported by bridge.bot before polling starts), not limp through sessions
# with no contract. In the no-vendors world this is a no-op.
_load_vendor_contract()


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
    options_classifier_injected: bool = False
    streamed: bool = False
    timed_out: bool = False
    resume_session_id: Optional[str] = None
    partial_preserved: bool = False
    draft_message_ids: List[int] = field(default_factory=list)
    # DGN-686: an is_error result whose LOCKED notice offers a [retry] action.
    # The bot layer renders the retry button when this is True (auth errors set
    # it False -- re-login is required, retry would just fail again).
    retry_offer: bool = False
    # DGN-686 MAJOR-1: classification of a failure ("transient"/"auth"/"other")
    # or None on success. The bot seat auto-retries ONCE on "transient" before
    # showing the retry notice -- the reader loop never re-dispatches itself.
    error_kind: Optional[str] = None
    # DGN-1253: True when the body was assembled from MULTIPLE terminal
    # segments (a Stop-hook block let the model continue the turn and emit
    # another terminal message). The bot seat must then bypass the no-op
    # skip in the streamed-prose finalize edit: the live draft glued BOTH
    # segments verbatim, while the assembled body may differ from that glue
    # (inter-segment dedup), so "draft already shows this text" cannot be
    # assumed.
    turn_assembled: bool = False
    # DGN-1586 (spec 3.6): typed notice-carrier metadata. When the finalize
    # seam synthesized an owner notice into the tail of `content`, these
    # identify the spool record + reserved attempt so the bot seat can
    # promote delivered from the SEND/EDIT SUCCESS of the message that
    # actually carried the notice text -- ChatResponse completion is NOT
    # delivery evidence, and an accidental identical string in the model
    # body is never a receipt (only this typed metadata is).
    notice_id: Optional[str] = None
    notice_attempt: Optional[int] = None
    notice_kind: Optional[str] = None
    notice_version: Optional[str] = None
    notice_text: Optional[str] = None
    # Background jobs killed by a timeout soft stop or hard teardown
    # (one owner-facing name or "" per job). The bot seat
    # sends the same count + bullets notice as the auto-interrupt path.
    killed_jobs: List[str] = field(default_factory=list)


# --- Inbound turn context for tools (per-turn, plain context, no interception) ---
# The CLI child's env is fixed at spawn, so the per-turn facts a tool needs to
# address the SAME conversation (which chat / topic, which inbound message)
# ride a small file the bridge rewrites at every owner turn and removes when
# the turn ends. Tools read it; nothing here inspects or alters the owner's
# message or the model's reply. A tool that finds no file is not inside an
# owner turn (background / injected turn) and must not assume one.
INBOUND_CONTEXT_FILENAME = "inbound-context.json"
INBOUND_CONTEXT_SCHEMA = 1
# DGN-1687: a dispatch-return turn is ownerless but must first surface the
# completed dispatch result before it starts new work. The bridge owns this
# short-lived record; the PreToolUse gate is only its reader.
DISPATCH_RETURN_CONTEXT_FILENAME = "dispatch-return-context.json"
DISPATCH_RETURN_CONTEXT_SCHEMA = 1
# Bridge process start identity: changes on every restart, stable while alive.
_RUNTIME_EPOCH = "%d.%d" % (os.getpid(), int(time.time()))


def inbound_context_path() -> Path:
    return PROJECT_ROOT / ".telegram_bot" / INBOUND_CONTEXT_FILENAME


def dispatch_return_context_path() -> Path:
    return PROJECT_ROOT / ".telegram_bot" / DISPATCH_RETURN_CONTEXT_FILENAME


def _write_dispatch_return_context(turn_id: str, user_id: int, session_id: str,
                                   visible: bool = False,
                                   delivering: bool = False,
                                   options_delivered: bool = False) -> None:
    """Publish the narrow result-first latch for one injected turn.

    A delivery acknowledgement, not model prose, flips ``visible``. This is
    deliberately separate from inbound-context: injected turns have no owner
    inbound message and must not masquerade as one.

    DGN-1715: ``delivering`` is written the moment the first text reaches the
    bridge, BEFORE the owner push is awaited. The CLI runs the next
    PreToolUse hook without waiting for the bridge's Telegram round trip, so
    the gate uses this mark to wait for the delivery verdict instead of
    denying a tool whose result line is already on its way.

    DGN-1732: ``options_delivered`` records that the delivered first text
    already carried an [[OPTIONS]] keyboard, so the Stop forcing seat does
    not ask the model for a second proposal + keyboard.
    """
    try:
        path = dispatch_return_context_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": DISPATCH_RETURN_CONTEXT_SCHEMA,
            "turn_id": turn_id,
            "runtime_epoch": _RUNTIME_EPOCH,
            "user_id": user_id,
            "session_id": session_id,
            "visible": bool(visible),
            "delivering": bool(delivering) and not visible,
            "options_delivered": bool(options_delivered) and bool(visible),
            "ts": time.time(),
        }
        tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("dispatch-return context write failed: %s", exc)


def _clear_dispatch_return_context(turn_id: str) -> None:
    try:
        path = dispatch_return_context_path()
        if json.loads(path.read_text(encoding="utf-8")).get("turn_id") == turn_id:
            path.unlink()
    except Exception:
        pass


def _write_inbound_context(request_id: str, user_id: int, inbound: Dict[str, Any]) -> None:
    """Atomically publish this turn's context (fail-soft: a lost file only
    means tools run without conversation binding, never a failed turn)."""
    try:
        path = inbound_context_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": INBOUND_CONTEXT_SCHEMA,
            "request_id": request_id,
            "runtime_epoch": _RUNTIME_EPOCH,
            "user_id": user_id,
            "chat_id": inbound.get("chat_id"),
            "thread_id": inbound.get("thread_id"),
            "message_id": inbound.get("message_id"),
            "source": inbound.get("source", "message"),
            "ts": time.time(),
        }
        tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("inbound context write failed: %s", exc)


def _clear_inbound_context(request_id: str) -> None:
    """Remove the file only if it still belongs to this request (a queued next
    turn may already have replaced it)."""
    try:
        path = inbound_context_path()
        if json.loads(path.read_text(encoding="utf-8")).get("request_id") == request_id:
            path.unlink()
    except Exception:
        pass


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
    # Inbound turn context (chat / thread / message id) published to tools.
    inbound: Optional[Dict[str, Any]] = None
    inbound_request_id: str = ""
    sent: bool = False
    # DGN-1819 B: the reader consumed this turn's ResultMessage and is inside
    # _finalize_result (which may await the options classifier for seconds).
    # The turn is over on the CLI side: interrupt() must not drain it -- the
    # drain would count a trailing result that never comes (discard_results
    # leak) and the reader's popleft would then take the NEXT request.
    finalizing: bool = False
    last_typing_at: float = 0.0
    last_assistant_texts: List[str] = field(default_factory=list)
    # DGN-1253: ordered per-TERMINAL-message capture for turn assembly. A
    # Stop-hook block lets the model continue the SAME turn and emit a second
    # terminal AssistantMessage; the per-message reset of last_assistant_texts
    # then erases the first (real) answer from the finalize assembly -- body,
    # send_file:: attachments and the [[OPTIONS]] keyboard all vanish at once
    # (measured 2026-09-03, three consecutive losses). Each terminal message's
    # guarded text is appended here in turn order; _finalize_result assembles
    # from this list when it holds 2+ segments and keeps the exact legacy
    # single-message expression otherwise (byte-identical non-hook path).
    # Non-terminal (interim) text NEVER enters this list -- fold/inline
    # interim routing (DGN-682/699/947) is untouched.
    final_segments: List[str] = field(default_factory=list)
    # DGN-1703: a Stop hook blocked this turn after its answer streamed; the
    # next main-agent message carrying text retracts that answer from the live
    # surface (StreamingMessageHandler.supersede_step). Cleared once consumed.
    stop_block_superseded: bool = False
    # DGN-1850: blocking_stop_gate_window() was open when this turn started.
    # Main-thread text from a message with no tool call is HELD here instead
    # of reaching the live/fold surfaces: a later tool call proves it was
    # narration (released as usual); a Stop-hook re-prompt proves it was a
    # blocked answer (discarded); the ResultMessage makes it the final answer
    # (_finalize_result delivers it as one new message). Entries are
    # (guarded text, raw text, is_terminal, mint_hold, message seq).
    hold_terminal: bool = False
    held_blocks: List[Tuple[str, str, bool, bool, int]] = field(default_factory=list)
    held_seq: int = 0
    # DGN-1850: the text a Stop block discarded. Delivered only when the turn
    # ends with no replacement text (the DGN-1651/1703 rule: a text-less
    # regeneration leaves the answer standing).
    held_discarded: List[str] = field(default_factory=list)
    # DGN-1850: final_segments index where the current model step began; a
    # Stop block inside the hold drops the segments its step captured.
    step_segment_start: int = 0
    # DGN-1857: raw text of the CLI's auth-failure message this turn. Set at
    # ingestion (the text never reaches a live surface); _finalize_result then
    # finishes the turn exactly like an is_error auth result.
    cli_auth_failure: Optional[str] = None
    synthetic_response: Optional[str] = None
    streaming_handler: Optional[Any] = None
    # DGN-086: count ToolUseBlocks in main-agent (non-subagent) messages for
    # placeholder-flake detection. Incremented in _reader_loop on each
    # AssistantMessage that has no parent_tool_use_id.
    tool_use_count: int = 0
    # DGN-670: single-retry loop guard. 0 = no flake retry dispatched yet;
    # 1 = the one allowed retry is (or was) in flight. Monotonic per request.
    flake_retry_count: int = 0
    # DGN-670 M1: subagent activity observed THIS turn. Set in _reader_loop
    # when a Task ToolUseBlock appears in a main-agent message or when any
    # parent_tool_use_id-bearing message streams by (subagent inner messages
    # are skipped from capture, so the evidence must be recorded during
    # reader iteration -- the final content never carries it). Recovery only
    # fires when this is True: a flake-looking reply with NO subagent
    # activity (main-agent plain text, Bash-dispatched background
    # juniors, meta-discussion of this bug) is never retried.
    subagent_activity: bool = False
    # DGN-670 M1: a Task with run_in_background=true was launched this turn.
    # A "subagent working in background" status is then LEGITIMATE and must
    # never be blocked or retried.
    background_task_launched: bool = False
    # DGN-682 D4/D9: fold-mode interim TextBlock capture buffer. Owned by the
    # request: a fresh empty list per turn, discarded together with the request
    # on EVERY termination path (normal, is_error, /stop, timeout) -- no
    # cross-turn bleed. Capture applies _scaffold_guard ONLY (D5).
    interim_texts: List[str] = field(default_factory=list)
    # DGN-699 D2: growing-fold state, deliberately SEPARATE from the
    # streaming_handler drafts (fold_msg_id must never enter
    # ChatResponse.draft_message_ids -- D4). fold_buf accumulates the same
    # captured narration that interim_texts holds; the dedicated fold
    # dispatch renders/edits from it. Per-request lifetime: a retry's new
    # request starts a fresh fold bubble (D7).
    fold_msg_id: Optional[int] = None
    fold_buf: List[str] = field(default_factory=list)
    # Throttle/lifecycle internals for the fold dispatch (D3/D7/D8).
    # fold_dirty removed (grill MINOR): field was written but never read for
    # a branch decision -- throttle logic relies solely on fold_last_edit_at
    # (time gate) and fold_retry_at (RetryAfter backoff).
    # fold_first_interim_at removed (grill MINOR): only served the T-gate
    # (FOLD_CREATE_MIN_SECS) which was also removed as dead -- the count gate
    # always fires before elapsed time would reach the threshold.
    fold_last_edit_at: float = 0.0
    fold_retry_at: float = 0.0
    fold_finalized: bool = False
    # DGN-1683: mint-verb turn mute (bridge/mint_gate.py). mint_call_ids holds
    # the tool_use ids of main-agent Bash calls that ran the verb; their
    # tool_result JSON sets mint_mute (latched, screen > n13). mint_spoke
    # records that the agent produced owner-bound text after the verdict --
    # the N13 fallback is only ever a replacement, never bridge-initiated.
    mint_call_ids: set = field(default_factory=set)
    mint_mute: Optional[str] = None
    mint_spoke: bool = False
    # DGN-1683 provisional hold: the CLI streams each block of one API
    # message as its own event, so narration written before a verb call
    # reaches the reader with no call in sight. Once the turn loads the mint
    # skill or calls a verb (mint_gate.arms_hold) mint_armed latches and
    # every block _route_text_block would put on a live surface is parked
    # in mint_held instead -- (guarded text, raw text, is_terminal,
    # mint_hold, mint_seq). The final assembly still sees the text. A
    # verdict discards the parked blocks; a turn that ends without one
    # releases them (_settle_mint_held). mint_seq numbers main-agent
    # assistant messages so the last one (delivered by finalize) is known.
    mint_armed: bool = False
    mint_held: List[Tuple[str, str, bool, bool, int]] = field(default_factory=list)
    mint_seq: int = 0


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
    # DGN-1842: the DGN-1703 supersede for no-pending turns. step_start is the
    # proactive_texts index where the current model step began (a main-thread
    # user message closes a step); superseded is the [start, end) span a
    # Stop-hook re-prompt marked as a blocked answer. The span is dropped only
    # when replacement text arrives, so a text-less regeneration leaves the
    # earlier answer standing. Both reset with the buffer at every turn end.
    proactive_step_start: int = 0
    proactive_superseded: Optional[Tuple[int, int]] = None
    # DGN-1857: a no-pending turn hit the CLI's auth failure; its result
    # surfaces the re-login notice. Resets with the buffer.
    proactive_auth_failure: bool = False
    last_proactive_sent: Optional[str] = None
    # DGN-1687: set only while this stream is processing a dispatch-return
    # injection. The bridge clears the shared latch when its Result arrives.
    dispatch_return_turn_id: Optional[str] = None
    dispatch_return_result_sent: bool = False
    # DGN-1732: the text the result-first push already put on the owner's
    # screen this turn. The turn's finalize subtracts it (options.
    # subtract_delivered) so the final never repeats that bubble/keyboard.
    dispatch_return_delivered: Optional[str] = None
    # DGN-581: count of trailing ResultMessages to swallow. A soft interrupt
    # drains the pending deque, but the CLI still emits a ResultMessage (and
    # possibly tail AssistantMessages) for each already-dispatched turn; with
    # no pending request left the reader loop would misroute that tail to the
    # proactive-push path. Each swallow decrements the counter.
    discard_results: int = 0
    # DGN-996: last session id THIS stream pre-persisted to sessions.json.
    # Dedupe marker for _persist_session_id -- prevents redundant disk writes
    # AND prevents a still-draining old reader from re-writing its own sid
    # after a /new//model reset already nulled session_id on disk (the old
    # sid is recorded here, so the re-write is skipped).
    persisted_session_id: Optional[str] = None
    # DGN-1016: live task tracking approximation. task_id -> monotonic start
    # time. Fed by _track_task_lifecycle from the reader loop: add on
    # task_started, discard on a terminal status from task_notification OR
    # task_updated (the SDK documents that a terminal state can arrive via
    # either message alone). No CLI/SDK "list running tasks" query exists
    # (SDK 0.2.110 only exposes stop_task), so this passive stream-derived
    # set is the only registry the bridge can hold. It dies with the stream
    # state on teardown, so a hard stop can never leak phantom entries
    # across sessions.
    active_tasks: Dict[str, float] = field(default_factory=dict)
    # DGN-1015: task_id -> spawn-time description (from TaskStartedMessage),
    # kept in lockstep with active_tasks so a kill notice can name what died
    # instead of just counting it.
    task_descriptions: Dict[str, str] = field(default_factory=dict)
    # DGN-1593 r2: task_id -> TaskStartedMessage.task_type, same lockstep.
    # Only a subagent's description can be its owner-facing name (the Agent
    # tool description); a shell task's may be the command line.
    task_types: Dict[str, str] = field(default_factory=dict)
    # DGN-1588/DGN-1591/DGN-1620: None means no injection in this turn;
    # "quiet" suppresses by default and "loud" latches delivery when any
    # injection demands a report. Consumed at every turn boundary.
    injected_turn_mode: Optional[str] = None
    # DGN-1689: Claude Code can open an ownerless turn solely to deliver a
    # completed background-task notification.  Reset at every no-pending
    # ResultMessage.
    # DGN-1642: this flag no longer makes the turn quiet.  A wake-up is a
    # turn that exists (the bridge cannot stop Claude Code creating it), and
    # a turn worth creating is a turn worth delivering: the author cannot see
    # a quiet default it was never told about, so it dropped real merge/push
    # reports and pending owner questions (observed 2026-09-27 18:04).  Silence
    # is the author's explicit NO_PUSH.  The flag now only feeds a
    # measurement log line.
    task_notification_wakeup: bool = False
    # DGN-1015: descriptions of background subagents killed by the MOST
    # RECENT interrupt() call for this user (see interrupt()). Root cause of
    # the 2026-08-22 09:33 silent death: an interrupt aborts the CLI's
    # session-wide abort tree, which kills every in-session background
    # subagent WITHOUT ever emitting a task_notification/task_updated
    # terminal event for them -- so active_tasks would otherwise leak these
    # entries as phantom "still live" forever (no lifecycle event will ever
    # arrive to clear them). interrupt() clears them synchronously and
    # stashes one entry per killed task here -- its owner-facing name, or ""
    # (DGN-1593 r2, _owner_task_name); pop_interrupt_killed() reads + clears.
    interrupt_killed_descriptions: List[str] = field(default_factory=list)
    # DGN-1112: SdkBridge._cred_gen at creation. A /login bumps the bridge
    # counter; _get_or_create_stream recreates an idle stream whose value lags.
    cred_gen: int = 0


# ESTATE:BEGIN DGN-1593 the background-job name registry is an estate routines/lib module loaded through the manifest-excluded artifacts layer; the public build has no registry, so killed jobs are counted without names
def _bg_job_notice():
    """routines/lib/bg_job_notice.py (the background-job name registry), or
    None when it cannot be loaded."""
    try:
        from bridge.artifacts import _load_path_module

        return _load_path_module(
            "bg_job_notice",
            PROJECT_ROOT / "routines" / "lib" / "bg_job_notice.py",
        )
    except Exception:
        return None
# ESTATE:ELSE
# def _bg_job_notice():
#     """The background-job name registry, or None: this build has none."""
#     return None
# ESTATE:END


def _owner_task_name(task_id: str, description: str, task_type: str) -> str:
    """DGN-1593 r2 (owner 2026-10-02 08:47): the name the owner already
    knows a killed background task by, or "" when none is recoverable.

    1. A background Bash/Monitor/Workflow job: the name its START push and
       workbench row used (DGN-1820: the launch description alone), read
       from bg_job_notice's state, keyed by the same task id.  A launch
       whose name the gate refused never got a START, so it has no name.
    2. A subagent: its Agent description (the workbench row's label),
       only when it passes the same owner-name gate (instance language, no
       execution identifier).
    Anything else -- a shell task outside the registry, whose SDK
    description may be the command line -- stays nameless.  Fail-open to
    "": a lookup problem costs the bullet, never the count line.
    """
    lib = _bg_job_notice()
    if lib is None:
        return ""
    try:
        rec = None
        with open(lib.state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            rec = data.get(task_id)
        if isinstance(rec, dict):
            if not rec.get("start_sent"):
                return ""
            # The slot (a CLI or tool name) is never a fallback here.
            return lib._unlabelled(rec.get("name"), rec.get("slot") or "")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("bg name lookup failed for %s: %s", task_id, e)
        return ""
    try:
        if "agent" in (task_type or "") and description:
            if lib.name_problem(description) is None:
                return description.strip()
    except Exception as e:
        logger.warning("subagent name gate failed: %s", e)
    return ""


class SdkBridge:
    """Routes Telegram messages through per-user persistent SDK streams."""

    def __init__(self) -> None:
        self.project_root = PROJECT_ROOT
        self._streams: Dict[int, _UserStreamState] = {}
        self._stream_init_locks: Dict[int, asyncio.Lock] = {}
        self._cred_gen = 0
        logger.info("SdkBridge initialized for %s", self.project_root)
        if not TASK_LIFECYCLE_AVAILABLE:
            # Silence here would be the DGN-1015 failure mode: the guard is
            # off and nobody can tell. Say it once per boot.
            logger.warning(
                "auto-interrupt background guard INACTIVE: this "
                "claude-agent-sdk build has no task-lifecycle messages "
                "(TaskStartedMessage/TERMINAL_TASK_STATUSES). Background "
                "subagents remain killable by the DGN-911 auto-interrupt. "
                "Upgrade claude-agent-sdk to re-arm the guard."
            )

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
            # DGN-429 hybrid leg 1: base fragment + output-language rule.
            "system_prompt": _compose_system_prompt(),
            "can_use_tool": can_use_tool,
            "permission_mode": "default",
            # DGN-460: default SDK transport buffer (1MB) is too small for
            # tool results carrying inline base64 media; raise it (env-tunable).
            "max_buffer_size": CLAUDE_MAX_BUFFER_SIZE,
            # DGN-1586 (spec 3.3): surface marker, owned by the session
            # spawner. The SDK merges this into the CLI child env, so
            # SessionStart hooks (version-check.py) fork their injection
            # wording on it. Shared vars like TELEGRAM_BOT_TOKEN are NOT
            # surface evidence (detached sessions carry them too); this
            # dedicated marker is the ONLY discriminator, and non-bridge
            # child launchers (dispatch-detached.sh) unset it.
            # ESTATE:BEGIN DGN-1586 the marker name is estate topology (the estate's SessionStart hooks fork their surface wording on it); the public bridge build has no hook consumer for it (L-SEAM _create_user_stream, DOGANY_BRIDGE_SESSION)
            "env": {"DOGANY_BRIDGE_SESSION": "1"},
            # ESTATE:END
            # DGN-670 F1: mechanical executor-contract injection on every Task
            # prompt (prevention). can_use_tool is NOT invoked for allowed_tools
            # calls, so this must ride the PreToolUse hook path.
            "hooks": {
                "PreToolUse": [
                    HookMatcher(
                        matcher="Task", hooks=[_task_executor_contract_hook]
                    )
                ],
            },
        }
        if model:
            opts["model"] = model
        # DGN-1814: always pass the machine CLI when one resolves (explicit
        # CLAUDE_CLI_PATH > PATH > ~/.local/bin); omitting cli_path makes the
        # SDK fall back to its BUNDLED CLI, which lags the machine CLI.
        cli_path = resolve_claude_cli()
        if cli_path:
            opts["cli_path"] = cli_path

        logger.info(
            "Creating SDK stream for user %s (max_buffer_size=%d)",
            user_id,
            CLAUDE_MAX_BUFFER_SIZE,
        )
        client = ClaudeSDKClient(options=ClaudeAgentOptions(**opts))
        await client.connect()
        state = _UserStreamState(client=client, model=model)
        state_holder["state"] = state
        state.reader_task = asyncio.create_task(self._reader_loop(user_id, state))
        state.typing_task = asyncio.create_task(self._typing_keepalive_loop(user_id, state))
        return state

    async def _disconnect_user_stream(
        self, user_id: int, cancel_message: Optional[str] = None, *, silent: bool = False
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
        # DGN-612: `silent=True` is the ONLY silent-teardown signal. A queued
        # (not-yet-dispatched) second request's future is still resolved here
        # -- but with content="" instead of a user-facing string -- so the
        # normal reply path (bot._reply_smart: `if display.strip():`) emits
        # nothing for it. This is deliberately separate from `cancel_message`
        # defaulting to None: every OTHER teardown caller in this file invokes
        # `_disconnect_user_stream(user_id)` with no cancel_message and RELIES
        # on that resolving pending futures with TASK_TERMINATED (stale-stream
        # recreate, model/session swap, reconnect-and-retry) -- collapsing
        # "no message supplied" into "stay silent" would silence those too.
        # Only handle_timeout_preserve passes silent=True, because the single
        # user-facing "still working" notice for that path is owned solely by
        # _auto_resume_loop (bot.py) -- see that call site for the full story.
        msg = cancel_message or messages.TASK_TERMINATED
        while state.pending:
            req = state.pending.popleft()
            # DGN-699 D7 (_disconnect_user_stream cleanup hook): this teardown
            # path never reaches _finalize_result, so an orphaned grown fold
            # is confirmed here (collapse + stop marker). Idempotent: paths
            # that already finalized (timeout/stop/finalize) are no-ops.
            await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content="" if silent else msg,
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
                pgid = os.getpgid(pid)
                own_pgid = os.getpgid(os.getpid())
                if pgid != own_pgid:
                    # DGN-484: kill the entire process group so zsh
                    # session-leader wrappers (PPID=1 orphans) are reaped
                    # together with the CLI child. The guard prevents the
                    # bridge from killing itself.
                    os.killpg(pgid, signal.SIGKILL)
                    logger.warning(
                        "Force-killed orphan CLI process group pgid=%s (pid=%s) for user %s",
                        pgid,
                        pid,
                        user_id,
                    )
                else:
                    # pgid matches bridge's own group -- fall back to single-pid
                    # kill to avoid killing the bridge process group.
                    os.kill(pid, signal.SIGKILL)
                    logger.warning(
                        "Force-killed orphan CLI subprocess pid=%s (shared pgid=%s, single-pid fallback) for user %s",
                        pid,
                        pgid,
                        user_id,
                    )
            except ProcessLookupError:
                pass
            except PermissionError as e:
                logger.error(
                    "Permission denied force-killing CLI subprocess pid=%s for user %s: %s",
                    pid,
                    user_id,
                    e,
                )
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
            cred_gen = getattr(self, "_cred_gen", 0)
            if state and state.cred_gen != cred_gen and not state.pending:
                # DGN-1112: a /login replaced the stored credential. The CLI
                # child behind this client authenticated with the old one;
                # a fresh child reads the store anew. Only at an idle
                # boundary (nothing pending), so no in-flight turn is cut.
                logger.info("Credentials renewed; recreating idle stream for user %s", user_id)
                await self._disconnect_user_stream(user_id)
                state = None
            if not state:
                state = await self._create_user_stream(user_id, model)
                state.cred_gen = cred_gen
                self._streams[user_id] = state
            return state

    def mark_credentials_renewed(self) -> None:
        """DGN-1112: a /login stored a new credential. Every live stream is
        recreated at its next idle request boundary; no bridge restart."""
        self._cred_gen = getattr(self, "_cred_gen", 0) + 1

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
        if head.inbound:
            _write_inbound_context(head.inbound_request_id, head.user_id, head.inbound)
        await state.client.query(head.user_message, session_id=head.sent_session_id)

    @staticmethod
    def _clean_response(response: str) -> str:
        cleaned = _ANSI_RE.sub("", response)
        cleaned = "".join(c for c in cleaned if ord(c) >= 32 or c in "\n\r\t")
        return cleaned.strip()

    async def _persist_session_id(
        self, user_id: int, state: _UserStreamState, sid: Optional[str]
    ) -> None:
        """DGN-996: pre-persist the live session id to sessions.json the moment
        the SDK delivers it (init SystemMessage / injected-turn result), instead
        of waiting for a user turn to complete (bot._save_session_id).

        Why: after a bridge restart the first turns are often INJECTED turns
        (cron-inject / session-inbox) that never produce a ChatResponse, so the
        turn-completion save path never runs and sessions.json keeps the OLD
        process's sid.  status-footer's _is_owner_session gate then stays closed
        and the workbench (dashboard.md) is never regenerated.

        Guard interplay (deliberate): this writes ONLY the on-disk value.  It
        must NOT touch bot._runtime_active_sessions -- that set is the
        cross-process guard in bot._effective_session_id which decides whether
        the persisted sid may be used as a RESUME target.  Membership is granted
        only by real turn completion / explicit resume in bot.py, so a fresh
        process still refuses to resume a previous process's session even
        though the disk value is now always fresh.

        Double-write with bot._save_session_id is harmless: same sid, and the
        dedupe marker (state.persisted_session_id) skips redundant disk writes.
        Fail-silent: persistence failure must never break the reader loop.
        """
        if not sid or sid == state.persisted_session_id:
            return
        try:
            await session_manager.update_session(user_id, {"session_id": sid})
            state.persisted_session_id = sid
        except Exception as e:
            logger.warning(
                "Session id pre-persist failed for user %s: %s", user_id, e
            )

    @staticmethod
    def _observe_mint_results(user_id: int, req: _PendingRequest, msg: Any) -> None:
        """DGN-1683: fold the mint verb's tool_result JSON into req.mint_mute.

        Only results answering a tool_use id recorded in req.mint_call_ids
        (a main-agent Bash call that ran the verb) are read. Never raises
        into the reader loop.
        """
        try:
            content = getattr(msg, "content", None)
            if not req.mint_call_ids or not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, ToolResultBlock):
                    continue
                if block.tool_use_id not in req.mint_call_ids:
                    continue
                verdict = mint_gate.verdict_from_result(block.content)
                merged = mint_gate.merge(req.mint_mute, verdict)
                if merged != req.mint_mute:
                    # The pre-verdict text parked by the provisional hold
                    # was narration of this mint flow: it never surfaces.
                    req.mint_held = []
                    logger.info(
                        "mint turn mute armed for user %s: %s "
                        "(tool_use %s)",
                        user_id, merged, block.tool_use_id,
                    )
                    req.mint_mute = merged
        except Exception as e:
            logger.error("mint result observation failed: %s", e)

    @staticmethod
    def _track_task_lifecycle(state: _UserStreamState, msg: Any) -> None:
        """DGN-1016: maintain state.active_tasks from stream lifecycle events.

        Must run for EVERY reader-loop message BEFORE the pending branch: a
        background task routinely outlives the turn that spawned it, so its
        terminal event often arrives while state.pending is empty (the
        proactive branch). Tracking failures must never break the reader
        loop -- this is telemetry for the auto-interrupt gate, not delivery
        machinery.
        """
        try:
            if isinstance(msg, TaskStartedMessage):
                state.active_tasks[msg.task_id] = time.monotonic()
                desc = getattr(msg, "description", None)
                if desc:
                    state.task_descriptions[msg.task_id] = desc
                ttype = getattr(msg, "task_type", None)
                if ttype:
                    state.task_types[msg.task_id] = ttype
            elif isinstance(msg, (TaskNotificationMessage, TaskUpdatedMessage)):
                if msg.status in TERMINAL_TASK_STATUSES:
                    state.active_tasks.pop(msg.task_id, None)
                    state.task_descriptions.pop(msg.task_id, None)
                    state.task_types.pop(msg.task_id, None)
        except Exception as e:
            logger.warning("task lifecycle tracking failed: %s", e)

    def live_task_count(self, user_id: int) -> int:
        """DGN-1016: number of tracked live (non-terminal) tasks for the user.

        Pure dict lookup -- safe to call under bot.py's per-user queue lock
        (no I/O, no await). Returns 0 when no stream exists (nothing can be
        alive without a CLI subprocess).
        """
        state = self._streams.get(user_id)
        if not state:
            return 0
        return len(state.active_tasks)

    @staticmethod
    def _collect_killed_tasks(state: _UserStreamState) -> List[str]:
        """Snapshot owner-facing names and clear tracked jobs without yielding."""
        killed = [
            _owner_task_name(
                tid,
                state.task_descriptions.get(tid, ""),
                state.task_types.get(tid, ""),
            )
            for tid in state.active_tasks
        ]
        state.active_tasks = {}
        state.task_descriptions = {}
        state.task_types = {}
        return killed

    def pop_interrupt_killed(self, user_id: int) -> List[str]:
        """DGN-1015: one entry per background task killed by the most
        recent interrupt() call for this user; cleared on read.  Each entry
        is the job's owner-facing name (DGN-1593 r2, _owner_task_name) or ""
        when none is recoverable -- never an internal label or a task id.

        Empty when no stream exists, or interrupt() found no live tasks at
        kill time. Best-effort/read-once: a caller that never polls this
        after interrupt() simply never learns what died -- it does not
        change what actually happened.
        """
        state = self._streams.get(user_id)
        if not state:
            return []
        killed = state.interrupt_killed_descriptions
        state.interrupt_killed_descriptions = []
        return killed

    async def stop_task(self, user_id: int, task_id: str) -> bool:
        """DGN-1016 O3: stop ONE background task, leaving the turn, the
        session, and every other task alive.

        The remote interrupt control request has no scope field (measured,
        DGN-991 rev3): it aborts the whole session tree, which is why
        stopping "just one thing" via interrupt() killed two subagents at
        once on 2026-08-22 11:18. The SDK has carried a per-task control
        request all along (ClaudeSDKClient.stop_task -> control subtype
        "stop_task", client.py:450 / query.py:796 in SDK 0.2.110); this is
        the bridge's first use of it.

        Registry cleanup is NOT done here: after the CLI acts it emits a
        task_notification with status "stopped", which is in
        TERMINAL_TASK_STATUSES (measured on 0.2.110 and 0.2.119), so
        _track_task_lifecycle clears active_tasks/task_descriptions from
        the reader loop. Clearing eagerly would fabricate an under-count
        if the CLI-side stop fails after the send.

        Returns False when there is nothing to act on: no stream, no
        connected streaming query, or an SDK too old to expose stop_task
        (declared floor is >=0.1.72; same degrade-don't-die stance as the
        TASK_LIFECYCLE_AVAILABLE guard). Raises (TimeoutError etc.) when
        the send itself fails, so a caller can distinguish "cannot" from
        "tried and failed".
        """
        state = self._streams.get(user_id)
        if not state:
            return False
        client_stop = getattr(state.client, "stop_task", None)
        if client_stop is None:
            logger.warning(
                "stop_task unavailable: this claude-agent-sdk "
                "build has no ClaudeSDKClient.stop_task -- per-task stop "
                "degrades to unavailable (upgrade the SDK to enable it)"
            )
            return False
        if getattr(state.client, "_query", None) is None:
            return False
        await asyncio.wait_for(
            client_stop(task_id), timeout=INTERRUPT_SEND_TIMEOUT
        )
        logger.info(
            "stop_task sent for user %s (task_id=%s)",
            user_id,
            task_id,
        )
        return True

    async def _reader_loop(self, user_id: int, state: _UserStreamState) -> None:
        try:
            async for msg in state.client.receive_messages():
                # DGN-1016: task lifecycle tracking sees every message,
                # including those routed to the proactive branch below.
                self._track_task_lifecycle(state, msg)
                if not state.pending:
                    # DGN-1689: task_notification is a Claude Code turn
                    # trigger, not owner input.  Mark only an otherwise
                    # unclaimed no-pending turn: dispatch-return and other
                    # injected turns retain their explicit loud/quiet mode.
                    if (
                        isinstance(msg, TaskNotificationMessage)
                        and state.injected_turn_mode is None
                    ):
                        state.task_notification_wakeup = True
                    # No request to answer. This happens when a subagent/background
                    # task completion injects a new turn into the main session. We
                    # must NOT drop the main agent's proactive output; route it to a
                    # proactive push instead. Subagent inner messages stay blocked.
                    await self._handle_proactive_message(user_id, state, msg)
                    continue
                req = state.pending[0]
                # DGN-670 M1: any parent_tool_use_id-bearing message is direct
                # evidence of subagent activity this turn. It must be captured
                # HERE, during reader iteration: subagent inner messages are
                # skipped from content capture below, and the final content
                # never carries the id.
                if getattr(msg, "parent_tool_use_id", None):
                    req.subagent_activity = True
                now = asyncio.get_event_loop().time()
                if req.typing_callback and now - req.last_typing_at >= TYPING_INTERVAL:
                    req.last_typing_at = now
                    try:
                        await req.typing_callback()
                    except Exception:
                        pass

                if isinstance(msg, SystemMessage):
                    _observe_live_model(msg)
                    data = getattr(msg, "data", None)
                    sid = data.get("session_id") if isinstance(data, dict) else None
                    if sid:
                        state.last_session_id = sid
                        # DGN-996: flow the init-time sid to sessions.json now.
                        await self._persist_session_id(user_id, state, sid)
                    continue

                if isinstance(msg, UserMessage):
                    # DGN-1683: the mint verb's tool_result is the ONLY input
                    # to the turn mute -- its JSON, never the agent's prose.
                    if not getattr(msg, "parent_tool_use_id", None):
                        self._observe_mint_results(user_id, req, msg)
                        # DGN-1850: the held step's outcome is known now.
                        if req.hold_terminal:
                            if _is_stop_hook_feedback(msg):
                                self._discard_held(user_id, req)
                            else:
                                await self._release_held(req)
                            req.step_segment_start = len(req.final_segments)
                        # DGN-1703: a main-thread user message closes a model
                        # step. The Stop-block re-prompt additionally marks
                        # the step it closes as a superseded answer.
                        if req.streaming_handler is not None:
                            try:
                                if _is_stop_hook_feedback(msg):
                                    if req.streaming_handler.supersede_step():
                                        req.stop_block_superseded = True
                                req.streaming_handler.begin_step()
                            except Exception as e:
                                logger.error("Step boundary failed: %s", e)
                    continue

                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "session_id", None):
                        state.last_session_id = msg.session_id
                    # Skip subagent inner messages (parent_tool_use_id set).
                    if getattr(msg, "parent_tool_use_id", None):
                        continue
                    req.last_assistant_texts = []
                    # DGN-1857: the CLI's auth failure arrives as a synthetic
                    # answer. Keep it off every owner surface (live stream,
                    # fold, final assembly); finalize shows the notice.
                    auth_fail = _cli_auth_failure(msg)
                    if auth_fail is not None:
                        req.cli_auth_failure = auth_fail
                        logger.warning(
                            "CLI auth failure for user %s held off "
                            "the owner surface: %s", user_id, auth_fail,
                        )
                        continue
                    # DGN-426 C-strict: determine whether this message is terminal.
                    # stop_reason="end_turn" is the measured 100%-clean terminality
                    # signal (164 turns). "tool_use", None, or a ServerToolUseBlock
                    # present -> non-terminal (suppress live display; typing indicator
                    # is the only feedback). DGN-682: interim mode "inline" (or the
                    # deprecated STREAM_INTERIM alias) bypasses the gating and all
                    # TextBlocks display live (pre-DGN-426 behavior); mode "fold"
                    # keeps interim off the live stream but CAPTURES it for the
                    # finalize-time fold blockquote.
                    stop_reason = getattr(msg, "stop_reason", None)
                    has_server_tool = any(
                        isinstance(b, ServerToolUseBlock) for b in msg.content
                    )
                    is_terminal = stop_reason == "end_turn" and not has_server_tool
                    interim_mode = _effective_interim_mode()
                    # DGN-1651: a Stop-hook block lets the model keep the turn
                    # and emit a SECOND terminal message. The first one already
                    # streamed onto the owner's screen, and the DGN-1253 turn
                    # assembly re-delivers it -- deduped against this one via
                    # _subtract_paras -- as part of the final body. The live
                    # copy is therefore a SUPERSEDED answer, not narration:
                    # retract it from the live surface so the regeneration
                    # rewrites the same bubble, instead of gluing onto it
                    # (fold/suppress: the bubble showed the answer twice) or
                    # getting sealed beside it as a standing duplicate (inline,
                    # the seal below). Only fires when this message actually
                    # carries replacement text -- a text-less continuation
                    # leaves the original answer on screen untouched. NO new
                    # dedup is introduced here: the retraction is an exact
                    # span cut, and _subtract_paras stays the one judgment of
                    # what the final body says.
                    # Guard each text block once so the boundary decisions and
                    # ingestion below cannot disagree about what survived.
                    guarded_block_text = {
                        id(b): _register_guard(_scaffold_guard(b.text))
                        for b in msg.content
                        if isinstance(b, TextBlock)
                    }
                    # DGN-1683: third ingestion stage -- the mint turn mute.
                    # After a verb verdict every later text block is held off
                    # the owner surface (live stream, fold, final assembly);
                    # _finalize_result delivers what the verdict allows. Text
                    # riding the SAME message as a verb call is held too: it
                    # streams before the result exists, and it is narration
                    # of the call, not an answer.
                    mint_hold = req.mint_mute is not None or any(
                        isinstance(b, ToolUseBlock)
                        and mint_gate.is_verb_call(b.name, getattr(b, "input", None))
                        for b in msg.content
                    )
                    if mint_hold:
                        if req.mint_mute is not None and any(
                            t.strip() for t in guarded_block_text.values()
                        ):
                            req.mint_spoke = True
                        guarded_block_text = {k: "" for k in guarded_block_text}
                    # DGN-1683 provisional hold: arm on the mint skill load or
                    # a verb call (this message included -- text before the
                    # call in the same message is narration too).
                    req.mint_seq += 1
                    if any(
                        isinstance(b, ToolUseBlock)
                        and mint_gate.arms_hold(b.name, getattr(b, "input", None))
                        for b in msg.content
                    ):
                        req.mint_armed = True
                    # A boundary only carries replacement text when some text
                    # survives the ingestion guards. Besides driving DGN-1651
                    # retraction, this keeps a text-less terminal continuation
                    # from sealing the answer that is already live in drafts.
                    terminal_has_text = bool(
                        is_terminal
                        and any(
                            isinstance(b, TextBlock)
                            and guarded_block_text[id(b)].strip()
                            for b in msg.content
                        )
                    )
                    superseding = bool(
                        terminal_has_text and req.final_segments
                    )
                    # DGN-1703: the live-stream shape. No message is terminal
                    # (stop_reason=None), so the Stop-block re-prompt marked
                    # the superseded span instead; retract it at the first
                    # message that carries replacement text, terminal or not.
                    if req.stop_block_superseded and any(
                        isinstance(b, TextBlock)
                        and guarded_block_text[id(b)].strip()
                        for b in msg.content
                    ):
                        superseding = True
                        req.stop_block_superseded = False
                    retracted = False
                    if req.streaming_handler is not None:
                        try:
                            retracted = req.streaming_handler.begin_message(
                                is_terminal, retract=superseding
                            )
                        except Exception as e:
                            logger.error("Segment boundary failed: %s", e)
                    if retracted:
                        logger.info(
                            "retracted the superseded terminal segment "
                            "for user %s (segment %d, mode=%s) -- the Stop-hook "
                            "regeneration rewrites the live bubble",
                            user_id,
                            len(req.final_segments),
                            interim_mode,
                        )
                    # DGN-947: inline glue teardown. In inline mode the interim
                    # narration streamed into req.streaming_handler's drafts;
                    # the terminal answer is about to stream into the SAME
                    # handler and glue onto that narration. Seal the interim
                    # bubbles NOW so the terminal answer opens a fresh draft and
                    # the finalize consumers see "drafts == final-answer only".
                    # Fold mode is untouched (its narration rides fold bubbles,
                    # not drafts). Never raises into the reader loop.
                    # DGN-1651: skipped when the retraction above emptied the
                    # live surface -- there is no narration left to seal, only
                    # the bubble holding the superseded answer, and sealing it
                    # would make that duplicate permanent. The regeneration
                    # rewrites it in place instead.
                    if (
                        interim_mode == "inline"
                        and is_terminal
                        and terminal_has_text
                        and req.streaming_handler is not None
                        and req.streaming_handler.drafts
                        and not (
                            retracted
                            and not req.streaming_handler.accumulated_text.strip()
                        )
                    ):
                        try:
                            await req.streaming_handler.seal_segment()
                        except Exception as e:
                            logger.error("Interim seal failed: %s", e)
                    # DGN-1850: inside the gated window a message with a tool
                    # call is narration (the turn goes on), so text held from
                    # earlier messages is released first, in order. A message
                    # without one may be the terminal answer: its text is held.
                    hold_message = False
                    if req.hold_terminal:
                        if any(
                            isinstance(b, (ToolUseBlock, ServerToolUseBlock))
                            for b in msg.content
                        ):
                            await self._release_held(req)
                        else:
                            hold_message = True
                            req.held_seq += 1
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            # DGN-285: guard at ingestion so both the final
                            # assembly and the live streaming drafts are clean.
                            # DGN-376 T2 seat 1/3: register guard (DGN-686 v2
                            # drop-only) runs as the next pipeline stage after
                            # the scaffold guard, on the same ingestion path. A
                            # dropped block comes back empty and must not open
                            # an empty streaming draft.
                            block_text = guarded_block_text[id(block)]
                            req.last_assistant_texts.append(block_text)
                            # DGN-1715: an owner message queued into a running
                            # dispatch-return turn makes it pending, so its
                            # text lands here, not in the proactive branch.
                            # Deliver the first result line as its own owner
                            # message regardless of interim mode; that push
                            # opens the result-first latch before the next
                            # ToolUse is evaluated. Already on screen, it is
                            # kept off the live/fold surfaces. DGN-1732: a
                            # terminal one stays in the final assembly only so
                            # finalize can subtract it (subtract_delivered);
                            # streaming it live was the second bubble.
                            pushed_result = bool(
                                state.dispatch_return_turn_id
                                and not state.dispatch_return_result_sent
                                and block_text.strip()
                                and await self._send_dispatch_return_first_text(
                                    user_id, state, block_text
                                )
                            )
                            if pushed_result:
                                continue
                            if hold_message:
                                req.held_blocks.append(
                                    (block_text, block.text, is_terminal,
                                     mint_hold, req.held_seq)
                                )
                                continue
                            await self._route_text_block(
                                req, block_text, block.text, is_terminal, mint_hold
                            )
                        elif isinstance(block, ToolUseBlock):
                            # DGN-086: track main-agent tool uses for flake detection.
                            req.tool_use_count += 1
                            if mint_gate.is_verb_call(
                                block.name, getattr(block, "input", None)
                            ):
                                req.mint_call_ids.add(block.id)
                            # DGN-670 M1: a Task tool call is subagent
                            # activity; run_in_background marks a legitimate
                            # background launch (its status report must never
                            # be treated as a flake).
                            if block.name == "Task":
                                req.subagent_activity = True
                                tin = getattr(block, "input", None)
                                if isinstance(tin, dict) and tin.get(
                                    "run_in_background"
                                ):
                                    req.background_task_launched = True
                    # DGN-1253: capture this TERMINAL message's assembled text
                    # as one turn segment (see _PendingRequest.final_segments).
                    # The join mirrors the legacy finalize expression so a
                    # single-segment turn reads back identically. Whitespace-
                    # only messages (all blocks guard-dropped) contribute no
                    # segment -- the legacy fallback chain in _finalize_result
                    # keeps handling those.
                    if is_terminal:
                        seg = "\n".join(req.last_assistant_texts)
                        if seg.strip():
                            req.final_segments.append(seg)
                    continue

                if isinstance(msg, ResultMessage):
                    state.last_session_id = msg.session_id or state.last_session_id
                    # DGN-1715: the dispatch-return turn may end on this
                    # (pending) path when an owner message was folded into
                    # it. Close its latch here too; left behind, a closed
                    # latch denied every later turn until its 1h expiry.
                    if state.dispatch_return_turn_id:
                        _clear_dispatch_return_context(state.dispatch_return_turn_id)
                        state.dispatch_return_turn_id = None
                        state.dispatch_return_result_sent = False
                    delivered = state.dispatch_return_delivered
                    state.dispatch_return_delivered = None
                    # DGN-581 M1: a soft-interrupted turn's trailing ResultMessage
                    # must be discarded even when a new request is already pending.
                    # Without this gate, the race (interrupt -> drain -> new message
                    # appended before CLI emits the trailing result) routes the stale
                    # result to the new request's future, corrupting or hanging it, and
                    # leaves discard_results=1 as a leak that silences the next genuine
                    # proactive push.  Check discard_results here, before _finalize_result,
                    # so the turn boundary is always request-scoped, not pending-queue-scoped.
                    if state.discard_results > 0:
                        state.discard_results -= 1
                        self._reset_proactive_buffer(state)
                        state.injected_turn_mode = None  # Never leak past a turn boundary.
                        logger.debug(
                            "Discarded stale trailing ResultMessage for user %s"
                            " (pending=%d, discard_results remaining=%d)",
                            user_id,
                            len(state.pending),
                            state.discard_results,
                        )
                        continue
                    # DGN-670: _finalize_result returns True when it blocked a
                    # placeholder flake and re-dispatched the turn. The request
                    # then STAYS at the head of the deque so this loop
                    # attributes all retry output to it; every other exit
                    # returns False and pops as before.
                    req.finalizing = True
                    await self._settle_held_for_result(req)
                    await self._settle_mint_held(user_id, req)
                    retried = await self._finalize_result(
                        user_id, state, req, msg, delivered=delivered
                    )
                    if retried:
                        # DGN-670 retry re-dispatched the turn: it is live
                        # (and interruptible) again.
                        req.finalizing = False
                        continue
                    # DGN-1819 B (defense): pop only the request finalized
                    # here. Anything else at the head (or an empty deque) means
                    # the queue changed under the await -- popping blindly
                    # orphaned the next request's future or crashed the reader
                    # (IndexError on an empty deque).
                    if state.pending and state.pending[0] is req:
                        state.pending.popleft()
                    else:
                        logger.warning(
                            "finalized request for user %s is no "
                            "longer the queue head (pending=%d) -- not popping",
                            user_id,
                            len(state.pending),
                        )
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
            # FixC: reader_loop crash path did not clean up the CLI subprocess.
            # Force-kill here so the orphan does not outlive the session.
            self._force_kill_client_subprocess(state.client, user_id)
            pending_copy = list(state.pending)
            state.pending.clear()
            for req in pending_copy:
                if req.streaming_handler:
                    try:
                        await req.streaming_handler.finalize_all()
                    except Exception:
                        pass
                # DGN-699 D7 (reader crash): a grown fold is confirmed
                # collapsed with the stop marker, never deleted (the user
                # already saw the progress). _fold_finalize never raises.
                await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
                if not req.future.done():
                    req.future.set_result(
                        ChatResponse(
                            content=messages.GENERIC_ERROR.format(error=e),
                            success=False,
                            error=str(e),
                            session_id=state.last_session_id,
                        )
                    )

    async def _route_text_block(
        self, req: _PendingRequest, block_text: str, raw_text: str,
        is_terminal: bool, mint_hold: bool,
    ) -> None:
        """Put one main-thread text block on the interim surfaces: the fold
        capture (DGN-682) and the live draft stream (DGN-426 gating)."""
        if req.mint_armed and req.mint_mute is None:
            # DGN-1683 provisional hold: parked until the turn's verdict is
            # known (_observe_mint_results / _settle_mint_held).
            req.mint_held.append(
                (block_text, raw_text, is_terminal, mint_hold, req.mint_seq)
            )
            return
        await self._route_live(req, block_text, raw_text, is_terminal, mint_hold)

    async def _route_live(
        self, req: _PendingRequest, block_text: str, raw_text: str,
        is_terminal: bool, mint_hold: bool,
    ) -> None:
        if not is_terminal and _interim_off_locale(raw_text):
            # Interim locale gate: an interim step note with no instance-
            # language character (English working narration on a ko
            # instance) stays off every live surface -- stream, fold capture
            # and fold bubble. The final assembly (last_assistant_texts /
            # final_segments) is untouched, so a text-only event that turns
            # out to be the answer is still delivered by _finalize_result.
            logger.info(
                "Interim locale gate dropped a %d-char interim block "
                "(no %s-script character)",
                len(raw_text), getattr(config, "locale", ""),
            )
            return
        interim_mode = _effective_interim_mode()
        if interim_mode == "fold" and not is_terminal and not mint_hold:
            # DGN-682 D4/D5: capture interim narration on the scaffold-guarded
            # block text (see _PendingRequest.interim_texts).
            captured = _scaffold_guard(raw_text)
            if captured.strip():
                req.interim_texts.append(captured)
                # DGN-699 D2: growing-fold dispatch rides the SAME captured
                # text. Never raises into the reader loop.
                await self._fold_dispatch(req, captured)
        live_stream = interim_mode == "inline" or is_terminal
        if req.streaming_handler and live_stream and block_text:
            try:
                await req.streaming_handler.update_if_needed(block_text)
            except Exception as e:
                logger.error("Streaming update failed: %s", e)

    async def _release_held(
        self, req: _PendingRequest, keep_seq: Optional[int] = None
    ) -> None:
        """DGN-1850: held text turned out to be narration -- route it, in
        order, exactly as it would have been routed live. Entries of message
        `keep_seq` stay held."""
        held = req.held_blocks
        req.held_blocks = [h for h in held if h[4] == keep_seq]
        for block_text, raw_text, is_terminal, mint_hold, seq in held:
            if seq != keep_seq:
                await self._route_text_block(
                    req, block_text, raw_text, is_terminal, mint_hold
                )

    def _discard_held(self, user_id: int, req: _PendingRequest) -> None:
        """DGN-1850: a Stop hook blocked the held answer. It never reached the
        owner; drop it (and the turn segments its step captured) so the
        regeneration is the turn's answer."""
        texts = [h[0] for h in req.held_blocks if h[0].strip()]
        req.held_blocks = []
        del req.final_segments[req.step_segment_start:]
        if texts:
            req.held_discarded = texts
        logger.info(
            "Stop block inside the gated window for user %s -- "
            "discarded %d held block(s) (%d chars) that never reached the owner",
            user_id, len(texts), sum(len(t) for t in texts),
        )

    async def _settle_held_for_result(self, req: _PendingRequest) -> None:
        """DGN-1850: the ResultMessage arrived inside the gated window. Held
        text of earlier messages was narration (routed now); the last held
        message is the final answer, which _finalize_result delivers. Live
        drafts (inline narration) are sealed as standing bubbles so the
        final answer goes out as one NEW message, never as an edit of a
        bubble the owner already read."""
        if not req.hold_terminal:
            return
        last = req.held_blocks[-1][4] if req.held_blocks else None
        await self._release_held(req, keep_seq=last)
        req.held_blocks = []
        handler = req.streaming_handler
        if handler is not None and handler.drafts:
            try:
                await handler.seal_segment()
            except Exception as e:
                logger.error("Held-answer seal failed: %s", e)

    async def _settle_mint_held(self, user_id: int, req: _PendingRequest) -> None:
        """DGN-1683 provisional hold: the ResultMessage arrived. With a
        verdict the parked text stays off the owner surface (the verdict
        branch of _finalize_result decides the owner text). Without one the
        turn was an ordinary answer: earlier messages' interim text is routed
        now, in order, as it would have been live; terminal text and the last
        message's text are already in the final assembly, which
        _finalize_result delivers as one new message (the DGN-1850 settle
        shape) -- routing them too would show them twice."""
        held, req.mint_held = req.mint_held, []
        if not req.mint_armed or req.mint_mute is not None or not held:
            return
        released = 0
        for block_text, raw_text, is_terminal, mint_hold, seq in held:
            if seq != req.mint_seq and not is_terminal:
                await self._route_live(
                    req, block_text, raw_text, is_terminal, mint_hold
                )
                released += 1
        logger.info(
            "mint hold released for user %s: no verdict, %d of %d "
            "parked block(s) routed, the rest is the final answer",
            user_id, released, len(held),
        )
        handler = req.streaming_handler
        if released and handler is not None and handler.drafts:
            try:
                await handler.seal_segment()
            except Exception as e:
                logger.error("Mint-hold seal failed: %s", e)

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
    async def _maybe_mark_options(prev_message: str, content: str) -> Tuple[str, bool]:
        """Append the [[OPTIONS]] marker if Haiku judges the trailing numbered
        list a pick-one menu. Runs only when a numbered list is present and the
        marker is absent. Fail-silent: any error leaves content unchanged.

        Returns (content, injected). injected=True ONLY when this method itself
        appended the marker -- DGN-665 provenance: the seat must render buttons
        for classifier-injected markers but must NOT strip the body list (the
        body-strip is gated to agent-AUTHORED markers per owner lock). An
        agent-authored marker already present in content returns injected=False.
        """
        # DGN-1021: "is a marker present?" has exactly ONE implementation --
        # the canonical line-based recognizer (formatting.is_options_marker_line
        # via has_options_marker), which knows every armable form (bare AND
        # labeled). The old `OPTIONS_MARKER in content` substring check is
        # retired everywhere: its only extra coverage was MID-LINE mentions,
        # which never arm buttons and are not an authored marker, so they must
        # not suppress injection over a genuine pick-one run.
        marker_present = has_options_marker(content)
        # DGN-1128: the single-option shape (exactly one trailing "1." line)
        # ALSO reaches the classifier -- the >=2 gate alone forced skills to
        # hand-author the marker for one-item menus, and a forgotten marker
        # left the user with nothing to tap. Scope guard: this widens ONLY
        # this classifier gate. The has_options OR-arms (:1546 proactive,
        # :2176 finalize) stay on has_numbered_list, so a marker-less,
        # non-injected single line never flips force_options downstream.
        # H17 stays intact by construction: an engine AUTO_ADVANCE seat is
        # resolved at the skill layer (the token is consumed and the action
        # executes immediately -- no numbered line is ever emitted), so the
        # bridge only ever classifies seats WITHOUT an engine signal, which
        # are exactly the seats that need buttons.
        if not (
            (has_numbered_list(content) or has_single_trailing_option(content))
            and not marker_present
        ):
            return content, False
        try:
            is_choice = await asyncio.to_thread(
                classify_is_choice, prev_message, content, resolve_claude_cli()
            )
            if is_choice:
                return f"{content}\n\n{OPTIONS_MARKER}", True
        except Exception as e:
            logger.warning("Option classifier failed (no buttons): %s", e)
        return content, False

    async def _send_dispatch_return_first_text(
        self, user_id: int, state: _UserStreamState, text: str
    ) -> bool:
        """Deliver the first dispatch-return text immediately, then open tools.

        Injected turns have no request streaming handler, so fold/suppress
        interim modes cannot make this acknowledgement owner-invisible. The
        latch changes only after the push succeeds; a delivery failure keeps
        the tool gate closed instead of treating generated-but-hidden prose as
        a result.
        """
        turn_id = state.dispatch_return_turn_id
        if state.dispatch_return_result_sent or not turn_id or not text.strip() or state.last_chat_id is None \
                or state.proactive_push is None:
            return False
        sid = state.last_session_id or "default"
        # DGN-1732: the delivery seat (bot._send_smart / machine-line gate)
        # strips NO_PUSH on its own; the same recognizer runs here only for
        # latch bookkeeping. A sentinel-only first text sends nothing and
        # counts as delivered-quiet so the result-first tool gate cannot
        # deadlock waiting for a result that was deliberately withheld.
        body, had_sentinel = strip_no_push_sentinel(text.strip())
        body = body.strip()
        if had_sentinel and not body:
            logger.info(
                "Dispatch-return first text was NO_PUSH only for user %s; "
                "latched delivered-quiet", user_id,
            )
            if state.dispatch_return_turn_id == turn_id:
                state.dispatch_return_result_sent = True
                _write_dispatch_return_context(turn_id, user_id, sid, visible=True)
            return True
        # DGN-1715: announce the in-flight delivery before awaiting it, so a
        # PreToolUse hook racing this push waits for the verdict below.
        _write_dispatch_return_context(turn_id, user_id, sid, delivering=True)
        try:
            # DGN-1732: same canonical recognizer as _flush_proactive, so an
            # owner-authored [[OPTIONS]] menu in the first text keeps its
            # buttons (a hard-coded False made _send_smart drop the keyboard).
            has_options = has_options_marker(body) or has_numbered_list(body)
            await state.proactive_push(state.last_chat_id, body, has_options, False)
            state.last_proactive_sent = body
            if state.dispatch_return_turn_id == turn_id:
                state.dispatch_return_result_sent = True
                state.dispatch_return_delivered = body
                _write_dispatch_return_context(
                    turn_id, user_id, sid, visible=True,
                    options_delivered=has_options_marker(body),
                )
            return True
        except Exception as exc:
            logger.error("Dispatch-return first result push failed for user %s: %s", user_id, exc)
        if state.dispatch_return_turn_id == turn_id:
            _write_dispatch_return_context(turn_id, user_id, sid)
        return False

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
            _observe_live_model(msg)
            data = getattr(msg, "data", None)
            sid = data.get("session_id") if isinstance(data, dict) else None
            if sid:
                state.last_session_id = sid
                # DGN-996: the restart-critical path.  A post-restart injected
                # turn (cron-inject / session-inbox) has no pending request and
                # no ChatResponse, so bot._save_session_id never runs for it --
                # persist the fresh sid here or the owner-session gate stays
                # closed on the old process's sid.
                await self._persist_session_id(user_id, state, sid)
            return

        if isinstance(msg, UserMessage):
            # DGN-1842: a main-thread user message closes a model step; the
            # Stop-block re-prompt additionally marks the step it closes as a
            # superseded answer (the DGN-1703 semantics of the pending path).
            # Text already sent directly (dispatch-return first text) never
            # entered the buffer, so it is neither retracted nor re-sent.
            if not getattr(msg, "parent_tool_use_id", None):
                end = len(state.proactive_texts)
                if _is_stop_hook_feedback(msg) and state.proactive_superseded is None:
                    start = state.proactive_step_start
                    if 0 <= start < end and "".join(
                        state.proactive_texts[start:end]
                    ).strip():
                        state.proactive_superseded = (start, end)
                state.proactive_step_start = end
            return

        if isinstance(msg, AssistantMessage):
            if getattr(msg, "session_id", None):
                state.last_session_id = msg.session_id
            # Subagent inner output must never leak to the user.
            if getattr(msg, "parent_tool_use_id", None):
                return
            auth_fail = _cli_auth_failure(msg)
            if auth_fail is not None:
                # DGN-1857: never buffered; the result surfaces the notice.
                state.proactive_auth_failure = True
                logger.warning(
                    "CLI auth failure in a no-pending turn for user "
                    "%s held off the owner surface: %s", user_id, auth_fail,
                )
                return
            for block in msg.content:
                if isinstance(block, TextBlock):
                    # DGN-376 T2 seat 2/3: proactive push bypasses
                    # _finalize_result, so the register guard must ride here or
                    # briefing/routine pushes escape it entirely (grill M3).
                    block_text = _register_guard(_scaffold_guard(block.text))
                    if state.proactive_superseded is not None and block_text.strip():
                        self._retract_proactive_superseded(user_id, state)
                    state.proactive_texts.append(block_text)
                    # A dispatch-return must become owner-visible before the
                    # next ToolUse can run. Send this first text directly;
                    # terminal flush later carries any follow-up without
                    # depending on interim fold/suppress behavior.
                    if state.dispatch_return_turn_id:
                        if await self._send_dispatch_return_first_text(user_id, state, block_text):
                            # It is already a separate owner message. Keep
                            # later turn text for terminal flush, but never
                            # duplicate this result line into it.
                            state.proactive_texts.pop()
            return

        if isinstance(msg, ResultMessage):
            if state.dispatch_return_turn_id:
                _clear_dispatch_return_context(state.dispatch_return_turn_id)
                state.dispatch_return_turn_id = None
                state.dispatch_return_result_sent = False
            delivered = state.dispatch_return_delivered
            state.dispatch_return_delivered = None
            state.last_session_id = msg.session_id or state.last_session_id
            # DGN-996: injected-turn completion analog of bot._save_session_id
            # (which only real user turns reach).  Dedupe makes this a no-op
            # when the init SystemMessage already persisted the same sid.
            await self._persist_session_id(user_id, state, state.last_session_id)
            if state.discard_results > 0:
                # DGN-581: trailing result of a soft-interrupted (drained) turn.
                # Swallow it -- and any tail text it buffered -- instead of
                # pushing the aborted turn's remains as a proactive message.
                state.discard_results -= 1
                self._reset_proactive_buffer(state)
                state.injected_turn_mode = None  # Never leak past a turn boundary.
                state.task_notification_wakeup = False
                return
            if getattr(msg, "is_error", False) or state.proactive_auth_failure:
                # A no-pending turn ended in an error (e.g. model overloaded /
                # api_error after retries). No assistant text was buffered, so the
                # normal flush would silently drop it. Surface a notice instead.
                await self._flush_proactive_error(user_id, state)
            else:
                await self._flush_proactive(user_id, state, delivered=delivered)

    @staticmethod
    def _reset_proactive_buffer(state: _UserStreamState) -> List[str]:
        """Empty the no-pending text buffer and its DGN-1842 step marks;
        returns what the buffer held."""
        texts = state.proactive_texts
        state.proactive_texts = []
        state.proactive_step_start = 0
        state.proactive_superseded = None
        state.proactive_auth_failure = False
        return texts

    def _retract_proactive_superseded(
        self, user_id: int, state: _UserStreamState
    ) -> None:
        """DGN-1842: replacement text arrived after a Stop-hook block in a
        no-pending turn; drop the blocked answer from the buffer so the flush
        delivers the regeneration only."""
        span = state.proactive_superseded
        state.proactive_superseded = None
        if span is None:
            return
        start, end = span
        dropped = state.proactive_texts[start:end]
        del state.proactive_texts[start:end]
        state.proactive_step_start = len(state.proactive_texts)
        logger.info(
            "retracted the superseded proactive segment for user %s "
            "(%d block(s), %d chars) -- the Stop-hook regeneration replaces it",
            user_id, len(dropped), sum(len(t) for t in dropped),
        )

    async def _flush_proactive_error(self, user_id: int, state: _UserStreamState) -> None:
        """Surface a failed no-pending (background/proactive) turn.

        Mirrors _flush_proactive's delivery guards but sends a fixed failure
        notice instead of buffered text (which is empty on an error result).
        """
        auth_failure = state.proactive_auth_failure
        self._reset_proactive_buffer(state)
        # A turn holding nothing but quiet records did not ask the owner for
        # a report, so its failure notice is the same noise class to suppress.
        # DGN-1642: a task-notification wake-up is not in that class (see
        # _UserStreamState.task_notification_wakeup).
        quiet = state.injected_turn_mode == "quiet"
        state.injected_turn_mode = None
        state.task_notification_wakeup = False
        if quiet:
            logger.warning(
                "Quiet injected turn for user %s ended in error; "
                "failure notice suppressed (quiet default)", user_id,
            )
            return
        if state.last_chat_id is None or state.proactive_push is None:
            logger.warning(
                "Proactive error for user %s dropped: no chat_id/push callback", user_id
            )
            return
        notice = (
            messages.ERROR_AUTH_RELOGIN if auth_failure
            else messages.PROACTIVE_TURN_FAILED
        )
        if notice == state.last_proactive_sent:
            return
        try:
            await state.proactive_push(state.last_chat_id, notice, False, False)
            state.last_proactive_sent = notice
        except Exception as e:
            logger.error("Proactive error push failed for user %s: %s", user_id, e)

    async def _flush_proactive(
        self, user_id: int, state: _UserStreamState, delivered: Optional[str] = None
    ) -> None:
        """Deliver buffered main-agent text that arrived with no pending request.

        Called on a ResultMessage when state.pending is empty. Noise guards:
        empty/whitespace-only text is dropped; an identical consecutive push is
        suppressed; ``delivered`` (the result-first push of this turn,
        DGN-1732) is subtracted so it never goes out twice. Missing chat_id or callback degrades to a logged skip (never
        crashes the reader loop). The normal request-response path never reaches
        here (it has a pending request), so this is regression-safe.
        """
        texts = self._reset_proactive_buffer(state)
        # Consume the injected-turn mode unconditionally: this flush is the
        # turn boundary, so it cannot silence a later proactive turn.
        # DGN-1642: a task-notification wake-up delivers by default; only an
        # explicit quiet injection inverts the default.
        quiet = state.injected_turn_mode == "quiet"
        wakeup_for_log = (
            state.injected_turn_mode is None and state.task_notification_wakeup
        )
        # DGN-1642: captured before the reset below so the NO_PUSH log line
        # can still report which turn source (loud/quiet/real) suppressed
        # itself -- state.injected_turn_mode is None again by the time that
        # branch runs otherwise.
        turn_mode_for_log = state.injected_turn_mode
        state.injected_turn_mode = None
        state.task_notification_wakeup = False
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
        # DGN-1732: recognition is the shared seat recognizer
        # (machine_gate.strip_no_push_sentinel); whole-turn silence on a
        # found sentinel stays this finalize's policy.
        stripped = content.strip()
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if strip_no_push_sentinel(stripped)[1]:
            # DGN-1642: measurement hook only, not a filter -- counts the
            # sole surviving silence path (4.5 of the design). Length and
            # mode only, never content (owner-sensitive).
            logger.info(
                "NO_PUSH suppression: injected_turn_mode=%s, %d chars",
                turn_mode_for_log, len(stripped),
            )
            return
        # DGN-1619: PUSH is a delivery opt-in only for quiet turns, but its
        # trailing sentinel must never reach the owner on either path.
        body_lines = stripped.splitlines()
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        if body_lines and body_lines[-1].strip() == "PUSH":
            body_lines.pop()
        content = "\n".join(body_lines).strip()
        if not content:
            return
        # DGN-1732: the result-first push already delivered part of this turn
        # (maybe with its keyboard). Send only what is new; a final that
        # fully reproduces it sends nothing (the DGN-947 lossless rule).
        if delivered:
            content = subtract_delivered(content, delivered)
            if not content:
                logger.info(
                    "final dropped for user %s: fully reproduces the "
                    "result-first push (lossless)", user_id,
                )
                return
        # DGN-1627: a decision menu must reach the owner even from a quiet
        # injected turn. Recognize it before quiet suppression, but after the
        # PUSH sentinel is stripped so the canonical recognizers see its body.
        has_options = has_options_marker(content) or has_numbered_list(content)
        # DGN-1588/DGN-1591: a QUIET injected turn (outbound-record /
        # operator-alert) inverts the delivery default. The model always
        # emits text when given a turn -- "응답 불필요" in prose produced
        # "기록만 확인했습니다" on the owner's screen (measured 2026-09-19).
        # So silence is enforced HERE: the turn's output is dropped unless
        # the model explicitly opts in by ending with the bare line PUSH or
        # emits a decision menu.
        if quiet:
            if not (has_options or (lines and lines[-1] == "PUSH")):
                logger.info(
                    "Quiet injected turn for user %s suppressed "
                    "(no PUSH sentinel; %d chars dropped)",
                    user_id, len(stripped),
                )
                return
        if wakeup_for_log:
            # DGN-1642: measurement hook only, not a filter -- counts
            # task-notification wake-ups that reach the owner so the noise
            # DGN-1689 saw stays countable. Length only, never content.
            logger.info(
                "Task-notification wake-up for user %s delivered (%d chars)",
                user_id, len(content),
            )
        if content == state.last_proactive_sent:
            return
        if state.last_chat_id is None or state.proactive_push is None:
            logger.warning(
                "Proactive output for user %s dropped: no chat_id/push callback", user_id
            )
            return
        dedup_key = content  # cleaned text, before any marker is appended
        content, classifier_injected = await self._maybe_mark_options("", content)
        # DGN-1021: marker recognition routes through the canonical recognizer
        # ONLY (bare + labeled forms; the substring check missed the labeled
        # form and killed proactive-push buttons silently). The
        # has_numbered_list OR-arm is load-bearing: a numbered run with no
        # marker (source 3 / classifier-injection path) must keep the gate
        # open, so it stays.
        has_options = has_options_marker(content) or has_numbered_list(content)
        try:
            # _send_smart strips the marker and renders [[OPTIONS]] buttons itself;
            # classifier_injected (DGN-665) keeps buttons but skips the body-strip.
            await state.proactive_push(
                state.last_chat_id, content, has_options, classifier_injected
            )
            state.last_proactive_sent = dedup_key
        except Exception as e:
            logger.error("Proactive push failed for user %s: %s", user_id, e)

    async def _fold_dispatch(self, req: _PendingRequest, captured: str) -> None:
        """DGN-699 D2/D3/D8: grow the dedicated fold bubble with new narration.

        Called from the reader loop on every captured fold-mode interim block.
        Contract:
        - DGN-930 live-then-fold creation: the fold bubble is created on the
          1st interim (FOLD_CREATE_MIN_INTERIMS=1, OR the char floor), so
          dev-agent progress is visible live from turn start. Turns that emit
          ZERO interim blocks never open a bubble and fall back to the
          finalize-time compose synthesis (or the graceful degradation path
          when a bubble send fails).
        - D3 throttle: edits pass a TIME-DOMINANT AND gate (new content has
          arrived AND FOLD_UPDATE_INTERVAL elapsed AND any RetryAfter backoff
          deadline passed). A rate limit defers to a later tick -- no sleep
          ever happens on this path, the reader loop never stalls.
        - Never raises: any failure logs and leaves the turn pipeline intact.
        """
        try:
            handler = req.streaming_handler
            if handler is None or req.fold_finalized:
                return
            from bridge.streaming import edit_fold_html, send_fold_html

            now = asyncio.get_event_loop().time()
            req.fold_buf.append(captured)

            if req.fold_msg_id is None:
                total_chars = sum(len(t) for t in req.fold_buf)
                if not (
                    len(req.fold_buf) >= FOLD_CREATE_MIN_INTERIMS
                    or total_chars >= FOLD_CREATE_MIN_CHARS
                ):
                    return
                html = render_fold_live(req.fold_buf)
                if not html:
                    return
                mid = await send_fold_html(handler.bot, req.chat_id, html)
                if mid is not None:
                    req.fold_msg_id = mid
                    req.fold_last_edit_at = now
                return

            if now < req.fold_retry_at:
                return
            if (now - req.fold_last_edit_at) < FOLD_UPDATE_INTERVAL:
                return
            html = render_fold_live(req.fold_buf)
            if not html:
                return
            ok, retry_after = await edit_fold_html(
                handler.bot, req.chat_id, req.fold_msg_id, html
            )
            if ok:
                req.fold_last_edit_at = now
            elif retry_after > 0:
                req.fold_retry_at = now + retry_after
        except Exception as e:
            logger.error(
                "Fold dispatch failed for user %s: %s", req.user_id, e
            )

    async def _fold_finalize(self, req: _PendingRequest, caption: str) -> bool:
        """DGN-699 D1/D7: swap the fold bubble to [caption + collapsed fold].

        The finalize edit re-renders from the FULL fold_buf, so it is also the
        D3 tail flush (any delta a throttled tick skipped lands here).
        Idempotent (fold_finalized latch): every D7 termination path may call
        it safely; only the first call edits. Returns True when a fold bubble
        existed for this turn (used by the timeout partial_preserved probe),
        False when there was nothing to finalize. Never raises.
        """
        try:
            if req.fold_msg_id is None or req.fold_finalized:
                return False
            req.fold_finalized = True
            handler = req.streaming_handler
            if handler is None:
                return True
            from bridge.streaming import finalize_fold_html

            html = render_fold_final(req.fold_buf, caption)
            if not html:
                return True
            ok = await finalize_fold_html(
                handler.bot, req.chat_id, req.fold_msg_id, html
            )
            if not ok:
                logger.error(
                    "Fold finalize edit failed for user %s (msg %s); bubble "
                    "left in last live form",
                    req.user_id,
                    req.fold_msg_id,
                )
            return True
        except Exception as e:
            logger.error("Fold finalize failed for user %s: %s", req.user_id, e)
            return req.fold_msg_id is not None

    async def flush_folds_for_shutdown(
        self, budget: float = FOLD_SHUTDOWN_FLUSH_BUDGET
    ) -> int:
        """DGN-946: finalize every in-flight fold bubble BEFORE HTTP teardown.

        Shutdown race: on a real stop the run loop tears the Telegram HTTP
        client down (application.shutdown()), while the _process_message
        tasks are still awaiting their futures. asyncio.run() only cancels
        those tasks AFTER the run coroutine returns ("Bot stopped"), so their
        CancelledError cleanup hook (_fold_finalize) fires on a dead client
        -- RuntimeError('This HTTPXRequest is not initialized!') -- and the
        bubble freezes in its last live (mid-stream) form. The run loop calls
        this sweep while the client is still alive; the fold_finalized latch
        then turns the late cancellation hooks into network-free no-ops.

        Bounded: the whole sweep shares one wall-clock budget so a Telegram
        RetryAfter can never stall shutdown past the restart window. A bubble
        that misses the budget keeps today's behavior (left in last live
        form) -- never worse. Returns the number of bubbles flushed (finalize
        attempted). Never raises.
        """
        flushed = 0
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + budget
            for state in list(self._streams.values()):
                for req in list(state.pending):
                    if req.fold_msg_id is None or req.fold_finalized:
                        continue
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.warning(
                            "Fold shutdown flush budget (%.1fs) exhausted; "
                            "remaining bubbles keep their last live form",
                            budget,
                        )
                        return flushed
                    try:
                        # A timeout cancels the edit mid-flight, but the
                        # fold_finalized latch is set before the edit inside
                        # _fold_finalize, so no later path re-edits.
                        await asyncio.wait_for(
                            self._fold_finalize(req, FOLD_CAPTION_STOPPED),
                            timeout=remaining,
                        )
                        flushed += 1
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Fold shutdown flush timed out for user %s (msg %s)",
                            req.user_id,
                            req.fold_msg_id,
                        )
        except Exception as e:  # noqa: BLE001 - shutdown sweep must never raise
            logger.error("Fold shutdown flush failed: %s", e)
        if flushed:
            logger.info("Fold shutdown flush finalized %d bubble(s)", flushed)
        return flushed

    @staticmethod
    def _dedup_final_against_interim(content: str, interim_texts: List[str]) -> str:
        """DGN-699 D5 code backstop: drop final-body paragraphs that EXACTLY
        duplicate live-shown interim narration.

        Normalized (whitespace-collapsed) FULL-match only -- containment
        judgments are forbidden (a short final answer that is a substring of
        the narration would be wrongly erased and the DGN-519 empty-drop
        would then silently discard the turn). If filtering would empty the
        result, the filter is skipped entirely (non-empty floor).

        DGN-777 final-sacred: NO LONGER applied to the final content in
        _finalize_result -- the answer keeps every word; overlap is now
        subtracted from the fold via _subtract_paras. Retained as a pure
        helper (referenced by tests / potential external callers).
        """
        try:
            if not content or not interim_texts:
                return content

            def _norm(s: str) -> str:
                return re.sub(r"\s+", " ", s).strip()

            seen = set()
            for t in interim_texts:
                for para in re.split(r"\n\s*\n", t or ""):
                    n = _norm(para)
                    if n:
                        seen.add(n)
            if not seen:
                return content
            kept = [
                p
                for p in re.split(r"\n\s*\n", content)
                if _norm(p) and _norm(p) not in seen
            ]
            deduped = "\n\n".join(kept).strip()
            if not deduped:
                return content  # non-empty floor: skip the filter
            return deduped
        except Exception:
            return content

    @staticmethod
    def _subtract_paras(fold_texts: List[str], final_content: str) -> List[str]:
        """DGN-777 final-sacred: subtract the final-answer overlap from the FOLD.

        Reverse direction of _dedup_final_against_interim: the final answer is
        sacred and keeps every word, so paragraphs of the fold (progress
        record) that also appear in the final body -- normalized FULL-paragraph
        match only, containment forbidden -- are removed from the fold instead.
        Each fold entry may hold multiple paragraphs: split, drop the
        overlapping ones, re-join; an entry is dropped entirely only when
        nothing survives. Non-overlapping paragraphs keep their order.
        Never raises (returns the original list on any error).
        """
        try:
            if not fold_texts or not final_content:
                return fold_texts

            def _norm(s: str) -> str:
                return re.sub(r"\s+", " ", s).strip()

            final_paras = set()
            for para in re.split(r"\n\s*\n", final_content):
                n = _norm(para)
                if n:
                    final_paras.add(n)
            if not final_paras:
                return fold_texts
            trimmed: List[str] = []
            for t in fold_texts:
                kept = [
                    p
                    for p in re.split(r"\n\s*\n", t or "")
                    if _norm(p) and _norm(p) not in final_paras
                ]
                entry = "\n\n".join(kept).strip()
                if entry:
                    trimmed.append(entry)
            return trimmed
        except Exception:
            return fold_texts

    async def _fold_delete(self, req: _PendingRequest) -> bool:
        """DGN-699 (owner 2026-08-02): drop a fully-redundant fold bubble.

        Called when the final answer body entirely reproduces the live fold
        narration -- the collapsed progress record would only duplicate the
        answer, so the bubble is deleted and the clean final message stands
        alone (owner-chosen option 1: overlap removed at the root). Sets the
        fold_finalized latch first so the normal finalize swap becomes a
        no-op. Idempotent; never raises.
        """
        try:
            if req.fold_msg_id is None or req.fold_finalized:
                return False
            req.fold_finalized = True
            handler = req.streaming_handler
            if handler is None:
                return True
            try:
                await handler.bot.delete_message(
                    chat_id=req.chat_id, message_id=req.fold_msg_id
                )
                # DGN-947 FOLD-2: observability. A successful redundant-fold
                # delete was previously silent, indistinguishable from a
                # budget-drop or a teardown-race loss. Log it as the LOSSLESS
                # cause so the three fold-absence reasons are separable in
                # bot.log.
                logger.info(
                    "fold deleted for user %s (msg %s): final fully "
                    "reproduces narration (lossless)",
                    req.user_id, req.fold_msg_id,
                )
            except Exception as e:
                logger.error(
                    "Fold delete failed for user %s (msg %s): %s",
                    req.user_id, req.fold_msg_id, e,
                )
            return True
        except Exception as e:
            logger.error("Fold delete failed for user %s: %s", req.user_id, e)
            return False

    async def _promote_interim_answers(
        self,
        user_id: int,
        state: _UserStreamState,
        req: _PendingRequest,
        promoted: List[str],
        is_streamed: bool,
    ) -> List[str]:
        """DGN-1838: deliver promoted interim answers ahead of the final answer.

        Each block goes out as its own normal message through the proactive
        delivery seat (bot._send_smart: same formatting as a reply), in turn
        order, BEFORE the final answer resolves -- so the chat reads
        [progress fold] -> [answer] -> [final status]. Only an authored
        [[OPTIONS]] marker builds buttons; a numbered answer list stays body
        text. Returns the blocks NOT delivered here: the caller prepends them
        to the final body, so a promoted answer is never lost and never sent
        twice. That fallback is also the path when the final answer already
        streamed into a draft (a separate push would land BELOW it). Never
        raises.
        """
        if not promoted:
            return []
        if (
            is_streamed
            or state.proactive_push is None
            or req.chat_id is None
        ):
            return list(promoted)
        for i, block in enumerate(promoted):
            body = block.strip()
            try:
                await state.proactive_push(
                    req.chat_id, body, has_options_marker(body), False
                )
            except Exception as e:
                logger.error(
                    "promoted interim answer push failed for user %s: "
                    "%s -- %d block(s) ride the final body instead",
                    user_id, e, len(promoted) - i,
                )
                return list(promoted[i:])
        logger.info(
            "promoted %d interim answer block(s) out of the fold for "
            "user %s (%s chars)",
            len(promoted), user_id, ",".join(str(len(b)) for b in promoted),
        )
        return []

    async def _scrub_flake_drafts(self, req: _PendingRequest) -> None:
        """DGN-670 M2: delete already-streamed placeholder draft bubbles.

        Must run BEFORE finalize_all(): StreamingMessageHandler.cancel() is a
        no-op once the handler is finalized, which would leave the placeholder
        permanently on screen. After the cancel the request gets a FRESH
        handler (same bot/chat/user) so a retry turn can stream normally.
        Fail-silent throughout: a Telegram delete failure must never crash
        finalize -- worst case the placeholder bubble survives, which is
        today's behavior.
        """
        handler = req.streaming_handler
        if handler is None:
            return
        try:
            await handler.cancel()
        except Exception as e:
            logger.error("placeholder draft scrub failed: %s", e)
        try:
            from bridge.streaming import StreamingMessageHandler

            req.streaming_handler = StreamingMessageHandler(
                handler.bot, handler.chat_id, handler.user_id
            )
        except Exception:
            req.streaming_handler = None

    async def _dispatch_flake_retry(
        self,
        user_id: int,
        state: _UserStreamState,
        req: _PendingRequest,
        msg: ResultMessage,
    ) -> bool:
        """DGN-670: block the placeholder and re-dispatch the turn ONCE.

        Sends the ORIGINAL req.user_message under the executor-contract retry
        prefix on the SAME session, keeping req at the head of the pending
        deque so the reader loop attributes all retry output to it (zero new
        attribution plumbing). Returns True when the retry query was sent.

        M3: a send failure must NEVER leave the head request with sent=True
        and no in-flight turn (orphaned future = permanent stream wedge,
        worse than a placeholder). On failure the future is resolved to the
        i18n failure notice and False is returned so the reader pops the
        request.
        """
        req.flake_retry_count += 1
        logger.warning(
            "flake recovery: blocking placeholder for user %s, "
            "re-dispatching once (tool_use_count=%d, num_turns=%d)",
            user_id,
            req.tool_use_count,
            msg.num_turns,
        )
        await self._scrub_flake_drafts(req)
        # Reset per-attempt capture state; keep future, user_message, chat_id,
        # callbacks and sent=True (blocks _dispatch_next_query double-send).
        req.last_assistant_texts = []
        # DGN-1253: the retry is a fresh attempt -- the flaked attempt's
        # terminal segments are scrubbed with its drafts.
        req.final_segments = []
        req.step_segment_start = 0
        req.held_blocks = []
        req.held_discarded = []
        req.mint_held = []
        req.tool_use_count = 0
        req.synthetic_response = None
        req.subagent_activity = False
        req.background_task_launched = False
        retry_text = messages.FLAKE_RETRY_PREFIX + req.user_message
        try:
            async with state.send_lock:
                await state.client.query(
                    retry_text,
                    session_id=state.last_session_id or req.sent_session_id,
                )
        except Exception as e:
            logger.error(
                "flake retry send FAILED for user %s: %s", user_id, e
            )
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content=messages.FLAKE_RECOVERY_FAILED,
                        success=False,
                        error="placeholder_flake",
                        session_id=msg.session_id or state.last_session_id,
                    )
                )
            return False
        return True

    async def _finalize_result(
        self, user_id: int, state: _UserStreamState, req: _PendingRequest,
        msg: ResultMessage, delivered: Optional[str] = None,
    ) -> bool:
        """Finalize a turn. Returns True ONLY when a DGN-670 flake retry was
        dispatched (the reader loop then keeps the request at the deque head);
        every other exit returns False."""
        # DGN-285: assemble user-facing text from the reader loop's TextBlock
        # capture (a structural block-type whitelist: thinking/tool blocks never
        # enter it) instead of trusting msg.result, a CLI-composed string that
        # sits outside that whitelist and can carry thinking/internal content
        # under degraded conditions. msg.result stays primary on error results
        # (it carries the error description) and remains the fallback for turns
        # that produced no main-agent TextBlock.
        #
        # DGN-1253 turn assembly: a Stop-hook block lets the model continue
        # the turn and emit MORE terminal messages; assembling from the last
        # message alone (the pre-fix expression) silently dropped the first
        # (real) answer's body, send_file:: attachments and [[OPTIONS]]
        # keyboard. With 2+ captured terminal segments, join them in turn
        # order with a paragraph boundary; paragraphs of a later segment that
        # EXACTLY reproduce earlier text (normalized full-paragraph match via
        # _subtract_paras -- the DGN-777/876 judgment, reused, containment
        # forbidden) are dropped so a post-hook restatement never shows twice.
        # Marker consequences of the multi-segment body are all last-wins /
        # once-only by existing code: resolve_send_paths dedups repeated
        # send_file:: paths, and extract_marker_labels / extract_options give
        # the LAST [[OPTIONS]] declaration the keyboard. Single-segment turns
        # (every non-hook turn) keep the legacy expression byte-identical.
        # DGN-1857: a CLI auth failure on the non-error path finishes the
        # turn exactly like an is_error auth result.
        is_error = msg.is_error or req.cli_auth_failure is not None
        turn_assembled = len(req.final_segments) >= 2
        if turn_assembled:
            assembled = [req.final_segments[0]]
            for seg in req.final_segments[1:]:
                assembled.extend(
                    self._subtract_paras([seg], "\n\n".join(assembled))
                )
            block_text = "\n\n".join(assembled)
        else:
            block_text = "\n".join(req.last_assistant_texts)
            if not block_text.strip() and req.final_segments:
                # DGN-1253 rescue: the turn DID produce a terminal answer,
                # but the LAST message's buffer is empty (a trailing message
                # whose blocks all guard-dropped, e.g. a post-hook
                # continuation caught by the register guard). Recover the
                # captured terminal segment instead of falling through to
                # the untrusted msg.result (DGN-285). Non-empty buffers are
                # never touched -- the plain turn stays byte-identical.
                block_text = req.final_segments[0]
            if not block_text.strip() and req.held_discarded:
                # DGN-1850: the regeneration after a gated Stop block carried
                # no text; the held answer it did not replace stands.
                block_text = "\n".join(req.held_discarded)
        if is_error or not block_text.strip():
            result_text = msg.result or block_text
        else:
            result_text = block_text
        # DGN-285 (leak class 2): signature guard also covers the msg.result
        # fallback path, which bypasses the guarded block capture above.
        # DGN-376 T2 seat 3/3: register guard (DGN-686 v2 drop-only) on the
        # finalized text. On a NON-error result a locale-register drop empties
        # result_text and the DGN-519 empty-final guard drops the turn silently.
        # On an is_error result the guard is NOT applied to the raw English
        # detail: DGN-686 classifies the error below and replaces the body with
        # a LOCKED ko notice, so the English detail never reaches the user
        # (it goes to the stderr log only). Guarding it would be redundant and
        # could swallow a detail we intend to log.
        if not is_error:
            result_text = _register_guard(_scaffold_guard(result_text))
        else:
            result_text = _scaffold_guard(result_text)

        if req.synthetic_response:
            content = self._clean_response(req.synthetic_response)
        else:
            content = self._clean_response(result_text)

        # DGN-1683: mint turn mute. The verb already sent the owner screen
        # (screen) or reported a delivery failure (n13); the owner-bound text
        # is what mint_gate allows and nothing else -- no footer, notice,
        # options or classifier rides it. The future is resolved explicitly
        # with the GATED text (which may be non-empty even when the agent's
        # own content is empty), so this cannot defer to the DGN-519 drop.
        if not is_error and req.mint_mute is not None:
            spoke = req.mint_spoke or bool(content.strip())
            gated = mint_gate.gate_text(req.mint_mute, content if spoke else "")
            logger.info(
                "mint turn mute for user %s: verdict=%s, agent text "
                "%d chars -> owner text %d chars",
                user_id, req.mint_mute, len(content), len(gated),
            )
            if req.streaming_handler:
                try:
                    await req.streaming_handler.finalize_all()
                except Exception as e:
                    logger.error("Streaming finalization failed: %s", e)
            await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content=gated,
                        success=True,
                        session_id=msg.session_id,
                    )
                )
            return False

        # DGN-1732: a dispatch-return turn that absorbed this owner message
        # already pushed its first text (maybe with its keyboard). Subtract it
        # (the DGN-947 lossless rule); when nothing new remains the turn is
        # complete on screen, so resolve with an empty body here (own log
        # line; skips the flake gate and classifier the DGN-519 path reaches).
        if delivered and not is_error and req.synthetic_response is None and content:
            content = subtract_delivered(content, delivered)
            if not content:
                logger.info(
                    "final dropped for user %s: fully reproduces the "
                    "result-first push (lossless)", user_id,
                )
                if req.streaming_handler:
                    try:
                        await req.streaming_handler.finalize_all()
                    except Exception as e:
                        logger.error("Streaming finalization failed: %s", e)
                await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
                if not req.future.done():
                    req.future.set_result(
                        ChatResponse(content="", success=True, session_id=msg.session_id)
                    )
                return False

        # DGN-670: placeholder-flake gate, HOISTED before draft finalization --
        # finalize_all() makes StreamingMessageHandler.cancel() a no-op, so
        # deciding after it would permanently finalize the placeholder bubble.
        # Firing condition (M1): flake regex AND subagent activity observed
        # this turn AND short content AND no legitimate background Task launch.
        # Detection (DGN-086 warning) still logs on every regex match.
        if not is_error and req.synthetic_response is None and content:
            flake = self._is_placeholder_flake(content)
            if flake:
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
                short = len(content) <= _FLAKE_SHORT_CONTENT_MAX
                if short and req.flake_retry_count >= 1:
                    # Loop guard: the single retry flaked again. Explicit
                    # failure notice -- never the placeholder, never silence.
                    logger.error(
                        "flake recovery FAILED after 1 retry for user %s",
                        user_id,
                    )
                    await self._scrub_flake_drafts(req)
                    # DGN-699 D7 (flake-recovery failure): a grown fold is
                    # confirmed collapsed with the stop marker before the
                    # failure notice resolves -- every termination path
                    # confirms, never deletes.
                    await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
                    if not req.future.done():
                        req.future.set_result(
                            ChatResponse(
                                content=messages.FLAKE_RECOVERY_FAILED,
                                success=False,
                                error="placeholder_flake",
                                session_id=msg.session_id,
                            )
                        )
                    return False
                if (
                    short
                    and req.flake_retry_count == 0
                    and req.subagent_activity
                    and not req.background_task_launched
                ):
                    return await self._dispatch_flake_retry(
                        user_id, state, req, msg
                    )
                # Gates not met: legitimate content that merely matches the
                # pattern (background-launch status, long report, main-agent
                # prose with no subagent this turn). Deliver as today.
                logger.info(
                    "flake pattern matched but recovery gates not met "
                    "(subagent_activity=%s, background_task_launched=%s, "
                    "len=%d, retry_count=%d) -- delivering as-is",
                    req.subagent_activity,
                    req.background_task_launched,
                    len(content),
                    req.flake_retry_count,
                )

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

        # DGN-519: empty-final turns are silently dropped for non-error results.
        # Error turns must still reach the PROCESSING_FAILED path below regardless
        # of content value, so the empty-drop guard is placed before the error
        # check only for the non-error branch.
        # DGN-670: an EMPTY retry result must resolve to the failure notice
        # instead -- silently dropping it would leave the original request's
        # future unresolved until timeout.
        if not is_error and not content:
            if req.flake_retry_count >= 1:
                logger.error(
                    "flake retry returned empty content for user %s",
                    user_id,
                )
                # DGN-699 D7: termination via failure notice -- confirm a
                # grown fold with the stop marker.
                await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
                if not req.future.done():
                    req.future.set_result(
                        ChatResponse(
                            content=messages.FLAKE_RECOVERY_FAILED,
                            success=False,
                            error="placeholder_flake",
                            session_id=msg.session_id,
                        )
                    )
                return False
            logger.info("empty-final turn dropped for user %s", user_id)
            # DGN-1838: an empty final after a substantive mid-turn answer
            # must not bury that answer in the fold (or, with no grown
            # bubble, drop it with the narration). Promote it on its own.
            unsent: List[str] = []
            if _effective_interim_mode() == "fold" and not req.fold_finalized:
                grown = req.fold_msg_id is not None
                promoted, kept = split_promoted_interim(
                    req.fold_buf if grown else req.interim_texts, ""
                )
                if grown:
                    req.fold_buf = kept
                    if promoted and not kept:
                        await self._fold_delete(req)
                unsent = await self._promote_interim_answers(
                    user_id, state, req, promoted, is_streamed
                )
            # DGN-699 D7 (empty-final drop): the answer body is silently
            # dropped, but a grown fold is CONFIRMED in place (caption +
            # collapse) -- the caption is then the turn's only signal.
            await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
            if unsent and not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content=INTERIM_FOLD_SEPARATOR.join(unsent),
                        success=True,
                        session_id=msg.session_id,
                        streamed=is_streamed,
                        draft_message_ids=draft_ids,
                        turn_assembled=True,
                    )
                )
                return False
            # DGN-1819 A: "dropped" means nothing rendered, NOT an orphaned
            # future. The reader pops this request next; a pending future
            # then waits out the soft budget, finds nothing to interrupt and
            # surfaces a false time-limit notice. Resolve with a no-render
            # shape: empty body (as DGN-1732) + streamed=True (as interrupt).
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content="",
                        success=True,
                        session_id=msg.session_id,
                        streamed=True,
                    )
                )
            return False

        # DGN-777 final-sacred (supersedes the DGN-699 D5 content-side
        # backstop): the final answer keeps every word, always. On a
        # grown-fold turn the overlap between answer and narration is
        # subtracted from the FOLD (the progress record), never from the
        # final content.
        if (
            not is_error
            and req.synthetic_response is None
            and req.fold_msg_id is not None
            and _effective_interim_mode() == "fold"
        ):
            # DGN-876: unify on _subtract_paras. Subtract the final-answer overlap
            # from the fold; delete ONLY when nothing survives (interim == final,
            # pure echo). A strict superset leaves extra progress paragraphs, kept
            # as the collapsed fold. (Removes the DGN-699 full-dup fast-path that
            # deleted the whole fold even when interim was a superset.)
            trimmed = self._subtract_paras(req.fold_buf, content)
            if not trimmed:
                await self._fold_delete(req)
            else:
                req.fold_buf = trimmed

        if is_error:
            # DGN-699 D7 (is_error): DGN-682 D9 stays -- no fold is ever
            # ATTACHED to an error notice -- but an already-grown fold bubble
            # is confirmed collapsed with the stop marker (the user saw it;
            # deleting it would erase real progress).
            await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
            # DGN-686: classify the failure and surface a LOCKED ko notice.
            # The English detail is logged to stderr only; the user sees only
            # the mapped message. Auth errors offer no retry (re-login needed);
            # transient/other offer a [retry] action rendered by the bot layer.
            #
            # DGN-686 MAJOR-1: the reader loop MUST NOT re-dispatch (re-entrancy
            # risk). Instead it stamps error_kind on the ChatResponse; the bot
            # seat -- which holds the chat_id/callback context -- auto-retries
            # ONCE on a "transient" kind before ever showing the notice. This is
            # the residual path where the SDK completed the turn but flagged
            # is_error (e.g. 529/overloaded_error arriving as a result, not a
            # raised exception). Raised transient exceptions stay covered by
            # process_message -> _reconnect_and_retry.
            detail = content or req.cli_auth_failure or ""
            kind = (
                "auth" if req.cli_auth_failure is not None
                else _classify_error_result(detail)
            )
            logger.warning(
                "is_error result for user %s classified as %s: %s",
                user_id, kind, detail,
            )
            if kind == "auth":
                notice = messages.ERROR_AUTH_RELOGIN
                retry_offer = False
            elif kind == "transient":
                notice = messages.ERROR_TRANSIENT_RETRY
                retry_offer = True
            else:
                notice = messages.ERROR_GENERIC_RETRY
                retry_offer = True
            req.future.set_result(
                ChatResponse(
                    content=notice,
                    success=False,
                    error=detail,
                    session_id=msg.session_id,
                    streamed=is_streamed,
                    draft_message_ids=draft_ids,
                    retry_offer=retry_offer,
                    error_kind=kind,
                )
            )
            return False

        # Haiku auto-classifier: only when no synthetic response, a numbered list
        # is present, and the marker is absent. Fail-silent. classifier_injected
        # records DGN-665 provenance so the seat renders buttons but skips the
        # body-strip for a classifier-injected (non-authored) marker.
        classifier_injected = False
        if req.synthetic_response is None:
            content, classifier_injected = await self._maybe_mark_options(
                req.user_message, content
            )

        # DGN-531: consume the footer sidecar written by status-footer.py.
        # Strip a model-written trailing [라이브]/[결정대기] footer block first
        # (the hook is the sole author; mid-body literals are preserved,
        # DGN-816), then append the canonical footer once.  Empty sidecar
        # (noise-suppression path) -> no footer appended.  Fail-silent:
        # sidecar missing / corrupt -> content unchanged.
        #
        # DGN-877: snapshot the PRE-footer body first -- the sidecar footer is
        # joined with a single "\n", which merges it into the final paragraph.
        # The compose-path fold subtraction below must run against this
        # snapshot: subtracting against the post-footer content loses the last
        # paragraph's boundary, so an interim-narrated final paragraph would
        # miss the full-paragraph match and leak into the fold once.
        prefooter_content = content
        content = _consume_footer_sidecar(content)

        # DGN-1021: marker recognition routes through the canonical recognizer
        # ONLY (bare + labeled; the old substring check missed the labeled
        # form -> has_options=False -> force_options=False -> the entire
        # button block in bot._send_content_artifacts skipped with zero
        # warnings). The has_numbered_list OR-arm is load-bearing (source 3 /
        # classifier-injection path) and stays.
        has_options = (
            req.synthetic_response is not None
            or has_options_marker(content)
            or has_numbered_list(content)
        )

        # DGN-1586 (spec 3.5): owner-notice synthesis, owned by THIS seam --
        # the finalize of an owner-request turn, right after footer
        # consumption and AFTER the has_options judgment (a machine-appended
        # notice body may carry numbered release-note lines; they must never
        # read as a choice menu). Automatic turns never reach here (this
        # method only runs with a pending request, and pending requests are
        # created solely by owner Telegram actions; injected/cron output
        # flows through _flush_proactive), so the owner protocol "never
        # speak the update notice on an automatic turn" holds mechanically.
        #
        # reserve_for_synthesis re-reads the freshest spool state under the
        # spool lock, applies the kind-specific validity predicates, picks
        # at most ONE oldest pending and PRE-PERSISTS its attempt before we
        # append anything; on any failure it returns None and this turn
        # keeps the original response (spec 5). The notice rides the TAIL of
        # the final content; downstream fold/options processing prepends or
        # strips other material but never relocates the tail.
        notice_id = notice_attempt = None
        notice_kind = notice_version = notice_text = None
        if req.synthetic_response is None:
            try:
                reservation = await asyncio.to_thread(
                    notice_spool.reserve_for_synthesis, PROJECT_ROOT
                )
            except Exception as e:
                logger.error("notice reservation failed: %s", e)
                reservation = None
            if reservation:
                for ex_rec in reservation.get("exhausted") or []:
                    # Spec 5 MINOR-1: exhaustion converts to an operator
                    # alarm. Network runs outside the spool lock, as its
                    # own task so a slow push never delays this finalize.
                    try:
                        asyncio.get_running_loop().create_task(
                            notice_spool.send_exhausted_alarm(
                                PROJECT_ROOT, ex_rec
                            )
                        )
                    except Exception:
                        logger.exception(
                            "exhausted-alarm scheduling failed"
                        )
                rec = reservation.get("record")
                if rec:
                    body = (rec.get("body_fold")
                            or rec.get("body_oneline") or "").strip()
                    if body:
                        notice_id = rec.get("id")
                        notice_attempt = reservation.get("attempt")
                        notice_kind = rec.get("kind")
                        notice_version = rec.get("version")
                        notice_text = body
                        content = content + "\n" + body

        # DGN-682 D2/D5/D10: fold-mode interim synthesis, at the finalize
        # TAIL END -- after the final guards (D5), the DGN-519 empty-drop,
        # and _maybe_mark_options / has_options (so quoted narration numbering
        # can never be mistaken for a choice menu, D10). The fold never
        # participates in the body guard / empty-drop / options judgments
        # above. is_error turns returned early above, so a fold never attaches
        # to an error notice (D9).
        #
        # DGN-699 D1/D4/D8: a turn whose fold bubble already GREW live takes
        # the 2-bubble path instead -- the bridge confirms the fold bubble
        # directly (caption + collapse; fold_msg_id never enters
        # draft_message_ids) and the final answer goes out as its own
        # separate message, with NO fold prepended (prepending would
        # duplicate what the user already watched grow). Turns that never
        # passed the D8 creation gate keep the finalize-time compose
        # synthesis below unchanged.
        # DGN-1838: substantive answers written mid-turn are lifted out of the
        # fold first and delivered as their own message ahead of this final
        # answer (see _promote_interim_answers). A block the final restates
        # stays in the fold; the final body itself is never edited.
        promoted_inline = False
        if _effective_interim_mode() == "fold":
            if req.fold_msg_id is not None:
                promoted: List[str] = []
                if not req.fold_finalized:
                    promoted, req.fold_buf = split_promoted_interim(
                        req.fold_buf, prefooter_content
                    )
                if promoted and not req.fold_buf:
                    # The live bubble held nothing but the promoted answer:
                    # collapsing it would show the answer a second time.
                    await self._fold_delete(req)
                await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
                unsent = await self._promote_interim_answers(
                    user_id, state, req, promoted, is_streamed
                )
                if unsent:
                    content = INTERIM_FOLD_SEPARATOR.join(unsent + [content])
                    promoted_inline = True
            else:
                # DGN-876: always subtract final overlap from the interim capture, then
                # compose the fold from whatever survives. Full duplication -> empty fold
                # -> nothing prepended (same clean-final outcome). A superset -> extra
                # progress paragraphs survive and are kept as the collapsed fold.
                # DGN-877: subtraction runs against the PRE-footer body -- the
                # footer join ("\n") merges into the final paragraph, so the
                # post-footer content would miss a narrated last paragraph.
                interim_trimmed = self._subtract_paras(
                    req.interim_texts, prefooter_content
                )
                promoted, interim_trimmed = split_promoted_interim(
                    interim_trimmed, prefooter_content
                )
                unsent = await self._promote_interim_answers(
                    user_id, state, req, promoted, is_streamed
                )
                if unsent:
                    content = INTERIM_FOLD_SEPARATOR.join(unsent + [content])
                    promoted_inline = True
                fold = compose_interim_fold(interim_trimmed, content)
                if fold:
                    content = fold + INTERIM_FOLD_SEPARATOR + content
                elif interim_trimmed:
                    # DGN-947 FOLD-1: compose returned "" but interim_trimmed
                    # survived subtraction -- the ONLY lossy cause. The combined
                    # fold+separator+final overran the single-bubble budget, so
                    # compose_interim_fold discarded the whole fold. In fold
                    # mode interim never streams live, so a plain drop here
                    # would erase this narration from EVERY surface.
                    if req.streaming_handler is not None:
                        # Emit it as its own bubble (grown-path final form:
                        # caption + expandable quote, its own 4096 rolling-
                        # window budget), ordered above the final answer.
                        # send_fold_html NEVER raises: it returns the message id
                        # on success or None on ANY failure (RetryAfter /
                        # BadRequest / network). The RETURN VALUE -- not an
                        # except clause -- is the success signal: a None means
                        # the narration was still lost and MUST log as a
                        # failure, never as a rescue. Empty render (marker-only)
                        # -> mid stays None -> failure log (nothing to preserve
                        # is caught earlier by interim_trimmed being empty).
                        from bridge.streaming import send_fold_html

                        html = render_fold_final(
                            interim_trimmed, FOLD_CAPTION_NORMAL
                        )
                        mid = (
                            await send_fold_html(
                                req.streaming_handler.bot, req.chat_id, html
                            )
                            if html
                            else None
                        )
                        if mid is not None:
                            logger.info(
                                "fold budget-drop rescued for user %s: "
                                "%d interim block(s) emitted as own bubble",
                                user_id,
                                len(req.interim_texts),
                            )
                        else:
                            logger.error(
                                "fold rescue send failed for user %s: "
                                "%d interim block(s), narration lost "
                                "(over budget, send returned no message)",
                                user_id,
                                len(req.interim_texts),
                            )
                    else:
                        # Background / injected turn (no streaming handler): the
                        # rescue path is unavailable, so the budget-drop stays a
                        # loss. Log it as the LOSSY cause -- never let it fall
                        # through to the echo branch and get mislabeled
                        # "lossless".
                        logger.error(
                            "fold budget-drop for user %s: %d interim "
                            "block(s) dropped, no streaming handler to rescue "
                            "(background turn, narration lost)",
                            user_id,
                            len(req.interim_texts),
                        )
                elif req.interim_texts and not promoted:
                    # DGN-947 FOLD-2: interim WAS captured but nothing survived
                    # subtraction (interim_trimmed empty) -- lossLESS. The final
                    # answer fully reproduces the narration paragraph-for-
                    # paragraph, so dropping the fold duplicates nothing. (The
                    # separate lossy budget-drop cause is handled above and can
                    # no longer reach here.)
                    logger.info(
                        "fold dropped for user %s: %d captured "
                        "interim block(s) fully subtracted as final-answer "
                        "overlap (lossless)",
                        user_id,
                        len(req.interim_texts),
                    )

        if not req.future.done():
            req.future.set_result(
                ChatResponse(
                    content=content,
                    success=True,
                    session_id=msg.session_id,
                    has_options=has_options,
                    options_classifier_injected=classifier_injected,
                    streamed=is_streamed,
                    draft_message_ids=draft_ids,
                    # DGN-1838: a promoted answer prepended to a streamed
                    # body differs from the draft; force the real edit.
                    turn_assembled=turn_assembled or promoted_inline,
                    notice_id=notice_id,
                    notice_attempt=notice_attempt,
                    notice_kind=notice_kind,
                    notice_version=notice_version,
                    notice_text=notice_text,
                )
            )
        return False

    async def ensure_owner_stream(
        self,
        user_id: int,
        model: Optional[str],
        chat_id: int,
        proactive_push: Optional[ProactivePushCallback],
    ) -> bool:
        """DGN-399: bootstrap the owner's live stream when none exists yet.

        A stream is normally created only by a real owner message
        (process_message). After a bridge restart at a quiet hour, no owner
        message arrives, so a queued session-inbox turn (e.g. a post-restart
        resume/verify spool) can never be injected -- inject_background_turn
        returns False forever. The session-inbox loop calls this first to
        create the stream, then retries injection.

        This wires the SAME delivery fields process_message sets (last_chat_id
        + proactive_push) so the injected turn's output reaches the owner chat
        instead of being dropped by _flush_proactive. Reuses
        _get_or_create_stream, whose init-lock re-checks self._streams, so a
        real first message racing this bootstrap cannot create a double stream.

        Idempotent: if a live stream already exists it only refreshes the
        delivery route and returns True. Caller gates on owner_id known + claim
        mode off (already enforced by the inbox loop). Returns False and leaves
        no stream on error (caller keeps the spool and retries next tick).
        """
        try:
            state = await self._get_or_create_stream(
                user_id, model, new_session=False
            )
            state.last_chat_id = chat_id
            if proactive_push is not None:
                state.proactive_push = proactive_push
            return True
        except Exception as e:
            logger.error("ensure_owner_stream failed for user %s: %s", user_id, e)
            return False

    async def inject_background_turn(
        self, user_id: int, text: str, quiet: bool = False
    ) -> bool:
        """DGN-217: inject a background/cron notification as a turn into the
        user's LIVE session, with no pending request attached.

        The turn's output flows through the existing no-pending path
        (_handle_proactive_message -> proactive push), so the agent both
        SEES the notification in-session and controls what (if anything)
        reaches the owner -- ending the turn with the bare sentinel NO_PUSH
        suppresses the push.

        quiet=True (DGN-1588/DGN-1591) INVERTS that default for this one
        turn: the output is suppressed unless the model explicitly ends it
        with the bare sentinel line PUSH. Used for self-record injections
        (outbound-record, operator-alert) whose useful case is "the agent
        acts on the record", not "the agent narrates receipt to the owner".

        Returns False (caller retries later) when:
        - no live stream exists for this user yet (bot just started); the
          caller bootstraps it via ensure_owner_stream and retries (DGN-399), or
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
            # DGN-1606: open every injected turn with the harness-owned mark
            # so transcript readers can tell a machine-opened turn from a
            # real owner message (identical plain-text user entries
            # otherwise).  Callers keep passing the RAW spool text -- quiet
            # detection (bot.py QUIET_INJECT_PREFIXES) runs on the original
            # content before this method is reached.
            is_dispatch_return = text.startswith("[dispatch-return]")
            # DGN-1688: the dispatching session intentionally canceled this
            # run.  It still receives the durable return record to recover
            # artifacts, but its response is quiet by default.  Do not create
            # the DGN-1687 owner-visible-result latch for this exempt turn.
            quiet_recovery = is_dispatch_return and "\n- cancel_by: self" in text
            turn_id = ""
            injected_text = INJECTED_TURN_MARK + "\n" + text
            if is_dispatch_return and not quiet_recovery:
                # The model receives an explicit instruction, while the
                # durable latch below is what actually governs tool use.
                injected_text += (
                    "\n\n[bridge:result-first] Before any follow-up work, "
                    "send one concise owner-visible result line about this "
                    "dispatch return."
                )
                turn_id = uuid.uuid4().hex
                state.dispatch_return_turn_id = turn_id
                state.dispatch_return_result_sent = False
                state.dispatch_return_delivered = None
                _write_dispatch_return_context(
                    turn_id, user_id, state.last_session_id or "default"
                )
            elif quiet_recovery:
                injected_text += (
                    "\n\n[bridge:quiet-recovery] This dispatch was canceled by "
                    "this session. Recover any useful artifacts, but end with "
                    "NO_PUSH unless there is a new owner-actionable fact. End "
                    "with PUSH only when that new fact must be delivered."
                )
            try:
                await state.client.query(
                    injected_text, session_id=state.last_session_id or "default",
                )
            except Exception:
                if turn_id:
                    _clear_dispatch_return_context(turn_id)
                    state.dispatch_return_turn_id = None
                    state.dispatch_return_result_sent = False
                raise
            # DGN-1620: a report-requesting injection latches delivery for
            # this turn; a later quiet record cannot silence it.
            if not (quiet or quiet_recovery):
                state.injected_turn_mode = "loud"
            elif state.injected_turn_mode is None:
                state.injected_turn_mode = "quiet"
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
        inbound: Optional[Dict[str, Any]] = None,
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
            inbound=inbound,
            inbound_request_id=uuid.uuid4().hex if inbound else "",
            hold_terminal=blocking_stop_gate_window(),
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
            return await asyncio.wait_for(future, timeout=self._soft_turn_budget())

        except asyncio.CancelledError:
            if streaming_handler:
                try:
                    await streaming_handler.cancel()
                except Exception:
                    pass
            # DGN-699 D7 (CancelledError): drafts are deleted above, but a
            # grown fold is confirmed with the stop marker, not deleted.
            await self._fold_finalize(request, FOLD_CAPTION_STOPPED)
            await self.stop(user_id)
            raise

        except asyncio.TimeoutError:
            # DGN-1499 stop-before-kill: the soft budget (PROCESS_TIMEOUT minus
            # the grace) expired. First send the turn a stop signal so the CLI
            # concludes it cleanly and the subprocess survives for the resume;
            # only when that fails does the legacy preserve+teardown below run
            # (whose force-kill is the designed last resort).
            soft_response = await self._timeout_stop_then_preserve(user_id)
            if soft_response is not None:
                return soft_response
            logger.warning("Query timed out for user %s after %ss", user_id, PROCESS_TIMEOUT)
            killed: List[str] = []
            resume_sid, partial = await self.handle_timeout_preserve(
                user_id, killed_jobs=killed
            )
            return ChatResponse(
                content=messages.TIMEOUT_PAUSED.format(timeout=PROCESS_TIMEOUT),
                success=False,
                error="timeout",
                session_id=resume_sid,
                timed_out=True,
                resume_session_id=resume_sid,
                partial_preserved=partial,
                streamed=partial,
                killed_jobs=killed,
            )

        except Exception as e:
            if state and request in state.pending:
                try:
                    state.pending.remove(request)
                except ValueError:
                    pass
            # DGN-699 D7: the request left the pending deque, so no cleanup
            # hook will ever see it again -- confirm a grown fold here before
            # the retry path builds a NEW request (fresh fold, no overlap).
            await self._fold_finalize(request, FOLD_CAPTION_STOPPED)
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

        finally:
            if request.inbound_request_id:
                _clear_inbound_context(request.inbound_request_id)

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
            hold_terminal=blocking_stop_gate_window(),
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
            # DGN-686: the transient auto-retry itself failed. Show the LOCKED
            # transient notice + [retry] button (auth errors get the re-login
            # notice, no button); the English detail goes to the log only.
            logger.error("Retry failed for user %s: %s", user_id, retry_err, exc_info=True)
            # This is already the SECOND failure (auto-retry ran and failed):
            # do NOT stamp error_kind="transient" here, or the bot seat would
            # auto-retry a third time. Offer the manual [retry] button instead.
            kind = _classify_error_result(str(retry_err))
            if kind == "auth":
                notice, retry_offer = messages.ERROR_AUTH_RELOGIN, False
            elif kind == "transient":
                notice, retry_offer = messages.ERROR_TRANSIENT_RETRY, True
            else:
                notice, retry_offer = messages.ERROR_GENERIC_RETRY, True
            return ChatResponse(
                content=notice,
                success=False,
                error=str(retry_err),
                retry_offer=retry_offer,
            )

    async def stop(self, user_id: int) -> bool:
        return await self._disconnect_user_stream(
            user_id, cancel_message=messages.TASK_TERMINATED
        )

    async def interrupt(self, user_id: int, *, trigger: str = "unspecified") -> bool:
        """DGN-581: ESC-style soft interrupt of the in-flight turn.

        DGN-1016: `trigger` tags the interrupt origin in the INFO log
        ("stop" = explicit /stop command, "auto" = DGN-911 in-flight
        debounce). Before this tag the two origins were indistinguishable
        in the log, which is why the 2026-08-22 09:33 subagent-death
        incident could not be attributed. DGN-1499 adds "timeout" (the
        stop-before-kill path, _timeout_stop_then_preserve); it is the one
        trigger that changes behavior, and only in one detail: a grown fold
        is confirmed with the timeout caption instead of the stop caption,
        matching what handle_timeout_preserve stamps on the hard path.

        Sends the SDK control-protocol interrupt (ClaudeSDKClient.interrupt()
        -> Query.interrupt() -> control request {"subtype": "interrupt"}) so
        the current turn stops generating, while the stream state, the client
        connection, and the CLI subprocess all stay alive -- session context
        is preserved. Contrast stop(), which pops the stream state and tears
        the client (and, on timeout, the subprocess) down.

        DGN-991 caveat (measured): "the CLI subprocess stays alive" does NOT
        mean the turn's work survives. The CLI handles the interrupt control
        request by aborting the in-flight turn's AbortController tree, and
        in-session Task-tool subagents execute inside that tree -- so a soft
        interrupt kills them (2026-08-21 incident: soft interrupt only in the
        log, no hard kill, subagents dead). Work detached via
        dispatch-detached.sh runs in its own setsid session and is NOT
        killed by this path. Since the two coexist, the /stop reply
        (messages.STOP_INTERRUPTED) makes no blanket background-work claim
        either way (2026-09-03 owner decision, DGN-991); when tracked
        background work is confirmed dead by an AUTOMATIC interrupt,
        bg_task_killed_notice counts it (DGN-1593: not after /stop, and
        never by its internal description). Root fix (background
        work outside the session process) is the DGN-991 v2.0 relocation,
        out of scope here.

        Queue policy = DRAIN: every pending request for this user is resolved,
        not just the in-flight head, so no queued input auto-fires after the
        stop. Drafted bubbles of the interrupted turn are finalized in place
        (the same preserve path handle_timeout_preserve uses), and the drained
        futures resolve silently: empty content + streamed=True renders
        nothing at the bot reply layer, so the /stop acknowledgement is the
        only message the user sees.

        Returns False when there is nothing to interrupt: no live stream, no
        dispatched in-flight turn, a head turn that already ended and is
        being finalized (DGN-1819), or a client without a connected streaming
        query (interrupt() is only valid in streaming mode). The caller falls
        back to the legacy hard-stop semantics. Raises (e.g. TimeoutError,
        CLIConnectionError) when the interrupt send fails on a stuck turn so
        the caller can fall back to the hard teardown -- /stop must never
        degrade to a silent no-op.

        Concurrency: deliberately takes NO send_lock. The interrupt is a
        control-channel write the SDK serializes internally; waiting on
        send_lock here could deadlock behind the very dispatch this call is
        trying to interrupt.
        """
        state = self._streams.get(user_id)
        if not state:
            return False
        head = state.pending[0] if state.pending else None
        if head is None or not head.sent:
            return False
        # DGN-1819 B: the head turn already ended (its result is being
        # finalized). Nothing to stop; the caller's fallback applies (auto:
        # DGN-616 coalescing sends the new message after this turn settles).
        if head.finalizing:
            logger.info(
                "Interrupt (%s) skipped for user %s: head turn is finalizing",
                trigger,
                user_id,
            )
            return False
        # interrupt() is only valid on a connected streaming client; the SDK
        # raises CLIConnectionError when _query is absent. Treat that as
        # nothing-to-interrupt (guard, not failure).
        if getattr(state.client, "_query", None) is None:
            return False
        await asyncio.wait_for(
            state.client.interrupt(), timeout=INTERRUPT_SEND_TIMEOUT
        )
        # DGN-1015: the control request above just aborted the CLI's
        # session-wide abort tree, which collaterally kills every tracked
        # background subagent -- and NO task_notification/task_updated
        # terminal event will ever arrive for them (measured: the
        # 2026-08-22 09:33 incident transcript shows the killed subagent's
        # own jsonl ending on "[Request interrupted by user]" with zero
        # task-notification anywhere in the main session). Without this,
        # active_tasks/task_descriptions would leak these ids as phantom
        # "still live" entries forever. Snapshot + clear here, the one place
        # that synchronously knows the kill just happened, and stash
        # descriptions for the caller (pop_interrupt_killed) to notify with.
        # DGN-1593 r2: resolved to owner-facing names now, while the type
        # map still exists ("" = counted without a name).
        # DGN-1593 r3: extend, never overwrite -- two overlapping interrupts
        # (timeout stop + auto-interrupt) each snapshot active_tasks after
        # their own await, and the later one sees it already empty. An
        # overwrite let that empty snapshot erase the earlier kill list;
        # extend + read-once pop means whichever caller pops first reports
        # every killed job exactly once.
        state.interrupt_killed_descriptions += self._collect_killed_tasks(state)
        fold_caption = (
            FOLD_CAPTION_TIMEOUT if trigger == "timeout" else FOLD_CAPTION_STOPPED
        )
        drained: List[_PendingRequest] = []
        while state.pending:
            drained.append(state.pending.popleft())
        for req in drained:
            if req.sent:
                # The CLI still emits a trailing ResultMessage for a
                # dispatched turn; its request is drained now, so mark the
                # result for a one-shot swallow in the reader loop.
                state.discard_results += 1
            # DGN-1850: an interrupted turn reaches no Stop gate; text held
            # for one goes to the surfaces it would have streamed to.
            try:
                await self._release_held(req)
            except Exception as e:
                logger.error("Held-text release failed for user %s: %s", user_id, e)
            if req.streaming_handler:
                try:
                    if trigger == "auto":
                        # Interrupt-fold ticket: an AUTOMATIC interrupt (a
                        # new message cut the turn short, not an explicit
                        # /stop) collapses the streamed draft into a
                        # "stopped" fold quote instead of leaving it frozen
                        # as a plain, unmarked partial answer -- mirrors
                        # what _fold_finalize already does for the
                        # dev-agent growing fold below. /stop keeps
                        # finalize_all() exactly as before (explicit user
                        # action; its UX is not this ticket's call to make).
                        await req.streaming_handler.cancel(
                            fold_caption=INTERRUPT_FOLD_CAPTION
                        )
                    else:
                        await req.streaming_handler.finalize_all()
                except Exception as e:
                    logger.error(
                        "Interrupt finalize failed for user %s: %s", user_id, e
                    )
            # DGN-699 D7 (soft interrupt): a grown fold is confirmed
            # collapsed with the stop marker (or, for the DGN-1499 timeout
            # trigger, the timeout marker) -- the progress the user watched
            # is preserved, never deleted.
            await self._fold_finalize(req, fold_caption)
            if not req.future.done():
                req.future.set_result(
                    ChatResponse(
                        content="",
                        success=False,
                        error="interrupted",
                        session_id=state.last_session_id,
                        streamed=True,
                    )
                )
        logger.info(
            "Soft-interrupted turn for user %s (trigger=%s, drained %d request(s))",
            user_id,
            trigger,
            len(drained),
        )
        return True

    @staticmethod
    def _soft_turn_budget() -> float:
        """DGN-1499: dispatch budget before the stop signal fires (T-N).

        PROCESS_TIMEOUT stays the total turn budget (T); the grace is carved
        out of it, not added on top. Disabled grace (0) or one that does not
        fit under PROCESS_TIMEOUT degrades to the legacy single deadline.
        """
        if 0 < TIMEOUT_STOP_GRACE < PROCESS_TIMEOUT:
            return PROCESS_TIMEOUT - TIMEOUT_STOP_GRACE
        return PROCESS_TIMEOUT

    async def _timeout_stop_then_preserve(
        self, user_id: int
    ) -> Optional[ChatResponse]:
        """DGN-1499: stop-before-kill for the turn timeout (the T-N hard rule).

        The order used to be timeout -> disconnect (3s budget, which loses to
        the SDK's own ~10s graceful sequence whenever the CLI is busy mid-turn)
        -> force-kill, so the designed last resort fired as the FIRST resort on
        every long turn (measured: bot.log 2026-09-15 18:31:45 / 18:40:55,
        "Error disconnecting" then "Force-killed orphan CLI subprocess").

        Now the turn gets a stop signal BEFORE expiry: the same SDK control
        interrupt /stop uses (interrupt(), trigger="timeout"). On success the
        CLI concludes the turn itself -- streamed drafts are finalized in
        place, a grown fold is confirmed with the timeout caption, and the
        stream state AND the CLI subprocess stay alive. The returned response
        carries timed_out=True, so bot._auto_resume_loop resumes the session
        on the SAME live client (no respawn). This response's own `content`
        (messages.TIMEOUT_PAUSED) is a plain statement of fact, not an
        instruction -- every caller must route it through
        bot._auto_resume_loop first, and its STILL_WORKING / tap-to-continue
        notices are the ONLY layer allowed to tell the owner to act (DGN-1523:
        a caller that skips that gate and forwards this content verbatim
        leaks a fact-only string with no button behind it). Nothing here
        restarts the bridge process: self_restart.sh's owner-notify contract
        is untouched by design.

        Returns None whenever the soft path cannot run -- grace disabled, no
        live stream, nothing dispatched, or the interrupt send failed / was
        not acked within INTERRUPT_SEND_TIMEOUT (the stuck-CLI case). The
        caller then falls back to the legacy preserve+teardown, where
        force-kill remains the last resort.
        """
        if not (0 < TIMEOUT_STOP_GRACE < PROCESS_TIMEOUT):
            return None
        state = self._streams.get(user_id)
        if state is None:
            return None
        resume_sid = state.last_session_id
        if not resume_sid and state.pending:
            head = state.pending[0]
            if head.requested_session_id not in (None, "default"):
                resume_sid = head.requested_session_id
            elif head.sent_session_id not in (None, "default"):
                resume_sid = head.sent_session_id
        # DGN-1523: neither the live state nor the pending head is guaranteed
        # to carry a sid at this exact instant (e.g. a stream still waiting on
        # its first SystemMessage, or a request built before
        # _runtime_active_sessions admitted it). The disk copy DGN-996
        # pre-persists on every session_id sighting is the durable fallback --
        # read it here, at the source, instead of leaving auto-resume to
        # depend on bot.py re-deriving the same value a second time.
        if not resume_sid:
            try:
                persisted = await session_manager.get_session(user_id)
                resume_sid = persisted.get("session_id")
            except Exception as e:
                logger.warning(
                    "resume sid disk fallback failed for user %s: %s", user_id, e
                )
        if not resume_sid:
            logger.warning(
                "Soft stop for user %s has no resume sid anywhere (live state, "
                "pending head, and session store all empty) -- auto-resume "
                "cannot fire this turn",
                user_id,
            )
        # Capture the partial-output flag BEFORE interrupt() finalizes the
        # drafts (finalize empties the draft list this predicate reads).
        partial = self.user_has_streamed_output(user_id)
        logger.warning(
            "Query hit soft stop for user %s after %ss -- sending stop signal "
            "(%ss grace before hard teardown)",
            user_id,
            self._soft_turn_budget(),
            TIMEOUT_STOP_GRACE,
        )
        try:
            stopped = await self.interrupt(user_id, trigger="timeout")
        except Exception as e:
            logger.warning(
                "Stop signal failed for user %s: %s -- falling back to hard teardown",
                user_id,
                e,
            )
            return None
        if not stopped:
            return None
        logger.info(
            "Stop signal landed for user %s -- session kept alive for resume",
            user_id,
        )
        # DGN-1593 r3: the stop signal kills background jobs exactly like an
        # auto-interrupt does. Drain the kill list now (read-once) and hand
        # it to the bot seat on the response, which owns the owner notice.
        killed = self.pop_interrupt_killed(user_id)
        return ChatResponse(
            content=messages.TIMEOUT_PAUSED.format(timeout=PROCESS_TIMEOUT),
            success=False,
            error="timeout",
            session_id=resume_sid,
            timed_out=True,
            resume_session_id=resume_sid,
            partial_preserved=partial,
            streamed=partial,
            killed_jobs=killed,
        )

    async def handle_timeout_preserve(
        self, user_id: int, *, killed_jobs: Optional[List[str]] = None
    ) -> Tuple[Optional[str], bool]:
        """Preserve drafts/sid and collect timeout kills for the bot notice."""
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
                # DGN-699 D4/D7 (timeout): a turn whose only streamed surface
                # is the grown fold still counts as partial output preserved.
                # The fold is confirmed with the timeout marker.
                if await self._fold_finalize(head, FOLD_CAPTION_TIMEOUT):
                    partial_preserved = True
        # DGN-612: silent teardown. _auto_resume_loop (bot.py) is the SOLE
        # source of the user-facing STILL_WORKING notice for a timed-out turn
        # -- it sends it once, keyed off the returned response's timed_out
        # flag. Before this fix, this call passed
        # cancel_message=messages.STILL_WORKING, which resolved any OTHER
        # request still queued behind the timed-out one (state.pending) with
        # that same string as its own ChatResponse.content -- and that
        # response reaches the normal (non-auto-resume) reply path for a
        # DIFFERENT user message, surfacing a second, unrelated STILL_WORKING
        # bubble. This teardown was ALREADY draining+terminating any such
        # queued future (pre-existing behavior, unchanged by this fix) --
        # silent=True only changes what that termination says: content=""
        # instead of a user-facing string, so nothing is emitted for it.
        # Capture at teardown, after draft finalization may have yielded to
        # another interrupt or task completion. No await separates this drain
        # from disconnect popping the stream: overlapping soft/hard paths
        # consume each job once, including unreported soft-interrupt kills.
        state = self._streams.get(user_id)
        if state is not None and killed_jobs is not None:
            killed_jobs.extend(self.pop_interrupt_killed(user_id))
            killed_jobs.extend(self._collect_killed_tasks(state))
        await self._disconnect_user_stream(user_id, silent=True)
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
            # DGN-699 D7 (/stop): drafts above are DELETED, but a grown fold
            # is confirmed collapsed with the stop marker -- the progress the
            # user watched is preserved, never deleted.
            if await self._fold_finalize(req, FOLD_CAPTION_STOPPED):
                cancelled = True
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
                # DGN-699 D4: a grown fold bubble IS streamed partial output
                # (a fold-only turn must not be misread as "nothing shown").
                if getattr(req, "fold_msg_id", None) is not None:
                    return True
        except Exception as e:
            logger.error("user_has_streamed_output check failed for %s: %s", user_id, e)
        return False


sdk_bridge = SdkBridge()
