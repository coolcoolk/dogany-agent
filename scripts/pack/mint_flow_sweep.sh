#!/bin/bash
# DGN-1683 I17: periodic backstop for detached mint workers.
# Best effort by design: a mint-flow failure must not turn an unrelated
# periodic job red. Each instance owns its own lock and owner delivery.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/discover_instances.sh"
while IFS= read -r row; do
  root="${row#*:}"
  [[ -d "$root/config" && -f "$root/config/mint-flow.json" ]] || continue
  "$SCRIPT_DIR/mint_flow.sh" --instance-root "$root" sweep >/dev/null 2>&1 || true
done < <(_discover_instances 2>/dev/null || true)
