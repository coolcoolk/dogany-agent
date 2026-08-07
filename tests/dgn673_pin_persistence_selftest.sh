#!/bin/bash
# dgn673_pin_persistence_selftest.sh -- DGN-673 B2 self-test (owner decision D2).
#
# Covers resolve_update_pin (DOGANY_UPDATE_PIN persistence: env wins,
# .instance.conf fallback) added to both update.sh and
# agents/.template/routines/self-update.sh, plus the S8 round-trip:
# a pin persisted in the conf keeps a later "scheduled" update on the pinned
# tag; removing the pin resumes channel following.
#
# Like dgn621_channel_selector_selftest.sh, the functions are EXTRACTED from
# the shipped scripts (sed between the function opener and its closing brace)
# and sourced here, so the test exercises the real shipped code, not a
# re-typed copy. Both copies are tested independently and must additionally
# be byte-identical (mirror invariant).
#
# SAFETY: throwaway conf files + throwaway git repos with crafted tags under
# a private mktemp WORK dir. No network, no remotes, repo read-only.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn673-pin.XXXXXX")"
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
assert_contains() { # assert_contains <desc> <needle> <haystack>
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1 (missing [$2] in [$3])" ;; esac
}
assert_not_contains() { # assert_not_contains <desc> <needle> <haystack>
  case "$3" in *"$2"*) bad "$1 (unexpected [$2] in [$3])" ;; *) ok "$1" ;; esac
}

# ---------------------------------------------------------------------------
# Extract a shipped function into a sourceable snippet (dgn621 pattern).
# ---------------------------------------------------------------------------
extract_fn() { # $1 = shipped script, $2 = function name; prints the body
  sed -n "/^$2() {/,/^}/p" "$1"
}

mk_snippet() { # $1 = shipped script; prints a self-contained snippet path
  local script="$1" out="$2"
  {
    printf '%s\n' 'msg() { printf "%s\n" "$2"; }'
    printf '%s\n' 'die() { printf "[ERROR] %s\n" "$1" >&2; exit 1; }'
    extract_fn "$script" resolve_channel_tag
    extract_fn "$script" resolve_update_pin
  } > "$out"
}

SNIP_UPDATE="$WORK/fn_update.sh"
SNIP_SELF="$WORK/fn_self.sh"
mk_snippet "$SANDBOX/update.sh" "$SNIP_UPDATE"
mk_snippet "$SANDBOX/agents/.template/routines/self-update.sh" "$SNIP_SELF"

[ -s "$SNIP_UPDATE" ] || { say "FATAL: extraction from update.sh empty"; exit 1; }
grep -q 'resolve_update_pin() {' "$SNIP_UPDATE" \
  || { say "FATAL: resolve_update_pin missing from update.sh"; exit 1; }
grep -q 'resolve_update_pin() {' "$SNIP_SELF" \
  || { say "FATAL: resolve_update_pin missing from self-update.sh"; exit 1; }

# ---------------------------------------------------------------------------
# 0) Mirror + wiring invariants.
# ---------------------------------------------------------------------------
CURRENT="mirror"
say "[0] mirror + wiring invariants"
if diff -q \
  <(extract_fn "$SANDBOX/update.sh" resolve_update_pin) \
  <(extract_fn "$SANDBOX/agents/.template/routines/self-update.sh" resolve_update_pin) \
  >/dev/null; then
  ok "resolve_update_pin byte-identical in update.sh and self-update.sh"
else
  bad "resolve_update_pin copies differ between update.sh and self-update.sh"
fi
grep -Eq '^\s*resolve_update_pin ' "$SANDBOX/update.sh" \
  && ok "update.sh has a resolve_update_pin call site" \
  || bad "update.sh never calls resolve_update_pin"
grep -Eq '^\s*resolve_update_pin ' "$SANDBOX/agents/.template/routines/self-update.sh" \
  && ok "self-update.sh has a resolve_update_pin call site" \
  || bad "self-update.sh never calls resolve_update_pin"

