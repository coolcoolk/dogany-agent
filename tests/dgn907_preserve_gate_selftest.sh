#!/bin/bash
# dgn907_preserve_gate_selftest.sh -- DGN-907 self-test.
#
# Covers the preserve-pin lockstep gate added to update.sh:
#   * _dgn907_ver_le          dotted-version compare (numeric per field)
#   * _dgn907_prune_after     "prune-after: vX.Y.Z" annotation extraction
#   * contract_smoke          shell-rail sanitize contract (push.sh import
#                             mirror) against a bridge-bearing root
#   * preserve_expiry_reconcile  hybrid pin-expiry model (owner decision
#                             2026-08-16): expired + payload smoke PASS ->
#                             auto-invalidate (re-land); expired + smoke FAIL
#                             -> keep pin + hard warning; unexpired -> no-op.
#
# Like dgn673_pin_persistence_selftest.sh, the functions are EXTRACTED from
# the shipped update.sh (sed between the function opener and its closing
# brace) and sourced here, so the test exercises the real shipped code, not a
# re-typed copy.
#
# SAFETY: throwaway fixture trees under a private mktemp WORK dir. No
# network, no instance touched, shipped script read-only.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATE_SH="$SANDBOX/update.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn907-gate.XXXXXX")"
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

# ---------------------------------------------------------------------------
# Extract shipped functions into a sourceable snippet (dgn621/dgn673 pattern).
# ---------------------------------------------------------------------------
extract_fn() { # $1 = shipped script, $2 = function name; prints the body
  sed -n "/^$2() {/,/^}/p" "$1"
}

for _fn in _dgn907_ver_le _dgn907_prune_after _contract_python \
           contract_smoke preserve_expiry_reconcile; do
  if ! grep -q "^${_fn}() {" "$UPDATE_SH"; then
    say "FATAL: function ${_fn} not found in $UPDATE_SH"
    exit 1
  fi
done

SNIP="$WORK/fns.sh"
{
  # msg stub: english arm only, to stdout (warnings in the shipped code go to
  # stderr at the CALL site via >&2, which survives extraction).
  printf '%s\n' 'msg() { printf "%s\n" "$2"; }'
  extract_fn "$UPDATE_SH" _dgn907_ver_le
  extract_fn "$UPDATE_SH" _dgn907_prune_after
  extract_fn "$UPDATE_SH" _contract_python
  extract_fn "$UPDATE_SH" contract_smoke
  extract_fn "$UPDATE_SH" preserve_expiry_reconcile
} > "$SNIP"
# shellcheck disable=SC1090
. "$SNIP"

# ---------------------------------------------------------------------------
# 1) _dgn907_ver_le
# ---------------------------------------------------------------------------
CURRENT="ver_le"
say "[1] _dgn907_ver_le"
vle() { if _dgn907_ver_le "$1" "$2"; then echo yes; else echo no; fi; }
assert_eq "1.23.0 <= 1.36.0"          "yes" "$(vle 1.23.0 1.36.0)"
assert_eq "1.36.1 <= 1.36.0 is false" "no"  "$(vle 1.36.1 1.36.0)"
assert_eq "equal versions"            "yes" "$(vle 1.36.0 1.36.0)"
assert_eq "numeric not lexical (1.9.0 <= 1.10.0)" "yes" "$(vle 1.9.0 1.10.0)"
assert_eq "2.0 <= 1.99.99 is false"   "no"  "$(vle 2.0 1.99.99)"
assert_eq "short form (1.23 <= 1.23.0)" "yes" "$(vle 1.23 1.23.0)"

# ---------------------------------------------------------------------------
# 2) _dgn907_prune_after
# ---------------------------------------------------------------------------
CURRENT="prune_after"
say "[2] _dgn907_prune_after"
assert_eq "v-prefixed"  "1.23.0" \
  "$(_dgn907_prune_after 'bridge/formatting.py  # HTML work -- prune-after: v1.23.0')"
assert_eq "bare version" "1.23.0" \
  "$(_dgn907_prune_after 'bridge/bot.py # prune-after: 1.23.0')"
assert_eq "no annotation -> empty" "" \
  "$(_dgn907_prune_after 'routines/reminder.sh  # plain comment')"
assert_eq "malformed (no digits) -> empty" "" \
  "$(_dgn907_prune_after 'x.py # prune-after: soon')"

# ---------------------------------------------------------------------------
# 3) contract_smoke against fixture roots (real function, real python).
#    _contract_python resolves $INSTANCE/bridge/venv first; fixtures carry no
#    venv, so it falls back to python3 -- the push.sh no-venv hop path.
# ---------------------------------------------------------------------------
CURRENT="contract_smoke"
say "[3] contract_smoke"
if ! command -v python3 >/dev/null 2>&1; then
  say "  SKIP: python3 not available on this host"
else
  INSTANCE="$WORK/no-such-instance"   # no venv -> python3 fallback

  GOOD="$WORK/good"
  mkdir -p "$GOOD/bridge"
  : > "$GOOD/bridge/__init__.py"
  cat > "$GOOD/bridge/formatting.py" <<'PY'
def sanitize_message_for_telegram(text):
    return text.replace("**x**", "<b>x</b>")
