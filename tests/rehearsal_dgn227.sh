#!/bin/bash
# rehearsal_dgn227.sh -- DGN-227 sandbox implementation rehearsal harness.
# Scripted assertions R1-R5; exits non-zero on any failure.
#
# SAFETY: every scenario runs against a throwaway FAKE_HOME (mktemp).
# No launchd unit is ever bootstrapped (DOGANY_LAUNCHD_CAPTURE seam),
# no real ~/.dogany state is touched, no network, no bot token.
# mint.sh is STUBBED (real minting needs token + venv build): the inline
# stub builds the instance skeleton from the shipped template, mirroring
# the mint steps this rehearsal depends on (AGENT.md copy, plist rename,
# plists.defer render, .instance.conf identity keys).
set -u

SANDBOX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
CURRENT=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "  ok: $*"; }
bad()  { FAIL=$((FAIL+1)); say "  FAIL[$CURRENT]: $*"; }
assert() { # assert <desc> <cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}

# ---------------------------------------------------------------------------
# mint stub -- skeleton of a minted instance (see header note)
# ---------------------------------------------------------------------------
mint_stub() { # mint_stub <root> <slug>
  local root="$1" slug="$2"
  local tpl="$SANDBOX/agents/.template"
  mkdir -p "$root/bridge" "$root/config" "$root/database" "$root/routines/lib" \
           "$root/files/handoff" "$root/files/_archive" "$root/scripts" \
           "$root/.claude/skills-bundle" "$root/.claude/skills" \
           "$root/.telegram_bot/logs" "$root/memories"
  cp "$tpl/AGENT.md" "$root/AGENT.md"
  cp "$tpl/routines/"*.sh "$root/routines/" 2>/dev/null || true
  cp "$tpl/routines/"*.py "$root/routines/" 2>/dev/null || true
  cp -R "$tpl/routines/lib/." "$root/routines/lib/" 2>/dev/null || true
  local p np
  for p in "$tpl/routines/"*.plist; do
    [ -e "$p" ] || continue
    np="$(basename "$p")"
    np="${np//telegram-agent/$slug}"
    cp "$p" "$root/routines/$np"
  done
  if [ -f "$tpl/routines/plists.defer" ]; then
    sed "s/telegram-agent/$slug/g" "$tpl/routines/plists.defer" > "$root/routines/plists.defer"
  fi
  printf '{\n  "model": "sonnet"\n}\n' > "$root/.claude/settings.json"
  echo "AGENT_LANG=ko" > "$root/config/agent.conf"
  printf 'LIFEKIT=pending\n' > "$root/config/lifekit.conf"
  {
    echo "DOGANY_AGENT_NAME=$slug"
    echo "DOGANY_AGENT_LABEL=$slug"
    echo "DOGANY_USER_LABEL=you"
    echo "DOGANY_AGENT_PREFIX=[agent]"
    echo "DOGANY_MINTED_AT=2026-07-18"
  } > "$root/.instance.conf"
  : > "$root/bridge/start.sh"
}
export SANDBOX
export -f mint_stub

# run_flow <fake_home> <script> -- run a driver snippet in a subshell with
# install.sh sourced as a library against the fake HOME.
run_flow() {
  local fake_home="$1"; shift
  local log="$fake_home/flow.log"
  (
    export HOME="$fake_home"
    export DOGANY_INSTALL_LIB=1
    export DOGANY_INSTALL_PINNED=1
    export DOGANY_LAUNCHD_CAPTURE="$fake_home/launchd.capture"
    cd "$SANDBOX"
    driver="$*"
    set --   # clear positional params: install.sh top-level parses "$@"
    # shellcheck disable=SC1091
    source "$SANDBOX/install.sh"
    trap - ERR
    DOGANY_LANG=ko
    DRY_RUN=0
    eval "$driver"
  ) >>"$log" 2>&1
}

hr() { printf -- '------------------------------------------------------------\n'; }
canon() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }

# ===========================================================================
# R1: MAIN mint via the new branch path
# ===========================================================================
CURRENT=R1
hr; say "R1: main mint (class record / marker / registry / defer respected)"
H1="$(mktemp -d /tmp/dgn227-r1.XXXXXX)"
R1_ROOT="$H1/.dogany/main"
mint_stub "$R1_ROOT" "dogany"
run_flow "$H1" '
  step_agent_class
  [ "$DOGANY_AGENT_CLASS" = "main" ] || { echo "class not main"; exit 10; }
  dgn227_postmint "'"$R1_ROOT"'"
  write_lite_marker "'"$R1_ROOT"'"
' || bad "flow exited non-zero (see $H1/flow.log)"

assert "class recorded main in .instance.conf" \
  grep -qx "DOGANY_AGENT_CLASS=main" "$R1_ROOT/.instance.conf"
assert "marker points to main root" \
  bash -c "[ \"\$(cat '$H1/.dogany/lite_instance')\" = \"\$(cd '$R1_ROOT' && pwd -P)\" ]"
assert "registry has exactly 1 entry" \
  bash -c "[ \"\$(wc -l < '$H1/.dogany/instances' | tr -d ' ')\" = 1 ]"
assert "registry entry is main" \
  grep -q "^main	" "$H1/.dogany/instances"
assert "role stamped (placeholder gone)" \
  bash -c "! grep -qF '(set at onboarding -- one prose line' '$R1_ROOT/AGENT.md'"
assert "Q6 excised from onboarding block" \
  bash -c "! grep -q '6\\. role' '$R1_ROOT/AGENT.md'"
assert "KIT=lifekit anchored (main)" \
  grep -qx "KIT=lifekit" "$R1_ROOT/config/agent.conf"
assert "lifekit.conf pending (main)" \
  grep -qx "LIFEKIT=pending" "$R1_ROOT/config/lifekit.conf"
assert "no generic-brief scheduling on main (no deferred load captured)" \
  bash -c "! grep -q -- '--deferred' '$H1/launchd.capture' 2>/dev/null"
run_flow "$H1" '
  plist_is_deferred "'"$R1_ROOT"'/routines" "com.telegram-skill-bot.dogany.generic-brief-morning.plist" || exit 11
  plist_is_deferred "'"$R1_ROOT"'/routines" "com.telegram-skill-bot.dogany.consolidate-0430.plist" && exit 12
  exit 0
' && ok "loader defer predicate: generic-brief deferred, core routine not" \
  || bad "loader defer predicate wrong (see $H1/flow.log)"

# ===========================================================================
# R2: BLANK DOMAIN standalone mint
# ===========================================================================
CURRENT=R2
hr; say "R2: blank domain (lifekit off / generic-brief scheduled / no migration keys)"
H2="$(mktemp -d /tmp/dgn227-r2.XXXXXX)"
R2_ROOT="$H2/.dogany/agents/zenith"
run_flow "$H2" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=zenith
  DOGANY_ROLE_PROSE="tax adviser -- personal tax filing and deduction strategy"
  step_agent_class
  [ "$INSTALL_ROOT" = "'"$R2_ROOT"'" ] || { echo "domain root not derived: $INSTALL_ROOT"; exit 10; }
  mint_stub "'"$R2_ROOT"'" zenith
  dgn227_postmint "'"$R2_ROOT"'"
  write_lite_marker "'"$R2_ROOT"'"
' || bad "flow exited non-zero (see $H2/flow.log)"

assert "class recorded domain" \
  grep -qx "DOGANY_AGENT_CLASS=domain" "$R2_ROOT/.instance.conf"
assert "LIFEKIT=off" \
  grep -qx "LIFEKIT=off" "$R2_ROOT/config/lifekit.conf"
assert "generic-brief explicit scheduling captured (deferred load call)" \
  grep -q "start --root $R2_ROOT --deferred" "$H2/launchd.capture"
assert "registry has exactly 1 entry (domain)" \
  bash -c "[ \"\$(wc -l < '$H2/.dogany/instances' | tr -d ' ')\" = 1 ] && grep -q '^domain	' '$H2/.dogany/instances'"
assert "marker points to domain root" \
  bash -c "[ \"\$(cat '$H2/.dogany/lite_instance')\" = \"\$(cd '$R2_ROOT' && pwd -P)\" ]"
assert "role stamped with blank prose" \
  grep -q "tax adviser" "$R2_ROOT/AGENT.md"
assert "no MIGRATION_PEER key (fresh => discriminator false)" \
  bash -c "! grep -q '^MIGRATION_PEER=' '$R2_ROOT/config/agent.conf'"
assert "no legacy HANDOFF_PEER_AG key" \
  bash -c "! grep -q '^HANDOFF_PEER_AG=' '$R2_ROOT/config/agent.conf'"
assert "no launchd bootstrap actually executed (capture-only seam)" \
  bash -c "! grep -q 'bootstrap' '$H2/launchd.capture'"

# ===========================================================================
# R3: domain-from-catalog with the dev pack (real pack_install chain)
# ===========================================================================
CURRENT=R3
hr; say "R3: domain-from-catalog (dev pack: ledger / preserve tags / reconcile)"
H3="$(mktemp -d /tmp/dgn227-r3.XXXXXX)"
R3_ROOT="$H3/.dogany/agents/dev"
run_flow "$H3" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=dev
  step_agent_class
  [ "$DOGANY_AGENT_SLUG" = "dev" ] || { echo "slug not derived from catalog id"; exit 10; }
  [ -n "$AGENT_ROLE_PROSE" ] || { echo "role prose not resolved from catalog"; exit 11; }
  mint_stub "'"$R3_ROOT"'" dev
  dgn227_postmint "'"$R3_ROOT"'"
  write_lite_marker "'"$R3_ROOT"'"
' || bad "flow exited non-zero (see $H3/flow.log)"

LEDGER="$R3_ROOT/config/packs/dev.files"
PRESERVE="$R3_ROOT/.claude/.dogany-preserve"
assert "pack ledger file written" test -f "$LEDGER"
assert "ledger records pack routine" \
  grep -qx "routines/dev-digest.sh" "$LEDGER"
assert "ledger records rendered plist (slug-derived name)" \
  grep -qx "routines/com.telegram-skill-bot.dev.dev-digest.plist" "$LEDGER"
assert "ledger records pack script" \
  grep -qx "scripts/secret-sweep.sh" "$LEDGER"
assert "ledger header carries pack_version" \
  grep -q "^# pack_version: 0.1.0" "$LEDGER"
assert "preserve entry tagged '# pack:dev' (routines zone)" \
  grep -q "^routines/dev-digest.sh  # pack:dev" "$PRESERVE"
assert "reconcile kept net-new entries in the SAME run" \
  grep -q "routines/com.telegram-skill-bot.dev.dev-digest.plist  # pack:dev" "$PRESERVE"
assert "AGENT.md fragment wrapped in BEGIN/END pair" \
  bash -c "grep -qF '<!-- DOGANY-PACK:dev:BEGIN -->' '$R3_ROOT/AGENT.md' && grep -qF '<!-- DOGANY-PACK:dev:END -->' '$R3_ROOT/AGENT.md'"
assert "DOGANY_PACKS list-form upsert (dev@0.1.0)" \
  grep -q "^DOGANY_PACKS=dev@0.1.0" "$R3_ROOT/.instance.conf"
assert "ledger-vs-disk consistency (every entry exists, H1-9)" \
  bash -c "rc=0; while read -r e; do [ -e \"$R3_ROOT/\$e\" ] || rc=1; done < <(grep -v '^#' '$LEDGER' | grep -v '^$'); exit \$rc"

# ===========================================================================
# R4: main-add flow onto R2's HOME (G2)
# ===========================================================================
CURRENT=R4
hr; say "R4: main-add onto R2 home (registry 2 / marker last / gated flip)"
R4_MAIN="$H2/.dogany/main"

# 4a. crash-order guard: finalize with NO minted main -> marker must not move
run_flow "$H2" '
  MAIN_ADD_FLOW=1
  EXISTING_DOMAIN_ROOT="'"$R2_ROOT"'"
  flow_main_add_finalize "'"$R4_MAIN"'" && exit 13
  exit 0
' && ok "finalize refuses before mint success (P28)" \
  || bad "finalize did not refuse on missing AGENT.md"
assert "marker STILL points at domain after refused finalize (marker-last)" \
  bash -c "[ \"\$(cat '$H2/.dogany/lite_instance')\" = \"\$(cd '$R2_ROOT' && pwd -P)\" ]"

