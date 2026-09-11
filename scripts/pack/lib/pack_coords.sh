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
  python3 - "$catalog" "$pack_id" <<'PYEOF'
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
  python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get(sys.argv[2]) or '')" "$row" "$key"
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
  catalog_dir="$(cd "$(dirname "$catalog")" && pwd)"
  printf '%s\n' "$catalog_dir/$value"
}
