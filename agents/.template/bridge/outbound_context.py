"""DGN-1951: the agent sees the messages it sent outside its conversation.

Every send that bypasses the live session (the host's push script, cron
routines, background-job start/end notices, relays) appends one row to the
outbound ledger (`.telegram_bot/outbound-ledger.jsonl`, the DGN-1528
contract; notice_spool.append_ledger is the bridge-side writer). The session
never sees those bubbles, so an owner's short reply to one ("commit") arrived
with no context and the agent had to ask what it meant.

This module is the ONE reader of that ledger. On each owner turn the bridge
prefixes the owner's text with a bounded block:

  * the pushes sent to THIS chat since the agent's previous turn ended
    (newest last, at most MAX_PUSHES, each cut at MAX_PUSH_CHARS with an
    ellipsis), as the plain text the owner saw;
  * when the owner replied (Telegram reply-to) to a bubble from this bot,
    that bubble's full text verbatim as the message being answered;
  * when the newest such push ends in a question and the owner's reply is
    short, "The user is answering: <question>".

No push and no reply-to -> nothing is added; the owner's text is sent as is.

The block is fenced by OPEN_MARK / CLOSE_MARK so transcript readers can cut
it off again (owner_region below): the pushes are the agent's words, never
the owner's. Readers outside the bridge load this file by path, so it is
stdlib-only and imports nothing from the bridge package.
"""

import datetime
import hashlib
import html
import json
import os
import re
import tempfile
import time
import unicodedata
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

LEDGER_FILENAME = "outbound-ledger.jsonl"
STATE_FILENAME = "outbound-context.json"

OPEN_MARK = "[bridge:outbound-context]"
CLOSE_MARK = "[/bridge:outbound-context]"

MAX_PUSHES = 5
MAX_PUSH_CHARS = 600
ELLIPSIS = "…"
# With no recorded turn end for a chat (first turn after install / state
# lost), look back this far at most.
DEFAULT_LOOKBACK_S = 12 * 3600
# A reply this short (stripped chars) to a push that asked something is
# read as the answer to it.
SHORT_REPLY_CHARS = 40

HEADER = (
    "Messages delivered to this chat outside the model conversation since "
    "the last turn, oldest first and newest last. Each item names its "
    "provenance. Text is a transport record, verbatim as the user saw it; "
    "do not rewrite it, claim that you authored it, or infer writes beyond "
    "the recorded effect. A newer keyboard/service item is later state, not "
    "prose to append after. Long items are cut with …."
)
OMITTED = "({n} earlier message(s) omitted)"
ITEM_HEAD = "--- sent {when} ---"
ITEM_META = "provenance={provenance}{effect}"
REPLY_HEAD = (
    "The user is replying to this message you sent outside this "
    "conversation (full text):"
)
PLAIN_REPLY_HEAD = "The user is replying to this message you sent (full text):"
ANSWERING = "The user is answering: {question}"

_TAG = re.compile(r"<[^>]+>")
_QUESTION_MARKS = ("?", "？")
# Interrogative endings of Korean prose (owner-language pushes often ask
# without a question mark): "...can I commit as is" style endings.
_QUESTION_ENDING = re.compile(
    r"(?:까요?|나요|가요|는가|인가"
    r"|냐|는지|을지|될지|할지|건지)$"
)
_TRAILING = " \t.!~…)]\"'”’"


# ---- the ledger reader ------------------------------------------------------

def ledger_paths(bot_data_dir: Any, extra: Sequence[Any] = ()) -> List[str]:
    """The ledger under `bot_data_dir`, plus any extra data dirs a host keeps
    (deduplicated by real path, order kept)."""
    out: List[str] = []
    for d in [bot_data_dir, *extra]:
        if not d:
            continue
        p = os.path.realpath(os.path.join(str(d), LEDGER_FILENAME))
        if p not in out:
            out.append(p)
    return out


def iter_ledger_rows(paths: Iterable[str]) -> Iterator[Dict[str, Any]]:
    """Every well-formed row (a dict with a str body), file order. Missing or
    unreadable files and torn lines are skipped: the ledger is append-only
    and fail-soft on the write side, so it is read the same way."""
    for path in paths:
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("body"), str):
                    yield row


