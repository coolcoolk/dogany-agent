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
# DGN-785 additions:
#   T8  ko_try key parsed correctly
#   T9  all three bracket labels present when all keys populated
#   T10 ko_try absent -> no third section (byte-parity)
#   T11 DGN-NNN in body -> ticket-strip removes all DGN- markers
#   T12 all three keys empty -> no blockquote
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
cat > "$WORK/full.md" <<'EOF'
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

# DGN-785: fixture with all three keys (ko + ko_detail + ko_try).
THREE_KEYS="$WORK/three_keys.md"
cat > "$THREE_KEYS" <<'EOF'
# v9.9.9 -- Three-key fixture

Date: 2026-08-04

<!-- user-summary
ko: |
  요약 한 줄.
ko_detail: |
  · 상세 항목1
  · 상세 항목2
ko_try: |
  · 이렇게 써보세요
en: |
  Summary line.
en_detail: |
  · Detail item1
en_try: |
  · Try this feature
-->
EOF

# DGN-785: fixture with ko/ko_detail but NO ko_try (parity with pre-785).
NO_TRY="$WORK/no_try.md"
cat > "$NO_TRY" <<'EOF'
<!-- user-summary
ko: |
  요약만 있음.
ko_detail: |
  · 상세만 있음.
-->
EOF

# DGN-785: fixture with DGN ticket numbers embedded in body text.
TICKET_BODY="$WORK/ticket_body.md"
cat > "$TICKET_BODY" <<'EOF'
<!-- user-summary
ko: |
  업데이트 완료 DGN-784 적용.
ko_detail: |
  · 상세 (DGN-784) 변경사항.
ko_try: |
  · 기능 DGN-785 사용해보세요.
-->
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
# DGN-785 tests
# strip_tickets mirrors the function defined inside maybe_restart() in
# self-update.sh. Defined here inline (pure sed) so the test does not need
# to source a nested function from inside another function body.
strip_tickets() {
  printf '%s\n' "$1" \
    | sed -e 's/([[:space:]]*DGN-[0-9][0-9]*[^)]*[[:space:]]*)//g' \
          -e 's/[[:space:]]*DGN-[0-9][0-9]*//g' \
          -e 's/  */ /g' \
          -e 's/[[:space:]]*--[[:space:]]*$//' \
          -e 's/^[[:space:]]*//'
}
html_esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

# Helper: simulate the DGN-785 three-section notice body build for a given
# releasefile + lang. Returns the blockquote body text (without the header
# line and without the outer <blockquote> tags) so tests can grep it cleanly.
build_notice_body() {  # <releasefile> <lang>
  _bnb_file="$1"
  _bnb_lang="$2"
  if [ "$_bnb_lang" = "ko" ]; then
    _bnb_ls="[업데이트 요약]"
    _bnb_ld="[업데이트 상세]"
    _bnb_lt="[새로 해볼 수 있는 것]"
  else
    _bnb_ls="[At a glance]"
    _bnb_ld="[What changed]"
    _bnb_lt="[Try this]"
  fi
  _bnb_notes="$(strip_tickets "$(user_summary "$_bnb_file" "$_bnb_lang")")"
  _bnb_detail="$(strip_tickets "$(user_summary "$_bnb_file" "${_bnb_lang}_detail")")"
  _bnb_try="$(strip_tickets "$(user_summary "$_bnb_file" "${_bnb_lang}_try")")"
  _bnb_body=""
  if [ -n "$_bnb_notes" ]; then
    _bnb_body="${_bnb_ls}
$(printf '%s\n' "$_bnb_notes" | html_esc)"
  fi
  if [ -n "$_bnb_detail" ]; then
    _bnb_detail_body="$(printf '%s\n' "$_bnb_detail" | html_esc)"
    if [ -n "$_bnb_body" ]; then
      _bnb_body="${_bnb_body}

${_bnb_ld}
${_bnb_detail_body}"
    else
      _bnb_body="${_bnb_ld}
${_bnb_detail_body}"
    fi
  fi
  if [ -n "$_bnb_try" ]; then
    _bnb_try_body="$(printf '%s\n' "$_bnb_try" | html_esc)"
    if [ -n "$_bnb_body" ]; then
      _bnb_body="${_bnb_body}

${_bnb_lt}
${_bnb_try_body}"
    else
      _bnb_body="${_bnb_lt}
${_bnb_try_body}"
    fi
  fi
  printf '%s\n' "$_bnb_body"
}

