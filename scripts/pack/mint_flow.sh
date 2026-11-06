#!/bin/bash
# DGN-1683 WP1: mint verb (open | next | status | cancel | table).
# One JSON object on stdout per call; never a token, never screen copy.
# Thin resolver wrapper around lib/mint_flow.py (see its header for the contract).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/py_resolve.sh"
pack_resolve_py3 || exit 1
exec "$PACK_PY3" "$SCRIPT_DIR/lib/mint_flow.py" "$@"
