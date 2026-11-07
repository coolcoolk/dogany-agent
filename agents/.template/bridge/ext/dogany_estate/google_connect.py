"""DGN-1854 W2: Google connect, bridge half (LOCKED spec sections 3.3, 3.5, 6.1).

Three jobs, all machine-only (the model never sees a pasted URL):

  1. Paste intercept (3.3). Path B of the loopback consent: Google redirects
     the owner's phone browser to http://127.0.0.1:<port>/?code=...&state=...,
     which cannot load there, and the owner pastes that address into Telegram.
     A message matching PASTE_RE is consumed before the DGN-1112 login-code
     check and before every model route. Private owner chat: a pending flow
     for this chat with the pasted state -> `connect finish`; anything else
     -> C7 (expired). Group chat: deleted, no finish, private_only.
     The text is never logged (length only) and never reaches argv of
     anything but the frozen `connect finish --url` call.
  2. Completion relay (3.5). A finish result is dropped into this instance's
     session-inbox (DGN-217 contract: temp name, atomic rename to *.md) so
     the agent resumes the kit step. Drop line format (path A listeners use
     the same one):  [google-connect-result] class=<cls> sub=<sub|->
  3. Block-screen ask (6.1, prompt part). Two consecutive expired flows with
     no callback hit for one chat within 24 h -> exactly one C15 with two
     option buttons. The answer is relayed the same way (class=cap_suspected
     for "block").

Frozen contract used (spec 1.2): `python3 -m service.google connect finish
--url <pasted> --origin <inst>:<chat> --json` -> {ok, class}, run from the
framework root. Store layout read-only (spec 2): ~/.dogany/google/pending/
<state>.json {..., origin, account_expected, expires_at, done}. This module
never writes the store.

<inst> is the instance directory name (PROJECT_ROOT.name); the kit's
`connect start --origin` must use the same value.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

# Spec 3.3, verbatim.
PASTE_RE = re.compile(
    r"^\s*https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/\S*[?&](code|error)="
)
# pending/<state>.json is a file name: only a safe charset is ever joined.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")

RELAY_PREFIX = "[google-connect-result]"
CB_PREFIX = "gconn:"
CB_BLOCK = "gconn:block"
CB_NONE = "gconn:none"

FINISH_TIMEOUT_S = 90
ASK_WINDOW_S = 24 * 3600

# finish class -> bridge i18n key (None = no bridge reply; the relay drives
# the agent). Spec section 6 table, owner copy column.
CLASS_KEYS: Dict[str, Optional[str]] = {
    "ok": None,
    "flow_done": None,
    "store_busy": None,
    "consent_denied": "google.login.denied",
    "scope_partial": "google.login.partial",
    "flow_expired": "google.login.expired",
    "flow_unknown": "google.login.expired",
    "account_mismatch": "google.login.account_mismatch",
    "browser_blocked": "google.login.browser_blocked",
    "cap_suspected": "google.login.cap",
    "client_invalid": "google.login.internal",
    "store_unwritable": "google.login.internal",
    "network": "google.login.network",
    "google_5xx": "google.login.network",
}
FALLBACK_KEY = "google.login.internal"


# --- locations ---------------------------------------------------------------

def store_root() -> Path:
    return Path.home() / ".dogany" / "google"


def instance_id(project_root: Path) -> str:
    return Path(project_root).name


def origin_for(project_root: Path, chat_id: int) -> str:
    return "%s:%s" % (instance_id(project_root), chat_id)


def framework_root(project_root: Path) -> Optional[Path]:
    """The checkout that carries service/google (.instance.conf
    DOGANY_REPO_ROOT, else the instance itself), or None."""
    candidates: List[Path] = []
    try:
        conf = (Path(project_root) / ".instance.conf").read_text(encoding="utf-8")
        for line in conf.splitlines():
            if line.startswith("DOGANY_REPO_ROOT="):
                candidates.append(Path(line.split("=", 1)[1].strip().strip("\"'")))
    except OSError:
        pass
    candidates.append(Path(project_root))
    for root in candidates:
        if (root / "service" / "google" / "__main__.py").is_file():
            return root
    return None


def cli_argv() -> List[str]:
    return [sys.executable, "-m", "service.google"]


# --- matcher / pending -------------------------------------------------------

def is_paste(text: Optional[str]) -> bool:
    return bool(text) and PASTE_RE.match(text) is not None


def pasted_state(text: str) -> Optional[str]:
    try:
        query = urlsplit(text.strip()).query
        values = parse_qs(query).get("state") or []
    except ValueError:
        return None
    state = values[0] if values else ""
    return state if _STATE_RE.match(state) else None


def _epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def read_pending(state: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    if not _STATE_RE.match(state or ""):
        return None
    path = (root or store_root()) / "pending" / ("%s.json" % state)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def match_pending(text: str, origin: str, now: Optional[float] = None,
                  root: Optional[Path] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """(state, pending) when a live flow for `origin` owns the pasted state,
    else (state_or_None, None)."""
    state = pasted_state(text)
    if state is None:
        return None, None
    pending = read_pending(state, root)
    if pending is None or pending.get("origin") != origin:
        return state, None
    expires = _epoch(pending.get("expires_at"))
    if expires is not None and expires <= (time.time() if now is None else now):
        return state, None
    return state, pending


# --- finish ------------------------------------------------------------------

def run_finish(text: str, origin: str, project_root: Path) -> Dict[str, Any]:
    """Frozen `connect finish`. Never logs the URL, stdout or stderr."""
    root = framework_root(project_root)
    if root is None:
        logger.warning("google-paste: service.google not found; finish not run")
        return {"ok": False, "class": "client_invalid"}
    argv = cli_argv() + ["connect", "finish", "--url", text.strip(),
                         "--origin", origin, "--json"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(argv, cwd=str(root), env=env, capture_output=True,
                              text=True, timeout=FINISH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("google-paste: finish timed out")
        return {"ok": False, "class": "network"}
    except OSError as e:
        logger.warning("google-paste: finish not started (%s)", type(e).__name__)
        return {"ok": False, "class": "client_invalid"}
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        out = {}
    if not isinstance(out, dict):
        out = {}
    cls = out.get("class")
    if not isinstance(cls, str) or not cls:
        cls = "ok" if out.get("ok") is True else ""
    if not cls:
        logger.warning("google-paste: finish rc=%s, unreadable result (stdout len=%d)",
                       proc.returncode, len(proc.stdout or ""))
        return {"ok": False, "class": "client_invalid"}
    result = {"ok": out.get("ok") is True, "class": cls}
    if isinstance(out.get("sub"), str):
        result["sub"] = out["sub"]
    logger.info("google-paste: finish rc=%s class=%s", proc.returncode, cls)
    return result


def account_email(sub: Optional[str], project_root: Path) -> Optional[str]:
    """Email of the expected account for C14 (status never returns a token)."""
    if not sub:
        return None
    root = framework_root(project_root)
    if root is None:
        return None
    try:
        proc = subprocess.run(cli_argv() + ["status", "--account", sub, "--json"],
                              cwd=str(root), capture_output=True, text=True, timeout=15)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    email = data.get("account") if isinstance(data, dict) else None
    return email if isinstance(email, str) and email else None


def reply_key(cls: str) -> Optional[str]:
    if cls in CLASS_KEYS:
        return CLASS_KEYS[cls]
    return FALLBACK_KEY


def _internal(project_root: Path, command: str, origin: str, *args: str) -> Dict[str, Any]:
    """W1 owns prompt claims and cap markers under its store-wide lock."""
    root = framework_root(project_root)
    if root is None:
        return {}
    try:
        proc = subprocess.run(cli_argv() + [command, "--origin", origin, *args],
                              cwd=str(root), capture_output=True, text=True, timeout=30)
        result = json.loads(proc.stdout)
        return result if proc.returncode == 0 and isinstance(result, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def claim_prompt(project_root: Path, chat_id: int) -> bool:
    return _internal(project_root, "_expired-prompt", origin_for(project_root, chat_id)).get("key") == "google.login.blocked_screen_ask"


def record_blocked(project_root: Path, chat_id: int, blocked: bool) -> bool:
    result = _internal(project_root, "_blocked-screen", origin_for(project_root, chat_id),
                       "--answer", "block" if blocked else "missed")
    return result.get("ok") is True


# --- relay (3.5) -------------------------------------------------------------

def service_relays(pending: Dict[str, Any], project_root: Path) -> bool:
    """W1 relays both completion paths when prepare stored this instance.

    Older pending records have no instance_path; the bridge supplies their
    fallback relay. Avoid posting the same completion twice on current W1.
    """
    path = pending.get("instance_path")
    try:
        return isinstance(path, str) and Path(path).resolve() == Path(project_root).resolve()
    except (OSError, ValueError):
        return False

def relay_text(cls: str, sub: Optional[str] = None, note: str = "") -> str:
    line = "%s class=%s sub=%s" % (RELAY_PREFIX, cls, sub or "-")
    body = ("Google connect flow result for this chat (machine relay, "
            "DGN-1854). Resume the kit connect step from this class; do not "
            "ask the owner to paste the address again unless the class says "
            "the flow expired.")
    if note:
        body += " " + note
    return line + "\n" + body + "\n"


def write_relay(inbox_dir: Path, text: str) -> Optional[Path]:
    """DGN-217 writer contract: temp name, then atomic rename to *.md."""
    try:
        inbox_dir.mkdir(parents=True, exist_ok=True)
        name = "%d-google-connect-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])
        tmp = inbox_dir / (".%s.tmp" % name)
        tmp.write_text(text, encoding="utf-8")
        final = inbox_dir / ("%s.md" % name)
        os.replace(tmp, final)
        return final
    except OSError as e:
        logger.warning("google-connect relay not written (%s)", type(e).__name__)
        return None


# --- block-screen ask (6.1, prompt part) -------------------------------------

def _ledger_path(bot_data_dir: Path) -> Path:
    return Path(bot_data_dir) / "state" / "google-connect.json"


def load_ledger(bot_data_dir: Path) -> Dict[str, Any]:
    try:
        data = json.loads(_ledger_path(bot_data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("hits", {})
    data.setdefault("asked", {})
    return data


def save_ledger(bot_data_dir: Path, data: Dict[str, Any]) -> None:
    path = _ledger_path(bot_data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("google-connect ledger not written (%s)", type(e).__name__)


def note_callback_hit(bot_data_dir: Path, state: str, now: Optional[float] = None) -> None:
    """A paste for this flow arrived: it is not a no-callback expiry."""
    now = time.time() if now is None else now
    data = load_ledger(bot_data_dir)
    hits = {k: v for k, v in data["hits"].items() if now - float(v) < 2 * ASK_WINDOW_S}
    hits[state] = now
    data["hits"] = hits
    save_ledger(bot_data_dir, data)


def chats_to_ask(project_root: Path, bot_data_dir: Path, now: Optional[float] = None,
                 root: Optional[Path] = None) -> List[int]:
    """Chats whose two most recent flows (links sent within 24 h) both
    expired with no callback hit, and which were not asked in the last 24 h.
    Read-only on the store."""
    now = time.time() if now is None else now
    prefix = instance_id(project_root) + ":"
    pending_dir = (root or store_root()) / "pending"
    try:
        files = [p for p in pending_dir.glob("*.json") if p.is_file()]
    except OSError:
        return []
    if not files:
        return []
    ledger = load_ledger(bot_data_dir)
    flows: Dict[int, List[Tuple[float, str, bool]]] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        origin = data.get("origin")
        if not isinstance(origin, str) or not origin.startswith(prefix):
            continue
        try:
            chat = int(origin[len(prefix):])
        except ValueError:
            continue
        expires = _epoch(data.get("expires_at"))
        if expires is None or now - expires > ASK_WINDOW_S:
            continue
        state = path.stem
        no_callback = (expires <= now and not data.get("done") and not data.get("callback_hit")
                       and state not in ledger["hits"])
        flows.setdefault(chat, []).append((expires, state, no_callback))
    out = []
    for chat, items in sorted(flows.items()):
        items.sort()
        last_two = items[-2:]
        if len(last_two) < 2 or not all(no_cb for _, _, no_cb in last_two):
            continue
        asked = ledger["asked"].get(str(chat))
        if asked is not None and now - float(asked) < ASK_WINDOW_S:
            continue
        out.append(chat)
    return out


def mark_asked(bot_data_dir: Path, chat_id: int, now: Optional[float] = None) -> None:
    data = load_ledger(bot_data_dir)
    data["asked"][str(chat_id)] = time.time() if now is None else now
    save_ledger(bot_data_dir, data)
