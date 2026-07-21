#!/usr/bin/env bash
# promote-to-main.sh -- estate-growth promotion core (부하/아래로 갈래).
#
# In-place promote an EXISTING HAND/implicit-main agent to explicit `main`
# class, then mint a domain subordinate ("부하") under it. The parent's bot /
# token / conversation-memory are UNCHANGED (dec-079 symmetrization): the
# front-door bot stays the same. Only the NEW domain subordinate gets its own
# Telegram bot (a domain agent is a full instance -- BotFather token needed).
# Depth-1 ONLY (2 layers: main > domain 부하). Domain-under-domain (3 layers)
# is FORBIDDEN -- g3b only knows the flat main/domain 2-row model.
#
# Usage:
#   DOGANY_BOT_TOKEN=<domain-bot-token> \
#     promote-to-main.sh --domain-slug <slug> --role <prose> \
#       [--root <parent-root>] [--pack <pack-id>] \
#       [--l1-db] [--tier basic] [--model <sonnet|opus|haiku>] \
#       [--dry-run]
#
#   --domain-slug   REQUIRED. ascii kebab-case slug for the new domain 부하.
#   --role          REQUIRED. Primary-focus prose stamped into the domain's
#                   AGENT.md. Agent-to-agent minting has no human onboarding
#                   to ask role, so the minting parent MUST supply it. This
#                   applies on BOTH the core-only path and the pack path.
#   --root          parent (promoting) agent root. Default: parent of this
#                   script's dir (routines/ -> agent root).
#   --pack          OPT-IN pack id. When set, routes the mint via mint_run.sh
#                   `pipeline` mode, which calls g3b_align_domain INTERNALLY
#                   (via --peer-main). Do NOT run a separate align-peer-main
#                   step in this path (avoid double-align). When unset (default):
#                   core-only mint + explicit align-peer-main step.
#   --l1-db         OPT-IN data-access step (default OFF). Wire the new domain's
#                   agent.conf L1_DB to the PARENT's shared lifekit data layer
#                   (<parent-root>/database/lifekit.db) + L1_EXPECTED_USER_VERSION.
#                   ORTHOGONAL to KIT= (ownership axis): warg is the precedent
#                   (KIT=none but L1_DB=Ag). Health domain needs it; dev does not.
#   --tier          parent tier to ensure after class stamp. Only `basic` is
#                   meaningful (lite -> basic accompaniment). Default: basic.
#   --model         model for the new domain instance (default: sonnet).
#   --dry-run       plan only; no writes anywhere (delegated to mint_run dry-run).
#
# Sequence (P28 pointer order = state entries first, identity/marker LAST, so a
# crash leaves a resumable/safe partial state). Every write is upsert or
# write-if-absent, so an idempotent re-run substitutes for a transaction:
#   (pre) BotFather token gate: moved BEFORE class stamp so a mis-fire stops
#         before mutating parent .instance.conf (MED-4). A domain 부하 is a
#         full instance -- absent token -> STOP CLEANLY, no partial mint.
#   1. class stamp (atomic): parent .instance.conf DOGANY_AGENT_CLASS
#      implicit-main -> explicit `main`; if DOGANY_TIER=lite also lite -> basic
#      (accompanying, install pattern). Idempotent no-op if already main/basic.
#      Tier is written BEFORE class so a crash never leaves a KIT/tier mismatch
#      that the onboarding reconcile guard rejects.
#   2. bot-count ceiling preflight: registry count vs effective_max_agents.
#      Fails fast (BEFORE mint) when ceiling would be breached.
#   3. 부하 민팅: mint the domain subordinate via mint_run.sh pointing at the
#      parent as its peer-main (--peer-main <parent-root>). If bridge/ already
#      exists (prior crash), skip mint and attempt recover instead.
#      Reuses the existing mint machinery + crash-safe journal; NO reimplementation.
#   4. routing wiring: mint_run.sh's g3b_align_domain runs as part of the mint
#      (registry + limit ledger + HANDOFF_PEER_MAIN + BRIEF_ROUTING=submit for
#      the new domain; parent gets BRIEF_PEERS / aggregator-capable). Reused,
#      never reimplemented. After align, auto-loads parent deferred brief units
#      when BRIEF_ROUTING came back standalone (gate failed on first try).
#   5. L1_DB OPTIONAL step (--l1-db): wire the domain's shared-read access to
#      the parent's lifekit L1 data layer. Default OFF.
#      NOTE: P28 order (state before marker): for the CORE-ONLY path, L1_DB
#      write (step 5) happens after align (step 4) and before any
#      identity-marker step -- this is correct P28 state-before-marker order.
#      For the PACK path the pipeline writes marker (pack-install step 2) before
#      L1_DB here -- that crash window is inherent to the pipeline design and is
#      covered by HIGH-2 re-run recovery (bridge/ present -> skip mint, re-align).
#   6. depth/count guard: front-door bot always 1; reject a 3rd-layer attempt
#      (parent is itself a domain). DOGANY_MAX_AGENTS is the bot-count ceiling,
#      maintained by g3b_align_domain.
#
# Safety (partial-state guarantee):
#   - BotFather token gate runs BEFORE class stamp (no parent mutation on mis-fire).
#   - class-stamp uses tmp+mv atomic writes; tier written first.
#   - Partial success leaves the parent a valid "main with no subordinate yet".
#   - Re-run after a crash: class stamp is a no-op (already main); if bridge/
#     exists mint is skipped and recover is attempted; g3b keys are idempotent.
#
# One-writer note: this is the SOLE orchestrator of the 부하-promotion sequence.
# It does NOT hand-edit registry / limit-ledger / routing keys -- those are
# owned by mint_run.sh's g3b_align_domain. It writes ONLY the parent's class/
# tier stamp and (opt-in) the domain's L1_DB keys, via the atomic conf helper.
#
# DGN-475 (estate-growth promotion, design SOUND-locked), dec-078 (골격),
# dec-079 (대칭화: 기존 봇 in-place, 새 봇/토큰 없음 for the parent),
# dec-080 (--role unconditionally required), dec-081 (auto-load parent brief).
# NOTE: .template has no sibling promote-to-craft.sh; that sibling exists
# only on instances that have received the craft-promotion pack.
#
# Exit codes:
#   0  success (or no-op when already promoted / dry-run plan OK)
#   1  invalid arguments, unsupported input, or depth/guard violation
#   2  a write or mint step failed
#   3  BotFather token gate not satisfied (no partial mint performed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

