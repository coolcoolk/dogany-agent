"""DGN-1173: the Dogany estate extension package.

Everything estate-specific that used to live loose in the bridge core moves
here, one proven seam at a time.  Current residents:

  boot_snapshot  DGN-986 [b] boot runtime snapshot (on_boot hook) -- the
                 DGN-1173 spike slice.

Excluded from the public-build extraction together with the rest of
bridge/ext/*/ (the loader in bridge/ext/__init__.py ships; this package
does not).
"""

from bridge.ext.dogany_estate import boot_snapshot


def register(api) -> None:
    # write_runtime_snapshot absorbs ALL failures internally (one WARNING
    # line) -- it can never block the boot (DGN-986 [b] contract).
    api.add_boot_hook(boot_snapshot.write_runtime_snapshot)
