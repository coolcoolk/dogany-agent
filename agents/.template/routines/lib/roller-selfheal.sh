#!/bin/bash
# roller-selfheal.sh -- unload the routine-roller unit on a kit-less instance
# (DGN-1959). Called by update.sh after the routines refresh.
#
# Usage: roller-selfheal.sh <instance_root>
#
# Why: the roller schedule belongs to the life kit (kit-handover/kit_life,
# DGN-1399), but instances minted before that move still have the daily unit
# loaded. Without the kit the run has nothing to roll; before DGN-1959 it died
# on `import lifekit` every night and cron-guard pushed "routine failed" (74
# times on one PoC). routine-roller.sh now no-ops there, and this removes the
# unit itself so the dead schedule stops firing at all.
#
# Kit present (life engine resolves via routines/lib/life-engine.sh) -> no-op:
# the kit's activation path owns the unit. Kit absent and the unit is loaded,
# or its LaunchAgents plist is still on disk -> routine-ctl.sh disable
# routine-roller, the sanctioned single-entry teardown (bootout + plist to
# Trash + health-observer retire marker, DGN-1610).
#
# Never fails the caller: rc 0 always, except 1 on a usage error.
set -u

ROOT="${1:-}"
[ -n "$ROOT" ] && [ -d "$ROOT" ] || { echo "usage: roller-selfheal.sh <instance_root>" >&2; exit 1; }
LIB="$ROOT/routines/lib"
[ -r "$LIB/life-engine.sh" ] && [ -f "$LIB/routine-ctl.sh" ] || exit 0

. "$LIB/life-engine.sh"
[ -n "$(life_engine_stem "$ROOT" py db)" ] && exit 0

# Same label derivation as routine-ctl.sh (manifest name, else dirname).
AGENT_NAME="$(grep -E '^DOGANY_AGENT_NAME=' "$ROOT/.instance.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
AGENT_NAME="${AGENT_NAME:-$(basename "$ROOT")}"
LABEL="com.telegram-skill-bot.${AGENT_NAME}.routine-roller"
LA_DIR="${ROUTINE_CTL_LA_DIR:-$HOME/Library/LaunchAgents}"

present=0
bash "$LIB/routine-ctl.sh" status routine-roller >/dev/null 2>&1 && present=1
[ -f "$LA_DIR/${LABEL}.plist" ] && present=1
[ "$present" = "1" ] || exit 0

if bash "$LIB/routine-ctl.sh" disable routine-roller >/dev/null 2>&1; then
  echo "[roller-selfheal] unloaded $LABEL: life kit not installed on this instance (DGN-1959)"
else
  echo "[roller-selfheal] WARN: could not fully unload $LABEL (routine-ctl disable failed); routine-roller.sh still no-ops without the kit" >&2
fi
exit 0