# 4b. real flow: choice=2 -> relax ledger -> mint main -> finalize (gate FAIL)
mint_stub "$R4_MAIN" "dogany"
rm -f "$R4_MAIN/routines/lib/handoff-aggregate"   # break the edition gate
run_flow "$H2" '
  DOGANY_MAIN_ADD_CHOICE=2
  export DOGANY_GATE_LOADED_OVERRIDE=1
  check_lite_single_agent "'"$R4_MAIN"'" || { echo "single-agent check refused"; exit 10; }
  [ "$MAIN_ADD_FLOW" = "1" ] || { echo "main-add flow not armed"; exit 11; }
  step_agent_class
  dgn227_postmint "'"$R4_MAIN"'"
  flow_main_add_finalize "'"$R4_MAIN"'"
' || bad "main-add flow exited non-zero (see $H2/flow.log)"

assert "limit ledger persisted (DOGANY_MAX_AGENTS=2)" \
  grep -qx "DOGANY_MAX_AGENTS=2" "$H2/.dogany/config"
assert "registry grew to 2 entries" \
  bash -c "[ \"\$(wc -l < '$H2/.dogany/instances' | tr -d ' ')\" = 2 ]"
assert "registry holds domain + main" \
  bash -c "grep -q '^domain	' '$H2/.dogany/instances' && grep -q '^main	' '$H2/.dogany/instances'"
assert "marker re-pointed to main root LAST" \
  bash -c "[ \"\$(cat '$H2/.dogany/lite_instance')\" = \"\$(cd '$R4_MAIN' && pwd -P)\" ]"
assert "peer registered on main with display name" \
  bash -c "grep -q \"BRIEF_PEERS=.*\$(cd '$R2_ROOT' && pwd -P)|zenith\" '$R4_MAIN/config/agent.conf'"
assert "gate FAIL => domain stays standalone" \
  grep -qx "BRIEF_ROUTING=standalone" "$R2_ROOT/config/agent.conf"
assert "gate FAIL => no HANDOFF_PEER_MAIN written" \
  bash -c "! grep -q '^HANDOFF_PEER_MAIN=' '$R2_ROOT/config/agent.conf'"

# 4c. resolve the gate (aggregation edition present) -> re-run -> flips
cp "$SANDBOX/agents/.template/routines/lib/handoff-aggregate" \
   "$R4_MAIN/routines/lib/handoff-aggregate"
run_flow "$H2" '
  export DOGANY_GATE_LOADED_OVERRIDE=1
  MAIN_ADD_FLOW=1
  EXISTING_DOMAIN_ROOT="$(cd "'"$R2_ROOT"'" && pwd -P)"
  flow_main_add_finalize "'"$R4_MAIN"'"
' || bad "gate-pass re-run exited non-zero (see $H2/flow.log)"
assert "gate PASS => domain flips to submit" \
  grep -qx "BRIEF_ROUTING=submit" "$R2_ROOT/config/agent.conf"
assert "gate PASS => HANDOFF_PEER_MAIN recorded (briefing axis)" \
  grep -qx "HANDOFF_PEER_MAIN=$R4_MAIN" "$R2_ROOT/config/agent.conf"
assert "flip did NOT leak into migration family (still no MIGRATION_PEER)" \
  bash -c "! grep -q '^MIGRATION_PEER=' '$R2_ROOT/config/agent.conf'"

# ===========================================================================
# R5: --upgrade rehearsal (dev pack payload rename -> stale removal)
# ===========================================================================
CURRENT=R5
hr; say "R5: --upgrade (ledger diff removal / bootout stub / re-record)"
PK="$(mktemp -d /tmp/dgn227-r5-packs.XXXXXX)"
cp -R "$SANDBOX/packs/." "$PK/"
mv "$PK/dev/refdev/routines/dev-digest.sh" "$PK/dev/refdev/routines/dev-digest2.sh"
mv "$PK/dev/refdev/routines/com.telegram-skill-bot.refdev.dev-digest.plist" \
   "$PK/dev/refdev/routines/com.telegram-skill-bot.refdev.dev-digest2.plist"

R5_LOG="$H3/upgrade.log"
(
  export HOME="$H3"
  export DOGANY_LAUNCHD_CAPTURE="$H3/launchd.capture"
  bash "$SANDBOX/scripts/pack/pack_install.sh" dev "$R3_ROOT" \
    --pack dev --catalog "$PK/catalog.json" --upgrade --no-start --no-state
) >"$R5_LOG" 2>&1 || bad "upgrade run exited non-zero (see $R5_LOG)"

assert "stale routine removed per ledger diff" \
  bash -c "! test -e '$R3_ROOT/routines/dev-digest.sh'"
assert "stale plist removed per ledger diff" \
  bash -c "! test -e '$R3_ROOT/routines/com.telegram-skill-bot.dev.dev-digest.plist'"
assert "renamed payload files applied" \
  bash -c "test -f '$R3_ROOT/routines/dev-digest2.sh' && test -f '$R3_ROOT/routines/com.telegram-skill-bot.dev.dev-digest2.plist'"
assert "bootout captured for the stale plist (stub, no live launchctl)" \
  grep -q "bootout gui/UID/com.telegram-skill-bot.dev.dev-digest" "$H3/launchd.capture"
assert "ledger re-recorded (new set, old entry gone)" \
  bash -c "grep -qx 'routines/dev-digest2.sh' '$LEDGER' && ! grep -qx 'routines/dev-digest.sh' '$LEDGER'"
assert "NM3 backup of removed files exists" \
  bash -c "ls '$R3_ROOT/files/_archive/' | grep -q 'pack-upgrade-dev'"
assert "preserve reconcile shed the stale tagged entries" \
  bash -c "! grep -q '^routines/dev-digest.sh  # pack:dev' '$PRESERVE'"
assert "preserve keeps the new tagged entries" \
  grep -q "^routines/dev-digest2.sh  # pack:dev" "$PRESERVE"
assert "hand-written preserve header untouched" \
  bash -c "grep -q '^# .dogany-preserve' '$PRESERVE'"
assert "AGENT.md fragment pair present exactly once after upgrade" \
  bash -c "[ \"\$(grep -cF '<!-- DOGANY-PACK:dev:BEGIN -->' '$R3_ROOT/AGENT.md')\" = 1 ]"

# ===========================================================================
# R6: DGN-416 hardening regressions (MAJOR-1 / MAJOR-2 / MAJOR-3)
# ===========================================================================
CURRENT=R6
hr; say "R6: DGN-416 hardening (lifekit write-if-absent / main-add class guard / occupancy re-run)"

# --- MAJOR-1: LIFEKIT=on instance re-run -> lifekit state UNCHANGED ----------
H6="$(mktemp -d /tmp/dgn227-r6.XXXXXX)"
R6_DOM="$H6/.dogany/agents/lumen"
mint_stub "$R6_DOM" lumen
# simulate a live instance that opted into lifekit post-install (C2)
printf 'LIFEKIT=on\n' > "$R6_DOM/config/lifekit.conf"
run_flow "$H6" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=lumen
  DOGANY_ROLE_PROSE="finance coach"
  dgn227_postmint "'"$R6_DOM"'"
' || bad "MAJOR-1 domain re-run flow exited non-zero (see $H6/flow.log)"
assert "MAJOR-1: LIFEKIT=on preserved on domain reconfigure re-run" \
  grep -qx "LIFEKIT=on" "$R6_DOM/config/lifekit.conf"
assert "MAJOR-1: LIFEKIT not reverted to off" \
  bash -c "! grep -qx 'LIFEKIT=off' '$R6_DOM/config/lifekit.conf'"

# main path: a live LIFEKIT=on main must not be reverted to pending on re-run
R6_MAIN="$H6/.dogany/main"
mint_stub "$R6_MAIN" dogany
printf 'LIFEKIT=on\n' > "$R6_MAIN/config/lifekit.conf"
run_flow "$H6" '
  DOGANY_AGENT_CLASS=main
  AGENT_ROLE_PROSE="lifekit main"
  dgn227_postmint "'"$R6_MAIN"'"
' || bad "MAJOR-1 main re-run flow exited non-zero (see $H6/flow.log)"
assert "MAJOR-1: LIFEKIT=on preserved on main reconfigure re-run (not pending)" \
  grep -qx "LIFEKIT=on" "$R6_MAIN/config/lifekit.conf"

# fresh absent-key path still seeds off (write-if-absent still writes)
R6_FRESH="$H6/.dogany/agents/fresh6"
mint_stub "$R6_FRESH" fresh6
: > "$R6_FRESH/config/lifekit.conf"
run_flow "$H6" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=fresh6
  DOGANY_ROLE_PROSE="x"
  dgn227_postmint "'"$R6_FRESH"'"
' || bad "MAJOR-1 fresh flow exited non-zero (see $H6/flow.log)"
assert "MAJOR-1: absent LIFEKIT key still seeded off on fresh domain" \
  grep -qx "LIFEKIT=off" "$R6_FRESH/config/lifekit.conf"

# --- MAJOR-2: domain instance reaching main-add finalize -> ABORT ------------
# A domain selector minted a 2nd DOMAIN (class=domain) and reached finalize.
# The class guard must abort BEFORE registry_upsert "main" overwrites the row.
H6B="$(mktemp -d /tmp/dgn227-r6b.XXXXXX)"
R6B_DOM1="$H6B/.dogany/agents/alpha"    # the correct existing domain
R6B_WRONG="$H6B/.dogany/agents/beta"    # a 2nd domain mis-routed into main-add
mint_stub "$R6B_DOM1" alpha
mint_stub "$R6B_WRONG" beta
# both are class=domain
printf 'DOGANY_AGENT_CLASS=domain\n' >> "$R6B_DOM1/.instance.conf"
printf 'DOGANY_AGENT_CLASS=domain\n' >> "$R6B_WRONG/.instance.conf"
# seed the registry with the correct domain row for the wrong root
run_flow "$H6B" '
  registry_upsert domain "'"$R6B_WRONG"'"
'
BADREG="$H6B/.dogany/instances"
run_flow "$H6B" '
  MAIN_ADD_FLOW=1
  EXISTING_DOMAIN_ROOT="'"$R6B_DOM1"'"
  flow_main_add_finalize "'"$R6B_WRONG"'" && exit 20
  exit 0
' && ok "MAJOR-2: finalize aborts when target is class=domain (not main)" \
  || bad "MAJOR-2: finalize did NOT abort on a domain target (class guard dead)"
assert "MAJOR-2: registry row NOT overwritten to main (still domain)" \
  bash -c "grep -q '^domain	' '$BADREG' && ! grep -q '^main	' '$BADREG'"

# --- MAJOR-3: crash after mint before marker -> re-run converges -------------
# marker-last design: instance minted, marker still points at it, --root unset.
# step_agent_class must NOT hard-exit; it passes through (same canon root).
H6C="$(mktemp -d /tmp/dgn227-r6c.XXXXXX)"
R6C_ROOT="$H6C/.dogany/agents/vega"
mint_stub "$R6C_ROOT" vega
# the marker already records this canonical root (mint happened, marker written,
# but a later step crashed -> user re-runs install with no --root)
mkdir -p "$H6C/.dogany"
( cd "$R6C_ROOT" && pwd -P ) > "$H6C/.dogany/lite_instance"
run_flow "$H6C" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=vega
  DOGANY_ROLE_PROSE="re-run role"
  step_agent_class || exit 30
  [ "$INSTALL_ROOT" = "'"$R6C_ROOT"'" ] || { echo "root drifted: $INSTALL_ROOT"; exit 31; }
  exit 0
' && ok "MAJOR-3: occupied same-canon root passes through (no occupancy strand)" \
  || bad "MAJOR-3: step_agent_class stranded on same-root re-run (see $H6C/flow.log)"

# foreign occupied root, non-interactive/preset -> still hard-exits (case d)
H6D="$(mktemp -d /tmp/dgn227-r6d.XXXXXX)"
R6D_ROOT="$H6D/.dogany/agents/nova"
mint_stub "$R6D_ROOT" nova   # occupied, but NOT the marker/target instance
# step_agent_class hard-exits the (sub)shell on case (d); run_flow then returns
# non-zero -> that non-zero IS the pass signal here.
if run_flow "$H6D" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=nova
  DOGANY_ROLE_PROSE="collide"
  step_agent_class
  exit 0