# ---------------------------------------------------------------------------
# run_pin <snippet> <conf> <channel> [env-pin]
#   Runs resolve_update_pin in a clean subshell.
#   Sets: PIN (resolved value), EXPORTED (yes/no), OUT (stdout), ERR (stderr).
# ---------------------------------------------------------------------------
run_pin() {
  local snip="$1" conf="$2" chan="$3" envpin="${4-}"
  local dump="$WORK/dump.$$"
  OUT="$(
    unset DOGANY_UPDATE_PIN
    # shellcheck disable=SC1090
    . "$snip"
    if [ -n "$envpin" ]; then DOGANY_UPDATE_PIN="$envpin"; fi
    resolve_update_pin "$conf" "$chan" 2>"$dump.err"
    printf 'PIN=%s\n' "${DOGANY_UPDATE_PIN:-}"
    if env | grep -q '^DOGANY_UPDATE_PIN='; then
      printf 'EXPORTED=yes\n'
    else
      printf 'EXPORTED=no\n'
    fi
  )"
  ERR="$(cat "$dump.err" 2>/dev/null || true)"; rm -f "$dump.err"
  PIN="$(printf '%s\n' "$OUT" | sed -n 's/^PIN=//p' | head -n1)"
  EXPORTED="$(printf '%s\n' "$OUT" | sed -n 's/^EXPORTED=//p' | head -n1)"
}

CONF_PINNED="$WORK/conf_pinned"
cat > "$CONF_PINNED" <<'EOF'
DOGANY_AGENT_NAME=testy
DOGANY_UPDATE_PIN=v1.0.0
DOGANY_REPO_ROOT=/nowhere
EOF

CONF_EMPTYPIN="$WORK/conf_emptypin"
cat > "$CONF_EMPTYPIN" <<'EOF'
DOGANY_AGENT_NAME=testy
DOGANY_UPDATE_PIN=
EOF

CONF_NOPIN="$WORK/conf_nopin"
cat > "$CONF_NOPIN" <<'EOF'
DOGANY_AGENT_NAME=testy
EOF

for pair in "update:$SNIP_UPDATE" "self:$SNIP_SELF"; do
  label="${pair%%:*}"; SNIP="${pair#*:}"

  # 1) conf fallback: env unset -> conf pin adopted + exported + banner.
  CURRENT="$label/conf-fallback"
  say "[1][$label] conf pin adopted when env unset"
  run_pin "$SNIP" "$CONF_PINNED" release
  assert_eq "conf pin adopted" "v1.0.0" "$PIN"
  assert_eq "pin exported for child runs" "yes" "$EXPORTED"
  assert_contains "PINNED banner printed" "PINNED to v1.0.0" "$OUT"
  assert_contains "banner names suspension" "channel following suspended" "$OUT"

  # 2) env wins over conf.
  CURRENT="$label/env-wins"
  say "[2][$label] env pin wins over conf pin"
  run_pin "$SNIP" "$CONF_PINNED" release "v2.0.0"
  assert_eq "env pin kept" "v2.0.0" "$PIN"
  assert_contains "banner names the env pin" "PINNED to v2.0.0" "$OUT"

  # 3) no pin anywhere: silent, nothing resolved.
  CURRENT="$label/no-pin"
  say "[3][$label] no pin key -> silent no-op"
  run_pin "$SNIP" "$CONF_NOPIN" release
  assert_eq "no pin resolved" "" "$PIN"
  assert_not_contains "no PINNED banner" "PINNED" "$OUT"
  assert_not_contains "no released notice" "released" "$OUT"

  # 4) empty pin key = released: notice, no pin, no banner.
  CURRENT="$label/released"
  say "[4][$label] empty DOGANY_UPDATE_PIN= line -> released notice"
  run_pin "$SNIP" "$CONF_EMPTYPIN" release
  assert_eq "no pin resolved" "" "$PIN"
  assert_contains "released notice printed" "pin released" "$OUT"
  assert_not_contains "no PINNED banner" "PINNED" "$OUT"

  # 5) channel=main: pin exported but loud no-effect warning, no banner.
  CURRENT="$label/main-channel"
  say "[5][$label] pin on channel=main -> no-effect warning"
  run_pin "$SNIP" "$CONF_PINNED" main
  assert_eq "pin still resolved/exported" "v1.0.0" "$PIN"
  assert_contains "no-effect warning on stderr" "NO effect on channel=main" "$ERR"
  assert_not_contains "no PINNED banner on main" "PINNED to" "$OUT"

  # 6) missing conf file: silent no-op (fresh scaffold safety).
  CURRENT="$label/no-conf"
  say "[6][$label] missing conf file -> silent no-op"
  run_pin "$SNIP" "$WORK/does_not_exist" release
  assert_eq "no pin resolved" "" "$PIN"
  assert_not_contains "silent" "PINNED" "$OUT"
