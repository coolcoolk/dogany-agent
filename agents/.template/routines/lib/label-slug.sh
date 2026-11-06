#!/bin/bash
# label-slug.sh -- DGN-2055: derive an instance's slug from its own launchd
# label prefix (com.telegram-skill-bot.<slug>.<unit>), exact case. Sourced by
# handoff-stale-sweep.sh and update.sh. Never guessed from the folder name.
#
# label_slug_for_root <root> [launch_agents_dir]
#   Scans <dir>/com.telegram-skill-bot.*.plist for plists that reference
#   <root>; prints the slug when all matches agree on one. Prints nothing
#   (rc 1) when no label resolves or the matches disagree.
label_slug_for_root() {
  local root="${1%/}" dir="${2:-${DOGANY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}}"
  local f base slug found=""
  [[ -n "$root" && -d "$dir" ]] || return 1
  for f in "$dir"/com.telegram-skill-bot.*.plist; do
    [[ -f "$f" ]] || continue
    grep -qF -- "$root/" "$f" 2>/dev/null || continue
    base="$(basename "$f" .plist)"
    slug="${base#com.telegram-skill-bot.}"
    slug="${slug%%.*}"
    [[ -n "$slug" ]] || continue
    if [[ -z "$found" ]]; then found="$slug"
    elif [[ "$found" != "$slug" ]]; then return 1
    fi
  done
  [[ -n "$found" ]] || return 1
  printf '%s\n' "$found"
}

# slug_backfill_conf <instance_root> [dry]
#   Appends SLUG=<label slug> to <root>/config/agent.conf when the line is
#   absent. Idempotent. rc 0 + one stdout line on backfill/dry/unresolved.
slug_backfill_conf() {
  local root="$1" dry="${2:-0}" conf slug
  conf="$root/config/agent.conf"
  [[ -f "$conf" ]] || return 0
  grep -q '^SLUG=' "$conf" && return 0
  if slug="$(label_slug_for_root "$root")" && [[ -n "$slug" ]]; then
    if [[ "$dry" == "1" ]]; then echo "would backfill SLUG=$slug into config/agent.conf"; return 0; fi
    [[ -z "$(tail -c1 "$conf")" ]] || printf '\n' >> "$conf"
    printf 'SLUG=%s\n' "$slug" >> "$conf"
    echo "backfilled SLUG=$slug into config/agent.conf"
  else
    echo "SLUG= absent and no launchd label resolves a slug; left unchanged"
  fi
}
