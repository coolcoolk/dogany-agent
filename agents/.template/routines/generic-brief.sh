#!/bin/bash
# generic-brief.sh {morning|retro|weekly} -- kit-neutral briefing skeleton
# (DGN-227 E1-1 layer 1). PII-free placeholder: content quality is NOT this
# rehearsal's scope -- the shipped skeleton composes kit-neutral sources only
# (schedule via mirror when present, memory highlights, own domain section,
# peer aggregation via routines/lib/handoff-aggregate when acting as main).
#
# Routing (DGN-227 E2-2/E3): reads BRIEF_ROUTING from config/agent.conf at
# RUN time -- standalone = self-publish via push, submit = write the section
# file into the main peer's files/handoff inbox (report.section.<slot>).
# Key precedence: BRIEF_ROUTING wins; absent -> peer-key fallback
# (HANDOFF_PEER_MAIN, then legacy HANDOFF_PEER_AG); conflict -> loud log.
set -euo pipefail

SLOT="${1:-}"
case "$SLOT" in
  morning|retro|weekly) : ;;
  *) echo "usage: generic-brief.sh {morning|retro|weekly}" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/config/agent.conf"

conf_get() { grep -E "^$1=" "$CONF" 2>/dev/null | head -1 | cut -d= -f2- || true; }

ROUTING="$(conf_get BRIEF_ROUTING)"
PEER_MAIN="$(conf_get HANDOFF_PEER_MAIN)"
PEER_AG="$(conf_get HANDOFF_PEER_AG)"

if [[ -z "$ROUTING" ]]; then
  # Fallback: peer key presence (briefing-reader scope ONLY -- E2-2 rule 2).
  if [[ -n "$PEER_MAIN" || -n "$PEER_AG" ]]; then ROUTING="submit"; else ROUTING="standalone"; fi
elif [[ "$ROUTING" == "standalone" && ( -n "$PEER_MAIN" || -n "$PEER_AG" ) ]]; then
  echo "[generic-brief] WARN: BRIEF_ROUTING=standalone but a briefing peer key is set -- BRIEF_ROUTING wins (E2-2 rule 3)" >&2
fi

# Aggregation edition marker (E3 gate stage 2 greps for this consumption):
# when acting as main, peer sections are aggregated via the kit-neutral
# routines/lib/handoff-aggregate library.
AGG_LIB="$ROOT/routines/lib/handoff-aggregate"
PEERS="$(conf_get BRIEF_PEERS)"
if [[ -n "$PEERS" && -f "$AGG_LIB" ]]; then
  # shellcheck source=/dev/null
  source "$AGG_LIB"
  handoff_aggregate "$ROOT" "$SLOT" "$PEERS" || true
fi

echo "[generic-brief] slot=$SLOT routing=$ROUTING (skeleton -- section composition placeholder)"
exit 0
