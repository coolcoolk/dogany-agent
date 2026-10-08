"""Tests for DGN-712: i18n resolver import-safety.

v1.24.0 bricked drifted live instances because bridge/messages.py binds i18n
strings at MODULE IMPORT time (MODEL_ALREADY_ACTIVE = t("model_already_active")).
On an instance whose i18n catalog never landed a newly code-referenced key, the
old t() raised KeyError at import -> bridge.bot could not import -> no poll
heartbeat -> watchdog restart loop -> live DOWN.

These tests pin the fix invariants:
  1. t() with a missing key does NOT raise; it returns a safe visible fallback
     (the raw key name) so module import never bricks.
  2. importing bridge.messages succeeds even if a referenced key is absent from
     the active catalog (simulated by dropping a key at runtime).
  3. t_strict() still raises on a missing key (the loud path for lint/tests).
  4. a present key still resolves normally (no regression).
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

# conftest.py inserts the package root and sets env vars; keep a standalone
# guard so this file also runs directly.
_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-dgn712")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")

from bridge import i18n  # noqa: E402
from bridge.i18n import en  # noqa: E402


def test_t_missing_key_does_not_raise_returns_key():
    """A key absent from BOTH catalogs must not raise; it yields the raw key."""
    missing = "dgn712_definitely_absent_key"
    assert missing not in en.STRINGS
    result = i18n.t(missing)
    assert result == missing  # visible fallback, never KeyError


def test_t_present_key_resolves_normally():
    """A real key still resolves to its template (no regression)."""
    # model_already_active is the exact key that broke v1.24.0; it must resolve.
    assert i18n.t("model_already_active")  # non-empty template
    assert "{label}" in i18n.t("model_already_active")


def test_t_strict_missing_key_raises():
    """The strict variant keeps the loud fail path for lint/tests."""
    with pytest.raises(KeyError):
        i18n.t_strict("dgn712_definitely_absent_key")


def test_messages_import_survives_missing_referenced_key(monkeypatch):
    """bridge.messages must import even if a referenced key is absent.

    Simulate a drifted i18n by removing 'model_already_active' from BOTH
    catalogs, then (re)import messages. With the old raising t() this import
    would KeyError; with the import-safe t() it must succeed and bind the raw
    key string as the constant value.
    """
    from bridge.i18n import en as en_mod, ko as ko_mod

    monkeypatch.delitem(en_mod.STRINGS, "model_already_active", raising=False)
    monkeypatch.delitem(ko_mod.STRINGS, "model_already_active", raising=False)

    # Force a fresh import of messages so its module-level t() bindings re-run
    # against the now-missing key.
    sys.modules.pop("bridge.messages", None)
    messages = importlib.import_module("bridge.messages")

    # Import succeeded; the missing key degraded to the raw key string.
    assert messages.MODEL_ALREADY_ACTIVE == "model_already_active"

    # Clean up so other tests re-import a normal messages module.
    sys.modules.pop("bridge.messages", None)


def test_messages_import_normal_binds_real_template():
    """With catalogs intact, messages binds the real template (regression)."""
    sys.modules.pop("bridge.messages", None)
    messages = importlib.import_module("bridge.messages")
    assert "{label}" in messages.MODEL_ALREADY_ACTIVE
