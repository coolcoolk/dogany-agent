#!/bin/bash
# mint_run.sh -- pack machinery wrapper around the framework installer
# (scripts/mint.sh). DGN-243, generalized per DGN-366 L1 (repo-resident
# machinery; instance context via explicit --instance-root).
#
# It does NOT reimplement minting: it resolves instance defaults (owner id,
# tz, lang) from the CALLING instance passed via --instance-root, validates
# inputs, and calls the framework mint.sh. The USER LABEL is deliberately
# NOT resolved from the calling instance: it is a per-agent axis (the
# parent's own address term for the owner), so the child always mints with
# the neutral default and onboarding fills the real term.
# Token comes from the DOGANY_BOT_TOKEN env var ONLY (never argv, never
# echoed).
#
# Instance context contract (DGN-366 L1): the calling instance's root is
# passed EXPLICITLY as --instance-root <path>. There is no script-location
# derivation of the instance root. When --instance-root is absent, every
# instance-dependent step (owner-id/tz/lang/models reads, mint journal,
# minting_state record) SKIPS WITH AN EXPLICIT LOG LINE and the
# flags/defaults take over -- never a silent skip, including channel resolution.
#
# Usage:
#   DOGANY_BOT_TOKEN=<token> mint_run.sh mint --slug <slug> [--root <dir>]
#            [--instance-root <dir>] [--owner-id <ids>] [--tz <tz>]
#            [--lang <en|ko>] [--core-only] [--model <sonnet|opus|haiku>]
#            [--channel <release|dev|staging|main>] [--dry-run]
#   mint_run.sh start --root <dir> [--deferred] [--dry-run]
#            # load launchd plists (LIVE op -- only after explicit user
#            # approval)
#   mint_run.sh verdict --root <dir> [--timeout <s>] [--since <epoch>]
#            [--fresh] [--process] [--strict] [--check viability|config]
#            # DGN-1683 WP5 (all opt-in): --fresh = the beat must also be
#            # inside the freshness window NOW (with --since); --process =
#            # the launchd bridge unit of this root is running under a live
#            # pid whose command line carries "--path <root>"; --strict =
#            # channel "not judged" fails (expected = --channel | ordering
#            # declaration | release default); --check runs only one half
#            # (rc 30 viability, rc 31 config) so both can be collected.
#            [--instance-root <dir>] [--channel <release|dev|staging|main>]
#            # DGN-1606 viability verdict, standalone seat: is this newborn's
#            # bridge actually alive? READ-ONLY (never repairs/deletes).
#            # rc 0 = alive, rc 30 = not confirmed within the window.
#            # Default window/freshness = STALE_S from the instance's own
#            # bridge/watchdog.sh; --since restricts to beats at/after the
#            # given epoch (the pipeline uses its pre-start epoch).
#            # With --instance-root (DGN-1608): after liveness confirms, the
#            # same seat also judges born-alive-and-wrong (cheap birth-defect
#            # checks vs the ordering instance) -- rc 31 = alive but
#            # defective. Without it the mode keeps its original
#            # liveness-only contract (no existing caller's meaning shifts).
#   DOGANY_BOT_TOKEN=<token> mint_run.sh pipeline --slug <slug> --pack <id>
#            [--root <dir>] [--instance-root <dir>] [--owner-id <ids>]
#            [--tz <tz>] [--lang <en|ko>] [--role <prose>] [--core-only]
#            [--model <sonnet|opus|haiku>] [--catalog <file>]
#            [--channel <release|dev|staging|main>]
#            [--source-tag <pack/<id>/vX.Y.Z[-pre]>]
#            [--untagged-local <reason>]
#            [--migrate-from <peer-root>] [--peer-main <main-root>]
#            [--no-start] [--no-state] [--dry-run]
#            # --peer-main: G3-b (DGN-227/DGN-422) main-install -> later
#            #          domain-add via v2 path. FRESH mint attaching to an
#            #          existing main: registry register + limit ledger +
#            #          BRIEFING-axis HANDOFF_PEER_MAIN + gated BRIEF_ROUTING.
#            #          NEVER writes MIGRATION_PEER (E2-1 axis separation).
#            #          Mutually exclusive with --migrate-from.
#            # --migrate-from: MIGRATION PATH (DGN-284): the new instance
#            #          migrates an existing user's records from the main
#            #          agent at <peer-root>. Sets peer keys
#            #          (HANDOFF_PEER_AG etc.) + domain seed pending_data
#            #          in pack_install. OMITTED (default) = fresh/
#            #          standalone mint: no peer keys, seed ready.
#            # --no-start: skip launchd bootstrap. DGN-1543: pack_install
#            #          never starts the bot on this path -- pipeline itself
#            #          does, as its own last step, strictly AFTER the
#            #          transaction marker closes (see pipeline mode below).
#            # --no-state: skip minting_state record (pack_install step 11)
#            # --model: model for the new instance (default: sonnet)
#            # --catalog: pack catalog override (passed to pack_install)
#            # --source-tag: exact tag ref the pack payload came from (dsp
#            #          t2pin). Passed through VERBATIM to pack_install.sh,
#            #          which owns ALL validation (tag grammar / semver /
#            #          base-version coherence / tag existence, fail-closed)
#            #          and makes the DOGANY_PACKS ledger record the TAG's
#            #          version instead of the manifest pack_version. This
#            #          wrapper never judges the ref itself -- one validator,
#            #          one rule set. Omitted = pack_install's source-tag
#            #          gate (DGN-1489) judges the lane: a git-work-tree
#            #          pack source must have the resolved version's tag or
#            #          an explicit --untagged-local declaration.
#            # --untagged-local: explicit opt-in for an untagged working-tree
#            #          install (DGN-1489). Passed through VERBATIM to
#            #          pack_install.sh, which owns the judgement and records
#            #          the declaration in the install receipt.
#   mint_run.sh handover-brief --pack <id> --instance-root <dir>
#            [--catalog <file>]
#            # READ-ONLY (dsp hslot, owner rulings 2026-09-18): the base-kit
#            # handover-summary slot. Asks the kit behind <pack-id> ONE
#            # question -- "a new agent is being minted with you; if anything
#            # carries over, describe it in sentences a person reads" -- by
#            # running the verb the KIT ITSELF declared (manifest top-level
#            # `handover_brief_verb`, packs/CONTRACT.md §2) on the LEADER's
#            # installed kit CLI (<instance-root>/database/<kit>.sh). The
#            # framework stays schema-blind: no table, no column, no SQL.
#            # stdout contract: rc 0 + body = judged owner-facing brief,
#            # render VERBATIM; rc 0 + empty = NO SCREEN (absence is normal:
#            # slot undeclared, kit absent, kit silent, kit errored/timed
#            # out, or the copy judge refused). Diagnostics: stderr only.
#   mint_run.sh catalog-brief --query <owner words> [--instance-root <dir>]
#            [--catalog <file>]
#            # DGN-1575: the mint flow's step-1 catalog branch as MACHINERY.
#            # Matches the owner's own domain words against the canonical
#            # catalog and EMITS the owner-facing official-vs-custom choice
#            # screen (tagline_ko + every includes_ko line verbatim, colon
#            # separators, LABELED [[OPTIONS]]) -- the model renders stdout
#            # VERBATIM instead of remembering to compose it. Writes the
#            # stamp (<instance-root>/config/mint-catalog-brief.json) the
#            # mint.catalog-brief gate reads. rc 0 + body = match, render;
#            # rc 3 + empty stdout = no match, freeform confirm; rc 4 =
#            # composed copy violates an owner ruling (refused LOUD, never
#            # silently skipped -- a silent skip IS the DGN-1575 incident).
#            # One matcher for this verb and the gate: lib/catalog_brief.py.
#   mint_run.sh occupancy {--slug <slug> | --root <dir>}
#            # READ-ONLY (DGN-1543 R6): is the mint target slot already
#            # taken? rc 0 = FREE, rc 10 = OCCUPIED (verdict + signals on
#            # stdout), rc 1 = usage. The mint flow (SKILL.md step 3) runs
#            # this the moment the slug exists -- BEFORE the BotFather
#            # walkthrough -- so an occupied slot stops the flow while the
#            # owner has invested nothing. The pipeline below carries the
#            # same predicate as a hard gate (mint.occupied), so skipping
#            # this probe only moves the stop later, never removes it.
#   mint_run.sh teardown --slug <slug> | --root <dir> [--instance-root <dir>]
#            [--archive-dir <dir>] [--dry-run]
#   mint_run.sh teardown --restore <archive-dir> [--instance-root <dir>]
#            # [--flow-receipt <file>] (DGN-1683: a wrapper-owned mint-flow
#            # receipt is a second "unfinished mint" evidence kind; see
#            # lib/mint_teardown.sh)
#            # DGN-1506: the way back out of a mint that stopped midway.
#            # `recover` above replays stamps FORWARD; nothing here ever
#            # removed anything, so a mint halted at a scope gate left a
#            # half-minted instance that a human had to delete by hand.
#            # teardown ARCHIVES it (never rm): the instance body, the
#            # unregistered ~/Library/LaunchAgents copies of its own units,
#            # its handoff mailbox and the parent's mint journal entry all
#            # move under <archive-dir>/<name>.<UTC ts>/ with a RECEIPT.json
#            # that --restore reads back. Two fail-closed gates stand in
#            # front of it (liveness / unfinished-mint evidence) -- full
#            # rationale in scripts/pack/lib/mint_teardown.sh. It NEVER
#            # boots out, loads or stops a unit: a still-registered instance
#            # is refused, not stopped.
#            # Before the first move it marks every health-observer-known
#            # Label of the instance retired and reads the markers back, so
#            # the approved teardown is not reported as a silent death
#            # (DGN-1616; rc 7 = not confirmed, nothing moved).
#   mint_run.sh recover <slug> --instance-root <dir> [--root <dir>]
#            # crash-recovery: if a prior mint session crashed after the
#            # framework mint.sh call but before post-mint stamps completed,
#            # replay stamps (role, model) from the saved mint journal.
#            # The journal lives in the CALLING instance
#            # (<instance-root>/.mint-journal), so --instance-root is
#            # required here. Safe to re-run: idempotent guards prevent
#            # double-stamping.
#
# --channel: mint/pipeline explicitly select release|dev|staging|main. If
# omitted, inherit DOGANY_UPDATE_CHANNEL from --instance-root/.instance.conf.
# Missing/empty declarations and absent ordering instances loudly skip
# measurement and let mint.sh apply its own default
# (explicit > kept declaration > release); the environment never seeds it.
# verdict accepts --channel to re-judge an intentional override.
#
# --dry-run: preflight checks + resolved plan (token masked) + a real
# mint.sh --print-env smoke (no writes anywhere). Exit 0 = plan OK.
# In start mode --dry-run prints the load/defer plan without copying or
# bootstrapping anything.
#
# Deferral manifest (DGN-238 grill-final MAJOR-1): an instance package may
# ship <root>/routines/plists.defer -- one plist basename per line ('#'
# comments allowed). Plain `start` SKIPS those units (staged, not loaded)
# so a package can stage e.g. daily routines behind a later gate. Load the
# deferred set later with `mint_run.sh start --root <dir> --deferred`
# (loads ONLY the deferred units). No manifest file = load everything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo shipping this machinery (default framework root for mint.sh).
MACHINERY_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# DGN-1572: launchd unit install directory, overridable so a sandboxed test
# instance can never register a real unit in the owner's live launchd domain.
# Unset (the live default) resolves to the exact historical path.
LAUNCH_AGENTS_DIR="${DOGANY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"

# DGN-1424: resolve the python3 interpreter ONCE, PATH-independent (a bare
# `python3` call resolves whatever the CALLER's PATH hands back -- see the
# lib file header). Reuses $PACK_PY3 if a parent (pack_install.sh) already
# resolved it.
PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

PLIST_LABEL_LIB="$SCRIPT_DIR/lib/plist_label.sh"
[[ -f "$PLIST_LABEL_LIB" && -r "$PLIST_LABEL_LIB" ]] \
  || { echo "ERROR: plist Label library missing ($PLIST_LABEL_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/plist_label.sh
. "$PLIST_LABEL_LIB"

# DGN-1875: units gated on an instance module flag (MIRROR_MODULE) are not
# loaded by `start` while the flag is off.
MODULE_GATE_LIB="$SCRIPT_DIR/lib/module_gate.sh"
[[ -f "$MODULE_GATE_LIB" && -r "$MODULE_GATE_LIB" ]] \
  || { echo "ERROR: module gate library missing ($MODULE_GATE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/module_gate.sh
. "$MODULE_GATE_LIB"

# DGN-773 R5/T4b: persona read/write-target resolver (PROFILE.md vs AGENT.md).
PERSONA_LIB="$SCRIPT_DIR/lib/persona_resolver.sh"
[[ -f "$PERSONA_LIB" && -r "$PERSONA_LIB" ]] \
  || { echo "ERROR: persona resolver library missing ($PERSONA_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/persona_resolver.sh
. "$PERSONA_LIB"

# DGN-1506: teardown machinery (gates + archive + restore + the unfinished-
# mint transaction marker the mint/pipeline paths below open and close).
TEARDOWN_LIB="$SCRIPT_DIR/lib/mint_teardown.sh"
[[ -f "$TEARDOWN_LIB" && -r "$TEARDOWN_LIB" ]] \
  || { echo "ERROR: teardown library missing ($TEARDOWN_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/mint_teardown.sh
. "$TEARDOWN_LIB"

# DGN-1624: completed-agent retirement is separate from unfinished teardown.
# mint_run.sh retire --root ROOT [--dry-run] | --resume RECEIPT | --restore ARCHIVE
if [[ "${1:-}" == "retire" ]]; then
  shift
  exec bash "$SCRIPT_DIR/mint_retire.sh" "$@"
fi

MODE="${1:-}"
[[ "$MODE" == "mint" || "$MODE" == "start" || "$MODE" == "pipeline" || "$MODE" == "recover" || "$MODE" == "stamp-role" || "$MODE" == "align-peer-main" || "$MODE" == "teardown" || "$MODE" == "occupancy" || "$MODE" == "handover-brief" || "$MODE" == "catalog-brief" || "$MODE" == "verdict" ]] || {
  echo "usage: mint_run.sh {mint|start|pipeline|recover|stamp-role|align-peer-main|teardown|occupancy|handover-brief|catalog-brief|verdict} ..." >&2; exit 1; }
shift

SLUG="" ROOT="" OWNER_IDS="" AGENT_TZ="" LANG_OPT="" DRY=0 CORE_ONLY=0
ONLY_DEFERRED=0 ROLE_PROSE="" KIT_PENDING=0 PACK_ID="" NO_START=0 NO_STATE=0 MODEL_OPT=""
PEER_ROOT="" INSTANCE_ROOT="" CATALOG_OPT="" PEER_MAIN="" SOURCE_TAG_OPT=""
UNTAGGED_LOCAL_OPT="" ARCHIVE_DIR="" RESTORE_DIR="" TXN_OPEN=0 QUERY_OPT=""
VERDICT_TIMEOUT_OPT="" VERDICT_SINCE_OPT="" CHANNEL_OPT="" CHANNEL_SET=0
VERDICT_FRESH=0 VERDICT_PROCESS=0 VERDICT_STRICT=0 VERDICT_CHECK="default"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)
      [[ $# -ge 2 && -n "$2" ]] || { echo "ERROR: --channel requires a value" >&2; exit 1; }
      CHANNEL_OPT="$2"; CHANNEL_SET=1; shift 2 ;;
    --slug)      SLUG="$2"; shift 2 ;;
    --root)      ROOT="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --owner-id)  OWNER_IDS="$2"; shift 2 ;;
    --tz)        AGENT_TZ="$2"; shift 2 ;;
    --lang)      LANG_OPT="$2"; shift 2 ;;
    --core-only) CORE_ONLY=1; shift ;;
    --dry-run|--dry) DRY=1; shift ;;
    --deferred)  ONLY_DEFERRED=1; shift ;;
    --role)      ROLE_PROSE="$2"; shift 2 ;;
    # stamp-role only: a kitless main carries no role yet (see _stamp_role).
    --kit-pending) KIT_PENDING=1; shift ;;
    --pack)      PACK_ID="$2"; shift 2 ;;
    --catalog)   CATALOG_OPT="$2"; shift 2 ;;
    --query)     QUERY_OPT="$2"; shift 2 ;;
    --source-tag) SOURCE_TAG_OPT="$2"; shift 2 ;;
    --untagged-local) UNTAGGED_LOCAL_OPT="$2"; shift 2 ;;
    --migrate-from) PEER_ROOT="$2"; shift 2 ;;
    # G3-b (DGN-227 / DGN-422): main install -> later domain-add via the v2
    # mint-agent path. --peer-main names the EXISTING main root the new domain
    # attaches to. This is a FRESH mint (NOT --migrate-from): it records the
    # BRIEFING-axis key HANDOFF_PEER_MAIN, never the migration-axis MIGRATION_PEER
    # (E2-1 key-axis separation -- so the onboarding migration discriminator does
    # NOT mis-fire on this fresh domain).
    --peer-main) PEER_MAIN="$2"; shift 2 ;;
    --no-start)  NO_START=1; shift ;;
    --no-venv)  NO_VENV=1; shift ;;
    --no-state)  NO_STATE=1; shift ;;
    --model)     MODEL_OPT="$2"; shift 2 ;;
    # DGN-1506 teardown axis.
    --archive-dir) ARCHIVE_DIR="$2"; shift 2 ;;
    --restore)     RESTORE_DIR="$2"; shift 2 ;;
    # DGN-1683 (grill fix 8): teardown's flow-receipt evidence kind. The path
    # is only a claim -- lib/mint_flow_guard.py verifies it against the
    # ordering instance's mint-flow.json before teardown believes it.
    --flow-receipt) MINT_TEARDOWN_FLOW_RECEIPT="$2"; export MINT_TEARDOWN_FLOW_RECEIPT; shift 2 ;;
    # INTERNAL: pipeline hands this to its own `mint` subcall so the
    # unfinished-mint marker stays open across mint -> pack_install instead of
    # closing when the mint half finishes. Not for hand use.
    --txn-open)    TXN_OPEN=1; shift ;;
    # DGN-1606 viability-verdict axis (verdict mode + pipeline verdict step).
    --timeout)     VERDICT_TIMEOUT_OPT="$2"; shift 2 ;;
    --since)       VERDICT_SINCE_OPT="$2"; shift 2 ;;
    # DGN-1683 WP5 readiness axis (verdict mode only; all opt-in, so no
    # existing caller's meaning shifts).
    --fresh)       VERDICT_FRESH=1; shift ;;
    --process)     VERDICT_PROCESS=1; shift ;;
    --strict)      VERDICT_STRICT=1; shift ;;
    --check)       VERDICT_CHECK="$2"; shift 2 ;;
    *)
      # recover mode takes the slug as a bare positional
      if [[ "$MODE" == "recover" && -z "$SLUG" && "${1:0:2}" != "--" ]]; then
        SLUG="$1"; shift
      else
        echo "unknown option: $1" >&2; exit 1
      fi
      ;;
  esac
