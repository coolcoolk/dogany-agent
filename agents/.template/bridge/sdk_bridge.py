"""Per-user long-lived Claude SDK streaming bridge.

Each user gets a persistent ClaudeSDKClient. Messages are serialized: only one
query is in flight at a time (the reader_loop attributes all streamed text to
the head request), while later messages queue immediately for a fast-typing UX.
Handles streaming drafts, AskUserQuestion degradation, the timeout/preserve +
resume capture path, and a single reconnect-retry on transient SDK errors.
"""

import asyncio
import json
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
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from bridge import messages
from bridge.config import (
    BRIDGE_REGISTER_GUARD,
    BRIDGE_SCAFFOLD_GUARD,
    CLAUDE_CLI_PATH,
    CLAUDE_MAX_BUFFER_SIZE,
    FOLD_UPDATE_INTERVAL,
    INTERIM_MODE,
    OUTPUT_LANG_GUARD,
    PROCESS_TIMEOUT,
    STREAM_INTERIM,
    config,
)
from bridge.formatting import (
    FOLD_CAPTION_NORMAL,
    FOLD_CAPTION_STOPPED,
    FOLD_CAPTION_TIMEOUT,
    INTERIM_FOLD_SEPARATOR,
    compose_interim_fold,
    render_fold_final,
    render_fold_live,
)
from bridge.options import OPTIONS_MARKER, classify_is_choice, has_numbered_list
from bridge.permissions import extract_outside_paths, extract_protected_paths

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


def _classify_error_result(detail: str) -> str:
    """Classify a failure detail into 'auth' | 'transient' | 'other'.

    Auth takes precedence over transient (a 401 must never be retried). Match
    is substring/case-insensitive against the raw failure text.
    """
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
    r"|\b(?:routines|memory-engine|bridge|worklog)/[\w./-]+\.(?:py|sh|md)\b"
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


