"""DGN-1173: extension loader contracts (bridge/ext/__init__.py).

Every claim the loader docstring makes is a test here -- fail-open must be
code, not prose:
  (a) no extensions / missing directory        -> zero registrations, silent
  (b) broken extension (import raises)         -> WARN, others still load
  (c) extension without register()             -> WARN, skipped
  (d) register() raises mid-way                -> that extension's partial
      registrations are rolled back; others keep theirs
  (e) ordering: sorted subpackage name across extensions, FIFO within one
  (f) run_boot_hooks absorbs a raising hook (WARN) and runs the rest
  (g) discovery runs once per process (memoized)
  (h) stray .py files in ext/ are not extensions (packages only)

These are CORE tests: the loader ships with the public build, so its
contracts must hold in a tree with zero extensions.  The estate-wiring
integration check lives in tests/ext/test_dgn1173_estate_wiring.py (excluded
from extraction together with bridge/ext/*/).
"""

import logging
import sys
import textwrap

import pytest

from bridge import ext


@pytest.fixture()
def ext_sandbox(tmp_path, monkeypatch):
    """Point bridge.ext discovery at an empty temp directory, reset after."""
    monkeypatch.setattr(ext, "__path__", [str(tmp_path)])
    ext._reset_for_tests()
    yield tmp_path
    # Drop fake extension modules so later tests re-discover from disk truth.
    for name in [n for n in sys.modules if n.startswith("bridge.ext.")]:
        if "dogany_estate" not in name:
            del sys.modules[name]
    ext._reset_for_tests()


def _make_ext(root, name, body):
    pkg = root / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")


# --- (a) fail-open discovery ----------------------------------------------

def test_empty_ext_dir_yields_zero_hooks(ext_sandbox):
    assert ext.boot_hooks() == ()


def test_missing_path_entry_yields_zero_hooks(ext_sandbox, monkeypatch):
    monkeypatch.setattr(ext, "__path__", [str(ext_sandbox / "does-not-exist")])
    ext._reset_for_tests()
    assert ext.boot_hooks() == ()  # must not raise


def test_run_boot_hooks_with_no_extensions_is_a_noop(ext_sandbox):
    ext.run_boot_hooks()  # must not raise


# --- (b)/(c) broken extensions are skipped with WARN ----------------------

def test_broken_import_warns_and_spares_the_others(ext_sandbox, caplog):
    _make_ext(ext_sandbox, "aa_broken", "raise RuntimeError('boom at import')\n")
    _make_ext(
        ext_sandbox,
        "bb_good",
        """
        def register(api):
            api.add_boot_hook(lambda: None)
        """,
    )
    with caplog.at_level(logging.WARNING, logger="bridge.ext"):
        hooks = ext.boot_hooks()
    assert len(hooks) == 1
    assert any(
        "aa_broken" in r.message and "failed to load" in r.message
        for r in caplog.records
    )


def test_extension_without_register_warns_and_is_skipped(ext_sandbox, caplog):
    _make_ext(ext_sandbox, "aa_mute", "x = 1\n")
    with caplog.at_level(logging.WARNING, logger="bridge.ext"):
        assert ext.boot_hooks() == ()
    assert any("no register" in r.message for r in caplog.records)


# --- (d) register() raising rolls back its partial registrations ----------

def test_register_raising_rolls_back_that_extensions_hooks(ext_sandbox, caplog):
    _make_ext(
        ext_sandbox,
        "aa_partial",
        """
        def register(api):
            api.add_boot_hook(lambda: None)   # would be half-wired
            raise RuntimeError('boom mid-register')
        """,
    )
    _make_ext(
        ext_sandbox,
        "bb_survivor",
        """
        MARK = 'survivor'
        def _hook():
            pass
        def register(api):
            api.add_boot_hook(_hook)
        """,
    )
    with caplog.at_level(logging.WARNING, logger="bridge.ext"):
        hooks = ext.boot_hooks()
    assert len(hooks) == 1  # aa_partial contributed NOTHING
    assert hooks[0].__module__ == "bridge.ext.bb_survivor"
    assert any("aa_partial" in r.message for r in caplog.records)


# --- (e) ordering contract ------------------------------------------------

def test_hooks_run_sorted_by_extension_then_fifo_within(ext_sandbox):
    _make_ext(
        ext_sandbox,
        "bb_second",
        """
        def register(api):
            api.add_boot_hook(lambda log=None: 'b1')
        """,
    )
    _make_ext(
        ext_sandbox,
        "aa_first",
        """
        def _one():
            return 'a1'
        def _two():
            return 'a2'
        def register(api):
            api.add_boot_hook(_one)
            api.add_boot_hook(_two)
        """,
    )
    hooks = ext.boot_hooks()
    assert [h() for h in hooks] == ["a1", "a2", "b1"]


# --- (f) a raising boot hook cannot block the boot ------------------------

def test_run_boot_hooks_absorbs_and_continues(ext_sandbox, caplog):
    ran = []
    api = ext._load()
    api.add_boot_hook(lambda: (_ for _ in ()).throw(RuntimeError("hook boom")))
    api.add_boot_hook(lambda: ran.append("after"))
    with caplog.at_level(logging.WARNING, logger="bridge.ext"):
        ext.run_boot_hooks()  # must not raise
    assert ran == ["after"]
    assert any("failed (absorbed)" in r.message for r in caplog.records)


# --- (g) discovery is once-per-process ------------------------------------

def test_discovery_is_memoized(ext_sandbox):
    _make_ext(
        ext_sandbox,
        "aa_once",
        """
        def register(api):
            api.add_boot_hook(lambda: None)
        """,
    )
    first = ext.boot_hooks()
    _make_ext(
        ext_sandbox,
        "zz_late",
        """
        def register(api):
            api.add_boot_hook(lambda: None)
        """,
    )
    assert ext.boot_hooks() == first  # late arrival NOT picked up mid-process


# --- (h) packages only ----------------------------------------------------

def test_stray_module_file_is_not_an_extension(ext_sandbox):
    (ext_sandbox / "loose.py").write_text(
        "def register(api):\n    api.add_boot_hook(lambda: None)\n",
        encoding="utf-8",
    )
    assert ext.boot_hooks() == ()
