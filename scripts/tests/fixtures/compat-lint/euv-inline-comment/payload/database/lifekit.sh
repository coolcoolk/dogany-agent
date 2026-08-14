#!/bin/bash
# FIXTURE STUB: lifekit.sh for compat-lint C6 smoke test
# Returns exit 0 for check and dump verbs (contract surface stub).
case "${1:-}" in
  check|dump) exit 0 ;;
  *) exit 1 ;;
esac
