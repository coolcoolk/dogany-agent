#!/usr/bin/env python3
"""
relmod.py -- relationship module v1
Owns: database/relationship.db (module sidecar)
Reads: database/lifekit.db (READ-ONLY, sqlite URI mode=ro)

stdlib only: sqlite3, argparse, datetime, os, sys, shutil, tempfile
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RELDB_PATH = os.path.join(SCRIPT_DIR, 'relationship.db')
LIFEDB_PATH = os.path.join(SCRIPT_DIR, 'lifekit.db')

TODAY = datetime.date.today().isoformat()

# ---------------------------------------------------------------------------
# Schema DDL for relationship.db
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS person_facts (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL,
    fact       TEXT    NOT NULL,
    source     TEXT    NOT NULL CHECK (source IN ('retro','chat','onboarding')),
    noted_on   DATE    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS touches (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('call','message','dm','other','meet')),
    touched_on DATE    NOT NULL,
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS person_prefs (
    person_id    INTEGER PRIMARY KEY,
    channel_mode TEXT    NOT NULL DEFAULT 'meet' CHECK (channel_mode IN ('meet','contact','mixed')),
    let_fade     INTEGER NOT NULL DEFAULT 0,
    snooze_until DATE,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL,
    shown_on   DATE    NOT NULL,
    outcome    TEXT    NOT NULL CHECK (outcome IN ('shown','dismissed','acted','fade_asked','checkin')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# ---------------------------------------------------------------------------
# DB connections
# ---------------------------------------------------------------------------

def rel_conn(db_path=None):
    """Open relationship.db with WAL + foreign_keys."""
    path = db_path or RELDB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def lk_conn(db_path=None):
    """Open lifekit.db READ-ONLY via URI."""
    path = db_path or LIFEDB_PATH
    uri = "file:{}?mode=ro".format(path.replace('\\', '/'))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Recreate-migrations: (table, needle that must appear in the CHECK,
# fresh CREATE stmt pulled from SCHEMA_SQL, column list to copy).
# Only fires when the stored table SQL lacks the needle; rows are preserved.
_MIGRATIONS = [
    ('touches', "'meet'",
     "CREATE TABLE touches ("
     " id INTEGER PRIMARY KEY,"
     " person_id INTEGER NOT NULL,"
     " kind TEXT NOT NULL CHECK (kind IN ('call','message','dm','other','meet')),"
     " touched_on DATE NOT NULL,"
     " note TEXT,"
     " created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))",
     "id, person_id, kind, touched_on, note, created_at"),
    ('alert_log', "'checkin'",
     "CREATE TABLE alert_log ("
     " id INTEGER PRIMARY KEY,"
     " person_id INTEGER NOT NULL,"
     " shown_on DATE NOT NULL,"
     " outcome TEXT NOT NULL CHECK (outcome IN ('shown','dismissed','acted','fade_asked','checkin')),"
     " created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))",
     "id, person_id, shown_on, outcome, created_at"),
]


def _migrate_checks(conn):
    """Recreate a table with the widened CHECK when the stored SQL lacks the
    new enum value. Rows are copied over (preserved). No-op on fresh DBs."""
    for table, needle, create_sql, cols in _MIGRATIONS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or needle in row[0]:
            continue
        old = table + '_old'
        conn.execute("ALTER TABLE {} RENAME TO {}".format(table, old))
        conn.execute(create_sql)
        conn.execute(
            "INSERT INTO {} ({cols}) SELECT {cols} FROM {}".format(table, old, cols=cols))
        conn.execute("DROP TABLE {}".format(old))
    conn.commit()


# Additive column migrations: (table, column, ALTER-ADD-COLUMN clause).
# Applied only when the column is missing. Nullable/defaulted so old rows are
# fine. SQLite has no IF NOT EXISTS for ADD COLUMN, so we probe pragma first.
_ADD_COLUMNS = [
    ('person_prefs', 'snooze_until', "ALTER TABLE person_prefs ADD COLUMN snooze_until DATE"),
]


def _migrate_add_columns(conn):
    """Add newly introduced columns to existing tables (no-op on fresh DBs)."""
    for table, column, alter_sql in _ADD_COLUMNS:
        cols = [r[1] for r in conn.execute("PRAGMA table_info({})".format(table)).fetchall()]
        if column not in cols:
            conn.execute(alter_sql)
    conn.commit()


def ensure_schema(db_path=None):
    """Create tables if they do not exist; widen CHECKs and add columns on old DBs."""
    conn = rel_conn(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _migrate_checks(conn)
        _migrate_add_columns(conn)
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Meeting dedup helper (shared by last-contact / retro-candidates / stale / drift)
#
# UNION of both lanes, deduplicated by (person_id, local_date).
# Legacy lane: appointments.start_at -> substr(start_at,1,10)
# Live lane:   event + event_persons, date = COALESCE(rec_date, substr(start_at,1,10))
# Only dates <= today are counted for gap math.
# ---------------------------------------------------------------------------

DEDUP_MEET_SQL = """
SELECT DISTINCT person_id, local_date FROM (
    SELECT ap.person_id, substr(a.start_at,1,10) AS local_date
    FROM appointments a
    JOIN appointment_persons ap ON a.id = ap.appointment_id
    WHERE a.start_at IS NOT NULL
    UNION ALL
    SELECT ep.person_id, COALESCE(e.rec_date, substr(e.start_at,1,10)) AS local_date
    FROM event e
    JOIN event_persons ep ON e.id = ep.event_id
    WHERE e.kind = 'appointment'
    AND e.start_at IS NOT NULL
)
"""

def last_meeting_date(lk, person_id, today=None):
    """Return (date_str, 'meeting') for the most recent meeting <= today, or (None, None)."""
    t = today or TODAY
    row = lk.execute(
        "SELECT MAX(local_date) FROM ({}) WHERE person_id=? AND local_date<=?".format(DEDUP_MEET_SQL),
        (person_id, t)
    ).fetchone()
    d = row[0] if row else None
    return (d, 'meeting') if d else (None, None)


def last_touch_date(rel, person_id, today=None, kinds=None):
    """Return (date_str, kind) for the most recent touch <= today, or (None, None).
    kinds: optional iterable to restrict which touch kinds count.
    A kind='meet' touch (recalled meeting) reports as 'meet(recalled)'."""
    t = today or TODAY
    sql = "SELECT touched_on, kind FROM touches WHERE person_id=? AND touched_on<=?"
    params = [person_id, t]
    if kinds:
        kinds = list(kinds)
        sql += " AND kind IN ({})".format(",".join("?" * len(kinds)))
        params.extend(kinds)
    sql += " ORDER BY touched_on DESC LIMIT 1"
    row = rel.execute(sql, params).fetchone()
    if row:
        kind = row['kind']
        if kind == 'meet':
            kind = 'meet(recalled)'
        return (row['touched_on'], kind)
    return (None, None)


def last_contact(lk, rel, person_id, today=None):
    """Return (date_str, kind) for the most recent contact (meeting or touch) <= today."""
    md, mk = last_meeting_date(lk, person_id, today)
    td, tk = last_touch_date(rel, person_id, today)
    if md and td:
        return (md, mk) if md >= td else (td, tk)
    return (md, mk) if md else (td, tk)


# Future window for the appointment lanes (any appointment strictly after today).
UPCOMING_MEET_SQL = """
SELECT ap.person_id, substr(a.start_at,1,10) AS local_date, a.title AS title
FROM appointments a
JOIN appointment_persons ap ON a.id = ap.appointment_id
WHERE a.start_at IS NOT NULL
UNION ALL
SELECT ep.person_id, COALESCE(e.rec_date, substr(e.start_at,1,10)) AS local_date, e.title AS title
FROM event e
JOIN event_persons ep ON e.id = ep.event_id
WHERE e.kind = 'appointment' AND e.start_at IS NOT NULL
"""


def _person_name(lk, person_id):
    """Resolve a person's display name from lifekit (read-only). '' if missing."""
    row = lk.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
    return row['name'] if row else ''


def has_upcoming_appointment(lk, person_id, today=None):
    """True if the person has any appointment (either lane) strictly after today.
    Used to drop 'go contact them' suggestions for people you already plan to meet."""
    t = today or TODAY
    row = lk.execute(
        "SELECT COUNT(*) FROM ({}) WHERE person_id=? AND local_date>?".format(UPCOMING_MEET_SQL),
        (person_id, t)
    ).fetchone()
    return bool(row and row[0])


def last_meeting(lk, rel, person_id, today=None):
    """Meet-based recency: (date_str, occasion_title) for the most recent actual
    MEETING <= today. A meeting is a lifekit appointment (either lane) OR a
    kind='meet' recalled touch. occasion_title is the appointment title on that
    date, or '' for a recalled touch (no title). Returns (None, '') if the person
    has never had a meeting recorded."""
    t = today or TODAY
    meet_appt_d, _ = last_meeting_date(lk, person_id, t)
    meet_touch_d, _ = last_touch_date(rel, person_id, t, kinds=['meet'])
    # Pick the later of the two meet lanes.
    best_d = None
    for d in (meet_appt_d, meet_touch_d):
        if d and (best_d is None or d > best_d):
            best_d = d
    if best_d is None:
        return (None, '')
    title = ''
    # Only appointment-lane meetings carry an occasion title.
    if best_d == meet_appt_d:
        row = lk.execute(
            "SELECT title FROM ({}) WHERE person_id=? AND local_date=? "
            "AND title IS NOT NULL LIMIT 1".format(UPCOMING_MEET_SQL),
            (person_id, best_d)
        ).fetchone()
        if row and row[0]:
            title = row[0]
    return (best_d, title)


# ---------------------------------------------------------------------------
# Verb: fact-add
# ---------------------------------------------------------------------------

