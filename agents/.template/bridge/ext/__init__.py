"""DGN-1173: bridge extension seam -- loader + BridgeExtensionAPI v0.

The product boundary made structural: code below this seam is the public
bridge, code registered THROUGH it is an extension.  Extensions live as
subpackages of this package (e.g. bridge/ext/dogany_estate/) and expose

    def register(api: BridgeExtensionAPI) -> None

which the loader calls exactly once per process.

v0 surface: `add_boot_hook(fn)` only.  fn is a zero-argument callable, run
once per process right before the "Bot is running" marker (bot.py first-boot
block).  More seams (commands, interceptors, callback prefixes, markers,
prompt injectables, i18n catalogs -- DGN-1173 spec S3) are added only when
their consumer migrates; nothing is stubbed ahead of proof.

Contracts (each one is enforced by tests/ext/test_dgn1173_ext_loader.py):
  - fail-open discovery: no extension subpackages (or a __path__ entry that
    does not exist on disk) -> zero registrations, silent pass.  This file
    itself SHIPS with the public build; the extraction step excludes only
    the extension subpackages, so "ext absent" means "this directory is
    empty", never "this module is missing".
  - fail-open load: an extension whose import raises, or that lacks a
    register() callable, is skipped with ONE WARNING line; the remaining
    extensions still load.
  - all-or-nothing per extension: if register() raises mid-way, every
    registration that extension already made is rolled back -- a half-wired
    extension is worse than none.
  - ordering: extensions load in sorted-subpackage-name order (what
    pkgutil.iter_modules yields for a file finder); within one extension,
    hooks run in registration (FIFO) order.  Boot hooks must not rely on
    cross-extension ordering beyond that.
  - boot hooks can never block the boot: run_boot_hooks() absorbs any
    exception a hook leaks with ONE WARNING line and keeps going (the
    DGN-986 write_runtime_snapshot contract, generalized -- bot.py boot
    block comment).
"""

import importlib
import logging
import pkgutil
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class BridgeExtensionAPI:
    """Registration surface handed to each extension's register()."""

    def __init__(self) -> None:
        self._boot_hooks: List[Callable[[], None]] = []

    def add_boot_hook(self, fn: Callable[[], None]) -> None:
        """Run fn once per process, right before the "Bot is running" marker.

        fn takes no arguments; its return value is ignored.  Exceptions it
        leaks are absorbed (WARN) by run_boot_hooks -- but a hook that owns a
        failure story should absorb internally and log its own single line,
        like write_runtime_snapshot does.
        """
        self._boot_hooks.append(fn)


_api: Optional[BridgeExtensionAPI] = None


def _load() -> BridgeExtensionAPI:
    """Discover and register every extension subpackage, exactly once."""
    global _api
    if _api is not None:
        return _api
    api = BridgeExtensionAPI()
    # iter_modules tolerates missing/empty path entries: an ext directory
    # with no subpackages simply yields nothing (fail-open discovery).
    for mod_info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not mod_info.ispkg:
            continue  # extensions are packages; stray .py files are not loaded
        name = "%s.%s" % (__name__, mod_info.name)
        undo_mark = len(api._boot_hooks)
        try:
            mod = importlib.import_module(name)
            register = getattr(mod, "register", None)
            if not callable(register):
                logger.warning("extension %s has no register(api); skipped", name)
                continue
            register(api)
        except Exception:
            # Roll back partial registrations, then keep loading the rest.
            del api._boot_hooks[undo_mark:]
            logger.warning(
                "extension %s failed to load; continuing without it",
                name,
                exc_info=True,
            )
    _api = api
    return _api


def boot_hooks() -> tuple:
    return tuple(_load()._boot_hooks)


def run_boot_hooks() -> None:
    """The boot-hook loop bot.py calls in its first-boot block.

    Absorbs everything: a boot hook can never block the boot (one WARNING
    line per failing hook).
    """
    for hook in boot_hooks():
        try:
            hook()
        except Exception:
            logger.warning("boot hook %r failed (absorbed)", hook, exc_info=True)


def _reset_for_tests() -> None:
    """Drop the memoized registry so tests can re-run discovery."""
    global _api
    _api = None
