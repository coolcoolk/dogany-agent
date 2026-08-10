#!/bin/bash
# publish.sh -- MF-5 allowlist export gate for the public Dogany product repo.
#
# DGN-604 Phase 1 (spec-v2 s5). Source model: a single PRIVATE canonical
# monorepo is the development tree; the PUBLIC repo (remote 'public') receives
# only a CURATED SNAPSHOT. A publish is one clean snapshot commit -- never a
# full-history push (that would leak future PoC/private commits).
#
# The gate is default-DENY: nothing exports unless a product.yaml unit that is
# BOTH visibility:public AND status:official explicitly OWNS the path via its
# `owns:` globs. Shared no-owner files (rules/, database/, docs/, repo-meta)
# never export. Ownership overlap (agent-template owns agents/.template/**,
# a module owns a subdir underneath) is resolved most-specific-owner-wins:
# the longest matching owns-glob wins. No exclusion globs -- specificity is
# the convention (M1 taxonomy rec A).
#
# On top of the allowlist, an F9 gate blocks a domain-realdata / PoC-fixture
# CLASS from the export even when a path is owned (owner-pattern sweep cannot
# see live/instance data; DGN-120 pack-RAG leak precedent).
#
# Two DECLARATIVE injection/exclusion classes sit beside the unit-owns
# allowlist (both enumerated, no broad globs -- they must not become a
# default-deny backdoor):
#   REPO_META : public-repo essentials that no unit owns (LICENSE, NOTICE,
#               READMEs, CHANGELOG, .gitignore, and the exact docs/img images
#               the READMEs reference). Injected INTO the export (FIX 1).
#   THIRD_PARTY_EXCLUDE : third-party IP that rides a broad public parent glob
#               but is NOT our product (agent-browser = Vercel Labs, M1
#               inventory-excluded / uncataloged). Removed FROM the export even
#               when a public+official parent glob claims it (FIX 2).
#
# This build produces the staged export tree and a dry-run report. The real
# remote-push code path exists but is guarded behind an explicit non-default
# flag and MUST NOT be run in this milestone.
#
# Usage:
#   publish.sh                 dry-run (default): build staged export tree,
#                              run read-only gates, print export/exclude lists.
#                              Creates and pushes NOTHING to remote.
#   publish.sh --dry-run       explicit dry-run (same as no args).
#   publish.sh --report FILE   dry-run + write the markdown report to FILE.
#   publish.sh --execute       run ALL gates then push a snapshot commit to
#                              the public target. Guarded; not used in M3.
#   publish.sh -h | --help
#
# There is deliberately NO --force escape hatch. Fix the gate, not the gate.
#
# Gates (inherited from release.sh, MF-5 s5(d) -- lossless carry of
# (1)(2)(3)(5)(7)(8)+no-force+(9); gate (4) version rewritten for the plain
# vX.Y.Z framework train; gate (3) sweep retargeted from repo-tracked to the
# EXPORT ARTIFACT tree per s5(a)):
#   (1) clean canonical working tree     (git status --porcelain empty)
#   (2) canonical on main + up to date   (branch == main, HEAD == origin/main)
#   (E) build allowlist export tree      (default-deny + most-specific-owner +
#                                         F9 domain/PoC-fixture class block)
#   (3) secret sweep on EXPORT ARTIFACTS (secret-sweep.sh on the staged tree)
#   (4) VERSION gate: plain vX.Y.Z       (strict bump; NO prefix tags -- MF-3)
#  (C1) cadence gate: VERSION gap alarm + brick prevention (DGN-731 FP2/FP3)
#         FP2: WARN when public vs canonical minor gap >= CADENCE_WARN (dflt 1)
#         FP3: BLOCK straight push when gap >= CADENCE_BLOCK_GAP (dflt 2) OR
#              migration-in-gap detected; requires step-through or jump-test.
#   (5) CHANGELOG has this version       (heading entry for the exact version)
#   (7) publish snapshot to public       (execute only; single curated commit)
#  (7a) tag snapshot on public           (execute only; push plain vX.Y.Z tag to
#                                         public remote so tags track content)
#   (8) post-push assert                 (execute only)
#   (9) exit checklist                   (manual, printed not automated)

set -u

# ---- config -----------------------------------------------------------------
# Canonical (private) source tree. Default to this script's repo root so the
# script works from a worktree without extra env.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# DGN-731: source the cadence gate library (FP2 drift alarm + FP3 brick
# prevention). The library defines cadence_check() and friends; it reads
# CADENCE_WARN / CADENCE_BLOCK_GAP from env or uses its defaults.
# CADENCE_WARN      -- minor-gap at which WARN fires             [default: 1]
# CADENCE_BLOCK_GAP -- minor-gap at/above which push is blocked  [default: 2]
CADENCE_LIB="$SCRIPT_DIR/lib/cadence-gate.sh"
[ -f "$CADENCE_LIB" ] || { echo "ERROR: cadence-gate.sh not found: $CADENCE_LIB" >&2; exit 1; }
# shellcheck source=lib/cadence-gate.sh
. "$CADENCE_LIB"
CANON="${DOGANY_CANONICAL_REPO:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)}"
SWEEP="${DOGANY_SWEEP_SCRIPT:-$CANON/scripts/secret-sweep.sh}"
# Public export target: git remote name + branch in the canonical repo.
PUBLIC_REMOTE="${DOGANY_PUBLIC_REMOTE:-public}"
PUBLIC_BRANCH="${DOGANY_PUBLIC_BRANCH:-main}"
# Staging dir for the curated export tree.
STAGE="${DOGANY_PUBLISH_STAGE:-$(mktemp -d "${TMPDIR:-/tmp}/dogany-publish.XXXXXX")}"

MODE="dry-run"
REPORT_FILE=""
ARGS=("$@")
i=0
while [ $i -lt ${#ARGS[@]} ]; do
  a="${ARGS[$i]}"
  case "$a" in
    --dry-run) MODE="dry-run" ;;
    --execute) MODE="execute" ;;
    --report)
      i=$(( i + 1 ))
      REPORT_FILE="${ARGS[$i]:-}"
      [ -n "$REPORT_FILE" ] || { echo "ERROR: --report requires a FILE arg" >&2; exit 2; }
      ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a (no --force exists on purpose)" >&2; exit 2 ;;
  esac
  i=$(( i + 1 ))
done

