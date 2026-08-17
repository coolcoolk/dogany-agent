#!/bin/bash
# mirror-forward.sh -- DGN-880 single always-mirror publish helpers.
#
# Sourced by publish.sh. Pure helper functions, no side effects at load time.
# All functions take explicit repo/remote parameters so they are unit-testable
# against fixture repos (scripts/tests/test-mirror-forward.sh).
#
# Model (DGN-880 LOCKED design v2): the public mirror is advanced one stable
# tag at a time, ascending, from the last published version (PUB_LAST) up to
# the newest canonical stable tag. Every step is verified against origin
# BEFORE any export is built:
#   - local tag SHA == origin tag SHA  (a moved/forged local tag never ships)
#   - tag commit is an ancestor of origin/main  (released state only)
#   - tag-tree VERSION file == tag name  (a mislabeled tag never ships)
# The skip predicate is two-sided (blocker 3): a version counts as published
# ONLY when the public HEAD subject carries it AND the tag exists on the
# public remote. Subject-only match (tag-loss window between branch-push and
# tag-push) => the step re-runs; the deterministic snapshot makes that a safe
# resume, never a divergent rewrite.
#
# This file is English/ASCII only.

# ---- mf_ver_ge A B -> 0 if version A >= B (semver triples) -------------------
mf_ver_ge() {
  if [ "$1" = "$2" ]; then
    return 0
  fi
  if [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; then
    return 0
  fi
  return 1
}

# ---- mf_stable_tags REPO MAIN_REF -------------------------------------------
# Prints plain vX.Y.Z tags (no dev/rc/prefix variants) that are ancestors of
# MAIN_REF, ascending version order (patch tags included -- DGN-763 full
# mirror, A3). Non-ancestor tags (unreleased branch tags) are silently
# excluded here; per-tag verification later re-asserts ancestry hard.
mf_stable_tags() {
  local repo="$1" main_ref="$2" t c
  git -C "$repo" tag -l 'v*' 2>/dev/null \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V \
    | while IFS= read -r t; do
        c="$(git -C "$repo" rev-list -n1 "refs/tags/$t" 2>/dev/null)"
        if [ -n "$c" ] && git -C "$repo" merge-base --is-ancestor "$c" "$main_ref" 2>/dev/null; then
          printf '%s\n' "$t"
        fi
      done
}

# ---- mf_pending_tags REPO MAIN_REF PUB_LAST ---------------------------------
# Prints the ascending step list: stable tags with version >= PUB_LAST.
# PUB_LAST itself is INCLUDED so the skip predicate can repair a tag-loss
# window on the already-current version (blocker 3); a fully-published
# PUB_LAST is skipped by the predicate, not by enumeration.
# PUB_LAST empty (no prior public snapshot) -> prints nothing; the caller
# handles first-publish explicitly.
mf_pending_tags() {
  local repo="$1" main_ref="$2" pub_last="$3" t
  if [ -z "$pub_last" ]; then
    return 0
  fi
  mf_stable_tags "$repo" "$main_ref" | while IFS= read -r t; do
    if mf_ver_ge "${t#v}" "$pub_last"; then
      printf '%s\n' "$t"
    fi
  done
}

# ---- mf_verify_tag REPO ORIGIN_REMOTE MAIN_REF TAG --------------------------
# DGN-880 blocker 2 verification. Prints "ok" and returns 0 on pass; prints
# the failure reason and returns 1 on any mismatch (ancestor-alone is
# insufficient -- all three checks must hold):
#   (a) local tag object SHA == origin tag object SHA (ls-remote)
#   (b) tag commit is ancestor of MAIN_REF
#   (c) tag-tree VERSION file content == tag version
mf_verify_tag() {
  local repo="$1" origin_remote="$2" main_ref="$3" tag="$4"
  local ref="refs/tags/$4" loc rem_out rem commit tree_ver

  loc="$(git -C "$repo" rev-parse -q --verify "$ref" 2>/dev/null)"
  if [ -z "$loc" ]; then
    printf 'local tag %s missing' "$tag"
    return 1
  fi

  if ! rem_out="$(git -C "$repo" ls-remote "$origin_remote" "$ref" 2>/dev/null)"; then
    printf 'cannot reach %s to verify tag %s' "$origin_remote" "$tag"
    return 1
  fi
  rem="$(printf '%s\n' "$rem_out" | awk -v r="$ref" '$2==r {print $1; exit}')"
  if [ -z "$rem" ]; then
    printf 'tag %s absent on %s -- un-released tag, never publish' "$tag" "$origin_remote"
    return 1
  fi
  if [ "$loc" != "$rem" ]; then
    printf 'local tag %s SHA %s != %s SHA %s' "$tag" "$loc" "$origin_remote" "$rem"
    return 1
  fi

  commit="$(git -C "$repo" rev-list -n1 "$ref" 2>/dev/null)"
  if [ -z "$commit" ] || ! git -C "$repo" merge-base --is-ancestor "$commit" "$main_ref" 2>/dev/null; then
    printf 'tag %s commit is not an ancestor of %s -- unreleased state' "$tag" "$main_ref"
    return 1
  fi

  tree_ver="$(git -C "$repo" show "$ref:VERSION" 2>/dev/null | tr -d '[:space:]')"
  if [ "v$tree_ver" != "$tag" ]; then
    printf 'tag %s tree VERSION reads "%s" -- mislabeled tag' "$tag" "$tree_ver"
    return 1
  fi

  printf 'ok'
  return 0
}

# ---- mf_public_action REPO PUBLIC_REMOTE PUBLIC_BRANCH PUBLIC_URL VER -------
# Two-sided skip predicate (blocker 3). Refreshes the public branch ref, then
# prints exactly one of:
#   skip     -- public HEAD subject == VER AND tag vVER exists on public
#   rerun    -- public HEAD subject == VER but tag vVER MISSING on public
#               (tag-loss window; the deterministic snapshot makes the re-run
#                a safe resume)
#   publish  -- version not on public HEAD subject -> normal step
mf_public_action() {
  local repo="$1" remote="$2" branch="$3" url="$4" ver="$5"
  local subj subj_ver tag_present=1

  git -C "$repo" fetch "$remote" "$branch" --quiet 2>/dev/null || true
  subj="$(git -C "$repo" log -1 --format=%s "$remote/$branch" 2>/dev/null)"
  subj_ver="$(printf '%s\n' "$subj" | sed -nE 's/^Dogany v([0-9]+\.[0-9]+\.[0-9]+) .*/\1/p')"

  if [ "$subj_ver" != "$ver" ]; then
    printf 'publish'
    return 0
  fi
  if git -C "$repo" ls-remote "$url" "refs/tags/v$ver" 2>/dev/null | grep -q "refs/tags/v$ver"; then
    tag_present=0
  fi
  if [ "$tag_present" -eq 0 ]; then
    printf 'skip'
  else
    printf 'rerun'
  fi
  return 0
}