PY

  MISSING="$WORK/missing-fn"
  mkdir -p "$MISSING/bridge"
  : > "$MISSING/bridge/__init__.py"
  printf 'OPTIONS_MARKER = "[[OPTIONS]]"\n' > "$MISSING/bridge/formatting.py"

  BROKEN="$WORK/broken-render"
  mkdir -p "$BROKEN/bridge"
  : > "$BROKEN/bridge/__init__.py"
  cat > "$BROKEN/bridge/formatting.py" <<'PY'
def sanitize_message_for_telegram(text):
    return text  # import resolves but markdown is NOT rendered
PY

  smoke() { if contract_smoke "$1"; then echo pass; else echo fail; fi; }
  assert_eq "healthy payload passes"                    "pass" "$(smoke "$GOOD")"
  assert_eq "missing sanitize function fires the gate"  "fail" "$(smoke "$MISSING")"
  assert_eq "non-rendering sanitize fires the gate"     "fail" "$(smoke "$BROKEN")"
  assert_eq "no bridge/ at all fires the gate"          "fail" "$(smoke "$WORK/nowhere")"
fi

# ---------------------------------------------------------------------------
# 4) preserve_expiry_reconcile (hybrid model). contract_smoke is stubbed so
#    the pin logic is tested in isolation; the real smoke is covered in [3].
# ---------------------------------------------------------------------------
CURRENT="expiry_reconcile"
say "[4] preserve_expiry_reconcile"
SMOKE_CALLS=0
SMOKE_VERDICT=0    # 0 = pass, 1 = fail
contract_smoke() { SMOKE_CALLS=$((SMOKE_CALLS+1)); return "$SMOKE_VERDICT"; }
TEMPLATE="$WORK/fake-template"

# 4a: expired pin + payload smoke PASS -> entry dropped, others kept.
PRESERVE_ENTRIES=("bridge/formatting.py" "bridge/bot.py" "routines/reminder.sh")
PRESERVE_PIN_PATHS=("bridge/formatting.py" "bridge/bot.py")
PRESERVE_PIN_VERS=("1.23.0" "1.23.0")
REPO_VERSION="1.36.0"
SMOKE_CALLS=0; SMOKE_VERDICT=0
preserve_expiry_reconcile > "$WORK/out.txt" 2>&1
OUT="$(cat "$WORK/out.txt")"
assert_eq "expired+pass: both pinned entries dropped" \
  "routines/reminder.sh" "${PRESERVE_ENTRIES[*]}"
assert_contains "expired+pass: EXPIRED notice printed" "preserve pin EXPIRED" "$OUT"
assert_eq "smoke memoized (1 call for 2 entries)" "1" "$SMOKE_CALLS"

# 4b: expired pin + payload smoke FAIL -> entry KEPT + hard warning.
PRESERVE_ENTRIES=("bridge/formatting.py" "routines/reminder.sh")
PRESERVE_PIN_PATHS=("bridge/formatting.py")
PRESERVE_PIN_VERS=("1.23.0")
REPO_VERSION="1.36.0"
SMOKE_CALLS=0; SMOKE_VERDICT=1
preserve_expiry_reconcile > "$WORK/out.txt" 2>&1
OUT="$(cat "$WORK/out.txt")"
assert_eq "expired+fail: pin kept" \
  "bridge/formatting.py routines/reminder.sh" "${PRESERVE_ENTRIES[*]}"
assert_contains "expired+fail: hard warning names no-release" "NOT released" "$OUT"

# 4c: unexpired pin (target > consumed) -> untouched, smoke never called.
PRESERVE_ENTRIES=("bridge/formatting.py")
PRESERVE_PIN_PATHS=("bridge/formatting.py")
PRESERVE_PIN_VERS=("9.99.0")
REPO_VERSION="1.36.0"
SMOKE_CALLS=0; SMOKE_VERDICT=0
preserve_expiry_reconcile > "$WORK/out.txt" 2>&1
OUT="$(cat "$WORK/out.txt")"
assert_eq "unexpired: pin kept" "bridge/formatting.py" "${PRESERVE_ENTRIES[*]}"
assert_eq "unexpired: smoke not called" "0" "$SMOKE_CALLS"

# 4d: unknown REPO_VERSION -> no-op (never invalidate on an unknown target).
PRESERVE_ENTRIES=("bridge/formatting.py")
PRESERVE_PIN_PATHS=("bridge/formatting.py")
PRESERVE_PIN_VERS=("1.23.0")
REPO_VERSION="unknown"
SMOKE_CALLS=0; SMOKE_VERDICT=0
preserve_expiry_reconcile > "$WORK/out.txt" 2>&1
OUT="$(cat "$WORK/out.txt")"
assert_eq "unknown version: pin kept" "bridge/formatting.py" "${PRESERVE_ENTRIES[*]}"
assert_eq "unknown version: smoke not called" "0" "$SMOKE_CALLS"

# 4e: no annotated entries -> no-op on the entry list.
PRESERVE_ENTRIES=("routines/reminder.sh")
PRESERVE_PIN_PATHS=()
PRESERVE_PIN_VERS=()
REPO_VERSION="1.36.0"
preserve_expiry_reconcile > "$WORK/out.txt" 2>&1
OUT="$(cat "$WORK/out.txt")"
assert_eq "no annotations: entries untouched" \
  "routines/reminder.sh" "${PRESERVE_ENTRIES[*]}"

# ---------------------------------------------------------------------------
say ""
say "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