die() { printf 'promote-to-main: ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf 'promote-to-main: %s\n' "$*"; }

# conf_read <file> <key> -- read a KEY=value line; prints value or empty.
# MED-6a: use || [ -n "$line" ] to handle a final newline-less line.
conf_read() {
  local file="$1" key="$2" line
  [ -f "$file" ] || { printf ''; return 0; }
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "${key}="*) printf '%s' "${line#*=}"; return 0 ;;
    esac
  done < "$file"
  printf ''
}

# conf_upsert_atomic <file> <key> <value> -- replace KEY= line or append;
# uses tmp+mv for atomicity. Creates parent dirs.
# NOTE latent: values with | & \ would corrupt the sed expression below.
conf_upsert_atomic() {
  local file="$1" key="$2" val="$3"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  local tmp
  tmp="$(mktemp "${file}.XXXXXX")"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed "s|^${key}=.*|${key}=${val}|" "$file" > "$tmp"
  else
    cat "$file" > "$tmp"
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

DOMAIN_SLUG=""
ROOT=""
WANT_L1DB=0
TIER="basic"
MODEL=""
ROLE_PROSE=""
PACK_ID=""
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain-slug)
      [ $# -ge 2 ] || die "--domain-slug requires an argument"
      DOMAIN_SLUG="$2"; shift 2 ;;
    --root)
      [ $# -ge 2 ] || die "--root requires an argument"
      ROOT="$2"; shift 2 ;;
    --pack)
      [ $# -ge 2 ] || die "--pack requires an argument"
      PACK_ID="$2"; shift 2 ;;
    --l1-db)
      WANT_L1DB=1; shift ;;
    --tier)
      [ $# -ge 2 ] || die "--tier requires an argument"
      TIER="$2"; shift 2 ;;
    --model)
      [ $# -ge 2 ] || die "--model requires an argument"
      MODEL="$2"; shift 2 ;;
    --role)
      [ $# -ge 2 ] || die "--role requires an argument"
      ROLE_PROSE="$2"; shift 2 ;;
    --dry-run|--dry)
      DRY=1; shift ;;
    *)
      die "unknown argument: $1" ;;
  esac
