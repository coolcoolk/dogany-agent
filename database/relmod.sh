#!/usr/bin/env bash
# relmod.sh -- thin wrapper for relmod.py
# Works from any cwd; resolves its own directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${LIFEKIT_PYTHON:-}"
if [[ -z "$PY" ]]; then
    PY="$(command -v python3 || true)"
fi
[[ -x "$PY" ]] || PY="/usr/bin/python3"

exec "$PY" "$SCRIPT_DIR/relmod.py" "$@"
