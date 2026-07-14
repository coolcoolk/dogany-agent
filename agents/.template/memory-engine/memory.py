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

# ---- path constants ----
HERE = os.path.dirname(os.path.abspath(__file__))
# MEMORIES_DIR: env seam for testing (set MEMORIES_DIR=/tmp/... to use a scratch directory).
# Production: always uses the default path derived from HERE.
MEMORIES_DIR = os.environ.get(
    "MEMORIES_DIR",
    os.path.normpath(os.path.join(HERE, "..", "memories"))
)
DB_PATH = os.path.join(HERE, "state.db")

# Files excluded from search indexing. USER.md is hot (always injected into
# CLAUDE.md via @import), so it is always in context -> indexing it is
# redundant -> exclude. (2026-06-25)
INDEX_EXCLUDE = {"USER.md"}

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024  # bge-m3 output dimension

RRF_K = 60          # standard RRF constant
MISS_THRESHOLD = 0.30  # top1 cosine below this counts as a "miss" (stats)
TRANSCRIPT_FTS_TOPK = 3  # max raw conversations to splice in from the same-day rolling index search


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


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ======================================================================
# index command
# ======================================================================
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
    index_transcript_fts(conn)

    # gather a generous candidate pool then fuse
    pool = max(k * 4, 20)
    fts_ids = fts_search(conn, query, pool)
    vec_scored = vector_search(conn, query, pool)

    fused = rrf_fuse(fts_ids, vec_scored)[:k]

    # load note metadata
    results = []
    for nid, rrf_score, cos_score in fused:
        row = conn.execute(
            "SELECT source_file, section, text FROM notes WHERE id=?", (nid,)
        ).fetchone()
        if not row:
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
    for tr in transcript_fts_search(conn, query, TRANSCRIPT_FTS_TOPK):
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
        print(f'"{query}" -- no results.')
        return 0

    print(f'🔎 "{query}" -- top {len(results)}\n')
    for i, r in enumerate(results, 1):
        cos = f"{r['cosine']:.3f}" if r["cosine"] is not None else "n/a"
        sect = r["section"] or "(no section)"
        print(f"[{i}] {r['source_file']} > {sect}  (rrf={r['rrf_score']:.4f}, cos={cos})")
        # print body indented
        for ln in r["text"].splitlines():
            print(f"    {ln}")
        print()
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