done

[ -n "$DOMAIN_SLUG" ] || die "--domain-slug is required (ascii kebab-case slug for the new domain 부하)"

# FATAL-1 / dec-080: --role is UNCONDITIONALLY REQUIRED on ALL paths.
# The catalog role_prose inheritance mechanism (catalog_role_prose in install.sh)
# exists ONLY on the interactive install.sh flow -- it is never called on the
# pack_install.sh pipeline path (grep -i role in pack_install.sh = 0 matches).
# A WARN-and-proceed would leave the domain permanently role-less, which is
# forbidden. Die clearly regardless of whether --pack is set.
[ -n "$ROLE_PROSE" ] || die "--role is required (both core-only and pack paths; catalog role_prose is only consumed by the interactive install.sh flow, not by the pipeline -- omitting --role leaves the domain role-less)"

# Slug format guard -- same regex mint_run.sh enforces (mint_run.sh:586), so a
# malformed slug fails HERE before any class stamp, not mid-sequence.
if ! printf '%s' "$DOMAIN_SLUG" | grep -Eq '^[a-z][a-z0-9-]{1,30}$'; then
  die "slug must be ascii kebab-case (^[a-z][a-z0-9-]{1,30}\$): $DOMAIN_SLUG"
fi

# --tier: only lite -> basic accompaniment is meaningful.
case "$TIER" in
  basic) : ;;
  *) die "unsupported --tier '$TIER' (only 'basic' is supported for parent promotion)" ;;
esac

# Default root: the parent of SCRIPT_DIR (routines/ -> agent root).
if [ -z "$ROOT" ]; then
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
[ -d "$ROOT" ] || die "parent root not a directory: $ROOT"

INSTANCE_CONF="$ROOT/.instance.conf"

# ---------------------------------------------------------------------------
# depth-1 guard (run FIRST): reject a 3rd-layer attempt.
# ---------------------------------------------------------------------------
# MED-6b: also die if the parent already carries HANDOFF_PEER_MAIN or
# BRIEF_ROUTING=submit in its config/agent.conf. A class-unrecorded de-facto
# domain must not become a 3rd layer.
current_class="$(conf_read "$INSTANCE_CONF" DOGANY_AGENT_CLASS)"
if [ "$current_class" = "domain" ]; then
  die "parent $ROOT is class=domain -- domain-under-domain (3 layers) is forbidden (depth-1 only)"
fi
PARENT_AGENT_CONF="$ROOT/config/agent.conf"
parent_handoff_peer="$(conf_read "$PARENT_AGENT_CONF" HANDOFF_PEER_MAIN)"
parent_brief_routing="$(conf_read "$PARENT_AGENT_CONF" BRIEF_ROUTING)"
if [ -n "$parent_handoff_peer" ]; then
  die "parent $ROOT carries HANDOFF_PEER_MAIN=$parent_handoff_peer -- de-facto domain cannot become a 3rd layer (depth-1 only)"
fi
if [ "$parent_brief_routing" = "submit" ]; then
  die "parent $ROOT carries BRIEF_ROUTING=submit -- de-facto domain cannot become a 3rd layer (depth-1 only)"
fi

