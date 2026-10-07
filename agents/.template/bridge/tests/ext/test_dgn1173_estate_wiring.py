"""DGN-1173: the real tree wires the estate extension's boot hook.

Split from tests/test_dgn1173_ext_loader.py: the loader contracts are core
(the loader ships extension-less), but THIS test requires the dogany_estate
package, so it is excluded from the public-build extraction with it.
"""

from bridge import ext


def test_real_tree_registers_write_runtime_snapshot():
    ext._reset_for_tests()
    try:
        from bridge.ext.dogany_estate import boot_snapshot

        hooks = ext.boot_hooks()
        assert boot_snapshot.write_runtime_snapshot in hooks
    finally:
        ext._reset_for_tests()