done

# Validate --model if supplied (allowed: sonnet, opus, haiku; default: sonnet).
ALLOWED_MODELS="sonnet opus haiku"
if [[ -n "$MODEL_OPT" ]]; then
  valid=0
  for m in $ALLOWED_MODELS; do [[ "$MODEL_OPT" == "$m" ]] && valid=1 && break; done
  [[ "$valid" -eq 1 ]] || {
    echo "ERROR: --model '$MODEL_OPT' not recognized; allowed values: $ALLOWED_MODELS" >&2
    exit 1
  }
fi
# Apply default after validation so error messages are accurate.
MODEL_OPT="${MODEL_OPT:-sonnet}"

case "$VERDICT_CHECK" in
  default|viability|config) : ;;
  *) echo "ERROR: --check must be viability|config: $VERDICT_CHECK" >&2; exit 1 ;;
esac
# Validate the DGN-1606 verdict flags if supplied (non-negative integers).
if [[ -n "$VERDICT_TIMEOUT_OPT" && ! "$VERDICT_TIMEOUT_OPT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --timeout must be a non-negative integer (seconds): $VERDICT_TIMEOUT_OPT" >&2
  exit 1
fi
if [[ -n "$VERDICT_SINCE_OPT" && ! "$VERDICT_SINCE_OPT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --since must be a non-negative integer (epoch seconds): $VERDICT_SINCE_OPT" >&2
  exit 1
fi

# G3-b guard: --peer-main (briefing-axis attach, fresh domain-add) is mutually
# exclusive with --migrate-from (migration-axis). Mixing the two axes is exactly
# the E2-1 conflation this design forbids.
if [[ -n "$PEER_MAIN" && -n "$PEER_ROOT" ]]; then
  echo "ERROR: --peer-main and --migrate-from are mutually exclusive (E2-1: briefing axis vs migration axis)" >&2
  exit 1
fi

# Validate --instance-root if supplied.
if [[ -n "$INSTANCE_ROOT" ]]; then
  [[ -d "$INSTANCE_ROOT" ]] || {
    echo "ERROR: --instance-root not a directory: $INSTANCE_ROOT" >&2
    exit 1
  }
fi

# Journal directory: instance-resident state (DGN-366 L1). Empty when no
# --instance-root -- journal write/cleanup then SKIP with an explicit log.
JOURNAL_DIR=""
[[ -n "$INSTANCE_ROOT" ]] && JOURNAL_DIR="$INSTANCE_ROOT/.mint-journal"

conf_get() { # conf_get <file> <key> -- empty (not fatal) when key/file absent
  { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'; } || true
}

CANONICAL_CATALOG_LIB="$SCRIPT_DIR/lib/canonical_catalog.sh"
[[ -f "$CANONICAL_CATALOG_LIB" && -r "$CANONICAL_CATALOG_LIB" ]] \
  || { echo "ERROR: canonical catalog library missing ($CANONICAL_CATALOG_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/canonical_catalog.sh
. "$CANONICAL_CATALOG_LIB"

# conf_upsert <file> <key> <value> -- replace the KEY= line or append it.
_conf_upsert() {
  local file="$1" key="$2" val="$3" tmp
  touch "$file"
  if grep -qE "^$key=" "$file" 2>/dev/null; then
    # grep -v exits 1 when it filters out ALL lines (single-key file); the
    # trailing `|| true` keeps that from tripping `set -e`.
    tmp="$(mktemp)"; grep -vE "^$key=" "$file" > "$tmp" || true; mv "$tmp" "$file"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$file"
}

# DGN-1719: carry the owner's STABLE profile facts (Name, timezone, job, ...)
# from the calling instance's owner file into the newborn's, and record that
# instance as the newborn's leader so onboarding Q3 can read the leader
# persona's address term (routines/lib/q3_candidates.py). Without this the
# newborn's owner file is an empty scaffold, Q3 derives fewer than two
# candidates and shows no buttons (rehearsal 2026-09-26 item B8). The address
# term itself is NEVER carried into the owner file -- it stays a Q3 answer.
# No --instance-root -> explicit SKIP, owner file unchanged. Idempotent.
_carry_owner_profile() {
  local child="$1" tag="$2"
  if [[ -z "$INSTANCE_ROOT" ]]; then
    echo "[$tag] SKIP owner-profile carry + leader record (--instance-root not supplied): newborn owner file unchanged" >&2
    return 0
  fi
  "$PACK_PY3" "$SCRIPT_DIR/lib/owner_carry.py" --parent "$INSTANCE_ROOT" --child "$child" \
    | sed "s/^/[$tag] /" \
    || echo "[$tag] WARN owner-profile carry failed -- newborn owner file left as minted" >&2
  _conf_upsert "$child/.instance.conf" DOGANY_LEADER_ROOT "$INSTANCE_ROOT"
  echo "[$tag] leader recorded for onboarding Q3: DOGANY_LEADER_ROOT=$INSTANCE_ROOT"
}

# _gate_stop <gate-id> <operator reason...> -- DGN-1506 item 3, this
# script's own seat (pack_install.sh holds the original; SAME contract, not
# a second doctrine: record WHICH planned gate fired as a machine fact, then
# stop). When the pipeline transaction exported DOGANY_GATE_STATE_FILE, the
# marker is written and the pipeline EXIT trap reads it back to send the
# gate's user_hint_ko (gates.json) through the leader's push.sh. A bare
# invocation (no marker env: dry-run, direct operator call) just stops loud
# -- the operator/model is the audience then. Registered ids are
# cross-checked by scripts/user-gate-lint.py (C2/C4).
_gate_stop() {
  local gid="$1"; shift
  if [[ -n "${DOGANY_GATE_STATE_FILE:-}" ]]; then
    printf 'gate_id=%s\noperator_reason=%s\n' "$gid" "$*" \
      > "$DOGANY_GATE_STATE_FILE" 2>/dev/null \
      || echo "WARN: gate marker unwritable ($DOGANY_GATE_STATE_FILE) -- stop proceeds, hint lane degrades to mint.fault" >&2
  fi
  echo "ERROR: stop at gate '$gid': $*" >&2
  exit 1
}

# DGN-1621: called after read-only preflight gates, before transaction setup.
# Preserve the real pipeline notification marker so channel stops reach leg (b).
# Operator/dry-run resolution retains its no-marker behavior.
_resolve_mint_channel() {
  local DOGANY_GATE_STATE_FILE="${DOGANY_GATE_STATE_FILE:-}"
  if [[ "$MODE" != pipeline || "$DRY" -ne 0 ]]; then
    DOGANY_GATE_STATE_FILE=""
  fi
  if [[ "$MODE" == "mint" || "$MODE" == "pipeline" ]]; then
    if [[ "$CHANNEL_SET" -eq 0 ]]; then
      if [[ -z "$INSTANCE_ROOT" ]]; then
        # DGN-1621 was an ORDERED dev mint seeding release; that path always
        # has an ordering instance. Operator runs have no fleet to diverge
        # from, and DGN-1608 likewise declines channel-coherence judgment
        # without a readable ordering manifest. Blocking breaks that lane.
        echo "[mint_run] SKIP channel measurement (--instance-root not supplied): no ordering instance supplied, so no channel could be measured; mint.sh defaults apply (explicit > kept declaration > release)" >&2
        return 0
      fi
      CHANNEL_OPT="$(conf_get "$INSTANCE_ROOT/.instance.conf" DOGANY_UPDATE_CHANNEL)"
      if [[ -z "$CHANNEL_OPT" ]]; then
        # Undeclared orderers resolve to release too; there is no divergence.
        echo "[mint_run] SKIP channel measurement (ordering instance has no non-empty DOGANY_UPDATE_CHANNEL): mint.sh defaults apply (explicit > kept declaration > release)" >&2
        return 0
      fi
    fi
  fi
  if [[ "$CHANNEL_SET" -eq 1 || -n "$CHANNEL_OPT" ]]; then
    case "$CHANNEL_OPT" in
      release|dev|staging|main) : ;;
      *) _gate_stop mint.channel-invalid "invalid --channel '$CHANNEL_OPT' (valid: release|dev|staging|main)" ;;
    esac
  fi
}

# Keep explicit-channel validation for non-mint verbs (including verdict).
if [[ "$MODE" != mint && "$MODE" != pipeline ]]; then
  _resolve_mint_channel
fi

# ---- viability verdict (DGN-1606) -------------------------------------------
# Answers ONE question after a mint believes it is done: did this newborn's
# bridge actually come up? Measured defect (DGN-1606): four of seven mints of
# one agent died with ZERO bridge heartbeats while every surface stayed
# silent -- the pipeline printed DONE right after `launchctl bootstrap`
# (which proves only that launchd ACCEPTED the job, never that a process
# runs), the delivery-census bot_start entry is verdict=deferred
# (pack_install.sh, DGN-1467 grill #step10 -- an attempt record, not a
# liveness check), the watchdog skips FOREVER while the heartbeat file is
# missing (bridge/watchdog.sh "bridge may not have started yet"), and no
# estate sweep judges a root it does not know (two of the dead mints were
# born outside every registry-resolving scan's world).
#
# The verdict applies the estate's ALREADY-SHIPPED liveness criterion once,
# at birth: the bridge writes .telegram_bot/poll_heartbeat at "Bot is
# running" (bridge/bot.py) and on every getUpdates round trip
# (bridge/heartbeat.py); watchdog.sh (STALE_S) and promote.sh gate 3 both
# define "bridge alive" as a fresh mtime on that file. No new liveness
# doctrine is invented here.
#
# REPORT-ONLY by owner ruling: a failed newborn stays on disk exactly as it
# is for a human to inspect (a deletion was explicitly HELD for that reason
# on 2026-09-20). This code never tears down, never restarts, never repairs
# -- every probe below is read-only.

# Poll cadence: the bridge throttles heartbeat disk writes to one per 5s
# (bridge/heartbeat.py _WRITE_THROTTLE_S), so polling faster buys nothing.
MINT_VERDICT_POLL_S="${MINT_VERDICT_POLL_S:-5}"

_mv_mtime() { # <file> -- epoch seconds, empty when unavailable (BSD/GNU stat)
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true
}

# mint_verdict_stale_s <root> -- the freshness window, extracted from the
# instance's OWN shipped watchdog (promote.sh:117 precedent: one constant,
# never a second copy). Extraction failure degrades LOUD to the shipped
# default (180) instead of fail-closing: the verdict must never become a new
# way to stop a healthy mint.
mint_verdict_stale_s() {
  local s
  s="$(sed -n 's/^STALE_S=\([0-9][0-9]*\).*/\1/p' "$1/bridge/watchdog.sh" 2>/dev/null | head -1)"
  if [[ -z "$s" ]]; then
    echo "[verdict] WARN: STALE_S not extractable from $1/bridge/watchdog.sh -- using the shipped default (180s)" >&2
    s=180
  fi
  printf '%s' "$s"
}

# mint_process_identity_ok <root> -- DGN-1683 WP5 (--process only; without the
# flag it is a no-op pass so every existing caller is unchanged). The heartbeat
# file proves a beat was WRITTEN, not that a live process of THIS newborn wrote
# it. Identity = the launchd bridge unit of this root is running under a pid
# that is alive AND whose command line carries "--path <root>" (the exact
# argument start.sh / `python -m bridge` receive). A copied heartbeat, a
# previous life's beat, or another agent's process cannot satisfy it.
# MINT_VERDICT_LAUNCHCTL overrides the launchctl binary (hermetic tests).
_mv_bridge_label() { # <root> -- Label of the plist that runs bridge/start.sh for <root>
  local p
  for p in "$1"/bridge/*.plist; do
    [[ -e "$p" ]] || continue
    "${PACK_PY3:-python3}" - "$p" "$1" <<'PY' && return 0
import plistlib, sys
try:
    d = plistlib.load(open(sys.argv[1], "rb"))
except Exception:
    sys.exit(1)
a = [str(x) for x in d.get("ProgramArguments", [])]
if any(x.endswith("bridge/start.sh") for x in a) and "--path" in a \
        and a[a.index("--path") + 1:a.index("--path") + 2] == [sys.argv[2]]:
    print(d.get("Label", "")); sys.exit(0 if d.get("Label") else 1)
sys.exit(1)
PY
  done
  return 1
}
_MV_PROC_WHY=""
mint_process_identity_ok() {
  local root="$1" label out pid cmd lc="${MINT_VERDICT_LAUNCHCTL:-launchctl}"
  [[ "$VERDICT_PROCESS" -eq 1 ]] || return 0
  _MV_PROC_WHY=""
  root="${root%/}"
  label="$(_mv_bridge_label "$root")" || { _MV_PROC_WHY="no bridge plist under $root/bridge names this root (--path $root)"; return 1; }
  out="$("$lc" print "gui/$(id -u)/$label" 2>/dev/null)" || { _MV_PROC_WHY="launchd unit $label is not loaded"; return 1; }
  grep -Eq '^[[:space:]]*state = running' <<<"$out" || { _MV_PROC_WHY="launchd unit $label is loaded but not running"; return 1; }
  pid="$(sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)[[:space:]]*$/\1/p' <<<"$out" | head -1)"
  [[ -n "$pid" ]] || { _MV_PROC_WHY="launchd unit $label reports no pid"; return 1; }
  kill -0 "$pid" 2>/dev/null || { _MV_PROC_WHY="pid $pid of $label is not alive"; return 1; }
  cmd="$(ps -p "$pid" -o command= 2>/dev/null)"
  case "$cmd " in
    *"--path $root "*) return 0 ;;
  esac
  _MV_PROC_WHY="pid $pid does not belong to this root (command line lacks '--path $root')"
  return 1
}

# mint_viability_verdict <root> <since_epoch> <timeout_s>
#   confirmed = poll_heartbeat exists AND mtime >= since_epoch  (since > 0:
#               the caller recorded the epoch just before starting the bot,
#               so any beat at/after it proves THIS life ran -- a stale beat
#               from a previous occupant can never pass), OR
#             = poll_heartbeat exists AND age < STALE_S          (since = 0:
#               standalone probe -- same freshness predicate as promote.sh
#               gate 3 / watchdog.sh).
#   rc 0 = confirmed alive; rc 30 = NOT confirmed within timeout_s (with a
#   read-only diagnosis on stderr). Waits in MINT_VERDICT_POLL_S steps so a
#   slow-but-fine start inside the window is never called dead: measured
#   healthy start = 3.05s from bridge init to "Bot is running" (= the first
#   heartbeat write, bot.py), so the default window (STALE_S=180s) is ~60x
#   the measured latency.
mint_viability_verdict() {
  local root="$1" since="$2" timeout="$3"
  local hb="$root/.telegram_bot/poll_heartbeat"
  local stale deadline now m age
  stale="$(mint_verdict_stale_s "$root")"
  now="$(date +%s)"; deadline=$((now + timeout))
  while :; do
    if [[ -f "$hb" ]]; then
      m="$(_mv_mtime "$hb")"
      if [[ -n "$m" ]]; then
        if [[ "$since" -gt 0 ]]; then
          age=$(( $(date +%s) - m ))
          # WP5 --fresh: since>0 alone accepts a beat that was fresh once and
          # then stopped; the beat must ALSO be inside the freshness window NOW.
          if [[ "$m" -ge "$since" ]] && { [[ "$VERDICT_FRESH" -ne 1 ]] || [[ "$age" -lt "$stale" ]]; } \
             && mint_process_identity_ok "$root"; then
            echo "[verdict] ALIVE -- bridge heartbeat observed $(( m - since ))s after start (poll_heartbeat mtime at/after the start epoch; writer: bridge 'Bot is running' + getUpdates round trips)"
            return 0
          fi
        else
          age=$(( $(date +%s) - m ))
          if [[ "$age" -lt "$stale" ]] && mint_process_identity_ok "$root"; then
            echo "[verdict] ALIVE -- bridge heartbeat fresh (age ${age}s < ${stale}s, the watchdog staleness criterion)"
            return 0
          fi
        fi
      fi
    fi
    now="$(date +%s)"
    [[ "$now" -lt "$deadline" ]] || break
    sleep "$MINT_VERDICT_POLL_S"
  done

  # NOT confirmed: read-only diagnosis, then hand the verdict to the caller.
  echo "[verdict] NOT CONFIRMED -- no bridge heartbeat within ${timeout}s" >&2
  if [[ ! -f "$hb" ]]; then
    echo "[verdict]   poll_heartbeat MISSING ($hb): the bridge never reached 'Bot is running' (that moment writes the first beat)" >&2
  else
    m="$(_mv_mtime "$hb")"
    echo "[verdict]   poll_heartbeat PRESENT but not attributable to this start (mtime=${m:-unreadable}, since=$since, stale-window=${stale}s) -- a previous life may have written it" >&2
  fi
  if [[ "$VERDICT_PROCESS" -eq 1 ]]; then
    mint_process_identity_ok "$root" || echo "[verdict]   process identity FAILED: $_MV_PROC_WHY" >&2
  fi
  if [[ "$VERDICT_FRESH" -eq 1 && -f "$hb" ]]; then
    echo "[verdict]   fresh mode: heartbeat must ALSO be younger than ${stale}s now (age=$(( $(date +%s) - $(_mv_mtime "$hb") ))s)" >&2
  fi
  local uid label p loaded=0 notloaded=0
  if command -v launchctl >/dev/null 2>&1; then
    uid="$(id -u)"
    for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
      [[ -e "$p" ]] || continue
      label="$(plist_label "$p")" || {
        echo "[verdict]   launchd unit skipped: cannot determine Label from $p" >&2
        notloaded=$((notloaded + 1))
        continue
      }
      if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
        loaded=$((loaded + 1))
      else
        notloaded=$((notloaded + 1))
      fi
    done
    echo "[verdict]   launchd units: $loaded loaded, $notloaded not loaded (a loaded unit is NOT a running bridge -- bootstrap acceptance is exactly the false 'ready' this verdict exists to replace)" >&2
  else
    echo "[verdict]   launchd units: not judged (launchctl unavailable on this host)" >&2
  fi
  if [[ -f "$root/.telegram_bot/logs/bot.log" ]]; then
    echo "[verdict]   bot.log last lines:" >&2
    tail -5 "$root/.telegram_bot/logs/bot.log" 2>/dev/null | sed 's/^/[verdict]     /' >&2
  else
    echo "[verdict]   bot.log MISSING ($root/.telegram_bot/logs/bot.log): the bridge process likely never ran at all" >&2
  fi
  echo "[verdict]   NOTHING torn down, restarted or repaired -- the instance stays on disk as-is for a human to inspect (DGN-1606 owner ruling)" >&2
  return 30
}

# ---- born-alive-and-wrong verdict (DGN-1608) --------------------------------
# The viability verdict above answers "did a bridge come up"; it has nothing
# to say about a newborn that is alive AND defective. Measured (2026-09-19):
# a mint was alive with four defects -- FW_TAG absent, channel seeded
# 'release' on a dev fleet, never registered in the estate ledger, 3.3MB of
# the predecessor's unrelated knowledge warehouse shipped unwired -- and no
# surface said a word; a human found it five days later by reading
# .instance.conf by hand. This verdict covers the two CHEAP, MACHINE-
# DECIDABLE members of that set:
#   (1) framework identity stamps: .instance.conf must exist and carry
#       non-empty DOGANY_FW_TAG and DOGANY_FW_REPO_SHA lines, and the two
#       may not BOTH be the literal 'unknown' (FW_TAG absent wedged the
#       first framework update on the drift guard; DGN-1605 (b)'s fallback
#       reads FW_REPO_SHA, so at least one real value must exist).
#   (2) update-channel coherence: the newborn's DOGANY_UPDATE_CHANNEL must
#       equal the resolved --channel (explicit override or inherited value).
#       Standalone verdict without --channel compares the ordering manifest.
#       Judged when a resolved channel or ordering manifest is available --
#       skipped LOUD otherwise, never silently passed.
# NOT covered, stated so nobody reads coverage into silence: estate-registry
# registration (DGN-1603 owns that repair) and predecessor knowledge-
# warehouse residue (no cheap machine predicate separates residue from an
# intended carry-over).
# rc 0 = checks passed (this is what EARNS `clean`); rc 31 = alive-but-wrong,
# findings on stderr. READ-ONLY: same owner ruling as the viability verdict
# -- the instance is alive and keeps running exactly as born.
mint_wrongness_verdict() {
  local root="$1" iroot="${2:-}" bad=0 conf="$1/.instance.conf" tag sha ch pch
  if [[ ! -f "$conf" ]]; then
    echo "[verdict] WRONG -- .instance.conf missing ($conf): the newborn carries no manifest at all" >&2
    echo "[verdict]   NOTHING torn down, restarted or repaired -- the instance is ALIVE and stays exactly as born (owner ruling 2026-09-20)" >&2
    return 31
  fi
  tag="$(conf_get "$conf" DOGANY_FW_TAG)"
  sha="$(conf_get "$conf" DOGANY_FW_REPO_SHA)"
  if [[ -z "$tag" ]]; then
    echo "[verdict] WRONG -- DOGANY_FW_TAG absent/empty in $conf (2026-09-19 shape: the first framework update wedges on the drift guard)" >&2
    bad=1
  fi
  if [[ -z "$sha" ]]; then
    echo "[verdict] WRONG -- DOGANY_FW_REPO_SHA absent/empty in $conf" >&2
    bad=1
  fi
  if [[ "$tag" == "unknown" && "$sha" == "unknown" ]]; then
    echo "[verdict] WRONG -- DOGANY_FW_TAG and DOGANY_FW_REPO_SHA are BOTH 'unknown': no drift baseline exists (the DGN-1605 (b) fallback has nothing to fall back to)" >&2
    bad=1
  fi
  if [[ "$VERDICT_STRICT" -eq 1 ]]; then
    # DGN-1683 WP5: "NOT judged" is not a success. The expected channel is the
    # canonical resolution (explicit > ordering declaration > release default,
    # gates.json: undeclared orderers share the release default) and the
    # newborn must carry exactly it. No resolvable basis at all is a failure.
    pch="$CHANNEL_OPT"
    if [[ -z "$pch" && -n "$iroot" && -f "$iroot/.instance.conf" ]]; then
      pch="$(conf_get "$iroot/.instance.conf" DOGANY_UPDATE_CHANNEL)"
      pch="${pch:-release}"
    fi
    ch="$(conf_get "$conf" DOGANY_UPDATE_CHANNEL)"
    if [[ -z "$pch" ]]; then
      echo "[verdict] WRONG -- strict: no channel basis (no --channel and no ordering-instance manifest); an unjudged channel is not a pass" >&2
      bad=1
    elif [[ "$ch" != "$pch" ]]; then
      echo "[verdict] WRONG -- update channel '${ch:-<absent>}' diverges from expected channel '$pch' (strict: resolver result recorded, newborn compared)" >&2
      bad=1
    else
      echo "[verdict] channel judged (strict): newborn '$ch' == expected '$pch'"
    fi
  elif [[ -n "$CHANNEL_OPT" || ( -n "$iroot" && -f "$iroot/.instance.conf" ) ]]; then
    pch="$CHANNEL_OPT"
    [[ -n "$pch" ]] || pch="$(conf_get "$iroot/.instance.conf" DOGANY_UPDATE_CHANNEL)"
    ch="$(conf_get "$conf" DOGANY_UPDATE_CHANNEL)"
    if [[ -z "$pch" ]]; then
      echo "[verdict] channel coherence NOT judged (ordering instance has no non-empty DOGANY_UPDATE_CHANNEL) -- stated, never silently passed" >&2
    elif [[ "$ch" != "$pch" ]]; then
      echo "[verdict] WRONG -- update channel '${ch:-<absent>}' diverges from expected channel '$pch' (explicit --channel or ordering instance measurement)" >&2
      bad=1
    fi
  else
    echo "[verdict] channel coherence NOT judged (no ordering-instance manifest to compare against) -- stated, never silently passed" >&2
  fi
  if [[ "$bad" -ne 0 ]]; then
    echo "[verdict]   NOTHING torn down, restarted or repaired -- the instance is ALIVE and stays exactly as born (owner ruling 2026-09-20)" >&2
    return 31
  fi
  echo "[verdict] WRONGNESS CHECKS PASSED -- fw stamps present, channel coherent where judged (covered: fw stamps, channel; not covered: estate registry, knowledge residue)"
  return 0
}

# KEEP-IN-SYNC (DGN-590): identical copies in install.sh / update.sh /
# scripts/pack/mint_run.sh -- tests/dgn813_bridge_models_allplan_selftest.sh
# asserts the 3 copies are byte-identical and always emit the 4-model list.
# DGN-813 (2026-09-04 correction): fable is exposed in the /model PICKER LIST
# on every plan (pro/team_premium/max_*), NOT set as anyone's DEFAULT selected
# model.
plan_bridge_models() {
  case "$1" in
    *) printf 'fable,opus,sonnet,haiku' ;;  # DGN-346 fable-first, DGN-813 all-plan list
  esac
}

# _stamp_domain_class <root> -- mirrors install.sh dgn227_postmint (domain arm).
# mint_run mints ONLY domain staff agents, so class is unconditionally domain.
# Seals the generator hole where mint_run children never received a class stamp
# (would silently reclassify to main under the DGN-590 class gate) and lifekit
# stayed pending (unwanted lifekit offer re-production).
_stamp_domain_class() {  # mirrors install.sh dgn227_postmint (domain arm)
  local root="$1"
  _conf_upsert "$root/.instance.conf" DOGANY_AGENT_CLASS "domain"
  # DGN-1676: kit conf resolved (existing kit_life.conf | lifekit.conf, else
  # the current name) by the framework resolver; lib absent -> legacy literal.
  local lk_conf="" lk_lib="$SCRIPT_DIR/../../agents/.template/routines/lib/life-engine.sh"
  if [[ -f "$lk_lib" ]]; then
    lk_conf="$( { . "$lk_lib" && life_engine_conf_write_path "$root"; } 2>/dev/null || true)"
  fi
  lk_conf="${lk_conf:-$root/config/lifekit.conf}"
  local lk; lk="$(conf_get "$lk_conf" LIFEKIT)"
  if [[ "$lk" != "on" && "$lk" != "off" ]]; then
    _conf_upsert "$lk_conf" LIFEKIT "off"
  fi
  # DGN-1406 (owner approved 2026-09-12, revises DGN-590 "domain is kitless
  # by construction" to "kitless by default, DOGANY_KIT preset can seed a
  # kit"): mirrors install.sh dgn227_postmint's preset override on the
  # mint_run domain path. Unset/none/unrecognized -> leave KIT= unwritten,
  # which is BYTE-IDENTICAL to pre-DGN-1406 behavior (this function never
  # wrote KIT= at all; onboarding-check.py's resolve_kit() reads an absent
  # key as "none", the same fail-safe default).
  if [[ -n "${DOGANY_KIT:-}" && "$DOGANY_KIT" != "none" ]]; then
    case "$DOGANY_KIT" in
      lifekit|bizkit)
        if ! grep -q "^KIT=" "$root/config/agent.conf" 2>/dev/null; then
          _conf_upsert "$root/config/agent.conf" KIT "$DOGANY_KIT"
        fi
        ;;
      *)
        printf '[WARN] DOGANY_KIT="%s" is not a recognised kit; leaving domain kitless\n' \
          "$DOGANY_KIT" >&2
        ;;
    esac
  fi
}

# _stamp_pack_kit <root> <pack-manifest.json> -- DGN-1826 F4: a domain whose
# pack declares requires_kit runs ON that kit, so it carries KIT=<kit> in
# config/agent.conf like any kit-holding instance (the life-setup class gate,
# onboarding-check.py kit readers). The pack manifest's requires_kit.kit is
# the ONE source -- a pack never duplicates it as a KIT= line in its own
# agent.conf.add. Only when KIT= is absent: a DOGANY_KIT preset (stamped by
# _stamp_domain_class at mint time) or any earlier value wins. Recognised
# kits = the values KIT= readers understand (the life kit under both stems,
# bizkit); anything else -> WARN, the domain stays kitless (no KIT= line).
# No manifest / no requires_kit -> no-op. Never fatal.
_stamp_pack_kit() {
  local root="$1" manifest="$2" kit
  [[ -f "$manifest" ]] || return 0
  kit="$("${PACK_PY3:-python3}" -c "
import json, sys
try:
    rk = json.load(open(sys.argv[1])).get('requires_kit')
except Exception:
    rk = None
v = rk.get('kit') if isinstance(rk, dict) else None
print(v if isinstance(v, str) else '')
" "$manifest" 2>/dev/null || true)"
  [[ -n "$kit" ]] || return 0
  if grep -q "^KIT=" "$root/config/agent.conf" 2>/dev/null; then
    echo "[mint_run] KIT= already set in agent.conf -- pack requires_kit '$kit' not stamped (existing value wins)"
    return 0
  fi
  case "$kit" in
    kit_life|lifekit|bizkit)
      _conf_upsert "$root/config/agent.conf" KIT "$kit"
      echo "[mint_run] KIT=$kit stamped from the pack's requires_kit"
      ;;
    *)
      printf '[WARN] pack requires_kit "%s" is not a recognised kit; leaving domain kitless\n' "$kit" >&2
      ;;
  esac
  return 0
}

_canon() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }

# ---- G3-b alignment (DGN-227 / DGN-422) ------------------------------------
# _g3b_submit_gate <main_root> -- E3 two-stage gate mirror (install.sh
# submit_flip_gate). Stage 1: main briefing unit loaded (launchd query, or the
# DOGANY_GATE_LOADED_OVERRIDE testing shim). Stage 2: aggregation-capable
# edition (routines/lib/handoff-aggregate present AND a briefing script consumes
# it). Returns 0 = flip allowed.
_g3b_submit_gate() {
  local main_root="$1" loaded=1
  if [[ -n "${DOGANY_GATE_LOADED_OVERRIDE:-}" ]]; then
    [[ "$DOGANY_GATE_LOADED_OVERRIDE" = "1" ]] && loaded=0
  else
    local uid lbl p; uid="$(id -u)"
    for p in "$main_root"/routines/*generic-brief-morning*.plist \
             "$main_root"/routines/*morning-brief*.plist; do
      [[ -e "$p" ]] || continue
      lbl="$(plist_label "$p")" || {
        echo "  [gate] cannot determine launchd Label from $p -- submit flip blocked" >&2
        return 1
      }
      if launchctl print "gui/$uid/$lbl" >/dev/null 2>&1; then loaded=0; break; fi
    done
  fi
  [[ "$loaded" -eq 0 ]] || { echo "  [gate] main briefing unit not loaded -- submit flip blocked" >&2; return 1; }
  [[ -e "$main_root/routines/lib/handoff-aggregate" ]] || {
    echo "  [gate] aggregation library absent -- submit flip blocked" >&2; return 1; }
  local edition_ok=1 s
  for s in "$main_root/routines/generic-brief.sh" "$main_root"/routines/bundle/*.sh; do
    [[ -f "$s" ]] || continue
    if grep -q "handoff-aggregate" "$s" 2>/dev/null; then edition_ok=0; break; fi
  done
  [[ "$edition_ok" -eq 0 ]] || { echo "  [gate] loaded briefing script not aggregation-capable -- submit flip blocked" >&2; return 1; }
  return 0
}

# _g3b_display_name <root> -- the owner-facing agent name (DGN-1744): the
# single resolver agents/.template/routines/lib/agent_name.py (identity
# "- Name:" -> DOGANY_AGENT_LABEL -> DOGANY_AGENT_NAME -> slug). The conf-only
# chain below is the fallback when the resolver cannot run.
_g3b_display_name() {
  local root="$1" v
  v="$(python3 "$MACHINERY_REPO/agents/.template/routines/lib/agent_name.py" "$root" 2>/dev/null || true)"
  [[ -n "$v" ]] || v="$(conf_get "$root/.instance.conf" DOGANY_AGENT_LABEL)"
  [[ -n "$v" ]] || v="$(conf_get "$root/.instance.conf" DOGANY_AGENT_NAME)"
  [[ -n "$v" ]] || v="$(basename "$root")"
  printf '%s' "$v"
}

# g3b_align_domain <domain_root> <main_root> <slug> -- fresh domain-add landing
# on the same state surface as the install-time G2 main-add flow (spec G3-b):
#   (a) registry register domain + main; (b) limit ledger cover the count;
#   (c) routing keys HANDOFF_PEER_MAIN + BRIEF_ROUTING, gated (fail=standalone).
# NEVER writes MIGRATION_PEER -- fresh mint, briefing axis only (E2-1).
g3b_align_domain() {
  local domain_root="$1" main_root="$2" slug="$3"
  local dogany_dir="$HOME/.dogany"
  local registry="$dogany_dir/instances"
  local global_conf="$dogany_dir/config"
  local dcanon mcanon; dcanon="$(_canon "$domain_root")"; mcanon="$(_canon "$main_root")"
  mkdir -p "$dogany_dir"; touch "$registry"

  # (a) registry: register the new domain + the main it attaches to (idempotent).
  local tmp; tmp="$(mktemp)"
  awk -F'\t' -v d="$dcanon" -v m="$mcanon" '$2 != d && $2 != m' "$registry" > "$tmp" || true
  printf 'domain\t%s\n' "$dcanon" >> "$tmp"
  printf 'main\t%s\n' "$mcanon" >> "$tmp"
  mv "$tmp" "$registry"
  echo "[g3b] registry: domain + main registered"

  # (b) limit ledger: the v2 path never read the limit before (G2-1 asymmetry).
  # Persist DOGANY_MAX_AGENTS to cover the registry count (>= current entries).
  local n; n="$(wc -l < "$registry" | tr -d ' ')"
  local cur; cur="$(conf_get "$global_conf" DOGANY_MAX_AGENTS)"
  if [[ -z "$cur" || "$cur" -lt "$n" ]]; then
    _conf_upsert "$global_conf" DOGANY_MAX_AGENTS "$n"
    echo "[g3b] limit ledger: DOGANY_MAX_AGENTS=$n"
  fi

  # (c) peer register on main (display-name capture) + routing flip (gated).
  local disp; disp="$(_g3b_display_name "$domain_root")"
  local mconf="$main_root/config/agent.conf"; touch "$mconf"
  local peers; peers="$(conf_get "$mconf" BRIEF_PEERS)"
  case ",$peers," in
    *",$dcanon|"*) : ;;
    *) if [[ -n "$peers" ]]; then peers="$peers,$dcanon|$disp"; else peers="$dcanon|$disp"; fi
       _conf_upsert "$mconf" BRIEF_PEERS "$peers" ;;
  esac

  local dconf="$domain_root/config/agent.conf"; touch "$dconf"
  if _g3b_submit_gate "$main_root"; then
    # HANDOFF_PEER_MAIN = briefing-topology key (E2-1); MIGRATION_PEER NEVER set.
    _conf_upsert "$dconf" HANDOFF_PEER_MAIN "$mcanon"
    _conf_upsert "$dconf" BRIEF_ROUTING "submit"
    echo "[g3b] routing: domain -> submit (gate passed; briefing axis only)"
  else
    _conf_upsert "$dconf" BRIEF_ROUTING "standalone"
    echo "[g3b] routing: gate failed -> domain stays standalone (no loss)"
  fi
}

# ---- role stamp: SINGLE canonical implementation (DGN-227 A3/P15) ----------
# _stamp_role <root> <prose> [log-prefix] [kit-pending]
# Stamps the Role Primary-focus prose into <root>/AGENT.md, excises the
# onboarding Q6 block and inserts the do-not-ask line. Idempotent (guard
# inside the python block). Three entry points call this ONE function:
#   (1) mint mode post-mint step, (2) recover mode journal replay,
#   (3) the stamp-role subcommand.
# Kit-pending (4th arg = 1, prose empty): a kitless main has no role yet --
# the role line belongs to the kit, which stamps it through this same
# subcommand when it is turned on. Only Q6 is excised (the kit-select offer
# after onboarding is the role decision); the Primary-focus placeholder is
# left for the kit. A later --role stamp fills the slot whether onboarding is
# still pending (kit-pending line swapped for the do-not-ask line) or done
# (onboarding block already gone: slot only).
# Returns non-zero on stamp failure; missing AGENT.md is a WARN + return 0
# (preserves the legacy per-caller skip semantics).
_stamp_role() {
  local root="$1" prose="$2" prefix="${3:-role-stamp}" kit_pending="${4:-0}"
  local agent_md
  agent_md="$(resolve_persona_md "$root")"
  if [[ ! -f "$agent_md" ]]; then
    echo "[$prefix] WARN AGENT.md not found at $agent_md; skipping role stamp" >&2
    return 0
  fi
  "$PACK_PY3" - "$agent_md" "$prose" "$prefix" "$root" "$INSTANCE_ROOT" "$MACHINERY_REPO/agents/.template/routines/lib" "$kit_pending" <<'PYEOF'
import sys, re, pathlib

agent_md = pathlib.Path(sys.argv[1])
role_prose = sys.argv[2]
prefix = sys.argv[3]
kit_pending = sys.argv[7] == "1"
sys.path.insert(0, sys.argv[6])
if not kit_pending:
    from role_prose import validate_role_prose
    try:
        validate_role_prose(role_prose, sys.argv[4], sys.argv[5])
    except ValueError as exc:
        print(f"[{prefix}] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
text = agent_md.read_text(encoding="utf-8")

MARKER = "<!-- ONBOARDING_PENDING -->"
placeholder = "(set at onboarding -- one prose line naming the main hat;"
do_not_ask = (
    "Do NOT ask about your role -- it is already set"
    " (filled at mint; see Role section)."
)
kit_pending_line = (
    "Do NOT ask about your role -- no kit is on yet; leave the Primary focus"
    " slot as it is (a kit sets it when it is turned on)."
)
keep_marker = "\nKeep each question"
q6_pattern = re.compile(
    r'\n  6\. role\s+--\s+LAST\b.*?(?=\nKeep each question)',
    re.DOTALL,
)


def excise_q6(t):
    # Excise Q6 bullet (the "6. role -- LAST[ + CONDITIONAL]: ..." block).
    # The header may read "LAST:" or "LAST + CONDITIONAL:" (DGN-422 conditional
    # retention wording) -- match through "LAST" and tolerate either tail.
    t, q6_count = q6_pattern.subn("", t)
    if q6_count != 1:
        print(f"[{prefix}] ERROR: expected 1 Q6 block match, got {q6_count}; aborting", file=sys.stderr)
        sys.exit(1)
    # Update completion criterion.
    return t.replace(
        "when all are filled (question 6 = the Primary-focus slot filled),",
        "when all 5 are filled,",
    )


# Idempotence guard: if Primary focus is already stamped, do not re-stamp.
if placeholder not in text:
    print(f"[{prefix}] Primary focus already stamped or placeholder absent; skipping (idempotent).", file=sys.stderr)
    sys.exit(0)

if kit_pending:
    if kit_pending_line in text or MARKER not in text:
        print(f"[{prefix}] kit-pending: nothing to do (already marked or onboarding done).", file=sys.stderr)
        sys.exit(0)
    new_text = excise_q6(text)
    new_text = new_text.replace(keep_marker, "\n" + kit_pending_line + keep_marker, 1)
    agent_md.write_text(new_text, encoding="utf-8")
    print(f"[{prefix}] OK: kit-pending -- Q6 excised, Primary focus left for the kit.")
    sys.exit(0)

# 1. Fill Primary-focus placeholder line.
pf_pattern = re.compile(
    r'\(set at onboarding -- one prose line naming the main hat;\s*'
    r'the general front-door bullet above still applies\)',
    re.DOTALL,
)
new_text, count = pf_pattern.subn(lambda match: role_prose, text)
if count != 1:
    print(f"[{prefix}] ERROR: expected 1 Primary-focus placeholder match, got {count}; aborting", file=sys.stderr)
    sys.exit(1)

if MARKER not in new_text:
    # Onboarding already done (kit turned on later): the block is gone, so
    # the slot is the whole stamp.
    agent_md.write_text(new_text, encoding="utf-8")
    print(f"[{prefix}] OK: Primary focus stamped (onboarding already complete).")
    sys.exit(0)

# 2. Excise Q6 -- unless a kit-pending stamp already did; its line then
#    gives way to the do-not-ask line (3).
if kit_pending_line in new_text:
    new_text = new_text.replace(kit_pending_line, do_not_ask, 1)
else:
    new_text = excise_q6(new_text)

# 3. Insert do-not-ask instruction before "Keep each question".
if do_not_ask not in new_text:
    new_text = new_text.replace(
        keep_marker,
        "\n" + do_not_ask + keep_marker,
    )

agent_md.write_text(new_text, encoding="utf-8")
print(f"[{prefix}] OK: Primary focus stamped, Q6 excised, do-not-ask added.")
PYEOF
}

# ---- stamp-role mode: standalone entry point (DGN-227 A3/P15 entry 3) ------
# install.sh calls this on every install path right after mint:
#   mint_run.sh stamp-role --root <root> --role "<prose>"
#   mint_run.sh stamp-role --root <root> --kit-pending   (kitless main)
# A kit that owns a role line stamps it later with the --role form.
if [[ "$MODE" == "stamp-role" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: stamp-role requires --root <dir>" >&2; exit 1; }
  if [[ "$KIT_PENDING" == 1 ]]; then
    [[ -z "$ROLE_PROSE" ]] || { echo "ERROR: stamp-role takes --role or --kit-pending, not both" >&2; exit 1; }
  else
    [[ -n "$ROLE_PROSE" ]] || { echo "ERROR: stamp-role requires --role <prose> (or --kit-pending)" >&2; exit 1; }
  fi
  [[ -d "$ROOT" ]] || { echo "ERROR: stamp-role root not a directory: $ROOT" >&2; exit 1; }
  # MINOR-8: the stamp-role SUBCOMMAND is a hard precondition of install (A3:
  # Q6 excision + role stamp). A missing AGENT.md here means a partial mint --
  # a WARN+skip (as _stamp_role does for the mint/recover callers) would let
  # install proceed with Q6 un-excised and the role un-stamped, silently
  # breaking A3. Fail loud on this entry; mint/recover keep the WARN semantics.
  _stamp_role_persona_md="$(resolve_persona_md "$ROOT")"
  [[ -f "$_stamp_role_persona_md" ]] || {
    echo "ERROR: stamp-role AGENT.md not found at $_stamp_role_persona_md (partial mint) -- refusing to proceed (A3)" >&2
    exit 1
  }
  _stamp_role "$ROOT" "$ROLE_PROSE" "stamp-role" "$KIT_PENDING"
  exit $?
fi

# ---- align-peer-main mode: G3-b alignment entry point (DGN-227 / DGN-422) ---
# Usage: mint_run.sh align-peer-main --root <domain-root> --peer-main <main-root>
#        [--slug <slug>]
# Runs the same alignment the pipeline does at step 4 (registry + limit ledger +
# BRIEFING-axis routing keys), callable on its own for the v2 main-add path when
# the domain is already minted. NEVER writes MIGRATION_PEER (E2-1).
if [[ "$MODE" == "align-peer-main" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: align-peer-main requires --root <domain-root>" >&2; exit 1; }
  [[ -n "$PEER_MAIN" ]] || { echo "ERROR: align-peer-main requires --peer-main <main-root>" >&2; exit 1; }
  [[ -d "$ROOT" ]] || { echo "ERROR: domain root not a directory: $ROOT" >&2; exit 1; }
  [[ -d "$PEER_MAIN" ]] || { echo "ERROR: peer-main root not a directory: $PEER_MAIN" >&2; exit 1; }
  g3b_align_domain "$ROOT" "$PEER_MAIN" "${SLUG:-$(basename "$ROOT")}"
  exit $?
fi

# ---- occupancy mode: is the mint target slot already taken? (DGN-1543 R6) --
# READ-ONLY probe, the entry-side twin of teardown. The predicate and the
# evidence lines live in ONE place -- mint_occupancy_probe in
# scripts/pack/lib/mint_teardown.sh -- shared with the pipeline's hard gate
# below, so the flow-level check and the machine backstop can never disagree.
if [[ "$MODE" == "occupancy" ]]; then
  if [[ -z "$ROOT" && -z "$SLUG" ]]; then
    echo "usage: mint_run.sh occupancy {--slug <slug> | --root <dir>}" >&2
    exit 1
  fi
  # DGN-1483: same derivation mint/pipeline/teardown use -- the probe can
  # never judge a different directory than the mint would write.
  [[ -n "$ROOT" ]] || ROOT="$HOME/.dogany/agents/$SLUG"
  echo "[occupancy] root $ROOT"
  OCCUPANCY="$(mint_occupancy_probe "$ROOT" || true)"
  [[ -n "$OCCUPANCY" ]] && printf '%s\n' "$OCCUPANCY" | sed 's/^/[occupancy] /'
  # DGN-1615: the owner-gated bootout uses THESE targets (plist Labels),
  # never names re-derived from plist file names.
  _mt_bootout_targets "$(printf '%s\n' "$OCCUPANCY" | sed -n 's/^OCCUPIED live /LIVE /p')" \
    | sed 's/^/[occupancy] /'
  if grep -q '^OCCUPIED ' <<< "$OCCUPANCY"; then
    echo "[occupancy] verdict OCCUPIED -- never mint over this; the owner decides between teardown-then-remint (mint_run.sh teardown, gates of its own) and abort"
    exit 10
  fi
  echo "[occupancy] verdict FREE"
  exit 0
fi

# ---- handover-brief mode: the base-kit handover-summary slot (dsp hslot) ---
# Owner rulings 2026-09-18 07:14~07:31 (live mint-flow roleplay):
#   * "어떤 목표가 이관되는지를 알려주자고" + "이건 민팅 전에 안내해야할듯" --
#     BEFORE the bot is created, the flow tells the owner what the new agent
#     will inherit; told afterwards it is a notification, not a choice.
#   * "킷쪽의 자율성을 열어두는 방식으로?" + "베이스킷이 이 구조를 잡아놔야
#     범용적용가능해" -- the framework must NOT read kit tables. It asks one
#     question and renders the answer; the KIT answers.
# The slot: a kit manifest may declare top-level `handover_brief_verb`
# (kind=kit optional, additive -- the completion_verb precedent, packs/
# CONTRACT.md §2). The value names a verb on the kit CLI the kit already
# ships (database/<kit>.sh -- the same CLI surface C6 smokes with
# check/dump). This mode runs the LEADER instance's INSTALLED copy of that
# CLI, because the leader is where today's data lives; the kit reads its
# own DB and answers in owner-facing Korean sentences on stdout.
# Absence doctrine (every arrow below is rc 0 + EMPTY stdout -- the caller
# renders no screen, says nothing, and the flow continues; stderr carries
# the operator note): slot undeclared -> no screen. kit CLI not installed
# on the leader -> no screen. kit answers nothing -> no screen. kit errors
# or exceeds the timeout -> no screen. copy judge refuses -> no screen.
# The judge: kit output is OWNER-FACING copy the framework did not write,
# so it passes scripts/user-gate-lint.py --kit-text (the same C3/C5/C6/C7
# judge as every other owner copy, plus the estate copy rules) BEFORE it
# can render -- judge missing = no screen (unjudged copy never reaches the
# owner). Oversized output = malformed = no screen.
if [[ "$MODE" == "handover-brief" ]]; then
  # All diagnostics on stderr: stdout is the body contract.
  hb_skip() { echo "[handover-brief] no brief: $*" >&2; exit 0; }
  if [[ -z "$PACK_ID" || -z "$INSTANCE_ROOT" ]]; then
    echo "usage: mint_run.sh handover-brief --pack <id> --instance-root <dir> [--catalog <file>]" >&2
    exit 1
  fi

  HB_COORDS_LIB="$SCRIPT_DIR/lib/pack_coords.sh"
  [[ -f "$HB_COORDS_LIB" && -r "$HB_COORDS_LIB" ]] \
    || hb_skip "coordinate resolver missing ($HB_COORDS_LIB)"
  # shellcheck source=lib/pack_coords.sh
  . "$HB_COORDS_LIB"

  # Catalog: same F-1 preference order as pipeline mode -- explicit
  # --catalog, else the CANONICAL repo's (sibling package_dir coordinates
  # resolve there), else this machinery tree's.
  HB_CATALOG="$CATALOG_OPT"
  if [[ -z "$HB_CATALOG" ]]; then
    HB_CANON="$(conf_get "$INSTANCE_ROOT/.instance.conf" DOGANY_REPO_ROOT)"
    if [[ -n "$HB_CANON" && -f "$HB_CANON/packs/catalog.json" ]]; then
      HB_CATALOG="$HB_CANON/packs/catalog.json"
    else
      HB_CATALOG="$MACHINERY_REPO/packs/catalog.json"
    fi
  fi
  [[ -f "$HB_CATALOG" ]] || hb_skip "catalog not found ($HB_CATALOG)"

  # pack -> kit, via CONTRACT surfaces only (provides_kit / requires_kit.kit
  # -- manifest fields, never a kit's schema).
  HB_ROW="$(pack_catalog_row "$HB_CATALOG" "$PACK_ID" 2>/dev/null)" || HB_ROW="null"
  [[ -n "$HB_ROW" && "$HB_ROW" != "null" ]] || hb_skip "pack '$PACK_ID' has no catalog row"
  # DGN-1818: sibling checkout, else the public-install pack-src clone.
  HB_DIR="$(pack_row_dir "$HB_CATALOG" "$HB_ROW")"
  [[ -n "$HB_DIR" && -f "$HB_DIR/pack-manifest.json" ]] \
    || hb_skip "pack '$PACK_ID' manifest unreachable (package_dir -> '${HB_DIR:-<empty>}')"
  HB_KIT="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
if d.get('kind') == 'kit':
    v = d.get('provides_kit')
else:
    rk = d.get('requires_kit')
    v = rk.get('kit') if isinstance(rk, dict) else None
print(v if isinstance(v, str) else '')
" "$HB_DIR/pack-manifest.json" 2>/dev/null || echo "")"
  [[ -n "$HB_KIT" ]] || hb_skip "pack '$PACK_ID' names no kit -- nothing can carry over"

  # The declaration: the KIT's own manifest, top-level handover_brief_verb.
  HB_KIT_ROW="$(pack_catalog_row "$HB_CATALOG" "$HB_KIT" 2>/dev/null)" || HB_KIT_ROW="null"
  [[ -n "$HB_KIT_ROW" && "$HB_KIT_ROW" != "null" ]] || hb_skip "kit '$HB_KIT' has no catalog row"
  HB_KIT_DIR="$(pack_row_dir "$HB_CATALOG" "$HB_KIT_ROW")"
  [[ -n "$HB_KIT_DIR" && -f "$HB_KIT_DIR/pack-manifest.json" ]] \
    || hb_skip "kit '$HB_KIT' manifest unreachable (package_dir -> '${HB_KIT_DIR:-<empty>}')"
  HB_VERB="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get('handover_brief_verb')
print(v.strip() if isinstance(v, str) else '')
" "$HB_KIT_DIR/pack-manifest.json" 2>/dev/null || echo "")"
  [[ -n "$HB_VERB" ]] || hb_skip "kit '$HB_KIT' declares no handover_brief_verb (slot not implemented -- normal)"

  HB_CLI="$INSTANCE_ROOT/database/$HB_KIT.sh"
  [[ -x "$HB_CLI" ]] || hb_skip "kit CLI not installed on the leader ($HB_CLI)"

  # Run the kit's verb -- bounded, degrade-only. DOGANY_HANDOVER_BRIEF_
  # TIMEOUT exists for tests; 10s default matches the hooks contract budget.
  HB_TIMEOUT="${DOGANY_HANDOVER_BRIEF_TIMEOUT:-10}"
  HB_RC=0
  HB_OUT="$("$PACK_PY3" - "$HB_CLI" "$HB_VERB" "$INSTANCE_ROOT" "$HB_TIMEOUT" <<'PYEOF'
import subprocess, sys
try:
    r = subprocess.run([sys.argv[1], sys.argv[2]], cwd=sys.argv[3],
                       capture_output=True, text=True,
                       timeout=float(sys.argv[4]))
except subprocess.TimeoutExpired:
    print("kit verb exceeded the %ss budget" % sys.argv[4], file=sys.stderr)
    sys.exit(124)
except Exception as e:
    print("kit verb could not run: %s" % e, file=sys.stderr)
    sys.exit(125)
sys.stderr.write(r.stderr or "")
if r.returncode != 0:
    sys.exit(r.returncode if 0 < r.returncode < 126 else 1)
sys.stdout.write(r.stdout or "")
PYEOF
)" || HB_RC=$?
  [[ "$HB_RC" -eq 0 ]] || hb_skip "kit verb '$HB_VERB' failed (rc=$HB_RC) -- degrading, never a broken screen"
  [[ -n "${HB_OUT//[[:space:]]/}" ]] || hb_skip "kit '$HB_KIT' answered nothing (nothing carries over)"

  # Size bound: a screen has a reader. Oversize = malformed, not rendered.
  HB_LINES="$(printf '%s\n' "$HB_OUT" | grep -c . || true)"
  HB_CHARS="${#HB_OUT}"
  if [[ "$HB_LINES" -gt 20 || "$HB_CHARS" -gt 2000 ]]; then
    hb_skip "kit answer oversized ($HB_LINES lines / $HB_CHARS chars; bounds 20 lines / 2000 chars) -- malformed, not rendered"
  fi

  # The judge. A kit returning rule-breaking copy must not be able to put
  # it on the owner's screen unchecked -- and no judge means no screen.
  HB_JUDGE="$SCRIPT_DIR/../user-gate-lint.py"
  [[ -f "$HB_JUDGE" ]] || hb_skip "copy judge missing ($HB_JUDGE) -- unjudged kit copy never reaches the owner"
  HB_JRC=0
  HB_FINDINGS="$(printf '%s\n' "$HB_OUT" | "$PACK_PY3" "$HB_JUDGE" --kit-text 2>&1)" || HB_JRC=$?
  if [[ "$HB_JRC" -ne 0 ]]; then
    printf '%s\n' "$HB_FINDINGS" | sed 's/^/[handover-brief] /' >&2
    hb_skip "kit copy refused by the judge (findings above) -- not rendered"
  fi
  echo "[handover-brief] kit '$HB_KIT' verb '$HB_VERB': $HB_LINES line(s), judge clean -- body on stdout" >&2
  printf '%s\n' "$HB_OUT"
  exit 0
fi

# ---- catalog-brief mode: the step-1 catalog branch as machinery (DGN-1575) --
# Measured 2026-09-18 16:08 (live owner session): the owner asked for a
# health trainer verbatim, pack_health carried the literal domain match, the
# SKILL ordered the catalog branch FIRST -- and the flow went straight to
# BotFather without naming the official pack, because the branch lived only
# as prose. This verb makes the presentation DATA: the model runs it and
# renders stdout verbatim (handover-brief precedent). The stamp it writes is
# what the mint.catalog-brief gate (mint/pipeline seats below) requires
# before anything is built -- the same two-seat shape as occupancy
# (flow-level seat here, machine backstop in the build path), sharing ONE
# matcher (lib/catalog_brief.py) so the two can never disagree.
# UNLIKE handover-brief, refused copy does NOT degrade to silence: absence
# of this screen is the incident itself, so a copy violation is rc 4 LOUD.
if [[ "$MODE" == "catalog-brief" ]]; then
  [[ -n "$QUERY_OPT" ]] || {
    echo "usage: mint_run.sh catalog-brief --query <owner words> [--instance-root <dir>] [--catalog <file>]" >&2
    exit 1
  }
  # Catalog: same F-1 preference order as pipeline/handover-brief -- explicit
  # --catalog, else the CANONICAL repo's (what the install will judge by:
  # presenting from the same catalog kills the consume-tree skew for this
  # screen), else this machinery tree's.
  CB_CATALOG="$CATALOG_OPT"
  if [[ -z "$CB_CATALOG" && -n "$INSTANCE_ROOT" ]]; then
    CB_CANON="$(conf_get "$INSTANCE_ROOT/.instance.conf" DOGANY_REPO_ROOT)"
    [[ -n "$CB_CANON" && -f "$CB_CANON/packs/catalog.json" ]] \
      && CB_CATALOG="$CB_CANON/packs/catalog.json"
  fi
  [[ -n "$CB_CATALOG" ]] || CB_CATALOG="$MACHINERY_REPO/packs/catalog.json"
  if [[ ! -f "$CB_CATALOG" ]]; then
    echo "[catalog-brief] no catalog to read ($CB_CATALOG) -- NO-MATCH by absence, freeform confirm" >&2
    exit 3
  fi
  CB_STAMP="" CB_LABEL=""
  if [[ -n "$INSTANCE_ROOT" ]]; then
    CB_STAMP="$INSTANCE_ROOT/config/mint-catalog-brief.json"
    CB_LABEL="$(conf_get "$INSTANCE_ROOT/.instance.conf" DOGANY_USER_LABEL)"
  else
    echo "[catalog-brief] SKIP stamp write (--instance-root not supplied): the step-6 gate will have nothing to read (DGN-366 L1)" >&2
  fi
  CB_ARGS=(brief --catalog "$CB_CATALOG" --query "$QUERY_OPT" --user-label "${CB_LABEL:-님}")
  [[ -n "$CB_STAMP" ]] && CB_ARGS+=(--stamp "$CB_STAMP")
  exec "$PACK_PY3" "$SCRIPT_DIR/lib/catalog_brief.py" "${CB_ARGS[@]}"
fi

# ---- teardown mode: the way back out of a half-finished mint (DGN-1506) ----
# Gates, archive layout and the "nothing is ever deleted" doctrine live in
# scripts/pack/lib/mint_teardown.sh. This block is argument resolution only.
if [[ "$MODE" == "teardown" ]]; then
  # Archive root default sits OUTSIDE every estate scan depth
  # (_discover_instances globs $HOME/.dogany/main/, $HOME/.dogany/agents/*/
  # and $HOME/dogany/*/, never $HOME/.dogany/torn-down/*; a registry line
  # naming a moved-away root drops) so an archived instance can never be
  # picked up again as an update/eval target.
  TEARDOWN_ARCHIVE_ROOT="${ARCHIVE_DIR:-$HOME/.dogany/torn-down}"
  TEARDOWN_HANDOFF_ROOT="${DOGANY_HANDOFF_ROOT:-$HOME/.dogany/handoff}"

  if [[ -n "$RESTORE_DIR" ]]; then
    mint_teardown_restore "$RESTORE_DIR" "$TEARDOWN_HANDOFF_ROOT" "$INSTANCE_ROOT"
    exit $?
  fi

  if [[ -z "$ROOT" && -z "$SLUG" ]]; then
    echo "usage: mint_run.sh teardown {--slug <slug> | --root <dir>} [--instance-root <dir>] [--archive-dir <dir>] [--dry-run]" >&2
    echo "  or:  mint_run.sh teardown --restore <archive-dir> [--instance-root <dir>]" >&2
    exit 1
  fi
  # DGN-1483: agents live under ~/.dogany/agents/<slug>, slug verbatim -- the
  # SAME derivation mint/pipeline use, so teardown can never resolve a
  # different directory than the mint that created it.
  [[ -n "$ROOT" ]] || ROOT="$HOME/.dogany/agents/$SLUG"

  if [[ ! -e "$ROOT" ]]; then
    echo "[teardown] nothing to tear down: $ROOT does not exist"
    exit 0
  fi

  # Ambiguity is a refusal, never a guess: when BOTH coordinates are given and
  # the instance's own stamped SLUG contradicts --slug, stop.
  if [[ -n "$SLUG" ]]; then
    TEARDOWN_STAMPED_SLUG="$(conf_get "$ROOT/config/agent.conf" SLUG)"
    if [[ -n "$TEARDOWN_STAMPED_SLUG" && "$TEARDOWN_STAMPED_SLUG" != "$SLUG" ]]; then
      echo "[teardown] REFUSE: --slug '$SLUG' contradicts the instance's stamped SLUG '$TEARDOWN_STAMPED_SLUG' at $ROOT" >&2
      exit 1
    fi
  fi

  if [[ "$DRY" -eq 1 ]]; then
    mint_teardown_plan "$ROOT" "$TEARDOWN_ARCHIVE_ROOT" "$INSTANCE_ROOT" "$TEARDOWN_HANDOFF_ROOT" \
      | sed 's/^PLAN /[teardown] (dry-run) would: /'
    TEARDOWN_RC=${PIPESTATUS[0]}
    [[ "$TEARDOWN_RC" -eq 0 ]] && echo "[teardown] DRY-RUN DONE -- nothing moved"
    exit "$TEARDOWN_RC"
  fi

  mint_teardown_apply "$ROOT" "$TEARDOWN_ARCHIVE_ROOT" "$INSTANCE_ROOT" "$TEARDOWN_HANDOFF_ROOT"
  TEARDOWN_RC=$?
  if [[ "$TEARDOWN_RC" -eq 0 ]]; then
    echo "[teardown] DONE -- the instance was archived, not deleted; crew registration (if any) was removed with an on-disk crew.conf backup (DGN-1543 R8); restore with 'mint_run.sh teardown --restore <archive-dir>'"
  fi
  exit "$TEARDOWN_RC"
fi

# ---- recover mode: replay post-mint stamps from journal (crash recovery) ----
if [[ "$MODE" == "recover" ]]; then
  [[ -n "$SLUG" ]] || {
    echo "usage: mint_run.sh recover --slug <slug> --instance-root <dir> [--root <dir>]" >&2
    echo "  or:  mint_run.sh recover <slug> --instance-root <dir> [--root <dir>]" >&2
    exit 1
  }

  # The journal is instance-resident state: recover cannot run without the
  # calling instance's root (explicit contract, DGN-366 L1).
  [[ -n "$JOURNAL_DIR" ]] || {
    echo "[recover] SKIP: --instance-root not supplied -- the mint journal is instance-resident (<instance-root>/.mint-journal); nothing to recover without it" >&2
    exit 1
  }

  JOURNAL_FILE="$JOURNAL_DIR/$SLUG.json"
  [[ -f "$JOURNAL_FILE" ]] || {
    echo "[recover] no journal found for slug '$SLUG' at $JOURNAL_FILE" >&2
    echo "[recover] nothing to recover" >&2
    exit 0
  }

  # Read journal values.
  J_ROOT="$("$PACK_PY3" -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('root',''))")"
  J_ROLE="$("$PACK_PY3" -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('role_prose',''))")"
  J_MODEL="$("$PACK_PY3" -c "import json,sys; d=json.load(open('$JOURNAL_FILE')); print(d.get('model','sonnet'))")"

  # --root flag overrides journal root.
  EFFECTIVE_ROOT="${ROOT:-$J_ROOT}"
  [[ -n "$EFFECTIVE_ROOT" ]] || {
    echo "[recover] cannot determine root: not in journal and --root not supplied" >&2
    exit 1
  }

  [[ -d "$EFFECTIVE_ROOT" ]] || {
    echo "[recover] target dir does not exist: $EFFECTIVE_ROOT" >&2
    echo "[recover] framework mint may not have completed; nothing to recover" >&2
    exit 1
  }

  echo "[recover] replaying post-mint stamps for slug '$SLUG' from $JOURNAL_FILE"

  # DGN-590 P5-b: domain class + lifekit-off stamp (idempotent, constant,
  # journal-independent). Mirrors the mint-mode post-mint stamp.
  _stamp_domain_class "$EFFECTIVE_ROOT"
  # DGN-1719: owner-profile carry + leader record (idempotent replay).
  _carry_owner_profile "$EFFECTIVE_ROOT" "recover"

  # Model stamp (idempotent: always overwrite with journaled value).
  INSTANCE_SETTINGS="$EFFECTIVE_ROOT/.claude/settings.json"
  if [[ -f "$INSTANCE_SETTINGS" ]]; then
    "$PACK_PY3" - "$INSTANCE_SETTINGS" "$J_MODEL" <<'PYEOF'
import json, sys
settings_path, model = sys.argv[1], sys.argv[2]
with open(settings_path, encoding="utf-8") as f:
    data = json.load(f)
data["model"] = model
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[recover] model set to {model!r} in settings.json")
PYEOF
  else
    echo "[recover] WARN settings.json not found at $INSTANCE_SETTINGS; skipping model stamp" >&2
  fi

  # Role stamp -- single canonical implementation (DGN-227 A3/P15).
  if [[ -n "$J_ROLE" ]]; then
    _stamp_role "$EFFECTIVE_ROOT" "$J_ROLE" "recover/role-stamp" || {
      STATUS=$?
      echo "[recover] ERROR: role-stamp step failed (exit $STATUS)" >&2; exit $STATUS
    }
  else
    echo "[recover] no role prose in journal; skipping role stamp"
  fi

  # Cleanup journal on success.
  rm -f "$JOURNAL_FILE"
  echo "[recover] journal removed: $JOURNAL_FILE"
  echo "[recover] DONE -- stamps replayed for $EFFECTIVE_ROOT"
  exit 0
fi

# ---- start mode: load the NEW agent's launchd units (live op, user-gated) --
if [[ "$MODE" == "start" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: start requires --root <dir>" >&2; exit 1; }
  [[ -d "$ROOT/bridge" ]] || { echo "ERROR: not a minted instance: $ROOT" >&2; exit 1; }

  # Deferral manifest: plist basenames staged behind a later gate.
  DEFER_FILE="$ROOT/routines/plists.defer"
  DEFER_LIST=()
  if [[ -f "$DEFER_FILE" ]]; then
    while IFS= read -r line; do
      line="${line%%#*}"
      line="${line//[[:space:]]/}"
      [[ -n "$line" ]] && DEFER_LIST+=("$line")
    done < "$DEFER_FILE"
  fi
  is_deferred() {
    local b="$1" d
    for d in ${DEFER_LIST[@]+"${DEFER_LIST[@]}"}; do
      [[ "$b" == "$d" ]] && return 0
    done
    return 1
  }
  if [[ "$ONLY_DEFERRED" -eq 1 && "${#DEFER_LIST[@]}" -eq 0 ]]; then
    echo "ERROR: --deferred but no defer manifest at $DEFER_FILE" >&2
    exit 1
  fi

  LA_DIR="$LAUNCH_AGENTS_DIR"
  if [[ "$DRY" -ne 1 ]]; then
    mkdir -p "$LA_DIR" || {
      echo "ERROR: cannot create launchd directory: $LA_DIR" >&2
      exit 1
    }
  fi
  loaded=0 deferred=0 gated=0 DRIVE_PENDING=0
  # Load order (DGN-1739): the bridge and routines first, watchdog units
  # LAST, each group sorted (LC_ALL=C) -- the mirror of update_apply.sh
  # _stop_order. A watchdog loaded before its bridge exists sees a vanished
  # label and runs its DGN-888 recovery against a start already in flight.
  start_order() {
    local p
    for p in "$ROOT"/bridge/*.plist; do
      [[ -e "$p" ]] || continue
      [[ "$p" == *.watchdog.plist ]] && continue
      printf '%s\n' "$p"
    done | LC_ALL=C sort
    for p in "$ROOT"/routines/*.plist; do
      if [[ -e "$p" ]]; then printf '%s\n' "$p"; fi
    done | LC_ALL=C sort
    for p in "$ROOT"/bridge/*.watchdog.plist; do
      if [[ -e "$p" ]]; then printf '%s\n' "$p"; fi
    done | LC_ALL=C sort
  }
  START_ORDER="$(start_order)" || {
    echo "ERROR: cannot list unit plists under $ROOT" >&2
    exit 1
  }
  while IFS= read -r p <&3; do
    [[ -n "$p" && -e "$p" ]] || continue
    base="$(basename "$p")"
    label="$(plist_label "$p")" || {
      echo "ERROR: cannot start unit without a valid plist Label: $p" >&2
      exit 1
    }
    if [[ "$ONLY_DEFERRED" -eq 1 ]]; then
      is_deferred "$base" || continue
    elif is_deferred "$base"; then
      echo "[start] deferred $label (routines/plists.defer; load later with --deferred)"
      deferred=$((deferred + 1))
      continue
    fi
    # DGN-1875: module flag explicitly off -> not loaded. The update cycle
    # (update_apply.sh _launchd_stop -> this start) used to re-load the
    # mirror pair on MIRROR_MODULE=off instances and undo the owner's
    # unschedule. A leftover registration or ~/Library/LaunchAgents copy (a
    # copy alone is re-loaded at next login) is retired through the
    # instance's routine-ctl.sh -- never a raw bootout here (DGN-1520).
    if gate="$(module_gate_off "$ROOT" "$base")"; then
      gated=$((gated + 1))
      if [[ "$DRY" -eq 1 ]]; then
        echo "[start] (dry-run) would skip $label ($gate -- module off, DGN-1875)"
        continue
      fi
      echo "[start] skipped $label ($gate -- module off, DGN-1875)"
      if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 \
         || [[ -e "$LA_DIR/$base" ]]; then
        RCTL="$ROOT/routines/lib/routine-ctl.sh"
        if [[ ! -f "$RCTL" ]]; then
          echo "[start] WARN: $label is still scheduled but $RCTL is missing -- left as is; unschedule it by hand with routine-ctl" >&2
        elif ! ROUTINE_CTL_LA_DIR="$LA_DIR" bash "$RCTL" disable "${label##*.}" < /dev/null; then
          echo "[start] WARN: routine-ctl disable ${label##*.} failed -- $label may stay scheduled" >&2
        elif launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 \
             || [[ -e "$LA_DIR/$base" ]]; then
          # routine-ctl addresses com.telegram-skill-bot.<agent>.<name>; a
          # plist whose Label is shaped differently is not its to retire.
          echo "[start] WARN: $label still scheduled after routine-ctl disable (Label not routine-ctl's shape?) -- unschedule it by hand" >&2
        fi
      fi
      continue
    fi
    # DGN-1655: the instance drive is never "already loaded -> skip": a
    # loaded label can still carry an OLD StartInterval (F3), so it is
    # always re-rendered, reloaded and verified live by drive-setup.sh
    # after this loop.
    if [[ "$label" == *.instance-drive ]]; then
      DRIVE_PENDING=1
      continue
    fi
    if [[ "$DRY" -eq 1 ]]; then
      echo "[start] (dry-run) would load $label"
      loaded=$((loaded + 1))
      continue
    fi
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "[start] already loaded $label"
      loaded=$((loaded + 1))
      continue
    fi
    cp -f "$p" "$LA_DIR/"
    launchctl bootstrap "gui/$(id -u)" "$LA_DIR/$base"
    echo "[start] loaded $label"
    loaded=$((loaded + 1))
  done 3<<< "$START_ORDER"
  # DGN-1655: the framework-owned instance drive -- (re)load and verify the
  # LIVE definition (run interval == rendered period, policy argv, runs,
  # first drive beat). FAIL-CLOSED: a start that cannot prove the drive is
  # not a completed mint/update, though every other unit stays loaded (the
  # bridge is never held hostage to the drive). rc 5 = sandbox refusal
  # (fake HOME / temp root): loud, never a pass, not fatal for fixtures.
  DRIVE_RC=0
  if [[ "$DRIVE_PENDING" -eq 1 ]]; then
    DRIVE_SETUP="$ROOT/routines/lib/drive-setup.sh"
    if [[ "$DRY" -eq 1 ]]; then
      echo "[start] (dry-run) would render, reload and live-verify the instance drive (drive-setup.sh install)"
      loaded=$((loaded + 1))
    elif [[ ! -f "$DRIVE_SETUP" ]]; then
      echo "ERROR: instance drive plist present but $DRIVE_SETUP is missing -- drive NOT loaded (DGN-1655)" >&2
      DRIVE_RC=3
    else
      DOGANY_LAUNCH_AGENTS_DIR="$LA_DIR" bash "$DRIVE_SETUP" install "$ROOT" < /dev/null || DRIVE_RC=$?
      case "$DRIVE_RC" in
        0) echo "[start] loaded + verified instance drive"; loaded=$((loaded + 1)) ;;
        5) echo "[start] WARN: instance drive NOT loaded/verified (sandbox refusal, DGN-1718) -- not a pass" >&2; DRIVE_RC=0 ;;
        *) echo "ERROR: instance drive could not be verified live (rc=$DRIVE_RC) -- the sweep has NO driver on this instance (DGN-1655 F6). Fix and re-run: bash $DRIVE_SETUP install $ROOT" >&2 ;;
      esac
    fi
  fi
  [[ "$loaded" -gt 0 ]] || { echo "ERROR: no plists to load under $ROOT" >&2; exit 1; }
  if [[ "$DRY" -eq 1 ]]; then
    echo "[start] DRY-RUN DONE ($loaded units would load, $deferred deferred$([[ "$gated" -gt 0 ]] && echo ", $gated module-off"))"
  else
    echo "[start] DONE ($loaded units loaded, $deferred deferred$([[ "$gated" -gt 0 ]] && echo ", $gated module-off"))"
  fi
  [[ "$DRIVE_RC" -eq 0 ]] || exit 1
  exit 0
fi

# ---- verdict mode: is this newborn actually alive? (DGN-1606, read-only) ----
# Standalone seat of the pipeline's viability verdict, so a newborn that got
# here through ANY lane (manual mint + pack_install, a refused install that
# left a workspace behind, a re-run `start`) can be judged by the same
# predicate. rc 0 = alive, rc 30 = not confirmed, other rc = usage/input.
# Probes are read-only; this mode never repairs, restarts or deletes.
if [[ "$MODE" == "verdict" ]]; then
  [[ -n "$ROOT" ]] || { echo "ERROR: verdict requires --root <dir>" >&2; exit 1; }
  [[ -d "$ROOT" ]] || { echo "ERROR: no such directory: $ROOT" >&2; exit 1; }
  # DGN-1683 WP5: --check config judges ONLY the birth-defect checks and
  # --check viability ONLY liveness, so a caller can collect both independently
  # (a dead bridge must not hide a channel/source defect). Default = the
  # original chained behaviour.
  if [[ "$VERDICT_CHECK" == "config" ]]; then
    mint_wrongness_verdict "$ROOT" "$INSTANCE_ROOT" || exit 31
    exit 0
  fi
  VERDICT_TIMEOUT="${VERDICT_TIMEOUT_OPT:-$(mint_verdict_stale_s "$ROOT")}"
  if [[ "$VERDICT_CHECK" == "viability" ]]; then
    mint_viability_verdict "$ROOT" "${VERDICT_SINCE_OPT:-0}" "$VERDICT_TIMEOUT" && exit 0
    exit 30
  fi
  if mint_viability_verdict "$ROOT" "${VERDICT_SINCE_OPT:-0}" "$VERDICT_TIMEOUT"; then
    # DGN-1608: alive is necessary, not sufficient. When the caller names the
    # ordering instance, the standalone seat earns the FULL verdict (so a
    # re-judge after a fix can close the ordered-mint obligation as clean,
    # not half of it). Without --instance-root the mode keeps its original
    # liveness-only contract.
    if [[ -n "$INSTANCE_ROOT" ]]; then
      mint_wrongness_verdict "$ROOT" "$INSTANCE_ROOT" || exit 31
    fi
    exit 0
  fi
  exit 30
fi

# ---- pipeline mode: mint -> pack_install -> done (one entry, no user asks) --
if [[ "$MODE" == "pipeline" ]]; then
  [[ -n "$SLUG" ]] || { echo "ERROR: pipeline requires --slug <slug>" >&2; exit 1; }
  [[ -n "$PACK_ID" ]] || { echo "ERROR: pipeline requires --pack <pack-id>" >&2; exit 1; }

  # Derive ROOT if not supplied (same rule as mint mode)
  # DGN-1483: agents live under ~/.dogany/agents/<slug>, slug verbatim
  # (no case mangling -- the derived path must be traceable to the slug).
  if [[ -z "$ROOT" ]]; then
    ROOT="$HOME/.dogany/agents/$SLUG"
  fi

  PACK_INSTALL_SH="$SCRIPT_DIR/pack_install.sh"
  [[ -x "$PACK_INSTALL_SH" ]] || { echo "ERROR: pack_install.sh not found: $PACK_INSTALL_SH" >&2; exit 1; }

  # --- DGN-1683 legacy retirement enforcement (grill v4) ---------------------
  # The mint verb (lib/mint_flow.py) owns the owner-facing procedure now. While
  # its flow file (<instance-root>/config/mint-flow.json) is active, this
  # pipeline runs ONLY as the flow worker's child: DOGANY_MINT_CONTROLLER_FLOW
  # + DOGANY_MINT_CONTROLLER_NONCE must match the active BUILDING flow (nonce
  # persisted in the flow file by the worker before GO) and its target slug.
  # A controller-less run is refused BEFORE any read of catalog/occupancy and
  # before any write. In controller mode the legacy hint push and the ordered-
  # mint obligation are OFF (the worker owns the terminal screen; one voice).
  # Limit, stated: a run that names no ordering instance (no --instance-root,
  # no $DOGANY_INSTANCE_ROOT, cwd not an instance root) cannot be judged here.
  MINT_CONTROLLED=0
  GUARD_OUT="$("$PACK_PY3" "$SCRIPT_DIR/lib/mint_flow_guard.py" pipeline-check \
      --instance-root "$INSTANCE_ROOT" --slug "$SLUG")" || {
    echo "ERROR: pipeline refused by the mint-flow guard (see above); nothing was read or written" >&2
    exit 3
  }
  [[ "$GUARD_OUT" == "controlled" ]] && MINT_CONTROLLED=1

  # DGN-1513 F-1 (grill G8 break 1): the DEFAULT catalog is the CANONICAL
  # repo's, not this machinery tree's. Catalog rows carry sibling-checkout
  # relative package_dir values ("../../dogany-lifekit" etc), and
  # pack_coord_resolve's ONE rule (pack_coords.sh: relative resolves against
  # the catalog FILE's directory) only lands on a real pack next to the
  # canonical repo. From a consume checkout (<instance>/.dogany-consume) the
  # own-tree default resolved every official pack to a path under the
  # instance that does not exist, killing the whole official-pack lane
  # mid-transaction (mint done, pack_install dead, marker open).
  # BOUNDARY (SPEC v2 SS4-6 scope): sibling-repo pack PAYLOADS are OUT OF
  # SCOPE for the framework consume pin -- payload pinning is owned by the
  # pack's own tag machinery (--source-tag / DGN-1489), so reading the
  # canonical catalog does not re-open the exposure the consume pin closes.
  # KNOWN SKEW (stated, not hidden): the consume tree's own catalog copy
  # (what SKILL.md step 1a reads for browsing) can lag the canonical catalog
  # by <=1 channel step, so the two id sets can differ in that window; the
  # INSTALL always judges against the canonical catalog resolved here.
  # Fail-closed BEFORE the mint half runs: a missing canonical catalog must
  # not produce another half-mint.
  if [[ -z "$CATALOG_OPT" ]]; then
    CATALOG_OPT="$(pack_default_catalog_for_instance "$MACHINERY_REPO" "$INSTANCE_ROOT" "pipeline")" || exit 1
  fi

  # --- DGN-1608: an ordered mint may not end in silence ----------------------
  # Measured (DGN-1608): one agent was minted seven times in eight days and
  # every failure produced SILENCE in the conversation that ordered it, via
  # four independent legs (no notice lane yet / lane routed to another room /
  # no verdict for alive-but-wrong / output discarded without a PUSH
  # sentinel). The repair is NOT a new mechanism: it EXTENDS the DGN-1012
  # terminal-state obligation pattern already in production on the dispatch
  # lane (routines/terminal-state-ledger.py + dispatch-detached.sh call
  # sites) -- open at order time, closable ONLY by a terminal verdict,
  # delivery failure leaves it open, the sweep expires it and the expiry is
  # itself a notification. Nothing in that chain asks a model whether the
  # result is worth reporting.
  #
  # Predicate (asked ONCE, here, where the fact is free): did the owner
  # ORDER this work? On this pipeline the ordering conversation is exactly
  # the --instance-root instance (the mint skill runs the pipeline from the
  # owner's conversation and passes its own root). Ordered -> obligation
  # opened. No --instance-root = operator lane -> silence-by-default stays
  # untouched, with a loud skip line (DGN-366 L1 discipline).
  #
  # ADDRESS IS STATED, never path-derived: terminal-state-ledger.py resolves
  # AGENT_ROOT from its own __file__ (ledger.py:98-99), so a bare invocation
  # routes by WHICH COPY EXECUTES, not by who ordered the mint -- a mint
  # ordered in the owner's conversation but executing the canonical copy
  # would land the verdict in a room nobody is in (the original failure,
  # reproduced by correct code). Every call below therefore passes
  # DOGANY_TSL_LEDGER/INBOX/ENV/PUSH pinned to the ORDERING instance, so the
  # obligation lives on ITS ledger, is expired by ITS existing sweep legs
  # (cron-guard + dispatch -- no new sweep), and notifies through ITS
  # session-inbox and push.sh.
  #
  # notify=both, decided (DGN-1608 item 4): the session leg drops the
  # itemized expiry into the ordering conversation's session-inbox (context
  # where the order lives); the owner leg goes through push.sh as a
  # bridge-independent curl, so it survives BOTH the bridge being the thing
  # that broke AND a model suppressing a sentinel-less turn (silence leg 4)
  # -- neither leg alone covers both failure modes.
  MINT_TSL_ID="" MINT_TSL_BIN="" MINT_TSL_VERDICT="" MINT_TSL_CLOSED=0
  _mint_tsl() { # <ledger argv...> -- fail-open (L5), stderr kept as the trace
    [[ -n "$MINT_TSL_ID" && -n "$MINT_TSL_BIN" ]] || return 0
    DOGANY_TSL_LEDGER="$INSTANCE_ROOT/.telegram_bot/terminal-state-ledger.jsonl" \
    DOGANY_TSL_INBOX="$INSTANCE_ROOT/.telegram_bot/session-inbox" \
    DOGANY_TSL_ENV="$INSTANCE_ROOT/.telegram_bot/.env" \
    DOGANY_TSL_PUSH="$INSTANCE_ROOT/routines/push.sh" \
      "$PACK_PY3" "$MINT_TSL_BIN" "$@" >/dev/null || true
  }
  # _mint_tsl_close_delivered <gate-id> -- translate a DELIVERED fault-verdict
  # stop into the obligation's terminal close. ONLY the two verdict gates
  # close it here (stillborn / born-alive-and-wrong; `clean` closes in the
  # rc=0 arm of the trap): every other stop -- planned gates, the mint.fault
  # crash lane -- deliberately leaves the obligation OPEN, because those
  # stops are not mint verdicts, and an ordered mint without a verdict must
  # resurface via the sweep, never fade out.
  _mint_tsl_close_delivered() {
    case "$1" in
      mint.stillborn)
        MINT_TSL_CLOSED=1
        _mint_tsl close --id "$MINT_TSL_ID" --state failed \
          --note "mint verdict: stillborn -- no first bridge heartbeat ($SLUG); owner hint delivered" \
          --evidence "$ROOT" ;;
      mint.born-alive-and-wrong)
        MINT_TSL_CLOSED=1
        _mint_tsl close --id "$MINT_TSL_ID" --state failed \
          --note "mint verdict: born-alive-and-wrong -- alive but failed birth-defect checks ($SLUG); owner hint delivered" \
          --evidence "$ROOT" ;;
    esac
  }
  _mint_tsl_open_note() {
    [[ -n "$MINT_TSL_ID" && "$MINT_TSL_CLOSED" -eq 0 ]] || return 0
    echo "[pipeline] ordered-mint obligation $MINT_TSL_ID stays OPEN (no terminal mint verdict was delivered) -- the ordering instance's hourly sweep will expire it into a notification (DGN-1608: silence is not an outcome)" >&2
  }

  # --- DGN-1506 item 3, leg (b): machine-sent stop sentence ------------------
  # On a REAL invocation the run is user-visible from the first step (the
  # skill has already said "새 에이전트를 만들고 있어요..."), so the hint
  # lane is armed BEFORE the read-only catalog, occupancy and channel gates.
  # DGN-1621: only a temporary notification marker and traps are armed here;
  # ledger/journal/target writes wait until all four refusal gates pass.
  # A dry-run-only
  # invocation gets no lane: nothing user-visible has been promised, the
  # operator/model reads stderr. This pipeline is the transaction and owns
  # every word after a stop -- the model never relays gate text (owner
  # ruling: needing a translation IS the defect). A planned gate records its
  # id via _gate_stop into DOGANY_GATE_STATE_FILE (leg a); this trap reads
  # the marker -- absent marker = the generic mint.fault lane, so a raw
  # _fail / crash / signal cannot dodge leg (b) -- looks up the gate's
  # user_hint_ko in gates.json and sends it ONCE through the LEADER's
  # routines/push.sh (outbound needs only the bot token, never the bridge:
  # update_apply.sh _notify_send precedent). Empty hint (owner copy pending)
  # = loud line, no push -- behavior identical to pre-DGN-1506.
  if [[ "$DRY" -eq 0 && "$MINT_CONTROLLED" -eq 1 ]]; then
    echo "[pipeline] controller mode (DGN-1683): hint push and ordered-mint obligation are OFF; the flow worker reports the outcome" >&2
    # A planned gate still records its id for the worker (its own marker file).
    [[ -n "${DOGANY_GATE_STATE_FILE:-}" ]] && : > "$DOGANY_GATE_STATE_FILE" 2>/dev/null || true
  elif [[ "$DRY" -eq 0 ]]; then
    GATES_JSON="$SCRIPT_DIR/gates.json"
    GATE_STATE_FILE="$(mktemp "${TMPDIR:-/tmp}/mint-gate-stop.XXXXXX")"
    export DOGANY_GATE_STATE_FILE="$GATE_STATE_FILE"
    _pipeline_hint_notify() {
      local rc=$? gid="mint.fault" hint="" ulabel=""
      trap - EXIT
      if [[ "$rc" -eq 0 ]]; then
        rm -f "$GATE_STATE_FILE"
        # DGN-1608: `clean` is EARNED, never inferred from rc=0. The
        # variable is assigned in exactly one place -- after BOTH verdicts
        # (viability AND wrongness) returned 0. A 0-exit without it (the
        # --no-start build-only branch) leaves the obligation OPEN: an
        # unjudged ordered mint is still an unfinished obligation, and the
        # sweep will say so (absence of an error is NOT clean).
        if [[ "$MINT_TSL_VERDICT" == "clean" ]]; then
          MINT_TSL_CLOSED=1
          _mint_tsl close --id "$MINT_TSL_ID" --state done \
            --note "mint verdict: clean -- viability + wrongness checks passed ($SLUG)" \
            --evidence "$ROOT"
        fi
        _mint_tsl_open_note
        exit 0
      fi
      if [[ -s "$GATE_STATE_FILE" ]]; then
        gid="$(sed -n 's/^gate_id=//p' "$GATE_STATE_FILE" | head -1)"
        [[ -n "$gid" ]] || gid="mint.fault"
      fi
      rm -f "$GATE_STATE_FILE"
      hint="$("$PACK_PY3" - "$GATES_JSON" "$gid" 2>/dev/null <<'PYEOF'
import json, sys
try:
    gates = json.load(open(sys.argv[1], encoding="utf-8")).get("gates", {})
except Exception:
    sys.exit(0)
print(gates.get(sys.argv[2], {}).get("user_hint_ko", ""), end="")
PYEOF
)" || hint=""
      if [[ -z "$hint" ]]; then
        echo "[pipeline] stop at gate '$gid' (rc=$rc) -- user hint PENDING (DGN-1506 item 3, gates.json user_hint_ko empty): no machine copy sent" >&2
        _mint_tsl_open_note
        exit "$rc"
      fi
      if [[ -z "$INSTANCE_ROOT" || ! -x "$INSTANCE_ROOT/routines/push.sh" ]]; then
        echo "[pipeline] stop at gate '$gid' (rc=$rc) -- user hint NOT sent: no leader push path (--instance-root ${INSTANCE_ROOT:-unset}); operator run, hint lane skipped. Hint body:" >&2
        printf '%s\n' "$hint" | sed 's/^/[pipeline]   /' >&2
        _mint_tsl_open_note
        exit "$rc"
      fi
      ulabel="$(conf_get "$INSTANCE_ROOT/.instance.conf" DOGANY_USER_LABEL)"
      hint="${hint//\{USER_LABEL\}/${ulabel:-님}}"
      agent_display="$(_g3b_display_name "$INSTANCE_ROOT")"
      hint="${hint//\{AGENT_DISPLAY_NAME\}/${agent_display:-에이전트}}"
      if "$INSTANCE_ROOT/routines/push.sh" --text "$hint" --audience owner --record-ledger-only >/dev/null 2>&1; then
        echo "[pipeline] stop at gate '$gid' (rc=$rc) -- user hint sent via leader push.sh (model untouched, DGN-1506 item 3)" >&2
        # DGN-1608: the delivered hint IS the terminal signal for the two
        # verdict gates -- mirror of dispatch-detached's inbox-drop-then-
        # close ordering. A FAILED push (else-arm) deliberately closes
        # nothing: the obligation stays open and the sweep re-delivers.
        _mint_tsl_close_delivered "$gid"
      else
        echo "[pipeline] stop at gate '$gid' (rc=$rc) -- user hint push FAILED (push.sh rc!=0); body follows for manual relay:" >&2
        printf '%s\n' "$hint" | sed 's/^/[pipeline]   /' >&2
      fi
      _mint_tsl_open_note
      exit "$rc"
    }
    trap _pipeline_hint_notify EXIT
    # An untrapped INT/TERM kills bash WITHOUT the EXIT trap; route them into
    # it so a signalled transaction still lands in the mint.fault hint lane
    # (SIGKILL/power loss stay uncovered -- same boundary update_apply records).
    trap 'exit 130' INT
    trap 'exit 143' TERM
  fi

  # --- DGN-1575: catalog-brief gate, official-pack seat ---------------------
  # The flow-level seat is SKILL.md step 1 (mint_run.sh catalog-brief, which
  # emits the official-vs-custom choice screen and writes the stamp); THIS
  # seat is the machine backstop no prose can skip: installing an official
  # catalog pack whose stamp does not exist / is stale / names a different
  # pack means the owner was never shown THIS pack's choice screen
  # (measured 2026-09-18: the flow went straight to BotFather; pack_health
  # was never mentioned). Sandbox/operator lanes stay open: a --pack with no
  # eligible catalog row (kit, unpublished, no domains) reads NO-MATCH, and
  # a run without --instance-root has no instance-resident stamp to read
  # (DGN-366 L1) -- loud skip, never silent. dry-run checks too (early
  # failure for the model), same as the occupancy gate below.
  CBG_CATALOG="${CATALOG_OPT:-$MACHINERY_REPO/packs/catalog.json}"
  if [[ -z "$INSTANCE_ROOT" ]]; then
    echo "[pipeline] SKIP catalog-brief gate (--instance-root not supplied): stamp is instance-resident (DGN-366 L1); operator lane" >&2
  elif [[ ! -f "$CBG_CATALOG" ]]; then
    echo "[pipeline] SKIP catalog-brief gate: catalog not found ($CBG_CATALOG)" >&2
  else
    CBG_RC=0
    CBG_OUT="$("$PACK_PY3" "$SCRIPT_DIR/lib/catalog_brief.py" gate \
      --catalog "$CBG_CATALOG" \
      --stamp "$INSTANCE_ROOT/config/mint-catalog-brief.json" \
      --ttl "${DOGANY_CATALOG_BRIEF_TTL_S:-21600}" \
      --pack "$PACK_ID" 2>&1)" || CBG_RC=$?
    printf '%s\n' "$CBG_OUT" | sed 's/^/[pipeline] catalog-brief-gate /' >&2
    if [[ "$CBG_RC" -eq 20 ]]; then
      _gate_stop mint.catalog-brief "catalog-brief gate (DGN-1575): official pack '$PACK_ID' reached the build step without a fresh catalog-brief stamp covering it -- the owner was never shown the official-vs-custom choice screen. Run mint_run.sh catalog-brief --query '<the owner's own domain words>' --instance-root $INSTANCE_ROOT, render its stdout VERBATIM to the owner, collect the choice, then re-run. Never work around this stop (no stamp forgery, no direct mint.sh)"
    elif [[ "$CBG_RC" -ne 0 ]]; then
      echo "ERROR: catalog-brief gate errored (rc=$CBG_RC) -- fail-closed before the mint half runs" >&2
      exit 1
    fi
  fi

  # --- DGN-1543 R6: occupancy gate -- never mint over an occupied slot ------
  # Measured 2026-09-17 (live owner run): re-minting over an existing agent
  # covers the disk with a blank slate while its Telegram chat survives, so
  # the freshly minted agent reads its predecessor's scrollback and skips its
  # own onboarding. The FLOW-level seat is SKILL.md step 3 (the moment the
  # slug exists, before the owner walks to BotFather); THIS seat is the
  # machine backstop no prose can skip -- same predicate, one implementation
  # (mint_occupancy_probe, lib/mint_teardown.sh). class=planned: an occupied
  # slot is a normal branch whose next verb already exists (teardown), never
  # a fault -- and never a silent overwrite.
  OCCUPANCY="$(mint_occupancy_probe "$ROOT" || true)"
  if grep -q '^OCCUPIED ' <<< "$OCCUPANCY"; then
    printf '%s\n' "$OCCUPANCY" | sed 's/^/[pipeline] /' >&2
    _gate_stop mint.occupied "occupancy gate (DGN-1543 R6): mint target $ROOT is already occupied (signals above) -- re-minting would overwrite it while its Telegram chat survives (measured 2026-09-17: onboarding skipped, predecessor persona spoken). The owner decides: teardown-then-remint (mint_run.sh teardown --slug $SLUG, gates of its own) or abort. Never overwrite, never work around this stop by hand"
  fi

  if ! _mt_transcript_clean "$ROOT"; then
    if [[ ! -e "$ROOT" ]]; then
      mint_transcript_clear_aborted "$ROOT" "$HOME/.dogany/agents/.retired" \
        || _gate_stop mint.transcript-dirty "aborted-mint transcript residue could not be archived"
    fi
  fi
  if ! _mt_transcript_clean "$ROOT"; then
    _gate_stop mint.transcript-dirty "predecessor transcripts remain on the workspace read key"
  fi

  # DGN-1700: the flow seat of this gate is lib/mint_flow.py LedgerGate (the
  # same minting_state.py validate call, run at open and at pack selection,
  # before the owner walks to BotFather); THIS seat is the backstop.
  if [[ -n "$INSTANCE_ROOT" ]]; then
    if ! "$PACK_PY3" "$MACHINERY_REPO/skills/dogany-mint-agent/scripts/minting_state.py" validate --state "$INSTANCE_ROOT/config/minting-state.json"; then
      _gate_stop mint.ledger-stale "dedicated agent ledger binding has no existing path"
    fi
  fi

  # All read-only refusals run inside the hint lane, before durable writes.
  _resolve_mint_channel

  if [[ "$DRY" -eq 0 && "$MINT_CONTROLLED" -eq 1 ]]; then
    echo "[pipeline] ordered-mint obligation SKIPPED (controller mode, DGN-1683): the flow worker owns the terminal screen"
  elif [[ "$DRY" -eq 0 ]]; then
    # DGN-1608 obligation open -- first transaction write, AFTER the read-only
    # gates. The hint trap is already armed; every subsequent exit (planned
    # gate, fault, signal, SIGKILL, host crash) is covered: anything that is
    # not one of the three terminal verdicts leaves the obligation open, and
    # the ordering instance's sweep expires it into a notification. Scope:
    # the pipeline is the ordered/product path (the mint skill's only entry);
    # bare `mint` / direct pack_install stay operator lanes (DGN-1607 owns
    # their liveness gap) and open nothing.
    if [[ -n "$INSTANCE_ROOT" ]]; then
      # Prefer the ordering instance's own vendored copy (version-coherent
      # with the ledger its sweeps fold); the machinery copy is the fallback.
      # The choice does NOT decide the address -- the env pins in _mint_tsl
      # do (that is the whole point: stated, never path-derived).
      for _tsl_cand in "$INSTANCE_ROOT/routines/terminal-state-ledger.py" \
                       "$MACHINERY_REPO/agents/.template/routines/terminal-state-ledger.py"; do
        [[ -f "$_tsl_cand" ]] && { MINT_TSL_BIN="$_tsl_cand"; break; }
      done
      if [[ -n "$MINT_TSL_BIN" ]]; then
        # TTL covers the whole transaction (mint + pack_install measured in
        # minutes + start + verdict window <=180s) with generous slack; a
        # SIGKILLed run is then detected on the next sweep tick (L6: up to
        # TTL + sweep cadence).
        MINT_TSL_ID="mint-$SLUG-$(date +%s)"
        _mint_tsl open --id "$MINT_TSL_ID" --surface mint \
          --ttl "${DOGANY_MINT_TSL_TTL:-5400}" --notify both \
          --note "ordered mint: $SLUG -> $ROOT (pipeline; ordering instance $INSTANCE_ROOT)" \
          --evidence "$ROOT"
        echo "[pipeline] ordered-mint obligation opened (DGN-1608): id=$MINT_TSL_ID ledger=$INSTANCE_ROOT/.telegram_bot/terminal-state-ledger.jsonl script=$MINT_TSL_BIN -- closes ONLY on a clean/stillborn/born-alive-and-wrong verdict; anything else expires on the ordering instance's sweep"
      else
        echo "[pipeline] WARN: terminal-state-ledger.py not found ($INSTANCE_ROOT/routines, $MACHINERY_REPO/agents/.template/routines) -- ordered-mint obligation NOT opened; a silent death of this run will NOT be swept (DGN-1608 uncovered on this host)" >&2
      fi
    else
      echo "[pipeline] ordered-mint obligation SKIPPED (--instance-root not supplied): no ordering conversation to address -- operator lane, silence-by-default stays (DGN-1608)"
    fi
  fi

  # 1. dry-run the mint (does NOT require the instance root to exist)
  echo "[pipeline] step 1/4: mint dry-run..."
  MINT_ARGS=(--slug "$SLUG" --root "$ROOT")
  [[ "${NO_VENV:-0}" -eq 1 ]] && MINT_ARGS+=(--no-venv)
  [[ -z "$CHANNEL_OPT" ]] || MINT_ARGS+=(--channel "$CHANNEL_OPT")
  [[ -n "$INSTANCE_ROOT" ]] && MINT_ARGS+=(--instance-root "$INSTANCE_ROOT")
  [[ -n "$OWNER_IDS" ]] && MINT_ARGS+=(--owner-id "$OWNER_IDS")
  [[ -n "$AGENT_TZ" ]]  && MINT_ARGS+=(--tz "$AGENT_TZ")
  [[ -n "$LANG_OPT" ]]  && MINT_ARGS+=(--lang "$LANG_OPT")
  [[ -n "$ROLE_PROSE" ]] && MINT_ARGS+=(--role "$ROLE_PROSE")
  [[ "$CORE_ONLY" -eq 1 ]] && MINT_ARGS+=(--core-only)
  MINT_ARGS+=(--model "$MODEL_OPT")

  "$0" mint "${MINT_ARGS[@]}" --dry-run
  echo "[pipeline] mint dry-run OK"

  # NOTE: standalone pack_install dry-run removed (it required the minted root
  # to exist and aborted on fresh mint -- chicken-and-egg). The real pack_install
  # run (step 3) already performs its own preflight after the mint.

  if [[ "$DRY" -eq 1 ]]; then
    # DGN-1682 item 2: say what this dry-run did NOT judge. Rehearsal 2
    # passed a pipeline --dry-run rc=0 whose real run then BLOCKed inside
    # pack_install (kit auto-resolve source-tag gate) -- an rc=0 dry-run must
    # not read as "the pack install will pass".
    echo "[pipeline] NOTE: pack install NOT exercised by this dry-run -- pack_install ($PACK_ID) and every gate it owns (source-tag / --untagged-local, requires_kit auto-resolve, crew join and kit-version coherence, compat-lint) run only in the real pipeline; rc=0 here says nothing about them"
    echo "[pipeline] DRY-RUN DONE -- mint preflight passed, nothing written (pack install not exercised)"
    exit 0
  fi

  # 2. real mint
  # --txn-open (DGN-1506): the mint half must NOT close the unfinished-mint
  # marker -- this pipeline is the transaction, and it is not done until
  # pack_install (and any G3-b alignment) has run.
  echo "[pipeline] step 2/4: minting instance..."
  "$0" mint "${MINT_ARGS[@]}" --txn-open
  echo "[pipeline] mint done -> $ROOT"

  # 3. real pack_install (step 1 inside it is preflight; NEVER starts the bot
  # here -- DGN-1543: bot start is deferred to step 5 below, AFTER the
  # transaction marker closes, so the instance cannot go live and talk to the
  # owner while the mint is still open).
  if [[ -n "$PEER_ROOT" ]]; then
    echo "[pipeline] step 3/4: installing pack (migration path, peer=$PEER_ROOT)..."
  else
    echo "[pipeline] step 3/4: installing pack (fresh/standalone path)..."
  fi
  PACK_RUN_ARGS=("$SLUG" "$ROOT" --pack "$PACK_ID" --model "$MODEL_OPT")
  [[ -n "$INSTANCE_ROOT" ]] && PACK_RUN_ARGS+=(--instance-root "$INSTANCE_ROOT")
  [[ -n "$CATALOG_OPT" ]] && PACK_RUN_ARGS+=(--catalog "$CATALOG_OPT")
  # dsp t2pin: hand the source tag to pack_install VERBATIM -- it is the sole
  # validator (fail-closed) and the sole ledger writer. NOTE: pipeline
  # --dry-run exits before pack_install runs, so a bad ref surfaces at the
  # real run's validation, not at the dry-run (same as every other
  # pack_install-owned gate here).
  [[ -n "$SOURCE_TAG_OPT" ]] && PACK_RUN_ARGS+=(--source-tag "$SOURCE_TAG_OPT")
  # DGN-1489: same verbatim hand-off as --source-tag -- pack_install is the
  # sole judge (contradiction with --source-tag, receipt record, gate SKIP).
  [[ -n "$UNTAGGED_LOCAL_OPT" ]] && PACK_RUN_ARGS+=(--untagged-local "$UNTAGGED_LOCAL_OPT")
  [[ -n "$PEER_ROOT" ]] && PACK_RUN_ARGS+=(--migrate-from "$PEER_ROOT")
  # DGN-1543: pack_install never starts the bot on this path, regardless of
  # the caller's own --no-start -- step 5 below is the single place pipeline
  # mode starts anything, and it only runs after the marker closes.
  PACK_RUN_ARGS+=(--no-start)
  [[ "$NO_STATE" -eq 1 ]] && PACK_RUN_ARGS+=(--no-state)
  # DGN-1509 S2: minting a NEW instance into an existing kit crew IS a
  # legitimate join (there is no other actor who could have named this
  # instance to a crew-scoped update_apply.sh transaction before it
  # existed) -- acknowledge pack_install's direct-crew-join refusal so the
  # mint pipeline does not become the one path S2 accidentally closes
  # (grill M5: "크루에 멤버를 추가할 방법이 하나도 없다"). Harmless when the
  # pack is not a kit, or is a kit with no existing crew (the gate is a
  # no-op in both cases -- see pack_install.sh _kit_crew_join_gate).
  DOGANY_CREW_JOIN_ACK=1 "$PACK_INSTALL_SH" "${PACK_RUN_ARGS[@]}"
  echo "[pipeline] mint+pack done -> $ROOT"
  # DGN-1826 F4: the pack's requires_kit -> KIT= (only if absent). Read AFTER
  # pack_install: on a public install the pack source clone exists only once
  # pack_install has fetched it (pack_row_dir falls back to that clone).
  _SPK_LIB="$SCRIPT_DIR/lib/pack_coords.sh"
  if [[ -f "$_SPK_LIB" ]]; then
    _SPK_DIR="$( { . "$_SPK_LIB" \
      && _spk_row="$(pack_catalog_row "$CATALOG_OPT" "$PACK_ID")" \
      && [[ -n "$_spk_row" && "$_spk_row" != "null" ]] \
      && pack_row_dir "$CATALOG_OPT" "$_spk_row"; } 2>/dev/null || true)"
    if [[ -n "$_SPK_DIR" ]]; then _stamp_pack_kit "$ROOT" "$_SPK_DIR/pack-manifest.json"; fi
  fi
  # DGN-1869: pack_install registers each shared-DB kit member itself.

  # 4. G3-b alignment (DGN-227 / DGN-422): main install -> later domain-add via
  # this v2 path must land on the SAME state surface as the install-time G2
  # main-add flow. When --peer-main names an existing main, align:
  #   (a) registry: register the new domain + capture its display name on main;
  #   (b) limit ledger: ensure ~/.dogany/config DOGANY_MAX_AGENTS covers the
  #       count (the v2 path never read the limit before -- G2-1 invariant now
  #       applies to BOTH mint paths);
  #   (c) routing: HANDOFF_PEER_MAIN (briefing axis) + BRIEF_ROUTING, gated by
  #       the E3 submit-flip gate (fail => standalone). MIGRATION_PEER is NEVER
  #       written here -- this is a fresh mint, briefing axis only (E2-1).
  if [[ -n "$PEER_MAIN" ]]; then
    g3b_align_domain "$ROOT" "$PEER_MAIN" "$SLUG"
  fi
  # DGN-1506: the transaction completed -- close the marker. Every earlier
  # exit path leaves it open on purpose, which is what makes `teardown`
  # possible without guessing.
  mint_teardown_txn_close "$ROOT"

  # 5. bot start -- DGN-1543: the ONLY place this pipeline starts the bot,
  # and it runs strictly AFTER the marker above closes. The prior order (a
  # bare pack_install-owned start at its own step 10, marker closed much
  # later here) let a fully live, owner-facing bot exist while the
  # transaction still read unfinished -- measured 2026-09-17: teardown found
  # `.mint-incomplete` on an instance that had already chatted with the
  # owner. Completion (the marker) and liveness (the bot) now share one
  # order: closed-then-started, never the reverse, so "the bot is live"
  # cannot be observed before "the mint is complete" is true.
  if [[ "$NO_START" -eq 1 ]]; then
    echo "[pipeline] bot start SKIPPED (--no-start) -- instance built, not started"
    # DGN-1606: --no-start is a legitimate build-only mode -- the verdict
    # ABSTAINS (an unstarted build has no liveness to judge); it must never
    # read as a false death. Judge later, after an explicit start.
    echo "[pipeline] viability verdict ABSTAINED (--no-start): judge after starting, via: $0 verdict --root $ROOT"
    # DGN-1683 grill v4 fix 8: this rc=0 means BUILT, never READY or successful.
    # A caller that needs success must run start + `verdict --fresh --process
    # --strict` (see lib/mint_ready.py); the marker above is already closed.
    echo "[pipeline] READINESS NOT JUDGED (--no-start): rc=0 means built only -- not started, not registered, not verified"
    echo "[pipeline] DONE -- instance ready at $ROOT (not started)"
    exit 0
  fi
  echo "[pipeline] step 4/4: starting bot..."
  # DGN-1606: epoch recorded BEFORE the units load -- the verdict below only
  # accepts a heartbeat written at/after this moment, so a beat left behind
  # by a previous life can never vouch for this one.
  VERDICT_SINCE="$(date +%s)"
  START_RC=0
  "$0" start --root "$ROOT" || START_RC=$?
  if [[ "$START_RC" -ne 0 ]]; then
    echo "[pipeline] instance built (mint complete) but bot start FAILED (rc=$START_RC) -- re-run: $0 start --root $ROOT" >&2
    exit "$START_RC"
  fi
  # DGN-1606 viability verdict: `start` rc=0 above proves only that launchd
  # ACCEPTED the units (launchctl bootstrap). Four of seven mints died with
  # zero bridge heartbeats while this line printed DONE anyway -- so DONE is
  # now EARNED: the newborn must write its first heartbeat within the
  # estate's own staleness window (STALE_S, ~60x the measured 3.05s healthy
  # start) or the stop lands in the DGN-1506 hint lane and reaches the owner
  # through the leader's push.sh. Report-only: a failed verdict tears
  # nothing down (owner ruling -- the newborn stays on disk for inspection).
  if ! mint_viability_verdict "$ROOT" "$VERDICT_SINCE" "${VERDICT_TIMEOUT_OPT:-$(mint_verdict_stale_s "$ROOT")}"; then
    _gate_stop mint.stillborn "viability verdict (DGN-1606): the newborn at $ROOT never wrote a first bridge heartbeat within the wait window -- launchd accepted the units but no bridge came up (read-only diagnosis above). REPORT-ONLY: the finding is REPORTED; NOTHING was torn down, restarted or repaired. The newborn stays on disk exactly as born (owner ruling 2026-09-20). Report the finding and the read-only diagnosis above to the owner for human inspection, including $ROOT/.telegram_bot/logs/. Read-only re-judge: '$0 verdict --root $ROOT'"
  fi
  # DGN-1608: alive is not clean -- the 2026-09-19 mint was alive with four
  # defects and no surface said a word. Judge the cheap machine-decidable
  # birth defects now; a failure stops through the SAME hint lane
  # (report-only, nothing torn down) and, once the hint is delivered, closes
  # the ordered-mint obligation as the born-alive-and-wrong verdict.
  if ! mint_wrongness_verdict "$ROOT" "$INSTANCE_ROOT"; then
    _gate_stop mint.born-alive-and-wrong "wrongness verdict (DGN-1608): the newborn at $ROOT is ALIVE (bridge heartbeat confirmed above) but failed the birth-defect checks (findings above: fw stamps / update-channel coherence). REPORT-ONLY: the findings are REPORTED; NOTHING was torn down, restarted or repaired. The instance keeps running exactly as born (owner ruling 2026-09-20). Report the findings and $ROOT/.instance.conf to the owner for human inspection. Read-only re-judge: '$0 verdict --root $ROOT --instance-root ${INSTANCE_ROOT:-<ordering-root>}'"
  fi
  # DGN-1608: `clean` is EARNED here and ONLY here -- both verdicts above
  # returned 0. The EXIT trap reads this to close the ordered-mint
  # obligation as done; a bare rc=0 never closes it (the --no-start branch
  # above exits 0 with no verdict and the obligation deliberately stays
  # open until an explicit start + re-judge).
  MINT_TSL_VERDICT="clean"
  echo "[pipeline] DONE -- instance ready at $ROOT"
  exit 0
fi

# ---- mint mode -------------------------------------------------------------
[[ -n "$SLUG" ]] || { echo "ERROR: --slug <slug> required" >&2; exit 1; }
[[ "$SLUG" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || {
  echo "ERROR: slug must be ascii kebab-case (^[a-z][a-z0-9-]{1,30}$): $SLUG" >&2
  exit 1; }

# Defaults resolved from the CALLING instance (--instance-root). When absent,
# each instance-dependent read SKIPS with an explicit log line (DGN-366 L1)
# and flags/defaults take over.
#
# DGN-1513 F-2 (grill G8 break 2) -- TWO repo facts, deliberately separated:
#   MACHINERY_REPO  = where the mint MACHINERY is read from: mint.sh and the
#                     agents/.template preflight both come from THIS script's
#                     own tree. When this tree is a tag-pinned consume
#                     checkout, the WHOLE mint (wrapper, pack_install, mint.sh,
#                     instance template) composes from ONE tag -- previously
#                     only the wrapper/pack_install were script-relative while
#                     MINT_SH and the template followed DOGANY_REPO_ROOT, so a
#                     "tag-pinned" mint silently composed two trees (the exact
#                     tag skew SPEC v2 SS4-6 claims to eliminate).
#   REPO_ROOT       = what the CHILD records as its canonical repo
#                     (DOGANY_REPO_ROOT, the crew-shared channel fetch/archive
#                     source, from the calling instance's conf). The child
#                     must NEVER inherit a parent-owned .dogany-consume
#                     worktree as its repo root -- mint.sh stamps its own tree
#                     (mint.sh:37,:618), so the restamp after the mint call
#                     below is what keeps the two facts apart.
REPO_ROOT=""
if [[ -n "$INSTANCE_ROOT" ]]; then
  INSTANCE_CONF="$INSTANCE_ROOT/.instance.conf"
  REPO_ROOT="$(conf_get "$INSTANCE_CONF" DOGANY_REPO_ROOT)"
else
  echo "[mint_run] SKIP instance conf read (--instance-root not supplied): child records this machinery's repo as canonical" >&2
fi
REPO_ROOT="${REPO_ROOT:-$MACHINERY_REPO}"

# --- DGN-1575: catalog-brief gate, PACKLESS seat ----------------------------
# The harm path the ticket measured: skipping the step-1 catalog branch
# routes to the custom/freeform lane, which mints WITHOUT --pack -- the
# owner asks for a health trainer and gets a blank shell (DGN-342 family,
# one layer up: the lost seed is a whole product pack, and pack_health is
# also the vessel a domain agent's archive handover lands in). This seat refuses a
# REAL standalone mint whose slug/role is claimed by an official catalog
# pack unless the catalog-brief stamp proves the owner was shown that
# pack's choice screen (picking custom AFTER seeing it is exactly what the
# stamp then covers). Skips, all LOUD, never silent:
#   --dry-run            preflight only, nothing written (SKILL step 6b runs
#                        dry first; the real run right after is gated)
#   --txn-open           pipeline's own internal mint subcall -- the
#                        pipeline seat above already judged the pack path
#   no --instance-root   the stamp is instance-resident state (DGN-366 L1)
#   no catalog on disk   nothing exists to show (sandbox trees)
if [[ "$DRY" -eq 0 && "$TXN_OPEN" -eq 0 ]]; then
  CBM_CATALOG="${CATALOG_OPT:-$REPO_ROOT/packs/catalog.json}"
  [[ -f "$CBM_CATALOG" ]] || CBM_CATALOG="$MACHINERY_REPO/packs/catalog.json"
  if [[ ! -f "$CBM_CATALOG" ]]; then
    echo "[mint_run] SKIP catalog-brief gate: no catalog on disk ($CBM_CATALOG)" >&2
  elif [[ -z "$INSTANCE_ROOT" ]]; then
    echo "[mint_run] SKIP catalog-brief gate (--instance-root not supplied): stamp is instance-resident (DGN-366 L1); operator lane" >&2
  else
    CBM_RC=0
    CBM_OUT="$("$PACK_PY3" "$SCRIPT_DIR/lib/catalog_brief.py" gate \
      --catalog "$CBM_CATALOG" \
      --stamp "$INSTANCE_ROOT/config/mint-catalog-brief.json" \
      --ttl "${DOGANY_CATALOG_BRIEF_TTL_S:-21600}" \
      --haystack "$SLUG $ROLE_PROSE" 2>&1)" || CBM_RC=$?
    printf '%s\n' "$CBM_OUT" | sed 's/^/[mint_run] catalog-brief-gate /' >&2
    if [[ "$CBM_RC" -eq 20 ]]; then
      _gate_stop mint.catalog-brief "catalog-brief gate (DGN-1575): packless mint '$SLUG' names a domain an official catalog pack claims, and no fresh catalog-brief stamp covers that pack -- the owner was never shown the official-vs-custom choice screen and would receive a blank agent. Run mint_run.sh catalog-brief --query '<the owner's own domain words>' --instance-root $INSTANCE_ROOT, render its stdout VERBATIM, collect the choice, then re-run. Never work around this stop"
    elif [[ "$CBM_RC" -ne 0 ]]; then
      echo "ERROR: catalog-brief gate errored (rc=$CBM_RC) -- fail-closed before any write" >&2
      exit 1
    fi
  fi
fi
# User label is a PER-AGENT axis and is NEVER inherited from the calling
# instance (owner decision 2026-09-15): the parent's DOGANY_USER_LABEL is its
# own address term for the owner, not the crew's. The child is minted with the
# neutral default and onboarding Q3 fills the real term. REPO_ROOT above IS
# still inherited (crew-shared physical repo) -- do not couple the two.
USER_LABEL="you"
MINT_SH="$MACHINERY_REPO/scripts/mint.sh"

if [[ -z "$LANG_OPT" ]]; then
  if [[ -n "$INSTANCE_ROOT" ]]; then
    LANG_OPT="$(conf_get "$INSTANCE_ROOT/config/agent.conf" AGENT_LANG)"
  else
    echo "[mint_run] SKIP lang read from instance agent.conf (--instance-root not supplied): default en unless --lang given" >&2
  fi
fi
LANG_OPT="${LANG_OPT:-en}"

ENV_FILE=""
[[ -n "$INSTANCE_ROOT" ]] && ENV_FILE="$INSTANCE_ROOT/.telegram_bot/.env"
if [[ -z "$OWNER_IDS" ]]; then
  if [[ -n "$ENV_FILE" ]]; then
    OWNER_IDS="$(conf_get "$ENV_FILE" ALLOWED_USER_IDS | cut -d, -f1)"
    # DGN-1856: a claimed main (install.sh + /claim) keeps ALLOWED_USER_IDS
    # empty and records its owner in .telegram_bot/owner.lock. Read it with
    # the instance's own ownership.py (the writer), never a second parser.
    if [[ -z "$OWNER_IDS" && -f "$INSTANCE_ROOT/bridge/ownership.py" ]]; then
      OWNER_IDS="$("${PACK_PY3:-python3}" - "$INSTANCE_ROOT/bridge" "$INSTANCE_ROOT/.telegram_bot" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
from ownership import read_owner_lock
oid = read_owner_lock(sys.argv[2])
print(oid if oid is not None else "")
PY
)"
      [[ -n "$OWNER_IDS" ]] && echo "[mint_run] owner id read from claimed owner.lock (ALLOWED_USER_IDS empty)" >&2
    fi
  else
    echo "[mint_run] SKIP owner-id read from instance .env (--instance-root not supplied): pass --owner-id" >&2
  fi
fi
if [[ -z "$AGENT_TZ" ]]; then
  if [[ -n "$ENV_FILE" ]]; then
    AGENT_TZ="$(conf_get "$ENV_FILE" TZ)"
  else
    echo "[mint_run] SKIP tz read from instance .env (--instance-root not supplied): default Asia/Seoul unless --tz given" >&2
  fi
  AGENT_TZ="${AGENT_TZ:-Asia/Seoul}"
fi

# The parent instance's explicit integer-hour declaration is carried as a
# FALLBACK only: mint.sh (routines/lib/tz_offset.sh order, DGN-1704) prefers
# the crew value and owns the host fallback, so a missing or
# reader-incompatible parent value never turns into a literal 0.
PARENT_TZ_OFFSET_HOURS=""
if [[ -n "$INSTANCE_ROOT" ]]; then
  _parent_tz_offset="$(conf_get "$INSTANCE_ROOT/config/agent.conf" AGENT_TZ_OFFSET_HOURS)"
  if [[ -n "$_parent_tz_offset" ]]; then
    if [[ "$_parent_tz_offset" =~ ^-?[0-9]+$ ]] \
       && (( _parent_tz_offset >= -12 && _parent_tz_offset <= 14 )); then
      PARENT_TZ_OFFSET_HOURS="$_parent_tz_offset"
    else
      echo "[mint_run] WARN parent AGENT_TZ_OFFSET_HOURS='$_parent_tz_offset' is not reader-compatible; mint.sh will use the crew value or host localtime" >&2
    fi
  else
    echo "[mint_run] SKIP parent AGENT_TZ_OFFSET_HOURS (undeclared): mint.sh will use the crew value or host localtime" >&2
  fi
fi

# DGN-590 MAJOR-2: derive the child's model picker list from the parent's
# PLAN= single source (config/agent.conf), replacing the old DGN-281 BRIDGE_MODELS
# .env inheritance (which broke the PLAN single-source contract -- e.g. a domain agent had a
# cached 4-model .env != a pro source). When the parent carries PLAN, derive via
# plan_bridge_models and propagate PLAN to the child at stamp time. When the parent
# is a pre-PLAN generation (no PLAN), fall back to the legacy .env inheritance
# (child then gets no PLAN, consistent with the P2-0 exception rule) + WARN.
PARENT_PLAN=""
[[ -n "$INSTANCE_ROOT" ]] && PARENT_PLAN="$(conf_get "$INSTANCE_ROOT/config/agent.conf" PLAN)"
HOST_BRIDGE_MODELS=""
if [[ -n "$PARENT_PLAN" ]]; then
  HOST_BRIDGE_MODELS="$(plan_bridge_models "$PARENT_PLAN")"
else
  # Parent has no PLAN (pre-PLAN generation): legacy .env inheritance path.
  if [[ -n "$ENV_FILE" ]]; then
    HOST_BRIDGE_MODELS="$(conf_get "$ENV_FILE" BRIDGE_MODELS)"
    if [[ -z "$HOST_BRIDGE_MODELS" ]]; then
      echo "[mint_run] WARN parent has no PLAN and BRIDGE_MODELS not found in $ENV_FILE; defaulting to sonnet,opus,haiku" >&2
    fi
  else
    echo "[mint_run] SKIP models read from instance .env (--instance-root not supplied): defaulting to sonnet,opus,haiku" >&2
  fi
  echo "[mint_run] WARN parent config/agent.conf has no PLAN= -- child PLAN left unstamped (P2-0 exception)" >&2
  HOST_BRIDGE_MODELS="${HOST_BRIDGE_MODELS:-sonnet,opus,haiku}"
fi

# DGN-1483: agents live under ~/.dogany/agents/<slug>, slug verbatim
# (no case mangling -- the derived path must be traceable to the slug).
if [[ -z "$ROOT" ]]; then
  ROOT="$HOME/.dogany/agents/$SLUG"
fi

# Preflight.
fail=0
if ! _mt_transcript_clean "$ROOT"; then
  if [[ ! -e "$ROOT" ]]; then
    mint_transcript_clear_aborted "$ROOT" "$HOME/.dogany/agents/.retired" || fail=1
  fi
fi
_mt_transcript_clean "$ROOT" || fail=1
[[ -x "$MINT_SH" ]] || { echo "[preflight] FAIL mint.sh not found/executable: $MINT_SH" >&2; fail=1; }
# Template comes from the machinery's OWN tree (F-2: one tag, one tree --
# same source as MINT_SH above), never from the recorded canonical repo.
[[ -d "$MACHINERY_REPO/agents/.template" ]] || { echo "[preflight] FAIL template missing: $MACHINERY_REPO/agents/.template" >&2; fail=1; }
if [[ -e "$ROOT" && -n "$(ls -A "$ROOT" 2>/dev/null)" ]]; then
  echo "[preflight] FAIL target dir not empty: $ROOT" >&2; fail=1
fi
command -v sqlite3 >/dev/null 2>&1 || echo "[preflight] WARN sqlite3 missing (lifekit.db init will be skipped)" >&2
# Q4 / DGN-475: mirror the token dry-run pattern for owner-id. In dry-run,
# an unresolved owner-id (e.g. parent lacks .telegram_bot/.env in a template
# context) is a WARN, not a FAIL -- matching the token gate treatment below.
if [[ -z "$OWNER_IDS" ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    echo "[preflight] WARN owner id unresolved (ok for dry-run; pass --owner-id for real mint)" >&2
  else
    echo "[preflight] FAIL owner id unresolved (pass --owner-id)" >&2; fail=1
  fi
fi
if [[ -z "${DOGANY_BOT_TOKEN:-}" ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    echo "[preflight] WARN DOGANY_BOT_TOKEN not set (ok for dry-run)" >&2
  else
    echo "[preflight] FAIL DOGANY_BOT_TOKEN env var required" >&2; fail=1
  fi
fi
[[ "$fail" -eq 0 ]] || exit 1

# Last pre-write seam, after catalog/preflight and before dry-run or journal.
_resolve_mint_channel

ARGS=(--root "$ROOT" --name "$SLUG" --label "$SLUG" --user "$USER_LABEL"
      --lang "$LANG_OPT" --owner-id "$OWNER_IDS" --tz "$AGENT_TZ"
      --models "$HOST_BRIDGE_MODELS")
[[ -n "$PARENT_TZ_OFFSET_HOURS" ]] && ARGS+=(--tz-offset-parent-hours "$PARENT_TZ_OFFSET_HOURS")
CHANNEL_ARGS=()
[[ -z "$CHANNEL_OPT" ]] || CHANNEL_ARGS=(--channel "$CHANNEL_OPT")
ARGS+=(${CHANNEL_ARGS[@]+"${CHANNEL_ARGS[@]}"})
[[ "$CORE_ONLY" -eq 1 ]] && ARGS+=(--core-only)
[[ "${NO_VENV:-0}" -eq 1 ]] && ARGS+=(--no-venv)

if [[ "$DRY" -eq 1 ]]; then
  echo "[dry-run] plan: DOGANY_BOT_TOKEN=*** $MINT_SH ${ARGS[*]}"
  [[ -n "$ROLE_PROSE" ]] && echo "[dry-run] --role will stamp AGENT.md: $ROLE_PROSE"
  echo "[dry-run] .env render smoke (token masked):"
  "$MINT_SH" --print-env --lang "$LANG_OPT" --owner-id "$OWNER_IDS" --tz "$AGENT_TZ" \
    --models "$HOST_BRIDGE_MODELS" ${CHANNEL_ARGS[@]+"${CHANNEL_ARGS[@]}"} \
    | sed -E 's/^([A-Z0-9_]*(TOKEN|PASSWORD|SECRET|KEY))=..*/\1=***masked***/'
  echo "[dry-run] OK -- nothing written"
  exit 0
fi

# Pre-record mint journal BEFORE any side effect (crash-safety). Persists all
# post-mint stamp inputs so a crashed session can recover them. The journal
# is instance-resident state: without --instance-root it SKIPS (explicit).
JOURNAL_FILE=""
if [[ -n "$JOURNAL_DIR" ]]; then
  mkdir -p "$JOURNAL_DIR"
  JOURNAL_FILE="$JOURNAL_DIR/$SLUG.json"
  JOURNAL_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  "$PACK_PY3" - "$JOURNAL_FILE" "$SLUG" "$ROOT" "$ROLE_PROSE" "$MODEL_OPT" "$JOURNAL_TS" <<'PYEOF'
import json, sys
journal_path, slug, root, role_prose, model, ts = sys.argv[1:7]
data = {
    "slug": slug,
    "root": root,
    "role_prose": role_prose,
    "model": model,
    "timestamp": ts,
}
with open(journal_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[mint_run] journal written: {journal_path}")
PYEOF
else
  echo "[mint_run] SKIP mint journal (--instance-root not supplied; journal is instance-resident) -- crash recovery unavailable for this run" >&2
fi

# Real mint. Token stays in env; mint.sh never prints it.
"$MINT_SH" "${ARGS[@]}"

# DGN-1513 F-2: mint.sh stamps DOGANY_REPO_ROOT from its OWN tree
# (mint.sh:37,:618). When this machinery runs from a consume checkout, that
# would hand the child the parent-owned .dogany-consume worktree as its
# canonical repo -- restamp with the crew-shared canonical repo resolved
# above. No-op when machinery tree == canonical repo (the classic layout).
if [[ "$REPO_ROOT" != "$MACHINERY_REPO" ]]; then
  _conf_upsert "$ROOT/.instance.conf" DOGANY_REPO_ROOT "$REPO_ROOT"
  echo "[mint_run] child DOGANY_REPO_ROOT restamped to canonical repo: $REPO_ROOT (machinery ran tag-pinned from $MACHINERY_REPO)"
fi

# DGN-1506: open the unfinished-mint transaction the moment an instance tree
# exists. This marker is the POSITIVE machine fact `teardown` needs before it
# will touch anything -- without it teardown falls back to "never ran", which
# cannot distinguish a half-mint from an agent that is merely stopped. It is
# removed below on success, or by the pipeline when --txn-open defers the
# close to the end of the whole mint -> pack_install transaction.
mint_teardown_txn_open "$ROOT" "$SLUG" "$MODE" "$INSTANCE_ROOT" \
  || echo "[mint_run] WARN could not open teardown marker at $ROOT (teardown falls back to never-ran evidence)" >&2

# DGN-590 P5-b: stamp domain class + lifekit off on the freshly minted child
# (mint_run mints only domain staff agents). Mirrors install.sh dgn227_postmint
# domain arm; seals the generator hole. Runs BEFORE the role stamp. Idempotent
# (constant stamp, journal-independent -> safe to replay in recover mode).
_stamp_domain_class "$ROOT"
# Propagate the parent's PLAN to the child (single-source contract). Only when
# the parent carried PLAN; a pre-PLAN parent leaves the child unstamped (P2-0).
if [[ -n "$PARENT_PLAN" ]]; then
  _conf_upsert "$ROOT/config/agent.conf" PLAN "$PARENT_PLAN"
fi
_carry_owner_profile "$ROOT" "mint_run"

# Domain-role stamping (DGN-277 finding 3): if --role was supplied, fill the
# Primary-focus placeholder and excise Q6 from the onboarding block.
# Runs FIRST among post-mint stamps (before model stamp) so that the most
# critical identity data is written immediately after the framework call.
# NOTE: the fuller Role-section rewrite (generic front-door bullet -> domain line)
# is intentionally NOT done here -- keep it a post-mint specialization step.
if [[ -n "$ROLE_PROSE" ]]; then
  _stamp_role "$ROOT" "$ROLE_PROSE" "role-stamp" || {
    STATUS=$?
    echo "[mint_run] ERROR: role-stamp step failed (exit $STATUS)" >&2; exit $STATUS
  }
fi

# Model stamp: write the requested model into the new instance's settings.json.
# Uses python3 JSON round-trip (atomic: load -> mutate -> write).
INSTANCE_SETTINGS="$ROOT/.claude/settings.json"
if [[ -f "$INSTANCE_SETTINGS" ]]; then
  "$PACK_PY3" - "$INSTANCE_SETTINGS" "$MODEL_OPT" <<'PYEOF'
import json, sys
settings_path, model = sys.argv[1], sys.argv[2]
with open(settings_path, encoding="utf-8") as f:
    data = json.load(f)
data["model"] = model
with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"[mint_run] model set to {model!r} in settings.json")
PYEOF
else
  echo "[mint_run] WARN settings.json not found at $INSTANCE_SETTINGS; skipping model stamp" >&2
fi

# Cleanup journal on fully successful stamp completion.
if [[ -n "$JOURNAL_FILE" ]]; then
  rm -f "$JOURNAL_FILE"
  echo "[mint_run] journal removed: $JOURNAL_FILE"
fi

# DGN-1506: close the unfinished-mint transaction -- UNLESS a pipeline owns it.
# A bare `mint` is complete here; a pipeline still has pack_install ahead of
# it, and an instance that dies in pack_install (the measured 2026-09-16
# requires_kit case) must still read as unfinished.
if [[ "$TXN_OPEN" -eq 1 ]]; then
  echo "[mint_run] teardown marker left OPEN for the caller's pipeline"
else
  mint_teardown_txn_close "$ROOT"
fi

echo
echo "[mint_run] minted -> $ROOT"
[[ -n "$ROLE_PROSE" ]] && echo "[mint_run] role stamped: $ROLE_PROSE"
echo "[mint_run] next: user-approved 'mint_run.sh start --root $ROOT' to load launchd,"
echo "[mint_run]       then hand off to the new bot (greeting + onboarding naming)."