'; then
  bad "MAJOR-3: foreign occupancy did not hard-exit under preset"
else
  ok "MAJOR-3: foreign occupied root under preset still hard-exits (case d preserved)"
fi

# marker-ABSENT crash: a first-install that crashed before the marker was
# written leaves an occupied root but NO marker.  A bare preset re-run (same
# slug, no --root) has no same-canon pass-through to rely on -> must hard-exit
# with a clear error (case d).  Recovery requires an explicit --root re-run.
# This documents the ACTUAL behavior -- do NOT assert strand-free auto-recovery.
H6E="$(mktemp -d /tmp/dgn227-r6e.XXXXXX)"
R6E_ROOT="$H6E/.dogany/agents/orbit"
mint_stub "$R6E_ROOT" orbit   # occupied, no marker written (crash-before-marker)
# marker file absent -> no lite_instance file
if run_flow "$H6E" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=orbit
  DOGANY_ROLE_PROSE="pre-marker crash"
  step_agent_class
  exit 0
'; then
  bad "MAJOR-3: marker-absent pre-marker-crash re-run did not hard-exit (should require --root)"
else
  ok "MAJOR-3: marker-absent occupied root hard-exits (recovery requires --root <occupied-root>)"
fi
# explicit --root re-run converges: ROOT_FORCED bypasses occupancy re-derivation (case a)
H6F="$(mktemp -d /tmp/dgn227-r6f.XXXXXX)"
R6F_ROOT="$H6F/.dogany/agents/orbit2"
mint_stub "$R6F_ROOT" orbit2   # occupied, no marker
run_flow "$H6F" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=orbit2
  DOGANY_ROLE_PROSE="explicit root recovery"
  INSTALL_ROOT="'"$R6F_ROOT"'"
  ROOT_FORCED=1
  step_agent_class || exit 40
  exit 0
' && ok "MAJOR-3: explicit --root re-run converges on marker-absent occupied root (case a)" \
  || bad "MAJOR-3: --root re-run failed on marker-absent occupied root (see $H6F/flow.log)"

# ===========================================================================
# R7: DGN-417 (MAJOR-4 re-mint keep-if-present / MAJOR-5 plists.defer subst)
# ===========================================================================
# These use the REAL mint.sh (--no-venv --force) so the actual .instance.conf
# heredoc + keep-if-present block and the step-3a plists.defer substitution are
# exercised -- the mint_stub above hand-writes .instance.conf and cannot cover
# the re-mint blind spot the grill flagged.
CURRENT=R7
hr; say "R7: DGN-417 (real mint.sh: class/pack keep-if-present + defer subst)"
H7="$(mktemp -d /tmp/dgn227-r7.XXXXXX)"
R7_ROOT="$H7/inst"
UPDATE_SH="$SANDBOX/update.sh"

# 7a. fresh real mint -- no CLASS/PACKS authored by mint.sh (install.sh/pack own
#     those); a fresh manifest must NOT carry spurious empty keys.
DOGANY_LAUNCHD_CAPTURE="$H7/cap" \
  bash "$SANDBOX/scripts/mint.sh" --root "$R7_ROOT" --name probe7 --no-venv --force \
  >"$H7/mint1.log" 2>&1 || bad "R7 fresh mint exited non-zero (see $H7/mint1.log)"
assert "MAJOR-4: fresh mint writes no empty DOGANY_AGENT_CLASS line" \
  bash -c "! grep -q '^DOGANY_AGENT_CLASS=$' '$R7_ROOT/.instance.conf'"
assert "MAJOR-4: fresh mint writes no empty DOGANY_PACKS line" \
  bash -c "! grep -q '^DOGANY_PACKS=$' '$R7_ROOT/.instance.conf'"

# 7b. simulate install.sh/pack_install having stamped class + pack record, then
#     RE-MINT (recover/reconfigure) and assert both survive the wholesale rewrite.
printf 'DOGANY_AGENT_CLASS=domain\n' >> "$R7_ROOT/.instance.conf"
printf 'DOGANY_PACKS=health-trainer@1.2.0\n' >> "$R7_ROOT/.instance.conf"
DOGANY_LAUNCHD_CAPTURE="$H7/cap" \
  bash "$SANDBOX/scripts/mint.sh" --root "$R7_ROOT" --name probe7 --no-venv --force \
  >"$H7/mint2.log" 2>&1 || bad "R7 re-mint exited non-zero (see $H7/mint2.log)"
assert "MAJOR-4: DOGANY_AGENT_CLASS=domain preserved across re-mint (not reset to main)" \
  grep -qx "DOGANY_AGENT_CLASS=domain" "$R7_ROOT/.instance.conf"
assert "MAJOR-4: DOGANY_PACKS preserved across re-mint (pack record not wiped)" \
  grep -qx "DOGANY_PACKS=health-trainer@1.2.0" "$R7_ROOT/.instance.conf"
assert "MAJOR-4: each preserved key appears exactly once (no duplication on re-mint)" \
  bash -c "[ \"\$(grep -c '^DOGANY_AGENT_CLASS=' '$R7_ROOT/.instance.conf')\" = 1 ] && [ \"\$(grep -c '^DOGANY_PACKS=' '$R7_ROOT/.instance.conf')\" = 1 ]"

# 7c. MAJOR-5 (mint side, step 3a): the real mint just ran -- plists.defer must
#     carry the agent name and no literal telegram-agent leftover, and its
#     entries must match the renamed plist filenames on disk.
DEFER7="$R7_ROOT/routines/plists.defer"
assert "MAJOR-5: mint 3a substituted defer -- no telegram-agent literal leftover" \
  bash -c "! grep -q 'telegram-agent' '$DEFER7'"
assert "MAJOR-5: defer entry carries agent name (com.telegram-skill-bot.probe7.*)" \
  grep -q "^com.telegram-skill-bot.probe7.generic-brief-morning.plist$" "$DEFER7"
assert "MAJOR-5: every non-comment defer entry matches a renamed plist on disk" \
  bash -c "rc=0; while read -r b; do [ -e \"$R7_ROOT/routines/\$b\" ] || rc=1; done < <(grep -v '^#' '$DEFER7' | grep -v '^\$'); exit \$rc"

# 7d. MAJOR-5 (update side): the update.sh rename block cannot be run end-to-end
#     in this harness (no library seam; full framework rsync), so guard the fix
#     statically -- update.sh MUST substitute plists.defer inside the rename
#     block, otherwise a re-vendored defer keeps literal telegram-agent entries
#     that a later defer-honoring loader bootstraps onto the live channel.
assert "MAJOR-5: update.sh substitutes routines/plists.defer (rename block guard)" \
  grep -q 'sed_inplace "\$INSTANCE/routines/plists.defer"' "$UPDATE_SH"
# and prove that exact substitution is faithful to mint 3a on a defer file that
# still carries literal telegram-agent (the post-rsync state update.sh sees):
DEFER_SIM="$H7/defer.sim"
cp "$SANDBOX/agents/.template/routines/plists.defer" "$DEFER_SIM"
( AGENT_NAME=probe7
  # exact substitution update.sh's block applies (LC_ALL=C sed, in place)
  LC_ALL=C sed "s/telegram-agent/$AGENT_NAME/g" "$DEFER_SIM" > "$DEFER_SIM.new" \
    && mv "$DEFER_SIM.new" "$DEFER_SIM" )
assert "MAJOR-5: update-side substitution yields no telegram-agent literal" \
  bash -c "! grep -q 'telegram-agent' '$DEFER_SIM'"
assert "MAJOR-5: update-side substitution renames the generic-brief basename" \
  grep -q "^com.telegram-skill-bot.probe7.generic-brief-retro.plist$" "$DEFER_SIM"

# ===========================================================================
# R8: DGN-418 pack lifecycle completion
#     B5 (bundled frozen knowledge snapshot) + B6 (net-new skill dir install)
#     + NM3 (checksum gate pass) + MINOR-5 (defer merge-append).
#     A self-contained knowledge pack is synthesized in a temp catalog so the
#     FULL pack_install chain (incl. STEP 7c gates G1-G4) runs end-to-end.
# ===========================================================================
CURRENT=R8
hr; say "R8: DGN-418 (B5 snapshot / B6 net-new skill / NM3 checksum / MINOR-5 defer merge)"

# --- fixture builders -------------------------------------------------------
# build_know_pack <packs_dir> -- write a warehouse pack 'kp' with a net-new
# consumer skill 'place-find', a bundled frozen snapshot warehouse 'testwh',
# a bundled plists.defer, and a checksums.sha over the payload.
build_know_pack() {
  local pd="$1"
  local pkg="$pd/kp" ref="$pd/kp/refkp"
  mkdir -p "$ref/skills/place-find" "$ref/scripts" "$ref/routines" \
           "$ref/knowledge/testwh/tools"
  # -- catalog --
  cat > "$pd/catalog.json" <<'JSON'
{ "version": 1, "packs": [
  { "id": "kp", "name_ko": "지식팩", "role_prose_ko": "knowledge pack",
    "package_dir": "kp", "status": "official", "pack_version": "1.0.0" } ] }
JSON
  # -- manifest: skills + knowledge_snapshot + scripts + plists + routines --
  cat > "$pkg/pack-manifest.json" <<'JSON'
{
  "name": "kp",
  "reference_slug": "refkp",
  "reference_root": "/opt/dogany/agents/refkp",
  "net_new_skills": ["place-find"],
  "categories": [
    {"category": "skills",            "required": true},
    {"category": "scripts",           "required": true},
    {"category": "knowledge_snapshot","required": true},
    {"category": "routines",          "required": false},
    {"category": "plists",            "required": false}
  ],
  "knowledge": {
    "warehouse": "testwh",
    "consumer_skills": { "place-find": ["places"] },
    "turns": [ {"type": "T1", "home": ".claude/skills-bundle/place-find/SKILL.md"} ],
    "smoke_item": "places/dummy",
    "smoke_args": ""
  }
}
JSON
  # -- net-new consumer skill: SKILL.md (with consumption block + a mint token
  #    to exercise the render pipeline) + a second file (multi-file dir) --
  cat > "$ref/skills/place-find/SKILL.md" <<'MD'
---
name: place-find
description: find places.
---
# place-find
root token: __PROJECT_ROOT__

## knowledge warehouse consumption
when finding a place, consult the warehouse FIRST:
warehouse root: `knowledge/testwh`.
relevant domains: places (via registry.yaml).
procedure: refract deterministically, speak refracted result only.
MD
  cat > "$ref/skills/place-find/helper.py" <<'PY'
#!/usr/bin/env python3
# net-new skill helper (multi-file dir) -- root token __PROJECT_ROOT__
print("place-find helper")
PY
  # -- bundled frozen snapshot warehouse (B5) --
  cat > "$ref/knowledge/testwh/.snapshot-pin" <<'PIN'
warehouse: testwh
release: 1.0.0
snapshot_date: 2026-07-18
source: bundled-frozen
PIN
  echo "# testwh registry" > "$ref/knowledge/testwh/registry.yaml"
  echo "# testwh readme" > "$ref/knowledge/testwh/README.md"
  cat > "$ref/knowledge/testwh/tools/refract_cli.py" <<'PY'
#!/usr/bin/env python3
import sys
# deterministic zero-model stub -- exit 0 for the smoke gate
print("refracted:", sys.argv[1] if len(sys.argv) > 1 else "")
PY
  # -- knowledge-snapshot.sh: idempotent rsync of the injected SOURCE into
  #    <root>/knowledge/, mirroring the publisher script contract (B5) --
  cat > "$ref/scripts/knowledge-snapshot.sh" <<'SNAP'
#!/bin/bash
set -euo pipefail
ROOT="$1"; SRC="${2:-}"
[ -n "$SRC" ] || { echo "no source"; exit 1; }
WH="$(basename "$SRC")"
mkdir -p "$ROOT/knowledge/$WH"
rsync -a --delete \
  --exclude 'instance/' --exclude 'GAPS-instance.md' \
  "$SRC/" "$ROOT/knowledge/$WH/"
echo "snapshot: delivered $WH from $SRC"
SNAP
  chmod +x "$ref/scripts/knowledge-snapshot.sh"
  # -- bundled plists.defer (MINOR-5): a pack-owned deferred unit --
  cat > "$ref/routines/plists.defer" <<'DEFER'
# pack-owned deferred units
com.telegram-skill-bot.refkp.kp-coach-brief.plist
DEFER
  cat > "$ref/routines/com.telegram-skill-bot.refkp.kp-coach-brief.plist" <<'PL'
<?xml version="1.0"?><plist><dict><key>Label</key>
<string>com.telegram-skill-bot.refkp.kp-coach-brief</string></dict></plist>
PL
  # -- checksums.sha over EVERY payload file (NM3, B4-5). sha256, format
  #    '<hex>  <relpath>' relative to package_dir --
  ( cd "$pkg" && find . -type f ! -name checksums.sha | sed 's|^\./||' | LC_ALL=C sort \
    | while IFS= read -r rel; do
        printf '%s  %s\n' \
          "$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$rel")" \
          "$rel"
      done ) > "$pkg/checksums.sha"
}

