#!/bin/bash
# cadence-gate.sh -- DGN-731 public-sync cadence: VERSION gap alarm + brick prevention.
#
# Sourced by publish.sh (and any future promotion gate) to enforce the two
# machine-visible forcing points added by DGN-731:
#
#   FP2 -- Drift alarm (interphone):
#     At the publish gate, measure the gap between the last-published PUBLIC
#     VERSION and the CANONICAL stable VERSION.  WARN when gap >= CADENCE_WARN.
#
#   FP3 -- Jump safety / brick prevention:
#     If the gap exceeds CADENCE_BLOCK_GAP OR the gap range contains a
#     data/schema migration (user_version / EXPECTED_USER_VERSION bump,
#     database/migrations/*.sql addition), BLOCK a straight snapshot push.
#     Require step-through (intermediate versions) or a pre-run jump-migration
#     test.  Small gap + no migration = pass frictionlessly.
#
# Thresholds (default, named constants):
#   CADENCE_WARN       -- gap (minor) at which WARN fires        [default: 1]
#   CADENCE_BLOCK_GAP  -- gap at/above which push is blocked     [default: 2]
#   Block also fires when gap >= 1 AND migration-in-gap = yes.
#
# Callers set these before sourcing, or leave defaults.
#
# Public functions:
#   cadence_read_pub_version   REPO PUBLIC_REMOTE PUBLIC_BRANCH
#       -> prints last-published version (X.Y.Z) or empty if none; 0 always.
#
#   cadence_minor_gap  VER_A VER_B
#       -> prints absolute minor-version gap (|minor(B)-minor(A)|); 0 always.
#
#   cadence_migration_in_gap   REPO  TAG_FROM  TAG_TO
#       -> prints "yes" if database/migrations/*.sql added OR EXPECTED_USER_VERSION
#          bumped between TAG_FROM and TAG_TO; "no" otherwise.
#          Returns 0 always (caller reads stdout).
#          Graceful: if tags not resolvable -> prints "unknown".
#
#   cadence_check  REPO  CANON_VER  PUB_VER  PUBLIC_REMOTE  PUBLIC_BRANCH  MODE
#       -> runs FP2+FP3 checks.  MODE = "execute" -> hard die on BLOCK;
#          "dry-run" -> WARN on BLOCK (non-fatal, same as publish.sh gate_fail).
#          Prints gate lines compatible with publish.sh formatting.
#          Returns 0 (pass/warn) or exits non-zero (block in execute mode).
#
# Source idiom (publish.sh):
#   CADENCE_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   . "$CADENCE_SCRIPT_DIR/lib/cadence-gate.sh"
#
# This file is English/ASCII only.

# ---- defaults ----------------------------------------------------------------
: "${CADENCE_WARN:=1}"
: "${CADENCE_BLOCK_GAP:=2}"

# ---- internal helpers --------------------------------------------------------

# version_minor <X.Y.Z> -> prints the minor component (Y as integer)
_cadence_minor() {
  printf '%s\n' "$1" | awk -F. '{ print $2 + 0 }'
}

# version_major <X.Y.Z> -> prints the major component (X as integer)
_cadence_major() {
  printf '%s\n' "$1" | awk -F. '{ print $1 + 0 }'
}

# _cadence_valid_ver <X.Y.Z> -> 0 if valid semver triple, 1 otherwise
_cadence_valid_ver() {
  printf '%s\n' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'
}

# ---- public: cadence_read_pub_version ----------------------------------------
# cadence_read_pub_version  REPO  PUBLIC_REMOTE  PUBLIC_BRANCH
# Reads the last-published VERSION from the public remote commit subject.
# publish.sh sets the subject to "Dogany vX.Y.Z -- curated public snapshot".
# If the remote is unreachable or has no versioned snapshot, prints empty.
# Re-uses the same fetch+log-subject pattern as publish.sh gate (4).
cadence_read_pub_version() {
  local repo="$1" remote="$2" branch="$3" subj ver
  git -C "$repo" fetch "$remote" "$branch" --quiet 2>/dev/null || return 0
  subj="$(git -C "$repo" log -1 --format=%s "$remote/$branch" 2>/dev/null)"
  ver="$(printf '%s\n' "$subj" | sed -nE 's/^Dogany v([0-9]+\.[0-9]+\.[0-9]+) .*/\1/p')"
  printf '%s' "$ver"
}