die() { echo ""; echo "XX PUBLISH BLOCKED -- $*" >&2; echo "   (fix the cause and rerun; there is no --force)" >&2; exit 1; }
gate() { echo ""; echo "== gate $*"; }
pass() { echo "   PASS $*"; }
info() { echo "   -- $*"; }
# cadence-gate failure: hard in --execute, non-fatal WARN in dry-run (a release
# cadence gate like VERSION-bump / CHANGELOG is not meaningful in an export
# rehearsal; the dry-run deliverable is the export/exclude report).
gate_fail() {
  if [ "$MODE" = "execute" ]; then die "$*"; fi
  echo "   WARN (dry-run, non-fatal; would BLOCK --execute) -- $*"
}

[ -d "$CANON" ] || die "canonical repo not found: $CANON"
git -C "$CANON" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "not a git repo: $CANON"
[ -f "$SWEEP" ] || die "sweep script not found: $SWEEP"

echo "=== dogany publish gate :: canonical=$CANON mode=$MODE ==="

# =============================================================================
# product.yaml parsing (flat schema, bash 3.2 / no yq).
# =============================================================================
# The manifest schema is a flat, predictable YAML (M1). We only need scalar
# fields (id/type/visibility/status/version/publish_target/default_enabled)
# and the `owns:` list of quoted globs. A minimal line parser is sufficient
# and avoids a yq dependency (RULES ladder: stdlib/one-liner before dep).

py_scalar() {  # $1=file $2=key  -> prints value or empty
  sed -n "s/^${2}:[[:space:]]*//p" "$1" | head -1 \
    | sed 's/[[:space:]]*#.*$//' | sed 's/^["'\'']//;s/["'\'']$//' \
    | sed 's/[[:space:]]*$//'
}

py_owns() {  # $1=file  -> prints one owns glob per line (unquoted)
  awk '
    /^owns:[[:space:]]*$/ { in_owns=1; next }
    /^[a-zA-Z_]+:/ { if (in_owns) in_owns=0 }
    in_owns && /^[[:space:]]*-[[:space:]]*/ {
      line=$0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      sub(/[[:space:]]*#.*$/, "", line)
      gsub(/^["'\'']|["'\'']$/, "", line)
      gsub(/[[:space:]]*$/, "", line)
      if (line != "") print line
    }
  ' "$1"
}

# =============================================================================
# F9 domain-realdata / PoC-fixture class (spec s5(c)).
# =============================================================================
# Paths matching any pattern below are blocked from export EVEN IF owned by a
# public+official unit. These are live/instance/domain-realdata surfaces that
# the owner-glob sweep cannot reason about (DGN-120 pack-RAG leak class).
# Patterns are shell case-globs matched against repo-relative paths.
# DB / dotenv / runtime patterns are LOCATION-INDEPENDENT on purpose: a SQLite
# db or a dotenv variant is domain-realdata wherever it sits, not only under a
# database/ dir (grill DGN-604 M3: `newunit/cache.db`, `newunit/.env.local`,
# `*.sqlite`, `.env.production` slipped the old database/-anchored patterns).
# `case` glob `*` spans '/', so a bare `*.db` matches at any depth.
F9_PATTERNS='
packs/health-trainer/warg/*
packs/dev/refdev/*
*.db
*.db-wal
*.db-shm
*.db-journal
*.sqlite
*.sqlite3
*/runtime/*
*/logs/*
*/sessions.json
sessions.json
*.env
.env
.env.*
*/.env
*/.env.*
'

# dotenv TEMPLATE files are legitimate public artifacts (they ship placeholder
# keys, not secrets) and MUST NOT be F9-blocked. `.env.example` /
# `.env.sample` / `.env.template` (and path-suffixed forms) are exempt; the
# real-secret variants (.env, .env.local, .env.production, ...) stay blocked.
f9_env_template() {  # $1=path -> 0 if it is a dotenv TEMPLATE (exempt)
  case "$1" in
    *.env.example|*.env.sample|*.env.template|.env.example|.env.sample|.env.template) return 0 ;;
  esac
  return 1
}

f9_blocked() {  # $1 = repo-relative path -> 0 if blocked by F9 class
  local p="$1" pat
  # dotenv templates are public artifacts -> never F9-blocked.
  f9_env_template "$p" && return 1
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    # shellcheck disable=SC2254
    case "$p" in $pat) return 0 ;; esac
  done <<EOF
$F9_PATTERNS
EOF
  return 1
}

# =============================================================================
# FIX 2 -- THIRD_PARTY_EXCLUDE class (agent-browser / Vercel Labs IP).
# =============================================================================
# agent-browser is NOT our product: M1 inventory-excluded it (Vercel Labs IP,
# uncataloged in product.yaml, auto-exposed as a platform capability like
# web-search). It has no product.yaml, so most-specific-owner-wins attributes
# its files to the broad agent-template parent glob (agents/.template/**) and it
# would leak into the export. This is a DECLARATIVE, explicitly enumerated
# exclusion -- a distinct concern from F9 (domain-realdata): here we exclude
# third-party IP that we must not redistribute. NOT a private-visibility unit
# (giving it a manifest would contradict M1's "uncataloged"), NOT a hardcoded
# one-off (that was the last resort) -- a named class with listed subtrees.
# Only the Vercel-owned skill subtree is listed; OUR framework browser-auth
# wrapper (routines/browser-auth-open.sh, docs/BROWSER-AUTH-CONVENTION.md,
# DGN-609) is ours and is deliberately NOT excluded here.
THIRD_PARTY_PATTERNS='
*/skills-bundle/agent-browser/*
*/skills-bundle/agent-browser
'

third_party_excluded() {  # $1 = repo-relative path -> 0 if third-party IP
  local p="$1" pat
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    # shellcheck disable=SC2254
    case "$p" in $pat) return 0 ;; esac
  done <<EOF
$THIRD_PARTY_PATTERNS
EOF
  return 1
}

# =============================================================================
# FIX 1 -- REPO_META injection class (public-repo essentials, no unit owns).
# =============================================================================
# The strict allowlist drops LICENSE / NOTICE / READMEs / CHANGELOG / .gitignore
# and the README-referenced docs images because no unit `owns:` them (they are
# repo-wide, not product-scoped). A public repo cannot ship without them. This
# is an EXPLICIT, per-file injection -- a declarative repo-meta CLASS distinct
# from "a unit owns X". To keep it from becoming a default-deny backdoor it is
# a fixed enumerated list, NOT a glob over docs/ or the repo root: only the
# named files plus exactly the docs/img images the READMEs reference. Each
# injected file must still be git-tracked AND pass the secret sweep and the
# F9 / third-party classes (belt: injection cannot smuggle a blocked path).
REPO_META_FILES='
LICENSE
NOTICE
README.md
README-ko.md
CHANGELOG.md
.gitignore
'