def parse_ts(raw: Any) -> Optional[float]:
    """Epoch seconds of a ledger ts ("2026-10-08T09:47:00Z"), or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def plain_text(body: str) -> str:
    """A ledger body as the owner saw it: sends are HTML, so tags go and
    entities are decoded."""
    return html.unescape(_TAG.sub("", body)).strip()


def _same_chat(row: Dict[str, Any], chat_id: Any) -> bool:
    return str(row.get("chat_id", "")).strip() == str(chat_id).strip()


def pushes_since(paths: Iterable[str], chat_id: Any, since: float,
                 until: Optional[float] = None) -> List[Dict[str, Any]]:
    """Rows sent to `chat_id` after `since` (and not after `until`), oldest
    first. Edit rows re-render a bubble already counted and are skipped."""
    out = []
    for row in iter_ledger_rows(paths):
        if row.get("tag") == "edit" or not _same_chat(row, chat_id):
            continue
        ts = parse_ts(row.get("ts"))
        if ts is None or ts <= since or (until is not None and ts > until):
            continue
        if not plain_text(row["body"]):
            continue
        out.append(dict(row, _ts=ts))
    out.sort(key=lambda r: r["_ts"])
    return out


def keyboard_bubbles(paths: Iterable[str], chat_id: Any) -> Dict[int, List[str]]:
    """DGN-2062: message id -> button texts of every opt: keyboard the push
    path sent to `chat_id` (push.sh records it as the row's "keyboard").
    File order, so a later edit row wins; an edit that dropped the keyboard
    maps to []. Rows without the field are not menus and are skipped."""
    out: Dict[int, List[str]] = {}
    for row in iter_ledger_rows(paths):
        kb = row.get("keyboard")
        if not isinstance(kb, list) or not _same_chat(row, chat_id):
            continue
        try:
            mid = int(str(row.get("message_id", "")).strip())
        except ValueError:
            continue
        out[mid] = [b for b in kb if isinstance(b, str)]
    return out


def keyboard_meta(paths: Iterable[str], chat_id: Any) -> Dict[int, Dict[str, Any]]:
    """DGN-2331: message id -> {"audience", "ts"} (epoch seconds or None) of
    every keyboard-bearing push to `chat_id`. The first send row decides; an
    edit row re-renders the same bubble and never changes either."""
    out: Dict[int, Dict[str, Any]] = {}
    for row in iter_ledger_rows(paths):
        if row.get("tag") == "edit" or not isinstance(row.get("keyboard"), list):
            continue
        if not _same_chat(row, chat_id):
            continue
        try:
            mid = int(str(row.get("message_id", "")).strip())
        except ValueError:
            continue
        out.setdefault(mid, {"audience": row.get("audience") or "",
                             "ts": parse_ts(row.get("ts"))})
    return out


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFC", text).split())


def norm_sha(text: Any, keep_space: bool = False) -> Optional[str]:
    """sha256 of NFC `text` with every whitespace run removed (the plain
    text Telegram returns differs from the sent markup in spacing only);
    keep_space=True hashes the NFC text as is. None for a non-string.
    The bridge's one text hash (bubble and inbound prompt shas)."""
    if not isinstance(text, str):
        return None
    norm = unicodedata.normalize("NFC", text) if keep_space else _norm(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def find_push(paths: Iterable[str], chat_id: Any, message_id: Any,
              text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The ledger row of the bubble the owner replied to: by message id when
    the row carries one, else by its text (whitespace-insensitive -- the
    plain text Telegram returns differs from the sent markup in spacing)."""
    want = _norm(text) if isinstance(text, str) and text.strip() else None
    by_text = None
    for row in iter_ledger_rows(paths):
        if not _same_chat(row, chat_id):
            continue
        mid = row.get("message_id")
        if message_id is not None and mid not in (None, "") and str(mid) == str(message_id):
            return row
        if want and _norm(plain_text(row["body"])) == want:
            by_text = row
    return by_text


# ---- questions --------------------------------------------------------------

def question_of(text: str) -> Optional[str]:
    """The question a message ends on (its last non-empty line), or None."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    core = last.rstrip(_TRAILING)
    if last.rstrip(" \t").endswith(_QUESTION_MARKS) or core.endswith(_QUESTION_MARKS):
        return last
    if _QUESTION_ENDING.search(unicodedata.normalize("NFC", core)):
        return last
    return None


# ---- per-chat turn-end watermark ---------------------------------------------

def state_path(bot_data_dir: Any) -> str:
    return os.path.join(str(bot_data_dir), "state", STATE_FILENAME)


def _load_state(bot_data_dir: Any) -> Dict[str, Any]:
    try:
        with open(state_path(bot_data_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_turn_end(bot_data_dir: Any, chat_id: Any) -> Optional[float]:
    v = _load_state(bot_data_dir).get(str(chat_id))
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def mark_turn_end(bot_data_dir: Any, chat_id: Any, ts: Optional[float] = None) -> None:
    """Record that the agent's turn in `chat_id` ended now (fail-soft: a lost
    mark only widens the next turn's window, bounded by MAX_PUSHES)."""
    if chat_id is None:
        return
    try:
        path = state_path(bot_data_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = _load_state(bot_data_dir)
        data[str(chat_id)] = float(time.time() if ts is None else ts)
        atomic_write_json(path, data)
    except Exception:
        pass


def atomic_write_json(path: Any, data: Any) -> None:
    """Write `data` as JSON to `path` atomically (temp file in the same
    directory, mode 0600, then rename). The bridge's one atomic JSON
    writer. Raises on failure; the temp file never survives one."""
    path = str(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the block ----------------------------------------------------------------

def _cut(text: str, limit: int = MAX_PUSH_CHARS) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + ELLIPSIS


def _when(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _row_provenance(row: Dict[str, Any]) -> str:
    """A stable, non-model ownership label for an outbound ledger row.

    Old rows predate explicit provenance.  They remain ``unknown`` rather
    than being credited to the model: that false attribution made recovered
    service cards look like draft assistant prose.
    """
    producer = row.get("producer")
    source = row.get("source")
    if isinstance(producer, str) and producer.strip():
        return "bridge-service:" + producer.strip()
    if source in ("bridge", "framework", "service"):
        return "bridge-service"
    if source == "model":
        return "model"
    if isinstance(source, str) and source.strip():
        return "external:" + source.strip()
    return "unknown"


def _row_effect(row: Dict[str, Any]) -> str:
    """Render only writer-supplied effect evidence, never prose guesses."""
    effect = row.get("effect")
    return "; effect=" + effect.strip() if isinstance(effect, str) and effect.strip() else ""


def build_block(pushes: Sequence[Dict[str, Any]], reply_text: Optional[str] = None,
                answering: Optional[str] = None, reply_head: str = REPLY_HEAD) -> str:
    """The fenced context block, or "" when there is nothing to say."""
    shown = list(pushes)[-MAX_PUSHES:]
    if not shown and not reply_text and not answering:
        return ""
    lines = [OPEN_MARK]
    if shown:
        lines.append(HEADER)
        if len(pushes) > len(shown):
            lines.append(OMITTED.format(n=len(pushes) - len(shown)))
        for row in shown:
            lines.append(ITEM_HEAD.format(when=_when(row["_ts"])))
            lines.append(ITEM_META.format(
                provenance=_row_provenance(row), effect=_row_effect(row)))
            lines.append(_cut(plain_text(row["body"])))
    if reply_text:
        lines.append(reply_head)
        lines.append(reply_text)
    if answering:
        lines.append(ANSWERING.format(question=answering))
    lines.append(CLOSE_MARK)
    return "\n".join(lines)


def compose(owner_text: str, chat_id: Any, bot_data_dir: Any,
            reply_to: Optional[Dict[str, Any]] = None,
            now: Optional[float] = None,
            extra_dirs: Sequence[Any] = ()) -> str:
    """The text to send for this owner turn: the block + owner_text, or
    owner_text unchanged when nothing applies.

    reply_to: {"message_id", "text", "from_bot"} of the bubble the owner
    replied to (None when not a reply). This bot's quoted text rides verbatim
    even without a ledger row. A "tapped": True key marks a button tap and
    preserves its existing context heading. Explicit replies never infer an
    answer to an unrelated recent push."""
    now = time.time() if now is None else now
    paths = ledger_paths(bot_data_dir, extra_dirs)
    since = last_turn_end(bot_data_dir, chat_id)
    floor = now - DEFAULT_LOOKBACK_S
    since = floor if since is None or since < floor else since
    pushes = pushes_since(paths, chat_id, since, now)

    reply_text = None
    reply_head = REPLY_HEAD
    if reply_to and reply_to.get("from_bot"):
        shown = reply_to.get("text")
        row = find_push(paths, chat_id, reply_to.get("message_id"), shown)
        if isinstance(shown, str) and shown.strip():
            reply_text = shown
            if row is None and not reply_to.get("tapped"):
                reply_head = PLAIN_REPLY_HEAD
        elif row is not None:
            reply_text = plain_text(row["body"])

    answering = None
    if reply_text:
        answering = question_of(reply_text)
    elif reply_to is None and pushes and len((owner_text or "").strip()) <= SHORT_REPLY_CHARS:
        for row in reversed(pushes):
            q = question_of(plain_text(row["body"]))
            if q:
                answering = q
                break

    block = build_block(pushes, reply_text, answering, reply_head=reply_head)
    return block + "\n" + owner_text if block else owner_text


def owner_region(text: str) -> Tuple[int, int]:
    """(start, end) of the owner's own words in a turn text: everything after
    the block when the turn opens with one, else the whole text. rfind, so a
    pushed body that happens to carry CLOSE_MARK cannot end the block early
    (an owner typing it only shrinks their own region)."""
    if not text.startswith(OPEN_MARK):
        return 0, len(text)
    k = text.rfind(CLOSE_MARK)
    if k < 0:
        return len(text), len(text)
    start = k + len(CLOSE_MARK)
    if text[start:start + 1] == "\n":
        start += 1
    return start, len(text)


def strip_block(text: str) -> str:
    lo, hi = owner_region(text)
    return text[lo:hi]
