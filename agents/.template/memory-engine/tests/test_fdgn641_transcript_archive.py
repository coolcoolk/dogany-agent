# test-lane: fast
# test-tier: 0
# test-component: transcript recall archive/restore CLI and rebuild exclusions
# test-prereq: tmp-files, pytest
# test-covers: agents/.template/memory-engine/memory.py agents/.template/memory-engine/archive_engine.py
# test-new-component: Durable transcript selection archive, independent of the cache.
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
import memory
from test_dgn501_sameday_recall import sandbox, _msg, _utc_iso, _write_session


@pytest.fixture
def world(sandbox, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_DATA_ROOT", str(sandbox.tmp_path / "archive-data"))
    monkeypatch.setattr(memory, "MEMORIES_DIR", str(sandbox.tmp_path / "notes"))
    os.mkdir(memory.MEMORIES_DIR)
    monkeypatch.setattr(memory, "embed", lambda text: [0.0] * memory.EMBED_DIM)
    # Two topics in the same session prove selection does not remove a session.
    base = sandbox.now - datetime.timedelta(hours=2)
    _write_session(sandbox.proj_dir, "S1", [
        _msg("user", "ledger card purpose", _utc_iso(base)),
        _msg("assistant", "ledger card is corporate", _utc_iso(base + datetime.timedelta(minutes=1))),
        _msg("user", "cooking recipe", _utc_iso(base + datetime.timedelta(minutes=2))),
        _msg("assistant", "cooking recipe uses rice", _utc_iso(base + datetime.timedelta(minutes=3))),
    ])
    _write_session(sandbox.proj_dir, "S2", [
        _msg("assistant", "ledger card second session", _utc_iso(base + datetime.timedelta(minutes=4))),
    ])
    conn = memory.connect()
    memory.init_db(conn)
    memory.index_transcript_notes(conn)
    # Exercise real old FTS entries as well as the currently empty shadow.
    conn.execute("INSERT INTO transcript_fts(transcript_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    return sandbox


def rows():
    with memory.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM transcript_notes ORDER BY id")]


def archive(**kwargs):
    args = dict(session=None, since=None, until=None, match=None, dry_run=False)
    args.update(kwargs)
    return memory.cmd_transcript_archive(argparse.Namespace(**args))


def archive_id():
    return memory.memory_archive_engine().transcript_state()["archives"][-1]["archive_id"]


def restore(item=None):
    return memory.cmd_transcript_restore(argparse.Namespace(archive_id=item or archive_id()))


def raw_hashes(world):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (world.tmp_path / "projects").glob("*.jsonl")}


@pytest.mark.parametrize("selector,count", [({"match": "ledger"}, 3),
                                           ({"session": "S1"}, 4),
                                           ({"since": "2026-07-22", "until": "2026-07-22"}, 5)])
def test_selectors_restore_exact_and_other_rows_untouched(world, capsys, selector, count):
    before, raw = rows(), raw_hashes(world)
    assert archive(**selector) == 0
    assert "Archived: %d transcript rows" % count in capsys.readouterr().out
    after = rows()
    assert len(after) == len(before) - count
    assert all(r in before for r in after)
    assert memory.cmd_transcript_list(argparse.Namespace()) == 0
    assert "1. " + archive_id() in capsys.readouterr().out
    assert restore() == 0
    assert rows() == before
    assert raw_hashes(world) == raw
    assert memory.cmd_transcript_list(argparse.Namespace()) == 0
    assert "Archived: 0 selections" in capsys.readouterr().out
    assert restore() == 2


def test_dry_run_is_read_only_and_literal_match(world, capsys):
    before = open(memory.DB_PATH, "rb").read()
    data = memory.memory_archive_engine().transcripts
    assert not os.path.exists(data)
    assert archive(match="ledger", dry_run=True) == 0
    output = capsys.readouterr().out
    assert "Dry run: 3 transcript rows" in output and "1. " in output
    assert archive(match="%", dry_run=True) == 0
    assert "Dry run: 0" in capsys.readouterr().out
    assert open(memory.DB_PATH, "rb").read() == before
    assert not os.path.exists(data)


@pytest.mark.parametrize("selector", [{"match": " "}, {"session": ""},
    {"since": "2026-07-22"}, {"until": "2026-07-22", "match": "ledger"},
    {"since": "2026-07-23", "until": "2026-07-22"},
    {"since": "bad", "until": "2026-07-22"}])
def test_invalid_selectors_do_not_archive(world, selector):
    before = rows()
    assert archive(**selector) == 2
    assert rows() == before
    assert not os.path.exists(memory.memory_archive_engine().transcripts)


def test_search_fts_today_and_both_hook_lanes(world, capsys):
    assert memory._hook_today_recall("ledger card", "other")
    assert memory._hook_same_day_answer("ledger card again", "other")
    before = rows()
    assert archive(match="ledger") == 0
    capsys.readouterr()
    with memory.connect() as conn:
        assert not memory.transcript_search(conn, "ledger", 20)
        assert not memory.same_day_answer_search(conn, "ledger card", 20)
        assert not conn.execute("SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH 'ledger'").fetchall()
        assert conn.execute("SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH 'cooking'").fetchall()
    assert memory._hook_today_recall("ledger card", "other") is None
    assert memory._hook_same_day_answer("ledger card again", "other") is None
    assert memory._hook_today_recall("cooking recipe", "other")
    assert memory.cmd_today(argparse.Namespace(since=None, json=True, limit=0)) == 0
    today = json.loads(capsys.readouterr().out)
    assert len(today) == 2 and all("cooking" in r["text"] for r in today)
    assert restore() == 0
    assert rows() == before
    assert memory._hook_today_recall("ledger card", "other")


def test_cache_and_full_database_rebuild_keep_exclusions(world):
    before = rows()
    assert archive(match="ledger") == 0
    for fresh_db in (False, True):
        if fresh_db:
            os.unlink(memory.DB_PATH)
        with memory.connect() as conn:
            memory.init_db(conn)
            conn.execute("DELETE FROM transcript_notes")
            conn.execute("DELETE FROM consolidation_state WHERE key='ts_fts_last'")
            conn.commit()
            memory.index_transcript_notes(conn)
        assert [r["text"] for r in rows()] == [r["text"] for r in before if "cooking" in r["text"]]
        # Full markdown index uses the same recovery/synchronization preamble.
        with open(os.path.join(memory.MEMORIES_DIR, "inbox.md"), "w") as stream:
            stream.write("# Notes\n\n- cooking baseline fact\n")
        assert memory.cmd_index(argparse.Namespace(lock=None)) == 0
        assert all("ledger" not in r["text"] for r in rows())
    assert restore() == 0
    assert sorted((r["ts"], r["role"], r["session"], r["text"]) for r in rows()) == sorted(
        (r["ts"], r["role"], r["session"], r["text"]) for r in before)


def test_raw_recent_history_fallback_respects_archive_tomorrow(world):
    tomorrow = world.now + datetime.timedelta(days=1)
    assert memory.recent_transcript_search("ledger card", 10, now=tomorrow)
    assert archive(match="ledger") == 0
    assert not memory.recent_transcript_search("ledger card", 10, now=tomorrow)
    assert memory.recent_transcript_search("cooking recipe", 10, now=tomorrow)
    assert restore() == 0
    assert memory.recent_transcript_search("ledger card", 10, now=tomorrow)


def test_identity_includes_text_hash_and_restore_refuses_conflict(world):
    original = rows()[0]
    assert archive(match="ledger") == 0
    with memory.connect() as conn:
        conn.execute("INSERT INTO transcript_notes(ts,role,session,text) VALUES(?,?,?,?)",
                     (original["ts"], original["role"], original["session"], "corrected card purpose"))
    with memory.connect() as conn:
        memory.init_db(conn)
    assert any(r["text"] == "corrected card purpose" for r in rows())
    assert restore() == 2
    assert memory.memory_archive_engine().transcript_state()["archives"][0]["state"] == "archived"


def test_rearchive_restore_and_retention(world):
    assert archive(match="ledger") == 0
    assert restore() == 0
    assert archive(match="ledger") == 0
    second = archive_id()
    with memory.connect() as conn:
        memory.init_db(conn)
    assert all("ledger" not in r["text"] for r in rows())
    assert restore(second) == 0
    with memory.connect() as conn:
        conn.execute("DELETE FROM transcript_notes")
        conn.commit()
        memory.init_db(conn)
    assert rows() == []  # Restored history does not defeat normal retention.


def test_cli_journal_crash_recovery_and_read_only_preview(world):
    engine = world.tmp_path / "memory-engine"
    shutil.copytree(memory.HERE, engine, ignore=shutil.ignore_patterns("tests", "__pycache__", "state.db*"))
    shutil.copy2(memory.DB_PATH, engine / "state.db")
    env = dict(os.environ, MEMORY_DATA_ROOT=memory.MEMORY_DATA_ROOT, MEMORIES_DIR=memory.MEMORIES_DIR)
    env.pop("MEMORY_TEST_KILL_AT", None)
    def cli(*args, **extra):
        return subprocess.run([sys.executable, str(engine / "memory.py"), *args],
                              env=dict(env, **extra), capture_output=True, text=True, timeout=15)
    assert cli("transcript-archive", "--match", "ledger", "--session", "S1").returncode == 2
    killed = cli("transcript-archive", "--match", "ledger", MEMORY_TEST_KILL_AT="after-journal")
    assert killed.returncode == -9
    journal = memory.memory_archive_engine().journal
    assert os.path.isfile(journal)
    preview = cli("transcript-archive", "--match", "ledger", "--dry-run")
    assert preview.returncode == 2 and os.path.isfile(journal)
    listed = cli("transcript-list")
    assert listed.returncode == 0 and "3 transcript rows" in listed.stdout
    assert not os.path.isfile(journal)
    # init repairs a cache left untouched by the killed process.
    with sqlite3.connect(engine / "state.db") as conn:
        conn.row_factory = sqlite3.Row
        memory.init_db(conn)
        assert conn.execute("SELECT count(*) FROM transcript_notes").fetchone()[0] == 2
    preview = cli("transcript-archive", "--match", "ledger", "--dry-run")
    assert preview.returncode == 0 and "Dry run: 0" in preview.stdout
    killed = cli("transcript-restore", archive_id(), MEMORY_TEST_KILL_AT="after-journal")
    assert killed.returncode == -9
    assert cli("transcript-list").returncode == 0
    with sqlite3.connect(engine / "state.db") as conn:
        conn.row_factory = sqlite3.Row
        memory.init_db(conn)
        assert conn.execute("SELECT count(*) FROM transcript_notes").fetchone()[0] == 5


def test_date_range_uses_local_days_and_excludes_adjacent_days(world):
    # Offset timestamps cannot be selected by string prefix or lexical bounds.
    midnight = world.now.replace(hour=0, minute=0, second=0, microsecond=0)
    values = [(midnight - datetime.timedelta(seconds=1), 'before'),
              (midnight, 'first'),
              (midnight + datetime.timedelta(days=1, seconds=-1), 'last'),
              (midnight + datetime.timedelta(days=1), 'after')]
    with memory.connect() as conn:
        conn.executemany('INSERT INTO transcript_notes(ts,role,session,text) VALUES(?,?,?,?)',
                         [(ts.isoformat(), 'assistant', 'boundary', text) for ts, text in values])
    assert archive(since='2026-07-22', until='2026-07-22') == 0
    assert [r['text'] for r in rows()] == ['before', 'after']
    assert restore() == 0
    assert len(rows()) == 9


def test_recovery_clears_fts_even_if_content_cache_was_already_reset(world):
    engine = memory.memory_archive_engine()
    with memory.connect() as conn:
        selected = [dict(r) for r in conn.execute("SELECT * FROM transcript_notes WHERE text LIKE '%ledger%'")]
        # Simulate a durable journal commit followed by content-only cache loss.
        with engine.transaction():
            engine.archive_transcripts(engine.transcript_state(), selected, {'match': 'ledger'})
        conn.execute('DELETE FROM transcript_notes')
        conn.commit()
        memory.init_db(conn)
        assert not conn.execute("SELECT rowid FROM transcript_fts WHERE transcript_fts MATCH 'ledger'").fetchall()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
