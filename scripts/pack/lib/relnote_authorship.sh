# relnote_authorship.sh -- "does this release note cite an owner utterance
# instead of stating the reason in the author's own sentence?" predicate.
# Sourced library (no shebang, no side effects). DGN-1147 A.
#
# THE RULE, stated once (packs/CONTRACT.md §6-1c carries the prose 정본):
#   릴리스 노트는 바뀐 것과 그 이유를 작성자 자기 문장으로 쓴다.
# The reason belongs to the WRITER. A release note may name a term it changed
# ("프라이머" -> "세팅"), may cite a commit, may cite a ticket -- what it may
# not do is hand the reason off to a quoted owner utterance and let that stand
# in for the sentence the author owes the reader.
#
# WHY A MACHINE AT ALL: a pack CHANGELOG.md is a SEAL ARTIFACT at the package
# ROOT (pack_publish.sh STEP 4 writes it, :655-668; CONTRACT §6-1 lists it),
# and it installs onto a stranger's machine. Measured 2026-08-28: a published
# pack's root CHANGELOG.md carried, on one line, a change entry whose REASON
# was a verbatim quoted owner utterance in Korean, attributed by name and
# timestamp -- shaped exactly like this file's positive canary below.
# Tracked, published, readable by anyone. pack_publish gate (s) could not see
# it: that gate scans REF_DIR (the payload) and its own header names this
# exact hole -- "Known boundary: seal artifacts at the package root
# (CHANGELOG.md, pack-manifest.json) are outside REF_DIR and not scanned"
# (pack_publish.sh:715-717). This library is what closes it.
#
# WHY NOT estate-scrub.sh (the ONE-judge rule, DGN-1034 형상): that judge is
# STRIP machinery -- no product.yaml `owns` entry, must never ship (its own
# header says so). compat-lint.sh DOES ship (product.yaml:32) and runs on
# arbitrary deployed instances, so it cannot depend on estate-scrub.sh: the
# dependency would be missing exactly where the linter runs. This file is the
# shipping-side declaration point instead, and it stays the ONLY one -- the
# regex is declared here and nowhere else.
#
# WHY NOT the existing estate-scrub `owner-quote-cited` pattern: measured, it
# does not catch this shape. It requires a [:,] juncture immediately before
# the quote (`owner ...: "<한국어>"`), the session-closeout citation grammar it
# was calibrated on. The release-note grammar is attribution + ref/timestamp +
# BARE quote -- no colon at the juncture. Positive control 2026-08-28, both
# lines run against that same pattern:
#   `owner ruling 12:04 "<원문>"`  -> exit 1  (MISS -- the real evidence line)
#   `owner 2026-08-27: "<원문>"`   -> exit 0  (pattern alive, instrument proven)
# A genuinely uncovered class, not a re-implementation.
#
# ---------------------------------------------------------------------------
# THE PREDICATE, and why it is this one
# ---------------------------------------------------------------------------
# Corpus for every number below (measured 2026-08-28): the three live pack-root
# CHANGELOGs (dogany-lifekit / dogany-agentpack-health-trainer /
# dogany-agentpack-dev, 831 lines) + the framework's own release notes
# (releases/*.md 47 files 6220 lines + CHANGELOG.md 2562 lines).
#
# REJECTED -- "따옴표 안 한국어" (any Korean-bearing quote): 103 hit lines.
# That is the 오탐 폭탄. Release notes legitimately quote UI copy, config
# values, and the removed term next to its replacement.
#
# REJECTED -- "attribution + any Korean quote on the same line": 8 hits, 4 of
# them plainly the author's own sentence quoting a TERM it changed
# (`오너 호칭 "형님" 잔재 58건 제거`, `"형님 확정" -> "오너 확정"`). ~50% FP.
#
# ADOPTED -- attribution + quoted UTTERANCE. The discriminator is grammatical,
# not statistical: a quoted TERM is a noun phrase; a quoted UTTERANCE is a
# sentence and ends in a Korean sentence-final ending. Measured with the real
# /usr/bin/grep (BSD grep 2.6.0-FreeBSD). NOT with an interactive shell's grep:
# this session's `grep` was a ugrep 7.8.4 shim, so the first round of numbers
# was taken with a binary the gate never runs. The table below is the re-run.
#
#   predicate                              evid  colon  knownFP  live-roots  corpus
#   any quoted Korean                        1     1      4/4        14        103
#   attribution(proximity) + any quote       1     1      4/4         4          8
#   attribution(proximity) + utterance       1     1      1/4         0          3
#   ADOPTED: attribution(A|B) + utterance    1     1      0/4         0          3
#
#   evid       = the DGN-1147 evidence line fires
#   colon      = `Owner <date>: "<원문>"` still covered
#   knownFP    = how many of the 4 measured author-quoting-a-TERM lines fire
#   live-roots = hits across all three published pack-root CHANGELOGs
#   corpus     = hits across the release-note corpus (3 pack CHANGELOGs 831
#                lines + framework releases/*.md 47 files 6220 lines +
#                framework CHANGELOG.md 2562 lines). The adopted predicate's
#                three are releases/v1.22.2.md:52, v1.27.2.md:82,
#                v1.43.0.md:204 -- three true positives, zero false positives.
#
# ZERO current hits in scope is what makes fail-closed affordable -- see the
# C-RELNOTE block in compat-lint.sh for the side/severity decision.
#
# Gap bound 24: measured, widening 24 -> 80 adds 0 true positives and 2
# out-of-scope false positives. The precision comes from the utterance shape,
# not the window; the window only keeps the attribution GOVERNING the quote
# instead of merely sharing a line with it.
#
# MEASURED RESIDUAL FALSE POSITIVE -- one, and it is out of scope: a design doc
# writing `오너 게이트). B의 안전 전제 = "2.0은 ... 유지"` trips grammar (B):
# an owner-token, an `=`, then a quoted clause the AUTHOR wrote
# (one canonical-side design doc; not a release note). Release notes are not written
# in that register and design docs are not in the scan target, so this is
# RECORDED rather than patched -- narrowing (B) further would cost the
# `Owner <date>: "..."` class, which is worth more.
#
# KNOWN NON-COVERAGE (enumerated, not hidden -- the discipline the canonical-side
# estate-scrub instance-pattern set already sets):
#   - English-only owner quotes (`owner said "just ship it"`): no Korean
#     sentence-ender to key on. Widening to any quoted English clause after an
#     owner token re-opens the 오탐 폭탄 this predicate exists to avoid.
#   - Paraphrase without quotes (`the owner felt it read badly`): outside a
#     lexical gate entirely. The RULE covers it; this machine does not.
#   - Single-quoted or italic-only citation (`owner: '...'`): ' is far too
#     common in prose and code to key on.
#   - 「」/『』-delimited citation: dropped for the ugrep complexity reason
#     above. estate-scrub's owner-quote-cited still carries 」 for the payload.
#   - Attribution by pronoun, or by a name other than owner/오너/형님.
#   - A speech-act word outside the (A) list with no [:,=] juncture either.
#   - Quote and attribution split across two lines (grep is line-scoped).
# These are the rule's honest edge. The RULE in CONTRACT §6-1c is the 정본;
# this gate is a floor under it, not its definition.
#
# ---------------------------------------------------------------------------
# LOCALE -- why this library carries its own canary
# ---------------------------------------------------------------------------
# The pattern's negated classes contain multibyte characters (“ ” 「 」 『 』).
# Under LC_ALL=C a bracket expression decomposes into BYTES, and the excluded
# bytes (0x80 0x8D 0x8F 0x9D ...) are UTF-8 continuation bytes that occur
# inside ordinary Hangul syllables -- the body class then cannot cross its own
# subject. Measured on the evidence line: C.UTF-8 / en_US.UTF-8 / ko_KR.UTF-8
# / inherited => 1 hit; LC_ALL=C => 0 hits. A gate that reports "0 hits"
# because it went blind is the exact failure this estate keeps hitting: "0을
# 봤다" and "볼 수 없었다" printing the same thing.
# So the predicate is never run on trust. relnote_authorship_locale() picks the
# first locale in which the pattern BOTH matches its positive canary AND
# rejects its negative canary; if no candidate does, it returns non-zero and
# the caller must fail closed. Every verdict this library produces is therefore
# accompanied by proof that the instrument was alive when it was produced.

