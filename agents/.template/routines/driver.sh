#!/usr/bin/env bash
# Versioned installed-pack resolver. Legacy entrypoints remain unchanged in G1.
set -eu
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -B "$_here/lib/driver_resolver.py" "$@"
