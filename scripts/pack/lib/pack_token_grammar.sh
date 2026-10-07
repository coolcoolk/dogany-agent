# pack_token_grammar.sh -- kit/service-namespace token grammar DATA (regex
# core + framework-reserved name list). Sourced library (no shebang, no
# side effects). DGN-1018 RR2.
#
# DATA ONLY. The VALIDATION IMPLEMENTATIONS that consume this stay
# INDEPENDENTLY DUPLICATED on purpose (DGN-1018 fail-open argument):
# compat-lint.sh's install-side gate is WARN+skip when compat-lint.sh is
# absent, so pack_install.sh cannot trust a third-party caller to have run
# it and re-validates on its own (see pack_install.sh's "poisoned-kind
# fail-closed" comment). Logic unification is REJECTED for that reason --
# see scripts/pack/lib/pack_coords.sh's header for the contrast case where
# logic sharing WAS the right call.
#
# What this file removes is a narrower defect: the two implementations were
# hand-typing byte-for-byte copies of the SAME regex text and the SAME
# 10-name reserved list (compat-lint.sh _KIT_TOKEN_RE/_RESERVED_TOKENS,
# pack_install.sh's bash KIT_NAME check, and two separate python heredocs)
# with no single place that forces them to match. That is exactly the shape
# of DGN-1018's two REALIZED drifts (not hypothetical: a bash [[ =~ ]] `$`
# anchor and a python re.match `$` anchor read the same literal differently
# -- python's `$` matches BEFORE a trailing newline, bash's effectively
# doesn't because the value already passed through a newline-stripping
# $(...) capture on that side). Centralizing the SPELLING closes the
# "forgot to update the other copy" class.
#
# ANCHOR NOTE -- the actual reason this file holds a CORE pattern, not a
# fully-anchored one: sharing the anchored bash form ('^...$') into python
# verbatim would SILENTLY REINTRODUCE the trailing-newline bug above (a
# python re.match('$') accepts "ns\n", exactly the divergence DGN-1018 5-B
# fixed by switching the python side to \Z). So anchoring is deliberately
# NOT shared -- each call site wraps PACK_TOKEN_CORE with the anchor form
# correct for its own regex engine (bash: ^...$ ; python: ^...\Z). Sharing
# the core (the character class + length) still eliminates the class of
# defect that WAS observed (a hand-typed copy losing a character), while
# leaving the language-correct anchor a local, reviewable decision.
#
# Consumers:
#   - scripts/pack/compat-lint.sh        (_kit_token_ok)
#   - scripts/pack/pack_install.sh       (KIT_NAME bash check; the
#     service_namespace/db_lane and requires_kit.kit python heredocs,
#     which receive PACK_TOKEN_CORE and the reserved list as argv since a
#     '<<PYEOF' heredoc body cannot see bash variables)
#
# NB: this directory is runtime (exported wholesale via the
# scripts/pack/lib/** glob in product.yaml; publish.sh gate E2b requires
# every file here to be statically referenced by a kept script -- both
# consumers above `. ` this file by a literal path, same as
# persona_resolver.sh / pack_coords.sh).

PACK_TOKEN_CORE='[a-z][a-z0-9_-]{0,31}'
PACK_RESERVED_TOKENS=(bridge memory-engine scripts routines service config database mirror skills-bundle payload)
