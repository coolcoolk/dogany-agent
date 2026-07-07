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
#   3. Read DOGANY_REPO_ROOT from .instance.conf and `git pull --ff-only`
#      the framework repo FIRST, so the newest framework AND the newest
#      update.sh are present before we invoke update.sh (a stale instance
#      must not run the old, un-gated update.sh against itself).
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

# 3b) Pull the newest framework + update.sh FIRST, so we run the current,
#     gated update.sh -- not the stale copy this instance may have shipped with.
if [ -d "$REPO_ROOT/.git" ]; then
  msg "[self-update] 프레임워크 최신화: git pull --ff-only ($REPO_ROOT)" \
      "[self-update] fetching latest framework: git pull --ff-only ($REPO_ROOT)"
  git -C "$REPO_ROOT" pull --ff-only \
    || die "git pull --ff-only failed in $REPO_ROOT (resolve manually, then re-run)"
else
  msg "[self-update] .git 없음 -> pull 건너뜀 (로컬 체크아웃 사용)" \
      "[self-update] no .git -> skipping pull (using local checkout)"
fi

# 4) Self-targeted, non-interactive update. --no-pull because we already
#    pulled above; exec so update.sh's exit status is this script's.
msg "[self-update] 이 인스턴스 업데이트: $SELF_ROOT" \
    "[self-update] updating this instance: $SELF_ROOT"
exec "$REPO_ROOT/update.sh" --root "$SELF_ROOT" --no-pull --yes "$@"