# --- release-note surface at a pack root ------------------------------------
# Root-level release notes only. The payload is gate (s)/C4/C5 territory; repo
# working docs (sot/, crew/) stay out of scope for the same reason
# pack_publish gate (s) leaves them out -- real-case narration there is
# legitimate and scanning it would drown the gate in noise.
RELNOTE_ROOT_FILES="CHANGELOG.md RELEASES.md RELEASE-NOTES.md"
RELNOTE_ROOT_DIRS="releases"

# --- the predicate (ONE declaration point) ----------------------------------
# ATTRIBUTION + <=24 of non-quote gap + a quoted UTTERANCE (a span ending in a
# Korean sentence-final ending, optionally followed by sentence punctuation).
#
# ATTRIBUTION is a union of two grammars, and it is a union because dropping
# either one was measured to cost something real:
#   (A) owner-token + a SPEECH-ACT noun -- `owner ruling`, `형님 지적`,
#       `형님 결정`, `owner approval`, `형님 원문`. This is the release-note
#       grammar: attribution + ref/timestamp + BARE quote, no colon.
#   (B) owner-token + <=20 gap + a [:,=] juncture -- `Owner 2026-08-27: "..."`.
#       This is the grammar estate-scrub's owner-quote-cited owns inside the
#       payload; without (B) the ROOT would silently lose a class the payload
#       already blocks.
# Bare PROXIMITY (owner-token anywhere before the quote) was tried and
# REJECTED on measurement: it fires on `Telling the owner "다음에 ...주세요"`
# -- the owner as ADDRESSEE of quoted product copy, not as speaker. That was
# caught by this file's own regression test (test-relnote-authorship.sh
# P1f-4) before landing, which is what the test is for.
#
# Quote delimiters are ASCII " plus the curly pair “ ”. 「」『』 were tried and
# dropped: they push the two negated classes past ugrep 7.8.4's regex
# complexity limit ("exceeds complexity limits"), and a gate whose grep errors
# out is a gate that reports zero. Cost of the drop: 「인용」-delimited citation
# is not matched -- recorded under KNOWN NON-COVERAGE below.
RELNOTE_CITED_UTTERANCE_RE='(([Oo]wner|오너|형님) ?(ruling|approval|decision|verdict|said|says|원문|발화|지적|확정|결정|판정|룰링|승인|지시|말|코멘트)|([Oo]wner|오너|형님)[^"“”]{0,20}[:,=])[^"“”]{0,24}["“][^"”]*(다|야|자|어|죠|요|네|군|까|지|라|래|해|봐|줘|돼|나)[.!?…~]*["”]'

