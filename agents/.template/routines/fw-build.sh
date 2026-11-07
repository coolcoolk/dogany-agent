#!/bin/bash
# fw-build.sh -- "which framework build is this instance on?", one command,
# the same on every instance (DGN-1587).
#
# WHY: consume verification used to be `git describe --tags` inside
# .dogany-consume, a checkout only some instances have. A direct-install
# instance had no such tree, so every consume check there became a manual
# install-hash comparison. And .instance.conf itself came in three shapes:
# DOGANY_FW_TAG (update.sh landing stamp), FW_TAG absent with
# FW_VERSION + FW_REPO_SHA (an older mint), and DOGANY_FW_VERSION alone,
# which never carries the pre-release suffix (2.6.0 for every 2.6.0-dev.N).
#
# The verdict now lives in the field every instance carries -- mint.sh and
# update.sh both write DOGANY_FW_VERSION + DOGANY_FW_REPO_SHA + DOGANY_FW_TAG
# -- and this script is the one reader. A consume checkout is not consulted;
# a leader and a direct-install instance answer the same way.
#
# Usage: bash routines/fw-build.sh [--root <instance_root>]
#   stdout: the exact tag (e.g. v2.6.0-dev.23), or "unknown"
#   stderr: one line naming the source and whether it was verified
#   exit 0  exact tag known; when DOGANY_REPO_ROOT is a git checkout, the
#           tag's commit also equals DOGANY_FW_REPO_SHA (or no SHA recorded)
#   exit 2  no .instance.conf under the root
#   exit 3  no exact tag: untagged build, or nothing usable recorded
#   exit 4  DOGANY_FW_TAG and DOGANY_FW_REPO_SHA name different commits
#
# Resolution (same rules update.sh uses for its drift baseline):
#   1. DOGANY_FW_TAG, only while its X.Y.Z equals DOGANY_FW_VERSION (a tag
#      left by an earlier landing is stale -- DGN-1888); "unknown" = absent.
#   2. else the tag pointing AT DOGANY_FW_REPO_SHA in DOGANY_REPO_ROOT,
#      stable preferred over pre-release (mint.sh's pick rule).
#   3. else unknown (exit 3).
#
# Read-only. bash 3.2 compatible. ASCII only.
set -u

ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --root=*) ROOT="${1#--root=}"; shift ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "fw-build: unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$ROOT" ] || ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/.instance.conf"
if [ ! -f "$CONF" ]; then
  echo "unknown"
  echo "fw-build: no .instance.conf under $ROOT" >&2
  exit 2
fi

conf_val() { sed -n "s/^$1=//p" "$CONF" | head -n1 | tr -d '[:space:]'; }
xyz() { printf '%s' "${1#v}" | sed -e 's/[-+].*$//'; }

ver="$(conf_val DOGANY_FW_VERSION)"
tag="$(conf_val DOGANY_FW_TAG)"
sha="$(conf_val DOGANY_FW_REPO_SHA)"
repo="$(conf_val DOGANY_REPO_ROOT)"
[ "$tag" = "unknown" ] && tag=""
[ "$sha" = "unknown" ] && sha=""
have_repo=0
[ -n "$repo" ] && git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 && have_repo=1

src=""
if [ -n "$tag" ]; then
  if [ -z "$ver" ] || [ "$(xyz "$tag")" = "$(xyz "$ver")" ]; then
    src="DOGANY_FW_TAG"
  else
    echo "fw-build: DOGANY_FW_TAG=$tag is stale (installed X.Y.Z is $ver) -- ignored" >&2
    tag=""
  fi
fi

if [ -z "$tag" ] && [ -n "$sha" ] && [ "$have_repo" = 1 ]; then
  at="$(git -C "$repo" tag --points-at "$sha" 2>/dev/null)"
  if [ -n "$at" ]; then
    tag="$(printf '%s\n' "$at" | { grep -v -- '-' || true; } | sort -V | tail -n1)"
    [ -n "$tag" ] || tag="$(printf '%s\n' "$at" | sort -V | tail -n1)"
    src="DOGANY_FW_REPO_SHA ${sha} (tag pointing at it in $repo)"
  fi
fi

if [ -z "$tag" ]; then
  echo "unknown"
  near=""
  [ -n "$sha" ] && [ "$have_repo" = 1 ] \
    && near="$(git -C "$repo" describe --tags "$sha" 2>/dev/null || true)"
  echo "fw-build: no exact tag -- DOGANY_FW_VERSION=${ver:-<absent>} DOGANY_FW_REPO_SHA=${sha:-<absent>}${near:+ (nearest: $near)}" >&2
  exit 3
fi

if [ "$src" = "DOGANY_FW_TAG" ]; then
  if [ "$have_repo" = 1 ] && [ -n "$sha" ]; then
    tsha="$(git -C "$repo" rev-parse -q --verify "${tag}^{commit}" 2>/dev/null || true)"
    if [ -z "$tsha" ]; then
      echo "$tag"
      echo "fw-build: source DOGANY_FW_TAG; unverified (tag not in $repo)" >&2
      exit 0
    fi
    if [ "$tsha" != "$sha" ]; then
      echo "$tag"
      echo "fw-build: MISMATCH -- DOGANY_FW_TAG=$tag is commit $tsha but DOGANY_FW_REPO_SHA=$sha" >&2
      exit 4
    fi
    echo "$tag"
    echo "fw-build: source DOGANY_FW_TAG; verified (= DOGANY_FW_REPO_SHA $sha)" >&2
    exit 0
  fi
  echo "$tag"
  echo "fw-build: source DOGANY_FW_TAG; unverified (no DOGANY_FW_REPO_SHA or no git checkout at DOGANY_REPO_ROOT)" >&2
  exit 0
fi

echo "$tag"
echo "fw-build: source $src" >&2
exit 0
