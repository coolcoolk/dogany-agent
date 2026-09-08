# payload_seeded.sh -- "does this payload root carry real content?" predicate.
# Sourced library (no shebang, no side effects). DGN-1045 follow-up.
#
# THE RULE, stated once: a payload root is SEEDED when it exists and holds at
# least one file that is not a `.gitkeep` scaffold. Nothing about WHICH files
# -- the allowlist of legal payload paths is compat-lint C4's job, and the
# publish refinement gates (a)/(b)/(c) judge their content. This predicate
# answers only "is there anything here at all".
#
# WHY THIS FILE EXISTS: the rule used to live only inside compat-lint.sh
# (_payload_seeded, the C3/C6 skip predicate) while pack_publish.sh's finalize
# gate G-F1 hand-rolled a SECOND, narrower answer to the same question: it
# looked for `<refslug>/AGENT.md.add` or a non-empty `<refslug>/{skills,
# routines,scripts,knowledge}/`. That list is the LEGACY agent/module payload
# shape. A contract pack (kind=kit|pack + contract_version) puts its payload
# under `payload/service/<ns>/`, `payload/database/`, `payload/config/`,
# `payload/skills-bundle/` -- none of which appear in that list. Measured
# 2026-08-25: `packs/dev` (kind=pack, contract_version=1, payload seeded with
# 4 files under payload/service/dev/) was refused by G-F1 as "payload has no
# sealable content", so the publisher could not seal a pack the contract
# linter considers fully seeded. Two answers to one question, and the machine
# that must agree with itself did not.
#
# SHAPE: shared LOGIC (a sourced function), the pack_coords.sh case, not the
# pack_token_grammar.sh case. There is no fail-open cover argument here --
# G-F1 is a "is there anything to seal" precondition on the publish side, not
# a security gate that a third party could route around, so the two sides must
# agree BY CONSTRUCTION rather than by two implementations that happen to match.
#
# NB: this directory is runtime (exported via the `scripts/pack/lib/**` glob
# in product.yaml; publish.sh gate E2b requires every file here to be
# statically referenced by a KEPT script -- compat-lint.sh's
# `. "$PAYLOAD_SEEDED_LIB"` is that reference). pack_publish.sh is STRIP dev
# machinery and may depend on runtime lib, exactly as it already does for
# lib/pack_coords.sh and lib/persona_resolver.sh.

# pack_payload_seeded <payload_dir>
# exit 0 = seeded, exit 1 = not seeded (absent dir, or only .gitkeep files).
pack_payload_seeded() {
  local dir="${1:-}"
  [ -n "$dir" ] || return 1
  [ -d "$dir" ] || return 1
  local n
  n="$(find "$dir" -type f ! -name '.gitkeep' | grep -c . || true)"
  [ "${n:-0}" -gt 0 ]
}
