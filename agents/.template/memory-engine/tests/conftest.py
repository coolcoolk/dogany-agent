# test-lane: lib    # shared pytest isolation fixtures, loaded by every memory-engine suite
"""Shared isolation for memory-engine tests.

DGN-1873: memory.REINDEX_LOCK_PATH defaults to a file beside memory.py. In the
release sweep several suites import this module at once, so a test that takes
(or waits on) that lock contends with an unrelated suite -- seen as 10 red
tests on 2026-10-06 that all passed alone. Every test gets its own lock file.
A test that needs a specific lock path still sets it explicitly.
"""
import os
import sys
import tempfile

import pytest

# FDGN-620: archive/journal state must never land under agents/.template/data.
# Set before any test imports memory (module-level default root).
os.environ.setdefault("MEMORY_DATA_ROOT", tempfile.mkdtemp(prefix="memory-data-"))

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
if ENGINE not in sys.path:
    sys.path.insert(0, ENGINE)


@pytest.fixture(autouse=True)
def _isolated_reindex_lock(tmp_path, monkeypatch):
    try:
        import memory  # noqa: WPS433 -- the module under test
    except Exception:
        yield
        return
    monkeypatch.setattr(memory, "MEMORY_DATA_ROOT", str(tmp_path / "memory-data"))
    monkeypatch.setattr(memory, "INJECTION_DB_PATH", str(tmp_path / ".injections.db"))
    if hasattr(memory, "REINDEX_LOCK_PATH"):
        monkeypatch.setattr(memory, "REINDEX_LOCK_PATH",
                            str(tmp_path / ".reindex.lock"))
    yield


# cmd_hook's last-resort catch-all ends in os._exit(0) (never block the turn).
# Inside pytest that kills the test process with rc 0 and no summary: a stale
# stub (DGN-1949's _hook_keyword_fallback after DGN-1962 added an argument)
# turned the whole suite QUIET-GREEN on 2026-10-09. In the pytest process
# itself os._exit raises instead, so the swallowed error fails its test with
# the original exception chained; a forked child still exits for real.
_PYTEST_PID = os.getpid()
_REAL_OS_EXIT = os._exit


def _guarded_os_exit(code):
    if os.getpid() != _PYTEST_PID:
        _REAL_OS_EXIT(code)
    raise AssertionError(
        "os._exit(%r) in the pytest process -- the code under test swallowed "
        "an error and hard-exited (would end the run silently with rc 0)" % (code,))


@pytest.fixture(autouse=True)
def _os_exit_fails_loud(monkeypatch):
    monkeypatch.setattr(os, "_exit", _guarded_os_exit)
    yield
