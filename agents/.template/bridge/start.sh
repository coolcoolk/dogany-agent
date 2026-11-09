#!/bin/bash
# Clean-env launcher for the bridge.
#
# Usage:
#   start.sh --path <project_dir> [--_host_child <launchd|app>] [--_launchd_child]
#
# Loads a minimal, predictable environment then exec's `python -m bridge`.
# --_host_child <launchd|app> is accepted (and consumed) and exported to the bridge as
# BRIDGE_HOST_CHILD; the bridge records it as "host" in bridge.lock (v2.7 T6 P1,
# dec-319). Absent -> "manual". --_launchd_child is the legacy alias for
# `--_host_child launchd`. Neither leaks into the python argv.

set -euo pipefail

PROJECT_PATH=""
HOST_CHILD="manual"
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)
      PROJECT_PATH="$2"
      shift 2
      ;;
    --_launchd_child)
      HOST_CHILD="launchd"
      shift
      ;;
    --_host_child)
      if [[ $# -lt 2 ]]; then
        echo "Error: --_host_child requires a value (launchd|app)" >&2
        exit 1
      fi
      case "$2" in
        launchd|app) HOST_CHILD="$2" ;;
        *) echo "Error: --_host_child must be launchd or app (got '$2')" >&2; exit 1 ;;
      esac
      shift 2
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
# $HOME/.local/bin FIRST (DGN-963): the Claude CLI native installer lands
# there and self-updates; it must win over any stale root-owned npm-global
# or Homebrew shadow copy. Homebrew still precedes /usr/local/bin so other
# brewed tools resolve normally. The launchd plist (newbridge) carries the
# same order -- keep both PATH declarations symmetric.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Resolve the python interpreter: prefer a venv next to this script, else env, else python3.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$SCRIPT_DIR/venv/bin/python" ]]; then
  PYTHON="$SCRIPT_DIR/venv/bin/python"
elif [[ -n "${BRIDGE_PYTHON:-}" ]]; then
  PYTHON="$BRIDGE_PYTHON"
else
  PYTHON="python3"
fi

export BRIDGE_HOST_CHILD="$HOST_CHILD"

# Package is importable from the parent of bridge/.
export PYTHONPATH="$(cd "$SCRIPT_DIR/.." && pwd):${PYTHONPATH:-}"

# Note: ${ARGS[@]+"${ARGS[@]}"} is the bash 3.2-safe expansion of a possibly-empty
# array under `set -u` (macOS ships bash 3.2 where a bare "${ARGS[@]}" errors).
exec "$PYTHON" -m bridge --path "$PROJECT_PATH" ${ARGS[@]+"${ARGS[@]}"}
