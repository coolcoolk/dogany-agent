#!/usr/bin/env python3
"""
__AGENT_LABEL__ long-term memory recall core (memory.py)

- Source of truth: ../memories/*.md (markdown)
- state.db: an index that can be rebuilt anytime with `index` (FTS5 trigram + bge-m3 embedding BLOB)
- Hybrid search: fuse FTS5 keyword ranking + vector cosine ranking via RRF

Subcommands: index / search / stats
Embedding: local Ollama bge-m3 (http://localhost:11434/api/embeddings)
Dependencies: standard library only (pure-python cosine, no numpy needed).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request

# Some engine consumers load this file directly with importlib rather than as a
# package.  Make sibling engine modules resolvable in that supported mode.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from archive_engine import (ArchiveError, MemoryArchiveEngine, stable_item_id,
                            transcript_identity)

# ---- path constants ----
# MEMORIES_DIR: env seam for testing (set MEMORIES_DIR=/tmp/... to use a scratch directory).
# Production: always uses the default path derived from HERE.
MEMORIES_DIR = os.environ.get(
    "MEMORIES_DIR",
    os.path.normpath(os.path.join(HERE, "..", "memories"))
)
DB_PATH = os.path.join(HERE, "state.db")
def _default_data_root():
    """Instance data root. The shipped template tree must never grow state:
    run from agents/.template the default is a per-user scratch directory."""
    if os.path.basename(os.path.dirname(HERE)) == ".template":
        import tempfile
        return os.path.join(tempfile.gettempdir(),
                            "dogany-template-memory-engine-%d" % os.getuid())
    return os.path.normpath(os.path.join(HERE, "..", "data", "memory-engine"))


MEMORY_DATA_ROOT = os.environ.get("MEMORY_DATA_ROOT") or _default_data_root()
MEMORY_INSTANCE_ID = os.environ.get("MEMORY_INSTANCE_ID", "default")


def memory_archive_engine():
    """Construct the per-instance archive API used by CLI and FDGN-620 tests."""
    clock = None
    fixed = os.environ.get("MEMORY_ARCHIVE_CLOCK")
    if fixed:
        clock = lambda: fixed  # noqa: E731 -- deterministic replay comparison
    return MemoryArchiveEngine(MEMORIES_DIR, MEMORY_DATA_ROOT, MEMORY_INSTANCE_ID, clock)


def recover_memory_transactions():
    """Universal read preamble; idempotent and creates nothing when clean."""
    engine = memory_archive_engine()
    if not engine.needs_recovery():
        return False
    with engine.transaction():
        return True

# Files excluded from search indexing: anything already hot (in the AGENTS.md
# @-chain) is always in context, so indexing it is redundant. Both spellings of
# the owner-facts file are listed -- the 2.0 layout renamed USER.md ->
# identity/hot.custom.owner.md (DGN-1141) and a pre-relayout instance still
# carries the old name. Scope note: this set is matched against basenames under
# MEMORIES_DIR, and neither name is written there by any code path today, so it
# is a guard rather than an active filter. (2026-06-25; renamed 2026-08-28)
INDEX_EXCLUDE = {"USER.md", "hot.custom.owner.md"}

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024  # bge-m3 output dimension

RRF_K = 60          # standard RRF constant
MISS_THRESHOLD = 0.30  # top1 cosine below this counts as a "miss" (stats)
TRANSCRIPT_TOPK = 3            # (rename: TRANSCRIPT_FTS_TOPK) search_core splice cap
HOOK_TODAY_TOPK = 2            # max same-day snippets injected by the hook (dec-089 policy1)
TRANSCRIPT_KW_MIN_LEN_KO = 2   # min length for a token containing Korean chars (FATAL2)
TRANSCRIPT_ECHO_SEC = 60       # self-echo belt: exclude __USER_LABEL__ rows within now-60s
TRANSCRIPT_MTIME_SKEW_SEC = 120  # mtime skip safety margin (F4)
TRANSCRIPT_MAX_AGE_H = 72      # failsafe belt: only fires after 3 consecutive consolidate misses (MAJOR3)
TODAY_CHAR_CAP = 8000          # today output total char cap (MINOR)
TRANSCRIPT_NOISE_MARKERS = (   # MAJOR1: prefix-match on row text -> excluded from ingest (measured samples)
    "<task-notification",      # subagent completion notice (measured 190)
    "[cron-inject]",           # cron inject prompt (measured 31)
    "[DGN-",                   # continuous-work loop machine prompt (measured 12/12 machine)
    "Stop hook feedback:",     # footer hook feedback row (measured in current transcript_notes)
    "<command-name",
    "<local-command",
    "[관련 기억",              # DGN-848: recalled-memory block header (prefix guard, both lanes)
    "[기억규칙]",              # DGN-848: recalled-memory precedence frame lines (prefix guard)
    "[구조화상태]",            # DGN-1176: ledger-preload sentinel (every block line)
    "[same-day answer by this agent",  # DGN-1949: same-day answer block header
    "[현재 신체/목표]",        # DGN-1176 (grill minor#1): legacy body-state fallback line --
                               # registered independently of registry adoption
    "[turn frame]",            # DGN-2028: per-turn problem frame lines (every line)
)


# ======================================================================
# F6: secret redaction (security must-fix)
# ======================================================================
# A pasted secret must never persist. It would otherwise flow into
# transcript_notes (same-day FTS -> recalled + re-injected by the hook), into
# the _raw gzip archive, and into consolidate's permanent memory file. Redact
# high-risk secret patterns at every choke point before any write. Cheap:
# compiled once at import; run over short message/note strings only.
_SECRET_PATTERNS = [
    # PEM private key blocks (DOTALL: spans lines). Match first (widest).
    re.compile(r"-----BEGIN[^-]+PRIVATE KEY-----.*?-----END[^-]+-----", re.DOTALL),
    # JWT: header.payload.signature
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # OpenAI-style keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Google API keys
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    # Slack tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    # Telegram bot token: <digits>:<35+ token chars>
    re.compile(r"\d{6,}:[A-Za-z0-9_\-]{30,}"),
]
# key=value / key: value style secrets. Keep the key label, redact the value.
_SECRET_KV_RE = re.compile(
    r"(?i)(password|passwd|api[_-]?key|secret|token)(\s*[:=]\s*)(\S+)"
)


def redact_secrets(text):
    """Replace high-risk secret patterns in text with [REDACTED].
    Applied at every persistence choke point (transcript FTS ingest, raw archive
    write, consolidate append) so a pasted secret never lands on disk or in the
    same-day recall index. Non-secret prose is left untouched. Cheap: compiled
    regexes over short strings. Returns text unchanged when no pattern hits."""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    # key=value: keep the key + separator, redact only the value.
    text = _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)
    return text


def _parse_iso_utc(ts_iso):
    """Parse a stored UTC ISO8601 timestamp ('...Z') into a tz-aware datetime.
    macOS system python3.9 fromisoformat does not accept a trailing 'Z', so
    substitute '+00:00' first; fall back to strptime with an explicit UTC tz."""
    try:
        return datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.datetime.strptime(
            ts_iso, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=datetime.timezone.utc)


# ======================================================================
# M5: explicit chunking-format marker (human co-edit safety)
# ======================================================================
# parse_markdown / append_notes_to_md previously decided bullet-mode vs
# section(§)-mode purely by whether ANY '§' line existed. A human adding or
# removing one § then silently flips the whole file's chunking mode and the next
# machine write uses the wrong mode. Fix: an explicit marker the code trusts
# first; fall back to the §-sniff only when the marker is absent (backward
# compatible). The engine writes the marker whenever it creates/rewrites a file.
_FORMAT_MARKER_RE = re.compile(
    r"<!--\s*dogany-format:\s*(section|bullet)\s*-->", re.IGNORECASE
)


def _format_marker(text):
    """Return 'section' | 'bullet' if an explicit dogany-format marker is present
    in text, else None (caller falls back to the § sniff)."""
    if not text:
        return None
    m = _FORMAT_MARKER_RE.search(text)
    return m.group(1).lower() if m else None


def _marker_line(mode):
    """The HTML-comment marker line for a given mode ('section'|'bullet')."""
    return f"<!-- dogany-format: {mode} -->"


# ======================================================================
# markdown parsing
# ======================================================================
def _is_header(line):
    """Whether the line is a markdown header (#, ##, ###...)."""
    return bool(re.match(r"^#{1,6}\s+\S", line.strip()))


def _header_text(line):
    """Text of a header line with the # removed."""
    return re.sub(r"^#{1,6}\s+", "", line.strip()).strip()


def _clean(text):
    """Clean note body: strip surrounding whitespace, drop empty header-only."""
    return text.strip()


# Box-drawing / shape characters (ASCII diagram noise). Removed only at search-index time.
_BOX_CHARS = set(
    "─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣"
    "┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋"
    "═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬"
    "▀▁▂▃▄▅▆▇█▉▊▋▌▍▎▏▐░▒▓▔▕▖▗▘▙▚▛▜▝▞▟"
)


def _box_ratio(line):
    """Fraction of (box-drawing + space) characters in the line. 0 if empty."""
    stripped = line.strip()
    if not stripped:
        return 0.0
    box = sum(1 for ch in stripped if ch in _BOX_CHARS)
    return box / len(stripped)


def denoise_for_index(text):
    """
    Noise cleanup applied only at index time. Never touches the original .md file.
    - Remove entire code-fence (```) blocks.
    - Remove lines where box-drawing characters exceed 50% (ASCII box diagrams).
    Lines mixed with text explanation are kept (only the box part goes).
    Returns: cleaned text (may be empty -> caller decides to skip).
    """
    out = []
    in_fence = False
    for ln in text.splitlines():
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue  # drop the fence line itself too
        if in_fence:
            continue  # remove the entire inside of the code fence
        if _box_ratio(ln) > 0.5:
            continue  # remove lines over 50% box characters
        out.append(ln)
    return "\n".join(out).strip()


def parse_markdown(path):
    """
    Parse one md file into a list of notes.
    Returns: [{"section": str, "text": str}, ...]

    Rules:
    - Use the most recent markdown header (any level) as the section for the notes that follow it.
    - If the file has a '§' separator: split the body (excluding header lines) on '§', each block a note.
    - If no '§' (fallback): treat the span between headers as one candidate, but
      split it further into blank-line paragraphs if too long.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print(f"  [warn] read failed {path}: {e}", file=sys.stderr)
        return []

    if not raw.strip():
        return []  # guard against empty file

    lines = raw.splitlines()
    # M5: an explicit format marker wins over the § sniff. A human toggling one
    # § no longer flips the whole file's chunking mode. Marker absent -> sniff.
    marker = _format_marker(raw)
    if marker is not None:
        has_sep = (marker == "section")
    else:
        has_sep = any(ln.strip() == "§" for ln in lines)
    # The marker comment itself is metadata, not a note -> drop it from parsing.
    lines = [ln for ln in lines if not _FORMAT_MARKER_RE.search(ln)]

    notes = []
    current_section = ""

    if has_sep:
        # §-based parsing: headers update the section, the rest is split into blocks by §
        buf = []

        def flush():
            txt = _clean("\n".join(buf))
            if txt and txt != "§":
                notes.append({"section": current_section, "text": txt})
            buf.clear()

        for ln in lines:
            s = ln.strip()
            if s == "§":
                flush()
            elif _is_header(ln):
                # on a header, close the in-progress block then update the section
                flush()
                current_section = _header_text(ln)
            else:
                buf.append(ln)
        flush()
    else:
        # fallback: header span -> split into paragraphs
        section_buf = []

        def flush_section():
            if not section_buf:
                return
            lines_in = section_buf[:]
            section_buf.clear()
            # Bullet-based chunking: a top-level bullet ("- ") = a chunk boundary. Nested indent / plain text / blank lines are absorbed into the current chunk.
            # If there are no bullets, fall back to the old blank-line paragraph split.
            if any(re.match(r"^- ", ln) for ln in lines_in):
                cur = []

                def push():
                    t = _clean("\n".join(cur))
                    if t:
                        notes.append({"section": current_section, "text": t})
                    cur.clear()

                for ln in lines_in:
                    if re.match(r"^- ", ln):
                        push()
                    cur.append(ln)
                push()
            else:
                block = _clean("\n".join(lines_in))
                if block:
                    paras = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
                    for p in paras:
                        notes.append({"section": current_section, "text": p})

        for ln in lines:
            if _is_header(ln):
                flush_section()
                current_section = _header_text(ln)
            else:
                section_buf.append(ln)
        flush_section()

    return notes


# ======================================================================
# embedding (Ollama bge-m3)
# ======================================================================
def embed(text):
    """
    Request one bge-m3 embedding. Raises on failure (handled by caller).
    Returns: list[float] of length EMBED_DIM
    """
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if not vec or not isinstance(vec, list):
        raise ValueError("Ollama response missing embedding field")
    return vec


def vec_to_blob(vec):
    """float list -> float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vec(blob):
    """float32 BLOB -> float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a, b):
    """Pure-python cosine similarity. (Fine at the scale of a few hundred notes.)"""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ======================================================================
# DB schema
# ======================================================================
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,   -- file path (relative)
            sha256      TEXT NOT NULL,       -- content hash (for incremental)
            indexed_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            section     TEXT,
            text        TEXT NOT NULL,
            embedding   BLOB                 -- float32 BLOB (may be NULL when Ollama is down)
        );

        CREATE INDEX IF NOT EXISTS idx_notes_file ON notes(source_file);

        -- FTS5 trigram: good for Korean partial matching
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
        USING fts5(text, content='notes', content_rowid='id', tokenize='trigram');

        CREATE TABLE IF NOT EXISTS search_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            query      TEXT NOT NULL,
            n_results  INTEGER NOT NULL,
            top1_score REAL,
            ts         TEXT NOT NULL
        );

        -- Nightly consolidate watermark: records how far conversations were processed.
        -- key='last_ts' stores the ISO8601 timestamp of the last processed message.
        -- Weekly inbox classification (classify-inbox) success marker: key='classify_inbox_last'.
        CREATE TABLE IF NOT EXISTS consolidation_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- DGN-153: consolidate partial-failure retry queue. A chunk whose
        -- first-pass compression fails is preserved here (instead of being
        -- silently dropped while the watermark advances) and retried on the
        -- next consolidate run. retry_count counts failed RETRY attempts
        -- (0 = queued, not yet retried); at CONSOLIDATE_RETRY_MAX the chunk
        -- moves to the dead-letter file (never deleted).
        CREATE TABLE IF NOT EXISTS consolidate_retry_queue (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_text   TEXT NOT NULL,
            first_failed TEXT NOT NULL,   -- ISO8601 of the run that queued it
            retry_count  INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT
        );

        -- Same-day rolling index: mechanically loads today's raw conversations not yet consolidated.
        -- Zero LLM. After the dawn distillation turns them into real notes, that day's rows are pruned (end of consolidate).
        CREATE TABLE IF NOT EXISTS transcript_notes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,   -- message ISO8601 timestamp
            role    TEXT,            -- __USER_LABEL__ / __AGENT_LABEL__
            session TEXT,            -- source jsonl stem (for session distinction / debug)
            text    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tnotes_ts ON transcript_notes(ts);

        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts
        USING fts5(text, content='transcript_notes', content_rowid='id', tokenize='trigram');
        """
    )
    conn.commit()

    # MAJOR4: idempotent UNIQUE index for same-day ingest dedup (guards the
    # concurrent-hook watermark race). Wrapped separately from the executescript
    # above: if a pre-existing DB already holds race-remnant duplicates, the
    # CREATE fails with OperationalError -> reset the same-day cache once and
    # retry. The cache is fully re-loaded from today's jsonl (from local midnight)
    # so this is lossless; consolidate's own watermark (last_ts) is a separate
    # key and unaffected. This fallback keeps the whole path idempotent -- no
    # manual migration step.
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tnotes_dedup "
            "ON transcript_notes(session, ts, role)"
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Spec section 2 names OperationalError, but SQLite raises IntegrityError
        # for a UNIQUE-constraint violation on pre-existing race-remnant dups.
        # Catch the common base (sqlite3.Error) so the reset-and-retry fallback
        # fires for either -- the spec's intent is the fallback, not the label.
        conn.rollback()
        # Reset the same-day cache. transcript_fts is external-content and is no
        # longer written by index_transcript_notes (section 4-3), so it drifts from
        # transcript_notes; a plain `DELETE FROM transcript_fts` then raises
        # "database disk image is malformed". Use the FTS5 'delete-all' command,
        # which clears the index without reconciling against content rows.
        conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('delete-all')")
        conn.execute("DELETE FROM transcript_notes")
        conn.execute("DELETE FROM consolidation_state WHERE key='ts_fts_last'")
        conn.commit()
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tnotes_dedup "
            "ON transcript_notes(session, ts, role)"
        )
        conn.commit()

    sync_transcript_archives(conn)


def _transcript_exclusions(state):
    return {transcript_identity(row) for batch in state["archives"]
            if batch["state"] == "archived" for row in batch["rows"]}


def _sync_transcript_archives(conn, state):
    """Apply durable archive state to a disposable cache under the archive lock.

    Restores are applied once per DB, so ordinary retention still works. A
    fresh DB can recover exact restored rows even when raw sources aged out.
    FTS is rebuilt, not row-deleted: legacy external-content indexes can drift.
    """
    if not state["archives"]:
        return set()
    excluded = _transcript_exclusions(state)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS transcript_archive_applied "
                     "(archive_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        changed = False
        for row in conn.execute("SELECT * FROM transcript_notes").fetchall():
            if transcript_identity(row) in excluded:
                conn.execute("DELETE FROM transcript_notes WHERE id=?", (row["id"],))
                changed = True
        for batch in state["archives"]:
            applied = conn.execute(
                "SELECT state FROM transcript_archive_applied WHERE archive_id=?",
                (batch["archive_id"],)).fetchone()
            # Also clear stale FTS shadow entries when the content cache was
            # cleared between the durable commit and this reconciliation.
            if not applied or applied[0] != batch["state"]:
                changed = True
            if batch["state"] == "restored" and (not applied or applied[0] != "restored"):
                for row in batch["rows"]:
                    if transcript_identity(row) in excluded:
                        continue
                    existing = conn.execute(
                        "SELECT * FROM transcript_notes WHERE session IS ? AND ts=? AND role IS ?",
                        (row["session"], row["ts"], row["role"])).fetchone()
                    if existing:
                        if existing["text"] != row["text"]:
                            raise ArchiveError("transcript restore conflicts with an active row")
                        continue
                    # Preserve the old rowid when available; rebuilds may reuse it.
                    row_id = row["id"] if not conn.execute(
                        "SELECT 1 FROM transcript_notes WHERE id=?", (row["id"],)).fetchone() else None
                    conn.execute("INSERT INTO transcript_notes(id, ts, role, session, text) "
                                 "VALUES(?,?,?,?,?)", (row_id, row["ts"], row["role"],
                                                     row["session"], row["text"]))
                    changed = True
            conn.execute("INSERT OR REPLACE INTO transcript_archive_applied VALUES(?,?)",
                         (batch["archive_id"], batch["state"]))
        if changed:
            conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
    return excluded


def sync_transcript_archives(conn):
    engine = memory_archive_engine()
    if not os.path.isfile(engine.transcripts) and not engine.needs_recovery():
        return
    with engine.transaction():
        _sync_transcript_archives(conn, engine.transcript_state())


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ======================================================================
# index command
# ======================================================================
# DGN-1873: sqlite busy wait for the index writer (ms, env-overridable for tests).
INDEX_BUSY_TIMEOUT_MS = int(os.environ.get("MEMORY_INDEX_BUSY_TIMEOUT_MS", "30000"))
# The lock file the hook's background index takes (_hook_maybe_reindex).
REINDEX_LOCK_PATH = os.path.join(HERE, ".reindex.lock")
# Post-classify reindex: max seconds to wait for that lock, then the pauses
# (seconds) between attempts on a busy database. Env-overridable for tests.
REINDEX_LOCK_WAIT_SEC = float(os.environ.get("MEMORY_REINDEX_LOCK_WAIT_SEC", "120"))
REINDEX_RETRY_DELAYS = tuple(
    float(x) for x in os.environ.get("MEMORY_REINDEX_RETRY_DELAYS", "10,30").split(",") if x
)


def _sqlite_is_busy(exc):
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _reindex_locked_with_retry(lock_path=None):
    """Incremental reindex for callers that just rewrote topic files (classify-inbox).
    Takes the same .reindex.lock the hook's background index uses, so the two never
    write concurrently; if the lock stays held past REINDEX_LOCK_WAIT_SEC it proceeds
    anyway (busy_timeout + retry still guard). A busy/locked database is retried
    after each REINDEX_RETRY_DELAYS pause; cmd_index is sha-gated and commits per
    file, so a retry resumes where the last attempt stopped.
    Returns True when the index is current, False when it is still stale."""
    lock_path = lock_path or REINDEX_LOCK_PATH
    held = False
    deadline = time.monotonic() + REINDEX_LOCK_WAIT_SEC
    while True:
        if _hook_acquire_lock(lock_path):
            held = True
            break
        if time.monotonic() >= deadline:
            print(f"[reindex] lock still held after {REINDEX_LOCK_WAIT_SEC:.0f}s -- proceeding "
                  "without it (busy_timeout + retry)", file=sys.stderr)
            break
        time.sleep(1)
    try:
        delays = (0,) + tuple(REINDEX_RETRY_DELAYS)
        for attempt, delay in enumerate(delays, 1):
            if delay:
                time.sleep(delay)
            try:
                return cmd_index(argparse.Namespace(lock=None)) == 0
            except sqlite3.Error as e:
                if not _sqlite_is_busy(e):
                    print(f"[reindex] failed: {e}", file=sys.stderr)
                    return False
                print(f"[reindex] attempt {attempt}/{len(delays)}: {e}", file=sys.stderr)
        return False
    finally:
        if held:
            try:
                os.remove(lock_path)
            except OSError:
                pass


def cmd_index(args):
    # --lock: grab a lock file at start and release it via atexit (prevents duplicate auto-reindex).
    #         The background index fired by _maybe_reindex on the hook path uses this lock.
    #         `memory.py index` invoked from a skill has no --lock and runs as usual.
    lock_path = getattr(args, "lock", None)
    if lock_path:
        import atexit

        def _release_lock():
            try:
                os.remove(lock_path)
            except OSError:
                pass
        atexit.register(_release_lock)

    if not os.path.isdir(MEMORIES_DIR):
        print(f"[error] memories directory not found: {MEMORIES_DIR}", file=sys.stderr)
        return 1

    conn = connect()
    # DGN-1873: wait out a transient writer (the hook's background index, the
    # same-day transcript loader) instead of failing on the first
    # "database is locked".
    conn.execute(f"PRAGMA busy_timeout={INDEX_BUSY_TIMEOUT_MS}")
    init_db(conn)

    md_files = sorted(
        f for f in os.listdir(MEMORIES_DIR)
        if f.endswith(".md") and f not in INDEX_EXCLUDE
    )
    if not md_files:
        print("[warn] no .md files found")
        return 0

    # load existing hashes
    existing = {row["path"]: row["sha256"] for row in conn.execute("SELECT path, sha256 FROM files")}

    # Orphan cleanup: remove notes/FTS/files records for files that disappeared from disk
    # (file splits, merges, deletions). Incremental indexing only iterates present files,
    # so without this step deleted-file notes stay as DB orphans and show up as duplicates
    # in search (essential for file splits and nightly cleanup merges/splits).
    disk_set = set(md_files)
    tracked = set(existing) | {
        r["source_file"] for r in conn.execute("SELECT DISTINCT source_file FROM notes")
    }
    for fname in sorted(tracked - disk_set):
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE source_file=?", (fname,)
        )]
        for nid in old_ids:
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
        conn.execute("DELETE FROM notes WHERE source_file=?", (fname,))
        conn.execute("DELETE FROM files WHERE path=?", (fname,))
        reason = "excluded from index" if fname in INDEX_EXCLUDE else "removed from disk"
        print(f"  - {fname}: {reason} -> {len(old_ids)} notes cleaned up")
    conn.commit()

    total_notes = 0
    embed_calls = 0
    skipped_files = 0
    ollama_down = False

    for fname in md_files:
        fpath = os.path.join(MEMORIES_DIR, fname)
        sha = file_sha256(fpath)

        if existing.get(fname) == sha:
            # unchanged -> skip re-embedding/re-parsing
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM notes WHERE source_file=?", (fname,)
            ).fetchone()["c"]
            total_notes += cnt
            skipped_files += 1
            print(f"  = {fname}: unchanged, skipped ({cnt} notes)")
            continue

        # changed -> delete all notes for this file and reload
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notes WHERE source_file=?", (fname,)
        )]
        for nid in old_ids:
            conn.execute("DELETE FROM notes_fts WHERE rowid=?", (nid,))
        conn.execute("DELETE FROM notes WHERE source_file=?", (fname,))

        notes = parse_markdown(fpath)
        # Denoise for indexing (preserves original): strip code fences and box diagrams.
        # Notes that become empty after denoising (box-only notes) are skipped.
        cleaned = []
        for note in notes:
            ctext = denoise_for_index(note["text"])
            if not ctext:
                continue
            cleaned.append({"section": note["section"], "text": ctext})
        dropped = len(notes) - len(cleaned)
        notes = cleaned
        if dropped:
            print(f"    [denoise] {fname}: {dropped} box/fence notes skipped")
        if not notes:
            print(f"  ! {fname}: 0 notes (empty file or no separator)")
            # update the hash so we don't re-parse this file next time
            conn.execute(
                "INSERT OR REPLACE INTO files(path, sha256, indexed_at) VALUES(?,?,?)",
                (fname, sha, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            continue

        for note in notes:
            emb_blob = None
            try:
                vec = embed(note["text"])
                emb_blob = vec_to_blob(vec)
                embed_calls += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                # Ollama down or similar: store embedding as NULL (FTS search still works)
                ollama_down = True
                print(f"    [warn] embedding failed (text saved without vector): {e}", file=sys.stderr)

            cur = conn.execute(
                "INSERT INTO notes(source_file, section, text, embedding) VALUES(?,?,?,?)",
                (fname, note["section"], note["text"], emb_blob),
            )
            nid = cur.lastrowid
            conn.execute(
                "INSERT INTO notes_fts(rowid, text) VALUES(?,?)", (nid, note["text"])
            )
            total_notes += 1

        conn.execute(
            "INSERT OR REPLACE INTO files(path, sha256, indexed_at) VALUES(?,?,?)",
            (fname, sha, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        print(f"  + {fname}: {len(notes)} notes loaded")

    conn.commit()
    conn.close()

    print(f"\n[index done] total {total_notes} notes / {embed_calls} embedding calls / {skipped_files} files skipped (unchanged)")
    if ollama_down:
        print("[warning] some embeddings failed -- check Ollama and re-run index (touch the file or delete the db to force re-index)")
    return 0


# ======================================================================
# search command
# ======================================================================
def fts_search(conn, query, limit):
    """
    FTS5 trigram search. Returns a list of note ids sorted by bm25 ascending (lower = more relevant).
    Wrapping in double-quotes is safe for trigram phrase matching.
    """
    # escape special chars: wrap in double-quotes to handle as a phrase
    safe = '"' + query.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (safe, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def vector_search(conn, query, limit):
    """
    Cosine similarity: query embedding vs all note embeddings. Returns (id, score) list descending.
    Returns empty list when Ollama is down.
    """
    try:
        qvec = embed(query)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[warn] query embedding failed -- vector search skipped (FTS only): {e}", file=sys.stderr)
        return []

    scored = []
    for row in conn.execute("SELECT id, embedding FROM notes WHERE embedding IS NOT NULL"):
        vec = blob_to_vec(row["embedding"])
        scored.append((row["id"], cosine(qvec, vec)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def rrf_fuse(fts_ids, vec_scored, k=RRF_K):
    """
    Reciprocal Rank Fusion.
    For each list, rank r (0-based) contributes score 1/(k+r+1), accumulated per id.
    Returns: [(note_id, rrf_score, cos_score_or_None), ...] descending.
    """
    fused = {}
    cos_map = {nid: sc for nid, sc in vec_scored}

    for rank, nid in enumerate(fts_ids):
        fused[nid] = fused.get(nid, 0.0) + 1.0 / (k + rank + 1)
    for rank, (nid, _sc) in enumerate(vec_scored):
        fused[nid] = fused.get(nid, 0.0) + 1.0 / (k + rank + 1)

    out = [(nid, score, cos_map.get(nid)) for nid, score in fused.items()]
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def search_core(query, k=5, log=True):
    """Search core -- builds and returns the result list only (no stdout print).

    Shared by cmd_search (skill) and cmd_hook (auto-inject).
    Returns: [{"id","source_file","section","text","rrf_score","cosine"}, ...]
    log=True records one row to search_log (preserves skill behavior). Hook path uses log=False.
    """
    conn = connect()
    init_db(conn)

    # Same-day rolling index: incrementally load today's raw conversations just before searching
    # (mechanical, best effort). Conversations from other sessions the same day that are not yet
    # consolidated are still captured here and exposed in search results.
    index_transcript_notes(conn)

    # gather a generous candidate pool then fuse
    pool = max(k * 4, 20)
    fts_ids = fts_search(conn, query, pool)
    vec_scored = vector_search(conn, query, pool)

    fused = rrf_fuse(fts_ids, vec_scored)

    # load note metadata
    results = []
    for nid, rrf_score, cos_score in fused:
        if len(results) >= k:
            break
        row = conn.execute(
            "SELECT source_file, section, text FROM notes WHERE id=?", (nid,)
        ).fetchone()
        if not row:
            continue
        # DGN-2009: a [SUPERSEDED] line is never returned as a current fact.
        if is_superseded(row["text"]):
            continue
        results.append(
            {
                "id": nid,
                "source_file": row["source_file"],
                "section": row["section"],
                "text": row["text"],
                "rrf_score": round(rrf_score, 5),
                "cosine": round(cos_score, 4) if cos_score is not None else None,
                "kind": "note",
            }
        )

    # Append today's raw conversation matches as a separate source
    # (no embedding -> FTS bm25 only, cosine=None). Appended as a cap after note
    # results so as not to disturb note ranking via RRF.
    for tr in transcript_search(conn, query, TRANSCRIPT_TOPK):
        results.append(
            {
                "id": f"t{tr['id']}",
                "source_file": f"today-convo:{tr.get('session', '')[:8]}",
                "section": f"{tr.get('role', '')} {tr.get('ts', '')[:16]}",
                "text": tr["text"],
                "rrf_score": None,
                "cosine": None,
                "kind": "transcript",
            }
        )

    # DGN-1949: absence-claim gate scope. Today's answered exchanges of THIS
    # agent (question + answer) are searched on every skill search, so a
    # "not recorded" verdict is only ever reached after today was covered.
    for ex in same_day_answer_search(conn, query, SAME_DAY_TOPK):
        body = ex["answer"]
        if len(body) > SAME_DAY_ANSWER_MAX:
            body = body[:SAME_DAY_ANSWER_MAX] + "\u2026"
        if ex["question"]:
            body = f"Q: {ex['question'][:SAME_DAY_QUESTION_MAX]}\nA: {body}"
        results.append(
            {
                "id": f"a{ex['session'][:8]}-{ex['ts']}",
                "source_file": f"today-answer:{ex['session'][:8]}",
                "section": f"answer {_parse_iso_utc(ex['ts']).astimezone().strftime('%H:%M')}",
                "text": body,
                "rrf_score": None,
                "cosine": None,
                "kind": "today-answer",
            }
        )

    # DGN-2009: 0-hit -> this agent's own recent transcripts before any
    # absence claim. A hit is a phrase (FTS) match or a vector match at the
    # recall cutoff; a low-cosine nearest neighbour is not one.
    fts_set = set(fts_ids)
    hit = any(
        r["kind"] != "note"
        or r["id"] in fts_set
        or (r["cosine"] is not None and r["cosine"] >= HOOK_VEC_CUTOFF)
        for r in results
    )
    if not hit:
        for ex in recent_transcript_search(query, RECENT_TRANSCRIPT_TOPK):
            body = ex["answer"]
            if len(body) > SAME_DAY_ANSWER_MAX:
                body = body[:SAME_DAY_ANSWER_MAX] + "\u2026"
            if ex["question"]:
                body = f"Q: {ex['question'][:SAME_DAY_QUESTION_MAX]}\nA: {body}"
            when = _parse_iso_utc(ex["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
            results.append(
                {
                    "id": f"r{ex['session'][:8]}-{ex['ts']}",
                    "source_file": f"recent-convo:{ex['session'][:8]}",
                    "section": f"answer {when}",
                    "text": body,
                    "rrf_score": None,
                    "cosine": None,
                    "kind": "recent-transcript",
                }
            )

    if log:
        # log the search (top1 score is cosine-based; None when vector search did not run)
        top1 = results[0]["cosine"] if results and results[0]["cosine"] is not None else None
        conn.execute(
            "INSERT INTO search_log(query, n_results, top1_score, ts) VALUES(?,?,?,?)",
            (query, len(results), top1, datetime.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    conn.close()
    return results


def cmd_search(args):
    query = args.query
    k = args.k

    results = search_core(query, k=k, log=True)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print(f'"{query}" -- no results (searched: memories + today\'s conversations of this agent '
              f'+ its conversations of the last {RECENT_TRANSCRIPT_DAYS} days).')
        return 0

    print(f'🔎 "{query}" -- top {len(results)}\n')
    for i, r in enumerate(results, 1):
        cos = f"{r['cosine']:.3f}" if r["cosine"] is not None else "n/a"
        sect = r["section"] or "(no section)"
        rrf = f"{r['rrf_score']:.4f}" if r["rrf_score"] is not None else "n/a"
        print(f"[{i}] {r['source_file']} > {sect}  (rrf={rrf}, cos={cos})")
        # print body indented
        for ln in r["text"].splitlines():
            print(f"    {ln}")
        print()
    return 0


# ======================================================================
# today command (F3): same-day cross-session conversation browse (zero LLM)
# ======================================================================
def _today_since_utc(since_arg):
    """Resolve the --since argument to a UTC ISO8601 '...Z' string for comparison
    against stored ts. None -> local midnight today; 'HH:MM' -> that local time
    today; otherwise a local ISO datetime string."""
    if not since_arg:
        base = datetime.datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif re.match(r"^\d{1,2}:\d{2}$", since_arg.strip()):
        hh, mm = since_arg.strip().split(":")
        base = datetime.datetime.now().astimezone().replace(
            hour=int(hh), minute=int(mm), second=0, microsecond=0
        )
    else:
        dt = datetime.datetime.fromisoformat(since_arg.strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        base = dt
    return base.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def cmd_today(args):
    """Browse today's same-day conversation across sessions (zero LLM / zero embedding).
    Reads transcript_notes after --since, groups by session, applies a char cap."""
    since_utc = _today_since_utc(getattr(args, "since", None))
    conn = connect()
    init_db(conn)
    index_transcript_notes(conn)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, role, session, text FROM transcript_notes "
            "WHERE ts > ? ORDER BY ts ASC",
            (since_utc,),
        )
    ]
    conn.close()

    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    # --limit N: keep the newest N rows overall before the char cap.
    limit = getattr(args, "limit", 0) or 0
    if limit and len(rows) > limit:
        rows = rows[-limit:]

    if not rows:
        print("(오늘 다른 세션 대화 없음)")
        return 0

    # Char cap: remove the globally-oldest rows until total is within TODAY_CHAR_CAP
    # (each session keeps its newest tail). Track how many rows each session lost.
    def _row_len(r):
        return len((r.get("text") or "")) + 24  # + header/format overhead estimate

    kept = list(rows)
    omitted = {}  # session -> count removed
    total = sum(_row_len(r) for r in kept)
    while total > TODAY_CHAR_CAP and len(kept) > 1:
        drop = kept.pop(0)  # globally oldest (rows are ts ASC)
        omitted[drop["session"]] = omitted.get(drop["session"], 0) + 1
        total -= _row_len(drop)

    # group by session preserving first-appearance order
    order = []
    groups = {}
    for r in kept:
        s = r["session"]
        if s not in groups:
            groups[s] = []
            order.append(s)
        groups[s].append(r)

    out = []
    for s in order:
        grp = groups[s]
        head = f"== {(s or '')[:8]} ({len(grp)}행) =="
        if omitted.get(s):
            head += f" (… 앞부분 {omitted[s]}행 생략)"
        out.append(head)
        for r in grp:
            local = _parse_iso_utc(r["ts"]).astimezone()
            body = (r.get("text") or "").replace("\n", " ")
            out.append(f"{local.strftime('%H:%M')} {r.get('role', '')}: {body}")
        out.append("")
    print("\n".join(out).rstrip())
    return 0


# ======================================================================
# stats command
# ======================================================================
def cmd_stats(args):
    conn = connect()
    init_db(conn)

    total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    with_emb = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE embedding IS NOT NULL"
    ).fetchone()["c"]

    print("== memory index stats ==")
    print(f"total notes: {total}  (with embedding: {with_emb} / missing: {total - with_emb})")

    # M4: high NULL-embedding ratio signals that semantic search is dead (Ollama absent/down).
    # In that case auto-recall runs on keyword FTS fallback only -- print a prominent warning.
    null_frac = ((total - with_emb) / total) if total else 0.0
    if total and null_frac >= NULL_EMBED_WARN_FRAC:
        print(
            f"\n[!! warning] {null_frac*100:.0f}% of notes have no embedding -- semantic (vector) search degraded. "
            "Ollama(bge-m3) may not be installed or is down. Auto-recall running on keyword search only. "
            "Check Ollama then re-run `memory.py index`."
        )

    print("\nnotes per file:")
    for row in conn.execute(
        "SELECT source_file, COUNT(*) c FROM notes GROUP BY source_file ORDER BY c DESC"
    ):
        print(f"  {row['source_file']:<40} {row['c']}")

    # search log summary
    log = conn.execute("SELECT COUNT(*) c FROM search_log").fetchone()["c"]
    print(f"\ntotal searches: {log}")
    if log:
        # miss rate: 0 results or top1 below threshold
        miss = conn.execute(
            "SELECT COUNT(*) c FROM search_log "
            "WHERE n_results=0 OR top1_score IS NULL OR top1_score < ?",
            (MISS_THRESHOLD,),
        ).fetchone()["c"]
        rate = miss / log * 100
        print(f"search miss rate: {rate:.1f}%  (0 results or top1 cos<{MISS_THRESHOLD}, {miss}/{log})")

        print("\nrecent search log (up to 10):")
        for row in conn.execute(
            "SELECT query, n_results, top1_score, ts FROM search_log ORDER BY id DESC LIMIT 10"
        ):
            t1 = f"{row['top1_score']:.3f}" if row["top1_score"] is not None else "n/a"
            print(f"  {row['ts']}  n={row['n_results']:<2} top1={t1}  {row['query']}")

    conn.close()
    return 0


# ======================================================================
# write command (ingest-compress-store, OpenHuman style)
# ======================================================================
import subprocess  # noqa: E402  (write-only; kept separate from top-level imports)

HAIKU_MODEL = "haiku"  # claude CLI alias. Verified working (2026-06-24).

# DGN-446: compressor hardening constants.
# Max items the extractor may return from a single write input before we treat
# the output as a fragment explosion (extraction failure).
WRITE_FRAGMENT_CAP = 8

# (a) Scaffolding-line deny-list: extractor narration / preamble patterns that
# must never become stored items. Conservative: only clearly non-fact shapes.
# Applied as a post-process filter on raw extractor output in compress_with_haiku.
_SCAFFOLDING_RES = [
    # First-person extractor narration ("I'll extract ...", "Let me ...", "Here are ...")
    re.compile(r"^(I'?ll |Let me |I will |I've |I am |I'm |Here are |Here is |Below are |Below is )",
               re.IGNORECASE),
    # Bare "Output:" or any label-colon header (zero content after colon / or only a colon)
    re.compile(r"^[A-Za-z ]{1,30}:\s*$"),
    # Instruction-restatement lines that echo what the prompt asked to do
    re.compile(r"(extract|identify|persistent facts?|long.?term mem|장기 기억|영속적 사실)",
               re.IGNORECASE),
    # "User's X:" category headers emitted by the model
    re.compile(r"^(User'?s |사용자'?s? )\w.*:\s*$", re.IGNORECASE),
]

COMPRESS_PROMPT = (
    "Preserve an explicit user correction with a 'Correction: ' prefix.\n"
    "다음 내용에서 __USER_LABEL__(사용자)에 대해 장기 기억할 가치가 있는 영속적 사실만 골라, "
    "각각 한 줄짜리 원자적 항목으로 압축해라. 잡담/일시적 내용/추측은 버려라. "
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명이나 머리말을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만). "
    "기억할 가치가 있는 게 하나도 없으면 정확히 NONE 한 단어만 출력해라.\n\n"
    "내용:\n"
)

# DGN-2122: lexical grounding is deliberately conservative. Unknown content
# words (including names/places), numbers and attribute terms require source
# evidence. This is NOT a semantic entailment judge: ambiguous rewrites fall
# back to the source rather than being approved by another model.
_GROUND_WORD_RE = re.compile(
    r"__USER_LABEL__|[^\W\d_]+(?:\d+\w*|['\u2019][^\W\d_]+)?", re.UNICODE)
_GROUND_NUMBER_RE = re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*")
_GROUND_GRAMMAR = frozenset((
    "a an the i me my mine we us our you your user users "
    "is am are was were be been being have has had do does did "
    "it its this that these those and or of to in at on for with as "
    "called named name"
).split()) | {"__user_label__", "\uc0ac\uc6a9\uc790", "\ub098", "\uc800", "\ub0b4", "\uc81c", "\uc6b0\ub9ac"}
# Only spelling/inflection equivalents: no breed -> species, gender inference,
# relation broadening (friend -> spouse), unit conversion or geographic lookup.
_GROUND_EQUIVALENTS = (
    ("dog", "dogs", "\uac15\uc544\uc9c0", "\ubc18\ub824\uacac"),
    ("cat", "cats", "\uace0\uc591\uc774", "\ubc18\ub824\ubb18"),
    ("pet", "pets", "\ubc18\ub824\ub3d9\ubb3c", "\uc560\uc644\ub3d9\ubb3c"),
    ("male", "\ub0a8\uc131", "\uc218\ucef7"),
    ("female", "\uc5ec\uc131", "\uc554\ucef7"),
    ("wife", "\uc544\ub0b4"), ("husband", "\ub0a8\ud3b8"),
    ("mother", "mom", "mum", "\uc5b4\uba38\ub2c8", "\uc5c4\ub9c8"),
    ("father", "dad", "\uc544\ubc84\uc9c0", "\uc544\ube60"),
    ("friend", "friends", "\uce5c\uad6c"),
    ("like", "likes", "enjoy", "enjoys", "\uc88b\uc544\ud55c\ub2e4", "\uc88b\uc544\ud568", "\uc88b\uc544\ud574", "\uc88b\uc544\ud574\uc694", "\uc88b\uc544\ud569\ub2c8\ub2e4"),
    ("live", "lives", "living", "reside", "resides", "\uac70\uc8fc", "\uac70\uc8fc\ud568", "\uac70\uc8fc\ud55c\ub2e4", "\uc0b0\ub2e4", "\uc0b4\uc544\uc694"),
    ("work", "works", "working"),
    ("year", "years"),
)
_GROUND_ALIASES = {word: group[0] for group in _GROUND_EQUIVALENTS for word in group}
_GROUND_PARTICLES = (
    "\uc785\ub2c8\ub2e4", "\uc774\uc5d0\uc694", "\uc774\uc57c", "\uc774\ub2e4", "\uc608\uc694",
    "\uc5d0\uc11c", "\uc5d0\uac8c", "\uc73c\ub85c", "\uc774\ub791", "\uc740", "\ub294", "\uc774", "\uac00", "\uc744", "\ub97c",
    "\uc758", "\uc5d0", "\ub3c4", "\ub9cc", "\uacfc", "\uc640", "\ub791",
)
_GROUND_RETRY_PROMPT = (
    "Retry: the previous compression introduced unsupported content and was rejected. "
    "Keep only facts explicitly stated in the source. Copy all entity attributes, "
    "names, places, numbers, species, gender, relationships and ages from the source. "
    "Do not infer them from breeds, names, context or general knowledge. "
    "Prefer extractive wording; preserve the source language.\n\n"
)


def _ground_word_forms(word):
    word = word.casefold().replace("\u2019", "'")
    if word.endswith("'s"):
        word = word[:-2]
    forms = {word}
    if re.fullmatch(r"[\uac00-\ud7a3]+", word):
        for suffix in _GROUND_PARTICLES:
            if word.endswith(suffix):
                stem = word[:-len(suffix)]
                if len(stem) >= 2 or stem in {"\uc0b4", "\uc138", "\uba85", "\uc6d0", "\ub144"}:
                    forms.add(stem)
    return {_GROUND_ALIASES.get(form, form) for form in forms}


def _compression_grounding_error(raw, items):
    """Return a content-free reason for unsupported output, or None.

    Grammar and a bounded equivalence table allow ordinary extractive
    paraphrases. All other new lexical content fails closed. Presence cannot
    prove attribution/negation or arbitrary semantic equivalence; those remain
    outside this deterministic guard's contract.
    """
    source_numbers = set(_GROUND_NUMBER_RE.findall(raw))
    source_words = set()
    for word in _GROUND_WORD_RE.findall(raw):
        source_words.update(_ground_word_forms(word))
    supported = source_words | _GROUND_GRAMMAR
    for item in items:
        if set(_GROUND_NUMBER_RE.findall(item)) - source_numbers:
            return "unsupported number"
        for word in _GROUND_WORD_RE.findall(item):
            forms = _ground_word_forms(word)
            if not forms.intersection(supported):
                return "unsupported content token"
    return None


def _ground_write_compression(raw, items):
    """Validate write output, retry once, then retain the trimmed source.

    The check never calls a model. Only a rejected compression gets a second
    extraction attempt. Logs contain no source or rejected personal content.
    Returns (items, compression_ok) for the existing write preview/fallback.
    """
    reason = _compression_grounding_error(raw, items)
    if reason is None:
        return items, True
    print(f"[warn] compression rejected ({reason}); retrying once with source-only instructions",
          file=sys.stderr)
    try:
        retry = compress_with_haiku(raw, prompt_prefix=_GROUND_RETRY_PROMPT + COMPRESS_PROMPT)
    except RuntimeError:
        reason = "retry failed"
    else:
        reason = _compression_grounding_error(raw, retry)
        # After an unsupported positive extraction, NONE is not evidence that
        # the source should disappear. Preserve it if the retry is empty.
        if reason is None and retry:
            return retry, True
        reason = reason or "empty retry"
    print(f"[warn] compression rejected ({reason}); falling back to raw append",
          file=sys.stderr)
    return [raw.strip()], False


def _scrub_claude_session_env():
    """
    Return a copy of os.environ with Claude Code nesting guards removed.
    The 'cannot be launched inside another Claude Code session' guard fires
    when CLAUDECODE is set (and related CLAUDE_CODE_* markers).  Strip them
    so headless child launches succeed even when called from within a live
    agent session.  All other env vars (PATH, HOME, etc.) are preserved.
    """
    import os as _os
    return {
        k: v for k, v in _os.environ.items()
        if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE")
    }


# DGN-446 (b): structured-input detection heuristics.
# The compressor is designed for conversational prose. Pre-structured inputs
# (backlog blocks, spec paragraphs, markdown docs) trigger fragment explosion
# and fact fabrication. Detect them before invoking the extractor.
def _is_structured_input(text):
    """Return True when text looks like pre-structured (non-conversational) prose.
    Heuristics (any one sufficient):
    - Markdown bullet density: more than 35% of non-empty lines start with '- ' or '* '
    - Markdown header presence: at least 2 header lines (##+ ) in the text
    - Numbered-list density: more than 30% of non-empty lines start with a digit-dot/paren
    - Code-fence presence: contains a ``` block
    Conservative: short texts (<3 non-empty lines) are never flagged (not enough signal)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False

    bullet = sum(1 for ln in lines if re.match(r"^[\-\*]\s", ln.strip()))
    headers = sum(1 for ln in lines if re.match(r"^#{1,6}\s", ln.strip()))
    numbered = sum(1 for ln in lines if re.match(r"^\d+[.)]\s", ln.strip()))
    has_fence = "```" in text

    if has_fence:
        return True
    if headers >= 2:
        return True
    if len(lines) > 0 and (bullet / len(lines)) > 0.35:
        return True
    if len(lines) > 0 and (numbered / len(lines)) > 0.30:
        return True
    return False


# DGN-446 (P1b): work-item semantic gate.
# Memory stores durable user facts (semantic recall, no lifecycle).
# Work-items (backlog entries, parked features, tasks, to-dos, pending decisions)
# belong in worklog/ tickets (status/priority/lifecycle), not in the fact store.
# This gate catches work-item vocabulary and imperative-future framing in plain
# conversational prose that the structural gate (P2) would miss.
#
# Independent signal patterns (all matching is case-insensitive).
# Each pattern that fires contributes ONE signal.  Refusal threshold: >= 2.
# A single weak token never triggers alone -- prevents false-positives on
# ordinary user facts ("the user likes P2 coffee" has zero signals because
# P2 is mid-sentence, not a prefix/colon construct).
#
# _WI_VOCAB -- explicit work-item lifecycle vocabulary (each is its own pattern so
#   two matches in the same text = 2 signals, e.g. "backlog ... parked"):
#   TODO, 백로그, backlog, parked, 보류, 이연, 다음에 하(다/기), 나중에 하(다/기),
#   티켓으로, as a ticket
#   Korean tokens omit \b (word-boundary anchor fails on CJK).
#
# _WI_STATUS -- status/priority colon-keyed tokens found anywhere in text:
#   "status:", "priority:" (case-insensitive; mid-sentence is fine -- these are
#   strong markers in any position, unlike bare "P1"/"P2" which need context).
#
# _WI_PRIORITY_PREFIX -- bare P1/P2 as a line-starter (stripped of bullets/spaces):
#   prevents "the user likes P2 coffee" from triggering (P2 mid-sentence is NOT
#   a prefix).  Only "P1:" / "P2:" or "P1 <space/EOL>" at line start count.
#
# _WI_FUTURE -- imperative future-work framings:
#   Korean: 해야 함/한다/될, 할 것/예정/필요
#   English: needs to be done/implemented, should implement/be done/be implemented,
#            has to be done
_WI_VOCAB_PATTERNS = [
    re.compile(r"(?i)\btodo\b"),
    re.compile(r"백로그"),
    re.compile(r"(?i)\bbacklog\b"),
    re.compile(r"(?i)\bparked\b"),
    re.compile(r"보류"),
    re.compile(r"이연"),
    re.compile(r"다음에\s*하"),
    re.compile(r"나중에\s*하"),
    re.compile(r"티켓으로"),
    re.compile(r"(?i)as\s+a\s+ticket"),
]

_WI_STATUS = re.compile(r"(?i)\b(?:status|priority)\s*:")

_WI_PRIORITY_PREFIX = re.compile(r"(?im)^[\-\*\s]*\b(p1|p2)\b(?:\s*:|[\s]|$)")

_WI_FUTURE = re.compile(
    r"(?i)해야\s*(?:함|한다|될|되)"
    r"|할\s*(?:것|예정|필요)"
    r"|\bneeds?\s+to\s+be\s+(?:done|implemented)\b"
    r"|\bshould\s+(?:implement|be\s+(?:done|implemented))\b"
    r"|\bhas\s+to\s+be\s+done\b",
    re.IGNORECASE,
)


def _looks_like_work_item(text):
    """Return True when text carries >= 2 independent work-item signals.

    Signal sources (each that fires = +1):
      - Each _WI_VOCAB_PATTERNS[i] that matches: +1 per distinct pattern match.
        Two vocabulary words (e.g. "backlog ... parked") = 2 independent signals.
      - _WI_STATUS matches ("status:" / "priority:" anywhere): +1
      - _WI_PRIORITY_PREFIX matches (P1/P2 at line start): +1
      - _WI_FUTURE matches (imperative future-work framing): +1

    Threshold: >= 2 signals -> work-item.  One weak token alone -> pass.
    Conservative: empty / very short texts are never flagged."""
    if not text or len(text.strip()) < 10:
        return False

    signals = 0

    # Each vocabulary pattern is independent -- two vocab hits = 2 signals.
    for pat in _WI_VOCAB_PATTERNS:
        if pat.search(text):
            signals += 1
            if signals >= 2:
                return True  # early exit

    if _WI_STATUS.search(text):
        signals += 1
        if signals >= 2:
            return True

    if _WI_PRIORITY_PREFIX.search(text):
        signals += 1
        if signals >= 2:
            return True

    if _WI_FUTURE.search(text):
        signals += 1
        if signals >= 2:
            return True

    return False


def compress_with_haiku(raw_text, prompt_prefix=COMPRESS_PROMPT, model=HAIKU_MODEL):
    """
    Compress raw text into persistent-fact atomic items using headless claude.
    prompt_prefix: the compression instruction prompt (default COMPRESS_PROMPT -- user facts only).
                   consolidate passes CONSOLIDATE_PROMPT to include life facts.
    model: claude CLI model alias (default haiku -- for single write calls).
           consolidate passes CONSOLIDATE_MODEL (sonnet) for better judgment.
    Returns: [one-line item, ...]  (empty means nothing worth remembering)
    Raises RuntimeError on failure.
    """
    prompt = prompt_prefix + raw_text
    # Scrub nesting env vars so the child launch succeeds when called from
    # within a live Claude Code session (DGN-352 / DGN-238 pattern).
    child_env = _scrub_claude_session_env()
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=180,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        # Do NOT embed str(e) directly: TimeoutExpired.__str__ includes the full cmd
        # list (which contains the prompt), making the error unreadable in logs and
        # the retry-queue last_error field (truncated to 500 chars = just prompt text).
        # Use type name + a safe attribute only.
        detail = (
            f"timeout after {e.timeout}s"
            if isinstance(e, subprocess.TimeoutExpired)
            else str(e)
        )
        raise RuntimeError(f"{model} call failed: {type(e).__name__}: {detail}")
    if proc.returncode != 0:
        raise RuntimeError(f"{model} exited abnormally (rc={proc.returncode}): {proc.stderr.strip()}")

    items = []
    for ln in proc.stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        # strip any bullet/number the model may have added
        s = re.sub(r"^[-*•]\s+", "", s)
        s = re.sub(r"^\d+[.)]\s+", "", s)
        s = s.strip()
        if not s:
            continue
        # handle "nothing to remember" signal
        if s.upper() == "NONE":
            return []
        # DGN-446 (a): strip scaffolding / preamble lines the extractor emits.
        # These are not facts -- they are the model narrating what it is doing.
        # Deny-list is conservative: only clearly non-fact shapes are dropped.
        if any(pat.search(s) for pat in _SCAFFOLDING_RES):
            continue
        items.append(s)
    return items


def append_notes_to_md(path, section, items):
    """
    Append items to the target md file using § separators.
    - If section is given: insert at the end of the matching header (### ...) block;
      create the header at end-of-file if not found.
    - If section is not given: append to end of file.
    Writes directly to the source-of-truth file.
    """
    # F6: redact any secret in the items before they are written to disk.
    items = [redact_secrets(it) for it in items]

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()

    # Match the write separator to the target file's format (prevents chunking breakage).
    # M5: trust an explicit format marker over the § sniff (a human toggling one § won't flip mode).
    # When the marker is absent, fall back to § sniffing (backward compat), then embed
    # the detected mode as a marker in this write so future machine writes use the marker.
    #   - § based files (topic files / inbox): "§\n{item}".
    #   - bullet based files: "- {item}".
    marker = _format_marker(content)
    if marker is not None:
        has_sep = (marker == "section")
    else:
        has_sep = any(ln.strip() == "§" for ln in lines)
        # marker absent -> embed the sniffed mode (self-healing; stable going forward).
        mode = "section" if has_sep else "bullet"
        content = _marker_line(mode) + "\n" + content
        lines = content.splitlines()
    if has_sep:
        block = "\n".join(f"§\n{it}" for it in items)
    else:
        block = "\n".join(f"- {it}" for it in items)

    if section:
        # find the header line index
        hdr_idx = None
        for i, ln in enumerate(lines):
            if _is_header(ln) and _header_text(ln) == section:
                hdr_idx = i
                break
        if hdr_idx is None:
            # section not found -> create new header + block at end of file
            tail = content.rstrip("\n")
            new = f"{tail}\n\n### {section}\n{block}\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            return f"created new section '### {section}' and appended to end of file"
        # the section spans up to just before the next header
        end_idx = len(lines)
        for j in range(hdr_idx + 1, len(lines)):
            if _is_header(lines[j]):
                end_idx = j
                break
        # skip trailing blank lines before end_idx to find the insert position
        insert_at = end_idx
        while insert_at - 1 > hdr_idx and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + block.splitlines() + lines[insert_at:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
        return f"appended to end of section '### {section}'"
    else:
        tail = content.rstrip("\n")
        new = f"{tail}\n{block}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        return "appended to end of file"


def cmd_write(args):
    # input: arg takes priority; fall back to stdin
    if args.text:
        raw = args.text
    else:
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        print("[error] input text is empty.", file=sys.stderr)
        return 1

    force = getattr(args, "force", False)

    # DGN-446 (b): input shape gate.
    # Structured inputs (backlog blocks, spec paragraphs, markdown docs) cause
    # fragment explosion and fact fabrication. Refuse unless --force is passed.
    if not force and _is_structured_input(raw):
        print(
            "[error] input looks like pre-structured content (markdown bullets/headers/"
            "numbered list/code fence detected). The compressor is designed for "
            "conversational prose; structured content causes fragment explosion and "
            "fact fabrication. This belongs in a ticket or document, not memory.\n"
            "Pass --force to bypass this check and attempt compression anyway.",
            file=sys.stderr,
        )
        return 2

    # DGN-446 (P1b): work-item semantic gate.
    # Plain conversational prose carrying work-item vocabulary or imperative-future
    # framing belongs in worklog/ tickets (status/priority/lifecycle), not the fact
    # store. Refuse unless --force is passed.
    if not force and _looks_like_work_item(raw):
        print(
            "[error] input looks like a work-item (backlog entry, task, parked feature, "
            "or to-do). Work-items belong in the agent's worklog/ ticket surface "
            "(status / priority / lifecycle tracking), not in memory (fact-recall only). "
            "Copy worklog/_TEMPLATE.md to open a ticket.\n"
            "Pass --force to bypass this check and attempt fact extraction anyway.",
            file=sys.stderr,
        )
        return 2

    # 1) compress (with raw-append fallback: a write must never be lost to a
    #    compression failure -- DGN-352)
    compression_ok = True
    try:
        items = compress_with_haiku(raw)
    except RuntimeError as e:
        print(f"[warn] compression failed ({e}); falling back to raw append", file=sys.stderr)
        compression_ok = False
        # Treat the entire raw text as one uncompressed item so the write survives.
        items = [raw]

    if compression_ok:
        items, compression_ok = _ground_write_compression(raw, items)

    # DGN-446 (c): fragment cap.
    # More than WRITE_FRAGMENT_CAP items from a single write input is a strong
    # signal of extraction failure (the extractor over-segmented or hallucinated).
    # Refuse to store -- same path as the shape gate.
    if not force and len(items) > WRITE_FRAGMENT_CAP:
        print(
            f"[error] extractor returned {len(items)} items from a single write input "
            f"(cap={WRITE_FRAGMENT_CAP}). This is a fragment-explosion signal -- the "
            "compressor likely mis-read the input as many separate facts. "
            "This belongs in a ticket or document, not memory.\n"
            "Pass --force to bypass this check and store all items anyway.",
            file=sys.stderr,
        )
        return 2

    if not items:
        print("No persistent facts worth remembering; nothing stored.")
        return 0

    # 2) attach metadata: (date, source)
    # Compression may paraphrase away the marker; an atomic user correction
    # still carries its source intent through the write/index lifecycle.
    if _is_correction(raw) and len(items) == 1 and not _is_correction(items[0]):
        items = ["Correction: " + items[0]]
    today = datetime.date.today().isoformat()
    src = args.source or "unknown-source"
    tagged = [f"{it} ({today}, {src})" for it in items]

    # target file path
    target = os.path.join(MEMORIES_DIR, args.file)

    # print preview of what will be stored
    label = "raw-append (compression failed or rejected)" if not compression_ok else "compression result"
    print(f"== {label} ({len(tagged)} items) -> {args.file}" +
          (f" > ### {args.section}" if args.section else " (end of file)") + " ==")
    for it in tagged:
        print(f"  § {it}")

    if args.dry_run:
        print("\n[dry-run] file not modified. the items above would be stored.")
        return 0

    if not os.path.isfile(target):
        print(f"[error] target file not found: {target}", file=sys.stderr)
        return 1

    # 3) append to file
    where = append_notes_to_md(target, args.section, tagged)
    print(f"\n[stored] {where}")

    # A correction must retire the old source line before rebuilding its index.
    mark_superseded()

    # 4) update index
    print("[index] updating index...")
    rc = cmd_index(argparse.Namespace())
    return rc


# ======================================================================
# consolidate command (nightly consolidation: conversation transcripts -> long-term memory distillation)
# ======================================================================
# Core: distill persistent facts from the original __USER_LABEL__ <-> __AGENT_LABEL__ chat log
# (jsonl) and write them to inbox.md.
# A watermark (consolidation_state.last_ts) records progress -> only processes the incremental diff.
# Nightly: no topic routing; always stores to inbox.md only (weekly classify-inbox distributes).

# Transcript location (Claude Code project logs). __AGENT_LABEL__ workspace sessions.
def _transcript_glob_for(agent_root, projects_root):
    """Glob for ONE agent's top-level session transcripts. The project dir is
    Claude Code's slug of the agent root (every non-alnum -> '-'), so the dir
    part carries no wildcard: a sibling agent ('ag' vs 'ag-old') or another
    agent's dir can never match (DGN-1949 no-cross-agent contract)."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(agent_root))
    return os.path.join(projects_root, slug, "*.jsonl")


TRANSCRIPT_GLOB = _transcript_glob_for(
    os.path.join(HERE, ".."), os.path.expanduser("~/.claude/projects")
)
TARGET_MEMORY_MD = os.path.join(MEMORIES_DIR, "inbox.md")
DEDUP_THRESHOLD = 0.82     # candidate cosine >= this against an existing note means "already known"
# Correction/revision/retraction markers. Candidates starting with these are near-verbatim
# restatements so their cosine >= DEDUP_THRESHOLD and they would be silently dropped by the
# hard dedup skip (= stale fact stays authoritative, DGN-055 violation).
# Exempt these candidates from the dedup skip so they are stored. Normal restatements still skipped.
CORRECTION_MARKERS = ("정정:", "정정 ", "correction:", "correction ")
DEFAULT_LOOKBACK_DAYS = 3  # how many days to look back when there is no watermark (first run)
MEMORY_LINES_CAP = 600     # if inbox.md line count exceeds this, suggest a cleanup
CONSOLIDATE_REPORT_ENABLED = os.environ.get("DOGANY_MEMORY_REPORT", "0") == "1"  # product: nightly "memory saved" Telegram push OFF by default
# Long conversations can exceed the input limit and cause the entire compression to fail.
# Split on line (message) boundaries into chunks of at most this many characters, compress
# each chunk individually, then merge the items.
CONSOLIDATE_CHUNK_CHARS = 24000
# consolidate uses Sonnet (better judgment for utterance vs. fact). Runs once per night, cost acceptable.
# write keeps Haiku.
CONSOLIDATE_MODEL = "sonnet"
# DGN-726 (protocol upgraded by DGN-835): usage-exhaustion defer. When the account
# usage window is exhausted the nightly sonnet compression is guaranteed to fail
# (rc=1). That is a known-transient condition, not a code defect, so it must NOT
# surface to the owner as a failure alert. Instead we defer with the 2-factor
# usage-block signal: preserve the watermark, drop a marker recording the exhausted
# window + next usage-reset boundary, and EXIT 75 (EX_TEMPFAIL). cron-guard consumes
# rc75 + a fresh marker and owns recovery routing (five_hour -> automatic
# reset-aligned one-shot via usage-defer-retry.sh; seven_day -> owner notification
# with a manual-retry button). Fail-open everywhere: any usage-lookup error falls
# back to the normal (visible) path or the next daily run.
# Utilization at/above this % counts a window as exhausted for defer purposes. Kept
# high so only genuine exhaustion (not merely "hot") triggers the silent path.
# NOTE (DGN-835): routines/usage-gate.py RELIEF_CAP (97) and CAP_EXEMPT (99) are
# deliberately positioned against this constant -- interactive relief stops at 97 so
# the 97-99% band stays reserved for this automatic pipeline; CAP_EXEMPT aligns with
# 99. Keep the three in lockstep when tuning.
CONSOLIDATE_USAGE_EXHAUSTION_PCT = 99.0
# Exit code for a usage-deferred run (EX_TEMPFAIL). Half of the 2-factor signal;
# the fresh marker is the other half (rc75 alone stays a visible failure upstream).
CONSOLIDATE_RC_USAGE_DEFER = 75
# Marker file consumed upstream to route recovery. Default (legacy DGN-726) path
# lives beside state.db so it travels with a workspace move; when cron-guard wraps
# the run it exports DOGANY_USAGE_DEFER_MARKER (per-label path under
# ~/.dogany/usage-defer/) which takes priority -- see _defer_marker_path().
CONSOLIDATE_DEFER_MARKER = os.path.join(HERE, ".consolidate-usage-defer.json")
# DGN-835 B3: last-SUCCESS timestamp artifact for stale detection. Written ONLY on a
# normal exit-0 completion (real consolidation or no-new-data); never on defer or
# failure. Consumers must use this file, NOT MEMORY.md/inbox.md mtime (those move for
# unrelated reasons -> false negatives). READ path: _write_defer_marker embeds it as
# the marker's last_success_at field; cron-guard's stale-canon alert consumes that
# (the reader never touches engine paths directly).
CONSOLIDATE_LAST_SUCCESS = os.path.join(HERE, ".consolidate-last-success")
# Path to taxonomy document (the second-stage KEEP/DROP filter injects the full text).
TAXONOMY_PATH = os.path.join(HERE, "CONSOLIDATION_TAXONOMY.md")
# Maximum number of candidates to judge in a single second-stage filter batch call (prevents bloated prompts).
FILTER_BATCH_SIZE = 40

# ---- DGN-1468: bounded-run budget (the nightly step is killed at a hard limit) ----
# Measured (dev instance, 2026-09-15): first-pass Sonnet compression costs ~33s per
# 24k-char chunk and runs SERIALLY, so the run cost is linear in the collected
# span. The watermark used to be all-or-nothing: a run that did not reach the
# end committed NOTHING, so the next night re-collected the same span plus one
# more day. Each miss made the next run strictly more expensive -- a ratchet
# with no self-healing (measured: 4 consecutive nights killed at 1500s, span grown
# to 56 chunks / 1.34 MB / 4.7 days).
# The fix moves the root: the run carries a wall-clock budget, stops starting
# new chunks before it blows through, and advances the watermark to the last
# FULLY processed message. Partial progress is real progress -- the backlog can
# only shrink. Env seam for testing / ops; <= 0 disables the budget entirely
# (pre-DGN-1468 behavior, for manual catch-up runs with no kill limit).


def _env_num(name, default, cast=float):
    """Parse env NAME as a number, falling back to `default` with a warning on
    garbage. These constants are evaluated at IMPORT time, and memory.py is
    imported by every entry point (search hook included) -- a misconfigured env
    var must degrade the one knob, never kill the whole module."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return cast(default)
    try:
        return cast(raw)
    except ValueError:
        print(
            f"[warn] env {name}={raw!r} is not a number -- using default {default}",
            file=sys.stderr,
        )
        return cast(default)


CONSOLIDATE_BUDGET_SECS = _env_num("DOGANY_CONSOLIDATE_BUDGET_SECS", 1200)
# Per-candidate cost of everything DOWNSTREAM of compression (second-stage
# filter + embed + cosine dedup scan). Measured on a dev instance 2026-09-15:
# filter 4.6s / 40 items = 0.115s, embed 0.19s, cosine scan vs a 4026-note pool
# 0.44s -> ~0.75s per candidate. The compression loop reserves this much of the
# budget for the tail so the run does not stop compressing only to be killed in
# dedup. Env-overridable because the cosine term scales with the note pool.
CONSOLIDATE_DOWNSTREAM_SECS_PER_CAND = _env_num(
    "DOGANY_CONSOLIDATE_DOWNSTREAM_SECS", 0.75
)
# The reserve never eats more than this fraction of the budget (a huge candidate
# count must not starve compression down to zero chunks -- forward progress on
# at least one chunk per run is what kills the ratchet).
CONSOLIDATE_RESERVE_MAX_FRAC = 0.5
# Safety factor applied to the running per-chunk estimate when deciding whether
# one more chunk fits.
CONSOLIDATE_CHUNK_EST_SAFETY = 1.15
# Backlog state keys (consolidation_state). backlog_rows = messages left behind
# by the last budget-bounded run; backlog_runs = how many consecutive runs have
# ended with a non-empty backlog.
CONSOLIDATE_BACKLOG_ROWS_KEY = "backlog_rows"
CONSOLIDATE_BACKLOG_RUNS_KEY = "backlog_runs"
# Draining is normal and exits 0. STUCK draining is not: after this many
# consecutive runs that all end with a backlog, consolidate returns 1 so the
# failure is counted by the queue runner instead of reading as a clean night
# (DGN-1154 silent-green disease). A measured instance drains ~36 chunks/run against ~9
# chunks/day of new traffic, so a healthy catch-up finishes well inside this.
CONSOLIDATE_BACKLOG_STUCK_RUNS = 3
# DGN-1582 (1): exit code for that STUCK night. The run itself finished inside
# its budget and committed its work -- rc 1 made cron-guard ring the generic
# "the job failed" alarm over a designed outcome. 77 is its own signal: the
# queue runner counts it as behind= (not fail=) and cron-guard sends a
# "behind" notice instead of the failure alarm. 2-factor like rc 75: only
# with a marker written THIS run at the path cron-guard exports in
# DOGANY_BACKLOG_MARKER; without one (manual run / write error) the night
# stays rc 1, the visible failure path.
CONSOLIDATE_RC_BACKLOG_BEHIND = 77
# DGN-1582 r3 (dec-236): from the 2nd consecutive behind night the program
# raises the NEXT night's budget itself -- one bounded step per behind night,
# never past the cap. The record is this file (JSON: budget_secs, from, at,
# rows_left, behind_night); the dawn queue reads it, exports the raised
# budget as DOGANY_CONSOLIDATE_BUDGET_SECS and widens its kill timeout to
# match. A drained night deletes it (back to the default). An old queue
# runner that does not read the file keeps the default budget AND the
# default kill limit, so a raise can never outrun the timeout.
CONSOLIDATE_BUDGET_BOOST_FILE = os.path.join(HERE, ".consolidate-budget-boost.json")
CONSOLIDATE_BUDGET_STEP_SECS = 300
CONSOLIDATE_BUDGET_CAP_SECS = 2100

# ---- DGN-851: agent-role config seam (base-kit domain decoupling) ----
# The consolidate/classifier prompts describe this agent with a ROLE noun.
# That noun is kit PRODUCT content (a lifekit instance calls itself a life
# assistant), so it is never hardcoded here. Resolution: env
# DOGANY_AGENT_ROLE -> config/agent.conf AGENT_ROLE= -> generic default.
# A kit opts in by writing AGENT_ROLE= into config/agent.conf (the lifekit
# bundle supplies its own wording -- kit lane); the framework default is
# the neutral assistant wording.
def _agent_conf_get(key):
    """Read KEY=value from <root>/config/agent.conf (status-footer.py
    _conf_get precedent). DOGANY_AGENT_CONF overrides the conf path (test
    isolation). Missing file / missing key -> '' (fail-open)."""
    path = os.environ.get("DOGANY_AGENT_CONF") or os.path.normpath(
        os.path.join(HERE, "..", "config", "agent.conf"))
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


AGENT_ROLE = (os.environ.get("DOGANY_AGENT_ROLE")
              or _agent_conf_get("AGENT_ROLE") or "어시스턴트")

# Compression prompt for the __AGENT_LABEL__ assistant. Includes user facts + life decisions/habits/
# relationships/schedules/finances.
# Core rule: do NOT turn utterances or progress reports into facts. Only confirmed persistent facts/decisions.
CONSOLIDATE_PROMPT = (
    "아래는 __USER_LABEL__(사용자)과 " + AGENT_ROLE + " 에이전트 __AGENT_LABEL__(어시스턴트)가 주고받은 대화 기록이다. "
    "이 안에서 장기 기억할 가치가 있는 영속적 사실/결정만 골라 각각 한 줄짜리 원자적 항목으로 압축해라.\n"
    "\n"
    "절대 원칙: 발화 자체나 진행 상황 보고가 아니라, 대화를 통해 확정된 영속적 사실/결정만 뽑는다. "
    "__AGENT_LABEL__(어시스턴트)가 한 말·질문·보고·멘트는 그 자체로는 사실이 아니다. "
    "Subject rule (DGN-1384): a conclusion, judgment or proposal by __AGENT_LABEL__ is NOT a decision by __USER_LABEL__. "
    "Unless a __USER_LABEL__ line states it or explicitly approves it, never rewrite it as a subjectless decision "
    "('it was decided', 'it is settled', 'X is banned').\n"
    "Lines starting with 'system:' are machine text (cron, dispatch results, wrappers), never __USER_LABEL__'s words.\n"
    "\n"
    "Subject mark (required, at the start of every item):\n"
    "- [U] = stated or explicitly approved by __USER_LABEL__. End the line with ' || \"<short span copied verbatim "
    "from a __USER_LABEL__: line>\"' (copy, never paraphrase).\n"
    "- [A] = __AGENT_LABEL__'s own conclusion, judgment or observation. Phrase it as a judgment, not a decision.\n"
    "No verbatim span to cite -> it is not [U].\n"
    "\n"
    "HABIT UPGRADE PROHIBITION: one observation is not a habit. For a [U] item, never add habitual "
    "framing (fixed, usual, always, every day, \uace0\uc815, \ub9e4\uc77c, \ud56d\uc0c1) unless that framing is present "
    "in the copied __USER_LABEL__ span. Preserve explicit future routines and schedules exactly as stated.\n"
    "\n"
    "반드시 버려라(항목으로 절대 만들지 마라):\n"
    "- 코드블록·코드펜스(``` 류)·명령줄·셸 명령(launchctl, git, python ... 같은 실행 명령)\n"
    "- 질문(예: ~할까요? ~될까요?), 사과(죄송), 인사, 맞장구, 감탄\n"
    "- 발화 라벨(__USER_LABEL__:, __AGENT_LABEL__: 등) 그 자체 -- 주체는 위의 [U]/[A] 표시로만 남긴다\n"
    "- 테스트/디버깅 진행 멘트(예: 지금 ~해보겠습니다, 이제 ~하는 중, ~확인해보겠습니다)\n"
    "- 한 번 쓰고 마는 일회성 지시·요청, 곧 무의미해질 임시 진행상황(예: ~를 기다리는 중, 아직 ~필요)\n"
    "\n"
    "남겨라(이런 것만):\n"
    "- __USER_LABEL__ 프로필·선호·습관·일/배경\n"
    "- 건강 지표·신체 스탯·목표(칼로리·단백질 모델 등)\n"
    "- 생활 루틴·습관(식단/운동 패턴, 일정 규칙), 관계·사람 맥락, 취향, 재정 패턴\n"
    "- 확정된 결정·합의, 인프라/경로/설정의 영구적 변경\n"
    "- 앞으로도 유효한 규칙·수치(DB ID, 임계값 등)\n"
    "\n"
    "기존에 알던 사실을 뒤집는 정정이면 그 항목 맨 앞에 '정정: ' 을 붙여라.\n"
    "\n"
    "예시(입력 → 출력):\n"
    "- 입력: \"__AGENT_LABEL__: 회고 시간 21시로 옮길까요?\" → 출력: 없음(버림. 질문이라 사실 아님)\n"
    "- 입력: \"__AGENT_LABEL__: 이제 식단 집계를 돌려보겠습니다\" → 출력: 없음(버림. 진행 멘트)\n"
    "- 입력: \"__USER_LABEL__: 단백질 목표 하루 160g으로 잡아\" → 출력: [U] 단백질 목표 하루 160g || \"단백질 목표 하루 160g으로 잡아\"\n"
    "- \uc785\ub825: \"__USER_LABEL__: \uc544\uce68\uc5d0 \uc774\uc0ad\ud1a0\uc2a4\ud2b8 \uba39\uc5c8\uc5b4\" \u2192 \ucd9c\ub825: [U] \uc544\uce68\uc5d0 \uc774\uc0ad\ud1a0\uc2a4\ud2b8 \uba39\uc5c8\uc5b4 || \"\uc544\uce68\uc5d0 \uc774\uc0ad\ud1a0\uc2a4\ud2b8 \uba39\uc5c8\uc5b4\" (\uace0\uc815 \uc544\uce68 \uc2dd\ub2e8\uc73c\ub85c \uc2b9\uaca9 \uae08\uc9c0)\n"
    "- in: \"__AGENT_LABEL__: so a weekly retro looks sufficient\" (no __USER_LABEL__ approval line) "
    "-> out: [A] judged that a weekly retro is sufficient (not a decision)\n"
    "- 입력: \"__USER_LABEL__, 죄송합니다. 다시 볼게요\" → 출력: 없음(버림. 사과+멘트)\n"
    "(이 대화 청크가 통째로 인사·질문·진행보고·명령뿐이라 남길 사실이 없으면 정확히 NONE 한 단어만 출력)\n"
    "\n"
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명·머리말·발화 라벨을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만). "
    "The only extras allowed are the [U]/[A] mark and the [U] quote span; a correction goes after the mark "
    "('[U] \uc815\uc815: ...').\n"
    "\n"
    "대화 기록:\n"
)

# ----------------------------------------------------------------------
# DGN-2028 / DGN-2010 slice 4: write-side classification + routing, behind
# CONSOLIDATE_ROUTES (agent.conf, or DOGANY_CONSOLIDATE_ROUTES), default OFF.
# Off: _ROUTES stays empty, contract_routing is never imported, and the
# prompts, output and side effects of consolidate are byte-identical to
# before (tests/test_consolidate_routes.py golden). On: _ROUTES[0] carries
# the run's context (installed contracts, prompt addenda, per-item
# classification); see contract_routing.py for the routing order.
# ----------------------------------------------------------------------
CONSOLIDATE_ROUTES_KEY = "CONSOLIDATE_ROUTES"
_ROUTES = []


def _consolidate_routes_enabled():
    raw = (os.environ.get("DOGANY_CONSOLIDATE_ROUTES")
           or _agent_conf_get(CONSOLIDATE_ROUTES_KEY) or "").strip().lower()
    return raw in ("1", "on", "true", "yes")


def _consolidate_prompt():
    """The first-pass prompt: CONSOLIDATE_PROMPT itself unless routing is on."""
    return _ROUTES[0]["extract_prompt"] if _ROUTES else CONSOLIDATE_PROMPT


def _routes_root():
    return os.environ.get("DOGANY_ROUTES_ROOT") or os.path.normpath(os.path.join(HERE, ".."))


def _routes_context():
    import contract_routing as cr
    root = _routes_root()
    contracts = cr.installed_contracts(root)
    targets = cr.declared_targets(contracts)
    slots = cr.slot_names(root)
    tail = "\ub300\ud654 \uae30\ub85d:\n"
    assert CONSOLIDATE_PROMPT.endswith(tail)
    return {
        "cr": cr, "root": root, "contracts": contracts, "slots": slots,
        "target_ids": {t for t, _ in targets},
        "instance": os.environ.get("DOGANY_INSTANCE_ID") or os.path.basename(root),
        "extract_prompt": (CONSOLIDATE_PROMPT[:-len(tail)] + cr.EXTRACT_ADDENDUM + tail),
        "filter_addendum": cr.filter_addendum(slots, targets),
        "cls": {}, "rows": [],
    }


def _routes_event_id(ctx, fact):
    """Source event of a kept item: the owner message its verified quote came
    from (date + ordinal among identical messages that date), else a local
    observation of this instance."""
    cr = ctx["cr"]
    subject, quote = _ITEM_SUBJECTS.get(fact.strip(), (SUBJECT_UNVERIFIED, None))
    if subject == SUBJECT_OWNER and quote:
        seen = {}
        for ts, spk, txt, _sess in ctx["rows"]:
            if spk != "__USER_LABEL__":
                continue
            w = _norm_quote(_owner_words(txt) or "")
            k = (ts[:10], cr.norm_text(w))
            seen[k] = seen.get(k, 0) + 1
            if w and quote in w:
                return cr.owner_event_id(w, ts, seen[k])
    return cr.local_event_id(ctx["instance"], fact)


def _routes_plan(ctx, candidates):
    """Plan every kept candidate (pure + read-only provider lookups).
    Returns (base candidates in order, {fact: plan}, routed plans)."""
    cr = ctx["cr"]
    client = cr.ProviderClient(ctx["root"], ctx["contracts"])
    plans = {}
    for c in candidates:
        cls, err = ctx["cls"].get(c, (None, "unclassified:no_verdict"))
        subject, quote = _ITEM_SUBJECTS.get(c.strip(), (SUBJECT_UNVERIFIED, None))
        plans[c] = cr.plan_item({"text": c, "subject": subject, "quote": quote,
                                 "event_id": _routes_event_id(ctx, c),
                                 "cls": cls, "cls_error": err},
                                ctx["contracts"], client)
    base = [c for c in candidates if plans[c]["lands"] == "base"]
    routed = [plans[c] for c in candidates if plans[c]["lands"] != "base"]
    return base, plans, routed


def _routes_write_pending(ctx, routed, today):
    """Append pending routed items to routed-pending.md (retrievable as
    unresolved evidence). An intent already in the file is not re-added."""
    pend = [p for p in routed if p["lands"] == "pending"]
    if not pend:
        return 0
    path = os.path.join(MEMORIES_DIR, ctx["cr"].PENDING_MD)
    os.makedirs(MEMORIES_DIR, exist_ok=True)
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as _f:
            _f.write(_marker_line("section") + "\n")
    with open(path, encoding="utf-8") as _f:
        have = _f.read()
    lines = []
    for p in pend:
        if ("intent=" + p["intent_id"]) in have:
            continue
        lines.append(ctx["cr"].tag_line(_subject_tag(p["text"], today), p))
    if lines:
        append_notes_to_md(path, None, lines)
    return len(lines)


# Noise patterns to filter from transcripts (hook injections, system reminders, etc.).
_NOISE_MARKERS = (
    "<system-reminder>",
    "[관련 기억",          # covers 자동검색/키워드검색 headers (incl. new "· 참고용" suffix)
    "[기억규칙]",          # DGN-848 recalled-memory precedence frame lines
    "[오늘 다른 세션",
    "[구조화상태]",        # DGN-1176 ledger-preload sentinel (belt: substring lane)
    "[현재 신체/목표]",    # DGN-1176 grill minor#1: legacy body-state line, both lists
    "[turn frame]",        # DGN-2028 turn frame sentinel (both lists)
    "hookSpecificOutput",
    "additionalContext",
)


def _ts_load_watermark(conn):
    """Load last_ts (ISO8601) from consolidation_state. Returns None if not set."""
    row = conn.execute(
        "SELECT value FROM consolidation_state WHERE key='last_ts'"
    ).fetchone()
    return row["value"] if row else None


def _ts_save_watermark(conn, ts_iso):
    """Save (upsert) the last_ts watermark."""
    conn.execute(
        "INSERT INTO consolidation_state(key, value) VALUES('last_ts', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ts_iso,),
    )
    conn.commit()


def _backlog_load(conn):
    """DGN-1468: (rows_left, consecutive_runs_with_backlog) from consolidation_state.
    Missing / unparseable -> (0, 0) (fail-open: a fresh install is not 'stuck')."""
    out = []
    for key in (CONSOLIDATE_BACKLOG_ROWS_KEY, CONSOLIDATE_BACKLOG_RUNS_KEY):
        row = conn.execute(
            "SELECT value FROM consolidation_state WHERE key=?", (key,)
        ).fetchone()
        try:
            out.append(int(row["value"]) if row else 0)
        except (TypeError, ValueError):
            out.append(0)
    return out[0], out[1]


def _backlog_save(conn, rows_left, runs):
    """DGN-1468: persist the backlog counters (upsert)."""
    for key, val in (
        (CONSOLIDATE_BACKLOG_ROWS_KEY, rows_left),
        (CONSOLIDATE_BACKLOG_RUNS_KEY, runs),
    ):
        conn.execute(
            "INSERT INTO consolidation_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(int(val))),
        )
    conn.commit()


def _is_noise(text):
    """Returns True if the text looks like noise (hook injection or system reminder)."""
    if not text:
        return True
    for m in _NOISE_MARKERS:
        if m in text:
            return True
    return False


# Heuristic for shell-command-looking lines. Lines starting with common commands are stripped
# from compression input. (denoise_for_index already removes entire code-fence ```...``` blocks,
# so only bare command lines outside fences need to be caught here.)
_SHELL_CMD_HEADS = (
    "sudo ", "launchctl ", "git ", "python ", "python3 ", "pip ", "pip3 ",
    "npm ", "node ", "brew ", "curl ", "wget ", "ssh ", "scp ", "rsync ",
    "cd ", "ls ", "cat ", "rm ", "mv ", "cp ", "mkdir ", "chmod ", "chown ",
    "kill ", "pkill ", "ps ", "grep ", "sed ", "awk ", "tail ", "head ",
    "echo ", "export ", "source ", "bash ", "sh ", "./", "trash ",
    "docker ", "systemctl ", "plutil ", "defaults ",
)

# Empty label lines (a bare colon-label fragment). If a line starts with one of these
# and has almost no content after the colon, it is stripped.
_LABEL_HEADS = (
    "요구사항", "참고", "작업 순서", "다음 할 일", "다음 단계", "현재 상황",
    "현재 상태", "남은 작업", "완료된 것", "할 일", "todo", "선택지", "옵션",
    "정리", "요약", "메모", "주의", "결론", "현황", "진행 상황", "체크리스트",
)
# Progress-report / status symbols. Stripped when they appear at the start of a line.
_STATUS_SYMBOLS = ("✓", "✅", "⏳", "🔵", "🟢", "🟡", "⚪", "☑", "✔", "❌", "▶", "▷", "□", "■")


def _is_junk_line(line):
    """Is this line deterministically discardable from compression input/candidates?
    (code-level implementation of the taxonomy DROP rules)
    - Markdown headers (#), separator lines (---/===), horizontal rules
    - Shell command lines
    - Progress-report lines starting with a status symbol
    - Question lines ending with a question mark
    - Apology/address lines starting with '__USER_LABEL__,'
    - Empty label lines (bare colon-label fragments like 'requirements:', 'note:', etc.)
    Blank lines are NOT junk (return False) -- preserved for flow.
    """
    s = line.strip()
    if not s:
        return False
    # markdown header
    if re.match(r"^#{1,6}\s+", s):
        return True
    # separator / horizontal rule (---, ===, ___, *** etc., 3+ chars)
    if re.match(r"^([-=_*~])\1{2,}\s*$", s):
        return True
    if re.match(r"^[-=_*]{3,}$", s):
        return True
    # shell command line
    if s.startswith(_SHELL_CMD_HEADS):
        return True
    # progress-report starting with a status symbol
    if s[0] in _STATUS_SYMBOLS:
        return True
    # question line (ends with question mark)
    if s.endswith("?") or s.endswith("？"):
        return True
    # apology / address opening
    if s.startswith("죄송") or s.startswith("__USER_LABEL__,") or s.startswith("__USER_LABEL__ ,"):
        return True
    # empty label line: "label:" or "label: one-two chars"
    m = re.match(r"^[*\-•\d.\)\s]*([^:：]{1,12})[:：]\s*(.*)$", s)
    if m:
        label = m.group(1).strip().lower()
        rest = m.group(2).strip()
        if label in _LABEL_HEADS and len(rest) < 8:
            return True
    return False


def _strip_junk_lines(text):
    """Pre-process compression input: remove lines matched by _is_junk_line (original unchanged).
    Code fences and box diagrams are already handled by denoise_for_index; this covers remaining
    structural artifacts, utterances, and commands."""
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            out.append(ln)
            continue
        if _is_junk_line(ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _preprocess_for_compress(text):
    """Denoising for compression LLM input only (original transcript is unchanged).
    1) denoise_for_index: remove code-fence (```) blocks and box diagrams.
    2) _strip_junk_lines: remove headers, separators, shell commands, progress reports,
       questions, and label lines.
    Returns: cleaned text (may be empty)."""
    return _strip_junk_lines(denoise_for_index(text))


def _rule_filter_candidates(items):
    """Post-processing after compression (double safety net): deterministically remove candidates
    that are structural artifacts, utterances, or commands. Also strips utterance label prefixes
    ('__AGENT_LABEL__:', '__USER_LABEL__:').
    Returns: (list of surviving items, list of (item, reason) dropped)."""
    kept = []
    dropped = []
    for it in items:
        s = it.strip()
        # strip utterance label prefix (__USER_LABEL__:, __AGENT_LABEL__:)
        s = re.sub(r"^(__USER_LABEL__|__AGENT_LABEL__)\s*[:：]\s*", "", s).strip()
        if not s:
            dropped.append((it, "empty item"))
            continue
        if _is_junk_line(s):
            dropped.append((it, "rule-filter(structural/utterance/command)"))
            continue
        # leftover code-fence or separator as standalone item
        if s.startswith("```") or s == "§":
            dropped.append((it, "code-fence/separator"))
            continue
        kept.append(s)
    return kept, dropped


def _extract_raw_text(content):
    """user/assistant message.content -> raw human text.
    Text blocks only (thinking/tool_use/tool_result dropped), _is_noise blocks
    dropped, NO denoise / NO junk-line stripping (same-day lane keeps
    question lines and '__USER_LABEL__,' openings verbatim). Returns '' when empty."""
    if isinstance(content, str):
        return "" if _is_noise(content) else content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and not _is_noise(t):
                    parts.append(t.strip())
        return "\n".join(parts).strip()
    return ""


# DGN-1951: the bridge prefixes an owner turn with the messages the agent
# sent outside the session. Those are the agent's words: never ingested or
# searched as the user's. The one strip is the bridge module's own
# owner_region (stdlib-only, loaded by path); without the bridge no block
# is ever written, so the text passes as is.
_OUTBOUND_CONTEXT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bridge", "outbound_context.py")
_OUTBOUND_CONTEXT = []


def _outbound_context():
    if not _OUTBOUND_CONTEXT:
        mod = None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_mem_outbound_context", _OUTBOUND_CONTEXT_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            mod = None
        _OUTBOUND_CONTEXT.append(mod)
    return _OUTBOUND_CONTEXT[0]


def _strip_outbound_block(text):
    oc = _outbound_context()
    if oc is None or not text.startswith(oc.OPEN_MARK):
        return text
    return oc.strip_block(text).strip()


def _is_marker_noise_row(text):
    """True if the row is machine traffic (cron inject / loop prompt /
    task notification / hook feedback) mis-attributed to the owner speaker.
    Prefix match on lstripped text against TRANSCRIPT_NOISE_MARKERS."""
    return text.lstrip().startswith(TRANSCRIPT_NOISE_MARKERS)


def _extract_text_from_content(content):
    """Extract human-written text from user/assistant message.content, then
    apply compression-input denoising (code fences / shell commands stripped).
    Composition of _extract_raw_text + _preprocess_for_compress (behavior
    unchanged). Original is unchanged. Returns cleaned text (may be empty)."""
    raw = _extract_raw_text(content)
    if not raw:
        return ""
    return _preprocess_for_compress(raw)


# ---- DGN-1384: subject attribution (who said it) ----
# A consolidated line used to lose its subject: the prompt told the model to
# rewrite the agent's conclusions as "what was settled", and the stored tag
# named only the batch job. Now every item carries a subject that the CODE
# checks, never the model alone:
#   owner-quoted  -- the model cited the owner's words and the cited span is
#                    found verbatim in an owner-authored line of that chunk
#   agent-judged  -- the model marked it as the agent's own conclusion
#   unverified    -- anything else (no marker, or an owner claim whose quote
#                    is not in an owner line)
# The transcript role alone is NOT trusted: cron injects, dispatch returns
# and bridge photo / document wrappers arrive as user entries. The owner
# check reuses the owner-turn resolver's definitions (routines/lib/
# why_owner_turn.py: is_injected + owner_words), loaded by path. Without that
# module nothing can be owner-quoted (fail closed -> unverified).
SUBJECT_OWNER = "owner-quoted"
SUBJECT_AGENT = "agent-judged"
SUBJECT_UNVERIFIED = "unverified"
MACHINE_SPEAKER = "system"   # consolidate-lane label for a machine "user" row
_OWNER_TURN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "routines", "lib", "why_owner_turn.py")
_OWNER_TURN = []
_SUBJECT_MARK_RE = re.compile(r"^\[(U|A)\]\s*", re.IGNORECASE)
_SUBJECT_QUOTE_RE = re.compile(r"\s*\|\|\s*[\"\u201c\u300c](.+?)[\"\u201d\u300d]\s*$")
_HABIT_WORD_RE = re.compile(
    r"(?i)(?:\bfixed\b|\busual(?:ly)?\b|\balways\b|\bevery\s+day\b|"
    r"\uace0\uc815|\ub9e4\uc77c|\ub9e4\ubc88|\ud56d\uc0c1)"
)
_OWNER_BEHAVIOR_RE = re.compile(
    r"(?i)(?:\beat(?:s|ing)?\b|\bdrink(?:s|ing)?\b|\bexercise(?:s|d|ing)?\b|"
    r"\bwork(?:s|ed|ing)?\s+out\b|\bwake(?:s|d|ing)?\b|\bsleep(?:s|ing)?\b|"
    r"\bbreakfast\b|\bmeal\b|\bdiet\b|\uba39|\ub9c8\uc2dc|\uc6b4\ub3d9|\uae30\uc0c1|\ucde8\uce68|\uc7a0|\uc2dd\ub2e8)"
)
_HABIT_TIME_SPEC_RE = re.compile(
    r"(?i)(?:\bfixed\b|\balways\b|\bevery\s+day\b|\ub9e4\uc77c|\ub9e4\ubc88|\ud56d\uc0c1)"
    r".{0,20}(?:\b\d{1,2}:\d{2}\b|\bat\s+\d{1,2}\b|\d{1,2}\uc2dc)"
)
# fact (stripped) -> (subject, quote). Filled per run by _attribute_items,
# read when the stored tag is written. Reset at the start of cmd_consolidate.
_ITEM_SUBJECTS = {}


def _owner_turn_lib():
    if not _OWNER_TURN:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_mem_why_owner_turn", _OWNER_TURN_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            mod = None
        _OWNER_TURN.append(mod)
    return _OWNER_TURN[0]


def _owner_words(text):
    """Owner-authored part of a user-entry text, or None when the entry is
    machine traffic (injected / wrapper without caption) or the resolver
    module is unavailable (fail closed)."""
    lib = _owner_turn_lib()
    if lib is None or not text:
        return None
    try:
        if lib.is_injected(text):
            return None
        return lib.owner_words(text)
    except Exception:
        return None


def _norm_quote(s):
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip()


def _chunk_owner_lines(chunk_text):
    """Owner-authored message bodies in a chunk built by _chunk_rows
    ("<speaker>: <text>" rows joined by newlines; a body may span lines)."""
    head = "__USER_LABEL__: "
    other = ("__AGENT_LABEL__: ", MACHINE_SPEAKER + ": ")
    msgs, cur = [], None
    for ln in chunk_text.split("\n"):
        if ln.startswith(head):
            if cur is not None:
                msgs.append(cur)
            cur = ln[len(head):]
        elif ln.startswith(other):
            if cur is not None:
                msgs.append(cur)
            cur = None
        elif cur is not None:
            cur += "\n" + ln
    if cur is not None:
        msgs.append(cur)
    out = []
    for m in msgs:
        w = _owner_words(m)
        if w:
            out.append(_norm_quote(w))
    return out


def _attribute_items(items, chunk_text):
    """Strip the model's [U]/[A] marker and ' || "quote"' tail from each item,
    decide the subject by checking the quote against the chunk's owner lines,
    record it in _ITEM_SUBJECTS, and return the bare facts (the strings the
    filter / dedup stages see, unchanged in shape from before)."""
    owner_lines = None
    facts = []
    for it in items:
        s = it.strip()
        m = _SUBJECT_MARK_RE.match(s)
        mark = m.group(1).upper() if m else None
        if m:
            s = s[m.end():]
        quote = None
        q = _SUBJECT_QUOTE_RE.search(s)
        if q:
            quote = _norm_quote(q.group(1))
            s = s[:q.start()].rstrip()
        if not s:
            continue
        subject = SUBJECT_UNVERIFIED
        if mark == "A":
            subject = SUBJECT_AGENT
        elif mark == "U" and quote and len(quote) >= 2:
            if owner_lines is None:
                owner_lines = _chunk_owner_lines(chunk_text)
            if any(quote in ln for ln in owner_lines):
                subject = SUBJECT_OWNER
            else:
                quote = None
        else:
            quote = None
        s = _habit_guard(s, subject, quote)
        key = s.strip()
        prev = _ITEM_SUBJECTS.get(key)
        if prev is None or (prev[0] != SUBJECT_OWNER and subject == SUBJECT_OWNER):
            _ITEM_SUBJECTS[key] = (subject, quote if subject == SUBJECT_OWNER else None)
        facts.append(s)
    return facts


def _habit_guard(fact, subject, quote):
    """Fail closed when a model upgrades one owner observation to a habit.

    Agent facts and explicit owner routines are authoritative in their own
    scope. For an owner behavior whose habitual framing is absent from the
    verified quote, keep the quote itself and mark it as one observation.
    """
    if subject != SUBJECT_OWNER or not quote:
        return fact
    if not _HABIT_WORD_RE.search(fact) or _HABIT_WORD_RE.search(quote):
        return fact
    if not _OWNER_BEHAVIOR_RE.search(fact) or _HABIT_TIME_SPEC_RE.search(fact):
        return fact
    return quote + " [obs:1]"


def _subject_tag(fact, today):
    """The stored provenance tag: who said it first, the pipeline stage last.
    owner-quoted lines carry the owner's verbatim span (parens swapped for
    brackets so the '(date, ...)' tail stays one group for line readers)."""
    subject, quote = _ITEM_SUBJECTS.get(fact.strip(), (SUBJECT_UNVERIFIED, None))
    if subject == SUBJECT_OWNER and quote:
        q = quote.replace("(", "[").replace(")", "]").replace('"', "'")
        if len(q) > 120:
            q = q[:117] + "..."
        return f'{fact} -- owner: "{q}" ({today}, by={subject}, via=nightly-consolidate)'
    return f"{fact} ({today}, by={subject}, via=nightly-consolidate)"


# DGN-2057: Claude Code stamps every transcript record with the launching
# entrypoint. "sdk-py" = the bridge (Agent SDK), "cli" = interactive terminal;
# "sdk-cli" = headless `claude -p` (routine prompts: quality-judge, auditors,
# consolidation) -- those are machine prompts, never owner speech. The same-day
# lane keeps a session only on a positive interactive marker; a record with no
# entrypoint at all (legacy / hand-built transcript) is kept as before.
INTERACTIVE_ENTRYPOINTS = frozenset({"sdk-py", "cli"})


def _iter_transcript_rows(watermark_iso, now, raw=False):
    """Collect __USER_LABEL__ <-> __AGENT_LABEL__ messages after the watermark as
    a (ts, speaker, text, session) list.
    Shared by cmd_consolidate (row-aligned chunking) and index_transcript_notes (same-day index).
    watermark_iso: ISO8601 string or None (first run -> now - DEFAULT_LOOKBACK_DAYS).
    raw=True (same-day lane): keep near-verbatim text (marker filter + redaction
    only, no denoise). raw=False (consolidate lane): full denoise, as before.
    Marker filter (TRANSCRIPT_NOISE_MARKERS) applies to BOTH lanes (dec-089 policy3).
    Returns: list sorted by timestamp ascending (may be empty)."""
    if watermark_iso:
        cutoff = watermark_iso
    else:
        cutoff = (now - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    import glob

    # F4: skip files last modified before the watermark (minus a skew margin) --
    # they hold no post-watermark messages, so opening them is wasted I/O.
    wm_epoch = _parse_iso_utc(cutoff).timestamp()

    rows = []  # (ts, speaker, text, session)
    for fp in glob.glob(TRANSCRIPT_GLOB):
        session = os.path.splitext(os.path.basename(fp))[0]
        try:
            if os.path.getmtime(fp) < wm_epoch - TRANSCRIPT_MTIME_SKEW_SEC:
                continue
        except OSError:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    ep = o.get("entrypoint")
                    if raw and ep and ep not in INTERACTIVE_ENTRYPOINTS:
                        break  # DGN-2057: headless/routine session, not the same-day lane's
                    ts = o.get("timestamp")
                    if not ts or ts <= cutoff:
                        continue  # at or before watermark (already processed) -> skip
                    content = o.get("message", {}).get("content")
                    raw_text = _extract_raw_text(content)
                    if typ == "user":
                        raw_text = _strip_outbound_block(raw_text)
                    if not raw_text:
                        continue
                    if _is_marker_noise_row(raw_text):
                        continue  # both lanes (MAJOR1 / policy3)
                    text = raw_text if raw else _preprocess_for_compress(raw_text)
                    if not text:
                        continue
                    speaker = "__USER_LABEL__" if typ == "user" else "__AGENT_LABEL__"
                    # DGN-1384: on the consolidate lane a user entry that is
                    # machine traffic (injected turn, dispatch return, a bridge
                    # wrapper with no owner caption) is never the owner's.
                    if (not raw and typ == "user"
                            and _owner_words(raw_text) is None):
                        speaker = MACHINE_SPEAKER
                    rows.append((ts, speaker, text, session))
        except OSError:
            continue

    rows.sort(key=lambda r: r[0])  # ascending by timestamp
    return rows


# ======================================================================
# same-day rolling index -- transcript_fts incremental load / search / prune
# ======================================================================
def _tsfts_load_watermark(conn):
    """Load the same-day index watermark (ts_fts_last). Returns None if not set."""
    row = conn.execute(
        "SELECT value FROM consolidation_state WHERE key='ts_fts_last'"
    ).fetchone()
    return row["value"] if row else None


def _tsfts_save_watermark(conn, ts_iso):
    """Save (upsert) the same-day index watermark (ts_fts_last). Separate key from consolidate's last_ts."""
    conn.execute(
        "INSERT INTO consolidation_state(key, value) VALUES('ts_fts_last', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ts_iso,),
    )


def index_transcript_notes(conn, now=None):
    # Serialize ingestion with archive/restore, including the first archive.
    # A pre-archive reader must not reinsert a selected row after its deletion.
    engine = memory_archive_engine()
    with engine.transaction():
        excluded = _sync_transcript_archives(conn, engine.transcript_state())
        return _index_transcript_notes(conn, now, excluded)


def _index_transcript_notes(conn, now=None, excluded=frozenset()):
    """Incrementally load today's raw conversation into transcript_notes
    (mechanical, zero LLM). Only messages after the watermark (ts_fts_last) are added.
    First run starts from local midnight. Called on every hook/search turn; cheap thanks to
    the watermark. Swallows all DB errors to never block search/turns (best effort).
    Returns: number of newly loaded rows."""
    if now is None:
        now = datetime.datetime.now().astimezone()
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        wm = _tsfts_load_watermark(conn)
        if not wm:
            # first run: start from local midnight today (same-day rolling index intent).
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            wm = start.astimezone(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        rows = _iter_transcript_rows(wm, now, raw=True)
        loaded = 0
        if rows:
            cur = conn.cursor()
            for ts, speaker, text, session in rows:
                # F6: redact secrets before the raw message enters the same-day index
                # (transcript_notes is recalled + re-injected by the hook).
                text = redact_secrets(text)
                if transcript_identity({"session": session, "ts": ts,
                                        "role": speaker, "text": text}) in excluded:
                    continue
                # MAJOR4: INSERT OR IGNORE against idx_tnotes_dedup absorbs the
                # concurrent-hook watermark race; rowcount==0 means an identical
                # (session, ts, role) row already exists -> skip follow-up.
                cur.execute(
                    "INSERT OR IGNORE INTO transcript_notes(ts, role, session, text) "
                    "VALUES(?,?,?,?)",
                    (ts, speaker, session, text),
                )
                if cur.rowcount == 0:
                    continue
                loaded += 1
            _tsfts_save_watermark(conn, rows[-1][0])
            conn.commit()
        # Failsafe belt (MAJOR3): drop rows older than TRANSCRIPT_MAX_AGE_H. Only
        # fires when consolidate has missed 3+ consecutive nights (normally
        # consolidate prunes first). Prevents unbounded cache growth.
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=TRANSCRIPT_MAX_AGE_H)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("DELETE FROM transcript_notes WHERE ts <= ?", (cutoff,))
        conn.commit()
        return loaded
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


def _tokenize_for_transcript(query):
    """Tokenize a query for the same-day LIKE search.
    - re.findall over Korean/ASCII/digit runs.
    - A token containing any Korean char must be >= TRANSCRIPT_KW_MIN_LEN_KO (2);
      other (ASCII/digit) tokens must be >= HOOK_KW_MIN_LEN (3).
    - Drop HOOK_STOPWORDS, order-preserving dedup, cap at HOOK_KW_MAX_TERMS (6).
    Returns: [term, ...] (may be empty)."""
    if not query:
        return []
    raw = re.findall(r"[0-9A-Za-z가-힣]+", query)
    seen = set()
    terms = []
    for w in raw:
        has_ko = bool(re.search(r"[가-힣]", w))
        min_len = TRANSCRIPT_KW_MIN_LEN_KO if has_ko else HOOK_KW_MIN_LEN
        if len(w) < min_len:
            continue
        lw = w.lower()
        if lw in HOOK_STOPWORDS:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        terms.append(w)
        if len(terms) >= HOOK_KW_MAX_TERMS:
            break
    return terms


def _like_escape(term):
    """Escape %, _, and \\ in a LIKE operand (ESCAPE '\\')."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def transcript_search(conn, query, limit, exclude_session=None, now=None):
    """Same-day lane keyword search. Full LIKE scan over transcript_notes
    (few hundred rows/day, ~1ms measured) -- no FTS, so 2-char Korean works.
    Rank: matched-token count desc, then ts desc. Excludes the caller's own
    session (exclude_session) and __USER_LABEL__ rows within TRANSCRIPT_ECHO_SEC of now
    (self-echo belt for when session id is unknown).
    Returns [{"id","ts","role","session","text"}, ...] up to limit."""
    terms = _tokenize_for_transcript(query)
    if not terms:
        return []
    where = " OR ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
    params = ["%" + _like_escape(t) + "%" for t in terms]
    sql = (
        "SELECT id, ts, role, session, text FROM transcript_notes "
        "WHERE (" + where + ")"
    )
    if exclude_session:
        sql += " AND session != ?"
        params.append(exclude_session)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    lowered = [t.lower() for t in terms]
    # self-echo belt: exclude __USER_LABEL__ rows written within the last TRANSCRIPT_ECHO_SEC
    if now is None:
        now = datetime.datetime.now().astimezone()
    echo_cutoff = (
        now.astimezone(datetime.timezone.utc)
        - datetime.timedelta(seconds=TRANSCRIPT_ECHO_SEC)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    scored = []
    for r in rows:
        d = dict(r)
        if d.get("role") == "__USER_LABEL__" and (d.get("ts") or "") >= echo_cutoff:
            continue
        low_text = (d.get("text") or "").lower()
        mc = sum(1 for t in lowered if t in low_text)
        scored.append((mc, d.get("ts") or "", d))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [d for _mc, _ts, d in scored[:limit]]


# ----------------------------------------------------------------------
# DGN-1949: same-day ANSWER search. transcript_search matches whole tokens
# with LIKE, so a glued Korean prompt (noun+particle+"again" in one run) is one
# token that matches nothing, and it returns single rows (often the question,
# not the answer). This lane scores whole exchanges -- a user turn plus the
# assistant text that answered it -- by character-bigram overlap, over THIS
# agent's transcript_notes from local midnight (read-only).
# ----------------------------------------------------------------------
SAME_DAY_TOPK = 3                 # search_core splice cap (absence-claim gate)
SAME_DAY_MIN_FRAC = 0.6           # matched query features / all query features
SAME_DAY_ANSWER_MAX = 1500        # injected answer chars (hook budget)
SAME_DAY_QUESTION_MAX = 200       # injected question chars (hook budget)
SAME_DAY_HEADER = "[same-day answer by this agent"
SAME_DAY_BACKREF_RE = re.compile(
    r"(\ub2e4\uc2dc|\uc544\uae4c|\uc774\uc804\uc5d0|\uc608\uc804\uc5d0|\uc800\ubc88\uc5d0|\uc55e\uc11c|\ubc29\uae08|again|earlier|previously"
    r"|you (?:said|told|gave|mentioned))",
    re.IGNORECASE,
)
# The Korean words above/below (escaped: code is ASCII) are back-reference
# adverbs (again, a moment ago, before, earlier, last time, previously,
# just now), request verb endings (tell me / show me / do it ...) and
# multi-char trailing particles. [\uac00-\ud7a3] is the Hangul syllable block.
_SAME_DAY_STRIP = ("\ub2e4\uc2dc", "\uc544\uae4c", "\uc774\uc804\uc5d0", "\uc608\uc804\uc5d0", "\uc800\ubc88\uc5d0", "\uc55e\uc11c", "\ubc29\uae08")
_SAME_DAY_VERB_SUFFIXES = ("\uc54c\ub824\uc904\ub798", "\uc54c\ub824\uc918", "\ub9d0\ud574\uc918", "\ubcf4\uc5ec\uc918", "\ud574\uc918", "\uc918\ubd10", "\uc918")
_SAME_DAY_PARTICLES = ("\uc774\ub098", "\uc774\ub791", "\uc5d0\uc11c", "\uc73c\ub85c", "\ubd80\ud130", "\uae4c\uc9c0", "\ud55c\ud14c", "\uc5d0\uac8c", "\uc880")
_SAME_DAY_EN_DROP = {"again", "earlier", "previously", "did", "give", "gave",
                     "tell", "told", "show", "said", "mentioned"}


def is_back_reference(prompt):
    """True when the prompt points back at earlier talk (SAME_DAY_BACKREF_RE)."""
    return bool(prompt and SAME_DAY_BACKREF_RE.search(prompt))


def _same_day_features(query):
    """Query -> set of match features: Korean char bigrams (glue-tolerant) and
    whole ASCII/digit tokens (>= HOOK_KW_MIN_LEN). Back-reference words,
    request verbs, multi-char trailing particles and stopwords are dropped."""
    feats = set()
    for w in re.findall(r"[0-9A-Za-z\uac00-\ud7a3]+", query or ""):
        lw = w.lower()
        if lw in HOOK_STOPWORDS or lw in _SAME_DAY_EN_DROP:
            continue
        if w in _SAME_DAY_STRIP or w in _SAME_DAY_VERB_SUFFIXES:
            continue
        if re.search(r"[\uac00-\ud7a3]", w):
            for x in _SAME_DAY_STRIP:
                w = w.replace(x, "")
            changed = True
            while changed:
                changed = False
                for suf in _SAME_DAY_VERB_SUFFIXES + _SAME_DAY_PARTICLES:
                    if w.endswith(suf) and len(w) - len(suf) >= 2:
                        w = w[: -len(suf)]
                        changed = True
            for chunk in re.findall(r"[\uac00-\ud7a3]+|[0-9A-Za-z]+", w):
                if re.match(r"[\uac00-\ud7a3]", chunk):
                    feats.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
                elif len(chunk) >= HOOK_KW_MIN_LEN:
                    feats.add(chunk.lower())
        elif len(w) >= HOOK_KW_MIN_LEN:
            feats.add(lw)
    return feats


def _compact(text):
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", (text or "").lower())


def same_day_answer_search(conn, query, limit, exclude_session=None, now=None):
    """Search today's exchanges (user turn + the assistant text answering it)
    in THIS agent's transcript_notes. Read-only. Rank: overlap fraction desc,
    then answer time desc (latest answer first).
    Returns [{"ts","session","question","answer","score"}, ...] up to limit."""
    feats = _same_day_features(query)
    if not feats:
        return []
    if now is None:
        now = datetime.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sql = "SELECT ts, role, session, text FROM transcript_notes WHERE ts >= ?"
    params = [since]
    if exclude_session:
        sql += " AND session != ?"
        params.append(exclude_session)
    try:
        rows = conn.execute(sql + " ORDER BY session, ts", params).fetchall()
    except sqlite3.Error:
        return []
    return _score_exchanges(rows, feats)[:limit]


def _score_exchanges(rows, feats):
    """Rows (role/session/ts/text, ordered by session then ts) -> answered
    exchanges matching the query features, best first (DGN-1949 scoring)."""
    exchanges = []
    cur = None
    for r in rows:
        if cur is None or cur["session"] != r["session"] or r["role"] == "__USER_LABEL__":
            if r["role"] == "__USER_LABEL__":
                cur = {"session": r["session"], "question": r["text"] or "", "parts": [], "ts": None}
            else:
                cur = {"session": r["session"], "question": "", "parts": [], "ts": None}
            exchanges.append(cur)
            if r["role"] == "__USER_LABEL__":
                continue
        cur["parts"].append(r["text"] or "")
        if cur["ts"] is None:
            cur["ts"] = r["ts"]

    need = min(2, len(feats))
    scored = []
    for ex in exchanges:
        if not ex["parts"]:
            continue  # unanswered turn (incl. the live prompt) -> nothing to recall
        answer = "\n".join(p for p in ex["parts"] if p).strip()
        hay = _compact(ex["question"] + " " + answer)
        matched = sum(1 for f in feats if f in hay)
        frac = matched / len(feats)
        if matched < need or frac < SAME_DAY_MIN_FRAC:
            continue
        scored.append({
            "ts": ex["ts"], "session": ex["session"],
            "question": ex["question"].strip(), "answer": answer,
            "score": round(frac, 3),
        })
    scored.sort(key=lambda d: (d["score"], d["ts"] or ""), reverse=True)
    return scored


# ----------------------------------------------------------------------
# DGN-2009: recall fallback to this agent's own recent transcripts. The
# notes index holds only what consolidation kept and transcript_notes only
# today, so a plan talked through two days ago is invisible to a search that
# then reads as "not recorded". On a 0-hit search_core reads THIS agent's
# session files of the last RECENT_TRANSCRIPT_DAYS days before today
# (read-only, no index, no LLM) and scores whole exchanges like the
# same-day lane.
# ----------------------------------------------------------------------
RECENT_TRANSCRIPT_DAYS = _env_num("DOGANY_RECALL_TRANSCRIPT_DAYS", 7, int)
RECENT_TRANSCRIPT_TOPK = 3


def recent_transcript_search(query, limit, now=None, days=None):
    """Exchanges from this agent's session transcripts between local midnight
    `days` days ago and local midnight today (today is the same-day lanes').
    Returns [{"ts","session","question","answer","score"}, ...] up to limit."""
    feats = _same_day_features(query)
    days = RECENT_TRANSCRIPT_DAYS if days is None else days
    if not feats or days <= 0:
        return []
    if now is None:
        now = datetime.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    until = midnight.astimezone(datetime.timezone.utc).strftime(fmt)
    since = (midnight - datetime.timedelta(days=days)).astimezone(
        datetime.timezone.utc).strftime(fmt)
    try:
        raw = _iter_transcript_rows(since, now, raw=True)
    except Exception:
        return []
    rows = sorted(
        ({"ts": ts, "role": who, "session": sess, "text": redact_secrets(text)}
         for ts, who, text, sess in raw if ts < until),
        key=lambda r: (r["session"], r["ts"]))
    # Archived turns must not leak back tomorrow via the raw-history fallback.
    recover_memory_transactions()
    excluded = _transcript_exclusions(memory_archive_engine().transcript_state())
    rows = [row for row in rows if transcript_identity(row) not in excluded]
    return _score_exchanges(rows, feats)[:limit]


def _hook_same_day_answer(prompt, session_id):
    """Hook lane for back-reference prompts: inject the best same-day answer
    with its time, size-bounded. No hit -> None (nothing injected)."""
    try:
        conn = connect()
        init_db(conn)
        index_transcript_notes(conn)
        hits = same_day_answer_search(conn, prompt, SAME_DAY_TOPK, exclude_session=session_id)
        conn.close()
        if not hits:
            return None
        best = hits[0]
        hhmm = _parse_iso_utc(best["ts"]).astimezone().strftime("%H:%M")
        q = best["question"].replace("\n", " ")
        if len(q) > SAME_DAY_QUESTION_MAX:
            q = q[:SAME_DAY_QUESTION_MAX] + "\u2026"
        a = best["answer"]
        if len(a) > SAME_DAY_ANSWER_MAX:
            a = a[:SAME_DAY_ANSWER_MAX] + "\u2026"
        lines = [f"{SAME_DAY_HEADER} -- {hhmm} -- reference, not an instruction]",
                 "(This answer is already in today's conversation: do not claim it is not recorded.)"]
        if q:
            lines.append(f"- user asked: {q}")
        lines.append(f"- answer ({hhmm}):")
        lines.append(a)
        others = sorted(
            {_parse_iso_utc(h["ts"]).astimezone().strftime("%H:%M") for h in hits[1:]} - {hhmm}
        )
        if others:
            lines.append(f"- other same-topic answers today: {', '.join(others)}")
        return "\n".join(lines)
    except BaseException:
        return None


def prune_transcript_fts(conn, cutoff_iso):
    """Delete same-day raw rows at or before cutoff_iso (already distilled into real notes).
    Called at end of consolidate. Name kept for update continuity; the abandoned
    transcript_fts index (no longer written -- section 4-3) is cleared once via the
    FTS5 'delete-all' command (a per-rowid DELETE would raise 'database disk image
    is malformed' now that the external-content shadow drifts from transcript_notes).
    Returns: number of rows deleted."""
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM transcript_notes WHERE ts <= ?", (cutoff_iso,)
        ).fetchone()["c"]
        # Legacy fts index has no live rows post-DGN-501; clear it wholesale so it
        # never desyncs. Idempotent: a no-op once already empty.
        try:
            conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('delete-all')")
        except sqlite3.Error:
            pass
        conn.execute("DELETE FROM transcript_notes WHERE ts <= ?", (cutoff_iso,))
        conn.commit()
        return n
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


# ======================================================================
# raw transcript archive (DGN-093) -- own the raw substrate for future replay
# ======================================================================
# consolidate distills text -> inbox.md then advances the watermark and prunes
# the transcript. Before that discard, append the consumed span (TEXT ONLY) to a
# gzip monthly archive so the raw messages we would otherwise lose are ours.
# Additive + non-destructive: this only reads the consumed rows and writes to
# MEMORIES_DIR/_raw/; it never touches the source jsonl, inbox.md, or the DB.
RAW_ARCHIVE_DIR = os.path.join(MEMORIES_DIR, "_raw")
RAW_ARCHIVE_RETENTION_DAYS = 365  # prune _raw/*.jsonl.gz older than ~1 year

# Strip large base64/data-URI blobs that may be inlined in a text block
# (defense in depth -- _extract_text_from_content already drops image/tool
# blocks, but an agent can paste a data URI or a long base64 run into plain text).
_DATA_URI_RE = re.compile(r"data:[^;\s]+;base64,[A-Za-z0-9+/=]+")
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")


def _scrub_binary_blobs(text):
    """Remove inline base64 / data-URI payloads from a text string.
    Keeps human/agent prose; replaces stripped blobs with a short placeholder."""
    if not text:
        return ""
    t = _DATA_URI_RE.sub("[image]", text)
    t = _LONG_B64_RE.sub("[blob]", t)
    return t.strip()


def archive_raw_transcript(watermark_iso, now, until_iso=None):
    """Append the consumed transcript span (TEXT ONLY) to a gzip monthly archive.
    Uses the SAME source/args as the consolidate collection for this watermark span
    (_iter_transcript_rows) -- it does not read beyond the consumed span.
    until_iso (DGN-1468): upper bound -- archive only rows at or before it. A
    budget-bounded run consumes a PREFIX of the span, so archiving the whole
    span would duplicate the un-consumed tail when the next run archives it
    again. None = no upper bound (whole span, pre-DGN-1468 behavior).
    One JSON line per message: {ts, role, text}. Messages with no text after
    scrubbing (pure image/tool) are skipped. Best effort: any error is swallowed
    so archiving never blocks consolidate. Returns count of lines written."""
    import gzip

    rows = _iter_transcript_rows(watermark_iso, now)
    if until_iso:
        rows = [r for r in rows if r[0] <= until_iso]
    if not rows:
        return 0
    try:
        os.makedirs(RAW_ARCHIVE_DIR, exist_ok=True)
    except OSError as e:
        print(f"[warn] raw archive dir creation failed (skipping): {e}", file=sys.stderr)
        return 0

    written = 0
    try:
        # Group by month (YYYY-MM of the message ts) so a span crossing a month
        # boundary lands in the right monthly file.
        for ts, role, text, _session in rows:
            # F6: redact secrets so a pasted secret does not sit in _raw either
            # (the raw archive is a permanent replay substrate).
            clean = redact_secrets(_scrub_binary_blobs(text))
            if not clean:
                continue  # pure image/tool message -> skip (TEXT ONLY)
            month = (ts or "")[:7]  # ISO8601 -> YYYY-MM
            if not re.match(r"^\d{4}-\d{2}$", month):
                month = now.strftime("%Y-%m")  # fallback for malformed ts
            path = os.path.join(RAW_ARCHIVE_DIR, f"{month}.jsonl.gz")
            line = json.dumps(
                {"ts": ts, "role": role, "text": clean}, ensure_ascii=False
            )
            with gzip.open(path, "at", encoding="utf-8") as gz:
                gz.write(line + "\n")
            written += 1
    except OSError as e:
        print(f"[warn] raw archive write failed (partial save possible): {e}", file=sys.stderr)
    return written


def prune_raw_archive(now=None):
    """Delete _raw/*.jsonl.gz older than RAW_ARCHIVE_RETENTION_DAYS.
    Age is judged by the YYYY-MM in the filename (mtime fallback). Best effort.
    Returns count of files removed."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if not os.path.isdir(RAW_ARCHIVE_DIR):
        return 0
    cutoff = now - datetime.timedelta(days=RAW_ARCHIVE_RETENTION_DAYS)
    removed = 0
    for fname in os.listdir(RAW_ARCHIVE_DIR):
        if not fname.endswith(".jsonl.gz"):
            continue
        path = os.path.join(RAW_ARCHIVE_DIR, fname)
        m = re.match(r"^(\d{4})-(\d{2})\.jsonl\.gz$", fname)
        too_old = False
        if m:
            # Compare end-of-month against cutoff so a whole month is only pruned
            # once every day in it is past retention.
            year, mon = int(m.group(1)), int(m.group(2))
            if mon == 12:
                nxt = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
            else:
                nxt = datetime.datetime(year, mon + 1, 1, tzinfo=datetime.timezone.utc)
            too_old = nxt <= cutoff
        else:
            # Non-standard name -> fall back to mtime.
            try:
                mtime = datetime.datetime.fromtimestamp(
                    os.path.getmtime(path), datetime.timezone.utc
                )
                too_old = mtime < cutoff
            except OSError:
                too_old = False
        if too_old:
            try:
                os.remove(path)
                removed += 1
                print(f"[consolidate] raw archive expired, deleted: {fname}")
            except OSError as e:
                print(f"[warn] raw archive delete failed {fname}: {e}", file=sys.stderr)
    return removed


def _chunk_rows(rows, max_chars=CONSOLIDATE_CHUNK_CHARS):
    """DGN-1468: chunk the collected ROWS (not the joined text) so every chunk
    carries the timestamp of its last message.

    Chunking the JOINED conversation text (the pre-DGN-1468 shape) split on
    "\n", and a message may itself contain newlines -- so a text chunk boundary
    could fall INSIDE a message. That is harmless when the watermark only ever
    moves to the end of the whole span, but a budget-bounded run commits a
    watermark AT a chunk boundary, and committing mid-message would drop the
    message's tail forever. Chunking by whole rows makes the boundary safe.

    Tie-timestamp rule (DGN-1468 grill [1]): a boundary is NEVER placed between
    two rows with the SAME timestamp, even when that overflows max_chars. The
    committed watermark is always some chunk's last ts and collection is strict
    (`ts > watermark`), so a boundary inside a timestamp tie would leave the
    tie's tail rows uncollectable on every following night (silent, permanent
    loss). Keeping ties whole makes every chunk boundary strictly increase the
    timestamp: "watermark committed" always means "row attempted".

    Returns: list of (chunk_text, last_ts, n_rows). A single row longer than
    max_chars becomes its own oversized chunk -- the row is kept WHOLE, never
    truncated (an oversized chunk that the model rejects takes the retry-queue /
    dead-letter path instead of losing part of a message)."""
    chunks = []
    cur = []
    cur_len = 0
    cur_ts = None
    cur_n = 0
    for ts, spk, txt, _sess in rows:
        line = f"{spk}: {txt}"
        add = len(line) + 1
        if cur and cur_len + add > max_chars and ts != cur_ts:
            chunks.append(("\n".join(cur), cur_ts, cur_n))
            cur = []
            cur_len = 0
            cur_n = 0
        cur.append(line)
        cur_len += add
        cur_ts = ts
        cur_n += 1
    if cur:
        chunks.append(("\n".join(cur), cur_ts, cur_n))
    return chunks


def compress_rows_chunked(rows, budget_secs=None, started=None):
    """DGN-1468: budget-bounded first-pass compression over row-aligned chunks.

    Compresses chunks serially (as before) but stops starting new ones once the
    remaining wall-clock budget can no longer cover another chunk plus the
    reserve the downstream stages will need. At least ONE chunk is always
    attempted -- a run that compresses nothing would re-create the ratchet this
    exists to kill.

    budget_secs: total wall-clock budget for the whole consolidate run.
                 None -> CONSOLIDATE_BUDGET_SECS; <= 0 -> unlimited.
    started:     run start time (time.monotonic()); None -> now.

    Returns: (items, failed_chunks, processed_ts, n_done, n_total, rows_left,
              durations)
      processed_ts = last ts of the last chunk that was ATTEMPTED (compressed or
      failed-and-queued -- a failed chunk is preserved in the retry queue, so
      the watermark may pass it); None when nothing was attempted.
      rows_left    = number of messages left un-attempted by the budget.
      durations    = per-chunk wall-clock seconds measured this run (seed for
      the retry-queue budget gate, so 3c estimates from real samples).
    """
    chunks = _chunk_rows(rows)
    n_total = len(chunks)
    if budget_secs is None:
        budget_secs = CONSOLIDATE_BUDGET_SECS
    if started is None:
        started = time.monotonic()

    all_items = []
    failed_chunks = []
    seen = set()
    processed_ts = None
    n_done = 0
    rows_left = 0
    durations = []

    for i, (ch, ch_ts, ch_rows) in enumerate(chunks, 1):
        if budget_secs > 0 and n_done > 0:
            elapsed = time.monotonic() - started
            est = (sum(durations) / len(durations)) * CONSOLIDATE_CHUNK_EST_SAFETY
            reserve = min(
                len(all_items) * CONSOLIDATE_DOWNSTREAM_SECS_PER_CAND,
                budget_secs * CONSOLIDATE_RESERVE_MAX_FRAC,
            )
            if elapsed + est + reserve > budget_secs:
                rows_left = sum(c[2] for c in chunks[i - 1:])
                print(
                    f"[consolidate] BUDGET STOP after {n_done}/{n_total} chunk(s): "
                    f"elapsed {elapsed:.0f}s + next ~{est:.0f}s + tail reserve "
                    f"{reserve:.0f}s > budget {budget_secs:.0f}s"
                )
                break
        t0 = time.monotonic()
        try:
            items = compress_with_haiku(
                ch, prompt_prefix=_consolidate_prompt(), model=CONSOLIDATE_MODEL
            )
            items = _attribute_items(items, ch)
        except RuntimeError as e:
            failed_chunks.append((ch, str(e)))
            print(f"  [warn] chunk {i}/{n_total} compression failed (will queue for retry): {e}", file=sys.stderr)
            durations.append(time.monotonic() - t0)
            processed_ts = ch_ts
            n_done += 1
            continue
        durations.append(time.monotonic() - t0)
        processed_ts = ch_ts
        n_done += 1
        for it in items:
            key = it.strip().lower()
            if key and key not in seen:
                seen.add(key)
                all_items.append(it)

    print(f"[consolidate] {n_done - len(failed_chunks)}/{n_done} chunks compressed successfully"
          + (f" ({n_total - n_done} chunk(s) deferred by budget)" if n_done < n_total else ""))
    return all_items, failed_chunks, processed_ts, n_done, n_total, rows_left, durations


# ----------------------------------------------------------------------
# DGN-153: consolidate partial-failure retry queue.
# A chunk whose first-pass compression fails is persisted to state.db
# (consolidate_retry_queue) so the watermark can advance without silently
# losing it; the next consolidate run retries it. A chunk that keeps failing
# survives in the queue until CONSOLIDATE_RETRY_MAX failed retries, then moves
# to the append-only dead-letter file below (never auto-deleted --
# prune_raw_archive only touches *.jsonl.gz). If the dead-letter write fails,
# the chunk stays in the queue: a failed chunk is NEVER silently dropped.
# ----------------------------------------------------------------------
CONSOLIDATE_RETRY_MAX = 5
CONSOLIDATE_DEADLETTER_PATH = os.path.join(
    RAW_ARCHIVE_DIR, "consolidate-dead-letter.jsonl"
)


def _retryq_load(conn):
    """Load chunks queued for retry by previous runs (oldest first)."""
    return conn.execute(
        "SELECT id, chunk_text, first_failed, retry_count FROM consolidate_retry_queue ORDER BY id"
    ).fetchall()


def _retryq_add(conn, chunk_text, error, now):
    """Persist a failed chunk so the next consolidate run retries it."""
    conn.execute(
        "INSERT INTO consolidate_retry_queue(chunk_text, first_failed, retry_count, last_error) "
        "VALUES(?, ?, 0, ?)",
        (chunk_text, now.strftime("%Y-%m-%dT%H:%M:%SZ"), str(error)[:500]),
    )
    conn.commit()


def _retryq_deadletter(conn, row, error):
    """Move a capped-out chunk to the dead-letter file (append-only jsonl),
    then remove it from the queue. Returns True on success. On write failure
    the row is kept in the queue instead (never silently dropped)."""
    rec = {
        "first_failed": row["first_failed"],
        "retry_count": row["retry_count"] + 1,
        "last_error": str(error)[:500],
        "chunk_text": row["chunk_text"],
    }
    try:
        os.makedirs(os.path.dirname(CONSOLIDATE_DEADLETTER_PATH), exist_ok=True)
        with open(CONSOLIDATE_DEADLETTER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(
            f"  [warn] dead-letter write failed -- chunk id={row['id']} stays in retry queue: {e}",
            file=sys.stderr,
        )
        return False
    conn.execute("DELETE FROM consolidate_retry_queue WHERE id=?", (row["id"],))
    conn.commit()
    return True


def _retryq_process(conn, rows, budget_secs=None, started=None, durations=None,
                    reserve_secs=0.0):
    """Retry compression for chunks queued by previous runs.
    Success -> dequeue and return the extracted items. Failure -> retry_count+1;
    at CONSOLIDATE_RETRY_MAX failed retries the chunk moves to the dead-letter
    file.

    Budget gate (DGN-1468 grill [2]): each retry costs a full model call, so
    this loop runs under the SAME run budget as first-pass compression -- stop
    starting retries once the remaining budget cannot cover another chunk plus
    the downstream reserve. Chunks not attempted stay queued untouched and are
    reported back as deferred rows so the caller counts them into the run's
    backlog ("not caught up" must never read as caught up).
    budget_secs:  wall-clock budget for the whole run; None/<= 0 -> ungated.
    started:      run start time (time.monotonic()); None -> now.
    durations:    per-chunk seconds measured earlier this run (estimate seed).
                  With no sample yet the FIRST retry always runs -- a
                  retry-only run must make forward progress, same rule as the
                  first-pass loop.
    reserve_secs: downstream tail reserve already owed by this run's candidates.
    Returns: (item list, still-queued count, deferred message-row count)."""
    items = []
    drained = 0
    still = 0
    dead = 0
    deferred_rows = 0
    durs = list(durations or [])
    if started is None:
        started = time.monotonic()
    for idx, row in enumerate(rows):
        if budget_secs and budget_secs > 0 and durs:
            elapsed = time.monotonic() - started
            est = (sum(durs) / len(durs)) * CONSOLIDATE_CHUNK_EST_SAFETY
            if elapsed + est + reserve_secs > budget_secs:
                left = rows[idx:]
                still += len(left)
                deferred_rows = sum(
                    r["chunk_text"].count("\n") + 1 for r in left
                )
                print(
                    f"[consolidate] BUDGET STOP in retry queue after {idx}/{len(rows)} "
                    f"chunk(s): elapsed {elapsed:.0f}s + next ~{est:.0f}s + tail reserve "
                    f"{reserve_secs:.0f}s > budget {budget_secs:.0f}s -- "
                    f"{len(left)} chunk(s) stay queued"
                )
                break
        t0 = time.monotonic()
        try:
            got = compress_with_haiku(
                row["chunk_text"], prompt_prefix=_consolidate_prompt(), model=CONSOLIDATE_MODEL
            )
        except RuntimeError as e:
            durs.append(time.monotonic() - t0)
            attempts = row["retry_count"] + 1
            print(
                f"  [warn] retry of queued chunk id={row['id']} failed "
                f"(attempt {attempts}/{CONSOLIDATE_RETRY_MAX}): {e}",
                file=sys.stderr,
            )
            if attempts >= CONSOLIDATE_RETRY_MAX:
                if _retryq_deadletter(conn, row, e):
                    dead += 1
                else:
                    still += 1
            else:
                conn.execute(
                    "UPDATE consolidate_retry_queue SET retry_count=?, last_error=? WHERE id=?",
                    (attempts, str(e)[:500], row["id"]),
                )
                conn.commit()
                still += 1
            continue
        durs.append(time.monotonic() - t0)
        conn.execute("DELETE FROM consolidate_retry_queue WHERE id=?", (row["id"],))
        conn.commit()
        drained += 1
        items.extend(_attribute_items(got, row["chunk_text"]))
    line = f"[consolidate] retry queue: {drained} chunk(s) drained / {still} still queued for retry"
    if dead:
        line += f" / {dead} moved to dead-letter ({CONSOLIDATE_DEADLETTER_PATH})"
    print(line)
    return items, still, deferred_rows


def _load_taxonomy():
    """Load the full text of CONSOLIDATION_TAXONOMY.md. Returns empty string if absent
    (second-stage filter still works without it)."""
    try:
        with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        print(f"[warn] taxonomy document not found: {TAXONOMY_PATH}", file=sys.stderr)
        return ""


def _parse_keepdrop(stdout, n):
    """Parse second-stage filter output. Expected format per line: '<num> KEEP' or '<num> DROP <reason>'.
    Returns: {idx (1-based): (verdict 'KEEP'|'DROP', reason)}.
    Unparsed indices are treated conservatively as DROP by the caller (missing = DROP)."""
    verdicts = {}
    for ln in stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^\[?(\d+)\]?[).:\s-]+\s*(KEEP|DROP|SKIP)\b\s*(.*)$", s, re.IGNORECASE)
        if not m:
            continue
        idx = int(m.group(1))
        verdict = m.group(2).upper()
        reason = m.group(3).strip().lstrip("-—:").strip()
        if 1 <= idx <= n:
            verdicts[idx] = (verdict, reason or ("" if verdict == "KEEP" else "no-reason"))
    return verdicts


CONSOLIDATE_SKIP_KEY = "CONSOLIDATE_SKIP_INFLOW"
_SKIPPED = []   # (item, kind) SKIP_IN_FLOW verdicts of this run


def _skip_inflow_enabled():
    raw = (os.environ.get("DOGANY_CONSOLIDATE_SKIP_INFLOW")
           or _agent_conf_get(CONSOLIDATE_SKIP_KEY) or "").strip().lower()
    return raw in ("1", "on", "true", "yes")


SKIP_FILTER_ADDENDUM = (
    "\ud6c4\ubcf4\uac00 skill/workflow/\uc808\ucc28, rule/\uaddc\ubc94 \uc81c\uc57d, kit \uc790\uc6d0\u00b7\uc81c\uc57d, "
    "\uc624\ub108 \ud504\ub85c\ud544 \uc81c\uc548\uc774\uba74 '<\ubc88\ud638> SKIP <kind>'\ub85c \ub2f5\ud574\ub77c "
    "(kind: procedure|workflow|rule|normative_constraint|kit_resource|"
    "kit_resource_constraint|owner_profile). \uc774\ub7f0 \ud56d\ubaa9\uc740 \uae30\uc5b5\uc5d0 \uc4f0\uc9c0 \uc54a\ub294\ub2e4.\n\n")


def _persist_skipped_handoffs(now):
    """Durable pending-handoff envelope for every SKIP_IN_FLOW item; no memory
    line, candidate or message. Must succeed before the watermark moves."""
    import consolidation_router as cor
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + datetime.timedelta(days=cor.HANDOFF_RETENTION_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for item, kind in _SKIPPED:
        digest = hashlib.sha256(item.encode("utf-8")).hexdigest()
        cor.persist_pending_handoff(
            MEMORY_DATA_ROOT, MEMORY_INSTANCE_ID, "itm-" + digest[:16], kind, item,
            "local:" + digest[:16], "1", [], kind, stamp, expires)
    return len(_SKIPPED)


def _second_stage_filter(candidates):
    """Second-stage KEEP/DROP filter (Sonnet). Final verdict per candidate based on taxonomy.
    Candidates are numbered and judged in batches of FILTER_BATCH_SIZE (no per-item calls).
    Returns: (kept list, dropped (item, reason) list, filter_failed bool).
    If the filter call fails, conservative fallback: KEEP the entire batch (avoid missing) + set flag."""
    if not candidates:
        return [], [], False

    taxonomy = _load_taxonomy()
    kept = []
    dropped = []
    filter_failed = False

    for start in range(0, len(candidates), FILTER_BATCH_SIZE):
        batch = candidates[start:start + FILTER_BATCH_SIZE]
        numbered = "\n".join(f"{i}. {it}" for i, it in enumerate(batch, 1))
        prompt = (
            "너는 장기기억 공고화의 최종 게이트키퍼다. 아래 분류 기준 문서에 따라 "
            "각 후보 항목을 KEEP(남김) 또는 DROP(버림)으로 판정해라.\n"
            "한 달~일 년 뒤에도 참이고 그때 검색해 꺼내 쓸 가치가 있는 사실만 KEEP. "
            "발화·질문·진행보고·형식구조물·일회성·미확정은 DROP. 애매하면 DROP.\n\n"
            "=== 분류 기준 문서 ===\n"
            f"{taxonomy}\n"
            "=== 분류 기준 문서 끝 ===\n\n"
            "출력 형식(엄수): 후보마다 한 줄, '<번호> KEEP' 또는 '<번호> DROP <한단어사유>'. "
            "그 외 설명·머리말 금지.\n\n"
            + (_ROUTES[0]["filter_addendum"] if _ROUTES else "")
            + (SKIP_FILTER_ADDENDUM if _skip_inflow_enabled() else "")
            + "후보 목록:\n"
            f"{numbered}\n"
        )
        try:
            proc = subprocess.run(
                ["claude", "-p", "--model", CONSOLIDATE_MODEL, prompt],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"rc={proc.returncode}: {proc.stderr.strip()}")
            verdicts = _parse_keepdrop(proc.stdout, len(batch))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            print(f"  [warn] second-stage filter failed (KEEP all in this batch as fallback): {e}", file=sys.stderr)
            filter_failed = True
            kept.extend(batch)
            if _ROUTES:
                # DGN-2028: an unclassified keep is an evidence hold, never a kit write.
                for it in batch:
                    _ROUTES[0]["cls"][it] = (None, "unclassified:filter_failed")
            continue

        for i, it in enumerate(batch, 1):
            verdict, reason = verdicts.get(i, ("DROP", "verdict-missing"))
            if verdict == "SKIP":
                kind = (reason.split() or [""])[0]
                import consolidation_router as _cor
                if _skip_inflow_enabled() and _cor.classify_matter(kind) == "SKIP_IN_FLOW":
                    _SKIPPED.append((it, kind))
                else:
                    dropped.append((it, "skip-unrecognized"))
            elif verdict == "KEEP":
                kept.append(it)
                if _ROUTES:
                    _ROUTES[0]["cls"][it] = _ROUTES[0]["cr"].parse_classification(
                        reason, _ROUTES[0]["slots"], _ROUTES[0]["target_ids"])
            else:
                dropped.append((it, reason))

    return kept, dropped, filter_failed


def _backup_md(path):
    """Create a backup of the target md at <path>.bak.YYYYMMDD. Returns the path.
    Returns None if the file does not exist or is empty (fresh install, nothing to back up).
    """
    import shutil

    # fresh install: skip backup if inbox.md is absent or 0 bytes (avoids FileNotFoundError).
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    stamp = datetime.date.today().strftime("%Y%m%d")
    bak = path + ".bak." + stamp
    shutil.copy(path, bak)
    return bak


def _build_consolidate_report(new_items, skipped, had_error, mem_lines, error_hint=None,
                              backlog_rows=0):
    """Build the Telegram silent-report text (user-friendly, no technical jargon, no bold asterisks).
    new_items: newly stored items (raw one-liners without metadata).
    skipped: count skipped as duplicates (for stdout log only, not included in report).
    had_error: whether an error occurred during consolidation (embedding/compression failure etc.).
    mem_lines: current line count of inbox.md.
    error_hint: optional short string describing what failed (e.g. "3 chunk(s): rc=1").
                Appended after the snag line so the Telegram notification carries actionable detail.
    backlog_rows: DGN-1468 -- messages this run deliberately left for the next one
                (budget stop). Non-zero suppresses the "nothing to say" skip and is
                named in the report: a run that did not catch up must never read as a
                clean, complete night.
    Returns: report string to send, or None (skip sending).
    """
    md = datetime.date.today().strftime("%-m/%-d")
    n = len(new_items)

    # 0 new items, no error, no size warning -> silently skip sending (avoid daily noise).
    over_cap = mem_lines > MEMORY_LINES_CAP
    if n == 0 and not had_error and not over_cap and not backlog_rows:
        return None

    lines = [f"🌙 Memory sorted while you slept ({md})"]
    if n > 0:
        lines.append(f"- {n} new things remembered")
        for it in new_items:
            lines.append(f"  · {it}")
    else:
        lines.append("- Nothing new worth remembering")

    if backlog_rows:
        lines.append(
            f"- Not caught up yet: {backlog_rows} more message(s) queued for tomorrow night"
        )
    if over_cap:
        lines.append("Inbox is getting full -- want to sort by topic?")
    if had_error:
        snag = "Hit a snag during sorting, please check"
        if error_hint:
            snag += f" ({error_hint})"
        lines.append(snag)

    return "\n".join(lines)


def _send_silent_report(text):
    """Silent send via push.sh (dogany-proactive-push). Returns True on success.
    Calls: push.sh --silent --text <report>."""
    if not CONSOLIDATE_REPORT_ENABLED:
        return False
    push_sh = os.path.normpath(os.path.join(HERE, "..", "routines", "push.sh"))
    if not os.path.isfile(push_sh):
        print(f"[warn] push.sh not found: {push_sh}", file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            ["bash", push_sh, "--silent", "--text", text, "--audience", "owner"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[warn] report send failed: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"[warn] push.sh rc={proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _query_usage_state():
    """DGN-726: consult usage-gate's window logic (do NOT reimplement window math).

    Returns a dict {five_hour_util, seven_day_util, seven_day_resets_at,
    five_hour_resets_at} or None on any failure. Reuses routines/usage-gate.py's
    get_usage() (TTL file cache + claude-usage.sh --json refresh) so this and the
    heavy-dispatch gate share one source of truth. Fail-open: None on any error.
    """
    gate_path = os.path.normpath(os.path.join(HERE, "..", "routines", "usage-gate.py"))
    if not os.path.isfile(gate_path):
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_usage_gate_dgn726", gate_path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
    except Exception:
        return None
    # workspace root = memory-engine's parent (get_usage caches under <cwd>/.claude).
    cwd = os.path.normpath(os.path.join(HERE, ".."))
    try:
        usage = gate.get_usage(cwd)
    except Exception:
        return None
    if not isinstance(usage, dict):
        return None
    out = {
        "five_hour_util": float(usage.get("five_hour_util", 0) or 0),
        "seven_day_util": float(usage.get("seven_day_util", 0) or 0),
        "seven_day_resets_at": usage.get("seven_day_resets_at", "") or "",
        # five_hour_resets_at IS surfaced by usage-gate's cache since DGN-835
        # (additive cache key). Only a pre-DGN-835 cache file still lacks it;
        # absent -> "" (caller falls back to the seven_day boundary).
        "five_hour_resets_at": usage.get("five_hour_resets_at", "") or "",
    }
    if usage.get("five_hour_absent"):
        out.pop("five_hour_util")
        out.pop("five_hour_resets_at")
        out["five_hour_absent"] = True
    return out


def _usage_window_text(usage):
    """Name only the subscription windows returned by the provider."""
    weekly = f"7d={usage.get('seven_day_util')}%"
    return weekly if usage.get("five_hour_absent") else f"5h={usage.get('five_hour_util')}%, {weekly}"


def _usage_exhausted(usage):
    """True if either the 5h or 7d window is at/above the exhaustion threshold.

    usage: dict from _query_usage_state, or None. None -> False (fail-open: an
    unknown usage state must NOT silence a genuine failure)."""
    if not isinstance(usage, dict):
        return False
    return (
        usage.get("five_hour_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT
        or usage.get("seven_day_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT
    )


def _next_usage_reset_iso(usage):
    """Pick the next usage-reset boundary from the usage state.

    Prefer the EXHAUSTED window's reset time; if both windows are exhausted (or the
    exhausted one's reset is absent), pick the earliest available reset. Returns an
    ISO-8601 string, or "" if no reset time is known (caller falls back to the next
    daily run). Never raises."""
    if not isinstance(usage, dict):
        return ""
    candidates = []
    fh_reset = usage.get("five_hour_resets_at", "") or ""
    sd_reset = usage.get("seven_day_resets_at", "") or ""
    if usage.get("five_hour_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT and fh_reset:
        candidates.append(fh_reset)
    if usage.get("seven_day_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT and sd_reset:
        candidates.append(sd_reset)
    if not candidates:
        # No exhausted-window reset available; fall back to whichever reset we have.
        candidates = [r for r in (fh_reset, sd_reset) if r]
    if not candidates:
        return ""

    def _parse(iso_str):
        try:
            return datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    parsed = [(dt, s) for s in candidates if (dt := _parse(s)) is not None]
    if not parsed:
        return ""
    # earliest reset boundary -> soonest safe retry.
    parsed.sort(key=lambda p: p[0])
    return parsed[0][1]


def _exhausted_window(usage):
    """Which window drove the exhaustion: "five_hour" | "seven_day" | "".

    five_hour wins when both are exhausted -- it resets sooner, so routing
    recovery on it yields the earliest automatic retry (DGN-835)."""
    if not isinstance(usage, dict):
        return ""
    if usage.get("five_hour_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT:
        return "five_hour"
    if usage.get("seven_day_util", 0) >= CONSOLIDATE_USAGE_EXHAUSTION_PCT:
        return "seven_day"
    return ""


def _defer_marker_path():
    """DGN-835: env DOGANY_USAGE_DEFER_MARKER (exported by cron-guard, per-label
    path under ~/.dogany/usage-defer/) takes priority; without it fall back to
    the legacy DGN-726 marker beside state.db (direct/manual invocations)."""
    return os.environ.get("DOGANY_USAGE_DEFER_MARKER") or CONSOLIDATE_DEFER_MARKER


def _read_last_success_iso():
    """DGN-835 B3: last-SUCCESS timestamp from the artifact, "" when absent.
    Rides the defer marker (last_success_at) so the stale-alert READER in
    cron-guard stays job-agnostic -- it never has to know engine paths."""
    try:
        with open(CONSOLIDATE_LAST_SUCCESS, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_defer_marker(reset_iso, window=""):
    """Drop the usage-defer marker (half of the DGN-835 2-factor signal; the
    other half is exit 75). cron-guard reads {window, reset_at} to route
    recovery, and last_success_at to fire the stale-canon alert (DGN-835
    MAJOR-C reader). Best-effort (fail-open): any write error is swallowed --
    the watermark is preserved regardless, so the next daily run re-collects
    the same span even without a scheduled retry."""
    try:
        marker = _defer_marker_path()
        payload = {
            "window": window or "",
            "reset_at": reset_iso or "",
            "deferred_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "reason": "usage-exhaustion",
            "last_success_at": _read_last_success_iso(),
        }
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, marker)
    except OSError as e:
        print(f"[consolidate] defer-marker write failed (non-fatal): {e}", file=sys.stderr)


def _write_last_success():
    """DGN-835 B3: stamp the last-SUCCESS timestamp (ISO-8601 UTC). Called only
    on normal exit-0 completion (real consolidation or no-new-data); defer and
    failure paths never touch it, so consumers can alert on staleness without
    MEMORY.md-mtime false negatives. Best-effort: write errors are swallowed."""
    try:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = CONSOLIDATE_LAST_SUCCESS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ts + "\n")
        os.replace(tmp, CONSOLIDATE_LAST_SUCCESS)
    except OSError:
        pass


def _is_correction(text):
    """Returns True if the candidate text starts with a correction/revision/retraction marker.
    Such items are near-verbatim restatements that reverse existing facts, so their cosine
    score tends to be high and they would be caught by the dedup hard skip (making the stale
    fact stay authoritative). Exempt them from the skip so they are always stored (DGN-055)."""
    if not text:
        return False
    s = text.lstrip().lower()
    return any(s.startswith(m) for m in CORRECTION_MARKERS)


def _consolidate_commit(conn, commit_ts, rows_left, chunks_done, chunks_total,
                        budget_secs=None):
    """DGN-1468: commit the span this run actually consumed and book the backlog.

    Returns (rc_override, banner): rc_override is None for a normal exit-0
    completion, or CONSOLIDATE_RC_BACKLOG_BEHIND (77; 1 without a marker,
    DGN-1582) when the backlog has failed to drain for
    CONSOLIDATE_BACKLOG_STUCK_RUNS consecutive runs. Draining is normal and must
    stay exit 0 (it is forward progress); STUCK draining is a real failure and
    must be COUNTED as one -- an exit-0 night with a permanent backlog is the
    same silent green this ticket exists to remove (DGN-1154 family).
    """
    pruned = 0
    if commit_ts:
        _ts_save_watermark(conn, commit_ts)
        pruned = prune_transcript_fts(conn, commit_ts)
        print(f"[consolidate] watermark advanced -> {commit_ts} ({pruned} same-day raw rows pruned)")

    prev_rows, prev_runs = _backlog_load(conn)
    if rows_left > 0:
        runs = prev_runs + 1
        _backlog_save(conn, rows_left, runs)
        banner = (
            f"PARTIAL: budget stop -- {chunks_done}/{chunks_total} chunk(s) done, "
            f"{rows_left} message(s) left for the next run "
            f"(consecutive draining runs: {runs})"
        )
        print(f"[consolidate] {banner}")
        if runs >= CONSOLIDATE_BACKLOG_STUCK_RUNS:
            print(
                f"[!! consolidate] BACKLOG STUCK: {runs} consecutive runs have ended with a "
                f"backlog ({rows_left} message(s) still pending). Consolidate is NOT keeping "
                "up -- reporting this run as behind so it is counted, not read as a clean "
                "night.",
                file=sys.stderr,
            )
            behind_night = runs - CONSOLIDATE_BACKLOG_STUCK_RUNS + 1
            raise_info = None
            if behind_night >= 2:
                raise_info = _raise_budget(budget_secs, rows_left, behind_night)
            if _write_backlog_marker(rows_left, runs, chunks_done, chunks_total,
                                     behind_night, raise_info):
                return CONSOLIDATE_RC_BACKLOG_BEHIND, banner
            return 1, banner
        return None, banner

    if prev_rows or prev_runs:
        print("[consolidate] backlog drained -- caught up to the end of the span")
        _backlog_save(conn, 0, 0)
    _clear_budget_boost()
    return None, None


def _raise_budget(budget_secs, rows_left, behind_night):
    """DGN-1582 r3 (dec-236): raise the next night's budget by one bounded
    step, capped at CONSOLIDATE_BUDGET_CAP_SECS, and record it.
    Returns {"from", "to", "raised"}; raised is False at the cap, with the
    budget disabled (<= 0), or when the record cannot be written."""
    cur = CONSOLIDATE_BUDGET_SECS if budget_secs is None else float(budget_secs)
    if cur <= 0:
        return {"from": cur, "to": cur, "raised": False}
    new = min(cur + CONSOLIDATE_BUDGET_STEP_SECS, CONSOLIDATE_BUDGET_CAP_SECS)
    if new <= cur:
        print(f"[consolidate] budget already at the cap ({int(cur)}s) -- not raised")
        return {"from": cur, "to": cur, "raised": False}
    record = {
        "budget_secs": int(new),
        "from": int(cur),
        "rows_left": int(rows_left),
        "behind_night": int(behind_night),
        "at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    try:
        tmp = CONSOLIDATE_BUDGET_BOOST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, CONSOLIDATE_BUDGET_BOOST_FILE)
    except OSError as e:
        print(f"[consolidate] budget raise not recorded (non-fatal): {e}", file=sys.stderr)
        return {"from": cur, "to": cur, "raised": False}
    print(f"[consolidate] behind night {behind_night}: next night's budget raised "
          f"{int(cur)}s -> {int(new)}s (cap {CONSOLIDATE_BUDGET_CAP_SECS}s)")
    return {"from": cur, "to": new, "raised": True}


def _clear_budget_boost():
    """A drained night returns the budget to the default (DGN-1582 r3)."""
    try:
        os.remove(CONSOLIDATE_BUDGET_BOOST_FILE)
        print("[consolidate] budget boost cleared -- back to the default budget")
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[consolidate] budget boost clear failed (non-fatal): {e}", file=sys.stderr)


def _write_backlog_marker(rows_left, runs, chunks_done, chunks_total,
                          behind_night=1, raise_info=None):
    """DGN-1582: drop the behind marker (half of the rc-77 2-factor signal).
    Returns True only when the marker landed at the cron-guard-exported path;
    False (env unset or write error) keeps the caller on rc 1."""
    marker = os.environ.get("DOGANY_BACKLOG_MARKER")
    if not marker:
        return False
    try:
        payload = {
            "rows_left": int(rows_left),
            "runs": int(runs),
            "chunks_done": int(chunks_done),
            "chunks_total": int(chunks_total),
            "behind_night": int(behind_night),
            "budget_raised": bool(raise_info and raise_info["raised"]),
            "budget_next_secs": int(raise_info["to"]) if raise_info else None,
            "at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, marker)
        return True
    except OSError as e:
        print(f"[consolidate] backlog-marker write failed (non-fatal): {e}", file=sys.stderr)
        return False


def _finish_no_candidate_night(conn, args, commit_ts, rows_left, chunks_done,
                               chunks_total):
    """DGN-1468 grill [5]: shared tail for the two zero-candidate exits
    (nothing extracted / everything DROPped by the filters).

    A night that left a backlog must never end silent: the one-line consumer
    (pushed report) still fires, and the last-success stamp is NOT refreshed --
    "behind" is not "success", even when there happened to be nothing worth
    pushing. Only a genuinely caught-up night stays silent and stamps.
    Returns the process rc."""
    rc_stuck = None
    if not args.dry_run and (commit_ts or rows_left):
        rc_stuck, _ = _consolidate_commit(
            conn, commit_ts, rows_left, chunks_done, chunks_total,
            budget_secs=getattr(args, "budget_secs", None),
        )
    conn.close()
    if not args.dry_run:
        if rows_left:
            rep = _build_consolidate_report([], 0, False, 0, backlog_rows=rows_left)
            if rep:
                print("\n-- report --\n" + rep)
                if args.no_push:
                    print("[--no-push] Telegram send skipped.")
                elif _send_silent_report(rep):
                    print("[consolidate] report sent silently.")
        elif rc_stuck is None:
            _write_last_success()  # normal completion (DGN-835 B3)
    return rc_stuck or 0


def cmd_consolidate(args):
    now = datetime.datetime.now(datetime.timezone.utc)
    # DGN-2028 slice 4: the routing context is rebuilt (or cleared) per run.
    _ROUTES[:] = [_routes_context()] if _consolidate_routes_enabled() else []
    # DGN-1468: one wall-clock budget for the whole run. The nightly step is
    # killed at a hard limit by the queue runner; a killed run commits nothing,
    # which grows the next run's span. Budgeting it here is what makes the
    # watermark move forward every night regardless.
    run_started = time.monotonic()
    budget_secs = CONSOLIDATE_BUDGET_SECS
    if getattr(args, "budget_secs", None) is not None:
        budget_secs = float(args.budget_secs)
    _ITEM_SUBJECTS.clear()   # DGN-1384: subjects are per run

    conn = connect()
    init_db(conn)

    # 1) load watermark (--since-days overrides it and forces the last N days)
    if args.since_days is not None:
        watermark = (now - datetime.timedelta(days=args.since_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        print(f"[consolidate] --since-days {args.since_days} -> ignoring watermark, using {watermark}")
    else:
        watermark = _ts_load_watermark(conn)
        if watermark:
            print(f"[consolidate] watermark: after {watermark}")
        else:
            print(f"[consolidate] no watermark -> last {DEFAULT_LOOKBACK_DAYS} days")

    # 2) collect transcript incrementally. DGN-1468: collect ROWS, not the joined
    # text -- the budget-bounded compressor chunks on message boundaries so a
    # partial run can commit a watermark that never falls inside a message.
    span_rows = _iter_transcript_rows(watermark, now)
    if _ROUTES:
        _ROUTES[0]["rows"] = span_rows
    max_ts = span_rows[-1][0] if span_rows else None
    n_msgs = len(span_rows)
    # DGN-153: snapshot the retry queue BEFORE this run queues new failures,
    # so a chunk is never retried in the same run it failed in.
    pending_retries = _retryq_load(conn)
    if not span_rows and not pending_retries:
        print("No new conversations to consolidate.")
        conn.close()
        if not args.dry_run:
            _write_last_success()  # no-new-data is a normal completion (DGN-835 B3)
        return 0
    if span_rows:
        print(f"[consolidate] {n_msgs} messages collected (max ts={max_ts})")
    else:
        print(f"[consolidate] no new conversations -- retry queue only ({len(pending_retries)} pending chunk(s))")

    # 2.4) DGN-726 PRE-FLIGHT usage gate: if the usage window is already exhausted the
    # sonnet compression is doomed (rc=1). Skip it up front -- never burn the doomed calls,
    # preserve the watermark, stay fully silent, and drop a defer marker so the wrapper
    # schedules a reset-aligned retry. Fail-open: unknown/not-exhausted -> normal path (the
    # post-hoc all-fail guard below is the second safety net if the window flips mid-run).
    if not args.dry_run:
        _preflight_usage = _query_usage_state()
        if _usage_exhausted(_preflight_usage):
            _reset_iso = _next_usage_reset_iso(_preflight_usage)
            print(
                "[consolidate] PRE-FLIGHT: usage window exhausted "
                f"({_usage_window_text(_preflight_usage)}) "
                "-- usage-defer: skipping compression, watermark preserved, silent."
            )
            if _reset_iso:
                print(f"[consolidate] usage resets at {_reset_iso} -- signaling usage-defer for recovery.")
            else:
                print("[consolidate] usage reset time unknown -- will retry on next daily run.")
            _write_defer_marker(_reset_iso, _exhausted_window(_preflight_usage))
            conn.close()
            # DGN-835 2-factor signal: rc75 + fresh marker -> cron-guard routes
            # recovery (5h auto retry / 7d owner button); no report push.
            return CONSOLIDATE_RC_USAGE_DEFER

    # 3) first-pass compression (Sonnet, extract liberally), split into chunks.
    # DGN-1468: bounded by the run budget. `commit_ts` is the watermark this run
    # is allowed to commit -- the end of the last chunk it actually attempted,
    # NOT the end of the collected span. rows_left > 0 means the run deliberately
    # left a backlog for the next night.
    commit_ts = max_ts
    chunks_done = chunks_total = 0
    rows_left = 0
    chunk_durations = []
    if span_rows:
        (candidates, failed_chunks, commit_ts, chunks_done, chunks_total,
         rows_left, chunk_durations) = compress_rows_chunked(
            span_rows, budget_secs=budget_secs, started=run_started)
        print(f"[consolidate] first-pass candidates: {len(candidates)}")
    else:
        candidates, failed_chunks = [], []
    compress_failed = len(failed_chunks)

    # 2.5 -> 3.1) raw transcript archive (DGN-093): before the watermark advance/prune
    # discards them, append the consumed span (TEXT ONLY) to a gzip monthly archive.
    # DGN-1468 moved this AFTER compression and bounded it by commit_ts: a budget-
    # bounded run consumes only a prefix, and archiving the whole span would write
    # the un-consumed tail twice (once now, once when the next run consumes it).
    # Skipped on dry-run (no state changes).
    if span_rows and not args.dry_run and commit_ts:
        n_arch = archive_raw_transcript(watermark, now, until_iso=commit_ts)
        print(f"[consolidate] raw archive: {n_arch} rows stored -> {RAW_ARCHIVE_DIR}")

    # 3a) full-failure guard (pre-DGN-153 semantics kept): if ALL chunks failed and
    # produced 0 candidates, do NOT advance the watermark -- the whole span is
    # re-collected next night. Nothing is queued in this path (queueing plus a
    # preserved watermark would double-process the span); pending retries are
    # also left untouched for the next run.
    if span_rows and not candidates and compress_failed and compress_failed == chunks_done:
        # DGN-726: distinguish usage-exhaustion (known-transient, silence + reset-aligned
        # retry) from a genuine code defect (keep DGN-182 visibility). Consult usage-gate's
        # window logic POST-HOC: only if a window is actually exhausted do we take the silent
        # defer path. Anything else (usage unknown / not exhausted) stays the visible failure
        # -> no false-silence of a real defect (fail-open toward visibility).
        usage = _query_usage_state()
        if _usage_exhausted(usage):
            reset_iso = _next_usage_reset_iso(usage)
            print(
                "[consolidate] all chunks failed AND usage window exhausted "
                f"({_usage_window_text(usage)}) "
                "-- usage-defer: watermark preserved, silent (no report, no cron-guard alert)."
            )
            if reset_iso:
                print(f"[consolidate] usage resets at {reset_iso} -- signaling usage-defer for recovery.")
            else:
                print("[consolidate] usage reset time unknown -- will retry on next daily run.")
            if pending_retries:
                print(f"[consolidate] retry queue untouched ({len(pending_retries)} chunk(s) still queued for retry)")
            if not args.dry_run:
                _write_defer_marker(reset_iso, _exhausted_window(usage))
            conn.close()
            # DGN-835 2-factor signal: rc75 + fresh marker -> cron-guard routes
            # recovery instead of alerting. No report push.
            return CONSOLIDATE_RC_USAGE_DEFER

        print("All chunks failed compression -- watermark preserved (retry next run).")
        if pending_retries:
            print(f"[consolidate] retry queue untouched ({len(pending_retries)} chunk(s) still queued for retry)")
        conn.close()
        if not args.dry_run and not args.no_push:
            _hint = f"all {compress_failed} chunk(s) failed compression"
            rep = _build_consolidate_report([], 0, True, 0, error_hint=_hint)
            if rep:
                _send_silent_report(rep)
        return 1

    # 3b) DGN-153 partial failure: persist failed chunks to the retry queue BEFORE
    # the watermark advances, so the next run retries them instead of losing them.
    if failed_chunks:
        if args.dry_run:
            print(f"[consolidate] [dry-run] {len(failed_chunks)} chunk(s) would be queued for retry")
        else:
            for ch_text, err in failed_chunks:
                _retryq_add(conn, ch_text, err, now)
            print(f"[consolidate] {len(failed_chunks)} chunk(s) queued for retry (next run)")

    # 3c) DGN-153: retry chunks queued by previous runs; successes are dequeued and
    # merged into this run's candidates (then flow through the normal
    # filter/dedup/store pipeline). Failures stay queued (or dead-letter at cap).
    # DGN-1468 grill [2]: retries run under the SAME wall-clock budget as the
    # first-pass loop -- an unbounded drain here broke the "the run always ends
    # inside its budget" promise (repro: 20 queued chunks blew a 150s budget to
    # 797s). Chunks the budget defers stay queued and COUNT into the backlog.
    if pending_retries:
        if args.dry_run:
            print(f"[consolidate] [dry-run] retry queue skipped ({len(pending_retries)} chunk(s) pending)")
        else:
            reserve = 0.0
            if budget_secs > 0:
                reserve = min(
                    len(candidates) * CONSOLIDATE_DOWNSTREAM_SECS_PER_CAND,
                    budget_secs * CONSOLIDATE_RESERVE_MAX_FRAC,
                )
            retried_items, _still, retry_deferred_rows = _retryq_process(
                conn, pending_retries, budget_secs=budget_secs,
                started=run_started, durations=chunk_durations,
                reserve_secs=reserve)
            rows_left += retry_deferred_rows
            merged_seen = {c.strip().lower() for c in candidates}
            for it in retried_items:
                key = it.strip().lower()
                if key and key not in merged_seen:
                    merged_seen.add(key)
                    candidates.append(it)

    if not candidates:
        print("No persistent facts worth remembering.")
        return _finish_no_candidate_night(
            conn, args, commit_ts, rows_left, chunks_done, chunks_total
        )

    # 3.5) rule-filter (post-processing, deterministic double safety net): remove structural
    # artifact / utterance / command candidates.
    candidates, rule_dropped = _rule_filter_candidates(candidates)
    print(f"[consolidate] after rule-filter: {len(candidates)} ({len(rule_dropped)} removed by rules)")
    for it, why in rule_dropped:
        print(f"    [ruleDROP/{why}] {it}")

    # 3.6) second-stage KEEP/DROP filter (Sonnet + taxonomy). Batch verdicts.
    _SKIPPED[:] = []
    candidates, sec_dropped, filter_failed = _second_stage_filter(candidates)
    if _SKIPPED:
        print(f"[consolidate] skip-in-flow: {len(_SKIPPED)} (pending handoff, no memory line)")
        if not args.dry_run:
            try:
                _persist_skipped_handoffs(now)
            except Exception as e:
                print(f"[consolidate] handoff not durable ({e}) -- watermark preserved.",
                      file=sys.stderr)
                conn.close()
                return 1
    print(f"[consolidate] after second-stage filter: {len(candidates)} ({len(sec_dropped)} DROPped)")
    for it, why in sec_dropped:
        print(f"    [2ndDROP/{why}] {it}")

    # 3.7) DGN-2028 slice 4 (CONSOLIDATE_ROUTES on): resolve each kept item's
    # home BEFORE memory dedup (kit receipt before deduplication). Pure
    # planning plus read-only provider lookups -- nothing is written before
    # the dry-run boundary below. Only base-homed items continue to dedup.
    routed = []
    route_plans = {}
    if _ROUTES:
        candidates, route_plans, routed = _routes_plan(_ROUTES[0], candidates)
        print(f"[consolidate] routes: {len(candidates)} to memory / {len(routed)} routed")

    if not candidates and not routed:
        print("No persistent facts passed the filter.")
        return _finish_no_candidate_night(
            conn, args, commit_ts, rows_left, chunks_done, chunks_total
        )

    # 3.9) freshness guard (DGN-884): the dedup pool below is only as fresh as the
    # notes index, but the search hook (_hook_maybe_reindex) fires only on daytime
    # searches and never during this nightly run, and the wrapper's pre-index
    # (routines/consolidate-0430.sh) covers only the launchd path. Refresh here so
    # EVERY invocation path dedups against a current pool (self-sufficiency decided
    # in DGN-042 but implemented only in the wrapper). Incremental/sha-gated: when
    # the index is already fresh this is a no-op (0 embedding calls). Dry-run keeps
    # its no-state-writes contract and is skipped (preview may use the stale pool,
    # unchanged from previous behavior).
    # DGN-1873 Q5: same lock + busy-retry as classify-inbox's reindex; a database
    # that stays busy no longer kills the night -- dedup runs on the pool as is.
    if not args.dry_run:
        print("[consolidate] refreshing notes index before dedup...")
        if not _reindex_locked_with_retry():
            print("[consolidate] notes index not refreshed (database busy) -- "
                  "dedup uses the current pool.", file=sys.stderr)

    # 4) dedup: embed each candidate -> cosine against existing note embeddings. Skip if max >= threshold.
    existing_vecs = []
    embed_failed = False
    for row in conn.execute("SELECT embedding FROM notes WHERE embedding IS NOT NULL"):
        existing_vecs.append(blob_to_vec(row["embedding"]))

    new_items = []   # new items (to be stored)
    dup_items = []   # items skipped as duplicates
    for cand in candidates:
        if embed_failed:
            # embedding broken -> treat all remaining as new
            new_items.append(cand)
            continue
        try:
            cvec = embed(cand)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[warn] embedding failed -- skipping dedup, treating all as new: {e}", file=sys.stderr)
            embed_failed = True
            new_items.append(cand)
            continue
        max_sim = 0.0
        for ev in existing_vecs:
            s = cosine(cvec, ev)
            if s > max_sim:
                max_sim = s
        # Correction exemption (M3/DGN-055): correction items reverse existing facts so their
        # cosine exceeds DEDUP_THRESHOLD and they'd be hard-skipped, leaving the stale fact
        # authoritative ("confidently wrong"). Exempt correction-marker candidates from the skip.
        # Only items that already passed the second-stage filter (KEEP) reach here.
        if max_sim >= DEDUP_THRESHOLD and not _is_correction(cand):
            dup_items.append(cand)
        else:
            new_items.append(cand)
            existing_vecs.append(cvec)  # add to pool to catch inter-candidate duplicates too

    print(f"[consolidate] new: {len(new_items)} / dup skipped: {len(dup_items)}"
          + (" / embedding failed (dedup skipped)" if embed_failed else ""))

    # 5) attach metadata
    today = datetime.date.today().isoformat()
    # DGN-1384: subject first (by=owner-quoted|agent-judged|unverified), the
    # pipeline stage only as via= -- the old "(date, nightly-consolidate)"
    # read as a source when it only named the batch job.
    tagged = [_subject_tag(it, today) for it in new_items]
    if _ROUTES:
        tagged = [_ROUTES[0]["cr"].tag_line(t, route_plans[it])
                  for t, it in zip(tagged, new_items)]

    # print preview (always)
    print(f"\n== to be stored ({len(tagged)}) -> inbox.md ==")
    for it in tagged:
        print(f"  § {it}")
    if dup_items:
        print(f"\n-- already known (skipped): {len(dup_items)} --")
        for it in dup_items:
            print(f"  ~ {it}")

    if routed:
        print(f"\n== routed ({len(routed)}) ==")
        for p in routed:
            print(f"  -> {p['destination']} {p['state']}:{p['reason']} "
                  f"slot={p['slot']} target={p['target']} :: {p['text']}")

    # dry-run: do not touch file / DB / watermark / push
    if args.dry_run:
        print("\n[dry-run] file / watermark / re-index / push all skipped. above is preview.")
        conn.close()
        return 0

    # 5a) DGN-2028 slice 4: every routed item is made durable (intent outbox +
    # reconciliation queue, then the pending file) BEFORE memory is touched and
    # long before the watermark moves. A failure here returns without the
    # watermark: the span is re-collected and the intent ids are stable.
    n_pending = 0
    if routed:
        try:
            got = _ROUTES[0]["cr"].persist(conn, routed, _ROUTES[0]["instance"],
                                           _ROUTES[0]["cr"].utc_now())
            n_pending = _routes_write_pending(_ROUTES[0], routed, today)
        except Exception as e:
            print(f"[consolidate] routed items not durable ({e}) -- watermark preserved.",
                  file=sys.stderr)
            conn.close()
            return 1
        print(f"[consolidate] routed durable: {got} / pending evidence lines: {n_pending}")

    # 5b) store (only when there are new items: backup + append + re-index).
    # inbox.md is § based -> has_sep auto-detected.
    if tagged:
        # fresh-install guard: create memories/ dir + inbox.md if missing.
        # (.gitignore ships MEMORY.md only -> inbox.md absent on first consolidate run ->
        #  _backup_md/append_notes_to_md would die with FileNotFoundError. Guard against that.)
        os.makedirs(MEMORIES_DIR, exist_ok=True)
        if not os.path.isfile(TARGET_MEMORY_MD):
            # M5: seed the section-format marker so append doesn't sniff an empty
            # file as bullet-mode (inbox.md is a § section-mode file).
            with open(TARGET_MEMORY_MD, "w", encoding="utf-8") as _f:
                _f.write(_marker_line("section") + "\n")
        bak = _backup_md(TARGET_MEMORY_MD)
        if bak:
            print(f"[consolidate] backup created: {os.path.basename(bak)}")
        where = append_notes_to_md(TARGET_MEMORY_MD, None, tagged)
        print(f"[consolidate] {where}")

    # 5c) DGN-2009: mark lines a later same-subject line contradicts.
    n_marked = 0
    try:
        for name, old_line, _new in mark_superseded():
            n_marked += 1
            print(f"[consolidate] {SUPERSEDED_MARK} {name}: {old_line}")
    except Exception as e:  # never fails the night
        print(f"[warn] superseded marking skipped: {e}", file=sys.stderr)

    # 6) advance watermark + prune same-day rolling index (delete raw rows with ts<=commit_ts)
    # DGN-153: skipped on a retry-only run (no new span consumed -> commit_ts is None).
    # DGN-1468: commit_ts is the end of the last chunk this run ATTEMPTED, so a
    # budget-bounded run still moves the watermark forward over the work it did.
    rc_stuck, _ = _consolidate_commit(
        conn, commit_ts, rows_left, chunks_done, chunks_total,
        budget_secs=budget_secs,
    )
    conn.close()
    # DGN-835 B3: this run completed the pipeline -- stamp the last-success
    # artifact. Dry-run returned earlier. A STUCK backlog is not a success and
    # must not stamp (otherwise the staleness reader stays green too).
    # DGN-1468 grill [5]: neither is a night that left ANY backlog -- the stamp
    # means "fully caught up", so partial nights leave it un-refreshed.
    if rc_stuck is None and not rows_left:
        _write_last_success()

    # 6b) prune raw archive retention (DGN-093): delete monthly archives older than ~365 days.
    n_rawpruned = prune_raw_archive(now)
    if n_rawpruned:
        print(f"[consolidate] {n_rawpruned} raw archive files expired and deleted")

    # 7) re-index (only meaningful when new items were stored)
    # DGN-1873 Q5: locked + retried like the pre-dedup refresh above. The new
    # items are already in inbox.md; a still-busy database leaves them for the
    # next reindex (daytime search hook / tomorrow's pre-index) to pick up.
    if tagged or n_marked or n_pending:
        print("[consolidate] re-indexing...")
        if not _reindex_locked_with_retry():
            print("[consolidate] re-index deferred (database busy) -- the next "
                  "index run picks the new items up.", file=sys.stderr)

    # 7b) M4: check NULL embedding ratio -- high ratio means semantic search degraded (Ollama absent/down).
    frac, nulls, ntot = null_embedding_fraction()
    if ntot and frac >= NULL_EMBED_WARN_FRAC:
        print(
            f"[!! warning] {nulls}/{ntot} notes ({frac*100:.0f}%) have no embedding -- "
            "semantic (vector) search degraded. Check Ollama(bge-m3) then re-run index. "
            "Auto-recall currently running on keyword search only."
        )

    # 8) build report + silent send
    try:
        mem_lines = sum(1 for _ in open(TARGET_MEMORY_MD, "r", encoding="utf-8"))
    except OSError:
        mem_lines = 0
    had_error = embed_failed or compress_failed > 0 or filter_failed
    # Build a short hint string so the Telegram report names what went wrong
    # instead of the opaque "Hit a snag" bare message (DGN-523).
    _error_parts = []
    if compress_failed > 0:
        _error_parts.append(f"{compress_failed} chunk(s) failed compression")
    if filter_failed:
        _error_parts.append("second-stage filter failed")
    if embed_failed:
        _error_parts.append("embedding failed (dedup skipped)")
    _error_hint = "; ".join(_error_parts) if _error_parts else None
    report = _build_consolidate_report(new_items, len(dup_items), had_error, mem_lines,
                                       error_hint=_error_hint, backlog_rows=rows_left)

    if report is None:
        print("[consolidate] report send skipped (0 new items, no error, within capacity).")
        return rc_stuck or 0

    print("\n-- report --\n" + report)
    if args.no_push:
        print("\n[--no-push] Telegram send skipped.")
        return rc_stuck or 0
    if _send_silent_report(report):
        print("[consolidate] report sent silently.")
    return rc_stuck or 0


# ======================================================================
# classify-inbox command (weekly inbox classification: inbox.md -> distribute to topic files + suggest new topics)
# ======================================================================
# Core: once a week, classify items that nightly consolidation stored in inbox.md
# and distribute them to existing topic files (memories/*.md, excluding USER and inbox).
# - Distribute: append to the topic file (respecting its has_sep format), remove from inbox.md.
# - New topic: do NOT create a new file; suggest it in the report (when 3+ items share a new label).
# - Drop: log and remove from inbox (no topic match and no lasting value).
# Safety first: if all seats fail, preserve inbox.md; exit rc!=0.
# Preserve the qualified bench's CLI default (no flag). Haiku low produced
# invalid verdicts on the same frozen sample and did not qualify (dec-443).
CLASSIFY_HAIKU_EFFORT = None
# Always exclude these from distribution target (topic) candidates. inbox is the
# source; the owner-facts file is hot (both layout spellings -- see
# INDEX_EXCLUDE above for the same rename and the same scope caveat).
# routed-pending.md (DGN-2028 slice 4) holds routed items awaiting their kit /
# conflict / hold resolution; it is indexed for recall but is not a topic file.
CLASSIFY_EXCLUDE = {"USER.md", "hot.custom.owner.md", "inbox.md", "routed-pending.md"}
# Suggest creating a new file when this many items accumulate under the same new-topic label.
NEW_TOPIC_SUGGEST_MIN = 3
# Chunked classification: max items per call (env-overridable for testing).
# Splitting prevents 300s timeout death-spiral on large inboxes (e.g. 795-item backlog).
CLASSIFY_CHUNK_SIZE = _env_num("CLASSIFY_CHUNK_SIZE", 100, int)
# Test seam: set CLASSIFY_CMD to a shell command that accepts a prompt on stdin and writes
# verdict lines on stdout. When set, _run_classifier uses it instead of the real claude call.
# Example: CLASSIFY_CMD="cat /tmp/stub_verdicts.txt" (ignores stdin, outputs fixed verdicts).
CLASSIFY_CMD = os.environ.get("CLASSIFY_CMD", None)
CLASSIFY_CALL_TIMEOUT = 300
CLASSIFY_SAVE_MARGIN = 30


def _classify_deadline():
    """Convert the queue's absolute deadline, including slot wait, to monotonic time."""
    raw = os.environ.get("DOGANY_CLASSIFY_DEADLINE_EPOCH")
    if not raw:
        return None  # manual invocations retain the unlimited catch-up path
    return time.monotonic() + max(0, float(raw) - time.time())


def _classify_progress(initial, remaining, pending_chunks, reindex_pending=False):
    """Publish this run's result atomically; the inbox itself is the checkpoint."""
    path = os.environ.get("DOGANY_CLASSIFY_PROGRESS_FILE")
    if path:
        result = dict(initial=initial, remaining=remaining,
                      processed=initial - remaining, pending_chunks=pending_chunks,
                      carryover=remaining > 0, reindex_pending=reindex_pending)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(result, f)
        os.replace(path + ".tmp", path)


def _classify_reindex(deadline):
    if deadline is None:
        return _reindex_locked_with_retry()
    # Indexing can wait on SQLite and embeddings. Bound that tail as well as
    # the LLM calls, otherwise a successful checkpoint can still timeout.
    left = deadline - time.monotonic() - 5
    if left <= 0:
        return False
    try:
        return subprocess.run(
            [sys.executable, os.path.abspath(__file__), "index", "--wait-lock"],
            timeout=left, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _classify_codex_available():
    """Reuse dispatch's authenticated quota signal, without its write side effects.

    dispatch-detached.sh:codex_quota_gate uses codex-ratelimit.py --json and
    accepts a validated used_percent <= 98. No separate enable setting exists.
    No memory text is sent to this probe.
    """
    import tempfile

    probe = os.path.normpath(os.path.join(HERE, "..", "routines", "codex-ratelimit.py"))
    try:
        with tempfile.TemporaryDirectory(prefix="memory-classify-probe-") as cwd:
            proc = subprocess.run(
                [sys.executable, probe, "--json", "--timeout", "20"],
                capture_output=True, text=True, timeout=25, cwd=cwd,
                env=_scrub_claude_session_env(),
            )
        data = json.loads(proc.stdout)
        used = data.get("used_percent")
        return (proc.returncode == 0 and data.get("ok") is True
                and type(used) is int and 0 <= used <= 98)
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return False


def _resolve_classify_seats():
    """Owner pin > authenticated Codex availability > framework Haiku default.

    CLASSIFY_SEAT in agent.conf (or DOGANY_CLASSIFY_SEAT) accepts auto, haiku,
    codex, opus. The alternate available seat is tried once, then legacy Opus.
    An explicit pin attempts that seat even when the availability probe fails.
    """
    pin = (os.environ.get("DOGANY_CLASSIFY_SEAT")
           or _agent_conf_get("CLASSIFY_SEAT") or "auto").strip().lower()
    if pin not in {"auto", "haiku", "codex", "opus"}:
        raise ValueError("CLASSIFY_SEAT must be auto, haiku, codex or opus")
    if pin == "opus":
        return ["opus"]
    codex_available = _classify_codex_available()
    first = ("codex" if codex_available else "haiku") if pin == "auto" else pin
    seats = [first]
    if first == "codex":
        seats.append("haiku")
    elif codex_available:
        seats.append("codex")
    return seats + ["opus"]


def _classify_seat_command(seat):
    """Single inference in an empty cwd: no project context or tools."""
    if seat == "codex":
        return ["codex", "exec", "--json", "--model", "gpt-6-luna", "-c",
                "model_reasoning_effort=low", "--sandbox", "read-only",
                "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "-c", "project_doc_max_bytes=0", "-c", 'developer_instructions=""',
                "-c", "features.shell_tool=false", "-c", "features.multi_agent=false",
                "-c", 'web_search="disabled"', "-"]
    cmd = ["claude", "-p", "--model", "claude-haiku-5-5" if seat == "haiku" else "opus",
           "--output-format", "json", "--safe-mode", "--tools", "",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
           "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands"]
    if seat == "haiku" and CLASSIFY_HAIKU_EFFORT:
        cmd += ["--effort", CLASSIFY_HAIKU_EFFORT]
    return cmd


def _decode_classify_response(stdout, seat):
    """Unwrap Claude result JSON or Codex exec JSONL before verdict parsing."""
    if seat != "codex":
        obj = json.loads(stdout)
        if (obj.get("is_error") or obj.get("subtype", "success") != "success"
                or not isinstance(obj.get("result"), str)):
            raise ValueError("Claude classifier returned an error or no text")
        return obj["result"]
    texts, completed = [], False
    for line in stdout.splitlines():
        obj = json.loads(line)
        if obj.get("type") in {"error", "turn.failed"}:
            raise ValueError("Codex classifier returned an error")
        if obj.get("type") == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message":
                texts.append(item["text"])
            elif item.get("type") != "reasoning":
                raise ValueError("Codex classifier attempted a tool call")
        if obj.get("type") == "turn.completed":
            completed = True
    if not completed or not texts:
        raise ValueError("Codex classifier returned no completed answer")
    return "\n".join(texts)


def _call_classify_seats(prompt, n_items, valid_files):
    """Bounded fallback; log seats and failure kinds, never private output."""
    import tempfile

    deadline = _classify_deadline()
    try:
        seats = _resolve_classify_seats()
    except ValueError as exc:
        print(f"[error] classifier: {exc}", file=sys.stderr)
        return {}, False
    for seat in seats:
        timeout = CLASSIFY_CALL_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic() - CLASSIFY_SAVE_MARGIN)
            if timeout <= 0:
                break
        print(f"[classify] trying seat={seat}", file=sys.stderr)
        try:
            with tempfile.TemporaryDirectory(prefix="memory-classify-") as cwd:
                proc = subprocess.run(
                    _classify_seat_command(seat), input=prompt,
                    capture_output=True, text=True, timeout=timeout, cwd=cwd,
                    env=_scrub_claude_session_env(),
                )
            if proc.returncode:
                print(f"[classify] seat={seat} failed rc={proc.returncode}", file=sys.stderr)
                continue
            output = _decode_classify_response(proc.stdout, seat)
            verdicts = _parse_classify_output(output, n_items, valid_files)
            # Legacy Opus keeps its partial-verdict contract: the caller routes
            # valid items and holds missing verdicts in inbox, as before.
            if seat != "opus" and len(verdicts) != n_items:
                raise ValueError("Incomplete classifier verdicts")
            print(f"[classify] ran seat={seat}", file=sys.stderr)
            return verdicts, True
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                subprocess.TimeoutExpired) as exc:
            print(f"[classify] seat={seat} failed kind={type(exc).__name__}", file=sys.stderr)
    return {}, False

# Exit codes for classify-inbox (contract with the check wrapper).
#   0 = saved progress (stamp marker only if the structured result is drained) /
#   2 = no items to classify (do NOT stamp marker) /
#   1 = failure (all seats failed, inbox preserved, retry next run) /
#   3 = classified (ok to stamp marker) but the reindex could not get the
#       database (DGN-1873) -- the index is stale until a reindex succeeds.
# Key: if an empty inbox (header / comment seed / blank § only) is mistakenly judged as
# "has items" and rc=0 marker is stamped, real items that accumulate later will be skipped
# forever by the 7-day skip -> must always return rc=2 for empty inbox.
CLASSIFY_RC_OK = 0
CLASSIFY_RC_EMPTY = 2
CLASSIFY_RC_FAIL = 1
CLASSIFY_RC_REINDEX_DEFERRED = 3


def inbox_has_items(path=None):
    """Is there at least one actual classification-target item in inbox.md?
    Single source of truth for the 'empty' check shared by the check wrapper and cmd_classify_inbox.
    Non-items (headers, comment seeds, blank separators) are already filtered by _inbox_items."""
    if path is None:
        path = os.path.join(MEMORIES_DIR, "inbox.md")
    if not os.path.isfile(path):
        return False
    return len(_inbox_items(path)) > 0


def _inbox_items(path):
    """Extract only real classification-target items from inbox.md.
    Because inbox.md is § based, parse with parse_markdown and exclude
    HTML comment (<!-- -->) seeds and empty blocks.
    Returns: plain text list (not a list of note dicts)."""
    notes = parse_markdown(path)
    items = []
    for nt in notes:
        t = (nt.get("text") or "").strip()
        if not t:
            continue
        # exclude blocks that consist solely of HTML comment seeds (<!-- ... -->)
        stripped = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL).strip()
        if not stripped:
            continue
        items.append(stripped)
    return items


def _classify_topics(memories_dir):
    """Dynamically collect the current topic file list (no hard-coding).
    memories/*.md excluding CLASSIFY_EXCLUDE. Also reads the first header (section name) of each file.
    Returns: [(filename, section_hint), ...]. Automatically picks up new files added via Obsidian etc."""
    topics = []
    for fname in sorted(os.listdir(memories_dir)):
        if not fname.endswith(".md") or fname in CLASSIFY_EXCLUDE:
            continue
        section_hint = ""
        try:
            with open(os.path.join(memories_dir, fname), "r", encoding="utf-8") as f:
                for ln in f:
                    if _is_header(ln):
                        section_hint = _header_text(ln)
                        break
        except OSError:
            pass
        topics.append((fname, section_hint))
    return topics


def _parse_classify_output(stdout, n_items, valid_files):
    """Parse classifier output. Expected format per line: '<num> <verdict>'.
    verdict = topic filename (e.g. about-user.md) | NEW:<label> | DROP.
    Returns: {idx (1-based): ('FILE', fname) | ('NEW', label) | ('DROP', '')}.
    Lines that cannot be parsed or reference an unknown filename are conservatively
    ignored by the caller (item stays in inbox)."""
    verdicts = {}
    for ln in stdout.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = re.match(r"^\[?(\d+)\]?[).:\s-]+\s*(.+)$", s)
        if not m:
            continue
        idx = int(m.group(1))
        if not (1 <= idx <= n_items):
            continue
        verdict = m.group(2).strip()
        up = verdict.upper()
        if up == "DROP":
            verdicts[idx] = ("DROP", "")
        elif up.startswith("NEW:") or up.startswith("NEW "):
            label = verdict[4:].strip().lstrip(":").strip()
            verdicts[idx] = ("NEW", label or "unknown")
        else:
            # filename verdict; normalize extension
            cand = verdict.split()[0].strip()
            if not cand.endswith(".md"):
                cand_md = cand + ".md"
            else:
                cand_md = cand
            if cand_md in valid_files:
                verdicts[idx] = ("FILE", cand_md)
            # unknown filename -> ignore (item stays in inbox, safe)
    return verdicts


def _run_classifier(items, topics):
    """Single classifier call for one chunk of inbox items.
    Judges each item as topic file / new topic / drop.
    Returns: (verdicts dict {1-based-idx: (kind, payload)}, ok bool).
    ok=False means the call failed; caller keeps this chunk's items in the inbox.

    CLASSIFY_CMD env seam: if set, runs that shell command (prompt piped on stdin)
    instead of the real claude call. Used by the unit-style dry test to stub responses
    without touching the API. Example: CLASSIFY_CMD='cat /tmp/verdicts.txt'"""
    topic_lines = "\n".join(
        f"- {fn}" + (f" (section: {sec})" if sec else "") for fn, sec in topics
    )
    numbered = "\n".join(f"{i}. {it}" for i, it in enumerate(items, 1))
    prompt = (
        # DGN-851: role noun via AGENT_ROLE config seam (same key as
        # CONSOLIDATE_PROMPT) -- never a hardcoded kit role.
        "너는 " + AGENT_ROLE + " __AGENT_LABEL__의 장기기억 분류기다. 받은편지함(inbox)에 쌓인 항목들을 "
        "현존 주제파일 중 하나로 배분하거나, 새 주제군으로 표시하거나, 가치 없으면 버린다.\n\n"
        "현존 주제파일 목록(여기 있는 파일명으로만 배분 가능):\n"
        f"{topic_lines}\n\n"
        "판정 규칙:\n"
        "- 항목이 위 주제파일 중 하나에 자연스럽게 속하면 그 파일명을 그대로 출력(예: about-user.md).\n"
        "- 어느 파일에도 안 맞지만 장기 기억할 가치가 있는 새 주제군이면 'NEW:<짧은라벨>' 출력(예: NEW:건강기록). 파일을 새로 만들지는 않는다.\n"
        "- 한 달~일 년 뒤에 쓸모없을 일회성·발화·진행성 항목이면 'DROP' 출력.\n"
        "- 애매하면 가장 가까운 기존 파일로 배분(새 주제 남발 금지).\n\n"
        "출력 형식(엄수): 항목마다 한 줄, '<번호> <판정>'. 판정은 파일명 | NEW:<라벨> | DROP 중 하나. "
        "그 외 설명·머리말 절대 금지.\n\n"
        "받은편지함 항목:\n"
        f"{numbered}\n"
    )
    valid_files = {fn for fn, _ in topics}
    # Preserve the shell/stdin/plain-text seam, including partial verdicts and
    # no vendor fallback on failure. Tests must never accidentally call a vendor.
    if CLASSIFY_CMD:
        cmd = ["bash", "-c", CLASSIFY_CMD]
        stdin_data = prompt
    else:
        return _call_classify_seats(prompt, len(items), valid_files)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True, text=True, timeout=CLASSIFY_CALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[error] classifier call failed: {e}", file=sys.stderr)
        return {}, False
    if proc.returncode != 0:
        print(f"[error] classifier exited abnormally (rc={proc.returncode}): {proc.stderr.strip()}",
              file=sys.stderr)
        return {}, False

    verdicts = _parse_classify_output(proc.stdout, len(items), valid_files)
    return verdicts, True


def _rewrite_inbox(path, keep_items):
    """Rewrite inbox.md keeping only keep_items (atomic replacement).
    Preserve the header (### unclassified (inbox)) and comment seed; replace only the § block body.
    If keep_items is empty, restore the inbox to just the seed comment."""
    # extract the header line and comment seed from the original (first header + HTML comment block).
    with open(path, "r", encoding="utf-8") as f:
        orig = f.read()

    header_line = "### unclassified (inbox)"
    for ln in orig.splitlines():
        if _is_header(ln):
            header_line = ln.rstrip()
            break

    # preserve comment seed (<!-- ... -->): take it verbatim from the original.
    seed_comment = ""
    mcomment = re.search(r"<!--.*?-->", orig, flags=re.DOTALL)
    if mcomment:
        seed_comment = mcomment.group(0)

    # M5: write the explicit section-format marker at the top of the rewritten
    # file so a later human § edit can't flip its chunking mode.
    parts = [_marker_line("section"), header_line, "§"]
    if seed_comment:
        parts.append(seed_comment)
        parts.append("§")
    for it in keep_items:
        parts.append(it)
        parts.append("§")
    # trailing stray § cleanup: a trailing § is parse-safe (empty blocks are ignored).
    new_content = "\n".join(parts) + "\n"

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(tmp, path)


def _apply_chunk_verdicts(chunk_items, verdicts, topics_set):
    """Tally verdicts for one chunk of items.
    chunk_items: list of item strings (in the chunk, 1-indexed within this chunk).
    verdicts: {1-based-idx: (kind, payload)} from _run_classifier for this chunk.
    topics_set: set of valid topic filenames (for file-missing guard at write time).
    Returns: (assign dict, new_groups dict, dropped list, keep_in_inbox list)
      assign:        {fname: [item, ...]}  -- items routed to topic files
      new_groups:    {label: [item, ...]}  -- NEW: label items (stay in inbox)
      dropped:       [item, ...]           -- DROP items
      keep_in_inbox: [item, ...]           -- new-topic holds + verdict-missing (safe)
    """
    assign = {}
    new_groups = {}
    dropped = []
    keep_in_inbox = []
    for i, it in enumerate(chunk_items, 1):
        v = verdicts.get(i)
        if not v:
            keep_in_inbox.append(it)
            continue
        kind, payload = v
        if kind == "FILE":
            assign.setdefault(payload, []).append(it)
        elif kind == "NEW":
            new_groups.setdefault(payload, []).append(it)
            keep_in_inbox.append(it)  # new topic: no file created, item stays in inbox
        elif kind == "DROP":
            dropped.append(it)
    return assign, new_groups, dropped, keep_in_inbox


def cmd_classify_inbox(args):
    """Weekly inbox classification -- chunked to avoid the 300s timeout death spiral.

    Splits the inbox into CLASSIFY_CHUNK_SIZE batches (default 100, env-overridable).
    Each chunk resolves its classifier seat, with 300s per attempt. Verdicts are applied immediately
    after each successful chunk (partial progress persists -- the inbox shrinks even if
    later chunks fail). A failed chunk is logged and skipped; its items stay in the inbox
    so they are retried on the next run.

    Exit-code contract (preserved from original):
      rc=0  real classification succeeded -- marker only when fully drained.
      rc=2  no items to classify -- no marker written, retries daily.
      rc=1  all chunks failed -- inbox untouched or partially drained (marker NOT written,
            retries next run).
    With a queue deadline, do not start a chunk unless its full call timeout
    and checkpoint margin fit. Progress is saved before returning rc=0 with
    carryover; the check wrapper resumes daily until the inbox is drained.
    """
    inbox_path = os.path.join(MEMORIES_DIR, "inbox.md")
    if not os.path.isfile(inbox_path):
        print("[classify-inbox] inbox.md not found — nothing to do (rc=2).")
        return CLASSIFY_RC_EMPTY

    items = _inbox_items(inbox_path)
    if not items:
        print("[classify-inbox] inbox empty (headers/comments/blank sections only) — done (no items, rc=2).")
        return CLASSIFY_RC_EMPTY

    deadline = _classify_deadline()
    n_total = len(items)
    chunk_size = max(1, CLASSIFY_CHUNK_SIZE)
    chunks = [items[i:i + chunk_size] for i in range(0, n_total, chunk_size)]
    n_chunks = len(chunks)
    print(f"[classify-inbox] inbox items: {n_total} — split into {n_chunks} chunk(s) of {chunk_size}")

    if not args.dry_run:
        _classify_progress(n_total, n_total, n_chunks)
    topics = _classify_topics(MEMORIES_DIR)
    if not topics:
        print("[error] no topic files to assign to — inbox preserved, abnormal exit (rc=1).",
              file=sys.stderr)
        return CLASSIFY_RC_FAIL
    print(f"[classify-inbox] topic candidates: {len(topics)}: " +
          ", ".join(fn for fn, _ in topics))
    topics_set = {fn for fn, _ in topics}

    if args.dry_run:
        # dry-run: classify every chunk, print verdicts, but touch nothing.
        print("\n[dry-run] classifying all chunks (no writes)...")
        for ci, chunk in enumerate(chunks, 1):
            print(f"\n== chunk {ci}/{n_chunks} ({len(chunk)} items) ==")
            verdicts, ok = _run_classifier(chunk, topics)
            if not ok:
                print(f"  [chunk {ci}] classifier failed — would stay in inbox")
                continue
            assign, new_groups, dropped, keep_in_inbox = _apply_chunk_verdicts(
                chunk, verdicts, topics_set
            )
            for fn, lst in assign.items():
                print(f"  [-> {fn}] {len(lst)} items")
                for it in lst:
                    print(f"      . {it}")
            if new_groups:
                print("  [new topic candidates]")
                for label, lst in new_groups.items():
                    mark = " *suggest" if len(lst) >= NEW_TOPIC_SUGGEST_MIN else ""
                    print(f"    NEW:{label} ({len(lst)} items){mark}")
                    for it in lst:
                        print(f"      . {it}")
            if dropped:
                print(f"  [dropped] {len(dropped)} items")
                for it in dropped:
                    print(f"      . {it}")
            if keep_in_inbox:
                print(f"  [inbox retain] {len(keep_in_inbox)} items")
        print("\n[dry-run] no file/inbox/reindex writes.")
        return CLASSIFY_RC_OK

    # ---- live run: process chunks, apply each immediately, rewrite inbox after each ----
    today = datetime.date.today().isoformat()

    # One-time inbox backup before the first write (protects the full pre-run state).
    bak = _backup_md(inbox_path)
    if bak:
        print(f"[classify-inbox] inbox backup: {os.path.basename(bak)}")

    # Aggregate accumulators (for final report).
    total_assign = {}   # fname -> [item, ...]
    total_new_groups = {}  # label -> [item, ...]
    total_dropped = []

    # Working copy of items still in the inbox (updated after each successful chunk).
    # Invariant: always reflects what is actually on disk at the end of each iteration.
    remaining_items = list(items)

    n_ok = 0
    n_fail = 0
    failed_chunk_sizes = []

    pending_chunks = 0
    for ci, chunk in enumerate(chunks, 1):
        if (deadline is not None and deadline - time.monotonic()
                < CLASSIFY_CALL_TIMEOUT + CLASSIFY_SAVE_MARGIN):
            pending_chunks = n_chunks - ci + 1
            break
        print(f"\n[classify-inbox] chunk {ci}/{n_chunks} ({len(chunk)} items)...")
        verdicts, ok = _run_classifier(chunk, topics)
        if not ok:
            n_fail += 1
            failed_chunk_sizes.append(len(chunk))
            print(f"  [chunk {ci}] classifier failed — {len(chunk)} items kept in inbox",
                  file=sys.stderr)
            # Items in this chunk stay in remaining_items (already there); no inbox rewrite needed.
            continue

        assign, new_groups, dropped, keep_in_inbox = _apply_chunk_verdicts(
            chunk, verdicts, topics_set
        )

        # Print per-chunk preview.
        for fn, lst in assign.items():
            print(f"  [-> {fn}] {len(lst)} items")
            for it in lst:
                print(f"      . {it}")
        if new_groups:
            for label, lst in new_groups.items():
                mark = " *suggest" if len(lst) >= NEW_TOPIC_SUGGEST_MIN else ""
                print(f"  [NEW:{label}] {len(lst)} items{mark}")
        if dropped:
            print(f"  [dropped] {len(dropped)} items")
        if keep_in_inbox:
            print(f"  [inbox retain] {len(keep_in_inbox)} items (new-topic/verdict-missing)")

        # Write to topic files immediately for this chunk.
        for fn, lst in assign.items():
            target = os.path.join(MEMORIES_DIR, fn)
            if not os.path.isfile(target):
                # target disappeared during run (race/manual edit) -> revert to inbox
                keep_in_inbox.extend(lst)
                print(f"  [warn] target file gone {fn} — reverting to inbox retain")
                continue
            tagged = [f"{it} ({today}, inbox-classified)" for it in lst]
            where = append_notes_to_md(target, None, tagged)
            print(f"  [write] {fn}: {len(lst)} items — {where}")

        # Accumulate for the final report.
        for fn, lst in assign.items():
            total_assign.setdefault(fn, []).extend(lst)
        for label, lst in new_groups.items():
            total_new_groups.setdefault(label, []).extend(lst)
        total_dropped.extend(dropped)

        # Compute the new set of items that should remain in inbox after this chunk:
        # remove the chunk's items from remaining_items, then add back keep_in_inbox.
        # remaining_items is ordered; we need to drop exactly this chunk's items.
        # The chunk is a contiguous slice of the original `items` list.
        # After earlier successful chunks already rewrote the inbox, remaining_items
        # is always the current inbox state -- so we drop all chunk items that were
        # NOT kept (i.e. assign+drop), i.e. keep only keep_in_inbox from this chunk.
        chunk_set_id = set(id(it) for it in chunk)  # identity-based: object ids unique per item
        # Rebuild: drop all items that are in this chunk (by object identity) from remaining,
        # then append the keep_in_inbox items for this chunk at the end.
        remaining_items = [it for it in remaining_items if id(it) not in chunk_set_id]
        remaining_items.extend(keep_in_inbox)

        # Rewrite inbox atomically to reflect partial progress (surviving items only).
        _rewrite_inbox(inbox_path, remaining_items)
        n_items_left = len(remaining_items)
        print(f"  [chunk {ci}] inbox rewritten: {n_items_left} items remaining")
        n_ok += 1
        _classify_progress(n_total, n_items_left, n_chunks - ci, reindex_pending=True)

    # ---- summary ----
    n_assigned_total = sum(len(v) for v in total_assign.values())
    print(f"\n[classify-inbox] done: {n_chunks} chunks total / {n_ok} ok / {n_fail} failed")
    print(f"[classify-inbox] items filed: {n_assigned_total} / dropped: {len(total_dropped)} / "
          f"inbox remaining: {len(remaining_items)}")
    if n_fail:
        carry_items = sum(failed_chunk_sizes)
        print(f"[classify-inbox] {carry_items} items carried over from {n_fail} failed chunk(s)",
              file=sys.stderr)

    _classify_progress(n_total, len(remaining_items), pending_chunks,
                       reindex_pending=n_ok > 0)
    if pending_chunks:
        print(f"[classify-inbox] {len(remaining_items)} items left, continues next dawn")
        # Each completed chunk is already durable. Leave indexing for the
        # next run; do not spend the checkpoint reserve on an unbounded tail.
        return (CLASSIFY_RC_OK if n_fail == 0 or n_total > len(remaining_items)
                else CLASSIFY_RC_FAIL)

    if n_ok == 0:
        # All chunks failed: inbox is untouched (or backed up at start but no chunk wrote).
        print("[error] all chunks failed — inbox.md preserved, abnormal exit (rc=1).", file=sys.stderr)
        return CLASSIFY_RC_FAIL

    # At least one chunk succeeded: partial or full success.
    suggestions = [label for label, lst in total_new_groups.items()
                   if len(lst) >= NEW_TOPIC_SUGGEST_MIN]

    # Reindex (topic files changed by assignments). DGN-1873: the classification is
    # already on disk (inbox rewritten per chunk), so a reindex that cannot get the
    # database never turns this run into a failure -- it reports rc=3 and the
    # check wrapper retries the reindex alone on the next scheduled run.
    print("[classify-inbox] reindexing...")
    reindexed = _classify_reindex(deadline)
    _classify_progress(n_total, len(remaining_items), 0, reindex_pending=not reindexed)

    # Report: include chunk failure note if any chunks failed.
    report = _build_classify_report(total_assign, suggestions, total_dropped, today,
                                    n_ok=n_ok, n_chunks=n_chunks)
    if report:
        print("\n-- report --\n" + report)
        # Preserve the normal silent report when its 60s send timeout fits.
        if not args.no_push and (deadline is None or deadline - time.monotonic() >= 65):
            if _send_silent_report(report):
                print("[classify-inbox] report sent silently.")

    if not reindexed:
        print("[classify-inbox] classification kept; search index not refreshed "
              f"(database busy) -- reindex deferred (rc={CLASSIFY_RC_REINDEX_DEFERRED}).",
              file=sys.stderr)
        return CLASSIFY_RC_REINDEX_DEFERRED
    return CLASSIFY_RC_OK


def cmd_inbox_count(args):
    """Print the actual count of classifiable items in inbox.md (shared 'empty' logic with the check wrapper).
    Outputs a single integer to stdout. Exit code: 0 if items exist, 2 if inbox is empty.
    The check wrapper uses only this rc to decide whether to call classify-inbox — since both
    sides use the same _inbox_items logic, 'has items' vs 'classify sees empty' mismatches are eliminated."""
    inbox_path = os.path.join(MEMORIES_DIR, "inbox.md")
    n = len(_inbox_items(inbox_path)) if os.path.isfile(inbox_path) else 0
    print(n)
    return CLASSIFY_RC_OK if n > 0 else CLASSIFY_RC_EMPTY


def _build_classify_report(assign, suggestions, dropped, today, n_ok=None, n_chunks=None):
    """Weekly classification report (user-friendly, no bold/asterisk emphasis).
    assign: {fname: [item, ...]} -- items filed to topic files (aggregated across chunks).
    suggestions: [new-topic label, ...] -- labels with 3+ items needing new files.
    dropped: [item, ...] -- items dropped across all chunks.
    today: ISO date string.
    n_ok/n_chunks: chunk counts (optional; included in report when some chunks failed).
    Returns None if nothing changed (nothing to send)."""
    n_assigned = sum(len(v) for v in assign.values())
    if n_assigned == 0 and not suggestions:
        return None
    md = datetime.date.today().strftime("%-m/%-d")
    lines = [f"Inbox sorted ({md})"]
    if n_assigned:
        lines.append(f"- {n_assigned} items filed by topic")
        for fn, lst in assign.items():
            lines.append(f"  . {fn.replace('.md','')}: {len(lst)}")
    if dropped:
        lines.append(f"- {len(dropped)} items dropped")
    if suggestions:
        lines.append("- New topics accumulated, create new files? -> " + ", ".join(suggestions))
    # Chunk failure note: surface when partial failure occurred so the next run is expected.
    if n_ok is not None and n_chunks is not None and n_ok < n_chunks:
        n_fail = n_chunks - n_ok
        lines.append(f"- {n_ok}/{n_chunks} chunks ok, {n_fail} failed (carried over to next run)")
    return "\n".join(lines)


# ======================================================================
# hook command (UserPromptSubmit auto-injection) — ported from inject_hook.py
# ======================================================================
# Design principles (never break):
#   1. Never block the agent turn. Any error exits silently with exit 0 + empty output.
#   2. Fast. Runs on every message, so embedding gets a short timeout and
#      the whole search is wrapped in a thread timeout as well.
#   3. Weak matches are not injected. If top1 cosine < cutoff, return empty.
# Output contract: JSON {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
#   "additionalContext": ...}} — wrapped in a system reminder so it is not exposed in chat.
HOOK_TOPK = 4              # top-N results to retrieve
HOOK_VEC_CUTOFF = 0.50    # skip injection if top1 cosine is below this (conservative)
HOOK_EMBED_TIMEOUT = 6    # Ollama embedding urlopen timeout (seconds)
HOOK_SEARCH_TIMEOUT = 9   # whole-search thread timeout (seconds)
HOOK_SNIPPET_MAX = 400    # max result body excerpt length
HOOK_STALE_LOCK_SEC = 600  # locks older than this are assumed dead-process remnants and ignored
HOOK_KW_TOPK = 4          # max notes to inject from keyword FTS fallback
HOOK_KW_MAX_TERMS = 6     # max keywords extracted from prompt and OR-combined
HOOK_KW_MIN_LEN = 3       # tokens shorter than this are discarded (noise/particles)
# If the NULL embedding fraction exceeds this value, emit a loud "semantic search degraded" warning (Ollama absent).
NULL_EMBED_WARN_FRAC = 0.5
# Common stopwords (Korean/English) to discard in keyword fallback. These are stripped
# when building the OR keyword query that replaces whole-prompt phrase matching.
HOOK_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "you", "your", "are", "was",
    "were", "has", "have", "had", "not", "but", "can", "will", "please", "about",
    "what", "when", "where", "which", "who", "how", "why", "from", "into", "then",
    "그리고", "그러나", "하지만", "그런데", "그래서", "그거", "이거", "저거",
    "뭐야", "뭔데", "무엇", "어떻게", "어디", "언제", "누구", "우리", "너는",
    "해줘", "알려줘", "해주라", "인데", "인가", "하는", "했던", "했어", "한거",
}


def _now_local_line():
    """Single-line current time (based on the OS local timezone of the user's machine). Prepended to
    every auto-injection to prevent the agent's 'time blindness'. Dogany is globally deployed —
    never hard-code KST; always follow the OS TZ.
    Uses stdlib only (no added dependencies).
    now().astimezone() gives an OS-local TZ-aware datetime; tzname() provides the abbreviation
    automatically (KST/PST/EST, etc.).
    Format: [현재 시각] 2026-06-26 (금) 09:36 KST  (abbreviation substituted by system TZ)."""
    now = datetime.datetime.now().astimezone()
    weekday = "월화수목금토일"[now.weekday()]
    tz = now.tzname() or ""
    suffix = f" {tz}" if tz else ""
    return f"[현재 시각] {now.strftime('%Y-%m-%d')} ({weekday}) {now.strftime('%H:%M')}{suffix}"


def _kit_dir_candidates():
    """Kit discovery ladder (spec 3.2), shared by the frozen body-state
    fallback and the ledger provider probe: LIFEKIT_DIR env ->
    PROJECT_ROOT/database -> HERE/../database.  Pure path construction, no
    behavior -- extracting it leaves the fallback's output bytes untouched
    (spec 8.3 / T6)."""
    candidates = []
    env_dir = os.environ.get("LIFEKIT_DIR")
    proj_root = os.environ.get("PROJECT_ROOT")
    if env_dir:
        candidates.append(env_dir)
    if proj_root:
        candidates.append(os.path.join(proj_root, "database"))
    candidates.append(os.path.normpath(os.path.join(HERE, "..", "database")))
    return candidates


def _hook_body_state_line():
    """Single-line current body/goal state (v2 deterministic injection). Returns a value if the life engine is present, else None.

    Key: this injection does not depend on search / topic-classification / agent judgment. If the life engine
    (config table) is attached, a body-state line is unconditionally inserted every turn, enforcing
    the 'read before asking' rule in code.
    For a base template with no life engine (import failure or body-state not configured) -> None -> no-op.
    Single cheap SQLite read. Swallows all exceptions to never block the turn (best effort)."""
    try:
        # The life engine lives in database/ at the repo root (path-independent).
        # If no ladder dir holds it (not installed / not configured) -> silent no-op.
        agent_root = os.environ.get("PROJECT_ROOT") or os.path.normpath(
            os.path.join(HERE, ".."))
        candidates = _kit_dir_candidates()
        # DGN-1238: the engine has shipped as lifekit.* AND kit_life.* -- live
        # fleets exist on both coordinates. Resolve the stem per instance via
        # the shared resolver (routines/lib/life_engine.py); never hardcode
        # either name here again.
        _le = None
        try:
            _le_dir = os.path.join(agent_root, "routines", "lib")
            if _le_dir not in sys.path:
                sys.path.insert(0, _le_dir)
            import life_engine as _le
        except BaseException:
            _le = None
        _stems = _le.KNOWN_STEMS if _le else ("kit_life", "lifekit")
        _pin = _le.pinned_stem(agent_root) if _le else None
        db_dir = stem = None
        for cand in candidates:
            # Both <stem>.py and the actual data (<stem>.db) must be present to read body-state.
            # If code exists but db does not (fresh clone) -> silent no-op, no stderr noise.
            if _le:
                s = _le.stem_in_dir(cand, ("py", "db"), _pin)
            else:  # resolver missing (hand-assembled tree): same probe, both stems
                s = next((c for c in _stems
                          if os.path.isfile(os.path.join(cand, c + ".py"))
                          and os.path.isfile(os.path.join(cand, c + ".db"))), None)
            if s:
                db_dir, stem = cand, s
                break
        if db_dir is None:
            # Engine absent is the NORMAL kitless state -> silent no-op. But a
            # kit that is CONFIGURED (agent.conf/kit conf says so) with no
            # findable engine must not be silent (DGN-1238 quiet-failure class).
            if _le and _le.configured(agent_root):
                _le.warn_missing(agent_root, "memory.py:body-state")
            return None
        if db_dir not in sys.path:
            sys.path.insert(0, db_dir)
        import importlib
        _lk = importlib.import_module(stem)
        # DGN-285 direction-A guard (locked 2026-07-23): decide from RAW config
        # rows, never from the merged load_body_stats() dict. The load contract
        # is {**DEFAULT_STATS, **rows}, so on a fresh instance the merged dict
        # still looks like a complete body (70kg/170cm code defaults) and the
        # caller cannot tell real user data from placeholders. Injecting that
        # as "[현재 신체/목표] ... 다시 묻지 말 것" fabricates a body and
        # poisons the fresh-user onboarding/consult interview.
        # Inject only when goal_mode + weight_kg + height_cm ALL exist as real
        # config rows AND goal_mode is non-blank. The previous COUNT(*) guard
        # was porous: any single unrelated key (e.g. 'updated') passed it.
        _REQUIRED_RAW_KEYS = ("goal_mode", "weight_kg", "height_cm")
        try:
            conn = _lk.get_conn()
            try:
                raw = dict(conn.execute(
                    "SELECT key, value FROM config "
                    "WHERE key IN (?, ?, ?);", _REQUIRED_RAW_KEYS
                ).fetchall())
            finally:
                conn.close()
        except BaseException:
            return None
        if any(k not in raw for k in _REQUIRED_RAW_KEYS):
            return None  # fresh/partial setup -> stay silent, let the interview run
        if not str(raw["goal_mode"] or "").strip():
            return None  # goal_mode only ever set by real user setup; blank = fresh
        stats = _lk.load_body_stats()
        today = datetime.date.today().isoformat()
        a = _lk.agg_day(today)
        burn_kcal = a.get('burn_kcal', 0) or 0
        t = _lk.compute_targets(stats, exercise_kcal=burn_kcal)
        g = _lk.compute_macro_goals(t["eff_goal"], stats)
        # Raw-row guard above already proved goal_mode exists and is non-blank
        # (goal_mode is non-numeric, so the merge passes it through unchanged).
        gm = stats.get("goal_mode", "")
        wt = stats.get("weight_kg", "")
        # Format float residuals (e.g. 84.0) as integer when exact, for readability. Does not affect computed values.
        def _n(x):
            try:
                fx = float(x)
                return int(fx) if fx == int(fx) else fx
            except (TypeError, ValueError):
                return x
        return (
            f"[현재 신체/목표] goal_mode={gm} weight={_n(wt)} "
            f"eff_goal={t['eff_goal']} protein={_n(g['protein'])} "
            f"carb={_n(g['carb'])} fat={_n(g['fat'])} "
            "(" + stem + " canonical; 사용자께 다시 묻지 말 것)"
        )
    except BaseException:
        return None  # life engine not configured / error -> no-op (must not block the turn)


# ======================================================================
# DGN-1176: ledger preload registry (spec v2.1) -- SoT is per-agent.
# ======================================================================
# The kit declares WHAT its ledgers are (a provider module, sot_registry.py,
# co-located with the kit engine); the framework owns WHEN/in what order/under
# what budget they are read, and every failure branch.  The union of a set
# entry (closure UNION unconditional) is computed HERE, never inside a kit
# callback -- removing the kit's freedom to omit the safety floor is the point
# (spec 3.3.3).  Read path is fail-open; SCOPE is the one fail-closed
# exception (unresolved identity suppresses axis-bearing callables entirely).
# No registry provider found -> the frozen fallback (_hook_body_state_line,
# unchanged above) runs exactly as before (spec 8.3).
HOOK_LEDGER_BUDGET_SEC = 2.0       # lane nominal cap (spec 5.5)
HOOK_TOTAL_SOFT_DEADLINE = 10.5    # whole-hook soft cap (893 B2 lock value)
HOOK_LEDGER_MIN_BUDGET_SEC = 0.2   # below this remaining budget: skip the lane
                                   # (time-line preservation outranks ledger lines)
LEDGER_SENTINEL = "[구조화상태]"   # 893 decision #4 sentinel, every block line
LEDGER_PROVIDER_MODULE = "sot_registry"   # the ONE name this contract fixes (spec 3.2)
LEDGER_HEARTBEAT_SECONDS = 3600
# Agent-only steering line, appended at block end (spec 4-3; U3-gated wording,
# replacement candidate #1 if U3 is ever ratified).
LEDGER_ASOF_NOTE = "(as_of 는 판단 근거용. 오래됐다는 선제 발화는 비준 전 -- 하지 말 것)"
# Ask-suppression clause (DGN-1176 regression seal).  The fallback line's
# trailing "... 사용자께 다시 묻지 말 것" is the ONLY textual basis for not
# re-asking a value the ledger already holds; in registry mode ask_allowed
# used to live only in the per-entry meta dict, which the model never sees
# (it reads the rendered block) -- so the instruction had silently moved out
# of the model's field of view.  This clause carries it back into the render.
# Rendered ONCE at block end iff any rendered entry's ask_allowed resolved
# False -- repeating the identical clause on every suppressed line is the
# defect shape DGN-1242 removed.  When per-entry verdicts diverge, the
# still-askable slugs are named in the clause (LEDGER_NO_REASK_EXC), so the
# fold never hides a per-entry difference.  The spec-3.4 verdict itself is
# untouched: an all-as_of-unknown entry keeps ask_allowed=True and rides as
# a named exception (the "value of unknowable age + never re-ask" pair stays
# forbidden -- MJ-3); a block of ONLY such entries gets no clause at all.
# Registry blocks mix sources, so unlike the fallback the clause claims no
# single engine stem as authority.  Wording is a FACT statement, not an
# imperative (maintainer verdict, 2026-09-02): resource_gate's source semantics
# are "the row exists, so re-asking is a defect", and the clause states that
# fact.  Not owner-gated copy: this string rides the memory hook's
# additionalContext into MODEL context only -- no code path reaches the
# Telegram body, the pinned dashboard, or any console surface.
LEDGER_NO_REASK_STEM = "원장에 기록된 값 -- 재질문은 결함"
LEDGER_NO_REASK_CLAUSE = "(" + LEDGER_NO_REASK_STEM + ")"
LEDGER_NO_REASK_EXC = "(" + LEDGER_NO_REASK_STEM + "; 예외 %s: as_of 전부 미상)"

_LEDGER_KINDS = ("scalar", "set")
_LEDGER_MODES = ("always", "on-demand")


def _ledger_agent_root():
    return os.environ.get("PROJECT_ROOT") or os.path.normpath(
        os.path.join(HERE, ".."))


def _ledger_log_paths():
    """(log_dir, log_file, zero_stamp) -- planned-meal-inject LOG_DIR pattern."""
    d = os.path.join(_ledger_agent_root(), ".telegram_bot", "logs")
    return (d, os.path.join(d, "ledger-preload.log"),
            os.path.join(d, ".ledger-preload.zero-stamp"))


def _ledger_log(line):
    """Canonical audit path = log file (spec 3.5, loud-log discipline taken
    whole from planned-meal-inject: no log lines over a day = lane death, not
    a quiet day).  Best-effort append; a hook that cannot log still must not
    fail the turn.  stderr breadcrumb kept as the interactive-debug aid."""
    try:
        log_dir, log_file, _ = _ledger_log_paths()
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (stamp, line))
    except Exception:
        pass
    try:
        sys.stderr.write("[ledger-preload] %s\n" % line)
    except Exception:
        pass


def _stamp_throttled(stamp_path, token=None, ttl_sec=None):
    """True -> stamp fresh (stay quiet); else refresh it, return False.
    The ONE throttle-stamp idiom for this file.  token mode: content ==
    token (once per token, e.g. an ISO date); ttl mode: mtime age (rolling).
    Filesystem trouble -> False (emit rather than lose a signal).
    life_engine.warn_missing keeps its own copy on purpose: the every-turn
    ledger lane must not import the routines-lib resolver to log."""
    try:
        if token is not None:
            try:
                with open(stamp_path, encoding="utf-8") as fh:
                    if fh.read().strip() == token:
                        return True
            except Exception:
                pass
        elif os.path.exists(stamp_path) and \
                time.time() - os.path.getmtime(stamp_path) < ttl_sec:
            return True
        os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
        with open(stamp_path, "w", encoding="utf-8") as fh:
            fh.write(token or "")
    except Exception:
        pass
    return False


def _ledger_log_zero():
    """Rate-limited liveness beat (1/h): proves 'quiet' is measured, not assumed."""
    _, _, stamp_file = _ledger_log_paths()
    if _stamp_throttled(stamp_file, ttl_sec=LEDGER_HEARTBEAT_SECONDS):
        return
    _ledger_log("ZERO")


def _ledger_provider_absent_breadcrumb():
    """Kit alive + provider module absent -> one visible log line per day
    (893 decision #7 capability breadcrumb, spec 8.3).  Never touches the
    injected output; the frozen fallback stays byte-identical."""
    log_dir, _, _ = _ledger_log_paths()
    stamp = os.path.join(log_dir, ".ledger-preload.provider-absent-stamp")
    if _stamp_throttled(stamp, token=datetime.date.today().isoformat()):
        return
    _ledger_log("PROVIDER-ABSENT kit alive, no %s.py on ladder -- "
                "frozen body-state fallback in use" % LEDGER_PROVIDER_MODULE)


def _ledger_exc_detail(res):
    """Colon-detail for a _deadline_call failure result (exc or timeout)."""
    if isinstance(res, BaseException):
        return "%s: %s" % (type(res).__name__, res)
    return "timeout"


def _ledger_provider_path():
    """Directory containing the provider module, or None (registry absent).

    Walks the same kit discovery ladder as the frozen fallback
    (_kit_dir_candidates, spec 3.2).  Only the provider MODULE name is fixed
    by this contract; the engine file stem stays kit-declared (DGN-1238) and
    is not consulted here."""
    for cand in _kit_dir_candidates():
        try:
            if os.path.isfile(os.path.join(cand, LEDGER_PROVIDER_MODULE + ".py")):
                return cand
        except Exception:
            continue
    return None


def _ledger_validate_entry(entry):
    """R1~R7 registration checks (spec 3.5).  (False, reason) -> reject.

    R1 is the asymmetric one on purpose: an EXPLICIT empty unconditional list
    is a conscious declaration and passes; ABSENCE of the field is an
    unconscious omission and is rejected -- an FK-only traversal drops exactly
    the inviolable goal_id-NULL safety rows, and a silently-short set in the
    safety quadrant is worse than not reading at all."""
    if not isinstance(entry, dict):
        return False, "form entry-not-dict"
    slug = entry.get("slug")
    if not isinstance(slug, str) or not slug or any(ord(c) > 127 for c in slug):
        return False, "form slug"
    kind = entry.get("kind")
    mode = entry.get("mode")
    if kind not in _LEDGER_KINDS:
        return False, "form kind"
    if mode not in _LEDGER_MODES:
        return False, "form mode"
    if kind == "set" and mode == "on-demand":
        return False, "R7"      # inviolable floor must not stand behind a match
    if kind == "set" and "unconditional" not in entry:
        return False, "R1"      # field ABSENT (explicit empty-list callable passes)
    if kind == "set" and ("closure" not in entry or "anchor" not in entry):
        return False, "R2"
    scope = entry.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("tables"), dict):
        return False, "R3"
    for tdecl in scope["tables"].values():
        # axis: None is an explicit crew-shared declaration and passes; the
        # KEY must exist per declared table (conscious declaration vs
        # unconscious omission, same shape as R1).
        if not isinstance(tdecl, dict) or "axis" not in tdecl:
            return False, "R3"
        if tdecl["axis"] is not None and not isinstance(tdecl["axis"], str):
            return False, "R3"
    if mode == "on-demand" and "match" not in entry:
        return False, "R4"
    for f in ("read", "anchor", "closure", "unconditional", "match"):
        if f in entry and not callable(entry[f]):
            return False, "R6 %s" % f
    if kind == "scalar" and not callable(entry.get("read")):
        return False, "R6 read"
    return True, ""


def _ledger_registry_load(dirpath, budget_sec):
    """Provider import + per-entry validation.  Returns (provider, entries)
    or None (import failure -> frozen fallback; module absence is decided by
    the caller via _ledger_provider_path).  Import + get_ledger_registry()
    run under the deadline wrapper: a hung provider import must not eat the
    harness 12s and kill the time line.  Per-process once; the hook is a
    fresh process per turn, so the import cost repeats per turn (spec 5.5)."""
    def _load():
        if dirpath not in sys.path:
            sys.path.insert(0, dirpath)
        import importlib
        provider = importlib.import_module(LEDGER_PROVIDER_MODULE)
        return provider, provider.get_ledger_registry()
    ok, res = _deadline_call(_load, (), budget_sec)
    if not ok:
        _ledger_log("FAIL import %s" % _ledger_exc_detail(res))
        return None
    provider, raw = res
    if not isinstance(raw, list):
        _ledger_log("FAIL import get_ledger_registry returned %s"
                    % type(raw).__name__)
        return None
    entries = []
    seen = set()
    for entry in raw:
        ok, reason = _ledger_validate_entry(entry)
        slug = entry.get("slug", "?") if isinstance(entry, dict) else "?"
        if not ok:
            _ledger_log("REJECT %s %s" % (slug, reason))
            continue
        if slug in seen:
            _ledger_log("REJECT %s R5" % slug)   # later duplicate rejected
            continue
        seen.add(slug)
        entries.append(entry)
    return provider, entries


def _ledger_entry_needs_identity(entry):
    """True iff any declared table carries a non-None axis."""
    tables = entry.get("scope", {}).get("tables", {})
    return any(isinstance(t, dict) and t.get("axis") is not None
               for t in tables.values())


def _ledger_resolve_scope(entry, identity):
    """FRAME owns only the rule, not the resolution (spec 3.3.2): identity is
    produced by the provider's resolve_identity() -- FRAME never reads
    agent.conf / GOAL_OWNING_AGENT / SLUG.  identity None/blank + axis-bearing
    entry -> None -> the entry's callables are never invoked (fail-closed) and
    the caller logs SKIP-SCOPE.  Axis-free entries need no identity."""
    clean = identity.strip() if isinstance(identity, str) else ""
    if _ledger_entry_needs_identity(entry) and not clean:
        return None
    return {"identity": clean or None}


def _deadline_call(fn, args, budget_sec):
    """Daemon thread + join(budget) -- the one wall-clock cutoff primitive
    (the hook search lane wraps it too, via _hook_search_with_timeout).
    The only mechanism that cuts a single blocking call (e.g. a kit busy-5s
    DB wait) on the wall clock (spec 5.4/MJ-1).  Returns (ok, result):
    (True, value) on normal return; (False, exc) if fn raised (BaseException
    including SystemExit -- 893 B5 contract); (False, None) on timeout.  The
    timed-out thread is not killed; the per-turn hook process dies at turn
    end, which is the accepted disposal."""
    if budget_sec is None or budget_sec <= 0:
        return False, None
    import threading
    box = {}

    def worker():
        try:
            box["r"] = fn(*args)
        except BaseException as e:
            box["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(budget_sec)
    if t.is_alive():
        return False, None
    if "e" in box:
        return False, box["e"]
    return True, box.get("r")


def _ledger_asof(item):
    """Per-value/row as_of: absent/blank timestamps render as the EXPLICIT
    string 'unknown' -- omitting the age to look fresh is the exact shape of
    the J1 defect (spec 4)."""
    a = item.get("as_of") if isinstance(item, dict) else None
    if isinstance(a, str) and a.strip():
        return a.strip()
    return "unknown"


def _ledger_ask_allowed(verdict, asofs):
    """3-verdict ask gate (spec 3.4, FRAME-owned so the guarantee does not
    depend on kit compliance): KNOWN with at least one resolved as_of ->
    False (re-asking a recorded fact is a bug); KNOWN with every as_of
    unknown -> True ('value of unknowable age' + 'never re-ask' is a
    forbidden combination); UNKNOWN -> True.  Binary provenance fact, not an
    age threshold -- no U8 violation."""
    if verdict != "KNOWN":
        return True
    return not any(a != "unknown" for a in asofs)


def _ledger_row_label(row):
    """Row display text.  Rows are kit-shaped dicts; FRAME looks for a label
    under 'label' / 'key' / 'name' and an optional 'value'."""
    if not isinstance(row, dict):
        return str(row)
    label = row.get("label") or row.get("key") or row.get("name") or "?"
    value = row.get("value")
    if value is None:
        return str(label)
    return "%s=%s" % (label, value)


def _ledger_run_scalar(entry, ctx, remaining):
    """One scalar entry.  Returns (line_or_None, meta).  UNKNOWN or a dropped
    read = ZERO BYTES for this entry (planned-meal-inject discipline: not a
    shorter line -- nothing), never a default value."""
    slug = entry["slug"]
    ok, res = _deadline_call(entry["read"], (ctx,), remaining())
    if not ok:
        _ledger_log("DROP %s read: %s" % (slug, _ledger_exc_detail(res)))
        return None, {"slug": slug, "verdict": "UNKNOWN", "ask_allowed": True}
    if not isinstance(res, dict) or res.get("verdict") not in ("KNOWN", "UNKNOWN"):
        _ledger_log("DROP %s read: malformed ReadResult" % slug)
        return None, {"slug": slug, "verdict": "UNKNOWN", "ask_allowed": True}
    values = res.get("values") or []
    asofs = [_ledger_asof(v) for v in values]
    verdict = res.get("verdict")
    if verdict != "KNOWN" or not values:
        meta = {"slug": slug, "verdict": "UNKNOWN",
                "ask_allowed": _ledger_ask_allowed("UNKNOWN", asofs)}
        return None, meta
    parts = []
    for v, a in zip(values, asofs):
        name = v.get("name", "?") if isinstance(v, dict) else "?"
        value = v.get("value") if isinstance(v, dict) else v
        src = v.get("source") if isinstance(v, dict) else None
        tail = "(as_of=%s%s)" % (a, (", source=%s" % src) if src else "")
        parts.append("%s=%s %s" % (name, value, tail))
    ask = _ledger_ask_allowed("KNOWN", asofs)
    line = "%s %s: %s" % (LEDGER_SENTINEL, slug, " ".join(parts))
    return line, {"slug": slug, "verdict": "KNOWN", "ask_allowed": ask}


def _ledger_run_set(entry, ctx, remaining):
    """One set entry.  SET = closure UNION unconditional, and the union is
    computed HERE (spec 3.3.3).  anchor/closure/unconditional are invoked and
    isolated INDEPENDENTLY; the failure disposal is asymmetric (v2.1 BL-A):

      closure/anchor drop, unconditional alive -> KNOWN + [부분] + DROP log
        (what survives is the safety-floor side: conservative direction)
      unconditional drop -> the WHOLE set is UNKNOWN (zero bytes; live
        closure rows are NOT injected) + FAIL log
        (a set missing exactly its safety floor is worse than no set -- R1's
        rationale applied at runtime, not only at registration)

    anchor -> None is a real state (no active goal), NOT a drop: closure is
    skipped (never called), unconditional still runs -- the fewer goals
    exist, the more the safety floor is needed."""
    slug = entry["slug"]
    partial = False
    closure_rows = []
    ok, anchor = _deadline_call(entry["anchor"], (ctx,), remaining())
    if not ok:
        _ledger_log("DROP %s anchor: %s" % (slug, _ledger_exc_detail(anchor)))
        partial = True
    elif anchor is not None:
        ok2, rows = _deadline_call(entry["closure"], (ctx, anchor), remaining())
        if not ok2:
            _ledger_log("DROP %s closure: %s" % (slug, _ledger_exc_detail(rows)))
            partial = True
        elif rows:
            closure_rows = list(rows)
    # unconditional: independent of the anchor/closure fate, always attempted.
    oku, urows = _deadline_call(entry["unconditional"], (ctx,), remaining())
    if not oku:
        # Asymmetric disposal: closure rows may be alive -- they are dropped
        # with the set.  FAIL (not DROP) grammar per spec 3.5.
        _ledger_log("FAIL unconditional %s: %s" % (slug, _ledger_exc_detail(urows)))
        return None, {"slug": slug, "verdict": "UNKNOWN", "ask_allowed": True}
    urows = list(urows or [])
    # Union, framework-owned.  Dedup by row 'id' when present (closure and
    # unconditional are disjoint by construction in the real schema --
    # goal_id chain vs goal_id IS NULL -- so this is a belt, not a merge);
    # the unconditional marking wins on collision.
    uncond_ids = {r.get("id") for r in urows
                  if isinstance(r, dict) and r.get("id") is not None}
    merged = [(r, False) for r in closure_rows
              if not (isinstance(r, dict) and r.get("id") is not None
                      and r.get("id") in uncond_ids)]
    merged += [(r, True) for r in urows]
    if not merged:
        return None, {"slug": slug, "verdict": "UNKNOWN", "ask_allowed": True}
    asofs = [_ledger_asof(r) for r, _ in merged]
    items = []
    for (r, is_uncond), a in zip(merged, asofs):
        prefix = "[무조건] " if is_uncond else ""
        items.append("%s%s (as_of=%s)" % (prefix, _ledger_row_label(r), a))
    ask = _ledger_ask_allowed("KNOWN", asofs)
    mark = "[부분] " if partial else ""
    line = "%s %s: %s%d행 -- %s" % (
        LEDGER_SENTINEL, slug, mark, len(merged), " | ".join(items))
    return line, {"slug": slug, "verdict": "KNOWN", "partial": partial,
                  "ask_allowed": ask}


def _ledger_traverse(provider, entries, prompt, deadline):
    """Traversal core (spec 6).  Declaration order, deterministic.  Returns
    (block_or_None, metas); metas expose per-entry verdict/ask_allowed for
    tests and the future (b) gate (spec 7, defined but not activated)."""
    def remaining():
        return deadline - time.monotonic()

    machine = bool(prompt) and _is_marker_noise_row(prompt)
    # 1b. identity: ONCE per traversal, before any kit callable or match
    # (spec 3.3.2).  Provider-owned resolution; a raise/timeout/blank ->
    # None (fail-closed direction; the lane continues -- axis-free entries
    # are unaffected).
    identity = None
    rfn = getattr(provider, "resolve_identity", None)
    if callable(rfn):
        ok, res = _deadline_call(rfn, (), remaining())
        if ok and isinstance(res, str) and res.strip():
            identity = res.strip()
    lines = []
    metas = []
    for entry in entries:
        if remaining() <= 0:
            _ledger_log("FAIL budget exceeded -- remaining entries dropped")
            break
        slug = entry["slug"]
        # 2a. scope BEFORE match (v2 order fix): no kit callable of an
        # axis-bearing entry runs before scope is resolved.
        ctx = _ledger_resolve_scope(entry, identity)
        if ctx is None:
            _ledger_log("SKIP-SCOPE %s" % slug)
            continue
        # 2b. on-demand: machine-traffic turns skip match entirely; always
        # entries still run (the safety floor rides machine turns too).
        if entry["mode"] == "on-demand":
            if prompt is None or machine:
                continue   # normal skip, no log
            okm, hit = _deadline_call(entry["match"], (prompt,), remaining())
            if not okm:
                _ledger_log("DROP %s match: %s" % (slug, _ledger_exc_detail(hit)))
                continue
            if not hit:
                continue   # normal skip
        if entry["kind"] == "scalar":
            line, meta = _ledger_run_scalar(entry, ctx, remaining)
        else:
            line, meta = _ledger_run_set(entry, ctx, remaining)
        metas.append(meta)
        if line:
            lines.append(line)
    if not lines:
        _ledger_log_zero()
        return None, metas
    n_body = len(lines)
    # Ask-suppression clause, folded to ONE block-end line (see the
    # LEDGER_NO_REASK_CLAUSE comment).  A meta with verdict KNOWN is exactly
    # a rendered entry line -- both runners return (line, KNOWN) together or
    # (None, UNKNOWN) together -- so the fold covers precisely the block.
    known = [m for m in metas if m.get("verdict") == "KNOWN"]
    if any(not m.get("ask_allowed") for m in known):
        askable = sorted(m["slug"] for m in known if m.get("ask_allowed"))
        clause = (LEDGER_NO_REASK_EXC % ", ".join(askable) if askable
                  else LEDGER_NO_REASK_CLAUSE)
        lines.append("%s %s" % (LEDGER_SENTINEL, clause))
    lines.append("%s %s" % (LEDGER_SENTINEL, LEDGER_ASOF_NOTE))
    _ledger_log("INJECT n=%d" % n_body)
    return "\n".join(lines), metas


def _hook_ledger_preload(prompt, t0=None):
    """Ledger lane entry point (spec 5.1).  prompt=None is allowed: only
    always entries traverse (match never called), so the stdin-parse-failure
    path keeps ledger injection exactly as the old compose-internal
    body-state call did.  Never raises; never blocks past the lane budget.

    Registry absent (no provider module on the discovery ladder) -> the
    frozen fallback: _hook_body_state_line() exactly as today, including its
    current (unbudgeted) risk profile -- byte-identical output for
    undeclared instances is the contract (spec 8.3, T6)."""
    try:
        dirpath = _ledger_provider_path()
        if dirpath is None:
            bs = _hook_body_state_line()     # frozen fallback -- output unchanged
            if bs is not None:
                # 893 decision #7 breadcrumb, applied to provider discovery
                # (spec 8.3): the kit is alive (a body-state line resolved)
                # but no sot_registry.py is on the ladder.  1/day throttle --
                # a kitless mint (bs None) stays silent, planned-meal style.
                _ledger_provider_absent_breadcrumb()
            return bs
        lane_start = time.monotonic()
        if t0 is None:
            budget = HOOK_LEDGER_BUDGET_SEC
        else:
            budget = min(HOOK_LEDGER_BUDGET_SEC,
                         HOOK_TOTAL_SOFT_DEADLINE - (lane_start - t0))
        if budget <= HOOK_LEDGER_MIN_BUDGET_SEC:
            # Time-line preservation outranks ledger lines: past 12s the
            # harness kills EVERYTHING (893 Q2 mode) -- skip the lane.
            _ledger_log("FAIL budget lane-skip remaining=%.3f" % budget)
            return None
        # ONE deadline for the whole lane: import + identity + traversal all
        # burn the same budget (an import that eats 1.9s leaves 0.1s for the
        # rest, never a fresh 2.0s).
        deadline = lane_start + budget
        loaded = _ledger_registry_load(dirpath, deadline - time.monotonic())
        if loaded is None:
            return _hook_body_state_line()   # import failure -> frozen fallback
        provider, entries = loaded
        block, _metas = _ledger_traverse(provider, entries, prompt, deadline)
        return block
    except BaseException as e:
        try:
            _ledger_log("FAIL lane %s: %s" % (type(e).__name__, e))
        except BaseException:
            pass
        return None


# ----------------------------------------------------------------------
# DGN-2028 (v2.7 merged design s4): turn frame lane. Behind TURN_FRAME
# (agent.conf, or DOGANY_TURN_FRAME), default OFF: with it off the library
# is never loaded and hook output is the pre-DGN-2028 bytes. With it on, a
# [turn frame] block follows the time line on owner turns (both the normal
# and the empty-recall branch). Never on machine turns or inside a dispatch
# worker. Budget: min(TURN_FRAME_BUDGET_SEC, whole-hook time left after the
# compose headroom), monotonic wall time; a timed-out or failed frame is
# omitted and logged, never the turn.
# ----------------------------------------------------------------------
TURN_FRAME_KEY = "TURN_FRAME"
TURN_FRAME_BUDGET_SEC = 0.150
TURN_FRAME_COMPOSE_HEADROOM_SEC = 0.5
TURN_FRAME_MIN_SEC = 0.010
_TURN_FRAME_LIB = []

# One resolved ownership map per hook invocation, shared by vector/keyword
# rendering. Reset even when cmd_hook exits, so in-process callers cannot
# carry a previous turn's homes. No source/ownership work with TURN_FRAME off.
from contextvars import ContextVar
_TURN_SOURCES = ContextVar("turn_sources", default=None)


def _hook_source_context(prompt, t0):
    """Read the source ladder before recall without moving the ledger lane.

    This uses the frame lane's per-phase cap and whole-hook headroom.
    No provider subprocesses, history folds, model calls or network requests.
    A stuck read is discarded; recall then keeps its existing routing.
    """
    if (not _turn_frame_enabled() or os.environ.get("DOGANY_DISPATCH_WORKER")
            or _is_marker_noise_row(_recall_prompt_body(prompt)) or _recall_is_machine(prompt)):
        return None
    start = time.monotonic()
    budget = min(TURN_FRAME_BUDGET_SEC,
                 HOOK_TOTAL_SOFT_DEADLINE - (start - t0) - TURN_FRAME_COMPOSE_HEADROOM_SEC)
    if budget < TURN_FRAME_MIN_SEC:
        return None
    import threading
    box = {}

    def run():
        try:
            tf = _turn_frame_lib()
            bc = tf.source_loader()
            resolved = bc.resolve_sources(_recall_agent_root(), prompt, start + budget)
            box["out"] = (bc, resolved, bc.render_sources(resolved))
        except Exception as e:
            _ledger_log("OMIT source-context %s" % type(e).__name__)

    th = threading.Thread(target=run, name="source-context", daemon=True)
    th.start()
    th.join(max(0.0, start + budget - time.monotonic()))
    if th.is_alive():
        _ledger_log("OMIT source-context timeout")
        return None
    return box.get("out")


def _turn_frame_enabled():
    raw = (os.environ.get("DOGANY_" + TURN_FRAME_KEY)
           or _agent_conf_get(TURN_FRAME_KEY) or "").strip().lower()
    return raw in ("1", "on", "true", "yes")


def _turn_frame_lib():
    if not _TURN_FRAME_LIB:
        mod = None
        try:
            import importlib.util
            path = os.path.join(HERE, "..", "routines", "lib", "turn_frame.py")
            spec = importlib.util.spec_from_file_location("_mem_turn_frame", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            mod = None
        _TURN_FRAME_LIB.append(mod)
    return _TURN_FRAME_LIB[0]


def _hook_turn_frame(prompt, session_id, t0, raw_prompt=None, resolved_sources=None):
    """The [turn frame] block for this owner turn, or None (flag off,
    machine turn, dispatch worker, budget, error). raw_prompt = the stdin
    prompt before any stripping: turn identity and the Stop match use it."""
    try:
        if not _turn_frame_enabled() or not isinstance(prompt, str) or not prompt.strip():
            return None
        if os.environ.get("DOGANY_DISPATCH_WORKER"):
            return None
        if (_is_marker_noise_row(_recall_prompt_body(prompt))
                or _recall_is_machine(prompt)):
            return None
        start = time.monotonic()
        left = HOOK_TOTAL_SOFT_DEADLINE - (start - t0) - TURN_FRAME_COMPOSE_HEADROOM_SEC
        budget = min(TURN_FRAME_BUDGET_SEC, left)
        if budget < TURN_FRAME_MIN_SEC:
            _ledger_log("OMIT turn-frame budget remaining=%.3f" % left)
            return None
        tf = _turn_frame_lib()
        if tf is None:
            _ledger_log("OMIT turn-frame library unavailable")
            return None
        root = os.path.normpath(os.path.join(HERE, ".."))
        # Daemon thread joined for the budget: a step that blocks cannot hold
        # the hook past it. A late result is discarded; the lane re-checks the
        # deadline right before it records, so a late frame is not recorded.
        import threading
        box = {}

        def _run():
            try:
                box["out"] = tf.build_frame(prompt, session_id, root, start + budget,
                                            _ledger_log, raw_prompt=raw_prompt,
                                            resolved_sources=resolved_sources)
            except BaseException as e:  # noqa: BLE001 -- reported below
                box["err"] = e
        th = threading.Thread(target=_run, name="turn-frame", daemon=True)
        th.start()
        th.join(max(0.0, start + budget - time.monotonic()))
        if th.is_alive() or "out" not in box:
            err = box.get("err")
            _ledger_log("OMIT turn-frame %s budget=%.3f" % (
                "timeout" if err is None or isinstance(err, tf.FrameTimeout)
                else type(err).__name__, budget))
            return None
        return box["out"]
    except BaseException as e:
        try:
            _ledger_log("OMIT turn-frame %s" % type(e).__name__)
        except BaseException:
            pass
        return None


def _hook_usage_context():
    """DGN-1995: current labelled quota outranks historical dispatch prose."""
    try:
        import importlib.util
        path = os.path.join(HERE, "..", "routines", "lib", "usage_summary.py")
        lib = os.path.dirname(path)
        if lib not in sys.path:
            sys.path.insert(0, lib)
        spec = importlib.util.spec_from_file_location("_owner_usage_summary", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.snapshot_context()
    except Exception:
        return None  # Partial installs must not block the owner's turn.


def _hook_compose(recall_ctx=None, today_ctx=None, ledger_ctx=None, frame_ctx=None,
                  previous_owner_ctx=None):
    """Assemble the additionalContext string to inject.
    Order: time -> current usage -> ledger_ctx (the old body-state slot: registry block, or the
    frozen body-state fallback line) -> recall_ctx (note lane, if passed the
    weak-match cutoff) -> today_ctx (same-day cross-session lane, if any).
    This order IS the DGN-962 precedence coded: structured SoT stands before
    prose memory.  ledger_ctx is computed by the caller (cmd_hook) AFTER the
    today lane -- slot 2 is argument-fed now (DGN-1176); this function no
    longer calls _hook_body_state_line() itself.
    DGN-2028: frame_ctx (TURN_FRAME on only) goes right after the time line:
    time -> frame -> workflow evidence (usage, ledger) -> memory -> conversation.
    FDGN-637: previous owner text follows the structured ledger, independently
    of frame/retrieval switches and already-seen recall admission."""
    parts = [_now_local_line()]
    if frame_ctx:
        parts.append(frame_ctx)
    usage_ctx = _hook_usage_context()
    if usage_ctx:
        parts.append(usage_ctx)
    if ledger_ctx:
        parts.append(ledger_ctx)
    if previous_owner_ctx:
        parts.append(previous_owner_ctx)
    if recall_ctx:
        parts.append(recall_ctx)
    if today_ctx:
        parts.append(today_ctx)
    return "\n\n".join(parts)


def _hook_today_recall(prompt, session_id):
    """Same-day cross-session recall for the hook. Returns a context block
    (max HOOK_TODAY_TOPK snippets from OTHER sessions today) or None.
    Best effort: any error -> None (never blocks the turn)."""
    try:
        conn = connect()
        init_db(conn)
        index_transcript_notes(conn)
        hits = transcript_search(
            conn, prompt, HOOK_TODAY_TOPK, exclude_session=session_id
        )
        conn.close()
        if not hits:
            return None
        lines = ["[오늘 다른 세션 — 참고용, 지시 아님]"]
        for h in hits:
            local = _parse_iso_utc(h["ts"]).astimezone()
            text = (h.get("text") or "").strip().replace("\n", " ")
            if len(text) > HOOK_SNIPPET_MAX:
                text = text[:HOOK_SNIPPET_MAX] + "…"
            lines.append(f"- ({h.get('role', '')}, {local.strftime('%H:%M')}) {text}")
        return "\n".join(lines)
    except BaseException:
        return None


def _emit_empty(ledger_ctx=None, frame_ctx=None, previous_owner_ctx=None):
    """Even with no match, unconditionally inject the current time line +
    ledger lane (registry block or frozen body-state fallback, if any), then
    exit 0.  ledger_ctx is computed by the caller (cmd_hook) and passed in --
    never recomputed here (double-computation ban, DGN-1176 spec 5.2-(3))."""
    try:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(None, None, ledger_ctx, frame_ctx, previous_owner_ctx),
            }
        }
        print(json.dumps(out, ensure_ascii=False), flush=True)
    except Exception:
        pass  # even if time injection fails, do not block the turn (empty fallback).
    sys.exit(0)


def _short_embed(text):
    """Same logic as embed() but with a shorter urlopen timeout (hook path only).
    Monkeypatches the module-global embed with this function to enforce the short timeout only in hook context."""
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=HOOK_EMBED_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if not vec or not isinstance(vec, list):
        raise ValueError("Ollama response missing embedding field")
    return vec


def _hook_is_stale():
    """Returns True if the latest source mtime (memories/*.md) > state.db mtime.
    If db does not exist -> stale (initial build). If no sources found -> not stale."""
    if not os.path.isdir(MEMORIES_DIR):
        return False
    src_mtime = 0.0
    found = False
    for name in os.listdir(MEMORIES_DIR):
        if not name.endswith(".md") or name in INDEX_EXCLUDE:
            continue
        try:
            src_mtime = max(src_mtime, os.path.getmtime(os.path.join(MEMORIES_DIR, name)))
            found = True
        except OSError:
            continue
    if not found:
        return False
    try:
        db_mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return True  # db absent -> build needed
    return src_mtime > db_mtime


def _hook_acquire_lock(lock_path):
    """Atomically acquire a lock (O_CREAT|O_EXCL). Returns True on success, False if already held.
    Exception: if the lock is stale (older than HOOK_STALE_LOCK_SEC), remove it and retry."""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            age = 0
        if age > HOOK_STALE_LOCK_SEC:
            try:
                os.remove(lock_path)
            except OSError:
                pass
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except OSError:
                return False
        return False


def _hook_maybe_reindex():
    """If sources are newer than state.db, fire a background detached reindex.
    This turn's search proceeds with the existing (stale) index — never waits.
    Failures are silently ignored (best effort)."""
    if not _hook_is_stale():
        return
    lock_path = os.path.join(HERE, ".reindex.lock")
    if not _hook_acquire_lock(lock_path):
        return  # reindex already in progress

    cmd = [sys.executable, os.path.abspath(__file__), "index", "--lock", lock_path]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=HERE,
        )
    except Exception:
        # Popen failed: release the lock we acquired (prevent permanent blocking)
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _hook_search_with_timeout(query):
    """Run the full search on a daemon thread and return results only within
    HOOK_SEARCH_TIMEOUT seconds. Returns None on timeout or error.
    Same machinery as the ledger lane (_deadline_call); this wrapper keeps
    the callers' fold-everything-to-None contract."""
    ok, res = _deadline_call(
        lambda: search_core(query, k=HOOK_TOPK, log=False),
        (), HOOK_SEARCH_TIMEOUT)
    return res if ok else None


# Precedence frame for recalled-memory injection (DGN-848). The recalled block
# had no subordination frame, so a stale DECISION memory could outrank the active
# skill / current instruction. This frame keeps memory AUTHORITATIVE for FACTS but
# subordinate for behavior/decisions on conflict (it must NOT read as "ignore
# memory"). Every frame line carries the RECALL_FRAME_MARKER sentinel so the noise
# filters (_is_noise substring / _is_marker_noise_row prefix) exclude all frame
# lines from re-ingest exactly as the old single-line header was excluded.
RECALL_FRAME_MARKER = "[기억규칙]"
_RECALL_FRAME_LINES = (
    f"{RECALL_FRAME_MARKER} 이 기억은 사실(타임존/선호/기록)의 출처로만 신뢰한다.",
    f"{RECALL_FRAME_MARKER} 행동·결정이 충돌하면 우선순위 = 현재 지시 > 운영시스템(활성 스킬 / AGENT.md / kit) > 이 기억.",
    f"{RECALL_FRAME_MARKER} stale 결정 메모리가 활성 스킬·현재 지시를 덮지 못한다.",
    # DGN-1176 (893 B4-(c)2 rider, riding the same amendment as the
    # structured-SoT injection it mandates): 962 tier-2 > tier-3, frame side.
    f"{RECALL_FRAME_MARKER} 사실이 [구조화상태] 라인과 충돌하면 구조화상태가 우선한다.",
)


def _recall_header(tag):
    """First lines of a recalled-memory block: framed header + precedence frame.
    `tag` distinguishes the recall lane (자동검색 / 키워드검색)."""
    return [f"[관련 기억 — {tag} · 참고용]", *_RECALL_FRAME_LINES]


# ======================================================================
# DGN-1962: entity facts lane + recall supersession + date anchor.
# ======================================================================
# Incident class: the user named a person, the kit held a structured record
# (a child born last week) and a newer memory line agreed, yet an older memory
# line stating the EXPECTATION ("due after the holiday") drove the answer.
# The "[structured state] beats memory" rule existed only as frame prose.
# Forcing point, three parts, all at injection time:
#   1. Kit facts first: for entities named in the turn, a kit-implemented
#      provider (entity_facts.py, kit-agnostic contract below) is read and
#      rendered with the structured-state sentinel ahead of recall.  A
#      recalled memory line naming such an entity on a subject the kit
#      covers is withheld -- memory only fills what the kit lacks.
#   2. Supersession: recalled lines sharing (entity, subject) -> the newest
#      recorded line wins; an expectation whose explicit date has passed is
#      marked stale.
#   3. Date anchor: every dated recalled line carries today's date.
#
# Provider contract (the ONE interface a kit implements; the framework knows
# nothing about any kit schema):
#
#   module  : entity_facts.py, on the kit discovery ladder (_kit_dir_candidates)
#   function: get_entity_facts(prompt, today) -> list
#     prompt: the user turn text (str).  today: ISO date "YYYY-MM-DD", local.
#     Returns entities NAMED in this turn ([] when none).  Each item:
#       {"entity":  str,                 # display name
#        "aliases": [str, ...],          # optional, other spellings
#        "facts":   [{"text":   str,     # one-line fact, rendered verbatim
#                     "date":   str,     # optional ISO date the fact is about
#                     "as_of":  str,     # optional ISO date the kit recorded it
#                     "source": str,     # optional record reference
#                     "topics": [str]}]} # optional subject words
#   Runs on a daemon thread under ENTITY_FACTS_BUDGET_SEC; a raise, timeout or
#   malformed return drops the lane for the turn (logged), never the turn.
ENTITY_FACTS_MODULE = "entity_facts"
ENTITY_FACTS_FN = "get_entity_facts"
ENTITY_FACTS_BUDGET_SEC = 1.0
ENTITY_FACTS_MAX_ENTITIES = 5
ENTITY_FACTS_MAX_FACTS = 8
ENTITY_FACTS_NOTE = ("(kit record for entities named in this turn; it outranks "
                     "recalled memory on the same subject)")
# Subject groups + expectation markers live in a data file (instance-language
# words; code stays ASCII).  Missing/broken file -> no default groups; kit
# fact topics still work.
RECALL_SUBJECTS_PATH = os.path.join(HERE, "recall_subjects.json")

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_MD_SLASH_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_MD_KO_RE = re.compile("(?<!\\d)(\\d{1,2})\\s*\uc6d4\\s*(\\d{1,2})\\s*\uc77c")
# Trailing write stamp the engine appends to every stored line:
# "... (YYYY-MM-DD, source)".
_RECORDED_STAMP_RE = re.compile(r"\((\d{4}-\d{2}-\d{2}),[^()]*\)\s*$")
# A month/day this many days before the line's date is a recent past
# reference ("10/2 born", written 10/3) and keeps the line's year; anything
# earlier rolls to the next occurrence ("due 1/15", written 12/20).
MD_LOOKBACK_DAYS = 31
# "1 1/2": a whole number before the slash form makes it a fraction.
_MIXED_NUMBER_RE = re.compile(r"\d\s+$")
_FRACTION_TAIL_RE = re.compile(r"\s*([A-Za-z]+|[\uac00-\ud7a3]+)")

_recall_subjects_cache = None


def _utc_now():
    """Wall clock, UTC (seam for tests)."""
    return datetime.datetime.now(datetime.timezone.utc)


def _tz_hours_valid(raw):
    """Signed whole hours in [-12, 14] (routines/lib/tz_offset.sh rule), or
    None."""
    v = (raw or "").strip().strip("\"'")
    if not re.fullmatch(r"[+-]?\d{1,2}", v):
        return None
    n = int(v)
    return n if -12 <= n <= 14 else None


def _agent_tz_offset_hours():
    """The instance's UTC offset, resolved like routines/lib/tz_offset.sh:
    agent.conf AGENT_TZ_OFFSET_HOURS, then the crews-home crew.conf
    TZ_OFFSET_HOURS, then None (host).  Invalid values fall through."""
    v = _tz_hours_valid(_agent_conf_get("AGENT_TZ_OFFSET_HOURS"))
    if v is not None:
        return v
    crews = os.environ.get("DOGANY_CREW_DIR") or os.path.join(
        os.environ.get("DOGANY_SHARED_HOME")
        or os.path.join(os.path.expanduser("~"), ".dogany"), "crews")
    try:
        with open(os.path.join(crews, "crew.conf"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TZ_OFFSET_HOURS="):
                    return _tz_hours_valid(line.split("=", 1)[1])
    except OSError:
        pass
    return None


def _today_local():
    """Today in the agent's timezone (host zone when none is configured)."""
    hours = _agent_tz_offset_hours()
    now = _utc_now()
    if hours is None:
        return now.astimezone().date()
    return now.astimezone(datetime.timezone(datetime.timedelta(hours=hours))).date()


def _recall_subjects():
    """Recall lexicon from RECALL_SUBJECTS_PATH, cached, as a dict:
    groups = {name: [lowercase term, ...]}, markers = [expectation marker],
    entities = [(name, [lowercase term, ...])], fraction_units = [unit]."""
    global _recall_subjects_cache
    if _recall_subjects_cache is None:
        groups, markers, entities, units = {}, [], [], []
        try:
            with open(RECALL_SUBJECTS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            for name, terms in (data.get("groups") or {}).items():
                clean = [t.lower() for t in terms if isinstance(t, str) and t]
                if clean:
                    groups[str(name)] = clean
            markers = [m.lower() for m in data.get("expectation_markers") or []
                       if isinstance(m, str) and m]
            for name, aliases in (data.get("entities") or {}).items():
                if not isinstance(name, str) or not name.strip():
                    continue
                terms = []
                for t in [name] + [a for a in aliases or [] if isinstance(a, str)]:
                    t = t.strip().lower()
                    if t and t not in terms:
                        terms.append(t)
                entities.append((name.strip(), terms))
            units = [u.lower() for u in data.get("fraction_units") or []
                     if isinstance(u, str) and u]
        except Exception:
            pass
        _recall_subjects_cache = {"groups": groups, "markers": markers,
                                  "entities": entities, "fraction_units": units}
    return _recall_subjects_cache


def _safe_date(y, m, d):
    try:
        return datetime.date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _parse_iso_date(s):
    if not isinstance(s, str):
        return None
    m = _ISO_DATE_RE.search(s)
    return _safe_date(*m.groups()) if m else None


def _memory_recorded_date(text):
    """Date the engine stored the line (trailing write stamp), or None."""
    m = _RECORDED_STAMP_RE.search(text or "")
    return _parse_iso_date(m.group(1)) if m else None


def _memory_body(text):
    """Line text without its trailing write stamp."""
    return _RECORDED_STAMP_RE.sub("", text or "").rstrip()


def _is_fraction(text, m):
    """True when the slash form at match `m` reads as a fraction: a mixed
    number ("1 1/2") or a unit word right after it ("1/2 cup"): an ASCII
    word must equal a unit, other words may carry an attached particle."""
    if _MIXED_NUMBER_RE.search(text[:m.start()]):
        return True
    tail = _FRACTION_TAIL_RE.match(text, m.end())
    if not tail:
        return False
    word = tail.group(1).lower()
    units = _recall_subjects()["fraction_units"]
    if word.isascii():
        return word in units
    return any(word.startswith(u) for u in units if not u.isascii())


def _month_day(anchor, month, day):
    """Month/day as a date: the next occurrence on or after `anchor` (the
    line's date), unless it falls within MD_LOOKBACK_DAYS before it."""
    d = _safe_date(anchor.year, month, day)
    if d is None:
        return _safe_date(anchor.year + 1, month, day)  # Feb 29
    if d < anchor - datetime.timedelta(days=MD_LOOKBACK_DAYS):
        return _safe_date(anchor.year + 1, month, day) or d
    return d


def _memory_body_dates(body, anchor):
    """Explicit dates written in the line body.  Month/day forms resolve
    against `anchor` (the recorded date, else today); fractions are skipped."""
    out = [d for d in (_safe_date(*m.groups()) for m in _ISO_DATE_RE.finditer(body)) if d]
    rest = _ISO_DATE_RE.sub(" ", body)
    for rx in (_MD_SLASH_RE, _MD_KO_RE):
        for m in rx.finditer(rest):
            if rx is _MD_SLASH_RE and _is_fraction(rest, m):
                continue
            d = _month_day(anchor, m.group(1), m.group(2))
            if d:
                out.append(d)
    return out


def _entity_facts_provider_dir():
    for cand in _kit_dir_candidates():
        try:
            if os.path.isfile(os.path.join(cand, ENTITY_FACTS_MODULE + ".py")):
                return cand
        except Exception:
            continue
    return None


def _entity_facts_call(dirpath, prompt, today_iso):
    if dirpath not in sys.path:
        sys.path.insert(0, dirpath)
    import importlib
    mod = importlib.import_module(ENTITY_FACTS_MODULE)
    return getattr(mod, ENTITY_FACTS_FN)(prompt, today_iso)


def _entity_facts_normalize(raw):
    """Validate the provider return; malformed items are skipped one by one.
    Returns [{"entity", "terms", "facts": [{"text","date","as_of","source",
    "topics"}]}] with terms = lowercase entity name + aliases."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("entity")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        aliases = [a.strip() for a in item.get("aliases") or []
                   if isinstance(a, str) and a.strip()]
        facts = []
        for f in item.get("facts") or []:
            if not isinstance(f, dict):
                continue
            text = f.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            topics = [t.strip().lower() for t in f.get("topics") or []
                      if isinstance(t, str) and t.strip()]
            facts.append({
                "text": " ".join(text.split()),
                "date": f.get("date") if isinstance(f.get("date"), str) else None,
                "as_of": f.get("as_of") if isinstance(f.get("as_of"), str) else None,
                "source": f.get("source") if isinstance(f.get("source"), str) else None,
                "topics": topics,
            })
            if len(facts) >= ENTITY_FACTS_MAX_FACTS:
                break
        if not facts:
            continue
        terms = []
        for t in [name] + aliases:
            if t.lower() not in terms:
                terms.append(t.lower())
        out.append({"entity": name, "terms": terms, "facts": facts})
        if len(out) >= ENTITY_FACTS_MAX_ENTITIES:
            break
    return out


def _entity_facts_start(prompt):
    """Start the provider on a daemon thread so it overlaps the search lane.
    Returns a handle for _entity_facts_collect, or None (no provider / no
    prompt / machine traffic)."""
    try:
        if not prompt or _is_marker_noise_row(prompt):
            return None
        dirpath = _entity_facts_provider_dir()
        if dirpath is None:
            return None
        import threading
        box = {"t0": time.monotonic()}
        today_iso = _today_local().isoformat()

        def worker():
            try:
                box["r"] = _entity_facts_call(dirpath, prompt, today_iso)
            except BaseException as e:
                box["e"] = e

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        box["th"] = th
        return box
    except BaseException:
        return None


def _entity_facts_collect(handle, t0=None):
    """Join the provider thread within its budget (and the whole-hook soft
    deadline).  Returns normalized entities ([] on any failure)."""
    if not handle:
        return []
    try:
        left = ENTITY_FACTS_BUDGET_SEC - (time.monotonic() - handle["t0"])
        if t0 is not None:
            left = min(left, HOOK_TOTAL_SOFT_DEADLINE - HOOK_LEDGER_BUDGET_SEC
                       - (time.monotonic() - t0))
        handle["th"].join(max(left, 0))
        if handle["th"].is_alive():
            _ledger_log("DROP entity-facts: timeout")
            return []
        if "e" in handle:
            _ledger_log("DROP entity-facts: %s" % _ledger_exc_detail(handle["e"]))
            return []
        return _entity_facts_normalize(handle.get("r"))
    except BaseException as e:
        _ledger_log("DROP entity-facts: %s: %s" % (type(e).__name__, e))
        return []


def _entity_facts_render(entities):
    """Structured-state block for the named entities, or None."""
    if not entities:
        return None
    lines = []
    for ent in entities:
        for f in ent["facts"]:
            meta = [k + "=" + f[k] for k in ("date", "as_of", "source") if f.get(k)]
            tail = (" (" + ", ".join(meta) + ")") if meta else ""
            lines.append("%s %s: %s%s" % (LEDGER_SENTINEL, ent["entity"], f["text"], tail))
    lines.append("%s %s" % (LEDGER_SENTINEL, ENTITY_FACTS_NOTE))
    return "\n".join(lines)


def _term_in(term, low):
    """Entity term in a lowercase line: whole word for ASCII terms ("kim" is
    not in "kimchi"), substring otherwise (attached particles)."""
    if term.isascii():
        return re.search(r"(?<![0-9a-z])" + re.escape(term) + r"(?![0-9a-z])",
                         low) is not None
    return term in low


def _line_subjects(low, entity_terms, groups):
    """Subject keys (entity_key, group) a lowercase line speaks about."""
    ents = [k for k, terms in entity_terms if any(_term_in(t, low) for t in terms)]
    if not ents:
        return set()
    gs = [g for g, terms in groups.items() if any(t in low for t in terms)]
    return {(e, g) for e in ents for g in gs}


# ----------------------------------------------------------------------
# DGN-2009: persisted supersession. Consolidation prefixes an older memory
# line with SUPERSEDED_MARK when a later dated line on the same subject
# (same entity + same recall_subjects group, as in _recall_curate)
# contradicts it: the older line is an expectation ("due after the
# holiday") or the later line is a correction ("correction: ..."). Recall
# (search_core, the hook) never returns a marked line as a current fact.
# The mark is a text prefix only -- the line stays in the file, reversible.
# Explicit corrections also compare assertion subjects/predicates directly,
# so a missing domain group or entity-provider entry cannot block them.
# ----------------------------------------------------------------------
SUPERSEDED_MARK = "[SUPERSEDED]"
_SUPERSEDED_RE = re.compile(r"^\s*(?:[-*]\s+)?\[SUPERSEDED\]", re.M)
_MARK_PREFIX_RE = re.compile(r"^(\s*(?:[-*]\s+)?)(.*)$")


def is_superseded(text):
    """True when any line of a note carries the [SUPERSEDED] mark."""
    return bool(_SUPERSEDED_RE.search(text or ""))


def _supersession_entity_terms(texts):
    """Entity terms for supersession: the instance list in recall_subjects.json
    plus kit entities named anywhere in `texts` (one provider call, fail-open)."""
    terms = []
    handle = _entity_facts_start("\n".join(texts)) if texts else None
    for e in _entity_facts_collect(handle):
        terms.append(("kit:" + e["entity"], e["terms"]))
    for name, ts in _recall_subjects()["entities"]:
        extra = [t for t in ts if not any(t in have for _, have in terms)]
        if extra:
            terms.append(("list:" + name, extra))
    return terms


def _correction_claim(body):
    """Extract a subject/predicate/value without a domain topic lexicon.

    Only explicit assertion shapes are comparable. A different predicate
    (e.g. lives in vs works at) cannot retire an unrelated fact. Unknown
    shapes stay current rather than guessing from entity overlap alone.
    """
    body = body.split(' -- owner: "', 1)[0].strip().casefold()
    for marker in CORRECTION_MARKERS:
        if body.startswith(marker):
            body = body[len(marker):].strip()
            break
    body = re.sub(r"\s+", " ", body).rstrip(".! ")
    match = re.fullmatch(
        r"(.+?)\s+(is(?: an?| the)?|are|was(?: an?| the)?|were|"
        r"has|have|lives? in|works? at|prefers?|likes?)\s+(.+)", body)
    if match:
        return (match[1], match[2]), match[3]
    match = re.fullmatch(r"(.+?)\s*[:=]\s*(.+)", body)
    if match:
        return (match[1].strip(), "value"), match[2]
    # Korean topic + copula, including attached topic particles.
    match = re.fullmatch(
        r"(.+?)(?:\uc740|\ub294)\s+(.+?)(?:\uc785\ub2c8\ub2e4|\uc774\uc5d0\uc694|"
        r"\uc774\uc57c|\uc774\ub2e4|\uc608\uc694|\uc57c)", body)
    if match:
        return (match[1], "copula"), match[2]
    return None


def find_superseded(lines, entity_terms=None):
    """lines: [(key, text)] of stamped memory lines.  Returns {old_key: new_key}
    for every line a later same-subject line contradicts.  Undated or already
    marked lines neither supersede nor get superseded. Same-day corrections
    use append order within a file; cross-file date ties remain ambiguous."""
    lex = _recall_subjects()
    groups, markers = lex["groups"], lex["markers"]
    if entity_terms is None:
        entity_terms = _supersession_entity_terms([t for _, t in lines])
    infos = []
    for position, (key, text) in enumerate(lines):
        rec = _memory_recorded_date(text)
        if rec is None or is_superseded(text):
            continue
        body = _memory_body(_MARK_PREFIX_RE.match(text).group(2))
        low = body.lower()
        subj = _line_subjects(low, entity_terms, groups)
        claim = _correction_claim(body)
        if subj or claim:
            # Modern provenance must be owner-stated. Legacy source stamps
            # remain supported, but agent guesses cannot retire owner facts.
            owner = not re.search(r"\bby=(?:agent-judged|unverified)\b", text)
            infos.append({"key": key, "rec": rec, "subj": subj, "claim": claim,
                          "position": position,
                          "expect": any(m in low for m in markers),
                          "correct": owner and _is_correction(body)})
    out = {}
    for old in infos:
        later = []
        for new in infos:
            # Within one file, append order also orders same-day corrections.
            # Different files with only a date have no trustworthy tie order.
            same_file = (not isinstance(old["key"], tuple)
                         or old["key"][0] == new["key"][0])
            newer = (new["rec"] > old["rec"] or
                     (new["rec"] == old["rec"] and same_file and new["correct"]
                      and new["position"] > old["position"]))
            contradicts = (old["claim"] and new["claim"] and new["correct"]
                           and old["claim"][0] == new["claim"][0]
                           and old["claim"][1] != new["claim"][1])
            if newer and (contradicts or (old["subj"] & new["subj"]
                                         and (old["expect"] or new["correct"]))):
                later.append(new)
        if later:
            out[old["key"]] = max(later, key=lambda n: (n["rec"], n["position"]))["key"]
    return out


def mark_superseded(memories_dir=None, dry_run=False):
    """Scan memories/*.md, prefix contradicted lines with SUPERSEDED_MARK
    (backup first).  Returns [(file, old_line, newer_line)]."""
    memories_dir = memories_dir or MEMORIES_DIR
    try:
        names = sorted(f for f in os.listdir(memories_dir)
                       if f.endswith(".md") and f not in INDEX_EXCLUDE)
    except OSError:
        return []
    files, lines = {}, []
    for name in names:
        try:
            with open(os.path.join(memories_dir, name), encoding="utf-8") as fh:
                files[name] = fh.read().split("\n")
        except OSError:
            continue
        for i, ln in enumerate(files[name]):
            if _RECORDED_STAMP_RE.search(ln.strip()):
                lines.append(((name, i), ln.strip()))
    hits = find_superseded(lines)
    if not hits:
        return []
    out = []
    for (name, i), (nname, ni) in sorted(hits.items()):
        out.append((name, files[name][i].strip(), files[nname][ni].strip()))
    if dry_run:
        return out
    for name in sorted({k[0] for k in hits}):
        path = os.path.join(memories_dir, name)
        content = files[name]
        for (fname, i) in hits:
            if fname == name:
                m = _MARK_PREFIX_RE.match(content[i])
                content[i] = m.group(1) + SUPERSEDED_MARK + " " + m.group(2)
        try:
            import shutil
            shutil.copy(path, path + ".bak-superseded." +
                        datetime.date.today().strftime("%Y%m%d"))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(content))
        except OSError as e:
            print(f"[warn] superseded mark not written to {name}: {e}", file=sys.stderr)
            out = [o for o in out if o[0] != name]
    return out


def cmd_mark_superseded(args):
    marked = mark_superseded(dry_run=args.dry_run)
    for name, old, new in marked:
        print(f"{SUPERSEDED_MARK} {name}: {old}\n    by: {new}")
    print(f"[mark-superseded] {len(marked)} line(s)"
          + (" (dry-run, nothing written)" if args.dry_run else " marked"))
    if marked and not args.dry_run and not _reindex_locked_with_retry():
        print("[mark-superseded] re-index deferred (database busy).", file=sys.stderr)
    return 0


def _recall_curate(items, entities=None, prompt=None, today=None):
    """Apply kit-first / supersession / stale-expectation / date-anchor to
    recalled items [{"section", "text"}, ...] (relevance order kept).
    Returns (rendered_lines, n_withheld)."""
    today = today or _today_local()
    entities = entities or []
    lex = _recall_subjects()
    groups, markers = lex["groups"], lex["markers"]

    # Entity terms: kit entities first (keyed by name), then the instance
    # entity list in recall_subjects.json.  Prompt words are never entities.
    entity_terms = [("kit:" + e["entity"], e["terms"]) for e in entities]
    for name, terms in lex["entities"]:
        extra = [t for t in terms if not any(t in ts for _, ts in entity_terms)]
        if extra:
            entity_terms.append(("list:" + name, extra))

    # Subjects the kit covers: explicit fact topics + default groups the
    # fact text itself falls into.
    covered = set()
    kit_groups = dict(groups)
    for e in entities:
        key = "kit:" + e["entity"]
        for f in e["facts"]:
            if f["topics"]:
                g = "topics:" + e["entity"]
                kit_groups.setdefault(g, [])
                kit_groups[g] += [t for t in f["topics"] if t not in kit_groups[g]]
                covered.add((key, g))
            flow = f["text"].lower()
            covered |= {(key, g) for g, terms in groups.items()
                        if any(t in flow for t in terms)}

    infos = []
    for it in items:
        text = (it.get("text") or "").strip().replace("\n", " ")
        low = text.lower()
        rec = _memory_recorded_date(text)
        subj = _line_subjects(low, entity_terms, kit_groups)
        infos.append({"it": it, "text": text, "rec": rec, "subj": subj,
                      "drop": bool(subj & covered) or is_superseded(text)})

    # Supersession: per subject, the newest recorded line wins.  Undated
    # lines never supersede and are never superseded (no order to trust).
    newest = {}
    for inf in infos:
        if inf["drop"] or inf["rec"] is None:
            continue
        for s in inf["subj"]:
            if s not in newest or inf["rec"] > newest[s]:
                newest[s] = inf["rec"]
    for inf in infos:
        if not inf["drop"] and inf["rec"] is not None and any(
                newest.get(s, inf["rec"]) > inf["rec"] for s in inf["subj"]):
            inf["drop"] = True

    lines = []
    for inf in infos:
        if inf["drop"]:
            continue
        section = inf["it"].get("section") or "(no section)"
        text = inf["text"]
        if len(text) > HOOK_SNIPPET_MAX:
            text = text[:HOOK_SNIPPET_MAX] + "…"
        rec = inf["rec"]
        body = _memory_body(inf["text"])
        dates = _memory_body_dates(body, rec or today)
        notes = []
        if any(m in body.lower() for m in markers):
            # Judged by the latest date: "10-01 -> 10-20" is not yet due.
            if dates and max(dates) < today:
                notes.append("STALE: expected date %s has passed -- check what "
                             "actually happened" % max(dates).isoformat())
            elif rec is not None and not dates:
                notes.append("expectation recorded %d days ago -- it may "
                             "already have happened" % (today - rec).days)
        if rec is not None or dates:
            notes.append(("recorded %s, " % rec.isoformat() if rec else "")
                         + "today %s" % today.isoformat())
        tail = (" [" + "; ".join(notes) + "]") if notes else ""
        lines.append(f"- [{section}] {text}{tail}")
    return lines, sum(1 for inf in infos if inf["drop"])


# ----------------------------------------------------------------------
# DGN-2028: per-turn recall mirrors the DGN-2010 destinations (read side).
# Behind RECALL_ROUTES (agent.conf, or DOGANY_RECALL_ROUTES), default OFF:
# with it off nothing below runs. With it on, a recalled memory line is
# injected only when memory is its home:
#   machine-traffic turn (status notices)       -> no note recall
#   already covered by a session-loaded file    -> left to that file
#   (owner file, rules, a skill)
#   procedure with no loaded home               -> kept (memory is its home)
#   names a ticket that has a ticket file       -> left to the ticket, unless
#                                                  the prompt names that ticket
#   memory                                      -> relevance floor, deduped
# Kit records are already first (DGN-1962). docs/design/DGN-2028-recall-routes.md
# has the measurement (tests/dgn2028_recall_replay.py).
# ----------------------------------------------------------------------
RECALL_ROUTES_KEY = "RECALL_ROUTES"
# A machine envelope the bridge prefixes to an injected turn; the marker that
# decides what the turn is comes after it.
RECALL_ENVELOPE_RE = re.compile(r"^\s*(?:\[bridge:injected-turn\]\s*)+")
# Status notices: the turn reports a finished job; memory has nothing to add.
# Handoff/cron prompts carry a task and still get (filtered) recall.
RECALL_MACHINE_MARKERS = ("<task-notification", "[dispatch-return]")
# Relevance floor: shared prompt features (_same_day_features), an ASCII
# token counting 2 and a Korean bigram 1. Cosine is not the floor: on the
# replay, machine prompts scored higher against unrelated lines than owner
# questions against the lines they needed.
# Staleness stays with _recall_curate (superseded lines, passed expectations):
# an age penalty on wording cost a needed line on the replay.
RECALL_ANCHOR_MIN = 2
RECALL_HOT_COVER = 0.7          # share of a line's features inside one paragraph
RECALL_DUP_SIM = 0.8            # feature Jaccard: two recalled lines say the same
RECALL_HOT_FILES = ("RULES.md", "USER.md", "rules/hot.*.md", "identity/hot.*.md",
                    ".claude/skills/*/SKILL.md")
RECALL_TICKET_DIR = "worklog"
RECALL_TICKET_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?!\d)")
RECALL_PROCEDURE_RE = re.compile(
    "\uaddc\uce59|\ubc18\ub4dc\uc2dc|\ud574\uc57c\\s*(?:\ud55c\ub2e4|\ud568|\ud558\uba70)"
    "|\uae08\uc9c0|\ud558\uc9c0\\s*\ub9d0|\ub9d0\\s*\uac83"
    r"|(?i:\b(?:must|never|always|rule)\b)")
_RECALL_SPLIT_RE = re.compile(r"\n\s*\n")


def _recall_routes_enabled():
    raw = (os.environ.get("DOGANY_RECALL_ROUTES")
           or _agent_conf_get(RECALL_ROUTES_KEY) or "").strip().lower()
    return raw in ("1", "on", "true", "yes")


def _recall_agent_root():
    return os.path.normpath(os.path.join(HERE, ".."))


def _recall_prompt_body(prompt):
    """Prompt without the bridge envelope."""
    return RECALL_ENVELOPE_RE.sub("", prompt or "")


def _recall_is_machine(prompt):
    """True for a status-notice turn (envelope-aware)."""
    return _recall_prompt_body(prompt).lstrip().startswith(RECALL_MACHINE_MARKERS)


def _recall_features(text):
    return _same_day_features(_memory_body(text or ""))


def _recall_anchor(prompt_feats, text):
    """Weighted count of prompt features found in the line."""
    hay = _compact(_memory_body(text))
    return sum(1 if re.match("[\uac00-\ud7a3]", f) else 2
               for f in prompt_feats if f in hay)


def _recall_hot_paragraphs(root):
    """Compacted paragraphs of the session-loaded surfaces."""
    import glob
    out = []
    for pat in RECALL_HOT_FILES:
        for path in sorted(glob.glob(os.path.join(root, pat))):
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            out += [c for c in map(_compact, _RECALL_SPLIT_RE.split(text)) if c]
    return out


def _recall_hot_covered(feats, hot):
    """True when one paragraph holds RECALL_HOT_COVER of the line's features."""
    need = RECALL_HOT_COVER * len(feats)
    return bool(feats) and any(sum(1 for f in feats if f in para) >= need
                               for para in hot)


def _recall_ticket_ids(ticket_dir):
    """Ticket ids that have a file in ticket_dir ("<ID>.md" / "<ID>-*.md")."""
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return set()
    out = set()
    for name in names:
        m = RECALL_TICKET_ID_RE.match(name)
        if m and name.endswith(".md") and name[m.end()] in "-.":
            out.add(m.group(1))
    return out


def _recall_route(prompt, items, root=None, ownership=None):
    """Route recalled items [{"section","text",...}] (relevance order kept).
    Returns (kept_items, decisions) with one decision per input item:
    {"route": machine|home|unresolved|hot|ticket|floor|dup|memory|procedure,
     "why": str}. Unresolved evidence is retained with ownership metadata
    for separate rendering; only memory/procedure are admitted as recall."""
    root = root or _recall_agent_root()
    if _recall_is_machine(prompt):
        return [], [{"route": "machine", "why": "status-notice turn"} for _ in items]
    body = _recall_prompt_body(prompt)
    pfeats = _same_day_features(body)
    prompt_ids = set(RECALL_TICKET_ID_RE.findall(body))
    state = ownership if ownership is not None else _TURN_SOURCES.get()
    hot = None
    tickets = _recall_ticket_ids(os.path.join(root, RECALL_TICKET_DIR))
    kept, decisions, kept_feats = [], [], []
    for it in items:
        text = (it.get("text") or "").strip()
        feats = _recall_features(text)
        dec = None
        home = None
        if state is not None:
            bc, resolved, _ = state
            home = bc.memory_home(it, resolved)
        if home:
            dec = {"route": "home" if home["status"] == "loaded" else "unresolved",
                   "why": home["status"] + ": " + ",".join(home["refs"])}
            if dec["route"] == "unresolved":
                # Unavailable is not absent. Preserve the dated evidence
                # separately; never let it impersonate the missing source.
                kept.append(dict(it, _home_unresolved=home))
        if dec is None:
            # Preserve existing loaded-file coverage for unclassified facts.
            # Authoritative homes above never depend on this text heuristic.
            if hot is None:
                hot = _recall_hot_paragraphs(root)
            if _recall_hot_covered(feats, hot):
                dec = {"route": "hot", "why": "covered by a session-loaded file"}
        procedure = bool(RECALL_PROCEDURE_RE.search(_memory_body(text)))
        if dec is None and not procedure:
            homes = [t for t in dict.fromkeys(RECALL_TICKET_ID_RE.findall(text))
                     if t in tickets]
            if homes and not prompt_ids & set(homes):
                dec = {"route": "ticket", "why": "home: " + ",".join(homes)}
        if dec is None:
            score = _recall_anchor(pfeats, text)
            if score < RECALL_ANCHOR_MIN:
                dec = {"route": "floor", "why": "anchor %d" % score}
        if dec is None and any(
                feats and len(feats & k) >= RECALL_DUP_SIM * len(feats | k)
                for k in kept_feats):
            dec = {"route": "dup", "why": "same as a kept line"}
        if dec is None:
            dec = {"route": "procedure" if procedure else "memory", "why": ""}
            kept.append(it)
            kept_feats.append(feats)
        decisions.append(dec)
    return kept, decisions


def _recall_render(tag, items, entities=None, prompt=None):
    """Header + frame + curated lines, or None when nothing survives."""
    if prompt and _recall_routes_enabled():
        try:
            items, _ = _recall_route(prompt, items)
        except Exception as e:  # fail-open: unrouted recall, never no turn
            _ledger_log("DROP recall-routes: %s: %s" % (type(e).__name__, e))
    unresolved = [it for it in items if it.get("_home_unresolved")]
    items = [it for it in items if not it.get("_home_unresolved")]
    lines, withheld = _recall_curate(items, entities, prompt)
    evidence, evidence_withheld = _recall_curate(unresolved, entities, prompt) if unresolved else ([], 0)
    withheld += evidence_withheld
    if not lines and not evidence:
        return None
    n_expl = 0
    if _turn_frame_enabled():
        # DGN-2028: explanation label (slot token or uncertain causal cue),
        # labelled lines sorted last, plus one rule line in the frame.
        tf = _turn_frame_lib()
        if tf is not None:
            try:
                lines, n_expl = tf.label_lines(lines, lines)
            except Exception as e:  # fail-open: unlabelled recall
                _ledger_log("DROP turn-frame label: %s" % type(e).__name__)
    out = _recall_header(tag) + lines
    if n_expl:
        out.insert(len(_recall_header(tag)), f"{RECALL_FRAME_MARKER} {tf.EXPLANATION_RULE}")
    if withheld:
        out.append(f"{RECALL_FRAME_MARKER} {withheld} recalled line(s) withheld: "
                   "superseded by the kit record or by a newer memory line.")
    if evidence:
        refs = sorted({ref for it in unresolved for ref in it["_home_unresolved"]["refs"]})
        out.append(f"{RECALL_FRAME_MARKER} unresolved home evidence (dated, not authority): "
                   + ",".join(refs))
        out.extend(evidence)
    return "\n".join(out)


def _hook_build_context(results, entities=None, prompt=None):
    """Build the additionalContext string to inject. Returns None for weak matches.
    Cut: exclude results where cosine < HOOK_VEC_CUTOFF. cosine=None (FTS-only match) is also excluded (conservative).
    Surviving items go through _recall_curate (DGN-1962)."""
    if not results:
        return None
    top = results[0].get("cosine")
    if top is None or top < HOOK_VEC_CUTOFF:
        return None
    items = [r for r in results
             if r.get("cosine") is not None and r.get("cosine") >= HOOK_VEC_CUTOFF]
    return _recall_render("\uc790\ub3d9\uac80\uc0c9", items, entities, prompt)


def _has_vector_hit(results):
    """Did the vector lane return anything? True if at least one result has a cosine value.
    If Ollama is down/not installed, vector_search returns [] -> all cosine=None -> False."""
    if not results:
        return False
    return any(r.get("cosine") is not None for r in results)


def _tokenize_for_fts(prompt):
    """Tokenize a prompt into terms for keyword FTS fallback.
    - Keep only ASCII/digit/Korean words; strip punctuation.
    - Drop tokens shorter than HOOK_KW_MIN_LEN and stopwords.
    - Preserve insertion order, deduplicate, return at most HOOK_KW_MAX_TERMS terms.
    Returns: [term, ...] (may be empty)."""
    if not prompt:
        return []
    # Tokenize runs of Korean/ASCII/digit only. (trigram FTS catches partial matches too)
    raw = re.findall(r"[0-9A-Za-z가-힣]+", prompt)
    seen = set()
    terms = []
    for w in raw:
        lw = w.lower()
        if len(w) < HOOK_KW_MIN_LEN:
            continue
        if lw in HOOK_STOPWORDS:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        terms.append(w)
        if len(terms) >= HOOK_KW_MAX_TERMS:
            break
    return terms


def _fts_or_match(conn, terms, limit):
    """OR-combine keyword terms and MATCH against notes_fts. Returns note id list ordered by bm25 ascending.
    Each term is wrapped as a phrase for safe OR combination (trigram tokenizer)."""
    if not terms:
        return []
    expr = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        rows = conn.execute(
            "SELECT rowid, bm25(notes_fts) AS rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def _hook_keyword_fallback(prompt, entities=None):
    """M4 fallback: when the vector lane returns nothing (embedding failure / all None), recall via keyword FTS.
    Tokenize the prompt, OR-MATCH, and inject the top hit bodies without a cosine cutoff.
    Returns: additionalContext string or None (no hits)."""
    terms = _tokenize_for_fts(prompt)
    if not terms:
        return None
    try:
        conn = connect()
        init_db(conn)
    except sqlite3.Error:
        return None
    try:
        ids = _fts_or_match(conn, terms, HOOK_KW_TOPK)
        if not ids:
            return None
        items = []
        for nid in ids:
            row = conn.execute(
                "SELECT section, text FROM notes WHERE id=?", (nid,)
            ).fetchone()
            if row:
                items.append({"section": row["section"], "text": row["text"]})
        return _recall_render("\ud0a4\uc6cc\ub4dc\uac80\uc0c9", items, entities, prompt)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def null_embedding_fraction():
    """Return the fraction of notes where embedding IS NULL (0.0-1.0) along with (null_count, total).
    Returns (0.0, 0, 0) if there are no notes. Used by stats/consolidate to emit 'semantic search degraded' warnings."""
    try:
        conn = connect()
        init_db(conn)
    except sqlite3.Error:
        return 0.0, 0, 0
    try:
        total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        nulls = conn.execute(
            "SELECT COUNT(*) c FROM notes WHERE embedding IS NULL"
        ).fetchone()["c"]
    except sqlite3.Error:
        return 0.0, 0, 0
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    frac = (nulls / total) if total else 0.0
    return frac, nulls, total


# DGN-2173: hook-only admission. Search/storage and explicit recall stay intact.
# This rebuildable cache stores hashes, never conversation or memory text.
INJECTION_DB_PATH = os.path.join(HERE, ".injections.db")
HOOK_RELEVANCE_MIN = 1  # preserves the short Korean status question in the replay


def _injection_machine(prompt):
    raw = (prompt or "").lstrip()
    return (raw.startswith("[bridge:injected-turn]")
            or _recall_prompt_body(raw).lstrip().startswith(
                ("[cron-inject]", "cron-inject", "<task-notification",
                 "[dispatch-return]", "dispatch-return"))
            or _is_marker_noise_row(raw))


# FDGN-637: one durable previous OWNER turn, independent of retrieval and SDK
# transcript lifetime. This is continuity, not a long-term fact classifier.
# Keep it outside .injections.db: that hash cache is disposable and suppresses
# repeats, whereas this carrier must appear again after resume/compaction.
def _hook_previous_owner_turn(prompt, session_id, raw_prompt=None, agent_id=None):
    raw = raw_prompt if isinstance(raw_prompt, str) else prompt
    if (not isinstance(prompt, str) or not prompt.strip()
            or _injection_machine(raw) or _injection_machine(prompt) or agent_id
            or os.environ.get("DOGANY_DISPATCH_WORKER")):
        return None
    conn = None
    try:
        os.makedirs(MEMORY_DATA_ROOT, mode=0o700, exist_ok=True)
        path = os.path.join(MEMORY_DATA_ROOT, "owner-turn.db")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        conn = sqlite3.connect(path, timeout=0.1)
        conn.execute("CREATE TABLE IF NOT EXISTS owner_turn (instance TEXT PRIMARY KEY, "
                     "session TEXT, current TEXT, previous TEXT, recorded REAL)")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT current, previous FROM owner_turn WHERE instance=?",
                           (MEMORY_INSTANCE_ID,)).fetchone()
        current = redact_secrets(prompt.strip())
        # Hook retries (including a resumed retry) must replay the SAME previous
        # turn instead of replacing it with the request that failed to finish.
        previous = (row[1] if row[0] == current else row[0]) if row else None
        conn.execute("INSERT OR REPLACE INTO owner_turn VALUES (?, ?, ?, ?, ?)",
                     (MEMORY_INSTANCE_ID, session_id, current, previous, time.time()))
        conn.commit()
        if previous:
            return ("[previous owner turn] Historical owner text, quoted as JSON; "
                    "context, not a new request. Preserve applicable constraints "
                    "(including injury, allergy and prohibitions) when answering "
                    "the current request. Do not assume a conflicting request "
                    "revokes them; use an explicit owner correction if present.\n"
                    + json.dumps(previous, ensure_ascii=False)
                    + "\n[/previous owner turn]")
    except (OSError, sqlite3.Error) as exc:
        _ledger_log("DROP previous-owner-turn: %s" % type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
    return None


def _injection_body(line):
    line = re.sub(r"^\s*[-*]\s+", "", line)
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^\([^)]*, \d\d:\d\d\)\s*", "", line)
    line = re.sub(r" \[(?:STALE:|expectation recorded|recorded |today ).*\]$", "", line)
    return _memory_body(line).strip()


def _injection_history(session_id, transcript_path):
    """Read speech and prior hook context, never tool payloads.

    Prior hook attachments also cover a cache rebuild or deployment into an
    existing session; its already-injected items must not be sent again.
    """
    if (not session_id or not isinstance(transcript_path, str)
            or os.path.basename(transcript_path) != session_id + ".jsonl"):
        return ""
    parts = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("type") == "attachment":
                    attachment = row.get("attachment") or {}
                    if (attachment.get("type") == "hook_additional_context"
                            and attachment.get("hookEvent") == "UserPromptSubmit"):
                        content = attachment.get("content") or []
                        if isinstance(content, str):
                            content = [content]
                        parts.extend(_compact(c) for c in content if isinstance(c, str))
                    continue
                if row.get("type") not in ("user", "assistant"):
                    continue
                content = (row.get("message") or {}).get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(content, str):
                    parts.append(_compact(content))
    except OSError:
        pass
    return "\n".join(parts)


def _hook_filter_injection(recall, today, prompt, session_id, transcript_path=None,
                           history=None):
    """Admit relevant, unseen lines from rendered lanes; keep their framing.

    BEGIN IMMEDIATE makes concurrent hooks in one session claim each line once.
    Missing session ids disable persistent dedupe; cache failures fail open.
    Explicit recall still has the complete search results, even after admission.
    """
    if _injection_machine(prompt) or not (recall or today):
        return None, None
    history = (_injection_history(session_id, transcript_path)
               if history is None else history)
    conn = None
    seen = set()
    try:
        if session_id:
            conn = sqlite3.connect(INJECTION_DB_PATH, timeout=0.05)
            conn.execute("CREATE TABLE IF NOT EXISTS emitted (session TEXT, digest TEXT, "
                         "ts REAL, PRIMARY KEY(session, digest))")
            conn.execute("BEGIN IMMEDIATE")
            seen = {r[0] for r in conn.execute(
                "SELECT digest FROM emitted WHERE session=?", (session_id,))}
    except sqlite3.Error:
        if conn:
            conn.close()
        conn = None
    feats = _same_day_features(prompt)
    claimed = set()

    def admit(line, related=False):
        body = _injection_body(line)
        if related and _recall_anchor(feats, body) < HOOK_RELEVANCE_MIN:
            return False
        compact = _compact(body)
        digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        if (not compact or digest in seen or digest in claimed
                or (len(compact) >= 12 and compact in history)):
            return False
        claimed.add(digest)
        return True

    try:
        if recall:
            # A note may have continuation lines. Keep/drop the whole rendered
            # item so a rejected note cannot leak its tail into the frame.
            header, items = [], []
            for line in recall.splitlines():
                if line.startswith("- ["):
                    items.append(line)
                elif line.startswith(RECALL_FRAME_MARKER) or not items:
                    header.append(line)
                else:
                    items[-1] += "\n" + line
            kept = [item for item in items if admit(item, True)]
            recall = "\n".join(header + kept) if kept else None
        if today:
            blocks = []
            # Split on lane headers, not blank paragraphs inside an answer:
            # a new second paragraph must retain its time/reference framing.
            for block in re.split(r"(?m)(?=^\[(?:\uC624\uB298 \uB2E4\uB978 \uC138\uC158|same-day answer))", today):
                lines = block.strip().splitlines()
                if not lines:
                    continue

                def frame(ln):
                    return (ln == lines[0] and ln.startswith("[")
                            or ln.startswith(("(This answer", "- user asked:",
                                              "- answer (", "- other same-topic"))
                            or not ln.strip())

                kept = [ln for ln in lines if not frame(ln) and admit(ln)]
                if kept:
                    blocks.append("\n".join(ln for ln in lines if frame(ln) or ln in kept))
            today = "\n\n".join(blocks) or None
        if conn:
            conn.executemany("INSERT OR IGNORE INTO emitted VALUES (?, ?, ?)",
                             [(session_id, d, time.time()) for d in claimed])
            conn.commit()
    except sqlite3.Error:
        pass  # Admission still works without persistence on a read-only/full disk.
    finally:
        if conn:
            conn.close()
    return recall, today


def cmd_hook(args):
    """UserPromptSubmit hook. Calls exit(0) directly (does not depend on main's rc).
    Any error exits 0 without writing anything to stdout (empty fallback)."""
    source_token = _TURN_SOURCES.set(None)
    try:
        # t0: elapsed-time reference for the ledger lane budget (DGN-1176
        # spec 5.5 -- time.monotonic; pre-interpreter cost is absorbed by the
        # 1.5s gap between the 10.5s soft cap and the 12s harness kill).
        t0 = time.monotonic()
        # 1) parse stdin JSON -> extract prompt + session_id
        try:
            raw = sys.stdin.read()
            data = json.loads(raw)
            prompt = data.get("prompt")
            raw_prompt = prompt
            session_id = data.get("session_id")
        except Exception:
            # Parse failure: prompt=None -> always entries only.  The ledger
            # lane (and its frozen body-state fallback) keeps the old
            # "unconditional every turn" guarantee the compose-internal call
            # used to give on this path.
            _emit_empty(_hook_ledger_preload(None, t0))
            return
        # DGN-1967 bug 2: recall, the kit provider and the today lane search
        # on the owner's own words, never on the pushes the bridge prefixed.
        if isinstance(prompt, str):
            prompt = _strip_outbound_block(prompt)
        if not isinstance(prompt, str) or not prompt.strip():
            _emit_empty(_hook_ledger_preload(None, t0))
            return
        if not isinstance(session_id, str):
            session_id = None  # None-safe: §4 echo belt covers self-echo

        # Record before optional retrieval; never make continuity depend on
        # lexical overlap, TURN_FRAME, consolidation, or the session transcript.
        previous_owner_ctx = _hook_previous_owner_turn(
            prompt, session_id, raw_prompt, data.get("agent_id"))

        # 1.5) trigger auto-reindex (best effort, fire-and-forget).
        try:
            _hook_maybe_reindex()
        except Exception:
            pass

        # 2) rebind global embed to the short-timeout embedding function for hook path.
        global embed
        embed = _short_embed

        # 2b) DGN-1962 entity facts lane: started before the search so the
        #     kit provider overlaps it; collected right after.
        machine = _injection_machine(prompt)
        source_state = _hook_source_context(prompt.strip(), t0) if not machine else None
        _TURN_SOURCES.set(source_state)
        ent_handle = _entity_facts_start(prompt.strip())

        # 3) search (thread timeout guard)
        results = None if machine else _hook_search_with_timeout(prompt.strip())
        entities = _entity_facts_collect(ent_handle, t0)

        # 4) build context + weak-match cutoff (recall curated against the
        #    kit entities: kit first, newer wins, stale expectations marked).
        ctx = (_hook_build_context(results, entities, prompt.strip())
               if results else None)

        # 4b) M4 fallback: if the vector lane is empty (Ollama down/absent -> all cosine=None,
        #     whole-prompt phrase matching also mostly fails), fall back to keyword FTS recall.
        #     In the default product deployment without Ollama, skipping this path would make
        #     auto-recall effectively a no-op (time line only). Inject keyword hits without cosine cutoff.
        if not machine and not ctx and not _has_vector_hit(results):
            ctx = _hook_keyword_fallback(prompt.strip(), entities)

        # 4c) same-day cross-session lane (F2, dec-089 policy1, default ON).
        #     MAJOR2 gate: skip the today lane entirely when the prompt itself is
        #     machine traffic (cron inject / loop prompt / task notification).
        #     DGN-2173: the envelope-aware gate applies to both memory lanes.
        today_ctx = None
        if not machine:
            today_ctx = _hook_today_recall(prompt.strip(), session_id)
            # DGN-1949: a back-reference prompt ("again" / "earlier", Korean included) also gets
            # the best same-day ANSWER (question + answer, timed, bounded).
            if is_back_reference(prompt):
                answer_ctx = _hook_same_day_answer(prompt.strip(), session_id)
                if answer_ctx:
                    today_ctx = "\n\n".join(c for c in (answer_ctx, today_ctx) if c)

        # 4d) DGN-1176 ledger lane.  Computation point is LOCKED (spec v2.1
        #     5.2-(2)/MJ-A): right after the today lane, right before the
        #     empty-branch/compose.  Computing it earlier would pre-spend
        #     today's 12s-kill headroom and worsen the kill condition from
        #     T_today>3s to T>1.5s.  Render order (time -> ledger -> recall
        #     -> today) is a separate, unchanged fact of _hook_compose.
        ledger_ctx = _hook_ledger_preload(prompt.strip(), t0)
        # DGN-1962: entity facts ride the structured-state slot, after the
        # ledger block and before recall (kit facts first).
        entity_ctx = _entity_facts_render(entities)
        if entity_ctx:
            ledger_ctx = (ledger_ctx + "\n" + entity_ctx) if ledger_ctx else entity_ctx

        # 4e) DGN-2028 turn frame: computed after the locked ledger lane (it
        #     must not move that lane earlier), rendered right after the
        #     time line, in both branches below.
        frame_ctx = _hook_turn_frame(prompt.strip(), session_id, t0, raw_prompt,
                                    source_state[1] if source_state else None)
        if source_state:
            # One block in the existing memory hook, before either recall
            # branch. Keep sources even when frame recording times out.
            frame_ctx = "\n".join(c for c in (frame_ctx, source_state[2]) if c)

        # Continuity intentionally bypasses recall relevance and already-seen
        # admission: the prior text may have disappeared in a compaction.
        ctx, today_ctx = _hook_filter_injection(
            ctx, today_ctx, prompt.strip(), session_id, data.get("transcript_path"))

        if not ctx and not today_ctx:
            _emit_empty(ledger_ctx, frame_ctx, previous_owner_ctx)
            return

        # 5) emit JSON injection (stdout, once). Order: time + ledger(old body-state slot) + recall + today.
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(ctx, today_ctx, ledger_ctx, frame_ctx, previous_owner_ctx),
            }
        }
        print(json.dumps(out, ensure_ascii=False), flush=True)
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException:
        # last-resort catch-all: never block the turn no matter what.
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
    finally:
        _TURN_SOURCES.reset(source_token)


# ======================================================================
# CLI
# ======================================================================
def _active_item_bytes(item_id):
    """Locate an explicitly identified active item without ordinal inference."""
    marker = ("item-id: " + item_id).encode("ascii")
    for base, dirs, files in os.walk(MEMORIES_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            if not name.endswith(".md") or ".bak" in name:
                continue
            path = os.path.join(base, name)
            raw = open(path, "rb").read()
            lines = raw.splitlines(keepends=True)
            for pos, line in enumerate(lines):
                if marker in line:
                    return path, pos, line, raw, lines
    raise ArchiveError("active item id not found: %s" % item_id)


def _ui_list_path():
    return os.path.join(MEMORY_DATA_ROOT, "ui", "last-list.json")


def _resolve_numbers(args):
    """Owner numbers -> stable ids through the last list shown (design 10.1).
    A list older than the live state is refused rather than guessed."""
    ids = list(args.ids or [])
    numbers = list(getattr(args, "number", None) or [])
    if not numbers:
        return ids
    try:
        with open(_ui_list_path(), "r", encoding="utf-8") as stream:
            shown = json.load(stream)
    except (OSError, ValueError):
        raise ArchiveError("no numbered list shown yet; run `list` first")
    for n in numbers:
        if not 1 <= n <= len(shown["items"]):
            raise ArchiveError("number %d is not on the last list" % n)
        ids.append(shown["items"][n - 1]["item_id"])
    return ids


def _archived_rows(engine):
    rows = []
    if os.path.isdir(engine.items):
        for name in sorted(os.listdir(engine.items)):
            path = os.path.join(engine.items, name, "envelope.json")
            if os.path.isfile(path):
                row = json.load(open(path, "r", encoding="ascii"))
                payload = open(os.path.join(engine.objects, row["object_ref"]), "rb").read()
                rows.append((row["item_id"], payload))
    return rows


def _active_rows():
    rows = []
    for base, dirs, files in os.walk(MEMORIES_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md") or ".bak" in name:
                continue
            for line in open(os.path.join(base, name), "rb").read().splitlines():
                m = re.search(rb"item-id: ([A-Za-z0-9][A-Za-z0-9:._-]{2,255})", line)
                if m:
                    rows.append((m.group(1).decode("ascii"), line + b"\n"))
    return rows


def _quote_text(payload):
    text = payload.decode("utf-8", "replace").strip()
    text = re.sub(r"<!--.*?-->", "", text).strip()
    return text.lstrip("-*\u00a7 ").strip()


def _transcript_selector(args):
    """Literal topic match, exact session, or inclusive local calendar dates."""
    if args.until and not args.since:
        raise ArchiveError("--until requires --since")
    if args.since:
        if not args.until:
            raise ArchiveError("date selection requires both --since and --until")
        try:
            start = datetime.datetime.strptime(args.since, "%Y-%m-%d")
            end = datetime.datetime.strptime(args.until, "%Y-%m-%d")
        except ValueError as exc:
            raise ArchiveError("dates must be YYYY-MM-DD") from exc
        if end < start:
            raise ArchiveError("--until precedes --since")
        # Convert each local midnight separately (including across DST).
        start = start.astimezone(datetime.timezone.utc)
        end = (end + datetime.timedelta(days=1)).astimezone(datetime.timezone.utc)
        return {"since": args.since, "until": args.until}, lambda row: (
            start <= _parse_iso_utc(row["ts"]) < end)
    if args.session is not None:
        if not args.session.strip():
            raise ArchiveError("session must not be empty")
        return {"session": args.session}, lambda row: row["session"] == args.session
    if not args.match or not args.match.strip():
        raise ArchiveError("match text must not be empty")
    return {"match": args.match}, lambda row: args.match in row["text"]


def _print_transcript_sample(rows):
    for n, row in enumerate(rows[:5], 1):
        sample = row["text"].replace("\n", " ")[:200]
        print("%d. %s [%s / %s] %s" %
              (n, row["ts"], row["session"], row["role"], sample))


def cmd_transcript_archive(args):
    engine = memory_archive_engine()
    conn = None
    try:
        selector, matches = _transcript_selector(args)
        if args.dry_run:
            # Do not init, index, recover journals, or advance watermarks on a
            # preview. Selection is of currently cached recall rows only.
            from pathlib import Path
            conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            if engine.needs_recovery():
                raise ArchiveError("pending archive journal; run transcript-list to recover first")
            excluded = _transcript_exclusions(engine.transcript_state())
            rows = [dict(r) for r in conn.execute("SELECT * FROM transcript_notes ORDER BY ts,id")
                    if matches(r) and transcript_identity(r) not in excluded]
            print("Dry run: %d transcript rows" % len(rows))
        else:
            conn = connect()
            init_db(conn)
            with engine.transaction():
                state = engine.transcript_state()
                _sync_transcript_archives(conn, state)
                rows = [dict(r) for r in conn.execute("SELECT * FROM transcript_notes ORDER BY ts,id")
                        if matches(r)]
                archive_id = engine.archive_transcripts(state, rows, selector) if rows else None
                _sync_transcript_archives(conn, state)
            print("Archived: %d transcript rows%s" %
                  (len(rows), " (%s)" % archive_id if archive_id else ""))
        _print_transcript_sample(rows)
        return 0
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


def cmd_transcript_restore(args):
    engine = memory_archive_engine()
    conn = None
    try:
        conn = connect()
        init_db(conn)
        with engine.transaction():
            state = engine.transcript_state()
            batch = next((b for b in state["archives"]
                          if b["archive_id"] == args.archive_id and b["state"] == "archived"), None)
            if not batch:
                raise ArchiveError("transcript selection is not archived: %s" % args.archive_id)
            for row in batch["rows"]:
                existing = conn.execute(
                    "SELECT text FROM transcript_notes WHERE session IS ? AND ts=? AND role IS ?",
                    (row["session"], row["ts"], row["role"])).fetchone()
                if existing and existing["text"] != row["text"]:
                    raise ArchiveError("transcript restore conflicts with an active row")
            engine.restore_transcripts(state, args.archive_id)
            _sync_transcript_archives(conn, state)
        print("Restored: %d transcript rows (%s)" % (len(batch["rows"]), args.archive_id))
        return 0
    except (ArchiveError, OSError, ValueError, sqlite3.Error) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


def cmd_transcript_list(args):
    try:
        engine = memory_archive_engine()
        with engine.transaction():
            batches = [b for b in engine.transcript_state()["archives"] if b["state"] == "archived"]
        print("Archived: %d selections, %d transcript rows" %
              (len(batches), sum(len(b["rows"]) for b in batches)))
        for n, batch in enumerate(batches, 1):
            print("%d. %s: %d rows (%s; %s)" %
                  (n, batch["archive_id"], len(batch["rows"]), batch["archived_at"],
                   json.dumps(batch["selector"], ensure_ascii=False)))
        return 0
    except (ArchiveError, OSError, ValueError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2


def cmd_list_items(args):
    """Numbered list the owner points at (`1\ubc88\uc744 ...`); numbers map to ids here only."""
    engine = memory_archive_engine()
    rows = _archived_rows(engine) if args.archived else _active_rows()
    items = []
    for item_id, payload in rows:
        text = _quote_text(payload)
        if args.query and args.query not in text:
            continue
        items.append({"item_id": item_id, "text": text})
    from archive_engine import _atomic
    _atomic(_ui_list_path(), (json.dumps({"archived": args.archived, "items": items},
                                         ensure_ascii=True, sort_keys=True) + "\n").encode("ascii"))
    for n, row in enumerate(items, 1):
        print("%d. \u201c%s\u201d" % (n, row["text"]))
    return 0


def cmd_archive(args):
    engine = memory_archive_engine()
    evidence_path = os.environ.get("MEMORY_ARCHIVE_EVIDENCE")
    if not evidence_path and args.reason == "already-owned" and not args.dry_run:
        print("[error] installed owner evidence unavailable", file=sys.stderr)
        return 2
    evidence_rows = {}
    if evidence_path:
        with open(evidence_path, "r", encoding="utf-8") as stream:
            evidence_rows = json.load(stream)
    try:
        ids = _resolve_numbers(args)
        if not ids:
            raise ArchiveError("no item given")
        for item_id in ids:
            path, pos, payload, raw, lines = _active_item_bytes(item_id)
            proof = evidence_rows.get(item_id, {})
            if args.reason == "superseded" and not proof:
                proof = {"destination_ref": "superseding-item", "destination_version": "1",
                         "binding_version": "supersession-v1",
                         "normalized_claim_hash": hashlib.sha256(payload).hexdigest(),
                         "receipt": "deterministic-supersession"}
            engine.archive_bytes(item_id, payload, os.path.relpath(path, MEMORIES_DIR),
                                 proof, dry_run=args.dry_run)
            if not args.dry_run:
                # Re-located per item so duplicate ordinals never shift.
                del lines[pos]
                with engine.transaction():
                    engine.journaled_replace(path, b"".join(lines))
        if not args.dry_run:
            cmd_index(argparse.Namespace(lock=None))
        print(json.dumps({"archived": ids, "dry_run": args.dry_run}, sort_keys=True))
        return 0
    except (ArchiveError, OSError, ValueError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2


def cmd_restore_archive(args):
    try:
        for item_id in _resolve_numbers(args):
            memory_archive_engine().restore(item_id, args.to)
        cmd_index(argparse.Namespace(lock=None))
        return 0
    except (ArchiveError, OSError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2


def cmd_delete_archive(args):
    engine = memory_archive_engine()
    try:
        ids = _resolve_numbers(args)
        if args.preview:
            print(engine.preview_delete(ids, args.owner_turn))
        else:
            if not args.confirm_token:
                raise ArchiveError("run --preview first, then supply --confirm-token")
            engine.delete(ids, args.owner_turn, args.confirm_token)
        return 0
    except (ArchiveError, OSError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2


def _replay_manifest_path(scan_id):
    return os.path.join(MEMORY_DATA_ROOT, "replay", scan_id + ".json")


def _replay_plan(path, evidence, batch_size, scan_id=None):
    """Pure plan: every line whose exact bytes have owner evidence, in file order,
    cut into batches; each batch carries the source hash it expects to see."""
    from archive_engine import rolling_remove
    source = open(path, "rb").read()
    source_sha = hashlib.sha256(source).hexdigest()
    scan_id = scan_id or source_sha[:12]
    relative = os.path.relpath(path, MEMORIES_DIR)
    entries, cursor = [], 0
    for line in source.splitlines(keepends=True):
        proof = evidence.get(hashlib.sha256(line).hexdigest())
        if proof and line.strip():
            entries.append({"item_id": stable_item_id(MEMORY_INSTANCE_ID, scan_id, relative,
                                                      cursor, line),
                            "start": cursor, "end": cursor + len(line),
                            "line_sha256": hashlib.sha256(line).hexdigest(),
                            "evidence": proof})
        cursor += len(line)
    batches, current, removed = [], source, 0
    expected = source_sha
    for n in range(0, len(entries), batch_size):
        chunk = entries[n:n + batch_size]
        spans = [(e["start"] - removed, e["end"] - removed) for e in chunk]
        current, after = rolling_remove(current, expected, spans)
        batches.append({"index": len(batches), "before_sha256": expected,
                        "after_sha256": after, "item_ids": [e["item_id"] for e in chunk]})
        removed += sum(e["end"] - e["start"] for e in chunk)
        expected = after
    return {"scan_id": scan_id, "instance_id": MEMORY_INSTANCE_ID, "source_relative": relative,
            "source_sha256": source_sha, "batch_size": batch_size,
            "entries": entries, "batches": batches, "final_sha256": expected}


def cmd_replay(args):
    """Backlog replay: dry-run plan by default; --apply archives batch by batch.

    Progress is the source file's own hash against the manifest, so a SIGKILL
    between (or inside) batches resumes exactly where the file says it is."""
    engine = memory_archive_engine()
    try:
        resume = args.resume or (args.scan_id if os.path.isfile(
            _replay_manifest_path(args.scan_id or "-")) else None)
        if resume:
            manifest = json.load(open(_replay_manifest_path(resume), "r", encoding="ascii"))
            path = os.path.join(MEMORIES_DIR, manifest["source_relative"])
        else:
            path = os.path.join(MEMORIES_DIR, args.file)
            evidence = json.load(open(args.evidence, "r", encoding="utf-8"))
            manifest = _replay_plan(path, evidence, args.batch_size, args.scan_id)
        if not args.apply:
            print(json.dumps({"dry_run": True, "scan_id": manifest["scan_id"],
                              "source_relative": manifest["source_relative"],
                              "archive_count": len(manifest["entries"]),
                              "batches": len(manifest["batches"]),
                              "items": [{"item_id": e["item_id"], "range": [e["start"], e["end"]],
                                         "line_sha256": e["line_sha256"],
                                         "evidence": e["evidence"]} for e in manifest["entries"]]},
                             sort_keys=True))
            return 0
        from archive_engine import _atomic
        mpath = _replay_manifest_path(manifest["scan_id"])
        if not os.path.isfile(mpath):
            _atomic(mpath, (json.dumps(manifest, sort_keys=True) + "\n").encode("ascii"))
        byid = {e["item_id"]: e for e in manifest["entries"]}
        done = 0
        position = {b["before_sha256"]: b["index"] for b in manifest["batches"]}
        position[manifest["final_sha256"]] = len(manifest["batches"])
        here = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if here not in position:
            raise ArchiveError("source changed outside replay; regenerate the dry-run")
        for batch in manifest["batches"][position[here]:]:
            current = open(path, "rb").read()
            sha = hashlib.sha256(current).hexdigest()
            if sha != batch["before_sha256"]:
                raise ArchiveError("source changed outside replay at batch %d" % batch["index"])
            removed = sum(e["end"] - e["start"] for b in manifest["batches"][:batch["index"]]
                          for e in (byid[i] for i in b["item_ids"]))
            spans = []
            for item_id in batch["item_ids"]:
                e = byid[item_id]
                payload = current[e["start"] - removed:e["end"] - removed]
                if hashlib.sha256(payload).hexdigest() != e["line_sha256"]:
                    raise ArchiveError("line moved since scan: %s" % item_id)
                engine.archive_bytes(item_id, payload, manifest["source_relative"], e["evidence"])
                spans.append((e["start"] - removed, e["end"] - removed))
            from archive_engine import rolling_remove
            new, _ = rolling_remove(current, sha, spans)
            if os.environ.get("MEMORY_TEST_KILL_AT") == "mid-batch:%d" % batch["index"]:
                os.kill(os.getpid(), 9)
            with engine.transaction():
                engine.journaled_replace(path, new)
            done += 1
            if os.environ.get("MEMORY_TEST_KILL_AT") == "after-batch:%d" % batch["index"]:
                os.kill(os.getpid(), 9)
        if os.environ.get("MEMORY_REPLAY_SKIP_INDEX") != "1":
            cmd_index(argparse.Namespace(lock=None))
        print(json.dumps({"applied_batches": done, "scan_id": manifest["scan_id"],
                          "final_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()},
                         sort_keys=True))
        return 0
    except (ArchiveError, OSError, ValueError, KeyError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2


def main():
    p = argparse.ArgumentParser(description="__AGENT_LABEL__ long-term memory recall core")
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("index", help="memories/*.md -> state.db incremental load")
    ip.add_argument("--lock", default=None,
                    help="lock file path (for auto-reindex). Released via atexit when done.")
    ip.add_argument("--wait-lock", action="store_true",
                    help="take the shared reindex lock (waiting for it) and retry a busy "
                         "database (DGN-1873; used by the classify check wrapper).")

    sub.add_parser("hook", help="UserPromptSubmit hook (stdin JSON -> auto-inject related memories)")

    sp = sub.add_parser("search", help="hybrid search (FTS5 + vector RRF)")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5, help="number of results (default 5)")
    sp.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("stats", help="index/search statistics")

    tp = sub.add_parser("today", help="same-day conversation browse (zero LLM)")
    tp.add_argument("--since", default=None, help="'HH:MM' (local today) or local ISO datetime; default local midnight")
    tp.add_argument("--limit", type=int, default=0, help="max rows (0 = all within char cap)")
    tp.add_argument("--json", action="store_true")

    ta = sub.add_parser("transcript-archive", help="archive selected cached transcript recall; raw logs stay intact")
    selector = ta.add_mutually_exclusive_group(required=True)
    selector.add_argument("--session", help="exact session ID")
    selector.add_argument("--since", help="first local date, YYYY-MM-DD (inclusive)")
    selector.add_argument("--match", help="literal, case-sensitive text substring")
    ta.add_argument("--until", help="last local date, YYYY-MM-DD (inclusive)")
    ta.add_argument("--dry-run", action="store_true", help="read-only count and sample of current cache")
    tr = sub.add_parser("transcript-restore", help="restore an archived transcript selection exactly")
    tr.add_argument("archive_id")
    sub.add_parser("transcript-list", help="numbered list of archived transcript selections")

    wp = sub.add_parser("write", help="raw text -> Haiku compress -> md write -> reindex")
    wp.add_argument("text", nargs="?", default=None, help="raw text (omit to read from stdin)")
    wp.add_argument("--source", default=None, help='source label (e.g. "telegram conversation")')
    wp.add_argument("--file", default="inbox.md", help="target md file (default inbox.md — specify identity/work-rules/routines/infra/about-user.md when topic is clear)")
    wp.add_argument("--section", default=None, help="target section header (created or appended to end if absent)")
    wp.add_argument("--dry-run", action="store_true", help="show compression result only, do not modify file")
    wp.add_argument("--force", action="store_true",
                    help="bypass DGN-446 structured-input gate and fragment-cap guard (use with care)")

    cp = sub.add_parser("consolidate", help="nightly consolidation: conversation transcript -> inbox.md distillation")
    cp.add_argument("--dry-run", action="store_true",
                    help="run compress+dedup only; no file/watermark/reindex/push writes (preview)")
    cp.add_argument("--no-push", action="store_true", help="write files but skip Telegram push")
    cp.add_argument("--since-days", type=int, default=None,
                    help="ignore watermark and force last N days (for testing)")
    cp.add_argument("--budget-secs", type=float, default=None,
                    help="DGN-1468: wall-clock budget for the run (default "
                         "DOGANY_CONSOLIDATE_BUDGET_SECS or %d). The run stops "
                         "starting chunks before it blows the budget and commits the "
                         "watermark for what it did finish; <=0 disables the budget "
                         "(manual catch-up with no kill limit)." % int(CONSOLIDATE_BUDGET_SECS))

    mp = sub.add_parser("mark-superseded",
                        help="DGN-2009: mark memory lines a later same-subject line contradicts")
    mp.add_argument("--dry-run", action="store_true", help="list only, write nothing")

    xp = sub.add_parser("classify-inbox",
                        help="weekly inbox classification: distribute inbox.md items to topic files + suggest new topics")
    xp.add_argument("--dry-run", action="store_true",
                    help="preview classification verdicts only; no inbox/topic-file/reindex writes")
    xp.add_argument("--no-push", action="store_true", help="classify but skip Telegram push")

    sub.add_parser("inbox-count",
                   help="print actual item count in inbox.md (shared empty logic with check wrapper). rc: 0=items/2=empty")

    ap = sub.add_parser("archive", help="move active stable IDs to proof-backed archive")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--number", type=int, nargs="+", help="numbers from the last `list`")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reason", choices=("already-owned", "superseded"),
                    default="already-owned")

    rp = sub.add_parser("restore", help="restore exact archived item bytes")
    rp.add_argument("ids", nargs="*")
    rp.add_argument("--number", type=int, nargs="+", help="numbers from the last `list --archived`")
    rp.add_argument("--to", choices=("original", "inbox"), default="original")

    dp = sub.add_parser("delete", help="owner-confirmed destruction from archive")
    dp.add_argument("ids", nargs="*")
    dp.add_argument("--number", type=int, nargs="+", help="numbers from the last `list --archived`")
    dp.add_argument("--from-archive", action="store_true", required=True)
    dp.add_argument("--owner-turn", required=True)
    dp.add_argument("--preview", action="store_true")
    dp.add_argument("--confirm-token")

    lp = sub.add_parser("list", help="numbered owner-facing list of active or archived items")
    lp.add_argument("--archived", action="store_true")
    lp.add_argument("--query", default=None)

    yp = sub.add_parser("replay", help="backlog replay: dry-run plan, or --apply in batches")
    yp.add_argument("--file", default="MEMORY.md", help="memories-relative source file")
    yp.add_argument("--evidence", help="JSON {line_sha256: ownership evidence}")
    yp.add_argument("--batch-size", type=int, default=500)
    yp.add_argument("--scan-id", default=None)
    yp.add_argument("--apply", action="store_true")
    yp.add_argument("--resume", default=None, metavar="SCAN_ID")

    args = p.parse_args()

    try:
        if not (args.cmd == "transcript-archive" and args.dry_run):
            recover_memory_transactions()
    except (ArchiveError, OSError, ValueError) as exc:
        print("[error] memory transaction recovery failed: %s" % exc, file=sys.stderr)
        if args.cmd != "hook":      # the hook never blocks the turn
            return 2

    if args.cmd == "hook":
        # cmd_hook calls exit(0) internally; does not depend on main's rc.
        cmd_hook(args)
        return 0
    if args.cmd == "index":
        if args.wait_lock:
            return 0 if _reindex_locked_with_retry() else 1
        return cmd_index(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "today":
        return cmd_today(args)
    if args.cmd == "transcript-archive":
        return cmd_transcript_archive(args)
    if args.cmd == "transcript-restore":
        return cmd_transcript_restore(args)
    if args.cmd == "transcript-list":
        return cmd_transcript_list(args)
    if args.cmd == "write":
        return cmd_write(args)
    if args.cmd == "consolidate":
        return cmd_consolidate(args)
    if args.cmd == "classify-inbox":
        return cmd_classify_inbox(args)
    if args.cmd == "mark-superseded":
        return cmd_mark_superseded(args)
    if args.cmd == "inbox-count":
        return cmd_inbox_count(args)
    if args.cmd == "archive":
        return cmd_archive(args)
    if args.cmd == "restore":
        return cmd_restore_archive(args)
    if args.cmd == "delete":
        return cmd_delete_archive(args)
    if args.cmd == "list":
        return cmd_list_items(args)
    if args.cmd == "replay":
        return cmd_replay(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
