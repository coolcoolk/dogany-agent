#!/bin/bash
# reclass_sole_domain.sh -- promote a SOLE domain instance to main
# (DGN-1892 part 2, dec-268).
#
# Measured dead end (owner relay 2026-10-06 17:32): an older installer flow
# minted the FIRST and only agent of a machine as DOGANY_AGENT_CLASS=domain
# (first agent = domain, main-add optional). Every class reader then treats
# the owner's one agent as a staff lane: the dogany-lifekit-setup gate
# refuses new activation ("point the user to their main agent" -- there is
# none), and pack_install's kit_subset delivers only a domain subset. So
# "라이프킷 켜줘" dead-ends with no supported way out.
#
# Fix = the class itself, not a second rule beside it: when this instance is
# class=domain AND the machine has no main at all AND no other instance, it
# IS the machine's main, and both class stores are rewritten to say so. Every
# existing reader (install.sh instance_class_of, onboarding-check.py
# instance_class, kit_subset.py, the setup skill's gate, update.sh's
# main-only steps) then reads main with no change of its own.
#
# The two stores (install.sh dgn227_postmint writes both):
#   <root>/.instance.conf     DOGANY_AGENT_CLASS=<main|domain> (absent = main)
#   $HOME/.dogany/instances   "<class>\t<canon-root>" (install registry)
#
# Promote only when ALL hold (any doubt -> leave it, report why):
#   - .instance.conf says DOGANY_AGENT_CLASS=domain;
#   - the registry names no main for ANOTHER root (main-max-1, install.sh
#     flow_main_add_finalize's invariant; a stale main row also blocks);
#   - discovery (lib/discover_instances.sh, the update planner's one rule)
#     finds no instance other than this one;
#   - config/agent.conf carries no HANDOFF_PEER_MAIN (a staff agent minted
#     under a main, mint_run.sh g3b_align_domain).
# Write order: registry row first, class stamp LAST. A crash in between
# leaves registry=main(self) + conf=domain, which the next run still
# promotes (a main row for THIS root does not block). Re-run after success
# = noop. Nothing else is touched: KIT= / LIFEKIT= / routines / bot stay as
# they are (the owner asks for the kit; the setup skill turns it on).
#
# Usage: reclass_sole_domain.sh <agent-root> [--dry-run]
# stdout (one line each):
#   reclass=promoted|would-promote|noop|skip
#   reason=<machine text>
# rc 0 for every outcome above; rc 1 a write failed; rc 2 usage.
# Callers: update.sh (every update, before its main-only steps).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: reclass_sole_domain.sh <agent-root> [--dry-run]" >&2; exit 2; }

ROOT="${1:-}"
[[ -n "$ROOT" && -d "$ROOT" ]] || usage
shift
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    *) usage ;;
  esac
done
ROOT="$(cd "$ROOT" && pwd -P)"
INSTANCE_CONF="$ROOT/.instance.conf"
REGISTRY="$HOME/.dogany/instances"

out() { printf 'reclass=%s\nreason=%s\n' "$1" "$2"; exit 0; }

# conf_val <file> <key> -- first KEY= value, '' when absent (install.sh conf_read).
conf_val() { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- || true; }

[[ -f "$INSTANCE_CONF" ]] || out skip "no .instance.conf"
[[ "$(conf_val "$INSTANCE_CONF" DOGANY_AGENT_CLASS)" == "domain" ]] \
  || out noop "class is main"

if [[ -f "$REGISTRY" ]]; then
  other_main="$(awk -F'\t' -v r="$ROOT" '$1 == "main" && $2 != r { print $2; exit }' "$REGISTRY")"
  [[ -z "$other_main" ]] || out skip "registry names a main: $other_main"
fi

[[ -z "$(conf_val "$ROOT/config/agent.conf" HANDOFF_PEER_MAIN)" ]] \
  || out skip "staff agent of a main (HANDOFF_PEER_MAIN set)"

# shellcheck source=lib/discover_instances.sh
. "$SCRIPT_DIR/lib/discover_instances.sh"
scan="$(DOGANY_DISCOVER_SELF_ROOT="$ROOT" _discover_instances)" \
  || out skip "cannot scan for other instances"
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  [[ "${row#*:}" == "$ROOT" ]] || out skip "other instance present: ${row#*:}"
done <<< "$scan"

[[ "$DRY_RUN" == "1" ]] && out would-promote "sole domain, no main on this machine"

# shellcheck source=lib/atomic_write.sh
. "$SCRIPT_DIR/lib/atomic_write.sh"
registry_body() {   # install.sh registry_upsert: drop this root's row, append the fresh one
  if [[ -f "$REGISTRY" ]]; then awk -F'\t' -v r="$ROOT" '$2 != r' "$REGISTRY"; fi
  printf 'main\t%s\n' "$ROOT"
}
class_body() { sed 's/^DOGANY_AGENT_CLASS=.*/DOGANY_AGENT_CLASS=main/' "$INSTANCE_CONF"; }

# 1. registry row, 2. class stamp LAST.
mkdir -p "$(dirname "$REGISTRY")"
atomic_write_cmd "$REGISTRY" registry_body \
  || { echo "reclass_sole_domain: registry write failed: $REGISTRY" >&2; exit 1; }
atomic_write_cmd "$INSTANCE_CONF" class_body \
  || { echo "reclass_sole_domain: class write failed: $INSTANCE_CONF" >&2; exit 1; }

out promoted "sole domain, no main on this machine"
