#!/bin/bash
# DGN-2291: shared-skill .prev-* bodies are permanent recovery evidence.
# Legacy source/API compatibility remains; selection never authorizes deletion.
prev_backup_select() { return 0; }
prev_backup_prune() {
  local root="$1" name="$2" path
  [[ -d "$root" ]] || return 0
  for path in "$root"/*.prev-*; do
    [[ -e "$path" || -L "$path" ]] || continue
    [[ "$name" == --all || "${path##*/}" == "$name.prev-"* ]] || continue
    printf 'retained: %s\n' "$path"
  done
  return 0
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  [[ "${1:-}" == --reconcile && -d "${2:-}" ]] || {
    echo 'usage: prev_backup_prune.sh --reconcile <shared_root> [--dry-run]' >&2
    exit 1
  }
  [[ -z "${3:-}" || "$3" == --dry-run ]] || { echo "unknown option: $3" >&2; exit 1; }
  prev_backup_prune "$2" --all
fi
