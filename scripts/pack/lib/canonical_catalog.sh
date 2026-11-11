# canonical_catalog.sh -- one runtime catalog default, including legacy fallback.
# Explicit overrides win; snapshot lookup is local and never fetches.

_pack_catalog_conf_get() { # <instance.conf> <key>; empty is not fatal
  { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; } || true
}

_CATALOG_INDEX_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../index" && pwd)/index_lib.sh"
# shellcheck source=../../index/index_lib.sh
. "$_CATALOG_INDEX_LIB"

pack_default_catalog_for_instance() {
  # <machinery-repo> <instance-root> <caller-label>
  local machinery_repo="$1" instance_root="${2:-}" caller="${3:-pack}"
  local canonical catalog snapshot
  if [[ -n "${DOGANY_PACK_CATALOG:-}" ]]; then
    printf '%s\n' "$DOGANY_PACK_CATALOG"
    return 0
  fi
  snapshot="$(index_path catalog "$instance_root")" || snapshot=""
  if [[ -n "$snapshot" && -f "$snapshot/catalog.json" ]]; then
    printf '%s\n' "$snapshot/catalog.json"
    return 0
  fi
  canonical=""
  [[ -z "$instance_root" ]] || canonical="$(_pack_catalog_conf_get "$instance_root/.instance.conf" DOGANY_REPO_ROOT)"
  catalog="${canonical:-$machinery_repo}/packs/catalog.json"
  if [[ -n "$canonical" && "$canonical" != "$machinery_repo" && ! -f "$catalog" ]]; then
    echo "ERROR: canonical catalog not found: $catalog -- refusing before runtime coordinate resolution (DGN-1513 F-1)" >&2
    return 1
  fi
  if [[ -n "$canonical" && "$canonical" != "$machinery_repo" ]]; then
    echo "[$caller] catalog defaulted to the canonical repo's: $catalog" >&2
  fi
  echo "[$caller] WARN: legacy catalog: $catalog (no cached catalog snapshot)" >&2
  printf '%s\n' "$catalog"
}

# Python readers use the same entry point; do not duplicate the fallback.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  pack_default_catalog_for_instance "$@"
fi
