"""DGN-986 [b]: boot-time runtime snapshot (bridge/boot_snapshot.py).

Covers the locked schema=1 contract and the boot-path first principles:
  (a) happy path: every locked field present, schema == 1, values sourced
      from the instance root (fw_version, engine_versions map, marker label).
  (b) env-file absence: missing .env / shared env / plist never raises; the
      entries are recorded with sha256 null (path null when unresolvable).
  (c) sha256 integrity: recorded digests equal the real file hashes.
  (d) failure absorption: a raising snapshot builder does NOT propagate to
      the caller (write_runtime_snapshot returns None, logs one WARNING).
  (e) atomicity: a mid-write failure leaves NO partial/tmp file behind and
      never clobbers an existing good snapshot; a successful write leaves
      no tmp droppings.
  (f) read-only engine access (R5): reading PRAGMA user_version creates no
      WAL/SHM side-effect files next to the database.
  (g) engine_versions per-kit map: 0 dbs -> {}, 1 db -> single key,
      2 dbs with different tiers -> BOTH keys preserved (never a scalar max).
"""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import bridge.boot_snapshot as boot_snapshot
from bridge import config as config_mod


@pytest.fixture()
def instance(tmp_path, monkeypatch):
    """A fake instance root wired into bridge.config's module constants."""
    root = tmp_path / "agent"
    data_dir = root / ".telegram_bot"
    data_dir.mkdir(parents=True)
    (root / "database").mkdir()

    project_env = data_dir / ".env"
    project_env.write_text("TELEGRAM_BOT_TOKEN=test:token\n", encoding="utf-8")

    shared_env = tmp_path / ".rules" / ".env"
    shared_env.parent.mkdir()
    shared_env.write_text("CLAUDE_CLI_PATH=/nonexistent\n", encoding="utf-8")

    plist = tmp_path / "com.telegram-skill-bot.test.newbridge.plist"
    plist.write_text("<plist/>\n", encoding="utf-8")
    (data_dir / ".service_plist").write_text(
        f"{plist}\ncom.telegram-skill-bot.test\n", encoding="utf-8"
    )

    (root / ".instance.conf").write_text(
        "DOGANY_INSTANCE=test\nDOGANY_FW_VERSION=1.39.3\n", encoding="utf-8"
    )

    db = root / "database" / "lifekit.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 29")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()

    # Fake claude CLI: a tiny executable that prints a version string.
    fake_cli = tmp_path / "claude"
    fake_cli.write_text("#!/bin/sh\necho '9.9.9 (Claude Code)'\n", encoding="utf-8")
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR)

    install_info = tmp_path / ".claude.json"
    install_info.write_text(json.dumps({"installMethod": "native"}), encoding="utf-8")

    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.setattr(config_mod, "BOT_DATA_DIR", data_dir)
    monkeypatch.setattr(config_mod, "PROJECT_ENV_PATH", project_env)
    monkeypatch.setattr(config_mod, "DOGANY_ENV_PATH", shared_env)
    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", str(fake_cli))
    monkeypatch.setattr(boot_snapshot, "CLAUDE_INSTALL_INFO_PATH", install_info)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)

    return {
        "root": root,
        "data_dir": data_dir,
        "project_env": project_env,
        "shared_env": shared_env,
        "plist": plist,
        "db": db,
    }


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- (a) happy path -------------------------------------------------------


