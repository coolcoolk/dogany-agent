"""DGN-1981: key input mode -- take one key from the chat, store it, forget it.

`/key <slot>` (owner, private chat) arms ONE pending intake bound to that
user + chat, for INTAKE_TIMEOUT_S. The bot layer hands the owner's next text
message here BEFORE every other route (split-merge, fast-path, the model) and
deletes it from the chat first. This module then:

  - checks the shape (one token, printable ASCII, nothing a key never has);
  - runs the slot's one tiny verify call (routines/lib/secret_verify.py):
    "rejected" -> not stored; "ok" / "unknown" -> stored;
  - stores it through the framework secret store (routines/lib/secret_store.py:
    macOS keychain from this bridge process, which runs in the logged-in user
    session so the keychain is reachable; 0600 file elsewhere).

The value is never logged, echoed, persisted anywhere but the store, nor kept
on the manager: the pending record holds the slot only. Outcomes are short
words the bot maps to owner copy; a store error carries its code
(keychain_locked, insecure_perms, ...) and never the value.
"""

import asyncio
import importlib.util
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

INTAKE_TIMEOUT_S = 120.0
MAX_KEY_LEN = 512

# A pasted Anthropic/OpenAI key outside key input mode (bot safety net).
STRAY_KEY_RE = re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}")

# Outcomes of consume().
STORED = "stored"
STORED_UNVERIFIED = "stored_unverified"
REJECTED = "rejected"
INVALID = "invalid"
STORE_FAILED = "store_failed"


def _lib_dirs():
    here = Path(__file__).resolve().parent
    try:
        from bridge.config import PROJECT_ROOT  # type: ignore
        yield Path(PROJECT_ROOT) / "routines" / "lib"
    except Exception:
        pass
    yield here.parent / "routines" / "lib"


def load_lib(name: str):
    """Import routines/lib/<name>.py (the minted root first, then the tree)."""
    cached = sys.modules.get(f"_routines_lib_{name}")
    if cached is not None:
        return cached
    for d in _lib_dirs():
        path = d / f"{name}.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location(f"_routines_lib_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules[f"_routines_lib_{name}"] = mod
            return mod
    raise ImportError(f"routines/lib/{name}.py not found")


@dataclass
class PendingIntake:
    user_id: int
    chat_id: int
    slot: str
    deadline: float
    on_stored: Optional[Callable[[], Awaitable[None]]] = None


class SecretIntakeManager:
    def __init__(
        self,
        store=None,
        verify: Optional[Callable[[str, str], str]] = None,
        slots: Optional[Dict] = None,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = INTAKE_TIMEOUT_S,
    ):
        if store is None or verify is None or slots is None:
            sv = load_lib("secret_verify")
            slots = sv.SLOTS if slots is None else slots
            verify = sv.verify if verify is None else verify
        self._store = store if store is not None else load_lib("secret_store")
        self._verify = verify
        self._slots = slots
        self._clock = clock
        self._timeout = timeout_s
        self._pending: Dict[int, PendingIntake] = {}

    def slot_names(self):
        return sorted(self._slots)

    def start(self, user_id: int, chat_id: int, slot: str,
              on_stored: Optional[Callable[[], Awaitable[None]]] = None) -> bool:
        name = (slot or "").strip().lower()
        if name not in self._slots:
            return False
        self._pending[user_id] = PendingIntake(
            user_id, chat_id, name, self._clock() + self._timeout, on_stored)
        return True

    def pending_for(self, user_id: int, chat_id: int) -> Optional[PendingIntake]:
        p = self._pending.get(user_id)
        if p is None:
            return None
        if self._clock() > p.deadline:
            self._pending.pop(user_id, None)
            return None
        return p if p.chat_id == chat_id else None

    def cancel(self, user_id: int) -> bool:
        return self._pending.pop(user_id, None) is not None

    async def consume(self, user_id: int, chat_id: int, text: str) -> Tuple[str, str]:
        """(outcome, error code). One message only: the pending is gone after."""
        p = self.pending_for(user_id, chat_id)
        if p is None:
            return INVALID, ""
        self._pending.pop(user_id, None)
        slot = self._slots[p.slot]
        value = (text or "").strip()
        if len(value) > MAX_KEY_LEN or not self._store.valid_value(value):
            return INVALID, ""
        try:
            verdict = await asyncio.to_thread(self._verify, slot.kind, value)
        except Exception:
            verdict = "unknown"
        if verdict == "rejected":
            return REJECTED, ""
        try:
            await asyncio.to_thread(self._store.set, slot.service, slot.account, value)
        except Exception as e:  # SecretStoreError carries .code, never the value
            code = getattr(e, "code", "store_failed")
            if code not in ("keychain_locked", "insecure_perms", "invalid_value",
                            "invalid_name", "not_found", "store_failed"):
                code = "store_failed"
            logger.warning("secret intake: store failed for slot %s (%s)", p.slot, code)
            return STORE_FAILED, code
        finally:
            del value
        if verdict not in ("ok", "unknown"):
            verdict = "unknown"
        logger.info("secret intake: stored slot %s (verify=%s)", p.slot, verdict)
        if p.on_stored is not None:
            try:
                await p.on_stored()
            except Exception:
                logger.warning("secret intake: completion failed for slot %s", p.slot)
        return (STORED if verdict == "ok" else STORED_UNVERIFIED), ""