# make_know_root <root> <slug> -- mint_stub + the pieces a knowledge pack needs
# but the stub omits: the framework dogany-memory-search skill (G3 canonical
# line) and a KNOWLEDGE-WIRING-POINTER block appended to AGENT.md by the pack
# is NOT here -- the pack has no AGENT.md.add, so we inject the G2 pointer +
# G3 conf key the way the pack fragments would (this fixture pack omits the
# fragment categories to keep the payload minimal; G2/G3 wiring is seeded here
# to isolate the B5/B6/NM3/MINOR-5 code paths under test).
make_know_root() {
  local root="$1" slug="$2"
  mint_stub "$root" "$slug"
  mkdir -p "$root/.claude/skills/dogany-memory-search"
  cp "$SANDBOX/skills/dogany-memory-search/SKILL.md" \
     "$root/.claude/skills/dogany-memory-search/SKILL.md"
  # G2 pointer marker + names the warehouse
  {
    echo ""
    echo "<!-- KNOWLEDGE-WIRING-POINTER -->"
    echo "Domain knowledge lives at \`knowledge/testwh\`."
  } >> "$root/AGENT.md"
  # G3 conf key
  echo "KNOWLEDGE_WAREHOUSE=testwh" >> "$root/config/agent.conf"
}

H8="$(mktemp -d /tmp/dgn227-r8.XXXXXX)"
PD8="$(mktemp -d /tmp/dgn227-r8-packs.XXXXXX)"
build_know_pack "$PD8"
R8_ROOT="$H8/.dogany/agents/kp"
make_know_root "$R8_ROOT" kp
# seed a framework defer so MINOR-5 merge (not clobber) is observable
cat > "$R8_ROOT/routines/plists.defer" <<'FWDEFER'
# plists.defer -- framework generic-brief units (DGN-227 E1-1)
com.telegram-skill-bot.kp.generic-brief-morning.plist
com.telegram-skill-bot.kp.generic-brief-retro.plist
com.telegram-skill-bot.kp.generic-brief-weekly.plist
FWDEFER

R8_LOG="$H8/install.log"
( export HOME="$H8"
  export DOGANY_LAUNCHD_CAPTURE="$H8/launchd.capture"
  bash "$SANDBOX/scripts/pack/pack_install.sh" kp "$R8_ROOT" \
    --pack kp --catalog "$PD8/catalog.json" --no-start --no-state
) >"$R8_LOG" 2>&1 || bad "R8 install exited non-zero (see $R8_LOG)"

R8_LEDGER="$R8_ROOT/config/packs/kp.files"
R8_PRESERVE="$R8_ROOT/.claude/.dogany-preserve"
R8_DEFER="$R8_ROOT/routines/plists.defer"

# -- B5: bundled frozen warehouse landed in the instance --
assert "B5: warehouse knowledge/testwh/ delivered to instance" \
  test -d "$R8_ROOT/knowledge/testwh"
assert "B5: .snapshot-pin present with release pin" \
  grep -q '^release: 1.0.0' "$R8_ROOT/knowledge/testwh/.snapshot-pin"
assert "B5: pin source records bundled-frozen channel" \
  grep -q '^source: bundled-frozen' "$R8_ROOT/knowledge/testwh/.snapshot-pin"
assert "B5: install log names the bundled frozen channel source" \
  grep -q 'bundled frozen channel, B5' "$R8_LOG"
assert "B5: warehouse dir recorded in ledger" \
  grep -qx "knowledge/testwh/" "$R8_LEDGER"

# -- B6: net-new skill directory installed (multi-file) + D1 tag --
assert "B6: net-new skill dir present on disk (SKILL.md)" \
  test -f "$R8_ROOT/.claude/skills-bundle/place-find/SKILL.md"
assert "B6: net-new skill second file present (multi-file dir)" \
  test -f "$R8_ROOT/.claude/skills-bundle/place-find/helper.py"
assert "B6: render pipeline substituted mint token (no residue) in SKILL.md" \
  bash -c "grep -q \"$R8_ROOT\" '$R8_ROOT/.claude/skills-bundle/place-find/SKILL.md' && ! grep -q '__PROJECT_ROOT__' '$R8_ROOT/.claude/skills-bundle/place-find/SKILL.md'"
assert "B6: render pipeline substituted token in the second file too" \
  bash -c "! grep -q '__PROJECT_ROOT__' '$R8_ROOT/.claude/skills-bundle/place-find/helper.py'"
assert "B6: net-new skill symlinked into .claude/skills/" \
  bash -c "[ -L '$R8_ROOT/.claude/skills/place-find' ]"
assert "D1: net-new SKILL.md preserve-registered pack-owned (# pack:kp)" \
  grep -q "^.claude/skills-bundle/place-find/SKILL.md  # pack:kp" "$R8_PRESERVE"
assert "D1: net-new second file preserve-registered pack-owned" \
  grep -q "^.claude/skills-bundle/place-find/helper.py  # pack:kp" "$R8_PRESERVE"
assert "D1: net-new files recorded in the install ledger" \
  bash -c "grep -qx '.claude/skills-bundle/place-find/SKILL.md' '$R8_LEDGER' && grep -qx '.claude/skills-bundle/place-find/helper.py' '$R8_LEDGER'"

# -- NM3: checksum gate passed (loud PASS in log) --
assert "NM3: checksum verification passed (log)" \
  grep -q 'NM3: checksum verification passed' "$R8_LOG"
assert "NM3: verified all payload files (NM3OK)" \
  grep -q 'NM3OK verified' "$R8_LOG"

# -- MINOR-5: defer merge-append (framework entries preserved + pack merged) --
assert "MINOR-5: framework generic-brief entries preserved in merged defer" \
  bash -c "grep -qx 'com.telegram-skill-bot.kp.generic-brief-morning.plist' '$R8_DEFER' && grep -qx 'com.telegram-skill-bot.kp.generic-brief-weekly.plist' '$R8_DEFER'"
assert "MINOR-5: pack defer entry merged in (slug-substituted)" \
  grep -qx "com.telegram-skill-bot.kp.kp-coach-brief.plist" "$R8_DEFER"
assert "MINOR-5: no literal telegram-agent / reference slug leftover in merged defer" \
  bash -c "! grep -q 'telegram-agent' '$R8_DEFER' && ! grep -q 'refkp' '$R8_DEFER'"
assert "MINOR-5: no duplicate entries in merged defer" \
  bash -c "[ \"\$(grep -v '^#' '$R8_DEFER' | grep -v '^\$' | sort | uniq -d | wc -l | tr -d ' ')\" = 0 ]"

# -- MINOR-5 idempotency: a second install must not re-append pack entries --
( export HOME="$H8"; export DOGANY_LAUNCHD_CAPTURE="$H8/launchd.capture"
  bash "$SANDBOX/scripts/pack/pack_install.sh" kp "$R8_ROOT" \
    --pack kp --catalog "$PD8/catalog.json" --no-start --no-state
) >"$H8/install2.log" 2>&1 || bad "R8 re-install exited non-zero (see $H8/install2.log)"
assert "MINOR-5: re-install does not duplicate the pack defer entry (idempotent)" \
  bash -c "[ \"\$(grep -cx 'com.telegram-skill-bot.kp.kp-coach-brief.plist' '$R8_DEFER')\" = 1 ]"

# ===========================================================================
# R9: NM3 checksum-mismatch payload -> install LOUD-FAILs (no warn-continue).
# ===========================================================================
CURRENT=R9
hr; say "R9: NM3 checksum-mismatch payload => install loud-FAILs"
H9="$(mktemp -d /tmp/dgn227-r9.XXXXXX)"
PD9="$(mktemp -d /tmp/dgn227-r9-packs.XXXXXX)"
build_know_pack "$PD9"
# tamper a payload file AFTER checksums.sha was generated -> mismatch
echo "TAMPERED" >> "$PD9/kp/refkp/skills/place-find/helper.py"
R9_ROOT="$H9/.dogany/agents/kp"
make_know_root "$R9_ROOT" kp

R9_LOG="$H9/install.log"
if ( export HOME="$H9"; export DOGANY_LAUNCHD_CAPTURE="$H9/launchd.capture"
     bash "$SANDBOX/scripts/pack/pack_install.sh" kp "$R9_ROOT" \
       --pack kp --catalog "$PD9/catalog.json" --no-start --no-state
   ) >"$R9_LOG" 2>&1; then
  bad "NM3: mismatch payload install DID NOT fail (gate is warn-continue?)"
else
  ok "NM3: mismatch payload install exits non-zero (loud-FAIL)"
fi
assert "NM3: FATAL checksum-verification line in log" \
  grep -q 'NM3: payload checksum verification FAILED' "$R9_LOG"
assert "NM3: the tampered file is named as a MISMATCH" \
  grep -q 'NM3FAIL MISMATCH: refkp/skills/place-find/helper.py' "$R9_LOG"
assert "NM3: gate aborted BEFORE STEP 2 copy (payload never applied)" \
  bash -c "! test -f '$R9_ROOT/.claude/skills-bundle/place-find/SKILL.md'"

# ===========================================================================
# DGN-419 Part A: B4 PUBLISH PIPELINE + 3 refinement gates + catalog en/status.
# Every scenario builds a SYNTHETIC fixture live-agent root under /tmp with
# FAKE personal data / fake conversation memory / fake persona fields. No live
# agent data is EVER touched. Data-boundary: fixtures live at /tmp/dgn227-r1[0-3]*.
# ===========================================================================

# build_publish_source <src_root> -- a synthetic "validated live agent" with:
#  - AGENT.md carrying a persona/Identity section (FAKE name/emoji/tone) that
#    the persona-blank gate must NOT extract, plus Role/Workflows domain sections
#  - fake personal data: memories/, USER.md, .telegram_bot/.env, a real *.db,
#    a conversation transcripts/ dir  (all must stay OUT of the payload)
#  - a clean net-new skill 'place-find' (structural token only, no persona token)
#  - a knowledge warehouse with a RELEASE-pinned .snapshot-pin + instance/ accum
build_publish_source() {
  local src="$1"
  mkdir -p "$src/.claude/skills-bundle/place-find" \
           "$src/knowledge/kimfake/tools" "$src/knowledge/kimfake/instance" \
           "$src/routines" "$src/scripts" "$src/memories" "$src/.telegram_bot" \
           "$src/transcripts" "$src/database"
  cat > "$src/AGENT.md" <<'MD'
# AGENT

## Identity
- Name: Fakey McTrainer
- Emoji: 🦾
- Tone: harsh
- Form of address: 형님

## Role
- Health trainer -- workout/diet/nutrition coach (long-term health).

## Workflows
### Coaching
- Push hard, track macros, respect the safety cap.
MD
  # -- FAKE personal data / conversation memory (must be excluded) --
  echo "FAKE_MEMORY former weight 82kg, injury history private" > "$src/memories/mem-2026-07.md"
  echo "USER private profile: real name, phone, address" > "$src/USER.md"
  echo "BOT_TOKEN=fake-secret-abcd" > "$src/.telegram_bot/.env"
  echo "FAKE conversation transcript line" > "$src/transcripts/2026-07-18.txt"
  printf 'SQLITEFAKE' > "$src/database/lifekit.db"
  # -- clean net-new skill (structural token OK, no persona token) --
  cat > "$src/.claude/skills-bundle/place-find/SKILL.md" <<'SK'
---
name: place-find
description: find places.
---
# place-find
root token: __PROJECT_ROOT__ (structural -- allowed)

## knowledge warehouse consumption
warehouse root: `knowledge/kimfake`.
SK
  cat > "$src/.claude/skills-bundle/place-find/helper.py" <<'PY'
#!/usr/bin/env python3
# net-new helper, structural token __PROJECT_ROOT__ only
print("place-find helper")
PY
  # -- routine (domain) --
  cat > "$src/routines/coach-brief.sh" <<'RT'
#!/bin/bash
echo "coach brief section"
RT
  # -- knowledge-snapshot.sh (STEP 6 install runs this; B5 publisher contract) --
  cat > "$src/scripts/knowledge-snapshot.sh" <<'SNAP'
#!/bin/bash
set -euo pipefail
ROOT="$1"; SRC="${2:-}"
[ -n "$SRC" ] || { echo "no source"; exit 1; }
WH="$(basename "$SRC")"
mkdir -p "$ROOT/knowledge/$WH"
rsync -a --delete \
  --exclude 'instance/' --exclude 'GAPS-instance.md' \
  "$SRC/" "$ROOT/knowledge/$WH/"
echo "snapshot: delivered $WH from $SRC"
SNAP
  chmod +x "$src/scripts/knowledge-snapshot.sh"
  # -- knowledge warehouse: release-pinned + instance accumulation --
  cat > "$src/knowledge/kimfake/.snapshot-pin" <<'PIN'
warehouse: kimfake
release: 2.3.0
snapshot_date: 2026-07-01
PIN
  echo "# kimfake registry" > "$src/knowledge/kimfake/registry.yaml"
  echo "# kimfake readme" > "$src/knowledge/kimfake/README.md"
  cat > "$src/knowledge/kimfake/tools/refract_cli.py" <<'PY'
#!/usr/bin/env python3
import sys
print("refracted:", sys.argv[1] if len(sys.argv) > 1 else "")
PY
  # instance accumulation (must NOT ship in the frozen snapshot)
  echo "FAKE instance-private touched set" > "$src/knowledge/kimfake/instance/touched-set.yaml"
  echo "FAKE instance gaps" > "$src/knowledge/kimfake/GAPS-instance.md"
}

