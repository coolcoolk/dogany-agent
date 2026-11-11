"""Durable photo intake, owned by the bridge's single asyncio event loop.

Persist before network I/O. Terminal receipts dedupe Telegram replays for a
day; pending records survive restarts. No network calls happen in this store.
"""

import json
import os
import tempfile
import time
from pathlib import Path


RETRY_WINDOW = 10 * 60
RECEIPT_TTL = 24 * 60 * 60
PENDING_NOTE = (
    "[Photo intake: a photo from this chat is still arriving. "
    "Its contents are not available yet; do not assume no photo was sent.]"
)


class PendingMedia:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.records = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.records = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pending-media-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.records, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def accept(self, update, caption, coalesce):
        message = update.message
        key = f"{message.chat_id}:{message.message_id}"
        now = time.time()
        # Only terminal receipts expire; never prune a pending download.
        self.records = {
            k: r for k, r in self.records.items()
            if r["status"] == "pending" or r.get("notice_pending")
            or now - r["finished_at"] < RECEIPT_TTL
        }
        if key in self.records:
            return None
        self.records[key] = {
            "chat": message.chat_id,
            "message_id": message.message_id,
            "user_id": update.effective_user.id,
            "file_id": message.photo[-1].file_id,
            "kind": "photo",
            "caption": caption,
            "coalesce": coalesce,
            "received_at": now,
            "group_id": message.media_group_id,
            "update": json.loads(update.to_json()),
            "status": "pending",
        }
        self.save()
        return key

    def pending_for(self, chat_id):
        return any(r["status"] == "pending" and r["chat"] == chat_id
                   for r in self.records.values())

    def finish(self, keys, status, *, notice=False):
        for key in keys:
            self.records[key]["status"] = status
            self.records[key]["finished_at"] = time.time()
            # A receipt needs only identity + status, not the owner's caption.
            self.records[key].pop("update", None)
            self.records[key].pop("caption", None)
        if notice:
            self.records[keys[0]]["notice_pending"] = True
        self.save()