# --- canaries: the instrument's own liveness proof ---------------------------
# POS is the DGN-1147 evidence shape (must match). NEG is the measured
# false-positive shape the predicate was designed to exclude (must NOT match).
# Both are synthetic strings living only in this file -- they are not owner
# data, they are the fixture that proves the gate can see and can discriminate.
RELNOTE_CANARY_POS='- **A → B** (X `deadbee`, owner ruling 12:04 "나만 이해하는 언어야").'
RELNOTE_CANARY_NEG='- 오너 호칭 "형님" 잔재 58건 제거, 일반 지칭으로 교체'

# Candidate locales, in order. "" = inherit the caller's environment.
# "-" is the sentinel for "inherit the caller's environment" (an empty word
# would be lost to word splitting).
RELNOTE_LOCALE_CANDIDATES='- C.UTF-8 en_US.UTF-8 ko_KR.UTF-8 UTF-8'

# Per-line waiver: same grammar and same token as the canonical-side
# estate-scrub gate, so
# authors learn ONE vocabulary -- append `estate-scrub:allow(<reason>)` to the
# offending line. An empty `allow()` is NOT a waiver. Waived lines are excluded
# from the verdict but always PRINTED by the caller: a waiver leaves a trace
# both in the file and in the gate output.
RELNOTE_ALLOW_RE='estate-scrub:allow\([^)][^)]*\)'