def test_snapshot_written_with_all_locked_fields(instance):
    target = boot_snapshot.write_runtime_snapshot()
    assert target is not None
    assert target == instance["data_dir"] / "runtime-snapshot.json"
    snap = _load(target)

    assert set(snap.keys()) == {
        "schema",
        "label",
        "pid",
        "started_at",
        "fw_version",
        "bridge_version",  # DGN-818 C3 -- additive at schema 1 (see module doc)
        "engine_versions",
        "claude_cli",
        "env_files",
    }
    assert snap["schema"] == 1
    assert snap["label"] == "com.telegram-skill-bot.test"
    assert snap["pid"] == os.getpid()
    # ISO8601 with a local UTC offset (never a naive timestamp).
    assert "T" in snap["started_at"]
    assert ("+" in snap["started_at"][10:]) or ("-" in snap["started_at"][10:])
    assert snap["fw_version"] == "1.39.3"
    assert snap["engine_versions"] == {"lifekit": 29}  # 1 db -> single key
    assert set(snap["claude_cli"].keys()) == {
        "resolved_path",
        "version",
        "install_method",
    }
    assert snap["claude_cli"]["version"] == "9.9.9 (Claude Code)"
    assert snap["claude_cli"]["install_method"] == "native"
    assert len(snap["env_files"]) == 3
    for entry in snap["env_files"]:
        assert set(entry.keys()) == {"path", "sha256"}


def test_snapshot_records_no_secret_content(instance):
    """Secrets safety: the snapshot must not carry env contents or key names."""
    secret = "TELEGRAM_BOT_TOKEN=TEST-ONLY-8766902149-VERY-SECRET-VALUE\n"
    instance["project_env"].write_text(secret, encoding="utf-8")
    target = boot_snapshot.write_runtime_snapshot()
    raw = target.read_text(encoding="utf-8")
    assert "VERY-SECRET-VALUE" not in raw
    assert "TELEGRAM_BOT_TOKEN" not in raw


# --- (b) absence tolerance ------------------------------------------------


def test_missing_env_files_still_write_snapshot(instance, monkeypatch):
    instance["project_env"].unlink()
    instance["plist"].unlink()
    monkeypatch.setattr(config_mod, "DOGANY_ENV_PATH", None)  # no shared env

    target = boot_snapshot.write_runtime_snapshot()
    assert target is not None
    snap = _load(target)
    own, shared, plist = snap["env_files"]
    assert own["path"] == str(instance["project_env"])
    assert own["sha256"] is None
    assert shared == {"path": None, "sha256": None}
    assert plist["path"] == str(instance["plist"])
    assert plist["sha256"] is None


def test_bare_instance_yields_nulls_not_errors(instance, monkeypatch):
    """No .instance.conf / no database/ / no marker / no CLI -> nulls."""
    (instance["root"] / ".instance.conf").unlink()
    instance["db"].unlink()
    (instance["data_dir"] / ".service_plist").unlink()
    monkeypatch.setattr(config_mod, "CLAUDE_CLI_PATH", str(instance["root"] / "nope"))
    monkeypatch.setattr(
        boot_snapshot, "CLAUDE_INSTALL_INFO_PATH", instance["root"] / "no.json"
    )

    target = boot_snapshot.write_runtime_snapshot()
    assert target is not None
    snap = _load(target)
    assert snap["schema"] == 1
    assert snap["label"] is None
    assert snap["fw_version"] is None
    assert snap["engine_versions"] == {}  # 0 dbs -> empty map, no error
    assert snap["claude_cli"] == {
        "resolved_path": None,
        "version": None,
        "install_method": None,
    }


# --- (c) sha256 integrity -------------------------------------------------


def test_sha256_matches_real_file_hashes(instance):
    target = boot_snapshot.write_runtime_snapshot()
    snap = _load(target)
    expected = {
        str(instance["project_env"]): instance["project_env"].read_bytes(),
        str(instance["shared_env"]): instance["shared_env"].read_bytes(),
        str(instance["plist"]): instance["plist"].read_bytes(),
    }
    for entry in snap["env_files"]:
        assert entry["sha256"] == hashlib.sha256(expected[entry["path"]]).hexdigest()


# --- (d) failure absorption ----------------------------------------------