# =============================================================================
# FIX 3 -- INSTALL_SHARED injection class (install/mint-essential shared files).
# =============================================================================
# The public repo is a real distribution: `clone -> install.sh -> mint.sh`
# scaffolds a working instance from it (README Setup + self-update consume this
# repo's published tag). mint.sh dereferences the template's shared symlinks
# (`agents/.template/RULES.md -> ../../rules/RULES.md`) and copies a fixed set
# of hoisted shared code out of `database/` and `rules/` INTO the instance.
# None of those shared roots is owned by any product.yaml unit, so the strict
# allowlist drops them: the symlink-escape gate discards the template RULES.md
# link (its `rules/RULES.md` target is not exported), and `database/schema.sql`
# et al fall under no-owner. The empirically-observed break (DGN-604 M3 verify):
# minted instance had NO RULES.md and NO schema.sql -> lifekit.db never inits,
# baseline @RULES.md include is dangling. A public snapshot that cannot mint is
# not a product.
#
# This is a DECLARATIVE, per-file allowlist -- exactly the files mint.sh reads
# by name, no more. NOT a glob over rules/ (that would ship dev surface); the
# enumeration keeps default-deny intact. It rides the SAME inject_meta belt
# as REPO_META (must be git-tracked, must survive F9 / third-party), so
# injection can never smuggle a blocked path. The list mirrors mint.sh 1c
# (shared-root bundle) + the template symlink target; if mint.sh's bundle
# list changes, this list must track it.
# DGN-803 LS-5: the database/* entries (schema.sql, lifekit.py, CLI,
# selectors) left this list with the lifekit extraction -- that content now
# ships as the independent lifekit pack and reaches instances via
# pack_install.sh at kit activation, never via mint/publish.
INSTALL_SHARED_FILES='
rules/RULES.md
rules/USER.example.md
'

# docs/img images the READMEs actually reference. Derived at runtime by
# scraping README.md + README-ko.md for docs/img/... paths, then intersecting
# with git-tracked files. This keeps the set tied to real references (a README
# that stops referencing an image stops shipping it) while never opening docs/
# wholesale. If scraping finds nothing (unexpected), the set is simply empty --
# fail-closed, never a wildcard.
repo_meta_readme_images() {  # prints repo-relative docs/img paths, one per line
  local readmes="README.md README-ko.md" r
  for r in $readmes; do
    [ -f "$CANON/$r" ] || continue
    grep -oE 'docs/img/[A-Za-z0-9._/-]+\.(png|jpg|jpeg|gif|svg|webp)' "$CANON/$r" 2>/dev/null
  done | sort -u
}

# =============================================================================
# most-specific-owner-wins glob matching (spec s5(b) / M1 rec A).
# =============================================================================
# owns globs use `**` (recursive) and plain `*`. We translate a glob to a
# shell case-pattern and pick, among all matching owned globs, the one with the
# greatest literal length (most specific). Ties are impossible in practice
# (distinct unit subtrees); if they occur we keep the first, deterministic by
# unit sort order.

glob_to_case() {  # $1 = owns glob -> prints a shell case pattern
  # "foo/**" should match foo/ and everything under it. Represent as two
  # alternatives is not possible in a single case-glob, so we match "foo/*"
  # (any depth via shell's greedy *, which spans '/' in case patterns) and
  # also the bare "foo". Emit the "prefix*" form; bare-dir handled by caller.
  local g="$1"
  # Strip a trailing /** -> prefix marker
  case "$g" in
    */\*\*) printf '%s' "${g%/**}/" ;;   # directory-recursive
    *\*\*)  printf '%s' "${g%\*\*}" ;;    # bare ** suffix
    *)      printf '%s' "$g" ;;
  esac
}

# =============================================================================
# Load units.
# =============================================================================
UNIT_FILES="$(cd "$CANON" && find . -name product.yaml -not -path './.git/*' -not -path './.worktrees/*' | sed 's#^\./##' | sort)"
UNIT_TOTAL=0
[ -n "$UNIT_FILES" ] && UNIT_TOTAL="$(printf '%s\n' "$UNIT_FILES" | grep -c .)"
info "product.yaml units found: $UNIT_TOTAL"

# Selected (public+official) unit ids and their owns globs, collected as
# newline records "unit_id\tglob" for the allowlist, plus excluded-unit report.
SELECTED_OWNS=""     # lines: "<unit_id>\t<glob>"  (public+official)
SELECTED_IDS=""      # lines: "<unit_id>"
PRIVATE_OWNS=""      # lines: "<unit_id>\t<glob>"  (private OR non-official)
EXCLUDED_UNITS=""    # lines: "<unit_id>\t<visibility>/<status>\t<file>"

while IFS= read -r uf; do
  [ -z "$uf" ] && continue
  abs="$CANON/$uf"
  uid="$(py_scalar "$abs" id)"
  uvis="$(py_scalar "$abs" visibility)"
  ustat="$(py_scalar "$abs" status)"
  [ -n "$uid" ] || uid="(no-id:$uf)"
  if [ "$uvis" = "public" ] && [ "$ustat" = "official" ]; then
    SELECTED_IDS="${SELECTED_IDS}${uid}"$'\n'
    while IFS= read -r g; do
      [ -z "$g" ] && continue
      SELECTED_OWNS="${SELECTED_OWNS}${uid}	${g}"$'\n'
    done <<EOF
$(py_owns "$abs")
EOF
  else
    # Private / non-official unit: record its owns as an explicit BLOCKLIST.
    # A private child nested under a public parent's broad glob (e.g. the PoC
    # module spending-log under agent-template's agents/.template/**) would
    # otherwise leak via the parent glob, because most-specific-owner-wins only
    # considers PUBLIC+OFFICIAL owners. Honoring private owns as a block layer
    # realizes the spec's "PoC/private 제외" (M1 lock: spending-log whole
    # module = PoC, publish excluded).
    EXCLUDED_UNITS="${EXCLUDED_UNITS}${uid}	${uvis}/${ustat}	${uf}"$'\n'
    while IFS= read -r g; do
      [ -z "$g" ] && continue
      PRIVATE_OWNS="${PRIVATE_OWNS}${uid}	${g}"$'\n'
    done <<EOF
$(py_owns "$abs")
EOF
  fi
done <<EOF
$UNIT_FILES
EOF

SELECTED_COUNT=0
[ -n "$SELECTED_IDS" ] && SELECTED_COUNT="$(printf '%s' "$SELECTED_IDS" | grep -c .)"
info "public+official units selected: $SELECTED_COUNT"

# =============================================================================
# gate (E): build allowlist export tree.
# =============================================================================
gate "E: allowlist export tree (default-deny + owner + F9)"

# Enumerate all tracked files in the canonical repo. product.yaml manifests
# themselves are metadata and never export.
ALL_TRACKED="$(git -C "$CANON" ls-files)"

