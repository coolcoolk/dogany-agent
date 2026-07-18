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
hr
say "RESULT: pass=$PASS fail=$FAIL"
say "fake homes: $H1 $H2 $H3 (inspect flow.log / launchd.capture on failure)"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
