#!/usr/bin/env python3
"""
notion_projects_resync.py -- DGN-256: delta resync Notion projects -> lifekit.db.

Reads every page from the Notion project DB (7534fb66-...) and upserts into
the local projects table keyed by notion_id.

Mapped Notion fields:
  이름       -> title
  상태       -> status
  시작 날짜  -> start_date
  끝 날짜    -> end_date
  비고       -> note
  영역       -> area_id (via areas.notion_id lookup; NULL if unresolvable)

Unmapped Notion fields (no local column): 기한, 태그, 진척도 -- ignored.

Local rows whose notion_id is absent from Notion are NOT deleted; they are
reported as orphans.

Idempotent: a second run against unchanged Notion data produces 0 changes.

Usage:
  python3 notion_projects_resync.py          # live run
  python3 notion_projects_resync.py --dry    # dry run (no writes, no commit)
"""

import argparse
import datetime
import json
import os
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
DB_PROJECT_ID = "7534fb66-1c49-48ec-bdc6-16fd34771642"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_DIR = os.path.dirname(_SCRIPT_DIR)       # database/
_AG_ROOT = os.path.dirname(_DB_DIR)          # agent root

DB_PATH = os.path.join(_DB_DIR, "lifekit.db")
ENV_PATH = os.path.join(_AG_ROOT, ".telegram_bot", ".env")

EXPECTED_USER_VERSION = 6

# ---------------------------------------------------------------------------
# ULID (stdlib only, matches lifekit.py new_ulid)
# ---------------------------------------------------------------------------
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value, length):
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid():
    ts_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    ts_part = _encode(ts_ms & ((1 << 48) - 1), 10)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    rand_part = _encode(rand, 16)
    return ts_part + rand_part


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def load_notion_token():
    if not os.path.isfile(ENV_PATH):
        sys.exit("ERROR: .env not found at " + ENV_PATH)
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("NOTION_TOKEN="):
                tok = line.split("=", 1)[1].strip()
                if tok:
                    return tok
    sys.exit("ERROR: NOTION_TOKEN not found in .env")


def notion_request(token, method, path, body=None):
    url = NOTION_BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("Notion API error %d: %s" % (e.code, e.read()[:400]))


def notion_query_all(token, db_id, page_size=100):
    results = []
    cursor = None
    while True:
        body = {"page_size": page_size}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion_request(token, "POST", "/databases/%s/query" % db_id, body)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


# ---------------------------------------------------------------------------
# Property extractors (minimal subset needed for projects)
# ---------------------------------------------------------------------------

def title_text(page):
    for p in page.get("properties", {}).values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
    return ""


def status_name(page, field):
    p = page.get("properties", {}).get(field)
    if p is None:
        return None
    s = p.get("status")
    return s.get("name") if s else None


def date_start(page, field):
    p = page.get("properties", {}).get(field)
    if p is None:
        return None
    d = p.get("date")
    if d is None:
        return None
    return d.get("start")


def rich_text_str(page, field):
    p = page.get("properties", {}).get(field)
    if p is None:
        return None
    s = "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
    return s if s else None


def relation_first_id(page, field):
    """Return the single relation page-id if there is exactly one; else None.
    Also handles has_more=True by noting it (we cannot paginate here without
    the token; callers pass the token separately if needed)."""
    p = page.get("properties", {}).get(field)
    if p is None:
        return None, 0
    ids = [r["id"] for r in p.get("relation", []) if r.get("id")]
    has_more = bool(p.get("has_more"))
    count = len(ids) + (1 if has_more else 0)
    if len(ids) == 1 and not has_more:
        return ids[0], 1
    return None, count   # 0 or ambiguous


def norm_id(notion_id):
    return notion_id.replace("-", "") if notion_id else None


def iso_to_canonical(s, fallback):
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    v = conn.execute("PRAGMA user_version;").fetchone()[0]
    if v != EXPECTED_USER_VERSION:
        conn.close()
        sys.exit("ERROR: DB user_version=%d, expected %d" % (v, EXPECTED_USER_VERSION))
    return conn