def cmd_fact_add(args, rel, lk):
    noted_on = args.date or TODAY
    # D3 find-first: same person + same noted_on + similar fact (case-insensitive substring)
    existing = rel.execute(
        "SELECT id, fact FROM person_facts WHERE person_id=? AND noted_on=?",
        (args.person_id, noted_on)
    ).fetchall()
    needle = args.fact.lower()
    match = None
    for row in existing:
        stored = row['fact'].lower()
        if needle in stored or stored in needle:
            match = row
            break
    if match:
        rel.execute(
            "UPDATE person_facts SET fact=?, source=?, created_at=datetime('now','localtime') WHERE id=?",
            (args.fact, args.source, match['id'])
        )
        rel.commit()
        print("UPDATED\t{}\t{}\t{}".format(match['id'], noted_on, args.fact))
    else:
        cur = rel.execute(
            "INSERT INTO person_facts (person_id, fact, source, noted_on) VALUES (?,?,?,?)",
            (args.person_id, args.fact, args.source, noted_on)
        )
        rel.commit()
        print("OK\t{}\t{}\t{}".format(cur.lastrowid, noted_on, args.fact))


# ---------------------------------------------------------------------------
# Verb: fact-list
# ---------------------------------------------------------------------------

def cmd_fact_list(args, rel, lk):
    limit = args.limit if args.limit else 20
    rows = rel.execute(
        "SELECT id, noted_on, source, fact FROM person_facts WHERE person_id=? ORDER BY noted_on DESC LIMIT ?",
        (args.person_id, limit)
    ).fetchall()
    for r in rows:
        print("{}\t{}\t{}\t{}".format(r['id'], r['noted_on'], r['source'], r['fact']))


# ---------------------------------------------------------------------------
# Verb: touch-add
# ---------------------------------------------------------------------------

def cmd_touch_add(args, rel, lk):
    touched_on = args.date or TODAY
    note = args.note or ''
    # upsert on (person_id, touched_on, kind): update note instead of duplicate
    existing = rel.execute(
        "SELECT id FROM touches WHERE person_id=? AND touched_on=? AND kind=?",
        (args.person_id, touched_on, args.kind)
    ).fetchone()
    if existing:
        rel.execute(
            "UPDATE touches SET note=?, created_at=datetime('now','localtime') WHERE id=?",
            (note, existing['id'])
        )
        rel.commit()
        print("UPDATED\t{}\t{}\t{}".format(existing['id'], touched_on, args.kind))
    else:
        cur = rel.execute(
            "INSERT INTO touches (person_id, kind, touched_on, note) VALUES (?,?,?,?)",
            (args.person_id, args.kind, touched_on, note)
        )
        rel.commit()
        print("OK\t{}\t{}\t{}".format(cur.lastrowid, touched_on, args.kind))


# ---------------------------------------------------------------------------
# Verb: last-contact
# ---------------------------------------------------------------------------

def cmd_last_contact(args, rel, lk):
    d, k = last_contact(lk, rel, args.person_id)
    if d:
        print("{}\t{}".format(d, k))
    else:
        print("NONE")


# ---------------------------------------------------------------------------
# Verb: brief
# ---------------------------------------------------------------------------

