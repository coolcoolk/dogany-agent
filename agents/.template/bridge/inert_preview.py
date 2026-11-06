"""Product-screen preview adaptation; never invoke a product renderer or action."""
from __future__ import annotations

import re

from bridge.live_surface import digest, require

PREFIX = "preview:"
_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")


def adapt(candidate_id, buttons):
    """Keep labels/rows/order, replace every action (including URLs)."""
    require(isinstance(candidate_id, str) and _CANDIDATE.fullmatch(candidate_id),
            5, "invalid preview candidate id")
    require(isinstance(buttons, list) and len(buttons) <= 100, 5, "invalid preview keyboard")
    require(sum(len(row) for row in buttons if isinstance(row, list)) <= 100,
            5, "preview keyboard exceeds Telegram's button budget")
    rows = []
    for row_index, row in enumerate(buttons):
        require(isinstance(row, list) and 0 < len(row) <= 8, 5, "invalid preview row")
        converted = []
        for column, button in enumerate(row):
            require(isinstance(button, dict) and isinstance(button.get("text"), str) and
                    bool(button["text"].strip()) and len(button) == 2 and
                    ("callback_data" in button or "url" in button), 5, "invalid preview button")
            action = button.get("callback_data", button.get("url"))
            require(isinstance(action, str) and bool(action), 5, "invalid preview action")
            converted.append(dict(text=button["text"],
                                  callback_data=f"{PREFIX}{candidate_id}:{row_index}:{column}"))
        rows.append(converted)
    return rows


def validate_frame(frame):
    """A preview is a send-only transient frame, never an action slot."""
    require(frame.get("frame") == "inert-preview" and frame.get("mode") == "send" and
            frame.get("slot") == "preview" and frame.get("target") is None and
            frame.get("message_id") is None and not frame.get("photo") and
            "raw_request" not in frame and "sealed_payload" not in frame and
            "entities" not in frame, 16, "invalid inert-preview frame")
    buttons = frame.get("buttons", [])
    require(isinstance(buttons, list) and len(buttons) <= 100, 16, "invalid preview keyboard")
    require(sum(len(row) for row in buttons if isinstance(row, list)) <= 100,
            16, "preview keyboard exceeds Telegram's button budget")
    for row in buttons:
        require(isinstance(row, list) and 0 < len(row) <= 8, 16, "invalid preview row")
        for button in row:
            require(isinstance(button, dict) and frozenset(button) == {"text", "callback_data"} and
                    isinstance(button["text"], str) and bool(button["text"].strip()) and
                    isinstance(button["callback_data"], str) and
                    button["callback_data"].startswith(PREFIX) and
                    len(button["callback_data"].encode("utf-8")) <= 64,
                    16, "preview frame contains a live action")


def prepare(root, candidate_id, text, buttons):
    """Reuse strict push rendering, binding retries to the producer payload."""
    from bridge.live_surface import render_push_notice
    rows = adapt(candidate_id, buttons)
    valid_text = isinstance(text, str) and text.strip() and "[[OPTIONS" not in text
    # ESTATE:BEGIN idrill is an instance-only bridge marker, never preview content
    valid_text = valid_text and "[[IDRILL:" not in text
    # ESTATE:END
    require(valid_text, 5, "preview requires a precomposed screen without bridge markers")
    payloads = render_push_notice(root, text, rows, strict=True)
    require(len(payloads) == 1 and len(payloads[0]["text"]) <= 4000,
            5, "preview must fit one strict message")
    return dict(payloads[0], preview_source_digest=digest(dict(text=text, buttons=buttons)))
