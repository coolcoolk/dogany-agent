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
from typing import Any, List, Optional, Tuple

from telegram import Bot, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError

from bridge.config import config, notify_silent
from bridge.formatting import (
    balance_telegram_html,
    demark_markdown_for_stream,
    html_to_plain_text,
    render_fold_final,
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
        # DGN-1651: where the assistant message currently being streamed starts
        # inside accumulated_text, and the (start, end) span of the last
        # TERMINAL message's text on the live surface. Maintained by
        # begin_message() / _track_segment(); both go None the moment an
        # overflow re-bases accumulated_text, because the offsets then no
        # longer describe what is on screen (retraction is skipped and the
        # pre-DGN-1651 behaviour stands).
        self._segment_start: Optional[int] = 0
        self._segment_terminal = False
        self._terminal_span: Optional[Tuple[int, int]] = None
        # DGN-1703: where the current model STEP's text starts (a step ends at
        # every main-thread user message: tool result or Stop-hook re-prompt).
        # The live stream carries no terminal flag, so the step is the unit a
        # Stop block supersedes. Same invalidation rules as the offsets above.
        self._step_start: Optional[int] = 0

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
            # DGN-969: overflow bubbles are sent as PLAIN text (no
            # parse_mode) and are never revisited with a converted HTML
            # edit in the single-bubble path -- de-mark so they never show
            # raw markdown, here or later.
            display_body = demark_markdown_for_stream(body)
            try:
                await self._retry_with_backoff(
                    lambda display_body=display_body: self.bot.send_message(
                        chat_id=self.chat_id,
                        text=display_body,
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
            # DGN-1651: the leading chunks leave as standing bubbles this
            # handler never edits again -- the live offsets stop describing
            # the screen.
            self._invalidate_segment()
            await self._send_extra_chunks(chunks[:-1])
        content = chunks[-1] or "..."
        # DGN-969: DISPLAY-only de-mark -- draft.text below still stores the
        # raw `content` untouched, so downstream char-count math, overflow
        # splitting, and the eventual HTML finalize all see the original text.
        display_content = demark_markdown_for_stream(content)
        try:
            sent = await self._retry_with_backoff(
                lambda: self.bot.send_message(
                    chat_id=self.chat_id,
                    text=display_content,
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
        # DGN-969: DISPLAY-only de-mark -- draft.text is set to the raw
        # `new_text` below unconditionally, untouched by the transform.
        display_text = demark_markdown_for_stream(strip_display_markers(new_text))
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=display_text,
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
            # DGN-1651: every iteration seals a bubble and re-bases the
            # accumulator -- the live offsets no longer describe the screen.
            self._invalidate_segment()
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

    def _invalidate_segment(self) -> None:
        """DGN-1651: forget the live segment offsets.

        Called from every path that RE-BASES accumulated_text (overflow drain,
        sealed-at-the-limit restart, a create_draft that peeled finalized
        bubbles off the front). The offsets then describe text that is no
        longer where they say it is -- and part of it may already sit in a
        bubble this handler can no longer rewrite -- so retraction is disabled
        for the rest of the turn and the pre-DGN-1651 behaviour stands. Never
        loses text: retraction is an optimisation of what the live bubble
        shows, never the carrier of the final body.
        """
        self._segment_start = None
        self._terminal_span = None
        self._step_start = None

    def _track_segment(self) -> None:
        """DGN-1651: keep the live TERMINAL segment's span in sync.

        Runs after every accepted chunk. Only a terminal message's text is
        tracked -- interim narration (inline mode) is a progress record, never
        superseded by a later answer, and must stay on screen.
        """
        if not self._segment_terminal or self._segment_start is None:
            return
        if self._segment_start > len(self.accumulated_text):
            self._invalidate_segment()
            return
        self._terminal_span = (self._segment_start, len(self.accumulated_text))

    def begin_message(self, terminal: bool, retract: bool = False) -> bool:
        """DGN-1651: open an assistant-message boundary on the live surface.

        A Stop-hook block lets the model keep the turn and emit a SECOND
        terminal message. The first one already streamed into this handler,
        and the DGN-1253 turn assembly re-delivers it (deduped) as part of the
        final body -- so the live copy is a SUPERSEDED answer, not narration.
        Left alone it is either glued to by the regeneration (fold/suppress:
        the bubble shows the answer twice) or sealed as a standing duplicate
        bubble (inline, seal_segment below).

        retract=True cuts exactly the recorded span of the previous terminal
        message back out of accumulated_text, so the regeneration REWRITES the
        same bubble instead. Nothing is deleted and nothing is re-sent: the
        bubble's own text is left untouched until the replacement arrives, so
        a regeneration that produces no text at all still leaves the original
        answer standing. Text arriving between the two terminal messages
        (inline narration) sits outside the span and survives.

        Returns True when a span was actually retracted.

        DGN-1703: retract no longer requires `terminal` -- the live stream
        marks nothing terminal, and the span may come from supersede_step().
        The caller only passes retract=True for a message carrying text.
        """
        retracted = False
        if retract and self._terminal_span is not None:
            start, end = self._terminal_span
            if 0 <= start <= end <= len(self.accumulated_text):
                head = self.accumulated_text[:start]
                tail = self.accumulated_text[end:]
                # The glue newline of the retracted block sits AT `start`
                # (it was prepended to the chunk), so it goes with the span.
                # When the segment started the surface, the survivor's own
                # glue newline is now leading -- strip it.
                self.accumulated_text = head + tail if head else tail.lstrip("\n")
                if self.drafts:
                    # The bubble still shows the superseded text: let the very
                    # next chunk re-render it instead of waiting for the
                    # char/interval threshold (the replacement can be SHORTER
                    # than what is on screen, which never meets min_chars).
                    self.drafts[-1].last_update_time = 0.0
                retracted = True
                if self._step_start is not None:
                    self._step_start = len(self.accumulated_text)
            self._terminal_span = None
        self._segment_start = len(self.accumulated_text)
        self._segment_terminal = terminal
        return retracted

    def begin_step(self) -> None:
        """DGN-1703: a main-thread user message closed the model step; the
        next step's text starts at the current end of the live surface."""
        if self._step_start is not None:
            self._step_start = len(self.accumulated_text)

    def supersede_step(self) -> bool:
        """DGN-1703: a Stop hook blocked the turn; the step that just ended
        was a finished answer the regeneration will replace.

        Records that step's text as the retractable span (begin_message with
        retract=True cuts it once replacement text arrives, so a regeneration
        with no text leaves the answer standing). An existing terminal span
        (DGN-1651, stop_reason-bearing streams) is kept as is. Returns True
        when a span is armed.
        """
        if self._terminal_span is not None:
            return True
        start = self._step_start
        end = len(self.accumulated_text)
        if start is None or not (0 <= start < end):
            return False
        if not self.accumulated_text[start:end].strip():
            return False
        self._terminal_span = (start, end)
        return True

    async def update_if_needed(self, new_chunk: str) -> bool:
        if self._finalized:
            return False
        try:
            return await self._append_chunk(new_chunk)
        finally:
            self._track_segment()

    async def _append_chunk(self, new_chunk: str) -> bool:
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
            # DGN-1651: the part of this segment already sealed at the limit
            # is out of reach -- give up the offsets (see _invalidate_segment).
            self._invalidate_segment()
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
        # DGN-969: DISPLAY-only de-mark of the sealed chunk -- `chunks`
        # (derived from the raw draft.text) is what the overflow-scrub /
        # multi-bubble delete+resend logic keys off; only the string handed
        # to Telegram here is transformed.
        display_first = demark_markdown_for_stream(chunks[0]) or "..."
        try:
            await self._retry_with_backoff(
                lambda: self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=draft.message_id,
                    text=display_first,
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
        # DGN-1651: the sealed bubbles are standing messages now; the live
        # surface is empty, so the message being opened starts at 0 and no
        # earlier terminal span is retractable.
        self._segment_start = 0
        self._terminal_span = None
        self._step_start = 0

    async def finalize_all(self) -> bool:
        if self._finalized:
            return False
        self._finalized = True
        if self.drafts and self.accumulated_text:
            self.drafts[-1].text = self.accumulated_text
        for draft in self.drafts:
            await self.finalize_draft(draft)
        return True

    async def cancel(self, fold_caption: Optional[str] = None) -> bool:
        """Cancel the in-flight streaming turn.

        fold_caption=None (default, UNCHANGED): delete every draft bubble
        outright. Every existing caller (/new, the /stop hard-teardown
        fallback) keeps today's silent-removal UX -- not this ticket's call
        to make.

        fold_caption=<str> (auto-interrupt only, interrupt-fold ticket):
        instead of deleting, collapse whatever streamed so far into ONE
        expandable fold quote -- the same render_fold_final +
        finalize_fold_html swap the growing dev-fold already uses on
        interrupt (see sdk_bridge._fold_finalize) -- so the user keeps a
        visible, collapsed record of the answer that got cut off. A draft
        list with no real content (nothing survives fold rendering, e.g. an
        empty/placeholder-only draft) still falls through to a plain delete:
        an empty collapsed quote would be pure noise.
        """
        if self._finalized:
            return False
        self._finalized = True
        if fold_caption is not None and self.drafts:
            if self.accumulated_text:
                self.drafts[-1].text = self.accumulated_text
            # "..." is create_draft's internal placeholder for "nothing real
            # has streamed yet" (DGN-947 invariant), never genuine content --
            # excluded here so an all-placeholder draft list renders no fold
            # body and falls through to the plain-delete branch below.
            fold_texts = [
                d.text for d in self.drafts if d.text.strip() not in ("", "...")
            ]
            html = render_fold_final(fold_texts, fold_caption) if fold_texts else ""
            if html:
                target = self.drafts[-1]
                ok = await finalize_fold_html(
                    self.bot, self.chat_id, target.message_id, html
                )
                if not ok:
                    logger.error(
                        "Interrupt fold finalize failed for chat %s (msg %s)",
                        self.chat_id,
                        target.message_id,
                    )
                # Earlier overflow bubbles are now redundant -- their content
                # is folded into the single collapsed quote above.
                for draft in self.drafts[:-1]:
                    try:
                        await self._retry_with_backoff(
                            lambda: self.bot.delete_message(
                                chat_id=self.chat_id, message_id=draft.message_id
                            )
                        )
                    except TelegramError as e:
                        logger.error(
                            "Failed to delete draft %s: %s", draft.message_id, e
                        )
                self.drafts.clear()
                self.accumulated_text = ""
                return True
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