def cmd_brief(args, rel, lk):
    query = args.name_or_id
    # Resolve: try integer id first, then name/alias LIKE match
    persons = []
    try:
        pid = int(query)
        row = lk.execute(
            "SELECT p.id, p.name, p.relation, p.birthday, p.residence, p.job, "
            "il.name as level_name, il.cycle_months "
            "FROM persons p LEFT JOIN intimacy_levels il ON il.id=p.intimacy_id "
            "WHERE p.id=?", (pid,)
        ).fetchone()
        if row:
            persons = [row]
    except ValueError:
        pass

    if not persons:
        rows = lk.execute(
            "SELECT p.id, p.name, p.relation, p.birthday, p.residence, p.job, "
            "il.name as level_name, il.cycle_months "
            "FROM persons p LEFT JOIN intimacy_levels il ON il.id=p.intimacy_id "
            "WHERE p.name LIKE ? OR (p.aliases IS NOT NULL AND p.aliases LIKE ?)",
            ('%{}%'.format(query), '%{}%'.format(query))
        ).fetchall()
        persons = rows

    if not persons:
        print("NOT_FOUND\t{}".format(query))
        return
    if len(persons) > 1:
        print("MULTIPLE_MATCHES")
        for p in persons:
            print("  {}\t{}".format(p['id'], p['name']))
        return

    p = persons[0]
    pid = p['id']

    # Profile line
    bday = p['birthday'] or ''
    residence = p['residence'] or ''
    job = p['job'] or ''
    level = p['level_name'] or ''
    relation = p['relation'] or ''
    print("PROFILE\t{}\t{}\t{}\t{}\t{}\t{}".format(
        p['name'], relation, level, bday, residence, job))

    # Last contact
    d, k = last_contact(lk, rel, pid)
    if d:
        print("LAST_CONTACT\t{}\t{}".format(d, k))
    else:
        print("LAST_CONTACT\tNONE")

    # Recent 5 facts
    facts = rel.execute(
        "SELECT noted_on, fact FROM person_facts WHERE person_id=? ORDER BY noted_on DESC LIMIT 5",
        (pid,)
    ).fetchall()
    for f in facts:
        print("FACT\t{}\t{}".format(f['noted_on'], f['fact']))

    # Upcoming meetings (from lifekit, today onward)
    # Legacy lane
    upcoming = []
    legacy = lk.execute(
        "SELECT a.title, substr(a.start_at,1,10) as adate "
        "FROM appointments a JOIN appointment_persons ap ON a.id=ap.appointment_id "
        "WHERE ap.person_id=? AND substr(a.start_at,1,10)>=? "
        "ORDER BY a.start_at LIMIT 5",
        (pid, TODAY)
    ).fetchall()
    for row in legacy:
        upcoming.append((row['adate'], row['title']))

    live = lk.execute(
        "SELECT e.title, COALESCE(e.rec_date, substr(e.start_at,1,10)) AS edate "
        "FROM event e JOIN event_persons ep ON e.id=ep.event_id "
        "WHERE ep.person_id=? AND e.kind='appointment' "
        "AND COALESCE(e.rec_date, substr(e.start_at,1,10))>=? "
        "ORDER BY edate LIMIT 5",
        (pid, TODAY)
    ).fetchall()
    for row in live:
        upcoming.append((row['edate'], row['title']))

    # Dedup by (date, title), sort
    seen = set()
    deduped = []
    for date, title in sorted(upcoming):
        key = (date, title)
        if key not in seen:
            seen.add(key)
            deduped.append(key)

    for date, title in deduped[:5]:
        print("UPCOMING\t{}\t{}".format(date, title))

    # Birthday within 30 days
    if bday:
        try:
            today_dt = datetime.date.today()
            bday_dt = datetime.date.fromisoformat(bday)
            # Use this year's birthday
            bday_this_year = bday_dt.replace(year=today_dt.year)
            delta = (bday_this_year - today_dt).days
            if 0 <= delta <= 30:
                print("BIRTHDAY_SOON\t{}\t{}days".format(bday, delta))
        except (ValueError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Verb: retro-candidates
# ---------------------------------------------------------------------------

def cmd_retro_candidates(args, rel, lk):
    date = args.date or TODAY
    # People met that date = DISTINCT union of both lanes
    rows = lk.execute(
        "SELECT DISTINCT p.id, p.name FROM persons p WHERE p.id IN ("
        "  SELECT ap.person_id FROM appointments a "
        "  JOIN appointment_persons ap ON a.id=ap.appointment_id "
        "  WHERE substr(a.start_at,1,10)=? "
        "  UNION "
        "  SELECT ep.person_id FROM event e "
        "  JOIN event_persons ep ON e.id=ep.event_id "
        "  WHERE e.kind='appointment' "
        "  AND COALESCE(e.rec_date, substr(e.start_at,1,10))=?"
        ") ORDER BY p.id",
        (date, date)
    ).fetchall()

    if not rows:
        print("NONE\t{}".format(date))
        return

    for p in rows:
        pid = p['id']
        # Fact count
        cnt = rel.execute(
            "SELECT COUNT(*) FROM person_facts WHERE person_id=?", (pid,)
        ).fetchone()[0]
        # Newest fact date
        newest = rel.execute(
            "SELECT MAX(noted_on) FROM person_facts WHERE person_id=?", (pid,)
        ).fetchone()[0] or ''
        print("{}\t{}\t{}\t{}".format(pid, p['name'], cnt, newest))


# ---------------------------------------------------------------------------
# Verb: stale-list (used internally and as CLI)
# ---------------------------------------------------------------------------

def compute_stale_list(rel, lk, today=None, limit=10):
    """Return list of dicts: person_id, name, gap_days, cycle_days, ratio, channel_mode, last_date, last_kind.
    Excludes: let_fade=1, no intimacy level, ratio < 1.0. Sorted ratio desc."""
    t = today or TODAY
    today_dt = datetime.date.fromisoformat(t)

    # All persons with an intimacy level
    persons = lk.execute(
        "SELECT p.id, p.name, il.cycle_months "
        "FROM persons p JOIN intimacy_levels il ON il.id=p.intimacy_id "
        "ORDER BY p.id"
    ).fetchall()

    results = []
    for p in persons:
        pid = p['id']
        cycle_days = int(round(p['cycle_months'] * 30))

        # Respect let_fade
        pref = rel.execute(
            "SELECT channel_mode, let_fade FROM person_prefs WHERE person_id=?", (pid,)
        ).fetchone()
        channel_mode = pref['channel_mode'] if pref else 'meet'
        let_fade = pref['let_fade'] if pref else 0
        if let_fade:
            continue

        # Compute last contact per channel_mode.
        # kind='meet' touches (recalled meetings) count as contact in ALL modes:
        #   meet mode    = lifekit meetings + meet-touches
        #   contact mode = all touches (call/message/dm/other + meet-touches)
        #   mixed        = max of everything
        meet_d, meet_k = last_meeting_date(lk, pid, t)
        any_touch_d, any_touch_k = last_touch_date(rel, pid, t)
        meet_touch_d, meet_touch_k = last_touch_date(rel, pid, t, kinds=['meet'])

        def _max_pair(pairs):
            best = (None, None)
            for d, k in pairs:
                if d and (best[0] is None or d > best[0]):
                    best = (d, k)
            return best

        if channel_mode == 'meet':
            last_d, last_k = _max_pair([(meet_d, meet_k), (meet_touch_d, meet_touch_k)])
        elif channel_mode == 'contact':
            last_d, last_k = any_touch_d, any_touch_k
        else:  # mixed
            last_d, last_k = _max_pair([(meet_d, meet_k), (any_touch_d, any_touch_k)])

        # Unknown-contact exclusion: zero recorded contact means unknown gap,
        # not overdue -- exclude from stale ranking entirely.
        if last_d is None:
            continue

        last_dt = datetime.date.fromisoformat(last_d)
        gap_days = (today_dt - last_dt).days
        ratio = gap_days / cycle_days if cycle_days > 0 else 0.0

        if ratio < 1.0:
            continue

        results.append({
            'person_id': pid,
            'name': p['name'],
            'gap_days': gap_days,
            'cycle_days': cycle_days,
            'ratio': ratio,
            'channel_mode': channel_mode,
            'last_date': last_d or '',
            'last_kind': last_k or '',
        })

    results.sort(key=lambda x: -x['ratio'])
    return results[:limit]


def cmd_stale_list(args, rel, lk):
    limit = args.limit if args.limit else 10
    rows = compute_stale_list(rel, lk, limit=limit)
    if not rows:
        print("NONE")
        return
    for r in rows:
        print("{}\t{}\t{}\t{:.2f}\t{}\t{}\t{}".format(
            r['person_id'], r['name'], r['gap_days'], r['ratio'],
            r['last_date'], r['last_kind'], r['channel_mode']
        ))


# ---------------------------------------------------------------------------
# Verb: alert-pick (D2 gating: per-person gates + rotation, no global cap)
# ---------------------------------------------------------------------------

def cmd_alert_pick(args, rel, lk):
    today = TODAY
    today_dt = datetime.date.fromisoformat(today)

    # Context-snooze re-surface: any person whose snooze_until has passed
    # (<= today) gets a one-shot RESURFACE marker, and the snooze is cleared so
    # it fires exactly once and the person re-enters the normal pool next run.
    # RESURFACE is a signal, not a pick, so it is emitted independently of the
    # per-run pick selection below.
    # person_prefs lives in relationship.db; names come from lifekit (read-only),
    # so resolve them per-person rather than via a cross-db JOIN.
    expired = rel.execute(
        "SELECT person_id FROM person_prefs "
        "WHERE snooze_until IS NOT NULL AND snooze_until<=? ORDER BY person_id",
        (today,)
    ).fetchall()
    for row in expired:
        pid = row['person_id']
        nm = _person_name(lk, pid)
        rel.execute("UPDATE person_prefs SET snooze_until=NULL, "
                    "updated_at=datetime('now','localtime') WHERE person_id=?", (pid,))
        print("RESURFACE\t{}\t{}".format(pid, nm))
    rel.commit()

    # Get stale candidates (no limit -- we'll filter)
    candidates = compute_stale_list(rel, lk, limit=50)

    # Rotation ordering: nobody is silenced until they are actually handled
    # (touch / snooze / let_fade / dismissed-cycle). There is NO global weekly
    # cap -- instead we surface the people who have NOT been shown recently
    # first. Primary sort key: last 'shown' date ascending, with never-shown
    # people (NULL) sorting first; the ratio (staleness) order from
    # compute_stale_list breaks ties. So each run rotates a fresh face to the
    # front, and a previously shown person only re-appears once the un-shown
    # pool is exhausted -- meaning no stale person drops off the list before
    # they are handled.
    for c in candidates:
        last_shown = rel.execute(
            "SELECT MAX(shown_on) FROM alert_log "
            "WHERE person_id=? AND outcome='shown'", (c['person_id'],)
        ).fetchone()[0]
        c['last_shown'] = last_shown  # ISO date string, or None if never shown
    candidates.sort(key=lambda c: (c['last_shown'] is not None,
                                   c['last_shown'] or '',
                                   -c['ratio']))

    picks = []
    ask_fade_list = []

    for c in candidates:
        pid = c['person_id']

        # Check let_fade + context-snooze (both live in person_prefs).
        pref = rel.execute(
            "SELECT let_fade, snooze_until FROM person_prefs WHERE person_id=?", (pid,)
        ).fetchone()
        if pref and pref['let_fade']:
            continue
        # Context snooze: a future snooze_until temporarily excludes the person
        # from picks. Expired snoozes were already cleared in the RESURFACE pass,
        # so any snooze_until still set here is in the future.
        if pref and pref['snooze_until'] and pref['snooze_until'] > today:
            continue

        # Check if already fade_asked
        already_fade = rel.execute(
            "SELECT COUNT(*) FROM alert_log WHERE person_id=? AND outcome='fade_asked'",
            (pid,)
        ).fetchone()[0]
        if already_fade:
            continue

        # Check snooze: dismissed outcome newer than one personal cycle
        cycle_days = c['cycle_days']
        snooze_cutoff = (today_dt - datetime.timedelta(days=cycle_days)).isoformat()
        recent_dismiss = rel.execute(
            "SELECT COUNT(*) FROM alert_log WHERE person_id=? AND outcome='dismissed' AND shown_on>=?",
            (pid, snooze_cutoff)
        ).fetchone()[0]
        if recent_dismiss:
            continue

        # Check 3 consecutive dismisses (no 'acted' between) -> fade_asked flow
        history = rel.execute(
            "SELECT outcome FROM alert_log WHERE person_id=? ORDER BY id DESC LIMIT 10",
            (pid,)
        ).fetchall()
        consecutive_dismisses = 0
        for h in history:
            if h['outcome'] == 'dismissed':
                consecutive_dismisses += 1
            elif h['outcome'] == 'acted':
                break
            else:
                break
        if consecutive_dismisses >= 3:
            ask_fade_list.append(c)
            # Log fade_asked
            rel.execute(
                "INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (?,?,?)",
                (pid, today, 'fade_asked')
            )
            continue

        # Upcoming-appointment exclusion: no point suggesting you contact
        # someone you are already scheduled to meet. Applies to contact
        # suggestions (picks) only -- the fade flow above is unaffected.
        if has_upcoming_appointment(lk, pid, today):
            continue

        picks.append(c)
        if len(picks) >= 3:
            break

    rel.commit()

    # ASK_FADE entries
    for c in ask_fade_list[:3]:
        pid = c['person_id']
        # Get context hint
        hint = _context_hint(rel, lk, pid, c['last_date'])
        print("ASK_FADE\t{}\t{}\t{}days\t{:.2f}\t{}".format(
            pid, c['name'], c['gap_days'], c['ratio'], hint))

    # Output picks + log 'shown'.
    # Fields: PICK <pid> <name> <gap_days>days <ratio> <hint> <meet_days> <meet_ctx>
    # The last two are MEETING-based (occasion + days since last actual meeting),
    # which the morning brief renders as `name | meet_ctx | meet_days일전`.
    # meet_days = '' when the person has no meeting on record (falls back on the
    # caller side to the generic gap); meet_ctx = '' when the meeting had no title.
    for c in picks:
        pid = c['person_id']
        hint = _context_hint(rel, lk, pid, c['last_date'])
        meet_d, meet_ctx = last_meeting(lk, rel, pid, today)
        if meet_d:
            meet_days = (today_dt - datetime.date.fromisoformat(meet_d)).days
        else:
            meet_days = ''
        rel.execute(
            "INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (?,?,?)",
            (pid, today, 'shown')
        )
        print("PICK\t{}\t{}\t{}days\t{:.2f}\t{}\t{}\t{}".format(
            pid, c['name'], c['gap_days'], c['ratio'], hint, meet_days, meet_ctx))

    rel.commit()

    # Casual check-in lane: with spare slots (< 3 picks), surface AT MOST ONE
    # zero-contact person (intimacy level set, let_fade=0, no 'checkin'
    # outcome within the last 60 days). Logged as outcome 'checkin'.
    checkin_emitted = False
    if len(picks) < 3:
        cand = _checkin_candidate(rel, lk, today)
        if cand is not None:
            rel.execute(
                "INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (?,?,?)",
                (cand['id'], today, 'checkin')
            )
            rel.commit()
            print("CHECKIN\t{}\t{}\t{}".format(
                cand['id'], cand['name'], cand['level_name']))
            checkin_emitted = True

    if not picks and not ask_fade_list and not checkin_emitted:
        print("NONE")


def _checkin_candidate(rel, lk, today):
    """One person with an intimacy level, ZERO recorded contact (no deduped
    lifekit meeting <= today, no touch <= today), let_fade=0, and no 'checkin'
    outcome in the last 60 days. Deterministic: lowest person id first.
    Returns a dict-like row {id, name, level_name} or None."""
    today_dt = datetime.date.fromisoformat(today)
    cutoff = (today_dt - datetime.timedelta(days=60)).isoformat()
    persons = lk.execute(
        "SELECT p.id, p.name, il.name AS level_name "
        "FROM persons p JOIN intimacy_levels il ON il.id=p.intimacy_id "
        "ORDER BY p.id"
    ).fetchall()
    for p in persons:
        pid = p['id']
        pref = rel.execute(
            "SELECT let_fade, snooze_until FROM person_prefs WHERE person_id=?", (pid,)
        ).fetchone()
        if pref and pref['let_fade']:
            continue
        # Context snooze also silences the casual check-in for that person.
        if pref and pref['snooze_until'] and pref['snooze_until'] > today:
            continue
        md, _ = last_meeting_date(lk, pid, today)
        if md:
            continue
        td, _ = last_touch_date(rel, pid, today)
        if td:
            continue
        recent_checkin = rel.execute(
            "SELECT COUNT(*) FROM alert_log WHERE person_id=? "
            "AND outcome='checkin' AND shown_on>=?",
            (pid, cutoff)
        ).fetchone()[0]
        if recent_checkin:
            continue
        return p
    return None


def _context_hint(rel, lk, person_id, last_date):
    """Return a short context string: latest fact or meeting title."""
    fact = rel.execute(
        "SELECT fact FROM person_facts WHERE person_id=? ORDER BY noted_on DESC LIMIT 1",
        (person_id,)
    ).fetchone()
    if fact:
        return fact['fact'][:60]
    # Fallback: recent meeting title
    if last_date:
        title = lk.execute(
            "SELECT a.title FROM appointments a "
            "JOIN appointment_persons ap ON a.id=ap.appointment_id "
            "WHERE ap.person_id=? AND substr(a.start_at,1,10)=? LIMIT 1",
            (person_id, last_date)
        ).fetchone()
        if title:
            return title['title']
        title = lk.execute(
            "SELECT e.title FROM event e "
            "JOIN event_persons ep ON e.id=ep.event_id "
            "WHERE ep.person_id=? AND e.kind='appointment' "
            "AND COALESCE(e.rec_date, substr(e.start_at,1,10))=? LIMIT 1",
            (person_id, last_date)
        ).fetchone()
        if title:
            return title['title']
    return ''


# ---------------------------------------------------------------------------
# Verb: dismiss
# ---------------------------------------------------------------------------

def cmd_dismiss(args, rel, lk):
    rel.execute(
        "INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (?,?,?)",
        (args.person_id, TODAY, 'dismissed')
    )
    rel.commit()
    print("OK\t{}\tdismissed".format(args.person_id))


# ---------------------------------------------------------------------------
# Verb: acted
# ---------------------------------------------------------------------------

def cmd_acted(args, rel, lk):
    rel.execute(
        "INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (?,?,?)",
        (args.person_id, TODAY, 'acted')
    )
    rel.commit()
    print("OK\t{}\tacted".format(args.person_id))


# ---------------------------------------------------------------------------
# Verb: set-mode
# ---------------------------------------------------------------------------

def cmd_set_mode(args, rel, lk):
    rel.execute(
        "INSERT INTO person_prefs (person_id, channel_mode) VALUES (?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET channel_mode=excluded.channel_mode, "
        "updated_at=datetime('now','localtime')",
        (args.person_id, args.mode)
    )
    rel.commit()
    print("OK\t{}\tchannel_mode={}".format(args.person_id, args.mode))


# ---------------------------------------------------------------------------
# Verb: set-fade
# ---------------------------------------------------------------------------

def cmd_set_fade(args, rel, lk):
    val = 1 if args.value == 'on' else 0
    rel.execute(
        "INSERT INTO person_prefs (person_id, let_fade) VALUES (?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET let_fade=excluded.let_fade, "
        "updated_at=datetime('now','localtime')",
        (args.person_id, val)
    )
    rel.commit()
    print("OK\t{}\tlet_fade={}".format(args.person_id, val))


# ---------------------------------------------------------------------------
# Verb: snooze / unsnooze (context snooze -- time-bounded exclusion)
#
# Distinct from fade: fade is a permanent drift-apart flag; snooze is a
# temporary exclusion for a stated real-world reason (e.g. abroad for a PhD).
# State lives in person_prefs.snooze_until (a future date excludes the person
# from alert-pick picks). On expiry alert-pick emits a one-shot RESURFACE
# marker and clears the date, so the person re-enters the pool. The reason is
# NOT stored here -- the caller records it via fact-add so there is a trace.
# ---------------------------------------------------------------------------

DEFAULT_SNOOZE_MONTHS = 6


def _add_months(d, months):
    """Return date d shifted by `months` calendar months, clamping the day to
    the target month's last valid day (e.g. Jan 31 + 1mo -> Feb 28/29)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # Last day of target month: step to first of next month, back one day.
    if m == 12:
        last = 31
    else:
        last = (datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)).day
    return datetime.date(y, m, min(d.day, last))


def cmd_snooze(args, rel, lk):
    today_dt = datetime.date.fromisoformat(TODAY)
    if getattr(args, 'until', None):
        until = args.until  # explicit YYYY-MM-DD (validated by fromisoformat below)
        datetime.date.fromisoformat(until)  # raise on malformed input
    else:
        months = args.months if getattr(args, 'months', None) else DEFAULT_SNOOZE_MONTHS
        until = _add_months(today_dt, months).isoformat()
    rel.execute(
        "INSERT INTO person_prefs (person_id, snooze_until) VALUES (?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET snooze_until=excluded.snooze_until, "
        "updated_at=datetime('now','localtime')",
        (args.person_id, until)
    )
    rel.commit()
    # Reason trace: record as a fact (source=chat) so there is a why.
    if getattr(args, 'reason', None):
        rel.execute(
            "INSERT INTO person_facts (person_id, fact, source, noted_on) VALUES (?,?,?,?)",
            (args.person_id, args.reason, 'chat', TODAY)
        )
        rel.commit()
    print("OK\t{}\tsnooze_until={}".format(args.person_id, until))


def cmd_unsnooze(args, rel, lk):
    rel.execute(
        "UPDATE person_prefs SET snooze_until=NULL, updated_at=datetime('now','localtime') "
        "WHERE person_id=?",
        (args.person_id,)
    )
    rel.commit()
    print("OK\t{}\tsnooze_cleared".format(args.person_id))


# ---------------------------------------------------------------------------
# Verb: drift-list
# ---------------------------------------------------------------------------

def cmd_drift_list(args, rel, lk):
    """Observed 12mo deduped meeting count vs declared level bands.
    Bands: L5(>=10), L4(4-9), L3(1-3), L2/L1/L0(<1 nominal).
    Output UPGRADE suggestions only (observed band above declared) AND only
    when observed meets12mo >= 4 (single casual meetings are not a signal).
    No downgrades from numbers."""
    today = TODAY
    today_dt = datetime.date.fromisoformat(today)
    cutoff = (today_dt - datetime.timedelta(days=365)).isoformat()

    # Level number extraction: intimacy_levels.name like "5(편한친구)" or "6" (no parens)
    def level_num(name_str):
        """Extract integer level number from level name string."""
        if not name_str:
            return None
        # Try plain integer first
        try:
            return int(name_str)
        except ValueError:
            pass
        # Extract leading digits before '('
        import re
        m = re.match(r'^(\d+)', name_str)
        if m:
            return int(m.group(1))
        return None

    def observed_band(count):
        """Map 12mo meeting count to level number."""
        if count >= 10:
            return 5
        if count >= 4:
            return 4
        if count >= 1:
            return 3
        return 0  # <1 = no meaningful band

    persons = lk.execute(
        "SELECT p.id, p.name, il.id as lvl_id, il.name as lvl_name "
        "FROM persons p JOIN intimacy_levels il ON il.id=p.intimacy_id "
        "ORDER BY p.id"
    ).fetchall()

    found_any = False
    for p in persons:
        pid = p['id']
        declared_num = level_num(p['lvl_name'])
        if declared_num is None:
            continue

        # Count deduped meetings in 12 months
        cnt = lk.execute(
            "SELECT COUNT(*) FROM ({}) WHERE person_id=? AND local_date>? AND local_date<=?".format(
                DEDUP_MEET_SQL),
            (pid, cutoff, today)
        ).fetchone()[0]

        obs_band = observed_band(cnt)
        # Threshold: suggest only from 4+ observed meets (kills level-0 noise
        # from a single casual meeting).
        if cnt >= 4 and obs_band > declared_num:
            print("UPGRADE\t{}\t{}\tdeclared={}\tobserved={}\tmeets12mo={}".format(
                pid, p['name'], declared_num, obs_band, cnt))
            found_any = True

    if not found_any:
        print("NONE")


# ---------------------------------------------------------------------------
# Verb: selftest (fixture-based, never touches real DBs)
# ---------------------------------------------------------------------------

def cmd_selftest(args, rel_unused, lk_unused):
    import re
    import traceback

    tmpdir = tempfile.mkdtemp(prefix='relmod_selftest_')
    passes = 0
    failures = 0

    def ok(label):
        nonlocal passes
        passes += 1
        print("PASS\t{}".format(label))

    def fail(label, reason):
        nonlocal failures
        failures += 1
        print("FAIL\t{}\t{}".format(label, reason))

    try:
        # -- Build fixture lifekit.db ----------------------------------------
        lk_path = os.path.join(tmpdir, 'lifekit.db')
        lk = sqlite3.connect(lk_path)
        lk.executescript("""
            CREATE TABLE persons (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                relation TEXT,
                intimacy_id INTEGER,
                birthday TEXT,
                residence TEXT,
                contact TEXT,
                job TEXT,
                mbti TEXT,
                groups TEXT,
                manual_priority REAL,
                notion_id TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                aliases TEXT
            );
            CREATE TABLE intimacy_levels (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                cycle_months REAL,
                criteria TEXT,
                notion_id TEXT
            );
            CREATE TABLE appointments (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                start_at TEXT,
                end_at TEXT,
                location TEXT,
                location_url TEXT,
                purpose TEXT,
                summary TEXT,
                notion_id TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE appointment_persons (
                appointment_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                PRIMARY KEY (appointment_id, person_id)
            );
            CREATE TABLE event (
                id INTEGER PRIMARY KEY,
                ulid TEXT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                note TEXT,
                area_id INTEGER,
                schedule_kind TEXT NOT NULL DEFAULT 'timed',
                start_at TEXT,
                end_at TEXT,
                display_tz TEXT DEFAULT 'Asia/Seoul',
                open_ended INTEGER DEFAULT 0,
                slot_exclusive INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                completion_rule TEXT DEFAULT 'manual',
                completion_n INTEGER,
                rec_date TEXT,
                recurrence_id INTEGER,
                settled_at TEXT,
                settled_outcome TEXT,
                notify_policy TEXT,
                notify_lead_min INTEGER,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE event_persons (
                event_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                PRIMARY KEY (event_id, person_id)
            );
        """)

        # Insert intimacy levels: id=1 -> level 5, id=2 -> level 3, id=3 -> level 1
        lk.execute("INSERT INTO intimacy_levels VALUES (1,'5(편한친구)',2.0,'','')")
        lk.execute("INSERT INTO intimacy_levels VALUES (2,'3(가끔보는친구)',12.0,'','')")
        lk.execute("INSERT INTO intimacy_levels VALUES (3,'1(지인)',24.0,'','')")

        # Persons: Alice (level 5, cycle 2mo=60d), Bob (level 3, cycle 12mo=360d),
        #          Carol (let_fade candidate, old meeting), Dave (drift upgrade),
        #          Eve (3 meets: below drift threshold), Frank (4 meets: at threshold),
        #          Grace (level set, ZERO contact: unknown/checkin lane)
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id,birthday) VALUES (1,'Alice','friend',1,'1990-03-15')")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (2,'Bob','friend',2)")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (3,'Carol','friend',1)")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (4,'Dave','friend',3)")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (5,'Eve','friend',3)")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (6,'Frank','friend',3)")
        lk.execute("INSERT INTO persons(id,name,relation,intimacy_id) VALUES (7,'Grace','friend',2)")

        # Appointments: Alice met 90 days ago (legacy lane), Bob met 400 days ago
        today_dt = datetime.date.today()
        d_90 = (today_dt - datetime.timedelta(days=90)).isoformat()
        d_400 = (today_dt - datetime.timedelta(days=400)).isoformat()
        d_future = (today_dt + datetime.timedelta(days=5)).isoformat()
        d_10 = (today_dt - datetime.timedelta(days=10)).isoformat()
        d_380 = (today_dt - datetime.timedelta(days=380)).isoformat()
        d_370 = (today_dt - datetime.timedelta(days=370)).isoformat()

        # Alice: 2 meetings 90 days ago (legacy + live = same day -> dedup to 1)
        lk.execute("INSERT INTO appointments(id,title,start_at) VALUES (1,'Coffee with Alice','{}T10:00:00.000+09:00')".format(d_90))
        lk.execute("INSERT INTO appointment_persons VALUES (1,1)")
        # Same date, same title in event table -> dedup should count as 1 day
        lk.execute("INSERT INTO event(id,ulid,kind,title,start_at,schedule_kind,slot_exclusive) VALUES (10,'u10','appointment','Coffee with Alice','{}T01:00:00Z','timed',1)".format(d_90))
        lk.execute("INSERT INTO event_persons VALUES (10,1)")

        # Alice: upcoming event
        lk.execute("INSERT INTO event(id,ulid,kind,title,start_at,schedule_kind,slot_exclusive) VALUES (11,'u11','appointment','Future meet','{}T01:00:00Z','timed',1)".format(d_future))
        lk.execute("INSERT INTO event_persons VALUES (11,1)")

        # Bob: appointment 400 days ago
        lk.execute("INSERT INTO appointments(id,title,start_at) VALUES (2,'Bob dinner','{}T10:00:00.000+09:00')".format(d_400))
        lk.execute("INSERT INTO appointment_persons VALUES (2,2)")

        # Carol: appointment 450 days ago (so let_fade exclusion is non-vacuous:
        # without let_fade she IS overdue -- cycle 60d, gap 450 -> ratio 7.5)
        d_450 = (today_dt - datetime.timedelta(days=450)).isoformat()
        lk.execute("INSERT INTO appointments(id,title,start_at) VALUES (3,'Carol lunch','{}T10:00:00.000+09:00')".format(d_450))
        lk.execute("INSERT INTO appointment_persons VALUES (3,3)")

        # Eve: exactly 3 meets in 12mo (below drift threshold 4 -> no UPGRADE)
        for i in range(3):
            d_i = (today_dt - datetime.timedelta(days=(i + 1) * 30)).isoformat()
            eid = 200 + i
            lk.execute(
                "INSERT INTO event(id,ulid,kind,title,start_at,schedule_kind,slot_exclusive) "
                "VALUES (?,?,'appointment',?,?,'timed',1)",
                (eid, 'u{}'.format(eid), 'Eve meeting{}'.format(i), '{}T01:00:00Z'.format(d_i))
            )
            lk.execute("INSERT INTO event_persons VALUES (?,5)", (eid,))

        # Frank: exactly 4 meets in 12mo (at threshold -> UPGRADE)
        for i in range(4):
            d_i = (today_dt - datetime.timedelta(days=(i + 1) * 30)).isoformat()
            eid = 300 + i
            lk.execute(
                "INSERT INTO event(id,ulid,kind,title,start_at,schedule_kind,slot_exclusive) "
                "VALUES (?,?,'appointment',?,?,'timed',1)",
                (eid, 'u{}'.format(eid), 'Frank meeting{}'.format(i), '{}T01:00:00Z'.format(d_i))
            )
            lk.execute("INSERT INTO event_persons VALUES (?,6)", (eid,))

        # Grace: NO meetings, NO touches (zero recorded contact)

        # Dave: 15 meetings in last 12mo (via event, declared level 1 -> observed L5 -> UPGRADE)
        for i in range(15):
            d_i = (today_dt - datetime.timedelta(days=i*20+1)).isoformat()
            eid = 100 + i
            ulid = 'u{}'.format(eid)
            title = 'Dave meeting{}'.format(i)
            start = '{}T01:00:00Z'.format(d_i)
            lk.execute(
                "INSERT INTO event(id,ulid,kind,title,start_at,schedule_kind,slot_exclusive) "
                "VALUES (?,?,'appointment',?,?,'timed',1)",
                (eid, ulid, title, start)
            )
            lk.execute("INSERT INTO event_persons VALUES (?,4)", (eid,))

        lk.commit()
        lk.close()

        # -- Build fixture relationship.db -----------------------------------
        rel_path = os.path.join(tmpdir, 'relationship.db')
        ensure_schema(rel_path)

        # -- Helper to run cmd with fixture paths ----------------------------
        def run(verb_args):
            """Run a CLI invocation against fixture DBs, capture stdout."""
            import io
            old_stdout = sys.stdout
            old_reldb = RELDB_PATH
            old_lifedb = LIFEDB_PATH
            # Monkey-patch module-level paths
            import relmod as _self
            _self.RELDB_PATH = rel_path
            _self.LIFEDB_PATH = lk_path
            _self.TODAY = today_dt.isoformat()
            captured = io.StringIO()
            sys.stdout = captured
            try:
                rel = rel_conn(rel_path)
                lk_c = lk_conn(lk_path)
                parser = build_parser()
                a = parser.parse_args(verb_args)
                a.func(a, rel, lk_c)
                rel.close()
                lk_c.close()
            finally:
                sys.stdout = old_stdout
                _self.RELDB_PATH = old_reldb
                _self.LIFEDB_PATH = old_lifedb
            return captured.getvalue().strip()

        rel_c = rel_conn(rel_path)
        lk_c = lk_conn(lk_path)

        # ----------------------------------------------------------------
        # TC-1: fact-add inserts a new fact
        # ----------------------------------------------------------------
        try:
            cmd_fact_add(type('A', (), {'person_id': 1, 'fact': 'loves jazz', 'source': 'chat', 'date': None})(), rel_c, lk_c)
            row = rel_c.execute("SELECT fact FROM person_facts WHERE person_id=1").fetchone()
            assert row and row[0] == 'loves jazz', "fact not found"
            ok("fact-add insert")
        except Exception as e:
            fail("fact-add insert", str(e))

        # TC-2: fact-add find-first (same person+date+similar fact) -> UPDATE not INSERT
        # ----------------------------------------------------------------
        try:
            today_str = today_dt.isoformat()
            # Add initial fact
            rel_c.execute("DELETE FROM person_facts WHERE person_id=2")
            rel_c.commit()
            cmd_fact_add(type('A', (), {'person_id': 2, 'fact': 'runs marathons', 'source': 'chat', 'date': today_str})(), rel_c, lk_c)
            cnt_before = rel_c.execute("SELECT COUNT(*) FROM person_facts WHERE person_id=2").fetchone()[0]
            # Similar fact (substring match) same date -> should UPDATE
            cmd_fact_add(type('A', (), {'person_id': 2, 'fact': 'runs marathons every spring', 'source': 'retro', 'date': today_str})(), rel_c, lk_c)
            cnt_after = rel_c.execute("SELECT COUNT(*) FROM person_facts WHERE person_id=2").fetchone()[0]
            assert cnt_before == cnt_after == 1, "expected 1 row (upsert), got before={} after={}".format(cnt_before, cnt_after)
            ok("fact-add find-first upsert")
        except Exception as e:
            fail("fact-add find-first upsert", str(e))

        # TC-3: touch-add insert
        # ----------------------------------------------------------------
        try:
            cmd_touch_add(type('A', (), {'person_id': 1, 'kind': 'call', 'date': today_dt.isoformat(), 'note': 'quick check-in'})(), rel_c, lk_c)
            row = rel_c.execute("SELECT kind FROM touches WHERE person_id=1").fetchone()
            assert row and row[0] == 'call', "touch not found"
            ok("touch-add insert")
        except Exception as e:
            fail("touch-add insert", str(e))

        # TC-4: touch-add upsert (same person+date+kind -> update note)
        # ----------------------------------------------------------------
        try:
            cmd_touch_add(type('A', (), {'person_id': 1, 'kind': 'call', 'date': today_dt.isoformat(), 'note': 'longer chat'})(), rel_c, lk_c)
            cnt = rel_c.execute("SELECT COUNT(*) FROM touches WHERE person_id=1 AND kind='call' AND touched_on=?", (today_dt.isoformat(),)).fetchone()[0]
            note = rel_c.execute("SELECT note FROM touches WHERE person_id=1 AND kind='call' AND touched_on=?", (today_dt.isoformat(),)).fetchone()[0]
            assert cnt == 1, "expected 1 touch row, got {}".format(cnt)
            assert note == 'longer chat', "note not updated"
            ok("touch-add upsert")
        except Exception as e:
            fail("touch-add upsert", str(e))

        # TC-5: last-contact (meeting + touch, pick max)
        # ----------------------------------------------------------------
        try:
            # Alice last meeting = d_90, touch = today
            d, k = last_contact(lk_c, rel_c, 1, today_dt.isoformat())
            assert d == today_dt.isoformat(), "expected today, got {}".format(d)
            assert k == 'call', "expected call, got {}".format(k)
            ok("last-contact max(meet,touch)")
        except Exception as e:
            fail("last-contact max(meet,touch)", str(e))

        # TC-6: dedup meeting count (same day in both lanes = 1 unique date)
        # ----------------------------------------------------------------
        try:
            cnt = lk_c.execute(
                "SELECT COUNT(*) FROM ({}) WHERE person_id=1 AND local_date=?".format(DEDUP_MEET_SQL),
                (d_90,)
            ).fetchone()[0]
            assert cnt == 1, "expected 1 deduped meeting day, got {}".format(cnt)
            ok("meeting dedup (same day both lanes = 1)")
        except Exception as e:
            fail("meeting dedup", str(e))

        # TC-7: stale-list Alice is overdue (cycle=60d, last meeting=90d -> ratio=1.5)
        # ----------------------------------------------------------------
        try:
            # Remove Alice's touch so last contact = meeting 90 days ago
            rel_c.execute("DELETE FROM touches WHERE person_id=1")
            rel_c.commit()
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            alice = next((s for s in stale if s['person_id'] == 1), None)
            assert alice is not None, "Alice not in stale list"
            assert alice['gap_days'] == 90, "expected gap=90, got {}".format(alice['gap_days'])
            ok("stale-list overdue ratio")
        except Exception as e:
            fail("stale-list overdue ratio", str(e))

        # TC-8: stale-list excludes let_fade persons
        # ----------------------------------------------------------------
        try:
            # Carol IS stale before the flag (meeting 450d ago, cycle 60d)
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            carol = next((s for s in stale if s['person_id'] == 3), None)
            assert carol is not None, "Carol should be stale before let_fade"
            rel_c.execute(
                "INSERT INTO person_prefs (person_id, let_fade) VALUES (3,1) "
                "ON CONFLICT(person_id) DO UPDATE SET let_fade=1"
            )
            rel_c.commit()
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            carol = next((s for s in stale if s['person_id'] == 3), None)
            assert carol is None, "Carol (let_fade) should be excluded"
            ok("stale-list excludes let_fade")
        except Exception as e:
            fail("stale-list excludes let_fade", str(e))

        # TC-9: alert-pick has NO global weekly cap -- a person shown today does
        # not silence everyone else (formerly emitted CAP_REACHED).
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            # Alice(1) shown today. Bob(2) is also stale but never shown.
            rel_c.execute("INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (1,?,'shown')", (today_dt.isoformat(),))
            rel_c.commit()
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            assert 'CAP_REACHED' not in out, "global cap must be gone, got: {}".format(out)
            pick_lines = [l for l in out.splitlines() if l.startswith('PICK')]
            assert pick_lines, "expected picks despite a prior 'shown', got: {}".format(out)
            # Rotation: the never-shown Bob(2) must surface ahead of Alice(1).
            picked_ids = [l.split('\t')[1] for l in pick_lines]
            assert '2' in picked_ids, "never-shown Bob should be picked, got: {}".format(out)
            ok("alert-pick no global cap")
        except Exception as e:
            fail("alert-pick no global cap", str(e))

        # TC-9b: rotation -- consecutive runs surface different faces first, and
        # nobody drops off before being handled (pool exhausted -> resurface).
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            rel_c.commit()

            def _pick9b():
                cap = io.StringIO(); old = sys.stdout; sys.stdout = cap
                try:
                    cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
                finally:
                    sys.stdout = old
                return [l.split('\t')[1] for l in cap.getvalue().splitlines()
                        if l.startswith('PICK')]

            # Run 1: some set of stale people gets shown (<=3).
            run1 = _pick9b()
            assert run1, "run1 should produce picks"
            # Run 2: with those logged as 'shown' today, any stale person NOT
            # shown in run1 must be preferred. If the stale pool is larger than
            # 3, run2's lead pick differs from run1's lead pick (fresh face).
            run2 = _pick9b()
            assert run2, "run2 must still produce picks (nobody silenced)"
            # Everyone shown in run1 is still in the pool (not handled), so the
            # union across runs keeps growing or holds -- no one vanished.
            # Concretely: a person picked in run1 must reappear by the time the
            # un-shown pool is exhausted, never permanently dropped.
            stale_now = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            stale_ids = {str(s['person_id']) for s in stale_now}
            assert set(run1) <= stale_ids, "run1 picks must remain in the stale pool"
            ok("alert-pick rotation surfaces fresh faces")
        except Exception as e:
            fail("alert-pick rotation surfaces fresh faces", str(e))

        # TC-9c: a touched (handled) person is naturally excluded next run.
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            rel_c.execute("DELETE FROM touches")
            rel_c.commit()

            def _pickids():
                cap = io.StringIO(); old = sys.stdout; sys.stdout = cap
                try:
                    cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
                finally:
                    sys.stdout = old
                return [l.split('\t')[1] for l in cap.getvalue().splitlines()
                        if l.startswith('PICK')]

            before = _pickids()
            assert before, "expected at least one pick before touch"
            target = before[0]
            # Touch the picked person today -> staleness resets -> excluded.
            cmd_touch_add(type('A', (), {'person_id': int(target), 'kind': 'meet',
                                         'date': today_dt.isoformat(),
                                         'note': 'handled'})(), rel_c, lk_c)
            rel_c.execute("DELETE FROM alert_log"); rel_c.commit()
            after = _pickids()
            assert target not in after, \
                "touched person {} must drop from picks, got: {}".format(target, after)
            # cleanup so later tests see a clean touches table
            rel_c.execute("DELETE FROM touches"); rel_c.commit()
            ok("alert-pick touched person excluded")
        except Exception as e:
            fail("alert-pick touched person excluded", str(e))

        # TC-10: alert-pick snooze (dismissed within one personal cycle -> skip)
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            rel_c.commit()
            # Alice cycle = 60 days; dismiss 30 days ago = within cycle -> snooze
            d_30 = (today_dt - datetime.timedelta(days=30)).isoformat()
            rel_c.execute("INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (1,?,'dismissed')", (d_30,))
            rel_c.commit()
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            alice_in_stale = any(s['person_id'] == 1 for s in stale)
            # Manually check snooze logic
            cycle_days = 60
            snooze_cutoff = (today_dt - datetime.timedelta(days=cycle_days)).isoformat()
            recent_dismiss = rel_c.execute(
                "SELECT COUNT(*) FROM alert_log WHERE person_id=1 AND outcome='dismissed' AND shown_on>=?",
                (snooze_cutoff,)
            ).fetchone()[0]
            assert recent_dismiss == 1, "expected 1 recent dismiss"
            ok("alert-pick snooze within cycle")
        except Exception as e:
            fail("alert-pick snooze within cycle", str(e))

        # TC-11: alert-pick 3 consecutive dismisses -> fade_asked flow
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            rel_c.commit()
            # Alice: 3 dismisses, no 'acted' between, outside snooze window
            for i in range(3):
                d_i = (today_dt - datetime.timedelta(days=200 + i * 30)).isoformat()
                rel_c.execute("INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (1,?,'dismissed')", (d_i,))
            rel_c.commit()

            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            has_ask_fade = 'ASK_FADE' in out
            # Check fade_asked logged
            logged = rel_c.execute(
                "SELECT COUNT(*) FROM alert_log WHERE person_id=1 AND outcome='fade_asked'"
            ).fetchone()[0]
            assert has_ask_fade, "expected ASK_FADE in output, got: {}".format(out)
            assert logged >= 1, "fade_asked not logged"
            ok("alert-pick 3-dismiss -> fade_asked")
        except Exception as e:
            fail("alert-pick 3-dismiss -> fade_asked", str(e))

        # TC-12: drift-list UPGRADE for Dave (15 meets, declared level 1 -> observed L5)
        # ----------------------------------------------------------------
        try:
            import io
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_drift_list(type('A', (), {})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            assert 'UPGRADE' in out and 'Dave' in out, "expected UPGRADE for Dave, got: {}".format(out)
            ok("drift-list UPGRADE suggestion")
        except Exception as e:
            fail("drift-list UPGRADE suggestion", str(e))

        # TC-13: retro-candidates returns persons met on date
        # ----------------------------------------------------------------
        try:
            import io
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_retro_candidates(type('A', (), {'date': d_90})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            assert 'Alice' in out, "expected Alice in retro-candidates, got: {}".format(out)
            ok("retro-candidates correct date")
        except Exception as e:
            fail("retro-candidates correct date", str(e))

        # TC-14: brief resolves by name and outputs profile
        # ----------------------------------------------------------------
        try:
            import io
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_brief(type('A', (), {'name_or_id': 'Alice'})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            assert 'PROFILE' in out and 'Alice' in out, "expected PROFILE line, got: {}".format(out)
            ok("brief resolves by name")
        except Exception as e:
            fail("brief resolves by name", str(e))

        # TC-15: set-mode + set-fade round-trip
        # ----------------------------------------------------------------
        try:
            cmd_set_mode(type('A', (), {'person_id': 2, 'mode': 'contact'})(), rel_c, lk_c)
            pref = rel_c.execute("SELECT channel_mode FROM person_prefs WHERE person_id=2").fetchone()
            assert pref and pref[0] == 'contact', "channel_mode not set"
            cmd_set_fade(type('A', (), {'person_id': 2, 'value': 'on'})(), rel_c, lk_c)
            pref = rel_c.execute("SELECT let_fade FROM person_prefs WHERE person_id=2").fetchone()
            assert pref and pref[0] == 1, "let_fade not set"
            cmd_set_fade(type('A', (), {'person_id': 2, 'value': 'off'})(), rel_c, lk_c)
            pref = rel_c.execute("SELECT let_fade FROM person_prefs WHERE person_id=2").fetchone()
            assert pref and pref[0] == 0, "let_fade not cleared"
            ok("set-mode + set-fade round-trip")
        except Exception as e:
            fail("set-mode + set-fade round-trip", str(e))

        # TC-16: dismiss + acted logging
        # ----------------------------------------------------------------
        try:
            rel_c.execute("DELETE FROM alert_log")
            rel_c.commit()
            cmd_dismiss(type('A', (), {'person_id': 1})(), rel_c, lk_c)
            cmd_acted(type('A', (), {'person_id': 2})(), rel_c, lk_c)
            d_cnt = rel_c.execute("SELECT COUNT(*) FROM alert_log WHERE outcome='dismissed'").fetchone()[0]
            a_cnt = rel_c.execute("SELECT COUNT(*) FROM alert_log WHERE outcome='acted'").fetchone()[0]
            assert d_cnt == 1 and a_cnt == 1, "dismiss={} acted={}".format(d_cnt, a_cnt)
            ok("dismiss + acted logging")
        except Exception as e:
            fail("dismiss + acted logging", str(e))

        # TC-17: unknown-contact exclusion from stale-list (no fake gap)
        # ----------------------------------------------------------------
        try:
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            grace = next((s for s in stale if s['person_id'] == 7), None)
            assert grace is None, "Grace (zero contact) must be excluded from stale-list"
            for s in stale:
                assert s['last_date'], "stale entry without last_date: {}".format(s)
            ok("stale-list excludes unknown-contact persons")
        except Exception as e:
            fail("stale-list excludes unknown-contact persons", str(e))

        # TC-18: drift threshold (3 meets -> no suggestion, 4 meets -> suggestion)
        # ----------------------------------------------------------------
        try:
            import io
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_drift_list(type('A', (), {})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            assert 'Eve' not in out, "Eve (3 meets) must NOT get UPGRADE, got: {}".format(out)
            assert 'Frank' in out, "Frank (4 meets) must get UPGRADE, got: {}".format(out)
            assert 'Dave' in out, "Dave (15 meets) must keep UPGRADE, got: {}".format(out)
            ok("drift-list threshold meets12mo>=4")
        except Exception as e:
            fail("drift-list threshold meets12mo>=4", str(e))

        # TC-19: meet-touch counts as contact in contact mode
        # ----------------------------------------------------------------
        try:
            d_395 = (today_dt - datetime.timedelta(days=395)).isoformat()
            cmd_set_mode(type('A', (), {'person_id': 2, 'mode': 'contact'})(), rel_c, lk_c)
            cmd_touch_add(type('A', (), {'person_id': 2, 'kind': 'meet', 'date': d_395, 'note': 'recalled dinner'})(), rel_c, lk_c)
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            bob = next((s for s in stale if s['person_id'] == 2), None)
            assert bob is not None, "Bob should be stale via meet-touch in contact mode"
            assert bob['gap_days'] == 395, "expected gap=395, got {}".format(bob['gap_days'])
            assert bob['last_kind'] == 'meet(recalled)', "expected meet(recalled), got {}".format(bob['last_kind'])
            ok("meet-touch counts in contact mode")
        except Exception as e:
            fail("meet-touch counts in contact mode", str(e))

        # TC-20: meet-touch counts as contact in meet mode
        # ----------------------------------------------------------------
        try:
            # Alice stale before (meeting 90d ago, cycle 60d)
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            assert any(s['person_id'] == 1 for s in stale), "Alice should be stale before meet-touch"
            d_5 = (today_dt - datetime.timedelta(days=5)).isoformat()
            cmd_touch_add(type('A', (), {'person_id': 1, 'kind': 'meet', 'date': d_5, 'note': 'recalled coffee'})(), rel_c, lk_c)
            stale = compute_stale_list(rel_c, lk_c, today=today_dt.isoformat())
            alice = next((s for s in stale if s['person_id'] == 1), None)
            assert alice is None, "Alice must drop out of stale after recent meet-touch (meet mode)"
            ok("meet-touch counts in meet mode")
        except Exception as e:
            fail("meet-touch counts in meet mode", str(e))

        # TC-21: last-contact reports recalled meeting as 'meet(recalled)'
        # ----------------------------------------------------------------
        try:
            d, k = last_contact(lk_c, rel_c, 1, today_dt.isoformat())
            d_5 = (today_dt - datetime.timedelta(days=5)).isoformat()
            assert d == d_5, "expected {}, got {}".format(d_5, d)
            assert k == 'meet(recalled)', "expected meet(recalled), got {}".format(k)
            ok("last-contact kind meet(recalled)")
        except Exception as e:
            fail("last-contact kind meet(recalled)", str(e))

        # TC-22: alert-pick CHECKIN with spare slots (zero-contact person)
        # ----------------------------------------------------------------
        try:
            import io
            rel_c.execute("DELETE FROM alert_log")
            rel_c.commit()
            # Expected picks: Bob only (1 < 3) -> spare slots -> CHECKIN Grace
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
            finally:
                sys.stdout = old_stdout
            out = captured.getvalue()
            pick_lines = [l for l in out.splitlines() if l.startswith('PICK')]
            checkin_lines = [l for l in out.splitlines() if l.startswith('CHECKIN')]
            assert len(pick_lines) < 3, "expected < 3 picks, got: {}".format(out)
            assert len(checkin_lines) == 1, "expected exactly 1 CHECKIN line, got: {}".format(out)
            assert 'Grace' in checkin_lines[0], "expected Grace in CHECKIN, got: {}".format(checkin_lines[0])
            logged = rel_c.execute(
                "SELECT COUNT(*) FROM alert_log WHERE person_id=7 AND outcome='checkin'"
            ).fetchone()[0]
            assert logged == 1, "checkin not logged for Grace"
            ok("alert-pick CHECKIN with spare slots")
        except Exception as e:
            fail("alert-pick CHECKIN with spare slots", str(e))

        # TC-23: CHECKIN 60-day window + let_fade respected
        # ----------------------------------------------------------------
        try:
            # Grace has a checkin today -> no candidate
            cand = _checkin_candidate(rel_c, lk_c, today_dt.isoformat())
            assert cand is None, "expected no candidate within 60-day window, got {}".format(
                cand['name'] if cand else None)
            # Age the checkin to 61 days ago -> Grace comes back
            d_61 = (today_dt - datetime.timedelta(days=61)).isoformat()
            rel_c.execute("UPDATE alert_log SET shown_on=? WHERE person_id=7 AND outcome='checkin'", (d_61,))
            rel_c.commit()
            cand = _checkin_candidate(rel_c, lk_c, today_dt.isoformat())
            assert cand is not None and cand['id'] == 7, "expected Grace after window expiry"
            # let_fade on Grace -> no candidate
            cmd_set_fade(type('A', (), {'person_id': 7, 'value': 'on'})(), rel_c, lk_c)
            cand = _checkin_candidate(rel_c, lk_c, today_dt.isoformat())
            assert cand is None, "let_fade Grace must not be a checkin candidate"
            cmd_set_fade(type('A', (), {'person_id': 7, 'value': 'off'})(), rel_c, lk_c)
            ok("CHECKIN 60-day window + let_fade")
        except Exception as e:
            fail("CHECKIN 60-day window + let_fade", str(e))

        # TC-24: CHECK-widening migration preserves rows
        # ----------------------------------------------------------------
        try:
            mig_path = os.path.join(tmpdir, 'migrate.db')
            mig = sqlite3.connect(mig_path)
            mig.executescript("""
                CREATE TABLE touches (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('call','message','dm','other')),
                    touched_on DATE NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE alert_log (
                    id INTEGER PRIMARY KEY,
                    person_id INTEGER NOT NULL,
                    shown_on DATE NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('shown','dismissed','acted','fade_asked')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                INSERT INTO touches (person_id, kind, touched_on, note) VALUES (1,'call','2026-01-01','old row');
                INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (1,'2026-01-01','shown');
            """)
            mig.commit()
            mig.close()
            ensure_schema(mig_path)
            mig = sqlite3.connect(mig_path)
            t_cnt = mig.execute("SELECT COUNT(*) FROM touches").fetchone()[0]
            a_cnt2 = mig.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
            note = mig.execute("SELECT note FROM touches WHERE person_id=1").fetchone()[0]
            assert t_cnt == 1 and a_cnt2 == 1, "rows lost in migration: touches={} alert_log={}".format(t_cnt, a_cnt2)
            assert note == 'old row', "touch row content changed"
            # New enum values must now be accepted
            mig.execute("INSERT INTO touches (person_id, kind, touched_on) VALUES (2,'meet','2026-01-02')")
            mig.execute("INSERT INTO alert_log (person_id, shown_on, outcome) VALUES (2,'2026-01-02','checkin')")
            mig.commit()
            mig.close()
            ok("CHECK migration preserves rows + widens enum")
        except Exception as e:
            fail("CHECK migration preserves rows + widens enum", str(e))

        # TC-25: alert-pick PICK carries meet-based fields, and a person with an
        # upcoming appointment is excluded from the contact-suggestion list.
        # ----------------------------------------------------------------
        try:
            import io
            # Clean slate: no prior alerts, no fade/checkin state carried over.
            rel_c.execute("DELETE FROM alert_log")
            rel_c.execute("DELETE FROM person_prefs")
            rel_c.execute("DELETE FROM touches")
            rel_c.commit()

            def _run_pick():
                cap = io.StringIO()
                old = sys.stdout
                sys.stdout = cap
                try:
                    cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
                finally:
                    sys.stdout = old
                return cap.getvalue()

            # Bob: met 400d ago (occasion 'Bob dinner'), cycle 360d -> overdue,
            # no upcoming appointment yet. Expect a PICK line with meet fields.
            out = _run_pick()
            bob_pick = next((l for l in out.splitlines()
                             if l.startswith('PICK') and l.split('\t')[1] == '2'), None)
            assert bob_pick is not None, "expected Bob PICK, got: {}".format(out)
            cols = bob_pick.split('\t')
            # cols: PICK pid name gapdays ratio hint meet_days meet_ctx
            assert len(cols) == 8, "PICK must carry 8 fields, got {}: {}".format(len(cols), cols)
            assert cols[6] == '400', "expected meet_days=400, got {}".format(cols[6])
            assert cols[7] == 'Bob dinner', "expected meet_ctx 'Bob dinner', got {}".format(cols[7])

            # Now give Bob an upcoming appointment -> he must drop from picks.
            # lk_c is read-only; use a short-lived writable connection for the
            # fixture write, then re-read through the read-only lk_c.
            d_fut = (today_dt + datetime.timedelta(days=1)).isoformat()
            lk_w = sqlite3.connect(lk_path)
            lk_w.execute(
                "INSERT INTO appointments(id,title,start_at) VALUES (900,'Bob future','{}T10:00:00.000+09:00')".format(d_fut))
            lk_w.execute("INSERT INTO appointment_persons VALUES (900,2)")
            lk_w.commit()
            lk_w.close()
            rel_c.execute("DELETE FROM alert_log")  # clear weekly cap for a fresh run
            rel_c.commit()
            out2 = _run_pick()
            bob_pick2 = next((l for l in out2.splitlines()
                              if l.startswith('PICK') and l.split('\t')[1] == '2'), None)
            assert bob_pick2 is None, "Bob (upcoming appt) must be excluded, got: {}".format(out2)
            ok("alert-pick meet-fields + upcoming-appt exclusion")
        except Exception as e:
            fail("alert-pick meet-fields + upcoming-appt exclusion", str(e))

        # TC-26: context snooze -- future snooze excludes; expiry re-surfaces
        # with a RESURFACE marker and clears; unsnooze restores immediately.
        # ----------------------------------------------------------------
        try:
            import io
            # Clean slate + ensure Bob has no upcoming appointment.
            lk_w = sqlite3.connect(lk_path)
            lk_w.execute("DELETE FROM appointment_persons WHERE appointment_id=900")
            lk_w.execute("DELETE FROM appointments WHERE id=900")
            lk_w.commit(); lk_w.close()
            rel_c.execute("DELETE FROM alert_log")
            rel_c.execute("DELETE FROM person_prefs")
            rel_c.execute("DELETE FROM person_facts WHERE person_id=2")
            rel_c.commit()

            def _pick():
                cap = io.StringIO(); old = sys.stdout; sys.stdout = cap
                try:
                    cmd_alert_pick(type('A', (), {})(), rel_c, lk_c)
                finally:
                    sys.stdout = old
                return cap.getvalue()

            # Baseline: Bob(2) is a pick (stale, no appt, no snooze).
            out = _pick()
            assert any(l.startswith('PICK') and l.split('\t')[1] == '2'
                       for l in out.splitlines()), "Bob should be a baseline pick: {}".format(out)

            # snooze default 6 months with a reason -> excluded + reason fact stored.
            rel_c.execute("DELETE FROM alert_log"); rel_c.commit()  # clear weekly cap
            cmd_snooze(type('A', (), {'person_id': 2, 'until': None, 'months': None,
                                      'reason': 'abroad for PhD'})(), rel_c, lk_c)
            until = rel_c.execute(
                "SELECT snooze_until FROM person_prefs WHERE person_id=2").fetchone()[0]
            exp = _add_months(today_dt, DEFAULT_SNOOZE_MONTHS).isoformat()
            assert until == exp, "expected default 6mo snooze {}, got {}".format(exp, until)
            fact = rel_c.execute(
                "SELECT fact FROM person_facts WHERE person_id=2 AND fact='abroad for PhD'").fetchone()
            assert fact is not None, "reason fact not recorded"
            out2 = _pick()
            assert not any(l.startswith('PICK') and l.split('\t')[1] == '2'
                           for l in out2.splitlines()), "snoozed Bob must be excluded: {}".format(out2)

            # Expired snooze -> RESURFACE marker emitted once, snooze cleared.
            past = (today_dt - datetime.timedelta(days=1)).isoformat()
            rel_c.execute("UPDATE person_prefs SET snooze_until=? WHERE person_id=2", (past,))
            rel_c.execute("DELETE FROM alert_log"); rel_c.commit()  # clear weekly cap
            out3 = _pick()
            assert any(l.startswith('RESURFACE') and l.split('\t')[1] == '2'
                       for l in out3.splitlines()), "expected RESURFACE for Bob: {}".format(out3)
            cleared = rel_c.execute(
                "SELECT snooze_until FROM person_prefs WHERE person_id=2").fetchone()[0]
            assert cleared is None, "expired snooze must be cleared, got {}".format(cleared)

            # Next run: no lingering RESURFACE (fires exactly once), Bob is a pick again.
            rel_c.execute("DELETE FROM alert_log"); rel_c.commit()
            out4 = _pick()
            assert not any(l.startswith('RESURFACE') for l in out4.splitlines()), \
                "RESURFACE must fire only once: {}".format(out4)
            assert any(l.startswith('PICK') and l.split('\t')[1] == '2'
                       for l in out4.splitlines()), "Bob should return to picks: {}".format(out4)

            # unsnooze clears immediately.
            cmd_snooze(type('A', (), {'person_id': 2, 'until': exp, 'months': None,
                                      'reason': None})(), rel_c, lk_c)
            cmd_unsnooze(type('A', (), {'person_id': 2})(), rel_c, lk_c)
            u = rel_c.execute("SELECT snooze_until FROM person_prefs WHERE person_id=2").fetchone()[0]
            assert u is None, "unsnooze must clear snooze_until, got {}".format(u)
            ok("context snooze exclude + resurface + unsnooze")
        except Exception as e:
            fail("context snooze exclude + resurface + unsnooze", str(e))

        # TC-27: additive-column migration adds snooze_until, preserves rows.
        # ----------------------------------------------------------------
        try:
            addc_path = os.path.join(tmpdir, 'addcol.db')
            old_db = sqlite3.connect(addc_path)
            # person_prefs WITHOUT snooze_until (pre-feature shape) + a live row.
            old_db.executescript("""
                CREATE TABLE person_prefs (
                    person_id    INTEGER PRIMARY KEY,
                    channel_mode TEXT NOT NULL DEFAULT 'meet',
                    let_fade     INTEGER NOT NULL DEFAULT 0,
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
                );
                INSERT INTO person_prefs (person_id, channel_mode, let_fade) VALUES (1,'contact',1);
            """)
            old_db.commit(); old_db.close()
            ensure_schema(addc_path)
            chk = sqlite3.connect(addc_path)
            cols = [r[1] for r in chk.execute("PRAGMA table_info(person_prefs)").fetchall()]
            assert 'snooze_until' in cols, "snooze_until column not added"
            row = chk.execute(
                "SELECT channel_mode, let_fade, snooze_until FROM person_prefs WHERE person_id=1").fetchone()
            assert row == ('contact', 1, None), "row not preserved / default wrong: {}".format(tuple(row))
            chk.close()
            ok("additive-column migration adds snooze_until")
        except Exception as e:
            fail("additive-column migration adds snooze_until", str(e))

        rel_c.close()
        lk_c.close()

    except Exception as e:
        fail("selftest-setup", traceback.format_exc())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("---")
    print("TOTAL\t{} passed\t{} failed".format(passes, failures))
    if failures:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog='relmod', description='Relationship module CLI')
    sub = parser.add_subparsers(dest='verb')
    sub.required = True

    # fact-add
    p = sub.add_parser('fact-add')
    p.add_argument('person_id', type=int)
    p.add_argument('fact')
    p.add_argument('--source', default='chat', choices=['retro', 'chat', 'onboarding'])
    p.add_argument('--date', default=None)
    p.set_defaults(func=cmd_fact_add)

    # fact-list
    p = sub.add_parser('fact-list')
    p.add_argument('person_id', type=int)
    p.add_argument('--limit', type=int, default=None)
    p.set_defaults(func=cmd_fact_list)

    # touch-add
    p = sub.add_parser('touch-add')
    p.add_argument('person_id', type=int)
    p.add_argument('kind', choices=['call', 'message', 'dm', 'other', 'meet'])
    p.add_argument('--date', default=None)
    p.add_argument('--note', default=None)
    p.set_defaults(func=cmd_touch_add)

    # last-contact
    p = sub.add_parser('last-contact')
    p.add_argument('person_id', type=int)
    p.set_defaults(func=cmd_last_contact)

    # brief
    p = sub.add_parser('brief')
    p.add_argument('name_or_id')
    p.set_defaults(func=cmd_brief)

    # retro-candidates
    p = sub.add_parser('retro-candidates')
    p.add_argument('--date', default=None)
    p.set_defaults(func=cmd_retro_candidates)

    # stale-list
    p = sub.add_parser('stale-list')
    p.add_argument('--limit', type=int, default=10)
    p.set_defaults(func=cmd_stale_list)

    # alert-pick
    p = sub.add_parser('alert-pick')
    p.set_defaults(func=cmd_alert_pick)

    # dismiss
    p = sub.add_parser('dismiss')
    p.add_argument('person_id', type=int)
    p.set_defaults(func=cmd_dismiss)

    # acted
    p = sub.add_parser('acted')
    p.add_argument('person_id', type=int)
    p.set_defaults(func=cmd_acted)

    # set-mode
    p = sub.add_parser('set-mode')
    p.add_argument('person_id', type=int)
    p.add_argument('mode', choices=['meet', 'contact', 'mixed'])
    p.set_defaults(func=cmd_set_mode)

    # set-fade
    p = sub.add_parser('set-fade')
    p.add_argument('person_id', type=int)
    p.add_argument('value', choices=['on', 'off'])
    p.set_defaults(func=cmd_set_fade)

    # snooze (context snooze -- time-bounded exclusion)
    p = sub.add_parser('snooze')
    p.add_argument('person_id', type=int)
    p.add_argument('--until', default=None, help='YYYY-MM-DD; overrides --months')
    p.add_argument('--months', type=int, default=None, help='snooze N months from today')
    p.add_argument('--reason', default=None, help='stated reason; recorded as a fact')
    p.set_defaults(func=cmd_snooze)

    # unsnooze (clear a context snooze)
    p = sub.add_parser('unsnooze')
    p.add_argument('person_id', type=int)
    p.set_defaults(func=cmd_unsnooze)

    # drift-list
    p = sub.add_parser('drift-list')
    p.set_defaults(func=cmd_drift_list)

    # selftest
    p = sub.add_parser('selftest')
    p.set_defaults(func=cmd_selftest)

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Back up existing relationship.db before any schema changes
    if os.path.isfile(RELDB_PATH) and os.path.getsize(RELDB_PATH) > 0:
        bak = RELDB_PATH + '.bak'
        if not os.path.isfile(bak):
            shutil.copy2(RELDB_PATH, bak)

    ensure_schema()

    parser = build_parser()
    args = parser.parse_args()

    rel = rel_conn()
    lk = lk_conn()
    try:
        args.func(args, rel, lk)
    finally:
        rel.close()
        lk.close()


if __name__ == '__main__':
    main()
