# pack_coords.sh -- pack COORDINATE resolver (DGN-1079 RR1).
# Sourced library (no shebang, no side effects). Requires python3.
#
# THE RULE, stated once: a pack's location is `package_dir` on its
# packs/catalog.json row, and a RELATIVE package_dir resolves against the
# directory holding THAT CATALOG FILE (an absolute package_dir is taken
# verbatim). That is what lets `"package_dir": "../../dogany-lifekit"`
# mean "the sibling checkout next to this agent repo" (DGN-803 LS-5 e2,
# DGN-681 S4b #2) while `"package_dir": "dev"` means the in-tree pack.
#
# WHY THIS FILE EXISTS (DGN-1079 root diagnosis): the rule used to live only
# inside pack_install.sh, while pack_publish.sh CONSTRUCTED its own coordinate
# ("$PACKS_DIR/$PACK_ID") and then wrote that construction back onto the
# catalog row. Two rules for one fact gave three defects a place to stand:
#   P1 publish overwrote `package_dir`, so one publish reverted the
#      independent-repo coordinate and the next install hit PREFLIGHT FAIL;
#   P2 publish re-injected `pack_version`, a second source of truth the
#      catalog deliberately does not carry (catalog.json:14,44,79 notes;
#      pack_install.sh's manifest-fallback comment says the same);
#   P3 publish had no way to address an independent-repo pack at all.
# With one resolver read by BOTH sides and NEITHER side writing the field
# back, none of the three has a place to stand. Publisher and installer now
# read the same fact from the same source -- the catalog is the single
# truth for coordinates, the pack's own manifest for pack_version.
#
# SHAPE: sourced bash functions, not a data module. The precedent is
# scripts/pack/lib/persona_resolver.sh (DGN-773 T4b) -- same directory, same
# "resolver returns the real target so the defect has nowhere to stand" move.
# A data module (the RR2 shape used for compat-lint/pack_install verdict
# tokens) is deliberately NOT used here: RR2 keeps two INDEPENDENT verdict
# implementations on purpose (structural fail-open cover) and shares only the
# constants they compare against. Coordinate resolution has no such reason to
# stay doubled -- publisher and installer must agree by CONSTRUCTION, not by
# two implementations that happen to match, so the LOGIC is what is shared.
#
# NB: this directory is runtime (exported wholesale via the
# `scripts/pack/lib/**` glob in product.yaml; publish.sh gate E2b requires
# every file here to be statically referenced by a kept script --
# pack_install.sh's `. "$COORDS_LIB"` is that reference). pack_publish.sh is
# STRIP dev machinery and may depend on runtime lib, exactly as it already
# does for persona_resolver.sh.

# DGN-1424: shared python3 resolver (PATH-independent -- see the lib file
# header for why a bare `python3` call is unsafe on a human's own terminal).
_PY_RESOLVE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/py_resolve.sh"
# shellcheck source=py_resolve.sh
. "$_PY_RESOLVE_LIB"

# pack_catalog_row <catalog_file> <pack_id>
#   Prints the pack's catalog row as a single line of JSON, or the literal
#   `null` when the catalog has no row for that id (or has no catalog file).
#   A malformed catalog raises -- fail-closed, never a silent "not found".
pack_catalog_row() {
  local catalog="$1" pack_id="$2"
  if [[ ! -f "$catalog" ]]; then
    printf 'null\n'
    return 0
  fi
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$catalog" "$pack_id" <<'PYEOF'
import json, sys

catalog_path = sys.argv[1]
pack_id = sys.argv[2]

with open(catalog_path) as f:
    cat = json.load(f)

for p in cat.get("packs", []):
    if p["id"] == pack_id:
        print(json.dumps(p))
        sys.exit(0)

print("null")
sys.exit(0)
PYEOF
}

# pack_row_field <row_json> <key>
#   String field off a row produced by pack_catalog_row ('' when absent, when
#   the value is null/false, or when the row itself is empty/`null`).
pack_row_field() {
  local row="$1" key="$2"
  [[ -n "$row" && "$row" != "null" ]] || { echo ""; return 0; }
  pack_resolve_py3 || return 1
  "$PACK_PY3" -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get(sys.argv[2]) or '')" "$row" "$key"
}