# ---------------------------------------------------------------------------
# resolve mint machinery + parent repo root.
# ---------------------------------------------------------------------------
REPO_ROOT="$(conf_read "$INSTANCE_CONF" DOGANY_REPO_ROOT)"
[ -n "$REPO_ROOT" ] || die "DOGANY_REPO_ROOT not set in $INSTANCE_CONF -- cannot locate mint machinery (no path guess)"
MINT_RUN="$REPO_ROOT/scripts/pack/mint_run.sh"
[ -f "$MINT_RUN" ] || die "mint_run.sh not found at $MINT_RUN"

GLOBAL_CONF="$HOME/.dogany/config"
REGISTRY_FILE="$HOME/.dogany/instances"

# The new domain's root.
CAP="$(printf '%s' "$DOMAIN_SLUG" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
DOMAIN_ROOT="$HOME/dogany/$CAP"

# ---------------------------------------------------------------------------
# HIGH-2(a): fail-fast pack id validation BEFORE any class stamp or mint.
# ---------------------------------------------------------------------------
if [ -n "$PACK_ID" ]; then
  CATALOG_FILE="$REPO_ROOT/packs/catalog.json"
  if [ ! -f "$CATALOG_FILE" ]; then
    die "catalog not found: $CATALOG_FILE -- cannot validate --pack $PACK_ID"
  fi
  pack_found="$(python3 - "$CATALOG_FILE" "$PACK_ID" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    cat = json.load(f)
found = any(p.get("id") == sys.argv[2] for p in cat.get("packs", []))
print("1" if found else "0")
PYEOF
  )"
  [ "$pack_found" = "1" ] || die "pack id '$PACK_ID' not found in catalog ($CATALOG_FILE) -- aborting before any class stamp or mint"
fi

# ---------------------------------------------------------------------------
# idempotency note: class stamp is a no-op if already main/basic.
# ---------------------------------------------------------------------------
current_tier="$(conf_read "$INSTANCE_CONF" DOGANY_TIER)"
if [ "$current_class" = "main" ] && [ "$current_tier" = "$TIER" ]; then
  info "parent already promoted (DOGANY_AGENT_CLASS=main, DOGANY_TIER=$TIER) -- class stamp is a no-op"
fi

# ---------------------------------------------------------------------------
# MED-4: BotFather token gate BEFORE step 1 (class stamp).
# Moved here so a token-less mis-fire stops WITHOUT mutating parent .instance.conf.
# ---------------------------------------------------------------------------
if [ "$DRY" -ne 1 ] && [ -z "${DOGANY_BOT_TOKEN:-}" ]; then
  printf 'promote-to-main: STOP: the new domain 부하 "%s" needs its OWN Telegram bot token.\n' "$DOMAIN_SLUG" >&2
  printf 'promote-to-main: The parent front-door bot stays unchanged (dec-079); only the\n' >&2
  printf 'promote-to-main: subordinate is a new full instance requiring a BotFather token.\n' >&2
  printf 'promote-to-main: Create the bot via BotFather, then re-run with:\n' >&2
  printf 'promote-to-main:   DOGANY_BOT_TOKEN=<token> %s --domain-slug %s ...\n' "$(basename "$0")" "$DOMAIN_SLUG" >&2
  printf 'promote-to-main: The parent .instance.conf was NOT modified (safe to retry).\n' >&2
  exit 3
fi

# ---------------------------------------------------------------------------
# step 1: class stamp (atomic). tier FIRST (safety), then class.
# ---------------------------------------------------------------------------
# Mirrors install.sh:641. Writing tier before class means a crash between the
# two never yields class=main + tier=lite that a reconcile guard would flag.
if [ "$DRY" -eq 1 ]; then
  info "step 1/5 (dry-run): would set DOGANY_TIER=$TIER then DOGANY_AGENT_CLASS=main in $INSTANCE_CONF"
