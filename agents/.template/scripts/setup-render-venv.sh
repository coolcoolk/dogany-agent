#!/bin/bash
# setup-render-venv.sh -- idempotent: ensure ~/dogany/.venvs/render exists with matplotlib.
#
# DGN-885: card render path (card.py, morning_brief_card.py, etc.) depends on
# ~/dogany/.venvs/render having matplotlib. This venv is the single canonical
# render interpreter; no other venv path is used by the card render surface.
#
# Canonical interpreter priority (all callers must follow this order):
#   1) RENDER_PYTHON env var (explicit override)
#   2) ~/dogany/.venvs/render/bin/python  <-- this script guarantees its existence
#   3) PATH python3 (only if matplotlib present, i.e. system already has it)
#   4) python3 (last resort; card.py exits code 3 gracefully if matplotlib absent)
#
# Safe to re-run: if the venv already exists and matplotlib is present, exits 0
# immediately with no changes.
#
# Usage:
#   scripts/setup-render-venv.sh          # uses python3 from PATH to create venv
#   VENV_PYTHON=/opt/homebrew/bin/python3 scripts/setup-render-venv.sh

set -euo pipefail

RENDER_VENV="$HOME/dogany/.venvs/render"
RENDER_PYTHON="${RENDER_VENV}/bin/python"

# --- Resolve the python to build the venv with ---
BUILD_PY="${VENV_PYTHON:-}"
if [[ -z "$BUILD_PY" ]]; then
  BUILD_PY="$(command -v python3 || true)"
fi
if [[ -z "$BUILD_PY" || ! -x "$BUILD_PY" ]]; then
  echo "setup-render-venv: no python3 found on PATH and VENV_PYTHON not set." >&2
  exit 1
fi

# --- Idempotent guard: skip if venv + matplotlib already present ---
if [[ -x "$RENDER_PYTHON" ]]; then
  if "$RENDER_PYTHON" -c "import matplotlib" 2>/dev/null; then
    echo "setup-render-venv: render venv OK (matplotlib present at $RENDER_VENV)."
    exit 0
  fi
  echo "setup-render-venv: venv exists but matplotlib missing -- installing." >&2
else
  echo "setup-render-venv: creating render venv at $RENDER_VENV using $BUILD_PY."
  mkdir -p "$(dirname "$RENDER_VENV")"
  "$BUILD_PY" -m venv "$RENDER_VENV"
fi

# --- Install matplotlib (and fonttools for CJK font extraction) ---
"$RENDER_VENV/bin/pip" install --quiet --upgrade pip
"$RENDER_VENV/bin/pip" install --quiet matplotlib fonttools

# --- Verify ---
if "$RENDER_PYTHON" -c "import matplotlib" 2>/dev/null; then
  mpl_ver="$("$RENDER_PYTHON" -c 'import matplotlib; print(matplotlib.__version__)')"
  echo "setup-render-venv: done. matplotlib $mpl_ver at $RENDER_VENV."
else
  echo "setup-render-venv: ERROR -- matplotlib install failed." >&2
  exit 1
fi