# ===========================================================================
CURRENT="T8"; say "T8: ko_try key parsed correctly by user_summary"
OUT="$(user_summary "$THREE_KEYS" ko_try)"
if printf '%s' "$OUT" | grep -q '이렇게 써보세요'; then
  ok "ko_try key extracted correctly"
else
  bad "ko_try extraction wrong: [$OUT]"
fi
if printf '%s' "$OUT" | grep -q 'Detail item'; then
  bad "ko_try leaked ko_detail content"
else
  ok "ko_try excludes ko_detail content"
fi

# ===========================================================================
CURRENT="T9"; say "T9: all three bracket labels present when all keys populated"
OUT="$(build_notice_body "$THREE_KEYS" ko)"
if printf '%s' "$OUT" | grep -qF '[업데이트 요약]'; then
  ok "ko summary label [업데이트 요약] present"
else
  bad "ko summary label missing: [$OUT]"
fi
if printf '%s' "$OUT" | grep -qF '[업데이트 상세]'; then
  ok "ko detail label [업데이트 상세] present"
else
  bad "ko detail label missing: [$OUT]"
fi
if printf '%s' "$OUT" | grep -qF '[새로 해볼 수 있는 것]'; then
  ok "ko try label [새로 해볼 수 있는 것] present"
else
  bad "ko try label missing: [$OUT]"
fi

# ===========================================================================
CURRENT="T10"; say "T10: ko_try absent -> no third section (parity)"
OUT="$(build_notice_body "$NO_TRY" ko)"
if printf '%s' "$OUT" | grep -qF '[새로 해볼 수 있는 것]'; then
  bad "try label should be absent when ko_try key missing: [$OUT]"
else
  ok "ko_try absent -> third section omitted"
fi
if printf '%s' "$OUT" | grep -qF '[업데이트 요약]' \
   && printf '%s' "$OUT" | grep -qF '[업데이트 상세]'; then
  ok "first two sections still present without ko_try"
else
  bad "parity: first two sections missing when only ko_try absent: [$OUT]"
fi

# ===========================================================================
CURRENT="T11"; say "T11: DGN-NNN in body -> ticket-strip removes all DGN- markers"
OUT="$(build_notice_body "$TICKET_BODY" ko)"
if printf '%s' "$OUT" | grep -q 'DGN-'; then
  bad "DGN- ticket marker leaked to rendered output: [$OUT]"
else
  ok "ticket-strip removed all DGN- markers from all three sections"
fi
# Verify the non-ticket content survives the strip.
if printf '%s' "$OUT" | grep -q '업데이트 완료'; then
  ok "non-ticket content survives ticket-strip"
else
  bad "ticket-strip removed legitimate content: [$OUT]"
fi

# ===========================================================================
CURRENT="T12"; say "T12: all three keys empty -> no blockquote body"
EMPTY_FILE="$WORK/empty_keys.md"
cat > "$EMPTY_FILE" <<'EOF'
<!-- user-summary
ko: |
en: |
-->
EOF
OUT="$(build_notice_body "$EMPTY_FILE" ko)"
if [ -z "$OUT" ]; then
  ok "all keys empty -> no blockquote body (no fold)"
else
  bad "expected empty body when all keys absent, got: [$OUT]"
fi

# ===========================================================================
say ""
say "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
