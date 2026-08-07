#!/bin/sh
# dgn699_user_summary_selftest.sh -- DGN-699 Spec A user-facing summary field.
#
# Covers the notice-body source swap in routines/self-update.sh:
#   T1 ko block present                 -> ko summary extracted
#   T2 en block present                 -> en summary extracted
#   T3 requested lang key absent        -> empty (caller falls back)
#   T4 whole user-summary block absent  -> empty (old release -> CHANGELOG)
#   T5 lang resolution ko/en select the right block (fallback chain proxy)
#   T6 the REAL v1.26.0.md ships the locked ko/en copy
#   T7 notice body wiring: user_summary wins over changelog_section when a
#      block exists; falls back to changelog_section when it does not
#
# The REAL shipped helpers are sourced out of routines/self-update.sh (exact
# line ranges), never re-typed, so the test exercises the shipped code as-is.
#
# SAFETY: pure text extraction against fixture files under a private mktemp
# WORK dir + the tracked releases/*.md. No network, no launchd, no restart.
set -u

SANDBOX="$(cd "$(dirname "$0")/.." && pwd)"
SU="$SANDBOX/agents/.template/routines/self-update.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn699-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""
say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }

# ---- source the REAL helpers out of the shipped script (no re-typing). ------
# Extract each function body by its stable start/end anchors and dot-source it,
# so a drift in the shipped extraction logic is caught here.
HELPERS="$WORK/helpers.sh"
awk '/^user_summary\(\) \{/{g=1} g{print} /^\}/{if(g){g=0}}' "$SU" > "$HELPERS"
awk '/^changelog_section\(\) \{/{g=1} g{print} /^\}/{if(g){g=0}}' "$SU" >> "$HELPERS"
# shellcheck source=/dev/null
. "$HELPERS"

# ---- fixtures ---------------------------------------------------------------
FULL="$WORK/full.md"
cat > "$FULL" <<'EOF'
# v9.9.9 -- Fixture

Date: 2026-08-04

<!-- user-summary
ko: |
  한글 요약 첫줄.
  한글 요약 둘째줄.
en: |
  English summary line one.
  English summary line two.
-->

## Purpose
Body text.
EOF

NOBLOCK="$WORK/noblock.md"
cat > "$NOBLOCK" <<'EOF'
# v9.9.8 -- Old fixture (pre Spec A)

Date: 2026-08-01

## Purpose
No user-summary block here.
EOF

KO_ONLY="$WORK/koonly.md"
cat > "$KO_ONLY" <<'EOF'
<!-- user-summary
ko: |
  오직 한글만.
-->
EOF

# ===========================================================================
CURRENT="T1"; say "T1: ko block present -> ko extracted"
OUT="$(user_summary "$FULL" ko)"
if printf '%s' "$OUT" | grep -q '한글 요약 첫줄.' \
   && printf '%s' "$OUT" | grep -q '한글 요약 둘째줄.'; then
  ok "both ko lines extracted"
else
  bad "ko extraction wrong: [$OUT]"
fi
if printf '%s' "$OUT" | grep -q 'English'; then
  bad "ko extraction leaked en content"
else
  ok "ko extraction excludes en block"
fi

# ===========================================================================
CURRENT="T2"; say "T2: en block present -> en extracted"
OUT="$(user_summary "$FULL" en)"
if printf '%s' "$OUT" | grep -q 'English summary line one.' \
   && printf '%s' "$OUT" | grep -q 'English summary line two.'; then
  ok "both en lines extracted"
else
  bad "en extraction wrong: [$OUT]"
fi
if printf '%s' "$OUT" | grep -q '한글'; then
  bad "en extraction leaked ko content"
else
  ok "en extraction excludes ko block"
fi

# ===========================================================================
CURRENT="T3"; say "T3: requested lang key absent -> empty"
OUT="$(user_summary "$KO_ONLY" en)"
if [ -z "$OUT" ]; then
  ok "missing en key yields empty (caller falls back)"
