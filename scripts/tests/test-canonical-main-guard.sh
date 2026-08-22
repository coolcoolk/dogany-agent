#!/bin/bash
# test-canonical-main-guard.sh -- sandbox tests for the canonical-main trunk
# guard in git-hooks/pre-commit (DGN-1005).
#
# The REAL shipped hook is installed into each mktemp sandbox repo via
# core.hooksPath, so the tests exercise git-hooks/pre-commit as-is, not a
# re-typed copy. Never touches the real repos.
#
# Covered:
#   G1  plain commit on main + marker            -> BLOCKED (DGN-1005 message)
#   G2  clean `git merge --no-ff` into main      -> ALLOWED (2-parent merge)
#   G3  conflicted merge resolution `git commit` -> ALLOWED (MERGE_HEAD pass)
#   G4  VERSION + CHANGELOG.md commit on main    -> ALLOWED (release sniff)
#   G5  VERSION only (no CHANGELOG.md) on main   -> BLOCKED (sniff needs both)
#   G6  plain commit on a non-main branch        -> ALLOWED
#   G7  plain commit on main, NO marker          -> ALLOWED (guard inert)
#   G8  `git commit --no-verify` on main         -> ALLOWED (bypass)
#   G9  clean cherry-pick onto main + marker     -> ALLOWED
#   G10 conflicted cherry-pick --continue on main-> ALLOWED (CHERRY_PICK_HEAD)
#   G11 conflicted rebase --continue             -> ALLOWED (rebase-merge pass)
#   G12 message-only --amend on main + marker    -> BLOCKED (pinned; documented)
#   G13 detached-HEAD commit still blocked       -> regression on prior guard
#
# Exit 0 = all assertions pass; nonzero = at least one failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SRC="$REPO_ROOT/git-hooks/pre-commit"

SANDBOX="$(mktemp -d /tmp/dgn1005-guard-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }

# mk_repo <marker|nomarker> <name> -- creates $SANDBOX/<name>: a fresh sandbox
# repo with the REAL hook wired via core.hooksPath and one initial commit on
# main. (No stdout capture: command substitution would subshell-swallow state.)
mk_repo() {
  local r="$SANDBOX/$2"
  git init -q -b main "$r"
  git -C "$r" config user.name t
  git -C "$r" config user.email t@t
  git -C "$r" config commit.gpgsign false
  mkdir -p "$r/git-hooks"
  cp "$HOOK_SRC" "$r/git-hooks/pre-commit"
  chmod +x "$r/git-hooks/pre-commit"
  git -C "$r" config core.hooksPath git-hooks
  if [ "$1" = "marker" ]; then
    printf 'canonical marker (test)\n' > "$r/.dogany-canonical"
  fi
  echo "base" > "$r/base.txt"
  git -C "$r" add --all
  # Initial commit bypasses the hook: repo bootstrap, not the behavior under test.
  git -C "$r" commit -q --no-verify -m "init"
}

# ---- G1: plain commit on main + marker -> blocked ---------------------------
mk_repo marker g1
r="$SANDBOX/g1"
echo "edit" > "$r/base.txt"
git -C "$r" add base.txt
OUT="$(git -C "$r" commit -m "plain on main" 2>&1)"; RC=$?
if [ $RC -ne 0 ] && printf '%s' "$OUT" | grep -q "DGN-1005"; then
  ok "G1 plain commit on main blocked with DGN-1005 message"
else
  bad "G1 expected block+message, rc=$RC out: $OUT"
fi

# ---- G2: clean merge --no-ff into main -> allowed ---------------------------
mk_repo marker g2
r="$SANDBOX/g2"
git -C "$r" switch -q -c feature
echo "feat" > "$r/feat.txt"
git -C "$r" add feat.txt
git -C "$r" commit -q -m "feat"          # on branch: hook must allow (also G6 shape)
git -C "$r" switch -q main
if git -C "$r" merge --no-ff -q -m "merge: feature" feature >/dev/null 2>&1 \
   && [ "$(git -C "$r" rev-list -1 --parents HEAD | wc -w | tr -d ' ')" = "3" ]; then
  ok "G2 clean git merge --no-ff into main allowed (2-parent merge commit)"
else
  bad "G2 clean merge into main failed"
fi

# ---- G3: conflicted merge resolution commit -> allowed (MERGE_HEAD) ---------
mk_repo marker g3
r="$SANDBOX/g3"
git -C "$r" switch -q -c feature
echo "branch-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q -m "branch edit"
git -C "$r" switch -q main
echo "main-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q --no-verify -m "main edit (setup)"
git -C "$r" merge feature >/dev/null 2>&1   # conflicts
echo "resolved" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" commit -q -m "merge resolution" >/dev/null 2>&1; then
  ok "G3 conflicted-merge resolution commit on main allowed (MERGE_HEAD pass)"
else
  bad "G3 merge resolution commit was blocked"
fi

# ---- G4: VERSION + CHANGELOG.md on main -> allowed --------------------------
mk_repo marker g4
r="$SANDBOX/g4"
echo "1.0.1" > "$r/VERSION"
echo "## v1.0.1" > "$r/CHANGELOG.md"
git -C "$r" add VERSION CHANGELOG.md
if git -C "$r" commit -q -m "v1.0.1: bump VERSION + CHANGELOG" >/dev/null 2>&1; then
  ok "G4 release bookkeeping (VERSION+CHANGELOG.md) on main allowed"
else
  bad "G4 VERSION+CHANGELOG commit on main was blocked"
