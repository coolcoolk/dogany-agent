#!/bin/bash
# Clean-env launcher for the bridge.
#
# Usage:
#   start.sh --path <project_dir> [--_launchd_child]
#
# Loads a minimal, predictable environment then exec's `python -m bridge`.
# --_launchd_child is accepted (and consumed) so a launchd plist can pass it as a
# guard flag without it leaking into the python argv.

set -euo pipefail

PROJECT_PATH=""
LAUNCHD_CHILD=0
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)
      PROJECT_PATH="$2"
      shift 2
      ;;
    --_launchd_child)
      LAUNCHD_CHILD=1
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$PROJECT_PATH" ]]; then
  echo "Error: --path <project_dir> is required" >&2
  exit 1
fi

# Predictable PATH so the Claude CLI and ffmpeg resolve under launchd.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Resolve the python interpreter: prefer a venv next to this script, else env, else python3.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$SCRIPT_DIR/venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/venv/bin/python"
elif [[ -n "${BRIDGE_PYTHON:-}" ]]; then
  PYTHON="$BRIDGE_PYTHON"
else
  PYTHON="python3"
fi

# Package is importable from the parent of bridge/.
export PYTHONPATH="$(cd "$SCRIPT_DIR/.." && pwd):${PYTHONPATH:-}"

# Note: ${ARGS[@]+"${ARGS[@]}"} is the bash 3.2-safe expansion of a possibly-empty
# array under `set -u` (macOS ships bash 3.2 where a bare "${ARGS[@]}" errors).
exec "$PYTHON" -m bridge --path "$PROJECT_PATH" ${ARGS[@]+"${ARGS[@]}"}
