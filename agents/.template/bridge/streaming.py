"""Progressive draft-message streaming.

Accumulates assistant text and edits a Telegram draft message in place, updating
when enough characters arrived OR enough time elapsed. Overflows past 4000 chars
into a new draft. Control markers are stripped from the bubble. RetryAfter is
backed off; "message is not modified" is treated as success.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from telegram import Bot, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError

from bridge.config import config, notify_silent
from bridge.formatting import (
    balance_telegram_html,
    html_to_plain_text,
    split_text,
    strip_display_markers,
)

logger = logging.getLogger(__name__)

_OVERFLOW_LIMIT = 4000

# DGN-376: auto link previews are OFF by default; streamed drafts can end up
# as the final bubble (no re-send), so previews are suppressed here as well.
_LINK_PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)


# --- DGN-699: growing-fold HTML send/edit helpers ----------------------------
#
# The StreamingMessageHandler above sends/edits PLAIN text (no parse_mode);
# the growing fold needs Telegram-HTML edits (blockquote rendering), so it
# gets its own dedicated helpers (spec D1). Content arrives here ALREADY
# rendered (formatting.render_fold_live / render_fold_final: escaping + code
# fence neutralization + rolling-window fit included).
#
# RetryAfter semantics differ per phase (spec D3):
#   - live growth (send_fold_html / edit_fold_html): NON-BLOCKING. A rate
#     limit never sleeps inside the reader loop await chain -- the helper
#     returns immediately with a retry-after hint and the caller retries on a
#     later tick, so the turn pipeline never stalls.
#   - finalize (finalize_fold_html): the turn is ending, so a small BOUNDED
#     backoff is allowed to make the caption+collapse swap stick.
#
# MAJOR-1 (DGN-699 grill): BadRequest (400) on HTML parse can occur when
# narration contains unclosed HTML-like tags or literal </blockquote> that
# the Telegram parser rejects. Each helper tries HTML first; on BadRequest
# it degrades once to plain-text (no parse_mode), which Telegram always
# accepts for prose content. The blockquote structure is lost on plain-text
# sends but the content is preserved and the bubble is never silently dropped.


def _fold_html_to_plain(html_text: str) -> str:
    """Strip HTML tags for plain-text fallback.

    Converts the pre-rendered fold HTML (which may contain <blockquote>,
    <b>, <i>, <code> etc.) back to legible plain text. Unescapes HTML
    entities so the user sees the original characters rather than &amp; etc.
    Only used on BadRequest (400) degradation -- not in the normal path.
    DGN-891: delegates to the shared formatting helper so every send path's
    plain fallback strips tags the same way.
    """
    return html_to_plain_text(html_text)


async def send_fold_html(bot: Bot, chat_id: int, html_text: str) -> Optional[int]:
    """Send the fold bubble (HTML). Returns message_id, or None on failure.

    Non-blocking: RetryAfter is not slept on -- creation is simply deferred
    to a later tick (the caller re-attempts on the next interim).
    MAJOR-1: BadRequest degrades to plain-text send once before giving up.
    """
    # DGN-891: universal tag-balance guard -- no-op on balanced input.
    html_text = balance_telegram_html(html_text)
    for parse_mode, text in [("HTML", html_text), (None, _fold_html_to_plain(html_text))]:
        try:
            kwargs = dict(
                chat_id=chat_id,
                text=text,
                link_preview_options=_LINK_PREVIEW_OFF,
                # DGN-932 mechanism A: the fold is a progress surface; its
                # FIRST send is silent by default (class "fold") and every
                # later edit is notification-free at the Telegram level.
                disable_notification=notify_silent("fold"),
            )
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            sent = await bot.send_message(**kwargs)
            mid = getattr(sent, "message_id", None)
            if parse_mode is None:
                logger.warning("Fold create fell back to plain text (HTML rejected)")
            return mid if isinstance(mid, int) else None
        except BadRequest as e:
            if parse_mode is None:
                logger.error("Fold create plain fallback also failed: %s", e)
                return None
            logger.warning("Fold create HTML rejected (400), retrying plain: %s", e)
            continue
        except RetryAfter as e:
            logger.warning(
                "Fold create rate-limited (retry_after=%s); deferring",
                getattr(e, "retry_after", None),
            )
            return None
        except Exception as e:
            logger.error("Failed to send fold bubble: %s", e)
            return None
    return None


async def edit_fold_html(
    bot: Bot, chat_id: int, message_id: int, html_text: str
) -> "tuple[bool, float]":
    """Live-growth HTML edit of the fold bubble. Returns (ok, retry_after).

    Non-blocking: on RetryAfter it returns (False, seconds) immediately so
    the caller can gate the next attempt; no sleep ever happens here.
    "message is not modified" counts as success.
    MAJOR-1: BadRequest degrades to plain-text edit once before giving up.
    """
    # DGN-891: universal tag-balance guard -- no-op on balanced input.
    html_text = balance_telegram_html(html_text)
    for parse_mode, text in [("HTML", html_text), (None, _fold_html_to_plain(html_text))]:
        try:
            kwargs = dict(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                link_preview_options=_LINK_PREVIEW_OFF,
            )
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            await bot.edit_message_text(**kwargs)
            if parse_mode is None:
                logger.warning("Fold edit fell back to plain text (HTML rejected)")
            return True, 0.0
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return True, 0.0
            if parse_mode is None:
                logger.error("Fold edit plain fallback also failed: %s", e)
                return False, 0.0
            logger.warning("Fold edit HTML rejected (400), retrying plain: %s", e)
            continue
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 3.0) or 3.0)
            logger.warning("Fold edit rate-limited for %.1fs (msg %s)", wait, message_id)
            return False, wait
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return True, 0.0
            logger.error("Failed to edit fold bubble %s: %s", message_id, e)
            return False, 0.0
        except Exception as e:
            logger.error("Failed to edit fold bubble %s: %s", message_id, e)
            return False, 0.0
    return False, 0.0


async def finalize_fold_html(
    bot: Bot, chat_id: int, message_id: int, html_text: str, max_retries: int = 3
) -> bool:
    """Finalize swap edit (caption + collapsed fold). Bounded backoff.

    Runs once per turn at termination, so a short capped RetryAfter sleep is
    acceptable here (unlike the live-growth path). Returns True on success or
    "not modified"; False when every attempt failed (the bubble then stays in
    its last live form -- degraded but never lost).
    MAJOR-1: BadRequest on HTML degrades to plain-text once (caption + body,
    no expandable structure -- fold meaning lost but content preserved).
    """
    # DGN-891: universal tag-balance guard -- no-op on balanced input.
    html_text = balance_telegram_html(html_text)
    plain_text = _fold_html_to_plain(html_text)
    # Each (parse_mode, text) pair is attempted with bounded RetryAfter backoff.
    for parse_mode, text in [("HTML", html_text), (None, plain_text)]:
        for attempt in range(max_retries):
            try:
                kwargs = dict(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    link_preview_options=_LINK_PREVIEW_OFF,
                )
                if parse_mode:
                    kwargs["parse_mode"] = parse_mode
                await bot.edit_message_text(**kwargs)
                if parse_mode is None:
                    logger.warning("Fold finalize fell back to plain text (HTML rejected)")
                return True
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return True
                if parse_mode is None:
                    logger.error("Fold finalize plain fallback also failed: %s", e)
                    return False
                # DGN-946 observability: a finalize-time HTML rejection means
                # the balanced render still failed the Telegram parser (seen
                # 2026-08-15: unmatched </b> before </blockquote>). Log a
                # bounded tail of the offending HTML so the assembly bug is
                # diagnosable from the log alone.
                logger.warning(
                    "Fold finalize HTML rejected (400), retrying plain: %s; "
                    "html tail: %r",
                    e,
                    html_text[-300:],
                )
                break  # break inner loop -> outer loop tries plain
            except RetryAfter as e:
                if attempt == max_retries - 1:
                    break
                wait = min(float(getattr(e, "retry_after", 3.0) or 3.0), 10.0)
                await asyncio.sleep(wait)
            except TelegramError as e:
                if "message is not modified" in str(e).lower():
                    return True
                logger.error("Failed to finalize fold bubble %s: %s", message_id, e)
                return False
            except Exception as e:
                logger.error("Failed to finalize fold bubble %s: %s", message_id, e)
                return False
        else:
            # RetryAfter exhausted all retries for this parse_mode tier
            if parse_mode == "HTML":
                logger.warning("Fold finalize HTML exhausted retries, trying plain")
                continue
            logger.error(
                "Fold finalize gave up after %d attempts (msg %s)", max_retries, message_id
            )
            return False
    logger.error("Fold finalize gave up after %d attempts (msg %s)", max_retries, message_id)
    return False


@dataclass
class DraftState:
    message_id: int
    text: str
    last_update_time: float


class StreamingMessageHandler:
    """Manages the lifecycle of streaming draft messages for one turn."""

    def __init__(self, bot: Bot, chat_id: int, user_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.drafts: List[DraftState] = []
        self.accumulated_text = ""
        self.min_chars = config.draft_update_min_chars
        self.min_interval = config.draft_update_interval
        self._finalized = False
        # True after a draft is sealed with no leftover tail: the next incoming
        # text must open a NEW bubble rather than edit the sealed one. Keeps the
        # "drafts[-1] is the active editable bubble" invariant honest so nothing
        # is silently dropped when an overflow lands exactly on the limit. (#1)
        self._need_new_draft = False

    async def _retry_with_backoff(self, operation, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                return await operation()
            except RetryAfter as e:
                if attempt == max_retries - 1:
                    raise
                wait = float(getattr(e, "retry_after", 2 ** attempt))
                logger.warning("Rate limited, waiting %.1fs (retry %d)", wait, attempt + 1)
                await asyncio.sleep(wait)

    @staticmethod
    def _is_not_modified(error: Exception) -> bool:
        return "message is not modified" in str(error).lower()

    @staticmethod
    def _message_id(message: Any) -> Optional[int]:
        mid = getattr(message, "message_id", None)
        return mid if isinstance(mid, int) else None

    async def _send_extra_chunks(self, chunks: List[str]) -> None:
        """Send fully-finalized overflow bubbles (all but the last split chunk).

        Each is a plain message, not an editable draft, so a single logical text
        that exceeds Telegram's 4096 hard limit is delivered as multiple bubbles
        with nothing dropped.
        """
        for chunk in chunks:
            body = strip_display_markers(chunk)
            if not body.strip():
                continue
            try:
                await self._retry_with_backoff(
                    lambda body=body: self.bot.send_message(
                        chat_id=self.chat_id,
                        text=body,
                        link_preview_options=_LINK_PREVIEW_OFF,
                        # DGN-932: overflow bubbles belong to the answer
                        # surface -> class "draft" (loud by default).
                        disable_notification=notify_silent("draft"),
                    )
                )
            except Exception as e:
                logger.error("Failed to send overflow bubble: %s", e)

    async def create_draft(self, text: str) -> Optional[DraftState]:
        # Split so no single send exceeds Telegram's 4096 hard limit. All but the
        # last chunk are sent as finalized bubbles; the last becomes the editable
        # draft that keeps streaming.
        chunks = split_text(strip_display_markers(text)) if text else [""]
        if len(chunks) > 1:
            await self._send_extra_chunks(chunks[:-1])
        content = chunks[-1] or "..."
        try:
            sent = await self._retry_with_backoff(
                lambda: self.bot.send_message(
                    chat_id=self.chat_id,
                    text=content,
                    link_preview_options=_LINK_PREVIEW_OFF,
                    # DGN-932 mechanism A: notification is decided at this
                    # FIRST send only (class "draft", loud by default = the
                    # answer-arrival signal); later streaming edits are
                    # notification-free at the Telegram level.
                    disable_notification=notify_silent("draft"),
                )
            )
            mid = self._message_id(sent)
            if mid is None:
                raise RuntimeError("send_message returned no message_id")
            # Track the raw (last-chunk) text so subsequent char-delta math and
            # finalize operate on what is actually in this bubble.
            draft = DraftState(
                message_id=mid, text=chunks[-1], last_update_time=time.time()
            )
            self.drafts.append(draft)
            return draft
        except Exception as e:
            logger.error("Failed to create draft: %s", e)
            return None

    async def update_draft(self, draft: DraftState, new_text: str) -> bool:
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=strip_display_markers(new_text),
                    link_preview_options=_LINK_PREVIEW_OFF,
                )
            )
            draft.text = new_text
            draft.last_update_time = time.time()
            return True
        except TelegramError as e:
            if self._is_not_modified(e):
                draft.text = new_text
                draft.last_update_time = time.time()
                return True
            logger.error("Failed to update draft %s: %s", draft.message_id, e)
            return False

    def should_update(self, draft: DraftState, new_char_count: int) -> bool:
        return (
            new_char_count >= self.min_chars
            or (time.time() - draft.last_update_time) >= self.min_interval
        )

    @staticmethod
    def _find_split_boundary(text: str, max_length: int = _OVERFLOW_LIMIT) -> int:
        if len(text) <= max_length:
            return len(text)
        search_start = max(0, max_length - 200)
        para = text.rfind("\n\n", search_start, max_length)
        if para > search_start:
            return para + 2
        line = text.rfind("\n", search_start, max_length)
        if line > search_start:
            return line + 1
        return max_length

    async def handle_overflow(self) -> bool:
        if not self.drafts:
            return False
        # Loop: keep peeling one <=limit bubble off the front until the tail is
        # under the limit. A single overflow that is many multiples of the limit
        # (the SDK delivers a long reply as ONE block) is fully drained here
        # instead of dropping everything past the first split. (RELIABILITY #1)
        while len(self.accumulated_text) >= _OVERFLOW_LIMIT:
            current = self.drafts[-1]
            split_point = self._find_split_boundary(self.accumulated_text)
            if split_point <= 0:
                split_point = _OVERFLOW_LIMIT
            current.text = self.accumulated_text[:split_point]
            await self.finalize_draft(current)
            remaining = self.accumulated_text[split_point:]
            self.accumulated_text = remaining
            if not remaining:
                # Current bubble sealed exactly at the limit, nothing left over.
                # The sealed draft must not keep being edited: flag that the next
                # incoming text opens a fresh bubble.
                self._need_new_draft = True
                return True
            # New editable draft for the remaining tail; if it is still over the
            # limit the loop peels the next bubble on the next iteration.
            if await self.create_draft(remaining) is None:
                # Draft creation failed; stop to avoid an infinite loop.
                return True
            # create_draft may itself have split the tail into finalized bubbles
            # + a last draft; resync accumulated_text to what the live draft holds
            # so the loop's length check reflects the un-delivered remainder.
            self.accumulated_text = self.drafts[-1].text
        return True

    async def update_if_needed(self, new_chunk: str) -> bool:
        if self._finalized:
            return False
        # Each call carries one COMPLETE TextBlock (no partial deltas are fed
        # here), so a call boundary is a block boundary. Blocks from separate
        # assistant messages (e.g. either side of a tool call) are distinct
        # paragraphs; joining them bare glues the second block onto the last
        # line of the first, which breaks line-anchored markers (send_file::,
        # [[OPTIONS]]) -- they then neither strip nor act. Insert a newline.
        if (
            new_chunk
            and self.accumulated_text
            and not self._need_new_draft
            and not self.accumulated_text.endswith("\n")
        ):
            new_chunk = "\n" + new_chunk
        if self._need_new_draft:
            # Previous bubble was sealed at the limit with no leftover; start the
            # tail as a brand-new bubble so this text is never lost.
            self._need_new_draft = False
            self.accumulated_text = new_chunk
            if len(self.accumulated_text) < _OVERFLOW_LIMIT:
                await self.create_draft(self.accumulated_text)
                return True
            await self.create_draft(self.accumulated_text)
            self.accumulated_text = self.drafts[-1].text
            await self.handle_overflow()
            return True
        self.accumulated_text += new_chunk
        if len(self.accumulated_text) >= _OVERFLOW_LIMIT:
            # The SDK can deliver a long reply as ONE block, so the very first
            # chunk may already be over the limit with no draft yet. Ensure a
            # draft exists (create_draft itself splits and drains most of it),
            # then drain any remaining overflow. Without this, handle_overflow's
            # empty-drafts early return would drop everything. (RELIABILITY #1)
            if not self.drafts:
                if await self.create_draft(self.accumulated_text) is None:
                    return True
                self.accumulated_text = self.drafts[-1].text
            await self.handle_overflow()
            return True
        if not self.drafts:
            await self.create_draft(self.accumulated_text)
            return True
        current = self.drafts[-1]
        chars_since = len(self.accumulated_text) - len(current.text)
        if self.should_update(current, chars_since):
            await self.update_draft(current, self.accumulated_text)
            return True
        return False

    async def finalize_draft(self, draft: DraftState) -> bool:
        # Split so the edit never exceeds Telegram's 4096 hard limit. The draft
        # message is edited to the first chunk; any overflow beyond it is sent as
        # extra finalized bubbles so nothing is dropped. (RELIABILITY #1)
        chunks = split_text(strip_display_markers(draft.text)) or [""]
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=chunks[0] or "...",
                    link_preview_options=_LINK_PREVIEW_OFF,
                )
            )
            ok = True
        except TelegramError as e:
            if self._is_not_modified(e):
                ok = True
            else:
                logger.error("Failed to finalize draft %s: %s", draft.message_id, e)
                ok = False
        if len(chunks) > 1:
            await self._send_extra_chunks(chunks[1:])
        return ok

    async def seal_segment(self) -> None:
        """DGN-947: seal accumulated interim bubbles as permanent messages so
        the terminal answer opens a FRESH draft.

        Restores the finalize-consumer invariant that _reply_smart's
        delete / edit / skip branches assume: drafts == final-answer bubbles
        only. In inline mode the reader loop streams interim narration and the
        terminal answer into the SAME handler; without this seal the two glue
        into one draft, so the decision-turn finalize (delete-drafts, HTML
        no-op skip, overflow scrub) operates on interim+final glue and either
        wipes the narration or freezes raw tags. Sealing at the terminal
        boundary edits each interim bubble to its permanent form and clears
        the draft list, leaving the narration as its own standing bubbles
        (DGN-930 inline live-narration intent, preserved) while the terminal
        answer streams cleanly into a new draft.

        NOT finalize_all: the handler is NOT latched (_finalized stays False),
        so the terminal answer's own finalize_all still runs. Idempotent on an
        empty draft list; never opens an empty bubble.
        """
        if self._finalized or not self.drafts:
            return
        if self.accumulated_text:
            self.drafts[-1].text = self.accumulated_text
        for draft in self.drafts:
            await self.finalize_draft(draft)
        self.drafts.clear()
        self.accumulated_text = ""
        self._need_new_draft = False

    async def finalize_all(self) -> bool:
        if self._finalized:
            return False
        self._finalized = True
        if self.drafts and self.accumulated_text:
            self.drafts[-1].text = self.accumulated_text
        for draft in self.drafts:
            await self.finalize_draft(draft)
        return True

    async def cancel(self) -> bool:
        if self._finalized:
            return False
        self._finalized = True
        for draft in self.drafts:
            try:
                await self._retry_with_backoff(
                    lambda: self.bot.delete_message(
                        chat_id=self.chat_id, message_id=draft.message_id
                    )
                )
            except TelegramError as e:
                logger.error("Failed to delete draft %s: %s", draft.message_id, e)
        self.drafts.clear()
        self.accumulated_text = ""
        return True