fi

# ---- G5: VERSION only (no CHANGELOG.md) -> blocked --------------------------
mk_repo marker g5
r="$SANDBOX/g5"
echo "1.0.1" > "$r/VERSION"
git -C "$r" add VERSION
if git -C "$r" commit -q -m "VERSION only" >/dev/null 2>&1; then
  bad "G5 VERSION-only commit on main passed (sniff must require CHANGELOG.md too)"
else
  ok "G5 VERSION-only commit on main blocked"
fi

# ---- G6: plain commit on a non-main branch -> allowed -----------------------
mk_repo marker g6
r="$SANDBOX/g6"
git -C "$r" switch -q -c work
echo "edit" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" commit -q -m "on branch" >/dev/null 2>&1; then
  ok "G6 plain commit on non-main branch allowed"
else
  bad "G6 commit on non-main branch was blocked"
fi

# ---- G7: plain commit on main WITHOUT marker -> allowed (inert) -------------
mk_repo nomarker g7
r="$SANDBOX/g7"
echo "edit" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" commit -q -m "instance-style main commit" >/dev/null 2>&1; then
  ok "G7 guard inert without .dogany-canonical marker (instance safety)"
else
  bad "G7 commit blocked in a repo without the marker -- SCOPING BROKEN"
fi

# ---- G8: --no-verify bypass on main + marker -> allowed ---------------------
mk_repo marker g8
r="$SANDBOX/g8"
echo "edit" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" commit -q --no-verify -m "deliberate bypass" >/dev/null 2>&1; then
  ok "G8 git commit --no-verify bypass works on main"
else
  bad "G8 --no-verify did not bypass the guard"
fi

# ---- G9: clean cherry-pick onto main + marker -> allowed --------------------
mk_repo marker g9
r="$SANDBOX/g9"
git -C "$r" switch -q -c feature
echo "pickme" > "$r/pick.txt"
git -C "$r" add pick.txt && git -C "$r" commit -q -m "pickable"
PICK_SHA="$(git -C "$r" rev-parse HEAD)"
git -C "$r" switch -q main
if git -C "$r" cherry-pick "$PICK_SHA" >/dev/null 2>&1; then
  ok "G9 clean cherry-pick onto main allowed"
else
  bad "G9 clean cherry-pick onto main was blocked"
fi

# ---- G10: conflicted cherry-pick --continue on main -> allowed --------------
mk_repo marker g10
r="$SANDBOX/g10"
git -C "$r" switch -q -c feature
echo "branch-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q -m "branch edit"
PICK_SHA="$(git -C "$r" rev-parse HEAD)"
git -C "$r" switch -q main
echo "main-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q --no-verify -m "main edit (setup)"
git -C "$r" cherry-pick "$PICK_SHA" >/dev/null 2>&1   # conflicts
echo "resolved" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" -c core.editor=true cherry-pick --continue >/dev/null 2>&1; then
  ok "G10 conflicted cherry-pick --continue on main allowed (CHERRY_PICK_HEAD pass)"
else
  bad "G10 cherry-pick --continue on main was blocked"
fi

# ---- G11: conflicted rebase --continue -> allowed ---------------------------
mk_repo marker g11
r="$SANDBOX/g11"
git -C "$r" switch -q -c feature
echo "branch-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q -m "branch edit"
git -C "$r" switch -q main
echo "main-side" > "$r/base.txt"
git -C "$r" add base.txt && git -C "$r" commit -q --no-verify -m "main edit (setup)"
git -C "$r" switch -q feature
git -C "$r" rebase main >/dev/null 2>&1   # conflicts
echo "resolved" > "$r/base.txt"
git -C "$r" add base.txt
if git -C "$r" -c core.editor=true rebase --continue >/dev/null 2>&1; then
  ok "G11 conflicted rebase --continue allowed (rebase-in-progress pass)"
else
  bad "G11 rebase --continue was blocked"
fi

# ---- G12: message-only --amend on main -> blocked (pinned behavior) ---------
# A message-only amend stages nothing vs the amended HEAD, so the VERSION sniff
# cannot classify it; the hook blocks and the comment block documents
# --no-verify as the deliberate path. This test PINS that trade-off.
mk_repo marker g12
r="$SANDBOX/g12"
echo "1.0.1" > "$r/VERSION"
echo "## v1.0.1" > "$r/CHANGELOG.md"
git -C "$r" add VERSION CHANGELOG.md
git -C "$r" commit -q -m "v1.0.1 bump" >/dev/null 2>&1
if git -C "$r" commit -q --amend -m "v1.0.1 bump (reworded)" >/dev/null 2>&1; then
  bad "G12 message-only amend on main passed (expected pinned block; update docs if intended)"
else
  ok "G12 message-only amend on main blocked (documented; --no-verify is the path)"
fi

# ---- G13: detached-HEAD guard regression ------------------------------------
mk_repo marker g13
r="$SANDBOX/g13"
git -C "$r" switch -q --detach HEAD
echo "detached" > "$r/base.txt"
git -C "$r" add base.txt
OUT="$(git -C "$r" commit -m "detached commit" 2>&1)"; RC=$?
if [ $RC -ne 0 ] && printf '%s' "$OUT" | grep -q "detached HEAD"; then
  ok "G13 detached-HEAD guard still blocks (no regression)"
else
  bad "G13 detached-HEAD guard regressed, rc=$RC"
fi

echo ""
echo "test-canonical-main-guard: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