# For each tracked file, resolve the most-specific matching owned glob.
# Outputs:
#   EXPORT_LIST   -> "<unit_id>\t<path>"  (kept)
#   F9_BLOCKED    -> "<unit_id>\t<path>"  (owned but F9-blocked)
#   PRIV_BLOCKED  -> "<unit_id>\t<path>"  (claimed by public parent but a
#                                          private/PoC unit owns it -> excluded)
#   NOOWNER_LIST  -> "<path>"             (no owning public+official unit)
EXPORT_LIST=""
F9_BLOCKED_LIST=""
PRIV_BLOCKED_LIST=""
TP_BLOCKED_LIST=""   # third-party IP (agent-browser) claimed by a public glob
NOOWNER_LIST=""
META_LIST=""         # FIX 1 repo-meta files injected into the export

resolve_owner() {  # $1=owns-set  $2=path -> prints "<unit_id>\t<matchlen>" of most specific owner, or empty
  local owns_set="$1" path="$2" best_id="" best_len=-1 uid g cpat mlen
  while IFS=$'\t' read -r uid g; do
    [ -z "$uid" ] && continue
    cpat="$(glob_to_case "$g")"
    # bare directory glob "foo/" -> also match the dir prefix
    # shellcheck disable=SC2254
    case "$path" in
      $cpat*|$g)
        mlen=${#g}
        if [ "$mlen" -gt "$best_len" ]; then
          best_len=$mlen; best_id="$uid"
        fi
        ;;
    esac
  done <<EOF
$owns_set
EOF
  [ -n "$best_id" ] && printf '%s\t%s' "$best_id" "$best_len"
}

while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$(basename "$f")" in product.yaml) continue ;; esac
  owner="$(resolve_owner "$SELECTED_OWNS" "$f")"
  oid="${owner%%	*}"
  if [ -z "$oid" ]; then
    NOOWNER_LIST="${NOOWNER_LIST}${f}"$'\n'
    continue
  fi
  # Private/PoC block: if a private (non-official) unit ALSO owns this path
  # (typically via a more-specific child glob under a public parent), exclude
  # it. This prevents PoC/private subtrees from leaking through a broad public
  # parent glob (spec "PoC/private 제외"; M1 spending-log lock).
  priv="$(resolve_owner "$PRIVATE_OWNS" "$f")"
  pid="${priv%%	*}"
  if [ -n "$pid" ]; then
    PRIV_BLOCKED_LIST="${PRIV_BLOCKED_LIST}${pid}	${f}"$'\n'
    continue
  fi
  # FIX 2: third-party IP (agent-browser) rides the broad agent-template parent
  # glob but must never redistribute. Checked BEFORE F9/export.
  if third_party_excluded "$f"; then
    TP_BLOCKED_LIST="${TP_BLOCKED_LIST}${oid}	${f}"$'\n'
    continue
  fi
  if f9_blocked "$f"; then
    F9_BLOCKED_LIST="${F9_BLOCKED_LIST}${oid}	${f}"$'\n'
    continue
  fi
  EXPORT_LIST="${EXPORT_LIST}${oid}	${f}"$'\n'
done <<EOF
$ALL_TRACKED
EOF

# ---------------------------------------------------------------------------
# FIX 1: inject repo-meta essentials that no unit owns. Each candidate must be
# git-tracked and must survive the SAME block classes (F9 / third-party) --
# injection is not a bypass. (Repo-meta files are simple root/doc paths and
# never match those classes, but the belt makes that structural, not assumed.)
# ---------------------------------------------------------------------------
inject_meta() {  # $1 = repo-relative path
  local f="$1"
  # must be tracked
  case "$ALL_TRACKED" in
    *"$f"*) : ;;
    *) return 0 ;;  # not tracked -> silently skip (e.g. optional NOTICE)
  esac
  # exact-line membership (avoid substring false positive)
  printf '%s\n' "$ALL_TRACKED" | grep -qxF "$f" || return 0
  # never let injection smuggle a blocked-class path
  if third_party_excluded "$f" || f9_blocked "$f"; then return 0; fi
  META_LIST="${META_LIST}${f}"$'\n'
}
while IFS= read -r mf; do
  [ -z "$mf" ] && continue
  inject_meta "$mf"
done <<EOF
$REPO_META_FILES
EOF
while IFS= read -r img; do
  [ -z "$img" ] && continue
  inject_meta "$img"
done <<EOF
$(repo_meta_readme_images)
EOF
# FIX 3: inject install/mint-essential shared files (rules/ + hoisted database/
# code mint.sh reads by name). Same inject_meta belt (tracked + F9/third-party).
# Injecting rules/RULES.md BEFORE the symlink-escape gate means the template's
# `agents/.template/RULES.md -> ../../rules/RULES.md` link now resolves into the
# export set and stops being dropped as an escaping symlink.
while IFS= read -r sf; do
  [ -z "$sf" ] && continue
  inject_meta "$sf"
done <<EOF
$INSTALL_SHARED_FILES
EOF

# ---------------------------------------------------------------------------
# Symlink-safety gate (grill DGN-604 M3, ATTACK 1c).
# cp -P never FOLLOWS a symlink, so a symlink can never copy target CONTENT
# into the export tree -- no content leak is possible. The residual risk is an
# exported symlink whose target is NOT itself exported: it would ship as a
# dangling/escaping link (broken in the public repo, and a pointer at a private
# path). We compute the final export path set and flag every exported symlink
# whose resolved target lands OUTSIDE that set. Flagged links are dropped from
# the export (defense-in-depth: never ship a link that escapes the curation).
# ---------------------------------------------------------------------------
EXPORT_SET_PATHS="$( { printf '%s' "$EXPORT_LIST" | awk -F'\t' 'NF{print $2}'; printf '%s' "$META_LIST"; } | sort -u )"
SYMLINK_ESCAPE_LIST=""   # lines: "<unit_id>\t<path>\t<target>"

