#!/bin/bash
case "${1:-}" in
  check|dump) exit 0 ;;
  *) exit 1 ;;
esac