def test_builder_exception_never_reaches_caller(instance, monkeypatch, caplog):
    def boom():
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(boot_snapshot, "_build_snapshot", boom)
    with caplog.at_level("WARNING"):
        result = boot_snapshot.write_runtime_snapshot()  # must NOT raise
    assert result is None
    assert any("runtime snapshot skipped" in r.message for r in caplog.records)


def test_unwritable_target_never_reaches_caller(instance, monkeypatch):
    monkeypatch.setattr(
        config_mod, "BOT_DATA_DIR", instance["project_env"]  # a FILE, not a dir
    )
    assert boot_snapshot.write_runtime_snapshot() is None  # must not raise


# --- (e) atomicity --------------------------------------------------------


def test_midwrite_failure_leaves_no_partial_file(instance, monkeypatch):
    def bad_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(boot_snapshot.json, "dump", bad_dump)
    assert boot_snapshot.write_runtime_snapshot() is None
    leftovers = list(instance["data_dir"].glob("runtime-snapshot.json*"))
    assert leftovers == []  # no target, no tmp dropping


def test_midwrite_failure_preserves_previous_snapshot(instance, monkeypatch):
    good = boot_snapshot.write_runtime_snapshot()
    before = good.read_bytes()

    def bad_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(boot_snapshot.json, "dump", bad_dump)
    assert boot_snapshot.write_runtime_snapshot() is None
    assert good.read_bytes() == before  # old snapshot intact, not truncated


def test_successful_write_leaves_no_tmp_droppings(instance):
    boot_snapshot.write_runtime_snapshot()
    tmps = [
        p
        for p in instance["data_dir"].iterdir()
        if p.name.startswith("runtime-snapshot.json.") and p.suffix == ".tmp"
    ]
    assert tmps == []


# --- (f) read-only engine access (R5) ------------------------------------


def test_engine_read_creates_no_wal_shm_side_effects(instance):
    """Quiescent WAL-mode db (no -wal on disk): the immutable=1 branch must
    read the version WITHOUT materializing -wal/-shm files. (A plain mode=ro
    open measurably creates both -- that regression is what this guards.)"""
    con = sqlite3.connect(instance["db"])
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()  # last close checkpoints and removes -wal/-shm
    for suffix in ("-wal", "-shm"):
        p = Path(str(instance["db"]) + suffix)
        if p.exists():
            p.unlink()

    snap = _load(boot_snapshot.write_runtime_snapshot())
    assert snap["engine_versions"] == {"lifekit": 29}
    for suffix in ("-wal", "-shm"):
        assert not Path(str(instance["db"]) + suffix).exists()


def test_engine_read_on_hot_wal_db(instance):
    """Hot WAL db (-wal present, writer connection still open): the plain
    mode=ro branch reads the correct version through the WAL."""
    con = sqlite3.connect(instance["db"])
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    try:
        assert Path(str(instance["db"]) + "-wal").exists()  # hot precondition
        snap = _load(boot_snapshot.write_runtime_snapshot())
        assert snap["engine_versions"] == {"lifekit": 29}
    finally:
        con.close()


# --- (g) engine_versions per-kit map --------------------------------------


def test_engine_versions_two_dbs_both_keys_preserved(instance):
    """Two kits with DIFFERENT tiers -> BOTH keys survive, per kit. The map
    must never collapse to a scalar max (which kit's tier would be lost --
    /health reports engine tiers per kit, e.g. lifekit v29 / memory v11)."""
    other = instance["root"] / "database" / "memorykit.db"
    con = sqlite3.connect(other)
    con.execute("PRAGMA user_version = 11")
    con.commit()
    con.close()

    snap = _load(boot_snapshot.write_runtime_snapshot())
    assert snap["engine_versions"] == {"lifekit": 29, "memorykit": 11}


def test_readonly_connection_cannot_write(instance):
    """The mode=ro URI must reject writes -- guard against a silent downgrade
    to a writable connection in a future edit."""
    uri = f"file:{instance['db']}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("PRAGMA user_version = 99")
    con.close()