# normalize "dir/a/../b" style relative target against the link's dir, output
# a repo-relative path (no leading ./). Pure string logic (no realpath, target
# need not exist).
norm_rel() {  # $1 = link repo-rel path  $2 = raw symlink target
  local base="$(dirname "$1")" tgt="$2" combined out=() comp
  case "$tgt" in
    /*) printf '%s' "ABSOLUTE"; return ;;  # absolute target -> always escape
  esac
  combined="$base/$tgt"
  local IFS=/
  for comp in $combined; do
    case "$comp" in
      ''|.) : ;;
      ..) [ ${#out[@]} -gt 0 ] && unset 'out[${#out[@]}-1]' || out+=("..") ;;
      *) out+=("$comp") ;;
    esac
  done
  printf '%s' "${out[*]}"
}

if [ -n "$EXPORT_LIST" ]; then
  NEW_EXPORT_LIST=""
  while IFS=$'\t' read -r oid f; do
    [ -z "$f" ] && continue
    if [ -L "$CANON/$f" ]; then
      raw="$(readlink "$CANON/$f")"
      resolved="$(norm_rel "$f" "$raw")"
      # target in export set? (exact match, or a prefix dir of an exported path)
      if [ "$resolved" = "ABSOLUTE" ] \
         || ! printf '%s\n' "$EXPORT_SET_PATHS" | grep -qE "^${resolved}(/|$)"; then
        SYMLINK_ESCAPE_LIST="${SYMLINK_ESCAPE_LIST}${oid}	${f}	${raw}"$'\n'
        continue   # drop escaping symlink from export
      fi
    fi
    NEW_EXPORT_LIST="${NEW_EXPORT_LIST}${oid}	${f}"$'\n'
  done <<EOF
$EXPORT_LIST
EOF
  EXPORT_LIST="$NEW_EXPORT_LIST"
fi

EXPORT_COUNT=0;  [ -n "$EXPORT_LIST" ]      && EXPORT_COUNT="$(printf '%s' "$EXPORT_LIST" | grep -c .)"
F9_COUNT=0;      [ -n "$F9_BLOCKED_LIST" ]  && F9_COUNT="$(printf '%s' "$F9_BLOCKED_LIST" | grep -c .)"
PRIV_COUNT=0;    [ -n "$PRIV_BLOCKED_LIST" ] && PRIV_COUNT="$(printf '%s' "$PRIV_BLOCKED_LIST" | grep -c .)"
TP_COUNT=0;      [ -n "$TP_BLOCKED_LIST" ]  && TP_COUNT="$(printf '%s' "$TP_BLOCKED_LIST" | grep -c .)"
META_COUNT=0;    [ -n "$META_LIST" ]        && META_COUNT="$(printf '%s' "$META_LIST" | grep -c .)"
SYMESC_COUNT=0;  [ -n "$SYMLINK_ESCAPE_LIST" ] && SYMESC_COUNT="$(printf '%s' "$SYMLINK_ESCAPE_LIST" | grep -c .)"
NOOWNER_COUNT=0; [ -n "$NOOWNER_LIST" ]     && NOOWNER_COUNT="$(printf '%s' "$NOOWNER_LIST" | grep -c .)"

pass "export=$EXPORT_COUNT  repo-meta=$META_COUNT  f9-blocked=$F9_COUNT  priv-blocked=$PRIV_COUNT  third-party-blocked=$TP_COUNT  symlink-escape=$SYMESC_COUNT  no-owner=$NOOWNER_COUNT"

# Materialize the staged export tree (copy kept files preserving structure).
# Both the unit-owned export list AND the injected repo-meta files ship.
stage_file() {  # $1 = repo-relative path
  local f="$1" dest="$STAGE/$1"
  mkdir -p "$(dirname "$dest")"
  # -P: preserve symlinks (do not follow); some tracked .claude/skills entries
  # are symlinks. -p: preserve mode/timestamps.
  cp -Pp "$CANON/$f" "$dest"
}
if [ "$EXPORT_COUNT" -gt 0 ]; then
  while IFS=$'\t' read -r oid f; do
    [ -z "$f" ] && continue
    stage_file "$f"
  done <<EOF
$EXPORT_LIST
EOF
fi
if [ "$META_COUNT" -gt 0 ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    stage_file "$f"
  done <<EOF
$META_LIST
EOF
fi
info "staged export tree at: $STAGE"

# =============================================================================
# gate (3): secret sweep on the EXPORT ARTIFACTS (spec s5(a)).
# =============================================================================
# The sweep target is the curated export tree, not the canonical repo. We
# run secret-sweep in a throwaway git repo seeded from the staged tree so the
# existing tracked-file scanner applies to exactly what would ship.
gate "3: secret-sweep on export artifacts"
if [ "$EXPORT_COUNT" -eq 0 ]; then
  die "export tree is EMPTY -- allowlist produced nothing to publish (misconfigured owns?)"
fi
SWEEP_REPO="$STAGE"
git -C "$SWEEP_REPO" init -q 2>/dev/null || die "could not init sweep repo at $SWEEP_REPO"
# Carry the canonical .sweepignore into the SWEEP repo root only (never into
# the public export). Paths in .sweepignore are canonical-root-relative and the
# export tree preserves the same relative layout, so allowlisted synthetic
# fixtures (fake tokens in test harnesses) stay recognized. The .sweepignore
# file is itself no-owner and is NOT part of what ships.
if [ -f "$CANON/.sweepignore" ]; then
  cp -p "$CANON/.sweepignore" "$SWEEP_REPO/.sweepignore"
fi
# -f: the export tree carries the exported .gitignore files (root +
# agents/.template), and a gate-approved file matching an ignore rule (e.g.
# agents/.template/.telegram_bot/.env.example under the template's
# `.telegram_bot/` ignore) would otherwise be silently SKIPPED here -- escaping
# the secret sweep AND dropping out of the --execute snapshot commit (which
# ships exactly this index). Force-add so index == gated export list, always.
# DGN-808: the private owner-backup exemption marker must NEVER ship in a
# public export, and must never weaken this gate (a marker anywhere in the
# sweep-repo tree would exempt owner-PII cats on the throwaway scan). Search
# the entire subtree -- a marker in a subdirectory (e.g.
# agents/.template/.dogany-private-backup) is just as dangerous as one at root.
if find "$SWEEP_REPO" -name '.dogany-private-backup' -print -quit 2>/dev/null | grep -q .; then
  die "export tree contains .dogany-private-backup (at root or subdirectory) -- the private-backup marker must never publish (DGN-808)"
fi
git -C "$SWEEP_REPO" add -A -f 2>/dev/null
if ! bash "$SWEEP" "$SWEEP_REPO"; then
  die "secret-sweep FAILED on EXPORT ARTIFACTS -- personal data / secrets in curated tree, DO NOT publish"
fi
pass "secret-sweep clean on export artifacts"

# =============================================================================
# gate (4): VERSION gate -- plain vX.Y.Z framework train (spec MF-3 / s5(d)).
# =============================================================================
gate "4: VERSION gate (strict bump vs last PUBLISHED snapshot; plain vX.Y.Z)"
[ -f "$CANON/VERSION" ] || die "no VERSION file in $CANON"
VER="$(tr -d '[:space:]' < "$CANON/VERSION")"
echo "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
  || die "VERSION content '$VER' is not X.Y.Z"
# Idempotency reference is the LAST PUBLISHED version on the public remote, NOT
# the canonical v* tag (DGN-651). publish.sh runs AFTER release.sh has tagged
# v$VER, so a matching canonical tag is EXPECTED and must not be conflated with
# "already published" (the old gate did, blocking every release->publish run).
# The public snapshot subject carries the version: "Dogany vX.Y.Z -- curated
# public snapshot". Compare VERSION against that.
PUB_LAST=""
if git -C "$CANON" fetch "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" --quiet 2>/dev/null; then
  PUB_SUBJ="$(git -C "$CANON" log -1 --format=%s "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null)"
  PUB_LAST="$(printf '%s\n' "$PUB_SUBJ" | sed -nE 's/^Dogany v([0-9]+\.[0-9]+\.[0-9]+) .*/\1/p')"
  if [ -z "$PUB_LAST" ]; then
    pass "public $PUBLIC_BRANCH has no versioned snapshot subject -- treating v$VER as first publish"
  elif [ "$VER" = "$PUB_LAST" ]; then
    gate_fail "v$VER already published to $PUBLIC_REMOTE/$PUBLIC_BRANCH -- bump VERSION for a new publish"
  else
    HIGHER="$(printf '%s\n%s\n' "$PUB_LAST" "$VER" | sort -V | tail -1)"
    if [ "$HIGHER" != "$VER" ]; then
      gate_fail "VERSION $VER is older than last published v$PUB_LAST -- regression"
    else
      pass "VERSION $VER > last published v$PUB_LAST"
    fi
  fi
elif git -C "$CANON" ls-remote --exit-code "$PUBLIC_REMOTE" >/dev/null 2>&1; then
  # remote reachable but branch absent -> genuine first publish
  pass "public $PUBLIC_BRANCH not present yet -- v$VER would be the first publish"
else
  gate_fail "cannot reach public remote '$PUBLIC_REMOTE' to verify last published version"
fi

# =============================================================================
# gate C1: DGN-731 cadence gate -- VERSION gap alarm (FP2) + brick prevention
# (FP3).  Runs here because PUB_LAST (public version from gate 4) is now set.
# cadence_check is a no-op when PUB_LAST is empty (first publish).
# =============================================================================
cadence_check "$CANON" "$VER" "$PUB_LAST" "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" "$MODE"

# =============================================================================
# gate (5): CHANGELOG has an entry for THIS version.
# =============================================================================
gate "5: CHANGELOG entry for $VER"
if [ ! -f "$CANON/CHANGELOG.md" ]; then
  gate_fail "no CHANGELOG.md in $CANON -- write the user-facing entry first"
elif ! grep -E '^##([^0-9]|$)' "$CANON/CHANGELOG.md" | grep -qF "$VER"; then
  gate_fail "CHANGELOG.md has no heading entry containing '$VER' -- write it (user-facing, English) first"
else
  pass "CHANGELOG.md contains a heading for $VER"
fi

# =============================================================================
# gates (1)(2): clean canonical tree + on main up to date.
# In dry-run these are reported as read-only observations (a dirty worktree in
# development is normal); in --execute they are HARD gates.
# =============================================================================
CANON_DIRTY="$(git -C "$CANON" status --porcelain)"
CANON_BRANCH="$(git -C "$CANON" rev-parse --abbrev-ref HEAD)"

# =============================================================================
# DRY-RUN report + stop.
# =============================================================================
emit_report() {
  local out="$1"
  {
    echo "# DGN-604 M3 publish.sh -- DRY-RUN report"
    echo ""
    echo "- canonical: \`$CANON\`  (branch: $CANON_BRANCH)"
    echo "- public target: remote \`$PUBLIC_REMOTE\` branch \`$PUBLIC_BRANCH\`"
    echo "- version: v$VER (plain framework train, no prefix tags)"
    echo "- units: $UNIT_TOTAL total, $SELECTED_COUNT public+official selected"
    echo "- files: export=$EXPORT_COUNT, repo-meta=$META_COUNT, f9-blocked=$F9_COUNT, priv-blocked=$PRIV_COUNT, third-party-blocked=$TP_COUNT, symlink-escape=$SYMESC_COUNT, no-owner=$NOOWNER_COUNT"
    echo ""
    echo "## Units EXCLUDED from export (not public+official)"
    if [ -n "$EXCLUDED_UNITS" ]; then
      printf '%s' "$EXCLUDED_UNITS" | while IFS=$'\t' read -r uid vs uf; do
        [ -z "$uid" ] && continue
        echo "- \`$uid\` ($vs) -- manifest: $uf"
      done
    else
      echo "- (none)"
    fi
    echo ""
    echo "## Export list -- paths that WOULD go public, per unit"
    if [ -n "$EXPORT_LIST" ]; then
      printf '%s' "$EXPORT_LIST" | sort | awk -F'\t' '
        { if ($1!=cur) { cur=$1; print "\n### "cur } print "- "$2 }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## REPO-META injected -- public-repo + install essentials (no unit owns; FIX 1/3)"
    echo ""
    echo "Declarative, per-file injection: FIX 1 repo-meta (LICENSE/NOTICE/"
    echo "READMEs/CHANGELOG/.gitignore + only the docs/img images the READMEs"
    echo "reference) PLUS FIX 3 install-shared (rules/RULES.md + rules/"
    echo "USER.example.md + the hoisted database/ code mint.sh reads by name, so"
    echo "clone->install->mint actually scaffolds a working instance). Not a glob"
    echo "over docs/ rules/ database/ or the repo root -- an enumerated allowlist"
    echo "so it cannot become a default-deny backdoor."
    if [ -n "$META_LIST" ]; then
      printf '%s' "$META_LIST" | sort | awk '{ print "- "$0 }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## F9-BLOCKED -- OWNED by a public+official unit but blocked (domain-realdata / PoC-fixture class)"
    if [ -n "$F9_BLOCKED_LIST" ]; then
      printf '%s' "$F9_BLOCKED_LIST" | sort | awk -F'\t' '{ print "- "$2"   (owner: "$1")" }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## PRIVATE-BLOCKED -- claimed by a public parent glob but owned by a private/PoC unit"
    echo ""
    echo "Nested private/PoC subtrees that a broad public parent glob would"
    echo "otherwise export (spec 'PoC/private 제외'; M1 spending-log lock)."
    if [ -n "$PRIV_BLOCKED_LIST" ]; then
      printf '%s' "$PRIV_BLOCKED_LIST" | sort | awk -F'\t' '{ print "- "$2"   (private owner: "$1")" }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## THIRD-PARTY-BLOCKED -- third-party IP claimed by a public parent glob (FIX 2)"
    echo ""
    echo "agent-browser (Vercel Labs) rides agent-template's broad glob but is"
    echo "M1 inventory-excluded (not our IP, uncataloged). Declarative exclusion"
    echo "class; our own DGN-609 browser-auth wrapper is NOT here (it is ours)."
    if [ -n "$TP_BLOCKED_LIST" ]; then
      printf '%s' "$TP_BLOCKED_LIST" | sort | awk -F'\t' '{ print "- "$2"   (would-be owner: "$1")" }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## SYMLINK-ESCAPE -- exported symlinks whose target leaves the export set (dropped)"
    echo ""
    echo "cp -P never follows a link (no content leak possible); these links are"
    echo "dropped so no dangling/escaping symlink ships to the public repo."
    if [ -n "$SYMLINK_ESCAPE_LIST" ]; then
      printf '%s' "$SYMLINK_ESCAPE_LIST" | sort | awk -F'\t' '{ print "- "$2" -> "$3"   (owner: "$1")" }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## No-owner -- tracked but owned by no public+official unit (default-deny drop)"
    echo ""
    echo "These are shared/framework-internal paths with no owning unit glob"
    echo "(rules/, database/, docs/, repo-meta, PoC-private unit subtrees, etc.)."
    if [ -n "$NOOWNER_LIST" ]; then
      # Collapse to top-level dirs for readability, plus full count.
      printf '%s' "$NOOWNER_LIST" | sed 's#/.*##' | sort | uniq -c \
        | awk '{ printf "- %s/  (%d path(s))\n", $2, $1 }'
    else
      echo "- (none)"
    fi
    echo ""
    echo "## Currently PUBLIC but DROPPED by this curation"
    echo ""
    echo "Paths present on remote \`$PUBLIC_REMOTE/$PUBLIC_BRANCH\` today that this"
    echo "allowlist curation would NOT re-export (informational; requires a"
    echo "reachable public remote to compute)."
    local pub_tree
    pub_tree="$(git -C "$CANON" ls-tree -r --name-only "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null)"
    if [ -z "$pub_tree" ]; then
      echo "- (public remote not reachable / not fetched -- run 'git fetch $PUBLIC_REMOTE' to populate)"
    else
      local exported_paths
      exported_paths="$( { printf '%s' "$EXPORT_LIST" | awk -F'\t' '{print $2}'; printf '%s' "$META_LIST"; } | sort -u )"
      printf '%s\n' "$pub_tree" | sort -u | comm -23 - <(printf '%s\n' "$exported_paths") \
        | sed 's#/.*##' | sort | uniq -c \
        | awk '{ printf "- %s/  (%d path(s) currently public, now dropped)\n", $2, $1 }'
    fi
  } > "$out"
}

