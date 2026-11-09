#!/bin/bash
# Compatibility entry point; the library owns all routine lifecycle changes.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/routine-ctl.sh" "$@"