else
  if [ "$current_tier" != "$TIER" ]; then
    info "step 1/5: DOGANY_TIER=$TIER -> $INSTANCE_CONF"
    conf_upsert_atomic "$INSTANCE_CONF" DOGANY_TIER "$TIER" \
      || { printf 'promote-to-main: ERROR: step 1 (tier) failed\n' >&2; exit 2; }
  fi
  if [ "$current_class" != "main" ]; then
    info "step 1/5: DOGANY_AGENT_CLASS=main -> $INSTANCE_CONF"
    conf_upsert_atomic "$INSTANCE_CONF" DOGANY_AGENT_CLASS "main" \
      || { printf 'promote-to-main: ERROR: step 1 (class) failed\n' >&2; exit 2; }
  fi
fi

# ---------------------------------------------------------------------------
# MED-5: bot-count ceiling preflight (BEFORE mint).
# Reuses the effective_max_agents logic from install.sh (~401-411):
#   non-1 DOGANY_MAX_AGENTS env wins; else persisted ledger; default 1.
# g3b only raises the odometer on success -- it never refuses. Enforcement
# must live here so a ceiling breach dies BEFORE consuming a bot token.
# ---------------------------------------------------------------------------
effective_max_agents() {
  local v="${DOGANY_MAX_AGENTS:-1}"
  if [ "$v" = "1" ] && [ -f "$GLOBAL_CONF" ]; then
    local lv; lv="$(conf_read "$GLOBAL_CONF" DOGANY_MAX_AGENTS)"
    [ -n "$lv" ] && v="$lv"
  fi
  printf '%s' "${v:-1}"
}

if [ "$DRY" -eq 1 ]; then
  info "step 2/5 (dry-run): would check bot-count ceiling (registry vs effective_max_agents)"
else
  _max="$(effective_max_agents)"
  _reg_count=0
  if [ -f "$REGISTRY_FILE" ]; then
    _reg_count="$(grep -c . "$REGISTRY_FILE" 2>/dev/null || true)"
  fi
  if [ "$_reg_count" -ge "$_max" ]; then
    die "bot-count ceiling: registry count ($_reg_count) >= effective_max_agents ($_max) -- raise DOGANY_MAX_AGENTS in $GLOBAL_CONF (or set env DOGANY_MAX_AGENTS=<n>) before minting a new domain 부하"
  fi
  info "step 2/5: bot-count preflight OK (registry=$_reg_count, max=$_max)"
fi

# ---------------------------------------------------------------------------
# step 3+4: mint the domain 부하 + routing wiring.
# ---------------------------------------------------------------------------
# Two paths:
#   --pack <id> (pack path): pipeline mode, g3b_align_domain runs internally.
#     Do NOT run a separate align-peer-main step to avoid double-align.
#     P28 note: the pipeline writes the pack marker (step 2c) before L1_DB
#     (step 5 here) -- that crash window is inherent to the pipeline design
#     and is covered by HIGH-2 re-run recovery (bridge/ present -> skip mint).
#   no --pack (core-only path): bare mint (core-only) + explicit align-peer-main.
#     P28 order preserved: mint -> L1_DB (step 5) -> no trailing marker step.
#
# HIGH-2(b) re-run safety: if bridge/ already exists (prior crash), skip mint
# and attempt recover mode instead. align-peer-main is idempotent, so it
# always runs after the (skipped or real) mint step.
# Contract verified: mint_run.sh recover requires --instance-root + --slug;
# if no journal exists it exits 0 with "nothing to recover" (safe).
#
# Q4 note (mint_run.sh:659 owner-id dry-run guard): mint_run.sh now mirrors
# the token dry-run pattern for owner-id (applied in mint_run.sh at line ~659).
# The --instance-root passthrough below ensures mint_run.sh finds .telegram_bot/.env
# on real live instance promotions (normal promotion context).

