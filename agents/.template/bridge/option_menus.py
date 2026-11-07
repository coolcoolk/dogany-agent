"""Durable answer latch, independent of Telegram edits and bounded bubble history.

Answers are saved before the first network await in the bridge event loop.
Tombstones survive restarts and prevent a replay from starting a second turn.
"""
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class OptionMenus:
    def __init__(self, path: Path):
        self.path = path
        self.rows = {}
        try:
            self.rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.rows, dict):
                raise ValueError("menu store is not an object")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # Never overwrite unreadable answer history with an empty registry.
            logger.exception("option menu history could not be loaded")
            raise

    @staticmethod
    def key(chat_id, message_id):
        return f"{chat_id}:{message_id}"

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as out:
                tmp = out.name
                os.chmod(tmp, 0o600)
                json.dump(self.rows, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, chat_id, message_id):
        return self.rows.get(self.key(chat_id, message_id))

    @staticmethod
    def keyboard_id(inline_keyboard):
        """Return a stable identity for the question displayed by a keyboard."""
        if inline_keyboard is None:
            return None
        buttons = []
        for row in inline_keyboard:
            buttons.append([
                [getattr(button, "text", None),
                 getattr(button, "callback_data", None)]
                for button in row
            ])
        payload = json.dumps(buttons, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def answer(self, chat_id, message_id, choice, inline_keyboard=None):
        key = self.key(chat_id, message_id)
        row = self.rows.get(key)
        keyboard_id = self.keyboard_id(inline_keyboard)
        keyboard_ids = []
        if row and row["answered"]:
            if isinstance(row.get("keyboard_ids"), list):
                keyboard_ids = row["keyboard_ids"]
            elif "keyboard_id" in row:
                keyboard_ids = [row["keyboard_id"]]
            else:
                # Rows written before keyboard identity existed stay answered.
                keyboard_ids = [None]
            if None in keyboard_ids or keyboard_id in keyboard_ids:
                return False
        # Existing options-bubbles/outbound-ledger own menu registration.
        # Legacy keyboards need a tombstone even if that history was pruned.
        self.rows[key] = dict(chat_id=chat_id, message_id=message_id,
                              answered=True, choice=choice,
                              keyboard_ids=keyboard_ids + [keyboard_id])
        self._save()
        return True