# write_publish_manifest_and_fields <manifest_out> <fields_out> -- an
# install-compatible manifest (R8 shape) + catalog en/ko fields fragment.
write_publish_manifest_and_fields() {
  local man="$1" fields="$2"
  cat > "$man" <<'JSON'
{
  "name": "hp",
  "reference_slug": "refhp",
  "reference_root": "/opt/dogany/agents/refhp",
  "net_new_skills": ["place-find"],
  "categories": [
    {"category": "skills",            "required": true},
    {"category": "scripts",           "required": true},
    {"category": "knowledge_snapshot","required": true},
    {"category": "routines",          "required": false}
  ],
  "knowledge": {
    "warehouse": "kimfake",
    "consumer_skills": { "place-find": ["places"] },
    "turns": [ {"type": "T1", "home": ".claude/skills-bundle/place-find/SKILL.md"} ],
    "smoke_item": "places/dummy",
    "smoke_args": ""
  }
}
JSON
  cat > "$fields" <<'JSON'
{
  "name_ko": "가짜 헬스팩",
  "name_en": "Fake Health Pack",
  "tagline_ko": "운동/식단 코칭",
  "tagline_en": "Workout and diet coaching",
  "role_prose_ko": "헬스트레이너 -- 운동/식단 코칭",
  "role_prose_en": "Health trainer -- workout and diet coaching"
}
JSON
}

# ===========================================================================
# R10: publish pipeline (clean synthetic source) => gates PASS, payload has
#      ZERO injected personal-data markers, persona blanked, knowledge pinned,
#      checksums.sha ROUND-TRIPS through DGN-418 install NM3 gate (green install)
# ===========================================================================
CURRENT=R10
hr; say "R10: publish clean synthetic source => gates pass + install round-trip"
H10="$(mktemp -d /tmp/dgn227-r10.XXXXXX)"
SRC10="$H10/src"; PD10="$H10/packs"
mkdir -p "$PD10"
build_publish_source "$SRC10"
MAN10="$H10/manifest.json"; FLD10="$H10/fields.json"
write_publish_manifest_and_fields "$MAN10" "$FLD10"
echo '{"version":1,"packs":[]}' > "$PD10/catalog.json"

PUB10_LOG="$H10/publish.log"
bash "$SANDBOX/scripts/pack/pack_publish.sh" \
  --source-root "$SRC10" --pack-id hp --pack-version 0.1.0 \
  --reference-slug refhp --packs-dir "$PD10" \
  --manifest-in "$MAN10" --catalog-fields-in "$FLD10" \
  --knowledge-warehouse kimfake \
  --section "## Role" --section "## Workflows" \
  --skill place-find --routine coach-brief.sh \
  --script knowledge-snapshot.sh \
  --changelog-note "first publish (synthetic)" \
  >"$PUB10_LOG" 2>&1 || bad "R10 publish exited non-zero (see $PUB10_LOG)"

PKG10="$PD10/hp"; REF10="$PKG10/refhp"

# -- gates all reported PASS --
assert "R10: gate (a) personal-data removal PASS logged" \
  grep -q 'GATE (a) personal-data/conversation-memory removal: PASS' "$PUB10_LOG"
assert "R10: gate (b) persona-token residue PASS logged" \
  grep -q 'GATE (b) persona-token residue: PASS' "$PUB10_LOG"
assert "R10: gate (c) knowledge release-pin PASS logged" \
  grep -q 'GATE (c) knowledge release-pin: PASS' "$PUB10_LOG"

# -- ZERO injected personal-data markers survive in the payload --
assert "R10: no FAKE_MEMORY marker anywhere in payload" \
  bash -c "! grep -rq 'FAKE_MEMORY' '$PKG10'"
assert "R10: no BOT_TOKEN / .env in payload" \
  bash -c "! grep -rq 'BOT_TOKEN' '$PKG10' && ! find '$PKG10' -name '.env' | grep -q ."
assert "R10: no memories/ or USER.md in payload" \
  bash -c "! find '$PKG10' -name 'mem-*' | grep -q . && ! find '$PKG10' -name 'USER.md' | grep -q ."
assert "R10: no transcripts/ or *.db in payload" \
  bash -c "! find '$PKG10' -type d -name transcripts | grep -q . && ! find '$PKG10' -name '*.db' | grep -q ."
assert "R10: knowledge instance-accumulation excluded from frozen snapshot" \
  bash -c "! find '$PKG10' -path '*instance*' | grep -q . && ! find '$PKG10' -name 'GAPS-instance.md' | grep -q ."

# -- persona fields BLANKED: AGENT.md.add carries Role/Workflows, NOT Identity --
assert "R10: AGENT.md.add has the Role domain section" \
  grep -q 'Health trainer' "$REF10/AGENT.md.add"
assert "R10: AGENT.md.add does NOT carry persona Identity (name blanked)" \
  bash -c "! grep -q 'Fakey McTrainer' '$REF10/AGENT.md.add' && ! grep -q 'Emoji:' '$REF10/AGENT.md.add'"
assert "R10: no persona render token in any payload file" \
  bash -c "! grep -rqE '__AGENT_LABEL__|__USER_LABEL__' '$PKG10'"

# -- knowledge pin present + release-pinned (concrete release, not floating) --
assert "R10: frozen snapshot .snapshot-pin present with concrete release" \
  grep -q '^release: 2.3.0' "$REF10/knowledge/kimfake/.snapshot-pin"

# -- .source-sync provenance + pack CHANGELOG present --
assert "R10: .source-sync provenance baseline written" \
  bash -c "test -f '$PKG10/.source-sync' && grep -q 'pack_version: 0.1.0' '$PKG10/.source-sync'"
assert "R10: pack CHANGELOG.md written with version entry" \
  bash -c "test -f '$PKG10/CHANGELOG.md' && grep -q '## 0.1.0' '$PKG10/CHANGELOG.md'"

# -- checksums.sha format: '<64hex>  <relpath>' two-space sep (DGN-418 NM3) --
assert "R10: checksums.sha lines match '<sha256hex>  <relpath>' (two-space)" \
  bash -c "grep -Eq '^[0-9a-f]{64}  [^ ].*' '$PKG10/checksums.sha'"
assert "R10: checksums.sha does NOT list itself" \
  bash -c "! grep -q 'checksums.sha' '$PKG10/checksums.sha'"
assert "R10: checksums.sha does NOT list .source-sync (publish provenance, not payload)" \
  bash -c "! grep -q '.source-sync' '$PKG10/checksums.sha'"

# -- ROUND-TRIP: feed the published pack straight into DGN-418's install NM3
#    gate. Green install proves the publish-side checksums.sha is verified by
#    the exact install-side gate (closes DGN-418 OQ3). --
R10_ROOT="$H10/.dogany/agents/hp"
make_know_root "$R10_ROOT" hp
# make_know_root wires warehouse 'testwh'; this pack uses 'kimfake' -> re-wire
sed -i.bak 's/testwh/kimfake/g' "$R10_ROOT/config/agent.conf" "$R10_ROOT/AGENT.md"
rm -f "$R10_ROOT/config/agent.conf.bak" "$R10_ROOT/AGENT.md.bak"

INST10_LOG="$H10/install.log"
( export HOME="$H10"; export DOGANY_LAUNCHD_CAPTURE="$H10/launchd.capture"
  bash "$SANDBOX/scripts/pack/pack_install.sh" hp "$R10_ROOT" \
    --pack hp --catalog "$PD10/catalog.json" --no-start --no-state
) >"$INST10_LOG" 2>&1 || bad "R10 install of published pack exited non-zero (see $INST10_LOG)"

assert "R10 round-trip: published checksums.sha verified by DGN-418 NM3 gate" \
  grep -q 'NM3: checksum verification passed' "$INST10_LOG"
assert "R10 round-trip: NM3OK counted all payload files" \
  grep -q 'NM3OK verified' "$INST10_LOG"
assert "R10 round-trip: net-new skill installed from published payload" \
  test -f "$R10_ROOT/.claude/skills-bundle/place-find/SKILL.md"
assert "R10 round-trip: frozen warehouse delivered from published payload" \
  test -d "$R10_ROOT/knowledge/kimfake"

# ===========================================================================
# R11: tamper a PUBLISHED payload file after checksums.sha -> install NM3 FAILs
# ===========================================================================
CURRENT=R11
hr; say "R11: tampered published payload => DGN-418 install NM3 loud-FAILs"
H11="$(mktemp -d /tmp/dgn227-r11.XXXXXX)"
SRC11="$H11/src"; PD11="$H11/packs"; mkdir -p "$PD11"
build_publish_source "$SRC11"
MAN11="$H11/manifest.json"; FLD11="$H11/fields.json"
write_publish_manifest_and_fields "$MAN11" "$FLD11"
echo '{"version":1,"packs":[]}' > "$PD11/catalog.json"
bash "$SANDBOX/scripts/pack/pack_publish.sh" \
  --source-root "$SRC11" --pack-id hp --pack-version 0.1.0 \
  --reference-slug refhp --packs-dir "$PD11" \
  --manifest-in "$MAN11" --catalog-fields-in "$FLD11" \
  --knowledge-warehouse kimfake --skill place-find \
  --script knowledge-snapshot.sh \
  >"$H11/publish.log" 2>&1 || bad "R11 publish exited non-zero"
# tamper AFTER publish (checksums.sha already generated)
echo "TAMPERED" >> "$PD11/hp/refhp/skills/place-find/helper.py"
R11_ROOT="$H11/.dogany/agents/hp"
make_know_root "$R11_ROOT" hp
sed -i.bak 's/testwh/kimfake/g' "$R11_ROOT/config/agent.conf" "$R11_ROOT/AGENT.md"
rm -f "$R11_ROOT/config/agent.conf.bak" "$R11_ROOT/AGENT.md.bak"
R11_LOG="$H11/install.log"
if ( export HOME="$H11"; export DOGANY_LAUNCHD_CAPTURE="$H11/launchd.capture"
     bash "$SANDBOX/scripts/pack/pack_install.sh" hp "$R11_ROOT" \
       --pack hp --catalog "$PD11/catalog.json" --no-start --no-state
   ) >"$R11_LOG" 2>&1; then
  bad "R11: tampered published payload install DID NOT fail"
else
  ok "R11: tampered published payload install loud-FAILs (NM3 gate)"
