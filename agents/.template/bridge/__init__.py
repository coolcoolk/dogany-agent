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
# DGN-1276 C9: `__oss_base__` is "the OSS release generation AT THE PIN", so it
# moves with the pin. It stayed at "1.0.0" after the pin advanced to the OSS
# 1.1.0 release commit, and since it is what the public build's `__version__`
# binds to (the ELSE body below), a generated bridge published a version that
# went BACKWARDS, 1.1.0 -> 1.0.0. It stays OUTSIDE the region for exactly that
# reason: it is the one identity half the public tree still needs.
#
# DGN-1276 D2 (owner decision 2026-09-09, D1 = "2.0.0"): with the first
# GENERATED publish the direction of this constant inverts. Up to now it
# trailed the pin -- the OSS release this vendored tree was copied FROM. From
# the generated line on it LEADS the pin: the public tree is no longer copied
# in, it is emitted, and this is the version that emission publishes. The pin
# below was deliberately NOT moved by that bump (it would have claimed a
# provenance that did not exist yet); it re-anchors once the release commit
# exists. That happened on 2026-09-09: 2.0.0 and 2.0.1 were both published,
# and a447c62f re-anchored `- Pinned commit:` in UPSTREAM.md to 09c20541
# (OSS 2.0.1) -- but left THIS line on the old c4bd9df1, which broke the
# lockstep test the two lines exist for. Re-anchored here to match.
#
# There IS a machine reading this: the publish gate refuses to land a tree
# whose own `__version__` disagrees with the version being published, because
# a published tree that misreports its version cannot be fixed after the fact.
#
# DGN-1362 (OSS 2.0.1): 2.0.0 shipped without `/usage` -- not a product call, a
# side effect of the routine it runs living outside the generator's --canon
# root. Restoring the command is user-visible behaviour on an already-published
# surface, so the emission needs its own number. PATCH, not MINOR: 2.0.0's own
# changelog announced the removal as a breaking change, and this reverses that
# announcement rather than adding anything new.
__oss_base__ = "2.0.1"
# ESTATE:BEGIN the provenance pair and the `+dogany.` composed version are canonical-only (DGN-818 C3); the public build carries the OSS base alone
__oss_pin__ = "09c20541acd4cfe90462886d49937e4405ccd9c6"
__vendor_rev__ = "dgn1408-btw-stray-result-20260911"
__version__ = "%s+dogany.%s" % (__oss_base__, __vendor_rev__)
# ESTATE:ELSE
# __version__ = __oss_base__
# ESTATE:END