def build_area_map(conn):
    """normalized notion_id -> areas.id"""
    area_map = {}
    for r in conn.execute(
            "SELECT id, notion_id FROM areas WHERE notion_id IS NOT NULL"):
        area_map[norm_id(r["notion_id"])] = r["id"]
    return area_map


# ---------------------------------------------------------------------------
# Main resync logic
# ---------------------------------------------------------------------------

def resync(conn, token, dry_run=False):
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    print("Fetching Notion project DB...", file=sys.stderr)
    pages = notion_query_all(token, DB_PROJECT_ID)
    print("Fetched %d pages." % len(pages), file=sys.stderr)

    area_map = build_area_map(conn)

    # Build set of all notion_ids in Notion response
    notion_ids_in_notion = set()

    scanned = 0
    updated = 0
    inserted = 0
    area_unresolved = 0

    for page in pages:
        notion_id = page["id"]
        title = title_text(page)
        if not title.strip():
            continue  # skip empty-title rows

        scanned += 1
        notion_ids_in_notion.add(notion_id)

        status = status_name(page, "상태")
        start_date = date_start(page, "시작 날짜")
        end_date = date_start(page, "끝 날짜")
        note = rich_text_str(page, "비고")
        created_at_raw = page.get("created_time", now_str)
        created_at = iso_to_canonical(created_at_raw, now_str)

        # Resolve area: exactly one unambiguous relation required
        rel_id, rel_count = relation_first_id(page, "영역")
        area_id = None
        if rel_id is not None:
            area_id = area_map.get(norm_id(rel_id))
            if area_id is None:
                area_unresolved += 1  # relation exists but not in areas table
        elif rel_count != 0:
            area_unresolved += 1  # ambiguous (>1) or has_more

        existing = conn.execute(
            "SELECT id, title, status, start_date, end_date, note, area_id "
            "FROM projects WHERE notion_id = ?",
            (notion_id,)).fetchone()

        if existing:
            # Check if any mapped field changed
            changed = (
                existing["title"] != title
                or existing["status"] != status
                or existing["start_date"] != start_date
                or existing["end_date"] != end_date
                or existing["note"] != note
                or existing["area_id"] != area_id
            )
            if changed:
                updated += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE projects SET title=?, status=?, start_date=?, "
                        "end_date=?, note=?, area_id=? WHERE notion_id=?",
                        (title, status, start_date, end_date, note, area_id,
                         notion_id))
        else:
            inserted += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO projects "
                    "(ulid, title, status, start_date, end_date, note, "
                    "area_id, notion_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (new_ulid(), title, status, start_date, end_date, note,
                     area_id, notion_id, created_at))

    # Orphans: local rows with notion_id not in Notion
    local_with_notion_id = conn.execute(
        "SELECT title, notion_id FROM projects "
        "WHERE notion_id IS NOT NULL").fetchall()
    orphan_titles = []
    for row in local_with_notion_id:
        if row["notion_id"] not in notion_ids_in_notion:
            orphan_titles.append(row["title"])
    orphaned = len(orphan_titles)

    if not dry_run:
        conn.commit()

    print("scanned=%d updated=%d inserted=%d orphaned=%d area_unresolved=%d"
          % (scanned, updated, inserted, orphaned, area_unresolved))

    if orphan_titles:
        print("Orphaned local projects (notion_id gone from Notion):")
        for t in orphan_titles:
            print("  " + t)

    return {
        "scanned": scanned,
        "updated": updated,
        "inserted": inserted,
        "orphaned": orphaned,
        "area_unresolved": area_unresolved,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="DGN-256: delta resync Notion projects -> lifekit.db")
    parser.add_argument("--dry", action="store_true",
                        help="Dry run: fetch and compare but do not write.")
    args = parser.parse_args()

    token = load_notion_token()
    conn = open_db(DB_PATH)
    try:
        resync(conn, token, dry_run=args.dry)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