fi
assert "R11: NM3 names the tampered published file as a MISMATCH" \
  grep -q 'NM3FAIL MISMATCH: refhp/skills/place-find/helper.py' "$R11_LOG"

# ===========================================================================
# R12: publish GATE failure modes (each loud-FAIL, publish aborts, no catalog)
# ===========================================================================
CURRENT=R12
hr; say "R12: publish gate failure modes (personal-data / persona / floating pin)"
H12="$(mktemp -d /tmp/dgn227-r12.XXXXXX)"

# (a) source skill carries personal data (USER.md) -> gate (a) FAIL
SRC12A="$H12/srcA"; PD12A="$H12/packsA"; mkdir -p "$PD12A"
build_publish_source "$SRC12A"
echo "FAKE personal profile leaked into skill" > "$SRC12A/.claude/skills-bundle/place-find/USER.md"
echo '{"version":1,"packs":[]}' > "$PD12A/catalog.json"
if bash "$SANDBOX/scripts/pack/pack_publish.sh" \
     --source-root "$SRC12A" --pack-id hp --pack-version 0.1.0 \
     --reference-slug refhp --packs-dir "$PD12A" --skill place-find \
     >"$H12/pubA.log" 2>&1; then
  bad "R12(a): publish with personal data in skill DID NOT fail"
else
  ok "R12(a): personal-data gate loud-FAILs the publish"
fi
assert "R12(a): gate (a) FATAL line present" \
  grep -q 'GATE (a): payload carries personal data' "$H12/pubA.log"
assert "R12(a): catalog NOT upserted on gate failure" \
  bash -c "[ \"\$(python3 -c \"import json;print(len(json.load(open('$PD12A/catalog.json'))['packs']))\")\" = 0 ]"

# (b) source skill carries a persona render token -> gate (b) FAIL
SRC12B="$H12/srcB"; PD12B="$H12/packsB"; mkdir -p "$PD12B"
build_publish_source "$SRC12B"
echo "greeting for __AGENT_LABEL__" >> "$SRC12B/.claude/skills-bundle/place-find/SKILL.md"
echo '{"version":1,"packs":[]}' > "$PD12B/catalog.json"
if bash "$SANDBOX/scripts/pack/pack_publish.sh" \
     --source-root "$SRC12B" --pack-id hp --pack-version 0.1.0 \
     --reference-slug refhp --packs-dir "$PD12B" --skill place-find \
     >"$H12/pubB.log" 2>&1; then
  bad "R12(b): publish with persona token DID NOT fail"
else
  ok "R12(b): persona-token gate loud-FAILs the publish"
fi
assert "R12(b): gate (b) FATAL line present" \
  grep -q 'GATE (b): persona render token' "$H12/pubB.log"

# (c) source warehouse pin is FLOATING (HEAD) -> gate (c) FAIL
SRC12C="$H12/srcC"; PD12C="$H12/packsC"; mkdir -p "$PD12C"
build_publish_source "$SRC12C"
cat > "$SRC12C/knowledge/kimfake/.snapshot-pin" <<'PIN'
warehouse: kimfake
release: HEAD
PIN
echo '{"version":1,"packs":[]}' > "$PD12C/catalog.json"
if bash "$SANDBOX/scripts/pack/pack_publish.sh" \
     --source-root "$SRC12C" --pack-id hp --pack-version 0.1.0 \
     --reference-slug refhp --packs-dir "$PD12C" --knowledge-warehouse kimfake \
     >"$H12/pubC.log" 2>&1; then
  bad "R12(c): publish with floating pin DID NOT fail"
else
  ok "R12(c): floating-pin gate loud-FAILs the publish"
fi
assert "R12(c): gate (c) FLOATING ref FATAL line present" \
  grep -q "GATE (c): knowledge snapshot pinned to a FLOATING ref" "$H12/pubC.log"

# (d) publish attempt to extract a PERSONA section is refused (blanking gate)
SRC12D="$H12/srcD"; PD12D="$H12/packsD"; mkdir -p "$PD12D"
build_publish_source "$SRC12D"
echo '{"version":1,"packs":[]}' > "$PD12D/catalog.json"
if bash "$SANDBOX/scripts/pack/pack_publish.sh" \
     --source-root "$SRC12D" --pack-id hp --pack-version 0.1.0 \
     --reference-slug refhp --packs-dir "$PD12D" \
     --section "## Identity" \
     >"$H12/pubD.log" 2>&1; then
  bad "R12(d): extracting a persona section DID NOT fail"
else
  ok "R12(d): persona-section extraction refused (persona-blank gate)"
fi

# ===========================================================================
# R13: catalog en fields (MINOR-1) + status!=official filter (MINOR-2)
# ===========================================================================
CURRENT=R13
hr; say "R13: catalog en locale stamps English + status!=official excluded"
H13="$(mktemp -d /tmp/dgn227-r13.XXXXXX)"
CAT13="$H13/catalog.json"
mkdir -p "$H13/enpk" "$H13/retiredpk"
printf '{}' > "$H13/enpk/pack-manifest.json"
printf '{}' > "$H13/retiredpk/pack-manifest.json"
cat > "$CAT13" <<'JSON'
{ "version": 1, "packs": [
  { "id": "enpk", "package_dir": "enpk", "status": "official", "pack_version": "1.0.0",
    "name_ko": "영문팩KO", "name_en": "EnglishPackEN",
    "tagline_ko": "한국어태그", "tagline_en": "english-tag",
    "role_prose_ko": "역할한국어", "role_prose_en": "role-english" },
  { "id": "retiredpk", "package_dir": "retiredpk", "status": "retired", "pack_version": "0.9.0",
    "name_ko": "폐기팩", "name_en": "RetiredPack" } ] }
JSON

# drive install.sh catalog readers directly against the fixture catalog.
# install.sh is sourced as a library (DOGANY_INSTALL_LIB=1 stops main()).
# stdout is written to a file inside the subshell (mirrors run_flow) so the
# `source` step's stderr noise never contaminates the captured value.
run_catalog() { # run_catalog <outfile> <lang> <fn-call...>
  local out="$1" lang="$2"; shift 2
  local call="$*"          # capture the fn-call BEFORE clearing positionals
  ( export HOME="$H13"; export DOGANY_INSTALL_LIB=1; export DOGANY_INSTALL_PINNED=1
    export DOGANY_LAUNCHD_CAPTURE="$H13/launchd.capture"
    cd "$SANDBOX"
    set +u    # install.sh top-level touches BASH_SOURCE[0] which set -u rejects
    set --    # clear positional params before sourcing (install.sh parses "$@")
    # shellcheck disable=SC1091
    source "$SANDBOX/install.sh"
    set +eu +o pipefail   # install.sh line 35 re-enables strict mode on source
    CATALOG_FILE="$CAT13"
    DOGANY_LANG="$lang"
    eval "$call" > "$out" 2>/dev/null )
}

# output files live OUTSIDE the fake HOME (install.sh sourcing operates on HOME)
OUT13="$(mktemp -d /tmp/dgn227-r13-out.XXXXXX)"

# MINOR-1: en locale -> English name in listing
EN_LIST="$OUT13/en_list.txt"; run_catalog "$EN_LIST" en catalog_entries
assert "MINOR-1: en locale stamps English name (EnglishPackEN)" \
  grep -q 'EnglishPackEN' "$EN_LIST"
assert "MINOR-1: en locale does NOT stamp the Korean name" \
  bash -c "! grep -q '영문팩KO' '$EN_LIST'"
EN_ROLE="$OUT13/en_role.txt"; run_catalog "$EN_ROLE" en catalog_role_prose enpk
assert "MINOR-1: en locale stamps English role prose" \
  grep -q 'role-english' "$EN_ROLE"

# ko locale still stamps Korean
KO_LIST="$OUT13/ko_list.txt"; run_catalog "$KO_LIST" ko catalog_entries
assert "MINOR-1: ko locale still stamps the Korean name" \
  grep -q '영문팩KO' "$KO_LIST"

# MINOR-2: retired pack excluded from BOTH locale listings
assert "MINOR-2: status!=official pack excluded from en listing" \
  bash -c "! grep -q 'retiredpk' '$EN_LIST'"
assert "MINOR-2: status!=official pack excluded from ko listing" \
  bash -c "! grep -q '폐기팩' '$KO_LIST'"

# ===========================================================================
# R14: DGN-420 briefing runtime -- generic-brief real composition + submit-mode
#      section write + P20 loud-fail fallback + MINOR-6 per-peer discrimination
#      + i18n (ko/en) + config-driven times.
#      SAFETY: every scenario drives generic-brief.sh with DOGANY_BRIEF_SINK set
#      to a temp capture file -- NO push.sh, NO live channel, NO live agent data.
#      Roots are synthetic dirs under /tmp built from the shipped template.
# ===========================================================================
CURRENT=R14
hr; say "R14: DGN-420 (composition / submit write / P20 / MINOR-6 / i18n / config times)"

GB="$SANDBOX/agents/.template/routines/generic-brief.sh"
AGG_SRC="$SANDBOX/agents/.template/routines/lib/handoff-aggregate"

# build_brief_root <root> <slug> <lang> -- minimal domain skeleton for the brief
build_brief_root() {
  local root="$1" slug="$2" lang="$3"
  mkdir -p "$root/routines/lib" "$root/config/i18n" "$root/files/handoff" \
           "$root/.telegram_bot/logs"
  cp "$GB" "$root/routines/generic-brief.sh"
  cp "$AGG_SRC" "$root/routines/lib/handoff-aggregate"
  cp "$SANDBOX/agents/.template/config/i18n/${lang}.json" "$root/config/i18n/${lang}.json"
  echo "AGENT_LANG=${lang}" > "$root/config/agent.conf"
}
# run a brief slot with the transport captured to a sink file
run_brief() { # run_brief <root> <slot> <sink>
  local root="$1" slot="$2" sink="$3"
  ( DOGANY_BRIEF_SINK="$sink" bash "$root/routines/generic-brief.sh" "$slot" ) >/dev/null 2>&1
}

RBASE="$(mktemp -d /tmp/dgn227-r14.XXXXXX)"

# --- 14a. standalone domain (ko): composes + utters all 3 slots w/ role prose ---
DOM_KO="$RBASE/dom_ko"
build_brief_root "$DOM_KO" auroria ko
KO_ROLE='당신의 일상을 곁에서 챙기는 생활비서'
for slot in morning retro weekly; do
  SINK="$RBASE/ko_${slot}.sink"
  run_brief "$DOM_KO" "$slot" "$SINK" || bad "R14a ko $slot exited non-zero"
  assert "14a: ko standalone $slot -> UTTER (not submit)" \
    grep -q "UTTER slot=$slot routing=standalone" "$SINK"
  assert "14a: ko standalone $slot carries generic role prose" \
    grep -qF "$KO_ROLE" "$SINK"
  assert "14a: ko standalone $slot did NOT write any submit file" \
    bash -c "! grep -q '^SUBMIT ' '$SINK'"
done

# --- 14b. standalone domain (en): role prose localizes ----------------------
DOM_EN="$RBASE/dom_en"
build_brief_root "$DOM_EN" enclave en
EN_ROLE="I'm your life assistant, looking after your day alongside you"
SINK="$RBASE/en_morning.sink"
run_brief "$DOM_EN" morning "$SINK" || bad "R14b en morning exited non-zero"
assert "14b: en standalone morning carries the EN role prose" \
  grep -qF "$EN_ROLE" "$SINK"
assert "14b: en briefing does NOT leak the KO role prose" \
  bash -c "! grep -qF '$KO_ROLE' '$SINK'"

# --- 14c. instance ROLE_PROSE overrides the generic default -----------------
DOM_OV="$RBASE/dom_override"
build_brief_root "$DOM_OV" taxbot ko
printf 'ROLE_PROSE=세금 신고와 공제 전략을 챙기는 세무 도우미\n' >> "$DOM_OV/config/agent.conf"
SINK="$RBASE/ov_morning.sink"
run_brief "$DOM_OV" morning "$SINK" || bad "R14c override exited non-zero"
assert "14c: instance ROLE_PROSE wins over generic default" \
  grep -qF '세무 도우미' "$SINK"
assert "14c: generic default suppressed when instance role present" \
  bash -c "! grep -qF '$KO_ROLE' '$SINK'"