if [ -n "$PACK_ID" ]; then
  # --- pack path: pipeline mode (g3b_align_domain runs internally) ---
  MINT_ARGS=(pipeline --slug "$DOMAIN_SLUG" --root "$DOMAIN_ROOT"
             --pack "$PACK_ID" --instance-root "$ROOT"
             --peer-main "$ROOT" --no-start)
  [ -n "$MODEL" ] && MINT_ARGS+=(--model "$MODEL")
  MINT_ARGS+=(--role "$ROLE_PROSE")
  [ "$DRY" -eq 1 ] && MINT_ARGS+=(--dry-run)

  if [ "$DRY" -eq 1 ]; then
    info "step 3+4/5 (dry-run, pack path): would run pipeline --pack $PACK_ID --peer-main $ROOT (g3b_align_domain runs internally; no separate align-peer-main)"
    bash "$MINT_RUN" "${MINT_ARGS[@]}" \
      || { printf 'promote-to-main: ERROR: step 3+4 dry-run (pipeline) failed\n' >&2; exit 2; }
  else
    info "step 3+4/5 (pack path): pipeline --pack $PACK_ID --peer-main $ROOT -> $DOMAIN_ROOT (g3b_align_domain runs internally)"
    bash "$MINT_RUN" "${MINT_ARGS[@]}" \
      || { printf 'promote-to-main: ERROR: step 3+4 (pipeline) failed\n' >&2; exit 2; }
  fi
else
  # --- core-only path: bare mint + explicit align-peer-main ---
  # HIGH-2(b): detect an already-minted DOMAIN_ROOT (bridge/ present).
  if [ "$DRY" -ne 1 ] && [ -d "$DOMAIN_ROOT/bridge" ]; then
    info "step 3/5: domain root already exists ($DOMAIN_ROOT/bridge present) -- skipping mint, attempting recover"
    bash "$MINT_RUN" recover --slug "$DOMAIN_SLUG" --instance-root "$ROOT" --root "$DOMAIN_ROOT" \
      || { printf 'promote-to-main: ERROR: step 3 (recover) failed\n' >&2; exit 2; }
  else
    MINT_ARGS=(mint --slug "$DOMAIN_SLUG" --root "$DOMAIN_ROOT"
               --instance-root "$ROOT" --core-only)
    [ -n "$MODEL" ] && MINT_ARGS+=(--model "$MODEL")
    MINT_ARGS+=(--role "$ROLE_PROSE")
    [ "$DRY" -eq 1 ] && MINT_ARGS+=(--dry-run)

    if [ "$DRY" -eq 1 ]; then
      info "step 3/5 (dry-run): would mint domain 부하 '$DOMAIN_SLUG' -> $DOMAIN_ROOT (core-only)"
      bash "$MINT_RUN" "${MINT_ARGS[@]}" \
        || { printf 'promote-to-main: ERROR: step 3 dry-run (mint) failed\n' >&2; exit 2; }
    else
      info "step 3/5: minting domain 부하 '$DOMAIN_SLUG' -> $DOMAIN_ROOT (core-only)"
      bash "$MINT_RUN" "${MINT_ARGS[@]}" \
        || { printf 'promote-to-main: ERROR: step 3 (mint) failed\n' >&2; exit 2; }
    fi
  fi

  # Step 4: explicit align-peer-main (idempotent; runs after mint or recover).
  if [ "$DRY" -eq 1 ]; then
    info "step 4/5 (dry-run): would run g3b align-peer-main (registry + limit ledger + HANDOFF_PEER_MAIN + BRIEF_ROUTING=submit)"
  else
    info "step 4/5: g3b routing wiring (align-peer-main) -> registry + limit ledger + routing keys"
    bash "$MINT_RUN" align-peer-main --root "$DOMAIN_ROOT" --peer-main "$ROOT" --slug "$DOMAIN_SLUG" \
      || { printf 'promote-to-main: ERROR: step 4 (g3b align) failed\n' >&2; exit 2; }

    # HIGH-3 / dec-081: auto-load parent's deferred brief units when the gate
    # failed and BRIEF_ROUTING came back standalone. The _g3b_submit_gate
    # (mint_run.sh) checks that the parent's briefing launchd unit is LOADED.
    # Template defers all generic-brief plists (plists.defer). The lite->basic
    # promotion step above does not load them; this block does.
    #
    # Mechanism: `mint_run.sh start --root <parent> --deferred` loads ONLY the
    # units listed in the parent's routines/plists.defer (DGN-238 grill-final
    # MAJOR-1 / mint_run.sh start mode --deferred flag). After loading, we
    # re-run align-peer-main (idempotent) so the gate re-evaluates with the
    # units now loaded -- if it passes this time, BRIEF_ROUTING flips to submit.
    #
    # SIDE EFFECT (intentional): loading the parent's brief units turns ON the
    # parent's briefing cron. This WILL produce actual briefing messages in the
    # parent's user-facing channel. This is inherent to making the parent an
    # aggregator -- you cannot have aggregation without the aggregator's brief
    # units running.
    domain_brief_routing="$(conf_read "$DOMAIN_ROOT/config/agent.conf" BRIEF_ROUTING)"
    if [ "$domain_brief_routing" = "standalone" ]; then
      info "step 4/5: BRIEF_ROUTING=standalone (gate failed -- parent brief units likely not loaded); loading parent deferred brief units then re-aligning"
      bash "$MINT_RUN" start --root "$ROOT" --deferred \
        || { printf 'promote-to-main: ERROR: step 4 (load parent deferred briefs) failed\n' >&2; exit 2; }
      info "step 4/5: re-running align-peer-main (idempotent) after parent brief units loaded"
      bash "$MINT_RUN" align-peer-main --root "$DOMAIN_ROOT" --peer-main "$ROOT" --slug "$DOMAIN_SLUG" \
        || { printf 'promote-to-main: ERROR: step 4 (g3b re-align) failed\n' >&2; exit 2; }
    fi
  fi
