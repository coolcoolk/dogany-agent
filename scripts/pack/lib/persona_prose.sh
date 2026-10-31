# persona_prose.sh -- "does the payload prose still address the owner by a
# persona-specific form of address?" predicate. Sourced library (no shebang,
# no side effects). DGN-1526 (compat-lint C5 widening).
#
# THE GAP: compat-lint C5 gate(b) only sees the four unrendered mint TOKENS
# (__AGENT_LABEL__ / __USER_LABEL__ / __AGENT_NAME__ / __USER_NAME__). A
# payload whose prose was written with the publisher's own form of address
# baked in -- the literal the token would have rendered to -- carries no
# token and passed C5. Same class as C-RELNOTE (DGN-1147 A): the token is
# machine-checked, the prose was checked by nobody. This file is the prose
# predicate, shaped like lib/relnote_authorship.sh: one declaration point,
# canary-proven instrument, caller owns the verdict.
#
# VOCABULARY: the finite owner-honorific set of scripts/user-gate-lint.py C3
# (DGN-1506: HONORIFICS) plus DOGANY_HONORIFIC_EXTRA (colon-separated), the
# same extension seam. Declared here as well, not imported: compat-lint.sh
# predates the lint shipping (DGN-1816), and a runtime import would tie the
# pack linter to the release-door lint's layout. Lockstep is by review; the
# DGN-1526 selftest pins that the two sets are equal.
#
# SURFACE: payload prose only -- *.md, *.add (AGENT.md.add / agent.conf.add
# fragments land verbatim in the instance), *.txt under the payload root.
# That is what installs onto a stranger's machine. Code files are gate (h) /
# user-gate-lint C3 territory (publish-side); root release notes and repo
# working docs are C-RELNOTE / out of scope (real-case narration there is
# history, not shipped copy).
#
# THE DISCRIMINATOR -- USE vs MENTION. A form of address fully wrapped in
# quote delimiters is a MENTION (a changelog-style `honorific "X" removed`
# line, a lint rule naming the word). Anything else is USE: the word with a
# particle attached, the word opening a quoted utterance (a cited owner
# sentence is still the owner's form of address shipped to a stranger), the
# word in running text. So: a hit is an occurrence NOT delimited on BOTH
# sides. Delimiters: ASCII " ' ` and the curly/CJK pairs.
#
# MEASURED (2026-09-30, live pack payloads at HEAD of the local checkouts,
# *.md/*.add/*.txt under payload/): dogany-agentpack-dev 0, -ledger 0,
# dogany-lifekit 0, dogany-agentpack-health-trainer 1 -- a TRUE positive:
# payload/skills-bundle/workout-program/reference/placement.md:68, a cited
# owner utterance opening with the honorific, present in the published tag
# pack/pack_health/v0.26.0. That count is why compat-lint runs this gate
# FAIL publish-side but WARN install-side: an install-side FAIL would refuse
# an already-published pack for a defect its installer cannot fix.
#
# KNOWN NON-COVERAGE (enumerated, not hidden):
#   - the reference agent's own NAME in prose (instance names are
#     estate-scrub's class, publish-side);
#   - forms of address outside the finite set (extend via
#     DOGANY_HONORIFIC_EXTRA at publish time);
#   - an honorific written with a space inside or romanized;
#   - code-file string literals (gate (h) / user-gate-lint C3).
#
# LOCALE: the word set and the delimiter classes are multibyte. Under
# LC_ALL=C a bracket expression decomposes into bytes and the gate can go
# blind or misfire, so -- like relnote_authorship.sh -- the predicate is never
# run on trust: persona_prose_locale() picks the first locale in which it
# matches the positive canary AND rejects the negative one; none => the
# caller must fail closed.

PERSONA_PROSE_HONORIFICS='형님 오빠 누나 사장님'

# Per-line waiver, same vocabulary as estate-scrub / C-RELNOTE.
PERSONA_PROSE_ALLOW_RE='estate-scrub:allow\([^)][^)]*\)'

PERSONA_PROSE_LOCALE_CANDIDATES='- C.UTF-8 en_US.UTF-8 ko_KR.UTF-8 UTF-8'

# Synthetic canaries (not owner data). POS = the word used as address with a
# particle; NEG = the same word only MENTIONED, fully quoted.
PERSONA_PROSE_CANARY_POS='- 매일 아침 형님께 브리핑을 보낸다.'
PERSONA_PROSE_CANARY_NEG='- 오너 호칭 "형님" 잔재 58건 제거, 일반 지칭으로 교체'

# persona_prose_words -- the effective word list, one per line.
persona_prose_words() {
  local w
  for w in $PERSONA_PROSE_HONORIFICS; do printf '%s\n' "$w"; done
  printf '%s' "${DOGANY_HONORIFIC_EXTRA:-}" | tr ':' '\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^$' || true
}

# persona_prose_re -- the ERE: an occurrence not wrapped on BOTH sides.
persona_prose_re() {
  local alt
  alt="$(persona_prose_words | paste -sd'|' -)"
  printf '%s' "(^|[^\"'\`“‘「『])($alt)|($alt)([^\"'\`”’」』]|\$)"
}

_persona_prose_grep() {
  local loc="${1:-}"; shift
  if [ -z "$loc" ]; then
    grep "$@"
  else
    LC_ALL="$loc" grep "$@"
  fi
}

# persona_prose_locale -- first locale passing both canaries ('' = inherit).
# Exit 1 = instrument blind everywhere; caller MUST fail closed.
persona_prose_locale() {
  local loc re pos neg
  re="$(persona_prose_re)"
  for loc in $PERSONA_PROSE_LOCALE_CANDIDATES; do
    if [ "$loc" = "-" ]; then loc=""; fi
    pos=1; neg=1
    if printf '%s\n' "$PERSONA_PROSE_CANARY_POS" | _persona_prose_grep "$loc" -qE -e "$re" 2>/dev/null; then pos=0; fi
    if printf '%s\n' "$PERSONA_PROSE_CANARY_NEG" | _persona_prose_grep "$loc" -qE -e "$re" 2>/dev/null; then neg=0; fi
    if [ "$pos" -eq 0 ] && [ "$neg" -ne 0 ]; then
      printf '%s\n' "$loc"
      return 0
    fi
  done
  return 1
}

# persona_prose_surface <payload_dir> -- prose files scanned, payload-relative.
persona_prose_surface() {
  local dir="${1:-}"
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  find "$dir" -type f \( -name '*.md' -o -name '*.add' -o -name '*.txt' \) ! -path '*/.git/*' 2>/dev/null \
    | sed "s|^$dir/||" | LC_ALL=C sort
  return 0
}

# persona_prose_scan <payload_dir> [locale]
# One row per hit: HIT|<rel>|<lineno>|<line> or ALLOW|... (waived line).
# Always returns 0 -- a reporter; the verdict belongs to the caller.
persona_prose_scan() {
  local dir="${1:-}" loc="${2-}" re rel hit ln content
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  re="$(persona_prose_re)"
  persona_prose_surface "$dir" | while IFS= read -r rel; do
    _persona_prose_grep "$loc" -nE -e "$re" "$dir/$rel" 2>/dev/null | while IFS= read -r hit; do
      ln="${hit%%:*}"; content="${hit#*:}"
      if printf '%s\n' "$content" | grep -qE "$PERSONA_PROSE_ALLOW_RE"; then
        printf 'ALLOW|%s|%s|%s\n' "$rel" "$ln" "$content"
      else
        printf 'HIT|%s|%s|%s\n' "$rel" "$ln" "$content"
      fi
    done
  done
  return 0
}
