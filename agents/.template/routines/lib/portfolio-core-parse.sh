#!/usr/bin/env bash
# portfolio-core-parse.sh -- generic parse-or-die entrypoint for a portfolio
# index (CORE profile). Framework asset; spec of record: docs/PORTFOLIO-CORE.md.
#
# Thin wrapper over portfolio-core-lint.py --parse-only: structural parse only
# (block marker registry, table shape, positional separator with content
# validation, no-silent-drop) -- NO header semantics, NO enum domains.
# CORE profile means the EDGES block is OPTIONAL (an instance-local hardened
# parser may be stricter, e.g. require EDGES exactly-once for a curated-full
# estate; such a parser stays instance-local under its own name and is
# preferred by consumers when present -- see dogany-upstream-report Layer B).
#
# Deliberately NOT named portfolio-parse.sh: the routines/ refresh in
# update.sh rsyncs without --delete, so a same-named framework file would
# overwrite an instance-local parser. Fresh name = no clobber by construction.
#
# Usage:
#   bash routines/lib/portfolio-core-parse.sh [index-path]
#     index-path default: <agent-root>/product/PORTFOLIO.md
# Output contract (same as the historical instance parsers):
#   stdout PORTFOLIO-PARSE-OK,   exit 0  on pass
#   stdout PORTFOLIO-PARSE-FAIL + "reason: ..." line, exit nonzero on fail
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEFAULT_INDEX="$AGENT_DIR/product/PORTFOLIO.md"

exec python3 "$SCRIPT_DIR/portfolio-core-lint.py" --parse-only "${1:-$DEFAULT_INDEX}"