fi

# ---------------------------------------------------------------------------
# step 5: L1_DB OPTIONAL data-access wiring (--l1-db; default OFF).
# ---------------------------------------------------------------------------
# ORTHOGONAL to KIT= (ownership axis). Warg precedent: KIT=none but L1_DB=Ag.
# P28 note (core-only path): this write (state) comes AFTER align-peer-main
# and before any identity-marker step -- correct P28 state-before-marker order.
if [ "$WANT_L1DB" -eq 1 ]; then
  L1_DB_PATH="$ROOT/database/lifekit.db"
  L1_VER="$(conf_read "$PARENT_AGENT_CONF" L1_EXPECTED_USER_VERSION)"
  DOMAIN_CONF="$DOMAIN_ROOT/config/agent.conf"
  if [ "$DRY" -eq 1 ]; then
    info "step 5/5 (dry-run): would set L1_DB=$L1_DB_PATH (+ L1_EXPECTED_USER_VERSION=${L1_VER:-<unresolved>}) on $DOMAIN_CONF"
  else
    info "step 5/5: L1_DB=$L1_DB_PATH -> $DOMAIN_CONF (shared-read access axis)"
    conf_upsert_atomic "$DOMAIN_CONF" L1_DB "$L1_DB_PATH" \
      || { printf 'promote-to-main: ERROR: step 5 (L1_DB) failed\n' >&2; exit 2; }
    if [ -n "$L1_VER" ]; then
      conf_upsert_atomic "$DOMAIN_CONF" L1_EXPECTED_USER_VERSION "$L1_VER" \
        || { printf 'promote-to-main: ERROR: step 5 (L1_EXPECTED_USER_VERSION) failed\n' >&2; exit 2; }
    else
      printf 'promote-to-main: WARN: L1_EXPECTED_USER_VERSION unresolved from parent agent.conf -- set it deliberately on %s\n' "$DOMAIN_CONF" >&2
    fi
  fi
else
  info "step 5/5: L1_DB wiring skipped (--l1-db not set; default off, dev-style domain)"
fi

info "done: 부하-promotion complete (parent=$ROOT is main; domain 부하=$DOMAIN_ROOT)"
exit 0