def _compose_system_prompt() -> str:
    """Compose the bridge system prompt, appending the DGN-429 output-language
    rule (hybrid leg 1: prompt first, charset detector as backstop).

    DGN-699 FATAL-1: the fold register fragment is gated on the CURRENT
    effective interim mode so that suppress/inline/off turns never receive a
    premise that is false for them ("the user already saw your progress live").

    Gated by OUTPUT_LANG_GUARD: when off (e.g. a dev agent legitimately working
    in the English technical register) the language fragment is skipped.
    """
    base = messages.SYSTEM_PROMPT
    if _effective_interim_mode() == "fold":
        base = base + messages.SYSTEM_PROMPT_FOLD_FRAGMENT
    if not OUTPUT_LANG_GUARD:
        return base
    locale = getattr(config, "locale", "") or "en"
    language = _LOCALE_LANGUAGE_NAMES.get(locale, "English")
    return base + messages.OUTPUT_LANG_PROMPT_TEMPLATE.format(language=language)


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
    # DGN-670: single-retry loop guard. 0 = no flake retry dispatched yet;
    # 1 = the one allowed retry is (or was) in flight. Monotonic per request.
    flake_retry_count: int = 0
    # DGN-670 M1: subagent activity observed THIS turn. Set in _reader_loop
    # when a Task ToolUseBlock appears in a main-agent message or when any
    # parent_tool_use_id-bearing message streams by (subagent inner messages
    # are skipped from capture, so the evidence must be recorded during
    # reader iteration -- the final content never carries it). Recovery only
    # fires when this is True: a flake-looking reply with NO subagent
    # activity (Warg-style main-agent plain text, Bash-dispatched background
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
    # DGN-581: count of trailing ResultMessages to swallow. A soft interrupt
    # drains the pending deque, but the CLI still emits a ResultMessage (and
    # possibly tail AssistantMessages) for each already-dispatched turn; with
    # no pending request left the reader loop would misroute that tail to the
    # proactive-push path. Each swallow decrements the counter.
    discard_results: int = 0


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
            # DGN-429 hybrid leg 1: base fragment + output-language rule.
            "system_prompt": _compose_system_prompt(),
            "can_use_tool": can_use_tool,
            "permission_mode": "default",
            # DGN-460: default SDK transport buffer (1MB) is too small for
            # tool results carrying inline base64 media; raise it (env-tunable).
            "max_buffer_size": CLAUDE_MAX_BUFFER_SIZE,
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
        if CLAUDE_CLI_PATH:
            # SDK >=0.2 exposes a supported cli_path option (no monkeypatch needed).
            opts["cli_path"] = CLAUDE_CLI_PATH

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
            # DGN-699 D7 (_disconnect_user_stream cleanup hook): this teardown
            # path never reaches _finalize_result, so an orphaned grown fold
            # is confirmed here (collapse + stop marker). Idempotent: paths
            # that already finalized (timeout/stop/finalize) are no-ops.
            await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
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
                    live_stream = interim_mode == "inline" or is_terminal
                    # DGN-947: inline glue teardown. In inline mode the interim
                    # narration streamed into req.streaming_handler's drafts;
                    # the terminal answer is about to stream into the SAME
                    # handler and glue onto that narration. Seal the interim
                    # bubbles NOW so the terminal answer opens a fresh draft and
                    # the finalize consumers see "drafts == final-answer only".
                    # Fold mode is untouched (its narration rides fold bubbles,
                    # not drafts). Never raises into the reader loop.
                    if (
                        interim_mode == "inline"
                        and is_terminal
                        and req.streaming_handler is not None
                        and req.streaming_handler.drafts
                    ):
                        try:
                            await req.streaming_handler.seal_segment()
                        except Exception as e:
                            logger.error("Interim seal failed: %s", e)
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            # DGN-285: guard at ingestion so both the final
                            # assembly and the live streaming drafts are clean.
                            # DGN-376 T2 seat 1/3: register guard (DGN-686 v2
                            # drop-only) runs as the next pipeline stage after
                            # the scaffold guard, on the same ingestion path. A
                            # dropped block comes back empty and must not open
                            # an empty streaming draft.
                            block_text = _register_guard(_scaffold_guard(block.text))
                            req.last_assistant_texts.append(block_text)
                            if interim_mode == "fold" and not is_terminal:
                                # DGN-682 D4/D5: capture interim narration on the
                                # scaffold-guarded block text (see
                                # _PendingRequest.interim_texts).
                                captured = _scaffold_guard(block.text)
                                if captured.strip():
                                    req.interim_texts.append(captured)
                                    # DGN-699 D2: growing-fold dispatch rides the
                                    # SAME captured text. Never raises into the
                                    # reader loop.
                                    await self._fold_dispatch(req, captured)
                            if req.streaming_handler and live_stream and block_text:
                                try:
                                    await req.streaming_handler.update_if_needed(block_text)
                                except Exception as e:
                                    logger.error("Streaming update failed: %s", e)
                        elif isinstance(block, ToolUseBlock):
                            # DGN-086: track main-agent tool uses for flake detection.
                            req.tool_use_count += 1
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
                    continue

                if isinstance(msg, ResultMessage):
                    state.last_session_id = msg.session_id or state.last_session_id
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
                        state.proactive_texts = []
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
                    retried = await self._finalize_result(user_id, state, req, msg)
                    if retried:
                        continue
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
        if not (has_numbered_list(content) and OPTIONS_MARKER not in content):
            return content, False
        try:
            is_choice = await asyncio.to_thread(
                classify_is_choice, prev_message, content, CLAUDE_CLI_PATH
            )
            if is_choice:
                return f"{content}\n\n{OPTIONS_MARKER}", True
        except Exception as e:
            logger.warning("Option classifier failed (no buttons): %s", e)
        return content, False

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
                    # DGN-376 T2 seat 2/3: proactive push bypasses
                    # _finalize_result, so the register guard must ride here or
                    # briefing/routine pushes escape it entirely (grill M3).
                    state.proactive_texts.append(
                        _register_guard(_scaffold_guard(block.text))
                    )
            return

        if isinstance(msg, ResultMessage):
            state.last_session_id = msg.session_id or state.last_session_id
            if state.discard_results > 0:
                # DGN-581: trailing result of a soft-interrupted (drained) turn.
                # Swallow it -- and any tail text it buffered -- instead of
                # pushing the aborted turn's remains as a proactive message.
                state.discard_results -= 1
                state.proactive_texts = []
                return
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
            await state.proactive_push(state.last_chat_id, notice, False, False)
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
        content, classifier_injected = await self._maybe_mark_options("", content)
        has_options = OPTIONS_MARKER in content or has_numbered_list(content)
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
                    "DGN-947 fold deleted for user %s (msg %s): final fully "
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
            logger.error("DGN-670: placeholder draft scrub failed: %s", e)
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
            "DGN-670 flake recovery: blocking placeholder for user %s, "
            "re-dispatching once (tool_use_count=%d, num_turns=%d)",
            user_id,
            req.tool_use_count,
            msg.num_turns,
        )
        await self._scrub_flake_drafts(req)
        # Reset per-attempt capture state; keep future, user_message, chat_id,
        # callbacks and sent=True (blocks _dispatch_next_query double-send).
        req.last_assistant_texts = []
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
                "DGN-670 flake retry send FAILED for user %s: %s", user_id, e
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
        self, user_id: int, state: _UserStreamState, req: _PendingRequest, msg: ResultMessage
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
        block_text = "\n".join(req.last_assistant_texts)
        if msg.is_error or not block_text.strip():
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
        if not msg.is_error:
            result_text = _register_guard(_scaffold_guard(result_text))
        else:
            result_text = _scaffold_guard(result_text)

        if req.synthetic_response:
            content = self._clean_response(req.synthetic_response)
        else:
            content = self._clean_response(result_text)

        # DGN-670: placeholder-flake gate, HOISTED before draft finalization --
        # finalize_all() makes StreamingMessageHandler.cancel() a no-op, so
        # deciding after it would permanently finalize the placeholder bubble.
        # Firing condition (M1): flake regex AND subagent activity observed
        # this turn AND short content AND no legitimate background Task launch.
        # Detection (DGN-086 warning) still logs on every regex match.
        if not msg.is_error and req.synthetic_response is None and content:
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
                        "DGN-670 flake recovery FAILED after 1 retry for user %s",
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
                    "DGN-670: flake pattern matched but recovery gates not met "
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
        if not msg.is_error and not content:
            if req.flake_retry_count >= 1:
                logger.error(
                    "DGN-670 flake retry returned empty content for user %s",
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
            # DGN-699 D7 (empty-final drop): the answer body is silently
            # dropped, but a grown fold is CONFIRMED in place (caption +
            # collapse) -- the caption is then the turn's only signal.
            await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
            return False

        # DGN-777 final-sacred (supersedes the DGN-699 D5 content-side
        # backstop): the final answer keeps every word, always. On a
        # grown-fold turn the overlap between answer and narration is
        # subtracted from the FOLD (the progress record), never from the
        # final content.
        if (
            not msg.is_error
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

        if msg.is_error:
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
            kind = _classify_error_result(content)
            logger.warning(
                "is_error result for user %s classified as %s: %s",
                user_id, kind, content,
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
                    error=content,
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

        has_options = (
            req.synthetic_response is not None
            or OPTIONS_MARKER in content
            or has_numbered_list(content)
        )

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
        if _effective_interim_mode() == "fold":
            if req.fold_msg_id is not None:
                await self._fold_finalize(req, FOLD_CAPTION_NORMAL)
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
                                "DGN-947 fold budget-drop rescued for user %s: "
                                "%d interim block(s) emitted as own bubble",
                                user_id,
                                len(req.interim_texts),
                            )
                        else:
                            logger.error(
                                "DGN-947 fold rescue send failed for user %s: "
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
                            "DGN-947 fold budget-drop for user %s: %d interim "
                            "block(s) dropped, no streaming handler to rescue "
                            "(background turn, narration lost)",
                            user_id,
                            len(req.interim_texts),
                        )
                elif req.interim_texts:
                    # DGN-947 FOLD-2: interim WAS captured but nothing survived
                    # subtraction (interim_trimmed empty) -- lossLESS. The final
                    # answer fully reproduces the narration paragraph-for-
                    # paragraph, so dropping the fold duplicates nothing. (The
                    # separate lossy budget-drop cause is handled above and can
                    # no longer reach here.)
                    logger.info(
                        "DGN-876 fold dropped for user %s: %d captured "
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

    async def inject_background_turn(self, user_id: int, text: str) -> bool:
        """DGN-217: inject a background/cron notification as a turn into the
        user's LIVE session, with no pending request attached.

        The turn's output flows through the existing no-pending path
        (_handle_proactive_message -> proactive push), so the agent both
        SEES the notification in-session and controls what (if anything)
        reaches the owner -- ending the turn with the bare sentinel NO_PUSH
        suppresses the push.

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
            # DGN-699 D7 (CancelledError): drafts are deleted above, but a
            # grown fold is confirmed with the stop marker, not deleted.
            await self._fold_finalize(request, FOLD_CAPTION_STOPPED)
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

    async def interrupt(self, user_id: int) -> bool:
        """DGN-581: ESC-style soft interrupt of the in-flight turn.

        Sends the SDK control-protocol interrupt (ClaudeSDKClient.interrupt()
        -> Query.interrupt() -> control request {"subtype": "interrupt"}) so
        the current turn stops generating, while the stream state, the client
        connection, and the CLI subprocess all stay alive -- session context
        is preserved. Contrast stop(), which pops the stream state and tears
        the client (and, on timeout, the subprocess) down.

        Queue policy = DRAIN: every pending request for this user is resolved,
        not just the in-flight head, so no queued input auto-fires after the
        stop. Drafted bubbles of the interrupted turn are finalized in place
        (the same preserve path handle_timeout_preserve uses), and the drained
        futures resolve silently: empty content + streamed=True renders
        nothing at the bot reply layer, so the /stop acknowledgement is the
        only message the user sees.

        Returns False when there is nothing to interrupt: no live stream, no
        dispatched in-flight turn, or a client without a connected streaming
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
        # interrupt() is only valid on a connected streaming client; the SDK
        # raises CLIConnectionError when _query is absent. Treat that as
        # nothing-to-interrupt (guard, not failure).
        if getattr(state.client, "_query", None) is None:
            return False
        await asyncio.wait_for(
            state.client.interrupt(), timeout=INTERRUPT_SEND_TIMEOUT
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
            if req.streaming_handler:
                try:
                    await req.streaming_handler.finalize_all()
                except Exception as e:
                    logger.error(
                        "Interrupt finalize failed for user %s: %s", user_id, e
                    )
            # DGN-699 D7 (soft interrupt): a grown fold is confirmed
            # collapsed with the stop marker -- the progress the user
            # watched is preserved, never deleted.
            await self._fold_finalize(req, FOLD_CAPTION_STOPPED)
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
            "Soft-interrupted turn for user %s (drained %d request(s))",
            user_id,
            len(drained),
        )
        return True

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
                # DGN-699 D4/D7 (timeout): a turn whose only streamed surface
                # is the grown fold still counts as partial output preserved.
                # The fold is confirmed with the timeout marker.
                if await self._fold_finalize(head, FOLD_CAPTION_TIMEOUT):
                    partial_preserved = True
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