if [ "$MODE" = "dry-run" ]; then
  gate "1/2 (dry-run, read-only observation)"
  [ -n "$CANON_DIRTY" ] && info "canonical working tree DIRTY (normal in dev; blocks --execute)" \
                        || info "canonical working tree clean"
  [ "$CANON_BRANCH" = "main" ] && info "on main" || info "on branch '$CANON_BRANCH' (not main; blocks --execute)"

  echo ""
  echo "== DRY-RUN summary =="
  echo "   units total            : $UNIT_TOTAL"
  echo "   public+official        : $SELECTED_COUNT"
  echo "   export paths           : $EXPORT_COUNT"
  echo "   repo-meta injected     : $META_COUNT"
  echo "   F9-blocked paths       : $F9_COUNT"
  echo "   private-blocked paths  : $PRIV_COUNT"
  echo "   third-party-blocked    : $TP_COUNT"
  echo "   symlink-escape dropped : $SYMESC_COUNT"
  echo "   no-owner drops         : $NOOWNER_COUNT"
  echo "   staged export tree     : $STAGE"

  if [ -n "$REPORT_FILE" ]; then
    emit_report "$REPORT_FILE"
    echo "   report written        : $REPORT_FILE"
  fi

  echo ""
  echo "== dry-run: read-only gates PASSED. Would now (on --execute):"
  echo "   (1) require clean canonical working tree"
  echo "   (2) require canonical on main == origin/main"
  echo "   (7) snapshot the staged export tree as ONE curated commit and push"
  echo "       to $PUBLIC_REMOTE/$PUBLIC_BRANCH (NEVER full-history)"
  echo "  (7a) create tag v$VER on the snapshot commit and push to $PUBLIC_REMOTE"
  echo "       (so public tags track published content, plain vX.Y.Z, MF-3)"
  echo "   (8) assert public HEAD == pushed snapshot"
  echo "   nothing was created or pushed."
  exit 0