# ---- public: cadence_minor_gap -----------------------------------------------
# cadence_minor_gap  VER_A  VER_B
# Prints the absolute minor-version gap when major versions match.
# Cross-major gap -> prints major*1000 (sentinel: always >= CADENCE_BLOCK_GAP).
# Graceful fallback for empty/invalid -> prints 0.
cadence_minor_gap() {
  local a="$1" b="$2"
  if ! _cadence_valid_ver "$a" || ! _cadence_valid_ver "$b"; then
    printf '0'
    return 0
  fi
  local ma mb
  ma="$(_cadence_major "$a")"
  mb="$(_cadence_major "$b")"
  if [ "$ma" != "$mb" ]; then
    # Cross-major: treat as a very large gap to force BLOCK.
    printf '%d' $(( (mb > ma ? mb - ma : ma - mb) * 1000 ))
    return 0
  fi
  local mna mnb diff
  mna="$(_cadence_minor "$a")"
  mnb="$(_cadence_minor "$b")"
  diff=$(( mnb > mna ? mnb - mna : mna - mnb ))
  printf '%d' "$diff"
}

# ---- public: cadence_migration_in_gap ----------------------------------------
# cadence_migration_in_gap  REPO  TAG_FROM  TAG_TO
# Detects whether any data/schema migration was introduced between the two tags.
# Detection criteria (either is sufficient):
#   (a) A file matching database/migrations/NNN_*.sql was ADDED in the range.
#       Added = appeared in git diff --name-status A lines.
#   (b) EXPECTED_USER_VERSION in database/lifekit.py changed value (grep diff
#       for the line; if it appears in + lines of the patch, a bump occurred).
#   (c) PRAGMA user_version in database/schema.sql changed value (same: + line
#       in patch means new version was written).
# Output: "yes" / "no" / "unknown" (if tags unresolvable).
cadence_migration_in_gap() {
  local repo="$1" tag_from="$2" tag_to="$3"

  # Validate tags are resolvable.
  if ! git -C "$repo" rev-parse -q --verify "refs/tags/v${tag_from}" >/dev/null 2>&1; then
    printf 'unknown'
    return 0
  fi
  if ! git -C "$repo" rev-parse -q --verify "refs/tags/v${tag_to}" >/dev/null 2>&1; then
    printf 'unknown'
    return 0
  fi

  local range="v${tag_from}..v${tag_to}"

  # (a) New migration file added in the range.
  local new_migs
  new_migs="$(git -C "$repo" diff --name-status "$range" -- 'database/migrations/*.sql' 2>/dev/null \
    | awk '$1=="A"' | grep -c .)"
  if [ "${new_migs:-0}" -gt 0 ]; then
    printf 'yes'
    return 0
  fi

  # (b) EXPECTED_USER_VERSION bump in lifekit.py.
  local lifekit_change
  lifekit_change="$(git -C "$repo" diff "$range" -- 'database/lifekit.py' 2>/dev/null \
    | grep '^+' | grep -v '^+++' | grep 'EXPECTED_USER_VERSION' | grep -c .)"
  if [ "${lifekit_change:-0}" -gt 0 ]; then
    printf 'yes'
    return 0
  fi

  # (c) PRAGMA user_version bump in schema.sql.
  local schema_change
  schema_change="$(git -C "$repo" diff "$range" -- 'database/schema.sql' 2>/dev/null \
    | grep '^+' | grep -v '^+++' | grep -i 'pragma user_version' | grep -c .)"
  if [ "${schema_change:-0}" -gt 0 ]; then
    printf 'yes'
    return 0
  fi

  printf 'no'
}