# _relnote_grep <locale> <grep args...>   ("" locale = inherit)
_relnote_grep() {
  local loc="${1:-}"; shift
  if [ -z "$loc" ]; then
    grep "$@"
  else
    LC_ALL="$loc" grep "$@"
  fi
}

# relnote_authorship_locale
# Prints the first locale in which the predicate passes BOTH canaries (empty
# line = "inherit the environment"). Exit 1 = no candidate works; the caller
# MUST fail closed rather than report a clean scan.
relnote_authorship_locale() {
  local loc pos neg
  for loc in $RELNOTE_LOCALE_CANDIDATES; do
    if [ "$loc" = "-" ]; then loc=""; fi
    pos=1; neg=1
    if printf '%s\n' "$RELNOTE_CANARY_POS" \
         | _relnote_grep "$loc" -qE -e "$RELNOTE_CITED_UTTERANCE_RE" 2>/dev/null; then pos=0; fi
    if printf '%s\n' "$RELNOTE_CANARY_NEG" \
         | _relnote_grep "$loc" -qE -e "$RELNOTE_CITED_UTTERANCE_RE" 2>/dev/null; then neg=0; fi
    if [ "$pos" -eq 0 ] && [ "$neg" -ne 0 ]; then
      printf '%s\n' "$loc"
      return 0
    fi
  done
  return 1
}

# relnote_authorship_surface <pack_dir>
# Prints the release-note files actually present at the pack root, one
# pack-relative path per line. The caller prints this: "0 hits" and "scanned
# nothing" must never be the same output.
relnote_authorship_surface() {
  local dir="${1:-}" f d
  [ -n "$dir" ] || return 0
  [ -d "$dir" ] || return 0
  for f in $RELNOTE_ROOT_FILES; do
    [ -f "$dir/$f" ] && printf '%s\n' "$f"
  done
  for d in $RELNOTE_ROOT_DIRS; do
    [ -d "$dir/$d" ] || continue
    find "$dir/$d" -maxdepth 1 -type f -name '*.md' 2>/dev/null \
      | sed "s|^$dir/||" | sort
  done
  return 0
}

# relnote_authorship_scan <pack_dir> [locale]
# Prints one classified row per hit, to stdout:
#   HIT|<pack-relative path>|<lineno>|<line content>
#   ALLOW|<pack-relative path>|<lineno>|<line content>
# `locale` defaults to the caller's environment; callers that care about a
# proven-live instrument pass relnote_authorship_locale()'s answer.
# Always returns 0 -- this is a reporter; the verdict belongs to the caller.
relnote_authorship_scan() {
  local dir="${1:-}" loc="${2-}" f d rel hit ln content
  [ -n "$dir" ] || return 0
  [ -d "$dir" ] || return 0

  {
    for f in $RELNOTE_ROOT_FILES; do
      [ -f "$dir/$f" ] || continue
      _relnote_grep "$loc" -InE -e "$RELNOTE_CITED_UTTERANCE_RE" "$dir/$f" /dev/null 2>/dev/null || true
    done
    for d in $RELNOTE_ROOT_DIRS; do
      [ -d "$dir/$d" ] || continue
      find "$dir/$d" -maxdepth 1 -type f -name '*.md' -print0 2>/dev/null \
        | xargs -0 env ${loc:+LC_ALL="$loc"} \
            grep -InE -e "$RELNOTE_CITED_UTTERANCE_RE" /dev/null 2>/dev/null || true
    done
  } | while IFS= read -r hit; do
      f="${hit%%:*}"; rel="${hit#*:}"
      ln="${rel%%:*}"; content="${rel#*:}"
      case "$f" in "$dir"/*) f="${f#"$dir"/}" ;; esac
      if printf '%s\n' "$content" | grep -qE "$RELNOTE_ALLOW_RE"; then
        printf 'ALLOW|%s|%s|%s\n' "$f" "$ln" "$content"
      else
        printf 'HIT|%s|%s|%s\n' "$f" "$ln" "$content"
      fi
    done
  return 0
}
