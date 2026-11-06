"""DGN-1985: /login asks Claude or Codex; Codex onboarding from chat.

Owner 2026-10-10 19:21-19:24: /login asks which one (Claude or Codex); the
Codex screen says plainly that, for now, Codex only runs as a background
worker (like a subagent) and the main agent stays Claude Code.

Two Codex paths:

  - Subscription (ChatGPT plan): `codex login --device-auth` on pipes. The CLI
    prints a verification URL and a one-time code; both are relayed to the
    chat, the owner approves on their phone, and this module polls
    `codex login status` (through the host lib) until it reports a login,
    the child exits, or the code expires.
  - API key: the key is collected by the secret intake (DGN-1981: chat ->
    keychain). This module only asks the intake for it through KeyIntake and,
    once stored, runs the host lib's `apikey` verb, which pipes the key from
    the store to `codex login --with-api-key` on stdin. The shared intake
    deletes the message first; this adapter only holds a completion callback.

"Onboarded" is derived from `codex login status` by
routines/lib/codex_login.py (the host lib, called as a subprocess); this
module never stores a flag of its own. Without the lib the Codex screen says
the state is unknown and the device flow does not start.

Pure render + one flow manager, like passes.py / auth_login.py; bot.py
routes the "login:" callbacks. Copy: bridge/i18n login_pick / codex_* keys
(PENDING_OWNER_CONFIRM).
"""

import asyncio
import codecs
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from bridge.config import PROJECT_ROOT
from bridge.i18n import t

logger = logging.getLogger(__name__)

CB = "login:"
CB_CLAUDE = "login:claude"
CB_CODEX = "login:codex"
CB_CX_SUB = "login:cx:sub"
CB_CX_KEY = "login:cx:key"

SECRET_NAME = "codex-api-key"     # same name as routines/lib/codex_login.py

Button = Tuple[str, str]
Rows = List[List[Button]]
SendFn = Callable[[int, str], Awaitable[None]]

# codex-cli 0.161.0 device-auth: "<issuer>/codex/device" with the issuer
# https://auth.openai.com. Only that endpoint is relayed; anything else
# (an override, a foreign host) fails closed.
_DEVICE_ENDPOINTS = frozenset({("auth.openai.com", "/codex/device")})
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_URL_RE = re.compile(r"https://[^\s\x00-\x1f<>\"']+")
# The one-time code stands alone on its line, after the line that mentions
# the code (e.g. "ABCD-12345"). Upper-case letters, digits and dashes only.
_CODE_RE = re.compile(r"^[A-Z0-9]{4,}(?:-[A-Z0-9]{3,})+$|^[A-Z0-9]{6,12}$")

DEVICE_TIMEOUT_S = 15 * 60.0      # the CLI's code lifetime
PROMPT_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 5.0
LIB_TIMEOUT_S = 30.0
KEY_TIMEOUT_S = 90.0
_TAIL_MAX = 4096
_EXITED = object()


# --- host lib (routines/lib/codex_login.py) ---------------------------------

def _lib() -> Path:
    return Path(PROJECT_ROOT) / "routines" / "lib" / "codex_login.py"