# --- 14d. submit mode: domain writes its section to the main inbox (not utter) ---
MAIN_ROOT="$RBASE/main"
mkdir -p "$MAIN_ROOT/files/handoff"
DOM_SUB="$RBASE/dom_submit"
build_brief_root "$DOM_SUB" auroria ko
{
  echo "BRIEF_ROUTING=submit"
  echo "HANDOFF_PEER_MAIN=$MAIN_ROOT"
  echo "DOGANY_AGENT_LABEL=오로리아"
} >> "$DOM_SUB/config/agent.conf"
SINK="$RBASE/sub_retro.sink"
run_brief "$DOM_SUB" retro "$SINK" || bad "R14d submit exited non-zero"
assert "14d: submit mode -> SUBMIT action captured (no UTTER)" \
  bash -c "grep -q '^SUBMIT slot=retro routing=submit' '$SINK' && ! grep -q '^UTTER ' '$SINK'"
assert "14d: section file landed in the MAIN peer handoff inbox" \
  bash -c "ls '$MAIN_ROOT/files/handoff/'*'report.section.retro-'*'.md' >/dev/null 2>&1"
SUB_FILE="$(ls "$MAIN_ROOT/files/handoff/"*report.section.retro-*.md 2>/dev/null | head -1)"
DOM_SUB_BN="$(basename "$DOM_SUB")"
assert "14d: section frontmatter carries from: sender (root basename) + type" \
  bash -c "grep -q '^from: $DOM_SUB_BN' '$SUB_FILE' && grep -q '^type: report.section.retro' '$SUB_FILE'"

# --- 14e. main aggregates the domain's submitted section (submission-box loop) ---
# main root now sees the section; drive main-side aggregation via the lib.
cp "$AGG_SRC" "$MAIN_ROOT/handoff-aggregate.lib"
AGG_OUT="$RBASE/agg_single.txt"
( source "$MAIN_ROOT/handoff-aggregate.lib"
  handoff_aggregate "$MAIN_ROOT" retro "$DOM_SUB|오로리아" ) > "$AGG_OUT" 2>/dev/null
assert "14e: main aggregation attributes the section to the submitting peer" \
  grep -q '^(오로리아)' "$AGG_OUT"

# --- 14f. MINOR-6: N>=2 peers -> each peer's section attributed correctly ----
# Two peers submit distinct sections; assert NO cross-attribution.
MAIN2="$RBASE/main2"
mkdir -p "$MAIN2/files/handoff"
PEER_A="$RBASE/peerA"; PEER_B="$RBASE/peerB"
build_brief_root "$PEER_A" alpha ko
build_brief_root "$PEER_B" beta ko
for p in "$PEER_A|alpha" "$PEER_B|beta"; do
  proot="${p%%|*}"; pslug="${p##*|}"
  {
    echo "BRIEF_ROUTING=submit"
    echo "HANDOFF_PEER_MAIN=$MAIN2"
    echo "DOGANY_AGENT_LABEL=$pslug"
  } >> "$proot/config/agent.conf"
  # give each peer a distinct own-section so bodies differ
  cat > "$proot/routines/domain-section.sh" <<SEC
#!/bin/bash
echo "SECRET-$pslug body line"
SEC
  chmod +x "$proot/routines/domain-section.sh"
  run_brief "$proot" morning "$RBASE/${pslug}_sub.sink" || bad "R14f $pslug submit non-zero"
done
AGG2="$RBASE/agg_two.txt"
( source "$AGG_SRC"
  handoff_aggregate "$MAIN2" morning "$PEER_A|alpha,$PEER_B|beta" ) > "$AGG2" 2>/dev/null
assert "14f/MINOR-6: peer alpha's body attributed under (alpha)" \
  bash -c "awk '/^\(alpha\)/{f=1;next} /^\(/{f=0} f&&/SECRET-alpha/{print}' '$AGG2' | grep -q SECRET-alpha"
assert "14f/MINOR-6: peer beta's body attributed under (beta)" \
  bash -c "awk '/^\(beta\)/{f=1;next} /^\(/{f=0} f&&/SECRET-beta/{print}' '$AGG2' | grep -q SECRET-beta"
assert "14f/MINOR-6: no cross-attribution (alpha body NOT under beta)" \
  bash -c "! awk '/^\(beta\)/{f=1;next} /^\(/{f=0} f&&/SECRET-alpha/{print}' '$AGG2' | grep -q SECRET-alpha"
assert "14f/MINOR-6: no cross-attribution (beta body NOT under alpha)" \
  bash -c "! awk '/^\(alpha\)/{f=1;next} /^\(/{f=0} f&&/SECRET-beta/{print}' '$AGG2' | grep -q SECRET-beta"

# --- 14g. P20 loud-fail: submit target invalid -> warn + standalone fallback --
DOM_P20="$RBASE/dom_p20"
build_brief_root "$DOM_P20" auroria ko
{
  echo "BRIEF_ROUTING=submit"
  echo "HANDOFF_PEER_MAIN=$RBASE/nonexistent-main-root"   # invalid peer root
} >> "$DOM_P20/config/agent.conf"
SINK="$RBASE/p20.sink"
run_brief "$DOM_P20" morning "$SINK" || bad "R14g P20 exited non-zero"
assert "14g/P20: invalid peer -> loud warning captured (not silent)" \
  grep -q '^P20-WARN slot=morning' "$SINK"
assert "14g/P20: falls back to a standalone UTTER this run (briefing not lost)" \
  grep -q '^UTTER slot=morning routing=standalone' "$SINK"
assert "14g/P20: no SUBMIT written to the dead inbox" \
  bash -c "! grep -q '^SUBMIT ' '$SINK'"

# --- 14h. config-driven times: defaults when unset; honors an override -------
SINK="$RBASE/time_default.sink"
run_brief "$DOM_KO" morning "$SINK" || bad "R14h default time non-zero"
assert "14h: morning default time 07:00 when BRIEF_TIME_MORNING unset" \
  grep -q 'time=07:00' "$SINK"
SINK="$RBASE/time_retro.sink"
run_brief "$DOM_KO" retro "$SINK" || bad "R14h retro default non-zero"
assert "14h: retro default time 22:00 when unset" \
  grep -q 'time=22:00' "$SINK"
SINK="$RBASE/time_weekly.sink"
run_brief "$DOM_KO" weekly "$SINK" || bad "R14h weekly default non-zero"
assert "14h: weekly default Sun 20:00 when unset" \
  grep -q 'time=Sun 20:00' "$SINK"
# override
DOM_T="$RBASE/dom_time"
build_brief_root "$DOM_T" chronos ko
printf 'BRIEF_TIME_MORNING=06:15\n' >> "$DOM_T/config/agent.conf"
SINK="$RBASE/time_override.sink"
run_brief "$DOM_T" morning "$SINK" || bad "R14h override non-zero"
assert "14h: overridden BRIEF_TIME_MORNING=06:15 honored" \
  grep -q 'time=06:15' "$SINK"

# --- 14i. E1 Warg-hardcoding generalization: bundle daily-retro is peer-generic ---
assert "14i: daily-retro.sh no longer hardcodes the '워그 건강 리포트 미도착' literal" \
  bash -c "! grep -q '워그 건강 리포트 미도착' '$SANDBOX/agents/.template/routines/bundle/daily-retro.sh'"
assert "14i: daily-retro.sh parameterizes the health-peer name (config-driven)" \
  grep -q 'RETRO_HEALTH_PEER_NAME' "$SANDBOX/agents/.template/routines/bundle/daily-retro.sh"

# ===========================================================================
# R15: DGN-421 mode transition -- the standalone->submit flip end-to-end,
#      co-verified against the DGN-420 briefing runtime (no double / dropped
#      utterance across the transition), the E3 two-stage gate half-pass
#      invariant, the G2-5 section-before-publish timing verify+adjust, the
#      C2/P22 lifekit post-mint slot swap, and the MINOR-7 gate-seam residual.
#      SAFETY: synthetic fake HOME (mktemp), generic-brief driven with a sink
#      (no push.sh / no live channel), no launchd bootstrap (capture seams),
#      no ~/.dogany or live-agent data touched.
# ===========================================================================
CURRENT=R15
hr; say "R15: DGN-421 (transition flip / 420 coupling / E3 half-gate / G2-5 timing / C2 swap / seam)"

H15="$(mktemp -d)"
GB="$SANDBOX/agents/.template/routines/generic-brief.sh"
AGG_SRC="$SANDBOX/agents/.template/routines/lib/handoff-aggregate"
R15_DOM="$H15/.dogany/agents/zephyr"
R15_MAIN="$H15/.dogany/main"

# run a brief slot for a REAL minted-style root, transport captured to a sink
r15_brief() { # r15_brief <root> <slot> <sink>
  ( DOGANY_BRIEF_SINK="$3" bash "$1/routines/generic-brief.sh" "$2" ) >/dev/null 2>&1
}

# --- 15a. mint a standalone domain via the real install branch; it UTTERS ----
run_flow "$H15" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=zephyr
  DOGANY_ROLE_PROSE="세무 신고를 곁에서 챙기는 도우미"
  step_agent_class
  [ "$INSTALL_ROOT" = "'"$R15_DOM"'" ] || { echo "domain root not derived: $INSTALL_ROOT"; exit 10; }
  check_lite_single_agent "'"$R15_DOM"'" || { echo refused; exit 11; }
  mint_stub "'"$R15_DOM"'" zephyr
  dgn227_postmint "'"$R15_DOM"'"
  write_lite_marker "'"$R15_DOM"'"
' || bad "R15a domain mint flow non-zero (see $H15/flow.log)"

# ensure the generic-brief runtime + aggregation lib are present on the domain
cp "$GB" "$R15_DOM/routines/generic-brief.sh"
mkdir -p "$R15_DOM/routines/lib"
cp "$AGG_SRC" "$R15_DOM/routines/lib/handoff-aggregate"

assert "15a: fresh domain routing is standalone (no BRIEF_ROUTING=submit)" \
  bash -c "! grep -q '^BRIEF_ROUTING=submit' '$R15_DOM/config/agent.conf'"
SINK_PRE="$H15/pre_morning.sink"
r15_brief "$R15_DOM" morning "$SINK_PRE" || bad "R15a pre-transition brief non-zero"
assert "15a: standalone domain UTTERS its own briefing (420 runtime)" \
  grep -q '^UTTER slot=morning routing=standalone' "$SINK_PRE"
assert "15a: standalone domain writes NO submit file pre-transition" \
  bash -c "! grep -q '^SUBMIT ' '$SINK_PRE'"

# --- 15b. add a main later; E3 gate PASSES -> flip to submit ------------------
mint_stub "$R15_MAIN" "solaris"
# make it a real main (class=main) so the main-add class guard passes
printf 'DOGANY_AGENT_CLASS=main\n' >> "$R15_MAIN/.instance.conf"
# aggregation edition present (stage-2 gate): handoff-aggregate lib + a briefing
# script that consumes it -- the mint stub already copies generic-brief.sh which
# calls handoff-aggregate, and lib/ is copied. Confirm before driving the flow.
assert "15b(pre): main carries the aggregation edition (lib + consumer script)" \
  bash -c "[ -e '$R15_MAIN/routines/lib/handoff-aggregate' ] && grep -q handoff-aggregate '$R15_MAIN/routines/generic-brief.sh'"

run_flow "$H15" '
  export DOGANY_GATE_LOADED_OVERRIDE=1
  MAIN_ADD_FLOW=1
  EXISTING_DOMAIN_ROOT="$(cd "'"$R15_DOM"'" && pwd -P)"
  flow_main_add_finalize "'"$R15_MAIN"'"
' || bad "R15b gated flip flow non-zero (see $H15/flow.log)"

assert "15b: E3 gate PASS => domain flips to submit" \
  grep -qx "BRIEF_ROUTING=submit" "$R15_DOM/config/agent.conf"
assert "15b: HANDOFF_PEER_MAIN recorded on the domain (briefing axis)" \
  grep -qx "HANDOFF_PEER_MAIN=$R15_MAIN" "$R15_DOM/config/agent.conf"
assert "15b: flip did NOT leak into the migration key family (no MIGRATION_PEER)" \
  bash -c "! grep -q '^MIGRATION_PEER=' '$R15_DOM/config/agent.conf'"