COMPRESS_PROMPT = (
    "다음 내용에서 __USER_LABEL__(사용자)에 대해 장기 기억할 가치가 있는 영속적 사실만 골라, "
    "각각 한 줄짜리 원자적 항목으로 압축해라. 잡담/일시적 내용/추측은 버려라. "
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명이나 머리말을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만). "
    "기억할 가치가 있는 게 하나도 없으면 정확히 NONE 한 단어만 출력해라.\n\n"
    "내용:\n"
)


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
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{model} call failed: {e}")
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

    # 1) compress
    try:
        items = compress_with_haiku(raw)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    if not items:
        print("No persistent facts worth remembering; nothing stored.")
        return 0

    # 2) attach metadata: (date, source)
    today = datetime.date.today().isoformat()
    src = args.source or "unknown-source"
    tagged = [f"{it} ({today}, {src})" for it in items]

    # target file path
    target = os.path.join(MEMORIES_DIR, args.file)

    # print preview of what will be stored
    print(f"== compression result ({len(tagged)} items) -> {args.file}" +
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
TRANSCRIPT_GLOB = os.path.join(
    os.path.expanduser("~/.claude/projects"),
    re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(os.path.join(HERE, ".."))),
    "*.jsonl",
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
# Path to taxonomy document (the second-stage KEEP/DROP filter injects the full text).
TAXONOMY_PATH = os.path.join(HERE, "CONSOLIDATION_TAXONOMY.md")
# Maximum number of candidates to judge in a single second-stage filter batch call (prevents bloated prompts).
FILTER_BATCH_SIZE = 40

# Compression prompt for the __AGENT_LABEL__ assistant. Includes user facts + life decisions/habits/
# relationships/schedules/finances.
# Core rule: do NOT turn utterances or progress reports into facts. Only confirmed persistent facts/decisions.
CONSOLIDATE_PROMPT = (
    "아래는 __USER_LABEL__(사용자)과 생활비서 에이전트 __AGENT_LABEL__(어시스턴트)가 주고받은 대화 기록이다. "
    "이 안에서 장기 기억할 가치가 있는 영속적 사실/결정만 골라 각각 한 줄짜리 원자적 항목으로 압축해라.\n"
    "\n"
    "절대 원칙: 발화 자체나 진행 상황 보고가 아니라, 대화를 통해 확정된 영속적 사실/결정만 뽑는다. "
    "__AGENT_LABEL__(어시스턴트)가 한 말·질문·보고·멘트는 그 자체로는 사실이 아니다. 그 대화로 무엇이 확정됐는지만 남겨라.\n"
    "\n"
    "반드시 버려라(항목으로 절대 만들지 마라):\n"
    "- 코드블록·코드펜스(``` 류)·명령줄·셸 명령(launchctl, git, python ... 같은 실행 명령)\n"
    "- 질문(예: ~할까요? ~될까요?), 사과(죄송), 인사, 맞장구, 감탄\n"
    "- 발화 라벨(__USER_LABEL__:, __AGENT_LABEL__: 등)이나 그 일부\n"
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
    "- 입력: \"__USER_LABEL__: 단백질 목표 하루 160g으로 잡아\" → 출력: 단백질 목표 하루 160g\n"
    "- 입력: \"__USER_LABEL__, 죄송합니다. 다시 볼게요\" → 출력: 없음(버림. 사과+멘트)\n"
    "(이 대화 청크가 통째로 인사·질문·진행보고·명령뿐이라 남길 사실이 없으면 정확히 NONE 한 단어만 출력)\n"
    "\n"
    "출력은 항목당 한 줄, 불릿이나 번호 없이 순수 텍스트 줄로만. "
    "설명·머리말·발화 라벨을 절대 붙이지 마라. 날짜·출처 같은 메타데이터도 붙이지 마라(사실 내용만).\n"
    "\n"
    "대화 기록:\n"
)

# Noise patterns to filter from transcripts (hook injections, system reminders, etc.).
_NOISE_MARKERS = (
    "<system-reminder>",
    "[관련 기억 — 자동검색]",
    "[관련 기억",
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


def _extract_text_from_content(content):
    """Extract human-written text from user/assistant message.content.
    - If a string: use as-is (returns empty string if it is noise).
    - If a list: keep only type=='text' blocks (discard thinking/tool_use/tool_result).
    After extraction, apply compression-input denoising (code fences / shell commands stripped).
    Original is unchanged.
    Returns: cleaned text (may be empty string)."""
    if isinstance(content, str):
        raw = "" if _is_noise(content) else content.strip()
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t and not _is_noise(t):
                    parts.append(t.strip())
        raw = "\n".join(parts).strip()
    else:
        return ""
    if not raw:
        return ""
    return _preprocess_for_compress(raw)


def _iter_transcript_rows(watermark_iso, now):
    """Collect __USER_LABEL__ <-> __AGENT_LABEL__ messages after the watermark as
    a (ts, speaker, text, session) list.
    Shared by collect_transcript (consolidate) and index_transcript_fts (same-day index).
    watermark_iso: ISO8601 string or None (first run -> now - DEFAULT_LOOKBACK_DAYS).
    Returns: list sorted by timestamp ascending (may be empty)."""
    if watermark_iso:
        cutoff = watermark_iso
    else:
        cutoff = (now - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    import glob

    rows = []  # (ts, speaker, text, session)
    for fp in glob.glob(TRANSCRIPT_GLOB):
        session = os.path.splitext(os.path.basename(fp))[0]
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
                    ts = o.get("timestamp")
                    if not ts or ts <= cutoff:
                        continue  # at or before watermark (already processed) -> skip
                    content = o.get("message", {}).get("content")
                    text = _extract_text_from_content(content)
                    if not text:
                        continue
                    speaker = "__USER_LABEL__" if typ == "user" else "__AGENT_LABEL__"
                    rows.append((ts, speaker, text, session))
        except OSError:
            continue

    rows.sort(key=lambda r: r[0])  # ascending by timestamp
    return rows


def collect_transcript(watermark_iso, now):
    """Collect __USER_LABEL__ <-> __AGENT_LABEL__ conversation after the watermark, in time order
    (input for consolidate).
    Returns: (conversation text str, max timestamp str of processed messages or None, message count int).
    """
    rows = _iter_transcript_rows(watermark_iso, now)
    if not rows:
        return "", None, 0
    max_ts = rows[-1][0]
    convo = "\n".join(f"{spk}: {txt}" for _ts, spk, txt, _sess in rows)
    return convo, max_ts, len(rows)


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


def index_transcript_fts(conn, now=None):
    """Incrementally load today's raw conversation into transcript_notes/transcript_fts
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
        rows = _iter_transcript_rows(wm, now)
        if not rows:
            return 0
        cur = conn.cursor()
        for ts, speaker, text, session in rows:
            # F6: redact secrets before the raw message enters the same-day FTS
            # (transcript_notes/transcript_fts are recalled + re-injected by the hook).
            text = redact_secrets(text)
            cur.execute(
                "INSERT INTO transcript_notes(ts, role, session, text) VALUES(?,?,?,?)",
                (ts, speaker, session, text),
            )
            cur.execute(
                "INSERT INTO transcript_fts(rowid, text) VALUES(?,?)",
                (cur.lastrowid, text),
            )
        _tsfts_save_watermark(conn, rows[-1][0])
        conn.commit()
        return len(rows)
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0


def transcript_fts_search(conn, query, limit):
    """FTS5 trigram search on the same-day transcript_fts (bm25 ascending). No embeddings -> FTS only.
    Returns: [{"id","ts","role","session","text"}, ...] up to limit."""
    safe = '"' + query.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT t.id AS id, t.ts AS ts, t.role AS role, "
            "t.session AS session, t.text AS text "
            "FROM transcript_fts f JOIN transcript_notes t ON t.id=f.rowid "
            "WHERE transcript_fts MATCH ? ORDER BY bm25(transcript_fts) LIMIT ?",
            (safe, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def prune_transcript_fts(conn, cutoff_iso):
    """Delete same-day raw rows at or before cutoff_iso (already distilled into real notes).
    Called at end of consolidate. External-content FTS: delete fts->notes in that order by rowid.
    Returns: number of rows deleted."""
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM transcript_notes WHERE ts <= ?", (cutoff_iso,)
            )
        ]
        for tid in ids:
            conn.execute("DELETE FROM transcript_fts WHERE rowid=?", (tid,))
        conn.execute("DELETE FROM transcript_notes WHERE ts <= ?", (cutoff_iso,))
        conn.commit()
        return len(ids)
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


def archive_raw_transcript(watermark_iso, now):
    """Append the consumed transcript span (TEXT ONLY) to a gzip monthly archive.
    Uses the SAME source/args as collect_transcript for this watermark span
    (_iter_transcript_rows) -- it does not read beyond the consumed span.
    One JSON line per message: {ts, role, text}. Messages with no text after
    scrubbing (pure image/tool) are skipped. Best effort: any error is swallowed
    so archiving never blocks consolidate. Returns count of lines written."""
    import gzip

    rows = _iter_transcript_rows(watermark_iso, now)
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


def _chunk_convo(convo, max_chars=CONSOLIDATE_CHUNK_CHARS):
    """Split conversation text into chunks of at most max_chars, splitting on line (message) boundaries.
    A single line longer than max_chars becomes its own chunk (unavoidably truncated by character count)."""
    chunks = []
    cur = []
    cur_len = 0
    for ln in convo.split("\n"):
        add = len(ln) + 1
        if cur and cur_len + add > max_chars:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def compress_convo_chunked(convo):
    """Split a long conversation into chunks and compress each with CONSOLIDATE_PROMPT (Sonnet).
    First pass extracts liberally (recall-first); rule-filter and second-stage filter handle precision.
    If one chunk fails, its raw text is returned to the caller so it can be queued
    for retry (DGN-153) and processing continues (partial failure allowed).
    Returns: (item list, list of (chunk_text, error str) for failed chunks)."""
    chunks = _chunk_convo(convo)
    all_items = []
    failed_chunks = []
    seen = set()
    for i, ch in enumerate(chunks, 1):
        try:
            items = compress_with_haiku(
                ch, prompt_prefix=CONSOLIDATE_PROMPT, model=CONSOLIDATE_MODEL
            )
        except RuntimeError as e:
            failed_chunks.append((ch, str(e)))
            print(f"  [warn] chunk {i}/{len(chunks)} compression failed (will queue for retry): {e}", file=sys.stderr)
            continue
        for it in items:
            key = it.strip().lower()
            if key and key not in seen:
                seen.add(key)
                all_items.append(it)
    print(f"[consolidate] {len(chunks) - len(failed_chunks)}/{len(chunks)} chunks compressed successfully")
    return all_items, failed_chunks


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


def _retryq_process(conn, rows):
    """Retry compression for chunks queued by previous runs.
    Success -> dequeue and return the extracted items. Failure -> retry_count+1;
    at CONSOLIDATE_RETRY_MAX failed retries the chunk moves to the dead-letter
    file. Returns: (item list, still-queued count)."""
    items = []
    drained = 0
    still = 0
    dead = 0
    for row in rows:
        try:
            got = compress_with_haiku(
                row["chunk_text"], prompt_prefix=CONSOLIDATE_PROMPT, model=CONSOLIDATE_MODEL
            )
        except RuntimeError as e:
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
        conn.execute("DELETE FROM consolidate_retry_queue WHERE id=?", (row["id"],))
        conn.commit()
        drained += 1
        items.extend(got)
    line = f"[consolidate] retry queue: {drained} chunk(s) drained / {still} still queued for retry"
    if dead:
        line += f" / {dead} moved to dead-letter ({CONSOLIDATE_DEADLETTER_PATH})"
    print(line)
    return items, still


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
        m = re.match(r"^\[?(\d+)\]?[).:\s-]+\s*(KEEP|DROP)\b\s*(.*)$", s, re.IGNORECASE)
        if not m:
            continue
        idx = int(m.group(1))
        verdict = m.group(2).upper()
        reason = m.group(3).strip().lstrip("-—:").strip()
        if 1 <= idx <= n:
            verdicts[idx] = (verdict, reason or ("" if verdict == "KEEP" else "no-reason"))
    return verdicts


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
            "후보 목록:\n"
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
            continue

        for i, it in enumerate(batch, 1):
            verdict, reason = verdicts.get(i, ("DROP", "verdict-missing"))
            if verdict == "KEEP":
                kept.append(it)
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


def _build_consolidate_report(new_items, skipped, had_error, mem_lines):
    """Build the Telegram silent-report text (user-friendly, no technical jargon, no bold asterisks).
    new_items: newly stored items (raw one-liners without metadata).
    skipped: count skipped as duplicates (for stdout log only, not included in report).
    had_error: whether an error occurred during consolidation (embedding/compression failure etc.).
    mem_lines: current line count of inbox.md.
    Returns: report string to send, or None (skip sending).
    """
    md = datetime.date.today().strftime("%-m/%-d")
    n = len(new_items)

    # 0 new items, no error, no size warning -> silently skip sending (avoid daily noise).
    over_cap = mem_lines > MEMORY_LINES_CAP
    if n == 0 and not had_error and not over_cap:
        return None

    lines = [f"🌙 Memory sorted while you slept ({md})"]
    if n > 0:
        lines.append(f"- {n} new things remembered")
        for it in new_items:
            lines.append(f"  · {it}")
    else:
        lines.append("- Nothing new worth remembering")

    if over_cap:
        lines.append("Inbox is getting full -- want to sort by topic?")
    if had_error:
        lines.append("Hit a snag during sorting, please check")

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
            ["bash", push_sh, "--silent", "--text", text],
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


def _is_correction(text):
    """Returns True if the candidate text starts with a correction/revision/retraction marker.
    Such items are near-verbatim restatements that reverse existing facts, so their cosine
    score tends to be high and they would be caught by the dedup hard skip (making the stale
    fact stay authoritative). Exempt them from the skip so they are always stored (DGN-055)."""
    if not text:
        return False
    s = text.lstrip().lower()
    return any(s.startswith(m) for m in CORRECTION_MARKERS)


def cmd_consolidate(args):
    now = datetime.datetime.now(datetime.timezone.utc)

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

    # 2) collect transcript incrementally
    convo, max_ts, n_msgs = collect_transcript(watermark, now)
    # DGN-153: snapshot the retry queue BEFORE this run queues new failures,
    # so a chunk is never retried in the same run it failed in.
    pending_retries = _retryq_load(conn)
    if not convo and not pending_retries:
        print("No new conversations to consolidate.")
        conn.close()
        return 0
    if convo:
        print(f"[consolidate] {n_msgs} messages collected (max ts={max_ts})")
    else:
        print(f"[consolidate] no new conversations -- retry queue only ({len(pending_retries)} pending chunk(s))")

    # 2.5) raw transcript archive (DGN-093): before the watermark advance/prune discards them,
    # append the consumed span (TEXT ONLY) to a gzip monthly archive. Uses the same
    # source/watermark (_iter_transcript_rows) as collect_transcript so it does not read
    # beyond the consumed span. Skipped on dry-run (no state changes).
    if convo and not args.dry_run:
        n_arch = archive_raw_transcript(watermark, now)
        print(f"[consolidate] raw archive: {n_arch} rows stored -> {RAW_ARCHIVE_DIR}")

    # 3) first-pass compression (Sonnet, extract liberally). Split into chunks if long.
    if convo:
        candidates, failed_chunks = compress_convo_chunked(convo)
        print(f"[consolidate] first-pass candidates: {len(candidates)}")
    else:
        candidates, failed_chunks = [], []
    compress_failed = len(failed_chunks)

    # 3a) full-failure guard (pre-DGN-153 semantics kept): if ALL chunks failed and
    # produced 0 candidates, do NOT advance the watermark -- the whole span is
    # re-collected next night. Nothing is queued in this path (queueing plus a
    # preserved watermark would double-process the span); pending retries are
    # also left untouched for the next run.
    if convo and not candidates and compress_failed and compress_failed == len(_chunk_convo(convo)):
        print("All chunks failed compression -- watermark preserved (retry next run).")
        if pending_retries:
            print(f"[consolidate] retry queue untouched ({len(pending_retries)} chunk(s) still queued for retry)")
        conn.close()
        if not args.dry_run and not args.no_push:
            rep = _build_consolidate_report([], 0, True, 0)
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
    if pending_retries:
        if args.dry_run:
            print(f"[consolidate] [dry-run] retry queue skipped ({len(pending_retries)} chunk(s) pending)")
        else:
            retried_items, _still = _retryq_process(conn, pending_retries)
            merged_seen = {c.strip().lower() for c in candidates}
            for it in retried_items:
                key = it.strip().lower()
                if key and key not in merged_seen:
                    merged_seen.add(key)
                    candidates.append(it)

    if not candidates:
        print("No persistent facts worth remembering.")
        if not args.dry_run and max_ts:
            _ts_save_watermark(conn, max_ts)
            prune_transcript_fts(conn, max_ts)
        conn.close()
        return 0

    # 3.5) rule-filter (post-processing, deterministic double safety net): remove structural
    # artifact / utterance / command candidates.
    candidates, rule_dropped = _rule_filter_candidates(candidates)
    print(f"[consolidate] after rule-filter: {len(candidates)} ({len(rule_dropped)} removed by rules)")
    for it, why in rule_dropped:
        print(f"    [ruleDROP/{why}] {it}")

    # 3.6) second-stage KEEP/DROP filter (Sonnet + taxonomy). Batch verdicts.
    candidates, sec_dropped, filter_failed = _second_stage_filter(candidates)
    print(f"[consolidate] after second-stage filter: {len(candidates)} ({len(sec_dropped)} DROPped)")
    for it, why in sec_dropped:
        print(f"    [2ndDROP/{why}] {it}")

    if not candidates:
        print("No persistent facts passed the filter.")
        if not args.dry_run and max_ts:
            _ts_save_watermark(conn, max_ts)
            prune_transcript_fts(conn, max_ts)
        conn.close()
        return 0

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
    tagged = [f"{it} ({today}, nightly-consolidate)" for it in new_items]

    # print preview (always)
    print(f"\n== to be stored ({len(tagged)}) -> inbox.md ==")
    for it in tagged:
        print(f"  § {it}")
    if dup_items:
        print(f"\n-- already known (skipped): {len(dup_items)} --")
        for it in dup_items:
            print(f"  ~ {it}")

    # dry-run: do not touch file / DB / watermark / push
    if args.dry_run:
        print("\n[dry-run] file / watermark / re-index / push all skipped. above is preview.")
        conn.close()
        return 0

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

    # 6) advance watermark + prune same-day rolling index (delete raw rows with ts<=max_ts)
    # DGN-153: skipped on a retry-only run (no new span consumed -> max_ts is None).
    if max_ts:
        _ts_save_watermark(conn, max_ts)
        pruned = prune_transcript_fts(conn, max_ts)
        print(f"[consolidate] watermark advanced -> {max_ts} ({pruned} same-day raw rows pruned)")
    conn.close()

    # 6b) prune raw archive retention (DGN-093): delete monthly archives older than ~365 days.
    n_rawpruned = prune_raw_archive(now)
    if n_rawpruned:
        print(f"[consolidate] {n_rawpruned} raw archive files expired and deleted")

    # 7) re-index (only meaningful when new items were stored)
    if tagged:
        print("[consolidate] re-indexing...")
        cmd_index(argparse.Namespace(lock=None))

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
    report = _build_consolidate_report(new_items, len(dup_items), had_error, mem_lines)

    if report is None:
        print("[consolidate] report send skipped (0 new items, no error, within capacity).")
        return 0

    print("\n-- report --\n" + report)
    if args.no_push:
        print("\n[--no-push] Telegram send skipped.")
        return 0
    if _send_silent_report(report):
        print("[consolidate] report sent silently.")
    return 0


# ======================================================================
# classify-inbox command (weekly inbox classification: inbox.md -> distribute to topic files + suggest new topics)
# ======================================================================
# Core: once a week, use Opus to judge items that nightly consolidation stored in inbox.md
# and distribute them to existing topic files (memories/*.md, excluding USER and inbox).
# - Distribute: append to the topic file (respecting its has_sep format), remove from inbox.md.
# - New topic: do NOT create a new file; suggest it in the report (when 3+ items share a new label).
# - Drop: log and remove from inbox (no topic match and no lasting value).
# Safety first: on failure (Opus limit / error) never touch inbox.md; exit rc!=0.
CLASSIFY_MODEL = "opus"
# Always exclude these from distribution target (topic) candidates. inbox is the source; USER is hot.
CLASSIFY_EXCLUDE = {"USER.md", "inbox.md"}
# Suggest creating a new file when this many items accumulate under the same new-topic label.
NEW_TOPIC_SUGGEST_MIN = 3
# Chunked classification: max items per Opus call (env-overridable for testing).
# Splitting prevents 300s timeout death-spiral on large inboxes (e.g. 795-item backlog).
CLASSIFY_CHUNK_SIZE = int(os.environ.get("CLASSIFY_CHUNK_SIZE", "100"))
# Test seam: set CLASSIFY_CMD to a shell command that accepts a prompt on stdin and writes
# verdict lines on stdout. When set, _run_classifier uses it instead of the real claude call.
# Example: CLASSIFY_CMD="cat /tmp/stub_verdicts.txt" (ignores stdin, outputs fixed verdicts).
CLASSIFY_CMD = os.environ.get("CLASSIFY_CMD", None)

# Exit codes for classify-inbox (contract with the check wrapper).
#   0 = actual classification success (ok to stamp marker) /
#   2 = no items to classify (do NOT stamp marker) /
#   1 = failure (Opus limit / error, inbox preserved, retry next run).
# Key: if an empty inbox (header / comment seed / blank § only) is mistakenly judged as
# "has items" and rc=0 marker is stamped, real items that accumulate later will be skipped
# forever by the 7-day skip -> must always return rc=2 for empty inbox.
CLASSIFY_RC_OK = 0
CLASSIFY_RC_EMPTY = 2
CLASSIFY_RC_FAIL = 1


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
        "너는 생활비서 __AGENT_LABEL__의 장기기억 분류기다. 받은편지함(inbox)에 쌓인 항목들을 "
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
    # CLASSIFY_CMD seam: override the real claude subprocess for testing.
    if CLASSIFY_CMD:
        cmd = ["bash", "-c", CLASSIFY_CMD]
        stdin_data = prompt
    else:
        cmd = ["claude", "-p", "--model", CLASSIFY_MODEL, prompt]
        stdin_data = None
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[error] classifier call failed: {e}", file=sys.stderr)
        return {}, False
    if proc.returncode != 0:
        print(f"[error] classifier exited abnormally (rc={proc.returncode}): {proc.stderr.strip()}",
              file=sys.stderr)
        return {}, False

    valid_files = {fn for fn, _ in topics}
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
    Each chunk gets its own Opus call at 300s timeout. Verdicts are applied immediately
    after each successful chunk (partial progress persists -- the inbox shrinks even if
    later chunks fail). A failed chunk is logged and skipped; its items stay in the inbox
    so they are retried on the next run.

    Exit-code contract (preserved from original):
      rc=0  real classification succeeded -- check wrapper writes the 7-day marker.
      rc=2  no items to classify -- no marker written, retries daily.
      rc=1  all chunks failed -- inbox untouched or partially drained (marker NOT written,
            retries next run).
    Partial success (some chunks ok, some failed) exits rc=0 and writes the marker;
    failed-chunk items carry over to the next weekly run.
    """
    inbox_path = os.path.join(MEMORIES_DIR, "inbox.md")
    if not os.path.isfile(inbox_path):
        print("[classify-inbox] inbox.md not found — nothing to do (rc=2).")
        return CLASSIFY_RC_EMPTY

    items = _inbox_items(inbox_path)
    if not items:
        print("[classify-inbox] inbox empty (headers/comments/blank sections only) — done (no items, rc=2).")
        return CLASSIFY_RC_EMPTY

    n_total = len(items)
    chunk_size = max(1, CLASSIFY_CHUNK_SIZE)
    chunks = [items[i:i + chunk_size] for i in range(0, n_total, chunk_size)]
    n_chunks = len(chunks)
    print(f"[classify-inbox] inbox items: {n_total} — split into {n_chunks} chunk(s) of {chunk_size}")

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

    for ci, chunk in enumerate(chunks, 1):
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

    # ---- summary ----
    n_assigned_total = sum(len(v) for v in total_assign.values())
    print(f"\n[classify-inbox] done: {n_chunks} chunks total / {n_ok} ok / {n_fail} failed")
    print(f"[classify-inbox] items filed: {n_assigned_total} / dropped: {len(total_dropped)} / "
          f"inbox remaining: {len(remaining_items)}")
    if n_fail:
        carry_items = sum(failed_chunk_sizes)
        print(f"[classify-inbox] {carry_items} items carried over from {n_fail} failed chunk(s)",
              file=sys.stderr)

    if n_ok == 0:
        # All chunks failed: inbox is untouched (or backed up at start but no chunk wrote).
        print("[error] all chunks failed — inbox.md preserved, abnormal exit (rc=1).", file=sys.stderr)
        return CLASSIFY_RC_FAIL

    # At least one chunk succeeded: partial or full success.
    suggestions = [label for label, lst in total_new_groups.items()
                   if len(lst) >= NEW_TOPIC_SUGGEST_MIN]

    # Reindex (topic files changed by assignments).
    print("[classify-inbox] reindexing...")
    cmd_index(argparse.Namespace(lock=None))

    # Report: include chunk failure note if any chunks failed.
    report = _build_classify_report(total_assign, suggestions, total_dropped, today,
                                    n_ok=n_ok, n_chunks=n_chunks)
    if report:
        print("\n-- report --\n" + report)
        if not args.no_push:
            if _send_silent_report(report):
                print("[classify-inbox] report sent silently.")

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


def _hook_body_state_line():
    """Single-line current body/goal state (v2 deterministic injection). Returns a value if lifekit is present, else None.

    Key: this injection does not depend on search / topic-classification / agent judgment. If lifekit
    (config table) is attached, a body-state line is unconditionally inserted every turn, enforcing
    the 'read before asking' rule in code.
    For a base template with no lifekit (import failure or body-state not configured) -> None -> no-op.
    Single cheap SQLite read. Swallows all exceptions to never block the turn (best effort)."""
    try:
        # lifekit lives in database/ at the repo root (path-independent).
        # Priority: 1) LIFEKIT_DIR env var, 2) PROJECT_ROOT/database, 3) HERE/../database.
        # If none found (lifekit not installed or body-state not configured) -> silent no-op.
        env_dir = os.environ.get("LIFEKIT_DIR")
        proj_root = os.environ.get("PROJECT_ROOT")
        candidates = []
        if env_dir:
            candidates.append(env_dir)
        if proj_root:
            candidates.append(os.path.join(proj_root, "database"))
        candidates.append(os.path.normpath(os.path.join(HERE, "..", "database")))
        db_dir = None
        for cand in candidates:
            # Both lifekit.py and the actual data (lifekit.db) must be present to read body-state.
            # If code exists but db does not (fresh clone) -> silent no-op, no stderr noise.
            if os.path.isfile(os.path.join(cand, "lifekit.py")) and \
               os.path.isfile(os.path.join(cand, "lifekit.db")):
                db_dir = cand
                break
        if db_dir is None:
            return None  # lifekit absent / not configured -> silent no-op
        if db_dir not in sys.path:
            sys.path.insert(0, db_dir)
        import lifekit as _lk
        # Fresh mint: db exists but the config table is empty (no real user data
        # yet). load_body_stats() would fall back to DEFAULT_STATS (a generic
        # placeholder body, NOT the owner's) -- injecting that would fake data.
        # So: no config rows -> no-op. The line appears only once the user sets
        # real stats via the lifekit CLI (config table gets rows).
        try:
            conn = _lk.get_conn()
            try:
                n = conn.execute("SELECT COUNT(*) FROM config;").fetchone()[0]
            finally:
                conn.close()
        except BaseException:
            return None
        if not n:
            return None  # empty config -> new user -> no body-state line
        stats = _lk.load_body_stats()
        today = datetime.date.today().isoformat()
        a = _lk.agg_day(today)
        burn_kcal = a.get('burn_kcal', 0) or 0
        t = _lk.compute_targets(stats, exercise_kcal=burn_kcal)
        g = _lk.compute_macro_goals(t["eff_goal"], stats)
        gm = stats.get("goal_mode", "")
        wt = stats.get("weight_kg", "")
        # DGN-285 guard: goal_mode is only ever set by real user setup.
        # Empty goal_mode = fresh instance -> lifekit would render CODE
        # DEFAULTS (weight 70 etc.) as if they were user facts and tell the
        # model "do not re-ask". Fabricated stats poison fresh onboarding/
        # consult flows, so stay silent instead.
        if not str(gm).strip():
            return None
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
            "(lifekit canonical; 사용자께 다시 묻지 말 것)"
        )
    except BaseException:
        return None  # lifekit not configured / error -> no-op (must not block the turn)


def _hook_compose(recall_ctx=None):
    """Assemble the additionalContext string to inject (always time + body-state if present + recall if present).
    body-state is v2 deterministic injection (every turn). recall is only included if it passed the weak-match cutoff."""
    parts = [_now_local_line()]
    bs = _hook_body_state_line()
    if bs:
        parts.append(bs)
    if recall_ctx:
        parts.append(recall_ctx)
    return "\n\n".join(parts)


def _emit_empty():
    """Even with no match, unconditionally inject the current time line + body-state (if present), then exit 0.
    body-state is v2: if lifekit is present, it is injected deterministically every turn regardless of search results."""
    try:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(None),
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
    """Run the full search on a daemon thread and return results only within HOOK_SEARCH_TIMEOUT seconds.
    Returns None on timeout or error."""
    import threading

    box = {}

    def worker():
        try:
            box["result"] = search_core(query, k=HOOK_TOPK, log=False)
        except BaseException as e:  # absorb everything including SystemExit
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(HOOK_SEARCH_TIMEOUT)
    if t.is_alive():
        return None  # timed out -> give up
    return box.get("result")


def _hook_build_context(results):
    """Build the additionalContext string to inject. Returns None for weak matches.
    Cut: exclude results where cosine < HOOK_VEC_CUTOFF. cosine=None (FTS-only match) is also excluded (conservative)."""
    if not results:
        return None
    top = results[0].get("cosine")
    if top is None or top < HOOK_VEC_CUTOFF:
        return None

    lines = ["[관련 기억 — 자동검색]"]
    for r in results:
        cs = r.get("cosine")
        if cs is None or cs < HOOK_VEC_CUTOFF:
            continue
        section = r.get("section") or "(no section)"
        text = (r.get("text") or "").strip().replace("\n", " ")
        if len(text) > HOOK_SNIPPET_MAX:
            text = text[:HOOK_SNIPPET_MAX] + "…"
        lines.append(f"- [{section}] {text}")
    if len(lines) == 1:  # only header remains -> no actual items
        return None
    return "\n".join(lines)


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


def _hook_keyword_fallback(prompt):
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
        lines = ["[관련 기억 — 키워드검색]"]
        for nid in ids:
            row = conn.execute(
                "SELECT section, text FROM notes WHERE id=?", (nid,)
            ).fetchone()
            if not row:
                continue
            section = row["section"] or "(no section)"
            text = (row["text"] or "").strip().replace("\n", " ")
            if len(text) > HOOK_SNIPPET_MAX:
                text = text[:HOOK_SNIPPET_MAX] + "…"
            lines.append(f"- [{section}] {text}")
        if len(lines) == 1:
            return None
        return "\n".join(lines)
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


def cmd_hook(args):
    """UserPromptSubmit hook. Calls exit(0) directly (does not depend on main's rc).
    Any error exits 0 without writing anything to stdout (empty fallback)."""
    try:
        # 1) parse stdin JSON -> extract prompt
        try:
            raw = sys.stdin.read()
            data = json.loads(raw)
            prompt = data.get("prompt")
        except Exception:
            _emit_empty()
            return
        if not isinstance(prompt, str) or not prompt.strip():
            _emit_empty()
            return

        # 1.5) trigger auto-reindex (best effort, fire-and-forget).
        try:
            _hook_maybe_reindex()
        except Exception:
            pass

        # 2) rebind global embed to the short-timeout embedding function for hook path.
        global embed
        embed = _short_embed

        # 3) search (thread timeout guard)
        results = _hook_search_with_timeout(prompt.strip())

        # 4) build context + weak-match cutoff.
        ctx = _hook_build_context(results) if results else None

        # 4b) M4 fallback: if the vector lane is empty (Ollama down/absent -> all cosine=None,
        #     whole-prompt phrase matching also mostly fails), fall back to keyword FTS recall.
        #     In the default product deployment without Ollama, skipping this path would make
        #     auto-recall effectively a no-op (time line only). Inject keyword hits without cosine cutoff.
        if not ctx and not _has_vector_hit(results):
            ctx = _hook_keyword_fallback(prompt.strip())

        if not ctx:
            _emit_empty()
            return

        # 5) emit JSON injection (stdout, once). Order: time + body-state (if present) + recall.
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _hook_compose(ctx),
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


# ======================================================================
# CLI
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="__AGENT_LABEL__ long-term memory recall core")
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("index", help="memories/*.md -> state.db incremental load")
    ip.add_argument("--lock", default=None,
                    help="lock file path (for auto-reindex). Released via atexit when done.")

    sub.add_parser("hook", help="UserPromptSubmit hook (stdin JSON -> auto-inject related memories)")

    sp = sub.add_parser("search", help="hybrid search (FTS5 + vector RRF)")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5, help="number of results (default 5)")
    sp.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("stats", help="index/search statistics")

    wp = sub.add_parser("write", help="raw text -> Haiku compress -> md write -> reindex")
    wp.add_argument("text", nargs="?", default=None, help="raw text (omit to read from stdin)")
    wp.add_argument("--source", default=None, help='source label (e.g. "telegram conversation")')
    wp.add_argument("--file", default="inbox.md", help="target md file (default inbox.md — specify identity/work-rules/routines/infra/about-user.md when topic is clear)")
    wp.add_argument("--section", default=None, help="target section header (created or appended to end if absent)")
    wp.add_argument("--dry-run", action="store_true", help="show compression result only, do not modify file")

    cp = sub.add_parser("consolidate", help="nightly consolidation: conversation transcript -> inbox.md distillation")
    cp.add_argument("--dry-run", action="store_true",
                    help="run compress+dedup only; no file/watermark/reindex/push writes (preview)")
    cp.add_argument("--no-push", action="store_true", help="write files but skip Telegram push")
    cp.add_argument("--since-days", type=int, default=None,
                    help="ignore watermark and force last N days (for testing)")

    xp = sub.add_parser("classify-inbox",
                        help="weekly inbox classification: distribute inbox.md items to topic files + suggest new topics")
    xp.add_argument("--dry-run", action="store_true",
                    help="preview classification verdicts only; no inbox/topic-file/reindex writes")
    xp.add_argument("--no-push", action="store_true", help="classify but skip Telegram push")

    sub.add_parser("inbox-count",
                   help="print actual item count in inbox.md (shared empty logic with check wrapper). rc: 0=items/2=empty")

    args = p.parse_args()

    if args.cmd == "hook":
        # cmd_hook calls exit(0) internally; does not depend on main's rc.
        cmd_hook(args)
        return 0
    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    if args.cmd == "write":
        return cmd_write(args)
    if args.cmd == "consolidate":
        return cmd_consolidate(args)
    if args.cmd == "classify-inbox":
        return cmd_classify_inbox(args)
    if args.cmd == "inbox-count":
        return cmd_inbox_count(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