fi

# =============================================================================
# EXECUTE path (NOT exercised in M3). Guarded hard gates + snapshot push.
# =============================================================================
gate "1: clean canonical working tree"
[ -z "$CANON_DIRTY" ] || { echo "$CANON_DIRTY" | sed 's/^/     /'; die "canonical working tree NOT clean"; }
pass "canonical working tree clean"

gate "2: canonical on main, up to date with origin/main"
[ "$CANON_BRANCH" = "main" ] || die "canonical branch is '$CANON_BRANCH', not main"
git -C "$CANON" fetch origin main --quiet || die "cannot fetch origin (network/remote problem)"
L="$(git -C "$CANON" rev-parse HEAD)"; R="$(git -C "$CANON" rev-parse origin/main)"
[ "$L" = "$R" ] || die "local main ($L) != origin/main ($R) -- push/pull first"
pass "canonical main @ ${L:0:12} == origin/main"

gate "7: publish curated snapshot to $PUBLIC_REMOTE/$PUBLIC_BRANCH (single commit)"
# Snapshot model (spec MF-2 / s7 / DGN-604 M3 Option 2 "history reset"): the
# public repo receives ONE clean ORPHAN commit whose tree is exactly the staged
# export tree. The public branch is FORCE-updated to that single commit, so the
# public repo's prior history is fully replaced by this snapshot (Option 2). The
# canonical history is NEVER pushed (that would leak private/PoC commits).
#
# Implementation: the staged tree ($STAGE) already had a throwaway git repo
# initialized in gate (3) for the secret sweep (git init + add -A). We reuse
# that same repo to build the orphan commit deterministically, wire the public
# remote by URL (resolved from the canonical repo's 'public' remote), and
# force-push. Author/committer identity + dates are pinned so the resulting
# snapshot commit is reproducible.
[ "$EXPORT_COUNT" -gt 0 ] || die "export tree empty at publish time -- refuse to push"
[ -d "$STAGE/.git" ] || die "staged sweep repo missing (.git) -- gate (3) did not run?"