# --- 15c. DGN-420 COUPLING: same domain now SUBMITS (no double / dropped) -----
mkdir -p "$R15_MAIN/files/handoff"
SINK_POST="$H15/post_morning.sink"
r15_brief "$R15_DOM" morning "$SINK_POST" || bad "R15c post-transition brief non-zero"
assert "15c/COUPLING: post-flip the SAME domain SUBMITS a section (not UTTER)" \
  bash -c "grep -q '^SUBMIT slot=morning routing=submit' '$SINK_POST' && ! grep -q '^UTTER ' '$SINK_POST'"
assert "15c/COUPLING: exactly ONE briefing action this run (no double-utterance)" \
  bash -c "[ \"\$(grep -cE '^(UTTER|SUBMIT) ' '$SINK_POST')\" = 1 ]"
assert "15c/COUPLING: section landed in the main peer inbox (briefing not dropped)" \
  bash -c "ls '$R15_MAIN/files/handoff/'*'report.section.morning-'*'.md' >/dev/null 2>&1"
# transition invariant: exactly one utterance BEFORE, exactly one submit AFTER,
# and the mode actually changed between the two runs (no missing, no double).
assert "15c/COUPLING: pre=UTTER, post=SUBMIT -- mode changed, briefing preserved both sides" \
  bash -c "grep -q '^UTTER ' '$SINK_PRE' && grep -q '^SUBMIT ' '$SINK_POST' && ! grep -q '^SUBMIT ' '$SINK_PRE' && ! grep -q '^UTTER ' '$SINK_POST'"

# --- 15d. E3 two-stage gate: HALF pass leaves mode UNCHANGED (loud, not silent) ---
# stage-1 loaded but stage-2 (aggregation edition) BROKEN -> flip must be refused.
# Fresh HOME so the main-max-1 registry invariant does not collide with 15b.
H15D="$(mktemp -d)"
R15_DOM2="$H15D/.dogany/agents/orbit"
run_flow "$H15D" '
  DOGANY_AGENT_CLASS=domain
  DOGANY_PACK_ID=blank
  DOGANY_AGENT_SLUG=orbit
  DOGANY_ROLE_PROSE="x"
  step_agent_class
  mint_stub "'"$R15_DOM2"'" orbit
  dgn227_postmint "'"$R15_DOM2"'"
  write_lite_marker "'"$R15_DOM2"'"
' || bad "R15d second-domain mint non-zero (see $H15D/flow.log)"
R15_MAIN2="$H15D/.dogany/main"
mint_stub "$R15_MAIN2" "vega"
printf 'DOGANY_AGENT_CLASS=main\n' >> "$R15_MAIN2/.instance.conf"
rm -f "$R15_MAIN2/routines/lib/handoff-aggregate"   # break stage-2 (edition)
GATE_LOG="$H15D/flow.log"
run_flow "$H15D" '
  export DOGANY_GATE_LOADED_OVERRIDE=1
  MAIN_ADD_FLOW=1
  EXISTING_DOMAIN_ROOT="$(cd "'"$R15_DOM2"'" && pwd -P)"
  flow_main_add_finalize "'"$R15_MAIN2"'"
' || true
assert "15d/E3: stage-1 pass + stage-2 fail => domain stays standalone (mode unchanged)" \
  grep -qx "BRIEF_ROUTING=standalone" "$R15_DOM2/config/agent.conf"
assert "15d/E3: half-gate refusal is LOUD (edition-absent gate message logged)" \
  grep -q 'aggregation library absent\|취합 라이브러리 부재' "$GATE_LOG"
assert "15d/E3: half-gate leaves NO HANDOFF_PEER_MAIN (no silent partial flip)" \
  bash -c "! grep -q '^HANDOFF_PEER_MAIN=' '$R15_DOM2/config/agent.conf'"

# --- 15e. G2-5 timing: domain generation must PRECEDE main publish (verify+adjust) ---
# Craft an inversion: domain morning fires at 08:00, main morning at 07:00.
# The transition must detect (dom >= main) and shift the DOMAIN slot earlier.
set_plist_time() { # set_plist_time <plist> <hh> <mm>
  perl -0pi -e "s#(<key>Hour</key>\s*<integer>)\d+#\${1}$2#; s#(<key>Minute</key>\s*<integer>)\d+#\${1}$3#" "$1"
}
DOM_MP="$(ls "$R15_DOM"/routines/*generic-brief-morning.plist 2>/dev/null | head -1)"
MAIN_MP="$(ls "$R15_MAIN"/routines/*generic-brief-morning.plist 2>/dev/null | head -1)"
set_plist_time "$DOM_MP" 8 0     # domain generation 08:00 (LATER -- inverted)
set_plist_time "$MAIN_MP" 7 0    # main publish       07:00
run_flow "$H15" '
  plist_slot_minutes "'"$R15_DOM"'/routines" morning
  echo ---
  verify_section_before_publish "$(cd "'"$R15_DOM"'" && pwd -P)" "$(cd "'"$R15_MAIN"'" && pwd -P)"
' || bad "R15e timing verify flow non-zero (see $H15/flow.log)"
# after adjust: domain morning should be main(07:00=420m) - 45 lead = 06:15
DOM_AFTER_MIN="$(perl -0ne 'if(/<key>Hour<\/key>\s*<integer>(\d+)/){$h=$1} if(/<key>Minute<\/key>\s*<integer>(\d+)/){$m=$1} END{print $h*60+$m}' "$DOM_MP")"
assert "15e/G2-5: inverted domain slot adjusted to precede main (06:15 = main-45m)" \
  bash -c "[ '$DOM_AFTER_MIN' = 375 ]"
assert "15e/G2-5: adjusted domain generation now strictly precedes main publish (420)" \
  bash -c "[ '$DOM_AFTER_MIN' -lt 420 ]"
# non-inverted case must NOT be moved: domain 06:00 < main 07:00 stays put
set_plist_time "$DOM_MP" 6 0
run_flow "$H15" '
  verify_section_before_publish "$(cd "'"$R15_DOM"'" && pwd -P)" "$(cd "'"$R15_MAIN"'" && pwd -P)"
' || bad "R15e non-inverted verify non-zero"
DOM_KEEP_MIN="$(perl -0ne 'if(/<key>Hour<\/key>\s*<integer>(\d+)/){$h=$1} if(/<key>Minute<\/key>\s*<integer>(\d+)/){$m=$1} END{print $h*60+$m}' "$DOM_MP")"
assert "15e/G2-5: already-preceding domain slot is left UNCHANGED (no needless reschedule)" \
  bash -c "[ '$DOM_KEEP_MIN' = 360 ]"

# --- 15f. C2/P22 lifekit post-mint opt-in slot swap ---------------------------
# The fresh domain shipped WITHOUT lifekit (LIFEKIT=off). Opt in after mint:
# turning on a slot-owning lifekit routine must UNSCHEDULE the same-slot
# generic-brief; turning it off must RESTORE it. Test the swap primitive with
# the load layer captured (BRIEF_SLOT_CAPTURE) -- no live launchctl.
assert "15f(pre): domain shipped lifekit-dormant (LIFEKIT=off)" \
  grep -qx "LIFEKIT=off" "$R15_DOM/config/lifekit.conf"
# invoke the DOMAIN's own copy (mint_stub places it under routines/lib) -- the
# skill runs `routines/lib/brief-slot-ctl.sh` from the agent root, so the script
# resolves AGENT_NAME from that root's .instance.conf.
BSC="$R15_DOM/routines/lib/brief-slot-ctl.sh"
SWAP_CAP="$H15/swap.cap"
# opt-in: lifekit morning-brief takes the morning slot -> disable generic morning
( cd "$R15_DOM" && BRIEF_SLOT_NO_LOAD=1 BRIEF_SLOT_CAPTURE="$SWAP_CAP" \
    bash "$BSC" disable morning ) >/dev/null 2>&1 || bad "R15f swap-disable non-zero"
assert "15f/C2: opt-in swaps OUT the same-slot generic-brief (disable captured)" \
  grep -q "disable com.telegram-skill-bot.zephyr.generic-brief-morning" "$SWAP_CAP"
# opt-out: lifekit morning-brief off -> restore generic morning (no gap)
( cd "$R15_DOM" && BRIEF_SLOT_NO_LOAD=1 BRIEF_SLOT_CAPTURE="$SWAP_CAP" \
    bash "$BSC" enable morning ) >/dev/null 2>&1 || bad "R15f swap-enable non-zero"
assert "15f/C2: opt-out restores the same-slot generic-brief (enable captured)" \
  grep -q "enable com.telegram-skill-bot.zephyr.generic-brief-morning" "$SWAP_CAP"
assert "15f/C2: swap NEVER deletes the generic-brief plist file (파일 배치 불변)" \
  bash -c "ls '$R15_DOM'/routines/*generic-brief-morning.plist >/dev/null 2>&1"
assert "15f/C2: weekly has no lifekit counterpart -- swap primitive still valid but unused by bundle" \
  bash -c "grep -q 'weekly)' '$BSC'"
# SKILL.md wiring: the lifekit-setup skill actually calls the swap primitive
assert "15f/C2: lifekit-setup SKILL.md wires the brief-slot swap (routine on/off)" \
  grep -q 'brief-slot-ctl.sh' "$SANDBOX/skills/dogany-lifekit-setup/SKILL.md"

# --- 15g. MINOR-7 gate-seam residual: shims NOT triggerable via plain env -----
# The two seams (DOGANY_GATE_LOADED_OVERRIDE / DOGANY_LAUNCHD_CAPTURE) are each
# honored ONLY when DOGANY_INSTALL_LIB=1. install.sh must be sourced with that
# flag to stop main(), so we exercise the gate's OWN guard: inside the sourced
# context, locally clear DOGANY_INSTALL_LIB (the shipped/live condition) then
# call the transition helpers with the override set -- the shim must be IGNORED,
# so submit_flip_gate falls through to the real launchctl query (which, for a
# synthetic never-loaded root, fails stage-1 => gate refuses).
R15_SEAM="$H15/.dogany/seamroot"
mint_stub "$R15_SEAM" "seam"
printf 'DOGANY_AGENT_CLASS=main\n' >> "$R15_SEAM/.instance.conf"
SEAM_LOG="$H15/seam.log"
(
  export HOME="$H15"
  export DOGANY_INSTALL_PINNED=1
  export DOGANY_INSTALL_LIB=1              # source-as-lib: stops main()
  cd "$SANDBOX"; set --
  source "$SANDBOX/install.sh"; trap - ERR
  DRY_RUN=0
  # shipped/live condition: NO lib flag in effect for the shim check
  unset DOGANY_INSTALL_LIB
  export DOGANY_GATE_LOADED_OVERRIDE=1     # plain env -- must be IGNORED now
  if submit_flip_gate "$R15_SEAM"; then echo "SEAM-LEAK: override bypassed real query"; else echo "SEAM-OK: gate used real query"; fi
) > "$SEAM_LOG" 2>&1
assert "15g/MINOR-7: plain DOGANY_GATE_LOADED_OVERRIDE does NOT bypass the gate (no shim leak)" \
  grep -q 'SEAM-OK' "$SEAM_LOG"
assert "15g/MINOR-7: no SEAM-LEAK reported on the transition gate path" \
  bash -c "! grep -q 'SEAM-LEAK' '$SEAM_LOG'"
# and the capture seam is likewise gated: with the lib flag cleared, a plain
# DOGANY_LAUNCHD_CAPTURE must be inert (the call falls through to dry-run path).
SEAM_CAP="$H15/seam.cap"
(
  export HOME="$H15"; export DOGANY_INSTALL_PINNED=1
  export DOGANY_INSTALL_LIB=1
  cd "$SANDBOX"; set --
  source "$SANDBOX/install.sh"; trap - ERR
  unset DOGANY_INSTALL_LIB
  export DOGANY_LAUNCHD_CAPTURE="$SEAM_CAP"   # plain env -- must be IGNORED
  DRY_RUN=1     # dry-run so no live launchctl in this seam probe
  schedule_deferred_briefs "$R15_SEAM"
) >/dev/null 2>&1 || true
assert "15g/MINOR-7: plain DOGANY_LAUNCHD_CAPTURE writes nothing (capture shim gated)" \
  bash -c "[ ! -s '$SEAM_CAP' ]"

# ===========================================================================
hr
say "RESULT: pass=$PASS fail=$FAIL"
say "fake homes: $H1 $H2 $H3 $H15 (inspect flow.log / launchd.capture on failure)"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
