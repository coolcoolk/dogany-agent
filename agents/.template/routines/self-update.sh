#!/bin/sh
# self-update.sh -- update THIS instance to the latest framework, zero args.
#
# WHAT THIS IS: the "update yourself" entrypoint. An agent told to update
# itself runs THIS script -- it needs no --root and no operator input.
#
# update != release:
#   - self-update.sh / update.sh CONSUME a published framework release into
#     an instance (this is "update yourself").
#   - bumping VERSION + tagging PRODUCES a release (that is release.sh, a
#     separate, maintainer-only act). "Update yourself" is NEVER a release.
#
# Behaviour:
#   1. Resolve THIS instance's own root from the script's own location
#      (routines/ -> instance root), NOT from cwd -- the job survives a
#      workspace move and does not depend on where it is invoked from.
#   2. Refuse if that root has no .instance.conf (mirrors update.sh's gate:
#      a real minted instance always carries one).
#   3. Read DOGANY_REPO_ROOT from .instance.conf and sync the framework repo
#      FIRST, so the newest framework AND the newest update.sh are present
#      before we invoke update.sh (a stale instance must not run the old,
#      un-gated update.sh against itself). Sync = latest RELEASE TAG (v*)
#      by default (DGN-221: instances consume releases, never main HEAD);
#      DOGANY_UPDATE_CHANNEL=main (env or .instance.conf) restores
#      `git pull --ff-only` for development checkouts that dogfood main.
#   4. exec update.sh --root <self> --yes  (self-targeted, non-interactive).
#
# No owner data, no machine paths, no identity placeholders: every path is
# resolved at runtime. Safe to ship generically (template / OSS).
#
# Usage:
#   ./self-update.sh            # update this instance to the latest framework
#   DOGANY_LANG=ko ./self-update.sh   # Korean messages (default: en)
#
# Pass-through: any extra args are forwarded to update.sh (e.g. --dry-run,
# --no-pull) for advanced/debug use; the common case is zero args.
set -eu

DOGANY_LANG="${DOGANY_LANG:-en}"
msg() { if [ "$DOGANY_LANG" = "ko" ]; then printf '%s\n' "$1"; else printf '%s\n' "$2"; fi; }
die() { msg "[오류] $1" "[ERROR] $1" >&2; exit 1; }

# 1) Resolve this instance's own root: routines/ -> instance root.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 2) Gate: a real minted instance carries a .instance.conf.
CONF="$SELF_ROOT/.instance.conf"
[ -f "$CONF" ] || die "not a minted Dogany instance (no .instance.conf): $SELF_ROOT"

# 3) Recover the framework repo root from the instance manifest.
#    Read DOGANY_REPO_ROOT without sourcing the whole conf (avoid importing
#    unrelated vars into this shell).
REPO_ROOT="$(sed -n 's/^DOGANY_REPO_ROOT=//p' "$CONF" | head -n1)"
[ -n "$REPO_ROOT" ] || die "DOGANY_REPO_ROOT missing from $CONF"
[ -d "$REPO_ROOT" ] || die "framework repo not found: $REPO_ROOT"
[ -f "$REPO_ROOT/update.sh" ] || die "update.sh not found in repo: $REPO_ROOT"

# 3b) Sync the newest framework + update.sh FIRST, so we run the current,
#     gated update.sh -- not the stale copy this instance may have shipped with.
#     Default channel "release": fetch tags and pin to the highest v* tag
#     (works from a detached-at-tag state, where `git pull` would fail).
#     Channel "main": old pull --ff-only behaviour for dev checkouts.
UPDATE_CHANNEL="${DOGANY_UPDATE_CHANNEL:-$(sed -n 's/^DOGANY_UPDATE_CHANNEL=//p' "$CONF" | head -n1)}"
UPDATE_CHANNEL="${UPDATE_CHANNEL:-release}"
if [ -d "$REPO_ROOT/.git" ]; then
  if [ "$UPDATE_CHANNEL" = "main" ]; then
    msg "[self-update] 프레임워크 최신화: git pull --ff-only ($REPO_ROOT, channel=main)" \
        "[self-update] fetching latest framework: git pull --ff-only ($REPO_ROOT, channel=main)"
    git -C "$REPO_ROOT" pull --ff-only \
      || die "git pull --ff-only failed in $REPO_ROOT (resolve manually, then re-run)"
  else
    msg "[self-update] 프레임워크 최신화: 최신 릴리스 태그 ($REPO_ROOT)" \
        "[self-update] fetching latest framework: latest release tag ($REPO_ROOT)"
    git -C "$REPO_ROOT" fetch --tags origin \
      || die "git fetch failed in $REPO_ROOT (resolve manually, then re-run)"
    LATEST_TAG="$(git -C "$REPO_ROOT" tag --list 'v*' --sort=-v:refname | head -n1)"
    [ -n "$LATEST_TAG" ] || die "no release tag (v*) found in $REPO_ROOT"
    if [ "$(git -C "$REPO_ROOT" rev-parse HEAD)" != "$(git -C "$REPO_ROOT" rev-parse "${LATEST_TAG}^{commit}")" ]; then
      git -C "$REPO_ROOT" checkout --quiet "$LATEST_TAG" \
        || die "checkout $LATEST_TAG failed in $REPO_ROOT (local changes? resolve manually, then re-run)"
    fi
    msg "[self-update] 릴리스 고정: $LATEST_TAG" "[self-update] pinned to release: $LATEST_TAG"
  fi
else
  msg "[self-update] .git 없음 -> pull 건너뜀 (로컬 체크아웃 사용)" \
      "[self-update] no .git -> skipping pull (using local checkout)"
fi

# 4) Self-targeted, non-interactive update. --no-pull because we already
#    pulled above; exec so update.sh's exit status is this script's.
msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
    "[self-update] updating this instance: $SELF_ROOT"
exec "$REPO_ROOT/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@"