# Resolve the public remote's fetch URL from the CANONICAL repo. We push to that
# URL from the isolated $STAGE repo (which has its own history = the orphan
# snapshot only), never from the canonical repo (whose history must not leak).
PUBLIC_URL="$(git -C "$CANON" remote get-url "$PUBLIC_REMOTE" 2>/dev/null)" \
  || die "canonical repo has no remote '$PUBLIC_REMOTE' -- cannot resolve public URL"
[ -n "$PUBLIC_URL" ] || die "public remote '$PUBLIC_REMOTE' resolved to empty URL"
info "public remote URL: $PUBLIC_URL"

# Deterministic snapshot identity (reproducible orphan commit).
SNAP_NAME="Dogany Publish Bot"
SNAP_EMAIL="publish@dogany.local"
SNAP_MSG="Dogany v$VER -- curated public snapshot"
SNAP_DATE="$(git -C "$CANON" show -s --format=%cI HEAD 2>/dev/null || date +%Y-%m-%dT%H:%M:%S%z)"

# Build the orphan commit inside the staged repo. The staged repo currently has
# one working tree (the export) already `git add -A`'d in gate (3) but not
# committed. Create an orphan branch so the commit has NO parent, then commit
# the full staged tree. The .sweepignore + .git carried for the sweep are NOT
# part of the export set: strip .sweepignore from the index before committing so
# it never ships. (.git is inherently excluded from the tree.)
if [ -f "$STAGE/.sweepignore" ]; then
  git -C "$STAGE" rm -q --cached .sweepignore >/dev/null 2>&1 || true
  rm -f "$STAGE/.sweepignore"
fi
git -C "$STAGE" checkout -q --orphan publish-snapshot 2>/dev/null \
  || die "could not create orphan branch in staged repo"
# -f: same ignore-skip belt as gate (3) -- the snapshot commit must carry the
# FULL gated export list even where an exported .gitignore matches a path.
git -C "$STAGE" add -A -f || die "could not stage export tree for snapshot commit"
GIT_AUTHOR_NAME="$SNAP_NAME"   GIT_AUTHOR_EMAIL="$SNAP_EMAIL"   GIT_AUTHOR_DATE="$SNAP_DATE" \
GIT_COMMITTER_NAME="$SNAP_NAME" GIT_COMMITTER_EMAIL="$SNAP_EMAIL" GIT_COMMITTER_DATE="$SNAP_DATE" \
  git -C "$STAGE" commit -q -m "$SNAP_MSG" \
  || die "could not create snapshot commit"

SNAP_SHA="$(git -C "$STAGE" rev-parse HEAD)"
SNAP_TREE="$(git -C "$STAGE" rev-parse 'HEAD^{tree}')"
SNAP_PARENTS="$(git -C "$STAGE" rev-list --parents -n1 HEAD | awk '{print NF-1}')"
[ "$SNAP_PARENTS" -eq 0 ] || die "snapshot commit has $SNAP_PARENTS parent(s) -- must be an orphan (0)"
info "orphan snapshot commit $SNAP_SHA (tree $SNAP_TREE, parents 0)"

# Force-push the single orphan commit to the public branch (Option 2 history
# reset). This is the one irreversible external outbound; it runs ONLY in
# --execute (dry-run exited long before this point).
git -C "$STAGE" push --force "$PUBLIC_URL" "HEAD:refs/heads/$PUBLIC_BRANCH" \
  || die "force-push to $PUBLIC_REMOTE/$PUBLIC_BRANCH FAILED"
pass "pushed orphan snapshot $SNAP_SHA -> $PUBLIC_REMOTE/$PUBLIC_BRANCH (history reset)"

# --- (7a) tag the snapshot on the public remote ------------------------------
# DGN-744: public repo tags were drifting from content (last tag v1.16.0 while
# content = v1.26.x). Each successful publish now creates a plain vX.Y.Z tag
# on the snapshot commit and pushes it to the public remote. Tag name matches
# the VERSION file exactly (MF-3: no prefix variants, no rc/dev tags here).
# This runs in the $STAGE repo (the orphan repo used for the snapshot commit),
# not in the canonical repo -- so only the public remote receives the tag, and
# the canonical v* tag created by release.sh is never conflated with this.
gate "7a: tag snapshot v$VER on $PUBLIC_REMOTE"
SNAP_TAG="v$VER"
# Tag the snapshot commit in the staged repo (lightweight tag pointing at HEAD,
# which is the orphan snapshot commit we just pushed).
git -C "$STAGE" tag "$SNAP_TAG" HEAD \
  || die "could not create tag $SNAP_TAG on snapshot commit $SNAP_SHA"
# Push the tag to the public remote by URL (same URL used for the branch push).
git -C "$STAGE" push "$PUBLIC_URL" "refs/tags/$SNAP_TAG" \
  || die "tag push $SNAP_TAG to $PUBLIC_REMOTE FAILED"
pass "tagged snapshot $SNAP_SHA as $SNAP_TAG and pushed to $PUBLIC_REMOTE"

# --- (8) post-push assert ----------------------------------------------------
gate "8: post-push assert (public tree == staged snapshot tree)"
# Re-fetch the public branch we just wrote and confirm its tree sha equals the
# staged snapshot tree sha (content match), and that its history is exactly one
# commit with no parent (Option 2 single-snapshot invariant).
git -C "$STAGE" fetch -q "$PUBLIC_URL" "$PUBLIC_BRANCH" \
  || die "could not re-fetch $PUBLIC_BRANCH from public for post-push assert"
PUB_SHA="$(git -C "$STAGE" rev-parse FETCH_HEAD)"
PUB_TREE="$(git -C "$STAGE" rev-parse 'FETCH_HEAD^{tree}')"
PUB_PARENTS="$(git -C "$STAGE" rev-list --parents -n1 FETCH_HEAD | awk '{print NF-1}')"
[ "$PUB_TREE" = "$SNAP_TREE" ] \
  || die "post-push tree mismatch: public $PUB_TREE != staged $SNAP_TREE"
[ "$PUB_PARENTS" -eq 0 ] \
  || die "post-push history not a single orphan commit (public has $PUB_PARENTS parent(s))"
pass "public/$PUBLIC_BRANCH @ ${PUB_SHA:0:12} tree $PUB_TREE == staged snapshot (1 commit, 0 parents)"

# --- (9) exit checklist (manual) ---------------------------------------------
echo ""
echo "=== PUBLISHED v$VER -- operator exit checklist (manual) ==="
echo "  [ ] product/releases/v$VER.md: operator notes"
echo "  [ ] worklog tickets: stamp 'published: v$VER'"
echo "  [ ] tag the canonical release (Metal, main session, user approval)"
exit 0