def run_lib(*args: str, timeout: float = LIB_TIMEOUT_S) -> Tuple[int, str]:
    """(rc, stdout) of the host lib; (127, "") when it is absent. Blocking."""
    lib = _lib()
    if not lib.is_file():
        return 127, ""
    try:
        proc = subprocess.run([sys.executable, "-B", str(lib), *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("codex_login %s failed: %s", args[:1], type(e).__name__)
        return 1, ""
    return proc.returncode, proc.stdout


def read_status() -> Dict[str, Any]:
    """The derived onboarded flag; state 'unavailable' without the lib."""
    rc, out = run_lib("status")
    if rc == 127:
        return {"onboarded": False, "state": "unavailable", "method": None}
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict) or "onboarded" not in data:
        return {"onboarded": False, "state": "error", "method": None}
    return data


def resolve_cli() -> Optional[str]:
    rc, out = run_lib("resolve")
    path = out.strip()
    return path if rc == 0 and path else None


def login_with_key(secret: str = SECRET_NAME) -> Dict[str, Any]:
    rc, out = run_lib("apikey", "--secret", secret, timeout=KEY_TIMEOUT_S)
    if rc == 127:
        return {"ok": False, "reason": "unavailable"}
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {"ok": False, "reason": "error"}


# --- render -----------------------------------------------------------------

def render_pick() -> Tuple[str, Rows]:
    return t("login_pick"), [[(t("login_btn_claude"), CB_CLAUDE),
                              (t("login_btn_codex"), CB_CODEX)]]


def status_line(st: Dict[str, Any]) -> str:
    state = st.get("state")
    if st.get("onboarded"):
        method = st.get("method")
        if method == "chatgpt":
            return t("codex_status_in_chatgpt")
        if method == "api_key":
            return t("codex_status_in_apikey")
        return t("codex_status_in")
    if state == "logged_out":
        return t("codex_status_out")
    if state == "no_cli":
        return t("codex_status_no_cli")
    return t("codex_status_unknown")


def render_codex(st: Dict[str, Any]) -> Tuple[str, Rows]:
    """Scope note + current state + the two paths. No path buttons when the
    CLI is missing or the host lib is absent (nothing could run)."""
    text = "\n\n".join([t("codex_scope"), status_line(st)])
    if st.get("state") in ("no_cli", "unavailable"):
        return text, []
    text += "\n\n" + t("codex_pick_path")
    return text, [[(t("codex_btn_sub"), CB_CX_SUB)], [(t("codex_btn_key"), CB_CX_KEY)]]


# --- device-auth prompt parsing ---------------------------------------------

def strip_terminal_codes(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def validate_device_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or port is not None or parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    return ((parts.hostname or "").lower(), parts.path.rstrip("/")) in _DEVICE_ENDPOINTS


def extract_device_prompt(buffer: str) -> Optional[Tuple[str, str]]:
    """(url, code) from the CLI's device-auth prompt once both are on
    COMPLETE lines, else None. The code is the first code-shaped line after
    a line mentioning 'code' (the URL line itself is skipped)."""
    clean = strip_terminal_codes(buffer)
    end = clean.rfind("\n")
    if end < 0:
        return None
    url = code = None
    after_code_word = False
    for raw in clean[:end].split("\n"):
        line = raw.strip()
        if not line:
            continue
        if url is None:
            m = _URL_RE.search(line)
            if m:
                url = m.group(0)
                continue
        if "code" in line.lower():
            after_code_word = True
            continue
        if after_code_word and code is None and _CODE_RE.match(line):
            code = line
    if url and code:
        return url, code
    return None


# --- device-auth flow -------------------------------------------------------

@dataclass
class DeviceFlow:
    user_id: int
    chat_id: int
    lock_key: str
    phase: str = "starting"   # starting -> awaiting_approval -> done
    proc: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = None
    lock_fd: Optional[int] = None
    tail: List[str] = field(default_factory=list)


class CodexDeviceLogin:
    """Owns every pending Codex device-auth flow of this bridge process."""

    def __init__(
        self,
        *,
        send: SendFn,
        cli_resolver: Callable[[], Optional[str]] = resolve_cli,
        status_reader: Callable[[], Dict[str, Any]] = read_status,
        lock_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        prompt_timeout: float = PROMPT_TIMEOUT_S,
        device_timeout: float = DEVICE_TIMEOUT_S,
        poll_interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._send = send
        self._cli_resolver = cli_resolver
        self._status_reader = status_reader
        self._lock_dir = lock_dir or tempfile.gettempdir()
        self._env = env
        self.prompt_timeout = prompt_timeout
        self.device_timeout = device_timeout
        self.poll_interval = poll_interval
        self._flows: Dict[str, DeviceFlow] = {}

    def pending_for(self, user_id: int) -> Optional[DeviceFlow]:
        for flow in self._flows.values():
            if flow.user_id == user_id and flow.phase != "done":
                return flow
        return None

    async def start(self, user_id: int, chat_id: int) -> str:
        """Begin a flow; the returned outcome tag is for tests/logs, every
        outcome has already been told to the owner."""
        env = self._child_env()
        key = os.path.realpath(os.path.expanduser(
            env.get("CODEX_HOME") or os.path.join(env.get("HOME") or "~", ".codex")))
        if key in self._flows:
            await self._send(chat_id, t("codex_already_pending"))
            return "duplicate"
        cli = await asyncio.to_thread(self._cli_resolver)
        if not cli:
            await self._send(chat_id, t("codex_no_cli"))
            return "no_cli"
        lock_fd = self._acquire_lock(key)
        if lock_fd is None:
            await self._send(chat_id, t("codex_busy"))
            return "busy"
        flow = DeviceFlow(user_id=user_id, chat_id=chat_id, lock_key=key, lock_fd=lock_fd)
        self._flows[key] = flow
        flow.task = asyncio.create_task(self._run(flow, cli, env))
        return "started"

    async def cancel(self, user_id: int) -> bool:
        flow = self.pending_for(user_id)
        if flow is None:
            return False
        await self._finish(flow, t("codex_cancelled"), "cancelled")
        return True

    async def shutdown(self) -> None:
        for flow in list(self._flows.values()):
            await self._finish(flow, None, "shutdown")

    async def _run(self, flow: DeviceFlow, cli: str, env: Dict[str, str]) -> None:
        try:
            await self._send(flow.chat_id, t("codex_device_preparing"))
            # A login that already exists must not read as this flow's
            # success: then only the child's own exit counts.
            before = await asyncio.to_thread(self._status_reader)
            poll = not before.get("onboarded")
            try:
                flow.proc = await asyncio.create_subprocess_exec(
                    cli, "login", "--device-auth",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
            except OSError as e:
                logger.error("codex login: could not spawn: %s", e)
                await self._finish(flow, t("codex_failed"), "spawn_error")
                return
            prompt: asyncio.Future = asyncio.get_running_loop().create_future()
            shared = {"buf": ""}
            readers = [asyncio.create_task(self._read(flow, s, prompt, shared))
                       for s in (flow.proc.stdout, flow.proc.stderr)]
            try:
                await self._drive(flow, prompt, poll)
            finally:
                for r in readers:
                    r.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- a flow must always end and unlock
            logger.error("codex login: flow crashed: %s", e, exc_info=True)
            await self._finish(flow, t("codex_failed"), "crash")

    async def _drive(self, flow: DeviceFlow, prompt: asyncio.Future, poll: bool) -> None:
        proc = flow.proc
        got = await self._first(prompt, proc, self.prompt_timeout)
        if not isinstance(got, tuple):
            logger.error("codex login: no device prompt (%s); tail=%r",
                         "exited" if got is _EXITED else "timeout", self._tail_text(flow))
            await self._finish(flow, t("codex_failed"), "no_prompt")
            return
        url, code = got
        if not validate_device_url(url):
            logger.error("codex login: device URL outside the allowed endpoint: %s",
                         url.split("?", 1)[0])
            await self._finish(flow, t("codex_failed"), "bad_url")
            return
        flow.phase = "awaiting_approval"
        # The code is a phishing target: relay it, never log it.
        logger.info("codex login: relaying device prompt (code len=%d)", len(code))
        await self._send(flow.chat_id, t("codex_device_relay").format(
            url=url, code=code, minutes=max(1, int(self.device_timeout // 60))))
        code = None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.device_timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                await self._finish(flow, t("codex_timeout"), "timeout")
                return
            try:
                rc = await asyncio.wait_for(asyncio.shield(proc.wait()),
                                            timeout=min(self.poll_interval, left))
            except asyncio.TimeoutError:
                rc = None
            if flow.phase == "done":
                return
            if rc is not None:
                break
            if poll:
                st = await asyncio.to_thread(self._status_reader)
                if st.get("onboarded"):
                    await self._finish(flow, t("codex_success"), "success_polled")
                    return
        if rc != 0:
            logger.error("codex login: CLI exited rc=%s; tail=%r", rc, self._tail_text(flow))
            await self._finish(flow, t("codex_failed"), "child_failed")
            return
        st = await asyncio.to_thread(self._status_reader)
        if not st.get("onboarded"):
            await self._finish(flow, t("codex_failed"), "status_not_logged_in")
            return
        await self._finish(flow, t("codex_success"), "success")

    async def _first(self, future: asyncio.Future, proc, timeout: float):
        exit_wait = asyncio.ensure_future(proc.wait())
        try:
            done, _ = await asyncio.wait({future, exit_wait}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not exit_wait.done():
                exit_wait.cancel()
        if future in done and not future.cancelled():
            return future.result()
        if exit_wait in done:
            await asyncio.sleep(0)
            if future.done() and not future.cancelled():
                return future.result()
            return _EXITED
        return None

    async def _read(self, flow: DeviceFlow, stream, prompt: asyncio.Future,
                    shared: Dict[str, str]) -> None:
        """Both streams feed one buffer (the CLI may split the prompt)."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(1024)
            if not chunk:
                break
            text = decoder.decode(chunk)
            self._push_tail(flow.tail, text)
            if not prompt.done():
                shared["buf"] += text
                found = extract_device_prompt(shared["buf"])
                if found:
                    prompt.set_result(found)
                    shared["buf"] = ""
                elif len(shared["buf"]) > 65536:
                    shared["buf"] = shared["buf"][-16384:]

    @staticmethod
    def _push_tail(tail: List[str], text: str) -> None:
        tail.append(text)
        joined = "".join(tail)
        if len(joined) > _TAIL_MAX:
            tail[:] = [joined[-_TAIL_MAX:]]

    @staticmethod
    def _tail_text(flow: DeviceFlow) -> str:
        # Error lines only: drop anything code-shaped before it reaches a log.
        lines = [ln for ln in strip_terminal_codes("".join(flow.tail)).splitlines()
                 if ln.strip() and not _CODE_RE.match(ln.strip())]
        return "\n".join(lines)[-500:]

    async def _finish(self, flow: DeviceFlow, notice: Optional[str], outcome: str) -> None:
        if flow.phase == "done":
            return
        flow.phase = "done"
        logger.info("codex login: flow for user %s ended: %s", flow.user_id, outcome)
        if flow.proc is not None:
            await self._reap(flow.proc)
        if self._flows.get(flow.lock_key) is flow:
            del self._flows[flow.lock_key]
        self._release_lock(flow)
        current = asyncio.current_task()
        if flow.task is not None and flow.task is not current and not flow.task.done():
            flow.task.cancel()
        if notice:
            try:
                await self._send(flow.chat_id, notice)
            except Exception as e:  # noqa: BLE001
                logger.error("codex login: could not send the %s notice: %s", outcome, e)

    @staticmethod
    async def _reap(proc) -> None:
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
        env["BROWSER"] = "true"   # never pop a tab on the (unattended) Mac
        env["NO_COLOR"] = "1"
        return env

    def _acquire_lock(self, key: str) -> Optional[int]:
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        path = os.path.join(self._lock_dir, f"bridge-codex-login-{os.getuid()}-{digest}.lock")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            logger.error("codex login: lock file unavailable: %s", e)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def _release_lock(flow: DeviceFlow) -> None:
        if flow.lock_fd is None:
            return
        try:
            fcntl.flock(flow.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(flow.lock_fd)
            flow.lock_fd = None


# --- API-key path: the DGN-1981 intake seam ---------------------------------

class KeyIntake:
    """Interface of the DGN-1981 chat secret intake, as this flow uses it.

    request() asks the owner for the secret `name` in chat and stores it in
    the keychain; it calls on_stored() once it is stored. It returns False
    when the intake cannot run (then nothing was asked). The secret itself
    never comes back to the caller."""

    def __init__(self, manager, send: SendFn):
        self._manager = manager
        self._send = send

    async def request(self, user_id: int, chat_id: int, name: str,
                      on_stored: Callable[[], Awaitable[None]]) -> bool:
        from bridge.secret_intake import INTAKE_TIMEOUT_S

        if not self._manager.start(user_id, chat_id, name, on_stored=on_stored):
            return False
        try:
            await self._send(chat_id, t("key_prompt").format(
                slot=name, minutes=max(1, int(INTAKE_TIMEOUT_S // 60))))
        except Exception:
            self._manager.cancel(user_id)
            return False
        return True


class UnavailableIntake(KeyIntake):
    """Fallback when the host's secret intake is unavailable."""

    def __init__(self):
        pass

    async def request(self, user_id, chat_id, name, on_stored) -> bool:
        return False


async def start_key_path(intake: KeyIntake, user_id: int, chat_id: int, send: SendFn,
                         key_login: Callable[[str], Dict[str, Any]] = login_with_key) -> str:
    """Ask the intake for the key; once stored, pipe it to the CLI (host lib)
    and report. Outcome tag for tests/logs."""
    async def on_stored() -> None:
        await send(chat_id, t("codex_key_checking"))
        try:
            res = await asyncio.to_thread(key_login, SECRET_NAME)
        except Exception:
            res = {"ok": False}
        logger.info("codex login: api-key path ended: %s", bool(res.get("ok")))
        await send(chat_id, t("codex_success") if res.get("ok") else t("codex_key_failed"))

    try:
        asked = await intake.request(user_id, chat_id, SECRET_NAME, on_stored)
    except Exception as e:  # noqa: BLE001 -- an intake fault is an absent intake
        logger.error("codex login: intake failed: %s", type(e).__name__)
        asked = False
    if not asked:
        await send(chat_id, t("codex_key_unavailable"))
        return "intake_unavailable"
    return "asked"
