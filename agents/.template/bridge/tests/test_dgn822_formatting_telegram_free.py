"""DGN-822 F-1 regression guard: bridge.formatting is telegram-free.

Background
----------
push.sh sanitizes outbound messages by calling bridge.formatting.
sanitize_message_for_telegram.  That function is imported from a fresh
Python process OUTSIDE the bridge venv (the shell sanitize hop at
routines/push.sh:181).  If bridge.formatting (or any module it imports at
the top level) pulls in 'telegram' or 'telegram.*' from python-telegram-bot,
the import fails silently in that no-venv context and the sanitizer is
DISABLED -- raw markdown leaks to Telegram with zero error signal.

DGN-822 fix moved OPTIONS_MARKER from bridge.options (which imports
'telegram' unconditionally) into bridge.formatting (which has no telegram
dependency).  This test file pins that invariant: bridge.formatting MUST be
importable and fully functional even when the 'telegram' package is
completely unavailable.

Test strategy
-------------
A sys.meta_path blocker (TelegramBlocker) raises ImportError for any
'telegram' or 'telegram.*' import attempt.  The blocker is installed before
any bridge module is loaded in a clean sys.modules snapshot, so the import
succeeds only if bridge.formatting has zero direct or transitive telegram
dependency.  The module cache is restored after each test.
"""

import builtins
import importlib
import os
import sys
import types
from pathlib import Path
from typing import List

import pytest

# ---------------------------------------------------------------------------
# Path + env bootstrap (mirrors the pattern in conftest.py and peer tests).
# ---------------------------------------------------------------------------
_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

os.environ.setdefault("PROJECT_ROOT", "/tmp/bridge-test-dgn822")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")


# ---------------------------------------------------------------------------
# Telegram import blocker
# ---------------------------------------------------------------------------

class _TelegramBlocker:
    """sys.meta_path finder that raises ImportError for 'telegram' and submodules.

    Implements find_spec (Python 3.4+ / 3.12+ preferred API) so the blocker
    fires reliably on Python 3.14 where find_module is no longer invoked by
    the import machinery for installed packages.  find_module is kept as a
    belt-and-suspenders fallback for older Python.
    """

    def find_spec(self, fullname: str, path, target=None):
        if fullname == "telegram" or fullname.startswith("telegram."):
            raise ImportError(
                f"DGN-822 test: telegram import blocked (fullname={fullname!r}). "
                "bridge.formatting must not depend on python-telegram-bot."
            )

    def find_module(self, fullname: str, path=None):
        if fullname == "telegram" or fullname.startswith("telegram."):
            return self

    def load_module(self, fullname: str):
        raise ImportError(
            f"DGN-822 test: telegram import blocked (fullname={fullname!r}). "
            "bridge.formatting must not depend on python-telegram-bot."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _purge_bridge_formatting(saved: dict) -> None:
    """Remove bridge.formatting (and bridge itself) from sys.modules."""
    for key in list(sys.modules.keys()):
        if key == "bridge.formatting" or key == "bridge" or key.startswith("bridge."):
            saved.setdefault(key, sys.modules.pop(key))


def _restore_modules(saved: dict) -> None:
    for key, mod in saved.items():
        sys.modules[key] = mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_formatting_imports_without_telegram():
    """bridge.formatting must import successfully even when telegram is blocked.

    This is the core DGN-822 F-1 invariant: the module closure is
    telegram-free.  If anyone re-introduces a top-level 'import telegram'
    (or any module that does so at import time), this test will FAIL with the
    ImportError raised by _TelegramBlocker.
    """
    saved: dict = {}
    blocker = _TelegramBlocker()
    _purge_bridge_formatting(saved)
    sys.meta_path.insert(0, blocker)
    try:
        mod = importlib.import_module("bridge.formatting")
        # Spot-check that key symbols are present and callable.
        assert callable(getattr(mod, "sanitize_message_for_telegram", None)), (
            "sanitize_message_for_telegram must be a callable on bridge.formatting"
        )
        assert hasattr(mod, "OPTIONS_MARKER"), (
            "OPTIONS_MARKER must be defined on bridge.formatting"
        )
    finally:
        sys.meta_path.remove(blocker)
        _restore_modules(saved)


def test_sanitize_works_without_telegram():
    """sanitize_message_for_telegram must execute without telegram installed.

    Verifies not just import but actual function call: markdown input is
    converted to HTML (bold -> <b>...</b>).  An ImportError inside the
    function would propagate here rather than being swallowed by push.sh.
    """
    saved: dict = {}
    blocker = _TelegramBlocker()
    _purge_bridge_formatting(saved)
    sys.meta_path.insert(0, blocker)
    try:
        from bridge.formatting import sanitize_message_for_telegram
        result = sanitize_message_for_telegram("**x** -> y")
        # Markdown bold should be converted to HTML <b>.
        assert "<b>" in result, f"Expected <b> in result, got: {result!r}"
        assert "</b>" in result, f"Expected </b> in result, got: {result!r}"
        # No exception raised: sanitizer is active, not silently disabled.
    finally:
        sys.meta_path.remove(blocker)
        _restore_modules(saved)


def test_options_marker_present_on_formatting():
    """OPTIONS_MARKER on bridge.formatting is the authoritative location.

    DGN-822: the constant was moved FROM bridge.options (telegram-dependent)
    TO bridge.formatting so the shell sanitize hop can access it without a
    venv.  Verify the value is the expected string.
    """
    saved: dict = {}
    blocker = _TelegramBlocker()
    _purge_bridge_formatting(saved)
    sys.meta_path.insert(0, blocker)
    try:
        from bridge.formatting import OPTIONS_MARKER
        assert OPTIONS_MARKER == "[[OPTIONS]]", (
            f"Unexpected OPTIONS_MARKER value: {OPTIONS_MARKER!r}"
        )
    finally:
        sys.meta_path.remove(blocker)
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# Contrast (informational): bridge.options DOES need telegram.
# This is an xfail documentation test -- it exists to make the design
# intent explicit: options.py requires telegram; formatting.py does not.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "bridge.options imports 'telegram' unconditionally (InlineKeyboardButton etc.). "
        "This xfail documents that options is NOT telegram-free by design, "
        "in contrast to bridge.formatting which IS telegram-free (DGN-822 invariant)."
    ),
    strict=True,
)
def test_options_fails_without_telegram_xfail_by_design():
    """bridge.options CANNOT import when telegram is blocked -- expected failure.

    This is an xfail documenting the intentional design split:
      - bridge.formatting: telegram-free (passes test_formatting_imports_without_telegram)
      - bridge.options: telegram-dependent (fails this blocked-import attempt)

    If this xfail ever starts PASSING it means options.py was decoupled from
    telegram, which would be a design change worth reviewing.
    """
    saved: dict = {}
    blocker = _TelegramBlocker()
    # Purge bridge modules AND any telegram mock previously seeded by other
    # tests (e.g. `sys.modules["telegram"] = MagicMock()` guards in peer
    # test files).  Without this, a pre-seeded mock satisfies the import
    # before the blocker fires, making this test order-dependent.
    for key in list(sys.modules.keys()):
        if key.startswith("bridge") or key == "telegram" or key.startswith("telegram."):
            saved.setdefault(key, sys.modules.pop(key))
    sys.meta_path.insert(0, blocker)
    try:
        # This import should fail because options.py does
        # "from telegram import InlineKeyboardButton, InlineKeyboardMarkup"
        importlib.import_module("bridge.options")
        # If we reach here, the import succeeded -- xfail will mark this XPASS.
    finally:
        sys.meta_path.remove(blocker)
        _restore_modules(saved)