else
  bad "expected empty, got: [$OUT]"
fi

# ===========================================================================
CURRENT="T4"; say "T4: whole user-summary block absent -> empty"
OUT="$(user_summary "$NOBLOCK" ko)"
if [ -z "$OUT" ]; then
  ok "no block yields empty (old release -> CHANGELOG fallback)"
else
  bad "expected empty, got: [$OUT]"
fi
OUT="$(user_summary "$WORK/does-not-exist.md" ko)"
if [ -z "$OUT" ]; then
  ok "missing file yields empty (robust to absence)"
else
  bad "missing file should be empty, got: [$OUT]"
fi

# ===========================================================================
CURRENT="T5"; say "T5: lang selects the matching block (ko != en)"
KO="$(user_summary "$FULL" ko)"
EN="$(user_summary "$FULL" en)"
if [ "$KO" != "$EN" ] && [ -n "$KO" ] && [ -n "$EN" ]; then
  ok "ko and en resolve to distinct non-empty summaries"
else
  bad "lang resolution did not distinguish ko/en (ko=[$KO] en=[$EN])"
fi

# ===========================================================================
CURRENT="T6"; say "T6: shipped releases/v1.26.0.md carries the locked copy"
REL="$SANDBOX/releases/v1.26.0.md"
if [ -f "$REL" ]; then
  KO="$(user_summary "$REL" ko)"
  EN="$(user_summary "$REL" en)"
  case "$KO" in
    *"내부 안전장치를 추가"*"눈에 보이는 기능 변화는 없습니다"*)
      ok "v1.26.0 ko summary matches locked copy" ;;
    *) bad "v1.26.0 ko summary drifted: [$KO]" ;;
  esac
  case "$EN" in
    *"internal safeguards"*"No visible feature changes"*)
      ok "v1.26.0 en summary matches locked copy" ;;
    *) bad "v1.26.0 en summary drifted: [$EN]" ;;
  esac
  # Jargon gate (spec content rule): no internal component names, no ticket ids.
  if printf '%s%s' "$KO" "$EN" | grep -qiE 'bridge|canonical|reconcile|DGN-[0-9]'; then
    bad "v1.26.0 summary leaked internal jargon / ticket ids"
  else
    ok "v1.26.0 summary is jargon-free (no component names / ticket ids)"
  fi
else
  bad "releases/v1.26.0.md not found at $REL"
fi

# ===========================================================================
CURRENT="T7"; say "T7: notice body wiring -- user_summary wins, else changelog"
# Reproduce the maybe_restart() body-source selection (self-update.sh) exactly:
#   _notes = user_summary(release); if empty -> changelog_section(CHANGELOG).
CL="$WORK/CHANGELOG.md"
cat > "$CL" <<'EOF'
## [9.9.9] - 2026-08-04
### Added
- English changelog body for 9.9.9.

## [9.9.8] - 2026-08-01
- older.
EOF
notice_body() {   # <releasefile> <changelog> <ver-plain> <lang>
  _n="$(user_summary "$1" "$4")"
  [ -n "$_n" ] || _n="$(changelog_section "$2" "$3" | head -n 30)"
  printf '%s\n' "$_n"
}
OUT="$(notice_body "$FULL" "$CL" 9.9.9 ko)"
if printf '%s' "$OUT" | grep -q '한글 요약 첫줄.' \
   && ! printf '%s' "$OUT" | grep -q 'English changelog body'; then
  ok "block present -> user_summary(ko) wins over CHANGELOG scrape"
else
  bad "block present but wiring did not prefer user_summary: [$OUT]"
fi
OUT="$(notice_body "$NOBLOCK" "$CL" 9.9.9 ko)"
if printf '%s' "$OUT" | grep -q 'English changelog body for 9.9.9.'; then
  ok "block absent -> falls back to changelog_section(English)"
else
  bad "fallback to CHANGELOG did not fire: [$OUT]"
fi

# ===========================================================================
say ""
say "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
