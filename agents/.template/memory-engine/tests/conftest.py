"""Shared isolation for memory-engine tests.

DGN-1873: memory.REINDEX_LOCK_PATH defaults to a file beside memory.py. In the
release sweep several suites import this module at once, so a test that takes
(or waits on) that lock contends with an unrelated suite -- seen as 10 red
tests on 2026-10-06 that all passed alone. Every test gets its own lock file.
A test that needs a specific lock path still sets it explicitly.
"""
import os
import sys

import pytest

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
    if hasattr(memory, "REINDEX_LOCK_PATH"):
        monkeypatch.setattr(memory, "REINDEX_LOCK_PATH",
                            str(tmp_path / ".reindex.lock"))
    yield
