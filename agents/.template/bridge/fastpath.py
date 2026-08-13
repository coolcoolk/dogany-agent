"""DGN-801: deterministic fast-path interceptor -- domain-agnostic runner.

Purpose: on instances that opt in (FASTPATH_HANDLER set), short numeric-looking
messages are offered to a domain-owned handler executable BEFORE an SDK model
turn spawns. A handler that recognizes and fully processes the message returns
the rendered push body on stdout; the bridge pushes it and suppresses the model
turn entirely. Everything else falls back to the normal SDK path. This removes
the full-history resume-turn cost for high-frequency deterministic inputs
(e.g. a workout set-result "8 2") without teaching the bridge any domain.

Domain isolation (root relocation, spec DGN-801 v1 section 1): the bridge knows
ONE configured handler path and a 3-value contract; zero domain vocabulary,
paths, or CLI names live here. The handler owns message grammar, session
markers, db paths, and step chaining internally.

Handler contract (`<handler> handle --raw "<text>"`):
  exit 0 -> PROCESSED. stdout = rendered user-facing body to push verbatim.
            The bridge treats exit 0 as the ONLY commit witness: state may have
            changed, so the message must never be re-fed to the model
            (double-processing seal, grill F3). Handler-side post-commit
            failures are degraded to exit 0 by the handler for the same reason.
  exit 2 -> FALLBACK. stdout = "FALLBACK <reason>". Message was not processed;
            the normal SDK turn runs. Misdetections land here, so the bridge
            prefilter can stay generous.
  anything else (other exit codes / timeout / spawn failure) -> treated as
            FALLBACK: fail-safe, never a dropped message. The bridge does not
            assume state is unchanged on non-zero exits -- it only refuses to
            claim "processed"; re-run side effects are the handler's job to
            prevent (its commit point degrades later failures to exit 0).

Timeout (grill F3): the handler is spawned in its OWN process group
(start_new_session=True) and on timeout the whole group is killed
(SIGTERM -> grace -> SIGKILL) so children sharing the group never orphan.
A child the handler deliberately detaches into its own session (e.g. a
long-lived timer armed post-commit) is by design out of kill reach.

Read-only invariant (grill M6): the bridge never writes any handler-side
marker or state file (e.g. files/program/.tip-pending); those have exactly one
writer on the domain side. This module only spawns the handler and reads its
stdout/exit code.
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bridge import config as config_mod
from bridge.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

EXIT_PROCESSED = 0
EXIT_FALLBACK = 2
# Grace between SIGTERM and SIGKILL on a timed-out handler group, seconds.
KILL_GRACE_S = 0.5
# Prefilter bound: real deterministic inputs are short; anything long is
# conversational and goes straight to the model without a spawn.
MAX_PREFILTER_LEN = 64


@dataclass
class FastpathResult:
    """Outcome of one handler invocation.

    processed=True ONLY on exit 0 (the commit witness). body carries the exit-0
    stdout to push. reason is a short diagnostic tag for logs (fallback reason,
    "timeout", "spawn_error:...").
    """

    processed: bool
    body: str = ""
    reason: str = ""


def handler_path() -> Optional[Path]:
    """Configured handler path, resolved against PROJECT_ROOT; None when unset."""
    raw = (config_mod.FASTPATH_HANDLER or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def enabled() -> bool:
    """True only when a handler is configured AND exists as an executable file.

    Unset / empty / missing / non-executable all read as OFF, so instances
    without a handler are byte-for-byte unaffected (grill G1).
    """
    path = handler_path()
    return path is not None and path.is_file() and os.access(path, os.X_OK)


def prefilter(text: str) -> bool:
    """Cheap DOMAIN-AGNOSTIC signal: is this message worth offering the handler?

    Deliberately generous -- the real verdict belongs to the handler (exit 2
    FALLBACK degrades any false positive safely to the model path). This gate
    only avoids a subprocess spawn for messages that are obviously
    conversational: long, multiline, digit-free, or bot commands.
    """
    t = text.strip()
    if not t or len(t) > MAX_PREFILTER_LEN:
        return False
    if t.startswith("/"):
        return False
    if "\n" in t:
        return False
    return any(c.isdigit() for c in t)


async def run_handler(text: str) -> FastpathResult:
    """Invoke the configured handler on one raw message text.

    Never raises: every failure mode collapses to a not-processed result so the
    caller's fail-safe (normal SDK turn) always has a defined path.
    """
    path = handler_path()
    if path is None:
        return FastpathResult(processed=False, reason="disabled")
    timeout = float(config_mod.FASTPATH_TIMEOUT_S)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            "handle",
            "--raw",
            text,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            # F3: own process group so a timeout kill can take the handler AND
            # any children still sharing the group, never a lone-pid kill.
            start_new_session=True,
        )
    except Exception as e:
        logger.error("fast-path handler spawn failed (%s): %s", path, e)
        return FastpathResult(processed=False, reason=f"spawn_error:{e}")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_process_group(proc)
        # B1: recover a committed exit0 from the SIGTERM grace window. The Skull
        # F3 contract degrades a post-commit failure to exit 0, and a handler
        # can perform that degrade inside the SIGTERM->SIGKILL grace window
        # while we are timing it out. _kill_process_group awaited proc.wait(),
        # so returncode is now authoritative: if the handler exited 0 the state
        # IS committed -- treating it as a plain timeout FALLBACK would re-feed
        # the model and double-log the next slot (UPSERT does NOT prevent that).
        # So exit0 wins even on the timeout path. communicate() was cancelled by
        # the timeout, so the rendered body is lost -> processed with no body,
        # which routes to the M3 death-notice (never a model fallback). Any
        # non-zero / still-None returncode is a genuine timeout FALLBACK.
        if proc.returncode == EXIT_PROCESSED:
            logger.warning(
                "fast-path handler committed (exit0) inside the timeout grace "
                "window; body lost -> death-notice, no model fallback"
            )
            return FastpathResult(processed=True, body="", reason="timeout_exit0")
        logger.warning(
            "fast-path handler timed out after %.1fs; process group killed", timeout
        )
        return FastpathResult(processed=False, reason="timeout")
    except asyncio.CancelledError:
        # Task cancelled mid-handler (e.g. /stop clears the user queue): do not
        # leave the handler group running unsupervised past the task's life.
        await asyncio.shield(_kill_process_group(proc))
        raise
    except Exception as e:
        await _kill_process_group(proc)
        logger.error("fast-path handler I/O failed: %s", e)
        return FastpathResult(processed=False, reason=f"io_error:{e}")
    stdout = (stdout_b or b"").decode("utf-8", "replace").strip()
    if proc.returncode == EXIT_PROCESSED:
        return FastpathResult(processed=True, body=stdout)
    if proc.returncode == EXIT_FALLBACK:
        reason = stdout.removeprefix("FALLBACK").strip() or "unspecified"
        logger.info("fast-path FALLBACK: %s", reason)
        return FastpathResult(processed=False, reason=reason)
    stderr = (stderr_b or b"").decode("utf-8", "replace").strip()
    logger.warning(
        "fast-path handler exit %s (treated as fallback): %s",
        proc.returncode,
        stderr[:200],
    )
    return FastpathResult(processed=False, reason=f"exit_{proc.returncode}")


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """F3: reap a timed-out handler by PROCESS GROUP, never by single pid.

    SIGTERM the group, wait KILL_GRACE_S, then SIGKILL the group. Always awaits
    the direct child so no zombie is left behind. Children sharing the group
    (plain forks / background jobs) die with it; a child that made itself a
    session leader (deliberate detach) is intentionally out of reach.
    """
    if proc.returncode is not None:
        return
    pgid: Optional[int] = None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pass
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
    except asyncio.TimeoutError:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except asyncio.TimeoutError:
            logger.error(
                "fast-path handler group %s did not die after SIGKILL", pgid
            )
