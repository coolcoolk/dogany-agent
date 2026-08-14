#!/bin/bash
# cadence-gate.sh -- DGN-731 public-sync cadence: VERSION gap telemetry.
#
# Sourced by publish.sh (and any future promotion gate).
#
#   FP2 -- Drift alarm (interphone):
#     At the publish gate, measure the gap between the last-published PUBLIC
#     VERSION and the CANONICAL stable VERSION.  WARN when gap >= CADENCE_WARN.
#
#   FP3 (RETIRED as a BLOCK -- DGN-880):
#     The original FP3 blocked a straight snapshot push at gap >=
#     CADENCE_BLOCK_GAP or when the gap contained a migration. DGN-880's
#     mirror-forward publisher ALWAYS steps one stable tag at a time
#     (ascending), so the multi-version straight-jump this BLOCK guarded
#     against is unreachable by construction -- brick prevention is now
#     STRUCTURAL, not gate-enforced. C1 therefore never dies: it reports the
#     gap (and migration-in-gap, as telemetry for manual pin-step recovery via
#     update.sh intermediate tags) and always returns 0. This also removes the
#     gap=1+migration deadlock the old rule could produce (DGN-880 A1).
#
# Thresholds (default, named constants):
#   CADENCE_WARN       -- gap (minor) at which the WARN fires    [default: 1]
#   CADENCE_BLOCK_GAP  -- retained for interface compat only; no longer
#                         triggers any block                     [default: 2]
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
#       -> prints "yes" if database/migrations/*.sql added OR schema.sql
#          PRAGMA user_version bumped between TAG_FROM and TAG_TO; "no"
#          otherwise (historical pre-split ranges only -- DGN-803 LS-5).
#          Returns 0 always (caller reads stdout).
#          Graceful: if tags not resolvable -> prints "unknown".
#
#   cadence_check  REPO  CANON_VER  PUB_VER  PUBLIC_REMOTE  PUBLIC_BRANCH  MODE
#       -> runs the FP2 gap telemetry. WARN-only (DGN-880): NEVER dies, ALWAYS
#          returns 0 in every MODE (the MODE arg is retained for interface
#          compatibility only). Prints gate lines compatible with publish.sh
#          formatting; migration-in-gap is reported as telemetry.
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
#   (b) PRAGMA user_version in database/schema.sql changed value (+ line
#       in patch means new version was written).
# DGN-803 LS-5: the lifekit.py EXPECTED_USER_VERSION pin-diff clause was
# REMOVED here -- lifekit content left the canonical tree (independent pack),
# so the canon pin diff is structurally empty for post-split ranges. The
# migration-cadence forcing point it carried now lives in the pack gate:
# scripts/pack/compat-lint.sh CHECK C3 (EXPECTED_USER_VERSION == max(migrations)
# == schema PRAGMA 3-point consistency + reversible marker), enforced
# publish-side AND install-side (LS-3). Criteria (a)/(b) stay: they still
# detect migrations inside HISTORICAL (pre-split) tag ranges for the public
# mirror step-through gate, and are naturally inert for post-split ranges.
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

  # (DGN-803 LS-5) lifekit.py EXPECTED_USER_VERSION pin-diff clause removed:
  # relocated to compat-lint C3 (see header comment).

  # (b) PRAGMA user_version bump in schema.sql.
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
# FP2: if gap >= CADENCE_WARN -> WARN (report the drift; publish climbs it).
# FP3 (DGN-880): WARN/telemetry ONLY -- never dies, never exits, in ANY mode.
# The mirror-forward publisher steps one stable tag at a time, so the straight
# multi-version jump the old BLOCK guarded against cannot occur; brick
# prevention is structural. Migration-in-gap is still measured and reported:
# intermediate tags must EXIST for manual pin-step recovery (update.sh), and
# the telemetry keeps that visible (DGN-880 A2).
#
# gate/pass/gate_fail must be defined in the caller (publish.sh already has
# them; any future caller must define the same interface). MODE is retained
# for interface compatibility; it no longer changes behavior.
cadence_check() {
  local repo="$1" canon_ver="$2" pub_ver="$3" remote="$4" branch="$5" mode="$6"

  printf '\n== gate C1: public-sync cadence gap (DGN-731 FP2; WARN-only per DGN-880)\n'

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

  # FP2: drift alarm (telemetry; the mirror-forward run itself closes the gap).
  if [ "$gap" -ge "$CADENCE_WARN" ]; then
    printf '   WARN gap=%d >= CADENCE_WARN=%d -- public mirror is behind; mirror-forward climbs it one step at a time\n' \
      "$gap" "$CADENCE_WARN"
  else
    printf '   PASS gap=%d < CADENCE_WARN=%d -- no drift\n' "$gap" "$CADENCE_WARN"
    return 0
  fi

  # Migration-in-gap telemetry (DGN-880: informational, never a block).
  local mig_in_gap="no"
  if [ "$gap" -ge 1 ]; then
    mig_in_gap="$(cadence_migration_in_gap "$repo" "$pub_ver" "$canon_ver")"
  fi
  if [ "$mig_in_gap" = "yes" ]; then
    printf '   INFO migration-in-gap=yes (v%s..v%s) -- intermediate tags stay published for manual pin-step recovery (update.sh)\n' \
      "$pub_ver" "$canon_ver"
  elif [ "$mig_in_gap" = "unknown" ]; then
    printf '   INFO migration-in-gap check inconclusive (tags not resolvable) -- telemetry only\n'
  else
    printf '   INFO migration-in-gap=no (v%s..v%s)\n' "$pub_ver" "$canon_ver"
  fi
  return 0
}
