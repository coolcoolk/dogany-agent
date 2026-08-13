#!/bin/bash
# dgn724_bridge_hook_selftest.sh -- DGN-724 pre-commit bridge/ edit forcing point.
#
# Covers the instance-layout bridge/ edit guard added to git-hooks/pre-commit:
#   H1 instance bridge/ staged (no bypass)     -> BLOCKED (non-zero exit)
#   H2 --no-verify bypass                       -> commit succeeds
#   H3 canonical agents/.template/bridge/ path  -> new guard does NOT fire
#   H4 merge in progress (MERGE_HEAD present)   -> passes through
#
# The REAL shipped hook is installed into each sandbox repo via core.hooksPath,
# so the test exercises git-hooks/pre-commit as-is, not a re-typed copy.
#
# SAFETY: every scenario runs against throwaway git repos under a private
# mktemp WORK dir. No network, no remotes, no launchd, no tokens.
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$SANDBOX/git-hooks"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dgn724-test.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }

# make_repo <dir>: throwaway repo on a branch with the shipped hook installed.
make_repo() {
  local r="$1"
  mkdir -p "$r"
  git -C "$r" init -q -b work
  git -C "$r" config core.hooksPath "$HOOKS_DIR"
  git -C "$r" config user.email t@t
  git -C "$r" config user.name t
  # A seed commit so HEAD exists (merge scenario needs two histories).
  printf 'seed\n' > "$r/README"
  git -C "$r" add README
  git -C "$r" commit -qm seed
}

# ===========================================================================
CURRENT="H1"
say "H1: instance bridge/ staged, no bypass -> commit BLOCKED"
r="$WORK/h1"; make_repo "$r"
mkdir -p "$r/bridge"
printf 'X = 1\n' > "$r/bridge/foo.py"
git -C "$r" add bridge/foo.py
if git -C "$r" commit -qm "edit bridge" >/dev/null 2>&1; then
  bad "commit succeeded but should have been blocked"
else
  ok "commit blocked (non-zero exit)"
fi
if git -C "$r" commit -qm "edit bridge" 2>&1 | grep -q 'DGN-724'; then
  ok "block message names DGN-724"
else
  bad "block message did not name DGN-724"
fi

# ===========================================================================
CURRENT="H2"
say "H2: --no-verify bypass -> commit succeeds"
r="$WORK/h2"; make_repo "$r"
mkdir -p "$r/bridge"
printf 'X = 2\n' > "$r/bridge/foo.py"
git -C "$r" add bridge/foo.py
if git -C "$r" commit -q --no-verify -m "deliberate local dogfood" >/dev/null 2>&1; then
  ok "--no-verify commit succeeds"
else
  bad "--no-verify commit failed"
fi

# ===========================================================================
CURRENT="H3"
say "H3: canonical agents/.template/bridge/ path -> new instance guard inert"
# Stage a canonical-layout bridge file WITH a pin bump (so the DGN-625 guard is
# satisfied) and assert the commit is NOT blocked by the DGN-724 instance guard.
r="$WORK/h3"; make_repo "$r"
mkdir -p "$r/agents/.template/bridge"
printf 'DASH = 1\n' > "$r/agents/.template/bridge/dashboard.py"
printf '# pin bump\n' > "$r/agents/.template/bridge/UPSTREAM.md"
git -C "$r" add agents/.template/bridge/dashboard.py agents/.template/bridge/UPSTREAM.md
if git -C "$r" commit -qm "canonical bridge + pin" >/dev/null 2>&1; then
  ok "canonical-layout commit passes (DGN-724 guard did not fire)"
else
  # If it failed, it must NOT be the DGN-724 guard that fired.
  out="$(git -C "$r" commit -qm "canonical bridge + pin" 2>&1)"
  if printf '%s' "$out" | grep -q 'DGN-724'; then
    bad "DGN-724 guard wrongly fired on agents/.template/bridge/ path"
  else
    bad "canonical commit failed for an unrelated reason: $out"
  fi
fi

# ===========================================================================
CURRENT="H4"
say "H4: merge in progress -> hook passes through (bridge/ staged)"
# Build two divergent branches, start a merge that conflicts so MERGE_HEAD is
# set, stage a bridge/ file, and assert the commit is allowed (the top-of-hook
# in-progress check exits 0 before the guards).
r="$WORK/h4"; make_repo "$r"
git -C "$r" checkout -qb other
printf 'other\n' > "$r/shared.txt"
git -C "$r" add shared.txt
git -C "$r" commit -qm "other side"
git -C "$r" checkout -q work
printf 'work\n' > "$r/shared.txt"
git -C "$r" add shared.txt
git -C "$r" commit -qm "work side"
# Conflicting merge leaves MERGE_HEAD set and the index in a merge state.
git -C "$r" merge --no-commit other >/dev/null 2>&1 || true
if [ ! -f "$r/.git/MERGE_HEAD" ]; then
  bad "setup: MERGE_HEAD not present (cannot exercise merge pass-through)"
else
  # Resolve the conflict and add a bridge/ file into the same merge commit.
  printf 'resolved\n' > "$r/shared.txt"
  mkdir -p "$r/bridge"
  printf 'X = 4\n' > "$r/bridge/foo.py"
  git -C "$r" add shared.txt bridge/foo.py
  if git -C "$r" commit -qm "merge with bridge edit" >/dev/null 2>&1; then
    ok "merge-in-progress commit passes despite staged bridge/"
  else
    bad "merge-in-progress commit was wrongly blocked"
  fi
fi

# ===========================================================================
say ""
say "RESULT: pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
