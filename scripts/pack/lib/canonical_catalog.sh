# canonical_catalog.sh -- resolve the runtime default pack catalog for an
# instance.  Sourced library; no side effects.
#
# A consume checkout is machinery, not the coordinate anchor for sibling pack
# payloads.  When its calling instance records another DOGANY_REPO_ROOT, the
# catalog in that canonical repo is the only default whose relative
# package_dir entries resolve beside the canonical checkout.  Explicit
# --catalog / DOGANY_PACK_CATALOG callers intentionally bypass this helper.

_pack_catalog_conf_get() { # <instance.conf> <key>; empty is not fatal
  { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; } || true
}

pack_default_catalog_for_instance() {
  # <machinery-repo> <instance-root> <caller-label>
  # stdout: catalog path only. stderr: diagnostic. rc 1: canonical catalog
  # was required but missing (fail closed before a relative coordinate can be
  # resolved against the wrong consume tree).
  local machinery_repo="$1" instance_root="${2:-}" caller="${3:-pack}" canonical catalog
  catalog="$machinery_repo/packs/catalog.json"
  [[ -n "$instance_root" ]] || { printf '%s\n' "$catalog"; return 0; }
  canonical="$(_pack_catalog_conf_get "$instance_root/.instance.conf" DOGANY_REPO_ROOT)"
  if [[ -n "$canonical" && "$canonical" != "$machinery_repo" ]]; then
    catalog="$canonical/packs/catalog.json"
    [[ -f "$catalog" ]] || {
      echo "ERROR: canonical catalog not found: $catalog (DOGANY_REPO_ROOT in $instance_root/.instance.conf names a repo without packs/catalog.json) -- refusing before runtime coordinate resolution (DGN-1513 F-1)" >&2
      return 1
    }
    echo "[$caller] catalog defaulted to the canonical repo's: $catalog (sibling package_dir entries resolve THERE, not in this machinery tree -- DGN-1513 F-1)" >&2
  fi
  printf '%s\n' "$catalog"
}
