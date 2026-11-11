# index_lib.sh -- bash entry points for the shared index resolver
# (DGN-1883/DGN-1893 design v2 sec 2). Sourced library; defining the functions
# has no side effect. All logic lives in index.py (stdlib python); these are
# thin callers so bash readers never re-implement the cache layout.
#
#   index_path <type> [<instance-root>]
#       Current snapshot dir on stdout, or nothing (rc 1). Never touches the
#       network -- safe on any reader path.
#   index_refresh <type> [<instance-root>] [--if-older <sec>]
#       Fetch + verify + swap. stdout = current snapshot dir when one exists.
#       rc 0 current valid, rc 1 this attempt failed (current untouched).

_INDEX_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_index_py() {
  if [[ -z "${PACK_PY3:-}" ]]; then
    # shellcheck source=/dev/null
    source "$_INDEX_LIB_DIR/../pack/lib/py_resolve.sh"
    pack_resolve_py3 >/dev/null 2>&1 || PACK_PY3=python3
  fi
  "$PACK_PY3" "$_INDEX_LIB_DIR/index.py" "$@"
}

index_path() {
  local type="$1" inst="${2:-}"
  if [[ -n "$inst" ]]; then _index_py path "$type" --instance "$inst"
  else _index_py path "$type"; fi
}

index_refresh() {
  local type="$1" inst=""
  shift
  if [[ $# -gt 0 && "$1" != --* ]]; then inst="$1"; shift; fi
  if [[ -n "$inst" ]]; then _index_py refresh "$type" --instance "$inst" "$@"
  else _index_py refresh "$type" "$@"; fi
}
