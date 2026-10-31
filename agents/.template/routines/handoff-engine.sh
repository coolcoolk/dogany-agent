#!/usr/bin/env bash
# handoff-engine.sh -- thin wrapper (DGN-1379-SPEC-consume-entrypoint S2).
#
# Sole job: resolve python3 at exec time and hand off to engine.py
# unchanged. NO other logic belongs here -- see the spec's S2 for why
# (the old handoff-consume.sh grew to 25 lines by breaking exactly this
# rule, and nobody noticed it was broken).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON3="$(command -v python3)"
exec "$PYTHON3" "$SCRIPT_DIR/lib/handoff/engine.py" "$@"
