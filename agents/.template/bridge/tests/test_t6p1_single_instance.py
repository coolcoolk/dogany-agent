"""v2.7 T6 P1 (dec-319): bridge single-instance guard + host-child flag."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bridge import instance_lock as il

BRIDGE_DIR = Path(__file__).resolve().parent.parent
START_SH = BRIDGE_DIR / "start.sh"
PKG_PARENT = BRIDGE_DIR.parent


def _root(tmp_path):
    (tmp_path / ".telegram_bot").mkdir()
    return tmp_path


# A tiny child that takes the lock the same way the bridge does and then idles.
HOLDER = (
    "import sys, time\n"
    "from bridge import instance_lock as il\n"
    "fd = il.acquire(sys.argv[1], host=sys.argv[2])\n"
    "print('locked', flush=True)\n"
    "time.sleep(float(sys.argv[3]))\n"
)


def _spawn_holder(root, host="manual", secs=30):
    env = dict(os.environ, PYTHONPATH=str(PKG_PARENT))
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(root), host, str(secs)],
                         stdout=subprocess.PIPE, text=True, env=env)
    assert p.stdout.readline().strip() == "locked"
    return p


def test_acquire_writes_holder_record(tmp_path):
    root = _root(tmp_path)
    fd = il.acquire(root, host="launchd")
    try:
        rec = json.loads(il.lock_path(root).read_text())
        assert rec["pid"] == os.getpid()
        assert rec["host"] == "launchd"
        assert rec["started_at"]
    finally:
        os.close(fd)


def test_creates_missing_dirs(tmp_path):
    fd = il.acquire(tmp_path, host="manual")
    os.close(fd)
    assert il.lock_path(tmp_path).exists()


def test_second_instance_exits_75_without_polling(tmp_path):
    root = _root(tmp_path)
    holder = _spawn_holder(root, host="app")
    try:
        # Guard must exit 75 before reaching anything after it (the "polling" sentinel).
        code = (
            "import sys\n"
            "from bridge import instance_lock as il\n"
            "il.guard(sys.argv[1], host='manual')\n"
            "print('POLLING')\n"
        )
        env = dict(os.environ, PYTHONPATH=str(PKG_PARENT))
        r = subprocess.run([sys.executable, "-c", code, str(root)],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 75
        assert "POLLING" not in r.stdout
        out = (r.stdout + r.stderr).strip().splitlines()
        assert len(out) == 1
        assert "pid=%d" % holder.pid in out[0] and "host=app" in out[0]
        # Holder record was not clobbered by the loser.
        assert json.loads(il.lock_path(root).read_text())["pid"] == holder.pid
    finally:
        holder.kill()
        holder.wait()


def test_lock_released_on_exit_lets_new_one_start(tmp_path):
    root = _root(tmp_path)
    holder = _spawn_holder(root)
    with pytest.raises(il.LockHeld):
        il.acquire(root)
    holder.kill()
    holder.wait()
    fd = il.acquire(root, host="manual")
    os.close(fd)


@pytest.mark.parametrize("env_val,expected", [
    ("launchd", "launchd"), ("app", "app"), (None, "manual"), ("bogus", "manual"),
])
def test_host_recorded_via_guard(tmp_path, monkeypatch, env_val, expected):
    root = _root(tmp_path)
    if env_val is None:
        monkeypatch.delenv(il.HOST_ENV, raising=False)
    else:
        monkeypatch.setenv(il.HOST_ENV, env_val)
    fd = il.guard(root)
    try:
        assert json.loads(il.lock_path(root).read_text())["host"] == expected
    finally:
        os.close(fd)


def test_cold_start_does_not_wait(tmp_path):
    root = _root(tmp_path)
    holder = _spawn_holder(root)
    try:
        t0 = time.monotonic()
        with pytest.raises(SystemExit) as e:
            il.guard(root, host="manual")
        assert e.value.code == 75
        assert time.monotonic() - t0 < 2.0
    finally:
        holder.kill()
        holder.wait()


def test_restart_wait_succeeds_when_old_pid_exits_within_10s(tmp_path):
    root = _root(tmp_path)
    holder = _spawn_holder(root, secs=1.5)
    il.marker_path(root).write_text(str(int(time.time())))
    try:
        t0 = time.monotonic()
        fd = il.guard(root, host="launchd")
        assert 0.5 < time.monotonic() - t0 < il.RESTART_WAIT_S
        os.close(fd)
        assert not il.marker_path(root).exists()  # consumed
    finally:
        holder.kill()
        holder.wait()


def test_restart_wait_gives_up_with_75(tmp_path, monkeypatch):
    root = _root(tmp_path)
    holder = _spawn_holder(root)
    il.marker_path(root).write_text("x")
    monkeypatch.setattr(il, "RESTART_WAIT_S", 0.6)
    try:
        with pytest.raises(SystemExit) as e:
            il.guard(root, host="manual")
        assert e.value.code == 75
    finally:
        holder.kill()
        holder.wait()


def test_stale_marker_is_ignored(tmp_path):
    root = _root(tmp_path)
    m = il.marker_path(root)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("x")
    old = time.time() - 600
    os.utime(m, (old, old))
    assert not il.restart_wait_requested(root)


def test_health_segment(tmp_path):
    root = _root(tmp_path)
    assert il.health_segment(root) is None
    fd = il.acquire(root, host="app")
    try:
        seg = il.health_segment(root)
        assert "app" in seg and str(os.getpid()) in seg
    finally:
        os.close(fd)


# --- start.sh plumbing (stub python prints the env it received) -------------

def _run_start(tmp_path, *flags):
    stub = tmp_path / "stubpy"
    stub.write_text('#!/bin/bash\necho "HOST=$BRIDGE_HOST_CHILD ARGS=$*"\n')
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "BRIDGE_HOST_CHILD"}
    env["BRIDGE_PYTHON"] = str(stub)
    return subprocess.run(["bash", str(START_SH), "--path", str(tmp_path), *flags],
                          capture_output=True, text=True, env=env)


@pytest.fixture(autouse=False)
def _no_venv():
    if (BRIDGE_DIR / "venv" / "bin" / "python").exists():
        pytest.skip("venv present: stub interpreter not used")


@pytest.mark.parametrize("flags,host", [
    (["--_host_child", "launchd"], "launchd"),
    (["--_host_child", "app"], "app"),
    ([], "manual"),
    (["--_launchd_child"], "launchd"),
])
def test_start_sh_host_plumbing(tmp_path, _no_venv, flags, host):
    r = _run_start(tmp_path, *flags)
    assert r.returncode == 0, r.stderr
    assert "HOST=%s " % host in r.stdout
    assert "_host_child" not in r.stdout and "_launchd_child" not in r.stdout


def test_start_sh_rejects_bad_host(tmp_path, _no_venv):
    assert _run_start(tmp_path, "--_host_child", "bogus").returncode == 1
    assert _run_start(tmp_path, "--_host_child").returncode == 1
