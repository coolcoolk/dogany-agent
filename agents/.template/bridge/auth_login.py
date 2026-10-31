"""DGN-1112: /login -- terminal-free Claude re-login relayed over Telegram.

The bridge spawns the installed CLI's `auth login --claudeai` with plain pipes.
Under a pipe the CLI (2.1.288, read-only evidence in DGN-1857 research
section 2) prints a MANUAL authorize URL on stdout -- the one whose
redirect_uri is the hosted code page, not the localhost callback -- and reads
one `code#state` line from stdin via readline. This module:

  - extracts that URL from the stdout stream (ANSI/OSC stripped, chunk
    boundaries tolerated), validates it against the CLI's OAuth endpoints,
    and hands it to the bot layer to send;
  - takes the owner's next message as the code (bound to this flow's user,
    chat and URL state), writes it to the SAME child's stdin, and never logs
    it, stores it, or lets it reach the model;
  - on exit 0, confirms with `auth status` and calls on_success so the SDK
    layer can recreate its clients at the next idle boundary.

It never runs logout, never copies or reads credential files, and never
touches the keychain itself -- the CLI child is the only credential writer
(DGN-1050). One flow per credential store: in-process registry plus an
advisory flock that other bridges on the same machine also honour.
"""

import asyncio
import codecs
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from bridge import messages

logger = logging.getLogger(__name__)

SendFn = Callable[[int, str], Awaitable[None]]

# CSI (colours, cursor) and OSC (OSC 8 hyperlinks wrap the URL the CLI prints:
# ESC ] 8 ;; URL BEL text ESC ] 8 ;; BEL). Stripping both leaves the visible text.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[@-Z\\-_]")
_URL_RE = re.compile(r"https://[^\s\x00-\x1f<>\"']+")

# OAuth endpoints = installed CLI 2.1.288 constants (CLAUDE_AI_AUTHORIZE_URL /
# MANUAL_REDIRECT_URL, dgn1857-cli-evidence.txt). Only evidenced values are
# listed: an older CLI printing other endpoints fails closed and the owner
# gets the terminal guide. Anything else (a localhost callback URL, a
# CLAUDE_LOCAL_OAUTH_API_BASE override, a foreign host) fails closed too.
_AUTHORIZE_ENDPOINTS = frozenset({("claude.com", "/cai/oauth/authorize")})
_MANUAL_REDIRECTS = frozenset({"https://platform.claude.com/oauth/code/callback"})
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=-]+"
_STATE_RE = re.compile(rf"^{_TOKEN_CHARS}$")
_CODE_RE = re.compile(rf"^({_TOKEN_CHARS})#({_TOKEN_CHARS})$")

# Bridge-env credentials the CLI prefers over a stored login: a fresh login
# would succeed and change nothing the SDK children use.
ENV_CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

URL_TIMEOUT_S = 30.0
CODE_TIMEOUT_S = 600.0
EXIT_TIMEOUT_S = 90.0
STATUS_TIMEOUT_S = 20.0
MAX_CODE_ATTEMPTS = 3
_TAIL_MAX = 4096
# LoginManager._first outcomes other than the awaited value.
_EXITED = object()
_TIMEOUT = object()


def strip_terminal_codes(text: str) -> str:
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    return _ESC_RE.sub("", text)


