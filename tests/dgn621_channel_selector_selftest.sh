#!/bin/bash
# dgn621_channel_selector_selftest.sh -- DGN-621 v2 phase1 self-test.
#
# Covers resolve_channel_tag (the channel-aware update-target selector added to
# both update.sh and agents/.template/routines/self-update.sh). Channels are
# distinguished by TAG SUFFIX: release = highest STABLE tag (pre-release '-*'
# excluded), dev = highest tag over the whole 'v*' set. DOGANY_UPDATE_PIN
# overrides channel and MUST fail loud when the pinned tag is missing.
#
# The function is EXTRACTED from the shipped scripts (sed between the
# 'resolve_channel_tag()' opener and its closing brace) and sourced here, so
# the test exercises the real shipped code, not a re-typed copy. Both copies
# are extracted and tested independently (they must stay behavior-identical).
#
# SAFETY: every scenario runs against throwaway git repos with crafted tags
# under a private mktemp WORK dir. The real repo's tag set is never consulted.
# No network, no remotes.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn621-test.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }
assert_eq() { # assert_eq <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2] got [$3])"; fi
}

# ---------------------------------------------------------------------------
# Extract resolve_channel_tag() from a shipped script into a sourceable snippet.
# The function body is bracketed by the 'resolve_channel_tag() {' line and the
# first line that is a lone '}' at column 0 (the function's closing brace).
# Also provide the msg/die helpers the function depends on (die must EXIT so
# the pin-miss failure is observable).
# ---------------------------------------------------------------------------
extract_fn() { # $1 = path to shipped script; prints a self-contained snippet
  printf '%s\n' 'msg() { printf "%s\n" "$2" >&2; }'
  printf '%s\n' 'die() { printf "[ERROR] %s\n" "$1" >&2; exit 1; }'
  sed -n '/^resolve_channel_tag() {/,/^}/p' "$1"
}

# Run the extracted selector in a subshell (so die's exit does not kill us).
# Sets: OUT (stdout tag), RC (exit code).
run_selector() { # $1 snippet-file, $2 repo, $3 channel, $4 pin(optional)
  local snip="$1" repo="$2" chan="$3" pin="${4-}"
  OUT="$(
    # shellcheck disable=SC1090
    . "$snip"
    if [ -n "$pin" ]; then DOGANY_UPDATE_PIN="$pin"; export DOGANY_UPDATE_PIN; fi
    resolve_channel_tag "$repo" "$chan"
  )"
  RC=$?
}

# Old one-liner (pre-DGN-621) -- the regression oracle for the stable-only case.
old_oneliner() { # $1 repo
  git -C "$1" tag --list 'v*' --sort=-v:refname | head -n1
}

mkrepo() { # $1 dir; then tags passed as $2..$N are created on a single commit
  local r="$1"; shift
  mkdir -p "$r"
  git -C "$r" init -q -b main
  printf 'seed\n' > "$r/seed.txt"
  git -C "$r" -c user.email=t@t -c user.name=t add -A
  git -C "$r" -c user.email=t@t -c user.name=t commit -qm seed
  local t
  for t in "$@"; do git -C "$r" tag "$t"; done
}

# Extract both shipped copies up front.
SNIP_UPDATE="$WORK/fn_update.sh"
SNIP_SELF="$WORK/fn_self.sh"
extract_fn "$SANDBOX/update.sh" > "$SNIP_UPDATE"
extract_fn "$SANDBOX/agents/.template/routines/self-update.sh" > "$SNIP_SELF"

# Sanity: both extractions actually captured the function.
assert_eq "extract update.sh: function captured" "1" \
  "$(grep -c '^resolve_channel_tag() {' "$SNIP_UPDATE")"
assert_eq "extract self-update.sh: function captured" "1" \
  "$(grep -c '^resolve_channel_tag() {' "$SNIP_SELF")"

# The two shipped function bodies must be byte-identical (mirror invariant).
body_update="$(sed -n '/^resolve_channel_tag() {/,/^}/p' "$SANDBOX/update.sh")"
body_self="$(sed -n '/^resolve_channel_tag() {/,/^}/p' "$SANDBOX/agents/.template/routines/self-update.sh")"
assert_eq "update.sh and self-update.sh selectors byte-identical" \
  "$body_update" "$body_self"

