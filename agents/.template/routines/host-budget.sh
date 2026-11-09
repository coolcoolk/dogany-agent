#!/usr/bin/env bash
# Additive generic host API. Old host_dispatcher CLI verbs stay available.
set -eu
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$_here/lib/host_budget.py" "$@"
