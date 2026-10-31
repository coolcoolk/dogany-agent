"""DGN-1173: the Dogany estate extension package.

Everything estate-specific that used to live loose in the bridge core moves
here, one proven seam at a time.  Current residents:

  boot_snapshot  DGN-986 [b] boot runtime snapshot (on_boot hook) -- the
                 DGN-1173 spike slice.
  voice_vocab    DGN-1694 derived voice vocabulary cache refresh (on_boot
                 hook, background thread).

Excluded from the public-build extraction together with the rest of
bridge/ext/*/ (the loader in bridge/ext/__init__.py ships; this package
does not).
"""

from bridge.ext.dogany_estate import boot_snapshot, voice_vocab


def register(api) -> None:
    # write_runtime_snapshot absorbs ALL failures internally (one WARNING
    # line) -- it can never block the boot (DGN-986 [b] contract).
    api.add_boot_hook(boot_snapshot.write_runtime_snapshot)
    api.add_boot_hook(voice_vocab.refresh_voice_vocab)