# For every case, run against BOTH shipped copies to prove parity.
for pair in "update:$SNIP_UPDATE" "self:$SNIP_SELF"; do
  who="${pair%%:*}"; SNIP="${pair#*:}"

  # =========================================================================
  CURRENT="release-ignores-dev/$who"
  say "[$who] release channel ignores '-dev' tags"
  r="$WORK/${who}_c1"; mkrepo "$r" v1.17.0 v2.0.0-dev.3
  run_selector "$SNIP" "$r" release
  assert_eq "release picks highest STABLE (v1.17.0, not v2.0.0-dev.3)" "v1.17.0" "$OUT"
  assert_eq "  exit 0" "0" "$RC"

  # =========================================================================
  CURRENT="release-stable-only-regression/$who"
  say "[$who] release with ONLY stable tags == old one-liner (regression guard)"
  r="$WORK/${who}_c2"; mkrepo "$r" v1.13.4 v1.14.0 v1.16.0 v1.15.0
  run_selector "$SNIP" "$r" release
  assert_eq "release == old one-liner result (v1.16.0)" "$(old_oneliner "$r")" "$OUT"
  assert_eq "  and it is the highest stable" "v1.16.0" "$OUT"

  # unset/unknown channel must behave like release.
  CURRENT="unset-and-unknown-channel/$who"
  say "[$who] unset/unknown channel -> release behavior"
  r="$WORK/${who}_c2b"; mkrepo "$r" v1.16.0 v2.0.0-dev.1
  run_selector "$SNIP" "$r" ""            # empty -> defaults to release
  assert_eq "empty channel -> release (v1.16.0)" "v1.16.0" "$OUT"
  run_selector "$SNIP" "$r" boguschannel  # unknown -> release
  assert_eq "unknown channel -> release (v1.16.0)" "v1.16.0" "$OUT"

  # =========================================================================
  CURRENT="dev-picks-highest-dev/$who"
  say "[$who] dev channel picks highest '-dev' when no stable of that base exists"
  r="$WORK/${who}_c3"; mkrepo "$r" v1.16.0 v2.0.0-dev.1 v2.0.0-dev.2 v2.0.0-dev.10
  run_selector "$SNIP" "$r" dev
  # --sort=-v:refname: v2.0.0-dev.10 > dev.2 > dev.1, and all rank ABOVE v1.16.0
  # (higher base version) since v2.0.0's stable does not yet exist.
  assert_eq "dev picks highest pre-release (v2.0.0-dev.10)" "v2.0.0-dev.10" "$OUT"

  # =========================================================================
  CURRENT="dev-prefers-promoted-stable/$who"
  say "[$who] dev channel: once the promoted stable exists it wins over its -dev"
  r="$WORK/${who}_c4"; mkrepo "$r" v1.16.0 v2.0.0-dev.3 v2.0.0
  run_selector "$SNIP" "$r" dev
  # The selector passes -c versionsort.suffix=- so a pre-release ranks BELOW
  # its stable; thus v2.0.0 (the promotion) outranks v2.0.0-dev.3 and dev
  # subscribers move forward to the stable, never regress to a -dev. NOTE: this
  # requires the explicit flag -- DEFAULT git version-sort ranks vX.Y.Z-dev.N
  # ABOVE vX.Y.Z (verified DGN-621 2026-07-28), which would be a regression.
  assert_eq "dev picks promoted stable (v2.0.0 over v2.0.0-dev.3)" "v2.0.0" "$OUT"

  # =========================================================================
  CURRENT="pin-existing/$who"
  say "[$who] PIN to an existing tag -> that exact tag regardless of channel"
  r="$WORK/${who}_c5"; mkrepo "$r" v1.16.0 v2.0.0-dev.3 v2.0.0
  run_selector "$SNIP" "$r" release v2.0.0-dev.3
  assert_eq "pin overrides release -> v2.0.0-dev.3" "v2.0.0-dev.3" "$OUT"
  assert_eq "  exit 0" "0" "$RC"
  run_selector "$SNIP" "$r" dev v1.16.0
  assert_eq "pin overrides dev -> v1.16.0" "v1.16.0" "$OUT"

  # =========================================================================
  CURRENT="pin-missing-fails-loud/$who"
  say "[$who] PIN to a missing tag -> loud failure, NOT silent latest"
  r="$WORK/${who}_c6"; mkrepo "$r" v1.16.0 v2.0.0
  run_selector "$SNIP" "$r" release v9.9.9-nope
  assert_eq "missing pin -> nonzero exit" "1" "$RC"
  # crucial: it must NOT have fallen back to the latest tag
  if [ "$OUT" = "v2.0.0" ] || [ "$OUT" = "v1.16.0" ]; then
    bad "missing pin silently fell back to a real tag ($OUT)"
  else
    ok "missing pin produced no tag on stdout (no silent latest)"
  fi
done

# ===========================================================================
say ""
say "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
