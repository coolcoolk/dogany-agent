#!/usr/bin/env bash
# index.sh -- CLI for the shared index resolver (DGN-1883/DGN-1893 design v2).
# Usage: index.sh {refresh|path|status} <catalog|knowledge>
#                 [--instance <root> | --channel dev|stable] [--if-older <sec>]
# See scripts/index/index.py for exit codes and the cache layout.
set -u
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/index_lib.sh"
_index_py "$@"
