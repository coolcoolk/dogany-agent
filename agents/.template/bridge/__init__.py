"""bridge: an original Telegram <-> Claude bridge built on claude-agent-sdk."""

# --- version identity (DGN-818 C3) -----------------------------------------
# There are TWO bridge lines, and until C3 they were indistinguishable to any
# machine:
#
#   OSS      coolcoolk/claude-code-telegram   __version__ = "1.1.0"
#   CANON    agents/.template/bridge/         __version__ = "1.0.0"
#
# "1.0.0" was not wrong -- it is the OSS release generation this vendored tree
# descends from (identical bytes at the pinned commit). It was USELESS: it says
# nothing about the ~8,000 canonical lines that never went to OSS, it is three
# OSS commits stale by construction, and the framework repo had ZERO consumers
# of it (DGN-818-DESIGN section 5.1). A shipped bridge could not answer "which
# generation am I".
#
# So the identity is split into its two real halves and recombined as a PEP 440
# local version. `__version__` can now never collide with an OSS one:
#
#   __oss_base__    the OSS release generation at the pin
#   __oss_pin__     the exact upstream commit the vendored tree was taken from
#   __vendor_rev__  the canonical generation marker (a UPSTREAM.md Vendor-rev)
#   __version__     "<oss_base>+dogany.<vendor_rev>"
#
# Lockstep, enforced by tests/dgn818_version_identity_selftest.sh and
# bridge/tests/test_dgn818_version_identity.py: __oss_pin__ MUST equal the
# `Pinned commit` line in UPSTREAM.md, and __vendor_rev__ MUST appear there as
# a Vendor-rev marker. UPSTREAM.md says of itself that "the pin/Vendor-rev
# markers above are PROVENANCE documentation only"; these two constants are the
# first machine that reads them.
__oss_base__ = "1.0.0"
__oss_pin__ = "2c18f0356070b7cd5725c32f063b5e5bdc88a8d6"
__vendor_rev__ = "DGN-818-C3"
__version__ = "%s+dogany.%s" % (__oss_base__, __vendor_rev__)