# ---- public: cadence_check ---------------------------------------------------
# cadence_check  REPO  CANON_VER  PUB_VER  PUBLIC_REMOTE  PUBLIC_BRANCH  MODE
#
# FP2: if gap >= CADENCE_WARN -> gate_fail (WARN / BLOCK per MODE).
# FP3: if gap >= CADENCE_BLOCK_GAP OR (gap >= 1 AND migration-in-gap=yes)
#      -> gate_fail with BLOCK message that names the mitigation path.
#
# gate/pass/gate_fail must be defined in the caller (publish.sh already has
# them; any future caller must define the same interface).
cadence_check() {
  local repo="$1" canon_ver="$2" pub_ver="$3" remote="$4" branch="$5" mode="$6"

  printf '\n== gate C1: public-sync cadence gap (DGN-731 FP2/FP3)\n'

  # No published version yet -> pass frictionlessly (first publish).
  if [ -z "$pub_ver" ]; then
    printf '   PASS no prior public snapshot -- first publish, no gap to measure\n'
    return 0
  fi

  # Validate both versions.
  if ! _cadence_valid_ver "$canon_ver" || ! _cadence_valid_ver "$pub_ver"; then
    printf '   WARN cadence-gate: cannot parse versions (canon=%s pub=%s) -- skipping\n' \
      "$canon_ver" "$pub_ver"
    return 0
  fi

  local gap
  gap="$(cadence_minor_gap "$pub_ver" "$canon_ver")"

  printf '   public: v%s  canonical: v%s  minor-gap: %s\n' "$pub_ver" "$canon_ver" "$gap"

  # FP2: drift alarm.
  if [ "$gap" -ge "$CADENCE_WARN" ]; then
    printf '   WARN gap=%d >= CADENCE_WARN=%d -- publish has drifted from stable; run publish to close\n' \
      "$gap" "$CADENCE_WARN"
  else
    printf '   PASS gap=%d < CADENCE_WARN=%d -- no drift\n' "$gap" "$CADENCE_WARN"
    return 0
  fi

  # FP3: brick prevention.
  # Check migration-in-gap first (needed for both BLOCK conditions).
  local mig_in_gap="no"
  if [ "$gap" -ge 1 ]; then
    mig_in_gap="$(cadence_migration_in_gap "$repo" "$pub_ver" "$canon_ver")"
  fi

  local block_reason=""
  if [ "$gap" -ge "$CADENCE_BLOCK_GAP" ]; then
    block_reason="gap=${gap} >= CADENCE_BLOCK_GAP=${CADENCE_BLOCK_GAP}"
  fi
  if [ "$mig_in_gap" = "yes" ] && [ "$gap" -ge 1 ]; then
    if [ -n "$block_reason" ]; then
      block_reason="${block_reason} AND migration-in-gap=yes"
    else
      block_reason="migration-in-gap=yes (gap=${gap})"
    fi
  fi

  if [ -n "$block_reason" ]; then
    local msg
    msg="straight snapshot BLOCKED: ${block_reason}"
    msg="${msg} -- required: step-through (publish each intermediate version)"
    if [ "$mig_in_gap" = "yes" ]; then
      msg="${msg} OR run a jump-migration test spanning v${pub_ver}..v${canon_ver} before publishing"
    fi
    if [ "$mode" = "execute" ]; then
      printf '\n'
      printf 'XX PUBLISH BLOCKED -- %s\n' "$msg" >&2
      printf '   (fix the cause and rerun; there is no --force)\n' >&2
      exit 1
    else
      printf '   WARN (dry-run, non-fatal; would BLOCK --execute) -- %s\n' "$msg"
    fi
  elif [ "$mig_in_gap" = "unknown" ]; then
    printf '   WARN migration-in-gap check inconclusive (tags not resolvable) -- verify manually before --execute\n'
  else
    printf '   PASS gap=%d but migration-in-gap=%s and gap < CADENCE_BLOCK_GAP=%d -- warn only, no block\n' \
      "$gap" "$mig_in_gap" "$CADENCE_BLOCK_GAP"
  fi
}
