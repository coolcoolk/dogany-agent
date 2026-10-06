"""Owner-send transport belt (DGN-1736 slice 0), OBSERVE mode.

Every in-process owner send -- message.reply_text, query.edit_message_text,
query.answer, bot.send_message in streaming / dashboard / countdown -- goes
through the one HTTPXRequest object installed with Application.builder()
.request(...). This module subclasses that transport on the same PTB seam
heartbeat.HeartbeatHTTPXRequest already uses (DGN-140) and inspects the
owner-visible text of each outbound request for bridge directive lines that
must never reach the owner: NO_PUSH, PUSH, [[OPTIONS...]], [[IDRILL:...]],
send_file::, link_preview:: and tool-call markup.

OBSERVE ONLY. A hit logs one "OWNER_BELT_HIT path=<endpoint> field=<name>
shape=<name>" line and the request is forwarded byte-identical: nothing is
stripped, dropped or raised. Any hit is a seat bug upstream (the send path
that produced it skipped a guard), to be filed, not tuned away here. An
enforcing mode is a separate, owner-noted change
(DGN-1736 delivery-seat design, section 4, slice 0).

Why only these shapes: at the transport the text is already rendered to
Telegram HTML. These directive lines survive rendering and HTML escaping
unchanged (tool-call markup arrives as "&lt;invoke"), so they are checkable
here. The machine-line gate, the leaked-tail strip and consumed-run
stripping must run pre-render (DGN-1209) and stay in the seat.

The scan is fence-blind on purpose: it judges what the owner would see, so a
code block that shows a directive line is reported too (observe cost: one
log line).

Out of scope: routines/push.sh is a separate process with its own HTTP
client and never reaches this transport (its python hop keeps its own
sanitize step). getUpdates keeps HeartbeatHTTPXRequest; it is inbound.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)

# Bot API method -> the parameter that carries owner-visible text.
OWNER_TEXT_FIELDS: Dict[str, str] = {
    "sendMessage": "text",
    "editMessageText": "text",
    "answerCallbackQuery": "text",
    "editMessageCaption": "caption",
    "sendPhoto": "caption",
    "sendDocument": "caption",
}

# Whole-line shapes, matched after inline HTML tags are removed from the line
# and the line is stripped (so "<b>NO_PUSH</b>" still counts).
_CORE_LINE_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("NO_PUSH", re.compile(r"^NO_PUSH$")),
    ("PUSH", re.compile(r"^PUSH$")),
    ("OPTIONS", re.compile(r"^\[\[OPTIONS\b[^\]]*\]\]$")),
    ("send_file", re.compile(r"^send_file::")),
    ("link_preview", re.compile(r"^link_preview::")),
)
# ESTATE:BEGIN idrill directive shape (the [[IDRILL:..]] marker is estate render vocabulary, DGN-924)
_ESTATE_LINE_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("IDRILL", re.compile(r"^\[\[IDRILL:[^\]]*\]\]$")),
)
# ESTATE:ELSE
# _ESTATE_LINE_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = ()
# ESTATE:END
_LINE_SHAPES = _CORE_LINE_SHAPES + _ESTATE_LINE_SHAPES
# Tool-call markup can sit anywhere in a line; raw (plain-text send) or
# HTML-escaped (rendered send).
_TOOLCALL = re.compile(r"(?:<|&lt;)(?:antml:)?invoke\b", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def endpoint_of(url: str) -> str:
    """Bot API method name from a request URL (never returns the token)."""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def scan_text(text: str) -> List[str]:
    """Return the directive shapes present in owner-visible `text`, in order,
    each at most once. Pure; never raises on str input."""
    found: List[str] = []
    if _TOOLCALL.search(text):
        found.append("toolcall")
    for raw in text.splitlines():
        line = _TAG.sub("", raw).strip()
        if not line:
            continue
        for name, pattern in _LINE_SHAPES:
            if name not in found and pattern.search(line):
                found.append(name)
    return found


def scan_request(url: str, request_data: Any) -> List[Tuple[str, str, str]]:
    """Return (endpoint, field, shape) hits for one outbound request.

    Endpoints outside OWNER_TEXT_FIELDS are not inspected. Reads
    request_data.parameters only; never mutates the request.
    """
    endpoint = endpoint_of(url)
    field = OWNER_TEXT_FIELDS.get(endpoint)
    if field is None or request_data is None:
        return []
    value = request_data.parameters.get(field)
    if not isinstance(value, str) or not value:
        return []
    return [(endpoint, field, shape) for shape in scan_text(value)]


def observe(url: str, request_data: Optional[Any]) -> List[Tuple[str, str, str]]:
    """Log every hit for one request. Never raises: a scan error is logged
    and the send goes out regardless."""
    try:
        hits = scan_request(url, request_data)
    except Exception as exc:  # the belt must never block a send
        logger.warning("OWNER_BELT_SCAN_ERROR path=%s err=%r", endpoint_of(url), exc)
        return []
    for endpoint, field, shape in hits:
        logger.warning(
            "OWNER_BELT_HIT path=%s field=%s shape=%s", endpoint, field, shape
        )
    return hits


class OwnerGuardRequest(HTTPXRequest):
    """HTTPXRequest that observes owner-bound text before each send.

    Wire this as the application's general `.request(...)` (not the
    get_updates_request). The request is forwarded unchanged.
    """

    async def do_request(self, url: str, method: str, request_data=None, **kwargs):
        observe(url, request_data)
        return await super().do_request(url, method, request_data=request_data, **kwargs)