# pack_coord_resolve <catalog_file> <package_dir_value>
#   Applies THE RULE above and prints the resolved package directory.
#   Empty package_dir -> empty output (the caller decides whether an absent
#   coordinate is fatal: pack_install.sh refuses, pack_publish.sh falls back
#   to the in-tree creation default for a pack that has no row yet).
#   The result is NOT canonicalized (no realpath): the directory legitimately
#   may not exist yet at publish time, and the un-normalized form is what the
#   installer has always logged.
pack_coord_resolve() {
  local catalog="$1" value="$2" catalog_dir
  [[ -n "$value" ]] || { echo ""; return 0; }
  if [[ "${value:0:1}" == "/" ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  # DOGANY_SIBLING_ROOT: explicit directory that holds the sibling checkouts,
  # for a lane whose worktree does not sit beside them (Codex Cloud returned
  # worktrees). Only `../../<name>` sibling coordinates are redirected.
  if [[ -n "${DOGANY_SIBLING_ROOT:-}" && "$value" == ../../* && "${value#../../}" != *..* ]]; then
    printf '%s\n' "${DOGANY_SIBLING_ROOT%/}/${value#../../}"
    return 0
  fi
  catalog_dir="$(cd "$(dirname "$catalog")" && pwd)"
  printf '%s\n' "$catalog_dir/$value"
}

# ---------------------------------------------------------------------------
# Public-install source (DGN-1818). A catalog row MAY carry `source_url`: the
# public git remote of a pack that lives in its own repo. The sibling
# checkout named by package_dir exists only on a developer machine; on a
# public install it is absent, and the pack is reached through ONE local
# clone per pack under the shared home instead. That clone is an ordinary
# git work tree checked out at a pack tag, so every downstream gate (D1,
# source-tag, NM3 sealed tag tree, the update registry) treats it exactly
# like a sibling checkout -- there is no second install lane.
# When the sibling checkout exists, nothing here runs (estate unchanged).
# ---------------------------------------------------------------------------

# pack_source_dir <pack_id> -- THE clone location (one rule, read by the
#   installer, update_seed.sh and update_plan.sh).
pack_source_dir() {
  printf '%s\n' "${DOGANY_SHARED_HOME:-$HOME/.dogany}/pack-src/$1"
}

# pack_source_is_clone <dir> -- rc 0 iff <dir> is a pack-src clone (the only
#   repos the update planner may fetch into; a sibling checkout is the
#   developer's and is never touched).
pack_source_is_clone() {
  local root
  root="${DOGANY_SHARED_HOME:-$HOME/.dogany}/pack-src/"
  [[ "$1" == "$root"* && -d "$1/.git" ]]
}

# pack_source_refresh <dir> -- non-force tag fetch from the clone's origin.
#   A tag that moved on the remote makes the fetch fail (rc 1): a published
#   pack tag is append-only, and silently re-pointing one would move what an
#   installed version means.
pack_source_refresh() {
  # No credential prompt: a bridge has no terminal, and a missing/private
  # remote must fail (rc 1), not hang waiting for a username.
  GIT_TERMINAL_PROMPT=0 git -C "$1" fetch -q --tags origin >&2
}

# pack_stable_tag_pick <pack_id> -- reads tag names on stdin (one per line)
#   and prints the newest STABLE pack/<id>/vX.Y.Z (empty when there is none).
#   The one selection rule shared by pack_source_fetch (tags of the clone) and
#   pack_source_remote_tag (tags of the remote before any clone exists).
#   DGN-2119: a dev-tail product (semver_range.py dev-tail <id>) is picked by
#   semver_range.py pick-tag stable instead -- its legacy stamps (v0.1.1 ...)
#   are dev builds, not releases, and while it is unreleased its newest
#   pack/<id>/v0.1.0-dev.N is the install candidate.
_PC_SEMVER="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/semver_range.py"
pack_stable_tag_pick() {
  if [[ -f "$_PC_SEMVER" ]] && python3 "$_PC_SEMVER" dev-tail "$1" 2>/dev/null; then
    python3 "$_PC_SEMVER" pick-tag "$1" stable 2>/dev/null || true
    return 0
  fi
  grep -E "^pack/$1/v[0-9]+\.[0-9]+\.[0-9]+$" | sort -V | tail -1 || true
}

# pack_source_remote_tag <pack_id> <source_url>
#   DGN-1844: the tag pack_source_fetch would check out when no tag is named,
#   read from the remote WITHOUT cloning (git ls-remote). Lets a reader that
#   runs before pack_install (the mint token step on a public install, where
#   the pack-src clone does not exist yet) pin the version the install will
#   fetch. Writes nothing. Prints the tag; rc 1 + reason on stderr.
pack_source_remote_tag() {
  local pack_id="$1" url="$2" refs tag
  refs="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs "$url" "refs/tags/pack/$pack_id/v*" 2>/dev/null)" \
    || { echo "pack_source_remote_tag: cannot list tags of $pack_id at $url" >&2; return 1; }
  tag="$(printf '%s\n' "$refs" | awk '{print $2}' | sed 's#^refs/tags/##' | pack_stable_tag_pick "$pack_id")"
  [[ -n "$tag" ]] || { echo "pack_source_remote_tag: no stable pack/$pack_id/vX.Y.Z tag at $url" >&2; return 1; }
  printf '%s\n' "$tag"
}

# pack_source_renamed_origin <old_url> <new_url>
#   dec-430 / DGN-2092: exact, directional GitHub rename pairs only. Do not
#   normalize hosts, owners or suffixes: an unrelated origin still refuses.
pack_source_renamed_origin() {
  case "$1|$2" in
    'https://github.com/coolcoolk/dogany-pack-health.git|https://github.com/coolcoolk/dogany-agentpack-health-trainer.git' \
    | 'https://github.com/coolcoolk/dogany-pack-health-canonical.git|https://github.com/coolcoolk/dogany-agentpack-health-trainer-canonical.git' \
    | 'https://github.com/coolcoolk/dogany-agentpack-ledger.git|https://github.com/coolcoolk/dogany-mod-ledger-canonical.git' \
    | 'https://github.com/coolcoolk/dogany-agentpack-po.git|https://github.com/coolcoolk/dogany-agentpack-po-canonical.git' \
    | 'https://github.com/coolcoolk/dogany-kit-dev.git|https://github.com/coolcoolk/dogany-kit-dev-canonical.git') return 0 ;;
    *) return 1 ;;
  esac
}

# pack_source_fetch <pack_id> <source_url> [<tag>]
#   Clone (first time) or tag-fetch (later) the pack's public remote into
#   pack_source_dir, then detach the work tree at <tag>, or at the newest
#   STABLE pack/<id>/vX.Y.Z when no tag is named (never a branch tip, never
#   a pre-release). An existing clone whose origin is not <source_url> is
#   refused unless it is an exact approved rename above (migrate, then fetch).
#   Prints the clone dir; rc 1 + reason on stderr.
pack_source_fetch() {
  local pack_id="$1" url="$2" want="${3:-}" dir tag origin
  dir="$(pack_source_dir "$pack_id")"
  if [[ -d "$dir/.git" ]]; then
    origin="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
    if [[ "$origin" != "$url" ]]; then
      pack_source_renamed_origin "$origin" "$url" \
        || { echo "pack_source_fetch: $dir tracks '$origin', not the catalog source_url '$url' -- refusing to reuse it" >&2; return 1; }
      git -C "$dir" remote set-url origin "$url" \
        || { echo "pack_source_fetch: cannot migrate origin for $pack_id ($origin -> $url)" >&2; return 1; }
      echo "pack_source_fetch: migrated origin for $pack_id ($origin -> $url)" >&2
    fi
    pack_source_refresh "$dir" \
      || { echo "pack_source_fetch: tag fetch failed for $pack_id ($url) -- offline, or a published tag moved" >&2; return 1; }
  elif [[ -e "$dir" ]]; then
    echo "pack_source_fetch: $dir exists but is not a git clone -- refusing to overwrite it" >&2
    return 1
  else
    mkdir -p "$(dirname "$dir")" \
      && GIT_TERMINAL_PROMPT=0 git clone -q --no-checkout "$url" "$dir" >&2 \
      || { echo "pack_source_fetch: clone failed for $pack_id ($url)" >&2; return 1; }
  fi
  if [[ -n "$want" ]]; then
    tag="$want"
    git -C "$dir" rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null \
      || { echo "pack_source_fetch: tag $tag not found at $url" >&2; return 1; }
  else
    tag="$(git -C "$dir" tag -l "pack/$pack_id/v*" | pack_stable_tag_pick "$pack_id")"
    [[ -n "$tag" ]] || { echo "pack_source_fetch: no stable pack/$pack_id/vX.Y.Z tag at $url" >&2; return 1; }
  fi
  git -C "$dir" -c advice.detachedHead=false checkout -q --force "refs/tags/$tag" >&2 \
    && git -C "$dir" clean -q -fd >&2 \
    || { echo "pack_source_fetch: checkout $tag failed in $dir" >&2; return 1; }
  printf '%s\n' "$dir"
}

# ---------------------------------------------------------------------------
# ONE coordinate lookup (DGN-1883 design v2 sec 3.4, WP2). `package_dir` is a
# LOCAL-ONLY field from here on: it exists on the legacy framework catalog
# (and on fixture catalogs) but never on a row fetched from the catalog repo.
# This file is the only place that reads it; every other consumer asks
# pack_coord_for / pack_row_dir (or the CLI forms below), so the day the
# field disappears only this file changes. A canonical-side reader scan
# (DGN-1883 WP2) checks that no other file reads package_dir.
# ---------------------------------------------------------------------------

# pack_row_package_dir <catalog_file> <row_json>
#   The row's legacy package_dir resolved by THE RULE ('' when the row has
#   none). The DECLARED coordinate, existing or not -- for the publish side,
#   which addresses the private checkout it is about to write.
pack_row_package_dir() {
  local catalog="$1" row="$2"
  pack_coord_resolve "$catalog" "$(pack_row_field "$row" package_dir)"
}

# pack_sources_repo <pack_id> -- `<id>.repo=` from pack-sources.conf ('' when
#   the registry or the key is absent). The developer's sibling checkout, or
#   the pack-src clone on a public install (update_seed.sh writes both).
pack_sources_repo() {
  local conf="${DOGANY_PACK_SOURCES:-${DOGANY_SHARED_HOME:-$HOME/.dogany}/pack-sources.conf}"
  [[ -n "$1" && -f "$conf" ]] || { echo ""; return 0; }
  awk -v k="$1.repo=" 'index($0, k) == 1 { print substr($0, length(k) + 1); exit }' "$conf"
}

# pack_coord_for <pack_id> [<catalog_file> [<row_json>]]
#   The pack's source directory on THIS machine -- a directory that holds a
#   pack-manifest.json -- or empty. First hit wins:
#     1. legacy package_dir of the row, resolved against the catalog file
#        (only a legacy/fixture catalog row has one);
#     2. pack-sources.conf <id>.repo;
#     3. the pack-src clone (pack_source_dir) when it is a clone;
#     4. empty.
#   Never fetches, never clones (pack_install.sh is the one writer). With a
#   catalog but no row, the row is looked up; with neither, steps 2-3 only.
#   Order note: design v2 lists pack-sources first; a row that names its own
#   package_dir wins here so an explicit --catalog fixture is never
#   hijacked by the machine's registry (for fetched rows -- no package_dir --
#   the order is the design's).
pack_coord_for() {
  local id="$1" catalog="${2:-}" row="${3:-}" d
  if [[ -n "$catalog" && -z "$row" ]]; then
    row="$(pack_catalog_row "$catalog" "$id")" || return 1
  fi
  [[ "$row" != "null" ]] || row=""
  if [[ -n "$row" && -n "$catalog" ]]; then
    d="$(pack_row_package_dir "$catalog" "$row")" || return 1
    [[ -n "$d" && -f "$d/pack-manifest.json" ]] && { printf '%s\n' "$d"; return 0; }
  fi
  [[ -n "$id" ]] || { echo ""; return 0; }
  d="$(pack_sources_repo "$id")"
  [[ -n "$d" && -f "$d/pack-manifest.json" ]] && { printf '%s\n' "$d"; return 0; }
  d="$(pack_source_dir "$id")"
  if [[ -f "$d/pack-manifest.json" ]] && pack_source_is_clone "$d"; then
    printf '%s\n' "$d"
    return 0
  fi
  echo ""
}

# pack_row_dir <catalog_file> <row_json>
#   THE read-side directory of a pack for every consumer that only READS a
#   pack's files (handover brief, mint screens, skill-collision universe,
#   requires_kit / requires_modules gates, kit first-install probe):
#   pack_coord_for when it finds one; otherwise the would-be location, for
#   the caller's "unreachable" message -- the declared package_dir, else the
#   pack-sources .repo, else (row with source_url) the pack-src path. Empty
#   iff the row has none of package_dir, a pack-sources .repo, source_url.
pack_row_dir() {
  local catalog="$1" row="$2" id d
  id="$(pack_row_field "$row" id)" || return 1
  d="$(pack_coord_for "$id" "$catalog" "$row")" || return 1
  [[ -n "$d" ]] && { printf '%s\n' "$d"; return 0; }
  d="$(pack_row_package_dir "$catalog" "$row")" || return 1
  [[ -n "$d" ]] || d="$(pack_sources_repo "$id")"
  if [[ -z "$d" && -n "$id" && -n "$(pack_row_field "$row" source_url)" ]]; then
    d="$(pack_source_dir "$id")"
  fi
  printf '%s\n' "$d"
}

# pack_catalog_relocate <src_catalog> <out_catalog>
#   Copies a catalog with every RELATIVE package_dir made absolute against
#   the SOURCE catalog's directory (THE RULE), so the copy resolves the same
#   coordinates from anywhere (pack_publish.sh STEP 7 rehearsal). A missing
#   source writes an empty catalog.
pack_catalog_relocate() {
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$1" "$2" <<'PYEOF'
import json, os, sys
src, out = sys.argv[1], sys.argv[2]
if os.path.isfile(src):
    with open(src) as f:
        cat = json.load(f)
    base = os.path.dirname(os.path.abspath(src))
else:
    cat, base = {"version": 1, "packs": []}, None
for p in cat.setdefault("packs", []):
    pd = p.get("package_dir")
    if base and isinstance(pd, str) and pd and not os.path.isabs(pd):
        p["package_dir"] = os.path.normpath(os.path.join(base, pd))
with open(out, "w") as f:
    json.dump(cat, f, ensure_ascii=False, indent=2)
PYEOF
}

# pack_catalog_coords <catalog_file> -- one `<id>\t<package_dir>` line per
#   row, package_dir RAW (unresolved, '' when absent). For whole-catalog
#   audits that judge the declared shape (publish.sh E3 in-tree vs sibling).
pack_catalog_coords() {
  pack_resolve_py3 || return 1
  "$PACK_PY3" -c '
import json, sys
with open(sys.argv[1]) as f:
    cat = json.load(f)
for row in cat.get("packs", []):
    pd = row.get("package_dir")
    print("%s\t%s" % (row.get("id", "(no-id)"), pd if isinstance(pd, str) else ""))
' "$1"
}

# CLI form for non-bash consumers (mint_screens.py, the python body of
# skill_collision_check.sh): `bash pack_coords.sh row-dir <catalog> <id>`
# prints pack_row_dir for that catalog row (empty when the row is absent or
# has no coordinate at all). Sourcing this file never reaches this block.
if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "row-dir" ]]; then
  _pc_row="$(pack_catalog_row "$2" "$3")" || exit 1
  [[ "$_pc_row" != "null" ]] || { echo ""; exit 0; }
  pack_row_dir "$2" "$_pc_row"
  exit $?
fi
# `bash pack_coords.sh coord-for <id> [<catalog>]` (DGN-1883 WP2): prints
# pack_coord_for (a directory holding pack-manifest.json, or empty).
if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "coord-for" ]]; then
  pack_coord_for "$2" "${3:-}"
  exit $?
fi
# `bash pack_coords.sh catalog-coords <catalog>` / `relocate <src> <out>`:
# pack_catalog_coords / pack_catalog_relocate for non-bash callers.
if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "catalog-coords" ]]; then
  pack_catalog_coords "$2"
  exit $?
fi
if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "relocate" ]]; then
  pack_catalog_relocate "$2" "$3"
  exit $?
fi
# `bash pack_coords.sh remote-tag <catalog> <id>` (DGN-1844): the newest
# stable tag of the row's source_url remote (pack_source_remote_tag). rc 1
# when the row is absent, carries no source_url, or the remote has no tag.
if [[ "${BASH_SOURCE[0]}" == "$0" && "${1:-}" == "remote-tag" ]]; then
  _pc_row="$(pack_catalog_row "$2" "$3")" || exit 1
  [[ "$_pc_row" != "null" ]] || { echo "pack_coords: no catalog row for $3" >&2; exit 1; }
  _pc_url="$(pack_row_field "$_pc_row" source_url)" || exit 1
  [[ -n "$_pc_url" ]] || { echo "pack_coords: $3 has no source_url" >&2; exit 1; }
  pack_source_remote_tag "$3" "$_pc_url"
  exit $?
fi
