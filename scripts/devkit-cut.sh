#!/usr/bin/env bash
# Product-root entry for the extracted devkit cut adapter.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
exec bash "$ROOT/extracted-devkit/framework/scripts/devkit-cut.sh" "$@"