def validate_login_url(url: str) -> Optional[str]:
    """The URL's `state` when it is a manual authorize URL of the CLI's OAuth
    endpoints, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.username or parts.password:
        return None
    if ((parts.hostname or "").lower(), parts.path) not in _AUTHORIZE_ENDPOINTS:
        return None
    query = parse_qs(parts.query, keep_blank_values=True)

    def _one(name: str) -> Optional[str]:
        values = query.get(name)
        return values[0] if values and len(values) == 1 else None

    if _one("redirect_uri") not in _MANUAL_REDIRECTS:
        return None
    if _one("response_type") != "code" or not _one("code_challenge"):
        return None
    state = _one("state")
    if not state or not _STATE_RE.match(state):
        return None
    return state


def extract_login_url(buffer: str) -> Optional[str]:
    """First https URL on a COMPLETE line of the raw stdout buffer.

    Only newline-terminated lines are considered, so a URL split across
    read() chunks is never cut short."""
    clean = strip_terminal_codes(buffer)
    end = clean.rfind("\n")
    if end < 0:
        return None
    for line in clean[:end].split("\n"):
        match = _URL_RE.search(line)
        if match:
            return match.group(0)
    return None


def credential_store_key(env: Optional[Dict[str, str]] = None) -> str:
    """Identity of the credential store a CLI child would write: its config
    dir (the keychain entry is keyed off the same dir)."""
    env = os.environ if env is None else env
    raw = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return os.path.realpath(os.path.expanduser(raw))


def env_credential_override(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    env = os.environ if env is None else env
    for name in ENV_CREDENTIAL_VARS:
        if env.get(name):
            return name
    return None


@dataclass
class LoginFlow:
    user_id: int
    chat_id: int
    store_key: str
    phase: str = "starting"  # starting -> awaiting_code -> verifying -> done
    state: Optional[str] = None
    attempts: int = 0
    proc: Optional[asyncio.subprocess.Process] = None
    code_future: Optional[asyncio.Future] = None
    task: Optional[asyncio.Task] = None
    stdout_tail: List[str] = field(default_factory=list)
    stderr_tail: List[str] = field(default_factory=list)
    lock_fd: Optional[int] = None


class LoginManager:
    """Owns every pending /login flow of this bridge process."""

    def __init__(
        self,
        *,
        send: SendFn,
        cli_resolver: Callable[[], Optional[str]],
        on_success: Callable[[], None],
        lock_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        url_timeout: float = URL_TIMEOUT_S,
        code_timeout: float = CODE_TIMEOUT_S,
        exit_timeout: float = EXIT_TIMEOUT_S,
        status_timeout: float = STATUS_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._cli_resolver = cli_resolver
        self._on_success = on_success
        self._lock_dir = lock_dir or tempfile.gettempdir()
        self._env = env
        self.url_timeout = url_timeout
        self.code_timeout = code_timeout
        self.exit_timeout = exit_timeout
        self.status_timeout = status_timeout
        self._flows: Dict[str, LoginFlow] = {}

    # --- public surface (bot layer) ---

    def pending_for(self, user_id: int, chat_id: Optional[int] = None) -> Optional[LoginFlow]:
        for flow in self._flows.values():
            if flow.user_id == user_id and (chat_id is None or flow.chat_id == chat_id):
                return flow
        return None

    async def start(self, user_id: int, chat_id: int) -> str:
        """Begin a flow. Returns the outcome tag (tests/logs); every outcome
        has already been told to the owner via send."""
        env = self._child_env()
        store = credential_store_key(env)
        existing = self._flows.get(store)
        if existing is not None:
            await self._send(chat_id, messages.LOGIN_ALREADY_PENDING)
            return "duplicate"
        override = env_credential_override(env)
        if override:
            logger.warning("login: refused, %s set in the bridge env overrides a stored login", override)
            await self._send(chat_id, messages.LOGIN_ENV_TOKEN)
            return "env_token"
        cli = self._cli_resolver()
        if not cli:
            await self._send(chat_id, messages.LOGIN_NO_CLI)
            return "no_cli"
        lock_fd = self._acquire_store_lock(store)
        if lock_fd is None:
            await self._send(chat_id, messages.LOGIN_BUSY)
            return "busy"
        flow = LoginFlow(user_id=user_id, chat_id=chat_id, store_key=store, lock_fd=lock_fd)
        flow.code_future = asyncio.get_running_loop().create_future()
        self._flows[store] = flow
        flow.task = asyncio.create_task(self._run(flow, cli, env))
        return "started"

    async def submit(self, user_id: int, chat_id: int, text: str) -> bool:
        """Offer an owner message to a pending flow. True = consumed: the
        caller must drop it (never forward, log, or store the text)."""
        flow = self.pending_for(user_id, chat_id)
        if flow is None or flow.phase == "done":
            return False
        if flow.phase != "awaiting_code":
            await self._send(chat_id, messages.LOGIN_WAIT)
            return True
        candidate = (text or "").strip()
        match = _CODE_RE.match(candidate)
        if not match:
            await self._bad_attempt(flow, messages.LOGIN_CODE_INVALID)
            return True
        if not hmac.compare_digest(match.group(2), flow.state or ""):
            await self._bad_attempt(flow, messages.LOGIN_CODE_MISMATCH)
            return True
        flow.phase = "verifying"
        logger.info("login: code received for user %s (len=%d)", user_id, len(candidate))
        if flow.code_future and not flow.code_future.done():
            flow.code_future.set_result(candidate)
        await self._send(chat_id, messages.LOGIN_CODE_RECEIVED)
        return True

    async def cancel(self, user_id: int) -> bool:
        flow = self.pending_for(user_id)
        if flow is None or flow.phase == "done":
            return False
        await self._finish(flow, messages.LOGIN_CANCELLED, "cancelled")
        return True

    async def shutdown(self) -> None:
        for flow in list(self._flows.values()):
            await self._finish(flow, None, "shutdown")

    # --- flow body ---

    async def _run(self, flow: LoginFlow, cli: str, env: Dict[str, str]) -> None:
        try:
            await self._send(flow.chat_id, messages.LOGIN_PREPARING)
            try:
                flow.proc = await asyncio.create_subprocess_exec(
                    cli, "auth", "login", "--claudeai",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
            except OSError as e:
                logger.error("login: could not spawn %s: %s", cli, e)
                await self._finish(flow, messages.LOGIN_FAILED, "spawn_error")
                return
            url_future: asyncio.Future = asyncio.get_running_loop().create_future()
            readers = [
                asyncio.create_task(self._read_stdout(flow, url_future)),
                asyncio.create_task(self._read_tail(flow.proc.stderr, flow.stderr_tail)),
            ]
            try:
                await self._drive(flow, url_future)
            finally:
                for reader in readers:
                    reader.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- a flow must always end and unlock
            logger.error("login: flow crashed: %s", e, exc_info=True)
            await self._finish(flow, messages.LOGIN_FAILED, "crash")

    async def _drive(self, flow: LoginFlow, url_future: asyncio.Future) -> None:
        proc = flow.proc
        url = await self._first(url_future, proc, self.url_timeout)
        if not isinstance(url, str):
            logger.error("login: no authorize URL from the CLI (%s); stderr=%r",
                         "timeout" if url is _TIMEOUT else "exited",
                         self._tail_text(flow.stderr_tail))
            await self._finish(flow, messages.LOGIN_FAILED, "no_url")
            return
        state = validate_login_url(url)
        if state is None:
            logger.error("login: CLI printed a URL outside the allowed OAuth endpoints: %s",
                         url.split("?", 1)[0])
            await self._finish(flow, messages.LOGIN_FAILED, "bad_url")
            return
        flow.state = state
        flow.phase = "awaiting_code"
        await self._send(flow.chat_id, messages.LOGIN_URL.format(
            url=url, minutes=max(1, int(self.code_timeout // 60))))

        code = await self._first(flow.code_future, proc, self.code_timeout)
        if code is _TIMEOUT:
            await self._finish(flow, messages.LOGIN_TIMEOUT, "timeout")
            return
        if not isinstance(code, str) or flow.phase != "verifying":
            logger.error("login: CLI exited (rc=%s) before a code arrived; stderr=%r",
                         proc.returncode, self._tail_text(flow.stderr_tail))
            await self._finish(flow, messages.LOGIN_FAILED, "child_exit")
            return
        try:
            proc.stdin.write((code + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.error("login: CLI stdin closed: %s", e)
            await self._finish(flow, messages.LOGIN_FAILED, "stdin_closed")
            return
        code = None  # drop the only reference this coroutine holds

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=self.exit_timeout)
        except asyncio.TimeoutError:
            logger.error("login: CLI did not exit %ss after the code", self.exit_timeout)
            await self._finish(flow, messages.LOGIN_FAILED, "exit_timeout")
            return
        if rc != 0:
            logger.error("login: CLI exited rc=%s; stderr=%r", rc, self._tail_text(flow.stderr_tail))
            await self._finish(flow, messages.LOGIN_FAILED, "child_failed")
            return
        if not await self._status_logged_in(flow):
            await self._finish(flow, messages.LOGIN_FAILED, "status_not_logged_in")
            return
        try:
            self._on_success()
        except Exception as e:  # noqa: BLE001 -- the login itself succeeded
            logger.error("login: on_success hook failed: %s", e, exc_info=True)
        await self._finish(flow, messages.LOGIN_SUCCESS, "success")

    async def _first(self, future: asyncio.Future, proc, timeout: float):
        """Wait for `future`, the child's exit, or the timeout -- whichever
        comes first. Returns the future's result, _EXITED, or _TIMEOUT."""
        exit_wait = asyncio.ensure_future(proc.wait())
        try:
            done, _ = await asyncio.wait(
                {future, exit_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not exit_wait.done():
                exit_wait.cancel()
        if future in done and not future.cancelled():
            return future.result()
        if exit_wait in done:
            # The child may exit right after printing; give a URL already
            # parsed (or a code already set) precedence over the exit.
            await asyncio.sleep(0)
            if future.done() and not future.cancelled():
                return future.result()
            return _EXITED
        return _TIMEOUT

    async def _read_stdout(self, flow: LoginFlow, url_future: asyncio.Future) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        stream = flow.proc.stdout
        while True:
            chunk = await stream.read(1024)
            if not chunk:
                break
            text = decoder.decode(chunk)
            self._push_tail(flow.stdout_tail, text)
            if not url_future.done():
                buffer += text
                url = extract_login_url(buffer)
                if url:
                    url_future.set_result(url)
                    buffer = ""
                elif len(buffer) > 65536:
                    buffer = buffer[-16384:]

    async def _read_tail(self, stream, tail: List[str]) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(1024)
            if not chunk:
                break
            self._push_tail(tail, decoder.decode(chunk))

    @staticmethod
    def _push_tail(tail: List[str], text: str) -> None:
        tail.append(text)
        joined = "".join(tail)
        if len(joined) > _TAIL_MAX:
            tail[:] = [joined[-_TAIL_MAX:]]

    @staticmethod
    def _tail_text(tail: List[str]) -> str:
        return strip_terminal_codes("".join(tail)).strip()[-500:]

    async def _status_logged_in(self, flow: LoginFlow) -> bool:
        cli = self._cli_resolver()
        if not cli:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "auth", "status", "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
                start_new_session=True,
            )
        except OSError as e:
            logger.error("login: auth status spawn failed: %s", e)
            return False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.status_timeout)
        except asyncio.TimeoutError:
            await self._reap(proc)
            logger.error("login: auth status timed out")
            return False
        text = out.decode("utf-8", errors="replace")
        start = text.find("{")
        try:
            data = json.loads(text[start:]) if start >= 0 else {}
        except ValueError:
            data = {}
        logged_in = isinstance(data, dict) and data.get("loggedIn") is True
        logger.info("login: auth status rc=%s loggedIn=%s method=%s", proc.returncode,
                    logged_in, data.get("authMethod") if isinstance(data, dict) else None)
        return proc.returncode == 0 and logged_in

    # --- teardown / helpers ---

    async def _bad_attempt(self, flow: LoginFlow, notice: str) -> None:
        flow.attempts += 1
        if flow.attempts >= MAX_CODE_ATTEMPTS:
            await self._finish(flow, messages.LOGIN_CODE_ATTEMPTS, "too_many_attempts")
            return
        await self._send(flow.chat_id, notice)

    async def _finish(self, flow: LoginFlow, notice: Optional[str], outcome: str) -> None:
        if flow.phase == "done":
            return
        flow.phase = "done"
        logger.info("login: flow for user %s ended: %s", flow.user_id, outcome)
        if flow.code_future and not flow.code_future.done():
            flow.code_future.cancel()
        if flow.proc is not None:
            await self._reap(flow.proc)
        if self._flows.get(flow.store_key) is flow:
            del self._flows[flow.store_key]
        self._release_store_lock(flow)
        current = asyncio.current_task()
        if flow.task is not None and flow.task is not current and not flow.task.done():
            flow.task.cancel()
        if notice:
            try:
                await self._send(flow.chat_id, notice)
            except Exception as e:  # noqa: BLE001
                logger.error("login: could not send the %s notice: %s", outcome, e)

    @staticmethod
    async def _reap(proc) -> None:
        """SIGTERM the child's process group, SIGKILL if it lingers."""
        for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            if proc.returncode is not None:
                return
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                continue

    def _child_env(self) -> Dict[str, str]:
        env = dict(os.environ if self._env is None else self._env)
        # The owner signs in on their phone from the relayed URL; do not pop
        # a browser tab on the (possibly unattended) Mac. UNVERIFIED: a
        # `browser` value in the CLI's own settings outranks BROWSER.
        env["BROWSER"] = "true"
        return env

    def _acquire_store_lock(self, store: str) -> Optional[int]:
        digest = hashlib.sha256(store.encode()).hexdigest()[:16]
        path = os.path.join(self._lock_dir, f"bridge-claude-login-{os.getuid()}-{digest}.lock")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            logger.error("login: lock file %s unavailable: %s", path, e)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def _release_store_lock(flow: LoginFlow) -> None:
        if flow.lock_fd is None:
            return
        try:
            fcntl.flock(flow.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(flow.lock_fd)
            flow.lock_fd = None