done

# ---------------------------------------------------------------------------
# 7) S8 round-trip against a real tag set: persisted pin holds a later
#    "scheduled" update on the pinned tag; removing the pin resumes channel
#    following (highest stable wins again).
# ---------------------------------------------------------------------------
CURRENT="round-trip"
say "[7] S8 round-trip: pin holds; unpin resumes channel"
REPO="$WORK/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q -b main
printf 'seed\n' > "$REPO/seed.txt"
git -C "$REPO" -c user.email=t@t -c user.name=t add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm seed
git -C "$REPO" tag v1.0.0
git -C "$REPO" tag v1.1.0   # the "bad" release the instance rolled back from

CONF_RT="$WORK/conf_rt"
printf 'DOGANY_UPDATE_PIN=v1.0.0\n' > "$CONF_RT"

TAG="$(
  unset DOGANY_UPDATE_PIN
  # shellcheck disable=SC1090
  . "$SNIP_SELF"
  resolve_update_pin "$CONF_RT" release >/dev/null 2>&1
  resolve_channel_tag "$REPO" release
)"
assert_eq "pinned run resolves the pinned tag, not the bad latest" "v1.0.0" "$TAG"

# Simulate the NEXT scheduled self-update (fresh process, env pin gone):
TAG2="$(
  unset DOGANY_UPDATE_PIN
  # shellcheck disable=SC1090
  . "$SNIP_SELF"
  resolve_update_pin "$CONF_RT" release >/dev/null 2>&1
  resolve_channel_tag "$REPO" release
)"
assert_eq "S8 closed: next scheduled run STAYS on the pin" "v1.0.0" "$TAG2"

# Incident close: pin removed (blank the value) -> channel following resumes.
printf 'DOGANY_UPDATE_PIN=\n' > "$CONF_RT"
TAG3="$(
  unset DOGANY_UPDATE_PIN
  # shellcheck disable=SC1090
  . "$SNIP_SELF"
  resolve_update_pin "$CONF_RT" release >/dev/null 2>&1
  resolve_channel_tag "$REPO" release
)"
assert_eq "unpinned run resumes channel following (highest stable)" "v1.1.0" "$TAG3"

# Missing pin tag still dies loudly through the selector (621 invariant intact).
RC=0
OUT_MISS="$(
  unset DOGANY_UPDATE_PIN
  # shellcheck disable=SC1090
  . "$SNIP_SELF"
  printf 'DOGANY_UPDATE_PIN=v9.9.9\n' > "$CONF_RT.miss"
  resolve_update_pin "$CONF_RT.miss" release >/dev/null 2>&1
  resolve_channel_tag "$REPO" release
)" 2>/dev/null || RC=$?
assert_eq "conf pin naming a missing tag fails loud (exit 1)" "1" "$RC"

# ---------------------------------------------------------------------------
say ""
say "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
